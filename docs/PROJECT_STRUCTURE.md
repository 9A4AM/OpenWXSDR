# OpenWXSDR Project Structure

## Root Files

```
OpenWXSDR/
├── config.yaml                 # Main configuration file
├── requirements.txt            # Python dependencies
├── openwxsdr.py               # Main entry point
├── install.sh                 # Installation script
├── README.md                  # Project overview
├── LICENSE                    # MIT License
└── .gitignore                 # Git ignore rules
```

## Source Code (`src/`)

### Main Application
```
src/
├── __init__.py                # Package initialization
└── openwxsdr_app.py          # Main application coordinator
```

### SDR Module (`src/sdr/`)
```
src/sdr/
├── __init__.py
├── rtlsdr_analyzer.py        # RTL-SDR spectrum analyzer and signal detector
└── ka9q_receiver.py          # KA9Q radio interface (optional)
```

**Key Features:**
- Real-time spectrum analysis
- Automatic signal detection
- Peak finding and signal characterization
- Background scanning thread
- Support for multiple SDR types

### Decoders Module (`src/decoders/`)
```
src/decoders/
├── __init__.py
├── models.py                 # Data models for telemetry
├── rs1729_decoder.py        # Interface to rs1729/RS decoders
└── decoder_manager.py       # Manages multiple decoder instances
```

**Key Features:**
- Automatic sonde type identification
- Multi-process decoder management
- Real-time telemetry parsing
- Support for 8 radiosonde types:
  - RS41 (Vaisala)
  - RS92 (Vaisala)
  - DFM (Graw)
  - M10/M20 (Meteomodem)
  - iMet (InterMet)
  - LMS6 (Lockheed Martin)
  - MRZ (Meteo-Radiy)

### Output Module (`src/output/`)
```
src/output/
├── __init__.py
└── udp_output.py            # UDP JSON output for OpenWX
```

**Key Features:**
- Horus UDP protocol compatible
- OpenWX.de server integration
- JSON telemetry formatting

### Web UI Module (`src/webui/`)
```
src/webui/
├── __init__.py
└── web_server.py            # Flask web server and REST API
```

**Key Features:**
- Real-time web dashboard
- REST API endpoints
- Telemetry history management
- CORS support

## Templates (`templates/`)

```
templates/
└── index.html               # Main web UI with Leaflet map
```

**Features:**
- Interactive Leaflet map
- Real-time telemetry display
- Flight path visualization
- Responsive design
- Auto-updating dashboard

## Scripts (`scripts/`)

```
scripts/
├── install_service.sh       # Install systemd service
└── test_components.py       # Component testing utility
```

## Documentation (`docs/`)

```
docs/
├── QUICKSTART.md           # 5-minute setup guide
├── CONFIGURATION.md        # Detailed configuration reference
├── TROUBLESHOOTING.md      # Common issues and solutions
└── API.md                  # REST API reference
```

## Generated Directories

These are created during installation/runtime:

```
decoders/rs1729/           # rs1729/RS decoder binaries (git clone)
logs/                      # Application logs
data/logs/                 # Telemetry logs
venv/                      # Python virtual environment
```

## Architecture Overview

```
┌─────────────────────────────────────────────────────────┐
│                    openwxsdr.py (Main)                  │
│                  Load config, setup logging             │
└────────────────────────┬────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────┐
│              src/openwxsdr_app.py (Coordinator)         │
│         Initializes and orchestrates all components     │
└────┬─────────┬──────────┬──────────┬────────────────┬───┘
     │         │          │          │                │
     ▼         ▼          ▼          ▼                ▼
┌─────────┐ ┌──────┐ ┌────────┐ ┌────────┐ ┌──────────────┐
│ SDR     │ │Decode│ │ Output │ │ WebUI  │ │ Configuration│
│ Module  │ │ Mgr  │ │ Module │ │ Server │ │    (YAML)    │
└────┬────┘ └──┬───┘ └───┬────┘ └───┬────┘ └──────────────┘
     │         │         │          │
     │         │         │          │
┌────▼─────────▼─────────▼──────────▼─────┐
│         Telemetry Data Flow              │
│  SDR → Decoder → [WebUI, UDP, Logs]      │
└──────────────────────────────────────────┘
```

## Data Flow

1. **Signal Detection:**
   - RTL-SDR captures RF spectrum
   - Spectrum analyzer detects peaks
   - Signals filtered by frequency range and strength

