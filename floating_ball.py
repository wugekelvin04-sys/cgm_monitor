import objc
import math
from AppKit import (
    NSPanel, NSWindowStyleMaskBorderless, NSFloatingWindowLevel,
    NSColor, NSPoint, NSMakeRect, NSBackingStoreBuffered,
    NSBezierPath, NSFont, NSAttributedString, NSMutableParagraphStyle,
    NSForegroundColorAttributeName, NSFontAttributeName,
    NSParagraphStyleAttributeName, NSEvent, NSMenu, NSMenuItem,
    NSScreen, NSTrackingArea,
)

_NSTrackingMouseEnteredAndExited = 0x001
_NSTrackingActiveAlways          = 0x080
_NSTrackingInVisibleRect         = 0x200
from logger import get_logger


def _screen_containing_point(pt):
    """Return the NSScreen whose frame contains pt, falling back to mainScreen."""
    for s in NSScreen.screens():
        f = s.frame()
        if (f.origin.x <= pt.x < f.origin.x + f.size.width and
                f.origin.y <= pt.y < f.origin.y + f.size.height):
            return s
    return NSScreen.mainScreen()

log = get_logger("ball")

BALL_W = 44
BALL_H = 44
GLOW_PAD = 14   # Transparent padding around the ball so shadow can extend outside the circle

# Trend arrow -> arc center angle (AppKit coordinates: 0°=right, 90°=up, counter-clockwise positive)
_TREND_ANGLE = {
    '↑↑': 90,
    '↑':  90,
    '↗':  45,
    '→':   0,
    '↘': -45,
    '↓': -90,
    '↓↓': -90,
}


