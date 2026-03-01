#!/bin/bash
set -uo pipefail
exec 2>&1

USB_PORT="$1"
UDP_PORT="${2:-5000}"
CAM_NAME="${3:-DSLR Camera}"
CAM_NAME="${CAM_NAME//,/}"

LOG="/tmp/canon_webcam_stream_${UDP_PORT}.log"
ERR_LOG="/tmp/gphoto_err_${UDP_PORT}.log"
> "$LOG"
> "$ERR_LOG"

# ── Step 1: Kill ONLY this camera's previous processes ──
if [ -n "$USB_PORT" ]; then
  pkill -f "gphoto2.*--port ${USB_PORT}" 2>/dev/null
  sleep 0.5
  pkill -9 -f "gphoto2.*--port ${USB_PORT}" 2>/dev/null
fi
pkill -f "ffmpeg.*udp://127.0.0.1:${UDP_PORT}" 2>/dev/null
sleep 0.5
pkill -9 -f "ffmpeg.*udp://127.0.0.1:${UDP_PORT}" 2>/dev/null
sleep 1

# ── Step 2: Kill GVFS interference ──
systemctl --user stop gvfs-gphoto2-volume-monitor.service 2>/dev/null
systemctl --user mask gvfs-gphoto2-volume-monitor.service 2>/dev/null
pkill -9 -f "gvfs-gphoto2-volume-monitor" 2>/dev/null
pkill -9 -f "gvfsd-gphoto2" 2>/dev/null
gio mount -u gphoto2://* 2>/dev/null
sleep 1

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

# ── Step 5: Validate and refresh camera port ──
if [ -z "$USB_PORT" ]; then
  echo "ERROR: No USB port specified."
  exit 1
fi

# Kill GVFS again right before port check (it respawns fast)
pkill -9 -f "gvfs-gphoto2-volume-monitor" 2>/dev/null
pkill -9 -f "gvfsd-gphoto2" 2>/dev/null
sleep 0.5

# Verify the specific camera is accessible, re-detect port if needed
if ! timeout 10 gphoto2 --auto-detect 2>&1 | grep -q "$USB_PORT"; then
  echo "WARN: Camera not at original port $USB_PORT, re-detecting..."
  # Try to find camera by name at a different port
  NEW_PORT=$(timeout 10 gphoto2 --auto-detect 2>/dev/null | grep -i "$CAM_NAME" | grep -oP 'usb:\S+' | head -1)
  if [ -n "$NEW_PORT" ]; then
    echo "INFO: Camera '$CAM_NAME' found at new port: $NEW_PORT"
    USB_PORT="$NEW_PORT"
  else
    # Do NOT pick a random camera — that would stream the wrong one
    echo "ERROR: Camera '$CAM_NAME' not detected at any port."
    exit 1
  fi
fi

# ── Step 6: Launch gphoto2 + ffmpeg with retry ──
MAX_ATTEMPTS=3
for attempt in $(seq 1 $MAX_ATTEMPTS); do
  > "$ERR_LOG"
  > "$LOG"

  if [ "$attempt" -gt 1 ]; then
    echo "Retry attempt $attempt/$MAX_ATTEMPTS..."
    pkill -9 -f "gvfs-gphoto2-volume-monitor" 2>/dev/null
    pkill -9 -f "gvfsd-gphoto2" 2>/dev/null
    sleep 3
  fi

  nohup bash -c "gphoto2 --stdout --capture-movie --port '$USB_PORT' 2>\"$ERR_LOG\" | \
    ffmpeg -y -hide_banner -loglevel error -stats -i - \
    -filter_complex \"[0:v]format=yuv420p,split=2[v1][v2]\" \
    -map \"[v1]\" -r 30 -f v4l2 \"$DEVICE_VIDEO\" \
    -map \"[v2]\" -f mpegts -r 30 -codec:v mpeg1video -b:v 5000k -bf 0 \
    \"udp://127.0.0.1:${UDP_PORT}?pkt_size=1316\" >\"$LOG\" 2>&1" &
  PID=$!
  disown

  # Wait and verify streaming actually works
  sleep 6

  if kill -0 "$PID" 2>/dev/null; then
    # Check for PTP errors
    if grep -q "PTP Timeout\|PTP Error\|Erro na captura" "$ERR_LOG" 2>/dev/null; then
      kill -9 "$PID" 2>/dev/null
      pkill -f "gphoto2.*--port ${USB_PORT}" 2>/dev/null
      pkill -f "ffmpeg.*udp://127.0.0.1:${UDP_PORT}" 2>/dev/null
      sleep 1
      continue
    fi

    # Verify ffmpeg is actually writing frames (check log for frame= stats)
    if [ -s "$LOG" ] || ! grep -q "Erro\|Error" "$ERR_LOG" 2>/dev/null; then
      echo "SUCCESS: $DEVICE_VIDEO"
      exit 0
    fi
  fi

  # Process died — retry with USB reset
  pkill -f "gphoto2.*--port ${USB_PORT}" 2>/dev/null
  pkill -f "ffmpeg.*udp://127.0.0.1:${UDP_PORT}" 2>/dev/null
  sleep 1
done

echo "ERROR: Pipeline failed after $MAX_ATTEMPTS attempts."
cat "$ERR_LOG"
cat "$LOG"
exit 1
