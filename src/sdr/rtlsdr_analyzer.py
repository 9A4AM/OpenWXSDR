"""
# =============================================================================
#  OpenWX -- Open Weather Radiosonde Telemetry System
# =============================================================================
#
#  File   : rtlsdr_analyzer.py
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
#  RTL-SDR spectrum analyzer and signal detector for OpenWX.
#
#  Provides SpectrumAnalyzer, which captures IQ samples via pyrtlsdr,
#  computes a Welch power spectral density estimate, and detects candidate
#  radiosonde signals as peaks above a configurable SNR threshold.
#
#  Detection pipeline:
#    pyrtlsdr (IQ capture) → scipy Welch PSD → peak detection
#      → bandwidth filter (2–30 kHz) → DetectedSignal list
#
#  The analyzer supports pause/resume to yield the USB device to rtl_fm
#  during active decoding. Continuous scanning runs in a background thread.
#
#  Decoder backend : rs1729 (RS41, DFM09, M10, iMet-C, ...)
#  Hardware        : RTL-SDR (RTL2832U-based receivers)
#
# =============================================================================
"""

import numpy as np
import logging
import time
import threading
from typing import List, Tuple, Optional, Dict
from dataclasses import dataclass
from scipy import signal as scipy_signal

try:
    from rtlsdr import RtlSdr
    RTLSDR_AVAILABLE = True
except ImportError:
    RTLSDR_AVAILABLE = False
    logging.warning("pyrtlsdr not available - RTL-SDR support disabled")


@dataclass
class DetectedSignal:
    """Represents a detected radiosonde signal"""
    frequency: float
    strength: float  # SNR in dB
    bandwidth: float
    timestamp: float
    

