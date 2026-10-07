param(
    [switch]$Demo,
    [string]$Upstream = 'https://anyrouter.top',
    [string]$OutboundProxy = ''
)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
. (Join-Path $PSScriptRoot 'scripts\bootstrap.ps1')
if ($Demo) {
    & $RouteScopePython -m route_scope demo
} else {
    $RouteScopeArgs = @('-m', 'route_scope', 'serve', '--upstream', $Upstream)
    if ($OutboundProxy) { $RouteScopeArgs += @('--outbound-proxy', $OutboundProxy) }
    & $RouteScopePython @RouteScopeArgs
}
exit $LASTEXITCODE
