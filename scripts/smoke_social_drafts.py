"""Smoke test for social_drafts skill.

Exercises the full role loop against a temp DB:
  Ada drafts -> Basil lists pending -> Basil reads -> Basil reviews
  -> Ada sees verdict -> Ada revises -> Basil approves -> Basil marks posted.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from skills.social_drafts import SocialDraftsSkill  # noqa: E402


def _mk_skill(author_name: str, role: str, db_path: Path) -> SocialDraftsSkill:
    cfg = {
        "skills": {
            "social_drafts": {
                "enabled": True,
                "author_name": author_name,
                "role": role,
                "path": str(db_path),
            }
        }
    }
    return SocialDraftsSkill(cfg)


def hr(title: str) -> None:
    print(f"\n=== {title} ===")


def expect(label: str, result: str, must_contain: str | None = None) -> None:
    print(f"  [{label}] {result}")
    if must_contain is not None and must_contain not in result:
        print(f"    !! expected substring not found: {must_contain!r}")
        sys.exit(1)


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="social_drafts_smoke_"))
    db = tmp / "social_drafts.db"
    print(f"Using temp db: {db}")

    ada = _mk_skill("Ada", "author", db)
    basil = _mk_skill("Basil", "reviewer", db)
    clover = _mk_skill("Clover", "author", db)

    hr("Tool surface per role")
    expect("ada tools", ", ".join(
        t["function"]["name"] for t in ada.get_tools()
    ))
    expect("basil tools", ", ".join(
        t["function"]["name"] for t in basil.get_tools()
    ))
    expect("clover tools", ", ".join(
        t["function"]["name"] for t in clover.get_tools()
    ))

    hr("Ada drafts a bluesky post")
    expect(
        "draft1",
        ada.execute("draft_post", {
            "platform": "bluesky",
            "content": "first light. small joys count.",
        }),
        must_contain="Drafted post",
    )

    hr("Ada drafts a 2nd post (instagram, missing image_idea -> should fail)")
    expect(
        "ig-no-image",
        ada.execute("draft_post", {
            "platform": "instagram",
            "content": "caption without image",
        }),
        must_contain="image_idea",
    )

    hr("Ada drafts a 2nd post (instagram, with image_idea)")
    expect(
        "draft2",
        ada.execute("draft_post", {
            "platform": "instagram",
            "content": "lighthouse fog. caption tk.",
            "image_idea": "low fog rolling over the lamp room at dawn",
        }),
        must_contain="Drafted post",
    )

    hr("Ada tries to draft a 3rd this week -> weekly cap")
    expect(
        "cap",
        ada.execute("draft_post", {
            "platform": "bluesky",
            "content": "third one shouldn't land",
        }),
        must_contain="cap",
    )

    hr("Clover drafts independently (own cap)")
    expect(
        "clover1",
        clover.execute("draft_post", {
            "platform": "threads",
            "content": "a short test post. anyway,",
        }),
        must_contain="Drafted post",
    )

    hr("Ada's context injection (should mention 2/2)")
    expect("ada context", ada.get_context(), must_contain="2 of 2")

    hr("Basil's context injection (should mention 3 pending)")
    expect("basil context", basil.get_context(), must_contain="3")

    hr("Basil lists pending reviews")
    expect(
        "pending",
        basil.execute("list_pending_reviews", {}),
        must_contain="Ada",
    )

    hr("Basil reads draft #1")
    expect("read", basil.execute("read_draft", {"id": 1}), must_contain="bluesky")

    hr("Basil approves draft #1")
    expect(
        "approve",
        basil.execute("review_draft", {
            "id": 1,
            "verdict": "approved",
            "notes": "this lands. keep it.",
        }),
        must_contain="approved",
    )

    hr("Basil asks for revision on draft #2")
    expect(
        "revise",
        basil.execute("review_draft", {
            "id": 2,
            "verdict": "revise",
            "notes": "love the image idea but the caption needs more body.",
        }),
        must_contain="revise",
    )

    hr("Ada lists own drafts (sees verdicts)")
    expect(
        "my drafts",
        ada.execute("list_my_drafts", {}),
        must_contain="approved",
    )

    hr("Ada revises draft #2")
    expect(
        "revise edit",
        ada.execute("revise_draft", {
            "id": 2,
            "content": "lighthouse fog. the lamp doesn't argue with the weather. it just keeps.",
        }),
        must_contain="resubmitted",
    )

    hr("Basil reviews the revision (approve)")
    expect(
        "approve2",
        basil.execute("review_draft", {
            "id": 2,
            "verdict": "approved",
        }),
        must_contain="approved",
    )

    hr("Wrong-author guard: Clover tries to withdraw Ada's draft")
    expect(
        "wrong author",
        clover.execute("withdraw_draft", {"id": 1}),
        must_contain="not you",
    )

    hr("Role guard: Ada tries to use reviewer tool")
    expect(
        "role guard",
        ada.execute("review_draft", {"id": 1, "verdict": "approved"}),
        must_contain="reviewers",
    )

    hr("Basil marks #1 posted")
    expect(
        "mark posted",
        basil.execute("mark_posted", {"id": 1}),
        must_contain="posted",
    )

    hr("Basil marks #2 posted (also approved now)")
    expect(
        "mark posted 2",
        basil.execute("mark_posted", {"id": 2}),
        must_contain="posted",
    )

    hr("Final list_drafts (all)")
    expect("all", basil.execute("list_drafts", {"limit": 50}))

    hr("Filter: approved/threads (Clover's still pending review)")
    expect(
        "filter",
        basil.execute("list_drafts", {"status": "pending_review", "platform": "threads"}),
        must_contain="Clover",
    )

    print("\nAll checks passed.")


if __name__ == "__main__":
    main()
