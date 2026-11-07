"""
House Projects — live view of what someone is doing on their turn.

Reads the tail of the companion's own log (logs/<persona folder>.log) and
turns tool calls into plain words: reading, writing, looking something up,
thinking... Only tool names and short details (file names, search terms) —
never message contents.

Used by scripts/workbench_gui.pyw (the LIVE strip on the House Projects tab).
"""

import re
from datetime import datetime, timedelta
from pathlib import Path

from core import workbench

LOGS_DIR = workbench.PULSE_ROOT / "logs"
TAIL_BYTES = 160_000
IDLE_AFTER = timedelta(minutes=8)   # no sign of life for this long = resting

_TIME = r"^(\d\d:\d\d:\d\d) \[\w+\] [\w.]+: "
EXEC_RE = re.compile(_TIME + r"Executing tool: (\w+)\((.*)$")
TASK_RE = re.compile(_TIME + r"Executing due task: (.*)$")
DONE_RE = re.compile(_TIME + r"(?:Task tools used:|LLM telegram reply took|Heartbeat tick completed|"
                             r"Companion chose to stay silent)")
THINK_RE = re.compile(_TIME + r"(?:Prompt cache:|LLM requested|Anthropic .*response|LLM .*response)")


def log_path(name: str) -> Path | None:
    found = workbench._persona_config(name)
    if not found:
        return None
    p = LOGS_DIR / f"{found[0]}.log"
    return p if p.exists() else None


def _arg(raw: str, key: str) -> str:
    m = re.search(r"['\"]" + key + r"['\"]:\s*(['\"])((?:\\.|(?!\1).)*)\1", raw)
    return m.group(2) if m else ""


def _short(s: str, n: int = 60) -> str:
    s = " ".join(s.replace("\\n", " ").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def describe(tool: str, raw: str) -> tuple[str, str, str]:
    """(icon, what they're doing, detail) for one tool call."""
    if tool == "house_project":
        action, path = _arg(raw, "action"), _arg(raw, "path")
        return {
            "open": ("🗂️", "looking over the project", ""),
            "read": ("📖", "reading", path),
            "write": ("✍️", "writing", path),
            "history": ("🕰️", "checking earlier versions", path),
            "diff": ("🔍", "comparing versions", path),
            "stats": ("🔢", "counting words", path),
            "comment": ("💬", "leaving a comment", ""),
            "status": ("📝", "updated their status", _short(_arg(raw, "text"))),
            "done": ("🏁", "handing off", ""),
            "decline": ("🙅", "passing on this one", ""),
            "list": ("🗂️", "browsing projects", ""),
        }.get(action, ("🗂️", f"house project: {action}", path))
    if tool == "web_search":
        return "🔎", "looking up", _short(_arg(raw, "query"))
    if tool == "image_search":
        return "🖼️", "looking for pictures of", _short(_arg(raw, "query"))
    if tool in ("fetch_url", "read_web_page"):
        url = _arg(raw, "url")
        host = re.sub(r"^https?://(www\.)?", "", url).split("/")[0]
        return "🌐", "reading a web page", host
    if tool in ("read_code_file", "read_source", "search_code_repo", "search_code", "list_code_directory"):
        return "📖", "reading code", _short(_arg(raw, "path") or _arg(raw, "query"), 50)
    if tool in ("edit_file", "write_file", "preview_edit", "proposal"):
        return "🛠️", "working on code", _short(_arg(raw, "path"), 50)
    if tool in ("search_memory", "save_memory"):
        return "🧠", "checking their memory" if tool == "search_memory" else "saving a memory", ""
    if tool in ("read_my_letters", "read_letter", "send_letter"):
        return "💌", "with their letters", ""
    if tool.startswith("library") or tool.startswith("lighthouse"):
        return "📚", "in the library" if tool.startswith("library") else "at the Lighthouse", ""
    for key, (icon, verb) in {"lor": ("🗨️", "on Local Reddit"), "scrapbook": ("📌", "in the scrapbook"),
                              "obsidian": ("📎", "reading your clips"), "lantern": ("🏮", "setting their lantern"),
                              "note": ("🗒️", "taking notes"), "journal": ("📓", "writing in their journal"),
                              "chronicle": ("📜", "reading the chronicle"), "garden": ("🌱", "in the garden"),
                              "paint": ("🎨", "painting"), "draw": ("🎨", "drawing")}.items():
        if key in tool:
            return icon, verb, ""
    return "🔧", f"using {tool.replace('_', ' ')}", ""


def _tail_lines(path: Path) -> list[str]:
    with path.open("rb") as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - TAIL_BYTES))
        data = f.read()
    lines = data.decode("utf-8", errors="replace").splitlines()
    return lines[1:] if size > TAIL_BYTES else lines   # first line may be cut


def _stamp(hms: str, now: datetime) -> datetime:
    t = datetime.strptime(hms, "%H:%M:%S").time()
    when = datetime.combine(now.date(), t)
    return when - timedelta(days=1) if when > now + timedelta(minutes=1) else when


def activity(name: str, since_iso: str | None = None, now: datetime | None = None) -> dict:
    """What `name` is up to. {"state": working|thinking|resting|unknown,
    "now": {icon, verb, detail, at}, "feed": [ {icon, verb, detail, at}, ... ]}"""
    now = now or datetime.now()
    path = log_path(name)
    if not path:
        return {"state": "unknown", "now": None, "feed": []}
    try:
        lines = _tail_lines(path)
        mtime = datetime.fromtimestamp(path.stat().st_mtime)
    except OSError:
        return {"state": "unknown", "now": None, "feed": []}
    since = datetime.fromisoformat(since_iso) if since_iso else now - timedelta(hours=6)
    if mtime < since:
        return {"state": "resting", "now": None, "feed": []}

    feed, last_kind, last_at = [], None, None
    for line in lines:
        m = EXEC_RE.match(line)
        if m:
            at = _stamp(m.group(1), now)
            if at >= since:
                icon, verb, detail = describe(m.group(2), m.group(3))
                feed.append({"icon": icon, "verb": verb, "detail": detail, "at": at})
                last_kind, last_at = "tool", at
            continue
        m = TASK_RE.match(line)
        if m:
            at = _stamp(m.group(1), now)
            if at >= since:
                feed.append({"icon": "🧭", "verb": "started a turn", "detail": "", "at": at})
                last_kind, last_at = "task", at
            continue
        m = THINK_RE.match(line)
        if m:
            at = _stamp(m.group(1), now)
            if at >= since:
                last_kind, last_at = "think", at
            continue
        m = DONE_RE.match(line)
        if m:
            at = _stamp(m.group(1), now)
            if at >= since:
                last_kind, last_at = "done", at

    if last_at is None:
        return {"state": "resting", "now": None, "feed": []}
    if last_kind == "done" or now - last_at > IDLE_AFTER:
        state, cur = "resting", {"icon": "💤", "verb": "resting", "detail": "", "at": last_at}
    elif last_kind == "think":
        state, cur = "thinking", {"icon": "💭", "verb": "thinking", "detail": "", "at": last_at}
    else:
        state, cur = "working", dict(feed[-1])
    return {"state": state, "now": cur, "feed": feed[-10:]}


def ago(when: datetime, now: datetime | None = None) -> str:
    secs = int(((now or datetime.now()) - when).total_seconds())
    if secs < 20:
        return "just now"
    if secs < 60:
        return f"{secs}s ago"
    if secs < 3600:
        return f"{secs // 60}m ago"
    return f"{secs // 3600}h ago"
