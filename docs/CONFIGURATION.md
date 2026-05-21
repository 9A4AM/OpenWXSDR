# OpenWXSDR Configuration Guide

## Overview

This guide explains all configuration options in `config.yaml`.

## SDR Configuration

### RTL-SDR Settings

```yaml
sdr:
  type: 'rtlsdr'
  rtlsdr:
    device_index: 0          # RTL-SDR device number (if multiple)
    center_freq: 402700000   # Center frequency in Hz
    sample_rate: 2400000     # Sample rate (2.4 MSPS recommended)
    gain: 40                 # Gain (0 = auto, or 0-50)
    ppm_error: 0             # Frequency correction in PPM
```

**Frequency Selection:**
- 400-406 MHz is the radiosonde band in most regions
- Set center frequency to middle of your region's band
- Check local regulations for exact frequencies

**Sample Rate:**
- 2.4 MSPS covers ~2.4 MHz bandwidth
- Can detect multiple sondes simultaneously
- Higher rates = more CPU usage

**Gain:**
- `0` = automatic gain control
- `20-40` = typical manual gain
- Too high = overload, too low = poor SNR

**PPM Error:**
- Corrects frequency offset of cheap dongles
- Use `rtl_test -p` to measure your dongle's PPM
- Typical values: -50 to +50

### KA9Q Radio Settings

```yaml
sdr:
  type: 'ka9q'
  ka9q:
    multicast_group: '239.1.2.3'
    port: 5004
    interface: 'eth0'
```

Use KA9Q for more advanced SDR setups with better sensitivity.

## Virtual Receiver Configuration

```yaml
receivers:
  max_concurrent: 4         # Max parallel decoders
  bandwidth: 12000          # Per-receiver bandwidth (Hz)
  scan_interval: 5          # Scan every 5 seconds
  min_signal_strength: -20  # Minimum SNR in dB
```

**max_concurrent:**
- Number of sondes to decode simultaneously
- Raspberry Pi 4: 4-8 recommended
- More = higher CPU usage

**bandwidth:**
- Bandwidth allocated per virtual receiver
- 12 kHz covers most radiosonde types
- Adjust based on your sonde types

**scan_interval:**
- How often to scan for new signals
- 5 seconds balances detection speed vs CPU
- Shorter = faster detection, more CPU

## Detection Configuration

```yaml
detection:
  freq_ranges:
    - [400000000, 406000000]  # 400-406 MHz
  fft_size: 2048
  detection_threshold: 15     # dB above noise
```

**freq_ranges:**
- List of frequency ranges to scan
- Add multiple ranges if your region uses non-contiguous bands
- Format: `[start_hz, end_hz]`

**Common Frequency Ranges by Region:**
- Europe: 400.15-406 MHz
- North America: 400.15-406 MHz, 1668-1700 MHz
- Russia: 400-406 MHz
- Australia: 400.15-406 MHz

**detection_threshold:**
- Signal must be X dB above noise floor
- Lower = more sensitive but more false positives
- 10-20 dB is typical

## Decoder Configuration

```yaml
decoders:
  rs1729_path: './decoders/rs1729'
  startup_timeout: 10
  max_idle_time: 300
```

**rs1729_path:**
- Path to compiled rs1729/RS decoder binaries
- Must contain: rs41mod, rs92mod, dfm09mod, etc.

**max_idle_time:**
- Stop decoder if no data for X seconds
- Frees resources for new sondes
- 300 seconds (5 minutes) is typical

## Web UI Configuration

```yaml
webui:
  enabled: true
  host: '0.0.0.0'      # Listen on all interfaces
  port: 5000
  debug: false
  
  map:
    default_lat: 51.5074   # Your location
    default_lon: 0.1278
    default_zoom: 8
```

**host:**
- `0.0.0.0` = accessible from network
- `127.0.0.1` = localhost only

**Map Settings:**
- Set `default_lat/lon` to your station location
- Map will auto-center on detected sondes

## Output Configuration

### UDP Output to OpenWX

```yaml
output:
  udp:
    enabled: true
    host: '127.0.0.1'    # OpenWX server IP
    port: 55672          # OpenWX UDP port
```

**Important:**
- Set `host` to your OpenWX server address
- Port 55672 is standard for Horus UDP protocol
- Set `enabled: false` to disable uploads

### Local Logging

```yaml
output:
  log:
    enabled: true
    path: './data/logs'
    format: 'json'
  update_interval: 1
```

Saves all decoded telemetry to local files for analysis.

## Uploader Configuration

Add this to your `config.yaml`:

```yaml
uploader_callsign: 'YOUR_CALLSIGN'
```

Replace with your amateur radio callsign or station identifier.

## Logging Configuration

```yaml
logging:
  level: 'INFO'        # DEBUG, INFO, WARNING, ERROR
  file: './logs/openwxsdr.log'
  max_size: 10485760   # 10 MB
  backup_count: 5      # Keep 5 old log files
```

**Log Levels:**
- `DEBUG` = Very detailed (for troubleshooting)
- `INFO` = Normal operation
- `WARNING` = Potential issues
- `ERROR` = Errors only

## Example Configurations

### High-Traffic Area (Many Sondes)

```yaml
receivers:
  max_concurrent: 8
  scan_interval: 3
  min_signal_strength: -15
```

### Low-Power Raspberry Pi Zero

```yaml
receivers:
  max_concurrent: 2
  scan_interval: 10
  min_signal_strength: -25
```

### Europe-Specific

```yaml
detection:
  freq_ranges:
    - [400150000, 406000000]  # 400.15-406 MHz
```

### North America (Dual Band)

```yaml
detection:
  freq_ranges:
    - [400150000, 406000000]  # 400-406 MHz
    - [1668000000, 1700000000]  # 1668-1700 MHz
```

## Troubleshooting

### No Signals Detected

1. Check antenna is connected and suitable for 400 MHz
2. Verify frequency range in config matches your region
3. Lower `detection_threshold` to 10 dB
4. Check `rtl_test` output for device health

### High CPU Usage

1. Reduce `max_concurrent` receivers
2. Increase `scan_interval`
3. Lower `sample_rate` to 1e6 (1 MSPS)

### Poor Decoding

1. Increase gain (if not using auto)
2. Check antenna positioning
3. Verify PPM correction
4. Check for interference sources

### Web UI Not Accessible

1. Check firewall allows port 5000
2. Verify `host: '0.0.0.0'` in config
3. Check service is running: `sudo systemctl status openwxsdr`
