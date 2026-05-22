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
        self._latest_fields: dict = {}
        self._latest_fields_time: Optional[float] = None
    
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
            cmd = [self.decoder_path]
            
            # Add decoder-specific flags
            if self.sonde_type == 'RS41':
                # RS41: rs41mod -v --ptu2 --sat --IQ 0.0 - 48000 16
                # --sat adds GPS satellite count to each output line as (N)
                # NOTE: --ptu2 should output T=...C, P=...hPa, RH=...% lines
                cmd.extend(['-v', '--ptu2', '--sat', '--IQ', '0.0', '-', '48000', '16'])
            elif self.sonde_type == 'DFM':
                # DFM: dfm09mod -i -vv --IQ 0.0 --ecc --json --dist --ptu - 48000 16
                cmd.extend(['-i', '-vv', '--IQ', '0.0', '--ecc', '--json', '--dist', '--ptu', '-', '48000', '16'])
            elif self.sonde_type == 'M10':
                # M10: m10mod -v --IQ 0.0 - 48000 16
                cmd.extend(['-v', '--IQ', '0.0', '-', '48000', '16'])
            elif self.sonde_type == 'RS92':
                # RS92: rs92mod -v --IQ 0.0 - 48000 16
                cmd.extend(['-v', '--IQ', '0.0', '-', '48000', '16'])
            elif self.sonde_type == 'M20':
                # M20: m20mod -v --IQ 0.0 - 48000 16
                cmd.extend(['-v', '--IQ', '0.0', '-', '48000', '16'])
            elif self.sonde_type == 'iMet':
                # iMet: imet54mod -v --IQ 0.0 - 48000 16
                cmd.extend(['-v', '--IQ', '0.0', '-', '48000', '16'])
            else:
                # Default: assume RS41-like syntax
                cmd.extend(['-v', '--ptu2', '--IQ', '0.0', '-', '48000', '16'])
            
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
        """Monitor decoder stdout for frame data"""
        if not self.process or not self.process.stdout:
            return

        try:
            for raw_line in self.process.stdout:
                if not self.running:
                    break

                # stdout is binary (bufsize=0, universal_newlines=False)
                if isinstance(raw_line, bytes):
                    line = raw_line.decode('utf-8', errors='replace').strip()
                else:
                    line = raw_line.strip()
                if not line:
                    continue
                
                # Check for frame data (rs41mod outputs lines with frame info)
                # Example: "[   20] (DL2MF-11)  Sat 2026-05-02 07:19:05.000  lat: 48.1234 ..."
                if line.startswith('[') and ']' in line:
                    self.frame_count += 1
                    self.last_frame_time = datetime.now()
                    self.logger.info(f"Frame {self.frame_count}: {line}")

                    # Parse and callback if handler registered
                    if self.frame_callback:
                        try:
                            frame_data = self._parse_frame(line)
                            frame_data = self._merge_latest_fields(frame_data)
                            if frame_data:
                                self.frame_callback(frame_data)
                        except Exception as e:
                            self.logger.error(f"Error in frame callback: {e}")
                else:
                    self._update_latest_fields(line)
                    # Log all other decoder stdout at DEBUG (verbose GPS/satellite data)
                    # This prevents log spam when debug mode is off
                    self.logger.debug(f"Decoder stdout: {line}")
        
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
    
    def _parse_frame(self, line: str) -> Optional[dict]:
        """
        Parse frame data from decoder output
        
        Args:
            line: Output line from decoder
            
        Returns:
            Dictionary with frame data or None
        """
        try:
            # First try to parse as JSON (if --json flag was used)
            if line.strip().startswith('{'):
                import json
                try:
                    json_data = json.loads(line)
                    frame = {
                        'raw_line': line,
                        'frequency': self.frequency,
                        'timestamp': datetime.now().isoformat(),
                        'sonde_id': json_data.get('id', 'UNKNOWN'),
                        'sonde_type': json_data.get('type', self.sonde_type),
                        'subtype': json_data.get('subtype'),  # e.g., "0xC:DFM17"
                        'lat': json_data.get('lat'),
                        'lon': json_data.get('lon'),
                        'alt': json_data.get('alt'),
                        'velocity_horizontal': json_data.get('vel_h'),
                        'velocity_vertical': json_data.get('vel_v'),
                        'heading': json_data.get('heading'),
                        'temp': json_data.get('temp'),
                        'humidity': json_data.get('humidity'),
                        'pressure': json_data.get('pressure'),
                        'battery': json_data.get('batt'),
                        'sats': json_data.get('sats')
                    }
                    return frame
                except json.JSONDecodeError:
                    pass  # Fall through to text parsing
            
            # Parse text format
            # Example RS41: "[   20] (DL2MF-11)  Sat 2026-05-02 07:19:05.000  lat: 48.1234  lon: 11.5678  alt: 123.45   vH:  5.0  D: 270.0  vV: 2.5"
            # Example DFM: "[225] 2026-05-04 16:32:44.0 (0,0,0)   lat: 53.07775 (0)   lon: 10.69429 (0)   alt: 12674.2 (0)   vH: 17.21  D:  77.5  vV: -5.45   T=-55.1C  (IDxC:23030665:DFM17)"
            
            frame = {
                'raw_line': line,
                'frequency': self.frequency,
                'timestamp': datetime.now().isoformat(),
                'sonde_type': self.sonde_type
            }
            
            # Extract sonde ID - for DFM, look for (IDxC:serial:type) pattern first
            if '(IDxC:' in line or '(ID' in line:
                # DFM format: (IDxC:23030665:DFM17)
                import re
                id_match = re.search(r'\(ID[^:]*:(\d+):([^)]+)\)', line)
                if id_match:
                    serial = id_match.group(1)
                    subtype = id_match.group(2)
                    frame['sonde_id'] = f"{self.sonde_type}-{serial}"
                    frame['subtype'] = subtype
            
            # If no special ID found, try standard parentheses format
            if 'sonde_id' not in frame and '(' in line and ')' in line:
                start = line.find('(') + 1
                end = line.find(')', start)
                sonde_id = line[start:end]
                # Skip if it's just coordinates like (0,0,0)
                if ',' not in sonde_id:
                    frame['sonde_id'] = sonde_id

            # RS41 subtype/model may appear near end of line, e.g.
            # ": RS41-SGP : RSM421". Capture subtype for SondeHub mapping.
            import re
            rs41_subtype_match = re.search(r':\s*(RS41-[A-Z0-9]+)\s*:', line)
            if rs41_subtype_match:
                frame['subtype'] = rs41_subtype_match.group(1)
                frame['sonde_type'] = 'RS41'

            rs41_model_match = re.search(r':\s*RS41-[A-Z0-9]+\s*:\s*([A-Z0-9]+)\s*$', line)
            if rs41_model_match:
                frame['rs41_model'] = rs41_model_match.group(1)

            # Parse optional PTU/satellite fields commonly present in rs41mod text output.
            temp_match = re.search(r'\bT=\s*(-?\d+(?:\.\d+)?)C\b', line)
            if temp_match:
                frame['temp'] = float(temp_match.group(1))
                self.logger.debug(f"[PTU] Parsed temp={frame['temp']}°C from frame line")

            pressure_match = re.search(r'\bP=\s*(-?\d+(?:\.\d+)?)hPa\b', line)
            if pressure_match:
                frame['pressure'] = float(pressure_match.group(1))
                self.logger.debug(f"[PTU] Parsed pressure={frame['pressure']}hPa from frame line")

            humidity_match = re.search(r'\bRH\d*=\s*(-?\d+(?:\.\d+)?)%\b', line)
            if humidity_match:
                frame['humidity'] = float(humidity_match.group(1))
                self.logger.debug(f"[PTU] Parsed humidity={frame['humidity']}% from frame line")

            # rs41mod may report GPS SV count as '(23)', sometimes followed by
            # extra decoder annotations such as ': fq 405700' or ': cd 223.5min'.
            sats_match = re.search(r'\bRH\d*=\s*-?\d+(?:\.\d+)?%\s+\((\d{1,2})\)(?:\s*:\s*.*)?$', line)
            if not sats_match:
                sats_match = re.search(r'\((\d{1,2})\)(?:\s*:\s*.*)?$', line)
            if sats_match:
                frame['sats'] = int(sats_match.group(1))
            
            # Extract coordinates and altitude
            parts = line.split()
            for i, part in enumerate(parts):
                if part == 'lat:' and i + 1 < len(parts):
                    try:
                        frame['lat'] = float(parts[i + 1])
                    except:
                        pass
                elif part == 'lon:' and i + 1 < len(parts):
                    try:
                        frame['lon'] = float(parts[i + 1])
                    except:
                        pass
                elif part == 'alt:' and i + 1 < len(parts):
                    try:
                        frame['alt'] = float(parts[i + 1])
                    except:
                        pass
                elif part == 'vH:' and i + 1 < len(parts):
                    try:
                        frame['velocity_horizontal'] = float(parts[i + 1])
                    except:
                        pass
                elif part == 'vV:' and i + 1 < len(parts):
                    try:
                        frame['velocity_vertical'] = float(parts[i + 1])
                    except:
                        pass
                elif part == 'D:' and i + 1 < len(parts):
                    try:
                        frame['heading'] = float(parts[i + 1])
                    except:
                        pass
            
            return frame
            
        except Exception as e:
            self.logger.debug(f"Could not parse frame: {e}")
            return None

    def _update_latest_fields(self, line: str):
        """Parse non-frame stdout lines and cache latest telemetry fields."""
        try:
            # Log raw line for PTU debugging if it might contain PTU data
            if any(pattern in line for pattern in ['T=', 'P=', 'RH', 'temp', 'pres', 'hum']):
                self.logger.info(f"[PTU-RAW] Potential PTU line: {line}")
            
            updated = {}

            # Example: "lat: 51.49438  lon: 7.41763  alt: 23699.61   vH:  1.9  D:  10.3  vV: 4.5"
            pos_match = re.search(
                r'lat:\s*(-?\d+(?:\.\d+)?)\s+lon:\s*(-?\d+(?:\.\d+)?)\s+alt:\s*(-?\d+(?:\.\d+)?)',
                line
            )
            if pos_match:
                updated['lat'] = float(pos_match.group(1))
                updated['lon'] = float(pos_match.group(2))
                updated['alt'] = float(pos_match.group(3))

            vh_match = re.search(r'\bvH:\s*(-?\d+(?:\.\d+)?)', line)
            if vh_match:
                updated['velocity_horizontal'] = float(vh_match.group(1))

            vv_match = re.search(r'\bvV:\s*(-?\d+(?:\.\d+)?)', line)
            if vv_match:
                updated['velocity_vertical'] = float(vv_match.group(1))

            heading_match = re.search(r'\bD:\s*(-?\d+(?:\.\d+)?)', line)
            if heading_match:
                updated['heading'] = float(heading_match.group(1))

            # Example: "numSatsFix: 10  sAcc: 0.1  pDOP: 1.3"
            sats_fix_match = re.search(r'\bnumSatsFix:\s*(\d+)', line)
            if sats_fix_match:
                sats_val = int(sats_fix_match.group(1))
                updated['sats'] = sats_val
                self.logger.debug(f"Parsed numSatsFix: {sats_val} from line: {line}")

            temp_match = re.search(r'\bT=\s*(-?\d+(?:\.\d+)?)C\b', line)
            if temp_match:
                updated['temp'] = float(temp_match.group(1))
                self.logger.debug(f"[PTU] Parsed temp={updated['temp']}°C from aux line")

            pressure_match = re.search(r'\bP=\s*(-?\d+(?:\.\d+)?)hPa\b', line)
            if pressure_match:
                updated['pressure'] = float(pressure_match.group(1))
                self.logger.debug(f"[PTU] Parsed pressure={updated['pressure']}hPa from aux line")

            humidity_match = re.search(r'\bRH\d*=\s*(-?\d+(?:\.\d+)?)%\b', line)
            if humidity_match:
                updated['humidity'] = float(humidity_match.group(1))
                self.logger.debug(f"[PTU] Parsed humidity={updated['humidity']}% from aux line")

            if updated:
                old_sats = self._latest_fields.get('sats')
                self._latest_fields.update(updated)
                self._latest_fields_time = time.time()
                if 'sats' in updated and updated['sats'] != old_sats:
                    self.logger.info(f"[SATS_UPDATE] Updated sats: {old_sats} -> {updated['sats']}")
        except Exception as e:
            self.logger.debug(f"Could not parse auxiliary decoder line: {e}")

    def _merge_latest_fields(self, frame_data: Optional[dict]) -> Optional[dict]:
        """Merge recently-seen non-frame fields into a parsed frame."""
        if not frame_data:
            return frame_data

        sonde_id = frame_data.get('sonde_id', '?')
        
        # Keep side-channel telemetry only while fresh to avoid stale carry-over.
        if not self._latest_fields or not self._latest_fields_time:
            self.logger.debug(f"[MERGE] {sonde_id}: No latest fields available")
            return frame_data
        age_s = time.time() - self._latest_fields_time
        if age_s > 15:
            self.logger.debug(f"[MERGE] {sonde_id}: latest fields too old ({age_s:.1f}s > 15s)")
            return frame_data

        merged_keys = []
        for key, value in self._latest_fields.items():
            if key not in frame_data and value is not None:
                frame_data[key] = value
                merged_keys.append(f"{key}={value}")

        if merged_keys:
            self.logger.info(f"[MERGE] {sonde_id}: Added fields: {', '.join(merged_keys)}")
        
        # Log if sats is missing
        if 'sats' not in frame_data:
            self.logger.debug(f"[MERGE] {sonde_id}: sats not available (fields: {list(self._latest_fields.keys())})")

        return frame_data
