#!/bin/bash
# BigCam Camera Diagnostics
# Checks USB topology, camera formats, v4l2loopback state, and bandwidth usage.
set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

echo -e "${BLUE}═══════════════════════════════════════════════${NC}"
echo -e "${BLUE}  BigCam Camera Diagnostics${NC}"
echo -e "${BLUE}═══════════════════════════════════════════════${NC}"
echo ""

# 1. USB Topology
echo -e "${YELLOW}▶ USB Topology (bus speed and hub sharing)${NC}"
echo "  Cameras on the same USB 2.0 hub share ~35 MB/s of bandwidth."
echo ""
if command -v lsusb &>/dev/null; then
    lsusb -t 2>/dev/null | grep -E "(root_hub|Video|Hub|Storage)" || true
else
    echo -e "  ${RED}lsusb not found${NC}"
fi
echo ""

# 2. Physical cameras
echo -e "${YELLOW}▶ Detected V4L2 Devices${NC}"
if command -v v4l2-ctl &>/dev/null; then
    v4l2-ctl --list-devices 2>/dev/null || echo -e "  ${RED}Failed to list devices${NC}"
else
    echo -e "  ${RED}v4l2-ctl not found (install v4l-utils)${NC}"
fi
echo ""

# 3. Camera formats and bandwidth estimate
echo -e "${YELLOW}▶ Camera Formats & USB Bandwidth Estimate${NC}"
for dev in /dev/video*; do
    [ -c "$dev" ] || continue
    # Skip v4l2loopback devices
    if [ -f "/sys/class/video4linux/$(basename "$dev")/device/driver/module" ]; then
        mod=$(basename "$(readlink -f "/sys/class/video4linux/$(basename "$dev")/device/driver/module")" 2>/dev/null || echo "")
        [ "$mod" = "v4l2loopback" ] && continue
    fi
    # Check if it's a capture device
    info=$(v4l2-ctl -d "$dev" --info 2>/dev/null || echo "")
    echo "$info" | grep -q "Video Capture" || continue
    
    name=$(echo "$info" | grep "Card type" | sed 's/.*: //' || echo "$dev")
    echo -e "  ${GREEN}$dev${NC} - $name"
    
    # Get formats
    formats=$(v4l2-ctl -d "$dev" --list-formats-ext 2>/dev/null || echo "")
    if [ -n "$formats" ]; then
        # Show highest resolution per format type
        echo "$formats" | grep -E "^\s+\[\d+\]:|Size:|Interval:" | head -15
        
        # Bandwidth estimate for YUYV
        yuyv_res=$(echo "$formats" | grep -A1 "YUYV" | grep "Size:" | head -1 | grep -oP '\d+x\d+' || echo "")
        if [ -n "$yuyv_res" ]; then
            w=$(echo "$yuyv_res" | cut -dx -f1)
            h=$(echo "$yuyv_res" | cut -dx -f2)
            fps=$(echo "$formats" | grep -A5 "$yuyv_res" | grep "fps" | head -1 | grep -oP '[\d.]+(?= fps)' || echo "25")
            bw=$(echo "$w * $h * 2 * $fps / 1048576" | bc -l 2>/dev/null | xargs printf "%.1f" 2>/dev/null || echo "?")
            echo -e "    ${BLUE}→ YUYV ${yuyv_res} @ ${fps}fps = ~${bw} MB/s USB bandwidth${NC}"
        fi
    fi
    echo ""
done

# 4. v4l2loopback state
echo -e "${YELLOW}▶ v4l2loopback Module State${NC}"
if [ -d /sys/module/v4l2loopback ]; then
    echo -e "  ${GREEN}Module loaded${NC}"
    for param in devices exclusive_caps max_buffers video_nr; do
        pfile="/sys/module/v4l2loopback/parameters/$param"
        [ -f "$pfile" ] && echo "  $param = $(cat "$pfile")"
    done
else
    echo -e "  ${RED}Module NOT loaded${NC}"
fi
echo ""

# 5. Device users
echo -e "${YELLOW}▶ Processes Using Camera Devices${NC}"
for dev in /dev/video*; do
    [ -c "$dev" ] || continue
    users=$(fuser "$dev" 2>/dev/null || echo "")
    if [ -n "$users" ]; then
        pids=$(echo "$users" | tr -s ' ' '\n' | sed 's/[^0-9]//g' | sort -u)
        names=""
        for pid in $pids; do
            [ -f "/proc/$pid/comm" ] && names="$names $(cat "/proc/$pid/comm" 2>/dev/null)"
        done
        echo -e "  $dev:${GREEN}$names${NC} (PIDs: $(echo $pids | tr '\n' ' '))"
    fi
done
echo ""

# 6. Quick frame integrity test
echo -e "${YELLOW}▶ Quick Frame Integrity Test (2 seconds per camera)${NC}"
for dev in /dev/video*; do
    [ -c "$dev" ] || continue
    # Skip v4l2loopback
    if [ -f "/sys/class/video4linux/$(basename "$dev")/device/driver/module" ]; then
        mod=$(basename "$(readlink -f "/sys/class/video4linux/$(basename "$dev")/device/driver/module")" 2>/dev/null || echo "")
        [ "$mod" = "v4l2loopback" ] && continue
    fi
    info=$(v4l2-ctl -d "$dev" --info 2>/dev/null || echo "")
    echo "$info" | grep -q "Video Capture" || continue
    
    name=$(echo "$info" | grep "Card type" | sed 's/.*: //' || echo "$dev")
    # Try to grab 5 frames with ffmpeg
    if command -v ffmpeg &>/dev/null; then
        result=$(timeout 3 ffmpeg -f v4l2 -video_size 320x240 -i "$dev" -frames:v 5 -f null - 2>&1 || echo "FAIL")
        if echo "$result" | grep -q "frame=.*5"; then
            echo -e "  $dev ($name): ${GREEN}OK${NC} - 5 frames captured"
        else
            echo -e "  $dev ($name): ${RED}FAILED or BUSY${NC}"
        fi
    else
        echo -e "  ${YELLOW}ffmpeg not found, skipping frame test${NC}"
        break
    fi
done
echo ""

echo -e "${BLUE}═══════════════════════════════════════════════${NC}"
echo -e "${BLUE}  Diagnostics complete${NC}"
echo -e "${BLUE}═══════════════════════════════════════════════${NC}"
