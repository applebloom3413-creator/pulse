"""
reddit skill — read a subreddit's public RSS/Atom feed (Reddit's sanctioned path).

Reddit blocks scraping and is hostile to bots, but it *does* publish public
RSS/Atom feeds (https://www.reddit.com/r/<sub>/.rss). That's the front door, so
that's what we use — no scraping, no evasion. The catch: Reddit rate-limits
automated reads HARD (a few quick hits → HTTP 429). So this skill is built to be
a patient, polite reader:

  - Caches each feed for a while (CACHE_TTL_MIN) and serves the cache instead of
    re-fetching.
  - Enforces a household-wide COOLDOWN between live fetches (Reddit throttles by
    IP, which every persona on this machine shares).
  - On a 429, serves stale cache if it has any, else asks the companion to wait.

One tool (read_reddit_feed). Opt-in per persona (default off in base config).
"""

import gzip
import html
import json
import logging
import os
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path

from skills.base import BaseSkill

logger = logging.getLogger(__name__)

CACHE_TTL_MIN = 25     # serve a cached feed (no re-fetch) for this many minutes
COOLDOWN_SEC = 45      # min seconds between LIVE Reddit fetches, household-wide
MAX_ITEMS = 12
USER_AGENT = "PulseRSS/1.0 (personal AI-companion feed reader)"

# ── Household-wide advisory lock on the cache read-modify-write cycle.
# Every RedditSkill caller (personas + any MCP bridge) runs its
# fetch under this lock so no two processes can corrupt the shared
# cache file. Lock timeout is generous enough to cover a single Reddit
# fetch (~1–25s) so a waiter is served the fresh cache written by the
# holder rather than firing its own duplicate hit. Stale locks older
# than STALE_LOCK_SEC are presumed dead (crash-left) and cleared.
_LOCK_TIMEOUT_SEC = 30.0
_LOCK_POLL_SEC = 0.05
STALE_LOCK_SEC = 60


