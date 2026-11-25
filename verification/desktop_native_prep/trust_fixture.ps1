# NEW current-user synthetic CA fixture. Never used on a persistent/user machine.
param([Parameter(Mandatory=$true)][string]$Mode,[Parameter(Mandatory=$true)][string]$Thumbprint,[string]$CertificatePath)
$ErrorActionPreference='Stop'
if ($env:GITHUB_ACTIONS -ne 'true' -or $env:RUNNER_OS -ne 'Windows' -or $Thumbprint -notmatch '^[0-9A-F]{40}$') { throw 'Disposable Windows CI and exact SHA1 certificate identity required.' }
$Matches=@(Get-ChildItem Cert:\CurrentUser\Root | Where-Object { $_.Thumbprint -eq $Thumbprint })
if ($Mode -eq 'absent') {
 if ($Matches.Count -ne 0) { throw 'Unexpected current-user CA match.' }
 Write-Output 'TEST_CA_EXACT_ABSENT'
} elseif ($Mode -eq 'import') {
 if ($Matches.Count -ne 0) { throw 'Refusing preexisting CA identity.' }
 $Certificate=New-Object Security.Cryptography.X509Certificates.X509Certificate2($CertificatePath)
 if ($Certificate.Thumbprint -ne $Thumbprint -or $Certificate.HasPrivateKey) { throw 'Wrong certificate identity or private key import.' }
 Import-Certificate -FilePath $CertificatePath -CertStoreLocation Cert:\CurrentUser\Root | Out-Null
 $After=@(Get-ChildItem Cert:\CurrentUser\Root | Where-Object { $_.Thumbprint -eq $Thumbprint })
 if ($After.Count -ne 1) { throw 'Test CA import unconfirmed.' }
 Write-Output 'TEST_CA_EXACT_IMPORT_PASS'
} elseif ($Mode -eq 'remove') {
 if ($Matches.Count -ne 1) { throw 'Owned test CA identity no longer exact.' }
 Remove-Item -LiteralPath ('Cert:\CurrentUser\Root\'+$Thumbprint)
 if (@(Get-ChildItem Cert:\CurrentUser\Root | Where-Object { $_.Thumbprint -eq $Thumbprint }).Count -ne 0) { throw 'Test CA removal unconfirmed.' }
 Write-Output 'TEST_CA_EXACT_REMOVE_PASS'
} else { throw 'Unknown fixture mode.' }
