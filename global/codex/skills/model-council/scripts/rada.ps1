param(
    [string]$Plan,
    [string]$PlanFile,
    [string]$Context = "",
    [string[]]$Models,
    [switch]$Quick,
    [switch]$Full,
    [switch]$Brainstorm,
    [switch]$IncludeFugu,
    [switch]$NoChair,
    [switch]$ResolveOnly,
    [switch]$RefreshRoster,
    [int]$MaxTokens = 5000,
    [ValidateSet("low", "medium", "high")]
    [string]$Effort = "medium",
    [int]$Throttle = 4,
    [string]$OutDir,
    [string]$Referer = "https://example.invalid/ai-stack",
    [string]$Title = "AI Stack - Model Council"
)

$ErrorActionPreference = "Stop"
try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 } catch {}

if (($Quick -and $Full) -or ($Brainstorm -and ($Quick -or $Full))) { throw "Wybierz tylko jeden tryb: -Quick, -Full albo -Brainstorm." }
if ($MaxTokens -lt 2048) { throw "-MaxTokens musi wynosic co najmniej 2048, bo reasoning zuzywa ten sam budzet co odpowiedz." }
if ($Throttle -lt 1) { throw "-Throttle musi wynosic co najmniej 1." }

$skillRoot = Split-Path -Parent $PSScriptRoot
$policyPath = Join-Path $skillRoot "references\roster-policy.json"
$resolverPath = Join-Path $PSScriptRoot "resolve_roster.py"
$securityPath = Join-Path $PSScriptRoot "security.ps1"
$brainstormTransportPath = Join-Path $PSScriptRoot "brainstorm_transport.ps1"
$promptBuilderPath = Join-Path $PSScriptRoot "prompt_builder.ps1"
$stateRoot = Join-Path $env:USERPROFILE ".agents\model-council"

foreach ($requiredPath in @($policyPath, $resolverPath, $securityPath, $brainstormTransportPath, $promptBuilderPath)) {
    if (-not (Test-Path -LiteralPath $requiredPath)) {
        throw "Brak wymaganego pliku skilla: $requiredPath"
    }
}
. $securityPath
. $brainstormTransportPath
. $promptBuilderPath

function Get-PublicPanelCredential {
    $value = [Environment]::GetEnvironmentVariable('OPENROUTER_API_KEY', 'Process')
    if ([string]::IsNullOrWhiteSpace($value)) {
        throw 'Public credential is unavailable.'
    }
    return [string]$value
}

function Resolve-FleetSuiteRoot {
    # Jawna zmienna wygrywa. Bez niej: najnowszy zainstalowany runtime z markerem
    # instalatora i skryptem Model Radar. Dawny fallback `...\current` nigdy nie
    # istnial (instalator tworzy tylko lean-<wersja>), wiec po usunieciu zmiennej
    # 2026-10-12 preflight konczyl sie MODEL_RADAR_MISSING.
    param(
        [string]$EnvRoot = $env:AGENT_FLEET_SUITE_ROOT,
        [string]$RuntimeParent = (Join-Path $env:USERPROFILE ".agents\runtime\agent-fleet-suite")
    )
    if (-not [string]::IsNullOrWhiteSpace($EnvRoot)) { return $EnvRoot }
    $best = $null
    $bestVersion = $null
    if (Test-Path -LiteralPath $RuntimeParent -PathType Container) {
        foreach ($dir in Get-ChildItem -LiteralPath $RuntimeParent -Directory) {
            $marker = Join-Path $dir.FullName ".agent-fleet-suite-runtime.json"
            $radar = Join-Path $dir.FullName "model-radar\scripts\model_radar.py"
            if (-not (Test-Path -LiteralPath $marker -PathType Leaf) -or
                -not (Test-Path -LiteralPath $radar -PathType Leaf)) { continue }
            try {
                $version = [version]((Get-Content -LiteralPath $marker -Raw | ConvertFrom-Json).package_version)
            } catch { continue }
            if ($null -eq $bestVersion -or $version -gt $bestVersion) {
                $best = $dir.FullName
                $bestVersion = $version
            }
        }
    }
    if ($null -eq $best) { return (Join-Path $RuntimeParent "<brak zainstalowanego runtime>") }
    return $best
}

