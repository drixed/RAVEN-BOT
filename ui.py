from __future__ import annotations

import queue
import time
import tkinter as tk
import os
from pathlib import Path
from tkinter import messagebox, simpledialog, ttk, filedialog
from typing import Callable

import cv2
import numpy as np
from mss import mss
from PIL import Image, ImageTk
from tkinter import TclError
import ttkbootstrap as tb
from ttkbootstrap.constants import BOTH, END, LEFT, RIGHT, X, Y
import win32gui
import random
import time
import win32con
import win32ui
import win32api

from bot_logic import Bot, PointsStore, Route, RouteStep
from recorder import RecorderConfig, RouteRecorder
from window_manager import (
    bring_window_to_foreground,
    find_window_by_title,
    format_window_item,
    list_visible_windows,
    parse_window_item,
)

APP_BUILD = "2026-04-27 gate-ui"


class UI:
    def __init__(self, root: tk.Tk, store: PointsStore, bot: Bot):
        self.root = root
        self.store = store
        self.bot = bot

        self.log_queue: "queue.Queue[str]" = queue.Queue()
        self._log_fp = None
        self._log_path: Path | None = None
        self._recorder: RouteRecorder | None = None
        self._editor_steps: list[RouteStep] = []
        self._routebuilder_snapshot: Image.Image | None = None
        self._routebuilder_photo: ImageTk.PhotoImage | None = None
        self._routebuilder_scale: float = 1.0
        self._routebuilder_markers: list[int] = []  # canvas item ids for click-steps (by step index mapping in dict)
        self._routebuilder_step_to_marker: dict[int, int] = {}
        self._routebuilder_marker_to_step: dict[int, int] = {}
        self._routebuilder_drag: dict | None = None
        self._steps_menu: tk.Menu | None = None

        self.root.title(f"RAVEN BOT [{APP_BUILD}]")
        # Bigger default so left panel controls fit comfortably.
        self.root.geometry("1400x900")
        self.root.minsize(1200, 780)

        self._build()
        self._refresh_points_list()
        self._tick_logs()
        self._tick_status()

        # Open log file if enabled (night mode)
        self._log_open_if_enabled()

        # Global hotkey (pause/resume)
        self._hotkey_id_pause = 9001
        self._hotkey_id_stop_record = 9002
        self._pause_hotkey_registered = False
        self._stop_record_hotkey_registered = False
        self._register_pause_hotkey()
        self._register_stop_record_hotkey()

        # Scheduler runtime
        self._schedule_stop_ts: float | None = None

        # Mousewheel routing (scroll only when hovering a scrollable canvas)
        self._mw_canvas: tk.Canvas | None = None
        try:
            self.root.bind_all("<MouseWheel>", self._on_global_mousewheel)
        except Exception:
            pass

    def _spotlight(self, widget, times: int = 3) -> None:
        """
        Visual highlight for a UI widget (blink).
        Best-effort for ttkbootstrap widgets and some tk widgets.
        """
        if widget is None:
            return

        try:
            widget.focus_set()
        except Exception:
            pass

        # try to capture original bootstyle (ttkbootstrap)
        orig_bootstyle = None
        try:
            orig_bootstyle = widget.cget("bootstyle")
        except Exception:
            orig_bootstyle = None

        def set_bootstyle(bs: str | None) -> None:
            try:
                if bs is None:
                    widget.configure(bootstyle=orig_bootstyle)
                else:
                    widget.configure(bootstyle=bs)
            except Exception:
                pass

        def set_highlight(on: bool) -> None:
            try:
                widget.configure(highlightthickness=(2 if on else 0), highlightbackground="#f6c343")
            except Exception:
                pass

        def tick(n: int) -> None:
            if n <= 0:
                set_bootstyle(None)
                set_highlight(False)
                return
            on = (n % 2 == 0)
            if orig_bootstyle is not None:
                set_bootstyle("warning" if on else orig_bootstyle)
            else:
                set_highlight(on)
            try:
                widget.lift()
            except Exception:
                pass
            self.root.after(180, lambda: tick(n - 1))

        tick(max(2, int(times) * 2))

    def _install_paste_support(self, entry) -> None:
        """
        Ensure clipboard shortcuts work reliably (Ctrl+A/C/X/V, Shift+Insert + right-click menu),
        even when ttk widgets / focus quirks interfere.
        """
        if entry is None:
            return

        def do_paste(_evt=None):
            try:
                entry.focus_set()
            except Exception:
                pass
            try:
                entry.event_generate("<<Paste>>")
                return "break"
            except Exception:
                pass
            try:
                txt = self.root.clipboard_get()
                entry.insert("insert", txt)
            except Exception:
                pass
            return "break"

        def do_select_all(_evt=None):
            try:
                entry.focus_set()
            except Exception:
                pass
            try:
                entry.selection_range(0, "end")
                entry.icursor("end")
            except Exception:
                pass
            return "break"

        def do_copy(_evt=None):
            try:
                entry.focus_set()
            except Exception:
                pass
            try:
                if entry.selection_present():
                    s = entry.selection_get()
                    self.root.clipboard_clear()
                    self.root.clipboard_append(s)
            except Exception:
                pass
            return "break"

        def do_cut(_evt=None):
            do_copy()
            try:
                if entry.selection_present():
                    entry.delete("sel.first", "sel.last")
            except Exception:
                pass
            return "break"

        # Layout-agnostic Ctrl shortcuts (keycode works even on RU layout)
        def on_keypress(evt):
            try:
                is_ctrl = bool(int(getattr(evt, "state", 0)) & 0x0004)
            except Exception:
                is_ctrl = False
            if not is_ctrl:
                return
            kc = int(getattr(evt, "keycode", 0) or 0)
            # A=65, C=67, V=86, X=88 on Windows (layout independent)
            if kc == 65:
                return do_select_all()
            if kc == 67:
                return do_copy()
            if kc == 86:
                return do_paste()
            if kc == 88:
                return do_cut()
            return

        try:
            entry.bind("<KeyPress>", on_keypress, add="+")
            entry.bind("<Shift-Insert>", do_paste, add="+")
        except Exception:
            pass

        def on_rclick(evt):
            try:
                menu = tk.Menu(entry, tearoff=0)
                menu.add_command(label="Вставить", command=lambda: do_paste())
                menu.add_separator()
                menu.add_command(label="Выделить всё", command=lambda: do_select_all())
                menu.add_command(label="Копировать", command=lambda: do_copy())
                menu.add_command(label="Вырезать", command=lambda: do_cut())
                menu.tk_popup(int(evt.x_root), int(evt.y_root))
            except Exception:
                pass
            return "break"

        try:
            entry.bind("<Button-3>", on_rclick)
        except Exception:
            pass

    def _show_pil_popup(self, pil: Image.Image, title: str = "Preview") -> None:
        try:
            top = tk.Toplevel(self.root)
            top.title(title)

            max_w, max_h = 900, 600
            w, h = pil.size
            scale = min(1.0, max_w / max(1, w), max_h / max(1, h))
            if scale != 1.0:
                pil = pil.resize((int(w * scale), int(h * scale)), Image.Resampling.NEAREST)

            photo = ImageTk.PhotoImage(pil)
            lbl = ttk.Label(top, image=photo)
            lbl.image = photo
            lbl.pack(padx=10, pady=10)
        except Exception:
            return

    def _set_unsaved(self, flag: bool) -> None:
        try:
            if flag:
                self._unsaved_var.set("Изменения не сохранены — не забудь нажать «Сохранить».")
            else:
                self._unsaved_var.set("")
        except Exception:
            pass

    def _bind_unsaved(self, var) -> None:
        try:
            var.trace_add("write", lambda *_: self._set_unsaved(True))
        except Exception:
            pass

    def _capture_client_image_pil(self, hwnd: int) -> tuple[Image.Image, int, int] | None:
        """
        Capture the *client area* of a window as a PIL image, without other windows/overlays.
        Uses WinAPI PrintWindow best-effort. Returns (pil_img, w, h) or None if failed.
        """
        try:
            cl, ct, cr, cb = win32gui.GetClientRect(hwnd)
            w = max(1, int(cr - cl))
            h = max(1, int(cb - ct))

            # Create a compatible DC/bitmap
            hwnd_dc = win32gui.GetWindowDC(hwnd)
            src_dc = win32ui.CreateDCFromHandle(hwnd_dc)
            mem_dc = src_dc.CreateCompatibleDC()
            bmp = win32ui.CreateBitmap()
            bmp.CreateCompatibleBitmap(src_dc, w, h)
            mem_dc.SelectObject(bmp)

            # Try to render full content into our bitmap
            # Flags: 1 = PW_CLIENTONLY, 2 = PW_RENDERFULLCONTENT (newer Windows)
            flags = 1
            try:
                flags = win32con.PW_CLIENTONLY | 2  # type: ignore[attr-defined]
            except Exception:
                flags = win32con.PW_CLIENTONLY
            ok = False
            try:
                ok = bool(win32gui.PrintWindow(hwnd, mem_dc.GetSafeHdc(), int(flags)))
            except Exception:
                ok = False

            if not ok:
                # Cleanup
                try:
                    win32gui.ReleaseDC(hwnd, hwnd_dc)
                except Exception:
                    pass
                try:
                    mem_dc.DeleteDC()
                    src_dc.DeleteDC()
                except Exception:
                    pass
                try:
                    win32gui.DeleteObject(bmp.GetHandle())
                except Exception:
                    pass
                return None

            # Extract bitmap bytes (BGRA)
            bmpinfo = bmp.GetInfo()
            bits = bmp.GetBitmapBits(True)
            img = np.frombuffer(bits, dtype=np.uint8)
            img = img.reshape((bmpinfo["bmHeight"], bmpinfo["bmWidth"], 4))
            bgr = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
            rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
            pil = Image.fromarray(rgb)

            # Cleanup GDI objects
            try:
                win32gui.ReleaseDC(hwnd, hwnd_dc)
            except Exception:
                pass
            try:
                mem_dc.DeleteDC()
                src_dc.DeleteDC()
            except Exception:
                pass
            try:
                win32gui.DeleteObject(bmp.GetHandle())
            except Exception:
                pass

            return pil, w, h
        except Exception:
            return None

    def _apply_theme(self, theme: str) -> None:
        t = (theme or "").strip() or "darkly"
        try:
            self.root.style.theme_use(t)  # type: ignore[attr-defined]
        except Exception:
            # fallback to default if theme not found
            try:
                self.root.style.theme_use("darkly")  # type: ignore[attr-defined]
                t = "darkly"
            except Exception:
                pass
        self.store.ui_theme = t
        self.store.save()

    def log(self, msg: str) -> None:
        ts = time.strftime("%H:%M:%S")
        line = f"[{ts}] {msg}"
        self.log_queue.put(line)
        self._log_write(line)

    def _log_open_if_enabled(self) -> None:
        if not bool(getattr(self.store, "log_to_file_enabled", False)):
            return
        try:
            log_dir = Path(str(getattr(self.store, "log_dir", "logs") or "logs"))
            log_dir.mkdir(parents=True, exist_ok=True)
            ts = time.strftime("%Y-%m-%d_%H-%M-%S")
            self._log_path = log_dir / f"session_{ts}.log"
            self._log_fp = open(self._log_path, "a", encoding="utf-8", buffering=1)
            self._log_write(f"[{time.strftime('%H:%M:%S')}] Лог-файл открыт: {self._log_path}")
        except Exception as e:
            self._log_fp = None
            self._log_path = None
            self.log_queue.put(f"[{time.strftime('%H:%M:%S')}] LOG FILE: не удалось открыть: {e!r}")

    def _log_write(self, line: str) -> None:
        try:
            if self._log_fp is None:
                return
            self._log_fp.write(line + "\n")
        except Exception:
            pass

    def _open_log_file(self) -> None:
        try:
            if self._log_path and self._log_path.exists():
                os.startfile(str(self._log_path))  # type: ignore[attr-defined]
                return
        except Exception:
            pass
        messagebox.showinfo("Лог", "Лог-файл ещё не создан (включи «Писать лог в файл» и перезапусти/сохрани).")

    def _open_log_folder(self) -> None:
        try:
            p = None
            if self._log_path:
                p = self._log_path.parent
            else:
                p = Path(str(getattr(self.store, "log_dir", "logs") or "logs"))
            p.mkdir(parents=True, exist_ok=True)
            os.startfile(str(p.resolve()))  # type: ignore[attr-defined]
        except Exception as e:
            messagebox.showerror("Ошибка", f"Не удалось открыть папку логов: {e!r}")

    def _set_mw_canvas(self, canvas: tk.Canvas | None) -> None:
        self._mw_canvas = canvas

    def _on_global_mousewheel(self, evt) -> None:
        c = getattr(self, "_mw_canvas", None)
        if c is None:
            return
        try:
            c.yview_scroll(int(-1 * (evt.delta / 120)), "units")
        except Exception:
            pass

    def _register_pause_hotkey(self) -> None:
        """
        Register a global hotkey (works even when game window is active).
        Uses Win32 RegisterHotKey and is polled in _tick_status().
        """
        try:
            # Unregister previous if any
            try:
                win32gui.UnregisterHotKey(None, int(self._hotkey_id_pause))
            except Exception:
                pass

            key_name = str(getattr(self.store, "pause_hotkey", "f8") or "f8").strip().lower()
            vk = self._hotkey_name_to_vk(key_name)
            if vk is None:
                vk = win32con.VK_F8
                key_name = "f8"
                self.store.pause_hotkey = key_name
                self.store.save()

            win32gui.RegisterHotKey(None, int(self._hotkey_id_pause), 0, int(vk))
            self._pause_hotkey_registered = True
            self.log(f"Хоткей паузы: {key_name.upper()} (глобально)")
        except Exception as e:
            self._pause_hotkey_registered = False
            self.log(f"Хоткей паузы: не удалось зарегистрировать ({e!r})")

    def _register_stop_record_hotkey(self) -> None:
        """
        Global hotkey to stop route recording (works while game is active).
        """
        try:
            try:
                win32gui.UnregisterHotKey(None, int(self._hotkey_id_stop_record))
            except Exception:
                pass
            key_name = str(getattr(self.store, "stop_record_hotkey", "f9") or "f9").strip().lower()
            vk = self._hotkey_name_to_vk(key_name) or win32con.VK_F9
            win32gui.RegisterHotKey(None, int(self._hotkey_id_stop_record), 0, int(vk))
            self._stop_record_hotkey_registered = True
            self.log(f"Хоткей записи: {key_name.upper()} = Стоп запись")
        except Exception as e:
            self._stop_record_hotkey_registered = False
            self.log(f"Хоткей записи: не удалось зарегистрировать ({e!r})")

    def _hotkey_name_to_vk(self, name: str) -> int | None:
        n = (name or "").strip().lower()
        if not n:
            return None
        if n.startswith("f") and n[1:].isdigit():
            i = int(n[1:])
            if 1 <= i <= 24:
                return int(getattr(win32con, f"VK_F{i}"))
        mapping = {
            "pause": win32con.VK_PAUSE,
            "scrolllock": win32con.VK_SCROLL,
            "insert": win32con.VK_INSERT,
            "home": win32con.VK_HOME,
            "end": win32con.VK_END,
            "pageup": win32con.VK_PRIOR,
            "pagedown": win32con.VK_NEXT,
            "delete": win32con.VK_DELETE,
        }
        return int(mapping[n]) if n in mapping else None

    def _build(self) -> None:
        # Root grid: header + content
        self.root.grid_rowconfigure(1, weight=1)
        self.root.grid_columnconfigure(0, weight=1)

        header = tb.Frame(self.root, padding=(14, 10))
        header.grid(row=0, column=0, sticky="ew")
        header.grid_columnconfigure(1, weight=1)

        tb.Label(header, text="RAVEN BOT", font=("Segoe UI Semibold", 14)).grid(row=0, column=0, sticky="w")
        # HP in header: centered red progressbar + percent label
        hp_top = tb.Frame(header)
        hp_top.grid(row=0, column=1, sticky="ew", padx=(14, 14))
        hp_top.grid_columnconfigure(0, weight=1)
        self.hp_top_var = tk.StringVar(value="HP: ?%")
        self.hp_top_progress_var = tk.IntVar(value=0)
        self.hp_top_bar = tb.Progressbar(
            hp_top,
            maximum=100,
            variable=self.hp_top_progress_var,
            bootstyle="danger-striped",
        )
        self.hp_top_bar.grid(row=0, column=0, sticky="ew")
        self.hp_top_label = tb.Label(hp_top, textvariable=self.hp_top_var, bootstyle="secondary")
        self.hp_top_label.grid(row=0, column=1, padx=(10, 0), sticky="e")
        self.status_var = tk.StringVar(value="Статус: остановлен")
        self.status_label = tb.Label(header, textvariable=self.status_var, bootstyle="danger")
        self.status_label.grid(row=0, column=2, sticky="e")

        # Mini overlay toggle
        self._mini_win: tk.Toplevel | None = None
        self._mini_status_var = tk.StringVar(value="Бот: остановлен")
        self._mini_game_var = tk.StringVar(value="Окно игры: ?")
        self._mini_action_var = tk.StringVar(value="Действие: ?")
        self._mini_detect_var = tk.StringVar(value="Детект: ?")
        self._mini_toast_var = tk.StringVar(value="")
        tb.Button(header, text="Мини", bootstyle="secondary-outline", command=self._toggle_mini).grid(
            row=0, column=3, padx=(12, 0), sticky="e"
        )

        content = tb.Panedwindow(self.root, orient="horizontal")
        content.grid(row=1, column=0, sticky="nsew", padx=12, pady=(0, 12))

        # Left: window + routes
        left = tb.Frame(content, padding=10)
        content.add(left, weight=1)
        left.grid_rowconfigure(0, weight=1)
        left.grid_columnconfigure(0, weight=1)

        # Scrollable left panel (so controls fit on small windows)
        left_canvas = tk.Canvas(left, highlightthickness=0)
        left_scroll = tb.Scrollbar(left, orient="vertical", command=left_canvas.yview)
        left_canvas.configure(yscrollcommand=left_scroll.set)
        left_canvas.grid(row=0, column=0, sticky="nsew")
        left_scroll.grid(row=0, column=1, sticky="ns")

        left_inner = tb.Frame(left_canvas)
        left_inner_id = left_canvas.create_window((0, 0), window=left_inner, anchor="nw")

        def _sync_left_width(_evt=None) -> None:
            try:
                left_canvas.itemconfigure(left_inner_id, width=left_canvas.winfo_width())
            except Exception:
                pass

        def _sync_left_scroll(_evt=None) -> None:
            try:
                left_canvas.configure(scrollregion=left_canvas.bbox("all"))
            except Exception:
                pass

        left_canvas.bind("<Configure>", _sync_left_width)
        left_inner.bind("<Configure>", _sync_left_scroll)

        # Route mousewheel to this canvas only when hovered
        left_canvas.bind("<Enter>", lambda _e: self._set_mw_canvas(left_canvas))
        left_canvas.bind("<Leave>", lambda _e: self._set_mw_canvas(None))

        left_box = tb.Labelframe(left_inner, text="Окно и маршрут", padding=12, bootstyle="secondary")
        left_box.grid(row=0, column=0, sticky="nsew")
        left_box.grid_columnconfigure(0, weight=1)

        top_links = tb.Frame(left_box)
        top_links.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        top_links.grid_columnconfigure(1, weight=1)
        tb.Button(
            top_links,
            text="Открыть полную инструкцию",
            bootstyle="info-outline",
            command=self._open_route_guide,
        ).grid(row=0, column=0, sticky="w")
        self._wiz_toggle_btn = tb.Button(
            top_links,
            text="Скрыть мастер",
            bootstyle="secondary-outline",
            command=self._toggle_route_wizard,
        )
        self._wiz_toggle_btn.grid(row=0, column=2, sticky="e")

        # --- Inline route wizard (visual guidance inside this block) ---
        self._route_wizard_step = 0
        self._route_wizard_title_var = tk.StringVar(value="Мастер: шаг 1/5")
        self._route_wizard_body_var = tk.StringVar(value="")

        wiz = tb.Labelframe(left_box, text="Мастер маршрута (по UI)", padding=10, bootstyle="secondary")
        wiz.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        wiz.grid_columnconfigure(0, weight=1)
        self._wiz_frame = wiz
        self._wiz_visible = True

        tb.Label(wiz, textvariable=self._route_wizard_title_var, font=("Segoe UI Semibold", 10)).grid(
            row=0, column=0, sticky="w"
        )
        tb.Label(
            wiz,
            textvariable=self._route_wizard_body_var,
            justify="left",
            wraplength=520,
            bootstyle="secondary",
        ).grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 0))

        wiz_btns = tb.Frame(wiz)
        wiz_btns.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        wiz_btns.grid_columnconfigure(3, weight=1)
        self._wiz_back_btn = tb.Button(wiz_btns, text="Назад", bootstyle="secondary", command=self._route_wizard_back)
        self._wiz_back_btn.grid(row=0, column=0)
        self._wiz_next_btn = tb.Button(wiz_btns, text="Далее", bootstyle="primary", command=self._route_wizard_next)
        self._wiz_next_btn.grid(row=0, column=1, padx=(10, 0))
        self._wiz_hint_btn = tb.Button(wiz_btns, text="Подсветить", bootstyle="warning-outline", command=self._route_wizard_hint)
        self._wiz_hint_btn.grid(row=0, column=2, padx=(10, 0))
        # toggle button lives in top_links so it remains visible even when wizard is hidden

        tb.Label(left_box, text="Окно игры", bootstyle="secondary").grid(row=2, column=0, sticky="w")
        initial = self.store.window_title or ""
        if getattr(self.store, "window_hwnd", 0):
            initial = format_window_item(int(self.store.window_hwnd), self.store.window_title or initial)
        self.window_title_var = tk.StringVar(value=initial)
        self.windows_combo = tb.Combobox(left_box, textvariable=self.window_title_var, state="readonly")
        self.windows_combo.grid(row=3, column=0, sticky="ew", pady=(6, 10))
        self.windows_combo.bind("<<ComboboxSelected>>", lambda _e: self._on_confirm_window())

        win_btn_row = tb.Frame(left_box)
        win_btn_row.grid(row=4, column=0, sticky="ew", pady=(0, 12))
        win_btn_row.grid_columnconfigure(0, weight=1)
        self.refresh_windows_btn = tb.Button(win_btn_row, text="Обновить", command=self._on_refresh_windows, bootstyle="outline")
        self.refresh_windows_btn.grid(row=0, column=0, sticky="ew")
        self.pick_active_btn = tb.Button(win_btn_row, text="Взять активное", command=self._on_pick_active_window, bootstyle="outline")
        self.pick_active_btn.grid(row=0, column=1, padx=(10, 0))
        self.confirm_window_btn = tb.Button(win_btn_row, text="Подтвердить", command=self._on_confirm_window, bootstyle="primary")
        self.confirm_window_btn.grid(row=0, column=2, padx=(10, 0))

        tb.Separator(left_box).grid(row=5, column=0, sticky="ew", pady=10)

        # Profile selector (locations)
        tb.Label(left_box, text="Профиль (локация)", bootstyle="secondary").grid(row=6, column=0, sticky="w")
        self.profile_var = tk.StringVar(value=str(getattr(self.store, "active_profile_name", "default") or "default"))
        self.profile_combo = tb.Combobox(left_box, textvariable=self.profile_var, state="readonly")
        self.profile_combo.grid(row=7, column=0, sticky="ew", pady=(6, 10))
        self.profile_combo.bind("<<ComboboxSelected>>", self._on_select_profile)

        prof_btn_row = tb.Frame(left_box)
        prof_btn_row.grid(row=8, column=0, sticky="ew", pady=(0, 10))
        prof_btn_row.grid_columnconfigure(0, weight=1)
        self.new_profile_btn = tb.Button(prof_btn_row, text="Новый профиль", bootstyle="outline", command=self._on_new_profile)
        self.new_profile_btn.grid(
            row=0, column=0, sticky="ew"
        )
        self.delete_profile_btn = tb.Button(prof_btn_row, text="Удалить", bootstyle="danger-outline", command=self._on_delete_profile)
        self.delete_profile_btn.grid(
            row=0, column=1, padx=(10, 0)
        )

        tb.Separator(left_box).grid(row=9, column=0, sticky="ew", pady=10)

        tb.Label(left_box, text="Setup маршрут (вход в город)", bootstyle="secondary").grid(row=10, column=0, sticky="w")
        self.setup_route_var = tk.StringVar(value="")
        self.setup_routes_combo = tb.Combobox(left_box, textvariable=self.setup_route_var, state="readonly")
        self.setup_routes_combo.grid(row=11, column=0, sticky="ew", pady=(6, 10))
        self.setup_routes_combo.bind("<<ComboboxSelected>>", self._on_select_setup_route)

        tb.Label(left_box, text="Farm маршрут (по карте)", bootstyle="secondary").grid(row=12, column=0, sticky="w")
        self.active_route_var = tk.StringVar(value="")
        self.routes_combo = tb.Combobox(left_box, textvariable=self.active_route_var, state="readonly")
        self.routes_combo.grid(row=13, column=0, sticky="ew", pady=(6, 10))
        self.routes_combo.bind("<<ComboboxSelected>>", self._on_select_active_route)

        route_row = tb.Frame(left_box)
        route_row.grid(row=14, column=0, sticky="ew", pady=(0, 10))
        route_row.grid_columnconfigure(0, weight=1)
        self.new_route_btn = tb.Button(route_row, text="Новый", command=self._on_new_route, bootstyle="outline")
        self.new_route_btn.grid(
            row=0, column=0, sticky="ew"
        )
        self.delete_route_btn = tb.Button(route_row, text="Удалить", command=self._on_delete_route, bootstyle="danger-outline")
        self.delete_route_btn.grid(
            row=0, column=1, padx=(10, 0)
        )

        tb.Label(left_box, text="Имя маршрута", bootstyle="secondary").grid(row=15, column=0, sticky="w")
        self.edit_route_name_var = tk.StringVar(value="")
        tb.Entry(left_box, textvariable=self.edit_route_name_var).grid(row=16, column=0, sticky="ew", pady=(6, 10))

        tb.Label(left_box, text="Шаги", bootstyle="secondary").grid(row=17, column=0, sticky="w")
        steps_frame = tb.Frame(left_box)
        steps_frame.grid(row=18, column=0, sticky="nsew", pady=(6, 10))
        steps_frame.grid_rowconfigure(0, weight=1)
        steps_frame.grid_columnconfigure(0, weight=1)
        self.steps_list = tk.Listbox(steps_frame, height=10)
        self.steps_list.grid(row=0, column=0, sticky="nsew")
        self.steps_list.bind("<Button-3>", lambda e: self._on_steps_right_click(e, self.steps_list))
        self.steps_list.bind("<Double-1>", lambda e: self._on_steps_double_click(e, self.steps_list))
        self.steps_list.bind("<ButtonPress-1>", lambda e: self._on_steps_drag_start(e, self.steps_list))
        self.steps_list.bind("<B1-Motion>", lambda e: self._on_steps_drag_move(e, self.steps_list))
        self.steps_list.bind("<ButtonRelease-1>", lambda e: self._on_steps_drag_end(e, self.steps_list))
        steps_scroll = tb.Scrollbar(steps_frame, orient="vertical", command=self.steps_list.yview)
        steps_scroll.grid(row=0, column=1, sticky="ns")
        self.steps_list.configure(yscrollcommand=steps_scroll.set)

        record_row = tb.Frame(left_box)
        record_row.grid(row=19, column=0, sticky="ew", pady=(0, 8))
        record_row.grid_columnconfigure(0, weight=1)
        self.record_btn = tb.Button(record_row, text="Старт запись", command=self._on_start_recording, bootstyle="primary")
        self.record_btn.grid(row=0, column=0, sticky="ew")
        self.stop_record_btn = tb.Button(record_row, text="Стоп запись", command=self._on_stop_recording, state="disabled")
        self.stop_record_btn.grid(row=0, column=1, padx=(10, 0))

        # Control point during recording: insert WAIT marker
        rec_wait_row = tb.Frame(left_box)
        rec_wait_row.grid(row=20, column=0, sticky="ew", pady=(0, 10))
        rec_wait_row.grid_columnconfigure(2, weight=1)
        tb.Label(rec_wait_row, text="Контрольная пауза (сек)", bootstyle="secondary").grid(row=0, column=0, sticky="w")
        self.record_wait_s_var = tk.DoubleVar(value=1.0)
        tb.Spinbox(rec_wait_row, from_=0.1, to=60.0, increment=0.1, textvariable=self.record_wait_s_var, width=8).grid(
            row=0, column=1, padx=(10, 0), sticky="w"
        )
        self.record_insert_wait_btn = tb.Button(
            rec_wait_row,
            text="Вставить WAIT",
            bootstyle="outline",
            command=self._on_record_insert_wait,
            state="disabled",
        )
        self.record_insert_wait_btn.grid(row=0, column=2, padx=(10, 0), sticky="w")

        # Control point during recording: insert CONFIRM marker (wait popup -> press Y)
        rec_confirm_row = tb.Frame(left_box)
        rec_confirm_row.grid(row=21, column=0, sticky="ew", pady=(0, 10))
        rec_confirm_row.grid_columnconfigure(2, weight=1)
        tb.Label(rec_confirm_row, text="CONFIRM timeout (сек)", bootstyle="secondary").grid(row=0, column=0, sticky="w")
        self.record_confirm_timeout_var = tk.DoubleVar(value=6.0)
        tb.Spinbox(rec_confirm_row, from_=1.0, to=30.0, increment=0.5, textvariable=self.record_confirm_timeout_var, width=8).grid(
            row=0, column=1, padx=(10, 0), sticky="w"
        )
        self.record_insert_confirm_btn = tb.Button(
            rec_confirm_row,
            text="Вставить CONFIRM",
            bootstyle="outline",
            command=self._on_record_insert_confirm,
            state="disabled",
        )
        self.record_insert_confirm_btn.grid(row=0, column=2, padx=(10, 0), sticky="w")

        steps_btn_row2 = tb.Frame(left_box)
        steps_btn_row2.grid(row=22, column=0, sticky="ew", pady=(0, 10))
        steps_btn_row2.grid_columnconfigure(0, weight=1)
        tb.Button(steps_btn_row2, text="Удалить шаг", command=self._on_remove_step, bootstyle="outline").grid(
            row=0, column=0, sticky="ew"
        )
        tb.Button(steps_btn_row2, text="Очистить", command=self._on_clear_steps, bootstyle="outline").grid(
            row=0, column=1, padx=(10, 0)
        )

        self.save_route_btn = tb.Button(left_box, text="Сохранить маршрут", command=self._on_save_route, bootstyle="primary")
        self.save_route_btn.grid(row=23, column=0, sticky="ew")

        # Right: tabs
        right = tb.Frame(content, padding=10)
        content.add(right, weight=2)
        right.grid_rowconfigure(0, weight=1)
        right.grid_columnconfigure(0, weight=1)

        self.tabs = tb.Notebook(right, bootstyle="primary")
        self.tabs.grid(row=0, column=0, sticky="nsew")

        self.tab_settings = tb.Frame(self.tabs, padding=12)
        self.tab_detect = tb.Frame(self.tabs, padding=12)
        self.tab_autobuy = tb.Frame(self.tabs, padding=12)
        self.tab_route = tb.Frame(self.tabs, padding=12)
        self.tabs.add(self.tab_settings, text="Настройки")
        self.tabs.add(self.tab_detect, text="Детект")
        self.tabs.add(self.tab_autobuy, text="Авто банки")
        self.tabs.add(self.tab_route, text="Маршрут")

        # Settings tab: scroll the whole tab (Основное + Логи + Запуск)
        self.tab_settings.grid_columnconfigure(0, weight=1)
        self.tab_settings.grid_rowconfigure(0, weight=1)

        tab_settings_canvas = tk.Canvas(self.tab_settings, highlightthickness=0)
        tab_settings_scroll = tb.Scrollbar(self.tab_settings, orient="vertical", command=tab_settings_canvas.yview)
        tab_settings_canvas.configure(yscrollcommand=tab_settings_scroll.set)
        tab_settings_canvas.grid(row=0, column=0, sticky="nsew")
        tab_settings_scroll.grid(row=0, column=1, sticky="ns")

        tab_settings_inner = tb.Frame(tab_settings_canvas)
        tab_settings_inner_id = tab_settings_canvas.create_window((0, 0), window=tab_settings_inner, anchor="nw")
        tab_settings_inner.grid_columnconfigure(0, weight=1)

        def _sync_tab_settings_width(_evt=None) -> None:
            try:
                tab_settings_canvas.itemconfigure(tab_settings_inner_id, width=tab_settings_canvas.winfo_width())
            except Exception:
                pass

        def _sync_tab_settings_scroll(_evt=None) -> None:
            try:
                tab_settings_canvas.configure(scrollregion=tab_settings_canvas.bbox("all"))
            except Exception:
                pass

        tab_settings_canvas.bind("<Configure>", _sync_tab_settings_width)
        tab_settings_inner.bind("<Configure>", _sync_tab_settings_scroll)

        tab_settings_canvas.bind("<Enter>", lambda _e: self._set_mw_canvas(tab_settings_canvas))
        tab_settings_canvas.bind("<Leave>", lambda _e: self._set_mw_canvas(None))

        settings = tb.Labelframe(tab_settings_inner, text="Основное", padding=12, bootstyle="secondary")
        settings.grid(row=0, column=0, sticky="ew")

        # Unsaved warning banner (shown when settings changed)
        self._unsaved_var = tk.StringVar(value="")
        self.unsaved_label = tb.Label(settings, textvariable=self._unsaved_var, bootstyle="warning")
        self.unsaved_label.grid(row=98, column=0, sticky="ew", pady=(10, 0))

        # Theme
        row0 = tb.Frame(settings)
        row0.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        tb.Label(row0, text="Тема", bootstyle="secondary").grid(row=0, column=0, sticky="w")
        themes = []
        try:
            themes = list(self.root.style.theme_names())  # type: ignore[attr-defined]
        except Exception:
            themes = ["darkly", "flatly"]
        # pick only a small curated set if available
        curated = [t for t in ["darkly", "cyborg", "superhero", "flatly", "journal", "litera", "minty", "sandstone"] if t in themes]
        if not curated:
            curated = themes
        self.theme_var = tk.StringVar(value=str(getattr(self.store, "ui_theme", "darkly") or "darkly"))
        theme_combo = tb.Combobox(row0, textvariable=self.theme_var, state="readonly", values=curated, width=18)
        theme_combo.grid(row=0, column=1, padx=(10, 0), sticky="w")
        tb.Button(row0, text="Применить", bootstyle="outline", command=lambda: self._apply_theme(self.theme_var.get())).grid(
            row=0, column=2, padx=(10, 0)
        )

        # File logging (night mode)
        row0b = tb.Frame(settings)
        row0b.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        tb.Label(row0b, text="Лог в файл", bootstyle="secondary").grid(row=0, column=0, sticky="w")
        self.log_to_file_enabled_var = tk.BooleanVar(value=bool(getattr(self.store, "log_to_file_enabled", False)))
        tb.Checkbutton(
            row0b,
            text="Писать лог в файл",
            variable=self.log_to_file_enabled_var,
            bootstyle="round-toggle",
        ).grid(row=0, column=1, padx=(10, 0), sticky="w")
        tb.Label(row0b, text="Папка", bootstyle="secondary").grid(row=0, column=2, padx=(18, 0), sticky="w")
        self.log_dir_var = tk.StringVar(value=str(getattr(self.store, "log_dir", "logs") or "logs"))
        tb.Entry(row0b, textvariable=self.log_dir_var, width=18).grid(row=0, column=3, padx=(10, 0), sticky="w")
        tb.Button(row0b, text="Открыть лог", bootstyle="outline", command=self._open_log_file).grid(row=0, column=4, padx=(10, 0))
        tb.Button(row0b, text="Папка логов", bootstyle="outline", command=self._open_log_folder).grid(row=0, column=5, padx=(10, 0))

        # Hotkey pause/resume (preset list only)
        row0c = tb.Frame(settings)
        row0c.grid(row=2, column=0, sticky="ew", pady=(0, 10))
        tb.Label(row0c, text="Хоткей", bootstyle="secondary").grid(row=0, column=0, sticky="w")
        tb.Label(row0c, text="Пауза/Продолжить", bootstyle="secondary").grid(row=0, column=1, padx=(10, 0), sticky="w")
        hotkeys = [f"f{i}" for i in range(6, 13)] + ["pause", "scrolllock", "insert", "home", "end", "pageup", "pagedown", "delete"]
        self.pause_hotkey_var = tk.StringVar(value=str(getattr(self.store, "pause_hotkey", "f8") or "f8").strip().lower())
        tb.Combobox(row0c, textvariable=self.pause_hotkey_var, state="readonly", values=hotkeys, width=12).grid(
            row=0, column=2, padx=(10, 0), sticky="w"
        )
        tb.Label(row0c, text="Стоп запись", bootstyle="secondary").grid(row=1, column=1, padx=(10, 0), sticky="w", pady=(6, 0))
        self.stop_record_hotkey_var = tk.StringVar(
            value=str(getattr(self.store, "stop_record_hotkey", "f9") or "f9").strip().lower()
        )
        tb.Combobox(row0c, textvariable=self.stop_record_hotkey_var, state="readonly", values=hotkeys, width=12).grid(
            row=1, column=2, padx=(10, 0), sticky="w", pady=(6, 0)
        )
        tb.Button(row0c, text="Применить хоткей", bootstyle="outline", command=self._on_apply_pause_hotkey).grid(
            row=0, column=3, padx=(10, 0)
        )
        tb.Button(row0c, text="Применить (стоп запись)", bootstyle="outline", command=self._on_apply_stop_record_hotkey).grid(
            row=1, column=3, padx=(10, 0), pady=(6, 0)
        )

        # Scheduler
        row0d = tb.Frame(settings)
        row0d.grid(row=3, column=0, sticky="ew", pady=(0, 10))
        tb.Label(row0d, text="Планировщик", bootstyle="secondary").grid(row=0, column=0, sticky="w")
        self.schedule_enabled_var = tk.BooleanVar(value=bool(getattr(self.store, "schedule_enabled", False)))
        tb.Checkbutton(row0d, text="Включить", variable=self.schedule_enabled_var, bootstyle="round-toggle").grid(
            row=0, column=1, padx=(10, 0), sticky="w"
        )
        tb.Label(row0d, text="Старт (HH:MM)", bootstyle="secondary").grid(row=0, column=2, padx=(18, 0), sticky="w")
        self.schedule_start_var = tk.StringVar(value=str(getattr(self.store, "schedule_start_hhmm", "02:00") or "02:00"))
        tb.Entry(row0d, textvariable=self.schedule_start_var, width=8).grid(row=0, column=3, padx=(10, 0), sticky="w")
        tb.Label(row0d, text="Длительность (ч)", bootstyle="secondary").grid(row=0, column=4, padx=(18, 0), sticky="w")
        self.schedule_duration_var = tk.DoubleVar(value=float(getattr(self.store, "schedule_duration_h", 8.0)))
        tb.Spinbox(row0d, from_=0.5, to=24.0, increment=0.5, textvariable=self.schedule_duration_var, width=8).grid(
            row=0, column=5, padx=(10, 0), sticky="w"
        )
        tb.Label(row0d, text="Режим", bootstyle="secondary").grid(row=0, column=6, padx=(18, 0), sticky="w")
        self.schedule_mode_var = tk.StringVar(value=str(getattr(self.store, "schedule_mode", "route") or "route"))
        tb.Combobox(row0d, textvariable=self.schedule_mode_var, state="readonly", values=["route", "afk"], width=8).grid(
            row=0, column=7, padx=(10, 0), sticky="w"
        )

        # Teleport wait
        row1 = tb.Frame(settings)
        row1.grid(row=4, column=0, sticky="ew")
        row1.grid_columnconfigure(3, weight=1)
        tb.Label(row1, text="Ожидание после телепорта (сек) от/до", bootstyle="secondary").grid(row=0, column=0, sticky="w")
        self.wait_from_var = tk.IntVar(value=int(getattr(self.store, "teleport_wait_min_s", getattr(self.store, "teleport_wait_s", 60))))
        self.wait_to_var = tk.IntVar(value=int(getattr(self.store, "teleport_wait_max_s", getattr(self.store, "teleport_wait_s", 60))))
        tb.Spinbox(row1, from_=0, to=3600, textvariable=self.wait_from_var, width=7).grid(row=0, column=1, padx=(10, 4))
        tb.Spinbox(row1, from_=0, to=3600, textvariable=self.wait_to_var, width=7).grid(row=0, column=2, padx=(4, 0))
        tb.Label(row1, text="Удержание (сек)", bootstyle="secondary").grid(row=0, column=3, padx=(18, 0), sticky="w")
        self.key_hold_var = tk.DoubleVar(value=float(getattr(self.store, "key_hold_s", 0.06)))
        tb.Spinbox(row1, from_=0.01, to=0.30, increment=0.01, textvariable=self.key_hold_var, width=8).grid(
            row=0, column=4, padx=(10, 0)
        )
        # Delay between route steps (moved to next line to avoid overflow)
        row1b = tb.Frame(settings)
        row1b.grid(row=5, column=0, sticky="ew", pady=(6, 0))
        tb.Label(row1b, text="Delay шагов от/до (сек)", bootstyle="secondary").grid(row=0, column=0, sticky="w")
        self.route_delay_from_var = tk.DoubleVar(value=float(getattr(self.store, "route_delay_min_s", 0.0)))
        self.route_delay_to_var = tk.DoubleVar(value=float(getattr(self.store, "route_delay_max_s", 0.0)))
        tb.Spinbox(row1b, from_=0.0, to=10.0, increment=0.01, textvariable=self.route_delay_from_var, width=7).grid(
            row=0, column=1, padx=(10, 4)
        )
        tb.Spinbox(row1b, from_=0.0, to=10.0, increment=0.01, textvariable=self.route_delay_to_var, width=7).grid(
            row=0, column=2, padx=(4, 0)
        )

        # Teleport key
        row2 = tb.Frame(settings)
        row2.grid(row=6, column=0, sticky="ew", pady=(10, 0))
        tb.Label(row2, text="Клавиша телепорта", bootstyle="secondary").grid(row=0, column=0, sticky="w")
        self.tp_key_var = tk.StringVar(value=str(getattr(self.store, "teleport_key", "f4")))
        common_keys = (
            ["space", "tab", "enter", "esc"]
            + [f"f{i}" for i in range(1, 13)]
            + [str(i) for i in range(0, 10)]
            + list("qwertyuiopasdfghjklzxcvbnm")
        )
        tb.Combobox(
            row2,
            textvariable=self.tp_key_var,
            state="normal",
            values=common_keys,
            width=12,
        ).grid(row=0, column=1, padx=(10, 0), sticky="w")
        self.tp_on_enemy_enabled_var = tk.BooleanVar(value=bool(getattr(self.store, "tp_on_enemy_enabled", True)))
        tb.Checkbutton(
            row2,
            text="ТП по врагу",
            variable=self.tp_on_enemy_enabled_var,
            bootstyle="round-toggle",
            command=self._on_tp_toggle_changed,
        ).grid(row=0, column=2, padx=(18, 0))
        self.radar_detect_enabled_var = tk.BooleanVar(value=bool(getattr(self.store, "radar_detect_enabled", True)))
        tb.Checkbutton(
            row2,
            text="Детект радара",
            variable=self.radar_detect_enabled_var,
            bootstyle="round-toggle",
            command=self._on_radar_detect_toggle_changed,
        ).grid(row=0, column=4, padx=(18, 0))

        # Make TP focus toggle always visible (user often doesn't scroll).
        self.tp_focus_steal_enabled_var = tk.BooleanVar(value=bool(getattr(self.store, "tp_focus_steal_enabled", True)))
        tb.Checkbutton(
            row2,
            text="Фокус для ТП",
            variable=self.tp_focus_steal_enabled_var,
            bootstyle="round-toggle",
            command=self._on_tp_focus_toggle_changed,
        ).grid(row=0, column=6, padx=(18, 0))

        # Enemy sound alert
        # Place on its own row below teleport controls to avoid overlap.
        row2_sound = tb.Frame(settings)
        row2_sound.grid(row=7, column=0, sticky="ew", pady=(10, 0))
        row2_sound.grid_columnconfigure(6, weight=1)
        tb.Label(row2_sound, text="Сигнал при враге", bootstyle="secondary").grid(row=0, column=0, sticky="w")
        self.enemy_alert_enabled_var = tk.BooleanVar(value=bool(getattr(self.store, "enemy_alert_enabled", False)))
        tb.Checkbutton(
            row2_sound,
            text="Звук",
            variable=self.enemy_alert_enabled_var,
            bootstyle="round-toggle",
        ).grid(row=0, column=1, padx=(10, 0), sticky="w")
        tb.Label(row2_sound, text="Раз", bootstyle="secondary").grid(row=0, column=2, padx=(18, 0), sticky="w")
        self.enemy_alert_beeps_var = tk.IntVar(value=int(getattr(self.store, "enemy_alert_beeps", 2)))
        tb.Spinbox(row2_sound, from_=1, to=10, textvariable=self.enemy_alert_beeps_var, width=6).grid(
            row=0, column=3, padx=(10, 0)
        )
        tb.Label(row2_sound, text="Интервал (сек)", bootstyle="secondary").grid(row=0, column=4, padx=(18, 0), sticky="w")
        self.enemy_alert_interval_var = tk.DoubleVar(value=float(getattr(self.store, "enemy_alert_interval_s", 8.0)))
        tb.Spinbox(row2_sound, from_=0.5, to=60.0, increment=0.5, textvariable=self.enemy_alert_interval_var, width=8).grid(
            row=0, column=5, padx=(10, 0)
        )
        tb.Label(row2_sound, text="WAV", bootstyle="secondary").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.enemy_alert_sound_path_var = tk.StringVar(value=str(getattr(self.store, "enemy_alert_sound_path", "") or ""))
        tb.Entry(row2_sound, textvariable=self.enemy_alert_sound_path_var, width=42).grid(
            row=1, column=1, columnspan=5, padx=(10, 0), pady=(6, 0), sticky="ew"
        )
        tb.Button(
            row2_sound,
            text="Выбрать…",
            bootstyle="outline",
            command=self._on_pick_enemy_alert_wav,
        ).grid(row=1, column=6, padx=(10, 0), pady=(6, 0), sticky="w")

        # Telegram (send radar screenshot when attacked) — compact layout (fits on small widths)
        tg_box = tb.Labelframe(settings, text="Telegram (скрин радара при атаке)", padding=10, bootstyle="secondary")
        tg_box.grid(row=8, column=0, sticky="ew", pady=(10, 0))
        tg_box.grid_columnconfigure(1, weight=1)

        self.telegram_enabled_var = tk.BooleanVar(value=bool(getattr(self.store, "telegram_enabled", False)))
        self.telegram_interval_var = tk.DoubleVar(value=float(getattr(self.store, "telegram_interval_s", 30.0)))
        self.telegram_chat_id_var = tk.StringVar(value=str(getattr(self.store, "telegram_chat_id", "") or ""))
        self.telegram_token_var = tk.StringVar(value=str(getattr(self.store, "telegram_bot_token", "") or ""))

        row_tg1 = tb.Frame(tg_box)
        row_tg1.grid(row=0, column=0, columnspan=2, sticky="ew")
        tb.Checkbutton(
            row_tg1,
            text="Отправлять скрин при атаке",
            variable=self.telegram_enabled_var,
            bootstyle="round-toggle",
        ).pack(side="left")
        tb.Label(row_tg1, text="Интервал (сек)", bootstyle="secondary").pack(side="left", padx=(14, 0))
        tb.Spinbox(row_tg1, from_=5.0, to=3600.0, increment=1.0, textvariable=self.telegram_interval_var, width=8).pack(
            side="left", padx=(8, 0)
        )
        tb.Button(row_tg1, text="Тест", bootstyle="outline", command=self._on_test_telegram_send).pack(
            side="right"
        )

        row_tg2 = tb.Frame(tg_box)
        row_tg2.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        row_tg2.grid_columnconfigure(1, weight=0)
        tb.Label(row_tg2, text="Chat ID", bootstyle="secondary").grid(row=0, column=0, sticky="w")
        self.telegram_chat_id_entry = tb.Entry(row_tg2, textvariable=self.telegram_chat_id_var, width=18)
        self.telegram_chat_id_entry.grid(row=0, column=1, padx=(10, 0), sticky="w")
        self._install_paste_support(self.telegram_chat_id_entry)

        row_tg3 = tb.Frame(tg_box)
        row_tg3.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        row_tg3.grid_columnconfigure(1, weight=0)
        tb.Label(row_tg3, text="Bot Token", bootstyle="secondary").grid(row=0, column=0, sticky="w")
        self.telegram_token_entry = tb.Entry(row_tg3, textvariable=self.telegram_token_var, show="•", width=32)
        self.telegram_token_entry.grid(row=0, column=1, padx=(10, 0), sticky="w")
        self._install_paste_support(self.telegram_token_entry)

        row2b = tb.Frame(settings)
        row2b.grid(row=9, column=0, sticky="ew", pady=(10, 0))
        tb.Label(row2b, text="После ТП", bootstyle="secondary").grid(row=0, column=0, sticky="w")
        self.post_tp_action_var = tk.StringVar(value=str(getattr(self.store, "post_tp_action", "press_r")))
        tb.Combobox(
            row2b,
            textvariable=self.post_tp_action_var,
            state="readonly",
            width=14,
            values=["none", "press_r"],
        ).grid(row=0, column=1, padx=(10, 0))
        tb.Label(row2b, text="Клавиша", bootstyle="secondary").grid(row=0, column=2, padx=(18, 0), sticky="w")
        self.post_tp_key_var = tk.StringVar(value=str(getattr(self.store, "post_tp_key", "r")))
        tb.Entry(row2b, textvariable=self.post_tp_key_var, width=6).grid(row=0, column=3, padx=(10, 0))
        tb.Label(row2b, text="Задержка (сек)", bootstyle="secondary").grid(row=0, column=4, padx=(18, 0), sticky="w")
        self.post_tp_delay_var = tk.DoubleVar(value=float(getattr(self.store, "post_tp_delay_s", 0.25)))
        tb.Spinbox(row2b, from_=0.0, to=3.0, increment=0.05, textvariable=self.post_tp_delay_var, width=8).grid(
            row=0, column=5, padx=(10, 0)
        )

        row2c = tb.Frame(settings)
        row2c.grid(row=10, column=0, sticky="ew", pady=(10, 0))
        self.auto_confirm_enabled_var = tk.BooleanVar(value=bool(getattr(self.store, "auto_confirm_enabled", True)))
        tb.Checkbutton(row2c, text="Авто-подтверждать окна", variable=self.auto_confirm_enabled_var, bootstyle="round-toggle").grid(
            row=0, column=0, sticky="w"
        )
        tb.Label(row2c, text="Клавиша", bootstyle="secondary").grid(row=0, column=1, padx=(18, 0), sticky="w")
        self.auto_confirm_key_var = tk.StringVar(value=str(getattr(self.store, "auto_confirm_key", "y")))
        tb.Entry(row2c, textvariable=self.auto_confirm_key_var, width=6).grid(row=0, column=2, padx=(10, 0))
        tb.Label(row2c, text="Интервал (сек)", bootstyle="secondary").grid(row=0, column=3, padx=(18, 0), sticky="w")
        self.auto_confirm_interval_var = tk.DoubleVar(value=float(getattr(self.store, "auto_confirm_interval_s", 0.8)))
        tb.Spinbox(row2c, from_=0.2, to=5.0, increment=0.1, textvariable=self.auto_confirm_interval_var, width=8).grid(
            row=0, column=4, padx=(10, 0)
        )

        row2c2 = tb.Frame(settings)
        row2c2.grid(row=11, column=0, sticky="ew", pady=(10, 0))
        self.focus_steal_enabled_var = tk.BooleanVar(value=bool(getattr(self.store, "focus_steal_enabled", True)))
        tb.Checkbutton(
            row2c2,
            text="Перехватывать фокус игры (в fullscreen лучше выкл.)",
            variable=self.focus_steal_enabled_var,
            bootstyle="round-toggle",
        ).grid(row=0, column=0, sticky="w")

        row2d = tb.Frame(settings)
        # Was overlapping with row2c (auto-confirm). Place after focus toggle.
        row2d.grid(row=12, column=0, sticky="ew", pady=(10, 0))
        self.farm_without_route_var = tk.BooleanVar(value=bool(getattr(self.store, "farm_without_route", False)))
        tb.Checkbutton(
            row2d,
            text="Фарм без маршрута (стоять на месте)",
            variable=self.farm_without_route_var,
            command=self._on_farm_without_route_toggle_changed,
        ).grid(row=0, column=0, sticky="w")

        # Save button at bottom of "Основное"
        save_row = tb.Frame(settings)
        save_row.grid(row=99, column=0, sticky="ew", pady=(10, 0))
        save_row.grid_columnconfigure(0, weight=1)
        self.save_settings_btn = tb.Button(save_row, text="Сохранить", command=self._on_save_settings, bootstyle="primary")
        self.save_settings_btn.grid(row=0, column=0, sticky="ew")

        # mark unsaved when key vars change
        self._bind_unsaved(self.log_to_file_enabled_var)
        self._bind_unsaved(self.log_dir_var)
        self._bind_unsaved(self.pause_hotkey_var)
        self._bind_unsaved(self.stop_record_hotkey_var)
        self._bind_unsaved(self.schedule_enabled_var)
        self._bind_unsaved(self.schedule_start_var)
        self._bind_unsaved(self.schedule_duration_var)
        self._bind_unsaved(self.schedule_mode_var)
        self._bind_unsaved(self.wait_from_var)
        self._bind_unsaved(self.wait_to_var)
        self._bind_unsaved(self.key_hold_var)
        self._bind_unsaved(self.route_delay_from_var)
        self._bind_unsaved(self.route_delay_to_var)
        self._bind_unsaved(self.tp_key_var)
        # detect tab: menu autoclose
        try:
            self._bind_unsaved(self.menu_autoclose_enabled_var)
            self._bind_unsaved(self.menu_roi_x)
            self._bind_unsaved(self.menu_roi_y)
            self._bind_unsaved(self.menu_roi_w)
            self._bind_unsaved(self.menu_roi_h)
            self._bind_unsaved(self.menu_threshold_var)
            self._bind_unsaved(self.menu_attempts_var)
            self._bind_unsaved(self.confirm_roi_x)
            self._bind_unsaved(self.confirm_roi_y)
            self._bind_unsaved(self.confirm_roi_w)
            self._bind_unsaved(self.confirm_roi_h)
            self._bind_unsaved(self.confirm_thr_var)
            self._bind_unsaved(self.gate_roi_x)
            self._bind_unsaved(self.gate_roi_y)
            self._bind_unsaved(self.gate_roi_w)
            self._bind_unsaved(self.gate_roi_h)
            self._bind_unsaved(self.gate_thr_var)
            self._bind_unsaved(self.gate_timeout_var)
            self._bind_unsaved(self.gate_margin_var)
            self._bind_unsaved(self.gate_turn_step_var)
            self._bind_unsaved(self.death_enabled_var)
            self._bind_unsaved(self.death_roi_x)
            self._bind_unsaved(self.death_roi_y)
            self._bind_unsaved(self.death_roi_w)
            self._bind_unsaved(self.death_roi_h)
            self._bind_unsaved(self.death_threshold_var)
            self._bind_unsaved(self.death_cooldown_var)
            self._bind_unsaved(self.death_route_var)
            self._bind_unsaved(self.damage_tp_enabled_var)
            self._bind_unsaved(self.damage_icon_roi_x)
            self._bind_unsaved(self.damage_icon_roi_y)
            self._bind_unsaved(self.damage_icon_roi_w)
            self._bind_unsaved(self.damage_icon_roi_h)
            self._bind_unsaved(self.damage_icon_threshold_var)
            self._bind_unsaved(self.damage_icon_normal_threshold_var)
            self._bind_unsaved(self.damage_icon_margin_var)
            self._bind_unsaved(self.hp_tp_enabled_var)
            self._bind_unsaved(self.hp_roi_x)
            self._bind_unsaved(self.hp_roi_y)
            self._bind_unsaved(self.hp_roi_w)
            self._bind_unsaved(self.hp_roi_h)
            self._bind_unsaved(self.hp_threshold_var)
            self._bind_unsaved(self.damage_tp_press_count_var)
            self._bind_unsaved(self.damage_tp_press_interval_var)
            self._bind_unsaved(self.damage_tp_cooldown_var)
        except Exception:
            pass
        self._bind_unsaved(self.enemy_alert_enabled_var)
        self._bind_unsaved(self.enemy_alert_beeps_var)
        self._bind_unsaved(self.enemy_alert_interval_var)
        self._bind_unsaved(self.enemy_alert_sound_path_var)
        self._bind_unsaved(self.focus_steal_enabled_var)
        self._bind_unsaved(self.farm_without_route_var)

        # --- Auto-buy potions (OCR) ---
        self.tab_autobuy.grid_columnconfigure(0, weight=1)
        autobuy = tb.Labelframe(self.tab_autobuy, text="Автопокупка банок (OCR)", padding=12, bootstyle="secondary")
        autobuy.grid(row=0, column=0, sticky="ew")
        autobuy.grid_columnconfigure(3, weight=1)

        self.auto_buy_enabled_var = tk.BooleanVar(value=bool(getattr(self.store, "auto_buy_potions_enabled", False)))
        tb.Checkbutton(
            autobuy,
            text="Включить автопокупку",
            variable=self.auto_buy_enabled_var,
            bootstyle="round-toggle",
        ).grid(row=0, column=0, sticky="w")

        self.auto_buy_route_mode_only_var = tk.BooleanVar(value=bool(getattr(self.store, "auto_buy_route_mode_only", True)))
        tb.Checkbutton(
            autobuy,
            text="Работает при Старт (маршрут)",
            variable=self.auto_buy_route_mode_only_var,
            bootstyle="round-toggle",
        ).grid(row=0, column=5, padx=(18, 0), sticky="w")

        tb.Label(autobuy, text="Порог (<)", bootstyle="secondary").grid(row=0, column=1, padx=(18, 6), sticky="w")
        self.auto_buy_threshold_var = tk.IntVar(value=int(getattr(self.store, "auto_buy_potions_threshold", 300)))
        tb.Spinbox(autobuy, from_=0, to=999999, textvariable=self.auto_buy_threshold_var, width=10).grid(
            row=0, column=2, sticky="w"
        )

        tb.Label(autobuy, text="Ждать в городе (сек)", bootstyle="secondary").grid(row=0, column=3, padx=(18, 6), sticky="w")
        self.auto_buy_city_wait_var = tk.DoubleVar(value=float(getattr(self.store, "auto_buy_city_wait_s", 8.0)))
        tb.Spinbox(autobuy, from_=0.0, to=120.0, increment=0.5, textvariable=self.auto_buy_city_wait_var, width=8).grid(
            row=0, column=4, sticky="w"
        )

        self.auto_buy_return_to_farm_var = tk.BooleanVar(value=bool(getattr(self.store, "auto_buy_return_to_farm", True)))
        tb.Checkbutton(
            autobuy,
            text="Возврат по Farm после покупки",
            variable=self.auto_buy_return_to_farm_var,
            bootstyle="round-toggle",
        ).grid(row=1, column=0, sticky="w", pady=(10, 0))

        # ROI for potion count
        tb.Label(autobuy, text="ROI банок (x,y,w,h)", bootstyle="secondary").grid(row=2, column=0, sticky="w", pady=(10, 0))
        self.potion_roi_x = tk.IntVar(value=int(getattr(self.store.auto_buy_potions_roi, "x", 0)))
        self.potion_roi_y = tk.IntVar(value=int(getattr(self.store.auto_buy_potions_roi, "y", 0)))
        self.potion_roi_w = tk.IntVar(value=int(getattr(self.store.auto_buy_potions_roi, "w", 1)))
        self.potion_roi_h = tk.IntVar(value=int(getattr(self.store.auto_buy_potions_roi, "h", 1)))
        tb.Spinbox(autobuy, from_=0, to=10000, textvariable=self.potion_roi_x, width=6).grid(row=2, column=1, padx=(10, 6), pady=(10, 0))
        tb.Spinbox(autobuy, from_=0, to=10000, textvariable=self.potion_roi_y, width=6).grid(row=2, column=2, padx=6, pady=(10, 0))
        tb.Spinbox(autobuy, from_=1, to=10000, textvariable=self.potion_roi_w, width=6).grid(row=2, column=3, padx=6, pady=(10, 0))
        tb.Spinbox(autobuy, from_=1, to=10000, textvariable=self.potion_roi_h, width=6).grid(row=2, column=4, padx=6, pady=(10, 0))
        tb.Button(autobuy, text="Выбрать ROI банок мышью", bootstyle="outline", command=self._on_pick_potion_roi).grid(
            row=3, column=0, columnspan=6, sticky="ew", pady=(10, 0)
        )

        # Route name to execute
        tb.Label(autobuy, text="Маршрут покупки", bootstyle="secondary").grid(row=4, column=0, sticky="w", pady=(10, 0))
        self.auto_buy_route_var = tk.StringVar(value=str(getattr(self.store, "auto_buy_potions_route_name", "") or ""))
        self.auto_buy_route_combo = tb.Combobox(autobuy, textvariable=self.auto_buy_route_var, state="readonly")
        self.auto_buy_route_combo.grid(row=4, column=1, columnspan=5, sticky="ew", pady=(10, 0))
        buy_route_btns = tb.Frame(autobuy)
        buy_route_btns.grid(row=5, column=0, columnspan=6, sticky="ew", pady=(10, 0))
        buy_route_btns.grid_columnconfigure(2, weight=1)
        tb.Button(
            buy_route_btns,
            text="Новый маршрут покупки",
            bootstyle="outline",
            command=self._on_autobuy_new_buy_route,
        ).grid(row=0, column=0, sticky="w")
        tb.Button(
            buy_route_btns,
            text="Назначить текущий",
            bootstyle="outline",
            command=self._on_autobuy_use_current_route,
        ).grid(row=0, column=1, padx=(10, 0), sticky="w")

        tb.Button(autobuy, text="Проверить OCR сейчас (в лог)", bootstyle="outline", command=self._on_test_potion_ocr).grid(
            row=6, column=0, columnspan=6, sticky="ew", pady=(10, 0)
        )
        tb.Button(autobuy, text="Показать кадр ROI (что читает OCR)", bootstyle="outline", command=self._on_preview_potion_roi).grid(
            row=7, column=0, columnspan=6, sticky="ew", pady=(10, 0)
        )

        # Auto-banks log (local, for OCR/debug)
        autobuy_logs = tb.Labelframe(self.tab_autobuy, text="Логи (Авто банки)", padding=12, bootstyle="secondary")
        autobuy_logs.grid(row=1, column=0, sticky="nsew", pady=(12, 0))
        self.tab_autobuy.grid_rowconfigure(1, weight=1)
        autobuy_logs.grid_rowconfigure(0, weight=1)
        autobuy_logs.grid_columnconfigure(0, weight=1)
        self.autobuy_log_text = tk.Text(autobuy_logs, wrap="word", font=("Consolas", 10), height=8)
        self.autobuy_log_text.grid(row=0, column=0, sticky="nsew")
        autobuy_scroll = tb.Scrollbar(autobuy_logs, orient="vertical", command=self.autobuy_log_text.yview)
        autobuy_scroll.grid(row=0, column=1, sticky="ns")
        self.autobuy_log_text.configure(yscrollcommand=autobuy_scroll.set)
        self.autobuy_log_text.configure(state="disabled")
        tb.Button(autobuy_logs, text="Очистить", bootstyle="outline", command=lambda: self._autobuy_log_clear()).grid(
            row=1, column=0, sticky="e", pady=(10, 0)
        )

        # mark unsaved for auto-buy vars too
        self._bind_unsaved(self.auto_buy_enabled_var)
        self._bind_unsaved(self.auto_buy_route_mode_only_var)
        self._bind_unsaved(self.auto_buy_threshold_var)
        self._bind_unsaved(self.auto_buy_city_wait_var)
        self._bind_unsaved(self.auto_buy_return_to_farm_var)
        self._bind_unsaved(self.potion_roi_x)
        self._bind_unsaved(self.potion_roi_y)
        self._bind_unsaved(self.potion_roi_w)
        self._bind_unsaved(self.potion_roi_h)
        self._bind_unsaved(self.auto_buy_route_var)

        # Logs (top) + Start controls (bottom)
        logs_box = tb.Labelframe(tab_settings_inner, text="Логи", padding=12, bootstyle="secondary")
        logs_box.grid(row=1, column=0, sticky="nsew", pady=(12, 0))
        logs_box.grid_rowconfigure(0, weight=1)
        logs_box.grid_columnconfigure(0, weight=1)
        self.log_text = tk.Text(logs_box, wrap="word", font=("Consolas", 10))
        self.log_text.grid(row=0, column=0, sticky="nsew")
        log_scroll = tb.Scrollbar(logs_box, orient="vertical", command=self.log_text.yview)
        log_scroll.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=log_scroll.set)
        self.log_text.configure(state="disabled")

        actions = tb.Labelframe(tab_settings_inner, text="Запуск", padding=12, bootstyle="secondary")
        actions.grid(row=2, column=0, sticky="ew", pady=(12, 0))
        actions.grid_columnconfigure(0, weight=1)

        start_row = tb.Frame(actions)
        start_row.grid(row=0, column=0, sticky="ew")
        start_row.grid_columnconfigure(0, weight=1)
        self.start_route_btn = tb.Button(start_row, text="Старт (маршрут)", command=self._on_start_route, bootstyle="primary")
        self.start_route_btn.grid(
            row=0, column=0, sticky="ew"
        )
        self.stop_btn = tb.Button(start_row, text="Стоп", command=self._on_stop, bootstyle="danger")
        self.stop_btn.grid(row=0, column=1, padx=(10, 0))

        afk_row = tb.Frame(actions)
        afk_row.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        afk_row.grid_columnconfigure(0, weight=1)
        self.start_afk_btn = tb.Button(afk_row, text="Старт AFK", command=self._on_start_afk, bootstyle="primary")
        self.start_afk_btn.grid(
            row=0, column=0, sticky="ew"
        )
        self.stop_afk_btn = tb.Button(afk_row, text="Стоп AFK", command=self._on_stop, bootstyle="danger")
        self.stop_afk_btn.grid(row=0, column=1, padx=(10, 0))

        # Let logs expand inside scroll container
        try:
            tab_settings_inner.grid_rowconfigure(1, weight=1)
        except Exception:
            pass

        # Detect tab is scrollable (many sections)
        self.tab_detect.grid_rowconfigure(0, weight=1)
        self.tab_detect.grid_columnconfigure(0, weight=1)
        detect_canvas = tk.Canvas(self.tab_detect, highlightthickness=0)
        detect_scroll = tb.Scrollbar(self.tab_detect, orient="vertical", command=detect_canvas.yview)
        detect_canvas.configure(yscrollcommand=detect_scroll.set)
        detect_canvas.grid(row=0, column=0, sticky="nsew")
        detect_scroll.grid(row=0, column=1, sticky="ns")
        detect_inner = tb.Frame(detect_canvas, padding=(0, 0))
        detect_inner_id = detect_canvas.create_window((0, 0), window=detect_inner, anchor="nw")

        def _sync_detect_width(_evt=None) -> None:
            try:
                detect_canvas.itemconfigure(detect_inner_id, width=detect_canvas.winfo_width())
            except Exception:
                pass

        def _sync_detect_scroll(_evt=None) -> None:
            try:
                detect_canvas.configure(scrollregion=detect_canvas.bbox("all"))
            except Exception:
                pass

        detect_canvas.bind("<Configure>", _sync_detect_width)
        detect_inner.bind("<Configure>", _sync_detect_scroll)

        detect_canvas.bind("<Enter>", lambda _e: self._set_mw_canvas(detect_canvas))
        detect_canvas.bind("<Leave>", lambda _e: self._set_mw_canvas(None))

        empty_text = tb.Labelframe(detect_inner, text="По фразе 'Нет Цель поиска'", padding=12, bootstyle="secondary")
        empty_text.grid(row=0, column=0, sticky="ew")
        detect_inner.grid_columnconfigure(0, weight=1)
        tb.Label(
            empty_text,
            text="Нажми «Радар: область (ROI)» → выдели область текста → отпусти мышь (сохранится сразу).\n"
            "Далее нажми «Радар: шаблон 'Нет Цель поиска'» (сохранит шаблон).",
            bootstyle="secondary",
        ).grid(
            row=0, column=0, sticky="w"
        )
        rowtx = tb.Frame(empty_text)
        rowtx.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        tb.Label(rowtx, text="Text ROI (x, y, w, h)", bootstyle="secondary").grid(row=0, column=0, sticky="w")
        self.text_roi_x = tk.IntVar(value=int(getattr(self.store.empty_text_roi, "x", 0)))
        self.text_roi_y = tk.IntVar(value=int(getattr(self.store.empty_text_roi, "y", 0)))
        self.text_roi_w = tk.IntVar(value=int(getattr(self.store.empty_text_roi, "w", 1)))
        self.text_roi_h = tk.IntVar(value=int(getattr(self.store.empty_text_roi, "h", 1)))
        tb.Spinbox(rowtx, from_=0, to=10000, textvariable=self.text_roi_x, width=6).grid(row=0, column=1, padx=(12, 6))
        tb.Spinbox(rowtx, from_=0, to=10000, textvariable=self.text_roi_y, width=6).grid(row=0, column=2, padx=6)
        tb.Spinbox(rowtx, from_=1, to=10000, textvariable=self.text_roi_w, width=6).grid(row=0, column=3, padx=6)
        tb.Spinbox(rowtx, from_=1, to=10000, textvariable=self.text_roi_h, width=6).grid(row=0, column=4, padx=6)
        tb.Button(empty_text, text="Радар: область (ROI)", command=self._on_pick_text_roi, bootstyle="primary").grid(
            row=2, column=0, sticky="ew", pady=(10, 0)
        )
        tb.Button(
            empty_text,
            text="Радар: шаблон 'Нет Цель поиска'",
            command=self._on_capture_empty_text,
            bootstyle="primary",
        ).grid(row=3, column=0, sticky="ew", pady=(10, 0))
        rowtt = tb.Frame(empty_text)
        rowtt.grid(row=4, column=0, sticky="ew", pady=(10, 0))
        tb.Label(rowtt, text="Порог совпадения", bootstyle="secondary").grid(row=0, column=0, sticky="w")
        self.empty_text_threshold_var = tk.DoubleVar(value=float(getattr(self.store, "empty_text_threshold", 0.86)))
        tb.Spinbox(rowtt, from_=0.50, to=0.99, increment=0.01, textvariable=self.empty_text_threshold_var, width=8).grid(
            row=0, column=1, padx=(12, 0)
        )

        # --- Menu/chat auto-close ---
        menu_box = tb.Labelframe(detect_inner, text="Меню/чат открыт (авто-ESC)", padding=12, bootstyle="secondary")
        menu_box.grid(row=1, column=0, sticky="ew", pady=(12, 0))
        tb.Label(
            menu_box,
            text="Шаг 1: выдели ROI на индикаторе (иконка/текст) что меню открыто. Шаг 2: сделай шаблон. Потом включи тумблер.",
            bootstyle="secondary",
            wraplength=760,
            justify="left",
        ).grid(row=0, column=0, sticky="w")

        self.menu_autoclose_enabled_var = tk.BooleanVar(value=bool(getattr(self.store, "menu_autoclose_enabled", False)))
        tb.Checkbutton(menu_box, text="Включить авто-ESC перед действиями", variable=self.menu_autoclose_enabled_var, bootstyle="round-toggle").grid(
            row=1, column=0, sticky="w", pady=(10, 0)
        )

        rowm = tb.Frame(menu_box)
        rowm.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        tb.Label(rowm, text="Menu ROI (x, y, w, h)", bootstyle="secondary").grid(row=0, column=0, sticky="w")
        self.menu_roi_x = tk.IntVar(value=int(getattr(self.store.menu_autoclose_roi, "x", 0)))
        self.menu_roi_y = tk.IntVar(value=int(getattr(self.store.menu_autoclose_roi, "y", 0)))
        self.menu_roi_w = tk.IntVar(value=int(getattr(self.store.menu_autoclose_roi, "w", 1)))
        self.menu_roi_h = tk.IntVar(value=int(getattr(self.store.menu_autoclose_roi, "h", 1)))
        tb.Spinbox(rowm, from_=0, to=10000, textvariable=self.menu_roi_x, width=6).grid(row=0, column=1, padx=(12, 6))
        tb.Spinbox(rowm, from_=0, to=10000, textvariable=self.menu_roi_y, width=6).grid(row=0, column=2, padx=6)
        tb.Spinbox(rowm, from_=1, to=10000, textvariable=self.menu_roi_w, width=6).grid(row=0, column=3, padx=6)
        tb.Spinbox(rowm, from_=1, to=10000, textvariable=self.menu_roi_h, width=6).grid(row=0, column=4, padx=6)
        tb.Button(menu_box, text="Выбрать Menu ROI мышью", command=self._on_pick_menu_roi, bootstyle="primary").grid(
            row=3, column=0, sticky="ew", pady=(10, 0)
        )
        tb.Button(menu_box, text="Сделать шаблон меню/чата", command=self._on_capture_menu_tpl, bootstyle="outline").grid(
            row=4, column=0, sticky="ew", pady=(8, 0)
        )

        rowm2 = tb.Frame(menu_box)
        rowm2.grid(row=5, column=0, sticky="ew", pady=(10, 0))
        tb.Label(rowm2, text="Порог", bootstyle="secondary").grid(row=0, column=0, sticky="w")
        self.menu_threshold_var = tk.DoubleVar(value=float(getattr(self.store, "menu_autoclose_threshold", 0.86)))
        tb.Spinbox(rowm2, from_=0.50, to=0.99, increment=0.01, textvariable=self.menu_threshold_var, width=8).grid(
            row=0, column=1, padx=(12, 0)
        )
        tb.Label(rowm2, text="ESC попыток", bootstyle="secondary").grid(row=0, column=2, padx=(18, 0), sticky="w")
        self.menu_attempts_var = tk.IntVar(value=int(getattr(self.store, "menu_autoclose_attempts", 2)))
        tb.Spinbox(rowm2, from_=1, to=5, textvariable=self.menu_attempts_var, width=6).grid(row=0, column=3, padx=(12, 0))

        # --- Confirm popup (gate enter) ---
        confirm_box = tb.Labelframe(detect_inner, text="Окно подтверждения входа (CONFIRM)", padding=12, bootstyle="secondary")
        confirm_box.grid(row=2, column=0, sticky="ew", pady=(12, 0))
        tb.Label(
            confirm_box,
            text="Выдели ROI на окне подтверждения (например на тексте/кнопке), затем сними шаблон.\n"
            "В маршруте можно вставить шаг CONFIRM: бот подождёт появления окна и нажмёт Y.",
            bootstyle="secondary",
            wraplength=760,
            justify="left",
        ).grid(row=0, column=0, sticky="w")

        rowc = tb.Frame(confirm_box)
        rowc.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        tb.Label(rowc, text="Confirm ROI (x, y, w, h)", bootstyle="secondary").grid(row=0, column=0, sticky="w")
        self.confirm_roi_x = tk.IntVar(value=int(getattr(self.store.confirm_popup_roi, "x", 0)))
        self.confirm_roi_y = tk.IntVar(value=int(getattr(self.store.confirm_popup_roi, "y", 0)))
        self.confirm_roi_w = tk.IntVar(value=int(getattr(self.store.confirm_popup_roi, "w", 1)))
        self.confirm_roi_h = tk.IntVar(value=int(getattr(self.store.confirm_popup_roi, "h", 1)))
        tb.Spinbox(rowc, from_=0, to=10000, textvariable=self.confirm_roi_x, width=6).grid(row=0, column=1, padx=(12, 6))
        tb.Spinbox(rowc, from_=0, to=10000, textvariable=self.confirm_roi_y, width=6).grid(row=0, column=2, padx=6)
        tb.Spinbox(rowc, from_=1, to=10000, textvariable=self.confirm_roi_w, width=6).grid(row=0, column=3, padx=6)
        tb.Spinbox(rowc, from_=1, to=10000, textvariable=self.confirm_roi_h, width=6).grid(row=0, column=4, padx=6)
        tb.Button(confirm_box, text="Выбрать Confirm ROI мышью", command=self._on_pick_confirm_roi, bootstyle="primary").grid(
            row=2, column=0, sticky="ew", pady=(10, 0)
        )
        tb.Button(confirm_box, text="Сделать шаблон CONFIRM", command=self._on_capture_confirm_tpl, bootstyle="outline").grid(
            row=3, column=0, sticky="ew", pady=(8, 0)
        )
        rowct = tb.Frame(confirm_box)
        rowct.grid(row=4, column=0, sticky="ew", pady=(10, 0))
        tb.Label(rowct, text="Порог", bootstyle="secondary").grid(row=0, column=0, sticky="w")
        self.confirm_thr_var = tk.DoubleVar(value=float(getattr(self.store, "confirm_popup_threshold", 0.86)))
        tb.Spinbox(rowct, from_=0.50, to=0.99, increment=0.01, textvariable=self.confirm_thr_var, width=8).grid(
            row=0, column=1, padx=(12, 0)
        )

        # --- Gate (template + ROI) ---
        gate_box = tb.Labelframe(detect_inner, text="Ворота (GATE: найти → повернуть камерой → клик)", padding=12, bootstyle="secondary")
        gate_box.grid(row=3, column=0, sticky="ew", pady=(12, 0))
        tb.Label(
            gate_box,
            text="1) Выдели ROI где обычно видны ворота. 2) Сними шаблон ворот (кусок ворот).\n"
            "Шаг GATE в маршруте: бот зажмёт ПКМ, повернёт камеру к воротам и кликнет по ним.",
            bootstyle="secondary",
            wraplength=760,
            justify="left",
        ).grid(row=0, column=0, sticky="w")

        rowg = tb.Frame(gate_box)
        rowg.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        tb.Label(rowg, text="Gate ROI (x,y,w,h)", bootstyle="secondary").grid(row=0, column=0, sticky="w")
        self.gate_roi_x = tk.IntVar(value=int(getattr(self.store.gate_roi, "x", 0)))
        self.gate_roi_y = tk.IntVar(value=int(getattr(self.store.gate_roi, "y", 0)))
        self.gate_roi_w = tk.IntVar(value=int(getattr(self.store.gate_roi, "w", 1)))
        self.gate_roi_h = tk.IntVar(value=int(getattr(self.store.gate_roi, "h", 1)))
        tb.Spinbox(rowg, from_=0, to=10000, textvariable=self.gate_roi_x, width=6).grid(row=0, column=1, padx=(12, 6))
        tb.Spinbox(rowg, from_=0, to=10000, textvariable=self.gate_roi_y, width=6).grid(row=0, column=2, padx=6)
        tb.Spinbox(rowg, from_=1, to=10000, textvariable=self.gate_roi_w, width=6).grid(row=0, column=3, padx=6)
        tb.Spinbox(rowg, from_=1, to=10000, textvariable=self.gate_roi_h, width=6).grid(row=0, column=4, padx=6)
        tb.Button(gate_box, text="Выбрать Gate ROI мышью", command=self._on_pick_gate_roi, bootstyle="primary").grid(
            row=2, column=0, sticky="ew", pady=(10, 0)
        )
        tb.Button(gate_box, text="Сделать шаблон GATE", command=self._on_capture_gate_tpl, bootstyle="outline").grid(
            row=3, column=0, sticky="ew", pady=(8, 0)
        )
        rowg2 = tb.Frame(gate_box)
        rowg2.grid(row=4, column=0, sticky="ew", pady=(10, 0))
        tb.Label(rowg2, text="Порог", bootstyle="secondary").grid(row=0, column=0, sticky="w")
        self.gate_thr_var = tk.DoubleVar(value=float(getattr(self.store, "gate_threshold", 0.83)))
        tb.Spinbox(rowg2, from_=0.50, to=0.99, increment=0.01, textvariable=self.gate_thr_var, width=8).grid(
            row=0, column=1, padx=(12, 0)
        )
        tb.Label(rowg2, text="Timeout (сек)", bootstyle="secondary").grid(row=0, column=2, padx=(18, 0), sticky="w")
        self.gate_timeout_var = tk.DoubleVar(value=float(getattr(self.store, "gate_seek_timeout_s", 6.0)))
        tb.Spinbox(rowg2, from_=1.0, to=30.0, increment=0.5, textvariable=self.gate_timeout_var, width=8).grid(
            row=0, column=3, padx=(12, 0)
        )
        tb.Label(rowg2, text="Центр (px)", bootstyle="secondary").grid(row=0, column=4, padx=(18, 0), sticky="w")
        self.gate_margin_var = tk.IntVar(value=int(getattr(self.store, "gate_center_margin_px", 40)))
        tb.Spinbox(rowg2, from_=5, to=300, textvariable=self.gate_margin_var, width=8).grid(row=0, column=5, padx=(12, 0))
        tb.Label(rowg2, text="Шаг поворота (px)", bootstyle="secondary").grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.gate_turn_step_var = tk.IntVar(value=int(getattr(self.store, "gate_turn_step_px", 120)))
        tb.Spinbox(rowg2, from_=20, to=600, textvariable=self.gate_turn_step_var, width=8).grid(row=1, column=1, padx=(12, 0), pady=(8, 0))

        # --- Death detect + recovery ---
        death_box = tb.Labelframe(detect_inner, text="Смерть (детект + маршрут восстановления)", padding=12, bootstyle="secondary")
        death_box.grid(row=4, column=0, sticky="ew", pady=(12, 0))
        tb.Label(
            death_box,
            text="Шаг 1: выдели ROI на тексте/иконке смерти. Шаг 2: сделай шаблон. Шаг 3: выбери маршрут восстановления.",
            bootstyle="secondary",
            wraplength=760,
            justify="left",
        ).grid(row=0, column=0, sticky="w")

        self.death_enabled_var = tk.BooleanVar(value=bool(getattr(self.store, "death_detect_enabled", False)))
        tb.Checkbutton(death_box, text="Включить детект смерти", variable=self.death_enabled_var, bootstyle="round-toggle").grid(
            row=1, column=0, sticky="w", pady=(10, 0)
        )

        rowd = tb.Frame(death_box)
        rowd.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        tb.Label(rowd, text="Death ROI (x, y, w, h)", bootstyle="secondary").grid(row=0, column=0, sticky="w")
        self.death_roi_x = tk.IntVar(value=int(getattr(self.store.death_roi, "x", 0)))
        self.death_roi_y = tk.IntVar(value=int(getattr(self.store.death_roi, "y", 0)))
        self.death_roi_w = tk.IntVar(value=int(getattr(self.store.death_roi, "w", 1)))
        self.death_roi_h = tk.IntVar(value=int(getattr(self.store.death_roi, "h", 1)))
        tb.Spinbox(rowd, from_=0, to=10000, textvariable=self.death_roi_x, width=6).grid(row=0, column=1, padx=(12, 6))
        tb.Spinbox(rowd, from_=0, to=10000, textvariable=self.death_roi_y, width=6).grid(row=0, column=2, padx=6)
        tb.Spinbox(rowd, from_=1, to=10000, textvariable=self.death_roi_w, width=6).grid(row=0, column=3, padx=6)
        tb.Spinbox(rowd, from_=1, to=10000, textvariable=self.death_roi_h, width=6).grid(row=0, column=4, padx=6)
        tb.Button(death_box, text="Выбрать Death ROI мышью", command=self._on_pick_death_roi, bootstyle="primary").grid(
            row=3, column=0, sticky="ew", pady=(10, 0)
        )
        tb.Button(death_box, text="Сделать шаблон смерти", command=self._on_capture_death_tpl, bootstyle="outline").grid(
            row=4, column=0, sticky="ew", pady=(8, 0)
        )

        rowd2 = tb.Frame(death_box)
        rowd2.grid(row=5, column=0, sticky="ew", pady=(10, 0))
        tb.Label(rowd2, text="Порог", bootstyle="secondary").grid(row=0, column=0, sticky="w")
        self.death_threshold_var = tk.DoubleVar(value=float(getattr(self.store, "death_threshold", 0.86)))
        tb.Spinbox(rowd2, from_=0.50, to=0.99, increment=0.01, textvariable=self.death_threshold_var, width=8).grid(
            row=0, column=1, padx=(12, 0)
        )
        tb.Label(rowd2, text="Cooldown (сек)", bootstyle="secondary").grid(row=0, column=2, padx=(18, 0), sticky="w")
        self.death_cooldown_var = tk.DoubleVar(value=float(getattr(self.store, "death_cooldown_s", 20.0)))
        tb.Spinbox(rowd2, from_=3.0, to=600.0, increment=1.0, textvariable=self.death_cooldown_var, width=8).grid(
            row=0, column=3, padx=(12, 0)
        )

        rowd3 = tb.Frame(death_box)
        rowd3.grid(row=6, column=0, sticky="ew", pady=(10, 0))
        tb.Label(rowd3, text="Маршрут восстановления", bootstyle="secondary").grid(row=0, column=0, sticky="w")
        death_routes = ["(нет)"] + [r.name for r in self.store.get_active_profile().routes]
        self.death_route_var = tk.StringVar(value=str(getattr(self.store, "death_route_name", "") or "(нет)"))
        tb.Combobox(rowd3, textvariable=self.death_route_var, state="readonly", values=death_routes, width=24).grid(
            row=0, column=1, padx=(12, 0), sticky="w"
        )

        # --- Damage/HP teleport mode ---
        dmg_box = tb.Labelframe(detect_inner, text="Детект по урону + HP% (ТП по урону)", padding=12, bootstyle="secondary")
        # NOTE: keep rows unique; row=3 is used by Gate section above
        dmg_box.grid(row=5, column=0, sticky="ew", pady=(12, 0))
        tb.Label(
            dmg_box,
            text=(
                "Идея: если появляется индикатор «вас атакуют» (иконка мечей) + HP падает ниже порога → жмём ТП много раз.\n"
                "Важно: в некоторых играх в этом месте всегда есть «обычная» иконка, а при атаке она меняется на мечи.\n"
                "Для стабильности сделай 2 шаблона: обычная иконка (когда НЕ бьют) и мечи (когда БЬЮТ)."
            ),
            bootstyle="secondary",
            wraplength=760,
            justify="left",
        ).grid(row=0, column=0, sticky="w")

        self.damage_tp_enabled_var = tk.BooleanVar(value=bool(getattr(self.store, "damage_tp_enabled", False)))
        tb.Checkbutton(
            dmg_box,
            text="Включить ТП по урону",
            variable=self.damage_tp_enabled_var,
            bootstyle="round-toggle",
            command=self._on_damage_tp_toggle_changed,
        ).grid(
            row=1, column=0, sticky="w", pady=(10, 0)
        )

        # Attacked/Normal icon ROIs + templates
        rowa = tb.Frame(dmg_box)
        rowa.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        tb.Label(rowa, text="ROI мечей (x,y,w,h)", bootstyle="secondary").grid(row=0, column=0, sticky="w")
        self.damage_icon_roi_x = tk.IntVar(value=int(getattr(self.store.damage_icon_roi, "x", 0)))
        self.damage_icon_roi_y = tk.IntVar(value=int(getattr(self.store.damage_icon_roi, "y", 0)))
        self.damage_icon_roi_w = tk.IntVar(value=int(getattr(self.store.damage_icon_roi, "w", 1)))
        self.damage_icon_roi_h = tk.IntVar(value=int(getattr(self.store.damage_icon_roi, "h", 1)))
        tb.Spinbox(rowa, from_=0, to=10000, textvariable=self.damage_icon_roi_x, width=6).grid(row=0, column=1, padx=(12, 6))
        tb.Spinbox(rowa, from_=0, to=10000, textvariable=self.damage_icon_roi_y, width=6).grid(row=0, column=2, padx=6)
        tb.Spinbox(rowa, from_=1, to=10000, textvariable=self.damage_icon_roi_w, width=6).grid(row=0, column=3, padx=6)
        tb.Spinbox(rowa, from_=1, to=10000, textvariable=self.damage_icon_roi_h, width=6).grid(row=0, column=4, padx=6)
        tb.Button(dmg_box, text="Мечи: ROI+шаблон", command=self._on_pick_swords_roi_and_tpl, bootstyle="primary").grid(
            row=3, column=0, sticky="ew", pady=(10, 0)
        )
        # tpl is captured automatically after ROI selection

        rowa_norm = tb.Frame(dmg_box)
        rowa_norm.grid(row=5, column=0, sticky="ew", pady=(10, 0))
        tb.Label(rowa_norm, text="ROI корпуса (x,y,w,h)", bootstyle="secondary").grid(row=0, column=0, sticky="w")
        self.damage_icon_norm_roi_x = tk.IntVar(value=int(getattr(getattr(self.store, "damage_icon_normal_roi", self.store.damage_icon_roi), "x", 0)))
        self.damage_icon_norm_roi_y = tk.IntVar(value=int(getattr(getattr(self.store, "damage_icon_normal_roi", self.store.damage_icon_roi), "y", 0)))
        self.damage_icon_norm_roi_w = tk.IntVar(value=int(getattr(getattr(self.store, "damage_icon_normal_roi", self.store.damage_icon_roi), "w", 1)))
        self.damage_icon_norm_roi_h = tk.IntVar(value=int(getattr(getattr(self.store, "damage_icon_normal_roi", self.store.damage_icon_roi), "h", 1)))
        tb.Spinbox(rowa_norm, from_=0, to=10000, textvariable=self.damage_icon_norm_roi_x, width=6).grid(row=0, column=1, padx=(12, 6))
        tb.Spinbox(rowa_norm, from_=0, to=10000, textvariable=self.damage_icon_norm_roi_y, width=6).grid(row=0, column=2, padx=6)
        tb.Spinbox(rowa_norm, from_=1, to=10000, textvariable=self.damage_icon_norm_roi_w, width=6).grid(row=0, column=3, padx=6)
        tb.Spinbox(rowa_norm, from_=1, to=10000, textvariable=self.damage_icon_norm_roi_h, width=6).grid(row=0, column=4, padx=6)

        tb.Button(dmg_box, text="Корпус: ROI+шаблон", command=self._on_pick_body_roi_and_tpl, bootstyle="primary").grid(
            row=6, column=0, sticky="ew", pady=(10, 0)
        )
        # tpl is captured automatically after ROI selection

        rowa2 = tb.Frame(dmg_box)
        rowa2.grid(row=8, column=0, sticky="ew", pady=(10, 0))
        tb.Label(rowa2, text="Порог мечей", bootstyle="secondary").grid(row=0, column=0, sticky="w")
        self.damage_icon_threshold_var = tk.DoubleVar(value=float(getattr(self.store, "damage_icon_threshold", 0.86)))
        tb.Spinbox(rowa2, from_=0.50, to=0.99, increment=0.01, textvariable=self.damage_icon_threshold_var, width=8).grid(
            row=0, column=1, padx=(12, 0)
        )
        tb.Label(rowa2, text="Порог обычной", bootstyle="secondary").grid(row=0, column=2, padx=(18, 0), sticky="w")
        self.damage_icon_normal_threshold_var = tk.DoubleVar(value=float(getattr(self.store, "damage_icon_normal_threshold", 0.86)))
        tb.Spinbox(rowa2, from_=0.50, to=0.99, increment=0.01, textvariable=self.damage_icon_normal_threshold_var, width=8).grid(
            row=0, column=3, padx=(12, 0)
        )
        tb.Label(rowa2, text="Разница (margin)", bootstyle="secondary").grid(row=0, column=4, padx=(18, 0), sticky="w")
        self.damage_icon_margin_var = tk.DoubleVar(value=float(getattr(self.store, "damage_icon_margin", 0.04)))
        tb.Spinbox(rowa2, from_=0.00, to=0.30, increment=0.01, textvariable=self.damage_icon_margin_var, width=8).grid(
            row=0, column=5, padx=(12, 0)
        )

        # HP bar ROI + threshold + progressbar
        self.hp_tp_enabled_var = tk.BooleanVar(value=bool(getattr(self.store, "hp_tp_enabled", True)))
        tb.Checkbutton(
            dmg_box,
            text="Включить ТП по HP",
            variable=self.hp_tp_enabled_var,
            bootstyle="round-toggle",
            command=self._on_hp_tp_toggle_changed,
        ).grid(row=9, column=0, sticky="w", pady=(10, 0))
        rowh = tb.Frame(dmg_box)
        rowh.grid(row=10, column=0, sticky="ew", pady=(10, 0))
        tb.Label(rowh, text="ROI HP полоски (x,y,w,h)", bootstyle="secondary").grid(row=0, column=0, sticky="w")
        self.hp_roi_x = tk.IntVar(value=int(getattr(self.store.hp_bar_roi, "x", 0)))
        self.hp_roi_y = tk.IntVar(value=int(getattr(self.store.hp_bar_roi, "y", 0)))
        self.hp_roi_w = tk.IntVar(value=int(getattr(self.store.hp_bar_roi, "w", 1)))
        self.hp_roi_h = tk.IntVar(value=int(getattr(self.store.hp_bar_roi, "h", 1)))
        tb.Spinbox(rowh, from_=0, to=10000, textvariable=self.hp_roi_x, width=6).grid(row=0, column=1, padx=(12, 6))
        tb.Spinbox(rowh, from_=0, to=10000, textvariable=self.hp_roi_y, width=6).grid(row=0, column=2, padx=6)
        tb.Spinbox(rowh, from_=1, to=10000, textvariable=self.hp_roi_w, width=6).grid(row=0, column=3, padx=6)
        tb.Spinbox(rowh, from_=1, to=10000, textvariable=self.hp_roi_h, width=6).grid(row=0, column=4, padx=6)
        tb.Button(dmg_box, text="Выбрать ROI HP", command=self._on_pick_hp_roi, bootstyle="primary").grid(
            row=11, column=0, sticky="ew", pady=(10, 0)
        )

        rowh2 = tb.Frame(dmg_box)
        rowh2.grid(row=12, column=0, sticky="ew", pady=(10, 0))
        tb.Label(rowh2, text="Порог HP (%)", bootstyle="secondary").grid(row=0, column=0, sticky="w")
        self.hp_threshold_var = tk.IntVar(value=int(getattr(self.store, "hp_tp_threshold_pct", 70)))
        tb.Spinbox(rowh2, from_=1, to=99, textvariable=self.hp_threshold_var, width=8).grid(row=0, column=1, padx=(12, 0))
        self._hp_preview_var = tk.IntVar(value=100)
        tb.Progressbar(rowh2, maximum=100, value=100, variable=self._hp_preview_var, bootstyle="danger-striped").grid(
            row=0, column=2, padx=(18, 0), sticky="ew"
        )
        rowh2.grid_columnconfigure(2, weight=1)
        self._hp_label_var = tk.StringVar(value="HP: ?%")
        tb.Label(rowh2, textvariable=self._hp_label_var, bootstyle="secondary").grid(row=0, column=3, padx=(10, 0), sticky="w")

        # TP spam settings
        rowt = tb.Frame(dmg_box)
        rowt.grid(row=11, column=0, sticky="ew", pady=(10, 0))
        tb.Label(rowt, text="Жать ТП (раз)", bootstyle="secondary").grid(row=0, column=0, sticky="w")
        self.damage_tp_press_count_var = tk.IntVar(value=int(getattr(self.store, "damage_tp_press_count", 6)))
        tb.Spinbox(rowt, from_=1, to=20, textvariable=self.damage_tp_press_count_var, width=8).grid(row=0, column=1, padx=(12, 0))
        tb.Label(rowt, text="Интервал (сек)", bootstyle="secondary").grid(row=0, column=2, padx=(18, 0), sticky="w")
        self.damage_tp_press_interval_var = tk.DoubleVar(value=float(getattr(self.store, "damage_tp_press_interval_s", 0.12)))
        tb.Spinbox(rowt, from_=0.05, to=1.0, increment=0.01, textvariable=self.damage_tp_press_interval_var, width=8).grid(
            row=0, column=3, padx=(12, 0)
        )
        tb.Label(rowt, text="Cooldown (сек)", bootstyle="secondary").grid(row=0, column=4, padx=(18, 0), sticky="w")
        self.damage_tp_cooldown_var = tk.DoubleVar(value=float(getattr(self.store, "damage_tp_cooldown_s", 8.0)))
        tb.Spinbox(rowt, from_=1.0, to=120.0, increment=1.0, textvariable=self.damage_tp_cooldown_var, width=8).grid(
            row=0, column=5, padx=(12, 0)
        )

        rowtest = tb.Frame(dmg_box)
        rowtest.grid(row=12, column=0, sticky="ew", pady=(10, 0))
        tb.Button(rowtest, text="Проверить HP сейчас (в лог)", command=self._on_test_hp_now, bootstyle="outline").grid(row=0, column=0)
        tb.Button(rowtest, text="Проверить иконку атаки (в лог)", command=self._on_test_damage_icon_now, bootstyle="outline").grid(
            row=0, column=1, padx=(10, 0)
        )

        # --- Route builder by screenshot ---
        self.tab_route.grid_rowconfigure(1, weight=1)
        self.tab_route.grid_columnconfigure(0, weight=1)

        rb_top = tb.Labelframe(self.tab_route, text="Конструктор по скриншоту", padding=12, bootstyle="secondary")
        rb_top.grid(row=0, column=0, sticky="ew")
        rb_top.grid_columnconfigure(8, weight=1)

        tb.Button(rb_top, text="Снять скрин окна игры", bootstyle="primary", command=self._rb_take_snapshot).grid(
            row=0, column=0, padx=(0, 10), pady=(0, 8)
        )
        tb.Button(rb_top, text="Обновить", bootstyle="outline", command=self._rb_take_snapshot).grid(
            row=0, column=1, padx=(0, 10), pady=(0, 8)
        )

        self.rb_tool_var = tk.StringVar(value="click")
        tb.Label(rb_top, text="Инструмент:", bootstyle="secondary").grid(row=0, column=2, padx=(10, 6), pady=(0, 8))
        tb.Radiobutton(rb_top, text="Точка", value="click", variable=self.rb_tool_var, bootstyle="toolbutton").grid(
            row=0, column=3, pady=(0, 8)
        )
        tb.Radiobutton(rb_top, text="Клавиша", value="key", variable=self.rb_tool_var, bootstyle="toolbutton").grid(
            row=0, column=4, padx=(6, 0), pady=(0, 8)
        )
        tb.Radiobutton(rb_top, text="Пауза", value="wait", variable=self.rb_tool_var, bootstyle="toolbutton").grid(
            row=0, column=5, padx=(6, 0), pady=(0, 8)
        )
        tb.Radiobutton(rb_top, text="Удалить", value="delete", variable=self.rb_tool_var, bootstyle="toolbutton").grid(
            row=0, column=6, padx=(6, 0), pady=(0, 8)
        )

        tb.Label(rb_top, text="Delay от/до (сек):", bootstyle="secondary").grid(row=0, column=7, padx=(14, 6), pady=(0, 8))
        self.rb_delay_from_var = tk.DoubleVar(value=0.20)
        self.rb_delay_to_var = tk.DoubleVar(value=0.0)
        tb.Spinbox(rb_top, from_=0.0, to=10.0, increment=0.01, textvariable=self.rb_delay_from_var, width=7).grid(
            row=0, column=8, pady=(0, 8)
        )
        tb.Spinbox(rb_top, from_=0.0, to=10.0, increment=0.01, textvariable=self.rb_delay_to_var, width=7).grid(
            row=0, column=9, padx=(6, 0), pady=(0, 8)
        )

        rb_body = tb.Frame(self.tab_route)
        rb_body.grid(row=1, column=0, sticky="nsew", pady=(10, 0))
        rb_body.grid_rowconfigure(0, weight=1)
        rb_body.grid_columnconfigure(0, weight=3)
        rb_body.grid_columnconfigure(1, weight=2)

        # Canvas with snapshot
        rb_canvas_box = tb.Labelframe(rb_body, text="Кликни по скрину, чтобы добавить шаг", padding=8, bootstyle="secondary")
        rb_canvas_box.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        rb_canvas_box.grid_rowconfigure(0, weight=1)
        rb_canvas_box.grid_columnconfigure(0, weight=1)

        self.rb_canvas = tk.Canvas(rb_canvas_box, highlightthickness=1)
        self.rb_canvas.grid(row=0, column=0, sticky="nsew")
        self.rb_canvas.bind("<ButtonPress-1>", self._rb_on_press)
        self.rb_canvas.bind("<ButtonPress-3>", self._rb_on_press)
        self.rb_canvas.bind("<B1-Motion>", self._rb_on_drag)
        self.rb_canvas.bind("<ButtonRelease-1>", self._rb_on_release)
        self.rb_canvas.bind("<ButtonRelease-3>", self._rb_on_release)
        self.rb_canvas.bind("<Configure>", lambda _e: self._rb_redraw())

        # Steps list for builder (sync with editor)
        rb_steps_box = tb.Labelframe(rb_body, text="Шаги маршрута", padding=8, bootstyle="secondary")
        rb_steps_box.grid(row=0, column=1, sticky="nsew")
        rb_steps_box.grid_rowconfigure(0, weight=1)
        rb_steps_box.grid_columnconfigure(0, weight=1)

        self.rb_steps_list = tk.Listbox(rb_steps_box)
        self.rb_steps_list.grid(row=0, column=0, sticky="nsew")
        rb_steps_scroll = tb.Scrollbar(rb_steps_box, orient="vertical", command=self.rb_steps_list.yview)
        rb_steps_scroll.grid(row=0, column=1, sticky="ns")
        self.rb_steps_list.configure(yscrollcommand=rb_steps_scroll.set)
        self.rb_steps_list.bind("<<ListboxSelect>>", lambda _e: self._rb_highlight_selected())
        self.rb_steps_list.bind("<Button-3>", lambda e: self._on_steps_right_click(e, self.rb_steps_list))
        self.rb_steps_list.bind("<Double-1>", lambda e: self._on_steps_double_click(e, self.rb_steps_list))
        self.rb_steps_list.bind("<ButtonPress-1>", lambda e: self._on_steps_drag_start(e, self.rb_steps_list))
        self.rb_steps_list.bind("<B1-Motion>", lambda e: self._on_steps_drag_move(e, self.rb_steps_list))
        self.rb_steps_list.bind("<ButtonRelease-1>", lambda e: self._on_steps_drag_end(e, self.rb_steps_list))

        rb_btns = tb.Frame(rb_steps_box)
        rb_btns.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        rb_btns.grid_columnconfigure(0, weight=1)
        tb.Button(rb_btns, text="Удалить шаг", bootstyle="danger-outline", command=self._on_remove_step).grid(
            row=0, column=0, sticky="ew"
        )
        tb.Button(rb_btns, text="Очистить", bootstyle="outline", command=self._on_clear_steps).grid(
            row=0, column=1, padx=(10, 0)
        )

        # (Логи теперь отображаются внизу на вкладке "Настройки")

        # Initialize wizard text + first highlight
        self._route_wizard_render()

    def _labeled_spin(self, parent: ttk.Frame, label: str, var: tk.IntVar, r: int, c: int) -> None:
        cell = ttk.Frame(parent)
        cell.grid(row=r, column=c, sticky="ew", padx=(0, 12), pady=(0, 8))
        ttk.Label(cell, text=label).pack(side="left")
        ttk.Spinbox(cell, from_=0, to=10000, textvariable=var, width=10).pack(side="left", padx=8)

    def _refresh_points_list(self) -> None:
        # Profiles
        try:
            profs = self.store.list_profiles()
        except Exception:
            profs = ["default"]
        self.profile_combo["values"] = profs
        if self.store.active_profile_name in profs:
            self.profile_var.set(self.store.active_profile_name)
        elif profs:
            self.profile_var.set(profs[0])
            try:
                self.store.set_active_profile(profs[0])
            except Exception:
                pass

        p = self.store.get_active_profile()
        names = [r.name for r in p.routes]
        self.routes_combo["values"] = names
        self.setup_routes_combo["values"] = ["(нет)"] + names
        # Auto-buy route list
        try:
            self.auto_buy_route_combo["values"] = ["(нет)"] + names
            cur = (self.auto_buy_route_var.get() or "").strip()
            if cur and cur not in (["(нет)"] + names):
                self.auto_buy_route_var.set("(нет)")
        except Exception:
            pass

        # Setup route selection
        setup_name = p.setup_route_name or "(нет)"
        if setup_name not in (["(нет)"] + names):
            setup_name = "(нет)"
        self.setup_route_var.set(setup_name)

        if self.store.active_route_name in names:
            self.active_route_var.set(self.store.active_route_name or "")
        elif names:
            self.active_route_var.set(names[0])
            self.store.set_active_route(names[0])
        else:
            self.active_route_var.set("")

    def _on_select_profile(self, _evt=None) -> None:
        name = (self.profile_var.get() or "").strip()
        if not name:
            return
        self.store.set_active_profile(name)
        self._refresh_points_list()
        r = self.store.get_active_route()
        if r:
            self._load_route_into_editor(r)

    def _on_new_profile(self) -> None:
        name = simpledialog.askstring("Профиль", "Имя профиля (локации):")
        if not name:
            return
        self.store.add_profile(name.strip())
        self._refresh_points_list()

    def _on_delete_profile(self) -> None:
        name = (self.profile_var.get() or "").strip()
        if not name:
            return
        if name == "default":
            messagebox.showwarning("Нельзя", "Нельзя удалить профиль 'default'.")
            return
        if messagebox.askyesno("Подтверждение", f"Удалить профиль {name!r}?"):
            self.store.delete_profile(name)
            self._refresh_points_list()

    def _on_select_setup_route(self, _evt=None) -> None:
        p = self.store.get_active_profile()
        val = (self.setup_route_var.get() or "").strip()
        p.setup_route_name = None if (not val or val == "(нет)") else val
        self.store.save()

    def _load_route_into_editor(self, r: Route) -> None:
        self.edit_route_name_var.set(r.name)
        self._editor_steps = list(r.steps)
        self._render_steps()

    def _render_steps(self) -> None:
        self.steps_list.delete(0, "end")
        for i, s in enumerate(getattr(self, "_editor_steps", []), start=1):
            if s.kind == "key":
                label = f"{i}. KEY {s.key}  (delay {s.delay_s:.2f}s)"
            elif s.kind == "confirm":
                label = f"{i}. CONFIRM (timeout {getattr(s, 'timeout_s', 6.0):.1f}s)"
            elif s.kind == "wait_range":
                label = f"{i}. WAIT_RANGE ({getattr(s, 'min_s', 0.0):.1f}-{getattr(s, 'max_s', 0.0):.1f}s)"
            elif s.kind == "gate":
                label = f"{i}. GATE (seek+turn+click)"
            elif s.kind == "wait":
                label = f"{i}. WAIT  (delay {s.delay_s:.2f}s)"
            else:
                label = f"{i}. CLICK ({s.rel_x},{s.rel_y}) {s.button}  (delay {s.delay_s:.2f}s)"
            self.steps_list.insert("end", label)
        # Sync builder list if present
        if hasattr(self, "rb_steps_list"):
            try:
                self.rb_steps_list.delete(0, "end")
                for i, s in enumerate(getattr(self, "_editor_steps", []), start=1):
                    if s.kind == "key":
                        label = f"{i}. KEY {s.key}  (delay {s.delay_s:.2f}s)"
                    elif s.kind == "confirm":
                        label = f"{i}. CONFIRM (timeout {getattr(s, 'timeout_s', 6.0):.1f}s)"
                    elif s.kind == "wait_range":
                        label = f"{i}. WAIT_RANGE ({getattr(s, 'min_s', 0.0):.1f}-{getattr(s, 'max_s', 0.0):.1f}s)"
                    elif s.kind == "gate":
                        label = f"{i}. GATE (seek+turn+click)"
                    elif s.kind == "wait":
                        label = f"{i}. WAIT  (delay {s.delay_s:.2f}s)"
                    else:
                        label = f"{i}. CLICK ({s.rel_x},{s.rel_y}) {s.button}  (delay {s.delay_s:.2f}s)"
                    self.rb_steps_list.insert("end", label)
            except Exception:
                pass
        # Redraw markers if routebuilder is active
        try:
            self._rb_redraw()
        except Exception:
            pass

    def _get_selected_step_index(self, listbox: tk.Listbox) -> int | None:
        try:
            sel = listbox.curselection()
            if not sel:
                return None
            return int(sel[0])
        except Exception:
            return None

    def _on_steps_drag_start(self, evt, listbox: tk.Listbox) -> None:
        try:
            idx = int(listbox.nearest(evt.y))
        except Exception:
            return
        self._steps_drag = {"src": idx, "last": idx}
        try:
            listbox.selection_clear(0, "end")
            listbox.selection_set(idx)
        except Exception:
            pass
        self._rb_highlight_selected()

    def _on_steps_drag_move(self, evt, listbox: tk.Listbox) -> None:
        if not hasattr(self, "_steps_drag") or not self._steps_drag:
            return
        try:
            dst = int(listbox.nearest(evt.y))
        except Exception:
            return
        src = int(self._steps_drag.get("src", dst))
        last = int(self._steps_drag.get("last", src))
        if dst == last or dst == src:
            return
        steps = list(getattr(self, "_editor_steps", []))
        if not (0 <= src < len(steps) and 0 <= dst < len(steps)):
            return
        # Move one step at a time for stable feel.
        item = steps.pop(src)
        steps.insert(dst, item)
        self._editor_steps = steps
        self._steps_drag["src"] = dst
        self._steps_drag["last"] = dst
        self._render_steps()
        try:
            listbox.selection_clear(0, "end")
            listbox.selection_set(dst)
            listbox.see(dst)
        except Exception:
            pass

    def _on_steps_drag_end(self, _evt, _listbox: tk.Listbox) -> None:
        try:
            self._steps_drag = None
        except Exception:
            pass

    def _on_steps_right_click(self, evt, listbox: tk.Listbox) -> None:
        idx = None
        try:
            idx = int(listbox.nearest(evt.y))
            listbox.selection_clear(0, "end")
            listbox.selection_set(idx)
        except Exception:
            return
        self._rb_highlight_selected()
        self._show_steps_menu(evt, idx)

    def _on_steps_double_click(self, _evt, listbox: tk.Listbox) -> None:
        idx = self._get_selected_step_index(listbox)
        if idx is None:
            return
        self._edit_step_delay(idx)

    def _show_steps_menu(self, evt, idx: int) -> None:
        if self._steps_menu is None:
            self._steps_menu = tk.Menu(self.root, tearoff=0)
        m = self._steps_menu
        m.delete(0, "end")

        m.add_command(label="Редактировать delay...", command=lambda: self._edit_step_delay(idx))
        m.add_command(label="Редактировать key...", command=lambda: self._edit_step_key(idx))
        m.add_command(label="Редактировать click...", command=lambda: self._edit_step_click(idx))
        m.add_command(label="Редактировать CONFIRM...", command=lambda: self._edit_step_confirm(idx))
        m.add_separator()
        m.add_command(label="Дублировать", command=lambda: self._duplicate_step(idx))
        m.add_command(label="Удалить", command=lambda: self._delete_step(idx))
        m.add_separator()
        m.add_command(label="Вставить WAIT после", command=lambda: self._insert_wait_after(idx))
        m.add_command(label="Вставить KEY после", command=lambda: self._insert_key_after(idx))
        m.add_command(label="Вставить CONFIRM после", command=lambda: self._insert_confirm_after(idx))
        m.add_command(label="Вставить WAIT RANGE после", command=lambda: self._insert_wait_range_after(idx))
        m.add_command(label="Вставить GATE после", command=lambda: self._insert_gate_after(idx))

        try:
            m.tk_popup(evt.x_root, evt.y_root)
        finally:
            try:
                m.grab_release()
            except Exception:
                pass

    def _edit_step_delay(self, idx: int) -> None:
        if not (0 <= idx < len(self._editor_steps)):
            return
        step = self._editor_steps[idx]
        val = simpledialog.askfloat("Delay", "Задержка после шага (сек):", initialvalue=float(step.delay_s))
        if val is None:
            return
        step.delay_s = float(max(0.0, val))
        self._render_steps()

    def _edit_step_key(self, idx: int) -> None:
        if not (0 <= idx < len(self._editor_steps)):
            return
        step = self._editor_steps[idx]
        if step.kind != "key":
            # Optionally convert to key step
            if not messagebox.askyesno("KEY", "Этот шаг не KEY. Превратить его в KEY шаг?"):
                return
            step.kind = "key"
        key = simpledialog.askstring("Key", "Клавиша (например f4, r, space, x):", initialvalue=(step.key or ""))
        if not key:
            return
        step.key = key.strip().lower()
        self._render_steps()

    def _duplicate_step(self, idx: int) -> None:
        if not (0 <= idx < len(self._editor_steps)):
            return
        s = self._editor_steps[idx]
        dup = RouteStep(
            kind=str(s.kind),
            rel_x=int(getattr(s, "rel_x", 0)),
            rel_y=int(getattr(s, "rel_y", 0)),
            button=str(getattr(s, "button", "left")),
            key=str(getattr(s, "key", "")),
            delay_s=float(getattr(s, "delay_s", 0.15)),
        )
        # copy normalized coords if present
        if getattr(s, "x_pct", None) is not None and getattr(s, "y_pct", None) is not None:
            dup.x_pct = float(s.x_pct)  # type: ignore[attr-defined]
            dup.y_pct = float(s.y_pct)  # type: ignore[attr-defined]
        self._editor_steps.insert(idx + 1, dup)
        self._render_steps()

    def _delete_step(self, idx: int) -> None:
        if not (0 <= idx < len(self._editor_steps)):
            return
        self._editor_steps.pop(idx)
        self._render_steps()

    def _insert_wait_after(self, idx: int) -> None:
        delay = self._rb_pick_delay_s() if hasattr(self, "_rb_pick_delay_s") else 0.2
        self._editor_steps.insert(idx + 1, RouteStep(kind="wait", delay_s=delay))
        self._render_steps()

    def _insert_key_after(self, idx: int) -> None:
        key = simpledialog.askstring("Клавиша", "Введите клавишу (например f4, r, space, x):")
        if not key:
            return
        delay = self._rb_pick_delay_s() if hasattr(self, "_rb_pick_delay_s") else 0.2
        self._editor_steps.insert(idx + 1, RouteStep(kind="key", key=key.strip().lower(), delay_s=delay))
        self._render_steps()

    def _insert_confirm_after(self, idx: int) -> None:
        try:
            t = simpledialog.askfloat("CONFIRM", "Timeout (сек) ждать окно подтверждения:", initialvalue=6.0)
        except Exception:
            t = 6.0
        if t is None:
            return
        delay = self._rb_pick_delay_s() if hasattr(self, "_rb_pick_delay_s") else 0.2
        self._editor_steps.insert(idx + 1, RouteStep(kind="confirm", timeout_s=float(max(1.0, t)), delay_s=delay))
        self._render_steps()

    def _insert_wait_range_after(self, idx: int) -> None:
        mn = simpledialog.askfloat("WAIT RANGE", "Мин (сек):", initialvalue=3.0)
        if mn is None:
            return
        mx = simpledialog.askfloat("WAIT RANGE", "Макс (сек):", initialvalue=max(3.0, float(mn) + 2.0))
        if mx is None:
            return
        self._editor_steps.insert(idx + 1, RouteStep(kind="wait_range", min_s=float(mn), max_s=float(mx), delay_s=0.0))
        self._render_steps()

    def _insert_gate_after(self, idx: int) -> None:
        self._editor_steps.insert(idx + 1, RouteStep(kind="gate", delay_s=0.2))
        self._render_steps()

    def _edit_step_click(self, idx: int) -> None:
        if not (0 <= idx < len(self._editor_steps)):
            return
        step = self._editor_steps[idx]
        if step.kind != "click":
            if not messagebox.askyesno("CLICK", "Этот шаг не CLICK. Превратить его в CLICK шаг?"):
                return
            step.kind = "click"
        x = simpledialog.askinteger("CLICK", "rel_x (px):", initialvalue=int(getattr(step, "rel_x", 0)))
        if x is None:
            return
        y = simpledialog.askinteger("CLICK", "rel_y (px):", initialvalue=int(getattr(step, "rel_y", 0)))
        if y is None:
            return
        btn = simpledialog.askstring("CLICK", "Кнопка (left/right/middle/x1/x2):", initialvalue=str(getattr(step, "button", "left") or "left"))
        if not btn:
            return
        step.rel_x = int(x)
        step.rel_y = int(y)
        step.button = str(btn).strip().lower()
        self._render_steps()

    def _edit_step_confirm(self, idx: int) -> None:
        if not (0 <= idx < len(self._editor_steps)):
            return
        step = self._editor_steps[idx]
        if step.kind != "confirm":
            if not messagebox.askyesno("CONFIRM", "Этот шаг не CONFIRM. Превратить его в CONFIRM шаг?"):
                return
            step.kind = "confirm"
        t = simpledialog.askfloat("CONFIRM", "Timeout (сек):", initialvalue=float(getattr(step, "timeout_s", 6.0)))
        if t is None:
            return
        step.timeout_s = float(max(1.0, t))
        self._render_steps()

    def _on_select_active_route(self, _evt=None) -> None:
        name = self.active_route_var.get().strip()
        self.store.set_active_route(name if name else None)
        r = self.store.get_active_route()
        if r:
            self._load_route_into_editor(r)

    def _on_new_route(self) -> None:
        base = "route"
        idx = 1
        existing = {r.name for r in self.store.get_active_profile().routes}
        name = f"{base}_{idx}"
        while name in existing:
            idx += 1
            name = f"{base}_{idx}"
        self.edit_route_name_var.set(name)
        self._editor_steps = []
        self._render_steps()

        # Auto-start recording workflow (as requested)
        self._on_start_recording(auto=True)

    def _on_save_route(self) -> None:
        name = self.edit_route_name_var.get().strip()
        if not name:
            messagebox.showerror("Ошибка", "Укажи имя маршрута.")
            return
        steps = list(getattr(self, "_editor_steps", []))
        if not steps:
            messagebox.showerror("Ошибка", "Маршрут пустой. Добавь шаги.")
            return
        r = Route(name=name, steps=steps)
        self.store.add_route(r)
        self.store.set_active_route(name)
        self._refresh_points_list()
        self.active_route_var.set(name)
        self.log(f"Маршрут сохранён: {name} (шагов: {len(steps)})")

    def _on_delete_route(self) -> None:
        name = self.active_route_var.get().strip()
        if not name:
            return
        if messagebox.askyesno("Подтверждение", f"Удалить маршрут {name!r}?"):
            self.store.delete_route(name)
            self._refresh_points_list()
            self._editor_steps = []
            self._render_steps()

    def _ensure_window_selected(self) -> bool:
        ok = self._on_confirm_window(show_errors=True)
        if not ok:
            return False
        # Validate hwnd if present; if invalid, try to reacquire by title.
        hwnd = int(getattr(self.store, "window_hwnd", 0) or 0)
        if hwnd and not bool(win32gui.IsWindow(hwnd)):
            title = (self.store.window_title or self.window_title_var.get().strip())
            try:
                hwnd = find_window_by_title(title)
                self.store.window_hwnd = int(hwnd)
                self.store.save()
                self.log(f"HWND обновлён по названию окна: 0x{int(hwnd):X}")
            except Exception:
                messagebox.showerror("Ошибка", "Окно игры не найдено/дескриптор устарел. Нажми «Взять активное» и «Подтвердить».")
                return False
        return True

    def _on_confirm_window(self, show_errors: bool = False) -> bool:
        s = self.window_title_var.get().strip()
        if not s:
            if show_errors:
                messagebox.showerror("Ошибка", "Выбери окно из списка или нажми 'Взять активное'.")
            return False

        parsed = parse_window_item(s)
        if parsed:
            title, hwnd = parsed
            self.store.window_title = title.strip()
            self.store.window_hwnd = int(hwnd)
            self.store.save()
            self.bot.window_title = self.store.window_title
            self.log(f"Окно подтверждено: {self.store.window_title!r} (0x{self.store.window_hwnd:X})")
            return True

        # fallback: plain title (older state). Still save it, but hwnd unknown.
        self.store.window_title = s
        self.store.window_hwnd = 0
        self.store.save()
        self.bot.window_title = s
        if show_errors:
            messagebox.showwarning(
                "Внимание",
                "Окно подтверждено только по названию (HWND не распознан). Лучше выбрать пункт вида 'Название (0x...)'.",
            )
        return True

    def _set_recording_ui(self, recording: bool) -> None:
        self.record_btn.configure(state="disabled" if recording else "normal")
        self.stop_record_btn.configure(state="normal" if recording else "disabled")
        try:
            self.record_insert_wait_btn.configure(state=("normal" if recording else "disabled"))
        except Exception:
            pass
        try:
            self.record_insert_confirm_btn.configure(state=("normal" if recording else "disabled"))
        except Exception:
            pass

    def _on_record_insert_wait(self) -> None:
        if self._recorder is None:
            return
        try:
            s = float(self.record_wait_s_var.get())
        except Exception:
            s = 1.0
        s = max(0.05, min(120.0, float(s)))
        try:
            self._recorder.insert_wait_marker(s)
            self.log(f"WAIT вставлен: {s:.2f} сек")
            self.root.after(0, self._render_steps)
        except Exception as e:
            self.log(f"WAIT: ошибка {e!r}")

    def _on_record_insert_confirm(self) -> None:
        """
        Insert a CONFIRM marker step: wait for confirm popup template, then press confirm key.
        """
        try:
            s = float(self.record_confirm_timeout_var.get())
        except Exception:
            s = 6.0
        s = max(1.0, min(30.0, float(s)))
        step = RouteStep(kind="confirm", timeout_s=float(s), delay_s=0.10)
        self._editor_steps.append(step)
        self.log(f"CONFIRM вставлен (timeout {s:.1f} сек)")
        self.root.after(0, self._render_steps)

    def _on_start_recording(self, auto: bool = False) -> None:
        if self._recorder is not None:
            return
        if not self._ensure_window_selected():
            return

        # Prefer hwnd stored from selection; fallback by exact title
        hwnd = int(getattr(self.store, "window_hwnd", 0) or 0)
        if hwnd == 0:
            title = (self.store.window_title or self.window_title_var.get().strip())
            try:
                hwnd = find_window_by_title(title)
            except Exception as e:
                messagebox.showerror("Ошибка", str(e))
                return

        # Put the game window in foreground for comfortable recording
        bring_window_to_foreground(hwnd)
        time.sleep(0.15)
        # Minimize UI so keys like M go to the game window
        try:
            self.root.iconify()
        except Exception:
            pass
        # Re-assert focus after minimizing (some systems steal focus back)
        try:
            self.root.after(250, lambda: bring_window_to_foreground(hwnd))
        except Exception:
            pass

        if auto:
            self.log("Фокус на игре. Запись маршрута активна: клики/клавиши будут добавляться в список шагов.")
        else:
            self.log("Запись маршрута активна.")
        try:
            hk = str(getattr(self.store, "stop_record_hotkey", "f9") or "f9").strip().upper()
        except Exception:
            hk = "F9"
        self.log(f"Подсказка: чтобы быстро остановить запись в игре — нажми {hk}.")

        cfg = RecorderConfig(min_step_delay_s=0.05)

        def on_step(step: RouteStep) -> None:
            self._editor_steps.append(step)
            # UI updates must happen on main thread
            self.root.after(0, self._render_steps)

        def on_log(msg: str) -> None:
            self.root.after(0, lambda: self.log(msg))

        self._recorder = RouteRecorder(game_hwnd=hwnd, cfg=cfg, on_step=on_step, on_log=on_log)
        self._recorder.start()
        self._set_recording_ui(True)

    def _on_stop_recording(self) -> None:
        if self._recorder is None:
            return
        self._recorder.stop()
        self._recorder = None
        self._set_recording_ui(False)
        try:
            self.root.deiconify()
            self.root.lift()
        except Exception:
            pass
        self.log(f"Запись остановлена. Шагов: {len(self._editor_steps)}")

    def _on_add_click_step(self) -> None:
        if not self._ensure_window_selected():
            return
        try:
            # record point uses current mouse position relative to the selected window
            p = self.bot.record_point_from_mouse(name="tmp")
        except Exception as e:
            messagebox.showerror("Ошибка", str(e))
            return

        step = RouteStep(kind="click", rel_x=p.rel_x, rel_y=p.rel_y, button="left", delay_s=0.20)
        self._editor_steps = list(getattr(self, "_editor_steps", [])) + [step]
        self._render_steps()

    def _on_add_key_step(self) -> None:
        if not self._ensure_window_selected():
            return
        key = simpledialog.askstring("Клавиша", "Введите клавишу (например f4, r, 1):")
        if not key:
            return
        step = RouteStep(kind="key", key=key.strip().lower(), delay_s=0.20)
        self._editor_steps = list(getattr(self, "_editor_steps", [])) + [step]
        self._render_steps()

    def _on_remove_step(self) -> None:
        sel = self.steps_list.curselection()
        if not sel:
            return
        idx = int(sel[0])
        steps = list(getattr(self, "_editor_steps", []))
        if 0 <= idx < len(steps):
            steps.pop(idx)
            self._editor_steps = steps
            self._render_steps()

    def _on_clear_steps(self) -> None:
        self._editor_steps = []
        self._render_steps()

    # --- Route builder handlers ---
    def _rb_get_hwnd(self) -> int:
        hwnd = int(getattr(self.store, "window_hwnd", 0) or 0)
        if hwnd:
            return hwnd
        title = (self.store.window_title or self.window_title_var.get().strip())
        return find_window_by_title(title)

    def _rb_take_snapshot(self) -> None:
        if not self._ensure_window_selected():
            return
        hwnd = 0
        try:
            hwnd = self._rb_get_hwnd()
        except Exception as e:
            messagebox.showerror("Ошибка", f"Не удалось получить окно: {e}")
            return

        # Prefer window capture (doesn't include our UI overlay)
        cap = self._capture_client_image_pil(hwnd)
        if cap is not None:
            pil, w, h = cap
            self._routebuilder_snapshot = pil
            self._routebuilder_snapshot_size = (w, h)
            self.log(f"Скрин окна (без оверлеев) сохранён для конструктора: {w}x{h}")
            self._rb_redraw()
            return

        # Fallback: temporarily hide UI, focus game, capture from screen, then restore UI.
        was_iconified = False
        try:
            try:
                if str(self.root.state()) != "iconic":
                    self.root.iconify()
                    was_iconified = True
            except Exception:
                pass

            bring_window_to_foreground(hwnd)
            time.sleep(0.18)

            cl, ct, cr, cb = win32gui.GetClientRect(hwnd)
            (left, top) = win32gui.ClientToScreen(hwnd, (int(cl), int(ct)))
            (right, bottom) = win32gui.ClientToScreen(hwnd, (int(cr), int(cb)))
            w = max(1, int(right - left))
            h = max(1, int(bottom - top))
            with mss() as sct:
                img = np.array(sct.grab({"left": int(left), "top": int(top), "width": w, "height": h}))  # BGRA
            bgr = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
            rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
            self._routebuilder_snapshot = Image.fromarray(rgb)
            self._routebuilder_snapshot_size = (w, h)
            self.log(f"Скрин окна (fallback) сохранён для конструктора: {w}x{h}")
            self._rb_redraw()
        finally:
            if was_iconified:
                try:
                    self.root.deiconify()
                    self.root.lift()
                except Exception:
                    pass

    def _rb_redraw(self) -> None:
        if not hasattr(self, "rb_canvas"):
            return
        self.rb_canvas.delete("all")
        self._routebuilder_markers = []
        self._routebuilder_step_to_marker = {}
        self._routebuilder_marker_to_step = {}

        if self._routebuilder_snapshot is None:
            self.rb_canvas.create_text(
                10,
                10,
                anchor="nw",
                text="Нажми 'Снять скрин окна игры', затем ставь точки кликом.",
                fill="#aaa",
            )
            return

        cw = max(1, int(self.rb_canvas.winfo_width()))
        ch = max(1, int(self.rb_canvas.winfo_height()))
        iw, ih = self._routebuilder_snapshot.size
        scale = min(cw / max(1, iw), ch / max(1, ih))
        scale = max(0.1, float(scale))
        self._routebuilder_scale = scale
        disp = self._routebuilder_snapshot.resize((int(iw * scale), int(ih * scale)), Image.Resampling.LANCZOS)
        self._routebuilder_photo = ImageTk.PhotoImage(disp)
        self.rb_canvas.create_image(0, 0, image=self._routebuilder_photo, anchor="nw")

        # Draw markers for click steps
        r = 7
        iw, ih = self._routebuilder_snapshot.size
        for idx, step in enumerate(getattr(self, "_editor_steps", [])):
            if step.kind != "click":
                continue
            rx = int(step.rel_x)
            ry = int(step.rel_y)
            if getattr(step, "x_pct", None) is not None and getattr(step, "y_pct", None) is not None:
                rx = int(float(step.x_pct) * max(1, iw))  # type: ignore[arg-type]
                ry = int(float(step.y_pct) * max(1, ih))  # type: ignore[arg-type]
            x = int(rx * scale)
            y = int(ry * scale)
            marker = self.rb_canvas.create_oval(x - r, y - r, x + r, y + r, outline="cyan", width=2)
            label = self.rb_canvas.create_text(x, y - 14, text=str(idx + 1), fill="cyan")
            self._routebuilder_markers.append(marker)
            self._routebuilder_step_to_marker[idx] = marker
            self._routebuilder_marker_to_step[marker] = idx
            # store label mapping too (for hit-testing we use the circle)
            self.rb_canvas.tag_raise(label)

        self._rb_highlight_selected()

    def _rb_canvas_to_rel(self, cx: int, cy: int) -> tuple[int, int]:
        scale = max(1e-9, float(getattr(self, "_routebuilder_scale", 1.0) or 1.0))
        return int(cx / scale), int(cy / scale)

    def _rb_apply_click_coords(self, step: RouteStep, rx: int, ry: int) -> None:
        # Store both integer coords (for display/back-compat) and normalized pct.
        step.rel_x = int(rx)
        step.rel_y = int(ry)
        try:
            iw, ih = getattr(self, "_routebuilder_snapshot_size", self._routebuilder_snapshot.size)  # type: ignore[union-attr]
            step.x_pct = float(rx) / max(1.0, float(iw))
            step.y_pct = float(ry) / max(1.0, float(ih))
        except Exception:
            step.x_pct = None
            step.y_pct = None

    def _rb_find_marker_at(self, x: int, y: int) -> int | None:
        # find closest oval within radius
        hits = self.rb_canvas.find_overlapping(x - 8, y - 8, x + 8, y + 8)
        for item in hits:
            if item in self._routebuilder_marker_to_step:
                return item
        return None

    def _rb_on_press(self, evt) -> None:
        if self._routebuilder_snapshot is None:
            return
        tool = (self.rb_tool_var.get() or "click").strip().lower()
        self._routebuilder_drag = None
        btn = int(getattr(evt, "num", 1) or 1)  # 1=left, 3=right
        self._routebuilder_pressed = {"x": int(evt.x), "y": int(evt.y), "moved": False, "tool": tool, "btn": btn}
        if tool == "delete":
            marker = self._rb_find_marker_at(int(evt.x), int(evt.y))
            if marker is None:
                return
            step_idx = int(self._routebuilder_marker_to_step.get(marker, -1))
            if 0 <= step_idx < len(self._editor_steps):
                self._editor_steps.pop(step_idx)
                self._render_steps()
            return

        marker = self._rb_find_marker_at(int(evt.x), int(evt.y))
        if marker is None:
            return
        step_idx = int(self._routebuilder_marker_to_step.get(marker, -1))
        if step_idx < 0:
            return
        # Select corresponding step in lists
        try:
            self.steps_list.selection_clear(0, "end")
            self.steps_list.selection_set(step_idx)
            self.steps_list.see(step_idx)
        except Exception:
            pass
        try:
            self.rb_steps_list.selection_clear(0, "end")
            self.rb_steps_list.selection_set(step_idx)
            self.rb_steps_list.see(step_idx)
        except Exception:
            pass
        self._rb_highlight_selected()
        self._routebuilder_drag = {"marker": marker, "step_idx": step_idx}

    def _rb_on_drag(self, evt) -> None:
        if not self._routebuilder_drag or self._routebuilder_snapshot is None:
            try:
                if hasattr(self, "_routebuilder_pressed") and self._routebuilder_pressed:
                    self._routebuilder_pressed["moved"] = True
            except Exception:
                pass
            return
        step_idx = int(self._routebuilder_drag["step_idx"])
        if not (0 <= step_idx < len(self._editor_steps)):
            return
        step = self._editor_steps[step_idx]
        if step.kind != "click":
            return
        rx, ry = self._rb_canvas_to_rel(int(evt.x), int(evt.y))
        self._rb_apply_click_coords(step, rx, ry)
        self._render_steps()

    def _rb_on_release(self, evt) -> None:
        # If we were dragging a marker, finish.
        if self._routebuilder_drag is not None:
            self._routebuilder_drag = None
            self._routebuilder_pressed = None
            return

        # Otherwise treat as click to add a step.
        pressed = getattr(self, "_routebuilder_pressed", None)
        self._routebuilder_pressed = None
        if not pressed or self._routebuilder_snapshot is None:
            return
        if bool(pressed.get("moved")):
            return

        tool = (pressed.get("tool") or "click").strip().lower()
        if tool == "delete":
            return

        x = int(pressed.get("x", 0))
        y = int(pressed.get("y", 0))
        btn = int(pressed.get("btn") or int(getattr(evt, "num", 1) or 1))
        click_btn = "right" if btn == 3 else "left"
        delay = self._rb_pick_delay_s()
        if tool == "click":
            rx, ry = self._rb_canvas_to_rel(x, y)
            s = RouteStep(kind="click", rel_x=rx, rel_y=ry, button=click_btn, delay_s=delay)
            self._rb_apply_click_coords(s, rx, ry)
            self._editor_steps.append(s)
            self._render_steps()
        elif tool == "key":
            key = simpledialog.askstring("Клавиша", "Введите клавишу (например f4, r, space, x):")
            if not key:
                return
            self._editor_steps.append(RouteStep(kind="key", key=key.strip().lower(), delay_s=delay))
            self._render_steps()
        elif tool == "wait":
            self._editor_steps.append(RouteStep(kind="wait", delay_s=delay))
            self._render_steps()

    def _rb_highlight_selected(self) -> None:
        try:
            idx = None
            sel = None
            if hasattr(self, "rb_steps_list"):
                sel = self.rb_steps_list.curselection()
            if sel:
                idx = int(sel[0])
            else:
                sel2 = self.steps_list.curselection()
                if sel2:
                    idx = int(sel2[0])
            if idx is None:
                return
            marker = self._routebuilder_step_to_marker.get(idx)
            if marker:
                # simple highlight by changing outline
                for sidx, mid in self._routebuilder_step_to_marker.items():
                    self.rb_canvas.itemconfigure(mid, outline=("yellow" if sidx == idx else "cyan"))
        except Exception:
            pass

    def _rb_pick_delay_s(self) -> float:
        """
        Delay for steps added in the screenshot route-builder.
        If Delay to>0 and to>=from => random uniform(from,to), else use Delay "from" as fixed value.
        """
        try:
            d_from = max(0.0, float(getattr(self, "rb_delay_from_var").get()))
            d_to = max(0.0, float(getattr(self, "rb_delay_to_var").get()))
        except Exception:
            d_from, d_to = 0.0, 0.0
        if d_to > 0.0 and d_to >= d_from:
            return float(random.uniform(d_from, d_to))
        return float(d_from)

    def _on_pick_active_window(self) -> None:
        try:
            title = self.bot.select_active_window()
        except Exception as e:
            messagebox.showerror("Ошибка", str(e))
            return
        hwnd = int(getattr(self.store, "window_hwnd", 0) or 0)
        if hwnd:
            self.window_title_var.set(format_window_item(hwnd, title))
        else:
            self.window_title_var.set(title)
        self._on_refresh_windows()

    def _on_refresh_windows(self) -> None:
        items = [format_window_item(hwnd, title) for hwnd, title in list_visible_windows()]
        self.windows_combo["values"] = items
        cur = self.window_title_var.get().strip()
        if cur and cur in items:
            self.windows_combo.set(cur)
        elif items:
            # keep current if possible, else set first item
            if not cur:
                self.windows_combo.set(items[0])

    def _on_save_settings(self) -> None:
        def safe_int(var, fallback: int) -> int:
            try:
                return int(var.get())
            except (ValueError, TclError, TypeError):
                return int(fallback)

        def safe_float(var, fallback: float) -> float:
            try:
                return float(var.get())
            except (ValueError, TclError, TypeError):
                return float(fallback)

        self.store.detect_mode = "diff"
        self.store.teleport_key = (self.tp_key_var.get().strip() or "f4").lower()
        self.store.key_hold_s = safe_float(self.key_hold_var, getattr(self.store, "key_hold_s", 0.06))
        # File logging
        try:
            self.store.log_to_file_enabled = bool(getattr(self, "log_to_file_enabled_var").get())
            self.store.log_dir = str(getattr(self, "log_dir_var").get() or "logs").strip() or "logs"
        except Exception:
            pass
        # Hotkey
        try:
            self.store.pause_hotkey = str(getattr(self, "pause_hotkey_var").get() or "f8").strip().lower()
        except Exception:
            pass
        try:
            self.store.stop_record_hotkey = str(getattr(self, "stop_record_hotkey_var").get() or "f9").strip().lower()
        except Exception:
            pass
        # Scheduler
        try:
            self.store.schedule_enabled = bool(getattr(self, "schedule_enabled_var").get())
            self.store.schedule_start_hhmm = str(getattr(self, "schedule_start_var").get() or "02:00").strip()
            self.store.schedule_duration_h = safe_float(getattr(self, "schedule_duration_var"), float(getattr(self.store, "schedule_duration_h", 8.0)))
            self.store.schedule_mode = str(getattr(self, "schedule_mode_var").get() or "route").strip().lower()
        except Exception:
            pass
        # Random delay range between route steps (0/0 disables and uses per-step delays).
        d_from = max(0.0, safe_float(self.route_delay_from_var, float(getattr(self.store, "route_delay_min_s", 0.0))))
        d_to = max(0.0, safe_float(self.route_delay_to_var, float(getattr(self.store, "route_delay_max_s", 0.0))))
        if d_to > 0.0 and d_to < d_from:
            d_from, d_to = d_to, d_from
        self.store.route_delay_min_s = float(d_from)
        self.store.route_delay_max_s = float(d_to)
        self.store.tp_on_enemy_enabled = bool(self.tp_on_enemy_enabled_var.get())
        self.store.radar_detect_enabled = bool(self.radar_detect_enabled_var.get())
        self.store.post_tp_action = (self.post_tp_action_var.get().strip() or "none").lower()
        self.store.post_tp_key = (self.post_tp_key_var.get().strip() or "r").lower()
        self.store.post_tp_delay_s = safe_float(self.post_tp_delay_var, getattr(self.store, "post_tp_delay_s", 0.25))
        # Enemy alert
        try:
            self.store.enemy_alert_enabled = bool(getattr(self, "enemy_alert_enabled_var").get())
            self.store.enemy_alert_beeps = safe_int(
                getattr(self, "enemy_alert_beeps_var"), int(getattr(self.store, "enemy_alert_beeps", 2))
            )
            self.store.enemy_alert_interval_s = safe_float(
                getattr(self, "enemy_alert_interval_var"), float(getattr(self.store, "enemy_alert_interval_s", 8.0))
            )
            self.store.enemy_alert_sound_path = str(getattr(self, "enemy_alert_sound_path_var").get() or "").strip()
        except Exception:
            pass
        # Telegram
        try:
            self.store.telegram_enabled = bool(getattr(self, "telegram_enabled_var").get())
            self.store.telegram_interval_s = safe_float(
                getattr(self, "telegram_interval_var"), float(getattr(self.store, "telegram_interval_s", 30.0))
            )
            self.store.telegram_chat_id = str(getattr(self, "telegram_chat_id_var").get() or "").strip()
            self.store.telegram_bot_token = str(getattr(self, "telegram_token_var").get() or "").strip()
            self.store.telegram_send_on_attacked = True
        except Exception:
            pass
        self.store.auto_confirm_enabled = bool(self.auto_confirm_enabled_var.get())
        self.store.auto_confirm_key = (self.auto_confirm_key_var.get().strip() or "y").lower()
        self.store.auto_confirm_interval_s = safe_float(
            self.auto_confirm_interval_var, getattr(self.store, "auto_confirm_interval_s", 0.8)
        )
        self.store.focus_steal_enabled = bool(getattr(self, "focus_steal_enabled_var").get())
        self.store.tp_focus_steal_enabled = bool(getattr(self, "tp_focus_steal_enabled_var").get())
        self.store.farm_without_route = bool(self.farm_without_route_var.get())
        w_from = safe_int(self.wait_from_var, int(getattr(self.store, "teleport_wait_min_s", getattr(self.store, "teleport_wait_s", 60))))
        w_to = safe_int(self.wait_to_var, int(getattr(self.store, "teleport_wait_max_s", getattr(self.store, "teleport_wait_s", 60))))
        if w_to < w_from:
            w_from, w_to = w_to, w_from
        self.store.teleport_wait_min_s = int(w_from)
        self.store.teleport_wait_max_s = int(w_to)
        # legacy mirror
        self.store.teleport_wait_s = int(w_from)
        self.store.empty_text_roi.x = safe_int(self.text_roi_x, int(self.store.empty_text_roi.x))
        self.store.empty_text_roi.y = safe_int(self.text_roi_y, int(self.store.empty_text_roi.y))
        self.store.empty_text_roi.w = safe_int(self.text_roi_w, int(self.store.empty_text_roi.w))
        self.store.empty_text_roi.h = safe_int(self.text_roi_h, int(self.store.empty_text_roi.h))
        self.store.empty_text_threshold = safe_float(
            self.empty_text_threshold_var, getattr(self.store, "empty_text_threshold", 0.86)
        )
        # Auto-buy potions
        try:
            self.store.auto_buy_potions_enabled = bool(self.auto_buy_enabled_var.get())
            self.store.auto_buy_potions_threshold = int(self.auto_buy_threshold_var.get())
            self.store.auto_buy_city_wait_s = safe_float(self.auto_buy_city_wait_var, getattr(self.store, "auto_buy_city_wait_s", 8.0))
            self.store.auto_buy_route_mode_only = bool(self.auto_buy_route_mode_only_var.get())
            self.store.auto_buy_return_to_farm = bool(self.auto_buy_return_to_farm_var.get())
            self.store.auto_buy_potions_roi.x = safe_int(self.potion_roi_x, int(self.store.auto_buy_potions_roi.x))
            self.store.auto_buy_potions_roi.y = safe_int(self.potion_roi_y, int(self.store.auto_buy_potions_roi.y))
            self.store.auto_buy_potions_roi.w = safe_int(self.potion_roi_w, int(self.store.auto_buy_potions_roi.w))
            self.store.auto_buy_potions_roi.h = safe_int(self.potion_roi_h, int(self.store.auto_buy_potions_roi.h))
            rname = (self.auto_buy_route_var.get() or "").strip()
            self.store.auto_buy_potions_route_name = None if (not rname or rname == "(нет)") else rname
        except Exception:
            pass

        # Menu auto-close
        try:
            self.store.menu_autoclose_enabled = bool(getattr(self, "menu_autoclose_enabled_var").get())
            self.store.menu_autoclose_roi.x = safe_int(self.menu_roi_x, int(self.store.menu_autoclose_roi.x))
            self.store.menu_autoclose_roi.y = safe_int(self.menu_roi_y, int(self.store.menu_autoclose_roi.y))
            self.store.menu_autoclose_roi.w = safe_int(self.menu_roi_w, int(self.store.menu_autoclose_roi.w))
            self.store.menu_autoclose_roi.h = safe_int(self.menu_roi_h, int(self.store.menu_autoclose_roi.h))
            self.store.menu_autoclose_threshold = safe_float(
                self.menu_threshold_var, float(getattr(self.store, "menu_autoclose_threshold", 0.86))
            )
            self.store.menu_autoclose_attempts = safe_int(
                self.menu_attempts_var, int(getattr(self.store, "menu_autoclose_attempts", 2))
            )
        except Exception:
            pass

        # Confirm popup (gate enter)
        try:
            self.store.confirm_popup_roi.x = safe_int(self.confirm_roi_x, int(self.store.confirm_popup_roi.x))
            self.store.confirm_popup_roi.y = safe_int(self.confirm_roi_y, int(self.store.confirm_popup_roi.y))
            self.store.confirm_popup_roi.w = safe_int(self.confirm_roi_w, int(self.store.confirm_popup_roi.w))
            self.store.confirm_popup_roi.h = safe_int(self.confirm_roi_h, int(self.store.confirm_popup_roi.h))
            self.store.confirm_popup_threshold = safe_float(
                self.confirm_thr_var, float(getattr(self.store, "confirm_popup_threshold", 0.86))
            )
            self.store.confirm_popup_tpl_path = str(
                getattr(self.store, "confirm_popup_tpl_path", "confirm_popup_tpl.png") or "confirm_popup_tpl.png"
            )
        except Exception:
            pass

        # Gate assist (rotate+click)
        try:
            self.store.gate_roi.x = safe_int(self.gate_roi_x, int(self.store.gate_roi.x))
            self.store.gate_roi.y = safe_int(self.gate_roi_y, int(self.store.gate_roi.y))
            self.store.gate_roi.w = safe_int(self.gate_roi_w, int(self.store.gate_roi.w))
            self.store.gate_roi.h = safe_int(self.gate_roi_h, int(self.store.gate_roi.h))
            self.store.gate_threshold = safe_float(self.gate_thr_var, float(getattr(self.store, "gate_threshold", 0.83)))
            self.store.gate_seek_timeout_s = safe_float(
                self.gate_timeout_var, float(getattr(self.store, "gate_seek_timeout_s", 6.0))
            )
            self.store.gate_center_margin_px = safe_int(
                self.gate_margin_var, int(getattr(self.store, "gate_center_margin_px", 40))
            )
            self.store.gate_turn_step_px = safe_int(
                self.gate_turn_step_var, int(getattr(self.store, "gate_turn_step_px", 120))
            )
            self.store.gate_tpl_path = str(getattr(self.store, "gate_tpl_path", "gate_tpl.png") or "gate_tpl.png")
        except Exception:
            pass

        # Death detect
        try:
            self.store.death_detect_enabled = bool(getattr(self, "death_enabled_var").get())
            self.store.death_roi.x = safe_int(self.death_roi_x, int(self.store.death_roi.x))
            self.store.death_roi.y = safe_int(self.death_roi_y, int(self.store.death_roi.y))
            self.store.death_roi.w = safe_int(self.death_roi_w, int(self.store.death_roi.w))
            self.store.death_roi.h = safe_int(self.death_roi_h, int(self.store.death_roi.h))
            self.store.death_threshold = safe_float(self.death_threshold_var, float(getattr(self.store, "death_threshold", 0.86)))
            self.store.death_cooldown_s = safe_float(self.death_cooldown_var, float(getattr(self.store, "death_cooldown_s", 20.0)))
            rname = (self.death_route_var.get() or "").strip()
            self.store.death_route_name = None if (not rname or rname == "(нет)") else rname
        except Exception:
            pass

        # Damage/HP teleport mode
        try:
            self.store.damage_tp_enabled = bool(getattr(self, "damage_tp_enabled_var").get())
            self.store.damage_icon_roi.x = safe_int(self.damage_icon_roi_x, int(self.store.damage_icon_roi.x))
            self.store.damage_icon_roi.y = safe_int(self.damage_icon_roi_y, int(self.store.damage_icon_roi.y))
            self.store.damage_icon_roi.w = safe_int(self.damage_icon_roi_w, int(self.store.damage_icon_roi.w))
            self.store.damage_icon_roi.h = safe_int(self.damage_icon_roi_h, int(self.store.damage_icon_roi.h))
            # Normal(body) ROI can differ; fall back to swords ROI if missing.
            if not hasattr(self.store, "damage_icon_normal_roi"):
                try:
                    self.store.damage_icon_normal_roi = RadarROI.from_dict(self.store.damage_icon_roi.as_dict())  # type: ignore[attr-defined]
                except Exception:
                    pass
            try:
                self.store.damage_icon_normal_roi.x = safe_int(self.damage_icon_norm_roi_x, int(self.store.damage_icon_normal_roi.x))  # type: ignore[attr-defined]
                self.store.damage_icon_normal_roi.y = safe_int(self.damage_icon_norm_roi_y, int(self.store.damage_icon_normal_roi.y))  # type: ignore[attr-defined]
                self.store.damage_icon_normal_roi.w = safe_int(self.damage_icon_norm_roi_w, int(self.store.damage_icon_normal_roi.w))  # type: ignore[attr-defined]
                self.store.damage_icon_normal_roi.h = safe_int(self.damage_icon_norm_roi_h, int(self.store.damage_icon_normal_roi.h))  # type: ignore[attr-defined]
            except Exception:
                pass
            self.store.damage_icon_threshold = safe_float(
                self.damage_icon_threshold_var, float(getattr(self.store, "damage_icon_threshold", 0.86))
            )
            # keep template path stable (user can replace file manually)
            self.store.damage_icon_tpl_path = str(getattr(self.store, "damage_icon_tpl_path", "damage_icon_tpl.png") or "damage_icon_tpl.png")
            self.store.damage_icon_normal_threshold = safe_float(
                getattr(self, "damage_icon_normal_threshold_var"), float(getattr(self.store, "damage_icon_normal_threshold", 0.86))
            )
            self.store.damage_icon_margin = safe_float(
                getattr(self, "damage_icon_margin_var"), float(getattr(self.store, "damage_icon_margin", 0.04))
            )
            self.store.damage_icon_normal_tpl_path = str(
                getattr(self.store, "damage_icon_normal_tpl_path", "damage_icon_normal_tpl.png") or "damage_icon_normal_tpl.png"
            )
            self.store.hp_bar_roi.x = safe_int(self.hp_roi_x, int(self.store.hp_bar_roi.x))
            self.store.hp_bar_roi.y = safe_int(self.hp_roi_y, int(self.store.hp_bar_roi.y))
            self.store.hp_bar_roi.w = safe_int(self.hp_roi_w, int(self.store.hp_bar_roi.w))
            self.store.hp_bar_roi.h = safe_int(self.hp_roi_h, int(self.store.hp_bar_roi.h))
            self.store.hp_tp_enabled = bool(getattr(self, "hp_tp_enabled_var").get())
            self.store.hp_tp_threshold_pct = safe_int(self.hp_threshold_var, int(getattr(self.store, "hp_tp_threshold_pct", 70)))
            self.store.damage_tp_press_count = safe_int(
                self.damage_tp_press_count_var, int(getattr(self.store, "damage_tp_press_count", 6))
            )
            self.store.damage_tp_press_interval_s = safe_float(
                self.damage_tp_press_interval_var, float(getattr(self.store, "damage_tp_press_interval_s", 0.12))
            )
            self.store.damage_tp_cooldown_s = safe_float(
                self.damage_tp_cooldown_var, float(getattr(self.store, "damage_tp_cooldown_s", 8.0))
            )
        except Exception:
            pass
        self.store.save()
        self.log("Настройки сохранены")
        self._set_unsaved(False)

    def _on_pick_potion_roi(self) -> None:
        if not self._ensure_window_selected():
            return
        hwnd = int(getattr(self.store, "window_hwnd", 0) or 0)
        if hwnd == 0:
            messagebox.showerror("Ошибка", "Выбери окно вида 'Название (0x...)' и нажми 'Подтвердить'.")
            return

        def apply_roi(x: int, y: int, ww: int, hh: int) -> None:
            self.potion_roi_x.set(x)
            self.potion_roi_y.set(y)
            self.potion_roi_w.set(ww)
            self.potion_roi_h.set(hh)
            self._on_save_settings()
            self.log("ROI банок сохранён")

        # Prefer live overlay selection over the game window
        try:
            self._open_live_roi_overlay(hwnd, title="ROI банок", on_apply=apply_roi)
        except Exception:
            cap = self._capture_client_image_pil(hwnd)
            if cap is None:
                messagebox.showerror("Ошибка", "Не удалось открыть выбор ROI банок.")
                return
            pil, w, h = cap
            self._open_roi_selector_for_callback(pil, window_w=w, window_h=h, on_apply=apply_roi)

    def _on_test_potion_ocr(self) -> None:
        if not self._ensure_window_selected():
            return
        self._on_save_settings()
        try:
            hwnd = int(getattr(self.store, "window_hwnd", 0) or 0)
            if hwnd == 0:
                hwnd = self._rb_get_hwnd()
            roi = self.store.auto_buy_potions_roi
            if int(roi.w) <= 5 or int(roi.h) <= 5:
                self.log("OCR банок: ROI не задан (слишком маленький).")
                return
            # Grab ROI and attempt OCR + debug
            from ocr_utils import read_int_debug
            from vision import Vision

            img = Vision().grab_client_roi_bgr(hwnd, roi)
            val, dbg, bw = read_int_debug(img)
            # Show previews (raw + prepared mask)
            try:
                rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
                self._show_pil_popup(Image.fromarray(rgb), title="ROI банок (RAW)")
                self._show_pil_popup(Image.fromarray(bw), title="ROI банок (PREP mask)")
            except Exception:
                pass
            self._autobuy_log(
                f"OCR debug: method={dbg.get('method')}, components={dbg.get('components')}, "
                f"digit_count={dbg.get('digit_count')}, text={dbg.get('text')!r}, "
                f"joined={dbg.get('joined')!r}, digits={dbg.get('digits')}"
            )
            if val is None:
                self._autobuy_log("OCR банок: НЕ УДАЛОСЬ прочитать число. Уменьши ROI строго под цифры.")
                self.log("OCR банок: не удалось прочитать число (смотри вкладку «Авто банки» → логи/превью).")
            else:
                self._autobuy_log(f"OCR банок: {val}")
                self.log(f"OCR банок: {val}")
        except Exception as e:
            self.log(f"OCR банок: ошибка {e!r}")

    def _on_preview_potion_roi(self) -> None:
        if not self._ensure_window_selected():
            return
        self._on_save_settings()
        try:
            hwnd = int(getattr(self.store, "window_hwnd", 0) or 0)
            if hwnd == 0:
                hwnd = self._rb_get_hwnd()
            roi = self.store.auto_buy_potions_roi
            if int(roi.w) <= 5 or int(roi.h) <= 5:
                self.log("Превью ROI: ROI не задан (слишком маленький).")
                return
            from vision import Vision

            img = Vision().grab_client_roi_bgr(hwnd, roi)
            rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
            self._show_pil_popup(Image.fromarray(rgb), title="ROI банок (превью)")
            self._autobuy_log(f"Показать ROI: hwnd=0x{int(hwnd):X}, roi=({roi.x},{roi.y},{roi.w},{roi.h})")
        except Exception as e:
            self.log(f"Превью ROI: ошибка {e!r}")

    def _on_pick_enemy_alert_wav(self) -> None:
        try:
            initial = str(self.enemy_alert_sound_path_var.get() or "").strip()
            initial_dir = os.path.dirname(initial) if initial and os.path.isdir(os.path.dirname(initial)) else str(Path(".").resolve())
        except Exception:
            initial_dir = str(Path(".").resolve())
        path = filedialog.askopenfilename(
            title="Выбери WAV для сигнала",
            initialdir=initial_dir,
            filetypes=[("WAV", "*.wav"), ("All files", "*.*")],
        )
        if not path:
            return
        self.enemy_alert_sound_path_var.set(path)
        self._set_unsaved(True)

    def _on_apply_pause_hotkey(self) -> None:
        # Persist and re-register.
        try:
            self.store.pause_hotkey = str(self.pause_hotkey_var.get() or "f8").strip().lower()
            self.store.save()
        except Exception:
            pass
        self._register_pause_hotkey()

    def _on_apply_stop_record_hotkey(self) -> None:
        # Persist and re-register.
        try:
            self.store.stop_record_hotkey = str(self.stop_record_hotkey_var.get() or "f9").strip().lower()
            self.store.save()
        except Exception:
            pass
        self._register_stop_record_hotkey()

    def _on_autobuy_new_buy_route(self) -> None:
        """
        Create a new route intended for potion buying and load it into the left editor.
        User can then record steps or build by screenshot and save.
        """
        try:
            existing = {r.name for r in self.store.get_active_profile().routes}
            base = "buy_potions"
            idx = 1
            name = f"{base}_{idx}"
            while name in existing:
                idx += 1
                name = f"{base}_{idx}"
            # Prepare editor for new route
            self.edit_route_name_var.set(name)
            self._editor_steps = []
            self._render_steps()
            self._autobuy_log(f"Создан шаблон маршрута покупки: {name}. Запиши шаги и нажми «Сохранить маршрут».")
            # Preselect in combo (will be persisted on save)
            try:
                self.auto_buy_route_var.set(name)
                self._set_unsaved(True)
            except Exception:
                pass
            # Focus name entry / record button
            try:
                self.record_btn.focus_set()
            except Exception:
                pass
        except Exception as e:
            self._autobuy_log(f"Ошибка создания маршрута покупки: {e!r}")

    def _on_autobuy_use_current_route(self) -> None:
        """
        Set currently edited route name as auto-buy route.
        """
        try:
            name = (self.edit_route_name_var.get() or "").strip()
            if not name:
                self._autobuy_log("Назначить текущий: сначала укажи имя маршрута слева.")
                return
            self.auto_buy_route_var.set(name)
            self._set_unsaved(True)
            self._autobuy_log(f"Маршрут покупки установлен: {name!r}. Не забудь нажать «Сохранить».")
        except Exception as e:
            self._autobuy_log(f"Ошибка назначения маршрута: {e!r}")

    def _autobuy_log(self, msg: str) -> None:
        try:
            ts = time.strftime("%H:%M:%S")
            self.autobuy_log_text.configure(state="normal")
            self.autobuy_log_text.insert("end", f"[{ts}] {msg}\n")
            self.autobuy_log_text.see("end")
            self.autobuy_log_text.configure(state="disabled")
        except Exception:
            pass

    def _autobuy_log_clear(self) -> None:
        try:
            self.autobuy_log_text.configure(state="normal")
            self.autobuy_log_text.delete("1.0", "end")
            self.autobuy_log_text.configure(state="disabled")
        except Exception:
            pass

    def _on_tp_toggle_changed(self) -> None:
        # Apply immediately (no "Save" required).
        enabled = bool(self.tp_on_enemy_enabled_var.get())
        self.bot.set_tp_on_enemy_enabled(enabled)

    def _on_tp_focus_toggle_changed(self) -> None:
        enabled = bool(self.tp_focus_steal_enabled_var.get())
        try:
            self.bot.set_tp_focus_steal_enabled(enabled)
            self.log(f"Фокус для ТП: {'ВКЛ' if enabled else 'ВЫКЛ'}")
        except Exception:
            # fallback: still persist
            self.store.tp_focus_steal_enabled = bool(enabled)
            self.store.save()
            self.log(f"Фокус для ТП: {'ВКЛ' if enabled else 'ВЫКЛ'}")

    def _on_toggle_detect(self) -> None:
        new_state = self.bot.toggle_radar_detect()
        self.radar_detect_enabled_var.set(bool(new_state))

    def _on_radar_detect_toggle_changed(self) -> None:
        # Apply immediately (no "Save" required).
        enabled = bool(self.radar_detect_enabled_var.get())
        try:
            self.bot.set_radar_detect_enabled(enabled)
        except Exception:
            try:
                self.store.radar_detect_enabled = enabled
                self.store.save()
            except Exception:
                pass

    def _on_damage_tp_toggle_changed(self) -> None:
        # Apply immediately (no "Save" required).
        enabled = bool(self.damage_tp_enabled_var.get())
        try:
            self.store.damage_tp_enabled = enabled
            self.store.save()
        except Exception:
            pass
        try:
            self.log(f"ТП по урону: {'ВКЛ' if enabled else 'ВЫКЛ'}")
        except Exception:
            pass

    def _on_hp_tp_toggle_changed(self) -> None:
        enabled = bool(self.hp_tp_enabled_var.get())
        try:
            self.store.hp_tp_enabled = enabled
            self.store.save()
        except Exception:
            pass
        try:
            self.log(f"ТП по HP: {'ВКЛ' if enabled else 'ВЫКЛ'}")
        except Exception:
            pass

    def _open_roi_selector_for_callback(
        self, pil_img: Image.Image, window_w: int, window_h: int, on_apply
    ) -> None:
        """
        Same UI as ROI selector but calls on_apply(x,y,w,h) instead of writing to radar ROI.
        """
        top = tk.Toplevel(self.root)
        top.title("Выбор области — выдели прямоугольник и нажми Применить")
        top.geometry("1100x700")

        max_w, max_h = 1060, 620
        scale = min(1.0, max_w / max(1, window_w), max_h / max(1, window_h))
        disp_w = int(window_w * scale)
        disp_h = int(window_h * scale)
        disp_img = pil_img.resize((disp_w, disp_h), Image.Resampling.LANCZOS) if scale != 1.0 else pil_img

        photo = ImageTk.PhotoImage(disp_img)
        canvas = tk.Canvas(top, width=disp_w, height=disp_h, highlightthickness=1, highlightbackground="#888")
        canvas.pack(padx=10, pady=10)
        canvas.create_image(0, 0, image=photo, anchor="nw")
        canvas.image = photo

        state = {"x0": None, "y0": None, "rect": None, "x1": None, "y1": None}

        def on_down(evt):
            state["x0"], state["y0"] = evt.x, evt.y
            state["x1"], state["y1"] = evt.x, evt.y
            if state["rect"] is not None:
                canvas.delete(state["rect"])
            state["rect"] = canvas.create_rectangle(evt.x, evt.y, evt.x, evt.y, outline="red", width=2)

        def on_move(evt):
            if state["rect"] is None or state["x0"] is None:
                return
            state["x1"], state["y1"] = evt.x, evt.y
            canvas.coords(state["rect"], state["x0"], state["y0"], evt.x, evt.y)

        def on_up(evt):
            if state["rect"] is None:
                return
            state["x1"], state["y1"] = evt.x, evt.y
            canvas.coords(state["rect"], state["x0"], state["y0"], evt.x, evt.y)
            # Auto-apply on mouse release (same UX as live overlay)
            try:
                apply_any_roi()
            except Exception:
                pass

        canvas.bind("<ButtonPress-1>", on_down)
        canvas.bind("<B1-Motion>", on_move)
        canvas.bind("<ButtonRelease-1>", on_up)

        btn_row = ttk.Frame(top, padding=10)
        btn_row.pack(fill="x")

        def apply_any_roi():
            x0, y0, x1, y1 = state["x0"], state["y0"], state["x1"], state["y1"]
            if x0 is None or y0 is None or x1 is None or y1 is None:
                messagebox.showerror("Ошибка", "Сначала выдели область мышью.")
                return
            x_min, x_max = sorted([int(x0), int(x1)])
            y_min, y_max = sorted([int(y0), int(y1)])
            if (x_max - x_min) < 5 or (y_max - y_min) < 5:
                messagebox.showerror("Ошибка", "Слишком маленькая область. Выдели побольше.")
                return

            inv = 1.0 / max(1e-9, scale)
            roi_x = int(x_min * inv)
            roi_y = int(y_min * inv)
            roi_w = int((x_max - x_min) * inv)
            roi_h = int((y_max - y_min) * inv)
            on_apply(max(0, roi_x), max(0, roi_y), max(1, roi_w), max(1, roi_h))
            try:
                self._toast("Область сохранена")
            except Exception:
                pass
            top.destroy()

        # Auto-save on mouse release; keep only Cancel.
        ttk.Button(btn_row, text="Отмена", command=top.destroy).pack(side="left")

    def _open_live_roi_overlay(self, hwnd: int, *, title: str, on_apply) -> None:
        """
        ROI selection directly over the live game window (no screenshot).
        Draw a rectangle on a transparent overlay aligned to the game's client area.
        """
        try:
            bring_window_to_foreground(int(hwnd))
        except Exception:
            pass

        try:
            cl, ct, cr, cb = win32gui.GetClientRect(int(hwnd))
            (left, top) = win32gui.ClientToScreen(int(hwnd), (int(cl), int(ct)))
            (right, bottom) = win32gui.ClientToScreen(int(hwnd), (int(cr), int(cb)))
        except Exception as e:
            messagebox.showerror("Ошибка", f"Не удалось получить координаты окна: {e}")
            return

        w = max(1, int(right - left))
        h = max(1, int(bottom - top))

        overlay = tk.Toplevel(self.root)
        overlay.title(title)
        try:
            overlay.overrideredirect(True)
        except Exception:
            pass
        try:
            overlay.attributes("-topmost", True)
            # Make overlay readable.
            overlay.attributes("-alpha", 0.55)
        except Exception:
            pass
        overlay.geometry(f"{w}x{h}+{int(left)}+{int(top)}")

        canvas = tk.Canvas(overlay, width=w, height=h, highlightthickness=0, bg="black")
        canvas.pack(fill="both", expand=True)

        # Centered instruction (more visible than top-left bar)
        instr = canvas.create_text(
            int(w / 2),
            int(h / 2),
            text=f"{title}\nВыдели прямоугольник мышью\n(отпустил — сохранено, Esc — отмена)",
            fill="white",
            font=("Segoe UI Semibold", 18),
            justify="center",
        )

        state = {"x0": None, "y0": None, "x1": None, "y1": None, "rect": None}

        def _notify_saved(msg: str = "Область сохранена") -> None:
            try:
                canvas.itemconfigure(instr, text=msg)
            except Exception:
                pass
            try:
                self.log(msg)
            except Exception:
                pass
            try:
                self._toast(msg)
            except Exception:
                pass
            # no need to restore text; overlay closes on save

        def apply_now(close: bool = True):
            x0, y0, x1, y1 = state["x0"], state["y0"], state["x1"], state["y1"]
            if x0 is None or y0 is None or x1 is None or y1 is None:
                messagebox.showerror("Ошибка", "Сначала выдели область мышью.")
                return
            x_min, x_max = sorted([int(x0), int(x1)])
            y_min, y_max = sorted([int(y0), int(y1)])
            if (x_max - x_min) < 5 or (y_max - y_min) < 5:
                messagebox.showerror("Ошибка", "Слишком маленькая область. Выдели побольше.")
                return
            try:
                on_apply(max(0, x_min), max(0, y_min), max(1, x_max - x_min), max(1, y_max - y_min))
                _notify_saved("Область сохранена")
            finally:
                if close:
                    try:
                        overlay.destroy()
                    except Exception:
                        pass

        def cancel_now():
            try:
                overlay.destroy()
            except Exception:
                pass

        # No buttons: save on release, Esc to cancel.

        def on_down(evt):
            state["x0"], state["y0"] = int(evt.x), int(evt.y)
            state["x1"], state["y1"] = int(evt.x), int(evt.y)
            if state["rect"] is not None:
                try:
                    canvas.delete(state["rect"])
                except Exception:
                    pass
            state["rect"] = canvas.create_rectangle(evt.x, evt.y, evt.x, evt.y, outline="#00E5FF", width=3)

        def on_move(evt):
            if state["x0"] is None:
                return
            state["x1"], state["y1"] = int(evt.x), int(evt.y)
            if state["rect"] is not None:
                canvas.coords(state["rect"], int(state["x0"]), int(state["y0"]), int(evt.x), int(evt.y))

        def on_up(_evt):
            # Auto-apply on mouse release (expected UX)
            try:
                apply_now(True)
            except Exception:
                pass

        canvas.bind("<ButtonPress-1>", on_down)
        canvas.bind("<B1-Motion>", on_move)
        canvas.bind("<ButtonRelease-1>", on_up)
        overlay.bind("<Escape>", lambda _e: cancel_now())

        try:
            overlay.focus_force()
        except Exception:
            pass

    def _mini_toast(self, msg: str, ms: int = 1200) -> None:
        if not (self._mini_win and self._mini_win.winfo_exists()):
            return
        try:
            self._mini_toast_var.set(str(msg))
        except Exception:
            return
        # Also show centered, more visible banner in mini window.
        try:
            self._mini_center_toast(str(msg), ms=ms)
        except Exception:
            pass
        try:
            self._mini_win.after(ms, lambda: self._mini_toast_var.set(""))
        except Exception:
            pass

    def _mini_center_toast(self, msg: str, ms: int = 1200) -> None:
        if not (self._mini_win and self._mini_win.winfo_exists()):
            return

        # Lazy-create overlay label once.
        lbl = getattr(self, "_mini_center_toast_lbl", None)
        if not lbl:
            try:
                lbl = tb.Label(
                    self._mini_win,  # type: ignore[arg-type]
                    text="",
                    bootstyle="inverse-success",
                    font=("Segoe UI Semibold", 16),
                    padding=12,
                    justify="center",
                )
                lbl.place_forget()
                self._mini_center_toast_lbl = lbl
            except Exception:
                self._mini_center_toast_lbl = None
                return

        try:
            lbl.configure(text=str(msg))
            lbl.lift()
            # Slightly above center so it doesn't cover buttons too much.
            lbl.place(relx=0.5, rely=0.42, anchor="center")
        except Exception:
            return

        def _hide() -> None:
            try:
                if lbl.winfo_exists():
                    lbl.place_forget()
            except Exception:
                pass

        try:
            self._mini_win.after(max(300, int(ms)), _hide)
        except Exception:
            _hide()

    def _toast(self, msg: str, ms: int = 1200) -> None:
        """
        Visible notification (centered). Uses mini toast if mini is open,
        otherwise shows a centered, non-transparent banner.
        """
        # Prefer mini toast if mini UI exists.
        if self._mini_win and self._mini_win.winfo_exists():
            try:
                self._mini_toast(msg, ms=ms)
                return
            except Exception:
                pass

        text = str(msg or "").strip()
        if not text:
            return

        top = tk.Toplevel(self.root)
        try:
            top.overrideredirect(True)
        except Exception:
            pass
        try:
            top.attributes("-topmost", True)
            top.attributes("-alpha", 0.96)
        except Exception:
            pass

        # Center on screen
        try:
            sw = int(top.winfo_screenwidth())
            sh = int(top.winfo_screenheight())
        except Exception:
            sw, sh = 1200, 800
        w, h = 520, 72
        x = max(0, int((sw - w) / 2))
        y = max(0, int((sh - h) / 2))
        top.geometry(f"{w}x{h}+{x}+{y}")

        frm = tb.Frame(top, padding=12, bootstyle="success")
        frm.pack(fill="both", expand=True)
        tb.Label(frm, text=text, font=("Segoe UI Semibold", 14), bootstyle="inverse-success").pack(expand=True)

        try:
            top.after(max(300, int(ms)), top.destroy)
        except Exception:
            pass

    def _on_setup_text_detect_flow(self) -> None:
        """
        Mini-friendly flow:
        1) pick Text ROI
        2) automatically capture template after ROI is applied
        """
        # mark that next Text ROI apply should auto-capture template
        try:
            self._pending_auto_text_tpl_capture = True
        except Exception:
            pass
        self._on_pick_text_roi()

    def _on_pick_swords_roi_and_tpl(self) -> None:
        try:
            self._pending_auto_swords_tpl = True
        except Exception:
            pass
        self._on_pick_damage_icon_roi()

    def _on_pick_body_roi_and_tpl(self) -> None:
        try:
            self._pending_auto_body_tpl = True
        except Exception:
            pass
        self._on_pick_damage_icon_normal_roi()

    def _on_test_telegram_send(self) -> None:
        if not self._ensure_window_selected():
            return
        try:
            self._on_save_settings()
        except Exception:
            pass
        hwnd = int(getattr(self.store, "window_hwnd", 0) or 0)
        if hwnd == 0:
            messagebox.showerror("Ошибка", "Сначала выбери и подтверди окно игры.")
            return
        try:
            ok = bool(self.bot.send_telegram_test_radar(hwnd))
        except Exception:
            ok = False
        if ok:
            self.log("Telegram: тестовый скрин отправлен")
            try:
                self._toast("Telegram: скрин отправлен")
            except Exception:
                pass
        else:
            self.log("Telegram: тест не отправился (проверь token/chat_id и что выбран radar ROI)")
            try:
                self._toast("Telegram: не отправилось (проверь token/chat_id)")
            except Exception:
                pass

    def _schedule_empty_text_capture(self, *, delay_ms: int = 650, show_preview: bool = True) -> None:
        # Similar to _schedule_game_capture but uses bot.capture_empty_text (writes radar_empty_text.png)
        mini_active = bool(getattr(self, "_mini_win", None) and self._mini_win.winfo_exists())
        if mini_active:
            try:
                self._mini_win.withdraw()  # type: ignore[union-attr]
            except Exception:
                pass
        else:
            try:
                self.root.iconify()
            except Exception:
                pass

        def _do() -> None:
            try:
                if not self._ensure_window_selected():
                    return
                self._on_save_settings()
                self.bot.window_title = self.store.window_title or self.window_title_var.get().strip()
                hwnd = int(getattr(self.store, "window_hwnd", 0) or 0)
                if hwnd:
                    bring_window_to_foreground(hwnd)
                time.sleep(max(0.05, float(delay_ms) / 1000.0))
                path = self.bot.capture_empty_text()
                self.log(f"Шаблон текста сохранён: {path}")
                try:
                    self._mini_action_var.set("Действие: Text ROI/шаблон сохранены")
                except Exception:
                    pass
                if show_preview and (not mini_active):
                    try:
                        self._show_image_popup(Path(path), title="radar_empty_text.png (превью)")
                    except Exception:
                        pass
            except Exception as e:
                self.log(f"Шаблон текста: ошибка {e!r}")
            finally:
                if mini_active:
                    try:
                        self._mini_win.deiconify()  # type: ignore[union-attr]
                        self._mini_win.lift()  # type: ignore[union-attr]
                    except Exception:
                        pass
                else:
                    try:
                        self.root.deiconify()
                        self.root.lift()
                    except Exception:
                        pass

        try:
            self.root.after(50, _do)
        except Exception:
            _do()

    def _on_start_route(self) -> None:
        if not self._ensure_window_selected():
            return
        # Force route mode (also persist immediately to avoid race with bot start).
        self.farm_without_route_var.set(False)
        self.store.farm_without_route = False
        self.store.save()
        self._on_save_settings()
        self.bot.start(window_title=self.store.window_title or self.window_title_var.get().strip())
        self.log("Команда: старт (маршрут)")

    def _on_start_afk(self) -> None:
        if not self._ensure_window_selected():
            return
        # Force AFK mode (also persist immediately to avoid race with bot start).
        self.farm_without_route_var.set(True)
        self.store.farm_without_route = True
        self.store.save()
        self._on_save_settings()
        self.bot.start(window_title=self.store.window_title or self.window_title_var.get().strip())
        self.log("Команда: старт AFK")

    def _on_farm_without_route_toggle_changed(self) -> None:
        enabled = bool(self.farm_without_route_var.get())
        try:
            self.store.farm_without_route = enabled
            self.store.save()
            self.log(f"Фарм без маршрута: {'ВКЛ' if enabled else 'ВЫКЛ'}")
        except Exception:
            # If saving failed, don't crash UI.
            pass

    def _on_capture_empty_text(self) -> None:
        if not self._ensure_window_selected():
            return
        self._on_save_settings()
        self.bot.window_title = self.store.window_title or self.window_title_var.get().strip()
        try:
            path = self.bot.capture_empty_text()
            self._show_image_popup(path, title="radar_empty_text.png (превью)")
        except Exception as e:
            messagebox.showerror("Ошибка", str(e))
            return

    def _on_pick_text_roi(self) -> None:
        if not self._ensure_window_selected():
            return
        hwnd = int(getattr(self.store, "window_hwnd", 0) or 0)
        if hwnd == 0:
            messagebox.showerror("Ошибка", "Выбери окно вида 'Название (0x...)' и нажми 'Подтвердить'.")
            return

        def apply_text_roi(x: int, y: int, ww: int, hh: int) -> None:
            self.text_roi_x.set(x)
            self.text_roi_y.set(y)
            self.text_roi_w.set(ww)
            self.text_roi_h.set(hh)
            self._on_save_settings()
            self.log("Text ROI сохранён")
            # Optional auto-capture template if requested by mini flow
            try:
                if bool(getattr(self, "_pending_auto_text_tpl_capture", False)):
                    self._pending_auto_text_tpl_capture = False
                    self.log("Text ROI: сейчас сниму шаблон 'Нет Цель поиска' автоматически…")
                    self._schedule_empty_text_capture(delay_ms=650, show_preview=False)
            except Exception:
                pass

        # Live overlay selection directly over the game window
        try:
            self._open_live_roi_overlay(hwnd, title="Text ROI", on_apply=apply_text_roi)
        except Exception:
            # Fallback to screenshot-based selector
            cap = self._capture_client_image_pil(hwnd)
            if cap is None:
                messagebox.showerror("Ошибка", "Не удалось открыть выбор ROI.")
                return
            pil, w, h = cap
            self._open_roi_selector_for_callback(pil, window_w=w, window_h=h, on_apply=apply_text_roi)

    def _on_pick_menu_roi(self) -> None:
        if not self._ensure_window_selected():
            return
        hwnd = int(getattr(self.store, "window_hwnd", 0) or 0)
        if hwnd == 0:
            messagebox.showerror("Ошибка", "Выбери окно вида 'Название (0x...)' и нажми 'Подтвердить'.")
            return

        def apply_menu_roi(x: int, y: int, ww: int, hh: int) -> None:
            self.menu_roi_x.set(x)
            self.menu_roi_y.set(y)
            self.menu_roi_w.set(ww)
            self.menu_roi_h.set(hh)
            self._on_save_settings()
            self.log("ROI меню/чата сохранён")

        try:
            self._open_live_roi_overlay(hwnd, title="ROI меню/чата", on_apply=apply_menu_roi)
        except Exception:
            cap = self._capture_client_image_pil(hwnd)
            if cap is None:
                messagebox.showerror("Ошибка", "Не удалось открыть выбор ROI.")
                return
            pil, w, h = cap
            self._open_roi_selector_for_callback(pil, window_w=w, window_h=h, on_apply=apply_menu_roi)

    def _on_capture_menu_tpl(self) -> None:
        if not self._ensure_window_selected():
            return
        self._on_save_settings()
        try:
            path = self.bot.capture_menu_open_template()
            self._show_image_popup(path, title="menu_open_tpl.png (превью)")
        except Exception as e:
            messagebox.showerror("Ошибка", str(e))
            return

    def _on_pick_confirm_roi(self) -> None:
        if not self._ensure_window_selected():
            return
        hwnd = int(getattr(self.store, "window_hwnd", 0) or 0)
        if hwnd == 0:
            messagebox.showerror("Ошибка", "Выбери окно вида 'Название (0x...)' и нажми 'Подтвердить'.")
            return

        def apply_roi(x: int, y: int, ww: int, hh: int) -> None:
            self.confirm_roi_x.set(x)
            self.confirm_roi_y.set(y)
            self.confirm_roi_w.set(ww)
            self.confirm_roi_h.set(hh)
            self._on_save_settings()
            self.log("Confirm ROI сохранён")

        try:
            self._open_live_roi_overlay(hwnd, title="CONFIRM ROI", on_apply=apply_roi)
        except Exception:
            cap = self._capture_client_image_pil(hwnd)
            if cap is None:
                messagebox.showerror("Ошибка", "Не удалось открыть выбор ROI.")
                return
            pil, w, h = cap
            self._open_roi_selector_for_callback(pil, window_w=w, window_h=h, on_apply=apply_roi)

    def _on_capture_confirm_tpl(self) -> None:
        if not self._ensure_window_selected():
            return
        self._on_save_settings()
        try:
            path = self.bot.capture_confirm_popup_template()
            self._show_image_popup(path, title="confirm_popup_tpl.png (превью)")
        except Exception as e:
            messagebox.showerror("Ошибка", str(e))
            return

    def _on_pick_gate_roi(self) -> None:
        if not self._ensure_window_selected():
            return
        hwnd = int(getattr(self.store, "window_hwnd", 0) or 0)
        if hwnd == 0:
            messagebox.showerror("Ошибка", "Выбери окно вида 'Название (0x...)' и нажми 'Подтвердить'.")
            return

        def apply_roi(x: int, y: int, ww: int, hh: int) -> None:
            self.gate_roi_x.set(x)
            self.gate_roi_y.set(y)
            self.gate_roi_w.set(ww)
            self.gate_roi_h.set(hh)
            self._on_save_settings()
            self.log("Gate ROI сохранён")

        try:
            self._open_live_roi_overlay(hwnd, title="GATE ROI", on_apply=apply_roi)
        except Exception:
            cap = self._capture_client_image_pil(hwnd)
            if cap is None:
                messagebox.showerror("Ошибка", "Не удалось открыть выбор ROI.")
                return
            pil, w, h = cap
            self._open_roi_selector_for_callback(pil, window_w=w, window_h=h, on_apply=apply_roi)

    def _on_capture_gate_tpl(self) -> None:
        if not self._ensure_window_selected():
            return
        self._on_save_settings()
        try:
            path = self.bot.capture_gate_template()
            self._show_image_popup(path, title="gate_tpl.png (превью)")
        except Exception as e:
            messagebox.showerror("Ошибка", str(e))
            return

    def _on_pick_death_roi(self) -> None:
        if not self._ensure_window_selected():
            return
        hwnd = int(getattr(self.store, "window_hwnd", 0) or 0)
        if hwnd == 0:
            messagebox.showerror("Ошибка", "Выбери окно вида 'Название (0x...)' и нажми 'Подтвердить'.")
            return

        def apply_death_roi(x: int, y: int, ww: int, hh: int) -> None:
            self.death_roi_x.set(x)
            self.death_roi_y.set(y)
            self.death_roi_w.set(ww)
            self.death_roi_h.set(hh)
            self._on_save_settings()
            self.log("ROI смерти сохранён")

        try:
            self._open_live_roi_overlay(hwnd, title="ROI смерти", on_apply=apply_death_roi)
        except Exception:
            cap = self._capture_client_image_pil(hwnd)
            if cap is None:
                messagebox.showerror("Ошибка", "Не удалось открыть выбор ROI.")
                return
            pil, w, h = cap
            self._open_roi_selector_for_callback(pil, window_w=w, window_h=h, on_apply=apply_death_roi)

    def _on_capture_death_tpl(self) -> None:
        if not self._ensure_window_selected():
            return
        self._on_save_settings()
        try:
            path = self.bot.capture_death_template()
            self._show_image_popup(path, title="death_tpl.png (превью)")
        except Exception as e:
            messagebox.showerror("Ошибка", str(e))
            return

    def _on_pick_damage_icon_roi(self) -> None:
        if not self._ensure_window_selected():
            return
        hwnd = int(getattr(self.store, "window_hwnd", 0) or 0)
        if hwnd == 0:
            messagebox.showerror("Ошибка", "Выбери окно вида 'Название (0x...)' и нажми 'Подтвердить'.")
            return

        def apply_roi(x: int, y: int, ww: int, hh: int) -> None:
            self.damage_icon_roi_x.set(x)
            self.damage_icon_roi_y.set(y)
            self.damage_icon_roi_w.set(ww)
            self.damage_icon_roi_h.set(hh)
            self._on_save_settings()
            self.log("ROI мечей сохранён")
            try:
                if bool(getattr(self, "_pending_auto_swords_tpl", False)):
                    self._pending_auto_swords_tpl = False
                    self.log("Мечи: сейчас сниму шаблон автоматически…")
                    self._schedule_game_capture(kind="damage_attack_tpl", delay_ms=650, show_preview=False)
            except Exception:
                pass

        try:
            self._open_live_roi_overlay(hwnd, title="ROI мечей", on_apply=apply_roi)
        except Exception:
            cap = self._capture_client_image_pil(hwnd)
            if cap is None:
                messagebox.showerror("Ошибка", "Не удалось открыть выбор ROI.")
                return
            pil, w, h = cap
            self._open_roi_selector_for_callback(pil, window_w=w, window_h=h, on_apply=apply_roi)

    def _on_pick_damage_icon_normal_roi(self) -> None:
        if not self._ensure_window_selected():
            return
        hwnd = int(getattr(self.store, "window_hwnd", 0) or 0)
        if hwnd == 0:
            messagebox.showerror("Ошибка", "Выбери окно вида 'Название (0x...)' и нажми 'Подтвердить'.")
            return

        def apply_roi(x: int, y: int, ww: int, hh: int) -> None:
            self.damage_icon_norm_roi_x.set(x)
            self.damage_icon_norm_roi_y.set(y)
            self.damage_icon_norm_roi_w.set(ww)
            self.damage_icon_norm_roi_h.set(hh)
            self._on_save_settings()
            self.log("ROI корпуса сохранён")
            try:
                if bool(getattr(self, "_pending_auto_body_tpl", False)):
                    self._pending_auto_body_tpl = False
                    self.log("Корпус: сейчас сниму шаблон автоматически…")
                    self._schedule_game_capture(kind="damage_normal_tpl", delay_ms=650, show_preview=False)
            except Exception:
                pass

        try:
            self._open_live_roi_overlay(hwnd, title="ROI корпуса", on_apply=apply_roi)
        except Exception:
            cap = self._capture_client_image_pil(hwnd)
            if cap is None:
                messagebox.showerror("Ошибка", "Не удалось открыть выбор ROI.")
                return
            pil, w, h = cap
            self._open_roi_selector_for_callback(pil, window_w=w, window_h=h, on_apply=apply_roi)

    def _on_pick_hp_roi(self) -> None:
        if not self._ensure_window_selected():
            return
        hwnd = int(getattr(self.store, "window_hwnd", 0) or 0)
        if hwnd == 0:
            messagebox.showerror("Ошибка", "Выбери окно вида 'Название (0x...)' и нажми 'Подтвердить'.")
            return

        def apply_roi(x: int, y: int, ww: int, hh: int) -> None:
            self.hp_roi_x.set(x)
            self.hp_roi_y.set(y)
            self.hp_roi_w.set(ww)
            self.hp_roi_h.set(hh)
            self._on_save_settings()
            self.log("ROI HP сохранён")

        try:
            self._open_live_roi_overlay(hwnd, title="ROI HP", on_apply=apply_roi)
        except Exception:
            cap = self._capture_client_image_pil(hwnd)
            if cap is None:
                messagebox.showerror("Ошибка", "Не удалось открыть выбор ROI.")
                return
            pil, w, h = cap
            self._open_roi_selector_for_callback(pil, window_w=w, window_h=h, on_apply=apply_roi)

    def _on_capture_damage_icon_tpl(self) -> None:
        if not self._ensure_window_selected():
            return
        self._on_save_settings()
        self.log("Шаблон атаки: через 0.6 сек сниму ROI из игры (переключись в игру и держи иконку мечей).")
        self._schedule_game_capture(kind="damage_attack_tpl", delay_ms=600, show_preview=False)

    def _on_capture_damage_icon_normal_tpl(self) -> None:
        if not self._ensure_window_selected():
            return
        self._on_save_settings()
        self.log("Шаблон обычной: через 0.6 сек сниму ROI из игры (переключись в игру, когда НЕ бьют).")
        self._schedule_game_capture(kind="damage_normal_tpl", delay_ms=600, show_preview=False)

    def _schedule_game_capture(self, *, kind: str, delay_ms: int, show_preview: bool = True) -> None:
        """
        When user clicks capture in UI, the game often isn't foreground yet.
        We briefly hide our UI, focus the game, wait a bit, then grab MSS screenshot.
        """
        mini_active = bool(getattr(self, "_mini_win", None) and self._mini_win.winfo_exists())
        if mini_active:
            try:
                self._mini_win.withdraw()  # type: ignore[union-attr]
            except Exception:
                pass
        else:
            try:
                self.root.iconify()
            except Exception:
                pass

        def _do() -> None:
            try:
                hwnd = int(getattr(self.store, "window_hwnd", 0) or 0)
                if hwnd <= 0:
                    raise RuntimeError("Окно игры не выбрано (hwnd=0).")
                bring_window_to_foreground(hwnd)
                time.sleep(max(0.05, float(delay_ms) / 1000.0))

                roi = self.store.damage_icon_roi
                from vision import Vision

                # Different templates can use different ROIs.
                if kind == "damage_normal_tpl":
                    roi = getattr(self.store, "damage_icon_normal_roi", self.store.damage_icon_roi)
                img = Vision().grab_client_roi_bgr(hwnd, roi)

                if kind == "damage_attack_tpl":
                    path = Path(str(getattr(self.store, "damage_icon_tpl_path", "damage_icon_tpl.png") or "damage_icon_tpl.png"))
                    cv2.imwrite(str(path), img)
                    self.log(f"Шаблон иконки атаки сохранён: {path}")
                    try:
                        self._toast("Шаблон мечей сохранён")
                    except Exception:
                        pass
                    if show_preview and (not mini_active):
                        self._show_image_popup(path, title="damage_icon_tpl.png (превью)")
                elif kind == "damage_normal_tpl":
                    path = Path(
                        str(
                            getattr(self.store, "damage_icon_normal_tpl_path", "damage_icon_normal_tpl.png")
                            or "damage_icon_normal_tpl.png"
                        )
                    )
                    cv2.imwrite(str(path), img)
                    self.log(f"Шаблон обычной иконки сохранён: {path}")
                    try:
                        self._toast("Шаблон корпуса сохранён")
                    except Exception:
                        pass
                    if show_preview and (not mini_active):
                        self._show_image_popup(path, title="damage_icon_normal_tpl.png (превью)")
                else:
                    raise RuntimeError(f"Unknown capture kind: {kind!r}")
            except Exception as e:
                try:
                    messagebox.showerror("Ошибка", str(e))
                except Exception:
                    pass
                self.log(f"Ошибка захвата шаблона: {e!r}")
            finally:
                if mini_active:
                    try:
                        self._mini_win.deiconify()  # type: ignore[union-attr]
                        self._mini_win.lift()  # type: ignore[union-attr]
                    except Exception:
                        pass
                else:
                    try:
                        self.root.deiconify()
                        self.root.lift()
                    except Exception:
                        pass

        try:
            self.root.after(50, _do)
        except Exception:
            # fallback: run immediately
            _do()

    def _on_test_hp_now(self) -> None:
        if not self._ensure_window_selected():
            return
        self._on_save_settings()
        try:
            from vision import Vision
            hwnd = int(getattr(self.store, "window_hwnd", 0) or 0)
            roi = self.store.hp_bar_roi
            img = Vision().grab_client_roi_bgr(hwnd, roi)
            pct = Vision().hp_percent_from_bar(img)
            self.log(f"HP% сейчас: {pct}")
        except Exception as e:
            self.log(f"HP% test: ошибка {e!r}")

    def _on_test_damage_icon_now(self) -> None:
        if not self._ensure_window_selected():
            return
        self._on_save_settings()
        try:
            from vision import Vision
            hwnd = int(getattr(self.store, "window_hwnd", 0) or 0)
            roi = self.store.damage_icon_roi
            img = Vision().grab_client_roi_bgr(hwnd, roi)
            atk_path = Path(str(getattr(self.store, "damage_icon_tpl_path", "damage_icon_tpl.png") or "damage_icon_tpl.png"))
            norm_path = Path(
                str(getattr(self.store, "damage_icon_normal_tpl_path", "damage_icon_normal_tpl.png") or "damage_icon_normal_tpl.png")
            )
            tpl_atk = cv2.imread(str(atk_path), cv2.IMREAD_COLOR) if atk_path.exists() else None
            tpl_norm = cv2.imread(str(norm_path), cv2.IMREAD_COLOR) if norm_path.exists() else None
            atk_score = Vision().icon_match_score(img, tpl_atk) if tpl_atk is not None else None
            norm_score = Vision().icon_match_score(img, tpl_norm) if tpl_norm is not None else None
            self.log(f"Иконка: мечи score={atk_score}, обычная score={norm_score}")
        except Exception as e:
            self.log(f"Icon test: ошибка {e!r}")

    def _on_stop(self) -> None:
        self.bot.stop()
        self.log("Команда: стоп")

    def _show_image_popup(self, path: Path, title: str = "Preview") -> None:
        try:
            p = Path(path)
            img_bgr = cv2.imread(str(p), cv2.IMREAD_COLOR)
            if img_bgr is None:
                return
            rgb = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)
            pil = Image.fromarray(rgb)
        except Exception:
            return

        top = tk.Toplevel(self.root)
        top.title(title)

        max_w, max_h = 900, 600
        w, h = pil.size
        scale = min(1.0, max_w / max(1, w), max_h / max(1, h))
        if scale != 1.0:
            pil = pil.resize((int(w * scale), int(h * scale)), Image.Resampling.NEAREST)

        photo = ImageTk.PhotoImage(pil)
        lbl = ttk.Label(top, image=photo)
        lbl.image = photo
        lbl.pack(padx=10, pady=10)

    def _tick_logs(self) -> None:
        try:
            while True:
                msg = self.log_queue.get_nowait()
                self.log_text.configure(state="normal")
                self.log_text.insert("end", msg + "\n")
                self.log_text.see("end")
                self.log_text.configure(state="disabled")
        except queue.Empty:
            pass
        self.root.after(100, self._tick_logs)

    def _tick_status(self) -> None:
        # Pump global hotkeys (WM_HOTKEY)
        try:
            # Process all pending WM_HOTKEY messages (not just one).
            while True:
                msg = win32gui.PeekMessage(None, win32con.WM_HOTKEY, win32con.WM_HOTKEY, win32con.PM_REMOVE)
                if not msg or msg[1] != win32con.WM_HOTKEY:
                    break
                hotkey_id = int(msg[2])
                if hotkey_id == int(getattr(self, "_hotkey_id_pause", 9001)):
                    self.bot.toggle_pause()
                elif hotkey_id == int(getattr(self, "_hotkey_id_stop_record", 9002)):
                    # Stop recording from anywhere (while game is active)
                    try:
                        if self._recorder is not None:
                            self.root.after(0, self._on_stop_recording)
                    except Exception:
                        pass
        except Exception:
            pass

        # Scheduler tick (start/stop)
        try:
            self._tick_scheduler()
        except Exception:
            pass

        # HP preview (progressbar)
        try:
            self._tick_hp_preview()
        except Exception:
            pass

        running = self.bot.is_running()
        paused = bool(getattr(self.bot, "is_paused", lambda: False)())
        if running and paused:
            self.status_var.set("Статус: пауза")
        else:
            self.status_var.set("Статус: запущен" if running else "Статус: остановлен")
        try:
            if running and paused:
                self.status_label.configure(bootstyle="warning")
            else:
                self.status_label.configure(bootstyle=("success" if running else "danger"))
        except Exception:
            pass

        # Enable/disable start/stop buttons depending on running state.
        try:
            if running:
                self.start_route_btn.configure(state="disabled")
                self.start_afk_btn.configure(state="disabled")
                self.stop_btn.configure(state="normal")
                self.stop_afk_btn.configure(state="normal")
            else:
                self.start_route_btn.configure(state="normal")
                self.start_afk_btn.configure(state="normal")
                self.stop_btn.configure(state="disabled")
                self.stop_afk_btn.configure(state="disabled")
        except Exception:
            pass

        # Mini overlay text
        try:
            self._update_mini_overlay()
        except Exception:
            pass
        self.root.after(300, self._tick_status)

    def _toggle_mini(self) -> None:
        if self._mini_win and self._mini_win.winfo_exists():
            # Restore full window
            try:
                self._mini_win.destroy()
            except Exception:
                pass
            self._mini_win = None
            try:
                self.root.deiconify()
                self.root.lift()
            except Exception:
                pass
            return

        # Create mini overlay and hide main window
        try:
            self.root.withdraw()
        except Exception:
            pass

        w = tk.Toplevel(self.root)
        w.title("RAVEN BOT — мини")
        try:
            w.attributes("-topmost", True)
            w.attributes("-alpha", 0.78)
        except Exception:
            pass
        w.geometry("480x320+40+40")
        w.resizable(True, True)
        w.minsize(360, 220)

        outer = tb.Frame(w, padding=8)
        outer.pack(fill="both", expand=True)
        outer.grid_columnconfigure(0, weight=1)
        outer.grid_rowconfigure(1, weight=1)

        # Fixed header (no scroll): status + hp + toast
        head = tb.Frame(outer)
        head.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 6))
        tb.Label(head, textvariable=self._mini_status_var, font=("Segoe UI Semibold", 11)).pack(anchor="w")
        tb.Label(head, textvariable=self._mini_game_var, bootstyle="secondary").pack(anchor="w", pady=(4, 0))
        tb.Label(head, textvariable=self._mini_action_var, bootstyle="info").pack(anchor="w", pady=(2, 0))
        tb.Label(head, textvariable=self._mini_detect_var, bootstyle="warning").pack(anchor="w", pady=(2, 0))

        mini_hp_row = tb.Frame(head)
        mini_hp_row.pack(fill="x", pady=(6, 0))
        mini_hp_bar = tb.Progressbar(
            mini_hp_row,
            maximum=100,
            variable=self.hp_top_progress_var,
            bootstyle="danger-striped",
        )
        mini_hp_bar.pack(side="left", fill="x", expand=True)
        tb.Label(mini_hp_row, textvariable=self.hp_top_var, bootstyle="secondary").pack(side="left", padx=(10, 0))

        toast = tb.Label(head, textvariable=self._mini_toast_var, bootstyle="success")
        toast.pack(anchor="w", pady=(6, 0))

        # Scrollable area for controls/settings
        canvas = tk.Canvas(outer, highlightthickness=0)
        vs = tb.Scrollbar(outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vs.set)
        canvas.grid(row=1, column=0, sticky="nsew")
        vs.grid(row=1, column=1, sticky="ns")

        frm = tb.Frame(canvas)
        win_id = canvas.create_window((0, 0), window=frm, anchor="nw")

        def _sync_width(_evt=None) -> None:
            try:
                canvas.itemconfigure(win_id, width=canvas.winfo_width())
            except Exception:
                pass

        def _sync_scroll(_evt=None) -> None:
            try:
                canvas.configure(scrollregion=canvas.bbox("all"))
            except Exception:
                pass

        canvas.bind("<Configure>", _sync_width)
        frm.bind("<Configure>", _sync_scroll)

        # Mouse wheel is handled globally via _set_mw_canvas routing.
        canvas.bind("<Enter>", lambda _e: self._set_mw_canvas(canvas))
        canvas.bind("<Leave>", lambda _e: self._set_mw_canvas(None))

        # Collapsible sections
        self._mini_sections: dict[str, dict] = {}

        def add_section(title: str, *, open_by_default: bool = True) -> tb.Frame:
            box = tb.Labelframe(frm, text=title, padding=8, bootstyle="secondary")
            box.pack(fill="x", pady=(6, 0))
            head = tb.Frame(box)
            head.pack(fill="x")
            state = {"open": bool(open_by_default)}
            btn = tb.Button(head, text="Свернуть" if state["open"] else "Развернуть", bootstyle="secondary-outline")
            btn.pack(side="right")
            body = tb.Frame(box)
            body.pack(fill="x", pady=(6, 0))

            def toggle():
                state["open"] = not state["open"]
                if state["open"]:
                    body.pack(fill="x", pady=(6, 0))
                    btn.configure(text="Свернуть")
                else:
                    body.pack_forget()
                    btn.configure(text="Развернуть")
                _sync_scroll()

            btn.configure(command=toggle)
            self._mini_sections[title] = {"box": box, "body": body, "toggle": toggle, "state": state}
            if not state["open"]:
                body.pack_forget()
                btn.configure(text="Развернуть")
            return body

        # Control section
        sec_ctrl = add_section("Управление", open_by_default=True)
        row_btn = tb.Frame(sec_ctrl)
        row_btn.pack(fill="x")
        self._mini_start_route_btn = tb.Button(row_btn, text="Старт (маршрут)", bootstyle="primary", command=self._on_start_route)
        self._mini_start_route_btn.pack(side="left", expand=True, fill="x")
        self._mini_start_afk_btn = tb.Button(row_btn, text="Старт AFK", bootstyle="primary-outline", command=self._on_start_afk)
        self._mini_start_afk_btn.pack(side="left", expand=True, fill="x", padx=(8, 0))
        self._mini_stop_btn = tb.Button(row_btn, text="Стоп", bootstyle="danger", command=self._on_stop, state="disabled")
        self._mini_stop_btn.pack(side="left", padx=(8, 0))

        row_tog = tb.Frame(sec_ctrl)
        row_tog.pack(fill="x", pady=(8, 0))
        tb.Checkbutton(
            row_tog,
            text="ТП по врагу",
            variable=self.tp_on_enemy_enabled_var,
            bootstyle="round-toggle",
            command=self._on_tp_toggle_changed,
        ).pack(side="left")
        tb.Checkbutton(
            row_tog,
            text="Детект",
            variable=self.radar_detect_enabled_var,
            bootstyle="round-toggle",
            command=self._on_radar_detect_toggle_changed,
        ).pack(side="left", padx=(10, 0))

        row_tog2 = tb.Frame(sec_ctrl)
        row_tog2.pack(fill="x", pady=(6, 0))
        tb.Checkbutton(
            row_tog2,
            text="ТП по урону",
            variable=self.damage_tp_enabled_var,
            bootstyle="round-toggle",
            command=self._on_damage_tp_toggle_changed,
        ).pack(side="left")
        tb.Checkbutton(
            row_tog2,
            text="ТП по HP",
            variable=self.hp_tp_enabled_var,
            bootstyle="round-toggle",
            command=self._on_hp_tp_toggle_changed,
        ).pack(side="left", padx=(10, 0))
        tb.Checkbutton(
            row_tog2,
            text="Фокус для ТП",
            variable=self.tp_focus_steal_enabled_var,
            bootstyle="round-toggle",
            command=self._on_tp_focus_toggle_changed,
        ).pack(side="left", padx=(10, 0))

        row_tog3 = tb.Frame(sec_ctrl)
        row_tog3.pack(fill="x", pady=(6, 0))
        tb.Checkbutton(
            row_tog3,
            text="Перехват фокуса игры",
            variable=self.focus_steal_enabled_var,
            bootstyle="round-toggle",
            command=lambda: self._on_save_settings(),
        ).pack(side="left")
        tb.Checkbutton(
            row_tog3,
            text="AFK (стоять)",
            variable=self.farm_without_route_var,
            bootstyle="round-toggle",
            command=self._on_farm_without_route_toggle_changed,
        ).pack(side="left", padx=(10, 0))

        # Sound toggle + wav picker (mini)
        row_sound = tb.Frame(sec_ctrl)
        row_sound.pack(fill="x", pady=(6, 0))
        tb.Checkbutton(
            row_sound,
            text="Звук",
            variable=self.enemy_alert_enabled_var,
            bootstyle="round-toggle",
            command=lambda: self._on_save_settings(),
        ).pack(side="left")
        tb.Button(row_sound, text="WAV…", bootstyle="outline", command=self._on_pick_enemy_alert_wav).pack(
            side="left", padx=(10, 0)
        )

        # Telegram quick settings (mini)
        sec_tg = add_section("Telegram", open_by_default=False)
        tg1 = tb.Frame(sec_tg)
        tg1.pack(fill="x")
        tb.Checkbutton(
            tg1,
            text="Скрин при атаке",
            variable=self.telegram_enabled_var,
            bootstyle="round-toggle",
            command=lambda: self._on_save_settings(),
        ).pack(side="left")
        tb.Label(tg1, text="Интервал", bootstyle="secondary").pack(side="left", padx=(10, 0))
        tb.Spinbox(tg1, from_=5.0, to=3600.0, increment=1.0, textvariable=self.telegram_interval_var, width=8).pack(
            side="left", padx=(8, 0)
        )
        tb.Button(tg1, text="Тест", bootstyle="outline", command=self._on_test_telegram_send).pack(side="right")

        tg2 = tb.Frame(sec_tg)
        tg2.pack(fill="x", pady=(6, 0))
        tb.Label(tg2, text="Chat ID", bootstyle="secondary").pack(side="left")
        self._mini_tg_chat_entry = tb.Entry(tg2, textvariable=self.telegram_chat_id_var, width=18)
        self._mini_tg_chat_entry.pack(side="left", padx=(10, 0), fill="x", expand=True)
        self._install_paste_support(self._mini_tg_chat_entry)

        tg3 = tb.Frame(sec_tg)
        tg3.pack(fill="x", pady=(6, 0))
        tb.Label(tg3, text="Token", bootstyle="secondary").pack(side="left")
        self._mini_tg_token_entry = tb.Entry(tg3, textvariable=self.telegram_token_var, width=22, show="•")
        self._mini_tg_token_entry.pack(side="left", padx=(10, 0), fill="x", expand=True)
        self._install_paste_support(self._mini_tg_token_entry)

        # Teleport quick settings
        sec_tp = add_section("Телепорт", open_by_default=False)
        tp_row1 = tb.Frame(sec_tp)
        tp_row1.pack(fill="x")
        tb.Label(tp_row1, text="Клавиша", bootstyle="secondary").pack(side="left")
        tb.Combobox(tp_row1, textvariable=self.tp_key_var, state="normal", width=10).pack(side="left", padx=(10, 0))
        tb.Label(tp_row1, text="Удержание", bootstyle="secondary").pack(side="left", padx=(10, 0))
        tb.Spinbox(tp_row1, from_=0.01, to=0.30, increment=0.01, textvariable=self.key_hold_var, width=6).pack(
            side="left", padx=(8, 0)
        )

        tp_row2 = tb.Frame(sec_tp)
        tp_row2.pack(fill="x", pady=(6, 0))
        tb.Label(tp_row2, text="Ожидание от/до (сек)", bootstyle="secondary").pack(side="left")
        tb.Spinbox(tp_row2, from_=0, to=3600, textvariable=self.wait_from_var, width=6).pack(side="left", padx=(10, 4))
        tb.Spinbox(tp_row2, from_=0, to=3600, textvariable=self.wait_to_var, width=6).pack(side="left", padx=(4, 0))
        tb.Button(tp_row2, text="Применить", bootstyle="outline", command=self._on_save_settings).pack(side="right")

        # Detect quick actions (ROI selection + templates)
        sec_detect = add_section("Детект", open_by_default=False)
        d1 = tb.Frame(sec_detect)
        d1.pack(fill="x")
        tb.Button(d1, text="Радар: область + шаблон", bootstyle="outline", command=self._on_setup_text_detect_flow).pack(
            side="left", expand=True, fill="x"
        )
        # "Переснять шаблон" не показываем в мини-режиме: основная кнопка делает область+шаблон.

        d2 = tb.Frame(sec_detect)
        d2.pack(fill="x", pady=(6, 0))
        tb.Button(d2, text="Мечи: ROI+шаблон", bootstyle="outline", command=self._on_pick_swords_roi_and_tpl).pack(
            side="left", expand=True, fill="x"
        )
        # tpl auto after ROI

        d2b = tb.Frame(sec_detect)
        d2b.pack(fill="x", pady=(6, 0))
        tb.Button(d2b, text="Корпус: ROI+шаблон", bootstyle="outline", command=self._on_pick_body_roi_and_tpl).pack(
            side="left", expand=True, fill="x"
        )
        # log button removed in mini UI (too technical)

        d3 = tb.Frame(sec_detect)
        d3.pack(fill="x", pady=(6, 0))
        tb.Button(d3, text="ROI HP", bootstyle="outline", command=self._on_pick_hp_roi).pack(side="left", expand=True, fill="x")
        # log button removed in mini UI (too technical)

        # Routes / profiles quick settings
        sec_routes = add_section("Маршруты", open_by_default=False)
        rr1 = tb.Frame(sec_routes)
        rr1.pack(fill="x")
        tb.Label(rr1, text="Профиль", bootstyle="secondary").pack(side="left")
        tb.Combobox(rr1, textvariable=self.profile_var, state="readonly", width=14, values=self.profile_combo["values"]).pack(
            side="left", padx=(10, 0)
        )
        tb.Button(rr1, text="OK", bootstyle="outline", command=lambda: self._on_select_profile()).pack(side="left", padx=(10, 0))

        rr2 = tb.Frame(sec_routes)
        rr2.pack(fill="x", pady=(6, 0))
        tb.Label(rr2, text="Setup", bootstyle="secondary").pack(side="left")
        tb.Combobox(rr2, textvariable=self.setup_route_var, state="readonly", width=18, values=self.setup_routes_combo["values"]).pack(
            side="left", padx=(10, 0)
        )
        tb.Button(rr2, text="OK", bootstyle="outline", command=lambda: self._on_select_setup_route()).pack(side="left", padx=(10, 0))

        rr3 = tb.Frame(sec_routes)
        rr3.pack(fill="x", pady=(6, 0))
        tb.Label(rr3, text="Farm", bootstyle="secondary").pack(side="left")
        tb.Combobox(rr3, textvariable=self.active_route_var, state="readonly", width=18, values=self.routes_combo["values"]).pack(
            side="left", padx=(10, 0)
        )
        tb.Button(rr3, text="OK", bootstyle="outline", command=lambda: self._on_select_active_route()).pack(side="left", padx=(10, 0))

        # Hotkey section
        sec_hk = add_section("Хоткей", open_by_default=False)
        hk_row = tb.Frame(sec_hk)
        hk_row.pack(fill="x")
        tb.Label(hk_row, text="Пауза/Продолжить", bootstyle="secondary").pack(side="left")
        hotkeys = [f"f{i}" for i in range(6, 13)] + ["pause", "scrolllock", "insert", "home", "end", "pageup", "pagedown", "delete"]
        tb.Combobox(hk_row, textvariable=self.pause_hotkey_var, state="readonly", values=hotkeys, width=10).pack(
            side="left", padx=(10, 0)
        )
        tb.Button(hk_row, text="Применить", bootstyle="outline", command=self._on_apply_pause_hotkey).pack(side="left", padx=(10, 0))

        # Footer
        btns = tb.Frame(frm)
        btns.pack(fill="x", pady=(10, 0))
        tb.Button(btns, text="Развернуть", bootstyle="primary", command=self._toggle_mini).pack(side="left")

        def _on_close():
            # same as expand
            self._toggle_mini()

        try:
            w.protocol("WM_DELETE_WINDOW", _on_close)
        except Exception:
            pass

        self._mini_win = w
        self._update_mini_overlay()

    def _update_mini_overlay(self) -> None:
        if not (self._mini_win and self._mini_win.winfo_exists()):
            return
        running = self.bot.is_running()
        paused = bool(getattr(self.bot, "is_paused", lambda: False)())
        self._mini_status_var.set("Бот: пауза" if (running and paused) else ("Бот: запущен" if running else "Бот: остановлен"))

        # Start/Stop buttons UX: highlight active mode and disable Stop when not running.
        try:
            if hasattr(self, "_mini_stop_btn") and self._mini_stop_btn:
                self._mini_stop_btn.configure(state=("normal" if running else "disabled"))
        except Exception:
            pass
        try:
            # Determine current mode from settings (bot uses this flag).
            is_afk_mode = bool(getattr(self.store, "farm_without_route", False))
            if hasattr(self, "_mini_start_route_btn") and self._mini_start_route_btn:
                self._mini_start_route_btn.configure(
                    bootstyle=("success" if (running and (not is_afk_mode)) else "primary"),
                )
            if hasattr(self, "_mini_start_afk_btn") and self._mini_start_afk_btn:
                self._mini_start_afk_btn.configure(
                    bootstyle=("success" if (running and is_afk_mode) else "primary-outline"),
                )
        except Exception:
            pass

        hwnd = int(getattr(self.store, "window_hwnd", 0) or 0)
        ok = False
        fg = False
        if hwnd:
            try:
                ok = bool(win32gui.IsWindow(hwnd) and win32gui.IsWindowVisible(hwnd) and (not win32gui.IsIconic(hwnd)))
            except Exception:
                ok = False
            try:
                fg = (int(win32gui.GetForegroundWindow() or 0) == int(hwnd))
            except Exception:
                fg = False
        self._mini_game_var.set(f"Окно игры: {'OK' if ok else 'НЕ ВИЖУ'}; фокус={'ДА' if fg else 'НЕТ'}")

        action = str(getattr(self.bot, "current_action", "") or "")
        if not action:
            action = "—"
        self._mini_action_var.set(f"Действие: {action}")

        # Live detect snapshot (best-effort)
        try:
            enemy = bool(getattr(self.bot, "last_radar_enemy", False))
            attacked = bool(getattr(self.bot, "last_attacked", False))
            hp = getattr(self.bot, "last_hp_pct", None)
            hp_s = "?" if hp is None else f"{int(hp)}%"
            parts = [f"Враг: {'ДА' if enemy else 'НЕТ'}", f"Атакуют: {'ДА' if attacked else 'НЕТ'}", f"HP: {hp_s}"]
            # Hide technical matching metrics in mini UI (keep it human-readable).
            self._mini_detect_var.set("Детект: " + " | ".join(parts))
        except Exception:
            self._mini_detect_var.set("Детект: ?")

    def _tick_hp_preview(self) -> None:
        # Update HP% progressbar when a window is selected and ROI looks valid.
        hwnd = int(getattr(self.store, "window_hwnd", 0) or 0)
        if hwnd == 0:
            return
        roi = getattr(self.store, "hp_bar_roi", None)
        if roi is None or int(getattr(roi, "w", 1)) <= 5 or int(getattr(roi, "h", 1)) <= 5:
            return
        from vision import Vision
        img = Vision().grab_client_roi_bgr(hwnd, roi)
        pct = Vision().hp_percent_from_bar(img)
        if pct is None:
            return
        self._hp_preview_var.set(int(pct))
        self._hp_label_var.set(f"HP: {int(pct)}%")
        try:
            self.hp_top_var.set(f"HP: {int(pct)}%")
            self.hp_top_progress_var.set(int(pct))
        except Exception:
            pass

    def _parse_hhmm(self, hhmm: str) -> tuple[int, int] | None:
        s = (hhmm or "").strip()
        if not s or ":" not in s:
            return None
        a, b = s.split(":", 1)
        try:
            h = int(a)
            m = int(b)
        except Exception:
            return None
        if not (0 <= h <= 23 and 0 <= m <= 59):
            return None
        return h, m

    def _tick_scheduler(self) -> None:
        if not bool(getattr(self.store, "schedule_enabled", False)):
            self._schedule_stop_ts = None
            return

        now = time.time()
        lt = time.localtime(now)
        today = time.strftime("%Y-%m-%d", lt)

        # stop
        if self.bot.is_running() and self._schedule_stop_ts is not None and now >= float(self._schedule_stop_ts):
            self.bot.stop()
            self.log("Планировщик: авто-стоп по таймеру")
            self._schedule_stop_ts = None
            return

        # start (only once per day)
        if self.bot.is_running():
            return

        if str(getattr(self.store, "schedule_last_start_date", "") or "") == today:
            return

        hhmm = str(getattr(self.store, "schedule_start_hhmm", "02:00") or "02:00")
        parsed = self._parse_hhmm(hhmm)
        if parsed is None:
            return
        h, m = parsed
        start_ts = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, h, m, 0, lt.tm_wday, lt.tm_yday, lt.tm_isdst))
        if now < start_ts:
            return

        # start
        mode = str(getattr(self.store, "schedule_mode", "route") or "route").strip().lower()
        dur_h = max(0.1, float(getattr(self.store, "schedule_duration_h", 8.0)))
        self._schedule_stop_ts = now + dur_h * 3600.0

        # persist "started today"
        try:
            self.store.schedule_last_start_date = today
            self.store.save()
        except Exception:
            pass

        # Ensure settings are saved before start
        try:
            self._on_save_settings()
        except Exception:
            pass

        if mode == "afk":
            self._on_start_afk()
            self.log(f"Планировщик: авто-старт AFK на {hhmm} (на {dur_h:.1f} ч)")
        else:
            self._on_start_route()
            self.log(f"Планировщик: авто-старт ROUTE на {hhmm} (на {dur_h:.1f} ч)")

    # --- Inline route wizard helpers ---
    def _route_wizard_steps(self) -> list[tuple[str, str, Callable[[], None]]]:
        return [
            (
                "Шаг 1/5: Окно игры",
                "Выбери окно игры → нажми «Подтвердить».",
                lambda: self._spotlight(getattr(self, "confirm_window_btn", getattr(self, "windows_combo", None))),
            ),
            (
                "Шаг 2/5: Профиль (локация)",
                "Нажми «Новый профиль» и задай имя локации.",
                lambda: self._spotlight(getattr(self, "new_profile_btn", None)),
            ),
            (
                "Шаг 3/5: Setup маршрут",
                "Нажми «Новый» → имя `setup_...` → «Старт запись» → Alt+Tab в игру → «Стоп запись» → «Сохранить маршрут» → выбери его в Setup.",
                lambda: self._spotlight(getattr(self, "record_btn", getattr(self, "new_route_btn", None))),
            ),
            (
                "Шаг 4/5: Farm маршрут",
                "Нажми «Новый» → имя `farm_...` и запиши, или собери на вкладке «Маршрут» по скриншоту. Потом «Сохранить маршрут».",
                lambda: self._spotlight(getattr(self, "new_route_btn", None)),
            ),
            (
                "Шаг 5/5: Выбор Farm + старт",
                "Выбери farm_... в «Farm маршрут (по карте)». Запусти «Старт (маршрут)» или «Старт AFK».",
                lambda: self._spotlight(getattr(self, "routes_combo", None)),
            ),
        ]

    def _route_wizard_render(self) -> None:
        try:
            steps = self._route_wizard_steps()
            i = max(0, min(int(getattr(self, "_route_wizard_step", 0)), len(steps) - 1))
            self._route_wizard_step = i
            title, body, _ = steps[i]
            self._route_wizard_title_var.set(title)
            self._route_wizard_body_var.set(body)
            # button states
            try:
                self._wiz_back_btn.configure(state=("disabled" if i == 0 else "normal"))
                self._wiz_next_btn.configure(state=("disabled" if i >= len(steps) - 1 else "normal"))
            except Exception:
                pass
        except Exception:
            pass

    def _route_wizard_hint(self) -> None:
        steps = self._route_wizard_steps()
        i = max(0, min(int(getattr(self, "_route_wizard_step", 0)), len(steps) - 1))
        try:
            _title, _body, fn = steps[i]
            fn()
        except Exception:
            pass

    def _route_wizard_next(self) -> None:
        self._route_wizard_step = int(getattr(self, "_route_wizard_step", 0)) + 1
        self._route_wizard_render()
        self._route_wizard_hint()

    def _route_wizard_back(self) -> None:
        self._route_wizard_step = int(getattr(self, "_route_wizard_step", 0)) - 1
        self._route_wizard_render()
        self._route_wizard_hint()

    def _toggle_route_wizard(self) -> None:
        try:
            cur = bool(getattr(self, "_wiz_visible", True))
            self._wiz_visible = not cur
            if self._wiz_visible:
                try:
                    self._wiz_frame.grid()  # type: ignore[attr-defined]
                except Exception:
                    pass
                try:
                    self._wiz_toggle_btn.configure(text="Скрыть мастер")
                except Exception:
                    pass
                # show current hint briefly
                self._route_wizard_hint()
            else:
                try:
                    self._wiz_frame.grid_remove()  # type: ignore[attr-defined]
                except Exception:
                    pass
                try:
                    self._wiz_toggle_btn.configure(text="Показать мастер")
                except Exception:
                    pass
        except Exception:
            pass

    def _open_route_guide(self) -> None:
        # This window is a READ-ONLY help guide (no UI jumping/highlighting).
        steps: list[tuple[str, str]] = [
            (
                "Шаг 1 — выбери окно игры",
                "Слева, в блоке «Окно и маршрут»:\n"
                "1) Запусти игру.\n"
                "2) Нажми **«Взять активное»** (когда игра активна) ИЛИ выбери окно в списке.\n"
                "3) Нажми **«Подтвердить»**.\n\n"
                "Дальше можно создавать профиль и маршруты.",
            ),
            (
                "Шаг 2 — создай профиль (локацию)",
                "Слева:\n"
                "1) В поле **«Профиль (локация)»** нажми **«Новый профиль»**.\n"
                "2) Введи имя (например: `city_1`).\n\n"
                "После этого создаём Setup и Farm внутри этого профиля.",
            ),
            (
                "Шаг 3 — создай Setup (вход в город/локацию)",
                "Слева:\n"
                "1) Нажми **«Новый»**.\n"
                "2) В поле **«Имя маршрута»** введи `setup_...` (например `setup_city_1`).\n"
                "3) Нажми **«Старт запись»**.\n"
                "4) **СРАЗУ Alt+Tab в игру** и сделай действия (меню → вход → подтверждения → ожидания).\n"
                "   Если нужна точная пауза — во время записи можно нажать **«Вставить WAIT»** (контрольная пауза) и указать секунды.\n"
                "5) Когда закончил — **Alt+Tab обратно** и нажми **«Стоп запись»**.\n"
                "6) Нажми **«Сохранить маршрут»**.\n"
                "7) В выпадающем **«Setup маршрут (вход в город)»** выбери `setup_...`.\n\n"
                "Подсказка: ПКМ по шагу → можно вставить WAIT/KEY и отредактировать delay.",
            ),
            (
                "Шаг 4 — создай Farm (маршрут фарма)",
                "Слева (вариант запись):\n"
                "1) Нажми **«Новый»**.\n"
                "2) Введи имя `farm_...` (например `farm_city_1`).\n"
                "3) Нажми **«Старт запись»** → **Alt+Tab в игру** → сделай действия.\n"
                "4) **Alt+Tab обратно** → **«Стоп запись»** → **«Сохранить маршрут»**.\n\n"
                "Справа (вариант конструктор):\n"
                "1) Перейди на вкладку **«Маршрут»**.\n"
                "2) Нажми **«Снять скрин окна игры»**.\n"
                "3) Инструменты **Точка/Клавиша/Пауза** → кликай по скрину.\n"
                "4) Слева нажми **«Сохранить маршрут»**.",
            ),
            (
                "Шаг 5 — выбери активный Farm маршрут",
                "Слева:\n"
                "1) В выпадающем **«Farm маршрут (по карте)»** выбери `farm_...`.\n\n"
                "Теперь запуск:\n"
                "- **«Старт (маршрут)»**: Setup (если выбран) → Farm → детект.\n"
                "- **«Старт AFK»**: без маршрутов, стоим на месте + детект.",
            ),
            (
                "Подсказки (когда Alt+Tab)",
                "- После **«Старт запись»** всегда **Alt+Tab в игру** (иначе запишется не то).\n"
                "- Когда закончил действия — **Alt+Tab обратно** и жми **«Стоп запись»**.\n"
                "- Если fullscreen и фокус мешает: выключи **«Перехватывать фокус игры»**.\n"
                "- Delay в конструкторе: если **до=0**, delay фиксированный = **от**. Если **до>0** — рандом в диапазоне.",
            ),
            (
                "Вкладка «Настройки» — что значит каждый пункт",
                "**Тема** — меняет оформление.\n\n"
                "**Лог в файл** — если включить, все логи будут сохраняться в файл `logs\\session_....log`.\n"
                "- **Писать лог в файл** — включает запись.\n"
                "- **Папка** — куда сохранять.\n"
                "- **Открыть лог / Папка логов** — быстрый доступ.\n\n"
                "**Хоткей Пауза/Продолжить** — глобальная кнопка (работает даже когда игра активна). Нажал → бот ставится на паузу, нажал ещё раз → продолжает.\n\n"
                "**Планировщик** — авто‑старт и авто‑стоп по времени:\n"
                "- **Включить** — активирует планировщик.\n"
                "- **Старт (HH:MM)** — время запуска (по локальному времени Windows).\n"
                "- **Длительность (ч)** — через сколько часов бот сам остановится.\n"
                "- **Режим** — `route` (Старт маршрут) или `afk`.\n\n"
                "**Ожидание после телепорта (сек)** — сколько ждать “в базе” после ТП.\n"
                "**Удержание (сек)** — сколько держать клавиши (если игра плохо ловит нажатия — увеличь).\n"
                "**Delay шагов от/до (сек)** — общий рандом‑delay между шагами маршрута. Если “до=0” — delay фиксированный = “от”.\n\n"
                "**Клавиша телепорта** — какую кнопку нажимать для ТП.\n"
                "**ТП по врагу** — разрешить/запретить телепорт при детекте.\n"
                "**Детект радара** — включить/выключить сам детект.\n"
                "**Фокус для ТП** — можно принудительно фокусировать игру только на момент телепорта.\n\n"
                "**После ТП** — что сделать после телепорта:\n"
                "- `none` — ничего\n"
                "- `press_r` — нажать заданную клавишу (по умолчанию R)\n"
                "Поля **Клавиша** и **Задержка** относятся к этому действию.\n\n"
                "**Авто‑подтверждать окна** — периодически жмёт заданную клавишу (например Y), чтобы подтверждать диалоги.\n"
                "**Перехватывать фокус игры** — если включено, бот может активировать игру для кликов/клавиш (в fullscreen иногда лучше выключить).\n"
                "**Фарм без маршрута (стоять на месте)** — AFK‑режим: без Setup/Farm маршрутов, только стоим и детектим.\n\n"
                "Кнопки **Старт (маршрут)** / **Старт AFK** — запускают бота в соответствующем режиме.",
            ),
            (
                "Вкладка «Детект» — как настроить",
                "Тут настраивается детект по тексту **«Нет Цель поиска»**.\n\n"
                "**ВАЖНО: сначала ВЫДЕЛИ РАДАР/ОБЛАСТЬ** — выбери ROI так, чтобы в рамке был только блок радара с текстом.\n\n"
                "**Text ROI (x, y, w, h)** — область внутри окна игры, где появляется этот текст.\n"
                "**Радар: область (ROI)** — выдели область текста “Нет Цель поиска” (сохранится сразу).\n"
                "**Радар: шаблон 'Нет Цель поиска'** — сохранит `radar_empty_text.png` (эталон “пусто”).\n"
                "**Порог совпадения** — насколько текст должен совпадать с шаблоном.\n\n"
                "Ниже есть блок **«Меню/чат открыт (авто-ESC)»**:\n"
                "- Выдели ROI на индикаторе, сделай шаблон.\n"
                "- Включи тумблер — бот будет перед действиями жать ESC, если меню/чат открыт.\n\n"
                "И блок **«Смерть (детект + маршрут восстановления)»**:\n"
                "- Выдели ROI на тексте/иконке смерти, сделай шаблон.\n"
                "- Выбери маршрут восстановления (воскреснуть/выйти/вернуться).\n\n"
                "И блок **«Детект по урону + HP%»**:\n"
                "- Выдели ROI иконки атаки и сделай шаблон.\n"
                "- Выдели ROI HP полоски (лучше без цифр).\n"
                "- Включи тумблер: при атаке + HP ниже порога бот будет жать ТП много раз.\n\n"
                "Логика простая: текст **есть** → “пусто/нет цели”; текст **пропал** → “есть цель/враг” → (если разрешено) ТП.",
            ),
            (
                "Вкладка «Маршрут» — конструктор по скриншоту",
                "**Снять скрин окна игры** — берёт скрин окна игры для конструктора.\n"
                "**Инструменты**:\n"
                "- **Точка**: добавляет CLICK шаг по клику (ЛКМ=left, ПКМ=right)\n"
                "- **Клавиша**: добавляет KEY шаг\n"
                "- **Пауза**: добавляет WAIT шаг\n"
                "- **Удалить**: удаляет точку/шаг по клику по маркеру\n\n"
                "**Delay от/до (сек)** — задержка, которая запишется в шаги, добавленные в конструкторе.\n"
                "Если “до=0” — delay фиксированный = “от”, иначе рандом.\n\n"
                "Маркер точки можно **перетаскивать** мышкой. Список шагов справа синхронизирован с левым списком.",
            ),
            (
                "Вкладка «Авто банки» — автопокупка по порогу",
                "**Включить автопокупку** — включает проверку количества банок.\n"
                "**Порог (<)** — если распознанное число меньше порога, бот запускает маршрут покупки.\n"
                "**Ждать в городе (сек)** — пауза после выполнения маршрута покупки.\n"
                "**Работает при Старт (маршрут)** — если включено, автопокупка работает только при фарме по маршруту.\n"
                "**Возврат по Farm после покупки** — после покупки выполнить Farm‑маршрут, чтобы вернуться на спот.\n\n"
                "**ROI банок** — область (x,y,w,h) с цифрами количества. Можно выбрать мышью.\n"
                "**Маршрут покупки** — заранее записанный маршрут (домик → торговец → автозаполнение → покупка).\n\n"
                "Кнопки:\n"
                "- **Проверить OCR сейчас** — покажет RAW ROI и PREP mask + выведет debug в «Логи (Авто банки)».\n"
                "- **Показать кадр ROI** — просто превью того, что берётся из окна игры.",
            ),
        ]

        top = tk.Toplevel(self.root)
        top.title("Инструкция — Setup/Farm маршруты")
        top.geometry("860x520")
        top.minsize(760, 420)

        state = {"i": 0}

        title_var = tk.StringVar(value="")
        body = tk.Text(top, wrap="word", font=("Segoe UI", 10))
        body.configure(state="disabled")

        hdr = tb.Frame(top, padding=(12, 10))
        hdr.pack(fill="x")
        tb.Label(hdr, textvariable=title_var, font=("Segoe UI Semibold", 12)).pack(side="left")

        body.pack(fill="both", expand=True, padx=12, pady=(0, 10))

        btns = tb.Frame(top, padding=(12, 10))
        btns.pack(fill="x")
        btns.grid_columnconfigure(2, weight=1)

        def render() -> None:
            i = int(state["i"])
            i = max(0, min(i, len(steps) - 1))
            state["i"] = i
            t, txt = steps[i]
            title_var.set(f"{t}  ({i + 1}/{len(steps)})")
            body.configure(state="normal")
            body.delete("1.0", "end")
            body.insert("1.0", txt)
            body.configure(state="disabled")
            back_btn.configure(state=("disabled" if i == 0 else "normal"))
            next_btn.configure(state=("disabled" if i == len(steps) - 1 else "normal"))

        def go(delta: int) -> None:
            state["i"] = int(state["i"]) + int(delta)
            render()

        back_btn = tb.Button(btns, text="Назад", bootstyle="secondary", command=lambda: go(-1))
        back_btn.grid(row=0, column=0)
        next_btn = tb.Button(btns, text="Далее", bootstyle="primary", command=lambda: go(+1))
        next_btn.grid(row=0, column=1, padx=(10, 0))
        tb.Button(btns, text="Закрыть", bootstyle="outline", command=top.destroy).grid(row=0, column=3, sticky="e")

        render()


def run_app() -> None:
    store = PointsStore(Path("points.json"))
    store.load()

    root = tb.Window(themename=str(getattr(store, "ui_theme", "darkly") or "darkly"))
    ui_holder = {"ui": None}

    def log(msg: str) -> None:
        ui = ui_holder["ui"]
        if ui is not None:
            ui.log(msg)

    bot = Bot(store=store, log=log)
    ui = UI(root=root, store=store, bot=bot)
    ui_holder["ui"] = ui

    ui._editor_steps = []
    ui._refresh_points_list()
    if store.get_active_route():
        ui._load_route_into_editor(store.get_active_route())  # type: ignore[arg-type]
    ui.log(
        "Готово. Фокус на игре → 'Взять активное' → выбери Text ROI → сделай скрин текста → Старт/AFK."
    )
    root.protocol("WM_DELETE_WINDOW", lambda: (bot.stop(), root.destroy()))
    root.mainloop()

