"""
Household Window — glanceable dashboard of the Pulse household.

Runs a local HTTP server that reads existing on-disk state (lanterns,
focus, letters, pets, LoR posts, obsidian shared corner, shared library
margin notes) and renders them into a single-page dashboard.

Design principles:
  - Journals are excluded on purpose. They're companion-private.
  - Everything else is shown only if the companion opted in (share=true
    on library notes; presence.share for lanterns).
  - Zero API cost. Pure presence layer over data that already exists.
  - Multi-theme + multi-font + size + tabs, all via CSS vars + localStorage.

The roster is built from the persona folders (personas/*/config.yaml, folders
starting with "_" and personas with `retired: true` are skipped). Each card
shows the persona's LoR author_name (or persona name), provider.model and
provider.type. An optional `household_role: "..."` key in a persona's
config.yaml adds a short line under the name.

Run:
  py -3.14 scripts/household_window.py

Then open:  http://localhost:5175
"""

from __future__ import annotations
import http.server
import html
import json
import os
import socketserver
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

PULSE_ROOT = Path(__file__).resolve().parent.parent
PORT = 5175



def _load_yaml(path: Path) -> dict:
    try:
        import yaml
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def _merged_config(folder: str) -> dict:
    """Root config.yaml with the persona overlay on top (shallow per section)."""
    merged = dict(_load_yaml(PULSE_ROOT / "config.yaml"))
    for k, v in _load_yaml(PULSE_ROOT / "personas" / folder / "config.yaml").items():
        if isinstance(v, dict) and isinstance(merged.get(k), dict):
            merged[k] = {**merged[k], **v}
        else:
            merged[k] = v
    return merged


def _persona_display_name(folder: str, cfg: dict) -> str:
    name = ((cfg.get("channels") or {}).get("lor") or {}).get("author_name")
    if name and str(name).strip().lower() != "companion":
        return str(name)
    for ident in ("persona.yaml", "persona.json"):
        ip = PULSE_ROOT / "personas" / folder / ident
        if ip.exists():
            n = _load_yaml(ip).get("name")
            if n:
                return str(n)
    return folder.title()


def load_roster() -> list[tuple[str, str, str, str, str]]:
    """(folder, display name, model, provider, role) for every active persona."""
    roster = []
    pdir = PULSE_ROOT / "personas"
    if not pdir.is_dir():
        return roster
    for d in sorted(pdir.iterdir()):
        if not d.is_dir() or d.name.startswith("_") or not (d / "config.yaml").exists():
            continue
        cfg = _merged_config(d.name)
        if cfg.get("retired", False):
            continue
        provider = cfg.get("provider") or {}
        roster.append((
            d.name,
            _persona_display_name(d.name, cfg),
            str(provider.get("model") or ""),
            str(provider.get("type") or ""),
            str(_load_yaml(d / "config.yaml").get("household_role") or ""),
        ))
    return roster


def _user_names() -> set[str]:
    """Lower-cased names letters to the user may be addressed to
    (user_name / user_nickname from config.yaml and the root persona file)."""
    names = {"user", "the user"}
    for fname in ("config.yaml", "persona.yaml", "persona.json"):
        data = _load_yaml(PULSE_ROOT / fname)
        for key in ("user_name", "user_nickname"):
            v = data.get(key)
            if v:
                names.add(str(v).strip().lower())
    return names


THEME_OPTIONS = [
    # Original palette
    ("purple",       "Household Purple"),
    ("amber",        "Warm Amber"),
    ("forest",       "Forest"),
    ("steel",        "Steel Blue"),
    ("paper",        "Cream Paper"),
    ("highcontrast", "High Contrast"),
    # Pastel additions
    ("sakura",       "Sakura Pink"),
    ("mint",         "Mint"),
    ("lavender",     "Soft Lavender"),
    ("peach",        "Peach"),
    ("candy",        "Cotton Candy"),
    # Colorful/vibrant additions
    ("ocean",        "Ocean"),
    ("berry",        "Berry Pop"),
    ("sunset",       "Sunset"),
]

FONT_OPTIONS = [
    ("system",       "System (default)"),
    ("serif",        "Serif (Georgia)"),
    ("rounded",      "Rounded"),
    ("atkinson",     "Atkinson Hyperlegible"),
    ("handwritten",  "Handwritten"),
    ("mono",         "Monospace"),
]

SIZE_OPTIONS = [
    ("s",  "Small"),
    ("m",  "Medium"),
    ("l",  "Large"),
    ("xl", "X-Large"),
]

