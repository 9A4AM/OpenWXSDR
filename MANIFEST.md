# OpenWXSDR v1.0.45 - Distribution Archive Contents

This archive contains the complete OpenWXSDR radiosonde decoder framework for Raspberry Pi.

## Archive Information

- **Filename:** openwxsdr-1.0.45.tar.gz
- **Size:** ~39 KB (compressed)
- **Format:** tar.gz (GNU tar compatible)
- **Created:** 2026-04-30

## Installation

Download and extract:
```bash
wget http://api.openwx.de/openwxsdr-1.0.45.tar.gz
tar -xzf openwxsdr-1.0.45.tar.gz
cd OpenWXSDR
```

See `INSTALLATION.md` for complete setup instructions.

## Archive Contents

### Root Files
```
OpenWXSDR/
├── README.md                  - Project overview and features
├── INSTALLATION.md            - Complete installation guide
├── LICENSE                    - MIT License
├── config.yaml                - Main configuration file
├── requirements.txt           - Python dependencies
├── openwxsdr.py              - Main application entry point
├── install.sh                - Automated installation script
└── .gitignore                - Git ignore patterns
```

### Source Code (src/)
```
src/
├── __init__.py               - Package initialization
├── openwxsdr_app.py         - Main application coordinator
├── sdr/                      - SDR interface modules
│   ├── __init__.py
│   ├── rtlsdr_analyzer.py   - RTL-SDR spectrum analyzer
│   └── ka9q_receiver.py     - KA9Q radio interface
├── decoders/                 - Decoder modules
│   ├── __init__.py
│   ├── models.py            - Telemetry data models
│   ├── rs1729_decoder.py    - rs1729/RS decoder interface
│   └── decoder_manager.py   - Multi-decoder coordinator
├── output/                   - Output modules
│   ├── __init__.py
│   └── udp_output.py        - UDP JSON output (OpenWX)
└── webui/                    - Web interface
    ├── __init__.py
    └── web_server.py        - Flask server and REST API
```

### Web Interface (templates/)
```
templates/
└── index.html                - Interactive Leaflet map UI
```

### Scripts
```
scripts/
├── install_service.sh        - systemd service installer
└── test_components.py        - Component testing utility
```

### Documentation (docs/)
```
docs/
├── QUICKSTART.md            - 5-minute setup guide
├── CONFIGURATION.md         - Detailed configuration reference
├── TROUBLESHOOTING.md       - Common issues and solutions
├── API.md                   - REST API documentation
└── PROJECT_STRUCTURE.md     - Architecture overview
```

## What's NOT Included (Downloaded During Installation)

The following components are downloaded/built by `install.sh`:

- **rs1729/RS decoders** - Downloaded from GitHub and compiled
  - rs41mod (Vaisala RS41)
  - rs92mod (Vaisala RS92)
  - dfm09mod (Graw DFM)
  - m10mod (Meteomodem M10)
  - m20mod (Meteomodem M20)
  - imet54mod (InterMet iMet)
  - lms6mod (Lockheed Martin LMS6)
  - mrzmod (Meteo-Radiy MRZ)

- **Python packages** - Installed via pip from requirements.txt
  - numpy, scipy (scientific computing)
  - pyrtlsdr (RTL-SDR interface)
  - Flask, Flask-CORS (web server)
  - PyYAML (configuration)

- **System packages** - Installed via apt-get
  - rtl-sdr tools
  - Build tools (gcc, make, cmake)
  - Python development headers
  - USB libraries

## File Integrity

You can verify the archive integrity:

```bash
# List contents
tar -tzf openwxsdr-1.0.45.tar.gz

# Test archive
tar -tzf openwxsdr-1.0.45.tar.gz > /dev/null && echo "Archive OK"

# Extract to test directory
mkdir test && tar -xzf openwxsdr-1.0.45.tar.gz -C test
```

## Requirements

**Hardware:**
- Raspberry Pi 4 (8GB recommended)
- RTL-SDR dongle
- Antenna for 400-406 MHz

**Software:**
- Raspberry Pi OS (Debian Trixie or newer)
- ~500 MB free disk space
- Internet connection for installation

## Quick Start

```bash
# Download
wget http://download.openwx.de/openwxsdr-1.0.45.tar.gz

# Extract
tar -xzf openwxsdr-1.0.45.tar.gz
cd OpenWXSDR

# Install
chmod +x install.sh
bash install.sh

# Configure
nano config.yaml

# Run
source venv/bin/activate
python3 openwxsdr.py
```

## Support

- **Documentation:** See `docs/` directory
- **Installation Help:** See `INSTALLATION.md`
- **Configuration:** See `docs/CONFIGURATION.md`
- **Troubleshooting:** See `docs/TROUBLESHOOTING.md`

## Version History

### v1.0.45 (2026-04-30)
- Initial release
- RTL-SDR and KA9Q radio support
- 8 radiosonde types supported
- Web UI with Leaflet map
- UDP JSON output for OpenWX
- Automatic signal detection
- Virtual receiver system

## License

MIT License - See LICENSE file

## Credits

- **rs1729/RS decoders:** https://github.com/rs1729/RS
- **Inspired by:** Project Horus radiosonde_auto_rx https://github.com/projecthorus/radiosonde_auto_rx/
- **Built for:** OpenWX.de Network

---

**OpenWXSDR v1.0.45** | Happy radiosonde tracking! 🎈
