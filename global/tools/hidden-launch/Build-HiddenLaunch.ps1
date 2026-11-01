[CmdletBinding()]
param([string]$Runtime = (Join-Path $env:USERPROFILE '.agents\runtime\hidden-launch\1.0.0'))
$ErrorActionPreference = 'Stop'
$compiler = Join-Path $env:SystemRoot 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
$source = Join-Path $PSScriptRoot 'HiddenLaunch.cs'
if (-not (Test-Path -LiteralPath $compiler -PathType Leaf)) { throw 'Installed .NET Framework compiler unavailable' }
New-Item -ItemType Directory -Path $Runtime -Force | Out-Null
$output = Join-Path $Runtime 'HiddenLaunch.exe'
if (Test-Path -LiteralPath $output) { throw 'Existing runtime: use a fresh version directory instead of overwriting deployed code' }
$info = [Diagnostics.ProcessStartInfo]::new()
$info.FileName = $compiler
$info.Arguments = '/nologo /target:winexe /out:"' + $output + '" "' + $source + '"'
$info.UseShellExecute = $false
$info.CreateNoWindow = $true
$info.RedirectStandardOutput = $true
$info.RedirectStandardError = $true
$process = [Diagnostics.Process]::Start($info)
$compilerOutput = $process.StandardOutput.ReadToEnd()
$compilerError = $process.StandardError.ReadToEnd()
$process.WaitForExit()
if ($process.ExitCode -ne 0) { throw ('GUI launcher compilation failed: ' + $compilerOutput + $compilerError) }
Copy-Item -LiteralPath $source -Destination (Join-Path $Runtime 'HiddenLaunch.cs')
[pscustomobject]@{Runtime=$Runtime;SourceHash=(Get-FileHash -LiteralPath $source -Algorithm SHA256).Hash;BinaryHash=(Get-FileHash -LiteralPath $output -Algorithm SHA256).Hash} | ConvertTo-Json -Compress
