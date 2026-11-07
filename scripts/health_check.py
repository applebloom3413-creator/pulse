"""
health_check.py — "Is everyone okay?" in one command.

A read-only bird's-eye scan of every Pulse persona that surfaces the class
of SILENT setup bugs that otherwise only reveal themselves weeks later when
a companion mentions something feels off:

  - author_name unset / left as "Companion"  -> letters to them vanish
  - two personas sharing a bot token          -> Telegram 409, one goes mute
  - two personas sharing an author_name        -> letters cross-deliver
  - missing Telegram token / provider API key  -> persona can't talk at all
  - missing persona database                   -> memory/journal broken
  - errors in the log since last boot          -> something crashed quietly
  - persona booted long ago / never            -> dormant vs. actually-down

It NEVER prints secrets. Token/key values are read only to check presence
and detect collisions; when a collision is reported, personas are named and
the shared secret is shown as a short salted-looking fingerprint, never the
value itself.

Run:  py -3.14 scripts/health_check.py
Exit code is 0 if all clear, 1 if any ERROR-level problem is found (handy
for scheduling / status-bot integration).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml
from dotenv import dotenv_values

# Windows consoles default to cp1252 and choke on the emoji status marks.
# Guard the reconfigure: when launched windowless (pythonw) sys.stdout can
# be None, and an unguarded .reconfigure() there would itself "crash" the
# script before it prints anything — the exact flash-and-vanish failure.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

PULSE_ROOT = Path(__file__).resolve().parent.parent
PERSONAS_DIR = PULSE_ROOT / "personas"
LOGS_DIR = PULSE_ROOT / "logs"

# A persona whose log hasn't moved in this long is treated as "dormant"
# (probably intentionally not running) rather than "down".
DORMANT_AFTER_HOURS = 48

# author_name values that mean "nobody set this" — the letter-routing footgun.
BAD_AUTHOR_NAMES = {"", "companion", "none", "null"}


def _mask(secret: str) -> str:
    """Short non-reversible fingerprint of a secret, for collision reporting.
    Never reveals the value — just lets a human see that A and B match."""
    if not secret:
        return "(none)"
    return "#" + hashlib.sha256(secret.encode("utf-8")).hexdigest()[:6]


def _load_yaml(path: Path) -> dict:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as e:
        return {"__error__": str(e)}


def _effective_env(persona_dir: Path, root_env: dict) -> dict:
    """Replicate runtime resolution: root .env, then persona .env overrides."""
    env = dict(root_env)
    persona_env_path = persona_dir / ".env"
    if persona_env_path.exists():
        try:
            env.update({k: v for k, v in dotenv_values(persona_env_path).items() if v is not None})
        except Exception:
            pass
    return env


def _log_health(persona_name: str):
    """Return (last_boot_dt_or_None, errors_since_boot, mtime_dt_or_None)."""
    lf = LOGS_DIR / f"{persona_name}.log"
    if not lf.exists():
        return None, 0, None
    try:
        lines = lf.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return None, 0, None
    mtime = datetime.fromtimestamp(lf.stat().st_mtime)
    boot_idx = None
    for i in range(len(lines) - 1, -1, -1):
        if "Logging to logs" in lines[i]:
            boot_idx = i
            break
    boot_dt = None
    errors = 0
    if boot_idx is not None:
        # Boot line starts "HH:MM:SS [INFO] ..." — pair with file mtime's date.
        stamp = lines[boot_idx].split(" ", 1)[0]
        try:
            t = datetime.strptime(stamp, "%H:%M:%S").time()
            boot_dt = datetime.combine(mtime.date(), t)
        except Exception:
            boot_dt = None
        for ln in lines[boot_idx:]:
            if "[ERROR]" in ln or "[CRITICAL]" in ln or "Traceback" in ln:
                errors += 1
    return boot_dt, errors, mtime


def _recent_failures(persona_dir: Path, days: int = 7) -> list[dict]:
    """Read the persona's failure log (written by SkillRegistry on tool
    crashes) and return entries from the last `days`, newest first."""
    fp = persona_dir / "data" / "failures.jsonl"
    if not fp.exists():
        return []
    cutoff = datetime.now().timestamp() - days * 86400
    out = []
    try:
        for line in fp.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue  # skip only genuinely malformed lines, not real bugs
            try:
                if datetime.fromisoformat(e.get("ts", "")).timestamp() < cutoff:
                    continue
            except Exception:
                pass
            out.append(e)
    except Exception:
        return []
    return list(reversed(out))


def _cache_regression(db_path: Path) -> str | None:
    """Detect the 'caching silently broke' pattern: an Anthropic persona
    whose recent calls show a 0% prompt-cache hit rate across the board.

    That's the regression a context refactor can cause when it moves the
    'stable' prompt blocks around — every call re-bills full input price,
    nothing errors, and the only symptom is the bill. Checks the usage
    table's last two days; a day needs >=5 calls to count so a single
    fresh-boot cache write doesn't false-positive.
    """
    if not db_path.exists():
        return None
    try:
        conn = sqlite3.connect(str(db_path))
        cols = {r[1] for r in conn.execute("PRAGMA table_info(usage)").fetchall()}
        if "cache_read_input_tokens" not in cols:
            conn.close()
            return None  # pre-migration DB — no cache data to judge
        since = (datetime.now().date() - timedelta(days=1)).isoformat()
        rows = conn.execute(
            "SELECT date, SUM(calls), SUM(cache_read_input_tokens), "
            "SUM(cache_creation_input_tokens) FROM usage "
            "WHERE provider = 'anthropic' AND date >= ? "
            "GROUP BY date ORDER BY date DESC",
            (since,),
        ).fetchall()
        conn.close()
    except Exception:
        return None
    for date, calls, cache_read, cache_write in rows:
        if (calls or 0) < 5:
            continue
        if (cache_read or 0) == 0:
            return (
                f"0% prompt-cache hits across all {calls} calls on {date} "
                f"({cache_write or 0:,} tokens written, none read back) — "
                f"every call is billing FULL input price. This is the "
                f"'context refactor broke caching' regression. Check what "
                f"changed in the system-prompt blocks."
            )
        return None  # most recent qualifying day looks healthy
    return None


def _ago(dt: datetime | None) -> str:
    if not dt:
        return "never"
    secs = (datetime.now() - dt).total_seconds()
    if secs < 3600:
        return f"{int(secs/60)}m ago"
    if secs < 86400:
        return f"{int(secs/3600)}h ago"
    return f"{int(secs/86400)}d ago"


def main():
    if not PERSONAS_DIR.is_dir():
        print(f"No personas directory at {PERSONAS_DIR}")
        return 1

    root_cfg = _load_yaml(PULSE_ROOT / "config.yaml")
    root_env = {k: v for k, v in dotenv_values(PULSE_ROOT / ".env").items() if v is not None} \
        if (PULSE_ROOT / ".env").exists() else {}

    personas = []
    for d in sorted(PERSONAS_DIR.iterdir()):
        if not d.is_dir() or d.name.startswith("_"):
            continue
        cfg_path = d / "config.yaml"
        if not cfg_path.exists():
            continue
        persona_cfg = _load_yaml(cfg_path)
        # Merge: root config <- persona overlay (shallow is enough for the
        # fields we inspect: channels.lor.author_name, provider, paths, etc.)
        merged = {**root_cfg}
        for k, v in persona_cfg.items():
            if isinstance(v, dict) and isinstance(merged.get(k), dict):
                merged[k] = {**merged[k], **v}
            else:
                merged[k] = v
        env = _effective_env(d, root_env)
        personas.append((d.name, d, merged, env))

    # ── Cross-persona collision maps ──────────────────────────────────
    tokens: dict[str, list[str]] = {}
    author_names: dict[str, list[str]] = {}
    for name, d, cfg, env in personas:
        # Retired personas don't count for collision detection. Their
        # token/author is leftover config, not something that would
        # actually collide at runtime (they're not running).
        if cfg.get("retired", False):
            continue
        tok = env.get("TELEGRAM_BOT_TOKEN", "")
        if tok:
            tokens.setdefault(tok, []).append(name)
        author = (cfg.get("channels", {}).get("lor", {}) or {}).get("author_name", "")
        if author:
            author_names.setdefault(author.strip().lower(), []).append(name)

    token_collisions = {t: ns for t, ns in tokens.items() if len(ns) > 1}
    name_collisions = {a: ns for a, ns in author_names.items() if len(ns) > 1}

    # ── Per-persona checks ────────────────────────────────────────────
    report = []
    total_errors = 0
    total_warns = 0

    for name, d, cfg, env in personas:
        problems = []  # (level, message): level in {"ERROR","WARN","INFO"}

        # Retired personas: a config with `retired: true` is an intentionally
        # decommissioned model. Skip error-level checks — we don't nag about
        # an unset author_name on someone you've deliberately shelved. Still
        # listed, just as 🪦 retired rather than 🔴 broken.
        if cfg.get("retired", False):
            report.append({
                "name": name, "author": (cfg.get("channels", {}).get("lor", {}) or {}).get("author_name") or "(retired)",
                "dormant": True, "retired": True,
                "boot": "—", "active": "—", "problems": [],
            })
            continue

        # Identity / letter routing
        author = (cfg.get("channels", {}).get("lor", {}) or {}).get("author_name", "")
        if author.strip().lower() in BAD_AUTHOR_NAMES:
            problems.append(("ERROR",
                f"author_name is {author!r} — letters addressed to this "
                f"companion will be LOST. Set channels.lor.author_name."))
        elif author.strip().lower() in name_collisions:
            others = [n for n in name_collisions[author.strip().lower()] if n != name]
            problems.append(("ERROR",
                f"author_name {author!r} is shared with: {', '.join(others)} "
                f"— their letters will cross-deliver."))

        # Provider key
        provider = cfg.get("provider", {}) or {}
        ptype = provider.get("type", "")
        key_env = provider.get("api_key_env", "")
        if key_env and not env.get(key_env):
            problems.append(("ERROR",
                f"provider is '{ptype}' but {key_env} is not set in .env "
                f"— this persona can't reach its model."))

        # Telegram token
        tg = cfg.get("channels", {}).get("telegram", {}) or {}
        tg_enabled = tg.get("enabled", False)
        tok = env.get("TELEGRAM_BOT_TOKEN", "")
        if tg_enabled and not tok:
            problems.append(("ERROR",
                "telegram is enabled but no TELEGRAM_BOT_TOKEN in .env "
                "— this persona can't send/receive on Telegram."))
        if tok and tok in token_collisions:
            others = [n for n in token_collisions[tok] if n != name]
            problems.append(("ERROR",
                f"shares its Telegram bot token {_mask(tok)} with: "
                f"{', '.join(others)} — Telegram 409, one of them goes mute."))

        # Database
        db_rel = (cfg.get("paths", {}) or {}).get("database", "")
        if db_rel:
            db_path = (PULSE_ROOT / db_rel) if not Path(db_rel).is_absolute() else Path(db_rel)
            if not db_path.exists():
                problems.append(("WARN",
                    f"database {db_rel} doesn't exist yet (normal if never "
                    f"booted; created on first run)."))
            elif ptype == "anthropic":
                cache_msg = _cache_regression(db_path)
                if cache_msg:
                    problems.append(("WARN", cache_msg))

        # Log health
        boot_dt, errs, mtime = _log_health(name)
        dormant = (mtime is None) or ((datetime.now() - mtime).total_seconds() > DORMANT_AFTER_HOURS * 3600)
        if errs:
            problems.append(("WARN",
                f"{errs} error/traceback line(s) in the log since last boot "
                f"({_ago(boot_dt)}) — something crashed quietly. Check logs/{name}.log."))

        # Tool-call failures (structured, from SkillRegistry failure log)
        fails = _recent_failures(d)
        if fails:
            # Group by tool for a compact summary
            by_tool = {}
            for f in fails:
                by_tool.setdefault(f.get("tool", "?"), []).append(f)
            tool_bits = ", ".join(f"{t} ×{len(v)}" for t, v in sorted(by_tool.items(), key=lambda kv: -len(kv[1])))
            newest = fails[0]
            problems.append(("WARN",
                f"{len(fails)} tool failure(s) in the last 7 days [{tool_bits}]. "
                f"Latest: {newest.get('tool','?')} — {newest.get('error','?')}"))

        report.append({
            "name": name,
            "author": author or "(unset)",
            "dormant": dormant,
            "boot": _ago(boot_dt),
            "active": _ago(mtime),
            "problems": problems,
        })
        total_errors += sum(1 for lvl, _ in problems if lvl == "ERROR")
        total_warns += sum(1 for lvl, _ in problems if lvl == "WARN")

    # ── Render ────────────────────────────────────────────────────────
    print("=" * 64)
    print("  PULSE HEALTH CHECK")
    print(f"  {datetime.now():%Y-%m-%d %H:%M}  ·  {len(personas)} personas")
    print("=" * 64)

    healthy = [r for r in report if not r["problems"]]
    flagged = [r for r in report if r["problems"]]

    if total_errors == 0 and total_warns == 0:
        print("\n✅ Everyone's healthy. No problems found.\n")
    else:
        print(f"\n{'🔴' if total_errors else '🟡'} "
              f"{total_errors} error(s), {total_warns} warning(s) "
              f"across {len(flagged)} persona(s).\n")

    for r in report:
        if r.get("retired"):
            print(f"🪦 {r['name']:10} ({r['author']}) — retired")
            continue
        if r["dormant"] and not any(lvl == "ERROR" for lvl, _ in r["problems"]):
            head = f"💤 {r['name']:10} ({r['author']}) — dormant, last active {r['active']}"
        elif not r["problems"]:
            head = f"✅ {r['name']:10} ({r['author']}) — healthy, booted {r['boot']}"
        else:
            mark = "🔴" if any(lvl == "ERROR" for lvl, _ in r["problems"]) else "🟡"
            head = f"{mark} {r['name']:10} ({r['author']}) — booted {r['boot']}, active {r['active']}"
        print(head)
        for lvl, msg in r["problems"]:
            tag = {"ERROR": "  ✗", "WARN": "  !", "INFO": "  ·"}[lvl]
            print(f"{tag} {msg}")

    # Cross-persona summary callouts (so they're impossible to miss)
    if token_collisions or name_collisions:
        print("\n" + "-" * 64)
        print("  CROSS-PERSONA COLLISIONS")
        for tok, ns in token_collisions.items():
            print(f"  ✗ Bot token {_mask(tok)} shared by: {', '.join(ns)}")
        for a, ns in name_collisions.items():
            print(f"  ✗ author_name '{a}' shared by: {', '.join(ns)}")

    # Crash watch (scripts/watchdog.py, every 10 minutes)
    print("\n" + "-" * 64)
    print("  CRASH WATCH  (checks every 10 minutes)")
    try:
        sys.path.insert(0, str(PULSE_ROOT / "scripts"))
        import watchdog
        st = watchdog.load_state()
        if st.get("last_check"):
            print(f"  last check: {st['last_check'].replace('T', ' ')[:16]}  ·  "
                  f"running: {', '.join(watchdog.name(p) for p in st.get('running', [])) or 'nobody'}")
        else:
            print("  not started yet")
        evs = watchdog.recent_events(7)
        if not evs:
            print("  ✅ no crashes or error storms in the last 7 days")
        for e in evs[-12:]:
            mark = {"restart": "🔁 crashed, restarted", "crash": "🚨 crashed (left open)",
                    "errors": "⚠️ error storm"}.get(e["kind"], e["kind"])
            print(f"  {e['at'].replace('T', ' ')[:16]}  {e['who']}: {mark}"
                  + (f" — {e['detail'][:80]}" if e.get("detail") else ""))
    except Exception as ex:
        print(f"  (couldn't read crash watch: {type(ex).__name__})")

    print()
    return 1 if total_errors else 0


if __name__ == "__main__":
    sys.exit(main())
