# Option B laptop check. SINGLE read-only entry point. PROTOTYPE. NOT executed on Windows or anywhere by its author.
#
# What it does: collects facts, redacts them, hands them to requirements_detect.py, prints a short copyable summary.
# What it never does: install, download, create, edit, delete, start/stop anything; change PATH, ACL, registry, services,
#   firewall, packages or any policy; connect to or read any database; read data folders, .env, or secrets; open ports.
# It writes NO file. Nothing is saved. Copy the printed summary from the console window if you want to share it.
#
# What it DOES execute (read this before running):
#   1. Python only: python.exe files that are (a) listed by the Python registry keys (PEP 514, PythonCore\*) or sit in
#      fixed install folders, (b) named exactly python.exe, (c) under Program Files, Program Files (x86), %LOCALAPPDATA%\Programs
#      or <SystemDrive>\Python*  (a path ALLOWLIST, not a trust decision: %LOCALAPPDATA%\Programs is user-writable), and (d) listed by the registry with Version 3.11. Fixed-folder finds have no registry version, so they are only listed, never run (UNKNOWN). A PythonCore key lacking a Version value is the same: a real 3.11 then reads UNKNOWN (fails safe). Each is run once as: python.exe -I -B -c "<print facts>".
#      -I ignores the current folder and user environment; -B writes no files. Other minors are NOT executed.
#   2. After that, ONE of the python.exe files probed in step 1 (it ran, final 3.x release) runs requirements_detect.py from this
#      folder to print the table. That .py file is NOT hashed or signed; review it with this script. The chosen interpreter is
#      named in the output. If none was probed, or the run fails, a plain summary is printed instead.
#   No database program and no unknown program is ever executed. The Microsoft Store alias is never run; this also means a real
#   Microsoft Store Python is not executed (it is reported as found-not-run).
#   The working folder is set to the Windows folder first, so nothing is picked up from where it was launched.
#
# Process-scope note: the double-click wrapper (check_laptop.bat) starts PowerShell with -ExecutionPolicy Bypass. That
# applies to that one PowerShell process only and is not saved anywhere. It lets this unsigned script run once. A policy
# set by an administrator (Group Policy) can still block it; then the script simply does not run, and nothing is changed.
# Review this file before running it.
param(
  [string]$Root = "",
  [switch]$ShowFacts   # also print the redacted raw facts (console only)
)
$ErrorActionPreference = "Continue"
Set-Location -LiteralPath $env:SystemRoot
if ([string]::IsNullOrWhiteSpace($Root)) { $Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path }
$OutputEncoding = New-Object System.Text.UTF8Encoding($false)   # this session only

function Redact([string]$s) {
  if ([string]::IsNullOrEmpty($s)) { return $s }
  # Path-based only: any \Users\<name> segment, plus the account and computer name when they stand alone as a whole
  # path segment. Names inside free text are NOT redacted, so short names like "admin" are not mangled elsewhere.
  $s = $s -replace '(?i)([a-z]:\\users\\)[^\\]+', '$1<USER>'
  foreach ($n in @($env:USERNAME, $env:COMPUTERNAME)) { if ($n) { $s = $s -replace ('(?i)(?<=[\\/])' + [regex]::Escape($n) + '(?=[\\/]|$)'), '<REDACTED>' } }
  return $s
}

$facts = [ordered]@{ collected_at = (Get-Date).ToString("s") }

# ---------- OS ----------
try {
  $os = Get-CimInstance Win32_OperatingSystem
  $facts.os = [ordered]@{ caption = $os.Caption; version = $os.Version; arch = $os.OSArchitecture; is64bit_os = [Environment]::Is64BitOperatingSystem }
} catch { $facts.os = $null }

