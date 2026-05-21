# OpenWXSDR API Reference

## REST API Endpoints

The OpenWXSDR web server provides the following REST API endpoints for accessing radiosonde data.

Base URL: `http://<raspberry-pi-ip>:5000`

### GET /

**Description:** Main web interface with interactive map.

**Response:** HTML page

---

### GET /api/sondes

**Description:** Get all active sondes with their complete telemetry history.

**Response:**
```json
{
  "sondes": [
    {
      "serial": "T1234567",
      "type": "RS41",
      "latest": {
        "type": "RS41",
        "id": "T1234567",
        "frame": 12345,
        "frequency": 402.700,
        "snr": 25.5,
        "lat": 51.5074,
        "lon": -0.1278,
        "alt": 15420.5,
        "vel_h": 12.3,
        "vel_v": 5.2,
        "heading": 285.5,
        "temp": -45.2,
        "humidity": 25.5,
        "pressure": 150.25,
        "datetime": "2026-04-30T12:34:56.000Z",
        "timestamp": "2026-04-30T12:34:56.789Z"
      },
      "path": [
        { /* telemetry frame 1 */ },
        { /* telemetry frame 2 */ },
        // ... up to 1000 frames
      ]
    }
  ],
  "timestamp": "2026-04-30T12:34:56.789Z"
}
```

**Fields:**
- `serial`: Radiosonde serial number
- `type`: Sonde type (RS41, RS92, DFM, etc.)
- `latest`: Most recent telemetry frame
- `path`: Array of all telemetry frames for this sonde
- `timestamp`: Server timestamp

---

### GET /api/sonde/<serial>

**Description:** Get telemetry for a specific radiosonde by serial number.

**Parameters:**
- `serial` (path): Radiosonde serial number (e.g., "T1234567")

**Response:**
```json
{
  "serial": "T1234567",
  "telemetry": [
    {
      "type": "RS41",
      "id": "T1234567",
      "frame": 12345,
      "frequency": 402.700,
      "snr": 25.5,
      "lat": 51.5074,
      "lon": -0.1278,
      "alt": 15420.5,
      "vel_h": 12.3,
      "vel_v": 5.2,
      "heading": 285.5,
      "temp": -45.2,
      "humidity": 25.5,
      "pressure": 150.25,
      "datetime": "2026-04-30T12:34:56.000Z",
      "timestamp": "2026-04-30T12:34:56.789Z"
    }
  ]
}
```

---

### GET /api/status

**Description:** Get system status and statistics.

**Response:**
```json
{
  "active_sondes": 3,
  "total_frames": 45678,
  "timestamp": "2026-04-30T12:34:56.789Z"
}
```

**Fields:**
- `active_sondes`: Number of currently tracked sondes
- `total_frames`: Total telemetry frames received
- `timestamp`: Server timestamp

---

## Telemetry Data Format

### Complete Telemetry Object

```json
{
  "type": "RS41",
  "id": "T1234567",
  "frame": 12345,
  "frequency": 402.700,
  "snr": 25.5,
  "lat": 51.5074,
  "lon": -0.1278,
  "alt": 15420.5,
  "vel_h": 12.3,
  "vel_v": 5.2,
  "heading": 285.5,
  "temp": -45.2,
  "humidity": 25.5,
  "pressure": 150.25,
  "datetime": "2026-04-30T12:34:56.000Z",
  "timestamp": "2026-04-30T12:34:56.789Z"
}
```

### Field Descriptions

| Field | Type | Unit | Description |
|-------|------|------|-------------|
| `type` | string | - | Radiosonde type (RS41, RS92, DFM, M10, M20, iMet, LMS6, MRZ) |
| `id` | string | - | Serial number |
| `frame` | integer | - | Frame number from radiosonde |
| `frequency` | float | MHz | Reception frequency |
| `snr` | float | dB | Signal-to-noise ratio |
| `lat` | float | degrees | Latitude (WGS84) |
| `lon` | float | degrees | Longitude (WGS84) |
| `alt` | float | meters | Altitude above mean sea level |
| `vel_h` | float | m/s | Horizontal speed |
| `vel_v` | float | m/s | Vertical speed (climb rate) |
| `heading` | float | degrees | Heading (0-360, 0=North) |
| `temp` | float | °C | Temperature (optional) |
| `humidity` | float | % | Relative humidity (optional) |
| `pressure` | float | hPa | Atmospheric pressure (optional) |
| `datetime` | string | ISO8601 | Radiosonde GPS time |
| `timestamp` | string | ISO8601 | Reception time (server) |

