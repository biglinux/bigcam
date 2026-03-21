# BigCam — Redesign UX/UI Completo

## Visão Geral

Transformar o BigCam de um layout sidebar/paned em um **app de câmera moderno full-viewport**,
onde o feed da câmera preenche 100% da janela e todos os controles são overlays flutuantes
com auto-hide por inatividade. Referências: **Google Camera** (layout mobile adaptado para
desktop) e **big-media-player** (padrão GTK4 de auto-hide, sidebar overlay deslizante,
theming dinâmico).

---

## 1. Princípios Fundamentais

| # | Princípio | Aplicação |
|---|-----------|-----------|
| P1 | **Feed preenche tudo** | A imagem da câmera ocupa 100% da área visível, sem barras pretas laterais ou superiores. Usar `Gtk.ContentFit.COVER` ou escalar/crop. |
| P2 | **Controles sobre a imagem** | Todo controle visível é um overlay transparente sobre o feed — nunca ao lado dele. |
| P3 | **Auto-hide universal** | Após 2.5s sem interação, TODOS os overlays fazem fade-out suave (400ms). Restauração instantânea ao mover mouse/teclado. Cursor oculto quando imerso. |
| P4 | **Sidebar deslizante** | Configurações, efeitos e galerias vivem em um painel lateral overlay (como `AdwOverlaySplitView` do big-media-player) que desliza sobre o feed. |
| P5 | **Acesso direto** | Os controles mais usados (captura, gravação, modo, zoom, timer, espelho) ficam permanentemente acessíveis no overlay inferior — não escondidos em menus ou abas. |
| P6 | **Desktop-first com inspiração mobile** | Google Camera é referência de layout, mas adaptado para janela redimensionável, mouse, teclado e monitores widescreen. |

---

## 2. Hierarquia de Widgets — Estrutura Alvo

