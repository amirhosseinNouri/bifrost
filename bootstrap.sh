#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
FORCE=0
PIP_INDEX_URL="${PIP_INDEX_URL:-https://mirror-pypi.runflare.com/simple}"
PIP_TIMEOUT="${PIP_TIMEOUT:-60}"
PIP_RETRIES="${PIP_RETRIES:-10}"

if [[ "${1:-}" == "--force" ]]; then
  FORCE=1
fi

log() {
  printf '[bootstrap] %s\n' "$1"
}

require_cmd() {
  local cmd="$1"
  local help_msg="$2"
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "$help_msg" >&2
    exit 1
  fi
}

pip_install() {
  python3 -m pip "$@" \
    --index-url "$PIP_INDEX_URL" \
    --timeout "$PIP_TIMEOUT" \
    --retries "$PIP_RETRIES"
}

install_formula_if_missing() {
  local formula="$1"
  local cmd="$2"

  if command -v "$cmd" >/dev/null 2>&1; then
    log "Skipping $formula ($cmd already exists)."
    return
  fi

  log "Installing $formula ..."
  brew install "$formula"
}

install_python_reqs() {
  log "Installing Python dependencies from: $PIP_INDEX_URL"
  pip_install install -r "$ROOT_DIR/requirements.txt"
}

install_bifrost_editable() {
  if [[ "$FORCE" -eq 0 ]] && command -v bifrost >/dev/null 2>&1; then
    log "Skipping bifrost install (bifrost command already exists)."
    log "Use ./bootstrap.sh --force to reinstall."
    return
  fi

  log "Installing bifrost package ..."
  pip_install install -e "$ROOT_DIR"
}

require_cmd brew "Homebrew is required. Install it first: https://brew.sh"
require_cmd python3 "Python 3 is required. Install Python 3.10+ first."

install_formula_if_missing "openvpn" "openvpn"
install_formula_if_missing "sstp-client" "sstpc"
install_formula_if_missing "xray" "xray"
install_python_reqs
install_bifrost_editable

log "Bootstrap complete. You can now run: sudo bifrost --help"
