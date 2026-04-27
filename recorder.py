from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

import win32con
import win32gui
import win32api

from bot_logic import RouteStep
from window_manager import WindowNotFoundError, get_window_rect


@dataclass
class RecorderConfig:
    min_step_delay_s: float = 0.05
    poll_interval_s: float = 0.003


def _vk_to_key_name(vk: int) -> Optional[str]:
    # A-Z
    if 0x41 <= vk <= 0x5A:
        return chr(vk).lower()
    # 0-9
    if 0x30 <= vk <= 0x39:
        return chr(vk)
    # F1-F12
    if win32con.VK_F1 <= vk <= win32con.VK_F12:
        return f"f{vk - win32con.VK_F1 + 1}"
    # common
    mapping = {
        win32con.VK_SPACE: "space",
        win32con.VK_RETURN: "enter",
        win32con.VK_ESCAPE: "esc",
        win32con.VK_TAB: "tab",
        win32con.VK_BACK: "backspace",
        win32con.VK_DELETE: "delete",
        win32con.VK_LEFT: "left",
        win32con.VK_RIGHT: "right",
        win32con.VK_UP: "up",
        win32con.VK_DOWN: "down",
        win32con.VK_HOME: "home",
        win32con.VK_END: "end",
        win32con.VK_PRIOR: "pageup",
        win32con.VK_NEXT: "pagedown",
    }
    if vk in mapping:
        return mapping[vk]
    # For everything else, keep the raw VK marker so playback still works via press_vk_hold
    # (InputController.press_key_any handles "vk:NN").
    return f"vk:{int(vk)}"


class RouteRecorder:
    """
    Recorder that uses polling (GetAsyncKeyState) instead of low-level hooks.
    This is more reliable for some games where hooks don't receive input.
    Records only while the game root window is foreground.
    """

    def __init__(
        self,
        game_hwnd: int,
        cfg: RecorderConfig,
        on_step: Callable[[RouteStep], None],
        on_log: Callable[[str], None],
    ):
        self.game_hwnd = int(game_hwnd)
        try:
            self._game_root_hwnd = int(win32gui.GetAncestor(self.game_hwnd, win32con.GA_ROOT))
        except Exception:
            self._game_root_hwnd = self.game_hwnd

        self.cfg = cfg
        self.on_step = on_step
        self.on_log = on_log

        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_ts: Optional[float] = None
        self._lock = threading.Lock()

        self._prev_left = False
        self._prev_right = False
        self._prev_mid = False
        self._prev_x1 = False
        self._prev_x2 = False
        self._prev_keys: dict[int, bool] = {}

        # Record virtually all VKs (so route recording captures "everything").
        self._watch_vks = list(range(1, 256))
        # Mouse buttons are recorded separately (click steps), so skip them here.
        for vk in [win32con.VK_LBUTTON, win32con.VK_RBUTTON, win32con.VK_MBUTTON, win32con.VK_XBUTTON1, win32con.VK_XBUTTON2]:
            try:
                self._watch_vks.remove(int(vk))
            except ValueError:
                pass

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def stop(self) -> None:
        self._stop.set()
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=1.0)
        self._thread = None

    def _step_delay(self) -> float:
        now = time.monotonic()
        with self._lock:
            if self._last_ts is None:
                self._last_ts = now
                return 0.0
            d = now - self._last_ts
            self._last_ts = now
            return max(0.0, d)

    def _is_game_foreground(self) -> bool:
        fg = win32gui.GetForegroundWindow()
        if not fg:
            return False
        try:
            root = int(win32gui.GetAncestor(int(fg), win32con.GA_ROOT))
        except Exception:
            root = int(fg)
        return root == self._game_root_hwnd

    def _record_step(self, step: RouteStep) -> None:
        d = self._step_delay()
        step.delay_s = max(float(self.cfg.min_step_delay_s), float(d))
        self.on_step(step)

    def _poll_down(self, vk: int) -> bool:
        # high bit set => key is down
        return bool(win32api.GetAsyncKeyState(vk) & 0x8000)

    def _run(self) -> None:
        with self._lock:
            self._last_ts = time.monotonic()
        self.on_log("Запись маршрута начата (polling). Нажми 'Стоп запись' в UI, когда закончишь.")

        while not self._stop.is_set():
            time.sleep(max(0.005, float(self.cfg.poll_interval_s)))

            game_fg = self._is_game_foreground()

            try:
                rect = get_window_rect(self.game_hwnd)
            except WindowNotFoundError:
                continue

            x, y = win32api.GetCursorPos()
            in_win = rect.left <= x < rect.right and rect.top <= y < rect.bottom

            # Mouse: edge-detect down state (doesn't "consume" click for some games).
            left_down = self._poll_down(win32con.VK_LBUTTON)
            right_down = self._poll_down(win32con.VK_RBUTTON)
            mid_down = self._poll_down(win32con.VK_MBUTTON)
            x1_down = self._poll_down(win32con.VK_XBUTTON1)
            x2_down = self._poll_down(win32con.VK_XBUTTON2)

            if in_win and left_down and not self._prev_left:
                self._record_step(
                    RouteStep(kind="click", rel_x=int(x - rect.left), rel_y=int(y - rect.top), button="left")
                )
            if in_win and right_down and not self._prev_right:
                self._record_step(
                    RouteStep(kind="click", rel_x=int(x - rect.left), rel_y=int(y - rect.top), button="right")
                )
            if in_win and mid_down and not self._prev_mid:
                self._record_step(
                    RouteStep(kind="click", rel_x=int(x - rect.left), rel_y=int(y - rect.top), button="middle")
                )
            if in_win and x1_down and not self._prev_x1:
                self._record_step(
                    RouteStep(kind="click", rel_x=int(x - rect.left), rel_y=int(y - rect.top), button="x1")
                )
            if in_win and x2_down and not self._prev_x2:
                self._record_step(
                    RouteStep(kind="click", rel_x=int(x - rect.left), rel_y=int(y - rect.top), button="x2")
                )

            self._prev_left = left_down
            self._prev_right = right_down
            self._prev_mid = mid_down
            self._prev_x1 = x1_down
            self._prev_x2 = x2_down

            # keys
            # For popups, cursor can be outside window while game still consumes keys.
            # Record keys when the game is foreground OR cursor is inside the window.
            if game_fg or in_win:
                for vk in self._watch_vks:
                    down = self._poll_down(vk)
                    prev = self._prev_keys.get(vk, False)
                    if down and not prev:
                        name = _vk_to_key_name(vk)
                        if name:
                            self._record_step(RouteStep(kind="key", key=name))
                    self._prev_keys[vk] = down
            else:
                # keep state updated to avoid "phantom" presses when re-entering window
                for vk in self._watch_vks:
                    self._prev_keys[vk] = self._poll_down(vk)

    def start(self) -> None:
        if self.is_running():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="RouteRecorder", daemon=True)
        self._thread.start()

    def insert_wait_marker(self, seconds: float) -> None:
        """
        Insert a WAIT marker step from UI during recording.
        Also resets internal timer so the next recorded action delay does not include this wait.
        """
        s = max(0.0, float(seconds))
        # Record a wait step with explicit duration.
        self.on_step(RouteStep(kind="wait", delay_s=s))
        # Reset delay baseline.
        with self._lock:
            self._last_ts = time.monotonic()

