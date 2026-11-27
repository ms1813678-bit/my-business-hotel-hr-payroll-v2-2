param(
    [string]$DailyTime = "02:00",
    [string]$WeeklyTime = "02:30",
    [string]$QuarterlyTime = "03:00"
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$Runner = Join-Path $ProjectRoot "scripts\backup\run_backup.py"

if (-not (Test-Path $Python)) {
    throw "Python virtual environment not found at $Python. Run the hotel setup script first."
}
if (-not (Test-Path $Runner)) {
    throw "Backup runner not found at $Runner."
}

# The tasks run as the Windows user who installs them, with limited rights: never SYSTEM, never elevated
# (Mo's WhatsApp reply "أ" at 2026-11-27 21:00, option A, wamid ...QTU2ODlFQTJBRjYzOEFCMkRBMjcxMjdFN0FGM0VCQkUA,
# relayed by the coordinator; earlier also his 17:29 reply agreeing with the recommendation). Editing these files in the user-owned program folder then gives no more
# power than that user already has. They run while that user is signed in (no stored password).
# Catch-up: Mo is not at the laptop at 02:00. A run missed because the laptop was off or nobody was signed in
# starts as soon as the laptop is on and the user is signed in (StartWhenAvailable). The laptop must be on and
# signed in at night for a backup at the planned hour. Battery rules are removed so a laptop on battery still runs.
$RunAsUser = [System.Security.Principal.WindowsIdentity]::GetCurrent()
if ($RunAsUser.IsSystem) { throw "Run this script as the hotel's normal Windows user, not as SYSTEM." }
$RunAsUser = $RunAsUser.Name

$Tasks = @(
    @{
        Name = "MY Business Hotel HR - Daily Backup"
        Schedule = @("/SC", "DAILY", "/ST", $DailyTime)
        Type = "daily"
    },
    @{
        Name = "MY Business Hotel HR - Weekly Backup"
        Schedule = @("/SC", "WEEKLY", "/D", "SUN", "/ST", $WeeklyTime)
        Type = "weekly"
    },
    @{
        Name = "MY Business Hotel HR - Quarterly Backup"
        Schedule = @("/SC", "MONTHLY", "/MO", "3", "/D", "1", "/ST", $QuarterlyTime)
        Type = "quarterly"
    }
)

foreach ($Task in $Tasks) {
    $TaskRun = '"' + $Python + '" "' + $Runner + '" --type ' + $Task.Type
    $Arguments = @(
        "/Create",
        "/TN", $Task.Name,
        "/TR", $TaskRun,
        "/RU", $RunAsUser,
        "/IT",
        "/RL", "LIMITED",
        "/F"
    ) + $Task.Schedule

    & schtasks.exe @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Could not create scheduled task: $($Task.Name)"
    }
    $Existing = Get-ScheduledTask -TaskName $Task.Name
    $Existing.Settings.StartWhenAvailable = $true
    $Existing.Settings.DisallowStartIfOnBatteries = $false
    $Existing.Settings.StopIfGoingOnBatteries = $false
    $Existing.Settings.ExecutionTimeLimit = "PT2H"
    Set-ScheduledTask -InputObject $Existing | Out-Null
    Write-Host "Installed scheduled task: $($Task.Name)"
}

Write-Host "Backup tasks installed for $RunAsUser with limited rights; they run while this user is signed in."
Write-Host "The laptop must be on and this user signed in at night. A missed backup runs when the laptop next starts and the user signs in."
Write-Host "No backup is ever deleted automatically; delete old backups manually from the backup folder."
Write-Host "Default schedule: daily $DailyTime, Sunday $WeeklyTime, first day of each quarter $QuarterlyTime."
Write-Host "Times can be changed by rerunning this script with -DailyTime, -WeeklyTime and -QuarterlyTime."
