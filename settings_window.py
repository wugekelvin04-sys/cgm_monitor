import json
import pathlib
import threading
import subprocess
import urllib.parse
from typing import Optional, Callable

import objc
from AppKit import (
    NSPanel, NSWindowStyleMaskTitled, NSWindowStyleMaskClosable,
    NSWindowStyleMaskMiniaturizable, NSNormalWindowLevel,
    NSColor, NSMakeRect, NSBackingStoreBuffered, NSApp,
)
from Foundation import NSURLResponse, NSData
from WebKit import WKWebView, WKWebViewConfiguration

from constants import SETTINGS_WINDOW_WIDTH, SETTINGS_WINDOW_HEIGHT
from logger import get_logger

log = get_logger("settings")

_CGM_SCHEME = "cgm"


def _settings_html_url():
    from Foundation import NSURL
    path = pathlib.Path(__file__).parent / "ui" / "settings.html"
    return NSURL.fileURLWithPath_(str(path))


class _SettingsSchemeHandler(objc.lookUpClass("NSObject")):
    """Intercepts cgm:// requests for JS→Python communication."""

    def initWithCallback_(self, callback: Callable):
        self = objc.super(_SettingsSchemeHandler, self).init()
        if self is None:
            return None
        self._callback = callback
        return self

    @objc.typedSelector(b'v@:@@')
    def webView_startURLSchemeTask_(self, webview, task):
        try:
            url_str = str(task.request().URL().absoluteString())
            # url format: cgm://%7B%22action%22%3A...%7D
            encoded = url_str[len(_CGM_SCHEME) + 3:]  # strip "cgm://"
            body = json.loads(urllib.parse.unquote(encoded))
            log.debug(f"Settings JS→Python: {body.get('action')}")
            self._callback(body)
        except Exception as e:
            log.error(f"Settings scheme handler error: {e}", exc_info=True)
        finally:
            # Must respond to task, otherwise fetch will hang
            try:
                from Foundation import NSURL as _NSURL
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


