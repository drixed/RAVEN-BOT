import ctypes


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


def main() -> None:
    _enable_dpi_awareness()
    run_app()


if __name__ == "__main__":
    main()

