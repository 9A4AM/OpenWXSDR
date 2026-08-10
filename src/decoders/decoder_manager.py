"""
# =============================================================================
#  OpenWX -- Open Weather Radiosonde Telemetry System
# =============================================================================
#
#  File   : decoder_manager.py
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
#  Decoder orchestration layer for the OpenWX RTL-SDR and KA9Q pipelines.
#
#  DecoderManager coordinates multiple concurrent rs1729 decoder instances,
#  one per detected signal. It reacts to signal lists from the spectrum
#  analyzer, starts and stops RS1729Decoder processes dynamically, and
#  manages the MultiChannelAudioPipeline (rtl_fm) that feeds them raw IQ.
#
#  Sonde type identification pipeline:
#    1. DFT correlation analysis via dft_detect (preferred, most accurate)
#    2. Bandwidth-based heuristic fallback (RS41 / DFM / M10 / M20 / iMet)
#
#  Key responsibilities:
#    - Multi-device RTL-SDR assignment and capacity management
#    - Spectrum analyzer pause/resume around active decoding sessions
#    - Frequency blacklist enforcement (configurable in config.yaml)
#    - Decoder health monitoring: idle timeout, dead-process detection
#    - Manual decoder entry for known frequencies and sonde types
#    - Frame routing via telemetry callback to output plugins
#
# =============================================================================
"""

import logging
import threading
import time
from typing import Dict, List, Optional, Callable, Tuple
from dataclasses import dataclass
from datetime import datetime

from .rs1729_decoder import RS1729Decoder
from .models import SondeTelemetry, SondePosition, SondeVelocity, SondeEnvironment
from ..sdr.rtlsdr_analyzer import DetectedSignal
from ..sdr.audio_pipeline import MultiChannelAudioPipeline
from ..sdr.dft_detector import DftDetector


@dataclass
class ActiveDecoder:
    """Represents an active decoder instance"""
    decoder: RS1729Decoder
    signal: DetectedSignal
    start_time: float
    last_update: float
    audio_pipeline: 'AudioPipeline'  # Reference to audio pipeline
    device_serial: str  # Which RTL-SDR device this decoder is using


