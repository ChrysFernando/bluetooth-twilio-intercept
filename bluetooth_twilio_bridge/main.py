"""
main.py — Entry point for the bluetooth-twilio-bridge CLI.

Run with:
  python -m bluetooth_twilio_bridge
or:
  python bluetooth_twilio_bridge/main.py
"""

from __future__ import annotations

import sys
import threading
import time
from datetime import datetime
from typing import Optional

import pyaudio

try:
    from colorama import Fore, Style, init as colorama_init
    colorama_init(autoreset=True)
    _HAVE_COLOR = True
except ImportError:
    _HAVE_COLOR = False

    class _FakeFore:  # noqa: E302 — fallback shim
        GREEN = RED = YELLOW = CYAN = WHITE = RESET = ""

    class _FakeStyle:  # noqa: E302
        BRIGHT = DIM = RESET_ALL = ""

    Fore = _FakeFore()  # type: ignore[assignment]
    Style = _FakeStyle()  # type: ignore[assignment]

from .audio_proc import SAMPLE_RATE, SAMPLE_WIDTH, CHANNELS, CHUNK_BYTES, rms, vu_bar
from .bt_monitor import BTMonitor, find_bt_devices, list_audio_devices, select_device
from .config import load_config, prompt_credentials, save_config
from .twilio_bridge import BRIDGE_TO_NUMBER, NgrokManager, TwilioCall, validate_credentials
from .ws_server import MediaStreamServer

VERSION = "1.0"


# ── Helpers ────────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _log(msg: str, color: str = "") -> None:
    prefix = f"[{_now()}] "
    if _HAVE_COLOR and color:
        print(color + prefix + msg + Style.RESET_ALL)
    else:
        print(prefix + msg)


def _banner(cfg: dict, bt_name: str, status: str) -> None:
    sid = cfg.get("account_sid", "")
    sid_display = sid[:2] + "…" + sid[-4:] if len(sid) > 6 else sid
    from_num = cfg.get("from_number", "not set")
    line = "=" * 40
    bright = Style.BRIGHT if _HAVE_COLOR else ""
    reset = Style.RESET_ALL if _HAVE_COLOR else ""
    cyan = Fore.CYAN if _HAVE_COLOR else ""
    print(f"\n{bright}{line}")
    print(f"  BLUETOOTH-TWILIO BRIDGE v{VERSION}")
    print(line)
    print(f"  Twilio Account : {sid_display}")
    print(f"  Twilio Number  : {from_num}")
    print(f"  Bridge TO      : {BRIDGE_TO_NUMBER}")
    print(f"  BT Device      : {bt_name}")
    print(f"  Bridge Status  : {cyan}{status}")
    print(f"{reset}{bright}{line}{reset}\n")


# ── Audio output (BT speaker) ──────────────────────────────────────────────

class BTOutput:
    """Plays PCM audio back to the Bluetooth HFP device (earpiece/speaker)."""

    def __init__(self, device_index: int):
        self._device_index = device_index
        self._pa: Optional[pyaudio.PyAudio] = None
        self._stream = None

    def open(self) -> None:
        self._pa = pyaudio.PyAudio()
        self._stream = self._pa.open(
            format=self._pa.get_format_from_width(SAMPLE_WIDTH),
            channels=CHANNELS,
            rate=SAMPLE_RATE,
            output=True,
            output_device_index=self._device_index,
            frames_per_buffer=CHUNK_BYTES // SAMPLE_WIDTH,
        )

    def write(self, pcm: bytes) -> None:
        if self._stream:
            try:
                self._stream.write(pcm)
            except Exception:
                pass

    def close(self) -> None:
        if self._stream:
            try:
                self._stream.stop_stream()
                self._stream.close()
            except Exception:
                pass
            self._stream = None
        if self._pa:
            try:
                self._pa.terminate()
            except Exception:
                pass
            self._pa = None


# ── Main application ────────────────────────────────────────────────────────

