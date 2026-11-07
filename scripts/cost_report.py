"""
cost_report — per-day, per-persona API cost breakdown in dollars.

Companion to cache_report.py (which shows raw cache stats): this one applies
each model's pricing so you can see what a day cost, per persona, without
opening several provider dashboards.

Personas are discovered from the personas/ folder (every folder not starting
with "_" or "."). The display name is the persona config's
channels.lor.author_name, else persona.yaml / persona.json "name", else the
folder name (same order as the Pulse Hub).

Data source is the `usage` table in each persona's SQLite DB (written live
by core/usage.py), NOT log parsing — the DB already carries per-day
uncached/cached splits and survives log rotation.

Provider semantics differ and the math respects that:
  - anthropic / bedrock: prompt_tokens EXCLUDES cache; input = prompt +
    cache_read + cache_creation (three disjoint buckets, three prices).
    Cache writes are priced from the recorded 5m/1h split when present,
    otherwise from the persona's configured cache_ttl.
  - everything else: prompt_tokens INCLUDES any cached tokens; the cached
    portion bills at the discounted rate when the model has one. Providers
    that don't report cache data simply show 0 cached.

Extended-thinking note: core/llm_anthropic.py doesn't send the `thinking`
API param, so completion_tokens is all visible output. If thinking ever
gets wired in, this report needs a thinking column.

Freshness caveat: the persona DBs run SQLite WAL, and on Windows a running
persona's most recent usage writes can lag for outside readers until the
process cycles. Treat TODAY's (and sometimes yesterday's) numbers as "at
least this much"; older days are stable. Provider consoles usually bucket
days in UTC, so their bars can straddle two local days.

Usage (from the Pulse folder):
    py scripts/cost_report.py                       # everyone, last 7 days
    py scripts/cost_report.py --yesterday           # everyone, yesterday only
    py scripts/cost_report.py <persona> --days 30   # one persona, 30 days
    py scripts/cost_report.py --jsonl               # also append to data/cost_history.jsonl
"""

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

import yaml

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

PULSE_ROOT = Path(__file__).resolve().parent.parent
PERSONAS_DIR = PULSE_ROOT / "personas"
HISTORY_PATH = PULSE_ROOT / "data" / "cost_history.jsonl"

# Providers whose prompt_tokens exclude cached tokens (disjoint buckets).
ANTHROPIC_PROVIDERS = ("anthropic", "bedrock")

# Per 1M tokens, USD. `est` marks numbers taken from provider pages that
# haven't been checked against an actual bill — treat those as ballpark.
# cache_write is keyed by TTL because Anthropic bills 1.25x (5m) / 2.0x (1h)
# of input. Prices change: check them against your provider's pricing page.
PRICING = {
    "claude-opus-4-6": {
        "input": 5.00, "cache_read": 0.50,
        "cache_write": {"5m": 6.25, "1h": 10.00},
        "output": 25.00,
    },
    "claude-sonnet-4-5": {
        "input": 3.00, "cache_read": 0.30,
        "cache_write": {"5m": 3.75, "1h": 6.00},
        "output": 15.00,
    },
    "claude-haiku-4-5": {
        "input": 1.00, "cache_read": 0.10,
        "cache_write": {"5m": 1.25, "1h": 2.00},
        "output": 5.00,
    },
    "gpt-5.1": {"input": 1.25, "cache_read": 0.13, "output": 10.00},
    "gpt-5.4": {"input": 2.50, "cache_read": 0.25, "output": 15.00, "est": True},
    "gpt-4.1": {"input": 2.00, "cache_read": 0.50, "output": 8.00, "est": True},
    "gemini-2.5-pro": {"input": 1.25, "output": 10.00, "est": True},
    "deepseek/deepseek-chat": {"input": 0.27, "output": 1.10, "est": True},
    "moonshotai/kimi-k2.6": {"input": 0.60, "output": 2.50, "est": True},
    "mistralai/mistral-large-2512": {"input": 2.00, "output": 6.00, "est": True},
}


