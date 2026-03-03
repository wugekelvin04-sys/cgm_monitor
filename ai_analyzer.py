import time
import threading
from typing import Optional, List, Callable

from models import GlucoseReading
from constants import AI_CACHE_SECONDS, KEYRING_SERVICE, KEYRING_GEMINI_KEY
from logger import get_logger

log = get_logger("ai")


class GeminiAnalyzer:
    """Gemini glucose analyzer — streaming output, 5-minute cache."""

    MODEL = "gemini-2.5-flash-lite"

    def __init__(self):
        self._api_key: Optional[str] = None
        self._cache_text: Optional[str] = None
        self._cache_time: float = 0
        self._lock = threading.Lock()

    def set_api_key(self, api_key: str):
        self._api_key = api_key

    def load_api_key_from_keyring(self) -> bool:
        try:
            import keyring
            key = keyring.get_password(KEYRING_SERVICE, KEYRING_GEMINI_KEY)
            if key:
                self._api_key = key
                log.info("Gemini API key loaded from keychain")
                return True
        except Exception as e:
            log.warning(f"Failed to load Gemini key: {e}")
        return False

    def has_api_key(self) -> bool:
        return bool(self._api_key)

    def test_api_key(self, api_key: str) -> tuple[bool, str]:
        """Test whether the API key is valid. Returns (success, message)."""
        try:
            from google import genai
            client = genai.Client(api_key=api_key)
            response = client.models.generate_content(
                model=self.MODEL,
                contents="Hello",
            )
            return True, "API key is valid"
        except Exception as e:
            return False, str(e)

    def is_cache_valid(self) -> bool:
        return (
            self._cache_text is not None
            and time.time() - self._cache_time < AI_CACHE_SECONDS
        )

    def analyze(
        self,
        current: GlucoseReading,
        history: List[GlucoseReading],
        on_chunk: Callable[[str], None],
        on_done: Callable[[str], None],
        on_error: Callable[[str], None],
        force: bool = False,
    ):
        """Run Gemini analysis in a background thread with streaming callbacks."""
        if not self._api_key:
            log.warning("AI analyze: Gemini API key not configured")
            on_error("Gemini API key not configured. Please add it in settings.")
            return

        with self._lock:
            if not force and self.is_cache_valid():
                log.debug("AI analyze: using cached result")
                on_done(self._cache_text)
                return

        thread = threading.Thread(
            target=self._analyze_thread,
            args=(current, history, on_chunk, on_done, on_error),
            daemon=True,
        )
        thread.start()

    def _analyze_thread(
        self,
        current: GlucoseReading,
        history: List[GlucoseReading],
        on_chunk: Callable[[str], None],
        on_done: Callable[[str], None],
        on_error: Callable[[str], None],
    ):
        log.info(f"Starting AI analysis, glucose {current.value} mg/dL")
        try:
            from google import genai

            prompt = self._build_prompt(current, history)
            client = genai.Client(api_key=self._api_key)

            full_text = ""
            stream = client.models.generate_content_stream(
                model=self.MODEL,
                contents=prompt,
            )
            for chunk in stream:
                if chunk.text:
                    full_text += chunk.text
                    on_chunk(chunk.text)

            with self._lock:
                self._cache_text = full_text
                self._cache_time = time.time()

            log.info(f"AI analysis complete, {len(full_text)} chars")
            on_done(full_text)

        except Exception as e:
            log.error(f"AI analysis error: {e}", exc_info=True)
            on_error(f"AI analysis failed: {e}")

    def _build_prompt(self, current: GlucoseReading, history: List[GlucoseReading]) -> str:
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)

        # Group 24h data by hour
        hourly: dict[int, list[int]] = {}
        for r in history:
            h = int((now - r.timestamp).total_seconds() / 3600)
            hourly.setdefault(h, []).append(r.value)

        hourly_lines = []
        for h in sorted(hourly.keys(), reverse=True):
            vals = hourly[h]
            avg = round(sum(vals) / len(vals))
            label = "now" if h == 0 else f"{h}h ago"
            hourly_lines.append(f"  ~{label}: avg {avg} ({min(vals)}–{max(vals)}) mg/dL, {len(vals)} readings")

        # Last 3 hours, individual readings
        recent = [r for r in history if (now - r.timestamp).total_seconds() < 3 * 3600]
        recent_lines = [
            f"  {r.timestamp.strftime('%H:%M')}  {r.value} mg/dL  {r.trend_arrow}"
            for r in sorted(recent, key=lambda x: x.timestamp)
        ]

        return f"""You are a professional diabetes management assistant. Analyze the following 24-hour glucose data.

## Current Glucose
{current.value} mg/dL {current.trend_arrow} — {current.status_text}, {current.age_text}

## 24-Hour Hourly Summary (oldest → newest)
{chr(10).join(hourly_lines) if hourly_lines else '  No data'}

## Last 3 Hours (individual readings)
{chr(10).join(recent_lines) if recent_lines else '  No data'}

Normal range: 70–180 mg/dL

Please respond with the following structure, using **bold** for section titles:

**Current Status** — Assess current glucose level and trend arrow meaning

**24h Patterns** — Identify daily fluctuation patterns, peak and trough times

**Risk Flags** — Note any concerning patterns (frequent lows, post-meal spikes, etc.)

**Action Tips** — 2–3 specific, actionable suggestions (diet/exercise/monitoring)

Keep the tone friendly and professional. Total response under 150 words."""
