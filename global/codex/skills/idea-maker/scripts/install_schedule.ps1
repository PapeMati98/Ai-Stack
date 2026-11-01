<#
Daily model-free check. Plan only unless -Apply; registers DISABLED before wrapping
with HiddenLaunch on AgentStackBackground. No deletion and no automatic run.
Enable only after the synthetic no-window check, with -Apply -Enable.
#>
param(
    [switch]$Apply,
    [switch]$Enable,
    [string]$TaskName = "AgentStack Idea Maker",
    [ValidatePattern('^([01][0-9]|2[0-3]):[0-5][0-9]$')][string]$At = "09:15",
    [string]$CycleScript = (Join-Path $env:USERPROFILE ".agents\skills\idea-maker\scripts\idea_cycle.py"),
    [string]$HiddenLaunchRuntime = (Join-Path $env:USERPROFILE ".agents\runtime\hidden-launch\1.0.3"),
    [string]$ProtectScript = (Join-Path $PSScriptRoot "..\..\..\..\tools\hidden-launch\Protect-ConsoleTasks.ps1")
)
$ErrorActionPreference = "Stop"
$python = (Get-Command python.exe -ErrorAction Stop).Source
$pythonw = Join-Path (Split-Path $python) "pythonw.exe"
$launcher = Join-Path $HiddenLaunchRuntime "HiddenLaunch.exe"
$plan = [ordered]@{
    task = $TaskName; execute = $pythonw; arguments = "-B `"$CycleScript`" run"
    daily_at = $At; wrapper = $launcher; register_disabled = $true
    enable_requested = [bool]$Enable; apply = [bool]$Apply
}
# Even -Enable alone is plan-only. No Task Scheduler API is called on this path.
if (-not $Apply) {
    $plan | ConvertTo-Json
    return
}
foreach ($required in @($pythonw, $CycleScript, $launcher, $ProtectScript)) {
    if (-not (Test-Path -LiteralPath $required)) { throw "missing: $required" }
}
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
$alreadyWrapped = $existing -and @($existing.Actions).Count -gt 0 -and
    @($existing.Actions | Where-Object { $_.Execute -eq $launcher }).Count -eq @($existing.Actions).Count
if ($alreadyWrapped) {
    $plan.registered = "ALREADY_WRAPPED"
} else {
    $action = New-ScheduledTaskAction -Execute $pythonw -Argument "-B `"$CycleScript`" run" `
        -WorkingDirectory (Split-Path $CycleScript)
    $trigger = New-ScheduledTaskTrigger -Daily -At ([datetime]::Today.Add([timespan]::Parse($At)))
    $settings = New-ScheduledTaskSettingsSet -Disable -MultipleInstances IgnoreNew -StartWhenAvailable `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 20) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
    $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
        -Principal $principal -Description "Idea Maker: propozycje skilli co 14 dni; bez automatycznego wdrozenia." `
        -Force | Out-Null
    & $ProtectScript -TaskName $TaskName -Runtime $HiddenLaunchRuntime -Apply | Out-Null
    $plan.registered = "REGISTERED_DISABLED_WRAPPED"
}
$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
$wrapped = @($task.Actions).Count -gt 0 -and
    @($task.Actions | Where-Object { $_.Execute -eq $launcher }).Count -eq @($task.Actions).Count
$plan.wrapped = $wrapped
if (-not $wrapped) { throw "refusing activation: action is not wrapped by HiddenLaunch" }
if ($Enable) {
    Enable-ScheduledTask -TaskName $TaskName | Out-Null
    $task = Get-ScheduledTask -TaskName $TaskName
}
$plan.state = [string]$task.State
$plan | ConvertTo-Json