def _read_persona_name(folder: Path) -> str:
    """Display name, in the same order the Pulse Hub uses: the persona
    config's channels.lor.author_name, else persona.yaml / persona.json
    `name`, else the folder name (title-cased)."""
    try:
        cfg = yaml.safe_load((folder / "config.yaml").read_text(encoding="utf-8")) or {}
        author = ((cfg.get("channels") or {}).get("lor") or {}).get("author_name")
        if author and str(author).strip().lower() != "companion":
            return str(author).strip()
    except Exception:
        pass
    for fname in ("persona.yaml", "persona.yml", "persona.json"):
        path = folder / fname
        if not path.exists():
            continue
        try:
            text = path.read_text(encoding="utf-8")
            data = json.loads(text) if path.suffix == ".json" else yaml.safe_load(text)
            name = (data or {}).get("name")
            if isinstance(name, str) and name.strip():
                return name.strip()
        except Exception:
            pass
        break
    return folder.name.title()


def discover_personas() -> dict[str, str]:
    """{folder: display name} for every persona folder under personas/."""
    if not PERSONAS_DIR.is_dir():
        return {}
    return {
        d.name: _read_persona_name(d)
        for d in sorted(PERSONAS_DIR.iterdir())
        if d.is_dir() and not d.name.startswith(("_", "."))
    }


def _openrouter_prices() -> dict:
    """Public OpenRouter price list (no key needed), cached for a day, so
    OpenRouter models not in PRICING still get a (ballpark) price."""
    import time
    import urllib.request
    cache = PULSE_ROOT / "data" / "openrouter_prices.json"
    try:
        if cache.exists() and time.time() - cache.stat().st_mtime < 86400:
            return json.loads(cache.read_text(encoding="utf-8"))
        data = json.load(urllib.request.urlopen("https://openrouter.ai/api/v1/models", timeout=20))["data"]
        prices = {m["id"]: m.get("pricing", {}) for m in data}
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(prices), encoding="utf-8")
        return prices
    except Exception:
        try:
            return json.loads(cache.read_text(encoding="utf-8"))
        except Exception:
            return {}


def model_pricing(model: str) -> dict | None:
    """Exact match first, then prefix match so dated snapshots
    (claude-sonnet-4-5-20250929) resolve to their base model, then substring
    match for provider-prefixed ids (Bedrock's "us.anthropic.claude-...").
    Unknown OpenRouter models ("org/model") fall back to OpenRouter's public
    prices."""
    if model in PRICING:
        return PRICING[model]
    for key, p in PRICING.items():
        if model.startswith(key):
            return p
    for key, p in PRICING.items():
        if key in model:
            return p
    if "/" in model:
        pr = _openrouter_prices().get(model)
        try:
            if pr and float(pr.get("prompt", 0)) > 0:
                return {"input": float(pr["prompt"]) * 1e6, "output": float(pr.get("completion", 0)) * 1e6,
                        "est": True}
        except (TypeError, ValueError):
            pass
    return None


def persona_cache_ttl(folder: str) -> str:
    """Effective provider.cache_ttl for a persona (root config <- overlay)."""
    ttl = "5m"
    for cfg_path in (PULSE_ROOT / "config.yaml",
                     PERSONAS_DIR / folder / "config.yaml"):
        try:
            cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
            ttl = (cfg.get("provider", {}) or {}).get("cache_ttl", ttl) or ttl
        except Exception:
            pass
    return ttl if ttl in ("5m", "1h") else "5m"


def _sum_cols(cols: set, *names: str) -> str:
    """SQL expression summing whichever of `names` exist in the usage table."""
    present = [f"COALESCE({n}, 0)" for n in names if n in cols]
    return f"SUM({' + '.join(present)})" if present else "0"


