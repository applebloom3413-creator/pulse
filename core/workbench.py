"""
Workbench — code changes as proposals that the user approves.

When a companion's code skill is in approval mode (the default), edit_file /
write_file don't touch the real files. The change is staged into that
companion's *draft* proposal instead. The companion submits it (optionally
asking a specific sibling to review), the reviewer reads the diff and leaves
notes, and the user approves or rejects it in the Workbench window
(scripts/workbench_gui.pyw). Approving applies it — with a conflict check and
a backup — and it can be undone.

Storage: data/workbench/proposals/<id>.json, backups in
data/workbench/backups/<id>/. One JSON file per proposal; writes are atomic.

Used by: skills/code.py (staging, submit, review tools) and
scripts/workbench_gui.pyw (the user's window). Pure stdlib + optional PyYAML.

Who's who comes from config, never from hardcoded names:
  - the user's name: config.yaml `user_name`, else the main persona's
    persona.yaml `user_name`, else "User" (see user_name()).
  - the companions: every personas/<folder>/ (not _template), by their
    channels.lor.author_name (see companion_names()).
  - folders an approved change may land in: PULSE_CODE_ROOTS (paths joined
    with os.pathsep), default just this Pulse folder (see code_roots()).
"""

import hashlib
import json
import os
import time
from datetime import date, datetime, timedelta
from pathlib import Path

PULSE_ROOT = Path(__file__).resolve().parent.parent
WB_DIR = PULSE_ROOT / "data" / "workbench"
PROPOSALS_DIR = WB_DIR / "proposals"
BACKUPS_DIR = WB_DIR / "backups"
LETTERS_DIR = PULSE_ROOT / "data" / "letters"

WORKBENCH_SENDER = "Workbench"



def code_roots() -> list[Path]:
    """The code folders companions may work on: PULSE_CODE_ROOTS (split on os.pathsep),
    default just this Pulse folder."""
    raw = os.environ.get("PULSE_CODE_ROOTS", "")
    roots = [Path(r.strip()) for r in raw.split(os.pathsep) if r.strip()]
    return roots or [PULSE_ROOT]


# Where an approved change is ever allowed to land, no matter what a
# proposal file says (defence in depth — the code skill already scopes paths).
APPLY_ROOTS = code_roots()
NEVER_APPLY_SEGMENTS = {".git", "node_modules", "__pycache__", ".venv", "venv"}

STATUSES = ("draft", "submitted", "applied", "rejected", "undone")
VERDICTS = ("looks good", "needs changes", "has concerns")


# ── who's who (from config) ─────────────────────────────────────────────

def _yaml(path: Path) -> dict:
    try:
        import yaml
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def _persona_dirs() -> list[Path]:
    """personas/<folder>/ for every real persona (folders starting with _ or . are templates)."""
    root = PULSE_ROOT / "personas"
    if not root.is_dir():
        return []
    return sorted((d for d in root.iterdir() if d.is_dir() and not d.name.startswith(("_", "."))),
                  key=lambda d: d.name.lower())


def user_name() -> str:
    """The person the household belongs to (the one who approves things): config.yaml `user_name`,
    else the main persona's (or any persona's) persona.yaml `user_name`, else "User"."""
    root = _yaml(PULSE_ROOT / "config.yaml")
    if str(root.get("user_name") or "").strip():
        return str(root["user_name"]).strip()
    main = root.get("active_persona") or ""
    for d in sorted(_persona_dirs(), key=lambda d: d.name != main):
        name = str(_yaml(d / "persona.yaml").get("user_name") or "").strip()
        if name:
            return name
    return "User"


def user_aliases() -> set[str]:
    """Lower-cased names that mean the user: their name, their nickname (config.yaml or
    persona.yaml `user_nickname`), and plain "user" / "the user"."""
    out = {user_name().lower(), "user", "the user"}
    root = _yaml(PULSE_ROOT / "config.yaml")
    nicks = [root.get("user_nickname")] + [_yaml(d / "persona.yaml").get("user_nickname") for d in _persona_dirs()]
    out.update(str(n).strip().lower() for n in nicks if n and str(n).strip())
    return out