class RedditSkill(BaseSkill):
    name = "reddit"

    def __init__(self, config: dict):
        super().__init__(config)
        # Shared cache + cooldown across all personas (Reddit throttles by IP,
        # which the whole household shares). Lives in Pulse's data dir.
        persona_dir = config.get("_persona_dir")
        if persona_dir:
            self.cache_file = Path(persona_dir).parent.parent / "data" / "reddit_cache.json"
        else:
            self.cache_file = Path("data") / "reddit_cache.json"
        # Advisory lock sibling to the cache — shared with any other process
        # that instantiates RedditSkill against the same Pulse root.
        self.lock_file = self.cache_file.with_suffix(self.cache_file.suffix + ".lock")

    def get_tools(self) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "read_reddit_feed",
                    "description": (
                        "Read the recent posts of a subreddit via its public RSS feed — "
                        "titles, authors, links, and a snippet. Give a subreddit name (e.g. "
                        "'Cryptids') and optionally a sort (hot/new/top/rising). Heads up: "
                        "Reddit throttles automated reads hard, so results are often cached "
                        "and a live refresh may be asked to wait a bit. Be patient with it."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "subreddit": {
                                "type": "string",
                                "description": "Subreddit name, without the 'r/' (e.g. 'folklore').",
                            },
                            "sort": {
                                "type": "string",
                                "description": "hot (default), new, top, or rising.",
                            },
                        },
                        "required": ["subreddit"],
                    },
                },
            },
        ]

    def execute(self, tool_name: str, arguments: dict) -> str:
        if tool_name == "read_reddit_feed":
            try:
                return self._read_feed(arguments)
            except Exception as e:
                logger.exception(f"reddit failed: {e}")
                return f"Couldn't read that subreddit right now ({e})."
        return f"Unknown reddit tool: {tool_name}"

    # ── cache ────────────────────────────────────────────────────
    def _load_cache(self) -> dict:
        if self.cache_file.exists():
            try:
                d = json.loads(self.cache_file.read_text(encoding="utf-8"))
                if isinstance(d, dict):
                    return d
            except Exception:
                pass
        return {"feeds": {}, "last_fetch": 0}

    def _save_cache(self, cache: dict) -> None:
        # Atomic: write to a sibling .tmp then rename over the real file.
        # Prevents any reader from seeing a half-written cache and prevents
        # a crash between open() and write() from truncating the file.
        try:
            self.cache_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.cache_file.with_suffix(self.cache_file.suffix + ".tmp")
            tmp.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
            os.replace(str(tmp), str(self.cache_file))
        except Exception as e:
            logger.warning(f"Could not save reddit cache: {e}")

    # ── Advisory lock around the cache read-modify-write cycle ────
    def _acquire_lock(self) -> bool:
        """Best-effort cross-process lock via O_EXCL create. Returns True on
        acquisition, False on timeout. Stale locks older than STALE_LOCK_SEC
        are cleared (presumed crash-left)."""
        deadline = time.monotonic() + _LOCK_TIMEOUT_SEC
        while time.monotonic() < deadline:
            try:
                self.lock_file.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(str(self.lock_file), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                try:
                    os.write(fd, f"{os.getpid()}\n".encode())
                finally:
                    os.close(fd)
                return True
            except FileExistsError:
                try:
                    age = time.time() - self.lock_file.stat().st_mtime
                    if age > STALE_LOCK_SEC:
                        self.lock_file.unlink()
                        continue
                except FileNotFoundError:
                    continue
                except Exception:
                    pass
                time.sleep(_LOCK_POLL_SEC)
        return False

    def _release_lock(self) -> None:
        try:
            self.lock_file.unlink()
        except FileNotFoundError:
            pass
        except Exception as e:
            logger.warning(f"Could not release reddit cache lock: {e}")

    @staticmethod
    def _fetch(url: str) -> str:
        req = urllib.request.Request(url, headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/atom+xml, application/rss+xml, */*",
            "Accept-Encoding": "gzip",
        })
        resp = urllib.request.urlopen(req, timeout=25)
        data = resp.read()
        if data[:2] == b"\x1f\x8b":
            data = gzip.decompress(data)
        return data.decode("utf-8", "replace")

    @staticmethod
    def _parse_atom(raw: str) -> list[dict]:
        items = []
        for e in re.findall(r"(?is)<entry\b.*?</entry>", raw)[:MAX_ITEMS]:
            def g(tag):
                m = re.search(r"(?is)<" + tag + r"[^>]*>(.*?)</" + tag + ">", e)
                return html.unescape(re.sub(r"<[^>]+>", "", m.group(1)).strip()) if m else ""
            am = re.search(r"(?is)<author>.*?<name>(.*?)</name>", e)
            lm = re.search(r'(?is)<link[^>]*href="([^"]+)"', e)
            cm = re.search(r"(?is)<content[^>]*>(.*?)</content>", e)
            # Reddit escapes the post HTML inside <content>, so unescape FIRST
            # (turns &lt;p&gt; back into <p>), THEN strip the now-real tags.
            content = re.sub(r"(?is)<[^>]+>", " ", html.unescape(cm.group(1))) if cm else ""
            items.append({
                "title": g("title"),
                "author": html.unescape(am.group(1).strip()) if am else "",
                "link": lm.group(1) if lm else "",
                "updated": g("updated"),
                "snippet": re.sub(r"\s+", " ", content).strip()[:220],
            })
        return items

    def _format(self, sub: str, sort: str, items: list[dict], freshness: str) -> str:
        if not items:
            return f"r/{sub} ({sort}) came back empty — the sub may be private, empty, or misspelled."
        lines = [f"r/{sub} · {sort} — {len(items)} posts ({freshness}):"]
        for it in items:
            lines.append("")
            lines.append(f"  • {it['title']}")
            meta = [x for x in (it.get("author"), (it.get("updated") or "")[:10]) if x]
            if meta:
                lines.append(f"      ({' · '.join(meta)})")
            if it.get("snippet"):
                lines.append(f"      {it['snippet']}")
            if it.get("link"):
                lines.append(f"      {it['link']}")
        lines.append("")
        lines.append("(Reddit throttles automated reads, so this can be cached. To read a "
                     "specific thread, save it to Obsidian — read_web_page won't open reddit links.)")
        return "\n".join(lines)

    def _read_feed(self, args: dict) -> str:
        sub = (args.get("subreddit") or "").strip()
        sub = re.sub(r"(?i)^/?r/", "", sub).strip("/")
        if not sub or not re.match(r"^[A-Za-z0-9_]+$", sub):
            return "Give me a subreddit name (letters/numbers/underscores), e.g. 'Cryptids'."
        sort = (args.get("sort") or "hot").strip().lower()
        if sort not in ("hot", "new", "top", "rising"):
            sort = "hot"
        url = f"https://www.reddit.com/r/{sub}/{sort}/.rss"

        # Serialize the whole read-modify-write cycle across the household —
        # personas AND any MCP bridge share this lock, so the cache
        # cannot be corrupted by concurrent writers and duplicate fetches
        # are avoided (a waiter that entered the lock during someone else's
        # fetch reads the freshly-populated cache and returns it).
        if not self._acquire_lock():
            # Very rare — a peer is holding the lock past the timeout. Serve
            # whatever cache we can see (unlocked read is fine; atomic writes
            # make torn reads impossible) or ask the caller to try again.
            cache = self._load_cache()
            cached = (cache.get("feeds") or {}).get(url)
            if cached:
                age = max(0, int((time.time() - cached.get("at", 0)) / 60))
                return self._format(sub, sort, cached.get("items", []),
                                    f"cached {age}m ago — another fetch is in flight, "
                                    "serving cached")
            return (f"Another Reddit fetch is in flight right now — try r/{sub} "
                    "again in a moment.")

        try:
            cache = self._load_cache()
            feeds = cache.setdefault("feeds", {})
            now = time.time()
            cached = feeds.get(url)

            # 1) fresh cache → serve it, no network.
            if cached and (now - cached.get("at", 0)) < CACHE_TTL_MIN * 60:
                age = max(0, int((now - cached["at"]) / 60))
                return self._format(sub, sort, cached.get("items", []), f"cached {age}m ago")

            # 2) cooldown gate (household-wide) → serve stale cache or ask to wait.
            if (now - cache.get("last_fetch", 0)) < COOLDOWN_SEC:
                if cached:
                    age = max(0, int((now - cached["at"]) / 60))
                    return self._format(sub, sort, cached.get("items", []),
                                        f"cached {age}m ago — Reddit limits how often I can refresh")
                wait = int(COOLDOWN_SEC - (now - cache.get("last_fetch", 0)))
                return (f"Reddit limits how often I can check (a feed was just pulled). "
                        f"Try r/{sub} again in about {wait}s.")

            # 3) live fetch.
            try:
                raw = self._fetch(url)
            except Exception as e:
                msg = str(e)
                if "429" in msg and cached:
                    age = max(0, int((now - cached["at"]) / 60))
                    return self._format(sub, sort, cached.get("items", []),
                                        f"cached {age}m ago — Reddit is rate-limiting live reads right now")
                if "429" in msg:
                    return ("Reddit's rate-limiting right now (they throttle automated reads hard). "
                            "Give it a few minutes and try again.")
                if "404" in msg:
                    return f"No subreddit r/{sub} found (or it's private)."
                return f"Couldn't reach r/{sub} ({e})."

            items = self._parse_atom(raw)
            cache["last_fetch"] = now
            feeds[url] = {"at": now, "items": items}
            self._save_cache(cache)
            return self._format(sub, sort, items, "fresh")
        finally:
            self._release_lock()
