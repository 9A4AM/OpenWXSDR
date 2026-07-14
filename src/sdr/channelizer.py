"""
# =============================================================================
#  OpenWX -- Open Weather Radiosonde Telemetry System
# =============================================================================
#
#  File   : channelizer.py
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
#  Multi-channel radiosonde decoder using iq_dec from flux242/radiosonde.
#  Enables decoding up to 4 sondes per RTL-SDR (2.4 MHz) or 8 sondes per
#  Airspy Mini (6 MHz) simultaneously.
#
#  Architecture:
#    - iq_dec process: Provides I/Q streaming and signal detection
#    - Multiple decoder processes: One rs1729 decoder per active channel
#    - Telemetry callbacks: Same interface as RS1729Decoder
#
#  Integration Status: STEP 2 - Module stub (not yet integrated into DeviceWorker)
#
# =============================================================================
"""

import logging
import subprocess
import threading
import time
import json
import queue
from typing import Optional, Callable, List, Dict
from dataclasses import dataclass
from datetime import datetime

from ..decoders.models import SondeTelemetry


@dataclass
class ChannelInfo:
    """Information about an active decoder channel"""
    frequency: float  # Hz
    sonde_type: str  # RS41, DFM, M10, etc.
    sonde_serial: Optional[str] = None
    decoder_process: Optional[subprocess.Popen] = None
    iq_client_process: Optional[subprocess.Popen] = None  # Pipeline: iq_client
    iq_fm_process: Optional[subprocess.Popen] = None  # Pipeline: iq_fm
    iq_dec_channel_id: Optional[int] = None  # Channel ID (legacy, unused with iq_svcl)
    start_time: float = 0.0
    last_frame_time: float = 0.0
    frame_count: int = 0
    is_active: bool = True


@dataclass
class DetectedSignal:
    """Signal detected by iq_dec scanner"""
    frequency: float  # Hz
    power_db: float  # Signal power in dB
    bandwidth: float  # Hz
    timestamp: float


