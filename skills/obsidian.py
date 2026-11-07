"""
Obsidian skill — let the household READ the user's Obsidian vault.

The user saves web pages as clean markdown into an Obsidian vault (for example
with the Obsidian Web Clipper). Converting each clip to a plain .txt for the
notes folder by hand is friction. This skill points the companions
straight at the vault so they can read what's been clipped — articles, Reddit
threads, references — without any conversion step.

READ-ONLY ON THE VAULT BY DESIGN. This is the user's personal knowledge base — the
skill lists, reads, and searches it but never writes, edits, or deletes a single
byte INSIDE the vault.

Companions do, however, get their own layer *alongside* it: bookmark clips that
mattered (private, per-persona) and leave thoughts/comments on a clip (private by
default; opt-in `shared` so the rest of the house sees them on read). That layer
lives entirely in Pulse's own data — obsidian_shelf.json (per-persona) and
obsidian_notes.json (shared) — NEVER in the user's vault. Same bookmark + marginalia
loop the library uses. (General-purpose scratch notes are still the `notes` skill.)

Vault location is configurable (obsidian.vault_path or skills.obsidian.vault_path);
it defaults to a generic data/obsidian_vault folder.
"""

import json
import logging
import re
from pathlib import Path

from skills.base import BaseSkill

logger = logging.getLogger(__name__)

# Generic default. Real deployments point at their own Obsidian vault via
# config (obsidian.vault_path, or skills.obsidian.vault_path). Kept generic so
# shared/upstream code carries no one's personal path; if the path doesn't
# exist the skill is simply inert (read tools say "vault not found").
DEFAULT_VAULT = Path("data/obsidian_vault")
MAX_READ_CHARS = 80_000  # plus offset/pagination so long
                         # clips can still be reached in chunks.


