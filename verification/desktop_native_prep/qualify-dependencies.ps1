# NEW source-only native dependency qualification. Never runs an installer.
# This script is for a disposable CI runner, not for hotel deployment.
param([Parameter(Mandatory=$true)][string]$RepoRoot)
$ErrorActionPreference = 'Stop'
$Here = $PSScriptRoot
$RepoRoot = (Resolve-Path -LiteralPath $RepoRoot).Path
$Temp = [IO.Path]::GetFullPath($env:RUNNER_TEMP)
if (-not $env:GITHUB_ACTIONS -or $env:RUNNER_OS -ne 'Windows' -or -not $Temp) {
    throw 'Only a disposable Windows Actions runner is supported by this preparation script.'
}
$Work = Join-Path $Temp 'hony-desktop-native-prep'
if (Test-Path -LiteralPath $Work) { throw 'Refusing an existing preparation root.' }
New-Item -ItemType Directory -Path $Work | Out-Null
$Python = (Get-Command python.exe -ErrorAction Stop).Source
& $Python -c "import sys,struct; assert sys.version_info[:2]==(3,11) and struct.calcsize('P')==8"
if ($LASTEXITCODE -ne 0) { throw 'Python target mismatch.' }
# Parse, never invoke, the candidate's administrative scripts.
$Files = @(Get-ChildItem -LiteralPath (Join-Path $Here 'scripts\windows') -Filter '*.ps1' -File)
if ($Files.Count -ne 9) { throw 'Expected exactly nine recovered administrative PowerShell source files.' }
foreach ($File in $Files) {
    $Tokens = $null
    $ParseErrors = $null
    [void][Management.Automation.Language.Parser]::ParseFile($File.FullName,[ref]$Tokens,[ref]$ParseErrors)
    if ($ParseErrors.Count -ne 0) { throw ('PowerShell parse failed: ' + $File.Name) }
    Write-Output ('PS_PARSE_PASS ' + $File.Name)
}
$Venv = Join-Path $Work 'venv'
& $Python -m venv $Venv
if ($LASTEXITCODE -ne 0) { throw 'Fresh candidate venv creation failed.' }
$VenvPython = Join-Path $Venv 'Scripts\python.exe'
# Build-tool installation must be separately hash-pinned before this preparation
# script can be executed. Do not implicitly resolve build dependencies.
$BuildPins = Join-Path $Here 'build-tools-HASHED.txt'
if (-not (Test-Path -LiteralPath $BuildPins -PathType Leaf)) {
    throw 'Preparation incomplete: separately reviewed hash-pinned build tools are required.'
}
& $VenvPython -m pip install --disable-pip-version-check --no-deps --require-hashes -r $BuildPins
if ($LASTEXITCODE -ne 0) { throw 'Pinned build tools failed.' }
& $VenvPython -m pip install --disable-pip-version-check --no-deps --require-hashes --no-build-isolation -r (Join-Path $Here 'requirements-native-SDIST-NEW-CANDIDATE.txt')
if ($LASTEXITCODE -ne 0) { throw 'Pinned native dependency installation failed.' }
& $VenvPython -I (Join-Path $Here 'dependency_smoke.py')
if ($LASTEXITCODE -ne 0) { throw 'Native dependency imports failed.' }
Write-Output 'DEPENDENCY_IMPORTS_PASS APP_SERVICE_UI_ACCEPTANCE_NOT_ESTABLISHED'
# ISCC compile stays a separate preparation gate. Do not silently download a new
# compiler, fabricate stage inputs, or invoke the compiled installer here.

# Actual hidden native root hooks only, no remote start or app configuration.
& $VenvPython -I (Join-Path $Here 'native_root_hooks.py') (Join-Path $Here 'desktop_webview_guard.RECOVERED-EXACT.py') (Join-Path $env:RUNNER_TEMP 'hony-hidden-root-hook-smoke')
if ($LASTEXITCODE -ne 0) { throw 'Hidden native root-hook smoke failed; no interactive acceptance.' }

# Native stager uses committed public source and explicit never-run fixture bytes.
& $VenvPython -I (Join-Path $Here 'stage_fixture_smoke.py')
if ($LASTEXITCODE -ne 0) { throw 'Native stage/verify/compile fixture refused.' }

# Real pinned wheel closure into a second fresh venv, offline at install.
& $VenvPython -I (Join-Path $Here 'offline_wheel_closure.py')
if ($LASTEXITCODE -ne 0) { throw 'Actual offline wheel closure qualification refused.' }

# Unique disposable SCM adapter, synthetic worker only, no app/TLS/DB config.
& $VenvPython -I (Join-Path $Here 'scm_adapter_smoke.py')
if ($LASTEXITCODE -ne 0) { throw 'Native isolated SCM adapter qualification refused.' }
