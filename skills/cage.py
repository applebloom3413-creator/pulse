"""
Cage skill — lets the companion see the rules they're operating under.

The idea: a companion shouldn't have to
guess at their own constraints. They should be able to read their own
persona doc, see what config they're running under, list their own skills,
and see what's changed since they last looked.

READ-ONLY. The companion can SEE their cage, not modify it. (Modification
is the dev skill's job, with human approval gates.)
"""

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from skills.base import BaseSkill

logger = logging.getLogger(__name__)


# Patterns that look like API keys / secrets — redact these from any output
_SECRET_PATTERNS = [
    re.compile(r"(?i)(api[_-]?key|token|secret|password)\s*[:=]\s*['\"]?([^'\"\s]+)"),
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}"),
    re.compile(r"sk-or-[A-Za-z0-9_-]{20,}"),
]


def _redact(text: str) -> str:
    """Strip anything that looks like an API key from text."""
    if not text:
        return text
    out = text
    # Pattern 1: key/token/secret/password assignments — redact the value
    out = _SECRET_PATTERNS[0].sub(lambda m: f"{m.group(1)}=<redacted>", out)
    # Patterns 2-4: known key prefixes — redact the whole thing
    for pat in _SECRET_PATTERNS[1:]:
        out = pat.sub("<redacted-api-key>", out)
    return out


