"""
# =============================================================================
#  OpenWX -- Open Weather Radiosonde Telemetry System
# =============================================================================
#
#  File   : rs1729_decoder.py
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
#  Subprocess wrapper around the rs1729 radiosonde decoder binaries.
#
#  RS1729Decoder spawns and manages a single rs1729 decoder child process
#  (rs41mod, dfm09mod, m10mod, m20mod, rs92mod, imet54mod, lms6mod, mrzmod)
#  fed with raw 48 kHz / 16-bit signed IQ audio piped directly from rtl_fm
#  or the Airspy channelizer via stdin.
#
#  Decoder binary selection:
#    RS41 → rs41mod    DFM → dfm09mod    M10 → m10mod
#    M20  → m20mod     RS92 → rs92mod    iMet → imet54mod
#
#  Output parsing handles both JSON (--json flag, DFM) and plain-text
#  (RS41/M10/RS92) decoder output formats, including side-channel PTU
#  and GPS satellite count lines merged into subsequent frame callbacks.
#  Uses stdbuf -oL (when available) to force line-buffered child stdout.
#
# =============================================================================
"""

import shutil
import subprocess
import json
import logging
import threading
import time
import os
import re
from typing import Optional, Callable
from datetime import datetime


class RS1729Decoder:
    """
    Manages rs1729 decoder processes for various radiosonde types
    Decodes raw IQ data from stdin (48 kHz, 16-bit signed)
    """
    
    # Mapping of sonde types to decoder binaries
    DECODER_MAP = {
        'RS41': 'rs41mod',
        'RS92': 'rs92mod',
        'DFM': 'dfm09mod',
        'M10': 'm10mod',
        'M20': 'm20mod',
        'iMet': 'imet54mod',
        'LMS6': 'lms6mod',
        'MRZ': 'mrzmod'
    }
    
    def __init__(self, frequency: float, sonde_type: str = 'RS41', decoder_path: str = None):
        """
        Initialize decoder
        
        Args:
            frequency: Signal frequency in Hz
            sonde_type: Type of radiosonde (RS41, DFM, M10, etc.)
            decoder_path: Path to decoder executable (default: auto-detect based on sonde_type)
        """
        self.frequency = frequency
        self.sonde_type = sonde_type.upper() if sonde_type else 'RS41'
        
        # Get decoder binary name for this sonde type
        decoder_binary = self.DECODER_MAP.get(self.sonde_type, 'rs41mod')
        
        # Auto-detect decoder path if not specified
        if decoder_path is None:
            # Try multiple locations
            possible_paths = [
                f'decoders/rs1729/{decoder_binary}',  # Relative to working directory
                f'/home/pi/OpenWXSDR/decoders/rs1729/{decoder_binary}',  # Installed location
                f'/home/pi/openwxsdr-1.0.21/decoders/rs1729/{decoder_binary}',  # Package directory
                decoder_binary  # In PATH
            ]
            
            for path in possible_paths:
                full_path = os.path.join(os.getcwd(), path) if not os.path.isabs(path) else path
                if os.path.isfile(full_path) or os.path.isfile(path):
                    decoder_path = path
                    break
            
            # Default fallback
            if decoder_path is None:
                decoder_path = decoder_binary
        
        self.decoder_path = decoder_path
        self.logger = logging.getLogger(f'{self.sonde_type}Decoder.{frequency/1e6:.3f}')
        
        self.process: Optional[subprocess.Popen] = None
        self.running = False
        self.frame_callback: Optional[Callable] = None
        self.last_frame_time: Optional[datetime] = None
        self.frame_count = 0
        self._start_time: Optional[float] = None
    
    def set_frame_callback(self, callback: Callable[[dict], None]):
        """Set callback for decoded frames"""
        self.frame_callback = callback
    
    def start(self, audio_stream) -> bool:
        """
        Start decoder with IQ stream from stdin
        
        Args:
            audio_stream: Audio stream file object (rtl_fm stdout)
            
        Returns:
            True if decoder started successfully
        """
        if not audio_stream:
            self.logger.error("No audio stream provided")
            return False
        
        try:
            # Build decoder command based on sonde type
            # Different decoders have different command-line options
            # NOTE: RS/demod/mod decoders (rs41mod, dfm09mod) support --json with full telemetry
            cmd = [self.decoder_path]
            
            # Add decoder-specific flags
            if self.sonde_type == 'RS41':
                # RS41: rs41mod (from RS/demod/mod) supports --json output
                # -v: verbose, --ptu2: PTU sensor data, --sat: satellite count
                # --json: JSON output with full telemetry
                cmd.extend(['-v', '--ptu2', '--sat', '--json', '--IQ', '0.0', '-', '48000', '16'])
            elif self.sonde_type == 'DFM':
                # DFM: dfm09mod -i -vv --IQ 0.0 --ecc --json --dist --ptu - 48000 16
                # DFM decoder reliably supports --json with full telemetry
                # -ID flag shows actual serial (without it, serial is masked as "xxxxxxxx")
                cmd.extend(['-i', '-vv', '-ID', '--IQ', '0.0', '--ecc', '--json', '--dist', '--ptu', '-', '48000', '16'])
            elif self.sonde_type == 'M10':
                cmd.extend(['-v', '--json', '--IQ', '0.0', '-', '48000', '16'])
            elif self.sonde_type == 'RS92':
                cmd.extend(['-v', '--json', '--IQ', '0.0', '-', '48000', '16'])
            elif self.sonde_type == 'M20':
                cmd.extend(['-v', '--json', '--IQ', '0.0', '-', '48000', '16'])
            elif self.sonde_type == 'iMet':
                cmd.extend(['-v', '--json', '--IQ', '0.0', '-', '48000', '16'])
            else:
                cmd.extend(['-v', '--json', '--IQ', '0.0', '-', '48000', '16'])
            
            # Wrap with stdbuf (if available) to force line-buffered stdout on the
            # child process.  Without this, libc switches to 8 KB block-buffering
            # when stdout is a pipe → first decoded frames only appear after
            # ~80 seconds (8192 bytes / ~100 bytes per frame / 1 frame/s).
            stdbuf = shutil.which('stdbuf')
            if stdbuf:
                cmd = [stdbuf, '-oL'] + cmd

            self.logger.info(f"Starting decoder: {' '.join(cmd)}")

            # Start decoder with stdin piped from rtl_fm / Airspy channelizer
            # bufsize=0 (unbuffered binary) is critical: the decoder reads raw int16 IQ
            # bytes; universal_newlines must be False to keep stdin in binary mode.
            self.process = subprocess.Popen(
                cmd,
                stdin=audio_stream,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
                universal_newlines=False
            )
            
            # Wait briefly to check startup
            time.sleep(0.5)
            if self.process.poll() is not None:
                exit_code = self.process.poll()
                stderr = self.process.stderr.read(500).decode('utf-8', errors='replace') if self.process.stderr else ""
                self.logger.error(f"Decoder exited immediately with code {exit_code}")
                if stderr:
                    self.logger.error(f"Decoder stderr: {stderr}")
                return False
            
            self.running = True
            self._start_time = time.time()
            
            # Start threads to monitor stdout and stderr
            self.stdout_thread = threading.Thread(
                target=self._monitor_stdout,
                daemon=True
            )
            self.stderr_thread = threading.Thread(
                target=self._monitor_stderr,
                daemon=True
            )
            
            self.stdout_thread.start()
            self.stderr_thread.start()
            
            self.logger.info(f"Decoder started - processing {self.frequency/1e6:.4f} MHz")
            return True
            
        except FileNotFoundError:
            self.logger.error(f"Decoder not found: {self.decoder_path}. Install rs1729 decoder tools.")
            return False
        except Exception as e:
            self.logger.error(f"Failed to start decoder: {e}")
            return False
    
    def stop(self):
        """Stop decoder process"""
        self.running = False
        
        if self.process:
            try:
                self.process.terminate()
                self.process.wait(timeout=2)
            except:
                self.process.kill()
            self.process = None
        
        self.logger.info("Decoder stopped")
    
    def is_alive(self) -> bool:
        """Check if decoder is still running"""
        if not self.process:
            return False
        
        exit_code = self.process.poll()
        if exit_code is not None:
            if exit_code != 0:
                self.logger.warning(f"Decoder exited with code {exit_code}")
            return False
        
        return True
    
    def is_idle(self, idle_threshold: int = 300) -> bool:
        """Check if decoder hasn't received frames recently."""
        if not self.last_frame_time:
            # No frames ever received — idle if the decoder has been running
            # longer than the idle threshold (start_time tracked via frame_count==0)
            if not hasattr(self, '_start_time') or self._start_time is None:
                return False
            return (time.time() - self._start_time) > idle_threshold

        # Frames were received before; check how long ago the last one was
        time_since_last = (datetime.now() - self.last_frame_time).total_seconds()
        return time_since_last > idle_threshold
    
    def get_frame_stats(self) -> dict:
        """Get decoder statistics"""
        return {
            'frequency': self.frequency,
            'frame_count': self.frame_count,
            'last_frame': self.last_frame_time.isoformat() if self.last_frame_time else None,
            'running': self.running and self.is_alive()
        }
    
    def _monitor_stdout(self):
        """Monitor decoder stdout for frame data.

        All supported decoders are started with --json, so every decoded frame
        arrives as a single JSON object on stdout.  TEXT lines (e.g. the verbose
        copy that rs41mod also writes) are logged for diagnostics only.
        """
        if not self.process or not self.process.stdout:
            return

        try:
            for raw_line in self.process.stdout:
                if not self.running:
                    break

                if isinstance(raw_line, bytes):
                    line = raw_line.decode('utf-8', errors='replace').strip()
                else:
                    line = raw_line.strip()
                if not line:
                    continue

                if line.startswith('{'):
                    # Primary frame source: JSON output from --json flag
                    self.logger.debug(f"Decoder JSON: {line}")
                    try:
                        json_data = json.loads(line)
                        frame_data = self._parse_json_frame(json_data)
                        if frame_data and self.frame_callback:
                            self.frame_count += 1
                            self.last_frame_time = datetime.now()
                            self.frame_callback(frame_data)
                    except (json.JSONDecodeError, Exception) as e:
                        self.logger.debug(f"Could not parse JSON decoder line: {e}")
                else:
                    # TEXT / verbose lines – log for diagnostics, not used for frame data
                    if line.startswith('[') and ']' in line:
                        self.logger.info(f"Decoder: {line}")
                    else:
                        self.logger.debug(f"Decoder: {line}")

        except Exception as e:
            if self.running:
                self.logger.error(f"Error monitoring decoder stdout: {e}")
    
    def _monitor_stderr(self):
        """Monitor decoder stderr for errors"""
        if not self.process or not self.process.stderr:
            return

        try:
            for raw_line in self.process.stderr:
                if not self.running:
                    break

                if isinstance(raw_line, bytes):
                    line = raw_line.decode('utf-8', errors='replace').strip()
                else:
                    line = raw_line.strip()
                if not line:
                    continue
                
                # Log all decoder stderr at INFO — even startup/debug messages
                # are valuable when diagnosing decode failures.
                self.logger.info(f"Decoder stderr: {line}")
        
        except Exception as e:
            if self.running:
                self.logger.error(f"Error monitoring decoder stderr: {e}")
    
    def _parse_json_frame(self, json_data: dict) -> Optional[dict]:
        """Build a normalised frame_data dict from a decoder JSON object.

        Validates the required fields (id, lat, lon, alt, frame) and maps
        the decoder's key names to the internal names expected by
        decoder_manager._on_frame_decoded().

        Returns None if any required field is missing or invalid.
        """
        sonde_id = str(json_data.get('id') or json_data.get('serial') or '').strip()
        if not sonde_id:
            self.logger.debug("Skipping JSON frame: missing 'id' field")
            return None

        lat = json_data.get('lat')
        lon = json_data.get('lon')
        alt = json_data.get('alt')
        frame_num = json_data.get('frame')
        if lat is None or lon is None or alt is None or frame_num is None:
            self.logger.debug(f"Skipping JSON frame for {sonde_id}: missing lat/lon/alt/frame")
            return None

        sonde_type = str(json_data.get('type') or self.sonde_type).strip().upper()

        # DFM serials arrive as plain digits – normalise to "DFM-<serial>"
        if 'DFM' in sonde_type and sonde_id.lstrip('D').isdigit():
            sonde_id = f"DFM-{sonde_id.lstrip('D')}"

        # GPS datetime from decoder (naive UTC)
        decoded_datetime = None
        dt_raw = json_data.get('datetime')
        if dt_raw:
            try:
                dt_str = dt_raw.rstrip('Z')
                fmt = '%Y-%m-%dT%H:%M:%S.%f' if '.' in dt_str else '%Y-%m-%dT%H:%M:%S'
                decoded_datetime = datetime.strptime(dt_str, fmt)
            except Exception:
                pass

        frame_data: dict = {
            'sonde_id':   sonde_id,
            'sonde_type': sonde_type,
            'frame_number': int(frame_num),
            'frequency':  self.frequency,
            'lat':  float(lat),
            'lon':  float(lon),
            'alt':  float(alt),
            'decoded_datetime': decoded_datetime,
        }

        # Optional fields – only include when present in this JSON frame
        for src, dst, cast in [
            ('vel_h',            'velocity_horizontal', float),
            ('vel_v',            'velocity_vertical',   float),
            ('heading',          'heading',             float),
            ('sats',             'sats',                int),
            ('batt',             'battery',             float),
            ('bt',               'burst_timer',         int),
            ('subtype',          'subtype',             str),
            ('rs41_mainboard',   'rs41_mainboard',      str),
            ('rs41_mainboard_fw','rs41_mainboard_fw',   int),
            ('tx_frequency',     'tx_frequency',        int),
            ('ref_datetime',     'ref_datetime',        str),
            ('ref_position',     'ref_position',        str),
            ('temp',             'temp',                float),
            ('pressure',         'pressure',            float),
            ('humidity',         'humidity',            float),
        ]:
            v = json_data.get(src)
            if v is not None:
                try:
                    frame_data[dst] = cast(v)
                except (TypeError, ValueError):
                    pass

        self.logger.debug(
            f"[JSON] {sonde_id} frame={frame_num} lat={lat:.5f} lon={lon:.5f} "
            f"alt={float(alt):.1f} sats={frame_data.get('sats')} "
            f"batt={frame_data.get('battery')} bt={frame_data.get('burst_timer')}"
        )
        return frame_data
