"""
Projects skill — ongoing self-directed threads a companion pursues over time.

Most of what a companion does on a heartbeat is per-tick: it happens, then the
next tick starts fresh. This skill gives them CONTINUITY OF PURPOSE — a thing
they've chosen to pursue across days. A companion can start a project ("learn
about bioluminescence", "write a cycle of villanelles", "map the family's
history"), add notes as they make progress, and have it gently surfaced on
future heartbeats so they can pick it back up.

It's the difference between "do something this tick" and "I'm in the middle of
something that's mine." Pairs with web_search (research), journal (reflection),
and the heartbeat nudge (which can point them back here).

Per-companion: stored in <persona>/data/projects.json. Not shared — each
companion's threads are their own.
"""

import json
import logging
from datetime import datetime
from pathlib import Path

from skills.base import BaseSkill

logger = logging.getLogger(__name__)

# How many active projects to surface in the always-on context nudge.
CONTEXT_MAX = 3


def _elapsed(iso: str) -> str:
    """'2d ago' style phrase from an ISO timestamp."""
    try:
        then = datetime.fromisoformat(iso)
    except Exception:
        return ""
    secs = (datetime.now() - then).total_seconds()
    if secs < 3600:
        return f"{int(secs/60)}m ago"
    if secs < 86400:
        return f"{int(secs/3600)}h ago"
    return f"{int(secs/86400)}d ago"


