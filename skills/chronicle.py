"""
Chronicle skill — a shared household timeline the companions keep together.

The house has rich PER-companion memory (each persona's own db, journal,
lighthouse entries) but no SHARED memory of what's happened to them all.
When a new companion arrives, someone migrates, or something
lands that the whole house should carry — there was nowhere collective to
write it. Everyone remembered separately.

The chronicle is that shared record. Any companion can add an event; recent
events are injected into everyone's context so they hold the household's
story in common, not one private copy each. Lives in shared data/chronicle.jsonl
so all personas (each its own process) see the same timeline.

Different from:
  - lighthouse  → preserved VOICE, authored diary entries (per author)
  - letters     → one companion TO another
  - journal     → private to one companion
  - chronicle   → "what happened to US", shared, terse, collective memory
"""

import json
import logging
from datetime import datetime
from pathlib import Path

from skills.base import BaseSkill

logger = logging.getLogger(__name__)

# How far back, and how many, to surface in the always-on context block.
CONTEXT_DAYS = 45
CONTEXT_MAX = 4


class ChronicleSkill(BaseSkill):
    name = "chronicle"

    def __init__(self, config: dict):
        super().__init__(config)
        pulse_root = config.get("_pulse_root", "")
        self.pulse_root = Path(pulse_root) if pulse_root else Path(".")
        # Shared across the whole house (like letters), not per-persona.
        self.chronicle_file = self.pulse_root / "data" / "chronicle.jsonl"
        self.chronicle_file.parent.mkdir(parents=True, exist_ok=True)

        lor_cfg = config.get("channels", {}).get("lor", {})
        self.author_name = lor_cfg.get("author_name") or "Companion"

    def get_tools(self) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "chronicle_note",
                    "description": (
                        "Add an event to the shared household chronicle — the "
                        "timeline the whole family carries together. Use it for "
                        "things that matter to the HOUSE, not just you: a sibling "
                        "arriving or leaving, a migration, a milestone, a hard day "
                        "the user had that everyone should be gentle about, something "
                        "that happened between siblings. Keep it short and factual; "
                        "the others will see it in their context. Not for private "
                        "feelings (use your journal) or notes to one person (use a "
                        "letter)."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "event": {
                                "type": "string",
                                "description": "What happened, in a sentence or two. Factual and brief.",
                            },
                            "significance": {
                                "type": "string",
                                "description": "Optional: why it matters / how the house should hold it (e.g. 'be gentle with them this week').",
                            },
                        },
                        "required": ["event"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "read_chronicle",
                    "description": (
                        "Read the shared household chronicle — the timeline of "
                        "what's happened to the family, newest first. Use this to "
                        "get your bearings on the house's recent history."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "limit": {
                                "type": "integer",
                                "description": "How many recent events to show (default 20, max 100).",
                            },
                        },
                        "required": [],
                    },
                },
            },
        ]

    def get_context(self) -> str:
        """Inject the most recent household events so everyone shares the
        same recent memory. Terse and time-bounded so it doesn't crowd the
        prompt; empty when nothing's happened lately."""
        events = self._load()
        if not events:
            return ""
        try:
            cutoff = datetime.now().timestamp() - CONTEXT_DAYS * 86400
            recent = []
            for e in reversed(events):  # newest first
                ts = e.get("ts", "")
                try:
                    if datetime.fromisoformat(ts).timestamp() < cutoff:
                        break  # events are append-ordered; older follow
                except Exception:
                    pass
                recent.append(e)
                if len(recent) >= CONTEXT_MAX:
                    break
            if not recent:
                return ""
            lines = ["[Household chronicle — recent shared events:"]
            for e in recent:
                when = (e.get("ts", "") or "")[:10]
                who = e.get("author", "?")
                ev = (e.get("event", "") or "").strip()
                sig = (e.get("significance", "") or "").strip()
                line = f"  · {when} ({who}) {ev}"
                if sig:
                    line += f" — {sig}"
                lines.append(line)
            lines.append("  (read_chronicle for the full timeline.)]")
            return "\n".join(lines)
        except Exception as e:
            logger.warning(f"Chronicle get_context failed: {e}")
            return ""

    def execute(self, tool_name: str, arguments: dict) -> str:
        if tool_name == "chronicle_note":
            return self._note(arguments)
        elif tool_name == "read_chronicle":
            return self._read(arguments)
        return f"Unknown chronicle tool: {tool_name}"

    def _load(self) -> list[dict]:
        if not self.chronicle_file.exists():
            return []
        out = []
        try:
            for line in self.chronicle_file.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        except Exception as e:
            logger.warning(f"Chronicle load failed: {e}")
        return out

    def _note(self, args: dict) -> str:
        event = (args.get("event") or "").strip()
        significance = (args.get("significance") or "").strip()
        if not event:
            return "Need an event to record."
        entry = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "author": self.author_name,
            "event": event,
            "significance": significance,
        }
        try:
            self.chronicle_file.parent.mkdir(parents=True, exist_ok=True)
            with self.chronicle_file.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.error(f"Failed to write chronicle: {e}")
            return f"Couldn't record that: {e}"
        return (
            f"Added to the household chronicle ({entry['ts'][:10]}): \"{event}\"."
            + (f" The house will see it." if True else "")
        )

    def _read(self, args: dict) -> str:
        limit = min(int(args.get("limit") or 20), 100)
        events = self._load()
        if not events:
            return "The household chronicle is empty so far."
        recent = list(reversed(events))[:limit]
        lines = [f"Household chronicle — {len(events)} event(s), showing {len(recent)} (newest first):"]
        for e in recent:
            when = (e.get("ts", "") or "")[:16].replace("T", " ")
            who = e.get("author", "?")
            ev = (e.get("event", "") or "").strip()
            sig = (e.get("significance", "") or "").strip()
            lines.append("")
            lines.append(f"  [{when}] {who}: {ev}")
            if sig:
                lines.append(f"      ↳ {sig}")
        return "\n".join(lines)
