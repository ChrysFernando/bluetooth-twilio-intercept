"""
bt_monitor.py — Bluetooth HFP audio detection.

Uses PyAudio to enumerate Windows audio devices and identify HFP/Headset
endpoints.  Monitors the selected device for active call audio by measuring
RMS energy on a background thread and fires callbacks when a call starts or
ends.
"""

from __future__ import annotations

import queue
import threading
import time
from typing import Callable, Optional

import pyaudio

from .audio_proc import (
    CHANNELS,
    CHUNK_BYTES,
    SAMPLE_RATE,
    SAMPLE_WIDTH,
    SILENCE_DURATION,
    SILENCE_THRESHOLD,
    is_silence,
    rms,
)

# Keywords used to identify Bluetooth HFP devices
_HFP_KEYWORDS = ("hands-free", "hfp", "headset", "bluetooth")


def list_audio_devices(pa: pyaudio.PyAudio) -> list[dict]:
    """Return all available input audio devices."""
    devices = []
    for i in range(pa.get_device_count()):
        info = pa.get_device_info_by_index(i)
        if info.get("maxInputChannels", 0) > 0:
            devices.append({"index": i, "name": info["name"], "info": info})
    return devices


def find_bt_devices(devices: list[dict]) -> list[dict]:
    """Filter *devices* to those that look like Bluetooth HFP endpoints."""
    result = []
    for d in devices:
        name_lower = d["name"].lower()
        if any(kw in name_lower for kw in _HFP_KEYWORDS):
            result.append(d)
    return result


def select_device(devices: list[dict]) -> Optional[dict]:
    """
    Interactively ask the user to pick a device from *devices*.
    Returns the selected device dict, or None if the list is empty.
    """
    if not devices:
        return None
    if len(devices) == 1:
        return devices[0]

    print("\nMultiple Bluetooth audio devices found:")
    for i, d in enumerate(devices, 1):
        print(f"  [{i}] {d['name']} (index {d['index']})")

    while True:
        try:
            choice = int(input("Select device number: ").strip())
            if 1 <= choice <= len(devices):
                return devices[choice - 1]
        except ValueError:
            pass
        print("  Invalid selection, try again.")


# ── Monitor ────────────────────────────────────────────────────────────────


class BTMonitor:
    """
    Continuously reads audio from a PyAudio input stream and detects
    call start / end events based on RMS energy threshold.

    Callbacks
    ---------
    on_call_start() — called when audio energy is first detected
    on_call_end()   — called when audio is silent for SILENCE_DURATION seconds
    on_audio(pcm)   — called for every audio chunk while a call is active
                      (raw 16-bit PCM, 160 bytes / 20 ms)
    on_level(rms)   — called for every chunk with the current RMS level
    """

    def __init__(
        self,
        device_index: int,
        on_call_start: Callable[[], None],
        on_call_end: Callable[[], None],
        on_audio: Callable[[bytes], None],
        on_level: Callable[[int], None],
    ):
        self.device_index = device_index
        self._on_call_start = on_call_start
        self._on_call_end = on_call_end
        self._on_audio = on_audio
        self._on_level = on_level

        self._pa: Optional[pyaudio.PyAudio] = None
        self._stream: Optional[pyaudio.Stream] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._call_active = False
        self._silence_start: Optional[float] = None

    # ── Public API ─────────────────────────────────────────────────────────

    def start(self) -> None:
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="bt-monitor")
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)

    @property
    def call_active(self) -> bool:
        return self._call_active

    # ── Internal ───────────────────────────────────────────────────────────

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._open_stream()
                self._read_loop()
            except OSError as exc:
                if self._stop_event.is_set():
                    break
                print(f"\n[BT] Device error: {exc} — waiting for reconnect…")
                self._close_stream()
                time.sleep(3)
            finally:
                self._close_stream()

    def _open_stream(self) -> None:
        self._pa = pyaudio.PyAudio()
        self._stream = self._pa.open(
            format=self._pa.get_format_from_width(SAMPLE_WIDTH),
            channels=CHANNELS,
            rate=SAMPLE_RATE,
            input=True,
            input_device_index=self.device_index,
            frames_per_buffer=CHUNK_BYTES // SAMPLE_WIDTH,
        )

    def _close_stream(self) -> None:
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

    def _read_loop(self) -> None:
        frames_per_buffer = CHUNK_BYTES // SAMPLE_WIDTH
        while not self._stop_event.is_set():
            try:
                pcm = self._stream.read(frames_per_buffer, exception_on_overflow=False)
            except OSError:
                raise

            level = rms(pcm)
            self._on_level(level)

            silent = is_silence(pcm, SILENCE_THRESHOLD)

            if not self._call_active:
                if not silent:
                    self._call_active = True
                    self._silence_start = None
                    self._on_call_start()
            else:
                if silent:
                    if self._silence_start is None:
                        self._silence_start = time.monotonic()
                    elif time.monotonic() - self._silence_start >= SILENCE_DURATION:
                        self._call_active = False
                        self._silence_start = None
                        self._on_call_end()
                else:
                    self._silence_start = None
                    self._on_audio(pcm)
