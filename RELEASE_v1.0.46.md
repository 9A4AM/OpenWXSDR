# OpenWXSDR v1.0.46 — Critical Bug Fix Release

**Release Date:** May 28, 2026

## Overview

This is an emergency release fixing a critical regression introduced in recent updates that prevented `fixed_channels` from working correctly. The scanner was interfering with explicitly configured channels and using incorrect decoder types.

## Critical Fixes

### 1. Scanner Interference with Fixed Channels *(Critical)*

- **Root Cause:** Scanner was auto-detecting `fixed_channel` frequencies before the `fixed_channels` startup could configure them with the correct types
- **Symptom:** User specifies `type='DFM'` in config, but system decodes as RS41
- **Impact:** 0 frames decoded — system appeared completely broken
- **Fix:** Scanner now skips frequencies that match `fixed_channels` configuration
- **Details:** Added `_is_fixed_channel_frequency()` check in scan loop to prevent auto-detection of explicitly configured frequencies

### 2. Fixed Channel Type Override Enforcement *(Improved)*

- `DeviceWorker` now receives reference to parent manager's `fixed_channels` list
- Scanner respects 10 kHz tolerance when checking for fixed channel match
- Prevents bandwidth fallback heuristics from overriding user specifications

## Technical Changes

**Modified:** `src/sdr/device_manager.py`

- `DeviceWorker.__init__()` now accepts `manager` parameter
- Added `_is_fixed_channel_frequency()` method to check against `fixed_channels`
- Scanner loop now checks fixed channel frequencies before auto-detection
- `RTLSDRDeviceManager.initialize()` passes `self` reference to workers

**Updated:** All version strings to `1.0.46`

- `src/__init__.py`
- `openwxsdr.py`
- `src/webui/web_server.py`
- `build_package.ps1`
- `build_package.sh`

## User Impact

- **Fixed:** System now correctly respects `type` specifications in `fixed_channels`
- **Fixed:** Scanner no longer interferes with explicitly configured frequencies
- **Fixed:** DFT failures on fixed channels no longer cause wrong decoder selection
- **Result:** Fixed channels work reliably as configured by user

## Deployment

This is a Python-only update. Deploy using:

```bash
# 1. Download
openwxsdr-1.0.46.tar.gz

# 2. Transfer to Raspberry Pi
scp openwxsdr-1.0.46.tar.gz pi@raspberry-pi:~/

# 3. Extract and install
ssh pi@raspberry-pi
tar -xzf openwxsdr-1.0.46.tar.gz
cd openwxsdr-1.0.46
sudo cp -r src/ /home/pi/OpenWXSDR/
sudo systemctl restart openwxsdr
```

## Verification

After deployment, verify the following:

1. Check systemd status: `sudo systemctl status openwxsdr`
2. Check logs: `sudo journalctl -u openwxsdr -f`
3. Look for: `"Skipping XXX.XXX MHz - reserved for fixed_channel with specified type"`
4. Verify Web UI shows correct decoder type for fixed channels
5. Confirm frames are being decoded (frame count increasing)

## Testing

Tested on:

- Raspberry Pi 4 with 4x RTL-SDR V3 dongles
- Fixed channel: DFM at 403.850 MHz
- Verified scanner skips the frequency
- Verified correct DFM decoder starts
- Verified frame decoding works correctly

## Background

Previous versions (v1.0.44–v1.0.45) introduced regression bugs where the scanner would start decoding signals before `fixed_channels` configuration could take effect. This caused auto-detection to run (and fail due to PLL errors), resulting in wrong decoder selection via bandwidth fallback heuristics.

> *"Nothing changed with hardware... only your software 'fixes' not working reliable since 2 days!!"*
> — System worked perfectly for 3 weeks with 200,000+ decoded frames before regression was introduced.

This release restores the correct behavior and ensures user type specifications are always respected.

## Known Issues

- RTL-SDR PLL lock failures still occur occasionally *(hardware limitation)*
- DFT detection may fail on first attempt *(USB settling time)*
- **Workaround:** Set `use_dft_detect: false` to disable auto-detection entirely

## Next Release

- Planned: Improved PLL failure handling and recovery
- Planned: Support for SDRplay devices
- Planned: Better USB device initialization sequencing
- Planned: Enhanced diagnostic logging for troubleshooting

---
