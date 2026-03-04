import threading
import time
from typing import Optional, List
from datetime import datetime

import keyring
from pydexcom import Dexcom
from pydexcom.const import Region
import pydexcom.dexcom as _pydexcom_dexcom
# pydexcom bug: HEADERS uses incorrect Accept-Encoding, causing request timeouts under proxies like Zscaler
# Must patch pydexcom.dexcom.HEADERS (not pydexcom.const), because dexcom.py uses "from .const import HEADERS" (already bound)
_pydexcom_dexcom.HEADERS = {"Accept": "application/json", "Content-Type": "application/json"}

from models import GlucoseReading
from constants import (
    KEYRING_SERVICE, KEYRING_USERNAME_KEY, KEYRING_PASSWORD_KEY,
    KEYRING_REGION_KEY, HISTORY_MINUTES, HISTORY_MAX_POINTS,
)
from local_store import LocalStore
from logger import get_logger

log = get_logger("dexcom")


def _is_rate_limit_error(e: Exception) -> bool:
    """Detect if the error is an API rate limit error (429 Too Many Requests).
    pydexcom wraps 429 as 'Invalid or malformed JSON', so we need to check the raw response body in the exception chain."""
    text = str(e).lower()
    if "too many" in text or "429" in text:
        return True
    # Check exception chain (pydexcom's JSONDecodeError doc contains the raw response body)
    ctx = e.__context__ or e.__cause__
    if ctx:
        doc = getattr(ctx, 'doc', '') or ''
        if "too many" in doc.lower():
            return True
    return False


def _is_session_error(e: Exception) -> bool:
    """Detect if the error is a session expiry error (JSON/malformed/session), but not a rate limit error"""
    if _is_rate_limit_error(e):
        return False
    text = str(e).lower()
    return "json" in text or "malformed" in text or "session" in text


