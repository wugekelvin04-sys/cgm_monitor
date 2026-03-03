"""CGM macOS Glucose Monitor - Entry point"""
import sys
import truststore

# Use macOS system trust chain (includes enterprise Zscaler certificates)
truststore.inject_into_ssl()

from AppKit import NSApplication, NSApplicationActivationPolicyAccessory
from logger import logger
from app import CGMApp


def _excepthook(exc_type, exc_value, exc_tb):
    logger.critical("Uncaught exception, application crashed", exc_info=(exc_type, exc_value, exc_tb))
    sys.__excepthook__(exc_type, exc_value, exc_tb)


def main():
    sys.excepthook = _excepthook
    # Must initialize NSApplication first so NSApp is not None
    NSApplication.sharedApplication().setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    logger.info("=" * 50)
    logger.info("CGM starting")
    try:
        CGMApp().run()
    except Exception:
        logger.critical("CGMApp.run() crashed", exc_info=True)
        raise


if __name__ == "__main__":
    main()
