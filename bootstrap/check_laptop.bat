@echo off
rem PROPOSED double-click wrapper for detect_requirements.ps1. REVIEW ONLY: not run by its author.
rem -ExecutionPolicy Bypass here applies to this ONE PowerShell process only. It is not saved and changes no policy.
rem It lets this unsigned script run once. An administrator policy can still block it, and then nothing happens.
rem The script is read-only and writes no file. See the comments at the top of detect_requirements.ps1.
cd /d "%SystemRoot%"
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "%~dp0detect_requirements.ps1"
echo.
echo Program and service names above are shown as found and are NOT redacted: check them before sharing.
echo If you saw an error above, copy it. This check changes nothing and saves nothing. You can copy the text above.
pause
