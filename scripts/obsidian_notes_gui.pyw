"""
obsidian_notes_gui — Tkinter window for reading the companions' notes on
the user's Obsidian clips and salon posts. Click a clip on the left, read the
notes on the right, reply as yourself at the bottom (signed with
user_nickname / user_name from the config). Third sibling of the
Lighthouse GUI and lor_gui — same twilight palette, same quiet manners.

Launch from the Pulse folder:
    py -3.14 scripts/obsidian_notes_gui.pyw

Pure local JSON (data/obsidian_notes.json); no API calls. Visibility
matches the obsidian skill: shared notes show, private notes stay with
their authors. Author colors come from each persona's `accent` setting.
"""

import json
import tkinter as tk

import yaml
from datetime import datetime
from pathlib import Path
from tkinter import scrolledtext

PULSE_ROOT = Path(__file__).resolve().parent.parent
NOTES_FILE = PULSE_ROOT / "data" / "obsidian_notes.json"

# Twilight palette (same family as the Lighthouse GUI / lor_gui).
BG = '#1a1a2e'
BG_LIGHT = '#16213e'
ACCENT = '#e2b4d3'
TEXT = '#f0e6ef'
TEXT_DIM = '#a89fb0'
ENTRY_BG = '#252547'
BUTTON_BG = '#3d2c5e'
BUTTON_HOVER = '#5a3d7a'
HIGHLIGHT = '#f7c8e0'
CARD_BG = '#2d2b52'

FONT = ('Atkinson Hyperlegible', 10)
FONT_BOLD = ('Atkinson Hyperlegible', 10, 'bold')
FONT_TITLE = ('Atkinson Hyperlegible', 13, 'bold')
FONT_NOTE = ('Atkinson Hyperlegible', 11)



def _user_name() -> str:
    """The user's display name: user_nickname, then user_name, from config.yaml
    and the root persona file. Falls back to "User"."""
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


USER_NAME = _user_name()


def _author_colors() -> dict:
    """One color per companion so their voices are tellable at a glance:
    each persona's `accent` (config.yaml), keyed by its LoR author_name.
    The user gets the highlight pink; anyone unlisted gets the accent pink."""
    colors = {}
    for cfg in sorted((PULSE_ROOT / 'personas').glob('*/config.yaml')):
        if cfg.parent.name.startswith('_'):
            continue
        try:
            data = yaml.safe_load(cfg.read_text(encoding='utf-8')) or {}
        except Exception:
            continue
        accent = data.get('accent')
        if not (isinstance(accent, str) and accent.startswith('#')):
            continue
        author = ((data.get('channels') or {}).get('lor') or {}).get('author_name')
        for key in {cfg.parent.name.lower(), str(author or '').lower()}:
            if key:
                colors[key] = accent
    colors[USER_NAME.lower()] = HIGHLIGHT
    return colors


AUTHOR_COLORS = _author_colors()


def load_notes() -> dict:
    if not NOTES_FILE.exists():
        return {}
    try:
        return json.loads(NOTES_FILE.read_text(encoding='utf-8'))
    except Exception:
        return {}


def save_notes(notes: dict):
    NOTES_FILE.parent.mkdir(parents=True, exist_ok=True)
    NOTES_FILE.write_text(json.dumps(notes, indent=2, ensure_ascii=False),
                          encoding='utf-8')


def is_visible(note: dict) -> bool:
    """Shared notes are public; private notes are the author's own margins."""
    if note.get('shared', True):
        return True
    return (note.get('author') or '').lower() == USER_NAME.lower()


def clip_display(name: str) -> str:
    return name.replace('\\', '/').removesuffix('.md')


def when_display(iso: str) -> str:
    return (iso or '')[:16].replace('T', ' ')


