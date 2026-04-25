# bluetooth-twilio-intercept

A Python CLI application that bridges Bluetooth HFP phone calls (from an
Android phone paired to Windows via HFP) bidirectionally to a Twilio AI voice
number so a Twilio AI voice agent handles the conversation transparently.

---

## Features

- Detects active Bluetooth HFP audio using PyAudio
- Automatically bridges inbound call audio to a Twilio Media Stream
- Uses ngrok to expose the local WebSocket server publicly
- Bidirectional audio: caller ↔ Twilio AI agent
- Encrypted local config (no plain-text credentials)
- Real-time ASCII VU meter in the terminal
- Coloured CLI output with `colorama`
- Graceful Ctrl+C shutdown

---

## Requirements

- **Windows 10/11**
- **Python 3.9–3.12** (the stdlib `audioop` module is deprecated in 3.11 and
  removed in 3.13 — use Python ≤ 3.12 until a replacement is integrated)
- Android phone paired via Bluetooth (HFP profile enabled)
- Twilio account with a phone number pointing to an AI voice agent
- [ngrok](https://ngrok.com/) installed (or `pip install pyngrok`)

---

## Installation

```bash
pip install -r requirements.txt
```

> **PyAudio on Windows** — if `pip install pyaudio` fails, install the
> pre-built wheel from [Unofficial Windows Binaries](https://www.lfd.uci.edu/~gohlke/pythonlibs/#pyaudio)
> or use `pipwin install pyaudio`.

---

## Usage

```bash
python -m bluetooth_twilio_bridge
```

On first run you will be prompted for:
- Twilio Account SID
- Twilio Auth Token
- Your Twilio outbound phone number (FROM number)

These are saved encrypted to `bluetooth_twilio_bridge/config.json`.

### Example terminal output

```
========================================
  BLUETOOTH-TWILIO BRIDGE v1.0
========================================
  Twilio Account : AC…xxxx
  Twilio Number  : +1xxxxxxxxxx
  Bridge TO      : +15706730291
  BT Device      : Headset (Samsung Galaxy)
  Bridge Status  : LISTENING
========================================

[14:32:01] Monitoring for calls…
[14:32:45] CALL DETECTED — bridging to Twilio…
[14:32:46] WebSocket server listening on port 54321
[14:32:46] ngrok tunnel: wss://xxxx.ngrok.io
[14:32:46] Twilio call initiated: CAxxxxxxxxxxxxx
[14:32:47] WebSocket connected
[14:32:47] Audio bridge LIVE
[14:32:47] IN  ████████░░░░  67%  OUT ████░░░░░░░░  34%
[14:35:12] Call ended — bridge closed
```

---

## File Structure

```
bluetooth_twilio_bridge/
├── __init__.py
├── __main__.py      ← python -m entry point
├── main.py          ← CLI loop, banner, App class
├── config.py        ← load/save/encrypt config
├── bt_monitor.py    ← Bluetooth HFP audio detection
├── twilio_bridge.py ← Twilio REST call + TwiML + ngrok
├── audio_proc.py    ← PCM↔mulaw conversion, VU meter
├── ws_server.py     ← Local WebSocket server
└── config.json      ← Saved credentials (encrypted, git-ignored)
requirements.txt
```

---

## Configuration file

`bluetooth_twilio_bridge/config.json` is created automatically and contains
encrypted Twilio credentials plus the selected Bluetooth device index.
**Never commit this file to version control.**

---

## Dependencies

| Package | Purpose |
|---------|---------|
| `twilio` | Twilio REST SDK |
| `pyaudio` | Audio capture / playback |
| `websockets` | WebSocket server |
| `audioop` | PCM↔mulaw conversion (stdlib) |
| `requests` | ngrok API polling |
| `pyngrok` | ngrok wrapper |
| `colorama` | Coloured CLI output |
| `cryptography` | Fernet encryption for config |

---

## How it works

1. **Startup** — load/prompt credentials, validate with Twilio API.
2. **Device scan** — enumerate PyAudio devices, auto-detect HFP endpoint.
3. **Monitor loop** — continuously read audio frames from BT device, measure
   RMS energy to detect call start/end.
4. **Call start** —
   a. Open BT output stream for audio playback.  
   b. Start local WebSocket server.  
   c. Start ngrok tunnel to expose WS server publicly.  
   d. Place outbound Twilio call with TwiML `<Stream>` pointing at ngrok URL.
5. **Audio bridge** — BT mic → WebSocket → Twilio (μ-law) and
   Twilio (μ-law) → WebSocket → BT speaker (PCM).
6. **Call end** — silence detected on BT device → hang up Twilio call →
   stop WebSocket server → stop ngrok tunnel.
