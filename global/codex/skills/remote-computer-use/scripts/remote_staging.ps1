# Remote staging helper for the remote-computer-use skill.
#
# Functions-only import with no top-level effects: dot-source this file to
# reuse the staging derivation. The staging directory is resolved from the
# current authenticated guest TEMP and profile through one fixed read-only
# SSH query at actual operation time, never from a hardcoded operator path.
# Untrusted query output is validated strictly before any SCP copy or remote
# cleanup. This is a string and remote-metadata gate, not an OS sandbox and
# not a claim of live SSH compatibility.
#
# Remote invocation preserves spaces, Unicode and arguments as data through
# UTF-16LE -EncodedCommand text. Parameter names are emitted as names and
# every value is a single-quoted literal, so a dash-prefixed value cannot
# bind as a switch. An untrusted path or argument is never evaluated as code.
#
# All source is plain ASCII and runs on Windows PowerShell 5.1. No global
# encoding or registry state is changed.

function ConvertTo-RemoteLiteral([string]$Value) {
    if ($null -eq $Value) { throw 'Remote literal value is missing.' }
    if ($Value -match '[\x00-\x1F\x7F]') { throw 'Remote literal value contains a control character.' }
    return "'" + ($Value -replace "'", "''") + "'"
}

function ConvertTo-EncodedCommand([string]$Command) {
    if ([string]::IsNullOrEmpty($Command)) { throw 'Encoded command text is missing.' }
    $bytes = [System.Text.Encoding]::Unicode.GetBytes($Command)
    return [System.Convert]::ToBase64String($bytes)
}

