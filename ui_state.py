"""Lightweight JSON state file for persisting UI preferences (window position, last range)."""
import json
import pathlib
import threading

_STATE_DIR = pathlib.Path.home() / "Library" / "Application Support" / "CGMMonitor"
_STATE_FILE = _STATE_DIR / "ui_state.json"
_lock = threading.Lock()


def _load() -> dict:
    try:
        return json.loads(_STATE_FILE.read_text())
    except Exception:
        return {}


def _save(data: dict):
    try:
        _STATE_DIR.mkdir(parents=True, exist_ok=True)
        _STATE_FILE.write_text(json.dumps(data))
    except Exception:
        pass


def save_window_pos(x: float, y: float):
    with _lock:
        d = _load()
        d["win_x"] = x
        d["win_y"] = y
        _save(d)


def load_window_pos() -> tuple:
    """Returns (x, y) or (None, None) if not saved."""
    with _lock:
        d = _load()
        x = d.get("win_x")
        y = d.get("win_y")
        if x is not None and y is not None:
            return float(x), float(y)
        return None, None


def save_range(minutes: int):
    with _lock:
        d = _load()
        d["last_range"] = minutes
        _save(d)


def load_range() -> int | None:
    with _lock:
        d = _load()
        return d.get("last_range")


def save_provider_type(provider: str):
    """Save the active provider type: 'dexcom' or 'freestyle_libre'."""
    with _lock:
        d = _load()
        d["provider"] = provider
        _save(d)


def load_provider_type() -> str:
    """Load the active provider type. Defaults to 'dexcom'."""
    with _lock:
        d = _load()
        return d.get("provider", "dexcom")
