# Optional local read-only inventory. Does not elevate or change settings.
$ErrorActionPreference='Stop'
$result=[ordered]@{schema=1;read_only=$true;machine_identity_collected=$false}
try {
  $os=Get-CimInstance Win32_OperatingSystem
  $result.os=[ordered]@{caption=$os.Caption;version=$os.Version;build=$os.BuildNumber;architecture=$os.OSArchitecture;sku=$os.OperatingSystemSKU}
} catch {$result.os=[ordered]@{query='unavailable'}}
try {
  $system=Get-CimInstance Win32_ComputerSystem
  $result.system=[ordered]@{ram_bytes=[long]$system.TotalPhysicalMemory;hypervisor_present=$system.HypervisorPresent}
} catch {$result.system=[ordered]@{query='unavailable'}}
try {
  $result.processors=@(Get-CimInstance Win32_Processor | ForEach-Object {
    [ordered]@{architecture=$_.Architecture;cores=$_.NumberOfCores;logical_processors=$_.NumberOfLogicalProcessors;virtualization_firmware_enabled=$_.VirtualizationFirmwareEnabled;slat=$_.SecondLevelAddressTranslationExtensions;vm_monitor_extensions=$_.VMMonitorModeExtensions}
  })
} catch {$result.processors=@([ordered]@{query='unavailable'})}
try {
  $disk=Get-CimInstance Win32_LogicalDisk -Filter ("DeviceID='"+$env:SystemDrive+"'")
  $result.system_disk=[ordered]@{size_bytes=[long]$disk.Size;free_bytes=[long]$disk.FreeSpace;filesystem=$disk.FileSystem}
} catch {$result.system_disk=[ordered]@{query='unavailable'}}
$result.features=[ordered]@{}
foreach($name in @('Microsoft-Hyper-V-All','Containers-DisposableClientVM')){
  try {
    $feature=Get-WindowsOptionalFeature -Online -FeatureName $name -ErrorAction Stop
    $result.features[$name]=[string]$feature.State
  } catch {$result.features[$name]='unknown_query_unavailable'}
}
$result | ConvertTo-Json -Depth 5
