"""Background Telethon client that the UI submits send-jobs to.

Telethon is asyncio-only, and pystray + tkinter are synchronous. We
run a single Telethon client on its own asyncio loop in a daemon
thread, and expose blocking `send_album(...)` from the UI thread by
submitting coroutines via `run_coroutine_threadsafe`.
"""
from __future__ import annotations

import asyncio
import os
import threading
from dataclasses import dataclass
from pathlib import Path

from .paths import ENV_FILE, SESSION_FILE


@dataclass
class SendResult:
    target_label: str
    ok: bool
    error: str = ""


def _load_env() -> tuple[int, str]:
    """Read API ID + Hash from tools/outbox/.env. Raises if missing."""
    if not ENV_FILE.exists():
        raise RuntimeError(
            f"No credentials at {ENV_FILE}. Run auth_setup.py first."
        )
    api_id = None
    api_hash = None
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if k == "TELEGRAM_API_ID":
            api_id = v
        elif k == "TELEGRAM_API_HASH":
            api_hash = v
    if not api_id or not api_hash:
        raise RuntimeError(
            f"TELEGRAM_API_ID / TELEGRAM_API_HASH missing in {ENV_FILE}."
        )
    return int(api_id), api_hash


class Sender:
    """Owns the Telethon client and its asyncio loop."""

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._client = None  # telethon.TelegramClient
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._start_error: BaseException | None = None

    def start(self) -> None:
        """Start the background loop + connect. Blocks until connected (or fails)."""
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run_loop, name="OutboxSender", daemon=True
        )
        self._thread.start()
        self._ready.wait()
        if self._start_error:
            raise self._start_error

    def _run_loop(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._connect())
        except BaseException as e:
            self._start_error = e
            self._ready.set()
            return
        self._ready.set()
        try:
            loop.run_forever()
        finally:
            loop.close()

    async def _connect(self) -> None:
        from telethon import TelegramClient
        api_id, api_hash = _load_env()
        self._client = TelegramClient(str(SESSION_FILE), api_id, api_hash)
        await self._client.connect()
        if not await self._client.is_user_authorized():
            raise RuntimeError(
                "Telethon session is not authorized. Run auth_setup.py."
            )

    def stop(self) -> None:
        if self._loop is None:
            return
        loop = self._loop
        async def _close():
            if self._client is not None:
                await self._client.disconnect()
        try:
            fut = asyncio.run_coroutine_threadsafe(_close(), loop)
            fut.result(timeout=5)
        except Exception:
            pass
        loop.call_soon_threadsafe(loop.stop)

    # --- Public send API ---

    def send_album(
        self,
        targets: list[tuple[str, str | int]],  # [(label, telethon_target), ...]
        image_paths: list[Path],
        caption: str = "",
    ) -> list[SendResult]:
        """Send the same album to every (label, target). Blocks until done.

        Telethon's send_file with a list automatically bundles into an album.
        The caption is attached to the first file (Telegram's native behavior).
        """
        if self._loop is None or self._client is None:
            raise RuntimeError("Sender not started.")

        async def _run() -> list[SendResult]:
            results: list[SendResult] = []
            paths_str = [str(p) for p in image_paths]

            # The client may have dropped its connection if the tray app sat
            # idle for a while (sleep/wake, network blip). Reconnect before
            # sending instead of failing with a "disconnected" error.
            try:
                if not self._client.is_connected():
                    await self._client.connect()
                if not await self._client.is_user_authorized():
                    return [
                        SendResult(label, False, "Telethon session not authorized — re-run auth_setup.py")
                        for label, _ in targets
                    ]
            except Exception as e:
                return [SendResult(label, False, f"Reconnect failed: {e}") for label, _ in targets]

            for label, target in targets:
                try:
                    await self._client.send_file(
                        target,
                        paths_str,
                        caption=caption or None,
                        album=True,
                    )
                    results.append(SendResult(target_label=label, ok=True))
                except Exception as e:
                    results.append(SendResult(target_label=label, ok=False, error=str(e)))
            return results

        fut = asyncio.run_coroutine_threadsafe(_run(), self._loop)
        return fut.result()
