[CmdletBinding()]
param([string]$State = (Join-Path $env:USERPROFILE '.agents\hidden-launch-state'))
$ErrorActionPreference = 'Stop'
# Run as the owner in the actual host context; MSIX callers can have a virtual HKCU.
# This disables default-terminal delegation, not the Windows Terminal application.
# Microsoft Support: /windows/apps/command-prompt-and-windows-powershell
$consoleHost = '{B23D10C0-E52E-411E-9D5B-C09FDF709C7D}'
$key = [Microsoft.Win32.Registry]::CurrentUser.CreateSubKey('Console\%%Startup')
try {
    $before = [ordered]@{}
    foreach ($name in 'DelegationConsole','DelegationTerminal') {
        $before[$name] = $key.GetValue($name,$null)
    }
    New-Item -ItemType Directory -Path $State -Force | Out-Null
    $backup = Join-Path $State 'console-host-original.json'
    if (-not (Test-Path -LiteralPath $backup)) {
        [IO.File]::WriteAllText($backup,($before | ConvertTo-Json),[Text.UTF8Encoding]::new($false))
    }
    foreach ($name in 'DelegationConsole','DelegationTerminal') {
        $key.SetValue($name,$consoleHost,[Microsoft.Win32.RegistryValueKind]::String)
        if ($key.GetValue($name) -ne $consoleHost) { throw 'Console host selection was not verified' }
    }
    [pscustomobject]@{Status='VERIFIED';Host='Windows Console Host';Before=$before;Backup=$backup;Identity=[Security.Principal.WindowsIdentity]::GetCurrent().Name} | ConvertTo-Json -Depth 4
} finally { $key.Dispose() }
