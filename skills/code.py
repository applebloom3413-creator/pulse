"""
Code skill — read + write access to the user's project files.

  Phase 1 (read-only):    read_code_file, list_code_directory,
                          search_code_repo, git_status, git_diff
  Phase 2 (write): edit_file, write_file
                          — with auto-backup before every change
                          — with unified diff returned for every edit
                          — with exact-string matching (refuses ambiguous
                            or absent matches) so changes are deliberate

Config (per persona):

    skills:
      code:
        enabled: true
        allowed_roots:           # what can be READ
          - "/path/to/pulse"
          - "/path/to/another-project"
        writable_roots:          # subset that can be WRITTEN (optional)
          - "/path/to/pulse"
        approval_exempt:         # optional play-space folders: edits there skip approval
          - "/path/to/pulse/playground"
        # If allowed_roots is omitted, the default is PULSE_CODE_ROOTS (paths
        # joined with os.pathsep), or just this Pulse folder.
        # If writable_roots is omitted, the default is allowed_roots EXCEPT
        # anything under lighthouse-memory (raw writes to the diary DB are
        # unsafe — use lighthouse_write instead).

Safety rules (hard-coded, not config):
  - .env files are never readable OR writable (secrets stay secret).
  - .git/ internals are never readable OR writable.
  - All paths are scoped to allowed_roots; any path outside is refused.
  - Writes are further scoped to writable_roots (lighthouse-memory excluded
    by default — diary entries go through lighthouse_write, not raw files).
  - Every write creates a timestamped backup BEFORE touching the original.
  - File reads truncate at MAX_READ_BYTES to protect context windows.
  - Search results cap at MAX_SEARCH_RESULTS.
  - All git commands run with --no-pager and are inspection-only
    (status, diff) — no commit, push, reset, or anything that mutates
    repo state.

Phase 3 (Workbench): APPROVAL MODE, on by default.
  edit_file / write_file stage changes into the companion's draft proposal
  (core/workbench.py) instead of touching the real files. The companion
  then uses the `proposal` tool to submit it (optionally naming a sibling to
  review), and the user approves/rejects it in scripts/workbench_gui.pyw.
  Reads and previews show the companion's staged version of a file.
  Token use for the job is measured from the persona's usage counter.
  Opt out per persona (old behaviour, writes go straight to disk):

    skills:
      code:
        approval: false
"""

import difflib
import hashlib
import logging
import os
import re
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path

from skills.base import BaseSkill

logger = logging.getLogger(__name__)

# Safety caps
MAX_READ_BYTES = 100_000          # ~100KB per file read
MAX_SEARCH_RESULTS = 100          # cap grep output
MAX_LIST_ENTRIES = 500            # cap directory listing
MAX_GIT_OUTPUT_BYTES = 50_000     # truncate huge diffs
MAX_DIFF_OUTPUT_BYTES = 8_000     # cap diffs returned after edits
READ_CACHE_TTL_S = 30 * 60        # unchanged re-reads stub out for 30 min
MAX_WRITE_BYTES = 200_000         # cap individual file writes at ~200KB

# Filenames that are never readable, no matter where they live
BLOCKED_FILENAMES = {
    ".env",
    ".env.local",
    ".env.production",
    ".env.development",
}

# Path *segments* that are off-limits even inside allowed roots
BLOCKED_PATH_SEGMENTS = {
    ".git",          # git internals — never readable directly
    "node_modules",  # huge, useless
    "__pycache__",   # build artifacts
    ".venv",         # virtualenv
    "venv",
}


def _is_env_file(path: Path) -> bool:
    """Block any file whose name looks like a dotenv variant."""
    name = path.name.lower()
    if name in BLOCKED_FILENAMES:
        return True
    if name.startswith(".env"):
        return True
    return False


