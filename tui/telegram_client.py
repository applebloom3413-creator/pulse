"""
Telethon wrapper for the Pulse Chat TUI — now multi-persona.

A single TelegramHub owns one Telethon user-session (authed as you) and
maintains a map of registered personas. Each persona is identified by
its bot @username; the hub resolves it to a Telethon entity at startup.

Send / receive routing:
  - hub.send(persona_id, text)  → sends to that persona's bot
  - hub.on_message(persona_id, cb)  → callback fires when THAT bot replies
  - Bot identification on incoming messages: by sender_id == bot_entity.id

Async-native so it composes with the textual TUI event loop.
"""

import logging
from pathlib import Path
from telethon import TelegramClient, events


ENV_PATH = Path(__file__).parent / ".env"
SESSION_DIR = Path(__file__).parent / "session"
SESSION_DIR.mkdir(exist_ok=True)
SESSION_NAME = str(SESSION_DIR / "pulse_chat")


def _load_env() -> dict[str, str]:
    """Minimal .env reader — KEY=VALUE per line, ignoring comments/blanks."""
    env: dict[str, str] = {}
    if not ENV_PATH.exists():
        return env
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip()
    return env


_env = _load_env()
API_ID = int(_env.get("TELEGRAM_API_ID", "0"))
API_HASH = _env.get("TELEGRAM_API_HASH", "")


class TelegramHub:
    """One Telethon client, multiple persona-bot targets."""

    def __init__(self):
        if not API_ID or not API_HASH:
            raise RuntimeError(
                f"TELEGRAM_API_ID / TELEGRAM_API_HASH missing in {ENV_PATH}"
            )
        self.client = TelegramClient(SESSION_NAME, API_ID, API_HASH)
        # persona_id -> {entity, display_name, bot_username}
        self.personas: dict[str, dict] = {}
        # persona_id -> list[async callback]
        self._message_callbacks: dict[str, list] = {}
        self._dispatch_installed = False

    async def start(self) -> None:
        """Connect (first run triggers interactive phone+code auth)."""
        await self.client.start()

    async def register_persona(
        self,
        persona_id: str,
        bot_username: str,
        display_name: str,
    ) -> bool:
        """Resolve a bot @username and add it to the hub.

        Returns True if the bot was found and registered, False otherwise.
        Skips silently if bot_username is empty.
        """
        if not bot_username:
            return False
        username = bot_username.lstrip("@")
        try:
            entity = await self.client.get_entity(username)
        except Exception as e:
            logging.warning(f"Could not resolve @{username}: {e}")
            return False
        self.personas[persona_id] = {
            "entity": entity,
            "display_name": display_name,
            "bot_username": username,
        }
        self._message_callbacks.setdefault(persona_id, [])
        return True

    def install_dispatcher(self) -> None:
        """Install a single event handler that routes bot messages by sender.

        Call this once AFTER all personas are registered. Re-calling is a no-op.
        """
        if self._dispatch_installed:
            return
        if not self.personas:
            return

        # Build a sender_id -> persona_id index for O(1) routing
        id_to_persona = {
            info["entity"].id: pid for pid, info in self.personas.items()
        }
        all_entities = [info["entity"] for info in self.personas.values()]

        @self.client.on(events.NewMessage(from_users=all_entities))
        async def _router(event):
            pid = id_to_persona.get(event.sender_id)
            if pid is None:
                return
            for cb in self._message_callbacks.get(pid, []):
                try:
                    await cb(event.message)
                except Exception:
                    logging.exception(f"on_message callback failed for {pid}")

        self._dispatch_installed = True

    async def send(self, persona_id: str, text: str) -> None:
        """Send a text message to a persona's bot."""
        if persona_id not in self.personas:
            raise RuntimeError(f"Unknown persona: {persona_id}")
        entity = self.personas[persona_id]["entity"]
        await self.client.send_message(entity, text)

    def on_message(self, persona_id: str, callback) -> None:
        """Register an async callback for replies from a specific persona's bot.

        Callback signature: `async def cb(message) -> None`
        """
        self._message_callbacks.setdefault(persona_id, []).append(callback)

    async def stop(self) -> None:
        await self.client.disconnect()
