# OpenCV Improvements — BigCam

Recursos do OpenCV 4.13.0 disponíveis no sistema (pacote `opencv` do Arch/BigLinux) que podem ser integrados ao BigCam como efeitos em tempo real na webcam.

## Versão instalada

- **OpenCV**: 4.13.0 com módulos extras (`opencv_contrib`)
- **Python binding**: `cv2` com 2756 símbolos exportados
- **Aceleração**: SSE4.1, SSE4.2, AVX, AVX2, AVX512

---

## 1. Efeitos Visuais em Tempo Real

### 1.1 Background Blur (Desfoque de Fundo / Bokeh)

- **Funções**: `cv2.GaussianBlur`, `cv2.bilateralFilter`, `cv2.medianBlur`
- **Descrição**: Desfoca o fundo mantendo o rosto/corpo nítido, simulando efeito bokeh de câmera DSLR. Amplamente usado em apps de videoconferência (Zoom, Teams, Meet).
- **Implementação**: Segmentar pessoa com face detection (YuNet) + máscara + blur no fundo.
- **Parâmetros ajustáveis**: Intensidade do desfoque (kernel size)
- **Prioridade**: ⭐⭐⭐ Alta — recurso mais pedido em apps de webcam
- **Performance**: Baixo custo (GaussianBlur ~1ms por frame em 1080p)

### 1.2 Substituição de Fundo (Virtual Background)

- **Funções**: `cv2.BackgroundSubtractorMOG2`, `cv2.BackgroundSubtractorKNN`, segmentação DNN
- **Descrição**: Substitui o fundo por uma imagem ou cor sólida. Essencial para videoconferência.
- **Implementação**: Background subtraction + máscara + composição com imagem escolhida pelo usuário
- **Parâmetros ajustáveis**: Imagem de fundo, sensibilidade da segmentação
- **Prioridade**: ⭐⭐⭐ Alta — complemento do background blur
- **Performance**: MOG2/KNN ~3ms, DNN ~15ms por frame

### 1.3 Pencil Sketch (Desenho a Lápis)

- **Função**: `cv2.pencilSketch(src, sigma_s, sigma_r, shade_factor)`
- **Descrição**: Transforma a imagem em um desenho a lápis/carvão em tempo real. Efeito artístico.
- **Parâmetros ajustáveis**: `sigma_s` (suavidade), `sigma_r` (contraste), `shade_factor` (sombreamento)
- **Prioridade**: ⭐⭐ Média — efeito divertido/artístico
- **Performance**: ~10ms por frame

### 1.4 Stylization (Efeito Pintura)

- **Função**: `cv2.stylization(src, sigma_s, sigma_r)`
- **Descrição**: Transforma a imagem em estilo pintura a óleo / aquarela. Efeito mais refinado que pencil sketch.
- **Parâmetros ajustáveis**: `sigma_s` (tamanho do filtro), `sigma_r` (alcance do filtro)
- **Prioridade**: ⭐⭐ Média — efeito artístico popular
- **Performance**: ~12ms por frame

### 1.5 Detail Enhance (Realce de Detalhes)

- **Função**: `cv2.detailEnhance(src, sigma_s, sigma_r)`
- **Descrição**: Realça detalhes e texturas da imagem, tornando a webcam mais nítida e definida. Útil para câmeras de baixa qualidade.
- **Parâmetros ajustáveis**: `sigma_s` (tamanho), `sigma_r` (intensidade)
- **Prioridade**: ⭐⭐⭐ Alta — melhora qualidade percebida da webcam
- **Performance**: ~8ms por frame

### 1.6 Edge Preserving Filter (Suavização com Preservação de Bordas)

- **Função**: `cv2.edgePreservingFilter(src, flags, sigma_s, sigma_r)`
- **Descrição**: Suaviza a pele/ruído mantendo bordas e detalhes nítidos. Efeito "beauty cam" / soft skin.
- **Parâmetros ajustáveis**: Modo (recursivo/normalizado), `sigma_s`, `sigma_r`
- **Prioridade**: ⭐⭐⭐ Alta — efeito "beauty" muito usado em webcams
- **Performance**: ~8ms por frame

### 1.7 Color Maps (LUT Effects / Filtros de Cor)

