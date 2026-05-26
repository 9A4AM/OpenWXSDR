"""
# =============================================================================
#  OpenWX -- Open Weather Radiosonde Telemetry System
# =============================================================================
#
#  File   : openwxsdr_app.py
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
#  Main application entry point and top-level orchestrator for OpenWX.
#
#  The OpenWXSDR class initializes, wires together, and manages the lifecycle
#  of all subsystems based on the active SDR backend type configured in
#  config.yaml. Supported backends: rtlsdr, airspy, ka9q, flux242.
#
#  Component lifecycle:
#    initialize() ? start() ? _main_loop() / _flux242_main_loop() ? stop()
#
#  Subsystems managed:
#    SDR backends    : RTLSDRDeviceManager, AirspyReceiver, KA9QReceiver,
#                      Flux242Receiver
#    Decoder backend : DecoderManager (rs1729)
#    Output plugins  : UDPOutput, MQTTOutput, HttpOutput,
#                      SondeHubOutput / SondeHubQueueOutput
#    Web interface   : WebUI (Flask + Leaflet map)
#
# =============================================================================
"""

import logging
import signal
import sys
import time
from typing import Optional, TYPE_CHECKING

from .sdr.rtlsdr_analyzer import SpectrumAnalyzer
from .sdr.ka9q_receiver import KA9QReceiver
from .sdr.flux242_receiver import Flux242Receiver, Flux242Config
from .sdr.device_manager import RTLSDRDeviceManager
from .decoders.decoder_manager import DecoderManager
from .decoders.models import SondeTelemetry
from .output.udp_output import UDPOutput
from .output.mqtt_output import MQTTOutput
from .output.http_output import HttpOutput
from .output.sondehub_output import SondeHubOutput
from .output.sondehub_queue import SondeHubQueueOutput
from .webui.web_server import WebUI

if TYPE_CHECKING:
    from .sdr.airspy_receiver import AirspyReceiver


