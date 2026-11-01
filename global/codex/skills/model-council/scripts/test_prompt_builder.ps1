$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "security.ps1")
. (Join-Path $PSScriptRoot "prompt_builder.ps1")

function Assert-True([bool]$condition, [string]$message) {
    if (-not $condition) { throw "ASSERTION_FAILED: $message" }
}

$plan = "PLAN_DO_REVIEW"
$context = "CONTEXT_DO_REVIEW"
$review = Build-CouncilReviewUser -LensName "Reviewer" -LensDescription "test" -ReviewPlan $plan -ReviewContext $context
$round1 = Build-CouncilBrainstormRoundOneUser -LensName "Reviewer" -LensDescription "test" -ReviewPlan $plan -ReviewContext $context
foreach ($message in @($review, $round1)) {
    Assert-True ($message -match [regex]::Escape($plan)) "review message contains the plan"
    Assert-True ($message -match [regex]::Escape($context)) "review message contains the context"
}

$round2 = @(Get-BrainstormRoundTwoCandidates -HealthyEvidence @(
    [pscustomobject]@{ model = "z-ai/glm-5.3-flash"; ok = $true },
    [pscustomobject]@{ model = "anthropic/claude-fable-5"; ok = $true }
))
Assert-True ($round2.Count -eq 2) "round two retains all healthy public models"

"prompt builder tests passed"
