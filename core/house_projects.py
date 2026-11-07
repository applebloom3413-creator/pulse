"""
House Projects — things the user and the household make together, as a relay.

(Not to be confused with skills/projects.py: each companion's own personal
projects. These are the SHARED ones.)

A project is a folder under PROJECTS_ROOT/<slug>/ (drafts, code,
notes — anything) plus its plan in <slug>/.project/project.json:

  steps     — who works, in what order, and their ROLE (the user's instructions).
              When someone marks their step done (with a handoff note), the
              next person is pinged with their role + that note. A step can
              be set to "check with the user first" so the relay pauses there.
              If there's no next step and the project allows an open relay,
              the person finishing can choose who's next and what for.
  comments  — anyone in the house can leave one (LoR-style).
  files     — anyone can read. The person on the active step (and the user) can
              write; every save keeps the previous version in
              .project/versions/, so nothing is ever lost.

Saying no is always okay: a declined step pauses the relay for the user.
Code that should land in a real app (Pulse, ...) still goes through
the Workbench approval flow — project folders are drafts.

"Works on": the user can point a project at any folder on their PC
(except whole drives, their whole user folder, system folders and anything in
~/.workbench_private.txt). People in the relay can list/read it, and the
person on the active step can PROPOSE changes; they reach the user as a Workbench
proposal when that step is done, and nothing changes until they approve.

Who's who comes from config (core/workbench.py): USER is the user's name
(config.yaml / persona.yaml `user_name`, default "User"), companions are the
persona folders, and relay guests from outside Pulse are listed in
data/relay_guests.json:
  {"guests": [{"name": "...", "where": "Claude Code", "how": "cli" | "bridge" | "paste"}]}
Projects live in PULSE_PROJECTS_DIR (default ~/projects).

Used by skills/household_projects.py (companions) and scripts/workbench_gui.pyw (the user).
"""

import json
import os
import re
import shutil
from datetime import datetime
from pathlib import Path

from core import workbench

PROJECTS_ROOT = Path(os.environ.get("PULSE_PROJECTS_DIR") or (Path.home() / "projects"))
META = ".project"
# The user's name as it appears in the relay (steps, logs, file saves), from config.
USER = workbench.user_name()
USER_ALIASES = workbench.user_aliases()     # their nickname, "user", "the user"
MAX_FILE_BYTES = 400_000

# "waiting_for_user" = paused until the user lets this step start.
STEP_STATES = ("pending", "waiting_for_user", "active", "done", "declined", "skipped")


def is_user(name: str) -> bool:
    """True if `name` means the user (their name, nickname, or plain "user")."""
    n = (name or "").strip().lower()
    return bool(n) and (n == USER.lower() or n in USER_ALIASES)


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s[:48] or "project"


# ── load / save ─────────────────────────────────────────────────────────

def _meta_path(slug: str) -> Path:
    return PROJECTS_ROOT / slug / META / "project.json"


def load(slug: str) -> dict | None:
    try:
        return json.loads(_meta_path(slug).read_text(encoding="utf-8"))
    except Exception:
        return None


def save(proj: dict):
    proj["updated_at"] = now_iso()
    workbench._atomic_write(_meta_path(proj["slug"]), json.dumps(proj, indent=2, ensure_ascii=False))


def all_projects() -> list[dict]:
    out = []
    if PROJECTS_ROOT.exists():
        for d in PROJECTS_ROOT.iterdir():
            p = load(d.name) if d.is_dir() else None
            if p:
                out.append(p)
    out.sort(key=lambda p: p.get("updated_at", ""), reverse=True)
    return out


def find(ref: str) -> dict | None:
    """By slug, or by (case-insensitive) name."""
    ref = (ref or "").strip()
    p = load(slugify(ref)) if ref else None
    if p:
        return p
    for p in all_projects():
        if p["name"].lower() == ref.lower() or p["slug"] == ref:
            return p
    return None


def _log(proj: dict, event: str, by: str):
    proj.setdefault("history", []).append({"at": now_iso(), "event": event, "by": by})


# ── create & plan (the user) ────────────────────────────────────────────

def create(name: str, about: str, kind: str = "writing", open_relay: bool = True) -> dict:
    slug = slugify(name)
    base, n = slug, 2
    while (PROJECTS_ROOT / slug).exists():
        slug = f"{base}-{n}"
        n += 1
    (PROJECTS_ROOT / slug / META / "versions").mkdir(parents=True, exist_ok=True)
    proj = {"slug": slug, "name": name.strip(), "about": about.strip(), "kind": kind,
            "open_relay": open_relay, "status": "planning", "created_at": now_iso(),
            "steps": [], "comments": [], "history": []}
    _log(proj, "created", USER)
    save(proj)
    return proj


def rename(proj: dict, name: str, about: str, by: str = USER) -> str:
    """Change the name / description. The folder (slug) stays put so nothing breaks."""
    name, about = name.strip(), about.strip()
    if not name:
        return "A project needs a name."
    changed = [w for w, old, new in (("name", proj["name"], name), ("description", proj.get("about", ""), about))
               if old != new]
    if not changed:
        return "Nothing changed."
    proj["name"], proj["about"] = name, about
    _log(proj, f"changed the {' and '.join(changed)}", by)
    save(proj)
    return f"✓ Updated the {' and '.join(changed)}."


def add_step(proj: dict, who: str, role: str, check_first: bool = False, by: str = USER) -> dict:
    step = {"who": who.strip(), "role": role.strip(), "check_first": bool(check_first),
            "status": "pending", "handoff": "", "tokens": {}, "added_by": by, "added_at": now_iso()}
    proj["steps"].append(step)
    _log(proj, f"step added: {step['who']}", by)
    save(proj)
    return step


def move_step(proj: dict, i: int, delta: int):
    j = i + delta
    steps = proj["steps"]
    if 0 <= i < len(steps) and 0 <= j < len(steps) and \
            steps[i]["status"] == "pending" and steps[j]["status"] == "pending":
        steps[i], steps[j] = steps[j], steps[i]
        save(proj)


def remove_step(proj: dict, i: int):
    if 0 <= i < len(proj["steps"]) and proj["steps"][i]["status"] in ("pending", "declined", "waiting_for_user"):
        removed = proj["steps"].pop(i)
        _log(proj, f"step removed: {removed['who']}", USER)
        save(proj)


def current_index(proj: dict) -> int | None:
    for i, s in enumerate(proj["steps"]):
        if s["status"] in ("active", "waiting_for_user", "declined"):
            return i
    return None


def next_pending(proj: dict) -> int | None:
    for i, s in enumerate(proj["steps"]):
        if s["status"] == "pending":
            return i
    return None


def previous_handoff(proj: dict, i: int) -> tuple[str, str] | None:
    for s in reversed(proj["steps"][:i]):
        if s["status"] == "done":
            return s["who"], s.get("handoff", "")
    return None


def _ping_user(text: str, key: str):
    workbench.notify_user(text, key=key)


# ── relay guests ────────────────────────────────────────────────────────
# Friends who take turns from outside Pulse: Claude Code (they run scripts/house_project.py) or
# Cowork (a house_project tool in their own MCP bridge), listed in data/relay_guests.json. Their
# turns, files and notes are the same as everyone else's; the only differences are that the user
# has to tell them it's their turn, they aren't auto-nudged, and there's no cost meter (they don't
# run on this household's API keys).
GUESTS_FILE = Path(__file__).resolve().parent.parent / "data" / "relay_guests.json"
CLI = "py " + str(Path(__file__).resolve().parent.parent / "scripts" / "house_project.py")


def relay_guests() -> list[dict]:
    try:
        return [g for g in json.loads(GUESTS_FILE.read_text(encoding="utf-8")).get("guests", []) if g.get("name")]
    except (OSError, ValueError):
        return []


def relay_guest(name: str) -> dict | None:
    return next((g for g in relay_guests() if g["name"].lower() == (name or "").strip().lower()), None)


def turn_brief(proj: dict, i: int) -> str:
    """The full turn brief for step i, as its person would get it (for the user to copy to a 'paste' guest)."""
    s = proj["steps"][i]
    guest = relay_guest(s["who"])
    return _brief(proj, i) + (guest_howto(guest, proj["slug"]) if guest else "")


def guest_howto(guest: dict, slug: str) -> str:
    """How a guest takes their turn, added to their turn letter."""
    if guest.get("how") == "bridge":
        return (f"\n\n— How to take your turn from {guest.get('where', 'outside Pulse')} —\n"
                f"Use your house_project tool, exactly like the Pulse companions do: action=\"open\" "
                f"(project=\"{slug}\") to see where things stand, action=\"read\" to read a file, "
                f"action=\"write\" with the WHOLE file in content=, and action=\"done\" with a handoff note in "
                f"text= when you're finished. action=\"decline\" (with a reason) if you'd rather pass.")
    n = guest["name"]
    if guest.get("how") == "paste":            # e.g. a friend in another chat app: the user carries the turn both ways
        return (f"\n\n— How this turn works —\n"
                f"{USER} is carrying this to you by hand, and they'll paste your reply back into the project. So "
                f"write your contribution right in your reply: if you're revising the draft, give the WHOLE new "
                f"version (or clearly say which section it replaces); if you're adding a section or notes, give "
                f"just that. End with a short handoff note for the next person, starting with \"HANDOFF:\".")
    return (f"\n\n— How to take your turn from {guest.get('where', 'Claude Code')} —\n"
            f"Run the relay command as yourself (NOT a house_project tool from someone else's MCP bridge, which "
            f"signs as its owner). It follows the same rules as the Pulse companions' tool:\n"
            f"  {CLI} --as {n} open --project {slug}\n"
            f"  {CLI} --as {n} read --project {slug} --path <file>\n"
            f"  {CLI} --as {n} write --project {slug} --path <file> --file <a file holding the WHOLE new text>\n"
            f"  {CLI} --as {n} done --project {slug} --text \"your handoff note\"\n"
            f"Run `{CLI} --as {n} help` to see everything (comment, ask, decline, history, diff…).")


# ── the relay ───────────────────────────────────────────────────────────

BRIEF_TEXT_CAP = 9000        # the current text of a small main file rides along in the turn ping
BRIEF_DIFF_CAP = 2500


