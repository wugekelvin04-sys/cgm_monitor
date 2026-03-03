"""Unified logging configuration: daily rotation, keep 7 days, auto-clean expired logs"""
import logging
import logging.handlers
import pathlib
import sys
import threading
import time


LOG_DIR = pathlib.Path.home() / "Library" / "Logs" / "CGM"
KEEP_DAYS = 7


def _cleanup_old_logs():
    """Delete log files older than KEEP_DAYS days"""
    cutoff = time.time() - KEEP_DAYS * 86400
    for f in LOG_DIR.glob("cgm.log.*"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
                logging.getLogger("cgm").info(f"Cleaned up expired log: {f.name}")
        except Exception:
            pass


def _schedule_daily_cleanup():
    """Run cleanup once daily at 00:05"""
    import datetime

    def _loop():
        while True:
            now = datetime.datetime.now()
            next_run = (now + datetime.timedelta(days=1)).replace(
                hour=0, minute=5, second=0, microsecond=0
            )
            time.sleep((next_run - now).total_seconds())
            _cleanup_old_logs()

    t = threading.Thread(target=_loop, daemon=True)
    t.start()


def _setup() -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOG_DIR / "cgm.log"

    fmt = logging.Formatter(
        "%(asctime)s  %(levelname)-8s  %(name)-20s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Daily rotation: cut at midnight, suffix format cgm.log.2026-03-03
    fh = logging.handlers.TimedRotatingFileHandler(
        log_file,
        when="midnight",
        interval=1,
        backupCount=KEEP_DAYS,
        encoding="utf-8",
        utc=False,
    )
    fh.suffix = "%Y-%m-%d"
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)

    # Console handler
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)

    root = logging.getLogger("cgm")
    root.setLevel(logging.DEBUG)
    root.addHandler(fh)
    root.addHandler(ch)
    root.propagate = False

    # Run cleanup once on startup, then schedule daily task
    _cleanup_old_logs()
    _schedule_daily_cleanup()

    root.info(f"Log file: {log_file} (daily rotation, keep {KEEP_DAYS} days)")
    return root


logger = _setup()


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"cgm.{name}")
