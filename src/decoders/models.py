"""
# =============================================================================
#  OpenWX -- Open Weather Radiosonde Telemetry System
# =============================================================================
#
#  File   : models.py
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
#  Shared data model definitions for OpenWX radiosonde telemetry.
#
#  Defines the canonical Python dataclasses used across the entire OpenWX
#  pipeline - from decoder output parsing through output plugins to the
#  web UI - ensuring a consistent, type-safe telemetry representation.
#
#  Dataclasses:
#    SondePosition    : GPS fix (latitude, longitude, altitude MSL, datetime)
#    SondeVelocity    : Horizontal speed (m/s), vertical speed (m/s), heading
#    SondeEnvironment : Optional PTU sensor data (temp C, humidity %, pressure hPa)
#    SondeTelemetry   : Full frame container aggregating all of the above,
#                       plus identity (type, serial, subtype), RF reception
#                       metrics (frequency, SNR, RSSI, satellite count), and
#                       decoder metadata; includes to_dict() for JSON output.
#
# =============================================================================
"""

from dataclasses import dataclass, field
from typing import Optional
from datetime import datetime


@dataclass
class SondePosition:
    """GPS position data"""
    latitude: float
    longitude: float
    altitude: float  # meters above MSL
    datetime: datetime
    
    
@dataclass
class SondeVelocity:
    """Velocity data"""
    horizontal_speed: float  # m/s
    vertical_speed: float    # m/s
    heading: float          # degrees (0-360)


@dataclass
class SondeEnvironment:
    """Environmental sensor data"""
    temperature: Optional[float] = None  # Celsius
    humidity: Optional[float] = None     # %
    pressure: Optional[float] = None     # hPa


@dataclass
class SondeTelemetry:
    """Complete radiosonde telemetry frame"""
    # Identity
    sonde_type: str
    serial: str
    frame_number: int
    subtype: Optional[str] = None  # Subtype like "DFM17", "RS41-SGP", etc.
    
    # Position & velocity
    position: Optional[SondePosition] = None
    velocity: Optional[SondeVelocity] = None
    
    # Environment
    environment: Optional[SondeEnvironment] = None
    
    # Reception info
    frequency: float = 0.0  # Hz
    snr: float = 0.0        # dB
    rssi: float = 0.0       # dBm
    satellites: Optional[int] = None  # GPS SVs used
    battery: Optional[float] = None  # Battery voltage
    
    # RS41-specific fields
    burst_timer: Optional[int] = None  # RS41 burst timer (seconds)
    rs41_mainboard: Optional[str] = None  # RS41 mainboard type (e.g., "RSM424")
    rs41_mainboard_fw: Optional[int] = None  # RS41 mainboard firmware version
    ref_datetime: Optional[str] = None  # RS41 datetime reference (e.g., "GPS")
    ref_position: Optional[str] = None  # RS41 position reference (e.g., "GPS")
    tx_frequency: Optional[int] = None  # Transmit frequency in Hz (from decoder)
    
    # Metadata
    timestamp: datetime = field(default_factory=datetime.utcnow)
    decoder_name: str = ""
    decoder_version: str = ""
    
    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization"""
        data = {
            'type': self.sonde_type,
            'id': self.serial,
            'frame': self.frame_number,
            'frequency': self.frequency / 1e6,  # Convert to MHz
            'snr': self.snr,
            'rssi': self.rssi,
            'timestamp': self.timestamp.isoformat() + 'Z'
        }
        
        if self.subtype:
            data['subtype'] = self.subtype
        if self.satellites is not None:
            data['sats'] = self.satellites
        
        if self.position:
            data.update({
                'lat': self.position.latitude,
                'lon': self.position.longitude,
                'alt': self.position.altitude,
                'datetime': self.position.datetime.isoformat() + 'Z'
            })
        
        if self.velocity:
            data.update({
                'vel_h': self.velocity.horizontal_speed,
                'vel_v': self.velocity.vertical_speed,
                'heading': self.velocity.heading
            })
        
        if self.environment:
            if self.environment.temperature is not None:
                data['temp'] = self.environment.temperature
            if self.environment.humidity is not None:
                data['humidity'] = self.environment.humidity
            if self.environment.pressure is not None:
                data['pressure'] = self.environment.pressure
        
        return data
