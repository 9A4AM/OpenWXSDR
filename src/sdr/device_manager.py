"""
# =============================================================================
#  OpenWX -- Open Weather Radiosonde Telemetry System
# =============================================================================
#
#  File   : device_manager.py
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
#  RTL-SDR device manager module for OpenWX.
#
#  Each physical RTL-SDR device operates as an independent scan → detect →
#  decode worker. There is no fixed scanner or decoder role: any free device
#  scans for signals, and upon detection switches autonomously to decoding.
#  When a sonde disappears or the decoder goes stale, the device returns to
#  scanning.
#
#  Architecture:
#
#  RTLSDRDeviceManager
#    ├── SondeRegistry   – thread-safe "which frequencies are already being decoded"
#    ├── DeviceWorker[0] – serial RTL00001, state: SCANNING / DECODING
#    ├── DeviceWorker[1] – serial RTL00002, …
#    ├── DeviceWorker[2] – serial RTL00003, …	
#    └── DeviceWorker[3] – serial RTL00004, …	
#
#  DeviceWorker state machine
#  --------------------------
#    IDLE ──► SCANNING ──► DECODING ──► SCANNING ──► …
#                │               │
#                └───────────────┘  (decoder dies / stale)
#  Classes:
#
#  SondeRegistry     Thread-safe frequency claim registry (±50 kHz tolerance).
#  ActiveDecoder     Snapshot dataclass; compatible with web_server.py API.
#  DeviceWorker      Per-device IDLE → SCANNING → DECODING state machine.
#  RTLSDRDeviceManager  Top-level manager; spawns workers, exposes web API.
#
#  Decoder backend   : rs1729 (RS41, DFM09, M10, iMet-C, ...)
#  Sonde detection   : SpectrumAnalyzer (pyrtlsdr FFT) + DftDetector
#
# =============================================================================
"""

import logging
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Dict, List, Optional

from .rtlsdr_analyzer import DetectedSignal, SpectrumAnalyzer
from .audio_pipeline import AudioPipeline
from .dft_detector import DftDetector
from ..decoders.rs1729_decoder import RS1729Decoder
from ..decoders.models import (
    SondeTelemetry, SondePosition, SondeVelocity, SondeEnvironment
)


# ---------------------------------------------------------------------------
# Shared sonde frequency registry
# ---------------------------------------------------------------------------

class SondeRegistry:
    """
    Thread-safe set of frequencies that are currently being decoded.
    Uses a tolerance window so small FFT drift doesn't cause double-decoding.
    """

    TOLERANCE_HZ = 50_000   # 50 kHz – matches DecoderManager tolerance

    def __init__(self):
        self._entries: Dict[float, bool] = {}
        self._lock = threading.Lock()

    def _find_near(self, freq_hz: float) -> Optional[float]:
        for f in self._entries:
            if abs(f - freq_hz) < self.TOLERANCE_HZ:
                return f
        return None

    def is_active(self, freq_hz: float) -> bool:
        with self._lock:
            return self._find_near(freq_hz) is not None

    def register(self, freq_hz: float) -> bool:
        """Atomically claim freq_hz.  Returns False if already claimed."""
        with self._lock:
            if self._find_near(freq_hz) is not None:
                return False
            self._entries[freq_hz] = True
            return True

    def unregister(self, freq_hz: float):
        with self._lock:
            key = self._find_near(freq_hz)
            if key is not None:
                del self._entries[key]

    def active_frequencies(self) -> List[float]:
        with self._lock:
            return list(self._entries.keys())


# ---------------------------------------------------------------------------
# ActiveDecoder – web_server.py-compatible snapshot
# ---------------------------------------------------------------------------

@dataclass
class ActiveDecoder:
    """Snapshot of a running decoder, compatible with web_server.py expectations."""
    decoder: object        # RS1729Decoder  (.running, .sonde_type)
    signal: object         # DetectedSignal (.frequency)
    start_time: float
    last_update: float
    audio_pipeline: object  # AudioPipeline
    device_serial: str


# ---------------------------------------------------------------------------
# Per-device worker
# ---------------------------------------------------------------------------

