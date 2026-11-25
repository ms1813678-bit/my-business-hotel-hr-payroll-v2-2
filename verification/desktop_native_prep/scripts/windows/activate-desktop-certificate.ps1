# Activates a certificate already issued by a separately approved ACME/DNS flow.
# Does not contact DuckDNS/ACME, collect tokens, or register a scheduled task.
param(
    [Parameter(Mandatory=$true)][string]$Certificate,
    [Parameter(Mandatory=$true)][string]$PrivateKey,
    [switch]$Apply
)
$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\desktop-acl.ps1"
$Root = (Resolve-Path "$PSScriptRoot\..\..").Path
. "$PSScriptRoot\desktop-python-preflight.ps1"
Assert-HOnYMachinePython -Root $Root
$Python = Join-Path $Root '.venv\Scripts\python.exe'
$ConfigPath = Join-Path $env:ProgramData 'HOnY\desktop.json'
$Config = Get-Content -Raw $ConfigPath | ConvertFrom-Json
$Candidate = "$ConfigPath.candidate"
$Previous = "$ConfigPath.previous"
if ((Test-Path $Candidate) -or (Test-Path $Previous)) { throw 'A previous certificate operation needs review first.' }
$Config.certificate = (Resolve-Path -LiteralPath $Certificate).Path
$Config.private_key = (Resolve-Path -LiteralPath $PrivateKey).Path
if (-not $Apply) { Write-Output 'Preview only: candidate certificate/key paths resolved. No service or files changed.'; return }
if ($Config.certificate -eq (Get-Content -Raw $ConfigPath | ConvertFrom-Json).certificate -or $Config.private_key -eq (Get-Content -Raw $ConfigPath | ConvertFrom-Json).private_key) { throw 'Use new versioned paths for BOTH certificate and key; never overwrite live material.' }
$WasRunning = (Get-Service HOnYBackend).Status -eq 'Running'
# A stopped service is permitted so a renewed certificate can recover expiry.
# If activation fails, restore its previous stopped/running state honestly.
Push-Location $Root
try {
    $Config | ConvertTo-Json | Set-Content -Encoding UTF8 $Candidate
    & $Python -I "$PSScriptRoot\desktop_admin_host.py" certificate --config $Candidate
    if ($LASTEXITCODE -ne 0) { throw 'Candidate certificate validation failed; live config unchanged.' }
    Set-HOnYAccess -Path $Config.private_key -ServiceRights Read
    Set-HOnYAccess -Path $Candidate -ServiceRights Read -UsersRead
    try {
        if ($WasRunning) { Stop-Service HOnYBackend -ErrorAction Stop }
        (Get-Service HOnYBackend).WaitForStatus('Stopped', [TimeSpan]::FromSeconds(60))
        [IO.File]::Replace($Candidate, $ConfigPath, $Previous)
        Start-Service HOnYBackend
        & $Python -I "$PSScriptRoot\desktop_admin_host.py" certificate --health --wait-seconds 30
        if ($LASTEXITCODE -ne 0) { throw 'Post-restart certificate health failed.' }
    } catch {
        # Never replace configuration while a listener might still be running.
        # If stopping fails, preserve BOTH configs for explicit recovery, do
        # not claim rollback succeeded and do not lose the failure category.
        try {
            Stop-Service HOnYBackend -ErrorAction Stop
            (Get-Service HOnYBackend).WaitForStatus('Stopped', [TimeSpan]::FromSeconds(60))
        } catch {
            throw 'Activation failed; rollback could not establish a stopped service. Candidate/live and previous configurations preserved. Explicit recovery required; rollback NOT verified.'
        }
        if (Test-Path $Previous) { [IO.File]::Replace($Previous, $ConfigPath, $null) }
        if ($WasRunning) {
            try {
                Start-Service HOnYBackend -ErrorAction Stop
                & $Python -I "$PSScriptRoot\desktop_admin_host.py" certificate --health --wait-seconds 30
                if ($LASTEXITCODE -ne 0) { throw 'health failed' }
            } catch {
                throw 'Activation failed; previous configuration restored but previous service health is unverified. Preserve certificate files and escalate.'
            }
        }
        throw 'New certificate activation failed; previous configuration restored. Verify service health.'
    }
    # Cleanup is outside activation rollback: an undeletable recovery copy
    # must never roll a healthy new certificate back. Preserve and warn.
    if (Test-Path $Previous) {
        try { Remove-Item -LiteralPath $Previous -ErrorAction Stop }
        catch { Write-Warning 'New certificate is healthy; recovery copy retained for manual review.' }
    }
} finally {
    if (Test-Path $Candidate) { Remove-Item -LiteralPath $Candidate }
    Pop-Location
}
