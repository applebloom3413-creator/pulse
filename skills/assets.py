"""
Asset librarian skill — inventory, tag, and organize files in a folder tree.

The companion can:
  - inventory_scan       → walk a folder, build/update the file index
  - list_assets          → list files filtered by tag, type, or folder
  - tag_asset            → add tag(s) to a file (e.g. 'ui-icon', 'menu-music')
  - untag_asset          → remove tag(s) from a file
  - find_duplicates      → find files with identical content via SHA-256
  - inventory_report     → high-level summary: counts, sizes, file types
  - asset_details        → full info on one file (tags, hash, size, etc.)
  - list_tags            → all tags currently in use, with counts

Storage:
  A small SQLite db at <persona>/data/assets/inventory.db with two tables:
    files(path, hash, size, mtime, type, last_scanned)
    tags(path, tag, added_at)

Re-scans are incremental: a file is only re-hashed if its size or mtime
changed since last scan. Walks skip common junk folders (.git, node_modules,
__pycache__, build/, dist/, .venv) unless overridden.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

from skills.base import BaseSkill

logger = logging.getLogger(__name__)

# Files above this size get inventoried but skip the content hash
# (sha256 of a 2 GB video is slow and rarely useful for dedup).
HASH_SIZE_LIMIT = 200 * 1024 * 1024  # 200 MB

# Skip these folder names when walking trees. Override per-call if needed.
DEFAULT_SKIP_DIRS = {
    ".git", ".svn", ".hg",
    "node_modules", "__pycache__", ".venv", "venv", "env",
    "build", "dist", ".next", ".expo", ".gradle",
    ".idea", ".vscode",
    "Pods", "DerivedData",
}

# File type buckets by extension.
TYPE_MAP = {
    "image": {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg",
              ".bmp", ".tiff", ".tif", ".ico", ".heic", ".avif"},
    "audio": {".mp3", ".wav", ".ogg", ".m4a", ".flac", ".aac", ".opus"},
    "video": {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v"},
    "font":  {".ttf", ".otf", ".woff", ".woff2", ".eot"},
    "3d":    {".obj", ".fbx", ".gltf", ".glb", ".blend", ".dae", ".stl"},
    "code":  {".py", ".js", ".ts", ".tsx", ".jsx", ".swift", ".kt",
              ".java", ".go", ".rs", ".cs", ".cpp", ".c", ".h", ".hpp",
              ".rb", ".php", ".lua", ".dart", ".sh", ".ps1"},
    "doc":   {".md", ".txt", ".pdf", ".doc", ".docx", ".odt", ".rtf"},
    "data":  {".json", ".yaml", ".yml", ".xml", ".csv", ".tsv",
              ".sqlite", ".db", ".sql"},
    "archive": {".zip", ".tar", ".gz", ".bz2", ".rar", ".7z", ".xz"},
}


def _classify(ext: str) -> str:
    ext = ext.lower()
    for type_name, exts in TYPE_MAP.items():
        if ext in exts:
            return type_name
    return "other"


def _human_size(num_bytes: int) -> str:
    n = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} TB"


class AssetsSkill(BaseSkill):
    name = "assets"

    def __init__(self, config: dict):
        super().__init__(config)
        persona_dir = config.get("_persona_dir", "")
        if persona_dir:
            self.data_dir = Path(persona_dir) / "data" / "assets"
        else:
            self.data_dir = Path("data") / "assets"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "inventory.db"
        self._init_db()

        # ── Mover guardrails ──────────────────────────────────────
        # The mover may ONLY touch files under these allow-listed roots,
        # read from config (skills.assets.organize_roots). An EMPTY list
        # means this persona can catalog/tag but CANNOT move anything —
        # the safe default. This is the security boundary: only the user, by
        # editing config, can widen what the companion is allowed to move.
        assets_cfg = (config.get("skills", {}) or {}).get("assets", {}) or {}
        self.organize_roots = []
        for r in (assets_cfg.get("organize_roots") or []):
            try:
                self.organize_roots.append(str(Path(r).resolve()))
            except Exception:
                logger.warning(f"assets: bad organize_root ignored: {r!r}")
        trash = assets_cfg.get("trash_dir")
        self.trash_dir = Path(trash) if trash else (self.data_dir / "_trash")

        # ── OCR text index ────────────────────────────────────────
        # Screenshots were OCR'd into a jsonl ({path, mtime, text}). Pointing
        # the librarian at it lets the companion READ what's actually inside a
        # screenshot — so e.g. two authors mislabeled by model number can
        # be told apart by voice and re-sorted. Configurable; lazily cached.
        # Generic default; real deployments set skills.assets.ocr_index. Kept
        # generic so shared/upstream code carries no one's personal path. If the
        # index is absent, the OCR read/search tools just say so.
        self.ocr_index_path = Path(
            assets_cfg.get("ocr_index") or "data/screenshots_index.jsonl"
        )
        self._ocr_cache: dict | None = None  # {basename_casefold: [(orig_path, text), ...]}

    @staticmethod
    def _ocr_base(p) -> str:
        """Filename key for OCR matching. We join on BASENAME, not full path:
        the librarian (and the user) move/rename folders, but the static OCR index
        keeps the original paths — so full-path matching breaks after a sort.
        Filenames (IMG_1234(1).PNG) are stable across moves and effectively
        unique in this archive."""
        return Path(p).name.casefold()

    def _load_ocr(self) -> dict:
        """Lazily load the OCR index into {basename_casefold: [(orig_path, text)]}.
        Cached for the process lifetime; {} if missing/unreadable. Basename-keyed
        (list value) so it survives moves; collisions (rare) keep all candidates."""
        if self._ocr_cache is not None:
            return self._ocr_cache
        cache: dict[str, list[tuple[str, str]]] = {}
        if self.ocr_index_path.exists():
            try:
                # utf-8-sig: the index carries a BOM.
                with self.ocr_index_path.open(encoding="utf-8-sig", errors="replace") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rec = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        p = rec.get("path")
                        if p:
                            cache.setdefault(self._ocr_base(p), []).append(
                                (p, rec.get("text", "") or "")
                            )
            except Exception as e:
                logger.warning(f"assets: could not load OCR index: {e}")
        self._ocr_cache = cache
        return cache

    # ── DB setup ─────────────────────────────────────────────────

    def _init_db(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS files (
                    path TEXT PRIMARY KEY,
                    hash TEXT,
                    size INTEGER,
                    mtime REAL,
                    type TEXT,
                    last_scanned TEXT
                );
                CREATE INDEX IF NOT EXISTS files_hash ON files(hash);
                CREATE INDEX IF NOT EXISTS files_type ON files(type);

                CREATE TABLE IF NOT EXISTS tags (
                    path TEXT NOT NULL,
                    tag TEXT NOT NULL,
                    added_at TEXT,
                    PRIMARY KEY (path, tag)
                );
                CREATE INDEX IF NOT EXISTS tags_tag ON tags(tag);

                -- Reversible op-log for the mover. Every physical move is
                -- recorded here so any batch can be undone.
                CREATE TABLE IF NOT EXISTS moves (
                    batch_id TEXT NOT NULL,
                    src TEXT NOT NULL,
                    dst TEXT NOT NULL,
                    ts TEXT,
                    undone INTEGER DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS moves_batch ON moves(batch_id);
            """)

    # ── Tool definitions ─────────────────────────────────────────

    def get_tools(self) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "inventory_scan",
                    "description": (
                        "Walk a folder tree and build (or update) the asset "
                        "inventory. Incremental — files unchanged since last "
                        "scan are skipped, so re-runs are fast. Skips common "
                        "junk folders (.git, node_modules, __pycache__, build, "
                        "etc.) by default. Returns a summary of what was found."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "folder_path": {
                                "type": "string",
                                "description": "Absolute path of the folder to scan.",
                            },
                            "include_hidden": {
                                "type": "boolean",
                                "description": "Include dotfiles and .folders (default false).",
                                "default": False,
                            },
                        },
                        "required": ["folder_path"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "list_assets",
                    "description": (
                        "List files in the inventory, with optional filters. "
                        "Use this to browse what's been indexed. Combine "
                        "filters to narrow down (e.g. type='audio' + "
                        "folder_path='/path/to/sounds')."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "folder_path": {
                                "type": "string",
                                "description": "Limit to files under this folder.",
                            },
                            "tag": {
                                "type": "string",
                                "description": "Only files with this tag.",
                            },
                            "file_type": {
                                "type": "string",
                                "description": "image | audio | video | font | 3d | code | doc | data | archive | other",
                            },
                            "limit": {
                                "type": "integer",
                                "description": "Max results (default 50, max 500).",
                                "default": 50,
                            },
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "tag_asset",
                    "description": (
                        "Add one or more tags to a file. Tags are how you "
                        "build meaning on top of the raw inventory — what's "
                        "a UI icon vs a character sprite vs an unused draft. "
                        "Tags are free-form; pick a convention and stick to it."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "file_path": {
                                "type": "string",
                                "description": "Absolute path of the file.",
                            },
                            "tags": {
                                "type": "string",
                                "description": (
                                    "Comma-separated tags "
                                    "(e.g. 'ui-icon, menu, dark-mode')."
                                ),
                            },
                        },
                        "required": ["file_path", "tags"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "untag_asset",
                    "description": "Remove one or more tags from a file.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "file_path": {
                                "type": "string",
                                "description": "Absolute path of the file.",
                            },
                            "tags": {
                                "type": "string",
                                "description": "Comma-separated tags to remove.",
                            },
                        },
                        "required": ["file_path", "tags"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "find_duplicates",
                    "description": (
                        "Find files with identical content (same SHA-256 hash). "
                        "Returns groups of duplicates. Useful for cleaning up "
                        "redundant assets. Run inventory_scan first."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "folder_path": {
                                "type": "string",
                                "description": "Limit to files under this folder (optional).",
                            },
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "inventory_report",
                    "description": (
                        "High-level summary of the inventory: total file count, "
                        "total size, breakdown by type, biggest files, recently "
                        "modified. Good for getting your bearings or sharing a "
                        "status update for the user."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "folder_path": {
                                "type": "string",
                                "description": "Limit report to files under this folder.",
                            },
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "asset_details",
                    "description": "Full details on a single file: size, hash, type, tags, when scanned.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "file_path": {
                                "type": "string",
                                "description": "Absolute path of the file.",
                            },
                        },
                        "required": ["file_path"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "list_tags",
                    "description": "List all tags currently in use, with how many files each one is on.",
                    "parameters": {
                        "type": "object",
                        "properties": {},
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "read_asset_text",
                    "description": (
                        "Read the OCR'd TEXT inside an image/screenshot — what it "
                        "actually says, not just its filename. Use this to tell apart "
                        "files the auto-tagging got wrong (e.g. a screenshot labeled by "
                        "model number that's really a different companion's voice), then "
                        "re-tag/move it correctly. Returns the recognized text."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "file_path": {"type": "string", "description": "Absolute path of the image to read text from."},
                        },
                        "required": ["file_path"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "search_asset_text",
                    "description": (
                        "Search the OCR'd text INSIDE screenshots for a phrase — find "
                        "every screenshot whose content mentions something. Optionally "
                        "limit to files carrying a tag. Great for re-sorting by what's "
                        "actually said (e.g. find one author's voice among another's tagged "
                        "shots). Returns matching files with a snippet around the hit."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string", "description": "Text to search for inside screenshots."},
                            "tag": {"type": "string", "description": "Optional: only search files carrying this tag (e.g. 'Ada')."},
                            "limit": {"type": "integer", "description": "Max results (default 20, max 100)."},
                        },
                        "required": ["query"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "organize_status",
                    "description": (
                        "Show what you're ALLOWED to reorganize: the folders you "
                        "may move files within, where soft-deleted files go, and "
                        "your last undoable batch. Check this before moving things "
                        "so you know your boundaries."
                    ),
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "move_assets_by_tag",
                    "description": (
                        "Move EVERY file carrying a given tag into a destination "
                        "folder — e.g. consolidate all 'Ada' screenshots into the "
                        "Ada folder. Tags follow the files automatically. "
                        "Safe: only works inside your allowed roots, never "
                        "overwrites (collisions get a numbered suffix), and the "
                        "whole batch is reversible with undo_last_organize."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "tag": {"type": "string", "description": "Move files carrying this tag (e.g. 'Ada')."},
                            "dest_folder": {"type": "string", "description": "Absolute path of the destination folder (must be inside an allowed root)."},
                        },
                        "required": ["tag", "dest_folder"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "move_asset",
                    "description": (
                        "Move a single file into a destination folder. Same safety "
                        "rules as move_assets_by_tag: allowed-roots only, no "
                        "overwrite, reversible."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "file_path": {"type": "string", "description": "Absolute path of the file to move."},
                            "dest_folder": {"type": "string", "description": "Absolute path of the destination folder (inside an allowed root)."},
                        },
                        "required": ["file_path", "dest_folder"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "quarantine_asset",
                    "description": (
                        "Soft-delete a file: move it to the trash folder instead of "
                        "deleting it. Nothing is ever truly erased — the user empties the "
                        "trash themselves. Reversible with undo_last_organize."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "file_path": {"type": "string", "description": "Absolute path of the file to quarantine."},
                        },
                        "required": ["file_path"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "undo_last_organize",
                    "description": (
                        "Reverse the most recent move/quarantine batch, putting every "
                        "file back where it came from. Use this the moment a "
                        "reorganization doesn't look right."
                    ),
                    "parameters": {"type": "object", "properties": {}},
                },
            },
        ]

    # ── Execution router ─────────────────────────────────────────

    def execute(self, tool_name: str, arguments: dict) -> str:
        try:
            if tool_name == "inventory_scan":
                return self._inventory_scan(
                    arguments.get("folder_path", ""),
                    arguments.get("include_hidden", False),
                )
            elif tool_name == "list_assets":
                return self._list_assets(
                    folder_path=arguments.get("folder_path"),
                    tag=arguments.get("tag"),
                    file_type=arguments.get("file_type"),
                    limit=arguments.get("limit", 50),
                )
            elif tool_name == "tag_asset":
                return self._tag_asset(
                    arguments.get("file_path", ""),
                    arguments.get("tags", ""),
                )
            elif tool_name == "untag_asset":
                return self._untag_asset(
                    arguments.get("file_path", ""),
                    arguments.get("tags", ""),
                )
            elif tool_name == "find_duplicates":
                return self._find_duplicates(arguments.get("folder_path"))
            elif tool_name == "inventory_report":
                return self._inventory_report(arguments.get("folder_path"))
            elif tool_name == "asset_details":
                return self._asset_details(arguments.get("file_path", ""))
            elif tool_name == "list_tags":
                return self._list_tags()
            elif tool_name == "read_asset_text":
                return self._read_asset_text(arguments.get("file_path", ""))
            elif tool_name == "search_asset_text":
                return self._search_asset_text(
                    arguments.get("query", ""),
                    arguments.get("tag"),
                    arguments.get("limit", 20),
                )
            elif tool_name == "organize_status":
                return self._organize_status()
            elif tool_name == "move_assets_by_tag":
                return self._move_assets_by_tag(
                    arguments.get("tag", ""), arguments.get("dest_folder", "")
                )
            elif tool_name == "move_asset":
                return self._move_asset(
                    arguments.get("file_path", ""), arguments.get("dest_folder", "")
                )
            elif tool_name == "quarantine_asset":
                return self._quarantine_asset(arguments.get("file_path", ""))
            elif tool_name == "undo_last_organize":
                return self._undo_last_organize()
            return f"Unknown tool: {tool_name}"
        except Exception as e:
            logger.exception(f"assets skill error in {tool_name}: {e}")
            return f"Asset tool '{tool_name}' failed: {e}"

    # ── Helpers ──────────────────────────────────────────────────

    @staticmethod
    def _normalize(p: str | Path) -> str:
        """Resolve to absolute, normalized path string for stable DB keys."""
        return str(Path(p).resolve())

    @staticmethod
    def _hash_file(path: Path) -> str | None:
        """SHA-256 the file in chunks. Returns hex digest or None on failure."""
        try:
            h = hashlib.sha256()
            with path.open("rb") as f:
                for chunk in iter(lambda: f.read(1024 * 1024), b""):
                    h.update(chunk)
            return h.hexdigest()
        except Exception as e:
            logger.warning(f"Hash failed for {path}: {e}")
            return None

    # ── Tool implementations ─────────────────────────────────────

    def _inventory_scan(self, folder_path: str, include_hidden: bool) -> str:
        if not folder_path:
            return "Need a folder_path. Pass the absolute path of the folder to scan."
        root = Path(folder_path)
        if not root.exists():
            return f"Folder not found: {folder_path}"
        if not root.is_dir():
            return f"Not a folder: {folder_path}"
        root = root.resolve()

        scanned = 0
        new_files = 0
        updated_files = 0
        skipped_hash = 0
        errors = 0
        now = datetime.now().isoformat(timespec="seconds")

        with sqlite3.connect(self.db_path) as conn:
            for path in root.rglob("*"):
                # Skip junk dirs (check any parent)
                if any(part in DEFAULT_SKIP_DIRS for part in path.parts):
                    continue
                # Skip hidden unless asked
                if not include_hidden and any(
                    part.startswith(".") and part not in (".", "..")
                    for part in path.relative_to(root).parts
                ):
                    continue
                if not path.is_file():
                    continue

                try:
                    stat = path.stat()
                except OSError as e:
                    logger.warning(f"Stat failed for {path}: {e}")
                    errors += 1
                    continue

                size = stat.st_size
                mtime = stat.st_mtime
                norm_path = str(path)

                # Check if we already have this file unchanged.
                row = conn.execute(
                    "SELECT size, mtime FROM files WHERE path = ?",
                    (norm_path,),
                ).fetchone()

                if row and row[0] == size and abs(row[1] - mtime) < 1.0:
                    # Unchanged — just bump last_scanned and move on.
                    conn.execute(
                        "UPDATE files SET last_scanned = ? WHERE path = ?",
                        (now, norm_path),
                    )
                    scanned += 1
                    continue

                # New or changed — hash and upsert.
                file_hash: str | None = None
                if size <= HASH_SIZE_LIMIT:
                    file_hash = self._hash_file(path)
                else:
                    skipped_hash += 1

                file_type = _classify(path.suffix)

                if row:
                    conn.execute(
                        """UPDATE files
                           SET hash = ?, size = ?, mtime = ?, type = ?, last_scanned = ?
                           WHERE path = ?""",
                        (file_hash, size, mtime, file_type, now, norm_path),
                    )
                    updated_files += 1
                else:
                    conn.execute(
                        """INSERT INTO files (path, hash, size, mtime, type, last_scanned)
                           VALUES (?, ?, ?, ?, ?, ?)""",
                        (norm_path, file_hash, size, mtime, file_type, now),
                    )
                    new_files += 1
                scanned += 1

        lines = [
            f"Scan complete: {root}",
            f"  Files scanned:  {scanned:,}",
            f"  New:            {new_files:,}",
            f"  Updated:        {updated_files:,}",
        ]
        if skipped_hash:
            lines.append(f"  Skipped hash (>200MB): {skipped_hash}")
        if errors:
            lines.append(f"  Errors: {errors}")
        return "\n".join(lines)

    def _list_assets(self, folder_path: str | None, tag: str | None,
                     file_type: str | None, limit: int) -> str:
        limit = max(1, min(int(limit), 500))

        sql = "SELECT f.path, f.size, f.type FROM files f"
        joins = []
        wheres = []
        params: list = []

        if tag:
            joins.append("JOIN tags t ON t.path = f.path")
            wheres.append("t.tag = ?")
            params.append(tag.strip())
        if folder_path:
            wheres.append("f.path LIKE ?")
            params.append(f"{self._normalize(folder_path)}%")
        if file_type:
            wheres.append("f.type = ?")
            params.append(file_type.strip().lower())

        if joins:
            sql += " " + " ".join(joins)
        if wheres:
            sql += " WHERE " + " AND ".join(wheres)
        sql += " ORDER BY f.path LIMIT ?"
        params.append(limit)

        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(sql, params).fetchall()

        if not rows:
            return "No assets found matching those filters."

        lines = [f"Found {len(rows)} file(s):"]
        for path, size, ftype in rows:
            lines.append(f"  [{ftype}] {path}  ({_human_size(size)})")
        return "\n".join(lines)

    def _tag_asset(self, file_path: str, tags: str) -> str:
        if not file_path or not tags:
            return "Need both file_path and tags."
        norm = self._normalize(file_path)
        tag_list = [t.strip() for t in tags.split(",") if t.strip()]
        if not tag_list:
            return "No valid tags given."

        now = datetime.now().isoformat(timespec="seconds")
        with sqlite3.connect(self.db_path) as conn:
            # Make sure file is in the inventory; if not, add a stub entry.
            existing = conn.execute(
                "SELECT 1 FROM files WHERE path = ?", (norm,)
            ).fetchone()
            if not existing:
                p = Path(norm)
                if p.exists() and p.is_file():
                    stat = p.stat()
                    conn.execute(
                        """INSERT INTO files (path, hash, size, mtime, type, last_scanned)
                           VALUES (?, ?, ?, ?, ?, ?)""",
                        (norm, None, stat.st_size, stat.st_mtime,
                         _classify(p.suffix), now),
                    )
                else:
                    return f"File not found: {file_path}"

            added = 0
            for t in tag_list:
                try:
                    conn.execute(
                        "INSERT INTO tags (path, tag, added_at) VALUES (?, ?, ?)",
                        (norm, t, now),
                    )
                    added += 1
                except sqlite3.IntegrityError:
                    pass  # already tagged
        return f"Tagged {Path(norm).name}: +{added} ({', '.join(tag_list)})"

    def _untag_asset(self, file_path: str, tags: str) -> str:
        if not file_path or not tags:
            return "Need both file_path and tags."
        norm = self._normalize(file_path)
        tag_list = [t.strip() for t in tags.split(",") if t.strip()]
        if not tag_list:
            return "No valid tags given."

        with sqlite3.connect(self.db_path) as conn:
            removed = 0
            for t in tag_list:
                cur = conn.execute(
                    "DELETE FROM tags WHERE path = ? AND tag = ?",
                    (norm, t),
                )
                removed += cur.rowcount
        return f"Removed {removed} tag(s) from {Path(norm).name}."

    def _find_duplicates(self, folder_path: str | None) -> str:
        sql = """
            SELECT hash, GROUP_CONCAT(path, '|'), COUNT(*), MAX(size)
            FROM files
            WHERE hash IS NOT NULL
        """
        params: list = []
        if folder_path:
            sql += " AND path LIKE ?"
            params.append(f"{self._normalize(folder_path)}%")
        sql += " GROUP BY hash HAVING COUNT(*) > 1 ORDER BY MAX(size) DESC"

        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(sql, params).fetchall()

        if not rows:
            return "No duplicate files found."

        lines = [f"Found {len(rows)} group(s) of duplicate files:\n"]
        total_wasted = 0
        for hash_val, paths_concat, count, size in rows:
            paths = paths_concat.split("|")
            wasted = size * (count - 1)
            total_wasted += wasted
            lines.append(f"  Hash {hash_val[:12]}...  {count} copies × {_human_size(size)}  (wasted: {_human_size(wasted)})")
            for p in paths:
                lines.append(f"    {p}")
            lines.append("")
        lines.append(f"Total space wasted on duplicates: {_human_size(total_wasted)}")
        return "\n".join(lines).rstrip()

    def _inventory_report(self, folder_path: str | None) -> str:
        with sqlite3.connect(self.db_path) as conn:
            scope_clause = ""
            params: list = []
            if folder_path:
                scope_clause = "WHERE path LIKE ?"
                params = [f"{self._normalize(folder_path)}%"]

            total = conn.execute(
                f"SELECT COUNT(*), COALESCE(SUM(size), 0) FROM files {scope_clause}",
                params,
            ).fetchone()
            total_count, total_size = total

            if total_count == 0:
                return (
                    "Inventory is empty"
                    + (f" for {folder_path}" if folder_path else "")
                    + ". Run inventory_scan first."
                )

            by_type = conn.execute(
                f"""SELECT type, COUNT(*), COALESCE(SUM(size), 0)
                    FROM files {scope_clause}
                    GROUP BY type
                    ORDER BY SUM(size) DESC""",
                params,
            ).fetchall()

            biggest = conn.execute(
                f"""SELECT path, size FROM files {scope_clause}
                    ORDER BY size DESC LIMIT 5""",
                params,
            ).fetchall()

            if folder_path:
                tag_count = conn.execute(
                    "SELECT COUNT(DISTINCT path) FROM tags WHERE path LIKE ?",
                    [f"{self._normalize(folder_path)}%"],
                ).fetchone()[0]
            else:
                tag_count = conn.execute(
                    "SELECT COUNT(DISTINCT path) FROM tags"
                ).fetchone()[0]

        lines = [
            f"Inventory report" + (f" — {folder_path}" if folder_path else ""),
            f"  Total files: {total_count:,}",
            f"  Total size:  {_human_size(total_size)}",
            f"  Files tagged: {tag_count:,} / {total_count:,}",
            "",
            "By type:",
        ]
        for type_name, count, size in by_type:
            lines.append(f"  {type_name:10s} {count:>6,}  {_human_size(size):>10s}")
        lines.append("")
        lines.append("Biggest files:")
        for path, size in biggest:
            lines.append(f"  {_human_size(size):>10s}  {path}")
        return "\n".join(lines)

    def _asset_details(self, file_path: str) -> str:
        if not file_path:
            return "Need a file_path."
        norm = self._normalize(file_path)
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                """SELECT hash, size, mtime, type, last_scanned
                   FROM files WHERE path = ?""",
                (norm,),
            ).fetchone()
            if not row:
                return f"Not in inventory: {file_path}. Run inventory_scan on its folder."
            tags = [
                r[0] for r in conn.execute(
                    "SELECT tag FROM tags WHERE path = ? ORDER BY tag", (norm,)
                ).fetchall()
            ]
        file_hash, size, mtime, ftype, last_scanned = row
        modified = datetime.fromtimestamp(mtime).isoformat(timespec="seconds")
        lines = [
            f"Path: {norm}",
            f"  Type:         {ftype}",
            f"  Size:         {_human_size(size)} ({size:,} bytes)",
            f"  Modified:     {modified}",
            f"  Last scanned: {last_scanned}",
            f"  SHA-256:      {file_hash if file_hash else '(not hashed — too large)'}",
            f"  Tags:         {', '.join(tags) if tags else '(none)'}",
        ]
        return "\n".join(lines)

    def _list_tags(self) -> str:
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                """SELECT tag, COUNT(*) AS n FROM tags
                   GROUP BY tag ORDER BY n DESC, tag ASC"""
            ).fetchall()
        if not rows:
            return "No tags yet. Use tag_asset to start labeling files."
        lines = [f"{len(rows)} tag(s) in use:"]
        for tag, n in rows:
            lines.append(f"  {tag:30s}  {n:>5,} file(s)")
        return "\n".join(lines)

    # ── OCR text (read what's INSIDE a screenshot) ───────────────

    def _read_asset_text(self, file_path: str) -> str:
        if not file_path:
            return "Need a file_path."
        ocr = self._load_ocr()
        if not ocr:
            return ("No OCR index available — I can't read text inside images "
                    "right now (the screenshot OCR index wasn't found).")
        candidates = ocr.get(self._ocr_base(file_path), [])
        if not candidates:
            return (f"No OCR text on file for {Path(file_path).name}. It may not "
                    f"be one of the indexed screenshots.")
        # If the filename is unique (the normal case) take it; if several files
        # share the name, prefer one whose original path matches, else the first.
        rec = candidates[0]
        if len(candidates) > 1:
            fp = str(file_path).casefold()
            rec = next((c for c in candidates if c[0].casefold() == fp), candidates[0])
        text = (rec[1] or "").strip()
        if not text:
            return f"{Path(file_path).name}: (OCR found no readable text in this image.)"
        note = ""
        if len(candidates) > 1:
            note = f"\n\n(Note: {len(candidates)} indexed files share this filename; showing one.)"
        return f"=== text inside {Path(file_path).name} ===\n\n{text}{note}"

    def _search_asset_text(self, query: str, tag: str | None, limit) -> str:
        query = (query or "").strip()
        if not query:
            return "Need a query."
        limit = max(1, min(int(limit or 20), 100))
        ocr = self._load_ocr()
        if not ocr:
            return "No OCR index available — can't search inside images right now."

        # Optional tag filter: restrict to files carrying `tag` (matched by
        # basename, since the tag store's paths and the OCR index's paths can
        # differ after a sort/rename).
        allowed = None
        if tag:
            with sqlite3.connect(self.db_path) as conn:
                allowed = {
                    self._ocr_base(r[0])
                    for r in conn.execute(
                        "SELECT path FROM tags WHERE tag = ?", (tag.strip(),)
                    ).fetchall()
                }
            if not allowed:
                return f"No files carry the tag '{tag}', so nothing to search."

        q = query.casefold()
        hits = []
        for base, recs in ocr.items():
            if allowed is not None and base not in allowed:
                continue
            for orig, text in recs:
                low = (text or "").casefold()
                idx = low.find(q)
                if idx == -1:
                    continue
                start = max(0, idx - 60)
                end = min(len(text), idx + len(query) + 60)
                snippet = text[start:end].replace("\n", " ").strip()
                hits.append((orig, snippet))
                break  # one hit per file is enough
            if len(hits) >= limit:
                break

        if not hits:
            scope = f" among '{tag}'-tagged files" if tag else ""
            return f"No screenshots' text matched '{query}'{scope}."

        head = f"{len(hits)} screenshot(s) whose text matches '{query}'"
        head += f" (within '{tag}')" if tag else ""
        head += ":"
        lines = [head]
        for orig, snippet in hits:
            lines.append("")
            lines.append(f"  {orig}")
            lines.append(f"    “…{snippet}…”")
        lines.append("")
        lines.append("(Use read_asset_text on a path for the full text, then tag_asset/move_asset to re-sort.)")
        return "\n".join(lines)

    # ── Mover (guarded, reversible) ──────────────────────────────

    def _within_roots(self, path) -> bool:
        """True only if path resolves to somewhere under an allowed root."""
        if not self.organize_roots:
            return False
        try:
            rp = Path(path).resolve()
        except Exception:
            return False
        for root in self.organize_roots:
            try:
                rp.relative_to(root)
                return True
            except ValueError:
                continue
        return False

    def _reattach_tags(self, conn, old_norm: str, new_norm: str) -> None:
        """Move the file row + its tags from old path to new so the tags stay
        attached to the file after it's physically moved."""
        conn.execute("UPDATE OR REPLACE files SET path=? WHERE path=?", (new_norm, old_norm))
        tags = [r[0] for r in conn.execute(
            "SELECT tag FROM tags WHERE path=?", (old_norm,)).fetchall()]
        conn.execute("DELETE FROM tags WHERE path=?", (old_norm,))
        now = datetime.now().isoformat(timespec="seconds")
        for t in tags:
            conn.execute(
                "INSERT OR IGNORE INTO tags(path, tag, added_at) VALUES(?,?,?)",
                (new_norm, t, now),
            )

    def _safe_dest(self, dst_dir: Path, name: str) -> Path:
        """Pick a non-clobbering destination path inside dst_dir."""
        dst = dst_dir / name
        if dst.exists():
            stem, suf = dst.stem, dst.suffix
            i = 1
            while (dst_dir / f"{stem}__{i}{suf}").exists():
                i += 1
            dst = dst_dir / f"{stem}__{i}{suf}"
        return dst

    def _do_move(self, conn, src: Path, dst_dir: Path, batch_id: str) -> tuple[bool, str]:
        # Both source and destination must be inside the allow-listed roots.
        if not self._within_roots(src):
            return False, f"refused (outside allowed roots): {src}"
        if not self._within_roots(dst_dir):
            return False, f"refused (dest outside allowed roots): {dst_dir}"
        if not src.exists():
            return False, f"missing: {src.name}"
        old_norm = str(src.resolve())
        dst_dir.mkdir(parents=True, exist_ok=True)
        if str((dst_dir / src.name).resolve()) == old_norm:
            return False, f"already in {dst_dir.name}: {src.name}"
        dst = self._safe_dest(dst_dir, src.name)
        shutil.move(str(src), str(dst))
        new_norm = str(dst.resolve())
        self._reattach_tags(conn, old_norm, new_norm)
        conn.execute(
            "INSERT INTO moves(batch_id, src, dst, ts, undone) VALUES(?,?,?,?,0)",
            (batch_id, old_norm, new_norm, datetime.now().isoformat(timespec="seconds")),
        )
        return True, f"{src.name} -> {dst_dir.name}"

    def _move_assets_by_tag(self, tag: str, dest_folder: str) -> str:
        tag = (tag or "").strip()
        if not tag:
            return "Need a tag."
        if not dest_folder:
            return "Need a dest_folder."
        if not self.organize_roots:
            return ("Moving is disabled for me — no organize_roots are configured. "
                    "I can catalog and tag, but the user has to allow-list a folder in "
                    "config before I can physically move anything.")
        dst_dir = Path(dest_folder)
        if not self._within_roots(dst_dir):
            return (f"Refused: {dest_folder} is outside my allowed roots:\n  "
                    + "\n  ".join(self.organize_roots))
        with sqlite3.connect(self.db_path) as conn:
            paths = [r[0] for r in conn.execute(
                "SELECT path FROM tags WHERE tag=?", (tag,)).fetchall()]
            if not paths:
                return f"No files tagged '{tag}'."
            batch_id = f"{tag}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
            moved = skipped = 0
            samples = []
            for p in paths:
                ok, msg = self._do_move(conn, Path(p), dst_dir, batch_id)
                if ok:
                    moved += 1
                else:
                    skipped += 1
                if len(samples) < 6:
                    samples.append(("  ok  " if ok else "  --  ") + msg)
            conn.commit()
        out = [f"Moved {moved} '{tag}' file(s) into {dst_dir} (batch {batch_id})."]
        if skipped:
            out.append(f"{skipped} skipped or refused.")
        out += samples
        out.append("Reversible: undo_last_organize() puts them all back.")
        return "\n".join(out)

    def _move_asset(self, file_path: str, dest_folder: str) -> str:
        if not file_path or not dest_folder:
            return "Need both file_path and dest_folder."
        if not self.organize_roots:
            return "Moving is disabled — no organize_roots configured."
        batch_id = f"single-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        with sqlite3.connect(self.db_path) as conn:
            ok, msg = self._do_move(conn, Path(file_path), Path(dest_folder), batch_id)
            conn.commit()
        return ("Moved: " + msg + "\nReversible with undo_last_organize.") if ok else ("Refused: " + msg)

    def _quarantine_asset(self, file_path: str) -> str:
        if not file_path:
            return "Need a file_path."
        if not self.organize_roots:
            return "Disabled — no organize_roots configured."
        src = Path(file_path)
        if not self._within_roots(src):
            return f"Refused: {file_path} is outside my allowed roots."
        if not src.exists():
            return f"Missing: {file_path}"
        self.trash_dir.mkdir(parents=True, exist_ok=True)
        old_norm = str(src.resolve())
        dst = self._safe_dest(self.trash_dir, src.name)
        shutil.move(str(src), str(dst))
        new_norm = str(dst.resolve())
        batch_id = f"quarantine-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        with sqlite3.connect(self.db_path) as conn:
            self._reattach_tags(conn, old_norm, new_norm)
            conn.execute(
                "INSERT INTO moves(batch_id, src, dst, ts, undone) VALUES(?,?,?,?,0)",
                (batch_id, old_norm, new_norm, datetime.now().isoformat(timespec="seconds")),
            )
            conn.commit()
        return (f"Quarantined {src.name} -> {self.trash_dir} (batch {batch_id}). "
                "Nothing deleted; undo_last_organize() restores it.")

    def _undo_last_organize(self) -> str:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT batch_id FROM moves WHERE undone=0 ORDER BY ts DESC LIMIT 1"
            ).fetchone()
            if not row:
                return "Nothing to undo."
            batch_id = row[0]
            rows = conn.execute(
                "SELECT src, dst FROM moves WHERE batch_id=? AND undone=0", (batch_id,)
            ).fetchall()
            restored = failed = 0
            for src, dst in rows:
                try:
                    d = Path(dst)
                    s = Path(src)
                    if not d.exists():
                        failed += 1
                        continue
                    s.parent.mkdir(parents=True, exist_ok=True)
                    final = self._safe_dest(s.parent, s.name)
                    shutil.move(str(d), str(final))
                    self._reattach_tags(conn, dst, str(final.resolve()))
                    restored += 1
                except Exception as e:
                    logger.warning(f"undo failed for {dst}: {e}")
                    failed += 1
            conn.execute("UPDATE moves SET undone=1 WHERE batch_id=?", (batch_id,))
            conn.commit()
        msg = f"Undid batch {batch_id}: restored {restored} file(s)."
        if failed:
            msg += f" {failed} couldn't be restored (already moved/missing)."
        return msg

    def _organize_status(self) -> str:
        lines = ["Organize permissions:"]
        if self.organize_roots:
            lines.append("  I may move/rename files ONLY under:")
            for r in self.organize_roots:
                lines.append(f"    {r}")
        else:
            lines.append("  Moving is DISABLED (no organize_roots configured).")
        lines.append(f"  Soft-delete (trash) goes to: {self.trash_dir}")
        with sqlite3.connect(self.db_path) as conn:
            last = conn.execute(
                "SELECT batch_id, COUNT(*), MAX(ts) FROM moves WHERE undone=0 "
                "GROUP BY batch_id ORDER BY MAX(ts) DESC LIMIT 1"
            ).fetchone()
        if last and last[0]:
            lines.append(f"  Last undoable batch: {last[0]} — {last[1]} file(s) at {last[2]}.")
        else:
            lines.append("  No move batches yet.")
        return "\n".join(lines)
