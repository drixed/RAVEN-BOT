from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen


@dataclass(frozen=True)
class ReleaseAsset:
    name: str
    url: str
    size: int


@dataclass(frozen=True)
class LatestRelease:
    tag: str
    name: str
    assets: list[ReleaseAsset]


_SEMVER_RE = re.compile(r"v?(\d+)\.(\d+)\.(\d+)")


def parse_semver(s: str) -> tuple[int, int, int] | None:
    m = _SEMVER_RE.search((s or "").strip())
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def is_newer(tag: str, current_version: str) -> bool:
    t = parse_semver(tag)
    c = parse_semver(current_version)
    if not t or not c:
        # If we can't parse, don't auto-update (safe default)
        return False
    return t > c


def _http_get_json(url: str, *, timeout_s: float = 15.0) -> dict[str, Any]:
    req = Request(
        url,
        headers={
            "User-Agent": "RAVEN-BOT-Updater",
            "Accept": "application/vnd.github+json",
        },
        method="GET",
    )
    with urlopen(req, timeout=timeout_s) as resp:  # nosec - user-controlled URL not used
        raw = resp.read()
    return json.loads(raw.decode("utf-8", errors="replace"))


def get_latest_release(owner: str, repo: str) -> LatestRelease:
    data = _http_get_json(f"https://api.github.com/repos/{owner}/{repo}/releases/latest")
    tag = str(data.get("tag_name") or "").strip()
    name = str(data.get("name") or "").strip()
    assets: list[ReleaseAsset] = []
    for a in (data.get("assets") or []):
        try:
            assets.append(
                ReleaseAsset(
                    name=str(a.get("name") or ""),
                    url=str(a.get("browser_download_url") or ""),
                    size=int(a.get("size") or 0),
                )
            )
        except Exception:
            continue
    return LatestRelease(tag=tag, name=name, assets=assets)


def find_asset(release: LatestRelease, *, name: str) -> ReleaseAsset | None:
    want = (name or "").strip().lower()
    for a in release.assets:
        if a.name.strip().lower() == want:
            return a
    return None


def download_file(url: str, dest: Path, *, timeout_s: float = 60.0) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = Request(url, headers={"User-Agent": "RAVEN-BOT-Updater"}, method="GET")
    with urlopen(req, timeout=timeout_s) as resp:  # nosec - url is trusted (GitHub release asset)
        with open(dest, "wb") as f:
            shutil.copyfileobj(resp, f)


def _write_update_bat(*, app_dir: Path, extracted_dir: Path, exe_name: str) -> Path:
    """
    Create a bat that:
    - waits a bit so current app can exit
    - copies extracted files over app_dir (robocopy)
    - relaunches exe
    """
    bat = app_dir / "update.bat"
    # robocopy exit codes: 0/1 are success; others may still be partial.
    content = rf"""@echo off
setlocal enabledelayedexpansion
cd /d "{app_dir}"

echo [UPDATER] Waiting for app to exit...
timeout /t 2 /nobreak >nul

echo [UPDATER] Copying files...
robocopy "{extracted_dir}" "{app_dir}" /E /NFL /NDL /NJH /NJS /NP /R:3 /W:1
set RC=%ERRORLEVEL%

echo [UPDATER] Robocopy exit code: %RC%

echo [UPDATER] Starting app...
start "" "{app_dir / exe_name}"

echo [UPDATER] Done.
exit /b 0
"""
    bat.write_text(content, encoding="utf-8", errors="ignore")
    return bat


def get_app_dir() -> Path:
    # Frozen exe: sys.executable points to .../RAVEN_BOT.exe
    # Source run: use project working dir.
    try:
        if getattr(sys, "frozen", False):
            return Path(sys.executable).resolve().parent
    except Exception:
        pass
    return Path(os.getcwd()).resolve()


def stage_update_from_zip(zip_path: Path, *, exe_name: str = "RAVEN_BOT.exe") -> Path:
    """
    Extract zip to a temporary directory and create update.bat in app dir.
    Returns path to update.bat.
    """
    app_dir = get_app_dir()
    tmp_root = Path(tempfile.gettempdir()) / "RAVEN_BOT_update"
    tmp_root.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    extracted = tmp_root / f"extracted_{stamp}"
    extracted.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(zip_path, "r") as z:
        z.extractall(extracted)

    # Many zips are packed as "RAVEN_BOT/<files...>" – if so, use that inner dir.
    inner = extracted / "RAVEN_BOT"
    extracted_dir = inner if inner.exists() else extracted

    return _write_update_bat(app_dir=app_dir, extracted_dir=extracted_dir, exe_name=exe_name)


def run_bat_and_exit(bat_path: Path) -> None:
    """
    Start updater bat and exit current process. Caller should close UI first.
    """
    try:
        subprocess.Popen(["cmd", "/c", "start", "", str(bat_path)], cwd=str(bat_path.parent))  # noqa: S603,S607
    except Exception:
        # fallback
        os.startfile(str(bat_path))  # type: ignore[attr-defined]
    raise SystemExit(0)

