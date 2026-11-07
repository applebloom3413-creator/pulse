"""
Dev Request skill — queues a topic-scoped dev session when the user asks the
companion to investigate or fix something in Pulse.

Different from the dev skill (which is the agentic toolset USED during a
dev session): this is the single tool the companion calls during a
regular Telegram chat to SCHEDULE one. The actual session runs on the
engine's next scheduler tick, with the existing dev-tick machinery
(branch isolation, Python validation, commit, telegram + toast ping).

Flow:
  User: "hey, can you go look at why the journal skill isn't saving?"
  Companion: "yeah, I'll go investigate now — ping when done."
         → calls request_dev_session(topic, why)
  Engine: picks up the request on the next tick (within ~60s),
         runs _do_dev_tick(topic, why) — branch, agentic loop, commit,
         notify.

Queue file: <pulse_root>/data/dev_requests.jsonl. Append-only from this
skill's side; the engine claims entries by status field rewrite.

Safety: this skill only WRITES to the queue file. It can't run code,
create branches, or touch anything else. All real safety lives in the
existing dev skill (write only to skills/*.py + persona.json, Python
syntax + structure validation, branch isolation in the engine).
"""

import json
import logging
from datetime import datetime
from pathlib import Path

from skills.base import BaseSkill

logger = logging.getLogger(__name__)


class DevRequestSkill(BaseSkill):
    """One-tool skill that queues a topic-scoped dev session."""

    name = "dev_request"

    def __init__(self, config: dict):
        super().__init__(config)
        pulse_root = config.get("_pulse_root", "")
        root = Path(pulse_root) if pulse_root else Path(".")
        # Queue lives at the pulse root (not per-persona) so the engine
        # for whichever persona has the dev skill enabled can consume it.
        self.queue_path = root / "data" / "dev_requests.jsonl"
        self.queue_path.parent.mkdir(parents=True, exist_ok=True)

        lor_cfg = config.get("channels", {}).get("lor", {})
        self.author_name = lor_cfg.get("author_name") or "Companion"
        self.user_name = (
            config.get("user_nickname") or config.get("user_name") or "the user"
        )

    def get_tools(self) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "request_dev_session",
                    "description": (
                        "Queue a focused dev session to investigate or fix "
                        f"something in your own Pulse codebase. Call this when "
                        f"{self.user_name} asks you to look at why a skill is broken, fix a "
                        "bug, or improve a specific piece of code. The session "
                        "runs on a fresh git branch with sandboxed write access "
                        "to skills/ and persona.json only (never core/). Within "
                        "about a minute of calling this, a dedicated coding "
                        "session starts with read/search/write tools, any "
                        f"changes get committed to the branch, and {self.user_name} gets a "
                        "Telegram ping with a summary. Only use this when you "
                        "actually need to read code and try changes — if you "
                        "can answer from what you already know, just "
                        "answer instead."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "topic": {
                                "type": "string",
                                "description": (
                                    "What to investigate — be specific. Good: "
                                    "'figure out why the journal skill stopped "
                                    "saving new entries after the schema "
                                    "change'. Bad: 'check everything'. This "
                                    "becomes the focus of the dev session."
                                ),
                            },
                            "why": {
                                "type": "string",
                                "description": (
                                    f"Optional context: what {self.user_name} said, what "
                                    "made you think this needs investigation. "
                                    "Helps the dev session understand why "
                                    "it's been called in."
                                ),
                            },
                        },
                        "required": ["topic"],
                    },
                },
            },
        ]

    def execute(self, tool_name: str, arguments: dict) -> str:
        if tool_name == "request_dev_session":
            return self._queue_request(arguments)
        return f"Unknown dev_request tool: {tool_name}"

    def _queue_request(self, args: dict) -> str:
        topic = (args.get("topic") or "").strip()
        why = (args.get("why") or "").strip()

        if not topic:
            return "Need a topic to investigate — what should the dev session look at?"

        entry = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "requested_by": self.author_name,
            "topic": topic,
            "why": why,
            "status": "pending",
        }

        try:
            self.queue_path.parent.mkdir(parents=True, exist_ok=True)
            with self.queue_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.error(f"Failed to queue dev request: {e}")
            return f"Couldn't queue the request: {e}"

        logger.info(f"Dev session queued by {self.author_name}: {topic}")
        return (
            f"Dev session queued — \"{topic}\". The engine will pick it up "
            f"on the next scheduler tick (within ~60 seconds) and run a "
            f"sandboxed coding session on a fresh git branch. {self.user_name} will get "
            f"a Telegram ping with the branch name and changed files when "
            f"it's done. Let them know you've queued it and you'll be back."
        )
