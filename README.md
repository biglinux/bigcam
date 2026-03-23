<p align="center">
  <img src="usr/share/biglinux/bigcam/icons/bigcam.svg" alt="BigCam" width="128" height="128">
</p>

<h1 align="center">BigCam 4.2.0</h1>

<p align="center">
  <b>The universal webcam control center for Linux — use any camera, including your smartphone, as a professional webcam. No expensive apps needed.</b>
</p>

<p align="center">
  <a href="#-the-story">The Story</a> •
  <a href="#-why-bigcam">Why BigCam?</a> •
  <a href="#-supported-cameras">Supported Cameras</a> •
  <a href="#-use-your-phone-as-a-webcam">Phone as Webcam</a> •
  <a href="#-features">Features</a> •
  <a href="#-installation">Installation</a> •
  <a href="#-architecture">Architecture</a> •
  <a href="#-contributing">Contributing</a> •
  <a href="#-license">License</a>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Version-4.2.0-brightgreen.svg" alt="Version 4.2.0">
  <img src="https://img.shields.io/badge/License-GPLv3-blue.svg" alt="License: GPL v3">
  <img src="https://img.shields.io/badge/Platform-Linux-green.svg" alt="Platform: Linux">
  <img src="https://img.shields.io/badge/GTK-4.0-blue.svg" alt="GTK 4.0">
  <img src="https://img.shields.io/badge/Libadwaita-1.x-purple.svg" alt="Libadwaita">
  <img src="https://img.shields.io/badge/Python-3.14-yellow.svg" alt="Python 3.14">
  <img src="https://img.shields.io/badge/Languages-29-orange.svg" alt="29 Languages">
</p>

---

## The Story

