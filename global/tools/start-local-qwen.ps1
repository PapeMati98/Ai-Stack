[CmdletBinding()]
param(
    # Optional caller override; default is profile-local so no private cache path is baked in.
    [string]$ModelRoot = (Join-Path $env:USERPROFILE '.agents\local-qwen\models')
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$ollamaExe = (Join-Path $env:LOCALAPPDATA 'MateuszAI\ollama\0.33.2\ollama.exe')
$expectedSha = 'C79DF1E0C1BFA10ED813C7030AC4C3BA38BB0E350BD7322D9BB58320343235C6'
$ollamaDir = Split-Path -Path $ollamaExe -Parent
$modelRoot = $ModelRoot
$hostValue = '127.0.0.1:11439'
$port = 11439
$contextLength = '16384'
$parallel = '1'
$maxLoaded = '1'
$noCloud = '1'
$keepAlive = '5m'
$logRoot = (Join-Path $env:USERPROFILE '.agents\local-qwen\logs')
$started = Get-Date

function Full([string]$Path) { return ([IO.Path]::GetFullPath($Path)).TrimEnd('\') }

function Get-FinalPath([string]$Path) {
    if (-not ('QwenLauncherPathNative' -as [type])) {
        Add-Type @'
using System;
using System.Text;
using System.Runtime.InteropServices;
public static class QwenLauncherPathNative {
    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern uint GetFinalPathNameByHandle(IntPtr hFile, StringBuilder lpszFilePath, uint cchFilePath, uint dwFlags);
}
'@
    }
    $stream = [IO.File]::Open($Path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)
    try {
        $buffer = New-Object Text.StringBuilder 4096
        $length = [QwenLauncherPathNative]::GetFinalPathNameByHandle($stream.SafeFileHandle.DangerousGetHandle(), $buffer, [uint32]$buffer.Capacity, 0)
        if ($length -eq 0) { throw "Could not resolve final path: $Path" }
        $resolved = $buffer.ToString()
        if ($resolved.StartsWith('\\?\', [StringComparison]::Ordinal)) { $resolved = $resolved.Substring(4) }
        return Full $resolved
    }
    finally { $stream.Dispose() }
}

function Get-Listeners {
    return @(Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue)
}

function Get-VerifiedOwner([object]$Listener) {
    $proc = Get-Process -Id ([int]$Listener.OwningProcess) -ErrorAction Stop
    $path = $proc.Path
    if ([string]::IsNullOrEmpty($path)) { throw "Cannot identify listener PID $($proc.Id)" }
    $final = Get-FinalPath $path
    $expectedFinal = Get-FinalPath $ollamaExe
    if (-not $final.Equals($expectedFinal, [StringComparison]::OrdinalIgnoreCase)) { throw "Port $port is owned by an unexpected executable" }
    $sha = (Get-FileHash -LiteralPath $final -Algorithm SHA256).Hash.ToUpperInvariant()
    if (-not $sha.Equals($expectedSha, [StringComparison]::OrdinalIgnoreCase)) { throw "Port $port owner hash is not the pinned Ollama hash" }
    return [pscustomobject]@{ pid = [int]$proc.Id; final_path = $final; sha256 = $sha; local_address = [string]$Listener.LocalAddress }
}

$result = $null
try {
    if (-not (Test-Path -LiteralPath $ollamaExe -PathType Leaf)) { throw "Pinned Ollama executable is missing: $ollamaExe" }
    if (-not (Test-Path -LiteralPath $modelRoot -PathType Container)) { throw "Model root is missing: $modelRoot" }
    $finalExe = Get-FinalPath $ollamaExe
    if (-not $finalExe.Equals((Full $ollamaExe), [StringComparison]::OrdinalIgnoreCase)) { throw 'Pinned Ollama path resolves through an unexpected reparse path' }
    $sha = (Get-FileHash -LiteralPath $finalExe -Algorithm SHA256).Hash.ToUpperInvariant()
    if (-not $sha.Equals($expectedSha, [StringComparison]::OrdinalIgnoreCase)) { throw "Pinned Ollama SHA mismatch: $sha" }
    [IO.Directory]::CreateDirectory($logRoot) | Out-Null

    $listeners = @(Get-Listeners)
    if ($listeners.Count -gt 0) {
        foreach ($listener in $listeners) {
            if ([string]$listener.LocalAddress -ne '127.0.0.1') { throw "Port $port is listening on an unexpected local address" }
        }
        $owners = @($listeners | ForEach-Object { Get-VerifiedOwner $_ })
        $result = [ordered]@{
            status = 'READY'
            already_running = $true
            pid = [int]$owners[0].pid
            final_path = $owners[0].final_path
            sha256 = $owners[0].sha256
            host = $hostValue
            model_root = $modelRoot
            context_length = [int]$contextLength
            parallel = [int]$parallel
            max_loaded_models = [int]$maxLoaded
            no_cloud = $true
            keep_alive = $keepAlive
            log_root = $logRoot
            hidden = $true
            launcher = (Full $MyInvocation.MyCommand.Path)
        }
    }
    else {
        $stamp = $started.ToUniversalTime().ToString('yyyyMMddTHHmmssfffZ')
        $stdout = Join-Path $logRoot ("ollama-$stamp.out.log")
        $stderr = Join-Path $logRoot ("ollama-$stamp.err.log")
        $env:OLLAMA_HOST = $hostValue
        $env:OLLAMA_MODELS = $modelRoot
        $env:OLLAMA_CONTEXT_LENGTH = $contextLength
        $env:OLLAMA_NUM_PARALLEL = $parallel
        $env:OLLAMA_MAX_LOADED_MODELS = $maxLoaded
        $env:OLLAMA_NO_CLOUD = $noCloud
        $env:OLLAMA_KEEP_ALIVE = $keepAlive
        $child = Start-Process -FilePath $finalExe -ArgumentList @('serve') -WorkingDirectory $ollamaDir -WindowStyle Hidden -RedirectStandardOutput $stdout -RedirectStandardError $stderr -PassThru
        $ready = $false
        for ($i = 0; $i -lt 30; $i++) {
            Start-Sleep -Seconds 1
            $now = @(Get-Listeners)
            if ($now.Count -gt 0) {
                foreach ($listener in $now) {
                    if ([string]$listener.LocalAddress -ne '127.0.0.1') { throw "Started Ollama on an unexpected local address" }
                }
                $owner = Get-VerifiedOwner $now[0]
                if ([int]$owner.pid -ne [int]$child.Id) { throw 'Port owner PID differs from the launched process' }
                $ready = $true
                break
            }
        }
        if (-not $ready) { throw "Ollama did not listen on 127.0.0.1:$port within 30 seconds (PID $($child.Id))" }
        $result = [ordered]@{
            status = 'READY'
            already_running = $false
            pid = [int]$child.Id
            final_path = $finalExe
            sha256 = $sha
            host = $hostValue
            model_root = $modelRoot
            context_length = [int]$contextLength
            parallel = [int]$parallel
            max_loaded_models = [int]$maxLoaded
            no_cloud = $true
            keep_alive = $keepAlive
            log_root = $logRoot
            stdout_log = $stdout
            stderr_log = $stderr
            hidden = $true
            launcher = (Full $MyInvocation.MyCommand.Path)
        }
    }
}
catch {
    $result = [ordered]@{ status = 'BLOCKED_OR_FAILED'; error = $_.Exception.Message; host = $hostValue; port = $port; launcher = (Full $MyInvocation.MyCommand.Path) }
}

$result | ConvertTo-Json -Compress
if ($result.status -ne 'READY') { exit 2 }
exit 0
