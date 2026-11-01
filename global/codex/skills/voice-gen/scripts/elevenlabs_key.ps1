param(
  [Parameter(Position = 0)]
  [ValidateSet("set", "status", "clear", "get")]
  [string]$Action = "set",

  [switch]$ForSubprocess
)

$ErrorActionPreference = "Stop"
$secretDir = Join-Path $env:APPDATA "Codex\secrets"
$secretPath = Join-Path $secretDir "elevenlabs_api_key.securestring"

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
  Write-Host "Paste ElevenLabs API key. It will not be displayed." -ForegroundColor Cyan
  $secureKey = Read-Host "ELEVENLABS_API_KEY" -AsSecureString
  if ($secureKey.Length -lt 20) {
    throw "The entered value looks too short for an API key."
  }
  $secureKey | ConvertFrom-SecureString | Set-Content -LiteralPath $secretPath -NoNewline -Encoding UTF8
  Write-Host "Saved ElevenLabs key with Windows DPAPI for the current Windows user." -ForegroundColor Green
  Write-Host "Path: $secretPath"
  Write-Host "Run check: python `"$PSScriptRoot\elevenlabs_voice.py`" check-key"
  exit 0
}

if ($Action -eq "status") {
  if (Test-Path -LiteralPath $secretPath) {
    Write-Host "DPAPI ElevenLabs key is present for this Windows user." -ForegroundColor Green
    Write-Host "Path: $secretPath"
    exit 0
  }
  Write-Host "No DPAPI ElevenLabs key found." -ForegroundColor Yellow
  exit 1
}

if ($Action -eq "clear") {
  if (Test-Path -LiteralPath $secretPath) {
    Remove-Item -LiteralPath $secretPath -Force
    Write-Host "Removed DPAPI ElevenLabs key." -ForegroundColor Green
    exit 0
  }
  Write-Host "No DPAPI ElevenLabs key found." -ForegroundColor Yellow
  exit 0
}

if ($Action -eq "get") {
  if (-not $ForSubprocess) {
    throw "Refusing to print the secret. 'get' is reserved for local subprocess use by voice-gen."
  }
  $plain = Read-PlainSecretFromFile
  if ([string]::IsNullOrWhiteSpace($plain)) {
    exit 1
  }
  [Console]::Out.Write($plain)
  exit 0
}
