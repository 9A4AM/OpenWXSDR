"""
# =============================================================================
#  OpenWX -- Open Weather Radiosonde Telemetry System
# =============================================================================
#
#  File   : rtl_power_scanner.py
#  Author : M.F. Guenther, DL2MF - DL2MF@darc.de
#  License: GNU General Public License v2.0 (GPL-2.0)
#
# -----------------------------------------------------------------------------
#  Description
# -----------------------------------------------------------------------------
#
#  Full-band spectrum scanner built on the `rtl_power` CLI (rtl-sdr package).
#
#  Unlike the pyrtlsdr + Welch scan in rtlsdr_analyzer.py (which captures a
#  single ~2.4 MHz instantaneous segment and needs band-sweep to cover more),
#  rtl_power retunes internally and stitches a PSD across an arbitrarily wide
#  range in ONE invocation. So a single RTL-SDR sees the whole 402-406 MHz band
#  every scan — no coverage gaps, catching just-launched low sondes far sooner
#  (this is radiosonde_auto_rx's scan approach).
#
#  This is a SUBPROCESS wrapper (sibling to AudioPipeline's rtl_fm usage), NOT a
#  pyrtlsdr device: it spawns rtl_power with a hard wall-clock timeout, so it
#  cannot wedge on an uninterruptible libusb read the way capture_spectrum() can.
#  It returns absolute (freqs_hz, power_db) arrays that the existing
#  SpectrumAnalyzer.detect_signals() consumes unchanged — identical peak
#  detection, grid filtering and DetectedSignal output regardless of scan source.
#
# =============================================================================
"""

import logging
import math
import os
import shutil
import signal
import subprocess
import tempfile
import time
from typing import Optional, Tuple

import numpy as np


