# OpenWX <img src="https://cdn.jsdelivr.net/npm/bootstrap-icons/icons/radar.svg" width="24"> SDR - Streamlined Radiosonde Decoder

# System Architecture & Developer Reference

> **OpenWXSDR** is an open-source, Raspberry Pi–optimised radiosonde telemetry collection system developed by M.F. Guenther (DL2MF) under the GNU General Public License v2.0. It decodes live atmospheric balloon transmissions from multiple simultaneous SDR receivers and forwards telemetry to local dashboards, MQTT brokers, the SondeHub global tracking network, and the OpenWX.de gateway.

---

## Table of Contents

1. [System Overview](#1-system-overview)
2. [High-Level Architecture](#2-high-level-architecture)
3. [Module Reference](#3-module-reference)
   - 3.1 [Application Entry Point — `openwxsdr_app.py`](#31-application-entry-point--openwxsdrapppy)
   - 3.2 [Data Models — `models.py`](#32-data-models--modelspy)
   - 3.3 [Decoder Layer — `rs1729_decoder.py`](#33-decoder-layer--rs1729decoderpy)
   - 3.4 [Decoder Orchestration — `decoder_manager.py`](#34-decoder-orchestration--decoder_managerpy)
   - 3.5 [Web Interface — `web_server.py`](#35-web-interface--web_serverpy)
   - 3.6 [MQTT Output — `mqtt_output.py`](#36-mqtt-output--mqtt_outputpy)
   - 3.7 [HTTP Output — `http_output.py`](#37-http-output--http_outputpy)
   - 3.8 [UDP Output — `udp_output.py`](#38-udp-output--udp_outputpy)
   - 3.9 [SondeHub Direct Upload — `sondehub_output.py`](#39-sondehub-direct-upload--sondehub_outputpy)
   - 3.10 [SondeHub Queue Upload — `sondehub_queue.py`](#310-sondehub-queue-upload--sondehub_queuepy)
4. [Data Flow](#4-data-flow)
5. [SDR Backend Modes](#5-sdr-backend-modes)
6. [Sonde Type Detection](#6-sonde-type-detection)
7. [Output Plugin Matrix](#7-output-plugin-matrix)
8. [Deployment Notes](#8-deployment-notes)

---

## 1. System Overview

OpenWXSDR turns one or more Software Defined Radio (SDR) dongles into a fully automated radiosonde ground station. It continuously scans the 400–406 MHz meteorological band, identifies active balloon transmissions, spawns per-frequency decoder processes, and streams the decoded telemetry to multiple downstream consumers in real time.

```
  ┌──────────────────────────────────────────────────────────────┐
  │                        OpenWXSDR                             │
  │                                                              │
  │ [SDR Hardware]──►[SDR Backend]──►[Decoder Layer]──►[Outputs] │
  │  RTL-SDR / Airspy   rtlsdr / ka9q    rs1729 decoders         │
  │  (400–406 MHz)      flux242 / airspy  RS41 / DFM / M10 …     │
  └──────────────────────────────────────────────────────────────┘
```

**Core capabilities:**

- Simultaneous decoding of multiple radiosonde types (RS41, RS92, DFM09/17, M10, M20, iMet-54, LMS6, MRZ)
- Automatic sonde type identification via DFT correlation or bandwidth heuristics
- Real-time Leaflet map web interface with full flight track history
- Fan-out telemetry forwarding: MQTT · HTTP · UDP · SondeHub v2 (direct & queued)
- Raspberry Pi–native deployment with systemd service management
- Multi-device RTL-SDR support with per-device channel assignment

---

## 2. High-Level Architecture

```
                            ┌─────────────────────┐
                            │    config.yaml      │
                            └────────┬────────────┘
                                     │
                            ┌────────▼────────────┐
                            │  openwxsdr_app.py   │
                            │  (OpenWXSDR class)  │
                            └──┬───────────────┬──┘
                               │               │
              ┌────────────────▼──┐      ┌─────▼────────────────┐
              │   SDR Backends    │      │   Output Plugins     │
              │                   │      │                      │
              │ • RTLSDRDevice-   │      │ • MQTTOutput         │
              │   Manager         │      │ • HttpOutput         │
              │ • AirspyReceiver  │      │ • UDPOutput          │
              │ • KA9QReceiver    │      │ • SondeHubOutput     │
              │ • Flux242Receiver │      │ • SondeHubQueueOutput│
              └────────┬──────────┘      └──────────────────────┘
                       │                           ▲
              ┌────────▼──────────┐                │
              │  DecoderManager   │                │
              │  (decoder_manager)│                │
              └────────┬──────────┘                │
                       │                           │
              ┌────────▼──────────┐                │
              │  RS1729Decoder    ├───────────────►│
              │  (rs1729_decoder) │  SondeTelemetry│
              └────────┬──────────┘                │
                       │                           │
              ┌────────▼──────────┐                │
              │  SondeTelemetry   │                │
              │  (models.py)      ├───────────────►│
              └───────────────────┘           ┌────┴───────┐
                                              │    WebUI   │
                                              │(web_server)│
                                              └────────────┘
```

---

## 3. Module Reference

### 3.1 Application Entry Point — `openwxsdr_app.py`

The `OpenWXSDR` class is the top-level application coordinator. It reads `config.yaml`, instantiates every subsystem, wires them together via callbacks, and manages the full lifecycle through `initialize() → start() → _main_loop() → stop()`.

**Lifecycle stages:**

| Stage | Method | Responsibility |
|-------|--------|---------------|
| Boot | `initialize()` | Instantiates all subsystems based on `sdr.type` config key |
| Run | `start()` | Starts all components, enters main loop |
| Idle | `_main_loop()` | RTL-SDR / KA9Q / Airspy idle supervisor |
| Flux | `_flux242_main_loop()` | Monitors autonomous flux242 process health |
| Shutdown | `stop()` | Tears down all components in reverse order |

SIGINT and SIGTERM are trapped so `stop()` is always called cleanly, flushing queues and closing SDR handles before exit.

**SDR type dispatch table:**

| `sdr.type` value | Backend class | Notes |
|-----------------|---------------|-------|
| `rtlsdr` | `RTLSDRDeviceManager` | Multi-dongle, per-device channel workers |
| `airspy` | `AirspyReceiver` | Airspy R2 / Mini wideband channelizer |
| `ka9q` | `KA9QReceiver` | KA9Q radio network receiver |
| `flux242` | `Flux242Receiver` | Autonomous `receivemultisonde.sh` wrapper |

---

### 3.2 Data Models — `models.py`

Defines the canonical Python dataclasses that flow through the entire pipeline. All decoder parsers emit `SondeTelemetry` objects; all output plugins consume them.

```
SondeTelemetry
 ├── sonde_type: str          # "RS41", "DFM", "M10", …
 ├── serial: str              # Manufacturer serial number
 ├── frame_number: int        # Decoder frame counter
 ├── subtype: str | None      # e.g. "RS41-SGP", "DFM17"
 ├── position: SondePosition  # lat, lon, alt MSL, datetime
 ├── velocity: SondeVelocity  # horizontal m/s, vertical m/s, heading °
 ├── environment: SondeEnvironment  # temp °C, humidity %, pressure hPa
 ├── frequency: float         # Reception frequency in Hz
 ├── snr: float               # Signal-to-noise ratio dB
 ├── rssi: float              # Received signal strength dBm
 ├── satellites: int | None   # GPS satellites used
 └── to_dict() → dict         # JSON-serialisation helper
```

`SondePosition`, `SondeVelocity`, and `SondeEnvironment` are standalone dataclasses that aggregate into `SondeTelemetry`. All fields use SI units internally; `to_dict()` converts frequency to MHz for serialisation.

---

### 3.3 Decoder Layer — `rs1729_decoder.py`

`RS1729Decoder` wraps a single rs1729 binary child process. It:

1. Selects the correct binary (`rs41mod`, `dfm09mod`, `m10mod`, …) for the identified sonde type
2. Builds a type-specific command line with appropriate flags (`--ptu2`, `--sat`, `--json`, `--ecc`, etc.)
3. Spawns the process with `stdin` piped from `rtl_fm` / Airspy channelizer raw IQ (48 kHz / 16-bit signed)
4. Wraps with `stdbuf -oL` when available to defeat libc block-buffering on pipe stdout
5. Monitors stdout and stderr in background daemon threads
6. Parses both **JSON** output (DFM09 `--json` mode) and **plain-text** output (RS41, M10, RS92)
7. Merges side-channel PTU and satellite-count lines (arriving between frame lines) into the subsequent frame dict
8. Invokes the registered `frame_callback(dict)` for every complete frame

**Decoder binary map:**

| Sonde type | Binary | Key flags |
|-----------|--------|-----------|
| RS41 | `rs41mod` | `-v --ptu2 --sat --IQ 0.0 - 48000 16` |
| RS92 | `rs92mod` | `-v --IQ 0.0 - 48000 16` |
| DFM | `dfm09mod` | `-i -vv --IQ 0.0 --ecc --json --dist --ptu - 48000 16` |
| M10 | `m10mod` | `-v --IQ 0.0 - 48000 16` |
| M20 | `m20mod` | `-v --IQ 0.0 - 48000 16` |
| iMet-54 | `imet54mod` | `-v --IQ 0.0 - 48000 16` |
| LMS6 | `lms6mod` | `-v --IQ 0.0 - 48000 16` |
| MRZ | `mrzmod` | `-v --IQ 0.0 - 48000 16` |

Idle detection: a decoder with no frames for 300 seconds (configurable) is considered idle and is stopped by `DecoderManager`.

---

### 3.4 Decoder Orchestration — `decoder_manager.py`

`DecoderManager` is the dynamic brain of the RTL-SDR and KA9Q pipelines. It maintains a dictionary of `ActiveDecoder` records keyed by frequency (Hz) and reacts to signal lists from the spectrum analyser.

**Core responsibilities:**

- **Signal tracking** — 50 kHz tolerance window prevents false stop/restart on FFT bin drift
- **Capacity management** — respects `receivers.max_concurrent` per device; protects actively-decoding sondes from eviction even when the signal drops from the scan
- **Spectrum analyser interlock** — pauses/resumes `SpectrumAnalyzer` around decoder sessions to avoid RTL-SDR device conflicts on single-dongle setups
- **DFT-then-bandwidth detection** — first attempts `dft_detect` correlation, falls back to bandwidth heuristics
- **Health monitoring** — background thread checks every 5 s for dead processes, dead audio pipelines, and idle decoders
- **Manual decoder API** — `start_manual_decoder(freq, type)` stops all auto-detected decoders and locks a specific frequency/type

```
update_signals(signals)
  │
  ├─ Update existing decoders (50 kHz tolerance match)
  ├─ Stop decoders with no scan match AND no recent frames (30 s grace)
  └─ Start decoders for new signals up to total_capacity
       │
       ├─ _identify_sonde_type()
       │    ├─ DftDetector.detect_sonde_type()   [preferred]
       │    └─ bandwidth heuristic fallback
       │
       └─ _start_decoder_for_signal()
            ├─ MultiChannelAudioPipeline.create_pipeline()
            └─ RS1729Decoder(freq, type).start(audio_stream)
```

---

### 3.5 Web Interface — `web_server.py`

`WebUI` hosts a Flask application serving a real-time Leaflet map and a comprehensive REST JSON API. The map auto-refreshes active sonde positions, draws continuous flight tracks, and overlays station health indicators.

**REST API surface:**

| Endpoint | Method | Description |
|---------|--------|-------------|
| `/` | GET | Leaflet map (index.html template) |
| `/api/sondes` | GET | All active sondes with latest telemetry and full track arrays |
| `/api/sonde/<serial>` | GET | Full telemetry history for one sonde |
| `/api/status` | GET | Frame counters, active frequencies, CPU/RAM metrics |
| `/api/health` | GET | SDR, decoder, MQTT, SondeHub component health |
| `/api/devices` | GET | RTL-SDR device assignments and decoder states |
| `/api/spectrum` | GET | Live spectrum snapshot for waterfall overlay |
| `/api/logs/<serial>` | GET | Per-sonde structured log file download |

**Key features:**

- Per-sonde log files written to `data/logs/` with structured position history
- GPS jump sanity filter (`_sanitize_position_jump`) using Haversine distance + elapsed-time bounds to eliminate corrupted GPS fixes
- Configurable sonde retention time (default 10 min) removes sondes that stop transmitting
- Up to 20,000 track points per sonde (configurable) for full stratospheric ascent/descent
- systemd service status modal with live `journalctl`-like output

---

### 3.6 MQTT Output — `mqtt_output.py`

`MQTTOutput` publishes decoded telemetry to an MQTT broker (default: OpenWX.de) using the OpenWX.de JSON schema. Supports paho-mqtt 1.x and 2.x APIs transparently.

**OpenWX.de payload schema (key fields):**

```json
{
  "gw": "DL2MF",       "active": 1,      "freq": 405.7,
  "id": "V1010401",    "ser": "V1010401", "validId": 1,
  "lat": 53.1,         "lon": 10.02,     "alt": 35121.1,
  "vvel": 3.3,         "speed": 11.8,    "dir": 350.9,
  "sats": 10,          "frame": 8676,    "validPos": 127,
  "tmp": -33.1,        "hum": 0.0,       "pres": 5.3,
  "subtype": "RS41-SGP"
}
```

Features: optional TLS/SSL, username/password authentication, TCP and WebSocket transports, automatic reconnection via `loop_start()`.

---

### 3.7 HTTP Output — `http_output.py`

`HttpOutput` POSTs telemetry to the OpenWX.de legacy HTTP gateway (`gate2.opnwx.de/upaprs.php`) using form-encoded fields compatible with the original `openwx.py` uploader format. Each upload runs in a daemon thread, decoupled from the decoder pipeline.

**Fields:** `callsign`, `ser`, `freq`, `frame`, `subtype`, `latitude`, `longitude`, `altitude`, `vel_v`, `vel_h`, `heading`, `temp`, `humidity`, `pressure`.

---

### 3.8 UDP Output — `udp_output.py`

`UDPOutput` sends compact JSON datagrams via UDP, compatible with the **Horus UDP telemetry protocol** used by `radiosonde_auto_rx` and related amateur radio tools. This enables OpenWXSDR to feed third-party map clients and aggregators that listen on a local UDP port.

Payload mirrors the SondeHub field set: `serial`, `lat`, `lon`, `alt`, `vel_h`, `vel_v`, `heading`, `temp`, `humidity`, `pressure`, `freq`, `snr`.

---

### 3.9 SondeHub Direct Upload — `sondehub_output.py`

`SondeHubOutput` uploads individual telemetry frames to the SondeHub v2 API (`PUT api.v2.sondehub.org/sondes/telemetry`) in a dedicated daemon thread per frame. Per-serial rate limiting (configurable, default 15 s) prevents API overload.

**Features:**

- gzip-compressed JSON array payload with RFC 7231 Date header
- Per-serial subtype and satellite count continuity (remembered across frames)
- Strict serial format validation (RS41, RS92, DFM, M10, iMet, LMS6, MRZ patterns)
- Jittered exponential backoff with `Retry-After` header support
- Periodic listener metadata registration (`/listeners` endpoint)

---

### 3.10 SondeHub Queue Upload — `sondehub_queue.py`

`SondeHubQueueOutput` is an alternative to the direct uploader, designed for high-volume or unstable-link deployments. Frames are enqueued non-blocking into a `queue.Queue` and flushed in configurable batches by a single background worker thread.

```
send_telemetry()
  └─► queue.Queue (max 2000 frames)
        └─► _worker_loop() @ flush_rate_s interval
              └─► _flush_queue_batch() (up to 200 frames/batch)
                    └─► gzip JSON array → PUT sondehub API
```

**Advantages over direct mode:**

| Aspect | Direct (`sondehub_output`) | Queued (`sondehub_queue`) |
|--------|---------------------------|--------------------------|
| Thread per frame | Yes | No (single worker) |
| Batch uploads | No | Yes (up to 200 frames) |
| Back-pressure handling | Rate-limit skip | Queue with drop counter |
| Best for | Low-volume, stable links | High-volume, Raspberry Pi |

---

## 4. Data Flow

The telemetry pipeline is a linear fan-out from SDR hardware to multiple consumers:

```
[RTL-SDR / Airspy / KA9Q / Flux242]
          │  raw IQ (48 kHz 16-bit signed)
          ▼
[MultiChannelAudioPipeline / rtl_fm]
          │  audio stream (stdin pipe)
          ▼
[RS1729Decoder process]
          │  stdout text / JSON lines
          ▼
[DecoderManager._on_frame_decoded()]
          │  raw dict
          ▼
[SondeTelemetry object (models.py)]
          │
    ┌─────┴──────────────────────────────────────┐
    │                                             │
    ▼                                             ▼
[WebUI.add_telemetry()]               [Output plugins]
  • Leaflet map update                  • MQTTOutput
  • Track history append                • HttpOutput
  • GPS sanity filter                   • UDPOutput
  • Per-sonde CSV log write             • SondeHubOutput
                                        • SondeHubQueueOutput
```

---

## 5. SDR Backend Modes

| Mode | Class | Decoder integration | Use case |
|------|-------|--------------------|---------| 
| `rtlsdr` | `RTLSDRDeviceManager` | DecoderManager + per-device workers | 1–N RTL-SDR dongles, automated scanning |
| `airspy` | `AirspyReceiver` | Integrated channelizer | Airspy R2 / Mini, wideband |
| `ka9q` | `KA9QReceiver` | DecoderManager, KA9Q feeds audio | Networked SDR via KA9Q radio |
| `flux242` | `Flux242Receiver` | Autonomous `receivemultisonde.sh` | Pre-built flux242 stack, no internal decoder |

In `rtlsdr` mode with multiple devices, device 0 is reserved exclusively for the spectrum scanner (`SpectrumAnalyzer`); all decoder instances use devices 1–N. In single-device mode the scanner is paused during active decoding sessions.

---

## 6. Sonde Type Detection

Detection uses a two-stage pipeline to maximise accuracy while providing a robust fallback:

**Stage 1 — DFT Correlation (`dft_detect`)**

Captures a short IQ burst from the target frequency and runs `dft_detect` (from `radiosonde_auto_rx`) to correlate against known sonde modulation signatures. This is the most accurate method, especially for distinguishing RS41 from DFM when bandwidths overlap.

**Stage 2 — Bandwidth Heuristic (fallback)**

When DFT detection is unavailable or inconclusive, bandwidth measured by the FFT spectrum analyser is used:

| Bandwidth | Detected type |
|-----------|--------------|
| ≥ 22 kHz | M20 |
| 16–22 kHz | iMet |
| 14–16 kHz | M10 |
| 10–14 kHz | DFM |
| < 10 kHz | RS41 (most common) |

> **Note:** At 2.4 MHz sample rate with a 1024-bin FFT each bin is ~2.3 kHz, so narrow signals like RS41 (~2.7 kHz native) appear inflated to 5–9 kHz. Thresholds are calibrated conservatively to avoid misclassifying RS41 as DFM.

---

## 7. Output Plugin Matrix

| Plugin | Protocol | Destination | Thread model | Compression |
|--------|----------|-------------|-------------|-------------|
| `MQTTOutput` | MQTT 3.1.1 | OpenWX.de broker | `paho loop_start()` | None |
| `HttpOutput` | HTTP POST | gate2.opnwx.de | Daemon thread/frame | None |
| `UDPOutput` | UDP datagram | Local / LAN | Synchronous | None |
| `SondeHubOutput` | HTTPS PUT | api.v2.sondehub.org | Daemon thread/frame | gzip |
| `SondeHubQueueOutput` | HTTPS PUT | api.v2.sondehub.org | Single batch worker | gzip |

All plugins validate sonde serial numbers before transmitting to avoid uploading partial / corrupted decode artefacts (patterns like `-+`, `---`, or strings shorter than 3 characters are rejected).

---

## 8. Deployment Notes

### Systemd Service

OpenWXSDR ships with a `openwxsdr.service` unit. The WebUI exposes live systemd status at `/api/service_status`, including `Active:`, `Loaded:`, `Main PID:`, and recent journal lines.

### Raspberry Pi Optimisations

- Default sample rate 48 kHz per decoder channel (not configurable — hardcoded for rs1729 compatibility)
- `stdbuf -oL` prevents 8 KB pipe buffering delay (~80 s first-frame latency without it)
- `SondeHubQueueOutput` preferred over direct upload on Pi to minimise thread count
- Per-sonde CSV logs capped at 20,000 points to bound memory usage during long flights

### Configuration Keys (excerpt)

```yaml
sdr:
  type: rtlsdr          # rtlsdr | airspy | ka9q | flux242
  rtlsdr:
    devices:
      - serial: "00000001"
        gain: 40
        ppm_error: 0

receivers:
  max_concurrent: 4     # Max simultaneous decoders per device

webui:
  port: 5000
  sonde_retention_time: 600   # seconds
  max_track_points: 20000

sondehub:
  enabled: true
  queue_mode: true            # Use SondeHubQueueOutput
  upload_rate_s: 1
  uploader_callsign: "DL2MF"

openwx:
  mqtt:
    enabled: true
    server: mqtt.openwx.de
    port: 8883
    tls_enabled: true
  http:
    enabled: true
```

### License

OpenWXSDR is free software released under the **GNU General Public License v2.0**.  
Copyright © 2025–2026 Meinhard F. Günther, DL2MF — OpenWX.de  
Free for amateur radio use. No commercial use without permission.  
See <https://www.gnu.org/licenses/gpl-2.0.html>.
