"""
Email skill — Gmail triage helper for whichever companion you give it to.

The companion can:
  - check_inbox          → list recent / unread mail
  - read_email           → read the full body of one message
  - categorize_email     → apply a Gmail label (e.g. triage/bug)
  - flag_for_user        → star + queue for surfacing in Telegram
  - archive_email        → remove from INBOX (newsletters, low-stakes stuff)
  - draft_reply          → save a draft to Gmail Drafts for human review

There is intentionally NO send_email tool. The companion drafts, the human
sends. Human-in-the-loop is the whole point.

Auth:
  Drop a Desktop-app OAuth `credentials.json` into the persona's
  data/email/ folder. First time a tool is called, the skill pops a
  browser window for one-time consent and saves a refresh token to
  token.json. After that it runs hands-free.

Context injection (optional): when listed in config.context.inject_skills,
a one-liner like "[Inbox: 3 unread]" is added to every prompt so the
companion knows when there's mail without burning a tool call.

Config (skills.email, or a top-level `email:` block; all optional):
  inbox_name: "Inbox"            # how the inbox is described to the companion
  label_prefix: "triage/"        # namespace for labels the companion applies
  categories:                    # label suffix -> when to use it
    bug: "someone reports something broken"
    feedback: "suggestions, feature requests"
    question: "how-do-I"
    spam: "junk"
    needs-review: "anything sensitive or you can't confidently classify"
  review_category: "needs-review"  # the "when in doubt" bucket; never archived
"""

from __future__ import annotations

import base64
import logging
from datetime import datetime
from email.mime.text import MIMEText
from pathlib import Path

from skills.base import BaseSkill

logger = logging.getLogger(__name__)

# Scope: read + modify labels + create drafts. Does NOT include send.
SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]

# Don't let a single email blow up the context window.
MAX_BODY_LENGTH = 10_000

# Max messages returned by check_inbox in one call.
DEFAULT_LIST_LIMIT = 20
HARD_LIST_LIMIT = 50

DEFAULT_LABEL_PREFIX = "triage/"
DEFAULT_REVIEW_CATEGORY = "needs-review"
DEFAULT_CATEGORIES = {
    "bug": "someone reports something broken",
    "feedback": "suggestions, feature requests",
    "question": "how-do-I",
    "spam": "junk",
    DEFAULT_REVIEW_CATEGORY: (
        "anything sensitive or you can't confidently classify — "
        "when in doubt, use this"
    ),
}


