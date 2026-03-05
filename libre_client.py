import threading
from datetime import datetime, timezone
from typing import Optional, List, Tuple

import keyring

from models import GlucoseReading
from constants import (
    KEYRING_SERVICE,
    KEYRING_LIBRE_EMAIL_KEY, KEYRING_LIBRE_PASSWORD_KEY, KEYRING_LIBRE_REGION_KEY,
    HISTORY_MINUTES,
)
from local_store import LocalStore
from logger import get_logger

log = get_logger("libre")

# Libre trend numeric → arrow / description
LIBRE_TREND_ARROWS = {1: '↓↓', 2: '↓', 3: '→', 4: '↑', 5: '↑↑'}
LIBRE_TREND_DESC   = {
    1: 'Double Down', 2: 'Single Down', 3: 'Flat',
    4: 'Single Up',   5: 'Double Up',
}


class LibreClient:
    """Thread-safe pylibrelinkup wrapper, duck-typed to match DexcomClient's public interface."""

    def __init__(self):
        self._client = None   # PyLibreLinkUp instance
        self._patient = None
        self._lock = threading.Lock()
        self._store: Optional[LocalStore] = None

    # ─── Credential management ─────────────────────────────────

    def save_credentials(self, email: str, password: str, region: str = "US"):
        keyring.set_password(KEYRING_SERVICE, KEYRING_LIBRE_EMAIL_KEY,    email)
        keyring.set_password(KEYRING_SERVICE, KEYRING_LIBRE_PASSWORD_KEY, password)
        keyring.set_password(KEYRING_SERVICE, KEYRING_LIBRE_REGION_KEY,   region)

    def load_credentials(self) -> Tuple[Optional[str], Optional[str], Optional[str]]:
        email    = keyring.get_password(KEYRING_SERVICE, KEYRING_LIBRE_EMAIL_KEY)
        password = keyring.get_password(KEYRING_SERVICE, KEYRING_LIBRE_PASSWORD_KEY)
        region   = keyring.get_password(KEYRING_SERVICE, KEYRING_LIBRE_REGION_KEY) or "US"
        return email, password, region

    def has_credentials(self) -> bool:
        email, password, _ = self.load_credentials()
        return bool(email and password)

    def clear_credentials(self):
        for key in [KEYRING_LIBRE_EMAIL_KEY, KEYRING_LIBRE_PASSWORD_KEY, KEYRING_LIBRE_REGION_KEY]:
            try:
                keyring.delete_password(KEYRING_SERVICE, key)
            except Exception:
                pass

    # ─── Session management ────────────────────────────────────

    def login(self, email: str, password: str, region: str = "US") -> bool:
        log.info(f"Logging into FreeStyle Libre: email={email}, region={region}")
        with self._lock:
            try:
                from pylibrelinkup import PyLibreLinkUp
                client = PyLibreLinkUp(email=email, password=password)
                client.authenticate()
                patients = client.get_patients()
                if not patients:
                    log.error("Libre login: no patients found")
                    return False
                self._client  = client
                self._patient = patients[0]
                self._store   = LocalStore(email)
                self.save_credentials(email, password, region)
                log.info(f"Libre login successful, patient: {self._patient}")
                return True
            except Exception as e:
                log.error(f"Libre login failed: {e}", exc_info=True)
                self._client  = None
                self._patient = None
                return False

    def login_from_keychain(self) -> bool:
        email, password, region = self.load_credentials()
        if not email or not password:
            log.warning("No Libre credentials in keychain")
            return False
        log.info("Auto-login Libre from keychain")
        return self.login(email, password, region or "US")

    def is_logged_in(self) -> bool:
        return self._client is not None and self._patient is not None

    def logout(self):
        log.info("Libre logout, clearing credentials")
        with self._lock:
            self._client  = None
            self._patient = None
            self._store   = None
        self.clear_credentials()

    # ─── Data fetching ─────────────────────────────────────────

    def get_current_reading(self) -> Optional[GlucoseReading]:
        with self._lock:
            if not self._client or not self._patient:
                return None
            try:
                self._reauth_if_needed_within_lock()
                data = self._client.latest(self._patient)
                reading = self._convert_reading(data)
                if reading:
                    log.debug(f"Libre current: {reading.value} {reading.trend_arrow} ({reading.age_text})")
                    if self._store:
                        self._store.upsert([reading])
                else:
                    log.warning("Libre get_current_reading returned empty")
                return reading
            except Exception as e:
                log.error(f"Libre get_current_reading error: {e}", exc_info=True)
                return None

    def get_history(self, minutes: int = HISTORY_MINUTES) -> List[GlucoseReading]:
        with self._lock:
            if not self._client or not self._patient:
                return []
            try:
                self._reauth_if_needed_within_lock()
                # Use logbook (2 weeks) for >720 min, graph (12h) otherwise
                if minutes > 720:
                    raw_list = self._client.logbook(self._patient)
                else:
                    raw_list = self._client.graph(self._patient)
                result = []
                for item in (raw_list or []):
                    r = self._convert_reading(item)
                    if r:
                        result.append(r)
                if result:
                    t0 = result[0].timestamp.strftime('%m-%d %H:%M')
                    t1 = result[-1].timestamp.strftime('%m-%d %H:%M')
                    log.debug(f"Libre history (API): {len(result)} records, {t0} ~ {t1}")
                else:
                    log.debug("Libre history (API): 0 records")
                if self._store:
                    if result:
                        self._store.upsert(result)
                    result = self._store.load(minutes)
                return result
            except Exception as e:
                log.error(f"Libre get_history error: {e}", exc_info=True)
                if self._store:
                    cached = self._store.load(minutes)
                    if cached:
                        log.info(f"Returning {len(cached)} cached Libre records from local store")
                        return cached
                return []

    def get_history_from_store(self, minutes: int = HISTORY_MINUTES) -> List[GlucoseReading]:
        with self._lock:
            if self._store:
                return self._store.load(minutes)
            return []

    # ─── Internal helpers ──────────────────────────────────────

    def _convert_reading(self, libre_reading) -> Optional[GlucoseReading]:
        try:
            trend_num = getattr(libre_reading, 'trend', None)
            if trend_num is None:
                # Some API responses wrap it in a TrendArrow enum-like object
                trend_obj = getattr(libre_reading, 'trend_arrow', None)
                if trend_obj is not None:
                    trend_num = int(trend_obj)
            arrow = LIBRE_TREND_ARROWS.get(trend_num, '→')
            desc  = LIBRE_TREND_DESC.get(trend_num, 'Flat')

            ts = getattr(libre_reading, 'timestamp', None)
            if ts is None:
                ts = datetime.now(timezone.utc)
            elif ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)

            value = int(getattr(libre_reading, 'value', 0))
            return GlucoseReading(
                value=value,
                trend_arrow=arrow,
                trend_description=desc,
                timestamp=ts,
            )
        except Exception as e:
            log.warning(f"Libre _convert_reading failed: {e}")
            return None

    def _reauth_if_needed_within_lock(self):
        """Called while holding self._lock. Re-authenticates if session appears expired."""
        # pylibrelinkup raises AuthenticationError on session expiry; we catch it at the call site.
        # This method exists as a hook for future proactive reauth logic.
        pass
