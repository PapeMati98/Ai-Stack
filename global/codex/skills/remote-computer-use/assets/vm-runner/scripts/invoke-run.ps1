[CmdletBinding()]
param(
    [ValidateSet('doctor', 'run', 'recover', 'reserve-origin-uat', 'release-origin-uat', 'origin-listener')]
    [string]$Action = 'doctor',
    [string]$ContractPath,
    [ValidatePattern('^[a-z0-9][a-z0-9-]{2,63}$')]
    [string]$TaskId,
    [ValidatePattern('^uat-[a-f0-9]{32}$')]
    [string]$ReservationId,
    [ValidateSet('any', 'browser', 'desktop')]
    [string]$RequireMode = 'any',
    [ValidateRange(1, 1440)]
    [int]$MaxAgeMinutes = 20
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$CurrentRoot = Split-Path -Parent $PSScriptRoot
$Root = Split-Path -Parent $CurrentRoot
$ConfigPath = Join-Path $Root 'config.json'
$StateRoot = Join-Path $Root 'state'
$InboxRoot = Join-Path $Root 'inbox'
$ArtifactRoot = Join-Path $Root 'artifacts'
$LeasePath = Join-Path $StateRoot 'browser.lease'
$LeaseMetadataPath = Join-Path $LeasePath 'lease.json'
$RunnerPath = Join-Path $CurrentRoot 'src\runner.mjs'
$DoctorPath = Join-Path $CurrentRoot 'src\doctor.mjs'
$ContractValidatorPath = Join-Path $CurrentRoot 'src\validate-contract.mjs'
$DesktopBrokerPath = Join-Path $CurrentRoot 'scripts\desktop-broker.ps1'
$PackagePath = Join-Path $CurrentRoot 'node_modules\playwright-core\package.json'
$RunnerVersion = '0.3.0'

function Write-JsonResult($Value, [int]$ExitCode = 0) {
    Write-Output ($Value | ConvertTo-Json -Depth 12 -Compress)
    exit $ExitCode
}

function Write-Utf8NoBom([string]$Path, [string]$Content) {
    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, $Content, $encoding)
}

function Initialize-KillOnCloseJobType {
    if ('RemoteComputerUse.NativeJob' -as [type]) { return }
    Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;

namespace RemoteComputerUse {
    [StructLayout(LayoutKind.Sequential)]
    public struct IO_COUNTERS {
        public ulong ReadOperationCount;
        public ulong WriteOperationCount;
        public ulong OtherOperationCount;
        public ulong ReadTransferCount;
        public ulong WriteTransferCount;
        public ulong OtherTransferCount;
    }

    [StructLayout(LayoutKind.Sequential)]
    public struct JOBOBJECT_BASIC_LIMIT_INFORMATION {
        public long PerProcessUserTimeLimit;
        public long PerJobUserTimeLimit;
        public uint LimitFlags;
        public UIntPtr MinimumWorkingSetSize;
        public UIntPtr MaximumWorkingSetSize;
        public uint ActiveProcessLimit;
        public UIntPtr Affinity;
        public uint PriorityClass;
        public uint SchedulingClass;
    }

    [StructLayout(LayoutKind.Sequential)]
    public struct JOBOBJECT_EXTENDED_LIMIT_INFORMATION {
        public JOBOBJECT_BASIC_LIMIT_INFORMATION BasicLimitInformation;
        public IO_COUNTERS IoInfo;
        public UIntPtr ProcessMemoryLimit;
        public UIntPtr JobMemoryLimit;
        public UIntPtr PeakProcessMemoryUsed;
        public UIntPtr PeakJobMemoryUsed;
    }

    public static class NativeJob {
        private const uint JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000;

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern IntPtr CreateJobObject(IntPtr attributes, string name);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool SetInformationJobObject(
            IntPtr job,
            int infoClass,
            IntPtr info,
            uint infoLength
        );

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool CloseHandle(IntPtr handle);

        public static IntPtr CreateKillOnClose() {
            IntPtr job = CreateJobObject(IntPtr.Zero, null);
            if (job == IntPtr.Zero) {
                throw new Win32Exception(Marshal.GetLastWin32Error(), "CreateJobObject failed");
            }
            var limits = new JOBOBJECT_EXTENDED_LIMIT_INFORMATION();
            limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
            int length = Marshal.SizeOf(typeof(JOBOBJECT_EXTENDED_LIMIT_INFORMATION));
            IntPtr buffer = Marshal.AllocHGlobal(length);
            try {
                Marshal.StructureToPtr(limits, buffer, false);
                if (!SetInformationJobObject(job, 9, buffer, (uint)length)) {
                    throw new Win32Exception(Marshal.GetLastWin32Error(), "SetInformationJobObject failed");
                }
                return job;
            } catch {
                CloseHandle(job);
                throw;
            } finally {
                Marshal.FreeHGlobal(buffer);
            }
        }

        public static void Assign(IntPtr job, IntPtr process) {
            if (!AssignProcessToJobObject(job, process)) {
                throw new Win32Exception(Marshal.GetLastWin32Error(), "AssignProcessToJobObject failed");
            }
        }

        public static void Close(IntPtr job) {
            if (job != IntPtr.Zero) {
                CloseHandle(job);
            }
        }
    }
}
'@
}

