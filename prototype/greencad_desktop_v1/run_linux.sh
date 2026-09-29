#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  echo 'Create .venv first: python3 -m venv .venv && .venv/bin/python -m pip install -r requirements.txt'
  echo 'Ubuntu prerequisites: python3-venv and python3-tk'
  exit 1
fi
exec .venv/bin/python run_editor.py "$@"