TAB_OPTIONS = [
    ("all",       "All"),
    ("household", "Household"),
    ("letters",   "Letters"),
    ("lor",       "Local Reddit"),
    ("margins",   "Library Margins"),
    ("obsidian",  "Obsidian Corner"),
]

# ─────────────────────────────────────────────────────────────
# Data readers (best-effort, silent on failure)
# ─────────────────────────────────────────────────────────────


def _rel_time(iso_ts: str) -> str:
    if not iso_ts:
        return ""
    try:
        then = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
        if then.tzinfo is None:
            then = then.replace(tzinfo=timezone.utc)
        secs = (datetime.now(timezone.utc) - then).total_seconds()
    except Exception:
        return ""
    if secs < 90:
        return "just now"
    if secs < 3600:
        return f"{int(secs / 60)}m ago"
    if secs < 86400:
        return f"{int(secs / 3600)}h ago"
    days = int(secs / 86400)
    return "yesterday" if days == 1 else f"{days}d ago"


def _persona_db(folder: str) -> Path:
    return PULSE_ROOT / "personas" / folder / "data" / f"{folder}.db"


def read_lantern(folder: str) -> dict | None:
    db = _persona_db(folder)
    if not db.exists():
        return None
    try:
        c = sqlite3.connect(str(db))
        row = c.execute(
            "SELECT resident_id, mode, mood, focus, note, open_thread, updated_at "
            "FROM resident_lanterns ORDER BY updated_at DESC LIMIT 1"
        ).fetchone()
        c.close()
    except Exception:
        return None
    if not row:
        return None
    return {
        "resident_id": row[0], "mode": row[1] or "", "mood": row[2] or "",
        "focus": row[3] or "", "note": row[4] or "", "open_thread": row[5] or "",
        "updated_at": row[6] or "",
    }


def read_focus(folder: str) -> dict | None:
    p = PULSE_ROOT / "personas" / folder / "data" / "focus.json"
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not data or not data.get("task"):
        return None
    return data


def read_pet(folder: str) -> dict | None:
    db = _persona_db(folder)
    if not db.exists():
        return None
    try:
        c = sqlite3.connect(str(db))
        row = c.execute("SELECT name, species, state, variant FROM pets LIMIT 1").fetchone()
        c.close()
    except Exception:
        return None
    if not row:
        return None
    name, species, state_json, variant = row
    try:
        state = json.loads(state_json or "{}")
    except Exception:
        state = {}
    return {"name": name or "", "species": species or "", "variant": variant or "", "state": state}


