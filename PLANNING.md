# PLANNING.md — Project Improvement Roadmap

## Files Analyzed
**Total files read:** 41  
**Total lines analyzed:** 9,551  
**Large files (>500 lines) confirmed read in full:**
- `ui/window.py` (877 lines)
- `core/stream_engine.py` (829 lines)
- `core/backends/gphoto2_backend.py` (791 lines)
- `ui/qr_dialog.py` (588 lines)
- `ui/settings_page.py` (572 lines)
- `core/effects.py` (506 lines)
- `core/phone_camera.py` (491 lines)
- `ui/preview_area.py` (457 lines)
- `core/backends/v4l2_backend.py` (456 lines)

---

## Current State Summary

BigCam is a GTK4/libadwaita webcam application with 41 Python source files (~9.5k LOC). Core architecture separates backends (V4L2, gPhoto2, libcamera, PipeWire, IP, Phone) from UI cleanly. GStreamer handles video pipelines. The app is functional but has several critical issues:

**What works well:**
- Backend abstraction via ABC + enum pattern
- Multiple camera support (USB, DSLR, IP, Phone)
- Effects pipeline with OpenCV
- i18n infrastructure (26+ locales)
- XDG-compliant configuration/storage

**What needs immediate attention:**
- Security: shell injection vulnerabilities in gphoto2 backend
- Data corruption: non-atomic settings writes, bool coercion bug
- Accessibility: visual-only status indicators, missing accessible labels
- CSS: hardcoded colors break light theme
- Code quality: 133 ruff errors, 45 mypy errors, dead code, silent exception swallowing

**Overall quality grade: C+** — Functional but fragile; one bad camera name or settings race could cause data loss or security issue.

---

## Automated Tool Results (Baseline)

| Tool | Result |
|------|--------|
| ruff check | 133 errors (87 E402, 35 F401, 3 F841, 3 F601, 3 E741, 2 F821) |
| ruff format | 30/41 files need reformatting |
| mypy | 45 errors in 8 files |
| vulture | 6 dead code items (3 unused imports, 3 unused variables) |
| radon cc ≥C | 23 functions with complexity ≥C (1 at F=76: `parse_qr`) |
| tech debt markers | 0 TODO/FIXME/HACK/XXX |

---

## Critical (fix immediately)

### Security

- [ ] **Shell injection in pkill regex**: `gphoto2_backend.py:443-445` — `f"gphoto2.*--port {launch_port}"` interpolated into pkill `-f` pattern. If `launch_port` contains regex metacharacters, it's command injection. → Sanitize with `re.escape()` or use PID-based kill instead of pattern matching.
- [ ] **Command injection in IP backend**: `ip_backend.py:80-82` — `src.split()` tokenizes GStreamer pipeline string by whitespace. Malformed URL with spaces breaks command. → Build argument list properly, don't split user-provided strings.
- [ ] **GStreamer pipeline path injection**: `video_recorder.py:157`, `stream_engine.py:294` — File paths and device paths interpolated into `Gst.parse_launch()` strings without escaping. Paths with spaces/quotes break parsing. → Use `shlex.quote()` or GStreamer element property API.

### Data Integrity

- [ ] **Boolean coercion bug**: `settings_manager.py:56` — `bool(value)` where value is string. `bool("false") == True`. → Implement proper bool parsing: `value in ("true", "1", "yes", True)`.
- [ ] **Non-atomic settings writes**: `settings_manager.py:87` — Direct `json.dump()` to target file. Crash mid-write corrupts settings. → Write to temp file, then `os.rename()` (atomic on same filesystem).
- [ ] **Thread-unsafe SettingsManager**: `settings_manager.py` — No locking on concurrent `set()` calls from multiple threads. → Add `threading.Lock` around `_save()`.

### Bugs

