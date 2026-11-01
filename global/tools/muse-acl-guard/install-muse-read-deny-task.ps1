param(
    [switch]$Uninstall,
    [int]$ApplyIntervalMinutes = 5
)
# Installs two per-user (non-elevated) scheduled tasks for muse_read_deny.ps1:
# - "AgentStack Muse Read Deny": -Mode Apply every $ApplyIntervalMinutes (cheap when nothing is missing;
#   re-asserts file roots such as .claude.json that an atomic replace can strip, adds new D:\ entries);
# - "AgentStack Muse Read Deny Scan": -Mode ScanDescendants daily at 03:30 (protected-DACL and
#   explicit-ALLOW descendants).
# Uninstall only unregisters the tasks; the ACEs stay until muse_read_deny.ps1 -Mode Rollback.
$ErrorActionPreference = 'Stop'
$applyTask = 'AgentStack Muse Read Deny'
$scanTask = 'AgentStack Muse Read Deny Scan'
$installDir = Join-Path $env:USERPROFILE '.agents\maintenance\muse-acl-guard'

if ($Uninstall) {
    foreach ($t in @($applyTask, $scanTask)) { Unregister-ScheduledTask -TaskName $t -Confirm:$false -ErrorAction SilentlyContinue }
    Write-Host "Wyrejestrowano: $applyTask, $scanTask (ACE zostaja; cofniecie: muse_read_deny.ps1 -Mode Rollback)"
    return
}

New-Item -ItemType Directory -Force -Path $installDir | Out-Null
$hashes = [ordered]@{}
foreach ($f in @('muse_read_deny.ps1', 'muse-read-deny.json')) {
    $src = Join-Path $PSScriptRoot $f; $dst = Join-Path $installDir $f
    Copy-Item -LiteralPath $src -Destination $dst -Force
    $h = (Get-FileHash -LiteralPath $dst -Algorithm SHA256).Hash
    if ($h -ne (Get-FileHash -LiteralPath $src -Algorithm SHA256).Hash) { throw "Hash mismatch po kopiowaniu: $f" }
    $hashes[$f] = $h
}
$runner = Join-Path $installDir 'muse_read_deny.ps1'
$settings = New-ScheduledTaskSettingsSet -Disable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Hours 3) -MultipleInstances IgnoreNew -StartWhenAvailable -Hidden
$principal = New-ScheduledTaskPrincipal -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited

$applyAction = New-ScheduledTaskAction -Execute 'powershell.exe' -WorkingDirectory $installDir `
    -Argument ('-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{0}" -Mode Apply -LockTimeoutSeconds 60' -f $runner)
$applyTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes $ApplyIntervalMinutes) -RepetitionDuration (New-TimeSpan -Days 3650)
Register-ScheduledTask -TaskName $applyTask -Action $applyAction -Trigger $applyTrigger -Settings $settings -Principal $principal `
    -Description 'Blokada odczytu dla sandboxu Muse (MuseSandboxUsers): poswiadczenia, profile przegladarek, dane klientow.' -Force | Out-Null

$scanAction = New-ScheduledTaskAction -Execute 'powershell.exe' -WorkingDirectory $installDir `
    -Argument ('-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{0}" -Mode ScanDescendants -LockTimeoutSeconds 1800' -f $runner)
$scanTrigger = New-ScheduledTaskTrigger -Daily -At '03:30'
Register-ScheduledTask -TaskName $scanTask -Action $scanAction -Trigger $scanTrigger -Settings $settings -Principal $principal `
    -Description 'Skan wyjatkow pod blokada odczytu Muse (chronione DACL, jawne ALLOW).' -Force | Out-Null

$protector = Join-Path $PSScriptRoot '..\hidden-launch\Protect-ConsoleTasks.ps1'
& $protector -TaskName $applyTask,$scanTask -Apply | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Hidden task-action protection failed' }
Enable-ScheduledTask -TaskName $applyTask -ErrorAction Stop | Out-Null
Enable-ScheduledTask -TaskName $scanTask -ErrorAction Stop | Out-Null

[pscustomobject]@{ status = 'INSTALLED'; tasks = @($applyTask, $scanTask); runner = $runner; sha256 = $hashes } | ConvertTo-Json -Depth 3