def companion_names(code_only: bool = False) -> list[str]:
    """Every companion's name (their channels.lor.author_name), from the persona folders. The main
    persona (config.yaml active_persona) may have no LoR name of its own and uses the root config's.
    code_only=True: only companions with the code skill enabled."""
    root = _yaml(PULSE_ROOT / "config.yaml")
    root_name = ((root.get("channels") or {}).get("lor") or {}).get("author_name") or ""
    names = []
    for d in _persona_dirs():
        cfg = _yaml(d / "config.yaml")
        name = ((cfg.get("channels") or {}).get("lor") or {}).get("author_name") or ""
        if not name and d.name == root.get("active_persona"):
            name = root_name
        code = (cfg.get("skills") or {}).get("code") or {}
        if name and (code.get("enabled") or not code_only):
            names.append(name)
    return sorted(set(names), key=str.lower)


def notify_user(text: str, key: str | None = None):
    """A short phone ping to the user (core/notify.py), if that's set up. Never raises."""
    try:
        from core import notify
        notify.ping_user(text, key=key)
    except Exception:
        pass


# ── helpers ─────────────────────────────────────────────────────────────

def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def sha(text: str | None) -> str | None:
    return None if text is None else hashlib.sha256(text.encode("utf-8")).hexdigest()


def _atomic_write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    for attempt in range(10):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 9:
                raise
            time.sleep(0.1)


def _new_id() -> str:
    raw = f"{time.time()}-{os.urandom(4).hex()}"
    return hashlib.sha256(raw.encode()).hexdigest()[:6]


def safe_to_apply(path: str) -> str | None:
    """None if this path may be written by an approved proposal, else why not."""
    p = Path(path).resolve()
    name = p.name.lower()
    if name.startswith(".env"):
        return "secrets files are never changed"
    if any(seg in NEVER_APPLY_SEGMENTS for seg in p.parts):
        return "blocked folder (.git / node_modules / caches)"
    if "lighthouse-memory" in str(p).lower().replace("\\", "/"):
        return "the Lighthouse is never written through the Workbench"
    for root in APPLY_ROOTS:
        try:
            p.relative_to(root.resolve())
            return None
        except ValueError:
            continue
    # Folders the user pointed a House Project at ("works on") count too — they chose them.
    try:
        from core import house_projects
        for root in house_projects.target_roots():
            try:
                p.relative_to(root)
                if any(p == q or q in p.parents for q in house_projects._private_paths()):
                    return "off-limits"
                return None
            except ValueError:
                continue
    except Exception:
        pass
    return "outside the project folders"


# ── load / save ─────────────────────────────────────────────────────────

def load(pid: str) -> dict | None:
    path = PROPOSALS_DIR / f"{pid}.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def save(prop: dict):
    prop["updated_at"] = now_iso()
    _atomic_write(PROPOSALS_DIR / f"{prop['id']}.json",
                  json.dumps(prop, indent=2, ensure_ascii=False))


def all_proposals() -> list[dict]:
    out = []
    if PROPOSALS_DIR.exists():
        for f in PROPOSALS_DIR.glob("*.json"):
            try:
                out.append(json.loads(f.read_text(encoding="utf-8")))
            except Exception:
                continue
    out.sort(key=lambda p: p.get("submitted_at") or p.get("created_at") or "", reverse=True)
    return out


def _event(prop: dict, event: str, by: str):
    prop.setdefault("history", []).append({"at": now_iso(), "event": event, "by": by})


# ── drafts (used by the code skill while a companion works) ─────────────

