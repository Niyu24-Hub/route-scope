#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
# Keep the Linux environment separate from the Windows .venv directory.
route_scope_venv="${ROUTE_SCOPE_VENV:-$HOME/.local/share/route-scope/venv}"
if [[ ! -x "$route_scope_venv/bin/python" ]] || ! "$route_scope_venv/bin/python" -m pip --version >/dev/null 2>&1; then
  if ! python3 -m venv "$route_scope_venv"; then
    # Ubuntu minimal images may lack ensurepip. Bootstrap an isolated environment,
    # without sudo or changing the system Python installation.
    route_scope_bootstrap="${XDG_CACHE_HOME:-$HOME/.cache}/route-scope/virtualenv.pyz"
    python3 - "$route_scope_bootstrap" <<'PY'
from pathlib import Path
import sys, urllib.request
target = Path(sys.argv[1])
target.parent.mkdir(parents=True, exist_ok=True)
urllib.request.urlretrieve('https://bootstrap.pypa.io/virtualenv.pyz', target)
PY
    python3 "$route_scope_bootstrap" "$route_scope_venv"
  fi
fi
"$route_scope_venv/bin/python" -m pip install -e .
if [[ "${1:-}" == "--demo" ]]; then
  exec "$route_scope_venv/bin/python" -m route_scope demo --data-dir "$HOME/.local/share/route-scope/demo"
fi
exec "$route_scope_venv/bin/python" -m route_scope serve --data-dir "$HOME/.local/share/route-scope/data" "$@"
