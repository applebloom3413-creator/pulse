"""Centralized paths for the outbox tool."""
from pathlib import Path

# tools/outbox/paths.py  ->  pulse/
PULSE_ROOT = Path(__file__).resolve().parents[2]
TOOL_DIR = Path(__file__).resolve().parent

# Where you drop screenshots you want to send.
OUTBOX_DIR = PULSE_ROOT / "outbox"
SENT_DIR = OUTBOX_DIR / "sent"

# Telethon state (gitignored).
ENV_FILE = TOOL_DIR / ".env"
SESSION_FILE = TOOL_DIR / "pulse_outbox"  # telethon appends .session
GROUPS_FILE = TOOL_DIR / "groups.json"
LOCK_FILE = TOOL_DIR / ".lock"

PERSONAS_DIR = PULSE_ROOT / "personas"
ICONS_DIR = PULSE_ROOT / "icons"

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}


def ensure_dirs():
    OUTBOX_DIR.mkdir(parents=True, exist_ok=True)
    SENT_DIR.mkdir(parents=True, exist_ok=True)


def list_outbox_images() -> list[Path]:
    """Return image files currently sitting in the outbox folder (top-level only)."""
    if not OUTBOX_DIR.exists():
        return []
    return sorted(
        p for p in OUTBOX_DIR.iterdir()
        if p.is_file() and p.suffix.lower() in IMAGE_EXTS
    )
