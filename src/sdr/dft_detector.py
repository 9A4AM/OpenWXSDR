"""
# =============================================================================
#  OpenWX -- Open Weather Radiosonde Telemetry System
# =============================================================================
#
#  File   : dft_detector.py
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
#  DFT-based radiosonde type detector for OpenWX.
#
#  Uses the dft_detect tool from the rs1729/RS repository to perform
#  correlation-based sonde identification against known sonde signatures.
#  This approach is significantly more accurate than bandwidth-based
#  detection and can reliably distinguish between RS41, RS92, DFM, M10
#  and iMet sondes with type-specific correlation thresholds.
#
#  Pipeline:
#    rtl_fm (FM demodulation, 48 kHz) → temp .raw file → dft_detect
#
#  Supported sonde types : RS41, RS92, DFM, M10, M20, iMet, LMS6
#  External dependency   : dft_detect (rs1729/RS, github.com/rs1729/RS)
#
# =============================================================================
"""

import subprocess
import logging
import tempfile
import os
import re
import time
from typing import Optional, Dict, Tuple
from dataclasses import dataclass


@dataclass
class CorrelationResult:
    """Correlation detection result from dft_detect"""
    sonde_type: str
    correlation: float
    frequency: float
    bandwidth: float


class DftDetector:
    """
    Sonde type detector using dft_detect correlation analysis.
    
    This is much more accurate than bandwidth-based detection because it:
    - Compares signals against known sonde signatures
    - Uses correlation thresholds optimized to prevent false positives
    - Can distinguish between sondes with similar bandwidths
    
    dft_detect from rs1729/RS repository (https://github.com/rs1729/RS/tree/master/scan)
    
    Correlation thresholds:
    - RS41: ~0.53
    - RS92: ~0.54
    - DFM: ~0.62
    - M10: ~0.75
    """
    
    # Correlation thresholds from radiosonde_auto_rx
    THRESHOLDS = {
        'RS41': 0.53,
        'RS92': 0.54,
        'DFM': 0.62,
        'M10': 0.75,
        'iMet': 0.65  # Estimated threshold for iMet
    }
    
    def __init__(self, dft_detect_path: str = 'dft_detect', sample_duration: float = 5.0):
        """
        Initialize DFT detector
        
        Args:
            dft_detect_path: Path to dft_detect binary (default: 'dft_detect' in PATH)
            sample_duration: Duration of IQ capture in seconds (default: 5.0s)
        """
        self.dft_detect_path = dft_detect_path
        self.sample_duration = sample_duration
        self.logger = logging.getLogger('DftDetector')
        
        # Check if dft_detect is available
        self.available = self._check_availability()
        if not self.available:
            self.logger.warning(
                "dft_detect not found! Falling back to bandwidth-based detection. "
                "Run install.sh to build dft_detect from rs1729/RS for accurate sonde identification."
            )
    
    def _check_availability(self) -> bool:
        """Check if dft_detect is available"""
        try:
            result = subprocess.run(
                [self.dft_detect_path, '--help'],
                capture_output=True,
                timeout=2.0
            )
            return result.returncode in [0, 1]  # Some versions return 1 for --help
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False
    
    def detect_sonde_type(
        self, 
        frequency: float, 
        device_serial: str = "0",
        sample_rate: int = 48000,
        bandwidth: Optional[float] = None
    ) -> Optional[str]:
        """
        Detect sonde type using correlation analysis.
        
        Process:
        1. Capture short burst of IQ samples at detected frequency
        2. Run dft_detect to correlate against known sonde signatures
        3. Return sonde type with highest correlation above threshold
        
        Args:
            frequency: Center frequency in Hz
            device_serial: RTL-SDR device serial number or index
            sample_rate: Sample rate in Hz (default: 48000)
            bandwidth: Optional bandwidth hint in Hz (for fallback)
            
        Returns:
            Sonde type string (e.g., 'RS41', 'DFM', 'RS92') or None if no match
        """
        if not self.available:
            self.logger.debug("dft_detect not available, skipping correlation detection")
            return None
        
        self.logger.info(f"Running correlation analysis at {frequency/1e6:.4f} MHz")
        
        # Capture FM-demodulated audio (rtl_fm -M fm output is the correct input for dft_detect)
        iq_file = self._capture_fm_audio(frequency, device_serial, sample_rate)
        if not iq_file:
            return None
        
        try:
            # Run dft_detect on captured samples
            results = self._run_dft_detect(iq_file, sample_rate)
            
            if results:
                # Find best match above threshold
                best_match = self._select_best_match(results)
                if best_match:
                    self.logger.info(
                        f"Detected {best_match.sonde_type} with correlation {best_match.correlation:.3f} "
                        f"(threshold: {self.THRESHOLDS.get(best_match.sonde_type, 0.5):.3f})"
                    )
                    return best_match.sonde_type
                else:
                    self.logger.info("No sonde type exceeded correlation threshold")
            else:
                self.logger.warning("dft_detect returned no results")
            
            return None
            
        finally:
            # Clean up temporary file
            try:
                os.unlink(iq_file)
            except:
                pass
    
    def _capture_fm_audio(
        self,
        frequency: float,
        device_serial: str,
        sample_rate: int,
        retry_count: int = 0
    ) -> Optional[str]:
        """
        Capture FM-demodulated audio using rtl_fm.

        This matches how radiosonde_auto_rx uses dft_detect:
        - rtl_fm handles internal decimation so 48 kHz output works reliably
        - FM-demodulated audio is the correct input format for dft_detect
        - rtl_fm accepts serial numbers directly (-d serial), no index lookup needed

        Args:
            frequency: Center frequency in Hz
            device_serial: RTL-SDR device serial number (used directly with rtl_fm -d)
            sample_rate: Output sample rate in Hz (48000 recommended)
            retry_count: Current retry attempt (for PLL lock failures)

        Returns:
            Path to temporary file containing FM audio samples, or None on failure
        """
        # CRITICAL: Add USB settling delay before opening device
        # This prevents "[R82XX] PLL not locked!" errors after scanner closes
        if retry_count == 0:
            self.logger.debug(f"Waiting 2s for USB device {device_serial} to settle before rtl_fm...")
            time.sleep(2.0)
        
        fd, audio_file = tempfile.mkstemp(suffix='.raw', prefix='openwxsdr_dft_')
        os.close(fd)

        # rtl_fm parameters:
        # -d serial  : device by serial number (no index lookup needed)
        # -M fm      : FM demodulation — produces signed 16-bit audio
        # -s rate    : output sample rate (48 kHz works with rtl_fm internal decimation)
        # -f freq    : tune frequency
        # -g gain    : gain (40 dB typical)
        # -E dc      : DC offset removal
        # -         : write to stdout (captured to file)
        cmd = [
            'rtl_fm',
            '-d', device_serial,
            '-M', 'fm',
            '-s', str(sample_rate),
            '-f', str(int(frequency)),
            '-g', '40',
            '-E', 'dc',
            '-'
        ]

        self.logger.debug(f"Capturing FM audio from device {device_serial}: {' '.join(cmd)}")

        proc = None
        try:
            with open(audio_file, 'wb') as outfile:
                proc = subprocess.Popen(
                    cmd,
                    stdout=outfile,
                    stderr=subprocess.PIPE
                )
                time.sleep(self.sample_duration)
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()

            # Check stderr for PLL lock failures
            stderr_out = b''
            if proc and proc.stderr:
                try:
                    stderr_out = proc.stderr.read()
                except Exception:
                    pass
            
            stderr_text = stderr_out.decode('utf-8', errors='ignore')
            
            # Detect PLL lock failure
            if 'PLL not locked' in stderr_text or 'usb_claim_interface' in stderr_text:
                if retry_count < 2:  # Allow up to 2 retries
                    self.logger.warning(
                        f"RTL-SDR PLL lock failure (attempt {retry_count + 1}/3), "
                        f"retrying after 3s cooldown..."
                    )
                    os.unlink(audio_file)
                    time.sleep(3.0)  # Longer cooldown for hardware recovery
                    return self._capture_fm_audio(frequency, device_serial, sample_rate, retry_count + 1)
                else:
                    self.logger.error(
                        f"RTL-SDR PLL lock failure after {retry_count + 1} attempts, giving up"
                    )
                    os.unlink(audio_file)
                    return None

            # Verify file has usable data
            file_size = os.path.getsize(audio_file) if os.path.exists(audio_file) else 0
            if file_size < 1000:
                self.logger.error(
                    f"FM capture file too small ({file_size} bytes): "
                    f"{stderr_text[:200]}"
                )
                os.unlink(audio_file)
                return None

            self.logger.debug(f"Captured {file_size} bytes of FM audio")
            return audio_file

        except Exception as e:
            self.logger.error(f"Failed to capture FM audio: {e}")
            if proc:
                try:
                    proc.kill()
                    proc.wait()
                except Exception:
                    pass
            try:
                os.unlink(audio_file)
            except Exception:
                pass
            return None
    
    def _run_dft_detect(self, iq_file: str, sample_rate: int) -> Dict[str, float]:
        """
        Run dft_detect on captured IQ samples.
        
        Args:
            iq_file: Path to IQ sample file
            sample_rate: Sample rate in Hz
            
        Returns:
            Dictionary of {sonde_type: correlation_score}
        """
        try:
            # dft_detect command:
            # -s: sample rate
            # -b: bandwidth (20 kHz standard, or 64 kHz for wideband)
            # input: IQ file
            cmd = [
                self.dft_detect_path,
                '-s', str(sample_rate),
                '-b', '20000',  # 20 kHz bandwidth (standard detection)
                iq_file
            ]
            
            self.logger.debug(f"Running dft_detect: {' '.join(cmd)}")
            
            result = subprocess.run(
                cmd,
                capture_output=True,
                timeout=10.0,
                text=True
            )
            
            if result.returncode != 0:
                # Exit code 206 typically means corrupted/insufficient input data
                # Often caused by PLL lock failures or USB issues in rtl_fm capture
                if result.returncode == 206:
                    self.logger.warning(
                        f"dft_detect exit code 206 (corrupted input data) - "
                        f"likely RTL-SDR USB/PLL issue"
                    )
                else:
                    self.logger.warning(f"dft_detect returned non-zero exit code: {result.returncode}")
                
                # Log stderr for debugging
                if result.stderr:
                    self.logger.debug(f"dft_detect stderr: {result.stderr[:200]}")
            
            # Parse output for correlation results
            # Expected format: "RS41: 0.653" or "DFM: 0.701"
            output = result.stdout + result.stderr
            results = self._parse_dft_output(output)
            
            if results:
                self.logger.debug(f"Correlation results: {results}")
            
            return results
            
        except subprocess.TimeoutExpired:
            self.logger.error("dft_detect timed out")
            return {}
        except Exception as e:
            self.logger.error(f"Failed to run dft_detect: {e}")
            return {}
    
    def _parse_dft_output(self, output: str) -> Dict[str, float]:
        """
        Parse dft_detect output for correlation scores.
        
        Example output:
            RS41: 0.653
            RS92: 0.412
            DFM: 0.701
            M10: 0.302
        
        Args:
            output: dft_detect stdout/stderr
            
        Returns:
            Dictionary of {sonde_type: correlation_score}
        """
        results = {}
        
        # Pattern: "TYPE: 0.XXX"
        pattern = r'(RS41|RS92|DFM|M10|M20|iMet|LMS6):\s*(\d+\.\d+)'
        
        for match in re.finditer(pattern, output):
            sonde_type = match.group(1)
            correlation = float(match.group(2))
            results[sonde_type] = correlation
        
        return results
    
    def _select_best_match(self, results: Dict[str, float]) -> Optional[CorrelationResult]:
        """
        Select best sonde type match from correlation results.
        
        Rules:
        1. Correlation must exceed type-specific threshold
        2. Select type with highest correlation among matches
        3. RS41 and DFM are most common, prefer these if close
        
        Args:
            results: Dictionary of {sonde_type: correlation_score}
            
        Returns:
            CorrelationResult for best match, or None if no match
        """
        # Filter by threshold
        matches = {}
        for sonde_type, correlation in results.items():
            threshold = self.THRESHOLDS.get(sonde_type, 0.6)
            if correlation >= threshold:
                matches[sonde_type] = correlation
        
        if not matches:
            return None
        
        # Find highest correlation
        best_type = max(matches.items(), key=lambda x: x[1])
        
        return CorrelationResult(
            sonde_type=best_type[0],
            correlation=best_type[1],
            frequency=0.0,  # Not used in this context
            bandwidth=0.0   # Not used in this context
        )