function Invoke-ModelRadarPreflight {
    $suite = Resolve-FleetSuiteRoot
    $radarPath = Join-Path $suite "model-radar\scripts\model_radar.py"
    if (-not (Test-Path -LiteralPath $radarPath)) {
        throw "MODEL_RADAR_MISSING: $radarPath"
    }

    $raw = @(& python $radarPath check 2>&1)
    if ($LASTEXITCODE -ne 0) {
        throw ("MODEL_RADAR_INVALID: " + ($raw -join " "))
    }
    try {
        $status = (($raw -join "`n") | ConvertFrom-Json)
    } catch {
        throw "MODEL_RADAR_INVALID: odpowiedz check nie jest poprawnym JSON."
    }
    if (-not $status.fresh -or -not $status.routing_allowed) {
        throw "MODEL_RADAR_STALE: odswiez rynek procedura skilla model-radar i uruchom rade ponownie."
    }
    return [pscustomobject]@{
        status = $status.status
        radar_version = $status.radar_version
        last_verified_at = $status.last_verified_at
        valid_until = $status.valid_until
        sha256 = $status.sha256
    }
}

function Invoke-RosterResolver {
    param(
        [string]$Mode,
        [string[]]$CustomModels,
        [switch]$ForceRefresh,
        [switch]$ExcludeChair,
        [switch]$AddFugu
    )

    $arguments = @(
        "resolve",
        "--policy", $policyPath,
        "--state-root", $stateRoot,
        "--mode", $Mode
    )
    if ($ForceRefresh) { $arguments += "--refresh-catalog" }
    if ($CustomModels) {
        foreach ($modelId in $CustomModels) {
            $arguments += @("--custom-model", $modelId)
        }
        if (-not $ExcludeChair) { $arguments += @("--include-role", "chair") }
    } elseif ($ExcludeChair) {
        $arguments += @("--exclude-role", "chair")
    }
    if ($AddFugu) { $arguments += @("--include-role", "arbiter") }

    $raw = @(& python $resolverPath @arguments 2>&1)
    if ($LASTEXITCODE -ne 0) {
        throw ("ROSTER_RESOLUTION_FAILED: " + ($raw -join " "))
    }
    try {
        return (($raw -join "`n") | ConvertFrom-Json)
    } catch {
        throw "ROSTER_RESOLUTION_FAILED: resolver nie zwrocil poprawnego JSON."
    }
}

function Record-ModelHealth {
    param(
        [string]$Model,
        [bool]$Success,
        [string]$FailureKind = "unknown"
    )
    $arguments = @(
        "record",
        "--policy", $policyPath,
        "--state-root", $stateRoot,
        "--model", $Model
    )
    if ($Success) {
        $arguments += "--success"
    } else {
        $arguments += @("--failure", "--failure-kind", $FailureKind)
    }
    $previousErrorAction = $ErrorActionPreference
    try {
        $ErrorActionPreference = "SilentlyContinue"
        & python $resolverPath @arguments 2>$null | Out-Null
        $recordExitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousErrorAction
    }
    if ($recordExitCode -ne 0) {
        Write-Warning "Nie udalo sie zapisac stanu circuit breakera dla $Model."
    }
}

if ($PlanFile) { $Plan = Get-Content -Raw -Encoding UTF8 -LiteralPath $PlanFile }
if (-not $ResolveOnly) {
    if ([string]::IsNullOrWhiteSpace($Plan)) {
        throw "Podaj -Plan '<tekst>' albo -PlanFile <sciezka>."
    }
    Assert-NoHighConfidenceSecrets -Plan $Plan -Context $Context
}

$apiKey = (
    Get-PublicPanelCredential
)
$processKeyBefore = [Environment]::GetEnvironmentVariable("OPENROUTER_API_KEY", "Process")
$restoreProcessKey = [string]::IsNullOrWhiteSpace($processKeyBefore)
if ($restoreProcessKey) { $env:OPENROUTER_API_KEY = $apiKey }

try {
    $radar = Invoke-ModelRadarPreflight
    $mode = if ($Full) { "full" } elseif ($Quick) { "quick" } else { "default" }
    $roster = Invoke-RosterResolver -Mode $mode -CustomModels $Models -ForceRefresh:$RefreshRoster -ExcludeChair:$NoChair -AddFugu:$IncludeFugu
} finally {
    if ($restoreProcessKey) { Remove-Item Env:OPENROUTER_API_KEY -ErrorAction SilentlyContinue }
}

