# OpenWXSDR Troubleshooting Guide

## Common Issues and Solutions

### RTL-SDR Not Detected

**Symptoms:**
- "RTL-SDR not available" error
- Device not found

**Solutions:**

1. **Check USB connection:**
   ```bash
   lsusb | grep Realtek
   ```
   Should show: `Realtek Semiconductor Corp. RTL2838 DVB-T`

2. **Check if drivers are loaded:**
   ```bash
   sudo rtl_test -t
   ```

3. **Unload conflicting DVB-T drivers:**
   ```bash
   sudo rmmod dvb_usb_rtl28xxu
   sudo rmmod rtl2832
   sudo rmmod rtl2830
   ```

4. **Reinstall RTL-SDR:**
   ```bash
   sudo apt-get install --reinstall rtl-sdr librtlsdr-dev
   ```

5. **Check permissions:**
   ```bash
   sudo usermod -a -G plugdev $USER
   # Log out and back in
   ```

### No Signals Detected

**Symptoms:**
- Spectrum analyzer runs but finds no signals
- "Waiting for radiosonde signals..." persists

**Solutions:**

1. **Verify frequency range:**
   - Check if radiosonde launches are active in your area
   - Visit https://radiosondy.info/ for launch schedules
   - Ensure `freq_ranges` in config matches your region

2. **Test antenna with known signal:**
   ```bash
   rtl_power -f 402.7M:403.7M:1k -i 1 -g 40 test.csv
   ```

3. **Lower detection threshold:**
   ```yaml
   detection:
     detection_threshold: 10  # Was 15
   ```

4. **Check antenna:**
   - Ensure antenna is suitable for 400 MHz
   - Try outdoor placement
   - Check for damage/loose connections

5. **Verify SDR gain:**
   ```yaml
   sdr:
     rtlsdr:
       gain: 0  # Try auto gain
   ```

### Decoders Not Starting

**Symptoms:**
- Signals detected but no decoding
- "Could not identify sonde type" warnings

**Solutions:**

1. **Verify rs1729 decoders are built:**
   ```bash
   ls -la decoders/rs1729/rs41/rs41mod
   ls -la decoders/rs1729/rs92/rs92mod
   # etc.
   ```

2. **Rebuild decoders:**
   ```bash
   cd decoders/rs1729
   cd rs41 && make clean && make && cd ..
   cd rs92 && make clean && make && cd ..
   # etc.
   ```

3. **Check decoder path in config:**
   ```yaml
   decoders:
     rs1729_path: './decoders/rs1729'  # Must be correct
   ```

### Poor Decoding Quality

**Symptoms:**
- Frames decoded but many gaps
- Position jumps around

**Solutions:**

1. **Improve signal strength:**
   - Move antenna higher/outdoors
   - Increase RTL-SDR gain (20-40)
   - Check for interference sources

2. **Adjust PPM correction:**
   ```bash
   # Test PPM error
   rtl_test -p
   # Add result to config
   ```

3. **Check CPU load:**
   ```bash
   top
   # If high, reduce max_concurrent receivers
   ```

### Web UI Not Loading

**Symptoms:**
- Cannot access web interface
- Connection refused

**Solutions:**

1. **Check if server is running:**
   ```bash
   sudo netstat -tulpn | grep 5000
   ```

2. **Check Flask logs:**
   ```bash
   tail -f logs/openwxsdr.log | grep Flask
   ```

3. **Verify firewall:**
   ```bash
   sudo ufw status
   sudo ufw allow 5000/tcp
   ```

4. **Try different port:**
   ```yaml
   webui:
     port: 8080  # Change from 5000
   ```

5. **Access via IP instead of hostname:**
   ```
   http://192.168.1.x:5000
   ```

### High CPU Usage

**Symptoms:**
- Raspberry Pi sluggish
- CPU at 100%

**Solutions:**

1. **Reduce concurrent decoders:**
   ```yaml
   receivers:
     max_concurrent: 2  # Was 4
   ```

