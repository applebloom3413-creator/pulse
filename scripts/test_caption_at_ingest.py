"""
End-to-end smoke test for the caption-at-ingest feature.

Loads a persona's config, initializes the captioner LLM (e.g. Haiku 4.5),
calls _caption_image() on one image, and prints the caption.
Costs a fraction of a cent for the one call.

    py -3.14 scripts/test_caption_at_ingest.py [--persona NAME] [path/to/image.png]

The persona defaults to $PULSE_PERSONA, then active_persona in config.yaml.
If no image path is given, uses the newest PNG in that persona's
data/paintings folder.
"""

from __future__ import annotations

import asyncio
import base64
import io
import os
import sys
from pathlib import Path

# Console safety — Windows cp1252 chokes on em dashes etc.
try:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
except Exception:
    pass

_HERE = Path(__file__).resolve().parent
_PULSE_ROOT = _HERE.parent
sys.path.insert(0, str(_PULSE_ROOT))
os.chdir(_PULSE_ROOT)  # so relative persona paths resolve

import argparse

import yaml
from core.engine import PulseEngine, CAPTION_SYSTEM_PROMPT


def _load_dotenv(path: Path) -> None:
    """Minimal .env reader — just enough for the API key."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _load_config(persona: str) -> dict:
    """Merge root config with the persona's overlay so PulseEngine can spin up."""
    with open(_PULSE_ROOT / "config.yaml", encoding="utf-8") as f:
        root = yaml.safe_load(f) or {}
    overlay_path = _PULSE_ROOT / "personas" / persona / "config.yaml"
    overlay = {}
    if overlay_path.exists():
        with open(overlay_path, encoding="utf-8") as f:
            overlay = yaml.safe_load(f) or {}
    for k, v in overlay.items():
        if isinstance(v, dict) and isinstance(root.get(k), dict):
            root[k] = {**root[k], **v}
        else:
            root[k] = v
    root["_db"] = None
    root["_shared_db"] = None
    root["_pulse_root"] = str(_PULSE_ROOT)
    return root


def _default_persona() -> str:
    if os.environ.get("PULSE_PERSONA"):
        return os.environ["PULSE_PERSONA"]
    try:
        with open(_PULSE_ROOT / "config.yaml", encoding="utf-8") as f:
            return (yaml.safe_load(f) or {}).get("active_persona") or ""
    except Exception:
        return ""


async def main() -> int:
    ap = argparse.ArgumentParser(description="Caption-at-ingest smoke test.")
    ap.add_argument("image", nargs="?", help="Image to caption (PNG/JPEG).")
    ap.add_argument("--persona", default=_default_persona(),
                    help="Persona folder under personas/ (default: $PULSE_PERSONA "
                         "or active_persona in config.yaml).")
    args = ap.parse_args()
    persona = args.persona
    if not persona:
        print("ERR: no persona given (use --persona NAME)")
        return 1

    _load_dotenv(_PULSE_ROOT / "personas" / persona / ".env")
    _load_dotenv(_PULSE_ROOT / ".env")

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        print(f"ERR: ANTHROPIC_API_KEY not set (looked in {persona}/.env then root .env)")
        return 1

    cfg = _load_config(persona)
    endpoint = cfg.get("provider", {}).get("endpoint", "https://api.anthropic.com/v1")
    try:
        engine = PulseEngine(
            config=cfg,
            channels={},
            skill_registry=None,
            llm_endpoint=endpoint,
            api_key=api_key,
        )
    except Exception as e:
        print(f"ERR: could not initialize engine: {e}")
        return 1

    if engine.captioner_llm is None:
        print("ERR: engine.captioner_llm is None — captioning_model not wired?")
        return 1

    print(f"Captioner LLM ready: {engine.captioner_llm.model_name}")
    print(f"System prompt: {len(CAPTION_SYSTEM_PROMPT)} chars\n")

    # Choose an image
    if args.image:
        img_path = Path(args.image)
    else:
        paintings = sorted(
            (_PULSE_ROOT / "personas" / persona / "data" / "paintings").glob("*.png"),
            key=lambda p: p.stat().st_mtime,
        )
        if not paintings:
            print(f"ERR: no image given and no paintings found for {persona}")
            return 1
        img_path = paintings[-1]
    if not img_path.exists():
        print(f"ERR: image not found: {img_path}")
        return 1

    print(f"Test image: {img_path}")
    print(f"Size: {img_path.stat().st_size:,} bytes")

    # Encode as data URI (same shape Telegram channel produces)
    ext = img_path.suffix.lower().lstrip(".")
    media_type = "image/png" if ext == "png" else "image/jpeg"
    b64 = base64.b64encode(img_path.read_bytes()).decode("utf-8")
    data_uri = f"data:{media_type};base64,{b64}"

    print("\nCalling _caption_image...")
    caption = await engine._caption_image(data_uri)

    if not caption:
        print("\nERR: caption was empty")
        return 1

    print("\n" + "=" * 60)
    print("CAPTION:")
    print("=" * 60)
    print(caption)
    print("=" * 60)
    print(f"\nCaption length: {len(caption)} chars")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
