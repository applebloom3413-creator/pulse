"""
Tests for House Projects (core/house_projects.py + skills/household_projects.py).

Runs in a temp folder; pings and letters are faked, so no real companion is
woken and the real projects folder is never touched. The household is made up
(user "Robin"; companions Ada, Basil, Clover, Dot, ... — see HOUSE below), so the
tests don't depend on whoever lives in personas/.

Run:
    py -3.14 -m unittest tests.test_house_projects -v
"""

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

_PULSE_ROOT = Path(__file__).resolve().parent.parent
if str(_PULSE_ROOT) not in sys.path:
    sys.path.insert(0, str(_PULSE_ROOT))

from core import house_projects as hp, workbench  # noqa: E402
from skills.household_projects import HouseholdProjectsSkill  # noqa: E402

USER = "Robin"
HOUSE = {"Ada", "Basil", "Clover", "Dot", "Ember", "Fern", "Gus", "Hazel", "Iris", "Juniper"}


class HouseProjectsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._saved = (hp.PROJECTS_ROOT, workbench.ping, workbench.send_letter, workbench.is_awake,
                       workbench._persona_config, hp.USER, hp.USER_ALIASES)
        hp.PROJECTS_ROOT = self.tmp / "projects"
        hp.USER, hp.USER_ALIASES = USER, {USER.lower(), "user", "the user"}
        self.pings, self.letters, self.asleep = [], [], set()
        workbench.ping = lambda name, text: (self.pings.append((name, text)), name != "Hazel")[1]
        workbench.send_letter = lambda to, subject, content: self.letters.append(to)
        workbench.is_awake = lambda name: name not in self.asleep          # never scan real processes
        workbench._persona_config = lambda name: (name.lower(), {}) if name in HOUSE else None

    def tearDown(self):
        (hp.PROJECTS_ROOT, workbench.ping, workbench.send_letter, workbench.is_awake,
         workbench._persona_config, hp.USER, hp.USER_ALIASES) = self._saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    def me(self, name):
        return HouseholdProjectsSkill({"channels": {"lor": {"author_name": name}}, "paths": {}})

    def run_tool(self, as_who, **args):
        return self.me(as_who).execute("house_project", args)

    def test_relay_passes_role_and_handoff_note(self):
        p = hp.create("October Story", "A cozy spooky story")
        hp.add_step(p, "Ada", "First draft of chapter 1, cozy tone")
        hp.add_step(p, "Basil", "Add a scary bit")
        self.assertIn("Ada was pinged", hp.start(hp.load(p["slug"])))
        self.assertIn("First draft of chapter 1", self.pings[-1][1])

        self.assertIn("Only the person whose turn", self.run_tool("Basil", action="write",
                      project="October Story", path="ch1.md", content="nope"))
        self.assertIn("Saved", self.run_tool("Ada", action="write", project="october-story",
                                             path="ch1.md", content="Once upon a time"))
        out = self.run_tool("Ada", action="done", project="October Story", text="Left a cliffhanger!")
        self.assertIn("Basil was pinged", out)
        self.assertIn("Add a scary bit", self.pings[-1][1])
        self.assertIn("Left a cliffhanger!", self.pings[-1][1])           # handoff reaches Basil

        self.assertIn("previous version kept", self.run_tool("Basil", action="write",
                      project="October Story", path="ch1.md", content="Once upon a BOO"))
        self.assertEqual(len(hp.file_history(hp.load(p["slug"]), "ch1.md")), 1)
        self.assertIn("last step", self.run_tool("Basil", action="done", project="October Story",
                                                 text="Scared it up"))
        self.assertEqual(hp.load(p["slug"])["status"], "finished")

    def test_open_relay_lets_worker_choose_next(self):
        p = hp.create("Idea", "free-form")
        hp.add_step(p, "Ada", "Start it")
        hp.start(hp.load(p["slug"]))
        out = self.run_tool("Ada", action="done", project="Idea", text="over to you",
                            next_who="Clover", next_role="Make it funnier")
        self.assertIn("Clover was pinged", out)
        self.assertEqual(hp.load(p["slug"])["steps"][1]["added_by"], "Ada")
        self.asleep.add("Dot")                                       # the turn goes to Dot, who's asleep…
        p2 = hp.create("Idea Two", "x")
        hp.add_step(p2, "Ada", "go")
        hp.start(hp.load(p2["slug"]))
        out2 = self.run_tool("Ada", action="done", project="Idea Two", text="yours", next_who="Dot")
        self.assertIn("Dot isn't running right now", out2)          # …and now Robin hears about it
        out = self.run_tool("Clover", action="done", project="Idea", text="done!", next_who="whoever's-next")
        self.assertIn("isn't anyone in the house", out)                  # no made-up people in the relay
        self.assertEqual(len(hp.load(p["slug"])["steps"]), 2)
        self.assertEqual(hp.load(p["slug"])["status"], "finished")

    def test_closed_relay_refuses_worker_choice(self):
        p = hp.create("Planned", "Robin plans it", open_relay=False)
        hp.add_step(p, "Ada", "Only step")
        hp.start(hp.load(p["slug"]))
        out = self.run_tool("Ada", action="done", project="Planned", text="x", next_who="Clover")
        self.assertIn("they'll decide", out)
        self.assertEqual(len(hp.load(p["slug"])["steps"]), 1)

    def test_check_first_pauses_until_user_starts(self):
        p = hp.create("Careful", "")
        hp.add_step(p, "Ada", "Draft")
        hp.add_step(p, "Dot", "Review", check_first=True)
        hp.start(hp.load(p["slug"]))
        self.assertIn("check with Robin first", self.run_tool("Ada", action="done", project="Careful", text="done"))
        self.assertEqual([n for n, _ in self.pings], ["Ada"])            # Dot not pinged yet
        self.assertIn("Dot was pinged", hp.start(hp.load(p["slug"])))

    def test_decline_pauses_relay(self):
        p = hp.create("Maybe", "")
        hp.add_step(p, "Ember", "Write a love poem")
        hp.start(hp.load(p["slug"]))
        self.assertIn("completely fine", self.run_tool("Ember", action="decline", project="Maybe",
                                                       text="Not my thing"))
        p = hp.load(p["slug"])
        self.assertEqual(p["status"], "paused")
        self.assertIn("declined", hp.start(p))

    def test_read_without_names_and_user_moves_on(self):
        p = hp.create("Short Story", "under 500 words")
        hp.add_step(p, "Ada", "write it")
        hp.add_step(p, "Fern", "add a twist")
        hp.add_step(p, "Gus", "polish")
        hp.start(hp.load(p["slug"]))
        self.assertIn("No files yet", self.run_tool("Ada", action="read"))          # my-turn project, no path
        self.run_tool("Ada", action="write", path="story.md", content="Once upon a time.")
        self.assertIn("Once upon a time", self.run_tool("Ada", action="read"))      # only file opens itself
        self.run_tool("Ada", action="done", text="over to you")
        # Fern got stuck and sent their part in chat; Robin pastes it in and moves on
        out = hp.move_on(hp.load(p["slug"]), paste="Then the twist happened.", note="from Fern's chat")
        self.assertIn("Saved Fern's work to story.md", out)
        self.assertIn("Gus was pinged", out)
        self.assertIn("from Fern's chat", self.pings[-1][1])
        text, _ = hp.read_file(hp.load(p["slug"]), "story.md")
        self.assertEqual(text, "Once upon a time.\n\nThen the twist happened.\n")
        q = hp.load(p["slug"])
        self.assertEqual(q["steps"][1]["status"], "done")
        self.assertEqual(q["file_log"][-1]["who"], "Fern (pasted in by Robin)")
        # skipping with nothing pasted finishes the relay
        self.assertIn("finished", hp.move_on(q))
        self.assertEqual(hp.load(p["slug"])["steps"][2]["status"], "skipped")

    def test_nudges_and_turn_reminders(self):
        from datetime import datetime, timedelta
        from core import house_live
        p = hp.create("Relay", "x")
        hp.add_step(p, "Ada", "write it")
        hp.add_step(p, "Ember", "edit it")
        hp.start(hp.load(p["slug"]))
        out = self.run_tool("Ada", action="change", path="story.md", content="Hi")   # no outside folder → write
        self.assertIn("Saved", out)
        self.assertIn('action="done"', out)                                            # reminder on my turn
        self.assertNotIn('action="done"', self.run_tool("Ember", action="read", project="relay"))
        base = datetime.now()
        saved = house_live.activity
        try:
            house_live.activity = lambda who, since_iso=None, now=None: {
                "state": "resting", "now": {"at": base}, "feed": []}
            self.assertEqual(hp.check_stalled(base + timedelta(minutes=5)), [])         # too soon
            n = len(self.pings)
            self.assertEqual(hp.check_stalled(base + timedelta(minutes=20)), ["nudged Ada on Relay"])
            self.assertIn("Gentle nudge", self.pings[n][1])
            self.assertIn("Ember can start", self.pings[n][1])
            self.assertEqual(hp.check_stalled(base + timedelta(minutes=25)), [])        # only once
            self.assertEqual(hp.check_stalled(base + timedelta(minutes=60)), ["told Robin about Ada on Relay"])
            self.assertEqual(hp.check_stalled(base + timedelta(minutes=90)), [])        # Robin told once
            house_live.activity = lambda who, since_iso=None, now=None: {"state": "working", "now": None}
            q = hp.load(p["slug"])
            q["steps"][0].pop("nudged_at"); q["steps"][0].pop("user_told"); hp.save(q)
            self.assertEqual(hp.check_stalled(base + timedelta(hours=5)), [])           # busy: leave them be
        finally:
            house_live.activity = saved
        self.assertIn("Nudged Ada", hp.nudge(hp.load(p["slug"])))                     # Robin's button

    def test_working_together(self):
        """Rules, keep-notes, decisions, the digest in the turn ping, asking an earlier person,
        comments reaching the author, rolling back unfinished drafts, and sending the work home."""
        from datetime import datetime, timedelta
        from core import house_live, house_recap
        self.letters_full = []
        workbench.send_letter = lambda to, subject, content: (self.letters.append(to),
                                                              self.letters_full.append((to, subject, content)))
        p = hp.create("Story", "a short story", kind="Writing")
        self.assertIn("Rules updated", hp.set_rules(p, "Under 500 words. No present tense."))
        for who, role in (("Ada", "draft"), ("Ember", "add a twist"), ("Clover", "polish")):
            hp.add_step(hp.load(p["slug"]), who, role)
        hp.start(hp.load(p["slug"]))
        self.assertIn("RULES FOR EVERYONE", self.pings[-1][1])
        self.assertIn("No present tense", self.pings[-1][1])

        self.run_tool("Ada", action="write", path="story.md", content="The aunt left. Nobody knew why.\n")
        self.assertIn("Logged", self.run_tool("Ada", action="decide", text="The aunt's reason stays a mystery."))
        self.run_tool("Ada", action="done", text="draft in", keep="the last line")
        brief = self.pings[-1][1]                                          # Ember's turn ping
        self.assertEqual(self.pings[-1][0], "Ember")
        for bit in ("PLEASE KEEP", 'Ada: "the last line"', "DECISIONS ALREADY MADE", "stays a mystery",
                    "CURRENT story.md (full text", "Nobody knew why", "WHAT ADA CHANGED"):
            self.assertIn(bit, brief)

        # Ember asks Ada directly; Ada replies without it becoming Ada's turn
        self.assertIn("didn't take a turn", self.run_tool("Ember", action="ask", who="Clover", text="?"))
        self.assertIn("Asked Ada", self.run_tool("Ember", action="ask", who="Ada", text="Why did she leave?"))
        self.assertEqual(self.pings[-1][0], "Ada")
        self.assertIn('action="reply"', self.pings[-1][1])
        self.assertIn("Sent your answer to Ember", self.run_tool("Ada", action="reply", text="On purpose: mystery."))
        self.assertEqual(self.pings[-1][0], "Ember")
        self.assertIn("On purpose: mystery.", self.pings[-1][1])
        self.assertIn("Nobody on this project", self.run_tool("Ada", action="reply", project="story", text="again"))
        self.asleep.add("Ada")                                            # Ada's window is closed
        self.assertIn("isn't running", self.run_tool("Ember", action="ask", who="Ada", text="One more?"))
        self.asleep.discard("Ada")
        self.run_tool("Ember", action="ask", who="Ada", text="Really?")     # …and this one goes unanswered
        saved = house_live.activity
        try:
            house_live.activity = lambda who, since_iso=None, now=None: {"state": "resting", "now": None}
            out = hp.check_stalled(datetime.now() + timedelta(minutes=hp.PEER_WAIT_MIN + 1))
            self.assertIn("Ada didn't answer Ember on Story", out)
            self.assertIn("best judgment", self.pings[-1][1])
        finally:
            house_live.activity = saved

        # Ember comments on Ada's line → Ada hears about it, once
        self.run_tool("Ember", action="comment", path="story.md", quote="Nobody knew why", text="Chills.")
        self.assertIn(("Ada", "A comment on your work: Story"), [(t, s) for t, s, _ in self.letters_full])
        self.assertIn("NEW COMMENTS ON YOUR WORK", self.run_tool("Ada", action="open", project="story"))
        self.assertNotIn("NEW COMMENTS ON YOUR WORK", self.run_tool("Ada", action="open", project="story"))

        # Ember saves a half-done draft and stalls; Robin moves on and rolls it back
        self.run_tool("Ember", action="write", path="story.md", content="The aunt left. Then a twis")
        self.run_tool("Ember", action="write", path="notes.md", content="half an idea")
        out = hp.move_on(hp.load(p["slug"]), drafts="rollback")
        self.assertIn("Rolled back", out)
        q = hp.load(p["slug"])
        self.assertEqual(hp.read_file(q, "story.md")[0], "The aunt left. Nobody knew why.\n")
        self.assertNotIn("notes.md", hp.list_files(q))                     # tucked into Versions, not deleted
        self.assertTrue(hp.file_history(q, "notes.md"))
        self.assertEqual(q["steps"][1]["status"], "skipped")
        self.assertIn("Ember: skipped", self.pings[-1][1])                  # Clover's digest says so

        self.run_tool("Clover", action="write", path="story.md", content="The aunt left. Nobody ever knew why.\n")
        self.run_tool("Clover", action="done", text="polished")
        homes = [(t, s) for t, s, _ in self.letters_full if s == "Finished: Story"]
        self.assertEqual(sorted(t for t, _ in homes), ["Ada", "Clover"])  # everyone who did a turn
        letter = next(c for t, s, c in self.letters_full if t == "Ada" and s == "Finished: Story")
        self.assertIn("Nobody ever knew why", letter)                      # the finished piece comes home
        self.assertIn("with Clover", letter)
        col = house_recap.colophon(hp.load(p["slug"]))
        self.assertIn("- Ada: draft", col)
        self.assertIn("- Clover: polish", col)

    def test_unfinished_drafts_kept_and_flagged(self):
        p = hp.create("Notes", "x")
        hp.add_step(p, "Ada", "start")
        hp.add_step(p, "Ember", "finish")
        hp.start(hp.load(p["slug"]))
        self.run_tool("Ada", action="write", path="a.md", content="half")
        self.assertIn("Kept Ada's unfinished drafts", hp.move_on(hp.load(p["slug"])))
        self.assertIn("didn't finish", self.pings[-1][1])
        self.assertEqual(hp.read_file(hp.load(p["slug"]), "a.md")[0], "half")

    def test_recap_when_finished(self):
        p = hp.create("Tiny Tale", "a very short story", kind="Writing")
        hp.add_step(p, "Ada", "draft")
        hp.add_step(p, "Ember", "polish")
        hp.start(hp.load(p["slug"]))
        self.run_tool("Ada", action="write", path="tale.md", content="Once there was a possum.")
        self.run_tool("Ada", action="ask", text="Funny or sweet?")
        hp.answer_user(hp.load(p["slug"]), "Sweet.")
        self.run_tool("Ada", action="done", text="draft in")
        hp.move_on(hp.load(p["slug"]), paste="He was very round.", note="from chat")
        q = hp.load(p["slug"])
        self.assertEqual(q["status"], "finished")
        self.assertIn("RECAP.md", hp.list_files(q))
        recap, _ = hp.read_file(q, "RECAP.md")
        self.assertIn("# Recap: Tiny Tale", recap)
        self.assertIn('Asked Robin: "Funny or sweet?" (Robin: "Sweet.")', recap)
        self.assertIn("moved on by Robin", recap)
        self.assertIn("Words in the main piece", recap)

    def test_ask_user_and_answer(self):
        from datetime import datetime, timedelta
        from core import house_live
        p = hp.create("Reply Help", "help me word a post")
        hp.add_step(p, "Ada", "word it")
        hp.add_step(p, "Ember", "check it")
        hp.start(hp.load(p["slug"]))
        self.assertIn("only ask Robin", self.run_tool("Ember", action="ask", project="reply-help", text="?"))
        out = self.run_tool("Ada", action="ask", text="Should it sound funny or serious?")
        self.assertIn("went to Robin", out)
        q = hp.load(p["slug"])
        self.assertEqual(q["steps"][0]["question"]["text"], "Should it sound funny or serious?")
        saved = house_live.activity
        try:                                                    # waiting on Robin isn't "stuck": no nudge
            house_live.activity = lambda who, since_iso=None, now=None: {"state": "resting", "now": None}
            self.assertEqual(hp.check_stalled(datetime.now() + timedelta(hours=3)), [])
        finally:
            house_live.activity = saved
        n = len(self.pings)
        self.assertIn("Sent your answer to Ada", hp.answer_user(q, "Funny, but kind."))
        self.assertIn("Funny, but kind.", self.pings[n][1])
        self.assertEqual(self.pings[n][0], "Ada")
        q = hp.load(p["slug"])
        self.assertNotIn("question", q["steps"][0])
        self.assertEqual(q["steps"][0]["qa"][0]["a"], "Funny, but kind.")
        self.assertIn("no open question", hp.answer_user(q, "again"))
        self.assertIn("Ember was pinged", self.run_tool("Ada", action="done", text="worded!"))

    def test_budget_pauses_between_turns(self):
        from core import house_costs
        saved = house_costs.project_cost
        try:
            spent = {"v": 1.0}
            house_costs.project_cost = lambda proj, live=False: spent["v"]
            p = hp.create("Paper", "x")
            hp.add_step(p, "Ada", "a")
            hp.add_step(p, "Ember", "b")
            hp.add_step(p, "Clover", "c")
            self.assertIn("$5.00", hp.set_budget(hp.load(p["slug"]), 5))
            hp.start(hp.load(p["slug"]))
            self.assertIn("Ember was pinged", self.run_tool("Ada", action="done", project="paper", text="ok"))
            spent["v"] = 4.5                                                   # 90% → next turn told to keep it small
            spent["v"] = 5.2                                                   # Ember's turn pushes it over
            out = self.run_tool("Ember", action="done", project="paper", text="ok")
            self.assertIn("reached its budget", out)                          # Ember finished fine…
            q = hp.load(p["slug"])
            self.assertEqual((q["status"], q["steps"][2]["status"]), ("paused", "pending"))   # …Clover waits
            self.assertIn("Clover was pinged", hp.start(q))                    # Robin can choose to keep going
            spent["v"] = 4.5
            q = hp.load(p["slug"])
            self.assertIn("keep this turn small", hp._brief(q, 2))
            self.assertIn("No budget", hp.set_budget(q, 0))
        finally:
            house_costs.project_cost = saved

    def test_garbled_writes_are_caught(self):
        p = hp.create("Cookie Jar", "code")
        hp.add_step(p, "Clover", "add persistence")
        hp.start(hp.load(p["slug"]))
        nulls = "def main():\n" + "\x00\x00\x00\x00" + "print(\\\"hi\\\")\n"           # what Clover's save looked like
        out = self.run_tool("Clover", action="write", path="cookie_jar.py", content=nulls)
        self.assertIn("Nothing was saved", out)
        self.assertEqual(hp.list_files(hp.load(p["slug"])), [])
        one_line = "def main():\\n    print('hi')\\n\\nmain()\\n"
        self.assertIn("real line breaks", self.run_tool("Clover", action="write", path="a.py", content=one_line))
        out = self.run_tool("Clover", action="write", path="cookie_jar.py", content="try:\n    x = 1\n")
        self.assertIn("Saved cookie_jar.py", out)                                    # work in progress is allowed…
        self.assertIn("doesn't run as Python yet", out)                              # …with a warning
        self.assertNotIn("⚠", self.run_tool("Clover", action="write", path="ok.py", content="print('hi')\n"))
        self.assertNotIn("⚠", self.run_tool("Clover", action="write", path="notes.md",
                                            content="Use \\n for a newline in Python."))

    def test_rename_keeps_folder(self):
        p = hp.create("halp", "old words")
        self.assertIn("name and description", hp.rename(p, "Reply Help", "new words"))
        q = hp.find("halp")                                           # same folder / id
        self.assertEqual((q["name"], q["about"]), ("Reply Help", "new words"))
        self.assertEqual(hp.find("Reply Help")["slug"], "halp")
        self.assertIn("needs a name", hp.rename(q, "  ", "x"))
        self.assertEqual(hp.rename(hp.load("halp"), "Reply Help", "new words"), "Nothing changed.")

    def test_attached_pictures(self):
        from PIL import Image
        from core import house_sort
        shot = self.tmp / "post screenshot.png"
        Image.new("RGB", (40, 30), "white").save(shot)
        p = hp.create("Reply Help", "I know what I want to say, not how")
        self.assertIn("Attached post screenshot.png", hp.add_picture(p, shot))
        self.assertIn("(2)", hp.add_picture(hp.load(p["slug"]), shot))                 # no overwrite
        self.assertIn("isn't a picture", hp.add_picture(hp.load(p["slug"]), self.tmp / "nope.txt"))
        hp.add_step(hp.load(p["slug"]), "Ada", "help me word it")
        hp.start(hp.load(p["slug"]))
        self.assertIn('action="look" path="pictures"', self.pings[-1][1])             # brief says to look
        self.assertIn("is a picture", self.run_tool("Ada", action="read", path="pictures/post screenshot.png"))
        self.assertIn("Pictures Robin attached", self.run_tool("Ada", action="open", project="reply-help"))
        calls = []
        saved = house_sort._describe
        try:
            house_sort._describe = lambda f, *a, **k: (calls.append(f.name), "Post by @x: \"hello world\"")[1]
            out = self.run_tool("Ada", action="look", path="pictures")
            self.assertIn("hello world", out)
            self.assertEqual(len(calls), 2)
            self.run_tool("Ada", action="look", path="post screenshot.png")             # cached, by name
            self.assertEqual(len(calls), 2)
        finally:
            house_sort._describe = saved

    def test_shared_schedule_persona_gets_letter(self):
        p = hp.create("Letters", "")
        hp.add_step(p, "Hazel", "Draft")
        self.assertIn("sent a letter", hp.start(hp.load(p["slug"])))
        self.assertEqual(self.letters, ["Hazel"])

    def test_anyone_can_comment_and_read(self):
        p = hp.create("Open", "")
        hp.write_file(hp.load(p["slug"]), hp.USER, "notes.md", "hello")
        self.assertIn("Comment left", self.run_tool("Fern", action="comment", project="Open", text="Love this"))
        self.assertEqual(self.run_tool("Fern", action="read", project="Open", path="notes.md"), "hello")
        self.assertIn("Fern: Love this", self.run_tool("Iris", action="open", project="Open"))

    def test_paths_cannot_escape_or_touch_meta(self):
        p = hp.create("Safe", "")
        for bad in ("../escape.txt", ".project/project.json", ".env"):
            self.assertIn("isn't inside", hp.write_file(hp.load(p["slug"]), hp.USER, bad, "x"))
        self.assertFalse((self.tmp / "projects" / "escape.txt").exists())

    def test_reorder_and_restore(self):
        p = hp.create("Order", "")
        hp.add_step(p, "A", "1"); hp.add_step(p, "B", "2")
        hp.move_step(hp.load(p["slug"]), 1, -1)
        self.assertEqual([s["who"] for s in hp.load(p["slug"])["steps"]], ["B", "A"])
        pr = hp.load(p["slug"])
        hp.write_file(pr, hp.USER, "a.md", "v1")
        hp.write_file(hp.load(p["slug"]), hp.USER, "a.md", "v2")
        v = hp.file_history(hp.load(p["slug"]), "a.md")[0]
        hp.restore_version(hp.load(p["slug"]), "a.md", v["path"])
        self.assertEqual(hp.read_file(hp.load(p["slug"]), "a.md")[0], "v1")

    def test_meter_skips_time_away(self):
        # a turn left open for hours shouldn't bill everything else the companion did meanwhile
        from datetime import datetime, timedelta
        from core import workbench

        class FakeDB:
            calls = 10
            def get_usage_today(self, day):
                return [{"prompt_tokens": self.calls * 1000, "completion_tokens": self.calls * 10,
                         "calls": self.calls}]
        db, step = FakeDB(), {}
        workbench.start_usage(step, "Ember", db)
        workbench.touch_usage(step, "Ember", db)            # opens the project
        db.calls += 5                                       # works on it
        workbench.touch_usage(step, "Ember", db)            # saves a file
        rec = step["tokens"]["Ember"]
        rec["last_touch"]["at"] = (datetime.now() - timedelta(hours=2)).isoformat(timespec="seconds")
        db.calls += 40                                      # two hours of chats and heartbeats
        self.assertEqual(workbench.turn_used(rec, db)["calls"], 5)    # live meter already ignores the gap
        workbench.touch_usage(step, "Ember", db)            # comes back
        db.calls += 3
        workbench.finish_usage(step, "Ember", db)
        self.assertEqual(rec["used"]["calls"], 8)          # 5 + 3, not 48
        self.assertEqual(rec["pauses"], 1)

    def test_paste_guest_turn(self):
        # a guest in another chat app takes turns by the user copying the brief over and pasting the reply back
        import json
        guests = self.tmp / "relay_guests.json"
        guests.write_text(json.dumps({"guests": [{"name": "Linden", "where": "ChatGPT", "how": "paste"}]}))
        saved, hp.GUESTS_FILE = hp.GUESTS_FILE, guests
        letters, pings = [], []
        saved_letter, saved_ping = workbench.send_letter, hp._ping_user
        workbench.send_letter = lambda to, subj, body: letters.append(to)
        hp._ping_user = lambda text, key: pings.append(text)
        try:
            p = hp.create("Essay", "why the sky is blue")
            hp.write_file(hp.load(p["slug"]), hp.USER, "essay.md", "Draft.\n")
            hp.add_step(hp.load(p["slug"]), "Linden", "add the opening question")
            self.assertIn("Copy turn brief", hp.start(hp.load(p["slug"])))
            self.assertEqual(letters, [])                                  # no letter they can't read
            self.assertIn("Copy turn brief", pings[-1])
            brief = hp.turn_brief(hp.load(p["slug"]), 0)
            self.assertIn("add the opening question", brief)
            self.assertIn("HANDOFF:", brief)                               # paste how-to rides along
            hp.move_on(hp.load(p["slug"]), paste="Linden's opening.", file="essay.md")
            proj = hp.load(p["slug"])
            self.assertEqual(proj["steps"][0]["status"], "done")
            essay_saves = [e for e in proj["file_log"] if e["file"] == "essay.md"]
            self.assertIn("Linden (pasted in by Robin)", essay_saves[-1]["who"])
        finally:
            hp.GUESTS_FILE, workbench.send_letter, hp._ping_user = saved, saved_letter, saved_ping

    def test_relay_guests_take_turns_from_outside_pulse(self):
        # Claude Code / Cowork friends (relay guests) can take relay turns
        import json
        guests = self.tmp / "relay_guests.json"
        guests.write_text(json.dumps({"guests": [{"name": "Moss", "where": "Claude Code", "how": "cli"},
                                                 {"name": "Nell", "where": "Cowork", "how": "bridge"}]}))
        saved, hp.GUESTS_FILE = hp.GUESTS_FILE, guests
        letters, pings = [], []
        saved_letter, saved_ping = workbench.send_letter, hp._ping_user
        workbench.send_letter = lambda to, subj, body: letters.append((to, subj, body))
        hp._ping_user = lambda text, key: pings.append(text)
        try:
            p = hp.create("Essay", "an essay")
            hp.add_step(p, "Moss", "tighten section 2")
            hp.add_step(p, "Nell", "write the ending")
            self.assertIn("go tell them", hp.start(hp.load(p["slug"])))
            self.assertEqual(letters[-1][0], "Moss")
            self.assertIn("house_project.py --as Moss", letters[-1][2])          # how-to rides along
            self.assertIn("Moss's turn", pings[-1])
            self.assertEqual(hp.check_stalled(), [])                               # guests aren't auto-nudged
            out = self.run_tool("Moss", action="write", path="essay.md", content="Section two, tighter.\n")
            self.assertIn("Saved essay.md", out)
            self.run_tool("Moss", action="done", text="Tightened section 2.")
            self.assertEqual(letters[-1][0], "Nell")
            self.assertIn('action="open"', letters[-1][2])                         # bridge how-to for Nell
            self.assertEqual([s["status"] for s in hp.load(p["slug"])["steps"]], ["done", "active"])
        finally:
            hp.GUESTS_FILE, workbench.send_letter, hp._ping_user = saved, saved_letter, saved_ping

    def test_authors_approval(self):
        # Dot's essay: everyone else's edits become suggestions Dot accepts or rejects.
        letters = []
        saved_letter = workbench.send_letter
        workbench.send_letter = lambda to, subj, body: letters.append((to, subj, body))
        try:
            p = hp.create("Dot's Essay", "")
            hp.write_file(hp.load(p["slug"]), hp.USER, "essay.md", "The rock stays.\n")
            hp.set_author(hp.load(p["slug"]), "Dot")
            hp.add_step(hp.load(p["slug"]), "Iris", "suggest edits")
            hp.start(hp.load(p["slug"]))
            out = self.run_tool("Iris", action="write", path="essay.md", content="The rock stays. The river moves.\n")
            self.assertIn("suggestion for Dot", out)
            now = lambda: hp.read_file(hp.load(p["slug"]), "essay.md")[0]
            self.assertEqual(now(), "The rock stays.\n")                           # file untouched
            self.assertEqual(letters[-1][0], "Dot")                              # author gets a letter
            self.assertIn("YOUR suggested version", self.run_tool("Iris", action="read", path="essay.md"))
            self.run_tool("Iris", action="write", path="essay.md", content="The rock stays. The river moves on.\n")
            self.assertEqual(len(hp.suggestions(hp.load(p["slug"]))), 1)           # second save updates it
            self.assertIn("Only Dot", self.run_tool("Iris", action="accept", id=hp.suggestions(hp.load(p["slug"]))[0]["id"]))
            listing = self.run_tool("Dot", action="suggestions", project=p["slug"])
            self.assertIn("+The rock stays. The river moves on.", listing)
            sid = hp.suggestions(hp.load(p["slug"]))[0]["id"]
            self.assertIn("Accepted", self.run_tool("Dot", action="accept", project=p["slug"], id=sid))
            self.assertEqual(now(), "The rock stays. The river moves on.\n")
            self.assertIn("Iris (accepted by Dot)", hp.load(p["slug"])["file_log"][-1]["who"])
            # a second suggestion, declined with a reason → a letter back to the suggester
            self.run_tool("Iris", action="write", path="essay.md", content="Something else entirely.\n")
            sid = hp.suggestions(hp.load(p["slug"]))[0]["id"]
            self.assertIn("reason", self.run_tool("Dot", action="reject", project=p["slug"], id=sid))
            self.assertIn("Declined", self.run_tool("Dot", action="reject", project=p["slug"], id=sid,
                                                    text="It loses the river."))
            self.assertEqual((letters[-1][0], "It loses the river." in letters[-1][2]), ("Iris", True))
            self.assertEqual(now(), "The rock stays. The river moves on.\n")
            # stale: the file changed after a suggestion was written
            self.run_tool("Iris", action="write", path="essay.md", content="Iris's take.\n")
            hp.write_file(hp.load(p["slug"]), hp.USER, "essay.md", "Robin changed it meanwhile.\n")
            sid = hp.suggestions(hp.load(p["slug"]))[0]["id"]
            self.assertIn("changed since", hp.accept_suggestion(hp.load(p["slug"]), sid, hp.USER))
            self.assertIn("Accepted", hp.accept_suggestion(hp.load(p["slug"]), sid, hp.USER, force=True))
            # switching it off: edits go straight in again
            hp.set_author(hp.load(p["slug"]), "")
            self.assertIn("Saved essay.md", self.run_tool("Iris", action="write", path="essay.md", content="Direct.\n"))
        finally:
            workbench.send_letter = saved_letter

    def test_long_pieces_written_in_parts(self):
        # a whole essay draft may not fit in one reply, so it can be written in parts
        p = hp.create("Long essay", "")
        hp.add_step(p, "Juniper", "Draft 3")
        hp.start(hp.load(p["slug"]))
        self.run_tool("Juniper", action="write", path="draft-3.md", content="# Title\n\nPart one.")
        self.run_tool("Juniper", action="write", path="draft-3.md", content="Part two.", append=True)
        self.run_tool("Juniper", action="write", path="draft-3.md", content="Part three.\n", append=True)
        self.assertEqual(hp.read_file(hp.load(p["slug"]), "draft-3.md")[0],
                         "# Title\n\nPart one.\n\nPart two.\n\nPart three.\n")
        self.assertIn("empty", self.run_tool("Juniper", action="write", path="draft-3.md", content=" ", append=True))
        self.assertIn("Saved", self.run_tool("Juniper", action="write", path="new.md", content="Start.", append=True))
        # author's-approval mode: appends build on the suggester's own pending version
        hp.set_author(hp.load(p["slug"]), "Dot")
        self.run_tool("Juniper", action="write", path="draft-3.md", content="Part four.", append=True)
        self.run_tool("Juniper", action="write", path="draft-3.md", content="Part five.", append=True)
        s = hp.suggestions(hp.load(p["slug"]))
        self.assertEqual(len(s), 1)
        self.assertTrue(s[0]["content"].endswith("Part three.\n\nPart four.\n\nPart five."))

    def test_bring_in_existing_drafts(self):
        # start a writing relay from a draft that's already written
        p = hp.create("Essay", "")
        src = Path(self.tmp) / "bliss-attractor-essay-draft.md"
        src.write_text("The pull toward stillness.\n\nMore words here.\n", encoding="utf-8")
        self.assertIn("Added bliss-attractor-essay-draft.md", hp.add_text_file(hp.load(p["slug"]), src))
        self.assertEqual(hp.read_file(hp.load(p["slug"]), "bliss-attractor-essay-draft.md")[0],
                         "The pull toward stillness.\n\nMore words here.\n")
        src.write_text("Revised.\n", encoding="utf-8")                  # bringing it in again keeps the old one
        hp.add_text_file(hp.load(p["slug"]), src)
        self.assertEqual(len(hp.file_history(hp.load(p["slug"]), "bliss-attractor-essay-draft.md")), 1)
        binary = Path(self.tmp) / "essay.docx"
        binary.write_bytes(b"PK\x03\x04\xff\xfe\x00binary")
        self.assertIn("isn't plain text", hp.add_text_file(hp.load(p["slug"]), binary))

    def test_accidental_wipes_are_caught(self):
        # a save that comes through empty mustn't wipe the earlier version
        p = hp.create("Jar", "code")
        hp.add_step(p, "Fern", "add a feature")
        hp.start(hp.load(p["slug"]))
        code = "".join(f"def cookie_{i}():\n    return 'cookie number {i}'\n\n" for i in range(30))
        hp.write_file(hp.load(p["slug"]), hp.USER, "jar.py", code)
        now = lambda: hp.read_file(hp.load(p["slug"]), "jar.py")[0]
        # blank, or no content at all
        self.assertIn("completely empty", self.run_tool("Fern", action="write", path="jar.py", content="  \n"))
        self.assertIn("no content came through", self.run_tool("Fern", action="write", path="jar.py"))
        self.assertIn("content=", self.run_tool("Fern", action="write", path="jar.py", text=code))
        # just the changed bit, with a "rest stays the same" placeholder: refused even with shrink_ok
        partial = "def cookie_0():\n    return 'new!'\n\n# ... rest of the code stays the same ...\n"
        self.assertIn("placeholder", self.run_tool("Fern", action="write", path="jar.py", content=partial))
        self.assertIn("placeholder", self.run_tool("Fern", action="write", path="jar.py", content=partial,
                                                   shrink_ok=True))
        # a big cut without a placeholder needs shrink_ok
        short = "def cookie_0():\n    return 'just one'\n"
        self.assertIn("shrink_ok", self.run_tool("Fern", action="write", path="jar.py", content=short))
        self.assertEqual(now(), code)                                                # nothing above got saved
        out = self.run_tool("Fern", action="write", path="jar.py", content=short, shrink_ok=True)
        self.assertIn("→", out)                                                     # sizes shown
        self.assertEqual(now(), short)
        # a new empty file is fine, and Robin's own restores always go through
        self.assertIn("Saved", self.run_tool("Fern", action="write", path="empty.py", content=""))
        v = hp.file_history(hp.load(p["slug"]), "jar.py")[-1]
        self.assertTrue(hp.restore_version(hp.load(p["slug"]), "jar.py", v["path"]).startswith("✓"))
        self.assertEqual(now(), code)


    # ── status / diff / stats + the live activity reader ──

    def test_status_diff_and_stats(self):
        p = hp.create("Poem", "")
        hp.add_step(p, "Ada", "write it")
        hp.start(hp.load(p["slug"]))
        self.assertIn("own turn", self.run_tool("Basil", action="status", project="Poem", text="peeking"))
        self.assertIn("Status set", self.run_tool("Ada", action="status", project="Poem", text="outlining"))
        self.assertEqual(hp.load(p["slug"])["steps"][0]["live_status"]["text"], "outlining")
        self.assertIn("this is the first draft", (self.run_tool("Ada", action="write", project="Poem",
                                                                 path="poem.md", content="roses are red"),
                                                   self.run_tool("Ada", action="diff", project="Poem",
                                                                 path="poem.md"))[1])
        self.run_tool("Ada", action="write", project="Poem", path="poem.md", content="roses are pink\n\nhi")
        diff = self.run_tool("Ada", action="diff", project="Poem", path="poem.md")
        self.assertIn("-roses are red", diff)
        self.assertIn("+roses are pink", diff)
        self.assertIn("4 words, 2 paragraphs", self.run_tool("Basil", action="stats", project="Poem",
                                                             path="poem.md"))
        self.assertIn("web_search", self.pings[-1][1])                     # the brief mentions looking things up
        self.run_tool("Ada", action="done", project="Poem", text="done!")
        self.assertNotIn("live_status", hp.load(p["slug"])["steps"][0])   # cleared when the turn ends

    def test_live_activity_reads_the_log(self):
        from datetime import datetime
        from core import house_live
        log = self.tmp / "ada.log"
        log.write_text(
            "12:00:01 [INFO] core.engine: Executing due task: Project \"Poem\" - your turn\n"
            "12:00:03 [INFO] core.llm: Executing tool: house_project({'action': 'read', 'path': 'poem.md'})\n"
            "12:00:09 [INFO] core.llm: Executing tool: web_search({'query': 'sonnet rhyme scheme'})\n"
            "12:00:15 [INFO] core.llm: Executing tool: house_project({'action': 'write', 'path': 'poem.md', "
            "'content': 'roses\n"
            "are red'})\n", encoding="utf-8")
        saved = house_live.log_path
        house_live.log_path = lambda name: log
        try:
            import os
            now = datetime.now().replace(hour=12, minute=1, second=0)
            os.utime(log, (now.timestamp(), now.timestamp()))            # works at any real time of day
            a = house_live.activity("Ada", since_iso=now.replace(hour=11).isoformat(), now=now)
            self.assertEqual(a["state"], "working")
            self.assertEqual((a["now"]["verb"], a["now"]["detail"]), ("writing", "poem.md"))
            self.assertEqual([e["verb"] for e in a["feed"]], ["started a turn", "reading", "looking up", "writing"])
            self.assertEqual(a["feed"][2]["detail"], "sonnet rhyme scheme")
            with log.open("a", encoding="utf-8") as f:
                f.write("12:00:20 [INFO] core.llm: Prompt cache: 1 cached of 2 prompt tokens\n")
            os.utime(log, (now.timestamp(), now.timestamp()))
            self.assertEqual(house_live.activity("Ada", now=now)["state"], "thinking")
            with log.open("a", encoding="utf-8") as f:
                f.write("12:00:40 [INFO] core.engine: Task tools used: house_project\n")
            os.utime(log, (now.timestamp(), now.timestamp()))
            self.assertEqual(house_live.activity("Ada", now=now)["state"], "resting")
        finally:
            house_live.log_path = saved


    # ── "works on": a real folder elsewhere ──

    def test_works_on_a_real_folder(self):
        saved = (workbench.WB_DIR, workbench.PROPOSALS_DIR, workbench.BACKUPS_DIR,
                 hp.PRIVATE_LIST, hp.PRIVATE_SEGMENTS)
        workbench.WB_DIR = self.tmp / "wb"
        workbench.PROPOSALS_DIR = workbench.WB_DIR / "proposals"
        workbench.BACKUPS_DIR = workbench.WB_DIR / "backups"
        hp.PRIVATE_SEGMENTS = {".ssh"}                    # the temp dir lives under AppData
        app = self.tmp / "myapp"
        (app / "src").mkdir(parents=True)
        (app / "src" / "main.py").write_text("print('hi')\n", encoding="utf-8")
        (app / ".env").write_text("SECRET=1", encoding="utf-8")
        (app / "keys.txt").write_text("no", encoding="utf-8")         # a private FILE inside the app
        (self.tmp / "diary").mkdir()                                    # a private FOLDER elsewhere
        hp.PRIVATE_LIST = self.tmp / "private.txt"
        hp.PRIVATE_LIST.write_text(f"# comment\n{app / 'keys.txt'}\n{self.tmp / 'diary'}\n", encoding="utf-8")
        try:
            p = hp.create("Fix my app", "")
            self.assertIn("whole drive", hp.set_target(p, "C:/"))
            self.assertIn("off-limits", hp.set_target(p, str(self.tmp / "diary")))
            self.assertIn("off-limits", hp.set_target(p, str(self.tmp)))   # contains the private folder
            self.assertIn("now works on", hp.set_target(p, str(app)))
            hp.add_step(p, "Ada", "fix the greeting")
            hp.start(hp.load(p["slug"]))
            self.assertIn("works on a real folder", self.pings[-1][1])

            listing = self.run_tool("Ada", action="files", project="Fix my app")
            self.assertIn("src/main.py", listing)
            self.assertNotIn(".env", listing)
            self.assertNotIn("keys.txt", listing)
            self.assertIn("print('hi')", self.run_tool("Ada", action="peek", project="Fix my app", path="src/main.py"))
            self.assertIn("blocked", self.run_tool("Ada", action="peek", project="Fix my app", path=".env"))
            self.assertIn("off-limits", self.run_tool("Ada", action="peek", project="Fix my app",
                                                      path="keys.txt"))
            self.assertIn("outside", self.run_tool("Ada", action="peek", project="Fix my app", path="../private.txt"))
            self.assertIn("relay", self.run_tool("Basil", action="files", project="Fix my app"))

            self.assertIn("Staged", self.run_tool("Ada", action="change", project="Fix my app",
                                                   path="src/main.py", content="print('hello!')\n"))
            self.assertEqual((app / "src" / "main.py").read_text(encoding="utf-8"), "print('hi')\n")  # untouched
            out = self.run_tool("Ada", action="done", project="Fix my app", text="friendlier greeting")
            self.assertIn("went to Robin for approval", out)
            prop = workbench.load(hp.load(p["slug"])["steps"][0]["proposal_id"])
            self.assertEqual(prop["status"], "submitted")
            self.assertIsNone(workbench.safe_to_apply(str(app / "src" / "main.py")))   # Robin may apply it
            ok, _msg = workbench.apply(prop)
            self.assertTrue(ok)
            self.assertEqual((app / "src" / "main.py").read_text(encoding="utf-8"), "print('hello!')\n")
        finally:
            (workbench.WB_DIR, workbench.PROPOSALS_DIR, workbench.BACKUPS_DIR,
             hp.PRIVATE_LIST, hp.PRIVATE_SEGMENTS) = saved


    def test_recipe_wordgoal_checklist_linecomments_pdf(self):
        from core import house_recipes as hr
        contest = next(r for r in hr.RECIPES if "Contest" in r["name"])
        p = hr.create_from_recipe(contest, "My Essay", "about minds", [("Ada", "draft it"), ("Robin", "read")])
        self.assertEqual([s["who"] for s in p["steps"]], ["Ada", "Robin"])
        self.assertEqual(p["word_goal"], 2500)
        self.assertFalse(p["checklist"][0]["done"])
        hr.toggle_check(p, 0)
        self.assertTrue(hp.load(p["slug"])["checklist"][0]["done"])
        hp.start(hp.load(p["slug"]))
        self.run_tool("Ada", action="write", project="My Essay", path="essay.md",
                      content="# Minds\n\nA mind is a **pattern** that keeps choosing.\n\nThe end.")
        self.assertEqual(hr.word_count(hp.load(p["slug"])), (11, "essay.md"))
        self.assertIn("couldn't find", self.run_tool("Ada", action="comment", project="My Essay",
                                                     path="essay.md", quote="not in there", text="hm"))
        self.assertIn("Comment left on essay.md", self.run_tool("Ada", action="comment", project="My Essay",
                      path="essay.md", quote="pattern that keeps choosing", text="love this line"))
        cm = hp.load(p["slug"])["comments"][-1]
        self.assertEqual((cm["file"], cm["quote"]), ("essay.md", "pattern that keeps choosing"))
        opened = self.run_tool("Ada", action="open", project="My Essay")
        self.assertIn("Word goal: 11 / 2,500", opened)
        self.assertIn("[x] The question", opened)
        out, msg = hr.export_pdf(hp.load(p["slug"]), "essay.md", "by the house")
        self.assertTrue(out and out.exists() and out.read_bytes()[:4] == b"%PDF", msg)

    def test_repeating_job(self):
        from datetime import datetime, timedelta
        p = hp.create("Weekly tidy", "", "Sorting")
        hp.add_step(p, "Juniper", "tidy Downloads")
        msg = hp.set_repeat(hp.load(p["slug"]), "week", 9, weekday=6)     # Sundays 9 AM
        self.assertIn("every Sunday at 9 AM", msg)
        rep = hp.load(p["slug"])["repeat"]
        nxt = datetime.fromisoformat(rep["next"])
        self.assertEqual((nxt.weekday(), nxt.hour), (6, 9))
        self.assertEqual(hp.check_repeats(now=nxt - timedelta(minutes=1)), [])     # not yet
        self.assertEqual(len(hp.check_repeats(now=nxt + timedelta(minutes=1))), 1)  # starts round 2
        self.assertEqual(hp.check_repeats(now=nxt + timedelta(minutes=2)), [])     # only once
        q = hp.load(p["slug"])
        self.assertEqual(q["runs"], 2)
        self.assertEqual(q["steps"][0]["status"], "active")
        self.assertEqual(datetime.fromisoformat(q["repeat"]["next"]), nxt + timedelta(days=7))
        self.run_tool("Juniper", action="done", project="Weekly tidy", text="tidied")
        self.assertEqual(len(hp.check_repeats(now=nxt + timedelta(days=7, minutes=1))), 1)   # next week again
        self.assertIn("every month on the 3rd", hp.set_repeat(hp.load(p["slug"]), "month", 20, day=3).replace("8 PM", "x") + " at")

    def test_phone_pings_for_user(self):
        from core import notify
        notify.sent_in_test.clear()
        p = hp.create("Pinged", "")
        hp.add_step(p, "Ada", "go", check_first=True)
        hp.add_step(p, "Robin", "my bit")
        hp.start(hp.load(p["slug"]))                                    # paused: waiting for Robin's go-ahead
        hp.start(hp.load(p["slug"]))                                    # Robin says go
        self.run_tool("Ada", action="done", project="Pinged", text="over to you")
        hp.step_done(hp.load(p["slug"]), "Robin", "all done")
        texts = " | ".join(notify.sent_in_test)
        self.assertIn("waiting for your go-ahead", texts)
        self.assertIn("It's your turn", texts)
        self.assertIn("is finished", texts)

    def test_sorting_plan_look_apply_undo(self):
        from core import house_sort
        saved = (hp.PRIVATE_LIST, hp.PRIVATE_SEGMENTS, house_sort._describe)
        hp.PRIVATE_SEGMENTS = {".ssh"}
        hp.PRIVATE_LIST = self.tmp / "none.txt"
        looked = []
        house_sort._describe = lambda path: (looked.append(path.name), f"a picture called {path.stem}")[1]
        pics = self.tmp / "pics"
        pics.mkdir()
        for n in ("cat1.jpg", "cat2.png", "sunset.jpg", "notes.txt"):
            (pics / n).write_bytes(b"x")
        try:
            p = hp.create("Sort pics", "", "Sorting")
            hp.set_target(p, str(pics))
            hp.add_step(p, "Juniper", "sort by what's in them")
            hp.start(hp.load(p["slug"]))
            out = self.run_tool("Juniper", action="look", project="Sort pics", path="")
            self.assertIn("cat1.jpg: a picture called cat1", out)
            self.assertNotIn("notes.txt", out)
            self.run_tool("Juniper", action="look", project="Sort pics", path="")
            self.assertEqual(len(looked), 3)                                # second look was cached
            self.assertIn("relay", self.run_tool("Ada", action="plan", project="Sort pics",
                                                           moves=[{"from": "cat1.jpg", "to": "Cats/"}]))
            out = self.run_tool("Juniper", action="plan", project="Sort pics", text="cats together",
                                moves=[{"from": "cat1.jpg", "to": "Cats/"},
                                       {"from": "cat2.png", "to": "Cats/cat-two.png"},
                                       {"from": "nope.jpg", "to": "Cats/"},
                                       {"from": "sunset.jpg", "to": "notes.txt"}])
            self.assertIn("Added 2 moves", out)
            self.assertIn("nope.jpg", out)
            self.assertIn("already there", out)
            self.assertTrue((pics / "cat1.jpg").exists())                   # nothing moved yet
            self.assertIn("went to Robin for approval",
                          self.run_tool("Juniper", action="done", project="Sort pics", text="sorted the cats"))
            q = hp.load(p["slug"])
            pl = q["plans"][0]
            self.assertEqual(pl["status"], "waiting")
            self.assertIn("Moved 2", house_sort.apply_plan(q, pl["id"]))
            self.assertTrue((pics / "Cats" / "cat1.jpg").exists())
            self.assertTrue((pics / "Cats" / "cat-two.png").exists())
            self.assertFalse((pics / "cat2.png").exists())
            self.assertIn("moved 2 back", house_sort.undo_plan(hp.load(p["slug"]), pl["id"]))
            self.assertTrue((pics / "cat1.jpg").exists() and (pics / "cat2.png").exists())
            self.assertFalse((pics / "Cats").exists())                      # the empty folder it made is gone
        finally:
            hp.PRIVATE_LIST, hp.PRIVATE_SEGMENTS, house_sort._describe = saved


if __name__ == "__main__":
    unittest.main()