class OpenWXSDR:
    """Main application coordinator"""
    
    def __init__(self, config: dict):
        self.config = config
        self.logger = logging.getLogger('OpenWXSDR')
        self.running = False
        
        # Components
        self.spectrum_analyzer: Optional[SpectrumAnalyzer] = None
        self.ka9q_receiver: Optional[KA9QReceiver] = None
        self.flux242_receiver: Optional[Flux242Receiver] = None
        self.decoder_manager: Optional[DecoderManager] = None
        self.device_manager: Optional[RTLSDRDeviceManager] = None
        self.airspy_receiver: Optional['AirspyReceiver'] = None
        self.udp_output: Optional[UDPOutput] = None
        self.mqtt_output: Optional[MQTTOutput] = None
        self.http_output: Optional[HttpOutput] = None
        self.sondehub_output: Optional[object] = None
        self.webui: Optional[WebUI] = None
        
        # Setup signal handlers
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)
    
    def initialize(self) -> bool:
        """Initialize all components"""
        self.logger.info("Initializing OpenWXSDR...")
        
        try:
            # Initialize SDR
            sdr_type = self.config['sdr']['type']
            
            if sdr_type == 'rtlsdr':
                self.logger.info("Initializing RTL-SDR device manager...")
                self.device_manager = RTLSDRDeviceManager(
                    self.config, self._handle_telemetry
                )
                if not self.device_manager.initialize():
                    self.logger.error("Failed to initialize RTL-SDR device manager")
                    return False
            
            elif sdr_type == 'ka9q':
                self.logger.info("Initializing KA9Q receiver...")
                self.ka9q_receiver = KA9QReceiver(self.config)
                if not self.ka9q_receiver.initialize():
                    self.logger.error("Failed to initialize KA9Q receiver")
                    return False
            
            elif sdr_type == 'flux242':
                self.logger.info("Initializing Flux242 receiver (receivemultisonde.sh)...")
                # For flux242, we don't need decoder_manager or spectrum_analyzer
                # The flux242 script handles everything internally
                flux_cfg = self.config['sdr']['flux242']
                flux242_config = Flux242Config(
                    center_freq=flux_cfg.get('center_freq', 403405000),
                    sample_rate=flux_cfg.get('sample_rate', 2400000),
                    gain=flux_cfg.get('gain', 40),
                    ppm_error=flux_cfg.get('ppm_error', 0),
                    threshold=flux_cfg.get('threshold', 4),
                    udp_port=flux_cfg.get('udp_port', 5678),
                    power_port=flux_cfg.get('power_port', 5676),
                    debug_port=flux_cfg.get('debug_port', 5675),
                    script_path=flux_cfg.get('script_path', './radiosonde/scripts/receivemultisonde.sh')
                )
                self.flux242_receiver = Flux242Receiver(flux242_config, self._handle_flux242_telemetry)
                
                # Skip decoder manager for flux242 mode
                self.decoder_manager = None
            
            elif sdr_type == 'airspy':
                self.logger.info("Initializing Airspy receiver...")
                try:
                    from .sdr.airspy_receiver import AirspyReceiver
                except Exception as e:
                    self.logger.error(f"Failed to import AirspyReceiver: {e}", exc_info=True)
                    return False
                self.airspy_receiver = AirspyReceiver(
                    self.config, self._handle_telemetry
                )
                if not self.airspy_receiver.initialize():
                    self.logger.error("Failed to initialize Airspy receiver")
                    return False

            else:
                self.logger.error(f"Unknown SDR type: {sdr_type}")
                return False
            
            # Initialize decoder manager (only for ka9q mode; rtlsdr uses DeviceManager)
            if sdr_type == 'ka9q':
                self.logger.info("Initializing decoder manager...")
                self.decoder_manager = DecoderManager(
                    self.config,
                    self._handle_telemetry,
                    spectrum_analyzer=None
                )
            
            # Initialize output
            self.logger.info("Initializing UDP output...")
            self.udp_output = UDPOutput(self.config)

            # Initialize MQTT output (optional, enabled via openwx.mqtt.enabled)
            self.logger.info("Initializing MQTT output...")
            self.mqtt_output = MQTTOutput(self.config)

            # Initialize HTTP output (optional, enabled via openwx.http.enabled)
            self.logger.info("Initializing HTTP output...")
            self.http_output = HttpOutput(self.config)

            # Initialize SondeHub output (optional, enabled via sondehub.enabled)
            self.logger.info("Initializing SondeHub output...")
            sondehub_cfg = self.config.get('sondehub', {})
            if bool(sondehub_cfg.get('queue_mode', False)):
                self.logger.info("SondeHub uploader mode: queue")
                self.sondehub_output = SondeHubQueueOutput(self.config)
            else:
                self.logger.info("SondeHub uploader mode: direct")
                self.sondehub_output = SondeHubOutput(self.config)
            
            # Initialize web UI
            self.logger.info("Initializing web UI...")
            self.webui = WebUI(self.config)
            
            # Set component references for health monitoring
            if self.webui:
                self.webui.set_components(
                    spectrum_analyzer=None,
                    decoder_manager=(
                        self.airspy_receiver or
                        self.device_manager or
                        self.decoder_manager
                    ),
                    flux242_receiver=self.flux242_receiver,
                    mqtt_output=self.mqtt_output,
                    sondehub_output=self.sondehub_output
                )
            
            self.logger.info("Initialization complete!")
            return True
            
        except Exception as e:
            self.logger.error(f"Initialization failed: {e}", exc_info=True)
            return False
    
    def start(self):
        """Start all components"""
        self.logger.info("Starting OpenWXSDR...")
        self.running = True
        
        try:
            # Start web UI
            if self.webui:
                self.webui.start()
            
            # Start decoder manager (only for rtlsdr/ka9q modes)
            if self.decoder_manager:
                self.decoder_manager.start()
            
            # Start appropriate SDR
            sdr_type = self.config['sdr']['type']
            
            if sdr_type == 'rtlsdr':
                # DeviceWorkers handle everything; just start them
                if self.device_manager:
                    self.device_manager.start()
            
            elif sdr_type == 'ka9q':
                if self.ka9q_receiver:
                    self.ka9q_receiver.start_receiving()
            
            elif sdr_type == 'flux242':
                # Start flux242 receiver
                if self.flux242_receiver:
                    if not self.flux242_receiver.start():
                        self.logger.error("Failed to start flux242 receiver!")
                        self.stop()
                        return

            elif sdr_type == 'airspy':
                if self.airspy_receiver:
                    self.airspy_receiver.start()

            self.logger.info("OpenWXSDR started successfully!")
            self.logger.info(f"Web UI available at http://localhost:{self.config['webui']['port']}")
            
            # Main loop (different for flux242 vs others)
            if sdr_type == 'flux242':
                self._flux242_main_loop()
            else:
                self._main_loop()  # airspy, rtlsdr, ka9q all use the same idle main loop
            
        except Exception as e:
            self.logger.error(f"Error during operation: {e}", exc_info=True)
            self.stop()
    
    def _main_loop(self):
        """Main application loop for rtlsdr/ka9q modes"""
        while self.running:
            try:
                # RTL-SDR mode: DeviceWorkers manage scan/decode internally
                # KA9Q mode: decoder_manager handles signals from ka9q_receiver
                if self.spectrum_analyzer and self.decoder_manager:
                    signals = self.spectrum_analyzer.get_detected_signals()
                    if signals:
                        self.decoder_manager.update_signals(signals)

                time.sleep(1)

            except Exception as e:
                self.logger.error(f"Error in main loop: {e}", exc_info=True)
                time.sleep(5)
    
    def _flux242_main_loop(self):
        """Main application loop for flux242 mode"""
        # flux242 runs autonomously, we just need to keep process alive
        # and monitor health
        while self.running:
            try:
                if self.flux242_receiver:
                    status = self.flux242_receiver.get_status()
                    if not status['running'] or not status['process_alive']:
                        self.logger.error("Flux242 receiver died, stopping...")
                        self.running = False
                        break
                
                time.sleep(5)
                
            except Exception as e:
                self.logger.error(f"Error in flux242 main loop: {e}", exc_info=True)
                time.sleep(5)
    
    def stop(self):
        """Stop all components"""
        self.logger.info("Stopping OpenWXSDR...")
        self.running = False
        
        # Stop components in reverse order
        if self.device_manager:
            self.device_manager.stop()

        if self.decoder_manager:
            self.decoder_manager.stop()
        
        if self.spectrum_analyzer:
            self.spectrum_analyzer.stop_scanning()
            self.spectrum_analyzer.close()
        
        if self.ka9q_receiver:
            self.ka9q_receiver.stop_receiving()
            self.ka9q_receiver.close()
        
        if self.flux242_receiver:
            self.flux242_receiver.stop()

        if self.airspy_receiver:
            self.airspy_receiver.stop()
        
        if self.udp_output:
            self.udp_output.close()

        if self.mqtt_output:
            self.mqtt_output.close()

        if self.http_output:
            self.http_output.close()

        if self.sondehub_output:
            self.sondehub_output.close()
        
        self.logger.info("OpenWXSDR stopped")
    
    def _check_priority_frequency(self):
        """
        Check priority frequency before starting scanner
        Waits for configured timeout to see if signal can be decoded
        """
        priority_freq_mhz = self.config.get('detection', {}).get('priority_frequency')
        timeout = self.config.get('detection', {}).get('priority_check_timeout', 30)
        
        # Skip if no priority frequency configured
        if not priority_freq_mhz or priority_freq_mhz <= 0:
            return
        
        priority_freq = priority_freq_mhz * 1e6  # Convert MHz to Hz
        
        # Get optional sonde type hint
        sonde_type_hint = self.config.get('detection', {}).get('priority_sonde_type')
        
        # Determine bandwidth based on sonde type
        # Different sonde types have characteristic bandwidths:
        bandwidth_map = {
            'RS41': 4500,   # 4-5 kHz
            'RS92': 2800,   # 2.6-3 kHz  
            'DFM': 7500,    # 6-9 kHz (DFM needs wider BW!)
            'M10': 9000,    # 9-15 kHz
            'M20': 20000,   # 18-22 kHz
            'iMet': 12000   # 10-15 kHz
        }
        
        if sonde_type_hint and sonde_type_hint.upper() in bandwidth_map:
            bandwidth = bandwidth_map[sonde_type_hint.upper()]
            self.logger.info(f"Checking priority frequency: {priority_freq_mhz:.3f} MHz as {sonde_type_hint} for {timeout}s")
        else:
            # Default to middle-range bandwidth that won't bias detection
            bandwidth = 7000  # Neutral value between RS41 and DFM
            self.logger.info(f"Checking priority frequency: {priority_freq_mhz:.3f} MHz (auto-detect) for {timeout}s before starting scanner")
        
        try:
            # Create a signal for the priority frequency
            from .sdr.rtlsdr_analyzer import DetectedSignal
            priority_signal = DetectedSignal(
                frequency=priority_freq,
                strength=25.0,  # Assume good signal
                bandwidth=bandwidth,
                timestamp=time.time()
            )
            
            # Try to start decoder for priority frequency
            if self.decoder_manager:
                # Inject the priority signal
                self.decoder_manager.update_signals([priority_signal])
                
                # Wait for timeout to see if frames are decoded
                start_time = time.time()
                frames_received = False
                
                while time.time() - start_time < timeout:
                    # Check if we're receiving frames
                    if self.webui and len(self.webui.sondes) > 0:
                        frames_received = True
                        self.logger.info(f"Priority frequency is decoding successfully - keeping decoder active")
                        break
                    
                    time.sleep(1)
                
                if not frames_received:
                    self.logger.info(f"No frames decoded on priority frequency after {timeout}s - will start scanner")
                    # The decoder manager will handle cleanup of idle decoders
            
        except Exception as e:
            self.logger.error(f"Error checking priority frequency: {e}", exc_info=True)
    
    def _handle_telemetry(self, telemetry: SondeTelemetry):
        """Handle decoded telemetry from decoders (rtlsdr/ka9q modes)"""
        try:
            self.logger.debug(f"[TELEMETRY] Received: serial={telemetry.serial}, type={telemetry.sonde_type}")
            # Log telemetry
            self.logger.info(
                f"Telemetry: {telemetry.sonde_type} {telemetry.serial} "
                f"F{telemetry.frame_number} "
                f"{telemetry.position.latitude:.5f},{telemetry.position.longitude:.5f} "
                f"Alt:{telemetry.position.altitude:.0f}m"
                if telemetry.position else ""
            )
            
            # Send to web UI
            if self.webui:
                self.webui.add_telemetry(telemetry)
            
            # Send to OpenWX via UDP
            if self.udp_output:
                self.udp_output.send_telemetry(telemetry)

            # Publish via MQTT
            if self.mqtt_output:
                self.mqtt_output.send_telemetry(telemetry)

            # Upload via HTTP
            if self.http_output:
                self.http_output.send_telemetry(telemetry)

            # Upload to SondeHub
            if self.sondehub_output:
                self.logger.debug(f"[TELEMETRY] Routing to SondeHub output for {telemetry.serial}")
                self.sondehub_output.send_telemetry(telemetry)
            else:
                self.logger.debug(f"[TELEMETRY] SondeHub output not initialized")
            
        except Exception as e:
            self.logger.error(f"Error handling telemetry: {e}", exc_info=True)
    
    def _handle_flux242_telemetry(self, telemetry_dict: dict):
        """Handle decoded telemetry from flux242 receiver (dict format)"""
        try:
            from .decoders.models import SondePosition, SondeVelocity, SondeEnvironment
            from datetime import datetime
            
            # Convert dict to SondeTelemetry object for compatibility
            position = None
            velocity = None
            environment = None
            
            # Parse datetime from ISO format (e.g., "2026-05-04T12:17:31.992Z")
            dt = None
            if telemetry_dict.get('datetime'):
                try:
                    dt = datetime.fromisoformat(telemetry_dict['datetime'].replace('Z', '+00:00'))
                except:
                    dt = datetime.utcnow()
            else:
                dt = datetime.utcnow()
            
            if telemetry_dict.get('lat') and telemetry_dict.get('lon'):
                position = SondePosition(
                    latitude=telemetry_dict['lat'],
                    longitude=telemetry_dict['lon'],
                    altitude=telemetry_dict.get('alt', 0),
                    datetime=dt
                )
            
            if telemetry_dict.get('vel_h') is not None:
                velocity = SondeVelocity(
                    horizontal_speed=telemetry_dict['vel_h'],
                    vertical_speed=telemetry_dict.get('vel_v', 0),
                    heading=telemetry_dict.get('heading', 0)
                )
            
            # Environmental data
            if telemetry_dict.get('temp') or telemetry_dict.get('humidity') or telemetry_dict.get('pressure'):
                environment = SondeEnvironment(
                    temperature=telemetry_dict.get('temp'),
                    humidity=telemetry_dict.get('humidity'),
                    pressure=telemetry_dict.get('pressure')
                )
            
            # Get frequency (flux242_receiver already converted to MHz)
            frequency_mhz = telemetry_dict.get('frequency', 0.0)
            rssi_value = telemetry_dict.get('rssi')
            if rssi_value is None:
                rssi_value = telemetry_dict.get('power_db', telemetry_dict.get('signal_db'))
            snr_value = telemetry_dict.get('snr')
            if snr_value is None:
                snr_value = telemetry_dict.get('signal_strength')
            
            telemetry = SondeTelemetry(
                serial=telemetry_dict.get('serial', 'UNKNOWN'),
                sonde_type=telemetry_dict.get('type', 'Unknown'),
                frame_number=telemetry_dict.get('frame', 0),
                position=position,
                velocity=velocity,
                environment=environment,
                satellites=telemetry_dict.get('sats'),
                frequency=frequency_mhz * 1e6,  # Convert MHz back to Hz for SondeTelemetry
                rssi=rssi_value,
                snr=snr_value,
            )
            
            # Log telemetry
            if telemetry.position:
                self.logger.info(
                    f"Flux242: {telemetry.sonde_type} {telemetry.serial} "
                    f"F{telemetry.frame_number} "
                    f"{telemetry.position.latitude:.5f},{telemetry.position.longitude:.5f} "
                    f"Alt:{telemetry.position.altitude:.0f}m "
                    f"Freq:{frequency_mhz:.3f}MHz"
                )
            else:
                self.logger.debug(f"Flux242: {telemetry.sonde_type} {telemetry.serial} F{telemetry.frame_number}")
            
            # Send to web UI
            if self.webui:
                self.webui.add_telemetry(telemetry)
            
            # Send to OpenWX via UDP
            if self.udp_output:
                self.udp_output.send_telemetry(telemetry)

            # Publish via MQTT
            if self.mqtt_output:
                self.mqtt_output.send_telemetry(telemetry)

            # Upload via HTTP
            if self.http_output:
                self.http_output.send_telemetry(telemetry)

            # Upload to SondeHub
            if self.sondehub_output:
                self.logger.debug(f"[TELEMETRY-Flux242] Routing to SondeHub output for {telemetry.serial}")
                self.sondehub_output.send_telemetry(telemetry)
            else:
                self.logger.debug(f"[TELEMETRY-Flux242] SondeHub output not initialized")
            
        except Exception as e:
            self.logger.error(f"Error handling flux242 telemetry: {e}", exc_info=True)
    
    def _signal_handler(self, signum, frame):
        """Handle shutdown signals"""
        self.logger.info(f"Received signal {signum}, shutting down...")
        self.stop()
        sys.exit(0)
