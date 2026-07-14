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
    _decoder_failures = {}  # Track failures per (path, type) for cooldown

    @classmethod
    def resolve_decoder_path(cls, decoder_binary: str) -> Optional[str]:
        """Locate a decoder binary: relative to the working directory first
        (matches the systemd unit's WorkingDirectory=<install dir>), then via
        PATH. Single source of truth for decoder path resolution — previously
        this list was duplicated (and had drifted slightly out of sync,
        including host-specific absolute paths like /home/pi/OpenWXSDR/...)
        between here and device_manager.py's own _get_decoder_path(); that one
        now delegates here too."""
        relative_path = f'decoders/rs1729/{decoder_binary}'
        full_path = os.path.join(os.getcwd(), relative_path)
        if os.path.isfile(full_path) or os.path.isfile(relative_path):
            return relative_path

        which_path = shutil.which(decoder_binary)
        if which_path:
            return which_path

        return None

    @classmethod
    def _detect_decoder_capabilities(cls, decoder_path: str) -> dict:
        """
        Detect decoder binary capabilities by probing --help output
        
        Args:
            decoder_path: Path to decoder binary
            
        Returns:
            Dict with capability flags: softin, json, ID, ptu, ecc, dist, sat
        """
        # Check cache first
        if decoder_path in cls._decoder_caps:
            return cls._decoder_caps[decoder_path]
        
        caps = {
            'softin': False,
            'json': False,
            'ID': False,
            'ptu': False,
            'ptu2': False,
            'ecc': False,
            'dist': False,
            'sat': False,
            'IQ': False,
            'dc': False,
            'lpIQ': False,
        }
        
        try:
            # Run decoder with --help and parse supported flags
            result = subprocess.run(
                [decoder_path, '--help'],
                capture_output=True,
                text=True,
                timeout=2
            )
            help_text = result.stdout + result.stderr
            
            # Check for each capability
            caps['softin'] = '--softin' in help_text
            caps['json'] = '--json' in help_text
            caps['ID'] = '-ID' in help_text
            caps['ptu'] = '--ptu' in help_text
            caps['ptu2'] = '--ptu2' in help_text
            caps['ecc'] = '--ecc' in help_text
            caps['dist'] = '--dist' in help_text
            caps['sat'] = '--sat' in help_text
            caps['IQ'] = '--IQ' in help_text
            caps['dc'] = '--dc' in help_text
            caps['lpIQ'] = '--lpIQ' in help_text
            
            cls._decoder_caps[decoder_path] = caps
            return caps
        except Exception:
            # If probe fails, return minimal safe capabilities
            cls._decoder_caps[decoder_path] = caps
            return caps
    
    @classmethod
    def _detect_softin_support(cls, decoder_path: str) -> bool:
        """
        Detect if decoder supports --softin flag (legacy method)
        
        Args:
            decoder_path: Path to decoder binary
            
        Returns:
            True if --softin is supported
        """
        caps = cls._detect_decoder_capabilities(decoder_path)
        return caps.get('softin', False)
    
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
            decoder_path = self.resolve_decoder_path(decoder_binary) or decoder_binary
        
        self.decoder_path = decoder_path
        self.logger = logging.getLogger(f'{self.sonde_type}Decoder.{frequency/1e6:.3f}')
        
        # Detect decoder capabilities comprehensively
        self.decoder_caps = self._detect_decoder_capabilities(self.decoder_path)
        self.has_softin = self.decoder_caps.get('softin', False)
        
        # Log detected capabilities for debugging
        caps_str = ', '.join(f"{k}={v}" for k, v in self.decoder_caps.items() if v)
        if caps_str:
            self.logger.info(f"Detected decoder capabilities: {caps_str}")
        
        if not self.has_softin and self.sonde_type in ['RS41', 'DFM']:
            self.logger.warning(f"{self.DECODER_MAP.get(self.sonde_type)} does not support --softin flag. "
                              f"Using IQ mode with text PTU fallback. "
                              f"For full PTU support, install Auto_RX-compatible decoders from rs1729/RS.")
        
        self.process: Optional[subprocess.Popen] = None
        self.running = False
        self.frame_callback: Optional[Callable] = None
        self.last_frame_time: Optional[datetime] = None
        self.frame_count = 0
        self._start_time: Optional[float] = None
        self.debug_json_ptu = os.environ.get("OPENWX_JSON_PTU_DEBUG", "0").lower() in ("1", "true", "yes", "on")
        self.ptu_cache = {}  # Cache PTU data from text lines, keyed by (serial, frame_num)
        self.ptu_cache_timestamps = {}  # Track PTU data timestamps for freshness check
        self.startup_failure_count = 0  # Track immediate startup failures
        self.last_failure_time = None  # Track last failure for cooldown
        self._logged_ptu_degraded_mode = False  # One-time warning for PTU fallback mode
    
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
                # CRITICAL: unlike the DFM branch below, --ptu2 was previously added
                # unconditionally without checking whether the installed rs41mod
                # build actually recognizes it (install.sh clones rs1729/RS unpinned,
                # so this varies per install — same root cause class as the
                # dft_detect CLI-format mismatch). If unsupported, PTU silently never
                # appears (JSON or text) with no diagnostic trail. Log it explicitly.
                if not self.decoder_caps.get('ptu2', False):
                    self.logger.warning(
                        "rs41mod does not report --ptu2 support via --help — PTU "
                        "(temp/humidity/pressure) will likely be unavailable for "
                        "this decoder. Set OPENWX_JSON_PTU_DEBUG=1 to see raw JSON "
                        "keys per frame for diagnosis."
                    )
            elif self.sonde_type == 'DFM':
                # DFM: dfm09mod with IQ input mode
                # --auto: Automatic DFM subtype detection (DFM06/DFM09/DFM17) - CRITICAL for correct detection
                # Without --auto, dfm09mod may not lock on DFM06/DFM17 variants
                cmd.extend(['--auto', '-vv', '--IQ', '0.0'])
                
                # Add optional enhancement flags if supported
                if self.decoder_caps.get('ecc', False):
                    cmd.append('--ecc')
                if self.decoder_caps.get('json', False):
                    cmd.append('--json')
                if self.decoder_caps.get('dist', False):
                    cmd.append('--dist')
                if self.decoder_caps.get('ptu', False):
                    cmd.append('--ptu')
                
                # Add -ID flag only if explicitly supported
                if self.decoder_caps.get('ID', False):
                    cmd.append('-ID')
                else:
                    self.logger.info("DFM decoder does not support -ID flag, serial may be masked")
                
                # Add verbosity and input parameters
                cmd.extend(['-', '48000', '16'])
            elif self.sonde_type in ('M10', 'M20'):
                # M10/M20: m10mod/m20mod with optional enhancement flags.
                # NOTE: --dc, --ptu, --json, --lpIQ are only added when the probed
                # binary actually supports them (see EMERGENCY_FIX_v1.0.46a: older
                # decoder builds crash immediately on an unrecognized flag, producing
                # zero frames). Baseline '-v --IQ 0.0 - 48000 16' always works.
                cmd.append('-v')
                if self.decoder_caps.get('dc', False):
                    cmd.append('--dc')  # DC offset removal (helps with subcarrier)
                if self.decoder_caps.get('ptu', False):
                    cmd.append('--ptu')  # PTU sensor output
                if self.decoder_caps.get('json', False):
                    cmd.append('--json')  # JSON structured output
                cmd.extend(['--IQ', '0.0'])
                if self.decoder_caps.get('lpIQ', False):
                    cmd.append('--lpIQ')  # Low-pass filter to reduce high-frequency noise
                cmd.extend(['-', '48000', '16'])
            elif self.sonde_type == 'RS92':
                # RS92: rs92mod -v --IQ 0.0 - 48000 16
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
                self.startup_failure_count += 1
                self.last_failure_time = time.time()
                self.logger.error(f"Decoder exited immediately with code {exit_code} (failure #{self.startup_failure_count})")
                
                # Log command for debugging startup failures
                self.logger.error(f"Failed command: {' '.join(cmd)}")
                
                # Track failure for cooldown
                failure_key = (self.decoder_path, self.sonde_type)
                if failure_key not in self._decoder_failures:
                    self._decoder_failures[failure_key] = []
                self._decoder_failures[failure_key].append(time.time())
                
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
                self.startup_failure_count += 1
                self.last_failure_time = time.time()
                self.logger.error(f"Decoder crashed early with exit code {exit_code} (failure #{self.startup_failure_count})")
                self.running = False
                
                # Track failure for cooldown
                failure_key = (self.decoder_path, self.sonde_type)
                if failure_key not in self._decoder_failures:
                    self._decoder_failures[failure_key] = []
                self._decoder_failures[failure_key].append(time.time())
                
                return False
            
            self.logger.info(f"Decoder healthy after 2s startup, PID={self.process.pid}")
            
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
            'running': self.running and self.is_alive(),
            'startup_failures': self.startup_failure_count,
            'last_failure': self.last_failure_time
        }
    
    @classmethod
    def should_retry_decoder(cls, decoder_path: str, sonde_type: str, cooldown_seconds: int = 60) -> bool:
        """
        Check if decoder should be retried based on recent failure history
        
        Args:
            decoder_path: Path to decoder binary
            sonde_type: Type of radiosonde
            cooldown_seconds: Minimum seconds between retry attempts
            
        Returns:
            True if decoder can be retried, False if in cooldown
        """
        failure_key = (decoder_path, sonde_type)
        if failure_key not in cls._decoder_failures:
            return True
        
        failures = cls._decoder_failures[failure_key]
        if not failures:
            return True
        
        # Check if last failure is outside cooldown window
        last_failure = failures[-1]
        time_since_failure = time.time() - last_failure
        
        if time_since_failure < cooldown_seconds:
            return False
        
        # Clean up old failures (keep last 10)
        cls._decoder_failures[failure_key] = failures[-10:]
        return True
    
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
                        # Basic validation: ensure critical fields exist
                        if not isinstance(json_data, dict):
                            continue
                        frame = self._parse_json_frame(json_data)
                        if frame and self.frame_callback:
                            self.frame_count += 1
                            self.last_frame_time = datetime.now()
                            self.frame_callback(frame)
                    except json.JSONDecodeError as e:
                        self.logger.warning(f"Invalid JSON from decoder: {e}")
                    except Exception as e:
                        self.logger.error(f"Error processing decoder frame: {e}")
                    continue

                # Non-JSON lines: extract PTU data
                if self.debug_json_ptu:
                    self.logger.info(f"Decoder stdout (non-JSON): {line}")
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
        """Legacy PTU fallback from verbose text output, cached with serial + timestamp.
        
        RS41 text format with -vv --ptu2:
        [ 5644] (W4060809)  Mon 2026-06-08 05:06:05.997  lat: 52.89519 lon: 7.89611 alt: 24350.9  vH: 10.4 D: 294.0 vV: 6.3  T=-47.6°C RH=5.8% P=24.38hPa
        
        Caches by (serial, frame_num) to prevent cross-contamination when multiple sondes are decoded.
        """
        # Extract frame number
        frame_match = re.search(r'\[\s*(\d+)\]', line)
        if not frame_match:
            return
        frame_num = int(frame_match.group(1))
        
        # Extract sonde serial (in parentheses)
        serial_match = re.search(r'\(([A-Z0-9]+)\)', line)
        if not serial_match:
            return  # Need serial for proper cache keying
        sonde_serial = serial_match.group(1)
        
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
            # Store with current timestamp, keyed by (serial, frame) to prevent cross-contamination
            now = time.time()
            cache_key = (sonde_serial, frame_num)
            self.ptu_cache[cache_key] = ptu
            self.ptu_cache_timestamps[cache_key] = now
            
            # Cleanup old cache entries aggressively to prevent memory growth
            if len(self.ptu_cache) > 100:  # Higher limit for multi-sonde scenarios
                # Remove entries older than 10 seconds
                cutoff_time = now - 10.0
                expired_keys = [k for k, t in self.ptu_cache_timestamps.items() if t < cutoff_time]
                for k in expired_keys:
                    self.ptu_cache.pop(k, None)
                    self.ptu_cache_timestamps.pop(k, None)
                    
            if self.debug_json_ptu:
                self.logger.info(f"Cached fallback PTU for {sonde_serial} frame {frame_num}: {ptu}")
    
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

            # Normalize sonde type (handle hex values from decoder output)
            sonde_type = str(json_data.get('type') or self.sonde_type).strip()
            if sonde_type.startswith('0x'):
                # Decoder returned hex type code - use configured sonde_type
                sonde_type = self.sonde_type
            
            # Strip any existing prefixes from sonde_id (M10-, M20-, DFM-, iMet-, etc.)
            # and keep only the actual serial number for all output streams
            for prefix in ['M10-', 'M20-', 'DFM-', 'iMet-', 'IMET-', 'LMS6-', 'MRZ-']:
                if sonde_id.startswith(prefix):
                    sonde_id = sonde_id[len(prefix):]
                    break
            
            # For DFM: also strip leading 'D' if the rest is numeric
            if sonde_type == 'DFM' and sonde_id.startswith('D') and sonde_id[1:].isdigit():
                sonde_id = sonde_id[1:]

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
            
            # Validate critical coordinate bounds
            if not (-90 <= frame['lat'] <= 90):
                self.logger.warning(f"Invalid latitude {frame['lat']} for {sonde_id}, skipping frame")
                return None
            if not (-180 <= frame['lon'] <= 180):
                self.logger.warning(f"Invalid longitude {frame['lon']} for {sonde_id}, skipping frame")
                return None
            if frame['alt'] < -1000 or frame['alt'] > 50000:
                self.logger.warning(f"Invalid altitude {frame['alt']}m for {sonde_id}, skipping frame")
                return None

            # Optional fields – only include when present in this JSON frame
            # Parse DFM subtype format: "0xC:DFM17" → subtype="DFM17", dfmcode="0xC"
            dfm_subtype_parsed = False
            if self.sonde_type == 'DFM' and json_data.get('subtype'):
                raw_subtype = str(json_data.get('subtype'))
                if ':' in raw_subtype:
                    # Split "0xC:DFM17" format
                    parts = raw_subtype.split(':', 1)
                    frame['dfmcode'] = parts[0]  # "0xC"
                    frame['subtype'] = parts[1]  # "DFM17"
                    dfm_subtype_parsed = True
                else:
                    frame['subtype'] = raw_subtype
                    dfm_subtype_parsed = True
            
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
                # Skip subtype if already parsed for DFM
                if dst == 'subtype' and dfm_subtype_parsed:
                    continue
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

            # Track PTU source for quality analysis
            ptu_source = 'none'
            has_json_ptu = all(frame.get(k) for k in ('temp', 'humidity', 'pressure'))
            
            if has_json_ptu:
                ptu_source = 'json'
            else:
                # Serial-aware PTU fallback: match by (serial, frame) proximity with freshness check
                current_frame = frame['frame_number']
                current_serial = frame['sonde_id']
                best_match = None
                best_distance = 999999
                now = time.time()
                freshness_window = 5.0  # 5 second expiry for stale data
                
                for (cached_serial, cached_frame), timestamp in self.ptu_cache_timestamps.items():
                    # Only match same serial to prevent cross-contamination
                    if cached_serial != current_serial:
                        continue
                    
                    # Check freshness (within 5 seconds)
                    age = now - timestamp
                    if age > freshness_window:
                        continue
                    
                    # Look for recent PTU data within +/- 3 frames
                    frame_distance = abs(cached_frame - current_frame)
                    if frame_distance <= 3 and frame_distance < best_distance:
                        best_match = (cached_serial, cached_frame)
                        best_distance = frame_distance
                
                if best_match is not None:
                    cached = self.ptu_cache[best_match]
                    frame.setdefault('temp', cached.get('temp'))
                    frame.setdefault('humidity', cached.get('humidity'))
                    frame.setdefault('pressure', cached.get('pressure'))
                    
                    # Update source if any PTU data was merged
                    if any(frame.get(k) for k in ('temp', 'humidity', 'pressure')):
                        ptu_source = 'text_fallback'
                        
                        # Log PTU degraded mode warning once per decoder session
                        if not self._logged_ptu_degraded_mode:
                            self.logger.warning(
                                f"PTU degraded mode: {self.sonde_type} decoder lacks --softin support or "
                                f"JSON PTU fields. Using text fallback (frame proximity + freshness check). "
                                f"For better PTU reliability, install Auto_RX-compatible rs1729 decoders."
                            )
                            self._logged_ptu_degraded_mode = True
                    
                    if self.debug_json_ptu:
                        self.logger.info(
                            f"[PTU Merged] {current_serial} frame {current_frame} matched with text frame "
                            f"{best_match[1]} (distance={best_distance}): T={frame.get('temp')}°C "
                            f"RH={frame.get('humidity')}% P={frame.get('pressure')}hPa"
                        )
            
            # Add PTU source tag to frame for quality tracking
            frame['ptu_source'] = ptu_source

            if self.debug_json_ptu:
                ptu_keys = {k: json_data.get(k) for k in ('temp', 'tempc', 'temperature', 'T', 'humidity', 'humidityrh', 'rh', 'RH', 'pressure', 'pressurehpa', 'pres', 'P') if k in json_data}
                self.logger.info(f"JSON frame keys={sorted(json_data.keys())}")
                self.logger.info(f"JSON PTU candidate fields for {sonde_id}: {ptu_keys}")
                final_ptu = {k: frame.get(k) for k in ('temp', 'humidity', 'pressure') if frame.get(k) is not None}
                self.logger.info(f"Final frame PTU for {sonde_id} frame {frame['frame_number']} (source={ptu_source}): {final_ptu}")

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
