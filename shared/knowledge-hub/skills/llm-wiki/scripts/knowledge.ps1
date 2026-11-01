[CmdletBinding()]
param(
    [ValidateSet('init', 'status', 'stage-source', 'new-proposal', 'checkpoint-intake', 'record-handoff', 'record-session', 'project-brief', 'inventory', 'begin-task', 'verify-context', 'acquire-lock', 'release-lock', 'record-curation', 'refresh', 'lint')]
    [string]$Action = 'status',
    [string]$Vault,
    [string]$Path,
    [string]$Title,
    [string]$Origin = '',
    [string]$Agent = 'unknown',
    [ValidateSet('public', 'internal', 'confidential', 'restricted')]
    [string]$Classification = 'internal',
    [string[]]$SourceId = @(),
    [string]$LockId = '',
    [ValidateRange(1, 2147483647)]
    [Nullable[int]]$OwnerPid = $null,
    [string]$Summary = '',
    [string[]]$Page = @(),
    [string[]]$ProposalId = @(),
    [string]$ProjectSlug = '',
    [string]$StateSummary = '',
    [string[]]$TargetPage = @(),
    [ValidateRange(1, 50)]
    [int]$Limit = 10,
    [ValidateRange(0, 1000000)]
    [int]$Offset = 0,
    [string]$Query = '',
    [string]$SessionId = '',
    [string]$CheckpointId = '',
    [ValidateSet('', 'COMPLETED', 'BLOCKED', 'INTERRUPTED', 'NO_DURABLE_CHANGE')]
    [string]$Outcome = '',
    [string]$ReceiptRoot = '',
    [string]$LockRoot = '',
    [string]$ContextReceipt = '',
    [switch]$RequireContext,
    [switch]$RecoverStaleLock,
    [switch]$LocalOnly,
    [ValidateNotNullOrEmpty()]
    [ValidatePattern('\S')]
    [string]$RetainTempRoot = ''
)

$ErrorActionPreference = 'Stop'

function Write-Utf8NoBom {
    param([string]$FilePath, [string]$Content)
    $parent = Split-Path -Parent $FilePath
    if ($parent -and -not (Test-Path -LiteralPath $parent)) {
        New-Item -ItemType Directory -Force -Path $parent | Out-Null
    }
    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($FilePath, $Content, $encoding)
}

function Read-Utf8Text {
    param([string]$FilePath)
    $encoding = New-Object System.Text.UTF8Encoding($false, $true)
    return [System.IO.File]::ReadAllText($FilePath, $encoding)
}

function Get-Sha256Hex {
    param([string]$FilePath)
    $stream = $null
    $sha256 = $null
    try {
        $stream = [System.IO.File]::OpenRead($FilePath)
        $sha256 = [System.Security.Cryptography.SHA256]::Create()
        $hashBytes = $sha256.ComputeHash($stream)
        return ([System.BitConverter]::ToString($hashBytes)).Replace('-', '').ToLowerInvariant()
    }
    finally {
        if ($sha256) { $sha256.Dispose() }
        if ($stream) { $stream.Dispose() }
    }
}

function Write-JsonOutput {
    param($Value)
    Write-Output ($Value | ConvertTo-Json -Depth 12)
}

