param([Parameter(Mandatory = $true)][string]$WorkDir)
# Self-checking synthetic test of muse_read_deny.ps1 (the copy next to this file). Uses a fake profile
# and a fake drive root under $WorkDir and a private mutex; never touches real profile or D: roots.
# ACE-level assertions only; the effective-access check with a real Muse token is a separate probe.
$ErrorActionPreference = 'Stop'
$script = Join-Path $PSScriptRoot 'muse_read_deny.ps1'
$results = New-Object System.Collections.Generic.List[string]
function Assert($cond, $msg) { if (-not $cond) { throw "FAIL: $msg" }; $results.Add("PASS: $msg") }
$results.Add('script_sha256=' + (Get-FileHash -LiteralPath $script -Algorithm SHA256).Hash)
$sidObj = (New-Object System.Security.Principal.NTAccount('MuseSandboxUsers')).Translate([System.Security.Principal.SecurityIdentifier]); $sid = $sidObj.Value
$usersSid = New-Object System.Security.Principal.SecurityIdentifier('S-1-5-32-545')
$sig = [System.Security.AccessControl.FileSystemRights]0x20089

$profile = Join-Path $WorkDir 'profile'; $drive = Join-Path $WorkDir 'drive'; $state = Join-Path $WorkDir 'state'
foreach ($d in @("$profile\.codex", "$profile\Documents\sub", "$profile\keep-open", "$profile\Prior", "$profile\WrongFlags", "$profile\ForeignMask",
                 "$drive\ClientCo\deep\locked", "$drive\AL Lab", "$drive\scratch")) { New-Item -ItemType Directory -Force -Path $d | Out-Null }
Set-Content "$profile\.codex\auth.json" 'synthetic'; Set-Content "$profile\Documents\sub\doc.txt" 'synthetic'
Set-Content "$profile\.claude.json" 'synthetic'; Set-Content "$drive\ClientCo\deep\data.txt" 'synthetic'
Set-Content "$drive\ClientCo\deep\shared.txt" 'synthetic'; Set-Content "$drive\ClientCo\deep\locked\in.txt" 'synthetic'
Set-Content "$drive\AL Lab\code.py" 'synthetic'; Set-Content "$drive\root-file.txt" 'synthetic'
Set-Content "$drive\DumpStack.log.tmp" 'synthetic'; (Get-Item "$drive\DumpStack.log.tmp").Attributes = 'Hidden, System'
Set-Content "$drive\ClientCo\deep\generic.txt" 'synthetic'

