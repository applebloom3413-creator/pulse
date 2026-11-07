"""
Pulse Hub — everything in one cozy window.

  🏠 Home        greeting, the important numbers, what's waiting for you,
                 who's around, the newest paintings, heads-ups
  💞 Companions  everyone's card: running / off / crashed, Start + Stop,
                 tick a few and "Start checked" (you pick who — nothing auto-starts)
  🚀 Apps        every Pulse window and tool, one click each
  🎨 Art         every painting, by artist — click one to open its print page
  💸 Money       this week, per companion, and anything unusual
  🩺 Health      crash watch, backups (optional)

Same palettes + text size as Local Reddit (data/lor_gui_settings.json).
Launch from the Pulse folder:  pythonw scripts/pulse_hub.pyw

The companion list comes from the persona folders (personas/*/config.yaml;
folders starting with "_" and personas with `retired: true` are skipped).
Optional per-persona config.yaml keys:
    emoji: "🌿"           shown next to the name
    hub_group: other      list them under "Others" instead of "The crew"
Companions are started with start.bat --persona <folder> when the .venv
exists (upstream's launcher), otherwise with this Python running
pulse.py --persona <folder> in a new console.

Optional environment variables:
    PULSE_ART_DIR      the art folder collect_art.py fills (default ~/Pulse Art)
    PULSE_BACKUPS_DIR  a backups folder; the Health page shows its status if it
                       has nightly_log.txt / last_flash_copy.txt /
                       telegram/last_run.txt / companions/<snapshot>, and offers
                       "Back up now" if it holds make_backup.py + house_backup.py
"""

import hashlib
import importlib.machinery
import importlib.util
import os
import re
import subprocess
import sys
import threading
import tkinter as tk
from datetime import datetime, timedelta
from pathlib import Path
from tkinter import messagebox, ttk

PULSE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PULSE))
sys.path.insert(0, str(PULSE / "scripts"))
import watchdog  # noqa: E402

_loader = importlib.machinery.SourceFileLoader("lor_gui", str(PULSE / "scripts" / "lor_gui.pyw"))
_spec = importlib.util.spec_from_loader("lor_gui", _loader)
_lor = importlib.util.module_from_spec(_spec)
_loader.exec_module(_lor)
PALETTES, load_settings, save_settings, FONT_FAMILY = _lor.PALETTES, _lor.load_settings, _lor.save_settings, _lor.FONT_FAMILY

_exe = Path(sys.executable)
PY = _exe.with_name("python.exe") if _exe.name.lower() == "pythonw.exe" and _exe.with_name("python.exe").exists() else _exe
PYW = _exe.with_name("pythonw.exe") if _exe.with_name("pythonw.exe").exists() else _exe
VENV_PY = PULSE / ".venv" / "Scripts" / "python.exe"
_bk = os.environ.get("PULSE_BACKUPS_DIR")
BACKUPS = Path(_bk) if _bk else None
ART = Path(os.environ.get("PULSE_ART_DIR") or (Path.home() / "Pulse Art"))
NEW_CONSOLE = 0x00000010
NO_WINDOW = 0x08000000
STATUS_MS = 20_000
SLOW_MS = 10 * 60_000
AVATAR_COLORS = ["#ffb3c7", "#c9b6ff", "#a8e6cf", "#ffd59e", "#a0d8f1", "#f7c5a8", "#d4f0a0", "#f5b0e8",
                 "#b8e0d2", "#ffcfa8"]

APPS = [  # (emoji, title, what it is, how to open) — apps whose file is missing are hidden
    ("🛠", "Workbench", "code changes + House Projects", ("pyw", PULSE / "scripts" / "workbench_gui.pyw")),
    ("🗨", "Local Reddit", "the house forum", ("pyw", PULSE / "scripts" / "lor_gui.pyw")),
    ("🖥", "Pulse GUI", "the desktop app", ("pyw", PULSE / "pulse_gui.py")),
    ("🪟", "Household Window", "the house dashboard (browser)", ("server", PULSE / "scripts" / "household_window.py")),
    ("📜", "Show Activity", "what everyone's been doing", ("console", PULSE / "scripts" / "show_activity.py")),
    ("🎨", "Pulse Art folder", "every painting, ready to print", ("open", ART)),
    ("📎", "Obsidian Comments", "notes on your clips", ("pyw", PULSE / "scripts" / "obsidian_notes_gui.pyw")),
    ("🩺", "Health Check", "is everyone okay?", ("console", PULSE / "scripts" / "health_check.py")),
    ("💸", "Cost Report", "the detailed money tables", ("console", PULSE / "scripts" / "cost_report.py")),
]
if BACKUPS:
    APPS += [
        ("💾", "Copy to Flash Drive", "plug in, click, done", ("open", BACKUPS / "Copy to Flash Drive.bat")),
        ("📨", "Telegram Backup", "save your chats", ("open", BACKUPS / "Telegram Backup.bat")),
        ("🗂", "Backups Folder", "everything that's backed up", ("open", BACKUPS)),
    ]
HOUSEHOLD_WINDOW_URL = "http://localhost:5175"
SECTIONS = [("home", "🏠", "Home"), ("crew", "💞", "Companions"), ("apps", "🚀", "Apps"), ("art", "🎨", "Art"),
            ("money", "💸", "Money"), ("health", "🩺", "Health & Backups")]


def is_light(hex_color: str) -> bool:
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return (0.299 * r + 0.587 * g + 0.114 * b) > 150


def ago(dt) -> str:
    if not dt:
        return "never"
    s = (datetime.now() - dt).total_seconds()
    if s < 90:
        return "just now"
    if s < 3600:
        return f"{int(s // 60)}m ago"
    if s < 86400:
        return f"{int(s // 3600)}h ago"
    return f"{int(s // 86400)}d ago"


# ── data ───────────────────────────────────────────────────────────────