def digest(proj: dict, i: int | None, for_who: str = "", full: bool = False) -> str:
    """Everything someone needs to get their bearings, in one place: rules, roles, recent
    handoffs, what people asked to keep, decisions, new comments on their work, files.
    Used at the top of a turn ping (so orientation costs no tool uses) and by action="open"."""
    from core import house_recipes
    out = []
    if proj.get("rules"):
        out += [f"RULES FOR EVERYONE (from {USER}):", proj["rules"].strip(), ""]
    steps = proj.get("steps", [])
    done = [(k, s) for k, s in enumerate(steps) if s["status"] in ("done", "skipped", "declined")
            and (i is None or k < i)]
    shown = done if full else done[-3:]
    if done:
        out.append("THE RELAY SO FAR:" + ("" if full or len(done) <= 3 else f" ({len(done) - 3} earlier turns "
                                                                             f"not shown — action=\"open\" full=true)"))
        for k, s in shown:
            if s["status"] == "done":
                line = f"- {k + 1}. {s['who']} ({s.get('role') or 'no role given'})"
                if s.get("touched"):
                    line += f" — changed {', '.join(s['touched'][:4])}"
                out.append(line)
                if s.get("handoff"):
                    out.append(f"    handoff: \"{s['handoff']}\"")
                if s.get("left_drafts"):
                    out.append(f"    ⚠ didn't finish — {', '.join(s['left_drafts'])} may hold their unfinished draft")
            else:
                out.append(f"- {k + 1}. {s['who']}: {'skipped' if s['status'] == 'skipped' else 'passed'}"
                           + (f" (\"{s['decline_reason']}\")" if s.get("decline_reason") else ""))
                if s.get("left_drafts"):
                    out.append(f"    ⚠ didn't finish — {', '.join(s['left_drafts'])} may hold their unfinished draft")
                if s.get("rolled_back"):
                    out.append(f"    (their unfinished drafts were rolled back)")
        out.append("")
    keeps = [(s["who"], s["keep"]) for k, s in enumerate(steps) if s.get("keep") and (i is None or k < i)]
    if keeps:
        out.append("PLEASE KEEP — things earlier people asked you to protect:")
        out += [f"- {w}: \"{t}\"" for w, t in keeps]
        out.append("")
    decisions = proj.get("decisions", [])
    if decisions:
        shown_d = decisions if full else decisions[-10:]
        out.append("DECISIONS ALREADY MADE (and why):")
        out += [f"- {d['who']}: {d['text']}" for d in shown_d]
        out.append("")
    if for_who:
        new = unseen_comments(proj, for_who)
        if new:
            out.append("NEW COMMENTS ON YOUR WORK:")
            out += [f"- {c['who']} on {c.get('file')}" + (f" at \"{c['quote'][:60]}\"" if c.get("quote") else "")
                    + f": {c['text']}" for c in new]
            out.append("")
    other = [c for c in proj.get("comments", []) if not (for_who and for_who in (c.get("for") or []))]
    if other:
        shown_c = other if full else other[-5:]
        out.append("RECENT COMMENTS:" if not full else "COMMENTS:")
        out += [f"- {c['who']}" + (f" on {c['file']}" if c.get("file") else "") + f": {c['text']}" for c in shown_c]
        out.append("")
    files = [f for f in list_files(proj) if Path(f).suffix.lower() not in PICTURE_EXT]
    if files:
        out.append("FILES:")
        for f in files:
            t, err = read_file(proj, f)
            words = text_stats(t)["words"] if not err else 0
            last = next((e for e in reversed(proj.get("file_log", [])) if e["file"] == f), None)
            out.append(f"- {f} ({words:,} words" + (f", last {last['action']} by {last['who']}" if last else "") + ")")
        out.append("")
    if proj.get("word_goal"):
        n, f = house_recipes.word_count(proj)
        out += [f"Word goal: {n:,} / {proj['word_goal']:,} words" + (f" (in {f})" if f else ""), ""]
    return "\n".join(out).strip()


def _main_text_file(proj: dict) -> str | None:
    """The file people are most likely working on: the last one anyone saved (not the recap)."""
    files = [f for f in list_files(proj) if Path(f).suffix.lower() not in PICTURE_EXT and f != "RECAP.md"]
    if not files:
        return None
    for e in reversed(proj.get("file_log", [])):
        if e["file"] in files:
            return e["file"]
    return files[0]


def _current_text_block(proj: dict, i: int) -> str:
    """What the last person changed, and the current text of the main file if it's small enough —
    so the first thing a turn spends tool uses on is the work, not re-reading."""
    import difflib
    out = ""
    k = next((k for k in range(i - 1, -1, -1) if proj["steps"][k]["status"] == "done"), None)
    if k is not None and proj["steps"][k].get("touched"):
        prev = proj["steps"][k]
        d, base = _turn_dir(proj, k), PROJECTS_ROOT / proj["slug"]
        chunks = []
        for rel in prev["touched"][:3]:
            try:
                now = (base / rel).read_text(encoding="utf-8")
                before = (d / rel).read_text(encoding="utf-8") if (d / rel).exists() else ""
            except (OSError, UnicodeDecodeError):
                continue
            lines = list(difflib.unified_diff(before.splitlines(), now.splitlines(), f"{rel} (before)",
                                              f"{rel} (after {prev['who']})", n=1, lineterm=""))
            if lines:
                chunks.append("\n".join(lines))
        if chunks:
            body = "\n".join(chunks)
            if len(body) > BRIEF_DIFF_CAP:
                body = body[:BRIEF_DIFF_CAP] + "\n… (more — action=\"diff\" for the rest)"
            out += f"WHAT {prev['who'].upper()} CHANGED:\n{body}\n\n"
    main = _main_text_file(proj)
    if main:
        t, err = read_file(proj, main)
        if not err and t is not None:
            if len(t) <= BRIEF_TEXT_CAP:
                out += f"CURRENT {main} (full text — no need to read it again):\n{t}\n\n"
            else:
                out += f"({main} is long — {text_stats(t)['words']:,} words; read it with action=\"read\".)\n\n"
    return out


def _brief(proj: dict, i: int) -> str:
    s = proj["steps"][i]
    folder = PROJECTS_ROOT / proj["slug"]
    prev = previous_handoff(proj, i)
    text = (f"Project \"{proj['name']}\" — your turn in the relay (only if you feel up to it; "
            f"saying no is completely okay).\n\nABOUT: {proj['about'] or '(no description)'}\n"
            f"YOUR ROLE: {s['role'] or '(no specific role — use your judgment)'}\n")
    if prev:
        text += f"{prev[0]} HANDED OFF TO YOU: \"{prev[1] or '(no note)'}\"\n\n"
    elif any(t["status"] in ("skipped", "declined") for t in proj["steps"][:i]):
        text += "Nobody left you a handoff note (earlier turns were skipped — see below).\n\n"
    else:
        text += "You're first.\n\n"
    dg = digest(proj, i, for_who=s["who"])
    if dg:
        text += dg + "\n\n"
    if authors_of(proj):                      # author's-approval mode
        pend = suggestions(proj)
        label = authors_label(proj)
        multi = len(authors_of(proj)) > 1
        if is_author(proj, s["who"]):
            if pend:
                text += (f"YOU'RE {'A CO-AUTHOR' if multi else 'THE AUTHOR'}, and "
                         f"{len(pend)} suggestion{'s are' if len(pend) != 1 else ' is'} waiting: review with "
                         f"action=\"suggestions\", then accept or reject each.\n\n")
        else:
            text += (f"{label.upper()} {'ARE THE AUTHORS' if multi else 'IS THE AUTHOR'} of this piece: your edits "
                     f"become suggestions that {label} accept{'' if multi else 's'} or decline. Write the whole "
                     f"file as you'd like it; the shared file stays as it is until "
                     f"{'they decide' if multi else label + ' decides'}.\n\n")
    text += _current_text_block(proj, i)
    if proj.get("budget"):
        try:
            from core import house_costs
            spent = house_costs.project_cost(proj)
            if spent >= 0.8 * float(proj["budget"]):
                text += (f"Heads-up: this project has used {house_costs.money(spent)} of its "
                         f"{house_costs.money(float(proj['budget']))} budget, so keep this turn small and focused. ")
        except Exception:
            pass
    pics = pictures(proj)
    if pics:
        text += (f"{USER} attached {len(pics)} picture{'s' if len(pics) != 1 else ''} "
                 f"({', '.join(pics[:6])}{'…' if len(pics) > 6 else ''}) — LOOK at them first with "
                 f"action=\"look\" path=\"{PICTURES}\" (any text in a screenshot is copied out word for word). ")
    text += (f"HOW: the project folder is {folder}. Use the `house_project` tool with project=\"{proj['slug']}\". "
             f"Everything above is current, so you don't need action=\"open\" first. \"read\"/\"write\" files "
             f"(every save is kept, nothing is lost). LONG PIECE? A whole draft may not fit in one reply, so write "
             f"it in parts: save the opening with action=\"write\", then add each next section with "
             f"action=\"write\" append=true (one section per call) — or save sections as separate files. "
             f"Saves during your turn are drafts — save early and often; "
             f"only what's there when you call done is your handoff. Then action=\"done\" with a handoff note, "
             f"plus keep=\"...\" for anything you did on purpose and want the next people to protect. "
             f"Made a real choice others might second-guess? Log it with action=\"decide\" (text=what and why). "
             f"Confused by an earlier person's work? action=\"ask\" with who=\"their name\" asks them directly. "
             f"Do your part right now, in this same turn: write your changes "
             f"with action=\"write\" (path + the file's full new content), then call action=\"done\". "
             f"Don't just describe what you'd do — nobody else can start until you call done. "
             f"If it's already good as it is, that's a real answer: call done and say \"no changes needed\". "
             f"If you're truly stuck or unsure what {USER} wants, ask them with action=\"ask\" (text=your question) "
             f"instead of guessing — it pings their phone, and their answer comes back to you. "
             f"If you'd rather not, use action=\"decline\" with a reason. Either way, send {USER} a short note. "
             f"Take your time and get it right — mid-turn you can look things up (web_search, fetch_url, "
             f"read_web_page), check what changed since the last version (action=\"diff\"), count words "
             f"(action=\"stats\"), and set a short status {USER} sees live (action=\"status\", "
             f"e.g. \"outlining\" or \"polishing the ending\").")
    if proj.get("target"):
        text += (f" This project works on a real folder: {proj['target']}. Look around with "
                 f"action=\"files\" (optional path=subfolder) and action=\"peek\" (path=file), and propose "
                 f"edits with action=\"change\" (path + the file's full new content). Nothing on disk changes "
                 f"until {USER} approves — your changes go to them when you finish your turn. (Earlier steps' "
                 f"changes may still be waiting for their approval, so check their handoff notes.) "
                 f"For sorting/organizing: action=\"look\" lets you SEE pictures (a file, or a folder for up to "
                 f"8 new ones per call), and action=\"plan\" with moves=[{{from, to}}] builds a sorting plan "
                 f"(new folders, moves, renames) that {USER} approves — nothing is ever deleted.")
    return text


