param(
    [ValidateSet('Plan', 'Apply', 'ScanDescendants', 'Rollback')][string]$Mode = 'Plan',
    [string]$ConfigPath = (Join-Path $PSScriptRoot 'muse-read-deny.json'),
    [string]$StateDir = "$env:USERPROFILE\.agents\maintenance\muse-acl-guard",
    [string]$ProfileRoot = $env:USERPROFILE,
    [string]$MutexName = 'Global\TbhWindowsSandboxAclPublication',
    [int]$LockTimeoutSeconds = 600,
    [string]$GroupName = '',
    [string]$RunMutexPrefix = 'Local\AgentStackMuseReadDeny'
)
# Keeps the Muse shell sandbox from READING credential stores, browser profiles, client and personal
# data. Adds one DENY (rights 0x20089 = Read without Synchronize, the signature of this tool; Muse's
# own denies use 0x120089/0x80120089) for the local group MuseSandboxUsers: inheritable
# (ContainerInherit+ObjectInherit) on directory roots, plain on file roots.
# - Apply: adds the ACE on every configured root, reconciles roots that left the config.
# - ScanDescendants: walks denied trees; objects the inherited DENY cannot protect (protected DACL, or an
#   explicit ALLOW with read for a SID the sandbox token carries) get their own explicit DENY.
# - Rollback: removes the ACE only where this tool added it (ownership in the state file); an identical
#   ACE that existed before is never removed.
# Only DACL sections are written (no SACL/owner/other ACE); no file is deleted. Profile roots and
# descendants are changed only under Muse's own ACL publication mutex so its read-refresh cannot race.
# Apply and ScanDescendants use separate private run locks and separate state files, so a long scan never
# blocks the frequent Apply (file-root re-assert); two runs of the same mode never overlap (the second
# exits 4 before writing anything). Rollback needs both locks.
# Exit codes (first match wins): 1 errors, 5 needs review / incomplete (unreadable objects, reparse
# points or unknown System entries), 4 lock busy, 3 group absent, 6 refused (owner in group), 0 ok.
$ErrorActionPreference = 'Stop'
$Signature = [System.Security.AccessControl.FileSystemRights]0x20089
$DirInherit = [System.Security.AccessControl.InheritanceFlags]'ContainerInherit, ObjectInherit'
$NoInherit = [System.Security.AccessControl.InheritanceFlags]::None
$SidType = [System.Security.Principal.SecurityIdentifier]
$config = Get-Content -LiteralPath $ConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
if (-not $GroupName) { $GroupName = [string]$config.group }
New-Item -ItemType Directory -Force -Path (Join-Path $StateDir 'backup') | Out-Null
$logPath = Join-Path $StateDir 'read-deny.jsonl'
$statePath = Join-Path $StateDir 'read-deny-state.json'
$scanStatePath = Join-Path $StateDir 'read-deny-scan-state.json'
$utf8 = New-Object System.Text.UTF8Encoding($false)
$runId = (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssfffZ')

function Write-Log($Entry) {
    $Entry.ts = (Get-Date).ToUniversalTime().ToString('o'); $Entry.run = $runId; $Entry.mode = $Mode
    [IO.File]::AppendAllText($logPath, ([pscustomobject]$Entry | ConvertTo-Json -Compress) + "`n", $utf8)
}

$stateSid = $null
foreach ($sp in @($statePath, (Join-Path $StateDir 'read-deny-scan-state.json'))) {
    if (-not $stateSid -and (Test-Path -LiteralPath $sp)) { $stateSid = (Get-Content -LiteralPath $sp -Raw -Encoding UTF8 | ConvertFrom-Json).group_sid }
}
$groupPresent = $true
try { $sid = (New-Object System.Security.Principal.NTAccount($GroupName)).Translate($SidType) }
catch {
    $groupPresent = $false
    # Rollback must still be able to remove ACEs of a deleted group: use the SID recorded at apply time.
    if ($Mode -eq 'Rollback' -and $stateSid) { $sid = New-Object System.Security.Principal.SecurityIdentifier($stateSid) }
    else { Write-Log ([ordered]@{ action = 'group_absent'; group = $GroupName }); 'group_absent'; exit 3 }
}
# The DENY applies to every token holding the group: never run for an owner who is a member.
if ([System.Security.Principal.WindowsIdentity]::GetCurrent().Groups.Value -contains $sid.Value) {
    Write-Log ([ordered]@{ action = 'refused_owner_in_group'; group = $GroupName }); 'refused_owner_in_group'; exit 6
}

# Broad SIDs every Muse sandbox token carries: Everyone, Authenticated Users, Users, INTERACTIVE. An
# explicit ALLOW for them below a denied root is evaluated before the inherited DENY, so such objects need
# their own explicit DENY. An explicit grant WITH WRITE to the sandbox group or one of its accounts marks a
# Muse workspace/explicit root (e.g. Fleet worktrees under agent-fleet\evidence) and exempts that object;
# read-only group/member grants still count as exposure (see Test-NeedsOwnDeny).
$sandboxSids = New-Object System.Collections.Generic.HashSet[string]
foreach ($s in @('S-1-1-0', 'S-1-5-11', 'S-1-5-32-545', 'S-1-5-4')) { [void]$sandboxSids.Add($s) }
$memberSids = New-Object System.Collections.Generic.HashSet[string]
$memberErrors = 0
if ($groupPresent) { try {
    foreach ($o in @(([ADSI]"WinNT://./$GroupName,group").psbase.Invoke('Members'))) {
        $n = $o.GetType().InvokeMember('Name', 'GetProperty', $null, $o, $null)
        try { [void]$memberSids.Add((New-Object System.Security.Principal.NTAccount($n)).Translate($SidType).Value) }
        catch { $memberErrors++; Write-Log ([ordered]@{ action = 'member_sid_unresolved'; member = $n }) }
    }
} catch { $memberErrors++; Write-Log ([ordered]@{ action = 'member_enumeration_failed'; error = $_.Exception.GetType().Name }) } }

function Get-Roots {
    $roots = New-Object System.Collections.Generic.List[string]
    $skipped = New-Object System.Collections.Generic.List[string]
    foreach ($rel in $config.profile_relative) {
        $p = Join-Path $ProfileRoot $rel
        if (Test-Path -LiteralPath $p) { $roots.Add($p) }
    }
    foreach ($d in $config.drive_roots) {
        if ($d.mode -ne 'deny_top_level_entries_except') { throw "Unknown drive_roots mode: $($d.mode)" }
        if (-not (Test-Path -LiteralPath $d.root)) { continue }
        $allow = @($d.allow | ForEach-Object { $_.ToLowerInvariant() })
        foreach ($e in (Get-ChildItem -LiteralPath $d.root -Force)) {
            if ($allow -contains $e.Name.ToLowerInvariant()) { continue }
            if ($e.Attributes -band [IO.FileAttributes]::ReparsePoint) {
                # reviewed_reparse: links an owner-side review found harmless (e.g. dangling, or the target
                # itself lies under a denied root). Anything else needs review (exit 5).
                $reviewed = @(@($d.reviewed_reparse) | Where-Object { $_ } | ForEach-Object { $_.ToLowerInvariant() })
                $skipped.Add($(if ($reviewed -contains $e.Name.ToLowerInvariant()) { 'reparse-reviewed:' } else { 'reparse:' }) + $e.FullName); continue
            }
            if ($e.Attributes -band [IO.FileAttributes]::System) {
                $known = @('dumpstack.log.tmp', 'dumpstack.log', 'pagefile.sys', 'hiberfil.sys', 'swapfile.sys')
                $skipped.Add($(if ($known -contains $e.Name.ToLowerInvariant()) { 'system:' } else { 'system-review:' }) + $e.FullName); continue
            }
            $roots.Add($e.FullName)
        }
    }
    return @{ Roots = $roots; Skipped = $skipped }
}

function Get-Sec($Item) { return $Item.GetAccessControl([System.Security.AccessControl.AccessControlSections]::Access) }

function Test-OurRule($Rule, [bool]$IsDir) {
    if ($Rule.IsInherited -or $Rule.AccessControlType -ne 'Deny') { return $false }
    if ($Rule.IdentityReference.Value -ne $sid.Value -or [int]$Rule.FileSystemRights -ne [int]$Signature) { return $false }
    if ($Rule.PropagationFlags -ne [System.Security.AccessControl.PropagationFlags]::None) { return $false }
    if ($IsDir) { return $Rule.InheritanceFlags -eq $DirInherit }
    return $Rule.InheritanceFlags -eq $NoInherit
}

function Set-Deny($Item) {
    $isDir = [bool]$Item.PSIsContainer
    $sec = Get-Sec $Item
    $explicit = @($sec.GetAccessRules($true, $false, $SidType))
    if (@($explicit | Where-Object { Test-OurRule $_ $isDir }).Count) { return 'present' }
    # Any foreign DENY for the same SID can be merged by the ACL API with ours (rights OR-ed or flags
    # widened), making an exact rollback impossible: protect anyway, but never claim ownership.
    $foreign = @($explicit | Where-Object { $_.AccessControlType -eq 'Deny' -and $_.IdentityReference.Value -eq $sid.Value }).Count
    [IO.File]::AppendAllText((Join-Path $StateDir "backup\read-deny-$runId.sddl.txt"), "$($Item.FullName)`t$($sec.GetSecurityDescriptorSddlForm('Access'))`n", $utf8)
    $flags = if ($isDir) { $DirInherit } else { $NoInherit }
    $sec.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule($sid, $Signature, $flags, [System.Security.AccessControl.PropagationFlags]::None, 'Deny')))
    $Item.SetAccessControl($sec)
    if ($foreign) { Write-Log ([ordered]@{ action = 'conflict_foreign_same_mask'; path = $Item.FullName }); return 'present' }
    return 'added'
}

