param([string]$ConfigPath = 'guard.example.toml',[string]$OutboundProxy = '')
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
. (Join-Path $PSScriptRoot 'scripts\bootstrap.ps1')
$RouteScopeArgs = @('-m','route_scope','serve','--config',$ConfigPath)
if ($OutboundProxy) { $RouteScopeArgs += @('--outbound-proxy',$OutboundProxy) }
& $RouteScopePython @RouteScopeArgs
exit $LASTEXITCODE
