"""
Group Chat helpers — coordinate multiple Pulse bots in a shared Telegram group.

Design:
- The user posts a message in the group, optionally tagging companions via @username.
- Tagged companions each get a budget of N messages for that "topic".
- Topic = a single user message + all follow-up bot responses.
- Topic resets when the user sends a NEW message in the group.
- Each bot tracks its own budget per topic in a shared state file.
- Bots can respond to either the user OR other bots (within budget).
- Random delay before responding so bots don't all reply simultaneously.
- @everyone or @all triggers all known bots.

State lives in data/group_chat_state.json so all bot instances see it.
"""

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_BUDGET = 3
EVERYONE_KEYWORDS = {"@everyone", "@all"}


def _state_path(pulse_root: Path) -> Path:
    return pulse_root / "data" / "group_chat_state.json"


def _load_state(pulse_root: Path) -> dict:
    p = _state_path(pulse_root)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_state(pulse_root: Path, state: dict) -> None:
    p = _state_path(pulse_root)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, indent=2), encoding="utf-8")


def parse_mentions(text: str, message_entities=None) -> tuple[set[str], bool]:
    """Extract @username mentions from a message.

    Returns (set of usernames lowercased without @, includes_everyone bool).
    Uses message_entities if provided (Telegram's structured mentions),
    falls back to regex if not.
    """
    text_lower = (text or "").lower()
    everyone = any(kw in text_lower for kw in EVERYONE_KEYWORDS)

    mentions: set[str] = set()
    if message_entities:
        for ent in message_entities:
            try:
                etype = getattr(ent, "type", None)
                if etype == "mention":
                    offset = ent.offset
                    length = ent.length
                    raw = text[offset:offset + length]
                    if raw.startswith("@"):
                        mentions.add(raw[1:].lower())
            except Exception:
                continue

    # Fallback: regex find @usernames
    for match in re.finditer(r"@([a-zA-Z][a-zA-Z0-9_]{2,31})", text or ""):
        mentions.add(match.group(1).lower())

    return mentions, everyone


def is_group_chat(update) -> bool:
    """Detect group/supergroup vs private chat."""
    try:
        chat_type = update.effective_chat.type
        return chat_type in ("group", "supergroup")
    except Exception:
        return False


def is_message_from_bot(update) -> bool:
    """Check if the message came from a bot (not a human)."""
    try:
        return bool(update.effective_user and update.effective_user.is_bot)
    except Exception:
        return False


def get_topic_state(pulse_root: Path, chat_id: str) -> dict:
    """Get the current topic state for a given chat."""
    state = _load_state(pulse_root)
    chats = state.get("chats", {})
    return chats.get(str(chat_id), {})


def start_new_topic(
    pulse_root: Path,
    chat_id: str,
    message_id: int,
    tagged_bots: set[str],
    everyone: bool,
    started_at: Optional[str] = None,
    budget: int = DEFAULT_BUDGET,
) -> None:
    """Start a fresh topic in the given chat.

    Resets all per-bot budgets. tagged_bots is a set of @usernames (lowercased,
    no @ prefix) that the user tagged. If everyone is True, all bots are eligible
    (we treat that as a wildcard).
    """
    state = _load_state(pulse_root)
    chats = state.setdefault("chats", {})
    chats[str(chat_id)] = {
        "topic_id": message_id,
        "started_at": started_at or datetime.now(timezone.utc).isoformat(),
        "tagged_bots": sorted(tagged_bots),
        "everyone": everyone,
        "responses": {},  # bot_username (lower) -> count
        "budget": budget,
    }
    _save_state(pulse_root, state)


def is_bot_eligible(topic: dict, my_username: str) -> bool:
    """Check if this bot is allowed to respond in the current topic."""
    if not topic:
        return False
    if topic.get("everyone"):
        return True
    tagged = set(topic.get("tagged_bots", []))
    return my_username.lower() in tagged


def get_bot_budget_remaining(topic: dict, my_username: str) -> int:
    """How many more messages can this bot send in the current topic."""
    budget = topic.get("budget", DEFAULT_BUDGET)
    used = topic.get("responses", {}).get(my_username.lower(), 0)
    return max(0, budget - used)


def record_bot_response(pulse_root: Path, chat_id: str, my_username: str) -> None:
    """Increment this bot's response counter for the current topic."""
    state = _load_state(pulse_root)
    chats = state.setdefault("chats", {})
    chat = chats.setdefault(str(chat_id), {})
    responses = chat.setdefault("responses", {})
    responses[my_username.lower()] = responses.get(my_username.lower(), 0) + 1
    _save_state(pulse_root, state)


def topic_matches(topic: dict, message_id: int) -> bool:
    """Check if the given message_id matches the current topic id."""
    return topic.get("topic_id") == message_id


def get_other_bot_names(pulse_root: Path, chat_id: str, my_username: str) -> list[str]:
    """Return the list of OTHER bots tagged in the current topic.

    Useful for the persona context — so a bot knows who else is in the
    conversation with them.
    """
    topic = get_topic_state(pulse_root, chat_id)
    tagged = topic.get("tagged_bots", [])
    return [b for b in tagged if b.lower() != my_username.lower()]
