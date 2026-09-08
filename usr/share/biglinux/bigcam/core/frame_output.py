"""Single-owner, bounded raw-frame output for a background browser camera."""
import logging
import queue
import threading
import cv2
import gi
gi.require_version("Gst", "1.0")
from gi.repository import Gst
from utils.urls import gst_quote
from utils.gst_buffers import bgr_buffer

log = logging.getLogger(__name__)


class FrameOutput:
    def __init__(self, device):
        self.device = device
        self._frames = queue.Queue(maxsize=2)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="bigcam-phone-output", daemon=True)
        self._thread.start()

    def push(self, frame):
        if self._stop.is_set():
            return
        try:
            self._frames.put_nowait(frame.copy())
        except queue.Full:
            try:
                self._frames.get_nowait()
            except queue.Empty:
                pass
            try:
                self._frames.put_nowait(frame.copy())
            except queue.Full:
                pass

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=2)

    def _run(self):
        pipeline = None
        try:
            while not self._stop.is_set():
                try:
                    frame = self._frames.get(timeout=0.1)
                except queue.Empty:
                    continue
                if pipeline is None:
                    h, w = frame.shape[:2]
                    w = max(2, w // 2 * 2)
                    pipeline = Gst.parse_launch(
                        f"appsrc name=source format=time is-live=true do-timestamp=true block=false "
                        f"max-buffers=2 leaky-type=downstream caps=video/x-raw,format=BGR,width={w},height={h},framerate=30/1 ! "
                        f"queue max-size-buffers=2 leaky=downstream ! videoconvert ! video/x-raw,format=YUY2 ! "
                        f"v4l2sink device={gst_quote(self.device)} sync=false")
                    if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
                        raise RuntimeError("Background virtual camera did not start")
                    source = pipeline.get_by_name("source")
                error = pipeline.get_bus().pop_filtered(Gst.MessageType.ERROR)
                if error:
                    raise RuntimeError(error.parse_error()[0].message)
                if frame.shape[:2] != (h, w):
                    frame = cv2.resize(frame, (w, h))
                if source.emit("push-buffer", bgr_buffer(frame)) != Gst.FlowReturn.OK:
                    break
        except Exception:
            log.exception("Background browser output failed")
        finally:
            self._stop.set()
            if pipeline is not None:
                pipeline.set_state(Gst.State.NULL)