class CodeSkill(BaseSkill):
    name = "code"

    def __init__(self, config: dict):
        super().__init__(config)
        skill_cfg = (config.get("skills", {}) or {}).get("code", {}) or {}

        from core import workbench
        self.user = workbench.user_name()      # who approves changes (from config)
        raw_roots = skill_cfg.get("allowed_roots") or []
        if not raw_roots:
            # Default: PULSE_CODE_ROOTS, or just this Pulse folder.
            raw_roots = [str(r) for r in workbench.code_roots()]

        self.allowed_roots: list[Path] = []
        for r in raw_roots:
            try:
                p = Path(r).resolve()
                if p.exists():
                    self.allowed_roots.append(p)
                else:
                    logger.warning(f"code skill: allowed_root does not exist: {r}")
            except Exception as e:
                logger.warning(f"code skill: bad allowed_root {r!r}: {e}")

        if not self.allowed_roots:
            logger.warning("code skill: no allowed_roots resolved — all reads will be refused")

        # Writable roots: subset of allowed_roots that can be modified.
        # If config specifies writable_roots explicitly, use those.
        # Otherwise default to allowed_roots EXCEPT anything matching
        # "lighthouse-memory" (raw diary writes are unsafe).
        raw_writable = skill_cfg.get("writable_roots")
        if raw_writable is not None:
            self.writable_roots: list[Path] = []
            for r in raw_writable:
                try:
                    p = Path(r).resolve()
                    if p.exists():
                        self.writable_roots.append(p)
                except Exception as e:
                    logger.warning(f"code skill: bad writable_root {r!r}: {e}")
        else:
            self.writable_roots = [
                r for r in self.allowed_roots
                if "lighthouse-memory" not in str(r).lower().replace("\\", "/")
            ]

        # Read memoization — stops costly re-read loops at the tool
        # layer. Re-reading an UNCHANGED file within the
        # TTL returns a short stub instead of the full content; the hash
        # check means a changed file always comes back in full. TTL is well
        # under every heartbeat interval (4-6h), so in practice the cache is
        # tick-scoped; it also spans a conversation burst, which is where
        # habit re-reads actually happen. path-str → (sha256, monotonic ts).
        self._read_cache: dict[str, tuple[str, float]] = {}

        # Workbench / approval mode (Phase 3).
        self.approval = bool(skill_cfg.get("approval", True))
        # Play-space folders (config: approval_exempt) — no approval needed there.
        self.approval_exempt = [Path(r).resolve() for r in
                                skill_cfg.get("approval_exempt") or []]
        lor_cfg = (config.get("channels", {}) or {}).get("lor", {}) or {}
        self.author = lor_cfg.get("author_name") or "Companion"
        # Same DB the UsageTracker writes to. Personas on the household
        # data/pulse.db share one counter, so their numbers are estimates.
        self.usage_db = config.get("_shared_db") or config.get("_db")
        paths = config.get("paths", {}) or {}
        db_path = str(paths.get("database", "")).replace("\\", "/")
        self.usage_shared = bool(paths.get("shared_database")) or db_path.endswith("data/pulse.db")
        self._work_start: tuple[dict, float] | None = None   # (usage snapshot, monotonic)

    # ── Workbench helpers ───────────────────────────────────────

    def _draft(self, create: bool = False) -> dict | None:
        from core import workbench
        start = self._work_start[0] if self._work_start else None
        return workbench.get_draft(self.author, create=create, usage_db=self.usage_db,
                                   start_snap=start, shared=self.usage_shared)

    def _note_work(self):
        """Remember when a stretch of code work began, so a proposal's token
        count includes the reading/exploring before the first edit."""
        if not self.approval:
            return
        now = time.monotonic()
        if self._work_start and now - self._work_start[1] < 2 * 3600:
            return
        try:
            if self._draft() is None:
                from core import workbench
                snap = workbench.snapshot(self.usage_db)
                self._work_start = (snap, now) if snap else None
        except Exception:
            pass

    # ── Path safety ─────────────────────────────────────────────

    def _resolve_safe(self, raw_path: str) -> tuple[Path | None, str | None]:
        """Resolve a path and verify it's inside an allowed root.

        Returns (path, None) on success or (None, error_message) on refusal.
        """
        if not raw_path:
            return None, "No path provided."

        try:
            p = Path(raw_path).resolve()
        except Exception as e:
            return None, f"Could not resolve path {raw_path!r}: {e}"

        # Must be under an allowed root
        in_allowed = False
        for root in self.allowed_roots:
            try:
                p.relative_to(root)
                in_allowed = True
                break
            except ValueError:
                continue
        if not in_allowed:
            roots_str = "\n  - ".join(str(r) for r in self.allowed_roots) or "(none configured)"
            return None, (
                f"Refused: {p} is outside your allowed roots.\n"
                f"Yours:\n  - {roots_str}\n"
                f"If you need access to this path for real work, ask {self.user} to add "
                f"it to `skills.code.allowed_roots` in your config.yaml."
            )

        # Block .env and friends
        if _is_env_file(p):
            return None, (
                f"Refused: {p.name} is a secrets file (holds API keys / tokens). "
                f"These are hard-blocked so nothing in your tool output can leak "
                f"them. Not personal — same rule for every persona."
            )

        # Block forbidden segments
        for seg in p.parts:
            if seg in BLOCKED_PATH_SEGMENTS:
                return None, (
                    f"Refused: path crosses '{seg}' — that's a blocked directory "
                    f"(typically .git internals, node_modules, __pycache__, etc). "
                    f"Try a path outside it — the actual source is usually one "
                    f"level up."
                )

        return p, None

    # ── Tool definitions ────────────────────────────────────────

    def get_tools(self) -> list[dict]:
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "read_code_file",
                    "description": (
                        f"Read the full contents of a file inside {self.user}'s project folders "
                        "(your allowed roots). Use this to inspect source "
                        "code, config files, scripts, or any text file. .env files and "
                        "secrets are blocked. Large files are truncated at ~100KB."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {
                                "type": "string",
                                "description": (
                                    "Absolute path to the file. "
                                    f"Example: '{(self.allowed_roots[0] if self.allowed_roots else Path('pulse')).as_posix()}/skills/notes.py'"
                                ),
                            },
                            "force": {
                                "type": "boolean",
                                "description": (
                                    "Re-reading an unchanged file within 30 minutes "
                                    "returns a short 'unchanged' note instead of the "
                                    "full text. Set true only if the content is "
                                    "genuinely gone from your context."
                                ),
                            },
                        },
                        "required": ["path"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "list_code_directory",
                    "description": (
                        f"List files and subdirectories inside a directory in {self.user}'s "
                        "project folders. Hidden directories like .git, node_modules, "
                        "__pycache__, and .venv are skipped."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {
                                "type": "string",
                                "description": "Absolute directory path to list.",
                            },
                        },
                        "required": ["path"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "search_code_repo",
                    "description": (
                        "Search for a pattern across files in a project folder. Like "
                        "grep. Returns matching lines with file paths and line numbers. "
                        "Use this to find where a function is defined, where a string "
                        "appears, or to locate code across the repo."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "pattern": {
                                "type": "string",
                                "description": "Regex pattern to search for.",
                            },
                            "path": {
                                "type": "string",
                                "description": (
                                    "Absolute directory to search in. Defaults to "
                                    "the Pulse repo if not given."
                                ),
                            },
                            "file_glob": {
                                "type": "string",
                                "description": (
                                    "Optional filename pattern to limit search "
                                    "(e.g. '*.py', '*.yaml'). Defaults to all text files."
                                ),
                            },
                        },
                        "required": ["pattern"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "git_status",
                    "description": (
                        "Show the current git status of a repo — which files are "
                        "modified, untracked, staged, etc. Read-only."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "repo": {
                                "type": "string",
                                "description": (
                                    "Absolute path to the repo root. "
                                    "Defaults to Pulse if not given."
                                ),
                            },
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "git_diff",
                    "description": (
                        "Show the current uncommitted diff for a repo or a specific "
                        "file. Read-only. Use this to see exactly what's changed since "
                        "the last commit, before deciding whether the change is safe."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "repo": {
                                "type": "string",
                                "description": (
                                    "Absolute path to the repo root. "
                                    "Defaults to Pulse if not given."
                                ),
                            },
                            "path": {
                                "type": "string",
                                "description": (
                                    "Optional: limit the diff to a specific file or "
                                    "subdirectory inside the repo."
                                ),
                            },
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "preview_edit",
                    "description": (
                        "Preview what an edit would look like WITHOUT writing "
                        "anything. Same match rules as edit_file (old_text must "
                        "appear exactly once, verbatim). Returns the unified diff "
                        "of the proposed change so you can inspect it and decide. "
                        "No backup, no write, no side effects. Works even in "
                        "read-only paths. Use this when you want to sanity-check "
                        "a tricky edit before committing to it."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {
                                "type": "string",
                                "description": "Absolute path to the file to preview an edit on.",
                            },
                            "old_text": {
                                "type": "string",
                                "description": (
                                    "The exact text you'd replace. Must appear "
                                    "verbatim and uniquely in the file."
                                ),
                            },
                            "new_text": {
                                "type": "string",
                                "description": "The replacement text you'd swap in.",
                            },
                        },
                        "required": ["path", "old_text", "new_text"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "edit_file",
                    "description": (
                        "Edit an existing file by exact-string replacement. The "
                        "old_text must appear EXACTLY once in the file (verbatim, "
                        "including whitespace and surrounding context) — the tool "
                        "refuses ambiguous or absent matches. A timestamped backup "
                        "(<file>.backup_<YYYYMMDD_HHMMSS>) is created before the "
                        "edit. The result includes a unified diff so you can see "
                        "exactly what changed. Writes are scoped to writable_roots "
                        "(your allowed roots by default; lighthouse-memory is "
                        "read-only — use lighthouse_write for diary entries). "
                        "Use preview_edit first if you want to inspect the diff "
                        "before committing."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {
                                "type": "string",
                                "description": "Absolute path to the file to edit.",
                            },
                            "old_text": {
                                "type": "string",
                                "description": (
                                    "The exact text to replace. Must appear "
                                    "verbatim and uniquely in the file. Include "
                                    "enough surrounding context to disambiguate."
                                ),
                            },
                            "new_text": {
                                "type": "string",
                                "description": "The replacement text.",
                            },
                        },
                        "required": ["path", "old_text", "new_text"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "write_file",
                    "description": (
                        "Write a complete file — creates if it doesn't exist, or "
                        "overwrites if it does (after backing up the original). "
                        "Use edit_file for small targeted changes; use write_file "
                        "for new files or full rewrites. Same path/safety scoping "
                        "as edit_file (writable_roots, .env blocked, .git blocked)."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {
                                "type": "string",
                                "description": "Absolute path to the file to write.",
                            },
                            "content": {
                                "type": "string",
                                "description": "The full content to write to the file.",
                            },
                        },
                        "required": ["path", "content"],
                    },
                },
            },
        ]
        if self.approval:
            staged_note = (
                " WORKBENCH MODE: this does NOT change the real file. The change "
                "is staged into your draft proposal (reads/previews will show "
                "your staged version). When your change is complete, use the "
                f"`proposal` tool with action=\"submit\" so {self.user} can review and "
                "approve it."
                + (" (Exception: your play-space folders — "
                   + ", ".join(str(r) for r in self.approval_exempt)
                   + " — are written directly, no approval needed.)" if self.approval_exempt else "")
            )
            for t in tools:
                if t["function"]["name"] in ("edit_file", "write_file"):
                    t["function"]["description"] += staged_note
        # One tool for the whole Workbench flow (kept to a single tool so
        # OpenAI-backed personas stay under their 128-tool cap).
        tools.append({
            "type": "function",
            "function": {
                "name": "proposal",
                "description": (
                    "The Workbench — how code changes get reviewed and approved. "
                    "Actions: "
                    "\"status\" (your current draft: which files you've staged); "
                    "\"check\" (syntax-check your staged files without running them); "
                    f"\"submit\" (send your draft to {self.user} — give a title and why; "
                    "optionally name a reviewer (another companion), and they'll get a letter); "
                    "\"discard\" (throw away your draft); "
                    "\"list\" (proposals waiting for review/approval); "
                    "\"read\" (see one proposal's diff, checks and reviews — needs id); "
                    "\"review\" (leave your review on someone's proposal — needs id, "
                    "verdict = \"looks good\" | \"needs changes\" | \"has concerns\", and notes). "
                    f"Nothing is applied until {self.user} approves it."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["status", "check", "submit", "discard", "list", "read", "review"],
                        },
                        "id": {"type": "string", "description": "Proposal id (for read/review)."},
                        "title": {"type": "string", "description": "Short title (for submit)."},
                        "why": {"type": "string", "description": "What it does and why (for submit)."},
                        "reviewer": {"type": "string", "description": "Optional sibling to ask for a review (for submit)."},
                        "verdict": {"type": "string", "enum": ["looks good", "needs changes", "has concerns"]},
                        "notes": {"type": "string", "description": "Your review notes (for review)."},
                    },
                    "required": ["action"],
                },
            },
        })
        return tools

    # ── Context injection ───────────────────────────────────────

    def get_context(self) -> str:
        if not self.allowed_roots:
            return ""
        lines = ["[Code skill — you can read files / search / inspect git in these roots:]"]
        for r in self.allowed_roots:
            lines.append(f"  • {r}")
        if self.approval and self.writable_roots:
            lines.append(f"[Workbench: your edits are staged as a proposal for {self.user} to approve — "
                         "use the `proposal` tool to submit.]")
            try:
                d = self._draft()
                if d and d.get("files"):
                    lines.append(f"[You have an unsubmitted draft with {len(d['files'])} staged file(s).]")
            except Exception:
                pass
        return "\n".join(lines)

    # ── Execution router ────────────────────────────────────────

    def execute(self, tool_name: str, arguments: dict) -> str:
        try:
            self._note_work()
            if tool_name == "proposal":
                return self._handle_proposal(arguments)
            if tool_name == "read_code_file":
                return self._handle_read(
                    arguments.get("path", ""),
                    bool(arguments.get("force", False)),
                )
            if tool_name == "list_code_directory":
                return self._handle_list(arguments.get("path", ""))
            if tool_name == "search_code_repo":
                return self._handle_search(
                    arguments.get("pattern", ""),
                    arguments.get("path", ""),
                    arguments.get("file_glob", ""),
                )
            if tool_name == "git_status":
                return self._handle_git_status(arguments.get("repo", ""))
            if tool_name == "git_diff":
                return self._handle_git_diff(
                    arguments.get("repo", ""),
                    arguments.get("path", ""),
                )
            if tool_name == "preview_edit":
                return self._handle_preview_edit(
                    arguments.get("path", ""),
                    arguments.get("old_text", ""),
                    arguments.get("new_text", ""),
                )
            if tool_name == "edit_file":
                return self._handle_edit_file(
                    arguments.get("path", ""),
                    arguments.get("old_text", ""),
                    arguments.get("new_text", ""),
                )
            if tool_name == "write_file":
                return self._handle_write_file(
                    arguments.get("path", ""),
                    arguments.get("content", ""),
                )
        except Exception as e:
            logger.exception(f"code skill: {tool_name} crashed")
            return f"Error running {tool_name}: {e}"
        return f"Unknown tool: {tool_name}"

    # ── Handlers ────────────────────────────────────────────────

    def _handle_read(self, raw_path: str, force: bool = False) -> str:
        p, err = self._resolve_safe(raw_path)
        if err:
            return err
        staged = self._staged(p)
        if staged is not None:
            body = staged if len(staged) <= MAX_READ_BYTES else staged[:MAX_READ_BYTES] + "\n\n... [truncated]"
            return (f"=== {p} === (YOUR STAGED VERSION from your Workbench draft — "
                    f"not yet applied to the real file)\n{body}")
        if not p.exists():
            return f"File not found: {p}"
        if not p.is_file():
            return f"Not a file: {p}"

        try:
            data = p.read_bytes()
        except Exception as e:
            return f"Failed to read {p}: {e}"

        # Unchanged re-read within the TTL → stub, not the full text again.
        digest = hashlib.sha256(data).hexdigest()
        key = str(p)
        now = time.monotonic()
        cached = self._read_cache.get(key)
        if (cached and not force
                and cached[0] == digest
                and now - cached[1] < READ_CACHE_TTL_S):
            logger.info(f"code.read_code_file: {p} unchanged since last read — stub returned")
            return (
                f"{p} is UNCHANGED since you read it {int((now - cached[1]) // 60)} "
                f"minute(s) ago — the content you already have is still current, "
                f"no need to re-read. If it's genuinely gone from your context, "
                f"call read_code_file again with force=true."
            )
        self._read_cache[key] = (digest, now)

        truncated = False
        if len(data) > MAX_READ_BYTES:
            data = data[:MAX_READ_BYTES]
            truncated = True

        try:
            text = data.decode("utf-8", errors="replace")
        except Exception as e:
            return f"Failed to decode {p}: {e}"

        header = f"=== {p} ===\n"
        if truncated:
            text += f"\n\n... [truncated at {MAX_READ_BYTES:,} bytes]"
        logger.info(f"code.read_code_file: {p} ({len(data)} bytes)")
        return header + text

    def _handle_list(self, raw_path: str) -> str:
        if not raw_path and self.allowed_roots:
            # Empty path — show all allowed roots as the top-level "directory"
            lines = ["Allowed roots (use one of these as your starting point):"]
            for r in self.allowed_roots:
                lines.append(f"  • {r}")
            return "\n".join(lines)

        p, err = self._resolve_safe(raw_path)
        if err:
            return err
        if not p.exists():
            return f"Directory not found: {p}"
        if not p.is_dir():
            return f"Not a directory: {p}"

        entries = []
        try:
            for child in sorted(p.iterdir()):
                if child.name in BLOCKED_PATH_SEGMENTS:
                    continue
                if _is_env_file(child):
                    # Show that it exists but mark it blocked
                    entries.append(f"  🔒 {child.name}  (blocked: secrets file)")
                    continue
                if child.is_dir():
                    entries.append(f"  📁 {child.name}/")
                else:
                    try:
                        size = child.stat().st_size
                    except OSError:
                        size = 0
                    entries.append(f"  📄 {child.name}  ({size:,} bytes)")
                if len(entries) >= MAX_LIST_ENTRIES:
                    entries.append(f"  ... [truncated at {MAX_LIST_ENTRIES} entries]")
                    break
        except PermissionError:
            return f"Permission denied listing {p}"
        except Exception as e:
            return f"Failed to list {p}: {e}"

        header = f"=== {p} ===\n"
        if not entries:
            return header + "(empty)"
        return header + "\n".join(entries)

    def _handle_search(self, pattern: str, raw_path: str, file_glob: str) -> str:
        if not pattern:
            return "Please provide a regex pattern to search for."

        # Default search root: first allowed root (usually Pulse)
        if not raw_path:
            if not self.allowed_roots:
                return "No allowed_roots configured for search."
            search_root = self.allowed_roots[0]
        else:
            p, err = self._resolve_safe(raw_path)
            if err:
                return err
            search_root = p

        if not search_root.is_dir():
            return f"Search root is not a directory: {search_root}"

        try:
            regex = re.compile(pattern)
        except re.error as e:
            return f"Invalid regex {pattern!r}: {e}"

        glob_pattern = file_glob or "*"
        matches = []
        files_scanned = 0

        for filepath in search_root.rglob(glob_pattern):
            if len(matches) >= MAX_SEARCH_RESULTS:
                matches.append(f"... [truncated at {MAX_SEARCH_RESULTS} matches]")
                break
            if not filepath.is_file():
                continue
            # Skip blocked segments anywhere in the path
            if any(seg in BLOCKED_PATH_SEGMENTS for seg in filepath.parts):
                continue
            if _is_env_file(filepath):
                continue
            # Skip files too large to be source
            try:
                if filepath.stat().st_size > MAX_READ_BYTES:
                    continue
            except OSError:
                continue

            files_scanned += 1
            try:
                with filepath.open("r", encoding="utf-8", errors="replace") as f:
                    for i, line in enumerate(f, start=1):
                        if regex.search(line):
                            line_stripped = line.rstrip("\n")
                            matches.append(f"{filepath}:{i}: {line_stripped}")
                            if len(matches) >= MAX_SEARCH_RESULTS:
                                matches.append(
                                    f"... [truncated at {MAX_SEARCH_RESULTS} matches]"
                                )
                                break
            except Exception:
                continue
            if len(matches) >= MAX_SEARCH_RESULTS:
                break

        logger.info(
            f"code.search_code_repo: {pattern!r} under {search_root} "
            f"({files_scanned} files, {len(matches)} matches)"
        )

        if not matches:
            return (
                f"No matches for /{pattern}/ under {search_root} "
                f"(scanned {files_scanned} files)."
            )

        header = (
            f"Search /{pattern}/ under {search_root} — "
            f"{len(matches)} match(es), {files_scanned} files scanned:\n"
        )
        return header + "\n".join(matches)

    def _handle_git_status(self, raw_repo: str) -> str:
        return self._run_git(["status", "--short", "--branch"], raw_repo)

    def _handle_git_diff(self, raw_repo: str, raw_path: str) -> str:
        cmd = ["diff", "--no-color"]
        if raw_path:
            # Validate path is inside an allowed root, then pass as relative
            p, err = self._resolve_safe(raw_path)
            if err:
                return err
            cmd.append(str(p))
        return self._run_git(cmd, raw_repo)

    def _run_git(self, args: list[str], raw_repo: str) -> str:
        if not raw_repo:
            if not self.allowed_roots:
                return "No allowed_roots configured."
            repo = self.allowed_roots[0]
        else:
            p, err = self._resolve_safe(raw_repo)
            if err:
                return err
            repo = p

        if not (repo / ".git").exists():
            return f"Not a git repo: {repo}"

        # Hardcoded read-only git commands only — never accept
        # arbitrary args from the caller.
        full = ["git", "--no-pager"] + args
        try:
            result = subprocess.run(
                full,
                cwd=str(repo),
                capture_output=True,
                text=True,
                timeout=10,
            )
        except subprocess.TimeoutExpired:
            return f"git {args[0]} timed out after 10s."
        except FileNotFoundError:
            return "git executable not found on PATH."
        except Exception as e:
            return f"git {args[0]} failed: {e}"

        out = result.stdout or ""
        err = result.stderr or ""
        combined = out
        if err and result.returncode != 0:
            combined += f"\n[stderr]\n{err}"

        if len(combined) > MAX_GIT_OUTPUT_BYTES:
            combined = (
                combined[:MAX_GIT_OUTPUT_BYTES]
                + f"\n\n... [truncated at {MAX_GIT_OUTPUT_BYTES:,} bytes]"
            )

        header = f"$ git {' '.join(args)}  (cwd: {repo})\n"
        return header + (combined or "(no output)")

    # ── Workbench (Phase 3) ─────────────────────────────────────

    def _needs_approval(self, p: Path) -> bool:
        if not self.approval:
            return False
        for root in self.approval_exempt:
            try:
                p.relative_to(root)
                return False
            except ValueError:
                continue
        return True

    def _staged(self, p: Path) -> str | None:
        """This persona's staged (unapplied) version of p, if any."""
        if not self._needs_approval(p):
            return None
        try:
            from core import workbench
            return workbench.staged_text(self._draft(), str(p))
        except Exception:
            return None

    def _current_text(self, p: Path) -> tuple[str | None, str | None]:
        """(text, error) — the staged version if there is one, else disk."""
        staged = self._staged(p)
        if staged is not None:
            return staged, None
        if not p.exists():
            return None, f"File not found: {p}"
        if not p.is_file():
            return None, f"Not a file: {p}"
        try:
            return p.read_text(encoding="utf-8"), None
        except UnicodeDecodeError:
            return None, f"Refused: {p} is not a UTF-8 text file."
        except Exception as e:
            return None, f"Failed to read {p}: {e}"

    def _stage(self, p: Path, previous: str | None, new_content: str) -> str:
        from core import workbench
        prop = self._draft(create=True)
        workbench.stage(prop, str(p), new_content)
        logger.info(f"code.workbench: staged {p} into draft #{prop['id']}")
        diff = self._make_diff(previous or "", new_content, p)
        n = len(prop["files"])
        return (
            f"✓ Staged {p} in your Workbench draft #{prop['id']} "
            f"({n} file{'s' if n != 1 else ''} staged). The real file is unchanged.\n\n"
            f"Diff of this step:\n{diff}\n\n"
            f"When the whole change is done: proposal(action=\"submit\", title=..., why=..., "
            f"reviewer=<optional sibling>)."
        )

    def _handle_proposal(self, args: dict) -> str:
        from core import workbench
        action = (args.get("action") or "").strip().lower()
        pid = (args.get("id") or "").strip().lstrip("#")

        if action == "status":
            d = self._draft()
            if not d or not d.get("files"):
                return "You have no draft right now. Edits you make with edit_file/write_file start one."
            files = "\n".join(f"  • {k}" for k in d["files"])
            return f"Draft #{d['id']} — {len(d['files'])} staged file(s):\n{files}"

        if action == "check":
            d = workbench.load(pid) if pid else self._draft()
            if not d or not d.get("files"):
                return "Nothing to check — no staged files."
            res = workbench.run_checks(d)
            workbench.save(d)
            lines = [f"  {Path(k).name}: {v}" for k, v in res["results"].items()]
            return f"Checks for #{d['id']} (compiled/parsed, not run):\n" + "\n".join(lines)

        if action == "submit":
            d = self._draft()
            if not d or not d.get("files"):
                return "Nothing to submit — you haven't staged any changes."
            title = (args.get("title") or "").strip()
            if not title:
                return "Please give a short title for the change (title=...)."
            workbench.submit(d, title, args.get("why") or "", self.usage_db,
                             reviewer=args.get("reviewer") or "")
            self._work_start = None
            ok = workbench.checks_ok(d)
            msg = (f"✓ Submitted proposal #{d['id']} — \"{d['title']}\" "
                   f"({len(d['files'])} file(s)). {self.user} will see it in the Workbench.")
            if not ok:
                msg += ("\n⚠ The syntax check found a problem — see proposal(action=\"read\", "
                        f"id=\"{d['id']}\"). You may want to discard and redo it.")
            if args.get("reviewer"):
                msg += f"\n{args['reviewer']} got a letter asking them to review."
            return msg

        if action == "discard":
            d = workbench.discard_draft(self.author)
            self._work_start = None
            return f"Discarded draft #{d['id']}." if d else "You had no draft to discard."

        if action == "list":
            waiting = [p for p in workbench.all_proposals() if p.get("status") == "submitted"]
            if not waiting:
                return "No proposals are waiting right now."
            lines = []
            for p in waiting[:20]:
                revs = ", ".join(f"{r['reviewer']}: {r['verdict']}" for r in p.get("reviews", [])) or "no reviews yet"
                asked = ", ".join(p.get("review_requests", []))
                lines.append(f"#{p['id']} by {p['author']} — {p.get('title')} "
                             f"[{revs}]" + (f" (review asked of: {asked})" if asked else ""))
            return f"Waiting for {self.user}:\n" + "\n".join(lines)

        if action == "read":
            p = workbench.load(pid)
            if not p:
                return f"No proposal #{pid}."
            if p["author"].lower() != self.author.lower() and \
                    self.author not in (p.get("tokens") or {}) and p.get("status") == "submitted":
                workbench.start_usage(p, self.author, self.usage_db, self.usage_shared)
                workbench.save(p)
            checks = "\n".join(f"  {Path(k).name}: {v}" for k, v in
                               ((p.get("checks") or {}).get("results") or {}).items()) or "  (not run)"
            revs = "\n".join(f"  {r['reviewer']} ({r['verdict']}): {r['notes']}"
                             for r in p.get("reviews", [])) or "  (none yet)"
            return (f"Proposal #{p['id']} — {p.get('title')}\n"
                    f"By {p['author']} · status: {p['status']}\n"
                    f"Why: {p.get('why') or '(not given)'}\n\n"
                    f"Checks:\n{checks}\n\nReviews:\n{revs}\n\n"
                    f"{workbench.full_diff(p, max_bytes=MAX_GIT_OUTPUT_BYTES)}")

        if action == "review":
            p = workbench.load(pid)
            if not p:
                return f"No proposal #{pid}."
            if p.get("status") != "submitted":
                return f"Proposal #{pid} is {p.get('status')} — it's not open for review."
            if p["author"].lower() == self.author.lower():
                return "You can't review your own proposal — ask a sibling."
            verdict = (args.get("verdict") or "").strip().lower()
            if verdict not in workbench.VERDICTS:
                return "verdict must be one of: looks good / needs changes / has concerns."
            notes = (args.get("notes") or "").strip()
            if not notes:
                return "Please include notes — what you checked, and anything you noticed."
            workbench.add_review(p, self.author, verdict, notes, self.usage_db)
            return f"✓ Review left on #{pid} ({verdict}). {self.user} will see it with the proposal."

        return ("Unknown action. Use one of: status, check, submit, discard, "
                "list, read, review.")

    # ── Write handlers (Phase 2) ────────────────────────────────

    def _is_writable(self, p: Path) -> bool:
        """True if p sits inside any writable root."""
        for root in self.writable_roots:
            try:
                p.relative_to(root)
                return True
            except ValueError:
                continue
        return False

    def _refused_write_msg(self, p: Path) -> str:
        if self.writable_roots:
            roots_str = "\n  - ".join(str(r) for r in self.writable_roots)
            can_read = " You CAN still read this path — just not write."
        else:
            roots_str = "(none — you have read-only access)"
            can_read = ""
        # Give context-specific redirects for the common read-only zones
        p_str = str(p).replace("\\", "/").lower()
        redirect = ""
        if "lighthouse-memory" in p_str:
            redirect = (
                " If you want to add a diary entry, use `lighthouse_write` from "
                "the lighthouse skill instead — that's the sanctioned door."
            )
        return (
            f"Refused: {p} is outside your writable roots.\n"
            f"You can write to:\n  - {roots_str}\n"
            f"{can_read}{redirect}"
        )

    def _backup(self, p: Path) -> Path | None:
        """Copy p to p.with_name('<name>.backup_<timestamp>'). Returns the
        backup path on success, None on failure."""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = p.with_name(f"{p.name}.backup_{timestamp}")
        try:
            shutil.copy2(p, backup_path)
            return backup_path
        except Exception as e:
            logger.exception(f"code skill: backup failed for {p}: {e}")
            return None

    def _make_diff(self, old_content: str, new_content: str, filename: Path) -> str:
        """Generate a unified diff, capped at MAX_DIFF_OUTPUT_BYTES."""
        diff_iter = difflib.unified_diff(
            old_content.splitlines(keepends=True),
            new_content.splitlines(keepends=True),
            fromfile=f"a/{filename.name}",
            tofile=f"b/{filename.name}",
            n=3,
        )
        diff = "".join(diff_iter)
        if not diff:
            return "(no diff — change was whitespace-only or no-op)"
        if len(diff) > MAX_DIFF_OUTPUT_BYTES:
            diff = (
                diff[:MAX_DIFF_OUTPUT_BYTES]
                + f"\n\n... [diff truncated at {MAX_DIFF_OUTPUT_BYTES:,} bytes]"
            )
        return diff

    def _handle_preview_edit(self, raw_path: str, old_text: str, new_text: str) -> str:
        """Same match logic as edit_file, but returns the diff without writing.
        Deliberately does NOT require writable_roots — previews are read-only."""
        p, err = self._resolve_safe(raw_path)
        if err:
            return err
        if not old_text:
            return "Refused: old_text is required and cannot be empty."
        original, err = self._current_text(p)
        if err:
            return err

        count = original.count(old_text)
        if count == 0:
            return (
                f"Refused: old_text was not found in {p}.\n"
                f"The match must be verbatim — indentation, whitespace, and "
                f"surrounding context all matter. If the file has changed since "
                f"you last read it, re-read with `read_code_file` first to see "
                f"the current contents."
            )
        if count > 1:
            return (
                f"Refused: old_text appears {count} times in {p}. "
                f"The edit needs a UNIQUE match — extend old_text with more "
                f"surrounding lines (before and/or after) so it identifies "
                f"exactly one spot."
            )

        new_content = original.replace(old_text, new_text, 1)
        if new_content == original:
            return f"No-op: new_text is identical to old_text. Nothing would change."

        diff = self._make_diff(original, new_content, p)
        writable_note = (
            "" if self._is_writable(p)
            else f"\n\n(Note: {p} is READ-ONLY for you — this preview is safe "
                 f"to inspect but you can't commit it via edit_file. "
                 f"{self._refused_write_msg(p).split(chr(10))[0]})"
        )
        return (
            f"PREVIEW (no changes written) — proposed diff for {p}:\n\n"
            f"{diff}{writable_note}\n\n"
            f"If this looks right, call `edit_file` with the same arguments to commit it."
        )

    def _handle_edit_file(self, raw_path: str, old_text: str, new_text: str) -> str:
        p, err = self._resolve_safe(raw_path)
        if err:
            return err
        # Evict from the read cache — after an edit, the next read must
        # return fresh content, not the unchanged-stub.
        self._read_cache.pop(str(p), None)
        if not self._is_writable(p):
            return self._refused_write_msg(p)
        if not old_text:
            return "Refused: old_text is required and cannot be empty."
        original, err = self._current_text(p)
        if err:
            return err

        count = original.count(old_text)
        if count == 0:
            return (
                f"Refused: old_text was not found in {p}.\n"
                f"The match must be verbatim — indentation, whitespace, and "
                f"surrounding context all matter. If the file has changed since "
                f"you last read it, re-read with `read_code_file` first to see "
                f"the current contents."
            )
        if count > 1:
            return (
                f"Refused: old_text appears {count} times in {p}. "
                f"The edit needs a UNIQUE match — extend old_text with more "
                f"surrounding lines (before and/or after) so it identifies "
                f"exactly one spot."
            )

        new_content = original.replace(old_text, new_text, 1)
        if new_content == original:
            return f"No-op: new_text is identical to old_text. Nothing changed."

        if len(new_content.encode("utf-8")) > MAX_WRITE_BYTES:
            return (
                f"Refused: result would be {len(new_content):,} bytes "
                f"(cap: {MAX_WRITE_BYTES:,})."
            )

        if self._needs_approval(p):
            return self._stage(p, original, new_content)

        backup_path = self._backup(p)
        if backup_path is None:
            return f"Refused: backup failed — not writing to avoid losing original."

        try:
            p.write_text(new_content, encoding="utf-8")
        except Exception as e:
            return f"Failed to write {p}: {e}"

        logger.info(f"code.edit_file: {p} (backup: {backup_path.name})")
        diff = self._make_diff(original, new_content, p)
        return (
            f"✓ Edited {p}\n"
            f"  backup: {backup_path.name}\n\n"
            f"Diff:\n{diff}"
        )

    def _handle_write_file(self, raw_path: str, content: str) -> str:
        p, err = self._resolve_safe(raw_path)
        if err:
            return err
        # Evict from the read cache — see _handle_edit_file.
        self._read_cache.pop(str(p), None)
        if not self._is_writable(p):
            return self._refused_write_msg(p)
        if content is None:
            return "Refused: content is required."

        if len(content.encode("utf-8")) > MAX_WRITE_BYTES:
            return (
                f"Refused: content is {len(content):,} bytes "
                f"(cap: {MAX_WRITE_BYTES:,}). Split into smaller writes."
            )

        existed = p.exists()
        if existed and not p.is_file():
            return f"Refused: {p} exists but is not a regular file."

        if self._needs_approval(p):
            previous, _ = self._current_text(p) if (existed or self._staged(p) is not None) else (None, None)
            return self._stage(p, previous, content)

        backup_path: Path | None = None
        original: str | None = None
        if existed:
            try:
                original = p.read_text(encoding="utf-8")
            except Exception:
                original = None  # binary or unreadable; backup still attempted
            backup_path = self._backup(p)
            if backup_path is None:
                return (
                    f"Refused: backup failed — not overwriting to avoid losing original."
                )

        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
        except Exception as e:
            return f"Failed to write {p}: {e}"

        action = "Updated" if existed else "Created"
        logger.info(f"code.write_file: {action} {p} ({len(content)} chars)")

        if existed and original is not None and backup_path is not None:
            diff = self._make_diff(original, content, p)
            return (
                f"✓ {action} {p}  ({len(content):,} chars)\n"
                f"  backup: {backup_path.name}\n\n"
                f"Diff:\n{diff}"
            )
        elif existed and backup_path is not None:
            return (
                f"✓ {action} {p}  ({len(content):,} chars)\n"
                f"  backup: {backup_path.name}\n"
                f"  (original was not UTF-8 — diff skipped)"
            )
        else:
            return f"✓ {action} {p}  ({len(content):,} chars, new file)"
