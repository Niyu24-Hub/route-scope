param([Parameter(Mandatory=$true)][string]$InstallerPath)
$ErrorActionPreference = 'Stop'
$RouteScopeInstaller = (Resolve-Path -LiteralPath $InstallerPath).Path
$RouteScopeSignature = Get-AuthenticodeSignature -LiteralPath $RouteScopeInstaller
if ($RouteScopeSignature.Status -ne 'Valid') { throw 'The installer does not have a valid Authenticode signature.' }
[pscustomobject]@{
    Installer = $RouteScopeInstaller
    Signature = $RouteScopeSignature.Status
    Publisher = $RouteScopeSignature.SignerCertificate.Subject
    SHA256 = (Get-FileHash -LiteralPath $RouteScopeInstaller -Algorithm SHA256).Hash
    SuggestedArguments = '/winpcap_mode=no /no_kill=yes'
    Executed = $false
} | Format-List
Write-Host 'Download from https://npcap.com/#download and review the publisher. This script has not installed or changed a driver.'
