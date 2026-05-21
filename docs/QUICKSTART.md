# OpenWXSDR Quick Start Guide

## 5-Minute Setup

### Prerequisites
- Raspberry Pi 4 (8GB recommended)
- RTL-SDR dongle
- Antenna suitable for 400 MHz
- Raspberry Pi OS (Debian Trixie or newer)

### Installation

```bash
# Clone repository
cd /home/pi
git clone <your-repo-url> OpenWXSDR
cd OpenWXSDR

# Run installation script
chmod +x install.sh
bash install.sh
```

The installer will:
- Install system dependencies
- Download and build rs1729/RS decoders
- Set up Python environment
- Test RTL-SDR connection

### Configuration

Edit `config.yaml`:

```bash
nano config.yaml
```

**Minimum required changes:**

1. Set your location (for map centering):
   ```yaml
   webui:
     map:
       default_lat: 51.5074  # Your latitude
       default_lon: -0.1278  # Your longitude
   ```

2. Set your callsign:
   ```yaml
   uploader_callsign: 'YOUR_CALLSIGN'
   ```

3. Set OpenWX server (if uploading):
   ```yaml
   output:
     udp:
       enabled: true
       host: 'your.openwx.server'  # Or 127.0.0.1 for local testing
       port: 55672
   ```

### First Run

```bash
# Activate Python environment
source venv/bin/activate

# Run OpenWXSDR
python3 openwxsdr.py
```

**You should see:**
```
==========================================
  OpenWXSDR - Streamlined Radiosonde Decoder Framework
  Version 1.0.45
==========================================

2026-04-30 12:34:56 - OpenWXSDR - INFO - Initializing OpenWXSDR...
2026-04-30 12:34:56 - OpenWXSDR - INFO - Initializing RTL-SDR...
2026-04-30 12:34:56 - SpectrumAnalyzer - INFO - RTL-SDR initialized: 402.700 MHz, 2.40 MSPS, gain=40
...
2026-04-30 12:34:57 - WebUI - INFO - Web UI started at http://0.0.0.0:5000
2026-04-30 12:34:57 - OpenWXSDR - INFO - OpenWXSDR started successfully!
```

### Access Web Interface

Open your browser:
```
http://<raspberry-pi-ip>:5000
```

For example:
```
http://192.168.1.100:5000
```

You should see:
- Interactive map
- System status (active sondes, frames)
- List of detected sondes (when available)

### Verify It's Working

1. **Check spectrum scanning:**
   ```bash
   tail -f logs/openwxsdr.log | grep "signal detected"
   ```

2. **Check web UI:**
   - Open browser to web interface
   - Status should show "0 Active Sondes" initially
   - Wait for radiosondes in your area

3. **Test with rtl_power:**
   ```bash
   rtl_power -f 402.5M:403M:1k -i 1 -g 40 test.csv
   # Should show frequency spectrum data
   ```

### Install as Service (Optional)

To run automatically on boot:

```bash
sudo bash scripts/install_service.sh
sudo systemctl enable openwxsdr
sudo systemctl start openwxsdr
```

Check status:
```bash
sudo systemctl status openwxsdr
```

View logs:
```bash
sudo journalctl -u openwxsdr -f
```

## Common First-Run Issues

### "RTL-SDR not available"

```bash
# Check if device is detected
lsusb | grep Realtek

# Test with rtl_test
sudo rtl_test -t

# If not working, unload conflicting drivers
sudo rmmod dvb_usb_rtl28xxu
```

### "No signals detected"

- Verify antenna is connected
- Check if radiosondes are active in your area: https://radiosondy.info/
- Try lowering detection threshold in config:
  ```yaml
  detection:
    detection_threshold: 10
  ```

### Web UI not accessible

```bash
# Check if Flask is running
sudo netstat -tulpn | grep 5000

# Try accessing via IP instead of hostname
# Check firewall
sudo ufw allow 5000/tcp
```

## Next Steps

### Optimize Configuration

See [CONFIGURATION.md](docs/CONFIGURATION.md) for detailed options:
- Adjust frequency ranges for your region
- Tune detection thresholds
- Configure max concurrent decoders

### Monitor Performance

```bash
# System resources
htop

# Disk usage
df -h

# Logs
tail -f logs/openwxsdr.log
```

### Join OpenWX Network

1. Register at OpenWX.de
2. Get your station credentials
3. Configure UDP output in config.yaml
4. Your decoded sondes will appear on OpenWX map!

## Quick Reference

### Start/Stop

```bash
# Manual start
source venv/bin/activate
python3 openwxsdr.py

# Service
sudo systemctl start openwxsdr
sudo systemctl stop openwxsdr
sudo systemctl restart openwxsdr
```

### Check Status

```bash
# Service status
sudo systemctl status openwxsdr

# Recent logs
tail -n 50 logs/openwxsdr.log

# Active Python processes
ps aux | grep openwxsdr.py

# Network connections
sudo netstat -tulpn | grep python
```

### Test Components

```bash
source venv/bin/activate
python3 scripts/test_components.py
```

## Getting Help

- **Documentation:** See `docs/` folder
- **Configuration:** [CONFIGURATION.md](docs/CONFIGURATION.md)
- **Troubleshooting:** [TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md)
- **API Reference:** [API.md](docs/API.md)

## Tips for Success

1. **Antenna placement:** Higher is better, outdoor is best
2. **Timing:** Check https://radiosondy.info/ for launch schedules
3. **Frequency:** Verify your region's radiosonde frequencies
4. **Interference:** Keep away from WiFi routers, computers
5. **Power:** Use good quality power supply for Raspberry Pi
6. **Cooling:** Ensure Pi has adequate cooling (especially Pi 4)

## Performance Expectations

**Raspberry Pi 4 8GB:**
- 6-8 concurrent decoders
- 2.4 MSPS sample rate
- ~50% CPU usage with 4 active sondes

**Raspberry Pi 3:**
- 3-4 concurrent decoders
- 1.2 MSPS sample rate recommended

**Detection Range:**
- Line-of-sight dependent
- Typical: 100-300 km with good antenna
- Maximum: 400+ km with radiosonde at high altitude

## Example: First Successful Decode

When you see this in logs:
```
2026-04-30 12:45:23 - DecoderManager - INFO - Starting RS41 decoder for 402.7000 MHz (SNR: 22.3 dB, BW: 4.8 kHz)
2026-04-30 12:45:25 - OpenWXSDR - INFO - Telemetry: RS41 T1234567 F12345 51.50740,-0.12780 Alt:15420m
```

**Congratulations!** You're decoding radiosondes. Check the web interface to see them on the map.

---

**Happy radiosonde tracking! 🎈**
