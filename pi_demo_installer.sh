#!/bin/bash

set -e

# Color codes
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[1;34m'
NC='\033[0m' # No Color

log_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}
log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}
log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}
log_step() {
    echo -e "\n${BLUE}==============================="
    echo -e ">>> $1"
    echo -e "===============================${NC}\n"
}

SCRIPT_PATH="$(realpath "$0")"
SCRIPT_DIR="$(dirname "$SCRIPT_PATH")"

# =============================
# 1. Welcome
# =============================
echo -e "${YELLOW}========================================="
echo -e " Dream Recorder DEMO Installer "
echo -e " Simplified Demo Version "
echo -e "=========================================${NC}"

# =============================
# 2. Update System
# =============================
log_step "Updating System Packages"
sudo apt update

# =============================
# 3. Install Python and Dependencies
# =============================
log_step "Installing Python and pip"
sudo apt install -y python3 python3-pip python3-venv

# =============================
# 4. Create Virtual Environment
# =============================
log_step "Creating Python Virtual Environment"
VENV_DIR="$SCRIPT_DIR/demo_venv"
if [ ! -d "$VENV_DIR" ]; then
    python3 -m venv "$VENV_DIR"
    log_info "Virtual environment created at $VENV_DIR"
else
    log_info "Virtual environment already exists at $VENV_DIR"
fi

# =============================
# 5. Install Python Requirements
# =============================
log_step "Installing Python Requirements"
source "$VENV_DIR/bin/activate"

# Create minimal requirements file for demo
cat > "$SCRIPT_DIR/demo_requirements.txt" <<EOL
Flask==2.3.3
flask-socketio==5.3.5
python-socketio==5.9.0
python-engineio==4.7.1
gevent==23.9.1
gevent-websocket==0.10.1
EOL

pip install --upgrade pip
pip install -r "$SCRIPT_DIR/demo_requirements.txt"
log_info "Python requirements installed"

# =============================
# 6. Create Demo Service
# =============================
log_step "Setting up Demo Service"

# Create systemd service for demo app
sudo tee /etc/systemd/system/dream-recorder-demo.service > /dev/null <<EOL
[Unit]
Description=Dream Recorder Demo Application
After=network.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$SCRIPT_DIR
Environment="PATH=$VENV_DIR/bin"
ExecStart=$VENV_DIR/bin/python $SCRIPT_DIR/demo_app.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOL

log_info "Created systemd service for demo app"

# =============================
# 7. Enable and Start Service
# =============================
log_step "Enabling and Starting Demo Service"
sudo systemctl daemon-reload
sudo systemctl enable dream-recorder-demo.service
sudo systemctl start dream-recorder-demo.service
log_info "Demo service enabled and started"

# =============================
# 8. Install Chromium Browser
# =============================
log_step "Installing Chromium Browser"
if command -v chromium-browser &> /dev/null; then
    log_info "Chromium is already installed"
    BROWSER_CMD="chromium-browser"
elif command -v chromium &> /dev/null; then
    log_info "Chromium is already installed"
    BROWSER_CMD="chromium"
else
    sudo apt install -y chromium-browser
    BROWSER_CMD="chromium-browser"
    log_info "Chromium browser installed"
fi

# =============================
# 9. Setup Auto-start in Kiosk Mode
# =============================
log_step "Setting up Kiosk Mode Auto-start"

# Create autostart directory
AUTOSTART_DIR="$HOME/.config/autostart"
mkdir -p "$AUTOSTART_DIR"

# Create desktop entry for kiosk mode
KIOSK_DESKTOP_FILE="$AUTOSTART_DIR/dream_recorder_demo_kiosk.desktop"
cat > "$KIOSK_DESKTOP_FILE" <<EOL
[Desktop Entry]
Type=Application
Name=Dream Recorder Demo Kiosk
Exec=$BROWSER_CMD --kiosk --no-first-run --disable-session-crashed-bubble --disable-infobars --app=http://localhost:5000
Hidden=false
X-GNOME-Autostart-enabled=true
EOL

log_info "Created kiosk mode autostart entry"

# =============================
# 10. Disable Screen Blanking
# =============================
log_step "Disabling Screen Blanking"

# Create script to disable screen blanking
SCREEN_SCRIPT="$HOME/disable-screen-blanking.sh"
cat > "$SCREEN_SCRIPT" <<'EOL'
#!/bin/bash
# Disable screen saver
xset s off
xset s noblank
# Disable DPMS (Display Power Management Signaling)
xset -dpms
# Prevent screen from going blank
setterm -blank 0 -powersave off -powerdown 0 2>/dev/null || true
EOL
chmod +x "$SCREEN_SCRIPT"

# Create autostart entry for screen blanking disable
BLANKING_AUTOSTART="$AUTOSTART_DIR/disable-screen-blanking.desktop"
cat > "$BLANKING_AUTOSTART" <<EOL
[Desktop Entry]
Type=Application
Name=Disable Screen Blanking
Exec=$SCREEN_SCRIPT
Hidden=false
X-GNOME-Autostart-enabled=true
EOL

