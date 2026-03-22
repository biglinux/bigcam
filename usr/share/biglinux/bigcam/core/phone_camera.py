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

from gi.repository import GLib, GObject

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
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no,viewport-fit=cover">
<title>BigCam</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{--bg:#121215;--surface:#1e1e24;--surface2:#2a2a32;--accent:#c9a00c;
  --accent2:#e6b800;--text:#f0f0f0;--dim:#888;--ok:#2ecc71;--warn:#f39c12;--err:#e74c3c;
  --radius:14px;
  --safe-t:env(safe-area-inset-top,0px);--safe-b:env(safe-area-inset-bottom,0px);
  --safe-l:env(safe-area-inset-left,0px);--safe-r:env(safe-area-inset-right,0px)}
html{height:100%}
body{font-family:'Segoe UI',system-ui,-apple-system,sans-serif;background:var(--bg);color:var(--text);
  display:flex;flex-direction:column;align-items:center;
  height:100dvh;height:100vh;
  padding:calc(10px + var(--safe-t)) calc(10px + var(--safe-r)) calc(10px + var(--safe-b)) calc(10px + var(--safe-l));
  gap:8px;overflow:hidden}

/* Header */
.header{display:flex;align-items:center;gap:8px;flex-shrink:0}
.logo{width:40px;height:40px;flex-shrink:0}
.brand{display:flex;flex-direction:column}
.brand h1{font-size:1.2em;font-weight:700;letter-spacing:.5px}
.brand span{font-size:.65em;color:var(--dim);font-weight:400}

/* Status badge */
.badge{padding:4px 16px;border-radius:20px;font-size:.75em;font-weight:600;text-align:center;
  transition:all .3s ease;flex-shrink:0}
