#!/usr/bin/env bash
set -euo pipefail

if ! command -v cmdvault >/dev/null 2>&1; then
  echo "cmdvault is not on PATH. Install with: pip install ." >&2
  exit 1
fi

cmdvault install-hooks

echo "Done. Open a new shell or source your ~/.bashrc or ~/.zshrc to activate."
