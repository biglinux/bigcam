"""Authenticated, bounded HTTPS/WebSocket and optional WebTransport camera server."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import html
import io
import json
import logging
import os
from pathlib import Path
import queue
import secrets
import socket
import ssl
import struct
import threading
import time
from urllib.parse import quote

import cv2
import numpy as np
import gi
gi.require_version("Gst", "1.0")
from gi.repository import GLib, GObject, Gst
from utils.i18n import _
from utils import xdg
from core.phone_protocol import MAX_FRAME_BYTES, jpeg_size, valid_token
from core.phone_tls import ensure_certificate

try:
    from aiohttp import web
except ImportError:
    web = None

log = logging.getLogger(__name__)
DEFAULT_PORT = 8443
ASSETS = Path(__file__).resolve().parents[1] / "web"
HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff",
           "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self' wss:; media-src 'self' blob:; frame-ancestors 'none'"}


class PhoneCameraServer(GObject.Object):
    __gsignals__ = {
        "status-changed": (GObject.SignalFlags.RUN_LAST, None, (str,)),
        "connected": (GObject.SignalFlags.RUN_LAST, None, (int, int)),
        "disconnected": (GObject.SignalFlags.RUN_LAST, None, ()),
    }

    def __init__(self):
        super().__init__()
        self._lock = threading.RLock()
        self._running = False
        self._thread = self._loop = None
        self._stop_request = threading.Event()
        self._start_event = threading.Event()
        self._start_error = ""
        self._token = secrets.token_urlsafe(32)
        self._port = DEFAULT_PORT
        self._frame_callback = None
        self._audio_callback = None
        self._width = self._height = 0
        self._last_frame_time = 0
        self._owner = None
        self._session_generation = 0
        self._pending_frame = None
        self._decoder_task = None
        self._audio_queue = queue.Queue(maxsize=8)
        self._desired_volume = 1.0
        self._desired_muted = False
        self._audio_pipeline = None
        self._audio_thread = None
        self._audio_stop = threading.Event()
        self._last_audio_sequence = -1

    @staticmethod
    def available():
        return web is not None

    @property
    def running(self):
        return self._running

    @property
    def port(self):
        return self._port

    @property
    def resolution(self):
        return self._width, self._height

    @property
    def is_connected(self):
        return self._running and self._owner is not None and time.monotonic() - self._last_frame_time < 5

    @property
    def audio_pid(self):
        return None  # No gst-launch subprocess: playback has an owned pipeline.

    def get_url(self):
        return f"https://{_get_local_ip()}:{self._port}/?token={quote(self._token)}"

    def set_frame_callback(self, callback):
        with self._lock:
            self._frame_callback = callback

    def set_audio_callback(self, callback):
        with self._lock:
            self._audio_callback = callback

    def set_audio_volume(self, value):
        self._desired_volume = max(0.0, min(float(value), 1.0))

    def set_audio_muted(self, muted):
        self._desired_muted = bool(muted)

    def _notify(self, name, *args):
        generation = self._session_generation
        def notify():
            if generation == self._session_generation:
                self.emit(name, *args)
            return GLib.SOURCE_REMOVE
        GLib.idle_add(notify)

    def start(self, port=DEFAULT_PORT):
        if not self.available():
            return False, _("python-aiohttp is not installed")
        if not isinstance(port, int) or not 1024 <= port <= 65535:
            return False, _("Invalid server port")
        with self._lock:
            if self._running:
                return True, ""
            if self._thread and self._thread.is_alive():
                return False, _("The previous server session is still stopping.")
            self._port = port
            self._token = secrets.token_urlsafe(32)
            self._stop_request = threading.Event()
            self._start_event = threading.Event()
            self._start_error = ""
            self._thread = threading.Thread(target=self._run_loop, name="bigcam-phone-server", daemon=True)
            self._thread.start()
        if not self._start_event.wait(12):
            self._stop_request.set()
            return False, _("Server did not start in time")
        if self._start_error:
            return False, self._start_error
        return self._running, ""

    def stop(self):
        self._stop_request.set()
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=8)
        # Keep the reference if it is still alive: a new server must not race it.
        if thread and thread.is_alive():
            log.warning("Phone server shutdown is still in progress")

    def _run_loop(self):
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        self._decoder = ThreadPoolExecutor(max_workers=1, thread_name_prefix="bigcam-jpeg")
        try:
            loop.run_until_complete(self._serve())
        except Exception as exc:
            self._start_error = _("Could not start the camera server: %s") % str(exc)
            log.exception("Phone server stopped with an error")
        finally:
            self._running = False
            self._start_event.set()
            tasks = asyncio.all_tasks(loop)
            for task in tasks:
                task.cancel()
            if tasks:
                loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
            loop.run_until_complete(loop.shutdown_asyncgens())
            self._decoder.shutdown(wait=True, cancel_futures=True)
            self._audio_stop.set()
            if self._audio_thread:
                self._audio_thread.join(timeout=2)
            loop.close()
            self._notify("status-changed", "stopped")

    async def _serve(self):
        cert, key, self._cert_hash = ensure_certificate(Path(xdg.cache_dir()) / "phone-tls")
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(cert, key)
        app = web.Application(client_max_size=MAX_FRAME_BYTES)
        app.router.add_get("/", self._index)
        app.router.add_get("/status", self._status)
        app.router.add_get("/phone.js", self._asset)
        app.router.add_get("/audio-worklet.js", self._asset)
        app.router.add_get("/ws", self._websocket)
        app.router.add_post("/frame", self._post)
        app.router.add_post("/disconnect", self._disconnect_http)
        runner = web.AppRunner(app, access_log=None, shutdown_timeout=2)
        quic = None
        try:
            await runner.setup()
            await web.TCPSite(runner, "0.0.0.0", self._port, ssl_context=context).start()
            try:
                from aioquic.asyncio import serve
                from aioquic.h3.connection import H3_ALPN
                from aioquic.quic.configuration import QuicConfiguration
                from core.phone_transport import PhoneTransport
                configuration = QuicConfiguration(is_client=False, alpn_protocols=H3_ALPN,
                    max_data=2 * MAX_FRAME_BYTES, max_stream_data=MAX_FRAME_BYTES,
                    max_datagram_frame_size=1200)
                configuration.load_cert_chain(cert, key)
                quic = await serve("0.0.0.0", self._port, configuration=configuration,
                    create_protocol=lambda *a, **kw: PhoneTransport(*a, phone_server=self, **kw))
            except ImportError:
                log.info("Optional QUIC support is not installed; WebSocket remains available")
            except Exception:
                log.warning("QUIC is unavailable; WebSocket remains available", exc_info=True)
            self._has_quic = quic is not None
            self._running = not self._stop_request.is_set()
            self._last_frame_time = time.monotonic()
            self._notify("status-changed", "listening")
            self._start_event.set()
            while not self._stop_request.is_set():
                await asyncio.sleep(0.1)
                if self._owner is not None and time.monotonic() - self._last_frame_time > 10:
                    owner = self._owner
                    self.release(owner)
                    if not isinstance(owner, str):
                        result = owner.close()
                        if asyncio.iscoroutine(result):
                            await result
        finally:
            owner = self._owner
            self.release(owner)
            if owner is not None and not isinstance(owner, str):
                result = owner.close()
                if asyncio.iscoroutine(result):
                    await result
            if quic:
                quic.close()
            await runner.cleanup()

    def _authorized(self, request):
        if not valid_token(request.query.get("token"), self._token):
            raise web.HTTPUnauthorized(text="Unauthorized", headers=HEADERS)

    async def _status(self, request):
        self._authorized(request)
        return web.json_response({"ready": self._running, "busy": self._owner is not None}, headers=HEADERS)

    async def _index(self, request):
        self._authorized(request)
        from core.phone_strings import phone_strings
        config = {"quic": self._has_quic, "certHash": self._cert_hash, "strings": phone_strings()}
        encoded = json.dumps(config, ensure_ascii=True).replace("<", "\u003c")
        page = (ASSETS / "phone.html").read_text(encoding="utf-8")
        page = page.replace("__CONFIG__", encoded).replace("__TOKEN__", quote(self._token))
        return web.Response(text=page, content_type="text/html", headers=HEADERS)

    async def _asset(self, request):
        self._authorized(request)
        # Fixed registered routes, never a client-supplied filesystem path.
        name = "audio-worklet.js" if request.path == "/audio-worklet.js" else "phone.js"
        return web.Response(text=(ASSETS / name).read_text(), content_type="application/javascript", headers=HEADERS)

    def claim(self, owner):
        if self._owner is not None and self._owner != owner:
            return False
        if self._owner is None:
            self._session_generation += 1
            self._last_audio_sequence = -1
            self._owner = owner
            self._last_frame_time = time.monotonic()
        return True

    def release(self, owner):
        if owner is None or self._owner != owner:
            return
        self._owner = None
        self._session_generation += 1
        self._pending_frame = None
        self._width = self._height = 0
        self._notify("disconnected")
        self._notify("status-changed", "listening" if self._running else "stopped")

    async def _websocket(self, request):
        self._authorized(request)
        if self._owner is not None:
            raise web.HTTPConflict(text="A camera is already connected", headers=HEADERS)
        ws = web.WebSocketResponse(max_msg_size=MAX_FRAME_BYTES, heartbeat=15, compress=False)
        if not self.claim(ws):
            raise web.HTTPConflict()
        try:
            await ws.prepare(request)
            async for message in ws:
                if message.type == web.WSMsgType.BINARY:
                    self.receive(bytes(message.data), ws)
                elif message.type == web.WSMsgType.ERROR:
                    break
        finally:
            self.release(ws)
        return ws

    def _http_owner(self, request):
        client = request.query.get("client", "")
        if not client.isascii() or not 16 <= len(client) <= 80 or not all(c.isalnum() or c == "-" for c in client):
            raise web.HTTPBadRequest(text="Invalid session")
        return "http:" + client

    async def _post(self, request):
        self._authorized(request)
        owner = self._http_owner(request)
        if not self.claim(owner):
            raise web.HTTPConflict(text="A camera is already connected", headers=HEADERS)
        packet = await request.read()
        if not self.receive(packet, owner):
            raise web.HTTPBadRequest(text="Invalid media packet", headers=HEADERS)
        return web.Response(status=204, headers=HEADERS)

    async def _disconnect_http(self, request):
        self._authorized(request)
        self.release(self._http_owner(request))
        return web.Response(status=204, headers=HEADERS)

    def receive(self, packet, owner):
        if self._owner != owner or not packet or len(packet) > MAX_FRAME_BYTES:
            return False
        if packet[0] == 1:
            # Each PCM packet is 20 ms @ 16 kHz mono, with a big-endian sequence.
            # Reliable streams can complete out of order; stale packets are dropped
            # rather than replayed backwards. No UDP-size assumptions are made.
            if len(packet) != 645:
                return False
            sequence = struct.unpack_from(">I", packet, 1)[0]
            if sequence <= self._last_audio_sequence:
                return False
            self._last_audio_sequence = sequence
            with self._lock:
                callback = self._audio_callback
            if callback:
                callback(packet[5:])
            try:
                self._audio_queue.put_nowait((self._session_generation, packet[5:]))
            except queue.Full:
                return False
            if self._audio_thread is None or not self._audio_thread.is_alive():
                self._audio_stop = threading.Event()
                self._audio_thread = threading.Thread(target=self._audio, name="bigcam-phone-audio", daemon=True)
                self._audio_thread.start()
            return True
        if not packet.startswith(b"\xff\xd8"):
            return False
        self._pending_frame = (self._session_generation, packet)
        if self._decoder_task is None or self._decoder_task.done():
            self._decoder_task = asyncio.create_task(self._decode_pending())
        return True

    async def _decode_pending(self):
        while self._pending_frame is not None:
            generation, packet = self._pending_frame
            self._pending_frame = None
            def decode():
                jpeg_size(packet)  # Refuse large dimensions before allocating pixels.
                image = cv2.imdecode(np.frombuffer(packet, dtype=np.uint8), cv2.IMREAD_COLOR)
                if image is None:
                    raise ValueError("Invalid JPEG")
                if generation == self._session_generation:
                    with self._lock:
                        callback = self._frame_callback
                    if callback:
                        callback(image)
                return image.shape[:2]
            try:
                h, w = await asyncio.get_running_loop().run_in_executor(self._decoder, decode)
            except Exception:
                log.debug("Rejected phone frame", exc_info=True)
                continue
            if generation != self._session_generation or self._owner is None:
                continue
            first = (w, h) != (self._width, self._height)
            self._width, self._height = w, h
            self._last_frame_time = time.monotonic()
            if first:
                self._notify("connected", w, h)
                self._notify("status-changed", "connected")

    def _audio(self):
        pipeline = None
        try:
            pipeline = Gst.parse_launch(
                "appsrc name=pcm format=time is-live=true do-timestamp=true block=false max-buffers=8 leaky-type=downstream "
                "caps=audio/x-raw,format=S16LE,rate=16000,channels=1,layout=interleaved ! "
                "audioconvert ! audioresample ! volume name=volume ! autoaudiosink sync=false")
            pipeline.set_state(Gst.State.PLAYING)
            source = pipeline.get_by_name("pcm")
            volume = pipeline.get_by_name("volume")
            while not self._audio_stop.is_set():
                try:
                    generation, pcm = self._audio_queue.get(timeout=0.1)
                except queue.Empty:
                    continue
                if generation != self._session_generation:
                    continue
                volume.set_property("volume", self._desired_volume)
                volume.set_property("mute", self._desired_muted)
                if pipeline.get_bus().pop_filtered(Gst.MessageType.ERROR):
                    break
                if source.emit("push-buffer", Gst.Buffer.new_wrapped(pcm)) != Gst.FlowReturn.OK:
                    break
        except Exception:
            log.exception("Phone audio playback failed")
        finally:
            if pipeline is not None:
                pipeline.set_state(Gst.State.NULL)


def _get_local_ip():
    # UDP connect only determines routing; it sends no packet.
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("192.0.2.1", 9))
            return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