class RtlPowerScanner:
    """Run `rtl_power` for one full-band pass and return (freqs_hz, power_db).

    rtl_power CSV line format (one line per tuning chunk):
        date, time, Hz_low, Hz_high, Hz_step, n_samples, dB, dB, dB, ...
    Multiple lines tile the requested range; concatenating them (sorted by
    frequency) gives the full-band PSD. Power is uncalibrated dB — fine for
    detect_signals(), which works relative to the median noise floor.
    """

    def __init__(self, device_serial, gain=0, ppm=0,
                 band_start_hz: int = 402_000_000,
                 band_stop_hz: int = 406_000_000,
                 step_hz: int = 1000,
                 integration_s: float = 5.0,
                 crop_percent: int = 25,
                 rtl_power_path: str = 'rtl_power',
                 wall_timeout_s: float = 60.0):
        self.device_serial = str(device_serial)
        self.gain = gain
        self.ppm = ppm
        self.band_start_hz = int(band_start_hz)
        self.band_stop_hz = int(band_stop_hz)
        self.step_hz = int(step_hz)
        self.integration_s = float(integration_s)
        self.crop_percent = int(crop_percent)
        self.rtl_power_path = rtl_power_path
        self.wall_timeout_s = float(wall_timeout_s)
        self.logger = logging.getLogger(f'RtlPowerScanner.{self.device_serial}')
        self._logged_missing = False

    def available(self) -> bool:
        """True if the rtl_power binary is on PATH."""
        return shutil.which(self.rtl_power_path) is not None

    def scan(self, abort_check=None) -> Optional[Tuple[np.ndarray, np.ndarray]]:
        """Run one full-band rtl_power sweep. Returns (freqs_hz, power_db) or None.

        rtl_power's `-1` (single shot) collects one sweep but on many builds does
        NOT self-exit — it hangs. We therefore let it run just long enough for one
        full sweep, then terminate it with SIGINT, which triggers rtl_power's own
        "finishing scan pass" handler → it FLUSHES the CSV and exits cleanly
        (exactly what a manual Ctrl-C does). The CSV is then parsed regardless of
        how the process ended — the data is already there. (An earlier version
        used subprocess.run(timeout), which SIGKILLs without a flush and discarded
        the CSV, throwing away a perfectly good sweep.)"""
        if shutil.which(self.rtl_power_path) is None:
            if not self._logged_missing:
                self.logger.error(
                    f"'{self.rtl_power_path}' not found — cannot run the rtl_power "
                    "scan (install the rtl-sdr package). Falling back to the Welch scan."
                )
                self._logged_missing = True
            return None

        if abort_check is not None and abort_check():
            return None

        tmp = tempfile.NamedTemporaryFile(prefix='rtlpwr_', suffix='.csv', delete=False)
        tmp_path = tmp.name
        tmp.close()

        cmd = [
            self.rtl_power_path,
            '-f', f'{self.band_start_hz}:{self.band_stop_hz}:{self.step_hz}',
            '-i', str(int(self.integration_s)),
            '-1',
            '-c', f'{self.crop_percent}%',
            '-p', str(int(self.ppm)),
            '-d', self.device_serial,
        ]
        # gain 0 == AUTO (omit -g), matching AudioPipeline's rtl_fm gain handling.
        try:
            gain_val = float(self.gain)
        except (TypeError, ValueError):
            gain_val = 0.0
        if gain_val > 0.0:
            cmd += ['-g', str(self.gain)]
        cmd += [tmp_path]

        # One full sweep ≈ integration_s per tuning hop + tuner/settle overhead,
        # capped by wall_timeout_s. rtl_power hops in ~2 MHz steps across the band.
        hops = max(1, math.ceil(max(1, self.band_stop_hz - self.band_start_hz) / 2_000_000))
        collect_s = min(self.wall_timeout_s, self.integration_s * hops + 8.0)
        self.logger.debug(
            f"rtl_power ({hops} hop(s), collect ~{collect_s:.0f}s): {' '.join(cmd)}")

        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL)
        except FileNotFoundError:
            self._cleanup(tmp_path)
            return None
        except Exception as e:  # noqa: BLE001 - never let a scan crash the worker
            self.logger.error(f"rtl_power spawn failed: {e}")
            self._cleanup(tmp_path)
            return None

        # Wait for a full sweep OR self-exit OR a manual-decode abort, then stop it.
        deadline = time.time() + collect_s
        aborted = False
        while time.time() < deadline:
            if proc.poll() is not None:
                break  # rtl_power self-exited (clean -1) — CSV flushed
            if abort_check is not None and abort_check():
                aborted = True
                break
            time.sleep(0.3)

        if proc.poll() is None:
            # SIGINT → rtl_power flushes the CSV and exits cleanly (like Ctrl-C).
            self._stop(proc)

        if aborted:
            self._cleanup(tmp_path)
            return None

        try:
            freqs, power = self._parse_csv(tmp_path)
        finally:
            self._cleanup(tmp_path)

        if freqs is None or freqs.size == 0:
            self.logger.debug("rtl_power produced no usable PSD this pass")
            return None
        return freqs, power

    @staticmethod
    def _stop(proc):
        """Stop rtl_power with SIGINT (clean flush+exit), escalating if needed."""
        try:
            proc.send_signal(signal.SIGINT)
            proc.wait(timeout=6)
            return
        except subprocess.TimeoutExpired:
            pass
        except Exception:
            return
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    @staticmethod
    def _cleanup(path: str):
        try:
            os.unlink(path)
        except OSError:
            pass

    def _parse_csv(self, path: str):
        """Parse rtl_power CSV → (freqs_hz, power_db), sorted by frequency."""
        freq_chunks = []
        power_chunks = []
        try:
            with open(path) as fh:
                for line in fh:
                    fields = line.split(',', 6)
                    if len(fields) < 7:
                        continue
                    try:
                        f_lo = float(fields[2])
                        f_hi = float(fields[3])
                        bins = np.array(
                            [x.strip() for x in fields[6].split(',') if x.strip() != ''],
                            dtype=float,
                        )
                    except (ValueError, IndexError):
                        continue
                    if bins.size == 0:
                        continue
                    freq_chunks.append(np.linspace(f_lo, f_hi, bins.size))
                    power_chunks.append(bins)
        except OSError:
            return None, None

        if not freq_chunks:
            return None, None

        freqs = np.concatenate(freq_chunks)
        power = np.nan_to_num(np.concatenate(power_chunks))
        # rtl_power emits chunks in tuning order; sort so detect_signals sees a
        # monotonic frequency axis.
        order = np.argsort(freqs)
        return freqs[order], power[order]
