#!/bin/bash
#
# OpenWXSDR Packaging Script
# Creates tar.gz distribution package for Raspberry Pi
#

set -e

VERSION="1.0.46"
PACKAGE_NAME="openwxsdr-${VERSION}"
BUILD_DIR="build/${PACKAGE_NAME}"

echo "================================================================"
echo "  OpenWXSDR v${VERSION} - Build Distribution Package"
echo "================================================================"
echo ""

# Clean previous build
if [ -d "build" ]; then
    echo "Cleaning previous build..."
    rm -rf build
fi

# Create build directory structure
echo "Creating package structure..."
mkdir -p "${BUILD_DIR}"
mkdir -p "${BUILD_DIR}/src/decoders"
mkdir -p "${BUILD_DIR}/src/sdr"
mkdir -p "${BUILD_DIR}/src/output"
mkdir -p "${BUILD_DIR}/src/webui"
mkdir -p "${BUILD_DIR}/templates"
mkdir -p "${BUILD_DIR}/scripts"
mkdir -p "${BUILD_DIR}/docs"

# Copy source files
echo "Copying source files..."
cp -r src/*.py "${BUILD_DIR}/src/"
cp -r src/decoders/*.py "${BUILD_DIR}/src/decoders/"
cp -r src/sdr/*.py "${BUILD_DIR}/src/sdr/"
cp -r src/output/*.py "${BUILD_DIR}/src/output/"
cp -r src/webui/*.py "${BUILD_DIR}/src/webui/"

# Copy templates
echo "Copying templates..."
cp templates/*.html "${BUILD_DIR}/templates/"

# Copy scripts
echo "Copying scripts..."
cp scripts/*.sh "${BUILD_DIR}/scripts/" 2>/dev/null || true
cp scripts/*.py "${BUILD_DIR}/scripts/" 2>/dev/null || true

# Copy documentation
echo "Copying documentation..."
cp docs/*.md "${BUILD_DIR}/docs/" 2>/dev/null || true
cp *.md "${BUILD_DIR}/" 2>/dev/null || true

# Copy configuration and root files
echo "Copying configuration files..."
cp openwxsdr.py "${BUILD_DIR}/"
cp config.yaml "${BUILD_DIR}/"
cp requirements.txt "${BUILD_DIR}/"
cp LICENSE "${BUILD_DIR}/"

# Copy test scripts
echo "Copying test scripts..."
cp test_*.sh "${BUILD_DIR}/" 2>/dev/null || true
cp diagnose_*.sh "${BUILD_DIR}/" 2>/dev/null || true
cp check_*.sh "${BUILD_DIR}/" 2>/dev/null || true
cp fix_*.sh "${BUILD_DIR}/" 2>/dev/null || true
cp verify_*.sh "${BUILD_DIR}/" 2>/dev/null || true

# Copy release notes
echo "Copying release documentation..."
cp RELEASE_v${VERSION}.txt "${BUILD_DIR}/" 2>/dev/null || true
cp CHANGELOG.md "${BUILD_DIR}/"

# Create installation script
echo "Creating installation script..."
cat > "${BUILD_DIR}/install.sh" << 'INSTALL_EOF'
#!/bin/bash
#
# OpenWXSDR Installation Script
# For Raspberry Pi running Debian/Raspbian
#

set -e

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

INSTALL_DIR="/home/pi/OpenWXSDR"
SERVICE_USER="pi"

echo "================================================================"
echo "  OpenWXSDR v1.0.17 Installation"
echo "================================================================"
echo ""

# Check if running on Raspberry Pi
if [ ! -f /proc/device-tree/model ]; then
    echo -e "${YELLOW}Warning: Not running on Raspberry Pi${NC}"
    read -p "Continue anyway? (y/n) " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        exit 1
    fi
fi

# Check prerequisites
echo "Step 1: Checking system prerequisites..."
echo "------------------------------------------------------------"

if ! command -v python3 &> /dev/null; then
    echo -e "${RED}✗${NC} Python 3 not found"
    echo "Install with: sudo apt-get install python3 python3-pip"
    exit 1
fi
echo -e "${GREEN}✓${NC} Python 3 found: $(python3 --version)"

if ! command -v rtl_sdr &> /dev/null; then
    echo -e "${YELLOW}!${NC} rtl-sdr not found - will install"
    INSTALL_RTL=1
else
    echo -e "${GREEN}✓${NC} rtl-sdr found"
    INSTALL_RTL=0
fi

if ! command -v sox &> /dev/null; then
    echo -e "${YELLOW}!${NC} sox not found - will install"
    INSTALL_SOX=1
else
    echo -e "${GREEN}✓${NC} sox found"
    INSTALL_SOX=0
fi

echo ""

# Install system packages
if [ $INSTALL_RTL -eq 1 ] || [ $INSTALL_SOX -eq 1 ]; then
    echo "Step 2: Installing system packages..."
    echo "------------------------------------------------------------"
    sudo apt-get update
    
    if [ $INSTALL_RTL -eq 1 ]; then
        echo "Installing rtl-sdr..."
        sudo apt-get install -y rtl-sdr
    fi
    
    if [ $INSTALL_SOX -eq 1 ]; then
        echo "Installing sox..."
        sudo apt-get install -y sox
    fi
    echo ""
fi

# Install Python packages
echo "Step 3: Installing Python dependencies..."
echo "------------------------------------------------------------"
if [ -f requirements.txt ]; then
    pip3 install --user -r requirements.txt
    echo -e "${GREEN}✓${NC} Python packages installed"
else
    echo -e "${YELLOW}!${NC} requirements.txt not found - skipping"
fi
echo ""

# Create installation directory
echo "Step 4: Installing OpenWXSDR..."
echo "------------------------------------------------------------"

if [ -d "$INSTALL_DIR" ]; then
    echo -e "${YELLOW}!${NC} $INSTALL_DIR already exists"
    read -p "Backup and replace? (y/n) " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        BACKUP_DIR="${INSTALL_DIR}_backup_$(date +%Y%m%d_%H%M%S)"
        echo "Backing up to $BACKUP_DIR..."
        sudo mv "$INSTALL_DIR" "$BACKUP_DIR"
    else
        echo "Installation cancelled"
        exit 1
    fi
fi

echo "Creating installation directory..."
sudo mkdir -p "$INSTALL_DIR"
sudo chown $SERVICE_USER:$SERVICE_USER "$INSTALL_DIR"

echo "Copying files..."
cp -r ./* "$INSTALL_DIR/"
sudo chown -R $SERVICE_USER:$SERVICE_USER "$INSTALL_DIR"

# Make scripts executable
chmod +x "$INSTALL_DIR/openwxsdr.py"
chmod +x "$INSTALL_DIR/scripts/"*.sh 2>/dev/null || true
chmod +x "$INSTALL_DIR/"*.sh 2>/dev/null || true

echo -e "${GREEN}✓${NC} Files installed to $INSTALL_DIR"
echo ""

# Install decoders
echo "Step 5: Installing rs1729 decoders..."
echo "------------------------------------------------------------"

DECODER_DIR="$INSTALL_DIR/decoders/rs1729"
if [ ! -d "$DECODER_DIR" ]; then
    echo "Creating decoder directory..."
    mkdir -p "$DECODER_DIR"
    
    echo ""
    echo -e "${YELLOW}Important: rs1729 decoder binaries not included${NC}"
    echo ""
    echo "You need to download and compile the rs1729 decoders:"
    echo "  1. git clone https://github.com/rs1729/RS.git"
    echo "  2. cd RS/demod/mod"
    echo "  3. gcc -O2 rs41mod.c -lm -o rs41mod"
    echo "  4. Copy binaries to: $DECODER_DIR"
    echo ""
    echo "Required decoders:"
    echo "  - rs41mod (RS41 radiosondes)"
    echo "  - rs92mod (RS92 radiosondes)"
    echo "  - dfm09mod (DFM radiosondes)"
    echo "  - m10mod, m20mod, imet54mod (optional)"
    echo ""
    read -p "Press Enter to continue..."
else
    echo -e "${GREEN}✓${NC} Decoder directory exists"
fi
echo ""

# Configure RTL-SDR permissions
echo "Step 6: Configuring RTL-SDR permissions..."
echo "------------------------------------------------------------"

if [ -f "$INSTALL_DIR/scripts/fix_rtlsdr_permissions.sh" ]; then
    bash "$INSTALL_DIR/scripts/fix_rtlsdr_permissions.sh"
else
    echo "Creating udev rules for RTL-SDR..."
    sudo tee /etc/udev/rules.d/20-rtlsdr.rules > /dev/null << 'UDEV_EOF'
# RTL-SDR
SUBSYSTEM=="usb", ATTRS{idVendor}=="0bda", ATTRS{idProduct}=="2838", GROUP="plugdev", MODE="0666", SYMLINK+="rtl_sdr"
UDEV_EOF
    sudo udevadm control --reload-rules
    sudo udevadm trigger
    echo -e "${GREEN}✓${NC} RTL-SDR udev rules installed"
fi
echo ""

# Install systemd service
echo "Step 7: Installing systemd service..."
echo "------------------------------------------------------------"

sudo tee /etc/systemd/system/openwxsdr.service > /dev/null << SERVICE_EOF
[Unit]
Description=OpenWXSDR Radiosonde Decoder
After=network.target

[Service]
Type=simple
User=$SERVICE_USER
WorkingDirectory=$INSTALL_DIR
ExecStart=/usr/bin/python3 $INSTALL_DIR/openwxsdr.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
SERVICE_EOF

sudo systemctl daemon-reload
echo -e "${GREEN}✓${NC} Systemd service installed"
echo ""

# Configure config.yaml
echo "Step 8: Configuring OpenWXSDR..."
echo "------------------------------------------------------------"

if [ -f "$INSTALL_DIR/config.yaml" ]; then
    echo "Configuration file: $INSTALL_DIR/config.yaml"
    echo ""
    echo "Important settings to review:"
    echo "  - sdr.rtlsdr.device_index (default: 0)"
    echo "  - sdr.rtlsdr.gain (default: 40)"
    echo "  - sdr.rtlsdr.sample_rate (default: 2400000)"
    echo "  - spectrum.scan_range (default: 400-406 MHz)"
    echo "  - decoders.rs1729_path (default: $INSTALL_DIR/decoders/rs1729)"
    echo ""
    read -p "Edit configuration now? (y/n) " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        ${EDITOR:-nano} "$INSTALL_DIR/config.yaml"
    fi
else
    echo -e "${RED}✗${NC} config.yaml not found"
fi
echo ""

# Installation complete
echo "================================================================"
echo "  Installation Complete!"
echo "================================================================"
echo ""
echo "Next steps:"
echo ""
echo "1. Install decoder binaries:"
echo "   See: $INSTALL_DIR/docs/INSTALLATION.md"
echo ""
echo "2. Edit configuration:"
echo "   nano $INSTALL_DIR/config.yaml"
echo ""
echo "3. Test RTL-SDR:"
echo "   rtl_test"
echo ""
echo "4. Test pipeline (with active radiosonde):"
echo "   cd $INSTALL_DIR"
echo "   ./test_v1.0.17_fifo.sh"
echo ""
echo "5. Start service:"
echo "   sudo systemctl start openwxsdr"
echo ""
echo "6. Enable auto-start:"
echo "   sudo systemctl enable openwxsdr"
echo ""
echo "7. Monitor logs:"
echo "   sudo journalctl -u openwxsdr -f"
echo ""
echo "Web interface will be available at:"
echo "   http://$(hostname -I | awk '{print $1}'):8080"
echo ""
INSTALL_EOF

chmod +x "${BUILD_DIR}/install.sh"

# Create README for package
echo "Creating package README..."
cat > "${BUILD_DIR}/README_PACKAGE.txt" << 'README_EOF'
================================================================================
  OpenWXSDR v1.0.17 - Distribution Package
  Open Weather Data Receiver for Radiosondes
================================================================================

This package contains everything needed to install OpenWXSDR on a Raspberry Pi.

CONTENTS:
---------
- openwxsdr.py          Main application
- src/                  Source code modules
- templates/            Web UI templates
- scripts/              Utility scripts
- docs/                 Documentation
- config.yaml           Configuration file
- requirements.txt      Python dependencies
- install.sh            Installation script
- test_*.sh             Test scripts

QUICK START:
------------
1. Extract package:
   tar -xzf openwxsdr-1.0.17.tar.gz
   cd openwxsdr-1.0.17

2. Run installer:
   ./install.sh

3. Follow on-screen instructions

HARDWARE REQUIREMENTS:
----------------------
- Raspberry Pi (3B+ or newer recommended)
- RTL-SDR USB dongle (RTL2832U + R820T/R820T2)
- Antenna for 400-406 MHz
- Internet connection (for installation)

SOFTWARE REQUIREMENTS:
----------------------
- Raspbian/Debian OS
- Python 3.7+
- rtl-sdr package
- sox package
- rs1729 decoder binaries (separate download)

INSTALLATION GUIDE:
-------------------
Full installation instructions: docs/INSTALLATION.md

Decoder binaries must be downloaded separately:
  https://github.com/rs1729/RS

DOCUMENTATION:
--------------
- INSTALLATION.md       Installation guide
- QUICKSTART.md         Quick start guide
- CONFIGURATION.md      Configuration reference
- TROUBLESHOOTING.md    Common issues
- API.md                API documentation
- RELEASE_v1.0.17.txt   Release notes

TESTING:
--------
Test scripts included:
- test_v1.0.17_fifo.sh       Test named pipe streaming
- diagnose_pipeline.sh       Pipeline diagnostics
- verify_installation.sh     Installation verification

SUPPORT:
--------
For issues or questions:
- Check docs/TROUBLESHOOTING.md
- Review system logs: journalctl -u openwxsdr
- Test with diagnostic scripts

VERSION INFORMATION:
--------------------
Version: 1.0.17
Release Date: 2026-05-01
Architecture: Named pipe WAV format streaming
Key Features:
  - rtl_sdr at 2.4 MSPS
  - sox resampling to 48 kHz
  - Named pipe (FIFO) WAV streaming
  - rs1729 decoder integration
  - Web UI for monitoring
  - UDP output for integration

CHANGELOG:
----------
See CHANGELOG.md for version history and changes.

LICENSE:
--------
See LICENSE file for licensing information.

================================================================================
End of README
================================================================================
README_EOF

# Create quick reference card
cat > "${BUILD_DIR}/QUICKREF.txt" << 'QUICKREF_EOF'
================================================================================
  OpenWXSDR v1.0.17 - Quick Reference Card
================================================================================

INSTALLATION:
  ./install.sh

SERVICE CONTROL:
  sudo systemctl start openwxsdr       # Start service
  sudo systemctl stop openwxsdr        # Stop service
  sudo systemctl restart openwxsdr     # Restart service
  sudo systemctl enable openwxsdr      # Auto-start on boot
  sudo systemctl status openwxsdr      # Check status

MONITORING:
  sudo journalctl -u openwxsdr -f      # Live logs
  sudo journalctl -u openwxsdr --since "1 hour ago"

CONFIGURATION:
  nano /home/pi/OpenWXSDR/config.yaml

TESTING:
  rtl_test                             # Test RTL-SDR hardware
  ./test_v1.0.17_fifo.sh              # Test pipeline
  ./diagnose_pipeline.sh               # Full diagnostics
  ./verify_installation.sh             # Verify setup

WEB INTERFACE:
  http://<raspberry-pi-ip>:8080

KEY FILES:
  /home/pi/OpenWXSDR/                  # Installation directory
  /home/pi/OpenWXSDR/config.yaml       # Configuration
  /etc/systemd/system/openwxsdr.service  # Service file
  /tmp/openwxsdr_*.wav                 # Named pipes (while running)

COMMON ISSUES:
  
  No RTL-SDR detected:
    - Check USB connection
    - Run: lsusb | grep Realtek
    - Check permissions: ls -l /dev/bus/usb/*/*
  
  Decoder crashes (exit code 255):
    - Verify decoder binaries exist
    - Check decoder has execute permission
    - Test manually: /path/to/rs41mod --help
  
  No signals detected:
    - Verify antenna connection
    - Check frequency range in config.yaml
    - Test with rtl_power
    - Verify radiosonde launch schedule
  
  Web UI not accessible:
    - Check service is running
    - Verify port 8080 is open
    - Check firewall settings

