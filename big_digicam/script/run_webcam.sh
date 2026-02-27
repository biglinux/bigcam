#!/bin/bash
exec 2>&1

USB_PORT="$1"
UDP_PORT="${2:-5000}"

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
gio mount -u gphoto2://* 2>/dev/null
sleep 2

# Reset the USB interface of the camera before starting
if [ -n "$USB_PORT" ]; then
    timeout 8 gphoto2 --port "$USB_PORT" --reset 2>/dev/null
else
    timeout 8 gphoto2 --reset 2>/dev/null
fi
sleep 2

# Load v4l2loopback with 4 virtual devices if not loaded
if ! lsmod | grep -q v4l2loopback; then
  bigsudo modprobe v4l2loopback devices=4 exclusive_caps=1 max_buffers=4 card_label="Canon DSLR Webcam,Canon DSLR Webcam 2,Canon DSLR Webcam 3,Canon DSLR Webcam 4"
  sleep 1
else
  # If loaded with exclusive_caps=0, reload only if no device is in use
  if [ "$(cat /sys/module/v4l2loopback/parameters/exclusive_caps 2>/dev/null)" = "0" ]; then
    if ! fuser /dev/video* >/dev/null 2>&1; then
      bigsudo modprobe -r v4l2loopback 2>/dev/null
      sleep 1
      bigsudo modprobe v4l2loopback devices=4 exclusive_caps=1 max_buffers=4 card_label="Canon DSLR Webcam,Canon DSLR Webcam 2,Canon DSLR Webcam 3,Canon DSLR Webcam 4"
      sleep 1
    fi
  fi
fi

# Find a free v4l2loopback virtual device
DEVICE_VIDEO=""
for dev in $(ls -v /dev/video* 2>/dev/null); do
  # Check if it's a v4l2loopback device via driver name
  DRIVER=$(v4l2-ctl -d "$dev" --info 2>/dev/null | grep "Driver name" | awk '{print $NF}')
  if [ "$DRIVER" = "v4l2" ] || echo "$DRIVER" | grep -qi "loopback"; then
    # Also check card name
    CARD=$(v4l2-ctl -d "$dev" --info 2>/dev/null | grep "Card type" | sed 's/.*: //')
    if echo "$CARD" | grep -qi "v4l2loopback\|Canon DSLR"; then
      # Check if NOT in use by another ffmpeg
      if ! fuser "$dev" >/dev/null 2>&1; then
        DEVICE_VIDEO="$dev"
        break
      fi
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
LOG="/tmp/canon_webcam_stream_${UDP_PORT}.log"
ERR_LOG="/tmp/gphoto_err_${UDP_PORT}.log"
> "$LOG"
> "$ERR_LOG"

# Quality Upgrades:
# - Bitrate was 800k (pixilated), now 5000k (sharp)
# - Removed downscaling (Full native T3 resolution)
# - Syncing to 30 FPS (Match T3 native output for stability)
nohup bash -c "gphoto2 --stdout --capture-movie $PORT_STR 2>\"$ERR_LOG\" | ffmpeg -y -hide_banner -loglevel error -stats -i - -filter_complex \"[0:v]format=yuv420p,split=2[v1][v2]\" -map \"[v1]\" -r 30 -f v4l2 \"$DEVICE_VIDEO\" -map \"[v2]\" -f mpegts -r 30 -codec:v mpeg1video -b:v 5000k -bf 0 \"udp://127.0.0.1:${UDP_PORT}?pkt_size=1316\" >\"$LOG\" 2>&1" &
PID=$!
disown

# Wait for it to stabilize
sleep 3

if kill -0 $PID 2>/dev/null; then
  echo "SUCCESS: $DEVICE_VIDEO"
  exit 0
else
  echo "ERROR: Pipeline failed."
  cat "$LOG"
  cat "$ERR_LOG"
  exit 1
fi
