param([Parameter(Mandatory=$true)][string]$Launcher,[Parameter(Mandatory=$true)][string]$Stage,[Parameter(Mandatory=$true)][string]$ManifestPin,[Parameter(Mandatory=$true)][string]$Request,[Parameter(Mandatory=$true)][string]$TrustedPython,[Parameter(Mandatory=$true)][string]$Owner,[Parameter(Mandatory=$true)][string]$ProductRoot,[switch]$Probe,[switch]$Harness,[string]$HarnessLock,[string]$HarnessPackagePaths)
# Disposable CI qualification only. Not installation, network acceptance or hotel execution.
$ErrorActionPreference='Stop'
if($env:GITHUB_ACTIONS -ne 'true'){throw 'Disposable CI only'}
$Stage=(Resolve-Path -LiteralPath $Stage).Path
$tempRoot=(Resolve-Path -LiteralPath $env:RUNNER_TEMP).Path.TrimEnd('\')+'\'
if(-not $Stage.StartsWith($tempRoot,[StringComparison]::OrdinalIgnoreCase) -or -not (Test-Path (Join-Path $Stage 'CANDIDATE-OWNER.json'))){throw 'Only owned runner temporary stage allowed'}
$ownerRecord=Get-Content (Join-Path $Stage 'CANDIDATE-OWNER.json') -Raw|ConvertFrom-Json
if($ownerRecord.owner -ne $Owner -or $ownerRecord.state -ne 'staged-not-accepted'){throw 'Stage ownership mismatch'}
if($Probe -and $Harness){throw 'Conflicting qualification modes'}
$primaryError=$null
$profile='Hony.Parser.'+[guid]::NewGuid().ToString('N')
Add-Type @'
using System;using System.Runtime.InteropServices;
public static class ParserProfile {
[DllImport("userenv.dll",CharSet=CharSet.Unicode)]public static extern int CreateAppContainerProfile(string n,string d,string s,IntPtr c,uint count,out IntPtr sid);
[DllImport("userenv.dll",CharSet=CharSet.Unicode)]public static extern int DeleteAppContainerProfile(string n);
[DllImport("advapi32.dll")]public static extern IntPtr FreeSid(IntPtr p);
}
'@
$sid=[IntPtr]::Zero;$created=$false
try {
 $hr=[ParserProfile]::CreateAppContainerProfile($profile,$profile,'Synthetic parser qualification',[IntPtr]::Zero,0,[ref]$sid)
 if($hr -ne 0){throw ('CreateProfile '+$hr)};$created=$true
 $sidText=([Security.Principal.SecurityIdentifier]::new($sid)).Value
 $me=[Security.Principal.WindowsIdentity]::GetCurrent().User.Value
 # Stage is a newly owned disposable copy; no hotel or system ACLs touched.
 foreach($item in @(Get-Item $Stage)+@(Get-ChildItem $Stage -Recurse -Force)){
  if($item.Attributes -band [IO.FileAttributes]::ReparsePoint){throw 'Reparse stage rejected'}
  if($item.PSIsContainer){$acl=[Security.AccessControl.DirectorySecurity]::new();$inherit=[Security.AccessControl.InheritanceFlags]'ContainerInherit,ObjectInherit'}
  else{$acl=[Security.AccessControl.FileSecurity]::new();$inherit=[Security.AccessControl.InheritanceFlags]::None}
  $acl.SetAccessRuleProtection($true,$false)
  foreach($owner in @($me,'S-1-5-18','S-1-5-32-544')){
   $rule=[Security.AccessControl.FileSystemAccessRule]::new([Security.Principal.SecurityIdentifier]::new($owner),[Security.AccessControl.FileSystemRights]::FullControl,$inherit,[Security.AccessControl.PropagationFlags]::None,[Security.AccessControl.AccessControlType]::Allow)
   $acl.AddAccessRule($rule)
  }
  $rule=[Security.AccessControl.FileSystemAccessRule]::new([Security.Principal.SecurityIdentifier]::new($sidText),[Security.AccessControl.FileSystemRights]::ReadAndExecute,$inherit,[Security.AccessControl.PropagationFlags]::None,[Security.AccessControl.AccessControlType]::Allow)
  $acl.AddAccessRule($rule);Set-Acl -LiteralPath $item.FullName -AclObject $acl
  $readback=Get-Acl -LiteralPath $item.FullName
  if(-not $readback.AreAccessRulesProtected){throw 'Unprotected stage ACL'}
  $seen=@{}
  foreach($rule in $readback.Access){
   $identity=$rule.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value
   $seen[$identity]=$true
   if($rule.InheritanceFlags -ne $inherit -or $rule.PropagationFlags -ne [Security.AccessControl.PropagationFlags]::None){throw 'ACL inheritance mismatch'}
   if($identity -ne $sidText -and $rule.FileSystemRights -ne [Security.AccessControl.FileSystemRights]::FullControl){throw 'Owner FullControl mismatch'}
   if($identity -notin @($me,'S-1-5-18','S-1-5-32-544',$sidText) -or $rule.AccessControlType -ne 'Allow'){throw 'Unexpected stage ACL identity/type'}
   if($identity -eq $sidText -and ([long]$rule.FileSystemRights -band -bnot [long]([Security.AccessControl.FileSystemRights]::ReadAndExecute -bor [Security.AccessControl.FileSystemRights]::Synchronize)) -ne 0){throw 'Unexpected package access rights'}
  }
  foreach($expected in @($me,'S-1-5-18','S-1-5-32-544')){if(-not $seen.ContainsKey($expected)){throw 'Required owner grant absent'}}
  if(-not @($readback.Access|Where-Object {$_.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value -eq $sidText}).Count){throw 'Package RX readback absent'}
 }
 Write-Output 'Disposable package RX ACL readback matched, no write grant'
 $launcherHash=(Get-FileHash -LiteralPath $Launcher -Algorithm SHA256).Hash.ToLower()
 if($Probe){
  # Diagnostic only: same ACLs, profile, job and token; the child is the kernel32-only loader probe.
  & $TrustedPython -I "$PSScriptRoot/run_probe.py" --launcher $Launcher --profile $profile --stage $Stage --manifest-pin $ManifestPin --launcher-pin $launcherHash
  if($LASTEXITCODE -ne 0){throw 'Loader isolation probe driver refused'}
 } elseif($Harness){
  # Package check (basis trusted_host_vendor_vs_lock) reads vendor-path metadata via PYTHONPATH below on the trusted host, not an independent host pip version.
  # Parse-equivalence harness inside this same profile/ACL/stage lifecycle, before the finally teardown. Exit gates every check.
  if(-not $HarnessLock -or -not $HarnessPackagePaths){throw 'Harness lock and package paths required'}
  $savedPath=$env:PYTHONPATH
  try {
   $env:PYTHONPATH=$ProductRoot+';'+$HarnessPackagePaths
   & $TrustedPython -B "$PSScriptRoot/qualify_parse_equivalence.py" $Launcher $profile $Stage $ManifestPin $launcherHash $HarnessLock
   $harnessExit=$LASTEXITCODE
  } finally {$env:PYTHONPATH=$savedPath}
  if($harnessExit -ne 0){throw 'Parse equivalence harness refused'}
 } else {
 & $TrustedPython -I "$PSScriptRoot/run_request.py" --product-root $ProductRoot --launcher $Launcher --profile $profile --stage $Stage --manifest-pin $ManifestPin --launcher-pin $launcherHash --request $Request
 if($LASTEXITCODE -ne 0){throw 'Contained real parser qualification refused'}
 }
} catch {
 $primaryError=$_.Exception.ToString()
} finally {
 $cleanupProblems=[Collections.Generic.List[string]]::new()
 if($sid -ne [IntPtr]::Zero){if([ParserProfile]::FreeSid($sid) -ne [IntPtr]::Zero){$cleanupProblems.Add('Profile SID cleanup failed')}}
 if($created){if([ParserProfile]::DeleteAppContainerProfile($profile) -ne 0){$cleanupProblems.Add('Profile cleanup failed')}}
 if($cleanupProblems.Count -or $primaryError){throw ('Qualification='+$primaryError+' Cleanup='+($cleanupProblems -join '; '))}
 Write-Output 'Owned synthetic profile deletion verified'
}