**BigCam** was born from a real need. It started as a humble shell script written by **Rafael Ruscher** and **Barnabé di Kartola** so that Ruscher could use his Canon Rebel T3 as a webcam during his live streams about [BigLinux](https://www.biglinux.com.br/). That small hack proved so useful that it evolved — first into a more capable script, then into a full-blown GTK4/Adwaita application deeply integrated into the BigLinux ecosystem.

**Version 1.0** was a simple Bash script that bridged gPhoto2 with FFmpeg, streaming DSLR output to a virtual V4L2 device. It worked, but it was fragile — no error handling, no hotplug, no live preview.

**Version 2.0** was the complete rewrite in Python with GTK4 and Libadwaita. It brought a proper GUI with live preview, multi-backend camera support (V4L2, gPhoto2, libcamera, PipeWire, IP cameras), real-time OpenCV effects, virtual camera output, photo/video capture, and a modular architecture with separated business logic and UI.

**Version 3.0** introduced the smartphone camera feature — turning any phone into a wireless webcam using only a web browser, no app installation required — alongside software-based camera controls (digital zoom, pan/tilt, sharpness, backlight compensation), an improved QR scanner with visual overlays, smarter smile detection with consecutive frame validation, complete internationalization coverage (29 languages), and dozens of refinements across the board.

**Version 4.0** was a complete UX overhaul — redesigned bottom bar with quick-access toggle buttons (QR scanner, smile capture, virtual camera, mirror), welcome screen dialog for first-time users, help-on-hover tooltips system, always-on-top window pin (Wayland-compatible via KWin D-Bus), fullscreen mode, capture timer with bi-directional sync between header and settings, window-level notification banner (Adw.Banner), mode-aware "last media" thumbnail (photo/video with ffmpeg preview), gPhoto2 capture flow improvements (mode dialog before timer), MediaPipe-powered background blur with real person segmentation (replacing the old Haar cascade face detection), zoom/pan/tilt/sharpness/backlight for gPhoto2 and IP camera pipelines, recording timer overlay, flash effect on capture, and a complete CSS restructuring.

**Version 4.0** consolidated all improvements into a single major release: barcode scanner (zbar), recording codec selector (H.264/H.265/VP9/MJPEG with HW acceleration), camera control profiles, control dependencies (auto-exposure/white-balance/focus), SpinButton on V4L2 integer controls, anti-flicker auto-set, effect pipeline optimizations, CSD window controls, and a full dependency audit.

**Version 4.2.0** (current) focused on performance and stability:

- **OpenCV V4L2 direct capture**: Replaced GStreamer pipeline with native OpenCV VideoCapture using V4L2 mmap backend for USB cameras. Eliminates the flickering, stuttering, and frame drops that plagued the GStreamer pipeline on USB 2.0 hardware. GStreamer remains as a fallback for non-V4L2 sources.
- **Background capture thread**: Dedicated daemon thread handles blocking `cap.read()` calls, keeping the GTK main loop responsive. Render timer on the main thread picks up the latest frame at display refresh rate.
- **Nix support**: Added `flake.nix`, `default.nix`, and `.envrc` for reproducible builds and development environments via Nix.
- **About dialog**: Now features the project's origin story and credits the original authors (Rafael Ruscher and Barnabé di Kartola).
- **CSD button fix**: Window control buttons (minimize, maximize, close) no longer show hover highlight artifacts.
- **JPEG warning suppression**: Corrupt JPEG data warnings from libjpeg-turbo are silenced during capture. OpenCV internal log level set to ERROR.
- **Project cleanup**: Removed 125+ unnecessary files (backup files, legacy artifacts, duplicate assets).

We are grateful to Rafael and Barnabé for starting this journey.

---

## What's New in 4.2.0

### OpenCV V4L2 Direct Capture

The GStreamer pipeline for USB cameras has been replaced by native OpenCV VideoCapture with V4L2 mmap backend. This is the same approach used by guvcview — direct V4L2 buffer access without the complexity of GStreamer's push-based pipeline. The result is zero flickering, zero stuttering, and zero frame drops, even on USB 2.0 hardware sharing bandwidth with other devices.

A dedicated background thread handles blocking `cap.read()` calls while a main-thread render timer picks up the latest frame at display refresh rate. GStreamer remains available as a fallback for non-V4L2 sources (gPhoto2, libcamera, PipeWire, IP cameras).

### Nix Support

BigCam now ships `flake.nix`, `default.nix`, and `.envrc` for reproducible builds and development environments via Nix. All runtime dependencies (GTK4, Adwaita, GStreamer, FFmpeg, OpenCV, v4l-utils, gPhoto2, PipeWire, zbar) are properly declared.

### About Dialog

The About dialog now tells BigCam's origin story and credits the original authors — Rafael Ruscher and Barnabé di Kartola.

### Previous (4.0)

### Recording Codec & Container Selector

Full control over recording format, accessible in Settings → Recording:

| Setting | Options | Default |
|---------|---------|--------|
| **Video Codec** | H.264 (HW/SW), H.265 (HW/SW), VP9, MJPEG | H.264 |
| **Audio Codec** | Opus, AAC, MP3, Vorbis | Opus |
| **Container** | MKV, WebM, MP4 | MKV |
| **Bitrate** | 500–50,000 kbps | 8,000 kbps |

Hardware-accelerated encoders (VA-API/VA) are preferred automatically, with software fallbacks (x264enc, x265enc) when unavailable. Settings are persisted and applied in real-time — no restart required.

### Camera Control Profiles

Save and restore per-camera V4L2 control presets:

- **Save Profile**: Captures all current control values (brightness, contrast, exposure, etc.) into a named JSON preset.
- **Load Profile**: Instantly applies a saved preset to the camera.
- **Delete Profile**: Removes a saved preset.
- **Hardware Defaults**: One-click reset of all controls to factory default values.

Profiles are stored per-camera in `~/.config/bigcam/profiles/<camera_name>/`.

### Control Dependencies

Controls that depend on auto modes are now properly disabled/enabled:

- `auto_exposure` → disables `exposure_time_absolute` / `exposure_absolute` when in Aperture Priority
- `white_balance_automatic` → disables `white_balance_temperature` when enabled
- `focus_auto` → disables `focus_absolute` when enabled

### SpinButton on Integer Controls

V4L2 integer controls now display both a slider (for quick adjustment) and a numeric SpinButton (for precise value entry) side by side.

### Anti-Flicker Auto-Set

Automatically sets `power_line_frequency` based on the system timezone when the camera has it disabled (value 0). Americas → 60Hz, rest of world → 50Hz. Does not override manual user settings.

### Barcode Scanner

Real-time 1D barcode detection via [zbar](https://github.com/mchehab/zbar). Supports EAN-13/8, UPC-A/E, Code 128/39/93, Codabar, ITF, ISBN, DataBar, and PDF417. Detected barcodes open a contextual dialog with copy action.

### Effect Pipeline Optimizations

- Beauty/Denoise: 50% downscale above 480p (~4× throughput on 1080p)
- GaussianBlur: `sigmaX` instead of capped kernel size (full 0–200 range)
- Pencil Sketch: `sigma_r` now controls Canny edge blending
- Stylization: edges computed on smoothed frame with scaling block size
- Vignette: integer 10–100 range for finer slider precision

---

## Why BigCam?

Most webcam tools on Linux are either too basic (just open the camera) or too complex (editing suites that happen to have a webcam input). BigCam fills the gap:

- **Zero cost, zero bloat**: Free, open source, no subscriptions, no telemetry. Runs natively on your desktop with GTK4 + Libadwaita.
- **Works with everything**: USB webcams, DSLR/mirrorless cameras (2,500+ models), Raspberry Pi cameras, IP/network cameras, PipeWire virtual cameras, and now **your smartphone** — all from one app.
- **Phone as webcam — for free**: No need to buy Camo, EpocCam, DroidCam, or iVCam. BigCam turns your Android or iPhone into a high-quality webcam using only the phone's built-in browser. No app to install, no account to create, just scan a QR code and start streaming.
- **Professional controls**: Full V4L2 control panel with software fallbacks for zoom, pan/tilt, sharpness, and backlight compensation — so every camera gets the same capabilities regardless of hardware support.
- **Real-time effects**: 17 OpenCV effects (filters, color grading, artistic styles, **background blur with AI segmentation**) applied live to the preview and virtual camera output.
- **Virtual camera output**: Sends the processed feed to any video conferencing app (Zoom, Teams, Google Meet, OBS, Discord) via v4l2loopback.

---

## Supported Cameras

BigCam detects and manages cameras through six independent backends. Multiple cameras can be connected simultaneously, and you can hot-swap between them without restarting the stream.

### USB Webcams (V4L2)

Virtually any USB webcam that exposes a V4L2 device works out of the box — Logitech, Microsoft LifeCam, Razer Kiyo, Trust, Creative, generic UVC cameras, and hundreds of others. Detected automatically via `v4l2-ctl`.

Supported resolutions depend on the camera: from 320×240 up to 4K (3840×2160) for high-end models. BigCam reads the camera's capabilities and presents only valid resolution/format combinations.

### DSLR / Mirrorless (gPhoto2)

Thanks to [libgphoto2](http://www.gphoto.org/proj/libgphoto2/), BigCam supports **2,500+ camera models** from every major brand:

| Brand | Example Models |
|-------|----------------|
| **Canon EOS** | Rebel T3/T5/T6/T7/T8i, SL2/SL3, 80D/90D, R5/R6/R7/R8, M50, RP, 6D Mark II, 5D Mark IV, 1D X |
| **Nikon** | D3200, D3500, D5300, D5600, D7500, D750, D850, Z5, Z6/Z6II, Z7/Z7II, Zfc, Z30 |
| **Sony Alpha** | A6000, A6100, A6400, A6600, A7III/A7IV, A7R IV/V, A7S III, ZV-E10, ZV-1 |
| **Fujifilm** | X-T3, X-T4, X-T5, X-H2S, X-S10, GFX 50S/100S |
| **Panasonic** | GH5/GH5S/GH6, G9, S5/S5II, S1/S1R |
| **Olympus/OM** | E-M1 III, E-M5 III, OM-1, OM-5 |
| **Pentax** | K-1, K-3 III, KF |
| **Others** | GoPro (USB mode), Sigma, Leica, Hasselblad (PTP-compatible models) |

> Full list: [libgphoto2 supported cameras](http://www.gphoto.org/proj/libgphoto2/support.php)

When a gPhoto2 camera is detected, BigCam automatically stops GVFS from claiming it, establishes a persistent PTP/MTP session, and exposes all camera settings (aperture, shutter speed, ISO, image quality, drive mode, etc.) directly in the control panel.

### CSI / ISP (libcamera)

Cameras connected via CSI ribbon cable (Raspberry Pi Camera Module v1/v2/v3, HQ Camera, Arducam) or platform-integrated ISPs (Intel IPU6 on laptops). Detected via `cam --list`.

### PipeWire Virtual Cameras

Any PipeWire video source — OBS Studio virtual camera, screen capture pipelines, other apps streaming via the XDG camera portal. Detected via `pw-cli`.

### Network / IP Cameras

Any camera with an RTSP or HTTP video stream URL — security cameras, action cameras with WiFi streaming, IP webcams. Configure the URL manually in the app.

### Smartphone Camera (Phone as Webcam)

**The highlight of BigCam 3.0, refined in 4.0.** See the dedicated section below.

---

## Use Your Phone as a Webcam

BigCam turns any smartphone (Android or iPhone) into a wireless webcam. Unlike commercial solutions like DroidCam, Camo, or EpocCam, BigCam:

- **Does NOT require installing any app** on your phone
- **Does NOT require creating an account** or signing up for anything
- **Does NOT require a paid subscription** for HD quality
- **Works with any phone** that has a modern web browser (Chrome, Firefox, Safari, Edge)
- **Works over WiFi** — phone and computer just need to be on the same network

### How It Works

1. Click the **phone icon** in BigCam's header bar
2. BigCam starts a secure local HTTPS server on your machine
3. A **QR code** appears on screen — scan it with your phone's camera
4. Your phone opens a web page in its browser
5. Accept the camera permission and tap **Start**
6. Your phone's camera feed streams directly to BigCam in real-time

That's it. No drivers, no USB cables, no third-party apps.

### Technical Details

- **Protocol**: WebSocket Secure (WSS) over HTTPS with automatic HTTP POST fallback for browsers that reject self-signed certificates (Safari/iOS).
- **Transport**: JPEG frames encoded in the phone's browser via HTML5 Canvas + WebRTC `getUserMedia()` API, sent as binary WebSocket messages.
- **Security**: Self-signed TLS certificates generated on-demand, stored in `~/.cache/bigcam/`. Traffic is encrypted. Connection is LAN-only.
- **Latency**: Real-time, limited only by your WiFi network quality.

### Phone Camera Options

| Feature | Options |
|---------|---------|
| **Camera** | Front (selfie) or Back (environment) — switchable mid-stream |
| **Resolution** | Auto, 480p, 720p (default), 1080p |
| **Quality** | Low (60%), Medium (75%, default), High (90%) JPEG compression |
| **Frame rate** | 15, 24, or 30 fps |
| **Orientation** | Automatic — detects portrait/landscape rotation |

### Connection Status

A colored dot next to the phone icon shows the current state:

| Color | Status |
|-------|--------|
| Gray | Server idle |
| Yellow | Waiting for phone connection |
| Green | Phone connected and streaming |
| Red | Phone disconnected |

---

## Features

### Live Preview & Streaming

- **High quality**: stream at your camera's native resolution (up to 4K) to Zoom, Teams, Google Meet, OBS Studio, Discord, or any app that reads a V4L2 device.
- **Low latency pipeline**: GStreamer with `pipewiresrc` for V4L2/PipeWire cameras, FFmpeg with MPEG-TS over localhost UDP for gPhoto2/IP cameras — optimized for minimal delay.
- **Multi-camera**: connect multiple cameras simultaneously and hot-swap between them with a single click. Each gPhoto2 camera keeps its own persistent PTP session.
- **Virtual camera output**: v4l2loopback integration exposes the active feed (with all effects applied) as a regular `/dev/video*` device visible to any video conferencing app.
- **USB hotplug**: cameras are automatically detected when plugged in or removed — no need to restart the app.

### Camera Controls

Full per-camera control panel with sliders, switches, and menus — automatically adapted to each camera's hardware capabilities. Controls are organized by category:

| Category | Controls |
|----------|----------|
| **Image** | Brightness, Contrast, Saturation, Hue, Gamma, Sharpness, Backlight Compensation |
| **Exposure** | Auto/Manual Exposure, Exposure Time, Gain, ISO, Exposure Bias, Auto Priority |
| **Focus** | Auto/Manual Focus, Focus Distance, Digital Zoom, Pan, Tilt |
| **White Balance** | Auto/Manual, Color Temperature, WB Presets |
| **Capture** | Scene Mode, Image Stabilization, LED Mode, 3A Lock |
| **gPhoto2** | Aperture (f-stop), Shutter Speed, ISO, Image Quality, Drive Mode, Focus Mode, Metering Mode, and any other setting the camera exposes |

#### Software Fallback Controls

When the camera's V4L2 driver accepts a control value but PipeWire doesn't forward it to the hardware (a common issue), BigCam applies the effect via software using OpenCV:

| Control | Software Implementation |
|---------|------------------------|
| **Zoom** | Center crop + resize (1x to 4x) |
| **Pan** | Horizontal offset of the crop area |
| **Tilt** | Vertical offset of the crop area |
| **Sharpness** | Unsharp mask (Gaussian blur + weighted blend) |
| **Backlight Compensation** | CLAHE on LAB luminance channel (adaptive histogram equalization) |

Pan and tilt automatically apply a minimum 1.5x zoom when activated to create movement room. When zoom is increased manually, the pan/tilt range increases proportionally.

### Real-Time Effects (OpenCV)

17 effects organized in four categories, plus the AI-powered background blur, all combinable and adjustable in real-time with individual parameter sliders:

| Category | Effects | Parameters | Implementation |
|----------|---------|------------|----------------|
| **Adjustments** | Brightness/Contrast | Brightness (-100 to 100), Contrast (0.5 to 3.0) | `convertScaleAbs(alpha, beta)` |
| | Gamma Correction | Gamma (0.1 to 3.0) | LUT-based `pow(x/255, 1/gamma) * 255` |
| | CLAHE (Adaptive Contrast) | Clip Limit (1.0 to 8.0), Grid Size (2 to 16) | `cv2.createCLAHE` on LAB L-channel |
| | Auto White Balance | — | `cv2.xphoto.createSimpleWB()` |
| **Filters** | Detail Enhance | Sigma S (0 to 200), Sigma R (0.0 to 1.0) | Unsharp mask via `GaussianBlur((0,0), sigmaX=sigma_s/6)` + weighted blend |
| | Beauty / Soft Skin | Smoothing (1 to 25), Detail (0.0 to 1.0) | `bilateralFilter(d, sigma_color, sigma_space)` with 50% downscale above 480p |
| | Sharpen | Kernel Size (1 to 31), Strength (0.0 to 5.0) | Gaussian unsharp mask with configurable kernel |
| | Denoise | Strength (1 to 30), Color Strength (1 to 30) | `bilateralFilter` with 50% downscale above 480p |
| **Artistic** | Grayscale, Sepia, Negative | — | `cvtColor` / fixed 3×3 kernel / `bitwise_not` |
| | Pencil Sketch | Sigma S (0 to 200), Sigma R (0.0 to 1.0) | Dodge-blend on inverted `GaussianBlur((0,0), sigmaX=sigma_s/6)`. Sigma R controls Canny edge line blending intensity |
| | Painting / Stylization | Sigma S (0 to 200), Sigma R (0.0 to 1.0) | `GaussianBlur((0,0), sigmaX=sigma_s/6)` + color quantization + `adaptiveThreshold` on smoothed frame with scaling block size |
| | Edge Detection | Low Threshold (0 to 200), High Threshold (0 to 400) | `cv2.Canny` |
| | Color Map | 21 palettes (Autumn, Bone, Jet, Winter, Rainbow, Ocean, Summer, Spring, Cool, HSV, Pink, Hot, Parula, Magma, Inferno, Plasma, Viridis, Cividis, Twilight, Twilight Shifted, Turbo) | `cv2.applyColorMap` |
| | Vignette | Strength (10 to 100) | Cached radial gradient mask via `np.meshgrid` with cosine falloff, normalized internally from integer range |
| **Advanced** | Background Blur (AI) | Strength (1 to 51) | MediaPipe selfie segmentation (float16 TFLite model) for per-pixel person/background separation, `GaussianBlur` on masked region |

Effects are applied in the GStreamer buffer probe before the frame reaches both the preview and the virtual camera output, so the processed feed is what external apps (Zoom, OBS, etc.) see.

### Tools

- **QR Code Scanner**: Real-time detection using OpenCV WeChatQRCode engine (primary) with OpenCV QRCodeDetector fallback. Detected QR codes are highlighted with a red bounding box and the surrounding area is darkened. Supports URL, WiFi credentials (auto connect), vCard contacts, calendar events, phone numbers, email addresses, SMS, geolocation, TOTP authentication, and plain text. Detected codes open a detailed dialog with contextual actions (open URL, copy text, connect to WiFi, export vCard, etc.).

- **Barcode Scanner**: Integrated alongside the QR scanner, using [zbar](https://github.com/mchehab/zbar) `ImageScanner` on grayscale frames extracted from the GStreamer buffer probe. Falls back automatically when no QR code is found — if a barcode is present, zbar decodes it and emits the result to the same dialog system. Supported symbologies:

  | Symbology | Format |
  |-----------|--------|
  | **EAN-13 / EAN-8** | European Article Number (retail products worldwide) |
  | **UPC-A / UPC-E** | Universal Product Code (North American retail) |
  | **Code 128** | High-density alphanumeric (shipping, logistics) |
  | **Code 39** | Alphanumeric (automotive, military, healthcare) |
  | **Code 93** | Compact alphanumeric (postal services) |
  | **Codabar** | Numeric with special chars (libraries, blood banks) |
  | **ITF** (Interleaved 2 of 5) | Numeric pairs (packaging, distribution) |
  | **ISBN-10 / ISBN-13** | International Standard Book Number |
  | **DataBar** (GS1) | Variable-length data (coupons, produce) |
  | **PDF417** | 2D stacked barcode (boarding passes, ID documents) |

  Detection pipeline: `WeChatQRCode.detectAndDecode()` → `QRCodeDetector.detectAndDecode()` → `zbar.ImageScanner.scan()`. The first successful result is emitted.

- **Smile Capture**: Automatic photo trigger on smile detection using Haar cascade classifiers. Uses a 3-consecutive-frame validation algorithm to eliminate false positives — the camera only fires when a genuine smile is consistently detected across multiple frames. Configurable detection sensitivity and cooldown between captures.

### Photo & Video

- **Photo capture**: single-click or timer-delayed capture. For gPhoto2 cameras, the photo is captured at the camera's native resolution (not the preview resolution) and automatically downloaded.
- **Video recording**: records from the GStreamer pipeline with configurable codecs (H.264/H.265/VP9/MJPEG), audio codecs (Opus/AAC/MP3/Vorbis), and containers (MKV/WebM/MP4). Hardware-accelerated encoding when available. Recording continues while the preview remains active.
- **Photo gallery**: browse captured images with lazy-loaded thumbnails. Delete photos directly from the gallery with confirmation dialog.
- **Video gallery**: browse and play recorded videos with the system's default player.
- **XDG-compliant paths**: photos and videos are saved to the system's configured Pictures and Videos directories (e.g., `~/Imagens/BigCam/` on Portuguese systems, `~/Pictures/BigCam/` on English systems) using `xdg-user-dir`.

### Interface

- **GTK4 + Libadwaita**: modern, native GNOME/KDE look-and-feel with full dark/light/system theme support. Uses Adw.NavigationView, Adw.PreferencesGroup, Adw.SwitchRow, Adw.ComboRow, Adw.ActionRow for a consistent Adwaita experience.
- **Paned layout**: resizable live preview + sidebar with tabbed pages (Controls, Effects, Tools, Settings, Photos, Videos).
- **Welcome screen**: guided first-run dialog introducing the app's key features.
- **Bottom bar**: quick-access toggle buttons for QR scanner, smile capture, virtual camera, and mirror — synced bidirectionally with Settings.
- **Always on Top**: window pin button (Wayland-compatible via KWin D-Bus scripting).
- **Fullscreen mode**: toggle fullscreen via header button or keyboard shortcut.
- **Capture timer**: configurable delay (3s, 5s, 10s) with countdown overlay, synced between header and Settings.
- **Help-on-hover**: optional tooltips system with Settings toggle.
- **Last media thumbnail**: circular button showing the most recent photo/video thumbnail, mode-aware (photo or video with ffmpeg preview).
- **Window notifications**: Adw.Banner at window top that pushes all content down for important messages.
- **Flash effect**: screen flash animation on photo capture.
- **Recording timer**: overlay showing elapsed recording time.
- **Responsive**: adapts to window size with saved/restored dimensions.
- **Grid overlay**: optional rule-of-thirds grid over the preview for composition.
- **FPS counter**: optional overlay showing current stream frame rate.
- **Keyboard navigation & accessibility**: all interactive elements have accessible labels for screen readers.

### Settings

| Setting | Description |
|---------|-------------|
| **Theme** | System / Light / Dark |
| **Photo directory** | System Pictures folder (XDG-compliant) |
| **Video directory** | System Videos folder (XDG-compliant) |
| **Mirror preview** | Flip the preview horizontally |
| **FPS overlay** | Show/hide framerate counter |
| **Grid overlay** | Show/hide rule-of-thirds grid |
| **Help-on-hover** | Show/hide contextual tooltips on hover |
| **Resolution** | Select from camera's available resolutions (filtered by tiers: 240p, 360p, 480p, 720p, 1080p, 1440p, 4K) |
| **FPS limit** | Auto, 15, 24, 30, 60 fps |
| **Capture timer** | Instant, 3s, 5s, 10s delay (synced with header button) |
| **Recording video codec** | H.264 (default), H.265, VP9, MJPEG — with hardware acceleration |
| **Recording audio codec** | Opus (default), AAC, MP3, Vorbis |
| **Recording container** | MKV (default), WebM, MP4 |
| **Recording bitrate** | 500–50,000 kbps (default: 8,000) |
| **QR Scanner** | Enable/disable real-time QR code detection (synced with bottom bar button) |
| **Smile Capture** | Enable/disable automatic smile-triggered photos (synced with bottom bar button) |
| **Virtual Camera** | Start/stop v4l2loopback output (synced with bottom bar button) |
| **Hotplug detection** | Automatically detect USB camera connect/disconnect |

### Internationalization

Fully translated into **29 languages** using GNU gettext:

Bulgarian, Chinese, Croatian, Czech, Danish, Dutch, English, Estonian, Finnish, French, German, Greek, Hebrew, Hungarian, Icelandic, Italian, Japanese, Korean, Norwegian, Polish, Portuguese, Brazilian Portuguese, Romanian, Russian, Slovak, Swedish, Turkish, Ukrainian.

All user-visible strings (labels, tooltips, dialogs, menus, accessibility text, effect names, camera control labels) are wrapped in translatable `_()` calls. The translation template (`.pot`) is automatically generated from source code with `xgettext`.

---

## Installation

### Arch Linux / BigLinux (recommended)

```bash
# Clone the repository
git clone https://github.com/biglinux/bigcam.git
cd bigcam

# Run the automated installer
chmod +x script/install-archlinux.sh
./script/install-archlinux.sh
```

The installer handles all dependencies, kernel module configuration (v4l2loopback), sudoers rules, and locale compilation.

### Manual / Other Distros

Install the dependencies:

**Core (required for basic operation):**
```
python  python-gobject  gtk4  libadwaita
gstreamer  gst-plugins-base  gst-plugins-good  gst-plugins-bad-libs
gst-plugins-ugly  gst-plugin-gtk4
pipewire  v4l-utils  ffmpeg  polkit
```

**Image processing & scanning:**
```
python-opencv         # Effects, QR scanner, smile capture, software controls
python-numpy          # Frame array processing (also a dependency of python-opencv)
zbar                  # 1D barcode scanning (EAN, UPC, Code 128, etc.)
```

**Camera backends:**
```
gphoto2               # DSLR / mirrorless cameras (2,500+ models)
libcamera             # CSI / ISP cameras (Raspberry Pi, Intel IPU6)
v4l2loopback-dkms     # Virtual camera output (/dev/video*)
x264                  # H.264 codec for video recording
```

**Phone camera & connectivity:**
```
python-aiohttp        # WebSocket server for smartphone streaming
python-qrcode         # QR code generation for phone camera dialog
```

**Optional (not packaged in all distros):**
```
mediapipe             # Background blur AI segmentation (selfie segmenter)
                      # Install via pip: pip install mediapipe
                      # Not available in pacman/apt repos
```

Then run:
```bash
cd usr/share/biglinux/bigcam
python3 main.py
```

### PKGBUILD

A ready-to-use `PKGBUILD` is available in [`pkgbuild/`](pkgbuild/PKGBUILD) for building an Arch Linux package.

---

## Architecture

BigCam follows a strict separation between business logic (`core/`) and user interface (`ui/`). Core modules never import GTK/Adwaita — they communicate through GObject signals.

```
bigcam/
├── usr/share/biglinux/bigcam/       # Application root
│   ├── main.py                      # Entry point (Adw.Application, single-instance)
│   ├── constants.py                 # App ID, version, enums (BackendType, ControlCategory, etc.)
│   ├── style.css                    # Custom CSS overrides
│   │
│   ├── core/                        # Business logic (no UI imports)
│   │   ├── camera_manager.py        # Backend registry, detection, hotplug (800ms debounce)
│   │   ├── camera_backend.py        # CameraInfo / VideoFormat / CameraControl data classes
│   │   ├── camera_profiles.py       # Save/load per-camera control presets (JSON in ~/.config/bigcam/profiles/)
│   │   ├── stream_engine.py         # GStreamer pipeline lifecycle, OpenCV probe, software controls, vcam appsrc
│   │   ├── effects.py               # EffectPipeline — 17 OpenCV effects + MediaPipe background blur
│   │   ├── audio_monitor.py         # Audio device detection and playback for USB cameras via GStreamer level element
│   │   ├── photo_capture.py         # Photo capture orchestration (preview snapshot + gPhoto2 download)
│   │   ├── video_recorder.py        # Multi-codec video recording (H.264/H.265/VP9/MJPEG, HW accel, MKV/WebM/MP4)
│   │   ├── virtual_camera.py        # v4l2loopback management (modprobe, device enumeration, start/stop)
│   │   ├── phone_camera.py          # HTTPS + WebSocket server for smartphone streaming (self-signed TLS)
│   │   └── backends/                # One module per camera type
│   │       ├── v4l2_backend.py      # V4L2: v4l2-ctl enumeration, pipewiresrc/v4l2src GStreamer elements
│   │       ├── gphoto2_backend.py   # gPhoto2: PTP/MTP session, settings, FFmpeg MPEG-TS → appsink pipeline
│   │       ├── libcamera_backend.py # libcamera: CSI/ISP detection via cam --list
│   │       ├── pipewire_backend.py  # PipeWire: virtual camera sources via pw-cli
│   │       └── ip_backend.py        # IP: RTSP (rtspsrc) / HTTP (souphttpsrc) stream probing
│   │
│   ├── ui/                          # GTK4 / Adwaita interface
│   │   ├── window.py                # Main window (paned layout, menu, keyboard shortcuts, bottom bar)
│   │   ├── welcome_dialog.py        # First-run welcome dialog with feature overview
│   │   ├── immersion.py             # Fullscreen / immersive mode controller
│   │   ├── preview_area.py          # Live camera preview (Gtk4PaintableSink + overlays)
│   │   ├── camera_selector.py       # Camera list dropdown with hotplug updates
│   │   ├── camera_controls_page.py  # Dynamic V4L2/gPhoto2 control panel with software fallbacks
│   │   ├── effects_page.py          # Effects toggle grid with parameter sliders
│   │   ├── tools_page.py            # QR/barcode scanner, smile capture toggles
│   │   ├── settings_page.py         # App preferences + QR/barcode detection engine (WeChatQRCode + zbar)
│   │   ├── photo_gallery.py         # Photo browser with lazy thumbnails and delete
│   │   ├── video_gallery.py         # Video browser with system player integration
│   │   ├── virtual_camera_page.py   # Virtual camera start/stop controls
│   │   ├── phone_camera_dialog.py   # Phone camera connection dialog with QR code
│   │   ├── ip_camera_dialog.py      # IP camera URL configuration dialog
│   │   ├── qr_dialog.py             # QR/barcode result display with contextual actions (URL, WiFi, vCard, Barcode copy)
│   │   ├── about_dialog.py          # Adw.AboutDialog with app info
│   │   └── notification.py          # Adw.Banner dismissable notifications
│   │
│   ├── utils/                       # Shared utilities
│   │   ├── i18n.py                  # gettext internationalization (29 languages)
│   │   ├── settings_manager.py      # JSON config persistence (~/.config/bigcam/)
│   │   ├── async_worker.py          # Background thread helper (GLib.idle_add pattern)
│   │   ├── dependency_checker.py    # Runtime dependency checks with user notification
│   │   └── xdg.py                   # XDG directory resolution via xdg-user-dir
│   │
│   ├── icons/                       # App icons (SVG, hicolor theme structure)
│   └── img/                         # Static images
│
├── locale/                          # Source translation files (.po/.pot)
├── pkgbuild/                        # Arch Linux packaging (PKGBUILD + install hooks)
│
├── etc/                             # System config templates
│   ├── modprobe.d/v4l2loopback.conf # v4l2loopback kernel module options
│   ├── sudoers.d/                   # Privilege escalation rules (mode 440)
│
└── COPYING                          # GPLv3 license
```

### Data Flow

```
Camera Sources
    │
    ├─ USB Webcam ──── V4L2 ──── pipewiresrc ────┐
    ├─ DSLR ────────── gPhoto2 → FFmpeg ─── UDP ─┤
    ├─ CSI Camera ──── libcamera ────────────────┤
    ├─ PipeWire ────── pw-cli ───────────────────┤
    ├─ IP Camera ───── RTSP/HTTP ──── UDP ───────┤
    └─ Smartphone ──── WebSocket (HTTPS) ────────┘
                                                  │
                              GStreamer Pipeline (tee)
                                      │
                    ┌─────────────────┼──────────────────┐
                    │                 │                   │
              Buffer Probe     gtk4paintablesink    v4l2loopback
              (OpenCV)          (GTK4 Preview)     (Virtual Cam)
                    │                                    │
            ┌───────┼───────┐                     Available to:
            │       │       │                     Zoom, Teams,
         Effects  Zoom    QR/Barcode/            Meet, OBS, etc.
         Pipeline Pan/Tilt Smile Detection
                  Sharpness    │
                  Backlight    ├─ WeChatQRCode (2D)
                  BG Blur (AI) ├─ QRCodeDetector (2D fallback)
                    │          └─ zbar ImageScanner (1D barcodes)
            ┌───────┼───────┐
            │               │
      Video Recorder   Photo Capture
      (x264 → MKV)    (JPEG → XDG dir)
```

### Key Design Decisions

- **PipeWire-first**: V4L2 cameras are accessed through `pipewiresrc` (PipeWire node targeting) rather than `v4l2src` for better integration with the modern Linux audio/video stack. The v4l2src fallback is only used when PipeWire is unavailable.
- **Single buffer probe, multiple passes**: All image processing (effects, zoom, pan/tilt, sharpness, backlight compensation, QR detection, barcode scanning, smile detection) happens in a single GStreamer buffer probe. The probe converts the buffer to a NumPy array, applies processing, converts back, and replaces the buffer — all in one pass per frame.
- **Cascading detection pipeline**: QR/barcode scanning uses a three-stage fallback: WeChatQRCode (fastest, highest accuracy for 2D QR) → QRCodeDetector (OpenCV built-in fallback) → zbar ImageScanner (1D barcodes only, runs on the grayscale frame already computed for QR detection).
- **Adaptive downscaling for bilateralFilter**: Beauty and Denoise effects detect frame dimensions and downscale to 50% before applying `bilateralFilter` (O(d² × pixels)) when the frame exceeds 480p. The result is upscaled back. This yields ~4× throughput on 1080p with negligible perceptual quality loss.
- **Software control fallbacks**: When V4L2 controls are accepted by the kernel driver but not applied by PipeWire, software equivalents are transparently applied. The user sees the same slider; the effect just works.
- **No heavy dependencies**: The phone camera feature uses `aiohttp` (async WebSocket server) + browser's native WebRTC — no Electron, no native mobile app, no proprietary SDKs.
- **GStreamer element mapping**: The pipeline uses elements from multiple GStreamer plugin packages: `gst-plugins-base` (videoconvert, decodebin, audioconvert, audioresample, queue, tee, appsrc/appsink), `gst-plugins-good` (v4l2src/v4l2sink, rtspsrc, souphttpsrc, level), `gst-plugins-bad-libs` (tsdemux), `gst-plugins-ugly` (x264enc), and `gst-plugin-gtk4` (gtk4paintablesink).

---

## Configuration

BigCam stores its configuration following the XDG Base Directory Specification:

| Path | Content |
|------|---------|
| `~/.config/bigcam/settings.json` | User preferences (theme, mirror, FPS, resolution, etc.) |
| `~/Pictures/BigCam/` | Captured photos (uses system XDG directory) |
| `~/Videos/BigCam/` | Recorded videos (uses system XDG directory) |
| `~/.cache/bigcam/` | Temporary files, self-signed TLS certificates, MediaPipe selfie segmenter model |
| `~/.config/bigcam/profiles/<camera>/` | Per-camera control presets (JSON profiles) |

Photo and video directories automatically adapt to the system language. For example, a Portuguese (Brazil) system uses `~/Imagens/BigCam/` and `~/Vídeos/BigCam/`.

---

## Troubleshooting

### Camera not detected (gPhoto2)

GVFS may be claiming the camera. BigCam handles this automatically by stopping the GVFS gphoto2 service, but if you still have issues:

```bash
systemctl --user stop gvfs-gphoto2-volume-monitor.service
systemctl --user mask gvfs-gphoto2-volume-monitor.service
pkill -9 gvfsd-gphoto2
```

### PTP Timeout (DSLR)

The camera may be in the wrong mode. Ensure it is:
- Turned **on** and connected via USB
- Set to **M** (Manual) or **P** (Program) mode
- Not in **Video** mode (some cameras lock PTP in video mode)
- Not in **sleep/auto-off** mode — set the auto-off timer to the maximum

### Virtual camera not appearing

The v4l2loopback kernel module must be loaded:

```bash
sudo modprobe v4l2loopback devices=4 exclusive_caps=1
```

BigCam includes a configuration file at `etc/modprobe.d/v4l2loopback.conf` for automatic loading on boot.

### Camera in use by another application

BigCam detects when a camera is in use (via `fuser`) and shows which process is holding it. Close the conflicting application or select a different camera.

### Phone camera: certificate warning

When scanning the QR code, your phone's browser will show a security warning because BigCam uses a self-signed certificate. This is normal and expected for LAN-only connections. Accept the warning to proceed.

### Permissions

Ensure your user is in the `video` group:

```bash
sudo usermod -aG video $USER
```

Log out and back in for the change to take effect.

---

## Contributing

BigCam is part of the [BigLinux](https://www.biglinux.com.br/) ecosystem.

**Original creators:**
- **Rafael Ruscher** ([@ruscher](https://github.com/ruscher))
- **Barnabé di Kartola**

Contributions are welcome! Please open an issue or pull request on [GitHub](https://github.com/biglinux/bigcam).

### Translating

Translation files are in `locale/` using GNU gettext PO format. The translation template (`bigcam.pot`) contains all 304 translatable strings.

To add or update a translation:

1. Copy `locale/bigcam.pot` to `locale/<lang>.po` (e.g., `locale/de.po` for German)
2. Translate the `msgstr` entries using a PO editor (Poedit, Lokalize, or any text editor)
3. Compile with `msgfmt -o usr/share/locale/<lang>/LC_MESSAGES/bigcam.mo locale/<lang>.po`
4. Submit a pull request

Currently supported: bg, cs, da, de, el, en, es, et, fi, fr, he, hr, hu, is, it, ja, ko, nl, no, pl, pt, pt_BR, ro, ru, sk, sv, tr, uk, zh.

### Development

```bash
# Run from source (no installation needed)
cd usr/share/biglinux/bigcam
python3 main.py

# Regenerate translation template after adding new strings
find usr/share/biglinux/bigcam -name '*.py' | sort | \
  xargs xgettext --language=Python --keyword=_ --output=locale/bigcam.pot \
  --from-code=UTF-8 --package-name=bigcam

# Update all translations with new strings
for po in locale/*.po; do msgmerge --update "$po" locale/bigcam.pot; done
```

---

## License

This project is licensed under the **GNU General Public License v3.0** — see [COPYING](COPYING) for the full text.

```
BigCam — Universal webcam control center for Linux
Copyright (C) 2026 BigLinux Team

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.
```

---

<p align="center">Made with care for the Linux desktop community</p>
