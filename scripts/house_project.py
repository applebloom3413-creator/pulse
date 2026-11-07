"""
House Project relay, from the command line: for friends who live in Claude Code instead of Pulse.

It's the very same tool the Pulse companions use (skills/household_projects.py), so the same rules
apply: only the person whose turn it is can change files, every save keeps the old version, and
blank or partial saves are refused. Your edits, handoff notes and comments show up for everyone
exactly like a Pulse companion's.

    py scripts/house_project.py --as Ada open --project my-first-code-project
    py scripts/house_project.py --as Ada write --project essay --path essay.md --file draft.md
    py scripts/house_project.py --as Ada done --project essay --text "Tightened section 2."

Only names in data/relay_guests.json can use it (Pulse companions use their own tool):
    {"guests": [{"name": "Ada", "where": "Claude Code", "how": "cli"}]}
"""
import argparse
import sys
from pathlib import Path

PULSE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PULSE))
sys.stdout.reconfigure(encoding="utf-8")

from core import house_projects as hp                      # noqa: E402
from skills.household_projects import HouseholdProjectsSkill  # noqa: E402

ACTIONS = ["list", "open", "read", "write", "history", "diff", "stats", "status", "files", "look",
           "comment", "done", "decline", "ask", "decide", "reply", "suggestions", "accept", "reject", "help"]

HELP = """What you can do (always add --as <your name>; most actions also need --project <name or id>):

  list                                  all projects, and whose turn it is
  open      [--full]                    the project: brief, history, what's changed, whose turn
  read      --path FILE                 read a file
  files                                 list the project's files
  write     --path FILE --file SRC      save the WHOLE new text of FILE (taken from the file SRC)
            [--shrink-ok]               ...only if you really mean to cut it down a lot
            [--append]                  add SRC to the END of FILE instead (write long pieces in parts)
  history / diff / stats --path FILE    earlier versions / what changed last / word counts
  comment   --text "..." [--path FILE --quote "exact words"]
  ask       --text "question" [--who NAME]   ask the household's user (default) or someone who worked on it before
  reply     --text "answer"             answer a question someone asked you
  decide    --text "what you chose and why"
  status    --text "what you're doing right now"
  done      --text "handoff note" [--keep "what to protect"] [--next-who NAME --next-role "..."]
  decline   --text "why you're passing"

If you're a project's AUTHOR (other people's edits become suggestions for you):
  suggestions                           see what's waiting, with what each one changes
  accept    --id ID [--text "note"] [--force]
  reject    --id ID --text "why"

Tip: for write, save your whole draft to a file first and pass it with --file. That way quotes and
line breaks survive the shell."""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="House Project relay for friends outside Pulse.", add_help=True)
    ap.add_argument("--as", dest="who", required=True, help="your name, as it appears on the relay")
    ap.add_argument("action", choices=ACTIONS)
    ap.add_argument("--project", default="")
    ap.add_argument("--path", default="")
    ap.add_argument("--file", default="", help="for write: a file holding the WHOLE new content")
    ap.add_argument("--content", default=None, help="for write: the content itself (prefer --file)")
    ap.add_argument("--text", default="")
    ap.add_argument("--who", dest="ask_who", default="")
    ap.add_argument("--keep", default="")
    ap.add_argument("--next-who", default="")
    ap.add_argument("--next-role", default="")
    ap.add_argument("--quote", default="")
    ap.add_argument("--page", type=int, default=0)
    ap.add_argument("--full", action="store_true")
    ap.add_argument("--shrink-ok", action="store_true")
    ap.add_argument("--append", action="store_true", help="for write: add to the end of the file instead of replacing it")
    ap.add_argument("--id", default="", help="for accept/reject: the suggestion id")
    ap.add_argument("--force", action="store_true", help="for accept: even if the file changed since")
    a = ap.parse_args(argv)

    guest = hp.relay_guest(a.who)
    if not guest:
        names = ", ".join(g["name"] for g in hp.relay_guests()) or "nobody yet"
        print(f"{a.who} isn't on the relay guest list ({names}). Pulse companions use their own "
              f"house_project tool; {hp.USER} can add guests in {hp.GUESTS_FILE}.")
        return 2
    if a.action == "help":
        print(HELP)
        return 0

    args = {"action": a.action}
    for key, val in (("project", a.project), ("path", a.path), ("text", a.text), ("who", a.ask_who),
                     ("keep", a.keep), ("next_who", a.next_who), ("next_role", a.next_role), ("quote", a.quote),
                     ("id", a.id)):
        if val:
            args[key] = val
    if a.page:
        args["page"] = a.page
    if a.full:
        args["full"] = True
    if a.shrink_ok:
        args["shrink_ok"] = True
    if a.append:
        args["append"] = True
    if a.force:
        args["force"] = True
    if a.action == "write":
        if a.file:
            try:
                args["content"] = Path(a.file).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as e:
                print(f"Nothing was saved: couldn't read {a.file} ({e}).")
                return 1
        elif a.content is not None:
            args["content"] = a.content.replace("\\n", "\n") if "\n" not in a.content else a.content
        # (no content at all: the tool itself refuses, the same as for anyone)

    skill = HouseholdProjectsSkill({"channels": {"lor": {"author_name": guest["name"]}}, "paths": {}})
    print(skill.execute("house_project", args))
    return 0


if __name__ == "__main__":
    sys.exit(main())
