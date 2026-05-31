"""
# =============================================================================
#  OpenWX -- Open Weather Radiosonde Telemetry System
# =============================================================================
#
#  File   : http_output.py
#  Author : M.F. Guenther, DL2MF - DL2MF@darc.de
#  License: GNU General Public License v2.0 (GPL-2.0)
#
#  This program is free software; you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation; version 2 of the License.
#
#  This program is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with this program. If not, see <https://www.gnu.org/licenses/>.
#
# -----------------------------------------------------------------------------
#  Description
# -----------------------------------------------------------------------------
#
#  HTTP telemetry upload output plugin for OpenWX.
#
#  Uploads decoded radiosonde telemetry frames to the OpenWX.de HTTP gateway
#  via POST requests, compatible with the legacy openwx.py uploader format.
#  Each upload runs in a background daemon thread to avoid blocking the
#  main decoder pipeline.
#
#  Endpoint  : POST http://gate2.opnwx.de/upaprs.php
#  Payload   : form-encoded fields (callsign, lat, lon, alt, freq,
#              temp, humidity, pressure, vel_v, vel_h, heading, ...)
#  Dependency: requests
#
# =============================================================================
"""

import logging
import threading
from datetime import datetime
from typing import Optional

from ..decoders.models import SondeTelemetry


class HttpOutput:
    """
    Sends telemetry frames to the OpenWX.de HTTP gateway.
    Uses a background thread so uploads never block the decoder loop.
    """

    UPLOAD_URL = 'http://gate2.opnwx.de/upaprs.php'

    def __init__(self, config: dict):
        self.config  = config
        self.logger  = logging.getLogger('HttpOutput')

        http_cfg = config.get('openwx', {}).get('http', {})
        self.enabled  = http_cfg.get('enabled', False)
        # Allow overriding the URL from config; default to legacy gate2 endpoint
        self.url      = http_cfg.get('url', self.UPLOAD_URL)
        self.api_key  = http_cfg.get('api_key', '')

        if self.enabled:
            try:
                import requests as _req  # noqa: F401 – verify available at startup
            except ImportError:
                self.logger.error(
                    "requests library not installed. "
                    "Run: pip install requests"
                )
                self.enabled = False
                return
            self.logger.info(f"HTTP upload enabled → {self.url}")

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def send_telemetry(self, telemetry: SondeTelemetry):
        """Queue a telemetry frame for HTTP upload (non-blocking)."""
        if not self.enabled:
            return
        t = threading.Thread(
            target=self._upload,
            args=(telemetry,),
            daemon=True,
            name=f"HttpUpload-{telemetry.serial}"
        )
        t.start()

    def close(self):
        """No persistent connections to close."""
        pass

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _build_payload(self, telemetry: SondeTelemetry) -> dict:
        station_cfg = self.config.get('station', {})
        callsign    = station_cfg.get('callsign', 'OPENWXSDR')
        
        sonde_serial = telemetry.serial or 'UNKNOWN'
        
        # Reject obviously malformed serials (partial decode artifacts like '-+', '---', etc.)
        import re
        if sonde_serial != 'UNKNOWN' and re.match(r'^[-+\s]+$', sonde_serial):
            self.logger.warning(
                f"[HTTP] Rejecting malformed serial '{sonde_serial}' (likely partial decode), "
                f"skipping HTTP upload"
            )
            return {}  # Return empty payload to skip upload

        data = {
            'callsign':  callsign,
            'ser':       sonde_serial,
            'freq':      round(telemetry.frequency / 1e6, 3),
            'frame':     telemetry.frame_number,
            'subtype':   telemetry.subtype or telemetry.sonde_type or '',
            # Position
            'latitude':  0.0,
            'longitude': 0.0,
            'altitude':  0.0,
            # Velocity
            'vel_v':     0.0,
            'vel_h':     0.0,
            'speed':     0.0,
            'heading':   0.0,
            # Environment
            'temp':      '',
            'humidity':  '',
            'pressure':  '',
            'batt':      '',
        }

        if self.api_key:
            data['api_key'] = self.api_key

        if telemetry.position:
            pos = telemetry.position
            data['latitude']  = round(pos.latitude,  5)
            data['longitude'] = round(pos.longitude, 5)
            data['altitude']  = round(pos.altitude,  1)

        if telemetry.velocity:
            vel = telemetry.velocity
            data['vel_v']   = round(vel.vertical_speed,   1)
            data['vel_h']   = round(vel.horizontal_speed, 1)
            data['speed']   = round(vel.horizontal_speed, 1)
            data['heading'] = round(vel.heading,          1)

        if telemetry.environment:
            env = telemetry.environment
            if env.temperature is not None:
                data['temp']     = round(env.temperature, 1)
            if env.humidity is not None:
                data['humidity'] = round(env.humidity,    1)
            if env.pressure is not None:
                data['pressure'] = round(env.pressure,    2)

        # Battery
        if telemetry.battery is not None:
            data['batt'] = round(telemetry.battery, 2)

        # RS41-specific fields
        if hasattr(telemetry, 'burst_timer') and telemetry.burst_timer is not None:
            data['burst_timer'] = int(telemetry.burst_timer)
        if hasattr(telemetry, 'rs41_mainboard') and telemetry.rs41_mainboard is not None:
            data['rs41_mainboard'] = str(telemetry.rs41_mainboard)
        if hasattr(telemetry, 'rs41_mainboard_fw') and telemetry.rs41_mainboard_fw is not None:
            data['rs41_mainboard_fw'] = int(telemetry.rs41_mainboard_fw)
        if hasattr(telemetry, 'ref_datetime') and telemetry.ref_datetime is not None:
            data['ref_datetime'] = str(telemetry.ref_datetime)
        if hasattr(telemetry, 'ref_position') and telemetry.ref_position is not None:
            data['ref_position'] = str(telemetry.ref_position)
        if hasattr(telemetry, 'tx_frequency') and telemetry.tx_frequency is not None:
            data['tx_frequency'] = int(telemetry.tx_frequency)

        return data

    def _upload(self, telemetry: SondeTelemetry):
        try:
            import requests
            payload = self._build_payload(telemetry)
            
            # Skip if payload is empty (malformed serial rejected)
            if not payload:
                self.logger.debug(f"HTTP: Empty payload for {telemetry.serial}, skipping upload")
                return
            
            resp = requests.post(self.url, data=payload, timeout=10)
            self.logger.debug(
                f"HTTP upload {telemetry.serial} → "
                f"{resp.status_code} {resp.text[:80]}"
            )
        except Exception as exc:
            self.logger.warning(f"HTTP upload failed for {telemetry.serial}: {exc}")
