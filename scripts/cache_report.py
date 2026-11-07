"""
cache_report — per-day, per-persona API usage with Anthropic prompt-cache
stats, so you can check whether cache breakpoints are landing without
digging through logs.

Reads the `usage` table from each persona's SQLite DB (personas are found
the same way cost_report.py finds them). The table carries
cache_read_input_tokens + cache_creation_input_tokens alongside the regular
prompt/completion counts; rows written before those columns existed show
zeros, which means "no data yet," not "cache off."

Usage (from the Pulse folder):
    py scripts/cache_report.py                        # everyone, last 7 days
    py scripts/cache_report.py --days 14              # 14 days
    py scripts/cache_report.py <persona>              # one persona
    py scripts/cache_report.py <persona> --days 30    # one persona, last 30 days
    py scripts/cache_report.py --no-color             # plain text
"""

import argparse
import os
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

if os.name == "nt":
    os.system("")

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cost_report as cr  # noqa: E402

PULSE_ROOT = cr.PULSE_ROOT

# ANSI
DIM = "\033[2m"
BOLD = "\033[1m"
RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
CYAN = "\033[36m"
RESET = "\033[0m"


def find_db(folder: str) -> Path | None:
    """Each persona's DB lives at personas/<folder>/data/<folder>.db."""
    p = cr.persona_db(folder)
    return p if p.exists() else None


def query_usage(db_path: Path, days: int) -> tuple[list[dict], bool]:
    """Return per-day rows for this persona's DB, newest first."""
    since = (date.today() - timedelta(days=days - 1)).isoformat()
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(usage)").fetchall()}
        has_cache = ("cache_read_input_tokens" in cols) or ("cache_read_tokens" in cols)
        c_read, c_write, _, _ = cr.usage_cache_selects(cols)
        sql = (
            "SELECT date, model, MAX(provider) AS provider, SUM(calls) AS calls, "
            "       SUM(prompt_tokens) AS prompt_tokens, "
            "       SUM(completion_tokens) AS completion_tokens, "
            f"      {c_read} AS cache_read, {c_write} AS cache_write "
            "FROM usage WHERE date >= ? GROUP BY date, model "
            "ORDER BY date DESC"
        )
        rows = [dict(r) for r in conn.execute(sql, (since,)).fetchall()]
    finally:
        conn.close()
    return rows, has_cache


def fmt_int(n: int) -> str:
    return f"{n:,}"


def as_cost_row(r: dict) -> dict:
    """Shape a row the way cost_report's helpers expect."""
    return {"provider": (r.get("provider") or "").lower(),
            "prompt": r["prompt_tokens"] or 0,
            "cache_read": r["cache_read"] or 0,
            "cache_write": r["cache_write"] or 0}


def color_pct(pct: int | None, color: bool) -> str:
    if pct is None:
        return f"{DIM}—{RESET}" if color else "—"
    s = f"{pct}%"
    if not color:
        return s
    if pct >= 60:
        return f"{GREEN}{s}{RESET}"
    if pct >= 30:
        return f"{YELLOW}{s}{RESET}"
    if pct > 0:
        return f"{RED}{s}{RESET}"
    return f"{DIM}{s}{RESET}"


def report(folder: str, name: str, days: int, color: bool):
    db_path = find_db(folder)
    if not db_path:
        print(f"{DIM}{name}: no db at personas/{folder}/data/{folder}.db{RESET}")
        return
    try:
        rows, has_cache = query_usage(db_path, days)
    except Exception as e:
        print(f"{RED}{name}: query failed — {e}{RESET}")
        return

    header = f"\n{BOLD}{CYAN}── {name} ({folder}){RESET}"
    if not has_cache:
        header += f"  {DIM}[cache columns absent, will populate after restart]{RESET}"
    print(header)

    if not rows:
        print(f"  {DIM}(no usage rows){RESET}")
        return

    # Column widths
    print(
        f"  {BOLD}{'date':<10}  {'model':<32}  "
        f"{'calls':>6}  {'in (un)':>11}  {'out':>9}  "
        f"{'cache r':>10}  {'cache w':>9}  {'hit':>5}{RESET}"
    )
    print(f"  {DIM}{'─' * 100}{RESET}")
    for r in rows:
        day = r["date"]
        model = (r["model"] or "?")[:32]
        calls = r["calls"] or 0
        ctok = r["completion_tokens"] or 0
        cr_row = as_cost_row(r)
        ptok = cr.fresh_input(cr_row)
        cread = cr_row["cache_read"]
        ccreate = cr_row["cache_write"]
        hit = cr.hit_pct(cr_row)
        print(
            f"  {day:<10}  {model:<32}  "
            f"{calls:>6}  {fmt_int(ptok):>11}  {fmt_int(ctok):>9}  "
            f"{fmt_int(cread):>10}  {fmt_int(ccreate):>9}  "
            f"{color_pct(hit, color):>5}"
        )


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("persona", nargs="?", help="persona folder name under personas/. Omit for all.")
    ap.add_argument("--days", type=int, default=7, help="how many days back to report (default 7)")
    ap.add_argument("--no-color", action="store_true", help="disable ANSI colors")
    args = ap.parse_args()

    color = not args.no_color
    if args.no_color:
        global DIM, BOLD, RED, GREEN, YELLOW, CYAN, RESET
        DIM = BOLD = RED = GREEN = YELLOW = CYAN = RESET = ""

    personas = cr.discover_personas()
    targets = ([(args.persona, personas.get(args.persona, args.persona))]
               if args.persona else list(personas.items()))

    print(
        f"\n{BOLD}Cache Report{RESET}  "
        f"{DIM}(last {args.days} days, newest first){RESET}\n"
        f"{DIM}'in (un)' = uncached input tokens. Cache hit % = cache_read / "
        f"total input (Anthropic: uncached + cache_read + cache_create).{RESET}"
    )
    if not targets:
        print(f"\n{DIM}No persona folders found under {cr.PERSONAS_DIR}.{RESET}")
    for folder, name in targets:
        report(folder, name, args.days, color)
    print()


if __name__ == "__main__":
    main()
