# NEW current-user synthetic CA fixture. Never used on a persistent/user machine.
param([Parameter(Mandatory=$true)][string]$Mode,[Parameter(Mandatory=$true)][string]$Thumbprint,[string]$CertificatePath)
$ErrorActionPreference='Stop'
if ($env:GITHUB_ACTIONS -ne 'true' -or $env:RUNNER_OS -ne 'Windows' -or $Thumbprint -notmatch '^[0-9A-F]{40}$') { throw 'Disposable Windows CI and exact SHA1 certificate identity required.' }
# Explicit .NET store avoids assuming a shell certificate-provider drive exists.
$Store=New-Object Security.Cryptography.X509Certificates.X509Store('Root','CurrentUser')
$Store.Open([Security.Cryptography.X509Certificates.OpenFlags]::ReadWrite)
try {
 $Found=@($Store.Certificates | Where-Object { $_.Thumbprint -eq $Thumbprint })
 if ($Mode -eq 'absent') {
  if ($Found.Count -ne 0) { throw 'Unexpected current-user CA match.' }
  Write-Output 'TEST_CA_EXACT_ABSENT'
 } elseif ($Mode -eq 'import') {
  if ($Found.Count -ne 0) { throw 'Refusing preexisting CA identity.' }
  $Certificate=New-Object Security.Cryptography.X509Certificates.X509Certificate2($CertificatePath)
  if ($Certificate.Thumbprint -ne $Thumbprint -or $Certificate.HasPrivateKey) { throw 'Wrong certificate identity or private key import.' }
  & certutil.exe -user -f -addstore Root $CertificatePath
  if ($LASTEXITCODE -ne 0) { throw 'Current-user test CA import refused.' }
  $Store.Close();$Store.Open([Security.Cryptography.X509Certificates.OpenFlags]::ReadWrite)
  $After=@($Store.Certificates | Where-Object { $_.Thumbprint -eq $Thumbprint })
  if ($After.Count -ne 1) { throw 'Test CA import unconfirmed.' }
  Write-Output 'TEST_CA_EXACT_IMPORT_PASS'
 } elseif ($Mode -eq 'remove') {
  if ($Found.Count -gt 1) { throw 'Owned test CA identity ambiguous.' }
  if ($Found.Count -eq 1) { $Store.Remove($Found[0]) }
  $Store.Close();$Store.Open([Security.Cryptography.X509Certificates.OpenFlags]::ReadWrite)
  if (@($Store.Certificates | Where-Object { $_.Thumbprint -eq $Thumbprint }).Count -ne 0) { throw 'Test CA removal unconfirmed.' }
  Write-Output 'TEST_CA_EXACT_REMOVE_PASS'
 } else { throw 'Unknown fixture mode.' }
} finally { $Store.Close() }
