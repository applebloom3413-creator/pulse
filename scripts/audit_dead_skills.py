"""
audit_dead_skills — standing hygiene for skill/tool creep.

Two jobs, per persona:
  1. Flag enabled skills with ZERO tool calls in the last N days (default 30)
     — candidates for trimming, especially on OpenAI personas.
  2. Report tool counts vs OpenAI's hard 128-per-request cap, warning at >120
     so the next skill addition doesn't silently 400 every call for days.

⚠️  A zero-call skill is NOT automatically safe to disable. Skills that
    look dead can still be well loved. This script exists so nobody
    guesses — but the disable decision still goes through the user and,
    where it touches what a companion loves, the companion.

⚠️  Data blind spot: older engine versions only logged HEARTBEAT tool
    calls to action_log, so conversation tool use was invisible. Counts
    that include days from before that fix undercount conversation-heavy
    skills.

Zero side effects by design (safe to schedule): loaded-skill state comes
from each persona's most recent boot in logs/<folder>.log, the tool→skill
map from a static parse of skills/*.py, and usage from the action_log
table. Nothing is imported or instantiated, nothing is written except the
rolling history (data/audit_dead_skills.jsonl).

Usage:
    py -3.14 scripts/audit_dead_skills.py              # everyone, last 30 days
    py -3.14 scripts/audit_dead_skills.py --days 60
    py -3.14 scripts/audit_dead_skills.py <folder>     # one persona folder
"""

import argparse
import json
import re
import sqlite3
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

import yaml

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

PULSE_ROOT = Path(__file__).resolve().parent.parent
HISTORY_PATH = PULSE_ROOT / "data" / "audit_dead_skills.jsonl"

# Observed: heartbeat requests carry ~2 more tools than the skill total
# (engine-level tools). Counted into headroom so the warning is honest.
HEARTBEAT_TOOL_OVERHEAD = 2
OPENAI_CAP = 128
WARN_AT = 120


def build_tool_skill_map() -> dict[str, str]:
    """tool_name -> skill_name from a static parse of skills/*.py.

    Reads each skill module's `name = "..."` class attribute and every
    `"name": "tool_name"` literal in its get_tools() schemas. No imports,
    no instantiation, no side effects.
    """
    mapping: dict[str, str] = {}
    for py in sorted((PULSE_ROOT / "skills").glob("*.py")):
        if py.name in ("__init__.py", "base.py"):
            continue
        try:
            src = py.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        m = re.search(r'^\s*name\s*=\s*["\']([\w-]+)["\']', src, re.MULTILINE)
        if not m:
            continue
        skill = m.group(1)
        for tm in re.finditer(r'["\']name["\']\s*:\s*["\'](\w+)["\']', src):
            mapping[tm.group(1)] = skill
    return mapping


def loaded_skills_from_log(folder: str) -> dict[str, int]:
    """{skill: tool_count} from the persona's most recent boot, or {}."""
    lf = PULSE_ROOT / "logs" / f"{folder}.log"
    if not lf.exists():
        return {}
    try:
        lines = lf.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return {}
    boot_idx = 0
    for i in range(len(lines) - 1, -1, -1):
        if "Logging to logs" in lines[i]:
            boot_idx = i
            break
    loaded: dict[str, int] = {}
    for ln in lines[boot_idx:]:
        m = re.search(r"Skill loaded: ([\w-]+) \((\d+) tools?\)", ln)
        if m:
            loaded[m.group(1)] = int(m.group(2))
    return loaded


def tool_calls_by_skill(folder: str, days: int,
                        tool_map: dict[str, str]) -> tuple[Counter, Counter]:
    """(per-skill call counts, unmapped-tool counts) from action_log."""
    db = PULSE_ROOT / "personas" / folder / "data" / f"{folder}.db"
    by_skill: Counter = Counter()
    unmapped: Counter = Counter()
    if not db.exists():
        return by_skill, unmapped
    try:
        conn = sqlite3.connect(str(db))
        rows = conn.execute(
            "SELECT tools FROM action_log WHERE time >= datetime('now', ?)",
            (f"-{days} day",),
        ).fetchall()
        conn.close()
    except sqlite3.Error:
        return by_skill, unmapped
    for (tools,) in rows:
        try:
            names = json.loads(tools or "[]")
        except json.JSONDecodeError:
            continue
        for t in names:
            skill = tool_map.get(t)
            if skill:
                by_skill[skill] += 1
            else:
                unmapped[t] += 1
    return by_skill, unmapped