function Assert-RemoteStagingPath([string]$Path) {
    if ([string]::IsNullOrEmpty($Path)) { throw 'Remote staging path is empty.' }
    if ($Path.Length -gt 1024) { throw 'Remote staging path exceeds the length bound.' }
    if ($Path -match '[\x00-\x1F\x7F]') { throw 'Remote staging path contains a control character.' }
    $normalized = $Path.Replace('\', '/')
    if ($normalized -notmatch '^[A-Za-z]:/') { throw 'Remote staging path is not a local absolute path.' }
    if ($normalized.Substring(2).Contains(':')) { throw 'Remote staging path must not contain an alternate stream.' }
    $body = $normalized.Substring(3)
    if ($body.EndsWith('/')) { $body = $body.Substring(0, $body.Length - 1) }
    if ([string]::IsNullOrEmpty($body)) { throw 'Remote staging path contains an empty or relative segment.' }
    $segments = $body.Split('/')
    $reserved = @(
        'CON', 'PRN', 'AUX', 'NUL',
        'COM1', 'COM2', 'COM3', 'COM4', 'COM5', 'COM6', 'COM7', 'COM8', 'COM9',
        'LPT1', 'LPT2', 'LPT3', 'LPT4', 'LPT5', 'LPT6', 'LPT7', 'LPT8', 'LPT9'
    )
    foreach ($segment in $segments) {
        if ($segment -eq '' -or $segment -eq '.' -or $segment -eq '..') {
            throw 'Remote staging path contains an empty or relative segment.'
        }
        if ($segment.Length -gt 255 -or $segment[0] -eq ' ' -or $segment[0] -eq '.' -or $segment.EndsWith(' ') -or $segment.EndsWith('.')) {
            throw 'Remote staging path contains an invalid segment.'
        }
        $device = $segment
        $dot = $segment.IndexOf('.')
        if ($dot -gt 0) { $device = $segment.Substring(0, $dot) }
        if ($reserved -contains $device.ToUpperInvariant() -or $device -match '^(COM|LPT)[\u00B9\u00B2\u00B3]$') {
            throw 'Remote staging path contains a device name.'
        }
        if ($segment -match '["''`$;|&<>(){}\[\]*?#%^]') {
            throw 'Remote staging path contains a shell metacharacter.'
        }
    }
    return ($normalized.Substring(0, 3) + $body)
}

function Assert-RemoteStagingRoot([string]$TempPath, [string]$ProfilePath) {
    $temp = Assert-RemoteStagingPath $TempPath
    $profile = Assert-RemoteStagingPath $ProfilePath
    $profileBound = $profile + '/'
    if (-not $temp.StartsWith($profileBound, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw 'Remote staging TEMP is outside the current authenticated profile.'
    }
    return $temp
}

function Join-RemoteStagingPath([string]$Root, [string]$Leaf) {
    $root = Assert-RemoteStagingPath $Root
    if ([string]::IsNullOrEmpty($Leaf)) { throw 'Remote staging leaf name is missing.' }
    if ($Leaf -notmatch '\A[A-Za-z0-9][A-Za-z0-9._-]{0,127}\z') {
        throw 'Remote staging leaf name must be a plain file name.'
    }
    return Assert-RemoteStagingPath ($root + '/' + $Leaf)
}

function Get-RemoteStagingQuery {
    # Use the same lexical validators on the guest BEFORE any metadata probe.
    # Only trusted function source is inserted here; candidate paths remain data.
    $validators = ''
    foreach ($name in @('Assert-RemoteStagingPath', 'Assert-RemoteStagingRoot')) {
        $command = Get-Command -Name $name -CommandType Function -ErrorAction Stop
        $definition = $command.ScriptBlock.Ast
        # Windows PowerShell 5.1 exposes the complete FunctionDefinitionAst.
        # Only a ScriptBlockAst may need its immediate function parent.
        if ($definition -is [System.Management.Automation.Language.ScriptBlockAst]) {
            $definition = $definition.Parent
        }
        if ($command -isnot [System.Management.Automation.FunctionInfo] -or $command.Name -cne $name -or
            $definition -isnot [System.Management.Automation.Language.FunctionDefinitionAst] -or $definition.Name -cne $name) {
            throw 'Remote staging validator definition is unavailable.'
        }
        # Copy the complete declaration, including its inline parameter list.
        $validators += $definition.Extent.Text + "`n"
    }
    $query = $validators + @'
$ErrorActionPreference = 'Stop'
function Resolve-StagingDirectory([string]$Candidate) {
    $full = Assert-RemoteStagingPath $Candidate
    $cursor = Get-Item -LiteralPath $full -Force -ErrorAction Stop
    if ($cursor -isnot [System.IO.DirectoryInfo]) { throw 'Remote staging directory does not exist.' }
    while ($null -ne $cursor) {
        if (($cursor.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw 'Remote staging path refuses a reparse point.'
        }
        $cursor = $cursor.Parent
    }
    return $full
}
$temp = Assert-RemoteStagingRoot $env:TEMP $env:USERPROFILE
$profile = Assert-RemoteStagingPath $env:USERPROFILE
$resolvedTemp = Resolve-StagingDirectory $temp
$resolvedProfile = Resolve-StagingDirectory $profile
# ASCII wire lines avoid native console code-page loss for Unicode profiles.
[Console]::Out.WriteLine([Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($resolvedTemp)))
[Console]::Out.WriteLine([Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($resolvedProfile)))
'@
    # Windows OpenSSH may use cmd.exe as its shell. Keep the encoded command
    # below that shell's length limit without maintaining a second validator.
    # Only trusted helper source is compressed; guest paths stay environment data.
    $memory = [System.IO.MemoryStream]::new()
    $compressor = [System.IO.Compression.DeflateStream]::new($memory, [System.IO.Compression.CompressionMode]::Compress, $true)
    try {
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($query)
        $compressor.Write($bytes, 0, $bytes.Length)
        $compressor.Dispose()
        $data = [Convert]::ToBase64String($memory.ToArray())
    }
    finally { $compressor.Dispose(); $memory.Dispose() }
    $bootstrap = @'
$ErrorActionPreference='Stop'
$m=[IO.MemoryStream]::new([Convert]::FromBase64String('QUERY_BYTES'))
$d=[IO.Compression.DeflateStream]::new($m,[IO.Compression.CompressionMode]::Decompress)
$r=[IO.StreamReader]::new($d,[Text.Encoding]::UTF8)
try { & ([scriptblock]::Create($r.ReadToEnd())) } finally { $r.Dispose(); $d.Dispose(); $m.Dispose() }
'@
    $bootstrap = $bootstrap.Replace('QUERY_BYTES', $data)
    if ((ConvertTo-EncodedCommand $bootstrap).Length -gt 7000) {
        throw 'Remote staging query exceeds the transport length bound.'
    }
    return $bootstrap
}

function Get-RemoteStagingRoot([string]$HostAlias) {
    if ([string]::IsNullOrWhiteSpace($HostAlias)) { throw 'Host alias is missing.' }
    if ($HostAlias -notmatch '\A[A-Za-z0-9][A-Za-z0-9._-]{0,63}\z') { throw 'Host alias is invalid.' }
    $encoded = ConvertTo-EncodedCommand (Get-RemoteStagingQuery)
    $raw = @(& ssh.exe -o BatchMode=yes -o ConnectTimeout=15 $HostAlias `
        powershell.exe -NoLogo -NoProfile -NonInteractive -EncodedCommand $encoded)
    $code = $LASTEXITCODE
    if ($null -eq $code -or $code -ne 0) {
        throw "Remote staging query failed on ${HostAlias} (exit $code)."
    }
    if ($raw.Count -ne 2) { throw 'Remote staging query must return exactly the TEMP and profile lines.' }
    $lines = New-Object System.Collections.Generic.List[string]
    foreach ($item in $raw) {
        if ($item -isnot [string]) { throw 'Remote staging query returned a non-text line.' }
        $text = [string]$item
        if ($text.EndsWith("`r")) { $text = $text.Substring(0, $text.Length - 1) }
        if ($text -match '[\r\n]') { throw 'Remote staging query returned a multiline line.' }
        if ($text.Length -gt 8192 -or $text -notmatch '\A[A-Za-z0-9+/]+={0,2}\z') {
            throw 'Remote staging query returned an invalid encoded line.'
        }
        $utf8 = New-Object System.Text.UTF8Encoding($false, $true)
        $lines.Add($utf8.GetString([Convert]::FromBase64String($text))) | Out-Null
    }
    return Assert-RemoteStagingRoot $lines[0] $lines[1]
}

function ConvertTo-RemoteBoundParameters([string[]]$Arguments) {
    $switches = @{
        Enable = $true
        AllowEnabledDesktopUpgrade = $true
        ApplyTemplateConfiguration = $true
    }
    $bound = @{}
    if ($null -eq $Arguments) { return $bound }
    $items = @($Arguments)
    $index = 0
    while ($index -lt $items.Count) {
        $token = [string]$items[$index]
        if ($token -notmatch '\A-[A-Za-z][A-Za-z0-9]*\z') {
            throw 'Remote argument list contains an unexpected token.'
        }
        $name = $token.Substring(1)
        if ($bound.ContainsKey($name)) { throw 'Remote argument list contains a duplicate parameter.' }
        if ($switches.ContainsKey($name)) {
            $bound[$name] = $true
            $index += 1
            continue
        }
        if (($index + 1) -ge $items.Count) { throw 'Remote named argument is missing a value.' }
        $bound[$name] = [string]$items[$index + 1]
        $index += 2
    }
    return $bound
}

function New-RemoteInvocationText([string]$ScriptPath, [hashtable]$BoundParameters) {
    $remote = Assert-RemoteStagingPath $ScriptPath
    $builder = New-Object System.Text.StringBuilder
    [void]$builder.Append('& ')
    [void]$builder.Append((ConvertTo-RemoteLiteral $remote))
    if ($null -eq $BoundParameters) { $BoundParameters = @{} }
    foreach ($key in @($BoundParameters.Keys)) {
        $name = [string]$key
        if ($name -notmatch '\A[A-Za-z][A-Za-z0-9]*\z') { throw 'Remote parameter name is invalid.' }
        $value = $BoundParameters[$key]
        if ($value -is [bool] -or $value -is [switch]) {
            if ($value) {
                [void]$builder.Append(' -')
                [void]$builder.Append($name)
            }
            continue
        }
        if ($null -eq $value) { throw 'Remote parameter value is missing.' }
        [void]$builder.Append(' -')
        [void]$builder.Append($name)
        [void]$builder.Append(' ')
        [void]$builder.Append((ConvertTo-RemoteLiteral ([string]$value)))
    }
    return $builder.ToString()
}

function Invoke-RemoteStagedScript {
    param(
        [Parameter(Mandatory)][string]$HostAlias,
        [Parameter(Mandatory)][string]$ScriptPath,
        [string[]]$Arguments,
        [hashtable]$BoundParameters
    )
    if ([string]::IsNullOrWhiteSpace($HostAlias)) { throw 'Host alias is missing.' }
    if ($HostAlias -notmatch '\A[A-Za-z0-9][A-Za-z0-9._-]{0,63}\z') { throw 'Host alias is invalid.' }
    $remote = Assert-RemoteStagingPath $ScriptPath
    $suppliedArguments = ($null -ne $Arguments -and @($Arguments).Count -gt 0)
    if ($null -ne $BoundParameters -and $suppliedArguments) {
        throw 'Pass either argument tokens or bound parameters, not both.'
    }
    if ($null -eq $BoundParameters) {
        $BoundParameters = ConvertTo-RemoteBoundParameters $Arguments
    }
    $encoded = ConvertTo-EncodedCommand (New-RemoteInvocationText $remote $BoundParameters)
    $output = @(& ssh.exe -o BatchMode=yes -o ConnectTimeout=15 $HostAlias `
        powershell.exe -NoLogo -NoProfile -NonInteractive -EncodedCommand $encoded)
    $code = $LASTEXITCODE
    if ($null -eq $code) { throw 'Remote invocation did not return a native exit code.' }
    return [pscustomobject]@{
        ExitCode = $code
        Output   = @($output)
    }
}

function Remove-RemoteStagedFile([string]$HostAlias, [string]$ScriptPath) {
    if ([string]::IsNullOrWhiteSpace($HostAlias)) { throw 'Host alias is missing.' }
    if ($HostAlias -notmatch '\A[A-Za-z0-9][A-Za-z0-9._-]{0,63}\z') { throw 'Host alias is invalid.' }
    $remote = Assert-RemoteStagingPath $ScriptPath
    $command = 'Remove-Item -LiteralPath ' + (ConvertTo-RemoteLiteral $remote) + ' -Force -ErrorAction SilentlyContinue'
    $encoded = ConvertTo-EncodedCommand $command
    $previousExitCode = $LASTEXITCODE
    try {
        & ssh.exe -o BatchMode=yes -o ConnectTimeout=15 $HostAlias `
            powershell.exe -NoLogo -NoProfile -NonInteractive -EncodedCommand $encoded | Out-Null
        if ($null -eq $LASTEXITCODE -or $LASTEXITCODE -ne 0) {
            Write-Warning 'Remote staged-file cleanup failed.'
        }
    }
    catch { Write-Warning 'Remote staged-file cleanup failed.' }
    finally { $global:LASTEXITCODE = $previousExitCode }
}
