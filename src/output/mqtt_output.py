"""
# =============================================================================
#  OpenWX -- Open Weather Radiosonde Telemetry System
# =============================================================================
#
#  File   : mqtt_output.py
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
#  MQTT telemetry output plugin for OpenWX.
#
#  Publishes decoded radiosonde telemetry frames to an MQTT broker using
#  the OpenWX.de broker payload format. Supports paho-mqtt 1.x and 2.x,
#  optional TLS/SSL, username/password authentication, TCP and WebSocket
#  transports, and automatic reconnection via paho loop_start().
#
#  Payload format : OpenWX.de JSON schema (gw, id, ser, lat, lon, alt,
#                   freq, tmp, hum, pres, batt, subtype, ...)
#  Transport      : TCP (default) or WebSockets
#  TLS            : optional, configurable via config.yaml
#  Dependency     : paho-mqtt >= 1.6
#
# =============================================================================
"""

import json
import logging
import os
import ssl
import threading
import time
import platform
from datetime import datetime
from typing import Optional, Set, Dict

from .. import __version__
from ..decoders.models import SondeTelemetry


class MQTTOutput:
    """Publishes telemetry data to an MQTT broker (e.g. OpenWX.de)"""

    def __init__(self, config: dict):
        self.config = config
        self.logger = logging.getLogger('MQTTOutput')

        mqtt_cfg = config.get('openwx', {}).get('mqtt', {})
        self.enabled = mqtt_cfg.get('enabled', False)
        self.server = mqtt_cfg.get('server', 'localhost')
        self.port = int(mqtt_cfg.get('port', 1883))
        self.username = mqtt_cfg.get('username', '')
        self.password = mqtt_cfg.get('password', '')
        self.topic_prefix = mqtt_cfg.get('topic_prefix', 'openwxsdr/')
        self.client_id = mqtt_cfg.get('client_id', 'openwxsdr')
        self.keepalive = int(mqtt_cfg.get('keepalive', 60))
        self.connect_timeout = int(mqtt_cfg.get('connect_timeout', 10))
        self.tls_enabled = bool(mqtt_cfg.get('tls_enabled', self.port == 8883))
        self.tls_insecure = bool(mqtt_cfg.get('tls_insecure', self.port == 8883))
        self.tls_ca_certs = mqtt_cfg.get('tls_ca_certs') or None
        self.transport = mqtt_cfg.get('transport', 'tcp')

        self._client = None
        self._connected = False
        self._lock = threading.Lock()
        self._connected_event = threading.Event()

        # Read debug_mqtt flag – suppresses keep-alive PING noise unless enabled
        log_cfg = config.get('logging', {})
        self._debug_mqtt = bool(log_cfg.get('debug_mqtt', False))

        # Gateway status tracking
        station_cfg = config.get('station', {})
        self._gateway_id = station_cfg.get('callsign', 'OPENWXSDR')
        self._gateway_lat = station_cfg.get('lat', 0.0)
        self._gateway_lon = station_cfg.get('lon', 0.0)
        self._receiver = station_cfg.get('receiver', 'Unknown')
        self._antenna = station_cfg.get('antenna', 'Unknown')
        self._upload_position = station_cfg.get('upload_position', False)
        self._upload_interval_minutes = max(15, min(720, int(station_cfg.get('upload_interval', 15))))
        
        self._start_time = time.time()
        self._total_frames = 0
        self._active_sondes: Set[str] = set()
        self._sonde_last_seen: Dict[str, float] = {}  # Track last frame time per sonde
        self._gateway_thread = None
        self._gateway_running = False
        self._hardware_info = self._get_hardware_info()
        self._version = self._get_version()

        if self.enabled:
            self._initialize()

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------

    def _initialize(self):
        """Set up and connect the MQTT client."""
        try:
            import paho.mqtt.client as mqtt
        except ImportError:
            self.logger.error(
                "paho-mqtt is not installed. "
                "Run: pip install paho-mqtt  "
                "or re-run install.sh and select 'y' for MQTT support."
            )
            self.enabled = False
            return

        try:
            # paho-mqtt 2.x requires an explicit CallbackAPIVersion;
            # fall back to the 1.x constructor when running an older version.
            try:
                self._client = mqtt.Client(
                    mqtt.CallbackAPIVersion.VERSION2,
                    client_id=self.client_id,
                    transport=self.transport,
                )
            except AttributeError:
                self._client = mqtt.Client(client_id=self.client_id, transport=self.transport)

            self._client.reconnect_delay_set(min_delay=2, max_delay=30)

            if self.username:
                self._client.username_pw_set(
                    self.username,
                    self.password if self.password else None
                )

            if self.tls_enabled:
                if self.tls_ca_certs:
                    self._client.tls_set(ca_certs=self.tls_ca_certs)
                elif self.tls_insecure:
                    self._client.tls_set(cert_reqs=ssl.CERT_NONE)
                else:
                    self._client.tls_set()
                self._client.tls_insecure_set(self.tls_insecure)

            self._client.on_connect = self._on_connect
            self._client.on_disconnect = self._on_disconnect
            self._client.on_log = self._on_log

            self.logger.info(
                f"MQTT output initializing: {self.server}:{self.port} "
                f"(prefix='{self.topic_prefix}', tls={self.tls_enabled}, "
                f"user={'set' if self.username else 'unset'})"
            )

            self._connected_event.clear()
            self._client.connect(self.server, self.port, keepalive=self.keepalive)
            self._client.loop_start()

            if not self._connected_event.wait(timeout=self.connect_timeout):
                self.logger.warning(
                    f"MQTT connect timeout after {self.connect_timeout}s "
                    f"to {self.server}:{self.port}"
                )
            
            # Start gateway status publishing thread if upload_position is enabled
            if self._upload_position:
                self._start_gateway_status_thread()

        except Exception as e:
            self.logger.error(f"Failed to initialize MQTT client: {e}")
            self.enabled = False

    # ------------------------------------------------------------------
    # paho callbacks
    # ------------------------------------------------------------------

    def _on_connect(self, client, userdata, flags, rc, *args):
        """Called when MQTT handshake completes (both paho 1.x and 2.x)."""
        # paho 2.x passes a ReasonCode object; 1.x passes an integer.
        # Treat any zero-valued reason code as success and never raise from the callback.
        try:
            rc_value = int(rc)
        except Exception:
            rc_value = getattr(rc, 'value', None)
            if rc_value is None:
                rc_value = 1
        success = (rc_value == 0)
        if success:
            self._connected = True
            self._connected_event.set()
            self.logger.info(f"MQTT connected to {self.server}:{self.port}")
        else:
            self._connected = False
            self._connected_event.set()
            self.logger.warning(f"MQTT connection refused (rc={rc})")

    def _on_disconnect(self, client, userdata, flags_or_rc, reason_code=None, *args):
        """Called on disconnect; paho will auto-reconnect via loop_start.
        Signature handles both paho 1.x (rc int) and paho 2.x (flags, rc, props).
        """
        self._connected = False
        # In paho 1.x: flags_or_rc is the integer reason code.
        # In paho 2.x: flags_or_rc is DisconnectFlags, reason_code is a ReasonCode.
        rc = reason_code if reason_code is not None else flags_or_rc
        try:
            rc_value = int(rc)
        except Exception:
            rc_value = getattr(rc, 'value', None)
            if rc_value is None:
                rc_value = 1
        not_clean = (rc_value != 0)
        if not_clean:
            self.logger.warning(
                f"MQTT unexpected disconnect (rc={rc}), will attempt reconnect"
            )

    def _on_log(self, client, userdata, level, buf):
        # Suppress keep-alive noise (PINGREQ / PINGRESP) unless debug_mqtt is on
        if not self._debug_mqtt and ('PINGREQ' in buf or 'PINGRESP' in buf):
            return
        if level == 16:
            self.logger.debug(f"MQTT: {buf}")
        elif level >= 8:
            self.logger.info(f"MQTT: {buf}")

    # ------------------------------------------------------------------
    # Gateway status methods
    # ------------------------------------------------------------------

    def _get_hardware_info(self) -> str:
        """Detect hardware platform information."""
        try:
            # Try to read Raspberry Pi model from /proc/cpuinfo
            with open('/proc/cpuinfo', 'r') as f:
                for line in f:
                    if line.startswith('Model'):
                        return line.split(':', 1)[1].strip()
            
            # Fallback to platform info
            return f"{platform.system()} {platform.machine()}"
        except Exception:
            return f"{platform.system()} {platform.machine()}"

    def _get_version(self) -> str:
        """Get OpenWXSDR version from the package's single source of truth."""
        return f"OpenWXSDR V{__version__}"

    def _start_gateway_status_thread(self):
        """Start background thread for publishing gateway status."""
        if self._gateway_running:
            return
        
        self._gateway_running = True
        self._gateway_thread = threading.Thread(
            target=self._gateway_status_loop,
            daemon=True,
            name="MQTT-Gateway-Status"
        )
        self._gateway_thread.start()
        self.logger.info(
            f"Gateway status publishing started (interval: {self._upload_interval_minutes} min)"
        )

    def _gateway_status_loop(self):
        """Background thread that publishes gateway status at configured interval."""
        # Publish initial status immediately
        time.sleep(5)  # Give system time to start up
        if self._connected:
            self._publish_gateway_status()
        
        while self._gateway_running and self.enabled:
            try:
                # Sleep for the configured interval
                time.sleep(self._upload_interval_minutes * 60)
                
                if not self._connected:
                    continue
                
                # Clean up stale sondes (not seen in last 5 minutes)
                self._cleanup_stale_sondes()
                
                # Build and publish gateway status
                self._publish_gateway_status()
                
            except Exception as e:
                self.logger.error(f"Error in gateway status loop: {e}", exc_info=True)
                time.sleep(60)  # Wait 1 minute before retry on error

    def _cleanup_stale_sondes(self, timeout_seconds: int = 300):
        """Remove sondes from active set that haven't sent frames recently."""
        try:
            with self._lock:
                current_time = time.time()
                stale_sondes = [
                    serial for serial, last_seen in self._sonde_last_seen.items()
                    if current_time - last_seen > timeout_seconds
                ]
                for serial in stale_sondes:
                    self._active_sondes.discard(serial)
                    del self._sonde_last_seen[serial]
                
                if stale_sondes:
                    self.logger.debug(f"Cleaned up {len(stale_sondes)} stale sondes: {stale_sondes}")
        except Exception as e:
            self.logger.error(f"Error cleaning up stale sondes: {e}")

    def _publish_gateway_status(self):
        """Publish gateway status message to MQTT broker."""
        if not self._connected or not self._client:
            return
        
        try:
            uptime_seconds = int(time.time() - self._start_time)
            
            status = {
                "gateway_id": self._gateway_id,
                "online": True,
                "last_seen": int(time.time()),
                "receiver": self._receiver,
                "antenna": self._antenna,
                "lat": round(self._gateway_lat, 5),
                "lon": round(self._gateway_lon, 5),
                "total_frames": self._total_frames,
                "active_sondes": len(self._active_sondes),
                "hardware": self._hardware_info,
                "active": uptime_seconds,
                "version": self._version
            }
            
            # Publish to OPENWXSDR/{gateway_id}/status
            topic = f"OPENWXSDR/{self._gateway_id}/status"
            json_data = json.dumps(status, separators=(',', ':'))
            
            result = self._client.publish(topic, json_data, qos=0, retain=True)
            
            if result.rc == 0:
                self.logger.info(
                    f"Gateway status published: uptime={uptime_seconds}s, "
                    f"frames={self._total_frames}, active_sondes={len(self._active_sondes)}"
                )
            else:
                self.logger.warning(f"Gateway status publish failed (rc={result.rc})")
                
        except Exception as e:
            self.logger.error(f"Failed to publish gateway status: {e}", exc_info=True)

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def send_telemetry(self, telemetry: SondeTelemetry):
        """Publish a single telemetry frame to the MQTT broker."""
        if not self.enabled or self._client is None:
            return

        if self._debug_mqtt:
            self.logger.info(
                f"MQTT send_telemetry: serial={telemetry.serial}, "
                f"connected={self._connected}, server={self.server}:{self.port}"
            )

        if not self._connected:
            try:
                self._client.reconnect()
            except Exception:
                pass
            self.logger.warning(
                f"MQTT not connected to {self.server}:{self.port} — "
                f"skipping publish for {telemetry.serial}"
            )
            return

        try:
            # Track statistics for gateway status
            with self._lock:
                self._total_frames += 1
                if telemetry.serial:
                    self._active_sondes.add(telemetry.serial)
                    self._sonde_last_seen[telemetry.serial] = time.time()
            
            payload = self._build_payload(telemetry)
            
            # Skip if payload is empty (malformed serial rejected)
            if not payload:
                self.logger.debug(f"MQTT: Empty payload for {telemetry.serial}, skipping publish")
                return
            
            topic = self.topic_prefix.strip() or 'openwxsdr/'
            json_data = json.dumps(payload, separators=(',', ':'))

            if self._debug_mqtt:
                self.logger.info(
                    f"MQTT publishing to topic '{topic}': {json_data[:200]}"
                )

            result = self._client.publish(topic, json_data, qos=0, retain=False)

            if result.rc == 0:
                if self._debug_mqtt:
                    self.logger.info(
                        f"MQTT published OK: {telemetry.serial} → {topic}"
                    )
            else:
                self.logger.warning(f"MQTT publish failed (rc={result.rc})")

        except Exception as e:
            self.logger.error(f"Failed to publish MQTT telemetry: {e}", exc_info=True)

    def close(self):
        """Disconnect from the broker and stop the network loop."""
        # Stop gateway status thread
        self._gateway_running = False
        if self._gateway_thread and self._gateway_thread.is_alive():
            self._gateway_thread.join(timeout=2)
        
        if self._client:
            try:
                # Publish final offline status before disconnecting
                if self._connected and self._upload_position:
                    self._publish_offline_status()
                
                self._client.loop_stop()
                self._client.disconnect()
            except Exception:
                pass
            self._client = None
        self._connected = False
        self.logger.info("MQTT output closed")

    def _publish_offline_status(self):
        """Publish offline status before shutdown."""
        try:
            uptime_seconds = int(time.time() - self._start_time)
            status = {
                "gateway_id": self._gateway_id,
                "online": False,
                "last_seen": int(time.time()),
                "receiver": self._receiver,
                "antenna": self._antenna,
                "lat": round(self._gateway_lat, 5),
                "lon": round(self._gateway_lon, 5),
                "total_frames": self._total_frames,
                "active_sondes": 0,
                "hardware": self._hardware_info,
                "active": uptime_seconds,
                "version": self._version
            }
            
            topic = f"OPENWXSDR/{self._gateway_id}/status"
            json_data = json.dumps(status, separators=(',', ':'))
            self._client.publish(topic, json_data, qos=0, retain=True)
            self.logger.info("Published offline status")
        except Exception as e:
            self.logger.error(f"Failed to publish offline status: {e}")

    def get_status(self) -> dict:
        """Return MQTT connection state for health endpoints."""
        if not self.enabled:
            return {'status': 'disabled'}
        return {
            'status': 'connected' if self._connected else 'disconnected',
            'server': self.server,
            'port': self.port,
        }

    # ------------------------------------------------------------------
    # Payload builder
    # ------------------------------------------------------------------

    def _build_payload(self, telemetry: SondeTelemetry) -> dict:
        """
        Build MQTT JSON payload matching the OpenWX.de broker format.

        Required format:
        {"gw": "DB0DAN", "active": 1, "freq": 405.7, "id": "V1010401",
         "ser": "V1010401", "validId": 1, "launchsite": "", "lat": 53.1,
         "lon": 10.02, "alt": 35121.1, "vvel": 3.3, "speed": 11.8,
         "dir": 350.9, "sats": 10, "validPos": 127, "time": 1778071928,
         "frame": 8676, "validTime": 1, "rssi": 0, "afc": 0,
         "launchKT": 0, "burstKT": 0,
         "tmp": -33.1, "hum": 0.0, "pres": 5.3, "batt": 2.6,
         "subtype": "RS41-SGP"}
        """
        station_cfg = self.config.get('station', {})
        callsign    = station_cfg.get('callsign', 'OPENWXSDR')

        freq_mhz  = round(telemetry.frequency / 1e6, 3)
        sonde_id  = telemetry.serial or 'UNKNOWN'
        
        # Reject obviously malformed serials (partial decode artifacts like '-+', '---', etc.)
        # Allow UNKNOWN but flag it with validId=0
        import re
        if sonde_id != 'UNKNOWN' and re.match(r'^[-+\s]+$', sonde_id):
            self.logger.warning(
                f"[MQTT] Rejecting malformed serial '{sonde_id}' (likely partial decode), "
                f"skipping telemetry upload"
            )
            return {}  # Return empty payload to skip upload

        payload: dict = {
            'gw':         callsign,
            'active':     1,
            'freq':       freq_mhz,
            'id':         sonde_id,
            'ser':        sonde_id,
            'validId':    1 if sonde_id not in ('UNKNOWN', '') else 0,
            'launchsite': '',
            # Position defaults
            'lat':        0.0,
            'lon':        0.0,
            'alt':        0.0,
            'validPos':   0,
            # Velocity defaults
            'vvel':       0.0,
            'speed':      0.0,
            'dir':        0.0,
            # Timing
            'sats':       0,
            'time':       int(datetime.utcnow().timestamp()),
            'validTime':  1,
            'frame':      telemetry.frame_number,
            # RF
            'rssi':       round(telemetry.rssi, 1) if telemetry.rssi else 0,
            'afc':        0,
            # Burst / launch (not available in auto_rx pipeline)
            'launchKT':   0,
            'burstKT':    0,
            # Environment defaults
            'tmp':        0.0,
            'hum':        0.0,
            'pres':       0.0,
            'batt':       0.0,
            'subtype':    telemetry.subtype or telemetry.sonde_type,
        }

        if telemetry.position:
            import calendar
            pos = telemetry.position
            payload.update({
                'lat':      round(pos.latitude,  5),
                'lon':      round(pos.longitude, 5),
                'alt':      round(pos.altitude,  1),
                'validPos': 127,
                'time':     int(calendar.timegm(pos.datetime.timetuple())),
            })

        if telemetry.velocity:
            vel = telemetry.velocity
            payload.update({
                'vvel':  round(vel.vertical_speed,   1),
                'speed': round(vel.horizontal_speed, 1),
                'dir':   round(vel.heading,          1),
            })

        if telemetry.environment:
            env = telemetry.environment
            if env.temperature is not None:
                payload['tmp']  = round(env.temperature, 1)
            if env.humidity is not None:
                payload['hum']  = round(env.humidity,    1)
            if env.pressure is not None:
                payload['pres'] = round(env.pressure,    2)

        if telemetry.satellites is not None:
            try:
                sats = int(telemetry.satellites)
                if sats >= 0:
                    payload['sats'] = sats
            except (TypeError, ValueError):
                pass

        # Battery
        if telemetry.battery is not None:
            payload['batt'] = round(telemetry.battery, 2)

        # RS41-specific fields
        if hasattr(telemetry, 'burst_timer') and telemetry.burst_timer is not None:
            payload['burst_timer'] = int(telemetry.burst_timer)
        if hasattr(telemetry, 'rs41_mainboard') and telemetry.rs41_mainboard is not None:
            payload['rs41_mainboard'] = str(telemetry.rs41_mainboard)
        if hasattr(telemetry, 'rs41_mainboard_fw') and telemetry.rs41_mainboard_fw is not None:
            payload['rs41_mainboard_fw'] = int(telemetry.rs41_mainboard_fw)
        if hasattr(telemetry, 'ref_datetime') and telemetry.ref_datetime is not None:
            payload['ref_datetime'] = str(telemetry.ref_datetime)
        if hasattr(telemetry, 'ref_position') and telemetry.ref_position is not None:
            payload['ref_position'] = str(telemetry.ref_position)
        if hasattr(telemetry, 'tx_frequency') and telemetry.tx_frequency is not None:
            payload['tx_frequency'] = round(float(telemetry.tx_frequency) / 1e6, 3)  # Convert Hz to MHz

        return payload