$panel = @($roster.panel)
$maxOutputCostUsd = 0.0
foreach ($member in $panel) {
    $completionPrice = 0.0
    if ([double]::TryParse(
        [string]$member.pricing_per_token_usd.completion,
        [Globalization.NumberStyles]::Float,
        [Globalization.CultureInfo]::InvariantCulture,
        [ref]$completionPrice
    )) {
        $maxOutputCostUsd += ($completionPrice * $MaxTokens)
    }
}
Write-Host ("[roster] policy={0}, catalog={1}, mode={2}, models={3}" -f $roster.policy_version, $roster.catalog.fetched_at, $roster.mode, $panel.Count) -ForegroundColor Cyan
foreach ($member in $panel) {
    Write-Host ("  - {0}: {1} ({2} endpoints)" -f $member.role, $member.model, $member.endpoint_count) -ForegroundColor DarkCyan
}
foreach ($skipped in @($roster.skipped)) {
    Write-Warning ("Pominieta rola {0}: {1}" -f $skipped.role, $skipped.reason)
}
foreach ($warning in @($roster.warnings)) { Write-Warning $warning }

if ($ResolveOnly) {
    [pscustomobject]@{
        status = "READY"
        inference_calls = 0
        max_output_cost_usd = [Math]::Round($maxOutputCostUsd, 6)
        model_radar = $radar
        roster = $roster
    } | ConvertTo-Json -Depth 12
    return
}

$runMode = if ($Brainstorm) { "brainstorm" } else { $roster.mode }

$SYS = @"
Jestes adwersaryjnym recenzentem planu wykonania zadania. Nie chwal planu ani go nie przepisuj. Znajdz realne ryzyka, luki, zla sekwencje i prostsze podejscie, zanim plan trafi do wykonania.
Zasady twarde:
- Krytykuj plan, nie autora. Atakuj zalozenia i decyzje.
- Podawaj konkret: co jest zle, dlaczego boli i jak sie objawi.
- Nie wymyslaj wymagan spoza kontekstu. Braki oznacz jako pytanie lub zalozenie.
- Oznacz pewnosc: [pewne], [prawdopodobne] albo [przypuszczenie].
- Preferuj najprostsze rozwiazanie spelniajace cel.
- Bez komplementow, ogolnikow i powtarzania planu.
Patrz przede wszystkim przez przypisana soczewke.
"@

function Invoke-Batch {
    param([array]$Entries)
    $pool = [runspacefactory]::CreateRunspacePool(1, [Math]::Max(1, $Throttle))
    $pool.Open()
    $jobs = @()
    foreach ($entry in $Entries) {
        $bodyObj = @{
            messages = @(
                @{ role="system"; content=$entry.system }
                @{ role="user"; content=$entry.user }
            )
            max_tokens = $MaxTokens
            usage = @{ include = $true }
            model = $entry.member.model
        }
        if ($entry.member.supports_reasoning) { $bodyObj.reasoning = @{ effort = $Effort } }
        $bodyJson = $bodyObj | ConvertTo-Json -Depth 8
        $ps = [powershell]::Create()
        $ps.RunspacePool = $pool
        [void]$ps.AddScript($worker).AddArgument("https://openrouter.ai/api/v1/chat/completions").AddArgument($headers).AddArgument($bodyJson)
        $jobs += [pscustomobject]@{ ps=$ps; handle=$ps.BeginInvoke(); entry=$entry }
    }
    $results = @()
    foreach ($job in $jobs) {
        try { $out = $job.ps.EndInvoke($job.handle) } catch { $out = @(@{ ok=$false; err=$_.Exception.Message; error_kind="unknown" }) }
        $result = @($out)[-1]
        if ($result.ok) { $result.content = Protect-HighConfidenceSecrets -Text ([string]$result.content) }
        $job.ps.Dispose()
        $results += [pscustomobject]@{ entry=$job.entry; response=$result }
    }
    $pool.Close(); $pool.Dispose()
    return $results
}