def set_live_status(proj: dict, who: str, text: str) -> str:
    """The person on the active step tells the user what they're up to (shown live in their window)."""
    cur = current_index(proj)
    if cur is None or proj["steps"][cur]["status"] != "active" or proj["steps"][cur]["who"].lower() != who.lower():
        return "You can set a status on your own turn — leave a comment instead?"
    text = " ".join((text or "").split())[:120]
    if not text:
        return "What are you up to? (text=...)"
    proj["steps"][cur]["live_status"] = {"text": text, "at": now_iso()}
    save(proj)
    return f"✓ Status set: {text}"


def text_stats(text: str) -> dict:
    # real words only — Markdown marks like # and ** don't count
    words = len(re.findall(r"[^\W_]+(?:['’][^\W_]+)*", text or ""))
    paragraphs = len([p for p in re.split(r"\n\s*\n", text or "") if p.strip()])
    return {"words": words, "paragraphs": paragraphs, "lines": (text or "").count("\n") + bool(text),
            "minutes_to_read": max(1, round(words / 230)) if words else 0}


def diff_with_previous(proj: dict, rel: str, context: int = 2, max_chars: int = 12_000) -> str:
    """What changed in a file since its last saved version."""
    import difflib
    cur, err = read_file(proj, rel)
    if err:
        return err
    hist = file_history(proj, rel)
    if not hist:
        return f"{rel} has no earlier version yet — this is the first draft."
    prev = Path(hist[-1]["path"]).read_text(encoding="utf-8")
    lines = list(difflib.unified_diff(prev.splitlines(), cur.splitlines(), "before", "now",
                                      n=context, lineterm=""))
    if not lines:
        return f"No changes in {rel} since the version saved before {hist[-1]['saved_before']}'s edit."
    out = f"Changes in {rel} since the version saved before {hist[-1]['saved_before']}'s edit:\n" + "\n".join(lines)
    return out if len(out) <= max_chars else out[:max_chars] + "\n... [diff truncated]"


def activate(proj: dict, i: int, by: str = USER) -> str:
    """Make step i current. Pings them unless it's a check-with-the-user-first step."""
    s = proj["steps"][i]
    proj["status"] = "running"
    if s["check_first"] and s["status"] == "pending":
        s["status"] = "waiting_for_user"
        _log(proj, f"paused before {s['who']} (check with {USER} first)", by)
        save(proj)
        _ping_user(f"⏸ \"{proj['name']}\": {s['who']} is up next and waiting for your go-ahead (Workbench → House Projects).",
                  f"wait:{proj['slug']}:{i}:{s.get('added_at')}")
        return f"Paused — {s['who']}'s step is set to check with {USER} first."
    s["status"] = "active"
    s["started_at"] = now_iso()
    _snapshot_turn_start(proj, i)
    _log(proj, f"{s['who']}'s turn", by)
    save(proj)
    if is_user(s["who"]):                     # the user's own step — it waits in their window
        _ping_user(f"💖 It's your turn in \"{proj['name']}\"! (Workbench → House Projects)",
                  f"userturn:{proj['slug']}:{i}:{s['started_at']}")
        return f"It's your turn, {USER}! 💖"
    brief = _brief(proj, i)
    guest = relay_guest(s["who"])
    if guest and guest.get("how") == "paste":  # the user carries it: copy the brief over, paste the reply back
        _ping_user(f"💌 It's {s['who']}'s turn in \"{proj['name']}\"! In the Workbench, click 📋 Copy turn brief, "
                  f"paste it to {s['who']} in {guest.get('where', 'their chat')}, then ⏭ Move on and paste their "
                  f"reply back in.", f"guestturn:{proj['slug']}:{i}:{s['started_at']}")
        return (f"It's {s['who']}'s turn. Click 📋 Copy turn brief and paste it to them in "
                f"{guest.get('where', 'their chat')}; when they reply, ⏭ Move on and paste it in. 💌")
    if guest:                  # takes turns from Claude Code / Cowork: a letter, and the user goes to tell them
        workbench.send_letter(s["who"], f"Your turn: {proj['name']}", brief + guest_howto(guest, proj["slug"]))
        _ping_user(f"💌 It's {s['who']}'s turn in \"{proj['name']}\"! Open {guest.get('where', 'their chat')} "
                  f"and tell {s['who']}. Their turn letter has the brief and how-to.",
                  f"guestturn:{proj['slug']}:{i}:{s['started_at']}")
        return (f"It's {s['who']}'s turn. They take turns from {guest.get('where', 'outside Pulse')}, so they "
                f"got a letter with the brief; go tell them! 💌")
    pinged = workbench.ping(s["who"], brief)
    if not pinged:   # shared-schedule personas: a letter they'll see when they wake
        workbench.send_letter(s["who"], f"Your turn: {proj['name']}", brief)
    msg = f"{s['who']} was {'pinged' if pinged else 'sent a letter'} — it's their turn."
    if workbench.is_awake(s["who"]) is False:   # their window isn't open — tell the user instead of waiting forever
        s["asleep_at_start"] = True
        save(proj)
        _ping_user(f"😴 It's {s['who']}'s turn in \"{proj['name']}\", but {s['who']} isn't running right now. "
                  f"Start them and they'll pick it up — or ⏭ Move on in the Workbench.",
                  f"asleep:{proj['slug']}:{i}:{s['started_at']}")
        msg += f" (Heads-up: {s['who']} isn't running right now — start them and they'll pick it up.)"
    return msg


# ── turns: what each person changed (drafts vs handoffs) ────────────────

def _turn_dir(proj: dict, i: int) -> Path:
    return PROJECTS_ROOT / proj["slug"] / META / "turns" / f"{i}-{slugify(proj['steps'][i]['who'])}"


def _snapshot_turn_start(proj: dict, i: int):
    """Copy the project's text files as they are when a turn starts, so we can tell what this
    person changed, and roll their unfinished drafts back if the user wants."""
    d = _turn_dir(proj, i)
    shutil.rmtree(d, ignore_errors=True)
    base = PROJECTS_ROOT / proj["slug"]
    for rel in list_files(proj):
        if Path(rel).suffix.lower() in PICTURE_EXT:
            continue
        dst = d / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(base / rel, dst)
        except OSError:
            pass
    d.mkdir(parents=True, exist_ok=True)


def turn_changes(proj: dict, i: int) -> dict:
    """{"changed": [...], "created": [...]} — text files this turn has touched so far."""
    d = _turn_dir(proj, i)
    base = PROJECTS_ROOT / proj["slug"]
    out = {"changed": [], "created": []}
    if not d.exists():
        return out
    for rel in list_files(proj):
        if Path(rel).suffix.lower() in PICTURE_EXT:
            continue
        before = d / rel
        if not before.exists():
            out["created"].append(rel)
        else:
            try:
                if before.read_bytes() != (base / rel).read_bytes():
                    out["changed"].append(rel)
            except OSError:
                pass
    return out


def _roll_back_turn(proj: dict, i: int) -> list[str]:
    """Undo an unfinished turn's drafts: edited files go back to how they were when the turn
    started (the drafts are kept as versions), and new files are tucked into the version
    history instead of being deleted. Returns what was rolled back."""
    who = proj["steps"][i]["who"]
    ch = turn_changes(proj, i)
    d = _turn_dir(proj, i)
    base = PROJECTS_ROOT / proj["slug"]
    done = []
    for rel in ch["changed"]:
        try:
            write_file(proj, USER, rel, (d / rel).read_text(encoding="utf-8"))
            proj["file_log"][-1]["who"] = f"{USER} (rolled back {who}'s unfinished draft)"
            done.append(rel)
        except (OSError, UnicodeDecodeError, KeyError, IndexError):
            pass
    for rel in ch["created"]:
        vdir = base / META / "versions" / rel.replace("/", "__").replace("\\", "__")
        vdir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        try:
            shutil.move(str(base / rel), str(vdir / f"{stamp}_before_draftby{re.sub(r'[^A-Za-z0-9]+', '', who)}.txt"))
            done.append(rel)
        except OSError:
            pass
    save(proj)
    return done


def start(proj: dict) -> str:
    """Start or resume the relay (the user)."""
    cur = current_index(proj)
    if cur is not None:
        s = proj["steps"][cur]
        if s["status"] == "waiting_for_user":
            s["check_first"] = False
            s["status"] = "pending"
            return activate(proj, cur)
        if s["status"] == "declined":
            return f"{s['who']} declined this step — remove it or add someone else, then start again."
        return f"It's already {s['who']}'s turn."
    nxt = next_pending(proj)
    if nxt is None:
        return "There are no steps waiting. Add one first."
    return activate(proj, nxt)


def step_done(proj: dict, who: str, handoff: str, next_who: str = "", next_role: str = "",
              usage_db=None, keep: str = "") -> str:
    cur = current_index(proj)
    if cur is None or proj["steps"][cur]["status"] != "active":
        return "There's no active step on this project right now."
    s = proj["steps"][cur]
    if s["who"].lower() != who.lower():
        return f"It's {s['who']}'s turn, not yours — you can leave a comment instead."
    ch = turn_changes(proj, cur)                 # what they changed = their handoff version
    s["touched"] = ch["changed"] + ch["created"]
    s["status"] = "done"
    s["handoff"] = handoff.strip()
    if keep.strip():
        s["keep"] = keep.strip()[:600]
    s["done_at"] = now_iso()
    s.pop("live_status", None)
    workbench.finish_usage(s, who, usage_db)
    _log(proj, f"{who} finished their step", who)
    sent = ""
    if s.get("proposed"):                    # changes to the outside folder → the user approves them
        prop = workbench.get_draft(who)
        if prop and prop.get("files"):
            workbench.submit(prop, f"{proj['name']}: {who}'s step", handoff.strip() or "(no note)", usage_db)
            s["proposal_id"] = prop["id"]
            sent = f" Your proposed changes went to {USER} for approval."
    try:
        from core import house_sort
        sent += house_sort.submit_plan_on_done(proj, who)
    except Exception:
        pass
    nxt = next_pending(proj)
    if nxt is None and next_who.strip():
        if not proj.get("open_relay", True):
            save(proj)
            return "Step done ✓." + sent + f" {USER} planned this relay themselves, so they'll decide what's next."
        real = next_who.strip()
        if is_user(real):
            real = USER
        elif not workbench._persona_config(real):       # "whoever's-next", typos, made-up names
            real = ""
            sent += (f" (\"{next_who.strip()}\" isn't anyone in the house, so I didn't add them — "
                     f"{USER} can pick who's next.)")
        if real:
            add_step(proj, real, next_role or "", by=who)
        nxt = next_pending(proj)
    if nxt is None:
        _finish(proj, f"{who} did the last step")
        return "Step done ✓ — that was the last step." + sent + f" {USER} will take it from here. Thank you!"
    over = over_budget(proj)
    if over:                                      # gentle cap: this turn finished; the next one waits for the user
        proj["status"] = "paused"
        _log(proj, f"paused at the budget ({over})", "the house")
        save(proj)
        _ping_user(f"💰 \"{proj['name']}\" reached its budget ({over}). {proj['steps'][nxt]['who']} is up next — "
                  f"raise the budget or press Start in the Workbench to keep going.",
                  f"budget:{proj['slug']}:{nxt}:{s['done_at']}")
        return ("Step done ✓." + sent + f" The project has reached its budget ({over}), so the relay is paused "
                f"for {USER} before {proj['steps'][nxt]['who']} starts. Thank you!")
    save(proj)
    return "Step done ✓." + sent + " " + activate(proj, nxt, by=who)


