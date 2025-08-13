#!/usr/bin/env bash
set -euo pipefail

REPO_URL="https://github.com/ElnatanSamuel/repty-cli.git"
APP_NAME="repty-cli"
BIN_NAME="repty"

say() { printf "\033[1;32m[repty]\033[0m %s\n" "$*"; }
warn() { printf "\033[1;33m[repty]\033[0m %s\n" "$*"; }
err() { printf "\033[1;31m[repty]\033[0m %s\n" "$*"; }

ensure_pipx() {
  if ! command -v pipx >/dev/null 2>&1; then
    warn "pipx not found; installing with pip --user..."
    if command -v python3 >/dev/null 2>&1; then
      python3 -m pip install --user pipx || true
    elif command -v python >/dev/null 2>&1; then
      python -m pip install --user pipx || true
    else
      err "Python not found. Please install Python 3.9+ and re-run."
      exit 1
    fi
    # try to add pipx to PATH for current shell
    export PATH="$HOME/.local/bin:$PATH"
  fi
}

ensure_path() {
  # Ask pipx to add ~/.local/bin to PATH in shell profile
  if command -v pipx >/dev/null 2>&1; then
    pipx ensurepath >/dev/null 2>&1 || true
  fi
  # Make sure current process can find the newly installed binary
  export PATH="$HOME/.local/bin:$PATH"
}

install_app() {
  say "Installing Repty via pipx from $REPO_URL ..."
  pipx install --force "git+${REPO_URL}" >/dev/null
  say "Installed."
}

run_app() {
  # Run via absolute path to avoid PATH issues in current shell
  if [ -x "$HOME/.local/bin/${BIN_NAME}" ]; then
    "$HOME/.local/bin/${BIN_NAME}"
  else
    # fallback if pipx uses different bin dir
    if command -v ${BIN_NAME} >/dev/null 2>&1; then
      ${BIN_NAME}
    else
      warn "Could not locate ${BIN_NAME} on PATH. You can run it with: $HOME/.local/bin/${BIN_NAME}"
    fi
  fi
}

main() {
  say "Preparing to install Repty (FTS + AI history search)"
  ensure_pipx
  ensure_path
  install_app
  say "Launching Repty onboarding wizard..."
  run_app
  say "Tip: open a new terminal or run 'source ~/.bashrc' (or '~/.zshrc') to pick up PATH changes and hooks."
}

main "$@"
