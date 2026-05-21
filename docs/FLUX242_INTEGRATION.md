# Flux242 Radiosonde Integration

OpenWXSDR now supports the flux242/radiosonde project for multi-sonde reception with a single RTL-SDR dongle!

## What is flux242/radiosonde?

The [flux242/radiosonde](https://github.com/flux242/radiosonde) project includes `receivemultisonde.sh`, a proven script that can decode **5-6 radiosondes simultaneously** using only one RTL-SDR dongle. 

### Advantages

- **Multi-sonde reception**: Track 5-6 sondes at once with single RTL-SDR
- **Automatic detection**: Scans spectrum and auto-detects sonde types
- **Efficient**: Uses iq_server channelizer (C-based, low CPU)
- **Proven**: Widely used in radiosonde community
- **Simple**: ~170 lines of bash script

### How it works

1. RTL-SDR captures 2.4 MHz baseband IQ samples
2. `iq_server` channelizer creates virtual receivers for each detected signal
3. Appropriate decoders (rs41mod, dfm09mod, etc.) run for each channel
4. Decoded JSON frames broadcast via UDP on port 5678

## Installation

### 1. Install Dependencies

```bash
# On Raspberry Pi / Debian / Ubuntu
sudo apt-get update
sudo apt-get install -y \
    rtl-sdr \
    gawk \
    bash \
    socat \
    jq \
    sox \
    build-essential \
    git
```

### 2. Clone and Compile flux242/radiosonde

```bash
cd /home/pi  # or your preferred location

# Clone repository
git clone https://github.com/flux242/radiosonde.git

# Compile decoders
cd radiosonde/decoders
make

# Compile iq_server channelizer
cd ../iq_svcl
make

# Verify
ls -la ../scripts/receivemultisonde.sh
ls -la iq_server
ls -la ../decoders/rs41mod
ls -la ../decoders/dfm09mod
```

### 3. Configure OpenWXSDR

Edit `config.yaml`:

```yaml
sdr:
  type: 'flux242'  # Change from 'rtlsdr' to 'flux242'
  
  flux242:
    center_freq: 403405000  # Tune to middle of your sonde frequency range
    sample_rate: 2400000    # 2.4 MHz recommended (covers 2.4 MHz bandwidth)
    gain: 40                # Tuner gain (0=auto, or 0-50)
    ppm_error: 0            # PPM correction if needed
    threshold: 4            # Detection threshold in dB (4-5 recommended)
    udp_port: 5678          # Port for decoded JSON frames
    power_port: 5676        # Port for power scanning data
    debug_port: 5675        # Port for debug info
    script_path: '/home/pi/radiosonde/scripts/receivemultisonde.sh'
```

**Center frequency selection:**
- If your sondes are between 402-405 MHz, use: `403405000` (403.405 MHz)
- For 400-406 MHz range: May need multiple instances or wider SDR
- The script scans ±1.2 MHz around center frequency

### 4. Test flux242 Standalone

Before integrating with OpenWXSDR, test the script standalone:

**IMPORTANT: You MUST cd into the scripts directory!**

```bash
cd /home/pi/radiosonde/scripts

# Start receiver (adjust -f for your region)
./receivemultisonde.sh -f 403405000 -s 2400000 -P 0 -g 40 -t 4

# In another terminal, listen for decoded frames
nc -luk 5678
```

**Note:** Do NOT run with absolute path from other directories - the script requires `defaults.conf` in the current working directory.

You should see JSON output like:
```json
{"type":"RS41","frame":3044,"id":"S3541192","datetime":"2021-04-10T05:16:25.000Z",
 "lat":48.88825,"lon":9.54869,"alt":9515.6267,"vel_h":21.0129,"heading":76.92116,
 "vel_v":3.66779,"sats":10,"bt":65535,"batt":2.8,"temp":-53.8,"humidity":65.6,
 "pressure":283.34,"subtype":"RS41-SGP","freq":"404500000"}
```

Press Ctrl+C to stop.

### 5. Run OpenWXSDR with Flux242

```bash
cd /home/pi/openwxsdr-1.0.45
./openwxsdr.py

# Or with systemd service
sudo systemctl restart openwxsdr
sudo journalctl -u openwxsdr -f
```

## Configuration Tips

### Frequency Selection

The script scans with 2 kHz steps around the center frequency. For German/European radiosondes:

- **402-405 MHz**: Most common, use center_freq: `403405000`
- **400-406 MHz**: Full range requires ~2.4 MHz bandwidth
- Avoid aliasing at edges - tune well inside your range

### Threshold Setting

- `threshold: 4` - Good balance (4-5 dB recommended)
- Lower value = more sensitive = earlier detection, but more false positives
- Higher value = less sensitive = fewer false alarms

### Gain Setting

- `gain: 40` - Usually works well
- `gain: 0` - Auto gain (if unsure)
- Too high gain can cause overload and false detections

### Troubleshooting

**Problem**: No sondes detected
- Check center_freq matches your region's sonde frequencies
- Lower threshold to 3
- Verify RTL-SDR works: `rtl_test`
- Check power scanning: `nc -luk 5676` (should show spectrum data)

**Problem**: Slots constantly allocated/deallocated
- False detections from noise or interference
- Increase threshold to 5
- Add problem frequencies to FREQ_BLACK_LIST in `defaults.conf`

**Problem**: CPU usage too high
- Reduce number of simultaneous sondes (max 5-6 on Celeron N5000)
- Consider faster hardware for more concurrent decodings

**Problem**: receivemultisonde.sh crashes
- Check debug output: `nc -luk 5675`
- Verify all decoders compiled: `ls -la radiosonde/decoders/`
- Check iq_server compiled: `ls -la radiosonde/iq_svcl/iq_server`

## Monitoring

### Debug Output

```bash
nc -luk 5675  # Debug messages
```

### Power Scanning Visualization

```bash
cd ~/radiosonde/scripts
nc -luk 5676 | ./plotpowerjson.sh  # Requires gnuplot
```

### Web UI

OpenWXSDR web UI shows all decoded sondes:
- Open browser: `http://raspberry-pi:5000`
- Map displays all active sondes
- Supports 5-6 sondes simultaneously

## Comparison: flux242 vs Built-in OpenWXSDR

| Feature | flux242 Mode | Built-in RTL-SDR Mode |
|---------|-------------|----------------------|
| **Sondes per RTL-SDR** | 5-6 | 1 |
| **Auto-detection** | Yes (spectrum scan) | Yes (spectrum scan) |
| **CPU Efficiency** | High (C-based) | Medium (Python + decoders) |
| **Setup Complexity** | Medium (compile needed) | Low (pure Python) |
| **Maturity** | Proven, widely used | New, under development |
| **Multi-dongle** | Possible (future) | Future feature |

## Recommended Use Cases

**Use flux242 mode when:**
- You need to track multiple sondes simultaneously
- Your region has 3+ active sondes at once
- CPU efficiency is important
- You want proven, mature codebase

**Use built-in rtlsdr mode when:**
- Only 1-2 sondes active in your area
- You want simplest setup (no compilation)
- You're testing/developing

## Advanced: Power Scanning Data

flux242 broadcasts power scanning on UDP port 5676:

```json
{"response_type":"log_power","samplerate":2400000,"tuner_freq":403405000,
 "result":"-83.45,...,-83.28"}
```

Result contains 4096 comma-separated power values across the spectrum. Could be used for:
- Custom spectrum visualization in web UI
- Signal strength monitoring
- Interference detection

## DFM Decoder Troubleshooting

If DFM radiosondes are detected but produce poor or no decoded data:

### 1. Check Decoder Binary
```bash
cd ~/radiosonde/decoders
ls -la dfm09mod
./dfm09mod --help
```

If missing or old version, rebuild:
```bash
cd ~/radiosonde
git pull
cd decoders
gcc -O2 dfm09mod.c -lm -o dfm09mod
```

### 2. Check Signal Quality
DFM signals are weaker and more sensitive to noise than RS41:
```bash
# Monitor detection logs
nc -luk 5675 | grep "Type detected: DFM"
```

If DFM detected but not decoded:
- **Signal may be too weak** - DFM requires better SNR than RS41
- **Frequency drift** - DFM is more sensitive to AFC/frequency errors
- **Antenna polarization** - DFM may use different polarization

### 3. Manual DFM Test
Test dfm09mod directly with raw IQ:
```bash
cd ~/radiosonde/scripts
# Find a detected DFM frequency (e.g., 402.700 MHz)
rtl_fm -f 402700000 -s 48000 -M fm -E deemp -g 30 - | ./dfm09mod -i -vv --IQ 0.0 --ecc --json --dist --ptu - 48000 16
```

Should show both text frames and JSON output:
```
[1234] 2026-05-04T12:34:56.000Z  lat: 52.1234 lon: 7.5678 alt: 15234.5  vH: 8.2 D: 45.6 vV: -9.1
{ "type": "DFM", "frame": 1234, "id": "DFM-12345678", "lat": 52.1234, "lon": 7.5678, "alt": 15234.5 }
```

**IMPORTANT: Correct DFM Decoder Flags**

For optimal DFM decoding, use these flags:
```bash
dfm09mod -i -vv --IQ 0.0 --ecc --json --dist --ptu - 48000 16
```

Explanation:
- `-i`: Invert signal (required for proper polarity)
- `-vv`: Verbose output (shows both text and debugging info)
- `--IQ 0.0`: Phase offset for IQ demodulation
- `--ecc`: Enable error correction coding
- `--json`: Output JSON format (machine-readable)
- `--dist`: Calculate distance from launch site
- `--ptu`: Decode PTU (Pressure, Temperature, Humidity) data

**Note for flux242/receivemultisonde.sh users:** The default receivemultisonde.sh script may use minimal flags like `-v --IQ 0.0`. For better DFM decoding, you may need to edit the script to add the full flag set above. Look for the line that starts `dfm09mod` and update it accordingly.

#### Patching receivemultisonde.sh for Better DFM Decoding

To improve DFM decoding in flux242 mode, edit the receivemultisonde.sh script:

```bash
cd ~/radiosonde/scripts
nano receivemultisonde.sh
```

Find the line that starts the DFM decoder (search for `dfm09mod`), it might look like:
```bash
$SONDEMOD/dfm09mod -v --IQ 0.0 - 48000 16 < ...
```

Change it to include the full flag set:
```bash
$SONDEMOD/dfm09mod -i -vv --IQ 0.0 --ecc --json --dist --ptu - 48000 16 < ...
```

Save and restart OpenWXSDR. This will provide:
- Better error correction for weak signals
- JSON output for more reliable parsing
- PTU (temperature/humidity/pressure) data
- Distance calculations from launch site

**After patching:** OpenWXSDR will automatically parse the JSON output and display DFM sondes with full telemetry including serial numbers like "DFM-23030665" instead of showing "UNKNOWN" until the ID is decoded from text frames.

### 4. DFM vs RS41 Identification
receivemultisonde.sh auto-detects sonde type by:
- **Bandwidth**: DFM ~6-9 kHz, RS41 ~4-6 kHz
- **Modulation**: Both use GFSK but different rates
- **Frame structure**: DFM has different sync pattern

If RS41 decoder runs on DFM signal (or vice versa):
- Check `detectsondes.sh` output - should show correct type
- Update radiosonde tools: `cd ~/radiosonde && git pull`
- Verify defaults.conf has correct decoder paths

### 5. Known DFM Limitations
- **Weaker signals** than RS41 - needs higher gain/better antenna
- **More CPU intensive** - may struggle on Pi 3 with 4+ sondes
- **Limited PTU data** - some DFM models don't output temp/humidity
- **Older DFM06** - may need different decoder (not supported yet)

### 6. Check OpenWXSDR Logs
```bash
sudo journalctl -u openwxsdr -n 100 | grep DFM
```

Should show frames like:
```
Flux242: DFM D4123456 F1234 52.12345,7.56789 Alt:15234m Freq:402.700MHz
```

If showing "UNKNOWN" or wrong frequency, check that you're running v1.0.45+ with the serial/frequency field fixes.

## Credits

- **flux242/radiosonde**: https://github.com/flux242/radiosonde
- **zilog80 decoders**: Original rs41mod, dfm09mod, etc.
- **iq_svcl**: Efficient IQ channelizer

## References

- flux242 Blog: http://flux242.blogspot.com/
- Project Issues: https://github.com/flux242/radiosonde/issues
- OpenWXSDR: https://github.com/DL2MF/OpenWXSDR
