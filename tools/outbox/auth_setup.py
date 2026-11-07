"""One-time Telethon auth for the Pulse Outbox.

Run this ONCE before the first send. It will:

  1. Ask for your Telegram API ID and API Hash (free, from https://my.telegram.org).
  2. Ask for your phone number (the one your Telegram account is on).
  3. Send a login code to that account; you paste it back here.
  4. Save a session file so future sends don't ask again.

After this completes, you never need to run it again — unless you
delete pulse_outbox.session or want to switch accounts.
"""
from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # so `tools.outbox` imports work when run directly

from tools.outbox.paths import ENV_FILE, SESSION_FILE


BANNER = """
================================================================
 Pulse Outbox — Telethon auth setup
================================================================

This is a one-time setup. You'll need:

  1. A Telegram "API ID" and "API Hash"
       Get them free at:  https://my.telegram.org
       Log in with your phone number, click "API development tools",
       create an app (any name/description), copy the two values.

  2. Your phone number in international format (e.g. +15551234567).
     This is the number your normal Telegram account uses.

Telegram will send a login code TO YOUR TELEGRAM APP. Paste it here.
If you have 2FA enabled, you'll also be asked for your password.

After this completes, sends from the outbox will appear as YOU
sending — exactly like sending from your phone. Companions see
the album the same way they see anything else you send.
"""


def _save_env(api_id: str, api_hash: str, phone: str) -> None:
    """Write API credentials to tools/outbox/.env (gitignored)."""
    lines = [
        "# Pulse Outbox — Telethon credentials. Do not commit.",
        f"TELEGRAM_API_ID={api_id}",
        f"TELEGRAM_API_HASH={api_hash}",
        f"TELEGRAM_PHONE={phone}",
        "",
    ]
    ENV_FILE.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    print(BANNER)

    if ENV_FILE.exists():
        print(f"Existing credentials found at {ENV_FILE}.")
        ans = input("Overwrite and re-auth? [y/N]: ").strip().lower()
        if ans != "y":
            print("Keeping existing credentials. Exiting.")
            return 0

    api_id = input("API ID:       ").strip()
    api_hash = input("API Hash:     ").strip()
    phone = input("Phone (+CC...): ").strip()

    if not api_id.isdigit():
        print(f"ERROR: API ID must be a number, got {api_id!r}.")
        return 1
    if not api_hash:
        print("ERROR: API Hash is empty.")
        return 1
    if not phone.startswith("+"):
        print("ERROR: Phone must start with + and country code.")
        return 1

    _save_env(api_id, api_hash, phone)
    print(f"\nSaved credentials to {ENV_FILE}.\n")

    # Lazy import — Telethon brings rsa/pyaes/etc. Only need them here.
    from telethon import TelegramClient

    client = TelegramClient(str(SESSION_FILE), int(api_id), api_hash)
    print("Connecting to Telegram...")
    try:
        client.start(phone=lambda: phone)
    except Exception as e:
        print(f"\nLogin failed: {e}")
        return 1

    me = client.loop.run_until_complete(client.get_me())
    print(f"\n✓ Logged in as: {me.first_name or ''} (@{me.username or 'no-username'})")
    print(f"✓ Session saved to: {SESSION_FILE}.session")
    print("\nYou're set. Next: pythonw tools/outbox/app.py")
    client.disconnect()
    return 0


if __name__ == "__main__":
    sys.exit(main())