```
AdwApplicationWindow (BigDigicamWindow)
└── AdwToastOverlay (_toast_overlay)
    └── AdwOverlaySplitView (_split_view)
        │
        ├── [sidebar, position=end, hidden by default]
        │   GtkBox (sidebar_outer, HORIZONTAL)
        │   ├── GtkSeparator (drag_handle, 6px, cursor=col-resize)
        │   └── GtkBox (sidebar_content, VERTICAL, hexpand)
        │       ├── AdwHeaderBar (sidebar_header, flat)
        │       │   ├── [title] AdwViewSwitcher (sidebar_switcher, WIDE)
        │       │   └── [end] GtkButton (close_sidebar, "window-close-symbolic")
        │       └── AdwViewStack (_sidebar_stack, vexpand)
        │           ├── "controls"  → CameraControlsPage
        │           ├── "effects"   → EffectsPage
        │           ├── "photos"    → PhotoGallery
        │           ├── "videos"    → VideoGallery
        │           └── "settings"  → SettingsPage
        │
        └── [content]
            GtkOverlay (_main_overlay, overflow=HIDDEN)
            │
            ├── [child] GtkBox (_video_bg, hexpand, vexpand)
            │   └── classes: "video-bg"  → background: #000000
            │
            ├── [overlay] MirroredPicture (_picture)
            │   content_fit = Gtk.ContentFit.COVER
            │   hexpand=True, vexpand=True
            │   halign=FILL, valign=FILL
            │   measure_overlay=False, clip_overlay=True
            │
            ├── [overlay] GtkRevealer (_top_bar_revealer)
            │   transition=CROSSFADE, duration=300ms
            │   valign=START, halign=FILL
            │   └── GtkBox (_top_bar, HORIZONTAL, classes: "osd-header")
            │       ├── [start] CameraSelector (_camera_selector)
            │       ├── [center] GtkBox (status_indicators)
            │       │   ├── GtkLabel (_rec_timer, "00:00", hidden)
            │       │   └── GtkLabel (_photo_count_label)
            │       └── [end] GtkBox (top_actions)
            │           ├── GtkButton (flash_btn, hidden if no flash)
            │           ├── GtkButton (timer_btn, "timer-symbolic")
            │           │   └── cycles: Off → 3s → 5s → 10s
            │           ├── GtkButton (grid_btn, "view-grid-symbolic")
            │           └── GtkButton (settings_btn, "open-menu-symbolic")
            │               → abre sidebar na aba "settings"
            │
            ├── [overlay] GtkDrawingArea (_grid_drawing)
            │   can_target=False, hexpand/vexpand=True
            │   (rule-of-thirds grid, só visível quando habilitado)
            │
            ├── [overlay] GtkBox (_zoom_indicator)
            │   halign=CENTER, valign=CENTER (ligeiramente acima do centro)
            │   visible=False, auto-hide após 1.5s
            │   └── GtkLabel ("1.0x" / "1.5x" / "2.0x")
            │
            ├── [overlay] GtkLabel (_countdown_label)
            │   halign=CENTER, valign=CENTER
            │   classes: "countdown-overlay"
            │   visible=False (aparece durante countdown)
            │
            ├── [overlay] GtkRevealer (_bottom_bar_revealer)
            │   transition=CROSSFADE, duration=300ms
            │   valign=END, halign=FILL
            │   └── GtkBox (_bottom_zone, VERTICAL, classes: "osd-controls")
            │
            │       ├── GtkBox (_mode_switcher, HORIZONTAL, halign=CENTER)
            │       │   classes: "mode-switcher"
            │       │   └── [GtkToggleButton × N, group linked]
            │       │       "Photo"  "Video"  "Portrait"  "Panorama" ...
            │       │       (estilo: texto flat, selecionado = pill highlight)
            │       │
            │       └── GtkCenterBox (_controls_bar)
            │           ├── [start] GtkBox (controls_start)
            │           │   ├── GtkButton (_last_photo_btn)
            │           │   │   └── GtkPicture (thumbnail 48×48, circular clip)
            │           │   └── GtkButton (_mirror_btn, flat circular)
            │           │       icon: "object-flip-horizontal-symbolic"
            │           │
            │           ├── [center] GtkBox (controls_center, HORIZONTAL)
            │           │   ├── GtkButton (_capture_btn)
            │           │   │   classes: "capture-button"
            │           │   │   → 64×64 círculo, borda branca 3px
            │           │   │   → interno: círculo branco preenchido
            │           │   │   → no modo vídeo: círculo vermelho
            │           │   └── GtkButton (_record_btn, hidden no modo foto)
            │           │       → 48×48, borda branca, dot vermelho interno
            │           │
            │           └── [end] GtkBox (controls_end)
            │               ├── GtkButton (_zoom_btn, flat circular)
            │               │   → "1x" label, cycles zoom levels
            │               └── GtkButton (_sidebar_btn, flat circular)
            │                   icon: "sidebar-show-right-symbolic"
            │                   → toggle sidebar (controles/efeitos/galeria)
            │
            ├── [overlay] GtkBox (_audio_box)
            │   halign=START, valign=TOP, margin 8px
            │   (indicador de áudio, mesmo design atual)
            │
            ├── [overlay] GtkLabel (_fps_label)
            │   halign=END, valign=TOP, margin 8px
            │   classes: "osd fps-label"
            │
            ├── [overlay] AdwBanner (_banner)
            │   valign=TOP (abaixo do top_bar)
            │   (mensagens persistentes: "Gravando...", erros, etc.)
            │
            └── [overlay] GtkBox (_status_overlay)
                halign=CENTER, valign=CENTER
                └── AdwStatusPage ("No camera" / "Connection failed")
                    (só visível quando NÃO há câmera ativa)
```

---

## 3. Feed Full-Viewport

### 3.1 Preenchimento Total

**Situação atual**: `MirroredPicture` usa `ContentFit.CONTAIN` → barras pretas.

**Alvo**: O feed da câmera preenche 100% da área do `_main_overlay`.

**Implementação**:

```python
# Opção A: COVER (recorta bordas para preencher)
self._picture.set_content_fit(Gtk.ContentFit.COVER)

# Opção B: FILL (estica — NÃO recomendado, distorce)
# self._picture.set_content_fit(Gtk.ContentFit.FILL)
```

`ContentFit.COVER` é o padrão para apps de câmera: mantém aspect ratio,
recorta o que sobrar nas bordas. A câmera sempre preenche toda a área visível.

### 3.2 Background Preto Puro

Como no big-media-player, o fundo atrás do vídeo é **preto puro** (`#000000`),
não cinza escuro (`#1a1a1a`). Isso garante que durante transições ou quando
nenhuma câmera está ativa, o fundo é completamente escuro.

```css
.video-bg {
    background-color: #000000;
}

window.bigcam {
    background-color: #000000;
}
```

### 3.3 measure_overlay e clip_overlay

Seguindo o padrão do big-media-player:

