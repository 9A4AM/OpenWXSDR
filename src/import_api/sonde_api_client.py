"""
Sonde Import API Client

Fetches nearby active sondes from external API and provides frequency/type information
for automatic SDR assignment.
"""

import requests
import logging
import math
import time
import threading
from typing import List, Dict, Optional, Callable
from datetime import datetime, timedelta


class SondeApiClient:
    """Client for fetching nearby sondes from external API."""
    
    def __init__(self, config: dict):
        """
        Initialize the API client.
        
        Args:
            config: import_api section from config.yaml
        """
        self.logger = logging.getLogger('SondeApiClient')
        self.config = config
        
        self.enabled = config.get('enabled', False)
        self.url = config.get('url', 'api.opnwx.de')
        self.check_interval_s = config.get('check_interval_s', 300)
        self.lat = config.get('lat', 0.0)
        self.lon = config.get('lon', 0.0)
        self.distance_km = config.get('distance_km', 50)
        self.time_range_minutes = config.get('time_range_minutes', 15)
        self.sonde_type_filter = config.get('sonde_type', 'all')
        self.max_sondes = config.get('max_sondes', 4)

        # api.v2.sondehub.org's /sondes endpoint returns the same fields as
        # api.opnwx.de EXCEPT it has no "distance" — so distance_km parses to 0
        # for every sonde, breaking the nearest-first sort and distance-based
        # receiver assignment. When talking to SondeHub we compute distance
        # ourselves (haversine from the station position to each sonde).
        self._is_sondehub = 'sondehub.org' in self.url.lower()

        self._poll_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._callback: Optional[Callable[[List[Dict]], None]] = None
        self._last_sondes: List[Dict] = []
        
    def start(self, callback: Callable[[List[Dict]], None]):
        """
        Start polling the API in background thread.
        
        Args:
            callback: Function to call with list of detected sondes
        """
        if not self.enabled:
            self.logger.info("Import API disabled in configuration")
            return
            
        if self._poll_thread and self._poll_thread.is_alive():
            self.logger.warning("Import API already running")
            return
            
        self._callback = callback
        self._stop_event.clear()
        self._poll_thread = threading.Thread(target=self._poll_loop, daemon=True, name="ImportApiPoller")
        self._poll_thread.start()
        self.logger.info(f"Import API started: {self.url}, interval={self.check_interval_s}s, "
                        f"lat={self.lat}, lon={self.lon}, distance={self.distance_km}km"
                        + (" (distance computed locally — SondeHub)" if self._is_sondehub else ""))
    
    def stop(self):
        """Stop the polling thread."""
        if self._poll_thread and self._poll_thread.is_alive():
            self.logger.info("Stopping Import API poller...")
            self._stop_event.set()
            self._poll_thread.join(timeout=5)
            self._poll_thread = None
    
    def _poll_loop(self):
        """Background polling loop."""
        while not self._stop_event.is_set():
            try:
                sondes = self.fetch_sondes()
                if sondes and self._callback:
                    self._callback(sondes)
                self._last_sondes = sondes
            except Exception as e:
                self.logger.error(f"Error fetching sondes from API: {e}")
            
            # Wait for next poll interval
            self._stop_event.wait(self.check_interval_s)
    
    def fetch_sondes(self) -> List[Dict]:
        """
        Fetch nearby sondes from the API.
        
        Returns:
            List of sonde dictionaries with keys: serial, frequency, type, distance, lat, lon, alt
        """
        if not self.enabled:
            return []
        
        try:
            # Build API URL
            # Example: https://api.opnwx.de/sondes.php?lat=52.62&lon=10.28&distance=999999&p=14400
            time_seconds = self.time_range_minutes * 60
            url = f"https://{self.url}/sondes"
            params = {
                'lat': self.lat,
                'lon': self.lon,
                'distance': self.distance_km * 1000,  # API expects meters
                'p': time_seconds,  # Time range in seconds
            }
            
            self.logger.debug(f"Fetching sondes from {url} with params {params}")
            
            # Make request with timeout
            response = requests.get(url, params=params, timeout=10)
            response.raise_for_status()
            
            # Parse JSON response
            data = response.json()
            
            # Handle different API response formats
            # Format 1: List of sonde objects (newer API format)
            # Format 2: Dict with serial keys (older API format)
            # Format 3: Empty list (no sondes)
            
            if isinstance(data, list):
                if len(data) == 0:
                    self.logger.debug("API returned empty list (no sondes found)")
                    return []
                # Non-empty list - process list format
                self.logger.debug(f"API returned list format with {len(data)} items")
                return self._parse_list_format(data)
            
            if isinstance(data, dict):
                # Dict format - process as before
                self.logger.debug(f"API returned dict format with {len(data)} items")
                return self._parse_dict_format(data)
            
            # Unknown format
            self.logger.warning(f"Unexpected API response format: {type(data)}")
            return []
        
        except requests.exceptions.Timeout:
            self.logger.error(f"Timeout fetching sondes from API")
            return []
        except requests.exceptions.RequestException as e:
            self.logger.error(f"Request error fetching sondes: {e}")
            return []
        except Exception as e:
            self.logger.error(f"Unexpected error fetching sondes: {e}")
            return []
    
    @staticmethod
    def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        """Great-circle distance in km between two lat/lon points."""
        r = 6371.0  # Earth radius (km)
        p1, p2 = math.radians(lat1), math.radians(lat2)
        dphi = math.radians(lat2 - lat1)
        dlmb = math.radians(lon2 - lon1)
        a = (math.sin(dphi / 2) ** 2
             + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2)
        return r * 2 * math.asin(min(1.0, math.sqrt(a)))

    def _resolve_distance_km(self, api_distance_km: float, sonde_lat: float,
                             sonde_lon: float) -> float:
        """Return a usable distance: the API value when present/nonzero,
        otherwise (SondeHub, or a missing/zero value) computed via haversine
        from the station position — provided the sonde has a real fix."""
        if not self._is_sondehub and api_distance_km > 0:
            return api_distance_km
        # SondeHub, or opnwx returned 0/missing: compute from coords if valid
        if sonde_lat != 0.0 or sonde_lon != 0.0:
            return self._haversine_km(self.lat, self.lon, sonde_lat, sonde_lon)
        return api_distance_km

    def _parse_dict_format(self, data: dict) -> List[Dict]:
        """Parse API response in dict format (serial as key)."""
        sondes = []
        for serial, sonde_data in data.items():
                try:
                    sonde_type = sonde_data.get('type', '').upper()
                    
                    # Apply type filter
                    if self.sonde_type_filter != 'all' and sonde_type != self.sonde_type_filter.upper():
                        continue
                    
                    # Extract frequency (prefer 'frequency' over 'tx_frequency')
                    frequency = sonde_data.get('frequency')
                    if frequency is None:
                        frequency = sonde_data.get('tx_frequency')
                    if frequency is None:
                        self.logger.debug(f"Skipping sonde {serial}: no frequency")
                        continue
                    
                    # Convert frequency to Hz if it's in MHz
                    frequency = float(frequency)
                    if frequency < 1000:  # Assume MHz
                        frequency = frequency * 1e6
                    
                    # Parse distance (format: "166.037 km"); may be a bare
                    # number or absent (SondeHub omits it entirely)
                    distance_val = sonde_data.get('distance', 0)
                    if isinstance(distance_val, str):
                        distance_km = float(distance_val.split()[0]) if distance_val.strip() else 0.0
                    else:
                        distance_km = float(distance_val or 0)

                    sonde_lat = float(sonde_data.get('lat', 0))
                    sonde_lon = float(sonde_data.get('lon', 0))
                    distance_km = self._resolve_distance_km(distance_km, sonde_lat, sonde_lon)

                    sonde_info = {
                        'serial': serial,
                        'frequency': frequency,
                        'type': sonde_type,
                        'distance_km': distance_km,
                        'lat': sonde_lat,
                        'lon': sonde_lon,
                        'alt': float(sonde_data.get('alt', 0)),
                        'frame': int(sonde_data.get('frame', 0)),
                        'datetime': sonde_data.get('datetime', ''),
                        'uploader': sonde_data.get('uploader_callsign', ''),
                        'launchsite': sonde_data.get('launchsite', ''),
                    }
                    
                    sondes.append(sonde_info)
                    
                except (ValueError, KeyError, TypeError) as e:
                    self.logger.debug(f"Error parsing sonde {serial}: {e}")
                    continue
            
        # Sort by distance (nearest first)
        sondes.sort(key=lambda s: s['distance_km'])
        
        # Limit to max_sondes
        sondes = sondes[:self.max_sondes]
        
        if sondes:
            self.logger.info(f"Fetched {len(sondes)} sondes from API (filter={self.sonde_type_filter})")
            for s in sondes:
                self.logger.debug(f"  {s['serial']} ({s['type']}) @ {s['frequency']/1e6:.3f} MHz, "
                                 f"distance={s['distance_km']:.1f}km")
        
        return sondes
    
    def _parse_list_format(self, data: list) -> List[Dict]:
        """Parse API response in list format."""
        sondes = []
        for item in data:
            try:
                if not isinstance(item, dict):
                    continue
                
                serial = item.get('serial', item.get('sonde_id', ''))
                if not serial:
                    self.logger.debug("Skipping item: no serial")
                    continue
                
                sonde_type = item.get('type', '').upper()
                
                # Apply type filter
                if self.sonde_type_filter != 'all' and sonde_type != self.sonde_type_filter.upper():
                    continue
                
                # Extract frequency
                frequency = item.get('frequency') or item.get('tx_frequency')
                if frequency is None:
                    self.logger.debug(f"Skipping sonde {serial}: no frequency")
                    continue
                
                # Convert frequency to Hz if it's in MHz
                frequency = float(frequency)
                if frequency < 1000:  # Assume MHz
                    frequency = frequency * 1e6
                
                # Parse distance (can be float, string "166.037 km", or absent
                # — SondeHub omits it entirely)
                distance_val = item.get('distance', 0)
                if isinstance(distance_val, str):
                    distance_km = float(distance_val.split()[0]) if distance_val.strip() else 0.0
                else:
                    distance_km = float(distance_val or 0)

                sonde_lat = float(item.get('lat', 0))
                sonde_lon = float(item.get('lon', 0))
                distance_km = self._resolve_distance_km(distance_km, sonde_lat, sonde_lon)

                sonde_info = {
                    'serial': serial,
                    'frequency': frequency,
                    'type': sonde_type,
                    'distance_km': distance_km,
                    'lat': sonde_lat,
                    'lon': sonde_lon,
                    'alt': float(item.get('alt', 0)),
                    'frame': int(item.get('frame', 0)),
                    'datetime': item.get('datetime', ''),
                    'uploader': item.get('uploader_callsign', ''),
                    'launchsite': item.get('launchsite', ''),
                }
                
                sondes.append(sonde_info)
                
            except (ValueError, KeyError, TypeError) as e:
                self.logger.debug(f"Error parsing sonde from list: {e}")
                continue
        
        # Sort by distance (nearest first)
        sondes.sort(key=lambda s: s['distance_km'])
        
        # Limit to max_sondes
        sondes = sondes[:self.max_sondes]
        
        if sondes:
            self.logger.info(f"Fetched {len(sondes)} sondes from API list format (filtered by type={self.sonde_type_filter})")
            for s in sondes:
                self.logger.debug(f"  {s['serial']} ({s['type']}) @ {s['frequency']/1e6:.3f} MHz, "
                                 f"distance={s['distance_km']:.1f}km")
        
        return sondes
    
    def get_last_sondes(self) -> List[Dict]:
        """Get the last fetched list of sondes."""
        return self._last_sondes.copy()
