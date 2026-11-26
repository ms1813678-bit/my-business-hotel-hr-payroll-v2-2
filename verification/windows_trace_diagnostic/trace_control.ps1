# Disposable CI only. Built-in Windows ETW through logman. No download, no driver or service install,
# no ACL/SACL/audit/registry/system-setting change, no debugger. Starts ONE NT Kernel Logger session
# and refuses before launch if another kernel session exists (never stops a session it did not start).
$script:TraceSession='NT Kernel Logger'
$script:TraceKeywords='(process,img,fileio,fileiocompletion)'
$script:TraceMaxMb=48
$script:TraceFile=''

function Assert-TraceAllowed {
  if($env:GITHUB_ACTIONS -ne 'true'){throw 'Disposable CI only'}
  $principal=[Security.Principal.WindowsPrincipal]::new([Security.Principal.WindowsIdentity]::GetCurrent())
  if(-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)){throw 'Trace refused before launch: runner user is not elevated'}
  $listing=(& logman query -ets 2>&1|Out-String)
  if($LASTEXITCODE -ne 0){throw 'Trace refused before launch: session query failed'}
  if($listing -match [regex]::Escape($script:TraceSession)){throw 'Trace refused before launch: a kernel session already exists'}
  Write-Output 'trace_precheck elevated=1 no_existing_kernel_session=1'
}

function Start-KernelTrace([string]$File){
  $script:TraceFile=$File
  & logman start $script:TraceSession -p 'Windows Kernel Trace' $script:TraceKeywords -o $File -ets -nb 64 256 -bs 1024 -max $script:TraceMaxMb
  if($LASTEXITCODE -ne 0){throw 'Trace refused before launch: logman start failed'}
  Write-Output ('trace_started session='+$script:TraceSession+' keywords='+$script:TraceKeywords+' max_mb='+$script:TraceMaxMb+' buffers=64-256x1024KB')
}

function Stop-KernelTrace {
  & logman stop $script:TraceSession -ets
  $stopExit=$LASTEXITCODE
  $listing=(& logman query -ets 2>&1|Out-String)
  $gone=($LASTEXITCODE -eq 0 -and $listing -notmatch [regex]::Escape($script:TraceSession))
  $etlBytes=-1
  try{if($script:TraceFile -and (Test-Path -LiteralPath $script:TraceFile)){$etlBytes=(Get-Item -LiteralPath $script:TraceFile).Length}}catch{}
  Write-Output ('trace_stopped stop_exit='+$stopExit+' session_gone='+[int]$gone+' etl_bytes='+$etlBytes)
  if(-not $gone){throw 'Trace session still present after stop; retained'}
}