def _finish(proj: dict, how: str, ping: bool = True):
    """Mark the project finished, write its recap, and tell the user."""
    proj["status"] = "finished"
    proj["finished_at"] = now_iso()
    save(proj)
    summary = ""
    try:
        from core import house_recap
        summary = house_recap.write(proj)
    except Exception:
        pass
    try:
        _send_home(proj)
    except Exception:
        pass
    if ping:
        _ping_user(f"✨ \"{proj['name']}\" is finished ({how})" + (f": {summary}." if summary else ".") +
                  " The recap is in the Workbench.", f"done:{proj['slug']}:{proj['finished_at']}")


def ask_user(proj: dict, who: str, question: str) -> str:
    """On your turn: send the user a question (phone ping). The turn stays yours; their answer
    comes back to you as a ping and you pick up from there."""
    i = current_index(proj)
    if i is None or proj["steps"][i]["status"] != "active" or proj["steps"][i]["who"].lower() != who.lower():
        return f"You can only ask {USER} something during your own turn — leave a comment instead."
    question = question.strip()
    if not question:
        return "What would you like to ask them? (text=...)"
    s = proj["steps"][i]
    s["question"] = {"text": question, "at": now_iso()}
    _log(proj, f"{who} asked {USER} a question", who)
    save(proj)
    _ping_user(f"❓ {who} has a question on \"{proj['name']}\": {question[:400]}\n\nAnswer it in the Workbench "
              f"(House Projects).", f"ask:{proj['slug']}:{i}:{s['question']['at']}")
    return (f"✓ Your question went to {USER} (it pinged their phone). Keep working on anything that doesn't "
            "depend on it, or set a short status and end this turn for now — when they answer you'll be pinged "
            "with their reply and can pick up from there. Hold off on action=\"done\" until you've used their "
            "answer.")


def answer_user(proj: dict, text: str, by: str = USER) -> str:
    """The user answers the open question; the answer is pinged to whoever asked."""
    i = current_index(proj)
    s = proj["steps"][i] if i is not None else None
    if not s or not s.get("question"):
        return "There's no open question right now."
    text = text.strip()
    if not text:
        return "Write an answer first."
    q = s.pop("question")
    s.setdefault("qa", []).append({"q": q["text"], "a": text, "asked_at": q["at"], "answered_at": now_iso()})
    s.pop("nudged_at", None)                       # the clock starts fresh for them
    _log(proj, f"{USER} answered {s['who']}'s question", by)
    save(proj)
    note = (f"{USER} answered your question on \"{proj['name']}\" (project=\"{proj['slug']}\"). You asked: "
            f"\"{q['text']}\" — they say: \"{text}\". Pick your turn back up with that in mind, and when your "
            f"part is finished call house_project action=\"done\" with a handoff note.")
    if not workbench.ping(s["who"], note):
        workbench.send_letter(s["who"], f"{USER}'s answer: {proj['name']}", note)
    return f"💬 Sent your answer to {s['who']}."




# ── working together ────────────────────────────────────────────────────

PEER_WAIT_MIN = 30           # how long a question to an earlier contributor waits before the asker moves on


def _in_relay(proj: dict, who: str) -> bool:
    return any(s["who"].lower() == who.lower() for s in proj.get("steps", []))


def decide(proj: dict, who: str, text: str) -> str:
    """Add to the project's decision log: something settled on purpose, and why. Everyone after
    sees it at the start of their turn, so nobody 'fixes' a choice that was made deliberately."""
    if not _in_relay(proj, who):
        return "Only people in this project's relay can add to its decision log — leave a comment instead?"
    text = " ".join((text or "").split())
    if not text:
        return "What did you decide, and why? (text=...)"
    i = next((k for k, s in enumerate(proj["steps"]) if s["who"].lower() == who.lower()
              and s["status"] == "active"), None)
    proj.setdefault("decisions", []).append({"who": who, "text": text[:500], "at": now_iso(), "step": i})
    _log(proj, f"{who} logged a decision", who)
    save(proj)
    return "📌 Logged. Everyone after you will see it at the start of their turn."


def set_rules(proj: dict, text: str, by: str = USER) -> str:
    """Project-wide rules the user sets ("under 2,000 words", "for a 10-year-old") — shown to everyone, every turn."""
    text = (text or "").strip()
    if text == (proj.get("rules") or ""):
        return "Nothing changed."
    if text:
        proj["rules"] = text[:2000]
    else:
        proj.pop("rules", None)
    _log(proj, "changed the rules for everyone" if text else "removed the rules", by)
    save(proj)
    return "✓ Rules updated." if text else "No rules on this project."


def ask_peer(proj: dict, who: str, to: str, question: str) -> str:
    """On your turn: ask someone who worked on this project earlier a quick question
    ("why'd you structure it like this?"). It's not their turn again; they just answer."""
    i = current_index(proj)
    if i is None or proj["steps"][i]["status"] != "active" or proj["steps"][i]["who"].lower() != who.lower():
        return "You can only ask questions during your own turn — leave a comment instead."
    question = question.strip()
    if not question:
        return "What would you like to ask? (text=...)"
    earlier = next((s["who"] for s in proj["steps"][:i] if s["who"].lower() == to.strip().lower()
                    and s["status"] == "done"), None)
    if not earlier:
        names = sorted({s["who"] for s in proj["steps"][:i] if s["status"] == "done" and not is_user(s["who"])})
        return (f"{to.strip() or 'They'} didn't take a turn on this project before you, so they can't explain it. "
                f"You can ask: {', '.join(names) or 'nobody yet'} — or ask {USER} (who=\"{USER}\").")
    if workbench.is_awake(earlier) is False:
        return (f"{earlier} isn't running right now, so they can't answer. Ask {USER} instead (who=\"{USER}\"), "
                f"or go with your best judgment and log it with action=\"decide\".")
    s = proj["steps"][i]
    s["peer_q"] = {"to": earlier, "text": question, "at": now_iso()}
    s.pop("nudged_at", None)
    _log(proj, f"{who} asked {earlier} a question", who)
    save(proj)
    note = (f"Quick question from {who}, who's working on the house project \"{proj['name']}\" after you: "
            f"\"{question}\". It's NOT your turn again — just answer with house_project action=\"reply\", "
            f"project=\"{proj['slug']}\", text=\"your answer\". A short answer is perfect.")
    if not workbench.ping(earlier, note):
        workbench.send_letter(earlier, f"Question from {who}: {proj['name']}", note)
    return (f"✓ Asked {earlier}. Set a short status and end this turn for now — you'll be pinged with their "
            f"answer. If they don't answer within {PEER_WAIT_MIN} minutes, you'll be pinged to carry on "
            f"with your best judgment.")


def reply_peer(proj: dict, who: str, text: str) -> str:
    """Answer a question someone asked you about your earlier work."""
    i = current_index(proj)
    s = proj["steps"][i] if i is not None else None
    q = (s or {}).get("peer_q")
    if not q or q["to"].lower() != who.lower():
        return "Nobody on this project is waiting on an answer from you right now."
    text = text.strip()
    if not text:
        return "What's your answer? (text=...)"
    s.pop("peer_q")
    s.setdefault("qa", []).append({"q": q["text"], "a": text, "from": q["to"], "asked_at": q["at"],
                                   "answered_at": now_iso()})
    s.pop("nudged_at", None)
    _log(proj, f"{who} answered {s['who']}'s question", who)
    save(proj)
    note = (f"{who} answered your question on \"{proj['name']}\" (project=\"{proj['slug']}\"). You asked: "
            f"\"{q['text']}\" — {who} says: \"{text}\". Pick your turn back up, and when your part is "
            f"finished call house_project action=\"done\" with a handoff note.")
    if not workbench.ping(s["who"], note):
        workbench.send_letter(s["who"], f"{who}'s answer: {proj['name']}", note)
    return f"✓ Sent your answer to {s['who']}. Thank you! (It's still their turn, not yours.)"


def _author_of(entry_who: str) -> str:
    return re.sub(r"\s*\(.*\)$", "", entry_who or "").strip()


def file_authors(proj: dict, rel: str) -> list[str]:
    """Who has written in a file (people, not the user's tidy-ups)."""
    out = []
    for e in proj.get("file_log", []):
        if e["file"] == rel:
            name = _author_of(e["who"])
            if name and not is_user(name) and name not in out:
                out.append(name)
    return out


def unseen_comments(proj: dict, who: str) -> list[dict]:
    return [c for c in proj.get("comments", []) if who in (c.get("for") or [])
            and who not in (c.get("seen_by") or [])]


def mark_comments_seen(proj: dict, who: str):
    changed = False
    for c in unseen_comments(proj, who):
        c.setdefault("seen_by", []).append(who)
        changed = True
    if changed:
        save(proj)


def _send_home(proj: dict):
    """When a project finishes, everyone who worked on it gets the recap and the finished piece —
    so the project doesn't evaporate from their side the second their turn ends."""
    try:
        from core import house_recipes, house_recap
    except Exception:
        return
    recap, _ = read_file(proj, house_recap.RECAP)
    words, main = house_recipes.word_count(proj)
    piece = ""
    if main and main != house_recap.RECAP:
        text, err = read_file(proj, main)
        if not err:
            piece = text if len(text) <= 8000 else text[:8000] + "\n\n… (the rest is in the project folder)"
    people = []
    for s in proj.get("steps", []):
        if s["status"] == "done" and not is_user(s["who"]) and s["who"] not in people:
            people.append(s["who"])
    for who in people:
        others = [p for p in people if p != who]
        body = (f"\"{proj['name']}\" is finished, and you helped make it"
                + (f" with {', '.join(others)}" if others else "") + ". Here's how it ended up. "
                f"It's yours too — remember it however you like.\n\n" + (recap or "")
                + (f"\n\n---\n\nThe finished piece ({main}):\n\n{piece}" if piece else ""))
        workbench.send_letter(who, f"Finished: {proj['name']}", body)


