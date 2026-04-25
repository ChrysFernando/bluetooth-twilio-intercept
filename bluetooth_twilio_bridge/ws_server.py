"""
ws_server.py — Local WebSocket server that handles the Twilio Media Stream
connection and bridges audio to/from the Bluetooth audio capture pipeline.

Protocol reference:
  https://www.twilio.com/docs/voice/twiml/stream#websocket-messages-from-twilio
"""

from __future__ import annotations

import asyncio
import base64
import json
import queue
import threading
from typing import Callable, Optional

import websockets
from websockets.server import WebSocketServerProtocol

from .audio_proc import mulaw_to_pcm, pcm_to_mulaw


class MediaStreamServer:
    """
    Runs an asyncio WebSocket server in a dedicated background thread.

    Audio flow
    ----------
    BT input  →  put_bt_audio(pcm)  →  [queue]  →  WebSocket → Twilio
    Twilio    →  WebSocket           →  on_twilio_audio(pcm) callback

    The server exposes:
      start(host, port)   — begin listening
      stop()              — graceful shutdown
      put_bt_audio(pcm)   — enqueue PCM audio captured from BT device
    """

    def __init__(
        self,
        on_twilio_audio: Callable[[bytes], None],
        on_connected: Callable[[], None],
        on_disconnected: Callable[[], None],
    ):
        self._on_twilio_audio = on_twilio_audio
        self._on_connected = on_connected
        self._on_disconnected = on_disconnected

        self._bt_audio_queue: queue.Queue[Optional[bytes]] = queue.Queue(maxsize=50)
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._server: Optional[websockets.WebSocketServer] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._stream_sid: Optional[str] = None

    # ── Public API ─────────────────────────────────────────────────────────

    def start(self, host: str = "0.0.0.0", port: int = 0) -> int:
        """
        Start the server.  If *port* is 0, the OS assigns an available port.
        Returns the actual bound port.
        """
        self._actual_port: Optional[int] = port
        self._host = host
        ready = threading.Event()
        self._thread = threading.Thread(
            target=self._run, args=(ready,), daemon=True, name="ws-server"
        )
        self._thread.start()
        ready.wait(timeout=10)
        return self._actual_port or 0

    def stop(self) -> None:
        self._stop_event.set()
        # Poison pill to unblock the send loop
        self._bt_audio_queue.put(None)
        if self._loop and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread:
            self._thread.join(timeout=5)

    def put_bt_audio(self, pcm: bytes) -> None:
        """Enqueue a PCM chunk captured from the BT device to be sent to Twilio."""
        if not self._bt_audio_queue.full():
            self._bt_audio_queue.put_nowait(pcm)

    # ── Internal ───────────────────────────────────────────────────────────

    def _run(self, ready: threading.Event) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._serve(ready))
        self._loop.close()

    async def _serve(self, ready: threading.Event) -> None:
        async with websockets.serve(
            self._handler, self._host, self._actual_port or 0
        ) as server:
            self._server = server
            # Discover actual bound port
            sockets = server.sockets
            if sockets:
                self._actual_port = sockets[0].getsockname()[1]
            ready.set()
            await asyncio.sleep(0)
            # Keep running until stop() is called
            while not self._stop_event.is_set():
                await asyncio.sleep(0.1)

    async def _handler(self, ws: WebSocketServerProtocol, path: str = "/") -> None:
        self._on_connected()
        try:
            send_task = asyncio.create_task(self._send_loop(ws))
            recv_task = asyncio.create_task(self._recv_loop(ws))
            done, pending = await asyncio.wait(
                {send_task, recv_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
        finally:
            self._on_disconnected()

    async def _recv_loop(self, ws: WebSocketServerProtocol) -> None:
        """Receive messages from Twilio and forward audio to BT output."""
        async for message in ws:
            try:
                msg = json.loads(message)
            except json.JSONDecodeError:
                continue

            event = msg.get("event")
            if event == "start":
                self._stream_sid = msg.get("streamSid") or msg.get("start", {}).get("streamSid")
            elif event == "media":
                payload_b64 = msg.get("media", {}).get("payload", "")
                if payload_b64:
                    mulaw_bytes = base64.b64decode(payload_b64)
                    pcm = mulaw_to_pcm(mulaw_bytes)
                    self._on_twilio_audio(pcm)
            elif event == "stop":
                break

    async def _send_loop(self, ws: WebSocketServerProtocol) -> None:
        """Pull BT audio from the queue and send it to Twilio."""
        loop = asyncio.get_event_loop()
        while True:
            # Offload the blocking queue.get to a thread executor
            pcm = await loop.run_in_executor(None, self._bt_audio_queue.get)
            if pcm is None:
                break  # Poison pill — server is shutting down
            mulaw = pcm_to_mulaw(pcm)
            payload = base64.b64encode(mulaw).decode()
            msg = json.dumps(
                {
                    "event": "media",
                    "streamSid": self._stream_sid or "",
                    "media": {"payload": payload},
                }
            )
            await ws.send(msg)
