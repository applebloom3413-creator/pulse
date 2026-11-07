"""
spending — the friendly money view, built on cost_report.py's math.

  week()      per-companion totals for the last 7 days + yesterday
  spikes()    anyone whose yesterday was way above their usual (2x their
              7-day average AND at least $2 more) — gets a heads-up ping
  summary_lines()  short text for the morning digest / Pulse Hub

Today's numbers can lag a little (see cost_report.py) — so spikes look at
yesterday, which has settled.

Usage (from the Pulse folder):
    py scripts/spending.py
"""

import sys
from datetime import date, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import cost_report as cr  # noqa: E402

def _name(folder: str) -> str:
    """Display name for a persona folder: the persona config's
    channels.lor.author_name, else persona.yaml `name`, else the folder name
    (the same order the Pulse Hub uses, so its money page lines up)."""
    return cr._read_persona_name(cr.PERSONAS_DIR / folder)


def by_day(days: int = 8) -> dict:
    """{companion: {date: dollars}} for priced models."""
    since = (date.today() - timedelta(days=days)).isoformat()
    until = date.today().isoformat()
    out = {}
    for d in sorted((cr.PULSE_ROOT / "personas").glob("*/data")):
        folder = d.parent.name
        if folder.startswith("_"):
            continue
        try:
            rows = cr.query_usage(folder, since, until)
        except Exception:
            continue
        ttl = cr.persona_cache_ttl(folder)
        for r in rows:
            cost, _ = cr.compute_cost(r, ttl)
            if cost:
                out.setdefault(_name(folder), {}).setdefault(r["date"], 0.0)
                out[_name(folder)][r["date"]] += cost
    return out


def week() -> dict:
    days = by_day(8)
    yday = (date.today() - timedelta(days=1)).isoformat()
    week_start = (date.today() - timedelta(days=7)).isoformat()
    per = {n: sum(v for d, v in dd.items() if week_start <= d <= yday) for n, dd in days.items()}
    total = sum(per.values())
    return {"per": dict(sorted(per.items(), key=lambda kv: -kv[1])), "total": total,
            "yesterday": sum(dd.get(yday, 0) for dd in days.values()),
            "per_day": total / 7, "month_pace": total / 7 * 30}


def spikes() -> list[tuple[str, float, float]]:
    """[(companion, yesterday, usual)]"""
    days = by_day(9)
    yday = (date.today() - timedelta(days=1)).isoformat()
    out = []
    for n, dd in days.items():
        before = [v for d, v in dd.items() if d < yday]
        usual = sum(before) / 7 if before else 0
        y = dd.get(yday, 0)
        if y >= 2 * usual and y - usual >= 2:
            out.append((n, y, usual))
    return out


def summary_lines(top: int = 4) -> list[str]:
    w = week()
    lines = [f"💸 Last 7 days: ${w['total']:.2f} (~${w['per_day']:.2f}/day, ~${w['month_pace']:.0f}/month pace)"]
    for n, v in list(w["per"].items())[:top]:
        lines.append(f"   {n}: ${v:.2f}")
    return lines


if __name__ == "__main__":
    print("\n".join(summary_lines(20)))
    for n, y, usual in spikes():
        print(f"⚠️ {n} spent ${y:.2f} yesterday (usually ~${usual:.2f}/day)")
