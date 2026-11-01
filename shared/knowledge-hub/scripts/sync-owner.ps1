[CmdletBinding()]
param(
    [ValidateSet('configure', 'status', 'publish-main', 'import-robert')]
    [string]$Action = 'status',
    [string]$HubRoot = '',
    [string]$RemoteName = 'origin',
    [string]$RemoteUrl = 'lab-server:/srv/ai-lab/git/shared-agent-knowledge-hub.git'
)

$ErrorActionPreference = 'Stop'

if ([string]::IsNullOrWhiteSpace($HubRoot)) {
    $HubRoot = Split-Path -Parent $PSScriptRoot
}
$hub = (Resolve-Path -LiteralPath $HubRoot -ErrorAction Stop).Path
$vault = Join-Path $hub 'vault'
$gateway = Join-Path $hub 'skills\llm-wiki\scripts\knowledge.ps1'
$robertRef = "refs/remotes/$RemoteName/contrib/robert-hermes"
$mainRef = "refs/remotes/$RemoteName/main"

if (-not (Test-Path -LiteralPath (Join-Path $hub '.git'))) { throw "Not a Git checkout: $hub" }
if (-not (Test-Path -LiteralPath $gateway -PathType Leaf)) { throw "Missing gateway: $gateway" }

function Invoke-Git {
    param([string[]]$Arguments, [switch]$AllowFailure)
    $previousErrorAction = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        $output = @(& git -c "safe.directory=$hub" -C $hub @Arguments 2>&1)
        $exitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousErrorAction
    }
    if ($exitCode -ne 0 -and -not $AllowFailure) {
        throw "git $($Arguments -join ' ') failed ($exitCode): $($output -join [Environment]::NewLine)"
    }
    return [PSCustomObject]@{ ExitCode = $exitCode; Output = $output }
}

function Get-RefSha {
    param([string]$Ref)
    $result = Invoke-Git -Arguments @('rev-parse', '--verify', $Ref) -AllowFailure
    if ($result.ExitCode -ne 0) { return $null }
    return [string]$result.Output[0]
}

