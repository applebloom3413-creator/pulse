"""
Fill in search data (embeddings) for Local Reddit posts that don't have any
— e.g. posts made from the LoR window, which never ran the companions' tool.
Same text recipe as skills/lor.py _save_post_embedding, so search treats them
all alike. Safe to run any time (e.g. from a nightly job).
"""
import json
import sys
from pathlib import Path

PULSE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PULSE))
LOR = PULSE / "data" / "lor"


def text_for(post: dict, by_id: dict) -> str:
    if post.get("reply_to"):
        parent = by_id.get(post["reply_to"])
        title = parent.get("title", "Unknown Thread") if parent else "Unknown Thread"
        return f"Reply in thread '{title}':\n{post['content']}".strip()
    return f"{post.get('title', '')}\n\n{post['content']}".strip()


def backfill() -> int:
    posts = json.loads((LOR / "posts.json").read_text(encoding="utf-8"))
    emb_path = LOR / "embeddings.json"
    emb = json.loads(emb_path.read_text(encoding="utf-8")) if emb_path.exists() else {}
    missing = [p for p in posts if p.get("id") not in emb and (p.get("content") or "").strip()]
    if not missing:
        return 0
    from core.context import load_embedding_model, _get_embedding_model
    load_embedding_model()
    model = _get_embedding_model()
    if not model:
        raise RuntimeError("embedding model didn't load")
    by_id = {p["id"]: p for p in posts}
    new = {p["id"]: model.encode(text_for(p, by_id)).tolist() for p in missing}
    emb = json.loads(emb_path.read_text(encoding="utf-8")) if emb_path.exists() else {}   # re-read right before saving
    emb.update({k: v for k, v in new.items() if k not in emb})
    tmp = emb_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(emb), encoding="utf-8")
    tmp.replace(emb_path)
    return len(new)


if __name__ == "__main__":
    print(f"Added search data for {backfill()} Local Reddit posts.")
