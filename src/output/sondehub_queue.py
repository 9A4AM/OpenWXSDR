"""
# =============================================================================
#  OpenWX -- Open Weather Radiosonde Telemetry System
# =============================================================================
#
#  File   : sondehub_queue.py
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
#  Queued/batched SondeHub v2 telemetry upload plugin for OpenWX.
#
#  Provides SondeHubQueueOutput, an alternative to SondeHubOutput that
#  accumulates telemetry frames in an in-memory queue and flushes them
#  in configurable batches. This reduces HTTP overhead, improves upload
#  continuity on unstable links, and prevents decoder-loop blocking.
#
#  Upload pipeline:
#    send_telemetry() → queue.Queue → _worker_loop() → _flush_queue_batch()
#      → gzip-compressed JSON array → PUT api.v2.sondehub.org/sondes/telemetry
#
#  Features: configurable batch size, jittered exponential retry with
#  Retry-After support, periodic listener metadata registration,
#  per-serial subtype and satellite count continuity.
#
# =============================================================================
"""

import gzip
import importlib
import json
import logging
import queue
import random
import threading
import time
from datetime import datetime, timezone
from email.utils import formatdate
from typing import Dict, Optional

from .. import __software_name__, __version__
from ..decoders.models import SondeTelemetry


class SondeHubQueueOutput:
    """Uploads radiosonde telemetry frames to SondeHub v2 using a queue/batch worker."""

    DEFAULT_UPLOAD_URL = 'https://api.v2.sondehub.org/sondes/telemetry'
    DEFAULT_LISTENERS_URL = 'https://api.v2.sondehub.org/listeners'

    def __init__(self, config: dict):
        self.config = config
        self.logger = logging.getLogger('SondeHubQueueOutput')

        sh_cfg = config.get('sondehub', {})
        enabled_value = sh_cfg.get('enabled', False)
        
        # Handle enabled as boolean (true/false) or string "json" for payload logging
        # enabled: true → self.enabled=True, self.log_json_only=False (upload to SondeHub)
        # enabled: false → self.enabled=False, self.log_json_only=False (disabled)
        # enabled: "json" → self.enabled=False, self.log_json_only=True (log to file, no upload)
        if enabled_value == 'json' or enabled_value == "json":
            self.log_json_only = True
            self.enabled = False  # Not uploading to SondeHub
        elif enabled_value:
            self.log_json_only = False
            self.enabled = True  # Upload to SondeHub
        else:
            self.log_json_only = False
            self.enabled = False  # Completely disabled
        
        self.upload_url = sh_cfg.get('upload_url', self.DEFAULT_UPLOAD_URL)
        self.listeners_url = sh_cfg.get('listeners_url', self.DEFAULT_LISTENERS_URL)
        self.upload_rate_s = max(1, int(sh_cfg.get('upload_rate_s', 1)))
        self.listener_upload_interval_s = max(60, int(sh_cfg.get('listener_upload_interval_s', 900)))

        self.queue_max_size = max(100, int(sh_cfg.get('queue_max_size', 2000)))
        self.queue_batch_max = max(1, int(sh_cfg.get('queue_batch_max', 200)))

        # Always use internal application identity/version for uploads.
        self.software_name = __software_name__
        self.software_version = __version__

        station_cfg = config.get('station', {})
        self.uploader_callsign = (
            sh_cfg.get('uploader_callsign')
            or sh_cfg.get('station_id')
            or station_cfg.get('callsign')
            or 'OPENWXSDR_STATION'
        )
        self.uploader_antenna = sh_cfg.get('uploader_antenna', '')
        self.uploader_radio = sh_cfg.get('uploader_radio', '')
        self.contact_email = sh_cfg.get('contact_email', '')

        self.station_lat = sh_cfg.get('uploader_lat', station_cfg.get('lat'))
        self.station_lon = sh_cfg.get('uploader_lon', station_cfg.get('lon'))
        self.station_alt = sh_cfg.get('uploader_alt', station_cfg.get('alt'))

        # JSON payload logging directory (used when enabled="json")
        self.json_log_dir = './data/logs'

        self._lock = threading.Lock()
        self._last_listener_upload_t = 0.0
        self._last_flush_t = time.monotonic()
        self._session = None

        self._telemetry_queue: "queue.Queue[dict]" = queue.Queue(maxsize=self.queue_max_size)
        self._stop_evt = threading.Event()
        self._worker_thread = None

        self._telemetry_enqueued = 0
        self._telemetry_uploaded = 0
        self._telemetry_dropped = 0
        self._last_upload_ok_t = 0.0
        self._last_upload_error = ''

        if self.enabled or self.log_json_only:
            # JSON logging mode doesn't need requests library
            if self.enabled:  # Only need requests for actual upload
                try:
                    requests_module = importlib.import_module('requests')
                    self._session = requests_module.Session()
                except ImportError:
                    self.logger.error("requests library not installed; SondeHub queue upload disabled")
                    self.enabled = False
                    return

            if self.log_json_only:
                self.logger.info(
                    f"SondeHub JSON logging mode enabled -> {self.json_log_dir}/<serial>.json "
                    f"(payloads written to file, NOT uploaded to SondeHub)"
                )
            elif self.enabled:
                self.logger.info(
                    f"SondeHub queue upload enabled -> {self.upload_url} "
                    f"(callsign={self.uploader_callsign}, flush_rate={self.upload_rate_s}s, "
                    f"listener_rate={self.listener_upload_interval_s}s, batch_max={self.queue_batch_max}, "
                    f"queue_max={self.queue_max_size})"
                )

            # Only start worker thread if actually uploading to SondeHub
            if self.enabled:
                self._worker_thread = threading.Thread(
                    target=self._worker_loop,
                    daemon=True,
                    name='SondeHubQueueWorker',
                )
                self._worker_thread.start()

                # Queue immediate listener metadata upload on startup.
                timer = threading.Timer(0.5, self._upload_listener_metadata)
                timer.daemon = True
                timer.start()
        else:
            self.logger.debug("SondeHub queue upload is disabled in configuration")

    def send_telemetry(self, telemetry: SondeTelemetry):
        """Queue a telemetry frame for SondeHub upload (non-blocking)."""
        if not self.enabled and not self.log_json_only:
            return

        payload = self._build_payload(telemetry, strict=not self.log_json_only)
        if not payload:
            return

        if self.log_json_only:
            self._write_json_log(payload)
            return

        try:
            self._telemetry_queue.put_nowait(payload)
            with self._lock:
                self._telemetry_enqueued += 1
        except queue.Full:
            with self._lock:
                self._telemetry_dropped += 1
            self.logger.warning(
                f"[SONDEHUB-QUEUE] queue full (max={self.queue_max_size}), dropping telemetry for {payload.get('serial', 'UNKNOWN')}"
            )

    def close(self):
        """Stop worker and close HTTP session."""
        self._stop_evt.set()
        if self._worker_thread is not None:
            self._worker_thread.join(timeout=3.0)
        # Best-effort final flush on shutdown.
        while not self._telemetry_queue.empty():
            if not self._flush_queue_batch():
                break
        if self._session is not None:
            try:
                self._session.close()
            except Exception:
                pass

    def get_status(self) -> dict:
        """Return SondeHub queue upload status for health endpoints."""
        if not self.enabled:
            return {'status': 'disabled', 'mode': 'queue'}

        if self.log_json_only:
            return {
                'status': 'json_logging',
                'mode': 'json_log',
                'log_dir': self.json_log_dir,
                'note': 'Payloads written to file, NOT uploaded to SondeHub',
            }

        with self._lock:
            queued = self._telemetry_queue.qsize()
            uploaded = self._telemetry_uploaded
            dropped = self._telemetry_dropped
            last_ok = self._last_upload_ok_t
            last_error = self._last_upload_error

        status = 'active' if uploaded > 0 else 'waiting'
        if queued > 0:
            status = 'active'

        return {
            'status': status,
            'mode': 'queue',
            'upload_rate_s': self.upload_rate_s,
            'callsign': self.uploader_callsign,
            'queued': queued,
            'uploaded': uploaded,
            'dropped': dropped,
            'last_upload_ok_t': last_ok,
            'last_upload_error': last_error,
        }

    def _worker_loop(self):
        while not self._stop_evt.is_set():
            try:
                now = time.monotonic()
                elapsed = now - self._last_flush_t

                should_flush = elapsed >= self.upload_rate_s
                if not should_flush and self._telemetry_queue.qsize() >= self.queue_batch_max:
                    should_flush = True

                if should_flush:
                    self._flush_queue_batch()
                    self._last_flush_t = time.monotonic()

                # Keep listener visible even when no fresh telemetry arrives.
                self._upload_listener_metadata()

                self._stop_evt.wait(0.25)
            except Exception as exc:
                self.logger.error(f"[SONDEHUB-QUEUE] worker error: {type(exc).__name__}: {exc}")
                self._stop_evt.wait(1.0)

    def _flush_queue_batch(self):
        to_upload = []
        for _ in range(self.queue_batch_max):
            try:
                to_upload.append(self._telemetry_queue.get_nowait())
            except queue.Empty:
                break

        if not to_upload:
            return True

        ok = self._upload_payloads(to_upload)
        if ok:
            with self._lock:
                self._telemetry_uploaded += len(to_upload)
                self._last_upload_ok_t = time.monotonic()
                self._last_upload_error = ''
        else:
            # Preserve telemetry on transient failures.
            for payload in to_upload:
                try:
                    self._telemetry_queue.put_nowait(payload)
                except queue.Full:
                    with self._lock:
                        self._telemetry_dropped += 1
            with self._lock:
                self._last_upload_error = 'upload_failed'
        return ok

    def _utc_iso(self, dt: Optional[datetime]) -> str:
        if dt is None:
            dt = datetime.now(timezone.utc)
        elif dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        return dt.isoformat(timespec='milliseconds').replace('+00:00', 'Z')

    def _manufacturer_for(self, sonde_type: str) -> str:
        st = (sonde_type or '').strip()
        base = st.split('-', 1)[0].upper() if st else ''
        mapping = {
            'RS41': 'Vaisala',
            'RS92': 'Vaisala',
            'DFM': 'Graw',
            'M10': 'Meteomodem',
            'M20': 'Meteomodem',
            'IMET': 'Intermet Systems',
            'LMS6': 'Lockheed Martin',
            'MRZ': 'Meteo-Radiy',
        }
        return mapping.get(base, 'Unknown')

    def _effective_type(self, telemetry: SondeTelemetry) -> str:
        sonde_type = (telemetry.sonde_type or '').strip() or 'Unknown'
        subtype = (telemetry.subtype or '').strip()

        if '-' in sonde_type:
            return sonde_type.split('-', 1)[0].upper()
        if '-' in subtype:
            return subtype.split('-', 1)[0].upper()
        return sonde_type

    def _effective_subtype(self, telemetry: SondeTelemetry, serial: str) -> str:
        subtype = (telemetry.subtype or '').strip()
        sonde_type = self._effective_type(telemetry)
        if not subtype and sonde_type.upper() == 'RS41':
            subtype = 'RS41-SGP' if serial.upper().startswith('V') else 'RS41'
        return subtype

    def _normalize_sats(self, value) -> Optional[int]:
        if value is None:
            return None
        try:
            sats_i = int(value)
        except (TypeError, ValueError):
            return None
        return sats_i if sats_i >= 0 else None

    def _effective_sats(self, telemetry: SondeTelemetry) -> Optional[int]:
        return self._normalize_sats(getattr(telemetry, 'satellites', None))

    def _uploader_position(self) -> Optional[list]:
        if self.station_lat is None or self.station_lon is None:
            return None

        alt = float(self.station_alt) if self.station_alt is not None else 0.0
        return [
            round(float(self.station_lat), 6),
            round(float(self.station_lon), 6),
            round(alt, 1),
        ]

    def _is_valid_serial(self, serial: str, sonde_type: str) -> bool:
        """
        Validate sonde serial format according to SondeHub requirements.
        
        RS41/RS92: Must start with A-Z followed by 7-8 digits (e.g., V1220530, S12345678)
        DFM: Must start with 'DFM-' followed by 8 digits (e.g., DFM-21065615)
        M10/M20: Must start with 'M' followed by 8-10 characters
        iMet: Must start with 'iMet' or 'IMET'
        LMS6: Starts with 'LMS'
        MRZ: Starts with 'MRZ'
        
        Returns False for partial/malformed serials like '-+', 'UNKNOWN', etc.
        """
        import re
        
        if not serial or serial == 'UNKNOWN':
            return False
        
        serial = serial.strip()
        sonde_type_upper = sonde_type.upper().split('-')[0]  # Strip subtype like RS41-SGP → RS41
        
        # RS41/RS92: [A-Z][0-9]{7,8} (8 or 9 chars total, e.g., V1220530 or S12345678)
        if sonde_type_upper in ('RS41', 'RS92'):
            return bool(re.match(r'^[A-Z][0-9]{7,8}$', serial))
        
        # DFM: DFM-[0-9]{8} (e.g., DFM-21065615)
        elif sonde_type_upper == 'DFM':
            return bool(re.match(r'^DFM-[0-9]{8}$', serial))
        
        # M10/M20: M[0-9A-Z]{8,10}
        elif sonde_type_upper in ('M10', 'M20'):
            return bool(re.match(r'^M[0-9A-Z]{8,10}$', serial, re.IGNORECASE))
        
        # iMet: Starts with iMet or IMET
        elif sonde_type_upper == 'IMET':
            return serial.upper().startswith('IMET') and len(serial) >= 4
        
        # LMS6: Starts with LMS
        elif sonde_type_upper == 'LMS6':
            return serial.upper().startswith('LMS') and len(serial) >= 4
        
        # MRZ: Starts with MRZ
        elif sonde_type_upper == 'MRZ':
            return serial.upper().startswith('MRZ') and len(serial) >= 4
        
        # Unknown type: reject if it contains common malformed patterns
        # Reject serials with only special characters, spaces, or very short
        if len(serial) < 3:
            return False
        if re.match(r'^[-+\s]+$', serial):
            return False
        
        # Allow other types with alphanumeric serials
        return bool(re.match(r'^[A-Z0-9][A-Z0-9\-]{2,}$', serial, re.IGNORECASE))

    def _build_payload(self, telemetry: SondeTelemetry, strict: bool = True) -> Optional[dict]:
        """Build a SondeHub telemetry payload dict.

        strict=True  – enforces serial validation; used for actual SondeHub uploads.
        strict=False – accepts any serial (falls back to 'UNKNOWN'); used for JSON file logging.
        """
        if not telemetry.position:
            return None

        serial = (telemetry.serial or '').strip()
        if strict:
            if not serial or serial == 'UNKNOWN':
                return None
        else:
            if not serial:
                serial = 'UNKNOWN'

        sonde_type = self._effective_type(telemetry)

        if strict:
            if not self._is_valid_serial(serial, sonde_type):
                self.logger.warning(
                    f"[SONDEHUB-QUEUE] Invalid serial format: '{serial}' for {sonde_type}. "
                    f"Skipping upload (likely partial/corrupted decode). "
                    f"Valid formats: RS41=[A-Z][0-9]{{7-8}}, DFM=DFM-[0-9]{{8}}"
                )
                return None
            if sonde_type.upper().startswith('DFM') and serial in ('UNKNOWN', ''):
                return None

        subtype = self._effective_subtype(telemetry, serial)
        sats = self._effective_sats(telemetry)

        payload = {
            'software_name': self.software_name,
            'software_version': self.software_version,
            'uploader_callsign': self.uploader_callsign,
            'time_received': self._utc_iso(datetime.now(timezone.utc)),
            'manufacturer': self._manufacturer_for(sonde_type),
            'type': sonde_type,
            'serial': serial,
            'frame': int(telemetry.frame_number or 0),
            'datetime': self._utc_iso(telemetry.position.datetime),
            'lat': round(float(telemetry.position.latitude), 6),
            'lon': round(float(telemetry.position.longitude), 6),
            'alt': round(float(telemetry.position.altitude), 1),
        }

        if subtype:
            payload['subtype'] = subtype

        if sats is not None:
            payload['sats'] = sats

        if telemetry.frequency:
            payload['frequency'] = round(float(telemetry.frequency) / 1e6, 3)

        if telemetry.velocity:
            payload['vel_h'] = round(float(telemetry.velocity.horizontal_speed), 2)
            payload['vel_v'] = round(float(telemetry.velocity.vertical_speed), 2)
            if telemetry.velocity.heading is not None:
                payload['heading'] = round(float(telemetry.velocity.heading), 1)

        if telemetry.environment:
            if telemetry.environment.temperature is not None:
                payload['temp'] = round(float(telemetry.environment.temperature), 1)
            if telemetry.environment.humidity is not None:
                payload['humidity'] = round(float(telemetry.environment.humidity), 1)
            if telemetry.environment.pressure is not None:
                payload['pressure'] = round(float(telemetry.environment.pressure), 2)
            self.logger.debug(
                f"[SONDEHUB-QUEUE-PTU] {serial}: Added environment to payload: "
                f"temp={payload.get('temp')}, hum={payload.get('humidity')}, pres={payload.get('pressure')}"
            )
        else:
            self.logger.debug(f"[SONDEHUB-QUEUE-PTU] {serial}: No environment data in telemetry object")

        if telemetry.battery is not None:
            payload['batt'] = round(float(telemetry.battery), 2)

        # RS41-specific fields
        if telemetry.burst_timer is not None:
            payload['burst_timer'] = int(telemetry.burst_timer)
        if telemetry.rs41_mainboard is not None:
            payload['rs41_mainboard'] = str(telemetry.rs41_mainboard)
        if telemetry.rs41_mainboard_fw is not None:
            payload['rs41_mainboard_fw'] = int(telemetry.rs41_mainboard_fw)
        if telemetry.ref_datetime is not None:
            payload['ref_datetime'] = str(telemetry.ref_datetime)
        if telemetry.ref_position is not None:
            payload['ref_position'] = str(telemetry.ref_position)
        if telemetry.tx_frequency is not None:
            # tx_frequency from rs41mod JSON is already in MHz format
            payload['tx_frequency'] = round(float(telemetry.tx_frequency), 3)

        # SNR and RSSI removed per user request - not needed for SondeHub

        uploader_position = self._uploader_position()
        if uploader_position is not None:
            payload['uploader_position'] = uploader_position
        if self.uploader_antenna:
            payload['uploader_antenna'] = self.uploader_antenna

        return payload

    def _headers(self, with_gzip: bool = False) -> dict:
        headers = {
            'User-Agent': f"{self.software_name}-{self.software_version}",
            'Date': formatdate(timeval=None, localtime=False, usegmt=True),
            'Content-Type': 'application/json',
        }
        if with_gzip:
            headers['Content-Encoding'] = 'gzip'
        return headers

    def _retry_delay_s(self, retries: int, resp_headers: Optional[dict] = None) -> float:
        if resp_headers:
            retry_after = resp_headers.get('Retry-After')
            if retry_after:
                try:
                    return max(0.25, float(retry_after))
                except (TypeError, ValueError):
                    pass

        base = min(5.0, 0.5 * (2 ** max(0, retries - 1)))
        return base + random.uniform(0.0, 0.25)

    def _write_json_log(self, payload: dict):
        """Write JSON payload to file (one JSON object per line) for SondeHub admin review."""
        serial = payload.get('serial', 'UNKNOWN')
        if serial == 'UNKNOWN':
            return

        try:
            import os
            os.makedirs(self.json_log_dir, exist_ok=True)
            log_file = os.path.join(self.json_log_dir, f"{serial}.json")
            
            with open(log_file, 'a', encoding='utf-8') as f:
                f.write(json.dumps(payload) + '\n')
            
            self.logger.debug(f"[SONDEHUB-JSON] Logged payload for {serial} to {log_file}")
        except Exception as exc:
            self.logger.error(f"[SONDEHUB-JSON] Failed to write JSON log: {type(exc).__name__}: {exc}")

    def _upload_payloads(self, payloads: list) -> bool:
        telem_json = json.dumps(payloads).encode('utf-8')
        compressed_payload = gzip.compress(telem_json)

        retries = 0
        max_retries = 3
        while retries < max_retries:
            try:
                client = self._session
                if client is None:
                    client = importlib.import_module('requests')

                resp = client.put(
                    self.upload_url,
                    compressed_payload,
                    headers=self._headers(with_gzip=True),
                    timeout=(10, 6.1),
                )

                if 200 <= resp.status_code < 300:
                    if resp.status_code == 202:
                        try:
                            resp_json = resp.json()
                            for error in resp_json.get('errors', []):
                                msg = error.get('error_message', 'unknown')
                                if 'z-check' not in msg:
                                    self.logger.warning(f"[SONDEHUB-QUEUE] Data error: {msg}")
                            for warning in resp_json.get('warnings', []):
                                self.logger.debug(
                                    f"[SONDEHUB-QUEUE] Data warning: {warning.get('warning_message', 'unknown')}"
                                )
                        except Exception:
                            pass

                    self.logger.info(
                        f"[SONDEHUB-QUEUE✓] Upload OK ({len(payloads)} frames): HTTP {resp.status_code}"
                    )
                    return True

                if resp.status_code in (429, 500, 502, 503, 504):
                    retries += 1
                    if retries < max_retries:
                        delay_s = self._retry_delay_s(retries, resp.headers)
                        self.logger.debug(
                            f"[SONDEHUB-QUEUE] Transient HTTP {resp.status_code}, retrying in {delay_s:.2f}s "
                            f"({retries}/{max_retries})"
                        )
                        time.sleep(delay_s)
                        continue

                self.logger.warning(
                    f"[SONDEHUB-QUEUE✗] Upload failed: HTTP {resp.status_code} | {resp.text[:200]}"
                )
                return False
            except Exception as exc:
                retries += 1
                if retries < max_retries:
                    delay_s = self._retry_delay_s(retries)
                    self.logger.debug(
                        f"[SONDEHUB-QUEUE] Request error: {exc}, retrying in {delay_s:.2f}s "
                        f"({retries}/{max_retries})"
                    )
                    time.sleep(delay_s)
                    continue

                self.logger.error(
                    f"[SONDEHUB-QUEUE✗] Upload exception: {type(exc).__name__}: {exc}"
                )
                return False

        return False

    def _upload_listener_metadata(self):
        if not self.enabled:
            return

        now = time.monotonic()
        with self._lock:
            if (now - self._last_listener_upload_t) < self.listener_upload_interval_s:
                return
            self._last_listener_upload_t = now

        listener = {
            'software_name': self.software_name,
            'software_version': self.software_version,
            'uploader_callsign': self.uploader_callsign,
            'mobile': False,
        }
        uploader_position = self._uploader_position()
        if uploader_position is not None:
            listener['uploader_position'] = uploader_position
        if self.uploader_antenna:
            listener['uploader_antenna'] = self.uploader_antenna
        if self.contact_email:
            listener['uploader_contact_email'] = self.contact_email

        retries = 0
        max_retries = 3
        while retries < max_retries:
            try:
                client = self._session
                if client is None:
                    client = importlib.import_module('requests')

                resp = client.put(
                    self.listeners_url,
                    json=listener,
                    headers=self._headers(),
                    timeout=(10, 6.1),
                )

                if 200 <= resp.status_code < 300:
                    self.logger.debug(
                        f"[SONDEHUB-QUEUE✓] Listener metadata uploaded (HTTP {resp.status_code})"
                    )
                    return

                if resp.status_code in (429, 500, 502, 503, 504):
                    retries += 1
                    if retries < max_retries:
                        delay_s = self._retry_delay_s(retries, resp.headers)
                        time.sleep(delay_s)
                        continue

                self.logger.warning(
                    f"[SONDEHUB-QUEUE✗] Listener upload failed: HTTP {resp.status_code} | {resp.text[:200]}"
                )
                return
            except Exception as exc:
                retries += 1
                if retries < max_retries:
                    delay_s = self._retry_delay_s(retries)
                    time.sleep(delay_s)
                    continue

                self.logger.error(
                    f"[SONDEHUB-QUEUE✗] Listener upload exception: {type(exc).__name__}: {exc}"
                )
                return
