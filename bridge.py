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

import keyring
from dexcom_client import DexcomClient
from libre_client import LibreClient
from constants import (
    REFRESH_INTERVAL_DEFAULT, KEYRING_SERVICE, KEYRING_PROVIDER_KEY,
    ALERT_COOLDOWN_SEC, DEFAULT_THRESH_LOW, DEFAULT_THRESH_HIGH, DEFAULT_THRESH_ALERT,
)

# ── Global state ──────────────────────────────────────────────────────────────
dexcom = DexcomClient()
libre  = LibreClient()
_provider = 'dexcom'   # 'dexcom' | 'freestyle_libre'
_refresh_interval = REFRESH_INTERVAL_DEFAULT
_refresh_lock = threading.Lock()
_stop_event = threading.Event()
_timer_thread: threading.Thread = None

# ── Alert state ───────────────────────────────────────────────────────────────
_thresh_low    = DEFAULT_THRESH_LOW
_thresh_high   = DEFAULT_THRESH_HIGH
_thresh_alert  = DEFAULT_THRESH_ALERT
_alert_enabled = True
_alert_last_notified: dict = {}
_alert_lock = threading.Lock()
_high_alert_active = False
_last_reading = None


def _active():
    return libre if _provider == 'freestyle_libre' else dexcom


# ── Protocol: stdout JSON Lines ───────────────────────────────────────────────
_write_lock = threading.Lock()

def send(msg: dict):
    """Send a JSON message to the Electron main process (thread-safe)"""
    with _write_lock:
        sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
        sys.stdout.flush()


# ── Alert logic ───────────────────────────────────────────────────────────────
def _load_thresholds():
    global _thresh_low, _thresh_high, _thresh_alert, _alert_enabled
    saved = dexcom.credentials.load_thresholds()
    if saved:
        _thresh_low, _thresh_high, _thresh_alert, _alert_enabled = saved
        log.info(f"Thresholds: low={_thresh_low} high={_thresh_high} alert={_thresh_alert} enabled={_alert_enabled}")


def _check_alerts(reading):
    global _high_alert_active
    import time
    v   = reading.value
    now = time.time()

    unit = dexcom.credentials.load_glucose_unit() or "mgdl"
    if unit == "mmol":
        val_str  = f"{v / 18.0182:.1f}"
        unit_str = "mmol/L"
    else:
        val_str  = str(v)
        unit_str = "mg/dL"

    # ── Visual alert state ─────────────────────────────────────
    new_high_alert = _alert_enabled and v > _thresh_alert
    if new_high_alert != _high_alert_active:
        _high_alert_active = new_high_alert
        log.info(f"High alert {'activated' if new_high_alert else 'cleared'}: {v} mg/dL")
        send({"type": "set_alert_ui", "active": new_high_alert})

    if not _alert_enabled:
        return

    # ── Determine alert type ───────────────────────────────────
    arrow = reading.trend_arrow
    if v < 55:
        alert_key, title, body = "very_low", "Very Low Glucose", f"Glucose {val_str} {unit_str} — Take action now!"
    elif v < _thresh_low:
        alert_key, title, body = "low", "Low Glucose", f"Glucose {val_str} {unit_str} {arrow}"
    elif v > _thresh_alert:
        alert_key, title, body = "high", "High Glucose", f"Glucose {val_str} {unit_str} {arrow}"
    elif arrow in ('↓↓',) and v < _thresh_high:
        alert_key, title, body = "drop_fast", "Glucose Dropping Fast", f"Glucose {val_str} {unit_str} {arrow}"
    else:
        return

    # ── 15-min cooldown ────────────────────────────────────────
    with _alert_lock:
        last = _alert_last_notified.get(alert_key, 0)
        if now - last < ALERT_COOLDOWN_SEC:
            return
        _alert_last_notified[alert_key] = now

    log.warning(f"[Alert:{alert_key}] {v} mg/dL — sending notification")
    send({"type": "glucose_alert", "title": title, "body": body})


# ── Refresh logic ─────────────────────────────────────────────────────────────
def _do_refresh():
    global _last_reading
    if not _refresh_lock.acquire(blocking=False):
        return
    try:
        provider = _active()
        reading = provider.get_current_reading()
        history = provider.get_history()
        if reading:
            _last_reading = reading
            data = reading.to_dict()
            data["history"] = [
                {"t": int(r.timestamp.timestamp()), "v": r.value}
                for r in history
            ]
            data["thresh_low"]    = _thresh_low
            data["thresh_high"]   = _thresh_high
            data["thresh_alert"]  = _thresh_alert
            data["alert_enabled"] = _alert_enabled
            send({"type": "glucose_data", "data": data})
            log.debug(f"Pushed glucose data: {reading.value} {reading.trend_arrow}")
            _check_alerts(reading)
        else:
            log.warning("Refresh returned empty result")
    except Exception as e:
        log.error(f"Refresh error: {e}", exc_info=True)
    finally:
        _refresh_lock.release()


