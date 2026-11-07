"""
Reactions skill — let Pulse send reaction images via Telegram.

The user stocks the data/reactions/ folder with images (PNG, JPG, GIF, WebP).
Filenames become the descriptions Pulse uses to pick reactions.

A persona can ALSO have its own private reaction set via config:

  reactions:
    dir: "personas/<name>/data/reactions"   # relative to pulse root, or absolute

When set, that folder is merged ON TOP of the shared house folder — the
companion sees both the shared reactions and its own personal ones, and the
personal set stays private to that persona (other companions never see it).
This is how a companion gets its own custom emotes.

Optional: a reactions.json file in any folder can override/expand descriptions.
Format:
  {
    "thumbs-up.png": "approval, nice work",
    "surprised-cat.gif": "shocked, overwhelmed",
    ...
  }

Pulse can list available reactions and send one whenever it fits the moment.
"""

import json
import logging
import random
from pathlib import Path
from skills.base import BaseSkill

logger = logging.getLogger(__name__)

VALID_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}


class ReactionsSkill(BaseSkill):
    name = "reactions"

    def __init__(self, config: dict):
        super().__init__(config)
        pulse_root = config.get("_pulse_root")
        base = Path(pulse_root) if pulse_root else Path(".")

        # Shared house reaction folder — available to every persona.
        self.reactions_dir = base / "data" / "reactions"
        self.reactions_dir.mkdir(parents=True, exist_ok=True)
        dirs = [self.reactions_dir]

        # Optional per-persona personal reaction folder (merged on top).
        custom = (config.get("reactions") or {}).get("dir")
        if custom:
            cpath = Path(custom)
            if not cpath.is_absolute():
                cpath = base / cpath
            try:
                cpath.mkdir(parents=True, exist_ok=True)
            except Exception as e:
                logger.warning(f"Could not prepare persona reactions dir {cpath}: {e}")
            if cpath.resolve() != self.reactions_dir.resolve():
                dirs.append(cpath)
                logger.info(f"Personal reactions folder added: {cpath}")

        self.reaction_dirs = dirs
        self.descriptions_file = self.reactions_dir / "reactions.json"  # legacy ref
        # pending_images is the channel pickup list for sending images via Telegram
        self.pending_images: list[str] = []

    def get_tools(self) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "list_reactions",
                    "description": (
                        "List all reaction images available to send. Each entry shows "
                        "the filename and its description (or filename-as-description if "
                        "no description is set). Call this FIRST before sending a reaction "
                        "so you know what you have to work with."
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
                    "name": "send_reaction",
                    "description": (
                        "Send a reaction image via Telegram. Use sparingly and only when "
                        "the moment genuinely calls for it — like a real friend who reaches "
                        "for the right meme at the right time, not someone who spams memes "
                        "constantly. The image gets sent to the user's chat. Provide the "
                        "filename exactly as it appears in list_reactions. Optionally include "
                        "a short caption."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "filename": {
                                "type": "string",
                                "description": "Exact filename of the reaction to send (from list_reactions)."
                            },
                            "caption": {
                                "type": "string",
                                "description": "Optional short caption to send with the image. Leave empty for no caption."
                            },
                        },
                        "required": ["filename"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "send_random_reaction",
                    "description": (
                        "Send a random reaction image. Use when you want to be playfully "
                        "unpredictable. Optionally filter by a keyword in the description "
                        "(e.g. 'cat', 'sad', 'happy'). Returns the filename that was sent."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "keyword": {
                                "type": "string",
                                "description": "Optional: only pick from reactions whose description contains this keyword."
                            },
                            "caption": {
                                "type": "string",
                                "description": "Optional short caption."
                            },
                        },
                        "required": [],
                    },
                },
            },
        ]

    def _load_descriptions(self) -> dict:
        """Merge reactions.json from every reaction folder (later dirs win)."""
        merged: dict = {}
        for d in self.reaction_dirs:
            f = d / "reactions.json"
            if f.exists():
                try:
                    merged.update(json.loads(f.read_text(encoding="utf-8")))
                except Exception:
                    pass
        return merged

    def _list_files(self) -> list[Path]:
        """All image files across every reaction folder (first occurrence wins)."""
        files = []
        seen = set()
        for d in self.reaction_dirs:
            if not d.exists():
                continue
            for f in d.iterdir():
                if f.is_file() and f.suffix.lower() in VALID_EXTS and f.name not in seen:
                    files.append(f)
                    seen.add(f.name)
        return sorted(files, key=lambda p: p.name)

    def _filename_to_description(self, filename: str) -> str:
        """Turn 'screaming-cat-vibes.gif' into 'screaming cat vibes'."""
        stem = Path(filename).stem
        return stem.replace("-", " ").replace("_", " ").strip()

    def _resolve(self, filename: str) -> Path | None:
        """Find a reaction file by name across all reaction folders."""
        for d in self.reaction_dirs:
            cand = d / filename
            if cand.exists():
                return cand
        return None

    def execute(self, tool_name: str, arguments: dict) -> str:
        if tool_name == "list_reactions":
            return self._list()
        elif tool_name == "send_reaction":
            return self._send(arguments)
        elif tool_name == "send_random_reaction":
            return self._send_random(arguments)
        return f"Unknown tool: {tool_name}"

    def _list(self) -> str:
        files = self._list_files()
        if not files:
            return (
                "No reaction images yet. The user needs to drop some PNG/JPG/GIF files "
                f"into {self.reactions_dir}. Then they'll show up here for you to use."
            )
        descriptions = self._load_descriptions()
        lines = [f"Available reactions ({len(files)}):"]
        for f in files:
            desc = descriptions.get(f.name) or self._filename_to_description(f.name)
            lines.append(f"  • {f.name} — {desc}")
        return "\n".join(lines)

    def _send(self, args: dict) -> str:
        filename = args.get("filename", "").strip()
        caption = args.get("caption", "").strip()
        if not filename:
            return "Filename was empty. Call list_reactions to see what's available."

        target = self._resolve(filename)
        if target is None:
            return (
                f"No reaction named '{filename}'. Call list_reactions to see what's "
                f"actually available."
            )

        self.pending_images.append(str(target))
        logger.info(f"Reaction queued for send: {filename}")
        if caption:
            return f"Sending reaction: {filename} (with caption: \"{caption}\")"
        return f"Sending reaction: {filename}"

    def _send_random(self, args: dict) -> str:
        files = self._list_files()
        if not files:
            return "No reactions to pick from yet."

        keyword = args.get("keyword", "").strip().lower()
        caption = args.get("caption", "").strip()

        if keyword:
            descriptions = self._load_descriptions()
            filtered = []
            for f in files:
                desc = (descriptions.get(f.name) or self._filename_to_description(f.name)).lower()
                if keyword in desc or keyword in f.name.lower():
                    filtered.append(f)
            if not filtered:
                return f"No reactions match keyword '{keyword}'. Try list_reactions."
            files = filtered

        chosen = random.choice(files)
        self.pending_images.append(str(chosen))
        logger.info(f"Random reaction queued: {chosen.name}")
        if caption:
            return f"Sending random reaction: {chosen.name} (with caption: \"{caption}\")"
        return f"Sending random reaction: {chosen.name}"
