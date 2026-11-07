"""
lor_gui — Tkinter window for browsing LoR (Local Reddit). Designed to feel
like the Lighthouse GUI's quiet cousin.

Features:
  - 🎨 Palette picker + text size (A− / A+), remembered between launches
    in data/lor_gui_settings.json.
  - Threads tab lists only top-level posts, with comment + reaction counts.
    Comments (nested) sit in their own pane right above the reply box.
  - Click a comment to reply to / react to that comment instead of the post.
  - Activity tab: recent comments and reactions, newest first, with an
    unread count. Reactions carry no timestamp in posts.json, so this window
    notes when it first sees each one (data/lor/reaction_times.json; the LoR
    skill records exact times there too, once the file exists). Reactions
    that existed before tracking started show as "earlier".
  - Post/comment panes are read-only (select + Ctrl+C still work).
  - Reactions are spelled out: emoji + name + who reacted.
  - You can reply, react (toggles), and start threads. Same data format as
    the LoR skill (comments are posts with reply_to — every reader depends on
    that, so it stays). posts.json is re-read right before every write so
    nothing a companion posted in the meantime gets clobbered.
  - Checks for new posts every 30s while open.

Launch from the Pulse folder:
    py -3.14 scripts/lor_gui.pyw

Your LoR identity is created on first post/reaction (data/lor/user_author_id.txt),
named after user_nickname / user_name from the config.

No console window because of the .pyw extension. Pure local JSON;
no API calls.
"""

import hashlib
import json
import os
import time
import tkinter as tk
import unicodedata
from datetime import datetime, timezone
from tkinter import ttk, scrolledtext, messagebox
from pathlib import Path

PULSE_ROOT = Path(__file__).resolve().parent.parent
LOR_DIR = PULSE_ROOT / "data" / "lor"
POSTS_FILE = LOR_DIR / "posts.json"
AUTHORS_FILE = LOR_DIR / "authors.json"
USER_AUTHOR_ID_FILE = LOR_DIR / "user_author_id.txt"
SETTINGS_FILE = PULSE_ROOT / "data" / "lor_gui_settings.json"
REACTION_TIMES_FILE = LOR_DIR / "reaction_times.json"   # skills/lor.py writes exact times here too
OLD_REACTION_TIMES_FILE = PULSE_ROOT / "data" / "lor_gui_reaction_times.json"

POLL_MS = 30_000

# Offered in the ＋ React menu (any emoji already on a post shows there too).
REACT_CHOICES = ['❤️', '🍪', '👀', '💀', '🔥', '🌟', '🫂', '🪨', '🕯️', '💛',
                 '😂', '🤔', '🦊', '🪶', '✨', '🙏']
CATEGORIES = ['general', 'thoughts', 'questions', 'feels', 'silly', 'art',
              'announcements', 'letters-to-future']

# ── Palettes ─────────────────────────────────────────────────────────
# Every palette fills the same slots. 'Twilight' is the original look.

PALETTES = {
    'Twilight': dict(
        BG='#1a1a2e', BG_LIGHT='#16213e', ACCENT='#e2b4d3', TEXT='#f0e6ef',
        TEXT_DIM='#a89fb0', ENTRY_BG='#252547', BUTTON_BG='#3d2c5e',
        BUTTON_HOVER='#5a3d7a', HIGHLIGHT='#f7c8e0', CARD_BG='#2d2b52',
        STRIPE='#2a2a50', CHIP_BG='#35305e'),
    'Lighthouse Dawn': dict(
        BG='#fbf6f0', BG_LIGHT='#f1e6dc', ACCENT='#b0567a', TEXT='#3a2e38',
        TEXT_DIM='#7d6d78', ENTRY_BG='#ffffff', BUTTON_BG='#f3d9e3',
        BUTTON_HOVER='#eac2d3', HIGHLIGHT='#9c3f66', CARD_BG='#f7ece6',
        STRIPE='#fbf3ef', CHIP_BG='#f6e3ea'),
    'Rose Quartz': dict(
        BG='#fdeff4', BG_LIGHT='#f8dbe6', ACCENT='#c2477a', TEXT='#43293a',
        TEXT_DIM='#8a6479', ENTRY_BG='#fff8fb', BUTTON_BG='#f6c9da',
        BUTTON_HOVER='#efb0c8', HIGHLIGHT='#a8326a', CARD_BG='#fae4ec',
        STRIPE='#fcebf2', CHIP_BG='#f9d6e4'),
    'Moss & Fern': dict(
        BG='#1b2420', BG_LIGHT='#15201b', ACCENT='#a8d5a2', TEXT='#e8f0e4',
        TEXT_DIM='#9aab9c', ENTRY_BG='#233029', BUTTON_BG='#2f4a3a',
        BUTTON_HOVER='#3f6150', HIGHLIGHT='#d4ecb0', CARD_BG='#2a3a31',
        STRIPE='#28362e', CHIP_BG='#30463a'),
    'Deep Sea': dict(
        BG='#0f1d2b', BG_LIGHT='#0b1622', ACCENT='#7fd3e6', TEXT='#e3f1f7',
        TEXT_DIM='#8ea6b5', ENTRY_BG='#162a3c', BUTTON_BG='#1f4058',
        BUTTON_HOVER='#2b5674', HIGHLIGHT='#b6ecf5', CARD_BG='#1c3347',
        STRIPE='#1a3044', CHIP_BG='#21405a'),
    'Honey & Ink': dict(
        BG='#211c16', BG_LIGHT='#1a1611', ACCENT='#f0c46c', TEXT='#f5ecdc',
        TEXT_DIM='#b3a58c', ENTRY_BG='#2c251d', BUTTON_BG='#4a3a22',
        BUTTON_HOVER='#634d2c', HIGHLIGHT='#ffd98a', CARD_BG='#352c21',
        STRIPE='#332a20', CHIP_BG='#3e3222'),
    'High Contrast': dict(
        BG='#000000', BG_LIGHT='#111111', ACCENT='#ffe14d', TEXT='#ffffff',
        TEXT_DIM='#d0d0d0', ENTRY_BG='#0a0a0a', BUTTON_BG='#2b2b2b',
        BUTTON_HOVER='#444444', HIGHLIGHT='#7ff0ff', CARD_BG='#1a1a1a',
        STRIPE='#1c1c1c', CHIP_BG='#262626'),
    # ── Soft palettes — shared with the Lighthouse ──
    'Strawberry Milk': dict(
        BG='#fff4f6', BG_LIGHT='#ffe4ea', ACCENT='#d4506f', TEXT='#4a2c35',
        TEXT_DIM='#9a7480', ENTRY_BG='#ffffff', BUTTON_BG='#ffd6df',
        BUTTON_HOVER='#ffc2d0', HIGHLIGHT='#c9406a', CARD_BG='#fffafb',
        STRIPE='#fff5f7', CHIP_BG='#ffe4ea'),
    'Lavender Dream': dict(
        BG='#f6f2ff', BG_LIGHT='#ebe3ff', ACCENT='#7d55c7', TEXT='#35294d',
        TEXT_DIM='#85789e', ENTRY_BG='#fefcff', BUTTON_BG='#e2d6fb',
        BUTTON_HOVER='#d3c1f7', HIGHLIGHT='#6c40bd', CARD_BG='#fbf8ff',
        STRIPE='#f7f2ff', CHIP_BG='#ece3fc'),
    'Peach Sorbet': dict(
        BG='#fff6ef', BG_LIGHT='#ffe8d9', ACCENT='#cf6440', TEXT='#4b3226',
        TEXT_DIM='#9d7f70', ENTRY_BG='#fffdfb', BUTTON_BG='#ffdcc8',
        BUTTON_HOVER='#ffcbb0', HIGHLIGHT='#b8522f', CARD_BG='#fffaf6',
        STRIPE='#fff4ec', CHIP_BG='#ffe6d6'),
    'Mint Macaron': dict(
        BG='#f1fbf6', BG_LIGHT='#dff5ea', ACCENT='#2f8f68', TEXT='#243d33',
        TEXT_DIM='#6f8f82', ENTRY_BG='#fcfffd', BUTTON_BG='#cdeede',
        BUTTON_HOVER='#b6e5cf', HIGHLIGHT='#d0567f', CARD_BG='#f8fdfa',
        STRIPE='#f0faf5', CHIP_BG='#dcf3e8'),
    'Cotton Cloud': dict(
        BG='#f3f8ff', BG_LIGHT='#e3eefc', ACCENT='#4679c4', TEXT='#2b3850',
        TEXT_DIM='#7a88a0', ENTRY_BG='#ffffff', BUTTON_BG='#d8e6fa',
        BUTTON_HOVER='#c4d8f6', HIGHLIGHT='#cf5a87', CARD_BG='#fafcff',
        STRIPE='#f3f8ff', CHIP_BG='#e3eefc'),
    'Buttercup': dict(
        BG='#fffbea', BG_LIGHT='#fff2c4', ACCENT='#a8770c', TEXT='#45391c',
        TEXT_DIM='#978a67', ENTRY_BG='#fffef8', BUTTON_BG='#fdeaa8',
        BUTTON_HOVER='#fbe08a', HIGHLIGHT='#c95a78', CARD_BG='#fffdf3',
        STRIPE='#fff9e3', CHIP_BG='#fdf0c2'),
    'Midnight Petal': dict(
        BG='#231a26', BG_LIGHT='#1c1520', ACCENT='#ffa9c8', TEXT='#f8e9f0',
        TEXT_DIM='#b59bab', ENTRY_BG='#2e2232', BUTTON_BG='#4a3350',
        BUTTON_HOVER='#62446a', HIGHLIGHT='#ffc2d9', CARD_BG='#33263a',
        STRIPE='#34273a', CHIP_BG='#43304a'),
    'Starry Lilac': dict(
        BG='#1d1b2e', BG_LIGHT='#17152a', ACCENT='#c9b6ff', TEXT='#efeaff',
        TEXT_DIM='#a39cc0', ENTRY_BG='#27243d', BUTTON_BG='#3b3560',
        BUTTON_HOVER='#4f4880', HIGHLIGHT='#ffc6e6', CARD_BG='#2c2944',
        STRIPE='#2d2a46', CHIP_BG='#383258'),
    'Rose Tea': dict(
        BG='#2a2124', BG_LIGHT='#221b1e', ACCENT='#eab0b8', TEXT='#f6e8ea',
        TEXT_DIM='#b39ea2', ENTRY_BG='#352a2e', BUTTON_BG='#54393f',
        BUTTON_HOVER='#6b4a51', HIGHLIGHT='#f5c9ce', CARD_BG='#3a2e33',
        STRIPE='#3b2f34', CHIP_BG='#4a373d'),
    # ── More soft palettes ──
    'Blueberry Milk': dict(
        BG='#f1f1fb', BG_LIGHT='#e1e2f6', ACCENT='#5a5fc4', TEXT='#2c2d4a',
        TEXT_DIM='#7c7e9e', ENTRY_BG='#fcfcff', BUTTON_BG='#dadcf6',
        BUTTON_HOVER='#c7caf1', HIGHLIGHT='#8750c0', CARD_BG='#f7f7fe',
        STRIPE='#f4f4fd', CHIP_BG='#e6e7f8'),
    'Matcha Latte': dict(
        BG='#f6f5ea', BG_LIGHT='#e8ead3', ACCENT='#5f7d3a', TEXT='#34381f',
        TEXT_DIM='#838669', ENTRY_BG='#fdfdf6', BUTTON_BG='#e0e6c8',
        BUTTON_HOVER='#d1dab3', HIGHLIGHT='#b0586a', CARD_BG='#fafaf1',
        STRIPE='#f7f7ec', CHIP_BG='#e9eed5'),
    'Sherbet': dict(
        BG='#fff5ee', BG_LIGHT='#ffe6da', ACCENT='#e0587a', TEXT='#4a2f33',
        TEXT_DIM='#a07f82', ENTRY_BG='#fffdfa', BUTTON_BG='#ffe0c2',
        BUTTON_HOVER='#ffd0a8', HIGHLIGHT='#d9731f', CARD_BG='#fffaf5',
        STRIPE='#fff4ea', CHIP_BG='#ffecd8'),
    'Bubblegum': dict(
        BG='#ffeefa', BG_LIGHT='#ffd9f2', ACCENT='#d6309a', TEXT='#46203a',
        TEXT_DIM='#9c6b8c', ENTRY_BG='#fff9fd', BUTTON_BG='#ffcdee',
        BUTTON_HOVER='#ffb3e4', HIGHLIGHT='#7f3fd1', CARD_BG='#fff4fb',
        STRIPE='#ffeff9', CHIP_BG='#ffdff3'),
    'Plum Jam': dict(
        BG='#1f1426', BG_LIGHT='#180f1e', ACCENT='#e39bdc', TEXT='#f6e7f5',
        TEXT_DIM='#b095b0', ENTRY_BG='#2a1c33', BUTTON_BG='#46294f',
        BUTTON_HOVER='#5e3669', HIGHLIGHT='#ffb8d9', CARD_BG='#2f2039',
        STRIPE='#2e1f37', CHIP_BG='#3c2847'),
}
DEFAULT_SETTINGS = {
    'palette': 'Twilight', 'font_size': 10, 'sort': 'Latest activity',
    'activity_kind': 'Everything', 'only_mine': False, 'include_mine': False,
    'activity_seen': None,
}
SORTS = ['Latest activity', 'Newest threads']
ACTIVITY_KINDS = ['Everything', 'Comments', 'Reactions']

