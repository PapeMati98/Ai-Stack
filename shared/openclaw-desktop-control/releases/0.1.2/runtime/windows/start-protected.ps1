param(
    [Parameter(Mandatory = $true)][string]$ConfigPath,
    [Parameter(Mandatory = $true)][string]$PythonPath,
    [Parameter(Mandatory = $true)][string]$SecretBlobPath
)
$ErrorActionPreference = 'Stop'
$runFile = Join-Path (Split-Path (Split-Path $PSScriptRoot -Parent) -Parent) 'runtime\run.py'
foreach ($requiredFile in @($ConfigPath, $PythonPath, $SecretBlobPath, $runFile)) {
    if (-not [IO.Path]::IsPathRooted($requiredFile) -or -not (Test-Path -LiteralPath $requiredFile -PathType Leaf)) {
        Write-Output '{"status":"BLOCKED","reason":"required-file-unavailable"}'
        exit 2
    }
}
$secure = (Get-Content -LiteralPath $SecretBlobPath -Raw).Trim() | ConvertTo-SecureString
$pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
try {
    $env:TELEGRAM_BOT_TOKEN = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
} finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
}
try {
    & $PythonPath -X utf8 -B $runFile --config $ConfigPath --serve
    $runtimeExit = $LASTEXITCODE
} finally {
    $env:TELEGRAM_BOT_TOKEN = $null
    $secure.Dispose()
}
exit $runtimeExit
