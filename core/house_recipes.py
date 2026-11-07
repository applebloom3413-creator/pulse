"""
House Projects — recipes, word goals, checklists and PDF export.

Recipes are ready-made relays: the user picks one, chooses who does each role
(suggestions are pre-filled from the household's own companions, see
suggested_steps()), and the steps appear already written.
"""

import html
import re
from datetime import datetime
from pathlib import Path

from core import house_projects as hp

TEXT_EXT = {".md", ".markdown", ".txt", ".text"}

# step = (slot, role text). Slots "A", "B", "C"... are companions: suggested_steps() fills
# them from the household's roster in order (the same letter = the same person), and
# USER_SLOT is a step for the user themselves.
USER_SLOT = "you"
RECIPES = [
    {"name": "✍️ Write a story together", "kind": "Writing", "about": "A story we write together.",
     "steps": [("A", "Outline: the main beats, the feeling, the ending. Save it as outline.md."),
               ("B", "First draft from the outline (story.md). Don't polish — just get it down."),
               ("C", "Second pass on story.md: dialogue, pacing, cut what drags."),
               ("D", "Polish: smooth sentences, check consistency, suggest 3 titles in the handoff."),
               (USER_SLOT, "Final read 💖")]},
    {"name": "🏆 Contest essay", "kind": "Writing", "word_goal": 2500,
     "about": "An essay for a contest or class. Check the rules for the word limit and theme.",
     "checklist": ["The question / theme is answered clearly", "Thesis is in the first paragraph",
                   "Within the word limit", "Sources or examples are named", "Has a title",
                   "Proofread once more", "Exported as a PDF"],
     "steps": [("A", "Brainstorm: 3 possible angles and a one-line thesis for each (ideas.md). "
                     "Look things up if it helps."),
               (USER_SLOT, "Pick the angle 💖"),
               ("B", "Outline the essay (outline.md): sections, key points, examples."),
               ("C", "Full draft (essay.md) following the outline."),
               ("D", "Review: leave line comments on essay.md (comment with path + quote) — "
                     "what's unclear, what's weak, what's great."),
               ("C", "Revise essay.md using the line comments."),
               (USER_SLOT, "Final read and submit 💖")]},
    {"name": "🗂 Sort a folder", "kind": "Sorting", "needs_folder": True,
     "about": "Tidy a folder so things are easy to find.",
     "steps": [("A", "Look through the folder (look at pictures if there are any) and make a sorting "
                     "plan: sensible folders, clear names. Explain your folders in the plan note."),
               (USER_SLOT, "Check the plan and press Apply 💖")]},
    {"name": "🐛 Fix a bug", "kind": "Code", "needs_folder": True,
     "about": "What's wrong (what you expected vs what happens):",
     "steps": [("A", "Find the cause: read the code (files / peek), explain what's wrong and where in "
                     "your handoff. Don't change anything yet."),
               ("B", "Propose the fix (change) based on the handoff. Keep it small."),
               ("A", "Review the proposed fix in the handoff/comments — does it really fix it?"),
               (USER_SLOT, "Approve the change in Code changes 💖")]},
    {"name": "🎉 Plan a party or trip", "kind": "Planning", "about": "What, when, who, and roughly how much:",
     "steps": [("A", "Ideas: 10 fun ideas for it (ideas.md). Look things up for real places/prices."),
               ("B", "Budget + schedule from the best ideas (plan.md)."),
               ("C", "Make a checklist of everything to buy/book/do (checklist.md)."),
               (USER_SLOT, "Look it over 💖")]},
    {"name": "📖 Review my writing", "kind": "Writing", "about": "Put your writing in the project folder first.",
     "steps": [("A", "Read it and leave line comments (comment with path + quote): what works, "
                     "what's confusing."),
               ("B", "A second read with line comments — different eyes, different notes."),
               ("C", "Write a short summary of all the notes, kindest first (notes.md)."),
               (USER_SLOT, "Read the notes 💖")]},
]


def suggested_steps(recipe: dict, people: list[str] | None = None) -> list[tuple[str, str]]:
    """The recipe's steps as [(who, role)], with companion slots filled from `people` (default: every
    companion in personas/, see workbench.companion_names) in order, wrapping around if there are
    fewer people than slots. The user's own steps get the user's name. With nobody to suggest,
    a slot's `who` is "" (the dialog lets the user pick)."""
    from core import workbench
    people = list(people) if people is not None else workbench.companion_names()
    slots: dict[str, str] = {}
    out = []
    for slot, role in recipe["steps"]:
        if slot == USER_SLOT:
            who = hp.USER
        else:
            if slot not in slots:
                slots[slot] = people[len(slots) % len(people)] if people else ""
            who = slots[slot]
        out.append((who, role))
    return out


