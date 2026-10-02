#!/usr/bin/env bash
# Install the panel UI on the Raspberry Pi. Privileged steps use sudo.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
REBOOT=0
for arg in "$@"; do
  if [[ "${arg}" == "--reboot" ]]; then
    REBOOT=1
  fi
done

if [[ "$(id -u)" -eq 0 ]]; then
  echo "run this as adel, not root. it will sudo when it needs to." >&2
  exit 1
fi

TARGET_USER="$(id -un)"
TARGET_HOME="${HOME}"
NEED_REBOOT=0
VENV="${TARGET_HOME}/.local/share/cctv/venv"
BIN="${TARGET_HOME}/.local/bin"

need_sudo() {
  sudo "$@"
}

install_packages() {
  echo "== apt =="
  local pkgs=(python3-picamera2 python3-pygame python3-venv python3-smbus i2c-tools)
  if ! apt-cache show python3-smbus >/dev/null 2>&1; then
    pkgs=(python3-picamera2 python3-pygame python3-venv python3-smbus2 i2c-tools)
  fi
  need_sudo apt-get update
  need_sudo apt-get install -y "${pkgs[@]}"
}

install_cloudflared() {
  echo "== cloudflared =="
  if command -v cloudflared >/dev/null 2>&1 || [[ -x "${BIN}/cloudflared" ]]; then
    echo "cloudflared already installed"
    return
  fi
  mkdir -p "${BIN}"
  curl -fsSL -o "${BIN}/cloudflared" \
    https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64
  chmod 755 "${BIN}/cloudflared"
}

setup_venv() {
  echo "== venv =="
  mkdir -p "${TARGET_HOME}/.local/share/cctv"
  if [[ ! -x "${VENV}/bin/python" ]]; then
    python3 -m venv --system-site-packages "${VENV}"
  fi
  "${VENV}/bin/pip" install -r "${REPO_ROOT}/requirements-pi.txt"
}

install_command() {
  echo "== cctv command =="
  mkdir -p "${BIN}"
  cat > "${BIN}/cctv" <<EOF
#!/bin/sh
ROOT="\${CCTV_ROOT:-${REPO_ROOT}}"
PY="${VENV}/bin/python"
if [ ! -x "\${PY}" ]; then
  echo "cctv: missing \${PY}" >&2
  echo "run: ${REPO_ROOT}/scripts/install-pi.sh" >&2
  exit 1
fi
export PYTHONPATH="\${ROOT}\${PYTHONPATH:+:\${PYTHONPATH}}"
export PATH="${BIN}:\${PATH}"
cd "\${ROOT}"
exec "\${PY}" -m cctv_ui "\$@"
EOF
  chmod 755 "${BIN}/cctv"
  need_sudo ln -sfn "${BIN}/cctv" /usr/local/bin/cctv
  local bashrc="${TARGET_HOME}/.bashrc"
  if ! grep -q '.local/bin' "${bashrc}"; then
    printf '\nexport PATH="$HOME/.local/bin:$PATH"\n' >> "${bashrc}"
  fi
}

install_profile() {
  echo "== tty1 autostart =="
  local profile="${TARGET_HOME}/.profile"
  local begin="# BEGIN cctv ui tty1"
  local end="# END cctv ui tty1"
  touch "${profile}"
  local tmp
  tmp="$(mktemp)"
  awk -v begin="${begin}" -v end="${end}" '
    $0 == begin {skip=1; next}
    $0 == end {skip=0; next}
    skip != 1 {print}
  ' "${profile}" > "${tmp}"
  cat >> "${tmp}" <<EOF
${begin}
if [ -z "\${SSH_CONNECTION:-}" ] && [ -z "\${DISPLAY:-}" ] && [ -z "\${WAYLAND_DISPLAY:-}" ] && [ "\$(tty 2>/dev/null)" = "/dev/tty1" ]; then
  if [ ! -f "\${HOME}/.local/share/cctv/skip-ui" ]; then
    exec "${REPO_ROOT}/scripts/cctv-tty.sh"
  fi
fi
${end}
EOF
  mv "${tmp}" "${profile}"
  chmod 755 "${REPO_ROOT}/scripts/cctv-tty.sh"
}

install_sudoers() {
  echo "== reboot and shutdown =="
  local systemctl
  systemctl="$(command -v systemctl)"
  local tmp sudoers
  tmp="$(mktemp)"
  sudoers="/etc/sudoers.d/cctv-power"
  cat > "${tmp}" <<EOF
${TARGET_USER} ALL=(root) NOPASSWD: ${systemctl} reboot, ${systemctl} poweroff
EOF
  need_sudo install -m 440 "${tmp}" "${sudoers}"
  rm -f "${tmp}"
  if ! need_sudo visudo -cf "${sudoers}"; then
    echo "sudoers check failed, removing ${sudoers}" >&2
    need_sudo rm -f "${sudoers}"
    exit 1
  fi
}

enable_i2c() {
  echo "== i2c for the touch panel =="
  local cfg="/boot/firmware/config.txt"
  if [[ ! -f "${cfg}" ]]; then
    echo "no ${cfg}, skip i2c" >&2
    return
  fi
  if grep -q '^dtparam=i2c_arm=on' "${cfg}"; then
    echo "i2c already enabled"
  elif grep -q '^#dtparam=i2c_arm=on' "${cfg}"; then
    need_sudo sed -i 's/^#dtparam=i2c_arm=on/dtparam=i2c_arm=on/' "${cfg}"
    NEED_REBOOT=1
  else
    echo 'dtparam=i2c_arm=on' | need_sudo tee -a "${cfg}" >/dev/null
    NEED_REBOOT=1
  fi
  # The adapter is not enough. /dev/i2c-1 comes from the i2c-dev module.
  echo i2c-dev | need_sudo tee /etc/modules-load.d/cctv-i2c.conf >/dev/null
  need_sudo /sbin/modprobe i2c-dev
}

install_packages
install_cloudflared
setup_venv
install_command
install_profile
install_sudoers
enable_i2c

echo
echo "installed. viewer password is pass123."
echo "commands: cctv start, cctv camera, cctv reboot, cctv shutdown, cctv link"
echo "panel recovery: touch ~/.local/share/cctv/skip-ui"
if [[ "${NEED_REBOOT}" == 1 ]]; then
  echo "i2c was enabled. the touch panel needs a reboot."
  if [[ "${REBOOT}" == 1 ]]; then
    echo "rebooting"
    need_sudo reboot
  fi
fi
