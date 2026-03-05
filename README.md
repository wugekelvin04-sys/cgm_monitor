# CGM Monitor

A macOS menu bar app for real-time blood glucose monitoring. Supports **Dexcom** and **FreeStyle Libre 2/3** as data sources, switchable from the settings panel without restarting.

## Features

- Menu bar display with live glucose value + trend arrow (e.g. `105 →`)
- Floating window: large glucose reading, multi-day trend chart with comparison lines
- Color-coded status: red / orange / green based on configurable thresholds
- macOS system notifications with 15-minute cooldown (very low, low, critically high, rapid drop)
- Configurable refresh interval (1 / 2 / 5 min)
- Credentials stored securely in macOS Keychain
- **Hover mode**: minimizes to a 44×44 floating ball; expands on hover/click
- **Electron build**: runs on both macOS and Windows (Dexcom only)

## Supported CGM Providers

| Provider | Library | Region |
|----------|---------|--------|
| Dexcom G6/G7 | pydexcom | US / OUS |
| FreeStyle Libre 2/3 | pylibrelinkup | US / EU |

Switch providers in **Settings → Data Source** at any time. Each provider's credentials are stored independently in the system keychain.

### Dexcom Setup
1. Open the Dexcom app on your phone and enable **Share**
2. If you log in to Dexcom with a Google account, you need a **Dexcom-specific password**:
   - Visit [account.dexcom.com](https://account.dexcom.com)
   - Use "Forgot Password" to reset via email

### FreeStyle Libre Setup
1. You need a **LibreLink Up** account (the companion sharing app)
2. In the LibreLinkUp app, accept the invitation from the sensor owner (or you are the owner)
3. Use the same email + password as your LibreLinkUp account in the settings panel

## Running (macOS native)

### Requirements
- macOS 12+
- Python 3.11+ (required by pylibrelinkup)

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

On first launch the settings overlay opens automatically. Select your data source, enter credentials, and click **Save & Login**.

## Running (Electron — macOS & Windows)

> Note: Electron build currently supports Dexcom only.

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
├── app.py                 # Core app (menu bar, refresh scheduler, provider dispatch)
├── html_window.py         # Floating window (WKWebView)
├── floating_ball.py       # Floating ball NSPanel
├── dexcom_client.py       # Dexcom API client + CredentialManager (shared settings)
├── libre_client.py        # FreeStyle Libre client (pylibrelinkup, duck-typed)
├── bridge.py              # Python sidecar for Electron (JSON Lines over stdio)
├── local_store.py         # SQLite local storage (per-user, 30-day retention)
├── models.py              # GlucoseReading dataclass
├── constants.py           # Thresholds, colors, keyring keys, window dimensions
├── ui_state.py            # Lightweight JSON state (window pos, range, provider)
├── logger.py              # Logging setup
├── electron/
│   ├── main.js            # Electron main process
│   ├── preload.js         # Context bridge
│   ├── ball.html          # Floating ball window
│   └── package.json
├── ui/
│   └── index.html         # Shared UI (HTML/CSS/JS, provider selector included)
├── requirements.txt       # macOS native dependencies
└── requirements-electron.txt  # Cross-platform dependencies (Dexcom only)
```

## Glucose Color Reference

| Range | Color | Meaning |
|-------|-------|---------|
| < 55 mg/dL | Red | Dangerously low |
| 55–70 mg/dL | Orange | Low |
| 70–180 mg/dL | Green | Normal |
| 180–250 mg/dL | Orange | High |
| > 250 mg/dL | Red | Dangerously high |

Default thresholds (70 / 180 / 250 mg/dL) are configurable in Settings. Values are stored in the system keychain and persist across restarts.
