"""
CGM Python sidecar for Electron.
Communication protocol: JSON Lines over stdin/stdout.
stdout is dedicated to the JSON protocol; all logging goes to stderr / file.
"""
import sys
import os
import json
import threading
import logging

# Windows stdout defaults to cp1252 which can't encode trend arrows (→ etc.)
# Reconfigure to UTF-8 so JSON protocol works correctly
if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')

# ── Fix logger: disable stdout handler to avoid polluting the JSON protocol ──────────────────────
# Must be set before importing logger, otherwise logger._setup() will have already added StreamHandler(stdout)
import logging.handlers as _lh

# Importing logger triggers logger.py's _setup(); remove the stdout handler afterwards
sys.path.insert(0, os.path.dirname(__file__))

from logger import get_logger as _get_logger, logger as _root_logger

# Remove all handlers writing to stdout
for _h in list(_root_logger.handlers):
    if isinstance(_h, logging.StreamHandler) and _h.stream is sys.stdout:
        _root_logger.removeHandler(_h)

# Add stderr handler (Electron captures stderr and displays it separately)
_stderr_handler = logging.StreamHandler(sys.stderr)
_stderr_handler.setLevel(logging.DEBUG)
_stderr_handler.setFormatter(logging.Formatter(
    "%(asctime)s  %(levelname)-8s  %(name)-20s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
))
_root_logger.addHandler(_stderr_handler)

log = _get_logger("bridge")

# ── SSL / Zscaler fix (macOS; skip on Windows) ─────────────────────────────────
try:
    import truststore
    truststore.inject_into_ssl()
    log.info("truststore injected successfully")
except Exception:
    log.info("truststore unavailable, using system default SSL")

from dexcom_client import DexcomClient
from constants import REFRESH_INTERVAL_DEFAULT

# ── Global state ──────────────────────────────────────────────────────────────
dexcom = DexcomClient()
_refresh_interval = REFRESH_INTERVAL_DEFAULT
_refresh_lock = threading.Lock()
_stop_event = threading.Event()
_timer_thread: threading.Thread = None


# ── Protocol: stdout JSON Lines ───────────────────────────────────────────────
_write_lock = threading.Lock()

def send(msg: dict):
    """Send a JSON message to the Electron main process (thread-safe)"""
    with _write_lock:
        sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
        sys.stdout.flush()


# ── Refresh logic ─────────────────────────────────────────────────────────────
def _do_refresh():
    if not _refresh_lock.acquire(blocking=False):
        return
    try:
        reading = dexcom.get_current_reading()
        history = dexcom.get_history()
        if reading:
            data = reading.to_dict()
            data["history"] = [
                {"t": int(r.timestamp.timestamp()), "v": r.value}
                for r in history
            ]
            send({"type": "glucose_data", "data": data})
            log.debug(f"Pushed glucose data: {reading.value} {reading.trend_arrow}")
        else:
            log.warning("Refresh returned empty result")
    except Exception as e:
        log.error(f"Refresh error: {e}", exc_info=True)
    finally:
        _refresh_lock.release()


def _do_startup_refresh():
    """On keychain login: first push cached store data immediately, then call API for fresh data.
    This ensures historical data is visible instantly on restart even before the API responds."""
    # Step 1: push whatever is already in the local store (fast, no API call)
    try:
        cached_history = dexcom.get_history_from_store()
        if cached_history:
            latest = cached_history[-1]
            data = latest.to_dict()
            data["history"] = [
                {"t": int(r.timestamp.timestamp()), "v": r.value}
                for r in cached_history
            ]
            send({"type": "glucose_data", "data": data})
            log.info(f"Pushed {len(cached_history)} cached store records before API call")
    except Exception as e:
        log.warning(f"Store pre-load failed: {e}")

    # Step 2: full API refresh (updates store and sends live data)
    _do_refresh()


# ── Timer ────────────────────────────────────────────────────────────────────
def _timer_loop(interval: int, stop: threading.Event):
    while not stop.wait(interval):
        if dexcom.is_logged_in():
            threading.Thread(target=_do_refresh, daemon=True).start()


