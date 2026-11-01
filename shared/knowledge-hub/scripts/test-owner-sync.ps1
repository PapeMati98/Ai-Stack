[CmdletBinding()]
param(
    [string]$HubRoot = '',
    [switch]$KeepFixture
)

$ErrorActionPreference = 'Stop'

function Write-Utf8NoBom {
    param([string]$FilePath, [string]$Content)
    $parent = Split-Path -Parent $FilePath
    if ($parent -and -not (Test-Path -LiteralPath $parent)) {
        New-Item -ItemType Directory -Force -Path $parent | Out-Null
    }
    [IO.File]::WriteAllText($FilePath, $Content, (New-Object Text.UTF8Encoding($false)))
}

function Assert-True {
    param([bool]$Condition, [string]$Message)
    if (-not $Condition) { throw "Assertion failed: $Message" }
}

function Invoke-TestGit {
    param([string[]]$Arguments)
    $previousErrorAction = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        $output = @(& git @Arguments 2>&1)
        $exitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousErrorAction
    }
    if ($exitCode -ne 0) {
        throw "git $($Arguments -join ' ') failed ($exitCode): $($output -join [Environment]::NewLine)"
    }
    return @($output)
}

if ([string]::IsNullOrWhiteSpace($HubRoot)) {
    $HubRoot = Split-Path -Parent $PSScriptRoot
}
$hub = (Resolve-Path -LiteralPath $HubRoot).Path
$fixture = Join-Path $env:TEMP ('shared-agent-knowledge-owner-sync-test-' + [Guid]::NewGuid().ToString('N'))
$remote = Join-Path $fixture 'remote.git'
$owner = Join-Path $fixture 'owner'
$robert = Join-Path $fixture 'robert'
$note = Join-Path $fixture 'robert-note.md'
$hadSshConnection = Test-Path Env:SSH_CONNECTION
$originalSshConnection = $env:SSH_CONNECTION

