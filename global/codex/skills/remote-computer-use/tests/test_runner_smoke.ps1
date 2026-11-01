[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$SkillRoot = Split-Path -Parent $PSScriptRoot
$RunnerSource = Join-Path $SkillRoot 'assets\vm-runner'
$ContractPath = Join-Path $SkillRoot 'examples\pilot-contract.json'
$EgressContractPath = Join-Path $SkillRoot 'examples\offline-egress-contract.json'
$TestRoot = Join-Path $env:TEMP ('rcu-runner-smoke-' + [Guid]::NewGuid().ToString('N'))
$InstallRoot = Join-Path $TestRoot 'installed'
$RunnerRoot = Join-Path $InstallRoot 'current'
$ArtifactRoot = Join-Path $TestRoot 'artifacts'
$originServer = $null
$reservationOriginServer = $null

try {
    New-Item -ItemType Directory -Force -Path `
        $RunnerRoot, `
        $ArtifactRoot, `
        (Join-Path $InstallRoot 'inbox'), `
        (Join-Path $InstallRoot 'state'), `
        (Join-Path $InstallRoot 'artifacts') | Out-Null
    Copy-Item -Path (Join-Path $RunnerSource '*') -Destination $RunnerRoot -Recurse -Force
    $npmOutput = & npm.cmd ci --omit=dev --ignore-scripts --prefix $RunnerRoot 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "npm ci failed: $($npmOutput -join ' ')"
    }
    $config = Get-Content -LiteralPath (Join-Path $RunnerRoot 'config.example.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    $browserCandidates = @(
        $config.browser_executable,
        (Join-Path $env:LOCALAPPDATA 'Google\Chrome\Application\chrome.exe'),
        (Join-Path $env:PROGRAMFILES 'Microsoft\Edge\Application\msedge.exe'),
        (Join-Path ${env:PROGRAMFILES(X86)} 'Microsoft\Edge\Application\msedge.exe')
    )
    $browser = @($browserCandidates | Where-Object { $_ -and (Test-Path -LiteralPath $_ -PathType Leaf) } | Select-Object -First 1)
    if ($browser.Count -eq 0) {
        throw 'No local Chromium browser is available for the smoke test'
    }
    $config.browser_executable = [string]$browser[0]
    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText(
        (Join-Path $RunnerRoot 'config.test.json'),
        (($config | ConvertTo-Json -Depth 6) + "`n"),
        $encoding
    )
    [System.IO.File]::WriteAllText(
        (Join-Path $InstallRoot 'config.json'),
        (($config | ConvertTo-Json -Depth 6) + "`n"),
        $encoding
    )

    $output = & node.exe (Join-Path $RunnerRoot 'src\runner.mjs') `
        $ContractPath (Join-Path $RunnerRoot 'config.test.json') $ArtifactRoot 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "Runner failed: $($output -join ' ')"
    }
    $result = Get-Content -LiteralPath (Join-Path $ArtifactRoot 'result.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    $network = Get-Content -LiteralPath (Join-Path $ArtifactRoot 'network.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    $checks = [ordered]@{
        status = $result.status
        network_policy = $result.network_policy
        final_screenshot = (Test-Path -LiteralPath (Join-Path $ArtifactRoot 'final.png') -PathType Leaf)
        action_screenshot = (Test-Path -LiteralPath (Join-Path $ArtifactRoot 'pilot-after-click.png') -PathType Leaf)
        trace = (Test-Path -LiteralPath (Join-Path $ArtifactRoot 'trace.zip') -PathType Leaf)
        external_requests = @($network | Where-Object { $_.url -notlike 'file://*' }).Count
    }

    $egressArtifactRoot = Join-Path $TestRoot 'egress-artifacts'
    New-Item -ItemType Directory -Force -Path $egressArtifactRoot | Out-Null
    $egressOutput = & node.exe (Join-Path $RunnerRoot 'src\runner.mjs') `
        $EgressContractPath (Join-Path $RunnerRoot 'config.test.json') $egressArtifactRoot 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "Offline egress probe failed: $($egressOutput -join ' ')"
    }
    $egressResult = Get-Content -LiteralPath (Join-Path $egressArtifactRoot 'result.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    $egressNetwork = Get-Content -LiteralPath (Join-Path $egressArtifactRoot 'network.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    $checks['egress_status'] = $egressResult.status
    $checks['egress_allowed_external'] = @(
        $egressNetwork | Where-Object { $_.decision -eq 'allowed' -and $_.url -notlike 'file://*' }
    ).Count
    $checks['post_blocked'] = @($egressNetwork | Where-Object { $_.method -eq 'POST' -and $_.decision -eq 'blocked' }).Count -ge 1
    $checks['websocket_blocked'] = @($egressNetwork | Where-Object { $_.method -eq 'WEBSOCKET' -and $_.decision -eq 'blocked' }).Count -ge 1

    $portProbe = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 0)
    $portProbe.Start()
    $originPort = ([System.Net.IPEndPoint]$portProbe.LocalEndpoint).Port
    $portProbe.Stop()
    $origin = "http://127.0.0.1:$originPort"
    $originStdout = Join-Path $TestRoot 'origin-server.stdout.log'
    $originStderr = Join-Path $TestRoot 'origin-server.stderr.log'
    $originServerPath = Join-Path $RunnerRoot 'test\origin-uat-server.mjs'
    $originServer = Start-Process -FilePath node.exe `
        -ArgumentList @("`"$originServerPath`"", [string]$originPort) `
        -RedirectStandardOutput $originStdout `
        -RedirectStandardError $originStderr `
        -WindowStyle Hidden -PassThru
    $originReady = $false
    for ($attempt = 0; $attempt -lt 40; $attempt++) {
        try {
            $probe = [System.Net.Sockets.TcpClient]::new()
            $probe.Connect('127.0.0.1', $originPort)
            $probe.Dispose()
            $originReady = $true
            break
        }
        catch {
            Start-Sleep -Milliseconds 100
        }
    }
    if (-not $originReady) {
        throw "Origin UAT smoke server failed to start: $(Get-Content -LiteralPath $originStderr -Raw -ErrorAction SilentlyContinue)"
    }

    $originConfig = ($config | ConvertTo-Json -Depth 10 | ConvertFrom-Json)
    $originConfig.allow_network = $true
    $originConfig.allowed_origins = @($origin)
    $originConfig.capture_console_text = $false
    $originConfigPath = Join-Path $RunnerRoot 'config.origin-uat.json'
    [System.IO.File]::WriteAllText(
        $originConfigPath,
        (($originConfig | ConvertTo-Json -Depth 10) + "`n"),
        $encoding
    )
    $fixturePath = Join-Path $RunnerRoot 'fixtures\files\revolut-synthetic-2026-07.txt'
    $fixtureSha = (Get-FileHash -LiteralPath $fixturePath -Algorithm SHA256).Hash.ToLowerInvariant()
    $originContract = [ordered]@{
        schema_version = 3
        task_id = 'origin-uat-smoke'
        mode = 'browser'
        headless = $true
        side_effects = 'test-only'
        capability = 'origin-uat'
        timeout_seconds = 30
        target = [ordered]@{ kind = 'url'; value = "$origin/" }
        actions = @(
            [ordered]@{
                type = 'select_option'
                selector = '#period'
                value = '2026-08-01_2026-08-27'
            },
            [ordered]@{ type = 'assert_text'; selector = '#period-status'; contains = '2026-08-01_2026-08-27' },
            [ordered]@{
                type = 'set_fixture_file'
                selector = '#statement'
                fixture_id = 'revolut-synthetic-2026-07'
                fixture_sha256 = $fixtureSha
            },
            [ordered]@{ type = 'assert_text'; selector = '#status'; contains = 'Zaimportowano syntetyczny plik' }
        )
    }
    $originContractPath = Join-Path $TestRoot 'origin-uat-contract.json'
    [System.IO.File]::WriteAllText(
        $originContractPath,
        (($originContract | ConvertTo-Json -Depth 10) + "`n"),
        $encoding
    )
    $originArtifactRoot = Join-Path $TestRoot 'origin-uat-artifacts'
    New-Item -ItemType Directory -Force -Path $originArtifactRoot | Out-Null
    $originOutput = & node.exe (Join-Path $RunnerRoot 'src\runner.mjs') `
        $originContractPath $originConfigPath $originArtifactRoot 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "Origin UAT runner smoke failed: $($originOutput -join ' ')"
    }
    $originResultPath = Join-Path $originArtifactRoot 'result.json'
    $originNetworkPath = Join-Path $originArtifactRoot 'network.json'
    $originResult = Get-Content -LiteralPath $originResultPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $originNetwork = Get-Content -LiteralPath $originNetworkPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $originEvidenceText = (Get-Content -LiteralPath $originResultPath -Raw -Encoding UTF8) + `
        (Get-Content -LiteralPath $originNetworkPath -Raw -Encoding UTF8)
    $fixtureAction = @($originResult.actions | Where-Object { $_.type -eq 'set_fixture_file' } | Select-Object -First 1)
    $selectAction = @($originResult.actions | Where-Object { $_.type -eq 'select_option' } | Select-Object -First 1)
    $checks['origin_status'] = $originResult.status
    $checks['origin_network_policy'] = $originResult.network_policy
    $checks['origin_privacy_mode'] = [string]$originResult.privacy_mode
    $checks['origin_no_trace'] = -not (Test-Path -LiteralPath (Join-Path $originArtifactRoot 'trace.zip'))
    $checks['origin_no_png'] = @(Get-ChildItem -LiteralPath $originArtifactRoot -Filter '*.png' -File -ErrorAction SilentlyContinue).Count -eq 0
    $checks['origin_fixture_verified'] = [bool]($fixtureAction.Count -eq 1 -and $fixtureAction[0].fixture_verified)
    $checks['origin_select_option'] = [bool]($selectAction.Count -eq 1 -and $selectAction[0].status -eq 'PASS')
    $checks['origin_post_allowed'] = @(
        $originNetwork | Where-Object { $_.method -eq 'POST' -and $_.decision -eq 'allowed' }
    ).Count -ge 1
    $checks['origin_delete_blocked'] = @(
        $originNetwork | Where-Object { $_.method -eq 'DELETE' -and $_.decision -eq 'blocked' }
    ).Count -ge 1
    $checks['origin_external_blocked'] = @(
        $originNetwork | Where-Object { $_.url -like 'http://127.0.0.1:1/*' -and $_.decision -eq 'blocked' }
    ).Count -ge 1
    $checks['origin_websocket_blocked'] = @(
        $originNetwork | Where-Object { $_.method -eq 'WEBSOCKET' -and $_.decision -eq 'blocked' }
    ).Count -ge 1
    $checks['origin_no_fixture_path_in_json'] = -not (
        $originEvidenceText.IndexOf($fixturePath, [System.StringComparison]::OrdinalIgnoreCase) -ge 0 -or
        $originEvidenceText.IndexOf('fixtures\\files', [System.StringComparison]::OrdinalIgnoreCase) -ge 0
    )

    $inboxContract = Join-Path $InstallRoot 'inbox\offline-pilot.json'
    Copy-Item -LiteralPath $ContractPath -Destination $inboxContract
    $wrapperOutput = & powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass `
        -File (Join-Path $RunnerRoot 'scripts\invoke-run.ps1') `
        -Action run -ContractPath $inboxContract
    if ($LASTEXITCODE -ne 0) {
        throw "Wrapper failed: $($wrapperOutput -join ' ')"
    }
    $wrapper = $wrapperOutput | Select-Object -Last 1 | ConvertFrom-Json
    $checks['wrapper_status'] = $wrapper.status
    $checks['inbox_contract_removed'] = -not (Test-Path -LiteralPath $inboxContract)
    $checks['lease_released'] = -not (Test-Path -LiteralPath (Join-Path $InstallRoot 'state\browser.lease'))

    $reservationConfig = ($config | ConvertTo-Json -Depth 10 | ConvertFrom-Json)
    $reservationConfig.allow_network = $true
    $reservationConfig.allowed_origins = @('http://127.0.0.1:13102')
    $reservationConfig.capture_console_text = $false
    [System.IO.File]::WriteAllText(
        (Join-Path $InstallRoot 'config.json'),
        (($reservationConfig | ConvertTo-Json -Depth 10) + "`n"),
        $encoding
    )
    $reservationId = 'uat-' + [Guid]::NewGuid().ToString('N')
    $reserveOutput = & powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass `
        -File (Join-Path $RunnerRoot 'scripts\invoke-run.ps1') -Action reserve-origin-uat `
        -TaskId origin-uat-smoke -ReservationId $reservationId
    $reserveExit = $LASTEXITCODE
    $reserve = $reserveOutput | Select-Object -Last 1 | ConvertFrom-Json
    $checks['reservation_status'] = $reserve.status
    $checks['reservation_exit'] = $reserveExit

    $secondReservationId = 'uat-' + [Guid]::NewGuid().ToString('N')
    $secondReserveOutput = & powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass `
        -File (Join-Path $RunnerRoot 'scripts\invoke-run.ps1') -Action reserve-origin-uat `
        -TaskId origin-uat-smoke -ReservationId $secondReservationId
    $secondReserveExit = $LASTEXITCODE
    $secondReserve = $secondReserveOutput | Select-Object -Last 1 | ConvertFrom-Json
    $checks['second_reservation_status'] = $secondReserve.status
    $checks['second_reservation_exit'] = $secondReserveExit

    $listenerOutput = & powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass `
        -File (Join-Path $RunnerRoot 'scripts\invoke-run.ps1') -Action origin-listener
    $listenerExit = $LASTEXITCODE
    $listener = $listenerOutput | Select-Object -Last 1 | ConvertFrom-Json
    $checks['closed_listener_status'] = $listener.status
    $checks['closed_listener_exit'] = $listenerExit

    $directOriginContract = Join-Path $InstallRoot 'inbox\origin-uat-direct.json'
    $directContract = ($originContract | ConvertTo-Json -Depth 10 | ConvertFrom-Json)
    $directContract.target.value = 'http://127.0.0.1:13102/'
    [System.IO.File]::WriteAllText(
        $directOriginContract,
        (($directContract | ConvertTo-Json -Depth 10) + "`n"),
        $encoding
    )
    $priorErrorActionPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $directRunOutput = & powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass `
        -File (Join-Path $RunnerRoot 'scripts\invoke-run.ps1') -Action run `
        -ContractPath $directOriginContract 2>&1
    $checks['direct_origin_exit'] = $LASTEXITCODE
    $ErrorActionPreference = $priorErrorActionPreference
    $checks['direct_origin_contract_removed'] = -not (Test-Path -LiteralPath $directOriginContract)
    $checks['direct_origin_rejected'] = ($directRunOutput -join ' ') -match 'matching reservation'

    $reservationOriginStdout = Join-Path $TestRoot 'reservation-origin.stdout.log'
    $reservationOriginStderr = Join-Path $TestRoot 'reservation-origin.stderr.log'
    $reservationOriginServer = Start-Process -FilePath node.exe `
        -ArgumentList @("`"$originServerPath`"", '13102') `
        -RedirectStandardOutput $reservationOriginStdout `
        -RedirectStandardError $reservationOriginStderr `
        -WindowStyle Hidden -PassThru
    $reservationOriginReady = $false
    for ($attempt = 0; $attempt -lt 40; $attempt++) {
        try {
            $probe = [System.Net.Sockets.TcpClient]::new()
            $probe.Connect('127.0.0.1', 13102)
            $probe.Dispose()
            $reservationOriginReady = $true
            break
        }
        catch {
            Start-Sleep -Milliseconds 100
        }
    }
    if (-not $reservationOriginReady) {
        throw "Reservation origin server failed to start: $(Get-Content -LiteralPath $reservationOriginStderr -Raw -ErrorAction SilentlyContinue)"
    }

    $reservedOriginContract = Join-Path $InstallRoot 'inbox\origin-uat-reserved.json'
    [System.IO.File]::WriteAllText(
        $reservedOriginContract,
        (($directContract | ConvertTo-Json -Depth 10) + "`n"),
        $encoding
    )
    $reservedRunOutput = & powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass `
        -File (Join-Path $RunnerRoot 'scripts\invoke-run.ps1') -Action run `
        -ContractPath $reservedOriginContract -ReservationId $reservationId
    $reservedRunExit = $LASTEXITCODE
    $reservedRun = $reservedRunOutput | Select-Object -Last 1 | ConvertFrom-Json
    $reservationLeaseMetadataPath = Join-Path $InstallRoot 'state\browser.lease\lease.json'
    $reservationLeaseMetadata = Get-Content -LiteralPath $reservationLeaseMetadataPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $checks['reserved_run_status'] = $reservedRun.status
    $checks['reserved_run_exit'] = $reservedRunExit
    $checks['reserved_run_lease_status'] = [string]$reservedRun.lease_status
    $checks['reservation_restored_mode'] = [string]$reservationLeaseMetadata.mode
    $checks['reservation_restored_process_id'] = [int]$reservationLeaseMetadata.process_id

    $activeRunMetadata = ($reservationLeaseMetadata | ConvertTo-Json -Depth 8 | ConvertFrom-Json)
    $activeRunMetadata.mode = 'browser-origin-uat'
    $activeRunMetadata.process_id = 0
    $activeRunMetadata | Add-Member -NotePropertyName started_at `
        -NotePropertyValue (Get-Date).AddMinutes(-5).ToString('o') -Force
    [System.IO.File]::WriteAllText(
        $reservationLeaseMetadataPath,
        (($activeRunMetadata | ConvertTo-Json -Depth 8) + "`n"),
        $encoding
    )
    (Get-Item -LiteralPath (Split-Path -Parent $reservationLeaseMetadataPath)).LastWriteTime = (Get-Date).AddMinutes(-5)
    $activeRunRecoverOutput = & powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass `
        -File (Join-Path $RunnerRoot 'scripts\invoke-run.ps1') -Action recover -MaxAgeMinutes 1
    $activeRunRecoverExit = $LASTEXITCODE
    $activeRunRecover = $activeRunRecoverOutput | Select-Object -Last 1 | ConvertFrom-Json
    $checks['active_run_listener_recover_status'] = $activeRunRecover.status
    $checks['active_run_listener_recover_exit'] = $activeRunRecoverExit
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $reservationLeaseMetadataPath) | Out-Null
    [System.IO.File]::WriteAllText(
        $reservationLeaseMetadataPath,
        (($reservationLeaseMetadata | ConvertTo-Json -Depth 8) + "`n"),
        $encoding
    )

    $activeRecoverOutput = & powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass `
        -File (Join-Path $RunnerRoot 'scripts\invoke-run.ps1') -Action recover -MaxAgeMinutes 1
    $activeRecoverExit = $LASTEXITCODE
    $activeRecover = $activeRecoverOutput | Select-Object -Last 1 | ConvertFrom-Json
    $checks['active_listener_recover_status'] = $activeRecover.status
    $checks['active_listener_recover_exit'] = $activeRecoverExit

    $activeReleaseOutput = & powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass `
        -File (Join-Path $RunnerRoot 'scripts\invoke-run.ps1') -Action release-origin-uat `
        -TaskId origin-uat-smoke -ReservationId $reservationId
    $activeReleaseExit = $LASTEXITCODE
    $activeRelease = $activeReleaseOutput | Select-Object -Last 1 | ConvertFrom-Json
    $checks['active_listener_release_status'] = $activeRelease.status
    $checks['active_listener_release_exit'] = $activeReleaseExit

    Stop-Process -Id $reservationOriginServer.Id -Force -ErrorAction SilentlyContinue
    $reservationOriginServer.WaitForExit(5000) | Out-Null
    $reservationOriginClosed = $false
    for ($attempt = 0; $attempt -lt 40; $attempt++) {
        $remaining = @(
            Get-NetTCPConnection -State Listen -LocalPort 13102 -ErrorAction SilentlyContinue
        )
        if ($remaining.Count -eq 0) {
            $reservationOriginClosed = $true
            break
        }
        Start-Sleep -Milliseconds 100
    }
    if (-not $reservationOriginClosed) {
        throw 'Reservation origin listener did not close after server stop'
    }

    $wrongReleaseOutput = & powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass `
        -File (Join-Path $RunnerRoot 'scripts\invoke-run.ps1') -Action release-origin-uat `
        -TaskId origin-uat-smoke -ReservationId $secondReservationId
    $wrongReleaseExit = $LASTEXITCODE
    $wrongRelease = $wrongReleaseOutput | Select-Object -Last 1 | ConvertFrom-Json
    $checks['wrong_release_status'] = $wrongRelease.status
    $checks['wrong_release_exit'] = $wrongReleaseExit

    $releaseOutput = & powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass `
        -File (Join-Path $RunnerRoot 'scripts\invoke-run.ps1') -Action release-origin-uat `
        -TaskId origin-uat-smoke -ReservationId $reservationId
    $releaseExit = $LASTEXITCODE
    $release = $releaseOutput | Select-Object -Last 1 | ConvertFrom-Json
    $checks['release_status'] = $release.status
    $checks['release_exit'] = $releaseExit
    $checks['reservation_lease_released'] = -not (Test-Path -LiteralPath (Join-Path $InstallRoot 'state\browser.lease'))
    [System.IO.File]::WriteAllText(
        (Join-Path $InstallRoot 'config.json'),
        (($config | ConvertTo-Json -Depth 6) + "`n"),
        $encoding
    )

    $doctorOutput = & powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass `
        -File (Join-Path $RunnerRoot 'scripts\invoke-run.ps1') -Action doctor
    $doctor = $doctorOutput | Select-Object -Last 1 | ConvertFrom-Json
    $checks['doctor_status'] = $doctor.status
    $checks['doctor_policy_valid'] = [bool]$doctor.policy_valid
    $checks['desktop_disabled'] = -not [bool]$doctor.desktop_enabled
    $checks['desktop_status'] = [string]$doctor.desktop_status

    $invalidBooleanConfig = $config.PSObject.Copy()
    $invalidBooleanConfig.desktop_enabled = 'false'
    [System.IO.File]::WriteAllText(
        (Join-Path $InstallRoot 'config.json'),
        (($invalidBooleanConfig | ConvertTo-Json -Depth 6) + "`n"),
        $encoding
    )
    $invalidBooleanOutput = & powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass `
        -File (Join-Path $RunnerRoot 'scripts\invoke-run.ps1') -Action doctor
    $invalidBooleanDoctor = $invalidBooleanOutput | Select-Object -Last 1 | ConvertFrom-Json
    $checks['string_false_policy_valid'] = [bool]$invalidBooleanDoctor.policy_valid
    $checks['string_false_desktop_enabled'] = [bool]$invalidBooleanDoctor.desktop_enabled
    [System.IO.File]::WriteAllText(
        (Join-Path $InstallRoot 'config.json'),
        (($config | ConvertTo-Json -Depth 6) + "`n"),
        $encoding
    )

    $desktopDoctorOutput = & powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass `
        -File (Join-Path $RunnerRoot 'scripts\invoke-run.ps1') -Action doctor -RequireMode desktop
    $desktopDoctorExit = $LASTEXITCODE
    $desktopDoctor = $desktopDoctorOutput | Select-Object -Last 1 | ConvertFrom-Json
    $checks['desktop_doctor_status'] = $desktopDoctor.status
    $checks['desktop_doctor_exit'] = $desktopDoctorExit

    $busyLease = Join-Path $InstallRoot 'state\browser.lease'
    New-Item -ItemType Directory -Path $busyLease | Out-Null
    $busyContract = Join-Path $InstallRoot 'inbox\offline-pilot-busy.json'
    Copy-Item -LiteralPath $ContractPath -Destination $busyContract
    $busyOutput = & powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass `
        -File (Join-Path $RunnerRoot 'scripts\invoke-run.ps1') `
        -Action run -ContractPath $busyContract
    $busyExit = $LASTEXITCODE
    $busy = $busyOutput | Select-Object -Last 1 | ConvertFrom-Json
    $checks['busy_status'] = $busy.status
    $checks['busy_exit'] = $busyExit
    $checks['busy_contract_removed'] = -not (Test-Path -LiteralPath $busyContract)
    Remove-Item -LiteralPath $busyLease -Recurse -Force

    Write-Output ($checks | ConvertTo-Json -Compress)
    if (
        $checks.status -ne 'PASS' -or
        $checks.network_policy -ne 'offline-only' -or
        -not $checks.final_screenshot -or
        -not $checks.action_screenshot -or
        -not $checks.trace -or
        $checks.external_requests -ne 0 -or
        $checks.egress_status -ne 'PASS' -or
        $checks.egress_allowed_external -ne 0 -or
        -not $checks.post_blocked -or
        -not $checks.websocket_blocked -or
        $checks.origin_status -ne 'PASS' -or
        $checks.origin_network_policy -ne 'exact-origin-get-head-post' -or
        $checks.origin_privacy_mode -ne 'synthetic-no-visual-capture' -or
        -not $checks.origin_no_trace -or
        -not $checks.origin_no_png -or
        -not $checks.origin_fixture_verified -or
        -not $checks.origin_post_allowed -or
        -not $checks.origin_delete_blocked -or
        -not $checks.origin_external_blocked -or
        -not $checks.origin_websocket_blocked -or
        -not $checks.origin_no_fixture_path_in_json -or
        $checks.wrapper_status -ne 'PASS' -or
        -not $checks.inbox_contract_removed -or
        -not $checks.lease_released -or
        $checks.reservation_status -ne 'RESERVED' -or
        $checks.reservation_exit -ne 0 -or
        $checks.second_reservation_status -ne 'BUSY' -or
        $checks.second_reservation_exit -ne 4 -or
        $checks.closed_listener_status -ne 'CLOSED' -or
        $checks.closed_listener_exit -ne 3 -or
        $checks.direct_origin_exit -eq 0 -or
        -not $checks.direct_origin_contract_removed -or
        -not $checks.direct_origin_rejected -or
        $checks.reserved_run_status -ne 'PASS' -or
        $checks.reserved_run_exit -ne 0 -or
        $checks.reserved_run_lease_status -ne 'HELD' -or
        $checks.reservation_restored_mode -ne 'browser-origin-uat-reserved' -or
        $checks.reservation_restored_process_id -ne 0 -or
        $checks.active_run_listener_recover_status -ne 'BLOCKED' -or
        $checks.active_run_listener_recover_exit -ne 4 -or
        $checks.active_listener_recover_status -ne 'BLOCKED' -or
        $checks.active_listener_recover_exit -ne 4 -or
        $checks.active_listener_release_status -ne 'BLOCKED' -or
        $checks.active_listener_release_exit -ne 4 -or
        $checks.wrong_release_status -ne 'BLOCKED' -or
        $checks.wrong_release_exit -ne 4 -or
        $checks.release_status -ne 'RELEASED' -or
        $checks.release_exit -ne 0 -or
        -not $checks.reservation_lease_released -or
        $checks.doctor_status -ne 'READY' -or
        -not $checks.doctor_policy_valid -or
        -not $checks.desktop_disabled -or
        $checks.desktop_status -ne 'DISABLED' -or
        $checks.string_false_policy_valid -or
        $checks.string_false_desktop_enabled -or
        $checks.desktop_doctor_status -ne 'NOT_READY' -or
        $checks.desktop_doctor_exit -ne 3 -or
        $checks.busy_status -ne 'BUSY' -or
        $checks.busy_exit -ne 4 -or
        -not $checks.busy_contract_removed
    ) {
        throw 'Runner smoke-test evidence is incomplete'
    }
}
finally {
    if ($reservationOriginServer -and -not $reservationOriginServer.HasExited) {
        Stop-Process -Id $reservationOriginServer.Id -Force -ErrorAction SilentlyContinue
        $reservationOriginServer.WaitForExit(5000) | Out-Null
    }
    if ($originServer -and -not $originServer.HasExited) {
        Stop-Process -Id $originServer.Id -Force -ErrorAction SilentlyContinue
        $originServer.WaitForExit(5000) | Out-Null
    }
    $resolved = [System.IO.Path]::GetFullPath($TestRoot)
    $tempRoot = [System.IO.Path]::GetFullPath($env:TEMP).TrimEnd('\')
    if (-not $resolved.StartsWith($tempRoot + '\', [System.StringComparison]::OrdinalIgnoreCase)) {
        throw 'Refusing cleanup outside TEMP'
    }
    if (Test-Path -LiteralPath $resolved) {
        Remove-Item -LiteralPath $resolved -Recurse -Force
    }
}