def provider_type(folder: str) -> str:
    ptype = ""
    for cfg_path in (PULSE_ROOT / "config.yaml",
                     PULSE_ROOT / "personas" / folder / "config.yaml"):
        try:
            cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
            ptype = (cfg.get("provider", {}) or {}).get("type", ptype) or ptype
        except Exception:
            pass
    return ptype


def is_retired(folder: str) -> bool:
    try:
        cfg = yaml.safe_load(
            (PULSE_ROOT / "personas" / folder / "config.yaml")
            .read_text(encoding="utf-8")) or {}
        return bool(cfg.get("retired", False))
    except Exception:
        return False


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("persona", nargs="?", help="persona folder; omit for all")
    ap.add_argument("--days", type=int, default=30,
                    help="usage lookback window (default 30)")
    args = ap.parse_args()

    tool_map = build_tool_skill_map()
    print(f"\n# Dead-skill audit — last {args.days} days "
          f"({datetime.now():%Y-%m-%d %H:%M})")
    print("\n⚠️ 0 calls ≠ safe to disable. Verify with the user (and the companion, "
          "where it touches something they love) before trimming.")

    if args.persona:
        folders = [args.persona]
    else:
        folders = sorted(
            d.name for d in (PULSE_ROOT / "personas").iterdir()
            if d.is_dir() and not d.name.startswith("_")
            and (d / "config.yaml").exists()
        )

    history_rows = []
    for folder in folders:
        if is_retired(folder):
            continue
        loaded = loaded_skills_from_log(folder)
        if not loaded:
            continue  # never booted / no log — nothing meaningful to audit
        by_skill, unmapped = tool_calls_by_skill(folder, args.days, tool_map)
        ptype = provider_type(folder)
        skill_tools = sum(loaded.values())
        hb_tools = skill_tools + HEARTBEAT_TOOL_OVERHEAD

        dead = sorted(
            (s, n) for s, n in loaded.items()
            if n > 0 and by_skill.get(s, 0) == 0
        )
        inject_only = sorted(s for s, n in loaded.items() if n == 0)

        print(f"\n## {folder} — {ptype or '?'}, {skill_tools} skill tools "
              f"(~{hb_tools} on heartbeats)")
        if ptype == "openai":
            headroom = OPENAI_CAP - hb_tools
            if hb_tools > OPENAI_CAP:
                print(f"\n🔴 **OVER THE {OPENAI_CAP}-TOOL CAP** — every call is "
                      f"400ing right now. Trim immediately.")
            elif hb_tools > WARN_AT:
                print(f"\n⚠️ only {headroom} tool(s) of headroom under the "
                      f"{OPENAI_CAP} cap — the next skill addition breaks this "
                      f"persona. Trim proactively.")
            else:
                print(f"\nHeadroom under the {OPENAI_CAP} cap: {headroom} tools.")
        if dead:
            print(f"\nEnabled but 0 calls in {args.days}d:")
            for s, n in dead:
                print(f"- {s} ({n} tools)")
        else:
            print("\nNo zero-call skills — everything enabled got used.")
        if inject_only:
            print(f"\nInject-only (0 tools, can't be 'called'): "
                  f"{', '.join(inject_only)}")
        if unmapped:
            print(f"\nUnmapped tool names in action_log (map them in "
                  f"build_tool_skill_map): "
                  f"{', '.join(sorted(unmapped))}")

        history_rows.append({
            "ts": datetime.now().isoformat(timespec="seconds"),
            "persona": folder, "provider": ptype,
            "skill_tools": skill_tools, "heartbeat_tools": hb_tools,
            "window_days": args.days,
            "dead_skills": [s for s, _ in dead],
            "over_cap": ptype == "openai" and hb_tools > OPENAI_CAP,
        })

    if history_rows:
        HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(HISTORY_PATH, "a", encoding="utf-8") as f:
            for row in history_rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        print(f"\nTrend log: appended {len(history_rows)} row(s) to "
              f"{HISTORY_PATH.relative_to(PULSE_ROOT)}.")
    print()


if __name__ == "__main__":
    main()