class CredentialManager:
    """Keyring credential manager"""

    def save(self, username: str, password: str, ous: bool = False):
        keyring.set_password(KEYRING_SERVICE, KEYRING_USERNAME_KEY, username)
        keyring.set_password(KEYRING_SERVICE, KEYRING_PASSWORD_KEY, password)
        keyring.set_password(KEYRING_SERVICE, KEYRING_REGION_KEY, "ous" if ous else "us")

    def load(self) -> tuple[Optional[str], Optional[str], bool]:
        username = keyring.get_password(KEYRING_SERVICE, KEYRING_USERNAME_KEY)
        password = keyring.get_password(KEYRING_SERVICE, KEYRING_PASSWORD_KEY)
        region = keyring.get_password(KEYRING_SERVICE, KEYRING_REGION_KEY)
        ous = (region == "ous")
        return username, password, ous

    def clear(self):
        for key in [KEYRING_USERNAME_KEY, KEYRING_PASSWORD_KEY, KEYRING_REGION_KEY]:
            try:
                keyring.delete_password(KEYRING_SERVICE, key)
            except Exception:
                pass

    def has_credentials(self) -> bool:
        username, password, _ = self.load()
        return bool(username and password)

    def save_gemini_key(self, api_key: str):
        from constants import KEYRING_GEMINI_KEY
        keyring.set_password(KEYRING_SERVICE, KEYRING_GEMINI_KEY, api_key)

    def load_gemini_key(self) -> Optional[str]:
        from constants import KEYRING_GEMINI_KEY
        return keyring.get_password(KEYRING_SERVICE, KEYRING_GEMINI_KEY)

    def save_refresh_interval(self, seconds: int):
        from constants import KEYRING_REFRESH_KEY
        keyring.set_password(KEYRING_SERVICE, KEYRING_REFRESH_KEY, str(seconds))

    def load_refresh_interval(self) -> Optional[int]:
        from constants import KEYRING_REFRESH_KEY
        val = keyring.get_password(KEYRING_SERVICE, KEYRING_REFRESH_KEY)
        return int(val) if val else None

    def save_display_mode(self, mode: str):
        from constants import KEYRING_DISPLAY_MODE_KEY
        keyring.set_password(KEYRING_SERVICE, KEYRING_DISPLAY_MODE_KEY, mode)

    def load_display_mode(self) -> Optional[str]:
        from constants import KEYRING_DISPLAY_MODE_KEY
        return keyring.get_password(KEYRING_SERVICE, KEYRING_DISPLAY_MODE_KEY)

    def save_glucose_unit(self, unit: str):
        from constants import KEYRING_UNIT_KEY
        keyring.set_password(KEYRING_SERVICE, KEYRING_UNIT_KEY, unit)

    def load_glucose_unit(self) -> Optional[str]:
        from constants import KEYRING_UNIT_KEY
        return keyring.get_password(KEYRING_SERVICE, KEYRING_UNIT_KEY)

    def save_comparison(self, mode: str):
        from constants import KEYRING_COMPARISON_KEY
        keyring.set_password(KEYRING_SERVICE, KEYRING_COMPARISON_KEY, mode)

    def load_comparison(self) -> Optional[str]:
        from constants import KEYRING_COMPARISON_KEY
        return keyring.get_password(KEYRING_SERVICE, KEYRING_COMPARISON_KEY)

    def save_thresholds(self, low: int, high: int, alert: int, alert_enabled: bool):
        from constants import KEYRING_THRESH_LOW, KEYRING_THRESH_HIGH, KEYRING_THRESH_ALERT, KEYRING_ALERT_ENABLED
        keyring.set_password(KEYRING_SERVICE, KEYRING_THRESH_LOW,    str(low))
        keyring.set_password(KEYRING_SERVICE, KEYRING_THRESH_HIGH,   str(high))
        keyring.set_password(KEYRING_SERVICE, KEYRING_THRESH_ALERT,  str(alert))
        keyring.set_password(KEYRING_SERVICE, KEYRING_ALERT_ENABLED, "1" if alert_enabled else "0")

    def load_thresholds(self) -> Optional[tuple]:
        """Returns (low, high, alert, alert_enabled) or None if not saved."""
        from constants import (KEYRING_THRESH_LOW, KEYRING_THRESH_HIGH,
                               KEYRING_THRESH_ALERT, KEYRING_ALERT_ENABLED,
                               DEFAULT_THRESH_LOW, DEFAULT_THRESH_HIGH, DEFAULT_THRESH_ALERT)
        try:
            low   = int(keyring.get_password(KEYRING_SERVICE, KEYRING_THRESH_LOW)   or DEFAULT_THRESH_LOW)
            high  = int(keyring.get_password(KEYRING_SERVICE, KEYRING_THRESH_HIGH)  or DEFAULT_THRESH_HIGH)
            alert = int(keyring.get_password(KEYRING_SERVICE, KEYRING_THRESH_ALERT) or DEFAULT_THRESH_ALERT)
            raw   = keyring.get_password(KEYRING_SERVICE, KEYRING_ALERT_ENABLED)
            alert_enabled = (raw != "0")  # default True when not saved
            return low, high, alert, alert_enabled
        except Exception:
            return None


def _parse_reading(bg) -> Optional[GlucoseReading]:
    """Convert a pydexcom BG object to a GlucoseReading"""
    if bg is None:
        return None
    try:
        return GlucoseReading(
            value=bg.value,
            trend_arrow=bg.trend_arrow,
            trend_description=bg.trend_description,
            timestamp=bg.datetime,
        )
    except Exception:
        return None