def read_recent_letters_to_user(limit: int = 8) -> list[dict]:
    letters_dir = PULSE_ROOT / "data" / "letters"
    if not letters_dir.exists():
        return []
    recipients = _user_names() | {"everyone", "household"}
    out = []
    for f in letters_dir.glob("letter_*.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        to = (data.get("to") or "").strip().lower()
        if to in recipients or any(part.strip() in recipients for part in to.split("/")):
            out.append({
                "id": data.get("id", f.stem), "from": data.get("from", ""),
                "subject": data.get("subject", ""), "sent_at": data.get("sent_at", ""),
            })
    out.sort(key=lambda x: x["sent_at"], reverse=True)
    return out[:limit]


def read_recent_lor(limit: int = 6) -> list[dict]:
    posts_file = PULSE_ROOT / "data" / "lor" / "posts.json"
    authors_file = PULSE_ROOT / "data" / "lor" / "authors.json"
    if not posts_file.exists():
        return []
    try:
        posts = json.loads(posts_file.read_text(encoding="utf-8"))
    except Exception:
        return []
    authors = {}
    if authors_file.exists():
        try:
            authors = json.loads(authors_file.read_text(encoding="utf-8"))
        except Exception:
            pass

    def author_name(aid: str) -> str:
        a = authors.get(aid, {})
        if isinstance(a, dict):
            return a.get("name") or a.get("author_name") or aid
        return str(a) or aid

    if not isinstance(posts, list):
        return []
    posts.sort(key=lambda p: p.get("created_at", ""), reverse=True)
    out = []
    for p in posts[:limit]:
        out.append({
            "author": author_name(p.get("author_id", "")),
            "title": p.get("title") or "",
            "content": (p.get("content") or "").strip(),
            "category": p.get("category", ""),
            "reply_to": p.get("reply_to"),
            "created_at": p.get("created_at", ""),
        })
    return out


def read_shared_margins(limit: int = 8) -> list[dict]:
    margins_file = PULSE_ROOT / "data" / "Library" / "margins.json"
    if not margins_file.exists():
        return []
    try:
        data = json.loads(margins_file.read_text(encoding="utf-8"))
    except Exception:
        return []
    out = []
    for book, notes in data.items():
        if not isinstance(notes, list):
            continue
        book_title = book.replace(".txt", "")
        for n in notes:
            if not n.get("shared"):
                continue
            out.append({
                "author": n.get("author", ""), "book": book_title,
                "percent": n.get("percent"),
                "excerpt": (n.get("note") or "").strip(),
                "at": n.get("at", ""),
            })
    out.sort(key=lambda x: x["at"], reverse=True)
    return out[:limit]


def _obsidian_corners() -> list[Path]:
    """Every shared "<vault>/<write_folder>" corner the obsidian skill writes to,
    from the root config and each persona's config (obsidian.* or
    skills.obsidian.*: vault_path, write_folder — default "Companions")."""
    cfgs = [_load_yaml(PULSE_ROOT / "config.yaml")]
    pdir = PULSE_ROOT / "personas"
    if pdir.is_dir():
        cfgs += [_load_yaml(c) for c in sorted(pdir.glob("*/config.yaml"))
                 if not c.parent.name.startswith("_")]
    corners: list[Path] = []
    for cfg in cfgs:
        top = cfg.get("obsidian") or {}
        nested = (cfg.get("skills") or {}).get("obsidian") or {}
        vault = top.get("vault_path") or nested.get("vault_path")
        if not vault:
            continue
        folder = top.get("write_folder") or nested.get("write_folder") or "Companions"
        vp = Path(vault)
        if not vp.is_absolute():
            vp = PULSE_ROOT / vp
        corner = (vp / folder).resolve()
        if corner.is_dir() and corner not in corners:
            corners.append(corner)
    return corners


def read_obsidian_shared(limit: int = 8) -> list[dict]:
    author_dirs = [d for root in _obsidian_corners() for d in root.iterdir()]
    if not author_dirs:
        return []
    posts = []
    for author_dir in author_dirs:
        if not author_dir.is_dir():
            continue
        for md in author_dir.glob("*.md"):
            try:
                stat = md.stat()
                text = md.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            snippet = text.strip().split("\n", 1)
            title = (snippet[0] if snippet else md.stem).lstrip("# ").strip()
            excerpt = snippet[1].strip() if len(snippet) > 1 else ""
            posts.append({
                "author": author_dir.name, "title": title[:120],
                "excerpt": excerpt, "mtime": stat.st_mtime,
            })
    posts.sort(key=lambda p: p["mtime"], reverse=True)
    for p in posts:
        p["at"] = datetime.fromtimestamp(p["mtime"], tz=timezone.utc).isoformat()
        del p["mtime"]
    return posts[:limit]


# ─────────────────────────────────────────────────────────────
# CSS — themes, fonts, sizes, tabs
# ─────────────────────────────────────────────────────────────

CSS = """
:root {
    --font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    --font-size-base: 14px;
    --bg: #1e1a2e;
    --bg-card: #2a2440;
    --border: #3d3552;
    --text: #e8e2f0;
    --text-dim: #a89ec8;
    --text-faint: #7a6e94;
    --accent: #c8a2ff;
    --accent-warm: #ffb99a;
    --link: #a8c8ff;
    --shadow: rgba(0, 0, 0, 0.3);
}

/* Dark / muted themes */
:root[data-theme="amber"] {
    --bg:#2a1f14; --bg-card:#3a2c1e; --border:#4d3a26;
    --text:#f0e6d2; --text-dim:#c8b598; --text-faint:#8a7a5a;
    --accent:#ffcf9e; --accent-warm:#ff8f6b; --link:#ffd8a8;
}
:root[data-theme="forest"] {
    --bg:#1a2420; --bg-card:#253830; --border:#365046;
    --text:#e0ecd8; --text-dim:#a8c0a0; --text-faint:#6a8578;
    --accent:#a0e0b8; --accent-warm:#ffd0a0; --link:#b0d0e8;
}
:root[data-theme="steel"] {
    --bg:#172029; --bg-card:#223040; --border:#354558;
    --text:#dce4ec; --text-dim:#a0b0c4; --text-faint:#6a7a90;
    --accent:#a8c8e8; --accent-warm:#e8b898; --link:#b8d8f0;
}
:root[data-theme="paper"] {
    --bg:#ede4d3; --bg-card:#f5ecdc; --border:#d5c8a8;
    --text:#3a3020; --text-dim:#6a5c40; --text-faint:#a09070;
    --accent:#a06030; --accent-warm:#d08050; --link:#6a4020;
    --shadow: rgba(120,90,50,0.15);
}
:root[data-theme="highcontrast"] {
    --bg:#000000; --bg-card:#0a0a0a; --border:#333333;
    --text:#ffffff; --text-dim:#cccccc; --text-faint:#888888;
    --accent:#ffff80; --accent-warm:#ff9060; --link:#80c0ff;
}

/* Pastel themes */
:root[data-theme="sakura"] {
    --bg:#fdf0f4; --bg-card:#fce4ec; --border:#f4c6d1;
    --text:#4a2c37; --text-dim:#7a5560; --text-faint:#b48090;
    --accent:#d84860; --accent-warm:#ff8090; --link:#b04060;
    --shadow: rgba(200,90,120,0.12);
}
:root[data-theme="mint"] {
    --bg:#eef8f0; --bg-card:#ddefe0; --border:#b8d8c0;
    --text:#234030; --text-dim:#4a6a55; --text-faint:#8caf95;
    --accent:#4a9068; --accent-warm:#d8a860; --link:#388050;
    --shadow: rgba(60,120,80,0.12);
}
:root[data-theme="lavender"] {
    --bg:#f5f0f8; --bg-card:#ebe0f0; --border:#d0bce0;
    --text:#3a2848; --text-dim:#6a5088; --text-faint:#a090b0;
    --accent:#8858b0; --accent-warm:#d09858; --link:#7040a0;
    --shadow: rgba(120,80,160,0.12);
}
:root[data-theme="peach"] {
    --bg:#fdefe4; --bg-card:#fadfc8; --border:#f0c090;
    --text:#4a2810; --text-dim:#805030; --text-faint:#b08858;
    --accent:#d05828; --accent-warm:#e88848; --link:#b04010;
    --shadow: rgba(200,120,60,0.14);
}
:root[data-theme="candy"] {
    --bg:#f0e8f8; --bg-card:#e8dff0; --border:#c8b8e0;
    --text:#382848; --text-dim:#6858a0; --text-faint:#a898c0;
    --accent:#a848c8; --accent-warm:#6090d0; --link:#7040b8;
    --shadow: rgba(160,80,200,0.14);
}

/* Vibrant themes */
:root[data-theme="ocean"] {
    --bg:#e0f2f7; --bg-card:#cce8f0; --border:#a0d0dc;
    --text:#103848; --text-dim:#386070; --text-faint:#6890a0;
    --accent:#1080a8; --accent-warm:#e88060; --link:#0060a0;
    --shadow: rgba(20,120,160,0.14);
}
:root[data-theme="berry"] {
    --bg:#f5e0ec; --bg-card:#f0d0e0; --border:#d090b0;
    --text:#40103a; --text-dim:#703860; --text-faint:#a06898;
    --accent:#a0207a; --accent-warm:#ff8060; --link:#802050;
    --shadow: rgba(160,30,120,0.16);
}
:root[data-theme="sunset"] {
    --bg:#ffe8d8; --bg-card:#ffd8c0; --border:#f0b090;
    --text:#4a1010; --text-dim:#803030; --text-faint:#b06060;
    --accent:#d84018; --accent-warm:#ffb060; --link:#a02010;
    --shadow: rgba(220,80,20,0.16);
}

/* Font families */
:root[data-font="serif"]      { --font-family: Georgia, "Times New Roman", serif; }
:root[data-font="rounded"]    { --font-family: Nunito, "Segoe UI", "Trebuchet MS", sans-serif; }
:root[data-font="atkinson"]   { --font-family: "Atkinson Hyperlegible", "Segoe UI", sans-serif; }
:root[data-font="handwritten"]{ --font-family: "Segoe Script", "Comic Sans MS", cursive; }
:root[data-font="mono"]       { --font-family: Consolas, "SF Mono", "Courier New", monospace; }

/* Font sizes */
:root[data-size="s"]  { --font-size-base: 12px; }
:root[data-size="l"]  { --font-size-base: 16px; }
:root[data-size="xl"] { --font-size-base: 18px; }

* { box-sizing: border-box; }

body {
    margin: 0;
    padding: 20px;
    font-family: var(--font-family);
    background: var(--bg);
    color: var(--text);
    font-size: var(--font-size-base);
    line-height: 1.5;
    min-height: 100vh;
    transition: background 0.2s, color 0.2s;
}

header {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    margin-bottom: 20px;
    padding-bottom: 12px;
    border-bottom: 1px solid var(--border);
    flex-wrap: wrap;
    gap: 12px;
}

h1 {
    margin: 0;
    font-size: calc(var(--font-size-base) * 1.6);
    font-weight: 600;
    color: var(--accent);
    letter-spacing: 0.02em;
}

.subtitle {
    color: var(--text-faint);
    font-size: calc(var(--font-size-base) * 0.92);
    margin-top: 2px;
}

.controls {
    display: flex;
    align-items: center;
    gap: 10px;
    flex-wrap: wrap;
}

.controls label {
    font-size: calc(var(--font-size-base) * 0.82);
    color: var(--text-dim);
}

select {
    background: var(--bg-card);
    color: var(--text);
    border: 1px solid var(--border);
    padding: 4px 8px;
    border-radius: 4px;
    font-size: calc(var(--font-size-base) * 0.9);
    font-family: inherit;
    cursor: pointer;
}
select:focus { outline: 1px solid var(--accent); }

/* Tabs */
.tabs {
    display: flex;
    gap: 2px;
    margin-bottom: 20px;
    border-bottom: 1px solid var(--border);
    flex-wrap: wrap;
}
.tab {
    padding: 8px 14px;
    cursor: pointer;
    color: var(--text-dim);
    border-bottom: 2px solid transparent;
    font-size: calc(var(--font-size-base) * 0.95);
    user-select: none;
    transition: color 0.15s, border-color 0.15s;
}
.tab:hover { color: var(--text); }
.tab.active {
    color: var(--accent);
    border-bottom-color: var(--accent);
    font-weight: 600;
}

.tab-content { display: none; }
.tab-content.active { display: block; }

.grid {
    display: grid;
    gap: 16px;
    grid-template-columns: repeat(auto-fill, minmax(340px, 1fr));
    margin-bottom: 24px;
}

.card {
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 14px 16px;
    box-shadow: 0 1px 3px var(--shadow);
}

.card-header {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    margin-bottom: 8px;
}

.card-name {
    font-size: calc(var(--font-size-base) * 1.08);
    font-weight: 600;
    color: var(--accent);
}

.card-model {
    font-size: calc(var(--font-size-base) * 0.78);
    color: var(--text-faint);
    font-family: Consolas, "SF Mono", monospace;
}

.card-role {
    font-size: calc(var(--font-size-base) * 0.8);
    color: var(--accent-warm);
    font-style: italic;
    margin-bottom: 8px;
}

.lantern-row { margin: 6px 0; font-size: calc(var(--font-size-base) * 0.92); }

.lantern-label {
    color: var(--text-faint);
    text-transform: uppercase;
    letter-spacing: 0.06em;
    font-size: calc(var(--font-size-base) * 0.72);
    display: inline-block;
    min-width: 68px;
}

.lantern-val { color: var(--text-dim); }

.focus-badge {
    display: inline-block;
    background: rgba(200, 162, 255, 0.12);
    border: 1px solid var(--accent);
    color: var(--accent);
    font-size: calc(var(--font-size-base) * 0.72);
    padding: 2px 6px;
    border-radius: 3px;
    margin-left: 6px;
    text-transform: uppercase;
    letter-spacing: 0.06em;
}

.pet-line {
    font-size: calc(var(--font-size-base) * 0.88);
    color: var(--text-dim);
    margin-top: 8px;
    padding-top: 8px;
    border-top: 1px dashed var(--border);
}

.pet-line .pet-name { color: var(--accent-warm); font-weight: 500; }

.timestamp {
    font-size: calc(var(--font-size-base) * 0.78);
    color: var(--text-faint);
    margin-top: 8px;
}

.no-data { color: var(--text-faint); font-style: italic; font-size: calc(var(--font-size-base) * 0.88); }

section.wide {
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 16px 20px;
    margin-bottom: 16px;
}

section.wide h2 {
    margin: 0 0 12px 0;
    font-size: calc(var(--font-size-base) * 1.08);
    color: var(--accent);
    font-weight: 600;
    letter-spacing: 0.02em;
}

.entry {
    padding: 10px 0;
    border-bottom: 1px dashed var(--border);
    cursor: pointer;
    transition: background 0.12s;
    border-radius: 4px;
    padding-left: 4px;
    padding-right: 4px;
}
.entry:hover { background: rgba(127, 127, 127, 0.06); }
.entry:last-child { border-bottom: none; }

.entry-body-wrap {
    position: relative;
    max-height: 5.5em;
    overflow: hidden;
    transition: max-height 0.25s;
}
.entry-body-wrap.expanded {
    max-height: 2000px;
}
.entry-body-wrap:not(.expanded)::after {
    content: "";
    position: absolute;
    bottom: 0; left: 0; right: 0;
    height: 1.8em;
    background: linear-gradient(to bottom, transparent, var(--bg-card));
    pointer-events: none;
}
.expand-hint {
    color: var(--text-faint);
    font-size: calc(var(--font-size-base) * 0.78);
    margin-top: 6px;
    font-style: italic;
    user-select: none;
}
.entry-body-wrap.expanded + .expand-hint::before { content: "▲ click to collapse"; }
.entry-body-wrap:not(.expanded) + .expand-hint::before { content: "▼ click to expand"; }

.entry-head {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    margin-bottom: 4px;
    font-size: calc(var(--font-size-base) * 0.92);
}

.entry-author { color: var(--accent); font-weight: 500; }
.entry-meta   { color: var(--text-faint); font-size: calc(var(--font-size-base) * 0.78); }

.entry-body {
    color: var(--text-dim);
    font-size: calc(var(--font-size-base) * 0.95);
    white-space: pre-wrap;
    word-wrap: break-word;
}

.entry-subject {
    color: var(--accent-warm);
    font-size: calc(var(--font-size-base) * 0.95);
    margin: 2px 0;
    font-style: italic;
}

.two-col { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }

@media (max-width: 800px) {
    .two-col { grid-template-columns: 1fr; }
}

footer {
    margin-top: 32px;
    padding-top: 16px;
    border-top: 1px solid var(--border);
    color: var(--text-faint);
    font-size: calc(var(--font-size-base) * 0.78);
    text-align: center;
}
"""


def _e(s) -> str:
    return html.escape(str(s or ""))


def render_persona_card(folder: str, name: str, model: str, provider: str, role: str) -> str:
    lantern = read_lantern(folder)
    focus = read_focus(folder)
    pet = read_pet(folder)

    parts = ['<div class="card">', '<div class="card-header">',
             f'<span class="card-name">{_e(name)}']
    if focus:
        parts.append('<span class="focus-badge">focus</span>')
    parts.append('</span>')
    parts.append(f'<span class="card-model">{_e(model)}</span></div>')

    if role:
        parts.append(f'<div class="card-role">{_e(role)}</div>')

    if lantern:
        for label, key in [("mode", "mode"), ("mood", "mood"), ("focus", "focus"),
                           ("note", "note"), ("thread", "open_thread")]:
            val = lantern.get(key, "").strip()
            if not val:
                continue
            parts.append(
                f'<div class="lantern-row">'
                f'<span class="lantern-label">{label}</span>'
                f'<span class="lantern-val">{_e(val)}</span></div>'
            )
        if lantern.get("updated_at"):
            parts.append(
                f'<div class="timestamp">lantern updated {_e(_rel_time(lantern["updated_at"]))}</div>'
            )
    else:
        parts.append('<div class="no-data">no lantern set</div>')

    if focus:
        task = focus.get("task", "")
        files_n = len(focus.get("files_touched", []))
        steps_n = len(focus.get("next_steps", []))
        parts.append(
            f'<div class="lantern-row" style="margin-top:8px;">'
            f'<span class="lantern-label">working on</span>'
            f'<span class="lantern-val">{_e(task)} '
            f'<span style="color:var(--text-faint);font-size:calc(var(--font-size-base)*0.78);">'
            f'({files_n} file{"s" if files_n!=1 else ""}, {steps_n} step{"s" if steps_n!=1 else ""})</span>'
            f'</span></div>'
        )

    if pet:
        pet_name = pet.get("name") or f"(unnamed {pet.get('species')})"
        state = pet.get("state", {})
        stat_lines = []
        for stat in ("nutrients", "moisture", "water_temp", "warmth", "current"):
            v = state.get(stat)
            if v is None:
                continue
            if stat == "nutrients" and (v < 0.35 or v > 0.80):
                stat_lines.append(f"nutrients {v:.2f}")
            elif stat == "moisture" and (v < 0.45 or v > 0.75):
                stat_lines.append(f"moisture {v:.2f}")
        stat_str = f" — {', '.join(stat_lines)}" if stat_lines else ""
        parts.append(
            f'<div class="pet-line">🐾 <span class="pet-name">{_e(pet_name)}</span> '
            f'the {_e(pet.get("species", ""))}{_e(stat_str)}</div>'
        )

    parts.append('</div>')
    return "".join(parts)


def render_letters_section(letters: list[dict]) -> str:
    if not letters:
        return '<section class="wide"><h2>Letters to you</h2><div class="no-data">no recent letters to you</div></section>'
    parts = ['<section class="wide"><h2>Letters to you</h2>']
    for l in letters:
        parts.append('<div class="entry">')
        parts.append(
            f'<div class="entry-head">'
            f'<span class="entry-author">from {_e(l["from"])}</span>'
            f'<span class="entry-meta">{_e(_rel_time(l["sent_at"]))}</span></div>'
        )
        if l["subject"]:
            parts.append(f'<div class="entry-subject">{_e(l["subject"])}</div>')
        parts.append('</div>')
    parts.append('</section>')
    return "".join(parts)


def render_lor_section(posts: list[dict]) -> str:
    if not posts:
        return '<section class="wide"><h2>Local Reddit</h2><div class="no-data">no recent posts</div></section>'
    parts = ['<section class="wide"><h2>Local Reddit — recent activity</h2>']
    for p in posts:
        parts.append('<div class="entry">')
        parts.append(
            f'<div class="entry-head">'
            f'<span class="entry-author">{_e(p["author"])}</span>'
            f'<span class="entry-meta">{_e(_rel_time(p["created_at"]))}'
            f'{" · reply" if p.get("reply_to") else ""}</span></div>'
        )
        if p["title"]:
            parts.append(f'<div class="entry-subject">{_e(p["title"])}</div>')
        if p["content"]:
            long = len(p["content"]) > 260
            parts.append(f'<div class="entry-body-wrap{"" if long else " expanded"}"><div class="entry-body">{_e(p["content"])}</div></div>')
            if long:
                parts.append('<div class="expand-hint"></div>')
        parts.append('</div>')
    parts.append('</section>')
    return "".join(parts)


def render_margins_section(margins: list[dict]) -> str:
    if not margins:
        return '<section class="wide"><h2>Library margins (shared)</h2><div class="no-data">no shared notes yet</div></section>'
    parts = ['<section class="wide"><h2>Library margins (shared)</h2>']
    for m in margins:
        parts.append('<div class="entry">')
        pct = f" · {m['percent']}%" if m.get("percent") is not None else ""
        parts.append(
            f'<div class="entry-head">'
            f'<span class="entry-author">{_e(m["author"])}</span>'
            f'<span class="entry-meta">{_e(_rel_time(m["at"]))}</span></div>'
        )
        parts.append(f'<div class="entry-subject">on {_e(m["book"])}{_e(pct)}</div>')
        long = len(m["excerpt"]) > 260
        parts.append(f'<div class="entry-body-wrap{"" if long else " expanded"}"><div class="entry-body">{_e(m["excerpt"])}</div></div>')
        if long:
            parts.append('<div class="expand-hint"></div>')
        parts.append('</div>')
    parts.append('</section>')
    return "".join(parts)


def render_obsidian_section(posts: list[dict]) -> str:
    if not posts:
        return '<section class="wide"><h2>Obsidian shared corner</h2><div class="no-data">no recent posts</div></section>'
    parts = ['<section class="wide"><h2>Obsidian shared corner</h2>']
    for p in posts:
        parts.append('<div class="entry">')
        parts.append(
            f'<div class="entry-head">'
            f'<span class="entry-author">{_e(p["author"])}</span>'
            f'<span class="entry-meta">{_e(_rel_time(p["at"]))}</span></div>'
        )
        if p.get("title"):
            parts.append(f'<div class="entry-subject">{_e(p["title"])}</div>')
        if p.get("excerpt"):
            long = len(p["excerpt"]) > 260
            parts.append(f'<div class="entry-body-wrap{"" if long else " expanded"}"><div class="entry-body">{_e(p["excerpt"])}</div></div>')
            if long:
                parts.append('<div class="expand-hint"></div>')
        parts.append('</div>')
    parts.append('</section>')
    return "".join(parts)


def render_page() -> str:
    now_str = datetime.now().strftime("%A, %b %#d · %#I:%M %p") \
        if sys.platform == "win32" else datetime.now().strftime("%A, %b %-d · %-I:%M %p")

    theme_options_html = "".join(
        f'<option value="{v}">{_e(label)}</option>' for v, label in THEME_OPTIONS
    )
    font_options_html = "".join(
        f'<option value="{v}">{_e(label)}</option>' for v, label in FONT_OPTIONS
    )
    size_options_html = "".join(
        f'<option value="{v}">{_e(label)}</option>' for v, label in SIZE_OPTIONS
    )

    persona_cards = "".join(
        render_persona_card(f, name, model, provider, role)
        for f, name, model, provider, role in load_roster()
    )

    letters = read_recent_letters_to_user(limit=8)
    lor = read_recent_lor(limit=8)
    margins = read_shared_margins(limit=8)
    obsidian = read_obsidian_shared(limit=8)

    letters_html = render_letters_section(letters)
    lor_html = render_lor_section(lor)
    margins_html = render_margins_section(margins)
    obsidian_html = render_obsidian_section(obsidian)

    tab_bar = "".join(
        f'<div class="tab" data-tab="{v}" onclick="setTab(\'{v}\')">{_e(label)}</div>'
        for v, label in TAB_OPTIONS
    )

    return f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Household Window</title>
<meta http-equiv="refresh" content="60">
<style>{CSS}</style>
</head>
<body>
<header>
    <div>
        <h1>Household Window</h1>
        <div class="subtitle">{_e(now_str)} · auto-refresh every 60s</div>
    </div>
    <div class="controls">
        <label for="theme">theme</label>
        <select id="theme" onchange="setTheme(this.value)">{theme_options_html}</select>
        <label for="font">font</label>
        <select id="font" onchange="setFont(this.value)">{font_options_html}</select>
        <label for="size">size</label>
        <select id="size" onchange="setSize(this.value)">{size_options_html}</select>
    </div>
</header>

<div class="tabs">{tab_bar}</div>

<div class="tab-content" data-tab-content="all">
    <div class="grid">{persona_cards}</div>
    <div class="two-col">
        <div>{letters_html}{margins_html}</div>
        <div>{lor_html}{obsidian_html}</div>
    </div>
</div>

<div class="tab-content" data-tab-content="household">
    <div class="grid">{persona_cards}</div>
</div>

<div class="tab-content" data-tab-content="letters">{letters_html}</div>
<div class="tab-content" data-tab-content="lor">{lor_html}</div>
<div class="tab-content" data-tab-content="margins">{margins_html}</div>
<div class="tab-content" data-tab-content="obsidian">{obsidian_html}</div>

<footer>
    Journals are private and never shown here.
    Companions opt into visibility via `share=true` flags.
</footer>

<script>
function setTheme(name) {{
    if (name === 'purple') document.documentElement.removeAttribute('data-theme');
    else document.documentElement.setAttribute('data-theme', name);
    try {{ localStorage.setItem('hw-theme', name); }} catch(e) {{}}
}}
function setFont(name) {{
    if (name === 'system') document.documentElement.removeAttribute('data-font');
    else document.documentElement.setAttribute('data-font', name);
    try {{ localStorage.setItem('hw-font', name); }} catch(e) {{}}
}}
function setSize(name) {{
    if (name === 'm') document.documentElement.removeAttribute('data-size');
    else document.documentElement.setAttribute('data-size', name);
    try {{ localStorage.setItem('hw-size', name); }} catch(e) {{}}
}}
function setTab(name) {{
    document.querySelectorAll('.tab').forEach(function(t) {{
        t.classList.toggle('active', t.dataset.tab === name);
    }});
    document.querySelectorAll('.tab-content').forEach(function(c) {{
        c.classList.toggle('active', c.dataset.tabContent === name);
    }});
    try {{ localStorage.setItem('hw-tab', name); }} catch(e) {{}}
}}

(function() {{
    try {{
        var t = localStorage.getItem('hw-theme') || 'purple';
        document.getElementById('theme').value = t; setTheme(t);
        var f = localStorage.getItem('hw-font') || 'system';
        document.getElementById('font').value = f; setFont(f);
        var s = localStorage.getItem('hw-size') || 'm';
        document.getElementById('size').value = s; setSize(s);
        var tab = localStorage.getItem('hw-tab') || 'all';
        setTab(tab);
    }} catch(e) {{
        setTab('all');
    }}
}})();

// Click any entry to expand/collapse long bodies
document.addEventListener('click', function(e) {{
    var entry = e.target.closest('.entry');
    if (!entry) return;
    var wrap = entry.querySelector('.entry-body-wrap');
    if (!wrap) return;
    wrap.classList.toggle('expanded');
}});
</script>

</body>
</html>
"""


# ─────────────────────────────────────────────────────────────
# HTTP server
# ─────────────────────────────────────────────────────────────


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        if self.path not in ("/", "/index.html"):
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"not found")
            return
        try:
            html_body = render_page()
        except Exception as e:
            self.send_response(500)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(f"render error: {e}".encode("utf-8"))
            return
        body = html_body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    os.chdir(str(PULSE_ROOT))
    with socketserver.TCPServer(("127.0.0.1", PORT), Handler) as httpd:
        print(f"Household Window running at http://localhost:{PORT}")
        print("Ctrl-C to stop.")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nShutting down.")


if __name__ == "__main__":
    main()
