from __future__ import annotations

from dataclasses import dataclass

import win32con
import win32gui
import win32api
import win32process


class WindowNotFoundError(RuntimeError):
    pass


@dataclass(frozen=True)
class WindowRect:
    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return max(0, self.right - self.left)

    @property
    def height(self) -> int:
        return max(0, self.bottom - self.top)


def _is_window_valid(hwnd: int) -> bool:
    if not win32gui.IsWindow(hwnd):
        return False
    if not win32gui.IsWindowVisible(hwnd):
        return False
    if win32gui.IsIconic(hwnd):
        return False
    return True


def find_window_by_title(window_title: str) -> int:
    """
    Finds a top-level window by exact title match.
    Raises WindowNotFoundError if not found/valid.
    """
    hwnd = win32gui.FindWindow(None, window_title)
    if not hwnd or not _is_window_valid(hwnd):
        raise WindowNotFoundError(f"Окно не найдено или недоступно: {window_title!r}")
    return hwnd


def get_foreground_window() -> int:
    hwnd = win32gui.GetForegroundWindow()
    if not hwnd or not _is_window_valid(hwnd):
        raise WindowNotFoundError("Активное окно недоступно (нет foreground окна).")
    return hwnd


def get_window_title(hwnd: int) -> str:
    if not _is_window_valid(hwnd):
        raise WindowNotFoundError("Окно недоступно.")
    return win32gui.GetWindowText(hwnd) or ""


def list_visible_windows(max_items: int = 200) -> list[tuple[int, str]]:
    items: list[tuple[int, str]] = []

    def enum_cb(hwnd: int, _lparam) -> None:
        nonlocal items
        if len(items) >= max_items:
            return
        if not win32gui.IsWindow(hwnd) or not win32gui.IsWindowVisible(hwnd):
            return
        if win32gui.IsIconic(hwnd):
            return
        title = win32gui.GetWindowText(hwnd) or ""
        title = title.strip()
        if not title:
            return
        # Skip our own app windows by title prefix
        if title.startswith("RAVEN BOT"):
            return
        items.append((int(hwnd), title))

    win32gui.EnumWindows(enum_cb, None)
    # keep stable order (by title then hwnd)
    items = sorted(items, key=lambda x: (x[1].lower(), x[0]))
    return items[:max_items]


def format_window_item(hwnd: int, title: str) -> str:
    return f"{title} (0x{int(hwnd):X})"


def parse_window_item(s: str) -> tuple[str, int] | None:
    """
    Parses strings like: "Some Title (0x1A2B3C)" -> (title, hwnd)
    """
    text = (s or "").strip()
    if not text.endswith(")") or "(0x" not in text:
        return None
    try:
        title, tail = text.rsplit(" (0x", 1)
        hex_part = tail[:-1]  # remove trailing ')'
        hwnd = int(hex_part, 16)
        return title, hwnd
    except Exception:
        return None


def bring_window_to_foreground(hwnd: int) -> None:
    """
    Best-effort bring target window to foreground.
    Windows sometimes blocks SetForegroundWindow; AttachThreadInput can help in many cases.
    """
    try:
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    except Exception:
        pass

    try:
        # Basic attempt first
        win32gui.SetForegroundWindow(hwnd)
        return
    except Exception:
        pass

    # Aggressive attempt: attach input threads between current foreground and target
    try:
        fg = int(win32gui.GetForegroundWindow() or 0)
        if fg:
            fg_tid, _ = win32process.GetWindowThreadProcessId(fg)
        else:
            fg_tid = 0
        tgt_tid, _ = win32process.GetWindowThreadProcessId(int(hwnd))

        cur_tid = int(win32api.GetCurrentThreadId())

        attached = []
        try:
            if fg_tid and fg_tid != cur_tid:
                win32process.AttachThreadInput(cur_tid, fg_tid, True)
                attached.append((cur_tid, fg_tid))
            if tgt_tid and tgt_tid != cur_tid:
                win32process.AttachThreadInput(cur_tid, tgt_tid, True)
                attached.append((cur_tid, tgt_tid))
            if fg_tid and tgt_tid and fg_tid != tgt_tid:
                win32process.AttachThreadInput(fg_tid, tgt_tid, True)
                attached.append((fg_tid, tgt_tid))

            try:
                win32gui.BringWindowToTop(int(hwnd))
            except Exception:
                pass
            # Topmost toggle sometimes helps
            try:
                win32gui.SetWindowPos(
                    int(hwnd),
                    win32con.HWND_TOPMOST,
                    0,
                    0,
                    0,
                    0,
                    win32con.SWP_NOMOVE | win32con.SWP_NOSIZE,
                )
                win32gui.SetWindowPos(
                    int(hwnd),
                    win32con.HWND_NOTOPMOST,
                    0,
                    0,
                    0,
                    0,
                    win32con.SWP_NOMOVE | win32con.SWP_NOSIZE,
                )
            except Exception:
                pass

            try:
                win32gui.SetForegroundWindow(int(hwnd))
            except Exception:
                pass
        finally:
            for a, b in reversed(attached):
                try:
                    win32process.AttachThreadInput(int(a), int(b), False)
                except Exception:
                    pass
    except Exception:
        # Some games/contexts still prevent focus stealing; we best-effort.
        pass


def get_window_rect(hwnd: int) -> WindowRect:
    if not _is_window_valid(hwnd):
        raise WindowNotFoundError("Окно стало недоступно (закрыто/свернуто).")

    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    return WindowRect(left=left, top=top, right=right, bottom=bottom)


def to_abs_xy(window_rect: WindowRect, rel_x: int, rel_y: int) -> tuple[int, int]:
    return window_rect.left + rel_x, window_rect.top + rel_y


def clamp_rel_xy(window_rect: WindowRect, rel_x: int, rel_y: int) -> tuple[int, int]:
    rel_x = max(0, min(rel_x, window_rect.width - 1))
    rel_y = max(0, min(rel_y, window_rect.height - 1))
    return rel_x, rel_y