```python
self._main_overlay.set_measure_overlay(self._picture, False)
self._main_overlay.set_clip_overlay(self._picture, True)
```

Isso impede que o `MirroredPicture` influencie o tamanho do overlay e garante
recorte correto.

---

## 4. Layout dos Overlays

### 4.1 Top Bar (Barra Superior)

Inspirada no Google Camera:

```
┌──────────────────────────────────────────────────────────┐
│ [📷 Camera ▾]          [152 📷]         [⏱] [⊞] [≡]    │
└──────────────────────────────────────────────────────────┘
```

- **Esquerda**: Seletor de câmera (dropdown compacto)
- **Centro**: Contador de fotos da sessão + indicador de gravação (quando ativo: `● 00:32`)
- **Direita**: Timer (Off/3s/5s/10s toggle cíclico), Grid (toggle), Menu/Settings (abre sidebar)

**CSS**:
```css
.osd-header {
    background: linear-gradient(to bottom,
        rgba(0, 0, 0, 0.55) 0%,
        transparent 100%);
    padding: 8px 16px;
    /* Gradiente escuro no topo para legibilidade */
}
```

### 4.2 Bottom Bar (Zona Inferior)

Inspirada no Google Camera:

```
┌──────────────────────────────────────────────────────────┐
│            Portrait  [Photo]  Video  Panorama            │
│                                                          │
│   [🖼]  [⇔]          [⊙ CAPTURE ⊙]        [1x]  [☰]   │
└──────────────────────────────────────────────────────────┘
```

**Linha 1 — Mode Switcher**:
- Botões de texto flat em linha horizontal, centralizados
- O modo ativo tem um pill/chip highlight arredondado
- Swipe horizontal no desktop = scroll (ou arrastar)
- Modos disponíveis: **Photo**, **Video**, **Portrait** (se OpenCV disponível),
  **Panorama** (futuro)

**Linha 2 — Controls Bar** (GtkCenterBox):
- **Esquerda**:
  - Thumbnail circular da última foto capturada (click = abrir)
  - Botão espelho (flip horizontal)
- **Centro**:
  - Botão de captura grande (64×64 círculo)
    - Modo foto: circle branco com borda
    - Modo vídeo: circle vermelho
    - Durante gravação: quadrado (stop) no hover
  - Botão de gravação (modo foto: escondido; modo vídeo: visível)
- **Direita**:
  - Botão zoom (mostra nível atual: 1x, 1.5x, 2x)
  - Botão sidebar (abre painel lateral)

**CSS**:
```css
.osd-controls {
    background: linear-gradient(to top,
        rgba(0, 0, 0, 0.55) 0%,
        transparent 100%);
    padding: 12px 16px 16px 16px;
}
```

### 4.3 Mode Switcher

```css
.mode-switcher button {
    background: none;
    border: none;
    color: rgba(255, 255, 255, 0.7);
    padding: 6px 14px;
    font-weight: 600;
    font-size: 0.9em;
    border-radius: 20px;
    transition: all 200ms ease;
}

.mode-switcher button:checked {
    background-color: rgba(255, 255, 255, 0.15);
    color: #ffffff;
}
```

### 4.4 Capture Button

O botão de captura segue o padrão visual do Google Camera:

```css
.capture-button {
    min-width: 64px;
    min-height: 64px;
    border-radius: 9999px;
    border: 4px solid rgba(255, 255, 255, 0.9);
    background-color: rgba(255, 255, 255, 0.85);
    transition: all 150ms ease;
    padding: 0;
}

.capture-button:hover {
    background-color: #ffffff;
    border-color: #ffffff;
}

.capture-button:active {
    min-width: 58px;
    min-height: 58px;
    /* Slight shrink on press */
}

/* Modo vídeo: botão vermelho */
.capture-button.video-mode {
    background-color: @error_color;
    border-color: rgba(255, 255, 255, 0.9);
}

/* Durante gravação: transforma em quadrado */
.capture-button.recording {
    border-radius: 8px;
    min-width: 32px;
    min-height: 32px;
    background-color: @error_color;
}
```

---

## 5. Sidebar Overlay (AdwOverlaySplitView)

### 5.1 Migração de GtkPaned para AdwOverlaySplitView

**Situação atual**: `Gtk.Paned(HORIZONTAL)` divide a janela em preview + sidebar,
tomando espaço permanente.

**Alvo**: `Adw.OverlaySplitView` com sidebar à direita, normalmente oculta,
que desliza sobre o feed quando ativada.