function Save-Batch-Evidence {
    param([array]$BatchResults, [string]$RoundName)
    $saved = @()
    foreach ($row in $BatchResults) {
        $member = $row.entry.member
        $result = $row.response
        $safe = (($member.role + "_" + $member.model) -replace '[^\w\.\-]', '_')
        $file = Join-Path $OutDir ("{0}_{1}.md" -f $RoundName, $safe)
        $healthDisposition = Get-BrainstormHealthDisposition `
            -Success ([bool]$result.ok) `
            -ErrorKind ([string]$result.error_kind) `
            -OutputTokens ([int]$result.out_tokens) `
            -RequestedMaxTokens $MaxTokens
        if ($result.ok) {
            $actual = if ($result.actual_model) { [string]$result.actual_model } else { [string]$member.model }
            Set-Content -LiteralPath $file -Value ("# {0} ({1})`r`n## Runda: {2}`r`n## Faktyczny model: {3}`r`n## Soczewka: {4}`r`n`r`n{5}" -f $member.model_name, $member.model, $RoundName, $actual, $member.label, $result.content) -Encoding UTF8
            Record-ModelHealth -Model $member.model -Success $true
        } elseif ($healthDisposition -eq "ignore-budget-exhausted") {
            $result.error_kind = "budget_exhausted"
        } else {
            Record-ModelHealth -Model $member.model -Success $false -FailureKind $result.error_kind
        }
        $saved += [pscustomobject]@{
            round=$RoundName; role=$member.role; model=$member.model; faktyczny_model=$result.actual_model
            nazwa=$member.model_name; soczewka=$member.label; ok=[bool]$result.ok; blad=$result.err
            error_kind=$result.error_kind; status_code=$result.status_code; attempts=$result.attempts
            health_disposition=$healthDisposition
            input_tokens=$result.in_tokens; output_tokens=$result.out_tokens; total_tokens=$result.total_tokens
            cost=$result.cost; plik=$file
        }
    }
    return $saved
}

