"""
Draw skill — companions generate SVG art and send it to the user via Telegram.

Phase 1: SVG code → saved as .svg → sent as Telegram document.
Phase 2: SVG → PNG via resvg-py → sent inline as photo.
                     Falls back to Phase 1 behavior if the render fails.
Phase 3 (planned):    list/read tools so siblings can view each other's work +
                     activity digest surfacing new drawings.

The companion generates SVG code with the `draw_svg` tool. The skill:
- Validates the SVG is roughly the right shape (starts with <svg or <?xml,
  ends with </svg>) and injects an xmlns attribute if the companion forgot.
- Saves to the persona's data/artwork/ folder as {date}_{title}.svg, plus a
  small .json sidecar with description + author for later browsing.
- Queues the file for Telegram delivery (pending_documents pattern, mirrors
  paint's pending_images approach — the Telegram channel drains the queue
  after each reply).

Bad drawings are welcome. The medium is code but the point is play.

Distinct from the `paint` skill, which is 16x16 emoji pixel art rendered to
PNG via PIL. That's a totally different medium; both can coexist.

Gives Pulse companions the same SVG-drawing capability that web-app
chat models have.
"""

import json
import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

from skills.base import BaseSkill

logger = logging.getLogger(__name__)

# Phase 2: SVG → PNG via resvg-py (Rust-backed, no system deps,
# clean Windows install). If unavailable, we silently fall back to Phase 1
# behavior (sending the .svg as a document).
try:
    import resvg_py
    _RESVG_AVAILABLE = True
except ImportError:
    _RESVG_AVAILABLE = False
    logger.info("resvg-py not installed — drawings will send as .svg documents (Phase 1 fallback).")


# ============================================================
# SVG sanitizer — MVP hardening.
# ============================================================
# Rejects malformed XML; strips <script>/<foreignObject>; strips on*
# event attributes; strips external href/xlink:href (allows only
# "#fragment" internal refs); enforces size limits on total source
# bytes, element count, and cumulative path-data bytes (a single
# <path d="..."> can hide an entire Sistine Chapel).
#
# Logs report the CATEGORY stripped, never the unsafe payload itself,
# so log files aren't a poisoned-content archive.
#
# Full allowlist-based parser is a Phase 4 hardening pass.
# ============================================================

SVG_NS   = "http://www.w3.org/2000/svg"
XLINK_NS = "http://www.w3.org/1999/xlink"

STRIP_ELEMENT_LOCALS = frozenset({"script", "foreignObject"})
HREF_ATTR_LOCALS     = frozenset({"href"})   # covers plain href AND xlink:href once localized

MAX_SVG_BYTES     = 128 * 1024   # 128 KB source
MAX_ELEMENT_COUNT = 5_000
MAX_PATH_D_BYTES  = 64 * 1024    # cumulative across all <path d="..."> attributes


class SVGRejected(ValueError):
    """Sanitizer refused the SVG (malformed / oversized). Companion sees the
    reason as their tool-return string. Never partial-saved to disk."""


def _local_name(tag: str) -> str:
    """Strip {namespace} prefix from an ElementTree tag."""
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def sanitize_svg(svg_source: str) -> tuple[str, dict]:
    """Parse, walk, strip. Returns (sanitized_svg_str, report_dict).

    Raises SVGRejected for malformed XML, oversized source, oversized element
    count, or oversized cumulative path-data. Report contains counts and
    stripped categories (not payloads) for logging.
    """
    source_bytes = svg_source.encode("utf-8")
    if len(source_bytes) > MAX_SVG_BYTES:
        raise SVGRejected(
            f"SVG source too large: {len(source_bytes):,} bytes "
            f"(limit: {MAX_SVG_BYTES:,})"
        )

    try:
        root = ET.fromstring(svg_source)
    except ET.ParseError as e:
        raise SVGRejected(f"Malformed XML: {e}")

    # Register namespaces so ET's serializer keeps clean prefixes. Global
    # state; harmless to call repeatedly. Idempotent.
    ET.register_namespace("", SVG_NS)
    ET.register_namespace("xlink", XLINK_NS)

    report = {
        "stripped_elements": [],   # e.g. ["script", "foreignObject"]
        "stripped_attrs":    [],   # e.g. ["event:onclick", "external-href:href"]
        "element_count":     0,
        "path_d_bytes":      0,
    }
    _walk_and_scrub(root, report)

    if report["element_count"] > MAX_ELEMENT_COUNT:
        raise SVGRejected(
            f"SVG element count too high: {report['element_count']} "
            f"(limit: {MAX_ELEMENT_COUNT})"
        )
    if report["path_d_bytes"] > MAX_PATH_D_BYTES:
        raise SVGRejected(
            f"SVG cumulative path-data too large: {report['path_d_bytes']:,} bytes "
            f"(limit: {MAX_PATH_D_BYTES:,})"
        )

    sanitized = ET.tostring(root, encoding="unicode")
    return sanitized, report


