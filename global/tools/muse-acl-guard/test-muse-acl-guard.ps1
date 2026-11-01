param([Parameter(Mandatory = $true)][string]$WorkDir)
# Self-checking synthetic test of muse_acl_guard.ps1 (the copy next to this file).
# Seeds throw-away directories under $WorkDir with DACLs in the live Muse form; never touches .config\muse.
$ErrorActionPreference = 'Stop'
$guard = Join-Path $PSScriptRoot 'muse_acl_guard.ps1'
$sandbox = 'S-1-5-21-2684172456-1304752593-309094149-1005'
$me = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$results = New-Object System.Collections.Generic.List[string]
function Assert($cond, $msg) { if (-not $cond) { throw "FAIL: $msg" }; $results.Add("PASS: $msg") }

Add-Type -TypeDefinition @"
using System; using System.Runtime.InteropServices;
public static class GuardTestSddl {
  [DllImport("advapi32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
  static extern bool ConvertStringSecurityDescriptorToSecurityDescriptor(string s, int rev, out IntPtr sd, out int len);
  [DllImport("advapi32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
  static extern bool SetFileSecurity(string name, int info, IntPtr sd);
  public static void SetDacl(string path, string sddl) {
    IntPtr sd; int len;
    if (!ConvertStringSecurityDescriptorToSecurityDescriptor(sddl, 1, out sd, out len)) throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
    if (!SetFileSecurity(path, 4, sd)) throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
  }
}
"@

function New-Case($name, $daclBody) {
    $dir = Join-Path $WorkDir $name
    New-Item -ItemType Directory -Force -Path (Join-Path $dir 'target') | Out-Null
    Set-Content -LiteralPath (Join-Path $dir 'target\auth.json') -Value 'synthetic'
    $inherited = "(A;OICIID;FA;;;SY)(A;OICIID;FA;;;BA)(A;OICIID;FA;;;$me)"
    [GuardTestSddl]::SetDacl((Join-Path $dir 'target'), "D:AI$daclBody$inherited")
    # Children re-inherit from the seeded parent: an empty, unprotected DACL written through
    # SetNamedSecurityInfo (Set-Acl) recomputes the inherited ACEs.
    $fs = New-Object System.Security.AccessControl.FileSecurity
    $fs.SetAccessRuleProtection($false, $true)
    Set-Acl -LiteralPath (Join-Path $dir 'target\auth.json') -AclObject $fs
    return $dir
}
function Invoke-Guard($dir, [int]$threshold = 100) {
    $out = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $guard -Apply -Target (Join-Path $dir 'target') -StateDir (Join-Path $dir 'state') -ThresholdBytes $threshold -MutexName 'Local\MuseAclGuardSelfTest' -LockTimeoutSeconds 2
    return ($out | Select-Object -Last 1 | ConvertFrom-Json)
}
function Test-ReadDenied($path) {
    @((Get-Acl -LiteralPath $path).Access | Where-Object {
        $_.AccessControlType -eq 'Deny' -and $_.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value -eq $sandbox -and
        ((([int64]([int]$_.FileSystemRights)) -band 0xFFFFFFFFL) -band 0x80000001L) -ne 0 }).Count -gt 0
}

$pair = "(D;;FR;;;$sandbox)(D;OICIIO;0x80120089;;;$sandbox)"
$session = "(D;OICIIO;0x40130156;;;S-1-5-21-1373571107-3775292811-1737983867-2331733014)(D;;0x130156;;;S-1-5-21-1373571107-3775292811-1737983867-2331733014)"
$results.Add("guard_sha256=" + (Get-FileHash -LiteralPath $guard -Algorithm SHA256).Hash)

# Live form: 6 duplicate pairs + session denies.
$c1 = New-Case 'live-form' (($pair * 6) + $session)
Assert (Test-ReadDenied (Join-Path $c1 'target\auth.json')) 'seed: auth.json read-denied before'
$r = Invoke-Guard $c1 100000
Assert ($r.action -eq 'below_threshold') 'below threshold does not write'
$r = Invoke-Guard $c1
Assert ($r.action -eq 'compacted') "compacted (got $($r.action))"
Assert ($r.drop_duplicates -ge 5 -and $r.drop_session_denies -eq 2) "dropped duplicates ($($r.drop_duplicates)) and 2 session denies"
Assert (Test-Path -LiteralPath $r.backup) 'backup SDDL written before the change'
$after = Get-Acl (Join-Path $c1 'target')
Assert ($after.AreAccessRulesCanonical) 'resulting DACL canonical'
Assert (-not $after.AreAccessRulesProtected) 'inheritance state preserved (not protected)'
Assert (@($after.Access | Where-Object { -not $_.IsInherited }).Count -eq 2) 'exactly the sandbox pair remains explicit'
Assert (Test-ReadDenied (Join-Path $c1 'target\auth.json')) 'auth.json still read-denied to sandbox after compaction'
Assert (Test-ReadDenied (Join-Path $c1 'target')) 'directory read-denied to sandbox after compaction'
$r = Invoke-Guard $c1
Assert ($r.action -eq 'nothing_to_drop') 'second run is a no-op'

# Only allow rules for the sandbox group (no deny pair): must refuse and leave the DACL unchanged.
$allow = "(A;;FR;;;$sandbox)(A;OICIIO;FR;;;$sandbox)"
$c2 = New-Case 'no-deny-pair' (($allow * 3) + $session)
$before = (Get-Acl (Join-Path $c2 'target')).Sddl
$r = Invoke-Guard $c2
Assert ($r.action -eq 'refused_sandbox_pair_missing') "allow-only plan refused (got $($r.action))"
Assert ((Get-Acl (Join-Path $c2 'target')).Sddl -eq $before) 'refused case left DACL unchanged'

# Protected DACL: must refuse.
$c3 = New-Case 'protected' (($pair * 3) + $session)
$p = Get-Acl (Join-Path $c3 'target'); $p.SetAccessRuleProtection($true, $true); Set-Acl -LiteralPath (Join-Path $c3 'target') -AclObject $p
$before = (Get-Acl (Join-Path $c3 'target')).Sddl
$r = Invoke-Guard $c3
Assert ($r.action -eq 'refused_protected_dacl') "protected DACL refused (got $($r.action))"
Assert ((Get-Acl (Join-Path $c3 'target')).Sddl -eq $before) 'protected case left DACL unchanged'

# Busy lock: must skip without writing.
$c4 = New-Case 'busy' (($pair * 3) + $session)
$before = (Get-Acl (Join-Path $c4 'target')).Sddl
$m = New-Object System.Threading.Mutex($true, 'Local\MuseAclGuardSelfTest')
try { $r = Invoke-Guard $c4 } finally { $m.ReleaseMutex(); $m.Dispose() }
Assert ($r.action -eq 'lock_busy') "busy lock skipped (got $($r.action))"
Assert ((Get-Acl (Join-Path $c4 'target')).Sddl -eq $before) 'busy case left DACL unchanged'

$results
"ALL PASS"
