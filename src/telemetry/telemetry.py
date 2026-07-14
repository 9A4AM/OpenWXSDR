"""
# =============================================================================
#  OpenWX -- Open Weather Radiosonde Telemetry System
# =============================================================================
#
#  File   : telemetry.py
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
#  Anonymous telemetry counter for OpenWXSDR.
#
#  Sends a minimal, anonymous telemetry heartbeat to count how many independent
#  OpenWXSDR installations are active and of what hardware people run it on.
#
#  What is sent, and ONLY this:
#    - install_id : a random UUID generated once and stored locally
#                    (./data/.install_id) — not derived from any station
#                    identity, hardware serial, or network identifier.
#    - version     : OpenWXSDR version string.
#    - sdr_type    : configured SDR backend ('rtlsdr', 'ka9q', 'airspy',
#                    'flux242').
#    - hardware    : generic host hardware description (e.g. "Raspberry Pi 4
#                    Model B Rev 1.4" or "x86_64 (Linux 6.1.0)"), the same
#                    detection used by the web UI's Service Status modal.
#
#  Explicitly NEVER sent: callsign, station lat/lon, MQTT/SondeHub
#  credentials, RTL-SDR serials, hostname, IP address, or any other
#  station-identifying data.
#
#  Controlled entirely by config.yaml -> telemetry.enabled (default true).
#  Set telemetry.enabled: false to opt out completely.
#
# =============================================================================
"""

import logging
import os
import threading
import uuid
from typing import Optional

import requests

from .. import __version__
from ..hardware_info import detect_host_hardware


class InstallPing:
    """Anonymous, opt-out installation counter. See module docstring for
    exactly what data is (and is not) sent."""

    ENDPOINT_URL = 'http://api.opnwx.de/telemetry/openwxsdr.php'
    DEFAULT_INTERVAL_HOURS = 24
    REQUEST_TIMEOUT_S = 5

    def __init__(self, config: dict, data_dir: str = './data'):
        telemetry_cfg = config.get('telemetry', {})
        self.enabled = bool(telemetry_cfg.get('enabled', True))
        self.interval_s = max(
            3600, int(telemetry_cfg.get('interval_hours', self.DEFAULT_INTERVAL_HOURS)) * 3600
        )
        self.sdr_type = config.get('sdr', {}).get('type', 'unknown')
        # Generic hardware description only (e.g. "Raspberry Pi 4 Model B") —
        # never hostname/IP, which _get_host_info() also exposes to the web
        # UI's Service Status modal but which are excluded here on purpose.
        self.hardware = detect_host_hardware()
        self.logger = logging.getLogger('Telemetry')
        self.data_dir = data_dir

        self._install_id: Optional[str] = None
        self._stop_evt = threading.Event()
        self._thread: Optional[threading.Thread] = None

        if self.enabled:
            self._install_id = self._load_or_create_install_id()

    def _load_or_create_install_id(self) -> str:
        """Load the persisted random install ID, or generate and store a new
        one. Never derived from station identity or hardware."""
        path = os.path.join(self.data_dir, '.install_id')
        try:
            os.makedirs(self.data_dir, exist_ok=True)
            if os.path.isfile(path):
                with open(path, 'r', encoding='utf-8') as f:
                    existing = f.read().strip()
                    if existing:
                        return existing
            new_id = str(uuid.uuid4())
            with open(path, 'w', encoding='utf-8') as f:
                f.write(new_id)
            return new_id
        except Exception as e:
            self.logger.debug(f"Could not persist install ID, using ephemeral one this run: {e}")
            return str(uuid.uuid4())

    def start(self):
        """Start the background ping loop (no-op if disabled)."""
        if not self.enabled:
            self.logger.debug("Anonymous telemetry disabled (telemetry.enabled: false)")
            return

        self.logger.info(
            "Telemetry enabled: send random install ID, version, hardware, "
            f"and SDR type to OpenWXSDR every {self.interval_s // 3600}h. "
        )
        self._thread = threading.Thread(target=self._loop, daemon=True, name='InstallPing')
        self._thread.start()

    def stop(self):
        self._stop_evt.set()
        if self._thread:
            self._thread.join(timeout=2)

    def _loop(self):
        # Send once shortly after startup, then repeat on the configured interval.
        self._send_ping()
        while not self._stop_evt.wait(self.interval_s):
            self._send_ping()

    def _send_ping(self):
        payload = {
            'install_id': self._install_id,
            'version': __version__,
            'sdr_type': self.sdr_type,
            'hardware': self.hardware,
        }
        try:
            requests.post(self.ENDPOINT_URL, json=payload, timeout=self.REQUEST_TIMEOUT_S)
            self.logger.debug(f"Install ping sent: {payload}")
        except Exception as e:
            # Never fatal — this must not affect decoding/upload operation.
            self.logger.debug(f"Install ping failed (non-fatal): {e}")