class DeviceWorker:
    """
    Manages one RTL-SDR device through independent scan → detect → decode cycles.

    SCANNING state: pyrtlsdr is open; spectrum is captured repeatedly to find
                    new radiosonde signals not already claimed by another worker.

    DECODING state: pyrtlsdr is closed; rtl_fm + rs1729 decoder are running for
                    the detected sonde.  Returns to SCANNING when the decoder
                    dies or goes silent.
    """

    STATE_IDLE     = 'idle'
    STATE_SCANNING = 'scanning'
    STATE_DECODING = 'decoding'

    def __init__(self, device_config: dict, app_config: dict,
                 sonde_registry: SondeRegistry,
                 telemetry_callback: Callable[[SondeTelemetry], None],
                 device_index: int = 0,
                 manager=None):
        self.device_config   = device_config
        self.device_serial   = device_config['serial']
        self.device_index    = device_index  # Used for staggered USB initialization
        self.app_config      = app_config
        self.registry        = sonde_registry
        self.telemetry_cb    = telemetry_callback
        self._manager        = manager  # Reference to RTLSDRDeviceManager for fixed_channels check
        self.logger          = logging.getLogger(f'Worker.{self.device_serial}')

        self._state          = self.STATE_SCANNING  # Start in SCANNING state (will open USB on first cycle)
        self._running        = False
        self._thread: Optional[threading.Thread] = None
        self._first_usb_init = True  # Flag to track first USB device open
        self._device_lock = threading.Lock()  # CRITICAL: Prevent USB race conditions
        self._manual_decode_pending = threading.Event()  # Signals scan cycle to abort early

        # Active scanning components
        self._analyzer: Optional[SpectrumAnalyzer] = None
        self._last_spectrum: dict = {}
        self._spectrum_lock = threading.Lock()

        # Active decoding components
        self._pipeline: Optional[AudioPipeline] = None
        self._decoder:  Optional[RS1729Decoder] = None
        self._cur_freq: Optional[float] = None
        self._cur_type: Optional[str]  = None
        self._cur_signal_strength_db: Optional[float] = None
        self._decode_start   = 0.0
        self._last_frame_t   = 0.0
        self._last_state     = self.STATE_IDLE  # Track previous state for transition delays

        # Timing
        cfg_rx = app_config.get('receivers', {})
        self._scan_interval  = cfg_rx.get('scan_interval', 15)
        self._idle_timeout   = 300   # seconds without frames → back to scan
        self._decode_expiration_time: Optional[float] = None  # For duration-limited decoding
        self._is_manual_decoder: bool = False  # Manual decoders ignore idle timeout

        # DFT detector for sonde type identification
        det_cfg = app_config.get('detection', {})
        if det_cfg.get('use_dft_detect', True):
            self._dft = DftDetector(
                dft_detect_path=det_cfg.get('dft_detect_path', 'dft_detect'),
                sample_duration=det_cfg.get('dft_sample_duration', 5.0)
            )
        else:
            self._dft = None

        # Frequency blacklist (Hz)
        bl = det_cfg.get('frequency_blacklist', [])
        self._blacklist = [f * 1e6 for f in bl]
        
        # RX Scan cycling (Phase 2)
        self._rx_scan_enabled = False
        self._rx_scan_channels: List[dict] = []
        self._rx_scan_index = 0
        self._fixed_channel_scantime = int(det_cfg.get('fixed_channel_scantime', 60))

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self):
        self._running = True
        self._thread = threading.Thread(
            target=self._run, daemon=True,
            name=f'Worker-{self.device_serial}'
        )
        self._thread.start()
        self.logger.info(f"Started (device {self.device_serial})")

    def stop(self):
        self._running = False
        self._teardown_decode()
        self._teardown_scan()
        if self._thread:
            self._thread.join(timeout=10)
        self._state = self.STATE_IDLE

    # ------------------------------------------------------------------
    # Public read-only state
    # ------------------------------------------------------------------

    @property
    def state(self) -> str:
        return self._state

    @property
    def current_freq(self) -> Optional[float]:
        return self._cur_freq

    @property
    def current_sonde_type(self) -> Optional[str]:
        return self._cur_type

    def get_active_decoder(self) -> Optional[ActiveDecoder]:
        """Return a snapshot if currently decoding, else None."""
        if self._state == self.STATE_DECODING and self._decoder and self._cur_freq:
            return ActiveDecoder(
                decoder=self._decoder,
                signal=DetectedSignal(
                    frequency=self._cur_freq,
                    strength=20.0,
                    bandwidth=5000,
                    timestamp=self._decode_start
                ),
                start_time=self._decode_start,
                last_update=self._last_frame_t or self._decode_start,
                audio_pipeline=self._pipeline,
                device_serial=self.device_serial
            )
        return None

    def get_spectrum(self) -> dict:
        """Return latest spectrum snapshot for this RTL-SDR worker."""
        with self._spectrum_lock:
            return dict(self._last_spectrum)

    def _update_spectrum_snapshot(self, freqs, power_db, signals: List[DetectedSignal]):
        """Build a compact spectrum payload for the web UI."""
        try:
            import numpy as np

            if freqs is None or power_db is None or len(freqs) == 0:
                return

            noise_floor = float(np.percentile(power_db, 20))
            threshold_db = float(self._analyzer.detection_threshold if self._analyzer else 10.0)

            ds = max(1, len(freqs) // 2000)
            spec = {
                'freqs_mhz': (freqs[::ds] / 1e6).tolist(),
                'power_db': power_db[::ds].tolist(),
                'noise_floor': noise_floor,
                'threshold_db': threshold_db,
                'signals': [
                    {
                        'freq_mhz': float(s.frequency / 1e6),
                        'snr_db': float(s.strength),
                    }
                    for s in signals
                ],
                'timestamp': datetime.utcnow().isoformat() + 'Z',
                'receiver_id': f"rtlsdr:{self.device_serial}",
                'receiver_name': f"RTL-SDR {self.device_serial}",
            }
            with self._spectrum_lock:
                self._last_spectrum = spec
        except Exception as exc:
            self.logger.debug(f"Failed to update spectrum snapshot: {exc}")

    # ------------------------------------------------------------------
    # RX Scan cycling (Phase 2)
    # ------------------------------------------------------------------

    def enable_rx_scan(self, channels: List[dict]):
        """Enable RX Scan mode with a list of channels to cycle through.
        
        Args:
            channels: List of channel dicts, each with: {frequency: MHz, type: str, enabled: bool}
        """
        self._rx_scan_channels = [ch for ch in channels if ch.get('enabled', False)]
        self._rx_scan_index = 0
        self._rx_scan_enabled = len(self._rx_scan_channels) > 0
        
        if self._rx_scan_enabled:
            self.logger.info(
                f"RX Scan enabled with {len(self._rx_scan_channels)} channels, "
                f"scantime={self._fixed_channel_scantime}s"
            )
    
    def disable_rx_scan(self):
        """Disable RX Scan cycling mode."""
        self._rx_scan_enabled = False
        self._rx_scan_channels = []
        self._rx_scan_index = 0
        self.logger.info("RX Scan disabled")
    
    def _start_next_rx_scan_channel(self) -> bool:
        """Start the next channel in RX Scan rotation. Returns True if successful."""
        if not self._rx_scan_enabled or not self._rx_scan_channels:
            return False
        
        # Get current channel (wrap around)
        channel = self._rx_scan_channels[self._rx_scan_index]
        current_idx = self._rx_scan_index  # Save for logging
        self._rx_scan_index = (self._rx_scan_index + 1) % len(self._rx_scan_channels)
        
        freq_hz = float(channel['frequency']) * 1e6
        stype = str(channel.get('type', 'RS41'))
        
        self.logger.info(
            f"RX Scan [{current_idx + 1}/{len(self._rx_scan_channels)}]: "
            f"{stype} at {freq_hz/1e6:.3f} MHz for {self._fixed_channel_scantime}s"
        )
        
        # Start decode with duration limit (will auto-cycle when time expires)
        sig = DetectedSignal(
            frequency=freq_hz, strength=25.0,
            bandwidth=7000, timestamp=time.time()
        )
        self._decode_expiration_time = None
        self._is_manual_decoder = False  # RX Scan decoders are NOT manual
        self._decode_expiration_time = time.time() + self._fixed_channel_scantime
        
        return self._start_decode(sig, override_type=stype)

    def start_manual_decode(self, frequency: float, sonde_type: str,
                           duration_seconds: Optional[float] = None) -> bool:
        """Force-start decoding a specific frequency (from web UI).
        
        Args:
            frequency: Target frequency in Hz
            sonde_type: Sonde type (RS41, RS92, etc.)
            duration_seconds: If set, auto-return to scanning after this many seconds.
                            None or 0 = infinite decoding.
        """
        # Signal the scan cycle to stop ASAP so we can acquire the lock quickly
        self._manual_decode_pending.set()
        self.logger.info(f"Manual decode: waiting for device lock on {self.device_serial}...")

        # CRITICAL: Acquire device lock to prevent race with worker thread.
        # Use a timeout so we get an error log instead of blocking forever if the
        # scan cycle is stuck (e.g. read_samples() hang on a USB error).
        if not self._device_lock.acquire(timeout=20.0):
            self._manual_decode_pending.clear()
            self.logger.error(
                f"Manual decode: could not acquire device lock on {self.device_serial} "
                f"after 20 s — scan cycle may be stuck"
            )
            return False

        try:
            self._manual_decode_pending.clear()  # Lock is ours; clear the interrupt flag

            if self._state == self.STATE_DECODING:
                self.logger.warning(f"Device {self.device_serial} already decoding, cannot start manual decode")
                return False
            
            # Set state to IDLE immediately to signal worker thread to stop scanning
            # Worker thread will see IDLE state and skip further scan cycles
            if self._state == self.STATE_SCANNING:
                self._state = self.STATE_IDLE  # Stop worker thread from scanning
                self.logger.info(f"Manual decode: stopping scanner on device {self.device_serial}")
        finally:
            self._device_lock.release()
        
        # Wait for USB device to be fully released before starting rtl_fm
        # Conservative 5-second delay for USB hub stability (increased from 3s for long-running sessions)
        self.logger.info(f"Manual decode: waiting 5s for USB device {self.device_serial} to settle")
        time.sleep(5.0)
        
        # Re-acquire lock for decoder setup
        with self._device_lock:
            sig = DetectedSignal(
                frequency=frequency, strength=25.0,
                bandwidth=7000, timestamp=time.time()
            )
            self._decode_expiration_time = None
            # Always mark as manual decoder (None/0 = infinite, >0 = timed)
            self._is_manual_decoder = True
            if duration_seconds and duration_seconds > 0:
                self._decode_expiration_time = time.time() + duration_seconds
            
            # _start_decode() will handle teardown_scan() and USB initialization
            success = self._start_decode(sig, override_type=sonde_type)
            if success:
                self.logger.info(
                    f"Manual decode started: {sonde_type} at {frequency/1e6:.3f} MHz "
                    f"on device {self.device_serial}"
                )
            else:
                self.logger.error(
                    f"Manual decode failed: {sonde_type} at {frequency/1e6:.3f} MHz "
                    f"on device {self.device_serial}"
                )
            return success

    def stop_decode_and_scan(self) -> bool:
        """Stop the current decode (if any) and return to scanning."""
        if self._state == self.STATE_DECODING:
            self._teardown_decode()   # sets _state = IDLE
        self._state = self.STATE_SCANNING
        self.logger.info(f"Device {self.device_serial}: returning to scan")
        return True

    # ------------------------------------------------------------------
    # Worker loop
    # ------------------------------------------------------------------

    def _run(self):
        idle_start = None  # Track when we entered IDLE state
        
        while self._running:
            try:
                # Track state transitions
                prev_state = self._last_state
                self._last_state = self._state
                
                if self._state == self.STATE_SCANNING:
                    idle_start = None  # Reset idle timer
                    self._scan_cycle()
                elif self._state == self.STATE_DECODING:
                    idle_start = None  # Reset idle timer
                    self._decode_cycle()
                elif self._state == self.STATE_IDLE:
                    # IDLE state: waiting for manual decode to start or transition back to scanning
                    if idle_start is None:
                        idle_start = time.time()
                    elif time.time() - idle_start > 15:
                        # Been idle for 15 seconds, transition back to scanning
                        self.logger.info(f"IDLE timeout - returning to SCANNING")
                        self._state = self.STATE_SCANNING
                        idle_start = None
                    else:
                        # Just wait a bit
                        time.sleep(0.5)
                        
            except Exception as exc:
                self.logger.error(f"Worker loop error: {exc}", exc_info=True)
                time.sleep(5)

    # ------------------------------------------------------------------
    # Scanning
    # ------------------------------------------------------------------

    def _scan_cycle(self):
        """Open the RTL-SDR, capture one spectrum, look for new signals."""
        # CRITICAL: Try to acquire device lock (non-blocking)
        # If manual decoder is setting up, skip this scan cycle
        if not self._device_lock.acquire(blocking=False):
            self.logger.debug(f"Device lock held (manual decoder active), skipping scan cycle")
            time.sleep(0.5)
            return
        
        try:
            # Abort early if manual decode was requested while we were waiting
            if self._manual_decode_pending.is_set():
                return

            if self._analyzer is None:
                # If we just transitioned from DECODING, wait for USB device to be fully released
                # Conservative 3-second delay for USB hub stability
                if self._last_state == self.STATE_DECODING:
                    time.sleep(3.0)
                
                # CRITICAL: Stagger first USB device open to prevent simultaneous access
                # This prevents "[R82XX] PLL not locked!" errors from USB bus contention
                if self._first_usb_init and self.device_index > 0:
                    stagger_delay = self.device_index * 1.0  # 1 second per device
                    self.logger.info(f"First USB init: waiting {stagger_delay:.1f}s to prevent conflicts")
                    time.sleep(stagger_delay)
                    self._first_usb_init = False
                
                self._analyzer = SpectrumAnalyzer(self.app_config, self.device_config)
                if not self._analyzer.initialize():
                    self.logger.error("Cannot open RTL-SDR — retrying in 15 s")
                    self._analyzer = None
                    time.sleep(15)
                    return
                self._state = self.STATE_SCANNING
                self.logger.info(
                    f"Scanning {self.device_config['center_freq']/1e6:.1f} MHz "
                    f"±{self.device_config['sample_rate']/2e6:.1f} MHz"
                )

            try:
                freqs, power_db = self._analyzer.capture_spectrum()
                signals = self._analyzer.detect_signals(freqs, power_db)
                signals = self._analyzer.filter_signals_in_ranges(signals)
                self._update_spectrum_snapshot(freqs, power_db, signals)
            except Exception as exc:
                self.logger.warning(f"Spectrum capture failed: {exc}")
                self._teardown_scan()
                time.sleep(5)
                return

            # Abort if manual decode was requested during spectrum capture
            if self._manual_decode_pending.is_set():
                return

            # Sort by strength descending; skip blacklisted / already-decoded freqs
            for sig in sorted(signals, key=lambda s: s.strength, reverse=True):
                if self._is_blacklisted(sig.frequency):
                    continue
                if self.registry.is_active(sig.frequency):
                    continue
                
                # CRITICAL: Skip signals that are assigned to fixed_channels
                # Let fixed_channels start them with the correct type, not auto-detection
                if self._is_fixed_channel_frequency(sig.frequency):
                    self.logger.debug(
                        f"Skipping {sig.frequency/1e6:.4f} MHz - reserved for fixed_channel with specified type"
                    )
                    continue
                
                self.logger.info(
                    f"New signal at {sig.frequency/1e6:.4f} MHz "
                    f"(SNR {sig.strength:.1f} dB, BW {sig.bandwidth/1e3:.1f} kHz)"
                )
                self._start_decode(sig)
                return   # worker will re-enter loop in DECODING state
        finally:
            self._device_lock.release()

        time.sleep(self._scan_interval)

    # ------------------------------------------------------------------
    # Decoding
    # ------------------------------------------------------------------

    def _decode_cycle(self):
        """Monitor the running decoder; return to scanning when it ends."""
        if not self._decoder or not self._decoder.is_alive():
            self.logger.info(
                f"Decoder ended on {self._cur_freq/1e6:.4f} MHz — back to scan"
            )
            self._teardown_decode()
            return

        # Manual decoders: duration=None/0 means infinite, skip idle check
        # Duration>0 is handled by timer expiration (not idle timeout)
        if not self._is_manual_decoder and self._decoder.is_idle():
            self.logger.info(
                f"Decoder idle for >{self._idle_timeout}s — back to scan"
            )
            self._teardown_decode()
            return
        
        # Check if duration-limited decode has expired
        if self._decode_expiration_time is not None:
            elapsed = time.time() - (self._decode_expiration_time - (time.time() - self._decode_start))
            remaining = self._decode_expiration_time - time.time()
            if remaining <= 0:
                self.logger.info(
                    f"Decoder duration limit expired on {self._cur_freq/1e6:.4f} MHz — back to scan"
                )
                self._teardown_decode()
                return

        time.sleep(2)

    # ------------------------------------------------------------------
    # Scan → Decode transition
    # ------------------------------------------------------------------

    def _start_decode(self, sig: DetectedSignal,
                      override_type: Optional[str] = None) -> bool:
        # Atomically claim the frequency in the shared registry
        if not self.registry.register(sig.frequency):
            self.logger.info(
                f"{sig.frequency/1e6:.4f} MHz already claimed — continuing scan"
            )
            return False

        # Close pyrtlsdr so rtl_fm can open the same USB device
        self._teardown_scan()
        # Wait for USB device to be fully released before DFT detection
        # This prevents "[R82XX] PLL not locked!" errors
        # Conservative 3-second delay for USB hub stability with multiple devices
        self.logger.debug(f"Waiting 3s for USB device {self.device_serial} to settle...")
        time.sleep(3.0)

        # Identify sonde type (this will use rtl_fm internally for DFT detection)
        if override_type:
            sonde_type = override_type
            self.logger.info(f"Manual decode: using override type {sonde_type} at {sig.frequency/1e6:.4f} MHz")
        else:
            sonde_type = self._identify_sonde_type(sig)
            if not sonde_type:
                self.logger.error(f"Failed to identify sonde type at {sig.frequency/1e6:.4f} MHz")
                self.registry.unregister(sig.frequency)
                return False

        # Add brief delay after DFT detection before starting AudioPipeline
        # DFT detection just used rtl_fm, so give device time to settle
        # Skip this delay if override_type was provided (DFT wasn't run)
        if not override_type:
            self.logger.debug(f"Waiting 1s after DFT detection before starting AudioPipeline...")
            time.sleep(1.0)

        # Start rtl_fm audio pipeline
        self.logger.info(f"Starting AudioPipeline for {sonde_type} at {sig.frequency/1e6:.4f} MHz on {self.device_serial}")
        pipeline = AudioPipeline(
            frequency=sig.frequency,
            sample_rate=48000,
            device_serial=self.device_serial,
            gain=self.device_config.get('gain', 40),
            ppm_correction=self.device_config.get('ppm_error', 0)
        )
        if not pipeline.start():
            self.logger.error(f"AudioPipeline failed to start for {sonde_type} at {sig.frequency/1e6:.4f} MHz")
            self.registry.unregister(sig.frequency)
            return False

        # Start rs1729 decoder
        self.logger.info(f"Starting RS1729 decoder for {sonde_type} at {sig.frequency/1e6:.4f} MHz")
        decoder = RS1729Decoder(frequency=sig.frequency, sonde_type=sonde_type)
        decoder.set_frame_callback(self._on_frame)
        audio_stream = pipeline.get_audio_stream()
        if not audio_stream:
            self.logger.error(f"AudioPipeline audio stream is None for {sonde_type} at {sig.frequency/1e6:.4f} MHz")
            pipeline.stop()
            self.registry.unregister(sig.frequency)
            return False
        if not decoder.start(audio_stream=audio_stream):
            self.logger.error(f"RS1729 decoder failed to start for {sonde_type} at {sig.frequency/1e6:.4f} MHz")
            pipeline.stop()
            self.registry.unregister(sig.frequency)
            return False

        self._pipeline     = pipeline
        self._decoder      = decoder
        self._cur_freq     = sig.frequency
        self._cur_type     = sonde_type
        self._cur_signal_strength_db = float(sig.strength)
        self._decode_start = time.time()
        self._last_frame_t = 0.0
        self._state        = self.STATE_DECODING
        self.logger.info(
            f"Decoding {sonde_type} at {sig.frequency/1e6:.4f} MHz "
            f"(device {self.device_serial})"
        )
        return True

    # ------------------------------------------------------------------
    # Teardown helpers
    # ------------------------------------------------------------------

    def _teardown_scan(self):
        if self._analyzer:
            try:
                self._analyzer.close()
            except Exception:
                pass
            self._analyzer = None

    def _teardown_decode(self):
        if self._decoder:
            try:
                self._decoder.stop()
            except Exception:
                pass
            self._decoder = None
        if self._pipeline:
            try:
                self._pipeline.stop()
            except Exception:
                pass
            self._pipeline = None
        if self._cur_freq:
            self.registry.unregister(self._cur_freq)
        self._cur_freq  = None
        self._cur_type  = None
        self._cur_signal_strength_db = None
        was_manual = self._is_manual_decoder
        self._is_manual_decoder = False
        self._state     = self.STATE_IDLE
        
        # Phase 2: If RX Scan is enabled and this was NOT a manual decoder, start next channel
        if self._rx_scan_enabled and not was_manual:
            self.logger.info("RX Scan: decode complete, cycling to next channel...")
            # Conservative 3-second USB delay before starting next decode
            time.sleep(3.0)
            # Start next channel (will set state to DECODING if successful)
            if not self._start_next_rx_scan_channel():
                self.logger.warning("RX Scan: failed to start next channel, returning to SCANNING")
                self._state = self.STATE_SCANNING

    # ------------------------------------------------------------------
    # Sonde-type identification
    # ------------------------------------------------------------------

    def _identify_sonde_type(self, sig: DetectedSignal) -> Optional[str]:
        if self._dft and self._dft.available:
            try:
                result = self._dft.detect_sonde_type(
                    frequency=sig.frequency,
                    device_serial=self.device_serial,
                    sample_rate=48000,
                    bandwidth=sig.bandwidth
                )
                if result:
                    self.logger.info(
                        f"DFT identified {result} at {sig.frequency/1e6:.4f} MHz"
                    )
                    return result
                self.logger.info("DFT: no confident match — using bandwidth fallback")
            except Exception as exc:
                self.logger.warning(f"DFT detection error: {exc}")
        return self._bandwidth_fallback(sig)

    def _bandwidth_fallback(self, sig: DetectedSignal) -> str:
        bw   = sig.bandwidth
        freq = sig.frequency
        if 400e6 <= freq <= 406e6:
            if bw >= 22000:   return 'M20'
            if bw >= 16000:   return 'iMet'
            if bw >= 14000:   return 'M10'
            if bw >= 12000:
                self.logger.info(f"BW {bw/1e3:.1f} kHz → RS92")
                return 'RS92'
            if bw >= 7000:
                # DFM-06/09/17 typically 7.5-8.5 kHz
                self.logger.info(f"BW {bw/1e3:.1f} kHz → DFM")
                return 'DFM'
            # RS41 typically 4.8-6 kHz
            self.logger.info(f"BW {bw/1e3:.1f} kHz → RS41 (default)")
            return 'RS41'
        return 'RS41'

    def _is_blacklisted(self, freq_hz: float) -> bool:
        return any(abs(freq_hz - b) < 10_000 for b in self._blacklist)

    def _is_fixed_channel_frequency(self, freq_hz: float) -> bool:
        """Check if frequency matches a configured fixed_channel (within 10 kHz tolerance)."""
        # Get fixed_channels from manager (they're stored at manager level)
        try:
            if not hasattr(self, '_manager') or not self._manager:
                self.logger.debug(f"_is_fixed_channel_frequency: No manager reference")
                return False
                
            if not hasattr(self._manager, '_fixed_channels'):
                self.logger.debug(f"_is_fixed_channel_frequency: Manager has no _fixed_channels attribute")
                return False
            
            fixed_channels = self._manager._fixed_channels
            if not fixed_channels:
                self.logger.debug(f"_is_fixed_channel_frequency: fixed_channels list is empty")
                return False
            
            self.logger.debug(f"_is_fixed_channel_frequency: Checking {freq_hz/1e6:.3f} MHz against {len(fixed_channels)} fixed channel(s)")
            
            for ch in fixed_channels:
                if not ch.get('enabled', False):
                    self.logger.debug(f"  Channel {ch.get('frequency')} MHz: disabled, skipping")
                    continue
                    
                ch_freq_mhz = ch.get('frequency', 0)
                ch_freq_hz = float(ch_freq_mhz) * 1e6
                freq_diff_khz = abs(freq_hz - ch_freq_hz) / 1e3
                
                self.logger.debug(
                    f"  Channel {ch_freq_mhz} MHz: diff={freq_diff_khz:.1f} kHz, "
                    f"type={ch.get('type')}, device={ch.get('receiver_device')}"
                )
                
                if abs(freq_hz - ch_freq_hz) < 10_000:  # Within 10 kHz
                    self.logger.info(
                        f"Frequency {freq_hz/1e6:.3f} MHz matches fixed_channel {ch_freq_mhz} MHz "
                        f"(type={ch.get('type')}) - will skip scanning"
                    )
                    return True
                    
            self.logger.debug(f"_is_fixed_channel_frequency: No match for {freq_hz/1e6:.3f} MHz")
            return False
            
        except Exception as exc:
            self.logger.error(f"_is_fixed_channel_frequency error: {exc}", exc_info=True)
            return False

    # ------------------------------------------------------------------
    # Telemetry / frame conversion
    # ------------------------------------------------------------------

    def _on_frame(self, frame_data: dict):
        """Convert raw rs1729 frame dict → SondeTelemetry and forward upstream."""
        self._last_frame_t = time.time()
        try:
            sonde_id     = frame_data.get('sonde_id', 'UNKNOWN')
            frequency_hz = frame_data.get('frequency', self._cur_freq or 0.0)

            def _parse_db(val) -> Optional[float]:
                if val is None:
                    return None
                if isinstance(val, (int, float)):
                    try:
                        return float(val)
                    except (TypeError, ValueError):
                        return None
                if isinstance(val, str):
                    m = re.search(r'(-?\d+(?:\.\d+)?)', val)
                    if m:
                        try:
                            return float(m.group(1))
                        except (TypeError, ValueError):
                            return None
                return None

            rssi_db = None
            for key in ('rssi', 'power_db', 'signal_db', 'signal_strength'):
                rssi_db = _parse_db(frame_data.get(key))
                if rssi_db is not None:
                    break

            snr_db = None
            for key in ('snr', 'signal_db', 'signal_strength'):
                snr_db = _parse_db(frame_data.get(key))
                if snr_db is not None:
                    break

            live_rssi = None
            live_snr = None
            if self._pipeline is not None:
                try:
                    live_rssi, live_snr = self._pipeline.get_signal_metrics_snapshot()
                except Exception:
                    live_rssi, live_snr = None, None

            if live_rssi is not None:
                rssi_db = live_rssi
            elif rssi_db is None:
                rssi_db = self._cur_signal_strength_db

            if live_snr is not None:
                snr_db = live_snr
            elif snr_db is None:
                snr_db = self._cur_signal_strength_db

            # Frame number from parsed frame_data or fallback to raw line "[  361] …"
            frame_number = frame_data.get('frame_number', 0)
            if frame_number == 0:
                # Try fallback parsing from raw line
                raw = frame_data.get('raw_line', '')
                if '[' in raw and ']' in raw:
                    try:
                        frame_number = int(raw[raw.find('[')+1:raw.find(']')].strip())
                    except ValueError:
                        pass
            
            # Skip upload if frame_number is still 0 or None (invalid/failed decode)
            if not frame_number or frame_number == 0:
                self.logger.debug(
                    f"Skipping frame with invalid frame_number={frame_number} for {sonde_id} "
                    f"(likely incomplete decode)"
                )
                return
            
            # Get decoded datetime from sonde (NOT gateway time!)
            decoded_datetime = frame_data.get('decoded_datetime')
            if not decoded_datetime:
                # Fallback to UTC now only if no decoded time available
                decoded_datetime = datetime.utcnow()
                self.logger.warning(
                    f"No decoded_datetime available for {sonde_id} frame {frame_number}, "
                    f"using gateway time as fallback"
                )

            position = None
            if 'lat' in frame_data and 'lon' in frame_data and 'alt' in frame_data:
                position = SondePosition(
                    latitude=frame_data['lat'],
                    longitude=frame_data['lon'],
                    altitude=frame_data['alt'],
                    datetime=decoded_datetime  # Use decoded sonde time!
                )

            velocity = None
            if 'velocity_horizontal' in frame_data:
                velocity = SondeVelocity(
                    horizontal_speed=frame_data.get('velocity_horizontal', 0.0),
                    vertical_speed=frame_data.get('velocity_vertical', 0.0),
                    heading=frame_data.get('heading', 0.0)
                )

            environment = None
            if any(k in frame_data for k in ('temp', 'humidity', 'pressure')):
                environment = SondeEnvironment(
                    temperature=frame_data.get('temp'),
                    humidity=frame_data.get('humidity'),
                    pressure=frame_data.get('pressure')
                )

            telemetry = SondeTelemetry(
                sonde_type=frame_data.get('sonde_type', self._cur_type or 'RS41'),
                serial=sonde_id,
                frame_number=frame_number,
                subtype=frame_data.get('subtype'),
                position=position,
                velocity=velocity,
                environment=environment,
                frequency=frequency_hz,
                snr=snr_db,
                rssi=rssi_db,
                satellites=frame_data.get('sats'),
                battery=frame_data.get('battery'),
                burst_timer=frame_data.get('burst_timer'),
                rs41_mainboard=frame_data.get('rs41_mainboard'),
                rs41_mainboard_fw=frame_data.get('rs41_mainboard_fw'),
                ref_datetime=frame_data.get('ref_datetime'),
                ref_position=frame_data.get('ref_position'),
                tx_frequency=frame_data.get('tx_frequency'),
                timestamp=decoded_datetime,  # Use decoded sonde time!
                decoder_name='rs1729',
                decoder_version='rs1729'
            )

            if self.telemetry_cb:
                self.telemetry_cb(telemetry)

        except Exception as exc:
            self.logger.error(f"Frame conversion error: {exc}", exc_info=True)


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

class RTLSDRDeviceManager:
    """
    Creates one DeviceWorker per configured RTL-SDR device and starts them all.
    Workers discover devices by serial number (USB order-independent) and
    independently scan → detect → decode in parallel.

    Exposes attributes compatible with web_server.py:
      .running           bool
      .lock              threading.Lock
      .active_decoders   dict {freq_hz: ActiveDecoder}
      .device_configs    list[dict]
      .first_device_serial   str
      .start_manual_decoder(freq, type) → bool
    """

    def __init__(self, config: dict,
                 telemetry_callback: Callable[[SondeTelemetry], None]):
        self.config             = config
        self.telemetry_callback = telemetry_callback
        self.logger             = logging.getLogger('RTLSDRDeviceManager')
        self.running            = False
        self.lock               = threading.Lock()   # web_server compatibility

        self._registry = SondeRegistry()
        self._workers: List[DeviceWorker] = []

        # Build device list from config
        rtlsdr_cfg = config.get('sdr', {}).get('rtlsdr', {})
        if 'devices' in rtlsdr_cfg:
            self.device_configs = rtlsdr_cfg['devices']
        else:
            # Legacy single-device format
            self.device_configs = [{
                'serial':      str(rtlsdr_cfg.get('device_index', 0)),
                'center_freq': rtlsdr_cfg.get('center_freq', 403_000_000),
                'sample_rate': rtlsdr_cfg.get('sample_rate', 2_400_000),
                'gain':        rtlsdr_cfg.get('gain', 40),
                'ppm_error':   rtlsdr_cfg.get('ppm_error', 0),
            }]

        self.first_device_serial = (
            self.device_configs[0]['serial'] if self.device_configs else '0'
        )
        self.logger.info(
            f"Configured {len(self.device_configs)} device(s): "
            f"{[d['serial'] for d in self.device_configs]}"
        )

        # Fixed channels support (up to 12 max for 3+ RTL-SDRs)
        det_cfg = config.get('detection', {})
        self._fixed_channels_enabled = det_cfg.get('fixed_channels_enable', False)
        self._fixed_channel_scantime = int(det_cfg.get('fixed_channel_scantime', 60))
        raw_fixed = det_cfg.get('fixed_channels', []) or []
        max_fixed = min(len(self.device_configs) * 4, 12)
        self._fixed_channels: List[dict] = list(raw_fixed[:max_fixed])
        self._fixed_start_done = (len(self._fixed_channels) == 0 or not self._fixed_channels_enabled)
        
        # Diagnostic logging for fixed_channels configuration
        self.logger.info(f"Fixed channels config: enabled={self._fixed_channels_enabled}, count={len(self._fixed_channels)}")
        if self._fixed_channels:
            for idx, ch in enumerate(self._fixed_channels):
                self.logger.info(
                    f"  Fixed channel {idx+1}: {ch.get('frequency')} MHz, type={ch.get('type')}, "
                    f"enabled={ch.get('enabled')}, device={ch.get('receiver_device')}"
                )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def initialize(self) -> bool:
        for idx, dev_cfg in enumerate(self.device_configs):
            worker = DeviceWorker(
                device_config=dev_cfg,
                app_config=self.config,
                sonde_registry=self._registry,
                telemetry_callback=self.telemetry_callback,
                device_index=idx,  # Pass index for staggered USB init
                manager=self  # Pass manager reference for fixed_channels check
            )
            self._workers.append(worker)
        self.logger.info(f"Initialized {len(self._workers)} worker(s)")
        return True

    def start(self):
        self.running = True
        # Start workers with delays to prevent USB bus contention
        # Each worker will open its USB device soon after starting
        for idx, w in enumerate(self._workers):
            w.start()
            self.logger.info(f"Started worker {idx+1}/{len(self._workers)}: {w.device_serial}")
            # CRITICAL: Delay between starting workers to prevent USB conflicts
            # This gives each worker time to initialize and open its USB device
            # before the next worker starts
            if idx < len(self._workers) - 1:  # Don't delay after last worker
                delay = 2.0  # 2 seconds between worker starts
                self.logger.debug(f"Waiting {delay}s before starting next worker")
                time.sleep(delay)
        self.logger.info("All device workers started")
        
        # Start fixed channels if enabled (wait for workers to stabilize)
        self.logger.debug(f"Checking fixed_channels startup: enabled={self._fixed_channels_enabled}, channels={len(self._fixed_channels)}")
        
        if self._fixed_channels_enabled and self._fixed_channels:
            self.logger.info(f"Fixed Channels enabled: {len(self._fixed_channels)} channels configured")
            threading.Thread(
                target=self._start_fixed_channels, daemon=True, name='FixedChannels-RTL'
            ).start()
        elif self._fixed_channels:
            self.logger.info("Fixed Channels configured but disabled (fixed_channels_enable: false)")
        else:
            self.logger.debug("No fixed channels configured")

    def stop(self):
        self.running = False
        for w in self._workers:
            w.stop()
        self.logger.info("All device workers stopped")

    def stop_all_decoders(self):
        """Force stop all active decoders and return devices to scanning/idle state.
        
        Used for emergency cleanup when decoders are stuck (e.g., PLL failures during priority check).
        """
        stopped_count = 0
        for w in self._workers:
            if w._state == w.STATE_DECODING:
                self.logger.info(f"Force stopping decoder on device {w.device_serial}")
                w.stop_decode_and_scan()
                stopped_count += 1
        
        if stopped_count > 0:
            self.logger.info(f"Force stopped {stopped_count} decoder(s)")
        else:
            self.logger.debug("No active decoders to stop")

    # ------------------------------------------------------------------
    # Fixed-channel startup
    # ------------------------------------------------------------------

    def _start_fixed_channels(self):
        """Decode fixed_channels list at startup with RX Scan cycling (Phase 2).
        
        Phase 2 Implementation:
        - Groups channels by device
        - Enables RX Scan cycling on each device
        - Each device cycles through its assigned channels every fixed_channel_scantime seconds
        - Conservative 3-second USB delays between device stops and starts
        """
        # Wait for ALL workers to reach SCANNING state AND complete first scan cycle
        self.logger.info("Fixed Channels: waiting for all workers to complete first scan cycle...")
        min_wait = 20  # Minimum 20 seconds (allows for staggered init + first scan)
        max_wait = 40  # Maximum 40 seconds
        start_wait = time.time()
        
        # First, wait for minimum time
        first_phase = min(min_wait, max_wait)
        while time.time() - start_wait < first_phase:
            time.sleep(1.0)
        
        elapsed = time.time() - start_wait
        self.logger.info(f"Fixed Channels: minimum wait complete ({elapsed:.1f}s)")
        
        # Then verify all workers are in SCANNING or DECODING state
        while time.time() - start_wait < max_wait:
            all_scanning = True
            for w in self._workers:
                if w.state not in (DeviceWorker.STATE_SCANNING, DeviceWorker.STATE_DECODING):
                    all_scanning = False
                    break
            
            if all_scanning:
                elapsed = time.time() - start_wait
                self.logger.info(f"Fixed Channels: all workers ready after {elapsed:.1f}s total")
                break
            
            time.sleep(1.0)
        else:
            # Timeout reached
            self.logger.warning(
                f"Fixed Channels: timeout waiting for workers (some may still be initializing)"
            )
        
        # Add final 2-second buffer to ensure USB devices are fully settled
        self.logger.info("Fixed Channels: adding 2s final buffer for USB stability...")
        time.sleep(2.0)
        
        try:
            self.logger.debug("Fixed Channels: starting channel assignment phase")
            
            # Filter for enabled channels only
            enabled_channels = [ch for ch in self._fixed_channels if ch.get('enabled', False)]
            
            if not enabled_channels:
                self.logger.info("No enabled Fixed Channels to start")
                return
            
            # Group channels by device
            device_channels = {}
            for ch in enabled_channels:
                device_id = ch.get('receiver_device', '')
                device_serial = device_id.split(':')[-1] if ':' in device_id else device_id
                if device_serial not in device_channels:
                    device_channels[device_serial] = []
                device_channels[device_serial].append(ch)
            
            # Check which channels have rx_scan enabled
            rx_scan_channels = [ch for ch in enabled_channels if ch.get('rx_scan', False)]
            continuous_channels = [ch for ch in enabled_channels if not ch.get('rx_scan', False)]
            
            self.logger.info(
                f"Fixed Channels: {len(enabled_channels)} total - "
                f"{len(rx_scan_channels)} RX Scan (cycling), "
                f"{len(continuous_channels)} continuous"
            )
            
            # Start RX Scan cycling or continuous decode per device
            success_count = 0
            skipped_count = 0
            
            for worker in self._workers:
                worker_channels = device_channels.get(worker.device_serial, [])
                
                if not worker_channels:
                    self.logger.debug(f"Device {worker.device_serial}: no fixed channels assigned")
                    continue
                
                self.logger.debug(
                    f"Device {worker.device_serial}: {len(worker_channels)} channel(s) assigned, "
                    f"current state={worker.state}"
                )
                
                # Check if ANY channel for this device has rx_scan enabled
                has_rx_scan = any(ch.get('rx_scan', False) for ch in worker_channels)
                rx_scan_count = sum(1 for ch in worker_channels if ch.get('rx_scan', False))
                continuous_count = len(worker_channels) - rx_scan_count
                
                if has_rx_scan:
                    # Phase 2: RX Scan cycling mode (at least one channel has rx_scan=true)
                    # Only include channels with rx_scan=true in rotation
                    cycling_channels = [ch for ch in worker_channels if ch.get('rx_scan', False)]
                    
                    self.logger.info(
                        f"Device {worker.device_serial}: RX Scan mode with "
                        f"{len(cycling_channels)} cycling channel(s), "
                        f"scantime={self._fixed_channel_scantime}s"
                    )
                    
                    if continuous_count > 0:
                        self.logger.warning(
                            f"Device {worker.device_serial}: {continuous_count} channel(s) with "
                            "rx_scan=false will be SKIPPED (device will cycle through rx_scan=true channels)"
                        )
                    
                    worker.enable_rx_scan(cycling_channels)
                    # Start first channel (will auto-cycle after scantime expires)
                    if worker._start_next_rx_scan_channel():
                        success_count += 1
                    else:
                        self.logger.warning(
                            f"Device {worker.device_serial}: Failed to start RX Scan"
                        )
                        skipped_count += 1
                        
                elif len(worker_channels) == 1:
                    # Phase 1: Single channel with rx_scan=false - continuous decode
                    ch = worker_channels[0]
                    freq_hz = float(ch['frequency']) * 1e6
                    stype = str(ch.get('type', 'RS41'))
                    
                    self.logger.info(
                        f"Device {worker.device_serial}: continuous decode of {stype} at "
                        f"{freq_hz/1e6:.3f} MHz (single channel, rx_scan=false)"
                    )
                    
                    try:
                        self.logger.debug(
                            f"Calling start_manual_decode() for {worker.device_serial}: "
                            f"freq={freq_hz/1e6:.3f} MHz, type={stype}"
                        )
                        result = worker.start_manual_decode(freq_hz, stype, duration_seconds=None)
                        self.logger.debug(
                            f"start_manual_decode() returned: {result} for {worker.device_serial}"
                        )
                        
                        if result:
                            success_count += 1
                        else:
                            self.logger.warning(
                                f"Device {worker.device_serial}: Failed to start decoder (returned False)"
                            )
                            skipped_count += 1
                    except Exception as e:
                        self.logger.error(
                            f"Device {worker.device_serial}: Exception starting decoder: {e}",
                            exc_info=True
                        )
                        skipped_count += 1
                        
                else:
                    # Multiple channels, all with rx_scan=false - CONFLICT!
                    # Can only decode one frequency at a time - start first, skip rest
                    ch = worker_channels[0]
                    freq_hz = float(ch['frequency']) * 1e6
                    stype = str(ch.get('type', 'RS41'))
                    
                    self.logger.warning(
                        f"Device {worker.device_serial}: {len(worker_channels)} channels configured "
                        "with rx_scan=false, but can only decode ONE at a time. "
                        f"Starting first channel ({freq_hz/1e6:.3f} MHz), "
                        f"skipping {len(worker_channels)-1} other(s). "
                        "Set rx_scan=true to enable cycling."
                    )
                    
                    try:
                        self.logger.debug(
                            f"Calling start_manual_decode() for {worker.device_serial}: "
                            f"freq={freq_hz/1e6:.3f} MHz, type={stype}"
                        )
                        result = worker.start_manual_decode(freq_hz, stype, duration_seconds=None)
                        self.logger.debug(
                            f"start_manual_decode() returned: {result} for {worker.device_serial}"
                        )
                        
                        if result:
                            success_count += 1
                            skipped_count += len(worker_channels) - 1
                        else:
                            self.logger.warning(
                                f"Device {worker.device_serial}: Failed to start decoder (returned False)"
                            )
                            skipped_count += len(worker_channels)
                    except Exception as e:
                        self.logger.error(
                            f"Device {worker.device_serial}: Exception starting decoder: {e}",
                            exc_info=True
                        )
                        skipped_count += len(worker_channels)
                
                # CRITICAL: 3-second delay between device starts to prevent USB conflicts
                time.sleep(3.0)
            
            self.logger.info(
                f"Fixed Channels startup complete: {success_count}/{len(self._workers)} "
                f"device(s) started, {skipped_count} channel(s) skipped"
            )
        
        except Exception as e:
            self.logger.error(
                f"Fatal error in Fixed Channels startup: {e}",
                exc_info=True
            )
                
        finally:
            self._fixed_start_done = True



    # ------------------------------------------------------------------
    # web_server.py compatible API
    # ------------------------------------------------------------------

    @property
    def active_decoders(self) -> Dict[float, ActiveDecoder]:
        """Return {frequency: ActiveDecoder} for all currently-decoding workers."""
        result = {}
        for w in self._workers:
            ad = w.get_active_decoder()
            if ad:
                result[ad.signal.frequency] = ad
        return result

    def start_manual_decoder(self, frequency: float, sonde_type: str,
                           duration_seconds: Optional[float] = None) -> bool:
        """Start a manual decoder on the first non-decoding worker (web UI).
        
        Args:
            frequency: Target frequency in Hz
            sonde_type: Sonde type (RS41, RS92, etc.)
            duration_seconds: If set, auto-return to scanning after this many seconds.
        """
        # Prefer a worker already in SCANNING state (SDR already warm)
        candidates = sorted(
            self._workers,
            key=lambda w: 0 if w.state == DeviceWorker.STATE_SCANNING else 1
        )
        for w in candidates:
            if w.state != DeviceWorker.STATE_DECODING:
                duration_label = 'infinite' if not duration_seconds or duration_seconds <= 0 else f'{int(duration_seconds)}s'
                self.logger.info(
                    f"Manual {sonde_type} at {frequency/1e6:.3f} MHz "
                    f"({duration_label}) → device {w.device_serial}"
                )
                return w.start_manual_decode(frequency, sonde_type, duration_seconds)
        self.logger.warning("No available worker for manual decoder")
        return False

    def start_manual_decoder_on(self, frequency: float, sonde_type: str,
                                device_serial: str = None,
                                duration_seconds: Optional[float] = None) -> bool:
        """Start a manual decoder targeting a specific device (or auto-select).
        
        Args:
            frequency: Target frequency in Hz
            sonde_type: Sonde type (RS41, RS92, etc.)
            device_serial: Target device serial (None = auto-select first available)
            duration_seconds: If set, auto-return to scanning after this many seconds.
        """
        if not device_serial:
            return self.start_manual_decoder(frequency, sonde_type, duration_seconds)
        for w in self._workers:
            if w.device_serial == device_serial:
                if w.state == DeviceWorker.STATE_DECODING:
                    self.logger.warning(
                        f"Device {device_serial} already decoding; "
                        "cannot start another manual decoder on it"
                    )
                    return False
                duration_label = 'infinite' if not duration_seconds or duration_seconds <= 0 else f'{int(duration_seconds)}s'
                self.logger.info(
                    f"Manual {sonde_type} at {frequency/1e6:.3f} MHz "
                    f"({duration_label}) → device {device_serial} (explicit)"
                )
                return w.start_manual_decode(frequency, sonde_type, duration_seconds)
        self.logger.warning(f"Device {device_serial} not found in worker list")
        return False

    def get_worker_status(self) -> List[dict]:
        """Per-worker state summary for web UI /api/devices."""
        result = []
        for w in self._workers:
            if w.state == 'decoding' and w.current_freq:
                freq_mhz   = w.current_freq / 1e6
                freq_label = f"{freq_mhz:.3f} MHz"
            elif w.state == 'scanning':
                cf         = w.device_config.get('center_freq', 0)
                sr         = w.device_config.get('sample_rate', 2_400_000)
                low_mhz    = (cf - sr / 2) / 1e6
                high_mhz   = (cf + sr / 2) / 1e6
                freq_mhz   = cf / 1e6
                freq_label = f"{low_mhz:.1f}-{high_mhz:.1f} MHz"
            else:
                freq_mhz   = None
                freq_label = None
            result.append({
                'serial':     w.device_serial,
                'state':      w.state,
                'frequency':  freq_mhz,
                'freq_label': freq_label,
                'sonde_type': w.current_sonde_type,
            })
        return result

    def get_spectrum_receivers(self) -> List[dict]:
        """Return selectable spectrum receiver list for web UI."""
        return [
            {
                'id': f"rtlsdr:{w.device_serial}",
                'name': f"RTL-SDR {w.device_serial}",
            }
            for w in self._workers
        ]

    def get_spectrum_for_receiver(self, receiver_id: str) -> dict:
        """Return latest spectrum for selected receiver id (rtlsdr:<serial>)."""
        target = receiver_id or ''
        if not target.startswith('rtlsdr:'):
            target = self.get_spectrum_receivers()[0]['id'] if self._workers else ''

        serial = target.split(':', 1)[1] if ':' in target else ''
        for w in self._workers:
            if w.device_serial == serial:
                spec = w.get_spectrum()
                if spec:
                    return spec
                return {
                    'receiver_id': target,
                    'receiver_name': f"RTL-SDR {serial}",
                    'freqs_mhz': [],
                    'power_db': [],
                    'signals': [],
                    'timestamp': datetime.utcnow().isoformat() + 'Z',
                }

        return {}
    
    # ------------------------------------------------------------------
    # Runtime configuration (for web UI)
    # ------------------------------------------------------------------
    
    def set_debug_mode(self, enabled: bool):
        """Enable/disable debug logging at runtime."""
        # Propagate to all workers
        for w in self._workers:
            # Workers inherit logger from parent, no need to propagate
            pass
    
    def set_snr_threshold(self, threshold_db: float):
        """Update SNR detection threshold at runtime (currently not used by RTL-SDR mode)."""
        self.logger.info(f"SNR threshold updated to {threshold_db} dB (note: RTL-SDR uses fixed detection threshold)")
    
    def set_scan_interval(self, seconds: float):
        """Update scan interval at runtime (currently not dynamically adjustable)."""
        self.logger.info(f"Scan interval updated to {seconds}s (note: requires restart to apply)")
    
    def set_fixed_channel_scantime(self, seconds: int):
        """Update Fixed Channel scan time (Phase 2: RX Scan cycling duration)."""
        self._fixed_channel_scantime = seconds
        self.logger.info(f"Fixed Channel scantime set to {seconds}s (will be used in Phase 2 RX Scan)")
    
    def get_runtime_config(self) -> dict:
        """Return current runtime configuration."""
        det_cfg = self.config.get('detection', {})
        rcv_cfg = self.config.get('receivers', {})
        log_cfg = self.config.get('logging', {})
        
        return {
            'debug_mode': bool(log_cfg.get('debug_mode', False)),
            'debug_level': str(log_cfg.get('debug_level', 'basic')),
            'snr_threshold': float(det_cfg.get('detection_threshold', 18.0)),
            'scan_interval': int(rcv_cfg.get('scan_interval', 15)),
            'fixed_channel_scantime': getattr(self, '_fixed_channel_scantime', 
                                             int(det_cfg.get('fixed_channel_scantime', 60))),
        }
