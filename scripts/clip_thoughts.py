#!/usr/bin/env python3
"""
clip_thoughts.py — the user's peek at what the companions thought about their clips.

The obsidian skill lets each companion leave thoughts on the web-clippings you
save. PRIVATE thoughts stay private — even from here. This shows only the ones a
companion chose to SHARE: their margins on your reading, gathered up for you.

    py scripts/clip_thoughts.py

(Run it from anywhere; it finds Pulse's data dir on its own.)
"""
import sys
import json
from pathlib import Path

NOTES = Path(__file__).resolve().parent.parent / "data" / "obsidian_notes.json"


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    if not NOTES.exists():
        print("No shared thoughts yet — once the companions share notes on your clips, they'll land here.")
        return
    try:
        data = json.loads(NOTES.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"Couldn't read {NOTES}: {e}")
        return

    items = []
    for clip, notes in (data.items() if isinstance(data, dict) else []):
        shared = [n for n in notes if isinstance(n, dict) and n.get("shared")]
        if shared:
            items.append((clip, shared))

    if not items:
        print("No shared thoughts yet — the companions haven't shared any notes on your clips.")
        return

    items.sort(key=lambda pair: max((n.get("at", "") for n in pair[1]), default=""), reverse=True)
    total = sum(len(s) for _, s in items)
    print(f"The companions' shared thoughts on your clippings — {total} note(s) across {len(items)} clip(s):\n")
    for clip, shared in items:
        title = clip[:-3] if clip.endswith(".md") else clip
        print(f"[ {title} ]")
        for n in sorted(shared, key=lambda x: x.get("at", "")):
            when = (n.get("at") or "")[:16].replace("T", " ")
            print(f"   - {n.get('author', '?')} ({when}): {n.get('note', '')}")
        print()


if __name__ == "__main__":
    main()