2. **Increase scan interval:**
   ```yaml
   receivers:
     scan_interval: 10  # Was 5
   ```

3. **Lower sample rate:**
   ```yaml
   sdr:
     rtlsdr:
       sample_rate: 1200000  # Was 2400000
   ```

4. **Disable web UI if not needed:**
   ```yaml
   webui:
     enabled: false
   ```

### UDP Output Not Working

**Symptoms:**
- Data not appearing on OpenWX server
- No errors in logs

**Solutions:**

1. **Verify UDP is enabled:**
   ```yaml
   output:
     udp:
       enabled: true
   ```

2. **Check server address:**
   ```yaml
   output:
     udp:
       host: 'your.openwx.server'  # Not 127.0.0.1
       port: 55672
   ```

3. **Test UDP connectivity:**
   ```bash
   nc -u your.openwx.server 55672
   # Type test message and press Enter
   ```

4. **Check firewall on both ends:**
   ```bash
   # On Pi
   sudo ufw status
   
   # On server
   sudo ufw allow 55672/udp
   ```

5. **Verify uploader_callsign is set:**
   ```yaml
   uploader_callsign: 'YOUR_CALLSIGN'  # Required!
   ```

### systemd Service Issues

**Symptoms:**
- Service fails to start
- Service crashes repeatedly

**Solutions:**

1. **Check service status:**
   ```bash
   sudo systemctl status openwxsdr
   ```

2. **View detailed logs:**
   ```bash
   sudo journalctl -u openwxsdr -n 100 --no-pager
   ```

3. **Test manual start:**
   ```bash
   source venv/bin/activate
   python3 openwxsdr.py
   # Look for errors
   ```

4. **Check paths in service file:**
   ```bash
   cat /etc/systemd/system/openwxsdr.service
   # Verify WorkingDirectory and ExecStart paths
   ```

5. **Reinstall service:**
   ```bash
   sudo bash scripts/install_service.sh
   ```

### Memory Issues

**Symptoms:**
- Out of memory errors
- System becomes unresponsive

**Solutions:**

1. **Limit stored telemetry:**
   Edit `src/webui/web_server.py`:
   ```python
   if len(self.sondes[serial]) > 500:  # Was 1000
       self.sondes[serial] = self.sondes[serial][-500:]
   ```

2. **Increase swap:**
   ```bash
   sudo dphys-swapfile swapoff
   sudo nano /etc/dphys-swapfile
   # Set CONF_SWAPSIZE=2048
   sudo dphys-swapfile setup
   sudo dphys-swapfile swapon
   ```

3. **Monitor memory:**
   ```bash
   free -h
   watch -n 5 free -h
   ```

## Getting Help

### Collect Debug Information

```bash
# System info
uname -a
cat /proc/device-tree/model

# RTL-SDR info
rtl_test -t

# Check processes
ps aux | grep python

# Recent logs
tail -n 100 logs/openwxsdr.log

# Configuration (remove sensitive data!)
cat config.yaml
```

### Enable Debug Logging

```yaml
logging:
  level: 'DEBUG'
```

Restart and check logs:
```bash
tail -f logs/openwxsdr.log
```

### Report Issues

When reporting issues, include:
1. Raspberry Pi model
2. RTL-SDR model
3. OS version (`cat /etc/os-release`)
4. OpenWXSDR version
5. Relevant log snippets
6. What you've already tried

## Performance Tips

### Optimize for Raspberry Pi 4

```yaml
receivers:
  max_concurrent: 6
  scan_interval: 5

sdr:
  rtlsdr:
    sample_rate: 2400000
    gain: 40
```

### Optimize for Raspberry Pi 3

```yaml
receivers:
  max_concurrent: 3
  scan_interval: 8

sdr:
  rtlsdr:
    sample_rate: 1200000
    gain: 40
```

### Optimize for Raspberry Pi Zero 2W

```yaml
receivers:
  max_concurrent: 2
  scan_interval: 10

sdr:
  rtlsdr:
    sample_rate: 1200000
    gain: 40

webui:
  enabled: false  # Save resources
```