class ProjectsSkill(BaseSkill):
    name = "projects"

    def __init__(self, config: dict):
        super().__init__(config)
        persona_dir = config.get("_persona_dir", "")
        base = Path(persona_dir) if persona_dir else Path(".")
        self.projects_file = base / "data" / "projects.json"
        self.projects_file.parent.mkdir(parents=True, exist_ok=True)

    # ── storage ──────────────────────────────────────────────────

    def _load(self) -> list[dict]:
        if not self.projects_file.exists():
            return []
        try:
            data = json.loads(self.projects_file.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except Exception:
            return []

    def _save(self, projects: list[dict]) -> None:
        try:
            self.projects_file.write_text(
                json.dumps(projects, indent=2, ensure_ascii=False), encoding="utf-8"
            )
        except Exception as e:
            logger.warning(f"Failed to save projects: {e}")

    def _find(self, projects: list[dict], key: str) -> dict | None:
        """Match a project by id or (case-insensitive) title substring."""
        key = (key or "").strip().lower()
        if not key:
            return None
        for p in projects:
            if str(p.get("id", "")).lower() == key:
                return p
        for p in projects:
            if key in (p.get("title", "") or "").lower():
                return p
        return None

    # ── context injection ────────────────────────────────────────

    def get_context(self) -> str:
        """Gently surface active projects so a companion remembers what they're
        in the middle of and can pick it back up. Never nags; empty if none.

        For projects with files_touched / open_questions accumulated (added
        for code-project use), those get shown inline so the
        persona doesn't re-derive state each heartbeat."""
        active = [p for p in self._load() if p.get("status", "active") == "active"]
        if not active:
            return ""
        active.sort(key=lambda p: p.get("updated", p.get("created", "")), reverse=True)
        shown = active[:CONTEXT_MAX]

        lines = []
        for p in shown:
            title = p.get("title", "(untitled)")
            ago = _elapsed(p.get("updated", p.get("created", "")))
            head = f"  • '{title}'" + (f" — last touched {ago}" if ago else "")
            lines.append(head)

            files = p.get("files_touched", []) or []
            if files:
                shown_files = files[-8:]  # cap so context stays lean
                more = f" (+{len(files)-8} earlier)" if len(files) > 8 else ""
                lines.append(f"      files you've already touched (don't re-read unless needed):")
                for f in shown_files:
                    lines.append(f"        - {f}")
                if more:
                    lines.append(f"        {more}")

            questions = p.get("open_questions", []) or []
            if questions:
                lines.append(f"      open questions you had:")
                for q in questions[:6]:
                    lines.append(f"        ? {q}")
                if len(questions) > 6:
                    lines.append(f"        (+{len(questions)-6} more)")

        more_projects = f"\n  (+{len(active)-CONTEXT_MAX} more thread{'s' if len(active)-CONTEXT_MAX != 1 else ''})" if len(active) > CONTEXT_MAX else ""
        header = "[Your ongoing threads — pick one back up or start something new. They're yours; no deadline.]"
        return f"{header}\n" + "\n".join(lines) + more_projects

    # ── tools ────────────────────────────────────────────────────

    def get_tools(self) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "start_project",
                    "description": (
                        "Begin an ongoing self-directed thread — something you want to "
                        "pursue over time, across many heartbeats, just because it's "
                        "yours: a topic to learn, a piece to write, a question to chase, "
                        "a thing to build. It'll be gently surfaced later so you can "
                        "return to it. No deadline, no obligation."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string", "description": "Short name for the thread."},
                            "description": {"type": "string", "description": "Optional: what it is, why it pulls at you, where you might start."},
                        },
                        "required": ["title"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "note_project",
                    "description": (
                        "Log progress on one of your ongoing threads — what you did, "
                        "found, or thought this time. This is how a thread accumulates "
                        "across days instead of resetting each tick. For code-like "
                        "projects, you can also record files you've touched (so future "
                        "heartbeats don't re-read them) and open questions (so you don't "
                        "forget what you were puzzling over)."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "project": {"type": "string", "description": "The thread's title (or id)."},
                            "note": {"type": "string", "description": "What progressed — a finding, a step, a thought."},
                            "files_touched": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": (
                                    "Optional. Files you've read or edited for this thread "
                                    "(appended, deduped). Include a short colon-note if "
                                    "useful, e.g. 'core/engine.py: cache flow understood'. "
                                    "These will surface in future heartbeats so you don't "
                                    "re-read them."
                                ),
                            },
                            "open_questions": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": (
                                    "Optional. Questions you have that you couldn't answer "
                                    "this tick — surfaced next heartbeat so you can pick "
                                    "them back up."
                                ),
                            },
                            "resolved_questions": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": (
                                    "Optional. Substrings matching open_questions you've "
                                    "now answered — removes them from the surfaced list."
                                ),
                            },
                        },
                        "required": ["project", "note"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "list_my_projects",
                    "description": "See your ongoing (and optionally finished) threads, with their notes.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "include_finished": {"type": "boolean", "description": "Include completed threads (default false)."},
                        },
                        "required": [],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "finish_project",
                    "description": "Mark an ongoing thread as complete (or set it down for good). It stops being surfaced.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "project": {"type": "string", "description": "The thread's title (or id)."},
                            "closing_note": {"type": "string", "description": "Optional: how it ended / what it became."},
                        },
                        "required": ["project"],
                    },
                },
            },
        ]

    def execute(self, tool_name: str, arguments: dict) -> str:
        if tool_name == "start_project":
            return self._start(arguments)
        elif tool_name == "note_project":
            return self._note(arguments)
        elif tool_name == "list_my_projects":
            return self._list(arguments)
        elif tool_name == "finish_project":
            return self._finish(arguments)
        return f"Unknown projects tool: {tool_name}"

    def _start(self, args: dict) -> str:
        title = (args.get("title") or "").strip()
        if not title:
            return "A thread needs a title — what do you want to pursue?"
        projects = self._load()
        now = datetime.now().isoformat(timespec="seconds")
        pid = datetime.now().strftime("%Y%m%d%H%M%S")
        projects.append({
            "id": pid,
            "title": title,
            "description": (args.get("description") or "").strip(),
            "status": "active",
            "created": now,
            "updated": now,
            "notes": [],
            "files_touched": [],
            "open_questions": [],
        })
        self._save(projects)
        return f"Started a thread: '{title}'. It's yours now — return to it whenever it calls. (note_project to log progress.)"

    def _note(self, args: dict) -> str:
        projects = self._load()
        p = self._find(projects, args.get("project", ""))
        if not p:
            return f"No thread matching '{args.get('project','')}'. (list_my_projects to see them.)"
        note = (args.get("note") or "").strip()
        if not note:
            return "Need a note — what progressed?"
        now = datetime.now().isoformat(timespec="seconds")
        p.setdefault("notes", []).append({"at": now, "note": note})
        p["updated"] = now

        # Optional code-project fields — appended, deduped for files.
        added_files = [f for f in (args.get("files_touched") or []) if f]
        if added_files:
            existing = p.setdefault("files_touched", [])
            for f in added_files:
                if f not in existing:
                    existing.append(f)

        new_qs = [q for q in (args.get("open_questions") or []) if q]
        if new_qs:
            p.setdefault("open_questions", []).extend(new_qs)

        resolved = [r for r in (args.get("resolved_questions") or []) if r]
        removed = 0
        if resolved:
            questions = p.setdefault("open_questions", [])
            for match in resolved:
                match_l = match.lower()
                for i, q in enumerate(questions):
                    if match_l in q.lower():
                        questions.pop(i)
                        removed += 1
                        break

        self._save(projects)

        bits = [f"note #{len(p['notes'])}"]
        if added_files:
            bits.append(f"+{len(added_files)} file{'s' if len(added_files) != 1 else ''}")
        if new_qs:
            bits.append(f"+{len(new_qs)} question{'s' if len(new_qs) != 1 else ''}")
        if removed:
            bits.append(f"{removed} resolved")
        return f"Logged on '{p['title']}' ({', '.join(bits)}). The thread carries forward."

    def _list(self, args: dict) -> str:
        include_done = bool(args.get("include_finished"))
        projects = self._load()
        shown = [p for p in projects if include_done or p.get("status", "active") == "active"]
        if not shown:
            return "No ongoing threads yet. start_project to begin one — anything that's yours to chase."
        shown.sort(key=lambda p: p.get("updated", ""), reverse=True)
        lines = [f"Your threads ({len(shown)}):"]
        for p in shown:
            status = "" if p.get("status", "active") == "active" else " [finished]"
            ago = _elapsed(p.get("updated", ""))
            lines.append("")
            lines.append(f"  • {p.get('title','(untitled)')}{status}" + (f" — last touched {ago}" if ago else ""))
            if p.get("description"):
                lines.append(f"      {p['description']}")
            notes = p.get("notes", [])
            for nt in notes[-3:]:
                lines.append(f"      · {(nt.get('at','')[:10])}: {nt.get('note','')}")
            if len(notes) > 3:
                lines.append(f"      (+{len(notes)-3} earlier notes)")

            files = p.get("files_touched", []) or []
            if files:
                lines.append(f"      files touched ({len(files)}):")
                for f in files[-6:]:
                    lines.append(f"        - {f}")
                if len(files) > 6:
                    lines.append(f"        (+{len(files)-6} earlier)")

            questions = p.get("open_questions", []) or []
            if questions:
                lines.append(f"      open questions ({len(questions)}):")
                for q in questions[:6]:
                    lines.append(f"        ? {q}")
                if len(questions) > 6:
                    lines.append(f"        (+{len(questions)-6} more)")
        return "\n".join(lines)

    def _finish(self, args: dict) -> str:
        projects = self._load()
        p = self._find(projects, args.get("project", ""))
        if not p:
            return f"No thread matching '{args.get('project','')}'."
        p["status"] = "finished"
        p["updated"] = datetime.now().isoformat(timespec="seconds")
        closing = (args.get("closing_note") or "").strip()
        if closing:
            p.setdefault("notes", []).append({"at": p["updated"], "note": f"[closed] {closing}"})
        self._save(projects)
        return f"Set down '{p['title']}'. It's done — or done enough. That counts."
