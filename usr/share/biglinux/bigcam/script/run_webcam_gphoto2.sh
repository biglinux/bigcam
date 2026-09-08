#!/usr/bin/env bash
# Foreground, owned camera producer. No sudo, global process kills or USB resets.
set -Eeuo pipefail
[[ $# -ge 2 && $# -le 4 ]] || { echo 'Usage: run_webcam_gphoto2.sh usb:BUS,DEVICE UDP_PORT [NAME] [none]' >&2; exit 2; }
[[ $1 =~ ^usb:[0-9]{1,3},[0-9]{1,3}$ && $2 =~ ^[0-9]{4,5}$ ]] || { echo 'Invalid camera/port' >&2; exit 2; }
[[ ${4:-none} == none ]] || { echo 'Use BigCam to create an authorized virtual camera output.' >&2; exit 2; }
HERE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
exec /usr/bin/python3 "$HERE/../core/gphoto_session.py" "$1" "$2"