function Remove-Deny($Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return 'absent' }
    $item = Get-Item -LiteralPath $Path -Force
    $sec = Get-Sec $item
    $ours = @($sec.GetAccessRules($true, $false, $SidType) | Where-Object { Test-OurRule $_ ([bool]$item.PSIsContainer) })
    if (-not $ours.Count) { return 'not_present' }
    foreach ($r in $ours) { [void]$sec.RemoveAccessRuleSpecific($r) }
    $item.SetAccessControl($sec)
    return 'removed'
}

function Test-NeedsOwnDeny($Sec) {
    $explicitRules = @($Sec.GetAccessRules($true, $false, $SidType))
    # Muse's workspace grant is an explicit ALLOW with WRITE (FILE_WRITE_DATA 0x2, observed 0x1301bf) for
    # the group; a read-only explicit group ALLOW is a copy of an inherited grant (e.g. a protected DACL
    # made while Muse's RX was inherited) and is still treated as an exposure.
    $museGrant = @($explicitRules | Where-Object { $_.AccessControlType -eq 'Allow' -and ($_.IdentityReference.Value -eq $sid.Value -or $memberSids.Contains($_.IdentityReference.Value)) -and ([int]$_.FileSystemRights -band 2) }).Count
    if ($museGrant) { return $null }
    if ($Sec.AreAccessRulesProtected) { return 'protected_dacl' }
    foreach ($r in $explicitRules) {
        # ReadData/ListDirectory (0x1), GENERIC_READ (0x80000000) or GENERIC_ALL (0x10000000).
        $bits = [int64]([int]$r.FileSystemRights) -band 0xFFFFFFFFL
        $who = $r.IdentityReference.Value
        if ($r.AccessControlType -eq 'Allow' -and ($sandboxSids.Contains($who) -or $who -eq $sid.Value -or $memberSids.Contains($who)) -and ($bits -band 0x90000001L)) { return 'explicit_allow' }
    }
    return $null
}

