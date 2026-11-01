<#
"AgentStack RSI Helpdesk": resolve_cycle.py every 2 h, the only launcher of the cycle
(helpdesk.py `file` asks Task Scheduler to run it on demand).

Plan by default. -Apply registers the task DISABLED and wraps its action with the
HiddenLaunch GUI host (private AgentStackBackground desktop, CREATE_NO_WINDOW), so
no trigger can fire before it is windowless. -Enable turns it on after the
synthetic no-window check. Never deletes anything.
Stop: Disable-ScheduledTask -TaskName "AgentStack RSI Helpdesk", or
~/.agents/helpdesk/STOP, or "enabled": false in ~/.agents/helpdesk/config.json.
Rollback of the wrapper: see global/tools/hidden-launch/README.md (XML backup).
#>
param(
    [switch]$Apply,
    [switch]$Enable,
    [string]$TaskName = "AgentStack RSI Helpdesk",
    [int]$EveryHours = 2,
    [string]$CycleScript = (Join-Path $env:USERPROFILE ".agents\skills\rsi-helpdesk\scripts\resolve_cycle.py"),
    [string]$HiddenLaunchRuntime = (Join-Path $env:USERPROFILE ".agents\runtime\hidden-launch\1.0.3"),
    [string]$ProtectScript = (Join-Path $PSScriptRoot "..\..\..\..\tools\hidden-launch\Protect-ConsoleTasks.ps1")
)
$ErrorActionPreference = "Stop"
$python = (Get-Command python.exe -ErrorAction Stop).Source
$pythonw = Join-Path (Split-Path $python) "pythonw.exe"
$launcher = Join-Path $HiddenLaunchRuntime "HiddenLaunch.exe"
foreach ($required in @($pythonw, $CycleScript, $launcher, $ProtectScript)) {
    if (-not (Test-Path -LiteralPath $required)) { throw "missing: $required" }
}
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
$plan = [ordered]@{
    task = $TaskName; execute = $pythonw; arguments = "-B `"$CycleScript`" run"
    every_hours = $EveryHours; wrapper = $launcher; exists = [bool]$existing
    state = if ($existing) { [string]$existing.State } else { $null }
}

if ($Apply) {
    if ($existing -and @($existing.Actions | Where-Object { $_.Execute -eq $launcher }).Count -gt 0) {
        $plan.registered = "ALREADY_WRAPPED"
    } else {
        $action = New-ScheduledTaskAction -Execute $pythonw -Argument "-B `"$CycleScript`" run" `
            -WorkingDirectory (Split-Path $CycleScript)
        $trigger = New-ScheduledTaskTrigger -Once -At ((Get-Date).Date.AddHours((Get-Date).Hour + 1)) `
            -RepetitionInterval (New-TimeSpan -Hours $EveryHours)
        $settings = New-ScheduledTaskSettingsSet -Disable -MultipleInstances IgnoreNew -StartWhenAvailable `
            -ExecutionTimeLimit (New-TimeSpan -Minutes 20) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
        $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
        Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
            -Principal $principal -Description "RSI Helpdesk: skan usterek AI stacku i jedna sesja naprawcza w tle (skill rsi-helpdesk)." `
            -Force | Out-Null
        & $ProtectScript -TaskName $TaskName -Runtime $HiddenLaunchRuntime -Apply | Out-Null
        $plan.registered = "REGISTERED_DISABLED_WRAPPED"
    }
}

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($task) {
    $wrapped = @($task.Actions | Where-Object { $_.Execute -eq $launcher }).Count -eq @($task.Actions).Count
    $plan.wrapped = $wrapped
    $plan.action = ($task.Actions | ForEach-Object { "$($_.Execute) $($_.Arguments)" }) -join " ; "
    $plan.repetition = [string]$task.Triggers[0].Repetition.Interval
    $plan.repetition_duration = [string]$task.Triggers[0].Repetition.Duration
    if ($Enable) {
        if (-not $wrapped) { throw "refusing to enable: action is not wrapped by HiddenLaunch" }
        Enable-ScheduledTask -TaskName $TaskName | Out-Null
        $task = Get-ScheduledTask -TaskName $TaskName
    }
    $plan.state = [string]$task.State
}
$plan | ConvertTo-Json
