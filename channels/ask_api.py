"""
Ask API channel — a tiny authenticated HTTP door into a persona.

Lets you talk to a companion from a phone shortcut (e.g. an iOS Shortcut
for "Hey Siri, ask <companion>"): the shortcut dictates a question, POSTs
it here over a private network, and speaks the reply. The question goes
through engine.handle_message() — the same pipeline as Telegram — so it's
the same companion: their memory, their skills (web_search included),
their voice, and the exchange lands in their conversation history like
any other chat.

Security model:
  - Off by default; per-persona opt-in (channels.ask_api.enabled).
  - Requires ASK_API_TOKEN in the persona's .env; refuses to start
    without one. Every request must carry it (X-Ask-Token header).
  - Intended to be reached over a private network (e.g. Tailscale or
    another VPN), never the open internet. Do not port-forward this.

Zero new dependencies: uvicorn (already installed) + a hand-rolled ASGI
app. No flask/fastapi/aiohttp needed.

Phone side: POST /ask with header X-Ask-Token and either a plain-text body
(plain-text reply) or JSON {"text": "..."} (JSON reply). GET /health
answers {"ok": true}.
"""

import asyncio
import json
import logging
import os

from channels.base import Channel

logger = logging.getLogger(__name__)

MAX_BODY_BYTES = 16_384          # a dictated question, not a document
DEFAULT_PORT = 8781


class AskApiChannel(Channel):
    """Authenticated POST /ask → engine.handle_message → JSON reply."""

    def __init__(self, config: dict):
        cfg = (config.get("channels", {}) or {}).get("ask_api", {}) or {}
        self.host = cfg.get("host", "0.0.0.0")
        self.port = int(cfg.get("port", DEFAULT_PORT))
        self.token = os.environ.get("ASK_API_TOKEN", "")
        # Echo each exchange into Telegram so it can be re-read later — a
        # voice assistant speaks the reply once and it's gone; the Telegram
        # copy is the scrollback.
        self.echo_to_telegram = bool(cfg.get("echo_to_telegram", True))
        self._engine = None
        self._server = None
        self._serve_task = None

    def set_engine(self, engine):
        self._engine = engine

    # ── ASGI app ─────────────────────────────────────────────────

    async def _asgi(self, scope, receive, send):
        if scope["type"] != "http":
            return

        path = scope.get("path", "")
        method = scope.get("method", "")

        # Match by suffix rather than exact path: a reverse proxy (e.g.
        # `tailscale serve`) may mount each persona under a path prefix
        # (/<persona>) and pass the prefix through to us — both /ask and
        # /<persona>/ask are this persona's door. (Each persona is its own process+port, so
        # there's no cross-talk risk.)
        if method == "GET" and path.endswith("/health"):
            await self._respond(send, 200, {"ok": True})
            return

        if method != "POST" or not path.endswith("/ask"):
            await self._respond(send, 404, {"error": "unknown endpoint"})
            return

        headers = {k.decode("latin-1").lower(): v.decode("latin-1")
                   for k, v in scope.get("headers", [])}
        if not self.token or headers.get("x-ask-token", "") != self.token:
            logger.warning("ask_api: rejected request with bad/missing token")
            await self._respond(send, 401, {"error": "bad token"})
            return

        body = b""
        while True:
            event = await receive()
            body += event.get("body", b"")
            if len(body) > MAX_BODY_BYTES:
                await self._respond(send, 413, {"error": "question too long"})
                return
            if not event.get("more_body"):
                break

        # Two body styles, so the iOS Shortcut can stay dead simple:
        #   JSON  {"text": "..."}  → JSON reply {"reply": ..., "tools_used": ...}
        #   plain text body        → plain text reply (no JSON anywhere)
        plain = False
        try:
            text = (json.loads(body or b"{}").get("text") or "").strip()
        except (json.JSONDecodeError, AttributeError):
            text = body.decode("utf-8", errors="replace").strip()
            plain = True
        if not text:
            await self._respond(send, 400, {"error": "send a question — plain text body, or JSON {\"text\": \"...\"}"})
            return

        if not self._engine:
            await self._respond(send, 503, {"reply": "(I'm still waking up — try again in a minute.)"})
            return

        logger.info(f"ask_api: question received ({len(text)} chars)")
        try:
            # handle_message returns (reply, tools_used, reasoning, msg_db_ids)
            result = await self._engine.handle_message(text, source="ask_api")
            reply, tools_used = result[0], result[1]
        except Exception as e:
            logger.error(f"ask_api: handle_message crashed: {e}", exc_info=True)
            await self._respond(send, 500, {"reply": "(Something broke on my end — check the logs when you're home.)"})
            return

        if not reply:
            fallback = getattr(getattr(self._engine, "llm", None), "last_error", "") or ""
            reply = f"({fallback})" if fallback else "(I couldn't come up with a reply just now — try me again.)"

        if plain:
            await self._respond_text(send, 200, reply)
        else:
            await self._respond(send, 200, {
                "reply": reply,
                "tools_used": tools_used or [],
            })

        # Fire-and-forget the Telegram echo AFTER responding, so a slow or
        # broken Telegram never delays the spoken answer.
        if self.echo_to_telegram:
            asyncio.create_task(self._echo_exchange(text, reply))

    async def _echo_exchange(self, question: str, reply: str):
        try:
            telegram = (getattr(self._engine, "channels", None) or {}).get("telegram")
            if not telegram:
                return
            await telegram.send(f"📻 (via Ask API)\nYou: {question}\n\n{reply}")
        except Exception as e:
            logger.warning(f"ask_api: telegram echo failed (reply already delivered): {e}")

    @staticmethod
    async def _respond_text(send, status: int, text: str):
        await send({
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", b"text/plain; charset=utf-8")],
        })
        await send({"type": "http.response.body", "body": text.encode("utf-8")})

    @staticmethod
    async def _respond(send, status: int, payload: dict):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        await send({
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", b"application/json; charset=utf-8")],
        })
        await send({"type": "http.response.body", "body": body})

    # ── Channel interface ────────────────────────────────────────

    async def initialize(self):
        if not self.token:
            raise RuntimeError(
                "ask_api enabled but ASK_API_TOKEN is not set in this "
                "persona's .env — refusing to start an unauthenticated door."
            )
        import uvicorn
        config = uvicorn.Config(
            app=self._asgi, host=self.host, port=self.port,
            log_level="warning", lifespan="off",
            # A bound method fails uvicorn's ASGI3 autodetection and gets
            # wrapped as legacy ASGI2 — declare the interface explicitly.
            interface="asgi3",
        )
        self._server = uvicorn.Server(config)
        self._serve_task = asyncio.create_task(self._server.serve())
        logger.info(f"ask_api: listening on {self.host}:{self.port} (token required)")

    async def send(self, message: str, **kwargs):
        # Inbound-only channel — heartbeat notifications don't go out this way.
        pass

    async def shutdown(self):
        if self._server:
            self._server.should_exit = True
        if self._serve_task:
            try:
                await asyncio.wait_for(self._serve_task, timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
        logger.info("ask_api: stopped.")
