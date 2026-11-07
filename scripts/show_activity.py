"""
show_activity — peek at what the companions have been doing with their
dev tools (read_code_file, search, git, etc.).

Pulls from logs/<persona folder>.log, filters to the code-touching
toolset by default, and pretty-prints in a colorized feed format. The
companions are the folders in personas/ (named by their channels.lor.author_name
when set); each gets its own accent color.

Usage:
    py scripts/show_activity.py              # everyone, dev tools only, last 15 each
    py scripts/show_activity.py ada          # just Ada (folder or display name)
    py scripts/show_activity.py ada 30       # just Ada, last 30
    py show_activity.py --all        # don't filter — show every tool
    py show_activity.py --no-color   # plain text, no ANSI
"""

import ast
import os
import re
import sys
from pathlib import Path

# Reconfigure stdout to UTF-8 (Windows console default is cp1252).
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# Enable ANSI escape sequence processing on Windows 10+.
# os.system('') is the cheapest way to flip VT_PROCESSING on for cmd.exe.
if os.name == "nt":
    os.system("")

PULSE_ROOT = Path(__file__).resolve().parent.parent
LOGS_DIR = PULSE_ROOT / "logs"


def _personas() -> dict:
    """{log stem (persona folder): display name} for every persona folder."""
    try:
        import yaml
    except ImportError:
        yaml = None
    out = {}
    pdir = PULSE_ROOT / "personas"
    for d in sorted(pdir.iterdir(), key=lambda d: d.name.lower()) if pdir.is_dir() else []:
        if not d.is_dir() or d.name.startswith(("_", ".")):
            continue
        name = ""
        if yaml is not None:
            try:
                cfg = yaml.safe_load((d / "config.yaml").read_text(encoding="utf-8")) or {}
                name = ((cfg.get("channels") or {}).get("lor") or {}).get("author_name") or ""
            except Exception:
                pass
        out[d.name] = name or d.name.title()
    return out


PERSONAS = _personas()

# Tools that touch code / files / git. Default filter set.
DEV_TOOLS = {
    # Code skill (Phase 1)
    "read_code_file",
    "list_code_directory",
    "search_code_repo",
    "git_status",
    "git_diff",
    # Existing dev skill
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
    "write_skill": "✏️ ",
    "list_skills": "📋",
    "dev_journal": "📓",
    "preview_edit": "👀",
    "edit_file": "✏️",
    "write_file": "📝",
    "proposal": "🛠️",
    "dev_journal_read": "📓",
    "dev_journal_write": "📓",
}

# Persona accent colors — give each companion their own (assigned in order from this palette).
ACCENTS = ["\033[38;5;177m", "\033[38;5;208m", "\033[38;5;110m", "\033[38;5;153m", "\033[38;5;215m",
           "\033[38;5;156m", "\033[38;5;180m", "\033[38;5;229m", "\033[38;5;152m", "\033[38;5;211m"]
PERSONA_COLORS = {name: ACCENTS[i % len(ACCENTS)] for i, name in enumerate(PERSONAS.values())}

# Tool category colors
CAT_READ = "\033[38;5;75m"       # blue
CAT_SEARCH = "\033[38;5;221m"    # yellow
CAT_GIT = "\033[38;5;114m"       # green
CAT_WRITE = "\033[38;5;176m"     # magenta

TOOL_COLORS = {
    "read_code_file": CAT_READ,
    "list_code_directory": CAT_READ,
    "read_source": CAT_READ,
    "list_skills": CAT_READ,
    "search_code_repo": CAT_SEARCH,
    "search_code": CAT_SEARCH,
    "git_status": CAT_GIT,
    "git_diff": CAT_GIT,
    "write_skill": CAT_WRITE,
    "dev_journal": CAT_WRITE,
}

DIM = "\033[2m"
RESET = "\033[0m"
BOLD = "\033[1m"
GRAY = "\033[38;5;245m"

