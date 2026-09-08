#!/usr/bin/env bash
# Compatibility entry point. Never accepts raw modprobe arguments.
set -euo pipefail
if [[ $# -gt 1 || ${1:-load} != load ]]; then
    echo 'Only load is supported. Shared kernel modules are never unloaded by BigCam.' >&2
    exit 2
fi
exec /usr/bin/pkexec /usr/lib/bigcam/virtual-camera-helper load
