"""
House Projects — sorting things (pictures, downloads, anything) in a folder
the user pointed a project at ("works on").

  look   — describe pictures with a small vision model (Claude Haiku, or
           GPT-4.1 mini for OpenAI-only companions), on the looking companion's
           own key, so they can sort by what's IN them.
           Descriptions are cached per file, so looking twice is free.
  plans  — the person on the active step proposes a SORTING PLAN: make folders,
           move / rename files. Nothing moves until the user presses Apply in
           their window. Every applied plan can be undone. Nothing is ever deleted.

Used by skills/household_projects.py and scripts/workbench_gui.pyw.
"""

import base64
import io
import json
import shutil
from pathlib import Path

from core import house_projects as hp

PULSE_ROOT = Path(__file__).resolve().parent.parent
VISION_MODEL = "claude-haiku-4-5-20251001"
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".heic"}
LOOK_PROMPT = ("Describe this picture in 1-3 short sentences so someone can sort it into folders: "
               "what or who is in it (pets, people, art, screenshot of an app/chat, memes, places, food...), "
               "and any obvious date, event or text. No preamble.")
READ_PROMPT = ("Describe this picture for a friend who can't see it. If it has ANY text in it — a social media "
               "post, messages, a caption, a sign, a meme — first copy all of that text out word for word "
               "(say who posted or sent it, and where, if that's shown). Then briefly describe everything "
               "else: people, layout, mood, style, anything notable. No preamble.")
MAX_LOOKS_PER_CALL = 8
MAX_MOVES_PER_PLAN = 2000


# ── looking at pictures ─────────────────────────────────────────────────

def _vision_route() -> tuple[str, str, str] | None:
    """(kind, model, key) using whatever key the LOOKING companion runs on, so a
    look is billed like the rest of their usage."""
    import os
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic", VISION_MODEL, os.environ["ANTHROPIC_API_KEY"]
    if os.environ.get("OPENROUTER_API_KEY"):
        return "openrouter", "anthropic/claude-haiku-4.5", os.environ["OPENROUTER_API_KEY"]
    if os.environ.get("OPENAI_API_KEY"):
        return "openai", "gpt-4.1-mini", os.environ["OPENAI_API_KEY"]
    return None


def _openai_style(endpoint: str, model: str, key: str, data: bytes, media: str,
                  prompt: str = LOOK_PROMPT, max_tokens: int = 160) -> str:
    import urllib.request
    body = {"model": model, "max_tokens": max_tokens, "messages": [{"role": "user", "content": [
        {"type": "text", "text": prompt},
        {"type": "image_url", "image_url": {"url": f"data:{media};base64,{base64.b64encode(data).decode()}"}}]}]}
    req = urllib.request.Request(endpoint, data=json.dumps(body).encode(),
                                 headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        out = json.loads(r.read().decode())
    return (out["choices"][0]["message"].get("content") or "").strip()


def _looks_path(proj: dict) -> Path:
    return hp.PROJECTS_ROOT / proj["slug"] / hp.META / "looks.json"


def _load_looks(proj: dict) -> dict:
    try:
        return json.loads(_looks_path(proj).read_text(encoding="utf-8"))
    except Exception:
        return {}


def _image_bytes(path: Path, size: int = 768) -> tuple[bytes, str]:
    """Shrink to ~768px JPEG so each look stays cheap (bigger for reading screenshots)."""
    from PIL import Image
    img = Image.open(path)
    img.thumbnail((size, size))
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=80)
    return buf.getvalue(), "image/jpeg"


def _describe(path: Path, prompt: str = LOOK_PROMPT, max_tokens: int = 160, size: int = 768) -> str:
    route = _vision_route()
    if not route:
        raise RuntimeError("no Anthropic / OpenRouter / OpenAI key to look with")
    kind, model, key = route
    data, media = _image_bytes(path, size)
    if kind == "openrouter":
        return _openai_style("https://openrouter.ai/api/v1/chat/completions", model, key, data, media,
                             prompt, max_tokens)
    if kind == "openai":
        return _openai_style("https://api.openai.com/v1/chat/completions", model, key, data, media,
                             prompt, max_tokens)
    import anthropic
    msg = anthropic.Anthropic(api_key=key).messages.create(
        model=model, max_tokens=max_tokens,
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": media,
                                         "data": base64.b64encode(data).decode()}},
            {"type": "text", "text": prompt}]}])
    return " ".join(b.text for b in msg.content if getattr(b, "type", "") == "text").strip()


