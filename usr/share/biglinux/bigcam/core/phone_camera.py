"""Phone camera – HTTPS + WebSocket server that receives JPEG frames from a smartphone."""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import ssl
import subprocess
import threading
import time
from typing import Any, Callable, Optional

import gi

gi.require_version("GLib", "2.0")

from gi.repository import GLib, GObject  # noqa: E402

log = logging.getLogger(__name__)

try:
    from aiohttp import web

    _HAS_AIOHTTP = True
except ImportError:
    _HAS_AIOHTTP = False

_CERT_DIR = os.path.join(GLib.get_user_cache_dir(), "bigcam")
_CERT_FILE = os.path.join(_CERT_DIR, "cert.pem")
_KEY_FILE = os.path.join(_CERT_DIR, "key.pem")

DEFAULT_PORT = 8443


# ---------------------------------------------------------------------------
# HTML page served to the smartphone browser
# ---------------------------------------------------------------------------

_PHONE_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,user-scalable=no">
<title>BigCam</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:system-ui,-apple-system,sans-serif;background:#1a1a2e;color:#e0e0e0;
  display:flex;flex-direction:column;align-items:center;min-height:100vh;padding:16px}
h1{font-size:1.4em;margin:12px 0 8px;display:flex;align-items:center;gap:8px}
h1 svg{width:28px;height:28px}
.badge{padding:6px 16px;border-radius:20px;font-size:.85em;font-weight:600;margin:8px 0 12px;
  transition:background .3s}
