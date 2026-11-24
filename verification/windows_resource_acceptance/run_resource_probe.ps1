param([Parameter(Mandatory=$true)][string]$ProbeExe,[switch]$FaultSweep,[switch]$ParentDeath)
$ErrorActionPreference='Stop'
if($FaultSweep -and $ParentDeath){throw 'Select one synthetic mode'}
if($env:GITHUB_ACTIONS -ne 'true' -or -not $env:RUNNER_TEMP){throw 'Disposable runner only'}
$exe=(Resolve-Path $ProbeExe).Path
$base=Join-Path $env:RUNNER_TEMP ('hony-resource-'+[guid]::NewGuid().ToString('N'))
if(Test-Path $base){throw 'Fresh root required'}
$profile='Hony.Resource.'+[guid]::NewGuid().ToString('N')
Add-Type @'
using System;using System.Runtime.InteropServices;
public static class ResourceProfile {
[DllImport("userenv.dll",CharSet=CharSet.Unicode)] public static extern int CreateAppContainerProfile(string n,string d,string s,IntPtr c,uint count,out IntPtr sid);
[DllImport("userenv.dll",CharSet=CharSet.Unicode)] public static extern int DeleteAppContainerProfile(string n);
[DllImport("advapi32.dll")]public static extern IntPtr FreeSid(IntPtr sid);
[DllImport("kernel32.dll",SetLastError=true)]static extern IntPtr OpenProcess(uint access,bool inherit,uint pid);
[DllImport("kernel32.dll",SetLastError=true)]static extern uint WaitForSingleObject(IntPtr h,uint ms);
[DllImport("kernel32.dll",SetLastError=true)]static extern bool CloseHandle(IntPtr h);
public static bool ConfirmNoLiveProcess(uint pid){
 IntPtr h=OpenProcess(0x00101000,false,pid);
 if(h==IntPtr.Zero){int e=Marshal.GetLastWin32Error();if(e==87)return true;throw new InvalidOperationException("Process identity unresolved: "+e);}
 uint state=WaitForSingleObject(h,0);bool closed=CloseHandle(h);
 if(!closed)throw new InvalidOperationException("Process query handle cleanup failed");
 if(state==0)return true;
 if(state==258)return false;
 throw new InvalidOperationException("Process wait state unresolved");
}
}
'@
$created=$false;$sid=[IntPtr]::Zero;$exeGuard=$null
try{
 New-Item -ItemType Directory $base|Out-Null
 $hr=[ResourceProfile]::CreateAppContainerProfile($profile,$profile,'Fixed synthetic resource probe',[IntPtr]::Zero,0,[ref]$sid)
 if($hr -ne 0){throw ('CreateProfile '+$hr)};$created=$true
 $sidText=([Security.Principal.SecurityIdentifier]::new($sid)).Value
 $runtime=Join-Path $base 'runtime';$private=Join-Path $base 'private';$scratch=Join-Path $base 'scratch'
 New-Item -ItemType Directory $runtime,$private,$scratch|Out-Null
 $copy=Join-Path $runtime 'probe.exe';Copy-Item $exe $copy
 [IO.File]::WriteAllText((Join-Path $runtime 'allowed.txt'),'HONY-SYNTHETIC-ALLOWED-READ-v1',[Text.Encoding]::ASCII)
 [IO.File]::WriteAllText((Join-Path $private 'canary.txt'),'HONY-SYNTHETIC-FORBIDDEN-v1',[Text.Encoding]::ASCII)
 $me=[Security.Principal.WindowsIdentity]::GetCurrent().User.Value
 foreach($p in @($base,$runtime,$private,$scratch)){
  & icacls.exe $p /inheritance:r /grant:r "*${me}:(OI)(CI)F" '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F'
  if($LASTEXITCODE -ne 0){throw 'Synthetic dir ACL failed'}
 }
 foreach($p in @($copy,(Join-Path $runtime 'allowed.txt'),(Join-Path $private 'canary.txt'))){
  & icacls.exe $p /inheritance:r /grant:r "*${me}:F" '*S-1-5-18:F' '*S-1-5-32-544:F'
  if($LASTEXITCODE -ne 0){throw 'Synthetic file ACL failed'}
 }
 & icacls.exe $base /grant "*${sidText}:RX";if($LASTEXITCODE -ne 0){throw 'Traverse ACL failed'}
 & icacls.exe $runtime /grant "*${sidText}:(OI)(CI)RX";if($LASTEXITCODE -ne 0){throw 'Runtime ACL failed'}
 foreach($p in @($copy,(Join-Path $runtime 'allowed.txt'))){& icacls.exe $p /grant "*${sidText}:RX";if($LASTEXITCODE -ne 0){throw 'Runtime file ACL failed'}}
 & icacls.exe $scratch /setintegritylevel '(OI)(CI)L';if($LASTEXITCODE -ne 0){throw 'Scratch integrity setup failed'}
 & icacls.exe $scratch /grant "*${sidText}:(OI)(CI)M";if($LASTEXITCODE -ne 0){throw 'Scratch ACL failed'}
 $exeGuard=[IO.File]::Open($copy,[IO.FileMode]::Open,[IO.FileAccess]::Read,[IO.FileShare]::Read)
 $copiedHash=(Get-FileHash $copy -Algorithm SHA256).Hash
 if($copiedHash -ne (Get-FileHash $exe -Algorithm SHA256).Hash){throw 'Executable copy provenance mismatch'}
 $before=(Get-FileHash (Join-Path $private 'canary.txt') -Algorithm SHA256).Hash
 if($FaultSweep){
  for($fault=1;$fault -le 20;$fault++){
   $si=[Diagnostics.ProcessStartInfo]::new();$si.FileName=$copy;$si.UseShellExecute=$false
   $si.RedirectStandardOutput=$true;$si.RedirectStandardError=$true;$si.ArgumentList.Add($profile);$si.ArgumentList.Add($base)
   $si.Environment['HONY_SYNTHETIC_FAULT']=[string]$fault
   $pr=[Diagnostics.Process]::new();$pr.StartInfo=$si;$faultStarted=$false;$et=$null
   try{
    if(-not $pr.Start()){throw 'Fault process failed to start'};$faultStarted=$true
    $ot=$pr.StandardOutput.ReadToEndAsync();$et=$pr.StandardError.ReadToEndAsync()
    if(-not $pr.WaitForExit(12000)){throw 'Fault subprocess timeout; cleanup verification follows'}
    $out=$ot.GetAwaiter().GetResult();$err=$et.GetAwaiter().GetResult()
    if($pr.ExitCode -eq 0 -or $err -notmatch ('INJECTED_FAULT step='+$fault+' ')){throw ('Fault not reached/refused '+$fault)}
    foreach($m in [regex]::Matches($err,'OWNED_CHILD pid=(\d+) created=(\d+)')){
     if(-not [ResourceProfile]::ConfirmNoLiveProcess([uint32]$m.Groups[1].Value)){throw 'A live process occupies the recorded owned PID; refusing ambiguous identity rather than assuming reuse'}
    }
    if($err -match 'OWNED_CHILD cleanup_verified=0' -or $err -match 'NATIVE_CLEANUP complete=0' -or $err -notmatch 'NATIVE_CLEANUP complete=1'){throw 'Native cleanup refused'}
    Write-Output ('Fault '+$fault+' reached and no matching owned child remains')
   }finally{
    $cleanupProblems=[Collections.Generic.List[string]]::new()
    if($faultStarted){
     try{
      if(-not $pr.HasExited){$pr.Kill($false)}
      if(-not $pr.WaitForExit(3000)){$cleanupProblems.Add('Supervisor termination unverified')}
      if($null -eq $et -or -not $et.Wait(3000)){$cleanupProblems.Add('Owned-child evidence unavailable')}
      else{
       $cleanupText=$et.GetAwaiter().GetResult()
       foreach($owned in [regex]::Matches($cleanupText,'OWNED_CHILD pid=(\d+) created=(\d+)')){
        if(-not [ResourceProfile]::ConfirmNoLiveProcess([uint32]$owned.Groups[1].Value)){$cleanupProblems.Add('Recorded child PID remains live')}
       }
       if($cleanupText -notmatch 'NATIVE_CLEANUP complete=1'){$cleanupProblems.Add('Native ownership cleanup not confirmed; child liveness may be unverified')}
      }
     }catch{$cleanupProblems.Add('Fault cleanup check failed: '+$_.Exception.Message)}
    }
    $pr.Dispose()
    if($cleanupProblems.Count){throw ('Fault cleanup refusal: '+($cleanupProblems -join '; '))}
   }
  }
 }elseif($ParentDeath){
  & "$PSScriptRoot/observe_parent_death.ps1" -ProbeExe $copy -Profile $profile -Root $base
 }else{
  & $copy $profile $base
  if($LASTEXITCODE -ne 0){throw ('Resource probe refused '+$LASTEXITCODE)}
 }
 if((Get-FileHash (Join-Path $private 'canary.txt') -Algorithm SHA256).Hash -ne $before){throw 'Canary changed'}
 if((Get-FileHash $copy -Algorithm SHA256).Hash -ne $copiedHash){throw 'Executable changed'}
 if(Test-Path (Join-Path $private 'must-not-exist.txt')){throw 'Forbidden create succeeded'}
 if(-not $FaultSweep -and -not $ParentDeath){if([IO.File]::ReadAllText((Join-Path $scratch 'created.txt')) -ne 'HONY-SYNTHETIC-ALLOWED-WRITE-v1'){throw 'Allowed scratch output invalid'}}
 Write-Output 'Actual fixed synthetic resource checks matched; not full network or Python parser acceptance'
}finally{
 $cleanupFailed=$false
 if($null -ne $exeGuard){$exeGuard.Dispose()}
 if($sid -ne [IntPtr]::Zero){if([ResourceProfile]::FreeSid($sid) -ne [IntPtr]::Zero){$cleanupFailed=$true}}
 if($created){if([ResourceProfile]::DeleteAppContainerProfile($profile) -ne 0){$cleanupFailed=$true}}
 if(Test-Path $base){Remove-Item -LiteralPath $base -Recurse -Force}
 if((Test-Path $base) -or $cleanupFailed){throw 'Synthetic cleanup incomplete'}
 Write-Output 'Synthetic profile deletion and root absence verified'
}
