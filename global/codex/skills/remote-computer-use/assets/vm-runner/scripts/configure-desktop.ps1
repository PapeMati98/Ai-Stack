[CmdletBinding(DefaultParameterSetName = 'Enable')]
param(
    [Parameter(ParameterSetName = 'Enable', Mandatory)]
    [switch]$Enable,
    [Parameter(ParameterSetName = 'Enable', Mandatory)]
    [ValidatePattern('^rcu-config-[0-9]{8}T[0-9]{6}Z-[a-f0-9]{8}$')]
    [string]$TransactionId,
    [Parameter(ParameterSetName = 'Enable', Mandatory)]
    [ValidatePattern('^[A-Za-z0-9._-]{1,64}\\[A-Za-z0-9._-]{1,64}$')]
    [string]$DesktopUser,
    [Parameter(ParameterSetName = 'Enable', Mandatory)]
    [ValidatePattern('^rcu-baseline-[0-9]{8}T[0-9]{6}Z$')]
    [string]$RecoveryBaseline,
    [Parameter(ParameterSetName = 'Enable', Mandatory)]
    [ValidatePattern('^[A-Za-z0-9._:-]{8,128}$')]
    [string]$ApprovalReference,
    [Parameter(ParameterSetName = 'Rollback', Mandatory)]
    [ValidatePattern('^rcu-config-[0-9]{8}T[0-9]{6}Z-[a-f0-9]{8}$')]
    [string]$RollbackTransaction,
    [string]$TargetRoot = (Join-Path $env:ProgramData 'MateuszAI\remote-computer-use')
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$Manager = 'remote-computer-use-desktop-config'
$AllowedRoot = [System.IO.Path]::GetFullPath((Join-Path $env:ProgramData 'MateuszAI')).TrimEnd('\')
$TargetRoot = [System.IO.Path]::GetFullPath($TargetRoot).TrimEnd('\')
$StateRoot = Join-Path $AllowedRoot 'remote-computer-use-config'
$TransactionsRoot = Join-Path $StateRoot 'transactions'
$ConfigPath = Join-Path $TargetRoot 'config.json'
$MarkerPath = Join-Path $TargetRoot 'current\.remote-computer-use-managed.json'
$LeasePath = Join-Path $TargetRoot 'state\browser.lease'

function Assert-UnderRoot([string]$Path, [string]$Root, [string]$Label) {
    $full = [System.IO.Path]::GetFullPath($Path)
    $rootFull = [System.IO.Path]::GetFullPath($Root).TrimEnd('\')
    if (-not $full.StartsWith($rootFull + '\', [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "$Label is outside the allowed root: $full"
    }
}

function Write-Utf8NoBom([string]$Path, [string]$Content) {
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Path) | Out-Null
    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, $Content, $encoding)
}

function Get-ConfigFingerprint([string]$Path) {
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Get-DesktopAclSnapshot {
    $paths = @(
        (Join-Path $TargetRoot 'current'),
        $ConfigPath,
        (Join-Path $TargetRoot 'state'),
        (Join-Path $TargetRoot 'artifacts')
    )
    return @($paths | ForEach-Object {
        $acl = Get-Acl -LiteralPath $_
        [ordered]@{
            path = $_
            is_directory = [bool](Test-Path -LiteralPath $_ -PathType Container)
            sddl = $acl.GetSecurityDescriptorSddlForm([System.Security.AccessControl.AccessControlSections]::All)
        }
    })
}

function Restore-DesktopAcl($Snapshot) {
    foreach ($entry in @($Snapshot)) {
        if ([bool]$entry.is_directory) {
            $security = New-Object System.Security.AccessControl.DirectorySecurity
        }
        else {
            $security = New-Object System.Security.AccessControl.FileSecurity
        }
        $security.SetSecurityDescriptorSddlForm(
            [string]$entry.sddl,
            [System.Security.AccessControl.AccessControlSections]::All
        )
        Set-Acl -LiteralPath ([string]$entry.path) -AclObject $security
    }
}

function Grant-DesktopAccess([string]$Identity) {
    $targets = @(
        [ordered]@{ path = (Join-Path $TargetRoot 'current'); rights = 'ReadAndExecute'; inherit = 'ContainerInherit,ObjectInherit' },
        [ordered]@{ path = $ConfigPath; rights = 'Read'; inherit = 'None' },
        [ordered]@{ path = (Join-Path $TargetRoot 'state'); rights = 'ReadAndExecute'; inherit = 'ContainerInherit,ObjectInherit' },
        [ordered]@{ path = (Join-Path $TargetRoot 'artifacts'); rights = 'Modify'; inherit = 'ContainerInherit,ObjectInherit' }
    )
    foreach ($target in $targets) {
        $acl = Get-Acl -LiteralPath ([string]$target.path)
        $rule = New-Object System.Security.AccessControl.FileSystemAccessRule(
            $Identity,
            [string]$target.rights,
            [string]$target.inherit,
            'None',
            'Allow'
        )
        $acl.SetAccessRule($rule)
        Set-Acl -LiteralPath ([string]$target.path) -AclObject $acl
    }
}

function Remove-DesktopTask([string]$TaskName) {
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if (-not $task) { return }
    if ($task.State -eq 'Running') { throw 'Desktop broker task is running' }
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

function Register-DesktopTask($Config) {
    $brokerPath = Join-Path $TargetRoot 'current\scripts\desktop-broker.ps1'
    if (-not (Test-Path -LiteralPath $brokerPath -PathType Leaf)) { throw 'Desktop broker script is missing' }
    $powershellPath = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $action = New-ScheduledTaskAction -Execute $powershellPath `
        -Argument ('-NoLogo -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy RemoteSigned -File "' + $brokerPath + '"')
    $principal = New-ScheduledTaskPrincipal -UserId ([string]$Config.desktop_user) -LogonType Interactive -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes 16) `
        -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
    Register-ScheduledTask -TaskName ([string]$Config.desktop_task_name) `
        -Action $action -Principal $principal -Settings $settings -Force | Out-Null
}

function Assert-IsolatedUser([string]$Identity) {
    if ($Identity.Equals("$env:COMPUTERNAME\Mateusz", [System.StringComparison]::OrdinalIgnoreCase)) {
        throw 'The ambient Mateusz account cannot be used for desktop automation'
    }
    $parts = $Identity.Split('\')
    if (-not $parts[0].Equals($env:COMPUTERNAME, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw 'Desktop automation requires a dedicated local Windows account'
    }
    $localUser = Get-LocalUser -Name $parts[1] -ErrorAction SilentlyContinue
    if (-not $localUser -or -not $localUser.Enabled) { throw 'Configured desktop user is missing or disabled' }
    $administratorGroup = Get-LocalGroup -SID 'S-1-5-32-544' -ErrorAction Stop
    $administrators = @(Get-LocalGroupMember -Group $administratorGroup.Name -ErrorAction Stop)
    if (@($administrators | Where-Object { $_.Name -eq $Identity }).Count -gt 0) {
        throw 'Desktop user must not be a local administrator'
    }
    $profileRoot = Join-Path 'C:\Users' $parts[1]
    $ambientPaths = @(
        (Join-Path $profileRoot 'OneDrive'),
        (Join-Path $profileRoot 'AppData\Local\Google\Chrome\User Data\Default\Login Data'),
        (Join-Path $profileRoot 'AppData\Local\Microsoft\Edge\User Data\Default\Login Data')
    )
    if (@($ambientPaths | Where-Object { Test-Path -LiteralPath $_ }).Count -gt 0) {
        throw 'Desktop user profile contains ambient sync or browser credentials'
    }
    $consoleUser = [string](Get-CimInstance Win32_ComputerSystem -ErrorAction Stop).UserName
    if (-not $consoleUser.Equals($Identity, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw 'Dedicated desktop user is not the active console session'
    }
}

Assert-UnderRoot $TargetRoot $AllowedRoot 'TargetRoot'
New-Item -ItemType Directory -Force -Path $TransactionsRoot | Out-Null
if (-not (Test-Path -LiteralPath $ConfigPath -PathType Leaf) -or -not (Test-Path -LiteralPath $MarkerPath -PathType Leaf)) {
    throw 'Managed remote-computer-use 0.2 deployment is missing'
}
$marker = Get-Content -LiteralPath $MarkerPath -Raw -Encoding UTF8 | ConvertFrom-Json
if ($marker.managed_by -ne 'remote-computer-use-vm-installer' -or $marker.package_version -ne '0.3.0') {
    throw 'Desktop configuration requires the managed 0.3.0 runner'
}
if (Test-Path -LiteralPath $LeasePath -PathType Container) {
    throw 'Refusing desktop configuration while a runner lease is active'
}

if ($PSCmdlet.ParameterSetName -eq 'Rollback') {
    $transactionRoot = Join-Path $TransactionsRoot $RollbackTransaction
    Assert-UnderRoot $transactionRoot $TransactionsRoot 'transaction root'
    $receiptPath = Join-Path $transactionRoot 'receipt.json'
    if (-not (Test-Path -LiteralPath $receiptPath -PathType Leaf)) { throw 'Configuration receipt is missing' }
    $receipt = Get-Content -LiteralPath $receiptPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($receipt.status -ne 'ENABLED' -or $receipt.transaction_id -ne $RollbackTransaction) {
        throw 'Only the matching enabled transaction can be rolled back'
    }
    if (Test-Path -LiteralPath (Join-Path $transactionRoot 'rollback.json') -PathType Leaf) {
        throw 'Configuration transaction has already been rolled back'
    }
    if ((Get-ConfigFingerprint $ConfigPath) -ne [string]$receipt.enabled_config_fingerprint) {
        throw 'Active desktop configuration drifted after enablement'
    }
    $activeConfig = Get-Content -LiteralPath $ConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
    Remove-DesktopTask ([string]$activeConfig.desktop_task_name)
    Copy-Item -LiteralPath (Join-Path $transactionRoot 'prior-config.json') -Destination $ConfigPath -Force
    $priorAcl = Get-Content -LiteralPath (Join-Path $transactionRoot 'prior-acl.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    Restore-DesktopAcl $priorAcl
    $rollback = [ordered]@{
        schema_version = 1
        action = 'rollback-desktop'
        status = 'ROLLED_BACK'
        transaction_id = $RollbackTransaction
        config_fingerprint = Get-ConfigFingerprint $ConfigPath
        rolled_back_at = [DateTime]::UtcNow.ToString('o')
    }
    Write-Utf8NoBom (Join-Path $transactionRoot 'rollback.json') (($rollback | ConvertTo-Json -Depth 6) + "`n")
    Write-Output ($rollback | ConvertTo-Json -Depth 6 -Compress)
    exit 0
}

$transactionRoot = Join-Path $TransactionsRoot $TransactionId
Assert-UnderRoot $transactionRoot $TransactionsRoot 'transaction root'
if (Test-Path -LiteralPath $transactionRoot) { throw 'Configuration transaction already exists' }
New-Item -ItemType Directory -Path $transactionRoot | Out-Null
$config = Get-Content -LiteralPath $ConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
if ($config.schema_version -ne 2 -or $config.runner_version -ne '0.3.0') { throw 'Runner config is not version 0.3.0' }
if ($config.desktop_enabled -isnot [bool]) { throw 'desktop_enabled must be a JSON boolean' }
if ($config.desktop_enabled) { throw 'Desktop mode is already enabled' }
Assert-IsolatedUser $DesktopUser
Copy-Item -LiteralPath $ConfigPath -Destination (Join-Path $transactionRoot 'prior-config.json')
$priorAcl = Get-DesktopAclSnapshot
Write-Utf8NoBom (Join-Path $transactionRoot 'prior-acl.json') (($priorAcl | ConvertTo-Json -Depth 8) + "`n")
$priorFingerprint = Get-ConfigFingerprint $ConfigPath

try {
    $config.desktop_user = $DesktopUser
    $config.desktop_enabled = $true
    Write-Utf8NoBom $ConfigPath (($config | ConvertTo-Json -Depth 12) + "`n")
    Grant-DesktopAccess $DesktopUser
    Register-DesktopTask $config
    $doctorScript = Join-Path $TargetRoot 'current\scripts\invoke-run.ps1'
    $doctorOutput = & powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass `
        -File $doctorScript -Action doctor -RequireMode desktop
    $doctor = $doctorOutput | Select-Object -Last 1 | ConvertFrom-Json
    if ($doctor.status -ne 'READY' -or -not [bool]$doctor.desktop_ready) {
        throw 'Desktop doctor did not return READY after enablement'
    }
    $receipt = [ordered]@{
        schema_version = 1
        managed_by = $Manager
        action = 'enable-desktop'
        status = 'ENABLED'
        transaction_id = $TransactionId
        desktop_user = $DesktopUser
        recovery_baseline = $RecoveryBaseline
        approval_reference = $ApprovalReference
        prior_config_fingerprint = $priorFingerprint
        enabled_config_fingerprint = Get-ConfigFingerprint $ConfigPath
        doctor_status = $doctor.status
        enabled_at = [DateTime]::UtcNow.ToString('o')
    }
    Write-Utf8NoBom (Join-Path $transactionRoot 'receipt.json') (($receipt | ConvertTo-Json -Depth 8) + "`n")
    Write-Output ($receipt | ConvertTo-Json -Depth 8 -Compress)
}
catch {
    $originalError = $_.Exception.Message
    $rollbackErrors = New-Object System.Collections.ArrayList
    try {
        $failedConfig = Get-Content -LiteralPath $ConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
        Remove-DesktopTask ([string]$failedConfig.desktop_task_name)
    }
    catch { [void]$rollbackErrors.Add("task cleanup: $($_.Exception.Message)") }
    try {
        Copy-Item -LiteralPath (Join-Path $transactionRoot 'prior-config.json') -Destination $ConfigPath -Force
    }
    catch { [void]$rollbackErrors.Add("config restore: $($_.Exception.Message)") }
    try { Restore-DesktopAcl $priorAcl }
    catch { [void]$rollbackErrors.Add("ACL restore: $($_.Exception.Message)") }
    $rollbackComplete = $rollbackErrors.Count -eq 0
    $failure = [ordered]@{
        schema_version = 1
        action = 'enable-desktop'
        status = $(if ($rollbackComplete) { 'FAILED_ROLLED_BACK' } else { 'FAILED_ROLLBACK_INCOMPLETE' })
        transaction_id = $TransactionId
        error = $originalError
        rollback_complete = $rollbackComplete
        rollback_errors = @($rollbackErrors)
        failed_at = [DateTime]::UtcNow.ToString('o')
    }
    Write-Utf8NoBom (Join-Path $transactionRoot 'failure.json') (($failure | ConvertTo-Json -Depth 6) + "`n")
    Write-Output ($failure | ConvertTo-Json -Depth 6 -Compress)
    exit $(if ($rollbackComplete) { 1 } else { 2 })
}
