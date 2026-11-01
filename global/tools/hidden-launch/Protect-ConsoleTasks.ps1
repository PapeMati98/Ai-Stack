[CmdletBinding()]
param(
    [Parameter(Mandatory)][string[]]$TaskName,
    [string]$Runtime = (Join-Path $env:USERPROFILE '.agents\runtime\hidden-launch\1.0.0'),
    [string]$State = (Join-Path $env:USERPROFILE '.agents\hidden-launch-state'),
    [switch]$Apply,
    [switch]$NoDesktopIsolation
)
$ErrorActionPreference = 'Stop'
$launcher = Join-Path $Runtime 'HiddenLaunch.exe'
if (-not (Test-Path -LiteralPath $launcher -PathType Leaf)) { throw "GUI launcher not built: $launcher" }
# A dry run (no -Apply) only reads; the state directory and its ACL change only with -Apply.
if ($Apply) {
    New-Item -ItemType Directory -Path (Join-Path $State 'specs'),(Join-Path $State 'backups'),(Join-Path $State 'logs') -Force | Out-Null
    # Newly created launch data and captured output are private to the owner account.
    # Do not inherit the stack's read grants for contractor sandbox accounts.
    $ownerSid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
    $privateAcl = Get-Acl -LiteralPath $State
    $allowedSids = @($ownerSid.Value,'S-1-5-18','S-1-5-32-544')
    $currentRules = @($privateAcl.GetAccessRules($true,$true,[System.Security.Principal.SecurityIdentifier]))
    $alreadyPrivate = $privateAcl.AreAccessRulesProtected -and $currentRules.Count -eq 3 -and
        @($currentRules | Where-Object {$_.IdentityReference.Value -notin $allowedSids -or $_.AccessControlType -ne 'Allow' -or $_.FileSystemRights -ne 'FullControl'}).Count -eq 0
    if (-not $alreadyPrivate) {
        # Retain the existing owner/audit/integrity descriptor; change DACL only.
        $privateAcl.SetAccessRuleProtection($true,$false)
        foreach ($rule in @($privateAcl.GetAccessRules($true,$false,[System.Security.Principal.SecurityIdentifier]))) { [void]$privateAcl.RemoveAccessRuleSpecific($rule) }
        foreach ($sid in $allowedSids) {
            $rule = [System.Security.AccessControl.FileSystemAccessRule]::new([System.Security.Principal.SecurityIdentifier]::new($sid),'FullControl','ContainerInherit,ObjectInherit','None','Allow')
            [void]$privateAcl.AddAccessRule($rule)
        }
        Set-Acl -LiteralPath $State -AclObject $privateAcl
    }
}
$rows = @()
foreach ($name in $TaskName) {
    $task = Get-ScheduledTask -TaskName $name
    if ($task.Count -gt 1) { throw "Ambiguous task name: $name" }
    if (@($task.Actions | Where-Object { $_.Execute -eq $launcher }).Count -gt 0) {
        $rows += [pscustomobject]@{Task=$name;Result='ALREADY_HIDDEN'}
        continue
    }
    if (@($task.Actions | Where-Object { $_.CimClass.CimClassName -ne 'MSFT_TaskExecAction' }).Count) { throw "Unsupported action: $name" }
    $xmlBefore = Export-ScheduledTask -TaskName $name -TaskPath $task.TaskPath
    $digest = [System.Security.Cryptography.SHA256]::Create()
    $identity = $task.TaskPath + $name + ([xml]$xmlBefore).Task.Actions.OuterXml
    # Version specifications by original action, retaining every earlier spec;
    # BitConverter also works in Windows PowerShell 5.1/.NET Framework.
    $id = ([BitConverter]::ToString($digest.ComputeHash([Text.Encoding]::UTF8.GetBytes($identity)))).Replace('-','').Substring(0,24).ToLowerInvariant()
    $specPath = Join-Path $State "specs\$id.xml"
    $doc = [xml]'<hidden-launch version="1" />'
    $doc.DocumentElement.SetAttribute('isolate-desktop',(-not $NoDesktopIsolation).ToString().ToLowerInvariant())
    $doc.DocumentElement.SetAttribute('log-directory',(Join-Path $State "logs\$id"))
    foreach ($action in $task.Actions) {
        $node = $doc.CreateElement('action')
        $executable = [string]$action.Execute
        # Task Scheduler accepts this built-in bare name. Pin the same Windows
        # binary instead of resolving it through an agent's potentially different PATH.
        if ($executable -ieq 'powershell.exe') {
            $executable = Join-Path ([Environment]::GetFolderPath('System')) 'WindowsPowerShell\v1.0\powershell.exe'
        }
        if (-not [IO.Path]::IsPathRooted($executable)) { throw "Resolve the original action's executable before wrapping: $name" }
        $node.SetAttribute('executable',$executable)
        $node.SetAttribute('arguments',[string]$action.Arguments)
        $node.SetAttribute('directory',[string]$action.WorkingDirectory)
        [void]$doc.DocumentElement.AppendChild($node)
    }
    $reuseSpec = Test-Path -LiteralPath $specPath
    if ($reuseSpec -and ([xml](Get-Content -LiteralPath $specPath -Raw)).DocumentElement.OuterXml -ne $doc.DocumentElement.OuterXml) {
        throw "Existing specification differs from current action; inspect before retry: $specPath"
    }
    if (-not $Apply) {
        $rows += [pscustomobject]@{Task=$name;Result='PLANNED';Actions=@($task.Actions).Count;Isolated=(-not $NoDesktopIsolation)}
        continue
    }
    # Immutable XML backup; never remove tasks, triggers, history or existing files.
    $stamp = [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfffffffZ')
    $backup = Join-Path $State "backups\$id-$stamp.xml"
    [IO.File]::WriteAllText($backup,$xmlBefore,[Text.UTF8Encoding]::new($false))
    if (-not $reuseSpec) { $doc.Save($specPath) }
    # Replace actions only. Principal, triggers, settings and credentials are preserved.
    $newAction = New-ScheduledTaskAction -Execute $launcher -Argument ('"' + $specPath + '"')
    try {
        Set-ScheduledTask -TaskName $name -TaskPath $task.TaskPath -Action $newAction | Out-Null
        $actual = Get-ScheduledTask -TaskName $name -TaskPath $task.TaskPath
        if (@($actual.Actions).Count -ne 1 -or $actual.Actions[0].Execute -ne $launcher -or $actual.Actions[0].Arguments -ne $newAction.Arguments) { throw 'Action verification failed' }
        # Verify the portions unrelated to presentation against the backup.
        $before = [xml]$xmlBefore
        $after = [xml](Export-ScheduledTask -TaskName $name -TaskPath $task.TaskPath)
        foreach ($element in 'Triggers','Principals','Settings') {
            $old = $before.Task.$element.OuterXml
            $current = $after.Task.$element.OuterXml
            if ($old -ne $current) { throw "Unrelated task section changed: $element" }
        }
        $rows += [pscustomobject]@{Task=$name;Result='HIDDEN';Backup=$backup;Spec=$specPath}
    } catch {
        $rows += [pscustomobject]@{Task=$name;Result='FAILED';Error=$_.Exception.GetType().Name;Backup=$backup;Spec=$specPath}
    }
}
$rows | ConvertTo-Json -Depth 5
if (@($rows | Where-Object Result -eq 'FAILED').Count) { exit 1 }
exit 0