def get_draft(author: str, create: bool = False, usage_db=None,
              start_snap: dict | None = None, shared: bool = False) -> dict | None:
    for p in all_proposals():
        if p.get("status") == "draft" and p.get("author", "").lower() == author.lower():
            return p
    if not create:
        return None
    prop = {
        "id": _new_id(), "author": author, "title": "", "why": "",
        "status": "draft", "created_at": now_iso(), "submitted_at": None,
        "files": {}, "reviews": [], "review_requests": [], "checks": None,
        "tokens": {}, "history": [],
    }
    if start_snap is not None:   # work began before the first edit (reading etc.)
        prop["tokens"][author] = {"start": start_snap, "used": None, "shared": shared}
    else:
        start_usage(prop, author, usage_db, shared)
    _event(prop, "started", author)
    save(prop)
    return prop


def staged_text(prop: dict | None, path: str) -> str | None:
    """The companion's proposed content for path, if it's staged."""
    if not prop:
        return None
    f = prop.get("files", {}).get(str(Path(path).resolve()))
    return None if f is None else f.get("new_text")


def stage(prop: dict, path: str, new_text: str):
    """Stage new_text for path. Records the file's on-disk version the first
    time, so apply() can tell if someone else changed it meanwhile."""
    key = str(Path(path).resolve())
    files = prop.setdefault("files", {})
    if key not in files:
        p = Path(key)
        base = p.read_text(encoding="utf-8") if p.exists() else None
        files[key] = {"base_sha": sha(base), "existed": p.exists(), "new_text": new_text}
    else:
        files[key]["new_text"] = new_text
    save(prop)


def base_text(entry: dict, path: str) -> str | None:
    """Current on-disk text (what the proposal will change)."""
    p = Path(path)
    if not p.exists():
        return None
    try:
        return p.read_text(encoding="utf-8")
    except Exception:
        return None


def discard_draft(author: str) -> dict | None:
    prop = get_draft(author)
    if not prop:
        return None
    try:
        (PROPOSALS_DIR / f"{prop['id']}.json").unlink()
    except Exception:
        pass
    return prop


# ── diffs & checks ──────────────────────────────────────────────────────

def file_diff(path: str, entry: dict, context: int = 3) -> str:
    import difflib
    old = base_text(entry, path) or ""
    new = entry.get("new_text") or ""
    name = Path(path).name
    diff = "".join(difflib.unified_diff(
        old.splitlines(keepends=True), new.splitlines(keepends=True),
        fromfile=f"a/{name}" if entry.get("existed") else "/dev/null",
        tofile=f"b/{name}", n=context))
    return diff or "(no changes)"


def full_diff(prop: dict, max_bytes: int | None = None) -> str:
    parts = []
    for path, entry in prop.get("files", {}).items():
        label = "NEW FILE" if not entry.get("existed") else "changed"
        parts.append(f"=== {path}  ({label}) ===\n{file_diff(path, entry)}")
    out = "\n".join(parts) or "(no files)"
    if max_bytes and len(out) > max_bytes:
        out = out[:max_bytes] + f"\n\n... [diff truncated at {max_bytes:,} bytes]"
    return out


def run_checks(prop: dict) -> dict:
    """Syntax-check every staged file WITHOUT running anything:
    Python is compiled (not executed), JSON/YAML are parsed."""
    results = {}
    for path, entry in prop.get("files", {}).items():
        text = entry.get("new_text") or ""
        suffix = Path(path).suffix.lower()
        try:
            if suffix in (".py", ".pyw"):
                compile(text, path, "exec")
                results[path] = "ok"
            elif suffix == ".json":
                json.loads(text)
                results[path] = "ok"
            elif suffix in (".yaml", ".yml"):
                try:
                    import yaml
                except ImportError:
                    results[path] = "skipped (PyYAML not installed)"
                    continue
                yaml.safe_load(text)
                results[path] = "ok"
            else:
                results[path] = "not checked (not code/config)"
        except SyntaxError as e:
            results[path] = f"SYNTAX ERROR line {e.lineno}: {e.msg}"
        except Exception as e:
            results[path] = f"ERROR: {e}"
    prop["checks"] = {"at": now_iso(), "results": results}
    return prop["checks"]


