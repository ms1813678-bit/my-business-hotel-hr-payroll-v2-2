# Shared ACL helper. Replaces explicit and inherited ACEs instead of appending
# grants to an unknown old DACL. Windows effective-access checks remain required.
function Set-HOnYAccess {
    param(
        [Parameter(Mandatory=$true)][string]$Path,
        [Parameter(Mandatory=$true)][string]$ServiceRights,
        [switch]$UsersRead,
        [switch]$Recurse,
        [string[]]$ExcludePaths = @()
    )
    $Items = @(Get-Item -Force -LiteralPath $Path)
    if ($Recurse) { $Items += @(Get-ChildItem -Force -LiteralPath $Path -Recurse) }
    foreach ($Item in $Items) {
        $Excluded = $false
        foreach ($ExcludedPath in $ExcludePaths) {
            $FullExcluded = [IO.Path]::GetFullPath($ExcludedPath).TrimEnd('\')
            if ($Item.FullName -eq $FullExcluded -or $Item.FullName.StartsWith($FullExcluded + '\', [StringComparison]::OrdinalIgnoreCase)) { $Excluded = $true }
        }
        if ($Excluded) { continue }
        if ($Item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
            throw 'Refusing ACL changes across a reparse point. Review the staging tree.'
        }
        if ($Item.PSIsContainer) {
            $Acl = New-Object Security.AccessControl.DirectorySecurity
            $Inheritance = [Security.AccessControl.InheritanceFlags]'ContainerInherit,ObjectInherit'
        } else {
            $Acl = New-Object Security.AccessControl.FileSecurity
            $Inheritance = [Security.AccessControl.InheritanceFlags]::None
        }
        $Acl.SetAccessRuleProtection($true, $false)
        $Admin = New-Object Security.Principal.SecurityIdentifier('S-1-5-32-544')
        $Acl.SetOwner($Admin)
        $Grants = @(@('S-1-5-32-544','FullControl'), @('S-1-5-18','FullControl'), @('S-1-5-20',$ServiceRights))
        if ($UsersRead) { $Grants += ,@('S-1-5-32-545','ReadAndExecute') }
        foreach ($Grant in $Grants) {
            $Sid = New-Object Security.Principal.SecurityIdentifier($Grant[0])
            $Rule = New-Object Security.AccessControl.FileSystemAccessRule($Sid, [Security.AccessControl.FileSystemRights]$Grant[1], $Inheritance, [Security.AccessControl.PropagationFlags]::None, [Security.AccessControl.AccessControlType]::Allow)
            $Acl.AddAccessRule($Rule)
        }
        Set-Acl -LiteralPath $Item.FullName -AclObject $Acl -ErrorAction Stop
    }
}
