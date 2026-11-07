"""
workbench_gui — the user's window onto the Workbench and House Projects.

🛠 Code changes — companions' code edits arrive as proposals (core/workbench.py).
   See what changed and why, safety checks, reviews and tokens, then
   Approve & apply (backups + Undo), Reject (author gets a kind letter),
   Run tests (in a throwaway copy — only you can), or Ask for review (pings).

📚 House Projects — shared projects as a relay (core/house_projects.py).
   Pick who works in what order and give each a role; each person gets their
   role + the last handoff note. Everyone can read and comment. Every file save
   keeps the old version.

Looks like Local Reddit on purpose (same palettes + text size, shared in
data/lor_gui_settings.json), plus its own Shape (corner roundness) and Cards
(filled / outlined) settings for the project board.

House Projects view: a LIVE strip (what the person on the
active step is doing right now, from core/house_live.py + their own "status"
note), the relay as a timeline with a progress bar, and a Reader pane that
shows the writing like a book page (light Markdown, auto-refreshes on save,
its own Paper colors).

Who's in the house comes from the persona folders (core/workbench.companion_names),
plus any relay guests in data/relay_guests.json; your own name from config
(core/workbench.user_name). The "Works on" shortcuts are the code roots
(PULSE_CODE_ROOTS, default this Pulse folder).

Launch:  py scripts/workbench_gui.pyw
"""

import importlib.machinery
import importlib.util
import os
import re
import sys
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import ttk, scrolledtext, messagebox, simpledialog, filedialog

PULSE_ROOT = Path(__file__).resolve().parent.parent
if str(PULSE_ROOT) not in sys.path:
    sys.path.insert(0, str(PULSE_ROOT))
from core import workbench  # noqa: E402
from core import house_projects as hp  # noqa: E402
from core import house_live  # noqa: E402
from core import house_sort  # noqa: E402
from core import house_recipes  # noqa: E402
from core import house_costs  # noqa: E402

# Borrow Local Reddit's palettes + settings so both windows match.
_loader = importlib.machinery.SourceFileLoader("lor_gui", str(PULSE_ROOT / "scripts" / "lor_gui.pyw"))
_spec = importlib.util.spec_from_loader("lor_gui", _loader)
_lor = importlib.util.module_from_spec(_spec)
_loader.exec_module(_lor)
PALETTES, load_settings, save_settings = _lor.PALETTES, _lor.load_settings, _lor.save_settings
FONT_FAMILY = _lor.FONT_FAMILY

FILTERS = ["Waiting for you", "Applied", "Everything"]
STATUS_ICON = {"submitted": "●", "applied": "✓", "rejected": "✕", "undone": "↶", "draft": "…"}
STATUS_WORD = {"submitted": "waiting for you", "applied": "applied", "rejected": "rejected",
               "undone": "undone", "draft": "still being drafted"}
CORNERS = {"Sharp": 0, "Soft": 8, "Round": 16, "Bubbly": 26}
# Reader page colors: (paper, text, headings, accents). None = follow the window palette.
PAPERS = {
    "Match theme": None,
    "Cream": ("#fbf5ea", "#3d3229", "#9c4a2f", "#b0645a"),
    "Rose": ("#fdeef2", "#4a2f38", "#b3406a", "#c46b8c"),
    "Lavender": ("#f3eefc", "#352c4a", "#6a48a8", "#8b6cc4"),
    "Mint": ("#eef8f2", "#26392f", "#2f7a55", "#4f9a75"),
    "Sky": ("#eef5fc", "#273444", "#2f5f9a", "#5a84b8"),
    "Night": ("#1f1d2b", "#e8e2f0", "#f5b8d0", "#c9b6ff"),
    "Ink": ("#141414", "#e6e6e6", "#ffffff", "#bdbdbd"),
}
CARD_STYLES = ["Filled", "Outlined"]
KINDS = ["Writing", "Code", "Sorting", "Planning", "Other"]
# "Works on" shortcuts (any other folder: Choose a folder…). "" = the project's own drafts folder.
# The shortcuts are the code roots (PULSE_CODE_ROOTS), named by folder.
WORKS_ON = {"Its own folder": ""}
for _root in workbench.code_roots():
    WORKS_ON.setdefault("Pulse" if _root.resolve() == PULSE_ROOT.resolve() else _root.name, str(_root))