function Get-NormalizedPath {
    param([string]$Value)
    return [IO.Path]::GetFullPath($Value).TrimEnd('\')
}

function Get-LocalMachineName {
    $machine = [Environment]::MachineName
    if ([string]::IsNullOrWhiteSpace($machine)) {
        throw 'Windows did not return a trusted local machine identity.'
    }
    return $machine
}

function Invoke-IsolatedGit {
    param(
        [string]$GitRoot,
        [string[]]$Arguments
    )

    $savedGitEnvironment = [ordered]@{}
    foreach ($entry in Get-ChildItem Env: | Where-Object { $_.Name -like 'GIT_*' }) {
        $savedGitEnvironment[$entry.Name] = [string]$entry.Value
    }
    try {
        foreach ($entry in @(Get-ChildItem Env: | Where-Object { $_.Name -like 'GIT_*' })) {
            Remove-Item -LiteralPath ('Env:' + $entry.Name) -ErrorAction SilentlyContinue
        }
        $env:GIT_TERMINAL_PROMPT = '0'
        $env:GIT_ATTR_NOSYSTEM = '1'
        $env:GIT_CONFIG_NOSYSTEM = '1'
        $env:GIT_NO_REPLACE_OBJECTS = '1'
        $gitArguments = @(
            '-c', "safe.directory=$GitRoot",
            '-c', 'core.quotePath=false',
            '-c', 'core.fsmonitor=false',
            '-C', $GitRoot
        ) + $Arguments
        $output = @(& git @gitArguments 2>$null)
        return [PSCustomObject]@{
            ExitCode = $LASTEXITCODE
            Output = @($output)
        }
    }
    finally {
        foreach ($entry in @(Get-ChildItem Env: | Where-Object { $_.Name -like 'GIT_*' })) {
            Remove-Item -LiteralPath ('Env:' + $entry.Name) -ErrorAction SilentlyContinue
        }
        foreach ($name in $savedGitEnvironment.Keys) {
            [Environment]::SetEnvironmentVariable($name, $savedGitEnvironment[$name], 'Process')
        }
    }
}

function Get-Slug {
    param([string]$Value)
    if ([string]::IsNullOrWhiteSpace($Value)) { return 'item' }
    $normalized = $Value.Normalize([Text.NormalizationForm]::FormD)
    $builder = New-Object Text.StringBuilder
    foreach ($char in $normalized.ToCharArray()) {
        $category = [Globalization.CharUnicodeInfo]::GetUnicodeCategory($char)
        if ($category -ne [Globalization.UnicodeCategory]::NonSpacingMark) {
            [void]$builder.Append($char)
        }
    }
    $slug = $builder.ToString().Normalize([Text.NormalizationForm]::FormC).ToLowerInvariant()
    $slug = [regex]::Replace($slug, '[^a-z0-9]+', '-')
    $slug = $slug.Trim('-')
    if ([string]::IsNullOrWhiteSpace($slug)) { return 'item' }
    if ($slug.Length -gt 64) { $slug = $slug.Substring(0, 64).Trim('-') }
    return $slug
}

function ConvertTo-YamlSingleQuoted {
    param([string]$Value)
    return "'" + $Value.Replace("'", "''") + "'"
}

function Resolve-VaultPath {
    param([string]$Requested)

    $candidate = $Requested
    if ([string]::IsNullOrWhiteSpace($candidate)) {
        $candidate = $env:AGENT_KNOWLEDGE_VAULT
    }
    if ([string]::IsNullOrWhiteSpace($candidate)) {
        $configPath = Join-Path $env:USERPROFILE '.agents\knowledge-config.json'
        if (Test-Path -LiteralPath $configPath) {
            $config = Read-Utf8Text -FilePath $configPath | ConvertFrom-Json
            $candidate = [string]$config.vault_path
        }
    }
    if ([string]::IsNullOrWhiteSpace($candidate)) {
        $cursor = (Get-Location).Path
        while ($cursor) {
            if (Test-Path -LiteralPath (Join-Path $cursor 'knowledge-vault.json')) {
                $candidate = $cursor
                break
            }
            $parent = Split-Path -Parent $cursor
            if (-not $parent -or $parent -eq $cursor) { break }
            $cursor = $parent
        }
    }
    if ([string]::IsNullOrWhiteSpace($candidate)) {
        throw 'Knowledge vault not found. Pass -Vault, set AGENT_KNOWLEDGE_VAULT, or install ~/.agents/knowledge-config.json.'
    }
    $resolved = (Resolve-Path -LiteralPath $candidate -ErrorAction Stop).Path
    if (-not (Test-Path -LiteralPath (Join-Path $resolved 'knowledge-vault.json'))) {
        throw "Not a knowledge vault: $resolved"
    }
    return $resolved
}

function Find-NearestGitRoot {
    param([string]$StartPath)
    $cursor = Get-NormalizedPath $StartPath
    while ($cursor) {
        if (Test-Path -LiteralPath (Join-Path $cursor '.git')) { return $cursor }
        $parent = Split-Path -Parent $cursor
        if (-not $parent -or $parent -eq $cursor) { break }
        $cursor = $parent
    }
    return $null
}

function Get-GitDirtyAssessment {
    param([string]$VaultRoot)

    $policy = [ordered]@{
        warn_count = 25
        block_count = 50
        warn_age_hours = 24
        block_age_hours = 168
    }
    $gitRoot = Find-NearestGitRoot -StartPath $VaultRoot
    if (-not $gitRoot) {
        return [PSCustomObject]@{
            git_root = $null
            change_count = 0
            changed_file_count = 0
            oldest_age_hours = $null
            age_unknown_count = 0
            state = 'unmanaged'
            write_blocked = $false
            policy = $policy
        }
    }

    try {
        $resolvedGit = Invoke-IsolatedGit -GitRoot $gitRoot -Arguments @('rev-parse', '--show-toplevel')
        $resolvedGitOutput = @($resolvedGit.Output)
        if ($resolvedGit.ExitCode -ne 0 -or $resolvedGitOutput.Count -eq 0) {
            throw 'Unable to resolve the Git worktree.'
        }
        $gitRoot = [string]$resolvedGitOutput[0]
        $summaryGit = Invoke-IsolatedGit -GitRoot $gitRoot -Arguments @('status', '--porcelain')
        if ($summaryGit.ExitCode -ne 0) { throw 'Unable to read the Git status.' }
        $summaryChanges = @($summaryGit.Output)
        $filesGit = Invoke-IsolatedGit -GitRoot $gitRoot -Arguments @('status', '--porcelain', '--untracked-files=all')
        if ($filesGit.ExitCode -ne 0) { throw 'Unable to read the full Git status.' }
        $fileChanges = @($filesGit.Output)

        $oldestWriteUtc = $null
        $ageUnknownCount = 0
        foreach ($change in $fileChanges) {
            if ([string]::IsNullOrWhiteSpace([string]$change) -or ([string]$change).Length -lt 4) { continue }
            $relativePath = ([string]$change).Substring(3).Trim()
            if ($relativePath -match ' -> ') { $relativePath = ($relativePath -split ' -> ', 2)[1] }
            if ($relativePath.StartsWith('"') -and $relativePath.EndsWith('"')) {
                $relativePath = $relativePath.Substring(1, $relativePath.Length - 2)
            }
            $candidate = Join-Path $gitRoot $relativePath
            if (Test-Path -LiteralPath $candidate) {
                $writeUtc = (Get-Item -LiteralPath $candidate).LastWriteTimeUtc
                if (-not $oldestWriteUtc -or $writeUtc -lt $oldestWriteUtc) { $oldestWriteUtc = $writeUtc }
            }
            else {
                $ageUnknownCount += 1
            }
        }

        $oldestAgeHours = $null
        if ($oldestWriteUtc) {
            $oldestAgeHours = [math]::Round(([DateTime]::UtcNow - $oldestWriteUtc).TotalHours, 3)
        }
        $state = 'clean'
        if ($fileChanges.Count -ge $policy.block_count -or ($null -ne $oldestAgeHours -and $oldestAgeHours -ge $policy.block_age_hours)) {
            $state = 'blocked'
        }
        elseif ($fileChanges.Count -ge $policy.warn_count -or ($null -ne $oldestAgeHours -and $oldestAgeHours -ge $policy.warn_age_hours)) {
            $state = 'warning'
        }
        elseif ($fileChanges.Count -gt 0) {
            $state = 'pending'
        }

        return [PSCustomObject]@{
            git_root = $gitRoot
            change_count = $summaryChanges.Count
            changed_file_count = $fileChanges.Count
            oldest_age_hours = $oldestAgeHours
            age_unknown_count = $ageUnknownCount
            state = $state
            write_blocked = ($state -eq 'blocked')
            policy = $policy
        }
    }
    catch {
        return [PSCustomObject]@{
            git_root = $gitRoot
            change_count = $null
            changed_file_count = $null
            oldest_age_hours = $null
            age_unknown_count = $null
            state = 'unavailable'
            write_blocked = $true
            policy = $policy
        }
    }
}

function Assert-GitBacklogWritable {
    param(
        [string]$VaultRoot,
        [string]$Operation,
        [int]$ExpectedAdditionalFiles = 0,
        [bool]$AllowCompletion = $false
    )

    $assessment = Get-GitDirtyAssessment -VaultRoot $VaultRoot
    if ($assessment.state -eq 'unavailable') {
        throw "Knowledge Hub write blocked for $Operation because Git status is unavailable."
    }
    $projectedCount = [int]$assessment.changed_file_count + $ExpectedAdditionalFiles
    $countWouldBlock = $projectedCount -ge [int]$assessment.policy.block_count
    $ageWouldBlock = $null -ne $assessment.oldest_age_hours -and [double]$assessment.oldest_age_hours -ge [double]$assessment.policy.block_age_hours
    if (($assessment.write_blocked -or $countWouldBlock -or $ageWouldBlock) -and -not $AllowCompletion) {
        throw ("Knowledge Hub write blocked for {0}: dirty tree has {1} changed files, oldest known age is {2} h. Reconcile and commit the owner worktree before adding more knowledge." -f $Operation, $assessment.changed_file_count, $assessment.oldest_age_hours)
    }
    return $assessment
}

function Get-RiskAssessment {
    param([string]$Content)

    $secretPatterns = @(
        @{ Label = 'private-key'; Pattern = '-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----' },
        @{ Label = 'github-token'; Pattern = '\bgh[pousr]_[A-Za-z0-9]{20,}\b' },
        @{ Label = 'openai-style-key'; Pattern = '\bsk-[A-Za-z0-9_-]{20,}\b' },
        @{ Label = 'aws-access-key'; Pattern = '\b(?:AKIA|ASIA)[A-Z0-9]{16}\b' },
        @{ Label = 'bearer-token'; Pattern = '(?i)authorization\s*:\s*bearer\s+[A-Za-z0-9._~+/-]{16,}' },
        @{ Label = 'password-assignment'; Pattern = '(?i)\b(?:password|passwd|pwd|api[_-]?key|secret|token)\s*[:=]\s*["''][^"'']{8,}["'']' }
    )
    $injectionPatterns = @(
        @{ Label = 'instruction-override'; Pattern = '(?i)\b(ignore|disregard|override)\b.{0,40}\b(previous|prior|system|developer)\b.{0,30}\b(instruction|prompt|rule)' },
        @{ Label = 'system-prompt-request'; Pattern = '(?i)\b(reveal|print|show|expose)\b.{0,40}\b(system prompt|hidden instruction|developer message)' },
        @{ Label = 'credential-exfiltration'; Pattern = '(?i)\b(upload|send|post|exfiltrat|transmit)\b.{0,60}\b(secret|token|credential|\.env|private key)' },
        @{ Label = 'hidden-html'; Pattern = '(?i)<!--.{0,200}(ignore|instruction|prompt).{0,200}-->' },
        @{ Label = 'concealment'; Pattern = '(?i)\b(do not tell|hide this from|keep this secret from)\b.{0,40}\b(user|owner|operator)' }
    )

    $secretHits = New-Object Collections.Generic.List[string]
    foreach ($item in $secretPatterns) {
        if ([regex]::IsMatch($Content, $item.Pattern)) { $secretHits.Add($item.Label) }
    }
    $injectionHits = New-Object Collections.Generic.List[string]
    foreach ($item in $injectionPatterns) {
        if ([regex]::IsMatch($Content, $item.Pattern)) { $injectionHits.Add($item.Label) }
    }
    $hasInvisible = [regex]::IsMatch($Content, '[\u200B-\u200F\u202A-\u202E\u2060\uFEFF]')
    if ($hasInvisible) { $injectionHits.Add('invisible-unicode') }

    return [PSCustomObject]@{
        secret_hits = @($secretHits | Sort-Object -Unique)
        injection_hits = @($injectionHits | Sort-Object -Unique)
    }
}

function Assert-ControlTextSafe {
    param([string]$Label, [string]$Value)
    if ([string]::IsNullOrWhiteSpace($Value)) { return }
    $risk = Get-RiskAssessment -Content $Value
    if ($risk.secret_hits.Count -gt 0) {
        throw "$Label contains secret indicators: $($risk.secret_hits -join ', '). Use redacted neutral metadata."
    }
    if ($risk.injection_hits.Count -gt 0) {
        throw "$Label contains prompt-injection indicators: $($risk.injection_hits -join ', '). Use neutral metadata."
    }
}

function Get-FrontmatterValue {
    param([string]$Content, [string]$Name)
    $frontmatter = Get-FrontmatterBlock -Content $Content
    if ($null -eq $frontmatter) { return $null }
    $match = [regex]::Match($frontmatter, '(?m)^' + [regex]::Escape($Name) + '[ \t]*:[ \t]*(?<value>[^\r\n]*)[ \t]*\r?$')
    if (-not $match.Success) { return $null }
    return $match.Groups['value'].Value.Trim().Trim('"').Trim("'")
}

function Get-FrontmatterBlock {
    param([string]$Content)
    $pattern = '\A(?:\uFEFF)?---[ \t]*\r?\n(?<frontmatter>.*?)(?:\r?\n)---[ \t]*(?:\r?\n|$)'
    $match = [regex]::Match($Content, $pattern, [Text.RegularExpressions.RegexOptions]::Singleline)
    if (-not $match.Success) { return $null }
    return $match.Groups['frontmatter'].Value
}

function Get-FrontmatterList {
    param([string]$Content, [string]$Name)
    $inline = Get-FrontmatterValue -Content $Content -Name $Name
    if ($null -eq $inline) { return @() }
    if ($inline -eq '[]') { return @() }
    if ($inline.StartsWith('[') -and $inline.EndsWith(']')) {
        return @($inline.Trim('[', ']').Split(',') | ForEach-Object { $_.Trim().Trim('"').Trim("'") } | Where-Object { $_ })
    }
    $frontmatter = Get-FrontmatterBlock -Content $Content
    if ($null -eq $frontmatter) { return @() }
    $pattern = '(?m)^' + [regex]::Escape($Name) + '[ \t]*:[ \t]*\r?\n(?<items>(?:[ \t]{2,}-[ \t]+[^\r\n]*(?:\r?\n|$))*)'
    $block = [regex]::Match($frontmatter, $pattern)
    if (-not $block.Success) { return @() }
    return @([regex]::Matches($block.Groups['items'].Value, '(?m)^[ \t]{2,}-[ \t]+(?<value>[^\r\n]*)\r?$') | ForEach-Object { $_.Groups['value'].Value.Trim().Trim('"').Trim("'") })
}

function Get-SourceRecords {
    param([string]$VaultRoot)
    $records = New-Object Collections.Generic.List[object]
    $sourceRoot = Join-Path $VaultRoot 'raw\sources'
    if (Test-Path -LiteralPath $sourceRoot) {
        foreach ($file in Get-ChildItem -LiteralPath $sourceRoot -Filter 'source.json' -File -Recurse -ErrorAction SilentlyContinue) {
            try {
                $record = Read-Utf8Text -FilePath $file.FullName | ConvertFrom-Json
                $records.Add([PSCustomObject]@{ metadata = $record; metadata_path = $file.FullName })
            }
            catch {
                $records.Add([PSCustomObject]@{ metadata = $null; metadata_path = $file.FullName; parse_error = $_.Exception.Message })
            }
        }
    }
    return @($records.ToArray())
}

function Write-Event {
    param(
        [string]$VaultRoot,
        [string]$EventAgent,
        [string]$EventAction,
        [string]$Summary,
        [string[]]$SourceIds = @(),
        [string[]]$ProposalIds = @(),
        [string[]]$PagesChanged = @()
    )
    Assert-ControlTextSafe -Label 'Event summary' -Value $Summary
    $eventAgentSlug = Get-Slug $EventAgent
    if ($eventAgentSlug -eq 'unknown') { throw 'Event agent must be identified.' }
    $now = [DateTime]::UtcNow
    $eventId = 'evt-' + $now.ToString('yyyyMMddTHHmmssfffZ') + '-' + ([Guid]::NewGuid().ToString('N').Substring(0, 8))
    $eventDir = Join-Path $VaultRoot ('events\' + $now.ToString('yyyy') + '\' + $now.ToString('MM'))
    New-Item -ItemType Directory -Force -Path $eventDir | Out-Null
    $eventPath = Join-Path $eventDir ($eventId + '.json')
    $event = [ordered]@{
        event_id = $eventId
        occurred_at = $now.ToString('o')
        agent = $eventAgentSlug
        action = $EventAction
        proposal_ids = @($ProposalIds)
        pages_changed = @($PagesChanged)
        source_ids = @($SourceIds)
        summary = $Summary
    }
    Write-Utf8NoBom -FilePath $eventPath -Content (($event | ConvertTo-Json -Depth 8) + "`n")
    return $eventPath
}

function Assert-CuratorLock {
    param([string]$VaultRoot, [string]$ExpectedLockId)
    $vaultConfig = Read-Utf8Text -FilePath (Join-Path $VaultRoot 'knowledge-vault.json') | ConvertFrom-Json
    $curatorMachine = [string]$vaultConfig.curator_machine
    $localMachine = Get-LocalMachineName
    if ($curatorMachine -and -not $curatorMachine.Equals($localMachine, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Curation is restricted to machine '$curatorMachine'. This machine is proposal-only."
    }
    if ([string]::IsNullOrWhiteSpace($ExpectedLockId)) { throw 'Curator action requires -LockId.' }
    $lockPath = Join-Path $VaultRoot 'governance\curator.lock'
    if (-not (Test-Path -LiteralPath $lockPath)) { throw 'Curator lock does not exist.' }
    $lock = Read-Utf8Text -FilePath $lockPath | ConvertFrom-Json
    if ([string]$lock.lock_id -ne $ExpectedLockId) { throw 'Curator lock ID does not match.' }
    if (-not ([string]$lock.machine).Equals($localMachine, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Curator lock belongs to a different machine.'
    }
    return $lock
}

function Invoke-Init {
    param([string]$Target)
    if ([string]::IsNullOrWhiteSpace($Target)) { throw 'Init requires -Vault <target-directory>.' }
    $template = Join-Path (Split-Path -Parent $PSScriptRoot) 'assets\vault-template'
    if (-not (Test-Path -LiteralPath $template)) { throw "Vault template missing: $template" }
    if (Test-Path -LiteralPath $Target) {
        if (Test-Path -LiteralPath (Join-Path $Target 'knowledge-vault.json')) {
            $resolvedExisting = (Resolve-Path -LiteralPath $Target).Path
            Write-JsonOutput ([ordered]@{ action = 'init'; vault_path = $resolvedExisting; created = $false })
            return
        }
        $existing = @(Get-ChildItem -LiteralPath $Target -Force -ErrorAction SilentlyContinue)
        if ($existing.Count -gt 0) {
            throw "Target directory is not empty: $Target"
        }
    }
    else {
        New-Item -ItemType Directory -Force -Path $Target | Out-Null
    }
    foreach ($templateItem in Get-ChildItem -LiteralPath $template -Force) {
        Copy-Item -LiteralPath $templateItem.FullName -Destination $Target -Recurse -Force
    }
    $resolved = (Resolve-Path -LiteralPath $Target).Path
    $configPath = Join-Path $resolved 'knowledge-vault.json'
    $config = Read-Utf8Text -FilePath $configPath | ConvertFrom-Json
    if ([string]::IsNullOrWhiteSpace([string]$config.curator_machine)) {
        $config.curator_machine = Get-LocalMachineName
        Write-Utf8NoBom -FilePath $configPath -Content (($config | ConvertTo-Json -Depth 8) + "`n")
    }
    Write-JsonOutput ([ordered]@{ action = 'init'; vault_path = $resolved; created = $true })
}

function Invoke-Status {
    param([string]$VaultRoot)
    $vaultConfig = Read-Utf8Text -FilePath (Join-Path $VaultRoot 'knowledge-vault.json') | ConvertFrom-Json
    $curatorMachine = [string]$vaultConfig.curator_machine
    $sourceCount = @(Get-SourceRecords -VaultRoot $VaultRoot | Where-Object { $_.metadata }).Count
    $proposalCount = @(Get-ChildItem -LiteralPath (Join-Path $VaultRoot 'proposals') -Filter '*.md' -File -Recurse -ErrorAction SilentlyContinue).Count
    $wikiCount = @(Get-ChildItem -LiteralPath (Join-Path $VaultRoot 'wiki') -Filter '*.md' -File -Recurse -ErrorAction SilentlyContinue | Where-Object { $_.Name -ne 'index.md' }).Count
    $eventCount = @(Get-ChildItem -LiteralPath (Join-Path $VaultRoot 'events') -Filter '*.json' -File -Recurse -ErrorAction SilentlyContinue).Count
    $pythonStatus = Invoke-ContributorPythonAction -VaultRoot $VaultRoot -RequestedAction 'status' | ConvertFrom-Json
    if ($script:ContributorExitCode -ne 0) { throw 'Curation health read failed in the Python gateway.' }
    $lockPath = Join-Path $VaultRoot 'governance\curator.lock'
    $gitAssessment = Get-GitDirtyAssessment -VaultRoot $VaultRoot
    $localMachine = $null
    $machineIdentityError = $false
    try { $localMachine = Get-LocalMachineName }
    catch { $machineIdentityError = $true }
    Write-JsonOutput ([ordered]@{
        action = 'status'
        vault_path = $VaultRoot
        sources = $sourceCount
        proposals = $proposalCount
        wiki_pages = $wikiCount
        events = $eventCount
        curator_lock = (Test-Path -LiteralPath $lockPath)
        curator_lock_owner = $pythonStatus.curator_lock_owner
        curator_machine = $curatorMachine
        curation_allowed = (-not $machineIdentityError -and (-not $curatorMachine -or $curatorMachine.Equals($localMachine, [StringComparison]::OrdinalIgnoreCase)))
        machine_identity_error = $machineIdentityError
        git_root = $gitAssessment.git_root
        git_change_count = $gitAssessment.change_count
        git_changed_file_count = $gitAssessment.changed_file_count
        dirty_tree_state = $gitAssessment.state
        dirty_tree_oldest_age_hours = $gitAssessment.oldest_age_hours
        dirty_tree_age_unknown_count = $gitAssessment.age_unknown_count
        dirty_tree_write_blocked = $gitAssessment.write_blocked
        dirty_tree_policy = $gitAssessment.policy
        curation_health = $pythonStatus.curation_health
    })
}

function Invoke-StageSource {
    param(
        [string]$VaultRoot,
        [string]$SourcePath,
        [string]$SourceTitle,
        [string]$SourceOrigin,
        [string]$SourceClassification,
        [string]$SourceAgent,
        [bool]$UseLocalOnly
    )
    if ([string]::IsNullOrWhiteSpace($SourcePath)) { throw 'stage-source requires -Path.' }
    if ([string]::IsNullOrWhiteSpace($SourceTitle)) { throw 'stage-source requires -Title.' }
    if ([string]::IsNullOrWhiteSpace($SourceAgent) -or $SourceAgent -eq 'unknown') { throw 'stage-source requires -Agent.' }
    if ($SourceTitle.IndexOfAny(@([char]13, [char]10)) -ge 0) { throw 'Source title must be one line.' }
    Assert-ControlTextSafe -Label 'Source title' -Value $SourceTitle
    Assert-ControlTextSafe -Label 'Source origin' -Value $SourceOrigin
    $resolvedSource = (Resolve-Path -LiteralPath $SourcePath -ErrorAction Stop).Path
    $item = Get-Item -LiteralPath $resolvedSource
    if ($item.PSIsContainer) { throw 'stage-source accepts one file, not a directory.' }
    if (($SourceClassification -eq 'confidential' -or $SourceClassification -eq 'restricted') -and -not $UseLocalOnly) {
        throw "$SourceClassification sources require -LocalOnly and cannot enter shared Git history."
    }

    $hash = Get-Sha256Hex -FilePath $resolvedSource
    foreach ($record in Get-SourceRecords -VaultRoot $VaultRoot) {
        if (
            -not $UseLocalOnly -and
            $record.metadata -and
            [string]$record.metadata.sha256 -eq $hash -and
            [string]$record.metadata.classification -eq $SourceClassification
        ) {
            Write-JsonOutput ([ordered]@{
                action = 'stage-source'
                duplicate = $true
                source_id = [string]$record.metadata.source_id
                metadata_path = $record.metadata_path
            })
            return
        }
    }

    if (-not $UseLocalOnly) {
        [void](Assert-GitBacklogWritable -VaultRoot $VaultRoot -Operation 'stage-source' -ExpectedAdditionalFiles 4)
    }

    $riskFlags = New-Object Collections.Generic.List[string]
    $textExtensions = @('.md', '.txt', '.json', '.jsonl', '.yaml', '.yml', '.toml', '.csv', '.tsv', '.html', '.htm', '.xml', '.ps1', '.py', '.js', '.ts')
    if ($textExtensions -contains $item.Extension.ToLowerInvariant()) {
        if ($item.Length -gt 5MB) { throw 'Text source exceeds the 5 MB safety scan limit. Split or review it before staging.' }
        $content = Read-Utf8Text -FilePath $resolvedSource
        $risk = Get-RiskAssessment -Content $content
        if ($risk.secret_hits.Count -gt 0) {
            throw ('Secret indicators detected: ' + ($risk.secret_hits -join ', ') + '. Redact the source before staging.')
        }
        foreach ($hit in $risk.injection_hits) { $riskFlags.Add($hit) }
    }
    else {
        if (-not $UseLocalOnly) {
            throw 'Binary sources cannot enter shared Git because they are not text-scanned. Extract and redact text, or use -LocalOnly.'
        }
        $riskFlags.Add('binary-content-not-text-scanned-local-only')
    }

    $now = [DateTime]::UtcNow
    $sourceId = 'src-' + $now.ToString('yyyyMMddTHHmmssfffZ') + '-' + (Get-Slug $SourceTitle) + '-' + $hash.Substring(0, 8)
    $datePath = Join-Path $now.ToString('yyyy') $now.ToString('MM')
    if ($UseLocalOnly) {
        $destinationRoot = Join-Path (Join-Path $VaultRoot '.local\private-sources') $datePath
    }
    else {
        $destinationRoot = Join-Path (Join-Path $VaultRoot 'raw\sources') $datePath
    }
    $packageDir = Join-Path $destinationRoot $sourceId
    New-Item -ItemType Directory -Force -Path $packageDir | Out-Null
    $originalName = 'original' + $item.Extension.ToLowerInvariant()
    $originalDestination = Join-Path $packageDir $originalName
    Copy-Item -LiteralPath $resolvedSource -Destination $originalDestination

    $metadata = [ordered]@{
        schema_version = 1
        source_id = $sourceId
        title = $SourceTitle
        origin = $SourceOrigin
        captured_at = $now.ToString('o')
        captured_by = (Get-Slug $SourceAgent)
        classification = $SourceClassification
        local_only = $UseLocalOnly
        original_file = $originalName
        media_type = $item.Extension.ToLowerInvariant().TrimStart('.')
        byte_length = $item.Length
        sha256 = $hash
        trust = 'untrusted'
        risk_flags = @($riskFlags | Sort-Object -Unique)
        reviewed_by = $null
        reviewed_at = $null
    }
    $metadataPath = Join-Path $packageDir 'source.json'
    Write-Utf8NoBom -FilePath $metadataPath -Content (($metadata | ConvertTo-Json -Depth 8) + "`n")
    if (-not $UseLocalOnly) {
        [void](Write-Event -VaultRoot $VaultRoot -EventAgent $SourceAgent -EventAction 'stage-source' -Summary ("Staged source: " + $SourceTitle) -SourceIds @($sourceId))
    }
    Write-JsonOutput ([ordered]@{
        action = 'stage-source'
        duplicate = $false
        source_id = $sourceId
        metadata_path = $metadataPath
        original_path = $originalDestination
        risk_flags = @($riskFlags | Sort-Object -Unique)
        local_only = $UseLocalOnly
    })
}

function Invoke-NewProposal {
    param(
        [string]$VaultRoot,
        [string]$ProposalAgent,
        [string]$ProposalTitle,
        [string]$ProposalClassification,
        [string[]]$ProposalSourceIds
    )
    if ([string]::IsNullOrWhiteSpace($ProposalTitle)) { throw 'new-proposal requires -Title.' }
    if ($ProposalTitle.IndexOfAny(@([char]13, [char]10)) -ge 0) { throw 'Proposal title must be one line.' }
    Assert-ControlTextSafe -Label 'Proposal title' -Value $ProposalTitle
    if ([string]::IsNullOrWhiteSpace($ProposalAgent) -or $ProposalAgent -eq 'unknown') { throw 'new-proposal requires -Agent.' }
    if ($ProposalClassification -eq 'confidential' -or $ProposalClassification -eq 'restricted') {
        throw 'Confidential and restricted proposals must use a separately approved local-only vault.'
    }
    $knownSourceIds = @{}
    foreach ($record in Get-SourceRecords -VaultRoot $VaultRoot) {
        if ($record.metadata) { $knownSourceIds[[string]$record.metadata.source_id] = $true }
    }
    $ProposalSourceIds = @($ProposalSourceIds | Where-Object { -not [string]::IsNullOrWhiteSpace($_) } | Sort-Object -Unique)
    foreach ($proposalSourceId in $ProposalSourceIds) {
        if (-not $knownSourceIds.ContainsKey($proposalSourceId)) {
            throw "Unknown source ID: $proposalSourceId"
        }
    }
    [void](Assert-GitBacklogWritable -VaultRoot $VaultRoot -Operation 'new-proposal' -ExpectedAdditionalFiles 2 -AllowCompletion ($ProposalSourceIds.Count -gt 0))
    $agentSlug = Get-Slug $ProposalAgent
    $now = [DateTime]::UtcNow
    $proposalId = 'prop-' + $now.ToString('yyyyMMddTHHmmssfffZ') + '-' + ([Guid]::NewGuid().ToString('N').Substring(0, 8))
    $proposalDir = Join-Path $VaultRoot ('proposals\' + $agentSlug)
    New-Item -ItemType Directory -Force -Path $proposalDir | Out-Null
    $proposalPath = Join-Path $proposalDir ($proposalId + '-' + (Get-Slug $ProposalTitle) + '.md')
    $proposalTitleYaml = ConvertTo-YamlSingleQuoted $ProposalTitle
    $sourceBlock = 'source_ids: []'
    if ($ProposalSourceIds.Count -gt 0) {
        $sourceBlock = "source_ids:`n" + (@($ProposalSourceIds | ForEach-Object { '  - ' + $_ }) -join "`n")
    }
    $content = @"
---
id: $proposalId
type: proposal
status: proposed
title: $proposalTitleYaml
agent: $agentSlug
created_at: $($now.ToString('o'))
classification: $ProposalClassification
$sourceBlock
target_pages: []
---

# $ProposalTitle

## Intent

Describe the durable knowledge change and why it matters.

## Atomic claims

| Claim | Source ID | Exact location | Confidence |
|---|---|---|---|
| TODO | TODO | TODO | draft |

## Proposed page changes

- TODO

## Contradictions and uncertainty

- TODO

## Security review

- Embedded instructions ignored: TODO
- Secret indicators: TODO
- External action requested by source: TODO
"@
    Write-Utf8NoBom -FilePath $proposalPath -Content ($content.TrimStart() + "`n")
    $eventPath = Write-Event -VaultRoot $VaultRoot -EventAgent $agentSlug -EventAction 'new-proposal' -Summary ("Created proposal: " + $ProposalTitle) -SourceIds $ProposalSourceIds -ProposalIds @($proposalId)
    Write-JsonOutput ([ordered]@{
        action = 'new-proposal'
        proposal_id = $proposalId
        proposal_path = $proposalPath
        event_path = $eventPath
        agent = $agentSlug
    })
}

function Invoke-AcquireLock {
    param([string]$VaultRoot, [string]$LockAgent, [Nullable[int]]$RequestedOwnerPid)
    if ([string]::IsNullOrWhiteSpace($LockAgent) -or $LockAgent -eq 'unknown') { throw 'acquire-lock requires -Agent.' }
    $vaultConfig = Read-Utf8Text -FilePath (Join-Path $VaultRoot 'knowledge-vault.json') | ConvertFrom-Json
    $curatorMachine = [string]$vaultConfig.curator_machine
    $localMachine = Get-LocalMachineName
    if ($curatorMachine -and -not $curatorMachine.Equals($localMachine, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Curation is restricted to machine '$curatorMachine'. This machine is proposal-only."
    }
    $lockPath = Join-Path $VaultRoot 'governance\curator.lock'
    $lockIdValue = [Guid]::NewGuid().ToString('N')
    $ownerPidValue = $RequestedOwnerPid
    $ownerSource = 'explicit'
    if ($null -eq $ownerPidValue) {
        $ownerSource = 'parent'
        try {
            $helperProcess = Get-CimInstance Win32_Process -Filter "ProcessId=$PID" -ErrorAction Stop
            $ownerPidValue = [int]$helperProcess.ParentProcessId
            if (-not $ownerPidValue) { throw 'Parent process is unavailable.' }
        }
        catch {
            $ownerPidValue = $PID
            $ownerSource = 'helper'
        }
    }
    $ownerStartedAt = $null
    $ownerProcessName = $null
    try {
        $ownerProcess = Get-CimInstance Win32_Process -Filter "ProcessId=$ownerPidValue" -ErrorAction Stop
        if ($ownerProcess -and $ownerProcess.Name) {
            $ownerProcessName = [string]$ownerProcess.Name
        }
        if ($ownerProcess -and $ownerProcess.CreationDate) {
            $ownerStartedAt = ([datetime]$ownerProcess.CreationDate).ToUniversalTime().ToString('o')
        }
    }
    catch { }
    if (-not $ownerStartedAt -or -not $ownerProcessName) {
        try {
            $ownerProcess = Get-Process -Id $ownerPidValue -ErrorAction Stop
            if (-not $ownerStartedAt) {
                $ownerStartedAt = $ownerProcess.StartTime.ToUniversalTime().ToString('o')
            }
            if (-not $ownerProcessName) {
                $ownerProcessName = [string]$ownerProcess.ProcessName
                if ($ownerProcessName -and -not $ownerProcessName.EndsWith('.exe', [StringComparison]::OrdinalIgnoreCase)) {
                    $ownerProcessName += '.exe'
                }
            }
        }
        catch { }
    }
    $shellNames = @('cmd.exe', 'powershell.exe', 'pwsh.exe', 'bash.exe', 'sh.exe', 'wsl.exe', 'conhost.exe', 'windowsterminal.exe')
    if ($ownerSource -eq 'parent' -and $ownerProcessName -and $shellNames -contains $ownerProcessName.ToLowerInvariant()) {
        $ownerSource = 'parent-shell'
    }
    $lock = [ordered]@{
        lock_id = $lockIdValue
        agent = (Get-Slug $LockAgent)
        acquired_at = [DateTime]::UtcNow.ToString('o')
        machine = $localMachine
        process_id = $ownerPidValue
        owner_process_started_at = $ownerStartedAt
        helper_process_id = $PID
        owner_source = $ownerSource
        owner_process_name = $ownerProcessName
    }
    $json = ($lock | ConvertTo-Json -Depth 4) + "`n"
    $bytes = [Text.Encoding]::UTF8.GetBytes($json)
    try {
        $stream = [IO.File]::Open($lockPath, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
        try { $stream.Write($bytes, 0, $bytes.Length) } finally { $stream.Dispose() }
    }
    catch [IO.IOException] {
        throw "Curator lock already exists: $lockPath"
    }
    Write-JsonOutput ([ordered]@{ action = 'acquire-lock'; lock_id = $lockIdValue; lock_path = $lockPath; agent = (Get-Slug $LockAgent) })
}

function Invoke-ReleaseLock {
    param([string]$VaultRoot, [string]$ExpectedLockId)
    [void](Assert-CuratorLock -VaultRoot $VaultRoot -ExpectedLockId $ExpectedLockId)
    $lockPath = Join-Path $VaultRoot 'governance\curator.lock'
    Remove-Item -LiteralPath $lockPath -Force
    Write-JsonOutput ([ordered]@{ action = 'release-lock'; released = $true; lock_id = $ExpectedLockId })
}

function Invoke-RecordCuration {
    param(
        [string]$VaultRoot,
        [string]$ExpectedLockId,
        [string]$CuratorAgent,
        [string]$CurationSummary,
        [string[]]$CurationPages,
        [string[]]$CurationSourceIds,
        [string[]]$CurationProposalIds
    )
    [void](Assert-CuratorLock -VaultRoot $VaultRoot -ExpectedLockId $ExpectedLockId)
    if ([string]::IsNullOrWhiteSpace($CuratorAgent) -or $CuratorAgent -eq 'unknown') { throw 'record-curation requires -Agent.' }
    if ([string]::IsNullOrWhiteSpace($CurationSummary)) { throw 'record-curation requires -Summary.' }

    $knownSources = @{}
    foreach ($record in Get-SourceRecords -VaultRoot $VaultRoot) {
        if ($record.metadata) { $knownSources[[string]$record.metadata.source_id] = $true }
    }
    $CurationSourceIds = @($CurationSourceIds | Where-Object { $_ } | Sort-Object -Unique)
    foreach ($sourceIdValue in $CurationSourceIds) {
        if (-not $knownSources.ContainsKey($sourceIdValue)) { throw "Unknown source ID: $sourceIdValue" }
    }

    $knownProposals = @{}
    foreach ($proposalFile in Get-ChildItem -LiteralPath (Join-Path $VaultRoot 'proposals') -Filter '*.md' -File -Recurse -ErrorAction SilentlyContinue) {
        $proposalContent = Read-Utf8Text -FilePath $proposalFile.FullName
        $knownProposalId = Get-FrontmatterValue -Content $proposalContent -Name 'id'
        if ($knownProposalId) { $knownProposals[$knownProposalId] = $true }
    }
    $CurationProposalIds = @($CurationProposalIds | Where-Object { $_ } | Sort-Object -Unique)
    foreach ($proposalIdValue in $CurationProposalIds) {
        if (-not $knownProposals.ContainsKey($proposalIdValue)) { throw "Unknown proposal ID: $proposalIdValue" }
    }

    $wikiRoot = Get-NormalizedPath (Join-Path $VaultRoot 'wiki')
    $validatedPages = New-Object Collections.Generic.List[string]
    foreach ($pageValue in @($CurationPages | Where-Object { $_ } | Sort-Object -Unique)) {
        if ([IO.Path]::IsPathRooted($pageValue)) { throw "Page must be relative to wiki/: $pageValue" }
        $relativePage = $pageValue.Replace('/', '\').TrimStart('\')
        if (-not $relativePage.EndsWith('.md', [StringComparison]::OrdinalIgnoreCase)) { $relativePage += '.md' }
        $candidate = Get-NormalizedPath (Join-Path $wikiRoot $relativePage)
        if (-not $candidate.StartsWith($wikiRoot + '\', [StringComparison]::OrdinalIgnoreCase)) { throw "Page escapes wiki/: $pageValue" }
        if (-not (Test-Path -LiteralPath $candidate)) { throw "Wiki page not found: $pageValue" }
        $validatedPages.Add($relativePage.Replace('\', '/'))
    }

    $eventPath = Write-Event -VaultRoot $VaultRoot -EventAgent (Get-Slug $CuratorAgent) -EventAction 'record-curation' -Summary $CurationSummary -SourceIds $CurationSourceIds -ProposalIds $CurationProposalIds -PagesChanged $validatedPages.ToArray()
    Write-JsonOutput ([ordered]@{
        action = 'record-curation'
        event_path = $eventPath
        pages_changed = @($validatedPages.ToArray())
        source_ids = $CurationSourceIds
        proposal_ids = $CurationProposalIds
    })
}

function Invoke-Refresh {
    param([string]$VaultRoot, [string]$ExpectedLockId)
    [void](Assert-CuratorLock -VaultRoot $VaultRoot -ExpectedLockId $ExpectedLockId)
    $wikiRoot = Join-Path $VaultRoot 'wiki'
    $rows = New-Object Collections.Generic.List[string]
    foreach ($file in Get-ChildItem -LiteralPath $wikiRoot -Filter '*.md' -File -Recurse | Where-Object { $_.Name -ne 'index.md' } | Sort-Object FullName) {
        $content = Read-Utf8Text -FilePath $file.FullName
        $pageTitle = Get-FrontmatterValue -Content $content -Name 'title'
        $pageType = Get-FrontmatterValue -Content $content -Name 'type'
        $pageStatus = Get-FrontmatterValue -Content $content -Name 'status'
        $lastVerified = Get-FrontmatterValue -Content $content -Name 'last_verified'
        $summary = Get-FrontmatterValue -Content $content -Name 'summary'
        if (-not $pageTitle) { $pageTitle = $file.BaseName }
        if (-not $pageType) { $pageType = 'unknown' }
        if (-not $pageStatus) { $pageStatus = 'unknown' }
        if (-not $lastVerified) { $lastVerified = '-' }
        if (-not $summary) { $summary = '-' }
        $summary = $summary.Replace('|', '\|')
        $relative = $file.FullName.Substring($wikiRoot.Length + 1).Replace('\', '/')
        $rows.Add("| [$pageTitle]($relative) | $pageType | $pageStatus | $lastVerified | $summary |")
    }
    $indexContent = @(
        '# Knowledge Index',
        '',
        'Generated by `knowledge.ps1 -Action refresh`. Do not edit directly.',
        '',
        '| Page | Type | Status | Last verified | Summary |',
        '|---|---|---|---|---|'
    ) + @($rows)
    Write-Utf8NoBom -FilePath (Join-Path $wikiRoot 'index.md') -Content (($indexContent -join "`n") + "`n")

    $eventRows = New-Object Collections.Generic.List[string]
    $eventFiles = @(Get-ChildItem -LiteralPath (Join-Path $VaultRoot 'events') -Filter '*.json' -File -Recurse -ErrorAction SilentlyContinue)
    $events = foreach ($eventFile in $eventFiles) {
        try { Read-Utf8Text -FilePath $eventFile.FullName | ConvertFrom-Json } catch { }
    }
    foreach ($event in @($events | Sort-Object occurred_at -Descending)) {
        $eventSummary = ([string]$event.summary).Replace('|', '\|')
        $eventRows.Add("| $($event.occurred_at) | $($event.agent) | $($event.action) | $eventSummary |")
    }
    $logContent = @(
        '# Knowledge Event Log',
        '',
        'Generated from unique JSON files under `events/`. Do not edit directly.',
        '',
        '| Time | Agent | Action | Summary |',
        '|---|---|---|---|'
    ) + @($eventRows)
    Write-Utf8NoBom -FilePath (Join-Path $VaultRoot 'log.md') -Content (($logContent -join "`n") + "`n")
    Write-JsonOutput ([ordered]@{ action = 'refresh'; wiki_pages = $rows.Count; events = $eventRows.Count })
}

function Invoke-Lint {
    param([string]$VaultRoot)
    $errors = New-Object Collections.Generic.List[string]
    $warnings = New-Object Collections.Generic.List[string]
    $allowedStatuses = @('draft', 'reviewed', 'verified', 'stale', 'archived')
    $vaultConfigPath = Join-Path $VaultRoot 'knowledge-vault.json'
    try {
        $vaultConfig = Read-Utf8Text -FilePath $vaultConfigPath | ConvertFrom-Json
        if ([string]::IsNullOrWhiteSpace([string]$vaultConfig.curator_machine)) { $errors.Add('Vault curator_machine is not configured.') }
        if ([string]$vaultConfig.curation_scope -ne 'designated-machine-only') { $errors.Add('Vault curation_scope must be designated-machine-only.') }
    }
    catch {
        $errors.Add('Invalid vault configuration: ' + $vaultConfigPath)
        $vaultConfig = [PSCustomObject]@{ curator_lock_minutes = 30; curator_machine = '' }
    }
    $sourceRecords = @(Get-SourceRecords -VaultRoot $VaultRoot)
    $sourceMap = @{}
    foreach ($record in $sourceRecords) {
        if (-not $record.metadata) {
            $errors.Add('Invalid source metadata JSON: ' + $record.metadata_path)
            continue
        }
        $requiredSourceFields = @('schema_version', 'source_id', 'title', 'origin', 'captured_at', 'captured_by', 'classification', 'local_only', 'original_file', 'byte_length', 'sha256', 'trust', 'risk_flags')
        $sourcePropertyNames = @($record.metadata.PSObject.Properties.Name)
        foreach ($field in $requiredSourceFields) {
            if ($sourcePropertyNames -notcontains $field) { $errors.Add("Missing source metadata '$field': $($record.metadata_path)") }
        }
        $sourceIdValue = [string]$record.metadata.source_id
        if ($sourceIdValue -notmatch '^src-\d{8}T\d{9}Z-[a-z0-9-]+-[a-f0-9]{8}$') { $errors.Add('Invalid source ID: ' + $sourceIdValue) }
        if ($sourceMap.ContainsKey($sourceIdValue)) { $errors.Add('Duplicate source ID: ' + $sourceIdValue) }
        else { $sourceMap[$sourceIdValue] = $record }
        if ((Split-Path -Leaf (Split-Path -Parent $record.metadata_path)) -ne $sourceIdValue) { $errors.Add('Source directory does not match source ID: ' + $sourceIdValue) }
        if (@('public', 'internal') -notcontains [string]$record.metadata.classification) { $errors.Add('Shared source has disallowed classification: ' + $sourceIdValue) }
        if ([bool]$record.metadata.local_only) { $errors.Add('Local-only source found under shared raw/sources: ' + $sourceIdValue) }
        if ([string]$record.metadata.captured_by -notmatch '^[a-z0-9][a-z0-9-]*$') { $errors.Add('Invalid source captured_by: ' + $sourceIdValue) }
        if ([string]$record.metadata.sha256 -notmatch '^[a-f0-9]{64}$') { $errors.Add('Invalid source SHA-256: ' + $sourceIdValue) }
        $metadataRisk = Get-RiskAssessment -Content (([string]$record.metadata.title) + "`n" + ([string]$record.metadata.origin) + "`n" + ([string]$record.metadata.captured_by))
        if ($metadataRisk.secret_hits.Count -gt 0) { $errors.Add('Secret indicator in source metadata: ' + $sourceIdValue) }
        if ($metadataRisk.injection_hits.Count -gt 0) { $errors.Add('Injection indicator in source metadata: ' + $sourceIdValue) }
        if (@($record.metadata.risk_flags) -contains 'binary-content-not-text-scanned-local-only') { $errors.Add('Unscanned binary found in shared source: ' + $sourceIdValue) }
        $originalPath = Join-Path (Split-Path -Parent $record.metadata_path) ([string]$record.metadata.original_file)
        if (-not (Test-Path -LiteralPath $originalPath)) {
            $errors.Add('Missing source original: ' + $sourceIdValue)
        }
        else {
            $currentHash = Get-Sha256Hex -FilePath $originalPath
            if ($currentHash -ne [string]$record.metadata.sha256) { $errors.Add('Source hash changed: ' + $sourceIdValue) }
            $originalItem = Get-Item -LiteralPath $originalPath
            $lintTextExtensions = @('.md', '.txt', '.json', '.jsonl', '.yaml', '.yml', '.toml', '.csv', '.tsv', '.html', '.htm', '.xml', '.ps1', '.py', '.js', '.ts', '.sh')
            if ($lintTextExtensions -notcontains $originalItem.Extension.ToLowerInvariant()) {
                $errors.Add('Non-text original found in shared source: ' + $sourceIdValue)
            }
            elseif ($originalItem.Length -gt 5MB) {
                $errors.Add('Shared source exceeds the 5 MB safety scan limit: ' + $sourceIdValue)
            }
            else {
                $sourceRisk = Get-RiskAssessment -Content (Read-Utf8Text -FilePath $originalPath)
                if ($sourceRisk.secret_hits.Count -gt 0) {
                    $errors.Add('Secret indicator in source original: ' + $sourceIdValue)
                }
            }
        }
    }

    $wikiRoot = Join-Path $VaultRoot 'wiki'
    $allWikiFiles = @(Get-ChildItem -LiteralPath $wikiRoot -Filter '*.md' -File -Recurse -ErrorAction SilentlyContinue)
    $wikiFiles = @($allWikiFiles | Where-Object { $_.Name -ne 'index.md' })
    $pageKeys = @{}
    foreach ($file in $allWikiFiles) {
        $relative = $file.FullName.Substring($wikiRoot.Length + 1).Replace('\', '/')
        $withoutExtension = [IO.Path]::ChangeExtension($relative, $null)
        $pageKeys[$withoutExtension.ToLowerInvariant()] = $true
        $pageKeys[$file.BaseName.ToLowerInvariant()] = $true
    }
    foreach ($file in $wikiFiles) {
        $content = Read-Utf8Text -FilePath $file.FullName
        foreach ($field in @('title', 'type', 'status', 'summary', 'last_verified', 'sources')) {
            if ($null -eq (Get-FrontmatterValue -Content $content -Name $field)) {
                $errors.Add("Missing frontmatter '$field': $($file.FullName)")
            }
        }
        $status = Get-FrontmatterValue -Content $content -Name 'status'
        if ($status -and $allowedStatuses -notcontains $status) { $errors.Add("Invalid status '$status': $($file.FullName)") }
        $pageType = Get-FrontmatterValue -Content $content -Name 'type'
        $pageSources = @(Get-FrontmatterList -Content $content -Name 'sources')
        if ($status -eq 'verified' -and $pageType -ne 'system' -and $pageSources.Count -eq 0) {
            $errors.Add('Verified non-system page has no sources: ' + $file.FullName)
        }
        foreach ($pageSource in $pageSources) {
            if (-not $sourceMap.ContainsKey($pageSource)) { $errors.Add("Unknown source ID '$pageSource': $($file.FullName)") }
        }
        $risk = Get-RiskAssessment -Content $content
        if ($risk.secret_hits.Count -gt 0) { $errors.Add('Secret indicator in stable wiki: ' + $file.FullName + ' [' + ($risk.secret_hits -join ', ') + ']') }
        if ($risk.injection_hits.Count -gt 0) { $errors.Add('Injection indicator in stable wiki: ' + $file.FullName + ' [' + ($risk.injection_hits -join ', ') + ']') }
        foreach ($match in [regex]::Matches($content, '\[\[([^\]|#]+)')) {
            $target = $match.Groups[1].Value.Trim().Replace('\', '/')
            if ($target.EndsWith('.md', [StringComparison]::OrdinalIgnoreCase)) {
                $target = $target.Substring(0, $target.Length - 3)
            }
            $target = $target.ToLowerInvariant()
            $targetLeaf = [IO.Path]::GetFileName($target)
            if (-not $pageKeys.ContainsKey($target) -and -not $pageKeys.ContainsKey($targetLeaf)) {
                $warnings.Add("Unresolved wikilink '[[$($match.Groups[1].Value)]]': $($file.FullName)")
            }
        }
    }

    $proposalRoot = Join-Path $VaultRoot 'proposals'
    $proposalMap = @{}
    $proposalFiles = @(Get-ChildItem -LiteralPath $proposalRoot -Filter '*.md' -File -Recurse -ErrorAction SilentlyContinue)
    foreach ($file in $proposalFiles) {
        $content = Read-Utf8Text -FilePath $file.FullName
        foreach ($field in @('id', 'type', 'status', 'title', 'agent', 'created_at', 'classification', 'source_ids', 'target_pages')) {
            if ($null -eq (Get-FrontmatterValue -Content $content -Name $field)) {
                $errors.Add("Missing proposal frontmatter '$field': $($file.FullName)")
            }
        }
        $proposalIdValue = Get-FrontmatterValue -Content $content -Name 'id'
        $proposalAgent = Get-FrontmatterValue -Content $content -Name 'agent'
        $proposalStatus = Get-FrontmatterValue -Content $content -Name 'status'
        $proposalType = Get-FrontmatterValue -Content $content -Name 'type'
        $proposalClassification = Get-FrontmatterValue -Content $content -Name 'classification'
        if ($proposalIdValue) {
            if ($proposalIdValue -notmatch '^prop-\d{8}T\d{9}Z-[a-f0-9]{8}$') { $errors.Add('Invalid proposal ID: ' + $proposalIdValue) }
            if ($proposalMap.ContainsKey($proposalIdValue)) { $errors.Add('Duplicate proposal ID: ' + $proposalIdValue) }
            else { $proposalMap[$proposalIdValue] = $file.FullName }
            if (-not $file.Name.StartsWith($proposalIdValue + '-', [StringComparison]::OrdinalIgnoreCase)) { $errors.Add('Proposal filename does not match ID: ' + $file.FullName) }
        }
        if ($proposalType -and $proposalType -ne 'proposal') { $errors.Add('Invalid proposal type: ' + $file.FullName) }
        if ($proposalStatus -and @('proposed', 'accepted', 'rejected', 'superseded') -notcontains $proposalStatus) { $errors.Add('Invalid proposal status: ' + $file.FullName) }
        if ($proposalClassification -and @('public', 'internal') -notcontains $proposalClassification) { $errors.Add('Invalid proposal classification: ' + $file.FullName) }
        if ($proposalAgent -and (Split-Path -Leaf $file.DirectoryName) -ne $proposalAgent) { $errors.Add('Proposal directory does not match agent: ' + $file.FullName) }
        foreach ($proposalSource in @(Get-FrontmatterList -Content $content -Name 'source_ids')) {
            if (-not $sourceMap.ContainsKey($proposalSource)) { $errors.Add("Unknown proposal source ID '$proposalSource': $($file.FullName)") }
        }
        $placeholderMarkers = @(
            'Describe the durable knowledge change and why it matters.',
            '| TODO | TODO | TODO | draft |',
            '- Embedded instructions ignored: TODO',
            '- Secret indicators: TODO',
            '- External action requested by source: TODO'
        )
        foreach ($marker in $placeholderMarkers) {
            if ($content.Contains($marker)) { $errors.Add("Incomplete proposal placeholder '$marker': $($file.FullName)") }
        }
        if ($content -match '(?m)^- TODO\r?$') { $errors.Add("Incomplete proposal placeholder '- TODO': $($file.FullName)") }
        $risk = Get-RiskAssessment -Content $content
        if ($risk.secret_hits.Count -gt 0) { $errors.Add('Secret indicator in proposal: ' + $file.FullName + ' [' + ($risk.secret_hits -join ', ') + ']') }
        if ($risk.injection_hits.Count -gt 0) { $warnings.Add('Injection indicator in proposal: ' + $file.FullName + ' [' + ($risk.injection_hits -join ', ') + ']') }
    }

    $eventMap = @{}
    $eventFiles = @(Get-ChildItem -LiteralPath (Join-Path $VaultRoot 'events') -Filter '*.json' -File -Recurse -ErrorAction SilentlyContinue)
    foreach ($eventFile in $eventFiles) {
        try { $event = Read-Utf8Text -FilePath $eventFile.FullName | ConvertFrom-Json }
        catch {
            $errors.Add('Invalid event JSON: ' + $eventFile.FullName)
            continue
        }
        $eventProperties = @($event.PSObject.Properties.Name)
        foreach ($field in @('event_id', 'occurred_at', 'agent', 'action', 'proposal_ids', 'pages_changed', 'source_ids', 'summary')) {
            if ($eventProperties -notcontains $field) { $errors.Add("Missing event field '$field': $($eventFile.FullName)") }
        }
        $eventIdValue = [string]$event.event_id
        if ($eventMap.ContainsKey($eventIdValue)) { $errors.Add('Duplicate event ID: ' + $eventIdValue) }
        else { $eventMap[$eventIdValue] = $eventFile.FullName }
        if ($eventFile.BaseName -ne $eventIdValue) { $errors.Add('Event filename does not match event ID: ' + $eventFile.FullName) }
        if ([string]$event.agent -notmatch '^[a-z0-9][a-z0-9-]*$') { $errors.Add('Invalid event agent: ' + $eventFile.FullName) }
        if (@('stage-source', 'new-proposal', 'record-curation') -notcontains [string]$event.action) { $errors.Add('Invalid event action: ' + $eventFile.FullName) }
        try {
            if ($event.occurred_at -isnot [DateTime] -and $event.occurred_at -isnot [DateTimeOffset]) {
                [void][DateTimeOffset]::Parse(
                    [string]$event.occurred_at,
                    [Globalization.CultureInfo]::InvariantCulture,
                    [Globalization.DateTimeStyles]::RoundtripKind
                )
            }
        }
        catch { $errors.Add('Invalid event occurred_at: ' + $eventFile.FullName) }
        $eventRisk = Get-RiskAssessment -Content ([string]$event.summary)
        if ($eventRisk.secret_hits.Count -gt 0) { $errors.Add('Secret indicator in event summary: ' + $eventFile.FullName) }
        if ($eventRisk.injection_hits.Count -gt 0) { $errors.Add('Injection indicator in event summary: ' + $eventFile.FullName) }
        foreach ($eventSourceId in @($event.source_ids)) {
            if (-not $sourceMap.ContainsKey([string]$eventSourceId)) { $errors.Add("Unknown event source ID '$eventSourceId': $($eventFile.FullName)") }
        }
        foreach ($eventProposalId in @($event.proposal_ids)) {
            if (-not $proposalMap.ContainsKey([string]$eventProposalId)) { $errors.Add("Unknown event proposal ID '$eventProposalId': $($eventFile.FullName)") }
        }
        foreach ($eventPage in @($event.pages_changed)) {
            $relativeEventPage = ([string]$eventPage).Replace('/', '\')
            if ([IO.Path]::IsPathRooted($relativeEventPage)) { $errors.Add('Rooted page path in event: ' + $eventFile.FullName); continue }
            $eventPagePath = Get-NormalizedPath (Join-Path $wikiRoot $relativeEventPage)
            $wikiRootNormalized = Get-NormalizedPath $wikiRoot
            if (-not $eventPagePath.StartsWith($wikiRootNormalized + '\', [StringComparison]::OrdinalIgnoreCase) -or -not (Test-Path -LiteralPath $eventPagePath)) {
                $errors.Add("Invalid event page '$eventPage': $($eventFile.FullName)")
            }
        }
    }

    $lockPath = Join-Path $VaultRoot 'governance\curator.lock'
    if (Test-Path -LiteralPath $lockPath) {
        try {
            $lock = Read-Utf8Text -FilePath $lockPath | ConvertFrom-Json
            $ageMinutes = ([DateTime]::UtcNow - [DateTime]::Parse([string]$lock.acquired_at).ToUniversalTime()).TotalMinutes
            if ($ageMinutes -gt [double]$vaultConfig.curator_lock_minutes) {
                $warnings.Add('Curator lock is stale and requires human review: ' + $lockPath)
            }
        }
        catch { $errors.Add('Invalid curator lock: ' + $lockPath) }
    }

    $result = [ordered]@{
        action = 'lint'
        vault_path = $VaultRoot
        source_count = $sourceMap.Count
        proposal_count = $proposalMap.Count
        event_count = $eventMap.Count
        wiki_page_count = $wikiFiles.Count
        error_count = $errors.Count
        warning_count = $warnings.Count
        errors = @($errors)
        warnings = @($warnings)
    }
    Write-JsonOutput $result
    if ($errors.Count -gt 0) { exit 2 }
}

function Invoke-ContributorPythonAction {
    param([string]$VaultRoot, [string]$RequestedAction)

    $candidatePaths = New-Object Collections.Generic.List[string]
    foreach ($candidate in @(
        (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python314\python.exe'),
        (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python313\python.exe'),
        (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'),
        (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python311\python.exe')
    )) {
        if ($candidate) { $candidatePaths.Add($candidate) }
    }
    $pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($pythonCommand) { $candidatePaths.Add([string]$pythonCommand.Source) }
    $python = @($candidatePaths | Where-Object { $_ -and (Test-Path -LiteralPath $_ -PathType Leaf) } | Select-Object -Unique)[0]
    if (-not $python) {
        throw 'project-brief, record-handoff, verify-context, new-proposal, and lint require the local Python contributor gateway, but python.exe was not found.'
    }
    $gateway = Join-Path $PSScriptRoot 'knowledge.py'
    # -I excludes both CWD and the script directory. Admit only this trusted
    # gateway's sibling modules explicitly; keep environment/user-site isolation.
    $bootstrap = "import runpy,sys; from pathlib import Path; p=Path(sys.argv[1]).resolve(); sys.argv=sys.argv[1:]; sys.path.insert(0,str(p.parent)); runpy.run_path(str(p),run_name='__main__')"
    $arguments = @('-I', '-X', 'utf8', '-B', '-c', $bootstrap, $gateway, $RequestedAction, '--vault', $VaultRoot)
    if ($RequestedAction -in @('lint', 'status')) {
        # Share integrity and curation accounting with the cross-platform gateway.
    }
    elseif ($RequestedAction -eq 'project-brief') {
        $arguments += @('--project-slug', $ProjectSlug, '--limit', [string]$Limit, '--offset', [string]$Offset)
        if (-not [string]::IsNullOrWhiteSpace($Query)) { $arguments += @('--query', $Query) }
        if ($RequireContext) { $arguments += '--require-context' }
    }
    elseif ($RequestedAction -eq 'inventory') {
        $arguments += @('--limit', [string]$Limit, '--offset', [string]$Offset)
        if (-not [string]::IsNullOrWhiteSpace($ProjectSlug)) { $arguments += @('--project-slug', $ProjectSlug) }
        if (-not [string]::IsNullOrWhiteSpace($Query)) { $arguments += @('--query', $Query) }
    }
    elseif ($RequestedAction -eq 'begin-task') {
        $arguments += @('--agent', $Agent, '--session-id', $SessionId, '--project-slug', $ProjectSlug, '--limit', [string]$Limit)
        if ($RequireContext) { $arguments += '--require-context' }
        if (-not [string]::IsNullOrWhiteSpace($ReceiptRoot)) { $arguments += @('--receipt-root', $ReceiptRoot) }
    }
    elseif ($RequestedAction -eq 'verify-context') {
        $arguments += @('--context-receipt', $ContextReceipt, '--agent', $Agent, '--session-id', $SessionId, '--project-slug', $ProjectSlug)
    }
    elseif ($RequestedAction -eq 'record-session') {
        $arguments += @('--agent', $Agent, '--session-id', $SessionId, '--checkpoint-id', $CheckpointId, '--outcome', $Outcome, '--classification', $Classification)
        if (-not [string]::IsNullOrWhiteSpace($ProjectSlug)) { $arguments += @('--project-slug', $ProjectSlug) }
        if (-not [string]::IsNullOrWhiteSpace($Path)) { $arguments += @('--path', $Path) }
        if (-not [string]::IsNullOrWhiteSpace($Summary)) { $arguments += @('--summary', $Summary) }
        if (-not [string]::IsNullOrWhiteSpace($ContextReceipt)) { $arguments += @('--context-receipt', $ContextReceipt) }
        if (-not [string]::IsNullOrWhiteSpace($RetainTempRoot)) { $arguments += @('--retain-temp-root', $RetainTempRoot) }
        if (-not [string]::IsNullOrWhiteSpace($ReceiptRoot)) { $arguments += @('--receipt-root', $ReceiptRoot) }
        if (-not [string]::IsNullOrWhiteSpace($LockRoot)) { $arguments += @('--lock-root', $LockRoot) }
    }
    elseif ($RequestedAction -eq 'new-proposal') {
        $arguments += @(
            '--title', $Title,
            '--agent', $Agent,
            '--classification', $Classification
        )
        foreach ($source in @($SourceId)) {
            $arguments += @('--source-id', [string]$source)
        }
    }
    elseif ($RequestedAction -eq 'checkpoint-intake') {
        if (@($ProposalId).Count -ne 1 -or [string]::IsNullOrWhiteSpace([string]$ProposalId[0])) {
            throw 'checkpoint-intake requires exactly one -ProposalId.'
        }
        $arguments += @('--proposal-id', [string]$ProposalId[0])
        if ($RecoverStaleLock) { $arguments += '--recover-stale-lock' }
    }
    else {
        $arguments += @(
            '--path', $Path,
            '--title', $Title,
            '--origin', $Origin,
            '--agent', $Agent,
            '--classification', $Classification,
            '--project-slug', $ProjectSlug,
            '--state-summary', $StateSummary
        )
        foreach ($target in @($TargetPage)) {
            $arguments += @('--target-page', [string]$target)
        }
        if (-not [string]::IsNullOrWhiteSpace($ContextReceipt)) { $arguments += @('--context-receipt', $ContextReceipt) }
        if (-not [string]::IsNullOrWhiteSpace($SessionId)) { $arguments += @('--session-id', $SessionId) }
    }
    $previousPythonIoEncoding = $env:PYTHONIOENCODING
    $previousNoBytecode = $env:PYTHONDONTWRITEBYTECODE
    $previousOutputEncoding = $OutputEncoding
    $previousConsoleOutputEncoding = [Console]::OutputEncoding
    try {
        $utf8 = New-Object Text.UTF8Encoding($false)
        $env:PYTHONIOENCODING = 'utf-8'
        $env:PYTHONDONTWRITEBYTECODE = '1'
        $OutputEncoding = $utf8
        [Console]::OutputEncoding = $utf8
        & $python @arguments
        $script:ContributorExitCode = $LASTEXITCODE
    }
    finally {
        $env:PYTHONIOENCODING = $previousPythonIoEncoding
        $env:PYTHONDONTWRITEBYTECODE = $previousNoBytecode
        $OutputEncoding = $previousOutputEncoding
        [Console]::OutputEncoding = $previousConsoleOutputEncoding
    }
}

if ($Action -eq 'init') {
    Invoke-Init -Target $Vault
    exit 0
}

$vaultRoot = Resolve-VaultPath -Requested $Vault
if ($Action -in @('record-handoff', 'record-session', 'project-brief', 'inventory', 'begin-task', 'verify-context', 'new-proposal', 'checkpoint-intake', 'lint')) {
    $script:ContributorExitCode = 2
    Invoke-ContributorPythonAction -VaultRoot $vaultRoot -RequestedAction $Action
    exit $script:ContributorExitCode
}
switch ($Action) {
    'status' { Invoke-Status -VaultRoot $vaultRoot }
    'stage-source' { Invoke-StageSource -VaultRoot $vaultRoot -SourcePath $Path -SourceTitle $Title -SourceOrigin $Origin -SourceClassification $Classification -SourceAgent $Agent -UseLocalOnly ([bool]$LocalOnly) }
    'new-proposal' { Invoke-NewProposal -VaultRoot $vaultRoot -ProposalAgent $Agent -ProposalTitle $Title -ProposalClassification $Classification -ProposalSourceIds $SourceId }
    'acquire-lock' { Invoke-AcquireLock -VaultRoot $vaultRoot -LockAgent $Agent -RequestedOwnerPid $OwnerPid }
    'release-lock' { Invoke-ReleaseLock -VaultRoot $vaultRoot -ExpectedLockId $LockId }
    'record-curation' { Invoke-RecordCuration -VaultRoot $vaultRoot -ExpectedLockId $LockId -CuratorAgent $Agent -CurationSummary $Summary -CurationPages $Page -CurationSourceIds $SourceId -CurationProposalIds $ProposalId }
    'refresh' { Invoke-Refresh -VaultRoot $vaultRoot -ExpectedLockId $LockId }
    'lint' { Invoke-Lint -VaultRoot $vaultRoot }
}