```python
self._split_view = Adw.OverlaySplitView(
    show_sidebar=False,          # Oculta por padrão
    sidebar_position=Gtk.PackType.END,  # Desliza da direita
    max_sidebar_width=400,
    min_sidebar_width=280,
    sidebar_width_fraction=0.35,
    collapsed=True,              # Sempre overlay, nunca side-by-side
)
```

### 5.2 Drag Handle Redimensionável

Seguindo o big-media-player, adicionar um `GtkSeparator` com `GestureDrag`
para permitir redimensionamento da sidebar:

```python
drag_handle = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
drag_handle.set_size_request(6, -1)
drag_handle.set_cursor(Gdk.Cursor.new_from_name("col-resize"))

gesture = Gtk.GestureDrag()
gesture.connect("drag-update", self._on_sidebar_drag)
drag_handle.add_controller(gesture)
```

### 5.3 Sidebar Header

Cada aba da sidebar tem seu conteúdo como hoje, mas o header da sidebar usa
`AdwViewSwitcher` em modo WIDE (ícones + texto inline):

```
┌──────────────────────────────────────────┐
│ [📷 Controls] [🎨 Effects] [🖼 Photos]  │
│ [🎥 Videos] [⚙ Settings]      [✕ Close] │
├──────────────────────────────────────────┤
│                                          │
│      (conteúdo da aba ativa)             │
│                                          │
└──────────────────────────────────────────┘
```

### 5.4 Interação com Auto-Hide

Quando a sidebar está aberta:
- O ImmersionController é **inibido** (UI não se esconde)
- Mouse sobre a sidebar mantém tudo visível
- Fechar a sidebar desinibe e reinicia o timer de inatividade

```python
def _on_sidebar_toggled(self, split_view, _pspec):
    if split_view.get_show_sidebar():
        self._immersion.inhibit()
    else:
        self._immersion.uninhibit()
```

---

## 6. Auto-Hide (ImmersionController) — Refinamentos

### 6.1 Escopo do Auto-Hide

Widgets gerenciados pelo auto-hide:
- `_top_bar_revealer` → Revealer slide/crossfade
- `_bottom_bar_revealer` → Revealer slide/crossfade
- `_fps_label` → opacity fade
- `_audio_box` → opacity fade
- `_grid_drawing` → opacity fade (se visível)
- Cursor do mouse → `none` quando imerso

Widgets **NÃO** afetados:
- `_status_overlay` → Visível quando sem câmera (o feed já é o foco)
- `_countdown_label` → Controlado pelo timer de captura
- `_banner` → Mensagens urgentes que não devem desaparecer
- Sidebar → Controlada separadamente (inibe auto-hide quando aberta)

### 6.2 Condições de Inibição

O auto-hide NÃO acontece quando:
1. Gravação em andamento (`is_recording`)
2. Countdown ativo (`is_countdown_active`)
3. Sidebar aberta (`split_view.get_show_sidebar()`)
4. Qualquer popover visível (dropdown câmera, menus)
5. Qualquer diálogo aberto (IP cam, phone, QR, about)
6. Playback está pausado (se aplicável)

### 6.3 Parâmetros

| Parâmetro | Valor | Justificativa |
|-----------|-------|---------------|
| Delay de inatividade | 2500ms | Padrão big-media-player (2.5s) |
| Duração do fade-out | 400ms | Suave mas rápido |
| Curva de easing | ease-out quad | Desacelera naturalmente |
| Delay de show | 0ms | Instantâneo (critical path) |
| Cursor escondido | Sempre quando imerso | Não apenas fullscreen |
| Tipo de transição dos Revealers | CROSSFADE | Como big-media-player, não SLIDE |

### 6.4 Fullscreen

- `F11` ou botão fullscreen alterna tela cheia
- Em fullscreen, o comportamento é idêntico — os overlays fazem auto-hide
- A diferença é que a window decoration (titlebar do WM) desaparece
- O cursor é oculto quando imerso (em fullscreen e janela normal)
- `Esc` sai do fullscreen

---

## 7. Adaptação dos Controles Existentes

### 7.1 Controles que Saem da Sidebar para o Overlay

Os controles mais usados migram da sidebar para overlays diretos:

