# No advanced parameter binder: native -v/-p/-c and literal -- must pass through.
$CliArgs = @($args)
$ErrorActionPreference = 'Stop'
$native = Join-Path $env:USERPROFILE 'AppData\Local\Programs\MiMoCode\0.1.15\mimo.exe'
$profileRoot = Join-Path $env:USERPROFILE '.mimocode'
if (-not (Test-Path -LiteralPath $native -PathType Leaf)) { throw 'MiMo Code 0.1.15 is not installed.' }
$expectedHash = 'EA706738D7F00ACE7E63EA4BA7E55C738730B960C330FD212C17654131E99A4C'
if ((Get-FileHash -LiteralPath $native -Algorithm SHA256).Hash -ne $expectedHash) {
    throw 'MiMo executable differs from the pinned package. Revalidate before use.'
}
if (@(Get-ChildItem -LiteralPath (Split-Path -Parent $native) -Filter 'mimo.exe.old_*' -Force).Count -gt 0) {
    throw 'MiMo old upgrade files exist. Native startup would delete them; owner review required.'
}
$cache = Join-Path $profileRoot 'cache'
$cacheVersion = Join-Path $cache 'version'
if (Test-Path -LiteralPath $cacheVersion) {
    if ([IO.File]::ReadAllText($cacheVersion).Trim() -ne '21') {
        throw 'MiMo cache version mismatch. Preserve existing data; do not let native startup purge it.'
    }
} else {
    if ((Test-Path -LiteralPath $cache) -and @(Get-ChildItem -LiteralPath $cache -Force).Count -gt 0) {
        throw 'MiMo cache has data but no version marker. Inspection required before startup.'
    }
    [IO.Directory]::CreateDirectory($cache) | Out-Null
    [IO.File]::WriteAllText($cacheVersion, '21', [Text.UTF8Encoding]::new($false))
}
$env:MIMOCODE_HOME = $profileRoot
$env:MIMOCODE_ENABLE_ANALYSIS = 'false'
$env:MIMOCODE_AUTO_SHARE = 'false'
$env:MIMOCODE_AUTO_APPROVE_DELETE = 'false'
$env:MIMOCODE_DANGEROUSLY_SKIP_PERMISSIONS = 'false'
$env:DO_NOT_TRACK = '1'
$env:OTEL_SDK_DISABLED = 'true'
$env:MIMOCODE_DISABLE_CLAUDE_IMPORT = 'true'
foreach ($name in @('MIMOCODE_DISABLE_SHARE','MIMOCODE_DISABLE_AUTOUPDATE','MIMOCODE_DISABLE_PRUNE',
    'MIMOCODE_DISABLE_LOG_ROTATION','MIMOCODE_DISABLE_DEFAULT_PLUGINS','MIMOCODE_DISABLE_LSP_DOWNLOAD',
    'MIMOCODE_DISABLE_EMBEDDED_WEB_UI','MIMOCODE_MIMO_ONLY','MIMOCODE_DISABLE_PROVIDER_ENV',
    'MIMOCODE_DISABLE_CLAUDE_CODE_MCP','MIMOCODE_DISABLE_BUILTIN_SKILLS','MIMOCODE_DISABLE_OFFICIAL_SKILLS')) {
    [Environment]::SetEnvironmentVariable($name, 'true', 'Process')
}
# Shared ~/.agents/skills remains visible in interactive use; Fleet isolates its own runs.
& $native @CliArgs
exit $LASTEXITCODE