class IqDecChannelizer:
    """
    Multi-channel radiosonde decoder using iq_dec from flux242/radiosonde.
    
    Manages concurrent decoding of multiple radiosondes within the SDR bandwidth.
    Uses iq_dec for signal detection and I/Q channelization, with separate
    rs1729 decoder processes for each active channel.
    
    Status: STEP 2 - Standalone module (not integrated into DeviceWorker yet)
    """

    def __init__(self, device_config: dict, app_config: dict, 
                 telemetry_callback: Optional[Callable[[SondeTelemetry], None]] = None,
                 device_serial: str = "channelizer",
                 device_index: int = 0):
        """
        Initialize channelizer for a specific SDR device.
        
        Args:
            device_config: Device-specific config (from config.yaml rtlsdr.devices[])
            app_config: Global app config
            telemetry_callback: Callback for decoded telemetry frames
            device_serial: Device serial number for logging
            device_index: Device index (0-3) for iq_server -d parameter
        """
        self.device_config = device_config
        self.app_config = app_config
        self.telemetry_callback = telemetry_callback
        self.device_serial = device_serial
        self.device_index = device_index
        self.logger = logging.getLogger(f'Channelizer.{device_serial}')
        
        # Configuration
        self.center_freq = device_config.get('center_freq', 404_000_000)
        self.sample_rate = device_config.get('sample_rate', 2_400_000)
        self.gain = device_config.get('gain', 40)
        self.ppm_error = device_config.get('ppm_error', 0)
        self.max_channels = device_config.get('max_channels', 4)
        self.channel_bandwidth = device_config.get('channel_bandwidth', 12000)
        self.detection_threshold = device_config.get('detection_threshold', 4)
        
        # Runtime state
        self._running = False
        self._iq_dec_process: Optional[subprocess.Popen] = None  # iq_server process
        self._rtl_sdr_process: Optional[subprocess.Popen] = None  # rtl_sdr process
        self._active_channels: Dict[float, ChannelInfo] = {}  # freq_hz -> ChannelInfo
        self._channel_lock = threading.Lock()
        
        # Decoder paths from config
        decoders_cfg = app_config.get('decoders', {})
        self._rs1729_path = decoders_cfg.get('rs1729_path', './decoders/rs1729')
        
        # flux242/radiosonde iq_svcl binary paths
        # These should be in ../radiosonde/iq_svcl/ or in PATH
        self._iq_server_path = '../radiosonde/iq_svcl/iq_server'
        self._iq_client_path = '../radiosonde/iq_svcl/iq_client'
        self._iq_fm_path = '../radiosonde/iq_svcl/iq_fm'
        
        # TCP port for iq_server (default 1280, can be configured)
        self._iq_server_port = device_config.get('iq_server_port', 1280)
        
        # Thread for monitoring decoder outputs
        self._monitor_thread: Optional[threading.Thread] = None
        
        self.logger.info(
            f"Initialized channelizer: center={self.center_freq/1e6:.3f} MHz, "
            f"rate={self.sample_rate/1e6:.1f} MHz, gain={self.gain} dB, "
            f"max_channels={self.max_channels}, port={self._iq_server_port}"
        )

    def start(self) -> bool:
        """
        Start the channelizer (rtl_sdr | iq_server pipeline).
        
        iq_server doesn't open RTL-SDR directly. It reads I/Q from stdin.
        We need to start rtl_sdr that pipes to iq_server.
        
        Returns:
            True if started successfully, False otherwise
        """
        if self._running:
            self.logger.warning("Channelizer already running")
            return True
        
        try:
            import os
            
            self.logger.info("Starting rtl_sdr | iq_server channelizer pipeline")
            
            # Check if iq_server binary exists
            if not os.path.exists(self._iq_server_path):
                self.logger.error(f"iq_server not found at {self._iq_server_path}")
                return False
            
            # Build rtl_sdr command (producer)
            # rtl_sdr -d <device_serial> -p <ppm> -f <freq> -g <gain> -s <sample_rate> -
            rtl_sdr_cmd = [
                'rtl_sdr',
                '-d', self.device_serial,  # RTL-SDR device serial (stable across reboots)
                '-f', str(int(self.center_freq)),  # Center frequency
                '-g', str(self.gain),  # Gain
                '-s', str(int(self.sample_rate)),  # Sample rate
                '-'  # Output to stdout
            ]
            
            # Add PPM correction if non-zero
            if self.ppm_error != 0:
                rtl_sdr_cmd.insert(-1, '-p')
                rtl_sdr_cmd.insert(-1, str(self.ppm_error))
            
            # Build iq_server command (consumer)
            # iq_server --bo 32 --if <if_freq> -p <tcp_port> - <sample_rate> <channels>
            # --bo 32: 32-bit output format
            # --if: intermediate frequency for decimation (24000 Hz per flux242 proven config)
            # -: read from stdin
            # <sample_rate>: input sample rate (must be multiple of IF: 2400000/24000=100)
            # <channels>: number of channels (8 for multi-channel)
            # Note: iq_server uses hardcoded default port (1280), no -p flag supported
            iq_server_cmd = [
                self._iq_server_path,
                '--bo', '32',  # 32-bit output
                '--if', '24000',  # IF frequency (24 kHz decimated output, flux242 proven)
                '-', str(int(self.sample_rate)), '8'  # stdin, sample rate, 8 channels
            ]
            
            self.logger.info(f"rtl_sdr command: {' '.join(rtl_sdr_cmd)}")
            self.logger.info(f"iq_server command: {' '.join(iq_server_cmd)}")
            
            # Start rtl_sdr process
            rtl_sdr_proc = subprocess.Popen(
                rtl_sdr_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,  # Capture stderr for diagnostics
                stdin=subprocess.DEVNULL
            )
            
            # Start iq_server process (reads from rtl_sdr stdout)
            self._iq_dec_process = subprocess.Popen(
                iq_server_cmd,
                stdin=rtl_sdr_proc.stdout,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0
            )
            
            # Close rtl_sdr stdout in parent to allow proper SIGPIPE handling
            if rtl_sdr_proc.stdout:
                rtl_sdr_proc.stdout.close()
            
            # Store rtl_sdr process for cleanup
            self._rtl_sdr_process = rtl_sdr_proc
            
            # Start monitoring threads for rtl_sdr and iq_server stderr
            threading.Thread(
                target=self._monitor_rtl_sdr_stderr,
                args=(rtl_sdr_proc,),
                daemon=True,
                name=f"RTL-SDR-{self.device_serial}"
            ).start()
            
            threading.Thread(
                target=self._monitor_iq_server_stderr,
                args=(self._iq_dec_process,),
                daemon=True,
                name=f"IqServer-{self.device_serial}"
            ).start()
            
            # Wait briefly for pipeline to initialize
            time.sleep(2.0)
            
            # Check if processes started successfully
            if rtl_sdr_proc.poll() is not None:
                stderr = rtl_sdr_proc.stderr.read() if rtl_sdr_proc.stderr else b''
                self.logger.error(
                    f"rtl_sdr failed to start (exit code {rtl_sdr_proc.returncode}): "
                    f"{stderr.decode('utf-8', errors='replace')}"
                )
                if self._iq_dec_process:
                    self._iq_dec_process.terminate()
                return False
            
            if self._iq_dec_process.poll() is not None:
                stderr = self._iq_dec_process.stderr.read() if self._iq_dec_process.stderr else b''
                self.logger.error(
                    f"iq_server failed to start (exit code {self._iq_dec_process.returncode}): "
                    f"{stderr.decode('utf-8', errors='replace')}"
                )
                rtl_sdr_proc.terminate()
                return False
            
            self._running = True
            self.logger.info(
                f"Channelizer started: rtl_sdr PID {rtl_sdr_proc.pid}, "
                f"iq_server PID {self._iq_dec_process.pid}, port {self._iq_server_port}"
            )
            return True
            
        except Exception as e:
            self.logger.error(f"Failed to start channelizer: {e}", exc_info=True)
            return False

    def stop(self):
        """Stop the channelizer and all active channels."""
        if not self._running:
            return
        
        self.logger.info("Stopping channelizer")
        self._running = False
        
        # Stop all active channels
        with self._channel_lock:
            for freq in list(self._active_channels.keys()):
                self._stop_channel_internal(freq)
        
        # Stop iq_server process
        if self._iq_dec_process:
            try:
                self._iq_dec_process.terminate()
                self._iq_dec_process.wait(timeout=5)
            except Exception as e:
                self.logger.error(f"Error stopping iq_server: {e}")
                try:
                    self._iq_dec_process.kill()
                except:
                    pass
            self._iq_dec_process = None
        
        # Stop rtl_sdr process
        if self._rtl_sdr_process:
            try:
                self._rtl_sdr_process.terminate()
                self._rtl_sdr_process.wait(timeout=5)
            except Exception as e:
                self.logger.error(f"Error stopping rtl_sdr: {e}")
                try:
                    self._rtl_sdr_process.kill()
                except:
                    pass
            self._rtl_sdr_process = None
        
        self.logger.info("Channelizer stopped")

    def scan_spectrum(self) -> List[DetectedSignal]:
        """
        Scan the spectrum for radiosonde signals.
        
        Uses iq_dec's signal detection to find active radiosondes within
        the SDR bandwidth.
        
        Returns:
            List of detected signals
        """
        if not self._running:
            self.logger.warning("Cannot scan - channelizer not running")
            return []
        
        # Placeholder implementation
        # Actual implementation will query iq_dec for detected signals
        self.logger.debug("Scanning spectrum (stub)")
        
        # For Step 2, return empty list
        # Real implementation will parse iq_dec output for signal detections
        return []

    def start_channel(self, frequency: float, sonde_type: str) -> bool:
        """
        Start decoding a channel at the specified frequency.
        
        Launches a decoder pipeline: iq_client | iq_fm | rs1729_decoder
        
        Args:
            frequency: Frequency in Hz
            sonde_type: Sonde type (RS41, DFM, M10, etc.)
            
        Returns:
            True if channel started successfully, False otherwise
        """
        with self._channel_lock:
            # Check if channel already exists
            if frequency in self._active_channels:
                self.logger.warning(f"Channel already active at {frequency/1e6:.3f} MHz")
                return False
            
            # Check channel limit
            if len(self._active_channels) >= self.max_channels:
                self.logger.warning(
                    f"Cannot start channel - max channels ({self.max_channels}) reached"
                )
                return False
            
            # Check if frequency is within bandwidth
            freq_offset = abs(frequency - self.center_freq)
            max_offset = self.sample_rate / 2
            if freq_offset > max_offset:
                self.logger.error(
                    f"Frequency {frequency/1e6:.3f} MHz outside bandwidth "
                    f"(center={self.center_freq/1e6:.3f} MHz, "
                    f"rate={self.sample_rate/1e6:.1f} MHz)"
                )
                return False
            
            self.logger.info(
                f"Starting channel {len(self._active_channels)+1}/{self.max_channels}: "
                f"{sonde_type} at {frequency/1e6:.3f} MHz"
            )
            
            # Get decoder path
            decoder_path = self._get_decoder_path(sonde_type)
            if not decoder_path:
                self.logger.error(f"No decoder available for {sonde_type}")
                return False
            
            try:
                import os
                
                # Check if binaries exist
                if not os.path.exists(self._iq_client_path):
                    self.logger.error(f"iq_client not found at {self._iq_client_path}")
                    return False
                if not os.path.exists(self._iq_fm_path):
                    self.logger.error(f"iq_fm not found at {self._iq_fm_path}")
                    return False
                
                # Build decoder pipeline: iq_client | decoder (--IQ mode)
                # iq_client connects to iq_server and tunes to frequency
                # decoder parses IQ telemetry directly and outputs JSON
                # Based on flux242/receivemultisonde.sh proven working configuration
                
                # Calculate frequency offset from center as RELATIVE frequency
                # iq_client expects -0.5 < freq < 0.5 where freq = offset_hz / sample_rate
                freq_offset_hz = frequency - self.center_freq
                freq_relative = freq_offset_hz / self.sample_rate
                
                self.logger.info(
                    f"Frequency tuning: {frequency/1e6:.3f} MHz, "
                    f"center {self.center_freq/1e6:.3f} MHz, "
                    f"offset {freq_offset_hz/1e3:.1f} kHz, "
                    f"relative {freq_relative:.4f}"
                )
                
                # Build command pipeline
                # iq_client -h localhost -p <port> --freq <relative_freq>
                iq_client_cmd = [
                    self._iq_client_path,
                    '-h', 'localhost',
                    '-p', str(self._iq_server_port),
                    '--freq', f'{freq_relative:.6f}'  # Relative frequency -0.5 to 0.5
                ]
                
                # Decoder command with --IQ mode for direct IQ processing
                # --IQ 0.0: IQ mode with DC offset 0.0
                # -: read from stdin
                # 24000: IF sample rate (must match iq_server --if parameter)
                # 32: bits per sample (float32)
                decoder_cmd = [
                    decoder_path,
                    '--json', '--ptu',
                    '--IQ', '0.0',
                    '-', '24000', '32'  # IF rate matches iq_server
                ]
                
                # Start pipeline processes
                # iq_client → decoder (direct IQ processing)
                iq_client_proc = subprocess.Popen(
                    iq_client_cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    stdin=subprocess.DEVNULL
                )
                
                # Decoder (reads IQ directly from iq_client)
                decoder_proc = subprocess.Popen(
                    decoder_cmd,
                    stdin=iq_client_proc.stdout,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,  # Capture stderr for monitoring thread
                    bufsize=1,
                    universal_newlines=True
                )
                
                # Close iq_client stdout in parent to allow proper SIGPIPE handling
                if iq_client_proc.stdout:
                    iq_client_proc.stdout.close()
                
                # Create channel info
                channel = ChannelInfo(
                    frequency=frequency,
                    sonde_type=sonde_type,
                    decoder_process=decoder_proc,  # Store final process for monitoring
                    start_time=time.time()
                )
                
                # Store pipeline processes for cleanup
                channel.iq_client_process = iq_client_proc
                
                self._active_channels[frequency] = channel
                
                # Start monitoring threads for decoder stdout and stderr
                stdout_thread = threading.Thread(
                    target=self._monitor_decoder_output,
                    args=(frequency, decoder_proc),
                    daemon=True,
                    name=f"StdoutMonitor-{self.device_serial}-{frequency/1e6:.1f}MHz"
                )
                stdout_thread.start()
                
                stderr_thread = threading.Thread(
                    target=self._monitor_decoder_stderr,
                    args=(frequency, decoder_proc),
                    daemon=True,
                    name=f"StderrMonitor-{self.device_serial}-{frequency/1e6:.1f}MHz"
                )
                stderr_thread.start()
                
                # Health check after 3 seconds
                threading.Thread(
                    target=self._check_decoder_health,
                    args=(frequency, decoder_proc),
                    daemon=True,
                    name=f"HealthCheck-{self.device_serial}-{frequency/1e6:.1f}MHz"
                ).start()
                
                self.logger.info(
                    f"Channel started: {sonde_type} at {frequency/1e6:.3f} MHz "
                    f"(PIDs: iq_client={iq_client_proc.pid}, "
                    f"decoder={decoder_proc.pid}) "
                    f"({len(self._active_channels)}/{self.max_channels} active)"
                )
                return True
                
            except Exception as e:
                self.logger.error(f"Failed to start channel: {e}", exc_info=True)
                return False

    def stop_channel(self, frequency: float) -> bool:
        """
        Stop decoding a channel.
        
        Args:
            frequency: Frequency in Hz
            
        Returns:
            True if channel stopped successfully, False if not found
        """
        with self._channel_lock:
            return self._stop_channel_internal(frequency)

    def _stop_channel_internal(self, frequency: float) -> bool:
        """Internal channel stop (must be called with lock held)."""
        if frequency not in self._active_channels:
            self.logger.warning(f"Channel not found at {frequency/1e6:.3f} MHz")
            return False
        
        channel = self._active_channels[frequency]
        
        self.logger.info(
            f"Stopping channel: {channel.sonde_type} at {frequency/1e6:.3f} MHz "
            f"(frames={channel.frame_count}, "
            f"duration={time.time()-channel.start_time:.1f}s)"
        )
        
        # Stop decoder process
        if channel.decoder_process:
            try:
                channel.decoder_process.terminate()
                channel.decoder_process.wait(timeout=5)
            except Exception as e:
                self.logger.error(f"Error stopping decoder: {e}")
                try:
                    channel.decoder_process.kill()
                except:
                    pass
        
        # Stop iq_client process
        if hasattr(channel, 'iq_client_process') and channel.iq_client_process:
            try:
                channel.iq_client_process.terminate()
                channel.iq_client_process.wait(timeout=2)
            except:
                try:
                    channel.iq_client_process.kill()
                except:
                    pass
        
        # Remove from active channels
        del self._active_channels[frequency]
        
        self.logger.info(
            f"Channel stopped at {frequency/1e6:.3f} MHz "
            f"({len(self._active_channels)}/{self.max_channels} active)"
        )
        return True

    def _monitor_decoder_output(self, frequency: float, decoder_process: subprocess.Popen):
        """
        Monitor decoder output and parse telemetry.
        
        Reads JSON lines from decoder stdout and calls telemetry_callback for each frame.
        Runs in a separate thread per channel.
        
        Args:
            frequency: Channel frequency in Hz
            decoder_process: Decoder subprocess with stdout to read
        """
        self.logger.info(f"Decoder stdout monitor started: {frequency/1e6:.3f} MHz, PID={decoder_process.pid}")
        
        try:
            while self._running:
                if not decoder_process.stdout:
                    break
                
                # Read line from decoder
                line = decoder_process.stdout.readline()
                if not line:
                    # EOF - decoder stopped
                    break
                
                line = line.strip()
                if not line:
                    continue
                
                try:
                    # Parse JSON telemetry
                    data = json.loads(line)
                    
                    # Update channel info
                    with self._channel_lock:
                        if frequency in self._active_channels:
                            channel = self._active_channels[frequency]
                            channel.frame_count += 1
                            channel.last_frame_time = time.time()
                            
                            # Extract serial if present
                            if 'serial' in data and not channel.sonde_serial:
                                channel.sonde_serial = data['serial']
                                self.logger.info(
                                    f"Identified sonde: {channel.sonde_type} "
                                    f"serial {channel.sonde_serial} at {frequency/1e6:.3f} MHz"
                                )
                    
                    # Convert to SondeTelemetry and call callback
                    if self.telemetry_callback:
                        telemetry = self._json_to_telemetry(data, frequency)
                        if telemetry:
                            self.telemetry_callback(telemetry)
                
                except json.JSONDecodeError as e:
                    # Not JSON - might be debug output from decoder
                    self.logger.debug(f"Non-JSON output from decoder: {line[:100]}")
                except Exception as e:
                    self.logger.error(f"Error processing telemetry: {e}", exc_info=True)
        
        except Exception as e:
            self.logger.error(f"Monitor thread error for {frequency/1e6:.3f} MHz: {e}")
        
        finally:
            exit_code = decoder_process.poll()
            self.logger.info(f"Decoder stdout monitor stopped: {frequency/1e6:.3f} MHz (exit code: {exit_code})")
    
    def _monitor_decoder_stderr(self, frequency: float, decoder_process: subprocess.Popen):
        """
        Monitor decoder stderr output and log diagnostic messages.
        
        Similar to legacy RS41Decoder stderr monitoring - logs startup messages,
        errors, and diagnostic output at INFO level.
        
        Args:
            frequency: Channel frequency in Hz
            decoder_process: Decoder subprocess with stderr to read
        """
        self.logger.info(f"Decoder stderr monitor started: {frequency/1e6:.3f} MHz")
        
        try:
            line_count = 0
            while self._running and decoder_process.poll() is None:
                if not decoder_process.stderr:
                    break
                
                line = decoder_process.stderr.readline()
                if not line:
                    break
                
                line = line.strip()
                if line:
                    line_count += 1
                    # Log first 10 lines at INFO (startup messages), rest at DEBUG
                    if line_count <= 10:
                        self.logger.info(f"Decoder stderr [{line_count}]: {line}")
                    else:
                        self.logger.debug(f"Decoder stderr: {line}")
        
        except Exception as e:
            self.logger.error(f"Stderr monitor error for {frequency/1e6:.3f} MHz: {e}")
        
        finally:
            self.logger.info(f"Decoder stderr monitor stopped: {frequency/1e6:.3f} MHz")
    
    def _check_decoder_health(self, frequency: float, decoder_process: subprocess.Popen):
        """
        Check decoder health after startup period.
        
        Waits 3 seconds then verifies decoder process is still running.
        Logs warning if decoder exited prematurely.
        
        Args:
            frequency: Channel frequency in Hz
            decoder_process: Decoder subprocess to check
        """
        time.sleep(3)
        
        exit_code = decoder_process.poll()
        if exit_code is not None:
            # Decoder exited during startup
            self.logger.error(
                f"Decoder failed startup for {frequency/1e6:.3f} MHz "
                f"(exit code {exit_code}, PID was {decoder_process.pid})"
            )
            
            # Check if channel still tracked
            with self._channel_lock:
                if frequency in self._active_channels:
                    channel = self._active_channels[frequency]
                    if channel.frame_count == 0:
                        self.logger.warning(
                            f"No frames decoded in 3s for {frequency/1e6:.3f} MHz - "
                            f"check iq_server/iq_client connection or signal strength"
                        )
        else:
            # Decoder still running - check frame count
            with self._channel_lock:
                if frequency in self._active_channels:
                    channel = self._active_channels[frequency]
                    if channel.frame_count > 0:
                        self.logger.info(
                            f"Decoder healthy: {frequency/1e6:.3f} MHz, "
                            f"PID={decoder_process.pid}, {channel.frame_count} frame(s)"
                        )
                    else:
                        self.logger.warning(
                            f"Decoder running but no frames yet: {frequency/1e6:.3f} MHz, "
                            f"PID={decoder_process.pid} - waiting for sync signal..."
                        )
    
    def _monitor_rtl_sdr_stderr(self, rtl_sdr_process: subprocess.Popen):
        """
        Monitor rtl_sdr stderr output for USB device errors and diagnostics.
        
        Args:
            rtl_sdr_process: rtl_sdr subprocess with stderr to read
        """
        self.logger.info(f"rtl_sdr stderr monitor started for {self.device_serial}, PID={rtl_sdr_process.pid}")
        
        try:
            line_count = 0
            while self._running:
                # Check if process still running
                if rtl_sdr_process.poll() is not None:
                    break
                
                if not rtl_sdr_process.stderr:
                    break
                
                line = rtl_sdr_process.stderr.readline()
                if not line:
                    # Empty line - wait briefly and check process status
                    time.sleep(0.1)
                    continue
                
                line = line.decode('utf-8', errors='replace').strip()
                if line:
                    line_count += 1
                    # Log all rtl_sdr output at INFO (usually just startup messages)
                    self.logger.info(f"rtl_sdr [{line_count}]: {line}")
        
        except Exception as e:
            self.logger.error(f"rtl_sdr stderr monitor error: {e}")
        
        finally:
            exit_code = rtl_sdr_process.poll()
            self.logger.warning(f"rtl_sdr stopped for {self.device_serial} (exit code: {exit_code})")
    
    def _monitor_iq_server_stderr(self, iq_server_process: subprocess.Popen):
        """
        Monitor iq_server stderr output for TCP server initialization and errors.
        
        Args:
            iq_server_process: iq_server subprocess with stderr to read
        """
        self.logger.info(f"iq_server stderr monitor started, PID={iq_server_process.pid}")
        
        try:
            line_count = 0
            while self._running:
                # Check if process still running
                if iq_server_process.poll() is not None:
                    break
                
                if not iq_server_process.stderr:
                    break
                
                line = iq_server_process.stderr.readline()
                if not line:
                    # Empty line - wait briefly and check process status
                    time.sleep(0.1)
                    continue
                
                line = line.decode('utf-8', errors='replace').strip()
                if line:
                    line_count += 1
                    # Log all iq_server output at INFO
                    self.logger.info(f"iq_server [{line_count}]: {line}")
        
        except Exception as e:
            self.logger.error(f"iq_server stderr monitor error: {e}")
        
        finally:
            exit_code = iq_server_process.poll()
            self.logger.warning(f"iq_server stopped (exit code: {exit_code})")
    
    def _json_to_telemetry(self, data: dict, frequency: float) -> Optional[SondeTelemetry]:
        """
        Convert decoder JSON to SondeTelemetry object.
        
        Args:
            data: JSON dict from decoder
            frequency: Channel frequency in Hz
            
        Returns:
            SondeTelemetry object or None if invalid
        """
        try:
            # Extract common fields (format varies by decoder)
            telemetry = SondeTelemetry(
                sonde_type=data.get('type', 'Unknown'),
                serial=data.get('serial', 'Unknown'),
                frequency=frequency,
                datetime_str=data.get('datetime', ''),
                latitude=float(data.get('lat', 0.0)),
                longitude=float(data.get('lon', 0.0)),
                altitude=float(data.get('alt', 0.0)),
                climb_rate=float(data.get('vel_v', 0.0)),
                heading=float(data.get('heading', 0.0)),
                speed_h=float(data.get('vel_h', 0.0)),
                temperature=float(data.get('temp', 0.0)) if 'temp' in data else None,
                humidity=float(data.get('humidity', 0.0)) if 'humidity' in data else None,
                pressure=float(data.get('pressure', 0.0)) if 'pressure' in data else None,
                frame_number=int(data.get('frame', 0)),
                rssi=float(data.get('rssi', 0.0)) if 'rssi' in data else None
            )
            return telemetry
        except Exception as e:
            self.logger.error(f"Failed to convert telemetry: {e}")
            return None

    def get_active_channels(self) -> List[ChannelInfo]:
        """
        Get list of active decoder channels.
        
        Returns:
            List of ChannelInfo objects for active channels
        """
        with self._channel_lock:
            return list(self._active_channels.values())

    def get_channel_count(self) -> int:
        """Get number of active channels."""
        with self._channel_lock:
            return len(self._active_channels)

    def get_channel_info(self, frequency: float) -> Optional[ChannelInfo]:
        """Get info for a specific channel."""
        with self._channel_lock:
            return self._active_channels.get(frequency)

    def is_frequency_active(self, frequency: float) -> bool:
        """Check if a frequency is currently being decoded."""
        with self._channel_lock:
            return frequency in self._active_channels

    def has_capacity(self) -> bool:
        """Check if channelizer can accept more channels."""
        with self._channel_lock:
            return len(self._active_channels) < self.max_channels

    def _get_decoder_path(self, sonde_type: str) -> Optional[str]:
        """
        Get path to decoder binary for sonde type.
        
        Args:
            sonde_type: Sonde type (RS41, DFM, M10, etc.)
            
        Returns:
            Path to decoder binary, or None if not found
        """
        import os
        
        # Map sonde types to decoder binaries
        decoder_map = {
            'RS41': 'rs41mod',
            'RS92': 'rs92mod',
            'DFM': 'dfm09mod',
            'M10': 'm10mod',
            'M20': 'm20mod',
            'iMet': 'imet54mod',
            'LMS6': 'lms6mod',
            'MRZ': 'mrzmod'
        }
        
        decoder_name = decoder_map.get(sonde_type)
        if not decoder_name:
            return None
        
        decoder_path = os.path.join(self._rs1729_path, decoder_name)
        
        # Check if binary exists
        if not os.path.exists(decoder_path):
            self.logger.warning(f"Decoder not found: {decoder_path}")
            return None
        
        return decoder_path

    def __repr__(self) -> str:
        """String representation for debugging."""
        return (
            f"IqDecChannelizer(device={self.device_serial}, "
            f"center={self.center_freq/1e6:.3f}MHz, "
            f"channels={len(self._active_channels)}/{self.max_channels}, "
            f"running={self._running})"
        )


