#!/bin/bash
# flux242 Radiosonde Setup Script
# Installs and compiles flux242/radiosonde for OpenWXSDR integration

set -e

echo "================================================================"
echo "  flux242/radiosonde Setup for OpenWXSDR"
echo "================================================================"
echo ""

# Configuration
INSTALL_DIR="${HOME}/radiosonde"
FLUX242_REPO="https://github.com/flux242/radiosonde.git"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Check if running as root
if [ "$EUID" -eq 0 ]; then
    echo -e "${RED}ERROR: Do not run this script as root!${NC}"
    echo "Run as normal user: ./install_flux242.sh"
    exit 1
fi

echo "This script will:"
echo "  1. Install required dependencies"
echo "  2. Clone flux242/radiosonde repository"
echo "  3. Compile decoders and iq_server"
echo "  4. Verify installation"
echo ""
read -p "Continue? (y/n) " -n 1 -r
echo
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo "Installation cancelled."
    exit 1
fi

echo ""
echo "Step 1/4: Installing dependencies..."
echo "----------------------------------------"

# Install dependencies
sudo apt-get update
sudo apt-get install -y \
    rtl-sdr \
    librtlsdr-dev \
    gawk \
    bash \
    socat \
    jq \
    sox \
    build-essential \
    git \
    pkg-config

if [ $? -eq 0 ]; then
    echo -e "${GREEN}✓${NC} Dependencies installed successfully"
else
    echo -e "${RED}✗${NC} Failed to install dependencies"
    exit 1
fi

echo ""
echo "Step 2/4: Cloning flux242/radiosonde..."
echo "----------------------------------------"

# Check if already exists
if [ -d "$INSTALL_DIR" ]; then
    echo -e "${YELLOW}Warning: $INSTALL_DIR already exists${NC}"
    read -p "Remove and re-clone? (y/n) " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        rm -rf "$INSTALL_DIR"
    else
        echo "Skipping clone, using existing directory"
    fi
fi

if [ ! -d "$INSTALL_DIR" ]; then
    git clone "$FLUX242_REPO" "$INSTALL_DIR"
    if [ $? -eq 0 ]; then
        echo -e "${GREEN}✓${NC} Repository cloned to $INSTALL_DIR"
    else
        echo -e "${RED}✗${NC} Failed to clone repository"
        exit 1
    fi
else
    echo -e "${GREEN}✓${NC} Repository exists at $INSTALL_DIR"
fi

echo ""
echo "Step 3/4: Compiling decoders..."
echo "----------------------------------------"

# Compile decoders
cd "$INSTALL_DIR/decoders"
echo "Compiling in: $(pwd)"

make clean 2>/dev/null || true
make

if [ $? -eq 0 ]; then
    echo -e "${GREEN}✓${NC} Decoders compiled successfully"
else
    echo -e "${RED}✗${NC} Failed to compile decoders"
    exit 1
fi

# Compile iq_server
echo ""
echo "Compiling iq_server channelizer..."
cd "$INSTALL_DIR/iq_svcl"
echo "Compiling in: $(pwd)"

make clean 2>/dev/null || true
make

if [ $? -eq 0 ]; then
    echo -e "${GREEN}✓${NC} iq_server compiled successfully"
else
    echo -e "${RED}✗${NC} Failed to compile iq_server"
    exit 1
fi

echo ""
echo "Step 4/4: Verifying installation..."
echo "----------------------------------------"

# Verify key files exist
ERRORS=0

echo -n "Checking receivemultisonde.sh... "
if [ -f "$INSTALL_DIR/scripts/receivemultisonde.sh" ]; then
    chmod +x "$INSTALL_DIR/scripts/receivemultisonde.sh"
    echo -e "${GREEN}✓${NC}"
else
    echo -e "${RED}✗${NC}"
    ERRORS=$((ERRORS + 1))
fi

