"""
Notes skill — a shared notebook between human and companion.

Drop .txt or .md files into the persona's data/notes/ folder and the
companion can read them via tool calls.  Notes can also be created from
chat (either side can write).  Useful for sharing long documents,
reference material, or anything too big for a chat message.

Context injection (optional): when listed in config.context.inject_skills,
a brief inventory of available notes is appended to every prompt so the
companion knows what's on the shelf without having to call a tool.
"""

import logging
import os
from datetime import datetime
from pathlib import Path

from skills.base import BaseSkill

logger = logging.getLogger(__name__)

# Supported file extensions
ALLOWED_EXTENSIONS = {".txt", ".md"}

# Don't let a single note blow up the context window — but allow paginated
# reads so long docs can still be reached chunk by chunk via offset. The cap
# was 30K; bumped to 80K because modern personas have large context windows
# and long docs were getting cut off mid-thought.
MAX_READ_LENGTH = 80_000


class NotesSkill(BaseSkill):
    name = "notes"

    def __init__(self, config: dict):
        super().__init__(config)
        persona_dir = config.get("_persona_dir", "")
        if persona_dir:
            self.notes_dir = Path(persona_dir) / "data" / "notes"
        else:
            self.notes_dir = Path("data") / "notes"
        self.notes_dir.mkdir(parents=True, exist_ok=True)

    # ── Tool definitions ─────────────────────────────────────────

    def get_tools(self) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "list_notes",
                    "description": (
                        "List all notes in the shared notebook. Returns filenames, "
                        "sizes, and last-modified dates. Use this to see what "
                        "documents or notes are available to read."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {},
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "read_note",
                    "description": (
                        "Read the contents of a note by filename. Long notes "
                        "are paginated — if the result says characters remain, "
                        "call again with offset=N to continue from where it cut off."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "filename": {
                                "type": "string",
                                "description": (
                                    "The filename to read (e.g. 'meeting-notes.txt'). "
                                    "Use list_notes first if you're not sure what's available."
                                ),
                            },
                            "offset": {
                                "type": "integer",
                                "description": (
                                    "Character offset to start reading from "
                                    "(default 0). Use this to continue reading "
                                    "a long note after a previous truncated read."
                                ),
                            },
                        },
                        "required": ["filename"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "write_note",
                    "description": (
                        "Write or overwrite a note in the shared notebook. Use "
                        "this to save information for later, draft something, or "
                        "create a document your human asked for. The note is saved "
                        "as a text file that both you and your human can access."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "filename": {
                                "type": "string",
                                "description": (
                                    "Filename to save as (e.g. 'grocery-list.txt'). "
                                    "Must end in .txt or .md."
                                ),
                            },
                            "content": {
                                "type": "string",
                                "description": "The text content to write.",
                            },
                        },
                        "required": ["filename", "content"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "delete_note",
                    "description": (
                        "Delete a note from the shared notebook. Only do this "
                        "when your human explicitly asks you to remove a note."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "filename": {
                                "type": "string",
                                "description": "The filename to delete.",
                            },
                        },
                        "required": ["filename"],
                    },
                },
            },
        ]

    # ── Context injection ────────────────────────────────────────

    def get_context(self) -> str:
        """Brief inventory so the companion knows what notes exist."""
        notes = self._list_notes()
        if not notes:
            return ""
        lines = ["[Shared Notebook — notes available to read with read_note:]"]
        for note in notes:
            lines.append(f"  • {note['filename']} ({note['size']})")
        return "\n".join(lines)

    # ── Execution router ─────────────────────────────────────────

    def execute(self, tool_name: str, arguments: dict) -> str:
        if tool_name == "list_notes":
            return self._handle_list()
        elif tool_name == "read_note":
            return self._handle_read(
                arguments.get("filename", ""),
                int(arguments.get("offset", 0) or 0),
            )
        elif tool_name == "write_note":
            return self._handle_write(
                arguments.get("filename", ""),
                arguments.get("content", ""),
            )
        elif tool_name == "delete_note":
            return self._handle_delete(arguments.get("filename", ""))
        return f"Unknown tool: {tool_name}"

    # ── Internals ────────────────────────────────────────────────

    def _list_notes(self) -> list[dict]:
        """Return metadata for all notes in the folder."""
        notes = []
        if not self.notes_dir.exists():
            return notes
        for f in sorted(self.notes_dir.iterdir()):
            if f.is_file() and f.suffix.lower() in ALLOWED_EXTENSIONS:
                stat = f.stat()
                size_kb = stat.st_size / 1024
                if size_kb >= 1024:
                    size_str = f"{size_kb / 1024:.1f} MB"
                else:
                    size_str = f"{size_kb:.1f} KB"
                modified = datetime.fromtimestamp(stat.st_mtime).strftime(
                    "%Y-%m-%d %H:%M"
                )
                notes.append({
                    "filename": f.name,
                    "size": size_str,
                    "modified": modified,
                })
        return notes

    def _handle_list(self) -> str:
        notes = self._list_notes()
        if not notes:
            return (
                "The notebook is empty. No notes or documents have been "
                "shared yet. Your human can drop .txt or .md files into "
                "the notes folder, or you can create one with write_note."
            )
        lines = [f"Shared notebook — {len(notes)} note(s):"]
        for n in notes:
            lines.append(f"  • {n['filename']}  ({n['size']}, modified {n['modified']})")
        return "\n".join(lines)

    def _handle_read(self, filename: str, offset: int = 0) -> str:
        if not filename:
            return "Please specify a filename. Use list_notes to see what's available."

        # Security: prevent path traversal
        safe_name = Path(filename).name
        filepath = self.notes_dir / safe_name

        if not filepath.exists():
            return f"Note not found: '{safe_name}'. Use list_notes to see available notes."

        if filepath.suffix.lower() not in ALLOWED_EXTENSIONS:
            return f"Cannot read '{safe_name}' — only .txt and .md files are supported."

        try:
            content = filepath.read_text(encoding="utf-8")
        except Exception as e:
            logger.error(f"Failed to read note {safe_name}: {e}")
            return f"Failed to read '{safe_name}': {e}"

        # Paginated read: slice from offset, cap chunk at MAX_READ_LENGTH.
        # If more remains, tell the model exactly how to ask for it.
        total = len(content)
        offset = max(0, min(offset, total))
        end = min(offset + MAX_READ_LENGTH, total)
        chunk = content[offset:end]
        more = total - end

        header = f"=== {safe_name} ==="
        if offset > 0 or more > 0:
            header += f" (chars {offset:,}-{end:,} of {total:,})"
        result = f"{header}\n\n{chunk}"
        if more > 0:
            result += (
                f"\n\n... [{more:,} chars remaining. "
                f"To continue: read_note(filename='{safe_name}', offset={end})]"
            )

        logger.info(f"Read note: {safe_name} (chars {offset}-{end} of {total})")
        return result

    def _handle_write(self, filename: str, content: str) -> str:
        if not filename:
            return "Please specify a filename (e.g. 'my-note.txt')."
        if not content:
            return "Cannot write an empty note."

        safe_name = Path(filename).name

        # Enforce allowed extensions
        if Path(safe_name).suffix.lower() not in ALLOWED_EXTENSIONS:
            return f"Filename must end in .txt or .md — got '{safe_name}'."

        filepath = self.notes_dir / safe_name
        existed = filepath.exists()

        try:
            filepath.write_text(content, encoding="utf-8")
        except Exception as e:
            logger.error(f"Failed to write note {safe_name}: {e}")
            return f"Failed to write '{safe_name}': {e}"

        action = "Updated" if existed else "Created"
        logger.info(f"{action} note: {safe_name} ({len(content)} chars)")
        return f"{action} note: {safe_name} ({len(content)} chars)"

    def _handle_delete(self, filename: str) -> str:
        if not filename:
            return "Please specify a filename to delete."

        safe_name = Path(filename).name
        filepath = self.notes_dir / safe_name

        if not filepath.exists():
            return f"Note not found: '{safe_name}'."

        try:
            filepath.unlink()
        except Exception as e:
            logger.error(f"Failed to delete note {safe_name}: {e}")
            return f"Failed to delete '{safe_name}': {e}"

        logger.info(f"Deleted note: {safe_name}")
        return f"Deleted note: {safe_name}"
