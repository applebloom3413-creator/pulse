"""Crash alarm (scripts/watchdog.py) with pretend processes — nothing real is touched."""
import json
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "scripts"))
import watchdog as w  # noqa: E402


class WatchdogTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.saved = (w.STATE, w.EVENTS, w.restart, w.persona_of_bat, w.PULSE)
        w.STATE, w.EVENTS = self.tmp / "state.json", self.tmp / "events.jsonl"
        self.restarted, self.sent = [], []
        w.restart = lambda pid, bat, persona='', args=None: self.restarted.append(pid)
        w.persona_of_bat = lambda bat: {"Start Ada.bat": "ada", "Start Basil.bat": "basil"}.get(Path(bat).name)
        w.PULSE = self.tmp                                   # no real logs / failures
        (self.tmp / "personas").mkdir()

    def tearDown(self):
        w.STATE, w.EVENTS, w.restart, w.persona_of_bat, w.PULSE = self.saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    def procs(self, ada_alive=True, ada_window=10, basil_window=None):
        p = [{"ProcessId": ada_window, "Name": "cmd.exe", "CommandLine": 'cmd /c ""C:\\p\\Start Ada.bat" "'}]
        if ada_alive:
            p.append({"ProcessId": 11, "Name": "python.exe", "CommandLine": "python pulse.py --persona ada"})
        if basil_window:
            p.append({"ProcessId": basil_window, "Name": "cmd.exe", "CommandLine": 'cmd /c ""C:\\p\\Start Basil.bat" "'})
        return p

    def run_check(self, procs, now=None):
        return w.check(procs=procs, now=now, send=self.sent.append)

    def test_running_is_quiet_and_closed_window_is_quiet(self):
        self.assertEqual(self.run_check(self.procs()), [])
        self.assertEqual(self.run_check([]), [])                 # user closed everyone: no alarm
        self.assertEqual(self.sent, [])

    def test_crash_restarts_once_per_window_and_gives_up_after_two(self):
        now = datetime(2026, 10, 3, 12, 0)
        self.assertEqual(self.run_check(self.procs(ada_alive=False), now), ["restarted ada"])
        self.assertEqual(self.run_check(self.procs(ada_alive=False), now), [])   # same window: handled
        self.assertIn("Ada crashed", self.sent[0])
        self.run_check(self.procs(ada_alive=False, ada_window=20), now + timedelta(minutes=10))
        out = self.run_check(self.procs(ada_alive=False, ada_window=30), now + timedelta(minutes=20))
        self.assertEqual(out, ["alerted ada"])                  # third crash in 6h: leave it for the user
        self.assertEqual(self.restarted, [10, 20])
        later = self.run_check(self.procs(ada_alive=False, ada_window=40), now + timedelta(hours=7))
        self.assertEqual(later, ["restarted ada"])              # window passed: restarts again
        kinds = [json.loads(l)["kind"] for l in w.EVENTS.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(kinds, ["restart", "restart", "crash", "restart"])

    def test_error_burst(self):
        d = self.tmp / "personas" / "basil" / "data"
        d.mkdir(parents=True)
        now = datetime(2026, 10, 3, 12, 0)
        d.joinpath("failures.jsonl").write_text("\n".join(json.dumps(
            {"ts": (now - timedelta(minutes=i)).isoformat(), "tool": "web_search"}) for i in range(6)), encoding="utf-8")
        self.assertEqual(self.run_check(self.procs(), now), ["burst basil"])
        self.assertEqual(self.run_check(self.procs(), now + timedelta(minutes=10)), [])   # once per 12h
        self.assertIn("Basil hit 6 errors", self.sent[0])

    def test_shared_launcher_reads_persona_from_command_line(self):
        win = {"ProcessId": 50, "Name": "cmd.exe",
               "CommandLine": 'cmd /c ""C:\\p\\start.bat" --persona clover"'}
        alive = {"ProcessId": 51, "Name": "python.exe",
                 "CommandLine": '"C:\\p\\.venv\\Scripts\\python.exe" "C:\\p\\pulse.py" --persona clover'}
        running, windows = w.snapshot([win, alive])
        self.assertEqual(running, {"clover"})
        self.assertEqual(windows[50][1:], ("clover", ["--persona", "clover"]))
        self.assertEqual(self.run_check([win, alive]), [])
        self.assertEqual(self.run_check([win]), ["restarted clover"])
        self.assertEqual(self.restarted, [50])


if __name__ == "__main__":
    unittest.main()