def look_own(proj: dict, rel: str = "", limit: int = MAX_LOOKS_PER_CALL) -> str:
    """Pictures the user attached to the project itself (e.g. a screenshot of a post).
    Text in them is copied out word for word."""
    pics = hp.pictures(proj)
    rel = rel.replace("\\", "/").strip().strip("/")
    if rel and rel != hp.PICTURES:
        chosen = [f for f in pics if f == rel or f.startswith(rel + "/")] or \
                 [f for f in pics if Path(f).name.lower() == Path(rel).name.lower()]
        if not chosen:
            return f"No picture called {rel} here. Pictures in this project: " + (", ".join(pics) or "(none)")
    else:
        chosen = pics
    if not chosen:
        return "This project has no pictures attached."
    base = hp.PROJECTS_ROOT / proj["slug"]
    looks = _load_looks(proj)
    out, new = [], 0
    for key in chosen:
        f = base / key
        stamp = f"{f.stat().st_size}:{int(f.stat().st_mtime)}"
        cached = looks.get("own:" + key)
        if cached and cached.get("stamp") == stamp:
            out.append(f"🖼 {key}:\n{cached['text']}")
            continue
        if new >= limit:
            continue
        try:
            text = _describe(f, READ_PROMPT, max_tokens=900, size=1400)
        except Exception as e:
            out.append(f"🖼 {key}: (couldn't look — {type(e).__name__}: {str(e)[:120]})")
            new += 1
            continue
        looks["own:" + key] = {"stamp": stamp, "text": text}
        new += 1
        out.append(f"🖼 {key}:\n{text}")
    hp.workbench._atomic_write(_looks_path(proj), json.dumps(looks, indent=1, ensure_ascii=False))
    if new:
        proj = hp.load(proj["slug"]) or proj
        proj["looks_count"] = proj.get("looks_count", 0) + new
        hp.save(proj)
    return "\n\n".join(out)


def look(proj: dict, rel: str, limit: int = MAX_LOOKS_PER_CALL) -> str:
    """Describe one picture, or up to `limit` not-yet-seen pictures in a folder."""
    norm = rel.replace("\\", "/").strip().strip("/")
    if not proj.get("target") or norm == hp.PICTURES or norm.startswith(hp.PICTURES + "/") \
            or (norm in hp.pictures(proj)):
        return look_own(proj, norm, limit)
    p, err = hp._target_file(proj, rel)
    if err:
        return err
    base = Path(proj["target"]).resolve()
    if p.is_dir():
        private = hp._private_paths()
        files = [f for f in sorted(p.iterdir()) if f.is_file() and f.suffix.lower() in IMAGE_EXT
                 and not any(f == q or hp._inside(f, q) for q in private)]
    elif p.is_file():
        files = [p]
    else:
        return f"Nothing called {rel} there."
    if not files:
        return "No pictures there (jpg, png, gif, webp, bmp, heic)."
    looks = _load_looks(proj)
    out, new = [], 0
    for f in files:
        key = str(f.relative_to(base)).replace("\\", "/")
        stamp = f"{f.stat().st_size}:{int(f.stat().st_mtime)}"
        cached = looks.get(key)
        if cached and cached.get("stamp") == stamp:
            out.append(f"• {key}: {cached['text']}")
            continue
        if new >= limit:
            continue
        try:
            text = _describe(f)
        except Exception as e:
            out.append(f"• {key}: (couldn't look — {type(e).__name__}: {str(e)[:120]})")
            new += 1
            continue
        looks[key] = {"stamp": stamp, "text": text}
        new += 1
        out.append(f"• {key}: {text}")
    hp.workbench._atomic_write(_looks_path(proj), json.dumps(looks, indent=1, ensure_ascii=False))
    proj = hp.load(proj["slug"]) or proj
    proj["looks_count"] = proj.get("looks_count", 0) + new
    hp.save(proj)
    left = sum(1 for f in files if str(f.relative_to(base)).replace("\\", "/") not in looks)
    tail = (f"\n\n({left} more picture{'s' if left != 1 else ''} not looked at yet — call look again "
            f"on the folder to continue.)") if left else ""
    return "\n".join(out[:200]) + tail


