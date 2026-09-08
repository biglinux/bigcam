#!/usr/bin/env python3
"""Real GTK/GStreamer integration with synthetic HTTP video and isolated HOME.

Calls the actual action handlers (not pixel coordinates), verifies saved media,
and requires a separate AT-SPI client. This is not a physical camera test.
"""
import io
import json
import os
import subprocess
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/"usr/share/biglinux/bigcam"))
RESULTS=Path(os.environ["BIGCAM_TEST_RESULTS"]);RESULTS.mkdir(parents=True,exist_ok=True)
import gi
import numpy as np
from PIL import Image, ImageDraw

gi.require_version("Gtk","4.0");gi.require_version("Adw","1");gi.require_version("Gst","1.0")
from gi.repository import Adw, GLib, Gst, Gtk

Gst.init(None)
from constants import APP_ID, APP_NAME

GLib.set_prgname(APP_ID)
GLib.set_application_name(APP_NAME)
from utils import xdg
from utils.settings_manager import SettingsManager

SettingsManager().update({"show-welcome":False,"virtual-camera-enabled":False,"hotplug_enabled":False,
                           "resource-monitor-enabled":False,"auto-hide-controls":False,"theme":"dark"})

stop=threading.Event()
class Camera(BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.requests = getattr(self.server, "requests", 0) + 1
        self.send_response(200);self.send_header("Content-Type","multipart/x-mixed-replace; boundary=frame");self.end_headers()
        number=0
        try:
            while not stop.is_set():
                im=Image.new("RGB",(642,360),(30,130,220));draw=ImageDraw.Draw(im)
                draw.rectangle((20+number%300,80,80+number%300,180),fill="red")
                draw.text((20,20),"BigCam synthetic camera / integration test",fill="white")
                data=io.BytesIO();im.save(data,format="JPEG");data=data.getvalue()
                self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "+str(len(data)).encode()+b"\r\n\r\n"+data+b"\r\n")
                self.wfile.flush();number+=1;stop.wait(1/25)
        except (ConnectionError,OSError): pass
    def log_message(self,*args): pass

server=ThreadingHTTPServer(("127.0.0.1",0),Camera)
threading.Thread(target=server.serve_forever,daemon=True).start()
context=GLib.MainContext.default()
def pump_until(predicate,seconds=15,label="condition"):
    deadline=time.monotonic()+seconds
    while time.monotonic()<deadline:
        for _ in range(100):
            if not context.pending():break
            context.iteration(False)
        if predicate(): return
        time.sleep(.01)
    raise AssertionError("Timed out waiting for "+label)
def pump(seconds):
    deadline=time.monotonic()+seconds
    pump_until(lambda:time.monotonic()>=deadline,seconds+1,"main loop interval")
def capture(name):
    subprocess.run(["scrot",str(RESULTS/name)],check=True,timeout=5)

summary={"full_functional_certification":False,"physical_hardware_tested":False,"checks":{}}
app=win=None
try:
    from main import BigDigicamApp
    app=BigDigicamApp();assert app.register(None);app.activate()
    pump_until(lambda: bool(app.get_windows()),label="application window")
    win=app.get_windows()[0];pump_until(lambda:win.get_mapped(),label="mapped window")
    summary["checks"]["window_opened"]=True
    assert not app.get_accels_for_action("win.toggle-sidebar")==["Tab"]
    win._on_ip_camera_added(None,"Synthetic HTTP",f"http://127.0.0.1:{server.server_port}/camera")
    pump_until(lambda:win._stream_engine.last_frame_bgr is not None,20,"actual decoded frames")
    pump(1)
    assert win._stream_engine.is_playing()
    assert win._preview._picture.get_paintable() is not None
    summary["checks"]["preview_decoded_and_rendered"]=True
    capture("01-preview.png")
    win.activate_action("win.toggle-sidebar",None);pump(.2)
    capture("02-controls.png")
    win.activate_action("win.toggle-sidebar",None)
    a11y=subprocess.Popen([sys.executable,str(ROOT/"tests/integration/a11y_client.py")],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    pump_until(lambda:a11y.poll() is not None,20,"AT-SPI peer process")
    out,err=a11y.communicate();(RESULTS/"a11y.json").write_text(out);(RESULTS/"a11y.log").write_text(err)
    assert a11y.returncode==0,"Actual application must appear in AT-SPI"
    summary["checks"]["atspi_tree_exported"]=True
    win._on_capture_action()
    pump_until(lambda:not win._capture_pending,10,"photo completion")
    photos=list(Path(xdg.photos_dir()).glob("*.png"));assert photos,"Photo action did not save a file"
    with Image.open(photos[-1]) as image:
        assert image.size==(642,360)
        assert np.asarray(image).std()>20
    summary["checks"]["photo_saved_and_decoded"]=True
    from core.backends.ip_backend import IPBackend
    with ThreadPoolExecutor() as executor:
        snapshot = RESULTS / "backend-photo.jpg"
        future = executor.submit(IPBackend().capture_photo, win._active_camera, str(snapshot))
        pump_until(future.done, 20, "network snapshot EOS")
        assert future.result()
        with Image.open(snapshot) as image:
            assert image.size == (642, 360)
    summary["checks"]["network_snapshot_eos"] = True
    win._on_record_toggle()
    pump_until(lambda:win._video_recorder.state=="recording",20,"verified recording encoder")
    pump(2.5);win._on_record_toggle()
    pump_until(lambda:win._video_recorder._done.is_set(),20,"EOS finalization")
    pump(.2)
    assert win._video_recorder.state=="idle",win._video_recorder.state
    path=win._video_recorder.output_path
    probe=subprocess.check_output(["ffprobe","-v","error","-show_streams","-show_format","-of","json",path],timeout=10,text=True)
    report=json.loads(probe);assert any(s["codec_type"]=="video" for s in report["streams"])
    assert float(report["format"]["duration"])>1
    subprocess.run(["ffmpeg","-v","error","-i",path,"-f","null","-"],check=True,timeout=20)
    (RESULTS/"recording-probe.json").write_text(probe)
    summary["checks"]["recording_finalized_and_decoded"]=True
    win._sidebar_ctrl._sidebar_tab_btns[3].set_active(True)
    assert win._sidebar_ctrl.stack.get_visible_child_name() == "videos"
    win._split_view.set_show_sidebar(True)
    gallery = win._video_gallery
    gallery.refresh()
    pump_until(lambda: bool(gallery._entries) and gallery._scope["active"] == 0,
               20, "video gallery metadata")
    def descendants(widget):
        yield widget
        child = widget.get_first_child()
        while child:
            yield from descendants(child)
            child = child.get_next_sibling()
    assert any(isinstance(widget, Gtk.Label) and widget.get_label().startswith("0:")
               for widget in descendants(gallery))
    assert any(isinstance(widget, Gtk.Image) and widget.get_icon_name() == "media-playback-start-symbolic"
               for widget in descendants(gallery))
    def sidebar_fully_visible():
        valid, bounds = win._split_view.get_sidebar().compute_bounds(win)
        return valid and bounds.get_x() >= 0 and bounds.get_x() + bounds.get_width() <= win.get_width()
    pump_until(sidebar_fully_visible, 5, "completed sidebar animation")
    pump(.1)
    capture("03-video-gallery.png")
    gallery._set_view("list")
    pump_until(lambda: gallery._scope["active"] == 0, 20, "list duration")
    row = gallery._list.get_first_child()
    row.grab_focus()
    focused = win.get_focus()
    subprocess.run(["xdotool", "key", "Tab"], check=True, timeout=5)
    pump(.2)
    assert win.get_focus() is not None and win.get_focus() != focused
    summary["checks"]["gallery_duration_play_icon_and_keyboard"] = True
    # An actual invalid audio source must fail, not remain visually recording.
    recorder=win._video_recorder
    recorder.start(None,audio_sources=["bigcam-does-not-exist"],active_audio_sources=["bigcam-does-not-exist"])
    recorder.write_frame(np.full((48,66,3),100,dtype=np.uint8))
    pump_until(lambda:recorder._done.is_set(),20,"error propagation")
    assert recorder.state=="error"
    summary["checks"]["recording_error_clears_state"]=True
    capture("03-recording-result.png")
    win._cleanup_and_close()
    pump_until(lambda:not app.get_windows(),40,"graceful close")
    summary["checks"]["graceful_close"]=True
    summary["versions"]={"python":sys.version,"gtk":[Gtk.get_major_version(),Gtk.get_minor_version()],
                         "adwaita":[Adw.get_major_version(),Adw.get_minor_version()],"gst":Gst.version_string()}
    summary["success"]=True
except Exception:
    summary["http_requests"] = getattr(server, "requests", 0)
    if win and win._stream_engine._pipeline:
        summary["pipeline_state"] = str(win._stream_engine._pipeline.get_state(0))
    summary["success"]=False;summary["exception"]=traceback.format_exc();traceback.print_exc()
    try:capture("failure.png")
    except Exception:pass
finally:
    if win:
        try:win._video_recorder.stop();win._stream_engine.stop();win._camera_manager.close()
        except Exception:traceback.print_exc()
    stop.set();server.shutdown();server.server_close()
    (RESULTS/"summary.json").write_text(json.dumps(summary,indent=2))
    print(json.dumps(summary,indent=2),flush=True)
# Executor threads must not conceal a failed test indefinitely.
os._exit(0 if summary.get("success") else 1)