| Controle | Antes (sidebar) | Depois (overlay) |
|----------|-----------------|-------------------|
| Capture timer | Settings → ComboRow | Top bar → toggle button (Off/3s/5s/10s) |
| Grid overlay | Settings → SwitchRow | Top bar → toggle button |
| Mirror preview | Settings → SwitchRow | Bottom bar → toggle button |
| Resolution | Settings → ComboRow | **Mantém na sidebar** (raramente alterado) |
| FPS limit | Settings → ComboRow | **Mantém na sidebar** |
| Zoom | Não existia | Bottom bar → toggle button com níveis |
| Modo (foto/vídeo) | Não existia | Bottom bar → mode switcher |

### 7.2 Controles que Permanecem na Sidebar

Tudo que é configuração avançada ou raramente alterado:
- **Controls**: Ajustes V4L2 (brilho, contraste, exposição, foco, etc.)
- **Effects**: Pipeline de efeitos OpenCV (18 efeitos com parâmetros)
- **Photos/Videos**: Galerias de mídia capturada
- **Settings**: Diretórios, tema, câmera virtual, QR scanner, sorriso

### 7.3 Mode Switcher — Comportamento por Modo

**Modo Photo**:
- Botão de captura: círculo branco (foto)
- Botão de gravação: escondido
- Timer: visível no top bar
- Ao capturar: animação de flash (overlay branco 100ms)

**Modo Video**:
- Botão de captura: muda para círculo vermelho
- Click inicia/para gravação
- Durante gravação:
  - `_rec_timer` visível no center do top bar ("● 00:32")
  - Botão vira quadrado (stop)
  - Auto-hide INIBIDO
  - Pulsação da borda vermelha no botão

**Modo Portrait** (se OpenCV disponível):
- Background Blur ativado automaticamente
- Botão de captura: círculo branco
- Controles de intensidade acessíveis via sidebar

---

## 8. CSS — Estilo Completo

### 8.1 Filosofia

Seguir o big-media-player: estilos estruturais em CSS estático,
cores em gradientes semi-transparentes sobre o feed.

Não usar cores hardcoded que conflitem com o tema Adwaita.
Os overlays usam `rgba(0,0,0,0.55)` com gradiente para legibilidade
sem bloquear a visão do feed.

### 8.2 Stylesheet Completa

