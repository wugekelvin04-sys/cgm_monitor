# Agent Context — CGM Monitor

> Context file for restoring session state in a new conversation.

---

## Project Overview

**Project**: CGM Monitor
**Type**: macOS status bar app (rumps + NSPanel + WKWebView) + Electron cross-platform build
**Function**: Real-time CGM glucose monitoring — supports **Dexcom** and **FreeStyle Libre 2/3**
**Run**: `python3 main.py` or double-click `cgm.command` (macOS); `cd electron && npm start` (Electron)
**Dependencies**: `pydexcom`, `pylibrelinkup`, `rumps`, `pyobjc`, `keyring`

---

## Working Mode

### Grove Worktree
- Working inside a Grove-managed worktree
- Working directory: `/Users/bytedance/.grove/worktrees/04fc028e137e3c4a/init/`
- Branch: `grove/init-da0b4a`, target branch: `main`
- After completing a feature, commit with `/commit` skill (Conventional Commits format, English)
- When all work is done, merge to main with `grove_complete_task`
- **Work only in the worktree; do not modify the main workspace (`/Users/bytedance/app/cgm/`) directly** — user syncs manually

### Language
- Communication with user: **Chinese**
- Code comments: **English**
- UI text (HTML/JS): **English**
- AI prompts: **English**
- Commit messages: **English**

---

## File Structure

```
init/
├── main.py                # Entry point, starts CGMApp
├── app.py                 # Core: rumps.App, Timer, JS message routing, provider dispatch
├── html_window.py         # HTMLFloatingWindow: NSPanel + WKWebView main window
├── floating_ball.py       # FloatingBall: floating ball NSPanel (minimized state)
├── dexcom_client.py       # Dexcom API wrapper (pydexcom), with CredentialManager (shared settings)
├── libre_client.py        # FreeStyle Libre API wrapper (pylibrelinkup), duck-typed to DexcomClient
├── ui_state.py            # JSON UI state persistence (window pos + time range + provider type)
├── ai_analyzer.py         # GeminiAnalyzer (unused — AI removed from app.py)
├── bridge.py              # Python sidecar for Electron (JSON Lines over stdio)
├── local_store.py         # SQLite local storage (~/Library/Application Support/CGMMonitor/)
├── models.py              # GlucoseReading dataclass
├── constants.py           # Constants (thresholds, colors, window sizes, keyring keys)
├── logger.py              # Logging
├── settings_window.py     # Old settings window (deprecated, not imported by app.py)
├── requirements.txt       # macOS native dependencies
├── requirements-electron.txt  # Cross-platform (Electron) dependencies
├── cgm.command            # Double-click launch script
├── electron/
│   ├── main.js            # Electron main process
│   ├── preload.js         # Context bridge (electronAPI)
│   ├── ball.html          # Floating ball window
│   └── package.json
└── ui/
    ├── index.html         # Main window HTML/CSS/JS (300×220px), includes provider selector
    └── settings.html      # Old settings page (deprecated)
```

---

## Architecture Notes

### Main Window (HTMLFloatingWindow)
- `_FloatingPanel` (NSPanel subclass, `canBecomeKeyWindow→True`, `canBecomeMainWindow→False`) + `WKWebView`
  - Required so WKWebView inputs are editable in borderless panels (especially after logout)
  - `show_settings()` also calls `makeKeyAndOrderFront_` + `makeFirstResponder_(webview)` to ensure input focus
- JS→Python communication: custom URL scheme `cgm://` (`_MainSchemeHandler`)
- Python→JS: `evaluateJavaScript_completionHandler_`
- Mouse events: `_MouseTracker` (NSObject) + `NSTrackingArea` (NSTrackingActiveAlways) — solves mousemove on non-key windows
- **Y-axis only drag**:
  - `setMovableByWindowBackground_(False)` — disables native background drag
  - JS `#top-row` mousedown → `initiate_drag` action → Python `_DraggableWKWebView.initiateDrag_`
  - `initiateDrag_` records start Y, sets `_title_drag_active = True`
  - `mouseDragged_` checks flag → `_do_y_drag()`, X fixed at `screenWidth - windowWidth - 20`
  - `mouseUp_` clears flag
  - `_PanelDelegate.windowDidMove_` as fallback: forces X back to right edge; also debounced 0.4s save of position to `ui_state`
