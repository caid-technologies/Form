#!/usr/bin/env bash
set -Eeuo pipefail

REPO_URL="${FORMA_OSS_REPO_URL:-https://github.com/caid-technologies/Forma-OSS.git}"
INSTALL_DIR="${FORMA_OSS_INSTALL_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/forma-oss}"
WORKSPACE_DIR="${FORMA_WORKSPACE_DIR:-$HOME/forma-workspace}"
REF="${FORMA_OSS_REF:-}"

fail() {
  printf 'forma install: %s\n' "$1" >&2
  exit 2
}

command -v git >/dev/null 2>&1 || fail "git was not found on PATH. Install Git and rerun this command."

if command -v python3 >/dev/null 2>&1; then
  PYTHON_BIN="$(command -v python3)"
elif command -v python >/dev/null 2>&1; then
  PYTHON_BIN="$(command -v python)"
else
  fail "Python 3.11 or newer was not found on PATH. Install Python and rerun this command."
fi

if [ -e "$INSTALL_DIR" ] && [ ! -d "$INSTALL_DIR/.git" ]; then
  fail "$INSTALL_DIR exists but is not a Forma Git checkout. Set FORMA_OSS_INSTALL_DIR to another path."
fi

if [ ! -d "$INSTALL_DIR/.git" ]; then
  mkdir -p "$(dirname "$INSTALL_DIR")"
  clone_args=(clone --depth 1)
  if [ -n "$REF" ]; then
    clone_args+=(--branch "$REF")
  fi
  clone_args+=("$REPO_URL" "$INSTALL_DIR")
  git "${clone_args[@]}"
else
  printf 'Using existing Forma checkout at %s\n' "$INSTALL_DIR"
fi

export PYTHON_BIN

"$PYTHON_BIN" "$INSTALL_DIR/scripts/development/setup-opencode.py" \
  --root "$INSTALL_DIR" \
  --workspace "$WORKSPACE_DIR" \
  --install-cli

printf '\nStarting the local Forma backend and UI. Keep this terminal open.\n\n'
exec bash "$INSTALL_DIR/scripts/development/dev.sh"
