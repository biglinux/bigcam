#!/bin/bash
set -euo pipefail
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color
echo -e "${BLUE}============================================${NC}"
echo -e "${BLUE}  GPhoto2 Webcam Controller - Installer    ${NC}"
echo -e "${BLUE}============================================${NC}"
echo ""
if [[ $EUID -eq 0 ]]; then
   echo -e "${YELLOW}Warning: Running as root. Some checks may not work correctly.${NC}"
fi
command_exists() {
    command -v "$1" &> /dev/null
}
package_installed() {
    pacman -Qi "$1" &> /dev/null
}
detect_package_manager() {
    if command_exists paru; then
        echo "paru"
    elif command_exists yay; then
        echo "yay"
    elif command_exists pacman; then
        echo "pacman"
    else
        echo "unknown"
    fi
}
PKG_MANAGER=$(detect_package_manager)
echo -e "${GREEN}Detected system:${NC} Arch Linux"
echo -e "${GREEN}Package manager:${NC} $PKG_MANAGER"
echo ""
PACMAN_PACKAGES=(
    "gphoto2"           # Camera control utility
    "libgphoto2"        # Library for camera access
    "ffmpeg"            # Video processing
    "v4l2loopback-dkms" # Virtual video device kernel module (requires DKMS)
    "python"            # Python 3
    "python-gobject"    # Python GObject bindings (PyGObject)
    "gtk4"              # GTK4 toolkit
    "libadwaita"        # Adwaita library for modern GNOME apps
    "linux-headers"     # Kernel headers for DKMS module compilation
)
OPTIONAL_PACKAGES=(
    "v4l-utils"         # Video4Linux utilities (v4l2-ctl)
)
echo -e "${YELLOW}Checking dependencies...${NC}"
echo ""
MISSING_PACKAGES=()
INSTALLED_PACKAGES=()
for pkg in "${PACMAN_PACKAGES[@]}"; do
    if package_installed "$pkg"; then
        INSTALLED_PACKAGES+=("$pkg")
        echo -e "  ${GREEN}✓${NC} $pkg"
    else
        MISSING_PACKAGES+=("$pkg")
        echo -e "  ${RED}✗${NC} $pkg ${YELLOW}(missing)${NC}"
    fi
done
echo ""
echo -e "${YELLOW}Optional packages:${NC}"
for pkg in "${OPTIONAL_PACKAGES[@]}"; do
    if package_installed "$pkg"; then
        echo -e "  ${GREEN}✓${NC} $pkg"
    else
        echo -e "  ${YELLOW}○${NC} $pkg (optional, not installed)"
    fi
done
echo ""
echo -e "${YELLOW}Checking Python modules...${NC}"
python3 -c "import gi" 2>/dev/null && echo -e "  ${GREEN}✓${NC} gi (PyGObject)" || echo -e "  ${RED}✗${NC} gi (PyGObject)"
python3 -c "from gi.repository import Gtk" 2>/dev/null && echo -e "  ${GREEN}✓${NC} Gtk" || echo -e "  ${RED}✗${NC} Gtk"
python3 -c "from gi.repository import Adw" 2>/dev/null && echo -e "  ${GREEN}✓${NC} Adw (libadwaita)" || echo -e "  ${RED}✗${NC} Adw (libadwaita)"
python3 -c "from gi.repository import GLib" 2>/dev/null && echo -e "  ${GREEN}✓${NC} GLib" || echo -e "  ${RED}✗${NC} GLib"
echo ""
echo -e "${YELLOW}Checking kernel module...${NC}"
if lsmod | grep -q "v4l2loopback"; then
    echo -e "  ${GREEN}✓${NC} v4l2loopback (loaded)"
else
    echo -e "  ${YELLOW}○${NC} v4l2loopback (not loaded - will be loaded when needed)"
