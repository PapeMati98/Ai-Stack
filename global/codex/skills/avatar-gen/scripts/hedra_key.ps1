param(
  [Parameter(Position = 0)]
  [ValidateSet("set", "status", "clear", "get")]
  [string]$Action = "set",

  [switch]$ForSubprocess
)

$ErrorActionPreference = "Stop"
$secretDir = Join-Path $env:APPDATA "Codex\secrets"
$secretPath = Join-Path $secretDir "hedra_api_key.securestring"
$envName = "HEDRA_API_KEY"

function Test-HasValue {
  param([AllowNull()][string]$Value)
  return -not [string]::IsNullOrWhiteSpace($Value)
}

function Read-PlainSecretFromFile {
  if (-not (Test-Path -LiteralPath $secretPath)) {
    return $null
  }
  $encrypted = Get-Content -LiteralPath $secretPath -Raw
  if ([string]::IsNullOrWhiteSpace($encrypted)) {
    return $null
  }
  $secure = ConvertTo-SecureString $encrypted
  $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
  try {
    return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
  } finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
  }
}

if ($Action -eq "set") {
  New-Item -ItemType Directory -Force -Path $secretDir | Out-Null
  Write-Host "Paste Hedra API key. It will not be displayed." -ForegroundColor Cyan
  $secureKey = Read-Host $envName -AsSecureString
  if ($secureKey.Length -lt 10) {
    throw "The entered value looks too short for an API key."
  }
  $secureKey | ConvertFrom-SecureString | Set-Content -LiteralPath $secretPath -NoNewline -Encoding UTF8
  Write-Host "Saved Hedra key with Windows DPAPI for the current Windows user." -ForegroundColor Green
  Write-Host "Path: $secretPath"
  Write-Host "Run check: python `"$PSScriptRoot\avatar_gen.py`" check-key"
  exit 0
}

if ($Action -eq "status") {
  $processValue = [Environment]::GetEnvironmentVariable($envName, "Process")
  $userValue = [Environment]::GetEnvironmentVariable($envName, "User")
  $machineValue = [Environment]::GetEnvironmentVariable($envName, "Machine")
  $dpapiFile = Test-Path -LiteralPath $secretPath

  Write-Host ("env_current_process: " + ($(if (Test-HasValue $processValue) { "yes" } else { "no" })))
  Write-Host ("env_user: " + ($(if (Test-HasValue $userValue) { "yes" } else { "no" })))
  Write-Host ("env_machine: " + ($(if (Test-HasValue $machineValue) { "yes" } else { "no" })))
  Write-Host ("dpapi_file: " + ($(if ($dpapiFile) { "yes" } else { "no" })))
  if ($dpapiFile) {
    Write-Host "dpapi_path: $secretPath"
  }
  exit 0
}

if ($Action -eq "clear") {
  if (Test-Path -LiteralPath $secretPath) {
    Remove-Item -LiteralPath $secretPath -Force
    Write-Host "Removed DPAPI Hedra key." -ForegroundColor Green
  } else {
    Write-Host "No DPAPI Hedra key found." -ForegroundColor Yellow
  }

  $userValue = [Environment]::GetEnvironmentVariable($envName, "User")
  $machineValue = [Environment]::GetEnvironmentVariable($envName, "Machine")
  if (Test-HasValue $userValue) {
    Write-Host "Warning: User environment variable HEDRA_API_KEY still exists. This script did not remove it." -ForegroundColor Yellow
  }
  if (Test-HasValue $machineValue) {
    Write-Host "Warning: Machine environment variable HEDRA_API_KEY still exists. This script did not remove it." -ForegroundColor Yellow
  }
  exit 0
}

if ($Action -eq "get") {
  if (-not $ForSubprocess) {
    throw "Refusing to print the secret. 'get' is reserved for local subprocess use by avatar-gen."
  }
  $plain = Read-PlainSecretFromFile
  if ([string]::IsNullOrWhiteSpace($plain)) {
    exit 1
  }
  [Console]::Out.Write($plain)
  exit 0
}
