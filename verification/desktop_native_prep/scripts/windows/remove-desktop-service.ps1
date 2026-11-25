param()
$ErrorActionPreference = 'Stop'
$Root = (Resolve-Path "$PSScriptRoot\..\..").Path
. "$PSScriptRoot\desktop-python-preflight.ps1"
Assert-HOnYMachinePython -Root $Root
$Python = Join-Path $Root '.venv\Scripts\python.exe'
$Service = Get-Service HOnYBackend -ErrorAction SilentlyContinue
if ($Service) {
    if ($Service.Status -ne 'Stopped') { Stop-Service HOnYBackend -ErrorAction Stop }
    $Service.WaitForStatus('Stopped', [TimeSpan]::FromSeconds(60))
    Push-Location $Root
    try {
        & $Python -I "$PSScriptRoot\desktop_admin_host.py" service remove
        if ($LASTEXITCODE -ne 0) { throw 'Service removal failed.' }
    } finally { Pop-Location }
}
# Never delete database, .env, certificates, renewal tasks, backups or reports.
Write-Output 'Service removed. Runtime files and any separately approved network/renewal policy are preserved.'
