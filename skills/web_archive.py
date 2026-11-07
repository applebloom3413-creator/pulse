"""
web_archive skill — read a web page through the Internet Archive (Wayback Machine).

The companions can already search the web (web_search) and read the user's clips
(obsidian), but they can't pull the full text of an arbitrary page. This gives
them a *snapshot* of a page via the Internet Archive — a bot-friendly nonprofit
(like Project Gutenberg) that's happy to be read programmatically.

Design / ethics:
  - It reads from web.archive.org, never the live site directly. So it's an
    archive lookup, not live scraping.
  - It will NOT route around sites that explicitly block bots. AO3 and Reddit
    have said "no bots" loudly; pulling their content via the archive anyway is
    still doing the thing they said no to. Those hosts are blocked here. (Reddit
    has a sanctioned RSS path; AO3 fics come in via the user's own downloads.)
  - One tool, lightweight (read_web_page). Opt-in/out per persona via config.

No personal config needed; works the same for everyone who has it enabled.
"""

import html
import json
import logging
import re
import urllib.parse
import urllib.request

from skills.base import BaseSkill

logger = logging.getLogger(__name__)

# Hosts that have explicitly refused automated access — we don't archive-launder
# our way past their "no." (Substring match against the URL's host.)
_BLOCKED_HOSTS = ("archiveofourown.org", "reddit.com", "redd.it")
MAX_CHARS = 50_000  # plus offset/pagination so long pages
                    # can still be reached in chunks.


class WebArchiveSkill(BaseSkill):
    name = "web_archive"

    def get_tools(self) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "read_web_page",
                    "description": (
                        "Read a web page through the Internet Archive's Wayback Machine — "
                        "the saved snapshot of a page, not the live site. Good for "
                        "articles, references, and dead links. Give a URL. Long pages "
                        "are paginated — if the result says characters remain, call "
                        "again with the same URL and offset=N to continue. (It won't "
                        "route around sites that block bots — for Reddit use its feed, "
                        "for AO3 ask the user to save it.)"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "url": {
                                "type": "string",
                                "description": "The web page URL to read (e.g. 'https://en.wikipedia.org/wiki/Owl').",
                            },
                            "offset": {
                                "type": "integer",
                                "description": (
                                    "Character offset to start reading from "
                                    "(default 0). Use to continue reading a long "
                                    "page after a previous truncated read."
                                ),
                            },
                        },
                        "required": ["url"],
                    },
                },
            },
        ]

    def execute(self, tool_name: str, arguments: dict) -> str:
        if tool_name == "read_web_page":
            try:
                return self._read_web_page(arguments)
            except Exception as e:
                logger.exception(f"web_archive failed: {e}")
                return f"Couldn't read that page right now ({e})."
        return f"Unknown web_archive tool: {tool_name}"

    @staticmethod
    def _get(url: str, timeout: int = 30) -> str:
        import gzip
        import zlib
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (PulseWebArchive)",
            "Accept-Encoding": "gzip, deflate",
        })
        resp = urllib.request.urlopen(req, timeout=timeout)
        data = resp.read()
        enc = (resp.headers.get("Content-Encoding") or "").lower()
        if "gzip" in enc or data[:2] == b"\x1f\x8b":
            try:
                data = gzip.decompress(data)
            except Exception:
                pass
        elif "deflate" in enc:
            try:
                data = zlib.decompress(data)
            except Exception:
                try:
                    data = zlib.decompress(data, -zlib.MAX_WBITS)
                except Exception:
                    pass
        return data.decode("utf-8", "replace")

    def _read_web_page(self, args: dict) -> str:
        url = (args.get("url") or "").strip()
        if not url:
            return "Give me a URL to read."
        if not re.match(r"(?i)^https?://", url):
            url = "https://" + url
        host = urllib.parse.urlparse(url).netloc.lower()
        if any(b in host for b in _BLOCKED_HOSTS):
            return (
                f"{host} has asked not to be read by bots, and I won't route around that "
                "through the archive. For Reddit, use its RSS feed; for AO3, have the user save "
                "the page and ingest it. Most of the rest of the web is fair game here."
            )

        # Find the closest Wayback snapshot.
        try:
            avail = json.loads(self._get(
                "http://archive.org/wayback/available?url=" + urllib.parse.quote(url, safe=""), 25
            ))
        except Exception as e:
            return f"Couldn't reach the Internet Archive just now ({e}). Try again shortly."
        snap = (avail.get("archived_snapshots") or {}).get("closest") or {}
        if not snap.get("available") or not snap.get("url"):
            return (f"The Internet Archive has no saved snapshot of {url} — it may be too "
                    "new, or never archived.")

        ts = snap.get("timestamp", "")
        # The 'id_' form returns the raw archived page without the Wayback toolbar/chrome.
        raw = None
        for snap_url in (f"http://web.archive.org/web/{ts}id_/{url}" if ts else snap["url"], snap["url"]):
            try:
                raw = self._get(snap_url, 35)
                if raw:
                    break
            except Exception:
                continue
        if not raw:
            return f"Found a snapshot of {url} but couldn't fetch its contents."

        title_m = re.search(r"(?is)<title[^>]*>(.*?)</title>", raw)
        title = html.unescape(re.sub(r"\s+", " ", title_m.group(1)).strip()) if title_m else url
        body = re.sub(r"(?is)<(script|style|noscript|head).*?</\1>", " ", raw)
        body = re.sub(r"(?is)<(nav|header|footer|aside).*?</\1>", " ", body)
        # Prefer the largest <main>/<article> region when the page marks one —
        # cuts the site's nav menu and footer chrome out of the read.
        best = ""
        for m in re.finditer(r"(?is)<(main|article)\b[^>]*>(.*?)</\1>", body):
            if len(m.group(2)) > len(best):
                best = m.group(2)
        if best and len(re.sub(r"(?s)<[^>]+>", "", best)) > 400:
            body = best
        body = re.sub(r"(?i)<br\s*/?>", "\n", body)
        body = re.sub(r"(?i)</(p|div|h[1-6]|li|tr|section|article)>", "\n\n", body)
        text = html.unescape(re.sub(r"<[^>]+>", " ", body))
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text).strip()
        # Paginated read: slice from offset, cap at MAX_CHARS. If more
        # remains, tell the model how to ask for the next chunk.
        total = len(text)
        offset = int(args.get("offset", 0) or 0)
        offset = max(0, min(offset, total))
        end = min(offset + MAX_CHARS, total)
        chunk = text[offset:end]
        more = total - end

        when = f"{ts[:4]}-{ts[4:6]}-{ts[6:8]}" if len(ts) >= 8 else "unknown date"
        header = f"=== {title} ==="
        if offset > 0 or more > 0:
            header += f" (chars {offset:,}-{end:,} of {total:,})"
        result = (
            f"{header}\n"
            f"(Internet Archive snapshot, {when} — {url})\n\n"
            f"{chunk}"
        )
        if more > 0:
            result += (
                f"\n\n... [{more:,} chars remaining. To continue: "
                f"read_web_page(url='{url}', offset={end})]"
            )
        return result
