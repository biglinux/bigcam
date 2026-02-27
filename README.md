<p align="center">
  <img src="usr/share/biglinux/big-digicam/icons/big-digicam.svg" alt="Big DigiCam" width="128" height="128">
</p>

<h1 align="center">📸 Big DigiCam</h1>

<p align="center">
  <b>Transforme sua câmera digital (DSLR/Mirrorless) em uma poderosa webcam profissional para Linux</b>
</p>

<p align="center">
  O <b>Big DigiCam</b> é baseado no projeto <a href="http://www.gphoto.org/proj/libgphoto2/">libgphoto2</a>, oferecendo suporte a mais de 2.500 modelos de câmeras.
</p>

<p align="center">
  <a href="#-funcionalidades-principais">Funcionalidades</a> •
  <a href="#-câmeras-suportadas">Câmeras Suportadas</a> •
  <a href="#-instalação-arch-linux--biglinux">Instalação</a> •
  <a href="#-contribuições">Contribuições</a> •
  <a href="#-licença">Licença</a>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/License-GPLv3-blue.svg" alt="License: GPL v3">
  <img src="https://img.shields.io/badge/Platform-Linux-green.svg" alt="Platform: Linux">
  <img src="https://img.shields.io/badge/GTK-4.0-blue.svg" alt="GTK 4.0">
  <img src="https://img.shields.io/badge/Libadwaita-1.0-purple.svg" alt="Libadwaita">
  <img src="https://img.shields.io/badge/Python-3.x-yellow.svg" alt="Python 3">
</p>

---

## 🌟 O Projeto

O **Big DigiCam** nasceu de uma necessidade real. O que começou como um pequeno script em shell criado por **Rafael Ruscher** e **Barnabé di Kartola** para permitir que o Ruscher usasse sua câmera Canon Rebel T3 em suas lives sobre o **BigLinux**, evoluiu para uma aplicação completa, elegante e robusta integrada ao ecossistema BigLinux.

Agradecemos imensamente aos pioneiros Rafael e Barnabé por iniciarem essa jornada que hoje ajuda milhares de usuários a terem qualidade de estúdio em suas videoconferências e produções de conteúdo.

---

## 🚀 Funcionalidades Principais

- **Webcam Profissional (4K/HD)**: Use a qualidade total do sensor da sua câmera em Zoom, Teams, Google Meet, OBS Studio e Skype.
- **Detecção Automática**: Conecte via USB e o Big DigiCam detecta o modelo e as capacidades da sua câmera instantaneamente.
- **Fotografia Remota**: Capture imagens diretamente do computador com pré-visualização em tempo real e download automático.
- **Alta Performance**: Pipeline otimizado com FFmpeg e GStreamer para garantir o menor atraso (latency) possível.
- **Interface Libadwaita**: Design moderno, limpo e totalmente compatível com o tema escuro/claro do sistema.
- **Suporte Multicam**: Gerencie múltiplas câmeras conectadas simultaneamente.

---

## 📸 Câmeras Suportadas

Graças ao driver `libgphoto2`, suportamos quase todas as câmeras DSLR e Mirrorless modernas que possuem porta USB.

### Marcas Principais
- **Canon EOS**: Rebel T3, T5, T6, T7, SL2, SL3, 80D, 90D, R5, R6, M50, etc. (Suporte nativo excelente).
- **Nikon**: D3200, D3500, D5300, D5600, D750, Z6, Z7, etc.
- **Sony Alpha**: A6000, A6400, A7III, A7R, ZV-E10 (requer modo "PC Remote").
- **FujiFilm**: X-T3, X-T4, X-H2S, etc.
- **Panasonic/Olympus**: Diversos modelos compatíveis com PTP.

> 🔗 **Verifique sua câmera**: [Lista Completa de Câmeras Suportadas](http://www.gphoto.org/proj/libgphoto2/support.php)

---

## 📦 Instalação (Arch Linux / BigLinux)

O Big DigiCam já inclui um instalador automatizado que configura os drivers de kernel necessários (`v4l2loopback`).

```bash
# Clone o repositório
git clone https://github.com/ruscher/cannon-rebel-t3-webcam-gphoto2-ffmpeg.git
cd cannon-rebel-t3-webcam-gphoto2-ffmpeg

# Execute o instalador (Arch/BigLinux)
chmod +x script/install-archlinux.sh
./script/install-archlinux.sh
```

---

## 🛠 Arquitetura do Projeto

O projeto segue os padrões de desenvolvimento do BigLinux, inspirado na estrutura do `biglinux-settings`.

```
.
├── main.py                     # Entry point da aplicação
├── script/                     # Scripts de sistema (Shell)
│   ├── run_webcam.sh           # Gestão do pipeline FFmpeg/GPhoto2
│   └── install-archlinux.sh    # Script de setup e drivers
├── utils/                      # Módulos Python auxiliares
│   └── i18n.py                 # Suporte a Internacionalização
├── locale/                     # Arquivos de tradução (gettext)
└── etc/                        # Configurações de sistema (sudoers/modprobe)
```

---

## 🤝 Contribuições

Este projeto é parte integrante do esforço da comunidade **BigLinux** para fornecer ferramentas de alta qualidade para usuários Linux.

**Desenvolvedores Originais:**
- Rafael Ruscher ([@ruscher](https://github.com/ruscher))
- Barnabé di Kartola

---

## ⚖️ Licença

Este projeto está licenciado sob a **GPLv3** (General Public License v3). Sendo software livre, você é encorajado a usar, modificar e distribuir.

---
*© 2026 BigLinux Team*
