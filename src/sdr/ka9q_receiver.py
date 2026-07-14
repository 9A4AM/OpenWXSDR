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
#  Provides KA9QReceiver, a UDP multicast client that subscribes to PCM
#  audio streams distributed by a KA9Q Radio server over RTP/multicast.
#  Supports automatic decoder spawning and audio streaming to rs1729 decoders.
#
#  Architecture:
#    KA9Q radiod (multicast) → UDP socket → RTP parser → PCM audio → decoder
#
#  RTP packet format (RFC 3550):
#    0               1               2               3
#    0 1 2 3 4 5 6 7 0 1 2 3 4 5 6 7 0 1 2 3 4 5 6 7 0 1 2 3 4 5 6 7
#   +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
#   |V=2|P|X|  CC   |M|     PT      |       sequence number         |
#   +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
#   |                           timestamp                           |
#   +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
#   |           synchronization source (SSRC) identifier            |
#   +=+=+=+=+=+=+=+=+=+=+=+=+=+=+=+=+=+=+=+=+=+=+=+=+=+=+=+=+=+=+=+=+
#   |            contributing source (CSRC) identifiers             |
#   |                             ....                              |
#   +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
#
#  External dependency: KA9Q Radio (github.com/ka9q/ka9q-radio)
#
# =============================================================================
"""

import socket
import struct
import logging
import threading
import time
import subprocess
import os
import json
from typing import Optional, Dict, List, Callable, Set
from dataclasses import dataclass

# Import KA9Q control interface for dynamic channel management
try:
    from .ka9q_control import KA9QControl
    KA9Q_CONTROL_AVAILABLE = True
except ImportError:
    KA9Q_CONTROL_AVAILABLE = False


@dataclass
class KA9QStream:
    """Represents an active KA9Q audio stream"""
    ssrc: int               # RTP SSRC identifier
    frequency: float        # Hz
    sample_rate: int        # Hz (typically 48000)
    channels: int           # Audio channels (1=mono, 2=stereo)
    last_seq: int           # Last sequence number seen
    last_timestamp: int     # Last RTP timestamp
    packet_count: int       # Total packets received
    last_activity: float    # Unix timestamp of last packet


class KA9QReceiver:
    """Interface to KA9Q radio multicast PCM streams"""
    
    def __init__(self, config: dict, decoder_callback: Optional[Callable] = None):
        self.config = config
        self.decoder_callback = decoder_callback  # Callback to spawn decoders
        self.logger = logging.getLogger('KA9QReceiver')
        self.running = False
        self.sock = None
        self.active_streams: Dict[int, KA9QStream] = {}  # SSRC -> stream info
        self.lock = threading.Lock()
        
        # Audio pipeline: RTP → fsk_demod (demodulate FSK) → decoder --softin
        self.active_decoders: Dict[int, subprocess.Popen] = {}  # SSRC → decoder process
        self.fsk_processes: Dict[int, subprocess.Popen] = {}    # SSRC → fsk_demod process
        
        # Decoder signal quality monitoring (EbNodB tracking)
        self.decoder_spawn_times: Dict[int, float] = {}  # SSRC → spawn timestamp
        self.decoder_ebnodb_values: Dict[int, List[float]] = {}  # SSRC → list of recent EbNodB values
        self.decoder_validation_timeout = 20.0  # Monitor decoders for 20s before auto-cleanup
        self.decoder_min_ebnodb = 6.0  # Minimum EbNodB required for reliable decode
        
        # KA9Q dynamic channel control (optional)
        self.ka9q_control: Optional[KA9QControl] = None
        self.enable_dynamic_channels = False
        self.managed_channels: Set[int] = set()  # Track SSRCs we created
        self.channel_idle_timeout = 60  # Seconds before deleting idle channels
        
        # Spectrum scanning configuration
        ka9q_config = self.config.get('sdr', {}).get('ka9q', {})
        self.scanning_mode = ka9q_config.get('scanning_mode', False)
        self.scan_interval = ka9q_config.get('scan_interval', 10)
        self.scan_frequency_min = ka9q_config.get('scan_frequency_min', 400e6)
        self.scan_frequency_max = ka9q_config.get('scan_frequency_max', 406e6)
        self.detection_threshold = ka9q_config.get('detection_threshold', 5.0)
        self.channel_bandwidth = ka9q_config.get('channel_bandwidth', 40000)
        self.scan_step_size = ka9q_config.get('scan_step_size', 1e6)  # 1 MHz scan steps for better coverage
        
        # Signal detection tracking
        self.detected_signals: Dict[float, float] = {}  # frequency (Hz) -> last_seen (unix timestamp)
        self.signal_detection_timeout = 60  # Remove signals not seen in 60 seconds
        self.confirmed_decode_channels: Set[int] = set()  # SSRCs that have confirmed signals
        self.scan_channels: Set[int] = set()  # SSRCs of active scan channels
        self.channel_frequencies: Dict[int, float] = {}  # SSRC → frequency mapping for created channels
        
    def _decode_frequency_from_ssrc(self, ssrc: int) -> float:
        """
        Decode frequency from SSRC identifier.
        
        radiod/KA9Q embeds frequency information in the SSRC.
        Empirical pattern: SSRC / 100000 ≈ frequency in MHz
        Example: SSRC 0x0269fb21 (40570145) / 100000 = 405.70145 MHz ≈ 405.7 MHz
        
        Returns frequency in Hz, or 0.0 if cannot decode
        """
        try:
            # Method 1: Divide by 100000 to get approximate MHz
            freq_mhz = ssrc / 100000.0
            
            # Sanity check: radiosonde frequencies are typically 400-406 MHz
            if 400.0 <= freq_mhz <= 407.0:
                freq_hz = freq_mhz * 1e6
                self.logger.debug(f"Decoded SSRC {ssrc:08x} ({ssrc}) → {freq_mhz:.3f} MHz")
                return freq_hz
            
            # Method 2: Try decimal string parsing (freq_khz followed by suffix)
            # Format: {freq_khz}XX where XX is mode/suffix
            ssrc_str = f"{ssrc:08d}"  # Zero-padded to 8 digits
            
            # Try different suffix lengths (2-3 digits)
            for suffix_len in [2, 3]:
                if len(ssrc_str) > suffix_len:
                    freq_khz_str = ssrc_str[:-suffix_len]
                    try:
                        freq_khz = int(freq_khz_str)
                        freq_hz = freq_khz * 1000.0
                        freq_mhz = freq_hz / 1e6
                        
                        if 400.0 <= freq_mhz <= 407.0:
                            self.logger.debug(f"Decoded SSRC {ssrc:08x} ({ssrc_str}) → {freq_khz} kHz = {freq_mhz:.3f} MHz")
                            return freq_hz
                    except ValueError:
                        continue
            
            return 0.0
            
        except Exception as e:
            self.logger.debug(f"Failed to decode frequency from SSRC {ssrc:08x}: {e}")
            return 0.0
        
    def initialize(self) -> bool:
        """Initialize KA9Q multicast receiver"""
        try:
            # Get KA9Q config with fallback defaults
            ka9q_config = self.config.get('sdr', {}).get('ka9q', {})
            # Support both multicast_group and multicast-group (YAML allows both)
            multicast_group = ka9q_config.get('multicast_group') or ka9q_config.get('multicast-group', '239.1.2.3')
            port = ka9q_config.get('port', 5004)
            interface = ka9q_config.get('interface', '0.0.0.0')  # Default to any interface
            
            # If interface is a device name (like 'wlp2s0'), resolve to IP
            # For multicast, we can just use 0.0.0.0 to listen on all interfaces
            if interface and not interface.replace('.', '').isdigit():
                self.logger.info(f"Interface '{interface}' is a device name, using 0.0.0.0 for multicast join")
                interface = '0.0.0.0'
            
            self.logger.info(f"KA9Q config: multicast={multicast_group}, port={port}, interface={interface}")
            
            # Create UDP socket
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            
            # Allow multiple sockets to bind to same port (required for multicast)
            try:
                self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except AttributeError:
                pass  # SO_REUSEPORT not available on all platforms
            
            # Increase receive buffer size for high data rate
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 2*1024*1024)  # 2MB
            
            # Bind to multicast group address (not 0.0.0.0) for proper multicast reception
            self.sock.bind((multicast_group, port))
            
            # Join multicast group on specified interface (or all interfaces if 0.0.0.0)
            mreq = struct.pack("4s4s", socket.inet_aton(multicast_group), socket.inet_aton(interface))
            self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
            
            self.logger.info(f"KA9Q receiver initialized: {multicast_group}:{port} on interface {interface}")
            
            # Initialize dynamic channel control (optional)
            if KA9Q_CONTROL_AVAILABLE:
                self.enable_dynamic_channels = ka9q_config.get('enable_dynamic_channels', False)
                
                if self.enable_dynamic_channels:
                    radio_hostname = ka9q_config.get('radio_hostname', 'sonde.local')
                    self.ka9q_control = KA9QControl(
                        radio_hostname=radio_hostname,
                        max_channels=ka9q_config.get('max_channels', 10)
                    )
                    
                    if self.ka9q_control.initialize():
                        self.logger.info(f"KA9Q dynamic channel control enabled (max {self.ka9q_control.max_channels} channels)")
                        self.logger.info(f"  Radio: {radio_hostname}")
                        self.logger.info(f"  Tune binary: {self.ka9q_control.tune_path}")
                        # Note: Cleanup monitor will start when start_receiving() is called
                    else:
                        self.logger.warning("Failed to initialize KA9Q control interface, dynamic channels disabled")
                        self.enable_dynamic_channels = False
                        self.ka9q_control = None
                else:
                    self.logger.info("KA9Q dynamic channel control disabled (using fixed channels)")
            else:
                self.logger.debug("KA9Q control module not available")
            
            return True
            
        except Exception as e:
            self.logger.error(f"Failed to initialize KA9Q receiver: {e}", exc_info=True)
            return False
    
    def close(self):
        """Close KA9Q receiver"""
        self.stop_receiving()
        
        # Cleanup dynamic channels
        if self.ka9q_control:
            self.logger.info("Cleaning up all managed KA9Q channels")
            self.ka9q_control.cleanup_all_channels()
            self.ka9q_control.close()
            self.ka9q_control = None
        
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
        self.recv_thread = threading.Thread(target=self._receive_loop, daemon=True, name="KA9Q-Receiver")
        self.recv_thread.start()
        
        # Start channel cleanup monitor if dynamic channels enabled
        if self.enable_dynamic_channels and self.ka9q_control:
            self.cleanup_thread = threading.Thread(
                target=self._channel_cleanup_monitor,
                daemon=True,
                name="KA9Q-Cleanup"
            )
            self.cleanup_thread.start()
            # Note: cleanup monitor logs its own startup message
        
        # Start spectrum scanning thread if scanning mode enabled
        if self.scanning_mode and self.enable_dynamic_channels and self.ka9q_control:
            self.scanning_thread = threading.Thread(
                target=self._spectrum_scanning_loop,
                daemon=True,
                name="KA9Q-Scanner"
            )
            self.scanning_thread.start()
            self.logger.info(f"KA9Q spectrum scanning enabled: {self.scan_frequency_min/1e6:.1f}-{self.scan_frequency_max/1e6:.1f} MHz")
        elif self.scanning_mode:
            self.logger.warning("KA9Q scanning mode enabled but dynamic channels disabled - scanning will not work")
        
        self.logger.info("KA9Q receiver started")
    
    def stop_receiving(self):
        """Stop receiving KA9Q data"""
        self.running = False
        if hasattr(self, 'recv_thread'):
            self.recv_thread.join(timeout=5)
        
        # Stop all active decoders and fsk_demod processes
        with self.lock:
            for ssrc, decoder in list(self.active_decoders.items()):
                try:
                    decoder.terminate()
                    decoder.wait(timeout=2)
                except:
                    decoder.kill()
            self.active_decoders.clear()
            
            # Stop fsk_demod processes
            if hasattr(self, 'fsk_processes'):
                for ssrc, fsk_proc in list(self.fsk_processes.items()):
                    try:
                        fsk_proc.terminate()
                        fsk_proc.wait(timeout=2)
                    except:
                        fsk_proc.kill()
                self.fsk_processes.clear()
        
        self.logger.info("KA9Q receiver stopped")
    
    def _receive_loop(self):
        """Background receive loop - processes RTP packets"""
        self.logger.info("KA9Q receive loop started")
        packet_count = 0
        last_status_time = time.time()
        
        while self.running:
            try:
                # Receive RTP packet with timeout
                self.sock.settimeout(1.0)
                data, addr = self.sock.recvfrom(65536)
                packet_count += 1
                
                # Log first packet as diagnostic
                if packet_count == 1:
                    self.logger.info(f"First RTP packet received: {len(data)} bytes from {addr}")
                
                # Parse and process RTP packet
                self._parse_rtp_packet(data)
                
                # Log status every 10 seconds
                current_time = time.time()
                if current_time - last_status_time >= 10:
                    with self.lock:
                        # Show detailed status for each stream
                        for ssrc, stream in self.active_streams.items():
                            decoder_status = "with decoder" if ssrc in self.active_decoders else "NO DECODER"
                            self.logger.info(f"Stream {ssrc:08x}: {stream.packet_count} packets, {decoder_status}")
                        self.logger.info(f"KA9Q status: {packet_count} total packets, {len(self.active_streams)} stream(s), {len(self.active_decoders)} decoder(s)")
                    last_status_time = current_time
                
            except socket.timeout:
                # Log periodic timeout status
                current_time = time.time()
                if current_time - last_status_time >= 10:
                    self.logger.debug(f"KA9Q: No packets received in last 10 seconds (total: {packet_count})")
                    last_status_time = current_time
                continue
            except Exception as e:
                self.logger.error(f"Error in KA9Q receive loop: {e}", exc_info=True)
                time.sleep(1)
        
        self.logger.info("KA9Q receive loop stopped")
    
    def _parse_rtp_packet(self, data: bytes):
        """
        Parse RTP packet and extract PCM audio samples.
        
        RTP header format (minimum 12 bytes):
        - V(2), P(1), X(1), CC(4): Version, padding, extension, CSRC count
        - M(1), PT(7): Marker, payload type
        - Sequence number (16 bits)
        - Timestamp (32 bits)
        - SSRC (32 bits)
        """
        try:
            if len(data) < 12:
                self.logger.debug(f"Packet too short: {len(data)} bytes")
                return
            
            # Parse RTP header
            byte0, byte1, seq, timestamp, ssrc = struct.unpack('!BBHII', data[0:12])
            
            version = (byte0 >> 6) & 0x03
            padding = (byte0 >> 5) & 0x01
            extension = (byte0 >> 4) & 0x01
            csrc_count = byte0 & 0x0F
            marker = (byte1 >> 7) & 0x01
            payload_type = byte1 & 0x7F
            
            # Validate RTP version
            if version != 2:
                self.logger.debug(f"Invalid RTP version: {version}")
                return
            
            # Log first packet details for each SSRC
            with self.lock:
                if ssrc not in self.active_streams:
                    self.logger.info(f"New RTP stream: SSRC={ssrc:08x}, PT={payload_type}, seq={seq}, ts={timestamp}, size={len(data)}")
            
            # Calculate header length
            header_len = 12 + (csrc_count * 4)
            
            # Skip extension header if present
            if extension and len(data) >= header_len + 4:
                ext_header = struct.unpack('!HH', data[header_len:header_len+4])
                ext_len = ext_header[1] * 4
                header_len += 4 + ext_len
            
            # Extract payload (PCM audio samples)
            if len(data) <= header_len:
                self.logger.debug(f"No payload: header_len={header_len}, total={len(data)}")
                return
            
            payload = data[header_len:]
            
            # Remove padding if present
            if padding and len(payload) > 0:
                pad_len = payload[-1]
                payload = payload[:-pad_len]
            
            # Update or create stream info
            spawn_decoder = False
            is_scan_channel = False
            with self.lock:
                if ssrc not in self.active_streams:
                    # New stream detected
                    # Look up frequency from channel mapping (if we created this channel)
                    stream_freq = self.channel_frequencies.get(ssrc, 0.0)
                    
                    # If no mapping exists, try to decode frequency from SSRC
                    # radiod uses format: {freq_khz}01 for regular channels
                    # Example: 405.700 MHz = 405700 kHz → SSRC 0x0269fb21 (40570001 decimal)
                    if stream_freq == 0.0:
                        stream_freq = self._decode_frequency_from_ssrc(ssrc)
                        if stream_freq > 0:
                            self.channel_frequencies[ssrc] = stream_freq  # Cache it
                    
                    stream = KA9QStream(
                        ssrc=ssrc,
                        frequency=stream_freq,  # Use mapped frequency, or 0.0 if unknown
                        sample_rate=48000,  # Typical KA9Q output
                        channels=1,  # Mono for radiosonde
                        last_seq=seq,
                        last_timestamp=timestamp,
                        packet_count=1,
                        last_activity=time.time()
                    )
                    self.active_streams[ssrc] = stream
                    freq_info = f" at {stream_freq/1e6:.4f} MHz" if stream_freq > 0 else ""
                    self.logger.info(f"New KA9Q stream detected: SSRC={ssrc:08x}{freq_info}, PT={payload_type}, payload_size={len(payload)} bytes")
                    
                    # Check if this is a scan channel or decode channel
                    is_scan_channel = ssrc in self.scan_channels
                    
                    # Only spawn decoder for decode channels (not scan channels)
                    # In scanning mode, decoders are spawned by _spectrum_scanning_loop
                    # when signals are confirmed
                    if not self.scanning_mode or ssrc in self.confirmed_decode_channels:
                        spawn_decoder = True
                        if self.scanning_mode:
                            self.logger.info(f"SSRC {ssrc:08x} is a confirmed decode channel - spawning decoder")
                else:
                    # Update existing stream
                    stream = self.active_streams[ssrc]
                    stream.last_seq = seq
                    stream.last_timestamp = timestamp
                    stream.packet_count += 1
                    stream.last_activity = time.time()
            
            # Spawn decoder OUTSIDE the lock to avoid blocking API calls
            # Only for decode channels with confirmed signals (not scan channels)
            if spawn_decoder and not is_scan_channel:
                self._spawn_decoder_for_stream(ssrc, self.active_streams[ssrc])
            
            # In scanning mode: promote pre-existing active channels to decode status
            # These might be fixed channels from KA9Q config that have real signals
            # Only check once when packet count reaches threshold
            if (self.scanning_mode and not spawn_decoder and not is_scan_channel and 
                ssrc not in self.active_decoders and ssrc not in self.scan_channels):
                stream = self.active_streams.get(ssrc)
                if stream and stream.packet_count == 100:  # Check exactly once at 100 packets
                    # Auto-promote to decode channel
                    with self.lock:
                        if ssrc not in self.confirmed_decode_channels:
                            self.confirmed_decode_channels.add(ssrc)
                            self.logger.info(f"Auto-promoting active stream SSRC {ssrc:08x} to decode channel ({stream.packet_count} packets)")
                    # Spawn decoder now
                    self._spawn_decoder_for_stream(ssrc, stream)
            
            # Send I/Q data to decoder if active
            if ssrc in self.active_decoders:
                # Send to fsk_demod process (which feeds decoder)
                fsk_proc = getattr(self, 'fsk_processes', {}).get(ssrc)
                decoder = self.active_decoders[ssrc]
                
                if not fsk_proc:
                    self.logger.error(f"SSRC {ssrc:08x} in active_decoders but fsk_proc is None! (fsk_processes has {len(getattr(self, 'fsk_processes', {}))} entries)")
                elif fsk_proc.poll() is not None:
                    self.logger.warning(f"fsk_demod process for SSRC {ssrc:08x} has exited with code {fsk_proc.poll()}")
                else:
                    try:
                        fsk_proc.stdin.write(payload)
                        fsk_proc.stdin.flush()
                        
                        # Log first write for debugging
                        stream = self.active_streams.get(ssrc)
                        if stream and stream.packet_count <= 3:  # Log first 3 writes
                            self.logger.info(f"I/Q write #{stream.packet_count} to fsk_demod SSRC={ssrc:08x}: {len(payload)} bytes")
                            
                    except BrokenPipeError:
                        self.logger.warning(f"fsk_demod pipe broken for SSRC={ssrc:08x}")
                        del self.active_decoders[ssrc]
                        if hasattr(self, 'fsk_processes') and ssrc in self.fsk_processes:
                            del self.fsk_processes[ssrc]
                        # Cleanup tracking data
                        self.decoder_spawn_times.pop(ssrc, None)
                        self.decoder_ebnodb_values.pop(ssrc, None)
                    except Exception as e:
                        self.logger.error(f"Error writing to fsk_demod SSRC={ssrc:08x}: {e}", exc_info=True)
                        del self.active_decoders[ssrc]
                        if hasattr(self, 'fsk_processes') and ssrc in self.fsk_processes:
                            del self.fsk_processes[ssrc]
                        # Cleanup tracking data
                        self.decoder_spawn_times.pop(ssrc, None)
                        self.decoder_ebnodb_values.pop(ssrc, None)
            
        except Exception as e:
            self.logger.debug(f"Error parsing RTP packet: {e}")
    
    def _spawn_decoder_for_stream(self, ssrc: int, stream: KA9QStream):
        """Spawn fsk_demod + rs1729 decoder pipeline for a KA9Q I/Q stream (softin method)"""
        try:
            # Determine decoder path
            rs1729_path = self.config.get('decoders', {}).get('rs1729_path', './decoders/rs1729')
            decoder_path = os.path.join(rs1729_path, 'rs41mod')
            fsk_demod_path = os.path.join(rs1729_path, 'fsk_demod')
            
            # Pipeline: KA9Q I/Q (cs16) → fsk_demod → rs41mod --softin

            # Build fsk_demod command (from auto_rx)
            # --cs16: complex signed 16-bit input (interleaved I/Q)
            # -b -5000, -u 5000: frequency bounds (±5kHz from center)
            # -s: Schmitt trigger
            # --mask 4800: mask frequency (related to baud rate)
            # --stats=5: statistics every 5 seconds
            # 2: number of channels (I/Q = 2)
            # 48000: sample rate
            # 4800: baud rate (RS41 uses 4800 baud)
            fsk_demod_cmd = [
                fsk_demod_path,
                '--cs16',
                '-b', '-5000',
                '-u', '5000',
                '-s',
                '--mask', '4800',
                '--stats=5',
                '2',  # I/Q = 2 channels
                str(stream.sample_rate),  # 48000
                '4800',  # Baud rate for RS41
                '-',  # stdin
                '-'   # stdout
            ]
            
            # Build decoder command (using softin method)
            # --softin: read soft-decision bits from fsk_demod
            # -i: invert signal
            decoder_cmd = [
                decoder_path,
                '--ptu2',
                '--json',
                '--jsnsubfrm1',
                '--softin',
                '-i'
            ]
            
            self.logger.info(f"Spawning fsk_demod decoder pipeline for SSRC={ssrc:08x}")
            self.logger.info(f"  fsk_demod: {' '.join(fsk_demod_cmd)}")
            self.logger.info(f"  Decoder: {' '.join(decoder_cmd)}")
            
            # Start fsk_demod process
            fsk_proc = subprocess.Popen(
                fsk_demod_cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0
            )
            
            # Start decoder process (reads from fsk_demod stdout)
            decoder_proc = subprocess.Popen(
                decoder_cmd,
                stdin=fsk_proc.stdout,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0
            )
            
            # Close fsk_demod stdout in parent so decoder gets EOF when fsk_demod exits
            fsk_proc.stdout.close()
            
            # Store both processes
            with self.lock:
                self.active_decoders[ssrc] = decoder_proc
                # Store fsk_demod process too for cleanup
                if not hasattr(self, 'fsk_processes'):
                    self.fsk_processes = {}
                self.fsk_processes[ssrc] = fsk_proc
                
                # Initialize EbNodB tracking for this decoder
                self.decoder_spawn_times[ssrc] = time.time()
                self.decoder_ebnodb_values[ssrc] = []
            
            self.logger.info(f"Decoder pipeline spawned for SSRC={ssrc:08x}, fsk_demod PID={fsk_proc.pid}, decoder PID={decoder_proc.pid}")
            
            # Give pipeline a moment to start
            time.sleep(0.1)
            
            # Check if either process exited immediately
            fsk_exit = fsk_proc.poll()
            decoder_exit = decoder_proc.poll()
            
            if fsk_exit is not None:
                self.logger.error(f"fsk_demod exited immediately for SSRC={ssrc:08x}, exit code={fsk_exit}")
                try:
                    stderr_output = fsk_proc.stderr.read(1024).decode('utf-8', errors='replace')
                    if stderr_output:
                        self.logger.error(f"fsk_demod stderr: {stderr_output}")
                except:
                    pass
                # Cleanup
                with self.lock:
                    if ssrc in self.active_decoders:
                        del self.active_decoders[ssrc]
                    if hasattr(self, 'fsk_processes') and ssrc in self.fsk_processes:
                        del self.fsk_processes[ssrc]
                    # Cleanup tracking data
                    self.decoder_spawn_times.pop(ssrc, None)
                    self.decoder_ebnodb_values.pop(ssrc, None)
                return
                
            if decoder_exit is not None:
                self.logger.error(f"Decoder exited immediately for SSRC={ssrc:08x}, exit code={decoder_exit}")
                try:
                    stderr_output = decoder_proc.stderr.read(1024).decode('utf-8', errors='replace')
                    if stderr_output:
                        self.logger.error(f"Decoder stderr: {stderr_output}")
                except:
                    pass
                # Cleanup
                fsk_proc.terminate()
                with self.lock:
                    if ssrc in self.active_decoders:
                        del self.active_decoders[ssrc]
                    if hasattr(self, 'fsk_processes') and ssrc in self.fsk_processes:
                        del self.fsk_processes[ssrc]
                    # Cleanup tracking data
                    self.decoder_spawn_times.pop(ssrc, None)
                    self.decoder_ebnodb_values.pop(ssrc, None)
                return
            
            # Start thread to read decoder output
            threading.Thread(
                target=self._read_decoder_output,
                args=(ssrc, decoder_proc),
                daemon=True,
                name=f"KA9Q-Decoder-{ssrc:08x}"
            ).start()
            
            # Start thread to read decoder stderr
            threading.Thread(
                target=self._read_decoder_stderr,
                args=(ssrc, decoder_proc),
                daemon=True,
                name=f"KA9Q-Decoder-stderr-{ssrc:08x}"
            ).start()
            
            # Start thread to read fsk_demod stderr
            threading.Thread(
                target=self._read_fsk_stderr,
                args=(ssrc, fsk_proc),
                daemon=True,
                name=f"KA9Q-fsk-{ssrc:08x}"
            ).start()
            
        except Exception as e:
            self.logger.error(f"Failed to spawn decoder for SSRC={ssrc:08x}: {e}", exc_info=True)
    
    def _read_decoder_output(self, ssrc: int, decoder: subprocess.Popen):
        """Read and process JSON output from decoder"""
        self.logger.debug(f"Decoder output reader started for SSRC={ssrc:08x}")
        
        try:
            for line in decoder.stdout:
                try:
                    line_str = line.decode('utf-8', errors='replace').strip()
                    if line_str.startswith('{'):
                        # JSON telemetry frame
                        self.logger.info(f"KA9Q decoder output: {line_str}")
                        
                        # TODO: Parse JSON and call decoder_callback
                        # if self.decoder_callback:
                        #     self.decoder_callback(json.loads(line_str))
                        
                except Exception as e:
                    self.logger.debug(f"Error processing decoder output: {e}")
        
        except Exception as e:
            self.logger.error(f"Error reading decoder output for SSRC={ssrc:08x}: {e}")
        
        finally:
            self.logger.debug(f"Decoder output reader stopped for SSRC={ssrc:08x}")
    
    def _read_decoder_stderr(self, ssrc: int, decoder: subprocess.Popen):
        """Read and log decoder stderr for diagnostics"""
        self.logger.info(f"Decoder stderr reader started for SSRC={ssrc:08x}")
        
        try:
            for line in decoder.stderr:
                try:
                    line_str = line.decode('utf-8', errors='replace').strip()
                    if line_str:
                        # Log at INFO level so we can see it in production
                        self.logger.info(f"Decoder stderr [{ssrc:08x}]: {line_str}")
                except Exception as e:
                    self.logger.debug(f"Error processing decoder stderr: {e}")
        
        except Exception as e:
            self.logger.error(f"Error reading decoder stderr for SSRC={ssrc:08x}: {e}")
        
        finally:
            self.logger.info(f"Decoder stderr reader stopped for SSRC={ssrc:08x}")
    
    def _read_fsk_stderr(self, ssrc: int, fsk_proc: subprocess.Popen):
        """Read and log fsk_demod stderr for diagnostics and extract EbNodB values"""
        self.logger.info(f"fsk_demod stderr reader started for SSRC={ssrc:08x}")
        
        try:
            for line in fsk_proc.stderr:
                try:
                    line_str = line.decode('utf-8', errors='replace').strip()
                    if line_str:
                        # Log fsk_demod output (stats, warnings, errors)
                        self.logger.info(f"fsk_demod [{ssrc:08x}]: {line_str}")
                        
                        # Parse EbNodB values for signal quality monitoring
                        # fsk_demod outputs JSON format: {"secs": ..., "EbNodB": 3.4, ...}
                        if "EbNodB" in line_str and line_str.startswith("{"):
                            try:
                                # Parse JSON to extract EbNodB value
                                stats = json.loads(line_str)
                                if "EbNodB" in stats:
                                    ebnodb = float(stats["EbNodB"])
                                    with self.lock:
                                        if ssrc in self.decoder_ebnodb_values:
                                            self.decoder_ebnodb_values[ssrc].append(ebnodb)
                                            # Keep only the last 10 values (rolling window)
                                            if len(self.decoder_ebnodb_values[ssrc]) > 10:
                                                self.decoder_ebnodb_values[ssrc].pop(0)
                                            # Log signal quality for debugging
                                            avg_ebnodb = sum(self.decoder_ebnodb_values[ssrc]) / len(self.decoder_ebnodb_values[ssrc])
                                            self.logger.debug(f"SSRC {ssrc:08x}: EbNodB={ebnodb:.1f} dB (avg last {len(self.decoder_ebnodb_values[ssrc])} samples: {avg_ebnodb:.1f} dB)")
                            except Exception as e:
                                self.logger.debug(f"Failed to parse EbNodB JSON from: {line_str[:100]}: {e}")
                                
                except Exception as e:
                    self.logger.debug(f"Error processing fsk_demod stderr: {e}")
        
        except Exception as e:
            self.logger.error(f"Error reading fsk_demod stderr for SSRC={ssrc:08x}: {e}")
        
        finally:
            self.logger.info(f"fsk_demod stderr reader stopped for SSRC={ssrc:08x}")
    
    def _stop_decoder(self, ssrc: int):
        """Stop decoder and fsk_demod for a given SSRC and cleanup tracking data"""
        self.logger.info(f"Stopping decoder for SSRC {ssrc:08x}")
        
        with self.lock:
            # Stop decoder process
            if ssrc in self.active_decoders:
                try:
                    decoder = self.active_decoders[ssrc]
                    decoder.terminate()
                    decoder.wait(timeout=2)
                except Exception as e:
                    self.logger.warning(f"Failed to terminate decoder gracefully: {e}")
                    try:
                        decoder.kill()
                    except:
                        pass
                del self.active_decoders[ssrc]
            
            # Stop fsk_demod process
            if ssrc in self.fsk_processes:
                try:
                    fsk_proc = self.fsk_processes[ssrc]
                    fsk_proc.terminate()
                    fsk_proc.wait(timeout=2)
                except Exception as e:
                    self.logger.warning(f"Failed to terminate fsk_demod gracefully: {e}")
                    try:
                        fsk_proc.kill()
                    except:
                        pass
                del self.fsk_processes[ssrc]
            
            # Cleanup tracking data
            self.decoder_spawn_times.pop(ssrc, None)
            self.decoder_ebnodb_values.pop(ssrc, None)
        
        self.logger.info(f"Decoder stopped for SSRC {ssrc:08x}")
    
    def _channel_cleanup_monitor(self):
        """Background thread to monitor and cleanup idle channels"""
        self.logger.info("KA9Q channel cleanup monitor started")
        
        while self.running:
            try:
                time.sleep(10)  # Check every 10 seconds
                
                current_time = time.time()
                decoders_to_kill = []
                
                # First, validate decoder signal quality (EbNodB)
                with self.lock:
                    for ssrc in list(self.active_decoders.keys()):
                        # Check if decoder has been running long enough for validation
                        spawn_time = self.decoder_spawn_times.get(ssrc)
                        if spawn_time is None:
                            continue
                            
                        elapsed_time = current_time - spawn_time
                        
                        # After validation timeout, check if EbNodB is consistently low
                        if elapsed_time > self.decoder_validation_timeout:
                            ebnodb_values = self.decoder_ebnodb_values.get(ssrc, [])
                            
                            # Need at least 3 samples for reliable assessment
                            if len(ebnodb_values) >= 3:
                                avg_ebnodb = sum(ebnodb_values) / len(ebnodb_values)
                                max_ebnodb = max(ebnodb_values)
                                
                                # Kill decoder if signal quality is consistently poor
                                if avg_ebnodb < self.decoder_min_ebnodb and max_ebnodb < self.decoder_min_ebnodb + 2.0:
                                    stream = self.active_streams.get(ssrc)
                                    freq_str = f"{stream.frequency/1e6:.4f} MHz" if stream else "unknown freq"
                                    self.logger.warning(
                                        f"Decoder SSRC {ssrc:08x} at {freq_str} has consistently low signal quality "
                                        f"(avg EbNodB: {avg_ebnodb:.1f} dB, max: {max_ebnodb:.1f} dB, threshold: {self.decoder_min_ebnodb:.1f} dB). "
                                        f"Auto-stopping decoder (likely noise, not radiosonde)."
                                    )
                                    decoders_to_kill.append(ssrc)
                                    
                                    # Remove from confirmed channels so it can be re-scanned later
                                    self.confirmed_decode_channels.discard(ssrc)
                                    
                            elif elapsed_time > self.decoder_validation_timeout * 2:
                                # No EbNodB stats after 40 seconds - decoder might be stuck/broken
                                self.logger.warning(
                                    f"Decoder SSRC {ssrc:08x} has been running for {elapsed_time:.0f}s "
                                    f"but no EbNodB stats received. Auto-stopping (decoder may be broken)."
                                )
                                decoders_to_kill.append(ssrc)
                                self.confirmed_decode_channels.discard(ssrc)
                
                # Kill weak decoders and delete their channels (outside lock to avoid blocking)
                for ssrc in decoders_to_kill:
                    self._stop_decoder(ssrc)
                    
                    # Delete the decode channel from radiod if dynamic channels enabled
                    if self.ka9q_control and self.enable_dynamic_channels:
                        stream = self.active_streams.get(ssrc)
                        freq_str = f"{stream.frequency/1e6:.4f} MHz" if stream else "unknown freq"
                        self.logger.info(f"Deleting weak decode channel SSRC {ssrc:08x} at {freq_str}")
                        
                        if self.ka9q_control.delete_channel(ssrc):
                            with self.lock:
                                self.managed_channels.discard(ssrc)
                                self.channel_frequencies.pop(ssrc, None)  # Clean up frequency mapping
                            self.logger.info(f"✓ Decode channel {ssrc:08x} deleted successfully")
                        else:
                            self.logger.warning(f"Failed to delete decode channel {ssrc:08x}")
                
                if not self.ka9q_control or not self.enable_dynamic_channels:
                    continue
                
                channels_to_delete = []
                
                with self.lock:
                    # Find channels with no activity
                    for ssrc in list(self.managed_channels):
                        stream = self.active_streams.get(ssrc)
                        
                        # Delete if:
                        # 1. No stream info (channel never received data)
                        # 2. No recent activity (idle timeout)
                        # 3. Decoder has exited
                        if stream is None:
                            # No stream info yet, give it some time
                            continue
                        
                        idle_time = current_time - stream.last_activity
                        decoder_active = ssrc in self.active_decoders
                        
                        if idle_time > self.channel_idle_timeout and not decoder_active:
                            channels_to_delete.append((ssrc, stream.frequency))
                            self.managed_channels.discard(ssrc)
                
                # Delete idle channels (outside lock to avoid blocking)
                for ssrc, frequency in channels_to_delete:
                    self.logger.info(f"Deleting idle channel: SSRC={ssrc:08x}, frequency={frequency/1e6:.4f} MHz")
                    
                    if self.ka9q_control.delete_channel(ssrc):
                        self.logger.info(f"✓ Channel {ssrc:08x} deleted successfully")
                        with self.lock:
                            self.channel_frequencies.pop(ssrc, None)  # Clean up frequency mapping
                    else:
                        self.logger.warning(f"Failed to delete channel {ssrc:08x}")
                
                # Log status periodically
                if len(self.managed_channels) > 0:
                    self.logger.debug(f"Managed channels: {len(self.managed_channels)}/{self.ka9q_control.max_channels}")
                
            except Exception as e:
                self.logger.error(f"Error in channel cleanup monitor: {e}", exc_info=True)
                time.sleep(5)
        
        self.logger.info("KA9Q channel cleanup monitor stopped")
    
    def _spectrum_scanning_loop(self):
        """Background thread for spectrum scanning and signal detection"""
        self.logger.info("KA9Q spectrum scanning started")
        self.logger.info(f"Scan range: {self.scan_frequency_min/1e6:.1f} - {self.scan_frequency_max/1e6:.1f} MHz")
        self.logger.info(f"Detection threshold: {self.detection_threshold} dB, Interval: {self.scan_interval}s")
        
        # Give system time to start up and wait for radiod to send RTP streams
        # radiod with many pre-configured channels (e.g., 10 kHz steps) needs time to start all streams
        self.logger.info("Waiting up to 30 seconds for radiod channels to start streaming...")
        
        # Check multiple times (every 5 seconds for 30 seconds) to detect radiod channels
        use_radiod_channels = False
        for check_attempt in range(6):  # 6 attempts * 5 seconds = 30 seconds max
            time.sleep(5)
            
            pre_existing_streams = 0
            pre_existing_with_freq = 0
            with self.lock:
                pre_existing_streams = len(self.active_streams)
                # Count streams with valid radiosonde frequencies (400-406 MHz)
                for ssrc, stream in self.active_streams.items():
                    if 400e6 <= stream.frequency <= 406e6:
                        pre_existing_with_freq += 1
            
            self.logger.info(f"Check {check_attempt+1}/6: Found {pre_existing_streams} streams ({pre_existing_with_freq} in 400-406 MHz band)")
            
            # If we have 50+ streams in radiosonde band, radiod has fine-grained channels
            if pre_existing_with_freq >= 50:
                use_radiod_channels = True
                self.logger.info(f"✓ Detected radiod's fine-grained channels after {(check_attempt+1)*5} seconds")
                break
            
            # If we have at least some streams, give radiod more time
            if pre_existing_streams > 0 and check_attempt < 5:
                continue
            
            # If after 30 seconds we still have <50 streams, radiod probably doesn't have fine channels
            if check_attempt == 5:
                self.logger.info(f"After 30 seconds: {pre_existing_streams} streams found (only {pre_existing_with_freq} in radiosonde band)")
                break
        
        if use_radiod_channels:
            self.logger.info(f"✓ Using radiod's pre-configured channels ({pre_existing_with_freq} channels in 400-406 MHz)")
            self.logger.info("Mode: Monitor all active streams, auto-spawn decoders, validate via EbNodB")
        else:
            self.logger.info(f"✓ Will create scan channels dynamically (radiod has {pre_existing_streams} channels, need 50+ for auto-detect)")
        
        # Check for pre-existing active channels and promote them to decode status
        self.logger.info("Checking for pre-existing active channels...")
        with self.lock:
            for ssrc, stream in list(self.active_streams.items()):
                # For radiod channels, use lower threshold (100 packets ~2 seconds)
                # For scan channels, use higher threshold (500 packets)
                min_packets = 100 if use_radiod_channels else 50
                if stream.packet_count > min_packets and ssrc not in self.confirmed_decode_channels:
                    self.confirmed_decode_channels.add(ssrc)
                    freq_info = f" at {stream.frequency/1e6:.4f} MHz" if stream.frequency > 0 else ""
                    self.logger.info(f"Found pre-existing active channel: SSRC {ssrc:08x}{freq_info} ({stream.packet_count} packets) - will spawn decoder")
        
        while self.running:
            try:
                # If using radiod's pre-configured channels, skip scan channel creation
                if use_radiod_channels:
                    # Monitor existing streams and auto-spawn decoders
                    self.logger.info("Monitoring radiod channels for new activity...")
                    
                    time.sleep(self.scan_interval)
                    
                    # Check for new active streams
                    new_active_streams = []
                    with self.lock:
                        for ssrc, stream in list(self.active_streams.items()):
                            # Skip if already has decoder or in confirmed decode channels
                            if ssrc in self.active_decoders or ssrc in self.confirmed_decode_channels:
                                continue
                            
                            # For radiod channels, use lower threshold to be more responsive
                            # Check if stream is active (200+ packets = ~4 seconds of data)
                            if stream.packet_count >= 200:
                                age = time.time() - stream.last_activity
                                if age < 10.0:  # Active in last 10 seconds
                                    new_active_streams.append((ssrc, stream.frequency, stream.packet_count))
                                    self.confirmed_decode_channels.add(ssrc)
                    
                    if new_active_streams:
                        self.logger.info(f"Detected {len(new_active_streams)} new active stream(s) from radiod")
                        for ssrc, freq, pkt_count in new_active_streams:
                            freq_str = f"{freq/1e6:.4f} MHz" if freq > 0 else "unknown freq"
                            self.logger.info(f"  SSRC {ssrc:08x} at {freq_str}: {pkt_count} packets - decoder will spawn on next RTP packet")
                    
                    # Continue monitoring loop
                    continue
                
                # Otherwise, use dynamic scan channel creation (original behavior)
                # Create scan channels across frequency range
                scan_frequencies = []
                freq = self.scan_frequency_min
                while freq <= self.scan_frequency_max:
                    scan_frequencies.append(freq)
                    freq += self.scan_step_size
                
                self.logger.info(f"Creating {len(scan_frequencies)} scan channels...")
                scan_channel_map = {}  # SSRC -> frequency mapping
                
                # Create scan channels
                for freq in scan_frequencies:
                    if not self.running:
                        break
                    
                    if not self.ka9q_control.has_capacity():
                        self.logger.warning(f"Max channels reached, skipping remaining scan channels")
                        break
                    
                    # Generate scan channel name
                    channel_name = f"scan-{freq/1e6:.3f}"
                    
                    # Create wide-bandwidth scan channel
                    if self.ka9q_control.create_channel(
                        name=channel_name,
                        frequency=freq,
                        mode='iq',
                        samprate=48000,
                        scan=True,
                        channel_filter=1000000  # 1 MHz bandwidth to ensure coverage
                    ):
                        ssrc = self.ka9q_control._generate_ssrc(freq, scan=True)
                        scan_channel_map[ssrc] = freq
                        with self.lock:
                            self.scan_channels.add(ssrc)
                            self.managed_channels.add(ssrc)
                            self.channel_frequencies[ssrc] = freq  # Store SSRC → frequency mapping
                        self.logger.info(f"Created scan channel {channel_name} at {freq/1e6:.3f} MHz (SSRC={ssrc:08x}, BW=±500 kHz)")
                    else:
                        self.logger.warning(f"Failed to create scan channel at {freq/1e6:.3f} MHz")
                    
                    time.sleep(0.5)  # Stagger channel creation
                
                # Wait longer for scan channels to start sending data
                self.logger.info(f"Scan channels active, waiting 5s for RTP streams to start...")
                time.sleep(5)
                
                # Log scan channel status
                with self.lock:
                    scan_streams = [
                        (ssrc, self.active_streams.get(ssrc)) 
                        for ssrc in self.scan_channels
                    ]
                
                self.logger.info(f"Scan channel status:")
                for ssrc, stream in scan_streams:
                    if stream:
                        freq = scan_channel_map.get(ssrc, 0)
                        self.logger.info(f"  SSRC {ssrc:08x} ({freq/1e6:.3f} MHz): {stream.packet_count} packets received")
                    else:
                        freq = scan_channel_map.get(ssrc, 0)
                        self.logger.info(f"  SSRC {ssrc:08x} ({freq/1e6:.3f} MHz): NO RTP STREAM (channel might not be active)")
                
                # Continue monitoring for remaining scan interval
                remaining_time = max(1, self.scan_interval - 5)
                self.logger.info(f"Monitoring scan channels for {remaining_time}s...")
                time.sleep(remaining_time)
                
                # Analyze received data for signals
                self.logger.info("Analyzing spectrum for radiosonde signals...")
                detected_signals = self._analyze_spectrum_for_signals(scan_channel_map)
                
                # CRITICAL: Delete scan channels BEFORE creating decode channels
                # to free up channel slots (max 8 channels, need slots for decode channels)
                self.logger.info("Cleaning up scan channels to free channel slots...")
                with self.lock:
                    scan_ssrcs = list(self.scan_channels)
                
                for ssrc in scan_ssrcs:
                    self.ka9q_control.delete_channel(ssrc)
                    with self.lock:
                        self.scan_channels.discard(ssrc)
                        self.managed_channels.discard(ssrc)
                        self.channel_frequencies.pop(ssrc, None)
                
                self.logger.info(f"Deleted {len(scan_ssrcs)} scan channels, freeing slots for decode channels")
                
                if detected_signals:
                    self.logger.info(f"Detected {len(detected_signals)} potential radiosonde signal(s)")
                    self.logger.info(f"Creating decode channels (max {self.ka9q_control.max_channels} channels available)...")
                    
                    # Sort by signal strength (descending) to prioritize strongest channels
                    # Strength values are based on scan channel packet counts, so higher = more likely radiosonde
                    sorted_signals = sorted(detected_signals.items(), key=lambda x: x[1], reverse=True)
                    
                    # Create decode channels for detected signals
                    created_count = 0
                    for freq, strength in sorted_signals:
                        if not self.running:
                            break
                        
                        # Check if we already have a decode channel for this frequency
                        decode_ssrc = self.ka9q_control._generate_ssrc(freq, scan=False)
                        if decode_ssrc in self.active_streams or decode_ssrc in self.managed_channels:
                            self.logger.debug(f"Decode channel already exists for {freq/1e6:.4f} MHz")
                            continue
                        
                        if not self.ka9q_control.has_capacity():
                            self.logger.warning(f"Max channels reached after creating {created_count} decode channels, cannot create more")
                            break
                        
                        # Create narrow-bandwidth decode channel
                        channel_name = f"decode-{freq/1e6:.4f}"
                        self.logger.info(f"Creating decode channel at {freq/1e6:.4f} MHz (priority: {strength:.2f})")
                        
                        if self.ka9q_control.create_channel(
                            name=channel_name,
                            frequency=freq,
                            mode='iq',
                            samprate=48000,
                            scan=False,
                            channel_filter=self.channel_bandwidth
                        ):
                            with self.lock:
                                self.managed_channels.add(decode_ssrc)
                                self.confirmed_decode_channels.add(decode_ssrc)
                                self.detected_signals[freq] = time.time()
                                self.channel_frequencies[decode_ssrc] = freq  # Store SSRC → frequency mapping
                            self.logger.info(f"✓ Decode channel created at {freq/1e6:.4f} MHz (SSRC={decode_ssrc:08x}), decoder will spawn on first RTP packet")
                            created_count += 1
                        else:
                            self.logger.error(f"Failed to create decode channel at {freq/1e6:.4f} MHz")
                        
                        time.sleep(0.5)  # Stagger channel creation
                    
                    self.logger.info(f"Created {created_count} decode channels for signal validation")
                else:
                    self.logger.info("No radiosonde signals detected in this scan")
                
                # Cleanup stale detected signals
                current_time = time.time()
                with self.lock:
                    stale_signals = [
                        freq for freq, last_seen in self.detected_signals.items()
                        if current_time - last_seen > self.signal_detection_timeout
                    ]
                    for freq in stale_signals:
                        del self.detected_signals[freq]
                        self.logger.info(f"Removed stale signal: {freq/1e6:.4f} MHz")
                
                # Wait before next scan cycle
                if self.running:
                    self.logger.info(f"Next scan in {self.scan_interval}s...")
                    time.sleep(self.scan_interval)
                
            except Exception as e:
                self.logger.error(f"Error in spectrum scanning loop: {e}", exc_info=True)
                time.sleep(30)  # Wait before retry on error
        
        self.logger.info("KA9Q spectrum scanning stopped")
    
    def _analyze_spectrum_for_signals(self, scan_channel_map: Dict[int, float]) -> Dict[float, float]:
        """
        Analyze received RTP data to detect radiosonde signals
        
        Args:
            scan_channel_map: Dictionary mapping SSRC to center frequency for scan channels
        
        Returns:
            Dictionary of frequency (Hz) -> signal strength (dB)
        """
        detected = {}
        
        try:
            # CRITICAL FIX: When multiple scan channels are active but we have limited decode slots (max 8),
            # we must prioritize the scan channel with the HIGHEST packet count (strongest signal).
            # Otherwise, decode channels from weaker scans get created first, exhausting slots before
            # reaching the actual radiosonde frequency.
            
            # First pass: Find scan channels with activity and their packet counts
            active_scans = []  # List of (ssrc, freq, packet_count) tuples
            
            with self.lock:
                for ssrc, stream in self.active_streams.items():
                    # Skip if this is a known decode channel
                    if ssrc in self.confirmed_decode_channels:
                        continue
                    
                    # Check if this stream has significant activity
                    if stream.packet_count < 500:  # Need minimum packets for analysis
                        continue
                    
                    # Check packet recency
                    age = time.time() - stream.last_activity
                    if age > 5.0:  # Stream not active recently
                        continue
                    
                    # If it's a scan channel, track it
                    if ssrc in scan_channel_map:
                        freq = scan_channel_map[ssrc]
                        if self.scan_frequency_min <= freq <= self.scan_frequency_max:
                            active_scans.append((ssrc, freq, stream.packet_count))
                            self.logger.info(f"Scan channel SSRC {ssrc:08x} at {freq/1e6:.3f} MHz has {stream.packet_count} packets")
            
            if not active_scans:
                self.logger.info("No active scan channels detected")
                return detected
            
            # Sort by packet count (descending) to prioritize strongest signal
            active_scans.sort(key=lambda x: x[2], reverse=True)
            
            # CRITICAL INSIGHT: With limited channel slots (typically 8) and multiple active scans,
            # we need to distribute decode channels across ALL strong scans, not just the strongest one.
            # Radiosondes can be in ANY scan, and packet counts are similar across scans (radiod constant rate).
            # Solution: Select top scans with >70% of max packet count, distribute slots proportionally.
            
            top_scan_count = active_scans[0][2]
            threshold = 0.7 * top_scan_count
            selected_scans = [scan for scan in active_scans if scan[2] >= threshold]
            
            # Limit to top 2 scans to ensure adequate coverage per scan
            if len(selected_scans) > 2:
                selected_scans = selected_scans[:2]
            
            self.logger.info(
                f"Selected {len(selected_scans)} strong scan(s) from {len(active_scans)} total: "
                + ", ".join([f"{s[1]/1e6:.1f} MHz ({s[2]} pkt)" for s in selected_scans])
            )
            
            # Distribute channel slots proportionally by packet count (stronger scan gets more channels)
            total_packets = sum(scan[2] for scan in selected_scans)
            channels_allocation = {}
            allocated_total = 0
            
            for i, (ssrc, freq, pkt_count) in enumerate(selected_scans):
                if i == len(selected_scans) - 1:
                    # Last scan gets remaining slots to use all available
                    allocated = self.ka9q_control.max_channels - allocated_total
                else:
                    # Proportional allocation, minimum 3 channels per scan
                    allocated = max(3, int(self.ka9q_control.max_channels * pkt_count / total_packets))
                    allocated_total += allocated
                
                channels_allocation[ssrc] = allocated
            
            self.logger.info(
                f"Channel allocation: " + 
                ", ".join([f"{s[1]/1e6:.1f} MHz: {channels_allocation[s[0]]} ch" for s in selected_scans])
            )
            
            # Generate decode channels only for selected scans
            for ssrc, freq, packet_count in selected_scans:
                # Get allocated channel count for this scan
                allocated_channels = channels_allocation.get(ssrc, 4)
                
                # CRITICAL: With limited decode channel slots and 1 MHz scan bandwidth,
                # we must choose decode frequencies carefully to maximize coverage.
                # Strategy: Use 50 kHz steps (~21 channels), then select N channels
                # that are EVENLY DISTRIBUTED across the scan range.
                # 50 kHz ensures radiosonde within ±25 kHz is decodable (48 kHz sample rate = ±24 kHz Nyquist)
                
                decode_step = 50000  # 50 kHz steps (critical for 48 kHz sample rate coverage)
                scan_bandwidth_half = 500000  # ±500 kHz
                
                # Cover the FULL ±500 kHz bandwidth
                start_freq = freq - scan_bandwidth_half
                end_freq = freq + scan_bandwidth_half
                
                # Generate all possible decode frequencies
                all_decode_freqs = []
                test_freq = start_freq
                while test_freq <= end_freq:
                    all_decode_freqs.append(test_freq)
                    test_freq += decode_step
                
                # Select N channels evenly distributed across the scan
                # This ensures better coverage than just selecting the middle
                if len(all_decode_freqs) > allocated_channels:
                    # Calculate step to pick evenly distributed channels
                    step = len(all_decode_freqs) / allocated_channels
                    decode_freqs = [all_decode_freqs[int(i * step)] for i in range(allocated_channels)]
                    self.logger.info(
                        f"Selected {allocated_channels} evenly-distributed channels "
                        f"({decode_freqs[0]/1e6:.1f}-{decode_freqs[-1]/1e6:.1f} MHz) from scan at {freq/1e6:.1f} MHz"
                    )
                else:
                    decode_freqs = all_decode_freqs
                
                # Add all decode frequencies to detected dict
                # Use packet_count as priority - higher count = higher priority for channel creation
                activity_metric = packet_count / 1000.0  # Normalize to reasonable range
                for decode_freq in decode_freqs:
                    detected[decode_freq] = activity_metric
                
                self.logger.info(
                    f"✓ Selected scan at {freq/1e6:.4f} MHz ({packet_count} packets) "
                    f"- will create {len(decode_freqs)} decode channels at 50 kHz steps "
                    f"({decode_freqs[0]/1e6:.3f}-{decode_freqs[-1]/1e6:.3f} MHz)"
                )
            
        except Exception as e:
            self.logger.error(f"Error analyzing spectrum: {e}", exc_info=True)
        
        return detected
    
    def get_active_streams(self) -> List[KA9QStream]:
        """Get list of active KA9Q streams (excluding scan channels)"""
        with self.lock:
            # Remove stale streams (no activity for 60 seconds)
            current_time = time.time()
            self.active_streams = {
                ssrc: stream for ssrc, stream in self.active_streams.items()
                if current_time - stream.last_activity < 60
            }
            # Filter out scan channels - only return decode channels for WebUI
            return [
                stream for ssrc, stream in self.active_streams.items()
                if ssrc not in self.scan_channels
            ]
    
    def get_stream_count(self) -> int:
        """Get number of active streams"""
        return len(self.get_active_streams())
    
    def get_decoder_count(self) -> int:
        """Get number of active decoders"""
        with self.lock:
            return len(self.active_decoders)
