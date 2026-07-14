# OpenWXSDR v1.0.52 — Detailed Changelog (since v1.0.50)

This release focuses on **reliability of the RTL-SDR decode pipeline**, **detection
accuracy**, **SondeHub upload correctness**, and a handful of **web UI improvements**.
None of the changes require a `config.yaml` migration — all new settings have safe
defaults and are optional.

---

## 🐛 Crash & Race-Condition Fixes

- **Fixed a race condition in manual/priority-frequency decoding** that let the
  auto-scan cycle and a manual decode request fight over the same physical RTL-SDR
  device at the same time. Symptoms: `rtl_fm exit: 1`, `dft_detect exit code 206
  (corrupted input data)`, decoders failing to start for no obvious reason.
- **Fixed the Import API "double-booking" a device** that was already mid-way into
  starting a manual/priority decode. The imported sonde's decode could win the race
  and silently evict the one you actually wanted running.
- **Fixed a registry self-collision bug** where a freshly detected signal's own
  frequency-correction step could make the system think the frequency was "already
  being decoded" — by itself. This caused many legitimate auto-detected decodes
  (especially anything that got even a 1 kHz DFT correction) to abort immediately
  with `already being decoded — aborting`, right after being found.
- **Fixed/removed a SIGSEGV-inducing recovery attempt.** An earlier fix that tried to
  close a hung USB handle from a background thread could crash the whole service
  (`status=11/SEGV`) if the original read was still blocked in the C extension.
  Reverted to a safe leak-and-retry approach.

## ⚙️ Self-Healing & Device Status

- **Automatic service self-restart** after a device fails to open **10 times in a
  row** (`LIBUSB_ERROR_BUSY` etc.). Since this failure mode never recovers within a
  running process, the service now exits cleanly and lets `systemd`
  (`Restart=on-failure`, `RestartSec=10`) bring everything back up and release the
  stuck USB interface — instead of polling a dead device forever.
- **New `Error` device state.** Previously a device that failed to open kept
  reporting `Scanning` (green) in the web UI forever — indistinguishable from a
  healthy receiver. The SDR Devices table now shows a red **Error** status the
  moment a device fails to open, so you can see hardware problems at a glance
  instead of digging through logs.
- **Manual/imported "decode until lost" decoders no longer hang forever.** A
  decoder started via the Import API or manual entry with no fixed duration had *no*
  staleness check at all — if the underlying process kept running without ever
  producing another frame (sonde landed, out of range, etc.), the receiver stayed
  stuck in `Decoding` indefinitely. It now falls back to scanning after
  **30 minutes** without a valid frame (configurable, see below).
- **Fixed a follow-on bug uncovered by testing the above**: once a decoder timed
  out and stopped, the device sometimes failed to reopen for scanning with
  `usb_claim_interface error -6` / `LIBUSB_ERROR_BUSY`, even on single-device
  systems with no other worker competing for the USB bus. Root cause:
  `AudioPipeline.stop()` sent `SIGKILL` to a stuck `rtl_fm` process but never
  waited to confirm it had actually been reaped, so cleanup could report
  "stopped" before the kernel had released rtl_fm's USB interface claim. Fixed
  by always confirming process death (`wait()`) after both `SIGTERM` and
  `SIGKILL`, with a clear log message if `rtl_fm` still won't die.

## 🎯 Detection Accuracy

- **`dft_detect` now self-adapts to whichever CLI convention is actually
  installed.** `install.sh` clones `rs1729/RS` unpinned, so the exact build (and its
  expected argument order) varies per install. The detector now tries both known
  conventions on first use and caches whichever one actually works — this was very
  likely the root cause of `dft_detect` returning "corrupted input data" on nearly
  every call in the field.
- **Fixed DFM misclassification in the bandwidth fallback.** The old "ambiguous
  zone" (6.5–10 kHz) always defaulted to RS41, which made DFM (typically
  7.5–8.5 kHz) almost unreachable whenever DFT correlation didn't return a
  confident match. The zone is now split so each type's typical range is favored.
- **Priority-frequency devices are now dynamically blacklisted on all other
  receivers** while active, so other RTL-SDRs whose scan range overlaps the
  priority frequency stop wasting USB/CPU cycles repeatedly re-detecting and
  aborting on it.

