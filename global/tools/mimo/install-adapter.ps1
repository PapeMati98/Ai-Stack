[CmdletBinding()]
param([switch]$Apply)
$ErrorActionPreference = 'Stop'
$source = Join-Path $PSScriptRoot 'launch.ps1'
$targetDir = Join-Path $env:USERPROFILE '.local\lib\mimo'
$target = Join-Path $targetDir 'launch.ps1'
$binDir = Join-Path $env:USERPROFILE '.local\bin'
$cmd = Join-Path $binDir 'mimo.cmd'
$configSource = Join-Path $PSScriptRoot 'mimocode.json'
$configTarget = Join-Path $env:USERPROFILE '.mimocode\config\mimocode.json'
$pwsh = 'C:\Program Files\PowerShell\7\pwsh.exe'
if (-not (Test-Path -LiteralPath $pwsh -PathType Leaf)) { throw 'PowerShell 7 executable is required.' }
$wrapper = "@echo off`r`n`"$pwsh`" -NoLogo -NoProfile -File `"%USERPROFILE%\.local\lib\mimo\launch.ps1`" %*`r`nexit /b %ERRORLEVEL%`r`n"
$settings = @{MIMOCODE_ENABLE_ANALYSIS='false';MIMOCODE_DISABLE_SHARE='true';MIMOCODE_DISABLE_AUTOUPDATE='true';MIMOCODE_DISABLE_PRUNE='true'}
$envChanges = @($settings.GetEnumerator() | Sort-Object Key | ForEach-Object {
    [ordered]@{name=$_.Key; before=[Environment]::GetEnvironmentVariable($_.Key,'User'); after=$_.Value}
})
$onPath = @($env:PATH -split ';' | Where-Object { $_.TrimEnd('\') -ieq $binDir.TrimEnd('\') }).Count -gt 0
if (-not $onPath) { Write-Warning "Add $binDir to PATH or invoke $cmd by its full path." }
if (-not $Apply) {
    [ordered]@{source=$source; launcher=$target; command=$cmd; config=$configTarget; env_user=$envChanges; path_present=$onPath; deletes=$false; modifies_credentials=$false} | ConvertTo-Json -Depth 5
    return
}
foreach ($pair in @(@($source,$target),@($null,$cmd),@($configSource,$configTarget))) {
    $wanted = if ($pair[0]) { [IO.File]::ReadAllText($pair[0]) } else { $wrapper }
    if (Test-Path -LiteralPath $pair[1]) {
        $existing = [IO.File]::ReadAllText($pair[1])
        if ($existing -ne $wanted) {
            $backup = $pair[1] + '.before-' + [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfffffffZ')
            Copy-Item -LiteralPath $pair[1] -Destination $backup
        }
    }
    [IO.Directory]::CreateDirectory((Split-Path -Parent $pair[1])) | Out-Null
    [IO.File]::WriteAllText($pair[1],$wanted,[Text.UTF8Encoding]::new($false))
}
# Also protects direct native invocations started in later Windows sessions.
$receipt = Join-Path $targetDir ('install-receipt-' + [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfffffffZ') + '.json')
$receiptData = [ordered]@{status='PREPARED'; launcher=$target; command=$cmd; config=$configTarget; env_user=$envChanges; path_present=$onPath; deletes=$false; modifies_credentials=$false}
[IO.File]::WriteAllText($receipt,($receiptData | ConvertTo-Json -Depth 5),[Text.UTF8Encoding]::new($false))
foreach ($setting in $settings.GetEnumerator()) {
    [Environment]::SetEnvironmentVariable($setting.Key,$setting.Value,'User')
}
$receiptData.status = 'INSTALLED'
[IO.File]::WriteAllText($receipt,($receiptData | ConvertTo-Json -Depth 5),[Text.UTF8Encoding]::new($false))
$receiptData.receipt_path = $receipt
$receiptData | ConvertTo-Json -Depth 5
