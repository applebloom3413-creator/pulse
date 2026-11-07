#!/usr/bin/env python3
"""stop_watch_report.py — summary of the Stop Watch log.

Usage (from the Pulse folder):
    py stop_watch_report.py            # last 7 days
    py stop_watch_report.py 30         # last 30 days

Reads data/stop_watch.jsonl (written by core/stop_watch.py).

The question this answers: "Did any companion get clipped lately?" — as a
number and a list, per persona.
"""
import json
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# Single source of truth for the log path lives in core/stop_watch.py.
try:
    from core.stop_watch import LOG_PATH
except Exception:
    # Fallback if run without the pulse root on sys.path: this file sits AT the
    # pulse root, so data/ is right beside it.
    LOG_PATH = Path(__file__).resolve().parent / "data" / "stop_watch.jsonl"


def _is_hard(r: dict) -> bool:
    """A real clip: refusal, error, or dropped call. Prefers the stored flag;
    falls back for any row written before 'hard' existed."""
    if "hard" in r:
        return bool(r["hard"])
    sr = r.get("stop_reason")
    return sr == "refusal" or sr in ("error", "unknown") or bool(r.get("error"))


def _is_soft(r: dict) -> bool:
    """A reply that just didn't end on punctuation, on a normal stop."""
    if "soft" in r:
        return bool(r["soft"])
    return bool(r.get("flag")) and not _is_hard(r)


def main():
    days = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 7
    cutoff = time.time() - days * 86400

    if not Path(LOG_PATH).exists():
        print(f"No stop-watch log yet at:\n  {LOG_PATH}\n")
        print("Nothing recorded — the log starts after the next bot "
              "restart. (It hooks in on load; a running bot won't pick it up "
              "until it restarts.)")
        return

    rows = []
    with open(LOG_PATH, encoding="utf-8") as f:
        for line in f:
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                # A half-written line from two bots appending at once — skip it.
                continue
            try:
                ts = time.mktime(time.strptime(r["ts"], "%Y-%m-%dT%H:%M:%S"))
            except (KeyError, ValueError):
                continue
            if ts >= cutoff:
                rows.append(r)

    if not rows:
        print(f"No calls recorded in the last {days} days.")
        return

    total = len(rows)
    hard = [r for r in rows if _is_hard(r)]
    soft = [r for r in rows if _is_soft(r)]
    refusals = [r for r in rows if r.get("stop_reason") == "refusal"]

    bypersona = defaultdict(Counter)
    modelof = defaultdict(Counter)
    for r in rows:
        who = r.get("persona") or "?"
        bypersona[who]["calls"] += 1
        modelof[who][r.get("model") or "?"] += 1
        if _is_hard(r):
            bypersona[who]["hard"] += 1
            if r.get("stop_reason") == "refusal":
                bypersona[who]["refusals"] += 1
        elif _is_soft(r):
            bypersona[who]["soft"] += 1

    print(f"STOP WATCH — last {days} days")
    print(f"  calls logged                              : {total}")
    print(f"  HARD clips  (refusal / error / dropped)   : {len(hard)}")
    print(f"      └ of which refusals (classifier stop): {len(refusals)}")
    print(f"  soft flags  (ended mid-sentence, normal)  : {len(soft)}")
    print()
    print("  per persona:")
    hdr = f"    {'persona':<16}{'model':<30}{'calls':>7}{'refus':>7}{'hard':>6}{'soft':>6}"
    print(hdr)
    print("    " + "-" * (len(hdr) - 4))
    for who in sorted(bypersona, key=lambda w: (-bypersona[w].get("hard", 0),
                                            -bypersona[w].get("refusals", 0), w)):
        c = bypersona[who]
        m = modelof[who].most_common(1)[0][0] if modelof[who] else "?"
        print(f"    {who:<16}{m:<30}{c['calls']:>7}"
              f"{c.get('refusals', 0):>7}{c.get('hard', 0):>6}{c.get('soft', 0):>6}")

    if hard:
        print("\n  most recent hard clips:")
        for r in hard[-12:]:
            who = (r.get("persona") or "?")
            line = (f"    {r.get('ts', '?')}  {who:<14}{r.get('stop_reason', '?'):<9}")
            if r.get("error"):
                line += f" err={r['error'][:50]}"
            tail = (r.get("tail") or "")[-24:]
            if tail:
                line += f"  …{tail!r}"
            print(line)
    else:
        print("\n  No hard clips in this window.")


if __name__ == "__main__":
    main()