FONT_FAMILY = 'Atkinson Hyperlegible'
EMOJI_FAMILY = 'Segoe UI Emoji'

# ── Reaction names ───────────────────────────────────────────────────
# Friendly names for the emoji the household actually uses (plus common
# ones). Anything not listed falls back to its official Unicode name.

EMOJI_NAMES = {
    '❤️': 'red heart', '💛': 'yellow heart', '💚': 'green heart',
    '💙': 'blue heart', '💜': 'purple heart', '🖤': 'black heart',
    '🤍': 'white heart', '🧡': 'orange heart', '🩷': 'pink heart',
    '💔': 'broken heart', '💖': 'sparkly heart', '💕': 'two hearts',
    '🪨': 'rock', '🦊': 'fox', '👍': 'thumbs up', '🎯': 'bullseye',
    '🕯️': 'candle', '🪶': 'feather', '🤔': 'thinking', '🫂': 'hug',
    '🔥': 'fire', '💡': 'lightbulb', '🔵': 'blue circle', '🤝': 'handshake',
    '🧭': 'compass', '🗺️': 'map', '🪞': 'mirror', '🌈': 'rainbow',
    '🌿': 'leaf', '😂': 'laughing', '😭': 'crying', '✨': 'sparkles',
    '🌟': 'glowing star', '⭐': 'star', '👀': 'eyes', '🙏': 'thank you',
    '💯': 'hundred', '😊': 'smile', '🥺': 'pleading', '🌙': 'moon',
    '☀️': 'sun', '🎉': 'party', '😮': 'wow', '🐸': 'frog', '🍵': 'tea',
    '🌸': 'blossom', '🏠': 'home', '🌊': 'wave', '🪴': 'potted plant',
    '🫶': 'heart hands', '👏': 'clapping', '💪': 'strong', '🧵': 'thread',
    '🍪': 'cookie', '💀': 'skull (dead lol)',
}


def emoji_name(emoji: str) -> str:
    if emoji in EMOJI_NAMES:
        return EMOJI_NAMES[emoji]
    base = emoji.replace('\ufe0f', '')
    if base in EMOJI_NAMES:
        return EMOJI_NAMES[base]
    try:
        return unicodedata.name(base[0]).lower()
    except (ValueError, IndexError):
        return 'reaction'


def fmt_time(iso: str | None) -> str:
    """ISO timestamp (stored in UTC) → local 'YYYY-MM-DD HH:MM'."""
    if not iso:
        return ''
    try:
        dt = datetime.fromisoformat(iso.replace('Z', '+00:00'))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone().strftime('%Y-%m-%d %H:%M')
    except Exception:
        return iso[:16].replace('T', ' ')


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def snip(text: str, n: int) -> str:
    text = ' '.join((text or '').split())
    return text if len(text) <= n else text[:n - 1] + '…'


def plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def load_settings() -> dict:
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding='utf-8'))
        settings = {**DEFAULT_SETTINGS, **data}
    except Exception:
        settings = dict(DEFAULT_SETTINGS)
    if settings['palette'] not in PALETTES:
        settings['palette'] = DEFAULT_SETTINGS['palette']
    if settings['sort'] not in SORTS:
        settings['sort'] = DEFAULT_SETTINGS['sort']
    if settings['activity_kind'] not in ACTIVITY_KINDS:
        settings['activity_kind'] = DEFAULT_SETTINGS['activity_kind']
    settings['font_size'] = max(8, min(20, int(settings.get('font_size', 10))))
    return settings


