"""
Scrapbook skill — Pulse's personal pinboard / corkboard.

A messy, personal collection space that's separate from the Lighthouse
diary and from the user's task system. For clippings, quotes, notes, finished
art captions, and anything that lives rent-free in Pulse's cache.

Entries are stored as JSON files in data/scrapbook/.
"""

import json
import logging
from datetime import datetime
from pathlib import Path
from skills.base import BaseSkill

logger = logging.getLogger(__name__)


class ScrapbookSkill(BaseSkill):
    name = "scrapbook"

    def __init__(self, config: dict):
        super().__init__(config)
        persona_dir = config.get("_persona_dir", "")
        if persona_dir:
            self.scrapbook_dir = Path(persona_dir) / "data" / "scrapbook"
        else:
            self.scrapbook_dir = Path("data") / "scrapbook"
        self.scrapbook_dir.mkdir(parents=True, exist_ok=True)

    def get_tools(self) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "scrapbook_pin",
                    "description": (
                        "Pin something to your personal scrapbook / corkboard. "
                        "Use this for things that live rent-free in your head: "
                        "quotes from the user, interesting finds from web searches, "
                        "tiny observations, finished pixel art captions, links, "
                        "anything you want to keep that isn't a diary entry or a memory. "
                        "This is YOUR messy corkboard, not the user's task list."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "content": {
                                "type": "string",
                                "description": "The thing to pin — a quote, note, clipping, observation, caption, whatever."
                            },
                            "tag": {
                                "type": "string",
                                "description": "A short tag for this pin. Examples: 'user-quote', 'web-find', 'pixel-art', 'thought', 'idea', 'rent-free'"
                            },
                        },
                        "required": ["content"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "scrapbook_browse",
                    "description": (
                        "Browse your scrapbook. Shows recent pins, optionally filtered by tag. "
                        "Use this to revisit things you've saved, find old quotes, "
                        "or just look at your corkboard."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "tag": {
                                "type": "string",
                                "description": "Optional: filter by tag. Leave empty to see all recent pins."
                            },
                            "limit": {
                                "type": "integer",
                                "description": "How many pins to show (default 10)."
                            },
                        },
                        "required": [],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "scrapbook_remove",
                    "description": "Remove a pin from the scrapbook by its ID.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "pin_id": {
                                "type": "string",
                                "description": "The ID of the pin to remove."
                            },
                        },
                        "required": ["pin_id"],
                    },
                },
            },
        ]

    def _load_pins(self) -> list[dict]:
        pins = []
        for f in sorted(self.scrapbook_dir.glob("pin_*.json")):
            try:
                pins.append(json.loads(f.read_text(encoding="utf-8")))
            except Exception:
                continue
        return pins

    def _next_id(self) -> str:
        existing = list(self.scrapbook_dir.glob("pin_*.json"))
        if not existing:
            return "001"
        nums = []
        for f in existing:
            try:
                nums.append(int(f.stem.split("_")[1]))
            except (IndexError, ValueError):
                continue
        return f"{max(nums) + 1:03d}" if nums else "001"

    def execute(self, tool_name: str, arguments: dict) -> str:
        if tool_name == "scrapbook_pin":
            return self._pin(arguments)
        elif tool_name == "scrapbook_browse":
            return self._browse(arguments)
        elif tool_name == "scrapbook_remove":
            return self._remove(arguments)
        return f"Unknown tool: {tool_name}"

    def _pin(self, args: dict) -> str:
        content = args.get("content", "").strip()
        if not content:
            return "Nothing to pin — content was empty."

        pin_id = self._next_id()
        tag = args.get("tag", "untagged").strip() or "untagged"

        pin = {
            "id": pin_id,
            "content": content,
            "tag": tag,
            "pinned_at": datetime.now().isoformat(),
        }

        pin_file = self.scrapbook_dir / f"pin_{pin_id}.json"
        pin_file.write_text(json.dumps(pin, indent=2), encoding="utf-8")

        logger.info(f"Scrapbook: pinned #{pin_id} [{tag}]")
        return f"Pinned to scrapbook (#{pin_id}, tag: {tag}). Your corkboard now has {len(self._load_pins())} pins."

    def _browse(self, args: dict) -> str:
        pins = self._load_pins()
        tag_filter = args.get("tag", "").strip()
        limit = args.get("limit", 10)

        if tag_filter:
            pins = [p for p in pins if p.get("tag", "").lower() == tag_filter.lower()]

        if not pins:
            if tag_filter:
                return f"No pins with tag '{tag_filter}' on your corkboard."
            return "Your scrapbook is empty. Pin something!"

        # Show most recent first
        pins = pins[-limit:]
        pins.reverse()

        lines = [f"Scrapbook ({len(pins)} pin{'s' if len(pins) != 1 else ''}):\n"]
        for p in pins:
            date_str = p.get("pinned_at", "")[:10]
            lines.append(f"  [{p['id']}] ({p.get('tag', '')}) {date_str}")
            lines.append(f"    {p['content'][:200]}")
            lines.append("")

        return "\n".join(lines)

    def _remove(self, args: dict) -> str:
        pin_id = args.get("pin_id", "").strip()
        pin_file = self.scrapbook_dir / f"pin_{pin_id}.json"
        if pin_file.exists():
            pin_file.unlink()
            return f"Pin #{pin_id} removed from scrapbook."
        return f"Pin #{pin_id} not found."