.disconnected{background:#dc3545}.connecting{background:#ffc107;color:#111}
.connected{background:#28a745}
video{width:100%;max-width:560px;border-radius:12px;background:#000;margin-bottom:12px}
canvas{display:none}
.controls{display:flex;flex-wrap:wrap;gap:8px;justify-content:center;margin-bottom:12px}
select,button{padding:10px 20px;border:none;border-radius:8px;font-size:.95em;
  cursor:pointer;transition:opacity .2s}
select{background:#2a2a4a;color:#e0e0e0}
button{color:#fff}
.btn-start{background:#4361ee}.btn-stop{background:#dc3545}
.btn-switch{background:#6c757d}
button:active{opacity:.7}
.info{font-size:.75em;color:#888;margin-top:auto;padding-top:16px}
.stats{font-size:.75em;color:#aaa;margin:4px 0}
</style>
</head>
<body>
<h1>
<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
<path d="M23 7l-7 5 7 5V7z"/><rect x="1" y="5" width="15" height="14" rx="2"/>
</svg>
BigCam
</h1>
<div id="status" class="badge disconnected">Disconnected</div>
<video id="video" autoplay playsinline muted></video>
<canvas id="canvas"></canvas>
<div id="stats" class="stats"></div>
<div class="controls">
<select id="resolution" aria-label="Resolution">
<option value="auto">Auto</option>
<option value="480">480p</option>
<option value="720" selected>720p</option>
<option value="1080">1080p</option>
</select>
<select id="facing" aria-label="Camera">
<option value="environment">Back</option>
<option value="user">Front</option>
</select>
<select id="quality" aria-label="Quality">
<option value="0.6">Low</option>
<option value="0.75" selected>Medium</option>
<option value="0.9">High</option>
</select>
<select id="fps" aria-label="FPS">
<option value="15">15 fps</option>
<option value="24">24 fps</option>
<option value="30" selected>30 fps</option>
</select>
<button id="btnStart" class="btn-start" onclick="start()">Start</button>
<button id="btnStop" class="btn-stop" onclick="stop()" hidden>Stop</button>
<button id="btnSwitch" class="btn-switch" onclick="switchCam()" hidden>&#x21C4;</button>
</div>
<div class="info">Tip: accept the security warning to allow camera access.</div>
<script>
let stream=null,ws=null,timer=null,frameCount=0,lastStatTime=0,sending=false;
let useHttp=false;
const video=document.getElementById('video'),
      canvas=document.getElementById('canvas'),
      ctx=canvas.getContext('2d');

function setStatus(t,c){const e=document.getElementById('status');e.textContent=t;e.className='badge '+c}

function getConstraints(){
  const r=document.getElementById('resolution').value,
        f=document.getElementById('facing').value,
        c={video:{facingMode:{ideal:f}},audio:false};
  if(r!=='auto'){const h=parseInt(r);c.video.height={ideal:h};c.video.width={ideal:Math.round(h*16/9)}}
  return c;
}

async function start(){
  setStatus('Connecting...','connecting');
  try{
    stream=await navigator.mediaDevices.getUserMedia(getConstraints());
    video.srcObject=stream;
    await video.play();

    /* Try WebSocket first, fallback to HTTP POST for Safari/iOS */
    const proto=location.protocol==='https:'?'wss:':'ws:';
    const wsUrl=proto+'//'+location.host+'/ws';
    useHttp=false;

    try{
      ws=await connectWS(wsUrl);
    }catch(e){
      console.warn('WebSocket failed, falling back to HTTP POST:',e);
      ws=null;useHttp=true;
    }

    setStatus('Connected','connected');
    startCapture();
    document.getElementById('btnStart').hidden=true;
    document.getElementById('btnStop').hidden=false;
    document.getElementById('btnSwitch').hidden=false;
  }catch(e){setStatus('Error: '+e.message,'disconnected')}
}

function connectWS(url){
  return new Promise((resolve,reject)=>{
    const s=new WebSocket(url);
    s.binaryType='arraybuffer';
    const t=setTimeout(()=>{s.close();reject(new Error('timeout'))},5000);
    s.onopen=()=>{clearTimeout(t);resolve(s)};
    s.onerror=(e)=>{clearTimeout(t);reject(e)};
  });
}

function startCapture(){
  const fps=parseInt(document.getElementById('fps').value)||30;
  const interval=Math.round(1000/fps);
  frameCount=0;lastStatTime=performance.now();sending=false;
  timer=setInterval(captureFrame,interval);
}

function captureFrame(){
  if(video.videoWidth===0)return;
  if(sending)return;
  sending=true;
  canvas.width=video.videoWidth;
  canvas.height=video.videoHeight;
  ctx.drawImage(video,0,0);
  const q=parseFloat(document.getElementById('quality').value)||0.75;
  canvas.toBlob(blob=>{
    if(!blob){sending=false;return}
    if(useHttp){
      fetch('/frame',{method:'POST',body:blob}).then(()=>{sending=false}).catch(()=>{sending=false});
    }else if(ws&&ws.readyState===1){
      blob.arrayBuffer().then(buf=>{ws.send(buf);sending=false});
    }else{sending=false}
    frameCount++;
    const now=performance.now();
    if(now-lastStatTime>=1000){
      const fps=Math.round(frameCount*1000/(now-lastStatTime));
      document.getElementById('stats').textContent=
        canvas.width+'x'+canvas.height+' @ '+fps+' fps | '+Math.round(blob.size/1024)+' KB/frame';
      frameCount=0;lastStatTime=now;
    }
  },'image/jpeg',q);
}

function stopCapture(){
  if(timer){clearInterval(timer);timer=null}
  if(ws){ws.close();ws=null}
  if(stream){stream.getTracks().forEach(t=>t.stop());stream=null}
  video.srcObject=null;
  document.getElementById('btnStart').hidden=false;
  document.getElementById('btnStop').hidden=true;
  document.getElementById('btnSwitch').hidden=true;
  document.getElementById('stats').textContent='';
}

function stop(){stopCapture();setStatus('Disconnected','disconnected')}

async function switchCam(){
  const sel=document.getElementById('facing');
  sel.value=sel.value==='user'?'environment':'user';
  stop();await start();
}

/* Re-acquire camera on orientation change so dimensions update */
if(screen.orientation){
  screen.orientation.addEventListener('change',()=>{
    if(stream){
      /* Only restart media capture, keep WebSocket alive */
      if(timer){clearInterval(timer);timer=null}
      stream.getTracks().forEach(t=>t.stop());
      navigator.mediaDevices.getUserMedia(getConstraints()).then(s=>{
        stream=s;video.srcObject=s;
        video.play().then(()=>startCapture());
      }).catch(e=>console.warn('Re-acquire failed:',e));
    }
  });
}
</script>
</body>
</html>
"""


class PhoneCameraServer(GObject.Object):
    """HTTPS + WebSocket server that receives JPEG frames from a smartphone.

    The phone browser captures camera frames on a canvas, encodes them as
    JPEG, and sends the binary data over a WebSocket connection.  The server
    decodes each frame with OpenCV and makes it available via a callback.
    """

    __gsignals__ = {
        "status-changed": (GObject.SignalFlags.RUN_LAST, None, (str,)),
        "connected": (GObject.SignalFlags.RUN_LAST, None, (int, int)),
        "disconnected": (GObject.SignalFlags.RUN_LAST, None, ()),
    }

    def __init__(self) -> None:
        super().__init__()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._runner: Optional[Any] = None
        self._running = False
        self._port = DEFAULT_PORT
        self._width = 0
        self._height = 0
        self._ws_clients: set[Any] = set()

        # fn(numpy_bgr_frame) — called from the asyncio thread
        self._frame_callback: Optional[Callable] = None
        self._last_frame_time: float = 0.0

    # -- public API ----------------------------------------------------------

    @staticmethod
    def available() -> bool:
        """Return True if aiohttp is installed."""
        return _HAS_AIOHTTP

    @property
    def running(self) -> bool:
        return self._running

    @property
    def port(self) -> int:
        return self._port

    @property
    def resolution(self) -> tuple[int, int]:
        return self._width, self._height

    @property
    def is_connected(self) -> bool:
        """Return True if a phone is actively sending frames."""
        if self._ws_clients:
            return True
        # HTTP POST fallback: check if frames arrived recently
        return (time.monotonic() - self._last_frame_time) < 3.0

    def get_url(self) -> str:
        return f"https://{_get_local_ip()}:{self._port}/"

    def set_frame_callback(self, callback: Optional[Callable]) -> None:
        self._frame_callback = callback

    def start(self, port: int = DEFAULT_PORT) -> bool:
        if not _HAS_AIOHTTP:
            log.error("python-aiohttp not installed")
            return False
        if self._running:
            return True

        self._port = port
        _ensure_cert()

        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run_loop, name="phone-cam", daemon=True
        )
        self._thread.start()
        self._running = True
        GLib.idle_add(self.emit, "status-changed", "listening")
        return True

    def stop(self) -> None:
        if not self._running:
            return
        self._running = False

        if self._loop and self._loop.is_running():

            async def _shutdown() -> None:
                for ws in list(self._ws_clients):
                    await ws.close()
                self._ws_clients.clear()
                if self._runner:
                    await self._runner.cleanup()

            fut = asyncio.run_coroutine_threadsafe(_shutdown(), self._loop)
            try:
                fut.result(timeout=5)
            except Exception:
                log.debug("Ignored exception", exc_info=True)
            self._loop.call_soon_threadsafe(self._loop.stop)

        if self._thread:
            self._thread.join(timeout=5)
        self._thread = None
        self._loop = None
        self._width = self._height = 0
        GLib.idle_add(self.emit, "status-changed", "stopped")

    # -- asyncio server ------------------------------------------------------

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._start_server())
        self._loop.run_forever()

    async def _start_server(self) -> None:
        app = web.Application(client_max_size=10 * 1024 * 1024)
        app.router.add_get("/", self._handle_index)
        app.router.add_get("/ws", self._handle_ws)
        app.router.add_post("/frame", self._handle_frame_post)

        ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ssl_ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ssl_ctx.load_cert_chain(_CERT_FILE, _KEY_FILE)

        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "0.0.0.0", self._port, ssl_context=ssl_ctx)
        await site.start()
        log.info("Phone camera server listening on port %d", self._port)

    async def _handle_index(self, _request: web.Request) -> web.Response:
        return web.Response(text=_PHONE_HTML, content_type="text/html")

    async def _handle_frame_post(self, request: web.Request) -> web.Response:
        """HTTP POST fallback for browsers that reject WSS with self-signed certs (Safari/iOS)."""
        try:
            import cv2
            import numpy as np
        except ImportError:
            return web.Response(status=500, text="opencv not available")

        data = await request.read()
        if not data:
            return web.Response(status=400)

        jpg_array = np.frombuffer(data, dtype=np.uint8)
        bgr = cv2.imdecode(jpg_array, cv2.IMREAD_COLOR)
        if bgr is None:
            return web.Response(status=400)

        h, w = bgr.shape[:2]
        if w != self._width or h != self._height:
            self._width, self._height = w, h
            GLib.idle_add(self.emit, "connected", w, h)
            GLib.idle_add(self.emit, "status-changed", "connected")

        cb = self._frame_callback
        if cb:
            cb(bgr)

        self._last_frame_time = time.monotonic()

        return web.Response(status=204)

    async def _handle_ws(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(max_msg_size=10 * 1024 * 1024)
        await ws.prepare(request)
        self._ws_clients.add(ws)

        log.info("Phone camera WebSocket connected from %s", request.remote)

        try:
            import cv2
            import numpy as np
        except ImportError:
            log.error("OpenCV (cv2) required for phone camera")
            await ws.close()
            return ws

        first_frame = True

        try:
            async for msg in ws:
                if msg.type == web.WSMsgType.BINARY:
                    # Decode JPEG
                    jpg_array = np.frombuffer(msg.data, dtype=np.uint8)
                    bgr = cv2.imdecode(jpg_array, cv2.IMREAD_COLOR)
                    if bgr is None:
                        continue

                    h, w = bgr.shape[:2]

                    if first_frame or (w != self._width or h != self._height):
                        self._width, self._height = w, h
                        GLib.idle_add(self.emit, "connected", w, h)
                        GLib.idle_add(self.emit, "status-changed", "connected")
                        first_frame = False

                    cb = self._frame_callback
                    if cb:
                        cb(bgr)
                    self._last_frame_time = time.monotonic()

                elif msg.type in (
                    web.WSMsgType.ERROR,
                    web.WSMsgType.CLOSE,
                ):
                    break
        finally:
            self._ws_clients.discard(ws)
            self._width = self._height = 0
            GLib.idle_add(self.emit, "disconnected")
            GLib.idle_add(self.emit, "status-changed", "disconnected")
            log.info("Phone camera WebSocket disconnected")

        return ws


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_local_ip() -> str:
    """Best-effort local LAN IP address (no external network contact)."""
    try:
        hostname = socket.gethostname()
        ip = socket.gethostbyname(hostname)
        if ip and not ip.startswith("127."):
            return ip
    except Exception:
        log.debug("Ignored exception", exc_info=True)
    # Fallback: UDP connect to a non-routable address (no packets sent)
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def _ensure_cert() -> None:
    """Generate a self-signed TLS certificate if missing."""
    if os.path.isfile(_CERT_FILE) and os.path.isfile(_KEY_FILE):
        return
    os.makedirs(_CERT_DIR, exist_ok=True)
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-keyout",
            _KEY_FILE,
            "-out",
            _CERT_FILE,
            "-days",
            "365",
            "-nodes",
            "-subj",
            "/CN=BigCam Phone Camera",
        ],
        check=True,
        capture_output=True,
        timeout=10,
    )
    os.chmod(_KEY_FILE, 0o600)
    log.info("Generated self-signed certificate at %s", _CERT_FILE)
