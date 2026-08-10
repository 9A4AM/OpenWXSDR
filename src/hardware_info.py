"""
# =============================================================================
#  OpenWX -- Open Weather Radiosonde Telemetry System
# =============================================================================
#
#  File   : hardware_info.py
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
#  Best-effort host hardware model detection, shared by the web UI's Service
#  Status modal and the anonymous telemetry ping. Deliberately returns only a
#  generic hardware description (e.g. "Raspberry Pi 4 Model B Rev 1.4" or
#  "x86_64 (Linux 6.1.0)") — never hostname, IP address, or anything else
#  that could identify a specific station or operator.
#
# =============================================================================
"""

import os
import platform


def detect_host_hardware() -> str:
    """Best-effort hardware description for the host machine."""
    for path in ('/sys/firmware/devicetree/base/model', '/proc/device-tree/model'):
        try:
            if os.path.exists(path):
                with open(path, 'r', encoding='utf-8', errors='ignore') as handle:
                    model = handle.read().strip('\x00\n\r ')
                    if model:
                        return model
        except Exception:
            pass

    processor = platform.processor().strip()
    machine = platform.machine().strip()
    system_name = platform.system().strip()
    release = platform.release().strip()

    details = ' '.join(part for part in [processor, machine] if part)
    if not details:
        details = machine or system_name or 'Unknown hardware'
    if system_name or release:
        details = f"{details} ({system_name} {release})".strip()
    return details