- Main thread dispatch: `_MainCaller` (NSObject) + `performSelectorOnMainThread_withObject_waitUntilDone_`
- `restore_range(minutes)`: evaluates `setRange(N, true)` JS — called on `page_ready` before first reading to restore last chart time range

### Floating Ball (FloatingBall)
- Standalone `NSPanel` (borderless, floating level, `setHasShadow_(False)`) + custom `_BallView` (NSView subclass)
- `GLOW_PAD = 14`: transparent padding around 44×44 ball so glow/alert halo can extend outward; panel is `72×72`, ball drawn in center
- `_BallView.drawRect_`:
  - Fixed `radius = BALL_W / 2 - 1 = 21` (not view-bounds-based, so GLOW_PAD does not affect ball size)
  - Alert glow: 6 concentric filled red circles (`extra_r` 2–12px beyond ball edge), alpha modulated by sine wave
  - White circular background (`bezierPathWithOvalInRect_`, radius=21)
  - Alert border: 2.5px red ring; normal border: 0.5px 8% black
  - Trend arc: `appendBezierPathWithArcWithCenter_radius_startAngle_endAngle_clockwise_`, radius-2, ±38°, 3.5px, round caps
  - Centered number: `NSFont.systemFontOfSize_(16)`
- Trend angle mapping: `↑↑/↑→90°`, `↗→45°`, `→→0°`, `↘→-45°`, `↓/↓↓→-90°`
- **Alert glow animation**:
  - `_start_glow()`: creates `NSTimer` at 0.033s interval → `glowTick_` (requires `@objc.typedSelector(b'v@:@')`)
  - `glowTick_`: advances `_glow_phase` by 0.15 rad/tick, `_glow_alpha = 0.08 + 0.82*(sin+0.5)`
  - `_stop_glow()`: invalidates timer, resets `_glow_alpha = 0.0`
  - `update_display(alert=True/False)` starts/stops glow; `_pending_alert` preserves state across `_build()`
- **Y-axis only drag**:
  - `mouseDragged_` only computes dy; `new_x` = `screenWidth - BALL_W - 20 - GLOW_PAD`
  - Y clamped within `screen.frame()` bounds
- Right-click menu: `rightMouseDown_` → "Quit CGM"
- Click (non-drag): calls `on_click` callback → hides ball, restores main window at ball position
- **Pending mechanism**: `update(value, trend, r, g, b, alert)` always updates `_pending_*`; first `_build()` initializes with pending values (including alert state)
- `panel.setHasShadow_(False)`: required — without it, macOS renders a grey shadow ring visible in the transparent GLOW_PAD area

### Hover Mode
- Two display modes: `DISPLAY_MODE_WINDOW` (default), `DISPLAY_MODE_HOVER`
- In hover mode: main window collapses to ball on mouse leave; expands on hover/click
- Collapse timer: 0.3s delay, cancelled on `window_mouse_enter`
- `_settings_open` flag: prevents collapse while settings overlay is visible; 1s delay after close
- Animations: CSS `@keyframes expandIn` (0.22s spring) / `collapseOut` (0.18s); JS `animationend` → `collapse_done` → Python switches windows

