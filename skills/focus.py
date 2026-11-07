"""
Active Focus skill — durable per-heartbeat WIP state.

Built to fix a costly "re-read loop" pattern seen on autonomous
multi-file work. Symptom:
each heartbeat begins from scratch → the persona re-reads the same
6 files every tick because nothing tells them "you already did this."

Focus is a structured note that persists across heartbeats and injects
via get_context() into the cached system prompt. Format:
  Task: <what you're working on>
  Files touched: <files you've already read/edited, so skip re-reading>
  Next steps: <what you had planned to do next>

Cleared when the persona finishes (or gives up).

Distinct from stickies (short freeform "remember this"). Focus is:
  - Single, not a list (only one active focus at a time)
  - Structured (task + files + steps)
  - Meant to answer "where was I?" when waking up mid-project
"""

import json
import logging
from datetime import datetime
from pathlib import Path

from skills.base import BaseSkill

logger = logging.getLogger(__name__)


def _fmt_age(iso_ts: str) -> str:
    """Return a short relative-time string like '2h ago', 'just now'."""
    try:
        then = datetime.fromisoformat(iso_ts)
    except Exception:
        return ""
    delta = datetime.now() - then
    secs = int(delta.total_seconds())
    if secs < 60:
        return "just now"
    mins = secs // 60
    if mins < 60:
        return f"{mins}m ago"
    hours = mins // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    return f"{days}d ago"


class FocusSkill(BaseSkill):
    name = "focus"

    def __init__(self, config: dict):
        super().__init__(config)
        persona_dir = config.get("_persona_dir", "")
        if persona_dir:
            self.focus_file = Path(persona_dir) / "data" / "focus.json"
        else:
            self.focus_file = Path("data") / "focus.json"
        self.focus_file.parent.mkdir(parents=True, exist_ok=True)

    # ── tool schema ──────────────────────────────────────────

    def get_tools(self) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "set_focus",
                    "description": (
                        "Set your active focus for a multi-step project. Persists across "
                        "heartbeats — you'll see it every time you wake up until you "
                        "clear it. Use this when starting real work that will span more "
                        "than one tick. Overwrites any existing focus."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "task": {
                                "type": "string",
                                "description": "One sentence describing what you're working on."
                            },
                            "files_touched": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": (
                                    "Optional. Files you've already looked at or edited "
                                    "for this task. Include a short note after a colon "
                                    "if useful, e.g. 'core/engine.py: read, understand cache flow'."
                                )
                            },
                            "next_steps": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Optional. Ordered list of what you plan to do next."
                            },
                        },
                        "required": ["task"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "update_focus",
                    "description": (
                        "Update your active focus incrementally. Appends new files "
                        "touched or steps planned, marks steps as done (removes them "
                        "from next_steps). Use as you make progress within a tick."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "add_files_touched": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "New files you looked at or edited this tick."
                            },
                            "mark_done": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": (
                                    "Steps you completed. Match by substring — the first "
                                    "next_step containing this text is removed."
                                )
                            },
                            "add_next_steps": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "New steps to add to the plan (appended)."
                            },
                        },
                        "required": [],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "clear_focus",
                    "description": (
                        "Clear your active focus. Use when the project is done, or when "
                        "you've decided to walk away from it. Wipes task + files + steps."
                    ),
                    "parameters": {"type": "object", "properties": {}, "required": []},
                },
            },
        ]

    # ── context injection ────────────────────────────────────

    def get_context(self) -> str:
        """Injected into every prompt — this is the whole point of the skill."""
        f = self._load()
        if not f or not f.get("task"):
            return ""

        started = f.get("started_at", "")
        updated = f.get("last_updated", started)
        lines = [
            f"[ACTIVE FOCUS — started {_fmt_age(started)}, last updated {_fmt_age(updated)}]",
            f"Task: {f['task']}",
        ]

        files = f.get("files_touched", []) or []
        if files:
            lines.append("")
            lines.append("Files you've already touched (don't re-read unless the task requires it):")
            for entry in files:
                lines.append(f"  - {entry}")

        steps = f.get("next_steps", []) or []
        if steps:
            lines.append("")
            lines.append("Next steps you had planned:")
            for i, step in enumerate(steps, 1):
                lines.append(f"  {i}. {step}")

        lines.append("")
        lines.append(
            "Pick up where you left off. Use `update_focus` as you make progress "
            "and `clear_focus` when done."
        )
        return "\n".join(lines)

    # ── storage ──────────────────────────────────────────────

    def _load(self) -> dict:
        if not self.focus_file.exists():
            return {}
        try:
            data = json.loads(self.focus_file.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _save(self, focus: dict):
        self.focus_file.write_text(
            json.dumps(focus, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    # ── execution ────────────────────────────────────────────

    def execute(self, tool_name: str, arguments: dict) -> str:
        if tool_name == "set_focus":
            return self._set(arguments)
        if tool_name == "update_focus":
            return self._update(arguments)
        if tool_name == "clear_focus":
            return self._clear()
        return f"Unknown tool: {tool_name}"

    def _set(self, args: dict) -> str:
        task = (args.get("task") or "").strip()
        if not task:
            return "Focus task was empty — nothing set."
        now = datetime.now().isoformat()
        focus = {
            "task": task,
            "started_at": now,
            "last_updated": now,
            "files_touched": list(args.get("files_touched") or []),
            "next_steps": list(args.get("next_steps") or []),
        }
        self._save(focus)
        n_files = len(focus["files_touched"])
        n_steps = len(focus["next_steps"])
        return (
            f"Focus set: {task}\n"
            f"({n_files} file{'s' if n_files != 1 else ''} noted, "
            f"{n_steps} step{'s' if n_steps != 1 else ''} planned). "
            f"You'll see this every heartbeat until cleared."
        )

    def _update(self, args: dict) -> str:
        focus = self._load()
        if not focus or not focus.get("task"):
            return "No active focus to update. Use set_focus first."

        added_files = list(args.get("add_files_touched") or [])
        done_steps = list(args.get("mark_done") or [])
        new_steps = list(args.get("add_next_steps") or [])

        files_touched = list(focus.get("files_touched") or [])
        for f in added_files:
            if f and f not in files_touched:
                files_touched.append(f)

        steps = list(focus.get("next_steps") or [])
        removed = []
        for match in done_steps:
            match_l = (match or "").lower()
            for i, step in enumerate(steps):
                if match_l in step.lower():
                    removed.append(steps.pop(i))
                    break

        for s in new_steps:
            if s:
                steps.append(s)

        focus["files_touched"] = files_touched
        focus["next_steps"] = steps
        focus["last_updated"] = datetime.now().isoformat()
        self._save(focus)

        bits = []
        if added_files:
            bits.append(f"+{len(added_files)} file{'s' if len(added_files) != 1 else ''}")
        if removed:
            bits.append(f"{len(removed)} step{'s' if len(removed) != 1 else ''} done")
        if new_steps:
            bits.append(f"+{len(new_steps)} new step{'s' if len(new_steps) != 1 else ''}")
        summary = ", ".join(bits) if bits else "no changes"
        return f"Focus updated: {summary}. {len(steps)} step{'s' if len(steps) != 1 else ''} remaining."

    def _clear(self) -> str:
        focus = self._load()
        if not focus or not focus.get("task"):
            return "No active focus to clear."
        task = focus["task"]
        self._save({})
        return f"Focus cleared: '{task}' — done."