def look_count(proj: dict) -> int:
    return len(_load_looks(proj))


# ── sorting plans ───────────────────────────────────────────────────────

def _plans(proj: dict) -> list:
    return proj.setdefault("plans", [])


def _draft_plan(proj: dict, who: str, create: bool) -> dict | None:
    for pl in _plans(proj):
        if pl["status"] == "draft" and pl["by"].lower() == who.lower():
            return pl
    if not create:
        return None
    pl = {"id": f"plan{len(_plans(proj)) + 1}", "by": who, "at": hp.now_iso(), "note": "",
          "status": "draft", "moves": []}
    _plans(proj).append(pl)
    return pl


def add_moves(proj: dict, who: str, moves: list, note: str = "") -> str:
    """Add moves ({from, to} paths inside the folder) to who's sorting plan."""
    if not hp.can_write(proj, who) or hp.is_user(who):
        return "Only the person whose turn it is can make a sorting plan (anyone can comment)."
    if not proj.get("target"):
        return f"This project doesn't work on a folder — {hp.USER} can set one with \"Works on\"."
    if not isinstance(moves, list) or not moves:
        return "Give moves as a list like [{\"from\": \"IMG_1.jpg\", \"to\": \"Pets/IMG_1.jpg\"}]."
    pl = _draft_plan(proj, who, create=True)
    taken_to = {m["to"] for m in pl["moves"]}
    taken_from = {m["from"] for m in pl["moves"]}
    ok, problems = 0, []
    for m in moves:
        src = str((m or {}).get("from", "")).strip().replace("\\", "/")
        dst = str((m or {}).get("to", "")).strip().replace("\\", "/")
        if dst.endswith("/"):
            dst += Path(src).name
        sp, err = hp._target_file(proj, src)
        if err or not sp.exists():
            problems.append(f"{src}: {err or 'not found'}")
            continue
        dp, err = hp._target_file(proj, dst)
        if err:
            problems.append(f"{dst}: {err}")
            continue
        if dp.exists() or dst in taken_to:
            problems.append(f"{dst}: something's already there — pick another name")
            continue
        if src in taken_from:
            problems.append(f"{src}: already in your plan")
            continue
        if sp.is_dir() and hp._inside(dp, sp):
            problems.append(f"{src}: can't move a folder inside itself")
            continue
        if len(pl["moves"]) >= MAX_MOVES_PER_PLAN:
            problems.append("plan is full — finish this one first")
            break
        pl["moves"].append({"from": src, "to": dst})
        taken_to.add(dst)
        taken_from.add(src)
        ok += 1
    if note.strip():
        pl["note"] = note.strip()
    s = proj["steps"][hp.current_index(proj)]
    s["plan_id"] = pl["id"]
    hp._log(proj, f"{who} added {ok} moves to a sorting plan", who)
    hp.save(proj)
    msg = f"✓ Added {ok} move{'s' if ok != 1 else ''} to your sorting plan ({len(pl['moves'])} total). " \
          f"Nothing has moved yet — {hp.USER} approves the plan after you finish your turn."
    if problems:
        msg += "\nSkipped:\n  " + "\n  ".join(problems[:30])
    return msg


def show_plan(proj: dict, who: str) -> str:
    pl = _draft_plan(proj, who, create=False)
    if not pl or not pl["moves"]:
        return "Your sorting plan is empty."
    lines = [f"  {m['from']}  →  {m['to']}" for m in pl["moves"][:300]]
    more = f"\n  ... and {len(pl['moves']) - 300} more" if len(pl["moves"]) > 300 else ""
    return f"Your sorting plan ({len(pl['moves'])} moves):\n" + "\n".join(lines) + more


