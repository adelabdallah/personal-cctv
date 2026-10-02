#!/usr/bin/env bash
# tty1 login wrapper. Restarts the panel UI, or drops to a shell when skip-ui is set.
set -u
export PYTHONUNBUFFERED=1
export PATH="${HOME}/.local/bin:${PATH}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
export CCTV_ROOT="${REPO_ROOT}"
LOG="${HOME}/.local/share/cctv/ui.log"
SKIP="${HOME}/.local/share/cctv/skip-ui"
CCTV="${HOME}/.local/bin/cctv"

mkdir -p "$(dirname "${LOG}")"

say() {
  printf '%s\n' "$*" | tee -a "${LOG}" >/dev/null
}

if [[ ! -x "${CCTV}" ]]; then
  say "cctv: missing ${CCTV}"
  say "run: ${REPO_ROOT}/scripts/install-pi.sh"
  sleep 20
  exit 1
fi

while true; do
  if [[ -f "${SKIP}" ]]; then
    say "cctv: skip-ui, dropping to a shell $(date -Iseconds)"
    rm -f "${SKIP}"
    exec bash -l
  fi
  say "cctv: starting ui $(date -Iseconds)"
  "${CCTV}" ui >>"${LOG}" 2>&1
  status=$?
  say "cctv: ui exited ${status}, retry in 2s (SSH: touch ${SKIP})"
  sleep 2
done
