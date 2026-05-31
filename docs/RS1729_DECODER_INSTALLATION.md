# RS1729 Decoder Installation for OpenWXSDR

## Overview

OpenWXSDR uses the [rs1729/RS](https://github.com/rs1729/RS) decoder suite for radiosonde telemetry decoding. **Important:** The decoders must be built from the `RS/demod/mod` directory, not from the individual type directories (RS/rs41, RS/dfm, etc.).

## Why RS/demod/mod?

The `RS/demod/mod` directory contains the modern decoders that:
- Support **--json** output for structured telemetry
- Work with FM/IQ data streams
- Use cross-correlation for header synchronization
- Provide consistent output format across all sonde types

The older decoders in RS/rs41, RS/dfm, etc. are audio-only decoders and lack proper JSON support.

## Quick Installation

**Prerequisites:**
```bash
sudo apt update
sudo apt install build-essential git
```

Run the automated installation script:

```bash
cd /home/pi/OpenWXSDR
sudo bash install_rs1729_decoders.sh
```

This script will:
1. Clone the rs1729/RS repository
2. Build all decoders from `RS/demod/mod` using `make`
3. Install them to `/home/pi/OpenWXSDR/decoders/rs1729/`
4. Set executable permissions
5. Show build log at `/tmp/decoder_build.log` if errors occur

## Manual Installation

If you prefer manual installation:

```bash
cd ~
git clone https://github.com/rs1729/RS.git
cd RS/demod/mod

# Compile decoders (with JSON support)
gcc -O2 rs41mod.c -lm -o rs41mod
gcc -O2 rs92mod.c -lm -o rs92mod
gcc -O2 dfm09mod.c -lm -o dfm09mod
gcc -O2 m10mod.c -lm -o m10mod
gcc -O2 m20mod.c -lm -o m20mod
gcc -O2 imet54mod.c -lm -o imet54mod

# Install to OpenWXSDR
mkdir -p /home/pi/OpenWXSDR/decoders/rs1729
cp *mod /home/pi/OpenWXSDR/decoders/rs1729/
chmod +x /home/pi/OpenWXSDR/decoders/rs1729/*mod
```

## Decoder Flags Used by OpenWXSDR

### RS41 (rs41mod)
```bash
rs41mod -v --ptu2 --sat --json --IQ 0.0 - 48000 16
```
- `-v`: Verbose output
- `--ptu2`: PTU sensor data (temperature, pressure, humidity)
- `--sat`: GPS satellite count
- `--json`: **JSON output** (requires RS/demod/mod build)
- `--IQ 0.0`: IQ data input with DC offset 0.0

### DFM (dfm09mod)
```bash
dfm09mod -i -vv -ID --IQ 0.0 --ecc --json --dist --ptu - 48000 16
```
- `-i`: Invert signal
- `-vv`: Very verbose
- `-ID`: **Show actual serial** (not masked)
- `--json`: **JSON output**
- `--ecc`: Error correction
- `--dist`: Distance calculation
- `--ptu`: PTU sensor data

### M10/M20/RS92 (m10mod, m20mod, rs92mod)
```bash
m10mod -v --IQ 0.0 - 48000 16
```
- `-v`: Verbose output
- `--IQ 0.0`: IQ data input

## Verification

Check that decoders are installed correctly:

```bash
ls -lh /home/pi/OpenWXSDR/decoders/rs1729/

# Should show:
# -rwxr-xr-x 1 pi pi  45K dfm09mod
# -rwxr-xr-x 1 pi pi  38K imet54mod
# -rwxr-xr-x 1 pi pi  42K m10mod
# -rwxr-xr-x 1 pi pi  40K m20mod
# -rwxr-xr-x 1 pi pi  51K rs41mod
# -rwxr-xr-x 1 pi pi  48K rs92mod
```

Test JSON output:

```bash
# RS41 should accept --json flag
/home/pi/OpenWXSDR/decoders/rs1729/rs41mod --json 2>&1 | head

# DFM should accept --json flag
/home/pi/OpenWXSDR/decoders/rs1729/dfm09mod --json 2>&1 | head
```

## Troubleshooting

### Error: "No decoders were built" or all builds fail

**Check prerequisites:**
```bash
# Install build tools
sudo apt update
sudo apt install build-essential git

# Verify gcc is installed
gcc --version
# Should show: gcc (Debian/Raspbian ...) 10.x or higher

# Verify make is installed
make --version
```

**Check build log:**
```bash
cat /tmp/decoder_build.log
# or
cat /tmp/rs1729_build.log
```

Common errors:
- `gcc: command not found` → Install build-essential
- `fatal error: math.h: No such file or directory` → Install libc6-dev: `sudo apt install libc6-dev`
- `undefined reference to 'sin'` → Math library issue, should use `-lm` flag (already in Makefile)

### Manual build test:
```bash
cd /tmp
git clone https://github.com/rs1729/RS.git
cd RS/demod/mod

# Try building manually
make clean
make

# Check what was built
ls -lh *mod

# If successful, check a specific decoder
./rs41mod --help 2>&1 | head
```

### Error: "gcc: command not found"
Install build tools:
```bash
sudo apt update
sudo apt install build-essential
```

### Error: "--json: unrecognized option"
Your decoder was built from the wrong directory (RS/rs41 instead of RS/demod/mod). Rebuild using the correct path.

### DFM serial shows "UNKNOWN" or "DFM-xxxxxxxx"
Make sure you're using the `-ID` flag to unmask the serial:
```bash
dfm09mod -i -vv -ID --IQ 0.0 --ecc --json --dist --ptu - 48000 16
```

### AttributeError: 'SondeTelemetry' object has no attribute 'battery_voltage'
This was fixed in v1.0.46. The attribute is now `battery` (not `battery_voltage`).

## References

- rs1729/RS GitHub: https://github.com/rs1729/RS
- OpenWXSDR Documentation: [docs/](../docs/)
- Decoder README: `RS/demod/mod/README.md` (in cloned repository)

## Version History

- **v1.0.46**: Added RS41 JSON support, fixed battery attribute, added `-ID` flag for DFM
- **v1.0.45**: Initial RS/demod/mod migration documentation