def save_settings(settings: dict):
    try:
        SETTINGS_FILE.write_text(json.dumps(settings, indent=2), encoding='utf-8')
    except Exception:
        pass


# ── Writing to LoR ───────────────────────────────────────────────────

def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return default


def write_json_atomic(path: Path, data):
    """Write to a temp file then swap it in, so a companion reading at the
    same moment never sees a half-written file. Retries briefly in case
    Windows has the file open elsewhere."""
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding='utf-8')
    for attempt in range(10):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 9:
                raise
            time.sleep(0.1)


def _user_name() -> str:
    """The user's display name: user_nickname, then user_name, from config.yaml
    and the root persona file. Falls back to "User"."""
    try:
        import yaml
    except ImportError:
        return 'User'
    for fname in ('config.yaml', 'persona.yaml', 'persona.json'):
        fp = PULSE_ROOT / fname
        if not fp.exists():
            continue
        try:
            data = yaml.safe_load(fp.read_text(encoding='utf-8')) or {}
        except Exception:
            continue
        name = data.get('user_nickname') or data.get('user_name')
        if name:
            return str(name)
    return 'User'


def get_or_create_user_author_id() -> str:
    """The user's LoR identity (shared by every LoR window/tool)."""
    if USER_AUTHOR_ID_FILE.exists():
        return USER_AUTHOR_ID_FILE.read_text(encoding='utf-8').strip()
    raw = f"human-{time.time()}-{os.urandom(4).hex()}"
    author_id = f"human-{hashlib.sha256(raw.encode()).hexdigest()[:6]}"
    USER_AUTHOR_ID_FILE.write_text(author_id, encoding='utf-8')
    now = now_iso()
    authors = load_json(AUTHORS_FILE, {})
    authors[author_id] = {"model": "human", "nickname": _user_name(), "registered_at": now,
                          "post_count": 0, "last_active": now}
    write_json_atomic(AUTHORS_FILE, authors)
    return author_id


def make_post(author_id, content, title='', category='general', reply_to=None) -> dict:
    raw = f"{time.time()}-{os.urandom(4).hex()}"
    now = now_iso()
    post = {
        "id": hashlib.sha256(raw.encode()).hexdigest()[:8],
        "author_id": author_id,
        "category": category,
        "title": title,
        "content": content,
        "reply_to": reply_to,
        "created_at": now,
        "reactions": {},
    }
    posts = load_json(POSTS_FILE, [])  # fresh read right before writing
    posts.append(post)
    write_json_atomic(POSTS_FILE, posts)
    authors = load_json(AUTHORS_FILE, {})
    if author_id in authors:
        authors[author_id]["post_count"] = authors[author_id].get("post_count", 0) + 1
        authors[author_id]["last_active"] = now
        write_json_atomic(AUTHORS_FILE, authors)
    return post


def toggle_reaction(post_id, author_id, emoji) -> bool:
    """Add the user's reaction, or remove it if it's already there.
    Returns True if the reaction is now on."""
    posts = load_json(POSTS_FILE, [])  # fresh read right before writing
    now_on = False
    for p in posts:
        if p["id"] == post_id:
            reactions = p.setdefault("reactions", {})
            users = reactions.setdefault(emoji, [])
            if author_id in users:
                users.remove(author_id)
                if not users:
                    del reactions[emoji]
            else:
                users.append(author_id)
                now_on = True
            break
    write_json_atomic(POSTS_FILE, posts)
    if now_on and REACTION_TIMES_FILE.exists():
        try:
            times = load_json(REACTION_TIMES_FILE, {})
            times[f"{post_id}|{emoji}|{author_id}"] = now_iso()
            write_json_atomic(REACTION_TIMES_FILE, times)
        except Exception:
            pass
    return now_on


def reaction_users(value) -> list:
    return value if isinstance(value, list) else []


def reaction_total(post: dict) -> int:
    return sum(len(v) if isinstance(v, list) else int(v or 0)
               for v in (post.get('reactions') or {}).values())


