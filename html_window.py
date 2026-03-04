import json
import pathlib
import threading
import urllib.parse
from typing import Optional, List, Callable

import objc
from AppKit import (
    NSPanel, NSWindowStyleMaskBorderless, NSFloatingWindowLevel,
    NSColor, NSPoint, NSMakeRect,
    NSBackingStoreBuffered, NSTrackingArea, NSScreen, NSEvent,
)
from Foundation import NSURLResponse, NSData
from WebKit import WKWebView, WKWebViewConfiguration


def _screen_containing_point(pt):
    """Return the NSScreen whose frame contains pt, falling back to mainScreen."""
    for s in NSScreen.screens():
        f = s.frame()
        if (f.origin.x <= pt.x < f.origin.x + f.size.width and
                f.origin.y <= pt.y < f.origin.y + f.size.height):
            return s
    return NSScreen.mainScreen()


class _DraggableWKWebView(WKWebView):
    """WKWebView subclass: supports free window dragging triggered from the title bar"""
    _title_drag_active = False
    _drag_start_screen_x = 0.0
    _drag_start_screen_y = 0.0
    _drag_start_window_x = 0.0
    _drag_start_window_y = 0.0

    def mouseDown_(self, event):
        self._title_drag_active = False
        objc.super(_DraggableWKWebView, self).mouseDown_(event)

    def mouseDragged_(self, event):
        if self._title_drag_active:
            self._do_drag()
        else:
            objc.super(_DraggableWKWebView, self).mouseDragged_(event)

    def mouseUp_(self, event):
        self._title_drag_active = False
        objc.super(_DraggableWKWebView, self).mouseUp_(event)

    def _do_drag(self):
        if not self.window():
            return
        loc = NSEvent.mouseLocation()
        dx = loc.x - self._drag_start_screen_x
        dy = loc.y - self._drag_start_screen_y
        new_x = self._drag_start_window_x + dx
        new_y = self._drag_start_window_y + dy
        # Clamp to whichever screen the cursor is currently on (multi-monitor support)
        screen = _screen_containing_point(loc)
        if screen:
            sf = screen.frame()
            win = self.window()
            win_w = win.frame().size.width
            win_h = win.frame().size.height
            new_x = max(sf.origin.x, min(new_x, sf.origin.x + sf.size.width - win_w))
            new_y = max(sf.origin.y, min(new_y, sf.origin.y + sf.size.height - win_h))
        self.window().setFrameOrigin_(NSPoint(new_x, new_y))

    def initiateDrag_(self, _):
        """JS-triggered: record start point, begin free drag"""
        if self.window():
            loc = NSEvent.mouseLocation()
            self._drag_start_screen_x = loc.x
            self._drag_start_screen_y = loc.y
            self._drag_start_window_x = self.window().frame().origin.x
            self._drag_start_window_y = self.window().frame().origin.y
            self._title_drag_active = True

# NSTrackingArea option constants
_NSTrackingMouseMoved          = 0x002
_NSTrackingMouseEnteredAndExited = 0x001
_NSTrackingActiveAlways        = 0x080
_NSTrackingInVisibleRect       = 0x200

from models import GlucoseReading
from constants import MAIN_WINDOW_WIDTH, MAIN_WINDOW_HEIGHT
from logger import get_logger
import ui_state

log = get_logger("window")

_CGM_SCHEME = "cgm"


def _html_url() -> "NSURL":
    from Foundation import NSURL
    path = pathlib.Path(__file__).parent / "ui" / "index.html"
    return NSURL.fileURLWithPath_(str(path))


class _MouseTracker(objc.lookUpClass("NSObject")):
    """NSTrackingArea owner: captures mouse movement and injects JS, solving the issue where non-key NSPanel doesn't send mousemove"""

    def initWithWebView_onEnter_onLeave_(self, webview, on_enter, on_leave):
        self = objc.super(_MouseTracker, self).init()
        if self is None:
            return None
        self._webview = webview
        self._on_enter = on_enter
        self._on_leave = on_leave
        return self

    def mouseMoved_(self, event):
        loc = event.locationInWindow()
        view_loc = self._webview.convertPoint_fromView_(loc, None)
        # AppKit Y-axis goes bottom-up, Web Y-axis goes top-down
        css_x = view_loc.x
        css_y = self._webview.frame().size.height - view_loc.y
        js = f"onChartMouseMove({{clientX:{css_x:.1f},clientY:{css_y:.1f}}})"
        self._webview.evaluateJavaScript_completionHandler_(js, None)

    def mouseEntered_(self, event):
        if self._on_enter:
            self._on_enter()

    def mouseExited_(self, event):
        self._webview.evaluateJavaScript_completionHandler_("onChartMouseLeave()", None)
        if self._on_leave:
            self._on_leave()