class _BallView(objc.lookUpClass("NSView")):

    def initWithOnClick_onQuit_onHover_value_trend_r_g_b_(self, on_click, on_quit, on_hover, value, trend, r, g, b):
        self = objc.super(_BallView, self).initWithFrame_(NSMakeRect(0, 0, 0, 0))
        if self is None:
            return None
        self._on_click = on_click
        self._on_quit = on_quit
        self._on_hover = on_hover
        self._value = value
        self._trend = trend
        self._r = r
        self._g = g
        self._b = b
        self._alert = False
        self._dragging = False
        self._drag_start_screen = None
        self._drag_start_origin = None
        self._glow_alpha = 0.0
        self._glow_phase = 0.0
        self._glow_timer = None
        self.setWantsLayer_(True)
        return self

    def isOpaque(self):
        return False

    def drawRect_(self, dirtyRect):
        bounds = self.bounds()
        cx = bounds.size.width / 2
        cy = bounds.size.height / 2
        radius = BALL_W / 2 - 1  # Fixed ball radius independent of (padded) view size

        # Clear (transparent)
        NSColor.clearColor().setFill()
        NSBezierPath.fillRect_(bounds)

        # Glow effect: concentric filled circles drawn from large→small (fade inward),
        # creating a soft pulsing halo in the GLOW_PAD area outside the ball circle.
        # Drawn BEFORE white fill so the white circle covers the inner parts.
        circle_rect = NSMakeRect(cx - radius, cy - radius, radius * 2, radius * 2)
        if self._alert and self._glow_alpha > 0:
            a = self._glow_alpha
            for extra_r, intensity in [(12, 0.07), (10, 0.12), (8, 0.20), (6, 0.28), (4, 0.38), (2, 0.50)]:
                gr = NSMakeRect(cx - radius - extra_r, cy - radius - extra_r,
                                (radius + extra_r) * 2, (radius + extra_r) * 2)
                NSColor.colorWithRed_green_blue_alpha_(0.863, 0.149, 0.149, a * intensity).setFill()
                NSBezierPath.bezierPathWithOvalInRect_(gr).fill()

        # White circular background
        NSColor.colorWithRed_green_blue_alpha_(1, 1, 1, 0.96).setFill()
        circle = NSBezierPath.bezierPathWithOvalInRect_(circle_rect)
        circle.fill()

        # Border: thick red ring when alert, thin gray otherwise
        if self._alert:
            NSColor.colorWithRed_green_blue_alpha_(0.863, 0.149, 0.149, 0.90).setStroke()
            circle.setLineWidth_(2.5)
        else:
            NSColor.colorWithRed_green_blue_alpha_(0, 0, 0, 0.08).setStroke()
            circle.setLineWidth_(0.5)
        circle.stroke()

        # Trend arc (outer arc segment, ±38° range)
        color = NSColor.colorWithRed_green_blue_alpha_(self._r, self._g, self._b, 1.0)
        angle = _TREND_ANGLE.get(self._trend)
        if angle is not None:
            arc = NSBezierPath.bezierPath()
            arc.appendBezierPathWithArcWithCenter_radius_startAngle_endAngle_clockwise_(
                NSPoint(cx, cy),
                radius - 2,          # Close to inner side of outer ring
                float(angle - 38),
                float(angle + 38),
                False,               # Counter-clockwise (standard math direction)
            )
            color.setStroke()
            arc.setLineWidth_(3.5)
            arc.setLineCapStyle_(1)  # NSRoundLineCapStyle
            arc.stroke()

        # Centered number (large font, filling the ball)
        para = NSMutableParagraphStyle.alloc().init()
        para.setAlignment_(1)  # NSTextAlignmentCenter
        attrs = {
            NSForegroundColorAttributeName: color,
            NSFontAttributeName: NSFont.systemFontOfSize_(16),
            NSParagraphStyleAttributeName: para,
        }
        attr_str = NSAttributedString.alloc().initWithString_attributes_(self._value, attrs)
        text_h = attr_str.size().height
        text_rect = NSMakeRect(
            0,
            (bounds.size.height - text_h) / 2 + 0.5,
            bounds.size.width,
            text_h,
        )
        attr_str.drawInRect_(text_rect)

    def updateTrackingAreas(self):
        for area in self.trackingAreas():
            self.removeTrackingArea_(area)
        area = NSTrackingArea.alloc().initWithRect_options_owner_userInfo_(
            self.bounds(),
            _NSTrackingMouseEnteredAndExited | _NSTrackingActiveAlways | _NSTrackingInVisibleRect,
            self, None,
        )
        self.addTrackingArea_(area)
        objc.super(_BallView, self).updateTrackingAreas()

    def mouseEntered_(self, event):
        if self._on_hover:
            self._on_hover()

    def mouseDown_(self, event):
        self._drag_start_screen = NSEvent.mouseLocation()
        if self.window():
            self._drag_start_origin = self.window().frame().origin
        self._dragging = False

    def mouseDragged_(self, event):
        if self._drag_start_screen is None:
            return
        loc = NSEvent.mouseLocation()
        dy = loc.y - self._drag_start_screen.y
        oy = self._drag_start_origin.y if self._drag_start_origin else 0
        new_y = oy + dy

        # Pin X to right edge of whichever screen the cursor is on (multi-monitor support)
        screen = _screen_containing_point(loc)
        if screen:
            sf = screen.frame()
            new_x = sf.origin.x + sf.size.width - BALL_W - 20 - GLOW_PAD
            new_y = max(sf.origin.y, min(new_y, sf.origin.y + sf.size.height - BALL_H - 2 * GLOW_PAD))
        else:
            new_x = self._drag_start_origin.x if self._drag_start_origin else 0

        if self.window():
            self.window().setFrameOrigin_(NSPoint(new_x, new_y))
        self._dragging = True

    def mouseUp_(self, event):
        if not self._dragging:
            self._on_click()

    def rightMouseDown_(self, event):
        menu = NSMenu.alloc().init()
        item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Quit CGM", "doQuit:", ""
        )
        item.setTarget_(self)
        menu.addItem_(item)
        NSMenu.popUpContextMenu_withEvent_forView_(menu, event, self)

    def doQuit_(self, sender):
        self._on_quit()

    @objc.typedSelector(b'v@:@')
    def glowTick_(self, timer):
        """NSTimer callback: advance glow sine-wave phase and redraw."""
        self._glow_phase = (self._glow_phase + 0.15) % (2 * math.pi)
        # Sine wave oscillates between 0.08 and 0.90
        self._glow_alpha = 0.08 + 0.82 * (math.sin(self._glow_phase) * 0.5 + 0.5)
        self.setNeedsDisplay_(True)

    def _start_glow(self):
        """Start pulsing glow via NSTimer + NSShadow in drawRect_ (no CGColor needed)."""
        if self._glow_timer:
            self._glow_timer.invalidate()
        self._glow_phase = 0.0
        self._glow_alpha = 0.08
        NSTimer = objc.lookUpClass('NSTimer')
        self._glow_timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            0.033, self, b'glowTick:', None, True
        )
        self.setNeedsDisplay_(True)

    def _stop_glow(self):
        """Stop glow timer and clear alpha."""
        if self._glow_timer:
            self._glow_timer.invalidate()
            self._glow_timer = None
        self._glow_alpha = 0.0
        self.setNeedsDisplay_(True)

    def update_display(self, value, trend, r, g, b, alert=False):
        prev_alert = self._alert
        self._value = value
        self._trend = trend
        self._r = r
        self._g = g
        self._b = b
        self._alert = alert
        if alert and not prev_alert:
            self._start_glow()
        elif not alert and prev_alert:
            self._stop_glow()
        self.setNeedsDisplay_(True)