# ---------- Python: registry (PEP 514) and fixed folders only. No where.exe, no PATH, no current folder. ----------
$trustedRoots = @($env:ProgramFiles, ${env:ProgramFiles(x86)}, (Join-Path $env:LOCALAPPDATA "Programs"), ($env:SystemDrive + "\")) | Where-Object { $_ }
function Test-TrustedPython([string]$p) {
  if ((Split-Path -Leaf $p) -ne "python.exe") { return $false }
  if ($p -like "*\WindowsApps\*") { return $false }
  foreach ($r in $trustedRoots) {
    $rr = $r.TrimEnd('\') + '\'
    if ($p.StartsWith($rr, [StringComparison]::OrdinalIgnoreCase)) {
      if ($rr -eq (($env:SystemDrive + "\"))) { return ($p -match '(?i)^[a-z]:\\Python[0-9]+\\python\.exe$') }
      return $true
    }
  }
  return $false
}
$found = @{}
$regRoots = @("HKCU:\Software\Python\PythonCore", "HKLM:\Software\Python\PythonCore", "HKLM:\Software\WOW6432Node\Python\PythonCore")
$collectErr = @()
foreach ($rk in $regRoots) {
  if (-not (Test-Path -LiteralPath $rk)) { continue }
  try {
    foreach ($sub in (Get-ChildItem -LiteralPath $rk -ErrorAction Stop)) {
      $tag = Split-Path -Leaf $sub.Name
      $verKey = Get-ItemProperty -LiteralPath $sub.PSPath -ErrorAction SilentlyContinue
      $ip = Get-ItemProperty -LiteralPath ($sub.PSPath + "\InstallPath") -ErrorAction SilentlyContinue
      if (-not $ip) { continue }
      $exe = $ip.ExecutablePath
      if (-not $exe -and $ip.'(default)') { $exe = Join-Path $ip.'(default)' "python.exe" }
      if ($exe) { $found[$exe.ToLowerInvariant()] = [pscustomobject]@{ path = $exe; source = "registry:" + $tag; static_version = $verKey.Version; static_sysversion = $verKey.SysVersion; static_arch = $verKey.SysArchitecture } }
    }
  } catch { $collectErr += "registry read failed" }
}
foreach ($pat in @((Join-Path $env:ProgramFiles "Python3*\python.exe"), (Join-Path $env:LOCALAPPDATA "Programs\Python\Python3*\python.exe"), ($env:SystemDrive + "\Python3*\python.exe"))) {
  foreach ($f in (Get-ChildItem -Path $pat -ErrorAction SilentlyContinue)) {
    if (-not $found.ContainsKey($f.FullName.ToLowerInvariant())) { $found[$f.FullName.ToLowerInvariant()] = [pscustomobject]@{ path = $f.FullName; source = "fixed-folder"; static_version = $null; static_sysversion = $null; static_arch = $null } }
  }
}
$storeAlias = Join-Path $env:LOCALAPPDATA "Microsoft\WindowsApps\python.exe"
$cands = @()
$script:runnerPath = $null
$probe = "import sys,struct,json,importlib.util as u;print(json.dumps({'version':sys.version.split()[0],'releaselevel':sys.version_info.releaselevel,'executable':sys.executable,'bits':struct.calcsize('P')*8,'platform':sys.platform,'venv':u.find_spec('venv') is not None,'ensurepip':u.find_spec('ensurepip') is not None}))"
foreach ($c in $found.Values) {
  $trusted = Test-TrustedPython $c.path
  $exists = Test-Path -LiteralPath $c.path
  $sv = if ($c.static_version) { [string]$c.static_version } elseif ($c.static_sysversion) { [string]$c.static_sysversion } else { $null }
  $e = [ordered]@{ path = (Redact $c.path); source = $c.source; store_alias = ($c.path -like "*\WindowsApps\*"); trusted_path = $trusted; exists = $exists
                   static_version = $sv; static_arch = $c.static_arch; ran = $false }
  # Execute only trusted, existing candidates whose REGISTRY entry says 3.11. Fixed-folder finds carry no registry
  # version, so they are listed but never run (they stay UNKNOWN).
  if ($trusted -and $exists -and $sv -match '^3\.11(\.|$)') {
    try {
      $o = & $c.path -I -B -c $probe 2>$null
      $r = $o | ConvertFrom-Json
      $e.ran = $true; $e.version = $r.version; $e.releaselevel = $r.releaselevel; $e.bits = $r.bits
      $e.venv = [bool]$r.venv; $e.ensurepip = [bool]$r.ensurepip; $e.executable = (Redact $r.executable)
      $e._raw_path = $c.path
      if (-not $script:runnerPath -and [string]$r.version -match '^3\.' -and $r.releaselevel -eq 'final') { $script:runnerPath = $c.path }
    } catch { $e.error = "run failed" }
  }
  $cands += [pscustomobject]$e
}
if (Test-Path -LiteralPath $storeAlias) { $cands += [pscustomobject][ordered]@{ path = (Redact $storeAlias); source = "store-alias"; store_alias = $true; trusted_path = $false; exists = $true; static_version = $null; static_arch = $null; ran = $false } }
$facts.python_candidates = @($cands | ForEach-Object { $_ | Select-Object * -ExcludeProperty _raw_path })

# ---------- Database programs: names/versions only. No database binary is run, no connection, no data folder read. ----------
# Matched on service/program NAME (not publisher). Client drivers and tools are listed separately and never counted.
$dbPattern = '(?i)postgres|mysql|\bmaria(db)?\b|mssql|\bsql ?server\b|sqlexpress|sqlserver|\bfirebird\b|\boracle (database|xe)\b|\bmongodb\b|\bredis\b|\bdb2\b|\bcockroach(db)?\b|\binfluxdb\b|\bcouchdb\b|\bcassandra\b|\bsybase\b|\bingres\b|\binterbase\b'
$driverPattern = '(?i)\b(odbc|ole ?db|jdbc|driver|connector|client|command line utilities|management studio|data tools|native client)\b'
$db = [ordered]@{ collection_ok = $true; services = @(); programs = @(); driver_entries = @(); errors = @() }
try {
  $svcs = @()
  foreach ($s in (Get-CimInstance Win32_Service -ErrorAction Stop)) {
    $exe = $null
    if ($s.PathName -match '^\s*"?(.+?\.exe)"?(\s|$)') { $exe = $Matches[1] }   # exe path only; arguments (may hold secrets) are dropped
    if (("$($s.Name) $($s.DisplayName) $exe") -match $dbPattern) {
      $vi = $null
      if ($exe -and (Test-Path -LiteralPath $exe)) { try { $vi = (Get-Item -LiteralPath $exe).VersionInfo } catch { } }   # reads file properties, does not run the file
      $svcs += [pscustomobject][ordered]@{ name = $s.Name; display_name = $s.DisplayName; state = $s.State; start_mode = $s.StartMode
        exe_name = $(if ($exe) { Split-Path -Leaf $exe } else { $null }); image_exe = (Redact $exe)
        file_product_name = $(if ($vi) { $vi.ProductName } else { $null }); file_product_version = $(if ($vi) { $vi.ProductVersion } else { $null }) }
    }
  }
  $db.services = @($svcs)
} catch { $db.collection_ok = $false; $db.errors += "service list failed" }
try {
  $progs = @(); $drv = @()
  foreach ($k in @("HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*", "HKLM:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*", "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*")) {
    foreach ($p in (Get-ItemProperty -Path $k -ErrorAction SilentlyContinue)) {
      if ($p.DisplayName -and ($p.DisplayName -match $dbPattern) -and ($p.DisplayName -match $driverPattern)) { $drv += [string]$p.DisplayName }
      elseif ($p.DisplayName -and ($p.DisplayName -match $dbPattern)) {
        $progs += [pscustomobject][ordered]@{ display_name = $p.DisplayName; display_version = $p.DisplayVersion; publisher = $p.Publisher }
      }
    }
  }
  $db.programs = @($progs); $db.driver_entries = @($drv)
} catch { $db.collection_ok = $false; $db.errors += "installed-program list failed" }
$facts.database = [pscustomobject]$db

# ---------- Wheels folder, tied to wheels.sha256 and the lock file ----------
$wd = Join-Path $Root "wheels"; $shaFile = Join-Path $Root "wheels.sha256"
$lockFile = Join-Path $Root "option_b\requirements-win-cp311.lock.txt"
$w = [ordered]@{ folder_exists = (Test-Path -LiteralPath $wd); sha_file_exists = (Test-Path -LiteralPath $shaFile); lock_found = (Test-Path -LiteralPath $lockFile)
                 file_count = 0; sha_line_count = 0; mismatches = @(); missing = @(); unlisted = @(); not_in_lock = @(); sha_file_hash = $null }
if ($w.folder_exists -and $w.sha_file_exists -and $w.lock_found) {
  $lockHashes = @{}
  foreach ($m in (Select-String -LiteralPath $lockFile -Pattern 'sha256:([0-9a-fA-F]{64})' -AllMatches)) { foreach ($x in $m.Matches) { $lockHashes[$x.Groups[1].Value.ToLower()] = 1 } }
  $files = @(Get-ChildItem -LiteralPath $wd -File)
  $w.file_count = $files.Count
  $w.sha_file_hash = (Get-FileHash -LiteralPath $shaFile -Algorithm SHA256).Hash.ToLower()
  $listed = @{}
  foreach ($line in (Get-Content -LiteralPath $shaFile)) {
    if ($line -match '^([0-9a-fA-F]{64})\s+\*?(.+)$') {
      $w.sha_line_count++
      $listed[$Matches[2]] = $Matches[1].ToLower()
    }
  }
  foreach ($name in $listed.Keys) { if (-not (Test-Path -LiteralPath (Join-Path $wd $name))) { $w.missing += $name } }
  foreach ($f in $files) {
    $h = (Get-FileHash -LiteralPath $f.FullName -Algorithm SHA256).Hash.ToLower()
    if (-not $listed.ContainsKey($f.Name)) { $w.unlisted += $f.Name }
    elseif ($listed[$f.Name] -ne $h) { $w.mismatches += $f.Name }
    if (-not $lockHashes.ContainsKey($h)) { $w.not_in_lock += $f.Name }
  }
} elseif ($w.folder_exists) { $w.file_count = @(Get-ChildItem -LiteralPath $wd -File).Count }
foreach ($k in @("mismatches", "missing", "unlisted", "not_in_lock")) { $w[$k] = @($w[$k]) }
$facts.wheels = [pscustomobject]$w

# ---------- Source tree: existence, and the archive hash if an archive is present. The tree itself is NOT verified. ----------
$arch = @(Get-ChildItem -LiteralPath (Join-Path $Root "archive") -Filter "*.gz" -File -ErrorAction SilentlyContinue)
$facts.source = [pscustomobject][ordered]@{ main_py_exists = (Test-Path -LiteralPath (Join-Path $Root "app\app\main.py"))
  archive_count = $arch.Count; archive_sha256 = $(if ($arch.Count -eq 1) { (Get-FileHash -LiteralPath $arch[0].FullName -Algorithm SHA256).Hash.ToLower() } else { $null }) }
$facts.collection_errors = @($collectErr)

# ---------- Output ----------
$json = $facts | ConvertTo-Json -Depth 6 -Compress
if ($ShowFacts) { Write-Host "REDACTED FACTS (console only):"; Write-Host $json }
$clf = Join-Path $PSScriptRoot "requirements_detect.py"
$ok = $false
if ($script:runnerPath -and (Test-Path -LiteralPath $clf)) {
  Write-Host ("Table produced by: " + (Redact $script:runnerPath) + " (a probed candidate that ran as a final 3.x release). requirements_detect.py next to this script runs UNHASHED.")
  $out = @($json | & $script:runnerPath -I -B $clf --facts - --plan 2>&1)
  $out | ForEach-Object { Write-Host $_ }
  # Success = the classifier printed its summary block. Exit code 1 also means "something not compatible" and a Python crash, so it is not used.
  if (($out -join "`n") -match 'COPYABLE SUMMARY') { $ok = $true }
}
Write-Host "NOTE: program and service names are shown as found and are NOT redacted. Check them before sharing."
if (-not $ok) {
  Write-Host "No probed Python could run the classifier (or it failed). Plain summary instead (nothing was changed):"
  Write-Host ("windows: " + $(if ($facts.os) { $facts.os.caption + " " + $facts.os.version + " 64-bit=" + $facts.os.is64bit_os } else { "unknown" }))
  Write-Host ("python candidates seen: " + @($cands).Count + $(if (@($cands).Count -eq 0) { " (missing)" } else { " (see -ShowFacts)" }))
  Write-Host ("database-like services: " + @($db.services).Count + "; installed-program entries: " + @($db.programs).Count + "; collection_ok=" + $db.collection_ok)
  Write-Host ("wheels folder: " + $w.folder_exists + "; source main.py: " + $facts.source.main_py_exists)
}