class App:
    def __init__(self) -> None:
        self._cfg: dict = {}
        self._bt_device: Optional[dict] = None
        self._ngrok = NgrokManager()
        self._ws_server: Optional[MediaStreamServer] = None
        self._twilio_call: Optional[TwilioCall] = None
        self._bt_output: Optional[BTOutput] = None
        self._ws_port: int = 0
        self._call_in_progress = False
        self._in_rms: int = 0
        self._out_rms: int = 0
        self._monitor: Optional[BTMonitor] = None
        self._shutdown_event = threading.Event()

    # ── Config / startup ───────────────────────────────────────────────────

    def _load_or_prompt_config(self) -> None:
        self._cfg = load_config()
        needs_save = False

        # Missing or invalid credentials → prompt
        if not self._cfg.get("account_sid") or not self._cfg.get("auth_token"):
            print("\nFirst-time setup: Twilio credentials required.")
            self._cfg = prompt_credentials(self._cfg)
            needs_save = True

        # Validate credentials
        print("  Validating Twilio credentials…", end=" ", flush=True)
        if validate_credentials(self._cfg["account_sid"], self._cfg["auth_token"]):
            print(Fore.GREEN + "OK" if _HAVE_COLOR else "OK")
        else:
            print(Fore.RED + "FAILED" if _HAVE_COLOR else "FAILED")
            print("  Credentials appear invalid. Please re-enter.")
            self._cfg = prompt_credentials(self._cfg)
            needs_save = True
            if not validate_credentials(self._cfg["account_sid"], self._cfg["auth_token"]):
                print("  Still invalid — continuing anyway. Calls may fail.")

        if not self._cfg.get("from_number"):
            from_number = input("  Twilio FROM number (your Twilio phone number): ").strip()
            self._cfg["from_number"] = from_number
            needs_save = True

        if needs_save:
            save_config(self._cfg)
            print("  Config saved.\n")

    def _select_bt_device(self) -> bool:
        pa = pyaudio.PyAudio()
        try:
            all_devices = list_audio_devices(pa)
        finally:
            pa.terminate()

        if not all_devices:
            print(Fore.RED + "[BT] No audio input devices found." if _HAVE_COLOR
                  else "[BT] No audio input devices found.")
            return False

        # Check saved device first
        saved_idx = self._cfg.get("bt_device_index")
        if saved_idx is not None:
            matches = [d for d in all_devices if d["index"] == saved_idx]
            if matches:
                self._bt_device = matches[0]
                return True

        # Auto-detect Bluetooth devices
        bt_devices = find_bt_devices(all_devices)
        if not bt_devices:
            print("\nNo Bluetooth HFP devices found automatically.")
            print("All available input devices:")
            for d in all_devices:
                print(f"  [{d['index']}] {d['name']}")
            try:
                idx = int(input("Enter device index to use: ").strip())
                matches = [d for d in all_devices if d["index"] == idx]
                self._bt_device = matches[0] if matches else None
            except (ValueError, IndexError):
                return False
        else:
            self._bt_device = select_device(bt_devices)

        if self._bt_device:
            self._cfg["bt_device_index"] = self._bt_device["index"]
            save_config(self._cfg)
            return True

        return False

    # ── Call lifecycle ─────────────────────────────────────────────────────

    def _on_call_start(self) -> None:
        if self._call_in_progress:
            return
        self._call_in_progress = True
        _log("CALL DETECTED — bridging to Twilio…", Fore.YELLOW if _HAVE_COLOR else "")

        # Open BT output stream
        self._bt_output = BTOutput(self._bt_device["index"])
        try:
            self._bt_output.open()
        except Exception as exc:
            _log(f"[BT] Cannot open output stream: {exc}", Fore.RED if _HAVE_COLOR else "")

        # Start WebSocket server
        self._ws_server = MediaStreamServer(
            on_twilio_audio=self._on_twilio_audio,
            on_connected=self._on_ws_connected,
            on_disconnected=self._on_ws_disconnected,
        )
        self._ws_port = self._ws_server.start()
        _log(f"WebSocket server listening on port {self._ws_port}")

        # Start ngrok tunnel
        _log("Starting ngrok tunnel…")
        ws_public_url = self._ngrok.start(self._ws_port)
        if not ws_public_url:
            _log("[ngrok] Could not obtain public URL. Call bridging aborted.", Fore.RED if _HAVE_COLOR else "")
            self._cleanup_call()
            return
        _log(f"ngrok tunnel: {ws_public_url}")

        # Place Twilio call
        self._twilio_call = TwilioCall(
            self._cfg["account_sid"],
            self._cfg["auth_token"],
            self._cfg["from_number"],
        )
        try:
            call_sid = self._twilio_call.place_call(ws_public_url)
            _log(f"Twilio call initiated: {call_sid}", Fore.GREEN if _HAVE_COLOR else "")
        except Exception as exc:
            _log(f"[Twilio] Call failed: {exc}", Fore.RED if _HAVE_COLOR else "")
            self._cleanup_call()

    def _on_call_end(self) -> None:
        if not self._call_in_progress:
            return
        _log("Call ended — bridge closed", Fore.CYAN if _HAVE_COLOR else "")
        self._cleanup_call()

    def _cleanup_call(self) -> None:
        self._call_in_progress = False
        if self._twilio_call:
            self._twilio_call.hangup()
            self._twilio_call = None
        if self._ws_server:
            self._ws_server.stop()
            self._ws_server = None
        if self._ngrok:
            self._ngrok.stop()
            self._ngrok = NgrokManager()
        if self._bt_output:
            self._bt_output.close()
            self._bt_output = None

    # ── Audio callbacks ────────────────────────────────────────────────────

    def _on_bt_audio(self, pcm: bytes) -> None:
        """Forward BT audio → WebSocket → Twilio."""
        self._in_rms = rms(pcm)
        if self._ws_server:
            self._ws_server.put_bt_audio(pcm)

    def _on_bt_level(self, level: int) -> None:
        self._in_rms = level

    def _on_twilio_audio(self, pcm: bytes) -> None:
        """Forward Twilio audio → BT output."""
        self._out_rms = rms(pcm)
        if self._bt_output:
            self._bt_output.write(pcm)

    def _on_ws_connected(self) -> None:
        _log("WebSocket connected")
        _log("Audio bridge LIVE", Fore.GREEN if _HAVE_COLOR else "")

    def _on_ws_disconnected(self) -> None:
        _log("WebSocket disconnected")

    # ── VU meter display loop ──────────────────────────────────────────────

    def _vu_loop(self) -> None:
        while not self._shutdown_event.is_set():
            if self._call_in_progress:
                in_bar = vu_bar(self._in_rms)
                out_bar = vu_bar(self._out_rms)
                ts = _now()
                if _HAVE_COLOR:
                    print(
                        f"\r{Fore.CYAN}[{ts}] IN  {in_bar}  "
                        f"OUT {out_bar}{Style.RESET_ALL}",
                        end="",
                        flush=True,
                    )
                else:
                    print(f"\r[{ts}] IN  {in_bar}  OUT {out_bar}", end="", flush=True)
            time.sleep(0.25)

    # ── Main entry point ───────────────────────────────────────────────────

    def run(self) -> None:
        try:
            self._load_or_prompt_config()
        except KeyboardInterrupt:
            print("\nAborted.")
            return

        if not self._select_bt_device():
            print("No BT device selected — exiting.")
            return

        bt_name = self._bt_device["name"] if self._bt_device else "Unknown"
        _banner(self._cfg, bt_name, "LISTENING")

        # Start BT monitor
        self._monitor = BTMonitor(
            device_index=self._bt_device["index"],
            on_call_start=self._on_call_start,
            on_call_end=self._on_call_end,
            on_audio=self._on_bt_audio,
            on_level=self._on_bt_level,
        )
        self._monitor.start()

        # Start VU meter display
        vu_thread = threading.Thread(target=self._vu_loop, daemon=True, name="vu-meter")
        vu_thread.start()

        _log("Monitoring for calls…")

        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\n")
            _log("Shutting down…", Fore.YELLOW if _HAVE_COLOR else "")
        finally:
            self._shutdown_event.set()
            if self._monitor:
                self._monitor.stop()
            self._cleanup_call()
            _log("Goodbye.")


def main() -> None:
    app = App()
    app.run()


if __name__ == "__main__":
    main()
