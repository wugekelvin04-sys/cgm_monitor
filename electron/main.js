'use strict'
const { app, BrowserWindow, Tray, Menu, ipcMain, screen, nativeImage, Notification } = require('electron')
const path = require('path')
const { spawn } = require('child_process')
const readline = require('readline')

// ── Constants ──────────────────────────────────────────────────────────────────
const MAIN_W    = 300
const MAIN_H    = 220
const COMPACT_H = 80
const BALL_W    = 72
const BALL_H    = 72
const BALL_PAD  = 14  // transparent padding around visual ball for glow
const MARGIN    = 20

// ── State ──────────────────────────────────────────────────────────────────────
let mainWindow   = null
let ballWindow   = null
let tray         = null
let python       = null
let displayMode  = 'window'   // 'window' | 'hover'
let glucoseUnit  = 'mgdl'     // 'mgdl' | 'mmol'
let currentInterval = 120     // refresh interval in seconds
let currentUsername = ''
let currentPassword = ''
let currentOus      = false
let ballAlertActive    = false
let currentProvider    = 'dexcom'
let currentLibreEmail  = ''
let currentLibreRegion = 'US'
let currentThreshLow    = 70
let currentThreshHigh   = 180
let currentThreshAlert  = 250
let currentAlertEnabled = true
let settingsOpen = false
let collapseTimer = null
let currentMainH = MAIN_H   // Track actual main window height (changes in compact mode)

// Drag: main window
let dragStartScreenX  = 0
let dragStartScreenY  = 0
let dragStartWindowX  = 0
let dragStartWindowY  = 0

// Drag: floating ball
let ballDragStartScreenY = 0
let ballDragStartWindowY = 0

// ── Utilities ──────────────────────────────────────────────────────────────────
function primaryDisplay() { return screen.getPrimaryDisplay() }

function fixedX(width) {
  const d = primaryDisplay()
  return d.bounds.x + d.bounds.width - width - MARGIN
}

function clampX(x, width) {
  const d = primaryDisplay()
  return Math.max(d.bounds.x, Math.min(x, d.bounds.x + d.bounds.width - width))
}

function clampY(y, height) {
  const d = primaryDisplay()
  return Math.max(d.bounds.y, Math.min(y, d.bounds.y + d.bounds.height - height))
}

function pyPath() {
  // In packaged app, python is at resources/python/bridge.py; in dev, it's in the parent directory
  if (app.isPackaged) {
    return path.join(process.resourcesPath, 'python', 'bridge.py')
  }
  return path.join(__dirname, '..', 'bridge.py')
}

function uiPath(file) {
  if (app.isPackaged) {
    return path.join(process.resourcesPath, 'python', 'ui', file)
  }
  return path.join(__dirname, '..', 'ui', file)
}

// ── Python sidecar ────────────────────────────────────────────────────────────
function startPython() {
  const exe = process.platform === 'win32' ? 'python' : 'python3'
  python = spawn(exe, [pyPath()], { stdio: ['pipe', 'pipe', 'pipe'] })

  const rl = readline.createInterface({ input: python.stdout })
  rl.on('line', (line) => {
    try { onPythonMsg(JSON.parse(line)) } catch (_) {}
  })

  python.stderr.on('data', (d) => process.stderr.write('[py] ' + d))
  python.on('exit', (code) => console.log('Python exited:', code))

  // Credentials are requested after page_ready to avoid race condition
  // (page must register IPC listeners before we send show_settings)
}

function toPython(msg) {
  if (python && python.stdin.writable) {
    python.stdin.write(JSON.stringify(msg) + '\n')
  }
}