# Regex
EXEC_RE = re.compile(r"(\d\d:\d\d:\d\d).*Executing tool:\s*(\w+)\((.*)\)\s*$")
RESULT_RE = re.compile(r"(\d\d:\d\d:\d\d).*Tool executed:\s*(\w+)\s*->\s*(.*)$")
HEARTBEAT_RE = re.compile(r"(\d\d:\d\d:\d\d).*Heartbeat tools used:\s*(.+)$")


def parse_cli(argv):
    persona = None
    limit = 15
    show_all = False
    use_color = True
    for arg in argv:
        if arg == "--all":
            show_all = True
        elif arg == "--no-color":
            use_color = False
        elif arg.startswith("--limit="):
            try:
                limit = int(arg.split("=", 1)[1])
            except ValueError:
                pass
        elif arg.isdigit():
            limit = int(arg)
        elif not arg.startswith("--"):
            persona = arg.lower()
    return persona, limit, show_all, use_color


def short_path(p):
    """Drop the home-folder prefix so paths read at a glance."""
    if not p:
        return ""
    p = str(p).replace("\\", "/")
    lower = p.lower()
    home = str(Path.home()).replace("\\", "/").rstrip("/").lower() + "/"
    for prefix in (home, "/" + home.replace(":", "", 1) if ":" in home else home):
        if lower.startswith(prefix):
            return p[len(prefix):]
    return p


def parse_args_dict(args_str):
    """Try to safely parse the args portion of a log line as a Python dict."""
    s = args_str.strip()
    if not s:
        return {}
    try:
        # Wrap as a single dict argument if it looks like kwargs
        parsed = ast.literal_eval(s if s.startswith("{") else "{" + s + "}")
        if isinstance(parsed, dict):
            return parsed
    except (ValueError, SyntaxError):
        pass
    return {"_raw": s}


def format_args(tool, args):
    """Render a one-line, tool-specific summary of the call's arguments."""
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
        repo = args.get("repo", "")
        return short_path(repo) or "(pulse)"
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
        return args.get("entry", args.get("note", ""))[:60]
    # Fallback
    parts = []
    for k, v in args.items():
        v_str = repr(v) if not isinstance(v, str) else v
        parts.append(f"{k}={v_str}")
    return ", ".join(parts)[:80]


def format_result(tool, result):
    """Trim a tool result to one-line scannable form.

    Returns None to signal "don't print a result line" — used for tools
    whose result line in the log is just a path header that the args
    already conveyed (read_*, list_*).
    """
    if not result:
        return None
    r = result.replace("\n", " ").replace("\r", " ").strip()

    # For these, args already say what was touched; the result preview is
    # just a header before the truncation cap, so adds nothing.
    if tool in ("read_code_file", "list_code_directory", "read_source", "list_skills"):
        return None

    if tool in ("search_code_repo", "search_code"):
        m = re.search(r"(\d+)\s+match", r)
        if m:
            return f"{m.group(1)} match(es)"
        if "No matches" in r or r.startswith("no matches"):
            return "no matches"

    if tool == "git_status":
        if "nothing to commit" in r:
            return "clean"
        # First clause is usually the meaningful line ("On branch main", etc.)
        first = r.split(".", 1)[0]
        return first[:90] if first else None

    if tool == "git_diff":
        if not r or r == "(no output)" or "(no output)" in r:
            return "no diff"
        return "diff present"

    if len(r) > 90:
        return r[:89] + "…"
    return r or None


