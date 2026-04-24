#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"

if ! command -v brew >/dev/null 2>&1; then
  echo "Homebrew is required. Install it first: https://brew.sh" >&2
  exit 1
fi

brew bundle --file "$ROOT_DIR/Brewfile"
python3 -m pip install --upgrade pip
python3 -m pip install -r "$ROOT_DIR/requirements.txt"
python3 -m pip install -e "$ROOT_DIR"

echo "Bootstrap complete. You can now run: sudo bifrost --help"
