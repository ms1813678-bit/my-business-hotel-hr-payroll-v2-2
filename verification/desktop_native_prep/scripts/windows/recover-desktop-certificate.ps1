# Explicit recovery only, never automatic: no cert issuance/DNS/token access.
# Select after inspecting desktop.json/.candidate/.previous and service state.
param(
    [Parameter(Mandatory=$true)][ValidateSet('RestorePrevious','KeepHealthyCurrent','ActivateCandidate')][string]$Choice,
    [switch]$Apply,
    [switch]$StartAfterRestore
)
$ErrorActionPreference = 'Stop'
$Root = (Resolve-Path "$PSScriptRoot\..\..").Path
. "$PSScriptRoot\desktop-python-preflight.ps1"
. "$PSScriptRoot\desktop-acl.ps1"
$Python = Join-Path $Root '.venv\Scripts\python.exe'
$ConfigPath = Join-Path $env:ProgramData 'HOnY\desktop.json'
$Previous = "$ConfigPath.previous"
$Candidate = "$ConfigPath.candidate"
$Stamp = [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfffZ')
if (-not $Apply) {
    Write-Output ('Live config present: ' + (Test-Path -LiteralPath $ConfigPath))
    Write-Output ('Candidate present: ' + (Test-Path -LiteralPath $Candidate))
    Write-Output ('Previous present: ' + (Test-Path -LiteralPath $Previous))
    $ObservedService = Get-Service HOnYBackend -ErrorAction SilentlyContinue
    Write-Output ('Service state: ' + $(if ($ObservedService) { $ObservedService.Status } else { 'not found' }))
    Write-Output 'Preview only. RestorePrevious and ActivateCandidate require service stopped; KeepHealthyCurrent requires strict trusted TLS health. Recovery files are archived, never silently deleted.'
    return
}
Assert-HOnYMachinePython -Root $Root
Push-Location $Root
try {
    if ($Choice -eq 'ActivateCandidate') {
        if (-not (Test-Path -LiteralPath $Candidate)) { throw 'Candidate configuration is absent.' }
        if (Test-Path -LiteralPath $Previous) { throw 'Previous copy also exists; inspect and choose a recovery path explicitly.' }
        if ((Get-Service HOnYBackend).Status -ne 'Stopped') { throw 'Candidate recovery requires an already stopped service.' }
        & $Python -I "$PSScriptRoot\desktop_admin_host.py" certificate --config $Candidate
        if ($LASTEXITCODE -ne 0) { throw 'Candidate certificate is not valid for activation; all files preserved.' }
        # Recheck immediately before swap. Pending SCM recovery remains a
        # Windows acceptance gate; the operator must disable/clear it first.
        if ((Get-Service HOnYBackend).Status -ne 'Stopped') { throw 'Service state changed; no replacement attempted.' }
        [IO.File]::Replace($Candidate, $ConfigPath, "$ConfigPath.failed-$Stamp")
        Set-HOnYAccess -Path $ConfigPath -ServiceRights Read -UsersRead
        if ($StartAfterRestore) {
            Start-Service HOnYBackend -ErrorAction Stop
            & $Python -I "$PSScriptRoot\desktop_admin_host.py" certificate --health --wait-seconds 30
            if ($LASTEXITCODE -ne 0) { throw 'Candidate selected; requested restart health unverified. Recovery copy preserved.' }
        }
    } elseif ($Choice -eq 'KeepHealthyCurrent') {
        & $Python -I "$PSScriptRoot\desktop_admin_host.py" certificate --health --wait-seconds 30
        if ($LASTEXITCODE -ne 0) { throw 'Current configuration health is not verified; recovery files preserved.' }
        # Healthy live config stays unchanged. Archive leftovers so a failed
        # cleanup does not silently block every future renewal attempt.
        foreach ($Path in @($Previous, $Candidate)) {
            if (Test-Path -LiteralPath $Path) {
                Move-Item -LiteralPath $Path -Destination "$Path.recovered-$Stamp" -ErrorAction Stop
            }
        }
    } else {
        if (-not (Test-Path -LiteralPath $Previous)) { throw 'Previous configuration is absent; nothing inferred.' }
        if ((Get-Service HOnYBackend).Status -ne 'Stopped') { throw 'Stop and verify the service before restoring configuration. Recovery never stops it implicitly.' }
        # Validate paths/key and protect old metadata, but expired previous
        # material may legitimately fail health. No TLS bypass or auto-start.
        & $Python -I "$PSScriptRoot\desktop_admin_host.py" certificate --config $Previous --structure-only
        if ($LASTEXITCODE -ne 0) { throw 'Previous configuration/key pair is not valid structurally; all files preserved.' }
        $RecoveryCopy = "$ConfigPath.failed-$Stamp"
        if ((Get-Service HOnYBackend).Status -ne 'Stopped') { throw 'Service state changed; no replacement attempted.' }
        [IO.File]::Replace($Previous, $ConfigPath, $RecoveryCopy)
        Set-HOnYAccess -Path $ConfigPath -ServiceRights Read -UsersRead
        if (Test-Path -LiteralPath $Candidate) {
            Move-Item -LiteralPath $Candidate -Destination "$Candidate.recovered-$Stamp" -ErrorAction Stop
        }
        if ($StartAfterRestore) {
            Start-Service HOnYBackend -ErrorAction Stop
            & $Python -I "$PSScriptRoot\desktop_admin_host.py" certificate --health --wait-seconds 30
            if ($LASTEXITCODE -ne 0) { throw 'Previous configuration restored; requested restart health is unverified. Recovery copies preserved.' }
        }
    }
    Write-Output 'Explicit recovery completed. Versioned certificates and recovery copies preserved.'
} finally { Pop-Location }
