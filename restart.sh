#!/usr/bin/env bash
set -euo pipefail

sudo systemctl restart openwxsdr
sudo systemctl status openwxsdr
sudo journalctl -u openwxsdr.service -f -n 50