.disconnected{background:var(--err);color:#fff}
.connecting{background:var(--warn);color:#111}
.connected{background:var(--ok);color:#fff}

/* Video */
.video-wrap{flex:1 1 0;display:flex;align-items:center;justify-content:center;width:100%;
  min-height:0;overflow:hidden;border-radius:var(--radius)}
video{width:100%;height:100%;object-fit:contain;background:#000;border-radius:var(--radius)}
canvas{display:none}

/* Stats */
.stats{font-size:.65em;color:var(--dim);text-align:center;flex-shrink:0;min-height:1em}

/* Controls */
.controls{display:grid;grid-template-columns:repeat(4,1fr);gap:6px;width:100%;max-width:400px;
  flex-shrink:0;align-self:center}
select,button{padding:10px 6px;border:none;border-radius:10px;font-size:.82em;
  cursor:pointer;transition:all .15s ease;text-align:center;-webkit-appearance:none;
  min-height:44px;touch-action:manipulation}
select{background:var(--surface2);color:var(--text);outline:none}
select:focus{box-shadow:0 0 0 2px var(--accent)}
button{color:#fff;font-weight:600}
.btn-start{background:var(--accent);color:#111;grid-column:span 2}
.btn-stop{background:var(--err);grid-column:span 2}
.btn-switch{background:var(--surface2);grid-column:span 2}
button:active{transform:scale(.95);opacity:.85}
.tip{font-size:.6em;color:var(--dim);text-align:center;flex-shrink:0}

/* ── Landscape: side-by-side layout ── */
@media (orientation:landscape) and (max-height:500px){
  body{
    display:grid;
    grid-template-columns:1fr clamp(180px,40%,320px);
    grid-template-rows:auto auto 1fr auto;
    padding:calc(4px + var(--safe-t)) calc(6px + var(--safe-r)) calc(4px + var(--safe-b)) calc(6px + var(--safe-l));
    gap:4px 10px;align-items:stretch}

  /* Video fills entire left column */
  .video-wrap{grid-column:1;grid-row:1/-1;height:100%;border-radius:10px}

  /* Right column panel */
  .header{grid-column:2;grid-row:1;gap:8px;justify-content:center}
  .logo{width:48px;height:48px}
  .brand h1{font-size:1.2em}
  .brand span{font-size:.65em}

  #status{grid-column:2;grid-row:2;padding:3px 12px;font-size:.7em;justify-self:center}

  .controls{
    grid-column:2;grid-row:3;
    grid-template-columns:1fr 1fr;
    max-width:none;gap:6px;
    align-content:start;align-self:start}
  select,button{padding:12px 6px;font-size:.85em;border-radius:8px;min-height:40px}
  .btn-start,.btn-stop,.btn-switch{grid-column:span 2}

  .stats{grid-column:2;grid-row:4;font-size:.6em;min-height:auto}
  .tip{display:none}
}

/* ── Tall portrait phones ── */
@media (orientation:portrait) and (min-height:700px){
  .video-wrap{max-height:55vh}
  .controls{gap:8px}
  select,button{padding:12px 8px;font-size:.9em}}

/* ── Very short landscape (foldables, small screens) ── */
@media (orientation:landscape) and (max-height:360px){
  body{grid-template-rows:auto auto 1fr auto;gap:2px 6px}
  .header{gap:2px}
  .logo{width:16px;height:16px}
  .brand h1{font-size:.6em}
  .brand span{display:none}
  #status{padding:1px 6px;font-size:.5em}
  select,button{padding:4px 2px;font-size:.6em;min-height:24px}
}
</style>
</head>
<body>

<div class="header">
  <svg class="logo" viewBox="0 0 52.351 52.351" xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink">
    <defs><linearGradient id="bg" x1=".05" x2="1" y2="1" gradientTransform="translate(-2.38 -2.38)scale(52.35)" gradientUnits="userSpaceOnUse">
      <stop offset=".2" stop-color="#595500" stop-opacity=".9"/><stop offset=".205" stop-color="#ffeb35"/>
      <stop offset="1" stop-color="#bf8c05"/></linearGradient></defs>
    <rect width="52.351" height="52.351" fill="url(#bg)" rx="15.705" opacity=".9"/>
    <path d="M31.768 7.438q-.296 0-.623.054c-3.527.59-3.38 4.696-3.38 4.696s0 1.817 1.696 2.736c1.411.814 2.223-.063 2.361-.201s3.009-3.73 2.547 3.088c0 0 1.01-1.1 1.074-3.717.062-2.618-1.623-2.78-1.623-2.78s-.743-.188-2.22 1.409c-1.245 1.344-1.518 1.506-1.573 1.523h-.004s-.418.295-.468-.445-.21-2.978 2.047-4.192c2.25-1.213 3.35 1.756 3.35 1.756s-.248-3.923-3.184-3.927M25.404 9.14a1.197 1.597 77.92 0 0-.414.046 1.197 1.597 77.92 0 0-1.308 1.504 1.197 1.597 77.92 0 0 1.808.838 1.197 1.597 77.92 0 0 1.313-1.506 1.197 1.597 77.92 0 0-1.399-.882m-7.431.505c-.015.017-1.011 1.115-1.073 3.713-.061 2.618 1.618 2.774 1.618 2.774s.743.184 2.22-1.41c1.2-1.297 1.5-1.495 1.567-1.524h.013s.416-.29.463.447c.048.738.211 2.987-2.045 4.194-2.253 1.206-3.35-1.756-3.35-1.756s.287 4.459 3.811 3.871c3.527-.588 3.38-4.695 3.38-4.695s.003-1.81-1.698-2.73c-1.411-.815-2.225.062-2.365.202s-3 3.729-2.541-3.086m7.941 2.176a1 1 0 0 0-.16.002 1.223 1.223 0 0 0-1.125 1.317 1.2 1.2 0 0 0 .055.283s.708 2.825 4.535 2.916c0 0-2.206-1.577-2.157-3.393a1.22 1.22 0 0 0-1.148-1.125m-2.418 8.041c-.553 0-.553.003-1.078.56L20.92 22.63h-3.508c-1.217 0-2.213.909-2.213 2.092v12.353c0 1.183.996 2.15 2.213 2.15h8.06v1.133l-8.02 8.02a.83.83 0 0 0 0 1.178.833.833 0 0 0 1.179.002l6.842-6.844v7.924a.83.83 0 0 0 .834.832.83.83 0 0 0 .832-.832V42.8l6.756 6.756a.83.83 0 0 0 1.177-.002.83.83 0 0 0 0-1.178l-7.933-7.932v-1.22h7.974c1.217 0 2.211-.968 2.211-2.15V24.72c0-1.183-.994-2.092-2.21-2.092h-3.51l-1.518-2.227c-.505-.536-.506-.539-1.059-.539zm11.738 4.149h.016a.69.69 0 0 1 .691.691.69.69 0 0 1-.691.692.69.69 0 0 1-.691-.692.69.69 0 0 1 .675-.691m-9.056 1.383h.084a5.53 5.53 0 0 1 5.531 5.533 5.53 5.53 0 0 1-5.531 5.531 5.53 5.53 0 0 1-5.532-5.531 5.53 5.53 0 0 1 5.448-5.533m.017 2.765a2.766 2.766 0 0 0-2.699 2.768 2.766 2.766 0 0 0 2.766 2.765 2.766 2.766 0 0 0 2.765-2.765 2.766 2.766 0 0 0-2.765-2.768z" fill="#444"/>
  </svg>
  <div class="brand">
    <h1>BigCam</h1>
    <span>Phone as Webcam</span>
  </div>
</div>

<div id="status" class="badge disconnected">Disconnected</div>

<div class="video-wrap">
  <video id="video" autoplay playsinline muted></video>
</div>
<canvas id="canvas"></canvas>
<div id="stats" class="stats"></div>

<div class="controls">
  <select id="resolution" aria-label="Resolution">
    <option value="auto">Auto</option>
    <option value="480">480p</option>
    <option value="720" selected>720p</option>
    <option value="1080">1080p</option>
  </select>
  <select id="facing" aria-label="Camera" onchange="if(stream){stop();start()}">
    <option value="environment">&#x1F4F7; Back</option>
    <option value="user">&#x1F933; Front</option>
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
  <button id="btnStart" class="btn-start" onclick="start()">&#x25B6; Start</button>
  <button id="btnStop" class="btn-stop" onclick="stop()" hidden>&#x25A0; Stop</button>
  <button id="btnSwitch" class="btn-switch" onclick="switchCam()" hidden>&#x21C4; Switch</button>
</div>

<div class="tip">Accept the security warning to allow camera access.</div>

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
        canvas.width+'\\u00d7'+canvas.height+' @ '+fps+' fps | '+Math.round(blob.size/1024)+' KB';
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

if(screen.orientation){
  screen.orientation.addEventListener('change',()=>{
    if(stream){
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

        had_clients = bool(self._ws_clients) or self._width > 0

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
                log.debug("Phone camera shutdown timed out", exc_info=True)
            self._loop.call_soon_threadsafe(self._loop.stop)

        if self._thread:
            self._thread.join(timeout=5)
        self._thread = None
        self._loop = None
        self._width = self._height = 0
        # Emit "disconnected" so the window cleans up the phone camera entry
        # even if the WebSocket handler's finally block didn't get a chance.
        if had_clients:
            GLib.idle_add(self.emit, "disconnected")
        GLib.idle_add(self.emit, "status-changed", "stopped")

    # -- asyncio server ------------------------------------------------------

    def _run_loop(self) -> None:
        _ensure_cert()
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._start_server())
        self._loop.run_forever()

    async def _start_server(self) -> None:
        app = web.Application(client_max_size=10 * 1024 * 1024)
        app.router.add_get("/", self._handle_index)
        app.router.add_get("/ws", self._handle_ws)
        app.router.add_post("/frame", self._handle_frame_post)

        ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
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
    """Best-effort local LAN IP address."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.5)
        s.connect(("8.8.8.8", 80))
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
    )
    os.chmod(_KEY_FILE, 0o600)
    log.info("Generated self-signed certificate at %s", _CERT_FILE)
