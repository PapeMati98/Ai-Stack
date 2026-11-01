param(
  [Parameter(Position = 0)]
  [ValidateSet("set", "status", "clear")]
  [string]$Action = "set"
)

$ErrorActionPreference = "Stop"
$secretDir = Join-Path $env:APPDATA "Codex\secrets"
$secretPath = Join-Path $secretDir "zai_coding_plan_api_key.securestring"
$securityModule = Join-Path $PSHOME "Modules\Microsoft.PowerShell.Security\Microsoft.PowerShell.Security.psd1"
Import-Module -Name $securityModule -ErrorAction Stop

function Test-DpapiSecret {
  if (-not (Test-Path -LiteralPath $secretPath)) {
    return $false
  }
  $encrypted = Get-Content -LiteralPath $secretPath -Raw
  if ([string]::IsNullOrWhiteSpace($encrypted)) {
    return $false
  }
  try {
    $secure = ConvertTo-SecureString $encrypted
    return $secure.Length -ge 20
  } catch {
    return $false
  }
}

if ($Action -eq "set") {
  New-Item -ItemType Directory -Force -Path $secretDir | Out-Null
  Write-Host "Paste the Z.ai Coding Plan API key. Input is hidden." -ForegroundColor Cyan
  Write-Host "The key will be encrypted with Windows DPAPI for the current user." -ForegroundColor DarkCyan
  $secureKey = Read-Host "ZHIPU_API_KEY" -AsSecureString
  if ($secureKey.Length -lt 20) {
    throw "The entered value looks too short for an API key. Nothing was saved."
  }
  $temporaryPath = "$secretPath.tmp-$([Guid]::NewGuid().ToString('N'))"
  try {
    $encrypted = $secureKey | ConvertFrom-SecureString
    [IO.File]::WriteAllText($temporaryPath, $encrypted, [Text.UTF8Encoding]::new($false))
    Move-Item -LiteralPath $temporaryPath -Destination $secretPath -Force
  } finally {
    if (Test-Path -LiteralPath $temporaryPath) {
      Remove-Item -LiteralPath $temporaryPath -Force
    }
  }
  if (-not (Test-DpapiSecret)) {
    throw "DPAPI verification failed after saving the key."
  }
  Write-Host "Saved the Z.ai Coding Plan key with Windows DPAPI." -ForegroundColor Green
  Write-Host "Path: $secretPath"
  Write-Host "No provider request was made and the key was not printed." -ForegroundColor Green
  exit 0
}

if ($Action -eq "status") {
  if (Test-DpapiSecret) {
    Write-Host "DPAPI Z.ai Coding Plan key is present and decryptable for this user." -ForegroundColor Green
    Write-Host "Path: $secretPath"
    exit 0
  }
  Write-Host "No usable DPAPI Z.ai Coding Plan key was found." -ForegroundColor Yellow
  exit 1
}

if ($Action -eq "clear") {
  if (Test-Path -LiteralPath $secretPath) {
    Remove-Item -LiteralPath $secretPath -Force
    Write-Host "Removed the DPAPI Z.ai Coding Plan key." -ForegroundColor Green
  } else {
    Write-Host "No DPAPI Z.ai Coding Plan key was found." -ForegroundColor Yellow
  }
  exit 0
}
