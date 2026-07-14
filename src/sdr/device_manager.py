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
import os
import queue
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
from ..import_api.sonde_api_client import SondeApiClient


# ---------------------------------------------------------------------------
# Shared sonde frequency registry
# ---------------------------------------------------------------------------

class SondeRegistry:
    """
    Thread-safe set of frequencies that are currently being decoded.
    Uses a tolerance window so small FFT drift doesn't cause double-decoding.
    """

    TOLERANCE_HZ = 20_000   # 20 kHz minimum gap between decoders

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
    STATE_ERROR    = 'error'  # Device open/test-read failed; not actually scanning

    # ~2-3 minutes of retries (mix of 10s/15s backoffs) before giving up on a
    # device and self-restarting the whole service. LIBUSB_ERROR_BUSY has never
    # been observed to self-recover within the process.
    MAX_CONSECUTIVE_OPEN_FAILURES = 10

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
        # Consecutive failed device-open attempts (init failure, test-read timeout/
        # error/zero-samples). Once a device hits LIBUSB_ERROR_BUSY it has never been
        # observed to recover within the process — the USB interface claim is leaked
        # by a thread stuck in the C extension and can only be released by the kernel
        # when the process exits. Tracked so we can self-restart via systemd instead
        # of polling a permanently dead device forever. Reset on any successful open.
        self._consecutive_open_failures = 0
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
        self._cur_serial: Optional[str] = None
        self._cur_signal_strength_db: Optional[float] = None
        self._decode_start   = 0.0
        self._last_frame_t   = 0.0
        self._last_state     = self.STATE_IDLE  # Track previous state for transition delays

        # Timing
        cfg_rx = app_config.get('receivers', {})
        cfg_dec = app_config.get('decoders', {})
        self._scan_interval  = cfg_rx.get('scan_interval', 15)
        self._idle_timeout   = cfg_dec.get('max_idle_time', 300)   # seconds without frames → back to scan
        # CRITICAL: manual/imported decoders with duration=None ("decode until
        # sonde lost") previously had NO staleness check at all — only the
        # decoder subprocess dying released the device. If the process keeps
        # running without ever producing another valid frame (sonde landed,
        # out of range, etc.), the device stayed stuck in DECODING forever.
        # Give these a much longer, separately configurable idle timeout
        # instead of none at all.
        self._manual_idle_timeout = cfg_dec.get('manual_idle_time', 1800)  # 30 min default
        self._decode_expiration_time: Optional[float] = None  # For duration-limited decoding
        self._is_manual_decoder: bool = False  # Manual decoders ignore the short auto-detect idle timeout

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

        # Channelizer mode (Step 3: Device Manager Integration)
        self._decoder_mode = device_config.get('decoder_mode', 'legacy')
        self._channelizer: Optional['IqDecChannelizer'] = None  # Will be instantiated if mode='channelizer'
        self._channelizer_manual_requests = queue.Queue()  # Manual decode requests for channelizer mode
        if self._decoder_mode == 'channelizer':
            # Import here to avoid circular dependency
            from .channelizer import IqDecChannelizer
            self._channelizer = IqDecChannelizer(
                device_config, app_config, telemetry_callback, self.device_serial, device_index
            )
            self.logger.info(f"Channelizer mode enabled (max {self._channelizer.max_channels} channels)")
        else:
            self.logger.info(f"Legacy mode enabled (single-channel)")

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
        # Cleanup based on decoder mode
        if self._decoder_mode == 'channelizer' and self._channelizer:
            self.logger.info("Stopping channelizer")
            self._channelizer.stop()
        else:
            # Legacy mode cleanup
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

    @property
    def current_sonde_serial(self) -> Optional[str]:
        return self._cur_serial

    @property
    def decoder_mode(self) -> str:
        """Return decoder mode: 'legacy' or 'channelizer'"""
        return self._decoder_mode

    @property
    def channelizer_active_channels(self) -> int:
        """Return number of active channelizer channels (0 if legacy mode)"""
        if self._channelizer:
            return self._channelizer.get_channel_count()
        return 0

    @property
    def channelizer_max_channels(self) -> int:
        """Return max channelizer channels (0 if legacy mode)"""
        if self._channelizer:
            return self._channelizer.max_channels
        return 0
    
    def get_channelizer_channel_details(self) -> List[dict]:
        """
        Get detailed info about active channelizer channels for status output.
        
        Returns:
            List of dicts with: frequency, sonde_type, sonde_serial, snr
        """
        if not self._channelizer:
            return []
        
        channels = self._channelizer.get_active_channels()
        result = []
        for ch in channels:
            result.append({
                'frequency': ch.frequency,
                'sonde_type': ch.sonde_type,
                'sonde_serial': ch.sonde_serial or 'N/A',
                'snr': 0.0  # TODO: Add SNR tracking to ChannelInfo
            })
        return result

    def get_scan_return_eta_s(self) -> Optional[float]:
        """Seconds remaining until this device returns to scanning if no more
        valid frames arrive, or None if not currently decoding. Mirrors the
        exact timeout branches in _decode_cycle() so the web UI countdown
        matches what the worker will actually do:
          - duration-limited decode (_decode_expiration_time set): hard
            deadline, independent of idle time.
          - manual/imported decoder with no duration (decode until lost):
            idle-based, using the longer _manual_idle_timeout.
          - auto-detected decoder: idle-based, using _idle_timeout.
        """
        if self._state != self.STATE_DECODING:
            return None

        now = time.time()

        if self._decode_expiration_time is not None:
            return max(0.0, self._decode_expiration_time - now)

        timeout = self._manual_idle_timeout if self._is_manual_decoder else self._idle_timeout
        last_activity = self._last_frame_t if self._last_frame_t else self._decode_start
        elapsed = now - last_activity
        return max(0.0, timeout - elapsed)

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
            has_data = bool(self._last_spectrum)
            if has_data:
                self.logger.debug(f"get_spectrum() returning data: {len(self._last_spectrum.get('freqs_mhz', []))} points")
            else:
                self.logger.warning(f"get_spectrum() returning EMPTY dict for {self.device_serial}")
            return dict(self._last_spectrum)

    def _update_spectrum_snapshot(self, freqs, power_db, signals: List[DetectedSignal]):
        """Build a compact spectrum payload for the web UI."""
        try:
            import numpy as np

            if freqs is None or power_db is None or len(freqs) == 0:
                self.logger.warning(f"_update_spectrum_snapshot() called with empty data: freqs={freqs is not None}, power_db={power_db is not None}, len={len(freqs) if freqs is not None else 0}")
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
            self.logger.debug(f"Spectrum snapshot updated: {len(spec['freqs_mhz'])} points, {len(signals)} signals, receiver_id={spec['receiver_id']}")
        except Exception as exc:
            self.logger.error(f"Failed to update spectrum snapshot: {exc}", exc_info=True)

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
        # Channelizer mode: Add manual decode request to queue
        if self._decoder_mode == 'channelizer':
            self.logger.info(
                f"Manual decode (channelizer): {sonde_type} at {frequency/1e6:.3f} MHz"
            )
            try:
                self._channelizer_manual_requests.put({
                    'frequency': frequency,
                    'sonde_type': sonde_type,
                    'duration_seconds': duration_seconds
                }, timeout=1.0)
                return True
            except queue.Full:
                self.logger.error("Manual decode queue full (channelizer mode)")
                return False
        
        # Legacy mode: Use state machine approach
        # Signal the scan cycle to stop ASAP so we can acquire the lock quickly
        self._manual_decode_pending.set()
        self.logger.info(f"Manual decode: waiting for device lock on {self.device_serial}...")

        # CRITICAL: _manual_decode_pending must stay set for the ENTIRE manual-decode
        # sequence (lock wait, settle sleeps, _start_decode's own teardown/DFT/rtl_fm
        # startup) — not just until we first grab the lock. _scan_cycle() checks this
        # flag at several points to back off; clearing it early lets an in-flight scan
        # cycle race _start_decode() against ours for the same physical USB device
        # (observed as concurrent rtl_fm/dft_detect processes fighting over the same
        # RTL-SDR dongle, causing "rtl_fm exit: 1" / "dft_detect exit code 206").
        try:
            # CRITICAL: Acquire device lock to prevent race with worker thread.
            # Use a timeout so we get an error log instead of blocking forever if the
            # scan cycle is stuck (e.g. read_samples() hang on a USB error).
            if not self._device_lock.acquire(timeout=20.0):
                self.logger.error(
                    f"Manual decode: could not acquire device lock on {self.device_serial} "
                    f"after 20 s — scan cycle may be stuck"
                )
                return False

            try:
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
                # Manual decodes use force_override=True to take over from auto-detect
                success = self._start_decode(sig, override_type=sonde_type, force_override=True)
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
        finally:
            self._manual_decode_pending.clear()

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
        """Main worker loop - dispatches to legacy or channelizer mode."""
        # Step 3: Check decoder mode and dispatch accordingly
        if self._decoder_mode == 'channelizer':
            self.logger.info(f"Starting channelizer mode for {self.device_serial}")
            self._run_channelizer()
        else:
            self.logger.info(f"Starting legacy mode for {self.device_serial}")
            self._run_legacy()

    def _run_legacy(self):
        """Legacy single-channel scan → decode loop (original behavior)."""
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

    def _run_channelizer(self):
        """
        Channelizer mode: Multi-channel scan and decode using iq_server/iq_client.
        
        This replaces the legacy scan→decode state machine with a persistent
        iq_server process that handles multiple sondes simultaneously via
        iq_client | iq_fm | decoder pipelines.
        
        Status: STEP 5 - Full iq_server integration with manual decode support
        """
        self.logger.info("Channelizer mode starting")
        
        if not self._channelizer:
            self.logger.error("Channelizer not initialized, falling back to idle")
            time.sleep(10)
            return
        
        # Start iq_server process
        if not self._channelizer.start():
            self.logger.error("Failed to start channelizer, returning to idle")
            time.sleep(10)
            return
        
        self.logger.info(f"Channelizer started: {self._channelizer.max_channels} max channels")
        
        try:
            while self._running:
                try:
                    # Check for manual decode requests
                    try:
                        request = self._channelizer_manual_requests.get(block=False)
                        frequency = request['frequency']
                        sonde_type = request['sonde_type']
                        duration_seconds = request.get('duration_seconds')
                        
                        self.logger.info(
                            f"Processing manual decode: {sonde_type} at {frequency/1e6:.3f} MHz"
                        )
                        
                        # Check if channel has capacity
                        if not self._channelizer.has_capacity():
                            self.logger.warning("Channelizer at capacity, cannot start manual decode")
                        else:
                            # Start channel
                            if self._channelizer.start_channel(frequency, sonde_type):
                                self.logger.info(
                                    f"Manual decode started: {sonde_type} at {frequency/1e6:.3f} MHz"
                                )
                            else:
                                self.logger.error(
                                    f"Failed to start manual decode: {sonde_type} at {frequency/1e6:.3f} MHz"
                                )
                    
                    except queue.Empty:
                        pass  # No manual requests pending
                    
                    # Monitor active channels
                    active_channels = self._channelizer.get_active_channels()
                    if active_channels:
                        self.logger.debug(
                            f"Channelizer: {len(active_channels)}/{self._channelizer.max_channels} channels active"
                        )
                    
                    # TODO (Future steps):
                    # - Automatic spectrum scanning for signal detection
                    # - Auto-start channels for detected sondes
                    # - Monitor channel health and stop inactive channels
                    # - Implement channel timeout/rotation logic
                    
                    # Update interval: check status every 2 seconds
                    time.sleep(2)
                    
                except Exception as e:
                    self.logger.error(f"Channelizer loop error: {e}", exc_info=True)
                    time.sleep(5)
        
        finally:
            # Clean shutdown
            self.logger.info("Channelizer shutting down")
            self._channelizer.stop()

    # ------------------------------------------------------------------
    # Scanning
    # ------------------------------------------------------------------

    def _note_open_failure(self):
        """Record a failed device-open/test-read attempt. Once a device has been
        stuck for MAX_CONSECUTIVE_OPEN_FAILURES retries in a row, self-restart the
        whole service instead of polling a permanently dead device forever — the
        leaked USB interface claim can only be released by the kernel when this
        process exits. systemd (Restart=on-failure, RestartSec=10) brings it back
        up cleanly and releases every device, not just this one."""
        self._consecutive_open_failures += 1
        # CRITICAL: _state defaults to STATE_SCANNING at construction and was
        # otherwise only ever written on a *successful* open, so a device stuck
        # failing to open forever kept reporting "Scanning" (green) to the web UI
        # with no way to tell it apart from a genuinely healthy scanner. Surface
        # the failure so /api/devices and other w.state consumers see it.
        self._state = self.STATE_ERROR
        if self._consecutive_open_failures >= self.MAX_CONSECUTIVE_OPEN_FAILURES:
            self.logger.critical(
                f"Device {self.device_serial} failed to open "
                f"{self._consecutive_open_failures} times in a row and is not "
                "recovering — restarting the whole service to release the stuck "
                "USB interface (systemd Restart=on-failure will bring it back up)"
            )
            os._exit(1)

    def _note_open_success(self):
        self._consecutive_open_failures = 0

    def _scan_cycle(self):
        """Open the RTL-SDR, capture one spectrum, look for new signals."""
        # CRITICAL: Acquire device lock only for state changes and analyzer init
        # Do NOT hold lock during spectrum capture or signal processing
        
        # Check if manual decode is pending before doing any work
        if self._manual_decode_pending.is_set():
            time.sleep(0.5)
            return
        
        # Initialize analyzer if needed (with lock protection)
        if self._analyzer is None:
            # If we just transitioned from DECODING, wait for USB device to be fully released
            # Increased from 3s to 5s for better USB/PLL stability on some Raspberry Pi systems
            # Check flag instead of _last_state because state goes DECODING → IDLE → SCANNING
            if hasattr(self, '_usb_reopen_after_decode') and self._usb_reopen_after_decode:
                self.logger.debug("Waiting 5s for USB device to settle after DECODING")
                time.sleep(5.0)
                self._usb_reopen_after_decode = False
            
            # CRITICAL: Stagger first USB device open to prevent simultaneous access
            # This prevents "[R82XX] PLL not locked!" errors from USB bus contention.
            # Device 0 previously got the SHORTEST delay ((index+1)*2.5 = 2.5s) even
            # though it's the one opened earliest — right when the USB subsystem is
            # least settled after a fresh service (re)start. Observed in the field:
            # on 4x RTL-SDR systems (even with a powered hub), device 0 consistently
            # failed its test read while devices 1-3 (5s/7.5s/10s) succeeded reliably
            # — this was fully deterministic, not random flakiness, so it reproduced
            # on every single restart (the auto-restart-after-N-failures safety net
            # doesn't help here since restarting hits the exact same race again).
            # Give every device the same baseline settle time device 1 already had,
            # then stagger on top of that.
            if self._first_usb_init:
                stagger_delay = 5.0 + self.device_index * 2.5  # 5s for device 0, 7.5s for device 1, etc.
                self.logger.info(f"First USB init: waiting {stagger_delay:.1f}s for USB/PLL stabilization")
                time.sleep(stagger_delay)
                self._first_usb_init = False
            
            # Acquire lock for analyzer initialization
            if not self._device_lock.acquire(blocking=False):
                self.logger.debug(f"Device lock held (manual decoder active), skipping scan cycle")
                time.sleep(0.5)
                return
            
            try:
                self._analyzer = SpectrumAnalyzer(self.app_config, self.device_config, self._blacklist)
                if not self._analyzer.initialize():
                    self.logger.error("Cannot open RTL-SDR — retrying in 15 s")
                    self._analyzer = None
                    self._note_open_failure()
                    # Release lock BEFORE long sleep
                    self._device_lock.release()
                    time.sleep(15)
                    return
                
                # CRITICAL: Test device immediately after init to catch PLL lock failures
                # Use thread-based timeout since signal.alarm() cannot interrupt C extensions
                self.logger.debug(f"Testing device {self.device_serial} with quick read...")
                test_result = {'samples': None, 'error': None}
                
                def test_read_worker():
                    """Worker thread for test read - can be abandoned if it hangs."""
                    try:
                        test_result['samples'] = self._analyzer.sdr.read_samples(2048)
                    except Exception as e:
                        test_result['error'] = e
                
                # Run test read in separate thread with 5-second timeout
                test_thread = threading.Thread(target=test_read_worker, daemon=True)
                test_thread.start()
                test_thread.join(timeout=5.0)
                
                # Check if test read completed or timed out
                if test_thread.is_alive():
                    # Thread is still running - read_samples() is hung
                    self.logger.error(
                        f"Device {self.device_serial} test read TIMEOUT after 5s. "
                        "USB read is blocked - likely PLL lock failure. Abandoning device and will retry."
                    )
                    # Cannot safely close analyzer while thread is blocked - just abandon it.
                    # NOTE: a background close() was tried here and reverted — calling
                    # close() from another thread while test_read_worker is still blocked
                    # inside librtlsdr's C extension on the same handle is not thread-safe
                    # and caused a SIGSEGV in production. Leaking the handle (USB interface
                    # stays busy until process restart) is safer than crashing the service.
                    self._analyzer = None
                    self._note_open_failure()
                    self._device_lock.release()
                    time.sleep(15)  # Longer wait before retry
                    return
                elif test_result['error'] is not None:
                    # Test read threw an exception
                    self.logger.error(
                        f"Device {self.device_serial} test read FAILED: {test_result['error']}. "
                        "Closing and will retry."
                    )
                    self._analyzer.close()
                    self._analyzer = None
                    self._note_open_failure()
                    self._device_lock.release()
                    time.sleep(10)
                    return
                elif test_result['samples'] is None or len(test_result['samples']) == 0:
                    # Test read completed but returned no data
                    self.logger.error(
                        f"Device {self.device_serial} returned zero samples. "
                        "Closing and will retry."
                    )
                    self._analyzer.close()
                    self._analyzer = None
                    self._note_open_failure()
                    self._device_lock.release()
                    time.sleep(10)
                    return

                # Test read succeeded
                self.logger.info(f"Device {self.device_serial} test read OK ({len(test_result['samples'])} samples)")
                self._note_open_success()

                self._state = self.STATE_SCANNING
                self.logger.info(
                    f"Scanning {self.device_config['center_freq']/1e6:.1f} MHz "
                    f"±{self.device_config['sample_rate']/2e6:.1f} MHz"
                )
            finally:
                # Release lock after analyzer init
                if self._device_lock.locked():
                    self._device_lock.release()

        # Capture spectrum WITHOUT holding the lock - this can take several seconds
        # Manual decode can interrupt by setting _manual_decode_pending flag
        try:
            self.logger.debug(f"Starting capture_spectrum() for {self.device_serial}...")
            freqs, power_db = self._analyzer.capture_spectrum()
            self.logger.debug(f"capture_spectrum() completed for {self.device_serial}: {len(freqs) if freqs is not None else 0} points")
            signals = self._analyzer.detect_signals(freqs, power_db)
            signals = self._analyzer.filter_signals_in_ranges(signals)
            self._update_spectrum_snapshot(freqs, power_db, signals)
        except Exception as exc:
            self.logger.error(f"Spectrum capture failed for {self.device_serial}: {exc}", exc_info=True)
            # Acquire lock to tear down scanner
            with self._device_lock:
                self._teardown_scan()
            time.sleep(5)
            return

        # Abort if manual decode was requested during spectrum capture
        if self._manual_decode_pending.is_set():
            return

        # Process signals WITHOUT holding the lock
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
            
            # Check one more time before starting decode
            if self._manual_decode_pending.is_set():
                return
            
            self.logger.info(
                f"New signal at {sig.frequency/1e6:.4f} MHz "
                f"(SNR {sig.strength:.1f} dB, BW {sig.bandwidth/1e3:.1f} kHz)"
            )
            self._start_decode(sig)
            return   # worker will re-enter loop in DECODING state

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

        if self._is_manual_decoder:
            # Manual/imported decoders with an explicit duration (e.g. priority
            # frequency checks) are governed purely by their expiration timer
            # below, regardless of idle time — unchanged from before.
            # Decoders with duration=None ("decode until sonde lost", e.g.
            # Import API assignments) have no expiration timer at all, so they
            # need their OWN staleness check here — otherwise a decoder whose
            # subprocess keeps running without ever producing another frame
            # ties up the device forever.
            if self._decode_expiration_time is None and self._decoder.is_idle(self._manual_idle_timeout):
                self.logger.info(
                    f"Manual/imported decoder idle for >{self._manual_idle_timeout}s — back to scan"
                )
                self._teardown_decode()
                return
        elif self._decoder.is_idle():
            self.logger.info(
                f"Decoder idle for >{self._idle_timeout}s — back to scan"
            )
            self._teardown_decode()
            return

        # Check if duration-limited decode has expired
        if self._decode_expiration_time is not None:
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
                      override_type: Optional[str] = None,
                      force_override: bool = False) -> bool:
        # Atomically claim the frequency in the shared registry
        # For manual decodes with force_override, unregister first to allow takeover
        if force_override:
            self.logger.info(f"Manual decode force override: unclaiming {sig.frequency/1e6:.4f} MHz")
            self.registry.unregister(sig.frequency)
        
        # Store original frequency for registry updates
        original_freq = sig.frequency
        
        if not self.registry.register(original_freq):
            self.logger.info(
                f"{sig.frequency/1e6:.4f} MHz already claimed (within ±{self.registry.TOLERANCE_HZ/1000:.0f} kHz) — continuing scan"
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
        sonde_offset = 0.0  # Frequency offset from DFT detection
        if override_type:
            sonde_type = override_type
            self.logger.info(f"Manual decode: using override type {sonde_type} at {sig.frequency/1e6:.4f} MHz")
        else:
            # CRITICAL: Check for manual decode before DFT (which takes 7+ seconds)
            # This allows import API or user requests to interrupt auto-detection
            if hasattr(self, '_manual_decode_pending') and self._manual_decode_pending.is_set():
                self.logger.info(f"Aborting auto-detection at {sig.frequency/1e6:.4f} MHz - manual decode pending")
                self.registry.unregister(sig.frequency)
                return False
            
            # _identify_sonde_type returns (sonde_type, frequency_offset)
            result = self._identify_sonde_type(sig)
            if isinstance(result, tuple) and len(result) > 1:
                sonde_type, sonde_offset = result
            else:
                # Legacy single return value
                sonde_type = result
                sonde_offset = 0.0
            
            if not sonde_type:
                self.logger.error(f"Failed to identify sonde type at {sig.frequency/1e6:.4f} MHz")
                self.registry.unregister(sig.frequency)
                return False
        
        # Apply frequency correction from DFT detection (critical for M10/M20)
        # Quantize to 1 kHz to avoid rtl_fm frequency jitter
        if sonde_offset != 0.0:
            corrected_freq = round((sig.frequency + sonde_offset) / 1000.0) * 1000.0
            self.logger.info(
                f"Applying DFT frequency correction: {sig.frequency/1e6:.4f} MHz "
                f"+ {sonde_offset:+.1f} Hz → {corrected_freq/1e6:.4f} MHz"
            )
            sig.frequency = corrected_freq
        else:
            # No DFT offset, just quantize to 1 kHz
            sig.frequency = round(sig.frequency / 1000.0) * 1000.0
        
        # CRITICAL: If frequency changed after correction, move the registry claim
        # from original_freq to the corrected frequency. Unregister BEFORE checking/
        # registering the corrected value — checking is_active() while still holding
        # the original_freq claim is a self-collision bug: the correction (even plain
        # 1 kHz quantization with no real DFT offset) is almost always well within
        # TOLERANCE_HZ (20 kHz) of original_freq, so is_active() kept finding this
        # call's own just-registered entry and aborting essentially every auto-detected
        # decode that received any correction at all.
        if sig.frequency != original_freq:
            self.registry.unregister(original_freq)
            if not self.registry.register(sig.frequency):
                self.logger.warning(
                    f"After DFT correction, {sig.frequency/1e6:.4f} MHz is already being decoded "
                    f"(within ±{self.registry.TOLERANCE_HZ/1000:.0f} kHz) — aborting"
                )
                return False
            self.logger.debug(f"Updated registry: {original_freq/1e6:.4f} → {sig.frequency/1e6:.4f} MHz")
        
        # Check decoder cooldown to prevent tight failure loops
        decoder_path = self._get_decoder_path(sonde_type)
        if decoder_path and not RS1729Decoder.should_retry_decoder(decoder_path, sonde_type, cooldown_seconds=60):
            self.logger.warning(
                f"Decoder {sonde_type} in cooldown after recent failures, "
                f"skipping {sig.frequency/1e6:.4f} MHz"
            )
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
        self._cur_serial = None
        self._cur_signal_strength_db = None
        was_manual = self._is_manual_decoder
        self._is_manual_decoder = False
        self._state     = self.STATE_IDLE
        
        # Mark that we need to wait before next USB reopen (prevents PLL lock failures)
        self._usb_reopen_after_decode = True
        
        # Critical: Wait for USB device to fully release after rtl_fm/decoder stop
        # Increased from 2s to 5s for better USB/PLL stability on some Raspberry Pi systems
        # Without this delay, reopening RTL-SDR too quickly causes PLL lock failures
        # and corrupted IQ data (exit code 206 in dft_detect)
        self.logger.debug("Waiting 5s for USB device to settle after decoder stop...")
        time.sleep(5.0)
        
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

    def _identify_sonde_type(self, sig: DetectedSignal) -> tuple:
        """
        Identify sonde type, returning (sonde_type, frequency_offset).
        
        Returns:
            Tuple of (sonde_type, offset_hz) where offset_hz is frequency correction from DFT
            or (sonde_type, 0.0) if using bandwidth fallback
        """
        sonde_offset = 0.0
        
        if self._dft and self._dft.available:
            try:
                result = self._dft.detect_sonde_type(
                    frequency=sig.frequency,
                    device_serial=self.device_serial,
                    sample_rate=48000,
                    bandwidth=sig.bandwidth
                )
                if result:
                    # DFT returns tuple: (sonde_type, frequency_offset)
                    if isinstance(result, tuple) and len(result) > 1:
                        sonde_type, sonde_offset = result[0], result[1]
                        self.logger.info(
                            f"DFT identified {sonde_type} at {sig.frequency/1e6:.4f} MHz "
                            f"(offset: {sonde_offset:+.1f} Hz)"
                        )
                        return sonde_type, sonde_offset
                    else:
                        # Legacy single-value return
                        self.logger.info(
                            f"DFT identified {result} at {sig.frequency/1e6:.4f} MHz"
                        )
                        return result, 0.0
                self.logger.info("DFT: no confident match — using bandwidth fallback")
            except Exception as exc:
                self.logger.warning(f"DFT detection error: {exc}")
        
        # Bandwidth fallback returns no offset
        return self._bandwidth_fallback(sig), 0.0

    def _bandwidth_fallback(self, sig: DetectedSignal) -> str:
        """Classify sonde type by bandwidth with confidence-aware ambiguous zone handling."""
        bw   = sig.bandwidth
        freq = sig.frequency
        
        if 400e6 <= freq <= 406e6:
            # Clear boundaries with high confidence
            if bw >= 22000:   return 'M20'
            if bw >= 16000:   return 'iMet'
            if bw >= 14000:   return 'M10'
            if bw >= 12000:
                self.logger.info(f"BW {bw/1e3:.1f} kHz → RS92 (confident)")
                return 'RS92'

            # CRITICAL: previously this whole 6.5-10 kHz band defaulted to RS41,
            # which made the 'bw >= 10000: DFM' branch below only reachable for a
            # narrow 10-12 kHz sliver — but DFM's own typical range (per the note
            # below) is 7.5-8.5 kHz, i.e. squarely inside the old RS41 default.
            # That meant a real DFM signal got misclassified as RS41 almost every
            # time dft_detect failed to return a confident correlation match
            # (which was happening on ~100% of calls due to a CLI-argument-format
            # mismatch with the installed dft_detect build — see dft_detector.py).
            # RS41 typically 4.8-6 kHz, can drift to 7-7.5 kHz.
            # DFM typically 7.5-8.5 kHz, can vary up toward 10 kHz.
            # Split the ambiguous zone at 7.5 kHz so each type's *typical* range
            # is favored, instead of defaulting the whole band to one type.
            if 6500 <= bw < 7500:
                self.logger.warning(
                    f"BW {bw/1e3:.1f} kHz in ambiguous zone (6.5-7.5 kHz) "
                    f"→ RS41 (typical drift range, may be DFM)"
                )
                return 'RS41'

            if 7500 <= bw < 10000:
                self.logger.warning(
                    f"BW {bw/1e3:.1f} kHz in ambiguous zone (7.5-10 kHz) "
                    f"→ DFM (typical range, may be drifted RS41)"
                )
                return 'DFM'

            if bw >= 10000:
                # Strong DFM indicator above 10 kHz
                self.logger.info(f"BW {bw/1e3:.1f} kHz → DFM (high confidence)")
                return 'DFM'

            # Below 6.5 kHz: clear RS41
            self.logger.info(f"BW {bw/1e3:.1f} kHz → RS41 (confident)")
            return 'RS41'
        
        return 'RS41'

    def _is_blacklisted(self, freq_hz: float) -> bool:
        """Check if frequency is blacklisted (±2.5 kHz tolerance)"""
        return any(abs(freq_hz - b) < 2_500 for b in self._blacklist)
    
    def _get_decoder_path(self, sonde_type: str) -> Optional[str]:
        """Get decoder path for a sonde type."""
        decoder_binary = RS1729Decoder.DECODER_MAP.get(sonde_type, 'rs41mod')
        # Delegate to RS1729Decoder's own path resolution (single source of
        # truth — this used to be a separately maintained, slightly-divergent
        # copy of the same lookup list).
        return RS1729Decoder.resolve_decoder_path(decoder_binary)

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
            
            # Track current sonde serial for dashboard display
            if sonde_id and sonde_id != 'UNKNOWN':
                self._cur_serial = sonde_id

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
                dfmcode=frame_data.get('dfmcode'),  # DFM type code (e.g., "0xC")
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
                decoder_version='rs1729',
                receiver_device=self.device_serial
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
                 telemetry_callback: Callable[[SondeTelemetry], None],
                 channelizer_status_output=None):
        self.config             = config
        self.telemetry_callback = telemetry_callback
        self.channelizer_status_output = channelizer_status_output
        self.logger             = logging.getLogger('RTLSDRDeviceManager')
        self.running            = False
        self.lock               = threading.Lock()   # web_server compatibility

        self._registry = SondeRegistry()
        self._workers: List[DeviceWorker] = []
        self._status_thread: Optional[threading.Thread] = None  # Channelizer status sender

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
        
        # Priority frequency configuration
        self._priority_freq = det_cfg.get('priority_frequency')  # MHz
        self._priority_sonde_type = det_cfg.get('priority_sonde_type')  # RS41, DFM, etc.
        self._priority_timeout = det_cfg.get('priority_check_timeout', 30)  # seconds

        # Import API configuration
        import_api_cfg = config.get('import_api', {})
        self._import_api_enabled = import_api_cfg.get('enabled', False)
        if self._import_api_enabled:
            try:
                self._api_client = SondeApiClient(import_api_cfg)
                self.logger.info(f"Import API initialized: {import_api_cfg.get('url', 'api.opnwx.de')}")
            except Exception as e:
                self.logger.error(f"Failed to initialize Import API: {e}")
                self._import_api_enabled = False
                self._api_client = None
        else:
            self._api_client = None
            self.logger.debug("Import API disabled in configuration")

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
            
            # Check priority frequency on first worker after it initializes.
            # CRITICAL: Run in a background thread — priority_check_timeout can be
            # configured up to hours long (e.g. 14400s), and this loop must not block
            # startup of the remaining workers (NESDR002+ would never call w.start()
            # until the priority check finished or timed out).
            if idx == 0 and self._priority_freq and self._priority_freq > 0:
                threading.Thread(
                    target=self._run_priority_frequency_check,
                    args=(w,),
                    daemon=True,
                    name='PriorityFreqCheck'
                ).start()

            # CRITICAL: Delay between starting workers to prevent USB conflicts
            # This gives each worker time to initialize and open its USB device
            # before the next worker starts
            if idx < len(self._workers) - 1:  # Don't delay after last worker
                delay = 2.0  # 2 seconds between worker starts
                self.logger.debug(f"Waiting {delay}s before starting next worker")
                time.sleep(delay)
        self.logger.info("All device workers started")
        
        # Start Import API polling if enabled
        if self._import_api_enabled and self._api_client:
            self.logger.info("Starting Import API polling...")
            self._api_client.start(self._on_imported_sondes)
        
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
        
        # Start channelizer status sender if enabled
        if self.channelizer_status_output and self.channelizer_status_output.enabled:
            self._status_thread = threading.Thread(
                target=self._send_channelizer_status_loop, daemon=True, name='ChannelizerStatus'
            )
            self._status_thread.start()
            self.logger.info("Channelizer status sender started")

    def stop(self):
        self.running = False
        
        # Stop Import API polling
        if self._api_client:
            self._api_client.stop()
        
        # Stop channelizer status sender
        if self._status_thread:
            self._status_thread.join(timeout=2)
            self._status_thread = None
        
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
    
    def _send_channelizer_status_loop(self):
        """
        Background thread that periodically sends channelizer status updates via UDP.
        
        Collects active channel info from all workers and sends formatted status
        similar to receivemultisonde's slot status output.
        """
        while self.running:
            try:
                if self.channelizer_status_output and self.channelizer_status_output.should_send_update():
                    # Collect status from all workers
                    device_statuses = {}
                    for worker in self._workers:
                        device_statuses[worker.device_serial] = {
                            'decoder_mode': worker.decoder_mode,
                            'channelizer_active': worker.get_channelizer_channel_details()
                        }
                    
                    # Send aggregated status
                    self.channelizer_status_output.send_status(device_statuses)
                
                # Sleep for 1 second between checks (actual send controlled by update_interval)
                time.sleep(1)
            
            except Exception as e:
                self.logger.error(f"Error in channelizer status sender: {e}", exc_info=True)
                time.sleep(5)
    
    def _run_priority_frequency_check(self, w):
        """Background-thread entry point: wait for worker USB init, then run the
        (potentially long-running) priority frequency check without blocking start()."""
        max_wait = 15  # seconds to wait for USB init
        waited = 0
        while waited < max_wait and w._analyzer is None:
            time.sleep(0.5)
            waited += 0.5

        if w._analyzer is not None:
            # USB initialized successfully, now check priority frequency
            self._check_priority_frequency()
        else:
            self.logger.warning("First worker USB initialization timeout, skipping priority check")

    def _check_priority_frequency(self):
        """Check priority frequency on first available worker before starting normal scanning."""
        if not self._priority_freq or self._priority_freq <= 0:
            return
        
        priority_freq_hz = self._priority_freq * 1e6
        sonde_type = self._priority_sonde_type or 'RS41'
        timeout = self._priority_timeout
        
        self.logger.info(
            f"Checking priority frequency: {self._priority_freq:.3f} MHz "
            f"as {sonde_type} for {timeout}s before starting scanner"
        )
        
        # Use first worker for priority check
        worker = self._workers[0]
        
        # Start manual decoder on priority frequency
        success = worker.start_manual_decode(
            frequency=priority_freq_hz,
            sonde_type=sonde_type,
            duration_seconds=timeout
        )
        
        if not success:
            self.logger.warning("Failed to start priority frequency decoder")
            return

        # CRITICAL: Blacklist this frequency on every other worker for as long as
        # the priority decode is active. Without this, any other worker whose scan
        # range overlaps the priority frequency re-detects it on every cycle and
        # runs the full teardown + 3s settle + DFT correlation dance, only to
        # abort once the registry catches the duplicate — wasting USB/CPU in a
        # tight loop indefinitely instead of just skipping it up front.
        # CRITICAL: reassign w._blacklist to a NEW list rather than mutating the
        # existing one in place. This runs on a background thread while each
        # worker's own thread concurrently reads self._blacklist in
        # _is_blacklisted() (a `for b in self._blacklist` loop) — mutating a
        # list another thread may be mid-iteration over is a real (if rare)
        # race. Swapping the attribute to a fresh list is race-free: any
        # in-flight iteration keeps seeing the old list object to completion,
        # and every subsequent attribute read gets the updated one.
        other_workers = [w for w in self._workers if w is not worker]
        for w in other_workers:
            w._blacklist = w._blacklist + [priority_freq_hz]

        # Wait for priority check timeout
        start_time = time.time()
        frames_received = False

        while time.time() - start_time < timeout:
            # Check if frames are being decoded
            decoder = worker._decoder
            if decoder and decoder.frame_count > 0:
                frames_received = True
                self.logger.info(
                    f"Priority frequency is decoding successfully "
                    f"({decoder.frame_count} frames) - keeping active"
                )
                # Keep decoder running, don't stop it — and keep the frequency
                # blacklisted on other workers for as long as it's decoding
                return

            time.sleep(1.0)

        # Timeout reached without successful decode
        if not frames_received:
            self.logger.info(
                f"Priority frequency check timeout ({timeout}s) - "
                f"no frames decoded, returning to scan mode"
            )
            # Frequency is free again - let other workers detect it normally.
            # Same copy-on-write reasoning as above: rebuild a new list rather
            # than mutating w._blacklist in place.
            for w in other_workers:
                if priority_freq_hz in w._blacklist:
                    w._blacklist = [f for f in w._blacklist if f != priority_freq_hz]
            # Force return to scanning
            worker.stop_decode_and_scan()
    
    def _on_imported_sondes(self, sondes: List[Dict]):
        """Callback for Import API: assign detected sondes to available SDR receivers.
        
        Args:
            sondes: List of sonde dicts with keys: serial, frequency, type, distance_km, lat, lon, alt
        """
        if not self.running or not sondes:
            return
        
        self.logger.info(f"Import API detected {len(sondes)} nearby sondes")
        
        # Find available workers (IDLE or SCANNING, not DECODING, and not already
        # mid-flight into a manual decode e.g. priority frequency startup — that
        # state transiently looks IDLE before its own _start_decode() runs, so
        # without this check we can double-book the same device and the imported
        # sonde's decode ends up winning the race and evicting the manual one)
        available_workers = [
            w for w in self._workers
            if w._state in (w.STATE_IDLE, w.STATE_SCANNING)
            and not w._manual_decode_pending.is_set()
        ]
        
        if not available_workers:
            self.logger.info("No available SDR receivers for imported sondes (all busy decoding)")
            return
        
        self.logger.info(f"Found {len(available_workers)} available receivers")
        
        # Assign sondes to available workers (prioritized by distance - nearest first)
        assigned_count = 0
        for sonde in sondes:
            if assigned_count >= len(available_workers):
                self.logger.info(f"All available receivers assigned, skipping remaining sondes")
                break
            
            serial = sonde['serial']
            frequency = sonde['frequency']  # Hz
            sonde_type = sonde['type']
            distance = sonde['distance_km']
            
            # Check if this frequency is already being decoded
            if self._registry.is_active(frequency):
                self.logger.debug(
                    f"Skipping imported sonde {serial} @ {frequency/1e6:.3f} MHz: "
                    f"already being decoded"
                )
                continue
            
            # Get next available worker
            worker = available_workers[assigned_count]
            
            self.logger.info(
                f"Assigning imported sonde {serial} ({sonde_type}) @ {frequency/1e6:.3f} MHz "
                f"({distance:.1f}km) to device {worker.device_serial}"
            )
            
            # Start manual decode on this worker
            # Use unlimited duration (None) so it keeps decoding until sonde disappears
            success = worker.start_manual_decode(
                frequency=frequency,
                sonde_type=sonde_type,
                duration_seconds=None  # Decode until sonde lost
            )
            
            if success:
                assigned_count += 1
                self.logger.info(
                    f"Successfully started decoder for imported sonde {serial} "
                    f"on device {worker.device_serial}"
                )
            else:
                self.logger.warning(
                    f"Failed to start decoder for imported sonde {serial} "
                    f"on device {worker.device_serial}"
                )
        
        if assigned_count > 0:
            self.logger.info(
                f"Import API: assigned {assigned_count}/{len(sondes)} sondes to available receivers"
            )
        else:
            self.logger.debug("Import API: no new sondes assigned (all already being decoded)")

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
            
            # Step 4: Add channelizer status info
            result.append({
                'serial':                w.device_serial,
                'state':                 w.state,
                'frequency':             freq_mhz,
                'freq_label':            freq_label,
                'sonde_type':            w.current_sonde_type,
                'sonde_serial':          w.current_sonde_serial,
                'gain':                  w.device_config.get('gain', 40),  # Current gain setting
                'decoder_mode':          w.decoder_mode,  # 'legacy' or 'channelizer'
                'channelizer_active':    w.get_channelizer_channel_details(),  # Active channel details
                'channelizer_max':       w.channelizer_max_channels,     # Max channels (0 for legacy)
                'scan_return_eta_s':     w.get_scan_return_eta_s(),  # Seconds until back to scanning, or None
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
        self.logger.debug(f"get_spectrum_for_receiver({receiver_id}) looking for serial='{serial}'")
        for w in self._workers:
            if w.device_serial == serial:
                self.logger.debug(f"Found worker with serial={serial}, state={w.state}")
                spec = w.get_spectrum()
                if spec:
                    self.logger.debug(f"Returning spectrum with {len(spec.get('freqs_mhz', []))} points")
                    return spec
                self.logger.warning(f"Worker {serial} returned empty spectrum, returning fallback")
                return {
                    'receiver_id': target,
                    'receiver_name': f"RTL-SDR {serial}",
                    'freqs_mhz': [],
                    'power_db': [],
                    'signals': [],
                    'timestamp': datetime.utcnow().isoformat() + 'Z',
                }

        self.logger.error(f"Worker with serial={serial} NOT FOUND in {len(self._workers)} workers")
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
