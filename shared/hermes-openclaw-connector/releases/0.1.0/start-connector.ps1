param(
    [string]$PythonExecutable = (Get-Command python.exe -ErrorAction Stop).Source,
    [string]$TokenFile = (Join-Path $env:USERPROFILE '.agents\secrets\hermes-openclaw-connector.token')
)
$ErrorActionPreference = 'Stop'
$server = Join-Path $PSScriptRoot 'server.py'
foreach ($path in @($PythonExecutable, $TokenFile, $server)) {
    if (-not [IO.Path]::IsPathRooted($path) -or $path -match '["\r\n]' -or
        -not (Test-Path -LiteralPath $path -PathType Leaf)) { throw 'An absolute existing file binding is required' }
}
$start = New-Object System.Diagnostics.ProcessStartInfo
$start.FileName = $PythonExecutable
$start.Arguments = '-B "' + $server + '" --token-file "' + $TokenFile + '" --network wsl-private'
$start.UseShellExecute = $false
$start.CreateNoWindow = $true
$start.RedirectStandardOutput = $true
$start.RedirectStandardError = $true
$process = New-Object System.Diagnostics.Process
$process.StartInfo = $start
if (-not $process.Start()) { throw 'Connector process did not start' }
$stdoutDrain = $process.StandardOutput.BaseStream.CopyToAsync([System.IO.Stream]::Null)
$stderrDrain = $process.StandardError.BaseStream.CopyToAsync([System.IO.Stream]::Null)
$process.WaitForExit()
$stdoutDrain.GetAwaiter().GetResult()
$stderrDrain.GetAwaiter().GetResult()
exit $process.ExitCode
