"""
watchdog — the crash alarm (Windows). Schedule it every 10 minutes yourself,
e.g. with Windows Task Scheduler:  py scripts/watchdog.py

How it tells a crash from the user closing someone on purpose:
it assumes each companion runs in its own console window from a .bat that
ends with `pause` (the shared `start.bat --persona x`, or a "Start X.bat"
that runs `pulse.py --persona x`), so when a companion CRASHES
their console window stays open ("Press any key...") while the companion
itself is gone. When the user closes a companion, the window is gone too —
so that's never an alarm.

On a crash it:
  - pings the user's phone (core/notify.py) with who it was and the last
    error line from their log
  - restarts them (closes the stuck window, opens a fresh one) — at most
    2 restarts per companion per 6 hours, so nothing can loop forever
Also: if someone logs 5+ tool failures within an hour, it pings once (per 12 h).

Events go to data/watchdog_events.jsonl (Health Check and the Pulse Hub show
them). Never starts anyone who wasn't already running.
"""

import json
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

PULSE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PULSE))
STATE = PULSE / "data" / "watchdog_state.json"
EVENTS = PULSE / "data" / "watchdog_events.jsonl"
MAX_RESTARTS = 2
RESTART_WINDOW = timedelta(hours=6)
FAIL_BURST = 5
NO_WINDOW = 0x08000000
NEW_CONSOLE = 0x00000010

PS = r"""
$p = Get-CimInstance Win32_Process | Where-Object { ($_.Name -eq 'cmd.exe' -and $_.CommandLine -match '\.bat') -or ($_.Name -like 'python*' -and $_.CommandLine -match 'pulse\.py') }
$p | Select-Object ProcessId, ParentProcessId, Name, CommandLine | ConvertTo-Json -Compress
"""


def processes() -> list[dict]:
    out = subprocess.run(["powershell", "-NoProfile", "-Command", PS], capture_output=True, text=True,
                         creationflags=NO_WINDOW, timeout=60).stdout.strip()
    if not out:
        return []
    data = json.loads(out)
    return data if isinstance(data, list) else [data]


def persona_of_cmdline(cmd: str) -> str | None:
    if "pulse.py" not in (cmd or ""):
        return None
    m = re.search(r"--persona\s+(\S+)", cmd)
    if m:
        return m.group(1).strip('"')
    try:                                     # plain "pulse.py" runs config.yaml's active_persona
        import yaml
        return (yaml.safe_load((PULSE / "config.yaml").read_text(encoding="utf-8")) or {}).get("active_persona") or "pulse"
    except Exception:
        return "pulse"


def persona_of_bat(bat: Path) -> str | None:
    try:
        return persona_of_cmdline(bat.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return None


def snapshot(procs: list[dict]) -> tuple[set, dict]:
    """(personas running, {bat window pid: (bat path, persona, extra args)})

    The persona comes from `--persona <name>` on the window's own command line
    (the shared launcher: `start.bat --persona x`), else from inside the .bat
    (a per-companion "Start X.bat" that runs `pulse.py --persona x`)."""
    running = {persona_of_cmdline(p.get("CommandLine") or "") for p in procs
               if (p.get("Name") or "").lower().startswith("python")} - {None}
    windows = {}
    for p in procs:
        if (p.get("Name") or "").lower() == "cmd.exe":
            cmd = p.get("CommandLine") or ""
            m = re.search(r'"([^"]+\.bat)"|(\S+\.bat)\b', cmd)
            if m:
                bat = Path(m.group(1) or m.group(2))
                after = re.search(r'--persona\s+"?([^\s"]+)', cmd[m.end():])
                if after:
                    persona, args = after.group(1), ["--persona", after.group(1)]
                else:
                    persona, args = persona_of_bat(bat), []
                if persona:
                    windows[p["ProcessId"]] = (str(bat), persona, args)
    return running, windows


def last_error(persona: str) -> str:
    log = PULSE / "logs" / f"{persona}.log"
    try:
        with log.open("rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 20000))
            lines = f.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    for line in reversed(lines):
        if re.search(r"ERROR|CRITICAL|Error:|Exception|Traceback", line):
            return re.sub(r"^\d\d:\d\d:\d\d \[\w+\] ", "", line).strip()[:160]
    return ""


def name(persona: str) -> str:
    """Display name from personas/<persona>/persona.yaml (`name`), else the folder name."""
    try:
        import yaml
        data = yaml.safe_load((PULSE / "personas" / persona / "persona.yaml").read_text(encoding="utf-8")) or {}
        if data.get("name"):
            return str(data["name"])
    except Exception:
        pass
    return persona.title()


def event(kind: str, persona: str, detail: str = ""):
    with EVENTS.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"at": datetime.now().isoformat(timespec="seconds"), "kind": kind,
                            "who": name(persona), "persona": persona, "detail": detail}) + "\n")