def usage_cache_selects(cols: set) -> tuple[str, str, str, str]:
    """SELECT expressions for (cache_read, cache_write, cache_write_5m,
    cache_write_1h). Reads the current column names and, if present, the
    older cache_read_tokens / cache_creation_tokens columns too."""
    return (
        _sum_cols(cols, "cache_read_input_tokens", "cache_read_tokens"),
        _sum_cols(cols, "cache_creation_input_tokens", "cache_creation_tokens"),
        _sum_cols(cols, "cache_creation_5m_input_tokens"),
        _sum_cols(cols, "cache_creation_1h_input_tokens"),
    )


def persona_db(folder: str) -> Path:
    """Each persona's DB lives at personas/<folder>/data/<folder>.db."""
    return PERSONAS_DIR / folder / "data" / f"{folder}.db"


def query_usage(folder: str, since: str, until: str) -> list[dict]:
    db = persona_db(folder)
    if not db.exists():
        return []
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(usage)").fetchall()}
        if not cols:
            return []
        c_read, c_write, c_5m, c_1h = usage_cache_selects(cols)
        rows = conn.execute(
            f"SELECT date, provider, model, SUM(calls), SUM(prompt_tokens), "
            f"SUM(completion_tokens), {c_read}, {c_write}, {c_5m}, {c_1h} "
            f"FROM usage WHERE date >= ? AND date <= ? "
            f"GROUP BY date, model ORDER BY date DESC",
            (since, until),
        ).fetchall()
    finally:
        conn.close()
    return [
        {"date": r[0], "provider": (r[1] or "").lower(), "model": r[2] or "?",
         "calls": r[3] or 0, "prompt": r[4] or 0, "output": r[5] or 0,
         "cache_read": r[6] or 0, "cache_write": r[7] or 0,
         "cache_write_5m": r[8] or 0, "cache_write_1h": r[9] or 0}
        for r in rows
    ]


def compute_cost(row: dict, ttl: str) -> tuple[float | None, bool]:
    """Return (cost_usd, is_estimate). None if model pricing unknown."""
    p = model_pricing(row["model"])
    if not p:
        return None, False
    M = 1_000_000
    est = bool(p.get("est"))
    if row["provider"] in ANTHROPIC_PROVIDERS and "cache_write" in p:
        rates = p["cache_write"]
        w5, w1 = row.get("cache_write_5m", 0), row.get("cache_write_1h", 0)
        rest = max(row["cache_write"] - w5 - w1, 0)  # writes without a TTL split
        write_cost = w5 * rates["5m"] + w1 * rates["1h"] + rest * rates[ttl]
        cost = (row["prompt"] * p["input"]
                + row["cache_read"] * p["cache_read"]
                + write_cost
                + row["output"] * p["output"]) / M
    elif row["provider"] in ANTHROPIC_PROVIDERS:
        cost = ((row["prompt"] + row["cache_read"] + row["cache_write"]) * p["input"]
                + row["output"] * p["output"]) / M
        est = True
    else:
        cached = min(row["cache_read"], row["prompt"])  # cached ⊆ prompt
        fresh = row["prompt"] - cached
        cost = (fresh * p["input"]
                + cached * p.get("cache_read", p["input"])
                + row["output"] * p["output"]) / M
    return cost, est


def fresh_input(row: dict) -> int:
    """Uncached input tokens for display."""
    if row["provider"] in ANTHROPIC_PROVIDERS:
        return row["prompt"]
    return row["prompt"] - min(row["cache_read"], row["prompt"])


def hit_pct(row: dict) -> int | None:
    if row["provider"] in ANTHROPIC_PROVIDERS:
        total = row["prompt"] + row["cache_read"] + row["cache_write"]
    elif row["cache_read"]:
        total = row["prompt"]
    else:
        return None
    if total == 0:
        return None
    return row["cache_read"] * 100 // total


def fmt_cost(cost: float | None, est: bool) -> str:
    if cost is None:
        return "?"
    return f"~${cost:,.2f}" if est else f"${cost:,.2f}"


