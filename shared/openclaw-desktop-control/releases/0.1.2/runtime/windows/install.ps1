# Write a Scheduled Task XML template. Does not register the task, does not
# store secrets, and does not enable the controller.
param(
    [Parameter(Mandatory = $true)][string]$ConfigPath,
    [Parameter(Mandatory = $true)][string]$OutXml
)
$ErrorActionPreference = "Stop"
$Project = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$RunPy = Join-Path $Project "runtime\run.py"
$xml = @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>OpenClaw local Telegram/Desktop controller (disabled template)</Description>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger><Enabled>false</Enabled></LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author"><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <Enabled>false</Enabled>
    <Hidden>false</Hidden>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>python</Command>
      <Arguments>$RunPy --config $ConfigPath --serve</Arguments>
    </Exec>
  </Actions>
</Task>
"@
Set-Content -Path $OutXml -Value $xml -Encoding UTF8
Write-Output "Wrote disabled task template (no secrets) to $OutXml"
