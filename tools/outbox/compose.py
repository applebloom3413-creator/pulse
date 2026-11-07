"""Tkinter compose window — thumbnails, caption, recipients, send."""
from __future__ import annotations

import shutil
import time
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

from PIL import Image, ImageTk

from .groups import Group, load_groups
from .paths import (
    ICONS_DIR,
    OUTBOX_DIR,
    SENT_DIR,
    ensure_dirs,
    list_outbox_images,
)
from .personas import Persona, discover_personas
from .sender import Sender, SendResult


THUMB_SIZE = (96, 96)


class ComposeWindow:
    """One-shot compose window. Lives only as long as it's open."""

    def __init__(self, sender: Sender) -> None:
        self.sender = sender

        ensure_dirs()

        self.root = tk.Tk()
        self.root.title("Pulse Outbox")
        self.root.geometry("640x640")
        self.root.minsize(520, 560)

        # Best-effort window icon
        try:
            ico = ICONS_DIR / "pulse_icon_v3.ico"
            if ico.exists():
                self.root.iconbitmap(str(ico))
        except Exception:
            pass

        self._thumb_refs: list[ImageTk.PhotoImage] = []  # keep refs or PIL releases them
        self._image_paths: list[Path] = []
        self._persona_vars: dict[str, tk.BooleanVar] = {}
        self._group_vars: dict[str, tk.BooleanVar] = {}
        self._personas: list[Persona] = []
        self._groups: list[Group] = []

        self._build_ui()
        self._refresh_thumbs()
        self._populate_recipients()

    # ----- UI scaffolding -----

    def _build_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=10)
        outer.pack(fill="both", expand=True)

        # Header
        header = ttk.Frame(outer)
        header.pack(fill="x", pady=(0, 6))
        ttk.Label(header, text="Drop screenshots into the outbox folder, then send.",
                  font=("Segoe UI", 10)).pack(side="left")
        ttk.Button(header, text="Open outbox folder", command=self._open_outbox).pack(side="right")

        # Thumbnails
        thumb_frame = ttk.LabelFrame(outer, text="Images in outbox", padding=6)
        thumb_frame.pack(fill="x", pady=(0, 8))
        self._thumb_inner = ttk.Frame(thumb_frame)
        self._thumb_inner.pack(fill="x")
        self._empty_label = ttk.Label(
            self._thumb_inner,
            text="(none — drop pics into the outbox folder, then click Refresh)",
            foreground="#888",
        )
        ttk.Button(thumb_frame, text="Refresh", command=self._refresh_thumbs).pack(anchor="e", pady=(4, 0))

        # Caption
        cap_frame = ttk.LabelFrame(outer, text="Caption (optional, attached to first image)", padding=6)
        cap_frame.pack(fill="x", pady=(0, 8))
        self._caption = tk.Text(cap_frame, height=3, wrap="word")
        self._caption.pack(fill="x")

        # Recipients
        rec_frame = ttk.LabelFrame(outer, text="Send to", padding=6)
        rec_frame.pack(fill="both", expand=True, pady=(0, 8))
        self._recipients_inner = ttk.Frame(rec_frame)
        self._recipients_inner.pack(fill="both", expand=True, anchor="nw")

        # Bottom buttons
        btn_frame = ttk.Frame(outer)
        btn_frame.pack(fill="x")
        self._status = ttk.Label(btn_frame, text="", foreground="#555")
        self._status.pack(side="left")
        ttk.Button(btn_frame, text="Cancel", command=self.root.destroy).pack(side="right", padx=(6, 0))
        self._send_btn = ttk.Button(btn_frame, text="Send", command=self._on_send)
        self._send_btn.pack(side="right")

    # ----- Populate dynamic content -----

    def _refresh_thumbs(self) -> None:
        for child in self._thumb_inner.winfo_children():
            child.destroy()
        self._thumb_refs.clear()

        self._image_paths = list_outbox_images()
        if not self._image_paths:
            self._empty_label = ttk.Label(
                self._thumb_inner,
                text="(none — drop pics into the outbox folder, then click Refresh)",
                foreground="#888",
            )
            self._empty_label.pack(anchor="w")
            return

        for col, path in enumerate(self._image_paths):
            tile = ttk.Frame(self._thumb_inner, padding=2)
            tile.grid(row=0, column=col, padx=2)
            try:
                img = Image.open(path)
                img.thumbnail(THUMB_SIZE)
                photo = ImageTk.PhotoImage(img)
            except Exception:
                photo = None

            if photo is not None:
                self._thumb_refs.append(photo)
                ttk.Label(tile, image=photo).pack()
            else:
                ttk.Label(tile, text="?", width=8).pack()

            ttk.Label(tile, text=path.name[:14], font=("Segoe UI", 8)).pack()
            ttk.Button(
                tile, text="✕", width=2,
                command=lambda p=path: self._remove_image(p),
            ).pack(pady=(2, 0))

    def _populate_recipients(self) -> None:
        for child in self._recipients_inner.winfo_children():
            child.destroy()
        self._persona_vars.clear()
        self._group_vars.clear()

        self._personas = discover_personas()
        self._groups = load_groups()

        if not self._personas and not self._groups:
            ttk.Label(
                self._recipients_inner,
                text=(
                    "No recipients found.\n"
                    " - Companions need a saved chat_id (run /start with each bot first).\n"
                    " - For group chats, add entries to tools/outbox/groups.json."
                ),
                foreground="#a04",
                justify="left",
            ).pack(anchor="w")
            return

        if self._personas:
            ttk.Label(self._recipients_inner, text="Companions",
                      font=("Segoe UI", 9, "bold")).pack(anchor="w", pady=(0, 2))
            for p in self._personas:
                var = tk.BooleanVar(value=False)
                self._persona_vars[p.name] = var
                ttk.Checkbutton(
                    self._recipients_inner, text=p.display, variable=var,
                ).pack(anchor="w")

        if self._groups:
            ttk.Label(self._recipients_inner, text="Group chats",
                      font=("Segoe UI", 9, "bold")).pack(anchor="w", pady=(8, 2))
            for g in self._groups:
                var = tk.BooleanVar(value=False)
                self._group_vars[g.name] = var
                ttk.Checkbutton(
                    self._recipients_inner, text=g.name, variable=var,
                ).pack(anchor="w")

    # ----- Actions -----

    def _open_outbox(self) -> None:
        import os
        os.startfile(str(OUTBOX_DIR))  # Windows-only; matches the project context

    def _remove_image(self, path: Path) -> None:
        try:
            path.unlink()
        except OSError as e:
            messagebox.showerror("Remove failed", f"Could not delete {path.name}:\n{e}")
            return
        self._refresh_thumbs()

    def _selected_targets(self) -> list[tuple[str, str | int]]:
        """Return [(human label, telethon target), ...] for every checked recipient."""
        out: list[tuple[str, str | int]] = []
        for p in self._personas:
            if self._persona_vars[p.name].get():
                # Telethon accepts numeric chat_id as int. Convert when possible.
                try:
                    target: str | int = int(p.chat_id)
                except ValueError:
                    target = p.chat_id
                out.append((p.display, target))
        for g in self._groups:
            if self._group_vars[g.name].get():
                target = g.target
                # Strings that look like ints get coerced (group chat IDs are negative ints).
                if isinstance(target, str):
                    try:
                        target = int(target)
                    except ValueError:
                        pass
                out.append((g.name, target))
        return out

    def _on_send(self) -> None:
        if not self._image_paths:
            messagebox.showwarning("Nothing to send", "Drop some images into the outbox first.")
            return
        targets = self._selected_targets()
        if not targets:
            messagebox.showwarning("No recipients", "Pick at least one companion or group chat.")
            return

        # Telegram albums max out at 10.
        if len(self._image_paths) > 10:
            messagebox.showerror(
                "Too many images",
                f"Telegram albums max out at 10 images per send — you have {len(self._image_paths)}.\n"
                "Remove some and try again.",
            )
            return

        caption = self._caption.get("1.0", "end").strip()

        self._send_btn.configure(state="disabled")
        self._status.configure(text="Sending…")
        self.root.update_idletasks()

        try:
            results = self.sender.send_album(
                targets=targets,
                image_paths=list(self._image_paths),
                caption=caption,
            )
        except Exception as e:
            import traceback as _tb
            self._log_send_error(_tb.format_exc())
            self._send_btn.configure(state="normal")
            self._status.configure(text="")
            messagebox.showerror(
                "Send failed",
                f"Telethon error:\n{e}\n\n(Full details written to tools\\outbox\\error.log)",
            )
            return

        ok_results = [r for r in results if r.ok]
        bad_results = [r for r in results if not r.ok]
        if bad_results:
            self._log_send_error(
                "Send results:\n" +
                "\n".join(f"  {r.target_label}: {'OK' if r.ok else r.error}" for r in results)
            )

        if ok_results and not bad_results:
            self._archive_sent()
            self._status.configure(text=f"Sent to {len(ok_results)} recipient(s) ✓")
            messagebox.showinfo(
                "Sent",
                "Sent to: " + ", ".join(r.target_label for r in ok_results),
            )
            self.root.destroy()
            return

        if ok_results and bad_results:
            self._archive_sent()
            msg = (
                "Sent to: " + ", ".join(r.target_label for r in ok_results) + "\n\n"
                "Failed:\n" + "\n".join(f"  - {r.target_label}: {r.error}" for r in bad_results)
            )
            self._status.configure(text=f"Partial: {len(ok_results)} ok, {len(bad_results)} failed")
            messagebox.showwarning("Partial send", msg)
            return

        # All failed
        self._send_btn.configure(state="normal")
        self._status.configure(text="All sends failed")
        messagebox.showerror(
            "Send failed",
            "Every recipient failed:\n\n" +
            "\n".join(f"  - {r.target_label}: {r.error}" for r in bad_results),
        )

    def _log_send_error(self, text: str) -> None:
        """Append a timestamped send error to tools/outbox/error.log."""
        try:
            from .paths import TOOL_DIR
            stamp = time.strftime("%Y-%m-%d %H:%M:%S")
            with open(TOOL_DIR / "error.log", "a", encoding="utf-8") as f:
                f.write(f"\n=== send error @ {stamp} ===\n{text}\n")
        except Exception:
            pass

    def _archive_sent(self) -> None:
        """Move the sent images into sent/<timestamp>/ so the outbox is empty."""
        stamp = time.strftime("%Y%m%d_%H%M%S")
        dest = SENT_DIR / stamp
        dest.mkdir(parents=True, exist_ok=True)
        for p in self._image_paths:
            try:
                shutil.move(str(p), str(dest / p.name))
            except OSError:
                pass
        self._image_paths = []

    # ----- Lifecycle -----

    def run(self) -> None:
        self.root.mainloop()


def open_compose_window(sender: Sender) -> None:
    """Open a fresh compose window. Returns when the user closes it."""
    ComposeWindow(sender).run()