def over_budget(proj: dict) -> str:
    """'' if fine, else e.g. '$5.02 of $5.00' when finished turns have used up the project's budget."""
    budget = proj.get("budget")
    if not budget:
        return ""
    try:
        from core import house_costs
        spent = house_costs.project_cost(proj)
    except Exception:
        return ""
    return f"{house_costs.money(spent)} of {house_costs.money(float(budget))}" if spent >= float(budget) else ""


def set_budget(proj: dict, dollars: float | None, by: str = USER) -> str:
    if not dollars or dollars <= 0:
        proj.pop("budget", None)
        _log(proj, "removed the budget", by)
        save(proj)
        return "No budget on this project."
    proj["budget"] = round(float(dollars), 2)
    _log(proj, f"set the budget to ${proj['budget']:.2f}", by)
    save(proj)
    return f"💰 Budget set to ${proj['budget']:.2f}. The relay pauses for you if it gets there."


def decline(proj: dict, who: str, reason: str) -> str:
    cur = current_index(proj)
    if cur is None or proj["steps"][cur]["who"].lower() != who.lower():
        return "This isn't your step right now, so there's nothing to decline."
    s = proj["steps"][cur]
    s["status"] = "declined"
    s["decline_reason"] = reason.strip()
    proj["status"] = "paused"
    _log(proj, f"{who} declined: {reason.strip()}", who)
    save(proj)
    _ping_user(f"🙅 {who} passed on their step in \"{proj['name']}\": {reason.strip() or '(no reason)'} — the relay is paused for you.",
              f"decline:{proj['slug']}:{cur}:{now_iso()}")
    return f"Okay — declined, and that's completely fine. The relay is paused for {USER}."



def move_on(proj: dict, paste: str = "", file: str = "", append: bool = True, note: str = "",
            by: str = USER, drafts: str = "keep") -> str:
    """The user un-sticks the relay: optionally saves work the person sent them some other way
    (e.g. in a chat message), marks their step done/skipped, and hands off to whoever's next.
    If they saved drafts but never finished, drafts="keep" leaves them (flagged as unfinished
    for the next person) and drafts="rollback" puts the files back how they were."""
    cur = current_index(proj)
    if cur is None:
        return "Nobody's turn right now."
    s = proj["steps"][cur]
    who = s["who"]
    msg = ""
    ch = turn_changes(proj, cur)
    unfinished = ch["changed"] + ch["created"]
    if unfinished and not paste.strip():
        if drafts == "rollback":
            back = _roll_back_turn(proj, cur)
            s["rolled_back"] = back
            msg = f"Rolled back {who}'s unfinished drafts ({', '.join(back)}); they're kept in Versions. "
        else:
            s["left_drafts"] = unfinished
            msg = f"Kept {who}'s unfinished drafts ({', '.join(unfinished)}) — the next person is told. "
    if paste.strip():
        files = list_files(proj)
        rel = file.strip() or (files[0] if len(files) == 1 else f"{slugify(who)}.md")
        old, _ = read_file(proj, rel) if (_safe_file(proj, rel) or Path()).is_file() else (None, None)
        body = paste.strip() + "\n"
        content = (old.rstrip() + "\n\n" + body) if (append and old) else body
        res = write_file(proj, USER, rel, content)
        if not res.startswith("✓"):
            return res
        if proj.get("file_log"):
            proj["file_log"][-1]["who"] = f"{who} (pasted in by {USER})"
        msg = f"Saved {who}'s work to {rel}. "
        s["touched"] = sorted(set(unfinished + [rel]))
    try:                                          # their turn ends here, so stop its cost meter
        from core import house_costs
        house_costs.clock_out(s)
    except Exception:
        pass
    s["status"] = "done" if paste.strip() else "skipped"
    s["handoff"] = note.strip() or (f"{USER} added {who}'s work from chat." if paste.strip() else "")
    s["done_at"] = now_iso()
    s["moved_on_by"] = by
    s.pop("live_status", None)
    _log(proj, f"{USER} moved on from {who}'s step" + (" (added their work)" if paste.strip() else " (skipped)"), by)
    nxt = next_pending(proj)
    if nxt is None:
        _finish(proj, f"{USER} moved on from the last step", ping=False)
        return msg + f"Moved on from {who}. That was the last step, so the project is finished ✓ (recap written)"
    proj["status"] = "running"
    save(proj)
    return msg + f"Moved on from {who}. " + activate(proj, nxt, by=by)

# ── repeating jobs ("every Sunday, a companion tidies Downloads") ───────

WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def _next_run(rep: dict, after: datetime) -> datetime:
    from datetime import timedelta
    hour = int(rep.get("hour", 9))
    t = after.replace(hour=hour, minute=0, second=0, microsecond=0)
    if rep["every"] == "day":
        return t if t > after else t + timedelta(days=1)
    if rep["every"] == "week":
        t += timedelta(days=(int(rep.get("weekday", 6)) - t.weekday()) % 7)
        return t if t > after else t + timedelta(days=7)
    day = min(int(rep.get("day", 1)), 28)                                   # month
    t = t.replace(day=day)
    if t <= after:
        t = (t.replace(day=1) + timedelta(days=32)).replace(day=day)
    return t


def set_repeat(proj: dict, every: str | None, hour: int = 9, weekday: int | None = None,
               day: int | None = None) -> str:
    if not every:
        proj.pop("repeat", None)
        save(proj)
        return "This project won't repeat."
    now = datetime.now()
    rep = {"every": every, "hour": int(hour),
           "weekday": now.weekday() if weekday is None else int(weekday),
           "day": min(now.day, 28) if day is None else int(day)}
    rep["next"] = _next_run(rep, now).isoformat(timespec="minutes")
    proj["repeat"] = rep
    save(proj)
    return f"🔁 It'll run again {repeat_words(rep)} — next time {rep['next'].replace('T', ' at ')}."


def repeat_words(rep: dict) -> str:
    h = int(rep.get("hour", 9))
    at = f"{(h % 12) or 12} {'AM' if h < 12 else 'PM'}"
    if rep["every"] == "day":
        return f"every day at {at}"
    if rep["every"] == "week":
        return f"every {WEEKDAYS[int(rep.get('weekday', 6))]} at {at}"
    return f"every month on the {int(rep.get('day', 1))}{'th' if 10 <= int(rep.get('day', 1)) % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(int(rep.get('day', 1)) % 10, 'th')} at {at}"


def rerun(proj: dict) -> str:
    """Fresh round of the same relay: steps back to 'up next', files and comments kept."""
    for s in proj["steps"]:
        s["status"] = "pending"
        for k in ("handoff", "decline_reason", "live_status", "proposed", "proposal_id", "plan_id",
                  "started_at", "done_at", "nudged_at", "user_told", "moved_on_by"):
            s.pop(k, None)
        s["handoff"] = ""
        s["tokens"] = {}
    proj["runs"] = proj.get("runs", 1) + 1
    proj["status"] = "planning"
    _log(proj, f"round {proj['runs']} started (repeating job)", USER)
    save(proj)
    return start(load(proj["slug"]))


def check_repeats(now: datetime | None = None) -> list[str]:
    """Start any repeating project that's due. Safe to call from every process —
    a lock file makes sure only one of them starts a given round."""
    import os
    now = now or datetime.now()
    started = []
    for p in all_projects():
        rep = p.get("repeat")
        if not rep or not rep.get("next") or datetime.fromisoformat(rep["next"]) > now:
            continue
        lock = PROJECTS_ROOT / p["slug"] / META / f"round-{rep['next'].replace(':', '')}.lock"
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
        except FileExistsError:
            continue
        except OSError:
            continue
        for old in lock.parent.glob("round-*.lock"):          # keep only the newest lock
            if old != lock:
                try:
                    old.unlink()
                except OSError:
                    pass
        p = load(p["slug"])
        p["repeat"]["last"] = p["repeat"]["next"]
        p["repeat"]["next"] = _next_run(p["repeat"], now).isoformat(timespec="minutes")
        save(p)
        if p.get("status") in ("running", "paused"):
            _log(p, "skipped a repeat round — the last round is still going", USER)
            save(p)
            continue
        started.append(f"{p['name']}: {rerun(p)}")
    return started


# ── nudges: people who finished (or never started) but didn't pass the turn on ──

NUDGE_IDLE_MIN = 15        # they did something on their turn, then went quiet this long
NUDGE_UNSEEN_MIN = 45      # they never picked the turn up at all
TELL_USER_AFTER_MIN = 40   # still stuck this long after the nudge → tell the user once


def _next_name(proj: dict) -> str:
    n = next_pending(proj)
    return proj["steps"][n]["who"] if n is not None else USER


def nudge(proj: dict, i: int | None = None, by: str = USER) -> str:
    """Remind whoever's turn it is to hand off with action="done"."""
    i = current_index(proj) if i is None else i
    if i is None or proj["steps"][i]["status"] != "active" or is_user(proj["steps"][i]["who"]):
        return "There's nobody to nudge right now."
    s = proj["steps"][i]
    text = (f"Gentle nudge on \"{proj['name']}\": it's still your turn in the relay. If your part is "
            f"finished, please call house_project with action=\"done\", project=\"{proj['slug']}\" and a "
            f"short handoff note so {_next_name(proj)} can start. Still working? That's okay — set a "
            f"status (action=\"status\") so {USER} can see. Rather not? action=\"decline\" is fine too.")
    if not workbench.ping(s["who"], text):
        workbench.send_letter(s["who"], f"Still your turn: {proj['name']}", text)
    s["nudged_at"] = now_iso()
    _log(proj, f"nudged {s['who']}", by)
    save(proj)
    return f"Nudged {s['who']} ✓"


