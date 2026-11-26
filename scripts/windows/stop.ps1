# Stops the running M.Y. Business Hotel HR program (the launcher process only). Changes no data.
$ErrorActionPreference = "Stop"
$procs = Get-CimInstance Win32_Process -Filter "Name = 'pythonw.exe'" |
    Where-Object { $_.CommandLine -like "*app.launcher*" -and $_.ProcessId -ne $PID }
foreach ($p in $procs) { Stop-Process -Id $p.ProcessId -Force }
exit 0