def extract_tool_calls(log_file: Path, tools_filter: set | None, limit: int):
    """Parse the log, pair exec→result, optionally filter by tool name."""
    if not log_file.exists():
        return []
    try:
        with log_file.open("r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except Exception:
        return []

    events = []
    for line in lines:
        em = EXEC_RE.search(line)
        if em:
            time, tool, args = em.groups()
            if tools_filter and tool not in tools_filter:
                continue
            events.append({
                "time": time,
                "tool": tool,
                "args_raw": args.strip(),
                "result": None,
            })
            continue
        rm = RESULT_RE.search(line)
        if rm:
            time, tool, result = rm.groups()
            if tools_filter and tool not in tools_filter:
                continue
            for ev in reversed(events):
                if ev["tool"] == tool and ev["result"] is None:
                    ev["result"] = result.strip()
                    break
    return events[-limit:]


def extract_heartbeats(log_file: Path, tools_filter: set | None, limit: int = 3):
    """Recent heartbeat summaries, filtered to tools-of-interest."""
    if not log_file.exists():
        return []
    try:
        with log_file.open("r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except Exception:
        return []
    out = []
    for line in lines:
        m = HEARTBEAT_RE.search(line)
        if m:
            time, tools_str = m.group(1), m.group(2).strip()
            tools = [t.strip() for t in tools_str.split(",")]
            if tools_filter:
                tools = [t for t in tools if t in tools_filter]
            if tools:
                out.append((time, tools))
    return out[-limit:]


def render_persona(stem, display, limit, tools_filter, use_color):
    log_file = LOGS_DIR / f"{stem}.log"
    events = extract_tool_calls(log_file, tools_filter, limit)
    heartbeats = extract_heartbeats(log_file, tools_filter, limit=3)

    if not events and not heartbeats:
        return False

    persona_color = PERSONA_COLORS.get(display, "") if use_color else ""
    reset = RESET if use_color else ""
    dim = DIM if use_color else ""
    gray = GRAY if use_color else ""
    bold = BOLD if use_color else ""

    bar = "━" * 56
    print(f"{persona_color}{bar}{reset}")
    label = "dev tool" if tools_filter else "tool"
    n = len(events)
    print(f"  {persona_color}{bold}{display}{reset}  {gray}— last {n} {label} call(s){reset}")
    print(f"{persona_color}{bar}{reset}")

    if not events:
        print(f"  {gray}(no individual tool-call lines; see heartbeat summaries below){reset}")

    for ev in events:
        tool = ev["tool"]
        icon = TOOL_ICONS.get(tool, "🔧")
        tool_color = TOOL_COLORS.get(tool, "") if use_color else ""
        args = parse_args_dict(ev["args_raw"])
        args_str = format_args(tool, args)
        result_str = format_result(tool, ev["result"] or "")

        line = (
            f"  {gray}{ev['time']}{reset}  "
            f"{icon} {tool_color}{tool:<22}{reset}  "
            f"{args_str}"
        )
        print(line)
        if result_str is not None:
            print(f"             {gray}→ {result_str}{reset}")

    if heartbeats:
        print()
        print(f"  {gray}recent heartbeat summaries (filtered):{reset}")
        for time, tools in heartbeats:
            tools_str = ", ".join(tools)
            if len(tools_str) > 100:
                tools_str = tools_str[:99] + "…"
            print(f"    {gray}{time}{reset}  → {tools_str}")

    print()
    return True


def main():
    persona_filter, limit, show_all, use_color = parse_cli(sys.argv[1:])

    if not LOGS_DIR.exists():
        print(f"No logs directory at {LOGS_DIR}")
        return 1

    tools_filter = None if show_all else DEV_TOOLS

    if use_color:
        title_color = "\033[38;5;215m"  # soft orange
    else:
        title_color = ""
    reset = RESET if use_color else ""
    gray = GRAY if use_color else ""

    print()
    if tools_filter:
        print(f"  {title_color}Pulse — dev tool activity{reset}")
        print(f"  {gray}(read_code_file · search_code_repo · git_status · git_diff · etc.){reset}")
        print(f"  {gray}Pass --all to see every tool.{reset}")
    else:
        print(f"  {title_color}Pulse — all tool activity{reset}")
    print()

    shown_any = False
    for stem, display in PERSONAS.items():
        if persona_filter and persona_filter not in (
            stem.lower(),
            display.lower(),
        ):
            continue
        if render_persona(stem, display, limit, tools_filter, use_color):
            shown_any = True

    if not shown_any:
        if persona_filter:
            print(f"  {gray}No matching activity for '{persona_filter}'.{reset}")
            print(f"  {gray}Known personas: {', '.join(PERSONAS.values())}{reset}")
        else:
            scope = "dev tool" if tools_filter else "tool"
            print(f"  {gray}No {scope} activity in any persona log yet.{reset}")
            if tools_filter:
                print(f"  {gray}(Try --all to see non-dev tool activity.){reset}")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
