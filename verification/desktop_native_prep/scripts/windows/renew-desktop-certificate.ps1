# Preparation-only renewal runner. Never invoked during source preparation.
# Assumes an ALREADY approved/provisioned Posh-ACME order under the task identity.
# No token argument, token collection, initial issuance or TOS acceptance here.
param([switch]$Apply)
$ErrorActionPreference = 'Stop'
if (-not $Apply) { Write-Output 'Preview: renew the existing exact HOnY order if due, stage versioned PEM files, validate and activate. No action performed.'; return }
$Root = (Resolve-Path "$PSScriptRoot\..\..").Path
. "$PSScriptRoot\desktop-python-preflight.ps1"
Assert-HOnYMachinePython -Root $Root
. "$PSScriptRoot\desktop-acl.ps1"
$Runtime = Join-Path $env:ProgramData 'HOnY'
$Config = Get-Content -Raw (Join-Path $Runtime 'desktop.json') | ConvertFrom-Json
$Domain = ([Uri]$Config.origin).DnsSafeHost
# Global named mutex prevents overlapping DNS TXT challenges on this machine.
# DuckDNS has account-wide TXT state; other machines/orders must not run concurrently.
$Mutex = New-Object Threading.Mutex($false, 'Global\HOnYDuckDNSRenewal')
$Held = $false
try {
    $Held = $Mutex.WaitOne(0)
    if (-not $Held) { throw 'Another HOnY certificate operation is active.' }
    Import-Module Posh-ACME -MinimumVersion 4.0 -MaximumVersion 4.99 -ErrorAction Stop
    $Order = Get-PAOrder -MainDomain $Domain -Name 'HOnY'
    if (-not $Order) { throw 'Approved HOnY ACME order is missing. No automatic initial issuance.' }
    if ($Order.MainDomain -ne $Domain -or @($Order.SANs).Count -ne 0 -or @($Order.Plugin).Count -ne 1 -or $Order.Plugin[0] -ne 'DuckDNS') { throw 'Order must contain only the exact canonical hostname and DuckDNS plugin.' }
    if (-not $Order.UseSerialValidation) { throw 'DuckDNS order must use serial validation.' }
    # Select exact order, never AllOrders/AllAccounts or Force (respects renewal window).
    $null = Submit-Renewal -MainDomain $Domain -Name 'HOnY' -ErrorAction Stop
    $Current = Get-PACertificate -MainDomain $Domain -Name 'HOnY'
    if (-not $Current) { throw 'No certificate available after renewal check.' }
    # A previous renewal may have succeeded while activation failed. Retry deployment
    # from the cached order even if Submit-Renewal skipped issuance this time.
    $Stage = Join-Path $Runtime ('tls\' + $Current.Thumbprint)
    if (-not (Test-Path $Stage)) {
        New-Item -ItemType Directory -Path $Stage -Force | Out-Null
        Set-HOnYAccess -Path $Stage -ServiceRights ReadAndExecute
        Copy-Item -LiteralPath $Current.FullChainFile -Destination (Join-Path $Stage 'fullchain.pem')
        Copy-Item -LiteralPath $Current.KeyFile -Destination (Join-Path $Stage 'privkey.pem')
        Set-HOnYAccess -Path $Stage -ServiceRights ReadAndExecute -Recurse
    }
    $Certificate = Join-Path $Stage 'fullchain.pem'
    $Key = Join-Path $Stage 'privkey.pem'
    if ($Config.certificate -ne $Certificate -or $Config.private_key -ne $Key) {
        & "$PSScriptRoot\activate-desktop-certificate.ps1" -Certificate $Certificate -PrivateKey $Key -Apply
    } else {
        & (Join-Path $Root '.venv\Scripts\python.exe') -I "$PSScriptRoot\desktop_admin_host.py" certificate --health
        if ($LASTEXITCODE -ne 0) { throw 'Current certificate health check failed.' }
    }
    Write-Output 'HOnY renewal/activation check completed. No token or key material logged.'
} catch {
    # Generic message: provider errors can contain sensitive request parameters.
    Write-Error 'HOnY certificate renewal/activation failed. Check secure local diagnostics and expiry; do not bypass TLS.'
    exit 1
} finally {
    if ($Held) { $Mutex.ReleaseMutex() }
    $Mutex.Dispose()
}