def _yaml(path: Path) -> dict:
    import yaml
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def user_name() -> str:
    """user_nickname, then user_name, from config.yaml / the root persona file."""
    for fname in ("config.yaml", "persona.yaml", "persona.json"):
        data = _yaml(PULSE / fname)
        name = data.get("user_nickname") or data.get("user_name")
        if name:
            return str(name)
    return "friend"


def crew() -> list[dict]:
    """Every active persona folder, with display name, model and optional emoji."""
    root_cfg = _yaml(PULSE / "config.yaml")
    out = []
    pdir = PULSE / "personas"
    for d in sorted(pdir.iterdir()) if pdir.is_dir() else []:
        if not d.is_dir() or d.name.startswith("_") or not (d / "config.yaml").exists():
            continue
        cfg = _yaml(d / "config.yaml")
        if cfg.get("retired", False):
            continue
        name = ((cfg.get("channels") or {}).get("lor") or {}).get("author_name")
        if not name or str(name).strip().lower() == "companion":
            name = watchdog.name(d.name)
        model = (cfg.get("provider") or {}).get("model") or (root_cfg.get("provider") or {}).get("model") or ""
        out.append({"persona": d.name, "name": str(name), "model": str(model).split("/")[-1],
                    "emoji": str(cfg.get("emoji") or ""),
                    "main": str(cfg.get("hub_group") or "").lower() != "other"})
    return sorted(out, key=lambda c: c["name"].lower())


def _persona_of_window(cmd: str) -> str | None:
    """Persona of a console window: `--persona X` on its own command line
    (start.bat --persona X), else whatever its .bat runs."""
    m = re.search(r"--persona\s+\"?([^\s\"]+)", cmd or "")
    if m:
        return m.group(1)
    m = re.search(r'"([^"]+\.bat)"', cmd or "") or re.search(r'(\S+\.bat)', cmd or "")
    return watchdog.persona_of_bat(Path(m.group(1))) if m else None


def status() -> dict:
    procs = watchdog.processes()
    running, _ = watchdog.snapshot(procs)
    win_of = {}
    for p in procs:
        if (p.get("Name") or "").lower() == "cmd.exe":
            persona = _persona_of_window(p.get("CommandLine") or "")
            if persona:
                win_of.setdefault(persona, []).append(p["ProcessId"])
    py_of = {}
    for p in procs:
        if (p.get("Name") or "").lower().startswith("python"):
            per = watchdog.persona_of_cmdline(p.get("CommandLine") or "")
            if per:
                py_of.setdefault(per, []).append(p["ProcessId"])
    crashed = {per for per in win_of if per not in running}
    active = {}
    for log in (PULSE / "logs").glob("*.log"):
        active[log.stem] = datetime.fromtimestamp(log.stat().st_mtime)
    return {"running": running, "crashed": crashed, "windows": win_of, "py": py_of, "active": active}


def waiting() -> list[str]:
    try:
        import morning_digest
        return morning_digest.waiting()
    except Exception:
        return []


def money() -> dict:
    try:
        import spending
        return {"week": spending.week(), "spikes": spending.spikes()}
    except Exception as e:
        return {"error": f"{type(e).__name__}"}


def backups() -> dict:
    info = {}
    if not BACKUPS or not BACKUPS.is_dir():
        return info
    info["configured"] = True
    log = BACKUPS / "nightly_log.txt"
    if log.exists():
        lines = log.read_text(encoding="utf-8").strip().splitlines()
        info["last_line"] = lines[-1] if lines else ""
        info["nightly"] = datetime.fromtimestamp(log.stat().st_mtime)
        info["failed"] = any("failed" in l for l in lines[-6:])
    try:
        info["flash"] = datetime.fromisoformat((BACKUPS / "last_flash_copy.txt").read_text(encoding="utf-8-sig").strip())
    except Exception:
        info["flash"] = None
    try:
        info["telegram"] = datetime.fromisoformat((BACKUPS / "telegram" / "last_run.txt").read_text(encoding="utf-8").strip())
    except Exception:
        info["telegram"] = None
    snaps = sorted((BACKUPS / "companions").glob("*")) if (BACKUPS / "companions").exists() else []
    info["snapshot"] = snaps[-1].name if snaps else None
    return info


def art_files() -> list[tuple[str, Path]]:
    out = []
    if ART.exists():
        for artist in ART.iterdir():
            if artist.is_dir():
                for f in artist.glob("*.png"):
                    out.append((artist.name, f))
    out.sort(key=lambda af: af[1].name, reverse=True)       # names start with the date
    return out


def open_thing(how, path: Path):
    kind = how
    if kind == "pyw":
        subprocess.Popen([str(PYW), str(path)], cwd=str(PULSE))
    elif kind == "console":                                  # a script that prints: keep its window open
        subprocess.Popen(f'cmd.exe /k ""{PY}" "{path}""', cwd=str(PULSE), creationflags=NEW_CONSOLE)
    elif kind == "server":                                   # a local web page: start it, then open the browser
        import webbrowser
        subprocess.Popen([str(PY), str(path)], cwd=str(PULSE), creationflags=NEW_CONSOLE)
        threading.Timer(2.0, lambda: webbrowser.open(HOUSEHOLD_WINDOW_URL)).start()
    elif kind == "notepad":
        subprocess.Popen(["notepad.exe", str(path)])
    else:
        os.startfile(str(path))


def start_command(persona: str) -> str:
    """How a companion is started: upstream's start.bat when the .venv exists
    (it ends with `pause`, so a crash leaves the window open and the Hub shows
    "crashed"), otherwise this Python running pulse.py in a console that stays
    open (the Hub can start/stop it, but can't tell a crash from a stop).
    Returned as one string so cmd's quote handling copes with spaces in paths."""
    if VENV_PY.exists() and (PULSE / "start.bat").exists():
        return f'cmd.exe /c ""{PULSE / "start.bat"}" --persona {persona}"'
    return f'cmd.exe /k ""{PY}" "{PULSE / "pulse.py"}" --persona {persona}"'


