param()
$ErrorActionPreference = 'Stop'
Set-Location (Resolve-Path "$PSScriptRoot\..\..")
$PythonW = Join-Path (Get-Location) '.venv\Scripts\pythonw.exe'
if (-not (Test-Path $PythonW)) { throw 'Install the optional desktop dependencies first.' }
# Window lifetime is intentionally unrelated to SCM service lifetime.
Start-Process -FilePath $PythonW -ArgumentList @('-I', ('"' + (Join-Path (Get-Location) 'scripts\windows\desktop_window_host.py') + '"')) -WorkingDirectory (Get-Location)
