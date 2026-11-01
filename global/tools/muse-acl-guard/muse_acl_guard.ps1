param(
    [switch]$Apply,
    [string]$Target = "$env:USERPROFILE\.config\muse",
    [int]$ThresholdBytes = 16384,
    [int]$LockTimeoutSeconds = 20,
    [string]$StateDir = "$env:USERPROFILE\.agents\maintenance\muse-acl-guard",
    [string]$MutexName = 'Global\TbhWindowsSandboxAclPublication'
)
# Standing guard for the Muse Windows sandbox DACL on the Muse config directory.
# Muse (windows_elevated backend) appends a duplicate MuseSandboxUsers DENY pair plus a DENY
# pair for the finished per-session SID on every sandbox session. Near the 64 KiB ACL limit
# SetNamedSecurityInfoW fails with 1340 and every Muse shell command fails in ~120 ms.
# Behaviour:
# - below ThresholdBytes: log and exit, no write;
# - otherwise, only with -Apply and only while holding Muse's own ACL publication mutex
#   (Global\TbhWindowsSandboxAclPublication), rewrite THIS directory's DACL keeping inherited
#   rules and one copy of each distinct explicit rule, dropping DENY rules for unresolvable
#   per-session S-1-5-21 SIDs. The MuseSandboxUsers DENY pair is always preserved.
# Never deletes files, never changes owner/allow rules/other paths. A backup SDDL precedes each write.
# Refuses protected DACLs and any plan without the sandbox read-DENY pair; post-verifies child files.
$ErrorActionPreference = 'Stop'
New-Item -ItemType Directory -Force -Path (Join-Path $StateDir 'backup') | Out-Null
$logPath = Join-Path $StateDir 'guard.jsonl'
$entry = [ordered]@{ ts = (Get-Date).ToUniversalTime().ToString('o'); target = $Target; threshold = $ThresholdBytes; apply = [bool]$Apply }

function Write-GuardLog {
    param($Entry)
    [IO.File]::AppendAllText($logPath, ([pscustomobject]$Entry | ConvertTo-Json -Compress) + "`n", (New-Object System.Text.UTF8Encoding($false)))
    [pscustomobject]$Entry | ConvertTo-Json -Compress
}

function Test-SandboxReadDeny {
    # True when the rule is a DENY for MuseSandboxUsers that removes read (ReadData bit or GENERIC_READ).
    param($Rule)
    if ($Rule.AccessControlType -ne 'Deny' -or $Rule.IdentityReference.Value -notlike '*MuseSandboxUsers') { return $false }
    $bits = [int64]([int]$Rule.FileSystemRights) -band 0xFFFFFFFFL
    return (($bits -band 0x80000001L) -ne 0)
}

function Test-SandboxDenyPair {
    # Requires both halves of Muse's pair: a DENY effective on the directory itself and an inheritable
    # DENY reaching files (ObjectInherit), so auth.json stays read-denied to the sandbox.
    param($Rules)
    $denies = @($Rules | Where-Object { Test-SandboxReadDeny $_ })
    $self = @($denies | Where-Object { ($_.PropagationFlags -band [System.Security.AccessControl.PropagationFlags]::InheritOnly) -eq 0 }).Count
    $files = @($denies | Where-Object { ($_.InheritanceFlags -band [System.Security.AccessControl.InheritanceFlags]::ObjectInherit) -ne 0 }).Count
    return ($self -ge 1 -and $files -ge 1)
}

function Get-CompactionPlan {
    param($Acl)
    $keep = New-Object System.Collections.Generic.List[object]
    $seen = @{}
    $dup = 0; $session = 0
    foreach ($r in @($Acl.Access | Where-Object { -not $_.IsInherited })) {
        $id = $r.IdentityReference.Value
        $key = "$id|$($r.AccessControlType)|$([int]$r.FileSystemRights)|$($r.InheritanceFlags)|$($r.PropagationFlags)"
        if ($seen.ContainsKey($key)) { $dup++; continue }
        $seen[$key] = $true
        if ($r.AccessControlType -eq 'Deny' -and $id -match '^S-1-5-21-\d+-\d+-\d+-\d+$') {
            $resolvable = $true
            try { [void](New-Object System.Security.Principal.SecurityIdentifier($id)).Translate([System.Security.Principal.NTAccount]) } catch { $resolvable = $false }
            if (-not $resolvable) { $session++; continue }
        }
        $keep.Add($r)
    }
    [pscustomobject]@{ Keep = $keep; Duplicates = $dup; SessionDenies = $session }
}