class _MainSchemeHandler(objc.lookUpClass("NSObject")):
    """Intercepts cgm:// requests to implement JS->Python communication (main window)"""

    def initWithCallback_(self, callback: Callable):
        self = objc.super(_MainSchemeHandler, self).init()
        if self is None:
            return None
        self._callback = callback
        return self

    @objc.typedSelector(b'v@:@@')
    def webView_startURLSchemeTask_(self, webview, task):
        try:
            url_str = str(task.request().URL().absoluteString())
            encoded = url_str[len(_CGM_SCHEME) + 3:]  # Strip "cgm://"
            body = json.loads(urllib.parse.unquote(encoded))
            log.debug(f"JS->Python: {body.get('action')}")
            self._callback(body)
        except Exception as e:
            log.error(f"Main window scheme handler error: {e}", exc_info=True)
        finally:
            try:
                resp = NSURLResponse.alloc().initWithURL_MIMEType_expectedContentLength_textEncodingName_(
                    task.request().URL(), "text/plain", 0, None
                )
                task.didReceiveResponse_(resp)
                task.didReceiveData_(NSData.data())
                task.didFinish()
            except Exception:
                pass

    @objc.typedSelector(b'v@:@@')
    def webView_stopURLSchemeTask_(self, webview, task):
        pass


class _FloatingPanel(objc.lookUpClass("NSPanel")):
    """NSPanel subclass that can always become key window (required for WKWebView text input)."""
    def canBecomeKeyWindow(self):
        return True

    def canBecomeMainWindow(self):
        return False


class _PanelDelegate(objc.lookUpClass("NSObject")):
    """Window delegate for the main floating window"""

    def initWithPanel_(self, panel):
        self = objc.super(_PanelDelegate, self).init()
        if self is None:
            return None
        self._panel = panel
        self._save_timer = None
        return self

    def windowDidMove_(self, notification):
        # Debounce: save position 0.4s after the last move event
        if self._save_timer:
            self._save_timer.cancel()
        import threading
        origin = self._panel.frame().origin
        x, y = origin.x, origin.y
        t = threading.Timer(0.4, lambda: ui_state.save_window_pos(x, y))
        t.daemon = True
        t.start()
        self._save_timer = t


