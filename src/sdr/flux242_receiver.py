"""
# =============================================================================
#  OpenWX -- Open Weather Radiosonde Telemetry System
# =============================================================================
#
#  File   : flux242_receiver.py
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
#  Flux242 radiosonde receiver interface for OpenWX.
#
#  Integrates with the external flux242/radiosonde project by launching the
#  receivemultisonde.sh shell script as a subprocess and listening for
#  decoded telemetry frames delivered as JSON via UDP broadcast.
#
#  Architecture:
#    receivemultisonde.sh (RTL-SDR ? iq_server ? decoders)
#      +-- UDP broadcast ? Flux242Receiver._udp_listener ? telemetry_callback
#
#  Supported sonde types : RS41, DFM, M10, iMet (via flux242 decoders)
#  External dependency   : flux242/radiosonde (github.com/flux242/radiosonde)
#
# =============================================================================
"""

import socket
import json
import logging
import threading
import subprocess
import time
import os
from typing import Optional, Callable
from datetime import datetime
from dataclasses import dataclass


@dataclass
class Flux242Config:
    """Configuration for flux242 receiver"""
    center_freq: int  # Center frequency in Hz
    sample_rate: int  # Sample rate (2.4 MHz recommended)
    gain: int  # Tuner gain
    ppm_error: int  # PPM correction
    threshold: int  # Detection threshold in dB (default 4)
    udp_port: int  # UDP port for decoded frames (default 5678)
    power_port: int  # UDP port for power scanning (default 5676)
    debug_port: int  # UDP port for debug info (default 5675)
    script_path: str  # Path to receivemultisonde.sh