- [ ] **NV12 color space mismatch**: `stream_engine.py:184` — Uses `cv2.COLOR_BGR2YUV_I420` for NV12 format output. Should use NV12-specific conversion. → Fix color space constant or use proper NV12 conversion pipeline.
- [ ] **Duplicate signal definitions**: `settings_page.py:38-43` — Signals `resolution-changed`, `fps-limit-changed`, `grid-overlay-changed` defined twice. Could cause handlers to fire twice. → Remove duplicate definitions.
- [ ] **V4L2 fps_list uninitialized**: `v4l2_backend.py:189` — `fps_list` used before definition in some code paths of `_parse_formats_ext()`. → Initialize `fps_list = []` before the loop.
- [ ] **V4L2 fps accumulation across sizes**: `v4l2_backend.py:189-205` — FPS values from different resolutions bleed into each other. → Flush fps_list when size changes.

### CSS/Theme

- [ ] **Hardcoded dark color**: `style.css:4` — `.preview-area { background-color: #1a1a1a }` breaks light theme. → Use `background-color: @theme_base_color;`.
- [ ] **Hardcoded white text**: `style.css:31` — `.countdown-overlay { color: white }` invisible on light backgrounds. → Use `color: @theme_fg_color;` with text-shadow fallback.

---

## High Priority (code quality)

### Error Handling

- [ ] **Silent exception swallowing**: `effects.py:476,503`, `stream_engine.py:188`, `tools_page.py:210`, `camera_manager.py:47,187` — Multiple `except Exception: pass` blocks hide real errors. → Add `log.warning()`/`log.error()` with traceback to all bare except blocks.
- [ ] **No input validation in IP camera dialog**: `ip_camera_dialog.py:58-59` — Empty URL silently ignored, no feedback. → Show inline error or notification when URL is empty/malformed.

### Resource Management

- [ ] **gPhoto2 orphan processes**: `gphoto2_backend.py:391-393` — Streaming subprocess PID not tracked; `_active_streams` can grow unbounded. → Store Popen objects, kill by PID on stop, clean up on app exit.
- [ ] **Controls cache unbounded**: `window.py:290` — `_controls_cache` dict grows indefinitely with no eviction. → Add LRU strategy or clear on camera disconnect.

### Code Quality

- [ ] **Unused imports (35)**: All F401 ruff errors — `utils.i18n._` imported but unused in video_recorder.py, virtual_camera.py; `re` unused in virtual_camera.py; etc. → Run `ruff check --fix` for auto-fixable imports.
- [ ] **Module-level import ordering (87)**: E402 violations caused by `gi.require_version()` calls before imports. → Reorganize: gi.require_version block at top, then gi.repository imports, then local imports.
- [ ] **Dead code in V4L2**: `v4l2_backend.py:142-157` — Format parsing code that's immediately overwritten by `_parse_formats_ext()` call. → Remove dead parsing block.
- [ ] **`print()` debug statements**: `window.py:337`, `tools_page.py:130,135,147,173` — Should use `logging.debug()`. → Replace all `print()` with `log.debug()`.

### Type Safety

- [ ] **Mypy errors (45)**: Mostly `call-overload` and `union-attr` issues in settings_page.py, window.py, stream_engine.py. → Add proper type narrowing and casts.
- [ ] **Missing type annotations**: `stream_engine.py:88` (`_gtksink: Any`), `video_recorder.py:42` (`_cv_writer: Any`), `effects.py:235` (`list[tuple[EffectInfo, Any]]`). → Use specific types.

---

## Medium Priority (UX improvements)

### Accessibility — Orca Screen Reader