```css
/* === BigCam Redesign — Full Viewport Camera App === */

/* --- Global --- */
window.bigcam {
    background-color: #000000;
}

.video-bg {
    background-color: #000000;
}

/* --- Top Bar Overlay --- */
.osd-header {
    background: linear-gradient(to bottom,
        rgba(0, 0, 0, 0.55) 0%,
        rgba(0, 0, 0, 0.25) 70%,
        transparent 100%);
    padding: 8px 16px;
}

.osd-header button {
    background: transparent;
    border: none;
    box-shadow: none;
    color: rgba(255, 255, 255, 0.9);
    min-width: 36px;
    min-height: 36px;
    -gtk-icon-size: 20px;
    border-radius: 9999px;
    transition: background 150ms ease;
}

.osd-header button:hover {
    background: rgba(255, 255, 255, 0.12);
}

.osd-header button:active {
    background: rgba(255, 255, 255, 0.20);
}

/* --- Bottom Controls Overlay --- */
.osd-controls {
    background: linear-gradient(to top,
        rgba(0, 0, 0, 0.55) 0%,
        rgba(0, 0, 0, 0.25) 70%,
        transparent 100%);
    padding: 12px 16px 20px 16px;
}

/* --- Mode Switcher --- */
.mode-switcher {
    margin-bottom: 12px;
}

.mode-switcher button {
    background: none;
    border: none;
    box-shadow: none;
    color: rgba(255, 255, 255, 0.65);
    padding: 6px 16px;
    font-weight: 600;
    font-size: 0.9em;
    border-radius: 20px;
    transition: all 200ms ease;
    min-height: 0;
}

.mode-switcher button:checked {
    background-color: rgba(255, 255, 255, 0.15);
    color: #ffffff;
}

.mode-switcher button:hover:not(:checked) {
    color: rgba(255, 255, 255, 0.85);
}

/* --- Capture Button --- */
.capture-button {
    min-width: 64px;
    min-height: 64px;
    border-radius: 9999px;
    border: 4px solid rgba(255, 255, 255, 0.9);
    background-color: rgba(255, 255, 255, 0.85);
    transition: all 150ms ease;
    padding: 0;
    box-shadow: 0 2px 8px rgba(0, 0, 0, 0.3);
}

.capture-button:hover {
    background-color: #ffffff;
    border-color: #ffffff;
    box-shadow: 0 2px 12px rgba(0, 0, 0, 0.4);
}

.capture-button:active {
    min-width: 58px;
    min-height: 58px;
}

.capture-button.video-mode {
    background-color: @error_color;
    border-color: rgba(255, 255, 255, 0.9);
}

.capture-button.recording {
    border-radius: 8px;
    min-width: 36px;
    min-height: 36px;
    background-color: @error_color;
    animation: rec-pulse 1.2s ease-in-out infinite;
}

@keyframes rec-pulse {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.7; }
}

/* --- Record Button (Video mode secondary) --- */
.record-button {
    min-width: 48px;
    min-height: 48px;
    border-radius: 9999px;
    border: 3px solid rgba(255, 255, 255, 0.8);
    background: transparent;
    padding: 0;
    transition: all 150ms ease;
}

.record-button .rec-dot {
    min-width: 20px;
    min-height: 20px;
    border-radius: 9999px;
    background-color: @error_color;
}

/* --- Last Photo Thumbnail --- */
.last-photo-btn {
    min-width: 48px;
    min-height: 48px;
    border-radius: 9999px;
    padding: 0;
    border: 2px solid rgba(255, 255, 255, 0.6);
    overflow: hidden;
}

.last-photo-btn picture {
    border-radius: 9999px;
}

/* --- Controls Bar Buttons --- */
.controls-bar button.flat {
    color: rgba(255, 255, 255, 0.9);
    min-width: 44px;
    min-height: 44px;
    -gtk-icon-size: 22px;
    border-radius: 9999px;
    transition: background 150ms ease;
}

.controls-bar button.flat:hover {
    background: rgba(255, 255, 255, 0.12);
}

/* --- Zoom Button --- */
.zoom-btn {
    font-weight: 700;
    font-size: 0.85em;
    color: rgba(255, 255, 255, 0.9);
    background: rgba(0, 0, 0, 0.4);
    border-radius: 9999px;
    min-width: 40px;
    min-height: 40px;
    padding: 0 8px;
    border: 1.5px solid rgba(255, 255, 255, 0.3);
}

/* --- FPS Label --- */
.fps-label {
    padding: 4px 8px;
    border-radius: 6px;
    font-size: 0.85em;
    font-variant-numeric: tabular-nums;
    background: rgba(0, 0, 0, 0.5);
    color: rgba(255, 255, 255, 0.8);
}

/* --- Audio Overlay --- */
.audio-overlay {
    background: rgba(0, 0, 0, 0.5);
    border-radius: 8px;
    padding: 6px 10px;
}

/* --- Countdown --- */
.countdown-overlay {
    font-size: 96px;
    font-weight: 800;
    color: #ffffff;
    text-shadow: 0 2px 16px rgba(0, 0, 0, 0.7);
}

/* --- Recording Timer --- */
.rec-timer {
    color: @error_color;
    font-weight: 700;
    font-variant-numeric: tabular-nums;
    font-size: 0.95em;
}

.rec-timer .rec-indicator {
    min-width: 8px;
    min-height: 8px;
    border-radius: 9999px;
    background-color: @error_color;
    margin-end: 6px;
    animation: rec-blink 1s step-end infinite;
}

@keyframes rec-blink {
    0%, 100% { opacity: 1; }
    50% { opacity: 0; }
}

/* --- Status Page (no camera) --- */
.status-overlay statuspage {
    background: transparent;
    color: rgba(255, 255, 255, 0.7);
}

/* --- Grid Overlay --- */
.grid-drawing {
    opacity: 0.3;
}

/* --- Sidebar (AdwOverlaySplitView) --- */
.sidebar-drag-handle {
    min-width: 6px;
    background: alpha(@borders, 0.3);
    cursor: col-resize;
}

.sidebar-drag-handle:hover {
    background: alpha(@accent_color, 0.5);
}
```

---

## 9. Atalhos de Teclado

| Atalho | Ação |
|--------|------|
| `Space` / `Enter` | Capturar foto (modo foto) / Start/Stop gravação (modo vídeo) |
| `Ctrl+R` | Toggle gravação |
| `Ctrl+S` | Salvar perfil de câmera |
| `Ctrl+L` | Carregar perfil de câmera |
| `Ctrl+M` | Toggle espelho |
| `Ctrl+G` | Toggle grid |
| `Ctrl+T` | Cycle timer (Off → 3s → 5s → 10s → Off) |
| `F11` | Toggle fullscreen |
| `Esc` | Sair do fullscreen / Fechar sidebar |
| `1` | Zoom 1x |
| `2` | Zoom 1.5x |
| `3` | Zoom 2x |
| `Tab` | Mostrar/ocultar sidebar |
| `Ctrl+1..5` | Trocar aba da sidebar |
| `Ctrl+Q` | Fechar aplicação |