# ── the window ─────────────────────────────────────────────────────────

class Hub:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("Pulse Hub")
        root.geometry("1220x820")
        root.minsize(980, 640)
        self.settings = load_settings()
        self.section = "home"
        self.crew = crew()
        self.st = {"running": set(), "crashed": set(), "windows": {}, "py": {}, "active": {}}
        self.waiting, self.money, self.backups = [], {}, {}
        self.checked = {c["persona"]: tk.BooleanVar(value=False) for c in self.crew}
        self.art_filter = tk.StringVar(value="Everyone")
        self._images = []                                    # keep PhotoImages alive
        self._busy = False
        self._jobs = {}
        self.build()
        self.refresh_status()
        self.refresh_slow()

    # ── look ──
    @property
    def c(self):
        return PALETTES[self.settings["palette"]]

    def fonts(self):
        s = self.settings["font_size"]
        self.F = (FONT_FAMILY, s + 1)
        self.FB = (FONT_FAMILY, s + 1, "bold")
        self.FS = (FONT_FAMILY, max(8, s - 1))
        self.FT = (FONT_FAMILY, s + 6, "bold")
        self.FH = (FONT_FAMILY, s + 14, "bold")
        self.FE = ("Segoe UI Emoji", s + 10)
        light = is_light(self.c["BG"])
        self.GOOD = "#2f8f55" if light else "#9be0a8"
        self.BAD = "#c0394a" if light else "#ff9aa6"
        self.WARN = "#b7791f" if light else "#ffd59e"

    def build(self):
        for w in self.root.winfo_children():
            w.destroy()
        self.fonts()
        c = self.c
        self.root.configure(bg=c["BG"])
        style = ttk.Style()
        style.theme_use("default")
        style.configure("Vertical.TScrollbar", background=c["BUTTON_BG"], troughcolor=c["BG"], arrowcolor=c["TEXT_DIM"],
                        borderwidth=0)
        # sidebar
        side = tk.Frame(self.root, bg=c["BG_LIGHT"], width=262)
        side.pack(side=tk.LEFT, fill=tk.Y)
        side.pack_propagate(False)
        tk.Label(side, text="Pulse", bg=c["BG_LIGHT"], fg=c["ACCENT"], font=self.FH).pack(anchor="w", padx=22, pady=(22, 0))
        tk.Label(side, text="hub", bg=c["BG_LIGHT"], fg=c["TEXT_DIM"], font=self.FT).pack(anchor="w", padx=24, pady=(0, 18))
        self.nav = {}
        for key, emoji, label in SECTIONS:
            b = tk.Label(side, text=f"  {emoji}   {label}", anchor="w", font=self.FB, padx=14, pady=10, cursor="hand2")
            b.pack(fill=tk.X, padx=12, pady=3)
            b.bind("<Button-1>", lambda e, k=key: self.show(k))
            b.bind("<Enter>", lambda e, b=b, k=key: k != self.section and b.config(bg=c["BUTTON_HOVER"]))
            b.bind("<Leave>", lambda e, b=b, k=key: k != self.section and b.config(bg=c["BG_LIGHT"]))
            self.nav[key] = b
        bottom = tk.Frame(side, bg=c["BG_LIGHT"])
        bottom.pack(side=tk.BOTTOM, fill=tk.X, padx=16, pady=16)
        tk.Label(bottom, text="🎨 palette", bg=c["BG_LIGHT"], fg=c["TEXT_DIM"], font=self.FS).pack(anchor="w")
        pv = tk.StringVar(value=self.settings["palette"])
        om = tk.OptionMenu(bottom, pv, *PALETTES, command=self.change_palette)
        om.config(bg=c["BUTTON_BG"], fg=c["TEXT"], font=self.FS, relief=tk.FLAT, highlightthickness=0,
                  activebackground=c["BUTTON_HOVER"], activeforeground=c["TEXT"], borderwidth=0)
        om["menu"].config(bg=c["ENTRY_BG"], fg=c["TEXT"], font=self.FS)
        om.pack(fill=tk.X, pady=(2, 8))
        self.status_lbl = tk.Label(bottom, text="", bg=c["BG_LIGHT"], fg=c["TEXT_DIM"], font=self.FS, justify="left",
                                   wraplength=180)
        self.status_lbl.pack(anchor="w")
        # content area (scrollable)
        holder = tk.Frame(self.root, bg=c["BG"])
        holder.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.canvas = tk.Canvas(holder, bg=c["BG"], highlightthickness=0, borderwidth=0)
        sb = ttk.Scrollbar(holder, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=sb.set)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.page = tk.Frame(self.canvas, bg=c["BG"])
        self.page_id = self.canvas.create_window(0, 0, window=self.page, anchor="nw")
        self.page.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfig(self.page_id, width=e.width))
        self.root.bind_all("<MouseWheel>", lambda e: self.canvas.yview_scroll(int(-e.delta / 120), "units"))
        self.show(self.section)

    def change_palette(self, value):
        fresh = load_settings()
        fresh["palette"] = value
        save_settings(fresh)
        self.settings = fresh
        self.build()

    # ── small widgets ──
    def card(self, parent, title=None, sub=None, pad=18):
        c = self.c
        f = tk.Frame(parent, bg=c["CARD_BG"], padx=pad, pady=pad - 4, highlightthickness=0)
        if title:
            row = tk.Frame(f, bg=c["CARD_BG"])
            row.pack(fill=tk.X)
            tk.Label(row, text=title, bg=c["CARD_BG"], fg=c["ACCENT"], font=self.FT).pack(side=tk.LEFT)
            if sub:
                tk.Label(row, text=sub, bg=c["CARD_BG"], fg=c["TEXT_DIM"], font=self.FS).pack(side=tk.LEFT, padx=10, pady=(6, 0))
        return f

    def button(self, parent, text, cmd, kind="plain", bg=None):
        c = self.c
        fill, fg = {"accent": (c["ACCENT"], c["BG"]), "plain": (c["BUTTON_BG"], c["TEXT"]),
                    "good": (self.GOOD, c["BG"]), "bad": (c["BUTTON_BG"], self.BAD)}[kind]
        b = tk.Label(parent, text=text, bg=fill, fg=fg, font=self.FB if kind != "plain" else self.F,
                     padx=14, pady=6, cursor="hand2")
        b.bind("<Button-1>", lambda e: cmd())
        b.bind("<Enter>", lambda e: b.config(bg=c["HIGHLIGHT"] if kind == "accent" else c["BUTTON_HOVER"]))
        b.bind("<Leave>", lambda e: b.config(bg=fill))
        return b

    def dot(self, parent, color, bg, size=12):
        cv = tk.Canvas(parent, width=size, height=size, bg=bg, highlightthickness=0)
        cv.create_oval(1, 1, size - 1, size - 1, fill=color, outline=color)
        return cv

    def avatar(self, parent, name, bg, size=54):
        col = AVATAR_COLORS[int(hashlib.md5(name.encode()).hexdigest(), 16) % len(AVATAR_COLORS)]
        cv = tk.Canvas(parent, width=size, height=size, bg=bg, highlightthickness=0)
        cv.create_oval(2, 2, size - 2, size - 2, fill=col, outline=col)
        cv.create_text(size // 2, size // 2, text=name[:1].upper(), font=(FONT_FAMILY, size // 3, "bold"), fill="#3d3229")
        return cv

    def heading(self, text, sub=""):
        c = self.c
        tk.Label(self.page, text=text, bg=c["BG"], fg=c["HIGHLIGHT"], font=self.FH).pack(anchor="w", padx=32, pady=(28, 0))
        if sub:
            tk.Label(self.page, text=sub, bg=c["BG"], fg=c["TEXT_DIM"], font=self.F).pack(anchor="w", padx=34, pady=(2, 14))

    # ── navigation ──
    def show(self, key):
        self.section = key
        c = self.c
        for k, b in self.nav.items():
            b.config(bg=c["ACCENT"] if k == key else c["BG_LIGHT"], fg=c["BG"] if k == key else c["TEXT"])
        for w in self.page.winfo_children():
            w.destroy()
        self._images.clear()
        getattr(self, f"page_{key}")()
        self.canvas.yview_moveto(0)

    def redraw(self):
        y = self.canvas.yview()[0]
        self.show(self.section)
        self.canvas.yview_moveto(y)

    # ── pages ──
    def page_home(self):
        c = self.c
        h = datetime.now().hour
        hello = "Good morning" if h < 12 else "Good afternoon" if h < 18 else "Good evening"
        self.heading(f"{hello}, {user_name()} 💖", f"{datetime.now():%A}, {datetime.now():%B} {datetime.now().day}")
        tiles = tk.Frame(self.page, bg=c["BG"])
        tiles.pack(fill=tk.X, padx=28)
        main = [m for m in self.crew if m["main"]]
        on = sum(1 for m in main if m["persona"] in self.st["running"])
        wk = (self.money.get("week") or {})
        bk = self.backups
        last_bk = ("not set up" if not bk.get("configured") else
                   "✓ last night" if bk.get("nightly") and datetime.now() - bk["nightly"] < timedelta(hours=30)
                   and not bk.get("failed") else "⚠ check" if bk.get("failed") else "not yet")
        for i, (big, small, col, go) in enumerate([
                (f"{on} / {len(main)}", "companions running", self.GOOD if on else c["TEXT_DIM"], "crew"),
                (str(len(self.waiting)), "waiting for you", c["ACCENT"] if self.waiting else c["TEXT_DIM"], "home"),
                (f"${wk.get('total', 0):.0f}", "spent this week", c["TEXT"], "money"),
                (last_bk, "backups", self.GOOD if "✓" in last_bk else self.WARN, "health")]):
            t = tk.Frame(tiles, bg=c["CARD_BG"], padx=20, pady=14, cursor="hand2")
            t.grid(row=0, column=i, sticky="nsew", padx=6, pady=6)
            tiles.columnconfigure(i, weight=1)
            a = tk.Label(t, text=big, bg=c["CARD_BG"], fg=col, font=self.FH)
            a.pack(anchor="w")
            b = tk.Label(t, text=small, bg=c["CARD_BG"], fg=c["TEXT_DIM"], font=self.F)
            b.pack(anchor="w")
            for w in (t, a, b):
                w.bind("<Button-1>", lambda e, g=go: self.show(g))
        two = tk.Frame(self.page, bg=c["BG"])
        two.pack(fill=tk.BOTH, padx=28, pady=6)
        two.columnconfigure(0, weight=3)
        two.columnconfigure(1, weight=2)
        # waiting
        w = self.card(two, "💖 Waiting for you")
        w.grid(row=0, column=0, sticky="nsew", padx=6, pady=6)
        if self.waiting:
            for line in self.waiting:
                tk.Label(w, text=line, bg=c["CARD_BG"], fg=c["TEXT"], font=self.F, anchor="w", justify="left",
                         wraplength=520).pack(anchor="w", pady=2)
            self.button(w, "Open the Workbench", lambda: open_thing("pyw", PULSE / "scripts" / "workbench_gui.pyw"),
                        "accent").pack(anchor="w", pady=(10, 0))
        else:
            tk.Label(w, text="Nothing's waiting on you. 🌸", bg=c["CARD_BG"], fg=c["TEXT_DIM"], font=self.F).pack(anchor="w", pady=4)
        # who's around
        a = self.card(two, "🌙 Who's around")
        a.grid(row=0, column=1, sticky="nsew", padx=6, pady=6)
        for m in main:
            row = tk.Frame(a, bg=c["CARD_BG"])
            row.pack(fill=tk.X, pady=1)
            per = m["persona"]
            col = self.BAD if per in self.st["crashed"] else self.GOOD if per in self.st["running"] else c["BUTTON_HOVER"]
            self.dot(row, col, c["CARD_BG"]).pack(side=tk.LEFT, padx=(0, 8))
            tk.Label(row, text=f"{m['name']} {m['emoji']}", bg=c["CARD_BG"], fg=c["TEXT"], font=self.F).pack(side=tk.LEFT)
            act = self.st["active"].get(per)
            tk.Label(row, text=ago(act) if per in self.st["running"] else "off", bg=c["CARD_BG"], fg=c["TEXT_DIM"],
                     font=self.FS).pack(side=tk.RIGHT)
        # heads-ups
        notes = []
        for e in watchdog.recent_events(2)[-3:]:
            notes.append(f"🚨 {e['who']}: " + {"restart": "crashed — restarted ✨", "crash": "crashed (window open)",
                                              "errors": "lots of errors"}.get(e["kind"], e["kind"]))
        for n, y, usual in self.money.get("spikes", []):
            notes.append(f"💸 {n} spent ${y:.2f} yesterday (usually ~${usual:.2f})")
        if bk.get("configured") and (bk.get("flash") is None or datetime.now() - bk["flash"] > timedelta(days=14)):
            notes.append("💾 Time to copy your backups to the flash drive")
        if notes:
            hn = self.card(self.page, "🔔 Heads-up")
            hn.pack(fill=tk.X, padx=34, pady=6)
            for n in notes:
                tk.Label(hn, text=n, bg=c["CARD_BG"], fg=c["TEXT"], font=self.F, anchor="w").pack(anchor="w", pady=1)
        # newest art
        files = art_files()[:6]
        if files:
            ar = self.card(self.page, "🎨 Newest paintings", "click one to open its print page")
            ar.pack(fill=tk.X, padx=34, pady=(6, 24))
            strip = tk.Frame(ar, bg=c["CARD_BG"])
            strip.pack(anchor="w", pady=(8, 0))
            for artist, f in files:
                self.thumb(strip, artist, f, 120).pack(side=tk.LEFT, padx=6)

    def thumb(self, parent, artist, f: Path, size=140):
        c = self.c
        box = tk.Frame(parent, bg=c["CARD_BG"], cursor="hand2")
        try:
            img = tk.PhotoImage(file=str(f))
            k = max(1, max(img.width(), img.height()) // size)
            img = img.subsample(k, k)
            self._images.append(img)
            lbl = tk.Label(box, image=img, bg=c["CARD_BG"])
        except Exception:
            lbl = tk.Label(box, text="🖼", bg=c["CARD_BG"], font=self.FE)
        lbl.pack()
        title = f.stem[11:] if len(f.stem) > 11 else f.stem
        cap = tk.Label(box, text=f"{title[:18]}\n{artist}", bg=c["CARD_BG"], fg=c["TEXT_DIM"], font=self.FS, justify="center")
        cap.pack(pady=(4, 0))
        page = f.parent / "For printing" / f.name
        for w in (box, lbl, cap):
            w.bind("<Button-1>", lambda e, p=page if page.exists() else f: os.startfile(str(p)))
        return box

    def page_crew(self):
        c = self.c
        self.heading("Companions", "Start whoever you like — tick a few and start them together. Nothing starts on its own.")
        bar = tk.Frame(self.page, bg=c["BG"])
        bar.pack(fill=tk.X, padx=34, pady=(0, 6))
        self.button(bar, "▶ Start checked", self.start_checked, "accent").pack(side=tk.LEFT)
        self.button(bar, "Clear ticks", lambda: [v.set(False) for v in self.checked.values()]).pack(side=tk.LEFT, padx=8)
        self.button(bar, "↻ Refresh", self.refresh_status).pack(side=tk.LEFT)
        for title, group in (("The crew", [m for m in self.crew if m["main"]]),
                             ("Others", [m for m in self.crew if not m["main"]])):
            if not group:
                continue
            tk.Label(self.page, text=title, bg=c["BG"], fg=c["ACCENT"], font=self.FT).pack(anchor="w", padx=34, pady=(16, 4))
            grid = tk.Frame(self.page, bg=c["BG"])
            grid.pack(fill=tk.X, padx=28)
            for i in range(3):
                grid.columnconfigure(i, weight=1, uniform="crew")
            for i, m in enumerate(group):
                self.crew_card(grid, m).grid(row=i // 3, column=i % 3, sticky="nsew", padx=6, pady=6)
        tk.Frame(self.page, bg=c["BG"], height=24).pack()

    def crew_card(self, parent, m):
        c = self.c
        per = m["persona"]
        f = tk.Frame(parent, bg=c["CARD_BG"], padx=16, pady=14)
        top = tk.Frame(f, bg=c["CARD_BG"])
        top.pack(fill=tk.X)
        self.avatar(top, m["name"], c["CARD_BG"]).pack(side=tk.LEFT)
        txt = tk.Frame(top, bg=c["CARD_BG"])
        txt.pack(side=tk.LEFT, padx=12, fill=tk.X, expand=True)
        tk.Label(txt, text=f"{m['name']} {m['emoji']}", bg=c["CARD_BG"], fg=c["HIGHLIGHT"], font=self.FT,
                 anchor="w").pack(anchor="w")
        tk.Label(txt, text=m["model"] or "", bg=c["CARD_BG"], fg=c["TEXT_DIM"], font=self.FS, anchor="w").pack(anchor="w")
        tk.Checkbutton(top, variable=self.checked[per], bg=c["CARD_BG"], activebackground=c["CARD_BG"],
                       selectcolor=c["ENTRY_BG"], highlightthickness=0, borderwidth=0).pack(side=tk.RIGHT, anchor="n")
        row = tk.Frame(f, bg=c["CARD_BG"])
        row.pack(fill=tk.X, pady=(10, 6))
        if per in self.st["crashed"]:
            col, word = self.BAD, "crashed — window still open"
        elif per in self.st["running"]:
            col, word = self.GOOD, f"running · active {ago(self.st['active'].get(per))}"
        else:
            col, word = c["BUTTON_HOVER"], f"off · last seen {ago(self.st['active'].get(per))}"
        self.dot(row, col, c["CARD_BG"]).pack(side=tk.LEFT, padx=(0, 8))
        tk.Label(row, text=word, bg=c["CARD_BG"], fg=c["TEXT"], font=self.FS).pack(side=tk.LEFT)
        cost = ((self.money.get("week") or {}).get("per") or {}).get(m["name"])
        if cost:
            tk.Label(f, text=f"${cost:.2f} this week", bg=c["CARD_BG"], fg=c["TEXT_DIM"], font=self.FS).pack(anchor="w")
        btns = tk.Frame(f, bg=c["CARD_BG"])
        btns.pack(fill=tk.X, pady=(8, 0))
        if per in self.st["running"] or per in self.st["crashed"]:
            self.button(btns, "■ Stop", lambda m=m: self.stop(m), "bad").pack(side=tk.LEFT)
            if per in self.st["crashed"]:
                self.button(btns, "↻ Restart", lambda m=m: (self.stop(m, ask=False), self.start(m)), "accent").pack(side=tk.LEFT, padx=6)
        else:
            self.button(btns, "▶ Start", lambda m=m: self.start(m), "good").pack(side=tk.LEFT)
        log = PULSE / "logs" / f"{per}.log"
        if log.exists():
            self.button(btns, "log", lambda l=log: subprocess.Popen(["notepad.exe", str(l)])).pack(side=tk.RIGHT)
        return f

    def page_apps(self):
        c = self.c
        self.heading("Apps & tools", "Everything in one place — one click each.")
        grid = tk.Frame(self.page, bg=c["BG"])
        grid.pack(fill=tk.X, padx=28, pady=(0, 24))
        cols = 3
        for i in range(cols):
            grid.columnconfigure(i, weight=1, uniform="apps")
        apps = [a for a in APPS if Path(a[3][1]).exists()]
        for i, (emoji, title, what, (how, path)) in enumerate(apps):
            t = tk.Frame(grid, bg=c["CARD_BG"], padx=16, pady=12, cursor="hand2")
            t.grid(row=i // cols, column=i % cols, sticky="nsew", padx=6, pady=6)
            e = tk.Label(t, text=emoji, bg=c["CARD_BG"], font=self.FE)
            e.pack(side=tk.LEFT, padx=(0, 12))
            col = tk.Frame(t, bg=c["CARD_BG"])
            col.pack(side=tk.LEFT, fill=tk.X, expand=True)
            a = tk.Label(col, text=title, bg=c["CARD_BG"], fg=c["TEXT"], font=self.FB, anchor="w")
            a.pack(anchor="w")
            b = tk.Label(col, text=what, bg=c["CARD_BG"], fg=c["TEXT_DIM"], font=self.FS, anchor="w")
            b.pack(anchor="w")
            ok = Path(path).exists()
            widgets = (t, e, col, a, b)
            for w in widgets:
                w.bind("<Button-1>", lambda ev, h=how, p=path, ok=ok, n=title: self.launch(h, p, n) if ok else
                       messagebox.showinfo("Not found", f"I couldn't find {n} at\n{p}"))
                w.bind("<Enter>", lambda ev, ws=widgets: [x.config(bg=c["BUTTON_HOVER"]) for x in ws])
                w.bind("<Leave>", lambda ev, ws=widgets: [x.config(bg=c["CARD_BG"]) for x in ws])

    def page_art(self):
        c = self.c
        files = art_files()
        artists = sorted({a for a, _ in files})
        self.heading("Art", f"{len(files)} paintings — click one to open its print page. "
                     "\"Look for new paintings\" collects new ones.")
        bar = tk.Frame(self.page, bg=c["BG"])
        bar.pack(fill=tk.X, padx=34, pady=(0, 8))
        tk.Label(bar, text="Show:", bg=c["BG"], fg=c["TEXT_DIM"], font=self.F).pack(side=tk.LEFT)
        om = tk.OptionMenu(bar, self.art_filter, "Everyone", *artists, command=lambda v: self.show("art"))
        om.config(bg=c["BUTTON_BG"], fg=c["TEXT"], font=self.F, relief=tk.FLAT, highlightthickness=0,
                  activebackground=c["BUTTON_HOVER"], borderwidth=0)
        om["menu"].config(bg=c["ENTRY_BG"], fg=c["TEXT"], font=self.F)
        om.pack(side=tk.LEFT, padx=8)
        self.button(bar, "📂 Open the Pulse Art folder", lambda: os.startfile(str(ART))).pack(side=tk.LEFT, padx=8)
        self.button(bar, "✨ Look for new paintings", self.collect_art).pack(side=tk.LEFT)
        pick = self.art_filter.get()
        shown = [(a, f) for a, f in files if pick in ("Everyone", a)][:60]
        grid = tk.Frame(self.page, bg=c["BG"])
        grid.pack(fill=tk.X, padx=28, pady=(0, 24))
        cols = max(3, (max(700, self.canvas.winfo_width()) - 70) // 172)    # fit the window
        for i, (artist, f) in enumerate(shown):
            cell = tk.Frame(grid, bg=c["CARD_BG"], padx=8, pady=8)
            cell.grid(row=i // cols, column=i % cols, padx=5, pady=5, sticky="n")
            self.thumb(cell, artist, f, 130).pack()
        if len([1 for a, _ in files if pick in ("Everyone", a)]) > 60:
            tk.Label(self.page, text="(showing the newest 60 — the folder has them all)", bg=c["BG"], fg=c["TEXT_DIM"],
                     font=self.FS).pack(pady=(0, 20))

    def page_money(self):
        c = self.c
        self.heading("Money", "From each companion's usage records, priced per model.")
        m = self.money
        if not m:
            tk.Label(self.page, text="Adding things up…", bg=c["BG"], fg=c["TEXT_DIM"], font=self.F).pack(anchor="w", padx=34)
            return
        if m.get("error"):
            tk.Label(self.page, text=f"Couldn't add it up ({m['error']}).", bg=c["BG"], fg=self.BAD, font=self.F).pack(anchor="w", padx=34)
            return
        wk = m["week"]
        tiles = tk.Frame(self.page, bg=c["BG"])
        tiles.pack(fill=tk.X, padx=28)
        for i, (big, small) in enumerate(((f"${wk['total']:.2f}", "last 7 days"), (f"${wk['yesterday']:.2f}", "yesterday"),
                                          (f"~${wk['per_day']:.2f}", "per day"), (f"~${wk['month_pace']:.0f}", "a month at this pace"))):
            t = tk.Frame(tiles, bg=c["CARD_BG"], padx=20, pady=14)
            t.grid(row=0, column=i, sticky="nsew", padx=6, pady=6)
            tiles.columnconfigure(i, weight=1)
            tk.Label(t, text=big, bg=c["CARD_BG"], fg=c["HIGHLIGHT"], font=self.FH).pack(anchor="w")
            tk.Label(t, text=small, bg=c["CARD_BG"], fg=c["TEXT_DIM"], font=self.F).pack(anchor="w")
        for n, y, usual in m.get("spikes", []):
            tk.Label(self.page, text=f"⚠️ {n} spent ${y:.2f} yesterday — usually about ${usual:.2f} a day",
                     bg=c["BG"], fg=self.WARN, font=self.FB).pack(anchor="w", padx=34, pady=(8, 0))
        box = self.card(self.page, "Who spent what (last 7 days)")
        box.pack(fill=tk.X, padx=34, pady=12)
        per = wk["per"]
        emoji_of = {m["name"]: m["emoji"] for m in self.crew}
        top = max(per.values()) if per else 1
        cv = tk.Canvas(box, bg=c["CARD_BG"], highlightthickness=0, height=34 * len(per) + 10)
        cv.pack(fill=tk.X, pady=(10, 0))

        def draw(_e=None):
            cv.delete("all")
            width = max(300, cv.winfo_width())
            for i, (n, v) in enumerate(per.items()):
                y = 8 + i * 34
                cv.create_text(4, y + 12, text=f"{n} {emoji_of.get(n, '')}", anchor="w", font=self.F, fill=c["TEXT"])
                x0, x1 = 160, 160 + (width - 260) * (v / top if top else 0)
                cv.create_rectangle(x0, y + 2, max(x0 + 4, x1), y + 22, fill=c["ACCENT"], outline="")
                cv.create_text(max(x0 + 4, x1) + 8, y + 12, text=f"${v:.2f}", anchor="w", font=self.FS, fill=c["TEXT_DIM"])
        cv.bind("<Configure>", draw)
        self.button(self.page, "Open the detailed cost report", lambda: open_thing("console", PULSE / "scripts" / "cost_report.py")).pack(
            anchor="w", padx=34, pady=(0, 24))

    def page_health(self):
        c = self.c
        self.heading("Health & Backups", "The crash watch (scripts/watchdog.py) checks every 10 minutes once scheduled.")
        st = watchdog.load_state()
        h = self.card(self.page, "🩺 Crash watch")
        h.pack(fill=tk.X, padx=34, pady=6)
        last = st.get("last_check")
        tk.Label(h, text=f"Last check: {ago(datetime.fromisoformat(last)) if last else 'not yet'}", bg=c["CARD_BG"],
                 fg=c["TEXT"], font=self.F).pack(anchor="w", pady=(6, 2))
        evs = watchdog.recent_events(7)
        if not evs:
            tk.Label(h, text="✅ No crashes or error storms this week.", bg=c["CARD_BG"], fg=self.GOOD, font=self.F).pack(anchor="w")
        for e in evs[-8:]:
            tk.Label(h, text=f"{e['at'][5:16].replace('T', ' ')}  {e['who']}: " +
                     {"restart": "crashed — restarted", "crash": "crashed (left open)", "errors": "error storm"}.get(e["kind"], e["kind"])
                     + (f" — {e['detail'][:70]}" if e.get("detail") else ""),
                     bg=c["CARD_BG"], fg=c["TEXT"], font=self.FS, anchor="w").pack(anchor="w")
        self.button(h, "Run the full Health Check", lambda: open_thing("console", PULSE / "scripts" / "health_check.py")).pack(anchor="w", pady=(10, 0))
        bk = self.backups
        b = self.card(self.page, "💾 Backups")
        b.pack(fill=tk.X, padx=34, pady=6)
        if not bk.get("configured"):
            tk.Label(b, text="No backups folder set. Set PULSE_BACKUPS_DIR to show its status here.",
                     bg=c["CARD_BG"], fg=c["TEXT_DIM"], font=self.F, anchor="w").pack(anchor="w", pady=(6, 2))
            return
        flash = bk.get("flash")
        rows = [("Nightly backup", (ago(bk.get("nightly")) if bk.get("nightly") else "none yet")
                 + (" — something failed, see the log" if bk.get("failed") else "")),
                ("Newest snapshot", bk.get("snapshot") or "none yet"),
                ("Telegram", ago(bk.get("telegram")) if bk.get("telegram") else "not signed in yet — click Telegram Backup once"),
                ("Flash drive", (f"{(datetime.now() - flash).days} days ago" if flash else "never — let's do it!"))]
        for k, v in rows:
            r = tk.Frame(b, bg=c["CARD_BG"])
            r.pack(fill=tk.X, pady=2)
            tk.Label(r, text=k, bg=c["CARD_BG"], fg=c["TEXT_DIM"], font=self.F, width=16, anchor="w").pack(side=tk.LEFT)
            warn = (k == "Flash drive" and (not flash or (datetime.now() - flash).days > 14)) or "failed" in v
            tk.Label(r, text=v, bg=c["CARD_BG"], fg=self.WARN if warn else c["TEXT"], font=self.F, anchor="w").pack(side=tk.LEFT)
        btns = tk.Frame(b, bg=c["CARD_BG"])
        btns.pack(anchor="w", pady=(12, 0))
        flash_bat, tg_bat = BACKUPS / "Copy to Flash Drive.bat", BACKUPS / "Telegram Backup.bat"
        if flash_bat.exists():
            self.button(btns, "💾 Copy to Flash Drive", lambda: os.startfile(str(flash_bat)), "accent").pack(side=tk.LEFT)
        if (BACKUPS / "make_backup.py").exists() and (BACKUPS / "house_backup.py").exists():
            self.button(btns, "Back up now", self.backup_now).pack(side=tk.LEFT, padx=8)
        if tg_bat.exists():
            self.button(btns, "📨 Telegram Backup", lambda: os.startfile(str(tg_bat))).pack(side=tk.LEFT)
        self.button(btns, "🗂 Folder", lambda: os.startfile(str(BACKUPS))).pack(side=tk.LEFT, padx=8)

    # ── actions ──
    def launch(self, how, path, name):
        try:
            open_thing(how, Path(path))
            self.status_lbl.config(text=f"Opened {name} ✨")
        except Exception as e:
            messagebox.showerror("Couldn't open", f"{name}: {e}")

    def start(self, m):
        if m["persona"] in self.st["running"]:
            return
        subprocess.Popen(start_command(m["persona"]), cwd=str(PULSE), creationflags=NEW_CONSOLE)
        self.status_lbl.config(text=f"Starting {m['name']}…")
        self.root.after(6000, self.refresh_status)

    def start_checked(self):
        picked = [m for m in self.crew if self.checked[m["persona"]].get()]
        if not picked:
            self.status_lbl.config(text="Tick the companions you want first ✔")
            return
        for i, m in enumerate(picked):
            self.root.after(i * 1500, lambda m=m: self.start(m))       # a little apart, so windows don't pile up
        for v in self.checked.values():
            v.set(False)

    def stop(self, m, ask=True):
        if ask and not messagebox.askyesno("Stop?", f"Stop {m['name']}? (Same as closing their window.)"):
            return
        for pid in self.st["windows"].get(m["persona"], []) + self.st["py"].get(m["persona"], []):
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, creationflags=NO_WINDOW)
        self.status_lbl.config(text=f"Stopped {m['name']}.")
        self.root.after(1500, self.refresh_status)

    def collect_art(self):
        def work():
            try:
                os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
                import collect_art
                n = collect_art.collect(verbose=False)
                msg = f"{n} new picture{'s' if n != 1 else ''} ✨" if n else "No new paintings yet."
            except Exception as e:
                msg = f"Couldn't collect art ({type(e).__name__})"
            self.root.after(0, lambda: (self.status_lbl.config(text=msg), self.section == "art" and self.redraw()))
        self.status_lbl.config(text="Looking for new paintings…")
        threading.Thread(target=work, daemon=True).start()

    def backup_now(self):
        if not messagebox.askyesno("Back up now?", "Make a fresh backup of every companion and the house now?\n"
                                   "(Takes a minute or two — you can keep using everything.)"):
            return

        def work():
            try:
                sys.path.insert(0, str(BACKUPS))
                import make_backup
                import house_backup
                make_backup.main()
                house_backup.backup()
                msg = "Backed up ✓"
            except Exception as e:
                msg = f"Backup hit a snag ({type(e).__name__})"
            self.root.after(0, lambda: (self.status_lbl.config(text=msg), self.refresh_slow()))
        self.status_lbl.config(text="Backing up…")
        threading.Thread(target=work, daemon=True).start()

    # ── refresh (background threads, then redraw on the UI thread) ──
    def refresh_status(self):
        if self._busy:
            return
        self._busy = True

        def work():
            try:
                st = status()
                wt = waiting()
            except Exception:
                st, wt = self.st, self.waiting
            self.root.after(0, lambda: self._apply(st, wt))
        threading.Thread(target=work, daemon=True).start()

    def _apply(self, st, wt):
        changed = (st["running"], st["crashed"], wt) != (self.st["running"], self.st["crashed"], self.waiting)
        self.st, self.waiting = st, wt
        self._busy = False
        self.status_lbl.config(text=f"updated {datetime.now():%I:%M %p}".replace(" 0", " "))
        if changed and self.section in ("home", "crew"):
            self.redraw()
        self._again("status", STATUS_MS, self.refresh_status)

    def refresh_slow(self):
        def work():
            mo, bk = money(), backups()
            self.root.after(0, lambda: self._apply_slow(mo, bk))
        threading.Thread(target=work, daemon=True).start()

    def _apply_slow(self, mo, bk):
        self.money, self.backups = mo, bk
        if self.section in ("home", "money", "health", "crew"):
            self.redraw()
        self._again("slow", SLOW_MS, self.refresh_slow)

    def _again(self, key, ms, fn):
        """One timer per kind, however often Refresh is pressed."""
        if self._jobs.get(key):
            try:
                self.root.after_cancel(self._jobs[key])
            except Exception:
                pass
        self._jobs[key] = self.root.after(ms, fn)


def main():
    root = tk.Tk()
    try:
        root.iconbitmap(default="")
    except Exception:
        pass
    Hub(root)
    root.mainloop()


if __name__ == "__main__":
    main()
