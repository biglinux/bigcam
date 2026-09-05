#!/bin/bash
set -uo pipefail
exec 2>&1

USB_PORT="${1:-}"
UDP_PORT="${2:-5000}"
CAM_NAME="${3:-Canon DSLR}"
# Remove commas to prevent modprobe array parsing errors
CAM_NAME="${CAM_NAME//,/}"


if [ -n "$USB_PORT" ]; then
  PORT_STR="--port $USB_PORT"
  # Kill only THIS camera's previous instances
  pkill -f "gphoto2.*--port $USB_PORT" 2>/dev/null
  pkill -f "ffmpeg.*udp://127.0.0.1:$UDP_PORT" 2>/dev/null
  sleep 1
else
  PORT_STR=""
fi

# Kill gvfs interference more effectively
systemctl --user stop gvfs-gphoto2-volume-monitor.service 2>/dev/null
pkill -9 -f "gvfs-gphoto2-volume-monitor" 2>/dev/null
gio mount -u 'gphoto2://' 2>/dev/null
sleep 2

# Reset the USB interface of the camera before starting
# if [ -n "$USB_PORT" ]; then
#     timeout 8 gphoto2 --port "$USB_PORT" --reset >/dev/null 2>&1
# else
#     timeout 8 gphoto2 --reset >/dev/null 2>&1
# fi
# sleep 2

# Load v4l2loopback through the privileged helper (the only root command
# BigCam is granted).  It owns the device-pool numbering, so the devices it
# creates are the ones virtual_camera.py expects to find.
HELPER="$(dirname "$(readlink -f "$0")")/bigcam-v4l2loopback"
if [ ! -d /sys/module/v4l2loopback ]; then
  if [ -x "$HELPER" ]; then
    sudo -n "$HELPER" load 2>/dev/null || echo "WARN: could not load v4l2loopback"
    sleep 1
  else
    echo "WARN: privileged helper not found at $HELPER"
  fi
fi

# Find a free v4l2loopback virtual device
DEVICE_VIDEO=""
for dev in /dev/video*; do
  [ -e "$dev" ] || continue
  # Check if it's a v4l2loopback device via driver name
  DRIVER=$(v4l2-ctl -d "$dev" --info 2>/dev/null | grep "Driver name" | sed 's/.*: //')
  if echo "$DRIVER" | grep -qi "v4l2.*loopback\|loopback"; then
    # Check if NOT in use by another ffmpeg
    if ! fuser "$dev" >/dev/null 2>&1; then
      DEVICE_VIDEO="$dev"
      break
    fi
  fi
done

[ -z "$DEVICE_VIDEO" ] && echo "ERROR: No free virtual video device found." && exit 1

# Verify camera is connected with a timeout to prevent hang
if [ -n "$USB_PORT" ]; then
  if ! timeout 10 gphoto2 --auto-detect 2>&1 | grep -q "$USB_PORT"; then
    echo "ERROR: Camera at $USB_PORT not found or device busy."
    exit 1
  fi
else
  if ! timeout 10 gphoto2 --auto-detect 2>&1 | grep -q "usb:"; then
    echo "ERROR: No camera detected."
    exit 1
  fi
fi

# Launch with high quality settings
# Private cache dir, not a predictable world-writable /tmp path.
LOG_DIR="${XDG_CACHE_HOME:-$HOME/.cache}/bigcam"
mkdir -p "$LOG_DIR"
chmod 700 "$LOG_DIR" 2>/dev/null || true
LOG="${LOG_DIR}/gphoto_stream_${UDP_PORT}.log"
ERR_LOG="${LOG_DIR}/gphoto_err_${UDP_PORT}.log"
: > "$LOG"
: > "$ERR_LOG"

# Quality Upgrades:
# - Bitrate was 800k (pixilated), now 5000k (sharp)
# - Removed downscaling (Full native T3 resolution)
# - Syncing to 30 FPS (Match T3 native output for stability)
nohup bash -c "gphoto2 --stdout --capture-movie $PORT_STR 2>\"$ERR_LOG\" | ffmpeg -y -hide_banner -loglevel error -stats -i - -filter_complex \"[0:v]format=yuv420p,split=2[v1][v2]\" -map \"[v1]\" -r 30 -f v4l2 \"$DEVICE_VIDEO\" -map \"[v2]\" -f mpegts -r 30 -codec:v mpeg1video -b:v 5000k -bf 0 \"udp://127.0.0.1:${UDP_PORT}?pkt_size=1316\" >\"$LOG\" 2>&1" &
PID=$!
disown

# Wait for it to stabilize
sleep 3

if kill -0 "$PID" 2>/dev/null; then
  echo "SUCCESS: $DEVICE_VIDEO"
  exit 0
else
  echo "ERROR: Pipeline failed."
  cat "$LOG"
  cat "$ERR_LOG"
  exit 1
fi
