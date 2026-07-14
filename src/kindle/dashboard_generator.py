"""
# =============================================================================
#  OpenWX -- Open Weather Radiosonde Telemetry System
# =============================================================================
#
#  File   : dashboard_generator.py
#  Author : M.F. Guenther, DL2MF - DL2MF@darc.de
#  License: GNU General Public License v2.0 (GPL-2.0)
#
#  E-ink optimized dashboard image generator for Kindle devices.
#  Generates 600x800 (Touch) and 758x1024 (Paperwhite) grayscale PNGs.
#
# =============================================================================
"""

import io
import time
from datetime import datetime
from PIL import Image, ImageDraw, ImageFont
from typing import Dict, List, Optional, Tuple
import logging

from .. import __version__


class KindleDashboardGenerator:
    """Generates e-ink optimized dashboard images showing receiver and sonde status."""
    
    # Color palette optimized for e-ink displays
    WHITE = 255
    BLACK = 0
    DARK_GRAY = 40
    MID_GRAY = 110
    LIGHT_GRAY = 185
    VERY_LIGHT_GRAY = 220
    
    # Device profiles (width, height, font_scale)
    PROFILES = {
        'touch': (600, 800, 1.0),      # Kindle Touch / Paperwhite 1-3
        'paperwhite': (758, 1024, 1.3), # Kindle Paperwhite 4+
    }
    
    def __init__(self, station_name: str = "OpenWXSDR Gateway", version: str = __version__):
        """Initialize dashboard generator.
        
        Args:
            station_name: Name to display in header
            version: Software version string
        """
        self.logger = logging.getLogger('KindleDashboard')
        self.station_name = station_name
        self.version = version
        self._font_cache = {}
        
    def _get_font(self, size: int, bold: bool = False, scale: float = 1.0) -> ImageFont.FreeTypeFont:
        """Load font with caching and fallback support.
        
        Args:
            size: Base font size in points
            bold: Whether to use bold weight
            scale: Scale factor for different device sizes
            
        Returns:
            PIL ImageFont object
        """
        scaled_size = int(size * scale)
        cache_key = (scaled_size, bold)
        
        if cache_key in self._font_cache:
            return self._font_cache[cache_key]
        
        # Font search paths for Linux systems
        font_paths_bold = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
            "C:\\Windows\\Fonts\\arialbd.ttf",  # Windows fallback
        ]
        font_paths_regular = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/TTF/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
            "C:\\Windows\\Fonts\\arial.ttf",  # Windows fallback
        ]
        
        paths = font_paths_bold if bold else font_paths_regular
        
        for path in paths:
            try:
                font = ImageFont.truetype(path, scaled_size)
                self._font_cache[cache_key] = font
                return font
            except (OSError, IOError):
                continue
        
        # Fallback to default bitmap font
        self.logger.warning(f"Could not load TrueType font, using default")
        font = ImageFont.load_default()
        self._font_cache[cache_key] = font
        return font
    
    def generate_dashboard(
        self,
        device: str,
        receivers: List[Dict],
        sondes: List[Dict],
        system_info: Optional[Dict] = None
    ) -> bytes:
        """Generate dashboard image for specified device type.
        
        Args:
            device: Device type ('touch' or 'paperwhite')
            receivers: List of receiver status dicts with keys:
                - device_id: str (e.g. "RTL00001")
                - state: str (e.g. "SCANNING", "DECODING")
                - frequency: Optional[float] in Hz
                - serial: Optional[str] sonde serial number
            sondes: List of active sonde dicts with telemetry:
                - serial: str
                - type: str (e.g. "RS41")
                - frequency: float in Hz
                - altitude: float in meters
                - latitude: float
                - longitude: float
                - temperature: Optional[float]
                - humidity: Optional[float]
                - pressure: Optional[float]
                - velocity_v: Optional[float]
                - last_update: float (unix timestamp)
            system_info: Optional dict with:
                - uptime_seconds: float
                - cpu_percent: float
                - memory_percent: float
                
        Returns:
            PNG image as bytes
        """
        if device not in self.PROFILES:
            raise ValueError(f"Unknown device type: {device}. Must be one of {list(self.PROFILES.keys())}")
        
        width, height, font_scale = self.PROFILES[device]
        margin = int(16 * font_scale)
        
        # Create canvas
        img = Image.new('L', (width, height), color=self.WHITE)
        draw = ImageDraw.Draw(img)
        
        # Layout sections
        y = margin
        y = self._draw_header(draw, width, margin, y, font_scale)
        y = self._draw_divider(draw, width, margin, y)
        y += int(8 * font_scale)
        y = self._draw_receivers(draw, width, margin, y, font_scale, receivers)
        y += int(8 * font_scale)
        y = self._draw_divider(draw, width, margin, y)
        y += int(8 * font_scale)
        y = self._draw_sondes(draw, width, height, margin, y, font_scale, sondes)
        
        # Footer at bottom
        self._draw_footer(draw, width, height, margin, font_scale, system_info)
        
        # Convert to PNG bytes
        output = io.BytesIO()
        img.save(output, format='PNG', optimize=True)
        return output.getvalue()
    
    def generate_receiver_detail(
        self,
        device: str,
        receiver: Dict,
        sonde: Optional[Dict],
        system_info: Optional[Dict] = None,
        telemetry_history: Optional[List[Dict]] = None
    ) -> bytes:
        """Generate detailed dashboard for a single receiver.
        
        Args:
            device: Device type ('touch' or 'paperwhite')
            receiver: Receiver status dict with keys:
                - device_id: str
                - state: str (e.g. "SCANNING", "DECODING")
                - frequency: Optional[float] in Hz
                - freq_label: str
                - sonde_type: Optional[str]
                - spectrum: Optional[Dict] with freqs_mhz, power_db
            sonde: Optional sonde telemetry dict (if decoding)
            system_info: Optional system information
            telemetry_history: Optional list of historical telemetry data points
                
        Returns:
            PNG image as bytes
        """
        if device not in self.PROFILES:
            raise ValueError(f"Unknown device type: {device}. Must be one of {list(self.PROFILES.keys())}")
        
        width, height, font_scale = self.PROFILES[device]
        margin = int(16 * font_scale)
        
        # Create canvas
        img = Image.new('L', (width, height), color=self.WHITE)
        draw = ImageDraw.Draw(img)
        
        # Layout sections
        y = margin
        y = self._draw_header(draw, width, margin, y, font_scale)
        y = self._draw_divider(draw, width, margin, y)
        y += int(8 * font_scale)
        
        # Receiver details
        y = self._draw_receiver_detail(draw, width, height, margin, y, font_scale, receiver, sonde, telemetry_history)
        
        # Footer at bottom
        self._draw_footer(draw, width, height, margin, font_scale, system_info)
        
        # Convert to PNG bytes
        output = io.BytesIO()
        img.save(output, format='PNG', optimize=True)
        return output.getvalue()
    
    def _draw_header(self, draw: ImageDraw, width: int, margin: int, y: int, scale: float) -> int:
        """Draw header with station name and timestamp."""
        # Dark background bar with minimal padding
        bar_height = int(28 * scale)
        draw.rectangle([0, y, width, y + bar_height], fill=self.DARK_GRAY)
        
        # Station name (left)
        font_title = self._get_font(16, bold=True, scale=scale)
        draw.text((margin, y + int(6 * scale)), self.station_name, font=font_title, fill=self.WHITE)
        
        # Timestamp (right)
        font_time = self._get_font(10, scale=scale)
        timestamp = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
        time_width = draw.textlength(timestamp, font=font_time)
        draw.text((width - margin - time_width, y + int(16 * scale)), timestamp, font=font_time, fill=self.LIGHT_GRAY)
        
        return y + bar_height + int(10 * scale)
    
    def _draw_divider(self, draw: ImageDraw, width: int, margin: int, y: int, light: bool = False) -> int:
        """Draw horizontal divider line."""
        color = self.LIGHT_GRAY if light else self.MID_GRAY
        draw.line([(margin, y), (width - margin, y)], fill=color, width=1)
        return y
    
    def _draw_spectrum_bar(self, draw: ImageDraw, x: int, y: int, width: int, height: int, 
                           freqs: List[float], powers: List[float], scale: float):
        """Draw simplified spectrum as horizontal bar chart.
        
        Args:
            x, y: Top-left position
            width, height: Size of the bar chart area
            freqs: Frequency values in MHz
            powers: Power values in dB
            scale: Font/size scale factor
        """
        if not freqs or not powers or len(freqs) < 2:
            return
        
        # Downsample to fit width (1 bar per 2-3 pixels)
        n_bars = min(len(freqs), width // 2)
        step = max(1, len(freqs) // n_bars)
        
        # Normalize power values
        powers_sample = powers[::step][:n_bars]
        min_power = min(powers_sample)
        max_power = max(powers_sample)
        power_range = max_power - min_power if max_power > min_power else 1.0
        
        # Draw bars
        bar_width = max(1, width // n_bars)
        for i, pwr in enumerate(powers_sample):
            # Normalize to 0-1
            normalized = (pwr - min_power) / power_range
            bar_height = max(1, int(normalized * height))
            
            bar_x = x + i * bar_width
            bar_y = y + height - bar_height
            
            # Draw bar (darker = stronger signal)
            color = int(255 - (normalized * 200))  # 255 (white) to 55 (dark gray)
            draw.rectangle([bar_x, bar_y, bar_x + bar_width - 1, y + height], fill=color)
    
    def _draw_receivers(self, draw: ImageDraw, width: int, margin: int, y: int, scale: float, receivers: List[Dict]) -> int:
        """Draw receiver status section with detailed info."""
        font_section = self._get_font(14, bold=True, scale=scale)
        font_normal = self._get_font(11, bold=True, scale=scale)
        font_small = self._get_font(9, scale=scale)
        font_tiny = self._get_font(8, scale=scale)
        
        # Section title
        draw.text((margin, y), "SDR Devices", font=font_section, fill=self.BLACK)
        y += int(22 * scale)
        
        if not receivers:
            draw.text((margin, y), "No receivers configured", font=font_small, fill=self.MID_GRAY)
            return y + int(20 * scale)
        
        # Draw receiver grid (2 columns)
        col_width = (width - 3 * margin) // 2
        col = 0
        x_start = margin
        y_row = y
        
        for i, rx in enumerate(receivers):
            x = x_start + col * (col_width + margin)
            state = rx.get('state', 'UNKNOWN')
            
            # Receiver box with state-based styling
            box_height = int(72 * scale)
            if state == 'DECODING':
                bg_color = self.VERY_LIGHT_GRAY
                border_color = self.DARK_GRAY
            elif state == 'SCANNING':
                bg_color = self.WHITE
                border_color = self.MID_GRAY
            else:
                bg_color = self.WHITE
                border_color = self.LIGHT_GRAY
            
            draw.rectangle([x, y_row, x + col_width, y_row + box_height], 
                          fill=bg_color, outline=border_color, width=2)
            
            # Device ID with icon
            device_id = rx.get('device_id', f"Device {i}")
            icon = "*" if state == 'DECODING' else "o"
            draw.text((x + int(6 * scale), y_row + int(4 * scale)), 
                     f"{icon} {device_id}", font=font_normal, fill=self.BLACK)
            
            # State indicator
            state_y = y_row + int(22 * scale)
            state_icons = {
                'DECODING': '>',
                'SCANNING': '~',
                'IDLE': '||'
            }
            state_icon = state_icons.get(state, '?')
            draw.text((x + int(6 * scale), state_y), 
                     f"{state_icon} {state}", font=font_small, 
                     fill=self.DARK_GRAY if state == 'DECODING' else self.MID_GRAY)
            
            # Frequency info
            freq_y = state_y + int(14 * scale)
            freq_label = rx.get('freq_label', '')
            if freq_label:
                draw.text((x + int(6 * scale), freq_y), 
                         f"F: {freq_label}", font=font_tiny, fill=self.BLACK)
            
            # Sonde serial and type (if decoding)
            if state == 'DECODING':
                sonde_y = freq_y + int(12 * scale)
                sonde_serial = rx.get('sonde_serial', '')
                sonde_type = rx.get('sonde_type', '')
                if sonde_serial:
                    draw.text((x + int(6 * scale), sonde_y), 
                             f"ID: {sonde_serial}", font=font_tiny, fill=self.DARK_GRAY)
                elif sonde_type:
                    draw.text((x + int(6 * scale), sonde_y), 
                             f"Type: {sonde_type}", font=font_tiny, fill=self.DARK_GRAY)
            
            # Spectrum minibar (if scanning)
            elif state == 'SCANNING' and rx.get('spectrum'):
                spec = rx['spectrum']
                freqs = spec.get('freqs_mhz', [])
                powers = spec.get('power_db', [])
                if freqs and powers and len(freqs) == len(powers):
                    self._draw_spectrum_bar(draw, x + int(6 * scale), freq_y + int(12 * scale), 
                                           col_width - int(12 * scale), int(16 * scale), 
                                           freqs, powers, scale)
            
            # Move to next position
            col += 1
            if col >= 2:
                col = 0
                y_row += box_height + int(6 * scale)
        
        # Return y position after last row
        return y_row if col == 0 else y_row + box_height + int(6 * scale)
    
    def _draw_sondes(self, draw: ImageDraw, width: int, height: int, margin: int, y: int, scale: float, sondes: List[Dict]) -> int:
        """Draw active sondes section."""
        font_section = self._get_font(14, bold=True, scale=scale)
        font_normal = self._get_font(12, scale=scale)
        font_small = self._get_font(10, scale=scale)
        font_tiny = self._get_font(9, scale=scale)
        
        # Section title
        draw.text((margin, y), f"Active Sondes ({len(sondes)})", font=font_section, fill=self.BLACK)
        y += int(22 * scale)
        
        if not sondes:
            draw.text((margin, y), "No active radiosondes", font=font_small, fill=self.MID_GRAY)
            return y + int(20 * scale)
        
        # Reserve space for footer
        footer_height = int(30 * scale)
        max_y = height - footer_height - margin
        
        # Draw each sonde card
        for i, sonde in enumerate(sondes):
            if y > max_y - int(100 * scale):  # Not enough space
                remaining = len(sondes) - i
                if remaining > 0:
                    draw.text((margin, y), f"+ {remaining} more sonde{'s' if remaining > 1 else ''}...", 
                             font=font_small, fill=self.MID_GRAY)
                break
            
            if i > 0:
                y = self._draw_divider(draw, width, margin, y, light=True)
                y += int(6 * scale)
            
            # Sonde header: serial, type, frequency
            serial = sonde.get('serial', '?')
            sonde_type = sonde.get('type', '?')
            freq = sonde.get('frequency', 0)
            freq_str = f"{freq/1e6:.3f} MHz" if freq else "?"
            lat = sonde.get('latitude', 0)
            lon = sonde.get('longitude', 0)
            lat_dir = 'N' if lat >= 0 else 'S'
            lon_dir = 'E' if lon >= 0 else 'W'
            
            # Format: Y0942931 • RS41 • 403.001 MHz with right-aligned coordinates
            sonde_left = f"{serial} • {sonde_type} • {freq_str}"
            sonde_coords = f"{abs(lat):.4f}°{lat_dir} {abs(lon):.4f}°{lon_dir}"
            draw.text((margin, y), sonde_left, font=font_normal, fill=self.BLACK)
            coords_width = draw.textlength(sonde_coords, font=font_normal)
            draw.text((width - margin - coords_width, y), sonde_coords, font=font_normal, fill=self.DARK_GRAY)             
            y += int(16 * scale)
            
            # Row 2: Altitude, Heading, Velocity horizontal, Velocity vertical
            alt = sonde.get('altitude', 0)
            heading = sonde.get('heading', 0)
            vel_h = sonde.get('velocity_h', 0)
            vel_v = sonde.get('velocity_v', 0)
            col_width = width // 4
            line_h = int(13 * scale)
            
            alt_text = f"Alt: {int(alt):,} m"
            heading_text = f"Dir: {heading:.0f}°"
            vel_h_text = f"Vh: {vel_h:.1f} m/s"
            vel_v_text = f"Vv: {vel_v:+.1f} m/s"
            draw.text((margin, y), alt_text, font=font_small, fill=self.BLACK)
            draw.text((margin + col_width, y), heading_text, font=font_small, fill=self.BLACK)
            draw.text((margin + col_width * 2, y), vel_h_text, font=font_small, fill=self.BLACK)
            draw.text((margin + col_width * 3, y), vel_v_text, font=font_small, fill=self.BLACK)
            y += line_h
            
            # Row 3: Battery, Satellites, SNR, RSSI
            battery = sonde.get('battery', 0)
            sats = sonde.get('sats', 0)
            snr = sonde.get('snr', 0)
            rssi = sonde.get('rssi', 0)
            
            battery_text = f"Bat: {battery:.1f}V" if battery is not None and battery > 0 else "Bat: --"
            sats_text = f"Sat: {sats}"
            snr_text = f"SNR: {snr:.1f}" if snr is not None and snr != 0 else "SNR: --"
            rssi_text = f"RSSI: {rssi:.1f}" if rssi is not None and rssi != 0 else "RSSI: --"
            
            draw.text((margin, y), battery_text, font=font_small, fill=self.BLACK)
            draw.text((margin + col_width, y), sats_text, font=font_small, fill=self.BLACK)
            draw.text((margin + col_width * 2, y), snr_text, font=font_small, fill=self.BLACK)
            draw.text((margin + col_width * 3, y), rssi_text, font=font_small, fill=self.BLACK)
            y += line_h
            
            # Last update time (right-aligned on same line as header)
            last_update = sonde.get('last_update', time.time())
            age_s = int(time.time() - last_update)
            age_str = f"{age_s}s" if age_s < 60 else f"{age_s//60}m"
            age_width = draw.textlength(age_str, font=font_tiny)
            draw.text((width - margin - age_width, y - int(14 * scale)), age_str, font=font_tiny, fill=self.LIGHT_GRAY)            
            
            # Row 4+: Temperature / Humidity / Pressure (keep as before)
            temp = sonde.get('temperature')
            hum = sonde.get('humidity')
            pres = sonde.get('pressure')
            if temp is not None and temp > -99:
                temp_text = f"T: {temp:+.1f}°C"
                draw.text((margin, y), temp_text, font=font_small, fill=self.BLACK)
            if hum is not None and hum >= 0:
                hum_text = f"H: {hum:.0f}%"
                draw.text((margin + col_width, y), hum_text, font=font_small, fill=self.BLACK)
            y += line_h
            
            # Row 5: Pressure
            if pres is not None and pres >= 0:
                pres_text = f"P: {pres:.0f} hPa"
                draw.text((margin, y), pres_text, font=font_small, fill=self.BLACK)
            y += line_h
        
        return y
    
    def _draw_footer(self, draw: ImageDraw, width: int, height: int, margin: int, scale: float, system_info: Optional[Dict]):
        """Draw footer with system info."""
        font_footer = self._get_font(9, scale=scale)
        y = height - int(20 * scale)
        
        # Divider
        self._draw_divider(draw, width, margin, y - int(8 * scale))
        
        # System info (left)
        if system_info:
            uptime_h = int(system_info.get('uptime_seconds', 0) / 3600)
            cpu = system_info.get('cpu_percent', 0)
            mem = system_info.get('memory_percent', 0)
            info = f"Up: {uptime_h}h  •  CPU: {cpu:.0f}%  •  Mem: {mem:.0f}%"
            draw.text((margin, y), info, font=font_footer, fill=self.MID_GRAY)
        
        # Version info (right)
        version_text = f"OpenWXSDR v{self.version}"
        version_width = draw.textlength(version_text, font=font_footer)
        draw.text((width - margin - version_width, y), version_text, font=font_footer, fill=self.MID_GRAY)
    
    def _draw_receiver_detail(self, draw: ImageDraw, width: int, height: int, margin: int, 
                              y: int, scale: float, receiver: Dict, sonde: Optional[Dict],
                              telemetry_history: Optional[List[Dict]] = None) -> int:
        """Draw detailed view of a single receiver."""
        font_title = self._get_font(16, bold=True, scale=scale)
        font_normal = self._get_font(11, bold=True, scale=scale)
        font_small = self._get_font(10, scale=scale)
        font_tiny = self._get_font(9, scale=scale)
        
        # Receiver title with name
        device_id = receiver.get('device_id', 'Unknown')
        state = receiver.get('state', 'UNKNOWN')
        draw.text((margin, y), f"SDR-Receiver: {device_id}", font=font_title, fill=self.BLACK)
        y += int(24 * scale)
        
        # State and frequency info
        state_icons = {
            'DECODING': '>',
            'SCANNING': '~',
            'IDLE': '||'
        }
        state_icon = state_icons.get(state, '?')
        state_text = f"{state_icon} Status: {state}"
        draw.text((margin, y), state_text, font=font_normal, fill=self.DARK_GRAY if state == 'DECODING' else self.MID_GRAY)
        y += int(18 * scale)
        
        # Frequency label
        freq_label = receiver.get('freq_label', '')
        if freq_label:
            draw.text((margin, y), f"F: {freq_label}", font=font_small, fill=self.BLACK)
            y += int(14 * scale)
        
        # Sonde type if decoding
        if state == 'DECODING' and receiver.get('sonde_type'):
            sonde_type = receiver.get('sonde_type', '')
            draw.text((margin, y), f"Type: {sonde_type}", font=font_small, fill=self.DARK_GRAY)
            y += int(14 * scale)
        
        y += int(8 * scale)
        y = self._draw_divider(draw, width, margin, y, light=True)
        y += int(12 * scale)
        
        # Large spectrum visualization
        spectrum = receiver.get('spectrum')
        if spectrum and spectrum.get('freqs_mhz') and spectrum.get('power_db'):
            draw.text((margin, y), "Spectrum:", font=font_normal, fill=self.BLACK)
            y += int(18 * scale)
            
            # Calculate spectrum area (use most of the available space)
            footer_space = int(30 * scale)
            sonde_space = int(120 * scale) if sonde else 0
            max_y = height - footer_space - margin
            spectrum_height = min(int(180 * scale), max_y - y - sonde_space - int(20 * scale))
            spectrum_width = width - 2 * margin
            
            if spectrum_height > int(40 * scale):
                self._draw_large_spectrum(draw, margin, y, spectrum_width, spectrum_height,
                                         spectrum['freqs_mhz'], spectrum['power_db'], scale)
                y += spectrum_height + int(12 * scale)
        
        # Sonde details if decoding
        if sonde:
            y = self._draw_divider(draw, width, margin, y, light=True)
            y += int(12 * scale)
            
            draw.text((margin, y), "Sonde Details:", font=font_normal, fill=self.BLACK)
            y += int(18 * scale)
            
            # Row 1: Serial, type, frequency, and position all in one line
            serial = sonde.get('serial', '?')
            sonde_type = sonde.get('type', '?')
            freq = sonde.get('frequency', 0)
            freq_str = f"{freq/1e6:.3f} MHz" if freq else "?"
            lat = sonde.get('latitude', 0)
            lon = sonde.get('longitude', 0)
            lat_dir = 'N' if lat >= 0 else 'S'
            lon_dir = 'E' if lon >= 0 else 'W'
            
            # Format: Y0942931 • RS41 • 403.001 MHz with right-aligned coordinates
            sonde_left = f"{serial} • {sonde_type} • {freq_str}"
            sonde_coords = f"{abs(lat):.4f}°{lat_dir} {abs(lon):.4f}°{lon_dir}"
            draw.text((margin, y), sonde_left, font=font_small, fill=self.BLACK)
            coords_width = draw.textlength(sonde_coords, font=font_small) + int(2 * scale)
            draw.text((width - margin - coords_width, y), sonde_coords, font=font_small, fill=self.DARK_GRAY)             
            y += int(16 * scale)
            
            # Row 2: Current telemetry values (compact display)
            alt = sonde.get('altitude', 0)
            vel_h = sonde.get('velocity_h', 0)
            vel_v = sonde.get('velocity_v', 0)
            heading = sonde.get('heading', 0)
            battery = sonde.get('battery', 0)
            sats = sonde.get('sats', 0)
            rssi = sonde.get('rssi', None)
            snr = sonde.get('snr', None)
            
            # Format telemetry values - 2 rows of 4 values each
            # Row 2a: Alt, Dir, Vh, V↕
            col_width = (width - 2 * margin) // 4
            alt_str = f"Alt: {int(alt):,} m" if alt else "Alt: --"
            dir_str = f"Dir: {int(heading)}°" if heading else "Dir: --"
            vh_str = f"Vh: {vel_h:.1f} m/s"
            vv_str = f"V↕: {vel_v:+.1f} m/s"
            
            draw.text((margin, y), alt_str, font=font_tiny, fill=self.BLACK)
            draw.text((margin + col_width, y), dir_str, font=font_tiny, fill=self.BLACK)
            draw.text((margin + 2 * col_width, y), vh_str, font=font_tiny, fill=self.BLACK)
            draw.text((margin + 3 * col_width, y), vv_str, font=font_tiny, fill=self.BLACK)
            y += int(14 * scale)
            
            # Row 2b: Bat, Sat, SNR, RSSI
            bat_str = f"Bat: {battery:.1f}V" if battery and battery > 0 else "Bat: --"
            sat_str = f"Sat: {sats}" if sats else "Sat: --"
            snr_str = f"SNR: {snr:.1f}" if snr is not None else "SNR: --"
            rssi_str = f"RSSI: {rssi:.1f}" if rssi is not None else "RSSI: --"
            
            draw.text((margin, y), bat_str, font=font_tiny, fill=self.BLACK)
            draw.text((margin + col_width, y), sat_str, font=font_tiny, fill=self.BLACK)
            draw.text((margin + 2 * col_width, y), snr_str, font=font_tiny, fill=self.BLACK)
            draw.text((margin + 3 * col_width, y), rssi_str, font=font_tiny, fill=self.BLACK)
            y += int(16 * scale)
            
            # Check if we have telemetry history to draw charts
            has_history = telemetry_history and len(telemetry_history) > 5
            
            if has_history:
                # Extract all telemetry data from history
                altitudes = []
                velocities_h = []
                velocities_v = []
                satellite_counts = []
                battery_voltages = []
                rssi_values = []
                
                for point in telemetry_history:
                    if point.get('alt') is not None:
                        altitudes.append(point['alt'])
                    if point.get('vel_h') is not None:
                        velocities_h.append(point['vel_h'])
                    if point.get('vel_v') is not None:
                        velocities_v.append(point['vel_v'])
                    if point.get('sats') is not None:
                        satellite_counts.append(point['sats'])
                    if point.get('batt') is not None and point.get('batt') > 0:
                        battery_voltages.append(point['batt'])
                    if point.get('rssi') is not None:
                        rssi_values.append(point['rssi'])
                
                # Chart dimensions
                chart_height = int(70 * scale)
                full_chart_width = width - 2 * margin
                chart_spacing = int(15 * scale)
                half_chart_width = (width - 2 * margin - chart_spacing) // 2
                
                # 1. Altitude chart - full width with scale left
                if altitudes and len(altitudes) >= 2:
                    draw.text((margin, y), "Altitude", font=font_normal, fill=self.BLACK)
                    y += int(16 * scale)
                    self._draw_line_chart(draw, margin, y, full_chart_width, chart_height,
                                         altitudes, "m", scale, font_tiny, fill_color=self.LIGHT_GRAY)
                    y += chart_height + int(10 * scale)
                
                # 2. Horizontal velocity chart - full width with scale left
                if velocities_h and len(velocities_h) >= 2:
                    draw.text((margin, y), "Horizontal Velocity", font=font_normal, fill=self.BLACK)
                    y += int(16 * scale)
                    self._draw_line_chart(draw, margin, y, full_chart_width, chart_height,
                                         velocities_h, "m/s", scale, font_tiny, fill_color=self.LIGHT_GRAY)
                    y += chart_height + int(10 * scale)
                
                # 3. Satellites (left) and Battery (right) - side by side as graphs
                row_y = y
                
                # Chart 3a: Satellites (left)
                if satellite_counts and len(satellite_counts) >= 2:
                    chart_x = margin
                    chart_y = row_y
                    draw.text((chart_x, chart_y), "Satellites", font=font_normal, fill=self.BLACK)
                    chart_y += int(16 * scale)
                    self._draw_line_chart(draw, chart_x, chart_y, half_chart_width, chart_height,
                                         satellite_counts, "", scale, font_tiny, fill_color=self.LIGHT_GRAY, integer_y=True)
                
                # Chart 3b: Battery (right)
                if battery_voltages and len(battery_voltages) >= 2:
                    chart_x = margin + half_chart_width + chart_spacing
                    chart_y = row_y
                    draw.text((chart_x, chart_y), "Battery", font=font_normal, fill=self.BLACK)
                    chart_y += int(16 * scale)
                    self._draw_line_chart(draw, chart_x, chart_y, half_chart_width, chart_height,
                                         battery_voltages, "V", scale, font_tiny, fill_color=self.LIGHT_GRAY, 
                                         align_right=True, show_last_value=True)
                
                y = row_y + int(16 * scale) + chart_height + int(10 * scale)
                
                # 4. RSSI (left) and Vertical velocity (right) - side by side as graphs
                row_y = y
                
                # Chart 4a: RSSI (left)
                if rssi_values and len(rssi_values) >= 2:
                    chart_x = margin
                    chart_y = row_y
                    draw.text((chart_x, chart_y), "RSSI", font=font_normal, fill=self.BLACK)
                    chart_y += int(16 * scale)
                    self._draw_line_chart(draw, chart_x, chart_y, half_chart_width, chart_height,
                                         rssi_values, "dB", scale, font_tiny, fill_color=self.LIGHT_GRAY)
                
                # Chart 4b: Vertical velocity (right) - ALWAYS DRAW
                # Use history if available, otherwise use current value
                vel_v_data = velocities_v if velocities_v and len(velocities_v) > 0 else None
                if not vel_v_data and vel_v is not None:
                    # No history, use current value twice to make a flat line
                    vel_v_data = [vel_v, vel_v]
                
                if vel_v_data and len(vel_v_data) >= 1:
                    chart_x = margin + half_chart_width + chart_spacing
                    chart_y = row_y
                    draw.text((chart_x, chart_y), "Vertical Velocity", font=font_normal, fill=self.BLACK)
                    chart_y += int(16 * scale)
                    # Ensure we have at least 2 points for line chart
                    if len(vel_v_data) < 2:
                        vel_v_data = [vel_v_data[0], vel_v_data[0]]  # Duplicate for flat line
                    self._draw_line_chart(draw, chart_x, chart_y, half_chart_width, chart_height,
                                         vel_v_data, "m/s", scale, font_tiny, fill_color=self.LIGHT_GRAY, align_right=True)
                
                y = row_y + int(16 * scale) + chart_height + int(10 * scale)
                
                # Add frame number and timestamp if available from history
                if telemetry_history and len(telemetry_history) > 0:
                    last_frame = telemetry_history[-1]
                    frame_num = sonde.get('frame', 0)
                    timestamp = last_frame.get('timestamp', '')
                    if frame_num or timestamp:
                        frame_text = f"Last Frame: {frame_num}" if frame_num else "Last Frame: N/A"
                        if timestamp:
                            frame_text += f" - {timestamp}"
                        draw.text((margin, y), frame_text, font=font_tiny, fill=self.LIGHT_GRAY)
                        y += int(14 * scale)
            else:
                # No history - graphs cannot be drawn
                y += int(8 * scale)
            
            # Age
            last_update = sonde.get('last_update', time.time())
            age_s = int(time.time() - last_update)
            age_str = f"{age_s}s" if age_s < 60 else f"{age_s//60}m"
            draw.text((margin, y), f"Last Update: {age_str}", font=font_tiny, fill=self.LIGHT_GRAY)
            y += int(14 * scale)
        
        return y
    
    def _draw_large_spectrum(self, draw: ImageDraw, x: int, y: int, width: int, height: int,
                            freqs: List[float], powers: List[float], scale: float):
        """Draw large spectrum visualization with labels.
        
        Args:
            x, y: Top-left position
            width, height: Size of the spectrum area
            freqs: Frequency values in MHz
            powers: Power values in dB
            scale: Font/size scale factor
        """
        if not freqs or not powers or len(freqs) < 2:
            return
        
        font_label = self._get_font(8, scale=scale)
        
        # Reserve space for labels
        label_h = int(12 * scale)
        y_axis_width = int(35 * scale)  # Space for Y-axis labels (dB)
        chart_height = height - label_h
        chart_width = width - y_axis_width
        chart_x = x + y_axis_width
        
        # Draw border
        draw.rectangle([chart_x, y, chart_x + chart_width, y + chart_height], outline=self.MID_GRAY, width=1)
        
        # Downsample to fit width (1 bar per pixel)
        n_bars = min(len(freqs), chart_width - 2)
        step = max(1, len(freqs) // n_bars)
        
        freqs_sample = freqs[::step][:n_bars]
        powers_sample = powers[::step][:n_bars]
        
        # Normalize power values
        min_power = min(powers_sample)
        max_power = max(powers_sample)
        power_range = max_power - min_power if max_power > min_power else 1.0
        
        # Draw Y-axis labels (dB scale)
        # Max power (top)
        max_text = f"{int(max_power)}"
        max_width = draw.textlength(max_text, font=font_label)
        draw.text((chart_x - max_width - int(3 * scale), y), max_text, font=font_label, fill=self.MID_GRAY)
        
        # Mid power (middle)
        mid_power = (min_power + max_power) / 2
        mid_text = f"{int(mid_power)}"
        mid_width = draw.textlength(mid_text, font=font_label)
        draw.text((chart_x - mid_width - int(3 * scale), y + chart_height // 2 - int(4 * scale)), mid_text, font=font_label, fill=self.MID_GRAY)
        
        # Min power (bottom)
        min_text = f"{int(min_power)} dB"
        min_width = draw.textlength(min_text, font=font_label)
        draw.text((chart_x - min_width - int(3 * scale), y + chart_height - int(10 * scale)), min_text, font=font_label, fill=self.MID_GRAY)
        
        # Draw bars
        for i in range(len(powers_sample)):
            pwr = powers_sample[i]
            normalized = (pwr - min_power) / power_range
            bar_height = max(1, int(normalized * (chart_height - 4)))
            
            bar_x = chart_x + 2 + i
            bar_y = y + chart_height - 2 - bar_height
            
            # Draw bar (darker = stronger signal)
            color = int(255 - (normalized * 200))  # 255 (white) to 55 (dark gray)
            draw.line([(bar_x, bar_y), (bar_x, y + chart_height - 2)], fill=color, width=1)
        
        # Frequency labels (X-axis) - 5 markers
        label_y = y + chart_height + int(2 * scale)
        freq_min = freqs_sample[0]
        freq_max = freqs_sample[-1]
        freq_range = freq_max - freq_min
        
        # Left label
        draw.text((chart_x, label_y), f"{freq_min:.1f}", font=font_label, fill=self.MID_GRAY)
        
        # Left-mid label (1/4)
        freq_q1 = freq_min + freq_range * 0.25
        q1_text = f"{freq_q1:.1f}"
        q1_width = draw.textlength(q1_text, font=font_label)
        draw.text((chart_x + chart_width//4 - q1_width//2, label_y), q1_text, font=font_label, fill=self.MID_GRAY)
        
        # Center label (1/2)
        freq_mid = freq_min + freq_range * 0.5
        mid_text = f"{freq_mid:.1f}"
        mid_width = draw.textlength(mid_text, font=font_label)
        draw.text((chart_x + chart_width//2 - mid_width//2, label_y), mid_text, font=font_label, fill=self.MID_GRAY)
        
        # Right-mid label (3/4)
        freq_q3 = freq_min + freq_range * 0.75
        q3_text = f"{freq_q3:.1f}"
        q3_width = draw.textlength(q3_text, font=font_label)
        draw.text((chart_x + chart_width*3//4 - q3_width//2, label_y), q3_text, font=font_label, fill=self.MID_GRAY)
        
        # Right label
        right_text = f"{freq_max:.1f} MHz"
        right_width = draw.textlength(right_text, font=font_label)
        draw.text((chart_x + chart_width - right_width, label_y), right_text, font=font_label, fill=self.MID_GRAY)
    
    def _draw_telemetry_charts(self, draw: ImageDraw, width: int, height: int, margin: int,
                               y: int, scale: float, telemetry_history: List[Dict]) -> int:
        """Draw telemetry charts (Altitude, RSSI, Satellites, Battery) in 2x2 grid.
        
        Args:
            draw: PIL ImageDraw object
            width: Canvas width
            height: Canvas height  
            margin: Page margin
            y: Current Y position
            scale: Font scale factor
            telemetry_history: List of historical telemetry data points
            
        Returns:
            Updated Y position
        """
        if not telemetry_history or len(telemetry_history) < 2:
            return y
        
        font_label = self._get_font(8, scale=scale)
        font_title = self._get_font(10, bold=True, scale=scale)
        
        # Calculate chart dimensions
        chart_spacing = int(12 * scale)
        chart_width = (width - 2 * margin - chart_spacing) // 2
        chart_height = int(80 * scale)
        
        # Check if we have enough space
        footer_space = int(30 * scale)
        available_height = height - footer_space - margin - y
        needed_height = chart_height * 2 + chart_spacing + int(40 * scale)
        
        if available_height < needed_height:
            # Not enough space, skip charts
            return y
        
        # Extract data series from history
        altitudes = []
        rssi_values = []
        satellite_counts = []
        battery_voltages = []
        
        for point in telemetry_history:
            alt = point.get('alt')
            if alt is not None:
                altitudes.append(alt)
            
            rssi = point.get('rssi')
            if rssi is not None:
                rssi_values.append(rssi)
            
            sats = point.get('sats')
            if sats is not None:
                satellite_counts.append(sats)
            
            batt = point.get('batt')
            if batt is not None:
                battery_voltages.append(batt)
        
        # Row 1: Altitude and RSSI
        # Chart 1: Altitude (top-left)
        if altitudes and len(altitudes) >= 2:
            chart_x = margin
            chart_y = y
            draw.text((chart_x, chart_y), "Altitude", font=font_title, fill=self.BLACK)
            chart_y += int(14 * scale)
            self._draw_line_chart(draw, chart_x, chart_y, chart_width, chart_height,
                                 altitudes, "m", scale, font_label, fill_color=self.LIGHT_GRAY)
        
        # Chart 2: RSSI (top-right)
        if rssi_values and len(rssi_values) >= 2:
            chart_x = margin + chart_width + chart_spacing
            chart_y = y
            draw.text((chart_x, chart_y), "RSSI", font=font_title, fill=self.BLACK)
            chart_y += int(14 * scale)
            self._draw_line_chart(draw, chart_x, chart_y, chart_width, chart_height,
                                 rssi_values, "dB", scale, font_label, fill_color=self.LIGHT_GRAY)
        
        y += chart_height + int(14 * scale) + chart_spacing
        
        # Row 2: Satellites and Battery
        # Chart 3: Satellites (bottom-left)
        if satellite_counts and len(satellite_counts) >= 2:
            chart_x = margin
            chart_y = y
            draw.text((chart_x, chart_y), "Satellites", font=font_title, fill=self.BLACK)
            chart_y += int(14 * scale)
            self._draw_line_chart(draw, chart_x, chart_y, chart_width, chart_height,
                                 satellite_counts, "", scale, font_label, fill_color=self.LIGHT_GRAY, integer_y=True)
        
        # Chart 4: Battery (bottom-right)
        if battery_voltages and len(battery_voltages) >= 2:
            chart_x = margin + chart_width + chart_spacing
            chart_y = y
            draw.text((chart_x, chart_y), "Battery", font=font_title, fill=self.BLACK)
            chart_y += int(14 * scale)
            self._draw_line_chart(draw, chart_x, chart_y, chart_width, chart_height,
                                 battery_voltages, "V", scale, font_label, fill_color=self.LIGHT_GRAY)
        
        y += chart_height + int(14 * scale)
        return y
    
    def _draw_line_chart(self, draw: ImageDraw, x: int, y: int, width: int, height: int,
                        values: List[float], unit: str, scale: float, font_label: ImageFont.FreeTypeFont,
                        fill_color: int = 185, integer_y: bool = False, align_right: bool = False, 
                        show_last_value: bool = False):
        """Draw a simple line chart with optional fill.
        
        Args:
            draw: PIL ImageDraw object
            x, y: Top-left position
            width, height: Chart dimensions
            values: Data values to plot
            unit: Unit string (e.g., "m", "dB", "V")
            scale: Font scale factor
            font_label: Font for labels
            fill_color: Fill color (grayscale 0-255)
            integer_y: Whether to use integer Y-axis labels
            align_right: Whether to align Y-axis labels to the right
            show_last_value: Whether to show last value instead of max value in legend
        """
        if not values or len(values) < 2:
            return
        
        # Draw border
        draw.rectangle([x, y, x + width, y + height], outline=self.MID_GRAY, width=1)
        
        # Calculate value range
        min_val = min(values)
        max_val = max(values)
        val_range = max_val - min_val if max_val > min_val else 1.0
        
        # Add 5% padding to range
        padding = val_range * 0.05
        min_val -= padding
        max_val += padding
        val_range = max_val - min_val
        
        # Draw Y-axis labels (min and max, or min and last)
        if integer_y:
            min_text = f"{int(min_val)}"
            if show_last_value:
                max_text = f"{int(values[-1])}{unit}"  # Last value
            else:
                max_text = f"{int(max_val)}{unit}"  # Max value
        else:
            min_text = f"{min_val:.1f}"
            if show_last_value:
                max_text = f"{values[-1]:.1f}{unit}"  # Last value
            else:
                max_text = f"{max_val:.1f}{unit}"  # Max value
        
        if align_right:
            # Right-aligned labels
            min_text_width = draw.textlength(min_text, font=font_label)
            max_text_width = draw.textlength(max_text, font=font_label)
            draw.text((x + width - min_text_width - 2, y + height - 10), min_text, font=font_label, fill=self.MID_GRAY)
            draw.text((x + width - max_text_width - 2, y + 2), max_text, font=font_label, fill=self.MID_GRAY)
        else:
            # Left-aligned labels (default)
            draw.text((x + 2, y + height - 10), min_text, font=font_label, fill=self.MID_GRAY)
            draw.text((x + 2, y + 2), max_text, font=font_label, fill=self.MID_GRAY)
        
        # Calculate points
        n_points = len(values)
        x_step = width / (n_points - 1) if n_points > 1 else width
        
        points = []
        for i, val in enumerate(values):
            px = x + i * x_step
            # Normalize value to chart height (invert Y axis)
            normalized = (val - min_val) / val_range
            py = y + height - (normalized * height)
            points.append((px, py))
        
        # Draw filled area (from line to bottom)
        if len(points) >= 2:
            fill_points = [(x, y + height)] + points + [(x + width, y + height)]
            draw.polygon(fill_points, fill=fill_color, outline=None)
        
        # Draw line
        if len(points) >= 2:
            draw.line(points, fill=self.BLACK, width=2)

def generate_dashboard_image(
    device_type: str,
    station_name: str,
    receivers: List[Dict],
    sondes: List[Dict],
    system_info: Optional[Dict] = None,
    version: str = __version__
) -> bytes:
    """Convenience function to generate dashboard image.
    
    Args:
        device_type: 'touch' or 'paperwhite'
        station_name: Station name for header
        receivers: List of receiver status dicts
        sondes: List of active sonde telemetry dicts
        system_info: Optional system information
        version: Software version string
        
    Returns:
        PNG image as bytes
    """
    generator = KindleDashboardGenerator(station_name=station_name, version=version)
    return generator.generate_dashboard(device_type, receivers, sondes, system_info)


def generate_receiver_detail_image(
    device_type: str,
    station_name: str,
    receiver: Dict,
    sonde: Optional[Dict],
    system_info: Optional[Dict] = None,
    version: str = __version__,
    telemetry_history: Optional[List[Dict]] = None
) -> bytes:
    """Generate detailed dashboard for a single receiver.
    
    Args:
        device_type: 'touch' or 'paperwhite'
        station_name: Station name for header
        receiver: Receiver status dict with spectrum data
        sonde: Optional sonde telemetry dict (if decoding)
        system_info: Optional system information
        version: Software version string
        telemetry_history: Optional list of historical telemetry data points
        
    Returns:
        PNG image as bytes
    """
    generator = KindleDashboardGenerator(station_name=station_name, version=version)
    return generator.generate_receiver_detail(device_type, receiver, sonde, system_info, telemetry_history)