function Test-PathUnderRoot([string]$Path, [string]$ExpectedRoot) {
    $fullPath = [System.IO.Path]::GetFullPath($Path)
    $fullRoot = [System.IO.Path]::GetFullPath($ExpectedRoot).TrimEnd('\')
    return $fullPath.StartsWith($fullRoot + '\', [System.StringComparison]::OrdinalIgnoreCase)
}

function Test-ExactJsonTrue($Value) {
    return ($Value -is [bool] -and $Value -eq $true)
}

function Get-LeaseState {
    if (-not (Test-Path -LiteralPath $LeasePath -PathType Container)) {
        return [ordered]@{ status = 'FREE'; metadata = $null }
    }
    $metadata = $null
    if (Test-Path -LiteralPath $LeaseMetadataPath -PathType Leaf) {
        try { $metadata = Get-Content -LiteralPath $LeaseMetadataPath -Raw -Encoding UTF8 | ConvertFrom-Json }
        catch { $metadata = $null }
    }
    return [ordered]@{ status = 'HELD'; metadata = $metadata }
}

function Get-ManagedDesktopBrokerProcesses {
    $needle = [System.IO.Path]::GetFullPath($DesktopBrokerPath)
    return @(
        Get-CimInstance Win32_Process -Filter "Name = 'powershell.exe'" -ErrorAction SilentlyContinue |
            Where-Object {
                $_.CommandLine -and
                $_.CommandLine.IndexOf($needle, [System.StringComparison]::OrdinalIgnoreCase) -ge 0
            }
    )
}

function Get-OriginUatEndpoint {
    $config = Get-Content -LiteralPath $ConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if (
        -not (Test-ExactJsonTrue $config.allow_network) -or
        @($config.allowed_origins).Count -ne 1 -or
        [bool]$config.capture_console_text
    ) {
        throw 'Origin UAT requires one allowed origin and disabled console capture'
    }
    $origin = [Uri]([string]$config.allowed_origins[0])
    if (
        $origin.Scheme -ne 'http' -or
        $origin.Host -ne '127.0.0.1' -or
        $origin.Port -ne 13102 -or
        $origin.AbsolutePath -ne '/'
    ) {
        throw 'Origin UAT is pinned to http://127.0.0.1:13102'
    }
    return $origin
}

if ($Action -eq 'doctor') {
    $config = $null
    try { $config = Get-Content -LiteralPath $ConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json }
    catch { }
    $allowNetworkEnabled = $config -and (Test-ExactJsonTrue $config.allow_network)
    $desktopEnabled = $config -and (Test-ExactJsonTrue $config.desktop_enabled)
    $node = Get-Command node.exe -ErrorAction SilentlyContinue
    $policyValid = $false
    if ($node -and (Test-Path -LiteralPath $DoctorPath -PathType Leaf)) {
        $doctorOutput = & $node.Source $DoctorPath $ConfigPath 2>$null
        $doctorExit = $LASTEXITCODE
        if ($doctorExit -eq 0) {
            try { $policyValid = (($doctorOutput | Select-Object -Last 1 | ConvertFrom-Json).status -eq 'VALID') }
            catch { $policyValid = $false }
        }
    }
    $browserExists = $config -and $config.browser_executable -and (Test-Path -LiteralPath $config.browser_executable -PathType Leaf)
    $browserReady = [bool](
        $node -and
        (Test-Path -LiteralPath $RunnerPath -PathType Leaf) -and
        (Test-Path -LiteralPath $DoctorPath -PathType Leaf) -and
        (Test-Path -LiteralPath $ContractValidatorPath -PathType Leaf) -and
        (Test-Path -LiteralPath $PackagePath -PathType Leaf) -and
        $config -and
        $policyValid -and
        $browserExists
    )
    $desktopTask = $null
    $consoleUser = $null
    if ($config -and $config.desktop_task_name) {
        $desktopTask = Get-ScheduledTask -TaskName ([string]$config.desktop_task_name) -ErrorAction SilentlyContinue
        try { $consoleUser = [string](Get-CimInstance Win32_ComputerSystem -ErrorAction Stop).UserName }
        catch { $consoleUser = $null }
    }
    $desktopIdentityReady = [bool](
        $config -and
        $consoleUser -and
        $consoleUser.Equals([string]$config.desktop_user, [System.StringComparison]::OrdinalIgnoreCase)
    )
    $expectedTaskExecute = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $expectedTaskArguments = '-NoLogo -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy RemoteSigned -File "' + $DesktopBrokerPath + '"'
    $taskAction = $(if ($desktopTask) { @($desktopTask.Actions) | Select-Object -First 1 } else { $null })
    $desktopActionReady = [bool](
        $taskAction -and
        [string]$taskAction.Execute -eq $expectedTaskExecute -and
        [string]$taskAction.Arguments -eq $expectedTaskArguments
    )
    $configuredDesktopUser = $(if ($config) { [string]$config.desktop_user } else { '' })
    $desktopUserParts = @($configuredDesktopUser.Split('\'))
    $desktopTaskUser = $(if ($desktopTask) { [string]$desktopTask.Principal.UserId } else { '' })
    $desktopTaskUserReady = [bool](
        $desktopTask -and
        (
            $desktopTaskUser.Equals($configuredDesktopUser, [System.StringComparison]::OrdinalIgnoreCase) -or
            (
                $desktopUserParts.Count -eq 2 -and
                $desktopUserParts[0].Equals($env:COMPUTERNAME, [System.StringComparison]::OrdinalIgnoreCase) -and
                $desktopTaskUser.Equals($desktopUserParts[1], [System.StringComparison]::OrdinalIgnoreCase)
            )
        )
    )
    $desktopLogonType = $(if ($desktopTask) { [string]$desktopTask.Principal.LogonType } else { $null })
    $desktopTaskReady = [bool](
        $desktopTask -and
        $desktopTaskUserReady -and
        $desktopLogonType -in @('Interactive', 'InteractiveToken') -and
        [string]$desktopTask.Principal.RunLevel -eq 'Limited' -and
        $desktopActionReady
    )
    $desktopReady = [bool](
        $browserReady -and
        $config -and
        $desktopEnabled -and
        (Test-Path -LiteralPath $DesktopBrokerPath -PathType Leaf) -and
        $desktopTaskReady -and
        $desktopIdentityReady
    )
    $requiredReady = $(if ($RequireMode -eq 'desktop') { $desktopReady } else { $browserReady })
    Write-JsonResult ([ordered]@{
        action = 'doctor'
        status = $(if ($requiredReady) { 'READY' } else { 'NOT_READY' })
        runner_version = $RunnerVersion
        root = $Root
        node = $(if ($node) { $node.Source } else { $null })
        browser = $(if ($config) { $config.browser_executable } else { $null })
        browser_exists = [bool]$browserExists
        policy_valid = [bool]$policyValid
        browser_ready = [bool]$browserReady
        allow_network = [bool]$allowNetworkEnabled
        allowed_origin_count = $(if ($config -and $config.allowed_origins) { @($config.allowed_origins).Count } else { 0 })
        desktop_enabled = [bool]$desktopEnabled
        desktop_ready = [bool]$desktopReady
        desktop_status = $(
            if (-not $desktopEnabled) { 'DISABLED' }
            elseif (-not $desktopTaskReady) { 'TASK_NOT_READY' }
            elseif (-not $desktopIdentityReady) { 'WAITING_FOR_INTERACTIVE_SESSION' }
            else { 'READY' }
        )
        desktop_user = $(if ($config) { [string]$config.desktop_user } else { $null })
        interactive_user = $consoleUser
        lease = Get-LeaseState
    }) $(if ($requiredReady) { 0 } else { 3 })
}

if ($Action -eq 'reserve-origin-uat') {
    if (-not $TaskId -or -not $ReservationId) {
        throw 'TaskId and ReservationId are required for origin UAT reservation'
    }
    [void](Get-OriginUatEndpoint)
    New-Item -ItemType Directory -Force -Path $StateRoot | Out-Null
    try {
        New-Item -ItemType Directory -Path $LeasePath -ErrorAction Stop | Out-Null
    }
    catch {
        Write-JsonResult ([ordered]@{
            action = 'reserve-origin-uat'
            status = 'BUSY'
            task_id = $TaskId
            lease = Get-LeaseState
        }) 4
    }
    try {
        Write-Utf8NoBom $LeaseMetadataPath (([ordered]@{
            schema_version = 3
            mode = 'browser-origin-uat-reserved'
            task_id = $TaskId
            reservation_id = $ReservationId
            process_id = 0
            reserved_at = (Get-Date).ToString('o')
        } | ConvertTo-Json -Depth 6) + "`n")
        Write-JsonResult ([ordered]@{
            action = 'reserve-origin-uat'
            status = 'RESERVED'
            task_id = $TaskId
            reservation_id = $ReservationId
            lease_status = 'HELD'
        })
    }
    catch {
        Remove-Item -LiteralPath $LeasePath -Recurse -Force -ErrorAction SilentlyContinue
        throw
    }
}

if ($Action -eq 'release-origin-uat') {
    if (-not $TaskId -or -not $ReservationId) {
        throw 'TaskId and ReservationId are required for origin UAT release'
    }
    $lease = Get-LeaseState
    if ($lease.status -eq 'FREE') {
        Write-JsonResult ([ordered]@{
            action = 'release-origin-uat'
            status = 'NOOP'
            task_id = $TaskId
        })
    }
    if (
        -not $lease.metadata -or
        [string]$lease.metadata.mode -ne 'browser-origin-uat-reserved' -or
        [string]$lease.metadata.task_id -ne $TaskId -or
        [string]$lease.metadata.reservation_id -ne $ReservationId -or
        [int]$lease.metadata.process_id -ne 0
    ) {
        Write-JsonResult ([ordered]@{
            action = 'release-origin-uat'
            status = 'BLOCKED'
            reason = 'reservation identity does not match the held lease'
        }) 4
    }
    $origin = Get-OriginUatEndpoint
    $originListeners = @(
        Get-NetTCPConnection -State Listen -LocalPort $origin.Port -ErrorAction SilentlyContinue
    )
    if ($originListeners.Count -gt 0) {
        Write-JsonResult ([ordered]@{
            action = 'release-origin-uat'
            status = 'BLOCKED'
            reason = 'origin UAT listener is still active'
            listen_count = $originListeners.Count
        }) 4
    }
    Remove-Item -LiteralPath $LeasePath -Recurse -Force
    Write-JsonResult ([ordered]@{
        action = 'release-origin-uat'
        status = 'RELEASED'
        task_id = $TaskId
    })
}

if ($Action -eq 'origin-listener') {
    $origin = Get-OriginUatEndpoint
    $rows = @(
        Get-NetTCPConnection -State Listen -LocalPort $origin.Port -ErrorAction SilentlyContinue |
            Select-Object LocalAddress, LocalPort, OwningProcess
    )
    $unsafe = @($rows | Where-Object { $_.LocalAddress -ne '127.0.0.1' })
    if ($unsafe.Count -gt 0) {
        Write-JsonResult ([ordered]@{
            action = 'origin-listener'
            status = 'UNSAFE'
            port = $origin.Port
            addresses = @($rows.LocalAddress | Sort-Object -Unique)
        }) 3
    }
    $loopback = @($rows | Where-Object { $_.LocalAddress -eq '127.0.0.1' })
    Write-JsonResult ([ordered]@{
        action = 'origin-listener'
        status = $(if ($loopback.Count -gt 0) { 'READY' } else { 'CLOSED' })
        port = $origin.Port
        addresses = @($rows.LocalAddress | Sort-Object -Unique)
        listen_count = $rows.Count
    }) $(if ($loopback.Count -gt 0) { 0 } else { 3 })
}

if ($Action -eq 'recover') {
    $lease = Get-LeaseState
    if ($lease.status -eq 'FREE') {
        Write-JsonResult ([ordered]@{ action = 'recover'; status = 'NOOP'; lease = 'FREE' })
    }
    $processId = $(if ($lease.metadata -and $lease.metadata.process_id) { [int]$lease.metadata.process_id } else { 0 })
    if ($processId -gt 0 -and (Get-Process -Id $processId -ErrorAction SilentlyContinue)) {
        Write-JsonResult ([ordered]@{
            action = 'recover'
            status = 'BLOCKED'
            reason = 'lease process is still running'
            process_id = $processId
        }) 4
    }
    if (
        $lease.metadata -and
        @('browser-origin-uat-reserved', 'browser-origin-uat') -contains [string]$lease.metadata.mode
    ) {
        $origin = Get-OriginUatEndpoint
        $originListeners = @(
            Get-NetTCPConnection -State Listen -LocalPort $origin.Port -ErrorAction SilentlyContinue
        )
        if ($originListeners.Count -gt 0) {
            Write-JsonResult ([ordered]@{
                action = 'recover'
                status = 'BLOCKED'
                reason = 'origin UAT listener is still active'
                listen_count = $originListeners.Count
            }) 4
        }
    }
    if ($lease.metadata -and $lease.metadata.mode -eq 'desktop') {
        $recoverConfig = Get-Content -LiteralPath $ConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
        $desktopTask = Get-ScheduledTask -TaskName ([string]$recoverConfig.desktop_task_name) -ErrorAction SilentlyContinue
        if ($desktopTask -and $desktopTask.State -eq 'Running') {
            Write-JsonResult ([ordered]@{
                action = 'recover'
                status = 'BLOCKED'
                reason = 'desktop broker task is still running'
                task_name = [string]$recoverConfig.desktop_task_name
            }) 4
        }
        $brokerProcesses = Get-ManagedDesktopBrokerProcesses
        if ($brokerProcesses.Count -gt 0) {
            Write-JsonResult ([ordered]@{
                action = 'recover'
                status = 'BLOCKED'
                reason = 'managed desktop broker process is still running'
                residual_process_ids = @($brokerProcesses.ProcessId)
            }) 4
        }
        $runtimePath = $(if ($lease.metadata.artifact_dir) {
            Join-Path ([string]$lease.metadata.artifact_dir) 'desktop-runtime.json'
        } else { $null })
        if ($runtimePath -and (Test-Path -LiteralPath $runtimePath -PathType Leaf)) {
            $runtime = Get-Content -LiteralPath $runtimePath -Raw -Encoding UTF8 | ConvertFrom-Json
            $runtimeIds = @([int]$runtime.broker_process_id, [int]$runtime.launcher_process_id, [int]$runtime.app_process_id) |
                Where-Object { $_ -gt 0 } | Select-Object -Unique
            $alive = @($runtimeIds | ForEach-Object { Get-Process -Id $_ -ErrorAction SilentlyContinue })
            if ($alive.Count -gt 0) {
                Write-JsonResult ([ordered]@{
                    action = 'recover'
                    status = 'BLOCKED'
                    reason = 'desktop broker or runner-started app is still running'
                    residual_process_ids = @($alive.Id)
                }) 4
            }
        }
    }
    $ageMinutes = ((Get-Date) - (Get-Item -LiteralPath $LeasePath).LastWriteTime).TotalMinutes
    if ($ageMinutes -lt $MaxAgeMinutes) {
        Write-JsonResult ([ordered]@{
            action = 'recover'
            status = 'BLOCKED'
            reason = 'lease is not stale'
            age_minutes = [math]::Round($ageMinutes, 2)
            required_age_minutes = $MaxAgeMinutes
        }) 4
    }
    Remove-Item -LiteralPath $LeasePath -Recurse -Force
    Write-JsonResult ([ordered]@{
        action = 'recover'
        status = 'RECOVERED'
        age_minutes = [math]::Round($ageMinutes, 2)
    })
}

if (-not $ContractPath) {
    throw 'ContractPath is required for run'
}
if (-not (Test-Path -LiteralPath $ContractPath -PathType Leaf)) {
    throw "Contract does not exist: $ContractPath"
}
if (-not (Test-PathUnderRoot $ContractPath $InboxRoot)) {
    throw 'ContractPath must stay under the runner inbox'
}

$contract = $null
try {
    $contract = Get-Content -LiteralPath $ContractPath -Raw -Encoding UTF8 | ConvertFrom-Json
}
finally {
    Remove-Item -LiteralPath $ContractPath -Force -ErrorAction SilentlyContinue
}
if ([string]$contract.task_id -notmatch '^[a-z0-9][a-z0-9-]{2,63}$') {
    throw 'Invalid task_id'
}
$config = Get-Content -LiteralPath $ConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
$timeoutSeconds = [int]$contract.timeout_seconds
if ($timeoutSeconds -lt 10 -or $timeoutSeconds -gt [int]$config.max_timeout_seconds) {
    throw 'Invalid timeout_seconds'
}

New-Item -ItemType Directory -Force -Path $StateRoot, $InboxRoot, $ArtifactRoot | Out-Null
$usesReservation = -not [string]::IsNullOrWhiteSpace($ReservationId)
$reservationReservedAt = $null
if ([int]$contract.schema_version -eq 3 -and -not $usesReservation) {
    throw 'Browser origin UAT contracts require a matching reservation'
}
if ($usesReservation) {
    if ([int]$contract.schema_version -ne 3 -or [string]$contract.capability -ne 'origin-uat') {
        throw 'Reservations are valid only for browser origin UAT contracts'
    }
    $reserved = Get-LeaseState
    if (
        $reserved.status -ne 'HELD' -or
        -not $reserved.metadata -or
        [string]$reserved.metadata.mode -ne 'browser-origin-uat-reserved' -or
        [string]$reserved.metadata.task_id -ne [string]$contract.task_id -or
        [string]$reserved.metadata.reservation_id -ne $ReservationId -or
        [int]$reserved.metadata.process_id -ne 0
    ) {
        throw 'Origin UAT reservation does not match the held lease'
    }
    $reservationReservedAt = [string]$reserved.metadata.reserved_at
}
else {
    try {
        New-Item -ItemType Directory -Path $LeasePath -ErrorAction Stop | Out-Null
    }
    catch {
        Write-JsonResult ([ordered]@{
            action = 'run'
            status = 'BUSY'
            task_id = [string]$contract.task_id
            lease = Get-LeaseState
        }) 4
    }
}

$runId = [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfffZ') + '-' + [Guid]::NewGuid().ToString('N').Substring(0, 8)
$taskArtifactRoot = Join-Path $ArtifactRoot ([string]$contract.task_id)
$runArtifactRoot = Join-Path $taskArtifactRoot $runId
New-Item -ItemType Directory -Force -Path $runArtifactRoot | Out-Null
$stdoutPath = Join-Path $runArtifactRoot 'runner.stdout.log'
$stderrPath = Join-Path $runArtifactRoot 'runner.stderr.log'
$resultPath = Join-Path $runArtifactRoot 'result.json'
$workingContractPath = Join-Path $LeasePath 'contract.json'
$process = $null
$jobHandle = [IntPtr]::Zero
$desktopTask = $null
$desktopTaskStarted = $false
$preserveLease = $false

try {
    Write-Utf8NoBom $workingContractPath (($contract | ConvertTo-Json -Depth 20) + "`n")
    $node = Get-Command node.exe -ErrorAction Stop
    $validationOutput = & $node.Source $ContractValidatorPath $workingContractPath $ConfigPath 2>$null
    $validationExit = $LASTEXITCODE
    $validation = $validationOutput | Select-Object -Last 1 | ConvertFrom-Json
    if ($validationExit -ne 0 -or $validation.status -ne 'VALID' -or $validation.mode -ne [string]$contract.mode) {
        throw "Task contract failed live policy validation: $([string]$validation.error)"
    }

    if ($contract.mode -eq 'desktop') {
        if (-not (Test-ExactJsonTrue $config.desktop_enabled)) { throw 'Desktop mode is disabled' }
        Write-Utf8NoBom $stdoutPath "desktop Session 0 controller started`n"
        Write-Utf8NoBom $stderrPath ''
        $desktopTask = Get-ScheduledTask -TaskName ([string]$config.desktop_task_name) -ErrorAction SilentlyContinue
        if (-not $desktopTask) { throw 'Managed desktop broker task is missing' }
        if ($desktopTask.State -eq 'Running') { throw 'Managed desktop broker task is already running' }
        $requestPath = Join-Path $LeasePath 'desktop-request.json'
        Write-Utf8NoBom $requestPath (([ordered]@{
            schema_version = 2
            contract_path = $workingContractPath
            config_path = $ConfigPath
            artifact_dir = $runArtifactRoot
        } | ConvertTo-Json -Depth 6) + "`n")
        Write-Utf8NoBom $LeaseMetadataPath (([ordered]@{
            schema_version = 2
            mode = 'desktop'
            task_id = [string]$contract.task_id
            run_id = $runId
            process_id = 0
            scheduled_task = [string]$config.desktop_task_name
            artifact_dir = $runArtifactRoot
            started_at = (Get-Date).ToString('o')
        } | ConvertTo-Json -Depth 6) + "`n")
        Start-ScheduledTask -TaskName ([string]$config.desktop_task_name)
        $desktopTaskStarted = $true
        $deadline = (Get-Date).AddSeconds($timeoutSeconds + 5)
        while (-not (Test-Path -LiteralPath $resultPath -PathType Leaf) -and (Get-Date) -lt $deadline) {
            Start-Sleep -Milliseconds 200
        }
    }
    else {
        $process = Start-Process -FilePath $node.Source -ArgumentList @(
            $RunnerPath,
            $workingContractPath,
            $ConfigPath,
            $runArtifactRoot
        ) -WorkingDirectory $CurrentRoot -NoNewWindow -PassThru `
            -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath
        Initialize-KillOnCloseJobType
        $jobHandle = [RemoteComputerUse.NativeJob]::CreateKillOnClose()
        [RemoteComputerUse.NativeJob]::Assign($jobHandle, $process.Handle)
        Write-Utf8NoBom $LeaseMetadataPath (([ordered]@{
            schema_version = $(if ([int]$contract.schema_version -eq 3) { 3 } else { 2 })
            mode = $(if ([int]$contract.schema_version -eq 3) { 'browser-origin-uat' } else { 'browser' })
            task_id = [string]$contract.task_id
            run_id = $runId
            reservation_id = $(if ($usesReservation) { $ReservationId } else { $null })
            process_id = $process.Id
            started_at = (Get-Date).ToString('o')
        } | ConvertTo-Json -Depth 6) + "`n")
        [void]$process.WaitForExit(($timeoutSeconds + 15) * 1000)
    }

    if (-not (Test-Path -LiteralPath $resultPath -PathType Leaf)) {
        if ($contract.mode -eq 'desktop' -and $desktopTaskStarted) {
            Write-Utf8NoBom (Join-Path $LeasePath 'stop.requested') ([DateTime]::UtcNow.ToString('o') + "`n")
            Stop-ScheduledTask -TaskName ([string]$config.desktop_task_name) -ErrorAction SilentlyContinue
            $stopDeadline = (Get-Date).AddSeconds(10)
            do {
                Start-Sleep -Milliseconds 200
                $taskAfterStop = Get-ScheduledTask -TaskName ([string]$config.desktop_task_name) -ErrorAction SilentlyContinue
            } while ($taskAfterStop -and $taskAfterStop.State -eq 'Running' -and (Get-Date) -lt $stopDeadline)
            if ($taskAfterStop -and $taskAfterStop.State -eq 'Running') {
                $preserveLease = $true
                Write-JsonResult ([ordered]@{
                    schema_version = 2
                    runner_version = $RunnerVersion
                    task_id = [string]$contract.task_id
                    run_id = $runId
                    mode = 'desktop'
                    status = 'RECOVERY_REQUIRED'
                    error = 'desktop broker did not stop after the bounded timeout'
                    artifact_dir = $runArtifactRoot
                    lease_status = 'HELD'
                }) 4
            }
            $brokerProcesses = Get-ManagedDesktopBrokerProcesses
            if ($brokerProcesses.Count -gt 0) {
                $preserveLease = $true
                Write-JsonResult ([ordered]@{
                    schema_version = 2
                    runner_version = $RunnerVersion
                    task_id = [string]$contract.task_id
                    run_id = $runId
                    mode = 'desktop'
                    status = 'RECOVERY_REQUIRED'
                    error = 'managed desktop broker process survived task stop'
                    residual_process_ids = @($brokerProcesses.ProcessId)
                    artifact_dir = $runArtifactRoot
                    lease_status = 'HELD'
                }) 4
            }
            $runtimePath = Join-Path $runArtifactRoot 'desktop-runtime.json'
            if (Test-Path -LiteralPath $runtimePath -PathType Leaf) {
                $runtime = Get-Content -LiteralPath $runtimePath -Raw -Encoding UTF8 | ConvertFrom-Json
                $residualIds = @([int]$runtime.broker_process_id, [int]$runtime.launcher_process_id, [int]$runtime.app_process_id) |
                    Where-Object { $_ -gt 0 } | Select-Object -Unique
                $residualProcesses = @($residualIds | ForEach-Object { Get-Process -Id $_ -ErrorAction SilentlyContinue })
                if ($residualProcesses.Count -gt 0) {
                    $preserveLease = $true
                    Write-JsonResult ([ordered]@{
                        schema_version = 2
                        runner_version = $RunnerVersion
                        task_id = [string]$contract.task_id
                        run_id = $runId
                        mode = 'desktop'
                        status = 'RECOVERY_REQUIRED'
                        error = 'desktop broker or runner-started app survived task stop'
                        residual_process_ids = @($residualProcesses.Id)
                        artifact_dir = $runArtifactRoot
                        lease_status = 'HELD'
                    }) 4
                }
            }
            if (Test-Path -LiteralPath $resultPath -PathType Leaf) {
                Move-Item -LiteralPath $resultPath -Destination (Join-Path $runArtifactRoot 'broker-result-after-timeout.json') -Force
            }
        }
        elseif ($process -and -not $process.HasExited) {
            & taskkill.exe /PID $process.Id /T /F 2>$null | Out-Null
        }
        $timeoutResult = [ordered]@{
            schema_version = $(
                if ($contract.mode -eq 'desktop') { 2 }
                elseif ([int]$contract.schema_version -eq 3) { 3 }
                else { 1 }
            )
            runner_version = $RunnerVersion
            task_id = [string]$contract.task_id
            run_id = $runId
            mode = [string]$contract.mode
            status = 'TIMEOUT'
            error = 'runner exceeded the bounded timeout'
            artifact_dir = $runArtifactRoot
            lease_status = $(if ($usesReservation) { 'HELD' } else { 'RELEASED' })
        }
        Write-Utf8NoBom $resultPath (($timeoutResult | ConvertTo-Json -Depth 6) + "`n")
        Write-JsonResult $timeoutResult 5
    }

    $result = Get-Content -LiteralPath $resultPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $result | Add-Member -NotePropertyName run_id -NotePropertyValue $runId -Force
    $result | Add-Member -NotePropertyName lease_status `
        -NotePropertyValue $(if ($usesReservation) { 'HELD' } else { 'RELEASED' }) -Force
    Write-JsonResult $result $(if ($result.status -eq 'PASS') { 0 } else { 1 })
}
finally {
    if ($desktopTaskStarted) {
        $taskState = Get-ScheduledTask -TaskName ([string]$config.desktop_task_name) -ErrorAction SilentlyContinue
        if ($taskState -and $taskState.State -eq 'Running') {
            Stop-ScheduledTask -TaskName ([string]$config.desktop_task_name) -ErrorAction SilentlyContinue
        }
    }
    if ($jobHandle -ne [IntPtr]::Zero) {
        [RemoteComputerUse.NativeJob]::Close($jobHandle)
    } elseif ($process -and -not $process.HasExited) {
        & taskkill.exe /PID $process.Id /T /F 2>$null | Out-Null
    }
    if (-not $preserveLease -and (Test-Path -LiteralPath $LeasePath -PathType Container)) {
        if ($usesReservation) {
            Remove-Item -LiteralPath $workingContractPath -Force -ErrorAction SilentlyContinue
            Write-Utf8NoBom $LeaseMetadataPath (([ordered]@{
                schema_version = 3
                mode = 'browser-origin-uat-reserved'
                task_id = [string]$contract.task_id
                reservation_id = $ReservationId
                process_id = 0
                reserved_at = $(
                    if ($reservationReservedAt) { $reservationReservedAt }
                    else { (Get-Date).ToString('o') }
                )
            } | ConvertTo-Json -Depth 6) + "`n")
        }
        else {
            Remove-Item -LiteralPath $LeasePath -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
}