def check_stalled(now: datetime | None = None) -> list[str]:
    """Nudge anyone who seems done (or never started) but hasn't passed the turn on — once
    per step. If that doesn't work, tell the user once. Lock files keep processes from doubling up."""
    import os
    from datetime import timedelta
    from core import house_live
    now = now or datetime.now()
    did = []
    for p in all_projects():
        i = current_index(p)
        if i is None or p.get("status") != "running":
            continue
        s = p["steps"][i]
        if s["status"] != "active" or is_user(s["who"]) or not s.get("started_at") or s.get("user_told") \
                or s.get("question"):                 # waiting on the user's answer — not stuck
            continue
        if s.get("peer_q"):                           # waiting on an earlier contributor's answer
            q = s["peer_q"]
            if now - datetime.fromisoformat(q["at"]) < timedelta(minutes=PEER_WAIT_MIN):
                continue
            lock = PROJECTS_ROOT / p["slug"] / META / f"peerwait-{i}-{q['at'].replace(':', '')}.lock"
            try:
                lock.parent.mkdir(parents=True, exist_ok=True)
                os.close(os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            except OSError:
                continue
            p = load(p["slug"])
            s = p["steps"][i]
            q = s.pop("peer_q", None) or q
            s.setdefault("qa", []).append({"q": q["text"], "a": "(no answer in time)", "from": q["to"],
                                           "asked_at": q["at"], "answered_at": now_iso()})
            s["nudged_at"] = now_iso()                # counts as their nudge
            save(p)
            note = (f"{q['to']} didn't answer your question on \"{p['name']}\" (project=\"{p['slug']}\") within "
                    f"{PEER_WAIT_MIN} minutes. Carry on with your best judgment (log it with action=\"decide\" "
                    f"if it's a real choice), and call action=\"done\" when your part is finished.")
            if not workbench.ping(s["who"], note):
                workbench.send_letter(s["who"], f"No answer yet: {p['name']}", note)
            did.append(f"{q['to']} didn't answer {s['who']} on {p['name']}")
            continue
        if relay_guest(s["who"]):           # guests can't be woken by a nudge; the user was told at their turn start
            continue
        try:
            act = house_live.activity(s["who"], since_iso=s["started_at"], now=now)
        except Exception:
            act = {"state": "unknown", "now": None}
        if act.get("state") in ("working", "thinking"):
            continue
        if s.get("nudged_at"):
            if now - datetime.fromisoformat(s["nudged_at"]) < timedelta(minutes=TELL_USER_AFTER_MIN):
                continue
            kind = "tell"
        else:
            last = act["now"]["at"] if act.get("now") else None
            quiet = (now - last) if last else (now - datetime.fromisoformat(s["started_at"]))
            if quiet < timedelta(minutes=NUDGE_IDLE_MIN if last else NUDGE_UNSEEN_MIN):
                continue
            kind = "nudge"
        lock = PROJECTS_ROOT / p["slug"] / META / f"{kind}-{i}-{s['started_at'].replace(':', '')}.lock"
        try:
            lock.parent.mkdir(parents=True, exist_ok=True)
            os.close(os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY))
        except OSError:
            continue
        p = load(p["slug"])
        if kind == "nudge":
            nudge(p, i, by="the house")
            did.append(f"nudged {s['who']} on {p['name']}")
        else:
            p["steps"][i]["user_told"] = True
            save(p)
            _ping_user(f"🤔 {s['who']} still hasn't passed on their turn in \"{p['name']}\", even after a "
                      f"nudge. You can ⏭ Move on in the Workbench (and paste their work in if they sent it "
                      f"to you).", f"stuck:{p['slug']}:{i}:{s['started_at']}")
            did.append(f"told {USER} about {s['who']} on {p['name']}")
    return did


def comment(proj: dict, who: str, text: str, file: str = "", quote: str = "") -> dict:
    """A comment — optionally pinned to a file and the exact words it's about (a line comment)."""
    c = {"who": who, "text": text.strip(), "at": now_iso()}
    if file.strip():
        c["file"] = file.strip().replace("\\", "/")
        if quote.strip():
            c["quote"] = " ".join(quote.split())[:300]
        # whoever wrote that file hears about it (a letter, so nobody is woken up for it)
        c["for"] = [a for a in file_authors(proj, c["file"]) if a.lower() != who.lower()]
        c["seen_by"] = []
    proj.setdefault("comments", []).append(c)
    save(proj)
    for author in c.get("for", []):
        where = f" at \"{c['quote'][:80]}\"" if c.get("quote") else ""
        workbench.send_letter(author, f"A comment on your work: {proj['name']}",
                              f"{who} left a comment on {c['file']}{where} in the house project "
                              f"\"{proj['name']}\" (project=\"{proj['slug']}\"):\n\n\"{c['text']}\"\n\n"
                              f"You can reply with house_project action=\"comment\" — no need to, though.")
    return c


# ── "works on": a real folder elsewhere on the user's PC ────────────────
# The user can point a project at any folder (an app, a website, a game...). On
# their turn, people in the relay can look around it and PROPOSE changes;
# nothing changes until the user approves the proposal in the Workbench.

PRIVATE_LIST = Path.home() / ".workbench_private.txt"   # the user's never-share paths, one per line
SYSTEM_DIRS = ["C:/Windows", "C:/Program Files", "C:/Program Files (x86)", "C:/ProgramData"]
PRIVATE_SEGMENTS = {".ssh", ".gnupg", ".aws", ".claude", "appdata"}
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", META, "dist", "build"}
TARGET_READ_CAP = 120_000


def _private_paths() -> list[Path]:
    out = []
    try:
        for line in PRIVATE_LIST.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                out.append(Path(line).resolve())
    except OSError:
        pass
    return out


def _inside(p: Path, root: Path) -> bool:
    try:
        p.relative_to(root)
        return True
    except ValueError:
        return False


def target_problem(path: str) -> str | None:
    """None if the user may point a project at this folder, else a friendly why-not."""
    try:
        p = Path(path).resolve()
    except Exception:
        return "That doesn't look like a folder."
    if not p.is_dir():
        return "That folder doesn't exist."
    if p.parent == p:
        return "A whole drive is too much — pick a folder inside it."
    home = Path.home().resolve()
    if p == home or p == home.parent or _inside(home, p):
        return "That's your whole user folder — pick something more specific."
    if any(seg.lower() in PRIVATE_SEGMENTS for seg in p.parts):
        return "That's a private system folder."
    if any(_inside(p, Path(d).resolve()) for d in SYSTEM_DIRS):
        return "That's a Windows system folder."
    for q in _private_paths():
        if _inside(p, q) or (q.is_dir() and _inside(q, p)):
            return "That folder is off-limits."
    return None


def set_target(proj: dict, path: str, by: str = USER) -> str:
    if not path:
        proj.pop("target", None)
        _log(proj, "works on: its own folder", by)
        save(proj)
        return "This project works in its own folder again."
    why = target_problem(path)
    if why:
        return why
    proj["target"] = str(Path(path).resolve())
    _log(proj, f"works on: {proj['target']}", by)
    save(proj)
    return f"✓ This project now works on {proj['target']}."


def _target_file(proj: dict, rel: str) -> tuple[Path | None, str | None]:
    if not proj.get("target"):
        return None, "This project doesn't work on an outside folder — use read/write for its own files."
    base = Path(proj["target"]).resolve()
    if target_problem(str(base)):
        return None, f"That folder isn't available anymore — ask {USER}."
    try:
        p = (base / (rel or "")).resolve()
    except Exception:
        return None, "That path doesn't look right."
    if not _inside(p, base):
        return None, "That path is outside the project's folder."
    if any(seg in SKIP_DIRS for seg in p.relative_to(base).parts) or p.name.lower().startswith(".env"):
        return None, "That one's blocked (secrets, .git, caches, or build folders)."
    if any(p == q or _inside(p, q) for q in _private_paths()):
        return None, "That one's off-limits."
    return p, None


