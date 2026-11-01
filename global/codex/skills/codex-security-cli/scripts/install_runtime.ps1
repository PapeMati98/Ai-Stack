[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$Package = "@openai/codex-security"
$Version = "0.1.1"
$ExpectedTreeHash = "87e8455013bf8672a43c46f5f2a0187f64628624551ca9e3de2cdc12e15594bc"
$ExpectedLockHash = "e7257e397f279162635448c55d9fcfefcb980082e60da2a23619a2d5c997ce42"
$ExpectedBinaryHash = "8464effc0af5c05c2d58703de9f43c315b0c9e4e66299937c5cf8a304294e326"
$ExpectedFileCount = 178
$ExpectedNodeHash = "3331e1ffe19874215472217c5e94f5a0c6d8e18c4ac7111d3937aa0ad5e9b4a5"

$ManifestPath = Join-Path $PSScriptRoot "..\references\runtime-manifest.json"
$Manifest = Get-Content -LiteralPath $ManifestPath -Raw | ConvertFrom-Json
$UserProfile = [Environment]::GetFolderPath([Environment+SpecialFolder]::UserProfile)
$ProgramFiles = [Environment]::GetFolderPath([Environment+SpecialFolder]::ProgramFiles)
$NodePath = Join-Path $ProgramFiles "nodejs\node.exe"
$NpmPath = Join-Path $ProgramFiles "nodejs\npm.cmd"

function Get-TreeSha256 {
    param([Parameter(Mandatory = $true)][string]$Root)

    $rootPath = (Resolve-Path -LiteralPath $Root).Path.TrimEnd("\")
    $sha = [System.Security.Cryptography.SHA256]::Create()
    $utf8 = [System.Text.UTF8Encoding]::new($false)
    try {
        $filesByRelativePath = @{}
        foreach ($file in Get-ChildItem -LiteralPath $rootPath -File -Recurse) {
            $relative = $file.FullName.Substring($rootPath.Length).TrimStart("\").Replace("\", "/")
            $filesByRelativePath[$relative] = $file
        }
        $relativePaths = [string[]]$filesByRelativePath.Keys
        [Array]::Sort($relativePaths, [System.StringComparer]::Ordinal)
        foreach ($relative in $relativePaths) {
            $file = $filesByRelativePath[$relative]
            $prefix = $utf8.GetBytes($relative + [char]0)
            [void]$sha.TransformBlock($prefix, 0, $prefix.Length, $null, 0)
            $bytes = [System.IO.File]::ReadAllBytes($file.FullName)
            [void]$sha.TransformBlock($bytes, 0, $bytes.Length, $null, 0)
            $separator = [byte[]](0)
            [void]$sha.TransformBlock($separator, 0, 1, $null, 0)
        }
        [void]$sha.TransformFinalBlock([byte[]]::new(0), 0, 0)
        return ([BitConverter]::ToString($sha.Hash)).Replace("-", "").ToLowerInvariant()
    }
    finally {
        $sha.Dispose()
    }
}

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

if (
    $Manifest.package -ne $Package -or
    $Manifest.version -ne $Version -or
    $Manifest.packageTreeSha256 -ne $ExpectedTreeHash -or
    $Manifest.packageLockSha256 -ne $ExpectedLockHash -or
    $Manifest.binarySha256 -ne $ExpectedBinaryHash -or
    $Manifest.packageFileCount -ne $ExpectedFileCount -or
    $Manifest.nodeSha256 -ne $ExpectedNodeHash
) {
    throw "Manifest runtime nie jest zgodny z przypietymi wartosciami instalatora."
}

$relativeParts = @($Manifest.runtimeRelativePath -split "[\\/]+" | Where-Object { $_ })
if (
    [System.IO.Path]::IsPathRooted($Manifest.runtimeRelativePath) -or
    $relativeParts -contains ".."
) {
    throw "Manifest zawiera niebezpieczna sciezke runtime."
}

$RuntimeRoot = [System.IO.Path]::GetFullPath((Join-Path $UserProfile $Manifest.runtimeRelativePath))
$RuntimeParent = Split-Path -Parent $RuntimeRoot
$RuntimeName = Split-Path -Leaf $RuntimeRoot
$uniqueSuffix = "$(Get-Date -Format 'yyyyMMdd-HHmmss')-$([guid]::NewGuid().ToString('N'))"
$StagingRoot = Join-Path $RuntimeParent ".$RuntimeName.staging-$uniqueSuffix"
$BackupRoot = Join-Path $RuntimeParent ".$RuntimeName.backup-$uniqueSuffix"
$ExistingMoved = $false
$Committed = $false
$Mutex = $null
$MutexAcquired = $false

Assert-ChildPath -Path $RuntimeRoot -Parent $UserProfile -Label "Runtime"
Assert-ChildPath -Path $StagingRoot -Parent $RuntimeParent -Label "Katalog staging"
Assert-ChildPath -Path $BackupRoot -Parent $RuntimeParent -Label "Katalog backup"
Assert-NoReparsePath -Path $UserProfile -Label "Profil uzytkownika"

if ([System.IO.Path]::GetFullPath($Manifest.nodePath) -ne [System.IO.Path]::GetFullPath($NodePath)) {
    throw "Manifest wskazuje inny Node.js niz kanoniczny Program Files."
}
if (-not (Test-Path -LiteralPath $NodePath -PathType Leaf)) {
    throw "Brak przypietego Node.js: $NodePath"
}
if (-not (Test-Path -LiteralPath $NpmPath -PathType Leaf)) {
    throw "Brak przypietego npm.cmd: $NpmPath"
}
$nodeHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $NodePath).Hash.ToLowerInvariant()
if ($nodeHash -ne $ExpectedNodeHash) {
    throw "Niezgodny SHA256 przypietego Node.js."
}

New-Item -ItemType Directory -Path $RuntimeParent -Force | Out-Null
Assert-NoReparsePath -Path $RuntimeParent -Label "Katalog runtime"

$sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value.Replace("-", "_")
$mutexName = "Local\MateuszAI_CodexSecurityRuntime_${sid}_${Version}"
$Mutex = [System.Threading.Mutex]::new($false, $mutexName)
try {
    try {
        $MutexAcquired = $Mutex.WaitOne(0)
    }
    catch [System.Threading.AbandonedMutexException] {
        $MutexAcquired = $true
    }
    if (-not $MutexAcquired) {
        throw "Inna instalacja Codex Security runtime jest juz uruchomiona."
    }

    if (
        (Test-Path -LiteralPath $StagingRoot) -or
        (Test-Path -LiteralPath $BackupRoot)
    ) {
        throw "Wylosowany katalog staging albo backup juz istnieje."
    }
    if (Test-Path -LiteralPath $RuntimeRoot) {
        Assert-NoReparsePath -Path $RuntimeRoot -Label "Istniejacy runtime"
    }

    New-Item -ItemType Directory -Path $StagingRoot | Out-Null
    Assert-NoReparsePath -Path $StagingRoot -Label "Katalog staging"

    Write-Host "Instaluje przypiety runtime $Package@$Version do katalogu staging..."
    & $NpmPath install --prefix $StagingRoot --ignore-scripts --save-exact "$Package@$Version"
    if ($LASTEXITCODE -ne 0) {
        throw "npm install zakonczyl sie kodem $LASTEXITCODE."
    }

    $packageRoot = Join-Path $StagingRoot "node_modules\@openai\codex-security"
    $packageJson = Join-Path $packageRoot "package.json"
    $binary = Join-Path $packageRoot "bin\codex-security.mjs"
    $lock = Join-Path $StagingRoot "package-lock.json"

    $metadata = Get-Content -LiteralPath $packageJson -Raw | ConvertFrom-Json
    if ($metadata.name -ne $Package -or $metadata.version -ne $Version) {
        throw "Nieoczekiwany pakiet: $($metadata.name)@$($metadata.version)."
    }

    $fileCount = (Get-ChildItem -LiteralPath $packageRoot -File -Recurse).Count
    $treeHash = Get-TreeSha256 -Root $packageRoot
    $lockHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $lock).Hash.ToLowerInvariant()
    $binaryHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $binary).Hash.ToLowerInvariant()

    if ($fileCount -ne $ExpectedFileCount) {
        throw "Niezgodna liczba plikow pakietu: $fileCount (oczekiwano $ExpectedFileCount)."
    }
    if ($treeHash -ne $ExpectedTreeHash) {
        throw "Niezgodny SHA256 drzewa pakietu."
    }
    if ($lockHash -ne $ExpectedLockHash) {
        throw "Niezgodny SHA256 package-lock.json."
    }
    if ($binaryHash -ne $ExpectedBinaryHash) {
        throw "Niezgodny SHA256 binarki."
    }

    Write-Host "Uruchamiam npm audit w katalogu staging..."
    & $NpmPath audit --prefix $StagingRoot --omit=dev
    if ($LASTEXITCODE -ne 0) {
        throw "npm audit wykryl problem albo zakonczyl sie bledem."
    }

    Write-Host "Weryfikuje podpisy rejestru i provenance..."
    & $NpmPath audit signatures --prefix $StagingRoot
    if ($LASTEXITCODE -ne 0) {
        throw "npm audit signatures zakonczyl sie bledem."
    }

    if (Test-Path -LiteralPath $RuntimeRoot) {
        Move-Item -LiteralPath $RuntimeRoot -Destination $BackupRoot
        $ExistingMoved = $true
    }

    Move-Item -LiteralPath $StagingRoot -Destination $RuntimeRoot
    $Committed = $true

    Write-Host "Runtime gotowy: $RuntimeRoot"
    if ($ExistingMoved) {
        Write-Host "Poprzedni runtime zachowano: $BackupRoot"
    }
    Write-Host "Pakiet: $Package@$Version"
    Write-Host "Node.js: $NodePath"
    Write-Host "SHA256 Node.js: $nodeHash"
    Write-Host "SHA256 drzewa: $treeHash"
    Write-Host "Nie wykonano logowania, skanu ani rejestracji pluginu."
}
catch {
    if (-not $Committed -and $ExistingMoved -and -not (Test-Path -LiteralPath $RuntimeRoot)) {
        Assert-NoReparsePath -Path $BackupRoot -Label "Kopia poprzedniego runtime"
        Move-Item -LiteralPath $BackupRoot -Destination $RuntimeRoot
    }
    throw
}
finally {
    if (Test-Path -LiteralPath $StagingRoot) {
        Assert-NoReparsePath -Path $StagingRoot -Label "Katalog staging do usuniecia"
        Remove-Item -LiteralPath $StagingRoot -Recurse -Force
    }
    if ($MutexAcquired -and $null -ne $Mutex) {
        $Mutex.ReleaseMutex()
    }
    if ($null -ne $Mutex) {
        $Mutex.Dispose()
    }
}
