$HighConfidenceSecretPatterns = @(
    [pscustomobject]@{
        Kind = "private-key"
        Pattern = '-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----[\s\S]*?-----END (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----'
    }
    [pscustomobject]@{
        Kind = "bearer-token"
        Pattern = '(?im)\bBearer\s+[A-Za-z0-9._~+/=-]{20,}\b'
    }
    [pscustomobject]@{
        Kind = "vendor-token-prefix"
        Pattern = '(?i)\b(?:sk-(?:proj-)?[A-Za-z0-9_-]{20,}|rk-[A-Za-z0-9_-]{20,}|pk_(?:live|test)_[A-Za-z0-9_-]{16,}|github_pat_[A-Za-z0-9_]{20,}|gh[pousr]_[A-Za-z0-9_]{20,}|xox[baprs]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|ASIA[0-9A-Z]{16})\b'
    }
    [pscustomobject]@{
        Kind = "assigned-key-or-token"
        Pattern = '(?im)\b(?:api[_ -]?key|access[_ -]?token|auth(?:entication)?[_ -]?token|client[_ -]?secret|private[_ -]?key|secret|password|token|key)\b\s*[:=]\s*["'']?[A-Za-z0-9][A-Za-z0-9._~+/=-]{19,}["'']?'
    }
)

function Find-HighConfidenceSecret {
    param([AllowNull()][string]$Text)

    if ([string]::IsNullOrEmpty($Text)) { return @() }
    $matches = @()
    foreach ($rule in $HighConfidenceSecretPatterns) {
        foreach ($match in [regex]::Matches($Text, $rule.Pattern)) {
            $matches += [pscustomobject]@{
                Kind = $rule.Kind
                Index = $match.Index
                Length = $match.Length
            }
        }
    }
    return $matches | Sort-Object Index, Kind -Unique
}

function Assert-NoHighConfidenceSecrets {
    param(
        [AllowNull()][string]$Plan,
        [AllowNull()][string]$Context
    )

    $locations = @(
        [pscustomobject]@{ Name = "Plan"; Text = $Plan }
        [pscustomobject]@{ Name = "Context"; Text = $Context }
    )
    foreach ($location in $locations) {
        $findings = @(Find-HighConfidenceSecret -Text $location.Text)
        if ($findings.Count -gt 0) {
            $kinds = ($findings | Select-Object -ExpandProperty Kind -Unique) -join ","
            throw "SECRET_PREFLIGHT_BLOCKED: high-confidence secret in $($location.Name) (kind=$kinds)"
        }
    }
}

function Protect-HighConfidenceSecrets {
    param([AllowNull()][string]$Text)

    if ([string]::IsNullOrEmpty($Text)) { return $Text }
    $protected = $Text
    foreach ($rule in $HighConfidenceSecretPatterns) {
        $protected = [regex]::Replace($protected, $rule.Pattern, "[REDACTED:high-confidence-secret]")
    }
    return $protected
}