---

## 10. Fluxo de Interação

### 10.1 Primeira Abertura

1. Janela abre com feed ocupando 100% da área
2. Top bar e bottom bar visíveis (overlays semi-transparentes)
3. Se nenhuma câmera: `AdwStatusPage` centralizado sobre fundo preto
4. Se câmera auto-detectada: feed inicia imediatamente
5. Após 2.5s sem interação: overlays fazem fade-out → tela limpa

### 10.2 Captura de Foto

1. Usuário move mouse → UI restaura instantaneamente
2. Clica no botão de captura (ou `Space`)
3. Se timer ativo (3s/5s/10s):
   - Countdown overlay aparece (3... 2... 1...)
   - Auto-hide inibido durante countdown
4. Flash overlay branco (100ms, 80% opacidade)
5. Thumbnail da última foto atualiza no canto inferior esquerdo
6. Toast breve: "Photo saved"
7. Timer de auto-hide reinicia

### 10.3 Gravação de Vídeo

1. Selecionar modo "Video" no mode switcher
2. Botão de captura muda para vermelho
3. Click inicia gravação:
   - `_rec_timer` aparece no top bar center: `● 00:00`
   - Auto-hide **inibido** (controles permanecem visíveis)
   - Borda do botão pulsa vermelho
4. Click novamente → para gravação
   - Timer desaparece
   - Toast: "Video saved: filename.mkv"
   - Auto-hide desinibido

### 10.4 Abrir Sidebar

1. Clicar no botão sidebar (bottom-right) ou `Tab`
2. `AdwOverlaySplitView` desliza sidebar da direita
3. Auto-hide **inibido** enquanto sidebar aberta
4. Navegar entre abas: Controls, Effects, Photos, Videos, Settings
5. `Esc` ou botão ✕ fecha a sidebar
6. Auto-hide desinibido

---

## 11. Animações

| Elemento | Tipo | Duração | Easing |
|----------|------|---------|--------|
| Top/Bottom bar fade-out | Revealer crossfade | 400ms | ease-out |
| Top/Bottom bar show | Revealer crossfade | 0ms | instantâneo |
| Sidebar open/close | OverlaySplitView slide | 250ms | ease-out (Adw default) |
| Mode switcher highlight | CSS transition | 200ms | ease |
| Capture button press | CSS min-width | 150ms | ease |
| Flash overlay | opacity 0.8 → 0 | 100ms | linear |
| Countdown number | scale 1.5 → 1.0, opacity 0 → 1 | 300ms | ease-out |
| Recording pulse | opacity 1 → 0.7 → 1 | 1200ms | ease-in-out, infinite |
| Recording timer blink | opacity step | 1000ms | step-end |
| Zoom indicator | opacity appear + auto-dismiss | 200ms + 1500ms delay | ease |
| FPS/Audio fade | set_opacity() | 400ms (10 steps) | ease-out quad |

---

## 12. Responsividade

### 12.1 Breakpoints (AdwBreakpoint, se disponível)

| Largura | Comportamento |
|---------|---------------|
| ≥ 900px | Mode switcher com texto completo, sidebar até 400px |
| 600–899px | Mode switcher compacto (3-4 modos visíveis), sidebar 280px |
| < 600px | Mode switcher com ícones apenas, controles empilhados, sidebar fullscreen |

### 12.2 Sidebar Redimensionamento

- Min: 280px
- Max: 50% da janela (ou 500px, o que for menor)
- Drag handle visial (6px separator com cursor col-resize)
- Em janelas < 600px: sidebar ocupa tela inteira (collapsed=True forced)

---

## 13. Comparação Antes × Depois

