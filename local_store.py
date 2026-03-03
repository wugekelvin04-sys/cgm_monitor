import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import List

from models import GlucoseReading
from logger import get_logger

log = get_logger("store")

DATA_DIR = Path.home() / "Library" / "Application Support" / "CGMMonitor"

# Local data retention days
RETAIN_DAYS = 30


class LocalStore:
    """Per-user SQLite store for glucose readings."""

    def __init__(self, username: str):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^a-zA-Z0-9._-]", "_", username)
        self._path = DATA_DIR / f"{safe}.db"
        log.info(f"Local store: {self._path}")
        self._init_db()
        self._prune()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_db(self):
        with self._conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS readings (
                    ts      INTEGER PRIMARY KEY,
                    value   INTEGER NOT NULL,
                    trend_arrow        TEXT,
                    trend_description  TEXT
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_ts ON readings(ts)")
            conn.commit()

    def upsert(self, readings: List[GlucoseReading]):
        if not readings:
            return
        rows = [
            (
                int(r.timestamp.timestamp()),
                r.value,
                r.trend_arrow,
                r.trend_description,
            )
            for r in readings
        ]
        with self._conn() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO readings (ts, value, trend_arrow, trend_description) VALUES (?,?,?,?)",
                rows,
            )
            conn.commit()
        log.debug(f"upserted {len(rows)} readings")

    def load(self, minutes: int) -> List[GlucoseReading]:
        cutoff = int(datetime.now(timezone.utc).timestamp()) - minutes * 60
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT ts, value, trend_arrow, trend_description "
                "FROM readings WHERE ts >= ? ORDER BY ts ASC",
                (cutoff,),
            ).fetchall()
        result = []
        for ts, value, trend_arrow, trend_description in rows:
            result.append(
                GlucoseReading(
                    value=value,
                    trend_arrow=trend_arrow or "",
                    trend_description=trend_description or "",
                    timestamp=datetime.fromtimestamp(ts, tz=timezone.utc),
                )
            )
        log.debug(f"loaded {len(result)} readings from store (last {minutes}min)")
        return result

    def _prune(self):
        cutoff = int(datetime.now(timezone.utc).timestamp()) - RETAIN_DAYS * 86400
        with self._conn() as conn:
            deleted = conn.execute(
                "DELETE FROM readings WHERE ts < ?", (cutoff,)
            ).rowcount
            conn.commit()
        if deleted:
            log.info(f"pruned {deleted} readings older than {RETAIN_DAYS} days")
