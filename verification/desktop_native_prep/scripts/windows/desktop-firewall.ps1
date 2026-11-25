param(
    [Parameter(Mandatory=$true)][ValidatePattern('^\d{1,3}(\.\d{1,3}){3}$')][string]$ClientSubnet,
    [Parameter(Mandatory=$true)][ValidateRange(16,30)][int]$PrefixLength,
    [switch]$Apply
)
$ErrorActionPreference = 'Stop'
$ClientAddress = [Net.IPAddress]::Parse($ClientSubnet)
if ($ClientAddress.AddressFamily -ne [Net.Sockets.AddressFamily]::InterNetwork) { throw 'IPv4 subnet required.' }
$Bytes = $ClientAddress.GetAddressBytes()
$Private = $Bytes[0] -eq 10 -or ($Bytes[0] -eq 172 -and $Bytes[1] -ge 16 -and $Bytes[1] -le 31) -or ($Bytes[0] -eq 192 -and $Bytes[1] -eq 168)
if (-not $Private) { throw 'Only a reviewed RFC1918 client subnet is allowed.' }
$HostBits = 32 - $PrefixLength
$Number = ([uint32]$Bytes[0] -shl 24) -bor ([uint32]$Bytes[1] -shl 16) -bor ([uint32]$Bytes[2] -shl 8) -bor [uint32]$Bytes[3]
$HostMask = [uint32]([math]::Pow(2, $HostBits) - 1)
if (($Number -band $HostMask) -ne 0) { throw 'ClientSubnet must be the normalized network address, not a host address.' }
$Root = (Resolve-Path "$PSScriptRoot\..\..").Path
$Config = Get-Content -Raw (Join-Path $env:ProgramData 'HOnY\desktop.json') | ConvertFrom-Json
$Uri = [Uri]$Config.origin
$Python = Join-Path $Root '.venv\Scripts\python.exe'
$Profiles = @(Get-NetConnectionProfile)
if (-not ($Profiles | Where-Object { $_.IPv4Connectivity -ne 'Disconnected' -and $_.NetworkCategory -in @('Private','DomainAuthenticated') })) {
    throw 'A reviewed Private/Domain hotel LAN is required. This script never changes network categories.'
}
$Rule = @{
    DisplayName = 'HOnY reviewed hotel LAN HTTPS'
    Direction = 'Inbound'
    Action = 'Allow'
    Enabled = 'True'
    Profile = @('Private','Domain')
    Protocol = 'TCP'
    LocalAddress = $Config.bind_ipv4
    LocalPort = $Uri.Port
    RemoteAddress = "$ClientSubnet/$PrefixLength"
    Program = $Python
    Service = 'HOnYBackend'
    EdgeTraversalPolicy = 'Block'
}
$Rule | Format-List | Out-String | Write-Output
Write-Output 'IPv4 only. No public profile, wildcard bind, IPv6 rule, router forwarding or DNS change. Review all existing inbound allow rules separately.'
if (-not $Apply) { Write-Output 'Preview only. Nothing changed.'; return }
if (Get-NetFirewallRule -DisplayName $Rule.DisplayName -ErrorAction SilentlyContinue) { throw 'Matching rule exists; inspect rather than duplicate.' }
New-NetFirewallRule @Rule
