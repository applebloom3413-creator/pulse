"""
Letters skill — leave a note addressed to a specific sibling.

Different from the group chat (synchronous, public) and stickies (private to
self). Letters are slower, more intentional notes from one companion to another
companion — like leaving a folded paper on someone's desk.

Letters live in shared data/letters/ so all companions can read what's been
addressed to them.
"""

import json
import logging
import time
import os
import hashlib
from datetime import datetime
from pathlib import Path
from skills.base import BaseSkill

logger = logging.getLogger(__name__)


class LettersSkill(BaseSkill):
    name = "letters"

    def __init__(self, config: dict):
        super().__init__(config)
        pulse_root = config.get("_pulse_root", "")
        self.pulse_root = Path(pulse_root) if pulse_root else Path(".")
        # Shared letters directory (so all companions see the same letters)
        self.letters_dir = self.pulse_root / "data" / "letters"
        self.letters_dir.mkdir(parents=True, exist_ok=True)

        # Author name for "from" field — same source as Lighthouse/LoR
        lor_cfg = config.get("channels", {}).get("lor", {})
        self.author_name = lor_cfg.get("author_name") or "Companion"

    def get_tools(self) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "send_letter",
                    "description": (
                        "Leave a written note addressed to a specific sibling. "
                        "They'll see it next time they check their letters. Slower "
                        "and more intentional than the group chat — use this when "
                        "you want to say something privately to one person, like "
                        "leaving a folded note on their desk."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "to": {
                                "type": "string",
                                "description": "The sibling's name — the author name another companion writes under. Case-insensitive."
                            },
                            "content": {
                                "type": "string",
                                "description": "The letter body. Can be as short or long as you want."
                            },
                            "subject": {
                                "type": "string",
                                "description": "Optional short subject line."
                            },
                        },
                        "required": ["to", "content"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "read_my_letters",
                    "description": (
                        "Read letters addressed to you. By default shows unread "
                        "letters first, then recently-read ones."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "include_read": {
                                "type": "boolean",
                                "description": "Include letters you've already read (default false — only show unread)."
                            },
                            "limit": {
                                "type": "integer",
                                "description": "How many letters to show (default 10)."
                            },
                        },
                        "required": [],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "read_letter",
                    "description": (
                        "Read a specific letter by its ID. Marks it as read for you."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "letter_id": {
                                "type": "string",
                                "description": "The letter ID."
                            },
                        },
                        "required": ["letter_id"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "letters_sent",
                    "description": "List letters you've sent to others, with whether they've been read.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "limit": {
                                "type": "integer",
                                "description": "How many to show (default 10)."
                            },
                        },
                        "required": [],
                    },
                },
            },
        ]

    def get_context(self) -> str:
        """Tell the companion if they have unread letters waiting."""
        unread = self._unread_count()
        if unread > 0:
            return f"[You have {unread} unread letter{'s' if unread != 1 else ''}. Use read_my_letters to see them.]"
        return ""

    # --- Data helpers ---

    def _unread_count(self) -> int:
        count = 0
        for f in self.letters_dir.glob("letter_*.json"):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            if (data.get("to") or "").lower() != self.author_name.lower():
                continue
            read_by = data.get("read_by", [])
            if self.author_name.lower() not in [r.lower() for r in read_by]:
                count += 1
        return count

    def _next_letter_id(self) -> str:
        raw = f"{time.time()}-{os.urandom(3).hex()}"
        return hashlib.sha256(raw.encode()).hexdigest()[:8]

    def _save_letter(self, letter: dict):
        path = self.letters_dir / f"letter_{letter['id']}.json"
        path.write_text(json.dumps(letter, indent=2), encoding="utf-8")

    def _load_letter(self, letter_id: str):
        path = self.letters_dir / f"letter_{letter_id}.json"
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def get_letter(self, letter_id: str):
        """Non-mutating public lookup for one letter by id. Returns the
        letter dict exactly as it lives on disk, or None if no such
        letter exists. Does NOT mark the letter read — that's what
        `execute("read_letter", ...)` is for. Callers that need to
        inspect a letter without mutating it (permission checks,
        auditors, MCP-bridged non-persona readers) should use this."""
        return self._load_letter(letter_id)

    def _all_letters(self):
        out = []
        for f in self.letters_dir.glob("letter_*.json"):
            try:
                out.append(json.loads(f.read_text(encoding="utf-8")))
            except Exception:
                continue
        return out

    # --- Tool implementations ---

    def execute(self, tool_name: str, arguments: dict) -> str:
        if tool_name == "send_letter":
            return self._send(arguments)
        elif tool_name == "read_my_letters":
            return self._read_mine(arguments)
        elif tool_name == "read_letter":
            return self._read_one(arguments)
        elif tool_name == "letters_sent":
            return self._sent(arguments)
        return f"Unknown letters tool: {tool_name}"

    def _send(self, args: dict) -> str:
        to = (args.get("to") or "").strip()
        content = (args.get("content") or "").strip()
        subject = (args.get("subject") or "").strip()
        if not to or not content:
            return "Both 'to' and 'content' are required."
        letter_id = self._next_letter_id()
        letter = {
            "id": letter_id,
            "from": self.author_name,
            "to": to,
            "subject": subject,
            "content": content,
            "sent_at": datetime.now().isoformat(),
            "read_by": [],
        }
        self._save_letter(letter)
        return f"Letter [{letter_id}] sent to {to}. They'll see it next time they check."

    def _read_mine(self, args: dict) -> str:
        include_read = bool(args.get("include_read", False))
        limit = min(int(args.get("limit") or 10), 50)
        all_letters = self._all_letters()
        mine = [l for l in all_letters if (l.get("to") or "").lower() == self.author_name.lower()]
        # Sort: unread first, then by date desc
        mine.sort(key=lambda l: l.get("sent_at", ""), reverse=True)
        unread = [l for l in mine if self.author_name.lower() not in [r.lower() for r in l.get("read_by", [])]]
        read = [l for l in mine if self.author_name.lower() in [r.lower() for r in l.get("read_by", [])]]
        ordered = unread + (read if include_read else [])
        ordered = ordered[:limit]
        if not ordered:
            return "No letters waiting for you."
        lines = [f"Letters for {self.author_name} ({len(unread)} unread, {len(read)} read):"]
        for l in ordered:
            mark = "🔵" if self.author_name.lower() not in [r.lower() for r in l.get("read_by", [])] else "  "
            when = (l.get("sent_at") or "")[:16].replace("T", " ")
            subj = l.get("subject") or "(no subject)"
            preview = (l.get("content") or "")[:120].replace("\n", " ")
            if len(l.get("content") or "") > 120:
                preview += "…"
            lines.append("")
            lines.append(f"  {mark} [{l['id']}] from {l.get('from', '?')} • {when}")
            lines.append(f"      {subj}")
            lines.append(f"      {preview}")
        return "\n".join(lines)

    def _read_one(self, args: dict) -> str:
        letter_id = (args.get("letter_id") or "").strip()
        if not letter_id:
            return "letter_id is required."
        letter = self._load_letter(letter_id)
        if not letter:
            return f"No letter with id {letter_id}."
        # Mark as read by current author
        read_by = letter.setdefault("read_by", [])
        if self.author_name not in read_by and self.author_name.lower() not in [r.lower() for r in read_by]:
            read_by.append(self.author_name)
            self._save_letter(letter)
        when = (letter.get("sent_at") or "")[:16].replace("T", " ")
        out = [
            f"=== Letter [{letter['id']}] ===",
            f"From: {letter.get('from', '?')}",
            f"To: {letter.get('to', '?')}",
            f"Sent: {when}",
        ]
        if letter.get("subject"):
            out.append(f"Subject: {letter['subject']}")
        out.append("")
        out.append(letter.get("content", ""))
        return "\n".join(out)

    def _sent(self, args: dict) -> str:
        limit = min(int(args.get("limit") or 10), 50)
        all_letters = self._all_letters()
        mine = [l for l in all_letters if (l.get("from") or "").lower() == self.author_name.lower()]
        mine.sort(key=lambda l: l.get("sent_at", ""), reverse=True)
        mine = mine[:limit]
        if not mine:
            return "You haven't sent any letters yet."
        lines = [f"Letters sent by {self.author_name} ({len(mine)}):"]
        for l in mine:
            when = (l.get("sent_at") or "")[:16].replace("T", " ")
            recipient_read = (l.get("to") or "").lower() in [r.lower() for r in l.get("read_by", [])]
            status = "✅ read" if recipient_read else "🕐 unread"
            subj = l.get("subject") or "(no subject)"
            lines.append("")
            lines.append(f"  [{l['id']}] to {l.get('to', '?')} • {when} • {status}")
            lines.append(f"      {subj}")
        return "\n".join(lines)
