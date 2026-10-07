param(
    [string]$Distro = '',
    [int]$DashboardPort = 15927,
    [switch]$MetadataOnly,
    [switch]$WslOnly
)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
. (Join-Path $PSScriptRoot 'scripts\bootstrap.ps1')
$RouteScopePreparation = & $RouteScopePython -m route_scope watch-prepare
if ($LASTEXITCODE -ne 0) { throw 'Could not prepare capture service. See the error above.' }
$RouteScopePreparation = ($RouteScopePreparation -join "`n") | ConvertFrom-Json
if ($RouteScopePreparation.action -eq 'reuse') {
    if ($RouteScopePreparation.dashboard_url -notmatch '^http://127\.0\.0\.1:[0-9]+$') { throw 'Invalid dashboard URL.' }
    Start-Process $RouteScopePreparation.dashboard_url
    Write-Host "Route Scope $($RouteScopePreparation.version) is already running. Opened its dashboard."
    exit 0
}
if ($RouteScopePreparation.action -eq 'restart') {
    Write-Host "Previous Route Scope stopped. Starting version $($RouteScopePreparation.version)."
}
$RouteScopeArgs = @('-m','route_scope','watch','--dashboard-port',"$DashboardPort",'--open-browser')
if ($Distro) { $RouteScopeArgs += @('--distro',$Distro) }
if ($MetadataOnly) { $RouteScopeArgs += '--metadata-only' }
if ($WslOnly) { $RouteScopeArgs += '--wsl-only' }
& $RouteScopePython @RouteScopeArgs
exit $LASTEXITCODE