// ── Python message handling ───────────────────────────────────────────────────
function onPythonMsg(msg) {
  switch (msg.type) {

    case 'credentials':
      displayMode        = msg.display_mode || 'window'
      glucoseUnit        = msg.unit || 'mgdl'
      currentInterval    = msg.interval || 120
      currentUsername    = msg.username || ''
      currentPassword    = msg.password || ''
      currentOus         = !!msg.ous
      currentProvider    = msg.provider_type || 'dexcom'
      currentLibreEmail  = msg.libre_email  || ''
      currentLibreRegion = msg.libre_region || 'US'
      currentThreshLow    = msg.thresh_low    ?? 70
      currentThreshHigh   = msg.thresh_high   ?? 180
      currentThreshAlert  = msg.thresh_alert  ?? 250
      currentAlertEnabled = msg.alert_enabled ?? true
      if (msg.has_credentials) {
        toPython({ type: 'login_from_keychain' })
      } else {
        showMain()
        toRenderer('show_settings', {
          provider_type:  currentProvider,
          username:       msg.username || '',
          ous:            msg.ous,
          libre_email:    currentLibreEmail,
          libre_region:   currentLibreRegion,
          interval:       msg.interval,
          display_mode:   msg.display_mode,
          unit:           msg.unit || 'mgdl',
          thresh_low:     currentThreshLow,
          thresh_high:    currentThreshHigh,
          thresh_alert:   currentThreshAlert,
          alert_enabled:  currentAlertEnabled,
        })
      }
      break

    case 'login_result':
      if (msg.success) {
        if (displayMode === 'hover') {
          hideMain()
          const d = primaryDisplay()
          showBall(d.workArea.y + 10)
        } else {
          showMain()
        }
      } else if (msg.from_keychain) {
        // Auto-login failed -> show settings
        showMain()
        toRenderer('show_settings', {})
      }
      break

    case 'glucose_data':
      if (msg.data.thresh_low    != null) currentThreshLow    = msg.data.thresh_low
      if (msg.data.thresh_high   != null) currentThreshHigh   = msg.data.thresh_high
      if (msg.data.thresh_alert  != null) currentThreshAlert  = msg.data.thresh_alert
      if (msg.data.alert_enabled != null) currentAlertEnabled = msg.data.alert_enabled
      toRenderer('update_glucose', { ...msg.data, unit: glucoseUnit })
      toBall('update_ball', {
        ...msg.data,
        alert:       ballAlertActive,
        thresh_low:  currentThreshLow,
        thresh_high: currentThreshHigh,
        thresh_alert: currentThreshAlert,
      })
      updateTray(msg.data)
      break

    case 'save_credentials_result':
      toRenderer('settings_result', {
        type: 'save_dexcom',
        success: msg.success,
        message: msg.success ? 'Logged in' : 'Login failed. Check credentials.',
      })
      if (msg.success && displayMode === 'hover') {
        setTimeout(minimizeToBall, 700)
      }
      break

    case 'test_credentials_result':
      toRenderer('settings_result', {
        type: 'test_dexcom',
        success: msg.success,
        message: msg.message,
      })
      break

    case 'save_libre_result':
      toRenderer('settings_result', {
        type: 'save_libre',
        success: msg.success,
        message: msg.success ? 'Logged in' : 'Login failed. Check credentials.',
      })
      if (msg.success && displayMode === 'hover') {
        setTimeout(minimizeToBall, 700)
      }
      break

    case 'test_libre_result':
      toRenderer('settings_result', {
        type: 'test_libre',
        success: msg.success,
        message: msg.message,
      })
      break

    case 'save_display_result':
      toRenderer('settings_result', { type: 'save_display', success: true, message: 'Saved' })
      break

    case 'logout_done':
      showMain()
      toRenderer('show_settings', {})
      break

    case 'glucose_alert':
      if (Notification.isSupported()) {
        new Notification({ title: msg.title, body: msg.body, silent: false }).show()
      }
      break

    case 'set_alert_ui':
      ballAlertActive = !!msg.active
      toRenderer('set_alert_ui', { active: msg.active })
      toBall('set_alert_ui', { active: msg.active })
      break

    case 'save_thresholds_result':
      toRenderer('settings_result', { type: 'save_thresholds', success: true, message: 'Saved' })
      break

    case 'error':
      console.error('[bridge error]', msg.message)
      break
  }
}

