[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = "High")]
param(
    [Parameter(Mandatory = $true)]
    [string]$BackupRoot,

    [switch]$ConfirmRollback
)

$ErrorActionPreference = "Stop"

function Assert-ChildPath {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Parent,
        [Parameter(Mandatory = $true)][string]$Label
    )

    $fullPath = [System.IO.Path]::GetFullPath($Path)
    $fullParent = [System.IO.Path]::GetFullPath($Parent).TrimEnd("\")
    if (-not $fullPath.StartsWith($fullParent + "\", [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "$Label wykracza poza dozwolony katalog: $fullPath"
    }
}

function Assert-NoReparsePath {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Label
    )

    $fullPath = [System.IO.Path]::GetFullPath($Path)
    $root = [System.IO.Path]::GetPathRoot($fullPath)
    $current = $root
    $relative = $fullPath.Substring($root.Length)
    foreach ($segment in ($relative -split "[\\/]+" | Where-Object { $_ })) {
        $current = Join-Path $current $segment
        if (Test-Path -LiteralPath $current) {
            $item = Get-Item -LiteralPath $current -Force
            if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "$Label przechodzi przez reparse point: $current"
            }
        }
    }
}

if (-not $ConfirmRollback) {
    throw "Rollback wymaga jawnego parametru -ConfirmRollback."
}

$UserProfile = [Environment]::GetFolderPath([Environment+SpecialFolder]::UserProfile)
$LocalAppData = [Environment]::GetFolderPath([Environment+SpecialFolder]::LocalApplicationData)
$AllowedBackupBase = [System.IO.Path]::GetFullPath(
    (Join-Path $LocalAppData "agent-stack-backups\codex-security-cli")
)
$BackupRoot = [System.IO.Path]::GetFullPath($BackupRoot)

Assert-ChildPath -Path $BackupRoot -Parent $AllowedBackupBase -Label "BackupRoot"
if ([System.IO.Path]::GetFullPath((Split-Path -Parent $BackupRoot)) -ne $AllowedBackupBase) {
    throw "BackupRoot musi byc bezposrednia, wersjonowana kopia pod: $AllowedBackupBase"
}
if (-not (Test-Path -LiteralPath $BackupRoot -PathType Container)) {
    throw "Nie istnieje katalog kopii zapasowej: $BackupRoot"
}
Assert-NoReparsePath -Path $AllowedBackupBase -Label "Baza kopii zapasowych"
Assert-NoReparsePath -Path $BackupRoot -Label "BackupRoot"

$targets = @(
    @{ Name = "codex"; Root = Join-Path $UserProfile ".codex\skills" },
    @{ Name = "claude"; Root = Join-Path $UserProfile ".claude\skills" },
    @{ Name = "agents"; Root = Join-Path $UserProfile ".agents\skills" },
    @{ Name = "gemini"; Root = Join-Path $UserProfile ".gemini\config\skills" },
    @{ Name = "grok"; Root = Join-Path $UserProfile ".grok\skills" }
)

$plan = @()
foreach ($target in $targets) {
    $skillsRoot = [System.IO.Path]::GetFullPath($target.Root)
    $skillPath = [System.IO.Path]::GetFullPath((Join-Path $skillsRoot "codex-security-cli"))
    $auditBackup = [System.IO.Path]::GetFullPath(
        (Join-Path $BackupRoot "$($target.Name)\security-audit\SKILL.md")
    )
    $auditTarget = [System.IO.Path]::GetFullPath(
        (Join-Path $skillsRoot "security-audit\SKILL.md")
    )

    Assert-ChildPath -Path $skillsRoot -Parent $UserProfile -Label "Katalog aktywnych skilli"
    Assert-ChildPath -Path $skillPath -Parent $skillsRoot -Label "Aktywny skill"
    Assert-ChildPath -Path $auditTarget -Parent $skillsRoot -Label "Aktywny audyt"
    Assert-ChildPath -Path $auditBackup -Parent $BackupRoot -Label "Kopia audytu"
    Assert-NoReparsePath -Path $skillsRoot -Label "Katalog aktywnych skilli"
    Assert-NoReparsePath -Path $skillPath -Label "Aktywny skill"
    Assert-NoReparsePath -Path $auditTarget -Label "Aktywny audyt"
    Assert-NoReparsePath -Path $auditBackup -Label "Kopia audytu"

    if (-not (Test-Path -LiteralPath $auditBackup -PathType Leaf)) {
        throw "Brak kopii security-audit dla $($target.Name): $auditBackup"
    }
    if (-not (Test-Path -LiteralPath (Split-Path -Parent $auditTarget) -PathType Container)) {
        throw "Brak docelowego katalogu audytu dla $($target.Name)."
    }

    $plan += [pscustomobject]@{
        Name = $target.Name
        SkillsRoot = $skillsRoot
        SkillPath = $skillPath
        AuditBackup = $auditBackup
        AuditTarget = $auditTarget
    }
}

$sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value.Replace("-", "_")
$Mutex = [System.Threading.Mutex]::new(
    $false,
    "Local\MateuszAI_CodexSecurityRollback_$sid"
)
$MutexAcquired = $false
try {
    try {
        $MutexAcquired = $Mutex.WaitOne(0)
    }
    catch [System.Threading.AbandonedMutexException] {
        $MutexAcquired = $true
    }
    if (-not $MutexAcquired) {
        throw "Inny rollback Codex Security jest juz uruchomiony."
    }

    foreach ($item in $plan) {
        if ($PSCmdlet.ShouldProcess($item.SkillsRoot, "Przywroc security-audit i usun codex-security-cli")) {
            Copy-Item -LiteralPath $item.AuditBackup -Destination $item.AuditTarget -Force
            if (Test-Path -LiteralPath $item.SkillPath) {
                Assert-NoReparsePath -Path $item.SkillPath -Label "Aktywny skill do usuniecia"
                Remove-Item -LiteralPath $item.SkillPath -Recurse -Force
            }
            Write-Host "Wycofano komponent dla: $($item.Name)"
        }
    }
}
finally {
    if ($MutexAcquired) {
        $Mutex.ReleaseMutex()
    }
    $Mutex.Dispose()
}

if ($WhatIfPreference) {
    Write-Host "Dry-run rollbacku zakonczony. Nie zmieniono aktywnych kopii."
}
else {
    Write-Host "Rollback aktywnych kopii zakonczony."
}
Write-Host "Runtime i wyniki skanow pozostaly bez zmian."
