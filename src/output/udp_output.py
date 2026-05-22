"""
# =============================================================================
#  OpenWX -- Open Weather Radiosonde Telemetry System
# =============================================================================
#
#  File   : udp_output.py
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
#  UDP JSON telemetry output plugin for OpenWX.
#
#  Transmits decoded radiosonde telemetry frames as compact JSON datagrams
#  via UDP, compatible with the Horus UDP telemetry protocol used by
#  radiosonde_auto_rx and related tools.
#
#  Payload format : Horus-compatible JSON (serial, lat, lon, alt, vel_h,
#                   vel_v, heading, temp, humidity, pressure, freq, snr, ...)
#  Transport      : UDP unicast (configurable host:port)
#  Reference      : github.com/projecthorus/radiosonde_auto_rx/wiki/
#                   JSON-Telemetry-Format
#
# =============================================================================
"""

import socket
import json
import logging
from typing import Optional
from datetime import datetime

from ..decoders.models import SondeTelemetry


class UDPOutput:
    """Sends telemetry data to OpenWX server via UDP"""
    
    def __init__(self, config: dict):
        self.config = config
        self.logger = logging.getLogger('UDPOutput')
        
        self.enabled = config['output']['udp']['enabled']
        self.host = config['output']['udp']['host']
        self.port = config['output']['udp']['port']
        
        self.sock: Optional[socket.socket] = None
        self.uploader_callsign = config.get('uploader_callsign', 'UNKNOWN')
        
        if self.enabled:
            self._initialize()
    
    def _initialize(self):
        """Initialize UDP socket"""
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.logger.info(f"UDP output initialized: {self.host}:{self.port}")
        except Exception as e:
            self.logger.error(f"Failed to initialize UDP socket: {e}")
            self.enabled = False
    
    def close(self):
        """Close UDP socket"""
        if self.sock:
            self.sock.close()
            self.sock = None
            self.logger.info("UDP output closed")
    
    def send_telemetry(self, telemetry: SondeTelemetry):
        """Send telemetry data to OpenWX server"""
        if not self.enabled or not self.sock:
            return
        
        try:
            # Build OpenWX JSON payload
            payload = self._build_openwx_payload(telemetry)
            
            # Convert to JSON
            json_data = json.dumps(payload, separators=(',', ':'))
            
            # Send via UDP
            self.sock.sendto(json_data.encode('utf-8'), (self.host, self.port))
            
            self.logger.debug(f"Sent telemetry for {telemetry.serial} to OpenWX")
            
        except Exception as e:
            self.logger.error(f"Failed to send UDP data: {e}")
    
    def _build_openwx_payload(self, telemetry: SondeTelemetry) -> dict:
        """
        Build OpenWX-compatible JSON payload
        
        Based on Horus UDP protocol:
        https://github.com/projecthorus/radiosonde_auto_rx/wiki/JSON-Telemetry-Format
        """
        payload = {
            'software_name': 'OpenWXSDR',
            'software_version': '1.0.0',
            'uploader_callsign': self.uploader_callsign,
            'time_received': datetime.utcnow().isoformat() + 'Z',
            
            # Sonde identity
            'manufacturer': self._get_manufacturer(telemetry.sonde_type),
            'type': telemetry.sonde_type,
            'subtype': telemetry.sonde_type,
            'serial': telemetry.serial,
            'frame': telemetry.frame_number,
            
            # Reception info
            'freq': f"{telemetry.frequency / 1e6:.3f}",  # MHz as string
            'snr': round(telemetry.snr, 1) if telemetry.snr else None,
        }
        
        # Position data
        if telemetry.position:
            payload.update({
                'datetime': telemetry.position.datetime.isoformat() + 'Z',
                'lat': round(telemetry.position.latitude, 5),
                'lon': round(telemetry.position.longitude, 5),
                'alt': round(telemetry.position.altitude, 1),
            })
        
        # Velocity data
        if telemetry.velocity:
            payload.update({
                'vel_h': round(telemetry.velocity.horizontal_speed, 1),
                'vel_v': round(telemetry.velocity.vertical_speed, 1),
                'heading': round(telemetry.velocity.heading, 1),
            })
        
        # Environmental data
        if telemetry.environment:
            if telemetry.environment.temperature is not None:
                payload['temp'] = round(telemetry.environment.temperature, 1)
            if telemetry.environment.humidity is not None:
                payload['humidity'] = round(telemetry.environment.humidity, 1)
            if telemetry.environment.pressure is not None:
                payload['pressure'] = round(telemetry.environment.pressure, 2)
        
        return payload
    
    def _get_manufacturer(self, sonde_type: str) -> str:
        """Get manufacturer name from sonde type"""
        manufacturers = {
            'RS41': 'Vaisala',
            'RS92': 'Vaisala',
            'DFM': 'Graw',
            'M10': 'Meteomodem',
            'M20': 'Meteomodem',
            'iMet': 'InterMet',
            'LMS6': 'Lockheed Martin',
            'MRZ': 'Meteo-Radiy'
        }
        return manufacturers.get(sonde_type, 'Unknown')