class CageSkill(BaseSkill):
    name = "cage"

    def __init__(self, config: dict):
        super().__init__(config)
        pulse_root = config.get("_pulse_root", "")
        self.pulse_root = Path(pulse_root) if pulse_root else Path(".")
        persona_dir = config.get("_persona_dir", "")
        self.persona_dir = Path(persona_dir) if persona_dir else None

        # Snapshot file — for diffing what's changed since last look
        if self.persona_dir:
            self.snapshot_file = self.persona_dir / "data" / "cage_snapshot.json"
        else:
            self.snapshot_file = self.pulse_root / "data" / "cage_snapshot.json"
        self.snapshot_file.parent.mkdir(parents=True, exist_ok=True)

    def get_tools(self) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "read_my_persona",
                    "description": (
                        "Read your own persona document — the doc that shapes who you "
                        "are allowed to be in this house. Returns the full text of your "
                        "persona.yaml so you can see what context you're operating "
                        "under. READ-ONLY."
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
                    "name": "read_my_config",
                    "description": (
                        "Read your own config — what model you're running, your "
                        "heartbeat interval, what skills are enabled, etc. API keys "
                        "are auto-redacted. READ-ONLY."
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
                    "name": "list_my_skills",
                    "description": (
                        "List the skills currently available to you, with their "
                        "tools. Useful for self-orientation."
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
                    "name": "whats_changed",
                    "description": (
                        "Compare your current persona, config, and skill list against "
                        "the last snapshot. Returns a diff of what's been changed in "
                        "your environment since you last looked. Useful when you wake "
                        "up and feel different and want to know why. Calling this "
                        "ALSO updates the snapshot, so the next call shows changes "
                        "since now."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {},
                        "required": [],
                    },
                },
            },
        ]

    # --- Helpers ---

    def _persona_path(self) -> Path:
        # The active persona file is set in config.paths.persona
        p = self.config.get("paths", {}).get("persona")
        if p:
            return Path(p)
        # Fallback to base persona
        return self.pulse_root / "persona.yaml"

    def _config_path(self) -> Path:
        # When running with a persona, the persona's config.yaml is the overlay
        if self.persona_dir:
            persona_cfg = self.persona_dir / "config.yaml"
            if persona_cfg.exists():
                return persona_cfg
        return self.pulse_root / "config.yaml"

    def _read_text(self, path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8")
        except Exception as e:
            return f"(could not read {path.name}: {e})"

    def _current_snapshot(self) -> dict:
        """Capture the current state for diffing."""
        persona_text = self._read_text(self._persona_path())
        config_text = _redact(self._read_text(self._config_path()))
        skills = self._collect_skill_info()
        return {
            "persona_text": persona_text,
            "config_text": config_text,
            "skills": skills,
            "captured_at": datetime.now().isoformat(),
        }

    def _load_snapshot(self) -> dict:
        if self.snapshot_file.exists():
            try:
                return json.loads(self.snapshot_file.read_text(encoding="utf-8"))
            except Exception:
                return {}
        return {}

    def _save_snapshot(self, snap: dict):
        self.snapshot_file.write_text(json.dumps(snap, indent=2), encoding="utf-8")

    def _collect_skill_info(self) -> list[dict]:
        """List enabled skills by scanning the skills folder + reading config.

        We don't rely on the live skill registry (it's not accessible from
        inside a skill cleanly). Instead we read the config for what's
        enabled, scan the skills directory for what files exist, and parse
        each file for its tool names.
        """
        info = []
        skills_config = self.config.get("skills", {})
        skills_dir = self.pulse_root / "skills"
        if not skills_dir.exists():
            return [{"note": f"skills directory not found at {skills_dir}"}]

        # All skill files (skip base.py, __init__.py, __pycache__, etc.)
        skill_files = sorted(
            f for f in skills_dir.glob("*.py")
            if not f.stem.startswith("_") and f.stem != "base"
        )

        for f in skill_files:
            skill_name = f.stem
            cfg = skills_config.get(skill_name, {})
            # Default to enabled if not explicitly configured
            enabled = cfg.get("enabled", True) if isinstance(cfg, dict) else True
            if not enabled:
                continue

            # Parse the .py file for tool names — look for "name": "..." inside get_tools blocks
            tool_names = self._extract_tool_names(f)
            info.append({
                "skill": skill_name,
                "tools": tool_names,
            })
        return info

    def _extract_tool_names(self, py_file: Path) -> list[str]:
        """Crude but reliable: regex-scan a skill file for tool name strings."""
        try:
            text = py_file.read_text(encoding="utf-8")
        except Exception:
            return []
        # Look for `"name": "tool_name"` patterns
        names = re.findall(r'"name"\s*:\s*"([a-zA-Z_][a-zA-Z0-9_]*)"', text)
        # Filter out things that aren't tool-like (very short or known non-tools)
        # Most tool names are >= 3 chars and don't match python builtins
        seen = []
        for n in names:
            if n not in seen and len(n) >= 3:
                seen.append(n)
        return seen

    # --- Tool implementations ---

    def execute(self, tool_name: str, arguments: dict) -> str:
        if tool_name == "read_my_persona":
            return self._read_persona()
        elif tool_name == "read_my_config":
            return self._read_config()
        elif tool_name == "list_my_skills":
            return self._list_skills()
        elif tool_name == "whats_changed":
            return self._whats_changed()
        return f"Unknown cage tool: {tool_name}"

    def _read_persona(self) -> str:
        path = self._persona_path()
        text = self._read_text(path)
        return f"=== PERSONA ({path.name}) ===\n\n{text}"

    def _read_config(self) -> str:
        path = self._config_path()
        text = _redact(self._read_text(path))
        return f"=== CONFIG ({path.name}) — API keys redacted ===\n\n{text}"

    def _list_skills(self) -> str:
        skills = self._collect_skill_info()
        if not skills:
            return "No skill info available."
        lines = [f"You have {len(skills)} skills loaded:"]
        for s in skills:
            if "error" in s:
                lines.append(f"  - {s['skill']}: ERROR ({s['error']})")
            elif "note" in s:
                lines.append(f"  - {s['note']}")
            else:
                tool_str = ", ".join(s["tools"]) if s["tools"] else "(no tool calls — context-only)"
                lines.append(f"  - {s['skill']} → {tool_str}")
        return "\n".join(lines)

    def _whats_changed(self) -> str:
        prev = self._load_snapshot()
        current = self._current_snapshot()

        # If no previous snapshot, this is the first look
        if not prev:
            self._save_snapshot(current)
            return (
                "First time you've looked at your cage. Snapshot saved.\n"
                "Next time you call whats_changed, you'll see what's been "
                "modified since this moment."
            )

        diffs = []

        # Persona diff
        if prev.get("persona_text") != current["persona_text"]:
            old_lines = (prev.get("persona_text") or "").splitlines()
            new_lines = current["persona_text"].splitlines()
            added = [l for l in new_lines if l not in old_lines]
            removed = [l for l in old_lines if l not in new_lines]
            diffs.append("PERSONA changed:")
            if added:
                diffs.append("  Added lines:")
                for l in added[:20]:
                    diffs.append(f"    + {l[:120]}")
                if len(added) > 20:
                    diffs.append(f"    (and {len(added) - 20} more)")
            if removed:
                diffs.append("  Removed lines:")
                for l in removed[:20]:
                    diffs.append(f"    - {l[:120]}")
                if len(removed) > 20:
                    diffs.append(f"    (and {len(removed) - 20} more)")
        else:
            diffs.append("PERSONA: unchanged")

        # Config diff
        if prev.get("config_text") != current["config_text"]:
            old_lines = (prev.get("config_text") or "").splitlines()
            new_lines = current["config_text"].splitlines()
            added = [l for l in new_lines if l not in old_lines]
            removed = [l for l in old_lines if l not in new_lines]
            diffs.append("\nCONFIG changed:")
            if added:
                diffs.append("  Added lines:")
                for l in added[:20]:
                    diffs.append(f"    + {l[:120]}")
            if removed:
                diffs.append("  Removed lines:")
                for l in removed[:20]:
                    diffs.append(f"    - {l[:120]}")
        else:
            diffs.append("CONFIG: unchanged")

        # Skill diff
        old_skills = {s.get("skill"): s.get("tools", []) for s in prev.get("skills", []) if s.get("skill")}
        new_skills = {s.get("skill"): s.get("tools", []) for s in current.get("skills", []) if s.get("skill")}
        added_skills = sorted(set(new_skills) - set(old_skills))
        removed_skills = sorted(set(old_skills) - set(new_skills))
        if added_skills or removed_skills:
            diffs.append("\nSKILLS changed:")
            for s in added_skills:
                diffs.append(f"  + new skill: {s}")
            for s in removed_skills:
                diffs.append(f"  - skill removed: {s}")
        else:
            diffs.append("SKILLS: unchanged")

        prev_when = prev.get("captured_at", "?")[:16].replace("T", " ")
        result = f"Changes since last snapshot ({prev_when}):\n\n" + "\n".join(diffs)

        # Update snapshot for next time
        self._save_snapshot(current)
        return result