class EmailSkill(BaseSkill):
    name = "email"

    def __init__(self, config: dict):
        super().__init__(config)
        persona_dir = config.get("_persona_dir", "")
        if persona_dir:
            self.data_dir = Path(persona_dir) / "data" / "email"
        else:
            self.data_dir = Path("data") / "email"
        self.data_dir.mkdir(parents=True, exist_ok=True)

        self.creds_path = self.data_dir / "credentials.json"
        self.token_path = self.data_dir / "token.json"

        # Generic, config-driven labels/naming (no account specifics baked in).
        top = config.get("email", {}) or {}
        nested = (config.get("skills", {}) or {}).get("email", {}) or {}
        cfg = {**top, **nested}
        self.user_name = (
            config.get("user_nickname") or config.get("user_name") or "the user"
        )
        self.inbox_name = cfg.get("inbox_name") or "Inbox"
        prefix = cfg.get("label_prefix")
        if prefix is None:
            prefix = DEFAULT_LABEL_PREFIX
        prefix = str(prefix)
        if prefix and not prefix.endswith("/"):
            prefix += "/"
        self.label_prefix = prefix
        cats = cfg.get("categories")
        self.categories = (
            {str(k): str(v) for k, v in cats.items()}
            if isinstance(cats, dict) and cats else dict(DEFAULT_CATEGORIES)
        )
        self.review_category = cfg.get("review_category") or DEFAULT_REVIEW_CATEGORY

        # Lazy-built Gmail service. None until first tool call.
        self._service = None
        # Cache of label name → label id (built on first need).
        self._label_cache: dict[str, str] | None = None

        # Surfaced to the conversation engine after a heartbeat/response.
        # Each entry: {"id", "from", "subject", "why"}.
        self.pending_flagged: list[dict] = []

    # ── Tool definitions ─────────────────────────────────────────

    def get_tools(self) -> list[dict]:
        u = self.user_name
        p = self.label_prefix
        cat_text = ", ".join(f"{p}{k} ({v})" for k, v in self.categories.items())
        example = f"{p}{next(iter(self.categories), 'bug')}"
        review = f"{p}{self.review_category}"
        return [
            {
                "type": "function",
                "function": {
                    "name": "check_inbox",
                    "description": (
                        f"List recent emails in the {self.inbox_name}. Returns ID, "
                        "sender, subject, date, and a short snippet for each. "
                        f"Use this on heartbeat ticks or when {u} asks what's in "
                        "the inbox. Pass unread_only=True (default) to skip stuff "
                        "you've already triaged."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "unread_only": {
                                "type": "boolean",
                                "description": "Only show unread mail (default true).",
                                "default": True,
                            },
                            "max_results": {
                                "type": "integer",
                                "description": f"How many to return (default {DEFAULT_LIST_LIMIT}, max {HARD_LIST_LIMIT}).",
                                "default": DEFAULT_LIST_LIMIT,
                            },
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "read_email",
                    "description": (
                        "Read the full body of a single email by its ID. Use this "
                        "after check_inbox when the snippet isn't enough to "
                        "categorize or respond. Returns sender, subject, date, "
                        "and the plain-text body."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "id": {
                                "type": "string",
                                "description": "Gmail message ID from check_inbox.",
                            },
                        },
                        "required": ["id"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "categorize_email",
                    "description": (
                        "Apply a category label to an email. Creates the label "
                        "in Gmail if it doesn't exist. Use after reading enough "
                        "to know what bucket it belongs in. Current categories: "
                        f"{cat_text}."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "id": {
                                "type": "string",
                                "description": "Gmail message ID.",
                            },
                            "category": {
                                "type": "string",
                                "description": (
                                    f"Label to apply. Should start with '{p}' "
                                    f"(e.g. '{example}'). Will be created if it "
                                    "doesn't exist yet."
                                ),
                            },
                        },
                        "required": ["id", "category"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "flag_for_user",
                    "description": (
                        f"Star an email and queue it to be surfaced to {u} in the "
                        "next Telegram message. Use this for anything urgent, "
                        "sensitive, or that you genuinely think they need to see "
                        "personally. Don't over-flag — if you flag everything, "
                        "the flag means nothing. Quality > quantity."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "id": {
                                "type": "string",
                                "description": "Gmail message ID.",
                            },
                            "why": {
                                "type": "string",
                                "description": (
                                    "Short reason you're flagging it — what you "
                                    f"want {u} to know about this one."
                                ),
                            },
                        },
                        "required": ["id", "why"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "archive_email",
                    "description": (
                        "Remove an email from the inbox (it stays in 'All Mail'). "
                        f"Use for low-stakes stuff {u} doesn't need to see — "
                        "newsletters, automated notifications, obvious spam after "
                        "you've labeled it. Don't archive anything you flagged or "
                        f"anything in '{review}'."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "id": {
                                "type": "string",
                                "description": "Gmail message ID.",
                            },
                        },
                        "required": ["id"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "draft_reply",
                    "description": (
                        f"Save a reply draft to Gmail Drafts for {u} to review and "
                        "send themselves. The draft is threaded with the original "
                        "email. Write in your own voice but remember: this goes "
                        f"out under {u}'s account. Keep it warm, honest, brief. "
                        "If you wouldn't want them to send it, don't draft it."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "id": {
                                "type": "string",
                                "description": "Gmail message ID you're replying to.",
                            },
                            "body": {
                                "type": "string",
                                "description": "The reply text (plain text, no HTML).",
                            },
                        },
                        "required": ["id", "body"],
                    },
                },
            },
        ]

    # ── Context injection ────────────────────────────────────────

    def get_context(self) -> str:
        """One-liner about inbox state. Empty string if no creds / no mail.

        Counts actual messages (up to a cap) rather than trusting Gmail's
        resultSizeEstimate, which is notoriously inaccurate for small
        inboxes.
        """
        if not self.creds_path.exists():
            return ""
        try:
            service = self._get_service()
            if service is None:
                return ""
            # Count up to 100 — anything beyond is just "100+"
            CONTEXT_COUNT_CAP = 100
            result = service.users().messages().list(
                userId="me", q="is:unread", maxResults=CONTEXT_COUNT_CAP
            ).execute()
            messages = result.get("messages", [])
            count = len(messages)
            if count == 0:
                return ""
            suffix = "+" if result.get("nextPageToken") else ""
            noun = "email" if count == 1 and not suffix else "emails"
            return f"[{self.inbox_name}: {count}{suffix} unread {noun}]"
        except Exception as e:
            logger.warning(f"email get_context failed: {e}")
            return ""

    # ── Execution router ─────────────────────────────────────────

    def execute(self, tool_name: str, arguments: dict) -> str:
        try:
            if tool_name == "check_inbox":
                return self._check_inbox(
                    unread_only=arguments.get("unread_only", True),
                    max_results=arguments.get("max_results", DEFAULT_LIST_LIMIT),
                )
            elif tool_name == "read_email":
                return self._read_email(arguments.get("id", ""))
            elif tool_name == "categorize_email":
                return self._categorize_email(
                    arguments.get("id", ""),
                    arguments.get("category", ""),
                )
            elif tool_name == "flag_for_user":
                return self._flag_for_user(
                    arguments.get("id", ""),
                    arguments.get("why", ""),
                )
            elif tool_name == "archive_email":
                return self._archive_email(arguments.get("id", ""))
            elif tool_name == "draft_reply":
                return self._draft_reply(
                    arguments.get("id", ""),
                    arguments.get("body", ""),
                )
            return f"Unknown tool: {tool_name}"
        except Exception as e:
            logger.exception(f"email skill error in {tool_name}: {e}")
            return f"Email tool '{tool_name}' failed: {e}"

    # ── Auth + service plumbing ──────────────────────────────────

    def _get_service(self):
        """Lazy-build the Gmail API service. Returns None if creds missing."""
        if self._service is not None:
            return self._service

        if not self.creds_path.exists():
            logger.error(f"No credentials.json at {self.creds_path}")
            return None

        try:
            from google.auth.transport.requests import Request
            from google.oauth2.credentials import Credentials
            from google_auth_oauthlib.flow import InstalledAppFlow
            from googleapiclient.discovery import build
        except ImportError as e:
            logger.error(f"Google API libs not installed: {e}")
            return None

        creds = None
        if self.token_path.exists():
            try:
                creds = Credentials.from_authorized_user_file(
                    str(self.token_path), SCOPES
                )
            except Exception as e:
                logger.warning(f"Could not load token.json: {e}")

        # Refresh or run consent flow if needed.
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                try:
                    creds.refresh(Request())
                except Exception as e:
                    logger.warning(f"Token refresh failed, re-running consent: {e}")
                    creds = None
            if not creds:
                flow = InstalledAppFlow.from_client_secrets_file(
                    str(self.creds_path), SCOPES
                )
                # Opens a browser window for the user to consent.
                creds = flow.run_local_server(port=0)
            # Persist the new/refreshed token.
            self.token_path.write_text(creds.to_json(), encoding="utf-8")

        self._service = build("gmail", "v1", credentials=creds, cache_discovery=False)
        return self._service

    # ── Helpers ──────────────────────────────────────────────────

    def _header(self, payload: dict, name: str) -> str:
        """Pull a header value (case-insensitive) from a message payload."""
        for h in payload.get("headers", []):
            if h.get("name", "").lower() == name.lower():
                return h.get("value", "")
        return ""

    def _extract_body(self, payload: dict) -> str:
        """Walk MIME parts and return the best plain-text body we can find."""
        # Plain payload with no parts
        if not payload.get("parts"):
            data = payload.get("body", {}).get("data", "")
            if data:
                return self._decode_b64url(data)
            return ""

        # Prefer text/plain. Fall back to text/html stripped of tags.
        plain = ""
        html = ""

        def walk(parts):
            nonlocal plain, html
            for p in parts:
                mime = p.get("mimeType", "")
                if mime == "text/plain" and not plain:
                    data = p.get("body", {}).get("data", "")
                    if data:
                        plain = self._decode_b64url(data)
                elif mime == "text/html" and not html:
                    data = p.get("body", {}).get("data", "")
                    if data:
                        html = self._decode_b64url(data)
                elif p.get("parts"):
                    walk(p["parts"])

        walk(payload["parts"])

        if plain:
            return plain
        if html:
            # Crude HTML strip — good enough for triage.
            import re
            text = re.sub(r"<[^>]+>", " ", html)
            text = re.sub(r"\s+", " ", text).strip()
            return text
        return ""

    @staticmethod
    def _decode_b64url(data: str) -> str:
        try:
            return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
        except Exception:
            return ""

    def _get_or_create_label(self, name: str) -> str | None:
        """Return label id for a given name, creating it if missing."""
        service = self._get_service()
        if service is None:
            return None

        if self._label_cache is None:
            result = service.users().labels().list(userId="me").execute()
            self._label_cache = {
                lbl["name"]: lbl["id"] for lbl in result.get("labels", [])
            }

        if name in self._label_cache:
            return self._label_cache[name]

        new_label = service.users().labels().create(
            userId="me",
            body={
                "name": name,
                "labelListVisibility": "labelShow",
                "messageListVisibility": "show",
            },
        ).execute()
        self._label_cache[name] = new_label["id"]
        logger.info(f"Created Gmail label: {name}")
        return new_label["id"]

    # ── Tool implementations ─────────────────────────────────────

    def _check_inbox(self, unread_only: bool, max_results: int) -> str:
        service = self._get_service()
        if service is None:
            return (
                "Email isn't set up — no credentials.json found at "
                f"{self.creds_path}. Ask {self.user_name} to finish the email skill setup."
            )

        max_results = max(1, min(int(max_results), HARD_LIST_LIMIT))
        query = "is:unread" if unread_only else ""

        result = service.users().messages().list(
            userId="me", q=query, maxResults=max_results
        ).execute()
        messages = result.get("messages", [])

        if not messages:
            return "Inbox is clear." if unread_only else "No mail at all."

        lines = [f"{self.inbox_name} — {len(messages)} message(s)"
                 + (" (unread only)" if unread_only else "") + ":\n"]
        for m in messages:
            msg = service.users().messages().get(
                userId="me", id=m["id"], format="metadata",
                metadataHeaders=["From", "Subject", "Date"],
            ).execute()
            payload = msg.get("payload", {})
            sender = self._header(payload, "From")
            subject = self._header(payload, "Subject") or "(no subject)"
            date = self._header(payload, "Date")
            snippet = msg.get("snippet", "")[:200]
            lines.append(f"  ID: {m['id']}")
            lines.append(f"  From:    {sender}")
            lines.append(f"  Subject: {subject}")
            lines.append(f"  Date:    {date}")
            if snippet:
                lines.append(f"  Snippet: {snippet}")
            lines.append("")
        return "\n".join(lines).rstrip()

    def _read_email(self, msg_id: str) -> str:
        if not msg_id:
            return "Need a message ID. Use check_inbox first."
        service = self._get_service()
        if service is None:
            return "Email isn't set up."

        msg = service.users().messages().get(
            userId="me", id=msg_id, format="full"
        ).execute()
        payload = msg.get("payload", {})
        sender = self._header(payload, "From")
        subject = self._header(payload, "Subject") or "(no subject)"
        date = self._header(payload, "Date")
        body = self._extract_body(payload).strip()

        if len(body) > MAX_BODY_LENGTH:
            body = body[:MAX_BODY_LENGTH] + (
                f"\n\n... [truncated at {MAX_BODY_LENGTH:,} chars — full body is "
                f"{len(body):,} chars]"
            )

        return (
            f"From:    {sender}\n"
            f"Subject: {subject}\n"
            f"Date:    {date}\n"
            f"--- BODY ---\n"
            f"{body if body else '(empty body)'}"
        )

    def _categorize_email(self, msg_id: str, category: str) -> str:
        if not msg_id or not category:
            return "Need both id and category."
        # Light validation — push toward the configured label namespace.
        if self.label_prefix and not category.startswith(self.label_prefix):
            category = f"{self.label_prefix}{category}"

        label_id = self._get_or_create_label(category)
        if label_id is None:
            return "Could not create/find label."

        service = self._get_service()
        service.users().messages().modify(
            userId="me", id=msg_id, body={"addLabelIds": [label_id]}
        ).execute()
        logger.info(f"Labeled {msg_id} as {category}")
        return f"Labeled as {category}."

    def _flag_for_user(self, msg_id: str, why: str) -> str:
        if not msg_id:
            return "Need a message ID."
        service = self._get_service()
        if service is None:
            return "Email isn't set up."

        # Star it in Gmail so the user sees it visually.
        service.users().messages().modify(
            userId="me", id=msg_id, body={"addLabelIds": ["STARRED"]}
        ).execute()

        # Pull headers for the Telegram surface.
        msg = service.users().messages().get(
            userId="me", id=msg_id, format="metadata",
            metadataHeaders=["From", "Subject"],
        ).execute()
        payload = msg.get("payload", {})
        self.pending_flagged.append({
            "id": msg_id,
            "from": self._header(payload, "From"),
            "subject": self._header(payload, "Subject") or "(no subject)",
            "why": why,
        })
        logger.info(f"Flagged {msg_id} for user: {why}")
        return f"Flagged for {self.user_name}. Reason: {why}"

    def _archive_email(self, msg_id: str) -> str:
        if not msg_id:
            return "Need a message ID."
        service = self._get_service()
        if service is None:
            return "Email isn't set up."

        service.users().messages().modify(
            userId="me", id=msg_id, body={"removeLabelIds": ["INBOX"]}
        ).execute()
        logger.info(f"Archived {msg_id}")
        return "Archived (moved out of inbox)."

    def _draft_reply(self, msg_id: str, body: str) -> str:
        if not msg_id or not body:
            return "Need both id and body."
        service = self._get_service()
        if service is None:
            return "Email isn't set up."

        # Pull headers for proper threading.
        original = service.users().messages().get(
            userId="me", id=msg_id, format="metadata",
            metadataHeaders=["From", "Subject", "Message-ID", "References"],
        ).execute()
        payload = original.get("payload", {})
        thread_id = original.get("threadId")
        original_from = self._header(payload, "From")
        original_subject = self._header(payload, "Subject") or ""
        original_msgid = self._header(payload, "Message-ID")
        original_refs = self._header(payload, "References")

        reply_subject = (
            original_subject
            if original_subject.lower().startswith("re:")
            else f"Re: {original_subject}"
        )

        mime = MIMEText(body, _charset="utf-8")
        mime["To"] = original_from
        mime["Subject"] = reply_subject
        if original_msgid:
            mime["In-Reply-To"] = original_msgid
            mime["References"] = (
                f"{original_refs} {original_msgid}".strip()
                if original_refs
                else original_msgid
            )

        raw = base64.urlsafe_b64encode(mime.as_bytes()).decode("ascii")
        draft = service.users().drafts().create(
            userId="me",
            body={"message": {"raw": raw, "threadId": thread_id}},
        ).execute()
        logger.info(f"Created draft {draft.get('id')} in thread {thread_id}")
        return (
            f"Draft saved to Gmail Drafts ({len(body)} chars). "
            f"{self.user_name} can review and send from Gmail."
        )