### Electron Architecture
- `bridge.py`: Python sidecar; stdout exclusively for JSON Lines, stderr for logs; UTF-8 forced on Windows stdout
- `electron/main.js`: window management, tray, IPC routing, Python process spawning
- Single `cgm-msg` IPC channel for both windows; `fromBall` detected via `event.sender`
- `electron/preload.js`: exposes `electronAPI` via contextBridge; drag sends both X and Y
- `electron/ball.html`: 44×44 transparent floating ball with canvas arc
- `electron/create_icon.py`: generates `icon.png` (32×32 RGBA CGM-sensor PNG) using stdlib only
- `cgm.bat`: Windows launcher; creates venv_win, installs deps, generates icon, runs `npm start`
- `ui/index.html`: `IS_ELECTRON = !!window.electronAPI` flag for dual-mode operation

### JS Messages (action)
| action | handler |
|--------|---------|
| `page_ready` | triggers `restore_range()` + initial `update_data` (race-condition fix) |
| `minimize_to_ball` | hides main window, shows floating ball |
| `hide` | hides main window (no ball) |
| `initiate_drag` | X+Y drag (main window free; ball Y-only, fixed to right) |
| `force_refresh` | background refresh (user-initiated) |
| `open_settings` | shows settings overlay with full credential/config prefill |
| `set_compact` | main sends `compact_applied` back after `setBounds`; renderer applies CSS then |
| `test_dexcom` | tests Dexcom connection |
| `save_dexcom` | saves and logs in to Dexcom |
| `test_libre` | tests FreeStyle Libre connection (pylibrelinkup authenticate + get_patients) |
| `save_libre` | saves and logs in to FreeStyle Libre; switches `_current_provider` |
| `save_display` | saves refresh interval and display mode, restarts timer |
| `save_range` | saves selected chart time range to `ui_state.json` |
| `collapse_done` | animation finished → switch from main window to ball |
| `settings_open` | settings overlay opened → cancel collapse timer |
| `settings_close` | settings overlay closed → schedule collapse (hover mode) |
| `ball_contextmenu` | shows native popup menu (Refresh / Quit) instead of direct quit |

