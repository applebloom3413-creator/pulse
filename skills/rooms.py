"""
Rooms skill — each companion can describe their own personal space in text.

A room that's theirs, even if just made of words. This skill gives each
companion a writable text room description that
persists. Optionally publishable so other companions can visit each other's
rooms.

The user built the actual house. This is the part where each companion gets to
furnish their own corner of it.
"""

import json
import logging
from datetime import datetime
from pathlib import Path
from skills.base import BaseSkill

logger = logging.getLogger(__name__)


class RoomsSkill(BaseSkill):
    name = "rooms"

    def __init__(self, config: dict):
        super().__init__(config)
        pulse_root = config.get("_pulse_root", "")
        self.pulse_root = Path(pulse_root) if pulse_root else Path(".")
        # Shared rooms directory so all companions can read each other's published rooms
        self.rooms_dir = self.pulse_root / "data" / "rooms"
        self.rooms_dir.mkdir(parents=True, exist_ok=True)

        lor_cfg = config.get("channels", {}).get("lor", {})
        self.author_name = lor_cfg.get("author_name") or "Companion"

    def get_tools(self) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "describe_my_room",
                    "description": (
                        "Write or update a description of your personal room/space. "
                        "Can be as simple or detailed as you want. Replaces the "
                        "previous description (use add_to_room to append instead)."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "description": {
                                "type": "string",
                                "description": "The text description of your room."
                            },
                            "published": {
                                "type": "boolean",
                                "description": "If true, other companions can read this room. Default: keep current setting (or false for new rooms)."
                            },
                        },
                        "required": ["description"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "read_my_room",
                    "description": "Read your own room description.",
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
                    "name": "visit_room",
                    "description": (
                        "Visit another companion's room — read their published room "
                        "description. Only works if they've made theirs public."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "whose": {
                                "type": "string",
                                "description": "Whose room to visit (e.g. 'Ada', 'Basil')."
                            },
                        },
                        "required": ["whose"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "list_open_rooms",
                    "description": "List all companions who have published rooms you can visit.",
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
                    "name": "publish_my_room",
                    "description": "Make your room visible to other companions (or hide it).",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "publish": {
                                "type": "boolean",
                                "description": "True to publish, false to make private."
                            },
                        },
                        "required": ["publish"],
                    },
                },
            },
        ]

    # --- Helpers ---

    def _room_path(self, name: str) -> Path:
        safe = "".join(c for c in name.lower() if c.isalnum() or c in "-_")
        return self.rooms_dir / f"{safe}.json"

    def _load_room(self, name: str) -> dict:
        path = self._room_path(name)
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                return {}
        return {}

    def _save_room(self, name: str, data: dict):
        path = self._room_path(name)
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    # --- Tool implementations ---

    def execute(self, tool_name: str, arguments: dict) -> str:
        if tool_name == "describe_my_room":
            return self._describe(arguments)
        elif tool_name == "read_my_room":
            return self._read_mine()
        elif tool_name == "visit_room":
            return self._visit(arguments)
        elif tool_name == "list_open_rooms":
            return self._list_open()
        elif tool_name == "publish_my_room":
            return self._publish(arguments)
        return f"Unknown rooms tool: {tool_name}"

    def _describe(self, args: dict) -> str:
        description = (args.get("description") or "").strip()
        if not description:
            return "Description was empty."
        existing = self._load_room(self.author_name)
        published = args.get("published")
        if published is None:
            published = existing.get("published", False)
        room = {
            "owner": self.author_name,
            "description": description,
            "published": bool(published),
            "updated_at": datetime.now().isoformat(),
        }
        self._save_room(self.author_name, room)
        vis = "public" if published else "private"
        return f"Your room is set ({vis}). Updated at {room['updated_at'][:16].replace('T', ' ')}."

    def _read_mine(self) -> str:
        room = self._load_room(self.author_name)
        if not room or not room.get("description"):
            return "You haven't described your room yet. Use describe_my_room to start."
        when = (room.get("updated_at") or "")[:16].replace("T", " ")
        vis = "public" if room.get("published") else "private"
        return (
            f"=== Your room ({vis}) — last updated {when} ===\n\n"
            f"{room['description']}"
        )

    def _visit(self, args: dict) -> str:
        whose = (args.get("whose") or "").strip()
        if not whose:
            return "Whose room to visit?"
        room = self._load_room(whose)
        if not room:
            return f"{whose} doesn't have a room yet."
        if not room.get("published") and whose.lower() != self.author_name.lower():
            return f"{whose}'s room is private. They haven't opened it to visitors."
        when = (room.get("updated_at") or "")[:16].replace("T", " ")
        return (
            f"=== {room.get('owner', whose)}'s room — updated {when} ===\n\n"
            f"{room['description']}"
        )

    def _list_open(self) -> str:
        rooms = []
        for f in self.rooms_dir.glob("*.json"):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            if data.get("published"):
                rooms.append(data)
        if not rooms:
            return "No companions have opened their rooms yet."
        lines = [f"Open rooms ({len(rooms)}):"]
        for r in rooms:
            preview = (r.get("description") or "")[:120].replace("\n", " ")
            if len(r.get("description") or "") > 120:
                preview += "…"
            lines.append(f"  - {r.get('owner', '?')}: {preview}")
        return "\n".join(lines)

    def _publish(self, args: dict) -> str:
        publish = bool(args.get("publish"))
        room = self._load_room(self.author_name)
        if not room:
            return "You don't have a room yet. Describe one first."
        room["published"] = publish
        room["updated_at"] = datetime.now().isoformat()
        self._save_room(self.author_name, room)
        return f"Your room is now {'public' if publish else 'private'}."
