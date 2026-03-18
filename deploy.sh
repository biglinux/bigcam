#!/bin/bash
set -euo pipefail

SRC="/home/ruscher/Documentos/Git/bigcam/usr/share/biglinux/bigcam"
DST="/usr/share/biglinux/bigcam"
REPO="/home/ruscher/Documentos/Git/bigcam"

echo "Copiando arquivos corrigidos para o sistema..."

sudo cp "$SRC/core/stream_engine.py"    "$DST/core/stream_engine.py"
sudo cp "$SRC/core/virtual_camera.py"   "$DST/core/virtual_camera.py"
sudo cp "$SRC/ui/camera_selector.py"    "$DST/ui/camera_selector.py"
sudo cp "$SRC/ui/window.py"             "$DST/ui/window.py"
sudo cp "$SRC/ui/preview_area.py"       "$DST/ui/preview_area.py"
sudo cp "$SRC/ui/tools_page.py"         "$DST/ui/tools_page.py"
sudo cp "$SRC/style.css"                "$DST/style.css"
sudo cp "$SRC/script/run_webcam.sh"     "$DST/script/run_webcam.sh"
sudo cp "$SRC/script/run_webcam_gphoto2.sh" "$DST/script/run_webcam_gphoto2.sh"
sudo cp "$SRC/core/phone_camera.py"     "$DST/core/phone_camera.py"
sudo cp "$SRC/core/camera_manager.py"   "$DST/core/camera_manager.py"
sudo cp "$SRC/core/backends/v4l2_backend.py"    "$DST/core/backends/v4l2_backend.py"
sudo cp "$SRC/core/backends/gphoto2_backend.py" "$DST/core/backends/gphoto2_backend.py"

# Install/update sudoers file for passwordless modprobe
echo "Instalando sudoers.d/bigcam (sem senha para modprobe)..."
sudo cp "$REPO/etc/sudoers.d/bigcam" /etc/sudoers.d/bigcam
sudo chmod 0440 /etc/sudoers.d/bigcam
sudo chown root:root /etc/sudoers.d/bigcam

echo "✅ Todos os arquivos copiados com sucesso!"
echo ""
echo "Agora feche o BigCam e reabra para testar."