- [ ] **Visual-only status dots**: `preview_area.py`, `phone_camera_dialog.py`, `virtual_camera_page.py`, `window.py` — DrawingArea status dots with color coding provide no accessible information. **Blind users cannot determine connection state.** → Add `set_accessible_role()` and `update_property(Gtk.AccessibleProperty.LABEL, "Connected")` that updates with state changes.
- [ ] **Countdown overlay inaccessible**: `preview_area.py:161-165` — Countdown timer is visual text overlay only. **Blind user doesn't know capture is imminent.** → Add accessible live region announcement or audio cue (system beep via `Gdk.Display.beep()`).
- [ ] **FPS label not announced**: `preview_area.py:127` — FPS counter is visual-only label. → Set `set_accessible_role(Gtk.AccessibleRole.STATUS)` as live region.
- [ ] **QR code image inaccessible**: `phone_camera_dialog.py` — QR code displayed as `Gtk.Picture` with no alternative text. **Blind user cannot scan QR.** → Add accessible description with the URL text so Orca announces it.
- [ ] **Action buttons missing labels**: `qr_dialog.py:550-561` — FlowBox action buttons created with icon+label child but button itself lacks `set_accessible_name()`. → Set accessible-name matching the visible label.
- [ ] **Scale widgets lack range**: `camera_controls_page.py`, `effects_page.py` — Scale adjustments don't announce min/max/current values clearly to Orca. → Set `Gtk.AccessibleProperty.VALUE_NOW`, `VALUE_MIN`, `VALUE_MAX`.

### UX — Feedback Loops

- [ ] **No feedback on control changes**: `camera_controls_page.py:247-280` — User adjusts slider/switch but gets zero confirmation that change was applied. **Creates uncertainty: "Did it work?"** → Show brief inline status or subtle animation on the adjusted row.
- [ ] **No feedback on effect parameter change**: `effects_page.py:196-212` — Same issue: effect parameter adjusted silently. → Visual feedback on the preview area (brief flash or label).
- [ ] **Missing keyboard shortcut hints**: `window.py:224-233` — Shortcuts defined (Ctrl+P, Ctrl+R, F5, etc.) but not shown anywhere in the UI. **Users don't discover features.** → Add shortcut hints to tooltips or menu items.

### UX — Error Prevention

- [ ] **IP camera URL validation**: `ip_camera_dialog.py:58-59` — No format validation (RTSP, HTTP). User can enter garbage. → Validate URL format, show inline error for malformed input.
- [ ] **Streaming lock potential deadlock**: `window.py:330-371` — Complex gPhoto2 streaming coordination with non-blocking lock acquisition. If main thread holds lock, background thread can't proceed. → Simplify coordination; use GLib.idle_add for all state transitions.

### UX — Progressive Disclosure

- [ ] **Gallery 100-item limit**: `photo_gallery.py:81`, `video_gallery.py:93` — Silently drops items beyond 100. **User doesn't know files exist.** → Show "Showing 100 of N" message or implement pagination.

---

## Low Priority (polish & optimization)

### Performance

- [ ] **Denoise effect too slow**: `effects.py:103-105` — `cv2.fastNlMeansDenoisingColored()` is O(n²) spatially; causes frame drops on live preview. → Add warning in UI when enabled, or apply only on still capture.
- [ ] **Face detection per-frame**: `effects.py:198-231` — Haar cascade runs on every frame for background blur. → Add skip-frame logic (detect every 5th frame, interpolate).
- [ ] **Thumbnail generation blocking**: `video_gallery.py:200-217` — FFmpeg subprocess for thumbnails runs during UI refresh. → Move to background thread with placeholder skeleton.
- [ ] **parse_qr complexity F(76)**: `qr_dialog.py:60` — Cyclomatic complexity of 76. → Split into type-specific parser functions.

### Code Organization

- [ ] **Ruff formatting**: 30 files need reformatting. → Run `ruff format .` once, commit as formatting-only change.
- [ ] **Root main.py duplicate**: Root `main.py` (68 lines) is a simplified copy of `usr/.../main.py` (96 lines). → Determine which is canonical; remove the other or make root a thin wrapper.
- [ ] **Hardcoded control categories**: `gphoto2_backend.py:241-287` — 60+ lines of manual category mapping. → Move to external JSON/TOML data file.
- [ ] **EffectPipeline callable type**: `effects.py:235` — Uses `Any` for callable. → Type as `Callable[[np.ndarray, dict[str, float]], np.ndarray]`.

### Minor Bugs & Polish

