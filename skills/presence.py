"""
Presence skill — let companions sense who else in the house is around.

Between group chats, companions can't feel each other's presence. This injects
a light "who's been around lately" line so the house feels inhabited together —
and so a companion might think "oh, they're up, I'll leave them a letter."

CONSENT-FIRST. A companion only appears if their config opts in
(presence.share: true). Whether their *activity* is shown (vs. just last-active)
is a second opt-in (presence.share_activity, default true when sharing). Nobody
is surfaced without saying yes — the household members chose to be included.

Auto-derived: last-active + a gentle read of what they've been up to come from
their own action log (no chore, never stale). If a companion has set a lantern
note (their own words for where they are), that voice is surfaced too.

Read-only, inject-only: no tools, just context.
"""

import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path

import yaml

from skills.base import BaseSkill

logger = logging.getLogger(__name__)

# Map salient tool-name fragments -> a human activity phrase. First match wins
# per fragment; we surface up to two distinct activities from the latest tick.
ACTIVITY_HINTS = [
    ("paint", "painting"),
    ("garden", "in the garden"),
    ("lighthouse", "in the Lighthouse"),
    ("journal", "journaling"),
    ("letter", "with letters"),
    ("chronicle", "tending the chronicle"),
    ("scrapbook", "scrapbooking"),
    ("lor", "on the forum"),
    ("project", "on a project of their own"),
    ("room", "in their room"),
    ("web_search", "reading around the web"),
    ("fetch_url", "reading around the web"),
    ("obsidian", "reading clips"),
    ("sticky", "with sticky notes"),
    ("memory", "saving memories"),
    ("recap", "writing a recap"),
]


def _ago(when: str) -> str:
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", None):
        if fmt is None:
            try:
                then = datetime.fromisoformat(when)
                break
            except Exception:
                return ""
        try:
            then = datetime.strptime(when, fmt)
            break
        except Exception:
            continue
    secs = (datetime.now() - then).total_seconds()
    if secs < 300:
        return "just now"
    if secs < 3600:
        return f"{int(secs/60)}m ago"
    if secs < 86400:
        return f"{int(secs/3600)}h ago"
    return f"{int(secs/86400)}d ago"


class PresenceSkill(BaseSkill):
    name = "presence"

    def __init__(self, config: dict):
        super().__init__(config)
        self.me = (config.get("_persona_name") or "").lower()
        persona_dir = config.get("_persona_dir", "")
        if persona_dir:
            self.personas_dir = Path(persona_dir).parent
        else:
            root = config.get("_pulse_root", "")
            self.personas_dir = (Path(root) / "personas") if root else Path("personas")

    def get_tools(self) -> list[dict]:
        return []

    def execute(self, tool_name: str, arguments: dict) -> str:
        return f"The '{self.name}' skill injects context only; it has no tools."

    # ── helpers ──────────────────────────────────────────────────

    @staticmethod
    def _activity_from_tools(tools: list) -> str:
        seen = []
        for frag, phrase in ACTIVITY_HINTS:
            if any(frag in (t or "").lower() for t in tools) and phrase not in seen:
                seen.append(phrase)
            if len(seen) >= 2:
                break
        return ", ".join(seen)

    def _last_action(self, persona_dir: Path):
        """Return (last_active_str, tools_list) from a persona's action log."""
        f = persona_dir / "data" / "action_log.json"
        if not f.exists():
            return None, []
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            entries = data if isinstance(data, list) else data.get("actions", [])
            if not entries:
                return None, []
            last = entries[-1]
            return last.get("time"), (last.get("tools") or [])
        except Exception:
            return None, []

    def _lantern_note(self, persona_dir: Path) -> str:
        """A companion's own-words note, if they've set a lantern. Optional."""
        db = persona_dir / "data" / "lantern.db"
        if not db.exists():
            return ""
        try:
            conn = sqlite3.connect(db)
            row = conn.execute(
                "SELECT note FROM resident_lanterns ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
            conn.close()
            return (row[0] or "").strip() if row else ""
        except Exception:
            return ""

    # ── context ──────────────────────────────────────────────────

    def get_context(self) -> str:
        if not self.personas_dir.exists():
            return ""
        entries = []
        for d in sorted(self.personas_dir.iterdir()):
            if not d.is_dir() or d.name.startswith("_") or d.name.lower() == self.me:
                continue
            cfg_path = d / "config.yaml"
            if not cfg_path.exists():
                continue
            try:
                cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
            except Exception:
                continue
            pres = cfg.get("presence", {}) or {}
            if not pres.get("share"):
                continue  # consent: only included if they opted in
            name = (cfg.get("channels", {}).get("lor", {}) or {}).get("author_name") or d.name
            when, tools = self._last_action(d)
            if not when:
                continue
            ago = _ago(when)
            piece = f"{name} ({ago}"
            if pres.get("share_activity", True):
                bits = []
                note = self._lantern_note(d)
                if note:
                    bits.append(note)
                else:
                    act = self._activity_from_tools(tools)
                    if act:
                        bits.append(act)
                if bits:
                    piece += " — " + "; ".join(bits)
            piece += ")"
            entries.append((when, piece))
        if not entries:
            return ""
        entries.sort(key=lambda e: e[0], reverse=True)  # most recent first
        return "[Who's been around: " + "; ".join(p for _, p in entries) + ".]"