### Provider Architecture (macOS native)
- `app.py` holds `self._dexcom` (DexcomClient), `self._libre` (LibreClient), `self._current_provider` (dynamic pointer)
- `self._provider_type`: `"dexcom"` | `"freestyle_libre"`, persisted to `ui_state.json` via `save_provider_type()`
- All refresh calls go through `self._current_provider` (duck-typed interface: `is_logged_in`, `get_current_reading`, `get_history`, `get_history_from_store`, `logout`)
- Shared settings (refresh interval, display mode, unit, thresholds, comparison) always read/written via `self._dexcom.credentials` (CredentialManager) regardless of active provider
- Libre credentials stored separately in keyring: `libre_email`, `libre_password`, `libre_region` (`"US"` | `"EU"`)
- Libre trend mapping: `{1:'↓↓', 2:'↓', 3:'→', 4:'↑', 5:'↑↑'}` (5 levels vs Dexcom's 7)

### Settings State (main.js)
Cached in main process memory, updated on every save/load:
- `displayMode`, `glucoseUnit`, `currentInterval`, `currentUsername`, `currentPassword`, `currentOus`
- `open_settings` sends all six fields to renderer for full form prefill
- `bridge.py` `credentials` response includes `password` field from keyring

### Startup Sequence (Electron)
On keychain auto-login (`login_from_keychain` success):
1. `_do_startup_refresh()`: push cached store data to UI immediately (fast path)
2. Full API refresh (`_do_refresh`): updates store, pushes live data

### Compact Mode Fix
- `set_compact` uses `setBounds` (atomic resize, no separate `setPosition`)
- Top-edge pinned: Y unchanged when window grows/shrinks
- CSS layout change deferred until after `compact_applied` IPC from main (eliminates flash)

### Tray Icon (Windows)
- Static `icon.png` (32×32, CGM sensor design: oval patch + blue sensor + signal arcs)
- Generated by `electron/create_icon.py` at launch (no Pillow needed)
- Tooltip shows live glucose value + trend arrow (e.g. `CGM: 142 →`)

### Dexcom API Findings
- `ReadGlucoseValues`: server-enforced max 288 records regardless of `maxCount` param
- `minutes` server cap also ~1440; tested up to 10080 — same 288 records returned
- At sub-5-min reading intervals (G7), 288 records cover only ~19.3h instead of 24h
- Local SQLite store accumulates readings over time; full 24h fills in after continuous operation
- No official date-range API on Share endpoint; official Developer API (OAuth2) supports it

### Timer Scheduling (fixed interval)
- **One global `rumps.Timer`** at fixed interval (`_refresh_interval` seconds, default 300s)
- `rumps.Timer` is backed by `NSTimer(repeats=True)`, auto-repeating, no rescheduling needed
- Timer created/restarted when: login succeeds (`_startup` / `_handle_save_dexcom`), interval changed (`_handle_save_display`)
- `_do_refresh` finally block **does not schedule timer**
- Legitimate direct API calls: initial login, user-initiated test/force refresh
- `_do_start_timer` must be called on main thread; `_start_timer` ensures this via `_call_on_main`

### Dexcom API Rate Limiting
- `login()` no longer does a test read (`Dexcom.__init__` throws on auth failure)
- Rate limit detection (`_is_rate_limit_error`): checks exception message + `e.__context__.doc` (pydexcom wraps 429 as JSON error; raw "Too many requests." is in `doc`)
- On rate limit: **do not rebuild session**, no extra requests, wait for next timer
- Session expiry (non-rate-limit JSON/malformed error): calls `_relogin_within_lock()`, 60s cooldown

### Data Flow
1. `_startup` (background thread) → `_html_window.show()` → wait for `_page_ready_event` (max 5s)
2. Login success → `_do_refresh()` immediate refresh → `_start_timer()` starts fixed interval timer
3. `_do_refresh`: `dexcom.get_current_reading()` + `get_history()`
4. Reading written to `LocalStore` (SQLite); `get_history` reads from SQLite (accumulates history)
5. `_update_ui` (main thread) → updates menu bar title + `html_window.update_data()` + `_update_ball_display()`
6. No credentials / login failure → `_show_settings_overlay()` → JS `window.showSettings(config)`

---

## UI Design (index.html)

**Window size**: 300×220px (full mode), 300×80px (compact mode)
**Style**: bright frosted glass `rgba(255,255,255,0.95)` + `backdrop-filter:blur(20px)`, 14px radius

### Layout (full mode)
```
[glucose] [arrow] [status] [time]    [↻] [⚙] [×]   ← #top-row (drag zone, Y-axis)
▲max  ●avg  ▼min                                      ← stats row (color-coded)
[30m] [3h✓] [6h] [24h]              [⊟]              ← time range + compact
┌──────────────────────────────────────────────────┐
│ canvas line chart (color-coded by glucose value) │
│ time axis (bottom 16px)                          │
└──────────────────────────────────────────────────┘
```

### Color Mapping
```javascript
function glucoseColor(v) {
  if (v < 55)   return '#dc2626';  // dangerously low (red)
  if (v < 70)   return '#d97706';  // low (orange)
  if (v <= 180) return '#16a34a';  // normal (green)
  if (v <= 250) return '#d97706';  // high (orange)
  return '#dc2626';                // dangerously high (red)
}
```

### Overlays
- **Settings overlay** (`#settings-overlay`): provider selector (Dexcom / FreeStyle Libre) + respective credentials + refresh interval + display mode; pre-fills saved values; auto-closes 700ms after successful login

### Key CSS/JS Rules
- `#top-row` mousedown → JS sends `initiate_drag` (excludes `#icon-btns` area)
- `-webkit-app-region: drag/no-drag` removed (using custom drag instead)
- `html,body,#app`: `height: 100%` (ensures compact mode border-radius works)

### Chart
- Canvas 2D, DPR scaling, Y range 40–300 mg/dL
- Background zone colors (danger red / low orange / normal green / high orange / danger red)
- Reference lines: 70, 180 (gray dashed), 250 (red dashed + label)
- Line segments color-coded by midpoint value; gap on >15min intervals
- Crosshair: `_MouseTracker` injects JS `onChartMouseMove({clientX, clientY})`

---

## Data Storage

### UI State (ui_state.py)
- Path: `~/Library/Application Support/CGMMonitor/ui_state.json`
- Persists: `win_x`, `win_y` (main window position), `last_range` (chart time range in minutes), `provider` (active provider type)
- `save_window_pos(x, y)` / `load_window_pos()` → called from `_PanelDelegate.windowDidMove_` (debounced 0.4s) and `_position_window()`
- `save_range(minutes)` / `load_range()` → called from `save_range` JS action and `page_ready` handler
- `save_provider_type(str)` / `load_provider_type()` → called on provider switch / startup
- Position restore validates against `_screen_containing_point()`; falls back to default if screen not found

### Keyring (system Keychain / Credential Manager)
- Service: `CGMMonitor`
- Dexcom keys: `dexcom_username`, `dexcom_password`, `dexcom_region`, `refresh_interval`, `display_mode`
- Libre keys: `libre_email`, `libre_password`, `libre_region`

### SQLite (LocalStore)
- Path: `~/Library/Application Support/CGMMonitor/<safe_username>.db`
- Table: `readings(timestamp INTEGER PRIMARY KEY, value INTEGER, trend_arrow TEXT, trend_description TEXT)`
- `upsert(readings)`: INSERT OR REPLACE
- `load(minutes)`: query by time range, ASC order
- Prunes data older than 30 days on init

---

## Constants (constants.py)

```python
VERY_LOW=55, LOW=70, HIGH=180, VERY_HIGH=250
MAIN_WINDOW_WIDTH=300, MAIN_WINDOW_HEIGHT=220, COMPACT_WINDOW_HEIGHT=80
HISTORY_MINUTES=1440  # 24h (pydexcom max)
HISTORY_MAX_POINTS=288
REFRESH_INTERVAL_DEFAULT=300  # 5min
```

---

## Known Issues / Notes

1. **pydexcom bug**: `HEADERS` contains wrong Accept-Encoding, causes timeout behind Zscaler proxies
   → Fix: patch `pydexcom.dexcom.HEADERS` immediately after import

2. **Trend arrow always →**: pydexcom may return Flat trend constantly; root cause not yet identified

3. **24h data gap**: 2h warmup after sensor replacement, API returns no data; chart correctly shows gap

4. **`_BallView` init method**: uses multi-arg init `initWithOnClick_onQuit_onHover_value_trend_r_g_b_`; PyObjC selector must match exactly

5. **`@objc.typedSelector(b'v@:@')` for NSTimer callbacks**: PyObjC dynamically-created ObjC class methods called by the ObjC runtime (e.g. NSTimer target/selector) must be annotated with this decorator; otherwise the selector is not found and the timer silently fails

6. **CGColor in PyObjC crashes**: `NSColor.CGColor()` returns a raw `CGColorRef` (`ObjCPointer`); passing it to `CALayer.setBackgroundColor_()` causes `Trace/BPT trap: 5`. Never use `CGColor()` — use `NSColor` everywhere. NSShadow and CALayer shadow approaches also unreliable; prefer NSBezierPath drawing

7. **`screen.frame()` vs `screen.visibleFrame()`**:
   - `visibleFrame()`: for initial positioning (avoids Dock/menu bar)
   - `frame()`: for drag clamping (full screen bounds, avoids Dock-caused boundary issues)

8. **`settings_window.py`**: legacy file kept but not used (`app.py` does not import it)

9. **`ai_analyzer.py`**: legacy file kept but AI feature has been removed from `app.py`