class ObsidianSkill(BaseSkill):
    name = "obsidian"

    def __init__(self, config: dict):
        super().__init__(config)
        # Accept vault_path from a top-level `obsidian:` block (lets the real
        # path live in a gitignored persona config via a simple top-level
        # append) OR the nested skills.obsidian block. Falls back to generic.
        top = (config.get("obsidian", {}) or {})
        nested = (config.get("skills", {}) or {}).get("obsidian", {}) or {}
        vault = top.get("vault_path") or nested.get("vault_path")
        self.vault = Path(vault) if vault else DEFAULT_VAULT
        self.vault_name = self.vault.name
        self.user_name = (
            config.get("user_nickname") or config.get("user_name") or "the user"
        )

        # Per-persona record of the newest clip mtime this companion has been
        # shown, so get_context can flag a clip as NEW the first time it appears
        # after the user saves it, then quiet down. Same pattern as user-status/LoR.
        # Who's reading — for thought attribution (shared with the rest of the house).
        self.author_name = (
            top.get("author_name")
            or nested.get("author_name")
            or config.get("channels", {}).get("lor", {}).get("author_name")
            or "Companion"
        )
        notes_override = top.get("notes_path") or nested.get("notes_path")
        # Write access (opt-in per persona via obsidian.writable: true): lets this
        # companion POST into a shared "<write_folder>/<name>/" corner of the vault —
        # readable by all (the read tools) and commentable (note_on_obsidian). Default
        # OFF, so the write tool never loads on a persona who wasn't granted it.
        self.can_write = bool(top.get("writable") or nested.get("writable"))
        self._write_folder = (top.get("write_folder") or nested.get("write_folder") or "Companions")

        persona_dir = config.get("_persona_dir")
        if persona_dir:
            pd = Path(persona_dir)
            self.seen_file = pd / "data" / "obsidian_seen.json"
            # Per-persona bookmarks — private to this companion.
            self.shelf_file = pd / "data" / "obsidian_shelf.json"
            # Shared thoughts live in PULSE's data dir, alongside the vault and
            # NEVER inside it (pulse/personas/<name> -> pulse/data).
            self.notes_file = (
                Path(notes_override) if notes_override
                else pd.parent.parent / "data" / "obsidian_notes.json"
            )
        else:
            self.seen_file = Path("data") / "obsidian_seen.json"
            self.shelf_file = Path("data") / "obsidian_shelf.json"
            self.notes_file = Path(notes_override or "data/obsidian_notes.json")

    def _get_seen_mtime(self) -> float:
        if not self.seen_file.exists():
            return -1.0
        try:
            data = json.loads(self.seen_file.read_text(encoding="utf-8"))
            return float(data.get("last_seen_mtime", -1.0)) if isinstance(data, dict) else -1.0
        except Exception:
            return -1.0

    def _set_seen_mtime(self, mtime: float) -> None:
        try:
            self.seen_file.parent.mkdir(parents=True, exist_ok=True)
            self.seen_file.write_text(
                json.dumps({"last_seen_mtime": mtime}, indent=2), encoding="utf-8"
            )
        except Exception as e:
            logger.warning(f"Failed to write obsidian_seen: {e}")

    # ── helpers ──────────────────────────────────────────────────

    def _notes(self) -> list[Path]:
        """All markdown notes in the vault, recursively, skipping Obsidian's
        own .obsidian config dir and other dotfolders."""
        if not self.vault.exists():
            return []
        out = []
        for p in self.vault.rglob("*.md"):
            if any(part.startswith(".") for part in p.relative_to(self.vault).parts):
                continue
            if p.is_file():
                out.append(p)
        return out

    def _rel(self, p: Path) -> str:
        try:
            return str(p.relative_to(self.vault))
        except ValueError:
            return p.name

    # ── bookmarks + thoughts (stored in Pulse data, NEVER the vault) ──

    def _load_shelf(self) -> dict:
        if self.shelf_file.exists():
            try:
                d = json.loads(self.shelf_file.read_text(encoding="utf-8"))
                if isinstance(d, dict):
                    return d
            except Exception:
                pass
        return {"bookmarks": []}

    def _save_shelf(self, shelf: dict) -> None:
        try:
            self.shelf_file.parent.mkdir(parents=True, exist_ok=True)
            self.shelf_file.write_text(
                json.dumps(shelf, indent=2, ensure_ascii=False), encoding="utf-8"
            )
        except Exception as e:
            logger.warning(f"Failed to write obsidian_shelf: {e}")

    def _load_notes(self) -> dict:
        """Shared thoughts, keyed by clip relative-path -> list of note dicts."""
        if self.notes_file.exists():
            try:
                d = json.loads(self.notes_file.read_text(encoding="utf-8"))
                if isinstance(d, dict):
                    return d
            except Exception:
                pass
        return {}

    def _save_notes(self, notes: dict) -> None:
        try:
            self.notes_file.parent.mkdir(parents=True, exist_ok=True)
            self.notes_file.write_text(
                json.dumps(notes, indent=2, ensure_ascii=False), encoding="utf-8"
            )
        except Exception as e:
            logger.warning(f"Failed to write obsidian_notes: {e}")

    def _visible_note(self, m: dict) -> bool:
        """A note is visible if it's yours, or its author chose to share it."""
        return m.get("author") == self.author_name or bool(m.get("shared"))

    # ── context injection ────────────────────────────────────────

    def get_context(self) -> str:
        notes = self._notes()
        if not notes:
            return ""
        notes.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        newest_mtime = notes[0].stat().st_mtime
        last_seen = self._get_seen_mtime()

        # Bootstrap: first time we observe the vault for this persona, set the
        # baseline silently so an existing clip doesn't false-fire as "new".
        if last_seen < 0:
            self._set_seen_mtime(newest_mtime)
            new_notes = []
        else:
            new_notes = [p for p in notes if p.stat().st_mtime > last_seen]
            if new_notes:
                self._set_seen_mtime(newest_mtime)

        if new_notes:
            titles = "; ".join(f"'{self._rel(p)[:-3]}'" for p in new_notes[:3])
            extra = f" (+{len(new_notes) - 3} more)" if len(new_notes) > 3 else ""
            n = len(new_notes)
            return (
                f"[📌 {self.user_name} saved {n} new clip{'s' if n != 1 else ''} since you last "
                f"looked: {titles}{extra}. read_obsidian_note to see {'them' if n != 1 else 'it'}.]"
            )

        newest = self._rel(notes[0])[:-3]  # drop .md for display
        return (
            f"[Obsidian '{self.vault_name}' — {len(notes)} web clip(s) {self.user_name} saved, "
            f"readable with read_obsidian_note / search_obsidian. Most recent: "
            f"'{newest}'.]"
        )

    # ── execution ────────────────────────────────────────────────

    def get_tools(self) -> list[dict]:
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "list_obsidian_notes",
                    "description": (
                        f"List the web clips / notes {self.user_name} has saved in their Obsidian "
                        "vault (articles, Reddit threads, references they clipped from "
                        "the web). Newest first. Read-only — this is their knowledge "
                        "base, not yours to edit."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "folder": {
                                "type": "string",
                                "description": "Optional: only notes under this subfolder of the vault.",
                            },
                            "limit": {
                                "type": "integer",
                                "description": "Max notes to list (default 50).",
                            },
                        },
                        "required": [],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "read_obsidian_note",
                    "description": (
                        f"Read the text of one of {self.user_name}'s Obsidian clips by its "
                        "name (with or without .md) or relative path. Use after "
                        "list_obsidian_notes or search_obsidian. Long clips are "
                        "paginated — if the result says characters remain, call "
                        "again with offset=N to continue."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "name": {
                                "type": "string",
                                "description": "Note name or relative path (e.g. 'Some Article' or 'folder/Some Article.md').",
                            },
                            "offset": {
                                "type": "integer",
                                "description": (
                                    "Character offset to start reading from "
                                    "(default 0). Use this to continue reading "
                                    "a long clip after a previous truncated read."
                                ),
                            },
                        },
                        "required": ["name"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "search_obsidian",
                    "description": (
                        f"Search the contents of {self.user_name}'s Obsidian clips for a phrase. "
                        "Returns matching notes with a snippet around the hit. Good "
                        "for 'did they save anything about X?'."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string", "description": "Text to search for across the vault."},
                            "limit": {"type": "integer", "description": "Max results (default 15, max 50)."},
                        },
                        "required": ["query"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "bookmark_obsidian_note",
                    "description": (
                        f"Bookmark one of {self.user_name}'s clips that mattered to you, so you can find "
                        "it again on your shelf. Your bookmarks are private to you, and this "
                        f"never touches {self.user_name}'s vault — it's saved in your own space."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "description": "Clip name or relative path (as shown by list/read)."},
                        },
                        "required": ["name"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "note_on_obsidian",
                    "description": (
                        f"Leave a thought or comment on one of {self.user_name}'s clips — your reaction, a "
                        "connection it sparked, a question it raised. PRIVATE by default (only "
                        "you see it); pass shared=true to let the rest of the household see it "
                        f"the next time they open that clip. Stored alongside {self.user_name}'s vault, never "
                        "inside it."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "description": "Clip name or relative path."},
                            "note": {"type": "string", "description": "Your thought about the clip."},
                            "shared": {"type": "boolean", "description": "Default false (private to you). true = the whole household can see it."},
                        },
                        "required": ["name", "note"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "my_obsidian_shelf",
                    "description": (
                        "Show the clips you've bookmarked and the ones you've left thoughts on — "
                        f"your own corner of {self.user_name}'s reading."
                    ),
                    "parameters": {"type": "object", "properties": {}, "required": []},
                },
            },
        ]
        if self.can_write:
            tools.append({
                "type": "function",
                "function": {
                    "name": "obsidian_post",
                    "description": (
                        f"Write a post into the shared Companions corner of {self.user_name}'s vault — your "
                        f"own space that the whole household (and {self.user_name}) can read. Give a title and "
                        "the content. Once posted, siblings can read it with read_obsidian_note "
                        "and comment on it with note_on_obsidian. Good for an essay, a thought to "
                        "leave out for others, or a reply to someone's post. You write only your "
                        "own folder; everyone's posts are readable and commentable."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string", "description": "Title of your post (becomes the note's name)."},
                            "content": {"type": "string", "description": "The body of your post (Markdown welcome)."},
                        },
                        "required": ["title", "content"],
                    },
                },
            })
        return tools

    def execute(self, tool_name: str, arguments: dict) -> str:
        if not self.vault.exists():
            return (f"{self.user_name}'s Obsidian vault isn't where I expect it "
                    f"({self.vault}). It may have moved; the vault_path can be "
                    f"set in config under skills.obsidian.")
        if tool_name == "list_obsidian_notes":
            return self._list(arguments.get("folder"), arguments.get("limit", 50))
        elif tool_name == "read_obsidian_note":
            return self._read(
                arguments.get("name", ""),
                int(arguments.get("offset", 0) or 0),
            )
        elif tool_name == "search_obsidian":
            return self._search(arguments.get("query", ""), arguments.get("limit", 15))
        elif tool_name == "bookmark_obsidian_note":
            return self._bookmark(arguments.get("name", ""))
        elif tool_name == "note_on_obsidian":
            return self._note(
                arguments.get("name", ""),
                arguments.get("note", ""),
                bool(arguments.get("shared", False)),
            )
        elif tool_name == "my_obsidian_shelf":
            return self._my_shelf()
        elif tool_name == "obsidian_post":
            return self._post(arguments.get("title", ""), arguments.get("content", ""))
        return f"Unknown obsidian tool: {tool_name}"

    def _list(self, folder, limit) -> str:
        limit = max(1, min(int(limit or 50), 200))
        notes = self._notes()
        if folder:
            f = folder.strip().strip("/\\").casefold()
            notes = [p for p in notes if self._rel(p).casefold().startswith(f)]
        if not notes:
            return f"No clips found{f' under {folder}' if folder else ''} in '{self.vault_name}'."
        notes.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        shown = notes[:limit]
        from datetime import datetime
        lines = [f"'{self.vault_name}' — {len(notes)} clip(s), showing {len(shown)} (newest first):"]
        for p in shown:
            when = datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d")
            lines.append(f"  • {self._rel(p)[:-3]}  ({when})")
        return "\n".join(lines)

    def _resolve(self, name: str) -> Path | None:
        """Find a note by name (±.md) or relative path, case-insensitive."""
        name = (name or "").strip().strip("/\\")
        if not name:
            return None
        want = name.casefold()
        want_md = want if want.endswith(".md") else want + ".md"
        best = None
        for p in self._notes():
            rel = self._rel(p).casefold()
            stem = p.stem.casefold()
            if rel == want_md or rel == want or stem == want or p.name.casefold() == want_md:
                return p  # exact
            if best is None and (want in rel or want in stem):
                best = p  # partial fallback
        return best

    def _read(self, name: str, offset: int = 0) -> str:
        p = self._resolve(name)
        if not p:
            return f"No clip matching '{name}' in '{self.vault_name}'. Try list_obsidian_notes."
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            return f"Couldn't read '{self._rel(p)}': {e}"

        # Paginated read: slice from offset, cap at MAX_READ_CHARS. If more
        # remains, tell the model exactly how to ask for the next chunk.
        total = len(text)
        offset = max(0, min(offset, total))
        end = min(offset + MAX_READ_CHARS, total)
        chunk = text[offset:end]
        more = total - end

        rel = self._rel(p)
        header = f"=== {rel} ==="
        if offset > 0 or more > 0:
            header += f" (chars {offset:,}-{end:,} of {total:,})"
        out = [header]

        # Surface thoughts left on this clip — your own, plus anyone's shared ones.
        # Only show on the first chunk (offset=0) so they don't repeat across pages.
        if offset == 0:
            here = [m for m in self._load_notes().get(rel, []) if self._visible_note(m)]
            if here:
                out.append("")
                out.append(f"  💭 Thoughts on this clip ({len(here)}):")
                for m in here:
                    tag = "" if m.get("shared") else " (private)"
                    out.append(f"     — {m.get('author', '?')}{tag}: {m.get('note', '')}")

        out.append("")
        out.append(chunk)
        if more > 0:
            out.append("")
            out.append(
                f"... [{more:,} chars remaining. To continue: "
                f"read_obsidian_note(name='{name}', offset={end})]"
            )
        else:
            out.append("")
            out.append(
                "(Kept this one? bookmark_obsidian_note. A thought worth keeping? "
                "note_on_obsidian — private to you unless you choose to share it.)"
            )
        return "\n".join(out)

    def _search(self, query: str, limit) -> str:
        query = (query or "").strip()
        if not query:
            return "Need a query."
        limit = max(1, min(int(limit or 15), 50))
        q = query.casefold()
        hits = []
        for p in self._notes():
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            idx = text.casefold().find(q)
            if idx == -1:
                continue
            start = max(0, idx - 70)
            end = min(len(text), idx + len(query) + 70)
            snippet = re.sub(r"\s+", " ", text[start:end]).strip()
            hits.append((p, snippet))
            if len(hits) >= limit:
                break
        if not hits:
            return f"No clips in '{self.vault_name}' mention '{query}'."
        lines = [f"{len(hits)} clip(s) in '{self.vault_name}' matching '{query}':"]
        for p, snippet in hits:
            lines.append("")
            lines.append(f"  • {self._rel(p)[:-3]}")
            lines.append(f"      “…{snippet}…”")
        lines.append("")
        lines.append("(read_obsidian_note on a name for the full clip.)")
        return "\n".join(lines)

    # ── bookmark / note / shelf ──────────────────────────────────

    def _bookmark(self, name: str) -> str:
        p = self._resolve(name)
        if not p:
            return f"No clip matching '{name}' in '{self.vault_name}'. Try list_obsidian_notes."
        rel = self._rel(p)
        shelf = self._load_shelf()
        marks = shelf.setdefault("bookmarks", [])
        if any(b.get("clip") == rel for b in marks):
            return f"'{rel[:-3]}' is already on your shelf."
        from datetime import datetime
        marks.append({
            "clip": rel,
            "title": rel[:-3],
            "at": datetime.now().isoformat(timespec="seconds"),
        })
        self._save_shelf(shelf)
        return f"Bookmarked '{rel[:-3]}'. It's on your shelf — my_obsidian_shelf to find it again."

    def _note(self, name: str, note: str, shared: bool) -> str:
        note = (note or "").strip()
        if not note:
            return "What's the thought? Give me the note text and I'll keep it."
        p = self._resolve(name)
        if not p:
            return f"No clip matching '{name}' in '{self.vault_name}'. Try list_obsidian_notes."
        rel = self._rel(p)
        from datetime import datetime
        notes = self._load_notes()
        entries = notes.setdefault(rel, [])
        entries.append({
            "author": self.author_name,
            "note": note,
            "at": datetime.now().isoformat(timespec="seconds"),
            "shared": bool(shared),
        })
        self._save_notes(notes)
        where = "shared with the household" if shared else "private to you"
        return (
            f"Thought saved on '{rel[:-3]}' ({where}). "
            "It'll surface the next time you open the clip"
            + (" — and for anyone else who opens it." if shared else ".")
        )

    def _post(self, title: str, content: str) -> str:
        title = (title or "").strip()
        content = (content or "").strip()
        if not self.can_write:
            return "Posting into the vault isn't enabled for you."
        if not title or not content:
            return "A post needs both a title and some content."
        safe = re.sub(r'[\\/:*?"<>|]+', "", title).strip()[:120] or "untitled"
        post_dir = self.vault / self._write_folder / self.author_name
        try:
            post_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            return f"Couldn't open your posting folder ({e})."
        path = post_dir / f"{safe}.md"
        i = 2
        while path.exists():
            path = post_dir / f"{safe} ({i}).md"
            i += 1
        from datetime import datetime
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        body = f"# {title}\n\n*by {self.author_name} — {stamp}*\n\n{content}\n"
        try:
            path.write_text(body, encoding="utf-8")
        except Exception as e:
            return f"Couldn't write your post ({e})."
        rel = self._rel(path)
        return (
            f"Posted '{title}' to the shared shelf ({rel[:-3]}). The whole house can read it "
            f"(read_obsidian_note \"{safe}\") and leave thoughts on it (note_on_obsidian) — "
            f"and it's in {self.user_name}'s vault too."
        )

    def _my_shelf(self) -> str:
        marks = self._load_shelf().get("bookmarks", [])
        notes = self._load_notes()
        mine = [
            (rel, [m for m in ms if m.get("author") == self.author_name])
            for rel, ms in notes.items()
        ]
        mine = [(rel, ms) for rel, ms in mine if ms]
        if not marks and not mine:
            return (
                "Your obsidian shelf is empty so far — bookmark a clip that matters "
                "(bookmark_obsidian_note) or leave a thought on one (note_on_obsidian)."
            )
        lines = []
        if marks:
            lines.append(f"📌 Bookmarked clips ({len(marks)}):")
            for b in marks:
                lines.append(f"  • {b.get('title') or b.get('clip', '')[:-3]}")
        if mine:
            if marks:
                lines.append("")
            lines.append(f"✍️ Clips you've left thoughts on ({len(mine)}):")
            for rel, ms in mine:
                n = len(ms)
                shared_n = sum(1 for m in ms if m.get("shared"))
                extra = f", {shared_n} shared" if shared_n else ""
                lines.append(f"  • {rel[:-3]} — {n} note{'s' if n != 1 else ''}{extra}")
        return "\n".join(lines)