- **Função**: `cv2.applyColorMap(src, colormap_type)`
- **Descrição**: Aplica filtros de cor tipo Instagram (22 colormaps disponíveis). Efeitos como: Autumn, Bone, Cool, Hot, HSV, Inferno, Jet, Magma, Ocean, Plasma, Rainbow, Twilight, Viridis, etc.
- **Colormaps disponíveis**: 22 (AUTUMN, BONE, CIVIDIS, COOL, DEEPGREEN, HOT, HSV, INFERNO, JET, MAGMA, OCEAN, PARULA, PINK, PLASMA, RAINBOW, SPRING, SUMMER, TURBO, TWILIGHT, TWILIGHT_SHIFTED, VIRIDIS, WINTER)
- **Prioridade**: ⭐⭐ Média — efeitos visuais divertidos, fácil implementação
- **Performance**: ~1ms por frame (muito leve)

### 1.8 Sepia / Grayscale / Negative

- **Funções**: `cv2.cvtColor`, `cv2.transform`, `cv2.bitwise_not`
- **Descrição**: Filtros clássicos — sépia (tom envelhecido), preto e branco, negativo. Rápidos e populares.
- **Prioridade**: ⭐⭐ Média — filtros básicos mas populares
- **Performance**: ~0.5ms por frame (desprezível)

---

## 2. Melhorias de Qualidade de Imagem

### 2.1 CLAHE (Adaptive Contrast Enhancement)

- **Função**: `cv2.createCLAHE(clipLimit, tileGridSize)`
- **Descrição**: Melhora o contraste de forma adaptativa, corrigindo iluminação desigual. Excelente para webcams em ambientes com luz ruim ou contra-luz.
- **Parâmetros ajustáveis**: `clipLimit` (intensidade), `tileGridSize` (granularidade)
- **Prioridade**: ⭐⭐⭐ Alta — melhora significativa em condições de luz ruins
- **Performance**: ~2ms por frame

### 2.2 Denoise (Redução de Ruído)

- **Função**: `cv2.fastNlMeansDenoisingColored(src, h, hForColorComponents, templateWindowSize, searchWindowSize)`
- **Descrição**: Remove ruído/granulação de webcams em ambientes escuros. Algoritmo Non-Local Means — resultado superior ao blur comum.
- **Parâmetros ajustáveis**: Intensidade do filtro (`h`), tamanho da janela
- **Prioridade**: ⭐⭐⭐ Alta — essencial para webcams com sensor pequeno
- **Performance**: ~30ms (pesado — usar com cuidado ou em resolução reduzida)

### 2.3 Sharpen (Nitidez / Unsharp Mask)

- **Funções**: `cv2.Laplacian`, `cv2.addWeighted`, `cv2.GaussianBlur`
- **Descrição**: Aumenta a nitidez da imagem via unsharp masking. Melhora detalhes que webcams baratas perdem.
- **Parâmetros ajustáveis**: Intensidade do sharpening
- **Prioridade**: ⭐⭐ Média — complemento do detail enhance
- **Performance**: ~2ms por frame

### 2.4 Gamma Correction (Correção de Gama)

- **Função**: `cv2.intensity_transform.gammaCorrection(src, gamma)`
- **Descrição**: Ajusta brilho/exposição de forma não-linear. Clareia sombras sem estourar highlights, ou escurece cenas muito claras.
- **Parâmetros ajustáveis**: Valor gamma (0.1-3.0)
- **Prioridade**: ⭐⭐⭐ Alta — ajuste básico fundamental
- **Performance**: ~1ms por frame

### 2.5 White Balance Automático

- **Funções**: `cv2.xphoto.createGrayworldWB()`, `cv2.xphoto.createSimpleWB()`
- **Descrição**: Corrige balanço de branco automaticamente quando a câmera não faz bem. Remove tons amarelados/azulados indesejados.
- **Prioridade**: ⭐⭐ Média — útil para câmeras sem AWB bom
- **Performance**: ~2ms por frame

---

## 3. Detecção e Tracking

### 3.1 Face Detection (YuNet)

- **Função**: `cv2.FaceDetectorYN.create(model, config, input_size)`
- **Descrição**: Detecção facial em tempo real usando modelo DNN YuNet. Base para todos os efeitos que dependem de localização do rosto (background blur, masks, beauty filter).
- **Uso**: Fundação para background blur inteligente, auto-framing, face tracking
- **Prioridade**: ⭐⭐⭐ Alta — habilita múltiplos outros recursos
- **Performance**: ~5ms por frame (YuNet é otimizado para real-time)
- **Modelo**: Incluído no OpenCV (não precisa de download externo)

### 3.2 Auto-Framing (Enquadramento Automático)

- **Funções**: Face detection + `cv2.resize` + crop
- **Descrição**: Detecta o rosto e faz crop/zoom automático para manter o usuário centralizado no frame. Simula o "Center Stage" da Apple.
- **Parâmetros ajustáveis**: Margem ao redor do rosto, suavidade do tracking
- **Prioridade**: ⭐⭐ Média — recurso premium em webcams
- **Performance**: ~6ms por frame

