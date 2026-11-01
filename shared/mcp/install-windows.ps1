param(
    [ValidateSet('all', 'codex', 'claude', 'qwen', 'grok', 'antigravity', 'kimi-desktop')]
    [string[]]$Tool = @('all'),
    [switch]$DryRun,
    [string]$RollbackTransaction
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$RegistryPath = Join-Path $PSScriptRoot 'mcp-registry.json'
$BackupRoot = Join-Path $env:USERPROFILE '.agents\backups\shared-mcp-registry'
$DeployedRegistryPath = Join-Path $env:USERPROFILE '.agents\mcp-registry.json'

function Write-Utf8NoBom([string]$Path, [string]$Content) {
    $parent = Split-Path -Parent $Path
    if ($parent) { New-Item -ItemType Directory -Force -Path $parent | Out-Null }
    [System.IO.File]::WriteAllText($Path, $Content, [System.Text.UTF8Encoding]::new($false))
}

function Get-FileSha256([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $null }
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Get-NormalizedPath([string]$Path) {
    return [System.IO.Path]::GetFullPath($Path).TrimEnd([System.IO.Path]::DirectorySeparatorChar)
}

function Assert-UnderRoot([string]$Candidate, [string]$Root, [string]$Label) {
    $candidatePath = Get-NormalizedPath $Candidate
    $rootPath = Get-NormalizedPath $Root
    if (-not ($candidatePath.Equals($rootPath, [StringComparison]::OrdinalIgnoreCase) -or
        $candidatePath.StartsWith($rootPath + [System.IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase))) {
        throw "$Label is outside approved root: $candidatePath"
    }
}

function Read-Registry {
    if (-not (Test-Path -LiteralPath $RegistryPath -PathType Leaf)) {
        throw "Registry not found: $RegistryPath"
    }
    $registry = Get-Content -LiteralPath $RegistryPath -Raw | ConvertFrom-Json
    if ($registry.schema_version -ne 1 -or $registry.managed_by -ne 'shared-agent-mcp-registry') {
        throw "Unsupported registry identity: $RegistryPath"
    }
    foreach ($server in @($registry.active_global)) {
        if (-not $server.name -or -not $server.url -or $server.transport -ne 'http' -or
            $server.auth -notin @('none', 'optional_client_oauth')) {
            throw "Invalid active_global entry: $($server.name)"
        }
        $uri = [Uri]$server.url
        if ($uri.Scheme -ne 'https') { throw "Only HTTPS remote MCP endpoints are allowed: $($server.name)" }
    }
    return $registry
}

function Get-ToolSpecs {
    $kimiMcp = Join-Path $env:APPDATA 'kimi-desktop\daimon-share\daimon\runtime\kimi-code\home\mcp.json'
    return [ordered]@{
        codex = [ordered]@{
            config = Join-Path $env:USERPROFILE '.codex\config.toml'
            executable = $null
        }
        claude = [ordered]@{
            config = Join-Path $env:USERPROFILE '.claude.json'
            executable = $null
        }
        qwen = [ordered]@{
            config = Join-Path $env:USERPROFILE '.qwen\settings.json'
            executable = $null
        }
        grok = [ordered]@{
            config = Join-Path $env:USERPROFILE '.grok\config.toml'
            executable = $null
        }
        antigravity = [ordered]@{
            config = Join-Path $env:USERPROFILE '.gemini\config\mcp_config.json'
            executable = $null
        }
        'kimi-desktop' = [ordered]@{
            config = $kimiMcp
            executable = $null
        }
    }
}

function Get-SelectedTools($Specs) {
    if ($Tool -contains 'all') {
        if ($Tool.Count -gt 1) { throw "Use either -Tool all or explicit tools, not both." }
        return @($Specs.Keys)
    }
    return @($Tool | Select-Object -Unique)
}

function Get-JsonMcpEntry([string]$Path, [string]$Name) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $null }
    try {
        $json = Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json
    }
    catch {
        throw "Invalid JSON MCP config: $Path"
    }
    if (-not $json.PSObject.Properties['mcpServers']) { return $null }
    $property = $json.mcpServers.PSObject.Properties[$Name]
    if (-not $property) { return $null }
    return $property.Value
}

function Get-EntryUrl($Entry) {
    if (-not $Entry) { return $null }
    foreach ($key in @('url', 'serverUrl', 'httpUrl')) {
        if ($Entry.PSObject.Properties[$key]) { return [string]$Entry.$key }
    }
    return $null
}

function Assert-NoConflict([string]$ToolName, [string]$Path, $Server) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return }
    if ([System.IO.Path]::GetExtension($Path) -eq '.json') {
        $entry = Get-JsonMcpEntry $Path ([string]$Server.name)
        if ($entry) {
            $existingUrl = Get-EntryUrl $entry
            if (-not $existingUrl -or $existingUrl -ne [string]$Server.url) {
                throw "Refusing to replace unmanaged/conflicting MCP '$($Server.name)' in ${ToolName}: $Path"
            }
        }
        return
    }

    $text = Get-Content -LiteralPath $Path -Raw
    $escapedName = [Regex]::Escape([string]$Server.name)
    $match = [Regex]::Match($text, "(?ms)^\[mcp_servers\.$escapedName\]\s*(.*?)(?=^\[|\z)")
    if ($match.Success) {
        $urlMatch = [Regex]::Match($match.Groups[1].Value, '(?m)^\s*url\s*=\s*["'']([^"'']+)["'']')
        if (-not $urlMatch.Success -or $urlMatch.Groups[1].Value -ne [string]$Server.url) {
            throw "Refusing to replace unmanaged/conflicting MCP '$($Server.name)' in ${ToolName}: $Path"
        }
    }
}