// ── Window: main window ─────────────────────────────────────────────────────
function createMain() {
  const d = primaryDisplay()
  mainWindow = new BrowserWindow({
    width:  MAIN_W,
    height: MAIN_H,
    x: fixedX(MAIN_W),
    y: d.workArea.y + 10,
    frame:       false,
    transparent: true,
    alwaysOnTop: true,
    resizable:   false,
    skipTaskbar: true,
    hasShadow:   true,
    show:        false,
    webPreferences: {
      preload:          path.join(__dirname, 'preload.js'),
      contextIsolation: true,
    },
  })
  mainWindow.loadFile(uiPath('index.html'))
  mainWindow.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true })
  mainWindow.setAlwaysOnTop(true, 'floating')
  mainWindow.on('closed', () => { mainWindow = null })
}

function showMain() {
  if (!mainWindow) createMain()
  mainWindow.show()
}

function hideMain() {
  if (mainWindow) mainWindow.hide()
}

function toRenderer(channel, data) {
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.webContents.send(channel, data)
  }
}

// ── Window: floating ball ─────────────────────────────────────────────────────
function createBall() {
  const d = primaryDisplay()
  ballWindow = new BrowserWindow({
    width:  BALL_W,
    height: BALL_H,
    x: fixedX(BALL_W - BALL_PAD),
    y: d.workArea.y + 10,
    frame:       false,
    transparent: true,
    alwaysOnTop: true,
    resizable:   false,
    skipTaskbar: true,
    hasShadow:   false,
    show:        false,
    webPreferences: {
      preload:          path.join(__dirname, 'preload.js'),
      contextIsolation: true,
    },
  })
  ballWindow.loadFile(path.join(__dirname, 'ball.html'))
  ballWindow.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true })
  ballWindow.setAlwaysOnTop(true, 'floating')
  ballWindow.on('closed', () => { ballWindow = null })
}

function showBall(y) {
  if (!ballWindow) createBall()
  if (y !== undefined) {
    ballWindow.setPosition(fixedX(BALL_W - BALL_PAD), clampY(y, BALL_H))
  }
  ballWindow.show()
}

function hideBall() {
  if (ballWindow) ballWindow.hide()
}

function toBall(channel, data) {
  if (ballWindow && !ballWindow.isDestroyed()) {
    ballWindow.webContents.send(channel, { ...data, display_value: fmtGlucose(data.value) })
  }
}

// ── Expand / Collapse ───────────────────────────────────────────────────────
function minimizeToBall() {
  const bounds = mainWindow ? mainWindow.getBounds() : null
  // Trigger JS collapse animation first; when animation ends JS sends collapse_done -> then switch windows
  toRenderer('do_collapse', {})
  // Save target ball Y, to be used when collapse_done arrives
  mainWindow._pendingBallY = bounds
    ? bounds.y
    : primaryDisplay().workArea.y + 10
}

function expandFromBall() {
  const ballBounds = ballWindow ? ballWindow.getBounds() : null
  const mainH = currentMainH
  hideBall()
  showMain()
  if (ballBounds && mainWindow) {
    const winX = ballBounds.x + (BALL_W - BALL_PAD) - MAIN_W
    mainWindow.setPosition(winX, clampY(ballBounds.y, currentMainH))
  }
  toRenderer('do_expand', {})
}

// ── Hover collapse scheduling ────────────────────────────────────────────────
function scheduleCollapse(delay) {
  clearCollapseTimer()
  collapseTimer = setTimeout(() => {
    collapseTimer = null
    if (mainWindow && mainWindow.isVisible()) minimizeToBall()
  }, delay)
}

function clearCollapseTimer() {
  if (collapseTimer) { clearTimeout(collapseTimer); collapseTimer = null }
}

// ── System tray ──────────────────────────────────────────────────────────────
function createTray() {
  let icon
  if (process.platform === 'win32') {
    const iconPath = app.isPackaged
      ? path.join(process.resourcesPath, 'icon.png')
      : path.join(__dirname, 'icon.png')
    icon = nativeImage.createFromPath(iconPath)
  } else {
    icon = nativeImage.createEmpty()
  }
  tray = new Tray(icon)
  if (process.platform !== 'win32') tray.setTitle('⏳')
  tray.setToolTip('CGM Monitor')
  tray.setContextMenu(Menu.buildFromTemplate([
    { label: 'Show / Hide', click: () => mainWindow?.isVisible() ? hideMain() : showMain() },
    { label: 'Refresh Now', click: () => toPython({ type: 'force_refresh' }) },
    { type: 'separator' },
    { label: 'Quit', click: () => app.quit() },
  ]))
  tray.on('click', () => mainWindow?.isVisible() ? hideMain() : showMain())
}

