"""
Export a House Projects workbench project — the whole record of who said and did what —
into one readable Markdown file you can keep, paste to someone, or convert to PDF/HTML with
any Markdown converter.

It is READ-ONLY: it never changes a project, a file, or a version. It just reads what's
already saved (the relay steps + handoffs, comments, decisions, questions & answers, the
event log, and every file's current text + saved-version history) and lays it out.

  python export_project.py                      list every project (name + slug)
  python export_project.py "Ada's essay"        export that one (by name OR slug)
  python export_project.py ada-s-essay          (slug works too)
  python export_project.py --all                export every project
  python export_project.py <name> --relay-only  just who-said-and-did-what (no file contents) — best for sharing
  python export_project.py <name> --versions    also include the FULL text of every saved version
  python export_project.py <name> --out path/to/file.md   choose where it's written

Exports land in <projects folder>/_exports/ (PULSE_PROJECTS_DIR, default ~/projects) unless you
pass --out. A sibling to scripts/house_project.py.
"""
import argparse
import re
import sys
from datetime import datetime
from pathlib import Path

PULSE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PULSE))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from core import house_projects as hp  # noqa: E402

EXPORTS = hp.PROJECTS_ROOT / "_exports"


def _dt(iso: str) -> str:
    """2025-01-02T09:38:00 -> 2025-01-02 09:38 (robust to anything odd)."""
    if not iso:
        return "?"
    return str(iso).replace("T", " ")[:16]


def _fence(text: str) -> str:
    """Wrap text in a code fence longer than any backtick run inside it, so nothing breaks out."""
    longest = max((len(r) for r in re.findall(r"`+", text or "")), default=0)
    ticks = "`" * max(3, longest + 1)
    return f"{ticks}\n{text}\n{ticks}"


def _people(proj: dict) -> list[str]:
    """Everyone who has a step, in the order they first appear."""
    seen = []
    for s in proj.get("steps", []):
        if s.get("who") and s["who"] not in seen:
            seen.append(s["who"])
    return seen


STEP_WORD = {"done": "✓ done", "skipped": "skipped", "declined": "🙅 passed",
             "active": "▶ in progress", "pending": "… waiting", "waiting_for_user": f"⏸ waiting for {hp.USER}"}


def export_markdown(proj: dict, include_versions: bool = False, relay_only: bool = False) -> str:
    L = []
    w = L.append

    # ── header ───────────────────────────────────────────────────────────
    w(f"# {proj.get('name', proj['slug'])}")
    if proj.get("about"):
        w(f"\n{proj['about']}")
    w("")
    meta = [
        f"- **Status:** {proj.get('status', '?')}",
        f"- **Kind:** {proj.get('kind', 'writing')}" + (f" · **Author:** {proj['author']}" if proj.get("author") else ""),
        f"- **Created:** {_dt(proj.get('created_at'))}"
        + f" · **Last updated:** {_dt(proj.get('updated_at'))}"
        + (f" · **Finished:** {_dt(proj.get('finished_at'))}" if proj.get("finished_at") else ""),
    ]
    people = _people(proj)
    if people:
        meta.append(f"- **Everyone who took part:** {', '.join(people)}")
    if proj.get("word_goal"):
        meta.append(f"- **Word goal:** {proj['word_goal']:,}")
    if proj.get("target"):
        meta.append(f"- **Works on folder:** {proj['target']}")
    w("\n".join(meta))
    if proj.get("rules"):
        w(f"\n> **Rules for everyone (from {hp.USER}):** {proj['rules'].strip()}")
    w(f"\n*Exported {_dt(datetime.now().isoformat())} — this is the full record of the relay; read-only.*")

    # ── the relay, step by step ───────────────────────────────────────────
    w("\n\n---\n\n## The relay, step by step")
    steps = proj.get("steps", [])
    if not steps:
        w("\n*(No steps yet.)*")
    for i, s in enumerate(steps, 1):
        who = s.get("who", "?")
        role = s.get("role") or "(no specific role)"
        w(f"\n### {i}. {who} — {STEP_WORD.get(s.get('status'), s.get('status', '?'))}")
        w(f"*Role:* {role}")
        times = []
        if s.get("started_at"):
            times.append(f"started {_dt(s['started_at'])}")
        if s.get("done_at"):
            times.append(f"finished {_dt(s['done_at'])}")
        if times:
            w(f"*When:* {', '.join(times)}")
        if s.get("touched"):
            w(f"*Changed:* {', '.join(s['touched'])}")
        if s.get("handoff"):
            w(f"\n**Handoff note:** {s['handoff']}")
        if s.get("keep"):
            w(f"\n**Asked to keep:** {s['keep']}")
        if s.get("decline_reason"):
            w(f"\n**Passed because:** {s['decline_reason']}")
        if s.get("left_drafts"):
            w(f"\n*⚠ Left unfinished drafts in:* {', '.join(s['left_drafts'])}")
        if s.get("moved_on_by"):
            w(f"\n*({hp.USER} moved the relay on from this step.)*")
        for qa in s.get("qa", []):
            answerer = qa.get("from") or hp.USER
            w(f"\n- **{who} asked:** {qa.get('q', '')}")
            w(f"  **{answerer} answered:** {qa.get('a', '')}")

    # ── decisions ─────────────────────────────────────────────────────────
    decisions = proj.get("decisions", [])
    if decisions:
        w("\n\n---\n\n## Decisions logged (and why)")
        for d in decisions:
            w(f"- **{d.get('who', '?')}** ({_dt(d.get('at'))}): {d.get('text', '')}")

    # ── comments ──────────────────────────────────────────────────────────
    comments = proj.get("comments", [])
    if comments:
        w("\n\n---\n\n## Comments & discussion")
        for c in comments:
            where = ""
            if c.get("file"):
                where = f" on `{c['file']}`"
                if c.get("quote"):
                    where += f' at "{c["quote"]}"'
            w(f"- **{c.get('who', '?')}**{where} ({_dt(c.get('at'))}): {c.get('text', '')}")

    # ── files: current text + version history ────────────────────────────
    files = [f for f in hp.list_files(proj) if Path(f).suffix.lower() not in hp.PICTURE_EXT]
    pics = hp.pictures(proj)
    if files:
        w("\n\n---\n\n## Files")
        for rel in files:
            text, err = hp.read_file(proj, rel)
            w(f"\n### `{rel}`")
            if err:
                w(f"*(couldn't read: {err})*")
                continue
            st = hp.text_stats(text)
            w(f"*{st['words']:,} words · {st['paragraphs']} paragraphs*")
            hist = hp.file_history(proj, rel)
            if hist:
                w(f"\n*Saved versions ({len(hist)}):*")
                for v in hist:
                    w(f"- {_dt(v['at'])} — the version kept just before **{v['saved_before']}**'s edit")
            if relay_only:
                w("\n*(file contents left out of this relay-only export)*")
                continue
            w("\n**Current text:**")
            w(_fence(text))
            if include_versions and hist:
                for v in hist:
                    try:
                        vtext = Path(v["path"]).read_text(encoding="utf-8")
                    except OSError:
                        continue
                    w(f"\n<details><summary>Earlier version — before {v['saved_before']}'s edit "
                      f"({_dt(v['at'])})</summary>\n")
                    w(_fence(vtext))
                    w("</details>")
    if pics:
        w("\n\n---\n\n## Pictures attached")
        for p in pics:
            w(f"- `{p}`")

    # ── raw timeline ──────────────────────────────────────────────────────
    history = proj.get("history", [])
    if history:
        w("\n\n---\n\n## Full timeline (every logged event)")
        for e in history:
            w(f"- `{_dt(e.get('at'))}` — {e.get('event', '')} *(by {e.get('by', '?')})*")

    return "\n".join(L).rstrip() + "\n"


