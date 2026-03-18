# BigCam — Plano de Melhorias Detalhado

> Documento gerado com base na análise completa do código-fonte (41 arquivos, ~9.551 linhas).  
> Cada seção contém: problema atual, solução proposta, arquivos afetados e nível de esforço.

---

## Índice

1. [Autenticação da Virtual Cam (senha única por sessão)](#1-autenticação-da-virtual-cam)
2. [Conflito com outros aplicativos](#2-conflito-com-outros-aplicativos)
3. [Botão REC estilizado](#3-botão-de-gravação-rec)
4. [Ícone Phone as Webcam](#4-ícone-phone-as-webcam)
5. [Gerenciamento de múltiplas câmeras](#5-gerenciamento-de-múltiplas-câmeras)
6. [Internacionalização](#6-internacionalização)
7. [Performance e desempenho](#7-performance-e-desempenho)
8. [Bugs críticos a corrigir em paralelo](#8-bugs-críticos)
9. [Cronograma sugerido](#9-cronograma-sugerido)

---

## 1. Autenticação da Virtual Cam

### Problema atual

A virtual camera usa `sudo modprobe v4l2loopback` (em `core/virtual_camera.py:69`). Atualmente existe o arquivo `/etc/sudoers.d/bigcam` que já concede NOPASSWD para `modprobe v4l2loopback`, mas:

- Se o arquivo sudoers não está instalado (primeiro uso, outra distro), o `sudo` pedirá senha toda vez.
- Se o módulo precisa ser recarregado (troca de câmera, `_reload_module()`), o `sudo` é invocado novamente.
- Não existe cache de credenciais interna — cada chamada a `sudo` é independente.

### Solução proposta

#### Opção A — Preferida: Polkit + pkexec (sem sudo)

Substituir `sudo` por `pkexec` com uma policy Polkit que conceda autorização por sessão:

```
Arquivos novos:
  etc/polkit-1/actions/br.com.biglinux.bigcam.policy

Arquivo modificado:
  core/virtual_camera.py
```

**Policy Polkit** (`br.com.biglinux.bigcam.policy`):
```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE policyconfig PUBLIC
 "-//freedesktop//DTD PolicyKit Policy Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/PolicyKit/1/policyconfig.dtd">
<policyconfig>
  <action id="br.com.biglinux.bigcam.load-v4l2loopback">
    <description>Load v4l2loopback kernel module for BigCam</description>
    <message>Authentication is required to enable the virtual camera</message>
    <defaults>
      <allow_any>auth_admin</allow_any>
      <allow_inactive>auth_admin</allow_inactive>
      <allow_active>auth_admin_keep</allow_active>
    </defaults>
    <annotate key="org.freedesktop.policykit.exec.path">/usr/bin/modprobe</annotate>
    <annotate key="org.freedesktop.policykit.exec.allow_gui">true</annotate>
  </action>
</policyconfig>
```

- `auth_admin_keep` = autentica uma vez e mantém até a sessão encerrar (ou timeout de 5 min padrão do Polkit).
- `allow_gui=true` = diálogo gráfico nativo do ambiente (KDE/GNOME).

**Mudanças em `virtual_camera.py`:**

```python
# Antes:
subprocess.run(["sudo", "modprobe", "v4l2loopback", ...])

# Depois:
subprocess.run(["pkexec", "modprobe", "v4l2loopback", ...])
```

**Script wrapper** (alternativa mais robusta):
Criar um script helper `/usr/share/biglinux/bigcam/script/load-v4l2loopback.sh`:
```bash
#!/bin/bash
set -euo pipefail
modprobe v4l2loopback devices=2 exclusive_caps=1 video_nr=10,11 \
    card_label="BigCam Virtual","BigCam Virtual (v4l2)"
```
E usar `pkexec /usr/share/biglinux/bigcam/script/load-v4l2loopback.sh` — isso permite a policy Polkit referenciar um caminho fixo e seguro.

#### Opção B — Fallback: Cache de sudo interno

Se Polkit não for viável em alguns ambientes:

```python
class VirtualCamera:
    _sudo_validated: bool = False

    @classmethod
    def _ensure_sudo_cache(cls) -> bool:
        """Validate sudo credentials once per session."""
        if cls._sudo_validated:
            return True
        result = subprocess.run(
            ["sudo", "-v"],  # refresh sudo timestamp
            capture_output=True,
        )
        cls._sudo_validated = result.returncode == 0
        return cls._sudo_validated
```

Chamar `_ensure_sudo_cache()` antes de qualquer `sudo modprobe`.

#### Opção C — Manter sudoers mas garantir instalação

O `etc/sudoers.d/bigcam` já existe no pacote. Garantir que o `pkgbuild.install` o instale corretamente e que `virtual_camera.py` verifique se o NOPASSWD está ativo antes de chamar `sudo`:

```python
@staticmethod
def _has_nopasswd() -> bool:
    """Check if sudo for modprobe is passwordless."""
    result = subprocess.run(
        ["sudo", "-n", "modprobe", "--version"],
        capture_output=True,
    )
    return result.returncode == 0
```

### Recomendação

**Usar Opção A (Polkit)** como método padrão, com **Opção C** como fallback automático.

### Arquivos afetados

| Arquivo | Ação |
|---------|------|
| `core/virtual_camera.py` | Substituir `sudo` por `pkexec` + fallback `sudo -n` |
| `etc/polkit-1/actions/br.com.biglinux.bigcam.policy` | **Novo** — policy Polkit |
| `script/load-v4l2loopback.sh` | **Novo** — script wrapper para modprobe |
| `pkgbuild/PKGBUILD` | Adicionar instalação da policy Polkit |
| `ui/virtual_camera_page.py` | Mostrar feedback de autenticação (spinner) |

### Esforço: Baixo-Médio

---

## 2. Conflito com outros aplicativos

### Problema atual

Quando outro aplicativo (ex: OBS) está usando a webcam, a `_find_device_users()` em `stream_engine.py:41` detecta os PIDs via `fuser`, mas o feedback ao usuário é apenas um toast genérico:

```python
self.emit("error", _("Camera in use by: %s") % apps)
```

Não há:
- Diálogo explicativo
- Opção de forçar fechamento
- Orientação para usar a Câmera Virtual como alternativa
- Informação sobre quais configurações continuam funcionando

### Solução proposta

#### 2.1 Diálogo de conflito detalhado

Criar um método `_show_device_busy_dialog()` em `window.py` que intercepte o sinal `error` do StreamEngine quando a mensagem contém nomes de processos:

```python
def _show_device_busy_dialog(
    self,
    device_name: str,
    blocking_apps: list[str],
    camera: CameraInfo,
) -> None:
    # Adw.AlertDialog com:
    # - Heading: "Camera in use"
    # - Body: "The camera '{device_name}' is being used by: {apps}.
    #          The image cannot be displayed while another application
    #          has exclusive access to this device.
    #          Camera settings and controls will continue to work normally."
    # - Response "force-close": "Force close {app}" (DESTRUCTIVE)
    # - Response "virtual-cam":  "Enable Virtual Camera" (SUGGESTED)
    # - Response "cancel": "Close"
```

#### 2.2 Forçar fechamento

```python
def _force_close_app(self, app_name: str, device_path: str) -> None:
    """Kill processes using the device, then retry camera."""
    pids = _get_pids_for_device(device_path)
    for pid in pids:
        os.kill(int(pid), signal.SIGTERM)
    # Aguardar 2s e tentar novamente
    GLib.timeout_add(2000, self._retry_camera_after_force_close)
```

**Importante**: Mostrar aviso confirmando a ação antes de matar o processo:

```python
confirm = Adw.AlertDialog.new(
    _("Force close %s?") % app_name,
    _("This will terminate %s. Unsaved data may be lost.") % app_name,
)
confirm.add_response("cancel", _("Cancel"))
confirm.add_response("force", _("Force Close"))
confirm.set_response_appearance("force", Adw.ResponseAppearance.DESTRUCTIVE)
```

#### 2.3 Sugerir Câmera Virtual

Se a virtual camera não está ativa, incluir no diálogo:

```
"Alternatively, you can enable the Virtual Camera to share
 the camera feed with multiple applications simultaneously."

[Enable Virtual Camera]  [Close]
```

Ao clicar "Enable Virtual Camera":
1. Ativar `VirtualCamera.set_enabled(True)`
2. Carregar módulo v4l2loopback
3. Reiniciar o pipeline com output para v4l2sink
4. Orientar o usuário a configurar o outro app para usar `/dev/video10`

#### 2.4 Informar que configurações continuam

Adicionar texto no corpo do diálogo:

```
"Note: Even without video preview, you can still adjust camera
 controls (brightness, contrast, exposure, etc.) and they will
 take effect immediately on the other application."
```

#### 2.5 Fluxo completo

```
Usuário seleciona câmera
    ↓
StreamEngine tenta iniciar pipeline
    ↓ falha
_find_device_users() → ["obs", "firefox"]
    ↓
emit("error", ...) interceptado por window.py
    ↓
_show_device_busy_dialog() apresenta:
┌──────────────────────────────────────────────┐
│  Camera in use                                │
│                                               │
│  The camera "Logitech C920" is being used     │
│  by: OBS Studio.                              │
│                                               │
│  The video preview cannot be displayed while   │
│  another application has exclusive access.     │
│                                               │
│  ℹ Camera settings will continue to work.     │
│                                               │
│  Tip: Enable Virtual Camera to share the      │
│  camera with multiple applications.           │
│                                               │
│ [Force Close OBS]  [Enable Virtual Cam]  [OK] │
└──────────────────────────────────────────────┘
```

### Arquivos afetados

| Arquivo | Ação |
|---------|------|
| `ui/window.py` | Novo `_show_device_busy_dialog()`, handler do sinal `error` |
| `core/stream_engine.py` | Emitir sinal com lista de apps (não string formatada) |
| `ui/virtual_camera_page.py` | Método público `activate_from_dialog()` |
| `style.css` | Estilo para ícone de info no diálogo (opcional) |

### Esforço: Médio

---

## 3. Botão de gravação (REC)

### Problema atual

O botão de gravação em `ui/preview_area.py` é um `Gtk.ToggleButton` com ícone padrão (`media-record-symbolic`). Não tem a aparência clássica de botão REC (círculo vermelho com borda).

### Solução proposta

#### 3.1 CSS para botão REC

Adicionar classes CSS específicas em `style.css`:

```css
/* Record button — classic REC style */
.record-button {
    min-width: 40px;
    min-height: 40px;
    padding: 0;
    border-radius: 50%;
    border: 3px solid @dark_3;
    background: transparent;
    transition: all 200ms ease-in-out;
}

.record-button .rec-dot {
    min-width: 18px;
    min-height: 18px;
    border-radius: 50%;
    background-color: @error_bg_color;
    transition: all 200ms ease-in-out;
}

/* When recording — pulsing animation */
.record-button:checked .rec-dot {
    background-color: @error_color;
    animation: rec-pulse 1s ease-in-out infinite;
}

.record-button:checked {
    border-color: @error_color;
}

@keyframes rec-pulse {
    0%, 100% { opacity: 1.0; }
    50% { opacity: 0.4; }
}
```

#### 3.2 Widget customizado

Em `preview_area.py`, substituir o ToggleButton padrão por um com child customizado:

```python
def _build_record_button(self) -> Gtk.ToggleButton:
    dot = Gtk.DrawingArea()
    dot.set_content_width(18)
    dot.set_content_height(18)
    dot.add_css_class("rec-dot")

    btn = Gtk.ToggleButton()
    btn.set_child(dot)
    btn.add_css_class("record-button")
    btn.set_tooltip_text(_("Record Video (Ctrl+R)"))
    btn.set_accessible_name(_("Record Video"))
    return btn
```

**Alternativa simplificada** (sem DrawingArea):

```python
rec_btn = Gtk.ToggleButton()
rec_icon = Gtk.Box()
rec_icon.add_css_class("rec-dot")
rec_btn.set_child(rec_icon)
rec_btn.add_css_class("record-button")
```

#### 3.3 Indicador de tempo durante gravação

Quando estiver gravando, exibir um label com o tempo decorrido ao lado do botão:

```python
self._rec_timer_label = Gtk.Label(label="00:00")
self._rec_timer_label.add_css_class("rec-timer")
# CSS: .rec-timer { color: @error_color; font-variant-numeric: tabular-nums; }
```

### Arquivos afetados

| Arquivo | Ação |
|---------|------|
| `ui/preview_area.py` | Recriar botão REC com estilo visual personalizado |
| `style.css` | Classes `.record-button`, `.rec-dot`, `.rec-timer` |

### Esforço: Baixo

---

## 4. Ícone Phone as Webcam

### Problema atual

O ícone de Phone as Webcam é um ícone simbólico no header bar (provavelmente `phone-symbolic` ou similar), pouco visível e sem texto descritivo. O usuário pode não perceber a funcionalidade.

### Solução proposta

#### 4.1 Botão com texto no header

Substituir o botão de ícone puro por um botão com ícone + label no header bar:

```python
phone_btn = Gtk.Button()
phone_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
phone_icon = Gtk.Image.new_from_icon_name("phone-symbolic")
phone_label = Gtk.Label(label=_("Phone as Webcam"))
phone_label.add_css_class("caption")
phone_box.append(phone_icon)
phone_box.append(phone_label)
phone_btn.set_child(phone_box)
phone_btn.add_css_class("flat")
phone_btn.add_css_class("phone-webcam-button")
phone_btn.set_tooltip_text(_("Use your phone as a webcam"))
```

#### 4.2 Estilo CSS discreto

```css
.phone-webcam-button {
    padding: 4px 10px;
    border-radius: 6px;
    font-size: 0.85em;
}

.phone-webcam-button:hover {
    background-color: alpha(@accent_color, 0.1);
}
```

#### 4.3 Indicador de conexão

Quando o phone estiver conectado, mudar o visual do botão:

```python
def _on_phone_status_changed(self, server, status):
    if status == "connected":
        self._phone_btn.add_css_class("suggested-action")
        self._phone_btn.set_tooltip_text(_("Phone connected — tap to configure"))
    else:
        self._phone_btn.remove_css_class("suggested-action")
```

### Arquivos afetados

| Arquivo | Ação |
|---------|------|
| `ui/window.py` | Reconstruir botão Phone no header bar |
| `style.css` | Classes `.phone-webcam-button` |

### Esforço: Baixo

---

## 5. Gerenciamento de múltiplas câmeras

### Problema atual

O código em `window.py:419` mostra que ao trocar de câmera:

```python
# Stop only the GStreamer pipeline, keep other cameras' backend alive
self._stream_engine.stop(stop_backend=False)
```

- O pipeline GStreamer é parado ao trocar de câmera
- Para backends gPhoto2, `stop_streaming()` é chamado em background
- Não existe conceito de manter múltiplos pipelines simultâneos
- O `_on_close()` oferece "Keep camera on" mas apenas para uma câmera

### Solução proposta

#### 5.1 Arquitetura multi-pipeline

Criar um gerenciador de sessões de câmera que mantém múltiplas câmeras ativas:

```python
# core/session_manager.py (NOVO)

@dataclass
class CameraSession:
    camera: CameraInfo
    pipeline: Gst.Pipeline | None
    backend_active: bool
    started_at: float  # time.monotonic()

class SessionManager:
    """Manage multiple simultaneous camera sessions."""

    def __init__(self, camera_manager: CameraManager) -> None:
        self._sessions: dict[str, CameraSession] = {}
        self._active_display: str | None = None  # camera.id being shown
        self._manager = camera_manager

    def start_camera(self, camera: CameraInfo) -> bool:
        """Start a camera session. Does NOT stop other sessions."""
        if camera.id in self._sessions:
            return True  # Already running
        session = CameraSession(
            camera=camera,
            pipeline=None,
            backend_active=False,
            started_at=time.monotonic(),
        )
        self._sessions[camera.id] = session
        return True

    def switch_display(self, camera_id: str) -> bool:
        """Switch which camera is shown in the preview without stopping others."""
        if camera_id not in self._sessions:
            return False
        self._active_display = camera_id
        return True

    def stop_camera(self, camera_id: str) -> None:
        """Explicitly stop a single camera session."""
        session = self._sessions.pop(camera_id, None)
        if session and session.pipeline:
            session.pipeline.set_state(Gst.State.NULL)

    def stop_all(self) -> None:
        """Stop all camera sessions (app closing)."""
        for cam_id in list(self._sessions):
            self.stop_camera(cam_id)

    @property
    def active_sessions(self) -> list[CameraSession]:
        return list(self._sessions.values())
```

#### 5.2 Comportamento ao trocar de câmera

```
Câmera A ativa → Usuário seleciona Câmera B
    ↓
SessionManager.start_camera(B)
SessionManager.switch_display(B)
    ↓
Pipeline de A continua rodando (sem preview, mas backend ativo)
Pipeline de B inicia e exibe no preview
    ↓
Ambas câmeras estão "ligadas" — StreamEngine de A apenas sem sink visual
```

**Limitação técnica**: Manter múltiplos pipelines GStreamer consome recursos (CPU, memória, USB bandwidth). Para V4L2 webcams, o kernel permite apenas um `open()` exclusivo em muitos drivers. Solução:

- Para gPhoto2/IP/Phone: manter o backend streaming ativo é viável
- Para V4L2: o pipeline precisa ser pausado (PAUSED state, não NULL) ou compartilhar via v4l2loopback

#### 5.3 Diálogo de fechamento atualizado

Na hora de fechar o programa, o diálogo precisa listar todas as câmeras ativas:

```python
def _on_close(self, _window):
    active = self._session_manager.active_sessions
    if not active:
        self._cleanup_and_close()
        return False

    camera_list = "\n".join(f"• {s.camera.name}" for s in active)
    dialog = Adw.AlertDialog.new(
        _("Active cameras"),
        _("The following cameras are currently active:\n\n%s\n\n"
          "Choose what to do:") % camera_list,
    )
    dialog.add_response("stop-all", _("Stop all cameras and close"))
    dialog.add_response("keep-all", _("Keep all cameras on"))
    dialog.add_response("cancel", _("Cancel"))
```

#### 5.4 Persistência ao reabrir

Quando o usuário escolhe "Keep all cameras on" e reabre o BigCam:

1. Salvar lista de câmeras ativas em settings.json:
   ```json
   {
     "active_sessions": [
       {"camera_id": "v4l2:/dev/video0", "format": "1920x1080@30"},
       {"camera_id": "gphoto2:usb:001,005", "format": "1920x1080@30"}
     ]
   }
   ```

2. No `do_activate()`, verificar se há sessões salvas e reconectar:
   ```python
   saved = self._settings.get("active_sessions", [])
   for session in saved:
       camera = self._camera_manager.find_camera(session["camera_id"])
       if camera:
           self._session_manager.start_camera(camera)
   ```

#### 5.5 Indicador visual de câmeras ativas

No header bar ou sidebar, mostrar badges ou indicadores para câmeras que estão ativas em background:

```python
# No camera_selector.py, adicionar indicador visual
def _update_camera_list(self, cameras):
    for cam in cameras:
        row = self._make_camera_row(cam)
        if self._session_manager.is_active(cam.id):
            indicator = Gtk.Image.new_from_icon_name("media-playback-start-symbolic")
            indicator.add_css_class("success")
            row.add_suffix(indicator)
```

### Arquivos afetados

| Arquivo | Ação |
|---------|------|
| `core/session_manager.py` | **Novo** — gerenciador de sessões multi-câmera |
| `ui/window.py` | Integrar SessionManager, atualizar `_on_close()` |
| `ui/camera_selector.py` | Indicador de câmeras ativas |
| `core/stream_engine.py` | Suportar pause/resume sem destruir pipeline |
| `utils/settings_manager.py` | Persistir sessões ativas |

### Esforço: **Alto** — mudança arquitetural significativa

### Riscos e mitigações

| Risco | Mitigação |
|-------|-----------|
| USB bandwidth insuficiente para múltiplas câmeras | Limitar resolução das câmeras em background |
| Leak de pipelines GStreamer | Cleanup rigoroso com `atexit` + signal handlers |
| V4L2 exclusive access | Usar PAUSED state ou v4l2loopback intermediário |
| Consumo de memória | Monitorar e alertar se > 500MB |

---

## 6. Internacionalização

### Problema atual

A infraestrutura de i18n já existe (`utils/i18n.py` + 26 locales via gettext). Porém é necessário verificar se:

1. Todos os textos visíveis ao usuário usam `_()`
2. Não há strings hardcoded em inglês
3. Os templates `.pot` estão atualizados

### Verificação proposta

#### 6.1 Auditoria de strings

Executar `xgettext` para extrair todas as strings marcadas e comparar com as não marcadas:

```bash
# Extrair strings marcadas
xgettext --language=Python --keyword=_ --output=- usr/share/biglinux/bigcam/**/*.py | grep msgid | wc -l

# Encontrar strings potencialmente não marcadas
grep -rn '"[A-Z][a-z]' usr/share/biglinux/bigcam/ --include="*.py" | grep -v '_(' | grep -v '#' | grep -v 'log\.'
```

#### 6.2 Categorias de strings a verificar

| Categoria | Status esperado | Ação |
|-----------|----------------|------|
| Labels de botões | `_()` obrigatório | Verificar todos os `set_label()` |
| Tooltips | `_()` obrigatório | Verificar todos os `set_tooltip_text()` |
| Diálogos | `_()` obrigatório | Verificar `Adw.AlertDialog.new()` |
| Toasts/Notifications | `_()` obrigatório | Verificar `notify_user()` |
| Logs/debug | Não traduzir | Verificar que logs usam inglês puro |
| CSS classes | Não traduzir | OK |
| GStreamer pipelines | Não traduzir | OK |
| Nomes acessíveis | `_()` obrigatório | Verificar `set_accessible_name()` |

#### 6.3 Atualizar template .pot

```bash
cd usr/share/biglinux/bigcam
xgettext --language=Python \
    --keyword=_ \
    --output=../../../../locale/bigcam.pot \
    --package-name=BigCam \
    --package-version=3.0.0 \
    --copyright-holder="BigLinux" \
    **/*.py
```

#### 6.4 Strings dos novos recursos

Todos os textos adicionados neste plano devem usar `_()` desde o início:

```python
# Diálogo de conflito
_("Camera in use")
_("The camera '%s' is being used by: %s.")
_("Force close %s")
_("Enable Virtual Camera")
_("Camera settings will continue to work normally.")

# Botão phone
_("Phone as Webcam")
_("Use your phone as a webcam")
_("Phone connected — tap to configure")

# Multi-câmera
_("Active cameras")
_("Stop all cameras and close")
_("Keep all cameras on")

# Botão REC
_("Record Video")
_("Recording: %s")
```

### Arquivos afetados

| Arquivo | Ação |
|---------|------|
| Todos os `ui/*.py` | Auditoria de strings |
| `locale/bigcam.pot` | Regenerar template |
| `locale/*.po` | Atualizar com `msgmerge` |

### Esforço: Baixo-Médio

---

## 7. Performance e desempenho

### 7.1 Effects Pipeline — Otimizações prioritárias

#### 7.1.1 Background Blur — Face detection skip-frame

**Arquivo**: `core/effects.py:198-231`

Problema: Haar cascade roda em cada frame — O(n²) por frame.

```python
# Antes: detecta face em CADA frame
faces = detector.detectMultiScale(gray, 1.1, 5)

# Depois: detecta a cada N frames, interpola máscara
class EffectPipeline:
    _face_detect_interval: int = 5
    _face_frame_counter: int = 0
    _last_face_mask: np.ndarray | None = None

    def _background_blur(self, frame: np.ndarray, params: dict) -> np.ndarray:
        self._face_frame_counter += 1
        if self._face_frame_counter >= self._face_detect_interval or self._last_face_mask is None:
            self._face_frame_counter = 0
            self._last_face_mask = self._detect_face_mask(frame)
        # Usar _last_face_mask (reutilizado entre frames)
        blurred = cv2.GaussianBlur(frame, (21, 21), 0)
        return np.where(self._last_face_mask, frame, blurred)
```

**Ganho esperado**: ~5x menos CPU no background blur.

#### 7.1.2 Migrar de Haar Cascade para YuNet DNN

```python
# Haar (atual): ~15ms por frame, muitos falsos positivos
detector = cv2.CascadeClassifier("haarcascade_frontalface_default.xml")

# YuNet (proposta): ~5ms por frame, muito mais preciso
detector = cv2.FaceDetectorYN.create(
    model="face_detection_yunet_2023mar.onnx",
    config="",
    input_size=(320, 320),
    score_threshold=0.9,
)
```

O modelo YuNet vem incluído no OpenCV 4.13 — não precisa download externo.

#### 7.1.3 Denoise — Bilateral filter

```python
# Antes: ~30ms (MUITO lento)
result = cv2.fastNlMeansDenoisingColored(frame, None, h, h, 7, 21)

# Depois: ~3ms (10x mais rápido, qualidade aceitável)
result = cv2.bilateralFilter(frame, 9, strength, strength)
```

#### 7.1.4 LUT cache para Gamma e Color Maps

Já parcialmente implementado. Garantir que o LUT seja recalculado apenas quando o parâmetro muda:

```python
_gamma_lut: np.ndarray | None = None
_gamma_value: float = 1.0

def _gamma_correction(self, frame, params):
    gamma = params.get("gamma", 1.0)
    if gamma != self._gamma_value or self._gamma_lut is None:
        inv = 1.0 / gamma
        self._gamma_lut = np.array(
            [((i / 255.0) ** inv) * 255 for i in range(256)],
            dtype=np.uint8,
        )
        self._gamma_value = gamma
    return cv2.LUT(frame, self._gamma_lut)
```

### 7.2 GStreamer Pipeline — Otimizações

#### 7.2.1 Thread pool para videoconvert

Já usa `n-threads=2`. Aumentar conforme CPU disponível:

```python
import os
n_threads = min(os.cpu_count() or 2, 4)
f"videoconvert n-threads={n_threads}"
```

#### 7.2.2 Queue sizing

Otimizar buffers para reduzir latência sem causar drops:

```python
# Preview queue (prioridade: baixa latência)
"queue max-size-buffers=2 leaky=downstream silent=true"

# Virtual camera queue (prioridade: estabilidade)
"queue max-size-buffers=5 leaky=downstream silent=true"
```

#### 7.2.3 Hardware acceleration detection

Verificar disponibilidade de VA-API/NVENC para encoding do recorder:

```python
def _detect_hw_encoder() -> str:
    """Return best available H.264 encoder."""
    for encoder in ("vaapih264enc", "vah264enc", "nvh264enc"):
        factory = Gst.ElementFactory.find(encoder)
        if factory:
            return encoder
    return "x264enc tune=zerolatency speed-preset=ultrafast"
```

### 7.3 UI Thread — Never block

#### 7.3.1 Thumbnail generation async

**Arquivo**: `ui/video_gallery.py:200-217`

```python
# Antes: bloqueia UI thread com subprocess
thumbnail = subprocess.run(["ffmpeg", ...])

# Depois: background thread com placeholder
def _load_thumbnails_async(self):
    for video in videos:
        self._model.append(VideoItem(video, placeholder=True))
    run_async(self._generate_thumbnails, on_success=self._update_thumbnails)
```

#### 7.3.2 Debounce para controles

Slider changes disparam `set_control()` em cada tick. Adicionar debounce:

```python
def _on_slider_changed(self, scale, camera, control_id):
    if hasattr(self, '_slider_timeout'):
        GLib.source_remove(self._slider_timeout)
    self._slider_timeout = GLib.timeout_add(
        50,  # 50ms debounce
        lambda: self._apply_control(camera, control_id, scale.get_value()) or False,
    )
```

### 7.4 Settings I/O

#### 7.4.1 Atomic writes

```python
def _save(self) -> None:
    with self._lock:
        fd, tmp = tempfile.mkstemp(
            dir=os.path.dirname(self._path),
            suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self._data, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, self._path)
        except Exception:
            os.unlink(tmp)
            raise
```

#### 7.4.2 Settings write coalescing

Agrupar múltiplas escritas rápidas (ex: arrastar slider):

```python
_save_pending: bool = False

def set(self, key: str, value: Any) -> None:
    with self._lock:
        self._data[key] = value
        if not self._save_pending:
            self._save_pending = True
            GLib.timeout_add(500, self._flush)

def _flush(self) -> bool:
    with self._lock:
        self._save_pending = False
        self._save()
    return False  # don't repeat
```

### Resumo de ganhos esperados

| Componente | Antes | Depois | Ganho |
|-----------|-------|--------|-------|
| Background blur (face detect) | ~15ms/frame | ~3ms/frame (amortizado) | **5x** |
| Denoise | ~30ms/frame | ~3ms/frame | **10x** |
| Gamma LUT (recalc) | ~2ms/frame | ~0.01ms/frame (cached) | **200x** |
| Settings write | Síncrono, não-atômico | Assíncrono, atômico, coalesced | Seguro + rápido |
| Thumbnails | Bloqueia UI | Background thread | UI sem travamento |
| Slider controls | Cada tick→I/O | Debounce 50ms | Menos I/O |

### Arquivos afetados para performance

| Arquivo | Ação |
|---------|------|
| `core/effects.py` | Skip-frame, bilateral filter, LUT cache, YuNet |
| `core/stream_engine.py` | Thread pool dinâmico, queue sizing |
| `core/video_recorder.py` | HW encoder detection |
| `ui/video_gallery.py` | Thumbnail async |
| `ui/camera_controls_page.py` | Slider debounce |
| `utils/settings_manager.py` | Atomic writes, write coalescing, bool fix |

### Esforço: Médio

---

## 8. Bugs críticos

Estes bugs existentes (documentados em `PLANNING.md`) devem ser corrigidos em paralelo com as melhorias acima, pois afetam estabilidade e segurança:

### 8.1 Segurança

| Bug | Arquivo | Correção |
|-----|---------|----------|
| Shell injection em `pkill -f` | `gphoto2_backend.py:443` | Usar PID tracking em vez de pattern matching |
| Pipeline path injection | `video_recorder.py:157`, `stream_engine.py:294` | `shlex.quote()` em paths |
| IP backend URL split | `ip_backend.py:80` | Construir arg list sem split em input do usuário |

### 8.2 Integridade de dados

| Bug | Arquivo | Correção |
|-----|---------|----------|
| `bool("false") == True` | `settings_manager.py:56` | Parsing explícito de strings booleanas |
| JSON write não-atômico | `settings_manager.py:87` | tempfile + `os.replace()` |
| Thread-unsafe | `settings_manager.py` | `threading.Lock` em `_save()` |

### 8.3 Visual

| Bug | Arquivo | Correção |
|-----|---------|----------|
| Cores hardcoded quebram tema claro | `style.css:4,31` | Variáveis `@theme_*` |
| NV12 color space errado | `stream_engine.py:184` | Conversão NV12 correta |

---

## 9. Cronograma sugerido

### Fase 1 — Fundação (bugs + quick wins)
- [ ] Corrigir bugs críticos de segurança (§8.1)
- [ ] Corrigir bugs de integridade de dados (§8.2)
- [ ] Corrigir CSS para tema claro (§8.3)
- [ ] Botão REC estilizado (§3)
- [ ] Botão Phone as Webcam com texto (§4)
- [ ] Auditoria de i18n (§6)

### Fase 2 — UX principal
- [ ] Autenticação Virtual Cam via Polkit (§1)
- [ ] Diálogo de conflito com outros aplicativos (§2)
- [ ] Performance: effects pipeline (§7.1)
- [ ] Performance: settings atômico (§7.4)

### Fase 3 — Arquitetural
- [ ] Session Manager multi-câmera (§5)
- [ ] Pipeline pause/resume (§5.2)
- [ ] Persistência de sessões (§5.4)
- [ ] Performance: UI thread (§7.3)

### Fase 4 — Polimento
- [ ] Indicadores visuais de câmeras ativas (§5.5)
- [ ] Diálogo de fechamento multi-câmera (§5.3)
- [ ] Performance: GStreamer pipeline (§7.2)
- [ ] Atualizar traduções para novos recursos

---

## Considerações técnicas gerais

### Padrões a seguir

- **GTK4/libadwaita**: Seguir GNOME HIG. Usar `Adw.AlertDialog`, `Adw.StatusPage`, `Adw.Toast` conforme o contexto.
- **Threading**: Nunca bloquear a UI thread. Usar `GLib.idle_add()` para atualizar UI de threads secundárias.
- **GStreamer**: Preferir element property API sobre string interpolation em `Gst.parse_launch()`.
- **Logging**: Substituir todos os `print()` por `log.debug()`/`log.info()`. Nunca fazer `except: pass`.
- **i18n**: Todo texto visível deve usar `_()`. Strings de log/debug em inglês sem tradução.
- **XDG**: Config em `~/.config/bigcam/`, dados em `~/.local/share/bigcam/`, cache em `~/.cache/bigcam/`.
- **Acessibilidade**: Todo widget interativo deve ter `accessible-name`. Status visual deve ter alternativa textual.
- **Segurança**: `subprocess.run()` com listas, nunca `shell=True`. Sanitizar caminhos em pipelines GStreamer.

### Testes recomendados

Para cada melhoria implementada, verificar:

1. **Funcionalidade**: A feature funciona como esperado?
2. **Regressão**: Nenhuma funcionalidade existente foi quebrada?
3. **Tema**: Funciona tanto no tema claro quanto no escuro?
4. **Teclado**: Navegável por teclado?
5. **i18n**: Todas as strings novas estão marcadas com `_()`?
6. **Performance**: Não introduziu travamentos ou lag na UI?
7. **Wayland + X11**: Funciona em ambos?