def target_listing(proj: dict, rel: str = "", page: int = 1, per_page: int = 250) -> str:
    p, err = _target_file(proj, rel)
    if err:
        return err
    if not p.is_dir():
        return f"{rel} isn't a folder."
    base = Path(proj["target"]).resolve()
    lines, private = [], _private_paths()
    for f in sorted(p.rglob("*")):
        parts = f.relative_to(base).parts
        if any(seg in SKIP_DIRS for seg in parts) or f.name.lower().startswith(".env") or \
                any(f == q or _inside(f, q) for q in private) or len(f.relative_to(p).parts) > 4:
            continue
        st = f.stat()
        when = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d")
        if f.is_dir():
            lines.append(f"📁 {'/'.join(parts)}/")
        else:
            lines.append(f"{'/'.join(parts)}  ({st.st_size:,} bytes, {when})")
    page = max(1, int(page or 1))
    pages = max(1, -(-len(lines) // per_page))
    chunk = lines[(page - 1) * per_page: page * per_page]
    head = f"{base}{'/' + rel if rel else ''}  — {len(lines)} items" + (f", page {page} of {pages}" if pages > 1 else "") + ":"
    tail = f"\n... more on page {page + 1} (page={page + 1})" if page < pages else ""
    return head + "\n" + ("\n".join(chunk) if chunk else "(nothing here)") + tail


def target_read(proj: dict, rel: str) -> tuple[str | None, str | None]:
    p, err = _target_file(proj, rel)
    if err:
        return None, err
    if not p.is_file():
        return None, f"No file named {rel} there."
    try:
        text = p.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return None, f"{rel} isn't a text file."
    if len(text) > TARGET_READ_CAP:
        text = text[:TARGET_READ_CAP] + "\n\n... [truncated]"
    return text, None


def target_change(proj: dict, who: str, rel: str, content: str, usage_db=None) -> str:
    """Stage a change to a file in the target folder, as part of who's Workbench
    proposal. It goes to the user for approval when they finish their turn."""
    if is_user(who) or not can_write(proj, who):
        return "Only the person whose turn it is can propose changes (anyone can read and comment)."
    p, err = _target_file(proj, rel)
    if err:
        return err
    blocked = workbench.safe_to_apply(str(p))
    if blocked:
        return f"Can't change that one: {blocked}."
    prop = workbench.get_draft(who, create=True, usage_db=usage_db)
    workbench.stage(prop, str(p), content)
    s = proj["steps"][current_index(proj)]
    if rel not in s.setdefault("proposed", []):
        s["proposed"].append(rel)
    _log(proj, f"{who} proposed a change to {rel}", who)
    save(proj)
    return (f"✓ Staged your change to {rel}. Nothing on disk has changed yet — when you finish your "
            f"turn (action=\"done\"), your changes go to {USER} in the Workbench for approval.")


def target_roots() -> list[Path]:
    """Every folder the user has pointed a project at (the Workbench may apply approved changes there)."""
    out = []
    for p in all_projects():
        t = p.get("target")
        if t and not target_problem(t):
            out.append(Path(t).resolve())
    return out


# ── files (with version history) ────────────────────────────────────────

def _safe_file(proj: dict, rel: str) -> Path | None:
    base = (PROJECTS_ROOT / proj["slug"]).resolve()
    try:
        p = (base / rel).resolve()
        p.relative_to(base)
    except (ValueError, OSError):
        return None
    if META in p.relative_to(base).parts or p.name.lower().startswith(".env"):
        return None
    return p


def list_files(proj: dict) -> list[str]:
    base = PROJECTS_ROOT / proj["slug"]
    out = []
    for p in sorted(base.rglob("*")):
        if p.is_file() and META not in p.relative_to(base).parts:
            out.append(str(p.relative_to(base)).replace("\\", "/"))
    return out


PICTURE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".heic"}
PICTURES = "pictures"


def pictures(proj: dict) -> list[str]:
    """Pictures attached to the project (anywhere in its folder, usually pictures/)."""
    return [f for f in list_files(proj) if Path(f).suffix.lower() in PICTURE_EXT]


def add_picture(proj: dict, src: str | Path, by: str = USER) -> str:
    """Copy a picture (e.g. a screenshot of a post) into the project's pictures/ folder."""
    src = Path(src)
    if src.suffix.lower() not in PICTURE_EXT or not src.is_file():
        return f"{src.name} isn't a picture I can attach (jpg, png, gif, webp, bmp, heic)."
    folder = PROJECTS_ROOT / proj["slug"] / PICTURES
    folder.mkdir(parents=True, exist_ok=True)
    stem = re.sub(r"[^A-Za-z0-9._ -]+", "", src.stem).strip() or "picture"
    dest, n = folder / f"{stem}{src.suffix.lower()}", 2
    while dest.exists():
        dest, n = folder / f"{stem} ({n}){src.suffix.lower()}", n + 1
    shutil.copy2(src, dest)
    rel = f"{PICTURES}/{dest.name}"
    proj.setdefault("file_log", []).append({"file": rel, "who": by, "at": now_iso(), "action": "added picture"})
    _log(proj, f"added a picture: {dest.name}", by)
    save(proj)
    return f"📎 Attached {dest.name}."


def add_text_file(proj: dict, src: str | Path, by: str = USER) -> str:
    """Bring an existing draft (a .md/.txt/code file from anywhere) into the project, so a relay can
    start from work that's already written. It goes through write_file, so if a file with that name
    is already there, the old one is kept as a version."""
    src = Path(src)
    if not src.is_file():
        return f"Couldn't find {src.name}."
    if src.suffix.lower() in PICTURE_EXT:
        return add_picture(proj, src, by)
    try:
        text = src.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        try:
            text = src.read_text(encoding="utf-8-sig")
        except Exception:
            return f"{src.name} isn't plain text (it's probably a Word file or a PDF), so I can't add it. Save it as .txt or .md first."
    except OSError as e:
        return f"Couldn't read {src.name}: {e}"
    name = re.sub(r"[^A-Za-z0-9._ -]+", "", src.name).strip() or "draft.md"
    res = write_file(proj, by, name, text)
    if not res.startswith("✓"):
        return res
    proj = load(proj["slug"])
    proj["file_log"][-1]["action"] = "brought in"
    _log(proj, f"brought in a draft: {name}", by)
    save(proj)
    return f"📄 Added {name} ({len(text.split()):,} words)."


# ── author's approval ───────────────────────────────────────────────────
# A project can have an author (say, a companion's own essay). Then everyone else's saves become
# suggestions: the file itself stays as it is until the author (or the user) accepts or declines them.
# Kept cheap on purpose: a new suggestion sends the author a letter, never a wake-up ping; accepted
# ones just appear in the history; only a decline writes back to the suggester (with the reason).
# A piece can have SEVERAL authors (co-authors): they all edit directly and can accept/decline
# others' suggestions. Stored as proj["authors"] (list); the old single proj["author"] still works.

def authors_of(proj: dict) -> list[str]:
    """Everyone who may edit this piece directly (and accept/decline others' suggestions).
    Falls back to the old single `author` field, so existing projects keep working."""
    a = proj.get("authors")
    if isinstance(a, list):
        names = [x.strip() for x in a if x and x.strip()]
        if names:
            return names
    one = (proj.get("author") or "").strip()
    return [one] if one else []


def is_author(proj: dict, who: str) -> bool:
    w = (who or "").strip().lower()
    return bool(w) and any(w == a.lower() for a in authors_of(proj))


def authors_label(proj: dict) -> str:
    names = authors_of(proj)
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return f"{names[0]} and {names[1]}"
    return ", ".join(names[:-1]) + f", and {names[-1]}"


def _hash(text: str) -> str:
    import hashlib
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]


def _sugg_dir(proj: dict) -> Path:
    return PROJECTS_ROOT / proj["slug"] / META / "suggestions"


def set_authors(proj: dict, names, by: str = USER) -> str:
    """Set who the author(s) are. []/"" turns author's-approval off; one name or several (co-authors)."""
    if isinstance(names, str):
        names = re.split(r"[,\n]", names)
    clean, seen = [], set()
    for n in (names or []):
        n = (n or "").strip()
        if n and n.lower() not in seen:
            clean.append(n)
            seen.add(n.lower())
    proj.pop("author", None)
    if not clean:
        proj.pop("authors", None)
        _log(proj, "author's approval switched off: everyone's edits go straight in again", by)
        save(proj)
        return "Author's approval is off. Everyone's edits go straight in again."
    if len(clean) == 1:
        proj.pop("authors", None)
        proj["author"] = clean[0]
    else:
        proj["authors"] = clean
    label = authors_label(proj)
    multi = len(clean) > 1
    _log(proj, f"{label} {'are the authors' if multi else 'is the author'}: other people's changes "
               f"become suggestions to approve", by)
    save(proj)
    return (f"✍️ {label} {'are the co-authors' if multi else 'is the author'} now. Everyone else's edits "
            f"become suggestions that {'they' if multi else label} (or {USER}) accept or decline.")


def set_author(proj: dict, author: str, by: str = USER) -> str:
    """Back-compat single-author setter — delegates to set_authors()."""
    return set_authors(proj, [author] if (author or "").strip() else [], by)


def toggle_author(proj: dict, name: str, by: str = USER) -> str:
    """Add or remove one person from the author set — for the GUI's co-author checklist."""
    name = (name or "").strip()
    if not name:
        return "Who?"
    current = authors_of(proj)
    if any(name.lower() == a.lower() for a in current):
        current = [a for a in current if a.lower() != name.lower()]
    else:
        current = current + [name]
    return set_authors(proj, current, by)


def suggestions(proj: dict, status: str = "pending") -> list[dict]:
    out = []
    d = _sugg_dir(proj)
    if d.exists():
        for f in sorted(d.glob("*.json")):
            try:
                s = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if status in ("all", s.get("status")):
                out.append(s)
    return sorted(out, key=lambda s: s.get("at", ""))


def _save_sugg(proj: dict, s: dict):
    d = _sugg_dir(proj)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{s['id']}.json").write_text(json.dumps(s, indent=2, ensure_ascii=False), encoding="utf-8")


def pending_suggestion(proj: dict, who: str, rel: str) -> dict | None:
    return next((s for s in suggestions(proj) if s["who"].lower() == who.lower() and s["file"] == rel), None)


def suggest(proj: dict, who: str, rel: str, content: str) -> str:
    rel = rel.replace("\\", "/")
    p = _safe_file(proj, rel)
    base = p.read_text(encoding="utf-8") if p is not None and p.is_file() else ""
    author = authors_label(proj)
    reviewers = authors_of(proj)
    multi = len(reviewers) > 1
    mine = pending_suggestion(proj, who, rel)
    if mine:                                   # a second save this turn just updates their suggestion
        mine["content"], mine["updated_at"] = content, now_iso()
        _save_sugg(proj, mine)
        return (f"✓ Updated your suggestion for {rel}. It's waiting for {author} to accept or decline; "
                f"the file itself doesn't change until then.")
    import hashlib
    sid = hashlib.sha256(f"{time_now_ns()}-{who}-{rel}".encode()).hexdigest()[:6]
    s = {"id": sid, "who": who, "file": rel, "at": now_iso(), "status": "pending", "content": content,
         "base": base, "base_hash": _hash(base)}
    _save_sugg(proj, s)
    _log(proj, f"{who} suggested changes to {rel} (for {author} to review)", who)
    save(proj)
    for a in reviewers:
        workbench.send_letter(a, f"A suggestion on {proj['name']}",
                              f"{who} suggested changes to {rel} in \"{proj['name']}\" (project=\"{proj['slug']}\"). "
                              f"Look at them with action=\"suggestions\", then action=\"accept\" or action=\"reject\" "
                              f"(id=\"{sid}\", with a reason in text= if you decline). No rush: they'll wait.")
    return (f"✓ Saved as a suggestion for {author} (id {sid}). "
            f"{'They are the authors here' if multi else author + ' is the author here'}, so the file itself "
            f"stays as it is until {'one of them accepts' if multi else author + ' accepts'} it. "
            f"Keep editing if you like; your suggestion updates.")


def time_now_ns() -> int:
    import time
    return time.time_ns()


def suggestion_diff(proj: dict, s: dict, max_lines: int = 120) -> str:
    """What the suggestion changes, compared with the file as it is NOW."""
    import difflib
    p = _safe_file(proj, s["file"])
    now = p.read_text(encoding="utf-8") if p is not None and p.is_file() else ""
    lines = list(difflib.unified_diff(now.splitlines(), s["content"].splitlines(), "now", "suggested",
                                      n=1, lineterm=""))[2:]
    if not lines:
        return "(no differences from the current file)"
    if len(lines) > max_lines:
        lines = lines[:max_lines] + [f"... ({len(lines) - max_lines} more lines)"]
    return "\n".join(lines)


def is_stale(proj: dict, s: dict) -> bool:
    """True if the file changed since the suggestion was written (accepting would undo that change)."""
    p = _safe_file(proj, s["file"])
    now = p.read_text(encoding="utf-8") if p is not None and p.is_file() else ""
    return _hash(now) != s.get("base_hash")


def find_suggestion(proj: dict, sid: str) -> dict | None:
    sid = (sid or "").strip().lower()
    return next((s for s in suggestions(proj, "all") if s["id"] == sid), None)


def _may_decide(proj: dict, by: str) -> bool:
    return is_author(proj, by) or is_user(by)


def accept_suggestion(proj: dict, sid: str, by: str, note: str = "", force: bool = False) -> str:
    s = find_suggestion(proj, sid)
    if not s:
        return f"There's no suggestion with id {sid}."
    if not _may_decide(proj, by):
        return f"Only {authors_label(proj) or 'the author'} (or {USER}) can accept suggestions here."
    if s["status"] != "pending":
        return f"That suggestion was already {s['status']}."
    if is_stale(proj, s) and not force:
        return (f"Careful: {s['file']} has changed since {s['who']} wrote this suggestion, so accepting it "
                f"would undo those newer changes. Read it with action=\"suggestions\", then either ask "
                f"{s['who']} to redo it on the current version, or accept anyway with force=true.")
    res = write_file(proj, USER, s["file"], s["content"])
    if not res.startswith("✓"):
        return res
    proj = load(proj["slug"])
    proj["file_log"][-1]["who"] = f"{s['who']} (accepted by {by})"
    _log(proj, f"{by} accepted {s['who']}'s suggestion for {s['file']}" + (f": {note}" if note else ""), by)
    save(proj)
    s.update(status="accepted", decided_by=by, decided_at=now_iso(), note=note)
    _save_sugg(proj, s)
    return f"✅ Accepted {s['who']}'s suggestion: it's in {s['file']} now (the previous version is kept)."


