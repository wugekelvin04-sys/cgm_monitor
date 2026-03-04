import threading
import json
from typing import Optional, List

import rumps

from dexcom_client import DexcomClient
from html_window import HTMLFloatingWindow
from floating_ball import FloatingBall, BALL_W, BALL_H
from models import GlucoseReading
from constants import (REFRESH_INTERVAL_DEFAULT, COMPACT_WINDOW_HEIGHT, MAIN_WINDOW_HEIGHT, MAIN_WINDOW_WIDTH,
                       DISPLAY_MODE_WINDOW, DISPLAY_MODE_HOVER, GLUCOSE_UNIT_MGDL, GLUCOSE_UNIT_MMOL)
from logger import get_logger

log = get_logger("app")


# Glucose alert deduplication
_last_alert_status: Optional[str] = None
_last_alert_lock = threading.Lock()


class CGMApp(rumps.App):

    def __init__(self):
        super().__init__("⏳ CGM", quit_button=None)

        self._dexcom = DexcomClient()
        self._refresh_lock = threading.Lock()
        self._refresh_interval = REFRESH_INTERVAL_DEFAULT
        self._timer: Optional[rumps.Timer] = None
        self._last_reading: Optional[GlucoseReading] = None
        self._history: List[GlucoseReading] = []
        self._page_ready_event = threading.Event()

        # Display mode / glucose unit / settings toggle / collapse timer
        self._display_mode = DISPLAY_MODE_WINDOW
        self._glucose_unit = GLUCOSE_UNIT_MGDL
        self._settings_open = False
        self._collapse_timer: Optional[threading.Timer] = None
        self._pending_ball_frame = None  # Temporarily store window frame during collapse animation

        # Floating window + floating ball
        self._html_window = HTMLFloatingWindow(
            self._on_js_message,
            on_mouse_enter=self._on_window_mouse_enter,
            on_mouse_leave=self._on_window_mouse_leave,
        )
        self._ball = FloatingBall(
            on_click=self._on_ball_click,
            on_quit=self._on_ball_quit,
            on_hover=self._on_ball_hover,
        )

        # Menu
        self._menu_reading = rumps.MenuItem("-- Waiting for data --", callback=None)
        self._menu_reading.set_callback(None)
        self._menu_toggle = rumps.MenuItem("Show/Hide Window", callback=self._on_toggle_window)
        self._menu_refresh = rumps.MenuItem("Refresh Now", callback=self._on_manual_refresh)
        self._menu_settings = rumps.MenuItem("Settings...", callback=self._on_open_settings)
        self._menu_logout = rumps.MenuItem("Logout", callback=self._on_logout)
        self._menu_quit = rumps.MenuItem("Quit", callback=self._on_quit)

        self.menu = [
            self._menu_reading,
            None,
            self._menu_toggle,
            self._menu_refresh,
            self._menu_settings,
            None,
            self._menu_logout,
            self._menu_quit,
        ]

        self._hotkey_monitor = None

        # Start up
        threading.Thread(target=self._startup, daemon=True).start()

    # ─── Startup sequence ─────────────────────────────────────

    def _startup(self):
        """Background thread: check credentials -> login -> show window/floating ball"""
        log.info("CGM App starting")

        saved_interval = self._dexcom.credentials.load_refresh_interval()
        if saved_interval:
            self._refresh_interval = saved_interval
            log.info(f"Refresh interval: {saved_interval}s")

        saved_mode = self._dexcom.credentials.load_display_mode()
        if saved_mode in (DISPLAY_MODE_WINDOW, DISPLAY_MODE_HOVER):
            self._display_mode = saved_mode
            log.info(f"Display mode: {saved_mode}")

        saved_unit = self._dexcom.credentials.load_glucose_unit()
        if saved_unit in (GLUCOSE_UNIT_MGDL, GLUCOSE_UNIT_MMOL):
            self._glucose_unit = saved_unit
            log.info(f"Glucose unit: {saved_unit}")

        self._html_window.show()
        # Wait for page ready (JS sets the event after page_ready is received)
        self._page_ready_event.wait(timeout=5.0)

        if self._dexcom.credentials.has_credentials():
            success = self._dexcom.login_from_keychain()
            if success:
                self._do_refresh()          # Immediately refresh once on startup
                self._start_timer()         # Start the fixed-interval timer
                self._call_on_main(self._register_global_hotkey)
                if self._display_mode == DISPLAY_MODE_HOVER:
                    # Hover mode: switch to floating ball after startup
                    self._call_on_main(self._do_minimize_to_ball)
                return

        log.info("No valid credentials or login failed, showing settings overlay")
        self._show_settings_overlay()

    # ─── Timer scheduling ───────────────────────────────────────

    def _start_timer(self):
        """Start/restart the fixed-interval timer (thread-safe, can be called repeatedly)"""
        self._call_on_main(self._do_start_timer)

    def _do_start_timer(self):
        """Main thread: stop old timer, create new timer with current interval"""
        if self._timer:
            self._timer.stop()
            self._timer = None
        if not self._dexcom.is_logged_in():
            return
        self._timer = rumps.Timer(self._on_timer_fire, self._refresh_interval)
        self._timer.start()
        log.info(f"Timer started, interval {self._refresh_interval}s")

    def _on_timer_fire(self, _):
        """Timer fired: start background refresh thread (timer repeats automatically, no rescheduling needed)"""
        threading.Thread(target=self._do_refresh, daemon=True).start()

    # ─── Refresh ──────────────────────────────────────────────

    def _on_manual_refresh(self, _):
        threading.Thread(target=self._do_refresh, daemon=True).start()

    def _do_refresh(self):
        if not self._refresh_lock.acquire(blocking=False):
            log.debug("Refresh skipped (previous refresh still running)")
            return
        try:
            if not self._dexcom.is_logged_in():
                log.warning("Not logged in, attempting to rebuild session from keychain...")
                if self._dexcom.credentials.has_credentials():
                    ok = self._dexcom.refresh_session()  # No test request, avoiding extra API call
                    if not ok:
                        log.warning("Auto session rebuild failed, showing settings page")
                        self._call_on_main(self._show_settings_overlay)
                        return
                else:
                    self._call_on_main(self._show_settings_overlay)
                    return
            log.debug("Starting glucose data refresh")
            reading = self._dexcom.get_current_reading()
            history = self._dexcom.get_history()
            if reading:
                self._last_reading = reading
                self._history = history
                self._call_on_main(lambda: self._update_ui(reading, history))
                self._check_alerts(reading)
            else:
                log.warning("Refresh returned empty result")
        except Exception as e:
            log.error(f"Refresh error: {e}", exc_info=True)
        finally:
            self._refresh_lock.release()

    def _fmt_glucose(self, value_mgdl: int) -> str:
        """Format a mg/dL value for display according to the current unit setting."""
        if self._glucose_unit == GLUCOSE_UNIT_MMOL:
            return str(round(value_mgdl / 18.0182, 1))
        return str(value_mgdl)

    def _unit_label(self) -> str:
        return "mmol/L" if self._glucose_unit == GLUCOSE_UNIT_MMOL else "mg/dL"

    def _update_ui(self, reading: GlucoseReading, history: List[GlucoseReading]):
        """Main thread: update menu bar + push JS + update floating ball"""
        log.debug(f"Updating UI: {reading.display_title}")
        val_str = self._fmt_glucose(reading.value)
        self.title = f" {val_str} {reading.trend_arrow}"
        self._menu_reading.title = f"{val_str} {self._unit_label()}  {reading.trend_description}"
        self._html_window.update_data(reading, history, unit=self._glucose_unit)
        self._update_ball_display(reading)

    def _update_ball_display(self, reading: GlucoseReading):
        """Update floating ball value, trend arc and color"""
        v = reading.value
        if v < 55 or v > 250:
            r, g, b = 0.863, 0.149, 0.149   # #dc2626 red
        elif v < 70 or v > 180:
            r, g, b = 0.851, 0.467, 0.024   # #d97706 orange
        else:
            r, g, b = 0.086, 0.639, 0.290   # #16a34a green
        self._ball.update(self._fmt_glucose(v), reading.trend_arrow, r, g, b)

    # ─── Glucose alerts ───────────────────────────────────────

    def _check_alerts(self, reading: GlucoseReading):
        global _last_alert_status
        with _last_alert_lock:
            status = reading.status
            trend = reading.trend_description.lower()

            # DoubleDown / DoubleUp rapid trend change
            rapid_down = "doubledown" in trend or "double down" in trend
            alert_key = status + ("_rapid" if rapid_down else "")

            if alert_key == _last_alert_status:
                return
            _last_alert_status = alert_key

            val_str = self._fmt_glucose(reading.value)
            unit_str = self._unit_label()
            if reading.value < 55:
                log.warning(f"[Critically Low Glucose] {reading.value} mg/dL")
                rumps.notification("⚠️ Very Low Glucose", "", f"Glucose {val_str} {unit_str} — Take action now!")
            elif reading.value < 70:
                log.warning(f"[Low Glucose] {reading.value} mg/dL")
                rumps.notification("🟡 Low Glucose", "", f"Glucose {val_str} {unit_str} {reading.trend_arrow}")
            elif reading.value > 250:
                log.warning(f"[High Glucose] {reading.value} mg/dL")
                rumps.notification("🔴 High Glucose", "", f"Glucose {val_str} {unit_str} {reading.trend_arrow}")
            elif rapid_down:
                log.warning(f"[Glucose Dropping Fast] {reading.value} mg/dL {reading.trend_arrow}")
                rumps.notification("⬇️ Glucose Dropping Fast", "", f"Glucose {val_str} {unit_str} {reading.trend_arrow}")

    # ─── Menu events ──────────────────────────────────────────

    def _on_toggle_window(self, _):
        self._html_window.toggle()

    def _on_open_settings(self, _):
        self._cancel_collapse()
        self._ball.hide()
        self._html_window.show()
        self._show_settings_overlay()

    # ─── Floating ball callbacks ──────────────────────────────

    def _do_minimize_to_ball(self):
        """Main thread: start collapse animation, then actually hide window when animation ends"""
        if self._pending_ball_frame is not None:
            return  # Already collapsing, ignore duplicate request
        self._pending_ball_frame = self._html_window.get_frame() or True  # True as "triggered" marker
        self._html_window.start_collapse_animation()

    def _finish_collapse(self):
        """Collapse animation ended: hide window, show floating ball"""
        win_frame = self._pending_ball_frame
        self._pending_ball_frame = None
        self._html_window.hide()
        if win_frame and win_frame is not True:
            ball_y = win_frame.origin.y + win_frame.size.height - BALL_H
            self._ball.show_at_y(ball_y)
        else:
            self._ball.show()

    def _on_ball_click(self):
        """Floating ball clicked: expand main window (with animation)"""
        if self._display_mode == DISPLAY_MODE_HOVER:
            self._cancel_collapse()
        self._html_window.cancel_animation()
        self._pending_ball_frame = None  # Cancel any pending collapse
        ball_frame = self._ball.get_frame()
        current_h = self._html_window.get_current_height()
        self._ball.hide()
        if ball_frame:
            win_x = ball_frame.origin.x + BALL_W - MAIN_WINDOW_WIDTH
            win_y = ball_frame.origin.y + BALL_H - current_h
            self._html_window.show_at(win_x, win_y)
        else:
            self._html_window.show()
        self._html_window.start_expand_animation()

    def _on_ball_quit(self):
        """Floating ball right-click quit"""
        self._call_on_main(rumps.quit_application)

    def _on_ball_hover(self):
        """Floating ball hover enter (hover mode only): cancel collapse, expand window"""
        if self._display_mode != DISPLAY_MODE_HOVER:
            return
        self._cancel_collapse()
        self._call_on_main(self._on_ball_click)

    def _on_window_mouse_enter(self):
        """Main window mouse enter (hover mode only): cancel collapse timer and animation"""
        if self._display_mode != DISPLAY_MODE_HOVER:
            return
        self._cancel_collapse()
        # If collapse animation is in progress, cancel it
        if self._pending_ball_frame is not None:
            self._pending_ball_frame = None
            self._html_window.cancel_animation()

    def _on_window_mouse_leave(self):
        """Main window mouse leave (hover mode only): schedule delayed collapse"""
        if self._display_mode != DISPLAY_MODE_HOVER:
            return
        if self._settings_open:
            return
        self._schedule_collapse()

    def _schedule_collapse(self, delay=0.3):
        """Schedule a delayed collapse (default 0.3s)"""
        self._cancel_collapse()
        t = threading.Timer(delay, self._do_collapse)
        t.daemon = True
        t.start()
        self._collapse_timer = t

    def _cancel_collapse(self):
        """Cancel pending collapse"""
        if self._collapse_timer:
            self._collapse_timer.cancel()
            self._collapse_timer = None

    def _do_collapse(self):
        """Execute collapse: hide window, show floating ball"""
        self._collapse_timer = None
        self._call_on_main(self._do_minimize_to_ball)

    def _on_logout(self, _):
        log.info("User logged out")
        self._dexcom.logout()
        self.title = "⏳ CGM"
        self._menu_reading.title = "-- Logged out --"
        if self._timer:
            self._timer.stop()
            self._timer = None
        self._cancel_collapse()
        self._html_window.show()
        self._show_settings_overlay()

    def _on_quit(self, _):
        rumps.quit_application()

    # ─── Settings overlay ────────────────────────────────────

    def _show_settings_overlay(self):
        """Push settings config to JS, show settings overlay"""
        username, password, ous = self._dexcom.credentials.load()
        config = {
            "username": username or "",
            "password": password or "",
            "ous": ous,
            "interval": self._refresh_interval,
            "display_mode": self._display_mode,
            "unit": self._glucose_unit,
        }
        self._html_window.show_settings(config)

    # ─── Global hotkey ────────────────────────────────────────

    def _register_global_hotkey(self):
        import AppKit
        ref = AppKit.NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
            AppKit.NSEventMaskKeyDown, self._on_global_key
        )
        self._hotkey_monitor = ref  # Hold reference to prevent GC
        log.info("Global hotkey registered (⌘⌥G)")

    def _on_global_key(self, event):
        import AppKit
        flags = event.modifierFlags()
        cmd = bool(flags & AppKit.NSEventModifierFlagCommand)
        opt = bool(flags & AppKit.NSEventModifierFlagOption)
        key = event.charactersIgnoringModifiers()
        if cmd and opt and key and key.lower() == 'g':
            self._html_window.toggle()

    # ─── JS message handling ──────────────────────────────────

    def _on_js_message(self, body: dict):
        action = body.get("action")
        if action == "open_settings":
            self._show_settings_overlay()
        elif action == "force_refresh":
            threading.Thread(target=self._do_refresh, daemon=True).start()
        elif action == "page_ready":
            self._page_ready_event.set()
            if self._last_reading:
                self._html_window.update_data(self._last_reading, self._history, unit=self._glucose_unit)
        elif action == "minimize_to_ball":
            self._call_on_main(self._do_minimize_to_ball)
        elif action == "hide":
            self._html_window.hide()
        elif action == "initiate_drag":
            self._html_window.initiate_drag()
        elif action == "set_compact":
            compact = body.get("compact", False)
            h = COMPACT_WINDOW_HEIGHT if compact else MAIN_WINDOW_HEIGHT
            self._html_window.resize(MAIN_WINDOW_WIDTH, h)
            self._html_window.compact_applied(compact)
        elif action == "collapse_done":
            self._call_on_main(self._finish_collapse)
        elif action == "settings_open":
            self._settings_open = True
            self._cancel_collapse()
        elif action == "settings_close":
            self._settings_open = False
            if self._display_mode == DISPLAY_MODE_HOVER:
                self._schedule_collapse(delay=1.0)
        elif action == "test_dexcom":
            threading.Thread(target=self._handle_test_dexcom, args=(body,), daemon=True).start()
        elif action == "save_dexcom":
            threading.Thread(target=self._handle_save_dexcom, args=(body,), daemon=True).start()
        elif action == "save_display":
            self._handle_save_display(body)

    def _handle_test_dexcom(self, body: dict):
        username = body.get("username", "").strip()
        password = body.get("password", "")
        ous = body.get("ous", False)
        if not password:
            _, stored_pw, _ = self._dexcom.credentials.load()
            password = stored_pw or ""
        if not username or not password:
            self._html_window.settings_result({"type": "test_dexcom", "success": False, "message": "Username and password required"})
            return
        try:
            from pydexcom import Dexcom
            from pydexcom.const import Region
            import pydexcom.dexcom as _pydex
            _pydex.HEADERS = {"Accept": "application/json", "Content-Type": "application/json"}
            dex = Dexcom(username=username, password=password, region=Region.OUS if ous else Region.US)
            dex.get_current_glucose_reading()
            self._html_window.settings_result({"type": "test_dexcom", "success": True, "message": "Connected successfully"})
        except Exception as e:
            self._html_window.settings_result({"type": "test_dexcom", "success": False, "message": str(e)})

    def _handle_save_dexcom(self, body: dict):
        username = body.get("username", "").strip()
        password = body.get("password", "")
        ous = body.get("ous", False)
        if not password:
            _, stored_pw, _ = self._dexcom.credentials.load()
            password = stored_pw or ""
        if not username or not password:
            self._html_window.settings_result({"type": "save_dexcom", "success": False, "message": "Username and password required"})
            return
        success = self._dexcom.login(username, password, ous)
        if success:
            self._html_window.settings_result({"type": "save_dexcom", "success": True, "message": "Logged in"})
            threading.Thread(target=self._do_refresh, daemon=True).start()  # Immediately refresh once
            self._start_timer()                                               # Start fixed-interval timer
            self._call_on_main(self._register_global_hotkey)
        else:
            self._html_window.settings_result({"type": "save_dexcom", "success": False, "message": "Login failed. Check credentials."})

    def _handle_save_display(self, body: dict):
        interval = body.get("interval", REFRESH_INTERVAL_DEFAULT)
        self._refresh_interval = interval
        self._dexcom.credentials.save_refresh_interval(interval)
        self._start_timer()
        mode = body.get("display_mode", DISPLAY_MODE_WINDOW)
        if mode in (DISPLAY_MODE_WINDOW, DISPLAY_MODE_HOVER):
            self._display_mode = mode
            self._dexcom.credentials.save_display_mode(mode)
        unit = body.get("unit", GLUCOSE_UNIT_MGDL)
        if unit in (GLUCOSE_UNIT_MGDL, GLUCOSE_UNIT_MMOL):
            self._glucose_unit = unit
            self._dexcom.credentials.save_glucose_unit(unit)
        self._html_window.settings_result({"type": "save_display", "success": True, "message": "Saved"})

    # ─── Main thread dispatch ────────────────────────────────

    def _call_on_main(self, func):
        import AppKit
        if AppKit.NSThread.isMainThread():
            func()
        else:
            import objc
            caller = _AppMainCaller.alloc().initWithBlock_(func)
            caller.performSelectorOnMainThread_withObject_waitUntilDone_(
                "run:", None, False
            )


import objc as _objc


class _AppMainCaller(_objc.lookUpClass("NSObject")):
    def initWithBlock_(self, block):
        self = _objc.super(_AppMainCaller, self).init()
        if self is None:
            return None
        self._block = block
        return self

    def run_(self, _):
        try:
            self._block()
        except Exception as e:
            log.error(f"Main thread dispatch error (app): {e}", exc_info=True)
