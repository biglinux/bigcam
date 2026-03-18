#!/bin/bash
# Load the v4l2loopback kernel module with BigCam parameters.
# Called via pkexec from virtual_camera.py.
set -euo pipefail

ACTION="${1:-load}"

case "$ACTION" in
    load)
        modprobe v4l2loopback \
            devices=4 \
            exclusive_caps=1,1,1,1 \
            max_buffers=4 \
            video_nr=10,11,12,13 \
            "card_label=BigCam Virtual 1,BigCam Virtual 2,BigCam Virtual 3,BigCam Virtual 4"
        ;;
    unload)
        modprobe -r v4l2loopback
        ;;
    reload)
        modprobe -r v4l2loopback 2>/dev/null || true
        modprobe v4l2loopback \
            devices=4 \
            exclusive_caps=1,1,1,1 \
            max_buffers=4 \
            video_nr=10,11,12,13 \
            "card_label=BigCam Virtual 1,BigCam Virtual 2,BigCam Virtual 3,BigCam Virtual 4"
        ;;
    *)
        echo "Usage: $0 {load|unload|reload}" >&2
        exit 1
        ;;
esac