function New-BackupState([string]$Name, [string]$Path, [string]$TransactionRoot) {
    $exists = Test-Path -LiteralPath $Path -PathType Leaf
    $backupName = "$Name.config.bin"
    if ($exists) {
        Copy-Item -LiteralPath $Path -Destination (Join-Path $TransactionRoot $backupName) -Force
    }
    return [ordered]@{
        name = $Name
        target = $Path
        before_existed = [bool]$exists
        backup = if ($exists) { $backupName } else { $null }
        before_sha256 = if ($exists) { Get-FileSha256 $Path } else { $null }
        after_sha256 = $null
    }
}

function Restore-States($States, [string]$TransactionRoot, [switch]$CheckAfterHash) {
    foreach ($state in @($States)) {
        $target = [string]$state.target
        if ($CheckAfterHash -and $state.after_sha256) {
            $current = Get-FileSha256 $target
            if ($current -ne [string]$state.after_sha256) {
                throw "Refusing rollback because target drifted after deploy: $target"
            }
        }
        if ([bool]$state.before_existed) {
            $backup = Join-Path $TransactionRoot ([string]$state.backup)
            if (-not (Test-Path -LiteralPath $backup -PathType Leaf)) { throw "Missing backup: $backup" }
            $parent = Split-Path -Parent $target
            if ($parent) { New-Item -ItemType Directory -Force -Path $parent | Out-Null }
            Copy-Item -LiteralPath $backup -Destination $target -Force
            if ((Get-FileSha256 $target) -ne [string]$state.before_sha256) {
                throw "Rollback hash mismatch: $target"
            }
        }
        elseif (Test-Path -LiteralPath $target) {
            Remove-Item -LiteralPath $target -Force
        }
    }
}

function Invoke-Native([string]$Executable, [string[]]$Arguments) {
    if (-not (Get-Command $Executable -ErrorAction SilentlyContinue)) {
        throw "Required executable not found: $Executable"
    }
    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Executable exited with code $LASTEXITCODE" }
}