class DecoderManager:
    """
    Manages multiple decoder processes
    Automatically starts/stops decoders based on detected signals
    """
    
    def __init__(self, config: dict, telemetry_callback: Callable[[SondeTelemetry], None], 
                 spectrum_analyzer=None, ka9q_receiver=None):
        self.config = config
        self.telemetry_callback = telemetry_callback
        self.spectrum_analyzer = spectrum_analyzer  # Reference to pause/resume during decoding
        self.ka9q_receiver = ka9q_receiver  # Reference to KA9Q receiver for status queries
        self.logger = logging.getLogger('DecoderManager')
        
        self.active_decoders: Dict[float, ActiveDecoder] = {}
        self.lock = threading.Lock()
        self.running = False
        
        self.max_concurrent = config['receivers']['max_concurrent']
        
        # Get frequency blacklist (in MHz, convert to Hz)
        blacklist_mhz = config.get('detection', {}).get('frequency_blacklist', [])
        self.frequency_blacklist = [freq * 1e6 for freq in blacklist_mhz]  # Convert MHz to Hz
        if self.frequency_blacklist:
            self.logger.info(f"Frequency blacklist loaded: {[f'{f/1e6:.3f} MHz' for f in self.frequency_blacklist]}")
        
        # RS41 decoders ALWAYS use 48 kHz (hardcoded, not from config)
        # The config sample_rate is for RTL-SDR spectrum analyzer only
        RS41_SAMPLE_RATE = 48000
        
        # Get device configurations based on SDR type
        sdr_type = config.get('sdr', {}).get('type', 'rtlsdr')
        device_configs = []
        
        if sdr_type == 'rtlsdr':
            # Get RTL-SDR device configurations
            rtlsdr_config = config.get('sdr', {}).get('rtlsdr', {})
            
            # Support both new (devices list) and old (single device) config formats
            if 'devices' in rtlsdr_config:
                device_configs = rtlsdr_config['devices']
                self.logger.info(f"Loaded {len(device_configs)} RTL-SDR device configuration(s)")
            else:
                # Backward compatibility: create single device config from old format
                device_configs = [{
                    'serial': str(rtlsdr_config.get('device_index', 0)),
                    'gain': rtlsdr_config.get('gain', 0),
                    'ppm_error': rtlsdr_config.get('ppm_error', 0)
                }]
                self.logger.info("Using legacy single-device configuration")
        
        elif sdr_type == 'ka9q':
            # KA9Q mode: create virtual device config for decoder manager
            # No physical RTL-SDR devices, but decoder_manager needs at least one entry
            device_configs = [{
                'serial': 'ka9q-radio',
                'gain': 0,
                'ppm_error': 0
            }]
            self.logger.info("KA9Q mode: using virtual device configuration")
        
        else:
            # Other SDR types (flux242, airspy) don't use decoder_manager device configs
            self.logger.info(f"SDR type '{sdr_type}': skipping device configuration")
        
        self.device_configs = device_configs
        
        # Store first device serial for manual decoder (always uses first device)
        self.first_device_serial = device_configs[0].get('serial', '0') if device_configs else '0'
        
        # Track active decoders by device for spectrum analyzer pause/resume logic
        self.decoders_on_first_device = 0  # Counter for decoders using first device
        
        # Initialize DFT-based sonde type detector (fallback to bandwidth-based if not available)
        detection_config = config.get('detection', {})
        use_dft_detect = detection_config.get('use_dft_detect', True)  # Enabled by default
        dft_detect_path = detection_config.get('dft_detect_path', 'dft_detect')
        dft_sample_duration = detection_config.get('dft_sample_duration', 5.0)
        
        if use_dft_detect:
            self.dft_detector = DftDetector(
                dft_detect_path=dft_detect_path,
                sample_duration=dft_sample_duration
            )
            if self.dft_detector.available:
                self.logger.info("DFT-based sonde detection enabled (using correlation analysis)")
            else:
                self.logger.warning("DFT detection unavailable - using bandwidth-based fallback")
        else:
            self.dft_detector = None
            self.logger.info("DFT detection disabled - using bandwidth-based detection")
        
        # Create audio pipeline manager with device configurations
        self.audio_manager = MultiChannelAudioPipeline(
            max_channels=self.max_concurrent,
            sample_rate=RS41_SAMPLE_RATE,
            device_configs=device_configs
        )
    
    def start(self):
        """Start decoder manager"""
        self.running = True
        self.management_thread = threading.Thread(target=self._management_loop, daemon=True)
        self.management_thread.start()
        self.logger.info("Decoder manager started")
    
    def stop(self):
        """Stop all decoders and manager"""
        self.running = False
        
        with self.lock:
            for freq, active in list(self.active_decoders.items()):
                active.decoder.stop()
                del self.active_decoders[freq]
        
        # Stop all audio pipelines
        self.audio_manager.stop_all()
        
        if hasattr(self, 'management_thread'):
            self.management_thread.join(timeout=5)
        
        self.logger.info("Decoder manager stopped")
    
    def start_manual_decoder(self, frequency: float, sonde_type: str) -> bool:
        """
        Manually start a decoder for a specific frequency and sonde type.
        Stops all existing decoders first to avoid RTL-SDR device conflicts.
        
        Args:
            frequency: Frequency in Hz
            sonde_type: Type of sonde (RS41, DFM, M10, etc.)
            
        Returns:
            True if decoder started successfully, False otherwise
        """
        # Stop all existing decoders first (manual decoder replaces auto-detected ones)
        self.logger.info(f"Manual decoder requested - stopping all existing decoders first")
        with self.lock:
            decoders_to_stop = list(self.active_decoders.items())
        
        # Stop decoders outside the lock
        for freq, active in decoders_to_stop:
            self.logger.info(f"Stopping decoder for {freq/1e6:.3f} MHz")
            active.decoder.stop()
        
        # Clean up and remove from tracking
        with self.lock:
            for freq, _ in decoders_to_stop:
                if freq in self.active_decoders:
                    del self.active_decoders[freq]
        
        # Stop all audio pipelines
        self.audio_manager.stop_all()
        self.logger.info("All existing decoders stopped")
        
        # Do expensive operations without holding lock
        self.logger.info(f"Starting manual {sonde_type} decoder for {frequency/1e6:.3f} MHz")
        
        try:
            # Pause spectrum analyzer (always needed since we stopped all decoders)
            if self.spectrum_analyzer:
                self.logger.info("Pausing spectrum analyzer to allow decoder access to RTL-SDR")
                self.spectrum_analyzer.pause()
            
            # Clean up any dead pipelines
            self.audio_manager.cleanup_dead_pipelines()
            
            # Create audio pipeline for channel 0 on first device
            # Manual frequency entry always uses first RTL-SDR device
            channel_id = 0
            
            self.logger.info(f"Creating audio pipeline for channel {channel_id} on device {self.first_device_serial} (manual decoder)")
            audio_pipeline = self.audio_manager.create_pipeline(frequency, channel_id, device_serial=self.first_device_serial)
            
            if not audio_pipeline:
                self.logger.error("Failed to create audio pipeline - device unavailable or at capacity")
                # Resume spectrum analyzer since no decoders will be on first device
                if self.spectrum_analyzer:
                    self.spectrum_analyzer.resume()
                return False
            
            # Track that we have a decoder on first device
            self.decoders_on_first_device = 1
            
            # Create decoder with specified sonde type
            self.logger.info(f"Creating {sonde_type} decoder instance")
            decoder = RS1729Decoder(
                frequency=frequency,
                sonde_type=sonde_type
            )
            
            decoder.set_frame_callback(self._on_frame_decoded)
            
            # Get audio stream
            self.logger.info("Getting audio stream from pipeline")
            audio_stream = audio_pipeline.get_audio_stream()
            if not audio_stream:
                self.logger.error("Failed to get audio stream from pipeline")
                audio_pipeline.stop()
                if self.spectrum_analyzer:
                    self.spectrum_analyzer.resume()
                return False
            
            # Start decoder
            self.logger.info("Starting decoder process")
            if not decoder.start(audio_stream=audio_stream):
                self.logger.error("Failed to start decoder process")
                audio_pipeline.stop()
                if self.spectrum_analyzer:
                    self.spectrum_analyzer.resume()
                return False
            
            self.logger.info(f"Manual decoder started successfully for {frequency/1e6:.3f} MHz")
            
            # Create a fake signal for tracking
            signal = DetectedSignal(
                frequency=frequency,
                strength=20.0,
                bandwidth=5000,
                timestamp=time.time()
            )
            
            active = ActiveDecoder(
                decoder=decoder,
                signal=signal,
                start_time=time.time(),
                last_update=time.time(),
                audio_pipeline=audio_pipeline,
                device_serial=self.first_device_serial  # Manual decoder always uses first device
            )
            
            # Add to active decoders with lock
            with self.lock:
                self.active_decoders[frequency] = active
            
            return True
            
        except Exception as e:
            self.logger.error(f"Failed to start manual decoder: {e}", exc_info=True)
            # Resume spectrum analyzer if no decoders were successfully started
            if len(self.active_decoders) == 0 and self.spectrum_analyzer:
                self.spectrum_analyzer.resume()
            return False
    
    def _find_signal_near_frequency(self, frequency: float, signals: List[DetectedSignal],
                                     tolerance_hz: float = 50000) -> Optional[DetectedSignal]:
        """Return the closest signal within tolerance_hz of frequency, or None."""
        best = None
        best_diff = tolerance_hz
        for sig in signals:
            diff = abs(sig.frequency - frequency)
            if diff < best_diff:
                best_diff = diff
                best = sig
        return best

    def update_signals(self, signals: List[DetectedSignal]):
        """
        Update with new detected signals.
        Uses 50 kHz tolerance for frequency matching to handle FFT bin drift.
        Protects actively-decoding sondes from being stopped.
        """
        FREQ_TOLERANCE = 50000  # 50 kHz — generous tolerance for FFT resolution drift

        with self.lock:
            # ----------------------------------------------------------
            # 1. Update or stop existing decoders
            # ----------------------------------------------------------
            for freq in list(self.active_decoders.keys()):
                active = self.active_decoders.get(freq)
                if active is None:
                    continue

                # Find closest scan result near this decoder's frequency
                closest = self._find_signal_near_frequency(freq, signals, FREQ_TOLERANCE)

                if closest is not None:
                    # Signal still present (may have drifted slightly) — update info
                    active.signal = closest
                    active.signal.timestamp = time.time()
                    continue

                # No nearby signal in scan — check if decoder has recent telemetry
                recent_frames = (time.time() - active.last_update) < 60
                if recent_frames:
                    self.logger.debug(
                        f"Decoder at {freq/1e6:.4f} MHz: no scan match but decoding "
                        f"({time.time() - active.last_update:.0f}s since last frame) — keeping"
                    )
                    continue

                # No scan match and no recent telemetry — stop after grace period
                if time.time() - active.signal.timestamp > 30:
                    self.logger.info(f"Stopping decoder for disappeared signal at {freq/1e6:.4f} MHz")
                    active.decoder.stop()
                    active.audio_pipeline.stop()            # was missing — caused pipeline leak
                    self.audio_manager.remove_pipeline(freq)  # was missing — caused device leak
                    if active.device_serial == self.first_device_serial:
                        self.decoders_on_first_device -= 1
                    del self.active_decoders[freq]
                    if self.decoders_on_first_device == 0 and self.spectrum_analyzer:
                        self.logger.info("Resuming spectrum analyzer - no decoders on first device")
                        self.spectrum_analyzer.resume()

            # ----------------------------------------------------------
            # 2. Start decoders for new signals not already covered
            # ----------------------------------------------------------
            # Total capacity = max_concurrent channels on each non-scanner device
            num_decoder_devices = max(len(self.device_configs) - 1, 1) if len(self.device_configs) > 1 else len(self.device_configs)
            total_capacity = num_decoder_devices * self.max_concurrent

            if len(self.active_decoders) < total_capacity:
                signals_sorted = sorted(signals, key=lambda s: s.strength, reverse=True)

                for signal in signals_sorted:
                    if len(self.active_decoders) >= total_capacity:
                        break

                    if self._is_frequency_blacklisted(signal.frequency):
                        continue

                    # Skip if an active decoder already covers this frequency
                    already_covered = self._find_signal_near_frequency(
                        signal.frequency,
                        [DetectedSignal(f, 0, 0, 0) for f in self.active_decoders.keys()],
                        FREQ_TOLERANCE
                    )
                    if already_covered:
                        continue

                    self._start_decoder_for_signal(signal)
    
    def _is_frequency_blacklisted(self, frequency: float) -> bool:
        """
        Check if a frequency is blacklisted
        
        Args:
            frequency: Frequency in Hz
            
        Returns:
            True if frequency is within 10 kHz of any blacklisted frequency
        """
        tolerance = 10000  # 10 kHz tolerance
        for blacklisted_freq in self.frequency_blacklist:
            if abs(frequency - blacklisted_freq) < tolerance:
                return True
        return False
    
    def _start_decoder_for_signal(self, signal: DetectedSignal):
        """Start a decoder for a detected signal"""
        try:
            # Clean up any dead pipelines before creating new one (prevents channel leak)
            self.audio_manager.cleanup_dead_pipelines()
            
            # Check if we have device capacity available
            # If only one device and it's at maximum capacity, don't try to start decoder
            if len(self.device_configs) == 1:
                first_device = self.first_device_serial
                device_usage = self.audio_manager.device_usage.get(first_device, [])
                max_channels = self.audio_manager.max_channels
                
                if len(device_usage) >= max_channels:
                    self.logger.warning(f"Cannot start decoder: device {first_device} already at maximum "
                                      f"capacity ({len(device_usage)}/{max_channels} channels active)")
                    self.logger.info(f"Active frequencies on {first_device}: "
                                   f"{[f'{f/1e6:.3f} MHz' for f in device_usage]}")
                    return
            
            # Determine which device to use FIRST (before DFT detection)
            # If spectrum analyzer is running (on first device), we have two options:
            # 1. If we have multiple devices: avoid first device, use others
            # 2. If we only have one device: pause spectrum analyzer first
            
            avoid_device = None
            should_pause_analyzer = False
            selected_device = None
            
            if self.spectrum_analyzer and len(self.device_configs) > 1:
                # Spectrum analyzer holds first device open (even when not actively scanning)
                # Always reserve first device exclusively for spectrum scanning
                avoid_device = self.first_device_serial
                selected_device = self.audio_manager._select_device(avoid_device=avoid_device)
            elif self.spectrum_analyzer and self.spectrum_analyzer.running:
                # Single device and spectrum analyzer is running - must pause it
                should_pause_analyzer = True
                selected_device = self.first_device_serial
            else:
                # No spectrum analyzer or single device not running
                selected_device = self.audio_manager._select_device()
            
            if selected_device is None:
                self.logger.error("No RTL-SDR devices available")
                return
            
            # NOW determine sonde type using the selected device for DFT detection
            sonde_type = self._identify_sonde_type(signal, device_serial=selected_device)
        
            if not sonde_type:
                self.logger.warning(f"Could not identify sonde type for signal at {signal.frequency/1e6:.4f} MHz")
                return
            
            self.logger.info(f"Starting {sonde_type} decoder for {signal.frequency/1e6:.4f} MHz "
                            f"(SNR: {signal.strength:.1f} dB, BW: {signal.bandwidth/1e3:.1f} kHz)")
            
            # Pause spectrum analyzer if needed (after DFT detection completes)
            if should_pause_analyzer and self.spectrum_analyzer:
                self.logger.info("Only one device - pausing spectrum analyzer for decoder")
                self.spectrum_analyzer.pause()
            
            # Create audio pipeline for this frequency
            channel_id = len(self.active_decoders)
            audio_pipeline = self.audio_manager.create_pipeline(signal.frequency, channel_id, avoid_device=avoid_device)
            
            if not audio_pipeline:
                self.logger.error("Failed to create audio pipeline - max channels reached or rtl_sdr unavailable")
                # Resume spectrum analyzer if we paused it but failed to create decoder
                if should_pause_analyzer and self.spectrum_analyzer:
                    self.spectrum_analyzer.resume()
                return
            
            # Check if this decoder is using the first device
            using_first_device = (audio_pipeline.device_serial == self.first_device_serial)
            
            # Track decoders on first device
            if using_first_device:
                self.decoders_on_first_device += 1
                self.logger.info(f"Decoder on first device - total: {self.decoders_on_first_device}")
            
            # Create decoder instance with correct sonde type
            decoder = RS1729Decoder(
                frequency=signal.frequency,
                sonde_type=sonde_type
            )
            
            # Set frame callback to handle decoded frames
            decoder.set_frame_callback(self._on_frame_decoded)
            
            # Get audio stream (stdout of rtl_fm) from audio pipeline
            # This is a file object that will be piped to decoder stdin
            audio_stream = audio_pipeline.get_audio_stream()
            if not audio_stream:
                self.logger.error("Failed to get audio stream from audio pipeline")
                audio_pipeline.stop()
                # Decrement counter if this was on first device
                if using_first_device:
                    self.decoders_on_first_device -= 1
                    # Resume spectrum analyzer if no more decoders on first device
                    if self.decoders_on_first_device == 0 and self.spectrum_analyzer:
                        self.spectrum_analyzer.resume()
                return
            
            # Start decoder with audio stream (stdin piping)
            # Decoder reads raw IQ from stdin, no file paths needed
            if not decoder.start(audio_stream=audio_stream):
                self.logger.error("Failed to start decoder process")
                audio_pipeline.stop()
                # Decrement counter if this was on first device
                if using_first_device:
                    self.decoders_on_first_device -= 1
                    # Resume spectrum analyzer if no more decoders on first device
                    if self.decoders_on_first_device == 0 and self.spectrum_analyzer:
                        self.logger.info("Resuming spectrum analyzer - no decoders on first device")
                        self.spectrum_analyzer.resume()
                return
            
            self.logger.info(f"Decoder started successfully for {signal.frequency/1e6:.4f} MHz")
            
            active = ActiveDecoder(
                decoder=decoder,
                signal=signal,
                start_time=time.time(),
                last_update=time.time(),
                audio_pipeline=audio_pipeline,
                device_serial=audio_pipeline.device_serial  # Track which device this decoder uses
            )
            
            self.active_decoders[signal.frequency] = active
            
        except Exception as e:
            self.logger.error(f"Failed to start decoder: {e}", exc_info=True)
            # Resume if this was the first decoder attempt and it failed
            if len(self.active_decoders) == 0 and self.spectrum_analyzer:
                self.spectrum_analyzer.resume()
    
    def _identify_sonde_type(self, signal: DetectedSignal, device_serial: Optional[str] = None) -> Optional[str]:
        """
        Identify sonde type based on signal characteristics.
        
        Uses two-stage detection:
        1. DFT correlation analysis (if available) - most accurate
        2. Bandwidth-based heuristics (fallback) - less accurate
        
        DFT correlation uses dft_detect from radiosonde_auto_rx to compare
        captured IQ samples against known sonde signatures. This is much more
        accurate than bandwidth-based detection.
        
        Different radiosondes use different modulation schemes and bandwidths:
        - RS41: ~3-5 kHz, GFSK (most common, narrow bandwidth)
        - RS92: ~2.6 kHz, GFSK (even narrower)
        - DFM: ~6-8 kHz, GFSK (wider than RS41)
        - M10: ~9 kHz, AFSK
        - M20: ~22 kHz, AFSK
        - iMet: ~10-15 kHz
        
        Args:
            signal: DetectedSignal object with frequency, bandwidth, strength
            device_serial: Optional RTL-SDR device serial for IQ capture (defaults to first device)
        
        Returns:
            Sonde type string (e.g., 'RS41', 'DFM', 'RS92') or None if unknown
        """
        freq = signal.frequency
        bw = signal.bandwidth
        
        # Use first device for detection if not specified
        if device_serial is None:
            device_serial = self.first_device_serial
        
        self.logger.debug(f"Identifying sonde type: freq={freq/1e6:.4f} MHz, bw={bw/1e3:.1f} kHz")
        
        # Try DFT correlation detection first (most accurate)
        if self.dft_detector and self.dft_detector.available:
            try:
                sonde_type = self.dft_detector.detect_sonde_type(
                    frequency=freq,
                    device_serial=device_serial,
                    sample_rate=48000,
                    bandwidth=bw
                )
                
                if sonde_type:
                    self.logger.info(
                        f"DFT correlation identified {sonde_type} at {freq/1e6:.4f} MHz "
                        f"(BW: {bw/1e3:.1f} kHz)"
                    )
                    return sonde_type
                else:
                    self.logger.info("DFT correlation: no confident match, using bandwidth fallback")
            except Exception as e:
                self.logger.warning(f"DFT detection failed: {e}, using bandwidth fallback")
        
        # Fall back to bandwidth-based detection
        # NOTE: At 2.4 MHz / 1024-bin FFT, each bin is ~2.3 kHz.
        # Narrow signals like RS41 (~2.7 kHz native) appear inflated to 5-9 kHz.
        # DFM17 is genuinely wide (~15 kHz native) and still appears wider.
        # Thresholds are conservative to avoid misclassifying RS41 as DFM.
        self.logger.debug("Using bandwidth-based sonde identification")

        # For 400-406 MHz range (European radiosonde band)
        if 400e6 <= freq <= 406e6:
            if bw >= 20000:  # 20+ kHz — clearly M20 or iMet
                if bw >= 22000:
                    return 'M20'
                else:
                    return 'iMet'
            elif bw >= 14000:  # 14-20 kHz — M10 or iMet
                if bw < 16000:
                    return 'M10'
                else:
                    return 'iMet'
            elif bw >= 10000:  # 10-14 kHz — DFM (genuinely wide signal)
                self.logger.info(f"Bandwidth {bw/1e3:.1f} kHz suggests DFM sonde")
                return 'DFM'
            else:
                # < 10 kHz — RS41 is most common; DFM at this width is unlikely
                # given our FFT resolution inflates narrow signals significantly
                self.logger.info(f"Bandwidth {bw/1e3:.1f} kHz — defaulting to RS41 (most common)")
                return 'RS41'

        # For other frequencies, use generic heuristic
        if bw >= 20000:
            return 'M20'
        elif bw >= 14000:
            if bw < 16000:
                return 'M10'
            else:
                return 'iMet'
        elif bw >= 10000:
            return 'DFM'
        else:
            return 'RS41'

    def _extract_numeric_db(self, value) -> Optional[float]:
        """Parse numeric dB-like values from decoder fields (float/int/string)."""
        if value is None:
            return None

        if isinstance(value, (int, float)):
            try:
                return float(value)
            except (TypeError, ValueError):
                return None

        if isinstance(value, str):
            v = value.strip()
            if not v:
                return None

            # Accept strings like "-92.5", "-92.5 dB", "RSSI=-92.5dBm".
            import re
            match = re.search(r'(-?\d+(?:\.\d+)?)', v)
            if match:
                try:
                    return float(match.group(1))
                except (TypeError, ValueError):
                    return None

        return None

    def _get_scan_signal_strength_db(self, frequency: float) -> Optional[float]:
        """Get best-available signal metric for this decoder frequency."""
        with self.lock:
            active = self.active_decoders.get(frequency)
            if active is None or active.signal is None:
                return None

            # Prefer rolling live IQ metrics when available from audio pipeline.
            try:
                pipeline = getattr(active, 'audio_pipeline', None)
                if pipeline is not None and hasattr(pipeline, 'get_signal_metrics_snapshot'):
                    live_rssi, _live_snr = pipeline.get_signal_metrics_snapshot()
                    live_rssi_db = self._extract_numeric_db(live_rssi)
                    if live_rssi_db is not None:
                        return live_rssi_db
            except Exception:
                pass

            # Non-live fallback: absolute peak power (dBFS) is the RSSI proxy.
            # Do NOT return strength here — that's the SNR, and using it as RSSI
            # is what made RSSI and SNR identical.
            pwr = self._extract_numeric_db(getattr(active.signal, 'power_dbfs', None))
            if pwr is not None and pwr != 0.0:
                return pwr
            return None

    # NOTE: use typing.Tuple, not builtin tuple[...] — the latter needs
    # Python 3.9+ and crashed on a fresh Python 3.8 install at import time
    # ("TypeError: 'type' object is not subscriptable").
    def _resolve_frame_db_values(self, frame_data: dict, frequency: float) -> Tuple[Optional[float], Optional[float]]:
        """Resolve (rssi_db, snr_db) from frame fields with scan-strength fallback."""
        rssi_candidates = (
            frame_data.get('rssi'),
            frame_data.get('power_db'),
            frame_data.get('signal_db'),
            frame_data.get('signal_strength'),
        )
        snr_candidates = (
            frame_data.get('snr'),
            frame_data.get('signal_db'),
            frame_data.get('signal_strength'),
        )

        rssi_db = None
        for candidate in rssi_candidates:
            rssi_db = self._extract_numeric_db(candidate)
            if rssi_db is not None:
                break

        snr_db = None
        for candidate in snr_candidates:
            snr_db = self._extract_numeric_db(candidate)
            if snr_db is not None:
                break

        # RSSI and SNR must fall back to DIFFERENT scan metrics, else they
        # display identically. RSSI ← live RSSI or the scan's absolute peak
        # power (dBFS); SNR ← the scan's SNR (peak above noise floor).
        with self.lock:
            active = self.active_decoders.get(frequency)
            sig = active.signal if (active is not None) else None
        scan_power = self._get_scan_signal_strength_db(frequency)  # live RSSI or power_dbfs
        scan_snr = self._extract_numeric_db(getattr(sig, 'strength', None)) if sig is not None else None

        if rssi_db is None:
            rssi_db = scan_power if scan_power is not None else scan_snr
        if snr_db is None:
            snr_db = scan_snr

        return rssi_db, snr_db
    
    def _on_frame_decoded(self, frame_data: dict):
        """
        Convert raw frame dict from decoder to SondeTelemetry object
        
        Args:
            frame_data: Raw frame data from decoder
        """
        try:
            # Extract required fields
            sonde_id = frame_data.get('sonde_id', 'UNKNOWN')
            frequency = frame_data.get('frequency', 0.0)
            sats_val = frame_data.get('sats')
            rssi_db, snr_db = self._resolve_frame_db_values(frame_data, frequency)
            
            # Log sats extraction for debugging
            if sats_val is not None:
                self.logger.info(f"[TELEMETRY] {sonde_id}: satellites={sats_val}")
            else:
                available_keys = [k for k in frame_data.keys() if k in ('sats', 'lat', 'lon', 'alt', 'velocity_horizontal')]
                self.logger.debug(f"[TELEMETRY] {sonde_id}: No sats field (available: {available_keys})")
            
            # Frame number comes directly from the decoder JSON
            frame_number = frame_data.get('frame_number', 0)
            
            # Create position if we have coordinates
            position = None
            if 'lat' in frame_data and 'lon' in frame_data and 'alt' in frame_data:
                position = SondePosition(
                    latitude=frame_data['lat'],
                    longitude=frame_data['lon'],
                    altitude=frame_data['alt'],
                    datetime=frame_data.get('decoded_datetime') or datetime.utcnow()
                )
            
            # Create velocity if we have speed data
            velocity = None
            if 'velocity_horizontal' in frame_data:
                velocity = SondeVelocity(
                    horizontal_speed=frame_data.get('velocity_horizontal', 0.0),
                    vertical_speed=frame_data.get('velocity_vertical', 0.0),
                    heading=frame_data.get('heading', 0.0)  # Use heading from frame data (D: field or JSON heading)
                )

            # Create environment block ONLY when PTU data actually exists (not just None values).
            # This ensures MQTT/SondeHub only carry PTU when we have real measurements.
            environment = None
            temp = frame_data.get('temp', frame_data.get('temperature'))
            hum = frame_data.get('humidity')
            pres = frame_data.get('pressure')
            
            # Only create environment if at least one PTU field has a real value
            if temp is not None or hum is not None or pres is not None:
                self.logger.info(f"[PTU] {sonde_id}: Creating environment temp={temp}, hum={hum}, pres={pres}")
                environment = SondeEnvironment(
                    temperature=temp,
                    humidity=hum,
                    pressure=pres
                )
            else:
                self.logger.debug(f"[PTU] {sonde_id}: No PTU data in frame_data keys: {list(frame_data.keys())}")
            
            # Create telemetry object
            telemetry = SondeTelemetry(
                sonde_type=frame_data.get('sonde_type', 'RS41'),  # Use type from frame data (DFM, M10, RS41)
                serial=sonde_id,
                frame_number=frame_number,
                subtype=frame_data.get('subtype'),  # DFM17, RS41-SGP, etc.
                dfmcode=frame_data.get('dfmcode'),  # DFM type code (e.g., "0xC")
                position=position,
                velocity=velocity,
                environment=environment,
                frequency=frequency,
                snr=snr_db,
                rssi=rssi_db,
                satellites=frame_data.get('sats'),
                battery=frame_data.get('battery'),  # Battery voltage
                burst_timer=frame_data.get('burst_timer'),  # RS41 burst timer
                rs41_mainboard=frame_data.get('rs41_mainboard'),  # RS41 mainboard type
                rs41_mainboard_fw=frame_data.get('rs41_mainboard_fw'),  # RS41 mainboard FW
                ref_datetime=frame_data.get('ref_datetime'),  # RS41 datetime reference
                ref_position=frame_data.get('ref_position'),  # RS41 position reference
                tx_frequency=frame_data.get('tx_frequency'),  # Transmit frequency (Hz)
                timestamp=datetime.utcnow(),  # Use UTC to match web UI expectations
                decoder_name='rs41mod',
                decoder_version='rs1729'
            )
            
            # Log telemetry summary for debugging
            self.logger.debug(
                f"[TELEMETRY] {sonde_id}: Created telemetry frame={frame_number}, "
                f"has_position={position is not None}, has_velocity={velocity is not None}, "
                f"has_environment={environment is not None}, sats={frame_data.get('sats')}, "
                f"batt={frame_data.get('battery')}"
            )
            
            # Forward to telemetry handler
            self._handle_telemetry(telemetry)
            
        except Exception as e:
            self.logger.error(f"Error converting frame to telemetry: {e}", exc_info=True)
    
    def _handle_telemetry(self, telemetry: SondeTelemetry):
        """Handle decoded telemetry from a decoder"""
        # Update last_update time for the decoder
        with self.lock:
            if telemetry.frequency in self.active_decoders:
                self.active_decoders[telemetry.frequency].last_update = time.time()
        
        # Forward to main telemetry callback
        if self.telemetry_callback:
            self.telemetry_callback(telemetry)
    
    def _management_loop(self):
        """Background thread that manages decoder health"""
        while self.running:
            try:
                self._check_decoder_health()
                time.sleep(5)
            except Exception as e:
                self.logger.error(f"Error in management loop: {e}", exc_info=True)
    
    def _check_decoder_health(self):
        """Check health of active decoders and stop idle/dead ones"""
        # Clean up any dead pipelines FIRST (fixes channel leak)
        self.audio_manager.cleanup_dead_pipelines()
        
        with self.lock:
            for freq, active in list(self.active_decoders.items()):
                # Check if audio pipeline died first  
                if not active.audio_pipeline.is_alive():
                    self.logger.warning(f"Audio pipeline for {freq/1e6:.4f} MHz died - stopping decoder")
                    active.decoder.stop()
                    self.audio_manager.remove_pipeline(freq)
                    # Track device usage
                    if active.device_serial == self.first_device_serial:
                        self.decoders_on_first_device -= 1
                    del self.active_decoders[freq]
                    continue
                
                # Check if decoder process died
                if not active.decoder.is_alive():
                    self.logger.warning(f"Decoder at {freq/1e6:.4f} MHz died unexpectedly")
                    active.audio_pipeline.stop()
                    self.audio_manager.remove_pipeline(freq)
                    # Track device usage
                    if active.device_serial == self.first_device_serial:
                        self.decoders_on_first_device -= 1
                    del self.active_decoders[freq]
                    continue
                
                # Check if decoder is idle (no data for timeout period)
                if active.decoder.is_idle():
                    self.logger.info(f"Stopping idle decoder at {freq/1e6:.4f} MHz")
                    active.decoder.stop()
                    active.audio_pipeline.stop()
                    self.audio_manager.remove_pipeline(freq)
                    # Track device usage
                    if active.device_serial == self.first_device_serial:
                        self.decoders_on_first_device -= 1
                    del self.active_decoders[freq]
            
            # Resume spectrum analyzer when no decoders on first device
            if self.decoders_on_first_device == 0 and self.spectrum_analyzer and not self.spectrum_analyzer.running:
                self.logger.info("No decoders on first device - resuming spectrum analyzer")
                self.spectrum_analyzer.resume()
    
    def get_active_decoders(self) -> List[Dict]:
        """Get list of active decoders with their info"""
        with self.lock:
            return [
                {
                    'frequency': freq,
                    'sonde_type': 'RS41',  # Type identification simplified for v1.0.19
                    'uptime': time.time() - active.start_time,
                    'signal_strength': active.signal.strength
                }
                for freq, active in self.active_decoders.items()
            ]
    
    def get_worker_status(self) -> List[Dict]:
        """Get receiver status for web UI (compatibility with DeviceManager API)"""
        sdr_type = self.config.get('sdr', {}).get('type', 'rtlsdr')
        
        if sdr_type == 'ka9q':
            # Return KA9Q receiver status with active stream info
            if self.ka9q_receiver:
                stream_count = self.ka9q_receiver.get_stream_count()
                decoder_count = self.ka9q_receiver.get_decoder_count()
                
                if stream_count > 0:
                    return [{
                        'serial': 'ka9q-radio',
                        'state': 'DECODING',
                        'frequency': None,
                        'freq_label': f'{stream_count} stream(s), {decoder_count} decoder(s) active',
                        'sonde_type': 'RS41',
                        'sonde_serial': None,
                        'decoder_mode': 'legacy',
                        'channelizer_active': decoder_count,
                        'channelizer_max': 0
                    }]
                else:
                    return [{
                        'serial': 'ka9q-radio',
                        'state': 'IDLE',
                        'frequency': None,
                        'freq_label': 'Listening for multicast (239.1.2.3:5004)',
                        'sonde_type': None,
                        'sonde_serial': None,
                        'decoder_mode': 'legacy',
                        'channelizer_active': 0,
                        'channelizer_max': 0
                    }]
            else:
                # Fallback if ka9q_receiver not available
                return [{
                    'serial': 'ka9q-radio',
                    'state': 'IDLE',
                    'frequency': None,
                    'freq_label': 'KA9Q receiver not initialized',
                    'sonde_type': None,
                    'sonde_serial': None,
                    'decoder_mode': 'legacy',
                    'channelizer_active': 0,
                    'channelizer_max': 0
                }]
        else:
            # For other SDR types, return empty (device_manager handles status)
            return []