class DexcomClient:
    """Thread-safe pydexcom wrapper"""

    _RELOGIN_COOLDOWN = 60  # Minimum interval between re-logins (seconds) to prevent rate limiting

    def __init__(self):
        self._dexcom: Optional[Dexcom] = None
        self._lock = threading.Lock()
        self.credentials = CredentialManager()
        self._store: Optional[LocalStore] = None
        self._last_relogin_time: float = 0.0

    def _relogin_within_lock(self) -> bool:
        """Rebuild Dexcom connection while holding the lock.
        Skips if within the 60s cooldown period to avoid triggering rate limits with repeated rapid requests."""
        now = time.time()
        if now - self._last_relogin_time < self._RELOGIN_COOLDOWN:
            log.debug(f"Re-login on cooldown (only one login per {self._RELOGIN_COOLDOWN}s), skipping")
            return False
        username, password, ous = self.credentials.load()
        if not username or not password:
            return False
        try:
            log.info("Session expired, rebuilding connection...")
            self._dexcom = Dexcom(
                username=username, password=password,
                region=Region.OUS if ous else Region.US,
            )
            self._last_relogin_time = time.time()
            log.info("Re-login successful")
            return True
        except Exception as e:
            log.error(f"Re-login failed: {e}")
            self._dexcom = None
            return False

    def login(self, username: str, password: str, ous: bool = False) -> bool:
        """Login and save credentials to keyring (pydexcom __init__ performs authentication internally, raises on bad credentials)"""
        log.info(f"Logging into Dexcom: user={username}, ous={ous}")
        with self._lock:
            try:
                dex = Dexcom(username=username, password=password, region=Region.OUS if ous else Region.US)
                self._dexcom = dex
                self.credentials.save(username, password, ous)
                self._store = LocalStore(username)
                log.info("Dexcom login successful")
                return True
            except Exception as e:
                log.error(f"Dexcom login failed: {e}", exc_info=True)
                self._dexcom = None
                return False

    def refresh_session(self) -> bool:
        """Rebuild session from keychain without a test request (used for auto-recovery)"""
        with self._lock:
            return self._relogin_within_lock()

    def login_from_keychain(self) -> bool:
        """Read credentials from keyring and login"""
        username, password, ous = self.credentials.load()
        if not username or not password:
            log.warning("No credentials found in keychain")
            return False
        log.info("Auto-login from keychain")
        return self.login(username, password, ous)

    def logout(self):
        """Logout and clear keyring"""
        log.info("Logging out, clearing credentials")
        with self._lock:
            self._dexcom = None
            self._store = None
        self.credentials.clear()

    def is_logged_in(self) -> bool:
        return self._dexcom is not None

    def get_current_reading(self) -> Optional[GlucoseReading]:
        with self._lock:
            if not self._dexcom:
                return None
            try:
                bg = self._dexcom.get_current_glucose_reading()
                reading = _parse_reading(bg)
                if reading:
                    log.debug(f"Current glucose: {reading.value} {reading.trend_arrow} ({reading.age_text})")
                    if self._store:
                        self._store.upsert([reading])
                else:
                    log.warning("get_current_reading returned empty")
                return reading
            except Exception as e:
                if _is_rate_limit_error(e):
                    log.warning(f"API rate limited, waiting for next refresh (not rebuilding session)")
                elif _is_session_error(e):
                    log.warning(f"API response error ({e}), rebuilding session and waiting for next refresh")
                    self._relogin_within_lock()
                else:
                    log.error(f"get_current_reading error: {e}", exc_info=True)
                return None

    def get_history_from_store(self, minutes: int = HISTORY_MINUTES) -> List[GlucoseReading]:
        """Read history from local store only (no API call). Returns empty list if store unavailable."""
        with self._lock:
            if self._store:
                return self._store.load(minutes)
            return []

    def get_history(self, minutes: int = HISTORY_MINUTES) -> List[GlucoseReading]:
        with self._lock:
            if not self._dexcom:
                return []
            try:
                readings = self._dexcom.get_glucose_readings(minutes=minutes, max_count=HISTORY_MAX_POINTS)
                result = []
                for bg in (readings or []):
                    r = _parse_reading(bg)
                    if r:
                        result.append(r)
                if result:
                    t0 = result[0].timestamp.strftime('%m-%d %H:%M') if result else '-'
                    t1 = result[-1].timestamp.strftime('%m-%d %H:%M') if result else '-'
                    log.debug(f"History glucose (API): {len(result)} records, {t0} ~ {t1}")
                else:
                    log.debug("History glucose (API): 0 records")
                if self._store:
                    if result:
                        self._store.upsert(result)
                    # Read from local store, which includes accumulated historical data
                    result = self._store.load(minutes)
                return result
            except Exception as e:
                if _is_rate_limit_error(e):
                    log.warning(f"get_history API rate limited, waiting for next refresh (not rebuilding session)")
                elif _is_session_error(e):
                    log.warning(f"get_history API response error ({e}), rebuilding session and waiting for next refresh")
                    self._relogin_within_lock()
                else:
                    log.error(f"get_history error: {e}", exc_info=True)
                # Return local cache on API failure
                if self._store:
                    cached = self._store.load(minutes)
                    if cached:
                        log.info(f"Returning {len(cached)} cached records from local store")
                        return cached
                return []
