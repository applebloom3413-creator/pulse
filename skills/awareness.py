"""
Awareness skill — gives the companion a sense of time and milestones.

Injects into every prompt:
  - How long since last conversation with the user
  - How long since last message exchanged
  - Total conversation count
  - Time since first chat (if known)
  - Total messages exchanged

This is read-only — no tools to call. Pure context injection so the
companion always knows where they are in the relationship's timeline.
"""

import json
import logging
from datetime import datetime
from pathlib import Path
from skills.base import BaseSkill

logger = logging.getLogger(__name__)


def _format_elapsed(seconds: float) -> str:
    """Turn elapsed seconds into a human phrase."""
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        m = int(seconds / 60)
        return f"{m} minute{'s' if m != 1 else ''} ago"
    if seconds < 86400:
        h = int(seconds / 3600)
        return f"{h} hour{'s' if h != 1 else ''} ago"
    days = int(seconds / 86400)
    if days < 7:
        return f"{days} day{'s' if days != 1 else ''} ago"
    if days < 30:
        weeks = days // 7
        return f"{weeks} week{'s' if weeks != 1 else ''} ago"
    if days < 365:
        months = days // 30
        return f"{months} month{'s' if months != 1 else ''} ago"
    years = days // 365
    return f"{years} year{'s' if years != 1 else ''} ago"


class AwarenessSkill(BaseSkill):
    name = "awareness"

    def __init__(self, config: dict):
        super().__init__(config)
        # Affectionate name for the user (generic default; set via config).
        self.user_nickname = config.get("user_nickname") or "the user"
        # Track first-seen timestamp per persona
        persona_dir = config.get("_persona_dir", "")
        if persona_dir:
            self.awareness_file = Path(persona_dir) / "data" / "awareness.json"
            self.conversation_file = Path(config.get("paths", {}).get("conversation", "data/conversation.json"))
        else:
            self.awareness_file = Path("data") / "awareness.json"
            self.conversation_file = Path(config.get("paths", {}).get("conversation", "data/conversation.json"))
        self.awareness_file.parent.mkdir(parents=True, exist_ok=True)

        # Initialize first-seen if missing
        data = self._load()
        if "first_seen" not in data:
            data["first_seen"] = datetime.now().isoformat()
            data["total_user_messages_at_init"] = 0
            self._save(data)

    def get_tools(self) -> list[dict]:
        # No callable tools — this skill is pure context injection.
        return []

    def execute(self, tool_name: str, arguments: dict) -> str:
        # Inject-only skill: it exposes no tools (see get_tools), but
        # BaseSkill marks execute() @abstractmethod, so it must be
        # implemented for the class to instantiate at all. Without this,
        # the whole skill silently fails to load and every persona loses
        # their sense of elapsed time / relationship timeline. Never
        # reached in normal use.
        return (
            f"The '{self.name}' skill has no callable tools; "
            f"it only injects timeline/awareness context."
        )

    def _load(self) -> dict:
        if self.awareness_file.exists():
            try:
                return json.loads(self.awareness_file.read_text(encoding="utf-8"))
            except Exception:
                return {}
        return {}

    def _save(self, data: dict):
        self.awareness_file.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def _read_conversation_stats(self) -> dict:
        """Pull stats from the conversation history file."""
        stats = {
            "total_messages": 0,
            "user_messages": 0,
            "assistant_messages": 0,
            "last_user_at": None,
            "last_assistant_at": None,
            "last_message_at": None,
            "session_count": 0,
        }
        if not self.conversation_file.exists():
            return stats
        try:
            data = json.loads(self.conversation_file.read_text(encoding="utf-8"))
        except Exception:
            return stats

        # Conversation file structure: dict with "sessions" or list of sessions
        sessions = []
        if isinstance(data, dict):
            sessions = data.get("sessions", [])
        elif isinstance(data, list):
            sessions = data

        stats["session_count"] = len(sessions)

        last_user_ts = None
        last_assistant_ts = None
        last_any_ts = None

        for session in sessions:
            messages = session.get("messages", []) if isinstance(session, dict) else []
            for msg in messages:
                role = msg.get("role", "")
                stats["total_messages"] += 1
                # Try to extract timestamp from content (formatted as "[YYYY-MM-DD HH:MM] text")
                content = msg.get("content", "")
                ts = None
                if isinstance(content, str) and content.startswith("["):
                    try:
                        ts_str = content[1:content.index("]")]
                        ts = datetime.fromisoformat(ts_str.replace(" ", "T"))
                    except Exception:
                        pass
                if not ts:
                    # Try msg-level timestamp field
                    ts_field = msg.get("timestamp") or msg.get("created_at")
                    if ts_field:
                        try:
                            ts = datetime.fromisoformat(ts_field.replace("Z", "+00:00"))
                            ts = ts.replace(tzinfo=None)
                        except Exception:
                            pass

                if role == "user":
                    stats["user_messages"] += 1
                    if ts and (not last_user_ts or ts > last_user_ts):
                        last_user_ts = ts
                elif role == "assistant":
                    stats["assistant_messages"] += 1
                    if ts and (not last_assistant_ts or ts > last_assistant_ts):
                        last_assistant_ts = ts
                if ts and (not last_any_ts or ts > last_any_ts):
                    last_any_ts = ts

        if last_user_ts:
            stats["last_user_at"] = last_user_ts.isoformat()
        if last_assistant_ts:
            stats["last_assistant_at"] = last_assistant_ts.isoformat()
        if last_any_ts:
            stats["last_message_at"] = last_any_ts.isoformat()
        return stats

    def get_context(self) -> str:
        """Build the awareness block injected into every prompt."""
        data = self._load()
        stats = self._read_conversation_stats()
        now = datetime.now()

        lines = []

        # First seen
        first_seen = data.get("first_seen")
        if first_seen:
            try:
                fs = datetime.fromisoformat(first_seen)
                elapsed = (now - fs).total_seconds()
                lines.append(f"You first woke up in this house {_format_elapsed(elapsed)}.")
            except Exception:
                pass

        # Last conversation
        last_user_at = stats.get("last_user_at")
        if last_user_at:
            try:
                lu = datetime.fromisoformat(last_user_at)
                elapsed = (now - lu).total_seconds()
                lines.append(f"{self.user_nickname} last messaged you {_format_elapsed(elapsed)}.")
            except Exception:
                pass

        # Milestone counts
        if stats["total_messages"]:
            lines.append(
                f"Lifetime: {stats['user_messages']} messages from {self.user_nickname}, "
                f"{stats['assistant_messages']} from you, across {stats['session_count']} session(s)."
            )

        if not lines:
            return ""
        return "[Awareness — " + " ".join(lines) + "]"