function Set-TomlServer([string]$Path, $Server) {
    $name = [string]$Server.name
    if ($name -notmatch '^[a-z0-9][a-z0-9-]*$') { throw "Unsafe MCP name for TOML: $name" }
    $url = [string]$Server.url
    if ($url.Contains('"') -or $url.Contains("`r") -or $url.Contains("`n")) {
        throw "Unsafe MCP URL for TOML: $name"
    }
    $parent = Split-Path -Parent $Path
    New-Item -ItemType Directory -Force -Path $parent | Out-Null
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        Write-Utf8NoBom $Path ""
    }
    $text = Get-Content -LiteralPath $Path -Raw
    $escapedName = [Regex]::Escape($name)
    if ([Regex]::IsMatch($text, "(?m)^\[mcp_servers\.$escapedName\]\s*$")) { return }
    $separator = if ($text.Length -eq 0 -or $text.EndsWith("`n")) { "" } else { "`r`n" }
    $block = $separator + "`r`n[mcp_servers.$name]`r`nurl = `"$url`"`r`n"
    [System.IO.File]::AppendAllText($Path, $block, [System.Text.UTF8Encoding]::new($false))
}

function Set-JsonServer([string]$ToolName, [string]$Path, $Server) {
    $parent = Split-Path -Parent $Path
    New-Item -ItemType Directory -Force -Path $parent | Out-Null
    if (Test-Path -LiteralPath $Path -PathType Leaf) {
        $json = Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json
    }
    else {
        $json = [pscustomobject]@{ mcpServers = [pscustomobject]@{} }
    }
    if (-not $json.PSObject.Properties['mcpServers']) {
        $json | Add-Member -NotePropertyName mcpServers -NotePropertyValue ([pscustomobject]@{})
    }
    $entry = switch ($ToolName) {
        'claude' {
            [pscustomobject]@{ type = 'http'; url = [string]$Server.url }
            break
        }
        'qwen' {
            [pscustomobject]@{ httpUrl = [string]$Server.url; description = [string]$Server.capability }
            break
        }
        'antigravity' {
            [pscustomobject]@{ disabled = $false; serverUrl = [string]$Server.url }
            break
        }
        'kimi-desktop' {
            [pscustomobject]@{ url = [string]$Server.url; transport = 'http' }
            break
        }
        default { throw "Unsupported JSON MCP client: $ToolName" }
    }
    $existing = $json.mcpServers.PSObject.Properties[[string]$Server.name]
    if ($existing) { $existing.Value = $entry }
    else { $json.mcpServers | Add-Member -NotePropertyName ([string]$Server.name) -NotePropertyValue $entry }
    Write-Utf8NoBom $Path (($json | ConvertTo-Json -Depth 30) + "`n")
}

function Add-Server([string]$ToolName, $Spec, $Server) {
    $name = [string]$Server.name
    $url = [string]$Server.url
    switch ($ToolName) {
        'codex' { Set-TomlServer ([string]$Spec.config) $Server }
        'claude' { Set-JsonServer $ToolName ([string]$Spec.config) $Server }
        'qwen' { Set-JsonServer $ToolName ([string]$Spec.config) $Server }
        'grok' { Set-TomlServer ([string]$Spec.config) $Server }
        'antigravity' { Set-JsonServer $ToolName ([string]$Spec.config) $Server }
        'kimi-desktop' { Set-JsonServer $ToolName ([string]$Spec.config) $Server }
        default { throw "Unsupported tool: $ToolName" }
    }
}

function Assert-Configured([string]$ToolName, [string]$Path, $Servers) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { throw "Config not created for ${ToolName}: $Path" }
    if ([System.IO.Path]::GetExtension($Path) -eq '.json') {
        foreach ($server in @($Servers)) {
            $entry = Get-JsonMcpEntry $Path ([string]$server.name)
            if (-not $entry -or (Get-EntryUrl $entry) -ne [string]$server.url) {
                throw "MCP verify failed for '$($server.name)' in ${ToolName}: $Path"
            }
        }
        return
    }
    $text = Get-Content -LiteralPath $Path -Raw
    foreach ($server in @($Servers)) {
        if (-not $text.Contains([string]$server.url)) {
            throw "MCP verify failed for '$($server.name)' in ${ToolName}: $Path"
        }
    }
}

if ($RollbackTransaction) {
    if ($RollbackTransaction -notmatch '^[0-9A-Za-z_-]+$') { throw "Invalid rollback transaction id." }
    $transactionRoot = Join-Path $BackupRoot $RollbackTransaction
    Assert-UnderRoot $transactionRoot $BackupRoot 'transaction root'
    $receiptPath = Join-Path $transactionRoot 'receipt.json'
    if (-not (Test-Path -LiteralPath $receiptPath -PathType Leaf)) { throw "Receipt not found: $receiptPath" }
    $receipt = Get-Content -LiteralPath $receiptPath -Raw | ConvertFrom-Json
    Restore-States $receipt.states $transactionRoot -CheckAfterHash
    $rollback = [ordered]@{
        transaction_id = $RollbackTransaction
        rolled_back_at = [DateTime]::UtcNow.ToString('o')
        status = 'ROLLED_BACK'
        restored = @($receipt.states).Count
    }
    Write-Utf8NoBom (Join-Path $transactionRoot 'rollback.json') (($rollback | ConvertTo-Json -Depth 10) + "`n")
    Write-Host ($rollback | ConvertTo-Json -Depth 10)
    exit 0
}