DECODER COMPILATION:
  git clone https://github.com/rs1729/RS.git
  cd RS/demod/mod
  gcc -O2 rs41mod.c -lm -o rs41mod
  gcc -O2 rs92mod.c -lm -o rs92mod
  gcc -O2 dfm09mod.c -lm -o dfm09mod
  cp *mod /home/pi/OpenWXSDR/decoders/rs1729/

PIPELINE ARCHITECTURE:
  rtl_sdr (2.4 MSPS) → sox → WAV FIFO → decoder → telemetry

USEFUL COMMANDS:
  # Check named pipes
  ls -l /tmp/openwxsdr_*.wav
  
  # Monitor processes
  ps aux | grep -E "rtl_sdr|sox|rs41mod"
  
  # Check RTL-SDR device
  rtl_test -t
  
  # Manual spectrum scan
  rtl_power -f 400M:406M:8k -i 1 -g 40 scan.csv

================================================================================
EOF
QUICKREF_EOF

# Create manifest
echo "Creating package manifest..."
cat > "${BUILD_DIR}/MANIFEST.txt" << MANIFEST_EOF
OpenWXSDR v${VERSION} - Package Manifest
Generated: $(date)

ROOT FILES:
$(ls -1 "${BUILD_DIR}" | grep -v "^src$\|^templates$\|^scripts$\|^docs$" | sed 's/^/  /')

