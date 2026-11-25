# Candidate only. Run after separate approval on the target machine, never during source preparation.
# No DNS update, certificate issuance, firewall opening or database migration is done here.
param([Parameter(Mandatory=$true)][string]$ConfigPath)
$ErrorActionPreference = 'Stop'
$Principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $Principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { throw 'Administrator required.' }
$Root = (Resolve-Path "$PSScriptRoot\..\..").Path
. "$PSScriptRoot\desktop-python-preflight.ps1"
Assert-HOnYMachinePython -Root $Root
$ProgramFilesRoot = [IO.Path]::GetFullPath($env:ProgramFiles).TrimEnd('\') + '\'
if (-not $Root.StartsWith($ProgramFilesRoot, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Service code must be staged under administrator-controlled Program Files, not LocalAppData.'
}
$Runtime = Join-Path $env:ProgramData 'HOnY'
$TargetConfig = Join-Path $Runtime 'desktop.json'
$Python = Join-Path $Root '.venv\Scripts\python.exe'
if (Get-Service HOnYBackend -ErrorAction SilentlyContinue) { throw 'Service already exists. Use reviewed upgrade procedure.' }
if (Test-Path $TargetConfig) { throw 'Existing runtime configuration is preserved. Review it before proceeding.' }
$Config = Get-Content -Raw -LiteralPath $ConfigPath | ConvertFrom-Json
if ([IO.Path]::GetFullPath($Config.project_root) -ne $Root) { throw 'Config project_root must match this application.' }
# All installed service code/interpreters must be administrator-controlled.
. "$PSScriptRoot\desktop-acl.ps1"
$EnvPath = Join-Path $Root '.env'
$VarPath = Join-Path $Root 'var'
# Protect secrets before making any code readable to desktop users.
Set-HOnYAccess -Path $EnvPath -ServiceRights Read
Set-HOnYAccess -Path $Config.private_key -ServiceRights Read
New-Item -ItemType Directory -Path $VarPath -Force | Out-Null
Set-HOnYAccess -Path $VarPath -ServiceRights Modify -Recurse
Set-HOnYAccess -Path $Root -ServiceRights ReadAndExecute -UsersRead -Recurse -ExcludePaths @($EnvPath, $Config.private_key, $VarPath)
New-Item -ItemType Directory -Path $Runtime -Force | Out-Null
Set-HOnYAccess -Path $Runtime -ServiceRights ReadAndExecute -UsersRead
Copy-Item -LiteralPath $ConfigPath -Destination $TargetConfig
Set-HOnYAccess -Path $TargetConfig -ServiceRights Read -UsersRead
$RegistrationAttempted = $false
# Runtime paths, root and TLS key pair validated before registration.
Push-Location $Root
try {
    & $Python -I "$PSScriptRoot\desktop_admin_host.py" certificate
    if ($LASTEXITCODE -ne 0) { throw 'Desktop configuration/TLS validation failed.' }
    # NetworkService, never LocalSystem. The account needs only existing app runtime
    # directories (var/data as configured) writable and DB loopback connectivity.
    # The operator must provision those reviewed paths before start.
    $RegistrationAttempted = $true
    & $Python -I "$PSScriptRoot\desktop_admin_host.py" service --startup delayed --username 'NT AUTHORITY\NetworkService' install
    if ($LASTEXITCODE -ne 0) { throw 'Service registration failed. Configuration kept for diagnosis.' }
    & sc.exe failureflag HOnYBackend 1
    if ($LASTEXITCODE -ne 0) { throw 'Service failure flag configuration failed. Service not started.' }
    & sc.exe failure HOnYBackend reset= 86400 actions= restart/10000/restart/30000/restart/60000
    if ($LASTEXITCODE -ne 0) { throw 'Service recovery configuration failed. Service not started.' }
} catch {
    # Only clean up the service/config this invocation created. Never remove
    # a pre-existing service (rejected before writes), data, keys or ACLs.
    $CanArchiveConfig = $true
    if ($RegistrationAttempted) {
        $PartialService = Get-Service HOnYBackend -ErrorAction SilentlyContinue
        if ($PartialService) {
            if ($PartialService.Status -ne 'Stopped') {
                $CanArchiveConfig = $false
            } else {
                & $Python -I "$PSScriptRoot\desktop_admin_host.py" service remove
                if ($LASTEXITCODE -ne 0) { $CanArchiveConfig = $false }
            }
        }
    }
    if ($CanArchiveConfig -and (Test-Path -LiteralPath $TargetConfig)) {
        $Stamp = [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfffZ')
        Move-Item -LiteralPath $TargetConfig -Destination "$TargetConfig.failed-install-$Stamp" -ErrorAction Stop
        throw 'Install failed; newly created stopped service removed when present and configuration archived for review. Restrictive ACLs remain. Inspect before retry.'
    }
    throw 'Install failed; partial service/config preserved because cleanup could not be verified. No retry or deployment success claimed. Inspect service state explicitly.'
} finally { Pop-Location }
Write-Output 'Registered but NOT started. Review certificate/key/.env/runtime ACLs and explicit LAN firewall policy before start.'
