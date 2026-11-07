"""
Pings to the user's phone when something is waiting for them — through the
status bot on Telegram (STATUS_BOT_TOKEN in the Pulse folder's .env, chat id
from data/status_chat_id.txt, which status_bot.py saves on /start).

  - a code change or a sorting plan needs their approval
  - it's their turn in a House Project, or a step is waiting for their go-ahead
  - someone passed on a step, or a project finished

Turn off with data/notify_settings.json -> {"enabled": false}.
Never blocks: sends from a background thread, and the same event is never
sent twice.
"""

import json
import logging
import threading
import urllib.parse
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)

PULSE_ROOT = Path(__file__).resolve().parent.parent
SETTINGS = PULSE_ROOT / "data" / "notify_settings.json"
SENT_LOG = PULSE_ROOT / "data" / "notify_sent.json"
CHAT_ID_FILE = PULSE_ROOT / "data" / "status_chat_id.txt"
_lock = threading.Lock()
TEST_MODE = False          # tests flip this so nothing is ever really sent
sent_in_test: list = []


def enabled() -> bool:
    try:
        return bool(json.loads(SETTINGS.read_text(encoding="utf-8")).get("enabled", True))
    except Exception:
        return True


def _token() -> str | None:
    try:
        from dotenv import dotenv_values
        return dotenv_values(PULSE_ROOT / ".env").get("STATUS_BOT_TOKEN") or None
    except Exception:
        return None


def _seen(key: str) -> bool:
    """Remember the key; True if it was already sent before."""
    with _lock:
        try:
            keys = json.loads(SENT_LOG.read_text(encoding="utf-8"))
        except Exception:
            keys = []
        if key in keys:
            return True
        keys = (keys + [key])[-500:]
        try:
            SENT_LOG.write_text(json.dumps(keys), encoding="utf-8")
        except Exception:
            pass
        return False


def _send(text: str):
    token = _token()
    try:
        chat_id = CHAT_ID_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        chat_id = ""
    if not token or not chat_id:
        return
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
    try:
        urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", data=data, timeout=10).read()
    except Exception as e:
        logger.warning(f"notify: couldn't ping the user ({type(e).__name__})")


def ping_user(text: str, key: str | None = None) -> bool:
    """Send text to the user's phone (once per key). Returns True if queued."""
    import sys
    if not enabled():
        return False
    if TEST_MODE or "unittest" in sys.modules:      # tests never buzz a real phone
        sent_in_test.append(text)
        return True
    if key and _seen(key):
        return False
    threading.Thread(target=_send, args=(text,), daemon=True).start()
    return True