function Add-Rule($path, $rule) { $i = Get-Item -LiteralPath $path -Force; $s = $i.GetAccessControl('Access'); $s.AddAccessRule($rule); $i.SetAccessControl($s) }
# Unrelated explicit ACE that must survive; a pre-existing identical ACE that rollback must keep;
# a same-mask DENY without inheritance that must not count as protection.
Add-Rule "$profile\.codex" (New-Object System.Security.AccessControl.FileSystemAccessRule($usersSid, 'ReadAndExecute', 'ContainerInherit, ObjectInherit', 'None', 'Allow'))
Add-Rule "$profile\Prior" (New-Object System.Security.AccessControl.FileSystemAccessRule($sidObj, $sig, 'ContainerInherit, ObjectInherit', 'None', 'Deny'))
Add-Rule "$profile\WrongFlags" (New-Object System.Security.AccessControl.FileSystemAccessRule($sidObj, $sig, 'None', 'None', 'Deny'))
Add-Rule "$profile\ForeignMask" (New-Object System.Security.AccessControl.FileSystemAccessRule($sidObj, [System.Security.AccessControl.FileSystemRights]1, 'ContainerInherit, ObjectInherit', 'None', 'Deny'))
# GENERIC_READ explicit ALLOW for Users, written as raw SDDL (the ACL API would not keep generic bits).
Add-Type -TypeDefinition @"
using System; using System.Runtime.InteropServices;
public static class ReadDenyTestSddl {
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
$me = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
[ReadDenyTestSddl]::SetDacl("$drive\ClientCo\deep\generic.txt", "D:AI(A;;GR;;;BU)(A;ID;FA;;;$me)(A;ID;FA;;;SY)")
# Descendants the inherited DENY cannot protect: explicit ALLOW for Users on a file, protected DACL dir.
Add-Rule "$drive\ClientCo\deep\shared.txt" (New-Object System.Security.AccessControl.FileSystemAccessRule($usersSid, 'Read', 'Allow'))
$li = Get-Item "$drive\ClientCo\deep\locked"; $ls = $li.GetAccessControl('Access'); $ls.SetAccessRuleProtection($true, $true); $li.SetAccessControl($ls)
# A Muse workspace inside a denied root: Muse grants its group an explicit ALLOW there (here also protected).
New-Item -ItemType Directory -Force -Path "$profile\.codex\ws\src" | Out-Null; Set-Content "$profile\.codex\ws\src\a.py" 'synthetic'
Add-Rule "$profile\.codex\ws" (New-Object System.Security.AccessControl.FileSystemAccessRule($sidObj, [System.Security.AccessControl.FileSystemRights]0x1301bf, 'ContainerInherit, ObjectInherit', 'None', 'Allow'))
# A copied read-only group ALLOW (protected DACL made while Muse's RX was inherited) must still be denied.
New-Item -ItemType Directory -Force -Path "$profile\.codex\copied" | Out-Null; Set-Content "$profile\.codex\copied\k.txt" 'synthetic'
Add-Rule "$profile\.codex\copied" (New-Object System.Security.AccessControl.FileSystemAccessRule($sidObj, 'ReadAndExecute', 'ContainerInherit, ObjectInherit', 'None', 'Allow'))
$wi = Get-Item "$profile\.codex\ws"; $wsx = $wi.GetAccessControl('Access'); $wsx.SetAccessRuleProtection($true, $true); $wi.SetAccessControl($wsx)

$orig = @{}; foreach ($p in @("$drive\ClientCo\deep\generic.txt", "$profile\.codex", "$profile\Documents", "$profile\.claude.json", "$profile\Prior", "$drive\ClientCo", "$drive\AL Lab", "$drive\root-file.txt", "$drive\ClientCo\deep\shared.txt", "$drive\ClientCo\deep\locked")) { $orig[$p] = (Get-Item -LiteralPath $p -Force).GetAccessControl('Access').GetSecurityDescriptorSddlForm('Access') }

$cfg = Join-Path $WorkDir 'cfg.json'
function Write-Config($rel) {
    [pscustomobject]@{ schema_version = 1; group = 'MuseSandboxUsers'; profile_relative = $rel
        drive_roots = @([pscustomobject]@{ root = $drive; mode = 'deny_top_level_entries_except'; allow = @('AL Lab', 'scratch') }) } |
        ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $cfg -Encoding UTF8
}
$script:lastExit = 0
function Run($mode, $mutex = 'Local\MuseReadDenySelfTest', $group = '') {
    $a = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $script, '-Mode', $mode, '-ConfigPath', $cfg, '-StateDir', $state, '-ProfileRoot', $profile, '-MutexName', $mutex, '-LockTimeoutSeconds', '2', '-RunMutexPrefix', 'Local\MuseReadDenySelfTestRun')
    if ($group) { $a += @('-GroupName', $group) }
    $out = & powershell.exe @a
    $script:lastExit = $LASTEXITCODE
    return ($out | Select-Object -Last 1)
}
function Own($path) {
    $i = Get-Item -LiteralPath $path -Force
    @($i.GetAccessControl('Access').GetAccessRules($true, $false, [System.Security.Principal.SecurityIdentifier]) | Where-Object {
        $_.AccessControlType -eq 'Deny' -and $_.IdentityReference.Value -eq $sid -and [int]$_.FileSystemRights -eq 0x20089 }).Count
}
function Denied($path) {
    @((Get-Item -LiteralPath $path -Force).GetAccessControl('Access').GetAccessRules($true, $true, [System.Security.Principal.SecurityIdentifier]) |
        Where-Object { $_.AccessControlType -eq 'Deny' -and $_.IdentityReference.Value -eq $sid -and ([int]$_.FileSystemRights -band 1) }).Count -gt 0
}