def create_from_recipe(recipe: dict, name: str, about: str, steps: list, target: str = "") -> dict:
    """steps: [(who, role), ...] as the user finalized them in the dialog."""
    proj = hp.create(name, about, recipe["kind"])
    if target:
        hp.set_target(proj, target)
        proj = hp.load(proj["slug"])
    if recipe.get("word_goal"):
        proj["word_goal"] = recipe["word_goal"]
    if recipe.get("checklist"):
        proj["checklist"] = [{"text": t, "done": False} for t in recipe["checklist"]]
    proj["recipe"] = recipe["name"]
    hp.save(proj)
    for who, role in steps:
        if who:
            hp.add_step(proj, who, role)
    return hp.load(proj["slug"])


# ── word goals ──────────────────────────────────────────────────────────

def text_files(proj: dict) -> list[str]:
    """The project's own writing (the auto-written RECAP.md isn't part of the piece)."""
    return [f for f in hp.list_files(proj) if Path(f).suffix.lower() in TEXT_EXT and f != "RECAP.md"]


def word_count(proj: dict) -> tuple[int, str | None]:
    """Words in the main piece: the biggest text file (drafts usually are)."""
    best, best_n = None, 0
    for f in text_files(proj):
        text, err = hp.read_file(proj, f)
        if not err:
            n = hp.text_stats(text)["words"]
            if n > best_n:
                best, best_n = f, n
    return best_n, best


# ── checklist ───────────────────────────────────────────────────────────

def toggle_check(proj: dict, i: int):
    items = proj.get("checklist", [])
    if 0 <= i < len(items):
        items[i]["done"] = not items[i]["done"]
        hp.save(proj)


def add_check(proj: dict, text: str):
    if text.strip():
        proj.setdefault("checklist", []).append({"text": text.strip(), "done": False})
        hp.save(proj)


# ── PDF export ──────────────────────────────────────────────────────────

CSS = """
@page { size: letter; margin: 2.2cm 2.4cm; }
body { font-family: Times-Roman; font-size: 12pt; line-height: 1.5; color: #222; }
h1 { font-size: 20pt; margin: 0 0 10pt 0; }
h2 { font-size: 15pt; margin: 14pt 0 6pt 0; }
h3 { font-size: 13pt; margin: 12pt 0 4pt 0; }
p { margin: 0 0 9pt 0; text-align: justify; }
blockquote { margin: 6pt 18pt; font-style: italic; color: #555; }
code, pre { font-family: Courier; font-size: 10pt; }
.byline { color: #777; font-size: 10pt; margin-bottom: 18pt; }
"""


def export_pdf(proj: dict, rel: str, byline: str = "", colophon: str = "") -> tuple[Path | None, str]:
    """Turn a Markdown/text file into a tidy PDF in <project>/exports/. A colophon (who made it)
    goes at the end, the way a book credits its makers."""
    text, err = hp.read_file(proj, rel)
    if err:
        return None, err
    if colophon:
        text = text.rstrip() + "\n\n---\n\n" + colophon
    import markdown
    from xhtml2pdf import pisa
    body = markdown.markdown(text, extensions=["extra", "sane_lists"])
    by = f'<div class="byline">{html.escape(byline)}</div>' if byline else ""
    doc = f"<html><head><meta charset='utf-8'><style>{CSS}</style></head><body>{by}{body}</body></html>"
    out_dir = hp.PROJECTS_ROOT / proj["slug"] / "exports"
    out_dir.mkdir(exist_ok=True)
    stem = re.sub(r"[^A-Za-z0-9_-]+", "-", Path(rel).stem).strip("-") or "export"
    out = out_dir / f"{stem}-{datetime.now():%Y%m%d-%H%M}.pdf"
    with out.open("wb") as f:
        result = pisa.CreatePDF(doc, dest=f, encoding="utf-8")
    if result.err:
        return None, "The PDF maker hit a snag with this file."
    return out, f"Saved {out.name} in the project's exports folder."