---

## 4. Efeitos Artísticos/Fun

### 4.1 Edge Detection (Contorno / Cartoon)

- **Funções**: `cv2.Canny`, `cv2.adaptiveThreshold`, `cv2.bilateralFilter`
- **Descrição**: Efeito cartoon combinando edge detection + bilateral filter para criar visual de desenho animado.
- **Prioridade**: ⭐ Baixa — efeito divertido mas nicho
- **Performance**: ~5ms por frame

### 4.2 Vinheta (Vignette)

- **Funções**: `cv2.multiply`, operações com máscara gaussiana
- **Descrição**: Escurece as bordas da imagem, efeito fotográfico clássico que direciona atenção ao centro.
- **Parâmetros ajustáveis**: Intensidade, raio
- **Prioridade**: ⭐ Baixa — efeito sutil
- **Performance**: ~1ms por frame

### 4.3 Color Quantization (Posterize)

- **Função**: `cv2.kmeans`
- **Descrição**: Reduz o número de cores da imagem criando efeito "poster" ou "pixel art".
- **Parâmetros ajustáveis**: Número de cores (2-32)
- **Prioridade**: ⭐ Baixa — efeito artístico
- **Performance**: ~20ms (pesado para muitas cores)

---

## 5. Recursos Avançados

### 5.1 Super Resolution (IA)

- **Função**: `cv2.dnn_superres.DnnSuperResImpl.create()`
- **Descrição**: Aumenta a resolução da webcam usando redes neurais (EDSR, ESPCN, FSRCNN, LapSRN). Transforma 720p em 1080p com detalhes convincentes.
- **Modelos**: EDSR (melhor qualidade), FSRCNN (mais rápido), ESPCN (balanceado)
- **Prioridade**: ⭐⭐ Média — impressionante mas pesado
- **Performance**: ~50-200ms (pode precisar de GPU/OpenCL)

### 5.2 HDR Tone Mapping

- **Funções**: `cv2.createTonemapDrago`, `cv2.createTonemapReinhard`, `cv2.createTonemapMantiuk`
- **Descrição**: Simula efeito HDR em imagens LDR, expandindo o range dinâmico visual.
- **Prioridade**: ⭐ Baixa — uso limitado em webcam
- **Performance**: ~5ms por frame

---

## Recomendação de Implementação por Fases

### Fase 1 — Essenciais (maior impacto, baixa complexidade)
1. **Gamma Correction** — ajuste de brilho básico, ~1ms
2. **CLAHE** — melhoria de contraste adaptativa, ~2ms
3. **Detail Enhance** — nitidez melhorada, ~8ms
4. **Edge Preserving Filter** — beauty/soft skin, ~8ms
5. **Sepia / Grayscale / Negative** — filtros clássicos, ~0.5ms

### Fase 2 — Efeitos Populares
6. **Pencil Sketch** — efeito artístico, ~10ms
7. **Stylization** — efeito pintura, ~12ms
8. **Color Maps** — 22 filtros de cor, ~1ms
9. **Sharpen** — unsharp mask, ~2ms
10. **White Balance Automático** — correção AWB, ~2ms

### Fase 3 — Avançados (requerem face detection)
11. **Face Detection (YuNet)** — base para efeitos inteligentes, ~5ms
12. **Background Blur** — desfoque de fundo, ~10ms
13. **Auto-Framing** — enquadramento automático, ~6ms
14. **Virtual Background** — substituição de fundo, ~15ms

### Fase 4 — Premium
15. **Denoise** — redução de ruído (usar seletivamente), ~30ms
16. **Super Resolution** — upscale IA (GPU recomendada), ~50-200ms
17. **Cartoon Effect** — edge + bilateral, ~5ms
18. **Vignette** — escurecimento de bordas, ~1ms

---

## Arquitetura Sugerida

Os efeitos devem ser integrados como **filtros no pipeline GStreamer** ou como processamento de frame no appsink:

```
Camera → GStreamer → appsink → OpenCV filter chain → preview + virtual cam
```

- Criar uma classe `EffectPipeline` com interface `apply(frame: np.ndarray) -> np.ndarray`
- Cada efeito implementa a interface
- Efeitos podem ser empilhados (chain)
- UI: nova aba "Effects" no sidebar com toggles e sliders por efeito
- Performance target: manter ≤30ms total para 30fps

## Dependências

- `opencv` (já instalado — pacote do sistema)
- `numpy` (dependência do cv2, já disponível)
- Nenhuma dependência adicional necessária para Fases 1-3
- Fase 4 (Super Resolution): requer download de modelos DNN (~5MB cada)
