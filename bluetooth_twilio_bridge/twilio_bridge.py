"""
twilio_bridge.py — Twilio REST API call + TwiML generation + ngrok management.
"""

from __future__ import annotations

import socket
import subprocess
import sys
import time
from typing import Optional

import requests
from twilio.rest import Client
from twilio.twiml.voice_response import VoiceResponse, Start, Stream

# Target number that the Twilio AI voice agent listens on (hardcoded per spec)
BRIDGE_TO_NUMBER = "+15706730291"

# How long (seconds) to keep a Twilio call alive while streaming over WebSocket
MAX_CALL_DURATION_SECONDS = 120


# ── ngrok helpers ──────────────────────────────────────────────────────────

def _find_free_port() -> int:
    # Bind to all interfaces with port 0 to let the OS assign a free port.
    # The resulting port number is used locally; only the ngrok tunnel is public.
    with socket.socket() as s:
        s.bind(("", 0))
        return s.getsockname()[1]


def _ngrok_api_url() -> str:
    return "http://127.0.0.1:4040/api/tunnels"


def get_ngrok_tunnel_url(port: int, timeout: float = 15.0) -> Optional[str]:
    """
    Poll the ngrok local API until a tunnel for *port* appears.
    Returns the public wss:// URL or None on timeout.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            resp = requests.get(_ngrok_api_url(), timeout=3)
            tunnels = resp.json().get("tunnels", [])
            for t in tunnels:
                addr = t.get("config", {}).get("addr", "")
                if f":{port}" in addr:
                    public_url = t.get("public_url", "")
                    # Convert http:// → ws://, https:// → wss://
                    if public_url.startswith("https://"):
                        return public_url.replace("https://", "wss://", 1)
                    if public_url.startswith("http://"):
                        return public_url.replace("http://", "ws://", 1)
                    return public_url
        except Exception:
            pass
        time.sleep(0.5)
    return None


class NgrokManager:
    """Launch and stop an ngrok tunnel for a given local port."""

    def __init__(self) -> None:
        self._proc: Optional[subprocess.Popen] = None
        self._public_url: Optional[str] = None

    def start(self, port: int) -> Optional[str]:
        """
        Try to start ngrok (via pyngrok or shell) for *port*.
        Returns the public wss:// URL, or None if ngrok is unavailable.
        """
        # Prefer pyngrok if available
        try:
            from pyngrok import ngrok as _ngrok, conf as _ngrok_conf

            tunnel = _ngrok.connect(port, "http")
            url = tunnel.public_url
            if url.startswith("https://"):
                url = url.replace("https://", "wss://", 1)
            elif url.startswith("http://"):
                url = url.replace("http://", "ws://", 1)
            self._public_url = url
            return url
        except ImportError:
            pass
        except Exception as exc:
            print(f"[ngrok] pyngrok error: {exc}")

        # Fall back to calling ngrok binary directly
        try:
            self._proc = subprocess.Popen(
                ["ngrok", "http", str(port)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            print(
                "\n[ngrok] ngrok not found. Install it with:\n"
                "  pip install pyngrok\n"
                "or download from https://ngrok.com/download\n"
            )
            return None

        self._public_url = get_ngrok_tunnel_url(port)
        return self._public_url

    def stop(self) -> None:
        try:
            from pyngrok import ngrok as _ngrok

            _ngrok.kill()
        except Exception:
            pass
        if self._proc:
            self._proc.terminate()
            self._proc = None

    @property
    def public_url(self) -> Optional[str]:
        return self._public_url


# ── TwiML builder ──────────────────────────────────────────────────────────

def build_twiml(ws_url: str) -> str:
    """
    Build TwiML that connects a call to a Media Stream WebSocket.

    Example output:
      <Response>
        <Start>
          <Stream url="wss://..."/>
        </Start>
        <Pause length="60"/>
      </Response>
    """
    response = VoiceResponse()
    start = Start()
    start.stream(url=ws_url)
    response.append(start)
    # Keep the call alive while we handle audio over the WebSocket
    response.pause(length=MAX_CALL_DURATION_SECONDS)
    return str(response)


# ── Twilio call management ──────────────────────────────────────────────────

class TwilioCall:
    """Manage one outbound Twilio call."""

    def __init__(self, account_sid: str, auth_token: str, from_number: str):
        self._client = Client(account_sid, auth_token)
        self._from_number = from_number
        self._call_sid: Optional[str] = None

    def place_call(self, ws_url: str) -> str:
        """
        Initiate an outbound call to BRIDGE_TO_NUMBER using TwiML that streams
        audio to *ws_url*.  Returns the Twilio CallSid.
        """
        twiml = build_twiml(ws_url)
        call = self._client.calls.create(
            to=BRIDGE_TO_NUMBER,
            from_=self._from_number,
            twiml=twiml,
        )
        self._call_sid = call.sid
        return call.sid

    def hangup(self) -> None:
        """Hang up the active call."""
        if not self._call_sid:
            return
        try:
            self._client.calls(self._call_sid).update(status="completed")
        except Exception as exc:
            print(f"[Twilio] Error hanging up call {self._call_sid}: {exc}")
        finally:
            self._call_sid = None

    @property
    def call_sid(self) -> Optional[str]:
        return self._call_sid


def validate_credentials(account_sid: str, auth_token: str) -> bool:
    """
    Make a lightweight Twilio API call to verify credentials.
    Returns True if credentials are valid, False otherwise.
    """
    try:
        client = Client(account_sid, auth_token)
        # fetch() is a cheap operation that will 401 if credentials are wrong
        client.api.accounts(account_sid).fetch()
        return True
    except Exception:
        return False