## 📡 SondeHub Upload Correctness

Following feedback from a SondeHub maintainer, both upload paths
(`sondehub_queue.py` and `sondehub_output.py`) were brought in line with
`radiosonde_auto_rx`'s own field policy:

- **`subtype` is no longer blindly passed through for every sonde type.** M10/M20
  (and any type auto_rx doesn't upload a subtype for) no longer send a meaningless
  `subtype` field that could confuse trackers. RS41/RS92/DFM/LMS6/MRZ still report
  their subtype as before.
- **Sentinel "no data" values are now filtered out** instead of being uploaded as
  real readings: temperature ≤ ‑273 °C, humidity/pressure < 0, velocity/heading
  ≤ ‑9999, and negative battery voltage are all now dropped rather than sent as-is.
- `dfmcode` is now only attached to DFM uploads (previously a global check).

## 🖥️ Web UI

- **SDR Devices table reordered**: status dot → Device → Frequency → Sonde →
  Status → **Timer** (new).
- **New countdown Timer column** (`HH:MM`) showing how long until a device returns
  to scanning if no more telemetry arrives — reflects whichever timeout actually
  applies (idle timeout, manual-idle timeout, or a fixed duration like a priority
  frequency check).
- **Sonde column renamed** from "Sonde Type" to "Sonde" and shortened: known types
  (`RS41`, `RS92`, `DFM06/09/17`, `M10`, `M20`) show as-is with any `-SG`/`-SGP`
  style suffix stripped; anything else is truncated to 5 characters to keep the
  column narrow.
- **New "home" button on the map**, stacked directly under the zoom control —
  recenters the map on your station's configured lat/lon at zoom level 9.

## 🔧 Maintenance & Code Quality

- Consolidated decoder binary path resolution into a single method
  (`RS1729Decoder.resolve_decoder_path()`); it was previously duplicated (and
  slightly out of sync) between `rs1729_decoder.py` and `device_manager.py`.
- Removed a handful of dead/no-op code paths and closed a rare thread-safety gap
  around the dynamic priority-frequency blacklist.
- Added diagnostic logging for RS41 PTU (temperature/humidity/pressure)
  availability — enable with `OPENWX_JSON_PTU_DEBUG=1` to see exactly what a
  decoder's JSON output contains if PTU data is missing.

## 🔒 Repository Updates & Changes

- **`.gitignore` hardened**: `config.yaml` (which contains your MQTT/SondeHub
  credentials and station identity) is now excluded.
- **New `config.yaml.example`** — a fully commented template with all secrets
  replaced by placeholders. If you're setting up a fresh install or your own fork,
  copy it: `cp config.yaml.example config.yaml`.

---

## ⬆️ Upgrade Notes for Gateway Operators

1. **Files touched this release**: `src/openwxsdr_app.py`, `src/hardware_info.py`,
    `src/sdr/device_manager.py`, `src/sdr/dft_detector.py`, `src/sdr/audio_pipeline.py`,
   `src/decoders/rs1729_decoder.py`, `src/output/sondehub_queue.py`, `src/telemetry/__init__.py`, `src/telemetry/telemetry.py`,
   `src/output/sondehub_output.py`, `src/webui/web_server.py`,
   `templates/index.html`. Make sure all of these are deployed together — a
   partial update (e.g. only `device_manager.py`) can leave the code referencing
   behavior that other files don't yet provide.
2. **No config.yaml changes required.** Everything is backward compatible with
   your existing configuration.
3. **Optional new setting**: `decoders.manual_idle_time` (seconds, default `1800`)
   controls how long an Import-API/manual "decode until lost" session waits without
   a frame before releasing the receiver. Lower it if you want faster recovery after
   a sonde lands; raise it if you're tracking sondes with long signal gaps.
4. **Recommended**: a full service restart (`sudo systemctl restart openwxsdr`)
   after updating, rather than relying on the auto-restart to pick up the new code.
5. If you maintain your own fork/repo: pull the updated `.gitignore` **before**
   your next commit if `config.yaml` was ever tracked, and consider rotating any
   credentials that may already be in your git history.
