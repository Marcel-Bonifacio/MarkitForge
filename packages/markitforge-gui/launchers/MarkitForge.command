#!/bin/bash
# SPDX-FileCopyrightText: 2026-present MarkitForge contributors
# SPDX-License-Identifier: Apache-2.0
# MarkitForge launcher for macOS (and Linux). Double-click it in Finder.
#
# First run: creates a private Python environment in "launchers/.venv" and
# installs MarkitForge from this folder's checkout. Later runs start straight
# away. To reinstall from scratch, delete the "launchers/.venv" folder.

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../../.." && pwd)"
VENV="$HERE/.venv"
READY="$VENV/.markitforge-ready"
CORE="$REPO/packages/markitforge"
GUI="$REPO/packages/markitforge-gui"

pause_and_exit() {
  echo
  read -r -p "Press Enter to close this window." _ || true
  exit "${1:-1}"
}

fail_setup() {
  echo
  echo "Setup did not finish. Nothing was changed outside this folder."
  echo "Check your internet connection and try again."
  rm -rf "$VENV"
  pause_and_exit 1
}

if [ ! -f "$CORE/pyproject.toml" ] || [ ! -f "$GUI/pyproject.toml" ]; then
  echo "This launcher must stay inside the MarkitForge folder"
  echo "(packages/markitforge-gui/launchers). It could not find:"
  echo "  $CORE"
  pause_and_exit 1
fi

if [ ! -f "$READY" ]; then
  echo "Setting up MarkitForge for the first time."
  echo "This downloads a few hundred MB and can take several minutes."
  echo
  rm -rf "$VENV"
  if command -v uv >/dev/null 2>&1; then
    uv venv --python ">=3.10,<3.15" "$VENV" || fail_setup
    uv pip install --python "$VENV/bin/python" \
      -e "$CORE[all]" -e "$GUI" || fail_setup
  else
    PY=""
    for candidate in python3.13 python3.12 python3.11 python3.10 python3; do
      if command -v "$candidate" >/dev/null 2>&1 && \
         "$candidate" -c 'import sys; sys.exit(0 if (3, 10) <= sys.version_info[:2] < (3, 15) else 1)' 2>/dev/null; then
        PY="$candidate"
        break
      fi
    done
    if [ -z "$PY" ]; then
      echo "MarkitForge needs Python 3.10 or newer."
      echo "Install it from https://www.python.org/downloads/ and double-click this file again."
      pause_and_exit 1
    fi
    "$PY" -m venv "$VENV" || fail_setup
    "$VENV/bin/python" -m pip install --upgrade pip || fail_setup
    "$VENV/bin/python" -m pip install -e "$CORE[all]" -e "$GUI" || fail_setup
  fi
  touch "$READY"
  echo
  echo "Setup complete."
fi

echo "Starting MarkitForge. Your browser will open in a moment."
echo "Keep this window open while you use MarkitForge; close it to stop."
echo
"$VENV/bin/markitforge-gui" "$@"
status=$?
if [ "$status" -ne 0 ] && [ "$status" -ne 130 ]; then
  pause_and_exit "$status"
fi
