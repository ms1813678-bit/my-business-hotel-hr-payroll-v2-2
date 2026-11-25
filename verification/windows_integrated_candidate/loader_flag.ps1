# Diagnostic-only image-name scope on a fresh disposable runner, not path-specific.
function Invoke-WithLoaderSnaps([scriptblock]$Action) {
  $base=$null;$parent=$null;$key=$null;$created=$false;$hadValue=$false;$restoreArmed=$false
  $actionFailed=$false;$restoreFailed=$false;$setupComplete=$false
  try {
    $base=[Microsoft.Win32.RegistryKey]::OpenBaseKey([Microsoft.Win32.RegistryHive]::LocalMachine,[Microsoft.Win32.RegistryView]::Registry64)
    $parent=$base.OpenSubKey('SOFTWARE\Microsoft\Windows NT\CurrentVersion\Image File Execution Options',$true)
    if($null -eq $parent){throw 'Existing IFEO parent required'}
    $key=$parent.OpenSubKey('python.exe',$true)
    if($null -eq $key){$key=$parent.CreateSubKey('python.exe');$created=$true}
    $hadValue=$key.GetValueNames() -contains 'GlobalFlag'
    if($hadValue){
      $oldKind=$key.GetValueKind('GlobalFlag')
      $oldValue=$key.GetValue('GlobalFlag',$null,[Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
      if($oldKind -ne [Microsoft.Win32.RegistryValueKind]::DWord){throw 'Prior GlobalFlag not DWORD; no value write'}
      $prior=[BitConverter]::ToUInt32([BitConverter]::GetBytes([int]$oldValue),0)
    }else{$prior=[uint32]0}
    # Numeric prior-state evidence for repair if restore fails, no unrelated values.
    [Console]::Error.WriteLine('loader_flag_prior existed={0} dword={1} owned_key={2} image_name_scope=1',$hadValue,$prior,$created)
    $newValue=$prior -bor [uint32]2
    $signedValue=[BitConverter]::ToInt32([BitConverter]::GetBytes([uint32]$newValue),0)
    $restoreArmed=$true
    $key.SetValue('GlobalFlag',$signedValue,[Microsoft.Win32.RegistryValueKind]::DWord)
    if($key.GetValueKind('GlobalFlag') -ne [Microsoft.Win32.RegistryValueKind]::DWord -or [int]$key.GetValue('GlobalFlag') -ne $signedValue){throw 'Loader flag write readback failed'}
    $setupComplete=$true
    [Console]::Error.WriteLine('loader_flag image_name_scope=1 readback=1 temporary=1')
    & $Action
  }catch{
    $actionFailed=$true
    # Keep raw exception/path/registry data out of public logs.
    [Console]::Error.WriteLine('loader_flag_action_or_setup_failed setup_complete={0}',$setupComplete)
  }finally{
    try{
      if($null -ne $key -and $restoreArmed){
        if($hadValue){
          $key.SetValue('GlobalFlag',$oldValue,$oldKind)
          if($key.GetValueKind('GlobalFlag') -ne $oldKind -or $key.GetValue('GlobalFlag',$null,[Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames) -ne $oldValue){throw 'Restore mismatch'}
        }else{
          $key.DeleteValue('GlobalFlag',$false)
          if($key.GetValueNames() -contains 'GlobalFlag'){throw 'Removal mismatch'}
        }
      }
    }catch{$restoreFailed=$true;[Console]::Error.WriteLine('loader_flag_restore value_restore_failed=1')}
    try{
      if($created -and $null -ne $key){
        if($key.SubKeyCount -ne 0 -or $key.ValueCount -ne 0){throw 'New key not empty'}
        $key.Dispose();$key=$null
        # Fresh dedicated runner only. Refuse nonempty keys, never DeleteSubKeyTree.
        $check=$parent.OpenSubKey('python.exe')
        try{if($null -eq $check -or $check.SubKeyCount -ne 0 -or $check.ValueCount -ne 0){throw 'Owned key changed'}}finally{if($null -ne $check){$check.Dispose()}}
        $parent.DeleteSubKey('python.exe',$false)
        $check=$parent.OpenSubKey('python.exe')
        if($null -ne $check){$check.Dispose();throw 'Key cleanup mismatch'}
      }
    }catch{$restoreFailed=$true;[Console]::Error.WriteLine('loader_flag_restore owned_key_cleanup_failed=1')}
    finally{
      if($null -ne $key){$key.Dispose()}
      if($null -ne $parent){$parent.Dispose()}
      if($null -ne $base){$base.Dispose()}
    }
    [Console]::Error.WriteLine('loader_flag_restore attempted={0} complete={1} action_failed={2} forced_host_kill_not_covered=1',$restoreArmed,(-not $restoreFailed),$actionFailed)
  }
  if($restoreFailed -or $actionFailed){throw 'Diagnostic setup/action/restore refused; see bounded flags above'}
}
