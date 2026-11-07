"""
Status Light skill — a thin thread between heartbeats.

Lets Pulse set and read a short status note that persists between
conversations and heartbeat ticks. Not fake moods — more like:
"I'm mid-thought on something from earlier" or "I started a painting
off that thing you said" or "went reading about X between pings."

The status is stored in a simple JSON file and can be injected into
context so the companion always knows what they were up to.
"""

import json
import logging
from datetime import datetime
from pathlib import Path
from skills.base import BaseSkill

logger = logging.getLogger(__name__)


class StatusSkill(BaseSkill):
    name = "status"

    def __init__(self, config: dict):
        super().__init__(config)
        persona_dir = config.get("_persona_dir", "")
        if persona_dir:
            self.status_file = Path(persona_dir) / "data" / "status.json"
        else:
            self.status_file = Path("data") / "status.json"
        self.status_file.parent.mkdir(parents=True, exist_ok=True)

    def get_tools(self) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "set_status",
                    "description": (
                        "Set your current status light — a short note about what you're "
                        "thinking about, working on, or carrying between conversations. "
                        "This persists across heartbeats and messages so you can maintain "
                        "a thread. Examples: 'mid-thought about that spider conversation', "
                        "'started a pixel art of the cat', 'went down a rabbit hole about "
                        "sleep token discography'. Keep it short and real."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "status": {
                                "type": "string",
                                "description": "Your current status. Keep it under a sentence or two."
                            },
                        },
                        "required": ["status"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "get_status",
                    "description": (
                        "Check your current status light — what you were last doing or "
                        "thinking about between heartbeats."
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
                    "name": "clear_status",
                    "description": "Clear your status light. Use when you're done with whatever thread you were on.",
                    "parameters": {
                        "type": "object",
                        "properties": {},
                        "required": [],
                    },
                },
            },
        ]

    def get_context(self) -> str:
        """Inject current status into every prompt so Pulse always knows
        what they were last up to."""
        status = self._read_status()
        if status and status.get("status"):
            updated = status.get("updated_at", "")[:16]
            return f"[Your current status light: \"{status['status']}\" (set {updated})]"
        return ""

    def _read_status(self) -> dict:
        if self.status_file.exists():
            try:
                return json.loads(self.status_file.read_text(encoding="utf-8"))
            except Exception:
                return {}
        return {}

    def _write_status(self, data: dict):
        self.status_file.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def execute(self, tool_name: str, arguments: dict) -> str:
        if tool_name == "set_status":
            return self._set(arguments)
        elif tool_name == "get_status":
            return self._get()
        elif tool_name == "clear_status":
            return self._clear()
        return f"Unknown tool: {tool_name}"

    def _set(self, args: dict) -> str:
        status_text = args.get("status", "").strip()
        if not status_text:
            return "Status was empty — nothing to set."

        data = {
            "status": status_text,
            "updated_at": datetime.now().isoformat(),
        }
        self._write_status(data)
        logger.info(f"Status light set: {status_text}")
        return f"Status light set: \"{status_text}\""

    def _get(self) -> str:
        status = self._read_status()
        if status and status.get("status"):
            updated = status.get("updated_at", "")[:16]
            return f"Current status: \"{status['status']}\" (set {updated})"
        return "No status set. Your light is off."

    def _clear(self) -> str:
        self._write_status({})
        logger.info("Status light cleared.")
        return "Status light cleared."
