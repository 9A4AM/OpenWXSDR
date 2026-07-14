"""
# =============================================================================
#  OpenWX -- Open Weather Radiosonde Telemetry System
# =============================================================================
#
#  File   : channelizer_status.py
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
#  UDP status output for channelizer active slots.
#
#  Sends periodic status updates showing active decoder channels, similar to
#  receivemultisonde's slot status output. Can be monitored with:
#    nc -luk 5675
#
#  Format:
#    YYYYMMDD-HHMMSS ----------------------------------------
#    active slots:
#    RTL00001: 405.700 MHz RS41 V1010940 SNR 15.2 dB
#    RTL00001: 404.250 MHz DFM S1234567 SNR 12.8 dB
#
# =============================================================================
"""

import socket
import logging
import time
from typing import Dict, List, Optional
from datetime import datetime


class ChannelizerStatusOutput:
    """
    Sends periodic status updates for channelizer active slots via UDP.
    
    This allows real-time monitoring of multi-channel decoder activity,
    similar to receivemultisonde's status output.
    """
    
    def __init__(self, config: dict):
        self.config = config
        self.logger = logging.getLogger('ChannelizerStatus')
        
        # Configuration
        channelizer_cfg = config.get('output', {}).get('channelizer_status', {})
        self.enabled = channelizer_cfg.get('enabled', False)
        self.host = channelizer_cfg.get('host', '127.0.0.1')
        self.port = channelizer_cfg.get('port', 5675)
        self.update_interval = channelizer_cfg.get('update_interval', 30)
        
        self.sock: Optional[socket.socket] = None
        self._last_update = 0  # Timestamp of last status send
        
        if self.enabled:
            self._initialize()
        else:
            self.logger.info("Channelizer status output disabled (enabled=false in config)")
    
    def _initialize(self):
        """Initialize UDP socket"""
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.logger.info(
                f"Channelizer status output initialized: {self.host}:{self.port} "
                f"(interval={self.update_interval}s)"
            )
        except Exception as e:
            self.logger.error(f"Failed to initialize channelizer status UDP socket: {e}")
            self.enabled = False
    
    def close(self):
        """Close UDP socket"""
        if self.sock:
            self.sock.close()
            self.sock = None
            self.logger.info("Channelizer status output closed")
    
    def should_send_update(self) -> bool:
        """Check if it's time to send a status update"""
        if not self.enabled or not self.sock:
            return False
        
        now = time.time()
        if now - self._last_update >= self.update_interval:
            self._last_update = now
            return True
        return False
    
    def send_status(self, device_statuses: Dict[str, dict]):
        """
        Send status update for all devices with channelizer mode.
        
        Args:
            device_statuses: Dict mapping device_serial -> status dict
                status dict contains:
                - decoder_mode: 'legacy' or 'channelizer'
                - channelizer_active: list of active channel info dicts
                  Each channel dict: {frequency, sonde_type, sonde_serial, snr}
        """
        if not self.enabled or not self.sock:
            return
        
        try:
            # Build status message
            timestamp = datetime.utcnow().strftime('%Y%m%d-%H%M%S')
            lines = [
                f"{timestamp} ----------------------------------------",
                "active slots:"
            ]
            
            # Collect all active channels from channelizer devices
            active_count = 0
            for device_serial, status in device_statuses.items():
                if status.get('decoder_mode') != 'channelizer':
                    continue
                
                channels = status.get('channelizer_active', [])
                for ch in channels:
                    freq_mhz = ch.get('frequency', 0) / 1e6
                    sonde_type = ch.get('sonde_type', 'UNKNOWN')
                    sonde_serial = ch.get('sonde_serial', 'N/A')
                    snr = ch.get('snr', 0)
                    
                    lines.append(
                        f"{device_serial}: {freq_mhz:.3f} MHz {sonde_type} {sonde_serial} "
                        f"SNR {snr:.1f} dB"
                    )
                    active_count += 1
            
            # If no active channels, just show empty slots
            if active_count == 0:
                lines.append("(no active channels)")
            
            # Send message
            message = '\n'.join(lines) + '\n'
            self.sock.sendto(message.encode('utf-8'), (self.host, self.port))
            
            self.logger.debug(f"Sent status: {active_count} active channel(s)")
            
        except Exception as e:
            self.logger.error(f"Failed to send channelizer status: {e}", exc_info=True)