# =============================================================================
# Testing / Development Helper
# =============================================================================

def test_channelizer():
    """Test function for development - not used in production."""
    logging.basicConfig(level=logging.DEBUG)
    
    # Example device config
    device_config = {
        'serial': 'TEST001',
        'center_freq': 404_000_000,
        'sample_rate': 2_400_000,
        'gain': 40,
        'ppm_error': 0,
        'decoder_mode': 'channelizer',
        'max_channels': 4,
        'channel_bandwidth': 12000,
        'detection_threshold': 4
    }
    
    app_config = {
        'decoders': {
            'rs1729_path': './decoders/rs1729'
        }
    }
    
    # Create channelizer
    channelizer = IqDecChannelizer(device_config, app_config, device_serial='TEST001', device_index=0)
    
    print(f"Created: {channelizer}")
    
    # Test lifecycle
    if channelizer.start():
        print("✓ Channelizer started")
        
        # Test channel management
        if channelizer.start_channel(405_700_000, 'RS41'):
            print("✓ Channel 1 started (RS41 @ 405.7 MHz)")
        
        if channelizer.start_channel(403_500_000, 'DFM'):
            print("✓ Channel 2 started (DFM @ 403.5 MHz)")
        
        # Show active channels
        channels = channelizer.get_active_channels()
        print(f"✓ Active channels: {len(channels)}")
        for ch in channels:
            print(f"  - {ch.sonde_type} @ {ch.frequency/1e6:.3f} MHz")
        
        # Test scan
        signals = channelizer.scan_spectrum()
        print(f"✓ Spectrum scan: {len(signals)} signals")
        
        # Cleanup
        channelizer.stop()
        print("✓ Channelizer stopped")
    else:
        print("✗ Failed to start channelizer")


if __name__ == '__main__':
    test_channelizer()
