from __future__ import annotations

import json
import random
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import win32api
import cv2
import win32gui
import win32con
import numpy as np
import winsound
import os
try:
    import requests  # type: ignore
except Exception:
    requests = None  # type: ignore

from input_controller import InputController
from vision import EnemyColorHSV, RadarROI, Vision
from ocr_utils import read_int_from_bgr, read_int_debug
from window_manager import (
    WindowNotFoundError,
    bring_window_to_foreground,
    clamp_rel_xy,
    find_window_by_title,
    get_foreground_window,
    get_window_title,
    get_window_rect,
    to_abs_xy,
)
 


@dataclass
class Point:
    name: str
    rel_x: int
    rel_y: int

    def as_dict(self) -> dict:
        return {"name": self.name, "rel_x": int(self.rel_x), "rel_y": int(self.rel_y)}

    @staticmethod
    def from_dict(d: dict) -> "Point":
        return Point(name=str(d["name"]), rel_x=int(d["rel_x"]), rel_y=int(d["rel_y"]))


@dataclass
class RouteStep:
    kind: str  # "click" | "key" | "wait" | "wait_range" | "confirm" | "gate"
    rel_x: int = 0
    rel_y: int = 0
    x_pct: float | None = None
    y_pct: float | None = None
    button: str = "left"
    key: str = ""
    delay_s: float = 0.15
    timeout_s: float = 6.0  # used by kind="confirm"
    min_s: float = 0.0  # used by kind="wait_range"
    max_s: float = 0.0  # used by kind="wait_range"

    def as_dict(self) -> dict:
        d = {
            "kind": self.kind,
            "rel_x": int(self.rel_x),
            "rel_y": int(self.rel_y),
            "button": self.button,
            "key": self.key,
            "delay_s": float(self.delay_s),
        }
        if self.kind == "click" and self.x_pct is not None and self.y_pct is not None:
            d["x_pct"] = float(self.x_pct)
            d["y_pct"] = float(self.y_pct)
        if self.kind == "confirm":
            d["timeout_s"] = float(self.timeout_s)
        if self.kind == "wait_range":
            d["min_s"] = float(self.min_s)
            d["max_s"] = float(self.max_s)
        return d

    @staticmethod
    def from_dict(d: dict) -> "RouteStep":
        return RouteStep(
            kind=str(d.get("kind", "click")),
            rel_x=int(d.get("rel_x", 0)),
            rel_y=int(d.get("rel_y", 0)),
            x_pct=(float(d["x_pct"]) if "x_pct" in d and d["x_pct"] is not None else None),
            y_pct=(float(d["y_pct"]) if "y_pct" in d and d["y_pct"] is not None else None),
            button=str(d.get("button", "left")),
            key=str(d.get("key", "")),
            delay_s=float(d.get("delay_s", 0.15)),
            timeout_s=float(d.get("timeout_s", 6.0)),
            min_s=float(d.get("min_s", 0.0)),
            max_s=float(d.get("max_s", 0.0)),
        )


@dataclass
class Route:
    name: str
    steps: list[RouteStep]

    def as_dict(self) -> dict:
        return {"name": self.name, "steps": [s.as_dict() for s in self.steps]}

    @staticmethod
    def from_dict(d: dict) -> "Route":
        return Route(
            name=str(d.get("name", "route")),
            steps=[RouteStep.from_dict(x) for x in d.get("steps", [])],
        )


@dataclass
class Profile:
    name: str
    routes: list[Route]
    setup_route_name: str | None = None  # enter city/location
    farm_route_name: str | None = None  # open map + pathing to farm

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "routes": [r.as_dict() for r in self.routes],
            "setup_route_name": self.setup_route_name,
            "farm_route_name": self.farm_route_name,
        }

    @staticmethod
    def from_dict(d: dict) -> "Profile":
        return Profile(
            name=str(d.get("name", "profile")),
            routes=[Route.from_dict(x) for x in d.get("routes", [])],
            setup_route_name=(d.get("setup_route_name") if d.get("setup_route_name") else None),
            farm_route_name=(d.get("farm_route_name") if d.get("farm_route_name") else None),
        )


