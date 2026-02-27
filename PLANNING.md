# Big DigiCam — Plano de Reconstrução Completa

> **Objetivo:** Transformar o Big DigiCam de um app exclusivo para câmeras gphoto2 em um **centro de controle universal de webcams** para Linux, com suporte de classe mundial a câmeras USB, DSLR/Mirrorless, IP, PipeWire, libcamera e v4l2loopback virtuais — pronto para competição internacional de usabilidade, design e acessibilidade.

---

## Índice

1. [Visão Geral da Arquitetura](#1-visão-geral-da-arquitetura)
2. [Backends de Câmera Suportados](#2-backends-de-câmera-suportados)
3. [Estrutura de Diretórios](#3-estrutura-de-diretórios)
4. [Fases de Implementação](#4-fases-de-implementação)
5. [Detalhamento por Módulo](#5-detalhamento-por-módulo)
6. [UI/UX — Layout e Fluxo](#6-uiux--layout-e-fluxo)
7. [Acessibilidade (Orca + Teclado)](#7-acessibilidade-orca--teclado)
8. [Internacionalização (i18n)](#8-internacionalização-i18n)
9. [Sistema de Controles da Câmera](#9-sistema-de-controles-da-câmera)
10. [Pipeline de Vídeo](#10-pipeline-de-vídeo)
11. [Empacotamento (PKGBUILD)](#11-empacotamento-pkgbuild)
12. [Checklist de Qualidade para Competição](#12-checklist-de-qualidade-para-competição)

---

## 1. Visão Geral da Arquitetura

### Princípios

- **Separação completa:** UI ↔ Lógica ↔ Dados
- **Backend agnóstico:** Camada de abstração `CameraBackend` com implementações plugáveis
- **Thread safety:** Toda operação de I/O, detecção e captura roda fora da main thread; GTK é atualizado apenas via `GLib.idle_add()`
- **Persistência XDG:** Configurações em `~/.config/big-digicam/`, cache em `~/.cache/big-digicam/`, fotos em `~/Pictures/Big DigiCam/`
- **Zero hardcode:** Nenhum caminho, porta ou nome de câmera fixo no código

### Diagrama de Camadas

```
┌─────────────────────────────────────────────────────────┐
│                     GTK4 / Adwaita UI                   │
│  ┌──────────┐ ┌──────────────┐ ┌──────────────────────┐ │
│  │ HeaderBar│ │  Preview     │ │  Controls Sidebar    │ │
│  │ + Status │ │  (GStreamer)  │ │  (AdwPreferencesPage)│ │
│  └──────────┘ └──────────────┘ └──────────────────────┘ │
├─────────────────────────────────────────────────────────┤
│                   Application Controller                 │
│  ┌─────────────┐  ┌─────────────┐  ┌────────────────┐  │
│  │ CameraManager│  │ StreamEngine│  │ SettingsManager│  │
│  └─────────────┘  └─────────────┘  └────────────────┘  │
├─────────────────────────────────────────────────────────┤
│                   Backend Abstraction Layer               │
│  ┌────────┐ ┌────────┐ ┌──────────┐ ┌────────┐ ┌─────┐ │
│  │ V4L2   │ │ GPhoto2│ │ PipeWire │ │libcam  │ │ IP  │ │
│  └────────┘ └────────┘ └──────────┘ └────────┘ └─────┘ │
├─────────────────────────────────────────────────────────┤
│              Linux Kernel / System Services               │
│  v4l2  ·  uvcvideo  ·  v4l2loopback  ·  PipeWire       │
└─────────────────────────────────────────────────────────┘
```

---

## 2. Backends de Câmera Suportados

### 2.1 V4L2 — USB Webcams (Primário)

**Cobre:** 95%+ das webcams USB (UVC) — Logitech, Razer, Microsoft, Elgato, AVerMedia, etc.

- **Detecção:** `v4l2-ctl --list-devices` + `udevadm monitor` (hotplug)
- **Controles:** `v4l2-ctl -d /dev/videoX --list-ctrls-menus` → V4L2 ioctl
- **Formatos:** `v4l2-ctl -d /dev/videoX --list-formats-ext`
- **Preview:** GStreamer `v4l2src` direto
- **Dependências:** `v4l-utils` (já instalado)

**Controles típicos V4L2:**
| Controle | Tipo | Faixa |
|---|---|---|
| brightness | int | 0–255 |
| contrast | int | 0–255 |
| saturation | int | 0–255 |
| hue | int | -180–180 |
| white_balance_automatic | bool | 0/1 |
| white_balance_temperature | int | 2800–6500 |
| gain | int | 0–255 |
| exposure_auto | menu | manual/auto/shutter/aperture |
| exposure_absolute | int | 3–2047 |
| focus_auto | bool | 0/1 |
| focus_absolute | int | 0–255 |
| zoom_absolute | int | 100–400 |
| pan_absolute | int | -36000–36000 |
| tilt_absolute | int | -36000–36000 |
| backlight_compensation | int | 0–2 |
| power_line_frequency | menu | disabled/50Hz/60Hz/auto |
| sharpness | int | 0–255 |

### 2.2 GPhoto2 — DSLR/Mirrorless (Mantido)

**Cobre:** 2.500+ câmeras Canon, Nikon, Sony, Fuji, Olympus, Pentax, etc.

- **Detecção:** `gphoto2 --auto-detect`
- **Controles:** `gphoto2 --list-all-config` → config get/set
- **Capture:** `gphoto2 --capture-image-and-download` (foto), `--stdout --capture-movie` (vídeo)
- **Pipeline:** gphoto2 → FFmpeg → v4l2loopback + UDP (existente)
- **Dependências:** `gphoto2`, `libgphoto2`, `v4l2loopback-dkms`, `ffmpeg`

### 2.3 libcamera — CSI/ISP Cameras

**Cobre:** Raspberry Pi cameras, Intel IPU6, MIPI CSI, câmeras modernas com ISP.

- **Detecção:** `cam --list` (libcamera-tools) ou API Python
- **Preview:** GStreamer `libcamerasrc`
- **Controles:** Via API libcamera (brightness, contrast, exposure, AWB, etc.)
- **Dependências:** `libcamera`, `libcamera-ipa`, `gst-plugin-libcamera`

### 2.4 PipeWire — Câmeras Virtuais e Stream Routing

**Cobre:** Câmeras virtuais, OBS Virtual Camera, screen capture, XDP camera portal.

- **Detecção:** `pw-cli list-objects | grep -i video` ou `wpctl status`
- **Preview:** GStreamer `pipewiresrc`
- **Controles:** Limitados (depende da fonte real)
- **Dependências:** `pipewire`, `pipewire-v4l2` (já instalados)

### 2.5 Câmeras IP (RTSP/HTTP)

**Cobre:** Câmeras de segurança, webcams IP, drones com stream.

- **Detecção:** Manual (URL RTSP/HTTP fornecida pelo usuário)
- **Preview:** GStreamer `rtspsrc` / `souphttpsrc`
- **Controles:** ONVIF PTZ (se suportado)
- **Dependências:** `gstreamer` (já instalado)

### Tabela de Prioridade de Backend

| Backend     | Detecção | Hotplug | Controles | Preview | Prioridade |
|-------------|----------|---------|-----------|---------|------------|
| V4L2/UVC    | Auto     | udev    | Completo  | Nativo  | P0         |
| GPhoto2     | Auto     | USB     | Parcial   | FFmpeg  | P0         |
| libcamera   | Auto     | -       | Parcial   | Nativo  | P1         |
| PipeWire    | Auto     | -       | Mínimo    | Nativo  | P1         |
| IP (RTSP)   | Manual   | -       | ONVIF     | Nativo  | P2         |

---

## 3. Estrutura de Diretórios

```
big-digicam/
├── main.py                         # Entry point, Application class
├── constants.py                    # App ID, versão, caminhos, enums
├── PLANNING.md                     # Este documento
├── COPYING                         # GPLv3
├── README.md                       # Documentação
│
├── core/                           # Lógica de negócio (ZERO imports de GTK)
│   ├── __init__.py
│   ├── camera_manager.py           # Detecção, seleção, hotplug de câmeras
│   ├── camera_backend.py           # ABC CameraBackend + registro
│   ├── backends/
│   │   ├── __init__.py
│   │   ├── v4l2_backend.py         # UVC/USB webcams via V4L2
│   │   ├── gphoto2_backend.py      # DSLR/Mirrorless via gphoto2
│   │   ├── libcamera_backend.py    # CSI/ISP cameras via libcamera
│   │   ├── pipewire_backend.py     # PipeWire virtual cameras
│   │   └── ip_backend.py           # RTSP/HTTP cameras
│   ├── stream_engine.py            # Construção de pipelines GStreamer
│   ├── photo_capture.py            # Captura de fotos (multi-backend)
│   ├── virtual_camera.py           # v4l2loopback management
│   └── controls.py                 # Abstração de controles (brilho, contraste, etc.)
│
├── ui/                             # Toda a interface GTK4/Adwaita
│   ├── __init__.py
│   ├── window.py                   # ApplicationWindow principal
│   ├── header_bar.py               # HeaderBar customizado
│   ├── preview_area.py             # Área de preview com GStreamer sink
│   ├── camera_controls_page.py     # Sidebar: controles da câmera ativa
│   ├── camera_selector.py          # Dropdown + status de câmera
│   ├── photo_gallery.py            # Galeria de fotos capturadas
│   ├── settings_page.py            # AdwPreferencesPage: config global
│   ├── virtual_camera_page.py      # Gestão de v4l2loopback devices
│   ├── ip_camera_dialog.py         # Diálogo para adicionar câmera IP
│   ├── welcome_dialog.py           # Boas-vindas / primeiro uso
│   ├── dependency_dialog.py        # VTE para instalar dependências
│   ├── about_dialog.py             # Diálogo Sobre
│   └── floating_toolbar.py         # Barra flutuante (foto/gravar/parar)
│
├── utils/                          # Utilitários compartilhados
│   ├── __init__.py
│   ├── i18n.py                     # Gettext wrapper
│   ├── settings_manager.py         # JSON em ~/.config/big-digicam/
│   ├── dependency_checker.py       # Verifica dependências do sistema
│   ├── xdg.py                      # Resolução de caminhos XDG
│   └── async_worker.py             # Helpers para threads + GLib.idle_add
│
├── icons/                          # SVGs do app
│   └── big-digicam.svg
│
├── locale/                         # Traduções .po
│   ├── big-digicam.pot
│   ├── pt_BR.po
│   ├── en.po
│   └── ...
│
├── script/
│   ├── install-archlinux.sh        # Instalação de dependências
│   └── run_webcam_gphoto2.sh       # Script de streaming gphoto2 (mantido)
│
├── etc/
│   └── modprobe.d/
│       └── v4l2loopback.conf
│
└── pkgbuild/
    ├── PKGBUILD
    └── pkgbuild.install
```

---

## 4. Fases de Implementação

### Fase 0 — Fundações (Infraestrutura)
> Criar a base sobre a qual todo o resto será construído.

- [ ] **0.1** Criar `constants.py` com APP_ID, versão, caminhos XDG
- [ ] **0.2** Criar `utils/settings_manager.py` (JSON em `~/.config/big-digicam/`)
- [ ] **0.3** Criar `utils/xdg.py` (caminhos XDG: config, data, cache, pictures)
- [ ] **0.4** Criar `utils/async_worker.py` (run_in_thread decorator + GLib.idle_add callback)
- [ ] **0.5** Adaptar `utils/i18n.py` (gettext com fallback)
- [ ] **0.6** Criar `utils/dependency_checker.py` (verifica gphoto2, ffmpeg, v4l-utils, libcamera, etc.)

### Fase 1 — Backend Abstraction Layer
> Camada que permite suportar qualquer tipo de câmera de forma plugável.

- [ ] **1.1** Criar `core/camera_backend.py` — ABC `CameraBackend`:
  ```python
  class CameraBackend(ABC):
      @abstractmethod
      def detect_cameras(self) -> list[CameraInfo]: ...
      @abstractmethod
      def get_controls(self, camera: CameraInfo) -> list[CameraControl]: ...
      @abstractmethod
      def set_control(self, camera: CameraInfo, control_id: str, value: Any) -> bool: ...
      @abstractmethod
      def get_gst_source(self, camera: CameraInfo) -> str: ...
      @abstractmethod
      def get_supported_formats(self, camera: CameraInfo) -> list[VideoFormat]: ...
      @abstractmethod
      def can_capture_photo(self) -> bool: ...
      @abstractmethod
      def capture_photo(self, camera: CameraInfo, output_path: str) -> bool: ...
  ```
- [ ] **1.2** Definir dataclasses: `CameraInfo`, `CameraControl`, `VideoFormat`
- [ ] **1.3** Implementar `core/backends/v4l2_backend.py`
  - Detecção via `v4l2-ctl --list-devices`
  - Controles via `v4l2-ctl -d /dev/videoX --list-ctrls-menus`
  - Formatos via `v4l2-ctl -d /dev/videoX --list-formats-ext`
  - Set controls via `v4l2-ctl -d /dev/videoX --set-ctrl name=value`
  - Source GStreamer: `v4l2src device=/dev/videoX`
  - Foto: GStreamer snapshot ou ffmpeg single frame
- [ ] **1.4** Implementar `core/backends/gphoto2_backend.py`
  - Detecção via `gphoto2 --auto-detect`
  - Controles via `gphoto2 --list-all-config` + `--get-config`
  - Foto via `gphoto2 --capture-image-and-download`
  - Vídeo via script `run_webcam_gphoto2.sh` → v4l2loopback
  - Source GStreamer: `udpsrc` (stream UDP do FFmpeg)
- [ ] **1.5** Implementar `core/backends/libcamera_backend.py`
  - Detecção via `cam --list` ou `libcamera-hello --list-cameras`
  - Source GStreamer: `libcamerasrc camera-name=X`
  - Controles via propriedades do element GStreamer
- [ ] **1.6** Implementar `core/backends/pipewire_backend.py`
  - Detecção via `pw-cli list-objects` filtrando Video/Source
  - Source GStreamer: `pipewiresrc path=X`
- [ ] **1.7** Implementar `core/backends/ip_backend.py`
  - Câmera adicionada manualmente (URL RTSP/HTTP)
  - Source GStreamer: `rtspsrc location=X` ou `souphttpsrc`

### Fase 2 — Camera Manager
> Orquestra todos os backends, detecta hotplug, gerencia seleção.

- [ ] **2.1** Criar `core/camera_manager.py`
  - Registra backends disponíveis (verifica dependências antes)
  - Detecta câmeras de todos os backends em paralelo (threads)
  - Emite sinais GObject: `camera-added`, `camera-removed`, `camera-changed`
  - Hotplug via `udevadm monitor --subsystem-type=video4linux` (pipe assíncrono)
  - Fallback: polling a cada 5s (como o original, mas otimizado com lsusb diff)
  - Filtra dispositivos duplicados (mesma câmera detectada por V4L2 + PipeWire)
- [ ] **2.2** Criar `core/virtual_camera.py`
  - Gerencia v4l2loopback: load, unload, list devices
  - Atribui virtual devices a backends que precisam (gphoto2, IP)

### Fase 3 — Stream Engine
> Motor de preview e streaming via GStreamer.

- [ ] **3.1** Criar `core/stream_engine.py`
  - Monta pipeline GStreamer dinâmico conforme o backend
  - Pipelines V4L2: `v4l2src ! videoconvert ! gtksink`
  - Pipelines gphoto2: `udpsrc ! tsdemux ! decodebin ! videoconvert ! gtksink`
  - Pipelines libcamera: `libcamerasrc ! videoconvert ! gtksink`
  - Pipelines PipeWire: `pipewiresrc ! videoconvert ! gtksink`
  - Pipelines RTSP: `rtspsrc ! rtph264depay ! decodebin ! videoconvert ! gtksink`
  - Suporte a `Gtk.Picture` + `Gdk.Paintable` para preview
  - Fallback automático entre pipelines
  - FPS counter integrado
  - Sinal: `stream-started`, `stream-stopped`, `stream-error`, `fps-updated`

### Fase 4 — Interface GTK4/Adwaita
> Seguindo padrões do big-video-converter como referência visual.

- [ ] **4.1** Criar `main.py` renovado (Application class)
  - `Adw.Application` com `application_id="br.com.biglinux.digicam"`
  - Flags: `HANDLES_COMMAND_LINE` (aceitar device como argumento)
  - `GtkApplication` single-instance padrão
  - Icon theme setup com ícones locais
  - Lifecycle: activate → create_window → detect_cameras → show
- [ ] **4.2** Criar `ui/window.py` — Layout principal
  ```
  ApplicationWindow (responsivo, min 700x500)
  └── Box VERTICAL
      ├── HeaderBar (status_câmera | título | menu)
      ├── ProgressBar (thin, pulse durante operações)
      └── Paned HORIZONTAL (responsivo via AdwBreakpoint)
          ├── LEFT: Preview Area (GStreamer sink + overlay toolbar)
          └── RIGHT: Controls Sidebar (ScrolledWindow + Clamp)
              └── ViewStack
                  ├── Page "controls": Camera Controls
                  ├── Page "gallery": Photo Gallery
                  ├── Page "virtual": Virtual Camera
                  └── Page "settings": Global Settings
  ```
- [ ] **4.3** Criar `ui/header_bar.py`
  - Camera selector (DropDown com ícone de status)
  - Título central: nome do app
  - Menu hamburger: Refresh, Nova Janela, IP Camera, Sobre, Sair
  - Action: `app.refresh`, `app.new_window`, `app.add_ip`, `app.about`, `app.quit`
- [ ] **4.4** Criar `ui/preview_area.py`
  - Container com preview GStreamer (Gtk.Picture + Gdk.Paintable)
  - Overlay com FPS counter (top-right, OSD style)
  - Overlay com floating toolbar (bottom-center)
  - Floating toolbar: [thumbnail] [📷 Capturar / 🔴 Gravar] [⏹ Parar]
  - Background escuro (#1a1a1a) com border-radius
  - Aspect ratio mantido (Gtk.ContentFit.CONTAIN)
  - Placeholder quando sem stream: `AdwStatusPage` com ícone de câmera
- [ ] **4.5** Criar `ui/camera_controls_page.py`
  - Lê controles do backend ativo dinamicamente
  - Agrupa por categoria:
    - 🎨 **Imagem:** Brilho, Contraste, Saturação, Matiz, Nitidez
    - 📸 **Exposição:** Auto/Manual, Tempo, Ganho, Compensação backlight
    - 🔍 **Foco:** Auto/Manual, Distância focal, Zoom
    - ⚖️ **Balanço de Branco:** Auto/Manual, Temperatura
    - 🎚️ **Avançado:** Frequência de energia, Pan/Tilt, gamma
  - Cada grupo: `Adw.PreferencesGroup`
  - Cada controle renderizado conforme tipo:
    - `int` → `Adw.ActionRow` + `Gtk.Scale` (slider horizontal)
    - `bool` → `Adw.SwitchRow`
    - `menu` → `Adw.ComboRow`
    - `int64` (absoluto) → `Adw.SpinRow`
  - Botão "Reset to Defaults" no topo de cada grupo
  - Mudanças aplicadas em real-time (debounce 50ms para sliders)
  - Salvar/Carregar perfis de configuração por câmera
- [ ] **4.6** Criar `ui/camera_selector.py`
  - `Adw.ComboRow` ou `Gtk.DropDown` com:
    - Ícone por tipo de backend (USB, DSLR, IP, virtual)
    - Nome da câmera
    - Subtítulo: backend + porta/device
  - Ícone de status (verde = ok, vermelho = erro, amarelo = ocupada)
  - Atualiza automaticamente com hotplug
- [ ] **4.7** Criar `ui/photo_gallery.py`
  - Grid de thumbnails das fotos capturadas
  - `Gtk.FlowBox` com `Gtk.Picture` redimensionados
  - Click abre foto com `xdg-open` (via portal `org.freedesktop.portal.OpenURI`)
  - Botão "Abrir pasta" → abre diretório de fotos
  - Mostra metadados (data, tamanho, resolução)
- [ ] **4.8** Criar `ui/virtual_camera_page.py`
  - Lista de v4l2loopback devices
  - Status: livre / em uso / por quem
  - Ação: Criar Virtual Camera a partir da câmera ativa
  - Mostra comando v4l2loopback para OBS/Meet
- [ ] **4.9** Criar `ui/settings_page.py`
  - `Adw.PreferencesPage` com:
    - **Geral:** Diretório de fotos, formato de nome, tema (sistema/claro/escuro)
    - **Preview:** Resolução preferida, FPS limit, mirror horizontal
    - **GPhoto2:** Bitrate, script customizado
    - **Avançado:** Logging, debug mode, reset tudo
- [ ] **4.10** Criar `ui/welcome_dialog.py`
  - `Adw.Dialog` com carousel de features
  - Mostrado no primeiro uso (controlado por settings)
  - Detecta dependências faltantes e oferece instalação
- [ ] **4.11** Criar `ui/ip_camera_dialog.py`
  - `Adw.Dialog` com:
    - Entry para URL (RTSP/HTTP)
    - Entry para nome amigável
    - Botão "Testar conexão"
    - Salvamento em lista persistente
- [ ] **4.12** Criar `ui/about_dialog.py`
  - `Adw.AboutDialog` completo
- [ ] **4.13** Criar `ui/floating_toolbar.py`
  - Barra flutuante estilo câmera de celular
  - Thumbnail circular da última foto (esquerda)
  - Botão de ação principal no centro (contexto-switch: foto/gravar)
  - Botão de parar (direita, só visível durante gravação)
  - Estilo OSD com blur background

### Fase 5 — Captura de Foto
> Captura multi-backend.

- [ ] **5.1** Criar `core/photo_capture.py`
  - V4L2: GStreamer snapshot (1 frame → JPEG/PNG)
  - gphoto2: `gphoto2 --capture-image-and-download --filename X`
  - libcamera: `libcamera-still -o X` ou GStreamer snapshot
  - PipeWire/IP: GStreamer snapshot
  - Salva em `~/Pictures/Big DigiCam/YYYY-MM-DD/`
  - Nome: `foto_YYYYMMDD_HHMMSS.jpg` (configurável)
  - Retorna path da foto capturada
  - Thumbnail gerado automaticamente (256px, cache em `~/.cache/big-digicam/thumbs/`)

### Fase 6 — Notificações e Feedback
> Sistema de feedback visual sem AdwToast.

- [ ] **6.1** Implementar sistema de notificação inline
  - `Gtk.Revealer` no topo do preview (slide-down)
  - Estilos: accent, success, warning, error (cores Adwaita)
  - Auto-hide após 3 segundos
  - Dismissível com click
  - Texto + ícone semântico
  - Anúncio acessível via `ATK` para Orca

### Fase 7 — Polimento e Competição

- [ ] **7.1** CSS conciso e consistente com Adwaita HIG
- [ ] **7.2** Responsive layout com `AdwBreakpoint` (mobile → sidebar oculta)
- [ ] **7.3** Testes de acessibilidade com Orca Screen Reader
- [ ] **7.4** Testes de teclado (Tab order, Enter/Space actions)
- [ ] **7.5** Teste com 200% font scaling
- [ ] **7.6** Teste de contraste WCAG AA
- [ ] **7.7** Traduções completas (30+ idiomas)
- [ ] **7.8** Ícone do app em resolução escalável (SVG)
- [ ] **7.9** Screenshots/mockups para README
- [ ] **7.10** Man page e `--help` no CLI

---

## 5. Detalhamento por Módulo

### 5.1 `constants.py`

```python
APP_ID = "br.com.biglinux.digicam"
APP_NAME = "Big DigiCam"
APP_VERSION = "2.0.0"
APP_ICON = "big-digicam"
APP_WEBSITE = "https://github.com/biglinux/big-digicam"
APP_ISSUE_URL = "https://github.com/biglinux/big-digicam/issues"
APP_LICENSE = Gtk.License.GPL_3_0
APP_COPYRIGHT = "© 2026 BigLinux Team"

# XDG paths resolved at runtime
# CONFIG_DIR = ~/.config/big-digicam/
# DATA_DIR = ~/.local/share/big-digicam/
# CACHE_DIR = ~/.cache/big-digicam/
# PHOTOS_DIR = ~/Pictures/Big DigiCam/

# Backend identifiers
class BackendType(enum.Enum):
    V4L2 = "v4l2"
    GPHOTO2 = "gphoto2"
    LIBCAMERA = "libcamera"
    PIPEWIRE = "pipewire"
    IP = "ip"

# Control categories
class ControlCategory(enum.Enum):
    IMAGE = "image"           # brightness, contrast, saturation, hue, sharpness
    EXPOSURE = "exposure"     # auto/manual, time, gain, backlight
    FOCUS = "focus"           # auto/manual, distance, zoom
    WHITE_BALANCE = "wb"      # auto/manual, temperature
    ADVANCED = "advanced"     # power_line, pan, tilt, gamma
```

### 5.2 `core/camera_backend.py` — Interface Abstrata

```python
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

class ControlType(Enum):
    INTEGER = "int"       # Slider
    BOOLEAN = "bool"      # Switch
    MENU = "menu"         # ComboBox (choices list)
    BUTTON = "button"     # Trigger action

@dataclass
class CameraControl:
    id: str                          # "brightness", "exposure_auto"
    name: str                        # Human-readable localized name
    category: ControlCategory        # IMAGE, EXPOSURE, etc.
    control_type: ControlType        # int, bool, menu
    value: Any                       # Current value
    default: Any                     # Default value
    minimum: int | None = None       # For int type
    maximum: int | None = None       # For int type
    step: int = 1                    # For int type
    choices: list[str] | None = None # For menu type
    flags: str = ""                  # "inactive", "read-only", etc.

@dataclass
class VideoFormat:
    width: int
    height: int
    fps: list[float]
    pixel_format: str               # "YUYV", "MJPG", "H264", etc.
    description: str = ""

@dataclass
class CameraInfo:
    id: str                          # Unique identifier
    name: str                        # Human-readable name
    backend: BackendType             # Which backend detected this
    device_path: str                 # /dev/video0, usb:001,004, rtsp://...
    capabilities: list[str] = field(default_factory=list)  # ["photo", "video", "controls"]
    formats: list[VideoFormat] = field(default_factory=list)
    is_virtual: bool = False         # v4l2loopback device

class CameraBackend(ABC):
    """Abstract base class for camera backends."""

    @abstractmethod
    def get_backend_type(self) -> BackendType: ...

    @abstractmethod
    def is_available(self) -> bool:
        """Check if this backend's dependencies are installed."""
        ...

    @abstractmethod
    def detect_cameras(self) -> list[CameraInfo]:
        """Detect all cameras supported by this backend."""
        ...

    @abstractmethod
    def get_controls(self, camera: CameraInfo) -> list[CameraControl]:
        """Get available controls for a camera."""
        ...

    @abstractmethod
    def set_control(self, camera: CameraInfo, control_id: str, value: Any) -> bool:
        """Set a control value. Returns True on success."""
        ...

    @abstractmethod
    def get_gst_source_element(self, camera: CameraInfo, format: VideoFormat | None = None) -> str:
        """Return GStreamer pipeline source string for this camera."""
        ...

    @abstractmethod
    def can_capture_photo(self) -> bool:
        """Whether this backend supports photo capture."""
        ...

    @abstractmethod
    def capture_photo(self, camera: CameraInfo, output_path: str) -> bool:
        """Capture a single photo. Returns True on success."""
        ...
```

### 5.3 `utils/async_worker.py`

```python
import threading
from gi.repository import GLib

def run_in_thread(callback_success=None, callback_error=None):
    """Decorator to run a function in a background thread.

    Results are posted to the GTK main thread via GLib.idle_add.
    """
    def decorator(func):
        def wrapper(*args, **kwargs):
            def thread_target():
                try:
                    result = func(*args, **kwargs)
                    if callback_success:
                        GLib.idle_add(callback_success, result)
                except Exception as e:
                    if callback_error:
                        GLib.idle_add(callback_error, e)
            threading.Thread(target=thread_target, daemon=True).start()
        return wrapper
    return decorator
```

---

## 6. UI/UX — Layout e Fluxo

### 6.1 Layout Principal (Desktop ≥ 900px)

```
┌────────────────────────────────────────────────────────────┐
│ [🟢 Logitech C920 ▾]        Big DigiCam           [≡ Menu]│ HeaderBar
├────────────────────────────────────────────────────────────┤
│ ▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓               │ ProgressBar (pulse)
├───────────────────────────────────┬────────────────────────┤
│                                   │  [🎨 Imagem] [📸 Exp] │ Sidebar tabs
│                                   │ ┌────────────────────┐ │
│        PREVIEW AREA               │ │ ☀ Brilho     [═══] │ │
│    (GStreamer live preview)        │ │ 🔲 Contraste [═══] │ │
│                                   │ │ 🎨 Saturação [═══] │ │
│                                   │ │ 🔄 Matiz     [═══] │ │
│              [FPS 30]             │ │ ✨ Nitidez   [═══] │ │
│                                   │ ├────────────────────┤ │
│                                   │ │ ↩ Reset Padrões    │ │
│   [📷]    [  ⏺  Capturar  ]      │ │ 💾 Salvar Perfil   │ │
│                                   │ │ 📂 Carregar Perfil │ │
└───────────────────────────────────┴────────────────────────┘
```

### 6.2 Layout Compacto (< 900px ou mobile)

```
┌──────────────────────────────┐
│ [🟢 ▾]   Big DigiCam  [≡]   │ HeaderBar (compacto)
├──────────────────────────────┤
│                              │
│      PREVIEW AREA            │
│    (fullscreen preview)      │
│                              │
│              [FPS 30]        │
│                              │
│   [📷]  [  ⏺  ]    [⚙]     │ Floating toolbar + gear
└──────────────────────────────┘
                 ↓ gear abre...
┌──────────────────────────────┐
│     Camera Controls          │ AdwBottomSheet
│  [🎨 Imagem] [📸 Exposição]   │
│  Brilho ═══════════          │
│  Contraste ════════          │
└──────────────────────────────┘
```

### 6.3 Estados da Interface

| Estado | Preview Area | Toolbar | Sidebar | Header Status |
|--------|-------------|---------|---------|---------------|
| Sem câmera | AdwStatusPage "Conecte uma câmera" | Oculta | Vazia | 🔴 "Nenhuma câmera" |
| Câmera detectada | AdwStatusPage "Clique para iniciar" | Visível | Controles carregados | 🟢 "Camera Name" |
| Preview ativo | Stream ao vivo | [📷] [⏹] | Controles responsivos | 🟢 "Camera Name" |
| Capturando foto | Frame congelado + flash | Desabilitado | Desabilitado | 🟡 "Capturando..." |
| gphoto2 iniciando | Spinner + texto | [⏹ Cancelar] | Desabilitado | 🟡 "Iniciando..." |
| Erro | AdwStatusPage com erro | [🔄 Tentar novamente] | Desabilitado | 🔴 "Erro" |

### 6.4 Princípios de UX

1. **Progressive Disclosure:**
   - Primeiro uso: só preview + botão de captura. Controles na sidebar.
   - Controles avançados (Pan/Tilt, Gamma) em grupo "Avançado" colapsado
   - Câmeras IP e Virtual Camera em páginas separadas (não poluem a tela principal)

2. **Cognitive Load:**
   - Preview central ocupa 60%+ da tela
   - Sidebar com máximo 7 controles visíveis por grupo
   - Grupos colapsáveis (ExpanderRow)

3. **Feedback Loops:**
   - Slider move → preview atualiza em tempo real (< 50ms)
   - Foto capturada → thumbnail atualiza + notificação slide-down
   - Câmera conectada → dropdown atualiza + ícone verde + notificação
   - Câmera desconectada → preview mostra placeholder + ícone vermelho

4. **Error Prevention:**
   - Botão de captura desabilitado quando sem câmera
   - Confirmação antes de resetar todos os controles
   - Formato/resolução compatíveis pré-filtrados no dropdown

5. **Forgiving Design:**
   - "Reset to Defaults" por grupo de controles
   - Perfis de configuração salvos/carregados
   - Desfazer último reset (guardar estado anterior por 30s)

---

## 7. Acessibilidade (Orca + Teclado)

### 7.1 Requisitos Obrigatórios

Cada widget interativo DEVE ter:

```python
# Botões com ícone sem texto:
button.set_accessible_name(_("Capture Photo"))

# Sliders:
scale.set_accessible_name(_("Brightness: {value}%"))
# Atualizar ao mover:
scale.connect("value-changed", lambda s: s.set_accessible_name(
    _("Brightness: {}%").format(int(s.get_value()))
))

# Switches:
switch_row = Adw.SwitchRow(title=_("Auto Focus"))
# SwitchRow já anuncia estado "on/off" — OK

# ComboRow:
combo = Adw.ComboRow(title=_("Resolution"))
combo.set_subtitle(_("Current: 1920x1080"))
# Subtitle atualiza com a seleção

# Preview area:
preview.set_accessible_name(_("Camera preview"))
preview.set_accessible_description(_("Live video from connected camera"))

# Status icon:
status.set_accessible_name(_("Camera status: connected"))
```

### 7.2 Navegação por Teclado

| Tecla | Ação |
|-------|------|
| Tab / Shift+Tab | Navega entre widgets |
| Enter / Space | Ativa botão / toggle switch |
| Arrows ← → | Ajusta slider / navega tabs |
| Escape | Fecha diálogo / cancela operação |
| Ctrl+Q | Sair |
| Ctrl+R | Refresh câmeras |
| Ctrl+N | Nova janela |
| F11 | Fullscreen preview |
| Space | Capturar foto (quando preview ativo) |

### 7.3 Contraste e Escalabilidade

- Todos os textos: contraste mínimo 4.5:1 (WCAG AA)
- Ícones de status: cor + ícone (nunca só cor)
  - 🟢 `emblem-ok-symbolic` + green
  - 🔴 `dialog-error-symbolic` + red
  - 🟡 `emblem-synchronizing-symbolic` + yellow
- Teste com GSetting `text-scaling-factor` = 2.0
- Nenhum `min-width`/`min-height` hardcoded que quebre em DPI alto

---

## 8. Internacionalização (i18n)

### Setup

```python
# utils/i18n.py
import gettext
import locale
import os

APP_NAME = "big-digicam"

def setup_i18n():
    locale_dir = os.path.join(os.path.dirname(__file__), '..', 'locale')
    locale.setlocale(locale.LC_ALL, '')
    gettext.bindtextdomain(APP_NAME, locale_dir)
    gettext.textdomain(APP_NAME)
    return gettext.gettext

_ = setup_i18n()
```

### Regras

- Toda string visível ao usuário usa `_()`
- Sem jargão técnico: `"Balanço de Branco"` (não `"White Balance Temperature K"`)
- Placeholders: `_("{camera} connected").format(camera=name)`
- Nomes de controle V4L2 têm tabela de tradução humanizada:
  ```python
  V4L2_CONTROL_NAMES = {
      "brightness": _("Brightness"),
      "contrast": _("Contrast"),
      "white_balance_temperature": _("White Balance Temperature"),
      "exposure_absolute": _("Exposure Time"),
      "focus_absolute": _("Focus Distance"),
      ...
  }
  ```

---

## 9. Sistema de Controles da Câmera

### 9.1 Fluxo de Controles

```
Backend detecta câmera → get_controls() → lista de CameraControl
                                            ↓
UI renderiza conforme tipo (slider/switch/combo)
                                            ↓
Usuário ajusta slider → debounce 50ms → backend.set_control()
                                            ↓
Backend executa (v4l2-ctl / gphoto2 --set-config)
                                            ↓
Sucesso: UI confirma visualmente (sem notificação)
Erro: inline error abaixo do controle
```

### 9.2 Perfis de Configuração

```json
// ~/.config/big-digicam/profiles/logitech_c920_office.json
{
  "name": "Office Lighting",
  "camera_pattern": "Logitech C920",
  "controls": {
    "brightness": 140,
    "contrast": 128,
    "saturation": 120,
    "white_balance_automatic": false,
    "white_balance_temperature": 4500,
    "exposure_auto": 1,
    "focus_auto": true
  }
}
```

- Perfis salvos por nome amigável
- Auto-load: quando câmera é conectada, se houver perfil automático
- Export/Import de perfis (JSON)

### 9.3 Mapeamento V4L2 → UI

| V4L2 Control              | UI Widget          | Grupo       |
|---------------------------|--------------------|-------------|
| brightness                | Scale (slider)     | Imagem      |
| contrast                  | Scale              | Imagem      |
| saturation                | Scale              | Imagem      |
| hue                       | Scale              | Imagem      |
| sharpness                 | Scale              | Imagem      |
| gamma                     | Scale              | Avançado    |
| exposure_auto             | ComboRow           | Exposição   |
| exposure_absolute         | Scale              | Exposição   |
| exposure_auto_priority    | SwitchRow          | Exposição   |
| gain                      | Scale              | Exposição   |
| backlight_compensation    | Scale              | Exposição   |
| focus_auto                | SwitchRow          | Foco        |
| focus_absolute            | Scale              | Foco        |
| zoom_absolute             | Scale              | Foco        |
| white_balance_automatic   | SwitchRow          | Bal. Branco |
| white_balance_temperature | Scale              | Bal. Branco |
| power_line_frequency      | ComboRow           | Avançado    |
| pan_absolute              | Scale              | Avançado    |
| tilt_absolute             | Scale              | Avançado    |

---

## 10. Pipeline de Vídeo

### 10.1 Pipelines GStreamer por Backend

**V4L2 (USB Webcam):**
```
v4l2src device=/dev/video0
  ! video/x-raw,width=1920,height=1080,framerate=30/1
  ! videoconvert
  ! video/x-raw,format=RGB
  ! appsink name=sink emit-signals=True drop=True max-buffers=2 sync=False
```

**GPhoto2 (DSLR via FFmpeg → UDP):**
```
udpsrc port={port} address=127.0.0.1 caps="video/mpegts"
  ! queue max-size-bytes=2097152
  ! tsdemux
  ! decodebin
  ! videoconvert
  ! video/x-raw,format=RGB
  ! appsink name=sink emit-signals=True drop=True max-buffers=2 sync=False
```

**libcamera:**
```
libcamerasrc camera-name={id}
  ! video/x-raw,width=1920,height=1080,framerate=30/1
  ! videoconvert
  ! video/x-raw,format=RGB
  ! appsink name=sink emit-signals=True drop=True max-buffers=2 sync=False
```

**PipeWire:**
```
pipewiresrc path={node_id}
  ! videoconvert
  ! video/x-raw,format=RGB
  ! appsink name=sink emit-signals=True drop=True max-buffers=2 sync=False
```

**RTSP:**
```
rtspsrc location={url} latency=300
  ! rtph264depay
  ! decodebin
  ! videoconvert
  ! video/x-raw,format=RGB
  ! appsink name=sink emit-signals=True drop=True max-buffers=2 sync=False
```

### 10.2 Preview Rendering

Usar `Gdk.MemoryTexture` + `Gtk.Picture.set_paintable()`:

```python
def on_new_sample(self, sink):
    sample = sink.emit("pull-sample")
    buf = sample.get_buffer()
    caps = sample.get_caps()
    struct = caps.get_structure(0)
    width = struct.get_value("width")
    height = struct.get_value("height")

    result, map_info = buf.map(Gst.MapFlags.READ)
    if result:
        glib_bytes = GLib.Bytes.new(map_info.data)
        buf.unmap(map_info)
        GLib.idle_add(self._update_texture, width, height, glib_bytes)
    return Gst.FlowReturn.OK

def _update_texture(self, w, h, data):
    texture = Gdk.MemoryTexture.new(w, h, Gdk.MemoryFormat.R8G8B8, data, w * 3)
    self.preview_picture.set_paintable(texture)
    return False
```

### 10.3 Virtual Camera Output

Para enviar preview para OBS/Meet/Zoom via v4l2loopback:

```
{camera_source}
  ! videoconvert
  ! tee name=t
    t. ! queue ! video/x-raw,format=RGB ! appsink (preview)
    t. ! queue ! video/x-raw,format=YUY2 ! v4l2sink device=/dev/video{virtual}
```

---

## 11. Empacotamento (PKGBUILD)

```bash
pkgname=big-digicam
pkgver=2.0.0
pkgrel=1
pkgdesc="Universal webcam control center for Linux"
arch=('any')
url="https://github.com/biglinux/big-digicam"
license=('GPL3')
depends=(
    # Core
    'python'
    'python-gobject'
    'gtk4'
    'libadwaita'
    'gstreamer'
    'gst-plugins-base'
    'gst-plugins-good'     # v4l2src, jpegdec, etc.
    'gst-plugins-bad'      # v4l2codecs
    'gst-libav'            # H264 decode

    # V4L2
    'v4l-utils'

    # GPhoto2
    'gphoto2'
    'libgphoto2'
    'ffmpeg'
    'v4l2loopback-dkms'

    # System
    'bigsudo'
    'xdg-utils'
)
optdepends=(
    'libcamera: Support for CSI/ISP cameras'
    'gst-plugin-libcamera: GStreamer libcamera plugin'
    'pipewire-v4l2: PipeWire virtual camera support'
    'python-opencv: Fallback video preview'
)
```

---

## 12. Checklist de Qualidade para Competição

### Arquitetura
- [ ] Lógica de negócio 100% separada da UI (core/ não importa gi.repository)
- [ ] Cada módulo tem responsabilidade única
- [ ] Signals GObject usados corretamente (notify::property, não callbacks diretos)
- [ ] Estado centralizado no Application, não espalhado pelos widgets
- [ ] Erros tratados em cada camada (backend → manager → UI)

### GTK4/Adwaita
- [ ] Widgets Adwaita corretos (PreferencesGroup, ComboRow, SwitchRow, ActionRow)
- [ ] SEM AdwToastOverlay — usa Gtk.Revealer personalizado
- [ ] Layout responsivo com AdwClamp (max 800px, threshold 600px)
- [ ] CSS usa variáveis Adwaita (@accent_bg_color, @window_fg_color, etc.)
- [ ] GActions para todas as ações do menu (app.about, app.quit, etc.)
- [ ] Singleton GtkApplication lifecycle correto
- [ ] Decoração de janela compatível com botões esquerda/direita

### Acessibilidade Orca
- [ ] CADA botão tem accessible-name (set_accessible_name ou label)
- [ ] CADA entry/spin tem label associado
- [ ] CADA combo/dropdown tem label + description
- [ ] CADA switch anuncia estado (on/off)
- [ ] CADA slider anuncia valor atual
- [ ] Foco lógico: Tab percorre todos os elementos interativos
- [ ] Conteúdo dinâmico anunciado (câmera conectada/desconectada)

### Acessibilidade Geral
- [ ] TUDO acessível por teclado (zero mouse-only)
- [ ] Cor nunca é único indicador (ícone + texto + cor)
- [ ] Teste com font-scaling 2.0 — nada quebra
- [ ] Contraste WCAG AA (4.5:1 texto, 3:1 UI)
- [ ] Sem interações baseadas em tempo sem alternativa

### UX/Psicologia
- [ ] Progressive disclosure: básico visível, avançado oculto
- [ ] Max 5-7 elementos interativos por tela/grupo
- [ ] Feedback para TODA ação (visual + acessível)
- [ ] Prevenção de erro > mensagem de erro
- [ ] Reset/Undo disponível para ações destrutivas
- [ ] Subtitles em AdwActionRow para features não-óbvias
- [ ] Hierarquia visual clara (ação primária highlighted)
- [ ] First-run experience (welcome dialog + dependency check)
- [ ] Linguagem humana e simples em TODOS os labels

### Desempenho
- [ ] UI thread nunca bloqueada (todas as operações I/O em threads)
- [ ] Debounce em sliders (50ms)
- [ ] GStreamer pipeline eficiente (drop=True, max-buffers=2)
- [ ] Detecção de câmera assíncrona com lock (_detecting)
- [ ] Thumbnails em cache (~/.cache/big-digicam/)

### Segurança
- [ ] subprocess.run() com lista, NUNCA shell=True
- [ ] Nenhum segredo no código
- [ ] xdg-desktop-portal para abrir arquivos/diretórios
- [ ] Suporte X11 e Wayland (sem chamadas X11-only)
- [ ] Caminhos de arquivo escapados/validados

---

## Referência: Padrões do big-video-converter

O projeto big-video-converter serve como referência de padrões a seguir:

1. **Layout:** `Gtk.Paned` horizontal com sidebar de controles + conteúdo principal
2. **Sidebar:** `Adw.ToolbarView` com ScrolledWindow, Clamp (max 400px)
3. **Settings:** `Adw.PreferencesGroup` com `SwitchRow`, `ComboRow`, `EntryRow`
4. **HeaderBar:** Custom com ações zonificadas (esquerda/centro/direita)
5. **State Persistence:** SettingsManager JSON com defaults tipados e debounce
6. **Threading:** `threading.Thread(daemon=True)` + `GLib.idle_add()` para resultados
7. **Signals:** `notify::selected` para combos, `notify::active` para switches, `clicked` para buttons
8. **CSS:** Variáveis Adwaita, nunca cores hardcoded (exceto preview background)
9. **Tooltips:** Sistema dual X11/Wayland com fallback
10. **About:** `Adw.AboutDialog` com créditos, links, licença

---

## Ordem de Execução Recomendada

```
Fase 0 (Fundações)     → ~2h de código
Fase 1.1-1.3 (V4L2)    → Backend principal, cobertura de 95% das webcams
Fase 2 (Camera Manager) → Detecção unificada
Fase 3 (Stream Engine)  → Preview funcional
Fase 4.1-4.5 (UI Core)  → Window, Header, Preview, Controls, Selector
Fase 5 (Photo Capture)  → Captura funcional
Fase 6 (Notificações)   → Feedback visual
--- MVP FUNCIONAL ---
Fase 1.4 (GPhoto2)      → Suporte DSLR (existente, migrar)
Fase 1.5-1.7 (extras)   → libcamera, PipeWire, IP
Fase 4.6-4.13 (UI extra)→ Galeria, Virtual Camera, Settings, Welcome
Fase 7 (Polimento)       → Acessibilidade, i18n, CSS, testes
--- RELEASE 2.0 ---
```

---

*Documento gerado para guiar a reconstrução completa do Big DigiCam.*
*Atualizar conforme decisões de implementação forem tomadas.*
