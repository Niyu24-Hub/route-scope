param(
    [string]$Distro = '',
    [int]$DashboardPort = 15927,
    [switch]$MetadataOnly,
    [switch]$WslOnly
)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$RouteScopeStatus = Join-Path $PSScriptRoot 'data\auto\watch-status.json'
if (Test-Path -LiteralPath $RouteScopeStatus) {
    try {
        $RouteScopeExisting = Get-Content -LiteralPath $RouteScopeStatus -Raw -Encoding UTF8 | ConvertFrom-Json
        $RouteScopeAge = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds() - $RouteScopeExisting.updated_epoch
        if ($RouteScopeExisting.state -eq 'running' -and $RouteScopeAge -lt 15 -and $RouteScopeExisting.dashboard_url -match '^http://127\.0\.0\.1:[0-9]+$') {
            Start-Process $RouteScopeExisting.dashboard_url
            Write-Host 'Route Scope is already running. Opened its dashboard.'
            exit 0
        }
    } catch { }
}
. (Join-Path $PSScriptRoot 'scripts\bootstrap.ps1')
$RouteScopeArgs = @('-m','route_scope','watch','--dashboard-port',"$DashboardPort",'--open-browser')
if ($Distro) { $RouteScopeArgs += @('--distro',$Distro) }
if ($MetadataOnly) { $RouteScopeArgs += '--metadata-only' }
if ($WslOnly) { $RouteScopeArgs += '--wsl-only' }
& $RouteScopePython @RouteScopeArgs
exit $LASTEXITCODE
