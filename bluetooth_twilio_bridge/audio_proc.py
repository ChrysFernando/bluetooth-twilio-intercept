"""
audio_proc.py — Audio conversion utilities and ASCII VU meter.

All conversion uses stdlib audioop so there are no extra dependencies.
"""

from __future__ import annotations

import audioop
import struct

# ── Constants ──────────────────────────────────────────────────────────────
SAMPLE_RATE = 8000          # Hz
CHANNELS = 1                # mono
SAMPLE_WIDTH = 2            # bytes (16-bit PCM)
CHUNK_BYTES = 160           # 20 ms @ 8 kHz, 16-bit, mono  (160 bytes = 80 samples)
SILENCE_THRESHOLD = 300     # RMS below this ⇒ silence
SILENCE_DURATION = 3.0      # seconds of silence before declaring call ended

# ── PCM ↔ μ-law ────────────────────────────────────────────────────────────

def pcm_to_mulaw(pcm_bytes: bytes) -> bytes:
    """Convert 16-bit linear PCM to 8-bit μ-law."""
    return audioop.lin2ulaw(pcm_bytes, SAMPLE_WIDTH)


def mulaw_to_pcm(mulaw_bytes: bytes) -> bytes:
    """Convert 8-bit μ-law to 16-bit linear PCM."""
    return audioop.ulaw2lin(mulaw_bytes, SAMPLE_WIDTH)


# ── Audio energy ───────────────────────────────────────────────────────────

def rms(pcm_bytes: bytes) -> int:
    """Return RMS energy level of a PCM frame."""
    if not pcm_bytes:
        return 0
    return audioop.rms(pcm_bytes, SAMPLE_WIDTH)


def is_silence(pcm_bytes: bytes, threshold: int = SILENCE_THRESHOLD) -> bool:
    return rms(pcm_bytes) < threshold


# ── ASCII VU meter ─────────────────────────────────────────────────────────
_VU_WIDTH = 12   # number of bar characters
_VU_MAX_RMS = 8000


def vu_bar(rms_val: int, width: int = _VU_WIDTH) -> str:
    """Return an ASCII progress bar string for *rms_val*."""
    pct = min(rms_val / _VU_MAX_RMS, 1.0)
    filled = round(pct * width)
    bar = "█" * filled + "░" * (width - filled)
    return f"{bar} {round(pct * 100):3d}%"


# ── Resampling helper (trivial nearest-neighbour for rate conversion) ──────

def resample(pcm_bytes: bytes, from_rate: int, to_rate: int) -> bytes:
    """Very simple rate conversion using audioop.ratecv."""
    if from_rate == to_rate:
        return pcm_bytes
    converted, _ = audioop.ratecv(
        pcm_bytes, SAMPLE_WIDTH, CHANNELS, from_rate, to_rate, None
    )
    return converted
