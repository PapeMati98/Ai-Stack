param(
    [switch]$Uninstall,
    [switch]$RunNow,
    [int]$IntervalMinutes = 30
)
# Installs the per-user (non-elevated) scheduled task that runs muse_acl_guard.ps1 -Apply.
# The guard writes only when the Muse config DACL exceeds its threshold; see muse_acl_guard.ps1.
$ErrorActionPreference = 'Stop'
$taskName = 'AgentStack Muse ACL Guard'
$installDir = Join-Path $env:USERPROFILE '.agents\maintenance\muse-acl-guard'
$source = Join-Path $PSScriptRoot 'muse_acl_guard.ps1'
$runner = Join-Path $installDir 'muse_acl_guard.ps1'

if ($Uninstall) {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "Wyrejestrowano zadanie: $taskName (pliki w $installDir pozostaja)"
    return
}

New-Item -ItemType Directory -Force -Path $installDir | Out-Null
Copy-Item -LiteralPath $source -Destination $runner -Force
$srcHash = (Get-FileHash -LiteralPath $source -Algorithm SHA256).Hash
$dstHash = (Get-FileHash -LiteralPath $runner -Algorithm SHA256).Hash
if ($srcHash -ne $dstHash) { throw "Hash mismatch po kopiowaniu runnera" }

$actionArgs = '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{0}" -Apply' -f $runner
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $actionArgs -WorkingDirectory $installDir
$trigger = New-ScheduledTaskTrigger `
    -Once `
    -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes) `
    -RepetitionDuration (New-TimeSpan -Days 3650)
$settings = New-ScheduledTaskSettingsSet `
    -Disable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 5) `
    -MultipleInstances IgnoreNew `
    -StartWhenAvailable `
    -Hidden
$principal = New-ScheduledTaskPrincipal `
    -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) `
    -LogonType Interactive `
    -RunLevel Limited

Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description 'Kompakcja DACL katalogu konfiguracji Muse (bug narastania DENY w sandboxie Muse, blad 1340).' `
    -Force `
    -ErrorAction Stop | Out-Null

$protector = Join-Path $PSScriptRoot '..\hidden-launch\Protect-ConsoleTasks.ps1'
& $protector -TaskName $taskName -Apply | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Hidden task-action protection failed' }
Enable-ScheduledTask -TaskName $taskName -ErrorAction Stop | Out-Null

if ($RunNow) {
    Start-ScheduledTask -TaskName $taskName -ErrorAction Stop
}

$task = Get-ScheduledTask -TaskName $taskName -ErrorAction Stop
[pscustomobject]@{
    status = 'INSTALLED'
    task_name = $task.TaskName
    state = [string]$task.State
    interval_minutes = $IntervalMinutes
    runner = $runner
    runner_sha256 = $dstHash
} | ConvertTo-Json -Depth 3