class SettingsWindow:
    """Settings window: standalone NSPanel + WKWebView, JS→Python via WKURLSchemeHandler."""

    def __init__(self, dexcom_client, ai_analyzer, on_settings_saved: Optional[Callable] = None):
        self._dexcom = dexcom_client
        self._ai = ai_analyzer
        self._on_settings_saved = on_settings_saved
        self._panel: Optional[NSPanel] = None
        self._webview: Optional[WKWebView] = None
        self._built = False

    def _build(self):
        if self._built:
            return
        self._built = True

        config = WKWebViewConfiguration.alloc().init()
        self._scheme_handler = _SettingsSchemeHandler.alloc().initWithCallback_(self._handle_message)
        config.setURLSchemeHandler_forURLScheme_(self._scheme_handler, _CGM_SCHEME)

        frame = NSMakeRect(0, 0, SETTINGS_WINDOW_WIDTH, SETTINGS_WINDOW_HEIGHT)
        webview = WKWebView.alloc().initWithFrame_configuration_(frame, config)
        webview.setOpaque_(False)
        webview.setBackgroundColor_(NSColor.clearColor())
        webview.setValue_forKey_(False, "drawsBackground")

        url = _settings_html_url()
        webview.loadFileURL_allowingReadAccessToURL_(url, url.URLByDeletingLastPathComponent())

        # Panel with title bar (user can drag via title bar)
        style = (NSWindowStyleMaskTitled | NSWindowStyleMaskClosable |
                 NSWindowStyleMaskMiniaturizable)
        panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            frame, style, NSBackingStoreBuffered, False
        )
        panel.setTitle_("CGM Settings")
        panel.setLevel_(NSNormalWindowLevel + 1)
        panel.setHidesOnDeactivate_(False)
        panel.setContentView_(webview)
        panel.center()

        self._panel = panel
        self._webview = webview

    def show(self):
        self._call_on_main(self._do_show)

    def _do_show(self):
        self._build()
        if self._panel:
            self._panel.makeKeyAndOrderFront_(None)
            NSApp.activateIgnoringOtherApps_(True)

    def hide(self):
        if self._panel:
            self._call_on_main(self._panel.orderOut_, None)

    def _handle_message(self, body: dict):
        """Handle messages from JS (called on background thread; blocking ops use threads)."""
        action = body.get("action")

        if action == "close":
            self._call_on_main(self._panel.orderOut_, None)

        elif action == "load":
            self._send_current_config()

        elif action == "test_dexcom":
            threading.Thread(target=self._test_dexcom, args=(body,), daemon=True).start()

        elif action == "save_dexcom":
            threading.Thread(target=self._save_dexcom, args=(body,), daemon=True).start()

        elif action == "test_gemini":
            threading.Thread(target=self._test_gemini, args=(body,), daemon=True).start()

        elif action == "save_gemini":
            self._save_gemini(body)

        elif action == "save_display":
            self._save_display(body)

        elif action == "open_url":
            url = body.get("url", "")
            if url.startswith("https://"):
                subprocess.Popen(["open", url])

    def _send_current_config(self):
        username, _, ous = self._dexcom.credentials.load()
        interval = self._dexcom.credentials.load_refresh_interval()
        result = {
            "action": "load",
            "dex_username": username or "",
            "region": "ous" if ous else "us",
            "refresh_interval": interval or 300,
        }
        self._callback_js(result)

    def _test_dexcom(self, body: dict):
        username = body.get("username", "").strip()
        password = body.get("password", "")
        ous = bool(body.get("ous", False))
        log.info(f"Testing Dexcom connection: user={username}")
        success = self._dexcom.login(username, password, ous)
        self._callback_js({
            "action": "test_dexcom",
            "success": success,
            "message": "Connected successfully ✓" if success else "Connection failed. Check credentials.",
        })

    def _save_dexcom(self, body: dict):
        username = body.get("username", "").strip()
        password = body.get("password", "")
        ous = bool(body.get("ous", False))
        log.info(f"Saving Dexcom credentials: user={username}")
        success = self._dexcom.login(username, password, ous)
        self._callback_js({
            "action": "save_dexcom",
            "success": success,
            "message": "Saved ✓" if success else "Save failed. Check credentials.",
        })
        if success and self._on_settings_saved:
            self._on_settings_saved("dexcom_saved")

    def _test_gemini(self, body: dict):
        api_key = body.get("api_key", "").strip()
        success, msg = self._ai.test_api_key(api_key)
        self._callback_js({
            "action": "test_gemini",
            "success": success,
            "message": msg,
        })

    def _save_gemini(self, body: dict):
        api_key = body.get("api_key", "").strip()
        log.info("Saving Gemini API key")
        try:
            self._dexcom.credentials.save_gemini_key(api_key)
            self._ai.set_api_key(api_key)
            self._callback_js({
                "action": "save_gemini",
                "success": True,
                "message": "API key saved ✓",
            })
        except Exception as e:
            log.error(f"Failed to save Gemini key: {e}", exc_info=True)
            self._callback_js({
                "action": "save_gemini",
                "success": False,
                "message": str(e),
            })

    def _save_display(self, body: dict):
        interval = int(body.get("refresh_interval", 300))
        try:
            self._dexcom.credentials.save_refresh_interval(interval)
            self._callback_js({
                "action": "save_display",
                "success": True,
                "message": "Saved ✓",
            })
            if self._on_settings_saved:
                self._on_settings_saved("display_saved", interval)
        except Exception as e:
            self._callback_js({
                "action": "save_display",
                "success": False,
                "message": str(e),
            })

    def _callback_js(self, result: dict):
        js = f"window.settingsCallback({json.dumps(result, ensure_ascii=False)})"
        self._call_on_main(lambda: self._eval_js(js))

    def _eval_js(self, js: str):
        if self._webview:
            self._webview.evaluateJavaScript_completionHandler_(js, None)

    def _call_on_main(self, func, *args):
        import AppKit
        block = (lambda: func(*args)) if args else func
        if AppKit.NSThread.isMainThread():
            block()
        else:
            caller = _SettingsMainCaller.alloc().initWithBlock_(block)
            caller.performSelectorOnMainThread_withObject_waitUntilDone_(
                "run:", None, False
            )


class _SettingsMainCaller(objc.lookUpClass("NSObject")):
    def initWithBlock_(self, block):
        self = objc.super(_SettingsMainCaller, self).init()
        if self is None:
            return None
        self._block = block
        return self

    def run_(self, _):
        try:
            self._block()
        except Exception as e:
            log.error(f"Main thread dispatch error (settings): {e}", exc_info=True)
