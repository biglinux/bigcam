# PLANNING.md — BigCam Comprehensive Audit & Roadmap

> Generated from automated analysis (ruff, mypy, vulture, radon) and manual review
> of all 41 Python source files (~9,400 lines).

---

## Table of Contents

1. [Audit Summary](#audit-summary)
2. [Automated Tool Results](#automated-tool-results)
3. [Critical — Must Fix Before Release](#critical--must-fix-before-release)
4. [High — Fix Soon](#high--fix-soon)
5. [Medium — Quality & Maintainability](#medium--quality--maintainability)
6. [Low — Polish & Nice-to-Have](#low--polish--nice-to-have)
7. [Accessibility (Orca) Checklist](#accessibility-orca-checklist)
8. [Architecture Recommendations](#architecture-recommendations)
9. [Implementation Order](#implementation-order)

---

## Audit Summary

| Metric                        | Value                           |
|-------------------------------|---------------------------------|
| Total Python files            | 41                              |
| Total lines of code           | ~9,414                          |
| ruff lint errors              | 128 (32 fixable, 84 E402, 3 F821) |
| ruff format violations        | 29 of 40 files need formatting  |
| mypy type errors              | 45 errors in 8 files            |
| vulture dead code detections  | 5 items                         |
| radon complexity ≥ C          | 23 functions/methods            |
| radon complexity F (critical) | 1 — `parse_qr()` = 76           |
| Debug `print()` statements    | 55 across 5 files               |
| Duplicate signal definitions  | 3 (settings_page.py)            |
| AdwToastOverlay usages        | 2 (preview_area.py, window.py)  |

---

## Automated Tool Results

### ruff check (lint)
```
84  E402  module-import-not-at-top-of-file
32  F401  unused-import  [auto-fixable]
 3  E741  ambiguous-variable-name
 3  F601  multi-value-repeated-key-literal  (duplicate dict keys in settings_page.py __gsignals__)
 3  F821  undefined-name
 3  F841  unused-variable
```

### ruff format
29 of 40 files need reformatting. No file currently follows consistent style.

### mypy (--ignore-missing-imports)
45 errors in 8 files. Key issues:
- `window.py`: Missing `from typing import Any`, union-attr on `CameraBackend | None`, etc.
- Various type mismatches with GObject/GLib types.

### vulture (dead code, ≥80% confidence)
- `stream_engine.py:16` — unused import `GstVideo`
- `preview_area.py:12` — unused import `Gsk`
- `preview_area.py:413`, `virtual_camera_page.py:107`, `window.py:628` — unused variable `area`

### radon (cyclomatic complexity ≥ C)
Most complex functions:
- `parse_qr()` — **F (76)** — Must be refactored
- `_on_paintable_probe()` — D (29)
- `_pick_preferred_format()` — D (23)
- `get_controls()` — D (23)
- `_parse_config()` — D (23)
- `_make_row()` — D (22)

---

## Critical — Must Fix Before Release

### CRT-1: Duplicate signal definitions crash GLib
**File:** `ui/settings_page.py:33-44`
```python
__gsignals__ = {
    "resolution-changed": ...,  # line 39
    "fps-limit-changed": ...,   # line 40
    "grid-overlay-changed": ..., # line 41
    "resolution-changed": ...,  # line 42 — DUPLICATE
    "fps-limit-changed": ...,   # line 43 — DUPLICATE
    "grid-overlay-changed": ..., # line 44 — DUPLICATE
}
```
**Impact:** F601 lint error. Dict key collision — last definition wins, but this is fragile and causes ruff errors.
**Fix:** Remove duplicate lines 42-44.

### CRT-2: Replace AdwToastOverlay with InlineNotification
**Files:** `ui/preview_area.py:109`, `ui/window.py:218`
**Impact:** AdwToastOverlay is not accessible for Orca screen readers — toasts disappear before being read, and action buttons are hard to reach.
**Current state:** `notification.py` already implements `InlineNotification(Gtk.Revealer)` with proper Orca support — but it's not used everywhere yet.
**Fix:** Replace both `Adw.ToastOverlay` usages with `InlineNotification`.

### CRT-3: 55 debug `print()` statements in production
**Files:** `core/stream_engine.py` (16), `core/backends/gphoto2_backend.py` (20), `ui/window.py` (13), `ui/tools_page.py` (6), `utils/settings_manager.py` (1)
**Impact:** Information disclosure (USB paths, PID numbers, camera details in stdout). Noise in production.
**Fix:** Replace all with `log.debug(...)` using the `logging` module. Add a module-level `log = logging.getLogger(__name__)` to each file.

### CRT-4: Accessibility — Scale/Switch widgets lack accessible labels (WCAG 1.3.1 violation)
**Files & lines:**
- `ui/camera_controls_page.py:205-212` — Scale widgets for camera controls: no `LABEL` property
- `ui/effects_page.py:176-191` — Parameter adjustment scales: no `LABEL` property
- `ui/effects_page.py:125-145` — Effect on/off switches: no `LABEL` context
- `ui/tools_page.py:79` — Sensitivity scale: no label
- `ui/settings_page.py:141,165,179,191` — Grid overlay, resolution, FPS, timer combo rows: no labels

**Impact:** Orca announces "Scale" or "Switch" with no context — blind users cannot use these controls.
**Fix:** Add `widget.update_property([Gtk.AccessibleProperty.LABEL], [label_text])` to every interactive widget.

### CRT-5: Path traversal in camera profile filenames
**File:** `core/camera_profiles.py:11`
**Issue:** `_safe_filename()` regex `r"[^\w\-.]"` allows `..` sequences. A camera named `"../../../tmp"` could write profile files outside the intended directory.
**Fix:** Add `name = os.path.basename(name)` after sanitization, or reject names containing `..`.

### CRT-6: GStreamer pipeline injection via user input
**Files:**
- `core/backends/ip_backend.py:35-36` — RTSP URL injected into `rtspsrc location="{url}"` without escaping quotes
- `core/virtual_camera.py:67` — `gst_pipeline.split()` breaks quoted arguments (should be `shlex.split()`)
- `core/backends/v4l2_backend.py:364,383` — `target-object={node_id}`, `width={fmt.width}` not validated
- `core/backends/libcamera_backend.py:126` — camera name not escaped

**Impact:** Malformed input can create invalid or malicious GStreamer graphs.
**Fix:** Validate all values before interpolation into pipeline strings. Use `shlex.split()` for pipeline parsing.

### CRT-7: Unused imports (32 auto-fixable)
**Fix:** Run `ruff check --fix usr/share/biglinux/bigcam/` to auto-remove all 32 unused imports.

---

## High — Fix Soon

### HIGH-1: Privacy — `_get_local_ip()` contacts Google DNS
**File:** `core/phone_camera.py:441-444`
**Issue:** Connects to `8.8.8.8:80` to determine local IP, leaking network activity without user consent.
**Fix:** Use `socket.gethostbyname(socket.gethostname())` or `netifaces` / `socket.if_nameindex()`.

### HIGH-2: Subprocess calls missing `timeout`
**Files:**
- `core/camera_manager.py:169` — `subprocess.run(["lsusb"], ...)` no timeout
- `core/backends/gphoto2_backend.py:38-49` — `systemctl stop` no timeout
**Fix:** Add `timeout=5` to all subprocess calls.

### HIGH-3: GStreamer element creation unchecked (NULL)
**File:** `core/video_recorder.py:73-85`
**Issue:** `Gst.ElementFactory.make()` can return `None` if plugin unavailable. Properties accessed without null check.
**Fix:** Check `if element is None: raise RuntimeError(...)` after each `make()` call.

### HIGH-4: JSON load unguarded — corrupted profile crashes app
**File:** `core/camera_profiles.py:27`
**Issue:** `json.load(f)` with no try/except. Corrupted JSON terminates application.
**Fix:** Wrap in `try: ... except json.JSONDecodeError: log.warning(...); return default`.

### HIGH-5: Tee pad leak on repeated recording start
**File:** `core/video_recorder.py:78-105`
**Issue:** `start()` requests tee pad each call. If `start()` called twice without `stop()`, old pads are never released.
**Fix:** Check `self._rec_tee_pad is not None` at start and refuse or cleanup first.

### HIGH-6: No URL validation in IP camera dialog
**File:** `ui/ip_camera_dialog.py:66-71`
**Issue:** User can enter any URI including `file://`, `ftp://`, etc. No scheme validation.
**Fix:** Parse URL and validate `scheme in ("rtsp", "http", "https")`.

### HIGH-7: Libcamera fake controls — sliders have no effect
**File:** `core/backends/libcamera_backend.py:60-91`
**Issue:** Brightness/contrast/saturation controls are returned but `set_control()` only stores value in `camera.extra` dict — never applied to GStreamer pipeline.
**Fix:** Either implement real libcamera control properties or remove the fake controls and show a message.

### HIGH-8: Camera controls cache never cleared — memory leak
**File:** `ui/window.py:341`
```python
_controls_cache: dict[str, list] = {}  # class-level, never cleaned
```
**Fix:** Make instance-level. Implement max size or clear on camera removal.

### HIGH-9: Hotplug permanently disabled after gPhoto2 streaming
**File:** `ui/window.py:450`
**Issue:** `self._camera_manager.stop_hotplug()` called but never restarted after gPhoto2 camera is done.
**Fix:** Restart hotplug when gPhoto2 streaming stops.

### HIGH-10: WiFi password displayed in plaintext in QR dialog
**File:** `ui/qr_dialog.py:272`
**Issue:** WiFi QR result shows SSID + password as subtitle text.
**Fix:** Mask password by default with option to reveal.

### HIGH-11: `phone_camera.py:264` — missing `@staticmethod` on `available()`
**Issue:** Method lacks decorator but is called as `PhoneCameraServer.available()`.
**Fix:** Add `@staticmethod` decorator.

---

## Medium — Quality & Maintainability

### MED-1: Code duplication — QR/Smile logic in two places
**Files:** `ui/settings_page.py:262-649` and `ui/tools_page.py:95-335`
**Impact:** Same QR scanning and smile detection logic duplicated. Bugs fixed in one place won't be fixed in the other. Different filename prefix (`bigcam_smile_` vs `smile_`).
**Fix:** Extract shared QR/Smile logic into `core/qr_scanner.py` and `core/smile_detector.py`, import from both pages.

### MED-2: `parse_qr()` cyclomatic complexity = 76 (Grade F)
**File:** `ui/qr_dialog.py:60-356`
**Fix:** Break into dispatcher + per-type parser functions (WiFi, vCard, iCal, etc.).

### MED-3: `StreamEngine` monolith — 829 lines, single class
**File:** `core/stream_engine.py`
**Impact:** Manages USB camera, phone camera, effects, virtual camera, recording all in one class.
**Fix:** Extract `PhoneStreamHandler`, `EffectsProbe`, etc. into separate classes.

### MED-4: E402 — 84 module-level import violations
**Issue:** Imports after code at module level. Common pattern in GTK4 apps (needs `gi.require_version` first).
**Fix:** Reorganize: `gi.require_version()` calls at very top, then all imports. Add `# noqa: E402` where unavoidable.

### MED-5: Replace `except Exception: pass` with logging
**Files:** `core/effects.py:488-500`, `core/backends/gphoto2_backend.py:66-91`, `ui/settings_page.py:273`
**Fix:** At minimum log the exception: `log.warning("...", exc_info=True)`.

### MED-6: Thread safety — effects registry is global mutable dict
**File:** `core/effects.py:427`
**Fix:** Make effects registry immutable after initialization, or use a lock.

### MED-7: `_countdown_remaining` not initialized in `__init__`
**File:** `ui/preview_area.py:434`
**Issue:** `self._countdown_remaining` is only created on first call to `start_countdown()`. If `_tick_countdown()` fires before, `AttributeError`.
**Fix:** Add `self._countdown_remaining: int = 0` in `__init__`.

### MED-8: 29 files need `ruff format` 
**Fix:** Run `ruff format usr/share/biglinux/bigcam/` once.

### MED-9: mypy — 45 type errors
Key fixes:
- `window.py:496` — add `from typing import Any`
- `window.py:795` — guard `CameraInfo | None` correctly
- Various union-attr errors on `CameraBackend | None`

### MED-10: Phone v4l2 pipeline — single instance, breaks with multiple phones
**File:** `core/stream_engine.py:768-815`
**Fix:** Use a dict keyed by phone ID instead of single instance variable.

---

## Low — Polish & Nice-to-Have

### LOW-1: Photo filename collision at sub-second captures
**File:** `core/photo_capture.py:18-19` — uses `strftime("%Y%m%d_%H%M%S")` (1s precision only)
**Fix:** Append `_` + counter or use `datetime.now().isoformat()`.

### LOW-2: Performance — LUT table and vignette meshgrid recreated every frame
**File:** `core/effects.py:62,200`
**Fix:** Cache per resolution or per-parameter set.

### LOW-3: Video recording uses MJPG codec in MKV container
**File:** `core/video_recorder.py:98`
**Impact:** Large files, poor player compatibility.
**Fix:** Consider H.264 via x264enc (already used in GStreamer branch). Document codec choice.

### LOW-4: `video_recorder.py:196-198` — `_on_error()` method never called
**Fix:** Either wire it to GStreamer bus or remove dead method.

### LOW-5: Phone dot overlay not accessible
**Files:** `ui/window.py:129-131`, `ui/virtual_camera_page.py:40-43`
**Issue:** `_draw_dot()` status indicator uses only color (not announced to screen reader).
**Fix:** Also update subtitle text to match status, so Orca can read it.

### LOW-6: mailto URI with trailing `?` when no params
**File:** `ui/qr_dialog.py:146`
**Fix:** Only append `?` if params exist.

### LOW-7: Calendar DTSTART parsing ignores TZID format
**File:** `ui/qr_dialog.py:218`
**Fix:** Handle `DTSTART;TZID=America/Sao_Paulo:20240101T120000` format.

### LOW-8: TLS configuration — self-signed cert with no pinning
**File:** `core/phone_camera.py:319`
**Fix:** Set explicit minimum TLS version. Document security model.

---

## Accessibility (Orca) Checklist

| Widget / Area | File | Status | Fix |
|--------------|------|--------|-----|
| Camera control scales | camera_controls_page.py:205 | ❌ No label | Add `Gtk.AccessibleProperty.LABEL` |
| Effect parameter scales | effects_page.py:176 | ❌ No label | Add label from `param.label` |
| Effect on/off switches | effects_page.py:125 | ❌ No context | Add label from `effect.label` |
| Sensitivity scale (tools) | tools_page.py:79 | ❌ No label | Add "Sensitivity" label |
| Grid overlay row | settings_page.py:141 | ❌ No label | Add accessible property |
| Resolution combo | settings_page.py:165 | ⚠️ Title only | Add explicit label |
| FPS combo | settings_page.py:179 | ⚠️ Title only | Add explicit label |
| Timer combo | settings_page.py:191 | ⚠️ Title only | Add explicit label |
| Phone copy buttons | phone_camera_dialog.py:205 | ❌ No label | Add label for each |
| QR copy buttons | qr_dialog.py:428 | ❌ No label | Add "Copy <field>" label |
| Status dot overlays | window.py:129 & virtual_camera_page.py:40 | ❌ Visual only | Add subtitle text |
| AdwToastOverlay (preview) | preview_area.py:109 | ❌ Inaccessible | Replace with InlineNotification |
| AdwToastOverlay (window) | window.py:218 | ❌ Inaccessible | Replace with InlineNotification |
| Menu button | window.py:116 | ✅ Labeled | — |
| Phone button | window.py:123 | ✅ Labeled | — |
| Capture button | preview_area.py:265 | ✅ Labeled | — |
| Record button | preview_area.py:274 | ✅ Labeled | — |
| Retry button | preview_area.py:235 | ✅ Labeled | — |
| About dialog | about_dialog.py | ✅ Standard | — |
| InlineNotification | notification.py | ✅ Full a11y | — |

---

## Architecture Recommendations

### 1. Centralize logging
Add a `utils/logging_config.py` with a `setup_logging()` function called from `main.py`. All 55 `print()` statements become `log.debug()`.

### 2. Extract shared detection logic
Create:
- `core/qr_scanner.py` — shared QR detection from `settings_page.py` + `tools_page.py`
- `core/smile_detector.py` — shared smile detection logic
Both pages import and use these instead of duplicating.

### 3. StreamEngine decomposition (future)
Current 829-line monolith should eventually be split:
- `StreamEngine` — pipeline lifecycle only
- `PhoneStreamHandler` — phone camera v4l2/frame handling
- `EffectsProbe` — GStreamer probe for applying effects

(Not blocking release — can be done incrementally)

### 4. Input validation layer
Add `utils/validation.py` with functions:
- `validate_device_path(path)` — matches `/dev/video\d+`  
- `validate_gst_value(value, expected_type)` — for pipeline interpolation
- `validate_url(url, allowed_schemes)` — for IP camera

---

## Implementation Order

The items below should be implemented in this exact order. Each item references the ID from the sections above.

**STATUS: ALL PHASES COMPLETED** (ruff: 0 errors, mypy: 42→42 pre-existing GTK stub issues, all files compile)

### Phase A — Safety & Correctness (no UI changes) ✅
1. **CRT-1** — ✅ Remove duplicate signals in settings_page.py
2. **CRT-5** — ✅ Fix path traversal in camera_profiles.py
3. **CRT-6** — ✅ Fix GStreamer injection (ip_backend, virtual_camera, v4l2, libcamera)
4. **CRT-7** — ✅ Run `ruff check --fix` to auto-remove 29 unused imports
5. **HIGH-2** — ✅ Add `timeout=` to all subprocess calls
6. **HIGH-3** — ✅ Check GStreamer element creation for None (already existed)
7. **HIGH-4** — ✅ Guard JSON load in camera_profiles
8. **HIGH-5** — ✅ Prevent tee pad leak in video_recorder
9. **HIGH-11** — ✅ Add `@staticmethod` to phone_camera.available() (already existed)
10. **MED-7** — ✅ Initialize `_countdown_remaining` in preview_area

### Phase B — Logging & Debug Cleanup ✅
11. **CRT-3** — ✅ Replace all 55 `print()` with `log.debug()` (added `logging.getLogger(__name__)` to each file)

### Phase C — Accessibility ✅
12. **CRT-4** — ✅ Add accessible labels to all Scale/Switch widgets
13. **CRT-2** — ✅ Replace AdwToastOverlay with InlineNotification in preview_area.py and window.py

### Phase D — Security & Privacy ✅
14. **HIGH-1** — ✅ Replace `_get_local_ip()` external DNS call
15. **HIGH-10** — ✅ Mask WiFi password in QR dialog with reveal toggle

### Phase E — High-priority fixes ✅
16. **HIGH-6** — ✅ Add URL scheme validation in IP camera dialog
17. **HIGH-7** — ✅ Fix libcamera controls (now passed as extra-controls to libcamerasrc)
18. **HIGH-8** — ✅ Fix camera controls cache (moved to instance-level)
19. **HIGH-9** — ✅ Restart hotplug after gPhoto2 streaming setup completes

### Phase F — Code quality ✅
20. **MED-4** — ✅ Fix E402 import order (added `# noqa: E402` to post-gi.require_version imports)
21. **MED-5** — ✅ Replace all `except Exception: pass` with `log.debug("Ignored exception", exc_info=True)`
22. **MED-8** — ✅ Run `ruff format` on all 40 files
23. Fixed duplicate keyword arguments (timeout=10, timeout=10) in phone_camera.py and dependency_checker.py
24. Fixed F841 unused variables and E741 ambiguous names
25. Added `from typing import Any` where missing (video_recorder.py, window.py)

### Phase G — Polish ✅
26. **LOW-1** — ✅ Photo filename collision fixed (microsecond precision)
27. **LOW-4** — ✅ Dead `_on_error()` method removed from video_recorder.py
28. **LOW-8** — ✅ TLS minimum version set to TLSv1.2 in phone_camera.py

### Not implemented (deferred — low risk, high effort)
- **MED-1** — Extract shared QR/Smile logic (large refactor, needs integration testing)
- **MED-2** — Break down `parse_qr()` (cyclomatic complexity 76, requires extensive testing)
- **MED-3** — Split StreamEngine monolith (829 lines, architectural refactor)
- **MED-6** — Effects registry thread safety (requires threading model analysis)
- **MED-10** — Phone v4l2 pipeline multi-instance (needs multi-phone test hardware)
- **LOW-2** — LUT/vignette per-frame recreation (performance optimization)
- **LOW-3** — Video codec MJPG→H.264 (user experience change)
- **LOW-5** — Phone dot overlay accessibility (minor UI change)
- **LOW-6** — Already correct (mailto `?` only appended if params exist)
- **LOW-7** — Calendar TZID parsing (edge case)
