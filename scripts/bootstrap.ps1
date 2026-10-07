# Shared by the Windows launchers. Does not start capture or change routing.
$RouteScopePython = Join-Path (Split-Path $PSScriptRoot -Parent) '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $RouteScopePython)) {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        py -3 -m venv (Split-Path (Split-Path $RouteScopePython -Parent) -Parent)
    } elseif (Get-Command python -ErrorAction SilentlyContinue) {
        python -m venv (Split-Path (Split-Path $RouteScopePython -Parent) -Parent)
    } else {
        throw 'Install Python 3.11+ with the Python launcher or add python to PATH.'
    }
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the Python environment.' }
}
& $RouteScopePython -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'
if ($LASTEXITCODE -ne 0) { throw 'Python 3.11+ is required. Recreate .venv with a supported Python.' }
& $RouteScopePython -c @'
import sys
try:
    import aiohttp, brotli, zstandard
    from importlib.metadata import version
    from route_scope import __version__
    sys.exit(0 if version('route-scope') == __version__ else 1)
except ImportError:
    sys.exit(1)
except Exception:
    sys.exit(1)
'@
if ($LASTEXITCODE -ne 0) {
    & $RouteScopePython -c "import importlib.util, sys; sys.exit(0 if importlib.util.find_spec('pip') else 1)"
    if ($LASTEXITCODE -ne 0) {
        & $RouteScopePython -m ensurepip --upgrade
        if ($LASTEXITCODE -ne 0) { throw 'Could not initialize pip in the Python environment.' }
    }
    & $RouteScopePython -m pip install -e (Split-Path $PSScriptRoot -Parent)
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed. Check your network and pip configuration.' }
}