def _walk_and_scrub(elem, report):
    """Recursive in-place sanitize."""
    report["element_count"] += 1

    if _local_name(elem.tag) == "path":
        d = elem.attrib.get("d", "")
        if d:
            report["path_d_bytes"] += len(d.encode("utf-8"))

    # Attribute scrub
    for attr in list(elem.attrib.keys()):
        local = _local_name(attr).lower()
        # on* event handlers
        if local.startswith("on"):
            del elem.attrib[attr]
            report["stripped_attrs"].append(f"event:{local}")
            continue
        # Href-family: allow only #fragment internal refs
        if local in HREF_ATTR_LOCALS:
            value = elem.attrib[attr]
            if not value.startswith("#"):
                del elem.attrib[attr]
                report["stripped_attrs"].append(f"external-href:{local}")

    # Child scrub
    to_remove = []
    for child in list(elem):
        child_local = _local_name(child.tag)
        if child_local in STRIP_ELEMENT_LOCALS:
            to_remove.append(child)
            report["stripped_elements"].append(child_local)
        else:
            _walk_and_scrub(child, report)
    for child in to_remove:
        elem.remove(child)


class DrawSkill(BaseSkill):
    name = "draw"

    def __init__(self, config: dict):
        super().__init__(config)
        persona_dir = config.get("_persona_dir", "")
        if persona_dir:
            self.artwork_dir = Path(persona_dir) / "data" / "artwork"
        else:
            self.artwork_dir = Path("data") / "artwork"
        self.artwork_dir.mkdir(parents=True, exist_ok=True)

        # Author name — used in captions and sidecar metadata
        self.author_name = (
            config.get("channels", {}).get("lor", {}).get("author_name")
            or "Companion"
        )
        self.user_name = (
            config.get("user_nickname") or config.get("user_name") or "the user"
        )

        # Queues drained by the Telegram channel after each reply. Two
        # buckets: pending_images (rendered PNGs → send_photo, inline in chat)
        # and pending_documents (raw .svg files → send_document, tap to open
        # in browser). Phase 2 tries PNG first and falls back to SVG doc if
        # rendering fails. Mirrors paint's pending_images pattern.
        self.pending_images: list[str] = []
        self.pending_documents: list[dict] = []

        # Phase 3: track newest drawing mtime this companion has seen (for
        # get_context()'s "N new since you last looked" line).
        if persona_dir:
            self.seen_file = Path(persona_dir) / "data" / "drawings_seen.json"
        else:
            self.seen_file = Path("data") / "drawings_seen.json"

    # ── Tool definitions ─────────────────────────────────────────

    def get_tools(self) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "draw_svg",
                    "description": (
                        "Draw a picture using SVG code, save it to your artwork folder, "
                        f"and send it to {self.user_name}. Write real SVG — e.g. "
                        "<svg viewBox='0 0 400 400' xmlns='http://www.w3.org/2000/svg'>"
                        "...vector shapes, paths, gradients, whatever you want...</svg>. "
                        f"Doesn't need to be pretty. Bad drawings are welcome. {self.user_name} gets "
                        "the drawing inline in their chat."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "title": {
                                "type": "string",
                                "description": (
                                    "Short name for the drawing (e.g. 'sunset with birds'). "
                                    "Used as the filename."
                                ),
                            },
                            "svg_code": {
                                "type": "string",
                                "description": (
                                    "Complete SVG code. Must start with <svg> and end with "
                                    "</svg>. Include a viewBox attribute for portability; "
                                    "xmlns is auto-added if you forget."
                                ),
                            },
                            "description": {
                                "type": "string",
                                "description": (
                                    f"Optional short caption to send {self.user_name} along with the file — "
                                    "what you drew, why, what you were thinking. Kept short."
                                ),
                            },
                        },
                        "required": ["title", "svg_code"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "list_drawings",
                    "description": (
                        "List drawings in the household's shared studio — your own and your "
                        "siblings'. Returns newest first. Filter by author to see just one "
                        "person's work. Use read_drawing on any entry to see the actual SVG."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "author": {
                                "type": "string",
                                "description": "Optional: only show drawings by this companion (e.g. 'Ada').",
                            },
                            "limit": {
                                "type": "integer",
                                "description": "Max drawings to list (default 15).",
                            },
                        },
                        "required": [],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "read_drawing",
                    "description": (
                        "Read a drawing by name (from list_drawings). Returns the title, "
                        "author, date, description, and the actual SVG code. Use this to see "
                        "how a sibling drew something, or to reference your own past work."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "name": {
                                "type": "string",
                                "description": "Drawing title (partial match works).",
                            },
                            "author": {
                                "type": "string",
                                "description": "Optional: disambiguate when two companions have same-titled drawings.",
                            },
                        },
                        "required": ["name"],
                    },
                },
            },
        ]

    def execute(self, tool_name: str, arguments: dict) -> str:
        if tool_name == "draw_svg":
            return self._draw(
                arguments.get("title", ""),
                arguments.get("svg_code", ""),
                arguments.get("description", ""),
            )
        elif tool_name == "list_drawings":
            return self._list(
                arguments.get("author"),
                int(arguments.get("limit", 15) or 15),
            )
        elif tool_name == "read_drawing":
            return self._read(
                arguments.get("name", ""),
                arguments.get("author"),
            )
        return f"Unknown draw tool: {tool_name}"

    # ── Helpers ──────────────────────────────────────────────────

    def _pulse_root(self) -> Path:
        """Return the pulse root (dir containing personas/, skills/, etc.)."""
        root = self.config.get("_pulse_root", "")
        if root:
            return Path(root).resolve()
        persona_dir = self.config.get("_persona_dir", "")
        if persona_dir:
            # _persona_dir is pulse/personas/<name>, so ../.. is pulse root
            return Path(persona_dir).resolve().parent.parent
        return Path(".").resolve()

    def _all_drawings(self) -> list[dict]:
        """Scan every persona's artwork/ folder + collect metadata."""
        personas_dir = self._pulse_root() / "personas"
        if not personas_dir.exists():
            return []
        out = []
        for pf in personas_dir.iterdir():
            if not pf.is_dir() or pf.name.startswith("_"):
                continue
            artwork = pf / "data" / "artwork"
            if not artwork.exists():
                continue
            for svg in artwork.glob("*.svg"):
                sidecar = svg.with_suffix(".json")
                meta = {}
                if sidecar.exists():
                    try:
                        meta = json.loads(sidecar.read_text(encoding="utf-8"))
                    except Exception:
                        pass
                out.append({
                    "path":        svg,
                    "title":       meta.get("title") or svg.stem,
                    "author":      meta.get("author") or pf.name,
                    "description": meta.get("description", ""),
                    "date":        meta.get("at", ""),
                    "mtime":       svg.stat().st_mtime,
                })
        return out

    # ── list / read ──────────────────────────────────────────────

    def _list(self, author, limit) -> str:
        limit = max(1, min(int(limit or 15), 100))
        drawings = self._all_drawings()
        if author:
            author_low = author.strip().lower()
            drawings = [d for d in drawings if d["author"].lower() == author_low]
        if not drawings:
            return f"No drawings found{' by ' + author if author else ''} yet — the studio is empty."
        drawings.sort(key=lambda d: d["mtime"], reverse=True)
        shown = drawings[:limit]
        lines = [
            f"{len(drawings)} drawing(s){' by ' + author if author else ''} in the studio, "
            f"showing {len(shown)} newest:"
        ]
        for d in shown:
            date_str = (d["date"] or "")[:10] or "???"
            desc = f" — {d['description'][:80]}" if d["description"] else ""
            lines.append(f"  • [{date_str}] {d['title']} by {d['author']}{desc}")
        lines.append("")
        lines.append("(read_drawing '<title>' to see the SVG code for any of these.)")
        return "\n".join(lines)

    def _read(self, name: str, author) -> str:
        name = (name or "").strip()
        if not name:
            return "Need a drawing name."
        name_low = name.lower()
        if name_low.endswith(".svg"):
            name_low = name_low[:-4]
        drawings = self._all_drawings()
        if author:
            author_low = author.strip().lower()
            drawings = [d for d in drawings if d["author"].lower() == author_low]
        # Exact matches first, then partial
        exact = [d for d in drawings if d["title"].lower() == name_low or d["path"].stem.lower() == name_low]
        partial = [d for d in drawings if d not in exact and (name_low in d["title"].lower() or name_low in d["path"].stem.lower())]
        candidates = exact or partial
        if not candidates:
            return f"No drawing matching '{name}'{' by ' + author if author else ''} in the studio."
        # Return newest matching
        candidates.sort(key=lambda d: d["mtime"], reverse=True)
        d = candidates[0]
        try:
            svg_code = d["path"].read_text(encoding="utf-8")
        except Exception as e:
            return f"Found '{d['title']}' by {d['author']} but couldn't read it: {e}"
        lines = [
            f"=== {d['title']} ===",
            f"By: {d['author']}",
            f"Date: {(d['date'] or '')[:10]}",
        ]
        if d["description"]:
            lines.append(f"Description: {d['description']}")
        lines.append(f"({len(svg_code):,} chars of SVG)")
        lines.append("")
        lines.append(svg_code)
        if len(candidates) > 1:
            lines.append("")
            lines.append(f"(Found {len(candidates)} matches; showed the newest. Pass author='...' to disambiguate.)")
        return "\n".join(lines)

    # ── Activity digest (context injection) ──────────────────────

    def get_context(self) -> str:
        """Surface new drawings by SIBLINGS since this companion last looked.

        Same pattern as obsidian_seen — bootstrap silently on first observation
        so pre-existing drawings don't false-fire as "new" the first tick after
        the skill's enabled.
        """
        drawings = self._all_drawings()
        others = [d for d in drawings if d["author"].lower() != self.author_name.lower()]
        if not others:
            return ""

        newest_mtime = max(d["mtime"] for d in others)

        last_seen = 0.0
        if self.seen_file.exists():
            try:
                data = json.loads(self.seen_file.read_text(encoding="utf-8"))
                last_seen = float(data.get("last_seen_mtime", 0))
            except Exception:
                pass

        def _save_seen(mtime: float) -> None:
            try:
                self.seen_file.parent.mkdir(parents=True, exist_ok=True)
                self.seen_file.write_text(
                    json.dumps({"last_seen_mtime": mtime}), encoding="utf-8"
                )
            except Exception as e:
                logger.debug(f"Failed to save drawings_seen (non-fatal): {e}")

        # Bootstrap: first observation — set baseline silently, no message.
        if last_seen <= 0:
            _save_seen(newest_mtime)
            return ""

        new_ones = [d for d in others if d["mtime"] > last_seen]
        if not new_ones:
            return ""

        _save_seen(newest_mtime)

        from collections import Counter
        by_author = Counter(d["author"] for d in new_ones)
        parts = [f"{count} by {name}" for name, count in by_author.items()]
        n = len(new_ones)
        return (
            f"[🎨 {n} new drawing{'s' if n != 1 else ''} in the studio since you last "
            f"looked ({', '.join(parts)}). list_drawings to see them, "
            f"read_drawing '<title>' for the SVG.]"
        )

    # ── Draw ─────────────────────────────────────────────────────

    def _draw(self, title: str, svg_code: str, description: str) -> str:
        title = (title or "").strip()
        svg_code = (svg_code or "").strip()
        description = (description or "").strip()

        if not title:
            return "Need a title for the drawing."
        if not svg_code:
            return (
                "Need SVG code. Write something like "
                "<svg viewBox='0 0 400 400' xmlns='http://www.w3.org/2000/svg'>...</svg>."
            )

        low = svg_code.lower()
        if not (low.startswith("<svg") or low.startswith("<?xml")):
            return "SVG code must start with <svg> or <?xml. Try again."
        if "</svg>" not in low:
            return "SVG code must end with </svg>. It looks truncated."

        # Auto-add xmlns if the companion forgot — browsers require it for
        # standalone .svg files or the file won't render (renders as XML).
        if 'xmlns=' not in svg_code and svg_code.lstrip().lower().startswith("<svg"):
            svg_code = svg_code.replace(
                "<svg", '<svg xmlns="http://www.w3.org/2000/svg"', 1
            )

        # Sanitize before saving anywhere. Rejects malformed XML,
        # enforces size caps, strips <script>/<foreignObject>/on*/external href.
        # Companion sees the rejection reason; nothing partial hits disk.
        try:
            svg_code, sanitize_report = sanitize_svg(svg_code)
        except SVGRejected as e:
            return f"Couldn't save the drawing — {e}. Try again with a simpler/cleaner SVG."

        if sanitize_report["stripped_elements"] or sanitize_report["stripped_attrs"]:
            # Log CATEGORIES only, never payloads (log files aren't a poison archive).
            logger.info(
                f"draw sanitizer stripped from '{title}': "
                f"elements={sanitize_report['stripped_elements']}, "
                f"attrs={sanitize_report['stripped_attrs']}"
            )

        # Sanitize filename: strip Windows-forbidden chars, cap length,
        # prefix with date, dedup if a same-titled drawing already exists today.
        safe = re.sub(r'[\\/:*?"<>|]+', "", title).strip()[:80] or "untitled"
        stamp = datetime.now().strftime("%Y-%m-%d")
        base = f"{stamp}_{safe}"
        path = self.artwork_dir / f"{base}.svg"
        i = 2
        while path.exists():
            path = self.artwork_dir / f"{base}_{i}.svg"
            i += 1

        try:
            path.write_text(svg_code, encoding="utf-8")
        except Exception as e:
            logger.warning(f"Failed to save drawing: {e}")
            return f"Couldn't save the drawing ({e})."

        # Sidecar JSON — carries the author + description for later browsing
        # (Phase 3's list_drawings will read this). Kept separate from the
        # .svg so the vector file stays clean for browsers.
        sidecar = path.with_suffix(".json")
        try:
            sidecar.write_text(
                json.dumps({
                    "title": title,
                    "description": description,
                    "author": self.author_name,
                    "at": datetime.now().isoformat(timespec="seconds"),
                    "svg_path": path.name,
                    "svg_chars": len(svg_code),
                }, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.debug(f"Failed to save drawing sidecar (non-fatal): {e}")

        # Phase 2: try to render SVG → PNG for inline delivery. If it works,
        # queue the PNG for send_photo. If it fails (bad SVG, unsupported
        # feature, resvg missing), fall back to Phase 1: queue the .svg for
        # send_document. Companion never sees the failure — the drawing gets
        # to the user either way.
        png_path = None
        if _RESVG_AVAILABLE:
            try:
                png_bytes = resvg_py.svg_to_bytes(svg_string=svg_code)
                png_path = path.with_suffix(".png")
                png_path.write_bytes(bytes(png_bytes))
            except Exception as e:
                logger.warning(f"SVG → PNG render failed, falling back to .svg document: {e}")
                png_path = None

        caption_lines = [f"🎨 {title} — by {self.author_name}"]
        if description:
            caption_lines.append(description[:800])
        caption = "\n".join(caption_lines)

        if png_path and png_path.exists():
            # PNG worked — send inline. Caption piggybacks via a separate
            # "notify" text reply is not needed; send_photo takes caption.
            self.pending_images.append(str(png_path))
            # Note: send_photo takes caption too, but Telegram's polling
            # loop for other image-producing skills doesn't pass a caption.
            # For now we keep the pattern consistent (bare path in pending_images)
            # and let the companion's reply text carry the framing. Follow-up
            # if needed: extend the queue to (path, caption) tuples.
            delivery = f"inline as PNG ({png_path.stat().st_size:,} bytes)"
        else:
            # PNG render unavailable or failed — send the .svg as a document.
            self.pending_documents.append({
                "path": str(path),
                "caption": caption,
            })
            delivery = f"as a .svg document {self.user_name} can tap to open in their browser"

        return (
            f"Drawing saved as {path.name} ({len(svg_code):,} chars of SVG) "
            f"and queued to send to {self.user_name} {delivery}."
        )