- [ ] **Brightness effect range**: `effects.py:88` — Beta value for `cv2.convertScaleAbs()` not normalized; range -100 to 100 may cause extreme output. → Scale to reasonable range.
- [ ] **Colormap hardcoded limit**: `effects.py:171` — Clamps to 21 colormaps. → Query `len(cv2.COLORMAP_*)` dynamically or document limit.
- [ ] **Phone FPS hardcoded**: `video_recorder.py:347` — OpenCV writer assumes 30fps. → Accept fps parameter from phone camera.
- [ ] **JSON encoding**: `camera_profiles.py:34,43` — No explicit `encoding='utf-8'`. → Add `encoding='utf-8'` to all `open()` calls.
- [ ] **Weak RSA key**: `phone_camera.py:450` — 2048-bit RSA for self-signed cert. → Use 4096-bit or switch to EC key.

---

## Architecture Recommendations

1. **Atomic Settings**: Replace raw JSON write with write-to-temp + `os.rename()`. Add `threading.Lock` for concurrent access. Fix bool coercion.

2. **GStreamer Pipeline Safety**: Create a helper `safe_gst_element(name, **props)` that uses `Gst.ElementFactory.make()` + `set_property()` instead of string interpolation in `Gst.parse_launch()`. This eliminates all pipeline injection risks.

3. **Backend Process Management**: Replace `pkill -f` pattern matching with explicit PID tracking via `Popen.pid`. Store active processes in a process registry with cleanup-on-exit via `atexit`.

4. **Error Logging Standard**: Establish convention: no bare `except: pass`. Minimum is `log.debug()` with traceback. Critical paths use `log.error()` with user notification.

5. **Accessibility Layer**: Create `a11y_helpers.py` utility with functions like `set_status_dot_accessible(widget, state_text)` that wraps the common pattern of updating accessible labels on visual-only components.

---

## UX Recommendations

