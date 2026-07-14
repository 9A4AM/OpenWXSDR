# OpenWX <img src="https://cdn.jsdelivr.net/npm/bootstrap-icons/icons/radar.svg" width="24"> SDR - Streamlined Radiosonde Decoder

A lightweight, efficient radiosonde decoder for Raspberry Pi, designed to work with RTL-SDR, Airspy and KA9Q radio receivers. 
Using the excellent rs1729/RS decoders embedded into this framework.

## Current Version:** 1.0.52 (July 10, 2026) available:

⚠ <u>Important notice:</u> With update to version 1.0.52 upload of radiosonde telemetry to sondehub.org has been further improved.

✨ What's New in v1.0.52

#### 🐛 Crash & Race-Condition Fixes

#### ⚙️ Self-Healing & Device Status

#### 🎯 Detection Accuracy

#### 📡 SondeHub Upload Correctness

Following feedback from a SondeHub maintainer, both upload paths
(`sondehub_queue.py` and `sondehub_output.py`) were brought in line with
`radiosonde_auto_rx`'s own field policy:

- **`subtype` is no longer blindly passed through for every sonde type.** M10/M20
  (and any type auto_rx doesn't upload a subtype for) no longer send a meaningless
  `subtype` field that could confuse trackers. RS41/RS92/DFM/LMS6/MRZ still report
  their subtype as before.
- **Sentinel "no data" values are now filtered out** instead of being uploaded as
  real readings: temperature ≤ ‑273 °C, humidity/pressure < 0, velocity/heading
  ≤ ‑9999, and negative battery voltage are all now dropped rather than sent as-is.
- `dfmcode` is now only attached to DFM uploads (previously a global check).

#### 🖥️ Web UI

#### 🔧 Maintenance & Code Quality

#### 🔒 Repository Updates & Changes

## 


## ⚠ <u>Important notice:</u> With update to version 1.0.50 several further adressed issues of radiosonde telemetry to sondehub.org has been fixed.
We are looking forward that the ban / blocking of OpenWXSDR uploads will be suspended after review of the submitted telemetry data


## ✨ What's New in v1.0.50

#### 🔧 PTU Data Improvements

- **Recency-Based PTU Merging**: Improved text fallback matching handles frame number misalignment
- **--softin Detection**: Auto-detects decoder capabilities at startup with clear warnings
- **Conditional Environment**: Only populates PTU when actual measurements exist
- **Smarter Cache Management**: Timestamp-based cleanup maintains last 100 entries

#### 🎯 Interactive Sonde Analysis

- **Context Menu**: Right-click any active sonde for quick access to statistics and predictions
- **Advanced Statistics Modal**: 7 interactive Chart.js graphs showing:
  - Altitude profile
  - Vertical velocity
  - Horizontal velocity
  - RSSI (signal strength)
  - SNR (signal-to-noise ratio)
  - GPS satellites count
  - Battery voltage
- **Historical Data Viewer**: Load and analyze logfiles from past flights with dropdown selector
- **24-Hour Time Format**: All charts use UTC time in HH:mm format for professional meteorological analysis
- **Gap Visualization**: Charts correctly show data gaps where telemetry was unavailable

#### 🗺️ Enhanced Map Features

- **Launch & Landing Markers**: Visual PNG icons mark takeoff and touchdown locations
- **Flight Path Overlay**: Display historical tracks from logfiles on the map
- **Inactive Sondes Management**: Track and manage previously decoded sondes with easy removal
- **Position Details**: Lat/Lon coordinates displayed in landing marker popups

#### 🔮 Flight Path Prediction

- **Tawhiri Integration**: Real-time flight path prediction using Sondehub's Tawhiri API
- **Intelligent Burst Altitude**: Automatic burst height estimation based on sonde type:
  - RS41: 30,000m (Bergen: 33,500m, Meppen: 25,000m)
  - DFM: 17,500m
  - Others: 25,000m
- **Drag Compensation**: Physics-based descent rate calculation accounting for air density
- **Visual Prediction**: Purple dashed line showing predicted trajectory
- **Landing Details**: Interactive popup with landing time, coordinates, and flight parameters
- **Burst Marker**: Shows estimated balloon burst location for ascending sondes

#### 📊 Data Improvements

- **Battery Tracking**: Complete battery voltage history now recorded and displayed
- **Satellite Data**: GPS satellite count tracking throughout flight
- **Enhanced Logfile Format**: All telemetry fields properly logged with timestamps
- **Live Data API**: Real-time statistics for active sondes via REST API

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

- Raspberry Pi 4 / 5 / 400 / 500 (8GB recommended) or Intel x86_64 client
- Debian 13 "Trixie" or Raspberry Pi OS (64 Bit)
- RTL-SDR, Airspy R2, Airspy Mini dongle or KA9Q-compatible SDR
- Antenna tuned for 400-406 MHz
- Selective 400 MHz LNA
- SAW filter recommended

## Architecture

```
┌─────────────────┐
│  RTL-SDR/Airspy │
│   KA9Q Radio    │
└────────┬────────┘
         │
    ┌────▼──────────────┐
    │ Spectrum Analyzer │
    │ Signal Detector   │
    └────┬──────────────┘
         │
    ┌────▼─────────────────┐
    │   Decoder Manager    │
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

## Installation

### 1a. One-Step-Installer

OpenWXSDR offers an easy-to-use One-Step-Installer to install the necessary libraries, packages and OpenWXSDR package.

See the wiki for detailed installation instructions: https://github.com/DL2MF/OpenWXSDR/wiki/Installation

If you prefer manual installation of the package, continue below.

### 1b. Install System Dependencies

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
- Set receiver specific settings (span, gain, fft, treshold)
- Configure OpenWX UDP and external output destinations

## Configuration

Key configuration options in `config.yaml`:

- **sdr.type**: Choose 'rtlsdr', 'airspy', 'flux242' or 'ka9q'
- **receivers.max_concurrent**: Number of parallel decoders (1-8 recommended)
- **detection.freq_ranges**: Frequency ranges to scan
- **output.udp**: Configure OpenWX server destination


## UDP JSON Format

Data sent to OpenWX server:

```json
{
  "software_name": "OpenWXSDR",
  "software_version": "1.0.45",
  "uploader_callsign": "<your-call>",
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


### 5. Run

```bash
python3 openwxsdr.py
```

Access the web UI at `http://raspberry-pi-ip:5000` 

Many configuration settings are also available in the WebUI and can be changed during operation:
- SNR treshold
- Scan interval
- Callsign and SSID
- SDR-Type and device settings
 - Center Frequency
 - Sample Rate
 - Gain
 - PPM correction
- MQTT and upload
 - Server IP / hostname
 - Port
 - User credentials


## Local WebUI on your device 

The local web UI is available at `http://yourdevice-ip:5000`

<img width="1405" height="903" alt="Screenshot 2026-05-19 171001" src="https://github.com/user-attachments/assets/272c99c1-7b79-4a65-9f65-273289bae137" />


For each configured receiver the frequency spectrum is available:

<img width="1361" height="824" alt="Screenshot 2026-05-18 132137" src="https://github.com/user-attachments/assets/157818ec-ce90-4e98-ba48-08f29d6dd2c5" />


## Telemetry data upload to external OpenWX.de

If enabled in config.yaml your device will upload radiosonde telemetry and sensor ptu to OpenWX.de `http://map.openwx.de`:

Upload to OpenWX.de is preferable via MQTT configurable. Generate your API-key in your OpenWX-Account and configure your credentials in config.yaml.

```
  # MQTT upload to OpenWX.de broker
  mqtt:
    enabled: true
    server: '<server-ip>'                   # MQTT broker hostname or IP
    port: 1883                              # MQTT broker port (1883 = plain, 8883 = TLS)
    username: '<username>'                  # MQTT username (leave empty if not required)
    password: '<password>'                  # MQTT password (leave empty if not required)
    topic_prefix: 'OPENWXSDR/<your-call>/'  # MQTT topic prefix (e.g. OPENWXSDR/<callsign>/<serial>)
    client_id: '<your-call>-15'             # MQTT client ID
    keepalive: 60                           # MQTT keepalive in seconds
    connect_timeout: 10                     # Wait this many seconds for initial CONNACK
    tls_enabled: true                       # Set false for username/password brokers on plain TCP (usually port 1883)
    tls_insecure: true                      # Accept brokers by IP/self-signed cert without CA validation
    tls_ca_certs: ''                        # Optional CA bundle path for strict TLS validation
    transport: 'tcp'                        # MQTT transport
```


<img width="1529" height="879" alt="grafik" src="https://github.com/user-attachments/assets/44f5b697-20b4-4619-8ef0-88396e06eaf9" />



## Telemetry data upload to external sondehub.org

If enabled in config.yaml your device will upload radiosonde telemetry and sensor ptu to sondehub.org:

```
# ============================================================
# SondeHub Upload
# ============================================================
sondehub:
  enabled: false                                           # See notice at the beginning, please don't activate in V1.0.45
  queue_mode: true                                         # false = direct upload mode, true = queued batch upload mode
  queue_batch_max: 50                                      # Max telemetry objects uploaded per queued request (10-fast 50-robust)
  queue_max_size: 1000                                     # Max queued  objects before oldest/new drops may occur (200-fast 1000-robust)
  upload_url: 'https://api.v2.sondehub.org/sondes/telemetry'
  listeners_url: 'https://api.v2.sondehub.org/listeners'   # Listener metadata endpoint
  station_id: '<your-call>->ssid>'                         # Station ID (use callsign/SSID style)
  uploader_callsign: '<your-call>'                         # Receiver callsign shown in SondeHub
  uploader_antenna: '1/4 wave UHF vertical'                # Receiver antenna
  uploader_radio: 'Airspy Mini + rs1729'                   # Optional receiver hardware/radio description
  contact_email: '<mail>@domain.com'                       # Optional contact email for listener metadata
  uploader_lat: 52.00                                      # Must match station.lat above
  uploader_lon: 10.00                                      # Must match station.lon above
  uploader_alt: 100                                        # Must match station.alt above
  upload_rate_s: 10                                        # Upload interval in seconds (1-5 for queue mode -10 recommended for single tracking)
  listener_upload_interval_s: 21600                        # Listener metadata upload interval in seconds (6h / 4 times/day recommend)
```


<img width="1376" height="943" alt="Screenshot 2026-05-19 152207" src="https://github.com/user-attachments/assets/1be3ef61-1a51-4f64-9e72-7c77dae0eca6" />



Grafana dashboard from Sondehub will show your receiver in the statistics including RSSI/SNR:

<img width="1630" height="1043" alt="Screenshot 2026-05-19 192420" src="https://github.com/user-attachments/assets/0cc6ad23-002a-4a99-8fbe-3bdd864ab81e" />


## License

GNU GPLv3 License - See [(https://github.com/DL2MF/OpenWXSDR/blob/main/LICENSE)](LICENSE) file

## Credits

- rs1729/RS decoders: https://github.com/rs1729/RS
- Inspired by Project Horus radiosonde_auto_rx: https://github.com/projecthorus/radiosonde_auto_rx/
- Built for the OpenWX.de network community

## Support

[![paypal](https://www.paypalobjects.com/en_US/i/btn/btn_donateCC_LG.gif)](https://www.paypal.com/donate?token=8zPp8MT2DKdshzvmRxDi6yJhCdXGJSb_wIulhbD73TTYGuveGIrCGbGb0jhV9m4Tpj3D2ijR2JXltlGC)

For issues and questions, please open an issue on GitHub.
