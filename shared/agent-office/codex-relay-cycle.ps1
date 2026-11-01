# Transport plumbing only; all Office and app-tool decisions belong to Python.
$ErrorActionPreference = 'Stop'
# This is the invisible tool-owned ConPTY only. Never echo raw app transcripts.
if ($null -eq ('OfficeRelayConsoleMode' -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class OfficeRelayConsoleMode {
 [DllImport("kernel32.dll")] public static extern IntPtr GetStdHandle(int n);
 [DllImport("kernel32.dll")] public static extern bool GetConsoleMode(IntPtr h, out uint mode);
 [DllImport("kernel32.dll")] public static extern bool SetConsoleMode(IntPtr h, uint mode);
}
'@
}
$inputHandle = [OfficeRelayConsoleMode]::GetStdHandle(-10)
[uint32]$originalInputMode = 0
$restoreInputMode = [OfficeRelayConsoleMode]::GetConsoleMode($inputHandle, [ref]$originalInputMode)
if ($restoreInputMode -and ![OfficeRelayConsoleMode]::SetConsoleMode($inputHandle, ($originalInputMode -band (-bnot 6)))) {
    [Console]::Out.WriteLine('{"action":"done","status":"unknown","reason":"stdin_echo_disable_failed"}')
    exit 0
}
$python = (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python313\python.exe')
$script = Join-Path $PSScriptRoot 'coordfs\codex_relay_cycle.py'
if (!(Test-Path -LiteralPath $script -PathType Leaf)) {
    [Console]::Out.WriteLine('{"action":"done","status":"no_work","reason":"deterministic_relay_not_installed"}')
    exit 0
}
$start = [Diagnostics.ProcessStartInfo]::new()
$start.FileName = $python
$start.Arguments = '-I -B -X utf8 "' + $script + '"'
$start.WorkingDirectory = $PSScriptRoot
$start.UseShellExecute = $false
$start.CreateNoWindow = $true
$start.RedirectStandardInput = $true
$start.RedirectStandardOutput = $true
$start.RedirectStandardError = $true
$start.StandardInputEncoding = [Text.UTF8Encoding]::new($false)
$start.StandardOutputEncoding = [Text.UTF8Encoding]::new($false)
$start.StandardErrorEncoding = [Text.UTF8Encoding]::new($false)
$clock = [Diagnostics.Stopwatch]::StartNew()
$process = [Diagnostics.Process]::Start($start)
$errors = $process.StandardError.ReadToEndAsync()
$incoming = [IO.StreamReader]::new([Console]::OpenStandardInput(), [Text.UTF8Encoding]::new($false))
$terminal = $false
function Write-RelayFrame([string]$value) {
    # ASCII lines remain below the 80-column ConPTY width. Cursor wrapping must
    # never change exact JSON/tool arguments; input and output are not echoed.
    $hex = [BitConverter]::ToString([Text.Encoding]::UTF8.GetBytes($value)).Replace('-', '').ToLowerInvariant()
    for ($i=0; $i -lt $hex.Length; $i+=64) {
        [Console]::Out.WriteLine('@O:' + $hex.Substring($i, [Math]::Min(64, $hex.Length-$i)))
    }
    [Console]::Out.WriteLine('@E')
    [Console]::Out.Flush()
}
try {
    while ($clock.ElapsedMilliseconds -lt 240000) {
        $line = $process.StandardOutput.ReadLineAsync()
        $remaining = [int][Math]::Max(1, 240000 - $clock.ElapsedMilliseconds)
        if (!$line.Wait($remaining)) { throw 'cycle deadline' }
        $value = $line.Result
        if ($null -eq $value) { break }
        Write-RelayFrame $value
        $request = ConvertFrom-Json -InputObject $value
        if ($request.action -eq 'done') { $terminal = $true; break }
        if ($request.action -ne 'call') { throw 'invalid cycle action' }
        # Console.In is a synchronized reader and can block before returning its
        # Task on .NET Framework. Read the redirected pipe through StreamReader.
        do {
            $inputLine = $incoming.ReadLineAsync()
            $remaining = [int][Math]::Max(1, 240000 - $clock.ElapsedMilliseconds)
            if (!$inputLine.Wait($remaining)) { throw 'cycle input deadline' }
            if ($null -eq $inputLine.Result) { throw 'cycle input closed' }
            $frame = ConvertFrom-Json -InputObject $inputLine.Result
            $process.StandardInput.WriteLine($inputLine.Result)
            $process.StandardInput.Flush()
            $more = ($null -ne $frame.part -and $frame.final -eq $false)
        } while ($more)
    }
    if (!$terminal) {
        Write-RelayFrame '{"action":"done","status":"unknown","reason":"cycle_process_ended"}'
    }
} catch {
    # Never print stderr, raw app results, paths, Office bodies or connection data.
    Write-RelayFrame '{"action":"done","status":"unknown","reason":"cycle_transport_ended"}'
} finally {
    $process.StandardInput.Close()
    if (!$process.HasExited) {
        $remaining = [int][Math]::Max(0, 240000 - $clock.ElapsedMilliseconds)
        if (!$process.WaitForExit($remaining)) { $process.Kill() }
    }
    $process.Dispose()
    if ($restoreInputMode) { [void][OfficeRelayConsoleMode]::SetConsoleMode($inputHandle, $originalInputMode) }
}