function fmtGlucose(mgdl) {
  return glucoseUnit === 'mmol' ? (mgdl / 18.0182).toFixed(1) : String(mgdl)
}

function updateTray(data) {
  if (!tray) return
  const label = `${fmtGlucose(data.value)} ${data.trend || ''}`.trim()
  if (process.platform === 'win32') {
    tray.setToolTip(`CGM: ${label}`)
  } else {
    tray.setTitle(` ${label}`)
  }
}

// ── IPC: unified entry point (main window + floating ball share preload, distinguished by sender) ───────────
ipcMain.on('cgm-msg', (event, msg) => {
  const fromBall = ballWindow && event.sender === ballWindow.webContents

  if (fromBall) {
    handleBallMsg(msg)
  } else {
    handleMainMsg(msg)
  }
})

function handleBallMsg(msg) {
  switch (msg.action) {
    case 'ball_click':
      if (displayMode === 'hover') clearCollapseTimer()
      expandFromBall()
      break
    case 'ball_hover':
      if (displayMode === 'hover') { clearCollapseTimer(); expandFromBall() }
      break
    case 'ball_contextmenu':
      Menu.buildFromTemplate([
        { label: 'Refresh Now', click: () => toPython({ type: 'force_refresh' }) },
        { type: 'separator' },
        { label: 'Quit', click: () => app.quit() },
      ]).popup({ window: ballWindow })
      break
    case 'ball_drag_start':
      ballDragStartScreenY = msg.screenY
      ballDragStartWindowY = ballWindow ? ballWindow.getPosition()[1] : 0
      break
    case 'ball_drag_move':
      if (ballWindow) {
        const dy   = msg.screenY - ballDragStartScreenY
        const newY = clampY(ballDragStartWindowY + dy, BALL_H)
        ballWindow.setPosition(fixedX(BALL_W - BALL_PAD), newY)
      }
      break
  }
}

