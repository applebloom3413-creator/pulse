"""
House Projects — the end-of-project recap.

When a relay finishes, write RECAP.md into the project (who did what, how long
each turn took, what it cost, the questions asked, the files) and, for writing
projects, a PDF of the finished piece. The user gets a short phone ping with the totals.
"""

from datetime import datetime

from core import house_projects as hp

RECAP = "RECAP.md"


def _dur(a: str | None, b: str | None) -> str:
    try:
        secs = (datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds()
    except (TypeError, ValueError):
        return ""
    if secs < 90:
        return "about a minute"
    if secs < 3600:
        return f"{round(secs / 60)} min"
    h, m = divmod(round(secs / 60), 60)
    return f"{h} h {m} min" if m else f"{h} h"


def _when(s: str | None) -> str:
    try:
        d = datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return ""
    return f"{d:%b} {d.day}, {d.hour % 12 or 12}:{d:%M} {'AM' if d.hour < 12 else 'PM'}"


def build(proj: dict) -> tuple[str, dict]:
    """(markdown, totals)"""
    from core import house_costs, house_recipes
    steps = proj.get("steps", [])
    started = next((s.get("started_at") for s in steps if s.get("started_at")), proj.get("created_at"))
    ended = max((s.get("done_at") or "" for s in steps), default="") or hp.now_iso()
    total = house_costs.project_cost(proj)
    words, main = house_recipes.word_count(proj)
    lines = [f"# Recap: {proj['name']}", ""]
    lines.append(f"Finished {_when(ended)}. Started {_when(started)}, so it took {_dur(started, ended) or 'a moment'}.")
    if proj.get("about"):
        lines += ["", f"**What it was about:** {proj['about']}"]
    lines += ["", "## The relay", ""]
    word = {"done": "done ✓", "skipped": "skipped", "declined": "said no", "pending": "didn't get a turn",
            "active": "still going", "waiting_for_user": f"waiting for {hp.USER}"}
    for n, s in enumerate(steps, 1):
        bits = [word.get(s["status"], s["status"])]
        d = _dur(s.get("started_at"), s.get("done_at"))
        if d and s["status"] == "done":
            bits.append(d)
        c = house_costs.step_cost(s)
        if c is not None:
            bits.append(house_costs.money(c))
        if s.get("moved_on_by"):
            bits.append(f"moved on by {hp.USER}")
        lines.append(f"{n}. **{s['who']}**: {s.get('role') or 'no role given'} ({', '.join(bits)})")
        if s.get("handoff"):
            lines.append(f"    - Handoff: \"{s['handoff']}\"")
        if s.get("decline_reason"):
            lines.append(f"    - Said no because: \"{s['decline_reason']}\"")
        for qa in s.get("qa", []):
            asked = qa.get("from") or hp.USER
            lines.append(f"    - Asked {asked}: \"{qa['q']}\" ({asked}: \"{qa['a']}\")")
        for d in [d for d in proj.get("decisions", []) if d.get("who") == s["who"]
                  and (d.get("step") is None or d.get("step") == n - 1)]:
            lines.append(f"    - Decided: {d['text']}")
        if s.get("keep"):
            lines.append(f"    - Asked everyone to keep: \"{s['keep']}\"")
    files = [f for f in hp.list_files(proj) if f != RECAP]
    if files:
        lines += ["", "## Files", ""]
        for f in files:
            last = next((e for e in reversed(proj.get("file_log", [])) if e["file"] == f), None)
            n_versions = len(hp.file_history(proj, f))
            extra = f", last {last['action']} by {last['who']}" if last else ""
            if n_versions:
                extra += f", {n_versions + 1} versions"
            lines.append(f"- {f}{extra}")
    n_comments = len(proj.get("comments", []))
    lines += ["", "## Totals", ""]
    lines.append(f"- Turns taken: {sum(1 for s in steps if s['status'] == 'done')} of {len(steps)}")
    if words:
        lines.append(f"- Words in the main piece ({main}): {words:,}")
    lines.append(f"- Comments left: {n_comments}")
    lines.append(f"- Cost: {house_costs.money(total)}")
    totals = {"turns": sum(1 for s in steps if s["status"] == "done"), "steps": len(steps), "cost": total,
              "words": words, "main": main, "took": _dur(started, ended)}
    return "\n".join(lines) + "\n", totals


def colophon(proj: dict) -> str:
    """'Made together by the house' credits for the end of the finished piece."""
    lines = ["*Made together by the house*", ""]
    for s in proj.get("steps", []):
        if hp.is_user(s["who"]):
            continue
        if s["status"] == "done":
            lines.append(f"- {s['who']}" + (f": {s['role']}" if s.get("role") else ""))
        elif s["status"] == "declined":
            lines.append(f"- {s['who']} passed" + (f" (\"{s['decline_reason']}\")" if s.get("decline_reason") else ""))
    if any(hp.is_user(s["who"]) for s in proj.get("steps", [])):
        lines.append(f"- {hp.USER}")
    lines += ["", f"*{proj['name']}, {datetime.now():%B %Y}*"]
    return "\n".join(lines)


def write(proj: dict) -> str:
    """Write RECAP.md (and a PDF for writing projects). Returns a short summary for the user's ping."""
    from core import house_costs, house_recipes
    text, t = build(proj)
    hp.write_file(proj, hp.USER, RECAP, text)       # the user may always write; earlier recaps are kept as versions
    if hp.load(proj["slug"]):
        proj.update(hp.load(proj["slug"]))
    summary = f"{t['turns']} turn{'s' if t['turns'] != 1 else ''}, {house_costs.money(t['cost'])}"
    if t["took"]:
        summary += f", {t['took']}"
    if t["words"]:
        summary += f", {t['words']:,} words"
        if str(proj.get("kind", "")).lower() == "writing" and t["main"]:
            try:
                house_recipes.export_pdf(proj, t["main"], colophon=colophon(proj))
                summary += " (PDF in exports)"
            except Exception:
                pass
    return summary
