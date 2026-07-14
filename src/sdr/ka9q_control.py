"""
# =============================================================================
#  OpenWX -- Open Weather Radiosonde Telemetry System
# =============================================================================
#
#  File   : ka9q_control.py
#  Author : M.F. Guenther, DL2MF - DL2MF@darc.de
#  License: GNU General Public License v2.0 (GPL-2.0)
#
#  KA9Q Radio control interface for dynamic channel management.
#
#  Uses the 'tune' binary from ka9q-radio to create/delete channels dynamically.
#  Based on radiosonde_auto_rx implementation by Mark Jessop VK5QI.
#
#  Commands:
#    - CREATE: tune --frequency <freq> --ssrc <ssrc> --radio <host>
#    - DELETE: tune --frequency 0 --ssrc <ssrc> --radio <host>
#
#  SSRC Format: {frequency_in_khz}{01} for decode, {frequency_in_khz}{04} for scan
#  Example: 404.090 MHz → SSRC 404090001 (decode) or 404090004 (scan)
#
# =============================================================================
"""

import subprocess
import logging
import shutil
from typing import Optional, Dict, List
from dataclasses import dataclass


@dataclass
class KA9QChannel:
    """Represents a KA9Q demodulator channel"""
    name: str
    frequency: float  # Hz
    mode: str         # 'iq' for IQ data
    samprate: int     # Sample rate in Hz
    ssrc: int         # RTP SSRC identifier
    scan: bool = False  # True for scan channels, False for decode