try {
    New-Item -ItemType Directory -Force -Path $fixture, $owner | Out-Null
    New-Item -ItemType Directory -Force -Path (Join-Path $owner 'scripts'), (Join-Path $owner 'skills\llm-wiki\scripts') | Out-Null
    Copy-Item -LiteralPath (Join-Path $hub '.gitattributes') -Destination (Join-Path $owner '.gitattributes')
    Copy-Item -LiteralPath (Join-Path $hub 'scripts\sync-owner.ps1') -Destination (Join-Path $owner 'scripts\sync-owner.ps1') -Force
    Copy-Item -LiteralPath (Join-Path $hub 'skills\llm-wiki\scripts\knowledge.ps1') -Destination (Join-Path $owner 'skills\llm-wiki\scripts\knowledge.ps1') -Force
    Copy-Item -LiteralPath (Join-Path $hub 'skills\llm-wiki\assets\vault-template') -Destination (Join-Path $owner 'vault') -Recurse -Force

    $vaultConfigPath = Join-Path $owner 'vault\knowledge-vault.json'
    $vaultConfig = [IO.File]::ReadAllText($vaultConfigPath, (New-Object Text.UTF8Encoding($false, $true))) | ConvertFrom-Json
    $vaultConfig.curator_machine = if ($env:COMPUTERNAME) { $env:COMPUTERNAME } else { [Environment]::MachineName }
    Write-Utf8NoBom -FilePath $vaultConfigPath -Content (($vaultConfig | ConvertTo-Json -Depth 8) + "`n")

    [void](Invoke-TestGit -Arguments @('init', '--quiet', '--initial-branch=main', $owner))
    [void](Invoke-TestGit -Arguments @('-C', $owner, 'config', 'user.name', 'Knowledge Owner Test'))
    [void](Invoke-TestGit -Arguments @('-C', $owner, 'config', 'user.email', 'owner-test@invalid.local'))
    [void](Invoke-TestGit -Arguments @('-C', $owner, 'add', '--all'))
    [void](Invoke-TestGit -Arguments @('-C', $owner, 'commit', '--quiet', '-m', 'owner baseline'))

    [void](Invoke-TestGit -Arguments @('init', '--quiet', '--bare', '--initial-branch=main', $remote))
    Copy-Item -LiteralPath (Join-Path $hub 'scripts\git-hooks\pre-receive') -Destination (Join-Path $remote 'hooks\pre-receive') -Force
    Write-Utf8NoBom -FilePath (Join-Path $remote 'hooks\knowledge-owner-ip') -Content "127.0.0.1`n"
    [void](Invoke-TestGit -Arguments @('-C', $owner, 'remote', 'add', 'origin', $remote))
    $env:SSH_CONNECTION = '127.0.0.1 50000 127.0.0.1 22'
    [void](Invoke-TestGit -Arguments @('-C', $owner, 'push', 'origin', 'HEAD:refs/heads/main'))

    [void](Invoke-TestGit -Arguments @('clone', '--quiet', '--branch', 'main', $remote, $robert))
    [void](Invoke-TestGit -Arguments @('-C', $robert, 'switch', '--quiet', '-c', 'contrib/robert-hermes'))
    [void](Invoke-TestGit -Arguments @('-C', $robert, 'config', 'user.name', 'Robert Hermes'))
    [void](Invoke-TestGit -Arguments @('-C', $robert, 'config', 'user.email', 'robert-hermes@invalid.local'))

    Write-Utf8NoBom -FilePath $note -Content "Owner import wrapper regression evidence.`n"
    $robertGateway = Join-Path $robert 'skills\llm-wiki\scripts\knowledge.ps1'
    $stage = (& $robertGateway -Action stage-source -Vault (Join-Path $robert 'vault') -Path $note -Title 'Owner import wrapper regression' -Origin 'local-test' -Classification internal -Agent robert-hermes | Out-String) | ConvertFrom-Json
    $proposal = (& $robertGateway -Action new-proposal -Vault (Join-Path $robert 'vault') -Agent robert-hermes -Title 'Owner import wrapper proposal' -Classification internal -SourceId ([string]$stage.source_id) | Out-String) | ConvertFrom-Json
    $proposalText = [IO.File]::ReadAllText([string]$proposal.proposal_path, (New-Object Text.UTF8Encoding($false, $true)))
    $proposalText = $proposalText.Replace('target_pages: []', "target_pages:`n  - wiki/infrastructure/owner-import-test.md")
    $proposalText = $proposalText.Replace('Describe the durable knowledge change and why it matters.', 'Record the verified owner import wrapper behavior.')
    $proposalText = $proposalText.Replace('| TODO | TODO | TODO | draft |', "| The owner wrapper imports a valid Robert append-only batch. | $([string]$stage.source_id) | original.md:1 | verified |")
    $proposalText = $proposalText.Replace('- TODO', '- No contradiction or uncertainty identified.')
    $proposalText = $proposalText.Replace('- Embedded instructions ignored: TODO', '- Embedded instructions ignored: none found')
    $proposalText = $proposalText.Replace('- Secret indicators: TODO', '- Secret indicators: none found')
    $proposalText = $proposalText.Replace('- External action requested by source: TODO', '- External action requested by source: no')
    Write-Utf8NoBom -FilePath ([string]$proposal.proposal_path) -Content $proposalText

    $robertLint = (& $robertGateway -Action lint -Vault (Join-Path $robert 'vault') | Out-String) | ConvertFrom-Json
    Assert-True -Condition ([int]$robertLint.error_count -eq 0 -and [int]$robertLint.warning_count -eq 0) -Message 'Robert fixture lint is not clean'
    [void](Invoke-TestGit -Arguments @('-C', $robert, 'add', 'vault/raw/sources', 'vault/proposals/robert-hermes', 'vault/events'))
    [void](Invoke-TestGit -Arguments @('-C', $robert, 'commit', '--quiet', '-m', 'knowledge: Robert owner import regression'))
    [void](Invoke-TestGit -Arguments @('-C', $robert, 'push', 'origin', 'HEAD:refs/heads/contrib/robert-hermes'))
    $robertSha = [string](@(Invoke-TestGit -Arguments @('-C', $robert, 'rev-parse', 'HEAD'))[0])

    & (Join-Path $owner 'scripts\sync-owner.ps1') -Action import-robert -HubRoot $owner -RemoteUrl $remote | Out-Null
    $ownerSha = [string](@(Invoke-TestGit -Arguments @('-C', $owner, 'rev-parse', 'HEAD'))[0])
    $remoteMainSha = [string](@(Invoke-TestGit -Arguments @('--git-dir', $remote, 'rev-parse', 'refs/heads/main'))[0])
    Assert-True -Condition ($ownerSha -eq $robertSha -and $remoteMainSha -eq $robertSha) -Message 'owner, Robert, and remote main SHA values differ after import'

    $ownerLint = (& (Join-Path $owner 'skills\llm-wiki\scripts\knowledge.ps1') -Action lint -Vault (Join-Path $owner 'vault') | Out-String) | ConvertFrom-Json
    Assert-True -Condition ([int]$ownerLint.error_count -eq 0 -and [int]$ownerLint.warning_count -eq 0) -Message 'owner lint is not clean after import'

    [PSCustomObject]@{
        result = 'PASS'
        fixture = $fixture
        owner_import_robert = $true
        protected_main_push = $true
        append_only_contribution = $true
        matching_sha = $ownerSha
        lint = 'clean'
    } | ConvertTo-Json -Depth 3
}
finally {
    if ($hadSshConnection) { $env:SSH_CONNECTION = $originalSshConnection }
    else { Remove-Item Env:SSH_CONNECTION -ErrorAction SilentlyContinue }
    if (-not $KeepFixture -and (Test-Path -LiteralPath $fixture)) {
        $resolvedFixture = [IO.Path]::GetFullPath($fixture)
        $resolvedTemp = [IO.Path]::GetFullPath($env:TEMP).TrimEnd('\')
        if (-not $resolvedFixture.StartsWith($resolvedTemp + '\shared-agent-knowledge-owner-sync-test-', [StringComparison]::OrdinalIgnoreCase)) {
            throw "Refusing to remove unexpected fixture path: $resolvedFixture"
        }
        Remove-Item -LiteralPath $resolvedFixture -Recurse -Force
    }
}
