"""
Sticky Notes skill — text notes the companion leaves in the room
between conversations.

Stickies persist across heartbeats and conversations. They're auto-injected
into context (so the companion sees them when waking up) AND can be browsed
or removed via tool calls.

Each sticky renders as a PNG using a pixel art sticky note background
with the note text drawn on top. PNGs are sent via Telegram so the visual
metaphor lands.
"""

import json
import logging
import textwrap
from datetime import datetime
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont
from skills.base import BaseSkill

logger = logging.getLogger(__name__)


class StickySkill(BaseSkill):
    name = "sticky"

    def __init__(self, config: dict):
        super().__init__(config)
        persona_dir = config.get("_persona_dir", "")
        if persona_dir:
            self.sticky_dir = Path(persona_dir) / "data" / "stickies"
        else:
            self.sticky_dir = Path("data") / "stickies"
        self.sticky_dir.mkdir(parents=True, exist_ok=True)

        # Locate the sticky background art
        pulse_root = config.get("_pulse_root")
        if pulse_root:
            self.sticky_bg = Path(pulse_root) / "data" / "assets" / "sticky_note.png"
        else:
            self.sticky_bg = Path("data") / "assets" / "sticky_note.png"

        self.pending_images: list[str] = []
        self.stickies_file = self.sticky_dir / "stickies.json"

    def get_tools(self) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "leave_sticky",
                    "description": (
                        "Leave a sticky note in the room. The note persists between "
                        "conversations and you'll see it next time you wake up. Use this "
                        "for: things to remember for next time, half-finished thoughts, "
                        "ideas you want to come back to, or messages to your future self. "
                        "Stickies are short — keep it under a few lines. They render as "
                        "actual yellow sticky note images sent via Telegram."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "note": {
                                "type": "string",
                                "description": "The note text. Keep it short — like an actual sticky note. A few lines max."
                            },
                        },
                        "required": ["note"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "read_stickies",
                    "description": (
                        "Read all sticky notes you've left in the room. Returns the "
                        "current set of notes you have posted."
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
                    "name": "remove_sticky",
                    "description": "Take down a sticky note by its ID. Use when you're done with it.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "sticky_id": {
                                "type": "string",
                                "description": "The ID of the sticky to remove."
                            },
                        },
                        "required": ["sticky_id"],
                    },
                },
            },
        ]

    def get_context(self) -> str:
        """Inject all current stickies into every prompt."""
        stickies = self._load()
        if not stickies:
            return ""
        lines = ["[Sticky notes you've left in the room:]"]
        for s in stickies[-10:]:  # cap at 10
            when = s.get("created_at", "")[:16].replace("T", " ")
            lines.append(f"  📌 [{s['id']}, {when}] {s['note']}")
        return "\n".join(lines)

    def _load(self) -> list[dict]:
        if self.stickies_file.exists():
            try:
                return json.loads(self.stickies_file.read_text(encoding="utf-8"))
            except Exception:
                return []
        return []

    def _save(self, stickies: list[dict]):
        self.stickies_file.write_text(json.dumps(stickies, indent=2), encoding="utf-8")

    def _next_id(self) -> str:
        existing = self._load()
        if not existing:
            return "001"
        nums = []
        for s in existing:
            try:
                nums.append(int(s["id"]))
            except Exception:
                pass
        return f"{(max(nums) + 1) if nums else 1:03d}"

    def _detect_paper_bounds(self, img: Image.Image) -> tuple[int, int, int, int]:
        """Detect the bounds of the yellow sticky paper within the image.

        Returns (left, top, right, bottom). Falls back to the full image if
        no yellow region is found (e.g. fallback solid yellow square).
        """
        try:
            import numpy as np
            arr = np.array(img)
            # Yellow paper detection (R>200, G>180, B<150, opaque)
            mask = (
                (arr[:, :, 0] > 200) &
                (arr[:, :, 1] > 180) &
                (arr[:, :, 2] < 150) &
                (arr[:, :, 3] > 0)
            )
            ys, xs = np.where(mask)
            if len(ys) == 0:
                return (0, 0, img.width, img.height)
            return (int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max()))
        except Exception:
            return (0, 0, img.width, img.height)

    def _render_sticky_image(self, sticky_id: str, note_text: str) -> str:
        """Render a sticky note PNG with note text drawn inside the yellow paper area.

        Steps:
          1. Open the sticky background asset
          2. Detect the bounds of the actual yellow paper area
          3. Crop the image to just the sticky (with a small margin) so it looks
             bigger when sent via Telegram
          4. Draw the text inside the yellow paper bounds with proper padding
        """
        out_path = self.sticky_dir / f"sticky_{sticky_id}.png"
        try:
            if self.sticky_bg.exists():
                img = Image.open(self.sticky_bg).convert("RGBA")
            else:
                # Fallback: yellow square if asset is missing
                img = Image.new("RGBA", (600, 600), (252, 232, 131, 255))
        except Exception:
            img = Image.new("RGBA", (600, 600), (252, 232, 131, 255))

        # Find yellow paper bounds
        paper_left, paper_top, paper_right, paper_bottom = self._detect_paper_bounds(img)
        paper_w = paper_right - paper_left
        paper_h = paper_bottom - paper_top

        # Crop to a tight bounding box that includes the sticky and a small
        # margin (so the tape decorations stay visible but excess whitespace
        # is removed).  Use the non-transparent bbox if available.
        crop_bbox = img.getbbox() or (paper_left, paper_top, paper_right, paper_bottom)
        cl, ct, cr, cb = crop_bbox
        # Add ~3% margin around the bbox
        margin_x = int(img.width * 0.02)
        margin_y = int(img.height * 0.02)
        cl = max(0, cl - margin_x)
        ct = max(0, ct - margin_y)
        cr = min(img.width, cr + margin_x)
        cb = min(img.height, cb + margin_y)
        cropped = img.crop((cl, ct, cr, cb))

        # Recompute paper bounds within the cropped image
        new_paper_left = paper_left - cl
        new_paper_top = paper_top - ct
        new_paper_right = paper_right - cl
        new_paper_bottom = paper_bottom - ct

        draw = ImageDraw.Draw(cropped)

        # Inner padding within the paper area (roughly 8% of the paper size)
        inner_pad_x = int(paper_w * 0.08)
        inner_pad_y = int(paper_h * 0.10)
        text_left = new_paper_left + inner_pad_x
        text_top = new_paper_top + inner_pad_y
        text_width = paper_w - 2 * inner_pad_x

        # Choose font size proportional to paper height (so it fits comfortably
        # regardless of how big the source asset is).
        font_size = max(int(paper_h * 0.06), 24)
        font = None
        for candidate in [
            "C:/Windows/Fonts/AtkinsonHyperlegible-Regular.ttf",
            "C:/Windows/Fonts/Arial.ttf",
            "C:/Windows/Fonts/comic.ttf",
        ]:
            try:
                font = ImageFont.truetype(candidate, font_size)
                break
            except Exception:
                continue
        if font is None:
            font = ImageFont.load_default()

        # Wrap text to fit within paper width
        # Estimate chars per line based on actual font metrics
        try:
            avg_char_w = font.getlength("M")
        except Exception:
            avg_char_w = font_size * 0.6
        chars_per_line = max(int(text_width / max(avg_char_w, 1)), 8)
        wrapped = textwrap.fill(note_text.strip(), width=chars_per_line)

        draw.multiline_text(
            (text_left, text_top),
            wrapped,
            fill=(40, 40, 40, 255),
            font=font,
            spacing=int(font_size * 0.25),
        )

        cropped.save(out_path, "PNG")
        return str(out_path)

    def execute(self, tool_name: str, arguments: dict) -> str:
        if tool_name == "leave_sticky":
            return self._leave(arguments)
        elif tool_name == "read_stickies":
            return self._read()
        elif tool_name == "remove_sticky":
            return self._remove(arguments)
        return f"Unknown tool: {tool_name}"

    def _leave(self, args: dict) -> str:
        note = args.get("note", "").strip()
        if not note:
            return "Sticky note was empty — nothing to post."
        sticky_id = self._next_id()
        sticky = {
            "id": sticky_id,
            "note": note,
            "created_at": datetime.now().isoformat(),
        }
        stickies = self._load()
        stickies.append(sticky)
        self._save(stickies)

        # Render the visual sticky and queue it for Telegram
        try:
            img_path = self._render_sticky_image(sticky_id, note)
            self.pending_images.append(img_path)
        except Exception as e:
            logger.warning(f"Failed to render sticky image: {e}")

        return f"Sticky note left in the room (#{sticky_id}). You'll see it next time you wake up."

    def _read(self) -> str:
        stickies = self._load()
        if not stickies:
            return "No sticky notes in the room right now."
        lines = [f"Your sticky notes ({len(stickies)} total):"]
        for s in stickies:
            when = s.get("created_at", "")[:16].replace("T", " ")
            lines.append(f"  📌 [#{s['id']}, {when}] {s['note']}")
        return "\n".join(lines)

    def _remove(self, args: dict) -> str:
        sticky_id = args.get("sticky_id", "").strip()
        stickies = self._load()
        new_stickies = [s for s in stickies if s["id"] != sticky_id]
        if len(new_stickies) == len(stickies):
            return f"No sticky #{sticky_id} found."
        self._save(new_stickies)
        # Also delete the rendered PNG
        png_path = self.sticky_dir / f"sticky_{sticky_id}.png"
        if png_path.exists():
            try:
                png_path.unlink()
            except Exception:
                pass
        return f"Sticky #{sticky_id} taken down."