SOURCE CODE:
$(find "${BUILD_DIR}/src" -type f -name "*.py" | sed "s|${BUILD_DIR}/||" | sed 's/^/  /')

TEMPLATES:
$(find "${BUILD_DIR}/templates" -type f | sed "s|${BUILD_DIR}/||" | sed 's/^/  /')

SCRIPTS:
$(find "${BUILD_DIR}/scripts" -type f 2>/dev/null | sed "s|${BUILD_DIR}/||" | sed 's/^/  /' || echo "  (none)")

DOCUMENTATION:
$(find "${BUILD_DIR}/docs" -type f 2>/dev/null | sed "s|${BUILD_DIR}/||" | sed 's/^/  /' || echo "  (none)")

TOTAL FILES: $(find "${BUILD_DIR}" -type f | wc -l)
PACKAGE SIZE: $(du -sh "${BUILD_DIR}" | cut -f1)
MANIFEST_EOF

# Create tarball
echo ""
echo "Creating tar.gz archive..."
cd build
tar -czf "${PACKAGE_NAME}.tar.gz" "${PACKAGE_NAME}"
cd ..

# Calculate checksums
echo "Generating checksums..."
cd build
sha256sum "${PACKAGE_NAME}.tar.gz" > "${PACKAGE_NAME}.tar.gz.sha256"
md5sum "${PACKAGE_NAME}.tar.gz" > "${PACKAGE_NAME}.tar.gz.md5"
cd ..