| Aspecto | Antes | Depois |
|---------|-------|--------|
| Feed da câmera | `ContentFit.CONTAIN`, barras pretas, ocupa ~60% da janela | `ContentFit.COVER`, preenche 100% da janela |
| Sidebar | Permanente, sempre visível (GtkPaned) | Overlay deslizante, oculta por padrão (OverlaySplitView) |
| Controles | Escondidos em 5 abas da sidebar | Sobrepostos no feed (top + bottom bar) |
| Header bar | Adwaita padrão com titlebar | Substituída por top bar transparente sobre o feed |
| Captura | Botão na toolbar pequena flutuante | Botão grande centralizado (64px, Google Camera style) |
| Timer | ComboRow na aba Settings | Toggle button direto no top bar |
| Grid | SwitchRow na aba Settings | Toggle button direto no top bar |
| Espelho | SwitchRow na aba Settings | Toggle button na barra inferior |
| Modos foto/vídeo | Sem seletor (funcionalidades misturadas) | Mode switcher visual no bottom bar |
| Galeria | Tab na sidebar sempre visível | Tab na sidebar overlay (mostrada sob demanda) |
| Background | `#1a1a1a` (cinza escuro) | `#000000` (preto puro) |
| Auto-hide | Implementado (Revealer + opacity) | Refinado (Crossfade em Revealers, gradientes CSS) |

---

## 14. Migração da Estrutura Paned → OverlaySplitView

### 14.1 Passos de Implementação

1. **Substituir `Gtk.Paned` por `Adw.OverlaySplitView`** em `window.py`
2. **Mover `PreviewArea`** para ser conteúdo direto do overlay principal (não mais start_child do paned)
3. **Reestruturar PreviewArea**: Remover a stack interna status/preview e colocar o `MirroredPicture` diretamente como filho do `_main_overlay`
4. **Criar top bar e bottom bar** como overlays no `_main_overlay`
5. **Mover controles frequentes** (timer, grid, mirror) da sidebar para os overlays
6. **Adaptar ImmersionController** para os novos Revealers (top + bottom, crossfade)
7. **Remover `_header_revealer`** (não há mais AdwHeaderBar — o top bar é um overlay custom)
8. **Manter sidebar content** (CameraControlsPage, EffectsPage, etc.) inalterado
9. **Adicionar AdwViewSwitcher** no header da sidebar (ao invés do ViewSwitcherBar no bottom)
10. **CSS**: Substituir stylesheet completa com o design de gradientes

### 14.2 Impacto em Outros Arquivos

| Arquivo | Mudança |
|---------|---------|
| `ui/window.py` | Reestruturação major (Paned → OverlaySplitView + Overlay) |
| `ui/preview_area.py` | Simplificação (Picture + status movidos; toolbar substituída) |
| `ui/immersion.py` | Adaptar para Revealers crossfade ao invés de Revealer slide + opacity |
| `ui/camera_selector.py` | Mover para o top bar overlay |
| `style.css` | Substituição completa |
| Sidebar pages | Sem mudança (permanecem no ViewStack) |
| `core/*` | Sem mudança (lógica de negócio inalterada) |

---

## 15. Referências Visuais

### 15.1 Google Camera (Android)

- Feed ocupa 100% da viewport
- Top: engrenagem, contador de fotos, flash — sobre gradiente escuro
- Bottom: mode switcher (labels com pill ativo) + shutter button grande
- Settings: painel flutuante que desce sobre o feed
- Gravação: botão vermelho com timer, botão pause
- Modo Pro: dials de exposição/foco no bottom com valores no top

### 15.2 Big Media Player (GTK4/Rust)

- Vídeo preenche 100% via GLArea no GtkOverlay
- `AdwOverlaySplitView` para playlist lateral
- Auto-hide com `HIDE_DELAY_MS = 2500`, crossfade 300ms
- Guard: pausa, popovers, dialogs inibem hide
- Header + controls como overlays com `measure_overlay=false`
- CSS dinâmico para theming (12 presets, custom colors)
- Drag handle redimensionável na sidebar (200–800px)
- Button zones configuráveis (5 zonas, 13 botões)

---

## 16. Prioridades de Implementação

| Fase | Scope | Esforço |
|------|-------|---------|
| **Fase 1** | Feed full-viewport (`COVER` + background preto) | Pequeno |
| **Fase 2** | Paned → OverlaySplitView, sidebar overlay | Médio |
| **Fase 3** | Top bar + bottom bar overlays com controles | Grande |
| **Fase 4** | Mode switcher (Photo/Video) + capture button redesign | Médio |
| **Fase 5** | CSS gradientes + theming | Médio |
| **Fase 6** | Refinamentos: zoom, last photo thumbnail, responsividade | Pequeno |
| **Fase 7** | ImmersionController adaptação final | Pequeno |