class PointsStore:
    def __init__(self, path: Path):
        self.path = path
        self.points: list[Point] = []  # legacy single-click points
        self.active_name: Optional[str] = None  # legacy active point

        # Legacy (kept for backward compat; populated via migration)
        self.routes: list[Route] = []
        self.active_route_name: Optional[str] = None

        # Profiles (locations)
        self.profiles: list[Profile] = []
        self.active_profile_name: str = "default"

        self.window_title: str = ""  # optional, for display
        self.window_hwnd: int = 0  # preferred (stable) window id
        self.radar_roi = RadarROI()
        self.enemy_hsv = EnemyColorHSV()
        self.detect_mode: str = "diff"  # keep in file; UI defaults to diff
        self.empty_radar_path: str = "radar_empty.png"
        self.empty_text_path: str = "radar_empty_text.png"
        self.empty_text_roi = RadarROI(x=0, y=0, w=1, h=1)
        self.empty_text_threshold: float = 0.86
        self.diff_min_changed_pixels: int = 400
        self.diff_threshold: int = 22
        self.teleport_key: str = "f4"
        self.key_hold_s: float = 0.06
        self.tp_on_enemy_enabled: bool = True
        self.radar_detect_enabled: bool = True
        self.post_tp_action: str = "press_r"  # "none" | "press_r" | "click_radar"
        self.post_tp_key: str = "r"
        self.post_tp_delay_s: float = 0.25
        self.auto_confirm_enabled: bool = True
        self.auto_confirm_key: str = "y"
        self.auto_confirm_interval_s: float = 0.8
        # If False, bot won't force-activate game window (useful in fullscreen while alt-tabbed).
        self.focus_steal_enabled: bool = True
        # If True, bot may force-focus the game ONLY when it needs to teleport.
        self.tp_focus_steal_enabled: bool = True
        self.farm_without_route: bool = False  # if True: no start route, no return route
        self.detect_confirm_streak: int = 3  # require N consecutive detections before TP
        self.min_tp_cooldown_s: float = 10.0  # minimum seconds between teleports
        self.empty_match_threshold: float = 0.88  # if score >= threshold => empty => no enemy
        # Teleport wait range (seconds). UI uses min/max and picks random in [min..max] each TP.
        self.teleport_wait_min_s: int = 60
        self.teleport_wait_max_s: int = 60
        self.check_interval_s: float = 0.25
        self.loop_jitter_s: float = 0.05
        self.move_time_min_s: float = 0.02
        self.move_time_max_s: float = 0.06
        # Random delay between route steps (if max>0 and max>=min). If disabled, uses per-step delay_s.
        self.route_delay_min_s: float = 0.0
        self.route_delay_max_s: float = 0.0

        # --- Auto-buy potions (OCR-based) ---
        self.auto_buy_potions_enabled: bool = False
        self.auto_buy_potions_threshold: int = 300
        # ROI in client coords where the potion count digits are rendered
        self.auto_buy_potions_roi = RadarROI(x=0, y=0, w=1, h=1)
        # Name of a route to execute when below threshold (should open town + buy)
        self.auto_buy_potions_route_name: str | None = None
        self.auto_buy_city_wait_s: float = 8.0
        self.auto_buy_check_interval_s: float = 5.0
        self.auto_buy_cooldown_s: float = 60.0
        # Only run auto-buy while in "route farming" mode (started via Start Route)
        self.auto_buy_route_mode_only: bool = True
        # After buying, run Farm route to return to spot
        self.auto_buy_return_to_farm: bool = True

        # --- Enemy sound alert ---
        self.enemy_alert_enabled: bool = False
        self.enemy_alert_beeps: int = 2
        self.enemy_alert_interval_s: float = 8.0
        # Optional custom sound (.wav). If set and file exists, will be played instead of Beep.
        self.enemy_alert_sound_path: str = ""

        # --- File logging (night mode) ---
        self.log_to_file_enabled: bool = False
        self.log_dir: str = "logs"

        # --- Telegram alerts (screenshot on attacked) ---
        self.telegram_enabled: bool = False
        self.telegram_bot_token: str = ""
        self.telegram_chat_id: str = ""
        self.telegram_send_on_attacked: bool = True
        self.telegram_interval_s: float = 30.0

        # --- Hotkeys ---
        # Key name from preset list, e.g. "f8", "pause", "scrolllock"
        self.pause_hotkey: str = "f8"
        self.stop_record_hotkey: str = "f9"

        # --- Menu/chat auto-close (ESC) ---
        self.menu_autoclose_enabled: bool = False
        self.menu_autoclose_key: str = "esc"
        self.menu_autoclose_attempts: int = 2
        self.menu_autoclose_roi = RadarROI(x=0, y=0, w=1, h=1)
        self.menu_autoclose_tpl_path: str = "menu_open_tpl.png"
        self.menu_autoclose_threshold: float = 0.86
        self.menu_autoclose_cooldown_s: float = 1.0

        # --- Confirm popup (gate/enter dungeon) ---
        self.confirm_popup_roi = RadarROI(x=0, y=0, w=1, h=1)
        self.confirm_popup_tpl_path: str = "confirm_popup_tpl.png"
        self.confirm_popup_threshold: float = 0.86

        # --- Gate click assist (rotate camera by RMB + click gate) ---
        self.gate_roi = RadarROI(x=0, y=0, w=1, h=1)
        self.gate_tpl_path: str = "gate_tpl.png"
        self.gate_threshold: float = 0.83
        self.gate_seek_timeout_s: float = 6.0
        self.gate_center_margin_px: int = 40
        self.gate_turn_step_px: int = 120

        # --- Death detection + recovery route ---
        self.death_detect_enabled: bool = False
        self.death_roi = RadarROI(x=0, y=0, w=1, h=1)
        self.death_tpl_path: str = "death_tpl.png"
        self.death_threshold: float = 0.86
        self.death_route_name: str | None = None
        self.death_cooldown_s: float = 20.0

        # --- Scheduler ---
        self.schedule_enabled: bool = False
        self.schedule_start_hhmm: str = "02:00"
        self.schedule_duration_h: float = 8.0
        self.schedule_mode: str = "route"  # "route" | "afk"
        self.schedule_daily: bool = True
        self.schedule_last_start_date: str = ""  # YYYY-MM-DD (UI uses this to avoid double-start)

        # --- Damage/HP teleport mode ---
        # When enabled: ignore "ТП по врагу" and use attacked-icon + HP threshold trigger.
        self.damage_tp_enabled: bool = False
        # ROI for attacked (swords) icon
        self.damage_icon_roi = RadarROI(x=0, y=0, w=1, h=1)
        self.damage_icon_tpl_path: str = "damage_icon_tpl.png"
        self.damage_icon_threshold: float = 0.86
        # Optional dual-template mode: normal icon vs attacked(swords) icon
        # ROI for normal (body) icon (can differ from swords ROI)
        self.damage_icon_normal_roi = RadarROI(x=0, y=0, w=1, h=1)
        self.damage_icon_normal_tpl_path: str = "damage_icon_normal_tpl.png"
        self.damage_icon_normal_threshold: float = 0.86
        self.damage_icon_margin: float = 0.04  # how much better attacked score must be than normal
        self.hp_bar_roi = RadarROI(x=0, y=0, w=1, h=1)
        self.hp_tp_enabled: bool = True
        self.hp_tp_threshold_pct: int = 70
        self.damage_tp_press_count: int = 6
        self.damage_tp_press_interval_s: float = 0.12
        self.damage_tp_cooldown_s: float = 8.0

        # UI preference
        self.ui_theme: str = "darkly"

    def load(self) -> None:
        if not self.path.exists():
            return
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.window_title = str(data.get("window_title", ""))
        self.window_hwnd = int(data.get("window_hwnd", 0) or 0)

        self.points = [Point.from_dict(x) for x in data.get("points", [])]
        self.active_name = data.get("active_name")

        self.routes = [Route.from_dict(x) for x in data.get("routes", [])]
        self.active_route_name = data.get("active_route_name")

        # Profiles (preferred)
        self.profiles = [Profile.from_dict(x) for x in data.get("profiles", [])]
        self.active_profile_name = str(data.get("active_profile_name", self.active_profile_name or "default") or "default")

        # Backward compat: if only points exist, expose them as single-step routes.
        if not self.routes and self.points:
            self.routes = [Route(name=p.name, steps=[RouteStep(kind="click", rel_x=p.rel_x, rel_y=p.rel_y)]) for p in self.points]
            self.active_route_name = self.active_route_name or self.active_name

        # Migration: if no profiles found, create default profile from legacy routes.
        if not self.profiles:
            farm = self.active_route_name
            self.profiles = [Profile(name="default", routes=list(self.routes), setup_route_name=None, farm_route_name=farm)]
            self.active_profile_name = "default"

        self.radar_roi = RadarROI.from_dict(data.get("radar_roi", {}))
        self.enemy_hsv = EnemyColorHSV.from_dict(data.get("enemy_hsv", {}))
        self.detect_mode = str(data.get("detect_mode", "diff"))
        self.empty_radar_path = str(data.get("empty_radar_path", "radar_empty.png"))
        self.empty_text_path = str(data.get("empty_text_path", "radar_empty_text.png"))
        self.empty_text_roi = RadarROI.from_dict(data.get("empty_text_roi", {}))
        self.empty_text_threshold = float(data.get("empty_text_threshold", 0.86))
        self.diff_min_changed_pixels = int(data.get("diff_min_changed_pixels", 400))
        self.diff_threshold = int(data.get("diff_threshold", 22))
        self.teleport_key = str(data.get("teleport_key", "f4"))
        self.key_hold_s = float(data.get("key_hold_s", 0.06))
        self.tp_on_enemy_enabled = bool(data.get("tp_on_enemy_enabled", True))
        self.radar_detect_enabled = bool(data.get("radar_detect_enabled", True))
        self.post_tp_action = str(data.get("post_tp_action", "press_r"))
        self.post_tp_key = str(data.get("post_tp_key", "r"))
        self.post_tp_delay_s = float(data.get("post_tp_delay_s", 0.25))
        self.auto_confirm_enabled = bool(data.get("auto_confirm_enabled", True))
        self.auto_confirm_key = str(data.get("auto_confirm_key", "y"))
        self.auto_confirm_interval_s = float(data.get("auto_confirm_interval_s", 0.8))
        # Backward/forward compat:
        # - prefer explicit boolean if present
        # - if newer "focus_mode" exists, treat "never" as disabled, otherwise enabled
        if "focus_steal_enabled" in data:
            self.focus_steal_enabled = bool(data.get("focus_steal_enabled", True))
        else:
            mode = str(data.get("focus_mode", "always")).strip().lower()
            self.focus_steal_enabled = (mode != "never")
        self.tp_focus_steal_enabled = bool(data.get("tp_focus_steal_enabled", True))
        self.farm_without_route = bool(data.get("farm_without_route", False))
        self.detect_confirm_streak = int(data.get("detect_confirm_streak", 3))
        self.min_tp_cooldown_s = float(data.get("min_tp_cooldown_s", 10.0))
        self.empty_match_threshold = float(data.get("empty_match_threshold", 0.88))
        # Backward compat: old single teleport_wait_s
        if "teleport_wait_min_s" in data or "teleport_wait_max_s" in data:
            self.teleport_wait_min_s = int(data.get("teleport_wait_min_s", data.get("teleport_wait_s", 60)))
            self.teleport_wait_max_s = int(data.get("teleport_wait_max_s", data.get("teleport_wait_s", 60)))
        else:
            v = int(data.get("teleport_wait_s", 60))
            self.teleport_wait_min_s = v
            self.teleport_wait_max_s = v
        self.check_interval_s = float(data.get("check_interval_s", 0.25))
        self.loop_jitter_s = float(data.get("loop_jitter_s", 0.05))
        self.move_time_min_s = float(data.get("move_time_min_s", 0.02))
        self.move_time_max_s = float(data.get("move_time_max_s", 0.06))
        self.route_delay_min_s = float(data.get("route_delay_min_s", 0.0))
        self.route_delay_max_s = float(data.get("route_delay_max_s", 0.0))
        self.auto_buy_potions_enabled = bool(data.get("auto_buy_potions_enabled", False))
        self.auto_buy_potions_threshold = int(data.get("auto_buy_potions_threshold", 300))
        self.auto_buy_potions_roi = RadarROI.from_dict(data.get("auto_buy_potions_roi", {}))
        self.auto_buy_potions_route_name = (data.get("auto_buy_potions_route_name") if data.get("auto_buy_potions_route_name") else None)
        self.auto_buy_city_wait_s = float(data.get("auto_buy_city_wait_s", 8.0))
        self.auto_buy_check_interval_s = float(data.get("auto_buy_check_interval_s", 5.0))
        self.auto_buy_cooldown_s = float(data.get("auto_buy_cooldown_s", 60.0))
        self.auto_buy_route_mode_only = bool(data.get("auto_buy_route_mode_only", True))
        self.auto_buy_return_to_farm = bool(data.get("auto_buy_return_to_farm", True))
        self.enemy_alert_enabled = bool(data.get("enemy_alert_enabled", False))
        self.enemy_alert_beeps = int(data.get("enemy_alert_beeps", 2))
        self.enemy_alert_interval_s = float(data.get("enemy_alert_interval_s", 8.0))
        self.enemy_alert_sound_path = str(data.get("enemy_alert_sound_path", "") or "")
        self.log_to_file_enabled = bool(data.get("log_to_file_enabled", False))
        self.log_dir = str(data.get("log_dir", "logs") or "logs")
        # Telegram
        self.telegram_enabled = bool(data.get("telegram_enabled", False))
        self.telegram_bot_token = str(data.get("telegram_bot_token", "") or "").strip()
        self.telegram_chat_id = str(data.get("telegram_chat_id", "") or "").strip()
        self.telegram_send_on_attacked = bool(data.get("telegram_send_on_attacked", True))
        self.telegram_interval_s = float(data.get("telegram_interval_s", 30.0))
        self.pause_hotkey = str(data.get("pause_hotkey", "f8") or "f8").strip().lower()
        self.stop_record_hotkey = str(data.get("stop_record_hotkey", "f9") or "f9").strip().lower()
        self.menu_autoclose_enabled = bool(data.get("menu_autoclose_enabled", False))
        self.menu_autoclose_key = str(data.get("menu_autoclose_key", "esc") or "esc").strip().lower()
        self.menu_autoclose_attempts = int(data.get("menu_autoclose_attempts", 2))
        self.menu_autoclose_roi = RadarROI.from_dict(data.get("menu_autoclose_roi", {}))
        self.menu_autoclose_tpl_path = str(data.get("menu_autoclose_tpl_path", "menu_open_tpl.png") or "menu_open_tpl.png")
        self.menu_autoclose_threshold = float(data.get("menu_autoclose_threshold", 0.86))
        self.menu_autoclose_cooldown_s = float(data.get("menu_autoclose_cooldown_s", 1.0))
        self.confirm_popup_roi = RadarROI.from_dict(data.get("confirm_popup_roi", {}))
        self.confirm_popup_tpl_path = str(data.get("confirm_popup_tpl_path", "confirm_popup_tpl.png") or "confirm_popup_tpl.png")
        self.confirm_popup_threshold = float(data.get("confirm_popup_threshold", 0.86))
        self.gate_roi = RadarROI.from_dict(data.get("gate_roi", {}))
        self.gate_tpl_path = str(data.get("gate_tpl_path", "gate_tpl.png") or "gate_tpl.png")
        self.gate_threshold = float(data.get("gate_threshold", 0.83))
        self.gate_seek_timeout_s = float(data.get("gate_seek_timeout_s", 6.0))
        self.gate_center_margin_px = int(data.get("gate_center_margin_px", 40))
        self.gate_turn_step_px = int(data.get("gate_turn_step_px", 120))
        self.death_detect_enabled = bool(data.get("death_detect_enabled", False))
        self.death_roi = RadarROI.from_dict(data.get("death_roi", {}))
        self.death_tpl_path = str(data.get("death_tpl_path", "death_tpl.png") or "death_tpl.png")
        self.death_threshold = float(data.get("death_threshold", 0.86))
        self.death_route_name = (data.get("death_route_name") if data.get("death_route_name") else None)
        self.death_cooldown_s = float(data.get("death_cooldown_s", 20.0))
        self.schedule_enabled = bool(data.get("schedule_enabled", False))
        self.schedule_start_hhmm = str(data.get("schedule_start_hhmm", "02:00") or "02:00").strip()
        self.schedule_duration_h = float(data.get("schedule_duration_h", 8.0))
        self.schedule_mode = str(data.get("schedule_mode", "route") or "route").strip().lower()
        self.schedule_daily = bool(data.get("schedule_daily", True))
        self.schedule_last_start_date = str(data.get("schedule_last_start_date", "") or "").strip()
        self.damage_tp_enabled = bool(data.get("damage_tp_enabled", False))
        self.damage_icon_roi = RadarROI.from_dict(data.get("damage_icon_roi", {}))
        # Backward compat: if normal ROI not present, use same as swords ROI
        if "damage_icon_normal_roi" in data:
            self.damage_icon_normal_roi = RadarROI.from_dict(data.get("damage_icon_normal_roi", {}))
        else:
            self.damage_icon_normal_roi = RadarROI.from_dict(data.get("damage_icon_roi", {}))
        self.damage_icon_tpl_path = str(data.get("damage_icon_tpl_path", "damage_icon_tpl.png") or "damage_icon_tpl.png")
        self.damage_icon_threshold = float(data.get("damage_icon_threshold", 0.86))
        self.damage_icon_normal_tpl_path = str(
            data.get("damage_icon_normal_tpl_path", "damage_icon_normal_tpl.png") or "damage_icon_normal_tpl.png"
        )
        self.damage_icon_normal_threshold = float(data.get("damage_icon_normal_threshold", 0.86))
        self.damage_icon_margin = float(data.get("damage_icon_margin", 0.04))
        self.hp_bar_roi = RadarROI.from_dict(data.get("hp_bar_roi", {}))
        self.hp_tp_enabled = bool(data.get("hp_tp_enabled", True))
        self.hp_tp_threshold_pct = int(data.get("hp_tp_threshold_pct", 70))
        self.damage_tp_press_count = int(data.get("damage_tp_press_count", 6))
        self.damage_tp_press_interval_s = float(data.get("damage_tp_press_interval_s", 0.12))
        self.damage_tp_cooldown_s = float(data.get("damage_tp_cooldown_s", 8.0))
        self.ui_theme = str(data.get("ui_theme", "darkly"))

    def save(self) -> None:
        data = {
            "window_title": self.window_title,
            "window_hwnd": int(self.window_hwnd),
            "points": [p.as_dict() for p in self.points],
            "active_name": self.active_name,
            "routes": [r.as_dict() for r in self.routes],
            "active_route_name": self.active_route_name,
            "profiles": [p.as_dict() for p in self.profiles],
            "active_profile_name": str(self.active_profile_name),
            "radar_roi": self.radar_roi.as_dict(),
            "enemy_hsv": self.enemy_hsv.as_dict(),
            "detect_mode": self.detect_mode,
            "empty_radar_path": self.empty_radar_path,
            "empty_text_path": self.empty_text_path,
            "empty_text_roi": self.empty_text_roi.as_dict(),
            "empty_text_threshold": float(self.empty_text_threshold),
            "diff_min_changed_pixels": int(self.diff_min_changed_pixels),
            "diff_threshold": int(self.diff_threshold),
            "teleport_key": self.teleport_key,
            "key_hold_s": float(self.key_hold_s),
            "tp_on_enemy_enabled": bool(self.tp_on_enemy_enabled),
            "radar_detect_enabled": bool(self.radar_detect_enabled),
            "post_tp_action": self.post_tp_action,
            "post_tp_key": self.post_tp_key,
            "post_tp_delay_s": float(self.post_tp_delay_s),
            "auto_confirm_enabled": bool(self.auto_confirm_enabled),
            "auto_confirm_key": self.auto_confirm_key,
            "auto_confirm_interval_s": float(self.auto_confirm_interval_s),
            "focus_steal_enabled": bool(self.focus_steal_enabled),
            "tp_focus_steal_enabled": bool(self.tp_focus_steal_enabled),
            "farm_without_route": bool(self.farm_without_route),
            "detect_confirm_streak": int(self.detect_confirm_streak),
            "min_tp_cooldown_s": float(self.min_tp_cooldown_s),
            "empty_match_threshold": float(self.empty_match_threshold),
            # Keep legacy key for old builds, but also store explicit range.
            "teleport_wait_s": int(self.teleport_wait_min_s),
            "teleport_wait_min_s": int(self.teleport_wait_min_s),
            "teleport_wait_max_s": int(self.teleport_wait_max_s),
            "check_interval_s": float(self.check_interval_s),
            "loop_jitter_s": float(self.loop_jitter_s),
            "move_time_min_s": float(self.move_time_min_s),
            "move_time_max_s": float(self.move_time_max_s),
            "route_delay_min_s": float(self.route_delay_min_s),
            "route_delay_max_s": float(self.route_delay_max_s),
            "auto_buy_potions_enabled": bool(self.auto_buy_potions_enabled),
            "auto_buy_potions_threshold": int(self.auto_buy_potions_threshold),
            "auto_buy_potions_roi": self.auto_buy_potions_roi.as_dict(),
            "auto_buy_potions_route_name": self.auto_buy_potions_route_name,
            "auto_buy_city_wait_s": float(self.auto_buy_city_wait_s),
            "auto_buy_check_interval_s": float(self.auto_buy_check_interval_s),
            "auto_buy_cooldown_s": float(self.auto_buy_cooldown_s),
            "auto_buy_route_mode_only": bool(self.auto_buy_route_mode_only),
            "auto_buy_return_to_farm": bool(self.auto_buy_return_to_farm),
            "enemy_alert_enabled": bool(self.enemy_alert_enabled),
            "enemy_alert_beeps": int(self.enemy_alert_beeps),
            "enemy_alert_interval_s": float(self.enemy_alert_interval_s),
            "enemy_alert_sound_path": str(self.enemy_alert_sound_path),
            "log_to_file_enabled": bool(self.log_to_file_enabled),
            "log_dir": str(self.log_dir),
            # Telegram
            "telegram_enabled": bool(self.telegram_enabled),
            "telegram_bot_token": str(self.telegram_bot_token),
            "telegram_chat_id": str(self.telegram_chat_id),
            "telegram_send_on_attacked": bool(self.telegram_send_on_attacked),
            "telegram_interval_s": float(self.telegram_interval_s),
            "pause_hotkey": str(self.pause_hotkey),
            "stop_record_hotkey": str(self.stop_record_hotkey),
            "menu_autoclose_enabled": bool(self.menu_autoclose_enabled),
            "menu_autoclose_key": str(self.menu_autoclose_key),
            "menu_autoclose_attempts": int(self.menu_autoclose_attempts),
            "menu_autoclose_roi": self.menu_autoclose_roi.as_dict(),
            "menu_autoclose_tpl_path": str(self.menu_autoclose_tpl_path),
            "menu_autoclose_threshold": float(self.menu_autoclose_threshold),
            "menu_autoclose_cooldown_s": float(self.menu_autoclose_cooldown_s),
            "confirm_popup_roi": self.confirm_popup_roi.as_dict(),
            "confirm_popup_tpl_path": str(self.confirm_popup_tpl_path),
            "confirm_popup_threshold": float(self.confirm_popup_threshold),
            "gate_roi": self.gate_roi.as_dict(),
            "gate_tpl_path": str(self.gate_tpl_path),
            "gate_threshold": float(self.gate_threshold),
            "gate_seek_timeout_s": float(self.gate_seek_timeout_s),
            "gate_center_margin_px": int(self.gate_center_margin_px),
            "gate_turn_step_px": int(self.gate_turn_step_px),
            "death_detect_enabled": bool(self.death_detect_enabled),
            "death_roi": self.death_roi.as_dict(),
            "death_tpl_path": str(self.death_tpl_path),
            "death_threshold": float(self.death_threshold),
            "death_route_name": self.death_route_name,
            "death_cooldown_s": float(self.death_cooldown_s),
            "schedule_enabled": bool(self.schedule_enabled),
            "schedule_start_hhmm": str(self.schedule_start_hhmm),
            "schedule_duration_h": float(self.schedule_duration_h),
            "schedule_mode": str(self.schedule_mode),
            "schedule_daily": bool(self.schedule_daily),
            "schedule_last_start_date": str(self.schedule_last_start_date),
            "damage_tp_enabled": bool(self.damage_tp_enabled),
            "damage_icon_roi": self.damage_icon_roi.as_dict(),
            "damage_icon_normal_roi": self.damage_icon_normal_roi.as_dict(),
            "damage_icon_tpl_path": str(self.damage_icon_tpl_path),
            "damage_icon_threshold": float(self.damage_icon_threshold),
            "damage_icon_normal_tpl_path": str(self.damage_icon_normal_tpl_path),
            "damage_icon_normal_threshold": float(self.damage_icon_normal_threshold),
            "damage_icon_margin": float(self.damage_icon_margin),
            "hp_bar_roi": self.hp_bar_roi.as_dict(),
            "hp_tp_enabled": bool(self.hp_tp_enabled),
            "hp_tp_threshold_pct": int(self.hp_tp_threshold_pct),
            "damage_tp_press_count": int(self.damage_tp_press_count),
            "damage_tp_press_interval_s": float(self.damage_tp_press_interval_s),
            "damage_tp_cooldown_s": float(self.damage_tp_cooldown_s),
            "ui_theme": str(self.ui_theme),
        }
        self.path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    # --- Profiles API ---
    def list_profiles(self) -> list[str]:
        return [p.name for p in self.profiles]

    def get_active_profile(self) -> Profile:
        for p in self.profiles:
            if p.name == self.active_profile_name:
                return p
        # fallback: ensure at least one
        if not self.profiles:
            self.profiles = [Profile(name="default", routes=list(self.routes), farm_route_name=self.active_route_name)]
            self.active_profile_name = "default"
        return self.profiles[0]

    def set_active_profile(self, name: str) -> None:
        if not name:
            return
        if name not in self.list_profiles():
            return
        self.active_profile_name = name
        self.save()

    def add_profile(self, name: str) -> None:
        n = (name or "").strip()
        if not n:
            return
        if n in self.list_profiles():
            return
        self.profiles.append(Profile(name=n, routes=[]))
        self.active_profile_name = n
        self.save()

    def delete_profile(self, name: str) -> None:
        if not name:
            return
        self.profiles = [p for p in self.profiles if p.name != name]
        if self.active_profile_name == name:
            self.active_profile_name = self.profiles[0].name if self.profiles else "default"
        if not self.profiles:
            self.profiles = [Profile(name="default", routes=list(self.routes), farm_route_name=self.active_route_name)]
        self.save()

    def add_point(self, p: Point) -> None:
        # overwrite by name
        self.points = [x for x in self.points if x.name != p.name]
        self.points.append(p)
        if not self.active_name:
            self.active_name = p.name
        self.save()

    def add_route(self, r: Route) -> None:
        p = self.get_active_profile()
        p.routes = [x for x in p.routes if x.name != r.name]
        p.routes.append(r)
        if not p.farm_route_name:
            p.farm_route_name = r.name
        # keep legacy mirror for compatibility
        self.routes = list(p.routes)
        self.active_route_name = p.farm_route_name
        self.save()

    def delete_route(self, name: str) -> None:
        p = self.get_active_profile()
        p.routes = [x for x in p.routes if x.name != name]
        if p.farm_route_name == name:
            p.farm_route_name = p.routes[0].name if p.routes else None
        if p.setup_route_name == name:
            p.setup_route_name = None
        self.routes = list(p.routes)
        self.active_route_name = p.farm_route_name
        self.save()

    def set_active_route(self, name: Optional[str]) -> None:
        p = self.get_active_profile()
        p.farm_route_name = name
        self.active_route_name = name
        self.save()

    def get_farm_route(self) -> Optional[Route]:
        p = self.get_active_profile()
        name = p.farm_route_name
        if not name:
            return None
        for r in p.routes:
            if r.name == name:
                return r
        return None

    def get_setup_route(self) -> Optional[Route]:
        p = self.get_active_profile()
        name = p.setup_route_name
        if not name:
            return None
        for r in p.routes:
            if r.name == name:
                return r
        return None

    # Backward-compatible alias
    def get_active_route(self) -> Optional[Route]:
        return self.get_farm_route()

    def delete_point(self, name: str) -> None:
        self.points = [x for x in self.points if x.name != name]
        if self.active_name == name:
            self.active_name = self.points[0].name if self.points else None
        self.save()

    def set_active(self, name: Optional[str]) -> None:
        self.active_name = name
        self.save()

    def get_active(self) -> Optional[Point]:
        if not self.active_name:
            return None
        for p in self.points:
            if p.name == self.active_name:
                return p
        return None