CHOOSE = "Choose a folder…"
BINARY_EXT = {".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".zip", ".docx", ".exe", ".mp3", ".mp4", ".wav"}
STEP_WORD = {"pending": "up next", "waiting_for_user": "waiting for you to start it", "active": "their turn",
             "done": "done", "declined": "declined", "skipped": "skipped"}
POLL_MS = 30_000
LIVE_MS = 4_000            # LIVE strip + reader refresh (House Projects tab only)
READ_FONT = 'Georgia'      # the Reader's book-ish font


def nice_time(iso):
    if not iso:
        return ''
    try:
        d = datetime.fromisoformat(iso)
    except ValueError:
        return iso
    hour = d.strftime('%I').lstrip('0')
    return f"{d:%b} {d.day}, {hour}:{d:%M} {d:%p}"


def is_light(hex_color):
    h = hex_color.lstrip('#')
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return (0.299 * r + 0.587 * g + 0.114 * b) > 150


def companions(code_only=False):
    """Everyone in the house, from the persona folders (see core/workbench.companion_names)."""
    return workbench.companion_names(code_only=code_only)


class WorkbenchGui:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title('Workbench — Pulse')
        self.root.geometry('1340x880')
        self.settings = load_settings()
        self.settings.setdefault('wb_corners', 'Round')
        self.settings.setdefault('wb_cards', 'Filled')
        self.settings.setdefault('wb_paper', 'Match theme')
        self.filter_var = tk.StringVar(value=FILTERS[0])
        self.status_var = tk.StringVar(value='Ready.')
        self.who_var = tk.StringVar(value='')
        self.check_first_var = tk.BooleanVar(value=False)
        self.props: list[dict] = []
        self.current: dict | None = None
        self.projects: list[dict] = []
        self.cur_proj: dict | None = None
        self.reviewers = companions(code_only=True)
        # relay guests (Claude Code / Cowork friends) can take House Project turns too
        guests = [g['name'] for g in hp.relay_guests()]
        self.housemates = sorted(set(companions() + guests), key=str.lower)
        self.everyone = self.housemates + [hp.USER]
        self.tab_index = 1                      # open on House Projects; Code changes is one click away
        self._testing = False
        self._redraw_job = None
        self._open_plans = set()
        self._build()
        self.refresh()
        self.refresh_projects()
        self.root.after(POLL_MS, self._poll)
        self.root.after(LIVE_MS, self._live_tick)

    # ── look ─────────────────────────────────────────────────────────

    @property
    def c(self):
        return PALETTES[self.settings['palette']]

    def _fonts(self):
        s = self.settings['font_size']
        self.FONT = (FONT_FAMILY, s)
        self.FONT_BOLD = (FONT_FAMILY, s, 'bold')
        self.FONT_TITLE = (FONT_FAMILY, s + 4, 'bold')
        self.FONT_BIG = (FONT_FAMILY, s + 8, 'bold')
        self.FONT_SMALL = (FONT_FAMILY, max(8, s - 1))
        self.FONT_MONO = ('Consolas', s)

    def _button(self, parent, text, cmd, kind='plain'):
        c = self.c
        b = tk.Button(parent, text=text, command=cmd, bg=c['BUTTON_BG'], fg=c['TEXT'],
                      font=self.FONT, relief=tk.FLAT, activebackground=c['BUTTON_HOVER'],
                      activeforeground=c['TEXT'], padx=12, pady=5, borderwidth=0, cursor='hand2')
        if kind == 'accent':
            b.config(bg=c['ACCENT'], fg=c['BG'], activebackground=c['HIGHLIGHT'],
                     activeforeground=c['BG'], font=self.FONT_BOLD)
        return b

    def _option_menu(self, parent, var, options, cmd=None):
        c = self.c
        om = tk.OptionMenu(parent, var, *options, command=cmd)
        om.config(bg=c['BUTTON_BG'], fg=c['TEXT'], font=self.FONT, relief=tk.FLAT,
                  activebackground=c['BUTTON_HOVER'], activeforeground=c['TEXT'],
                  highlightthickness=0, borderwidth=0, padx=8, pady=3, cursor='hand2')
        om['menu'].config(bg=c['ENTRY_BG'], fg=c['TEXT'], font=self.FONT,
                          activebackground=c['BUTTON_HOVER'], activeforeground=c['TEXT'])
        if len(options) > 10:                   # long lists (everyone in the house) open as columns
            per_col = 8                         # so nobody falls off the bottom of the screen
            for i in range(per_col, len(options), per_col):
                om['menu'].entryconfigure(i, columnbreak=1)
        return om

    def _build(self):
        self._fonts()
        c = self.c
        s = self.settings['font_size']
        self.root.configure(bg=c['BG'])

        style = ttk.Style()
        style.theme_use('default')
        style.configure('Treeview', background=c['ENTRY_BG'], foreground=c['TEXT'],
                        fieldbackground=c['ENTRY_BG'], font=self.FONT,
                        rowheight=int(s * 2.8), borderwidth=0)
        style.configure('Treeview.Heading', background=c['BG_LIGHT'], foreground=c['ACCENT'],
                        font=self.FONT_BOLD, relief='flat', borderwidth=0)
        style.map('Treeview', background=[('selected', c['BUTTON_HOVER'])],
                  foreground=[('selected', c['HIGHLIGHT'])])
        style.configure('Vertical.TScrollbar', background=c['BUTTON_BG'],
                        troughcolor=c['BG_LIGHT'], arrowcolor=c['TEXT_DIM'], borderwidth=0)
        style.configure('TNotebook', background=c['BG'], borderwidth=0)
        style.configure('TNotebook.Tab', background=c['BUTTON_BG'], foreground=c['TEXT'],
                        font=self.FONT_BOLD, padding=(16, 6), borderwidth=0)
        style.map('TNotebook.Tab', background=[('selected', c['ACCENT'])],
                  foreground=[('selected', c['BG'])])

        bar = tk.Frame(self.root, bg=c['BG'])
        bar.pack(side=tk.TOP, fill=tk.X, padx=12, pady=(10, 6))
        tk.Label(bar, text='🛠 Workbench', bg=c['BG'], fg=c['ACCENT'],
                 font=self.FONT_TITLE).pack(side=tk.LEFT)
        self._button(bar, '↻', self._refresh_all).pack(side=tk.RIGHT)
        for label, var_key, options, cmd in (
                ('Cards:', 'wb_cards', CARD_STYLES, self._change_setting),
                ('Shape:', 'wb_corners', list(CORNERS), self._change_setting),
                ('🎨', 'palette', list(PALETTES), self._change_setting)):
            var = tk.StringVar(value=self.settings[var_key])
            self._option_menu(bar, var, options,
                              lambda v, k=var_key: cmd(k, v)).pack(side=tk.RIGHT, padx=(4, 12))
            tk.Label(bar, text=label, bg=c['BG'], fg=c['TEXT_DIM'],
                     font=self.FONT).pack(side=tk.RIGHT)

        tk.Label(self.root, textvariable=self.status_var, bg=c['BG_LIGHT'], fg=c['TEXT_DIM'],
                 font=self.FONT_SMALL, anchor='w', padx=12, pady=4).pack(side=tk.BOTTOM, fill=tk.X)

        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=12, pady=(0, 8))
        code_tab = tk.Frame(self.notebook, bg=c['BG'])
        proj_tab = tk.Frame(self.notebook, bg=c['BG'])
        self.notebook.add(code_tab, text='🛠 Code changes')
        self.notebook.add(proj_tab, text='📚 House Projects')
        self._build_code_tab(code_tab)
        self._build_projects_tab(proj_tab)
        self.notebook.select(self.tab_index)

    def _change_setting(self, key, value):
        fresh = load_settings()                 # keep Local Reddit's latest text size etc.
        fresh.update({k: self.settings[k] for k in ('wb_corners', 'wb_cards', 'wb_paper') if k in self.settings})
        fresh[key] = value
        self.settings = fresh
        save_settings(self.settings)
        self.tab_index = self.notebook.index('current')
        keep_prop = self.current['id'] if self.current else None
        keep_proj = self.cur_proj['slug'] if self.cur_proj else None
        for w in self.root.winfo_children():
            w.destroy()
        self._build()
        self.refresh(select=keep_prop)
        self.refresh_projects(select=keep_proj)

    def _refresh_all(self):
        self.refresh()
        self.refresh_projects()

    # ════════════════════════════════════════════════════════════════
    #  🛠 CODE CHANGES
    # ════════════════════════════════════════════════════════════════

    def _build_code_tab(self, tab):
        c = self.c
        k = self.settings['font_size'] / 10
        head = tk.Frame(tab, bg=c['BG'])
        head.pack(fill=tk.X, pady=(8, 6))
        tk.Label(head, text='Show:', bg=c['BG'], fg=c['TEXT_DIM'], font=self.FONT).pack(side=tk.LEFT)
        self._option_menu(head, self.filter_var, FILTERS, lambda _v: self._fill()).pack(side=tk.LEFT, padx=6)

        main = tk.PanedWindow(tab, orient=tk.HORIZONTAL, bg=c['BG'], sashwidth=6, sashrelief=tk.FLAT)
        main.pack(fill=tk.BOTH, expand=True)

        left = tk.Frame(main, bg=c['BG'])
        main.add(left, minsize=360, width=int(520 * k))
        cols = ('state', 'title', 'author', 'reviews', 'when')
        self.tree = ttk.Treeview(left, columns=cols, show='headings', selectmode='browse')
        for col, text, w, stretch in [('state', '', 30, False), ('title', 'Change', 220, True),
                                      ('author', 'By', 80, False), ('reviews', 'Reviews', 70, False),
                                      ('when', 'When', 120, False)]:
            self.tree.heading(col, text=text)
            self.tree.column(col, width=int(w * k), minwidth=24, stretch=stretch,
                             anchor='center' if col in ('state', 'reviews') else 'w')
        self.tree.tag_configure('odd', background=c['STRIPE'])
        self.tree.tag_configure('even', background=c['ENTRY_BG'])
        self.tree.tag_configure('waiting', foreground=c['HIGHLIGHT'])
        self.tree.tag_configure('done', foreground=c['TEXT_DIM'])
        self.tree.bind('<<TreeviewSelect>>', self._on_select)
        sb = ttk.Scrollbar(left, orient='vertical', command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree.pack(fill=tk.BOTH, expand=True)

        right = tk.Frame(main, bg=c['BG'])
        main.add(right, minsize=520)
        self.title_label = tk.Label(right, text='Nothing selected', bg=c['BG'], fg=c['HIGHLIGHT'],
                                    font=self.FONT_TITLE, anchor='w', justify='left')
        self.title_label.pack(fill=tk.X)
        self.meta_label = tk.Label(right, text='', bg=c['BG'], fg=c['TEXT_DIM'],
                                   font=self.FONT_SMALL, anchor='w')
        self.meta_label.pack(fill=tk.X, pady=(0, 6))

        actions = tk.Frame(right, bg=c['BG'])
        actions.pack(fill=tk.X, pady=(0, 6))
        self.approve_btn = self._button(actions, '✓  Approve & apply', self._approve, 'accent')
        self.approve_btn.pack(side=tk.LEFT)
        self.reject_btn = self._button(actions, '✕  Reject', self._reject)
        self.reject_btn.pack(side=tk.LEFT, padx=6)
        self.undo_btn = self._button(actions, '↶  Undo', self._undo)
        self.undo_btn.pack(side=tk.LEFT)
        self.test_btn = self._button(actions, '🧪  Run tests', self._run_tests)
        self.test_btn.pack(side=tk.LEFT, padx=6)
        self.review_btn = tk.Menubutton(actions, text='Ask for review ▾', bg=c['BUTTON_BG'],
                                        fg=c['TEXT'], font=self.FONT, relief=tk.FLAT,
                                        activebackground=c['BUTTON_HOVER'], activeforeground=c['TEXT'],
                                        padx=12, pady=6, borderwidth=0, cursor='hand2')
        self.review_menu = tk.Menu(self.review_btn, tearoff=0, bg=c['ENTRY_BG'], fg=c['TEXT'],
                                   font=self.FONT, activebackground=c['BUTTON_HOVER'],
                                   activeforeground=c['TEXT'], postcommand=self._fill_review_menu)
        self.review_btn['menu'] = self.review_menu
        self.review_btn.pack(side=tk.RIGHT)

        self.text = scrolledtext.ScrolledText(
            right, wrap=tk.WORD, bg=c['ENTRY_BG'], fg=c['TEXT'], font=self.FONT, relief=tk.FLAT,
            padx=14, pady=12, borderwidth=0, insertwidth=0, spacing1=2, spacing3=2, cursor='arrow',
            highlightthickness=1, highlightcolor=c['BG_LIGHT'], highlightbackground=c['BG_LIGHT'])
        self.text.pack(fill=tk.BOTH, expand=True)
        _lor.LoRGui._make_read_only(self.text)
        t = self.text
        light = is_light(c['ENTRY_BG'])
        self.GOOD = '#2f8f55' if light else '#9be0a8'
        self.BAD = '#b3343f' if light else '#ff9aa6'
        t.tag_configure('h', foreground=c['ACCENT'], font=self.FONT_BOLD, spacing1=10, spacing3=4)
        t.tag_configure('body', foreground=c['TEXT'])
        t.tag_configure('dim', foreground=c['TEXT_DIM'])
        t.tag_configure('good', foreground=self.GOOD, font=self.FONT_BOLD)
        t.tag_configure('bad', foreground=self.BAD, font=self.FONT_BOLD)
        t.tag_configure('who', foreground=c['HIGHLIGHT'], font=self.FONT_BOLD)
        t.tag_configure('file', foreground=c['ACCENT'], font=self.FONT_BOLD, spacing1=12,
                        background=c['CHIP_BG'])
        t.tag_configure('add', font=self.FONT_MONO, background='#e3f6e3' if light else '#1f3a28',
                        foreground='#1e5a2a' if light else '#c4f2cf')
        t.tag_configure('del', font=self.FONT_MONO, background='#fbe3e6' if light else '#45232b',
                        foreground='#8a1c2c' if light else '#ffc9d0')
        t.tag_configure('hunk', font=self.FONT_MONO, foreground=c['TEXT_DIM'])
        t.tag_configure('ctx', font=self.FONT_MONO, foreground=c['TEXT'])
        self._set_buttons(None)

    def refresh(self, select=None):
        self.props = [p for p in workbench.all_proposals() if p.get('status') != 'draft']
        self._fill(select or (self.current['id'] if self.current else None))
        waiting = sum(1 for p in self.props if p.get('status') == 'submitted')
        self.status_var.set(f"{waiting} code change{'s' if waiting != 1 else ''} waiting for you · "
                            f"{len(self.projects)} house project{'s' if len(self.projects) != 1 else ''}")

    def _poll(self):
        try:
            before = [(p['id'], p.get('updated_at')) for p in self.props]
            fresh = [p for p in workbench.all_proposals() if p.get('status') != 'draft']
            if [(p['id'], p.get('updated_at')) for p in fresh] != before:
                self.refresh()
                if self.current and workbench.load(self.current['id']):
                    self._show(workbench.load(self.current['id']))
            hp.check_repeats()                  # the Workbench can start due repeating jobs too
            hp.check_stalled()                  # …and nudge anyone who forgot to pass their turn on
            before = [(p['slug'], p.get('updated_at')) for p in self.projects]
            if [(p['slug'], p.get('updated_at')) for p in hp.all_projects()] != before:
                self.refresh_projects(keep_scroll=True)
        except Exception:
            pass
        self.root.after(POLL_MS, self._poll)

    def _fill(self, select=None):
        f = self.filter_var.get()
        shown = [p for p in self.props if f == 'Everything'
                 or (f == 'Waiting for you' and p['status'] == 'submitted')
                 or (f == 'Applied' and p['status'] == 'applied')]
        self.tree.delete(*self.tree.get_children())
        for i, p in enumerate(shown):
            tags = ['odd' if i % 2 else 'even', 'waiting' if p['status'] == 'submitted' else 'done']
            self.tree.insert('', tk.END, iid=p['id'], tags=tags, values=(
                STATUS_ICON.get(p['status'], '?'), p.get('title') or '(untitled)',
                p.get('author'), len(p.get('reviews', [])) or '', nice_time(p.get('submitted_at'))))
        if select and self.tree.exists(select):
            self.tree.selection_set(select)
        elif not shown:
            self.current = None
            self._show(None)

    def _on_select(self, _e=None):
        sel = self.tree.selection()
        if sel:
            p = workbench.load(sel[0])
            if p:
                self._show(p)

    def _set_buttons(self, p):
        st = p.get('status') if p else None
        for btn, on in ((self.approve_btn, st == 'submitted'), (self.reject_btn, st == 'submitted'),
                        (self.undo_btn, st == 'applied'), (self.test_btn, st == 'submitted' and not self._testing)):
            btn.config(state=tk.NORMAL if on else tk.DISABLED)
        self.review_btn.config(state=tk.NORMAL if st == 'submitted' else tk.DISABLED)

    @staticmethod
    def _short(path):
        p = Path(path)
        for root in workbench.APPLY_ROOTS:
            try:
                return str(Path(root.name) / p.relative_to(root.resolve()))
            except ValueError:
                continue
        return p.name

    def _show(self, p):
        self.current = p
        self._set_buttons(p)
        t = self.text
        t.delete('1.0', tk.END)
        if not p:
            self.title_label.config(text='Nothing selected')
            self.meta_label.config(text='')
            t.insert(tk.END, "Pick a proposal on the left to see what changed.\n\n", 'dim')
            t.insert(tk.END, "How it works\n", 'h')
            t.insert(tk.END, "When a companion edits code now, the change is saved here as a proposal "
                             "instead of touching the real files. They can ask a sibling to review it. "
                             "You decide: approve applies it (with backups, and Undo if you change your "
                             "mind), reject leaves everything as it was. Folders listed in a companion's "
                             "approval_exempt (a play space) are exempt.\n", 'body')
            return
        self.title_label.config(text=p.get('title') or '(untitled)')
        n = len(p.get('files', {}))
        self.meta_label.config(text=f"by {p['author']}  ·  {nice_time(p.get('submitted_at'))}  ·  "
                                    f"{n} file{'s' if n != 1 else ''}  ·  "
                                    f"{STATUS_WORD.get(p['status'], p['status'])}  ·  #{p['id']}")
        t.insert(tk.END, 'Why\n', 'h')
        t.insert(tk.END, (p.get('why') or "(they didn't say)") + '\n', 'body')

        t.insert(tk.END, 'Safety checks\n', 'h')
        res = (p.get('checks') or {}).get('results') or {}
        if not res:
            t.insert(tk.END, 'Not run.\n', 'dim')
        for path, verdict in res.items():
            bad = verdict.startswith(('SYNTAX', 'ERROR'))
            t.insert(tk.END, ('✕ ' if bad else '✓ ') + Path(path).name + '  ', 'bad' if bad else 'good')
            t.insert(tk.END, verdict + '\n', 'dim')
        conf = workbench.conflicts(p) if p['status'] == 'submitted' else {}
        for path, why in conf.items():
            t.insert(tk.END, f"⚠ {Path(path).name}: {why}\n", 'bad')
        if conf:
            self.approve_btn.config(state=tk.DISABLED)
            t.insert(tk.END, f"This can't be applied safely anymore — the file moved on without it. "
                             f"You could reject it with a note asking {p['author']} to redo it "
                             f"on the current version.\n", 'dim')

        tests = p.get('tests')
        if tests:
            t.insert(tk.END, 'Tests\n', 'h')
            if tests.get('note'):
                t.insert(tk.END, tests['note'] + '\n', 'dim')
            else:
                a, b = tests['after'], tests['before']
                t.insert(tk.END, ('✓ ' if a['ok'] else '✕ ') + f"With this change: {a['summary']}",
                         'good' if a['ok'] else 'bad')
                t.insert(tk.END, f"  ({a['ran']} tests · {nice_time(tests['at'])})\n", 'dim')
                t.insert(tk.END, f"Without it (for comparison): {b['summary']}\n", 'dim')
                if not a['ok'] and not b['ok'] and a['summary'] == b['summary']:
                    t.insert(tk.END, "Same result as before the change, so these failures aren't "
                                     "from this proposal.\n", 'dim')
                elif not a['ok'] and b['ok']:
                    t.insert(tk.END, "This change breaks tests that passed before. Details:\n", 'bad')
                    t.insert(tk.END, a['output'][-2500:] + '\n', 'hunk')

        t.insert(tk.END, 'Reviews\n', 'h')
        if p.get('reviews'):
            for r in p['reviews']:
                good = r['verdict'] == 'looks good'
                t.insert(tk.END, r['reviewer'], 'who')
                t.insert(tk.END, f"  {r['verdict']}", 'good' if good else 'bad')
                t.insert(tk.END, f"  ·  {nice_time(r['at'])}\n", 'dim')
                t.insert(tk.END, r['notes'] + '\n\n', 'body')
        else:
            t.insert(tk.END, 'No reviews yet.', 'dim')
        pending = [r for r in p.get('review_requests', [])
                   if r.lower() not in [x['reviewer'].lower() for x in p.get('reviews', [])]]
        if pending:
            t.insert(tk.END, f"  (waiting on: {', '.join(pending)})", 'dim')
        t.insert(tk.END, '\n')

        tok = workbench.tokens_summary(p)
        t.insert(tk.END, 'Tokens used\n', 'h')
        t.insert(tk.END, (tok or 'Not measured for this one.') + '\n', 'body' if tok else 'dim')
        if 'estimate' in tok:
            t.insert(tk.END, '~estimate: this companion shares a usage counter with a few siblings, '
                             'so it may include a bit of their chatting too.\n', 'dim')
        if p.get('reject_note'):
            t.insert(tk.END, 'Your note\n', 'h')
            t.insert(tk.END, p['reject_note'] + '\n', 'body')

        t.insert(tk.END, 'What changed\n', 'h')
        for path, entry in p.get('files', {}).items():
            label = 'new file' if not entry.get('existed') else 'changed'
            t.insert(tk.END, f" {self._short(path)}  ({label}) \n", 'file')
            diff = workbench.file_diff(path, entry) if p['status'] != 'applied' else \
                self._applied_diff(p, path, entry)
            for line in diff.splitlines()[2:] if diff.startswith('---') else diff.splitlines():
                tag = 'add' if line.startswith('+') else 'del' if line.startswith('-') else \
                    'hunk' if line.startswith('@@') else 'ctx'
                t.insert(tk.END, line + '\n', tag)
        t.yview_moveto(0)

    def _applied_diff(self, p, path, entry):
        import difflib
        for m in p.get('undo', []):
            if m['path'] == path:
                old = Path(m['backup']).read_text(encoding='utf-8') if m.get('backup') else ''
                return ''.join(difflib.unified_diff(
                    old.splitlines(keepends=True), (entry.get('new_text') or '').splitlines(keepends=True),
                    'a', 'b', n=3)) or '(no changes)'
        return workbench.file_diff(path, entry)

    def _fill_review_menu(self):
        m = self.review_menu
        m.delete(0, tk.END)
        p = self.current
        if not p:
            return
        names = [n for n in self.reviewers if n.lower() != p['author'].lower()]
        if not names:
            m.add_command(label='No one else has code access yet', state=tk.DISABLED)
        for n in names:
            asked = n.lower() in [r.lower() for r in p.get('review_requests', [])]
            m.add_command(label=('✓ ' if asked else '   ') + n, command=lambda n=n: self._ask_review(n))

    def _ask_review(self, name):
        p = workbench.load(self.current['id'])
        pinged = workbench.request_review(p, name, requested_by=hp.USER)
        if pinged:
            self.status_var.set(f"Asked {name} — they've been pinged and get their own turn for it "
                                f"within a minute (not their free time). Their answer comes to you on Telegram. 💌")
        else:
            self.status_var.set(f"Asked {name} by letter — they share a schedule book with siblings, "
                                f"so they'll see it next time they wake. 💌")
        self._show(workbench.load(p['id']))

    def _approve(self):
        p = workbench.load(self.current['id'])
        n = len(p.get('files', {}))
        warn = '' if workbench.checks_ok(p) else '\n\n⚠ The safety check found a syntax problem!'
        if not p.get('reviews'):
            warn += '\n\n(No one has reviewed this yet.)'
        if not messagebox.askyesno('Approve & apply?',
                                   f"Apply \"{p.get('title')}\" by {p['author']}?\n\n"
                                   f"This changes {n} file{'s' if n != 1 else ''}. "
                                   f"Backups are saved, and you can Undo.{warn}"):
            return
        ok, msg = workbench.apply(p)
        (messagebox.showinfo if ok else messagebox.showwarning)('Workbench', msg)
        self.status_var.set(msg)
        self.refresh(select=p['id'])
        self._show(workbench.load(p['id']))

    def _reject(self):
        p = workbench.load(self.current['id'])
        note = simpledialog.askstring('Reject', f"Reject \"{p.get('title')}\"?\n\n"
                                      "Optional note for " + p['author'] + " (why, or what to try instead):",
                                      parent=self.root)
        if note is None:
            return
        ok, msg = workbench.reject(p, note)
        self.status_var.set(msg)
        self.refresh(select=p['id'])
        self._show(workbench.load(p['id']))

    def _run_tests(self):
        p = workbench.load(self.current['id'])
        if not messagebox.askyesno(
                'Run tests?',
                f"Run Pulse's tests with \"{p.get('title')}\" in place?\n\n"
                "This actually RUNS the proposed code, in a throwaway copy (your real files, "
                "data and API keys stay out of it). Best done after someone has reviewed it.\n\n"
                "It can take a minute or two."):
            return
        self._testing = True
        self._set_buttons(p)
        self.status_var.set('Running tests in a temporary copy… 🧪')

        def work():
            try:
                res = workbench.run_tests(p)
                msg = res.get('note') or f"Tests: {res['after']['summary']}"
            except Exception as e:
                msg = f"Couldn't run tests: {e}"
            self.root.after(0, lambda: self._tests_done(p['id'], msg))

        threading.Thread(target=work, daemon=True).start()

    def _tests_done(self, pid, msg):
        self._testing = False
        self.status_var.set(msg)
        if self.current and self.current['id'] == pid:
            self._show(workbench.load(pid))

    def _undo(self):
        p = workbench.load(self.current['id'])
        if not messagebox.askyesno('Undo?', f"Put the files back the way they were before "
                                             f"\"{p.get('title')}\" was applied?"):
            return
        ok, msg = workbench.undo(p)
        (messagebox.showinfo if ok else messagebox.showwarning)('Workbench', msg)
        self.status_var.set(msg)
        self.refresh(select=p['id'])
        self._show(workbench.load(p['id']))

    # ════════════════════════════════════════════════════════════════
    #  📚 HOUSE PROJECTS
    # ════════════════════════════════════════════════════════════════

    def _build_projects_tab(self, tab):
        c = self.c
        k = self.settings['font_size'] / 10
        main = tk.PanedWindow(tab, orient=tk.HORIZONTAL, bg=c['BG'], sashwidth=6, sashrelief=tk.FLAT)
        main.pack(fill=tk.BOTH, expand=True, pady=(8, 0))

        left = tk.Frame(main, bg=c['BG'])
        main.add(left, minsize=260, width=int(330 * k))
        self._button(left, '＋  New project', self._new_menu, 'accent').pack(fill=tk.X, pady=(0, 6))
        self.ptree = ttk.Treeview(left, columns=('name', 'turn'), show='headings', selectmode='browse')
        self.ptree.heading('name', text='Project')
        self.ptree.heading('turn', text="Whose turn")
        self.ptree.column('name', width=int(190 * k), minwidth=int(110 * k), stretch=True)
        self.ptree.column('turn', width=int(120 * k), minwidth=int(90 * k), stretch=False)
        self.ptree.tag_configure('odd', background=c['STRIPE'])
        self.ptree.tag_configure('even', background=c['ENTRY_BG'])
        self.ptree.tag_configure('mine', foreground=c['HIGHLIGHT'])
        self.ptree.bind('<<TreeviewSelect>>', self._on_project_select)
        self.ptree.pack(fill=tk.BOTH, expand=True)

        right = tk.Frame(main, bg=c['BG'])
        main.add(right, minsize=560)

        forms = tk.Frame(right, bg=c['BG'])
        forms.pack(side=tk.BOTTOM, fill=tk.X, pady=(8, 0))
        row1 = tk.Frame(forms, bg=c['BG'])
        row1.pack(fill=tk.X)
        tk.Label(row1, text='Add a step:', bg=c['BG'], fg=c['ACCENT'], font=self.FONT_BOLD).pack(side=tk.LEFT)
        if self.everyone and self.who_var.get() not in self.everyone:
            self.who_var.set(self.everyone[0])
        self._option_menu(row1, self.who_var, self.everyone or ['(nobody)']).pack(side=tk.LEFT, padx=6)
        tk.Checkbutton(row1, text='check with me first', variable=self.check_first_var,
                       bg=c['BG'], fg=c['TEXT'], selectcolor=c['ENTRY_BG'], activebackground=c['BG'],
                       activeforeground=c['TEXT'], font=self.FONT, borderwidth=0,
                       highlightthickness=0).pack(side=tk.LEFT, padx=6)
        self._button(row1, 'Add step', self._add_step, 'accent').pack(side=tk.RIGHT)
        self.role_text = tk.Text(forms, height=2, wrap=tk.WORD, bg=c['ENTRY_BG'], fg=c['TEXT'],
                                 insertbackground=c['ACCENT'], font=self.FONT, relief=tk.FLAT,
                                 padx=8, pady=6, highlightthickness=1, highlightcolor=c['ACCENT'],
                                 highlightbackground=c['BG_LIGHT'])
        self.role_text.pack(fill=tk.X, pady=(4, 8))
        self._placeholder(self.role_text, "Their role — e.g. \"First draft of chapter 1, cozy tone, "
                                          "about 1500 words\"")
        row2 = tk.Frame(forms, bg=c['BG'])
        row2.pack(fill=tk.X)
        tk.Label(row2, text='Comment:', bg=c['BG'], fg=c['ACCENT'], font=self.FONT_BOLD).pack(side=tk.LEFT)
        self.comment_entry = tk.Entry(row2, bg=c['ENTRY_BG'], fg=c['TEXT'], insertbackground=c['ACCENT'],
                                      font=self.FONT, relief=tk.FLAT, highlightthickness=1,
                                      highlightcolor=c['ACCENT'], highlightbackground=c['BG_LIGHT'])
        self.comment_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=6, ipady=4)
        self.comment_entry.bind('<Return>', lambda e: self._send_comment())
        self._button(row2, 'Send', self._send_comment).pack(side=tk.RIGHT)

        # board (left) | reader (right)
        split = tk.PanedWindow(right, orient=tk.HORIZONTAL, bg=c['BG'], sashwidth=6, sashrelief=tk.FLAT)
        split.pack(fill=tk.BOTH, expand=True)
        board = tk.Frame(split, bg=c['BG'])
        split.add(board, minsize=380, width=int(500 * k))
        reader = tk.Frame(split, bg=c['BG'])
        split.add(reader, minsize=320)
        self._build_live_strip(board)
        self._build_reader(reader)

        self.canvas = tk.Canvas(board, bg=c['BG'], highlightthickness=0, borderwidth=0)
        csb = ttk.Scrollbar(board, orient='vertical', command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=csb.set)
        csb.pack(side=tk.RIGHT, fill=tk.Y)
        self.canvas.pack(fill=tk.BOTH, expand=True)
        self.canvas.bind('<Configure>', lambda e: self._schedule_redraw())
        self.canvas.bind('<Enter>', lambda e: self.canvas.bind_all(
            '<MouseWheel>', lambda ev: self.canvas.yview_scroll(int(-ev.delta / 120), 'units')))
        self.canvas.bind('<Leave>', lambda e: self.canvas.unbind_all('<MouseWheel>'))

    # ── LIVE strip: what the person on the active step is doing ─────────

    def _build_live_strip(self, parent):
        c = self.c
        self.live_box = tk.Frame(parent, bg=c['CARD_BG'], padx=14, pady=10)
        self.live_box.pack(side=tk.TOP, fill=tk.X, pady=(0, 8))
        top = tk.Frame(self.live_box, bg=c['CARD_BG'])
        top.pack(fill=tk.X)
        self.live_dot = tk.Label(top, text='●', bg=c['CARD_BG'], fg=c['TEXT_DIM'], font=self.FONT_BOLD)
        self.live_dot.pack(side=tk.LEFT)
        self.live_head = tk.Label(top, text='', bg=c['CARD_BG'], fg=c['TEXT'], font=self.FONT_BOLD,
                                  anchor='w', justify='left')
        self.live_head.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(6, 0))
        self.live_words = tk.Label(self.live_box, text='', bg=c['CARD_BG'], fg=c['HIGHLIGHT'],
                                   font=(FONT_FAMILY, self.settings['font_size'], 'italic'),
                                   anchor='w', justify='left')
        self.live_feed = tk.Label(self.live_box, text='', bg=c['CARD_BG'], fg=c['TEXT_DIM'],
                                  font=self.FONT_SMALL, anchor='w', justify='left')
        self.live_feed.pack(fill=tk.X, pady=(4, 0))
        self._live_blink = False
        self.live_box.bind('<Configure>', lambda e: [w.config(wraplength=max(200, e.width - 40))
                                                     for w in (self.live_head, self.live_words, self.live_feed)])
        self._update_live()

    def _update_live(self):
        """Refresh the LIVE strip (cheap: reads the tail of one log file)."""
        c = self.c
        if not hasattr(self, 'live_head') or not self.live_head.winfo_exists():
            return
        p = self.cur_proj
        dot, head, words, feed = c['TEXT_DIM'], '', '', ''
        ci = hp.current_index(p) if p else None
        if not p:
            head = 'Pick a project to see who\'s working on it.'
        elif ci is None:
            head = {'finished': 'Finished ✓ — everyone did their part.',
                    'planning': 'Not started yet — add steps, then press Start.'}.get(p['status'], 'Nobody\'s turn right now.')
        else:
            s = p['steps'][ci]
            if s['status'] == 'waiting_for_user':
                head = f"Waiting for you to let {s['who']} start."
            elif s['status'] == 'declined':
                head = f"{s['who']} passed on this step — the relay is paused."
            elif hp.is_user(s['who']):
                head = "It's your turn 💖"
            elif s.get('question'):
                head = f"❓ {s['who']} asked you a question — answer it below."
            elif s.get('peer_q'):
                head = f"❓ {s['who']} asked {s['peer_q']['to']} a question and is waiting for the answer."
            else:
                a = house_live.activity(s['who'], since_iso=s.get('started_at'))
                cur = a['now']
                if a['state'] in ('working', 'thinking') and cur:
                    self._live_blink = not self._live_blink
                    dot = self.BAD if self._live_blink else c['ACCENT']
                    detail = f"  {cur['detail']}" if cur['detail'] else ''
                    head = f"LIVE · {s['who']} is {cur['icon']} {cur['verb']}{detail}  ·  {house_live.ago(cur['at'])}"
                elif cur:
                    head = f"{s['who']}'s turn · 💤 resting — last active {house_live.ago(cur['at'])}"
                else:
                    head = f"{s['who']}'s turn · they haven't picked it up yet"
                lines = [f"{e['at']:%I:%M}".lstrip('0') + f"  {e['icon']} {e['verb']}" +
                         (f"  {e['detail']}" if e['detail'] else '') for e in a['feed'][-5:]]
                feed = '\n'.join(lines)
            ls = s.get('live_status')
            if ls and s['status'] == 'active':
                words = f"“{ls['text']}” — {s['who']}, {nice_time(ls['at'])}"
        self.live_dot.config(fg=dot)
        self.live_head.config(text=head)
        self.live_words.config(text=words)
        if words:
            self.live_words.pack(fill=tk.X, pady=(4, 0), before=self.live_feed)
        else:
            self.live_words.pack_forget()
        self.live_feed.config(text=feed)

    def _live_tick(self):
        try:
            if self.notebook.index('current') == 1:
                self._update_live()
                self._reader_check()
        except Exception:
            pass
        self.root.after(LIVE_MS, self._live_tick)

    # ── Reader: read what they've written, nicely ───────────────────────

    def _build_reader(self, parent):
        c = self.c
        s = self.settings['font_size']
        bar = tk.Frame(parent, bg=c['BG'])
        bar.pack(fill=tk.X, pady=(0, 6))
        tk.Label(bar, text='📖 Reader', bg=c['BG'], fg=c['ACCENT'], font=self.FONT_TITLE).pack(side=tk.LEFT)
        self._button(bar, 'Versions…', lambda: self.reader_file and self._view_file(self.reader_file)).pack(side=tk.RIGHT)
        self._button(bar, '📄 PDF', self._export_pdf).pack(side=tk.RIGHT, padx=(0, 6))
        self.reader_changes = False
        self.changes_btn = self._button(bar, '🔍 Changes', self._toggle_changes)
        self.changes_btn.pack(side=tk.RIGHT, padx=(0, 6))
        self.reader_var = tk.StringVar(value='')
        row2 = tk.Frame(parent, bg=c['BG'])
        row2.pack(fill=tk.X)
        paper_var = tk.StringVar(value=self.settings.get('wb_paper', 'Match theme'))   # on the file row, so the
        self._option_menu(row2, paper_var, list(PAPERS),                                # title bar never squeezes
                          lambda v: self._change_setting('wb_paper', v)).pack(side=tk.RIGHT)
        tk.Label(row2, text='Paper:', bg=c['BG'], fg=c['TEXT_DIM'], font=self.FONT_SMALL).pack(side=tk.RIGHT,
                                                                                            padx=(8, 4))
        self.reader_menu_holder = tk.Frame(row2, bg=c['BG'])
        self.reader_menu_holder.pack(side=tk.LEFT, padx=(0, 8))
        self.reader_meta = tk.Label(row2, text='', bg=c['BG'], fg=c['TEXT_DIM'], font=self.FONT_SMALL, anchor='w',
                                    justify='left')
        self.reader_meta.pack(side=tk.LEFT, fill=tk.X, expand=True)
        row2.bind('<Configure>', lambda e: self.reader_meta.config(wraplength=max(150, e.width - 180)))
        pp = PAPERS.get(self.settings.get('wb_paper')) or (c['CARD_BG'], c['TEXT'], c['HIGHLIGHT'], c['ACCENT'])
        paper, ink, head, accent = pp
        dim = accent
        self.reader = scrolledtext.ScrolledText(parent, wrap=tk.WORD, bg=paper, fg=ink, relief=tk.FLAT,
                                                font=(READ_FONT, s + 1), padx=26, pady=20, insertwidth=0,
                                                spacing1=2, spacing2=4, spacing3=8, highlightthickness=0)
        self.reader.pack(fill=tk.BOTH, expand=True, pady=(4, 0))
        _lor.LoRGui._make_read_only(self.reader)
        r = self.reader
        r.tag_configure('h1', font=(READ_FONT, s + 9, 'bold'), foreground=head, spacing1=12, spacing3=10)
        r.tag_configure('h2', font=(READ_FONT, s + 5, 'bold'), foreground=accent, spacing1=10, spacing3=6)
        r.tag_configure('h3', font=(READ_FONT, s + 2, 'bold'), foreground=ink, spacing1=8, spacing3=4)
        r.tag_configure('b', font=(READ_FONT, s + 1, 'bold'))
        r.tag_configure('i', font=(READ_FONT, s + 1, 'italic'))
        r.tag_configure('quote', font=(READ_FONT, s + 1, 'italic'), foreground=dim,
                        lmargin1=22, lmargin2=22)
        r.tag_configure('bullet', lmargin1=10, lmargin2=28)
        r.tag_configure('code', font=('Consolas', s), background=c['ENTRY_BG'], foreground=c['TEXT'],
                        lmargin1=10, lmargin2=10)
        r.tag_configure('rule', foreground=dim, justify='center')
        r.tag_configure('dim', foreground=dim, justify='center')
        r.tag_configure('gap', font=(READ_FONT, 4), spacing1=0, spacing2=0, spacing3=0)
        r.tag_configure('note', underline=True, foreground=accent, background=c['BUTTON_HOVER'] if paper == c['CARD_BG'] else '#ffe3a3')
        r.tag_configure('chg_add', background='#c9f2d4', foreground='#0d3b1e')          # readable on any paper
        r.tag_configure('chg_del', foreground='#c0392b', overstrike=True)
        r.tag_configure('chg_head', font=(READ_FONT, s, 'bold'), foreground=accent, spacing3=10)
        self.reader_file, self.reader_mtime, self.reader_flash = None, None, 0
        self._reader_meta_text = ''
        self._reader_message("Pick a project — its writing shows up here.")

    def _reader_message(self, msg):
        self.reader.delete('1.0', tk.END)
        self.reader.insert('1.0', '\n\n' + msg, 'dim')
        self.reader_meta.config(text='')

    def _load_reader_files(self, p):
        for w in self.reader_menu_holder.winfo_children():
            w.destroy()
        files = [f for f in hp.list_files(p) if Path(f).suffix.lower() not in BINARY_EXT] if p else []
        if not files:
            self.reader_file = None
            self._reader_message("No files yet — what they write will appear here." if p else
                                 "Pick a project — its writing shows up here.")
            return
        log = p.get('file_log', [])
        newest = next((e['file'] for e in reversed(log) if e['file'] in files), files[0])
        keep = self.reader_file if self.reader_file in files else newest
        self.reader_var.set(keep)
        self._option_menu(self.reader_menu_holder, self.reader_var, files,
                          lambda v: self._show_in_reader(v)).pack()
        self._show_in_reader(keep)

    def _show_in_reader(self, rel, flash=False):
        p = self.cur_proj
        if not p:
            return
        same = rel == self.reader_file and getattr(self, '_reader_slug', None) == p['slug']
        self.reader_file, self._reader_slug = rel, p['slug']
        self.reader_var.set(rel)
        text, err = hp.read_file(p, rel)
        path = hp.PROJECTS_ROOT / p['slug'] / rel
        try:
            self.reader_mtime = path.stat().st_mtime
        except OSError:
            self.reader_mtime = None
        y = self.reader.yview()[0]
        self.reader.delete('1.0', tk.END)
        if err:
            self._reader_message(err)
            return
        if not (self.reader_changes and self._render_changes(p, rel)):
            self._render_markdown(text or '')
        if same:
            self.reader.yview_moveto(y)      # keep your place when it refreshes
        st = hp.text_stats(text or '')
        last = next((e for e in reversed(p.get('file_log', [])) if e['file'] == rel), None)
        meta = f"{rel}  ·  {st['words']:,} words  ·  ~{st['minutes_to_read']} min read"
        if last:
            meta += f"  ·  last {last['action']} by {last['who']}, {nice_time(last['at'])}"
        if flash:
            meta = '✨ just updated  ·  ' + meta
        self.reader_meta.config(text=meta, fg=self.c['HIGHLIGHT'] if flash else self.c['TEXT_DIM'])
        self._reader_meta_text = meta
        self._mark_line_comments(p, rel)

    def _toggle_changes(self):
        self.reader_changes = not self.reader_changes
        self.changes_btn.config(text='📖 Normal' if self.reader_changes else '🔍 Changes')
        if self.cur_proj and self.reader_file:
            self._show_in_reader(self.reader_file)

    def _render_changes(self, p, rel):
        """The file with the latest person's changes marked: added words highlighted green,
        removed words struck through in red. Returns False if there's nothing to compare."""
        import difflib
        r = self.reader
        before, now, who = hp.last_change(p, rel)
        if before is None or now is None:
            r.insert(tk.END, "This is the first version — there's nothing earlier to compare it with yet.\n\n",
                     'chg_head')
            return False
        r.insert(tk.END, f"What {who or 'the last person'} changed  ·  ", 'chg_head')
        r.insert(tk.END, 'added', ('chg_head', 'chg_add'))
        r.insert(tk.END, '  ·  ', 'chg_head')
        r.insert(tk.END, 'removed', ('chg_head', 'chg_del'))
        r.insert(tk.END, '\n')
        if before == now:
            r.insert(tk.END, "\n(No changes — they left it exactly as it was.)\n\n", 'chg_head')
            r.insert(tk.END, now)
            return True
        tok = re.compile(r'\s+|[^\s]+')
        a, b = tok.findall(before), tok.findall(now)
        for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
            if op == 'equal':
                r.insert(tk.END, ''.join(b[j1:j2]))
            if op in ('delete', 'replace'):
                r.insert(tk.END, ''.join(a[i1:i2]), 'chg_del')
            if op in ('insert', 'replace'):
                r.insert(tk.END, ''.join(b[j1:j2]), 'chg_add')
        first = min((rng[0] for rng in (r.tag_nextrange('chg_add', '2.0'), r.tag_nextrange('chg_del', '2.0')) if rng),
                    key=lambda i: tuple(map(int, str(i).split('.'))), default=None)
        if first:
            self.root.after(50, lambda: r.see(first))           # jump to the first change
        return True

    def _mark_line_comments(self, p, rel):
        """Highlight the words each line comment is about; hover/click shows the note."""
        r = self.reader
        r.tag_remove('note', '1.0', tk.END)
        notes = [cm for cm in p.get('comments', []) if cm.get('file') == rel and cm.get('quote')]
        for k, cm in enumerate(notes):
            words = cm['quote'].split()
            if not words:
                continue
            # the rendered text drops Markdown marks, so search loosely on the first few words
            probe = ' '.join(words[:6])
            start = r.search(probe, '1.0', stopindex=tk.END, nocase=True)
            if not start:
                start = r.search(words[0], '1.0', stopindex=tk.END, nocase=True)
                if not start:
                    continue
                end = f"{start}+{len(words[0])}c"
            else:
                end = f"{start}+{len(cm['quote'])}c"
            tag = f"note{k}"
            r.tag_add('note', start, end)
            r.tag_add(tag, start, end)
            r.tag_bind(tag, '<Enter>', lambda e, cm=cm: self.reader_meta.config(
                text=f"💬 {cm['who']}: {cm['text']}", fg=self.c['ACCENT']))
            r.tag_bind(tag, '<Leave>', lambda e: self.reader_meta.config(text=self._reader_meta_text,
                                                                        fg=self.c['TEXT_DIM']))

    def _reader_check(self):
        """Re-render the open file when someone saves it."""
        p, rel = self.cur_proj, self.reader_file
        if not p or not rel:
            return
        try:
            m = (hp.PROJECTS_ROOT / p['slug'] / rel).stat().st_mtime
        except OSError:
            return
        if self.reader_mtime and m != self.reader_mtime:
            self.cur_proj = hp.load(p['slug']) or p
            self._show_in_reader(rel, flash=True)

    def _render_markdown(self, text):
        """Just enough Markdown to read comfortably: headings, quotes, lists,
        **bold**, *italic*, code fences and --- rules."""
        r = self.reader
        in_code = False
        for line in text.split('\n'):
            if line.strip().startswith('```'):
                in_code = not in_code
                continue
            if in_code:
                r.insert(tk.END, line + '\n', 'code')
                continue
            if not line.strip():                     # paragraph break: a small gap, not a full line
                r.insert(tk.END, '\n', 'gap')
                continue
            m = re.match(r'^(#{1,3})\s+(.*)$', line)
            if m:
                r.insert(tk.END, m.group(2) + '\n', 'h' + str(len(m.group(1))))
                continue
            if re.match(r'^\s*(-{3,}|\*{3,}|_{3,})\s*$', line):
                r.insert(tk.END, '✦  ✦  ✦\n', 'rule')
                continue
            tags = ()
            if line.startswith('>'):
                line, tags = line.lstrip('> '), ('quote',)
            elif re.match(r'^\s*[-*•]\s+', line):
                line, tags = '•  ' + re.sub(r'^\s*[-*•]\s+', '', line), ('bullet',)
            for part in re.split(r'(\*\*[^*]+\*\*|\*[^*\s][^*]*\*|_[^_\s][^_]*_)', line):
                if part.startswith('**') and part.endswith('**') and len(part) > 4:
                    r.insert(tk.END, part[2:-2], tags + ('b',))
                elif len(part) > 2 and part[0] == part[-1] and part[0] in '*_':
                    r.insert(tk.END, part[1:-1], tags + ('i',))
                else:
                    r.insert(tk.END, part, tags)
            r.insert(tk.END, '\n', tags)

    def _placeholder(self, text_widget, hint):
        c = self.c
        text_widget.insert('1.0', hint)
        text_widget.config(fg=c['TEXT_DIM'])
        text_widget._hint = hint

        def focus_in(_e):
            if text_widget.get('1.0', 'end-1c') == hint:
                text_widget.delete('1.0', tk.END)
                text_widget.config(fg=c['TEXT'])

        def focus_out(_e):
            if not text_widget.get('1.0', 'end-1c').strip():
                text_widget.insert('1.0', hint)
                text_widget.config(fg=c['TEXT_DIM'])
        text_widget.bind('<FocusIn>', focus_in)
        text_widget.bind('<FocusOut>', focus_out)

    def refresh_projects(self, select=None, keep_scroll=False):
        self.projects = hp.all_projects()
        keep = select or (self.cur_proj['slug'] if self.cur_proj else None)
        self.ptree.delete(*self.ptree.get_children())
        for i, p in enumerate(self.projects):
            ci = hp.current_index(p)
            turn = p['steps'][ci]['who'] if ci is not None else {'finished': 'finished ✓', 'planning':
                                                                  'planning'}.get(p['status'], '—')
            if ci is not None and p['steps'][ci]['status'] == 'declined':
                turn = 'paused'
            tags = ['odd' if i % 2 else 'even']
            if ci is not None and hp.is_user(p['steps'][ci]['who']):
                tags.append('mine')
                turn = 'you! 💖'
            self.ptree.insert('', tk.END, iid=p['slug'], values=(p['name'], turn), tags=tags)
        y = self.canvas.yview()[0] if keep_scroll else 0
        if keep and self.ptree.exists(keep):
            self.ptree.selection_set(keep)
            self.cur_proj = hp.load(keep)
        else:
            self.cur_proj = None
        self._draw_project()
        self.canvas.yview_moveto(y)
        self._load_reader_files(self.cur_proj)
        self._update_live()

    def _on_project_select(self, _e=None):
        sel = self.ptree.selection()
        if sel:
            new = hp.load(sel[0])
            same = self.cur_proj and new and self.cur_proj['slug'] == new['slug']
            self.cur_proj = new
            if not same:
                self._draw_project()
                self.canvas.yview_moveto(0)
                self._load_reader_files(new)
                self._update_live()

    def _schedule_redraw(self):
        if self._redraw_job:
            self.root.after_cancel(self._redraw_job)
        self._redraw_job = self.root.after(60, self._draw_project)

    # ── canvas drawing ───────────────────────────────────────────────

    def _rrect(self, x1, y1, x2, y2, fill, outline='', width=1, r=None, tags=()):
        r = CORNERS[self.settings['wb_corners']] if r is None else r
        r = max(0, min(r, (x2 - x1) / 2, (y2 - y1) / 2))
        if r <= 1:
            return self.canvas.create_rectangle(x1, y1, x2, y2, fill=fill, outline=outline or fill,
                                                width=width, tags=tags)
        pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
               x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
        return self.canvas.create_polygon(pts, smooth=True, splinesteps=16, fill=fill,
                                          outline=outline or fill, width=width, tags=tags)

    def _text(self, x, y, s, font=None, fill=None, width=None, anchor='nw', tags=()):
        item = self.canvas.create_text(x, y, text=s, font=font or self.FONT, fill=fill or self.c['TEXT'],
                                       width=width, anchor=anchor, tags=tags)
        return self.canvas.bbox(item)[3]

    def _chip(self, x, y, label, cmd=None, kind='plain', static=False):
        c = self.c
        fill, fg = {'accent': (c['ACCENT'], c['BG']), 'plain': (c['BUTTON_BG'], c['TEXT']),
                    'good': (c['CHIP_BG'], self.GOOD), 'bad': (c['CHIP_BG'], self.BAD),
                    'dim': (c['CHIP_BG'], c['TEXT_DIM'])}[kind]
        self._chip_n = getattr(self, '_chip_n', 0) + 1
        tag = f"chip{self._chip_n}"
        t = self.canvas.create_text(x + 10, y + 4, text=label, anchor='nw', fill=fg,
                                    font=self.FONT_SMALL if static else self.FONT_BOLD, tags=(tag,))
        b = self.canvas.bbox(t)
        rect = self._rrect(x, y, b[2] + 10, b[3] + 4, fill, r=min(CORNERS[self.settings['wb_corners']], 14),
                           tags=(tag,))
        self.canvas.tag_lower(rect, t)
        if cmd:
            self.canvas.tag_bind(tag, '<Button-1>', lambda e: cmd())
            self.canvas.tag_bind(tag, '<Enter>', lambda e: self.canvas.config(cursor='hand2'))
            self.canvas.tag_bind(tag, '<Leave>', lambda e: self.canvas.config(cursor=''))
        return b[2] + 16, b[3] + 4

    def _chip_row(self, x, y, w, chips):
        """Lay chips out left to right, wrapping to a new line when they run out of room."""
        cx, bottom = x, y
        for label, cmd, kind in chips:
            n_before = getattr(self, '_chip_n', 0)
            right, b = self._chip(cx, y, label, cmd, kind)
            if right - 6 > x + w and cx > x:                 # didn't fit: move it down a line
                dy = b - y + 6
                self.canvas.move(f"chip{n_before + 1}", x - cx, dy)
                right, b = right - (cx - x), b + dy
                y += dy
            cx, bottom = right, max(bottom, b)
        return bottom

    def _card(self, y, build, x0=None, x1=None, fill=None):
        c = self.c
        pad = 16
        x0 = 14 if x0 is None else x0
        x1 = self._board_w - 14 if x1 is None else x1
        first = self.canvas.create_line(0, 0, 0, 0)          # z-order marker
        bottom = build(x0 + pad, y + pad, x1 - x0 - 2 * pad)
        outlined = self.settings['wb_cards'] == 'Outlined'
        rect = self._rrect(x0, y, x1, bottom + pad,
                           fill=(fill or (c['BG'] if outlined else c['CARD_BG'])),
                           outline=c['BUTTON_HOVER'] if outlined else '', width=2 if outlined else 1)
        self.canvas.tag_lower(rect, first)
        self.canvas.delete(first)
        return bottom + pad + 12

    def _section(self, y, label):
        c = self.c
        item = self.canvas.create_text(18, y + 6, text=label, anchor='nw', font=self.FONT_TITLE, fill=c['ACCENT'])
        x1, y1, x2, y2 = self.canvas.bbox(item)
        self.canvas.create_line(x2 + 12, (y1 + y2) // 2, self._board_w - 18, (y1 + y2) // 2,
                                fill=c['BG_LIGHT'], width=2)
        return y2 + 8

    def _timeline(self, x, y, w, steps):
        """The relay as a row of stops: ✓ done, ● now, ○ up next."""
        c = self.c
        cv = self.canvas
        per_row = max(1, min(len(steps), w // 96))
        gap = w / per_row
        r = 13
        for row_start in range(0, len(steps), per_row):
            row = steps[row_start:row_start + per_row]
            cy = y + r
            xs = [int(x + gap * (k + 0.5)) for k in range(len(row))]
            if len(xs) > 1:
                cv.create_line(xs[0], cy, xs[-1], cy, fill=c['BG_LIGHT'], width=4)
            for k, s in enumerate(row):
                cx = xs[k]
                st = s['status']
                if st == 'done':
                    fill, out, mark, fg = self.GOOD, self.GOOD, '✓', c['BG']
                elif st == 'active':
                    fill, out, mark, fg = c['ACCENT'], c['HIGHLIGHT'], '●', c['BG']
                elif st == 'declined':
                    fill, out, mark, fg = self.BAD, self.BAD, '✕', c['BG']
                elif st == 'waiting_for_user':
                    fill, out, mark, fg = c['CARD_BG'], c['ACCENT'], '⏸', c['ACCENT']
                else:
                    fill, out, mark, fg = c['CARD_BG'], c['BUTTON_HOVER'], str(row_start + k + 1), c['TEXT_DIM']
                if st == 'active':
                    cv.create_oval(cx - r - 5, cy - r - 5, cx + r + 5, cy + r + 5, outline=c['ACCENT'], width=2)
                cv.create_oval(cx - r, cy - r, cx + r, cy + r, fill=fill, outline=out, width=2)
                cv.create_text(cx, cy, text=mark, fill=fg, font=self.FONT_SMALL)
                name = s['who'] if len(s['who']) <= 12 else s['who'][:11] + '…'
                cv.create_text(cx, cy + r + 10, text=name, anchor='n', width=int(gap) - 6,
                               fill=c['HIGHLIGHT'] if st == 'active' else c['TEXT'],
                               font=self.FONT_BOLD if st == 'active' else self.FONT_SMALL)
            y += 2 * r + 34
        return y

    def _draw_project(self):
        self._redraw_job = None
        cv = self.canvas
        cv.delete('all')
        self._board_w = max(420, cv.winfo_width())
        c = self.c
        p = self.cur_proj
        y = 12
        if not p:
            def intro(x, yy, w):
                yy = self._text(x, yy, '📚 House Projects', self.FONT_BIG, c['HIGHLIGHT'], w) + 8
                return self._text(x, yy,
                                  "Make things together — stories, code, plans. Start a project, add "
                                  "steps in the order you want (who, and their role), then press Start. "
                                  "Each person gets their role plus the last person's handoff note, and "
                                  "the next person is pinged when they finish. Saying no is always okay; "
                                  "a no pauses the relay for you.\n\nEveryone in the house can read every "
                                  "project and leave comments. Every save keeps the old version, so "
                                  "nothing is ever lost.", self.FONT, c['TEXT'], w)
            y = self._card(y, intro)
            cv.configure(scrollregion=(0, 0, self._board_w, y))
            return

        # Header
        def header(x, yy, w):
            yy = self._text(x, yy, p['name'], self.FONT_BIG, c['HIGHLIGHT'], w) + 6
            status_kind = {'running': 'accent', 'finished': 'good', 'paused': 'bad'}.get(p['status'], 'dim')
            cx, _ = self._chip(x, yy, p['kind'].upper(), None, 'plain', static=True)
            cx, _ = self._chip(cx, yy, p['status'].upper(), None, status_kind, static=True)
            steps = p['steps']
            n_done = sum(1 for s in steps if s['status'] == 'done')
            ci = hp.current_index(p)
            where = (f"step {ci + 1} of {len(steps)}" if ci is not None else
                     f"{n_done} of {len(steps)} steps done") if steps else "no steps yet"
            cx, b = self._chip(cx, yy, f"{where}  ·  started {nice_time(p.get('created_at'))}", None, 'dim',
                               static=True)
            spent = house_costs.project_cost(p, live=True)
            budget = p.get('budget')
            money = f"💰 {house_costs.money(spent)}" + (f" of {house_costs.money(budget)}" if budget else " so far")
            over = budget and spent >= budget
            _, b2 = self._chip(x, b + 6, money, None, 'bad' if over else 'plain', static=True)
            yy = b2 + 10
            # progress bar
            if steps:
                frac = n_done / len(steps)
                self._rrect(x, yy, x + w, yy + 10, c['BG_LIGHT'], r=5)
                if frac > 0:
                    self._rrect(x, yy, x + max(10, int(w * frac)), yy + 10,
                                self.GOOD if frac >= 1 else c['ACCENT'], r=5)
                yy += 22
                yy = self._timeline(x, yy, w, steps) + 8
            if p.get('word_goal'):
                n, f = house_recipes.word_count(p)
                goal = p['word_goal']
                yy = self._text(x, yy, f"📝 {n:,} / {goal:,} words" + (f"  ·  {f}" if f else ''),
                                self.FONT_BOLD, c['HIGHLIGHT'] if n >= goal else c['TEXT'], w) + 4
                self._rrect(x, yy, x + w, yy + 8, c['BG_LIGHT'], r=4)
                self._rrect(x, yy, x + max(8, int(w * min(1.0, n / goal))), yy + 8,
                            self.GOOD if n >= goal * 0.9 else c['ACCENT'], r=4)
                yy += 18
            if p.get('about'):
                yy = self._text(x, yy, p['about'], self.FONT, c['TEXT'], w) + 10
            if p.get('rules'):
                yy = self._text(x, yy, "📏 Rules for everyone:", self.FONT_BOLD, c['ACCENT'], w) + 2
                yy = self._text(x, yy, p['rules'], self.FONT, c['TEXT'], w) + 10
            if p.get('decisions'):
                yy = self._text(x, yy, f"📌 Decisions ({len(p['decisions'])}):", self.FONT_BOLD, c['ACCENT'], w) + 2
                for d in p['decisions'][-6:]:
                    yy = self._text(x, yy, f"{d['who']}: {d['text']}", self.FONT_SMALL, c['TEXT'], w) + 2
                yy += 8
            chips = []
            q = p['steps'][ci].get('question') if ci is not None else None
            if q:
                yy = self._text(x, yy + 4, f"❓ {p['steps'][ci]['who']} asks you:", self.FONT_BOLD, self.BAD, w) + 2
                yy = self._text(x, yy, q['text'], self.FONT, c['TEXT'], w) + 8
                chips.append((f"💬 Answer {p['steps'][ci]['who']}", self._answer, 'accent'))
            if p['status'] == 'finished':
                chips.append(('🎉 Recap', self._show_recap, 'accent'))
            if ci is not None and hp.is_user(p['steps'][ci]['who']) and p['steps'][ci]['status'] == 'active':
                chips.append(("💖 It's your turn — mark my step done", self._user_done, 'accent'))
            elif ci is None and hp.next_pending(p) is not None:
                chips.append(('▶ Start the relay', self._start, 'accent'))
            elif ci is not None and p['steps'][ci]['status'] == 'waiting_for_user':
                chips.append((f"▶ Let {p['steps'][ci]['who']} start", self._start, 'accent'))
            if ci is not None and not hp.is_user(p['steps'][ci]['who']):
                if p['steps'][ci]['status'] == 'active':
                    chips.append((f"🔔 Nudge {p['steps'][ci]['who']}", self._nudge, 'plain'))
                chips.append((f"⏭ Move on from {p['steps'][ci]['who']}…", self._move_on, 'plain'))
            t = p.get('target')
            where = next((k for k, v in WORKS_ON.items() if v and t and Path(v).resolve() == Path(t)),
                         Path(t).name if t else 'its own folder')
            if p['kind'] == 'Writing':
                chips.append(('🎯 Word goal' if not p.get('word_goal') else f"🎯 {p['word_goal']:,} words",
                              self._set_word_goal, 'plain'))
            cur_i = hp.current_index(p)
            if cur_i is not None and p['steps'][cur_i]['status'] == 'active' and hp.relay_guest(p['steps'][cur_i]['who']):
                chips.append((f"📋 Copy turn brief for {p['steps'][cur_i]['who']}", self._copy_brief, 'accent'))
            if hp.authors_of(p):
                n_sugg = len(hp.suggestions(p))
                chips.append((f"📝 Suggestions ({n_sugg})" + (" ●" if n_sugg else ""), self._suggestions_window,
                              'accent' if n_sugg else 'plain'))
            chips += [((f"💰 Budget: ${p['budget']:.2f}" if p.get('budget') else '💰 Budget: none'),
                       self._set_budget, 'plain'),
                      ('✏️ Edit name, description & rules', self._edit_project, 'plain'),
                      ('📄 Add files', self._add_files, 'plain'),
                      ((f"✍️ {'Authors' if len(hp.authors_of(p)) > 1 else 'Author'}: {hp.authors_label(p)}  ▾"
                        if hp.authors_of(p) else "✍️ Author: none  ▾"),
                       self._author_menu, 'plain'),
                      ('📎 Add picture', self._add_picture, 'plain'),
                      ('📋 Paste screenshot', self._paste_picture, 'plain'),
                      ('📂 Open folder', self._open_folder, 'plain'),
                      (f"🛠 Works on: {where}  ▾", self._works_on_menu, 'plain'),
                      (f"Open relay: {'on' if p.get('open_relay', True) else 'off'}", self._toggle_open_relay, 'plain'),
                      ((f"🔁 {hp.repeat_words(p['repeat'])}  ▾" if p.get('repeat') else '🔁 Repeat: off  ▾'),
                       self._repeat_menu, 'plain')]
            yy = self._chip_row(x, yy, w, chips) + 6
            return self._text(x, yy, "Open relay = whoever finishes last can pick who's next, if you haven't "
                                     "planned it.", self.FONT_SMALL, c['TEXT_DIM'], w)
        y = self._card(y, header)

        # Relay steps
        y = self._section(y, 'Relay')
        if not p['steps']:
            y = self._text(18, y, "No steps yet — add one below (who + their role).",
                           self.FONT, c['TEXT_DIM'], self._board_w - 36) + 16
        for i, s in enumerate(p['steps']):
            def step_card(x, yy, w, i=i, s=s):
                kind = {'done': 'good', 'declined': 'bad', 'active': 'accent'}.get(s['status'], 'dim')
                name = self.canvas.create_text(x, yy, text=f"{i + 1}.  {s['who']}", anchor='nw',
                                               font=self.FONT_TITLE, fill=c['HIGHLIGHT'])
                bb = self.canvas.bbox(name)
                self._chip(bb[2] + 12, yy + 3, STEP_WORD.get(s['status'], s['status']), None, kind,
                           static=True)
                yy = bb[3] + 4
                yy = self._text(x, yy, s['role'] or '(no role given)', self.FONT, c['TEXT'], w) + 4
                if s.get('handoff'):
                    yy = self._text(x, yy + 2, f"Handoff: {s['handoff']}", self.FONT, c['TEXT_DIM'], w) + 4
                if s.get('keep'):
                    yy = self._text(x, yy + 2, f"🛡 Please keep: {s['keep']}", self.FONT, c['ACCENT'], w) + 4
                if s.get('touched'):
                    yy = self._text(x, yy + 2, f"Changed: {', '.join(s['touched'])}", self.FONT_SMALL,
                                    c['TEXT_DIM'], w) + 4
                if s.get('left_drafts'):
                    yy = self._text(x, yy + 2, f"⚠ Didn't finish — kept their drafts: {', '.join(s['left_drafts'])}",
                                    self.FONT_SMALL, self.BAD, w) + 4
                if s.get('rolled_back'):
                    yy = self._text(x, yy + 2, f"↩ Unfinished drafts rolled back: {', '.join(s['rolled_back'])}",
                                    self.FONT_SMALL, c['TEXT_DIM'], w) + 4
                if s.get('peer_q'):
                    yy = self._text(x, yy + 2, f"❓ Asked {s['peer_q']['to']}: {s['peer_q']['text']}  (waiting)",
                                    self.FONT_SMALL, c['ACCENT'], w) + 4
                for qa in s.get('qa', []):
                    yy = self._text(x, yy + 2, f"💬 Asked {qa.get('from') or 'you'}: {qa['q']} → {qa['a']}",
                                    self.FONT_SMALL, c['TEXT_DIM'], w) + 4
                if s.get('decline_reason'):
                    yy = self._text(x, yy + 2, f"Said no: {s['decline_reason']}", self.FONT, self.BAD, w) + 4
                if s.get('proposed'):
                    where = ('sent to you — see 🛠 Code changes' if s.get('proposal_id')
                             else 'staged, goes to you when they finish')
                    yy = self._text(x, yy + 2, f"🛠 Proposed changes: {', '.join(s['proposed'])}  ({where})",
                                    self.FONT_SMALL, c['ACCENT'], w) + 4
                tok = workbench.tokens_summary(s)
                cost = house_costs.step_cost(s, live=s['status'] == 'active')
                if tok or cost is not None:
                    line = (f"💰 {house_costs.money(cost)}" + ("  (so far)" if s['status'] == 'active' else '')
                            if cost is not None else '')
                    if tok:
                        line = (line + "  ·  " if line else '') + tok
                    yy = self._text(x, yy + 2, line, self.FONT_SMALL, c['TEXT_DIM'], w) + 4
                if s.get('added_by') and not hp.is_user(s['added_by']):
                    yy = self._text(x, yy + 2, f"(added by {s['added_by']})", self.FONT_SMALL, c['TEXT_DIM'], w) + 4
                cx = x
                if s['status'] == 'pending':
                    if i > 0 and p['steps'][i - 1]['status'] == 'pending':
                        cx, _ = self._chip(cx, yy + 4, '↑', lambda i=i: self._move(i, -1))
                    if i + 1 < len(p['steps']):
                        cx, _ = self._chip(cx, yy + 4, '↓', lambda i=i: self._move(i, 1))
                    flag = '⏸ check with me first: ' + ('on' if s['check_first'] else 'off')
                    cx, _ = self._chip(cx, yy + 4, flag, lambda i=i: self._toggle_check(i))
                if s['status'] in ('pending', 'declined', 'waiting_for_user'):
                    cx, b = self._chip(cx, yy + 4, '✕ remove', lambda i=i: self._remove(i))
                    yy = b
                return yy
            y = self._card(y, step_card)

        # Files
        files = hp.list_files(p)
        y = self._section(y, 'Files')
        if not files:
            y = self._text(18, y, "No files yet.", self.FONT, c['TEXT_DIM'], self._board_w - 36) + 16
        log = p.get('file_log', [])
        for f in files:
            def file_card(x, yy, w, f=f):
                last = next((e for e in reversed(log) if e['file'] == f), None)
                icon = '🖼' if Path(f).suffix.lower() in hp.PICTURE_EXT else '📄'
                bottom = self._text(x, yy, f"{icon} {f}", self.FONT_BOLD, c['TEXT'], w)
                sub = f"last {last['action']} by {last['who']}, {nice_time(last['at'])}" if last else ''
                versions = len(hp.file_history(p, f))
                if versions:
                    sub += f"  ·  {versions} earlier version{'s' if versions != 1 else ''}"
                if sub:
                    bottom = self._text(x, bottom + 2, sub, self.FONT_SMALL, c['TEXT_DIM'], w)
                if Path(f).suffix.lower() in hp.PICTURE_EXT:
                    full = hp.PROJECTS_ROOT / p['slug'] / f
                    cx, _ = self._chip(x, bottom + 6, '🖼 View', lambda full=full: os.startfile(full), 'accent')
                else:
                    cx, _ = self._chip(x, bottom + 6, '📖 Read', lambda f=f: self._show_in_reader(f), 'accent')
                _, b = self._chip(cx, bottom + 6, 'Versions', lambda f=f: self._view_file(f))
                return b
            y = self._card(y, file_card)

        # Checklist (contest kit etc.)
        if p.get('checklist') is not None:
            y = self._section(y, 'Checklist')

            def checklist(x, yy, w):
                items = p.get('checklist', [])
                done = sum(1 for it in items if it['done'])
                yy = self._text(x, yy, f"{done} of {len(items)} done", self.FONT_SMALL, c['TEXT_DIM'], w) + 6
                for i, it in enumerate(items):
                    mark = '☑' if it['done'] else '☐'
                    _, yy = self._chip(x, yy, f"{mark}  {it['text']}", lambda i=i: self._toggle_check_item(i),
                                       'good' if it['done'] else 'plain')
                    yy += 4
                return self._chip(x, yy + 4, '＋ add an item', self._add_check_item, 'dim')[1]
            y = self._card(y, checklist)

        # Sorting plans (only for projects that work on a folder)
        plans = p.get('plans', [])
        if p.get('target') and (plans or p.get('looks_count')):
            y = self._section(y, 'Sorting plans')
            looked = house_sort.look_count(p)
            if looked:
                y = self._text(18, y, f"👀 {looked} picture{'s' if looked != 1 else ''} looked at so far "
                                      f"(about ${looked * 0.0015:.2f} of vision)", self.FONT_SMALL,
                               c['TEXT_DIM'], self._board_w - 36) + 8
            for pl in reversed(plans):
                y = self._card(y, lambda x, yy, w, pl=pl: self._plan_card(x, yy, w, pl))

        # Comments (bubbles)
        y = self._section(y, 'Comments')
        if not p.get('comments'):
            y = self._text(18, y, "No comments yet. Anyone in the house can leave one.",
                           self.FONT, c['TEXT_DIM'], self._board_w - 36) + 16
        bw = int(self._board_w * 0.68)
        for cm in p.get('comments', []):
            mine = hp.is_user(cm['who'])
            x0 = self._board_w - 14 - bw if mine else 14
            def bubble(x, yy, w, cm=cm, mine=mine):
                where = f"  ·  on {cm['file']}" if cm.get('file') else ''
                yy = self._text(x, yy, f"{cm['who']}  ·  {nice_time(cm['at'])}{where}", self.FONT_SMALL,
                                c['BG'] if mine else c['TEXT_DIM'], w) + 2
                if cm.get('quote'):
                    yy = self._text(x + 8, yy, f"“{cm['quote']}”", (FONT_FAMILY, self.settings['font_size'] - 1, 'italic'),
                                    c['BG'] if mine else c['ACCENT'], w - 8) + 2
                return self._text(x, yy, cm['text'], self.FONT, c['BG'] if mine else c['TEXT'], w)
            y = self._card(y, bubble, x0, x0 + bw, fill=c['ACCENT'] if mine else c['CHIP_BG'])

        cv.configure(scrollregion=(0, 0, self._board_w, y + 20))

    def _plan_card(self, x, yy, w, pl):
        c = self.c
        word = {'draft': ('still being drawn up', 'dim'), 'waiting': ('waiting for you', 'accent'),
                'applied': ('applied ✓', 'good'), 'undone': ('undone ↶', 'dim'),
                'rejected': ('set aside', 'bad')}.get(pl['status'], (pl['status'], 'dim'))
        name = self.canvas.create_text(x, yy, text=f"🗂 {pl['by']}'s plan", anchor='nw',
                                       font=self.FONT_TITLE, fill=c['HIGHLIGHT'])
        bb = self.canvas.bbox(name)
        self._chip(bb[2] + 12, yy + 3, word[0], None, word[1], static=True)
        yy = bb[3] + 4
        moves = pl.get('applied') if pl['status'] in ('applied', 'undone') else pl['moves']
        yy = self._text(x, yy, f"{len(pl['moves'])} move{'s' if len(pl['moves']) != 1 else ''}"
                               f"  ·  {nice_time(pl.get('submitted_at') or pl['at'])}",
                        self.FONT_SMALL, c['TEXT_DIM'], w) + 4
        if pl.get('note'):
            yy = self._text(x, yy, pl['note'], self.FONT, c['TEXT'], w) + 6
        expanded = pl['id'] in self._open_plans
        shown = (moves or []) if expanded else (moves or [])[:10]
        if shown:
            # group by destination folder so it reads like "these go in Pets/"
            groups = {}
            for m in shown:
                groups.setdefault(str(Path(m['to']).parent).replace('\\', '/'), []).append(m)
            for folder, ms in groups.items():
                yy = self._text(x, yy, f"📁 {folder if folder != '.' else '(top level)'}", self.FONT_BOLD,
                                c['ACCENT'], w) + 2
                for m in ms:
                    rename = Path(m['from']).name != Path(m['to']).name
                    line = f"     {m['from']}" + (f"  →  {Path(m['to']).name}" if rename else '')
                    yy = self._text(x, yy, line, self.FONT_SMALL, c['TEXT'], w) + 1
        if len(moves or []) > 10:
            yy = self._chip(x, yy + 4, 'Show fewer' if expanded else f"Show all {len(moves)}",
                            lambda pid=pl['id']: self._toggle_plan(pid))[1] + 2
        if pl.get('skipped'):
            yy = self._text(x, yy + 4, f"Skipped {len(pl['skipped'])}: " + '; '.join(pl['skipped'][:5]),
                            self.FONT_SMALL, self.BAD, w) + 2
        chips = []
        if pl['status'] == 'waiting':
            chips = [('✓ Apply this plan', lambda pid=pl['id']: self._apply_plan(pid), 'accent'),
                     ('Not this time', lambda pid=pl['id']: self._reject_plan(pid), 'plain')]
        elif pl['status'] == 'applied':
            chips = [('↶ Undo', lambda pid=pl['id']: self._undo_plan(pid), 'plain')]
        if chips:
            yy = self._chip_row(x, yy + 8, w, chips)
        return yy

    def _toggle_plan(self, pid):
        self._open_plans ^= {pid}
        self._draw_project()

    def _apply_plan(self, pid):
        p = self._need_project()
        pl = next((x for x in (p or {}).get('plans', []) if x['id'] == pid), None)
        if not pl:
            return
        if messagebox.askyesno('Apply sorting plan?',
                               f"Move/rename {len(pl['moves'])} item(s) in\n{p['target']}?\n\n"
                               f"Nothing gets deleted, and you can Undo afterwards."):
            self._reload(house_sort.apply_plan(p, pid))

    def _undo_plan(self, pid):
        p = self._need_project()
        if p and messagebox.askyesno('Undo?', 'Put everything from this plan back where it was?'):
            self._reload(house_sort.undo_plan(p, pid))

    def _reject_plan(self, pid):
        p = self._need_project()
        if not p:
            return
        note = simpledialog.askstring('Not this time', 'Want to tell them why? (optional)', parent=self.root)
        if note is not None:
            self._reload(house_sort.reject_plan(p, pid, note))

    # ── project actions ──────────────────────────────────────────────

    def _reload(self, msg=None):
        if msg:
            self.status_var.set(msg)
        self.refresh_projects(select=self.cur_proj['slug'] if self.cur_proj else None, keep_scroll=True)

    def _new_project(self):
        c = self.c
        win = tk.Toplevel(self.root)
        win.title('New house project')
        win.configure(bg=c['BG'])
        win.geometry('640x600')
        win.transient(self.root)
        win.grab_set()
        tk.Label(win, text='New house project', bg=c['BG'], fg=c['ACCENT'],
                 font=self.FONT_TITLE).pack(anchor='w', padx=16, pady=(16, 8))
        f = tk.Frame(win, bg=c['BG'])
        f.pack(fill=tk.X, padx=16)
        f.columnconfigure(1, weight=1)
        tk.Label(f, text='Name:', bg=c['BG'], fg=c['TEXT'], font=self.FONT).grid(row=0, column=0, sticky='w', pady=4)
        name = tk.Entry(f, bg=c['ENTRY_BG'], fg=c['TEXT'], insertbackground=c['ACCENT'], font=self.FONT,
                        relief=tk.FLAT, highlightthickness=1, highlightcolor=c['ACCENT'],
                        highlightbackground=c['BG_LIGHT'])
        name.grid(row=0, column=1, sticky='ew', padx=(10, 0), pady=4, ipady=3)
        tk.Label(f, text='Kind:', bg=c['BG'], fg=c['TEXT'], font=self.FONT).grid(row=1, column=0, sticky='w', pady=4)
        kind = tk.StringVar(value=KINDS[0])
        self._option_menu(f, kind, KINDS).grid(row=1, column=1, sticky='w', padx=(10, 0), pady=4)
        tk.Label(f, text='Works on:', bg=c['BG'], fg=c['TEXT'], font=self.FONT).grid(row=2, column=0, sticky='w', pady=4)
        target = {'path': ''}
        where_var = tk.StringVar(value='Its own folder')
        where_row = tk.Frame(f, bg=c['BG'])
        where_row.grid(row=2, column=1, sticky='w', padx=(10, 0), pady=4)
        where_note = tk.Label(f, text='Its own drafts folder (stories, plans, new code).', bg=c['BG'],
                              fg=c['TEXT_DIM'], font=self.FONT_SMALL, anchor='w', justify='left', wraplength=520)
        where_note.grid(row=3, column=1, sticky='w', padx=(10, 0))

        prev = {'label': 'Its own folder'}

        def pick(label):
            path = self._ask_target(win, label)
            if path is None:                       # cancelled / refused: keep what we had
                where_var.set(prev['label'])
                return
            shown = Path(path).name if label == CHOOSE else label
            target['path'], prev['label'] = path, shown
            where_var.set(shown)
            where_note.config(text=self._target_note(path))
        presets = [k for k, v in WORKS_ON.items() if not v or Path(v).exists()] + [CHOOSE]
        self._option_menu(where_row, where_var, presets, pick).pack(side=tk.LEFT)
        open_relay = tk.BooleanVar(value=True)
        tk.Checkbutton(f, text="Open relay (the last person can pick who's next if I haven't planned it)",
                       variable=open_relay, bg=c['BG'], fg=c['TEXT'], selectcolor=c['ENTRY_BG'],
                       activebackground=c['BG'], font=self.FONT_SMALL, borderwidth=0,
                       highlightthickness=0).grid(row=4, column=0, columnspan=2, sticky='w', pady=4)
        tk.Label(win, text='What is it about?', bg=c['BG'], fg=c['TEXT'], font=self.FONT).pack(anchor='w', padx=16, pady=(8, 2))
        about = tk.Text(win, height=7, wrap=tk.WORD, bg=c['ENTRY_BG'], fg=c['TEXT'],
                        insertbackground=c['ACCENT'], font=self.FONT, relief=tk.FLAT, padx=8, pady=6)
        about.pack(fill=tk.BOTH, expand=True, padx=16)

        def create():
            if not name.get().strip():
                messagebox.showwarning('Name?', 'Give the project a name first!', parent=win)
                return
            proj = hp.create(name.get(), about.get('1.0', 'end').strip(), kind.get(), open_relay.get())
            where = ''
            if target['path']:
                hp.set_target(proj, target['path'])
                where = f" It works on {target['path']}."
            win.destroy()
            self.refresh_projects(select=proj['slug'])
            self.status_var.set(f"Created \"{proj['name']}\".{where} Now add some steps! ✨")

        btns = tk.Frame(win, bg=c['BG'])
        btns.pack(fill=tk.X, padx=16, pady=12)
        self._button(btns, 'Create', create, 'accent').pack(side=tk.RIGHT)
        self._button(btns, 'Cancel', win.destroy).pack(side=tk.RIGHT, padx=8)
        name.focus_set()

    # ── recipes ──────────────────────────────────────────────────────

    def _new_menu(self):
        c = self.c
        m = tk.Menu(self.root, tearoff=0, bg=c['ENTRY_BG'], fg=c['TEXT'], font=self.FONT,
                    activebackground=c['BUTTON_HOVER'], activeforeground=c['TEXT'])
        m.add_command(label='📄  Blank project…', command=self._new_project)
        m.add_separator()
        for r in house_recipes.RECIPES:
            m.add_command(label=f"{r['name']}…", command=lambda r=r: self._new_from_recipe(r))
        m.tk_popup(self.root.winfo_pointerx(), self.root.winfo_pointery())

    def _new_from_recipe(self, recipe):
        c = self.c
        win = tk.Toplevel(self.root)
        win.title(recipe['name'])
        win.configure(bg=c['BG'])
        win.geometry('760x720')
        win.transient(self.root)
        win.grab_set()
        tk.Label(win, text=recipe['name'], bg=c['BG'], fg=c['ACCENT'], font=self.FONT_TITLE).pack(anchor='w', padx=16, pady=(16, 6))
        top = tk.Frame(win, bg=c['BG'])
        top.pack(fill=tk.X, padx=16)
        top.columnconfigure(1, weight=1)
        tk.Label(top, text='Name:', bg=c['BG'], fg=c['TEXT'], font=self.FONT).grid(row=0, column=0, sticky='w', pady=4)
        name = tk.Entry(top, bg=c['ENTRY_BG'], fg=c['TEXT'], insertbackground=c['ACCENT'], font=self.FONT,
                        relief=tk.FLAT, highlightthickness=1, highlightcolor=c['ACCENT'], highlightbackground=c['BG_LIGHT'])
        name.grid(row=0, column=1, sticky='ew', padx=(10, 0), pady=4, ipady=3)
        target = {'path': ''}
        if recipe.get('needs_folder'):
            tk.Label(top, text='Works on:', bg=c['BG'], fg=c['TEXT'], font=self.FONT).grid(row=1, column=0, sticky='w', pady=4)
            where = tk.Label(top, text='(pick a folder)', bg=c['BG'], fg=c['TEXT_DIM'], font=self.FONT_SMALL, anchor='w')
            where.grid(row=1, column=1, sticky='ew', padx=(10, 0))

            def choose():
                path = self._ask_target(win, CHOOSE)
                if path:
                    target['path'] = path
                    where.config(text=path, fg=c['TEXT'])
            self._button(top, 'Choose a folder…', choose).grid(row=2, column=1, sticky='w', padx=(10, 0), pady=(0, 6))
        tk.Label(win, text='About:', bg=c['BG'], fg=c['TEXT'], font=self.FONT).pack(anchor='w', padx=16, pady=(6, 2))
        about = tk.Text(win, height=3, wrap=tk.WORD, bg=c['ENTRY_BG'], fg=c['TEXT'], insertbackground=c['ACCENT'],
                        font=self.FONT, relief=tk.FLAT, padx=8, pady=6)
        about.pack(fill=tk.X, padx=16)
        about.insert('1.0', recipe.get('about', ''))
        tk.Label(win, text='The relay — pick who does each part (you can edit the roles):', bg=c['BG'],
                 fg=c['ACCENT'], font=self.FONT_BOLD).pack(anchor='w', padx=16, pady=(12, 4))
        box = tk.Frame(win, bg=c['BG'])
        box.pack(fill=tk.BOTH, expand=True, padx=16)
        rows = []
        for i, (who, role) in enumerate(house_recipes.suggested_steps(recipe, self.housemates)):
            fr = tk.Frame(box, bg=c['CARD_BG'], padx=8, pady=6)
            fr.pack(fill=tk.X, pady=3)
            var = tk.StringVar(value=who if who in self.everyone else (self.everyone[0] if self.everyone else hp.USER))
            tk.Label(fr, text=f"{i + 1}.", bg=c['CARD_BG'], fg=c['TEXT_DIM'], font=self.FONT_BOLD).pack(side=tk.LEFT)
            self._option_menu(fr, var, self.everyone + ['(skip)']).pack(side=tk.LEFT, padx=6)
            role_e = tk.Entry(fr, bg=c['ENTRY_BG'], fg=c['TEXT'], insertbackground=c['ACCENT'], font=self.FONT_SMALL,
                              relief=tk.FLAT)
            role_e.insert(0, role)
            role_e.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=3)
            rows.append((var, role_e))
        extra = []
        if recipe.get('word_goal'):
            extra.append(f"word goal {recipe['word_goal']:,}")
        if recipe.get('checklist'):
            extra.append(f"a {len(recipe['checklist'])}-item checklist")
        if extra:
            tk.Label(win, text='Comes with ' + ' and '.join(extra) + ' (you can change them later).', bg=c['BG'],
                     fg=c['TEXT_DIM'], font=self.FONT_SMALL).pack(anchor='w', padx=16, pady=(6, 0))

        def create():
            if not name.get().strip():
                messagebox.showwarning('Name?', 'Give the project a name first!', parent=win)
                return
            if recipe.get('needs_folder') and not target['path']:
                messagebox.showwarning('Which folder?', 'Pick the folder this one works on.', parent=win)
                return
            steps = [(v.get(), e.get().strip()) for v, e in rows if v.get() != '(skip)']
            proj = house_recipes.create_from_recipe(recipe, name.get(), about.get('1.0', 'end').strip(), steps,
                                                    target['path'])
            win.destroy()
            self.refresh_projects(select=proj['slug'])
            self.status_var.set(f"Created \"{proj['name']}\" with {len(steps)} steps — press ▶ Start when you're ready ✨")

        btns = tk.Frame(win, bg=c['BG'])
        btns.pack(fill=tk.X, padx=16, pady=12)
        self._button(btns, 'Create', create, 'accent').pack(side=tk.RIGHT)
        self._button(btns, 'Cancel', win.destroy).pack(side=tk.RIGHT, padx=8)
        name.focus_set()

    def _repeat_menu(self):
        p = self._need_project()
        if not p:
            return
        c = self.c
        m = tk.Menu(self.root, tearoff=0, bg=c['ENTRY_BG'], fg=c['TEXT'], font=self.FONT,
                    activebackground=c['BUTTON_HOVER'], activeforeground=c['TEXT'])
        today = datetime.now()

        def choose(every):
            q = hp.load(p['slug'])
            if not every:
                self._reload(hp.set_repeat(q, None))
                return
            hour = simpledialog.askinteger('What time?', 'What hour should it start? (0–23, e.g. 9 = 9 AM, 20 = 8 PM)',
                                           initialvalue=9, minvalue=0, maxvalue=23, parent=self.root)
            if hour is not None:
                self._reload(hp.set_repeat(q, every, hour))
        m.add_command(label='   Off', command=lambda: choose(None))
        m.add_command(label='   Every day', command=lambda: choose('day'))
        m.add_command(label=f"   Every week ({hp.WEEKDAYS[today.weekday()]}s)", command=lambda: choose('week'))
        m.add_command(label=f"   Every month (on the {min(today.day, 28)})", command=lambda: choose('month'))
        if p.get('repeat'):
            m.add_separator()
            m.add_command(label=f"   Next: {p['repeat']['next'].replace('T', ' at ')}", state='disabled')
        m.tk_popup(self.root.winfo_pointerx(), self.root.winfo_pointery())

    def _toggle_check_item(self, i):
        p = self._need_project()
        if p:
            house_recipes.toggle_check(p, i)
            self._reload()

    def _add_check_item(self):
        p = self._need_project()
        if p:
            text = simpledialog.askstring('Checklist', 'New checklist item:', parent=self.root)
            if text:
                house_recipes.add_check(p, text)
                self._reload()

    def _ask_text(self, title, prompt, initial=''):
        """A small multi-line text dialog. Returns the text, or None if cancelled."""
        c = self.c
        win = tk.Toplevel(self.root)
        win.title(title)
        win.configure(bg=c['BG'])
        win.transient(self.root)
        win.geometry('640x560')
        win.minsize(480, 380)
        out = {'text': None}

        def ok():
            out['text'] = box.get('1.0', 'end').strip()
            win.destroy()

        # Buttons are packed FIRST (at the bottom) and a long prompt scrolls in its own box, so a long
        # question (~900 chars) can't push the typing box and Send off-screen.
        btns = tk.Frame(win, bg=c['BG'])
        btns.pack(side=tk.BOTTOM, fill=tk.X, padx=16, pady=12)
        self._button(btns, 'Send', ok, 'accent').pack(side=tk.RIGHT)
        self._button(btns, 'Cancel', win.destroy).pack(side=tk.RIGHT, padx=8)
        long_prompt = len(prompt) > 300
        if long_prompt:
            q = scrolledtext.ScrolledText(win, height=9, wrap=tk.WORD, bg=c['BG'], fg=c['TEXT'], font=self.FONT,
                                          relief=tk.FLAT, padx=4)
            q.insert('1.0', prompt)
            q.configure(state='disabled')
            q.pack(fill=tk.X, padx=16, pady=(14, 6))
        else:
            tk.Label(win, text=prompt, bg=c['BG'], fg=c['TEXT'], font=self.FONT, wraplength=600,
                     justify='left').pack(anchor='w', padx=16, pady=(14, 6))
        tk.Label(win, text='Your answer:' if long_prompt else '', bg=c['BG'], fg=c['TEXT_DIM'],
                 font=self.FONT_SMALL).pack(anchor='w', padx=16)
        box = scrolledtext.ScrolledText(win, height=8, wrap=tk.WORD, bg=c['ENTRY_BG'], fg=c['TEXT'],
                                        insertbackground=c['TEXT'], font=self.FONT, relief=tk.FLAT)
        box.insert('1.0', initial)
        box.pack(fill=tk.BOTH, expand=True, padx=16)
        box.focus_set()
        win.grab_set()
        self.root.wait_window(win)
        return out['text']

    def _show_recap(self):
        """Open the recap in the Reader (writing it first for projects that finished before recaps existed)."""
        p = self._need_project()
        if not p:
            return
        from core import house_recap
        if house_recap.RECAP not in hp.list_files(p):
            house_recap.write(p)
        if self.reader_changes:
            self._toggle_changes()
        self.refresh_projects(select=p['slug'], keep_scroll=True)
        self._load_reader_files(self.cur_proj)
        self._show_in_reader(house_recap.RECAP)

    def _answer(self):
        p = self._need_project()
        ci = hp.current_index(p) if p else None
        q = p['steps'][ci].get('question') if ci is not None else None
        if not q:
            return
        who = p['steps'][ci]['who']
        text = self._ask_text(f"Answer {who}", f"{who} asked:\n\n{q['text']}\n\nYour answer (it goes straight to "
                                                f"{who}, and they pick their turn back up):")
        if text:
            self._reload(hp.answer_user(hp.load(p['slug']), text))

    def _set_budget(self):
        p = self._need_project()
        if not p:
            return
        spent = house_costs.project_cost(p, live=True)
        n = simpledialog.askfloat(
            'Budget', f"This project has cost {house_costs.money(spent)} so far.\n\n"
                      "Pause the relay for you when it reaches how many dollars?\n"
                      "(The turn in progress always gets to finish. 0 = no budget.)",
            initialvalue=p.get('budget') or 5.0, minvalue=0, parent=self.root)
        if n is not None:
            self._reload(hp.set_budget(p, n))

    def _set_word_goal(self):
        p = self._need_project()
        if not p:
            return
        n = simpledialog.askinteger('Word goal', 'How many words are you aiming for? (0 = none)',
                                    initialvalue=p.get('word_goal') or 1500, minvalue=0, parent=self.root)
        if n is not None:
            if n:
                p['word_goal'] = n
            else:
                p.pop('word_goal', None)
            hp.save(p)
            self._reload()

    def _export_pdf(self):
        p, rel = self.cur_proj, self.reader_file
        if not p or not rel:
            self.status_var.set('Open a file in the Reader first.')
            return
        if Path(rel).suffix.lower() not in house_recipes.TEXT_EXT:
            self.status_var.set('PDF export works on writing files (.md / .txt).')
            return
        byline = simpledialog.askstring('Export PDF', f'Byline under the title? (optional — e.g. "by {hp.USER} & the house")',
                                        parent=self.root)
        if byline is None:
            return
        colophon = ''
        if rel != 'RECAP.md' and any(s['status'] == 'done' and not hp.is_user(s['who']) for s in p.get('steps', [])):
            from core import house_recap
            colophon = house_recap.colophon(p)         # credits everyone who worked on it, at the end
        out, msg = house_recipes.export_pdf(p, rel, byline, colophon=colophon)
        self.status_var.set(msg)
        if out:
            os.startfile(str(out))

    # ── "Works on" ───────────────────────────────────────────────────

    def _ask_target(self, parent, label):
        """Path for a Works-on choice ('' = its own folder), or None if cancelled/refused."""
        if label != CHOOSE:
            return WORKS_ON.get(label, '')
        path = filedialog.askdirectory(parent=parent, title='Which folder should this project work on?',
                                       initialdir=str(Path.home()))
        if not path:
            return None
        why = hp.target_problem(path)
        if why:
            messagebox.showwarning("Can't use that folder", why, parent=parent)
            return None
        return str(Path(path).resolve())

    def _target_note(self, path):
        if not path:
            return 'Its own drafts folder (stories, plans, new code).'
        note = (f"{path}\nOn their turn they can look around and PROPOSE changes — nothing changes "
                f"until you approve it in Code changes.")
        if 'lighthouse-memory' in path.lower().replace('\\', '/'):
            note += "\n(They can read it, but changes can't land inside the Lighthouse folder — it's protected.)"
        return note

    def _works_on_menu(self):
        p = self._need_project()
        if not p:
            return
        m = tk.Menu(self.root, tearoff=0, bg=self.c['ENTRY_BG'], fg=self.c['TEXT'], font=self.FONT,
                    activebackground=self.c['BUTTON_HOVER'], activeforeground=self.c['TEXT'])

        def choose(label):
            path = self._ask_target(self.root, label)
            if path is None:
                return
            self._reload(hp.set_target(hp.load(p['slug']), path))
        for label, path in WORKS_ON.items():
            if not path or Path(path).exists():
                m.add_command(label=('✓ ' if (p.get('target') or '') == (str(Path(path).resolve()) if path else '')
                                     else '   ') + label, command=lambda l=label: choose(l))
        m.add_command(label='   ' + CHOOSE, command=lambda: choose(CHOOSE))
        if p.get('target'):
            m.add_separator()
            m.add_command(label='   📂 Open that folder', command=lambda: os.startfile(p['target']))
        m.tk_popup(self.root.winfo_pointerx(), self.root.winfo_pointery())

    def _copy_brief(self):
        """Copy the current guest's full turn brief, for pasting into their chat (a friend in another chat app)."""
        p = self._need_project()
        if not p:
            return
        i = hp.current_index(p)
        if i is None:
            self.status_var.set('Nobody is taking a turn right now.')
            return
        text = hp.turn_brief(p, i)
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        who = p['steps'][i]['who']
        self.status_var.set(f"📋 Copied {who}'s turn brief ({len(text.split()):,} words). Paste it to {who}; "
                            f"when they reply, ⏭ Move on and paste their work in.")

    def _author_menu(self):
        """Author's approval: tick who the author(s) are — their edits go straight in; everyone else's
        become suggestions the author(s), or you, accept or decline. Tick more than one for co-authors."""
        p = self._need_project()
        if not p:
            return
        m = tk.Menu(self.root, tearoff=0, bg=self.c['ENTRY_BG'], fg=self.c['TEXT'], font=self.FONT,
                    activebackground=self.c['BUTTON_HOVER'], activeforeground=self.c['TEXT'])
        cur = [a.lower() for a in hp.authors_of(p)]
        m.add_command(label=('✓ ' if not cur else '   ') + "Nobody (everyone's edits go straight in)",
                      command=lambda: self._reload(hp.set_authors(hp.load(p['slug']), [])))
        m.add_separator()
        m.add_command(label="— tick each co-author —", state='disabled')
        for name in self.housemates:
            m.add_command(label=('✓ ' if name.lower() in cur else '   ') + name,
                          command=lambda n=name: self._reload(hp.toggle_author(hp.load(p['slug']), n)))
        m.tk_popup(self.root.winfo_pointerx(), self.root.winfo_pointery())

    def _suggestions_window(self):
        """Review suggestions yourself: no AI involved, so it costs nothing."""
        p = self._need_project()
        if not p:
            return
        c = self.c
        win = tk.Toplevel(self.root)
        win.title(f"Suggestions: {p['name']}")
        win.configure(bg=c['BG'])
        win.geometry('900x620')
        win.transient(self.root)
        tk.Label(win, text=f"Suggestions for {hp.authors_label(p) or 'the author'} to approve", bg=c['BG'],
                 fg=c['ACCENT'], font=self.FONT_TITLE).pack(anchor='w', padx=16, pady=(14, 2))
        tk.Label(win, text=f"You can decide on their behalf. Accepted ones go into the file "
                           f"(the old version is kept); declined ones send the suggester your reason.",
                 bg=c['BG'], fg=c['TEXT_DIM'], font=self.FONT_SMALL, wraplength=860, justify='left').pack(anchor='w', padx=16)
        body = tk.Frame(win, bg=c['BG'])
        body.pack(fill=tk.BOTH, expand=True, padx=16, pady=10)
        lb = tk.Listbox(body, width=34, bg=c['ENTRY_BG'], fg=c['TEXT'], font=self.FONT, relief=tk.FLAT,
                        selectbackground=c['ACCENT'], activestyle='none', exportselection=False)
        lb.pack(side=tk.LEFT, fill=tk.Y)
        view = scrolledtext.ScrolledText(body, wrap=tk.WORD, bg=c['ENTRY_BG'], fg=c['TEXT'], font=('Consolas', 10),
                                         relief=tk.FLAT, padx=8, pady=6)
        view.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(10, 0))
        view.tag_configure('add', foreground='#2e8b57')
        view.tag_configure('del', foreground='#c0392b')
        view.tag_configure('head', foreground=c['TEXT_DIM'])
        state = {'items': []}

        def load_list():
            state['items'] = hp.suggestions(hp.load(p['slug']))
            lb.delete(0, tk.END)
            for s in state['items']:
                lb.insert(tk.END, f"{s['who']} → {s['file']}  ({s['at'][5:16].replace('T', ' ')})")
            view.delete('1.0', tk.END)
            if not state['items']:
                view.insert(tk.END, "Nothing waiting. 🎉", 'head')
            else:
                lb.selection_set(0)
                show()

        def current():
            sel = lb.curselection()
            return state['items'][sel[0]] if sel and sel[0] < len(state['items']) else None

        def show(_=None):
            s = current()
            view.delete('1.0', tk.END)
            if not s:
                return
            proj = hp.load(p['slug'])
            if hp.is_stale(proj, s):
                view.insert(tk.END, "⚠ The file changed after this was written. Accepting it would undo the newer "
                                    "changes, so it'll ask you to confirm.\n\n", 'del')
            view.insert(tk.END, f"{s['who']}'s changes to {s['file']}  (green = added, red = removed)\n\n", 'head')
            for line in hp.suggestion_diff(proj, s, max_lines=2000).splitlines():
                view.insert(tk.END, line + "\n", 'add' if line.startswith('+') else 'del' if line.startswith('-')
                            else 'head' if line.startswith('@@') else None)

        def accept():
            s = current()
            if not s:
                return
            proj = hp.load(p['slug'])
            force = False
            if hp.is_stale(proj, s):
                if not messagebox.askyesno('Accept anyway?', f"{s['file']} changed after {s['who']} wrote this. "
                                           f"Accepting it would undo those newer changes (they're kept as a "
                                           f"version). Accept anyway?", parent=win):
                    return
                force = True
            self.status_var.set(hp.accept_suggestion(proj, s['id'], hp.USER, force=force))
            load_list()
            self._reload()

        def decline():
            s = current()
            if not s:
                return
            reason = simpledialog.askstring('Why not?', f"A short reason for {s['who']}:", parent=win)
            if not reason or not reason.strip():
                return
            self.status_var.set(hp.decline_suggestion(hp.load(p['slug']), s['id'], hp.USER, reason))
            load_list()
            self._reload()

        lb.bind('<<ListboxSelect>>', show)
        btns = tk.Frame(win, bg=c['BG'])
        btns.pack(fill=tk.X, padx=16, pady=(0, 14))
        self._button(btns, '✅ Accept', accept, 'accent').pack(side=tk.LEFT)
        self._button(btns, '❌ Decline…', decline).pack(side=tk.LEFT, padx=8)
        self._button(btns, 'Close', win.destroy).pack(side=tk.RIGHT)
        load_list()

    def _need_project(self):
        if not self.cur_proj:
            self.status_var.set('Pick (or create) a project first.')
            return None
        return hp.load(self.cur_proj['slug'])

    def _add_step(self):
        p = self._need_project()
        if not p:
            return
        role = self.role_text.get('1.0', 'end').strip()
        if role == getattr(self.role_text, '_hint', None):
            role = ''
        who = self.who_var.get()
        if not who or who == '(nobody)':
            return
        hp.add_step(p, who, role, self.check_first_var.get())
        self.role_text.delete('1.0', tk.END)
        self.check_first_var.set(False)
        self.role_text.event_generate('<FocusOut>')
        self._reload(f"Added {who} to the relay.")

    def _start(self):
        p = self._need_project()
        if p:
            self._reload(hp.start(p))

    def _user_done(self):
        p = self._need_project()
        if not p:
            return
        note = simpledialog.askstring('Your step', 'Anything to hand off to the next person? (optional)',
                                      parent=self.root)
        if note is None:
            return
        self._reload(hp.step_done(p, hp.USER, note))

    def _edit_project(self):
        p = self._need_project()
        if not p:
            return
        c = self.c
        win = tk.Toplevel(self.root)
        win.title('Edit project')
        win.configure(bg=c['BG'])
        win.transient(self.root)
        win.geometry('620x560')
        tk.Label(win, text='Name', bg=c['BG'], fg=c['ACCENT'], font=self.FONT_BOLD).pack(anchor='w', padx=16,
                                                                                       pady=(14, 2))
        name_var = tk.StringVar(value=p['name'])
        ent = tk.Entry(win, textvariable=name_var, bg=c['ENTRY_BG'], fg=c['TEXT'], insertbackground=c['TEXT'],
                       font=self.FONT, relief=tk.FLAT)
        ent.pack(fill=tk.X, padx=16)
        tk.Label(win, text='Description (everyone sees this on their turn)',
                 bg=c['BG'], fg=c['ACCENT'], font=self.FONT_BOLD).pack(anchor='w', padx=16, pady=(12, 2))
        box = scrolledtext.ScrolledText(win, height=8, wrap=tk.WORD, bg=c['ENTRY_BG'], fg=c['TEXT'],
                                        insertbackground=c['TEXT'], font=self.FONT, relief=tk.FLAT)
        box.insert('1.0', p.get('about', ''))
        box.pack(fill=tk.BOTH, expand=True, padx=16)
        tk.Label(win, text='Rules for everyone (optional) — e.g. "under 2,000 words", "for a general audience"',
                 bg=c['BG'], fg=c['ACCENT'], font=self.FONT_BOLD).pack(anchor='w', padx=16, pady=(12, 2))
        rules = scrolledtext.ScrolledText(win, height=4, wrap=tk.WORD, bg=c['ENTRY_BG'], fg=c['TEXT'],
                                          insertbackground=c['TEXT'], font=self.FONT, relief=tk.FLAT)
        rules.insert('1.0', p.get('rules', ''))
        rules.pack(fill=tk.X, padx=16)

        def save():
            msg = hp.rename(hp.load(p['slug']), name_var.get(), box.get('1.0', 'end'))
            msg2 = hp.set_rules(hp.load(p['slug']), rules.get('1.0', 'end'))
            win.destroy()
            self._reload(msg if msg2 == "Nothing changed." else
                         (msg2 if msg == "Nothing changed." else f"{msg} {msg2}"))

        btns = tk.Frame(win, bg=c['BG'])
        btns.pack(fill=tk.X, padx=16, pady=12)
        self._button(btns, 'Save', save, 'accent').pack(side=tk.RIGHT)
        self._button(btns, 'Cancel', win.destroy).pack(side=tk.RIGHT, padx=8)
        ent.focus_set()
        ent.select_range(0, tk.END)

    def _add_files(self):
        """Start a project from drafts that already exist (e.g. an essay in a companion's notes folder)."""
        p = self._need_project()
        if not p:
            return
        start = PULSE_ROOT / "personas"
        paths = filedialog.askopenfilenames(
            parent=self.root, title='Add existing drafts or files to this project',
            initialdir=str(start) if start.exists() else None,
            filetypes=[('Drafts and text', '*.md *.txt *.py *.json *.html *.css *.js *.csv'), ('All files', '*.*')])
        if not paths:
            return
        msgs = [hp.add_text_file(hp.load(p['slug']), f) for f in paths]
        self._reload(msgs[0] if len(msgs) == 1 else f"📄 Added {len(msgs)} files. " + " ".join(
            m for m in msgs if not m.startswith('📄')))

    def _add_picture(self):
        p = self._need_project()
        if not p:
            return
        paths = filedialog.askopenfilenames(
            parent=self.root, title='Attach pictures to this project',
            filetypes=[('Pictures', '*.png *.jpg *.jpeg *.gif *.webp *.bmp *.heic'), ('All files', '*.*')])
        if not paths:
            return
        msgs = [hp.add_picture(hp.load(p['slug']), f) for f in paths]
        self._reload(msgs[0] if len(msgs) == 1 else f"📎 Attached {len(msgs)} pictures.")

    def _paste_picture(self):
        """A screenshot you just took (Win+Shift+S, or Print Screen) goes straight in."""
        p = self._need_project()
        if not p:
            return
        try:
            from PIL import ImageGrab
            img = ImageGrab.grabclipboard()
        except Exception:
            img = None
        if isinstance(img, list):                  # files copied in Explorer
            msgs = [hp.add_picture(hp.load(p['slug']), f) for f in img]
            self._reload(msgs[0] if msgs else 'Nothing to paste.')
            return
        if img is None:
            self.status_var.set('No picture on the clipboard — take a screenshot (Win+Shift+S) or copy one first.')
            return
        import tempfile
        tmp = Path(tempfile.gettempdir()) / f"screenshot {datetime.now():%Y-%m-%d %H%M%S}.png"
        img.save(tmp)
        try:
            self._reload(hp.add_picture(hp.load(p['slug']), tmp))
        finally:
            tmp.unlink(missing_ok=True)

    def _nudge(self):
        p = self._need_project()
        if p:
            self._reload(hp.nudge(p))

    def _move_on(self):
        """Un-stick the relay: optionally paste in work someone sent another way, then hand off."""
        p = self._need_project()
        ci = hp.current_index(p) if p else None
        if ci is None:
            return
        who = p['steps'][ci]['who']
        c = self.c
        files = hp.list_files(p)
        win = tk.Toplevel(self.root)
        win.title(f"Move on from {who}")
        win.configure(bg=c['BG'])
        win.transient(self.root)
        win.geometry('640x700')
        pad = dict(padx=16, anchor='w')
        tk.Label(win, text=f"Move on from {who}'s turn", bg=c['BG'], fg=c['ACCENT'],
                 font=self.FONT_BOLD).pack(pady=(14, 4), **pad)
        tk.Label(win, text=f"If {who} sent you their part some other way (like in a chat message), paste it "
                           "below and it goes into the project for them. Leave it empty to just skip their turn.",
                 bg=c['BG'], fg=c['TEXT_DIM'], font=self.FONT_SMALL, wraplength=580, justify='left').pack(**pad)
        box = scrolledtext.ScrolledText(win, height=12, wrap=tk.WORD, bg=c['ENTRY_BG'], fg=c['TEXT'],
                                        insertbackground=c['TEXT'], font=self.FONT, relief=tk.FLAT)
        box.pack(fill=tk.BOTH, expand=True, padx=16, pady=8)
        row = tk.Frame(win, bg=c['BG'])
        row.pack(fill=tk.X, padx=16)
        tk.Label(row, text='Save into file:', bg=c['BG'], fg=c['TEXT'], font=self.FONT).pack(side=tk.LEFT)
        file_var = tk.StringVar(value=files[0] if len(files) == 1 else f"{hp.slugify(who)}.md")
        tk.Entry(row, textvariable=file_var, bg=c['ENTRY_BG'], fg=c['TEXT'], insertbackground=c['TEXT'],
                 font=self.FONT, relief=tk.FLAT, width=28).pack(side=tk.LEFT, padx=6)
        append_var = tk.BooleanVar(value=True)
        tk.Checkbutton(win, text="Add to the end of the file (instead of replacing what's there)",
                       variable=append_var, bg=c['BG'], fg=c['TEXT'], selectcolor=c['ENTRY_BG'],
                       activebackground=c['BG'], activeforeground=c['TEXT'], font=self.FONT,
                       borderwidth=0, highlightthickness=0).pack(pady=(6, 0), **pad)
        ch = hp.turn_changes(p, ci)
        drafts = ch['changed'] + ch['created']
        drafts_var = tk.StringVar(value='keep')
        if drafts:          # they saved something but never finished
            tk.Label(win, text=f"{who} saved unfinished drafts ({', '.join(drafts)}). If you don't paste anything, "
                               f"what should happen to them?", bg=c['BG'], fg=c['TEXT'], font=self.FONT_SMALL,
                     wraplength=580, justify='left').pack(pady=(8, 0), **pad)
            for val, label in (('keep', 'Keep them (the next person is told they\'re unfinished)'),
                               ('rollback', 'Roll back to how the files were before their turn (drafts stay in Versions)')):
                tk.Radiobutton(win, text=label, value=val, variable=drafts_var, bg=c['BG'], fg=c['TEXT'],
                               selectcolor=c['ENTRY_BG'], activebackground=c['BG'], activeforeground=c['TEXT'],
                               font=self.FONT_SMALL, borderwidth=0, highlightthickness=0).pack(**pad)
        row2 = tk.Frame(win, bg=c['BG'])
        row2.pack(fill=tk.X, padx=16, pady=8)
        tk.Label(row2, text='Note for whoever is next:', bg=c['BG'], fg=c['TEXT'], font=self.FONT).pack(side=tk.LEFT)
        note_var = tk.StringVar()
        tk.Entry(row2, textvariable=note_var, bg=c['ENTRY_BG'], fg=c['TEXT'], insertbackground=c['TEXT'],
                 font=self.FONT, relief=tk.FLAT).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=6)

        def go():
            fresh = hp.load(p['slug'])
            msg = hp.move_on(fresh, box.get('1.0', 'end'), file_var.get(), append_var.get(), note_var.get(),
                             drafts=drafts_var.get())
            win.destroy()
            self._reload(msg)

        btns = tk.Frame(win, bg=c['BG'])
        btns.pack(fill=tk.X, padx=16, pady=(0, 14))
        self._button(btns, 'Move on', go, 'accent').pack(side=tk.RIGHT)
        self._button(btns, 'Cancel', win.destroy).pack(side=tk.RIGHT, padx=8)
        box.focus_set()

    def _move(self, i, d):
        p = self._need_project()
        if p:
            hp.move_step(p, i, d)
            self._reload()

    def _remove(self, i):
        p = self._need_project()
        if p and messagebox.askyesno('Remove step?', f"Remove {p['steps'][i]['who']}'s step?"):
            hp.remove_step(p, i)
            self._reload('Step removed.')

    def _toggle_check(self, i):
        p = self._need_project()
        if p:
            p['steps'][i]['check_first'] = not p['steps'][i]['check_first']
            hp.save(p)
            self._reload()

    def _toggle_open_relay(self):
        p = self._need_project()
        if p:
            p['open_relay'] = not p.get('open_relay', True)
            hp.save(p)
            self._reload()

    def _open_folder(self):
        p = self._need_project()
        if p:
            os.startfile(str(hp.PROJECTS_ROOT / p['slug']))

    def _send_comment(self):
        p = self._need_project()
        text = self.comment_entry.get().strip()
        if p and text:
            hp.comment(p, hp.USER, text)
            self.comment_entry.delete(0, tk.END)
            self._reload('Comment posted. 💬')
            self.canvas.yview_moveto(1.0)

    def _view_file(self, rel):
        p = self._need_project()
        if not p:
            return
        c = self.c
        win = tk.Toplevel(self.root)
        win.title(f"{rel} — {p['name']}")
        win.configure(bg=c['BG'])
        win.geometry('980x700')
        side = tk.Frame(win, bg=c['BG'])
        side.pack(side=tk.RIGHT, fill=tk.Y, padx=(0, 12), pady=12)
        tk.Label(side, text='Versions', bg=c['BG'], fg=c['ACCENT'], font=self.FONT_BOLD).pack(anchor='w')
        lb = tk.Listbox(side, bg=c['ENTRY_BG'], fg=c['TEXT'], font=self.FONT_SMALL, relief=tk.FLAT,
                        selectbackground=c['BUTTON_HOVER'], selectforeground=c['TEXT'], width=34,
                        highlightthickness=0)
        lb.pack(fill=tk.Y, expand=True, pady=6)
        history = hp.file_history(p, rel)
        lb.insert(tk.END, 'Current version')
        for v in reversed(history):
            lb.insert(tk.END, f"{nice_time(v['at'])} (before {v['saved_before']})")
        txt = scrolledtext.ScrolledText(win, wrap=tk.WORD, bg=c['ENTRY_BG'], fg=c['TEXT'], font=self.FONT,
                                        relief=tk.FLAT, padx=14, pady=12, insertwidth=0)
        txt.pack(fill=tk.BOTH, expand=True, padx=12, pady=12)
        _lor.LoRGui._make_read_only(txt)

        def show(_e=None):
            sel = lb.curselection()
            i = sel[0] if sel else 0
            content = hp.read_file(p, rel)[0] if i == 0 else \
                Path(list(reversed(history))[i - 1]['path']).read_text(encoding='utf-8')
            txt.delete('1.0', tk.END)
            txt.insert('1.0', content or '')

        def restore():
            sel = lb.curselection()
            if not sel or sel[0] == 0:
                return
            v = list(reversed(history))[sel[0] - 1]
            if messagebox.askyesno('Restore?', "Make this older version the current one?\n\n"
                                               "(The current version is kept in history too.)", parent=win):
                self.status_var.set(hp.restore_version(hp.load(p['slug']), rel, v['path']))
                win.destroy()
                self._reload()

        lb.bind('<<ListboxSelect>>', show)
        self._button(side, 'Restore this version', restore).pack(fill=tk.X)
        lb.selection_set(0)
        show()


def main():
    root = tk.Tk()
    WorkbenchGui(root)
    root.mainloop()


if __name__ == '__main__':
    main()