1. **Control Change Feedback** (Principle: *Feedback Loop / Norman's Gulf of Evaluation*): When a camera control is adjusted, the user needs confirmation that the change took effect. A brief 300ms highlight animation or checkmark icon on the row provides this without cognitive interruption.

2. **Gallery Pagination** (Principle: *Progressive Disclosure*): Showing 100 items and silently dropping the rest violates user expectations. Add a simple "Showing 100 of 247 — Load more" button. This respects performance constraints while maintaining user trust.

3. **IP Camera Validation** (Principle: *Error Prevention > Error Messages*): Disable the "Add" button until URL field contains a valid format (starts with `rtsp://`, `http://`, or `https://`). This prevents invalid entries entirely rather than showing error after the fact.

4. **Keyboard Shortcuts Discovery** (Principle: *Learnability*): Add shortcut hints to button tooltips: "Capture Photo (Ctrl+P)". Users who hover or focus will discover shortcuts naturally without dedicated documentation.

5. **Effect Toggle Clarity** (Principle: *Visibility of System Status*): The dual-control pattern (expander arrow + switch) on effects is confusing. Make the switch control both expansion AND activation: when switch turns on, row expands to show parameters; when off, collapses. This maps one action to one outcome.

---

## Orca Screen Reader Compatibility

### Issues Found

- [ ] **Status dots** (4 locations): `preview_area.py`, `phone_camera_dialog.py:86-95`, `virtual_camera_page.py:32-38`, `window.py:71-83` — DrawingArea-based colored dots have no accessible role or label. **Orca announces nothing.** → Update accessible label on each state change.
- [ ] **Countdown overlay**: `preview_area.py:161-165` — Visual-only countdown number. **Blind user has no warning before capture.** → Emit accessible announcement or system beep.
- [ ] **QR code picture**: `phone_camera_dialog.py:73-82` — `Gtk.Picture` with no alt text. → Set accessible description to the URL text.
- [ ] **FPS counter**: `preview_area.py:127` — Visual-only. → Mark as `AccessibleRole.STATUS` live region.
- [ ] **Grid overlay**: `preview_area.py:168-172` — Visual-only drawing. → Not critical (compositional aid), but should be toggleable by keyboard.
- [ ] **Scale value announcements**: `camera_controls_page.py`, `effects_page.py` — Scales don't explicitly expose `VALUE_NOW`/`VALUE_MIN`/`VALUE_MAX`. → Set accessible value properties.

### Test Checklist for Manual Verification

- [ ] Launch app with Orca running (`orca &; python main.py`)
- [ ] Navigate entire UI using only Tab/Shift+Tab
- [ ] Verify Orca announces every button, field, and state change
- [ ] Test camera selection dropdown with arrow keys
- [ ] Test capture flow (Ctrl+P) without looking at screen — verify countdown announced
- [ ] Test recording flow (Ctrl+R) — verify start/stop state announced
- [ ] Navigate effects page — verify each effect name and parameter announced
- [ ] Navigate settings page — verify combo selections announced with current value
- [ ] Open phone camera dialog — verify connection status announced
- [ ] Verify error messages are announced by Orca (disconnect camera during preview)

---

## Accessibility Checklist (General)

- [ ] All interactive elements have accessible labels *(~85% complete; status dots and overlays missing)*
- [ ] Keyboard navigation works for all flows *(mostly complete; shortcuts exist but undiscoverable)*
- [ ] Color is never the only indicator *(PARTIALLY FAILING: status dots rely on color alone)*
- [ ] Text is readable at 2x font size *(not tested; AdwClamp helps responsiveness)*
- [ ] Focus indicators are visible *(Adwaita default focus rings present)*

---

## Tech Debt

### From ruff (grouped by type)
| Code | Count | Description | Fix |
|------|-------|-------------|-----|
| E402 | 87 | Import not at top of file | Reorder after `gi.require_version()` blocks |
| F401 | 35 | Unused imports | `ruff check --fix` (auto-fixable) |
| F841 | 3 | Unused local variables | Remove assignments |
| F601 | 3 | `bool(x) is True` comparisons | Simplify to `x` |
| E741 | 3 | Ambiguous variable names (l, I, O) | Rename |
| F821 | 2 | Undefined names | Fix reference or import |

### From mypy (grouped by file)
| File | Errors | Primary Issue |
|------|--------|---------------|
| settings_page.py | 8 | `call-overload`, `arg-type` from `dict.get()` with wrong types |
| window.py | 12 | `union-attr` on optional CameraBackend; `arg-type` mismatches |
| stream_engine.py | 4 | `attr-defined` (CameraBackend missing `start_streaming`) |
| qr_dialog.py | 4 | Variable redefinition, type mismatch (str vs list) |
| phone_camera.py | 2 | Optional event loop access |
| video_recorder.py | 1 | Optional pipeline access |
| tools_page.py | 2 | None attribute access |
| settings_manager.py | 2 | `call-overload` on int/float coercion |

### From vulture (dead code)
| File | Line | Item | Confidence |
|------|------|------|------------|
| stream_engine.py | 16 | Unused import `GstVideo` | 90% |
| video_recorder.py | 16 | Unused import `GstApp` | 90% — **FALSE POSITIVE**: GstApp required for `Gst.ElementFactory.make("appsink")` |
| preview_area.py | 12 | Unused import `Gsk` | 90% — **FALSE POSITIVE**: Used in `MirroredPicture.do_snapshot()` |
| preview_area.py | 413 | Unused variable `area` | 100% |
| virtual_camera_page.py | 107 | Unused variable `area` | 100% |
| window.py | 629 | Unused variable `area` | 100% |

### From radon (high complexity)
| File | Method | Grade | CC |
|------|--------|-------|----|
| qr_dialog.py | `parse_qr` | **F** | 76 |
| stream_engine.py | `_on_paintable_probe` | D | 29 |
| gphoto2_backend.py | `get_controls` | D | 23 |
| gphoto2_backend.py | `_parse_config` | D | 23 |
| camera_controls_page.py | `_make_row` | D | 22 |
| window.py | `_pick_preferred_format` | D | 23 |

---

## Metrics (before)

```
Files: 41
Lines: 9,551
Ruff errors: 133 (35 auto-fixable)
Mypy errors: 45
Vulture dead code: 6 items
Radon CC ≥ C: 23 functions
Formatting violations: 30 files
```