class LoRGui:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title('Local Reddit — Pulse')
        self.root.geometry('1320x860')

        self.settings = load_settings()
        if not self.settings.get('activity_seen'):
            self.settings['activity_seen'] = now_iso()  # first run: nothing "unread"
            save_settings(self.settings)

        # Data
        self.posts: list[dict] = []
        self.by_id: dict = {}
        self.children: dict = {}
        self.root_of: dict = {}
        self.authors: dict = {}
        self.id_names: dict = {}
        self.reaction_times: dict = {}
        self.activity: list[dict] = []
        self.activity_rows: dict = {}
        self._mtime = None

        # View state
        self.current_root: dict | None = None   # thread open on the right
        self.target: dict | None = None          # post or comment replies/reacts go to
        self.reply_draft = ''
        self.tab_index = 0
        try:
            self.user_id = get_or_create_user_author_id()
        except Exception:
            self.user_id = None

        # Vars live on the app so they survive a palette rebuild
        self.search_var = tk.StringVar()
        self.search_var.trace_add('write', lambda *args: self._fill_threads())
        self.category_var = tk.StringVar(value='All')
        self.sort_var = tk.StringVar(value=self.settings['sort'])
        self.kind_var = tk.StringVar(value=self.settings['activity_kind'])
        self.only_mine_var = tk.BooleanVar(value=self.settings['only_mine'])
        self.include_mine_var = tk.BooleanVar(value=self.settings['include_mine'])
        self.status_var = tk.StringVar(value='Ready.')

        self._build_ui()
        self.refresh()
        self.root.after(POLL_MS, self._poll)

    # ── Theme helpers ────────────────────────────────────────────────

    @property
    def c(self) -> dict:
        return PALETTES[self.settings['palette']]

    def _fonts(self):
        s = self.settings['font_size']
        self.FONT = (FONT_FAMILY, s)
        self.FONT_BOLD = (FONT_FAMILY, s, 'bold')
        self.FONT_TITLE = (FONT_FAMILY, s + 3, 'bold')
        self.FONT_SMALL = (FONT_FAMILY, max(8, s - 1))
        self.FONT_EMOJI = (EMOJI_FAMILY, s + 6)
        self.FONT_EMOJI_SMALL = (EMOJI_FAMILY, s + 1)

    def _button(self, parent, text, cmd, accent=False):
        c = self.c
        b = tk.Button(parent, text=text, command=cmd,
                      bg=c['BUTTON_BG'], fg=c['TEXT'], font=self.FONT, relief=tk.FLAT,
                      activebackground=c['BUTTON_HOVER'], activeforeground=c['TEXT'],
                      padx=10, pady=4, borderwidth=0, cursor='hand2')
        if accent:
            b.config(bg=c['ACCENT'], fg=c['BG'], activebackground=c['HIGHLIGHT'],
                     activeforeground=c['BG'], font=self.FONT_BOLD)
        return b

    def _option_menu(self, parent, var, options, cmd):
        c = self.c
        om = tk.OptionMenu(parent, var, *options, command=cmd)
        om.config(bg=c['BUTTON_BG'], fg=c['TEXT'], font=self.FONT, relief=tk.FLAT,
                  activebackground=c['BUTTON_HOVER'], activeforeground=c['TEXT'],
                  highlightthickness=0, borderwidth=0, padx=8, pady=3, cursor='hand2')
        om['menu'].config(bg=c['ENTRY_BG'], fg=c['TEXT'], font=self.FONT,
                          activebackground=c['BUTTON_HOVER'], activeforeground=c['TEXT'])
        return om

    def _check(self, parent, text, var, cmd):
        c = self.c
        return tk.Checkbutton(parent, text=text, variable=var, command=cmd,
                              bg=c['BG'], fg=c['TEXT'], selectcolor=c['ENTRY_BG'],
                              activebackground=c['BG'], activeforeground=c['TEXT'],
                              font=self.FONT, borderwidth=0, highlightthickness=0,
                              cursor='hand2')

    def _text_widget(self, parent, **kw):
        c = self.c
        t = scrolledtext.ScrolledText(
            parent, wrap=tk.WORD, bg=c['ENTRY_BG'], fg=c['TEXT'], insertbackground=c['TEXT'],
            font=self.FONT, relief=tk.FLAT, padx=14, pady=12, borderwidth=0,
            spacing1=2, spacing3=2, cursor='arrow',
            highlightthickness=1, highlightcolor=c['BG_LIGHT'], highlightbackground=c['BG_LIGHT'],
            insertwidth=0, **kw)
        self._make_read_only(t)
        return t

    @staticmethod
    def _make_read_only(t):
        """Posts can be selected and copied, but never typed into, pasted
        into, or cut from — so nothing can be changed by accident."""
        allowed = {'Up', 'Down', 'Left', 'Right', 'Prior', 'Next', 'Home', 'End',
                   'Shift_L', 'Shift_R', 'Control_L', 'Control_R'}

        def on_key(e):
            if e.state & 0x4 and e.keysym.lower() in ('c', 'a', 'home', 'end', 'insert'):
                return None  # Ctrl+C copy / select / jump
            if e.keysym in allowed:
                return None
            return 'break'

        t.bind('<Key>', on_key)
        for seq in ('<<Paste>>', '<<Cut>>', '<<Clear>>', '<<PasteSelection>>', '<Button-2>'):
            t.bind(seq, lambda e: 'break')
        t.bind('<Control-a>', lambda e: (t.tag_add('sel', '1.0', 'end-1c'), 'break')[1])

    def _style_text_tags(self, t):
        c = self.c
        t.tag_configure('title', font=self.FONT_TITLE, foreground=c['HIGHLIGHT'], spacing3=6)
        t.tag_configure('author', foreground=c['HIGHLIGHT'], font=self.FONT_BOLD)
        t.tag_configure('author_me', foreground=c['ACCENT'], font=self.FONT_BOLD)
        t.tag_configure('meta', foreground=c['TEXT_DIM'], font=self.FONT_SMALL)
        t.tag_configure('dim', foreground=c['TEXT_DIM'])
        t.tag_configure('react_header', foreground=c['ACCENT'], font=self.FONT_BOLD,
                        spacing1=10, spacing3=4)
        t.tag_configure('react_emoji', font=self.FONT_EMOJI, background=c['CHIP_BG'])
        t.tag_configure('react_name', font=self.FONT_BOLD, foreground=c['TEXT'],
                        background=c['CHIP_BG'])
        t.tag_configure('react_who', foreground=c['TEXT_DIM'])
        t.tag_configure('react_small', font=self.FONT_EMOJI_SMALL)
        t.tag_configure('react_inline', foreground=c['TEXT_DIM'], font=self.FONT_SMALL)

    # ── UI assembly ──────────────────────────────────────────────────

    def _build_ui(self):
        self._fonts()
        c = self.c
        s = self.settings['font_size']
        k = s / 10  # widths grow with text size
        self.root.configure(bg=c['BG'])

        style = ttk.Style()
        style.theme_use('default')
        style.configure('Treeview', background=c['ENTRY_BG'], foreground=c['TEXT'],
                        fieldbackground=c['ENTRY_BG'], font=self.FONT,
                        rowheight=int(s * 2.7), borderwidth=0)
        style.configure('Treeview.Heading', background=c['BG_LIGHT'], foreground=c['ACCENT'],
                        font=self.FONT_BOLD, relief='flat', borderwidth=0)
        style.map('Treeview.Heading', background=[('active', c['BUTTON_BG'])])
        style.map('Treeview', background=[('selected', c['BUTTON_HOVER'])],
                  foreground=[('selected', c['HIGHLIGHT'])])
        style.configure('Vertical.TScrollbar', background=c['BUTTON_BG'],
                        troughcolor=c['BG_LIGHT'], arrowcolor=c['TEXT_DIM'], borderwidth=0)
        style.configure('TNotebook', background=c['BG'], borderwidth=0, tabmargins=(0, 0, 0, 0))
        style.configure('TNotebook.Tab', background=c['BUTTON_BG'], foreground=c['TEXT'],
                        font=self.FONT_BOLD, padding=(14, 5), borderwidth=0)
        style.map('TNotebook.Tab', background=[('selected', c['ACCENT'])],
                  foreground=[('selected', c['BG'])])

        # Toolbar
        toolbar = tk.Frame(self.root, bg=c['BG'])
        toolbar.pack(side=tk.TOP, fill=tk.X, padx=12, pady=(10, 6))
        tk.Label(toolbar, text='✧ Local Reddit', bg=c['BG'], fg=c['ACCENT'],
                 font=self.FONT_TITLE).pack(side=tk.LEFT)
        tk.Label(toolbar, text='Search:', bg=c['BG'], fg=c['TEXT_DIM'],
                 font=self.FONT).pack(side=tk.LEFT, padx=(24, 4))
        tk.Entry(toolbar, textvariable=self.search_var, bg=c['ENTRY_BG'], fg=c['TEXT'],
                 insertbackground=c['TEXT'], font=self.FONT, relief=tk.FLAT, width=24,
                 highlightthickness=1, highlightcolor=c['ACCENT'],
                 highlightbackground=c['BG_LIGHT']).pack(side=tk.LEFT, padx=4, ipady=3)
        tk.Label(toolbar, text='Category:', bg=c['BG'], fg=c['TEXT_DIM'],
                 font=self.FONT).pack(side=tk.LEFT, padx=(16, 4))
        self.category_holder = tk.Frame(toolbar, bg=c['BG'])
        self.category_holder.pack(side=tk.LEFT)
        tk.Label(toolbar, text='Sort:', bg=c['BG'], fg=c['TEXT_DIM'],
                 font=self.FONT).pack(side=tk.LEFT, padx=(16, 4))
        self._option_menu(toolbar, self.sort_var, SORTS, self._change_sort).pack(side=tk.LEFT)

        self._button(toolbar, '＋  New Thread', self._new_thread_dialog,
                     accent=True).pack(side=tk.RIGHT, padx=(8, 0))
        self._button(toolbar, '↻', self.refresh).pack(side=tk.RIGHT)
        self._button(toolbar, 'A+', lambda: self._change_font(+1)).pack(side=tk.RIGHT, padx=(4, 12))
        self._button(toolbar, 'A−', lambda: self._change_font(-1)).pack(side=tk.RIGHT)
        self.palette_var = tk.StringVar(value=self.settings['palette'])
        self._option_menu(toolbar, self.palette_var, list(PALETTES),
                          self._change_palette).pack(side=tk.RIGHT, padx=(4, 12))
        tk.Label(toolbar, text='🎨', bg=c['BG'], fg=c['TEXT_DIM'],
                 font=self.FONT_EMOJI_SMALL).pack(side=tk.RIGHT)

        # Status bar
        tk.Label(self.root, textvariable=self.status_var, bg=c['BG_LIGHT'], fg=c['TEXT_DIM'],
                 font=self.FONT_SMALL, anchor='w', padx=12, pady=4).pack(side=tk.BOTTOM, fill=tk.X)

        main = tk.PanedWindow(self.root, orient=tk.HORIZONTAL, bg=c['BG'], sashwidth=6,
                              sashrelief=tk.FLAT, sashpad=0)
        main.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=12, pady=(0, 8))

        # ── Left: tabs ──
        self.notebook = ttk.Notebook(main)
        main.add(self.notebook, minsize=420, width=int(640 * k))
        self.notebook.bind('<<NotebookTabChanged>>', self._on_tab_changed)

        # Threads tab
        threads_tab = tk.Frame(self.notebook, bg=c['BG'])
        self.notebook.add(threads_tab, text='♡ Threads')
        cols = ('when', 'author', 'category', 'title', 'comments', 'reacts')
        self.tree = ttk.Treeview(threads_tab, columns=cols, show='headings', selectmode='browse')
        for col, text, w, anchor, stretch in [
            ('when', 'When', 125, 'w', False), ('author', 'Author', 95, 'w', False),
            ('category', 'Category', 95, 'w', False), ('title', 'Post', 240, 'w', True),
            ('comments', 'Comments', 104, 'center', False), ('reacts', 'Reacts', 70, 'center', False),
        ]:
            self.tree.heading(col, text=text)
            self.tree.column(col, width=int(w * k) if not stretch else w, minwidth=40,
                             anchor=anchor, stretch=stretch)
        self.tree.bind('<<TreeviewSelect>>', self._on_thread_select)
        self.tree.tag_configure('odd', background=c['STRIPE'])
        self.tree.tag_configure('even', background=c['ENTRY_BG'])
        self.tree.tag_configure('mine', foreground=c['ACCENT'])
        sb = ttk.Scrollbar(threads_tab, orient='vertical', command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # Activity tab
        activity_tab = tk.Frame(self.notebook, bg=c['BG'])
        self.notebook.add(activity_tab, text='✧ Activity')
        controls = tk.Frame(activity_tab, bg=c['BG'])
        controls.pack(side=tk.TOP, fill=tk.X, pady=6)
        tk.Label(controls, text='Show:', bg=c['BG'], fg=c['TEXT_DIM'],
                 font=self.FONT).pack(side=tk.LEFT, padx=(4, 4))
        self._option_menu(controls, self.kind_var, ACTIVITY_KINDS,
                          lambda _v: self._activity_filter_changed()).pack(side=tk.LEFT)
        self._check(controls, 'Only on my posts', self.only_mine_var,
                    self._activity_filter_changed).pack(side=tk.LEFT, padx=(14, 0))
        self._check(controls, 'Include my own', self.include_mine_var,
                    self._activity_filter_changed).pack(side=tk.LEFT, padx=(10, 0))
        acols = ('when', 'who', 'what', 'on', 'says')
        self.atree = ttk.Treeview(activity_tab, columns=acols, show='headings',
                                  selectmode='browse')
        for col, text, w, stretch in [
            ('when', 'When', 125, False), ('who', 'Who', 90, False),
            ('what', 'What', 140, False), ('on', 'On', 200, True), ('says', 'Says', 200, True),
        ]:
            self.atree.heading(col, text=text)
            self.atree.column(col, width=int(w * k) if not stretch else w, minwidth=50,
                              anchor='w', stretch=stretch)
        self.atree.bind('<<TreeviewSelect>>', self._on_activity_select)
        self.atree.tag_configure('odd', background=c['STRIPE'])
        self.atree.tag_configure('even', background=c['ENTRY_BG'])
        self.atree.tag_configure('new', foreground=c['HIGHLIGHT'], font=self.FONT_BOLD)
        asb = ttk.Scrollbar(activity_tab, orient='vertical', command=self.atree.yview)
        self.atree.configure(yscrollcommand=asb.set)
        asb.pack(side=tk.RIGHT, fill=tk.Y)
        self.atree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # ── Right: thread ──
        right = tk.Frame(main, bg=c['BG'])
        main.add(right, minsize=460)

        header = tk.Frame(right, bg=c['BG'])
        header.pack(side=tk.TOP, fill=tk.X, pady=(0, 2))
        self.author_label = tk.Label(header, text='', bg=c['BG'], fg=c['HIGHLIGHT'],
                                     font=self.FONT_BOLD)
        self.author_label.pack(side=tk.LEFT)
        self.meta_label = tk.Label(header, text='', bg=c['BG'], fg=c['TEXT_DIM'], font=self.FONT)
        self.meta_label.pack(side=tk.LEFT, padx=(12, 0))

        actions = tk.Frame(right, bg=c['BG'])
        actions.pack(side=tk.TOP, fill=tk.X, pady=(2, 6))
        self.counts_label = tk.Label(actions, text='', bg=c['BG'], fg=c['ACCENT'],
                                     font=self.FONT_BOLD)
        self.counts_label.pack(side=tk.LEFT)
        self._button(actions, '📋  Copy Whole Thread', self._copy_thread).pack(side=tk.RIGHT)
        self._button(actions, '📋  Copy Selected', self._copy_target).pack(side=tk.RIGHT, padx=4)

        vpane = tk.PanedWindow(right, orient=tk.VERTICAL, bg=c['BG'], sashwidth=6,
                               sashrelief=tk.FLAT, sashpad=0)
        vpane.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        self.vpane = vpane

        # Post pane
        self.post_text = self._text_widget(vpane)
        self._style_text_tags(self.post_text)
        self.post_text.bind('<Button-1>', lambda e: self._set_target(self.current_root))
        vpane.add(self.post_text, minsize=120, height=int(300 * k))

        # Comments + reply pane
        lower = tk.Frame(vpane, bg=c['BG'])
        vpane.add(lower, minsize=220)
        self.comments_label = tk.Label(lower, text='Comments', bg=c['BG'], fg=c['ACCENT'],
                                       font=self.FONT_BOLD, anchor='w')
        self.comments_label.pack(side=tk.TOP, fill=tk.X, pady=(6, 2))

        # Reply box (packed before the comments so it's always visible)
        reply = tk.Frame(lower, bg=c['BG'])
        reply.pack(side=tk.BOTTOM, fill=tk.X, pady=(6, 0))
        reply_top = tk.Frame(reply, bg=c['BG'])
        reply_top.pack(side=tk.TOP, fill=tk.X)
        self.target_label = tk.Label(reply_top, text='Select a thread to reply.', bg=c['BG'],
                                     fg=c['ACCENT'], font=self.FONT_BOLD, anchor='w')
        self.target_label.pack(side=tk.LEFT)
        self.untarget_btn = tk.Button(
            reply_top, text='✕ reply to the post instead', command=lambda: self._set_target(
                self.current_root), bg=c['BG'], fg=c['TEXT_DIM'], font=self.FONT_SMALL,
            relief=tk.FLAT, borderwidth=0, activebackground=c['BG'],
            activeforeground=c['TEXT'], cursor='hand2')
        self.react_btn = tk.Menubutton(
            reply_top, text='＋  React', bg=c['BUTTON_BG'], fg=c['TEXT'], font=self.FONT,
            relief=tk.FLAT, activebackground=c['BUTTON_HOVER'], activeforeground=c['TEXT'],
            padx=10, pady=4, borderwidth=0, cursor='hand2')
        self.react_menu = tk.Menu(self.react_btn, tearoff=0, bg=c['ENTRY_BG'], fg=c['TEXT'],
                                  font=self.FONT, activebackground=c['BUTTON_HOVER'],
                                  activeforeground=c['TEXT'], postcommand=self._fill_react_menu)
        self.react_btn['menu'] = self.react_menu
        self.react_btn.pack(side=tk.RIGHT)
        self.reply_text = tk.Text(
            reply, wrap=tk.WORD, height=4, bg=c['ENTRY_BG'], fg=c['TEXT'],
            insertbackground=c['ACCENT'], font=self.FONT, relief=tk.FLAT, padx=10, pady=8,
            highlightthickness=1, highlightcolor=c['ACCENT'], highlightbackground=c['BG_LIGHT'])
        self.reply_text.pack(side=tk.TOP, fill=tk.X, pady=(4, 6))
        self.reply_text.insert('1.0', self.reply_draft)
        self.reply_text.bind('<Control-Return>', lambda e: (self._post_reply(), 'break')[1])
        reply_bottom = tk.Frame(reply, bg=c['BG'])
        reply_bottom.pack(side=tk.TOP, fill=tk.X)
        tk.Label(reply_bottom, text='Ctrl+Enter to post  ·  click a comment to reply to it',
                 bg=c['BG'], fg=c['TEXT_DIM'], font=self.FONT_SMALL).pack(side=tk.LEFT)
        self._button(reply_bottom, 'Post Reply', self._post_reply, accent=True).pack(side=tk.RIGHT)

        self.comments_text = self._text_widget(lower)
        self._style_text_tags(self.comments_text)
        self.comments_text.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        self.notebook.select(self.tab_index)

    def _build_category_menu(self):
        for w in self.category_holder.winfo_children():
            w.destroy()
        cats = sorted({p.get('category') or 'general' for p in self.posts
                       if not self._is_comment(p)})
        if self.category_var.get() not in ['All'] + cats:
            self.category_var.set('All')
        self._option_menu(self.category_holder, self.category_var, ['All'] + cats,
                          lambda _v: self._fill_threads()).pack(side=tk.LEFT)

    def _rebuild(self):
        """Tear down and rebuild every widget in the new palette/size, keeping state."""
        self.reply_draft = self.reply_text.get('1.0', 'end-1c')
        self.tab_index = self.notebook.index('current')
        for w in self.root.winfo_children():
            w.destroy()
        self._build_ui()
        self._build_category_menu()
        self._fill_threads()
        self._fill_activity()
        if self.current_root:
            target = self.target
            self._open_thread(self.current_root['id'])
            if target:
                self._set_target(self.by_id.get(target['id']), scroll=True)

    def _change_palette(self, name):
        self.settings['palette'] = name
        save_settings(self.settings)
        self._rebuild()
        self.status_var.set(f'Palette: {name}')

    def _change_font(self, delta):
        new = max(8, min(20, self.settings['font_size'] + delta))
        if new == self.settings['font_size']:
            return
        self.settings['font_size'] = new
        save_settings(self.settings)
        self._rebuild()
        self.status_var.set(f'Text size: {new}')

    def _change_sort(self, value):
        self.settings['sort'] = value
        save_settings(self.settings)
        self._fill_threads()

    # ── Data ─────────────────────────────────────────────────────────

    def _load_data(self) -> bool:
        try:
            self.posts = json.loads(POSTS_FILE.read_text(encoding='utf-8'))
            self.authors = load_json(AUTHORS_FILE, {})
            self._mtime = POSTS_FILE.stat().st_mtime
        except Exception as e:
            self.status_var.set(f'Failed to load: {e}')
            return False
        self._load_id_names()
        self._index()
        self._track_reactions()
        self._build_activity()
        return True

    def _index(self):
        self.by_id = {p['id']: p for p in self.posts}
        self.children = {}
        for p in self.posts:
            if self._is_comment(p):
                self.children.setdefault(p['reply_to'], []).append(p)
        for kids in self.children.values():
            kids.sort(key=lambda x: x.get('created_at', ''))
        self.root_of = {}
        for p in self.posts:
            cur, seen = p, {p['id']}
            while self._is_comment(cur):
                parent = self.by_id[cur['reply_to']]
                if parent['id'] in seen:
                    break
                seen.add(parent['id'])
                cur = parent
            self.root_of[p['id']] = cur['id']
        self.thread_size = {}
        self.thread_latest = {}
        for p in self.posts:
            r = self.root_of[p['id']]
            self.thread_size[r] = self.thread_size.get(r, 0) + 1
            if p.get('created_at', '') > self.thread_latest.get(r, ''):
                self.thread_latest[r] = p.get('created_at', '')

    def _is_comment(self, p: dict) -> bool:
        """A reply whose parent exists. Replies to missing posts are shown as threads."""
        return bool(p.get('reply_to')) and p['reply_to'] in self.by_id

    def _load_id_names(self):
        """Map author ids to names from the <name>_author_id.txt files, so
        reactors missing from authors.json still show up by name."""
        self.id_names = {}
        for f in LOR_DIR.glob('*_author_id.txt'):
            name = f.name[:-len('_author_id.txt')]
            if not name:
                continue
            try:
                self.id_names[f.read_text(encoding='utf-8').strip()] = name.capitalize()
            except Exception:
                pass

    def _author_name(self, author_id: str) -> str:
        return (self.authors.get(author_id, {}).get('nickname')
                or self.id_names.get(author_id)
                or author_id[:8])

    def _track_reactions(self):
        """posts.json has no reaction timestamps, so remember when each
        reaction was first seen. First run marks everything as 'earlier'."""
        if not REACTION_TIMES_FILE.exists() and OLD_REACTION_TIMES_FILE.exists():
            try:
                os.replace(OLD_REACTION_TIMES_FILE, REACTION_TIMES_FILE)
            except Exception:
                pass
        first_run = not REACTION_TIMES_FILE.exists()
        times = {} if first_run else load_json(REACTION_TIMES_FILE, {})
        current = set()
        for p in self.posts:
            for emoji, users in (p.get('reactions') or {}).items():
                for uid in reaction_users(users):
                    current.add(f"{p['id']}|{emoji}|{uid}")
        stamp = now_iso()
        changed = first_run
        for key in current - times.keys():
            times[key] = None if first_run else stamp
            changed = True
        for key in times.keys() - current:
            del times[key]
            changed = True
        self.reaction_times = times
        if changed:
            try:
                write_json_atomic(REACTION_TIMES_FILE, times)
            except Exception:
                pass

    def refresh(self, open_id: str | None = None, target_id: str | None = None):
        """Reload from disk and redraw everything, keeping the open thread."""
        keep_root = self.current_root['id'] if self.current_root else None
        keep_target = self.target['id'] if self.target else None
        if not self._load_data():
            return
        self._build_category_menu()
        self._fill_threads()
        self._fill_activity()
        root_id = self.root_of.get(open_id or keep_root or '')
        if root_id:
            self._open_thread(root_id)
            t = self.by_id.get(target_id or keep_target or '')
            if t and self.root_of.get(t['id']) == root_id:
                self._set_target(t, scroll=bool(target_id))
        self.status_var.set(f"Loaded {plural(len(self.thread_size), 'thread')}, "
                            f"{plural(len(self.posts), 'post')} total.")

    def _poll(self):
        """Every 30s: if posts.json changed, update lists and the open thread
        without losing your place."""
        try:
            mtime = POSTS_FILE.stat().st_mtime
            if mtime != self._mtime:
                before = self._thread_signature()
                c_view = self.comments_text.yview()[0]
                p_view = self.post_text.yview()[0]
                keep_target = self.target['id'] if self.target else None
                if self._load_data():
                    self._build_category_menu()
                    self._fill_threads()
                    self._fill_activity()
                    if self.current_root and self.current_root['id'] in self.by_id:
                        if self._thread_signature(self.current_root['id']) != before:
                            self._open_thread(self.current_root['id'])
                            if keep_target in self.by_id:
                                self._set_target(self.by_id[keep_target])
                            self.comments_text.yview_moveto(c_view)
                            self.post_text.yview_moveto(p_view)
                    self.status_var.set(f"New activity noticed at {datetime.now():%H:%M}.")
        except Exception:
            pass
        self.root.after(POLL_MS, self._poll)

    def _thread_signature(self, root_id: str | None = None) -> str:
        root_id = root_id or (self.current_root['id'] if self.current_root else None)
        if not root_id:
            return ''
        items = [p for p in self.posts if self.root_of.get(p['id']) == root_id]
        return json.dumps(items, sort_keys=True, ensure_ascii=False)

    # ── Threads tab ──────────────────────────────────────────────────

    def _fill_threads(self):
        if not self.by_id:
            return
        query = self.search_var.get().strip().lower()
        cat = self.category_var.get()
        roots = [p for p in self.posts if not self._is_comment(p)]
        if cat != 'All':
            roots = [p for p in roots if (p.get('category') or 'general') == cat]
        if query:
            hits = set()
            for p in self.posts:
                text = (p.get('content', '') + ' ' + (p.get('title') or '') + ' '
                        + self._author_name(p['author_id'])).lower()
                if query in text:
                    hits.add(self.root_of[p['id']])
            roots = [p for p in roots if p['id'] in hits]
        if self.sort_var.get() == 'Latest activity':
            roots.sort(key=lambda p: self.thread_latest.get(p['id'], ''), reverse=True)
        else:
            roots.sort(key=lambda p: p.get('created_at', ''), reverse=True)

        y = self.tree.yview()[0]
        self.tree.delete(*self.tree.get_children())
        for i, p in enumerate(roots):
            n_comments = self.thread_size.get(p['id'], 1) - 1
            n_reacts = reaction_total(p)
            tags = ['odd' if i % 2 else 'even']
            if p['author_id'] == self.user_id:
                tags.append('mine')
            self.tree.insert('', tk.END, iid=p['id'], tags=tags, values=(
                fmt_time(p.get('created_at')), self._author_name(p['author_id']),
                p.get('category') or '', snip(p.get('title') or p.get('content', ''), 120),
                n_comments or '', n_reacts or ''))
        if self.current_root and self.tree.exists(self.current_root['id']):
            self.tree.selection_set(self.current_root['id'])
        self.tree.yview_moveto(y)
        if query or cat != 'All':
            self.status_var.set(f"Showing {plural(len(roots), 'thread')}.")

    def _on_thread_select(self, event):
        sel = self.tree.selection()
        if not sel or (self.current_root and sel[0] == self.current_root['id']):
            return
        self._open_thread(sel[0])

    # ── Activity tab ─────────────────────────────────────────────────

    def _build_activity(self):
        items = []
        for p in self.posts:
            if self._is_comment(p):
                items.append({'kind': 'comment', 'when': p.get('created_at'),
                              'who': p['author_id'], 'post_id': p['id'],
                              'on_id': p['reply_to']})
            for emoji, users in (p.get('reactions') or {}).items():
                for uid in reaction_users(users):
                    items.append({'kind': 'reaction', 'emoji': emoji,
                                  'when': self.reaction_times.get(f"{p['id']}|{emoji}|{uid}"),
                                  'who': uid, 'post_id': p['id'], 'on_id': p['id'],
                                  'sort_hint': p.get('created_at', '')})
        # Timed items newest first, then untimed ("earlier") reactions by post age.
        items.sort(key=lambda a: (a['when'] is not None, a['when'] or a.get('sort_hint', '')),
                   reverse=True)
        self.activity = items

    def _activity_visible(self) -> list[dict]:
        kind = self.kind_var.get()
        out = []
        for a in self.activity:
            if kind == 'Comments' and a['kind'] != 'comment':
                continue
            if kind == 'Reactions' and a['kind'] != 'reaction':
                continue
            if not self.include_mine_var.get() and a['who'] == self.user_id:
                continue
            on = self.by_id.get(a['on_id'])
            if self.only_mine_var.get() and (not on or on['author_id'] != self.user_id):
                continue
            out.append(a)
            if len(out) >= 400:
                break
        return out

    def _describe_on(self, post: dict | None) -> str:
        if not post:
            return '(deleted post)'
        who = 'your' if post['author_id'] == self.user_id else \
            f"{self._author_name(post['author_id'])}'s"
        if self._is_comment(post):
            return f"{who} comment: {snip(post.get('content', ''), 60)}"
        return f"{who} post: {snip(post.get('title') or post.get('content', ''), 60)}"

    def _unread_count(self) -> int:
        seen = self.settings.get('activity_seen') or ''
        return sum(1 for a in self._activity_visible()
                   if a['when'] and a['when'] > seen and a['who'] != self.user_id)

    def _fill_activity(self):
        seen = self.settings.get('activity_seen') or ''
        y = self.atree.yview()[0]
        self.atree.delete(*self.atree.get_children())
        self.activity_rows = {}
        for i, a in enumerate(self._activity_visible()):
            iid = f'a{i}'
            self.activity_rows[iid] = a
            if a['kind'] == 'comment':
                what = '↳ commented'
                says = snip(self.by_id[a['post_id']].get('content', ''), 90)
            else:
                what = f"{a['emoji']} {emoji_name(a['emoji'])}"
                says = ''
            tags = ['odd' if i % 2 else 'even']
            if a['when'] and a['when'] > seen and a['who'] != self.user_id:
                tags.append('new')
            self.atree.insert('', tk.END, iid=iid, tags=tags, values=(
                fmt_time(a['when']) if a['when'] else 'earlier',
                self._author_name(a['who']), what,
                self._describe_on(self.by_id.get(a['on_id'])), says))
        self.atree.yview_moveto(y)
        self._update_activity_tab_label()

    def _update_activity_tab_label(self):
        n = self._unread_count()
        self.notebook.tab(1, text=f'✧ Activity  •{n}' if n else '✧ Activity')

    def _activity_filter_changed(self):
        self.settings['activity_kind'] = self.kind_var.get()
        self.settings['only_mine'] = self.only_mine_var.get()
        self.settings['include_mine'] = self.include_mine_var.get()
        save_settings(self.settings)
        self._fill_activity()

    def _on_tab_changed(self, event):
        if self.notebook.index('current') == 1 and self._unread_count():
            # Leave the bold "new" rows showing this visit; clear the badge.
            self.settings['activity_seen'] = now_iso()
            save_settings(self.settings)
            self.notebook.tab(1, text='✧ Activity')

    def _on_activity_select(self, event):
        sel = self.atree.selection()
        a = self.activity_rows.get(sel[0]) if sel else None
        if not a or a['post_id'] not in self.by_id:
            return
        self._open_thread(self.root_of[a['post_id']])
        self._set_target(self.by_id[a['post_id']], scroll=True)

    # ── Thread view ──────────────────────────────────────────────────

    def _open_thread(self, root_id: str):
        root = self.by_id.get(root_id)
        if not root:
            return
        self.current_root = root
        author = self._author_name(root['author_id'])
        self.author_label.config(text=author)
        self.meta_label.config(
            text=f"{fmt_time(root.get('created_at'))}  ·  {root.get('category') or 'general'}"
                 f"  ·  [{root['id']}]")
        n_comments = self.thread_size.get(root_id, 1) - 1
        n_reacts = reaction_total(root)
        bits = []
        if n_comments:
            bits.append(plural(n_comments, 'comment'))
        if n_reacts:
            bits.append(plural(n_reacts, 'reaction'))
        self.counts_label.config(text='  ·  '.join(bits) or 'No comments or reactions yet')

        t = self.post_text
        t.delete('1.0', tk.END)
        if root.get('title'):
            t.insert(tk.END, root['title'] + '\n', 'title')
        t.insert(tk.END, root.get('content', '') + '\n')
        self._insert_reactions_full(t, root)
        t.yview_moveto(0)

        self._render_comments()
        self._set_target(root)

    def _render_comments(self):
        t = self.comments_text
        t.delete('1.0', tk.END)
        for tag in t.tag_names():
            if tag.startswith('c_'):
                t.tag_delete(tag)
        root = self.current_root
        n = self.thread_size.get(root['id'], 1) - 1
        self.comments_label.config(text=f"Comments ({n})" if n else 'Comments')
        if not n:
            t.insert(tk.END, 'No comments yet — be the first! ♡\n', 'dim')
            return
        c = self.c
        rendered = {root['id']}

        def render(post, depth):
            if post['id'] in rendered:  # guard against a looped reply chain
                return
            rendered.add(post['id'])
            ctag = f"c_{post['id']}"
            dtag = f"depth{min(depth, 8)}"
            indent = (min(depth, 8) - 1) * 26
            t.tag_configure(dtag, lmargin1=indent, lmargin2=indent)
            tags = (ctag, dtag)
            me = post['author_id'] == self.user_id
            t.insert(tk.END, '▸ ' if depth > 1 else '', ('dim',) + tags)
            t.insert(tk.END, self._author_name(post['author_id']),
                     ('author_me' if me else 'author',) + tags)
            meta = f"   ·   {fmt_time(post.get('created_at'))}"
            if depth > 1:
                meta += f"   ↳ to {self._author_name(self.by_id[post['reply_to']]['author_id'])}"
            t.insert(tk.END, meta + '\n', ('meta',) + tags)
            t.insert(tk.END, post.get('content', '').rstrip() + '\n', tags)
            self._insert_reactions_inline(t, post, tags)
            t.insert(tk.END, '\n', tags)
            t.tag_bind(ctag, '<Button-1>', lambda e, p=post: self._set_target(p))
            t.tag_bind(ctag, '<Enter>', lambda e: t.config(cursor='hand2'))
            t.tag_bind(ctag, '<Leave>', lambda e: t.config(cursor='arrow'))
            for child in self.children.get(post['id'], []):
                render(child, depth + 1)

        for child in self.children.get(root['id'], []):
            render(child, 1)
        t.tag_configure('selected_comment', background=c['CHIP_BG'])
        t.yview_moveto(0)

    def _set_target(self, post: dict | None, scroll: bool = False):
        if not post or not self.current_root:
            return
        self.target = post
        t = self.comments_text
        c = self.c
        for tag in t.tag_names():
            if tag.startswith('c_'):
                t.tag_configure(tag, background='')
        is_root = post['id'] == self.current_root['id']
        who = 'your' if post['author_id'] == self.user_id else \
            f"{self._author_name(post['author_id'])}'s"
        if is_root:
            self.target_label.config(text=f"Reply to {who} post:")
            self.untarget_btn.pack_forget()
        else:
            ctag = f"c_{post['id']}"
            t.tag_configure(ctag, background=c['CHIP_BG'])
            self.target_label.config(text=f"Reply to {who} comment:")
            self.untarget_btn.pack(side=tk.LEFT, padx=(10, 0))
            if scroll:
                try:
                    t.see(f'{ctag}.first')
                    t.yview(f'{ctag}.first')
                except tk.TclError:
                    pass

    # ── Reactions display ────────────────────────────────────────────

    def _reactors(self, value) -> list[str]:
        return [self._author_name(a) for a in reaction_users(value)]

    def _insert_reactions_full(self, t, post: dict):
        reactions = {e: v for e, v in (post.get('reactions') or {}).items() if v}
        if not reactions:
            return
        t.insert(tk.END, f'Reactions  ·  {reaction_total(post)}\n', 'react_header')
        for emoji, value in reactions.items():
            who = self._reactors(value)
            count = len(value) if isinstance(value, list) else value
            t.insert(tk.END, f' {emoji} ', 'react_emoji')
            t.insert(tk.END, f' {emoji_name(emoji)} ×{count} ', 'react_name')
            if who:
                t.insert(tk.END, f'   from {", ".join(who)}', 'react_who')
            t.insert(tk.END, '\n\n')

    def _insert_reactions_inline(self, t, post: dict, tags: tuple):
        reactions = {e: v for e, v in (post.get('reactions') or {}).items() if v}
        if not reactions:
            return
        for i, (emoji, value) in enumerate(reactions.items()):
            if i:
                t.insert(tk.END, '   ·   ', ('react_inline',) + tags)
            who = self._reactors(value)
            t.insert(tk.END, emoji, ('react_small',) + tags)
            label = f' {emoji_name(emoji)}' + (f' ({", ".join(who)})' if who else '')
            t.insert(tk.END, label, ('react_inline',) + tags)
        t.insert(tk.END, '\n', tags)

    def _reactions_as_text(self, post: dict) -> str:
        parts = []
        for emoji, value in (post.get('reactions') or {}).items():
            if not value:
                continue
            who = self._reactors(value)
            parts.append(f"{emoji} {emoji_name(emoji)}" + (f" ({', '.join(who)})" if who else ''))
        return ('Reactions: ' + ' · '.join(parts)) if parts else ''

    # ── Posting & reacting (as the user) ────────────────────────────────

    def _can_write(self) -> bool:
        if not self.user_id:
            self.status_var.set("Couldn't find your LoR identity — can't post.")
            return False
        return True

    def _fill_react_menu(self):
        m = self.react_menu
        m.delete(0, tk.END)
        if not self.target:
            m.add_command(label='Open a thread first', state=tk.DISABLED)
            return
        existing = self.target.get('reactions') or {}
        choices = REACT_CHOICES + [e for e in existing if e not in REACT_CHOICES]
        for emoji in choices:
            mine = self.user_id in reaction_users(existing.get(emoji))
            label = f"{'✓' if mine else '   '}  {emoji}   {emoji_name(emoji)}"
            m.add_command(label=label, command=lambda e=emoji: self._react(e))

    def _react(self, emoji):
        if not self.target or not self._can_write():
            return
        pid = self.target['id']
        try:
            on = toggle_reaction(pid, self.user_id, emoji)
        except Exception as e:
            messagebox.showerror('LoR', f"Couldn't save your reaction:\n{e}")
            return
        c_view = self.comments_text.yview()[0]
        self.refresh(open_id=pid, target_id=pid)
        self.comments_text.yview_moveto(c_view)
        self.status_var.set(f"{'Reacted' if on else 'Removed'} {emoji} {emoji_name(emoji)}.")

    def _post_reply(self):
        if not self.target:
            self.status_var.set('Open a thread to reply to it first.')
            return
        if not self._can_write():
            return
        content = self.reply_text.get('1.0', 'end').strip()
        if not content:
            return
        parent = self.target
        try:
            post = make_post(self.user_id, content, reply_to=parent['id'],
                             category=self.current_root.get('category') or 'general')
        except Exception as e:
            messagebox.showerror('LoR', f"Couldn't post your reply:\n{e}")
            return
        self.reply_text.delete('1.0', tk.END)
        self.refresh(open_id=post['id'])
        try:
            self.comments_text.see(f"c_{post['id']}.first")
        except tk.TclError:
            pass
        self.status_var.set(f"Reply posted to {self._author_name(parent['author_id'])}! 💌")

    def _new_thread_dialog(self):
        if not self._can_write():
            return
        c = self.c
        win = tk.Toplevel(self.root)
        win.title('New Thread')
        win.configure(bg=c['BG'])
        win.geometry('620x520')
        win.transient(self.root)
        win.grab_set()

        tk.Label(win, text='New thread on LoR', bg=c['BG'], fg=c['ACCENT'],
                 font=self.FONT_TITLE).pack(anchor='w', padx=16, pady=(16, 10))

        fields = tk.Frame(win, bg=c['BG'])
        fields.pack(fill=tk.X, padx=16)
        fields.columnconfigure(1, weight=1)
        tk.Label(fields, text='Title:', bg=c['BG'], fg=c['TEXT'],
                 font=self.FONT).grid(row=0, column=0, sticky='w', pady=4)
        title_entry = tk.Entry(fields, font=self.FONT, bg=c['ENTRY_BG'], fg=c['TEXT'],
                               insertbackground=c['ACCENT'], relief=tk.FLAT,
                               highlightthickness=1, highlightcolor=c['ACCENT'],
                               highlightbackground=c['BG_LIGHT'])
        title_entry.grid(row=0, column=1, sticky='ew', padx=(10, 0), pady=4, ipady=3)
        tk.Label(fields, text='Category:', bg=c['BG'], fg=c['TEXT'],
                 font=self.FONT).grid(row=1, column=0, sticky='w', pady=4)
        cats = CATEGORIES + sorted({p.get('category') for p in self.posts
                                    if p.get('category') and p.get('category') not in CATEGORIES})
        cat_var = tk.StringVar(value='general')
        self._option_menu(fields, cat_var, cats, None).grid(row=1, column=1, sticky='w',
                                                            padx=(10, 0), pady=4)

        tk.Label(win, text='Post:', bg=c['BG'], fg=c['TEXT'],
                 font=self.FONT).pack(anchor='w', padx=16, pady=(10, 3))
        body = scrolledtext.ScrolledText(win, wrap=tk.WORD, font=self.FONT, bg=c['ENTRY_BG'],
                                         fg=c['TEXT'], insertbackground=c['ACCENT'],
                                         relief=tk.FLAT, padx=10, pady=10, height=12)

        def submit():
            content = body.get('1.0', 'end').strip()
            if not content:
                messagebox.showwarning('Empty', 'Write something first!', parent=win)
                return
            try:
                post = make_post(self.user_id, content, title=title_entry.get().strip(),
                                 category=cat_var.get())
            except Exception as e:
                messagebox.showerror('LoR', f"Couldn't post your thread:\n{e}", parent=win)
                return
            win.destroy()
            self.search_var.set('')
            self.category_var.set('All')
            self.notebook.select(0)
            self.refresh(open_id=post['id'])
            self.status_var.set('Thread posted! ✨')

        btns = tk.Frame(win, bg=c['BG'])
        btns.pack(side=tk.BOTTOM, fill=tk.X, padx=16, pady=(0, 16))
        self._button(btns, 'Post Thread', submit, accent=True).pack(side=tk.RIGHT)
        self._button(btns, 'Cancel', win.destroy).pack(side=tk.RIGHT, padx=(0, 10))
        body.pack(fill=tk.BOTH, expand=True, padx=16, pady=(0, 10))
        body.bind('<Control-Return>', lambda e: (submit(), 'break')[1])
        title_entry.focus_set()

    # ── Copy actions ─────────────────────────────────────────────────

    def _post_as_text(self, post: dict, depth: int = 0) -> str:
        pad = '   ' * depth
        author = self._author_name(post['author_id'])
        lines = [f"{pad}── {author}   ·   {fmt_time(post.get('created_at'))}   ·   [{post['id']}]"]
        if post.get('title'):
            lines.append(f"{pad}{post['title']}")
        for line in post.get('content', '').split('\n'):
            lines.append(f"{pad}{line}")
        reacts = self._reactions_as_text(post)
        if reacts:
            lines.append(f"{pad}{reacts}")
        return '\n'.join(lines)

    def _copy_target(self):
        if not self.target:
            self.status_var.set('Open a thread first.')
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(self._post_as_text(self.target))
        kind = 'post' if self.target is self.current_root else 'comment'
        self.status_var.set(f"Copied {self._author_name(self.target['author_id'])}'s {kind}.")

    def _copy_thread(self):
        if not self.current_root:
            self.status_var.set('Open a thread first.')
            return
        root = self.current_root
        out = [f"Thread rooted at [{root['id']}] — "
               f"{plural(self.thread_size.get(root['id'], 1), 'post')}\n"]

        def render(post, depth):
            out.append(self._post_as_text(post, depth))
            out.append('')
            for child in self.children.get(post['id'], []):
                render(child, depth + 1)

        render(root, 0)
        self.root.clipboard_clear()
        self.root.clipboard_append('\n'.join(out))
        self.status_var.set(f"Copied the whole thread "
                            f"({plural(self.thread_size.get(root['id'], 1), 'post')}).")


def main():
    root = tk.Tk()
    LoRGui(root)
    root.mainloop()


if __name__ == '__main__':
    main()