2. **Decoder Management:**
   - Decoder manager receives detected signals
   - Identifies sonde type based on bandwidth
   - Launches appropriate rs1729 decoder binary
   - Manages up to N concurrent decoders

3. **Telemetry Processing:**
   - Decoders parse output (JSON or text)
   - Converts to standardized SondeTelemetry objects
   - Forwards to output modules

4. **Data Distribution:**
   - **Web UI:** Stores in memory, serves via REST API
   - **UDP Output:** Sends to OpenWX server
   - **Logs:** Writes to disk (optional)

## Key Design Decisions

### Why rs1729/RS?
- Lightweight, efficient decoders
- Well-maintained and accurate
- Support for many sonde types
- Less overhead than radiosonde_auto_rx

### Why Virtual Receivers?
- Decode multiple sondes simultaneously
- No physical multicoupler needed
- Efficient use of SDR bandwidth

### Why Flask?
- Lightweight web framework
- Easy to extend and customize
- Good REST API support
- Millions of simultaneous connections not needed

### Why UDP for Output?
- Standard protocol for Horus/OpenWX
- Fire-and-forget (no connection overhead)
- Minimal latency

## Module Dependencies

```
openwxsdr.py
  └─> openwxsdr_app.py
       ├─> sdr/rtlsdr_analyzer.py
       │    └─> numpy, scipy, rtlsdr
       ├─> sdr/ka9q_receiver.py (optional)
       ├─> decoders/decoder_manager.py
       │    ├─> decoders/rs1729_decoder.py
       │    │    └─> subprocess, rs1729 binaries
       │    └─> decoders/models.py
       ├─> output/udp_output.py
       │    └─> socket, json
       └─> webui/web_server.py
            └─> Flask, Flask-CORS

External Dependencies:
  - rs1729/RS decoder binaries
  - pyrtlsdr (RTL-SDR Python wrapper)
  - Leaflet.js (web map library)
```

## Configuration Flow

```
config.yaml
  ├─> SDR settings → SpectrumAnalyzer / KA9QReceiver
  ├─> Detection settings → Signal detection thresholds
  ├─> Decoder settings → DecoderManager
  ├─> Output settings → UDPOutput
  ├─> WebUI settings → WebUI server
  └─> Logging settings → Python logging
```

## File Sizes (Approximate)

```
Core Python Code:      ~15 KB total
  - openwxsdr_app.py:   ~5 KB
  - rtlsdr_analyzer.py: ~8 KB
  - decoder_manager.py: ~6 KB
  - rs1729_decoder.py:  ~9 KB
  - udp_output.py:      ~3 KB
  - web_server.py:      ~4 KB

Templates/UI:         ~10 KB
  - index.html:        ~10 KB

Documentation:        ~50 KB
Scripts:              ~5 KB
Config:               ~2 KB

Total Project Size:   ~85 KB (without dependencies)
```

## Development Guidelines

### Adding a New Sonde Type

1. Add decoder binary to `DECODER_BINARIES` in `rs1729_decoder.py`
2. Update `_identify_sonde_type()` in `decoder_manager.py`
3. Update color mapping in `templates/index.html`
4. Build/install decoder binary in `decoders/rs1729/`

### Adding a New Output Type

1. Create module in `src/output/`
2. Implement with `send_telemetry(telemetry)` method
3. Initialize in `openwxsdr_app.py`
4. Call from `_handle_telemetry()` method

### Adding Configuration Options

1. Add to `config.yaml`
2. Access via `self.config` dictionary in relevant module
3. Document in `docs/CONFIGURATION.md`

## Testing

```bash
# Component tests
python3 scripts/test_components.py

# SDR test
python3 openwxsdr.py --test-sdr

# Manual module test
python3 -c "from src.sdr.rtlsdr_analyzer import SpectrumAnalyzer; print('OK')"
```

## Performance Characteristics

**Memory Usage:**
- Base: ~50-100 MB
- Per active sonde: +10-20 MB
- Flask server: +20-40 MB

**CPU Usage (Pi 4):**
- Spectrum scanning: ~10%
- Per decoder: ~5-10%
- Flask server: ~5%

**Disk Usage:**
- Application: <1 MB
- rs1729 decoders: ~5 MB
- Logs: Varies (configurable rotation)
- Telemetry data: Stored in memory only

**Network Usage:**
- UDP output: <1 KB per frame (~1-10 frames/sec per sonde)
- Web UI: Minimal (REST API polling)

---

This structure provides a modular, maintainable, and efficient radiosonde decoding framework optimized for Raspberry Pi.