$resolved = Get-Roots
$desired = @($resolved.Roots)
$state = @{ owned = @(); owned_descendants = @() }
if (Test-Path -LiteralPath $statePath) { $state.owned = @((Get-Content -LiteralPath $statePath -Raw -Encoding UTF8 | ConvertFrom-Json).owned | Where-Object { $_ }) }
if (Test-Path -LiteralPath $scanStatePath) { $state.owned_descendants = @((Get-Content -LiteralPath $scanStatePath -Raw -Encoding UTF8 | ConvertFrom-Json).owned_descendants | Where-Object { $_ }) }
$profilePrefix = $ProfileRoot.TrimEnd('\') + '\'
function Test-Profile($p) { return $p.StartsWith($profilePrefix, [StringComparison]::OrdinalIgnoreCase) }

if ($Mode -eq 'Plan') {
    $missing = @($desired | Where-Object { $i = Get-Item -LiteralPath $_ -Force
        -not @((Get-Sec $i).GetAccessRules($true, $false, $SidType) | Where-Object { Test-OurRule $_ ([bool]$i.PSIsContainer) }).Count })
    [pscustomobject]@{ desired = $desired.Count; missing = $missing; stale = @($state.owned | Where-Object { $desired -notcontains $_ }); skipped = @($resolved.Skipped) } | ConvertTo-Json -Depth 3 -Compress
    return
}

$summary = [ordered]@{ added = 0; present = 0; removed = 0; errors = $memberErrors; lock_busy = 0; skipped = $resolved.Skipped.Count; review = 0; scanned = 0; unreadable = 0 }
$owned = New-Object System.Collections.Generic.HashSet[string]([StringComparer]::OrdinalIgnoreCase)
foreach ($o in $state.owned) { [void]$owned.Add($o) }
$ownedDesc = New-Object System.Collections.Generic.HashSet[string]([StringComparer]::OrdinalIgnoreCase)
foreach ($o in $state.owned_descendants) { [void]$ownedDesc.Add($o) }
foreach ($k in $resolved.Skipped) {
    Write-Log ([ordered]@{ action = 'skipped_root'; entry = $k })
    # A top-level junction/symlink cannot be protected by a DENY on the link: flag it for review.
    if ($k -like 'reparse:*' -or $k -like 'system-review:*') { $summary.review++ }
}

function Open-NamedMutex($Name) {
    $m = $null
    if (-not [System.Threading.Mutex]::TryOpenExisting($Name, [ref]$m)) { $c = $false; $m = New-Object System.Threading.Mutex -ArgumentList $false, $Name, ([ref]$c) }
    return $m
}
function Enter-NamedMutex($M, [int]$Seconds) {
    try { return $M.WaitOne([TimeSpan]::FromSeconds($Seconds)) } catch [System.Threading.AbandonedMutexException] { return $true }
}

# Serialize runs of the same mode (they share a state file); Rollback needs both.
$lockNames = switch ($Mode) { 'Apply' { @('apply') } 'ScanDescendants' { @('scan') } default { @('apply', 'scan') } }
$runLocks = New-Object System.Collections.Generic.List[object]
foreach ($ln in $lockNames) {
    $l = Open-NamedMutex ($RunMutexPrefix + '-' + $ln)
    if (-not (Enter-NamedMutex $l 0)) {
        $l.Dispose(); foreach ($h in $runLocks) { $h.ReleaseMutex(); $h.Dispose() }
        Write-Log ([ordered]@{ action = 'run_lock_busy'; lock = $ln }); 'run_lock_busy'; exit 4
    }
    $runLocks.Add($l)
}
# Muse's publication mutex is only OPENED, never created (a mutex created here would carry this user's
# default DACL and could get in Muse's way). Absent = no Muse publication running: profile writes proceed
# unlocked (logged; a lost update is restored by the next Apply). Present but not openable = busy.
$museMutex = $null; $museLock = @{ failed = $false; absent = $false }
try {
    if (-not [System.Threading.Mutex]::TryOpenExisting($MutexName, [ref]$museMutex)) {
        $museMutex = $null; $museLock.absent = $true; Write-Log ([ordered]@{ action = 'muse_mutex_absent' })
    }
} catch { $museMutex = $null; $museLock.failed = $true; Write-Log ([ordered]@{ action = 'muse_mutex_unopenable'; error = $_.Exception.GetType().Name }) }

function Invoke-UnderMuseLock([scriptblock]$Body, [int]$Count) {
    # Profile objects are what Muse's read-refresh rewrites: change them only while holding its mutex,
    # and only for one object at a time (never for a whole walk).
    if ($Count -le 0) { return }
    # After one timeout in this run, skip remaining profile objects at once instead of waiting per object.
    if ($null -eq $museMutex -and $museLock.absent) { & $Body; return }
    if ($museLock.failed -or -not (Enter-NamedMutex $museMutex $LockTimeoutSeconds)) {
        if (-not $museLock.failed) { Write-Log ([ordered]@{ action = 'muse_lock_busy' }) }
        $museLock.failed = $true; $summary.lock_busy += $Count; return
    }
    try { & $Body } finally { $museMutex.ReleaseMutex() }
}

function Add-Root($p) {
    $t = [Diagnostics.Stopwatch]::StartNew()
    try {
        $r = Set-Deny (Get-Item -LiteralPath $p -Force); $summary[$r]++
        if ($r -eq 'added') { [void]$owned.Add($p); Write-Log ([ordered]@{ action = 'added'; path = $p; seconds = [math]::Round($t.Elapsed.TotalSeconds, 1) }) }
    } catch { $summary.errors++; Write-Log ([ordered]@{ action = 'error'; path = $p; error = $_.Exception.GetType().Name }) }
}

function Invoke-PerObject($List, [scriptblock]$Action) {
    # Profile objects: take Muse's lock per object so Muse sessions can interleave between objects.
    foreach ($p in $List) { if (Test-Profile $p) { Invoke-UnderMuseLock { & $Action $p } 1 } else { & $Action $p } }
}

function Remove-Owned($p) {
    try {
        $r = Remove-Deny $p
        if ($r -eq 'removed') { $summary.removed++; Write-Log ([ordered]@{ action = 'removed'; path = $p }) }
        [void]$owned.Remove($p); [void]$ownedDesc.Remove($p)
    } catch { $summary.errors++; Write-Log ([ordered]@{ action = 'error'; path = $p; error = $_.Exception.GetType().Name }) }
}

function Add-Descendant($path, $why) {
    $r = Set-Deny (Get-Item -LiteralPath $path -Force)
    if ($r -eq 'added') { [void]$ownedDesc.Add($path); $summary.added++; Write-Log ([ordered]@{ action = 'added_descendant'; path = $path; reason = $why }) }
}

function Scan-Tree($Root) {
    $stack = New-Object System.Collections.Generic.Stack[System.IO.DirectoryInfo]
    $stack.Push([IO.DirectoryInfo]$Root)
    while ($stack.Count) {
        $dir = $stack.Pop()
        try { $entries = @($dir.EnumerateFileSystemInfos()) } catch { $summary.unreadable++; Write-Log ([ordered]@{ action = 'unreadable'; path = $dir.FullName }); continue }
        foreach ($e in $entries) {
            $summary.scanned++
            $isReparse = [bool]($e.Attributes -band [IO.FileAttributes]::ReparsePoint)
            try {
                $why = Test-NeedsOwnDeny ($e.GetAccessControl([System.Security.AccessControl.AccessControlSections]::Access))
                if ($why -and $isReparse) {
                    # Setting an ACL through a link may land on its target: report instead of writing.
                    $summary.review++; Write-Log ([ordered]@{ action = 'reparse_needs_review'; path = $e.FullName; reason = $why })
                } elseif ($why) {
                    # Reading needs no lock; only this short write on a profile object takes Muse's lock.
                    $target = $e.FullName
                    if (Test-Profile $target) { Invoke-UnderMuseLock { Add-Descendant $target $why } 1 } else { Add-Descendant $target $why }
                }
            } catch { $summary.unreadable++; Write-Log ([ordered]@{ action = 'unreadable'; path = $e.FullName; error = $_.Exception.GetType().Name }) }
            # Never descend through a link (it may leave the denied tree).
            if ($e -is [IO.DirectoryInfo] -and -not $isReparse) { $stack.Push($e) }
        }
    }
}

try {
    if ($Mode -eq 'Apply') {
        Invoke-PerObject $desired { param($x) Add-Root $x }
        $stale = @($owned | Where-Object { $desired -notcontains $_ })
    } elseif ($Mode -eq 'ScanDescendants') {
        foreach ($root in $desired) {
            if (-not (Get-Item -LiteralPath $root -Force).PSIsContainer) { continue }
            Scan-Tree $root
        }
        # Descendant denies whose root left the config are released by the scan (it owns them).
        $stale = @($ownedDesc | Where-Object { $d = $_; -not @($desired | Where-Object { $d.StartsWith($_.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase) }).Count })
    } else {
        $stale = @(@($owned) + @($ownedDesc))
    }
    Invoke-PerObject $stale { param($x) Remove-Owned $x }
    # Descendant denies of a root that is no longer owned are released together with that root's rollback.
    if ($Mode -ne 'ScanDescendants') { [IO.File]::WriteAllText($statePath, ([pscustomobject]@{ group_sid = $sid.Value; owned = @($owned); updated = $runId } | ConvertTo-Json -Depth 3), $utf8) }
    if ($Mode -ne 'Apply') { [IO.File]::WriteAllText($scanStatePath, ([pscustomobject]@{ group_sid = $sid.Value; owned_descendants = @($ownedDesc); updated = $runId } | ConvertTo-Json -Depth 3), $utf8) }
}
finally {
    if ($museMutex) { $museMutex.Dispose() }
    foreach ($h in $runLocks) { $h.ReleaseMutex(); $h.Dispose() }
}
$summary.desired = $desired.Count
Write-Log ([ordered]@{ action = 'summary'; summary = $summary })
[pscustomobject]$summary | ConvertTo-Json -Compress
if ($summary.errors) { exit 1 }
if ($summary.review -or $summary.unreadable) { exit 5 }
if ($summary.lock_busy) { exit 4 }
