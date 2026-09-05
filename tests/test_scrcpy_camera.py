"""ScrcpyCamera: adb parsing plus the public surface the UI depends on."""

from __future__ import annotations

import types


from core.scrcpy_camera import DeviceInfo, ScrcpyCamera

ADB_DEVICES = """\
List of devices attached
R58M12ABCDE            device product:a52qnaxx model:SM_A525M device:a52q transport_id:3
192.168.1.44:5555      device product:redfin model:Pixel_5 device:redfin transport_id:5
0123456789ABCDEF       unauthorized usb:1-2
badserial              offline
"""


def _fake_runner(monkeypatch, stdout: str, rc: int = 0):
    from utils.command_runner import SecureCommandRunner

    def _run(args, *a, **kw):
        return types.SimpleNamespace(returncode=rc, stdout=stdout, stderr="")

    monkeypatch.setattr(SecureCommandRunner, "run_safe", staticmethod(_run))


def test_list_devices_parses_model_and_transport(monkeypatch):
    _fake_runner(monkeypatch, ADB_DEVICES)
    devs = ScrcpyCamera.list_devices()
    assert [d.serial for d in devs] == ["R58M12ABCDE", "192.168.1.44:5555"]
    assert devs[0].model == "SM A525M"
    assert devs[0].transport == "usb"
    assert devs[1].transport == "tcpip"


def test_list_devices_can_include_unauthorized(monkeypatch):
    _fake_runner(monkeypatch, ADB_DEVICES)
    devs = ScrcpyCamera.list_devices(include_unauthorized=True)
    states = {d.serial: d.state for d in devs}
    assert states["0123456789ABCDEF"] == "unauthorized"
    assert "badserial" not in states, "offline devices must stay hidden"


def test_list_cameras_parses_scrcpy_output(monkeypatch):
    _fake_runner(
        monkeypatch,
        "--camera-id=0    (facing=back, size=4000x3000)\n"
        "--camera-id=1    (facing=front, size=3264x2448)\n",
    )
    cams = ScrcpyCamera.list_cameras("SERIAL")
    assert cams == [
        {"id": "0", "facing": "back", "size": "4000x3000"},
        {"id": "1", "facing": "front", "size": "3264x2448"},
    ]


# -- the bug the mobile controller trips over ------------------------------


def test_camera_exposes_model_for_the_ui():
    """MobileDeviceController reads ``camera.model`` on the 'connected' signal.

    Without it, the Android camera is never registered.
    """
    cam = ScrcpyCamera()
    assert hasattr(cam, "model"), "ScrcpyCamera must expose .model for the UI"
    assert cam.model == ""


def test_model_is_populated_by_start(monkeypatch):
    cam = ScrcpyCamera()
    monkeypatch.setattr(
        "core.scrcpy_camera.SecureCommandRunner.popen_safe",
        staticmethod(lambda *a, **kw: types.SimpleNamespace(
            pid=1234, stdout=None, poll=lambda: None
        )),
    )
    ok = cam.start("SERIAL123", "/dev/video20", model="Pixel 5")
    assert ok
    assert cam.model == "Pixel 5"
    assert cam.device_serial == "SERIAL123"
    cam._running = False


def test_device_info_falls_back_to_serial():
    assert DeviceInfo("ABC").model == "ABC"