def write_export(proj: dict, out: Path | None, include_versions: bool, relay_only: bool = False) -> Path:
    md = export_markdown(proj, include_versions=include_versions, relay_only=relay_only)
    if out is None:
        EXPORTS.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M")
        suffix = "_relay" if relay_only else ""
        out = EXPORTS / f"{proj['slug']}_export{suffix}_{stamp}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md, encoding="utf-8")
    return out


def list_projects():
    projs = hp.all_projects()
    if not projs:
        print("No projects found under", hp.PROJECTS_ROOT)
        return
    print(f"{len(projs)} project(s):\n")
    for p in projs:
        n_steps = len(p.get("steps", []))
        print(f"  • {p.get('name', p['slug'])}")
        print(f"      slug: {p['slug']}  |  status: {p.get('status', '?')}  |  "
              f"{n_steps} step(s)  |  updated {_dt(p.get('updated_at'))}")
    print("\nExport one with:  python export_project.py \"<name or slug>\"")
    print("Export all with:  python export_project.py --all")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Export a workbench project's full record to Markdown.")
    ap.add_argument("project", nargs="?", default="", help="project name or slug (omit to list them)")
    ap.add_argument("--all", action="store_true", help="export every project")
    ap.add_argument("--versions", action="store_true", help="also include the full text of every saved version")
    ap.add_argument("--relay-only", dest="relay_only", action="store_true",
                    help="just who-said-and-did-what (steps, comments, decisions, timeline) — leave file contents out")
    ap.add_argument("--out", default="", help="write to this exact path (single project only)")
    a = ap.parse_args(argv)

    if a.all:
        projs = hp.all_projects()
        if not projs:
            print("No projects to export.")
            return 1
        for p in projs:
            path = write_export(p, None, a.versions, a.relay_only)
            print(f"✓ {p.get('name', p['slug'])}  →  {path}")
        print(f"\nAll {len(projs)} exported into {EXPORTS}")
        return 0

    if not a.project:
        list_projects()
        return 0

    proj = hp.find(a.project)
    if not proj:
        print(f"Couldn't find a project called \"{a.project}\".\n")
        list_projects()
        return 2
    out = Path(a.out) if a.out else None
    path = write_export(proj, out, a.versions, a.relay_only)
    print(f"✓ Exported \"{proj.get('name', proj['slug'])}\"  →  {path}")
    words = hp.text_stats(path.read_text(encoding='utf-8'))["words"]
    print(f"  ({words:,} words of record — paste it anywhere, or convert it to a PDF.)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