def checks_ok(prop: dict) -> bool:
    res = (prop.get("checks") or {}).get("results") or {}
    return all(not (v.startswith("SYNTAX") or v.startswith("ERROR")) for v in res.values())


# ── tests (user-triggered only) ─────────────────────────────────────────
# Running tests RUNS the proposed code, so only the user's window calls this —
# companions never can (that would sidestep approval). It runs in a
# throwaway copy of Pulse's code (no data/, logs/, personas/, .env), with
# secret-looking environment variables removed, and compares against the
# same tests on the unchanged code so pre-existing failures aren't blamed
# on the proposal.

TEST_COPY_DIRS = ["core", "skills", "channels", "scripts", "tests", "tui"]
TEST_TIMEOUT_S = 180


def _copy_code(dest: Path):
    import shutil
    ignore = shutil.ignore_patterns("__pycache__", ".env*", "*.backup_*", "*.bak*",
                                    "session", "*.db", "*.log")
    for d in TEST_COPY_DIRS:
        src = PULSE_ROOT / d
        if src.is_dir():
            shutil.copytree(src, dest / d, ignore=ignore)
    for f in PULSE_ROOT.glob("*.py"):
        shutil.copy2(f, dest / f.name)


def _run_unittest(where: Path) -> dict:
    import re
    import subprocess
    import sys
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe" and exe.with_name("python.exe").exists():
        exe = exe.with_name("python.exe")
    env = {k: v for k, v in os.environ.items()
           if not any(s in k.upper() for s in ("KEY", "TOKEN", "SECRET", "PASSWORD"))}
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        r = subprocess.run([str(exe), "-m", "unittest", "discover", "-s", "tests"],
                           cwd=str(where), capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=TEST_TIMEOUT_S, env=env,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        out = (r.stdout or "") + (r.stderr or "")
    except subprocess.TimeoutExpired:
        return {"ok": False, "summary": f"timed out after {TEST_TIMEOUT_S}s", "ran": 0, "output": ""}
    ran = re.search(r"Ran (\d+) tests?", out)
    tail = re.search(r"\n(OK.*|FAILED \(.*\))\s*$", out.strip() + "\n")
    summary = tail.group(1).strip() if tail else "couldn't read the result"
    return {"ok": summary.startswith("OK"), "summary": summary,
            "ran": int(ran.group(1)) if ran else 0, "output": out[-6000:]}


def run_tests(prop: dict) -> dict:
    import shutil
    import tempfile
    mine = {}
    for path, entry in prop.get("files", {}).items():
        try:
            mine[Path(path).resolve().relative_to(PULSE_ROOT)] = entry["new_text"]
        except ValueError:
            continue
    if not mine:
        result = {"at": now_iso(), "note": "No Pulse files in this change — there are no tests to run for it."}
        prop["tests"] = result
        save(prop)
        return result
    tmp = Path(tempfile.mkdtemp(prefix="workbench_tests_"))
    try:
        before_dir, after_dir = tmp / "before", tmp / "after"
        _copy_code(before_dir)
        _copy_code(after_dir)
        for rel, text in mine.items():
            target = after_dir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        before, after = _run_unittest(before_dir), _run_unittest(after_dir)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    result = {"at": now_iso(), "before": before, "after": after}
    prop["tests"] = result
    _event(prop, f"tests run: {after['summary']}", user_name())
    save(prop)
    return result


# ── token accounting ────────────────────────────────────────────────────
# Pulse records usage as per-day totals in each persona's DB. We snapshot the
# totals when work starts and when it ends; the difference is this job's cost.
# Personas that share a database (data/pulse.db) get an estimate that may
# include a sibling's chatting in the same window — flagged as "shared".

def _usage_totals(usage_db, day: str) -> dict:
    tot = {"prompt": 0, "completion": 0, "cache_read": 0, "cache_write": 0,
           "cache_write_5m": 0, "cache_write_1h": 0, "calls": 0}
    if usage_db is None:
        return tot
    try:
        for r in usage_db.get_usage_today(day):
            tot["prompt"] += r.get("prompt_tokens") or 0
            tot["completion"] += r.get("completion_tokens") or 0
            tot["cache_read"] += r.get("cache_read_input_tokens") or 0
            tot["cache_write"] += r.get("cache_creation_input_tokens") or 0
            tot["cache_write_5m"] += r.get("cache_creation_5m_input_tokens") or 0
            tot["cache_write_1h"] += r.get("cache_creation_1h_input_tokens") or 0
            tot["calls"] += r.get("calls") or 0
    except Exception:
        pass
    return tot


def snapshot(usage_db) -> dict | None:
    return _snapshot(usage_db)


def _snapshot(usage_db) -> dict | None:
    if usage_db is None:
        return None
    today = date.today().isoformat()
    return {"date": today, "totals": _usage_totals(usage_db, today)}


def start_usage(prop: dict, who: str, usage_db, shared: bool = False):
    snap = _snapshot(usage_db)
    if snap is not None:
        prop.setdefault("tokens", {})[who] = {"start": snap, "used": None, "shared": shared}


# A companion can open a relay turn, go off and do other things for hours, then finish — and the
# meter would bill the project for all of it (usage is only stored as daily totals, so there's no
# telling calls apart afterwards). So each project action "touches" the meter; a quiet gap longer
# than this means they were away, and the gap isn't counted.
IDLE_GAP_MIN = 20


def _used_between(usage_db, a: dict, b: dict | None = None) -> dict:
    """Tokens used between snapshot a and snapshot b (None = now)."""
    d0 = date.fromisoformat(a["date"])
    d1 = date.fromisoformat(b["date"]) if b else date.today()
    used = {k: 0 for k in a["totals"]}
    d = d0
    while d <= d1:
        t = b["totals"] if (b and d == d1) else _usage_totals(usage_db, d.isoformat())
        for k in used:
            used[k] += t.get(k, 0) - (a["totals"][k] if d == d0 else 0)
        d += timedelta(days=1)
    return {k: max(0, v) for k, v in used.items()}


def _away(rec: dict) -> bool:
    last = rec.get("last_touch")
    return bool(last) and (datetime.now() - datetime.fromisoformat(last["at"])).total_seconds() > IDLE_GAP_MIN * 60


def touch_usage(prop: dict, who: str, usage_db) -> None:
    """Note that `who` is working on their turn right now. Coming back after a long quiet gap banks
    the earlier stretch and restarts the meter, so the time away isn't billed."""
    rec = (prop.get("tokens") or {}).get(who)
    if not rec or rec.get("used") is not None or usage_db is None:
        return
    snap = _snapshot(usage_db)
    if _away(rec):
        seg = _used_between(usage_db, rec["start"], rec["last_touch"]["snap"])
        bank = rec.setdefault("banked", {k: 0 for k in seg})
        for k, v in seg.items():
            bank[k] = bank.get(k, 0) + v
        rec["start"] = snap
        rec["pauses"] = rec.get("pauses", 0) + 1
    rec["last_touch"] = {"at": datetime.now().isoformat(timespec="seconds"), "snap": snap}


def turn_used(rec: dict, usage_db) -> dict:
    """Tokens a turn has used: the banked stretches plus the current one. If they've been away since
    their last project action, the current stretch stops at that action."""
    used = _used_between(usage_db, rec["start"], rec["last_touch"]["snap"] if _away(rec) else None)
    for k, v in (rec.get("banked") or {}).items():
        used[k] = used.get(k, 0) + v
    return used


def finish_usage(prop: dict, who: str, usage_db):
    rec = (prop.get("tokens") or {}).get(who)
    if not rec or usage_db is None:
        return
    rec["used"] = turn_used(rec, usage_db)
    rec["finished_at"] = now_iso()


def tokens_summary(prop: dict) -> str:
    parts = []
    for who, rec in (prop.get("tokens") or {}).items():
        u = rec.get("used")
        if not u:
            continue
        total = u["prompt"] + u["completion"]
        s = f"{who}: {total:,} tokens ({u['prompt']:,} in / {u['completion']:,} out, {u['calls']} calls)"
        if u.get("cache_read"):
            s += f", {u['cache_read']:,} from cache"
        if rec.get("shared"):
            s += " ~estimate"
        parts.append(s)
    return "\n".join(parts)


# ── submit / review ─────────────────────────────────────────────────────

def submit(prop: dict, title: str, why: str, usage_db=None, reviewer: str = "") -> dict:
    prop["title"] = title.strip() or "(untitled change)"
    prop["why"] = why.strip()
    prop["status"] = "submitted"
    prop["submitted_at"] = now_iso()
    run_checks(prop)
    finish_usage(prop, prop["author"], usage_db)
    _event(prop, "submitted", prop["author"])
    if reviewer.strip():
        request_review(prop, reviewer.strip(), requested_by=prop["author"], save_now=False)
    save(prop)
    notify_user(f"🛠 {prop['author']} sent you a code change to look at: \"{prop['title']}\" "
                f"— it's in the Workbench (Code changes).", key=f"proposal:{prop['id']}")
    return prop


def _persona_config(name: str) -> tuple[str, dict] | None:
    """(folder, config) for the companion whose author name is `name`."""
    try:
        import yaml
    except ImportError:
        return None
    for cfg_path in (PULSE_ROOT / "personas").glob("*/config.yaml"):
        if cfg_path.parent.name.startswith("_"):
            continue
        try:
            cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
        author = ((cfg.get("channels") or {}).get("lor") or {}).get("author_name") or ""
        if author.lower() == name.lower():
            return cfg_path.parent.name, cfg
    # The main persona may have no LoR name of its own — it uses the root config's.
    # Also accept a folder name directly.
    try:
        root = yaml.safe_load((PULSE_ROOT / "config.yaml").read_text(encoding="utf-8")) or {}
    except Exception:
        root = {}
    main = root.get("active_persona") or ""
    root_name = ((root.get("channels") or {}).get("lor") or {}).get("author_name") or ""
    for folder in ([main] if name.lower() in (root_name.lower(), main.lower()) and main else []) + [name]:
        cfg_path = PULSE_ROOT / "personas" / folder / "config.yaml"
        if folder and not folder.startswith("_") and cfg_path.exists():
            try:
                return folder, yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
            except Exception:
                return None
    return None


_running_cache: dict = {"at": 0.0, "folders": None}


def running_personas() -> set[str] | None:
    """Persona folders with a pulse.py running right now (None if we can't tell).
    Cached for 30s; the main persona's plain `pulse.py` counts as its active_persona."""
    if time.time() - _running_cache["at"] < 30 and _running_cache["folders"] is not None:
        return _running_cache["folders"]
    try:
        import subprocess
        import re as _re
        ps = ("Get-CimInstance Win32_Process | Where-Object { $_.Name -like 'python*' -and "
              "$_.CommandLine -match 'pulse\\.py' } | ForEach-Object { $_.CommandLine }")
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True,
                             timeout=20, creationflags=0x08000000).stdout
        folders = set()
        for line in out.splitlines():
            m = _re.search(r"--persona\s+(\S+)", line)
            if m:
                folders.add(m.group(1).strip('"'))
            elif "pulse.py" in line:
                import yaml
                root = yaml.safe_load((PULSE_ROOT / "config.yaml").read_text(encoding="utf-8")) or {}
                folders.add(root.get("active_persona") or "")
        _running_cache.update(at=time.time(), folders=folders)
        return folders
    except Exception:
        return None