class ObsidianNotesGui:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title('Obsidian Notes — Pulse')
        self.root.configure(bg=BG)
        self.root.geometry('1200x800')

        self.notes: dict = {}
        self.clips: list[tuple[str, list[dict]]] = []   # filtered, sorted
        self.current_clip: str | None = None

        self._build_ui()
        self.refresh()

    # ── UI assembly ──────────────────────────────────────────────────

    def _build_ui(self):
        toolbar = tk.Frame(self.root, bg=BG)
        toolbar.pack(side=tk.TOP, fill=tk.X, padx=12, pady=(10, 6))

        tk.Label(toolbar, text='Obsidian Notes', bg=BG, fg=ACCENT,
                 font=FONT_TITLE).pack(side=tk.LEFT)

        tk.Label(toolbar, text='Filter:', bg=BG, fg=TEXT_DIM,
                 font=FONT).pack(side=tk.LEFT, padx=(24, 4))
        self.search_var = tk.StringVar()
        self.search_var.trace_add('write', lambda *_: self._apply_filter())
        search = tk.Entry(toolbar, textvariable=self.search_var, bg=ENTRY_BG,
                          fg=TEXT, insertbackground=TEXT, font=FONT,
                          relief=tk.FLAT, width=30, highlightthickness=1,
                          highlightcolor=ACCENT, highlightbackground=BG_LIGHT)
        search.pack(side=tk.LEFT, ipady=3)

        self._make_button(toolbar, 'Refresh', self.refresh).pack(
            side=tk.RIGHT, padx=(6, 0))

        # Split: clip list left, notes right
        body = tk.Frame(self.root, bg=BG)
        body.pack(fill=tk.BOTH, expand=True, padx=12, pady=(0, 8))

        left = tk.Frame(body, bg=BG, width=430)
        left.pack(side=tk.LEFT, fill=tk.Y)
        left.pack_propagate(False)

        self.clip_list = tk.Listbox(
            left, bg=BG_LIGHT, fg=TEXT, font=FONT, relief=tk.FLAT,
            selectbackground=BUTTON_HOVER, selectforeground=HIGHLIGHT,
            highlightthickness=0, activestyle='none')
        self.clip_list.pack(fill=tk.BOTH, expand=True)
        self.clip_list.bind('<<ListboxSelect>>', self._on_select)

        right = tk.Frame(body, bg=BG)
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(12, 0))

        self.header = tk.Label(right, text='Pick a clip on the left. 🌙',
                               bg=BG, fg=TEXT_DIM, font=FONT_BOLD,
                               anchor='w', justify=tk.LEFT)
        self.header.pack(fill=tk.X, pady=(2, 6))

        self.view = scrolledtext.ScrolledText(
            right, bg=BG_LIGHT, fg=TEXT, font=FONT_NOTE, relief=tk.FLAT,
            wrap=tk.WORD, padx=14, pady=12, state=tk.DISABLED,
            highlightthickness=0)
        self.view.pack(fill=tk.BOTH, expand=True)

        self.view.tag_configure('when', foreground=TEXT_DIM,
                                font=('Atkinson Hyperlegible', 9))
        self.view.tag_configure('note', foreground=TEXT, spacing3=4)
        self.view.tag_configure('sep', foreground=BG_LIGHT,
                                font=('Atkinson Hyperlegible', 4))
        self.view.tag_configure('private', foreground=TEXT_DIM,
                                font=('Atkinson Hyperlegible', 9, 'italic'))
        for author, color in AUTHOR_COLORS.items():
            self.view.tag_configure(f'author_{author}', foreground=color,
                                    font=FONT_BOLD)
        self.view.tag_configure('author_other', foreground=ACCENT,
                                font=FONT_BOLD)

        # Reply bar
        reply_bar = tk.Frame(right, bg=BG)
        reply_bar.pack(fill=tk.X, pady=(8, 0))

        self.reply_entry = tk.Text(
            reply_bar, bg=ENTRY_BG, fg=TEXT, insertbackground=TEXT,
            font=FONT_NOTE, relief=tk.FLAT, height=3, wrap=tk.WORD,
            padx=8, pady=6, highlightthickness=1,
            highlightcolor=ACCENT, highlightbackground=BG_LIGHT)
        self.reply_entry.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.reply_btn = self._make_button(reply_bar, f'Reply as {USER_NAME}',
                                           self._send_reply)
        self.reply_btn.pack(side=tk.LEFT, padx=(8, 0), fill=tk.Y)

        self.status = tk.Label(self.root, text='', bg=BG, fg=TEXT_DIM,
                               font=('Atkinson Hyperlegible', 9), anchor='w')
        self.status.pack(fill=tk.X, padx=14, pady=(0, 6))

    def _make_button(self, parent, text, command):
        btn = tk.Button(parent, text=text, command=command, bg=BUTTON_BG,
                        fg=TEXT, font=FONT, relief=tk.FLAT, padx=14, pady=4,
                        activebackground=BUTTON_HOVER, activeforeground=TEXT,
                        cursor='hand2')
        btn.bind('<Enter>', lambda e: btn.configure(bg=BUTTON_HOVER))
        btn.bind('<Leave>', lambda e: btn.configure(bg=BUTTON_BG))
        return btn

    # ── Data ─────────────────────────────────────────────────────────

    def refresh(self):
        self.notes = load_notes()
        self._apply_filter()
        total = sum(len([n for n in ns if is_visible(n)])
                    for ns in self.notes.values())
        self.status.configure(
            text=f'{len(self.clips)} clip(s) with notes · {total} visible '
                 f'note(s) · {NOTES_FILE.name}')
        if self.current_clip and self.current_clip in self.notes:
            self._show_clip(self.current_clip)

    def _apply_filter(self):
        query = self.search_var.get().strip().lower()
        visible = []
        for clip, ns in self.notes.items():
            vs = [n for n in ns if is_visible(n)]
            if not vs:
                continue
            if query and query not in clip.lower() and not any(
                    query in (n.get('note') or '').lower()
                    or query in (n.get('author') or '').lower()
                    for n in vs):
                continue
            visible.append((clip, vs))
        visible.sort(key=lambda x: max((n.get('added_at', '') for n in x[1]),
                                       default=''), reverse=True)
        self.clips = visible

        self.clip_list.delete(0, tk.END)
        for clip, ns in visible:
            authors = sorted({(n.get('author') or '?') for n in ns})
            label = f' {clip_display(clip)}   ·  {len(ns)} 💬  {", ".join(authors)}'
            self.clip_list.insert(tk.END, label)

    # ── Interaction ──────────────────────────────────────────────────

    def _on_select(self, _event):
        sel = self.clip_list.curselection()
        if not sel or sel[0] >= len(self.clips):
            return
        self._show_clip(self.clips[sel[0]][0])

    def _show_clip(self, clip: str):
        self.current_clip = clip
        ns = sorted((n for n in self.notes.get(clip, []) if is_visible(n)),
                    key=lambda n: n.get('added_at', ''))
        self.header.configure(text=clip_display(clip), fg=HIGHLIGHT)

        self.view.configure(state=tk.NORMAL)
        self.view.delete('1.0', tk.END)
        for i, n in enumerate(ns):
            author = n.get('author') or '?'
            tag = f'author_{author.lower()}'
            if f'author_{author.lower()}' not in AUTHOR_COLORS and \
                    author.lower() not in AUTHOR_COLORS:
                tag = 'author_other'
            if i:
                self.view.insert(tk.END, '\n\n', 'sep')
            self.view.insert(tk.END, f'{author}  ', tag)
            self.view.insert(tk.END, when_display(n.get('added_at', '')), 'when')
            if not n.get('shared', True):
                self.view.insert(tk.END, '  (private)', 'private')
            self.view.insert(tk.END, f"\n{(n.get('note') or '').strip()}", 'note')
        if not ns:
            self.view.insert(tk.END, 'No visible notes here yet.', 'when')
        self.view.configure(state=tk.DISABLED)

    def _send_reply(self):
        text = self.reply_entry.get('1.0', tk.END).strip()
        if not text:
            self.status.configure(text='Nothing to send — the reply box is empty.')
            return
        if not self.current_clip:
            self.status.configure(text='Pick a clip first, then reply.')
            return
        # Re-load before writing so a companion's note added meanwhile
        # doesn't get clobbered.
        fresh = load_notes()
        fresh.setdefault(self.current_clip, []).append({
            'author': USER_NAME,
            'note': text,
            'shared': True,
            'added_at': datetime.now().isoformat(),
        })
        save_notes(fresh)
        self.reply_entry.delete('1.0', tk.END)
        self.refresh()
        self.status.configure(
            text=f'✓ Note added to "{clip_display(self.current_clip)}" as {USER_NAME}. '
                 f'The companions will see it next time they read the margins.')


if __name__ == '__main__':
    root = tk.Tk()
    ObsidianNotesGui(root)
    root.mainloop()