function Invoke-BrainstormProtocol {
    $script:brainstormFailure = $null
    $script:brainstormFailureKind = $null
    $script:brainstormAttemptedRound2 = 0
    $script:brainstormPartialResults = @()
    $minimum = switch ($roster.mode) { "quick" { 3 } "default" { 4 } "full" { 6 } default { 1 } }
    $round1Entries = @($panel | ForEach-Object {
        [pscustomobject]@{ member=$_; system=$SYS; user=(Build-CouncilBrainstormRoundOneUser -LensName $_.label -LensDescription $_.description -ReviewPlan $Plan -ReviewContext $Context) }
    })
    $round1 = @(Invoke-Batch $round1Entries)
    $round1Evidence = @(Save-Batch-Evidence $round1 "round1")
    $script:brainstormPartialResults = @($round1Evidence)
    $healthy = @($round1Evidence | Where-Object { $_.ok })
    if ($healthy.Count -lt $minimum) {
        $script:brainstormFailure = ("BRAINSTORM_INSUFFICIENT_ROUND1: {0}/{1}; minimum={2}; round2=NOT_STARTED" -f $healthy.Count, $panel.Count, $minimum)
        $script:brainstormFailureKind = "insufficient_responses"
        return $round1Evidence
    }

    $inputPath = Join-Path $OutDir "_round1-for-anonymizer.json"
    $bundlePath = Join-Path $OutDir "_anonymous-bundle.json"
    $redactions = @($healthy | ForEach-Object { $_.model; $_.faktyczny_model; $_.nazwa; $_.role; $_.soczewka } | Where-Object { -not [string]::IsNullOrWhiteSpace([string]$_) } | Select-Object -Unique)
    Write-BrainstormAnonymizerInput -Evidence $healthy -Path $inputPath
    Invoke-BrainstormAnonymizer `
        -PythonCommand "python" `
        -ScriptPath (Join-Path $PSScriptRoot "brainstorm_protocol.py") `
        -InputPath $inputPath `
        -OutputPath $bundlePath `
        -Redactions $redactions
    $bundle = (Get-Content -Raw -LiteralPath $bundlePath | ConvertFrom-Json).bundle
    if ([string]::IsNullOrWhiteSpace($bundle)) { throw "BRAINSTORM_EMPTY_ANONYMOUS_BUNDLE" }

    $round2System = @"
Jestes uczestnikiem drugiej rundy brainstormingu. Otrzymujesz anonimowy bundle pomyslow bez nazw i modeli.
Krytykuj argumenty, nie autorow. Wskaz najmocniejsze elementy, bledy, spory, integracje i testy rozstrzygajace.
Nie glosuj i nie wydawaj decyzji produkcyjnej. Zachowaj rozne hipotezy, jesli nie ma dowodu rozstrzygajacego.
"@
    $round2Candidates = @(Get-BrainstormRoundTwoCandidates -HealthyEvidence $healthy)
    $round2Entries = @($round2Candidates | ForEach-Object {
        $user = @"
# ZADANIE / PROBLEM
$Plan

# KONTEKST
$Context

# ANONIMOWY BUNDLE POMYSLOW
$bundle

# TWOJA SOCZEWKA
$($_.soczewka)

# POLECENIE: RUNDA 2
Ocen kazdy istotny kierunek. Wypisz: najmocniejsze elementy, krytyke i ryzyka,
co da sie zintegrowac, jawne spory oraz minimalne testy rozstrzygajace dla sporow.
Nie ujawniaj ani nie probuj odtwarzac nazw/modeli.
"@
        [pscustomobject]@{ member=$null; system=$round2System; user=$user }
    })
    for ($i=0; $i -lt $round2Candidates.Count; $i++) {
        $member = $panel | Where-Object { $_.model -eq $round2Candidates[$i].model } | Select-Object -First 1
        $round2Entries[$i].member = $member
    }
    $script:brainstormAttemptedRound2 = $round2Candidates.Count
    $round2 = @(Invoke-Batch $round2Entries)
    $round2Evidence = @(Save-Batch-Evidence $round2 "round2")
    $script:brainstormPartialResults = @($round1Evidence + $round2Evidence)
    $round2Ok = @($round2Evidence | Where-Object { $_.ok }).Count
    if ($round2Ok -lt $minimum) {
        $script:brainstormFailure = ("BRAINSTORM_INSUFFICIENT_ROUND2: {0}/{1}; minimum={2}; no fallback" -f $round2Ok, $healthy.Count, $minimum)
        $script:brainstormFailureKind = "insufficient_responses"
    }
    return @($round1Evidence + $round2Evidence)
}

if (-not $OutDir) {
    $OutDir = Join-Path ([IO.Path]::GetTempPath()) ("rada_" + [Guid]::NewGuid().ToString("N").Substring(0, 8))
}
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$headers = @{
    "Authorization" = [string]::Concat('Bearer',' ',$apiKey)
    "HTTP-Referer" = $Referer
    "X-Title" = $Title
}

$worker = {
    param($uri, $headers, $bodyJson)
    $attempt = 0
    $lastError = "unknown error"
    $lastCode = $null
    while ($attempt -lt 3) {
        $attempt++
        try {
            $bytes = [Text.Encoding]::UTF8.GetBytes($bodyJson)
            $wr = Invoke-WebRequest -Method Post -Uri $uri -Headers $headers -ContentType "application/json; charset=utf-8" -Body $bytes -TimeoutSec 300 -UseBasicParsing
            $json = [Text.Encoding]::UTF8.GetString($wr.RawContentStream.ToArray())
            $resp = $json | ConvertFrom-Json
            if ($resp.error) {
                return @{ ok=$false; err=("API error: " + [string]$resp.error.message); error_kind="hard"; status_code=$null }
            }
            if (-not $resp.choices -or @($resp.choices).Count -eq 0) {
                throw "brak choices w odpowiedzi providera"
            }
            $content = [string]$resp.choices[0].message.content
            if ([string]::IsNullOrWhiteSpace($content)) {
                return @{
                    ok=$false
                    err="pusta tresc (budzet odpowiedzi mogl zostac zuzyty przez reasoning)"
                    error_kind="empty"
                    status_code=200
                    in_tokens=$resp.usage.prompt_tokens
                    out_tokens=$resp.usage.completion_tokens
                    total_tokens=$resp.usage.total_tokens
                    cost=$resp.usage.cost
                    actual_model=$resp.model
                    attempts=$attempt
                }
            }
            return @{
                ok=$true
                content=$content
                in_tokens=$resp.usage.prompt_tokens
                out_tokens=$resp.usage.completion_tokens
                total_tokens=$resp.usage.total_tokens
                cost=$resp.usage.cost
                actual_model=$resp.model
                attempts=$attempt
            }
        } catch {
            $code = $null
            try { $code = [int]$_.Exception.Response.StatusCode.value__ } catch {}
            $lastCode = $code
            $lastError = $_.Exception.Message
            $transient = ($null -eq $code -or $code -eq 408 -or $code -eq 409 -or $code -eq 425 -or $code -eq 429 -or $code -ge 500)
            if (-not $transient) {
                return @{ ok=$false; err=$lastError; error_kind="hard"; status_code=$code; attempts=$attempt }
            }
            if ($attempt -lt 3) {
                Start-Sleep -Seconds ([Math]::Min(8, [Math]::Pow(2, $attempt)))
            }
        }
    }
    return @{ ok=$false; err=$lastError; error_kind="transient"; status_code=$lastCode; attempts=$attempt }
}

if ($Brainstorm) {
    $brainstormResults = @()
    try {
        $brainstormResults = @(Invoke-BrainstormProtocol)
    } catch {
        $script:brainstormFailure = $_.Exception.Message
        if ([string]::IsNullOrWhiteSpace($script:brainstormFailureKind)) {
            $script:brainstormFailureKind = "internal_error"
        }
        $brainstormResults = @($script:brainstormPartialResults)
    }
    $brainstormIndexPath = Join-Path $OutDir "_index.json"
    $brainstormManifestPath = Join-Path $OutDir "_manifest.json"
    $brainstormResults | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $brainstormIndexPath -Encoding UTF8
    $round1Results = @($brainstormResults | Where-Object { $_.round -eq "round1" })
    $round2Results = @($brainstormResults | Where-Object { $_.round -eq "round2" })
    $brainstormStatus = Get-BrainstormRunStatus -Failure $brainstormFailure -FailureKind $brainstormFailureKind
    [pscustomobject]@{
        schema_version = 2
        mode = "brainstorm"
        status = $brainstormStatus
        created_at = (Get-Date).ToUniversalTime().ToString("o")
        model_radar = $radar
        roster = $roster
        rounds = @(
            [pscustomobject]@{ name="round1"; purpose="independent_divergence"; results=$round1Results; evidence_files=@($round1Results.plik) }
            [pscustomobject]@{ name="round2"; purpose="anonymous_critique_integration"; results=$round2Results; evidence_files=@($round2Results.plik); anonymous_bundle=(Join-Path $OutDir "_anonymous-bundle.json") }
        )
        requested_max_tokens = $MaxTokens
        requested_effort = $Effort
        max_output_cost_usd = [Math]::Round(($maxOutputCostUsd * 2), 6)
        attempted_models_round1 = $panel.Count
        attempted_models_round2 = $script:brainstormAttemptedRound2
        successful_models_round1 = (@($round1Results | Where-Object { $_.ok })).Count
        successful_models_round2 = (@($round2Results | Where-Object { $_.ok })).Count
        total_input_tokens = ($brainstormResults | Measure-Object -Property input_tokens -Sum).Sum
        total_output_tokens = ($brainstormResults | Measure-Object -Property output_tokens -Sum).Sum
        reported_cost = ($brainstormResults | Measure-Object -Property cost -Sum).Sum
        no_fallback = $true
        parent_is_synthesizer = $true
        failure_kind = $brainstormFailureKind
        failure = $brainstormFailure
    } | ConvertTo-Json -Depth 15 | Set-Content -LiteralPath $brainstormManifestPath -Encoding UTF8
    "=== RADA MODELI BRAINSTORM: {0} odpowiedzi w dwoch rundach ===" -f $brainstormResults.Count
    "INDEX:    $brainstormIndexPath"
    "MANIFEST: $brainstormManifestPath"
    if ($brainstormFailure) { throw $brainstormFailure }
    return
}

$pool = [runspacefactory]::CreateRunspacePool(1, [Math]::Max(1, $Throttle))
$pool.Open()
$jobs = @()
$index = 0
foreach ($member in $panel) {
    $index++
    $bodyObj = @{
        messages = @(
            @{ role="system"; content=$SYS },
            @{ role="user"; content=(Build-CouncilReviewUser -LensName $member.label -LensDescription $member.description -ReviewPlan $Plan -ReviewContext $Context) }
        )
        max_tokens = $MaxTokens
        usage = @{ include = $true }
        model = $member.model
    }
    if ($member.supports_reasoning) {
        $bodyObj.reasoning = @{ effort = $Effort }
    }
    $bodyJson = $bodyObj | ConvertTo-Json -Depth 8
    Write-Host ("[start {0}/{1}] {2} -> {3}" -f $index, $panel.Count, $member.model_name, $member.label) -ForegroundColor Cyan
    $ps = [powershell]::Create()
    $ps.RunspacePool = $pool
    [void]$ps.AddScript($worker).AddArgument("https://openrouter.ai/api/v1/chat/completions").AddArgument($headers).AddArgument($bodyJson)
    $jobs += [pscustomobject]@{
        ps = $ps
        handle = $ps.BeginInvoke()
        member = $member
    }
}

$results = @()
foreach ($job in $jobs) {
    $out = $null
    try {
        $out = $job.ps.EndInvoke($job.handle)
    } catch {
        $out = @(@{ ok=$false; err=$_.Exception.Message; error_kind="unknown" })
    }
    $result = @($out)[-1]
    if ($result.ok) { $result.content = Protect-HighConfidenceSecrets -Text ([string]$result.content) }
    $job.ps.Dispose()
    $member = $job.member
    $safe = (($member.role + "_" + $member.model) -replace '[^\w\.\-]', '_')
    $file = Join-Path $OutDir "$safe.md"

    if ($result.ok) {
        $actual = if ($result.actual_model) { [string]$result.actual_model } else { [string]$member.model }
        Set-Content -LiteralPath $file -Value ("# {0} ({1})`r`n## Faktyczny model: {2}`r`n## Soczewka: {3}`r`n`r`n{4}" -f $member.model_name, $member.model, $actual, $member.label, $result.content) -Encoding UTF8
        Write-Host ("    OK  {0} (out_tok={1}, model={2})" -f $member.model_name, $result.out_tokens, $actual) -ForegroundColor Green
        Record-ModelHealth -Model $member.model -Success $true
    } else {
        Write-Host ("    BLAD {0}: {1}" -f $member.model_name, $result.err) -ForegroundColor Red
        Record-ModelHealth -Model $member.model -Success $false -FailureKind $result.error_kind
    }

    $results += [pscustomobject]@{
        role = $member.role
        model = $member.model
        faktyczny_model = $result.actual_model
        nazwa = $member.model_name
        soczewka = $member.label
        ok = [bool]$result.ok
        blad = $result.err
        error_kind = $result.error_kind
        status_code = $result.status_code
        attempts = $result.attempts
        input_tokens = $result.in_tokens
        output_tokens = $result.out_tokens
        total_tokens = $result.total_tokens
        cost = $result.cost
        plik = $file
    }
}
$pool.Close()
$pool.Dispose()

$okCount = @($results | Where-Object { $_.ok }).Count
$minimumSuccessful = switch ($roster.mode) {
    "quick" { 3 }
    "default" { 4 }
    "full" { 6 }
    default { 1 }
}
$runStatus = if ($okCount -ge $minimumSuccessful) { "SUCCESS" } else { "INSUFFICIENT_RESPONSES" }
$indexPath = Join-Path $OutDir "_index.json"
$manifestPath = Join-Path $OutDir "_manifest.json"
$results | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $indexPath -Encoding UTF8
[pscustomobject]@{
    schema_version = 1
    status = $runStatus
    created_at = (Get-Date).ToUniversalTime().ToString("o")
    model_radar = $radar
    roster = $roster
    requested_max_tokens = $MaxTokens
    requested_effort = $Effort
    max_output_cost_usd = [Math]::Round($maxOutputCostUsd, 6)
    successful_models = $okCount
    minimum_successful_models = $minimumSuccessful
    attempted_models = $panel.Count
    total_input_tokens = ($results | Measure-Object -Property input_tokens -Sum).Sum
    total_output_tokens = ($results | Measure-Object -Property output_tokens -Sum).Sum
    reported_cost = ($results | Measure-Object -Property cost -Sum).Sum
} | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $manifestPath -Encoding UTF8

""
"=== RADA MODELI: {0}/{1} odpowiedzialo ===" -f $okCount, $panel.Count
$results | Format-Table nazwa, soczewka, ok, output_tokens, faktyczny_model -AutoSize
"KRYTYKI:  $OutDir"
"INDEX:    $indexPath"
"MANIFEST: $manifestPath"
if ($okCount -lt $minimumSuccessful) {
    throw ("RADA_INSUFFICIENT_RESPONSES: {0}/{1}; minimum={2}; evidence={3}" -f $okCount, $panel.Count, $minimumSuccessful, $manifestPath)
}
