$ErrorActionPreference = 'Stop'

$repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..\..\..\..')).Path
$skillFiles = @(
  'model-audit/SKILL.md',
  'skill-auditor/SKILL.md',
  'creating-mermaid-diagrams/SKILL.md',
  'google-ads-strategy/SKILL.md',
  'meta-ads-strategy/SKILL.md',
  'audio-bed-gen/SKILL.md',
  'avatar-gen/SKILL.md',
  'video-gen/SKILL.md',
  'voice-gen/SKILL.md',
  'pl-tax-compliance/SKILL.md',
  'search-skills/SKILL.md'
)

foreach ($relative in $skillFiles) {
  $path = Join-Path $repo "global\codex\skills\$relative"
  $lines = Get-Content -LiteralPath $path
  if ($lines[0] -ne '---') { throw "Missing frontmatter: $relative" }
  $end = [Array]::IndexOf($lines, '---', 1)
  if ($end -lt 0) { throw "Unclosed frontmatter: $relative" }
  $keys = @($lines[1..($end - 1)] | Where-Object { $_ -match '^[A-Za-z][A-Za-z0-9_-]*:' } | ForEach-Object { ($_ -split ':', 2)[0] })
  if (($keys -join '|') -ne 'name|description') { throw "Non-portable frontmatter in ${relative}: $($keys -join ', ')" }
}

$portableDocs = $skillFiles | Where-Object { $_ -notin @('skill-auditor/SKILL.md') } | ForEach-Object { Join-Path $repo "global\codex\skills\$_" }
foreach ($path in $portableDocs) {
  if ((Get-Content -Raw -LiteralPath $path) -match '(?i)\.claude') { throw "Runtime-coupled path in $path" }
}

foreach ($path in @(
  (Join-Path $repo 'global\codex\skills\avatar-gen\scripts\hedra_key.ps1'),
  (Join-Path $repo 'global\codex\skills\voice-gen\scripts\elevenlabs_key.ps1')
)) {
  if ((Get-Content -Raw -LiteralPath $path) -match '(?i)\.claude') { throw "Helper mentions .claude: $path" }
}

$search = Get-Content -Raw -LiteralPath (Join-Path $repo 'global\codex\skills\search-skills\SKILL.md')
if ($search -match '(?i)(?<![\w-])(ls|find)\s+|2>/dev/null|```bash') { throw 'Bash-only discovery example remains in search-skills.' }

$mermaid = Get-Content -Raw -LiteralPath (Join-Path $repo 'global\codex\skills\creating-mermaid-diagrams\SKILL.md')
# The username portion is a regex character class, not an account; keep this generic detector intact.
if ($mermaid -match 'chrome-headless-shell\\win\d+\\|win64-\d+|C:\\Users\\[^`\r\n]+chrome') { throw 'Hardcoded browser user/version path remains.' }

Write-Output "PASS: $($skillFiles.Count) skill contracts and runtime-path rules verified."