def clear_plan(proj: dict, who: str) -> str:
    pl = _draft_plan(proj, who, create=False)
    if not pl:
        return "You don't have a plan in progress."
    _plans(proj).remove(pl)
    hp.save(proj)
    return "Plan cleared — start fresh any time."


def submit_plan_on_done(proj: dict, who: str) -> str:
    """Called when who finishes their step: their draft plan goes to the user."""
    pl = _draft_plan(proj, who, create=False)
    if not pl:
        return ""
    if not pl["moves"]:
        _plans(proj).remove(pl)
        return ""
    pl["status"] = "waiting"
    pl["submitted_at"] = hp.now_iso()
    hp.workbench.notify_user(f"🗂 {who}'s sorting plan for \"{proj['name']}\" is ready — {len(pl['moves'])} "
                             f"moves waiting for your OK in the Workbench.", key=f"plan:{proj['slug']}:{pl['id']}")
    return f" Your sorting plan ({len(pl['moves'])} moves) went to {hp.USER} for approval."


def apply_plan(proj: dict, plan_id: str) -> str:
    """The user pressed Apply. Moves files; never overwrites, never deletes."""
    pl = next((p for p in _plans(proj) if p["id"] == plan_id), None)
    if not pl or pl["status"] != "waiting":
        return "That plan isn't waiting for approval."
    done, skipped, created = [], [], []
    for m in pl["moves"]:
        sp, e1 = hp._target_file(proj, m["from"])
        dp, e2 = hp._target_file(proj, m["to"])
        if e1 or e2 or not sp.exists():
            skipped.append(f"{m['from']}: {e1 or e2 or 'not there anymore'}")
            continue
        if dp.exists():
            skipped.append(f"{m['to']}: already exists")
            continue
        parent = dp.parent
        new_dirs = []
        while not parent.exists():
            new_dirs.append(parent)
            parent = parent.parent
        try:
            dp.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(sp), str(dp))
        except OSError as e:
            skipped.append(f"{m['from']}: {e}")
            continue
        created += [str(d) for d in reversed(new_dirs)]
        done.append(m)
    pl.update({"status": "applied", "applied_at": hp.now_iso(), "applied": done, "skipped": skipped,
               "created_dirs": created})
    hp._log(proj, f"{hp.USER} applied {pl['by']}'s sorting plan ({len(done)} moves)", hp.USER)
    hp.save(proj)
    msg = f"Sorted! Moved {len(done)} item{'s' if len(done) != 1 else ''}."
    if skipped:
        msg += f" Skipped {len(skipped)} (details in the plan)."
    return msg


def undo_plan(proj: dict, plan_id: str) -> str:
    pl = next((p for p in _plans(proj) if p["id"] == plan_id), None)
    if not pl or pl["status"] != "applied":
        return "That plan hasn't been applied."
    back, stuck = 0, []
    for m in reversed(pl.get("applied", [])):
        sp, _ = hp._target_file(proj, m["from"])
        dp, _ = hp._target_file(proj, m["to"])
        if dp is None or sp is None or not dp.exists() or sp.exists():
            stuck.append(m["to"])
            continue
        sp.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(dp), str(sp))
        back += 1
    for d in reversed(pl.get("created_dirs", [])):     # tidy folders the plan made, if empty
        try:
            Path(d).rmdir()
        except OSError:
            pass
    pl["status"] = "undone"
    pl["undone_at"] = hp.now_iso()
    hp._log(proj, f"{hp.USER} undid {pl['by']}'s sorting plan", hp.USER)
    hp.save(proj)
    return f"Undone — moved {back} back." + (f" {len(stuck)} couldn't go back (changed since)." if stuck else "")


def reject_plan(proj: dict, plan_id: str, note: str = "") -> str:
    pl = next((p for p in _plans(proj) if p["id"] == plan_id), None)
    if not pl or pl["status"] != "waiting":
        return "That plan isn't waiting."
    pl["status"] = "rejected"
    pl["reject_note"] = note
    hp.save(proj)
    hp.comment(proj, hp.USER, f"(About {pl['by']}'s sorting plan: not this time.{' ' + note if note else ''})")
    return "Plan set aside — nothing moved."
