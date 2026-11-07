"""
lor_read — browse LoR (Local Reddit) from the command line so you can read
and COPY posts/comments as plain text, without going through a persona.

The companions already read/write LoR via their `lor` skill; this script
gives the user direct access for reading. Pure local JSON; no API calls.

Usage:
    py scripts/lor_read.py                              # latest 10 posts (newest first)
    py scripts/lor_read.py --recent 30                  # latest 30
    py scripts/lor_read.py <post_id>                    # show that post + its full thread
    py scripts/lor_read.py --search "possum"            # keyword search in content/title
    py scripts/lor_read.py --by Ada                     # all posts by an author (name match)
    py scripts/lor_read.py <post_id> --out thread.md    # also write to a markdown file
    py scripts/lor_read.py --no-color                   # plain text, no ANSI
"""

import argparse
import json
import os
import sys
from pathlib import Path

# UTF-8 stdout for Windows console.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
if os.name == "nt":
    os.system("")

PULSE_ROOT = Path(__file__).resolve().parent.parent
LOR_DIR = PULSE_ROOT / "data" / "lor"

# ANSI
DIM = "\033[2m"
BOLD = "\033[1m"
CYAN = "\033[36m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RESET = "\033[0m"


def load():
    posts = json.loads((LOR_DIR / "posts.json").read_text(encoding="utf-8"))
    authors = json.loads((LOR_DIR / "authors.json").read_text(encoding="utf-8"))
    return posts, authors


def author_name(author_id: str, authors: dict) -> str:
    return authors.get(author_id, {}).get("nickname") or author_id[:12]


def fmt_when(iso: str) -> str:
    return (iso or "")[:16].replace("T", " ")


def fmt_post_block(p: dict, authors: dict, indent: int = 0, plain: bool = False) -> str:
    pad = "  " * indent
    name = author_name(p["author_id"], authors)
    when = fmt_when(p.get("created_at", ""))
    out = []
    if plain:
        out.append(f"{pad}── {name} · {when} · [{p['id']}]")
    else:
        out.append(f"{pad}{BOLD}── {name}{RESET} {DIM}· {when} · [{p['id']}]{RESET}")
    if p.get("title"):
        if plain:
            out.append(f"{pad}{p['title']}")
        else:
            out.append(f"{pad}{BOLD}{p['title']}{RESET}")
    for line in (p.get("content") or "").split("\n"):
        out.append(f"{pad}{line}")
    if p.get("reactions"):
        emojis = " ".join(f"{e}×{c}" for e, c in p["reactions"].items())
        if emojis.strip():
            out.append(f"{pad}{DIM}reactions: {emojis}{RESET}" if not plain else f"{pad}reactions: {emojis}")
    return "\n".join(out)


def list_recent(posts: list, authors: dict, n: int) -> str:
    out = [f"\n{BOLD}Latest {n} LoR posts (newest first){RESET}\n"]
    for p in posts[-n:][::-1]:
        name = author_name(p["author_id"], authors)
        when = fmt_when(p.get("created_at", ""))
        title = (p.get("title") or (p.get("content") or "")[:90]).replace("\n", " ")
        if len(title) >= 88:
            title = title[:88] + "…"
        reply = f" {DIM}↳ {p['reply_to'][:8]}{RESET}" if p.get("reply_to") else ""
        out.append(f"  {DIM}{when}{RESET}  [{p['id']}]  {GREEN}{name:>10}{RESET}{reply}  {title}")
    out.append("")
    out.append(f"{DIM}Show a thread: py scripts/lor_read.py <post_id>{RESET}")
    return "\n".join(out)


def find_thread(posts: list, post_id: str) -> list | None:
    """Given any post in a thread, walk to root then collect all descendants."""
    by_id = {p["id"]: p for p in posts}
    if post_id not in by_id:
        return None
    # Walk to root
    cur = by_id[post_id]
    seen = set()
    while cur.get("reply_to") and cur["reply_to"] not in seen:
        seen.add(cur["id"])
        parent = by_id.get(cur["reply_to"])
        if not parent:
            break
        cur = parent
    root = cur
    # BFS for descendants
    thread = [root]
    in_thread = {root["id"]}
    changed = True
    while changed:
        changed = False
        for p in posts:
            if p["id"] in in_thread:
                continue
            if p.get("reply_to") in in_thread:
                thread.append(p)
                in_thread.add(p["id"])
                changed = True
    thread.sort(key=lambda p: p.get("created_at", ""))
    return thread


def show_thread(posts: list, authors: dict, post_id: str, plain: bool = False) -> str:
    thread = find_thread(posts, post_id)
    if not thread:
        return f"No post with id '{post_id}'."
    header = (
        f"\n{BOLD}{CYAN}── Thread starting at {thread[0]['id']} "
        f"({len(thread)} posts){RESET}\n"
        if not plain else
        f"\n── Thread starting at {thread[0]['id']} ({len(thread)} posts)\n"
    )
    children: dict = {}
    for p in thread:
        rt = p.get("reply_to")
        children.setdefault(rt, []).append(p)

    out = [header]

    def render(p, depth):
        out.append(fmt_post_block(p, authors, depth, plain=plain))
        out.append("")
        for c in sorted(children.get(p["id"], []), key=lambda x: x.get("created_at", "")):
            render(c, depth + 1)

    render(thread[0], 0)
    return "\n".join(out)


def search(posts: list, authors: dict, query: str) -> str:
    q = query.lower()
    matches = [
        p for p in posts
        if q in (p.get("content", "") + " " + p.get("title", "")).lower()
    ]
    out = [f"\n{BOLD}Search: '{query}' — {len(matches)} matches (newest 20){RESET}\n"]
    for p in matches[-20:][::-1]:
        name = author_name(p["author_id"], authors)
        when = fmt_when(p.get("created_at", ""))
        snip = (p.get("content") or "").replace("\n", " ")[:100]
        out.append(f"  {DIM}{when}{RESET}  [{p['id']}]  {GREEN}{name:>10}{RESET}  {snip}…")
    return "\n".join(out)


def by_author(posts: list, authors: dict, name_query: str) -> str:
    nq = name_query.lower()
    matches = [
        p for p in posts
        if nq in author_name(p["author_id"], authors).lower()
    ]
    out = [f"\n{BOLD}Posts by '{name_query}' — {len(matches)} found (newest 30){RESET}\n"]
    for p in matches[-30:][::-1]:
        name = author_name(p["author_id"], authors)
        when = fmt_when(p.get("created_at", ""))
        snip = (p.get("content") or "").replace("\n", " ")[:100]
        out.append(f"  {DIM}{when}{RESET}  [{p['id']}]  {GREEN}{name:>10}{RESET}  {snip}…")
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(
        description="Read LoR posts and threads as plain text. Designed for copying.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("post_id", nargs="?", help="Show this post + its full thread.")
    ap.add_argument("--recent", type=int, default=10,
                    help="With no post_id: show N latest posts (default 10).")
    ap.add_argument("--search", metavar="QUERY", help="Find posts matching keyword.")
    ap.add_argument("--by", metavar="NAME", help="Show posts by author (nickname match).")
    ap.add_argument("--out", metavar="FILE", help="Also write the result to a markdown file.")
    ap.add_argument("--no-color", action="store_true",
                    help="Plain text output (no ANSI). Auto-set when --out is used.")
    args = ap.parse_args()

    use_color = not args.no_color and not args.out
    if not use_color:
        global DIM, BOLD, CYAN, GREEN, YELLOW, RESET
        DIM = BOLD = CYAN = GREEN = YELLOW = RESET = ""

    posts, authors = load()

    if args.post_id:
        text = show_thread(posts, authors, args.post_id, plain=not use_color)
    elif args.search:
        text = search(posts, authors, args.search)
    elif args.by:
        text = by_author(posts, authors, args.by)
    else:
        text = list_recent(posts, authors, args.recent)

    print(text)
    print()

    if args.out:
        out_path = Path(args.out)
        out_path.write_text(text, encoding="utf-8")
        print(f"[wrote {out_path}]")


if __name__ == "__main__":
    main()
