# Read-only preflight, no ownership/ACL/PATH/interpreter changes.
# Conservative gate, not a substitute for effective-access tests on Windows.
function Assert-HOnYMachinePython {
    param([Parameter(Mandatory=$true)][string]$Root)
    $VenvConfig = Join-Path $Root '.venv\pyvenv.cfg'
    $HomeLines = @(Get-Content -LiteralPath $VenvConfig | Where-Object { $_ -match '^home\s*=' })
    if ($HomeLines.Count -ne 1) { throw 'Unambiguous machine Python base is required.' }
    $HomePath = [IO.Path]::GetFullPath(($HomeLines[0] -replace '^home\s*=\s*','').Trim()).TrimEnd('\')
    $ProgramFilesRoot = [IO.Path]::GetFullPath($env:ProgramFiles).TrimEnd('\') + '\'
    if (-not $HomePath.StartsWith($ProgramFilesRoot, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'This candidate requires an administrator-controlled machine Python under Program Files. Per-user or other bases need separate review.'
    }
    $BaseExe = Join-Path $HomePath 'python.exe'
    if (-not (Test-Path -LiteralPath $BaseExe -PathType Leaf)) { throw 'Base Python executable is missing.' }
    $AllowedOwners = @('S-1-5-18','S-1-5-32-544','S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464')
    $BroadGroups = @('S-1-1-0','S-1-5-11','S-1-5-32-545')
    $WriteMask = [Security.AccessControl.FileSystemRights]'WriteData,AppendData,WriteAttributes,WriteExtendedAttributes,ChangePermissions,TakeOwnership,Delete,DeleteSubdirectoriesAndFiles'
    foreach ($Path in @($HomePath, $BaseExe, (Join-Path $Root '.venv\Scripts\python.exe'))) {
        $Item = Get-Item -LiteralPath $Path -Force
        if ($Item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Python preflight refuses reparse points.' }
        $Acl = Get-Acl -LiteralPath $Path
        $OwnerSid = $Acl.GetOwner([Security.Principal.SecurityIdentifier]).Value
        if ($OwnerSid -notin $AllowedOwners) { throw 'Python location owner is not a reviewed machine administrator principal.' }
        foreach ($Rule in $Acl.GetAccessRules($true,$true,[Security.Principal.SecurityIdentifier])) {
            if ($Rule.IdentityReference.Value -in $BroadGroups -and $Rule.AccessControlType -eq [Security.AccessControl.AccessControlType]::Allow -and ($Rule.FileSystemRights -band $WriteMask)) {
                throw 'Python location grants broad write access. Candidate refuses to run elevated.'
            }
        }
    }
}
