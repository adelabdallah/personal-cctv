#!/usr/bin/env bash
# Run as root (systemd ExecStartPre=+...). Seize the PiTFT from the console.
set -euo pipefail

systemctl stop getty@tty1 2>/dev/null || true
systemctl stop carriagetty@tty1 2>/dev/null || true

unbound=0
for vt in /sys/class/vtconsole/vtcon*; do
  if [[ -f "${vt}/name" ]] && grep -qi 'frame buffer' "${vt}/name" 2>/dev/null; then
    echo 0 >"${vt}/bind" 2>/dev/null && unbound=1 || true
  fi
done
if [[ "$unbound" -eq 0 ]]; then
  echo 0 >/sys/class/vtconsole/vtcon1/bind 2>/dev/null || true
fi

if [[ -w /sys/class/graphics/fbcon/cursor_blink ]]; then
  echo 0 >/sys/class/graphics/fbcon/cursor_blink 2>/dev/null || true
fi

python3 - <<'PY' 2>/dev/null || true
import fcntl, os
KDSETMODE = 0x4B3A
KD_GRAPHICS = 1
for path in ("/dev/tty0", "/dev/tty1", "/dev/console"):
    try:
        fd = os.open(path, os.O_RDWR)
    except OSError:
        continue
    try:
        fcntl.ioctl(fd, KDSETMODE, KD_GRAPHICS)
    except OSError:
        pass
    finally:
        os.close(fd)
PY

sysctl -w kernel.printk="3 3 1 3" >/dev/null 2>&1 || true