def _do_startup_refresh():
    """On keychain login: first push cached store data immediately, then call API for fresh data.
    This ensures historical data is visible instantly on restart even before the API responds."""
    provider = _active()
    # Step 1: push whatever is already in the local store (fast, no API call)
    try:
        cached_history = provider.get_history_from_store()
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
        if _active().is_logged_in():
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
    global _provider, _refresh_interval, _thresh_low, _thresh_high, _thresh_alert, _alert_enabled, _last_reading
    action = cmd.get("type")
    log.debug(f"Received command: {action}")

    if action == "load_credentials":
        saved_provider = keyring.get_password(KEYRING_SERVICE, KEYRING_PROVIDER_KEY) or 'dexcom'
        _provider = saved_provider

        username, password, ous = dexcom.credentials.load()
        interval = dexcom.credentials.load_refresh_interval() or REFRESH_INTERVAL_DEFAULT
        mode = dexcom.credentials.load_display_mode() or "window"
        unit = dexcom.credentials.load_glucose_unit() or "mgdl"

        libre_email, libre_password, libre_region = libre.load_credentials()

        _load_thresholds()

        send({
            "type": "credentials",
            "provider_type": saved_provider,
            "username": username or "",
            "password": password or "",
            "has_credentials": bool(username and password) or libre.has_credentials(),
            "ous": ous,
            "interval": interval,
            "display_mode": mode,
            "unit": unit,
            "libre_email": libre_email or "",
            "libre_region": libre_region or "US",
            "libre_has_credentials": libre.has_credentials(),
            "thresh_low":    _thresh_low,
            "thresh_high":   _thresh_high,
            "thresh_alert":  _thresh_alert,
            "alert_enabled": _alert_enabled,
        })

    elif action == "login_from_keychain":
        _provider = keyring.get_password(KEYRING_SERVICE, KEYRING_PROVIDER_KEY) or 'dexcom'
        success = _active().login_from_keychain()
        send({"type": "login_result", "success": success, "from_keychain": True})
        if success:
            threading.Thread(target=_do_startup_refresh, daemon=True).start()
            _start_timer()

    elif action == "save_credentials":
        username = cmd.get("username", "").strip()
        password = cmd.get("password", "")
        ous = cmd.get("ous", False)
        success = dexcom.login(username, password, ous)
        if success:
            _provider = 'dexcom'
            keyring.set_password(KEYRING_SERVICE, KEYRING_PROVIDER_KEY, 'dexcom')
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

    elif action == "test_libre":
        email = cmd.get("email", "").strip()
        password = cmd.get("password", "")
        region = "EU" if cmd.get("is_eu") else "US"
        try:
            from pylibrelinkup import PyLibreLinkUp
            client = PyLibreLinkUp(email=email, password=password)
            client.authenticate()
            patients = client.get_patients()
            if not patients:
                raise Exception("No patients found")
            send({"type": "test_libre_result", "success": True, "message": "Connected successfully"})
        except Exception as e:
            log.warning(f"test_libre failed: {type(e).__name__}: {e}")
            send({"type": "test_libre_result", "success": False, "message": str(e)})

    elif action == "save_libre":
        email = cmd.get("email", "").strip()
        password = cmd.get("password", "")
        region = "EU" if cmd.get("is_eu") else "US"
        success = libre.login(email, password, region)
        if success:
            _provider = 'freestyle_libre'
            keyring.set_password(KEYRING_SERVICE, KEYRING_PROVIDER_KEY, 'freestyle_libre')
            _start_timer()
            threading.Thread(target=_do_refresh, daemon=True).start()
        send({"type": "save_libre_result", "success": success})

    elif action == "save_thresholds":
        def _clamp(v, lo, hi): return max(lo, min(hi, int(v)))
        _thresh_low    = _clamp(cmd.get("thresh_low",    DEFAULT_THRESH_LOW),   40,  200)
        _thresh_high   = _clamp(cmd.get("thresh_high",   DEFAULT_THRESH_HIGH),  80,  350)
        _thresh_alert  = _clamp(cmd.get("thresh_alert",  DEFAULT_THRESH_ALERT), 100, 400)
        _alert_enabled = bool(cmd.get("alert_enabled", True))
        dexcom.credentials.save_thresholds(_thresh_low, _thresh_high, _thresh_alert, _alert_enabled)
        if _last_reading:
            _check_alerts(_last_reading)
        send({"type": "save_thresholds_result", "success": True})

    elif action == "save_display":
        interval = cmd.get("interval", REFRESH_INTERVAL_DEFAULT)
        mode = cmd.get("display_mode", "window")
        unit = cmd.get("unit", "mgdl")
        dexcom.credentials.save_refresh_interval(interval)
        dexcom.credentials.save_display_mode(mode)
        dexcom.credentials.save_glucose_unit(unit)
        _refresh_interval = interval
        # Save thresholds if included in the same message
        if "thresh_low" in cmd or "thresh_alert" in cmd:
            def _clamp(v, lo, hi): return max(lo, min(hi, int(v)))
            _thresh_low    = _clamp(cmd.get("thresh_low",    DEFAULT_THRESH_LOW),   40,  200)
            _thresh_high   = _clamp(cmd.get("thresh_high",   DEFAULT_THRESH_HIGH),  80,  350)
            _thresh_alert  = _clamp(cmd.get("thresh_alert",  DEFAULT_THRESH_ALERT), 100, 400)
            _alert_enabled = bool(cmd.get("alert_enabled", True))
            dexcom.credentials.save_thresholds(_thresh_low, _thresh_high, _thresh_alert, _alert_enabled)
            if _last_reading:
                _check_alerts(_last_reading)
        _start_timer()
        send({"type": "save_display_result", "success": True})

    elif action == "force_refresh":
        threading.Thread(target=_do_refresh, daemon=True).start()

    elif action == "logout":
        _active().logout()
        _provider = 'dexcom'
        try:
            keyring.delete_password(KEYRING_SERVICE, KEYRING_PROVIDER_KEY)
        except Exception:
            pass
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
