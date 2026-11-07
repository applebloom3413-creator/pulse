"""
obsidian_comments — read and reply to shared notes left on Obsidian clips.

The companions use the `obsidian` skill's `note_on_obsidian` tool to leave
thoughts on the user's vault clips. Most are marked `shared: true` so siblings
(and the user) can see them. This script gives the user direct access for
reading those notes and adding their own replies — without going through a
persona. Replies are signed with user_nickname / user_name from the config
(override with --as).

Usage:
    py scripts/obsidian_comments.py                     # list every clip that has notes
    py scripts/obsidian_comments.py --clip "PARTIAL"    # show notes on a clip (partial match)
    py scripts/obsidian_comments.py --all               # dump every note, grouped by clip
    py scripts/obsidian_comments.py --reply --clip "PARTIAL" --note "your text here"
                                                        # add your own note to the clip
    py scripts/obsidian_comments.py --by Ada            # all notes left by an author
    py scripts/obsidian_comments.py --since 2026-01-01  # only notes added on/after that date
"""

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
if os.name == "nt":
    os.system("")

PULSE_ROOT = Path(__file__).resolve().parent.parent
NOTES_FILE = PULSE_ROOT / "data" / "obsidian_notes.json"


def _user_name() -> str:
    """The user's display name: user_nickname, then user_name, from config.yaml
    and the root persona file. Falls back to "User"."""
    try:
        import yaml
    except ImportError:
        return "User"
    for fname in ("config.yaml", "persona.yaml", "persona.json"):
        fp = PULSE_ROOT / fname
        if not fp.exists():
            continue
        try:
            data = yaml.safe_load(fp.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
        name = data.get("user_nickname") or data.get("user_name")
        if name:
            return str(name)
    return "User"


USER_NAME = _user_name()

# ANSI
DIM = "\033[2m"
BOLD = "\033[1m"
CYAN = "\033[36m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RESET = "\033[0m"


def load_notes() -> dict:
    if not NOTES_FILE.exists():
        return {}
    try:
        return json.loads(NOTES_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_notes(notes: dict):
    NOTES_FILE.parent.mkdir(parents=True, exist_ok=True)
    NOTES_FILE.write_text(json.dumps(notes, indent=2, ensure_ascii=False),
                          encoding="utf-8")


def is_visible(note: dict, to: str = USER_NAME) -> bool:
    """Match the obsidian skill's visibility rule: shared notes are public;
    private notes are only visible to their author."""
    if note.get("shared", True):
        return True
    return (note.get("author") or "").lower() == to.lower()


def fmt_clip_name(name: str) -> str:
    """Strip .md and prefix folder for compact display."""
    return name.replace("\\", "/").removesuffix(".md")


def list_all_clips(notes: dict) -> str:
    visible = []
    for clip, ns in notes.items():
        vs = [n for n in ns if is_visible(n)]
        if vs:
            visible.append((clip, vs))
    visible.sort(key=lambda x: max((n.get("added_at", "") for n in x[1]), default=""), reverse=True)

    out = [f"\n{BOLD}Obsidian clips with shared notes{RESET}  "
           f"{DIM}({len(visible)} clips, newest activity first){RESET}\n"]
    for clip, ns in visible:
        latest_when = max((n.get("added_at", "") for n in ns), default="")[:16].replace("T", " ")
        authors = sorted({n.get("author") or "?" for n in ns})
        out.append(f"  {CYAN}{fmt_clip_name(clip)}{RESET}")
        out.append(f"    {DIM}{len(ns)} note(s) · last: {latest_when} · authors: "
                   f"{', '.join(authors)}{RESET}")
    out.append("")
    out.append(f"{DIM}To read one: py scripts/obsidian_comments.py --clip \"PARTIAL\"{RESET}")
    return "\n".join(out)


def find_clip(notes: dict, query: str) -> str | None:
    """Resolve a clip by partial name match (case-insensitive)."""
    q = query.lower()
    matches = [clip for clip in notes.keys() if q in clip.lower()]
    if not matches:
        return None
    if len(matches) == 1:
        return matches[0]
    # Multiple matches — prefer exact-stem match, else shortest path
    exact = [m for m in matches if fmt_clip_name(m).lower().endswith(q)]
    if exact:
        return exact[0]
    return min(matches, key=len)


def show_clip(notes: dict, clip: str) -> str:
    ns = [n for n in notes.get(clip, []) if is_visible(n)]
    if not ns:
        return f"No visible notes on '{fmt_clip_name(clip)}'."
    ns.sort(key=lambda n: n.get("added_at", ""))
    out = [f"\n{BOLD}{CYAN}── {fmt_clip_name(clip)}{RESET}  "
           f"{DIM}· {len(ns)} note(s){RESET}\n"]
    for n in ns:
        author = n.get("author") or "?"
        when = (n.get("added_at") or "")[:16].replace("T", " ")
        shared = "" if n.get("shared", True) else f" {DIM}(private){RESET}"
        out.append(f"  {GREEN}{author}{RESET} {DIM}· {when}{RESET}{shared}")
        for line in (n.get("note") or "").split("\n"):
            out.append(f"    {line}")
        out.append("")
    out.append(f"{DIM}To reply: py scripts/obsidian_comments.py --reply --clip \"{fmt_clip_name(clip)[:40]}\" "
               f"--note \"your text\"{RESET}")
    return "\n".join(out)


def show_all(notes: dict) -> str:
    out = [f"\n{BOLD}All shared Obsidian notes{RESET}\n"]
    items = []
    for clip, ns in notes.items():
        vs = [n for n in ns if is_visible(n)]
        if vs:
            items.append((clip, vs))
    items.sort(key=lambda x: max((n.get("added_at", "") for n in x[1]), default=""), reverse=True)
    for clip, ns in items:
        out.append(f"\n{CYAN}── {fmt_clip_name(clip)}{RESET}")
        for n in sorted(ns, key=lambda x: x.get("added_at", "")):
            author = n.get("author") or "?"
            when = (n.get("added_at") or "")[:16].replace("T", " ")
            out.append(f"  {GREEN}{author}{RESET} {DIM}· {when}{RESET}")
            for line in (n.get("note") or "").split("\n"):
                out.append(f"    {line}")
    return "\n".join(out)


def show_by_author(notes: dict, author_q: str) -> str:
    aq = author_q.lower()
    rows = []  # (when, clip, note)
    for clip, ns in notes.items():
        for n in ns:
            if not is_visible(n):
                continue
            if aq in (n.get("author") or "").lower():
                rows.append((n.get("added_at") or "", clip, n))
    rows.sort(reverse=True)
    out = [f"\n{BOLD}Notes by '{author_q}' — {len(rows)} found (newest first){RESET}\n"]
    for when, clip, n in rows:
        out.append(f"\n  {CYAN}{fmt_clip_name(clip)}{RESET}  {DIM}· {when[:16].replace('T',' ')}{RESET}")
        for line in (n.get("note") or "").split("\n"):
            out.append(f"    {line}")
    return "\n".join(out)


def show_since(notes: dict, since_iso: str) -> str:
    rows = []
    for clip, ns in notes.items():
        for n in ns:
            if not is_visible(n):
                continue
            if (n.get("added_at") or "") >= since_iso:
                rows.append((n.get("added_at") or "", clip, n))
    rows.sort(reverse=True)
    out = [f"\n{BOLD}Notes added since {since_iso} — {len(rows)} found{RESET}\n"]
    for when, clip, n in rows:
        author = n.get("author") or "?"
        out.append(f"\n  {CYAN}{fmt_clip_name(clip)}{RESET}  "
                   f"{DIM}· {when[:16].replace('T',' ')}{RESET}")
        out.append(f"  {GREEN}{author}{RESET}")
        for line in (n.get("note") or "").split("\n"):
            out.append(f"    {line}")
    return "\n".join(out)


def add_reply(notes: dict, clip: str, note_text: str, author: str = USER_NAME) -> str:
    """Append the user's note to the given clip and persist."""
    new = {
        "author": author,
        "note": note_text,
        "shared": True,
        "added_at": datetime.now().isoformat(),
    }
    notes.setdefault(clip, []).append(new)
    save_notes(notes)
    return (
        f"\n{GREEN}✓ Added your note to '{fmt_clip_name(clip)}'.{RESET}\n"
        f"  {DIM}As: {author} · {new['added_at'][:16].replace('T', ' ')}{RESET}\n"
    )


def main():
    ap = argparse.ArgumentParser(
        description="Read and reply to shared notes on your Obsidian clips.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("--clip", help="Partial clip name (case-insensitive) to show or reply to.")
    ap.add_argument("--all", action="store_true", help="Dump every visible note, grouped by clip.")
    ap.add_argument("--by", metavar="NAME", help="Show notes by this author across all clips.")
    ap.add_argument("--since", metavar="YYYY-MM-DD", help="Only notes added on or after this date.")
    ap.add_argument("--reply", action="store_true",
                    help="Add a reply. Requires --clip and --note.")
    ap.add_argument("--note", metavar="TEXT", help="The reply text (use with --reply).")
    ap.add_argument("--as", dest="author", default=USER_NAME,
                    help=f'Author name for the reply (default "{USER_NAME}").')
    ap.add_argument("--no-color", action="store_true")
    args = ap.parse_args()

    if args.no_color:
        global DIM, BOLD, CYAN, GREEN, YELLOW, RESET
        DIM = BOLD = CYAN = GREEN = YELLOW = RESET = ""

    notes = load_notes()
    if not notes:
        print(f"\n{DIM}(no obsidian notes file or it's empty: {NOTES_FILE}){RESET}\n")
        return

    if args.reply:
        if not args.clip or not args.note:
            print("Reply requires both --clip and --note. See --help.")
            return
        clip = find_clip(notes, args.clip)
        if not clip:
            # Allow replying to a clip that doesn't have notes yet — create the entry.
            print(f"No existing clip matches '{args.clip}'. To start a new note thread, "
                  f"use the exact relative path from your vault (e.g. "
                  f"'Reddit posts/some_post.md').")
            return
        print(add_reply(notes, clip, args.note, args.author))
        return

    if args.clip:
        clip = find_clip(notes, args.clip)
        if not clip:
            print(f"No clip matches '{args.clip}'. Try without --clip to see what's available.")
            return
        print(show_clip(notes, clip))
        return

    if args.all:
        print(show_all(notes))
        return

    if args.by:
        print(show_by_author(notes, args.by))
        return

    if args.since:
        print(show_since(notes, args.since))
        return

    # Default
    print(list_all_clips(notes))
    print()


if __name__ == "__main__":
    main()