def _start_timer():
    global _timer_thread, _stop_event, _refresh_interval
    _stop_event.set()  # Stop old timer
    _stop_event = threading.Event()
    saved = dexcom.credentials.load_refresh_interval()
    if saved:
        _refresh_interval = saved
    t = threading.Thread(
        target=_timer_loop,
        args=(_refresh_interval, _stop_event),
        daemon=True,
    )
    t.start()
    _timer_thread = t
    log.info(f"Timer started, interval {_refresh_interval}s")


def _stop_timer():
    _stop_event.set()


# ── Command handling ─────────────────────────────────────────────────────────
def handle(cmd: dict):
    action = cmd.get("type")
    log.debug(f"Received command: {action}")

    if action == "load_credentials":
        username, password, ous = dexcom.credentials.load()
        interval = dexcom.credentials.load_refresh_interval() or REFRESH_INTERVAL_DEFAULT
        mode = dexcom.credentials.load_display_mode() or "window"
        unit = dexcom.credentials.load_glucose_unit() or "mgdl"
        send({
            "type": "credentials",
            "username": username or "",
            "password": password or "",
            "has_credentials": bool(username and password),
            "ous": ous,
            "interval": interval,
            "display_mode": mode,
            "unit": unit,
        })

    elif action == "login_from_keychain":
        success = dexcom.login_from_keychain()
        send({"type": "login_result", "success": success, "from_keychain": True})
        if success:
            threading.Thread(target=_do_startup_refresh, daemon=True).start()
            _start_timer()

    elif action == "save_credentials":
        username = cmd.get("username", "").strip()
        password = cmd.get("password", "")
        ous = cmd.get("ous", False)
        success = dexcom.login(username, password, ous)
        send({"type": "save_credentials_result", "success": success})
        if success:
            threading.Thread(target=_do_refresh, daemon=True).start()
            _start_timer()

    elif action == "test_credentials":
        username = cmd.get("username", "").strip()
        password = cmd.get("password", "")
        ous = cmd.get("ous", False)
        try:
            from pydexcom import Dexcom
            from pydexcom.const import Region
            import pydexcom.dexcom as _pydex
            _pydex.HEADERS = {"Accept": "application/json", "Content-Type": "application/json"}
            dex = Dexcom(username=username, password=password,
                         region=Region.OUS if ous else Region.US)
            dex.get_current_glucose_reading()
            send({"type": "test_credentials_result", "success": True,
                  "message": "Connected successfully"})
        except Exception as e:
            log.warning(f"test_credentials failed: {type(e).__name__}: {e}")
            send({"type": "test_credentials_result", "success": False, "message": str(e)})

    elif action == "save_display":
        interval = cmd.get("interval", REFRESH_INTERVAL_DEFAULT)
        mode = cmd.get("display_mode", "window")
        unit = cmd.get("unit", "mgdl")
        dexcom.credentials.save_refresh_interval(interval)
        dexcom.credentials.save_display_mode(mode)
        dexcom.credentials.save_glucose_unit(unit)
        global _refresh_interval
        _refresh_interval = interval
        _start_timer()
        send({"type": "save_display_result", "success": True})

    elif action == "force_refresh":
        threading.Thread(target=_do_refresh, daemon=True).start()

    elif action == "logout":
        dexcom.logout()
        _stop_timer()
        send({"type": "logout_done"})

    else:
        log.warning(f"Unknown command: {action}")


# ── Main loop: read stdin ──────────────────────────────────────────────────
def main():
    log.info("bridge.py started, waiting for Electron commands")
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            cmd = json.loads(raw)
            threading.Thread(target=handle, args=(cmd,), daemon=True).start()
        except json.JSONDecodeError as e:
            log.error(f"JSON parse error: {e} | raw={raw!r}")
        except Exception as e:
            log.error(f"Command handling error: {e}", exc_info=True)
            send({"type": "error", "message": str(e)})
    log.info("stdin closed, bridge.py exiting")


if __name__ == "__main__":
    main()