class KA9QControl:
    """KA9Q Radio control interface using 'tune' binary"""
    
    def __init__(self, radio_hostname: str = 'sonde.local', tune_path: str = 'tune', max_channels: int = 10):
        """
        Initialize KA9Q control interface
        
        Args:
            radio_hostname: KA9Q radio hostname (default: 'sonde.local')
            tune_path: Path to 'tune' binary (default: 'tune' in PATH)
            max_channels: Maximum concurrent channels (default: 10)
        """
        self.radio_hostname = radio_hostname
        self.tune_path = tune_path
        self.logger = logging.getLogger('KA9QControl')
        self.active_channels: Dict[int, KA9QChannel] = {}  # SSRC -> channel
        self.max_channels = max_channels  # Maximum concurrent channels
        
    def initialize(self) -> bool:
        """Check if tune binary is available"""
        try:
            # Check if 'tune' binary exists
            tune_location = shutil.which(self.tune_path)
            if not tune_location:
                self.logger.error(f"KA9Q 'tune' binary not found at '{self.tune_path}'")
                self.logger.error("Install ka9q-radio tools or specify correct path")
                return False
            
            self.tune_path = tune_location
            self.logger.info(f"KA9Q control interface initialized using: {self.tune_path}")
            self.logger.info(f"Radio hostname: {self.radio_hostname}")
            return True
            
        except Exception as e:
            self.logger.error(f"Failed to initialize KA9Q control: {e}")
            return False
    
    def _generate_ssrc(self, frequency: float, scan: bool = False) -> int:
        """
        Generate SSRC from frequency
        
        Args:
            frequency: Frequency in Hz
            scan: True for scan channel (suffix 04), False for decode (suffix 01)
            
        Returns:
            SSRC integer (e.g., 404090001 for 404.090 MHz decode)
        """
        freq_khz = round(frequency / 1000)
        suffix = "04" if scan else "01"
        return int(f"{freq_khz}{suffix}")
    
    def create_channel(
        self,
        name: str,
        frequency: float,
        mode: str = 'iq',
        samprate: int = 48000,
        scan: bool = False,
        channel_filter: Optional[float] = None
    ) -> bool:
        """
        Create a new demodulator channel using 'tune' command
        
        Args:
            name: Channel name (for tracking)
            frequency: Center frequency in Hz
            mode: Demodulation mode ('iq' for radiosondes)
            samprate: Sample rate in Hz
            scan: True for scan channel, False for decode channel
            channel_filter: Optional channel filter bandwidth in Hz
            
        Returns:
            True if channel created successfully
        """
        if len(self.active_channels) >= self.max_channels:
            self.logger.warning(f"Cannot create channel {name}: max channels ({self.max_channels}) reached")
            return False
        
        # Generate SSRC
        ssrc = self._generate_ssrc(frequency, scan)
        
        if ssrc in self.active_channels:
            self.logger.warning(f"Channel with SSRC {ssrc} already exists")
            return False
        
        try:
            # Calculate filter bandwidth
            if channel_filter:
                low = int(channel_filter * -1.0)
                high = int(channel_filter)
            else:
                low = int(int(samprate) / (-2.4))
                high = int(int(samprate) / 2.4)
            
            # Build tune command
            cmd = [
                self.tune_path,
                '--samprate', str(int(samprate)),
                '--mode', mode,
                '--low', str(low),
                '--high', str(high),
                '--frequency', str(int(frequency)),
                '--ssrc', str(ssrc),
                '--radio', self.radio_hostname
            ]
            
            self.logger.debug(f"Creating channel: {' '.join(cmd)}")
            
            # Execute command with timeout
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=10
            )
            
            if result.returncode != 0:
                self.logger.error(f"Failed to create channel {name}: {result.stderr}")
                return False
            
            # Track channel
            channel = KA9QChannel(
                name=name,
                frequency=frequency,
                mode=mode,
                samprate=samprate,
                ssrc=ssrc,
                scan=scan
            )
            self.active_channels[ssrc] = channel
            
            self.logger.info(f"Created channel {name} (SSRC {ssrc}) at {frequency/1e6:.4f} MHz")
            return True
            
        except subprocess.TimeoutExpired:
            self.logger.error(f"Timeout creating channel {name}")
            return False
        except Exception as e:
            self.logger.error(f"Failed to create channel {name}: {e}")
            return False
    
    def delete_channel(self, ssrc: int) -> bool:
        """
        Delete a demodulator channel using 'tune' command
        
        To delete a channel in KA9Q, set its frequency to 0.
        
        Args:
            ssrc: SSRC of channel to delete
            
        Returns:
            True if channel deleted successfully
        """
        if ssrc not in self.active_channels:
            self.logger.warning(f"Channel with SSRC {ssrc} not found")
            return False
        
        channel = self.active_channels[ssrc]
        
        try:
            # Build tune command with frequency=0 to delete
            cmd = [
                self.tune_path,
                '--samprate', str(channel.samprate),
                '--mode', channel.mode,
                '--frequency', '0',  # Frequency 0 = delete channel
                '--ssrc', str(ssrc),
                '--radio', self.radio_hostname
            ]
            
            self.logger.debug(f"Deleting channel: {' '.join(cmd)}")
            
            # Execute command with timeout
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=10
            )
            
            if result.returncode != 0:
                self.logger.error(f"Failed to delete channel {channel.name}: {result.stderr}")
                return False
            
            # Remove from tracking
            del self.active_channels[ssrc]
            
            self.logger.info(f"Deleted channel {channel.name} (SSRC {ssrc})")
            return True
            
        except subprocess.TimeoutExpired:
            self.logger.error(f"Timeout deleting channel {channel.name}")
            return False
        except Exception as e:
            self.logger.error(f"Failed to delete channel {channel.name}: {e}")
            return False
    
    def delete_channel_by_frequency(self, frequency: float, scan: bool = False) -> bool:
        """
        Delete channel by frequency
        
        Args:
            frequency: Frequency in Hz
            scan: True if it was a scan channel
            
        Returns:
            True if deleted successfully
        """
        ssrc = self._generate_ssrc(frequency, scan)
        return self.delete_channel(ssrc)
    
    def list_channels(self) -> List[KA9QChannel]:
        """Get list of active channels"""
        return list(self.active_channels.values())
    
    def get_channel_count(self) -> int:
        """Get number of active channels"""
        return len(self.active_channels)
    
    def has_capacity(self) -> bool:
        """Check if we can create more channels"""
        return len(self.active_channels) < self.max_channels
    
    def cleanup_all_channels(self):
        """Delete all tracked channels"""
        self.logger.info("Cleaning up all KA9Q channels")
        ssrcs = list(self.active_channels.keys())
        for ssrc in ssrcs:
            self.delete_channel(ssrc)
    
    def close(self):
        """Close control interface and cleanup channels"""
        self.cleanup_all_channels()
