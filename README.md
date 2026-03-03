# CGM Monitor

A macOS menu bar app for real-time blood glucose monitoring via the Dexcom Share API.

## Features

- Menu bar display with live glucose value + trend arrow (e.g. `105 →`)
- Floating window: large glucose reading, 3-hour trend chart
- Color-coded alerts: red / orange / green
- macOS system notifications (low glucose, dangerously high)
- 5-minute auto-refresh with configurable interval
- Credentials stored securely in macOS Keychain
- **Hover mode**: minimizes to a 44×44 floating ball; expands on hover/click
- **Electron build**: runs on both macOS and Windows

## Prerequisites

### Dexcom Setup
1. Open the Dexcom app on your phone and enable **Share**
2. If you log in to Dexcom with a Google account, you need a **Dexcom-specific password**:
   - Visit [account.dexcom.com](https://account.dexcom.com)
   - Use "Forgot Password" to reset via email
   - This password is separate from your Google account password

## Running (macOS native)

### Requirements
- macOS 12+
- Python 3.9+

### Install

```bash
# Must use python3 -m venv (not virtualenv)
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### Start

```bash
python3 main.py
```

On first launch the settings overlay opens automatically. Enter your Dexcom credentials and click Save.

## Running (Electron — macOS & Windows)

### Requirements
- Node.js 18+
- Python 3.9+

### Install

```bash
cd electron
npm install
pip install -r ../requirements-electron.txt
```

### Start

```bash
cd electron
npm start
```

## File Structure

```
cgm/
├── main.py                # Entry point (macOS native)
├── app.py                 # Core app (menu bar, refresh scheduler, hover mode)
├── html_window.py         # Floating window (WKWebView)
├── floating_ball.py       # Floating ball NSPanel
├── dexcom_client.py       # Dexcom API client + credential manager
├── bridge.py              # Python sidecar for Electron (JSON Lines over stdio)
├── local_store.py         # SQLite local storage
├── models.py              # GlucoseReading dataclass
├── constants.py           # Thresholds, colors, window dimensions
├── logger.py              # Logging setup
├── electron/
│   ├── main.js            # Electron main process
│   ├── preload.js         # Context bridge
│   ├── ball.html          # Floating ball window
│   └── package.json
├── ui/
│   └── index.html         # Main window UI (HTML/CSS/JS)
├── requirements.txt       # macOS native dependencies
└── requirements-electron.txt  # Cross-platform dependencies
```

## Glucose Color Reference

| Range | Color | Meaning |
|-------|-------|---------|
| < 55 mg/dL | Red | Dangerously low |
| 55–70 mg/dL | Orange | Low |
| 70–180 mg/dL | Green | Normal |
| 180–250 mg/dL | Orange | High |
| > 250 mg/dL | Red | Dangerously high |
