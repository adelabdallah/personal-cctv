#!/usr/bin/env bash
# Install the Pi Zero W camera menu. Privileged steps use sudo.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

if [[ "$(id -u)" -eq 0 ]]; then
  echo "run this as adel, not root. it will sudo when it needs to." >&2
  exit 1
fi

TARGET_USER="$(id -un)"
TARGET_HOME="${HOME}"
BIN="${TARGET_HOME}/.local/bin"

need_sudo() {
  sudo "$@"
}

echo "== apt =="
need_sudo apt-get update
need_sudo apt-get install -y \
  ffmpeg \
  fbset \
  python3-fastapi \
  python3-uvicorn \
  python3-pydantic-settings \
  python3-yaml \
  python3-jinja2 \
  python3-dotenv \
  python3-multipart \
  python3-pil \
  python3-picamera2 \
  python3-gpiozero \
  python3-numpy \
  iputils-ping

echo "== groups =="
need_sudo usermod -aG spi,gpio,video,sudo "${TARGET_USER}" 2>/dev/null \
  || need_sudo usermod -aG gpio,video,sudo "${TARGET_USER}"

echo "== cctv command =="
mkdir -p "${BIN}"
cat > "${BIN}/cctv" <<EOF
#!/bin/sh
ROOT="\${CCTV_ROOT:-${REPO_ROOT}}"
export PYTHONPATH="\${ROOT}\${PYTHONPATH:+:\${PYTHONPATH}}"
cd "\${ROOT}"
exec /usr/bin/python3 -m cctv_ui "\$@"
EOF
chmod 755 "${BIN}/cctv"
need_sudo ln -sfn "${BIN}/cctv" /usr/local/bin/cctv

echo "== pitft, menu, and health check =="
need_sudo install -m 755 "${REPO_ROOT}/scripts/claim-pitft-display.sh" /usr/local/sbin/claim-pitft-display.sh
need_sudo install -m 755 "${REPO_ROOT}/scripts/cctv-repair-network.sh" /usr/local/sbin/cctv-repair-network
need_sudo install -d /etc/systemd/system.conf.d
need_sudo install -m 644 "${REPO_ROOT}/scripts/systemd/cctv-zero-watchdog.conf" /etc/systemd/system.conf.d/cctv-watchdog.conf
for unit in cctv-stream.service cctv-supervisor.service cctv-supervisor.timer; do
  unit_src="${REPO_ROOT}/scripts/systemd/${unit}"
  sed -e "s/@USER@/${TARGET_USER}/g" -e "s|@HOME@|${TARGET_HOME}|g" "${unit_src}" \
    | need_sudo tee "/etc/systemd/system/${unit}" >/dev/null
done
sed -e "s/@USER@/${TARGET_USER}/g" \
  -e "s|@HOME@|${TARGET_HOME}|g" \
  -e "s|@ROOT@|${REPO_ROOT}|g" \
  "${REPO_ROOT}/scripts/systemd/cctv-zero-ui.service" \
  | need_sudo tee /etc/systemd/system/cctv-zero-ui.service >/dev/null

systemctl="$(command -v systemctl)"
tmp="$(mktemp)"
cat > "${tmp}" <<EOF
${TARGET_USER} ALL=(root) NOPASSWD: ${systemctl} reboot, ${systemctl} poweroff, /usr/local/sbin/claim-pitft-display.sh, /usr/local/sbin/cctv-repair-network
EOF
need_sudo install -m 440 "${tmp}" /etc/sudoers.d/cctv-zero-power
rm -f "${tmp}"
if ! need_sudo visudo -cf /etc/sudoers.d/cctv-zero-power; then
  echo "sudoers check failed, removing /etc/sudoers.d/cctv-zero-power" >&2
  need_sudo rm -f /etc/sudoers.d/cctv-zero-power
  exit 1
fi

echo "== retire todo =="
if [[ -f /opt/todo-gadget/data/lists.json ]]; then
  cp -f /opt/todo-gadget/data/lists.json "${TARGET_HOME}/todo-lists.json"
fi
need_sudo systemctl disable --now todo-ui@"${TARGET_USER}".service 2>/dev/null || true
need_sudo rm -f /usr/local/bin/todo /usr/local/bin/todo-ui /etc/systemd/system/todo-ui@.service
need_sudo rm -rf /opt/todo-gadget

mkdir -p "${TARGET_HOME}/.local/share/cctv"
if [[ ! -f "${TARGET_HOME}/.local/share/cctv/supervisor.conf" ]]; then
  cat > "${TARGET_HOME}/.local/share/cctv/supervisor.conf" <<'EOF'
# Times are seconds. The Zero does not own the public tunnel.

boot_grace_s=180
failure_window_s=300
repair_wait_s=90
max_reboots_per_hour=6

check_route=yes
check_ping_gateway=yes
check_ping_public=yes
check_tailscale=yes
check_stream=yes
check_tunnel=no

ping_public=1.1.1.1
interface=wlan0
EOF
fi

need_sudo systemctl daemon-reload
need_sudo systemctl enable --now cctv-zero-ui.service
need_sudo systemctl enable --now cctv-stream.service cctv-supervisor.timer

echo
echo "menu installed. a selected camera comes back after a crash or reboot."
echo "a check every 30 seconds repairs Wi-Fi, Tailscale, or the stream."
echo "viewer password is pass123."
echo "slow mode is the one-frame-a-second switch on the viewer page."
