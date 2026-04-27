from __future__ import annotations

import random
import time

import pydirectinput
import win32api
import win32con


class InputController:
    def __init__(
        self,
        min_move_time: float = 0.02,
        max_move_time: float = 0.06,
        click_base_delay_s: float = 0.02,
        click_jitter_s: float = 0.01,
        key_base_delay_s: float = 0.03,
        key_jitter_s: float = 0.02,
    ):
        self.min_move_time = float(min_move_time)
        self.max_move_time = float(max_move_time)
        self.click_base_delay_s = float(click_base_delay_s)
        self.click_jitter_s = float(click_jitter_s)
        self.key_base_delay_s = float(key_base_delay_s)
        self.key_jitter_s = float(key_jitter_s)

        # pydirectinput safety knobs
        pydirectinput.PAUSE = 0.0
        pydirectinput.FAILSAFE = False

    def _jitter(self, base_s: float, jitter_s: float) -> None:
        delay = max(0.0, base_s + random.uniform(-jitter_s, jitter_s))
        time.sleep(delay)

    def move_and_click_abs(self, x: int, y: int, button: str = "left") -> None:
        move_time = random.uniform(self.min_move_time, self.max_move_time)
        pydirectinput.moveTo(int(x), int(y), duration=move_time)
        self._jitter(self.click_base_delay_s, self.click_jitter_s)
        pydirectinput.click(button=button)

    def click_abs(self, x: int, y: int, button: str = "left") -> None:
        """
        Click at absolute screen coordinates with minimal movement delay.
        Useful for "click to focus" where we want as little cursor travel as possible.
        """
        self._jitter(self.click_base_delay_s, self.click_jitter_s)
        pydirectinput.click(int(x), int(y), button=button)

    def mouse_down(self, button: str = "left") -> None:
        try:
            pydirectinput.mouseDown(button=button)
        except Exception:
            pass

    def mouse_up(self, button: str = "left") -> None:
        try:
            pydirectinput.mouseUp(button=button)
        except Exception:
            pass

    def move_rel(self, dx: int, dy: int, *, duration: float = 0.0) -> None:
        try:
            pydirectinput.moveRel(int(dx), int(dy), duration=float(duration))
        except Exception:
            # fallback: absolute move by current pos
            try:
                x, y = win32api.GetCursorPos()
                pydirectinput.moveTo(int(x + dx), int(y + dy), duration=float(duration))
            except Exception:
                pass

    def press_key(self, key: str) -> None:
        self.press_key_hold(key, hold_s=0.05)

    def press_key_hold(self, key: str, hold_s: float = 0.05) -> None:
        """
        Some games miss very short taps. Holding the key for a few ms makes
        input more reliable in fullscreen/DirectInput titles.
        """
        self._jitter(self.key_base_delay_s, self.key_jitter_s)
        k = (key or "").strip().lower()
        if not k:
            return
        try:
            pydirectinput.keyDown(k)
            time.sleep(max(0.01, float(hold_s)))
        finally:
            try:
                pydirectinput.keyUp(k)
            except Exception:
                pass

    def press_vk_hold(self, vk: int, hold_s: float = 0.05) -> None:
        """
        Fallback for keys that don't map nicely to pydirectinput names.
        Uses Win32 keybd_event by virtual-key code.
        """
        v = int(vk) & 0xFF
        if v <= 0:
            return
        self._jitter(self.key_base_delay_s, self.key_jitter_s)
        try:
            win32api.keybd_event(v, 0, 0, 0)
            time.sleep(max(0.01, float(hold_s)))
        finally:
            try:
                win32api.keybd_event(v, 0, win32con.KEYEVENTF_KEYUP, 0)
            except Exception:
                pass

    def press_key_any(self, key: str, *, hold_s: float = 0.05) -> None:
        """
        Press either a normal pydirectinput key name or a raw VK marker ("vk:NN" or "vk:0xNN").
        """
        k = str(key or "").strip().lower()
        if not k:
            return
        if k.startswith("vk:"):
            raw = k[3:].strip()
            try:
                v = int(raw, 16) if raw.startswith("0x") else int(raw)
            except Exception:
                return
            self.press_vk_hold(int(v), hold_s=hold_s)
            return
        self.press_key_hold(k, hold_s=hold_s)