try {
    $item = Get-Item -LiteralPath $Target -Force
    if (-not $item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw "Target is not a real directory: $Target" }
    $acl = Get-Acl -LiteralPath $Target
    $entry.sd_bytes = $acl.GetSecurityDescriptorBinaryForm().Length
    $entry.rules = @($acl.Access).Count
    if ($entry.sd_bytes -lt $ThresholdBytes) { $entry.action = 'below_threshold'; Write-GuardLog $entry; return }
    if (-not $Apply) { $entry.action = 'would_compact'; Write-GuardLog $entry; return }

    $mutex = $null
    $created = $false
    if (-not [System.Threading.Mutex]::TryOpenExisting($MutexName, [ref]$mutex)) {
        $mutex = New-Object System.Threading.Mutex($false, $MutexName, [ref]$created)
    }
    $held = $false
    try {
        try { $held = $mutex.WaitOne([TimeSpan]::FromSeconds($LockTimeoutSeconds)) }
        catch [System.Threading.AbandonedMutexException] { $held = $true }
        if (-not $held) { $entry.action = 'lock_busy'; Write-GuardLog $entry; return }

        # Re-read under the lock: Muse may have changed the DACL while we waited.
        $acl = Get-Acl -LiteralPath $Target
        $plan = Get-CompactionPlan $acl
        $entry.drop_duplicates = $plan.Duplicates
        $entry.drop_session_denies = $plan.SessionDenies
        if ($acl.AreAccessRulesProtected) { $entry.action = 'refused_protected_dacl'; Write-GuardLog $entry; return }
        if (-not (Test-SandboxDenyPair $plan.Keep)) { $entry.action = 'refused_sandbox_pair_missing'; Write-GuardLog $entry; return }
        if (($plan.Duplicates + $plan.SessionDenies) -eq 0) { $entry.action = 'nothing_to_drop'; Write-GuardLog $entry; return }

        $stamp = (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssfffZ')
        $backup = Join-Path $StateDir "backup\muse-config-acl-$stamp.sddl.txt"
        $lines = @("$Target`t" + $acl.Sddl)
        Get-ChildItem -LiteralPath $Target -Force | ForEach-Object { $lines += ($_.FullName + "`t" + (Get-Acl -LiteralPath $_.FullName).Sddl) }
        [IO.File]::WriteAllLines($backup, $lines)

        $new = New-Object System.Security.AccessControl.DirectorySecurity
        $new.SetAccessRuleProtection($false, $true)
        foreach ($r in $plan.Keep) { $new.AddAccessRule($r) }
        Set-Acl -LiteralPath $Target -AclObject $new

        $after = Get-Acl -LiteralPath $Target
        $entry.backup = $backup
        $entry.sd_bytes_after = $after.GetSecurityDescriptorBinaryForm().Length
        $entry.sandbox_deny_rules_after = @($after.Access | Where-Object { Test-SandboxReadDeny $_ }).Count
        # Post-verify: the pair on the directory and a read DENY on every child file (e.g. auth.json).
        $childrenDenied = @(Get-ChildItem -LiteralPath $Target -Force -File | Where-Object {
            -not @((Get-Acl -LiteralPath $_.FullName).Access | Where-Object { Test-SandboxReadDeny $_ }).Count })
        if (-not (Test-SandboxDenyPair @($after.Access | Where-Object { -not $_.IsInherited })) -or $childrenDenied.Count) {
            $entry.action = 'post_verify_failed'
            $entry.children_without_deny = @($childrenDenied | ForEach-Object { $_.Name })
            throw 'post_verify_failed'
        }
        $entry.action = 'compacted'
        Write-GuardLog $entry
    }
    finally {
        if ($held) { $mutex.ReleaseMutex() }
        $mutex.Dispose()
    }
}
catch {
    # post_verify_failed is raised after the mutex was released by finally; keep its label.
    if ($entry.action -eq 'post_verify_failed') { Write-GuardLog $entry; exit 2 }
    $entry.action = 'error'
    $entry.error = $_.Exception.GetType().Name
    $entry.error_line = $_.InvocationInfo.ScriptLineNumber
    Write-GuardLog $entry
    exit 1
}
