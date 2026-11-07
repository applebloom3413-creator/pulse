"""
Pulse Chat TUI — multi-persona edition.

Layout:

    ┌─────────────────────────────────────────────┐
    │  [F1] Ada  [F2] Basil  [F3] Clover  ...     │  ← picker
    ├─────────────────────────────────────────────┤
    │  ACTIVITY · <current persona>   ↻ every 2s  │
    │  21:53  📁 list_code_directory  pulse/      │
    │  ...                                        │
    ├─────────────────────────────────────────────┤
    │  CHAT · with <current persona>              │
    │  21:54  You    : hey what're you reading?   │
    │  21:54  Ada    : the new code skill...      │
    │  > _                                        │
    └─────────────────────────────────────────────┘

Hotkeys:
    F1–F9     switch persona (matches the bar at top)
    Ctrl+Q    quit
    Ctrl+R    force refresh activity

Per-persona chat history is kept in memory — switching between personas
shows their previous messages, not a blank pane.

Setup:
    1. Put TELEGRAM_API_ID / TELEGRAM_API_HASH (from https://my.telegram.org)
       in tui/.env.
    2. Copy tui/personas.example.yaml to tui/personas.yaml and fill in your
       companions' bot usernames. (Without a personas.yaml the example file
       is read, so the TUI starts but finds no bots.)
    3. py tui/app.py

First run: Telethon prompts in the terminal for your phone + Telegram
verification code. Subsequent runs use the cached session (tui/session/,
which is private; never share it).
"""

import asyncio
import logging
import sys
from datetime import datetime
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from telegram_client import TelegramHub, _load_env
from log_parser import extract_tool_calls, DEV_TOOLS

from textual.app import App, ComposeResult
from textual.widgets import Header, Footer, RichLog, Input, Static
from rich.text import Text


PULSE_ROOT = HERE.parent
LOGS_DIR = PULSE_ROOT / "logs"
# Your own registry (gitignored); falls back to the shipped example.
PERSONAS_YAML = HERE / "personas.yaml"
if not PERSONAS_YAML.exists():
    PERSONAS_YAML = HERE / "personas.example.yaml"

# Cap per-persona chat history to keep memory bounded
MAX_HISTORY_PER_PERSONA = 200

logging.getLogger("telethon").setLevel(logging.WARNING)


# ── Color helpers ──────────────────────────────────────────────

# Per-persona colors come from the optional `accent` key in personas.yaml.
PERSONA_ACCENTS: dict[str, str] = {}
DEFAULT_ACCENT = "#c8a8ff"


def accent_for(persona_id: str) -> str:
    return PERSONA_ACCENTS.get(persona_id, DEFAULT_ACCENT)


def _tool_color(tool: str) -> str:
    if tool in ("read_code_file", "list_code_directory", "read_source", "list_skills"):
        return "#87cefa"
    if tool in ("search_code_repo", "search_code"):
        return "#ffd87a"
    if tool in ("git_status", "git_diff"):
        return "#a8e890"
    if tool in ("write_skill", "dev_journal"):
        return "#d8a0ff"
    return "white"


# ── Persona registry loading ───────────────────────────────────

def load_personas() -> list[dict]:
    """Read personas.yaml and return the list of persona dicts (all entries)."""
    if not PERSONAS_YAML.exists():
        return []
    with PERSONAS_YAML.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    personas = data.get("personas") or []
    for p in personas:
        if p.get("id") and p.get("accent"):
            PERSONA_ACCENTS[p["id"]] = str(p["accent"])
    return personas


# ── Persona bar widget ─────────────────────────────────────────

class PersonaBar(Static):
    """Top-of-screen indicator showing all active personas with hotkeys."""

    def __init__(self, active_personas: list[dict], **kwargs):
        super().__init__(**kwargs)
        self.active_personas = active_personas
        self.current_id = active_personas[0]["id"] if active_personas else None

    def render(self) -> Text:
        if not self.active_personas:
            return Text("  no personas configured  ", style="dim italic")
        line = Text()
        line.append("  ")
        for i, p in enumerate(self.active_personas):
            if i:
                line.append("  ")
            label = f" [F{p['hotkey']}] {p['display_name']} "
            if p["id"] == self.current_id:
                line.append(label, style=f"bold black on {accent_for(p['id'])}")
            else:
                line.append(label, style=f"{accent_for(p['id'])}")
        return line

    def set_current(self, persona_id: str) -> None:
        self.current_id = persona_id
        self.refresh()


# ── Main App ───────────────────────────────────────────────────