**Notes:**
- Environmental sensors (temp, humidity, pressure) are optional and depend on sonde type
- All timestamps are in UTC
- Position data uses WGS84 coordinate system

---

## UDP Output Format (OpenWX/Horus Protocol)

OpenWXSDR sends telemetry to OpenWX servers via UDP in JSON format compatible with the Horus protocol.

**Destination:** Configured in `config.yaml` under `output.udp`

**Format:**
```json
{
  "software_name": "OpenWXSDR",
  "software_version": "1.0.45",
  "uploader_callsign": "YOUR_CALL",
  "time_received": "2026-04-30T12:34:56.789Z",
  "manufacturer": "Vaisala",
  "type": "RS41",
  "subtype": "RS41",
  "serial": "T1234567",
  "frame": 12345,
  "freq": "402.700",
  "snr": 25.5,
  "datetime": "2026-04-30T12:34:56.000Z",
  "lat": 51.50740,
  "lon": -0.12780,
  "alt": 15420.5,
  "vel_h": 12.3,
  "vel_v": 5.2,
  "heading": 285.5,
  "temp": -45.2,
  "humidity": 25.5,
  "pressure": 150.25
}
```

**Additional Fields:**
- `manufacturer`: Derived from sonde type
- `uploader_callsign`: Your station identifier from config
- `time_received`: When data was received by OpenWXSDR
- `freq`: String representation of frequency in MHz

---

## Python API Examples

### Fetching Current Sondes

```python
import requests
import json

# Get all sondes
response = requests.get('http://192.168.1.100:5000/api/sondes')
data = response.json()

for sonde in data['sondes']:
    print(f"Sonde: {sonde['serial']} ({sonde['type']})")
    latest = sonde['latest']
    if latest:
        print(f"  Position: {latest['lat']:.5f}, {latest['lon']:.5f}")
        print(f"  Altitude: {latest['alt']:.0f} m")
```

### Monitoring Specific Sonde

```python
import requests
import time

serial = "T1234567"

while True:
    response = requests.get(f'http://192.168.1.100:5000/api/sonde/{serial}')
    data = response.json()
    
    if data['telemetry']:
        latest = data['telemetry'][-1]
        print(f"Alt: {latest['alt']:.0f} m, Speed: {latest['vel_v']:.1f} m/s")
    
    time.sleep(5)
```

### System Status Dashboard

```python
import requests
import time

def get_status():
    response = requests.get('http://192.168.1.100:5000/api/status')
    return response.json()

while True:
    status = get_status()
    print(f"Active sondes: {status['active_sondes']}, "
          f"Frames: {status['total_frames']}")
    time.sleep(10)
```

---

## JavaScript API Examples

### Real-time Updates with Fetch

```javascript
async function updateSondes() {
    const response = await fetch('/api/sondes');
    const data = await response.json();
    
    data.sondes.forEach(sonde => {
        console.log(`${sonde.serial}: ${sonde.latest.alt} m`);
    });
}

// Update every 2 seconds
setInterval(updateSondes, 2000);
```

### WebSocket Alternative (Future Enhancement)

Currently, OpenWXSDR uses polling. For real-time updates, poll `/api/sondes` every 1-2 seconds.

---

## CORS Support

OpenWXSDR includes CORS headers, allowing access from web applications on different domains.

---

## Rate Limiting

No rate limiting is currently implemented. Be respectful with API calls:
- Recommended polling interval: 1-5 seconds
- Batch requests when possible
- Cache data locally

---

## Error Responses

All endpoints return JSON errors in the following format:

```json
{
  "error": "Error message"
}
```

**HTTP Status Codes:**
- `200 OK`: Success
- `404 Not Found`: Sonde not found
- `500 Internal Server Error`: Server error
