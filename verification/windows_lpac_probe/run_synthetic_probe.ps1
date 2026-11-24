# Instinct: ephemeral Windows CI-only harness proposal. NOT EXECUTED.
# This intentionally changes ACLs/profile ONLY inside an isolated runner temp.
param([Parameter(Mandatory=$true)][string]$ProbeExe)
$ErrorActionPreference='Stop'
$exe=(Resolve-Path $ProbeExe).Path
if ($env:GITHUB_ACTIONS -ne 'true' -or -not $env:RUNNER_TEMP) { throw 'Disposable GitHub runner only' }
$base=Join-Path $env:RUNNER_TEMP 'hony-lpac-synthetic-only'
if (Test-Path $base) { throw 'Fresh private synthetic directory required' }
New-Item -ItemType Directory -Path $base | Out-Null
$profile='Instinct.Hony.SyntheticProbe.'+[guid]::NewGuid().ToString('N')
Add-Type @'
using System;
using System.Runtime.InteropServices;
public static class ProbeProfile {
 [DllImport("userenv.dll",CharSet=CharSet.Unicode)] public static extern int CreateAppContainerProfile(string n,string d,string s,IntPtr c,uint count,out IntPtr sid);
 [DllImport("userenv.dll",CharSet=CharSet.Unicode)] public static extern int DeleteAppContainerProfile(string n);
 [DllImport("advapi32.dll")] public static extern IntPtr FreeSid(IntPtr sid);
}
'@
$sid=[IntPtr]::Zero
$created=$false
try {
 $hr=[ProbeProfile]::CreateAppContainerProfile($profile,$profile,'Fixed synthetic probe only',[IntPtr]::Zero,0,[ref]$sid)
 if ($hr -ne 0) { throw ('Create profile failed '+$hr) }
 $created=$true
 $sidText=([System.Security.Principal.SecurityIdentifier]::new($sid)).Value
 $runtime=Join-Path $base 'runtime'
 $private=Join-Path $base 'private'
 New-Item -ItemType Directory -Path $runtime,$private | Out-Null
 $copy=Join-Path $runtime 'windows_probe.exe'
 Copy-Item $exe $copy
 $canary=Join-Path $private 'synthetic-canary.txt'
 Set-Content -LiteralPath $canary -Value 'SYNTHETIC-NO-HOTEL-DATA' -NoNewline
 # Remove ambient app package grants from our synthetic dirs; explicit user/system/admin only.
 $me=[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
 foreach ($p in @($base,$runtime,$private)) {
  & icacls.exe $p /inheritance:r /grant:r "*${me}:(OI)(CI)F" '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F'
  if ($LASTEXITCODE -ne 0) { throw 'Synthetic ACL setup failed' }
 }
 foreach ($p in @($copy,$canary)) {
  & icacls.exe $p /inheritance:r /grant:r "*${me}:F" '*S-1-5-18:F' '*S-1-5-32-544:F'
  if ($LASTEXITCODE -ne 0) { throw 'Synthetic file ACL setup failed' }
 }
 & icacls.exe $copy /grant "*${sidText}:RX"
 if ($LASTEXITCODE -ne 0) { throw 'Synthetic executable ACL failed' }
 & icacls.exe $base /grant "*${sidText}:RX"
 if ($LASTEXITCODE -ne 0) { throw 'Synthetic traverse ACL failed' }
 & icacls.exe $runtime /grant "*${sidText}:(OI)(CI)RX"
 if ($LASTEXITCODE -ne 0) { throw 'Synthetic runtime ACL failed' }
 & $copy $profile $canary
 if ($LASTEXITCODE -ne 0) { throw ('Native probe failed/inconclusive '+$LASTEXITCODE) }
} finally {
 $cleanupFailed=$false
 if ($sid -ne [IntPtr]::Zero) { [void][ProbeProfile]::FreeSid($sid) }
 try {
  if ($created) {
   $hr=[ProbeProfile]::DeleteAppContainerProfile($profile)
   if ($hr -ne 0) { $cleanupFailed=$true }
  }
 } finally {
  if (Test-Path $base) { Remove-Item -LiteralPath $base -Recurse -Force }
 }
 if ($cleanupFailed) { throw 'Synthetic profile cleanup incomplete' }
}