class HTMLFloatingWindow:
    """Main floating window: NSPanel + WKWebView"""

    def __init__(self, on_js_message: Callable[[dict], None],
                 on_mouse_enter: Callable = None, on_mouse_leave: Callable = None):
        self._panel: Optional[NSPanel] = None
        self._webview: Optional[WKWebView] = None
        self._on_js_message = on_js_message
        self._on_mouse_enter = on_mouse_enter
        self._on_mouse_leave = on_mouse_leave
        self._built = False
        self._lock = threading.Lock()

    def _build(self):
        """Lazy build (must be called on the main thread)"""
        with self._lock:
            if self._built:
                return
            self._built = True
        log.info("Building main floating window")

        # WKWebView configuration
        config = WKWebViewConfiguration.alloc().init()
        self._scheme_handler = _MainSchemeHandler.alloc().initWithCallback_(self._on_js_message)
        config.setURLSchemeHandler_forURLScheme_(self._scheme_handler, _CGM_SCHEME)

        # WKWebView (subclass supports native dragging)
        frame = NSMakeRect(0, 0, MAIN_WINDOW_WIDTH, MAIN_WINDOW_HEIGHT)
        webview = _DraggableWKWebView.alloc().initWithFrame_configuration_(frame, config)
        webview.setOpaque_(False)
        webview.setBackgroundColor_(NSColor.clearColor())
        webview.setValue_forKey_(False, "drawsBackground")

        # Load local HTML
        url = _html_url()
        webview.loadFileURL_allowingReadAccessToURL_(url, url.URLByDeletingLastPathComponent())

        # Mouse tracking: add an ActiveAlways tracking area to WKWebView so non-key windows receive mousemove
        self._mouse_tracker = _MouseTracker.alloc().initWithWebView_onEnter_onLeave_(
            webview, self._on_mouse_enter, self._on_mouse_leave)
        tracking_area = NSTrackingArea.alloc().initWithRect_options_owner_userInfo_(
            webview.bounds(),
            _NSTrackingMouseMoved | _NSTrackingMouseEnteredAndExited
            | _NSTrackingActiveAlways | _NSTrackingInVisibleRect,
            self._mouse_tracker,
            None,
        )
        webview.addTrackingArea_(tracking_area)

        # NSPanel (subclassed to ensure canBecomeKeyWindow = True for WKWebView text input)
        style = NSWindowStyleMaskBorderless
        panel = _FloatingPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            frame, style, NSBackingStoreBuffered, False
        )
        panel.setLevel_(NSFloatingWindowLevel)
        panel.setOpaque_(False)
        panel.setBackgroundColor_(NSColor.clearColor())
        panel.setMovableByWindowBackground_(False)
        panel.setHasShadow_(True)
        panel.setHidesOnDeactivate_(False)
        panel.setAcceptsMouseMovedEvents_(True)
        panel.setContentView_(webview)

        # Window delegate
        self._panel_delegate = _PanelDelegate.alloc().initWithPanel_(panel)
        panel.setDelegate_(self._panel_delegate)

        # Initial position: top-right corner of screen
        self._position_window(panel)

        self._panel = panel
        self._webview = webview

    def _position_window(self, panel):
        """Position window: restore last saved position if valid, else top-right corner."""
        saved_x, saved_y = ui_state.load_window_pos()
        if saved_x is not None:
            # Verify the saved position is on a connected screen before restoring
            pt = NSPoint(saved_x + MAIN_WINDOW_WIDTH / 2, saved_y + MAIN_WINDOW_HEIGHT / 2)
            screen = _screen_containing_point(pt)
            if screen:
                sf = screen.frame()
                cx = max(sf.origin.x, min(saved_x, sf.origin.x + sf.size.width - MAIN_WINDOW_WIDTH))
                cy = max(sf.origin.y, min(saved_y, sf.origin.y + sf.size.height - MAIN_WINDOW_HEIGHT))
                panel.setFrameOrigin_(NSPoint(cx, cy))
                return
        # Fallback: top-right corner of main screen
        screen = NSScreen.mainScreen()
        if screen:
            sf = screen.visibleFrame()
            x = sf.origin.x + sf.size.width - MAIN_WINDOW_WIDTH - 20
            y = sf.origin.y + sf.size.height - MAIN_WINDOW_HEIGHT - 10
            panel.setFrameOrigin_(NSPoint(x, y))

    def _ensure_built(self):
        if not self._built:
            self._call_on_main(self._build)

    def show(self):
        self._ensure_built()
        self._call_on_main(self._do_show)

    def _do_show(self):
        if self._panel:
            self._panel.orderFront_(None)

    def show_at(self, x: float, y: float):
        """Show window at specified position (clamped to screen bounds)"""
        self._ensure_built()
        self._call_on_main(lambda: self._do_show_at(x, y))

    def _do_show_at(self, x: float, y: float):
        if not self._panel:
            return
        # Find the screen containing the target position (multi-monitor support)
        screen = _screen_containing_point(NSPoint(x, y))
        if screen:
            sf = screen.frame()
            win_w = self._panel.frame().size.width
            win_h = self._panel.frame().size.height
            cx = max(sf.origin.x, min(x, sf.origin.x + sf.size.width - win_w))
            cy = max(sf.origin.y, min(y, sf.origin.y + sf.size.height - win_h))
            self._panel.setFrameOrigin_(NSPoint(cx, cy))
        self._panel.orderFront_(None)

    def hide(self):
        if self._panel:
            self._call_on_main(self._panel.orderOut_, None)

    def toggle(self):
        if self._panel and self._panel.isVisible():
            self.hide()
        else:
            self.show()

    def is_visible(self) -> bool:
        return bool(self._panel and self._panel.isVisible())

    def resize(self, width: int, height: int):
        self._call_on_main(lambda: self._do_resize(width, height))

    def _do_resize(self, width: int, height: int):
        if not self._panel:
            return
        frame = self._panel.frame()
        # Keep top edge position fixed when resizing height
        new_y = frame.origin.y + frame.size.height - height
        self._panel.setFrame_display_animate_(
            NSMakeRect(frame.origin.x, new_y, width, height), True, False
        )
        if self._webview:
            self._webview.setFrame_(NSMakeRect(0, 0, width, height))

    def initiate_drag(self):
        """JS requests native window drag"""
        self._call_on_main(self._do_initiate_drag)

    def _do_initiate_drag(self):
        if self._webview and hasattr(self._webview, 'initiateDrag_'):
            self._webview.initiateDrag_(None)

    def get_current_height(self) -> int:
        """Return current window height (call on main thread)"""
        if self._panel:
            return int(self._panel.frame().size.height)
        from constants import MAIN_WINDOW_HEIGHT
        return MAIN_WINDOW_HEIGHT

    def get_frame(self):
        """Return current window frame (call on main thread)"""
        if self._panel:
            return self._panel.frame()
        return None

    def update_data(self, reading: GlucoseReading, history: List[GlucoseReading],
                    unit: str = "mgdl", comparison: str = "off",
                    thresh_low: int = 70, thresh_high: int = 180,
                    thresh_alert: int = 250, alert_enabled: bool = True,
                    last_range: int = None):
        """Push glucose data to JS"""
        data = reading.to_dict()
        data["history"] = [
            {"t": int(r.timestamp.timestamp()), "v": r.value}
            for r in history
        ]
        data["unit"] = unit
        data["comparison"] = comparison
        data["thresh_low"]    = thresh_low
        data["thresh_high"]   = thresh_high
        data["thresh_alert"]  = thresh_alert
        data["alert_enabled"] = alert_enabled
        if last_range is not None:
            data["last_range"] = last_range
        js = f"window.updateGlucose({json.dumps(data, ensure_ascii=False)})"
        self._call_on_main(lambda: self._eval_js(js))

    def set_alert(self, active: bool):
        """Push high-glucose alert state to JS (shows/hides the red ! badge)"""
        js = f"typeof window.setAlert === 'function' && window.setAlert({str(active).lower()})"
        self._call_on_main(lambda: self._eval_js(js))

    def show_settings(self, config: dict):
        """Push settings config to JS, show settings overlay"""
        safe = json.dumps(config)
        js = f"window.showSettings({safe})"
        self._call_on_main(lambda: self._eval_js(js))
        # Make window key and set WKWebView as first responder so inputs accept keyboard events
        def _activate():
            if self._panel:
                self._panel.makeKeyAndOrderFront_(None)
            if self._panel and self._webview:
                self._panel.makeFirstResponder_(self._webview)
        self._call_on_main(_activate)

    def restore_range(self, minutes: int):
        """Restore the last selected time range in JS (called on page_ready before first data)."""
        js = f"typeof setRange==='function'&&setRange({int(minutes)},true)"
        self._call_on_main(lambda: self._eval_js(js))

    def compact_applied(self, compact: bool):
        """Notify JS that window resize is done and it should apply compact CSS layout"""
        js = f"typeof applyCompactLayout === 'function' && applyCompactLayout({str(compact).lower()})"
        self._call_on_main(lambda: self._eval_js(js))

    def settings_result(self, result: dict):
        """Push operation result to JS"""
        safe = json.dumps(result)
        js = f"window.settingsResult({safe})"
        self._call_on_main(lambda: self._eval_js(js))

    def start_expand_animation(self):
        """Play expand animation (scale from top-right corner)"""
        self._call_on_main(lambda: self._eval_js("window.startExpandAnimation && window.startExpandAnimation()"))

    def start_collapse_animation(self):
        """Play collapse animation, JS sends collapse_done when complete"""
        self._call_on_main(lambda: self._eval_js("window.startCollapseAnimation && window.startCollapseAnimation()"))

    def cancel_animation(self):
        """Cancel the current animation and restore normal state"""
        self._call_on_main(lambda: self._eval_js("window.cancelAnimation && window.cancelAnimation()"))

    def _eval_js(self, js: str):
        if self._webview:
            self._webview.evaluateJavaScript_completionHandler_(js, None)

    # ─── Main thread dispatch ────────────────────────────────

    def _call_on_main(self, func, *args):
        """Ensure func is executed on the main thread"""
        import AppKit
        block = (lambda: func(*args)) if args else func
        if AppKit.NSThread.isMainThread():
            block()
        else:
            caller = _MainCaller.alloc().initWithBlock_(block)
            caller.performSelectorOnMainThread_withObject_waitUntilDone_(
                "run:", None, False
            )


class _MainCaller(objc.lookUpClass("NSObject")):
    def initWithBlock_(self, block):
        self = objc.super(_MainCaller, self).init()
        if self is None:
            return None
        self._block = block
        return self

    def run_(self, _):
        try:
            self._block()
        except Exception as e:
            log.error(f"Main thread dispatch error (window): {e}", exc_info=True)
