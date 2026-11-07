"""Pulse Outbox — tray icon entry point.

Lives in the system tray. Left-click → open compose window. Right-click → menu.

Run from the Pulse folder with `pythonw tools/outbox/app.py` (pythonw, so no
console pops up). Run tools/outbox/auth_setup.py once first.
"""
from __future__ import annotations

import os
import sys
import threading
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # enable `from tools.outbox import ...`

from PIL import Image  # noqa: E402
import pystray  # noqa: E402

from tools.outbox.paths import ICONS_DIR, LOCK_FILE, OUTBOX_DIR, ensure_dirs  # noqa: E402
from tools.outbox.sender import Sender  # noqa: E402


# Track a single open compose window at a time, since pystray clicks
# fire fast enough that a user could double-open by accident.
_compose_lock = threading.Lock()
_compose_open = False


def _load_icon_image() -> Image.Image:
    candidates = [
        ICONS_DIR / "pulse_icon_big.png",
        ICONS_DIR / "pulse_icon_v3_preview.png",
        ICONS_DIR / "pulse_icon_preview.png",
    ]
    for c in candidates:
        if c.exists():
            try:
                return Image.open(c)
            except Exception:
                continue
    # Fallback: a tiny neutral square
    return Image.new("RGB", (64, 64), color=(70, 130, 180))


def _open_compose(icon, sender: Sender) -> None:
    global _compose_open
    with _compose_lock:
        if _compose_open:
            return
        _compose_open = True

    def _runner():
        global _compose_open
        try:
            # Late import — tkinter init happens here, in this thread
            from tools.outbox.compose import open_compose_window
            open_compose_window(sender)
        except Exception:
            traceback.print_exc()
        finally:
            with _compose_lock:
                _compose_open = False

    threading.Thread(target=_runner, name="OutboxCompose", daemon=True).start()


def _open_outbox_folder(icon) -> None:
    try:
        os.startfile(str(OUTBOX_DIR))
    except OSError:
        pass


def _quit(icon, sender: Sender) -> None:
    try:
        sender.stop()
    except Exception:
        pass
    icon.stop()


def _acquire_single_instance_lock() -> bool:
    """Best-effort single-instance lock via a sentinel file.

    On Windows we can't easily get a fully atomic OS lock without pywin32,
    but the file-existence check is good enough for a UX-only tray app.
    """
    if LOCK_FILE.exists():
        # Try to remove a stale lock — if another process has it open and
        # we somehow get permission to delete it, fine; otherwise we bail.
        try:
            LOCK_FILE.unlink()
        except OSError:
            return False
    try:
        LOCK_FILE.write_text(str(os.getpid()), encoding="utf-8")
    except OSError:
        return False
    return True


def main() -> int:
    ensure_dirs()

    if not _acquire_single_instance_lock():
        # Already running; just exit silently.
        return 0

    try:
        sender = Sender()
        try:
            sender.start()
        except Exception as e:
            # Fail loudly — show a console message and exit. The user almost
            # certainly hasn't run auth_setup.py yet.
            print(f"Outbox could not start Telethon:\n  {e}")
            print("Run tools/outbox/auth_setup.py first.")
            return 1

        image = _load_icon_image()
        icon = pystray.Icon("pulse-outbox", image, "Pulse Outbox")

        icon.menu = pystray.Menu(
            pystray.MenuItem(
                "Open Compose",
                lambda i, item: _open_compose(i, sender),
                default=True,
            ),
            pystray.MenuItem(
                "Open Outbox Folder",
                lambda i, item: _open_outbox_folder(i),
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "Quit",
                lambda i, item: _quit(i, sender),
            ),
        )

        icon.run()
        return 0
    finally:
        try:
            LOCK_FILE.unlink()
        except OSError:
            pass


def _run_with_crash_log() -> int:
    """Wrap main() so a crash under pythonw (no console) still leaves a trace.

    Without this, any startup exception just makes the window vanish with
    zero feedback. We write the full traceback to tools/outbox/error.log
    AND pop a native message box so the failure is impossible to miss.
    """
    try:
        return main()
    except SystemExit:
        raise
    except BaseException:
        tb = traceback.format_exc()
        try:
            from tools.outbox.paths import TOOL_DIR
            (TOOL_DIR / "error.log").write_text(tb, encoding="utf-8")
        except Exception:
            pass
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(
                0,
                "Pulse Outbox crashed on startup.\n\n"
                "The full error was saved to:\n"
                "tools\\outbox\\error.log\n\n"
                + tb[-600:],
                "Pulse Outbox - Crash",
                0x10,  # MB_ICONERROR
            )
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    sys.exit(_run_with_crash_log())