function handleMainMsg(msg) {
  switch (msg.action) {
    case 'page_ready':
      toPython({ type: 'load_credentials' })
      break
    case 'minimize_to_ball':
      minimizeToBall()
      break
    case 'collapse_done':
      // JS animation ended -> actually switch windows
      if (mainWindow) {
        const targetY = mainWindow._pendingBallY ?? (primaryDisplay().workArea.y + 10)
        mainWindow._pendingBallY = null
        hideMain()
        showBall(targetY)
      }
      break
    case 'open_settings':
      showMain()
      toRenderer('show_settings', {
        provider_type:  currentProvider,
        username:       currentUsername,
        password:       currentPassword,
        ous:            currentOus,
        libre_email:    currentLibreEmail,
        libre_region:   currentLibreRegion,
        interval:       currentInterval,
        display_mode:   displayMode,
        unit:           glucoseUnit,
        thresh_low:     currentThreshLow,
        thresh_high:    currentThreshHigh,
        thresh_alert:   currentThreshAlert,
        alert_enabled:  currentAlertEnabled,
      })
      break
    case 'settings_open':
      settingsOpen = true
      clearCollapseTimer()
      break
    case 'settings_close':
      settingsOpen = false
      if (displayMode === 'hover') scheduleCollapse(1000)
      break
    case 'window_mouse_enter':
      if (displayMode === 'hover') clearCollapseTimer()
      break
    case 'window_mouse_leave':
      if (displayMode === 'hover' && !settingsOpen) scheduleCollapse(300)
      break
    case 'force_refresh':
      toPython({ type: 'force_refresh' })
      break
    case 'save_dexcom':
      currentUsername = msg.username || ''
      currentPassword = msg.password || ''
      currentOus      = !!msg.ous
      currentProvider = 'dexcom'
      toPython({ type: 'save_credentials', username: msg.username, password: msg.password, ous: msg.ous })
      break
    case 'test_dexcom':
      toPython({ type: 'test_credentials', username: msg.username, password: msg.password, ous: msg.ous })
      break
    case 'save_libre':
      currentLibreEmail  = msg.email  || ''
      currentLibreRegion = msg.is_eu ? 'EU' : 'US'
      currentProvider    = 'freestyle_libre'
      toPython({ type: 'save_libre', email: msg.email, password: msg.password, is_eu: msg.is_eu })
      break
    case 'test_libre':
      toPython({ type: 'test_libre', email: msg.email, password: msg.password, is_eu: msg.is_eu })
      break
    case 'save_display':
      displayMode          = msg.display_mode || 'window'
      glucoseUnit          = msg.unit || 'mgdl'
      currentInterval      = msg.interval || 120
      currentThreshLow     = msg.thresh_low    ?? currentThreshLow
      currentThreshHigh    = msg.thresh_high   ?? currentThreshHigh
      currentThreshAlert   = msg.thresh_alert  ?? currentThreshAlert
      currentAlertEnabled  = msg.alert_enabled ?? currentAlertEnabled
      toPython({
        type: 'save_display',
        interval: msg.interval, display_mode: msg.display_mode, unit: msg.unit || 'mgdl',
        thresh_low: currentThreshLow, thresh_high: currentThreshHigh,
        thresh_alert: currentThreshAlert, alert_enabled: currentAlertEnabled,
      })
      break
    case 'set_compact': {
      const h = msg.compact ? COMPACT_H : MAIN_H
      currentMainH = h
      if (mainWindow) {
        const [wx, wy] = mainWindow.getPosition()
        // setBounds is atomic (avoids flicker from separate setSize + setPosition)
        // Pin top edge: Y stays fixed, window grows/shrinks downward
        mainWindow.setBounds({ x: wx, y: wy, width: MAIN_W, height: h })
      }
      // Notify renderer AFTER resize so CSS layout change happens at the right size
      toRenderer('compact_applied', { compact: msg.compact })
      break
    }
    case 'save_thresholds':
      currentThreshLow    = msg.thresh_low    ?? currentThreshLow
      currentThreshHigh   = msg.thresh_high   ?? currentThreshHigh
      currentThreshAlert  = msg.thresh_alert  ?? currentThreshAlert
      currentAlertEnabled = msg.alert_enabled ?? currentAlertEnabled
      toPython({
        type: 'save_thresholds',
        thresh_low:    currentThreshLow,
        thresh_high:   currentThreshHigh,
        thresh_alert:  currentThreshAlert,
        alert_enabled: currentAlertEnabled,
      })
      break
    case 'logout':
      toPython({ type: 'logout' })
      break
    case 'initiate_drag':
      // Electron uses cgm-drag-start/move instead
      break
  }
}

// Main window drag (X and Y)
ipcMain.on('cgm-drag-start', (_event, screenX, screenY) => {
  const [wx, wy] = mainWindow ? mainWindow.getPosition() : [0, 0]
  dragStartScreenX = screenX
  dragStartScreenY = screenY
  dragStartWindowX = wx
  dragStartWindowY = wy
})

ipcMain.on('cgm-drag-move', (_event, screenX, screenY) => {
  if (!mainWindow) return
  const newX = clampX(dragStartWindowX + (screenX - dragStartScreenX), MAIN_W)
  const newY = clampY(dragStartWindowY + (screenY - dragStartScreenY), mainWindow.getBounds().height)
  mainWindow.setPosition(newX, newY)
})

// ── App lifecycle ──────────────────────────────────────────────────────────────
app.whenReady().then(() => {
  createMain()
  createBall()
  createTray()
  startPython()
})

// Keep running in the background, do not quit when all windows are closed
app.on('window-all-closed', (e) => e.preventDefault())

app.on('before-quit', () => {
  if (python) { python.kill(); python = null }
})