log_info "Screen blanking disabled"

# =============================
# 11. Configure Boot Options
# =============================
log_step "Configuring Boot Options"

# Check if we're on Raspberry Pi OS
if [ -f /boot/config.txt ] || [ -f /boot/firmware/config.txt ]; then
    log_info "Configuring Raspberry Pi boot options..."
    
    # Determine config.txt location
    if [ -f /boot/firmware/config.txt ]; then
        CONFIG_FILE="/boot/firmware/config.txt"
    else
        CONFIG_FILE="/boot/config.txt"
    fi
    
    # Disable overscan if not already set
    if ! grep -q "^disable_overscan=1" "$CONFIG_FILE"; then
        echo "disable_overscan=1" | sudo tee -a "$CONFIG_FILE" > /dev/null
        log_info "Disabled overscan"
    fi
    
    # Set HDMI mode for better compatibility
    if ! grep -q "^hdmi_force_hotplug=1" "$CONFIG_FILE"; then
        echo "hdmi_force_hotplug=1" | sudo tee -a "$CONFIG_FILE" > /dev/null
        log_info "Forced HDMI hotplug"
    fi
fi

# =============================
# 12. Create Uninstaller Script
# =============================
log_step "Creating Uninstaller Script"

cat > "$SCRIPT_DIR/pi_demo_uninstaller.sh" <<'EOL'
#!/bin/bash

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo -e "${YELLOW}Dream Recorder Demo Uninstaller${NC}"
echo -e "${RED}This will remove the demo installation. Continue? (y/n)${NC}"
read -r response

if [[ "$response" != "y" ]]; then
    echo "Uninstall cancelled."
    exit 0
fi

SCRIPT_DIR="$(dirname "$(realpath "$0")")"

# Stop and disable service
sudo systemctl stop dream-recorder-demo.service 2>/dev/null || true
sudo systemctl disable dream-recorder-demo.service 2>/dev/null || true
sudo rm -f /etc/systemd/system/dream-recorder-demo.service
sudo systemctl daemon-reload

# Remove autostart entries
rm -f "$HOME/.config/autostart/dream_recorder_demo_kiosk.desktop"
rm -f "$HOME/.config/autostart/disable-screen-blanking.desktop"
rm -f "$HOME/disable-screen-blanking.sh"

# Remove virtual environment
rm -rf "$SCRIPT_DIR/demo_venv"
rm -f "$SCRIPT_DIR/demo_requirements.txt"

echo -e "${GREEN}Demo uninstalled successfully!${NC}"
echo -e "${YELLOW}Note: Demo application files have been kept in place.${NC}"
EOL

chmod +x "$SCRIPT_DIR/pi_demo_uninstaller.sh"
log_info "Created uninstaller script at $SCRIPT_DIR/pi_demo_uninstaller.sh"

# =============================
# 13. Final Summary
# =============================
log_step "Installation Complete!"

# ASCII art
cat <<'EOF'
   ____                         ____                          _           
  |  _ \ _ __ ___  __ _ _ __ __|  _ \ ___  ___ ___  _ __ ___| | ___ _ __ 
  | | | | '__/ _ \/ _` | '_ ` _ \ |_) / _ \/ __/ _ \| '__/ _` |/ _ \ '__|
  | |_| | | |  __/ (_| | | | | | |  _ <  __/ (_| (_) | | | (_| |  __/ |   
  |____/|_|  \___|\__,_|_| |_| |_|_| \_\___|\___\___/|_|  \__,_|\___|_|   
                                                                           
                            D E M O   M O D E
EOF

echo ""
echo -e "${GREEN}=========================================${NC}"
echo -e "${GREEN}Demo installation successful!${NC}"
echo -e "${GREEN}=========================================${NC}"
echo ""
echo -e "The demo will:"
echo -e "  • Cycle through logo, icons, and clock"
echo -e "  • Change backgrounds every ~10 seconds"
echo -e "  • Start automatically on boot in kiosk mode"
echo ""
echo -e "${YELLOW}Service Status:${NC}"
sudo systemctl status dream-recorder-demo.service --no-pager | head -n 5
echo ""
echo -e "${YELLOW}Access the demo at: ${GREEN}http://localhost:5000${NC}"
echo ""
echo -e "${YELLOW}To view logs:${NC} sudo journalctl -u dream-recorder-demo.service -f"
echo -e "${YELLOW}To restart service:${NC} sudo systemctl restart dream-recorder-demo.service"
echo -e "${YELLOW}To uninstall:${NC} ./pi_demo_uninstaller.sh"
echo ""
echo -e "${RED}Please reboot for all changes to take effect!${NC}"
echo -e "${YELLOW}Run: ${GREEN}sudo reboot${NC}"