LogFn = Callable[[str], None]


class Bot:
    def _tp_wait_s(self) -> int:
        mn = int(getattr(self.store, "teleport_wait_min_s", getattr(self.store, "teleport_wait_s", 60)) or 60)
        mx = int(getattr(self.store, "teleport_wait_max_s", getattr(self.store, "teleport_wait_s", 60)) or 60)
        mn = max(0, mn)
        mx = max(0, mx)
        if mx < mn:
            mn, mx = mx, mn
        if mx == mn:
            return int(mn)
        return int(random.randint(int(mn), int(mx)))
    def __init__(self, store: PointsStore, log: LogFn):
        self.store = store
        self.log = log

        # Human-friendly runtime status (UI mini window reads this).
        self.current_action: str = "Ожидание"

        # Mini UI live "detect" indicators (best-effort).
        self.last_radar_enemy: bool = False
        self.last_radar_score_label: str = ""
        self.last_text_match_score: float | None = None
        self.last_text_cap_mode: str = ""
        self.last_attacked: bool = False
        self.last_hp_pct: int | None = None

        self._stop = threading.Event()
        self._pause = threading.Event()
        self._thread: Optional[threading.Thread] = None

        self._vision = Vision()
        self._input = InputController(
            min_move_time=self.store.move_time_min_s,
            max_move_time=self.store.move_time_max_s,
        )

        self.window_title: str = ""

        self._in_base = False
        self._armed = False  # becomes True after first "go farm" route execution
        self._last_tp_block_reason: str | None = None
        self._last_score_log_ts: float = 0.0
        self._last_auto_confirm_ts: float = 0.0
        self._enemy_streak: int = 0
        self._last_tp_ts: float = 0.0
        self._last_auto_buy_check_ts: float = 0.0
        self._last_auto_buy_ts: float = 0.0
        self._tp_debug_dir = Path("tp_debug")
        self._tp_debug_dir.mkdir(parents=True, exist_ok=True)
        self._last_focus_warn_ts: float = 0.0
        self._last_enemy_alert_ts: float = 0.0
        self._last_menu_close_ts: float = 0.0
        self._last_death_ts: float = 0.0
        self._last_damage_tp_ts: float = 0.0
        self._last_attacked_alert_ts: float = 0.0
        self._last_tg_attacked_ts: float = 0.0
        self._last_damage_debug_ts: float = 0.0
        self._last_damage_missing_tpl_ts: float = 0.0
        self._last_damage_err_ts: float = 0.0

    def _send_telegram_photo(self, *, caption: str, png_bytes: bytes) -> bool:
        if not bool(getattr(self.store, "telegram_enabled", False)):
            return False
        token = str(getattr(self.store, "telegram_bot_token", "") or "").strip()
        chat_id = str(getattr(self.store, "telegram_chat_id", "") or "").strip()
        if not token or not chat_id:
            return False

        url = f"https://api.telegram.org/bot{token}/sendPhoto"
        cap = str(caption or "").strip()[:900]
        try:
            # Prefer requests if available (simpler multipart).
            if requests is not None:
                files = {"photo": ("radar.png", png_bytes, "image/png")}
                data = {"chat_id": chat_id, "caption": cap}
                r = requests.post(url, data=data, files=files, timeout=10)
                return bool(getattr(r, "ok", False))

            # Fallback without external deps: build multipart manually.
            import uuid
            import urllib.request

            boundary = "----RAVENBOT" + uuid.uuid4().hex
            parts: list[bytes] = []

            def add_field(name: str, value: str) -> None:
                parts.append(f"--{boundary}\r\n".encode("utf-8"))
                parts.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"))
                parts.append((value or "").encode("utf-8"))
                parts.append(b"\r\n")

            def add_file(name: str, filename: str, content_type: str, content: bytes) -> None:
                parts.append(f"--{boundary}\r\n".encode("utf-8"))
                parts.append(
                    f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'.encode("utf-8")
                )
                parts.append(f"Content-Type: {content_type}\r\n\r\n".encode("utf-8"))
                parts.append(content)
                parts.append(b"\r\n")

            add_field("chat_id", chat_id)
            if cap:
                add_field("caption", cap)
            add_file("photo", "radar.png", "image/png", png_bytes)
            parts.append(f"--{boundary}--\r\n".encode("utf-8"))

            body = b"".join(parts)
            req = urllib.request.Request(url, data=body, method="POST")
            req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
            req.add_header("Content-Length", str(len(body)))
            with urllib.request.urlopen(req, timeout=10) as resp:
                return int(getattr(resp, "status", 0) or 0) == 200
        except Exception:
            return False

    def send_telegram_test_radar(self, hwnd: int) -> bool:
        """
        Capture current radar ROI and send to Telegram (manual test button).
        """
        try:
            rect = get_window_rect(hwnd)
        except Exception:
            return False
        # Prefer Text ROI ("Нет Цель поиска") because it's the most meaningful for the user.
        roi = getattr(self.store, "empty_text_roi", None)
        if roi is None or int(getattr(roi, "w", 1)) <= 5 or int(getattr(roi, "h", 1)) <= 5:
            roi = getattr(self.store, "radar_roi", None)
        try:
            radar = self._vision.grab_radar_bgr(rect, roi)
        except Exception:
            try:
                radar = self._vision.grab_client_roi_bgr(hwnd, roi)
            except Exception:
                radar = None
        if radar is None:
            return False
        try:
            ok, buf = cv2.imencode(".png", radar)
            if not ok:
                return False
            caption = "RAVEN BOT: ТЕСТ — скрин Text ROI"
            return bool(self._send_telegram_photo(caption=caption, png_bytes=bytes(buf)))
        except Exception:
            return False

    def _set_action(self, text: str) -> None:
        try:
            self.current_action = str(text or "").strip() or "Ожидание"
        except Exception:
            self.current_action = "Ожидание"

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def is_paused(self) -> bool:
        return bool(self._pause.is_set())

    def toggle_pause(self) -> bool:
        if self._pause.is_set():
            self._pause.clear()
            self.log("Пауза: ВЫКЛ (продолжаю)")
            return False
        self._pause.set()
        self.log("Пауза: ВКЛ")
        return True

    def _pause_wait(self) -> None:
        while (not self._stop.is_set()) and self._pause.is_set():
            time.sleep(0.05)

    def _is_game_foreground(self, hwnd: int) -> bool:
        try:
            return int(win32gui.GetForegroundWindow()) == int(hwnd)
        except Exception:
            return False

    def _maybe_focus_game(self, hwnd: int) -> bool:
        """
        Returns True if game is in foreground (either already, or focused by us).
        If focus stealing is disabled, we never bring game to foreground.
        """
        if self._is_game_foreground(hwnd):
            return True
        if not bool(getattr(self.store, "focus_steal_enabled", True)):
            return False
        try:
            bring_window_to_foreground(hwnd)
        except Exception:
            pass
        return self._is_game_foreground(hwnd)

    def _click_to_focus(self, hwnd: int) -> None:
        """
        Fallback when SetForegroundWindow is blocked by Windows: do a tiny click inside client area.
        We try to keep it minimally intrusive and restore mouse position afterwards.
        """
        try:
            (cx, cy) = win32gui.ClientToScreen(int(hwnd), (8, 8))
            old_x, old_y = win32api.GetCursorPos()
            try:
                self._input.click_abs(int(cx), int(cy), button="left")
            finally:
                try:
                    win32api.SetCursorPos((int(old_x), int(old_y)))
                except Exception:
                    pass
        except Exception:
            pass

    def _ensure_game_foreground_strict(self, hwnd: int, *, allow_steal: bool, why: str) -> bool:
        """
        Strict foreground check before actions.
        - If allow_steal is False: never focuses, just returns current state.
        - If allow_steal is True: tries normal focus, then click-to-focus, then re-check.
        """
        if self._is_game_foreground(hwnd):
            return True
        if not allow_steal:
            # avoid log spam
            now = time.time()
            if now - float(self._last_focus_warn_ts) >= 3.0:
                self._last_focus_warn_ts = now
                self.log(f"Окно игры НЕ foreground (фокус выключен). Пропуск действия: {why}")
            return False

        try:
            bring_window_to_foreground(hwnd)
        except Exception:
            pass
        if self._is_game_foreground(hwnd):
            return True

        # Windows sometimes blocks foreground switch: click inside client area.
        self._click_to_focus(hwnd)
        return self._is_game_foreground(hwnd)

    def _tp_confirm_roi(self, hwnd: int) -> RadarROI:
        """
        ROI for confirming that something actually changed on screen after TP key press.
        We use a small center region of the client area to detect 'real' frame changes.
        """
        try:
            cl, ct, cr, cb = win32gui.GetClientRect(int(hwnd))
            w = max(1, int(cr - cl))
            h = max(1, int(cb - ct))
        except Exception:
            w, h = 800, 600
        rw = max(80, int(w * 0.18))
        rh = max(60, int(h * 0.14))
        x = max(0, int((w - rw) // 2))
        y = max(0, int((h - rh) // 2))
        return RadarROI(x=x, y=y, w=rw, h=rh)

    def _frame_change_score(self, a_bgr: np.ndarray, b_bgr: np.ndarray) -> float:
        """
        Returns mean absolute difference in grayscale [0..255].
        Higher => more change between frames.
        """
        try:
            if a_bgr is None or b_bgr is None:
                return 0.0
            if a_bgr.shape[:2] != b_bgr.shape[:2]:
                b_bgr = cv2.resize(b_bgr, (a_bgr.shape[1], a_bgr.shape[0]))
            ga = cv2.cvtColor(a_bgr, cv2.COLOR_BGR2GRAY)
            gb = cv2.cvtColor(b_bgr, cv2.COLOR_BGR2GRAY)
            diff = cv2.absdiff(ga, gb)
            return float(np.mean(diff))
        except Exception:
            return 0.0

    def _tp_debug_dump(
        self,
        *,
        prefix: str,
        hwnd: int,
        tp_key: str,
        attempt: int,
        fg_before: bool,
        fg_after: bool,
        change_score: float,
        change_thr: float,
        roi_bgr_before: np.ndarray | None,
        roi_bgr_after: np.ndarray | None,
        text_roi_bgr: np.ndarray | None,
        text_score: float | None,
        text_thr: float | None,
    ) -> None:
        ts = time.strftime("%Y%m%d_%H%M%S")
        tag = f"{prefix}_{ts}_a{attempt}"
        try:
            if roi_bgr_before is not None:
                cv2.imwrite(str(self._tp_debug_dir / f"{tag}_confirm_before.png"), roi_bgr_before)
            if roi_bgr_after is not None:
                cv2.imwrite(str(self._tp_debug_dir / f"{tag}_confirm_after.png"), roi_bgr_after)
            if text_roi_bgr is not None:
                cv2.imwrite(str(self._tp_debug_dir / f"{tag}_text_roi.png"), text_roi_bgr)
        except Exception:
            pass

        title = ""
        try:
            title = get_window_title(hwnd)
        except Exception:
            pass

        self.log(
            "ТП FAIL DEBUG: "
            f"title={title!r}, key={tp_key.upper()}, attempt={attempt}, "
            f"fg {int(fg_before)}->{int(fg_after)}, "
            f"change={change_score:.2f} thr={change_thr:.2f}, "
            + (f"text_score={text_score:.3f} thr={text_thr:.3f}" if text_score is not None and text_thr is not None else "text_score=n/a")
            + f" (скрины: {self._tp_debug_dir})"
        )

    def _teleport_with_retries(self, hwnd: int, rect, *, score_label: str) -> bool:
        tp_key = (self.store.teleport_key or "f4").strip().lower()
        hold_s = float(getattr(self.store, "key_hold_s", 0.06))
        attempts = 3
        # "Change" threshold: tuned to be tolerant of minor UI flicker but detect real transitions.
        change_thr = 6.0

        confirm_roi = self._tp_confirm_roi(hwnd)

        # Optional: also snapshot the "empty text" ROI used by detection (gives hints about UI state).
        text_roi = getattr(self.store, "empty_text_roi", RadarROI(x=0, y=0, w=1, h=1))
        text_tpl = None
        text_thr = None
        try:
            text_path = Path(self.store.empty_text_path)
            text_tpl = cv2.imread(str(text_path), cv2.IMREAD_COLOR) if text_path.exists() else None
            text_thr = float(getattr(self.store, "empty_text_threshold", 0.86))
        except Exception:
            text_tpl = None

        for attempt in range(1, attempts + 1):
            fg_before = self._is_game_foreground(hwnd)

            if not self._ensure_game_foreground_strict(
                hwnd,
                allow_steal=bool(getattr(self.store, "tp_focus_steal_enabled", True)),
                why=f"ТП ({tp_key.upper()})",
            ):
                self.log(
                    "ТП пропущен: игра не в фокусе "
                    f"(Фокус для ТП={'ВКЛ' if bool(getattr(self.store, 'tp_focus_steal_enabled', True)) else 'ВЫКЛ'})."
                )
                return False

            # take "before" snapshots
            roi_before = None
            text_cur = None
            text_score = None
            try:
                roi_before = self._vision.grab_client_roi_bgr(hwnd, confirm_roi)
            except Exception:
                roi_before = None
            try:
                if int(getattr(text_roi, "w", 1)) > 5 and int(getattr(text_roi, "h", 1)) > 5:
                    # For legacy text detection, ROI is relative to window rect; but we want client-based.
                    # Many users configured it from client screenshots, so use client grab.
                    text_cur = self._vision.grab_client_roi_bgr(hwnd, text_roi)
                    if text_tpl is not None and text_cur is not None:
                        text_score = self._vision.text_match_score(cur_bgr=text_cur, tpl_bgr=text_tpl)
            except Exception:
                text_cur = None
                text_score = None

            self.log(f"Телепорт ({tp_key.upper()}) попытка {attempt}/{attempts} [{score_label}]")
            self._input.press_key_hold(tp_key, hold_s=hold_s)
            self._last_tp_ts = time.time()

            # Wait a bit for animation/loading/UI change
            self._sleep_interruptible(0.45 + random.uniform(0.0, 0.18))

            fg_after = self._is_game_foreground(hwnd)
            roi_after = None
            try:
                roi_after = self._vision.grab_client_roi_bgr(hwnd, confirm_roi)
            except Exception:
                roi_after = None

            change = self._frame_change_score(roi_before, roi_after)

            # Confirmation logic:
            # - primary: frame change in center ROI
            # - secondary: if text_score was "empty" before, after TP it often changes (UI clears/loads)
            confirmed = (change >= change_thr)
            if (not confirmed) and (text_score is not None) and (text_thr is not None):
                # If the "empty" text is stable and still looks identical, TP likely didn't happen.
                # This doesn't guarantee failure, but helps confirm success when it DOES change.
                confirmed = False

            if confirmed:
                self.log(f"ТП подтверждён: change={change:.2f} (thr={change_thr:.2f}), fg={int(fg_before)}->{int(fg_after)}")
                return True

            # Not confirmed: log + maybe retry
            self.log(f"ТП НЕ подтверждён: change={change:.2f} (thr={change_thr:.2f}), попытка {attempt}/{attempts}")
            if attempt == attempts:
                self._tp_debug_dump(
                    prefix="tp_fail",
                    hwnd=hwnd,
                    tp_key=tp_key,
                    attempt=attempt,
                    fg_before=fg_before,
                    fg_after=fg_after,
                    change_score=change,
                    change_thr=change_thr,
                    roi_bgr_before=roi_before,
                    roi_bgr_after=roi_after,
                    text_roi_bgr=text_cur,
                    text_score=text_score,
                    text_thr=text_thr,
                )
                return False

            self._sleep_interruptible(0.25 + random.uniform(0.0, 0.18))

        return False

    def _menu_open_score(self, hwnd: int) -> float | None:
        if not bool(getattr(self.store, "menu_autoclose_enabled", False)):
            return None
        roi = getattr(self.store, "menu_autoclose_roi", RadarROI(x=0, y=0, w=1, h=1))
        if int(getattr(roi, "w", 1)) <= 5 or int(getattr(roi, "h", 1)) <= 5:
            return None
        tpl_path = Path(str(getattr(self.store, "menu_autoclose_tpl_path", "menu_open_tpl.png") or "menu_open_tpl.png"))
        if not tpl_path.exists():
            return None
        tpl = cv2.imread(str(tpl_path), cv2.IMREAD_COLOR)
        if tpl is None:
            return None
        try:
            cur = self._vision.grab_client_roi_bgr(hwnd, roi)
            return float(self._vision.text_match_score(cur_bgr=cur, tpl_bgr=tpl))
        except Exception:
            return None

    def _maybe_close_menu(self, hwnd: int) -> None:
        if not bool(getattr(self.store, "menu_autoclose_enabled", False)):
            return
        now = time.time()
        cooldown = max(0.2, float(getattr(self.store, "menu_autoclose_cooldown_s", 1.0)))
        if now - float(self._last_menu_close_ts) < cooldown:
            return

        score = self._menu_open_score(hwnd)
        thr = float(getattr(self.store, "menu_autoclose_threshold", 0.86))
        if score is None or score < thr:
            return

        attempts = max(1, min(5, int(getattr(self.store, "menu_autoclose_attempts", 2) or 2)))
        key = str(getattr(self.store, "menu_autoclose_key", "esc") or "esc").strip().lower()
        self._last_menu_close_ts = now
        self.log(f"Меню/чат открыт (score={score:.3f}>=thr={thr:.3f}). Закрываю: {key.upper()} x{attempts}")

        for _ in range(attempts):
            if self._stop.is_set():
                break
            self._pause_wait()
            # Ensure focus to the game for ESC
            self._ensure_game_foreground_strict(hwnd, allow_steal=bool(getattr(self.store, "focus_steal_enabled", True)), why="menu_close")
            if key:
                self._input.press_key_hold(key, hold_s=float(getattr(self.store, "key_hold_s", 0.06)))
            # click-to-focus to exit chat input etc.
            self._click_to_focus(hwnd)
            self._sleep_interruptible(0.12)

            score2 = self._menu_open_score(hwnd)
            if score2 is None or score2 < thr:
                break

    def capture_damage_icon_template(self) -> Path:
        hwnd = self._get_game_hwnd()
        roi = getattr(self.store, "damage_icon_roi", RadarROI(x=0, y=0, w=1, h=1))
        img = self._vision.grab_client_roi_bgr(hwnd, roi)
        path = Path(str(getattr(self.store, "damage_icon_tpl_path", "damage_icon_tpl.png") or "damage_icon_tpl.png"))
        cv2.imwrite(str(path), img)
        self.log(f"Шаблон иконки атаки сохранён: {path} (roi={roi.x},{roi.y},{roi.w},{roi.h})")
        return path

    def capture_damage_icon_normal_template(self) -> Path:
        hwnd = self._get_game_hwnd()
        roi = getattr(self.store, "damage_icon_normal_roi", getattr(self.store, "damage_icon_roi", RadarROI(x=0, y=0, w=1, h=1)))
        img = self._vision.grab_client_roi_bgr(hwnd, roi)
        path = Path(
            str(getattr(self.store, "damage_icon_normal_tpl_path", "damage_icon_normal_tpl.png") or "damage_icon_normal_tpl.png")
        )
        cv2.imwrite(str(path), img)
        self.log(f"Шаблон корпуса сохранён: {path} (roi={roi.x},{roi.y},{roi.w},{roi.h})")
        return path

    def _damage_icon_score(self, hwnd: int) -> float | None:
        # Kept for backward-compat; prefer _damage_icon_scores()
        scores = self._damage_icon_scores(hwnd)
        return scores[0] if scores is not None else None

    def _damage_icon_scores(self, hwnd: int) -> tuple[float | None, float | None] | None:
        """
        Returns (attacked_score, normal_score).
        Either score can be None if template missing or ROI invalid.
        """
        if not bool(getattr(self.store, "damage_tp_enabled", False)):
            return None
        roi_atk = getattr(self.store, "damage_icon_roi", RadarROI(x=0, y=0, w=1, h=1))
        roi_norm = getattr(
            self.store,
            "damage_icon_normal_roi",
            getattr(self.store, "damage_icon_roi", RadarROI(x=0, y=0, w=1, h=1)),
        )
        if int(getattr(roi_atk, "w", 1)) <= 5 or int(getattr(roi_atk, "h", 1)) <= 5:
            return (None, None)
        if int(getattr(roi_norm, "w", 1)) <= 5 or int(getattr(roi_norm, "h", 1)) <= 5:
            return (None, None)

        cur_atk = None
        cur_norm = None
        try:
            cur_atk = self._vision.grab_client_roi_bgr(hwnd, roi_atk)
        except Exception:
            cur_atk = None
        try:
            cur_norm = self._vision.grab_client_roi_bgr(hwnd, roi_norm)
        except Exception:
            cur_norm = None

        atk_score: float | None = None
        norm_score: float | None = None

        atk_path = Path(str(getattr(self.store, "damage_icon_tpl_path", "damage_icon_tpl.png") or "damage_icon_tpl.png"))
        if atk_path.exists():
            tpl = cv2.imread(str(atk_path), cv2.IMREAD_COLOR)
            if tpl is not None and cur_atk is not None:
                try:
                    atk_score = float(self._vision.icon_match_score(cur_bgr=cur_atk, tpl_bgr=tpl))
                except Exception:
                    atk_score = None

        norm_path = Path(
            str(getattr(self.store, "damage_icon_normal_tpl_path", "damage_icon_normal_tpl.png") or "damage_icon_normal_tpl.png")
        )
        if norm_path.exists():
            tpl2 = cv2.imread(str(norm_path), cv2.IMREAD_COLOR)
            if tpl2 is not None and cur_norm is not None:
                try:
                    norm_score = float(self._vision.icon_match_score(cur_bgr=cur_norm, tpl_bgr=tpl2))
                except Exception:
                    norm_score = None

        return (atk_score, norm_score)

    def _is_attacked_by_icon(self, hwnd: int) -> tuple[bool, float | None, float | None]:
        """
        Decide attacked state using dual-template logic.
        Returns (attacked, attacked_score, normal_score).
        """
        scores = self._damage_icon_scores(hwnd)
        if scores is None:
            return (False, None, None)
        atk, norm = scores
        atk_thr = float(getattr(self.store, "damage_icon_threshold", 0.86))
        norm_thr = float(getattr(self.store, "damage_icon_normal_threshold", 0.86))
        margin = max(0.0, float(getattr(self.store, "damage_icon_margin", 0.04)))

        # Primary: attacked template passes threshold.
        if atk is not None and atk >= atk_thr:
            return (True, atk, norm)

        # If we have both scores, compare which one matches better.
        if atk is not None and norm is not None:
            if (atk - norm) >= margin and atk >= max(0.55, atk_thr - 0.25):
                return (True, atk, norm)
            if (norm - atk) >= margin and norm >= max(0.55, norm_thr - 0.25):
                return (False, atk, norm)
            # Grey zone: pick the higher score if it's reasonably strong.
            if atk > norm and atk >= 0.70:
                return (True, atk, norm)
            return (False, atk, norm)

        # Fallback: only attacked template exists
        if atk is not None:
            return (bool(atk >= max(0.70, atk_thr - 0.20)), atk, norm)

        return (False, atk, norm)

    def _hp_percent(self, hwnd: int) -> int | None:
        roi = getattr(self.store, "hp_bar_roi", RadarROI(x=0, y=0, w=1, h=1))
        if int(getattr(roi, "w", 1)) <= 5 or int(getattr(roi, "h", 1)) <= 5:
            return None
        try:
            cur = self._vision.grab_client_roi_bgr(hwnd, roi)
            return self._vision.hp_percent_from_bar(cur)
        except Exception:
            return None

    def _teleport_spam(self, hwnd: int, rect, *, reason: str) -> bool:
        """
        Press TP key multiple times; stop early if frame-change confirmation succeeds.
        """
        now = time.time()
        cooldown = max(0.5, float(getattr(self.store, "damage_tp_cooldown_s", 8.0)))
        if now - float(self._last_damage_tp_ts) < cooldown:
            return False

        tp_key = (self.store.teleport_key or "f4").strip().lower()
        cnt = max(1, min(30, int(getattr(self.store, "damage_tp_press_count", 6) or 6)))
        interval = max(0.05, float(getattr(self.store, "damage_tp_press_interval_s", 0.12)))

        self._last_damage_tp_ts = now
        self.log(f"ТП по урону: {reason}. Жму Телепорт ({tp_key.upper()}) x{cnt}")
        self._set_action(f"ТП по урону: телепорт ({tp_key.upper()})")

        if not self._maybe_focus_game_for_tp(hwnd):
            # Important: failure here can be due to toggle OFF OR OS/game refusing focus.
            if not bool(getattr(self.store, "tp_focus_steal_enabled", True)):
                self.log("ТП по урону: игра не в фокусе (Фокус для ТП выключен).")
            else:
                try:
                    fg = win32gui.GetForegroundWindow()
                except Exception:
                    fg = 0
                self.log(f"ТП по урону: игра не в фокусе (не удалось сфокусировать, fg=0x{int(fg):X}).")
            return False

        for i in range(cnt):
            if self._stop.is_set():
                break
            self._pause_wait()
            try:
                self._maybe_close_menu(hwnd)
            except Exception:
                pass
            # Use the existing confirm logic per press
            ok = self._teleport_with_retries(hwnd, rect, score_label=f"damage_tp press {i+1}/{cnt}")
            if ok:
                self._set_action("ТП по урону: телепорт подтверждён")
                return True
            time.sleep(interval)
        self._set_action("ТП по урону: телепорт не подтверждён")
        return False

    def _maybe_enemy_alert(self, *, score_label: str) -> None:
        """
        Play a short sound when enemy detection is confirmed (streak reached).
        Anti-spam via enemy_alert_interval_s.
        """
        if not bool(getattr(self.store, "enemy_alert_enabled", False)):
            return
        now = time.time()
        interval = max(0.5, float(getattr(self.store, "enemy_alert_interval_s", 8.0)))
        if now - float(self._last_enemy_alert_ts) < interval:
            return
        self._last_enemy_alert_ts = now

        beeps = int(getattr(self.store, "enemy_alert_beeps", 2) or 2)
        beeps = max(1, min(10, beeps))

        # If user provided custom wav sound, play it instead of beeps.
        wav_path = str(getattr(self.store, "enemy_alert_sound_path", "") or "").strip()
        if wav_path and os.path.isfile(wav_path) and wav_path.lower().endswith(".wav"):
            for _ in range(beeps):
                try:
                    winsound.PlaySound(wav_path, winsound.SND_FILENAME | winsound.SND_ASYNC)
                except Exception:
                    break
                time.sleep(0.12)
        else:
            # Keep it subtle but noticeable.
            for _ in range(beeps):
                try:
                    winsound.Beep(1100, 120)  # freq Hz, duration ms
                except Exception:
                    try:
                        winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
                    except Exception:
                        break
                time.sleep(0.06)
        # Optional log line (not spammy due to interval)
        self.log(f"Сигнал: враг подтверждён ({score_label})")

    def _maybe_attacked_alert(
        self,
        *,
        hwnd: int,
        rect,
        atk_score: float | None,
        norm_score: float | None,
        atk_thr: float,
        norm_thr: float,
        hp_pct: int | None,
    ) -> None:
        """
        Log when 'attacked' icon is detected (damage mode).
        Sound is optional and reuses enemy_alert_* settings.
        """
        now = time.time()
        interval = max(0.5, float(getattr(self.store, "enemy_alert_interval_s", 8.0)))
        if now - float(self._last_attacked_alert_ts) < interval:
            return
        self._last_attacked_alert_ts = now

        hp_part = f", hp={hp_pct}%" if hp_pct is not None else ""
        a = "None" if atk_score is None else f"{atk_score:.3f}"
        n = "None" if norm_score is None else f"{norm_score:.3f}"
        self.log(f"Вас атакуют (atk={a} thr={atk_thr:.3f}, norm={n} thr={norm_thr:.3f}{hp_part})")
        # Use same sound as enemy alert if enabled
        if bool(getattr(self.store, "enemy_alert_enabled", False)):
            try:
                self._maybe_enemy_alert(score_label="attacked_icon")
            except Exception:
                pass

        # Telegram screenshot (radar) on attacked
        try:
            if bool(getattr(self.store, "telegram_enabled", False)) and bool(getattr(self.store, "telegram_send_on_attacked", True)):
                now2 = time.time()
                interval2 = max(5.0, float(getattr(self.store, "telegram_interval_s", 30.0)))
                if now2 - float(self._last_tg_attacked_ts) >= interval2:
                    self._last_tg_attacked_ts = now2
                    # Prefer Text ROI ("Нет Цель поиска") so screenshot is always meaningful.
                    radar = None
                    roi2 = getattr(self.store, "empty_text_roi", None)
                    if roi2 is None or int(getattr(roi2, "w", 1)) <= 5 or int(getattr(roi2, "h", 1)) <= 5:
                        roi2 = getattr(self.store, "radar_roi", None)
                    try:
                        radar = self._vision.grab_radar_bgr(rect, roi2)
                    except Exception:
                        try:
                            radar = self._vision.grab_client_roi_bgr(hwnd, roi2)
                        except Exception:
                            radar = None
                    if radar is not None:
                        ok, buf = cv2.imencode(".png", radar)
                        if ok:
                            hp_s = "?" if hp_pct is None else f"{int(hp_pct)}%"
                            caption = f"RAVEN BOT: Атакуют=ДА | HP={hp_s}"
                            sent = self._send_telegram_photo(caption=caption, png_bytes=bytes(buf))
                            self.log("Telegram: скрин радара отправлен" if sent else "Telegram: не удалось отправить скрин")
        except Exception:
            pass

    def _maybe_focus_game_for_tp(self, hwnd: int) -> bool:
        """
        For teleport only: allow focusing game even if focus_steal_enabled is off,
        controlled by tp_focus_steal_enabled.
        """
        if self._is_game_foreground(hwnd):
            return True
        if not bool(getattr(self.store, "tp_focus_steal_enabled", True)):
            return False
        # Best-effort strict focus for TP: foreground + click-to-focus fallback.
        # This is intentionally more aggressive than _maybe_focus_game().
        for _ in range(3):
            try:
                bring_window_to_foreground(hwnd)
            except Exception:
                pass
            if self._is_game_foreground(hwnd):
                return True
            # Fallback if SetForegroundWindow is blocked.
            self._click_to_focus(hwnd)
            if self._is_game_foreground(hwnd):
                return True
            time.sleep(0.05)
        return False

    def start(self, window_title: str) -> None:
        if self.is_running():
            return
        self.window_title = window_title
        self._stop.clear()
        self._pause.clear()
        self._thread = threading.Thread(target=self._run, name="BotThread", daemon=True)
        self._thread.start()

    def _get_game_hwnd(self) -> int:
        if int(self.store.window_hwnd) != 0:
            return int(self.store.window_hwnd)
        # fallback legacy: exact title
        return find_window_by_title(self.window_title)

    def stop(self) -> None:
        self._stop.set()
        self._pause.clear()
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=2.0)
        self._thread = None

    def set_tp_on_enemy_enabled(self, enabled: bool) -> None:
        self.store.tp_on_enemy_enabled = bool(enabled)
        self.store.save()
        state = "ВКЛ" if self.store.tp_on_enemy_enabled else "ВЫКЛ"
        self.log(f"ТП по врагу: {state}")

    def toggle_tp_on_enemy(self) -> bool:
        cur = bool(getattr(self.store, "tp_on_enemy_enabled", True))
        self.set_tp_on_enemy_enabled(not cur)
        return bool(self.store.tp_on_enemy_enabled)

    def set_radar_detect_enabled(self, enabled: bool) -> None:
        self.store.radar_detect_enabled = bool(enabled)
        self.store.save()
        state = "ВКЛ" if self.store.radar_detect_enabled else "ВЫКЛ"
        self.log(f"Детект радара: {state}")

    def toggle_radar_detect(self) -> bool:
        cur = bool(getattr(self.store, "radar_detect_enabled", True))
        self.set_radar_detect_enabled(not cur)
        return bool(self.store.radar_detect_enabled)

    def set_tp_focus_steal_enabled(self, enabled: bool) -> None:
        self.store.tp_focus_steal_enabled = bool(enabled)
        self.store.save()
        state = "ВКЛ" if self.store.tp_focus_steal_enabled else "ВЫКЛ"
        self.log(f"Перехват фокуса для ТП: {state}")

    def record_point_from_mouse(self, name: str) -> Point:
        hwnd = self._get_game_hwnd()
        rect = get_window_rect(hwnd)

        abs_x, abs_y = win32api.GetCursorPos()
        rel_x = int(abs_x - rect.left)
        rel_y = int(abs_y - rect.top)
        rel_x, rel_y = clamp_rel_xy(rect, rel_x, rel_y)

        p = Point(name=name, rel_x=rel_x, rel_y=rel_y)
        self.store.add_point(p)
        self.log(f"Точка сохранена: {p.name} ({p.rel_x}, {p.rel_y})")
        return p

    def select_active_window(self) -> str:
        hwnd = get_foreground_window()
        title = get_window_title(hwnd).strip()
        if not title:
            raise WindowNotFoundError("У активного окна пустой заголовок.")
        self.window_title = title
        self.store.window_title = title
        self.store.window_hwnd = int(hwnd)
        self.store.save()
        self.log(f"Выбрано активное окно: {title!r}")
        return title

    def capture_empty_radar(self) -> Path:
        hwnd = self._get_game_hwnd()
        # Prefer client-area coordinates (matches ROI picked from client screenshots).
        # Fall back to window-rect capture for backward compatibility.
        try:
            radar = self._vision.grab_client_roi_bgr(hwnd, self.store.radar_roi)
        except Exception:
            rect = get_window_rect(hwnd)
            radar = self._vision.grab_radar_bgr(rect, self.store.radar_roi)
        path = Path(self.store.empty_radar_path)
        cv2.imwrite(str(path), radar)
        self.log(f"Эталон пустого радара сохранён: {path}")
        return path

    def capture_empty_text(self) -> Path:
        hwnd = self._get_game_hwnd()
        roi = self.store.empty_text_roi
        # IMPORTANT: ROI is stored in client coordinates (picked from client screenshots),
        # so we must capture from client area to avoid titlebar/border offsets (DPI).
        # Keep a fallback to window-rect capture for older configs.
        try:
            radar = self._vision.grab_client_roi_bgr(hwnd, roi)
            cap_mode = "client"
        except Exception:
            rect = get_window_rect(hwnd)
            radar = self._vision.grab_radar_bgr(rect, roi)
            cap_mode = "window"
        path = Path(self.store.empty_text_path)
        cv2.imwrite(str(path), radar)
        self.log(
            f"Шаблон текста пустого радара сохранён: {path} "
            f"(mode={cap_mode}, Text ROI x={roi.x}, y={roi.y}, w={roi.w}, h={roi.h}, img={radar.shape[1]}x{radar.shape[0]})"
        )
        return path

    def capture_menu_open_template(self) -> Path:
        hwnd = self._get_game_hwnd()
        roi = getattr(self.store, "menu_autoclose_roi", RadarROI(x=0, y=0, w=1, h=1))
        img = self._vision.grab_client_roi_bgr(hwnd, roi)
        path = Path(str(getattr(self.store, "menu_autoclose_tpl_path", "menu_open_tpl.png") or "menu_open_tpl.png"))
        cv2.imwrite(str(path), img)
        self.log(f"Шаблон меню/чата сохранён: {path} (roi={roi.x},{roi.y},{roi.w},{roi.h})")
        return path

    def capture_death_template(self) -> Path:
        hwnd = self._get_game_hwnd()
        roi = getattr(self.store, "death_roi", RadarROI(x=0, y=0, w=1, h=1))
        img = self._vision.grab_client_roi_bgr(hwnd, roi)
        path = Path(str(getattr(self.store, "death_tpl_path", "death_tpl.png") or "death_tpl.png"))
        cv2.imwrite(str(path), img)
        self.log(f"Шаблон смерти сохранён: {path} (roi={roi.x},{roi.y},{roi.w},{roi.h})")
        return path

    def capture_confirm_popup_template(self) -> Path:
        """
        Capture a template of the "confirm enter" popup (e.g. gate/dungeon entry dialog).
        """
        hwnd = self._get_game_hwnd()
        roi = getattr(self.store, "confirm_popup_roi", RadarROI(x=0, y=0, w=1, h=1))
        img = self._vision.grab_client_roi_bgr(hwnd, roi)
        path = Path(str(getattr(self.store, "confirm_popup_tpl_path", "confirm_popup_tpl.png") or "confirm_popup_tpl.png"))
        cv2.imwrite(str(path), img)
        self.log(f"Шаблон подтверждения входа сохранён: {path} (roi={roi.x},{roi.y},{roi.w},{roi.h})")
        return path

    def capture_gate_template(self) -> Path:
        """
        Capture a template of the gate/portal (for GATE step).
        """
        hwnd = self._get_game_hwnd()
        roi = getattr(self.store, "gate_roi", RadarROI(x=0, y=0, w=1, h=1))
        img = self._vision.grab_client_roi_bgr(hwnd, roi)
        path = Path(str(getattr(self.store, "gate_tpl_path", "gate_tpl.png") or "gate_tpl.png"))
        cv2.imwrite(str(path), img)
        self.log(f"Шаблон ворот сохранён: {path} (roi={roi.x},{roi.y},{roi.w},{roi.h})")
        return path

    def _confirm_popup_score(self, hwnd: int) -> float | None:
        roi = getattr(self.store, "confirm_popup_roi", RadarROI(x=0, y=0, w=1, h=1))
        if int(getattr(roi, "w", 1)) <= 5 or int(getattr(roi, "h", 1)) <= 5:
            return None

    def _gate_match(self, hwnd: int) -> tuple[float, int, int] | None:
        roi = getattr(self.store, "gate_roi", RadarROI(x=0, y=0, w=1, h=1))
        if int(getattr(roi, "w", 1)) <= 10 or int(getattr(roi, "h", 1)) <= 10:
            return None
        tpl_path = Path(str(getattr(self.store, "gate_tpl_path", "gate_tpl.png") or "gate_tpl.png"))
        if not tpl_path.exists():
            return None
        tpl = cv2.imread(str(tpl_path), cv2.IMREAD_COLOR)
        if tpl is None:
            return None
        try:
            cur = self._vision.grab_client_roi_bgr(hwnd, roi)
        except Exception:
            return None
        # If template equals ROI (captured as full ROI), comparing identical sizes is OK.
        # For better robustness users should capture a smaller distinctive patch, but we handle both cases.
        try:
            a = cv2.cvtColor(cur, cv2.COLOR_BGR2GRAY)
            b = cv2.cvtColor(tpl, cv2.COLOR_BGR2GRAY)
            a = cv2.GaussianBlur(a, (3, 3), 0)
            b = cv2.GaussianBlur(b, (3, 3), 0)
            if a.shape[0] < b.shape[0] or a.shape[1] < b.shape[1]:
                # resize template down if misconfigured
                b = cv2.resize(b, (min(a.shape[1], b.shape[1]), min(a.shape[0], b.shape[0])))
            res = cv2.matchTemplate(a, b, cv2.TM_CCOEFF_NORMED)
            _min_val, max_val, _min_loc, max_loc = cv2.minMaxLoc(res)
            cx = int(max_loc[0] + (b.shape[1] // 2))
            cy = int(max_loc[1] + (b.shape[0] // 2))
            return float(max_val), cx, cy
        except Exception:
            return None

    def _gate_seek_and_click(self, hwnd: int) -> bool:
        """
        Find gate by template in gate_roi, rotate camera using RMB drag to center it, then left click.
        """
        roi = getattr(self.store, "gate_roi", RadarROI(x=0, y=0, w=1, h=1))
        thr = float(getattr(self.store, "gate_threshold", 0.83))
        timeout = max(1.0, float(getattr(self.store, "gate_seek_timeout_s", 6.0)))
        margin = max(5, int(getattr(self.store, "gate_center_margin_px", 40)))
        step_px = max(20, int(getattr(self.store, "gate_turn_step_px", 120)))

        # keep RMB held while turning
        self._input.mouse_down("right")
        t0 = time.time()
        ok = False
        try:
            while (time.time() - t0) < timeout and (not self._stop.is_set()):
                self._pause_wait()
                m = self._gate_match(hwnd)
                if m is None:
                    time.sleep(0.05)
                    continue
                score, cx, cy = m
                if score < thr:
                    time.sleep(0.05)
                    continue
                # want gate near center of ROI
                dx = int(cx - (int(roi.w) // 2))
                if abs(dx) <= margin:
                    ok = True
                    break
                # drag opposite direction to bring gate to center (heuristic)
                drag = -step_px if dx > 0 else step_px
                self._input.move_rel(drag, 0, duration=0.0)
                time.sleep(0.04)
        finally:
            self._input.mouse_up("right")

        if not ok:
            return False

        # Recompute final location and click it
        m2 = self._gate_match(hwnd)
        if m2 is None or m2[0] < thr:
            return False
        _sc, cx2, cy2 = m2
        client_x = int(roi.x + cx2)
        client_y = int(roi.y + cy2)
        try:
            abs_x, abs_y = win32gui.ClientToScreen(int(hwnd), (int(client_x), int(client_y)))
        except Exception:
            # fallback: window-rect based
            rect = get_window_rect(hwnd)
            abs_x, abs_y = to_abs_xy(rect, client_x, client_y)
        self._input.move_and_click_abs(int(abs_x), int(abs_y), button="left")
        return True
        tpl_path = Path(str(getattr(self.store, "confirm_popup_tpl_path", "confirm_popup_tpl.png") or "confirm_popup_tpl.png"))
        if not tpl_path.exists():
            return None
        tpl = cv2.imread(str(tpl_path), cv2.IMREAD_COLOR)
        if tpl is None:
            return None
        try:
            cur = self._vision.grab_client_roi_bgr(hwnd, roi)
        except Exception:
            return None
        try:
            a = cv2.cvtColor(cur, cv2.COLOR_BGR2GRAY)
            b = cv2.cvtColor(tpl, cv2.COLOR_BGR2GRAY)
            a = cv2.GaussianBlur(a, (3, 3), 0)
            b = cv2.GaussianBlur(b, (3, 3), 0)
            if a.shape[0] < b.shape[0] or a.shape[1] < b.shape[1]:
                b = cv2.resize(b, (min(a.shape[1], b.shape[1]), min(a.shape[0], b.shape[0])))
            res = cv2.matchTemplate(a, b, cv2.TM_CCOEFF_NORMED)
            _min_val, max_val, _min_loc, _max_loc = cv2.minMaxLoc(res)
            return float(max_val)
        except Exception:
            return None

    def _death_score(self, hwnd: int) -> float | None:
        if not bool(getattr(self.store, "death_detect_enabled", False)):
            return None
        roi = getattr(self.store, "death_roi", RadarROI(x=0, y=0, w=1, h=1))
        if int(getattr(roi, "w", 1)) <= 5 or int(getattr(roi, "h", 1)) <= 5:
            return None
        tpl_path = Path(str(getattr(self.store, "death_tpl_path", "death_tpl.png") or "death_tpl.png"))
        if not tpl_path.exists():
            return None
        tpl = cv2.imread(str(tpl_path), cv2.IMREAD_COLOR)
        if tpl is None:
            return None
        try:
            cur = self._vision.grab_client_roi_bgr(hwnd, roi)
            return float(self._vision.text_match_score(cur_bgr=cur, tpl_bgr=tpl))
        except Exception:
            return None

    def _maybe_handle_death(self, hwnd: int) -> bool:
        """
        If death screen detected, runs configured recovery route.
        Returns True if handled (death detected and action taken), else False.
        """
        if not bool(getattr(self.store, "death_detect_enabled", False)):
            return False
        now = time.time()
        cooldown = max(3.0, float(getattr(self.store, "death_cooldown_s", 20.0)))
        if now - float(self._last_death_ts) < cooldown:
            return False

        score = self._death_score(hwnd)
        thr = float(getattr(self.store, "death_threshold", 0.86))
        if score is None or score < thr:
            return False

        route_name = getattr(self.store, "death_route_name", None)
        if not route_name:
            self._last_death_ts = now
            self.log(f"Смерть обнаружена (score={score:.3f}>=thr={thr:.3f}), но маршрут восстановления не выбран.")
            return True

        recovery = None
        try:
            for r in self.store.get_active_profile().routes:
                if r.name == route_name:
                    recovery = r
                    break
        except Exception:
            recovery = None

        self._last_death_ts = now
        self.log(f"Смерть обнаружена (score={score:.3f}>=thr={thr:.3f}). Запускаю маршрут восстановления: {route_name!r}")
        self._in_base = True
        try:
            if not self._maybe_focus_game(hwnd):
                self.log("Восстановление: игра не в фокусе (перехват фокуса выключен).")
            elif recovery:
                self.run_route(recovery)
        except Exception as e:
            self.log(f"Восстановление: ошибка {e!r}")
        finally:
            self._in_base = False
        return True

    def click_point(self, p: Point) -> None:
        hwnd = self._get_game_hwnd()
        self._maybe_focus_game(hwnd)
        rect = get_window_rect(hwnd)

        abs_x, abs_y = to_abs_xy(rect, p.rel_x, p.rel_y)
        self._input.move_and_click_abs(abs_x, abs_y)

    def run_route(self, route: Route) -> None:
        self._set_action(f"Маршрут: {route.name}")
        hwnd = self._get_game_hwnd()
        rect = get_window_rect(hwnd)

        for idx, step in enumerate(route.steps):
            self._pause_wait()
            if self._stop.is_set():
                return

            self._set_action(f"Маршрут: {route.name} (шаг {idx+1}/{len(route.steps)})")
            self._maybe_auto_confirm(hwnd)
            # If a menu/chat steals input, close it before performing actions.
            try:
                self._maybe_close_menu(hwnd)
            except Exception:
                pass

            if step.kind == "wait":
                pass
            elif step.kind == "wait_range":
                mn = float(getattr(step, "min_s", 0.0) or 0.0)
                mx = float(getattr(step, "max_s", 0.0) or 0.0)
                if mx < mn:
                    mn, mx = mx, mn
                delay = random.uniform(max(0.0, mn), max(0.0, mx))
                if delay > 0:
                    self._sleep_interruptible(delay)
                # already slept; do not apply extra post-step delay below
                continue
            elif step.kind == "gate":
                if not self._ensure_game_foreground_strict(
                    hwnd,
                    allow_steal=bool(getattr(self.store, "focus_steal_enabled", True)),
                    why="route:gate",
                ):
                    continue
                ok = False
                try:
                    ok = self._gate_seek_and_click(hwnd)
                except Exception:
                    ok = False
                self.log("GATE: клик выполнен" if ok else "GATE: не удалось найти/кликнуть ворота")
                # let next steps handle CONFIRM etc.
            elif step.kind == "confirm":
                # Wait until confirm popup appears, then press confirm key once.
                timeout_s = max(0.5, float(getattr(step, "timeout_s", 6.0) or 6.0))
                thr = float(getattr(self.store, "confirm_popup_threshold", 0.86))
                key = (self.store.auto_confirm_key or "y").strip().lower() or "y"
                t0 = time.time()
                ok_popup = False
                while (time.time() - t0) < timeout_s and (not self._stop.is_set()):
                    self._pause_wait()
                    sc = self._confirm_popup_score(hwnd)
                    if sc is not None and sc >= thr:
                        ok_popup = True
                        break
                    time.sleep(0.05)
                if ok_popup:
                    self.log(f"CONFIRM: окно найдено, жму {key.upper()}")
                    try:
                        self._input.press_key_any(key, hold_s=float(getattr(self.store, "key_hold_s", 0.06)))
                    except Exception:
                        pass
                else:
                    self.log("CONFIRM: окно не найдено (timeout)")
            elif step.kind == "key":
                if not self._ensure_game_foreground_strict(
                    hwnd,
                    allow_steal=bool(getattr(self.store, "focus_steal_enabled", True)),
                    why=f"route:key {step.key!r}",
                ):
                    # don't press keys into background window
                    continue
                key = (step.key or "").strip().lower()
                if key:
                    self._input.press_key_any(key, hold_s=float(getattr(self.store, "key_hold_s", 0.06)))
            else:
                # Prefer normalized coordinates if present (robust to resolution/scale changes)
                if not self._ensure_game_foreground_strict(
                    hwnd,
                    allow_steal=bool(getattr(self.store, "focus_steal_enabled", True)),
                    why=f"route:click ({step.rel_x},{step.rel_y})",
                ):
                    continue
                rx = int(step.rel_x)
                ry = int(step.rel_y)
                if step.x_pct is not None and step.y_pct is not None:
                    rx = int(float(step.x_pct) * max(1, rect.width))
                    ry = int(float(step.y_pct) * max(1, rect.height))
                abs_x, abs_y = to_abs_xy(rect, rx, ry)
                self._input.move_and_click_abs(abs_x, abs_y, button=step.button or "left")

            # Delay after step: either per-step delay_s, or random range from settings.
            delay = max(0.0, float(step.delay_s))
            rmin = max(0.0, float(getattr(self.store, "route_delay_min_s", 0.0) or 0.0))
            rmax = max(0.0, float(getattr(self.store, "route_delay_max_s", 0.0) or 0.0))
            if rmax > 0.0 and rmax >= rmin:
                delay = random.uniform(rmin, rmax)
            if delay > 0.0:
                self._sleep_interruptible(delay)

        self._set_action("Ожидание")

    def _sleep_interruptible(self, seconds: float) -> None:
        end = time.time() + seconds
        while not self._stop.is_set() and time.time() < end:
            self._pause_wait()
            # if some modal popup blocks progress, try to confirm it
            try:
                self._maybe_auto_confirm(self._get_game_hwnd())
            except Exception:
                pass
            time.sleep(0.05)

    def _maybe_auto_confirm(self, hwnd: int) -> None:
        if not bool(self.store.auto_confirm_enabled):
            return
        now = time.time()
        interval = max(0.2, float(self.store.auto_confirm_interval_s))
        if now - float(self._last_auto_confirm_ts) < interval:
            return
        key = (self.store.auto_confirm_key or "").strip().lower()
        if not key:
            return
        self._last_auto_confirm_ts = now
        # Don't steal focus while user is alt-tabbed to UI (especially in fullscreen games).
        if not self._maybe_focus_game(hwnd):
            return
        self._input.press_key_hold(key, hold_s=float(getattr(self.store, "key_hold_s", 0.06)))

    def _run(self) -> None:
        self._set_action("Запуск")
        self.log("Старт бота")
        self.log(f"ТП по врагу: {'ВКЛ' if bool(getattr(self.store, 'tp_on_enemy_enabled', True)) else 'ВЫКЛ'}")
        self.log(
            f"Фокус для ТП: {'ВКЛ' if bool(getattr(self.store, 'tp_focus_steal_enabled', True)) else 'ВЫКЛ'}; "
            f"перехват фокуса игры: {'ВКЛ' if bool(getattr(self.store, 'focus_steal_enabled', True)) else 'ВЫКЛ'}"
        )
        self.log(f"ТП по урону: {'ВКЛ' if bool(getattr(self.store, 'damage_tp_enabled', False)) else 'ВЫКЛ'}")
        if bool(getattr(self.store, "damage_tp_enabled", False)):
            try:
                atk_path = Path(str(getattr(self.store, "damage_icon_tpl_path", "damage_icon_tpl.png") or "damage_icon_tpl.png"))
                norm_path = Path(
                    str(
                        getattr(self.store, "damage_icon_normal_tpl_path", "damage_icon_normal_tpl.png")
                        or "damage_icon_normal_tpl.png"
                    )
                )
                self.log(
                    f"ТП по урону: шаблоны: мечи={'OK' if atk_path.exists() else 'НЕТ'}, обычная={'OK' if norm_path.exists() else 'НЕТ'}"
                )
            except Exception:
                pass

        # refresh input timings from config on start
        self._input = InputController(
            min_move_time=self.store.move_time_min_s,
            max_move_time=self.store.move_time_max_s,
        )

        # On start: optional setup (enter city/location), then go farm (route mode),
        # or just arm immediately (stay mode).
        try:
            setup_route = self.store.get_setup_route()
            farm_route = self.store.get_farm_route()

            # AFK mode means "stand still": do not run any routes on start.
            if (not self.store.farm_without_route) and setup_route:
                self._in_base = True
                self._set_action("Setup: вход в локацию/город")
                self.log("Старт: выполняю setup маршрут (вход в локацию/город)")
                self.run_route(setup_route)
                self._sleep_interruptible(0.8 + random.uniform(0.0, 0.4))

            if (not self.store.farm_without_route) and farm_route:
                self._in_base = True
                self._armed = False
                self._set_action("Farm: выход на фарм")
                self.log("Старт: выполняю маршрут (выход на фарм)")
                self.run_route(farm_route)
                self._in_base = False
                self._sleep_interruptible(0.8 + random.uniform(0.0, 0.4))
                self._armed = True
                self._set_action("Фарм: мониторинг")
                self.log("Маршрут выполнен. Бот в режиме фарма (детект включен).")
            else:
                self._armed = True if self.store.farm_without_route else False
                if self.store.farm_without_route:
                    self._in_base = False
                    self._set_action("AFK: мониторинг")
                    self.log("Старт: режим без маршрута (стоим на месте, детект включен).")
        except Exception as e:
            self.log(f"Ошибка старта маршрута: {e!r}")
            self._armed = True  # still allow detection if route failed

        while not self._stop.is_set():
            self._pause_wait()
            try:
                if not self._pause.is_set():
                    # default action while looping
                    if bool(getattr(self.store, "damage_tp_enabled", False)):
                        self._set_action("Мониторинг: урон/HP")
                    else:
                        self._set_action("Мониторинг: радар")
                if not bool(getattr(self.store, "radar_detect_enabled", True)):
                    # Detection disabled: still allow damage-mode check (and keep auto-confirm alive)
                    try:
                        self._maybe_auto_confirm(self._get_game_hwnd())
                    except Exception:
                        pass
                    # Damage/HP mode should still work even if radar detect is OFF.
                    try:
                        if bool(getattr(self.store, "damage_tp_enabled", False)):
                            hwnd = self._get_game_hwnd()
                            attacked, atk_score, norm_score = self._is_attacked_by_icon(hwnd)
                            # Always compute hp_pct (optional trigger), even if "attacked" is False.
                            hp_pct = None
                            try:
                                hp_pct = self._hp_percent(hwnd)
                            except Exception:
                                hp_pct = None
                            if attacked:
                                icon_thr = float(getattr(self.store, "damage_icon_threshold", 0.86))
                                norm_thr = float(getattr(self.store, "damage_icon_normal_threshold", 0.86))
                                self._maybe_attacked_alert(
                                    hwnd=hwnd,
                                    rect=rect,
                                    atk_score=atk_score,
                                    norm_score=norm_score,
                                    atk_thr=float(icon_thr),
                                    norm_thr=float(norm_thr),
                                    hp_pct=hp_pct,
                                )

                            # Perform TP by either trigger:
                            # - attacked icon detected
                            # - (optional) HP below threshold
                            try:
                                rect = get_window_rect(hwnd)
                            except Exception:
                                rect = None
                            if rect is not None:
                                hp_gate = bool(getattr(self.store, "hp_tp_enabled", True))
                                hp_thr = int(getattr(self.store, "hp_tp_threshold_pct", 70))
                                trigger_hp = bool(hp_gate and (hp_pct is not None) and (int(hp_pct) <= int(hp_thr)))
                                trigger_attacked = bool(attacked)
                                if (trigger_attacked or trigger_hp) and (not self._in_base):
                                    reason_parts = []
                                    if trigger_attacked:
                                        reason_parts.append(f"attacked(atk={atk_score}, norm={norm_score})")
                                    if trigger_hp:
                                        reason_parts.append(f"hp={hp_pct}%<={hp_thr}%")
                                    ok = self._teleport_spam(hwnd, rect, reason=" + ".join(reason_parts) or "trigger")
                                    if ok:
                                        self._in_base = True
                                        wait_s = max(1, int(self._tp_wait_s()))
                                        self.log(f"ТП по урону: в базе. Жду {wait_s} сек.")
                                        self._sleep_interruptible(wait_s + random.uniform(-0.6, 0.6))
                                        if self._stop.is_set():
                                            break
                                        self.log("ТП по урону: возврат к фарму")
                                        farm_route = self.store.get_farm_route()
                                        if (not self.store.farm_without_route) and farm_route:
                                            self.run_route(farm_route)
                                        else:
                                            self.log("Режим без маршрута: возврат пропущен (остаюсь на месте).")
                                        self._in_base = False
                    except Exception as e:
                        now = time.time()
                        if now - float(self._last_damage_err_ts) >= 5.0:
                            self._last_damage_err_ts = now
                            self.log(f"ТП по урону: ошибка (radar_detect=off): {e!r}")
                    time.sleep(0.25)
                    continue

                farm_route = self.store.get_farm_route()
                if (not self.store.farm_without_route) and (not farm_route):
                    self.log("Нет активного маршрута. Создай маршрут и выбери активный.")
                    self._sleep_interruptible(1.0)
                    continue

                hwnd = self._get_game_hwnd()
                rect = get_window_rect(hwnd)

                # Death detection (before doing anything else)
                try:
                    if self._maybe_handle_death(hwnd):
                        time.sleep(0.25)
                        continue
                except Exception:
                    pass

                # Damage/HP teleport mode (alternative to enemy radar TP)
                try:
                    if bool(getattr(self.store, "damage_tp_enabled", False)):
                        attacked, atk_score, norm_score = self._is_attacked_by_icon(hwnd)
                        icon_thr = float(getattr(self.store, "damage_icon_threshold", 0.86))
                        norm_thr = float(getattr(self.store, "damage_icon_normal_threshold", 0.86))

                        hp_pct = None
                        try:
                            hp_pct = self._hp_percent(hwnd)
                        except Exception:
                            hp_pct = None
                        try:
                            self.last_attacked = bool(attacked)
                            self.last_hp_pct = int(hp_pct) if hp_pct is not None else None
                        except Exception:
                            pass
                        hp_gate = bool(getattr(self.store, "hp_tp_enabled", True))
                        hp_thr = int(getattr(self.store, "hp_tp_threshold_pct", 70))
                        trigger_hp = bool(hp_gate and (hp_pct is not None) and (int(hp_pct) <= int(hp_thr)))
                        trigger_attacked = bool(attacked)

                        # Debug (not spammy): show why attacked isn't triggering.
                        now_dbg = time.time()
                        if now_dbg - float(self._last_damage_debug_ts) >= 2.0:
                            self._last_damage_debug_ts = now_dbg
                            tpl_path = Path(str(getattr(self.store, "damage_icon_tpl_path", "damage_icon_tpl.png") or "damage_icon_tpl.png"))
                            norm_path = Path(
                                str(getattr(self.store, "damage_icon_normal_tpl_path", "damage_icon_normal_tpl.png") or "damage_icon_normal_tpl.png")
                            )
                            if not tpl_path.exists():
                                if now_dbg - float(self._last_damage_missing_tpl_ts) >= 10.0:
                                    self._last_damage_missing_tpl_ts = now_dbg
                                    self.log(f"ТП по урону: нет шаблона иконки атаки: {tpl_path} (сделай «Сделать шаблон иконки атаки»)")
                            elif not norm_path.exists():
                                if now_dbg - float(self._last_damage_missing_tpl_ts) >= 10.0:
                                    self._last_damage_missing_tpl_ts = now_dbg
                                    self.log(f"ТП по урону: нет шаблона обычной иконки: {norm_path} (сделай «Сделать шаблон обычной иконки»)")
                            else:
                                self.log(
                                    "ТП по урону: "
                                    f"atk_score={atk_score if atk_score is not None else 'None'} (thr={icon_thr:.3f}), "
                                    f"norm_score={norm_score if norm_score is not None else 'None'} (thr={norm_thr:.3f}), "
                                    f"attacked={int(attacked)}, hp={hp_pct}, hp_thr={hp_thr}, "
                                    f"trigger_attacked={int(trigger_attacked)}, trigger_hp={int(trigger_hp)}, in_base={int(self._in_base)}"
                                )

                        if attacked:
                            self._maybe_attacked_alert(
                                hwnd=hwnd,
                                rect=rect,
                                atk_score=atk_score,
                                norm_score=norm_score,
                                atk_thr=float(icon_thr),
                                norm_thr=float(norm_thr),
                                hp_pct=hp_pct,
                            )

                        # Only perform TP when not in base/route-transition state.
                        # Triggers are independent:
                        # - attacked icon detected
                        # - (optional) HP below threshold
                        if (trigger_attacked or trigger_hp) and (not self._in_base):
                            reason_parts = []
                            if trigger_attacked:
                                reason_parts.append(f"attacked(atk={atk_score}, norm={norm_score})")
                            if trigger_hp:
                                reason_parts.append(f"hp={hp_pct}%<={hp_thr}%")
                            reason = " + ".join(reason_parts) or "trigger"
                            ok = self._teleport_spam(hwnd, rect, reason=reason)
                            if ok:
                                # Post-TP flow is same as normal TP: wait + return route.
                                self._in_base = True
                                wait_s = max(1, int(self._tp_wait_s()))
                                self.log(f"ТП по урону: в базе. Жду {wait_s} сек.")
                                self._sleep_interruptible(wait_s + random.uniform(-0.6, 0.6))
                                if self._stop.is_set():
                                    break
                                self.log("ТП по урону: возврат к фарму")
                                farm_route = self.store.get_farm_route()
                                if (not self.store.farm_without_route) and farm_route:
                                    self.run_route(farm_route)
                                else:
                                    self.log("Режим без маршрута: возврат пропущен (остаюсь на месте).")
                                self._in_base = False
                            # Regardless of ok, do not run enemy radar TP logic when damage mode enabled
                            time.sleep(0.15)
                            continue
                except Exception as e:
                    now = time.time()
                    if now - float(self._last_damage_err_ts) >= 5.0:
                        self._last_damage_err_ts = now
                        self.log(f"ТП по урону: ошибка: {e!r}")

                # --- Auto-buy potions (OCR) ---
                try:
                    if bool(getattr(self.store, "auto_buy_potions_enabled", False)):
                        now = time.time()
                        interval = max(1.0, float(getattr(self.store, "auto_buy_check_interval_s", 5.0)))
                        cooldown = max(5.0, float(getattr(self.store, "auto_buy_cooldown_s", 60.0)))
                        if (now - float(self._last_auto_buy_check_ts)) >= interval and (now - float(self._last_auto_buy_ts)) >= cooldown:
                            self._last_auto_buy_check_ts = now
                            # Optional constraint: only in route farming mode (not AFK).
                            if bool(getattr(self.store, "auto_buy_route_mode_only", True)) and bool(getattr(self.store, "farm_without_route", False)):
                                # AFK mode: skip auto-buy if user requested "route mode only".
                                pass
                            else:
                                roi = getattr(self.store, "auto_buy_potions_roi", RadarROI(x=0, y=0, w=1, h=1))
                                if int(getattr(roi, "w", 1)) > 5 and int(getattr(roi, "h", 1)) > 5:
                                    img = self._vision.grab_client_roi_bgr(hwnd, roi)
                                    cnt, dbg, _bw = read_int_debug(img)
                                    thr = int(getattr(self.store, "auto_buy_potions_threshold", 300))
                                    route_name = getattr(self.store, "auto_buy_potions_route_name", None)
                                    buy_route = None
                                    if route_name:
                                        for r in self.store.get_active_profile().routes:
                                            if r.name == route_name:
                                                buy_route = r
                                                break
                                # Heuristic without Tesseract:
                                # - if we see 4+ digits, it's definitely >=1000, so skip (not low stock)
                                # - if OCR fails but digit_count>=4, also skip
                                digit_count = int(dbg.get("digit_count") or 0)
                                if cnt is None and digit_count >= 4:
                                    cnt = 9999

                                if cnt is not None and buy_route is not None and cnt < thr and (not self._in_base) and self._armed:
                                        self.log(f"Банок мало: {cnt} < {thr}. Запускаю автозакупку: {buy_route.name!r}")
                                        self._last_auto_buy_ts = now
                                        self._in_base = True
                                        if not self._maybe_focus_game(hwnd):
                                            self.log("Автозакупка: игра не в фокусе (перехват фокуса выключен).")
                                        else:
                                            self.run_route(buy_route)
                                            wait_s = max(0.0, float(getattr(self.store, "auto_buy_city_wait_s", 8.0)))
                                            if wait_s:
                                                self._sleep_interruptible(wait_s + random.uniform(0.0, 0.6))
                                            # Return to farm after buying (route mode)
                                            if bool(getattr(self.store, "auto_buy_return_to_farm", True)) and (not bool(getattr(self.store, "farm_without_route", False))):
                                                farm_route = self.store.get_farm_route()
                                                if farm_route:
                                                    self.log("Автозакупка: возврат по Farm маршруту")
                                                    self.run_route(farm_route)
                                        self._in_base = False
                except Exception:
                    pass

                # If the game is not in foreground, the captured ROI may include other windows.
                # To avoid false teleports while the user is alt-tabbed / overlays cover the game,
                # pause detection unless we are allowed to steal focus globally.
                if not self._is_game_foreground(hwnd) and not bool(getattr(self.store, "focus_steal_enabled", True)):
                    time.sleep(0.25)
                    continue

                if self.store.detect_mode == "color":
                    # Legacy mode (rarely used in UI now): fall back to full radar ROI.
                    radar = self._vision.grab_radar_bgr(rect, self.store.radar_roi)
                    enemy, score = self._vision.detect_enemy_by_color(radar, self.store.enemy_hsv)
                    score_label = f"pixels={score}"
                else:
                    # Text-only detection: "Нет Цель поиска" present => EMPTY (no target/enemy).
                    text_roi = self.store.empty_text_roi
                    text_path = Path(self.store.empty_text_path)
                    text_tpl = cv2.imread(str(text_path), cv2.IMREAD_COLOR) if text_path.exists() else None

                    if text_tpl is None or int(text_roi.w) <= 5 or int(text_roi.h) <= 5:
                        self.log("Нет шаблона текста. Выбери Text ROI и нажми 'Сделать скриншот текста'.")
                        self._sleep_interruptible(1.0)
                        continue

                    if True:
                        # ROI is usually picked in client coordinates; older configs might be window-rect based.
                        # We try both and take the better match to avoid "ROI shifted" issues.
                        cur_text_client = None
                        cur_text_window = None
                        score_client = -1.0
                        score_window = -1.0
                        try:
                            cur_text_client = self._vision.grab_client_roi_bgr(hwnd, text_roi)
                            score_client = self._vision.text_match_score(cur_bgr=cur_text_client, tpl_bgr=text_tpl)
                        except Exception:
                            pass
                        try:
                            cur_text_window = self._vision.grab_radar_bgr(rect, text_roi)
                            score_window = self._vision.text_match_score(cur_bgr=cur_text_window, tpl_bgr=text_tpl)
                        except Exception:
                            pass

                        if score_client >= score_window and cur_text_client is not None:
                            cur_text = cur_text_client
                            score = float(score_client)
                            cap_mode = "client"
                        else:
                            cur_text = cur_text_window if cur_text_window is not None else cur_text_client
                            score = float(score_window if score_window >= 0 else score_client)
                            cap_mode = "window"
                        thr = float(self.store.empty_text_threshold)

                        # Fail-safe: if the current frame has almost no "text pixels",
                        # it's usually a redraw/overlay glitch. Treat as empty/unknown to avoid false TP.
                        cur_cnt = self._vision.text_mask_pixel_count(cur_text)
                        tpl_cnt = self._vision.text_mask_pixel_count(text_tpl)
                        min_cnt = max(20, int(tpl_cnt * 0.10))
                        if cur_cnt < min_cnt and tpl_cnt >= 20:
                            is_empty = True
                            enemy = False
                            score_label = (
                                f"empty_text_match={score:.3f} (thr={thr:.3f}, {cap_mode}), mask_low {cur_cnt}<{min_cnt}"
                            )
                        else:
                            # Big hysteresis to avoid false "text missing" when UI flickers/brightens.
                            # We treat as "missing" only if score is *far* below thr.
                            miss_thr = max(0.0, thr - 0.35)
                            if score >= thr:
                                is_empty = True
                                enemy = False
                            elif score <= miss_thr:
                                is_empty = False
                                enemy = True
                            else:
                                # Grey zone: keep it as empty to prevent false positives.
                                is_empty = True
                                enemy = False
                            score_label = f"empty_text_match={score:.3f} (thr={thr:.3f}, miss={miss_thr:.3f}, {cap_mode})"

                # Debounce false-positives: require consecutive detections
                if enemy:
                    self._enemy_streak += 1
                else:
                    self._enemy_streak = 0

                # Publish latest detect info for mini UI.
                try:
                    self.last_radar_enemy = bool(enemy)
                    self.last_radar_score_label = str(score_label)
                    if "empty_text_match=" in str(score_label):
                        self.last_text_match_score = float(score)
                        self.last_text_cap_mode = str(cap_mode)
                except Exception:
                    pass

                required = max(1, int(self.store.detect_confirm_streak))
                cooldown = max(0.0, float(self.store.min_tp_cooldown_s))
                can_tp_by_time = (time.time() - self._last_tp_ts) >= cooldown

                tp_enabled = bool(getattr(self.store, "tp_on_enemy_enabled", True))
                enemy_confirmed = bool(enemy and self._armed and not self._in_base and self._enemy_streak >= required)

                if enemy_confirmed:
                    # Sound notification even if TP is disabled/cooldown (helps user notice real detections).
                    self._maybe_enemy_alert(score_label=score_label)

                if enemy_confirmed and tp_enabled and can_tp_by_time:
                    self.log(f"Враг/цель подтверждена ({score_label}, streak={self._enemy_streak}/{required})")
                    # Close menu/chat before teleport key (common reason why TP doesn't fire).
                    try:
                        self._maybe_close_menu(hwnd)
                    except Exception:
                        pass
                    ok = self._teleport_with_retries(hwnd, rect, score_label=score_label)
                    if not ok:
                        self.log("ТП не прошёл после ретраев. Детект сброшен, продолжаю мониторинг.")
                        self._enemy_streak = 0
                        time.sleep(0.25)
                        continue

                    # Optional action right after teleport: press R or click radar to "reset target"
                    action = (self.store.post_tp_action or "none").strip().lower()
                    delay = max(0.0, float(self.store.post_tp_delay_s))
                    if delay:
                        self._sleep_interruptible(delay + random.uniform(0.0, 0.08))

                    if action == "press_r":
                        key = (self.store.post_tp_key or "r").strip().lower()
                        if key:
                            self.log(f"После ТП: нажимаю {key.upper()}")
                            self._input.press_key_hold(key, hold_s=float(getattr(self.store, "key_hold_s", 0.06)))
                    elif action == "click_radar":
                        # click in the center of radar ROI
                        rx = int(self.store.radar_roi.x + self.store.radar_roi.w // 2)
                        ry = int(self.store.radar_roi.y + self.store.radar_roi.h // 2)
                        abs_x, abs_y = to_abs_xy(rect, rx, ry)
                        self.log("После ТП: клик по радару")
                        self._input.move_and_click_abs(abs_x, abs_y, button="left")
                    else:
                        pass

                    self._in_base = True

                    wait_s = max(1, int(self._tp_wait_s()))
                    self.log(f"В базе. Жду {wait_s} сек.")
                    self._sleep_interruptible(wait_s + random.uniform(-0.6, 0.6))

                    if self._stop.is_set():
                        break

                    self.log("Возврат к фарму")
                    if (not self.store.farm_without_route) and farm_route:
                        self.run_route(farm_route)
                    else:
                        self.log("Режим без маршрута: возврат пропущен (остаюсь на месте).")
                    self._in_base = False

                    # give time for movement to start
                    self._sleep_interruptible(0.7 + random.uniform(0.0, 0.4))
                else:
                    # Debug/clarity: if detection triggers but TP blocked, log the reason (not spammy)
                    if enemy:
                        reason = "ok"
                        if not tp_enabled:
                            reason = "tp_disabled"
                        elif not self._armed:
                            reason = "not_armed"
                        elif self._in_base:
                            reason = "in_base"
                        elif self._enemy_streak < required:
                            reason = f"streak {self._enemy_streak}/{required}"
                        elif not can_tp_by_time:
                            reason = "cooldown"

                        if reason != "ok":
                            msg = f"Детект есть ({score_label}), но ТП заблокирован: {reason}"
                            if msg != self._last_tp_block_reason:
                                self._last_tp_block_reason = msg
                                self.log(msg)
                        else:
                            self._last_tp_block_reason = None

                        # Also log score occasionally to help tune thresholds
                        now = time.time()
                        if now - self._last_score_log_ts >= 2.5:
                            self._last_score_log_ts = now
                            self.log(f"Детект активен ({score_label})")

                    # AFK farm mode: just wait and keep checking radar
                    base = max(0.05, float(self.store.check_interval_s))
                    jitter = max(0.0, float(self.store.loop_jitter_s))
                    time.sleep(base + random.uniform(0.0, jitter))

            except WindowNotFoundError as e:
                self.log(str(e))
                self._sleep_interruptible(1.0)
            except Exception as e:
                self.log(f"Ошибка: {e!r}")
                self._sleep_interruptible(0.5)

        self.log("Стоп бота")

