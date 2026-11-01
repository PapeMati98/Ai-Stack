[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$SkillRoot = Split-Path -Parent $PSScriptRoot
$TestRoot = Join-Path $env:TEMP ('rcu-' + [Guid]::NewGuid().ToString('N').Substring(0, 8))
$ProfileRoot = Join-Path $TestRoot 'profile'
$AppDataRoot = Join-Path $TestRoot 'appdata'
$OldProfile = $env:USERPROFILE
$OldAppData = $env:APPDATA

try {
    $priorSkill = Join-Path $ProfileRoot '.claude\skills\remote-computer-use'
    New-Item -ItemType Directory -Force -Path $priorSkill | Out-Null
    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText((Join-Path $priorSkill 'SKILL.md'), 'prior-state', $encoding)

    $env:USERPROFILE = $ProfileRoot
    $env:APPDATA = $AppDataRoot
    $installer = Join-Path $SkillRoot 'scripts\install_skill.ps1'
    $deploy = & $installer | ConvertFrom-Json
    $verify = & $installer -Verify | ConvertFrom-Json
    $rollback = & $installer -RollbackTransaction $deploy.transaction_id | ConvertFrom-Json

    $checks = [ordered]@{
        deploy = $deploy.status
        verify = $verify.status
        rollback = $rollback.status
        restored_claude = ((Get-Content -LiteralPath (Join-Path $priorSkill 'SKILL.md') -Raw) -eq 'prior-state')
        codex_absent = (-not (Test-Path -LiteralPath (Join-Path $ProfileRoot '.codex\skills\remote-computer-use')))
        shared_absent = (-not (Test-Path -LiteralPath (Join-Path $ProfileRoot '.agents\skills\remote-computer-use')))
        antigravity_absent = (-not (Test-Path -LiteralPath (Join-Path $ProfileRoot '.gemini\config\skills\remote-computer-use')))
        grok_absent = (-not (Test-Path -LiteralPath (Join-Path $ProfileRoot '.grok\skills\remote-computer-use')))
        kimi_absent = (-not (Test-Path -LiteralPath (Join-Path $AppDataRoot 'kimi-desktop\daimon-share\daimon\skills\remote-computer-use')))
    }
    $failed = @($checks.GetEnumerator() | Where-Object {
        ($_.Key -in @('deploy', 'verify', 'rollback') -and $_.Value -notin @('DEPLOYED', 'PASS', 'ROLLED_BACK')) -or
        ($_.Key -notin @('deploy', 'verify', 'rollback') -and $_.Value -ne $true)
    })
    Write-Output ($checks | ConvertTo-Json -Compress)
    if ($failed.Count -gt 0) {
        throw "Installer regression failed: $($failed.Name -join ', ')"
    }
}
finally {
    $env:USERPROFILE = $OldProfile
    $env:APPDATA = $OldAppData
    $resolved = [System.IO.Path]::GetFullPath($TestRoot)
    $tempRoot = [System.IO.Path]::GetFullPath($env:TEMP).TrimEnd('\')
    if (-not $resolved.StartsWith($tempRoot + '\', [System.StringComparison]::OrdinalIgnoreCase)) {
        throw 'Refusing cleanup outside TEMP'
    }
    if (Test-Path -LiteralPath $resolved) {
        for ($attempt = 0; $attempt -lt 10 -and (Test-Path -LiteralPath $resolved); $attempt++) {
            Remove-Item -LiteralPath $resolved -Recurse -Force -ErrorAction SilentlyContinue
            if (Test-Path -LiteralPath $resolved) { Start-Sleep -Milliseconds 100 }
        }
        if (Test-Path -LiteralPath $resolved) {
            throw "Test cleanup did not converge inside TEMP: $resolved"
        }
    }
}
