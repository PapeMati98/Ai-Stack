[CmdletBinding(DefaultParameterSetName = 'Deploy')]
param(
    [Parameter(ParameterSetName = 'Deploy', Mandatory)]
    [string]$PackageZip,
    [Parameter(ParameterSetName = 'Deploy', Mandatory)]
    [ValidatePattern('^rcu-[0-9]{8}T[0-9]{6}Z-[a-f0-9]{8}$')]
    [string]$TransactionId,
    [Parameter(ParameterSetName = 'Rollback', Mandatory)]
    [ValidatePattern('^rcu-[0-9]{8}T[0-9]{6}Z-[a-f0-9]{8}$')]
    [string]$RollbackTransaction,
    [Parameter(ParameterSetName = 'Deploy')]
    [switch]$AllowEnabledDesktopUpgrade,
    [Parameter(ParameterSetName = 'Deploy')]
    [switch]$ApplyTemplateConfiguration,
    [string]$TargetRoot = (Join-Path $env:ProgramData 'MateuszAI\remote-computer-use')
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$PackageVersion = '0.3.0'
$Manager = 'remote-computer-use-vm-installer'
$AllowedRoot = [System.IO.Path]::GetFullPath((Join-Path $env:ProgramData 'MateuszAI')).TrimEnd('\')
$TargetRoot = [System.IO.Path]::GetFullPath($TargetRoot).TrimEnd('\')
$StateRoot = Join-Path $AllowedRoot 'remote-computer-use-deploy'
$TransactionsRoot = Join-Path $StateRoot 'transactions'

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

function Get-PayloadRows([string]$Root) {
    $resolved = (Get-Item -LiteralPath $Root).FullName.TrimEnd('\')
    $rows = @()
    foreach ($file in Get-ChildItem -LiteralPath $resolved -File -Recurse -Force) {
        $relative = $file.FullName.Substring($resolved.Length).TrimStart('\').Replace('\', '/')
        if ($relative -eq 'release.manifest.json' -or $relative -eq '.remote-computer-use-managed.json') { continue }
        if ($relative -like 'node_modules/*') { continue }
        $hash = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
        $rows += "$relative|$($file.Length)|$hash"
    }
    $ordered = [string[]]@($rows)
    [Array]::Sort($ordered, [System.StringComparer]::Ordinal)
    return @($ordered)
}

function Get-PayloadFingerprint([string]$Root) {
    $joined = (Get-PayloadRows $Root) -join "`n"
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($joined)
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($sha.ComputeHash($bytes))).Replace('-', '').ToLowerInvariant() }
    finally { $sha.Dispose() }
}

function Grant-DesktopAccess([string]$ManagedRoot, [string]$Identity) {
    $targets = @(
        [ordered]@{ path = (Join-Path $ManagedRoot 'current'); rights = 'ReadAndExecute'; inherit = 'ContainerInherit,ObjectInherit' },
        [ordered]@{ path = (Join-Path $ManagedRoot 'config.json'); rights = 'Read'; inherit = 'None' },
        [ordered]@{ path = (Join-Path $ManagedRoot 'state'); rights = 'ReadAndExecute'; inherit = 'ContainerInherit,ObjectInherit' },
        [ordered]@{ path = (Join-Path $ManagedRoot 'artifacts'); rights = 'Modify'; inherit = 'ContainerInherit,ObjectInherit' }
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

function Sync-DesktopTask([string]$ManagedRoot) {
    $configPath = Join-Path $ManagedRoot 'config.json'
    $currentRoot = Join-Path $ManagedRoot 'current'
    if (-not (Test-Path -LiteralPath $configPath -PathType Leaf)) { return 'ABSENT' }
    $config = Get-Content -LiteralPath $configPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $taskName = $(if ($config.PSObject.Properties.Name -contains 'desktop_task_name') {
        [string]$config.desktop_task_name
    } else {
        'MateuszAI-RemoteComputerUseDesktop'
    })
    $existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if (-not ($config.PSObject.Properties.Name -contains 'desktop_enabled')) {
        if ($existing) {
            if ($existing.State -eq 'Running') { throw 'Refusing to remove a running desktop broker task' }
            Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
        }
        return 'DISABLED'
    }
    if ($config.desktop_enabled -isnot [bool]) { throw 'desktop_enabled must be a JSON boolean' }
    if (-not $config.desktop_enabled) {
        if ($existing) {
            if ($existing.State -eq 'Running') { throw 'Refusing to remove a running desktop broker task' }
            Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
        }
        return 'DISABLED'
    }
    $brokerPath = Join-Path $currentRoot 'scripts\desktop-broker.ps1'
    if (-not (Test-Path -LiteralPath $brokerPath -PathType Leaf)) {
        throw 'Desktop broker script is missing'
    }
    $userName = [string]$config.desktop_user
    $localName = $userName.Split('\')[-1]
    if (-not (Get-LocalUser -Name $localName -ErrorAction SilentlyContinue)) {
        throw "Configured desktop user does not exist: $userName"
    }
    Grant-DesktopAccess $ManagedRoot $userName
    $powershellPath = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $action = New-ScheduledTaskAction -Execute $powershellPath `
        -Argument ('-NoLogo -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy RemoteSigned -File "' + $brokerPath + '"')
    $principal = New-ScheduledTaskPrincipal -UserId $userName -LogonType Interactive -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes 16) `
        -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
    Register-ScheduledTask -TaskName $taskName -Action $action -Principal $principal -Settings $settings -Force | Out-Null
    return 'REGISTERED'
}

Assert-UnderRoot $TargetRoot $AllowedRoot 'TargetRoot'
New-Item -ItemType Directory -Force -Path $TransactionsRoot | Out-Null

if ($PSCmdlet.ParameterSetName -eq 'Rollback') {
    $transactionRoot = Join-Path $TransactionsRoot $RollbackTransaction
    Assert-UnderRoot $transactionRoot $TransactionsRoot 'transaction root'
    $receiptPath = Join-Path $transactionRoot 'receipt.json'
    if (-not (Test-Path -LiteralPath $receiptPath -PathType Leaf)) {
        throw "Rollback receipt not found: $receiptPath"
    }
    $receipt = Get-Content -LiteralPath $receiptPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($receipt.status -ne 'DEPLOYED' -or $receipt.transaction_id -ne $RollbackTransaction) {
        throw 'Only the matching DEPLOYED transaction can be rolled back'
    }
    if (Test-Path -LiteralPath (Join-Path $transactionRoot 'rollback.json') -PathType Leaf) {
        throw 'This deployment transaction has already been rolled back'
    }
    if (Test-Path -LiteralPath $TargetRoot -PathType Container) {
        if (Test-Path -LiteralPath (Join-Path $TargetRoot 'state\browser.lease') -PathType Container) {
            throw 'Refusing rollback while the browser lease is active'
        }
        $markerPath = Join-Path $TargetRoot 'current\.remote-computer-use-managed.json'
        if (-not (Test-Path -LiteralPath $markerPath -PathType Leaf)) {
            throw 'Refusing rollback because the active deployment marker is missing'
        }
        $marker = Get-Content -LiteralPath $markerPath -Raw -Encoding UTF8 | ConvertFrom-Json
        $hasConfigFingerprint = (
            $receipt.PSObject.Properties.Name -contains 'config_fingerprint' -and
            -not [string]::IsNullOrWhiteSpace([string]$receipt.config_fingerprint)
        )
        if (
            $marker.managed_by -ne $Manager -or
            $marker.transaction_id -ne $RollbackTransaction -or
            $marker.payload_fingerprint -ne $receipt.payload_fingerprint -or
            ($hasConfigFingerprint -and $marker.config_fingerprint -ne $receipt.config_fingerprint)
        ) {
            throw 'Refusing rollback because the active deployment identity drifted'
        }
        $actual = Get-PayloadFingerprint (Join-Path $TargetRoot 'current')
        if ($actual -ne $receipt.payload_fingerprint) {
            throw 'Refusing rollback because the active payload drifted after deployment'
        }
        if ($hasConfigFingerprint) {
            $activeConfig = Join-Path $TargetRoot 'config.json'
            if (-not (Test-Path -LiteralPath $activeConfig -PathType Leaf)) {
                throw 'Refusing rollback because the active configuration is missing'
            }
            $actualConfigFingerprint = (Get-FileHash -LiteralPath $activeConfig -Algorithm SHA256).Hash.ToLowerInvariant()
            if ($actualConfigFingerprint -ne $receipt.config_fingerprint) {
                throw 'Refusing rollback because the active configuration drifted after deployment'
            }
        }
        $activeConfigObject = Get-Content -LiteralPath (Join-Path $TargetRoot 'config.json') -Raw -Encoding UTF8 | ConvertFrom-Json
        $activeTaskName = $(if ($activeConfigObject.PSObject.Properties.Name -contains 'desktop_task_name') {
            [string]$activeConfigObject.desktop_task_name
        } else { 'MateuszAI-RemoteComputerUseDesktop' })
        $activeTask = Get-ScheduledTask -TaskName $activeTaskName -ErrorAction SilentlyContinue
        if ($activeTask -and $activeTask.State -eq 'Running') {
            throw 'Refusing rollback while the desktop broker task is running'
        }
        if ($activeTask) {
            Unregister-ScheduledTask -TaskName $activeTaskName -Confirm:$false
        }
        $preserved = Join-Path $transactionRoot 'deployed-state-at-rollback'
        if (Test-Path -LiteralPath $preserved) {
            throw "Rollback preservation path already exists: $preserved"
        }
        Move-Item -LiteralPath $TargetRoot -Destination $preserved
    }
    $prior = Join-Path $transactionRoot 'prior-target'
    if ([bool]$receipt.target_existed_before) {
        if (-not (Test-Path -LiteralPath $prior -PathType Container)) {
            throw 'Prior target backup is missing'
        }
        Move-Item -LiteralPath $prior -Destination $TargetRoot
    }
    $restoredDesktopTask = $(if (Test-Path -LiteralPath $TargetRoot -PathType Container) {
        Sync-DesktopTask $TargetRoot
    } else { 'ABSENT' })
    $rollback = [ordered]@{
        schema_version = 1
        action = 'rollback'
        status = 'ROLLED_BACK'
        transaction_id = $RollbackTransaction
        restored_prior_target = [bool]$receipt.target_existed_before
        target_exists_after = $(Test-Path -LiteralPath $TargetRoot)
        desktop_task = $restoredDesktopTask
        rolled_back_at = [DateTime]::UtcNow.ToString('o')
    }
    Write-Utf8NoBom (Join-Path $transactionRoot 'rollback.json') (($rollback | ConvertTo-Json -Depth 8) + "`n")
    Write-Output ($rollback | ConvertTo-Json -Depth 8 -Compress)
    exit 0
}

if (-not (Test-Path -LiteralPath $PackageZip -PathType Leaf)) {
    throw "Package zip not found: $PackageZip"
}
$transactionRoot = Join-Path $TransactionsRoot $TransactionId
Assert-UnderRoot $transactionRoot $TransactionsRoot 'transaction root'
if (Test-Path -LiteralPath $transactionRoot) {
    throw "Transaction already exists: $TransactionId"
}
New-Item -ItemType Directory -Force -Path $transactionRoot | Out-Null
$staging = Join-Path $transactionRoot 'staging'
New-Item -ItemType Directory -Force -Path $staging | Out-Null
Expand-Archive -LiteralPath $PackageZip -DestinationPath $staging -Force

$manifestPath = Join-Path $staging 'release.manifest.json'
if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
    throw 'Release manifest is missing from the package'
}
$manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
if ($manifest.package_version -ne $PackageVersion -or $manifest.managed_by -ne $Manager) {
    throw 'Release manifest identity mismatch'
}
$fingerprint = Get-PayloadFingerprint $staging
if ($fingerprint -ne $manifest.payload_fingerprint) {
    throw 'Release payload fingerprint mismatch'
}

$npmStdout = Join-Path $transactionRoot 'npm.stdout.log'
$npmStderr = Join-Path $transactionRoot 'npm.stderr.log'
$npm = Start-Process -FilePath 'npm.cmd' -ArgumentList @('ci', '--omit=dev', '--ignore-scripts') `
    -WorkingDirectory $staging -NoNewWindow -Wait -PassThru `
    -RedirectStandardOutput $npmStdout -RedirectStandardError $npmStderr
if ($npm.ExitCode -ne 0) {
    throw "npm ci failed with exit code $($npm.ExitCode)"
}

$configExample = Join-Path $staging 'config.example.json'
if (-not (Test-Path -LiteralPath $configExample -PathType Leaf)) {
    throw 'Runner configuration template is missing'
}
$browser = (Get-Content -LiteralPath $configExample -Raw -Encoding UTF8 | ConvertFrom-Json).browser_executable
if (-not (Test-Path -LiteralPath $browser -PathType Leaf)) {
    throw "Configured browser does not exist: $browser"
}

$targetExisted = Test-Path -LiteralPath $TargetRoot -PathType Container
if ($targetExisted) {
    if (Test-Path -LiteralPath (Join-Path $TargetRoot 'state\browser.lease') -PathType Container) {
        throw 'Refusing deployment while the browser lease is active'
    }
    $existingConfigPath = Join-Path $TargetRoot 'config.json'
    if (-not (Test-Path -LiteralPath $existingConfigPath -PathType Leaf)) {
        throw 'Refusing deployment because the existing configuration is missing'
    }
    $existingConfig = Get-Content -LiteralPath $existingConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($existingConfig.PSObject.Properties.Name -contains 'desktop_enabled') {
        if ($existingConfig.desktop_enabled -isnot [bool]) { throw 'Existing desktop_enabled must be a JSON boolean' }
        if ($existingConfig.desktop_enabled -and -not $AllowEnabledDesktopUpgrade) {
            throw 'Refusing to upgrade an enabled desktop broker without AllowEnabledDesktopUpgrade'
        }
    }
    if (
        [bool]$existingConfig.allow_network -or
        @($existingConfig.allowed_origins).Count -gt 0 -or
        [bool]$existingConfig.capture_console_text
    ) {
        throw 'Refusing to preserve an expanded runner configuration without a separate reviewed configuration transaction'
    }
    $existingTaskName = $(if ($existingConfig.PSObject.Properties.Name -contains 'desktop_task_name') {
        [string]$existingConfig.desktop_task_name
    } else { 'MateuszAI-RemoteComputerUseDesktop' })
    $existingTask = Get-ScheduledTask -TaskName $existingTaskName -ErrorAction SilentlyContinue
    if ($existingTask -and $existingTask.State -eq 'Running') {
        throw 'Refusing deployment while the desktop broker task is running'
    }
    Move-Item -LiteralPath $TargetRoot -Destination (Join-Path $transactionRoot 'prior-target')
}
try {
    New-Item -ItemType Directory -Force -Path $TargetRoot | Out-Null
    Move-Item -LiteralPath $staging -Destination (Join-Path $TargetRoot 'current')
    $priorConfig = Join-Path $transactionRoot 'prior-target\config.json'
    $configPreserved = Test-Path -LiteralPath $priorConfig -PathType Leaf
    $templateConfig = Get-Content -LiteralPath (Join-Path $TargetRoot 'current\config.example.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($configPreserved -and -not $ApplyTemplateConfiguration) {
        $priorConfigObject = Get-Content -LiteralPath $priorConfig -Raw -Encoding UTF8 | ConvertFrom-Json
        if (
            $priorConfigObject.schema_version -eq 2 -and
            [string]$priorConfigObject.runner_version -in @('0.2.1', $PackageVersion)
        ) {
            if ($priorConfigObject.desktop_enabled -isnot [bool]) {
                throw 'Existing desktop_enabled must be a JSON boolean'
            }
            $deployedConfig = [ordered]@{
                schema_version = 2
                runner_version = $PackageVersion
                browser_executable = [string]$priorConfigObject.browser_executable
                allow_network = [bool]$priorConfigObject.allow_network
                allowed_origins = @($priorConfigObject.allowed_origins)
                capture_console_text = [bool]$priorConfigObject.capture_console_text
                max_timeout_seconds = [int]$priorConfigObject.max_timeout_seconds
                max_actions = [int]$priorConfigObject.max_actions
                desktop_enabled = [bool]$priorConfigObject.desktop_enabled
                desktop_task_name = [string]$priorConfigObject.desktop_task_name
                desktop_user = [string]$priorConfigObject.desktop_user
                desktop_max_text_length = [int]$priorConfigObject.desktop_max_text_length
                desktop_allowed_apps = @($priorConfigObject.desktop_allowed_apps)
            }
        }
        else {
            $deployedConfig = [ordered]@{
                schema_version = 2
                runner_version = $PackageVersion
                browser_executable = [string]$priorConfigObject.browser_executable
                allow_network = [bool]$priorConfigObject.allow_network
                allowed_origins = @($priorConfigObject.allowed_origins)
                capture_console_text = [bool]$priorConfigObject.capture_console_text
                max_timeout_seconds = [int]$priorConfigObject.max_timeout_seconds
                max_actions = [int]$priorConfigObject.max_actions
                desktop_enabled = $false
                desktop_task_name = [string]$templateConfig.desktop_task_name
                desktop_user = [string]$templateConfig.desktop_user
                desktop_max_text_length = [int]$templateConfig.desktop_max_text_length
                desktop_allowed_apps = @($templateConfig.desktop_allowed_apps)
            }
        }
    }
    else {
        $deployedConfig = $templateConfig
    }
    Write-Utf8NoBom (Join-Path $TargetRoot 'config.json') (($deployedConfig | ConvertTo-Json -Depth 12) + "`n")
    $configFingerprint = (Get-FileHash -LiteralPath (Join-Path $TargetRoot 'config.json') -Algorithm SHA256).Hash.ToLowerInvariant()
    New-Item -ItemType Directory -Force -Path `
        (Join-Path $TargetRoot 'inbox'), `
        (Join-Path $TargetRoot 'state'), `
        (Join-Path $TargetRoot 'artifacts') | Out-Null
    $marker = [ordered]@{
        schema_version = 1
        managed_by = $Manager
        package_version = $PackageVersion
        transaction_id = $TransactionId
        payload_fingerprint = $fingerprint
        config_fingerprint = $configFingerprint
        deployed_at = [DateTime]::UtcNow.ToString('o')
    }
    Write-Utf8NoBom (Join-Path $TargetRoot 'current\.remote-computer-use-managed.json') (($marker | ConvertTo-Json -Depth 6) + "`n")

    $desktopTaskStatus = Sync-DesktopTask $TargetRoot

    $doctorScript = Join-Path $TargetRoot 'current\scripts\invoke-run.ps1'
    $doctorOutput = & powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass `
        -File $doctorScript -Action doctor
    $doctor = $doctorOutput | Select-Object -Last 1 | ConvertFrom-Json
    if ($doctor.status -ne 'READY') {
        throw 'Installed runner doctor did not return READY'
    }

    $receipt = [ordered]@{
        schema_version = 1
        action = 'deploy'
        status = 'DEPLOYED'
        managed_by = $Manager
        package_version = $PackageVersion
        transaction_id = $TransactionId
        payload_fingerprint = $fingerprint
        config_fingerprint = $configFingerprint
        target_root = $TargetRoot
        target_existed_before = [bool]$targetExisted
        config_preserved = [bool]$configPreserved
        config_source = $(if ($ApplyTemplateConfiguration -or -not $configPreserved) { 'template' } else { 'prior' })
        desktop_task = $desktopTaskStatus
        doctor_status = $doctor.status
        deployed_at = [DateTime]::UtcNow.ToString('o')
    }
    Write-Utf8NoBom (Join-Path $transactionRoot 'receipt.json') (($receipt | ConvertTo-Json -Depth 8) + "`n")
    Write-Output ($receipt | ConvertTo-Json -Depth 8 -Compress)
}
catch {
    $originalError = $_.Exception.Message
    $rollbackErrors = New-Object System.Collections.ArrayList
    try {
        if (Test-Path -LiteralPath $TargetRoot) {
            Remove-Item -LiteralPath $TargetRoot -Recurse -Force
        }
    }
    catch { [void]$rollbackErrors.Add("target cleanup: $($_.Exception.Message)") }
    $prior = Join-Path $transactionRoot 'prior-target'
    try {
        if (Test-Path -LiteralPath $prior) {
            Move-Item -LiteralPath $prior -Destination $TargetRoot
        }
        if (Test-Path -LiteralPath $TargetRoot -PathType Container) {
            [void](Sync-DesktopTask $TargetRoot)
        }
        else {
            $strayTask = Get-ScheduledTask -TaskName 'MateuszAI-RemoteComputerUseDesktop' -ErrorAction SilentlyContinue
            if ($strayTask -and $strayTask.State -ne 'Running') {
                Unregister-ScheduledTask -TaskName 'MateuszAI-RemoteComputerUseDesktop' -Confirm:$false
            }
        }
    }
    catch { [void]$rollbackErrors.Add("state or task restore: $($_.Exception.Message)") }
    $rollbackComplete = $rollbackErrors.Count -eq 0
    $failure = [ordered]@{
        schema_version = 1
        action = 'deploy'
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