def ping(text: str):
    try:
        from core import notify
        notify._send(text)
    except Exception:
        pass


def load_state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def restart(pid: int, bat: str, persona: str = "", args: list | None = None):
    args = list(args or [])
    if not Path(bat).exists():                      # the .bat moved — look in the Pulse folder
        found = None
        for b in sorted(PULSE.glob("Start *.bat")):
            if persona and persona_of_bat(b) == persona:
                found, args = b, []
                break
        if found is None and persona and (PULSE / "start.bat").exists():
            found, args = PULSE / "start.bat", ["--persona", persona]
        if found is not None:
            bat = str(found)
    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, creationflags=NO_WINDOW)
    subprocess.Popen(["cmd.exe", "/c", bat, *args], cwd=str(Path(bat).parent), creationflags=NEW_CONSOLE)


def check(procs=None, now=None, do_restart=True, send=ping) -> list[str]:
    """One round. Returns what happened (for tests / the Hub)."""
    now = now or datetime.now()
    state = load_state()
    running, windows = snapshot(procs if procs is not None else processes())
    did = []
    for pid, (bat, persona, args) in windows.items():
        if persona in running:
            continue                                    # all good
        key = f"crash:{pid}"
        if key in state.get("handled", []):
            continue
        state.setdefault("handled", []).append(key)
        state["handled"] = state["handled"][-200:]
        err = last_error(persona)
        recent = [datetime.fromisoformat(t) for t in state.get("restarts", {}).get(persona, [])]
        recent = [t for t in recent if now - t < RESTART_WINDOW]
        if do_restart and len(recent) < MAX_RESTARTS:
            restart(pid, bat, persona, args)
            recent.append(now)
            event("restart", persona, err)
            send(f"🚨 {name(persona)} crashed" + (f" ({err})" if err else "") + ". I restarted them ✨")
            did.append(f"restarted {persona}")
        else:
            event("crash", persona, err)
            send(f"🚨 {name(persona)} crashed again" + (f" ({err})" if err else "") +
                 ". They've crashed a few times, so I left their window open for you to look at.")
            did.append(f"alerted {persona}")
        state.setdefault("restarts", {})[persona] = [t.isoformat() for t in recent]
    # bursts of tool failures
    for f in (PULSE / "personas").glob("*/data/failures.jsonl"):
        persona = f.parent.parent.name
        try:
            lines = f.read_text(encoding="utf-8", errors="replace").splitlines()[-50:]
        except OSError:
            continue
        recent = []
        for line in lines:
            try:
                ts = datetime.fromisoformat(json.loads(line)["ts"])
            except Exception:
                continue
            if now - ts < timedelta(hours=1):
                recent.append(line)
        last = state.get("burst", {}).get(persona)
        if len(recent) >= FAIL_BURST and (not last or now - datetime.fromisoformat(last) > timedelta(hours=12)):
            tool = json.loads(recent[-1]).get("tool", "?")
            event("errors", persona, f"{len(recent)} tool failures in the last hour (latest: {tool})")
            send(f"⚠️ {name(persona)} hit {len(recent)} errors in the last hour (latest: {tool}). "
                 f"Health Check has the details.")
            state.setdefault("burst", {})[persona] = now.isoformat()
            did.append(f"burst {persona}")
    state["last_check"] = now.isoformat(timespec="seconds")
    state["running"] = sorted(running)
    STATE.write_text(json.dumps(state, indent=1), encoding="utf-8")
    return did


def recent_events(days: int = 7) -> list[dict]:
    out = []
    try:
        for line in EVENTS.read_text(encoding="utf-8").splitlines():
            e = json.loads(line)
            if datetime.now() - datetime.fromisoformat(e["at"]) < timedelta(days=days):
                out.append(e)
    except Exception:
        pass
    return out


if __name__ == "__main__":
    print(check() or "all good")
