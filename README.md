<p align="center">
  <img src="usr/share/biglinux/bigcam/icons/bigcam.svg" alt="BigCam" width="128" height="128">
</p>

<h1 align="center">BigCam</h1>

<p align="center">
  <b>Universal webcam control center for Linux — turn any camera into a professional streaming device</b>
</p>

<p align="center">
  <a href="#-features">Features</a> •
  <a href="#-supported-cameras">Supported Cameras</a> •
  <a href="#-installation">Installation</a> •
  <a href="#-architecture">Architecture</a> •
  <a href="#-the-story">The Story</a> •
  <a href="#-contributing">Contributing</a> •
  <a href="#-license">License</a>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/License-GPLv3-blue.svg" alt="License: GPL v3">
  <img src="https://img.shields.io/badge/Platform-Linux-green.svg" alt="Platform: Linux">
  <img src="https://img.shields.io/badge/GTK-4.0-blue.svg" alt="GTK 4.0">
  <img src="https://img.shields.io/badge/Libadwaita-1.0-purple.svg" alt="Libadwaita">
  <img src="https://img.shields.io/badge/Python-3.x-yellow.svg" alt="Python 3">
</p>

---

## The Story

**BigCam** was born from a real need. It started as a humble shell script written by **Rafael Ruscher** and **Barnabé di Kartola** so that Ruscher could use his Canon Rebel T3 as a webcam during his live streams about [BigLinux](https://www.biglinux.com.br/). That small hack proved so useful that it evolved — first into a more capable script, then into a full-blown GTK4/Adwaita application integrated into the BigLinux ecosystem.

Today BigCam supports USB webcams, DSLR/mirrorless cameras (2,500+ models via libgphoto2), CSI/ISP cameras (libcamera), PipeWire virtual cameras, and network IP cameras — all from a single, polished interface.

We are grateful to Rafael and Barnabé for starting this journey.

---

## Features

### Live Preview & Streaming

- **Professional quality**: stream at your camera's native resolution (up to 4K) to Zoom, Teams, Google Meet, OBS Studio, Discord, or any app that reads a V4L2 device.
- **Low latency pipeline**: GStreamer + FFmpeg with MPEG-TS over localhost UDP — optimised for minimal delay.
- **Multi-camera**: connect multiple cameras simultaneously and hot-swap between them without restarting the stream. Each gPhoto2 camera keeps its own persistent session.
- **Virtual camera output**: v4l2loopback integration exposes the active feed as a regular `/dev/video*` device.

### Camera Backends

| Backend | Devices | Detection |
|---------|---------|-----------|
| **V4L2** | USB/UVC webcams (95%+ coverage) | Automatic via `v4l2-ctl` |
| **gPhoto2** | DSLR/mirrorless (2,500+ models) | Automatic via `gphoto2 --auto-detect` |
| **libcamera** | Raspberry Pi CSI, Intel IPU6 | Automatic via `cam --list` |
| **PipeWire** | OBS virtual cam, XDG camera portal | Automatic via `pw-cli` |
| **IP** | RTSP/HTTP network cameras | Manual configuration |

### Camera Controls

Full per-camera control panel with sliders, switches, and menus — automatically adapted to each camera's capabilities:

- **Image**: brightness, contrast, saturation, hue, gamma, sharpness, backlight compensation
- **Exposure**: auto/manual exposure, exposure time, gain, ISO
- **Focus**: auto/manual focus, focus distance
- **White Balance**: auto/manual, temperature
- **gPhoto2 extended**: all settings exposed by libgphoto2 (aperture, shutter speed, ISO, image quality, drive mode, etc.)

### Real-Time Effects (OpenCV)

17 effects organised in four categories, all combinable and adjustable in real-time:

| Category | Effects |
|----------|---------|
| **Adjustments** | Brightness/Contrast, Gamma Correction, CLAHE Adaptive Contrast, Auto White Balance |
| **Filters** | Detail Enhance, Beauty/Soft Skin, Sharpen, Denoise |
| **Artistic** | Grayscale, Sepia, Negative, Pencil Sketch, Painting/Stylization, Cartoon, Edge Detection, Color Map (21 palettes), Vignette |
| **Advanced** | Background Blur (face-detection-based) |

### Tools

- **QR Code Scanner**: real-time detection using OpenCV WeChatQRCode with detailed result dialog (URL, WiFi, vCard, text).
- **Smile Capture**: automatic photo trigger on smile detection via Haar cascades. Configurable sensitivity and cooldown.

### Photo & Video

- **Remote photo capture**: click to capture, preview the result instantly, automatic download from DSLR.
- **Video recording**: records directly from the GStreamer pipeline (x264 ultrafast, MKV container) without interrupting the preview.
- **Photo gallery**: browse and manage captured images with thumbnails. Delete directly from the gallery.
- **Video gallery**: browse and play recorded videos.

### Interface

- **GTK4 + Libadwaita**: modern, native GNOME look-and-feel with full dark/light theme support.
- **Paned layout**: resizable preview + sidebar with collapsible pages (Controls, Effects, Tools, Settings, Gallery).
- **Responsive**: adapts to window size.
- **Keyboard navigation & accessibility**: labels on all interactive elements.

### Settings

- Theme preference (System / Light / Dark)
- Photo and video output directories (XDG-compliant)
- Mirror preview toggle
- FPS counter overlay
- QR Scanner and Smile Capture enable/disable
- Virtual camera management
- USB hotplug auto-detection

### Internationalization

Translated into **29 languages**: Bulgarian, Chinese, Croatian, Czech, Danish, Dutch, English, Estonian, Finnish, French, German, Greek, Hebrew, Hungarian, Icelandic, Italian, Japanese, Korean, Norwegian, Polish, Portuguese, Brazilian Portuguese, Romanian, Russian, Slovak, Swedish, Turkish, Ukrainian.

---

## Supported Cameras

### USB Webcams (V4L2)

Virtually any USB webcam that exposes a V4L2 device will work out of the box.

### DSLR / Mirrorless (gPhoto2)

Thanks to [libgphoto2](http://www.gphoto.org/proj/libgphoto2/), BigCam supports 2,500+ camera models:

- **Canon EOS**: Rebel T3/T5/T6/T7, SL2/SL3, 80D/90D, R5/R6, M50, 550D, 1100D, etc.
- **Nikon**: D3200, D3500, D5300, D5600, D750, Z6, Z7, etc.
- **Sony Alpha**: A6000, A6400, A7III, A7R, ZV-E10 (PC Remote mode required).
- **Fujifilm**: X-T3, X-T4, X-H2S.
- **Panasonic / Olympus**: various PTP-compatible models.

> Full list: [libgphoto2 supported cameras](http://www.gphoto.org/proj/libgphoto2/support.php)

### CSI / ISP (libcamera)

Raspberry Pi cameras, Intel IPU6, and other platform cameras detected via libcamera.

### Network (IP)

Any RTSP or HTTP camera stream — configure the URL manually.

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

The installer handles all dependencies, kernel module configuration (v4l2loopback), and sudoers rules.

### Manual / Other Distros

Install the dependencies:

**Required:**
```
python  python-gobject  gtk4  libadwaita  gstreamer  gst-plugins-base
gst-plugins-good  gst-plugin-gtk4  ffmpeg  v4l-utils
```

**Optional (for specific features):**
```
gphoto2             # DSLR / mirrorless cameras
libcamera           # CSI / ISP cameras
pipewire            # PipeWire virtual cameras
v4l2loopback-dkms   # Virtual camera output
x264                # Video recording (H.264)
python-opencv       # Effects, QR scanner, smile capture
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

```
bigcam/
├── usr/share/biglinux/bigcam/       # Application root
│   ├── main.py                      # Entry point (Adw.Application)
│   ├── constants.py                 # App ID, version, enums
│   ├── style.css                    # Custom CSS overrides
│   │
│   ├── core/                        # Business logic (no UI imports)
│   │   ├── camera_manager.py        # Backend registry, detection
│   │   ├── camera_profiles.py       # CameraInfo / VideoFormat models
│   │   ├── stream_engine.py         # GStreamer pipeline management
│   │   ├── photo_capture.py         # Photo capture orchestration
│   │   ├── video_recorder.py        # H.264 video recording
│   │   ├── virtual_camera.py        # v4l2loopback management
│   │   └── backends/                # One module per camera type
│   │       ├── v4l2_backend.py
│   │       ├── gphoto2_backend.py
│   │       ├── libcamera_backend.py
│   │       ├── pipewire_backend.py
│   │       └── ip_backend.py
│   │
│   ├── ui/                          # GTK4 / Adwaita interface
│   │   ├── window.py                # Main window (paned layout)
│   │   ├── preview_area.py          # Live camera preview + effects
│   │   ├── camera_selector.py       # Camera list and switcher
│   │   ├── camera_controls_page.py  # Dynamic control panel
│   │   ├── effects_page.py          # Effects toggle and parameters
│   │   ├── tools_page.py            # QR scanner, smile capture
│   │   ├── settings_page.py         # App preferences
│   │   ├── photo_gallery.py         # Photo browser with delete
│   │   ├── video_gallery.py         # Video browser
│   │   ├── virtual_camera_page.py   # Virtual camera controls
│   │   ├── ip_camera_dialog.py      # IP camera configuration
│   │   ├── qr_dialog.py             # QR code result display
│   │   ├── about_dialog.py          # Adw.AboutDialog
│   │   └── notification.py          # Adw.Banner notifications
│   │
│   ├── utils/                       # Shared utilities
│   │   ├── i18n.py                  # gettext internationalisation
│   │   ├── settings_manager.py      # JSON config persistence
│   │   ├── async_worker.py          # Background thread helper
│   │   ├── dependency_checker.py    # Runtime dependency checks
│   │   └── xdg.py                   # XDG directory helpers
│   │
│   ├── script/                      # Shell scripts
│   │   ├── run_webcam_gphoto2.sh    # gPhoto2 + FFmpeg pipeline
│   │   └── install-archlinux.sh     # System setup
│   │
│   ├── icons/                       # App icons (SVG)
│   └── locale/                      # Translations (29 languages)
│
├── pkgbuild/                        # Arch Linux packaging
│   ├── PKGBUILD
│   └── pkgbuild.install
│
├── etc/                             # System config templates
│   ├── modprobe.d/v4l2loopback.conf
│   └── sudoers.d/
│
└── COPYING                          # GPLv3 license
```

### Data Flow

```
Camera (USB/Network)
    │
    ├─ V4L2 ──────────────┐
    ├─ gPhoto2 → FFmpeg ──┤ UDP (localhost)
    ├─ libcamera ─────────┤
    ├─ PipeWire ──────────┤
    └─ IP (RTSP/HTTP) ────┘
                           │
                    GStreamer Pipeline
                           │
               ┌───────────┼───────────┐
               │           │           │
          GTK4 Preview   Effects    v4l2loopback
          (Paintable)   (OpenCV)   (Virtual Cam)
               │           │           │
               └───────────┼───────────┘
                           │
                    ┌──────┴──────┐
                    │             │
              Video Recorder  Photo Capture
              (x264 → MKV)   (JPEG → XDG)
```

---

## Configuration

BigCam stores its configuration following the XDG Base Directory Specification:

| Path | Content |
|------|---------|
| `~/.config/bigcam/settings.json` | User preferences |
| `~/Pictures/BigCam/` | Captured photos (default) |
| `~/Videos/BigCam/` | Recorded videos (default) |
| `~/.cache/bigcam/` | Temporary files |

---

## Troubleshooting

### Camera not detected (gPhoto2)

GVFS may be claiming the camera. BigCam handles this automatically, but if you still have issues:

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
- Not in **sleep/auto-off** mode

### Virtual camera not appearing

The v4l2loopback kernel module must be loaded:

```bash
sudo modprobe v4l2loopback devices=4 exclusive_caps=1
```

### Permissions

If `fuser` or `pkill` fails with "Operation not permitted", ensure your user is in the `video` group:

```bash
sudo usermod -aG video $USER
```

---

## Contributing

BigCam is part of the [BigLinux](https://www.biglinux.com.br/) ecosystem.

**Original creators:**
- **Rafael Ruscher** ([@ruscher](https://github.com/ruscher))
- **Barnabé di Kartola**

Contributions are welcome! Please open an issue or pull request on [GitHub](https://github.com/biglinux/bigcam).

### Translating

Translation files are in `locale/` using gettext PO format. To add or update a translation:

1. Copy an existing `.po` file and rename it to your language code
2. Translate the strings
3. Submit a pull request

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
