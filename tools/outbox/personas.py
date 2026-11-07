"""Discover Pulse personas + their Telegram destinations."""
from __future__ import annotations
import re
from dataclasses import dataclass
from pathlib import Path

from .paths import PERSONAS_DIR


@dataclass
class Persona:
    name: str           # directory name, e.g. "ada"
    display: str        # nicer label for UI (capitalized)
    chat_id: str        # numeric chat_id with the user (str form, as Telegram returns)
    bot_token: str      # for future use; not needed for Telethon delivery
    persona_dir: Path


_TOKEN_RE = re.compile(r"^\s*TELEGRAM_BOT_TOKEN\s*=\s*(.+?)\s*$", re.MULTILINE)


def _read_bot_token(env_path: Path) -> str | None:
    """Read TELEGRAM_BOT_TOKEN from a persona .env without pulling in python-dotenv."""
    try:
        text = env_path.read_text(encoding="utf-8")
    except OSError:
        return None
    m = _TOKEN_RE.search(text)
    if not m:
        return None
    raw = m.group(1).strip().strip('"').strip("'")
    return raw or None


def discover_personas() -> list[Persona]:
    """Walk personas/ and return every persona with a bot token + saved chat_id."""
    if not PERSONAS_DIR.exists():
        return []

    found: list[Persona] = []
    for child in sorted(PERSONAS_DIR.iterdir()):
        if not child.is_dir() or child.name.startswith("_"):
            continue

        env_path = child / ".env"
        chat_id_path = child / "data" / "telegram_chat_id.txt"
        if not env_path.exists() or not chat_id_path.exists():
            continue

        token = _read_bot_token(env_path)
        if not token:
            continue

        try:
            chat_id = chat_id_path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if not chat_id:
            continue

        found.append(Persona(
            name=child.name,
            display=child.name.capitalize(),
            chat_id=chat_id,
            bot_token=token,
            persona_dir=child,
        ))
    return found
