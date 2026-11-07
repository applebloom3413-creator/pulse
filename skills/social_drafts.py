"""
Social drafts skill — shared draft queue for the user's social media.

Author companions draft a capped number of posts per ISO week. A reviewer
companion batch-reviews everyone's drafts. The user copies the approved
drafts and posts them themselves on whichever platforms are configured.

There is intentionally NO send_post tool. Human-in-the-loop is the spine,
same as the email skill.

Storage:
    Shared SQLite db at data/social_drafts.db (NOT persona-scoped — the
    whole point is cross-persona reads).

Config:
    skills:
      social_drafts:
        enabled: true
        author_name: "Ada"        # display name for this persona
        role: "author"            # "author" | "reviewer"
        path: "data/social_drafts.db"   # optional override
        # Optional (generic defaults shown):
        weekly_cap: 2             # drafts per author per ISO week
        reviewer_name: "the reviewer"   # how authors refer to the reviewer
        platforms:                # platform -> soft character limit
          bluesky: 300
          threads: 500
          instagram: 2200
        image_platforms: ["instagram"]  # drafts here must include image_idea

Roles:
    author    → draft_post, list_my_drafts, revise_draft, withdraw_draft,
                read_draft, list_drafts
    reviewer  → list_pending_reviews, review_draft,
                read_draft, list_drafts, mark_posted

    read_draft is shared — authors can read each other's drafts and see
    the reviewer's notes. The team isn't writing in private.

Context injection (optional): when listed in config.context.inject_skills,
authors see "[Social drafts: you've written X of N this week]" and the
reviewer sees "[Social drafts: N pending your review]".
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime
from pathlib import Path

from skills.base import BaseSkill

logger = logging.getLogger(__name__)

# Per-author posts allowed per ISO week. Forces selectivity.
# Overridable via skills.social_drafts.weekly_cap.
DEFAULT_WEEKLY_CAP = 2

# Platform character limits. Soft-warn on overflow rather than block —
# the user can trim before pasting. Overridable via
# skills.social_drafts.platforms (name -> limit).
DEFAULT_PLATFORM_LIMITS = {
    "bluesky": 300,
    "threads": 500,
    "instagram": 2200,
}

# Platforms where a draft must come with an image idea (the user makes the
# image, the companion writes the caption). Overridable via
# skills.social_drafts.image_platforms.
DEFAULT_IMAGE_PLATFORMS = ["instagram"]
VALID_STATUSES = {"pending_review", "approved", "revise", "rejected", "posted"}
VALID_VERDICTS = {"approved", "revise", "rejected"}
VALID_ROLES = {"author", "reviewer"}


def _iso_week(now: datetime | None = None) -> str:
    """ISO week string like '2026-W20' (Monday-Sunday weeks)."""
    return (now or datetime.now()).strftime("%G-W%V")


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class SocialDraftsSkill(BaseSkill):
    name = "social_drafts"

    def __init__(self, config: dict):
        super().__init__(config)

        skill_cfg = (config.get("skills") or {}).get("social_drafts") or {}

        author_name = skill_cfg.get("author_name") or ""
        role = (skill_cfg.get("role") or "").lower()
        if not author_name:
            logger.warning(
                "social_drafts: no author_name in skills.social_drafts config; "
                "tools that write will refuse until set."
            )
        if role and role not in VALID_ROLES:
            logger.warning(
                f"social_drafts: invalid role {role!r}; expected one of {VALID_ROLES}. "
                f"Falling back to 'author'."
            )
            role = "author"
        if not role:
            role = "author"

        self.author_name = author_name
        self.role = role

        self.user_name = (
            config.get("user_nickname") or config.get("user_name") or "the user"
        )
        self.reviewer_name = skill_cfg.get("reviewer_name") or "the reviewer"
        try:
            self.weekly_cap = max(1, int(skill_cfg.get("weekly_cap") or DEFAULT_WEEKLY_CAP))
        except (TypeError, ValueError):
            self.weekly_cap = DEFAULT_WEEKLY_CAP
        limits = skill_cfg.get("platforms")
        if isinstance(limits, dict) and limits:
            self.platform_limits = {
                str(k).lower(): int(v) for k, v in limits.items()
            }
        else:
            self.platform_limits = dict(DEFAULT_PLATFORM_LIMITS)
        self.valid_platforms = set(self.platform_limits)
        img = skill_cfg.get("image_platforms")
        if img is None:
            img = DEFAULT_IMAGE_PLATFORMS
        self.image_platforms = {str(p).lower() for p in (img or [])}

        db_path = skill_cfg.get("path") or "data/social_drafts.db"
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        self._init_db()

    # ── DB setup ─────────────────────────────────────────────────

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._conn() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS drafts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    author TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    content TEXT NOT NULL,
                    image_idea TEXT,
                    status TEXT NOT NULL,
                    week_iso TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    reviewed_at TEXT,
                    reviewer TEXT,
                    reviewer_notes TEXT,
                    posted_at TEXT
                );
                CREATE INDEX IF NOT EXISTS drafts_status ON drafts(status);
                CREATE INDEX IF NOT EXISTS drafts_author_week
                    ON drafts(author, week_iso);
                """
            )

    # ── Tool definitions ─────────────────────────────────────────

    def get_tools(self) -> list[dict]:
        if self.role == "reviewer":
            return self._reviewer_tools() + self._shared_tools()
        return self._author_tools() + self._shared_tools(include_mark_posted=False)

    def _author_tools(self) -> list[dict]:
        platform_list = ", ".join(sorted(self.valid_platforms))
        limits_text = ", ".join(
            f"{p} {n}" for p, n in sorted(self.platform_limits.items())
        )
        image_list = ", ".join(sorted(self.image_platforms))
        image_rule = (
            f"Drafts for {image_list} must include image_idea "
            f"({self.user_name} makes the image, you write the caption). "
            if self.image_platforms else ""
        )
        return [
            {
                "type": "function",
                "function": {
                    "name": "draft_post",
                    "description": (
                        "Draft a social media post. You get "
                        f"{self.weekly_cap} drafts per ISO week — make each one count. "
                        f"{image_rule}Platforms: "
                        f"{platform_list}. Soft char limits: {limits_text}."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "platform": {
                                "type": "string",
                                "enum": sorted(self.valid_platforms),
                                "description": "Where this post is meant for.",
                            },
                            "content": {
                                "type": "string",
                                "description": (
                                    "Post body. For image platforms, this is "
                                    "the caption."
                                ),
                            },
                            "image_idea": {
                                "type": "string",
                                "description": (
                                    f"Required for: {image_list or 'none'}. A short "
                                    "description of what the visual should be — "
                                    f"{self.user_name} makes the image."
                                ),
                            },
                        },
                        "required": ["platform", "content"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "list_my_drafts",
                    "description": (
                        "List your own drafts across recent weeks. Optional "
                        "status filter."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "status": {
                                "type": "string",
                                "enum": sorted(VALID_STATUSES),
                                "description": "Only show drafts in this status.",
                            },
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "revise_draft",
                    "description": (
                        f"Edit one of your own drafts that {self.reviewer_name} marked 'revise'. "
                        "Replaces content (and image_idea if provided) and "
                        "moves the draft back to pending_review."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "id": {
                                "type": "integer",
                                "description": "Draft id from list_my_drafts.",
                            },
                            "content": {
                                "type": "string",
                                "description": "New post body.",
                            },
                            "image_idea": {
                                "type": "string",
                                "description": (
                                    "New image idea (image platforms). Omit to keep "
                                    "the existing one."
                                ),
                            },
                        },
                        "required": ["id", "content"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "withdraw_draft",
                    "description": (
                        f"Pull one of your own drafts before {self.reviewer_name} reviews it. "
                        "Sets status to rejected. NOTE: withdrawing does NOT "
                        "free up a slot in your weekly cap — selectivity is "
                        "the feature."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "id": {
                                "type": "integer",
                                "description": "Draft id from list_my_drafts.",
                            },
                        },
                        "required": ["id"],
                    },
                },
            },
        ]

    def _reviewer_tools(self) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "list_pending_reviews",
                    "description": (
                        "List all drafts across all authors waiting for your "
                        "review (status = pending_review). Use this to see the "
                        "week's queue before reading individual drafts."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {},
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "review_draft",
                    "description": (
                        f"Set a verdict on a draft. 'approved' → goes to {self.user_name}. "
                        "'revise' → author can edit and resubmit; notes "
                        "encouraged. 'rejected' → archived, no resubmit."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "id": {
                                "type": "integer",
                                "description": "Draft id.",
                            },
                            "verdict": {
                                "type": "string",
                                "enum": sorted(VALID_VERDICTS),
                                "description": "Your call on this draft.",
                            },
                            "notes": {
                                "type": "string",
                                "description": (
                                    "Optional but encouraged, especially for "
                                    "'revise' — tell the author what to change."
                                ),
                            },
                        },
                        "required": ["id", "verdict"],
                    },
                },
            },
        ]

    def _shared_tools(self, include_mark_posted: bool = True) -> list[dict]:
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "read_draft",
                    "description": (
                        "Read the full content of any draft by id — author, "
                        "platform, content, image_idea, status, and the "
                        "reviewer's notes if any. Authors can read each other's "
                        "drafts (visibility is intentional — you're a team)."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "id": {
                                "type": "integer",
                                "description": "Draft id.",
                            },
                        },
                        "required": ["id"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "list_drafts",
                    "description": (
                        "General query across all drafts. Filter by status, "
                        "platform, and/or author. Use this for cross-cutting "
                        "views (e.g. 'all approved drafts for one platform this week')."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "status": {
                                "type": "string",
                                "enum": sorted(VALID_STATUSES),
                            },
                            "platform": {
                                "type": "string",
                                "enum": sorted(self.valid_platforms),
                            },
                            "author": {
                                "type": "string",
                                "description": "Filter by author display name.",
                            },
                            "limit": {
                                "type": "integer",
                                "description": "Max rows returned (default 10).",
                                "default": 10,
                            },
                        },
                    },
                },
            },
        ]
        if include_mark_posted:
            tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": "mark_posted",
                        "description": (
                            f"Mark an approved draft as posted, after {self.user_name} has "
                            "copy-pasted it to the platform. Only call when "
                            f"{self.user_name} has confirmed they actually posted."
                        ),
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "id": {
                                    "type": "integer",
                                    "description": "Draft id.",
                                },
                            },
                            "required": ["id"],
                        },
                    },
                }
            )
        return tools

    # ── Context injection ────────────────────────────────────────

    def get_context(self) -> str:
        try:
            if self.role == "reviewer":
                with self._conn() as conn:
                    row = conn.execute(
                        "SELECT COUNT(*) AS n FROM drafts "
                        "WHERE status = 'pending_review'"
                    ).fetchone()
                n = row["n"] if row else 0
                if n == 0:
                    return ""
                noun = "draft" if n == 1 else "drafts"
                return f"[Social drafts: {n} {noun} pending your review]"

            # author
            if not self.author_name:
                return ""
            with self._conn() as conn:
                row = conn.execute(
                    "SELECT COUNT(*) AS n FROM drafts "
                    "WHERE author = ? AND week_iso = ?",
                    (self.author_name, _iso_week()),
                ).fetchone()
            n = row["n"] if row else 0
            if n == 0:
                return ""
            return f"[Social drafts: you've written {n} of {self.weekly_cap} this week]"
        except Exception as e:
            logger.warning(f"social_drafts get_context failed: {e}")
            return ""

    # ── Execution router ─────────────────────────────────────────

    def execute(self, tool_name: str, arguments: dict) -> str:
        try:
            if tool_name == "draft_post":
                return self._draft_post(
                    platform=arguments.get("platform", ""),
                    content=arguments.get("content", ""),
                    image_idea=arguments.get("image_idea"),
                )
            if tool_name == "list_my_drafts":
                return self._list_my_drafts(status=arguments.get("status"))
            if tool_name == "revise_draft":
                return self._revise_draft(
                    draft_id=arguments.get("id"),
                    content=arguments.get("content", ""),
                    image_idea=arguments.get("image_idea"),
                )
            if tool_name == "withdraw_draft":
                return self._withdraw_draft(draft_id=arguments.get("id"))
            if tool_name == "list_pending_reviews":
                return self._list_pending_reviews()
            if tool_name == "read_draft":
                return self._read_draft(draft_id=arguments.get("id"))
            if tool_name == "review_draft":
                return self._review_draft(
                    draft_id=arguments.get("id"),
                    verdict=arguments.get("verdict", ""),
                    notes=arguments.get("notes", ""),
                )
            if tool_name == "list_drafts":
                return self._list_drafts(
                    status=arguments.get("status"),
                    platform=arguments.get("platform"),
                    author=arguments.get("author"),
                    limit=arguments.get("limit", 10),
                )
            if tool_name == "mark_posted":
                return self._mark_posted(draft_id=arguments.get("id"))
            return f"Unknown tool: {tool_name}"
        except Exception as e:
            logger.exception(f"social_drafts error in {tool_name}: {e}")
            return f"social_drafts tool '{tool_name}' failed: {e}"

    # ── Author tool impls ────────────────────────────────────────

    def _draft_post(
        self,
        platform: str,
        content: str,
        image_idea: str | None,
    ) -> str:
        if self.role != "author":
            return (
                f"draft_post is for authors. This persona is configured as "
                f"role={self.role!r}."
            )
        if not self.author_name:
            return (
                "No author_name configured for this persona — set "
                "skills.social_drafts.author_name in the persona config."
            )

        platform = (platform or "").lower().strip()
        if platform not in self.valid_platforms:
            return (
                f"Unknown platform {platform!r}. Use one of: "
                f"{', '.join(sorted(self.valid_platforms))}."
            )
        if not content.strip():
            return "Draft is empty — write something."
        if platform in self.image_platforms and not (image_idea or "").strip():
            return (
                f"{platform} drafts must include image_idea ({self.user_name} "
                "makes the image, you write the caption)."
            )

        week = _iso_week()
        with self._conn() as conn:
            count = conn.execute(
                "SELECT COUNT(*) AS n FROM drafts "
                "WHERE author = ? AND week_iso = ?",
                (self.author_name, week),
            ).fetchone()["n"]
            if count >= self.weekly_cap:
                return (
                    f"You're at this week's cap ({count}/{self.weekly_cap} for "
                    f"{week}). New drafts open Monday."
                )

            limit = self.platform_limits[platform]
            warning = ""
            if len(content) > limit:
                warning = (
                    f"\n\n⚠ Heads up: content is {len(content)} chars but "
                    f"{platform} caps at {limit}. {self.user_name} will need to trim before "
                    f"posting."
                )

            cur = conn.execute(
                """
                INSERT INTO drafts
                    (author, platform, content, image_idea, status,
                     week_iso, created_at)
                VALUES (?, ?, ?, ?, 'pending_review', ?, ?)
                """,
                (
                    self.author_name,
                    platform,
                    content,
                    (image_idea or None),
                    week,
                    _now(),
                ),
            )
            new_id = cur.lastrowid
            new_count = count + 1

        logger.info(
            f"Drafted post {new_id} for {self.author_name} ({platform}, "
            f"{new_count}/{self.weekly_cap} this week)"
        )
        return (
            f"Drafted post #{new_id} for {platform}. "
            f"You're at {new_count}/{self.weekly_cap} for {week}. "
            f"{self.reviewer_name[:1].upper() + self.reviewer_name[1:]} will see it "
            f"in the next review pass.{warning}"
        )

    def _list_my_drafts(self, status: str | None) -> str:
        if not self.author_name:
            return "No author_name configured — can't list."
        sql = (
            "SELECT id, platform, status, week_iso, created_at, "
            "       reviewer, reviewer_notes, "
            "       SUBSTR(content, 1, 120) AS preview "
            "FROM drafts WHERE author = ?"
        )
        params: list = [self.author_name]
        if status:
            if status not in VALID_STATUSES:
                return (
                    f"Unknown status {status!r}. Valid: "
                    f"{', '.join(sorted(VALID_STATUSES))}."
                )
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY id DESC LIMIT 50"

        with self._conn() as conn:
            rows = conn.execute(sql, params).fetchall()
        if not rows:
            return (
                f"No drafts yet"
                + (f" with status={status}" if status else "")
                + "."
            )
        lines = [f"Your drafts ({len(rows)}):"]
        for r in rows:
            lines.append(
                f"  #{r['id']} [{r['status']}] {r['platform']} "
                f"({r['week_iso']}, {r['created_at']})"
            )
            lines.append(f"      {r['preview']}")
            if r["reviewer_notes"]:
                lines.append(
                    f"      notes from {r['reviewer'] or 'reviewer'}: "
                    f"{r['reviewer_notes']}"
                )
        return "\n".join(lines)

    def _revise_draft(
        self,
        draft_id: int | None,
        content: str,
        image_idea: str | None,
    ) -> str:
        if self.role != "author":
            return (
                f"revise_draft is for authors. This persona is role={self.role!r}."
            )
        if draft_id is None:
            return "Need a draft id."
        if not content.strip():
            return "Revision content is empty."

        with self._conn() as conn:
            row = conn.execute(
                "SELECT author, status, platform, image_idea FROM drafts WHERE id = ?",
                (draft_id,),
            ).fetchone()
            if not row:
                return f"No draft #{draft_id}."
            if row["author"] != self.author_name:
                return (
                    f"Draft #{draft_id} belongs to {row['author']}, "
                    f"not you ({self.author_name})."
                )
            if row["status"] != "revise":
                return (
                    f"Draft #{draft_id} is {row['status']!r}, not 'revise'. "
                    f"Only drafts {self.reviewer_name} marked 'revise' can be edited."
                )

            new_image = (
                image_idea if image_idea is not None else row["image_idea"]
            )
            if row["platform"] in self.image_platforms and not (new_image or "").strip():
                return (
                    f"{row['platform']} drafts must keep an image_idea — pass one or "
                    "leave the field unset to retain the existing one."
                )

            conn.execute(
                """
                UPDATE drafts
                SET content = ?, image_idea = ?, status = 'pending_review',
                    reviewed_at = NULL, reviewer = NULL, reviewer_notes = NULL
                WHERE id = ?
                """,
                (content, new_image, draft_id),
            )
        logger.info(f"Revised draft {draft_id} by {self.author_name}")
        return f"Draft #{draft_id} revised and resubmitted for review."

    def _withdraw_draft(self, draft_id: int | None) -> str:
        if self.role != "author":
            return (
                f"withdraw_draft is for authors. This persona is role={self.role!r}."
            )
        if draft_id is None:
            return "Need a draft id."

        with self._conn() as conn:
            row = conn.execute(
                "SELECT author, status FROM drafts WHERE id = ?",
                (draft_id,),
            ).fetchone()
            if not row:
                return f"No draft #{draft_id}."
            if row["author"] != self.author_name:
                return (
                    f"Draft #{draft_id} belongs to {row['author']}, "
                    f"not you ({self.author_name})."
                )
            if row["status"] in ("posted", "rejected"):
                return f"Draft #{draft_id} is already {row['status']}."

            conn.execute(
                "UPDATE drafts SET status = 'rejected' WHERE id = ?",
                (draft_id,),
            )
        logger.info(f"Withdrew draft {draft_id} by {self.author_name}")
        return (
            f"Draft #{draft_id} withdrawn. Reminder: this doesn't free up "
            f"a weekly-cap slot."
        )

    # ── Reviewer tool impls ──────────────────────────────────────

    def _list_pending_reviews(self) -> str:
        if self.role != "reviewer":
            return (
                f"list_pending_reviews is for reviewers. This persona is "
                f"role={self.role!r}."
            )
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT id, author, platform, week_iso, created_at,
                       SUBSTR(content, 1, 160) AS preview
                FROM drafts
                WHERE status = 'pending_review'
                ORDER BY created_at ASC
                """
            ).fetchall()
        if not rows:
            return "No drafts pending review."
        lines = [f"Pending review ({len(rows)}):"]
        for r in rows:
            lines.append(
                f"  #{r['id']} {r['author']} → {r['platform']} "
                f"({r['week_iso']}, {r['created_at']})"
            )
            lines.append(f"      {r['preview']}")
        return "\n".join(lines)

    def _read_draft(self, draft_id: int | None) -> str:
        if draft_id is None:
            return "Need a draft id."
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM drafts WHERE id = ?", (draft_id,)
            ).fetchone()
        if not row:
            return f"No draft #{draft_id}."
        parts = [
            f"Draft #{row['id']}",
            f"Author:    {row['author']}",
            f"Platform:  {row['platform']}",
            f"Week:      {row['week_iso']}",
            f"Status:    {row['status']}",
            f"Created:   {row['created_at']}",
        ]
        if row["reviewed_at"]:
            parts.append(f"Reviewed:  {row['reviewed_at']} by {row['reviewer']}")
        if row["reviewer_notes"]:
            parts.append(f"Notes:     {row['reviewer_notes']}")
        if row["posted_at"]:
            parts.append(f"Posted:    {row['posted_at']}")
        if row["image_idea"]:
            parts.append(f"Image:     {row['image_idea']}")
        parts.append("--- CONTENT ---")
        parts.append(row["content"])
        return "\n".join(parts)

    def _review_draft(
        self,
        draft_id: int | None,
        verdict: str,
        notes: str,
    ) -> str:
        if self.role != "reviewer":
            return (
                f"review_draft is for reviewers. This persona is role={self.role!r}."
            )
        if draft_id is None:
            return "Need a draft id."
        verdict = (verdict or "").lower().strip()
        if verdict not in VALID_VERDICTS:
            return (
                f"Unknown verdict {verdict!r}. Use one of: "
                f"{', '.join(sorted(VALID_VERDICTS))}."
            )

        with self._conn() as conn:
            row = conn.execute(
                "SELECT status FROM drafts WHERE id = ?", (draft_id,)
            ).fetchone()
            if not row:
                return f"No draft #{draft_id}."
            if row["status"] not in ("pending_review", "revise"):
                return (
                    f"Draft #{draft_id} is {row['status']!r} — can only "
                    f"review drafts in 'pending_review' or 'revise'."
                )

            new_status = verdict  # approved | revise | rejected
            conn.execute(
                """
                UPDATE drafts
                SET status = ?, reviewed_at = ?, reviewer = ?, reviewer_notes = ?
                WHERE id = ?
                """,
                (
                    new_status,
                    _now(),
                    self.author_name or "Reviewer",
                    (notes or None),
                    draft_id,
                ),
            )
        logger.info(
            f"Reviewed draft {draft_id}: {new_status}"
            + (f" ({notes!r})" if notes else "")
        )
        return (
            f"Draft #{draft_id} → {new_status}"
            + (f". Notes: {notes}" if notes else ".")
        )

    # ── Shared tool impls ────────────────────────────────────────

    def _list_drafts(
        self,
        status: str | None,
        platform: str | None,
        author: str | None,
        limit: int,
    ) -> str:
        try:
            limit = max(1, min(int(limit or 10), 100))
        except (TypeError, ValueError):
            limit = 10

        clauses: list[str] = []
        params: list = []
        if status:
            if status not in VALID_STATUSES:
                return (
                    f"Unknown status {status!r}. Valid: "
                    f"{', '.join(sorted(VALID_STATUSES))}."
                )
            clauses.append("status = ?")
            params.append(status)
        if platform:
            platform = platform.lower()
            if platform not in self.valid_platforms:
                return (
                    f"Unknown platform {platform!r}. Valid: "
                    f"{', '.join(sorted(self.valid_platforms))}."
                )
            clauses.append("platform = ?")
            params.append(platform)
        if author:
            clauses.append("author = ?")
            params.append(author)

        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = (
            "SELECT id, author, platform, status, week_iso, created_at, "
            "       SUBSTR(content, 1, 120) AS preview "
            f"FROM drafts{where} ORDER BY id DESC LIMIT ?"
        )
        params.append(limit)

        with self._conn() as conn:
            rows = conn.execute(sql, params).fetchall()
        if not rows:
            return "No drafts match."
        lines = [f"Drafts ({len(rows)}):"]
        for r in rows:
            lines.append(
                f"  #{r['id']} {r['author']} → {r['platform']} "
                f"[{r['status']}] ({r['week_iso']}, {r['created_at']})"
            )
            lines.append(f"      {r['preview']}")
        return "\n".join(lines)

    def _mark_posted(self, draft_id: int | None) -> str:
        if self.role != "reviewer":
            return (
                f"mark_posted is reviewer-only (the reviewer calls this on "
                f"{self.user_name}'s say-so). This persona is role={self.role!r}."
            )
        if draft_id is None:
            return "Need a draft id."
        with self._conn() as conn:
            row = conn.execute(
                "SELECT status FROM drafts WHERE id = ?", (draft_id,)
            ).fetchone()
            if not row:
                return f"No draft #{draft_id}."
            if row["status"] != "approved":
                return (
                    f"Draft #{draft_id} is {row['status']!r} — only approved "
                    f"drafts can be marked posted."
                )
            conn.execute(
                "UPDATE drafts SET status = 'posted', posted_at = ? WHERE id = ?",
                (_now(), draft_id),
            )
        logger.info(f"Marked draft {draft_id} as posted")
        return f"Draft #{draft_id} marked as posted."