def is_awake(name: str) -> bool | None:
    """True / False if we can tell whether this companion's window is running, else None."""
    found = _persona_config(name)
    folders = running_personas()
    if not found or folders is None:
        return None
    return found[0] in folders


def ping(name: str, task_text: str) -> bool:
    """Wake a companion for a dedicated turn (NOT their free-time heartbeat):
    adds a one-time task, due now, to their own schedule. Pulse checks for due
    tasks every 60s.

    Note: pulse.py ALWAYS runs a persona on
    personas/<folder>/data/<folder>.db, whatever paths.database says in its
    config — so every companion has their own schedule. (Before this, the ones
    without paths.database in their config only ever got a letter.)"""
    found = _persona_config(name)
    if not found:
        return False
    folder, cfg = found
    db_path = PULSE_ROOT / "personas" / folder / "data" / f"{folder}.db"
    if not db_path.exists():
        return False
    try:
        from datetime import timezone
        from core.db import PulseDatabase
        from core.scheduler import generate_schedule_id
        now_utc = datetime.now(timezone.utc).isoformat()
        PulseDatabase(db_path).save_schedule({
            "id": generate_schedule_id(), "task": task_text, "created_by": "Workbench",
            "origin": "user", "priority": "routine", "created_at": now_utc,
            "created_at_local": datetime.now().strftime("%A %b %d, %I:%M %p"),
            "enabled": True, "schedule_type": "once", "run_at": now_utc,
            "run_at_local": datetime.now().strftime("%A %b %d, %I:%M %p"), "completed": False,
        })
        return True
    except Exception:
        return False