fi
echo ""
if [ ${#MISSING_PACKAGES[@]} -eq 0 ]; then
    echo -e "${GREEN}All dependencies are installed!${NC}"
else
    echo -e "${YELLOW}Missing packages: ${MISSING_PACKAGES[*]}${NC}"
    echo ""

    read -p "Do you want to install the missing packages? [Y/n] " -n 1 -r
    echo ""

    if [[ $REPLY =~ ^[YySs]$ ]] || [[ -z $REPLY ]]; then
        echo -e "${BLUE}Installing packages...${NC}"
        
        if [ "$PKG_MANAGER" == "pacman" ]; then
            sudo pacman -S --needed "${MISSING_PACKAGES[@]}"
        elif [ "$PKG_MANAGER" == "yay" ]; then
            yay -S --needed "${MISSING_PACKAGES[@]}"
        elif [ "$PKG_MANAGER" == "paru" ]; then
            paru -S --needed "${MISSING_PACKAGES[@]}"
        else
            echo -e "${RED}Unsupported package manager. Install manually:${NC}"
            echo "  ${MISSING_PACKAGES[*]}"
            exit 1
        fi

        echo ""
        echo -e "${GREEN}Installation complete!${NC}"
    else
        echo -e "${YELLOW}Installation cancelled.${NC}"
        exit 0
    fi
fi
echo ""
echo -e "${BLUE}Post-install configuration...${NC}"
if groups | grep -qw "video"; then
    echo -e "  ${GREEN}✓${NC} User is in the 'video' group"
else
    echo -e "  ${YELLOW}!${NC} Adding user to the 'video' group..."
    sudo usermod -aG video "$USER"
    echo -e "  ${YELLOW}!${NC} You need to log out and log back in for this to take effect."
fi
UDEV_RULE="/etc/udev/rules.d/90-libgphoto2.rules"
if [ -f "$UDEV_RULE" ]; then
    echo -e "  ${GREEN}✓${NC} udev rule for libgphoto2 already exists"
else
    echo -e "  ${YELLOW}!${NC} Creating udev rule for camera access..."
    TMPFILE=$(mktemp /tmp/90-libgphoto2.rules.XXXXXX)
    sudo /usr/lib/libgphoto2/print-camera-list udev-rules version 201 > "$TMPFILE" 2>/dev/null || true
    if [ -s "$TMPFILE" ]; then
        sudo mv "$TMPFILE" "$UDEV_RULE"
        sudo udevadm control --reload-rules
        echo -e "  ${GREEN}✓${NC} udev rule created"
    else
        echo -e "  ${YELLOW}○${NC} udev rule not created (usually not required)"
    fi
fi
V4L2_CONF="/etc/modprobe.d/v4l2loopback.conf"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Go up from usr/share/biglinux/bigcam/script/ to the project root
REPO_ROOT="$(cd "$SCRIPT_DIR/../../../../.." && pwd)"
LOCAL_CONF="$REPO_ROOT/etc/modprobe.d/v4l2loopback.conf"
if [ -f "$LOCAL_CONF" ]; then
    if [ -f "$V4L2_CONF" ]; then
        echo -e "  ${GREEN}✓${NC} v4l2loopback configuration already exists"
    else
        echo -e "  ${YELLOW}!${NC} Installing v4l2loopback configuration for multiple consumers..."
        sudo cp "$LOCAL_CONF" "$V4L2_CONF"
        echo -e "  ${GREEN}✓${NC} Configuration installed (OBS, Meet, etc. can access it simultaneously)"
    fi
else
    echo -e "  ${YELLOW}○${NC} v4l2loopback configuration file not found in the project"
fi
echo ""
echo -e "${BLUE}============================================${NC}"
echo -e "${GREEN}Installation finished!${NC}"
echo -e "${BLUE}============================================${NC}"
echo ""
echo -e "To run the application:"
echo -e "  ${GREEN}bigcam${NC}"
echo ""
echo -e "Important notes:"
echo -e "  • Connect your camera via USB before starting"
echo -e "  • The camera must be turned on"
echo -e "  • The v4l2loopback module will be loaded automatically"
echo -e "  • A reboot may be required after the first installation"
echo ""
echo -e "${YELLOW}Checking for connected camera...${NC}"
if command_exists gphoto2; then
    CAMERA=$(gphoto2 --auto-detect 2>/dev/null | tail -n +3 | head -1)
    if [ -n "$CAMERA" ]; then
        echo -e "  ${GREEN}✓${NC} Camera detected: $CAMERA"
    else
        echo -e "  ${YELLOW}○${NC} No camera detected at the moment"
    fi
else
    echo -e "  ${RED}✗${NC} gphoto2 is not installed"
fi
echo ""
