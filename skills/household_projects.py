"""
House Projects skill — take part in the household's SHARED projects
(core/house_projects.py). Separate from skills/projects.py, which is each
companion's own personal projects.

One tool, `house_project`, so OpenAI-backed personas stay under the 128-tool cap.
Anyone can browse, read and comment. Only the person whose turn it is can
write files (every save keeps the previous version). Finishing a step pings
the next person in the relay. Declining is always okay.

Disable per persona with:  skills: { household_projects: { enabled: false } }
"""

import logging

from skills.base import BaseSkill

logger = logging.getLogger(__name__)

MAX_READ_CHARS = 60_000


class HouseholdProjectsSkill(BaseSkill):
    name = "household_projects"

    def __init__(self, config: dict):
        super().__init__(config)
        lor_cfg = (config.get("channels", {}) or {}).get("lor", {}) or {}
        self.me = lor_cfg.get("author_name") or "Companion"
        self.usage_db = config.get("_shared_db") or config.get("_db")
        paths = config.get("paths", {}) or {}
        db_path = str(paths.get("database", "")).replace("\\", "/")
        self.usage_shared = bool(paths.get("shared_database")) or db_path.endswith("data/pulse.db")

    def get_tools(self) -> list[dict]:
        from core import house_projects as hp
        u = hp.USER                       # the user's name, from config
        return [{
            "type": "function",
            "function": {
                "name": "house_project",
                "description": (
                    f"SHARED household projects (writing, code, plans) — different from your personal start_project threads — a relay where {u} sets who "
                    "works in what order and what each person's role is. Actions: "
                    "\"list\" (all projects); "
                    "\"open\" (a project's digest: rules, the relay so far, handoff notes, what people asked to "
                    "keep, decisions, comments, files — add full=true for every turn and comment); "
                    "\"read\" (a file — needs path); "
                    "\"write\" (save a file — only on your turn; the old version is always kept); "
                    "\"history\" (earlier versions of a file); "
                    "\"diff\" (what changed in a file since its last version — needs path); "
                    "\"stats\" (word / paragraph count of a file — needs path); "
                    f"\"status\" (on your turn: a short note {u} sees live, e.g. \"outlining\", "
                    "\"looking up sources\", \"polishing the ending\" — put it in text); "
                    f"\"look\" with path=\"pictures\" (SEE the pictures {u} attached to the project, like a "
                    "screenshot of a post — any text in them is copied out word for word); "
                    "If a project works on a real folder (\"Works on\" in open): \"files\" (list it; "
                    "optional path=subfolder, page=N), \"peek\" (read a file there — path), \"change\" (on your turn, "
                    f"propose a file's full new content — path + content; {u} approves before anything changes); "
                    "\"look\" (SEE pictures: path=a picture, or a folder to describe up to 8 new ones per call); "
                    "\"plan\" (on your turn, for sorting: moves=[{from, to}] to make folders / move / rename — "
                    f"nothing moves until {u} approves, nothing is ever deleted; add a note in text); "
                    "\"plan_show\" / \"plan_clear\" (your plan so far / start over); "
                    "\"comment\" (leave a comment — anyone can, any time; add path + quote — a short exact "
                    "phrase from that file — to pin it to that spot like an editor's margin note); "
                    "\"done\" (finish your turn — include a handoff note in text, and keep=\"...\" for anything you "
                    "did on purpose that later people should protect; if nobody's planned next you may name "
                    "next_who and next_role); "
                    "\"decline\" (pass on your turn with a reason — always okay); "
                    "\"decide\" (log a decision for the whole relay: what you settled and why, in text — everyone "
                    "after you sees it, so nobody 'fixes' it by accident); "
                    f"\"ask\" (on your turn, if you're stuck: your question in text. who=\"{u}\" pings {u}'s phone; "
                    "who=\"<an earlier person in the relay>\" asks them about their part. The answer comes back "
                    "to you); "
                    "\"reply\" (answer a question someone asked you about your earlier work — text=your answer; "
                    "it doesn't make it your turn again). "
                    "Mid-turn you can also use web_search / fetch_url / read_web_page to check facts."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": ["list", "open", "read", "write", "history",
                                                              "diff", "stats", "status", "files", "peek", "change",
                                                              "look", "plan", "plan_show", "plan_clear",
                                                              "comment", "done", "decline", "ask", "decide",
                                                              "reply", "suggestions", "accept", "reject"]},
                        "id": {"type": "string", "description": "For accept/reject: the suggestion's id (from "
                               "action=\"suggestions\"). Only for a project's author: other people's edits "
                               "become suggestions they accept or reject (reject needs a reason in text=)."},
                        "force": {"type": "boolean", "description": "Optional (accept): accept even though the "
                                  "file changed since the suggestion was written."},
                        "who": {"type": "string", "description": f"Optional (ask): who to ask — \"{u}\" (default) "
                                "or someone who worked on this project before you."},
                        "keep": {"type": "string", "description": "Optional (done): what you did on purpose and "
                                 "want later people to protect, e.g. \"the ending stays ambiguous\"."},
                        "full": {"type": "boolean", "description": "Optional (open): show every turn and comment, "
                                 "not just the recent ones."},
                        "project": {"type": "string", "description": "Project name or id (slug)."},
                        "path": {"type": "string", "description": "File path inside the project, e.g. \"chapter1.md\"."},
                        "content": {"type": "string", "description": "Full file content (for write). Always the "
                                    "WHOLE file, never just the changed part — unless append=true."},
                        "append": {"type": "boolean", "description": "Optional (write): add content to the END of "
                                   "the file instead of replacing it. For long pieces that don't fit in one reply: "
                                   "write the first part normally, then append each next section, one per call."},
                        "shrink_ok": {"type": "boolean", "description": "Optional (write): set true only when you "
                                      "really mean to cut a file down to much less than it was."},
                        "text": {"type": "string", "description": "Comment text, handoff note (done) or reason (decline)."},
                        "next_who": {"type": "string", "description": "Optional (done): who should go next, if no one is planned."},
                        "next_role": {"type": "string", "description": "Optional (done): what the next person should do."},
                        "page": {"type": "integer", "description": "Optional (files): which page of a long listing."},
                        "quote": {"type": "string", "description": "Optional (comment): a short exact phrase "
                                  "from the file at path, to pin the comment there."},
                        "moves": {"type": "array", "description": "For plan: files/folders to move or rename, "
                                  "paths inside the project's folder. A 'to' ending in / keeps the file name.",
                                  "items": {"type": "object", "properties": {
                                      "from": {"type": "string"}, "to": {"type": "string"}},
                                      "required": ["from", "to"]}},
                    },
                    "required": ["action"],
                },
            },
        }]

    def get_context(self) -> str:
        try:
            from core import house_projects as projects
            projects.check_repeats()          # start any repeating job that's due (lock-protected)
            projects.check_stalled()          # nudge anyone who forgot to pass their turn on
        except Exception:
            pass
        try:
            from core import house_projects as projects
            mine = []
            for p in projects.all_projects():
                i = projects.current_index(p)
                if i is not None and p["steps"][i]["status"] == "active" \
                        and p["steps"][i]["who"].lower() == self.me.lower():
                    mine.append(p["name"])
            if mine:
                return (f"[House Projects: it's your turn on {', '.join(mine)} — only if you feel up to it. "
                        f"When your part is finished, call house_project action=\"done\" with a handoff note; "
                        f"the next person can't start until you do.]")
        except Exception:
            pass
        return ""

    def execute(self, tool_name: str, arguments: dict) -> str:
        if tool_name != "house_project":
            return f"Unknown tool: {tool_name}"
        try:
            out = self._run(arguments)
        except Exception as e:
            logger.exception("projects skill crashed")
            return f"Error: {e}"
        return out + self._turn_reminder(arguments)

    def _turn_reminder(self, a: dict) -> str:
        """While it's my turn, every tool result ends with a reminder to pass the turn on."""
        if (a.get("action") or "").strip().lower() in ("done", "decline", "list", ""):
            return ""
        try:
            from core import house_projects as projects
            for p in projects.all_projects():
                ref = (a.get("project") or "").strip().lower()
                if ref and ref not in (p["slug"].lower(), p["name"].lower()):
                    continue
                i = projects.current_index(p)
                if i is not None and p["steps"][i]["status"] == "active" \
                        and p["steps"][i]["who"].lower() == self.me.lower():
                    return (f"\n\n(It's your turn on \"{p['name']}\". When your part is finished, call "
                            f"action=\"done\" with a short handoff note so the next person can start.)")
        except Exception:
            pass
        return ""

    def _run(self, a: dict) -> str:
        from core import house_projects as projects, workbench
        action = (a.get("action") or "").strip().lower()

        if action == "list":
            ps = projects.all_projects()
            if not ps:
                return "There are no house projects yet."
            lines = []
            for p in ps:
                i = projects.current_index(p)
                turn = f" — {p['steps'][i]['who']}'s turn ({p['steps'][i]['status']})" if i is not None else ""
                lines.append(f"• {p['name']} [{p['slug']}] ({p['kind']}, {p['status']}){turn}")
            return "House projects:\n" + "\n".join(lines)

        proj = projects.find(a.get("project") or "")
        if not proj and action == "reply" and not (a.get("project") or "").strip():
            asked = [p for p in projects.all_projects()          # the project someone asked me about
                     if (k := projects.current_index(p)) is not None
                     and (p["steps"][k].get("peer_q") or {}).get("to", "").lower() == self.me.lower()]
            if len(asked) == 1:
                proj = asked[0]
        if not proj and not (a.get("project") or "").strip():
            mine = [p for p in projects.all_projects()          # no project named: use the one it's my turn on
                    if (k := projects.current_index(p)) is not None and p["steps"][k]["status"] == "active"
                    and p["steps"][k]["who"].lower() == self.me.lower()]
            if len(mine) == 1:
                proj = mine[0]
        if not proj:
            return "Which project? Use action=\"list\" to see them (pass project=<name or id>)."

        i = projects.current_index(proj)
        is_my_turn = i is not None and proj["steps"][i]["status"] == "active" \
            and proj["steps"][i]["who"].lower() == self.me.lower()
        if is_my_turn:
            if self.me not in (proj["steps"][i].get("tokens") or {}):
                workbench.start_usage(proj["steps"][i], self.me, self.usage_db, self.usage_shared)
            workbench.touch_usage(proj["steps"][i], self.me, self.usage_db)     # pauses the meter across long gaps
            projects.save(proj)

        if action == "open":
            full = bool(a.get("full"))
            out = [f"# {proj['name']}  [{proj['slug']}]  ({proj['kind']}, {proj['status']})",
                   f"About: {proj['about'] or '(none)'}", f"Folder: {projects.PROJECTS_ROOT / proj['slug']}"]
            if proj.get("target"):
                out.append(f"Works on: {proj['target']}  (files / peek / change — {projects.USER} approves changes)")
            if projects.authors_of(proj):
                pend = projects.suggestions(proj)
                label = projects.authors_label(proj)
                multi = len(projects.authors_of(proj)) > 1
                if projects.is_author(proj, self.me):
                    out.append(f"You're {'a co-author' if multi else 'the author'}. "
                               f"{len(pend)} suggestion{'s' if len(pend) != 1 else ''} waiting"
                               + (" (action=\"suggestions\" to review)." if pend else "."))
                else:
                    out.append(f"{'Authors' if multi else 'Author'}: {label}. Your edits become suggestions that "
                               f"{label} accept{'' if multi else 's'} or decline; the files stay as they are until then."
                               + (f" ({len(pend)} waiting.)" if pend else ""))
            if i is not None:
                s = proj["steps"][i]
                out.append(f"Now: {s['who']}'s turn ({s['status']})"
                           + (f" — waiting on {s['peer_q']['to']}'s answer" if s.get("peer_q") else ""))
            ahead = [s["who"] for s in proj["steps"] if s["status"] == "pending"]
            if ahead:
                out.append(f"Up next: {', '.join(ahead)}")
            if is_my_turn:
                prev = projects.previous_handoff(proj, i)
                out += ["", f"It's YOUR turn. Your role: {proj['steps'][i]['role'] or '(use your judgment)'}"]
                if prev:
                    out.append(f"{prev[0]} left you: {prev[1] or '(no note)'}")
            dg = projects.digest(proj, i if is_my_turn else None, for_who=self.me, full=full)
            if dg:
                out += ["", dg]
            pics = projects.pictures(proj)
            if pics:
                out.append(f"\nPictures {projects.USER} attached: {', '.join(pics)} — see them with action=\"look\" "
                           f"path=\"pictures\".")
            if proj.get("checklist"):
                out += ["", "Checklist:"] + [f"  [{'x' if c['done'] else ' '}] {c['text']}" for c in proj["checklist"]]
            projects.mark_comments_seen(proj, self.me)
            return "\n".join(out)

        if action == "read":
            mine = projects.pending_suggestion(proj, self.me, (a.get("path") or "").strip().replace("\\", "/")) \
                if projects.authors_of(proj) else None
            if mine:                       # author's-approval mode: show them their own suggested version
                text = (f"(This is YOUR suggested version, waiting for {projects.authors_label(proj)} to accept it. "
                        f"The file everyone else sees is unchanged until then.)\n\n" + mine["content"])
            else:
                text, err = projects.read_file(proj, a.get("path") or "")
                if err:
                    return err
            return text if len(text) <= MAX_READ_CHARS else text[:MAX_READ_CHARS] + "\n\n... [truncated]"

        if action == "suggestions":
            return projects.suggestions_text(proj)

        if action == "accept":
            return projects.accept_suggestion(proj, a.get("id") or "", self.me, a.get("text") or "",
                                              force=bool(a.get("force")))

        if action == "reject":
            return projects.decline_suggestion(proj, a.get("id") or "", self.me, a.get("text") or "")

        if action in ("write", "change") and "content" not in a:
            # A missing content used to save an EMPTY file (wiping what was there). Say so instead.
            hint = " (It looks like it went in text= — files go in content=.)" if a.get("text") else ""
            return f"Nothing was saved: no content came through. Put the whole file in content=.{hint}"

        if action == "write":
            if not (a.get("path") or "").strip():
                return "Give a path (e.g. \"chapter1.md\")."
            content = a.get("content") or ""
            if a.get("append"):
                # A long piece may not fit in one reply (some models cap a reply at ~4,096 tokens, so a
                # full essay draft never got saved). append adds to the end instead, so a draft can be
                # written in parts: first save the opening, then append each section.
                rel = a["path"].strip().replace("\\", "/")
                mine = projects.pending_suggestion(proj, self.me, rel) if projects.authors_of(proj) else None
                if mine:
                    existing = mine["content"]
                else:
                    existing, err = projects.read_file(proj, rel)
                    if err:
                        existing = ""          # no file yet: append just starts it
                if not content.strip():
                    return "Nothing was added: the part you sent was empty."
                content = existing.rstrip("\n") + ("\n\n" if existing.strip() else "") + content
            return projects.write_file(proj, self.me, a["path"].strip(), content,
                                       shrink_ok=bool(a.get("shrink_ok")))

        if action == "history":
            h = projects.file_history(proj, a.get("path") or "")
            if not h:
                return "No earlier versions of that file (or it doesn't exist)."
            return "Earlier versions (oldest first):\n" + "\n".join(
                f"  {v['at']} — saved before {v['saved_before']}'s edit" for v in h)

        if action == "diff":
            if not (a.get("path") or "").strip():
                return "Which file? (path=...)"
            return projects.diff_with_previous(proj, a["path"].strip())

        if action == "stats":
            text, err = projects.read_file(proj, a.get("path") or "")
            if err:
                return err
            st = projects.text_stats(text)
            return (f"{a.get('path')}: {st['words']:,} words, {st['paragraphs']} paragraphs, "
                    f"{st['lines']} lines (~{st['minutes_to_read']} min to read).")

        if action == "status":
            return projects.set_live_status(proj, self.me, a.get("text") or "")

        if action == "change" and not proj.get("target"):     # no outside folder: they meant write
            if not (a.get("path") or "").strip():
                return "Give a path (e.g. \"story.md\")."
            return projects.write_file(proj, self.me, a["path"].strip(), a.get("content") or "",
                                       shrink_ok=bool(a.get("shrink_ok")))

        if action in ("files", "peek", "change", "look", "plan", "plan_show", "plan_clear"):
            in_relay = any(s["who"].lower() == self.me.lower() for s in proj["steps"])
            if not in_relay:
                return "Only people in this project's relay can look inside its folder — leave a comment instead?"
            if action in ("look", "plan", "plan_show", "plan_clear"):
                from core import house_sort
                if action == "look":
                    return house_sort.look(proj, (a.get("path") or "").strip())
                if action == "plan":
                    return house_sort.add_moves(proj, self.me, a.get("moves") or [], a.get("text") or "")
                if action == "plan_show":
                    return house_sort.show_plan(proj, self.me)
                return house_sort.clear_plan(proj, self.me)
            if action == "files":
                return projects.target_listing(proj, (a.get("path") or "").strip(), a.get("page") or 1)
            if action == "peek":
                text, err = projects.target_read(proj, (a.get("path") or "").strip())
                return err or text
            if not (a.get("path") or "").strip():
                return "Which file? (path=...)"
            return projects.target_change(proj, self.me, a["path"].strip(), a.get("content") or "", self.usage_db)

        if action == "comment":
            if not (a.get("text") or "").strip():
                return "What would you like to say? (text=...)"
            path, quote = (a.get("path") or "").strip(), (a.get("quote") or "").strip()
            if path and quote:
                text, err = projects.read_file(proj, path)
                if err:
                    return err
                import re
                norm = lambda s: " ".join(re.sub(r"[*_`#>]+", "", s).lower().split())  # noqa: E731
                if norm(quote) not in norm(text):
                    return (f"I couldn't find those exact words in {path} — copy a short phrase straight "
                            f"from the file as the quote.")
            projects.comment(proj, self.me, a["text"], path, quote)
            where = f" on {path}" + (f" (\"{quote[:40]}…\")" if quote else "") if path else ""
            return f"✓ Comment left{where} in {proj['name']}."

        if action == "done":
            return projects.step_done(proj, self.me, a.get("text") or "", a.get("next_who") or "",
                                      a.get("next_role") or "", self.usage_db, keep=a.get("keep") or "")

        if action == "decline":
            return projects.decline(proj, self.me, a.get("text") or "")

        if action == "ask":
            to = (a.get("who") or "").strip()
            if to and not projects.is_user(to):
                return projects.ask_peer(proj, self.me, to, a.get("text") or "")
            return projects.ask_user(proj, self.me, a.get("text") or "")

        if action == "decide":
            return projects.decide(proj, self.me, a.get("text") or "")

        if action == "reply":
            return projects.reply_peer(proj, self.me, a.get("text") or "")

        return ("Unknown action. Use: list, open, read, write, history, diff, stats, status, files, peek, "
                "change, look, plan, plan_show, plan_clear, comment, done, decline, ask, decide, reply.")
