#!/usr/bin/env bash
# Private X11/D-Bus/PulseAudio test session. Never expose host devices or sockets.
set -Eeuo pipefail
[[ $EUID != 0 ]] || { echo "Run graphical tests as a normal user" >&2; exit 1; }
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)
export BIGCAM_TEST_RESULTS=${BIGCAM_TEST_RESULTS:?Set an absolute output directory}
mkdir -p "$BIGCAM_TEST_RESULTS"
export XDG_RUNTIME_DIR
XDG_RUNTIME_DIR=$(mktemp -d /tmp/bigcam-tests.XXXXXXXX)
chmod 700 "$XDG_RUNTIME_DIR"
export HOME="$XDG_RUNTIME_DIR/home" XDG_CONFIG_HOME="$XDG_RUNTIME_DIR/config" XDG_CACHE_HOME="$XDG_RUNTIME_DIR/cache" XDG_STATE_HOME="$XDG_RUNTIME_DIR/state"
mkdir -p "$HOME" "$XDG_CONFIG_HOME" "$XDG_CACHE_HOME" "$XDG_STATE_HOME"
export GTK_A11Y=atspi NO_AT_BRIDGE=0 GDK_BACKEND=x11 GSK_RENDERER=gl LIBGL_ALWAYS_SOFTWARE=1
export LANG=C.UTF-8 LC_ALL=C.UTF-8 PYTHONDONTWRITEBYTECODE=1 PYTHONNOUSERSITE=1
export PULSE_SERVER="unix:$XDG_RUNTIME_DIR/pulse-native"
cleanup() {
    cp -a "$XDG_STATE_HOME/." "$BIGCAM_TEST_RESULTS/state/" 2>/dev/null || true
    rm -rf -- "$XDG_RUNTIME_DIR"
}
trap cleanup EXIT
xvfb-run -a -s '-screen 0 1280x900x24 -nolisten tcp' dbus-run-session -- bash -c '
    set -euo pipefail
    openbox > "$BIGCAM_TEST_RESULTS/openbox.log" 2>&1 & wm=$!
    pulseaudio --daemonize=no --exit-idle-time=-1 --disable-shm=yes -n         --load="module-native-protocol-unix socket=$XDG_RUNTIME_DIR/pulse-native auth-anonymous=1"         --load="module-null-sink sink_name=bigcam_test" > "$BIGCAM_TEST_RESULTS/audio.log" 2>&1 & pa=$!
    trap '''kill "$wm" "$pa" 2>/dev/null || true; wait "$wm" "$pa" 2>/dev/null || true''' EXIT
    for _ in {1..40}; do [[ -S $XDG_RUNTIME_DIR/pulse-native ]] && break; sleep .1; done
    /usr/bin/python3 "$1/tests/integration/run_graphical.py"
' _ "$ROOT"
