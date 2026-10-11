#!/bin/sh
# Bring Wi-Fi and Tailscale back. Installed for passwordless sudo.
IFACE="${CCTV_IFACE:-wlan0}"
/usr/sbin/ip link set "${IFACE}" down || true
/usr/sbin/ip link set "${IFACE}" up || true
if /usr/bin/systemctl is-active --quiet NetworkManager; then
  /usr/bin/systemctl restart NetworkManager || true
elif /usr/bin/systemctl is-active --quiet wpa_supplicant; then
  /usr/bin/systemctl restart wpa_supplicant || true
fi
if /usr/bin/systemctl is-active --quiet tailscaled; then
  /usr/bin/systemctl restart tailscaled || true
fi
exit 0
