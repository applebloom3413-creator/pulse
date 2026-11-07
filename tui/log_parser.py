"""
Log parser — pulls tool-call events out of a Pulse persona log.

Shared by:
  - scripts/show_activity.py (one-shot CLI viewer)
  - tui/app.py (live activity pane)
"""

import ast
import re
from pathlib import Path


EXEC_RE = re.compile(r"(\d\d:\d\d:\d\d).*Executing tool:\s*(\w+)\((.*)\)\s*$")
RESULT_RE = re.compile(r"(\d\d:\d\d:\d\d).*Tool executed:\s*(\w+)\s*->\s*(.*)$")

# Code/dev tools — same set as scripts/show_activity.py
DEV_TOOLS = {
    "read_code_file",
    "list_code_directory",
    "search_code_repo",
    "git_status",
    "git_diff",
    "read_source",
    "search_code",
    "write_skill",
    "list_skills",
    "dev_journal",
    # Writes + Workbench
    "preview_edit",
    "edit_file",
    "write_file",
    "proposal",
    "dev_journal_read",
    "dev_journal_write",
}

TOOL_ICONS = {
    "read_code_file": "📖",
    "list_code_directory": "📁",
    "search_code_repo": "🔍",
    "git_status": "🌿",
    "git_diff": "📜",
    "read_source": "📖",
    "search_code": "🔍",
    "write_skill": "✏️",
    "list_skills": "📋",
    "dev_journal": "📓",
    "preview_edit": "👀",
    "edit_file": "✏️",
    "write_file": "📝",
    "proposal": "🛠️",
    "dev_journal_read": "📓",
    "dev_journal_write": "📓",
}


_HOME = Path.home().as_posix().rstrip("/") + "/"
# e.g. "c:/users/name/" and its git-bash form "/c/users/name/"
_HOME_PREFIXES = tuple({_HOME.lower(), ("/" + _HOME.replace(":", "", 1)).lower() if ":" in _HOME else _HOME.lower()})


def short_path(p: str) -> str:
    """Show paths under the home folder relative to it."""
    if not p:
        return ""
    p = str(p).replace("\\", "/")
    lower = p.lower()
    for prefix in _HOME_PREFIXES:
        if lower.startswith(prefix):
            return p[len(prefix):]
    return p


def parse_args_dict(args_str: str) -> dict:
    s = args_str.strip()
    if not s:
        return {}
    try:
        parsed = ast.literal_eval(s if s.startswith("{") else "{" + s + "}")
        if isinstance(parsed, dict):
            return parsed
    except (ValueError, SyntaxError):
        pass
    return {"_raw": s}


def format_args(tool: str, args: dict) -> str:
    if "_raw" in args:
        return args["_raw"][:80]
    if tool == "read_code_file":
        return short_path(args.get("path", ""))
    if tool == "list_code_directory":
        p = args.get("path", "")
        return (short_path(p) + "/") if p else "(allowed roots)"
    if tool == "search_code_repo":
        pattern = args.get("pattern", "?")
        path = args.get("path") or ""
        glob = args.get("file_glob") or ""
        loc = f" in {short_path(path)}" if path else ""
        glob_str = f" ({glob})" if glob else ""
        return f"/{pattern}/{loc}{glob_str}"
    if tool == "git_status":
        return short_path(args.get("repo", "")) or "(pulse)"
    if tool == "git_diff":
        repo = args.get("repo", "")
        path = args.get("path", "")
        if path:
            return f"{short_path(path)} (in {short_path(repo) or 'pulse'})"
        return short_path(repo) or "(pulse)"
    if tool == "read_source":
        return short_path(args.get("path", args.get("file", "")))
    if tool == "search_code":
        return f"/{args.get('query', args.get('pattern', '?'))}/"
    if tool == "write_skill":
        return args.get("name", args.get("skill_name", "?"))
    if tool == "dev_journal":
        return (args.get("entry") or args.get("note") or "")[:60]
    parts = []
    for k, v in args.items():
        v_str = repr(v) if not isinstance(v, str) else v
        parts.append(f"{k}={v_str}")
    return ", ".join(parts)[:80]


def extract_tool_calls(log_file: Path, tools_filter: set | None, limit: int) -> list[dict]:
    """Parse the log file and return up to `limit` recent tool-call dicts.

    Each dict has: {time, tool, args (dict), args_str (formatted), icon}.
    """
    if not log_file.exists():
        return []
    try:
        with log_file.open("r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except Exception:
        return []

    events: list[dict] = []
    for line in lines:
        em = EXEC_RE.search(line)
        if em:
            time, tool, args_raw = em.groups()
            if tools_filter and tool not in tools_filter:
                continue
            args = parse_args_dict(args_raw.strip())
            events.append({
                "time": time,
                "tool": tool,
                "args": args,
                "args_str": format_args(tool, args),
                "icon": TOOL_ICONS.get(tool, "🔧"),
            })
    return events[-limit:]
