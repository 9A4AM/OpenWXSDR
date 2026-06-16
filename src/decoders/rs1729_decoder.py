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
import json
import re
import os
from typing import Optional, Callable
from datetime import datetime


class RS1729Decoder:
    """
    Manages rs1729 decoder processes for various radiosonde types
    Decodes raw IQ data from stdin (48 kHz, 16-bit signed)
    Detects --softin support and uses appropriate decoder flags
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
    
    # Class-level cache for decoder capabilities
    _decoder_caps = {}
    
    @classmethod
    def _detect_softin_support(cls, decoder_path: str) -> bool:
        """
        Detect if decoder supports --softin flag
        
        Args:
            decoder_path: Path to decoder binary
            
        Returns:
            True if --softin is supported
        """
        # Check cache first
        if decoder_path in cls._decoder_caps:
            return cls._decoder_caps[decoder_path]
        
        try:
            # Run decoder with --help and check for --softin
            result = subprocess.run(
                [decoder_path, '--help'],
                capture_output=True,
                text=True,
                timeout=2
            )
            has_softin = '--softin' in result.stdout or '--softin' in result.stderr
            cls._decoder_caps[decoder_path] = has_softin
            return has_softin
        except Exception:
            # If we can't detect, assume not available
            cls._decoder_caps[decoder_path] = False
            return False
    
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
        
        # Detect decoder capabilities
        self.has_softin = self._detect_softin_support(self.decoder_path)
        if not self.has_softin and self.sonde_type in ['RS41', 'DFM']:
            self.logger.warning(f"{decoder_binary} does not support --softin flag. "
                              f"Using IQ mode with text PTU fallback. "
                              f"For full PTU support, install Auto_RX-compatible decoders from rs1729/RS.")
        
        self.process: Optional[subprocess.Popen] = None
        self.running = False
        self.frame_callback: Optional[Callable] = None
        self.last_frame_time: Optional[datetime] = None
        self.frame_count = 0
        self._start_time: Optional[float] = None
        #self.debug_json_ptu = os.environ.get("OPENWX_JSON_PTU_DEBUG", "0").lower() in ("1", "true", "yes", "on")
        self.debug_json_ptu = 1
        self.ptu_cache = {}  # Cache PTU data from text lines (recency-based merge)
        self.ptu_cache_timestamps = {}  # Track PTU data timestamps for recency matching
    
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
                # RS41: rs41mod with --json and --ptu2 for full telemetry including PTU
                # -vv: VERY verbose (needed to get PTU in text when using --json)
                # --ptu2: PTU sensor data, --sat: satellite count
                # --json: JSON output with position/velocity
                # PTU data appears in verbose text lines, not in JSON (with --IQ mode)
                cmd.extend(['-vv', '--ptu2', '--sat', '--json', '--IQ', '0.0', '-', '48000', '16'])
            elif self.sonde_type == 'DFM':
                # DFM: dfm09mod -i -vv --IQ 0.0 --ecc --json --dist --ptu - 48000 16
                # DFM decoder reliably supports --json with full telemetry
                # -ID flag shows actual serial (without it, serial is masked as "xxxxxxxx")
                cmd.extend(['-i', '-vv', '-ID', '--IQ', '0.0', '--ecc', '--json', '--dist', '--ptu', '-', '48000', '16'])
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
                cmd.extend(['-vv', '--ptu2', '--sat', '--json', '--IQ', '0.0', '-', '48000', '16'])
            
            # Wrap with stdbuf (if available) to force line-buffered stdout AND stderr
            # on the child process.  Without this, libc switches to block-buffering
            # when stdout/stderr are pipes → PTU data on stderr arrives too late!
            # -oL: line-buffered stdout (for JSON frames)
            # -eL: line-buffered stderr (for PTU text lines - CRITICAL!)
            stdbuf = shutil.which('stdbuf')
            if stdbuf:
                cmd = [stdbuf, '-oL', '-eL'] + cmd

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
            
            # Monitor for early crashes
            time.sleep(2.0)
            if self.process.poll() is not None:
                exit_code = self.process.poll()
                self.logger.error(f"Decoder crashed early with exit code {exit_code}")
                return False
            else:
                self.logger.info(f"Decoder still running after 2s, PID={self.process.pid}")
            
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
        """Monitor decoder stdout. Prefer JSON frames, use text lines only as PTU fallback/debug."""
        if not self.process or not self.process.stdout:
            self.logger.error("stdout monitoring: no process or stdout available")
            return

        line_count = 0
        try:
            for raw_line in self.process.stdout:
                if not self.running:
                    self.logger.info(f"stdout monitoring: stopped (running=False) after {line_count} lines")
                    break

                line = raw_line.decode('utf-8', errors='replace').strip() if isinstance(raw_line, bytes) else raw_line.strip()
                if not line:
                    continue
                
                line_count += 1

                if line.startswith('{') and line.endswith('}'):
                    try:
                        json_data = json.loads(line)
                        frame = self._parse_json_frame(json_data)
                        if frame and self.frame_callback:
                            self.frame_count += 1
                            self.last_frame_time = datetime.now()
                            self.frame_callback(frame)
                    except Exception as e:
                        self.logger.error(f"Could not parse decoder JSON line: {e}")
                    continue

                # Non-JSON lines: extract PTU data
                try:
                    self._extract_ptu_from_text(line)
                except Exception as e:
                    self.logger.debug(f"PTU text fallback parse failed: {e}")
        except Exception as e:
            if self.running:
                self.logger.error(f"Error monitoring decoder stdout: {e}")
    def _monitor_stderr(self):
        """Monitor decoder stderr mainly for debug and legacy PTU text fallback."""
        if not self.process or not self.process.stderr:
            self.logger.warning("stderr monitoring: no process or stderr available")
            return
        
        stderr_line_count = 0
        try:
            for raw_line in self.process.stderr:
                if not self.running:
                    self.logger.info(f"stderr monitoring stopped after {stderr_line_count} lines")
                    break
                
                line = raw_line.decode('utf-8', errors='replace').strip() if isinstance(raw_line, bytes) else raw_line.strip()
                if not line:
                    continue
                
                stderr_line_count += 1
                
                # Always log the first 10 stderr lines for debugging
                if stderr_line_count <= 10:
                    self.logger.info(f"Decoder stderr [{stderr_line_count}]: {line}")
                
                # Try to extract PTU data from this line
                try:
                    self._extract_ptu_from_text(line)
                except Exception as e:
                    if self.debug_json_ptu:
                        self.logger.debug(f"PTU extraction failed: {e}")
                
                # Log all stderr lines when debug mode is on
                if self.debug_json_ptu and stderr_line_count > 10:
                    self.logger.info(f"Decoder stderr: {line}")
        
        except Exception as e:
            self.logger.error(f"Error monitoring decoder stderr: {e}", exc_info=True)

    def _extract_ptu_from_text(self, line: str):
        """Legacy PTU fallback from verbose text output, cached with timestamps for recency-based merging.
        
        RS41 text format with -vv --ptu2:
        [ 5644] (W4060809)  Mon 2026-06-08 05:06:05.997  lat: 52.89519 lon: 7.89611 alt: 24350.9  vH: 10.4 D: 294.0 vV: 6.3  T=-47.6°C RH=5.8% P=24.38hPa
        
        Uses recency-based caching instead of exact frame number matching,
        because frame numbers from text and JSON may not align perfectly.
        """
        frame_match = re.search(r'\[\s*(\d+)\]', line)
        if not frame_match:
            return
        frame_num = int(frame_match.group(1))
        ptu = {}

        # Match T=-47.6°C or T=-47.6
        m = re.search(r'T=([+-]?\d+(?:\.\d+)?)', line)
        if m:
            ptu['temp'] = float(m.group(1))
        # Match RH=5.8% or RH=5.8
        m = re.search(r'RH=(\d+(?:\.\d+)?)', line)
        if m:
            ptu['humidity'] = float(m.group(1))
        # Match P=24.38hPa or P=24.38
        m = re.search(r'P=(\d+(?:\.\d+)?)', line)
        if m:
            ptu['pressure'] = float(m.group(1))

        if ptu:
            # Store with current timestamp for recency-based matching
            now = time.time()
            self.ptu_cache[frame_num] = ptu
            self.ptu_cache_timestamps[frame_num] = now
            
            # Cleanup old cache entries (keep last 100)
            if len(self.ptu_cache) > 200:
                # Remove oldest by timestamp
                sorted_by_time = sorted(self.ptu_cache_timestamps.items(), key=lambda x: x[1])
                for k, _ in sorted_by_time[:-100]:
                    self.ptu_cache.pop(k, None)
                    self.ptu_cache_timestamps.pop(k, None)
                    
            if self.debug_json_ptu:
                self.logger.info(f"Cached fallback PTU for frame {frame_num}: {ptu}")
    def _parse_json_frame(self, json_data: dict) -> Optional[dict]:
        """Parse decoder JSON output. JSON is the primary source for PTU and navigation data."""
        try:
            sonde_id = str(json_data.get('id') or json_data.get('serial') or '').strip()
            if not sonde_id:
                return None

            lat = json_data.get('lat')
            lon = json_data.get('lon')
            alt = json_data.get('alt')
            frame_num = json_data.get('frame')
            if lat is None or lon is None or alt is None or frame_num is None:
                return None

            sonde_type = str(json_data.get('type') or self.sonde_type).strip()
            if sonde_type.startswith('0x'):
                sonde_type = self.sonde_type
            if 'DFM' in sonde_type and sonde_id.lstrip('D').isdigit():
                sonde_id = f"DFM-{sonde_id.lstrip('D')}"

            decoded_datetime = None
            dt_raw = json_data.get('datetime')
            if dt_raw:
                try:
                    dt_str = dt_raw.rstrip('Z')
                    fmt = '%Y-%m-%dT%H:%M:%S.%f' if '.' in dt_str else '%Y-%m-%dT%H:%M:%S'
                    decoded_datetime = datetime.strptime(dt_str, fmt)
                except Exception:
                    pass

            frame = {
                'sonde_id': sonde_id,
                'sonde_type': sonde_type,
                'frame_number': int(frame_num),
                'frequency': self.frequency,
                'lat': float(lat),
                'lon': float(lon),
                'alt': float(alt),
                'decoded_datetime': decoded_datetime,
            }

            # Optional fields – only include when present in this JSON frame
            for src, dst, cast in [
                ('vel_h', 'velocity_horizontal', float),
                ('vel_v', 'velocity_vertical', float),
                ('heading', 'heading', float),
                ('sats', 'sats', int),
                ('batt', 'battery', float),
                ('bt', 'burst_timer', int),
                ('subtype', 'subtype', str),
                ('rs41_mainboard', 'rs41_mainboard', str),
                ('rs41_mainboard_fw', 'rs41_mainboard_fw', int),
                ('tx_frequency', 'tx_frequency', float),
                ('ref_datetime', 'ref_datetime', str),
                ('ref_position', 'ref_position', str),
            ]:
                value = json_data.get(src)
                if value is not None:
                    try:
                        frame[dst] = cast(value)
                    except (TypeError, ValueError):
                        pass

            for field in ('temp', 'tempc', 'temperature', 'T'):
                if json_data.get(field) is not None:
                    try:
                        frame['temp'] = float(json_data.get(field))
                        break
                    except (TypeError, ValueError):
                        pass
            for field in ('humidity', 'humidityrh', 'rh', 'RH'):
                if json_data.get(field) is not None:
                    try:
                        frame['humidity'] = float(json_data.get(field))
                        break
                    except (TypeError, ValueError):
                        pass
            for field in ('pressure', 'pressurehpa', 'pres', 'P'):
                if json_data.get(field) is not None:
                    try:
                        frame['pressure'] = float(json_data.get(field))
                        break
                    except (TypeError, ValueError):
                        pass

            # Recency-based PTU fallback merge: match by proximity instead of exact frame number
            # because text and JSON frame numbers may not align perfectly
            if not all(frame.get(k) for k in ('temp', 'humidity', 'pressure')):
                # Look for recent PTU data within +/- 5 frames
                current_frame = frame['frame_number']
                best_match = None
                best_distance = 999999
                
                for cached_frame, timestamp in self.ptu_cache_timestamps.items():
                    frame_distance = abs(cached_frame - current_frame)
                    if frame_distance <= 5 and frame_distance < best_distance:
                        best_match = cached_frame
                        best_distance = frame_distance
                
                if best_match is not None:
                    cached = self.ptu_cache[best_match]
                    frame.setdefault('temp', cached.get('temp'))
                    frame.setdefault('humidity', cached.get('humidity'))
                    frame.setdefault('pressure', cached.get('pressure'))
                    if self.debug_json_ptu:
                        self.logger.info(f"[PTU Merged by recency] Frame {current_frame} matched with text frame {best_match} (distance={best_distance}): T={frame.get('temp')}°C RH={frame.get('humidity')}% P={frame.get('pressure')}hPa")

            if self.debug_json_ptu:
                ptu_keys = {k: json_data.get(k) for k in ('temp', 'tempc', 'temperature', 'T', 'humidity', 'humidityrh', 'rh', 'RH', 'pressure', 'pressurehpa', 'pres', 'P') if k in json_data}
                # self.logger.info(f"JSON frame keys={sorted(json_data.keys())}")
                # self.logger.info(f"JSON PTU candidate fields for {sonde_id}: {ptu_keys}")
                final_ptu = {k: frame.get(k) for k in ('temp', 'humidity', 'pressure') if frame.get(k) is not None}
                # self.logger.info(f"Final frame PTU for {sonde_id} frame {frame['frame_number']}: {final_ptu}")

            return frame
        except Exception as e:
            self.logger.error(f"Error parsing JSON frame: {e}")
            return None

    def _parse_frame(self, line: str) -> Optional[dict]:
        """Legacy text-frame parser retained as fallback/debug helper."""
        try:
            frame = {}
            if '(' in line and ')' in line:
                start = line.find('(') + 1
                end = line.find(')', start)
                sonde_id = line[start:end]
                if ',' not in sonde_id:
                    frame['sonde_id'] = sonde_id
            parts = line.split()
            for i, part in enumerate(parts):
                if part == 'lat:' and i + 1 < len(parts):
                    frame['lat'] = float(parts[i + 1])
                elif part == 'lon:' and i + 1 < len(parts):
                    frame['lon'] = float(parts[i + 1])
                elif part == 'alt:' and i + 1 < len(parts):
                    frame['alt'] = float(parts[i + 1])
                elif part == 'vH:' and i + 1 < len(parts):
                    frame['velocity_horizontal'] = float(parts[i + 1])
                elif part == 'vV:' and i + 1 < len(parts):
                    frame['velocity_vertical'] = float(parts[i + 1])
                elif part == 'D:' and i + 1 < len(parts):
                    frame['heading'] = float(parts[i + 1])
            return frame or None
        except Exception as e:
            self.logger.debug(f"Could not parse legacy text frame: {e}")
            return None
