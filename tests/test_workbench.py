"""
Tests for the Workbench (core/workbench.py + approval mode in skills/code.py).

Everything runs in a temp folder: the Workbench's data dirs, the letters
folder and the "project" being edited are all redirected, so no real file,
proposal or letter is touched.

Run:
    py -3.14 -m unittest tests.test_workbench -v
"""

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

_PULSE_ROOT = Path(__file__).resolve().parent.parent
if str(_PULSE_ROOT) not in sys.path:
    sys.path.insert(0, str(_PULSE_ROOT))

from core import workbench  # noqa: E402
from skills.code import CodeSkill  # noqa: E402


class FakeUsageDB:
    """Stands in for PulseDatabase's usage table."""
    def __init__(self):
        self.prompt = 1000
        self.completion = 100

    def get_usage_today(self, day):
        return [{"prompt_tokens": self.prompt, "completion_tokens": self.completion,
                 "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0, "calls": 5}]


class WorkbenchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.project = self.tmp / "project"
        self.project.mkdir()
        (self.project / "hello.py").write_text("def hi():\n    return 'hi'\n", encoding="utf-8")
        self._saved = (workbench.WB_DIR, workbench.PROPOSALS_DIR, workbench.BACKUPS_DIR,
                       workbench.LETTERS_DIR, workbench.APPLY_ROOTS)
        self._saved_ping = workbench.ping
        self.pings = []
        workbench.ping = lambda name, text: (self.pings.append(name), True)[1]   # never wake real companions
        workbench.WB_DIR = self.tmp / "wb"
        workbench.PROPOSALS_DIR = workbench.WB_DIR / "proposals"
        workbench.BACKUPS_DIR = workbench.WB_DIR / "backups"
        workbench.LETTERS_DIR = self.tmp / "letters"
        workbench.APPLY_ROOTS = [self.project]
        self.db = FakeUsageDB()

    def tearDown(self):
        (workbench.WB_DIR, workbench.PROPOSALS_DIR, workbench.BACKUPS_DIR,
         workbench.LETTERS_DIR, workbench.APPLY_ROOTS) = self._saved
        workbench.ping = self._saved_ping
        shutil.rmtree(self.tmp, ignore_errors=True)

    def skill(self, name, approval=True, writable=True):
        cfg = {"skills": {"code": {"allowed_roots": [str(self.project)],
                                   "writable_roots": [str(self.project)] if writable else [],
                                   "approval": approval}},
               "channels": {"lor": {"author_name": name}},
               "_db": self.db, "paths": {"database": f"personas/{name}/data/{name}.db"}}
        return CodeSkill(cfg)

    def letters(self):
        return sorted(p.name for p in workbench.LETTERS_DIR.glob("letter_*.json")) \
            if workbench.LETTERS_DIR.exists() else []

    # ── staging ──────────────────────────────────────────────────────

    def test_edit_is_staged_not_written(self):
        ada = self.skill("Ada")
        out = ada.execute("edit_file", {"path": str(self.project / "hello.py"),
                                         "old_text": "'hi'", "new_text": "'hello'"})
        self.assertIn("Staged", out)
        self.assertIn("'hi'", (self.project / "hello.py").read_text())   # real file untouched
        read = ada.execute("read_code_file", {"path": str(self.project / "hello.py")})
        self.assertIn("STAGED", read)
        self.assertIn("'hello'", read)

    def test_second_edit_builds_on_staged_version(self):
        ada = self.skill("Ada")
        path = str(self.project / "hello.py")
        ada.execute("edit_file", {"path": path, "old_text": "'hi'", "new_text": "'hello'"})
        out = ada.execute("edit_file", {"path": path, "old_text": "'hello'", "new_text": "'hey'"})
        self.assertIn("Staged", out)
        d = workbench.get_draft("Ada")
        self.assertIn("'hey'", workbench.staged_text(d, path))
        self.assertEqual(len(d["files"]), 1)

    def test_new_file_staged(self):
        ada = self.skill("Ada")
        new = self.project / "new.py"
        ada.execute("write_file", {"path": str(new), "content": "x = 1\n"})
        self.assertFalse(new.exists())
        self.assertIn("x = 1", ada.execute("read_code_file", {"path": str(new)}))

    def test_approval_off_keeps_old_behaviour(self):
        direct = self.skill("Dot", approval=False)
        direct.execute("edit_file", {"path": str(self.project / "hello.py"),
                                     "old_text": "'hi'", "new_text": "'direct'"})
        self.assertIn("'direct'", (self.project / "hello.py").read_text())

    def test_play_space_is_exempt_from_approval(self):
        play = self.project / "playground" / "ada"
        play.mkdir(parents=True)
        cfg = {"skills": {"code": {"allowed_roots": [str(self.project)],
                                   "writable_roots": [str(self.project)],
                                   "approval_exempt": [str(self.project / "playground")]}},
               "channels": {"lor": {"author_name": "Ada"}}, "_db": self.db, "paths": {}}
        ada = CodeSkill(cfg)
        toy = play / "toy.py"
        out = ada.execute("write_file", {"path": str(toy), "content": "print('play')\n"})
        self.assertTrue(toy.exists(), out)                                 # written directly
        self.assertIsNone(workbench.get_draft("Ada"))
        ada.execute("edit_file", {"path": str(self.project / "hello.py"),
                                   "old_text": "'hi'", "new_text": "'hello'"})
        self.assertIn("'hi'", (self.project / "hello.py").read_text())    # real project: staged

    def test_read_only_persona_cannot_stage(self):
        ro = self.skill("Clover", writable=False)
        out = ro.execute("edit_file", {"path": str(self.project / "hello.py"),
                                       "old_text": "'hi'", "new_text": "'x'"})
        self.assertIn("Refused", out)
        self.assertIsNone(workbench.get_draft("Clover"))

    # ── submit / review / tokens ─────────────────────────────────────

    def test_full_flow_submit_review_apply_undo(self):
        ada, dot = self.skill("Ada"), self.skill("Dot")
        path = self.project / "hello.py"
        ada.execute("read_code_file", {"path": str(path)})            # work starts here
        self.db.prompt += 5000; self.db.completion += 300                # ...tokens spent
        ada.execute("edit_file", {"path": str(path), "old_text": "'hi'", "new_text": "'hello'"})
        out = ada.execute("proposal", {"action": "submit", "title": "Friendlier greeting",
                                        "why": "hi is too short", "reviewer": "Dot"})
        self.assertIn("Submitted", out)
        prop = workbench.all_proposals()[0]
        self.assertEqual(prop["status"], "submitted")
        self.assertEqual(prop["tokens"]["Ada"]["used"]["prompt"], 5000)
        self.assertEqual(prop["checks"]["results"][str(path.resolve())], "ok")
        self.assertEqual(len(self.letters()), 1)                          # Dot was asked
        self.assertEqual(self.pings, ["Dot"])                           # ...and pinged

        self.assertIn("'hello'", dot.execute("proposal", {"action": "read", "id": prop["id"]}))
        self.assertIn("can't review your own",
                      ada.execute("proposal", {"action": "review", "id": prop["id"],
                                                "verdict": "looks good", "notes": "me!"}))
        out = dot.execute("proposal", {"action": "review", "id": prop["id"],
                                         "verdict": "looks good", "notes": "Clean and small."})
        self.assertIn("Review left", out)

        prop = workbench.load(prop["id"])
        ok, msg = workbench.apply(prop)
        self.assertTrue(ok, msg)
        self.assertIn("'hello'", path.read_text())
        self.assertEqual(len(self.letters()), 2)                          # Ada told it's approved

        ok, msg = workbench.undo(workbench.load(prop["id"]))
        self.assertTrue(ok, msg)
        self.assertIn("'hi'", path.read_text())

    def test_conflict_blocks_apply(self):
        ada = self.skill("Ada")
        path = self.project / "hello.py"
        ada.execute("edit_file", {"path": str(path), "old_text": "'hi'", "new_text": "'hello'"})
        ada.execute("proposal", {"action": "submit", "title": "x"})
        path.write_text("someone else changed it\n", encoding="utf-8")
        ok, msg = workbench.apply(workbench.all_proposals()[0])
        self.assertFalse(ok)
        self.assertIn("changed on disk", msg)
        self.assertEqual(path.read_text(), "someone else changed it\n")

    def test_undo_refuses_if_file_changed_after_apply(self):
        ada = self.skill("Ada")
        path = self.project / "hello.py"
        ada.execute("edit_file", {"path": str(path), "old_text": "'hi'", "new_text": "'hello'"})
        ada.execute("proposal", {"action": "submit", "title": "x"})
        prop = workbench.all_proposals()[0]
        workbench.apply(prop)
        path.write_text("later work\n", encoding="utf-8")
        ok, msg = workbench.undo(workbench.load(prop["id"]))
        self.assertFalse(ok)
        self.assertEqual(path.read_text(), "later work\n")

    def test_new_file_apply_and_undo_removes_it(self):
        ada = self.skill("Ada")
        new = self.project / "sub" / "new.py"
        ada.execute("write_file", {"path": str(new), "content": "x = 1\n"})
        ada.execute("proposal", {"action": "submit", "title": "new file"})
        prop = workbench.all_proposals()[0]
        self.assertTrue(workbench.apply(prop)[0])
        self.assertTrue(new.exists())
        self.assertTrue(workbench.undo(workbench.load(prop["id"]))[0])
        self.assertFalse(new.exists())

    def test_syntax_error_is_flagged(self):
        ada = self.skill("Ada")
        ada.execute("write_file", {"path": str(self.project / "bad.py"), "content": "def (:\n"})
        out = ada.execute("proposal", {"action": "submit", "title": "oops"})
        self.assertIn("syntax check found a problem", out)
        self.assertFalse(workbench.checks_ok(workbench.all_proposals()[0]))

    def test_reject_and_discard(self):
        ada = self.skill("Ada")
        path = self.project / "hello.py"
        ada.execute("edit_file", {"path": str(path), "old_text": "'hi'", "new_text": "'yo'"})
        self.assertIn("Discarded", ada.execute("proposal", {"action": "discard"}))
        self.assertEqual(workbench.all_proposals(), [])
        ada.execute("edit_file", {"path": str(path), "old_text": "'hi'", "new_text": "'yo'"})
        ada.execute("proposal", {"action": "submit", "title": "yo"})
        ok, _ = workbench.reject(workbench.all_proposals()[0], note="keep hi")
        self.assertTrue(ok)
        self.assertIn("'hi'", path.read_text())

    def test_apply_refuses_unsafe_paths(self):
        self.assertIsNotNone(workbench.safe_to_apply(str(self.project / ".env")))
        self.assertIsNotNone(workbench.safe_to_apply(str(self.tmp / "elsewhere.py")))
        self.assertIsNone(workbench.safe_to_apply(str(self.project / "fine.py")))

    def test_real_ping_adds_a_due_task_to_their_own_db(self):
        from core.db import PulseDatabase
        fake_root = self.tmp / "pulse"
        (fake_root / "personas" / "dot" / "data").mkdir(parents=True)
        (fake_root / "personas" / "shared").mkdir(parents=True)
        db_file = fake_root / "personas" / "dot" / "data" / "dot.db"
        PulseDatabase(db_file)                                             # creates the tables
        (fake_root / "personas" / "dot" / "config.yaml").write_text(
            'channels:\n  lor:\n    author_name: "Dot"\n'
            'paths:\n  database: "personas/dot/data/dot.db"\n', encoding="utf-8")
        (fake_root / "personas" / "shared" / "config.yaml").write_text(
            'channels:\n  lor:\n    author_name: "Hazel"\n', encoding="utf-8")   # no own DB
        saved_root = workbench.PULSE_ROOT
        workbench.PULSE_ROOT = fake_root
        try:
            self.assertTrue(self._saved_ping("Dot", "please review, only if you feel up to it"))
            self.assertFalse(self._saved_ping("Hazel", "x"))               # shared DB → letter only
            self.assertFalse(self._saved_ping("Nobody", "x"))
        finally:
            workbench.PULSE_ROOT = saved_root
        rows = PulseDatabase(db_file).get_schedules()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["schedule_type"], "once")
        self.assertIn("only if you feel up to it", rows[0]["task"])

    def test_tool_count_is_one_extra(self):
        names = [t["function"]["name"] for t in self.skill("Ada").get_tools()]
        self.assertEqual(names.count("proposal"), 1)
        self.assertEqual(len(names), 9)   # the 8 original code tools + proposal


if __name__ == "__main__":
    unittest.main()
