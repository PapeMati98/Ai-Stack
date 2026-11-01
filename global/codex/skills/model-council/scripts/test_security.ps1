$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "security.ps1")

function Assert-True([bool]$Condition, [string]$Message) {
    if (-not $Condition) { throw "ASSERTION_FAILED: $Message" }
}

$neutral = "Authentication uses a secret manager; no credentials are included in this plan."
Assert-True (@(Find-HighConfidenceSecret -Text $neutral).Count -eq 0) "neutral auth/secret description is not a finding"
Assert-True ((Protect-HighConfidenceSecrets -Text $neutral) -eq $neutral) "neutral text remains unchanged"

$vendorToken = "sk-" + "proj-" + (('1' * 30) -join '')
$vendorSecret = "OPENAI_API_KEY=" + $vendorToken
$vendorFinding = @(Find-HighConfidenceSecret -Text $vendorSecret)
Assert-True ($vendorFinding.Count -gt 0) "vendor-prefixed assigned key is detected"
try {
    Assert-NoHighConfidenceSecrets -Plan $vendorSecret -Context ""
    throw "ASSERTION_FAILED: true secret was accepted"
} catch {
    Assert-True ($_.Exception.Message -match "SECRET_PREFLIGHT_BLOCKED") "preflight fails closed"
    Assert-True ($_.Exception.Message -notlike "*$vendorSecret*") "preflight error does not expose the secret"
}

$bearerValue = "abcdefghijklmnopqrstuvwxyz" + "123456"
$genericVendorToken = "sk-" + (('1' * 30) -join '')
$providerResponse = "Use Bearer $bearerValue and $genericVendorToken in the example."
$redacted = Protect-HighConfidenceSecrets -Text $providerResponse
Assert-True ($redacted -notlike "*$bearerValue*") "bearer value is redacted"
Assert-True ($redacted -notlike "*$genericVendorToken*") "vendor token is redacted"
Assert-True ($redacted.Contains("[REDACTED:high-confidence-secret]")) "provider response has a deterministic marker"

$privateKey = ('-----BEGIN ' + 'PRIVATE KEY-----') + "`nSYNTHETIC_NOT_A_KEY`n" + ('-----END ' + 'PRIVATE KEY-----')
$privateRedacted = Protect-HighConfidenceSecrets -Text $privateKey
Assert-True ($privateRedacted -notlike "*BEGIN PRIVATE KEY*") "private key block is redacted"

"security tests passed"
