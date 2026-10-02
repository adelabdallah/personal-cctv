#!/usr/bin/env bash
# Load the I2C device node the touch panel needs. Asks for a sudo password.
set -euo pipefail
sudo /sbin/modprobe i2c-dev
echo i2c-dev | sudo tee /etc/modules-load.d/cctv-i2c.conf >/dev/null
echo "touch bus ready"
