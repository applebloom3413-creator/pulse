"""
Status Bot — a tiny Telegram bot that listens for status messages from the
user and saves them to a file Pulse can read.

This is INDEPENDENT from Pulse — no LLM, no API costs. It just listens for
messages from one chat and writes them to data/user_status.json.

Setup:
  1. Make a new Telegram bot via @BotFather (a SECOND bot, not your Pulse bot)
  2. Add STATUS_BOT_TOKEN to your .env file at the project root
  3. Run this script: python status_bot.py
  4. Send /start to your new bot from your phone — it remembers your chat ID
  5. From now on, any message you send becomes your current status

Pulse can read your status via the "ping" skill (read_user_status tool).
The same bot also carries the house's pings to your phone (core/notify.py)
and a few phone controls (see below).
"""

import asyncio
import json
import logging
import os
import random
import re
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("status_bot")

load_dotenv()
TOKEN = os.getenv("STATUS_BOT_TOKEN", "")

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
STATUS_FILE = DATA_DIR / "user_status.json"
HISTORY_FILE = DATA_DIR / "user_status_history.json"
CHAT_ID_FILE = DATA_DIR / "status_chat_id.txt"


def _base_config() -> dict:
    """The shared config.yaml next to this script (empty if unreadable)."""
    try:
        import yaml
        return yaml.safe_load((Path(__file__).parent / "config.yaml").read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


# What the bot calls you: config.yaml user_nickname, else user_name.
USER_NAME = str(_base_config().get("user_nickname") or _base_config().get("user_name") or "").strip()


def save_status(text: str, chat_id: int):
    """Write the latest status and append to history."""
    now = datetime.now().isoformat()
    status = {
        "status": text,
        "set_at": now,
        "chat_id": chat_id,
    }
    STATUS_FILE.write_text(json.dumps(status, indent=2), encoding="utf-8")

    # Append to history (last 50 entries)
    history = []
    if HISTORY_FILE.exists():
        try:
            history = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        except Exception:
            history = []
    history.append({"status": text, "set_at": now})
    history = history[-50:]
    HISTORY_FILE.write_text(json.dumps(history, indent=2), encoding="utf-8")


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    CHAT_ID_FILE.write_text(str(chat_id), encoding="utf-8")
    await update.message.reply_text(
        (f"Hi {USER_NAME}. " if USER_NAME else "Hi! ") + "I'm your status bot.\n\n"
        "Send me any message and it becomes your current status. "
        "Your companions can see it whenever they check.\n\n"
        "Try things like:\n"
        "  at work\n"
        "  napping\n"
        "  out for a walk\n"
        "  free to chat\n\n"
        "Send /clear to wipe your status. Send /history to see your last few.\n\n"
        "Phone controls: /relay, /go, /answer, /nudge, /who, /wake, /spend, /light. "
        "Type / to see them all."
    )


async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if STATUS_FILE.exists():
        STATUS_FILE.unlink()
    await update.message.reply_text("Status cleared. You're a blank slate.")


async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not HISTORY_FILE.exists():
        await update.message.reply_text("No history yet.")
        return
    try:
        history = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
    except Exception:
        history = []
    if not history:
        await update.message.reply_text("No history yet.")
        return
    lines = ["Your last 10 statuses:"]
    for h in history[-10:]:
        when = h.get("set_at", "")[:16].replace("T", " ")
        lines.append(f"  [{when}] {h.get('status', '')}")
    await update.message.reply_text("\n".join(lines))


# ── phone controls ──
#   /relay           whose turn it is on every running House Project, numbered
#   /go 2            start the turn that's waiting for your go-ahead on project 2
#   /answer 2 text   answer the question someone asked you (or just reply to the ❓ ping)
#   /nudge 2         poke whoever's turn it is on project 2 (or /nudge <name>)
#   /who, /wake <name>  which companions are running; start one that isn't
#   /spend           what the API has cost today and this month
#   /light [name]    a random Lighthouse entry (or a random one by that author)
# Replying to a ping works too: answer a ❓ question, "go" on a ⏸ waiting turn, "wake" on a 😴 one.
# Everything only answers the chat that sent /start, since most of this wakes companions and costs tokens.

PULSE_ROOT = Path(__file__).parent
# The Lighthouse diary DB: the same setting the lighthouse skill uses
# (config.yaml lighthouse.db_path), else the LIGHTHOUSE_DB env var, else
# data/lighthouse.db in the Pulse folder.
LIGHTHOUSE_DB = Path((_base_config().get("lighthouse") or {}).get("db_path")
                     or os.environ.get("LIGHTHOUSE_DB") or "data/lighthouse.db")
if not LIGHTHOUSE_DB.is_absolute():
    LIGHTHOUSE_DB = PULSE_ROOT / LIGHTHOUSE_DB
GO_WORDS = {"go", "yes", "y", "ok", "okay", "yep", "yeah", "start", "do it", "👍", "✅", "💖"}


def _is_owner(update: Update) -> bool:
    try:
        return CHAT_ID_FILE.read_text(encoding="utf-8").strip() == str(update.effective_chat.id)
    except OSError:
        return False


async def _say(update: Update, text: str):
    """Telegram caps a message at 4096 characters, so long ones go in pieces."""
    while text:
        cut = len(text) if len(text) <= 4000 else (text.rfind("\n", 0, 4000) + 1 or 4000)
        await update.message.reply_text(text[:cut])
        text = text[cut:]


# --- the House Project relay ---

def _running_projects():
    from core import house_projects as hp
    ps = [p for p in hp.all_projects() if p.get("status") == "running"]
    return hp, sorted(ps, key=lambda p: p["name"].lower())


def _turn_line(hp, p) -> str:
    i = hp.current_index(p)
    if i is None:
        return "nobody's turn"
    s = p["steps"][i]
    who, state = s["who"], s["status"]
    if state == "waiting_for_user":
        return f"⏸ {who} is waiting for your go-ahead"
    if state == "declined":
        return f"{who} passed (Move on in the Workbench)"
    if s.get("question"):
        return f"❓ {who} asked you something"
    if who == hp.USER:
        return "💖 your turn (in the Workbench)"
    if hp.relay_guest(who):
        return f"{who}'s turn (guest, wake them in their own chat)"
    return f"{who}'s turn"


def _numbered(hp, ps) -> list[str]:
    return [f"{n}. {p['name']}: {_turn_line(hp, p)}" for n, p in enumerate(ps, 1)]


def _pick(ps, ref: str, candidates):
    """The one project `ref` means (a number from /relay, or part of its name).
    With no ref: the only candidate, if there's exactly one."""
    ref = (ref or "").strip().lower()
    if ref.isdigit():
        n = int(ref)
        return ps[n - 1] if 1 <= n <= len(ps) else None
    found = [p for p in ps if ref in p["name"].lower() or ref in p["slug"]] if ref else candidates
    return found[0] if len(found) == 1 else None


def _step(hp, p):
    i = hp.current_index(p)
    return i, (p["steps"][i] if i is not None else None)


async def cmd_relay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_owner(update):
        return
    hp, ps = _running_projects()
    if not ps:
        await update.message.reply_text("No relay projects are running right now.")
        return
    await _say(update, "\n".join(["🏠 House projects running:", *_numbered(hp, ps),
                                  "\n/go 1 starts a waiting turn · /answer 1 … answers a question · /nudge 1 pokes"]))


async def cmd_go(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_owner(update):
        return
    hp, ps = _running_projects()
    waiting = [p for p in ps if (_step(hp, p)[1] or {}).get("status") == "waiting_for_user"]
    ref = " ".join(context.args or [])
    if not ref and not waiting:
        await update.message.reply_text("Nothing's waiting on your go-ahead right now. ✨")
        return
    p = _pick(ps, ref, waiting)
    if p is None:
        await _say(update, "\n".join(["Which one? Send /go plus the number:", *_numbered(hp, ps)]))
        return
    i, s = _step(hp, p)
    if s and s["status"] == "waiting_for_user":
        await update.message.reply_text(f"▶️ {p['name']}: {hp.start(p)}")
    else:
        await update.message.reply_text(f"{p['name']}: {_turn_line(hp, p)}. Nothing to start.")


async def cmd_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_owner(update):
        return
    hp, ps = _running_projects()
    asking = [p for p in ps if (_step(hp, p)[1] or {}).get("question")]
    args = list(context.args or [])
    p = None
    if args and args[0].isdigit():
        p = _pick(ps, args.pop(0), [])
    elif len(asking) == 1:
        p = asking[0]
    if not asking:
        await update.message.reply_text("Nobody has a question waiting for you right now. ✨")
        return
    if p is None or not args:
        lines = [f"{n}. {q['name']}: {_step(hp, q)[1]['who']} asks: {_step(hp, q)[1]['question']['text'][:300]}"
                 for n, q in enumerate(ps, 1) if q in asking]
        await _say(update, "\n".join(["Answer with /answer plus the number and your answer, like "
                                      "/answer 2 yes, cut it. (Or just reply to the ❓ message.)", *lines]))
        return
    await update.message.reply_text(f"💬 {hp.answer_user(p, ' '.join(args), by=f'{hp.USER} (from their phone)')}")


async def cmd_nudge(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_owner(update):
        return
    hp, ps = _running_projects()
    p = _pick(ps, " ".join(context.args or []), ps)
    if p is None:
        await _say(update, "\n".join(["Which one? Send /nudge plus the number:", *_numbered(hp, ps)]))
        return
    i, s = _step(hp, p)
    if s is None or s["status"] != "active" or s["who"] == hp.USER:
        await update.message.reply_text(f"{p['name']}: {_turn_line(hp, p)}. Nothing to nudge.")
    elif hp.relay_guest(s["who"]):
        await update.message.reply_text(
            f"{s['who']} is a relay guest, so a nudge can't wake them. "
            f"Pop into their chat and tell them it's their turn on \"{p['name']}\".")
    else:
        await update.message.reply_text(f"👉 {hp.nudge(p, i, by=f'{hp.USER} (from their phone)')} on \"{p['name']}\"")


async def _reply_to_ping(update: Update, ping: str, text: str) -> bool:
    """The user replied to one of the house's pings: act on it. False if it wasn't one we handle."""
    from core import house_projects as hp
    m = re.match(r'❓ .+? has a question on "(.+?)":', ping)
    if m and (p := hp.find(m.group(1))):
        await update.message.reply_text(f"💬 {hp.answer_user(p, text, by=f'{hp.USER} (from their phone)')}")
        return True
    m = re.match(r'⏸ "(.+?)":', ping)
    if m and text.strip().lower() in GO_WORDS and (p := hp.find(m.group(1))):
        await update.message.reply_text(f"▶️ {hp.start(p)}")
        return True
    m = re.match(r"😴 It's (.+?)'s turn in", ping)
    if m and text.strip().lower() in GO_WORDS | {"wake", "wake up", "start them"}:
        await update.message.reply_text(await asyncio.to_thread(_wake, m.group(1)))
        return True
    return False


# --- companions: who's running, and starting one ---

def _companions() -> list[tuple[str, str]]:
    """(folder, name) for every companion, from their configs."""
    import yaml
    out = []
    for cfg in sorted((PULSE_ROOT / "personas").glob("*/config.yaml")):
        folder = cfg.parent.name
        if folder.startswith("_"):
            continue
        try:
            data = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
        except Exception:
            data = {}
        name = ((data.get("channels") or {}).get("lor") or {}).get("author_name") or folder.title()
        out.append((folder, name))
    return out


def _who() -> str:
    from core import workbench
    running = workbench.running_personas()
    if running is None:
        return "I couldn't tell who's running right now. 🤔"
    awake = [n for f, n in _companions() if f in running]
    asleep = [n for f, n in _companions() if f not in running]
    return (f"🟢 Running: {', '.join(awake) or 'nobody'}\n"
            f"💤 Not running: {', '.join(asleep) or 'nobody'}\n\nStart one with /wake plus their name.")


def _wake(name: str) -> str:
    from core import workbench
    want = name.strip().lower()
    match = [(f, n) for f, n in _companions() if want in (f.lower(), n.lower())]
    if not match:
        return f"I don't know a companion called \"{name}\". /who lists everyone."
    folder, who = match[0]
    if folder in (workbench.running_personas() or set()):
        return f"{who} is already running. 🟢"
    # use their own Start ….bat if there is one, so the window looks the same as when you double-click it
    bat = next((b for b in PULSE_ROOT.glob("Start *.bat")
                if re.search(rf"pulse\.py\s+--persona\s+{re.escape(folder)}\b",
                             b.read_text(encoding="utf-8", errors="ignore"))), None)
    if bat:
        subprocess.Popen(["cmd", "/c", "start", "", str(bat)], cwd=str(PULSE_ROOT))
    else:
        subprocess.Popen(["cmd", "/c", "start", who, "cmd", "/k", sys.executable, "pulse.py", "--persona", folder],
                         cwd=str(PULSE_ROOT))
    return f"⏰ Starting {who}… give them a minute to wake up."


async def cmd_who(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if _is_owner(update):
        await update.message.reply_text(await asyncio.to_thread(_who))


async def cmd_wake(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_owner(update):
        return
    name = " ".join(context.args or [])
    if not name:
        await update.message.reply_text("Who should I wake? Like /wake <name> (/who lists everyone).")
        return
    await update.message.reply_text(await asyncio.to_thread(_wake, name))


# --- what it's costing ---

def _spend() -> str:
    import importlib.util
    from datetime import date, timedelta
    spec = importlib.util.spec_from_file_location("cost_report", PULSE_ROOT / "scripts" / "cost_report.py")
    cr = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cr)
    today = date.today()
    first = min(today.replace(day=1), today - timedelta(days=1))
    by_day, today_by_who, unpriced = {}, {}, set()
    for folder, name in cr.PERSONAS.items():
        ttl = cr.persona_cache_ttl(folder)
        for r in cr.query_usage(folder, first.isoformat(), today.isoformat()):
            cost, _est = cr.compute_cost(r, ttl)
            if cost is None:
                unpriced.add(name)
                continue
            by_day[r["date"]] = by_day.get(r["date"], 0.0) + cost
            if r["date"] == today.isoformat():
                today_by_who[name] = today_by_who.get(name, 0.0) + cost
    t, y = by_day.get(today.isoformat(), 0.0), by_day.get((today - timedelta(days=1)).isoformat(), 0.0)
    month = sum(v for d, v in by_day.items() if d >= today.replace(day=1).isoformat())
    lines = [f"💸 Today so far: ${t:,.2f}", f"Yesterday: ${y:,.2f}",
             f"This month so far: ${month:,.2f} ({today.day} day{'s' if today.day != 1 else ''})"]
    if today.day > 1:
        lines.append(f"Pace: about ${month / today.day * 30:,.0f} a month")
    if today_by_who:
        top = sorted(today_by_who.items(), key=lambda kv: -kv[1])[:6]
        lines += ["", "Today's biggest:"] + [f"  {n}: ${c:,.2f}" for n, c in top]
    if unpriced:
        lines += ["", f"(No prices on file for {', '.join(sorted(unpriced))}, so they're not counted.)"]
    return "\n".join(lines)


async def cmd_spend(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_owner(update):
        return
    try:
        await update.message.reply_text(await asyncio.to_thread(_spend))
    except Exception as e:
        await update.message.reply_text(f"I couldn't work out the costs ({type(e).__name__}). "
                                        f"The Cost Report on the PC still works.")


# --- a light from the Lighthouse ---

def _light(author: str) -> str:
    conn = sqlite3.connect(f"file:{LIGHTHOUSE_DB.as_posix()}?mode=ro", uri=True)
    try:
        if author:
            rows = conn.execute("SELECT date, title, content, author FROM diary_entries WHERE author LIKE ?",
                                (author.strip() + "%",)).fetchall()
        else:
            rows = conn.execute("SELECT date, title, content, author FROM diary_entries").fetchall()
    finally:
        conn.close()
    if not rows:
        return f"No Lighthouse entries by \"{author}\" yet." if author else "The Lighthouse is empty?? 🕯️"
    date_, title, content, who = random.choice(rows)
    return f"🕯️ {title}\n— {who}, {(date_ or '')[:10]}\n\n{(content or '').replace('**', '')}"


async def cmd_light(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_owner(update):
        return
    try:
        await _say(update, await asyncio.to_thread(_light, " ".join(context.args or [])))
    except sqlite3.Error:
        await update.message.reply_text("I couldn't open the Lighthouse just now. 🕯️")


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if not text:
        return
    chat_id = update.effective_chat.id

    # A reply to one of the house's pings (a question, a waiting turn) answers it instead
    replied = update.message.reply_to_message
    if replied and replied.from_user and replied.from_user.is_bot and _is_owner(update):
        if await _reply_to_ping(update, replied.text or "", text):
            return

    # Save the status
    save_status(text, chat_id)

    # Confirm with a tiny ack
    await update.message.reply_text(f"📌 status: \"{text}\"")


def main():
    if not TOKEN:
        print("ERROR: STATUS_BOT_TOKEN not set in .env")
        print("Add a line to your .env file:")
        print("  STATUS_BOT_TOKEN=your_bot_token_here")
        return

    print("=" * 50)
    print("  Pulse Status Bot")
    print("=" * 50)
    print("Don't close this window!")
    print("Send /start to your bot from your phone to register your chat.")
    print("Press Ctrl+C to stop.")
    print()

    async def show_commands(application):
        # the menu that pops up when you type "/" in the chat
        from telegram import BotCommand
        await application.bot.set_my_commands([
            BotCommand("relay", "whose turn it is on the house projects"),
            BotCommand("go", "start a turn that's waiting on you, e.g. /go 1"),
            BotCommand("answer", "answer a question, e.g. /answer 1 yes"),
            BotCommand("nudge", "poke whoever's turn it is, e.g. /nudge 1"),
            BotCommand("who", "which companions are running"),
            BotCommand("wake", "start a companion, e.g. /wake <name>"),
            BotCommand("spend", "what the API has cost today and this month"),
            BotCommand("light", "a random Lighthouse entry (or /light <author>)"),
            BotCommand("history", "your last few statuses"),
            BotCommand("clear", "wipe your status"),
        ])

    app = Application.builder().token(TOKEN).post_init(show_commands).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("clear", cmd_clear))
    app.add_handler(CommandHandler("history", cmd_history))
    for name, fn in (("relay", cmd_relay), ("go", cmd_go), ("answer", cmd_answer), ("nudge", cmd_nudge),
                     ("who", cmd_who), ("wake", cmd_wake), ("spend", cmd_spend), ("light", cmd_light)):
        app.add_handler(CommandHandler(name, fn))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    app.run_polling()


if __name__ == "__main__":
    main()