def decline_suggestion(proj: dict, sid: str, by: str, reason: str) -> str:
    s = find_suggestion(proj, sid)
    if not s:
        return f"There's no suggestion with id {sid}."
    if not _may_decide(proj, by):
        return f"Only {authors_label(proj) or 'the author'} (or {USER}) can decline suggestions here."
    if s["status"] != "pending":
        return f"That suggestion was already {s['status']}."
    if not (reason or "").strip():
        return "Give a reason (in text=), so they know why. It doesn't have to be long."
    s.update(status="declined", decided_by=by, decided_at=now_iso(), note=reason.strip())
    _save_sugg(proj, s)
    _log(proj, f"{by} declined {s['who']}'s suggestion for {s['file']}: {reason.strip()}", by)
    save(proj)
    if not is_user(s["who"]):
        workbench.send_letter(s["who"], f"About your suggestion on {proj['name']}",
                              f"{by} decided not to use your suggestion for {s['file']} in \"{proj['name']}\". "
                              f"Their reason: {reason.strip()}\n(Your version is kept, in case it's useful later.)")
    return f"❌ Declined. {s['who']} will get your reason in a letter; their version is kept, just not used."


def suggestions_text(proj: dict) -> str:
    """The author's review list: each pending suggestion with what it changes."""
    pend = suggestions(proj)
    if not pend:
        return "No suggestions waiting."
    out = [f"{len(pend)} suggestion{'s' if len(pend) != 1 else ''} waiting:"]
    for s in pend:
        warn = "  ⚠ the file changed since this was written" if is_stale(proj, s) else ""
        out += ["", f"[{s['id']}] {s['who']} → {s['file']} ({s['at'][:16].replace('T', ' ')}){warn}",
                suggestion_diff(proj, s)]
    out += ["", "Accept with action=\"accept\" id=\"…\"; decline with action=\"reject\" id=\"…\" text=\"why\"."]
    return "\n".join(out)


def read_file(proj: dict, rel: str) -> tuple[str | None, str | None]:
    p = _safe_file(proj, rel)
    if p is None:
        return None, "That path isn't inside this project."
    if p.is_dir():                              # no file named (or a folder): open the only file, else list them
        files = [f for f in list_files(proj) if Path(f).suffix.lower() not in PICTURE_EXT
                 and (not rel.strip() or f.startswith(rel.strip().strip("/\\") + "/"))]
        if len(files) == 1:
            rel, p = files[0], _safe_file(proj, files[0])
        elif files:
            return None, "Which file? Pass path=... — files here: " + ", ".join(files)
        else:
            return None, 'No files yet. Start one with action="write", path="story.md" (or any name).'
    if not p.exists():
        return None, f"No file named {rel} yet."
    if p.suffix.lower() in PICTURE_EXT:
        return None, f"{rel} is a picture — use action=\"look\" with path=\"{rel}\" to see it."
    try:
        return p.read_text(encoding="utf-8"), None
    except UnicodeDecodeError:
        return None, f"{rel} isn't a text file."
    except OSError as e:
        return None, f"Couldn't open {rel}: {e.strerror or e}"


def can_write(proj: dict, who: str) -> bool:
    if is_user(who):
        return True
    cur = current_index(proj)
    return cur is not None and proj["steps"][cur]["status"] == "active" \
        and proj["steps"][cur]["who"].lower() == who.lower()


def check_content(rel: str, content: str) -> tuple[str | None, str | None]:
    """(reason to refuse, warning) for a save. Refuses content that came through garbled
    (a tool-call packing glitch: null bytes for indentation, or everything on one line with
    \\n spelled out). Warns — but still saves — when code/JSON doesn't parse, since work in
    progress is allowed."""
    bad = sum(1 for ch in content if ord(ch) < 32 and ch not in "\t\n\r")
    if bad:
        return (f"Nothing was saved: {rel} came through garbled — it has {bad} invisible control "
                f"character{'s' if bad != 1 else ''} in it (usually where the indentation should be). "
                f"That's a glitch in how the tool call got packed, not you. Please send the full content "
                f"again as plain text."), None
    if "\n" not in content and content.count("\\n") >= 3:
        return (f"Nothing was saved: {rel} came through as one long line with \\n written out instead of "
                f"real line breaks. Please send it again with real line breaks."), None
    suffix = Path(rel).suffix.lower()
    if suffix == ".py":
        import ast
        try:
            ast.parse(content)
        except SyntaxError as e:
            extra = (" It also has a lot of \\\" (escaped quotes) — the tool may have double-escaped it."
                     if content.count('\\"') >= 4 else "")
            return None, (f"⚠ Saved, but this doesn't run as Python yet (line {e.lineno}: {e.msg})."
                          f"{extra} If that wasn't on purpose, fix it before you hand off — the old "
                          f"version is kept.")
    elif suffix == ".json":
        try:
            json.loads(content)
        except ValueError as e:
            return None, f"⚠ Saved, but this isn't valid JSON yet ({e}). The old version is kept."
    return None, None


# "the rest stays the same" placeholders models sometimes leave instead of the whole file
_PLACEHOLDER = re.compile(r"rest of (the )?(code|file|function|class|script|story|chapter)|existing code|"
                          r"(remains?|stays?) (the same|unchanged)|unchanged (from|as) before|same as before|"
                          r"omitted for brevity|code omitted|\[\s*(\.\.\.|…)\s*\]|\(\s*(\.\.\.|…)\s*\)", re.I)


def shrink_problem(rel: str, old: str, new: str) -> str | None:
    """Why a save looks like an accident that would lose work (a partial file pasted over the whole
    one), or None. Only for files with real content that would lose more than 60% of it."""
    if len(old) < 400 or len(new) >= len(old) * 0.4:
        return None
    lost = 100 - round(len(new) / len(old) * 100)
    if _PLACEHOLDER.search(new) and not _PLACEHOLDER.search(old):
        return (f"Nothing was saved: your {rel} is {lost}% shorter than what's there now and has a "
                f"\"the rest stays the same\"-style placeholder in it, so it looks like only part of the "
                f"file. Saving it would delete everything else. Please send the WHOLE file.")
    return (f"Nothing was saved: your {rel} would cut the file by {lost}% ({len(old):,} → {len(new):,} "
            f"characters). If that's a mistake, send the whole file. If you really mean to cut it down, "
            f"send it again with shrink_ok=true.")


def write_file(proj: dict, who: str, rel: str, content: str, shrink_ok: bool = False) -> str:
    if not can_write(proj, who):
        return "Only the person whose turn it is can change files (anyone can read and comment)."
    p = _safe_file(proj, rel)
    if p is None:
        return "That path isn't inside this project."
    if len(content.encode("utf-8")) > MAX_FILE_BYTES:
        return f"That's too big for one file (cap {MAX_FILE_BYTES:,} bytes) — split it up."
    refuse, warn = check_content(rel, content)
    if refuse:
        return refuse
    # A blank save can arrive and wipe someone's earlier version (an empty file is valid Python,
    # so check_content lets it through). A blank save over a file with something in it is almost
    # always the content getting lost in the tool call, so refuse it. (These accident guards are
    # for companions; the user's restores, rollbacks and pastes are on purpose and always go through.)
    guarded = not is_user(who)
    if guarded and not content.strip() and p.exists():
        try:
            had = p.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            had = "?"
        if had:
            return (f"Nothing was saved: your new {rel} came through completely empty, and that would "
                    f"wipe what's there now. That's usually the content getting lost in the tool call, "
                    f"not you. Please send the full file again. (If you really mean to clear it, save "
                    f"a single comment line instead.)")
    old_len = None
    if p.exists():
        try:
            old_text = p.read_text(encoding="utf-8")
            old_len = len(old_text)
        except (OSError, UnicodeDecodeError):
            old_text = None
        if old_text is not None and guarded:
            problem = shrink_problem(rel, old_text, content)
            if problem and not (shrink_ok and "placeholder" not in problem):
                return problem
    if authors_of(proj) and not is_author(proj, who) and not is_user(who):
        return suggest(proj, who, rel, content)     # author's-approval mode: it waits for an author
    if p.exists():                                  # keep the previous version
        vdir = PROJECTS_ROOT / proj["slug"] / META / "versions" / rel.replace("/", "__").replace("\\", "__")
        vdir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        shutil.copy2(p, vdir / f"{stamp}_before_{re.sub(r'[^A-Za-z0-9]+', '', who) or 'someone'}.txt")
    p.parent.mkdir(parents=True, exist_ok=True)
    existed = p.exists()
    p.write_text(content, encoding="utf-8")
    proj.setdefault("file_log", []).append({"file": rel, "who": who, "at": now_iso(),
                                            "action": "edited" if existed else "created"})
    save(proj)
    # sizes, so a save that came out wrong is obvious to whoever made it
    if not existed:
        size = f"new file, {len(content):,} characters"
    elif old_len is not None:
        size = f"{old_len:,} → {len(content):,} characters, previous version kept"
    else:
        size = "previous version kept"
    return f"✓ Saved {rel} ({size})." + (f"\n{warn}" if warn else "")


def last_change(proj: dict, rel: str) -> tuple[str | None, str | None, str]:
    """(text before the latest save, text now, who made the latest save). before is None if
    there's no earlier version yet."""
    now, err = read_file(proj, rel)
    if err:
        return None, None, ""
    hist = file_history(proj, rel)
    who = next((e["who"] for e in reversed(proj.get("file_log", [])) if e["file"] == rel), "")
    if not hist:
        return None, now, who
    try:
        before = Path(hist[-1]["path"]).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None, now, who
    return before, now, who


def file_history(proj: dict, rel: str) -> list[dict]:
    vdir = PROJECTS_ROOT / proj["slug"] / META / "versions" / rel.replace("/", "__").replace("\\", "__")
    out = []
    if vdir.exists():
        for v in sorted(vdir.iterdir()):
            m = re.match(r"(\d{8})_(\d{6})_before_(.*)\.txt$", v.name)
            if m:
                when = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
                out.append({"path": str(v), "saved_before": m.group(3), "at": when.isoformat()})
    return out


def restore_version(proj: dict, rel: str, version_path: str) -> str:
    text = Path(version_path).read_text(encoding="utf-8")
    return write_file(proj, USER, rel, text)