# Display results
echo ""
echo "================================================================"
echo "  Package Build Complete!"
echo "================================================================"
echo ""
echo "Package: build/${PACKAGE_NAME}.tar.gz"
echo "Size: $(du -h build/${PACKAGE_NAME}.tar.gz | cut -f1)"
echo "Files: $(find ${BUILD_DIR} -type f | wc -l)"
echo ""
echo "Checksums:"
echo "  SHA256: $(cat build/${PACKAGE_NAME}.tar.gz.sha256 | cut -d' ' -f1)"
echo "  MD5:    $(cat build/${PACKAGE_NAME}.tar.gz.md5 | cut -d' ' -f1)"
echo ""
echo "To deploy to Raspberry Pi:"
echo "  1. scp build/${PACKAGE_NAME}.tar.gz pi@raspberry-pi:~/"
echo "  2. ssh pi@raspberry-pi"
echo "  3. tar -xzf ${PACKAGE_NAME}.tar.gz"
echo "  4. cd ${PACKAGE_NAME}"
echo "  5. ./install.sh"
echo ""
echo "Package contents:"
echo "  - Complete source code"
echo "  - Configuration template"
echo "  - Installation scripts"
echo "  - Test and diagnostic tools"
echo "  - Documentation"
echo "  - Service files"
echo ""
echo "================================================================"
