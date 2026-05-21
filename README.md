# OpenWXSDR - Streamlined Radiosonde Decoder Framework

A lightweight, efficient radiosonde decoder framework for Raspberry Pi, designed to work with RTL-SDR and KA9Q radio receivers. Uses the excellent rs1729/RS decoder suite.

## Features

- 🎯 **Automatic Signal Detection**: Scans spectrum and automatically detects radiosonde signals
- 🔧 **Multiple Sonde Types**: Supports RS41, RS92, DFM, M10, M20, iMet, LMS6, MRZ
- 📡 **Dual SDR Support**: Works with RTL-SDR, Airspy R2, Airspy Mini USB dongles
- 🎛️ **Virtual Receivers**: Configurable parallel decoding of multiple sondes with KA9Q radio
- 🗺️ **Web Interface**: Clean Leaflet-based map showing real-time flight paths
- 📤 **OpenWX Integration**: MQTT & UDP JSON output for seamless data upload
- 📤 **Sondehub Integration**: seamless data upload to sondehub.org
- ⚡ **Lightweight**: Minimal overhead compared to several other decoder solutions
- 🔧 **One-step installer**: Easy installtion of all required packages 

## Hardware Requirements

- Raspberry Pi 4 (8GB recommended)
- RTL-SDR dongle or KA9Q-compatible SDR
- Antenna tuned for 400-406 MHz

## Installation

### 1. Install System Dependencies

```bash
sudo apt-get update
sudo apt-get install -y git build-essential cmake pkg-config \
    libusb-1.0-0-dev python3-pip python3-venv rtl-sdr
```

### 2. Clone and Set Up rs1729/RS Decoders

```bash
mkdir -p decoders
cd decoders
git clone https://github.com/rs1729/RS.git rs1729
cd rs1729

# Build the decoders
cd demod && make
cd ../rs41 && make
cd ../rs92 && make
cd ../dfm && make
cd ../m10 && make
cd ../imet && make
cd ../lms6 && make
cd ..
```

### 3. Install OpenWXSDR

```bash
cd ~/!Develop_OpenWXSDR
python3 -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 4. Configure

Edit `config.yaml` to match your setup:
- Set SDR type and parameters
- Configure frequency ranges for your region
- Set max concurrent receivers
- Configure OpenWX UDP output destination

### 5. Run

```bash
python3 openwxsdr.py
```

Access the web UI at `http://raspberry-pi-ip:5000`

## Configuration

Key configuration options in `config.yaml`:

- **sdr.type**: Choose 'rtlsdr' or 'ka9q'
- **receivers.max_concurrent**: Number of parallel decoders (2-8 recommended)
- **detection.freq_ranges**: Frequency ranges to scan
- **output.udp**: Configure OpenWX server destination

## Architecture

```
┌─────────────────┐
│   RTL-SDR/      │
│   KA9Q Radio    │
└────────┬────────┘
         │
    ┌────▼──────────────┐
    │ Spectrum Analyzer │
    │ Signal Detector   │
    └────┬──────────────┘
         │
    ┌────▼─────────────────┐
    │ Decoder Manager      │
    │ (rs1729/RS decoders) │
    └────┬─────────────────┘
         │
    ┌────▼────────┬──────────┬─────────┐
    │             │          │         │
┌───▼────┐   ┌────▼─────┐  ┌─▼──────┐  │
│ Web UI │   │ UDP JSON │  │  Log   │  │
│ Flask  │   │ OpenWX   │  │  File  │  │
└────────┘   └──────────┘  └────────┘  │
                                       │ 
                                   [sondehub]
```

## UDP JSON Format

Data sent to OpenWX server:

```json
{
  "software_name": "OpenWXSDR",
  "software_version": "1.0.45",
  "uploader_callsign": "YOUR_CALL",
  "time_received": "2026-04-30T12:34:56.789Z",
  "type": "RS41",
  "frame": 12345,
  "id": "T1234567",
  "datetime": "2026-04-30T12:34:56.000Z",
  "lat": 51.5074,
  "lon": -0.1278,
  "alt": 15420.5,
  "vel_h": 12.3,
  "vel_v": 5.2,
  "heading": 285.5,
  "temp": -45.2,
  "humidity": 25.5,
  "pressure": 150.25,
  "frequency": 402.700,
  "sats": 8,  
  "snr": 25.5
}
```

## Supported Radiosonde Types

| Type | Decoder   | Notes                   |
|------|-----------|-------------------------|
| RS41 | rs41mod   | Vaisala RS41-SG/SGP/SGM |
| RS92 | rs92mod   | Vaisala RS92-SGP/NGP    |
| DFM  | dfm09mod  | Graw DFM06/09/17        |
| M10  | m10mod    | Meteomodem M10          |
| M20  | m20mod    | Meteomodem M20          |
| iMet | imet54mod | InterMet iMet-54        |
| LMS6 | lms6mod   | Lockheed Martin LMS6    |
| MRZ  | mrzmod    | Meteo-Radiy MRZ         |

## License

MIT License - See LICENSE file

## Credits

- rs1729/RS decoders: https://github.com/rs1729/RS
- Inspired by Project Horus radiosonde_auto_rx: https://github.com/projecthorus/radiosonde_auto_rx/
- Built for the OpenWX.de network community

## Support

For issues and questions, please open an issue on GitHub.
