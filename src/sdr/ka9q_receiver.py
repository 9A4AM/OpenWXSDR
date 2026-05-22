"""
# =============================================================================
#  OpenWX -- Open Weather Radiosonde Telemetry System
# =============================================================================
#
#  File   : ka9q_receiver.py
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
#  KA9Q Radio multicast receiver interface for OpenWX.
#
#  Provides KA9QReceiver, a UDP multicast client that subscribes to IQ
#  audio streams distributed by a KA9Q Radio server over RTP/multicast.
#  Channel allocation and release are handled via KA9Q control messages.
#
#  Architecture:
#    KA9Q Radio server (multicast) ? UDP socket ? KA9QReceiver ? channel IQ
#
#  Note: RTP packet parsing is partially implemented as a placeholder;
#  full protocol support depends on the target KA9Q Radio version.
#
#  External dependency : KA9Q Radio (github.com/ka9q/ka9q-radio)
#
# =============================================================================
"""

import socket
import struct
import logging
import threading
import time
from typing import Optional, Dict, List
from dataclasses import dataclass


@dataclass
class KA9QSignal:
    """Represents a signal from KA9Q radio"""
    frequency: float
    strength: float
    timestamp: float


class KA9QReceiver:
    """Interface to KA9Q radio multicast streams"""
    
    def __init__(self, config: dict):
        self.config = config
        self.logger = logging.getLogger('KA9QReceiver')
        self.running = False
        self.sock = None
        self.active_channels: Dict[float, KA9QSignal] = {}
        self.lock = threading.Lock()
    
    def initialize(self) -> bool:
        """Initialize KA9Q multicast receiver"""
        try:
            ka9q_config = self.config['sdr']['ka9q']
            multicast_group = ka9q_config['multicast_group']
            port = ka9q_config['port']
            interface = ka9q_config.get('interface', '')
            
            # Create UDP socket
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            
            # Bind to port
            self.sock.bind(('', port))
            
            # Join multicast group
            mreq = struct.pack("4sl", socket.inet_aton(multicast_group), socket.INADDR_ANY)
            self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
            
            self.logger.info(f"KA9Q receiver initialized: {multicast_group}:{port}")
            return True
            
        except Exception as e:
            self.logger.error(f"Failed to initialize KA9Q receiver: {e}")
            return False
    
    def close(self):
        """Close KA9Q receiver"""
        if self.sock:
            self.sock.close()
            self.sock = None
            self.logger.info("KA9Q receiver closed")
    
    def start_receiving(self):
        """Start receiving KA9Q data in background thread"""
        if self.running:
            self.logger.warning("KA9Q receiver already running")
            return
        
        self.running = True
        self.recv_thread = threading.Thread(target=self._receive_loop, daemon=True)
        self.recv_thread.start()
        self.logger.info("KA9Q receiver started")
    
    def stop_receiving(self):
        """Stop receiving KA9Q data"""
        self.running = False
        if hasattr(self, 'recv_thread'):
            self.recv_thread.join(timeout=5)
        self.logger.info("KA9Q receiver stopped")
    
    def _receive_loop(self):
        """Background receive loop"""
        while self.running:
            try:
                # Receive data with timeout
                self.sock.settimeout(1.0)
                data, addr = self.sock.recvfrom(65536)
                
                # Parse KA9Q metadata (simplified - actual format depends on KA9Q version)
                # This is a placeholder for the actual KA9Q protocol parsing
                self._parse_ka9q_packet(data)
                
            except socket.timeout:
                continue
            except Exception as e:
                self.logger.error(f"Error in KA9Q receive loop: {e}", exc_info=True)
                time.sleep(1)
    
    def _parse_ka9q_packet(self, data: bytes):
        """
        Parse KA9Q packet (placeholder - actual implementation depends on KA9Q format)
        KA9Q uses RTP packets with custom metadata
        """
        # This is a simplified placeholder
        # Real implementation would parse RTP header and KA9Q metadata
        try:
            # Extract basic info (this is hypothetical)
            if len(data) < 12:
                return
            
            # For now, just log that we received data
            # Real implementation would extract frequency, signal strength, etc.
            pass
            
        except Exception as e:
            self.logger.debug(f"Error parsing KA9Q packet: {e}")
    
    def get_available_channels(self) -> List[KA9QSignal]:
        """Get list of available channels from KA9Q"""
        with self.lock:
            # Remove stale channels (older than 60 seconds)
            current_time = time.time()
            self.active_channels = {
                freq: sig for freq, sig in self.active_channels.items()
                if current_time - sig.timestamp < 60
            }
            return list(self.active_channels.values())
    
    def request_channel(self, frequency: float, bandwidth: float = 12000) -> bool:
        """
        Request a new channel from KA9Q radio
        Returns True if successful
        
        Note: This requires KA9Q radio control protocol support
        """
        # Placeholder for KA9Q channel control
        # Real implementation would send control messages to KA9Q
        self.logger.info(f"Requesting KA9Q channel at {frequency/1e6:.4f} MHz")
        
        # For now, assume success
        with self.lock:
            self.active_channels[frequency] = KA9QSignal(
                frequency=frequency,
                strength=0.0,
                timestamp=time.time()
            )
        return True
    
    def release_channel(self, frequency: float):
        """Release a KA9Q channel"""
        with self.lock:
            if frequency in self.active_channels:
                del self.active_channels[frequency]
                self.logger.info(f"Released KA9Q channel at {frequency/1e6:.4f} MHz")