def request_review(prop: dict, reviewer: str, requested_by: str, save_now: bool = True) -> bool:
    """Letter + (where possible) a ping. Returns True if they were pinged."""
    if reviewer.lower() not in [r.lower() for r in prop.setdefault("review_requests", [])]:
        prop["review_requests"].append(reviewer)
    _event(prop, f"review requested from {reviewer}", requested_by)
    pinged = ping(reviewer, (
        f"Workbench review request (only if you feel up to it — saying no is completely okay). "
        f"{requested_by} asked if you'd look over proposal #{prop['id']}, "
        f"\"{prop.get('title')}\" by {prop['author']}. "
        f"If you'd like to: use the `proposal` tool with action=\"read\", id=\"{prop['id']}\", "
        f"then action=\"review\" with your verdict and notes. If you'd rather not (or not right "
        f"now), that's fine — just let {user_name()} know. Either way, send them a short note."))
    if pinged:
        _event(prop, f"{reviewer} pinged", "Workbench")
    send_letter(reviewer, f"Review request: {prop.get('title') or prop['id']}",
                f"{requested_by} asked you to look over a code change on the Workbench.\n\n"
                f"Proposal #{prop['id']} by {prop['author']}: {prop.get('title')}\n"
                f"Why: {prop.get('why') or '(not given)'}\n\n"
                f"To see it, use the `proposal` tool with action=\"read\" and id=\"{prop['id']}\". "
                f"When you've looked, leave your thoughts with action=\"review\" "
                f"(verdict: looks good / needs changes / has concerns, plus notes). "
                f"Nothing is applied until {user_name()} approves it. Only if you feel up to it — "
                f"it's okay to say no.")
    if save_now:
        save(prop)
    return pinged