echo -n "Checking iq_server... "
if [ -f "$INSTALL_DIR/iq_svcl/iq_server" ]; then
    chmod +x "$INSTALL_DIR/iq_svcl/iq_server"
    echo -e "${GREEN}✓${NC}"
else
    echo -e "${RED}✗${NC}"
    ERRORS=$((ERRORS + 1))
fi

echo -n "Checking defaults.conf... "
if [ -f "$INSTALL_DIR/scripts/defaults.conf" ]; then
    echo -e "${GREEN}✓${NC}"
else
    echo -e "${RED}✗${NC}"
    ERRORS=$((ERRORS + 1))
fi

# Make sure iq_server is in PATH or create symlink
echo -n "Setting up iq_server PATH... "
if [ -f "$INSTALL_DIR/iq_svcl/iq_server" ]; then
    # Create symlink in scripts directory for easy access
    ln -sf "$INSTALL_DIR/iq_svcl/iq_server" "$INSTALL_DIR/scripts/iq_server"
    echo -e "${GREEN}✓${NC}"
else
    echo -e "${RED}✗${NC}"
    ERRORS=$((ERRORS + 1))
fi

echo -n "Checking rs41mod... "
if [ -f "$INSTALL_DIR/decoders/rs41mod" ]; then
    echo -e "${GREEN}✓${NC}"
else
    echo -e "${RED}✗${NC}"
    ERRORS=$((ERRORS + 1))
fi

echo -n "Checking dfm09mod... "
if [ -f "$INSTALL_DIR/decoders/dfm09mod" ]; then
    echo -e "${GREEN}✓${NC}"
else
    echo -e "${RED}✗${NC}"
    ERRORS=$((ERRORS + 1))
fi

echo -n "Checking m10mod... "
if [ -f "$INSTALL_DIR/decoders/m10mod" ]; then
    echo -e "${GREEN}✓${NC}"
else
    echo -e "${RED}✗${NC}"
    ERRORS=$((ERRORS + 1))
fi

if [ $ERRORS -eq 0 ]; then
    echo ""
    echo -e "${GREEN}================================================================"
    echo "  Installation Complete!"
    echo "================================================================${NC}"
    echo ""
    echo "flux242/radiosonde installed to: $INSTALL_DIR"
    echo ""
    echo "IMPORTANT: Fix RTL-SDR lock file permissions:"
    echo "  sudo chmod 777 /var/run/lock"
    echo "  (or run: sudo mkdir -p /var/run/lock && sudo chmod 777 /var/run/lock)"
    echo ""
    echo "Next steps:"
    echo "  1. Fix permissions (run command above)"
    echo ""
    echo "  2. Test standalone (MUST run from scripts directory!):"
    echo "     cd $INSTALL_DIR/scripts"
    echo "     ./receivemultisonde.sh -f 403405000 -s 2400000 -P 0 -g 40 -t 4 &"
    echo ""
    echo "  3. In another terminal, watch output:"
    echo "     nc -luk 5678"
    echo ""
    echo "  IMPORTANT: Always run receivemultisonde.sh from the scripts/ directory!"
    echo "            Do NOT use absolute path from other directories."
    echo ""
    echo "  4. Configure OpenWXSDR config.yaml:"
    echo "     sdr:"
    echo "       type: 'flux242'"
    echo "       flux242:"
    echo "         script_path: '$INSTALL_DIR/scripts/receivemultisonde.sh'"
    echo "         center_freq: 403405000  # Adjust for your region"
    echo ""
    echo "  5. Start OpenWXSDR:"
    echo "     ./openwxsdr.py"
    echo ""
    echo "See docs/FLUX242_INTEGRATION.md for full documentation."
    echo ""
else
    echo ""
    echo -e "${RED}================================================================"
    echo "  Installation encountered $ERRORS error(s)"
    echo "================================================================${NC}"
    echo ""
    echo "Please check the output above and resolve any issues."
    exit 1
fi