function Test-FreshIntakePath {
    param([string]$Path)
    $normalized = $Path.Trim('"').Replace('\', '/')
    return (
        $normalized -like 'vault/events/*' -or
        $normalized -like 'vault/proposals/*' -or
        $normalized -like 'vault/raw/sources/*'
    )
}

function Assert-CleanMain {
    $branch = (Invoke-Git -Arguments @('symbolic-ref', '--quiet', '--short', 'HEAD')).Output[0]
    if ([string]$branch -ne 'main') { throw "Owner sync requires branch main, got '$branch'." }
    $changes = @(Invoke-Git -Arguments @('status', '--porcelain=v1', '--untracked-files=all')).Output
    $blocking = @()
    foreach ($line in $changes) {
        if ([string]::IsNullOrWhiteSpace($line)) { continue }
        if ($line.Length -lt 4) { $blocking += $line; continue }
        $statusCode = $line.Substring(0, 2)
        $relPath = $line.Substring(3).Trim()
        if ($statusCode -eq '??' -and (Test-FreshIntakePath -Path $relPath)) {
            continue
        }
        $blocking += $line
    }
    if ($blocking.Count -gt 0) {
        throw "Owner sync requires a clean worktree; found $($blocking.Count) change(s): $($blocking -join ', ')."
    }
}

function Assert-LintClean {
    $start = New-Object System.Diagnostics.ProcessStartInfo
    $start.FileName = (Get-Command powershell.exe -CommandType Application -ErrorAction Stop).Source
    $start.Arguments = '-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "{0}" -Action lint -Vault "{1}"' -f $gateway, $vault
    $start.UseShellExecute = $false
    $start.CreateNoWindow = $true
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true
    $start.StandardOutputEncoding = New-Object Text.UTF8Encoding($false)
    $start.StandardErrorEncoding = New-Object Text.UTF8Encoding($false)
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $start
    try {
        if (-not $process.Start()) { throw 'Knowledge lint gateway did not start.' }
        $stdoutTask = $process.StandardOutput.ReadToEndAsync()
        $stderrTask = $process.StandardError.ReadToEndAsync()
        $process.WaitForExit()
        $stdout = $stdoutTask.GetAwaiter().GetResult()
        $stderr = $stderrTask.GetAwaiter().GetResult()
        $exitCode = $process.ExitCode
    }
    finally { $process.Dispose() }
    if ($exitCode -ne 0) {
        throw "Knowledge lint gateway failed ($exitCode): $($stderr.Trim())"
    }
    $jsonText = $stdout.Trim()
    if (-not $jsonText.StartsWith('{') -or -not $jsonText.EndsWith('}')) {
        throw 'Knowledge lint gateway must return one JSON object.'
    }
    $lint = $jsonText | ConvertFrom-Json -ErrorAction Stop
    foreach ($name in @('error_count', 'warning_count')) {
        $field = $lint.PSObject.Properties[$name]
        if (-not $field -or -not ($field.Value -is [int] -or $field.Value -is [long]) -or $field.Value -lt 0) {
            throw "Knowledge lint gateway returned an invalid $name."
        }
    }
    if ($lint.error_count -ne 0 -or $lint.warning_count -ne 0) {
        throw "Knowledge lint failed: $($lint.error_count) error(s), $($lint.warning_count) warning(s)."
    }
}

function Test-Ancestor {
    param([string]$Older, [string]$Newer)
    $result = Invoke-Git -Arguments @('merge-base', '--is-ancestor', $Older, $Newer) -AllowFailure
    return $result.ExitCode -eq 0
}

function Test-RobertPath {
    param([string]$Path)
    return (
        $Path -match '^vault/raw/sources/[0-9]{4}/[0-9]{2}/src-[A-Za-z0-9._-]+/(original\.[A-Za-z0-9]+|source\.json)$' -or
        $Path -match '^vault/proposals/robert-hermes/prop-[A-Za-z0-9._-]+\.md$' -or
        $Path -match '^vault/events/[0-9]{4}/[0-9]{2}/evt-[A-Za-z0-9._-]+\.json$'
    )
}

function Assert-RobertDiff {
    param([string]$Base, [string]$Tip)
    $lines = @(Invoke-Git -Arguments @('diff', '--name-status', "$Base..$Tip")).Output
    if ($lines.Count -eq 0) { throw 'Robert branch contains no contribution files.' }
    foreach ($line in $lines) {
        $parts = ([string]$line).Split(@("`t"), 2, [StringSplitOptions]::None)
        if ($parts.Count -ne 2 -or $parts[0] -ne 'A') { throw "Robert diff is not append-only: $line" }
        if (-not (Test-RobertPath -Path $parts[1])) { throw "Robert diff contains forbidden path: $($parts[1])" }
    }
}

function Ensure-RemoteConfigured {
    $existing = Invoke-Git -Arguments @('remote', 'get-url', $RemoteName) -AllowFailure
    if ($existing.ExitCode -ne 0) {
        [void](Invoke-Git -Arguments @('remote', 'add', $RemoteName, $RemoteUrl))
    }
    elseif ([string]$existing.Output[0] -ne $RemoteUrl) {
        throw "Remote '$RemoteName' already points to '$($existing.Output[0])'; refusing to overwrite it."
    }
    $fetchSpec = '+refs/heads/*:refs/remotes/{0}/*' -f $RemoteName
    [void](Invoke-Git -Arguments @('config', "remote.$RemoteName.fetch", $fetchSpec))
}

switch ($Action) {
    'configure' {
        Ensure-RemoteConfigured
        [void](Invoke-Git -Arguments @('fetch', '--prune', $RemoteName))
    }
    'status' {
        $remote = Invoke-Git -Arguments @('remote', 'get-url', $RemoteName) -AllowFailure
        $branch = Invoke-Git -Arguments @('symbolic-ref', '--quiet', '--short', 'HEAD') -AllowFailure
        $changes = @(Invoke-Git -Arguments @('status', '--porcelain=v1', '--untracked-files=all')).Output
        Assert-LintClean
        [PSCustomObject]@{
            action = 'status'
            remote = if ($remote.ExitCode -eq 0) { [string]$remote.Output[0] } else { $null }
            branch = if ($branch.ExitCode -eq 0) { [string]$branch.Output[0] } else { $null }
            head = [string](Invoke-Git -Arguments @('rev-parse', 'HEAD')).Output[0]
            origin_main = Get-RefSha -Ref $mainRef
            robert_branch = Get-RefSha -Ref $robertRef
            dirty_count = $changes.Count
            lint = 'clean'
        } | ConvertTo-Json -Depth 3
    }
    'publish-main' {
        Ensure-RemoteConfigured
        Assert-CleanMain
        Assert-LintClean
        [void](Invoke-Git -Arguments @('fetch', '--prune', $RemoteName))
        $originMain = Get-RefSha -Ref $mainRef
        if ($originMain -and -not (Test-Ancestor -Older $mainRef -Newer 'HEAD')) {
            throw 'Local main is not a fast-forward of origin/main; review the divergence manually.'
        }
        [void](Invoke-Git -Arguments @('push', $RemoteName, 'HEAD:refs/heads/main'))
    }
    'import-robert' {
        Ensure-RemoteConfigured
        Assert-CleanMain
        Assert-LintClean
        [void](Invoke-Git -Arguments @('fetch', '--prune', $RemoteName))
        $originMain = Get-RefSha -Ref $mainRef
        $robert = Get-RefSha -Ref $robertRef
        if (-not $originMain -or -not $robert) { throw 'Remote main or Robert contributor branch is missing.' }
        if ([string](Invoke-Git -Arguments @('rev-parse', 'HEAD')).Output[0] -ne $originMain) {
            throw 'Local main must exactly match origin/main before importing Robert.'
        }
        if (-not (Test-Ancestor -Older $mainRef -Newer $robertRef)) {
            throw 'Robert branch is not based on the current remote main.'
        }
        Assert-RobertDiff -Base $mainRef -Tip $robertRef
        [void](Invoke-Git -Arguments @('merge', '--ff-only', $robertRef))
        Assert-LintClean
        [void](Invoke-Git -Arguments @('push', $RemoteName, 'HEAD:refs/heads/main'))
    }
}

if ($Action -ne 'status') {
    Write-Host "Owner knowledge sync completed: $Action"
    Write-Host "Hub: $hub"
    Write-Host "Remote: $RemoteName -> $RemoteUrl"
}