def add_review(prop: dict, reviewer: str, verdict: str, notes: str, usage_db=None):
    finish_usage(prop, reviewer, usage_db)
    prop.setdefault("reviews", []).append(
        {"reviewer": reviewer, "verdict": verdict, "notes": notes.strip(), "at": now_iso()})
    _event(prop, f"reviewed: {verdict}", reviewer)
    save(prop)


def send_letter(to: str, subject: str, content: str):
    """Drop a letter in the household letters folder (same format as the
    letters skill), so companions hear about Workbench events."""
    letter_id = hashlib.sha256(f"{time.time()}-{os.urandom(3).hex()}".encode()).hexdigest()[:8]
    letter = {"id": letter_id, "from": WORKBENCH_SENDER, "to": to, "subject": subject,
              "content": content, "sent_at": datetime.now().isoformat(), "read_by": []}
    try:
        _atomic_write(LETTERS_DIR / f"letter_{letter_id}.json", json.dumps(letter, indent=2))
    except Exception:
        pass


# ── apply / reject / undo (the user's window) ───────────────────────────

def conflicts(prop: dict) -> dict:
    """path -> problem, for files that can't be applied cleanly."""
    probs = {}
    for path, entry in prop.get("files", {}).items():
        why = safe_to_apply(path)
        if why:
            probs[path] = why
            continue
        p = Path(path)
        current = p.read_text(encoding="utf-8") if p.exists() else None
        if sha(current) != entry.get("base_sha"):
            probs[path] = "changed on disk since the proposal was made"
    return probs


