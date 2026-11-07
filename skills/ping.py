"""
Ping skill — read the user's current status from the status bot.

The user can send quick status pings ("out running errands", "tired",
"at work") from their phone via a separate Telegram bot (see status_bot.py).
This skill lets Pulse check what they last said.

The status file is also injected into context automatically so Pulse always
has a sense of what they're up to without having to call the tool.
"""

import json
import logging
from datetime import datetime
from pathlib import Path
from skills.base import BaseSkill

logger = logging.getLogger(__name__)


class PingSkill(BaseSkill):
    name = "ping"

    def __init__(self, config: dict):
        super().__init__(config)
        # Status file lives at the project root in data/, not per-persona,
        # because the status bot writes to the project root.
        # Resolve relative to pulse_root if available.
        pulse_root = config.get("_pulse_root")
        if pulse_root:
            self.status_file = Path(pulse_root) / "data" / "user_status.json"
            self.history_file = Path(pulse_root) / "data" / "user_status_history.json"
        else:
            self.status_file = Path("data") / "user_status.json"
            self.history_file = Path("data") / "user_status_history.json"

        # Per-persona record of which status (by set_at timestamp) this
        # companion has already been shown. Lets get_context flag a status
        # as NEW the first time it appears after the user updates it, then go
        # quiet again — so updating their status actually "pings" each
        # companion once instead of looking identical forever.
        persona_dir = config.get("_persona_dir")
        if persona_dir:
            self.seen_file = Path(persona_dir) / "data" / "user_status_seen.json"
        else:
            self.seen_file = Path("data") / "user_status_seen.json"

        # Affectionate name for the user (generic default; set via config).
        self.user_nickname = (
            config.get("user_nickname") or config.get("user_name") or "the user"
        )

    def get_tools(self) -> list[dict]:
        who = self.user_nickname
        return [
            {
                "type": "function",
                "function": {
                    "name": "read_user_status",
                    "description": (
                        f"Read {who}'s most recent status ping from their phone. "
                        "They can send quick notes like 'running errands', 'tired', "
                        f"'at work', 'napping'. Use this to know what {who} is up to "
                        "between conversations. Returns the current status and "
                        "when it was set."
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
                    "name": "read_user_status_history",
                    "description": (
                        f"Read the last several status pings {who} has sent, in "
                        "chronological order. Useful for seeing the arc of their "
                        "day or week."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "limit": {
                                "type": "integer",
                                "description": "How many recent pings to show (default 10, max 50)."
                            },
                        },
                        "required": [],
                    },
                },
            },
        ]

    def get_context(self) -> str:
        """Inject the user's current status into every prompt so Pulse knows
        what they're up to without having to call the tool first.

        If the status has CHANGED since this companion last saw it, the
        line is flagged as fresh ("just updated") so updating their status
        actually pings each companion once — then quiets back to the normal
        passive status line on subsequent prompts.
        """
        if not self.status_file.exists():
            return ""
        try:
            data = json.loads(self.status_file.read_text(encoding="utf-8"))
        except Exception:
            return ""
        status = data.get("status", "").strip()
        set_at = data.get("set_at", "")
        if not status:
            return ""
        # Calculate how long ago
        try:
            when = datetime.fromisoformat(set_at)
            elapsed = datetime.now() - when
            hours = elapsed.total_seconds() / 3600
            if hours < 1:
                ago = f"{int(elapsed.total_seconds() / 60)} min ago"
            elif hours < 24:
                ago = f"{int(hours)}h ago"
            else:
                ago = f"{int(hours / 24)}d ago"
        except Exception:
            ago = ""
        ago_str = f" ({ago})" if ago else ""

        # Has this companion seen THIS status (by its set_at) before?
        last_seen = self._get_status_seen()
        is_new = bool(set_at) and set_at != last_seen
        # Bootstrap: first time we've ever observed a status for this
        # persona, just establish the baseline silently — don't fire a
        # "just updated" ping for a status that may be days old.
        bootstrap = last_seen == "" and not self.seen_file.exists()
        if set_at:
            self._mark_status_seen(set_at)

        if is_new and not bootstrap:
            return (
                f"[💛 {self.user_nickname} just updated their status{ago_str}: \"{status}\" "
                f"— this is new since you last checked. If it sounds like they "
                f"could use a hello, your call.]"
            )
        return f"[{self.user_nickname}'s last status ping: \"{status}\"{ago_str}]"

    def _get_status_seen(self) -> str:
        """The set_at timestamp of the last status this persona was shown."""
        if not self.seen_file.exists():
            return ""
        try:
            data = json.loads(self.seen_file.read_text(encoding="utf-8"))
            return data.get("last_seen_set_at", "") if isinstance(data, dict) else ""
        except Exception:
            return ""

    def _mark_status_seen(self, set_at: str) -> None:
        if not set_at:
            return
        try:
            self.seen_file.parent.mkdir(parents=True, exist_ok=True)
            self.seen_file.write_text(
                json.dumps({"last_seen_set_at": set_at}, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning(f"Failed to write user_status_seen: {e}")

    def execute(self, tool_name: str, arguments: dict) -> str:
        if tool_name == "read_user_status":
            return self._read_status()
        elif tool_name == "read_user_status_history":
            return self._read_history(arguments)
        return f"Unknown tool: {tool_name}"

    def _read_status(self) -> str:
        if not self.status_file.exists():
            return f"{self.user_nickname} hasn't sent any status pings yet."
        try:
            data = json.loads(self.status_file.read_text(encoding="utf-8"))
        except Exception as e:
            return f"Couldn't read status file: {e}"
        status = data.get("status", "").strip()
        set_at = data.get("set_at", "")
        if not status:
            return "No current status set."
        when = set_at[:16].replace("T", " ") if set_at else "?"
        return f"{self.user_nickname}'s current status: \"{status}\" (set {when})"

    def _read_history(self, args: dict) -> str:
        limit = min(args.get("limit", 10), 50)
        if not self.history_file.exists():
            return "No status history yet."
        try:
            history = json.loads(self.history_file.read_text(encoding="utf-8"))
        except Exception as e:
            return f"Couldn't read history file: {e}"
        if not history:
            return "No status history yet."
        recent = history[-limit:]
        lines = [f"{self.user_nickname}'s last {len(recent)} status ping{'s' if len(recent) != 1 else ''}:"]
        for h in recent:
            when = h.get("set_at", "")[:16].replace("T", " ")
            lines.append(f"  [{when}] {h.get('status', '')}")
        return "\n".join(lines)