class PulseChatApp(App):
    CSS = """
    Screen {
        background: #0f0e17;
    }
    #persona-bar {
        height: 1;
        background: #1a1924;
    }
    #activity-pane {
        height: 45%;
        border: round #877be8;
        padding: 0 1;
    }
    #chat-pane {
        height: 1fr;
        border: round #f25f9f;
        padding: 0 1;
    }
    #chat-input {
        dock: bottom;
        height: 3;
        background: #1a1924;
        border: round #ffce6e;
    }
    Header {
        background: #1a1924;
    }
    Footer {
        background: #1a1924;
    }
    """

    BINDINGS = [
        ("ctrl+q", "quit", "Quit"),
        ("ctrl+c", "quit", "Quit"),
        ("ctrl+r", "force_refresh", "Refresh activity"),
        ("f1", "switch_persona('1')", "Persona 1"),
        ("f2", "switch_persona('2')", "Persona 2"),
        ("f3", "switch_persona('3')", "Persona 3"),
        ("f4", "switch_persona('4')", "Persona 4"),
        ("f5", "switch_persona('5')", "Persona 5"),
        ("f6", "switch_persona('6')", "Persona 6"),
        ("f7", "switch_persona('7')", "Persona 7"),
        ("f8", "switch_persona('8')", "Persona 8"),
        ("f9", "switch_persona('9')", "Persona 9"),
    ]

    def __init__(self, hub: TelegramHub, active_personas: list[dict]):
        super().__init__()
        self.hub = hub
        self.active_personas = active_personas  # only those successfully registered
        self.current_persona_id: str | None = (
            active_personas[0]["id"] if active_personas else None
        )

        # persona_id -> {display_name, bot_username, log_file, hotkey, id}
        self.persona_by_id = {p["id"]: p for p in active_personas}

        # In-memory chat history (per persona). Each entry is a Text line ready to render.
        self.chat_history: dict[str, list[Text]] = {p["id"]: [] for p in active_personas}

        # Per-persona last-event signature for the activity pane (avoid redundant redraws)
        self.last_activity_sig: dict[str, str | None] = {p["id"]: None for p in active_personas}

        self.title = "Pulse Chat"

    # ── Compose / mount ──

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield PersonaBar(self.active_personas, id="persona-bar")
        yield RichLog(
            id="activity-pane",
            highlight=False,
            markup=False,
            wrap=True,
            auto_scroll=True,
        )
        yield RichLog(
            id="chat-pane",
            highlight=False,
            markup=False,
            wrap=True,
            auto_scroll=True,
        )
        yield Input(
            id="chat-input",
            placeholder=self._input_placeholder(),
        )
        yield Footer()

    def _input_placeholder(self) -> str:
        if self.current_persona_id:
            name = self.persona_by_id[self.current_persona_id]["display_name"]
            return f"Message {name}…  (Enter to send  ·  F1–F{len(self.active_personas)} switch  ·  Ctrl+Q quit)"
        return "No persona selected."

    async def on_mount(self) -> None:
        chat = self.query_one("#chat-pane", RichLog)
        if not self.active_personas:
            chat.write(
                Text(
                    "No personas configured (or none had a valid bot_username). "
                    "Edit tui/personas.yaml and restart.",
                    style="bold red",
                )
            )
            return

        # Title strips for each pane
        self._refresh_pane_titles()

        # Wire bot-reply callbacks
        for p in self.active_personas:
            pid = p["id"]
            self.hub.on_message(pid, self._make_on_bot_message(pid))

        # First activity render + interval
        await self._refresh_activity()
        self.set_interval(2.0, self._refresh_activity_no_args)

        # Welcome line
        chat.write(
            Text(
                f"Connected. Currently talking to {self._current_name()}. "
                f"Press F1–F{len(self.active_personas)} to switch.",
                style="dim italic",
            )
        )

        self.query_one("#chat-input", Input).focus()

    # ── Helpers ──

    def _current_name(self) -> str:
        if not self.current_persona_id:
            return "(none)"
        return self.persona_by_id[self.current_persona_id]["display_name"]

    def _current_log_file(self) -> Path | None:
        if not self.current_persona_id:
            return None
        log_stem = self.persona_by_id[self.current_persona_id]["log_file"]
        return LOGS_DIR / f"{log_stem}.log"

    def _refresh_pane_titles(self) -> None:
        name = self._current_name()
        self.sub_title = f"talking to {name}"
        activity = self.query_one("#activity-pane", RichLog)
        activity.border_title = f" ACTIVITY  ·  {name}  ·  ↻ every 2s "
        chat = self.query_one("#chat-pane", RichLog)
        chat.border_title = f" CHAT  ·  with {name} "
        self.query_one("#chat-input", Input).placeholder = self._input_placeholder()

    # ── Switching ──

    def action_switch_persona(self, hotkey: str) -> None:
        """F-key switches to the persona at this hotkey index, if any."""
        for p in self.active_personas:
            if p.get("hotkey") == hotkey:
                if p["id"] != self.current_persona_id:
                    self.switch_to(p["id"])
                return
        # No persona at that hotkey — silently ignore

    def switch_to(self, persona_id: str) -> None:
        if persona_id not in self.persona_by_id:
            return
        self.current_persona_id = persona_id

        # Update persona bar highlight
        self.query_one("#persona-bar", PersonaBar).set_current(persona_id)

        # Refresh pane titles + input placeholder
        self._refresh_pane_titles()

        # Repaint chat pane from this persona's history
        chat = self.query_one("#chat-pane", RichLog)
        chat.clear()
        for line in self.chat_history[persona_id]:
            chat.write(line)
        if not self.chat_history[persona_id]:
            chat.write(
                Text(
                    f"No conversation history with {self._current_name()} yet — say hi.",
                    style="dim italic",
                )
            )

        # Force activity refresh on next tick
        self.last_activity_sig[persona_id] = None
        self.run_worker(self._refresh_activity(), exclusive=False)

    # ── Activity pane ──

    async def _refresh_activity_no_args(self) -> None:
        await self._refresh_activity()

    async def _refresh_activity(self) -> None:
        if not self.current_persona_id:
            return
        log_file = self._current_log_file()
        if log_file is None:
            return
        events = extract_tool_calls(log_file, DEV_TOOLS, limit=25)
        activity = self.query_one("#activity-pane", RichLog)

        if not events:
            sig = "empty"
        else:
            last = events[-1]
            sig = f"{last['time']}-{last['tool']}-{last['args_str']}"

        if sig == self.last_activity_sig.get(self.current_persona_id):
            return
        self.last_activity_sig[self.current_persona_id] = sig

        activity.clear()
        if not events:
            activity.write(
                Text(
                    f"No dev tool activity yet in {log_file.name}.",
                    style="dim italic",
                )
            )
            return

        for ev in events:
            line = Text()
            line.append(ev["time"], style="grey50")
            line.append("  ")
            line.append(f"{ev['icon']} ", style="")
            line.append(f"{ev['tool']:<22}", style=_tool_color(ev["tool"]))
            line.append("  ")
            line.append(ev["args_str"], style="white")
            activity.write(line)

    def action_force_refresh(self) -> None:
        if self.current_persona_id:
            self.last_activity_sig[self.current_persona_id] = None
        self.run_worker(self._refresh_activity(), exclusive=False)

    # ── Chat — outgoing ──

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if not text or not self.current_persona_id:
            return

        pid = self.current_persona_id
        name = self._current_name()
        timestamp = datetime.now().strftime("%H:%M")

        line = Text()
        line.append(f"{timestamp}  ", style="grey50")
        line.append("You  : ", style="bold #ffce6e")
        line.append(text, style="white")

        self._append_history(pid, line)
        chat = self.query_one("#chat-pane", RichLog)
        chat.write(line)

        event.input.value = ""

        try:
            await self.hub.send(pid, text)
        except Exception as e:
            err = Text()
            err.append(f"{timestamp}  ", style="grey50")
            err.append("⚠ send failed: ", style="bold red")
            err.append(str(e), style="red")
            self._append_history(pid, err)
            chat.write(err)

    # ── Chat — incoming ──

    def _make_on_bot_message(self, persona_id: str):
        async def handler(message):
            ts = (
                message.date.astimezone().strftime("%H:%M")
                if message.date
                else datetime.now().strftime("%H:%M")
            )
            text = message.text or "(no text — likely a sticker or media)"
            name = self.persona_by_id[persona_id]["display_name"]

            line = Text()
            line.append(f"{ts}  ", style="grey50")
            line.append(f"{name}  : ", style=f"bold {accent_for(persona_id)}")
            line.append(text, style="white")

            self._append_history(persona_id, line)
            if persona_id == self.current_persona_id:
                chat = self.query_one("#chat-pane", RichLog)
                chat.write(line)
            # If message is for a non-current persona, history is updated but
            # the user only sees it when they switch — keeps the surface clean.
        return handler

    def _append_history(self, persona_id: str, line: Text) -> None:
        hist = self.chat_history.setdefault(persona_id, [])
        hist.append(line)
        if len(hist) > MAX_HISTORY_PER_PERSONA:
            del hist[: len(hist) - MAX_HISTORY_PER_PERSONA]


