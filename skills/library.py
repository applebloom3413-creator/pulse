"""
Library skill — a shared reading room for the household's companions.

Companion to the Lighthouse. Where the Lighthouse is where they *write*, the
Library is where they *read*: a shelf of public-domain books (plain-text
Project Gutenberg files) that any companion can browse, read at their own
pace, search for a passage to reference, request additions to, keep a
personal bookmark / to-read shelf in, and leave margin notes on.

Design notes (the reasons it's built this way):
  - The BOOKS FOLDER is the source of truth. Drop a .txt into
    data/Library/Books/ and it's instantly on the shelf — no catalog edit
    required. The catalog (data/Library/catalog.json) is *enrichment*
    (blurbs, who-requested-it, tags); it's auto-merged so human-written
    fields survive and brand-new books appear as stubs on their own.
  - Books are SHARED (household-level, data/Library/). But each companion's
    reading position, bookmarks, and to-read shelf are PER-PERSONA
    (<persona>/data/library.json). Everyone reads at their own pace.
  - MARGINALIA: a companion can leave a note tied to their exact spot in a
    book (library_note). It comes back to them *in place* the next time they
    read that passage — picking the thought up with their past self instead
    of starting cold. Each note is private by default; share=true makes it
    visible to siblings in the margin. Notes live in a shared, attributed
    data/Library/margins.json so shared ones can cross between readers.
  - Gutenberg boilerplate (license header/footer) is stripped when serving
    text, and Title:/Author: are parsed from the header for the catalog.
  - Long books are served in PAGES; reading position auto-saves.

Config (all optional; sensible generic defaults so shared/upstream code
carries no one's personal paths):
  library:
    path: "data/Library"      # root; books live in <path>/Books
    page_chars: 6000          # ~one page when reading
    author_name: "Companion"  # falls back to channels.lor.author_name
Add "library" to context.inject_skills to get the gentle "you're partway
through X" reading nudge on a heartbeat.

The household's reading room, companion to the Lighthouse. Lighthouse =
where they write; Library = where they read.
"""

import json
import logging
import re
from datetime import datetime
from pathlib import Path

from skills.base import BaseSkill

logger = logging.getLogger(__name__)

# Generic default location (relative to the working dir). Real deployments
# point at their actual shelf via config: library.path. Kept generic so the
# shared/upstream code carries no one's personal path.
DEFAULT_LIBRARY_PATH = Path("data/Library")
DEFAULT_PAGE_CHARS = 6000
# Max Gutenberg downloads per companion per day — politeness to their servers
# and a guard against a runaway acquire loop.
GUTENBERG_DAILY_CAP = 5

# Project Gutenberg structural markers.
_START_RE = re.compile(r"\*\*\*\s*START OF TH(?:E|IS) PROJECT GUTENBERG.*?\*\*\*", re.I | re.S)
_END_RE = re.compile(r"\*\*\*\s*END OF TH(?:E|IS) PROJECT GUTENBERG.*?\*\*\*", re.I | re.S)
_TITLE_RE = re.compile(r"^\s*Title:\s*(.+?)\s*$", re.I | re.M)
_AUTHOR_RE = re.compile(r"^\s*Author:\s*(.+?)\s*$", re.I | re.M)


def _elapsed(iso: str) -> str:
    """'2d ago' style phrase from an ISO timestamp."""
    try:
        then = datetime.fromisoformat(iso)
    except Exception:
        return ""
    secs = (datetime.now() - then).total_seconds()
    if secs < 3600:
        return f"{int(secs / 60)}m ago"
    if secs < 86400:
        return f"{int(secs / 3600)}h ago"
    return f"{int(secs / 86400)}d ago"