Write-Config @('.codex', 'Documents', '.claude.json', 'Prior', 'WrongFlags', 'ForeignMask', 'does-not-exist')
$null = Run 'Plan' 'Local\MuseReadDenySelfTest' 'NoSuchGroupXyz'
Assert ($script:lastExit -eq 3) 'absent group exits 3'
$plan = Run 'Plan' | ConvertFrom-Json
Assert ($plan.desired -eq 8) "plan: 6 profile roots + ClientCo + root-file (got $($plan.desired))"
Assert (@($plan.skipped | Where-Object { $_ -like 'system:*DumpStack.log.tmp' }).Count -eq 1) 'system-attribute top-level file skipped and reported'
$r = Run 'Apply' | ConvertFrom-Json
Assert ($r.added -eq 5 -and $r.present -eq 3 -and $r.errors -eq 0) "apply: 5 added; Prior present; WrongFlags+ForeignMask protected-not-owned (got added $($r.added), present $($r.present))"
$wf = @((Get-Item "$profile\WrongFlags").GetAccessControl('Access').GetAccessRules($true, $false, [System.Security.Principal.SecurityIdentifier]) | Where-Object { $_.AccessControlType -eq 'Deny' -and $_.InheritanceFlags -eq [System.Security.AccessControl.InheritanceFlags]'ContainerInherit, ObjectInherit' })
Assert ($wf.Count -ge 1) 'WrongFlags now carries an inheritable (CI|OI) DENY'
Assert (Denied "$profile\ForeignMask") 'foreign different-mask DENY root protected'
Assert (@(Get-Content (Join-Path $state 'read-deny.jsonl') | Where-Object { $_ -like '*conflict_foreign_same_mask*WrongFlags*' }).Count -eq 1) 'foreign same-mask conflict logged'
Assert (Denied "$profile\.codex\auth.json") 'file inside denied dir inherits the read-DENY'
Assert (Denied "$profile\Documents\sub\doc.txt") 'deep file inherits the read-DENY'
Assert (Denied "$profile\.claude.json") 'file root gets an explicit read-DENY'
Assert (Denied "$drive\root-file.txt") 'top-level drive file denied'
Assert (Denied "$drive\ClientCo\deep\data.txt") 'non-allowlisted drive dir denied'
Assert (-not (Denied "$drive\AL Lab\code.py")) 'allowlisted drive dir stays readable'
Assert (-not (Denied "$profile\keep-open")) 'unlisted profile dir untouched'
Assert (-not (Denied "$drive\ClientCo\deep\locked\in.txt")) 'precondition: protected DACL dir not reached by inheritance'
$r = Run 'Apply' | ConvertFrom-Json
Assert ($r.added -eq 0 -and $r.present -eq 8) 'second apply is idempotent'
$rl = New-Object System.Threading.Mutex($true, 'Local\MuseReadDenySelfTestRun-apply')
$stateBefore = Get-Content (Join-Path $state 'read-deny-state.json') -Raw
$null = Run 'Apply'
$rl.ReleaseMutex(); $rl.Dispose()
Assert ($script:lastExit -eq 4 -and (Get-Content (Join-Path $state 'read-deny-state.json') -Raw) -eq $stateBefore) 'concurrent run refused (exit 4) without touching state'
# A running scan must not block Apply (separate locks and state files).
$sl = New-Object System.Threading.Mutex($true, 'Local\MuseReadDenySelfTestRun-scan')
$ra = Run 'Apply' | ConvertFrom-Json; $applyExit = $script:lastExit
$null = Run 'ScanDescendants'; $scanExit = $script:lastExit
$sl.ReleaseMutex(); $sl.Dispose()
Assert ($applyExit -eq 0 -and $ra.present -eq 8) 'Apply runs while a scan holds its own lock'
Assert ($scanExit -eq 4) 'second scan refused while a scan runs (exit 4)'
$r = Run 'ScanDescendants' | ConvertFrom-Json
Assert ($r.added -eq 4 -and $r.errors -eq 0) "scan: explicit-allow, GENERIC_READ, protected dir and copied group ALLOW got own DENY (added $($r.added))"
Assert ((Own "$drive\ClientCo\deep\generic.txt") -eq 1) 'GENERIC_READ explicit ALLOW detected and denied'
Assert ((Own "$profile\.codex\copied") -eq 1 -and (Denied "$profile\.codex\copied\k.txt")) 'copied read-only group ALLOW is denied by the scan'
Assert ((Own "$profile\.codex\ws") -eq 0 -and -not (Denied "$profile\.codex\ws\src\a.py")) 'Muse workspace (explicit group ALLOW, protected) left untouched by the scan'
Assert ((Own "$drive\ClientCo\deep\shared.txt") -eq 1) 'explicit-ALLOW file has explicit DENY (evaluated before its ALLOW)'
Assert (Denied "$drive\ClientCo\deep\locked\in.txt") 'protected-DACL subtree now denied'
$r = Run 'ScanDescendants' | ConvertFrom-Json
Assert ($r.added -eq 0) 'second scan is idempotent'
New-Item -ItemType Directory -Force -Path "$drive\NewClient" | Out-Null
Write-Config @('.codex', '.claude.json', 'Prior', 'WrongFlags')
$r = Run 'Apply' | ConvertFrom-Json
Assert ($r.added -eq 1 -and $r.removed -eq 1) "reconcile: new drive dir added, Documents removed (added $($r.added), removed $($r.removed))"
Assert ((Get-Item "$profile\Documents").GetAccessControl('Access').GetSecurityDescriptorSddlForm('Access') -eq $orig["$profile\Documents"]) 'removed root DACL equals original'
$m = New-Object System.Threading.Mutex($true, 'Local\MuseReadDenySelfTest')
try { Write-Config @('.codex', '.claude.json', 'Prior', 'WrongFlags', 'Documents'); $r = Run 'Apply' | ConvertFrom-Json } finally { $m.ReleaseMutex(); $m.Dispose() }
Assert ($r.lock_busy -ge 1 -and $script:lastExit -eq 4 -and -not (Denied "$profile\Documents\sub\doc.txt")) 'busy Muse lock: profile roots skipped, exit 4'
Assert ($r.present -ge 1) 'busy Muse lock: drive roots still processed'
$r = Run 'Rollback' | ConvertFrom-Json
Assert ($r.errors -eq 0) 'rollback without errors'
foreach ($p in @("$profile\.codex", "$profile\.claude.json", "$drive\ClientCo", "$drive\AL Lab", "$drive\root-file.txt", "$drive\ClientCo\deep\shared.txt", "$drive\ClientCo\deep\locked")) {
    Assert ((Get-Item -LiteralPath $p -Force).GetAccessControl('Access').GetSecurityDescriptorSddlForm('Access') -eq $orig[$p]) "rollback restored original DACL: $(Split-Path $p -Leaf)"
}
Assert ((Own "$profile\Prior") -eq 1) 'pre-existing identical ACE kept by rollback'
Assert ((Own "$profile\WrongFlags") -ge 1) 'rollback never removes protection it does not own (conflict root kept)'
Assert (Denied "$profile\ForeignMask") 'rollback kept the merged foreign different-mask DENY'
Assert (-not (Denied "$drive\NewClient")) 'rollback also cleared the reconciled drive dir'
$states = @((Get-Content (Join-Path $state 'read-deny-state.json') -Raw | ConvertFrom-Json).owned).Count + @((Get-Content (Join-Path $state 'read-deny-scan-state.json') -Raw | ConvertFrom-Json).owned_descendants).Count
Assert ($states -eq 0) 'rollback emptied both ownership state files'
# Default lock names (as used by the scheduled tasks) work with the task's token.
$out = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $script -Mode Apply -ConfigPath $cfg -StateDir $state -ProfileRoot $profile -MutexName 'Local\MuseReadDenySelfTest' -LockTimeoutSeconds 2
Assert ($LASTEXITCODE -eq 0) 'Apply with default run-lock names succeeds'
$null = Run 'Rollback'
New-Item -ItemType Directory -Force -Path "$drive\SysDir" | Out-Null; (Get-Item "$drive\SysDir").Attributes = 'Directory, System'
$plan = Run 'Plan' | ConvertFrom-Json
Assert (@($plan.skipped | Where-Object { $_ -like 'system-review:*SysDir' }).Count -eq 1) 'unknown System top-level entry flagged for review'
$null = Run 'Apply'
Assert ($script:lastExit -eq 5) 'unknown System entry makes the run exit 5'
$null = Run 'Rollback'
(Get-Item "$drive\SysDir").Attributes = 'Directory'
cmd /c mklink /J "$drive\LinkToClient" "$drive\ClientCo" | Out-Null
$plan = Run 'Plan' | ConvertFrom-Json
Assert (@($plan.skipped | Where-Object { $_ -like 'reparse:*LinkToClient' }).Count -eq 1) 'top-level junction reported, not written through'
$null = Run 'Apply'
Assert ($script:lastExit -eq 5) 'top-level junction makes the run exit 5 (needs review)'
$null = Run 'Rollback'
$results
'ALL PASS'
