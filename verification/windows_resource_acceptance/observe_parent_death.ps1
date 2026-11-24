# Source-only observer component. Fixture/profile owner must invoke it inside
# existing synthetic harness try/finally. NO runtime effect has been executed.
param([Parameter(Mandatory=$true)][string]$ProbeExe,[Parameter(Mandatory=$true)][string]$Profile,[Parameter(Mandatory=$true)][string]$Root)
$ErrorActionPreference='Stop'
if($env:GITHUB_ACTIONS -ne 'true' -or -not $env:RUNNER_TEMP){throw 'Disposable runner only'}
$si=[Diagnostics.ProcessStartInfo]::new()
$si.FileName=(Resolve-Path $ProbeExe).Path
$si.ArgumentList.Add($Profile);$si.ArgumentList.Add($Root)
$si.UseShellExecute=$false;$si.RedirectStandardOutput=$true;$si.RedirectStandardError=$true
$si.Environment['HONY_SYNTHETIC_PARK']='1'
$p=[Diagnostics.Process]::new();$p.StartInfo=$si
$child=$null;$started=$false
try{
 if(-not $p.Start()){throw 'Supervisor start failed'};$started=$true
 $errors=$p.StandardError.ReadToEndAsync()
 $deadline=[datetime]::UtcNow.AddSeconds(10)
 $ready=$null
 $lineTask=$p.StandardOutput.ReadLineAsync()
 while([datetime]::UtcNow -lt $deadline){
  if(-not $lineTask.Wait(1000)){continue}
  $line=$lineTask.Result
  if($null -eq $line){break}
  $lineTask=$p.StandardOutput.ReadLineAsync()
  if($line -match '^PARKED_CHILD pid=(\d+) created=(\d+)$'){$ready=@($Matches[1],$Matches[2]);break}
 }
 if($null -eq $ready){throw 'Parked child identity unavailable'}
 $child=[Diagnostics.Process]::GetProcessById([int]$ready[0])
 # Force a stable kernel handle before killing supervisor. Creation time rejects PID reuse.
 $null=$child.Handle
 if([uint64]$child.StartTime.ToUniversalTime().ToFileTimeUtc() -ne [uint64]$ready[1]){throw 'Child identity mismatch'}
 $p.Kill($false)
 if(-not $p.WaitForExit(3000)){throw 'Supervisor did not exit'}
 if(-not $child.WaitForExit(3000)){throw 'Kill-on-close child survival detected'}
 Write-Output 'Forced supervisor death: exact pre-opened child identity exited within bound'
}finally{
 $cleanupProblems=[Collections.Generic.List[string]]::new()
 if($started){
  try{if(-not $p.HasExited){$p.Kill($false)};if(-not $p.WaitForExit(3000)){$cleanupProblems.Add('Supervisor termination unverified')}}catch{$cleanupProblems.Add('Supervisor cleanup failed: '+$_.Exception.Message)}
 }
 if($null -ne $child){
  try{if(-not $child.HasExited){$child.Kill($false)};if(-not $child.WaitForExit(3000)){$cleanupProblems.Add('Held child termination unverified')}}catch{$cleanupProblems.Add('Held child cleanup failed: '+$_.Exception.Message)}
  $child.Dispose()
 }elseif($started){$cleanupProblems.Add('No verified held child identity; descendant cleanup unverified')}
 $p.Dispose()
 if($cleanupProblems.Count){throw ('Observer cleanup refusal: '+($cleanupProblems -join '; '))}
 Write-Output 'Observer checked supervisor and held-child termination'
}