$registry = Read-Registry
$specs = Get-ToolSpecs
$selectedTools = Get-SelectedTools $specs
$activeServers = @($registry.active_global)

foreach ($toolName in $selectedTools) {
    $spec = $specs[$toolName]
    if ($spec.executable -and -not (Get-Command ([string]$spec.executable) -ErrorAction SilentlyContinue)) {
        throw "Required executable not found for ${toolName}: $($spec.executable)"
    }
    foreach ($server in $activeServers) {
        if (@($server.targets) -contains $toolName) {
            Assert-NoConflict $toolName ([string]$spec.config) $server
        }
    }
}

if ($DryRun) {
    $plan = [ordered]@{
        status = 'DRY_RUN'
        writes_performed = 0
        registry = $RegistryPath
        registry_sha256 = Get-FileSha256 $RegistryPath
        servers = @($activeServers | ForEach-Object { $_.name })
        targets = @($selectedTools | ForEach-Object { [ordered]@{ tool = $_; config = $specs[$_].config } })
    }
    Write-Host ($plan | ConvertTo-Json -Depth 10)
    exit 0
}

$transactionId = [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfffZ') + '-' + [Guid]::NewGuid().ToString('N').Substring(0, 8)
$transactionRoot = Join-Path $BackupRoot $transactionId
Assert-UnderRoot $transactionRoot $BackupRoot 'transaction root'
New-Item -ItemType Directory -Force -Path $transactionRoot | Out-Null

$states = @()
try {
    foreach ($toolName in $selectedTools) {
        $states += New-BackupState $toolName ([string]$specs[$toolName].config) $transactionRoot
    }
    $states += New-BackupState 'shared-registry' $DeployedRegistryPath $transactionRoot

    $pendingReceipt = [ordered]@{
        schema_version = 1
        managed_by = 'shared-agent-mcp-registry'
        transaction_id = $transactionId
        status = 'PENDING'
        started_at = [DateTime]::UtcNow.ToString('o')
        registry_sha256 = Get-FileSha256 $RegistryPath
        servers = @($activeServers | ForEach-Object { $_.name })
        tools = $selectedTools
        writes_performed = 0
        states = $states
    }
    Write-Utf8NoBom (Join-Path $transactionRoot 'receipt.json') (($pendingReceipt | ConvertTo-Json -Depth 20) + "`n")

    $registryParent = Split-Path -Parent $DeployedRegistryPath
    New-Item -ItemType Directory -Force -Path $registryParent | Out-Null
    Copy-Item -LiteralPath $RegistryPath -Destination $DeployedRegistryPath -Force

    foreach ($toolName in $selectedTools) {
        $spec = $specs[$toolName]
        $serversForTool = @($activeServers | Where-Object { @($_.targets) -contains $toolName })
        foreach ($server in $serversForTool) { Add-Server $toolName $spec $server }
        Assert-Configured $toolName ([string]$spec.config) $serversForTool
    }

    foreach ($state in $states) { $state.after_sha256 = Get-FileSha256 ([string]$state.target) }
    $receipt = [ordered]@{
        schema_version = 1
        managed_by = 'shared-agent-mcp-registry'
        transaction_id = $transactionId
        status = 'DEPLOYED'
        deployed_at = [DateTime]::UtcNow.ToString('o')
        registry_sha256 = Get-FileSha256 $RegistryPath
        servers = @($activeServers | ForEach-Object { $_.name })
        tools = $selectedTools
        writes_performed = $states.Count
        states = $states
    }
    Write-Utf8NoBom (Join-Path $transactionRoot 'receipt.json') (($receipt | ConvertTo-Json -Depth 20) + "`n")
    $summary = [ordered]@{
        transaction_id = $receipt.transaction_id
        status = $receipt.status
        servers = $receipt.servers
        tools = $receipt.tools
        writes_performed = $receipt.writes_performed
    }
    Write-Host ($summary | ConvertTo-Json -Depth 10)
}
catch {
    try { Restore-States $states $transactionRoot } catch { Write-Warning "Automatic rollback also failed: $_" }
    $failure = [ordered]@{
        transaction_id = $transactionId
        status = 'AUTO_ROLLED_BACK'
        failed_at = [DateTime]::UtcNow.ToString('o')
        error = $_.Exception.Message
    }
    Write-Utf8NoBom (Join-Path $transactionRoot 'failure.json') (($failure | ConvertTo-Json -Depth 10) + "`n")
    throw
}