class Flux242Receiver:
    """
    Receives decoded radiosonde data from flux242/radiosonde project
    """
    
    def __init__(self, config: Flux242Config, telemetry_callback: Callable):
        self.config = config
        self.telemetry_callback = telemetry_callback
        self.logger = logging.getLogger('Flux242Receiver')
        
        self.running = False
        self.process: Optional[subprocess.Popen] = None
        self.udp_thread: Optional[threading.Thread] = None
        self.monitor_thread: Optional[threading.Thread] = None
        
        # Track received sondes
        self.active_sondes = {}
        
    def start(self) -> bool:
        """Start flux242 receivemultisonde.sh script and UDP listener"""
        
        # Check if script exists
        if not os.path.isfile(self.config.script_path):
            self.logger.error(f"receivemultisonde.sh not found at: {self.config.script_path}")
            self.logger.error("Please clone and compile flux242/radiosonde:")
            self.logger.error("  git clone https://github.com/flux242/radiosonde.git")
            self.logger.error("  cd radiosonde/decoders && make")
            self.logger.error("  cd ../iq_svcl && make")
            return False
        
        try:
            # Start receivemultisonde.sh
            # Example: ./receivemultisonde.sh -f 403405000 -s 2400000 -P 35 -g 40 -t 4
            cmd = [
                'bash',
                self.config.script_path,
                '-f', str(self.config.center_freq),
                '-s', str(self.config.sample_rate),
                '-P', str(self.config.ppm_error),
                '-g', str(self.config.gain),
                '-t', str(self.config.threshold)
            ]
            
            self.logger.info(f"Starting flux242 receiver: {' '.join(cmd)}")
            
            # Start in scripts directory
            script_dir = os.path.dirname(self.config.script_path)
            
            # Add iq_svcl to PATH so receivemultisonde.sh can find iq_server
            # Assumes standard flux242 directory structure: scripts/ and iq_svcl/ are siblings
            env = os.environ.copy()
            radiosonde_root = os.path.dirname(script_dir)
            iq_svcl_path = os.path.join(radiosonde_root, 'iq_svcl')
            
            if os.path.isdir(iq_svcl_path):
                env['PATH'] = f"{iq_svcl_path}:{script_dir}:{env.get('PATH', '')}"
                self.logger.debug(f"Added to PATH: {iq_svcl_path}")
            
            self.process = subprocess.Popen(
                cmd,
                cwd=script_dir,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )
            
            self.running = True
            
            # Start UDP listener for decoded frames
            self.udp_thread = threading.Thread(target=self._udp_listener, daemon=True)
            self.udp_thread.start()
            
            # Start monitor thread for process health
            self.monitor_thread = threading.Thread(target=self._monitor_process, daemon=True)
            self.monitor_thread.start()
            
            self.logger.info(f"Flux242 receiver started, listening on UDP port {self.config.udp_port}")
            return True
            
        except Exception as e:
            self.logger.error(f"Failed to start flux242 receiver: {e}", exc_info=True)
            return False
    
    def stop(self):
        """Stop receiver and cleanup"""
        self.running = False
        
        if self.process:
            self.logger.info("Stopping flux242 receiver...")
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
            self.process = None
        
        if self.udp_thread:
            self.udp_thread.join(timeout=2)
        if self.monitor_thread:
            self.monitor_thread.join(timeout=2)
        
        self.logger.info("Flux242 receiver stopped")
    
    def _udp_listener(self):
        """Listen for UDP JSON frames from receivemultisonde.sh"""
        
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)  # Enable broadcast reception
        sock.settimeout(1.0)
        
        try:
            sock.bind(('', self.config.udp_port))  # Bind to all interfaces to receive broadcast
            self.logger.info(f"UDP listener bound to port {self.config.udp_port}")
            
            while self.running:
                try:
                    data, addr = sock.recvfrom(4096)
                    if data:
                        self.logger.info(f"Received {len(data)} bytes from {addr}")
                        self._process_json_frame(data.decode('utf-8'))
                except socket.timeout:
                    continue
                except Exception as e:
                    self.logger.error(f"Error receiving UDP data: {e}")
                    
        except Exception as e:
            self.logger.error(f"Failed to bind UDP socket: {e}")
        finally:
            sock.close()
    
    def _process_json_frame(self, json_str: str):
        """
        Process JSON frame from flux242
        
        Example frame:
        {"type":"RS41","frame":3044,"id":"S3541192","datetime":"2021-04-10T05:16:25.000Z",
         "lat":48.88825,"lon":9.54869,"alt":9515.6267,"vel_h":21.0129,
         "heading":76.92116,"vel_v":3.66779,"sats":10,"bt":65535,"batt":2.8,
         "temp":-53.8,"humidity":65.6,"pressure":283.34,"subtype":"RS41-SGP",
         "freq":"404500000"}
        """
        try:
            frame = json.loads(json_str)
            
            # Extract data
            sonde_type = frame.get('type', 'Unknown')
            serial = frame.get('id', 'UNKNOWN')
            
            # Parse datetime
            timestamp_str = frame.get('datetime')
            if timestamp_str:
                timestamp = datetime.fromisoformat(timestamp_str.replace('Z', '+00:00'))
            else:
                timestamp = datetime.utcnow()
            
            # Parse frequency (comes as string in Hz)
            freq_str = frame.get('freq', '0')
            frequency = float(freq_str) / 1e6  # Convert Hz to MHz
            
            # Create telemetry object matching our existing format
            telemetry = {
                'serial': serial,
                'type': sonde_type,
                'subtype': frame.get('subtype'),
                'frame': frame.get('frame', 0),
                'timestamp': timestamp,
                'lat': frame.get('lat'),
                'lon': frame.get('lon'),
                'alt': frame.get('alt'),
                'vel_h': frame.get('vel_h'),
                'vel_v': frame.get('vel_v'),
                'heading': frame.get('heading'),
                'sats': frame.get('sats'),
                'battery': frame.get('batt'),
                'temp': frame.get('temp'),
                'humidity': frame.get('humidity'),
                'pressure': frame.get('pressure'),
                'frequency': frequency
            }
            
            # Track active sondes
            self.active_sondes[serial] = time.time()
            
            # Send to callback
            if self.telemetry_callback:
                self.telemetry_callback(telemetry)
                self.logger.info(f"Decoded {sonde_type} frame from {serial} at {frequency:.3f} MHz (Frame #{frame.get('frame', 0)})")
            else:
                self.logger.warning("No telemetry callback configured!")
            
        except json.JSONDecodeError as e:
            self.logger.warning(f"Invalid JSON received: {json_str[:100]}")
        except Exception as e:
            self.logger.error(f"Error processing frame: {e}", exc_info=True)
    
    def _monitor_process(self):
        """Monitor receivemultisonde.sh process health"""
        while self.running and self.process:
            # Check if process is still alive
            if self.process.poll() is not None:
                self.logger.error("receivemultisonde.sh process died unexpectedly!")
                self.logger.error(f"Exit code: {self.process.returncode}")
                
                # Log stderr
                if self.process.stderr:
                    stderr = self.process.stderr.read()
                    if stderr:
                        self.logger.error(f"Process stderr: {stderr}")
                
                self.running = False
                break
            
            time.sleep(5)
    
    def get_status(self) -> dict:
        """Get receiver status"""
        return {
            'running': self.running,
            'process_alive': self.process is not None and self.process.poll() is None,
            'active_sondes': len(self.active_sondes),
            'sonde_list': list(self.active_sondes.keys())
        }