def apply(prop: dict, by: str = "") -> tuple[bool, str]:
    by = by or user_name()
    if prop.get("status") != "submitted":
        return False, f"Only submitted proposals can be applied (this one is {prop.get('status')})."
    probs = conflicts(prop)
    if probs:
        return False, "Not applied — " + "; ".join(f"{Path(k).name}: {v}" for k, v in probs.items())
    bdir = BACKUPS_DIR / prop["id"]
    bdir.mkdir(parents=True, exist_ok=True)
    manifest = []
    for i, (path, entry) in enumerate(prop["files"].items()):
        p = Path(path)
        backup = None
        if p.exists():
            backup = bdir / f"{i}_{p.name}"
            backup.write_bytes(p.read_bytes())
        manifest.append({"path": path, "backup": str(backup) if backup else None,
                         "applied_sha": sha(entry["new_text"])})
    written = []
    try:
        for path, entry in prop["files"].items():
            p = Path(path)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(entry["new_text"], encoding="utf-8")
            written.append(path)
    except Exception as e:
        _restore(manifest, only=written)
        return False, f"Failed while writing ({e}); anything already written was put back."
    prop["status"] = "applied"
    prop["applied_at"] = now_iso()
    prop["undo"] = manifest
    _event(prop, "approved & applied", by)
    save(prop)
    send_letter(prop["author"], f"Approved: {prop.get('title')}",
                f"{by} approved your Workbench proposal #{prop['id']} (\"{prop.get('title')}\"). "
                f"It's applied now. Thank you! 🛠️")
    return True, f"Applied {len(written)} file(s). Backups are saved; you can undo."


def _restore(manifest: list, only: list | None = None):
    for m in manifest:
        if only is not None and m["path"] not in only:
            continue
        p = Path(m["path"])
        if m["backup"]:
            p.write_bytes(Path(m["backup"]).read_bytes())
        elif p.exists():
            p.unlink()


def undo(prop: dict, by: str = "") -> tuple[bool, str]:
    by = by or user_name()
    if prop.get("status") != "applied":
        return False, "Only applied proposals can be undone."
    changed = []
    for m in prop.get("undo", []):
        p = Path(m["path"])
        current = p.read_text(encoding="utf-8") if p.exists() else None
        if sha(current) != m["applied_sha"]:
            changed.append(p.name)
    if changed:
        return False, ("Not undone — these files were changed again after applying: "
                       + ", ".join(changed) + ". Undoing would erase that later work.")
    _restore(prop["undo"])
    prop["status"] = "undone"
    _event(prop, "undone", by)
    save(prop)
    return True, "Undone — the files are back to how they were before."


def reject(prop: dict, note: str = "", by: str = "") -> tuple[bool, str]:
    by = by or user_name()
    if prop.get("status") != "submitted":
        return False, "Only submitted proposals can be rejected."
    prop["status"] = "rejected"
    prop["reject_note"] = note.strip()
    _event(prop, "rejected", by)
    save(prop)
    send_letter(prop["author"], f"Not this time: {prop.get('title')}",
                f"{by} decided not to apply your Workbench proposal #{prop['id']} "
                f"(\"{prop.get('title')}\")."
                + (f"\n\nTheir note: {note.strip()}" if note.strip() else "")
                + "\n\nNothing was changed. You're welcome to try again with a new proposal.")
    return True, "Rejected. Nothing was changed, and the author got a letter."
