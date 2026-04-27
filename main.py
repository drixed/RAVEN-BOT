import ctypes
import os
from pathlib import Path
import subprocess


def _enable_dpi_awareness() -> None:
    """
    Make the process DPI-aware so WinAPI window coordinates match physical pixels.
    This is critical for correct window screenshots/cropping with MSS on scaled displays.
    Best-effort: works across Windows 7-11.
    """
    try:
        # Windows 8.1+ (per-monitor DPI aware)
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # type: ignore[attr-defined]
        return
    except Exception:
        pass
    try:
        # Windows Vista+ (system DPI aware)
        ctypes.windll.user32.SetProcessDPIAware()  # type: ignore[attr-defined]
    except Exception:
        pass


from ui import run_app  # noqa: E402


def _kill_previous_instance() -> None:
    """
    Ensure single-instance by killing previous PID (best-effort).
    This helps during rapid restarts/dev and avoids locked dist folders.
    """
    pid_path = Path("app.pid")
    cur = int(os.getpid())
    try:
        old_s = pid_path.read_text(encoding="utf-8").strip()
        old = int(old_s) if old_s else 0
    except Exception:
        old = 0

    if old and old != cur:
        # Best-effort: terminate old pid if it still exists.
        try:
            subprocess.run(["taskkill", "/PID", str(int(old)), "/T", "/F"], capture_output=True, text=True)
        except Exception:
            pass

    try:
        pid_path.write_text(str(cur), encoding="utf-8")
    except Exception:
        pass


def main() -> None:
    _enable_dpi_awareness()
    _kill_previous_instance()
    run_app()


if __name__ == "__main__":
    main()