class SpectrumAnalyzer:
    """Analyzes spectrum and detects radiosonde signals"""
    
    def __init__(self, config: dict, device_config: dict = None):
        """
        Initialize spectrum analyzer
        
        Args:
            config: Full application config
            device_config: Specific device configuration (from rtlsdr.devices list)
                          If None, uses first device from config
        """
        self.config = config
        self.logger = logging.getLogger('SpectrumAnalyzer')
        self.sdr = None
        self.running = False
        self.detected_signals: List[DetectedSignal] = []
        self.lock = threading.Lock()
        
        # Get device configuration
        if device_config is None:
            # Backward compatibility: use first device or old format
            rtlsdr_config = config['sdr']['rtlsdr']
            if 'devices' in rtlsdr_config:
                device_config = rtlsdr_config['devices'][0]
            else:
                # Old config format
                device_config = rtlsdr_config
        
        self.device_config = device_config
        self.device_serial = device_config.get('serial', '0')
        
        # Configuration
        self.center_freq = device_config['center_freq']
        self.sample_rate = device_config['sample_rate']
        self.fft_size = config['detection']['fft_size']
        self.detection_threshold = config['detection']['detection_threshold']
        self.scan_interval = config['receivers']['scan_interval']
        
    def initialize(self) -> bool:
        """Initialize RTL-SDR device"""
        if not RTLSDR_AVAILABLE:
            self.logger.error("RTL-SDR support not available")
            return False
            
        try:
            # Support both serial number and device index.
            # If serial is all digits treat as a raw device index;
            # otherwise resolve the serial to an index via librtlsdr.
            if self.device_serial.isdigit():
                device_index = int(self.device_serial)
                self.sdr = RtlSdr(device_index)
                self.logger.info(f"Opening RTL-SDR by index: {device_index}")
            else:
                device_index = RtlSdr.get_device_index_by_serial(self.device_serial)
                self.sdr = RtlSdr(device_index)
                self.logger.info(f"Opening RTL-SDR serial '{self.device_serial}' → index {device_index}")
            
            # Configure SDR
            self.sdr.sample_rate = self.sample_rate
            self.sdr.center_freq = self.center_freq
            
            gain = self.device_config.get('gain', 0)
            if gain == 0:
                self.sdr.gain = 'auto'
            else:
                self.sdr.gain = gain
                
            ppm = self.device_config.get('ppm_error', 0)
            if ppm != 0:
                self.sdr.freq_correction = ppm
            
            self.logger.info(f"RTL-SDR initialized: {self.center_freq/1e6:.3f} MHz, "
                           f"{self.sample_rate/1e6:.2f} MSPS, gain={self.sdr.gain}, serial={self.device_serial}")
            return True
            
        except Exception as e:
            self.logger.error(f"Failed to initialize RTL-SDR: {e}")
            return False
    
    def close(self):
        """Close RTL-SDR device"""
        if self.sdr:
            self.sdr.close()
            self.sdr = None
            self.logger.info("RTL-SDR closed")
    
    def capture_spectrum(self, num_samples: int = None) -> Tuple[np.ndarray, np.ndarray]:
        """
        Capture and analyze spectrum
        Returns (frequencies, power_db)
        """
        if not self.sdr:
            raise RuntimeError("SDR not initialized")
        
        if num_samples is None:
            num_samples = self.fft_size * 16
        
        # Read samples
        samples = self.sdr.read_samples(num_samples)
        
        # Compute power spectral density
        freqs, psd = scipy_signal.welch(
            samples,
            fs=self.sample_rate,
            nperseg=self.fft_size,
            scaling='density',
            return_onesided=False
        )
        
        # Convert to dB and shift to match frequency ordering
        power_db = 10 * np.log10(psd)
        
        # Shift FFT output (fftshift equivalent)
        power_db = np.fft.fftshift(power_db)
        freqs = np.fft.fftshift(freqs) + self.center_freq
        
        return freqs, power_db
    
    def detect_signals(self, freqs: np.ndarray, power_db: np.ndarray) -> List[DetectedSignal]:
        """
        Detect peaks in spectrum that could be radiosonde signals
        """
        # Estimate noise floor (median of lower 30% of values)
        sorted_power = np.sort(power_db)
        noise_floor = np.median(sorted_power[:len(sorted_power)//3])
        
        # Find peaks above threshold
        threshold = noise_floor + self.detection_threshold
        
        # Use scipy peak detection
        peaks, properties = scipy_signal.find_peaks(
            power_db,
            height=threshold,
            distance=self.fft_size // 20,  # Minimum distance between peaks
            width=5  # Minimum width
        )
        
        detected = []
        for peak_idx in peaks:
            freq = freqs[peak_idx]
            strength = power_db[peak_idx] - noise_floor
            
            # Estimate bandwidth (3dB bandwidth)
            half_power = power_db[peak_idx] - 3
            left_idx = peak_idx
            right_idx = peak_idx
            
            while left_idx > 0 and power_db[left_idx] > half_power:
                left_idx -= 1
            while right_idx < len(power_db) - 1 and power_db[right_idx] > half_power:
                right_idx += 1
            
            bandwidth = abs(freqs[right_idx] - freqs[left_idx])
            
            # Filter by radiosonde typical bandwidth (4-20 kHz)
            if 2000 < bandwidth < 30000:
                detected.append(DetectedSignal(
                    frequency=freq,
                    strength=strength,
                    bandwidth=bandwidth,
                    timestamp=time.time()
                ))
        
        return detected
    
    def filter_signals_in_ranges(self, signals: List[DetectedSignal]) -> List[DetectedSignal]:
        """Filter signals to only those in configured frequency ranges"""
        freq_ranges = self.config['detection']['freq_ranges']
        filtered = []
        
        for sig in signals:
            for freq_min, freq_max in freq_ranges:
                if freq_min <= sig.frequency <= freq_max:
                    filtered.append(sig)
                    break
        
        return filtered
    
    def scan_spectrum(self):
        """Perform one spectrum scan and update detected signals"""
        try:
            freqs, power_db = self.capture_spectrum()
            detected = self.detect_signals(freqs, power_db)
            detected = self.filter_signals_in_ranges(detected)
            
            # Update detected signals list
            with self.lock:
                # Remove old detections (older than 30 seconds)
                current_time = time.time()
                self.detected_signals = [
                    s for s in self.detected_signals
                    if current_time - s.timestamp < 30
                ]
                
                # Add new detections (avoid duplicates within 10 kHz)
                for new_sig in detected:
                    duplicate = False
                    for existing_sig in self.detected_signals:
                        if abs(new_sig.frequency - existing_sig.frequency) < 10000:
                            # Update existing signal
                            existing_sig.strength = new_sig.strength
                            existing_sig.timestamp = new_sig.timestamp
                            duplicate = True
                            break
                    
                    if not duplicate:
                        self.detected_signals.append(new_sig)
                        self.logger.info(
                            f"New signal detected: {new_sig.frequency/1e6:.4f} MHz, "
                            f"SNR: {new_sig.strength:.1f} dB, "
                            f"BW: {new_sig.bandwidth/1e3:.1f} kHz"
                        )
            
        except Exception as e:
            self.logger.error(f"Error during spectrum scan: {e}", exc_info=True)
    
    def get_detected_signals(self) -> List[DetectedSignal]:
        """Get current list of detected signals"""
        with self.lock:
            return self.detected_signals.copy()
    
    def start_scanning(self):
        """Start continuous spectrum scanning in background thread"""
        if self.running:
            self.logger.warning("Spectrum scanning already running")
            return
        
        self.running = True
        self.scan_thread = threading.Thread(target=self._scan_loop, daemon=True)
        self.scan_thread.start()
        self.logger.info("Spectrum scanning started")
    
    def stop_scanning(self):
        """Stop spectrum scanning"""
        self.running = False
        if hasattr(self, 'scan_thread'):
            self.scan_thread.join(timeout=5)
        self.logger.info("Spectrum scanning stopped")
    
    def pause(self):
        """
        Pause scanning and close RTL-SDR device
        This allows other processes (like rtl_fm) to access the device
        """
        if self.sdr:
            self.logger.info("Pausing spectrum analyzer - closing RTL-SDR device")
            try:
                self.sdr.close()
                self.sdr = None
            except Exception as e:
                self.logger.warning(f"Error closing RTL-SDR during pause: {e}")
    
    def resume(self):
        """
        Resume scanning by reopening RTL-SDR device
        """
        if not self.sdr:
            self.logger.info("Resuming spectrum analyzer - reopening RTL-SDR device")
            try:
                # Resolve serial to index the same way initialize() does
                if self.device_serial.isdigit():
                    device_index = int(self.device_serial)
                else:
                    device_index = RtlSdr.get_device_index_by_serial(self.device_serial)
                self.sdr = RtlSdr(device_index)
                
                # Reconfigure SDR from stored device config
                self.sdr.sample_rate = self.sample_rate
                self.sdr.center_freq = self.center_freq
                
                gain = self.device_config.get('gain', 0)
                if gain == 0:
                    self.sdr.gain = 'auto'
                else:
                    self.sdr.gain = gain
                    
                ppm = self.device_config.get('ppm_error', 0)
                if ppm != 0:
                    self.sdr.freq_correction = ppm
                
                self.logger.info("RTL-SDR reopened successfully")
            except Exception as e:
                self.logger.error(f"Failed to reopen RTL-SDR: {e}")
    
    def _scan_loop(self):
        """Background scanning loop"""
        while self.running:
            try:
                # Skip scanning if SDR is paused (closed)
                if self.sdr is not None:
                    self.scan_spectrum()
                time.sleep(self.scan_interval)
            except Exception as e:
                self.logger.error(f"Error in scan loop: {e}", exc_info=True)
                time.sleep(5)  # Wait before retrying
