#!/bin/bash
set -uo pipefail
exec 2>&1

USB_PORT="${1:-}"
UDP_PORT="${2:-5000}"
CAM_NAME="${3:-DSLR Camera}"
CAM_NAME="${CAM_NAME//,/}"
# When called with V4L2_DEV=none, skip writing to v4l2loopback (BigCam handles via appsrc)
V4L2_DEV="${4:-auto}"

# Logs go to the user's private cache dir.  A fixed /tmp path is predictable
# and world-writable: another user can pre-create it as a symlink and have us
# truncate whatever it points at.
LOG_DIR="${XDG_CACHE_HOME:-$HOME/.cache}/bigcam"
mkdir -p "$LOG_DIR"
chmod 700 "$LOG_DIR" 2>/dev/null || true
LOG="${LOG_DIR}/gphoto_stream_${UDP_PORT}.log"
ERR_LOG="${LOG_DIR}/gphoto_err_${UDP_PORT}.log"
: > "$LOG"
: > "$ERR_LOG"

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
pkill -9 -f "gvfs-gphoto2-volume-monitor" 2>/dev/null
pkill -9 -f "gvfsd-gphoto2" 2>/dev/null
gio mount -u 'gphoto2://' 2>/dev/null
sleep 1

# ── Step 3: Load v4l2loopback ──
# Always go through the privileged helper: it is the only command BigCam is
# allowed to run as root, and it owns the device-pool numbering.  Calling
# modprobe here directly used to create devices at /dev/video10-13 while
# virtual_camera.py looked for them at /dev/video20+, so the two never agreed.
HELPER="$(dirname "$(readlink -f "$0")")/bigcam-v4l2loopback"
if [ "$V4L2_DEV" != "none" ] && [ ! -d /sys/module/v4l2loopback ]; then
  if [ -x "$HELPER" ]; then
    sudo -n "$HELPER" load 2>/dev/null || echo "WARN: could not load v4l2loopback"
    sleep 1
  else
    echo "WARN: privileged helper not found at $HELPER"
  fi
fi

# ── Step 4: Find a free v4l2loopback virtual device (unless skipped) ──
DEVICE_VIDEO=""
if [ "$V4L2_DEV" = "none" ]; then
  # BigCam handles v4l2loopback output via appsrc pipeline — only need UDP
  DEVICE_VIDEO=""
elif [ "$V4L2_DEV" != "auto" ] && [ -e "$V4L2_DEV" ]; then
  # Specific device pre-allocated by BigCam — use it directly
  DEVICE_VIDEO="$V4L2_DEV"
else
  for dev in /dev/video*; do
    [ -e "$dev" ] || continue
    DRIVER=$(v4l2-ctl -d "$dev" --info 2>/dev/null | grep "Driver name" | sed 's/.*: //')
    if echo "$DRIVER" | grep -qi "v4l2.*loopback\|loopback"; then
      if ! fuser "$dev" >/dev/null 2>&1; then
        DEVICE_VIDEO="$dev"
        break
      fi
    fi
  done
  [ -z "$DEVICE_VIDEO" ] && echo "ERROR: No free virtual video device found." && exit 1
fi

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
if ! timeout 10 gphoto2 --auto-detect 2>&1 | grep -qF -- "$USB_PORT"; then
  echo "WARN: Camera not at original port $USB_PORT, re-detecting..."
  # Try to find camera by name at a different port
  NEW_PORT=$(timeout 10 gphoto2 --auto-detect 2>/dev/null | grep -Fi "$CAM_NAME" | grep -o 'usb:[^ ]*' | head -1)
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
for attempt in $(seq 1 "$MAX_ATTEMPTS"); do
  : > "$ERR_LOG"
  : > "$LOG"

  if [ "$attempt" -gt 1 ]; then
    echo "Retry attempt $attempt/$MAX_ATTEMPTS..."
    pkill -9 -f "gvfs-gphoto2-volume-monitor" 2>/dev/null
    pkill -9 -f "gvfsd-gphoto2" 2>/dev/null
    sleep 3
  fi

  # Build the ffmpeg argument list as an array.  The previous version
  # concatenated everything into a string and ran it through `bash -c`, so a
  # quote in $USB_PORT or $DEVICE_VIDEO (both derived from device output)
  # would have been re-parsed as shell syntax.  Arrays keep every value as a
  # single argv entry, with no second round of word splitting.
  UDP_URL="udp://127.0.0.1:${UDP_PORT}?pkt_size=1316"
  FFMPEG_ARGS=(ffmpeg -y -hide_banner -loglevel error -stats -i -)
  if [ -n "$DEVICE_VIDEO" ]; then
    # Split to v4l2loopback + UDP
    FFMPEG_ARGS+=(
      -filter_complex "[0:v]format=yuv420p,split=2[v1][v2]"
      -map "[v1]" -r 30 -f v4l2 "$DEVICE_VIDEO"
      -map "[v2]" -f mpegts -r 30 -codec:v mpeg1video -b:v 5000k -bf 0
      "$UDP_URL"
    )
  else
    # UDP only (BigCam handles v4l2loopback via appsrc)
    FFMPEG_ARGS+=(
      -f mpegts -r 30 -codec:v mpeg1video -b:v 5000k -bf 0
      "$UDP_URL"
    )
  fi

  # Run the pipeline in a detached subshell.  Everything crossing the boundary
  # travels as an argument or a redirection target, never as shell source text.
  (
    gphoto2 --stdout --capture-movie --port "$USB_PORT" 2>"$ERR_LOG" \
      | "${FFMPEG_ARGS[@]}" >"$LOG" 2>&1
  ) &
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
      if [ -n "$DEVICE_VIDEO" ]; then
        echo "SUCCESS: $DEVICE_VIDEO"
      else
        echo "SUCCESS: UDP"
      fi
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
