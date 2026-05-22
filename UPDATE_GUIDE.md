# OpenWX <img src="https://cdn.jsdelivr.net/npm/bootstrap-icons/icons/radar.svg" width="24"> SDR - Streamlined Radiosonde Decoder

# Python Update Package - Quick Reference

## Overview
These scripts create a lightweight update package containing **only Python files** from the `src/` directory for fast deployment to your Raspberry Pi systems. Config files and templates are preserved unchanged.

## Files Created
- **build_python_update.sh** - Linux/WSL build script
- **build_python_update.ps1** - Windows PowerShell build script
- **install_python_update.sh** - Generated installer script (embedded in tarball)

---

## Usage

### **On Windows (Development Machine)**

```powershell
# Run the PowerShell build script
.\build_python_update.ps1
```

### **On Linux/WSL (Development Machine)**

```bash
# Make the script executable
chmod +x build_python_update.sh

# Run the build script
./build_python_update.sh
```

---

## What Gets Packaged

The build script collects all `.py` files from:
- `src/openwxsdr_app.py`
- `src/__init__.py`
- `src/decoders/*.py`
- `src/output/*.py`
- `src/sdr/*.py`
- `src/webui/*.py`

**Total:** ~23 Python files

---

## Output Structure

```
build/python-update-20260522-143000/
├── openwxsdr-python-20260522-143000.tar.gz       # Update package
├── openwxsdr-python-20260522-143000.tar.gz.sha256 # Checksum
└── install_python_update.sh                       # Installer script
```

---

## Deployment Steps

### **1. Copy Files to Raspberry Pi**

```bash
# From your Windows/Linux development machine
cd build/python-update-YYYYMMDD-HHMMSS/
scp openwxsdr-python-*.tar.gz install_python_update.sh pi@<raspberry-pi-ip>:/home/pi/
```

**Example:**
```bash
scp openwxsdr-python-20260522-143000.tar.gz install_python_update.sh pi@192.168.1.100:/home/pi/
```

### **2. SSH to Raspberry Pi**

```bash
ssh pi@<raspberry-pi-ip>
```

### **3. Run Installer**

```bash
# Make installer executable
chmod +x /home/pi/install_python_update.sh

# Install update (default location: /home/pi/OpenWXSDR)
sudo /home/pi/install_python_update.sh /home/pi/openwxsdr-python-20260522-143000.tar.gz

# Or specify custom install directory
sudo /home/pi/install_python_update.sh /home/pi/openwxsdr-python-20260522-143000.tar.gz /opt/openwxsdr
```

---

## What the Installer Does

1. **Validates** installation directory and tarball
2. **Backs up** current Python files to `backups/python-backup-YYYYMMDD-HHMMSS/`
3. **Extracts** and deploys new Python files
4. **Restarts** openwxsdr service automatically
5. **Verifies** service is running

---

## Safety Features

### **Automatic Backup**
All existing Python files are backed up before deployment:
```
/home/pi/OpenWXSDR/backups/python-backup-20260522-143000/
└── src/
    ├── decoders/
    ├── output/
    ├── sdr/
    └── webui/
```

### **Config Preservation**
The following are **NOT modified**:
- `config.yaml`
- `templates/index.html`
- Device serial assignments
- SondeHub/MQTT credentials

### **Rollback**
To rollback to previous version:
```bash
cd /home/pi/OpenWXSDR
sudo systemctl stop openwxsdr
sudo rm -rf src
sudo cp -a backups/python-backup-YYYYMMDD-HHMMSS/src .
sudo systemctl start openwxsdr
```

---

## Verification

After deployment, verify the update:

```bash
# Check service status
sudo systemctl status openwxsdr

# View live logs
sudo journalctl -u openwxsdr -f

# Check for errors
sudo journalctl -u openwxsdr -n 50 | grep -i error

# Access Web UI
# Open browser: http://<raspberry-pi-ip>:5000
```

---

## Multiple Raspberry Pi Deployment

Deploy to multiple systems in one command:

```bash
# Create a list of Pi addresses
RASPBERRY_PIS=(
    "192.168.1.100"
    "192.168.1.101"
    "192.168.1.102"
    "192.168.1.103"
)

# Copy and install on all systems
for pi_ip in "${RASPBERRY_PIS[@]}"; do
    echo "Deploying to $pi_ip..."
    scp openwxsdr-python-*.tar.gz install_python_update.sh pi@$pi_ip:/home/pi/
    ssh pi@$pi_ip "chmod +x /home/pi/install_python_update.sh && sudo /home/pi/install_python_update.sh /home/pi/openwxsdr-python-*.tar.gz"
    echo "✓ $pi_ip complete"
done
```

---

## Troubleshooting

### **Permission Denied**
```bash
# Ensure installer is executable
chmod +x install_python_update.sh

# Run with sudo
sudo ./install_python_update.sh <tarball>
```

### **Service Fails to Start**
```bash
# Check logs for errors
sudo journalctl -u openwxsdr -n 100

# Verify Python dependencies
pip3 list | grep -E "yaml|flask|paho-mqtt"

# Manual restart
sudo systemctl restart openwxsdr
```

### **Wrong Install Directory**
```bash
# Specify correct path as second argument
sudo ./install_python_update.sh <tarball> /opt/openwxsdr
```

### **Tarball Not Created (Windows)**
PowerShell script requires `tar` command (included with Git for Windows):
```powershell
# Install Git for Windows
# Or use WSL: wsl ./build_python_update.sh
```

---

## Comparison: Python-Only vs Full Update

| Feature | Python-Only Update | Full Update (build_update_package.sh) |
|---------|-------------------|---------------------------------------|
| **Files** | ~23 Python files | Python + config.yaml + templates |
| **Size** | ~500 KB | ~600 KB |
| **Config** | Preserved | Merged with new keys |
| **Templates** | Preserved | Updated |
| **Use Case** | Code fixes, feature updates | New installations, config changes |
| **Speed** | Fast (30 seconds) | Slower (1-2 minutes) |
| **Risk** | Low (code only) | Medium (config merge) |

---

## Version History

### v1.0.45 (Current - May 22, 2026)
- **Fixed**: USB race condition crash after 20+ hours (threading.Lock)
- **Fixed**: Manual decoder SEGV/ABRT with 5-second USB settling delay
- **Enhanced**: Device lock prevents simultaneous USB access
- **Added**: Non-blocking lock in scanner thread

### v1.0.44 (May 12, 2026)
- UI improvements (sonde track width)

### v1.0.32 (May 5, 2026)
- DFT device selection fix
- Enhanced Flux242 logging

---

## Best Practices

1. **Test on One Pi First**
   - Deploy to a test Raspberry Pi before rolling out to all systems
   - Verify logs and functionality

2. **Schedule During Low Activity**
   - Deploy updates when few sondes are active
   - Avoid peak radiosonde launch times (00:00/12:00 UTC)

3. **Keep Backups**
   - Installer creates automatic backups
   - Don't delete backup folders immediately

4. **Monitor After Deployment**
   - Watch logs for 10-15 minutes post-deployment
   - Verify SondeHub uploads are working

5. **Document Custom Changes**
   - If you have local modifications, document them
   - Re-apply after updates if needed

---

## Questions?

For issues or questions:
1. Check `sudo journalctl -u openwxsdr -n 100`
2. Verify files in `/home/pi/OpenWXSDR/src/`
3. Test with manual decoder to verify USB stability
4. Check rollback procedure if needed

**Package built:** May 22, 2026  
**Target version:** OpenWXSDR v1.0.45+