class FloatingBall:
    """Small floating ball: displays glucose value in large text + outer arc indicating trend direction, draggable on the right side of screen"""

    def __init__(self, on_click, on_quit, on_hover=None):
        self._on_click = on_click
        self._on_quit = on_quit
        self._on_hover = on_hover
        self._panel = None
        self._view = None
        self._built = False
        self._pending_value = "--"
        self._pending_trend = "→"
        self._pending_r = 0.086
        self._pending_g = 0.639
        self._pending_b = 0.290
        self._pending_alert = False

    def _build(self):
        if self._built:
            return
        self._built = True
        log.info("Building floating ball")

        _pw = BALL_W + 2 * GLOW_PAD
        _ph = BALL_H + 2 * GLOW_PAD

        view = _BallView.alloc().initWithOnClick_onQuit_onHover_value_trend_r_g_b_(
            self._on_click,
            self._on_quit,
            self._on_hover,
            self._pending_value,
            self._pending_trend,
            self._pending_r,
            self._pending_g,
            self._pending_b,
        )
        view.setFrame_(NSMakeRect(0, 0, _pw, _ph))

        # Restore alert state that was set before the view was built
        if self._pending_alert:
            view._alert = True
            view._start_glow()

        panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, _pw, _ph),
            NSWindowStyleMaskBorderless,
            NSBackingStoreBuffered,
            False,
        )
        panel.setLevel_(NSFloatingWindowLevel)
        panel.setOpaque_(False)
        panel.setBackgroundColor_(NSColor.clearColor())
        panel.setHasShadow_(False)
        panel.setHidesOnDeactivate_(False)
        panel.setContentView_(view)

        self._position_ball(panel)
        self._panel = panel
        self._view = view

    def _position_ball(self, panel):
        screen = NSScreen.mainScreen()
        if screen:
            sf = screen.visibleFrame()
            x = sf.origin.x + sf.size.width - BALL_W - 20 - GLOW_PAD
            y = sf.origin.y + sf.size.height - BALL_H - 10 - GLOW_PAD
            panel.setFrameOrigin_(NSPoint(x, y))

    def show(self):
        self._build()
        if self._panel:
            self._panel.orderFront_(None)

    def show_at_y(self, y: float, screen_hint_x: float = None):
        """Show ball at specified Y coordinate (X pinned to right edge of target screen, Y clamped).

        screen_hint_x: X coordinate used to detect which screen to use (e.g. main window origin X).
                       Defaults to the ball's current position when not provided.
        """
        self._build()
        if not self._panel:
            return
        # Use the hint point (e.g. main window's origin) to determine which screen the ball should appear on.
        # Falling back to the ball's current origin only when no hint is provided.
        if screen_hint_x is not None:
            hint_pt = NSPoint(screen_hint_x, y)
        else:
            hint_pt = self._panel.frame().origin
        screen = _screen_containing_point(hint_pt)
        if screen:
            sf = screen.frame()
            x = sf.origin.x + sf.size.width - BALL_W - 20 - GLOW_PAD
            y = max(sf.origin.y, min(y, sf.origin.y + sf.size.height - BALL_H - 2 * GLOW_PAD))
            self._panel.setFrameOrigin_(NSPoint(x, y))
        self._panel.orderFront_(None)

    def hide(self):
        if self._panel and self._panel.isVisible():
            self._panel.orderOut_(None)

    def is_visible(self):
        return bool(self._panel and self._panel.isVisible())

    def update(self, value: str, trend: str, r: float, g: float, b: float, alert: bool = False):
        """Update value, trend, color and alert state; must be called on the main thread"""
        self._pending_value = value
        self._pending_trend = trend
        self._pending_r = r
        self._pending_g = g
        self._pending_b = b
        self._pending_alert = alert
        if self._view:
            self._view.update_display(value, trend, r, g, b, alert=alert)

    def get_frame(self):
        """Return the ball's NSRect (call on main thread)"""
        if self._panel:
            return self._panel.frame()
        return None