def fmt_tok(n: int) -> str:
    return f"{n:,}"


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("persona", nargs="?",
                    help="persona folder name under personas/. Omit for all.")
    ap.add_argument("--days", type=int, default=7,
                    help="how many days back to report (default 7)")
    ap.add_argument("--yesterday", action="store_true",
                    help="report yesterday only")
    ap.add_argument("--jsonl", action="store_true",
                    help=f"append rows to {HISTORY_PATH.relative_to(PULSE_ROOT)}")
    args = ap.parse_args()

    today = datetime.now().date()
    if args.yesterday:
        since = until = (today - timedelta(days=1)).isoformat()
    else:
        since = (today - timedelta(days=args.days - 1)).isoformat()
        until = today.isoformat()

    personas = discover_personas()
    targets = ([(args.persona, personas.get(args.persona, args.persona))]
               if args.persona else list(personas.items()))

    print(f"\n# Pulse cost report — {since}" +
          (f" to {until}" if until != since else ""))
    print("\nCache-write rate follows the recorded 5m/1h split, else each "
          "persona's configured cache_ttl. `~` marks unverified pricing; `?` "
          "means no pricing on file.")

    day_totals: dict[str, float] = {}
    any_unknown = False
    jsonl_rows = []

    for folder, name in targets:
        rows = query_usage(folder, since, until)
        if not rows:
            continue
        ttl = persona_cache_ttl(folder)
        print(f"\n## {name} ({folder})" +
              (f" — cache_ttl {ttl}" if rows[0]["provider"] in ANTHROPIC_PROVIDERS else ""))
        print("\n| date | model | calls | input (fresh) | cache read | cache write | output | hit | cost |")
        print("|---|---|---:|---:|---:|---:|---:|---:|---:|")
        for r in rows:
            cost, est = compute_cost(r, ttl)
            if cost is None:
                any_unknown = True
            else:
                day_totals[r["date"]] = day_totals.get(r["date"], 0.0) + cost
            hp = hit_pct(r)
            fresh = fresh_input(r)
            print(f"| {r['date']} | {r['model']} | {r['calls']} "
                  f"| {fmt_tok(fresh)} | {fmt_tok(r['cache_read'])} "
                  f"| {fmt_tok(r['cache_write'])} | {fmt_tok(r['output'])} "
                  f"| {'—' if hp is None else str(hp) + '%'} "
                  f"| {fmt_cost(cost, est)} |")
            if args.jsonl:
                jsonl_rows.append({
                    "generated_at": datetime.now().isoformat(timespec="seconds"),
                    "date": r["date"], "persona": name, "folder": folder,
                    "provider": r["provider"], "model": r["model"],
                    "calls": r["calls"], "input_fresh": fresh,
                    "cache_read": r["cache_read"], "cache_write": r["cache_write"],
                    "output": r["output"], "hit_pct": hp,
                    "cache_ttl": ttl if r["provider"] in ANTHROPIC_PROVIDERS else None,
                    "cost_usd": None if cost is None else round(cost, 4),
                    "cost_is_estimate": est,
                })

    if not personas and not args.persona:
        print(f"\nNo persona folders found under {PERSONAS_DIR}.")

    if day_totals:
        print("\n## Daily totals, all personas (priced models only)")
        print("\n| date | total |")
        print("|---|---:|")
        for d in sorted(day_totals, reverse=True):
            print(f"| {d} | ${day_totals[d]:,.2f} |")
        avg = sum(day_totals.values()) / len(day_totals)
        print(f"\nAverage ${avg:,.2f}/day over {len(day_totals)} day(s) "
              f"→ ~${avg * 30:,.0f}/month at this pace.")
    if any_unknown:
        print("\nSome models had no pricing on file (`?`) — their spend is "
              "NOT in the totals above. Add them to PRICING in this script.")

    if args.jsonl and jsonl_rows:
        HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(HISTORY_PATH, "a", encoding="utf-8") as f:
            for row in jsonl_rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        print(f"\nAppended {len(jsonl_rows)} row(s) to {HISTORY_PATH.relative_to(PULSE_ROOT)}.")
    print()


if __name__ == "__main__":
    main()