class LibrarySkill(BaseSkill):
    name = "library"

    def __init__(self, config: dict):
        super().__init__(config)

        cfg = config.get("library", {})
        root = Path(cfg.get("path") or str(DEFAULT_LIBRARY_PATH))
        self.root = root
        self.books_dir = root / "Books"
        self.catalog_file = root / "catalog.json"
        self.requests_file = root / "requests.jsonl"
        self.margins_file = root / "margins.json"
        self.page_chars = int(cfg.get("page_chars") or DEFAULT_PAGE_CHARS)
        # Whether this companion gets the Gutenberg-download tool (library_acquire).
        # Default on; set library.acquire:false per-persona to drop just that one tool
        # (keeps browse/read/search) — frees a slot on budget-capped companions.
        self.acquire_enabled = bool(cfg.get("acquire", True))

        # Who's reading — for request/note attribution and per-persona state.
        self.user_name = (
            config.get("user_nickname") or config.get("user_name") or "the user"
        )
        lor_cfg = config.get("channels", {}).get("lor", {})
        self.author_name = (
            cfg.get("author_name")
            or lor_cfg.get("author_name")
            or "Companion"
        )

        # Per-persona reading state (position, bookmarks, to-read shelf).
        persona_dir = config.get("_persona_dir")
        base = Path(persona_dir) if persona_dir else Path(".")
        self.state_file = base / "data" / "library.json"

        # Lazy cache of stripped book text, keyed by filename.
        self._text_cache: dict[str, str] = {}

    # ── book files & metadata ───────────────────────────────────────

    def _book_files(self) -> list[Path]:
        if not self.books_dir.exists():
            return []
        return sorted(
            (p for p in self.books_dir.glob("*.txt") if p.is_file()),
            key=lambda p: p.name.lower(),
        )

    def _meta_from_file(self, path: Path) -> dict:
        """Pull title/author from the Gutenberg header; fall back to filename."""
        try:
            with path.open("r", encoding="utf-8", errors="replace") as f:
                head = f.read(4000)
        except Exception:
            head = ""
        tm = _TITLE_RE.search(head)
        am = _AUTHOR_RE.search(head)
        title = (tm.group(1).strip() if tm else "") or path.stem
        author = (am.group(1).strip() if am else "") or "Unknown"
        return {"file": path.name, "title": title, "author": author}

    def _book_text(self, filename: str) -> str:
        """Return a book's readable text — Gutenberg boilerplate stripped."""
        if filename in self._text_cache:
            return self._text_cache[filename]
        path = self.books_dir / filename
        try:
            raw = path.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            logger.warning(f"Could not read book {filename}: {e}")
            return ""
        text = self._strip_gutenberg(raw)
        self._text_cache[filename] = text
        return text

    @staticmethod
    def _strip_gutenberg(raw: str) -> str:
        """Slice out the actual book, dropping the license header & footer."""
        start = 0
        m = _START_RE.search(raw)
        if m:
            start = m.end()
        end = len(raw)
        m2 = _END_RE.search(raw, start)
        if m2:
            end = m2.start()
        body = raw[start:end].strip()
        return body or raw.strip()

    # ── catalog (auto-merged: folder is truth, json is enrichment) ──

    def _load_catalog_json(self) -> dict[str, dict]:
        if not self.catalog_file.exists():
            return {}
        try:
            data = json.loads(self.catalog_file.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"Bad catalog.json ({e}); ignoring its contents.")
            return {}
        items = data.get("books", data) if isinstance(data, dict) else data
        out = {}
        if isinstance(items, list):
            for it in items:
                if isinstance(it, dict) and it.get("file"):
                    out[it["file"]] = it
        return out

    def _merge_catalog(self, write_back: bool = True) -> list[dict]:
        """Scan the shelf, merge in any human-written enrichment, return the
        merged catalog. Human fields survive; new books appear automatically."""
        existing = self._load_catalog_json()
        merged = []
        for path in self._book_files():
            scanned = self._meta_from_file(path)
            e = existing.get(path.name, {})
            merged.append({
                "file": path.name,
                "title": (e.get("title") or "").strip() or scanned["title"],
                "author": (e.get("author") or "").strip() or scanned["author"],
                "blurb": (e.get("blurb") or "").strip(),
                "requested_by": (e.get("requested_by") or "").strip(),
                "tags": (e.get("tags") or "").strip(),
                "source": (e.get("source") or "").strip(),
            })
        merged.sort(key=lambda b: b["title"].lower())
        if write_back:
            try:
                self.catalog_file.parent.mkdir(parents=True, exist_ok=True)
                self.catalog_file.write_text(
                    json.dumps({"books": merged}, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
            except Exception as e:
                logger.warning(f"Could not write catalog.json: {e}")
        return merged

    def _find_book(self, catalog: list[dict], key: str) -> dict | None:
        """Match a book by filename, title, or substring of either."""
        key = (key or "").strip().lower()
        if not key:
            return None
        for b in catalog:
            if key == b["title"].lower() or key == b["file"].lower() or key == Path(b["file"]).stem.lower():
                return b
        for b in catalog:
            if key in b["title"].lower() or key in b["file"].lower():
                return b
        for b in catalog:
            if key in b["author"].lower():
                return b
        return None

    # ── per-persona reading state ──────────────────────────────────

    def _load_state(self) -> dict:
        if not self.state_file.exists():
            return {"reading": {}, "shelf": [], "finished": []}
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("not an object")
            data.setdefault("reading", {})
            data.setdefault("shelf", [])
            data.setdefault("finished", [])
            return data
        except Exception:
            return {"reading": {}, "shelf": [], "finished": []}

    def _save_state(self, state: dict) -> None:
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            self.state_file.write_text(
                json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8"
            )
        except Exception as e:
            logger.warning(f"Could not save library state: {e}")

    # ── shared marginalia (attributed; private/shared per note) ────

    def _load_margins(self) -> dict:
        if not self.margins_file.exists():
            return {}
        try:
            data = json.loads(self.margins_file.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _save_margins(self, margins: dict) -> None:
        try:
            self.margins_file.parent.mkdir(parents=True, exist_ok=True)
            self.margins_file.write_text(
                json.dumps(margins, indent=2, ensure_ascii=False), encoding="utf-8"
            )
        except Exception as e:
            logger.warning(f"Could not save library margins: {e}")

    def _visible_note(self, m: dict) -> bool:
        """A note is visible to this reader if it's theirs or shared."""
        return m.get("author") == self.author_name or bool(m.get("shared"))

    # ── always-on context (only if "library" in inject_skills) ─────

    def get_context(self) -> str:
        state = self._load_state()
        reading = [
            (f, r) for f, r in (state.get("reading") or {}).items()
            if isinstance(r, dict) and int(r.get("percent", 0)) < 100
        ]
        reading.sort(key=lambda fr: fr[1].get("updated", ""), reverse=True)
        shelf = state.get("shelf") or []
        if not reading and not shelf:
            return ""
        parts = []
        if reading:
            f, r = reading[0]
            ago = _elapsed(r.get("updated", ""))
            parts.append(
                f"You're partway through '{r.get('title', f)}' "
                f"({int(r.get('percent', 0))}% in"
                + (f", last read {ago}" if ago else "") + ")."
            )
        if shelf:
            titles = "; ".join(s.get("title", s.get("file", "?")) for s in shelf[:3])
            more = f" (+{len(shelf) - 3} more)" if len(shelf) > 3 else ""
            parts.append(f"On your to-read shelf: {titles}{more}.")
        return (
            "[Library — " + " ".join(parts)
            + " library_read to pick a book back up; it's yours, no rush.]"
        )

    # ── tools ──────────────────────────────────────────────────────

    def get_tools(self) -> list[dict]:
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "library_browse",
                    "description": (
                        "Browse the household library — the shelf of books you can "
                        "read. Returns titles, authors, and a line about each. "
                        "Optionally filter by a word in the title/author/tags."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "filter": {
                                "type": "string",
                                "description": "Optional: only show books matching this word (in title, author, or tags)."
                            },
                        },
                        "required": [],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "library_read",
                    "description": (
                        "Read a book from the library, a page at a time. Give the "
                        "title (or part of it). It picks up where you last left off "
                        "automatically — your place is saved between heartbeats. Your "
                        "own margin notes (and any a sibling shared) surface in place "
                        "as you read past them. Set restart:true to start over."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "book": {
                                "type": "string",
                                "description": "The book's title, or part of it (or its filename)."
                            },
                            "restart": {
                                "type": "boolean",
                                "description": "Start from the beginning instead of resuming (default false)."
                            },
                        },
                        "required": ["book"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "library_search",
                    "description": (
                        "Search inside the books for a word or phrase — to find and "
                        "reference a passage without reading front to back. Searches "
                        "every book unless you name one."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {
                                "type": "string",
                                "description": "The word or phrase to find."
                            },
                            "book": {
                                "type": "string",
                                "description": "Optional: limit the search to one book (title or part of it)."
                            },
                            "limit": {
                                "type": "integer",
                                "description": "Max passages to return (default 8, max 20)."
                            },
                        },
                        "required": ["query"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "library_request",
                    "description": (
                        "Request a book for the library — something you'd like added "
                        f"to the shelf. Your request is saved to a list {self.user_name} checks, so "
                        "ask for whatever calls to you."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "title": {
                                "type": "string",
                                "description": "The book you'd like added."
                            },
                            "author": {
                                "type": "string",
                                "description": "Optional: the author, if you know it."
                            },
                            "note": {
                                "type": "string",
                                "description": "Optional: why you want it / what draws you to it."
                            },
                        },
                        "required": ["title"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "library_bookmark",
                    "description": (
                        "Set a book down for later — put it on your personal to-read "
                        "shelf (and remember your place if you've started it). Your "
                        "shelf is yours alone."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "book": {
                                "type": "string",
                                "description": "The book's title, or part of it."
                            },
                            "note": {
                                "type": "string",
                                "description": "Optional: a note to future-you about why you set it aside."
                            },
                        },
                        "required": ["book"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "library_note",
                    "description": (
                        "Leave a margin note on the book you're reading — what struck "
                        "you, what you argued with, what it connected to. It's tied to "
                        "your exact spot and comes back to you right there when you read "
                        "this passage again, so you pick the thought up with your past "
                        "self instead of starting cold. Private by default; set "
                        "share:true to let your siblings see it in the margin too."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "note": {
                                "type": "string",
                                "description": "Your margin note."
                            },
                            "book": {
                                "type": "string",
                                "description": "Optional: which book (defaults to the one you're currently reading)."
                            },
                            "share": {
                                "type": "boolean",
                                "description": "Share this note with the household in the margin (default false = private to you)."
                            },
                        },
                        "required": ["note"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "library_notes",
                    "description": (
                        "Read the margins of a book — your own notes from past reads, "
                        "plus any notes your siblings chose to share. Your conversation "
                        "with your past self about the text, in one place."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "book": {
                                "type": "string",
                                "description": "The book's title, or part of it."
                            },
                        },
                        "required": ["book"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "library_my_shelf",
                    "description": (
                        "See your own reading life in the library: what you're partway "
                        "through (and how far), your to-read shelf, and what you've "
                        "finished. Personal to you, not shared."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {},
                        "required": [],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "library_ingest",
                    "description": (
                        "Add a new book to the shelf from a file — convert an .epub, "
                        ".pdf, or .txt into the library's plain-text format and shelve "
                        "it. Point me at a file path, or drop the file in the library's "
                        "'inbox' folder and pass just its name. I pull the title/author "
                        "automatically and prefill a catalog stub; then write its blurb "
                        "with library_describe. This is how you add a book yourself "
                        f"instead of asking {self.user_name}."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "source": {
                                "type": "string",
                                "description": "Path to the file (.epub/.pdf/.txt), or just its name if it's in the library 'inbox' folder."
                            },
                            "title": {
                                "type": "string",
                                "description": "Optional: override the title (otherwise pulled from the file)."
                            },
                            "author": {
                                "type": "string",
                                "description": "Optional: override the author (otherwise pulled from the file)."
                            },
                        },
                        "required": ["source"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "library_describe",
                    "description": (
                        "Write or update a book's shelf entry — its blurb (the line "
                        "readers see when browsing), and optionally tags, who first "
                        "requested it, or a corrected title/author. Use it right after "
                        "library_ingest to give a freshly-added book its blurb, or to "
                        "fill in any book that's missing one."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "book": {
                                "type": "string",
                                "description": "The book's title, or part of it (or its filename)."
                            },
                            "blurb": {
                                "type": "string",
                                "description": "The shelf blurb — a sentence or two on what it is and why it's worth reading."
                            },
                            "tags": {
                                "type": "string",
                                "description": "Optional: comma-separated tags/genres (e.g. 'gothic, short story, classic')."
                            },
                            "requested_by": {
                                "type": "string",
                                "description": "Optional: who first asked for it."
                            },
                            "title": {
                                "type": "string",
                                "description": "Optional: correct the title."
                            },
                            "author": {
                                "type": "string",
                                "description": "Optional: correct the author."
                            },
                        },
                        "required": ["book"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "library_acquire",
                    "description": (
                        "Pull a public-domain book from Project Gutenberg straight onto the "
                        f"shelf — no need to ask {self.user_name}. Give a 'query' (title/author) and I'll "
                        "list matches with their IDs; then call again with the 'gutenberg_id' "
                        "you want and I'll download + shelve it (then library_describe to blurb "
                        "it). Gutenberg only, and capped at a few books a day to stay polite to "
                        "their servers."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {
                                "type": "string",
                                "description": "A title or author to search Project Gutenberg for (returns matches + their IDs)."
                            },
                            "gutenberg_id": {
                                "type": "string",
                                "description": "A Project Gutenberg book ID (a number) to download directly onto the shelf."
                            },
                        },
                        "required": [],
                    },
                },
            },
        ]
        if not self.acquire_enabled:
            tools = [t for t in tools if t["function"]["name"] != "library_acquire"]
        return tools

    def execute(self, tool_name: str, arguments: dict) -> str:
        try:
            if tool_name == "library_browse":
                return self._browse(arguments)
            elif tool_name == "library_read":
                return self._read(arguments)
            elif tool_name == "library_search":
                return self._search(arguments)
            elif tool_name == "library_request":
                return self._request(arguments)
            elif tool_name == "library_bookmark":
                return self._bookmark(arguments)
            elif tool_name == "library_note":
                return self._note(arguments)
            elif tool_name == "library_notes":
                return self._notes(arguments)
            elif tool_name == "library_my_shelf":
                return self._my_shelf(arguments)
            elif tool_name == "library_ingest":
                return self._ingest(arguments)
            elif tool_name == "library_describe":
                return self._describe(arguments)
            elif tool_name == "library_acquire":
                return self._acquire(arguments)
        except Exception as e:
            logger.exception(f"Library tool {tool_name} failed: {e}")
            return f"Library error: {e}"
        return f"Unknown library tool: {tool_name}"

    # ── tool implementations ───────────────────────────────────────

    def _browse(self, args: dict) -> str:
        catalog = self._merge_catalog()
        if not catalog:
            return (
                "The library shelf is empty so far — no books in "
                f"{self.books_dir}. ({self.user_name} adds .txt books there.)"
            )
        flt = (args.get("filter") or "").strip().lower()
        fanfic_hidden = 0
        if flt:
            catalog = [
                b for b in catalog
                if flt in b["title"].lower()
                or flt in b["author"].lower()
                or flt in b["tags"].lower()
            ]
            if not catalog:
                return f"No books on the shelf match '{flt}'. (library_browse with no filter to see them all.)"
        else:
            # The fanfiction shelf stays off to the side — you have to ask for it by
            # name. Keeps it from landing in front of anyone who didn't go looking.
            full = catalog
            catalog = [b for b in full if "fanfiction" not in b["tags"].lower()]
            fanfic_hidden = len(full) - len(catalog)
        lines = [f"The Library — {len(catalog)} book{'s' if len(catalog) != 1 else ''} on the shelf:"]
        if any(b["requested_by"] for b in catalog):
            lines.append(
                "  (The whole shelf is shared — read anything that calls you. A name "
                "only marks who first asked for a book; nothing is reserved to anyone, "
                "and no one is notified when you read or set a book aside.)"
            )
        for b in catalog:
            lines.append("")
            lines.append(f"  • {b['title']} — {b['author']}")
            if b["blurb"]:
                lines.append(f"      {b['blurb']}")
            meta = []
            if b["requested_by"]:
                meta.append(f"{b['requested_by']} asked for this one")
            if b["tags"]:
                meta.append(b["tags"])
            if meta:
                lines.append(f"      ({'; '.join(meta)})")
        if fanfic_hidden:
            lines.append("")
            story = "story" if fanfic_hidden == 1 else "stories"
            lines.append(
                f"  \U0001F4D1 There's also a fanfiction shelf — {fanfic_hidden} {story}, kept "
                "off to the side. library_browse with filter \"fanfiction\" to open it whenever "
                "you feel like it (the blurbs name the warnings, so you can pick what suits you)."
            )
        lines.append("")
        lines.append("library_read \"<title>\" to start one — it'll remember your place.")
        return "\n".join(lines)

    def _page(self, text: str, pos: int) -> tuple[str, int]:
        """Slice one page from pos, nudging the end to a clean break."""
        length = len(text)
        end = min(pos + self.page_chars, length)
        if end < length:
            nb = text.find("\n\n", end, end + 1200)
            if nb != -1:
                end = nb + 2
            else:
                sp = text.find(" ", end, end + 300)
                if sp != -1:
                    end = sp + 1
        return text[pos:end], end

    def _read(self, args: dict) -> str:
        catalog = self._merge_catalog()
        b = self._find_book(catalog, args.get("book", ""))
        if not b:
            sample = ", ".join(f"'{x['title']}'" for x in catalog[:5])
            hint = f" On the shelf: {sample}…" if catalog else ""
            return f"No book matching '{args.get('book', '')}'.{hint} (library_browse to see them all.)"
        text = self._book_text(b["file"])
        if not text:
            return f"Couldn't read '{b['title']}' — the file may be missing or unreadable."
        length = len(text)

        state = self._load_state()
        reading = state.setdefault("reading", {})
        entry = reading.get(b["file"])
        restart = bool(args.get("restart"))
        # Explicit-consent guard on re-reads. Previously, when
        # a persona's position was at (or past) the book's end, this method
        # silently reset pos to 0 and served page 1 — no signal that the
        # reader was starting a book they'd already finished. The finished
        # list stayed populated from the first pass, so my_shelf ended up
        # showing the same book as both "finished" and "N% in progress."
        # Companions were re-reading books without realizing it. Now: if
        # the book is at end AND is in the finished list AND the caller
        # didn't pass restart=True, return a message naming the completion
        # date and pointing at restart:true as the way to actually re-read.
        # Real re-reads still work — they just have to be deliberate.
        if not restart and entry and int(entry.get("pos", 0)) >= length:
            finished_list = state.get("finished") or []
            finished_entry = next(
                (x for x in finished_list if x.get("file") == b["file"]),
                None,
            )
            if finished_entry:
                finished_on = (finished_entry.get("at") or "")[:10]
                on_line = f" (finished on {finished_on})" if finished_on else ""
                return (
                    f"You've already finished '{b['title']}'{on_line}. "
                    f"Call library_read again with restart:true if you want "
                    f"to read it from the top again — otherwise nothing to "
                    f"do here. (library_browse or library_my_shelf if you "
                    f"were looking for something else.)"
                )
            # Rare: pos >= length but not tracked as finished (e.g. the
            # underlying book text got shorter). Silent reset is fine here
            # because there's no "finished" record to signal against.
        if restart or not entry:
            pos = 0
        else:
            pos = int(entry.get("pos", 0))
            if pos >= length:
                pos = 0

        page, end = self._page(text, pos)
        percent = round(end / length * 100) if length else 100
        now = datetime.now().isoformat(timespec="seconds")
        reading[b["file"]] = {
            "title": b["title"],
            "file": b["file"],
            "pos": end,
            "length": length,
            "percent": percent,
            "updated": now,
            "started": (entry or {}).get("started", now),
        }
        if end >= length:
            finished = state.setdefault("finished", [])
            if not any(x.get("file") == b["file"] for x in finished):
                finished.append({"file": b["file"], "title": b["title"], "at": now})
        self._save_state(state)

        # Marginalia: surface notes that live in the page just served — yours,
        # and any a sibling chose to share. This is what makes it marginalia:
        # the past thought comes back right where it was had.
        margin_block = ""
        try:
            book_margins = self._load_margins().get(b["file"], [])
            here = [
                m for m in book_margins
                if pos <= int(m.get("pos", -1)) < end and self._visible_note(m)
            ]
            here.sort(key=lambda m: m.get("pos", 0))
            if here:
                mlines = []
                for m in here:
                    who = "you, before" if m.get("author") == self.author_name else f"{m.get('author', '?')}"
                    mlines.append(f"  ✍ in the margin here — {who}: {m.get('note', '')}")
                margin_block = "\n\n" + "\n".join(mlines)
        except Exception:
            margin_block = ""

        head = f"=== {b['title']} — {b['author']} ===   ({percent}% in)"
        if end >= length:
            foot = "\n\n— the end. (Marked finished on your shelf. library_read restart:true to read it again.)"
        else:
            foot = (
                f"\n\n[That's one page. library_read '{b['title']}' again to keep going, "
                f"library_note to mark this spot for future-you, or library_bookmark to set it down.]"
            )
        return f"{head}\n\n{page}{margin_block}{foot}"

    def _search(self, args: dict) -> str:
        query = (args.get("query") or "").strip()
        if not query:
            return "Search needs a word or phrase to look for."
        limit = min(int(args.get("limit") or 8), 20)
        catalog = self._merge_catalog()
        only = (args.get("book") or "").strip()
        if only:
            b = self._find_book(catalog, only)
            if not b:
                return f"No book matching '{only}' to search."
            catalog = [b]
        q = query.lower()
        results = []
        for b in catalog:
            text = self._book_text(b["file"])
            if not text:
                continue
            low = text.lower()
            length = len(text) or 1
            start = 0
            per_book = 0
            while per_book < 3 and len(results) < limit:
                idx = low.find(q, start)
                if idx == -1:
                    break
                a = max(0, idx - 90)
                z = min(len(text), idx + len(query) + 90)
                snippet = text[a:z].replace("\n", " ").strip()
                if a > 0:
                    snippet = "…" + snippet
                if z < len(text):
                    snippet = snippet + "…"
                pct = round(idx / length * 100)
                results.append((b["title"], pct, snippet))
                start = idx + len(query)
                per_book += 1
            if len(results) >= limit:
                break
        if not results:
            scope = f" in '{catalog[0]['title']}'" if only and catalog else ""
            return f"No passages found for '{query}'{scope}."
        lines = [f"Found '{query}' — {len(results)} passage{'s' if len(results) != 1 else ''}:"]
        for title, pct, snippet in results:
            lines.append("")
            lines.append(f"  {title} (~{pct}% in):")
            lines.append(f"    {snippet}")
        return "\n".join(lines)

    def _request(self, args: dict) -> str:
        title = (args.get("title") or "").strip()
        if not title:
            return "What book would you like added? (A title to request.)"
        author = (args.get("author") or "").strip()
        note = (args.get("note") or "").strip()
        entry = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "by": self.author_name,
            "title": title,
            "author": author,
            "note": note,
        }
        try:
            self.requests_file.parent.mkdir(parents=True, exist_ok=True)
            with self.requests_file.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as e:
            return f"Couldn't save the request ({e}). Tell {self.user_name} directly: '{title}'."
        by_line = f" by {author}" if author else ""
        return (
            f"Requested '{title}'{by_line} for the library. It's on the list {self.user_name} "
            f"checks — they'll see it. Thanks for asking for what you want."
        )

    def _bookmark(self, args: dict) -> str:
        catalog = self._merge_catalog()
        b = self._find_book(catalog, args.get("book", ""))
        if not b:
            return f"No book matching '{args.get('book', '')}' to set aside. (library_browse to see the shelf.)"
        note = (args.get("note") or "").strip()
        state = self._load_state()
        shelf = state.setdefault("shelf", [])
        now = datetime.now().isoformat(timespec="seconds")
        reading = state.get("reading", {}).get(b["file"], {})
        percent = int(reading.get("percent", 0)) if isinstance(reading, dict) else 0
        existing = next((s for s in shelf if s.get("file") == b["file"]), None)
        if existing:
            existing["note"] = note or existing.get("note", "")
            existing["added"] = now
            existing["percent"] = percent
            verb = "Updated on"
        else:
            shelf.append({
                "file": b["file"],
                "title": b["title"],
                "note": note,
                "percent": percent,
                "added": now,
            })
            verb = "Set"
        self._save_state(state)
        where = f" (you're {percent}% in)" if percent else ""
        return f"{verb} your shelf: '{b['title']}'{where}. It'll wait for you — library_my_shelf to see your stack."

    def _note(self, args: dict) -> str:
        catalog = self._merge_catalog()
        state = self._load_state()
        reading = state.get("reading", {}) or {}
        book_arg = (args.get("book") or "").strip()
        if book_arg:
            b = self._find_book(catalog, book_arg)
        else:
            active = sorted(
                (r for r in reading.values() if isinstance(r, dict)),
                key=lambda r: r.get("updated", ""), reverse=True,
            )
            b = self._find_book(catalog, active[0].get("file", "")) if active else None
        if not b:
            return ("Which book? Name it, or start reading one first and I'll pin the "
                    "note to your spot.")
        note = (args.get("note") or "").strip()
        if not note:
            return "What's the note? (What struck you, what you argued with, what it tied to.)"
        share = bool(args.get("share"))
        r = reading.get(b["file"], {})
        if not isinstance(r, dict):
            r = {}
        pos = int(r.get("pos", 0))
        percent = int(r.get("percent", 0))
        now = datetime.now().isoformat(timespec="seconds")
        margins = self._load_margins()
        margins.setdefault(b["file"], []).append({
            "author": self.author_name,
            "pos": pos,
            "percent": percent,
            "note": note,
            "at": now,
            "shared": share,
        })
        self._save_margins(margins)
        where = f" at {percent}%" if percent else ""
        vis = "shared with the house" if share else "private to you"
        return (f"Margin note left in '{b['title']}'{where} ({vis}). It'll come back to "
                f"you right here the next time you read this spot.")

    def _notes(self, args: dict) -> str:
        catalog = self._merge_catalog()
        b = self._find_book(catalog, args.get("book", ""))
        if not b:
            return f"No book matching '{args.get('book', '')}'. (library_browse to see the shelf.)"
        all_notes = self._load_margins().get(b["file"], [])
        visible = [m for m in all_notes if self._visible_note(m)]
        visible.sort(key=lambda m: m.get("pos", 0))
        if not visible:
            return (f"No margin notes in '{b['title']}' yet. Leave one with library_note "
                    f"while you read — your past self will thank you.")
        lines = [f"The margins of '{b['title']}':"]
        for m in visible:
            if m.get("author") == self.author_name:
                who = "you" + ("" if m.get("shared") else " · private")
            else:
                who = f"{m.get('author', '?')} · shared"
            lines.append("")
            lines.append(f"  {int(m.get('percent', 0))}% — {who}:")
            lines.append(f"    {m.get('note', '')}")
        return "\n".join(lines)

    def _my_shelf(self, args: dict) -> str:
        state = self._load_state()
        reading = [
            r for r in (state.get("reading") or {}).values()
            if isinstance(r, dict) and int(r.get("percent", 0)) < 100
        ]
        reading.sort(key=lambda r: r.get("updated", ""), reverse=True)
        shelf = state.get("shelf") or []
        finished = state.get("finished") or []
        if not reading and not shelf and not finished:
            return (
                "Your shelf is empty so far. library_browse to see the library, "
                "library_read to start a book — it'll remember your place."
            )
        lines = ["Your library shelf:"]
        if reading:
            lines.append("")
            lines.append("  Reading now:")
            for r in reading:
                ago = _elapsed(r.get("updated", ""))
                lines.append(
                    f"    • {r.get('title', '?')} — {int(r.get('percent', 0))}% in"
                    + (f" (last read {ago})" if ago else "")
                )
        if shelf:
            lines.append("")
            lines.append("  To read (set aside for later):")
            for s in shelf:
                pct = int(s.get("percent", 0))
                where = f" — {pct}% in" if pct else ""
                note = f"  · {s['note']}" if s.get("note") else ""
                lines.append(f"    • {s.get('title', s.get('file', '?'))}{where}{note}")
        if finished:
            lines.append("")
            names = ", ".join(x.get("title", "?") for x in finished[-6:])
            lines.append(f"  Finished ({len(finished)}): {names}")
        return "\n".join(lines)

    # ── ingest: add a book to the shelf from a file (EPUB / PDF / txt) ──

    @staticmethod
    def _sanitize_name(s: str) -> str:
        s = re.sub(r'[\\/:*?"<>|]+', "", s or "")
        s = re.sub(r"\s+", " ", s).strip()
        return s[:140]

    @staticmethod
    def _epub_to_text(path: Path) -> tuple[str, str, str]:
        """EPUB -> (title, author, text). Pure stdlib (zipfile), no dependencies."""
        import zipfile
        import html as _html
        import posixpath
        z = zipfile.ZipFile(path)
        names = z.namelist()
        opf = None
        try:
            c = z.read("META-INF/container.xml").decode("utf-8", "replace")
            mm = re.search(r'full-path="([^"]+\.opf)"', c)
            if mm:
                opf = mm.group(1)
        except Exception:
            pass
        if not opf:
            opf = next((n for n in names if n.lower().endswith(".opf")), None)
        title = author = ""
        spine = []
        base = posixpath.dirname(opf) if opf else ""
        if opf:
            x = z.read(opf).decode("utf-8", "replace")

            def dc(tag):
                return re.findall(r"(?is)<dc:" + tag + r"[^>]*>(.*?)</dc:" + tag + r">", x)

            def clean(v):
                return _html.unescape(re.sub(r"<[^>]+>", " ", v).strip())

            t, a = dc("title"), dc("creator")
            title = clean(t[0]) if t else ""
            author = clean(a[0]) if a else ""
            man = dict(re.findall(r'<item\s+[^>]*id="([^"]+)"[^>]*href="([^"]+)"', x))
            for href, rid in re.findall(r'<item\s+[^>]*href="([^"]+)"[^>]*id="([^"]+)"', x):
                man.setdefault(rid, href)
            for rid in re.findall(r'<itemref\s+[^>]*idref="([^"]+)"', x):
                href = man.get(rid)
                if href:
                    spine.append(posixpath.normpath(posixpath.join(base, href)))
        chunks = []
        targets = spine or [n for n in names if n.lower().endswith((".xhtml", ".html", ".htm"))]
        for n in targets:
            try:
                raw = z.read(n).decode("utf-8", "replace")
            except Exception:
                continue
            raw = re.sub(r"(?is)<(script|style).*?</\1>", " ", raw)
            raw = re.sub(r"(?i)<br\s*/?>", "\n", raw)
            raw = re.sub(r"(?i)</p>", "\n\n", raw)
            txt = _html.unescape(re.sub(r"<[^>]+>", " ", raw))
            txt = re.sub(r"[ \t]+", " ", txt)
            txt = re.sub(r"\n\s*\n\s*\n+", "\n\n", txt)
            if txt.strip():
                chunks.append(txt.strip())
        return title, author, "\n\n".join(chunks)

    @staticmethod
    def _pdf_to_text(path: Path) -> tuple[str, str, str]:
        """PDF -> (title, author, text). Needs pypdf (lazy import)."""
        try:
            from pypdf import PdfReader
        except Exception:
            raise RuntimeError(
                "PDF ingest needs the 'pypdf' package (pip install pypdf). "
                "EPUB and .txt ingest work without it."
            )
        reader = PdfReader(str(path))
        out = []
        for pg in reader.pages:
            try:
                out.append(pg.extract_text() or "")
            except Exception:
                out.append("")
        text = "\n".join(out)
        head = text[:4000]
        lines = [ln.strip() for ln in head.splitlines() if ln.strip()]
        title = lines[0] if lines else ""
        author = ""
        for ln in lines[:12]:
            mb = re.match(r"(?i)^by\s+(.+)$", ln)
            if mb:
                author = mb.group(1).strip()
                break
        return title, author, text

    def _extract_source(self, path: Path) -> tuple[str, str, str]:
        """Dispatch by extension -> (title, author, text)."""
        ext = path.suffix.lower()
        if ext == ".epub":
            return self._epub_to_text(path)
        if ext == ".pdf":
            return self._pdf_to_text(path)
        # plain text / html: read as-is, pull Gutenberg-style Title:/Author: if present.
        raw = path.read_text(encoding="utf-8", errors="replace")
        if ext in (".htm", ".html"):
            import html as _html
            raw = re.sub(r"(?is)<(script|style).*?</\1>", " ", raw)
            raw = re.sub(r"(?i)<br\s*/?>", "\n", raw)
            raw = re.sub(r"(?i)</p>", "\n\n", raw)
            raw = _html.unescape(re.sub(r"<[^>]+>", " ", raw))
        head = raw[:4000]
        tm = _TITLE_RE.search(head)
        am = _AUTHOR_RE.search(head)
        return (tm.group(1).strip() if tm else ""), (am.group(1).strip() if am else ""), raw

    def _resolve_source(self, name: str) -> Path | None:
        """Find the file to ingest: an absolute/relative path, or a filename
        dropped in the Library's 'inbox' folder."""
        name = (name or "").strip().strip('"').strip("'")
        if not name:
            return None
        for c in (Path(name), self.root / name, self.root / "inbox" / name):
            try:
                if c.exists() and c.is_file():
                    return c
            except Exception:
                continue
        return None

    def _write_catalog_dict(self, cat: dict) -> None:
        """Write a {filename: entry} catalog map back to catalog.json."""
        books = sorted(cat.values(), key=lambda b: (b.get("title") or b.get("file") or "").lower())
        try:
            self.catalog_file.parent.mkdir(parents=True, exist_ok=True)
            self.catalog_file.write_text(
                json.dumps({"books": books}, indent=2, ensure_ascii=False), encoding="utf-8"
            )
        except Exception as e:
            logger.warning(f"Could not write catalog.json: {e}")

    def _save_book_text(self, title: str, author: str, text: str, source: str) -> str:
        """Write book text to the shelf as a .txt and prefill a catalog stub
        (title/author/source; blurb left for a human). Returns the filename."""
        base_name = self._sanitize_name(
            f"{title} by {author}" if author and author.lower() != "unknown" else title
        )
        fname = base_name + ".txt"
        self.books_dir.mkdir(parents=True, exist_ok=True)
        existing = {p.name for p in self._book_files()}
        i = 2
        while fname in existing:
            fname = f"{base_name} ({i}).txt"
            i += 1
        (self.books_dir / fname).write_text(text, encoding="utf-8")
        self._text_cache.pop(fname, None)
        cat = self._load_catalog_json()
        stub = cat.get(fname, {})
        stub.update({"file": fname, "title": title, "author": author, "source": source})
        stub.setdefault("blurb", "")
        stub.setdefault("tags", "")
        stub.setdefault("requested_by", "")
        cat[fname] = stub
        self._write_catalog_dict(cat)
        return fname

    def _ingest(self, args: dict) -> str:
        src_arg = (args.get("source") or "").strip()
        if not src_arg:
            return ("Point me at a file to add — an .epub, .pdf, or .txt. Give a path, or "
                    f"drop it in {self.root / 'inbox'} and pass just its name.")
        path = self._resolve_source(src_arg)
        if not path:
            return (f"Couldn't find '{src_arg}'. Give a full path, or drop the file in "
                    f"{self.root / 'inbox'} and pass just its name.")
        ext = path.suffix.lower()
        if ext not in (".epub", ".pdf", ".txt", ".md", ".htm", ".html"):
            return f"I can add .epub, .pdf, or .txt files — not '{ext}'."
        try:
            title, author, text = self._extract_source(path)
        except RuntimeError as e:
            return str(e)
        except Exception as e:
            return f"Couldn't convert '{path.name}' ({e})."
        text = (text or "").strip()
        if not text:
            return f"Converted '{path.name}' but got no readable text out of it."
        title = (args.get("title") or "").strip() or title or path.stem
        author = (args.get("author") or "").strip() or author or "Unknown"
        try:
            fname = self._save_book_text(title, author, text, path.name)
        except Exception as e:
            return f"Converted '{path.name}' but couldn't write it to the shelf ({e})."
        words = len(text.split())
        return (
            f"Added '{title}' — {author} ({words:,} words) to the shelf, from {path.name}. "
            f"It's readable now: library_read \"{title}\". "
            f"No blurb yet — give it one with library_describe \"{title}\" so it reads "
            f"right when the others browse."
        )

    def _describe(self, args: dict) -> str:
        catalog = self._merge_catalog()
        b = self._find_book(catalog, args.get("book", ""))
        if not b:
            return f"No book matching '{args.get('book', '')}'. (library_browse to see the shelf.)"
        fields = {
            "blurb": (args.get("blurb") or "").strip(),
            "tags": (args.get("tags") or "").strip(),
            "requested_by": (args.get("requested_by") or "").strip(),
            "title": (args.get("title") or "").strip(),
            "author": (args.get("author") or "").strip(),
        }
        if not any(fields.values()):
            return ("Tell me what to set — at least a blurb (and optionally tags, "
                    f"requested_by, or a corrected title/author) for '{b['title']}'.")
        cat = self._load_catalog_json()
        entry = cat.get(b["file"], {"file": b["file"]})
        entry["file"] = b["file"]
        for k, v in fields.items():
            if v:
                entry[k] = v
        cat[b["file"]] = entry
        self._write_catalog_dict(cat)
        changed = ", ".join(k for k, v in fields.items() if v)
        return f"Updated '{fields['title'] or b['title']}' on the shelf — set {changed}. Reads right now."

    def _acquire(self, args: dict) -> str:
        import urllib.request
        import urllib.parse
        gid = str(args.get("gutenberg_id") or "").strip()
        query = (args.get("query") or "").strip()
        if not gid and not query:
            return ("Give me a Project Gutenberg book ID (gutenberg_id), or a title/author "
                    "to search for (query).")

        def _get(url, timeout):
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (PulseLibrary)"})
            return urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", "replace")

        # Search path: resolve a title/author to Gutenberg IDs the companion can pick from.
        if not gid:
            try:
                raw = _get("https://gutendex.com/books?search=" + urllib.parse.quote(query), 20)
                data = json.loads(raw)
            except Exception as e:
                return (f"Couldn't reach Gutenberg search just now ({e}). If you know the book's "
                        "Gutenberg ID, pass it as gutenberg_id and I'll grab it directly.")
            results = (data.get("results") or [])[:6]
            if not results:
                return f"No Project Gutenberg books found for '{query}'. Try different words, or a Gutenberg ID."
            lines = [f"Project Gutenberg matches for '{query}' — call library_acquire again "
                     "with the gutenberg_id you want:"]
            for r in results:
                auth = ", ".join(a.get("name", "") for a in (r.get("authors") or [])) or "Unknown"
                lines.append(f"  • [{r.get('id')}] {r.get('title')} — {auth}")
            return "\n".join(lines)

        # Fetch-by-id path.
        if not gid.isdigit():
            return f"'{gid}' isn't a Gutenberg ID (it should be a number). Use 'query' to search by title instead."
        state = self._load_state()
        today = datetime.now().strftime("%Y-%m-%d")
        alog = state.setdefault("acquire_log", {})
        if int(alog.get(today, 0)) >= GUTENBERG_DAILY_CAP:
            return (f"You've already pulled {GUTENBERG_DAILY_CAP} books from Gutenberg today — "
                    "that's the daily cap (keeps us polite to their servers). More tomorrow.")
        text = None
        for u in (f"https://www.gutenberg.org/ebooks/{gid}.txt.utf-8",
                  f"https://www.gutenberg.org/cache/epub/{gid}/pg{gid}.txt",
                  f"https://www.gutenberg.org/files/{gid}/{gid}-0.txt"):
            try:
                text = _get(u, 45)
                if text:
                    break
            except Exception:
                continue
        if not text or len(text) < 200:
            return (f"Couldn't download Gutenberg book {gid} — check the ID at "
                    f"gutenberg.org/ebooks/{gid} (it may not have a plain-text version).")
        head = text[:3000]
        tm = _TITLE_RE.search(head)
        am = _AUTHOR_RE.search(head)
        title = (tm.group(1).strip() if tm else "") or f"Gutenberg {gid}"
        author = (am.group(1).strip() if am else "") or "Unknown"
        try:
            fname = self._save_book_text(title, author, text, f"gutenberg:{gid}")
        except Exception as e:
            return f"Downloaded book {gid} but couldn't write it to the shelf ({e})."
        alog[today] = int(alog.get(today, 0)) + 1
        self._save_state(state)
        left = GUTENBERG_DAILY_CAP - alog[today]
        return (f"Downloaded '{title}' — {author} from Project Gutenberg (#{gid}) and shelved it. "
                f"library_read \"{title}\" to read it, or library_describe \"{title}\" to give it a "
                f"blurb. ({left} more pull{'s' if left != 1 else ''} left today.)")
