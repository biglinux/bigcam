#!/bin/bash
exec 2>&1

USB_PORT="$1"
UDP_PORT="${2:-5000}"
CAM_NAME="${3:-DSLR Camera}"
CAM_NAME="${CAM_NAME//,/}"

LOG="/tmp/canon_webcam_stream_${UDP_PORT}.log"
ERR_LOG="/tmp/gphoto_err_${UDP_PORT}.log"
> "$LOG"
> "$ERR_LOG"

# ── Helper: find the sysfs path for a Canon camera ──
find_canon_sysfs() {
  for d in /sys/bus/usb/devices/*; do
    [ -f "$d/idVendor" ] || continue
    local vendor
    vendor=$(cat "$d/idVendor" 2>/dev/null)
    if [ "$vendor" = "04a9" ]; then
      echo "$d"
      return 0
    fi
  done
  return 1
}

# ── Helper: USB reset via ioctl (no sudo needed) ──
usb_reset_camera() {
  python3 -c "
import fcntl, os, subprocess
USBDEVFS_RESET = 21780
result = subprocess.run(['lsusb'], capture_output=True, text=True)
for line in result.stdout.strip().split('\n'):
    if '04a9' in line:  # Canon vendor ID
        parts = line.split()
        bus = parts[1]
        dev = parts[3].rstrip(':')
        path = f'/dev/bus/usb/{bus}/{dev}'
        try:
            fd = os.open(path, os.O_WRONLY)
            fcntl.ioctl(fd, USBDEVFS_RESET, 0)
            os.close(fd)
            print(f'USB reset OK: {path}')
        except Exception as e:
            print(f'USB reset error: {e}')
        break
" 2>&1
  sleep 5
}

# ── Step 1: Kill previous streaming processes ──
pkill -f "gphoto2 --" 2>/dev/null
pkill -f "ffmpeg.*mpegts" 2>/dev/null
pkill -f "ffmpeg.*v4l2" 2>/dev/null
sleep 1
pkill -9 -f "gphoto2 --" 2>/dev/null
pkill -9 -f "ffmpeg.*mpegts" 2>/dev/null
pkill -9 -f "ffmpeg.*v4l2" 2>/dev/null

# ── Step 2: Kill GVFS interference ──
systemctl --user stop gvfs-gphoto2-volume-monitor.service 2>/dev/null
systemctl --user mask gvfs-gphoto2-volume-monitor.service 2>/dev/null
pkill -9 -f "gvfs-gphoto2-volume-monitor" 2>/dev/null
pkill -9 -f "gvfsd-gphoto2" 2>/dev/null
gio mount -u gphoto2://* 2>/dev/null

# Wait for USB to stabilize
sleep 3

# ── Step 3: Load v4l2loopback ──
CARD_LABELS="${CAM_NAME} (v4l2),${CAM_NAME} 2 (v4l2),${CAM_NAME} 3 (v4l2),${CAM_NAME} 4 (v4l2)"
if ! lsmod | grep -q v4l2loopback; then
  bigsudo modprobe v4l2loopback devices=4 exclusive_caps=1 max_buffers=4 \
    "card_label=$CARD_LABELS"
  sleep 1
else
  if [ "$(cat /sys/module/v4l2loopback/parameters/exclusive_caps 2>/dev/null)" = "0" ]; then
    if ! fuser /dev/video* >/dev/null 2>&1; then
      bigsudo modprobe -r v4l2loopback 2>/dev/null
      sleep 1
      bigsudo modprobe v4l2loopback devices=4 exclusive_caps=1 max_buffers=4 \
        "card_label=$CARD_LABELS"
      sleep 1
    fi
  fi
fi

# ── Step 4: Find a free v4l2loopback virtual device ──
DEVICE_VIDEO=""
for dev in $(ls -v /dev/video* 2>/dev/null); do
  DRIVER=$(v4l2-ctl -d "$dev" --info 2>/dev/null | grep "Driver name" | sed 's/.*: //')
  if echo "$DRIVER" | grep -qi "v4l2.*loopback\|loopback"; then
    if ! fuser "$dev" >/dev/null 2>&1; then
      DEVICE_VIDEO="$dev"
      break
    fi
  fi
done
[ -z "$DEVICE_VIDEO" ] && echo "ERROR: No free virtual video device found." && exit 1

# ── Step 5: Detect camera port (fresh) ──
DETECTED_PORT=$(timeout 10 gphoto2 --auto-detect 2>&1 | grep "usb:" | awk '{print $NF}')
if [ -z "$DETECTED_PORT" ]; then
  echo "ERROR: No camera detected."
  exit 1
fi
PORT_STR="--port $DETECTED_PORT"

# ── Step 6: Launch gphoto2 + ffmpeg with retry and auto USB reset ──
MAX_ATTEMPTS=3
for attempt in $(seq 1 $MAX_ATTEMPTS); do
  > "$ERR_LOG"
  > "$LOG"

  # Wait for USB to settle (more time after USB reset)
  if [ "$attempt" -gt 1 ]; then
    echo "Retry attempt $attempt/$MAX_ATTEMPTS after USB reset..."
    # Kill any GVFS that may have restarted
    pkill -9 -f "gvfs-gphoto2-volume-monitor" 2>/dev/null
    pkill -9 -f "gvfsd-gphoto2" 2>/dev/null
    # Re-detect port since USB reset changes device number
    sleep 5
    DETECTED_PORT=$(timeout 10 gphoto2 --auto-detect 2>&1 | grep "usb:" | awk '{print $NF}')
    [ -z "$DETECTED_PORT" ] && continue
    PORT_STR="--port $DETECTED_PORT"
    sleep 2
  else
    sleep 2
  fi

  nohup bash -c "gphoto2 --stdout --capture-movie $PORT_STR 2>\"$ERR_LOG\" | \
    ffmpeg -y -hide_banner -loglevel error -stats -i - \
    -filter_complex \"[0:v]format=yuv420p,split=2[v1][v2]\" \
    -map \"[v1]\" -r 30 -f v4l2 \"$DEVICE_VIDEO\" \
    -map \"[v2]\" -f mpegts -r 30 -codec:v mpeg1video -b:v 5000k -bf 0 \
    \"udp://127.0.0.1:${UDP_PORT}?pkt_size=1316\" >\"$LOG\" 2>&1" &
  PID=$!
  disown

  # Wait and check if streaming started
  sleep 6

  if kill -0 "$PID" 2>/dev/null; then
    # Check if gphoto2 actually produced frames (not just waiting to timeout)
    if grep -q "PTP Timeout" "$ERR_LOG" 2>/dev/null; then
      # Process alive but PTP timeout — kill and retry with USB reset
      kill -9 "$PID" 2>/dev/null
      pkill -9 -f "gphoto2 --" 2>/dev/null
      pkill -9 -f "ffmpeg.*mpegts" 2>/dev/null
      sleep 1
      usb_reset_camera
      continue
    fi
    echo "SUCCESS: $DEVICE_VIDEO"
    exit 0
  fi

  # Process died — check if PTP error, do USB reset and retry
  if grep -q "PTP Timeout\|Dispositivo ou recurso está ocupado" "$ERR_LOG" 2>/dev/null; then
    usb_reset_camera
    continue
  fi
  break
done

echo "ERROR: Pipeline failed after $MAX_ATTEMPTS attempts."
cat "$ERR_LOG"
cat "$LOG"
exit 1