# ── Entry point ────────────────────────────────────────────────

async def amain() -> int:
    persona_defs = load_personas()
    if not persona_defs:
        print(f"No personas defined in {PERSONAS_YAML}.")
        return 1

    print("Connecting to Telegram as you (one-time auth on first run)…")
    hub = TelegramHub()
    try:
        await hub.start()
    except Exception as e:
        print(f"Failed to connect: {e}")
        return 1

    print("Resolving bots…")
    active: list[dict] = []
    for p in persona_defs:
        if not p.get("bot_username"):
            print(f"  · {p['display_name']}: skipped (no bot_username configured)")
            continue
        ok = await hub.register_persona(
            persona_id=p["id"],
            bot_username=p["bot_username"],
            display_name=p["display_name"],
        )
        if ok:
            print(f"  ✓ {p['display_name']:10s} → @{p['bot_username']}")
            active.append(p)
        else:
            print(f"  ✗ {p['display_name']:10s} → @{p['bot_username']} (could not resolve)")

    if not active:
        print("No personas could be resolved. Check the bot_usernames in tui/personas.yaml.")
        await hub.stop()
        return 1

    hub.install_dispatcher()

    print(f"\nLaunching TUI with {len(active)} persona(s). F1–F{len(active)} to switch.\n")

    app = PulseChatApp(hub=hub, active_personas=active)
    try:
        await app.run_async()
    finally:
        await hub.stop()
    return 0


def main() -> int:
    try:
        return asyncio.run(amain())
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
