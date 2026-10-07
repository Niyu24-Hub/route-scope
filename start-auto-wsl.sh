#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
route_scope_project="$PWD"
route_scope_data="${ROUTE_SCOPE_DATA:-$HOME/.local/share/route-scope/auto}"
route_scope_python="${ROUTE_SCOPE_PYTHON:-$HOME/.local/share/route-scope/venv/bin/python}"
if [[ ! -x "$route_scope_python" ]]; then
  route_scope_python="$HOME/.local/share/route-scope/test-env/bin/python"
fi
if [[ ! -x "$route_scope_python" ]]; then
  echo 'Create a Python 3.11+ environment and install this project first, or launch start-auto.cmd from Windows.' >&2
  exit 1
fi
if [[ "$EUID" -ne 0 ]]; then
  exec sudo env "PYTHONPATH=$route_scope_project" "$route_scope_python" -m route_scope watch --data-dir "$route_scope_data" "$@"
fi
exec env "PYTHONPATH=$route_scope_project" "$route_scope_python" -m route_scope watch --data-dir "$route_scope_data" "$@"
