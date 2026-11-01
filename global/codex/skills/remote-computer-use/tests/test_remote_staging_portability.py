"""Offline wrapper integration tests; execute only after parent source inspection.

All files are synthetic and retained beneath TEAM_TEST_ROOT. Native transports
are unavailable in the child PATH and replaced by verified GLOBAL functions.
REMOTE_STAGING_TEST_SKILL_ROOT is exclusively a test seam for replaying the same
tests/helper against the actual old wrapper bodies. No VM or network is used.
"""

import base64
import json
import os
from pathlib import Path
import subprocess
import unittest
import uuid


SKILL_ROOT = Path(os.environ.get(
    "REMOTE_STAGING_TEST_SKILL_ROOT", str(Path(__file__).resolve().parents[1])
)).resolve()
WRAPPERS = ("configure_vm_desktop.ps1", "configure_vm_origin_uat.ps1", "deploy_vm.ps1")
BASELINE = "rcu-baseline-20261027T120000Z"
GUEST_PROFILE = "R:/Synthetic Profiles/Zo\u00eb User"
GUEST_TEMP = GUEST_PROFILE + "/AppData/Local/Temp"
STATE_DIRS = {
    WRAPPERS[0]: "remote-computer-use-config",
    WRAPPERS[1]: "remote-computer-use-origin-config",
    WRAPPERS[2]: "remote-computer-use-vm-deploy",
}


# The mock parses the actual generated command. It never evaluates a wrapper's
# remote command. The sole execution exception is the explicitly requested inert
# receiver, after proving its exact path and literal-only command AST.
HARNESS = r"""
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = New-Object Text.UTF8Encoding($false)
# This state exists only in this dedicated synthetic child. Wrapper/helper script
# scopes must never supply ambient configuration, counters or encoders to mocks.
$global:__RemoteStagingFixtureState = [pscustomobject]@{
    Cfg = ([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('SETTINGS')) | ConvertFrom-Json)
    CopyCount = 0
    QueryText = $null
    Utf8 = (New-Object Text.UTF8Encoding($false))
    InstanceId = [Guid]::NewGuid().ToString('N')
    MetadataProbes = 0
    ProbeMode = 'safe'
}
# Test bodies may mutate this reference; callbacks always resolve explicit state.
$cfg = $global:__RemoteStagingFixtureState.Cfg
function global:Write-RemoteStagingFixtureEvent($Event) {
    $Event.stateId = $global:__RemoteStagingFixtureState.InstanceId
    [IO.File]::AppendAllText($global:__RemoteStagingFixtureState.Cfg.events, (($Event | ConvertTo-Json -Depth 12 -Compress) + "`n"), $global:__RemoteStagingFixtureState.Utf8)
}
. (Join-Path $global:__RemoteStagingFixtureState.Cfg.skill 'scripts/remote_staging.ps1')
$global:__RemoteStagingFixtureState.QueryText = Get-RemoteStagingQuery
function global:Read-RemoteStagingFixtureInvocation([string]$Text) {
    $tokens = $null; $errors = $null
    $ast = [Management.Automation.Language.Parser]::ParseInput($Text, [ref]$tokens, [ref]$errors)
    if ($errors.Count -ne 0 -or $ast.EndBlock.Statements.Count -ne 1) { throw 'Mock refuses a non-single command' }
    $pipeline = $ast.EndBlock.Statements[0]
    if ($pipeline -isnot [Management.Automation.Language.PipelineAst] -or $pipeline.PipelineElements.Count -ne 1) { throw 'Mock refuses a pipeline' }
    $command = $pipeline.PipelineElements[0]
    if ($command -isnot [Management.Automation.Language.CommandAst]) { throw 'Mock refuses a non-command' }
    $parts = @($command.CommandElements)
    if ($parts[0] -isnot [Management.Automation.Language.StringConstantExpressionAst]) { throw 'Mock refuses a dynamic target' }
    $bound = @{}
    for ($i = 1; $i -lt $parts.Count; $i++) {
        if ($parts[$i] -isnot [Management.Automation.Language.CommandParameterAst] -or $null -ne $parts[$i].Argument) { throw 'Mock refuses a non-named argument' }
        $name = $parts[$i].ParameterName
        if ($bound.ContainsKey($name)) { throw 'Mock refuses duplicate parameters' }
        if (($i + 1) -lt $parts.Count -and $parts[$i + 1] -is [Management.Automation.Language.StringConstantExpressionAst]) {
            $i++; $bound[$name] = $parts[$i].Value
        } else { $bound[$name] = $true }
    }
    return @{path=$parts[0].Value; bound=$bound}
}
function global:scp.exe {
    $a = @($args)
    $global:__RemoteStagingFixtureState.CopyCount++
    Write-RemoteStagingFixtureEvent @{kind='scp'; argv=$a; number=$global:__RemoteStagingFixtureState.CopyCount}
    # PS5.1 consumes -- when binding a FUNCTION. Keep the observed arguments
    # unchanged; the native marker is proved separately on actual wrapper ASTs.
    if ($a.Count -ne 3 -or $a[0] -cne '-q') { throw 'Unexpected SCP function argument vector' }
    if (-not (Test-Path -LiteralPath $a[1] -PathType Leaf)) { throw 'SCP fixture source missing' }
    $global:LASTEXITCODE = 0
    if ($global:__RemoteStagingFixtureState.Cfg.copyFailureAt -eq $global:__RemoteStagingFixtureState.CopyCount) { $global:LASTEXITCODE = 61 }
    if ($global:__RemoteStagingFixtureState.Cfg.copyMissingExit) { $global:LASTEXITCODE = $null }
}
function global:ssh.exe {
    $a = @($args)
    if ($a -contains 'virsh') {
        Write-RemoteStagingFixtureEvent @{kind='snapshot'; argv=$a}
        $global:LASTEXITCODE = $global:__RemoteStagingFixtureState.Cfg.snapshotExit
        if (-not $global:__RemoteStagingFixtureState.Cfg.snapshotMissing) { return $global:__RemoteStagingFixtureState.Cfg.baseline }
        return 'synthetic missing checkpoint'
    }
    $index = [Array]::IndexOf($a, '-EncodedCommand')
    $decoded = $null
    if ($index -ge 0) {
        if ($index -ne ($a.Count - 2)) { throw 'Unexpected encoded command vector' }
        $decoded = [Text.Encoding]::Unicode.GetString([Convert]::FromBase64String($a[$index + 1]))
    }
    if ($decoded -ceq $global:__RemoteStagingFixtureState.QueryText) {
        Write-RemoteStagingFixtureEvent @{kind='query'; argv=$a; text=$decoded}
        $global:LASTEXITCODE = $global:__RemoteStagingFixtureState.Cfg.queryExit
        if ($global:__RemoteStagingFixtureState.Cfg.queryMissingExit) { $global:LASTEXITCODE = $null }
        foreach ($line in @($global:__RemoteStagingFixtureState.Cfg.queryLines)) { Write-Output $line }
        return
    }
    # Legacy -Command/-File handling is inert, so old wrappers reach the
    # current-profile assertion rather than failing on an unsupported mock.
    if (($null -ne $decoded -and $decoded.StartsWith('Remove-Item -LiteralPath ')) -or $a -contains '-Command') {
        Write-RemoteStagingFixtureEvent @{kind='cleanup'; argv=$a; text=$decoded}
        $global:LASTEXITCODE = $global:__RemoteStagingFixtureState.Cfg.cleanupExit
        return
    }
    if ($null -ne $decoded) {
        $inv = Read-RemoteStagingFixtureInvocation $decoded
    } else {
        $fileIndex = [Array]::IndexOf($a, '-File')
        if ($fileIndex -lt 0) { throw 'Unexpected SSH command; no transport fallback is allowed' }
        $bound = @{}
        for ($i = $fileIndex + 2; $i -lt $a.Count; $i++) {
            $name = ([string]$a[$i]).TrimStart('-')
            if ($name -in @('Enable', 'AllowEnabledDesktopUpgrade', 'ApplyTemplateConfiguration')) { $bound[$name] = $true }
            else { $i++; $bound[$name] = $a[$i] }
        }
        $inv = @{path=$a[$fileIndex + 1]; bound=$bound}
    }
    Write-RemoteStagingFixtureEvent @{kind='invoke'; argv=$a; text=$decoded; path=$inv.path; bound=$inv.bound}
    if ($global:__RemoteStagingFixtureState.Cfg.receiver) {
        if ($null -eq $decoded -or $inv.path -cne $global:__RemoteStagingFixtureState.Cfg.receiver.Replace('\','/')) { throw 'Receiver path mismatch' }
        # The AST above permits only one call with literal strings and names.
        & ([scriptblock]::Create($decoded))
        $global:LASTEXITCODE = $global:__RemoteStagingFixtureState.Cfg.remoteExit
        return
    }
    $global:LASTEXITCODE = $global:__RemoteStagingFixtureState.Cfg.remoteExit
    if ($global:__RemoteStagingFixtureState.Cfg.remoteExit -ne 0) { return 'synthetic-remote-failure' }
    $tx = $inv.bound['TransactionId']
    $rollback = $inv.bound.ContainsKey('RollbackTransaction')
    if ($rollback) { $tx = $inv.bound['RollbackTransaction'] }
    $status = 'ENABLED'
    if ($rollback) { $status = 'ROLLED_BACK' }
    elseif ($global:__RemoteStagingFixtureState.Cfg.name -eq 'deploy_vm.ps1') { $status = 'DEPLOYED' }
    $receipt = [ordered]@{status=$status; transaction_id=$tx}
    if ($global:__RemoteStagingFixtureState.Cfg.name -eq 'deploy_vm.ps1' -and -not $rollback) {
        $manifest = Join-Path (Join-Path (Join-Path $global:__RemoteStagingFixtureState.Cfg.temp $tx) 'package') 'release.manifest.json'
        $receipt.payload_fingerprint = ([IO.File]::ReadAllText($manifest) | ConvertFrom-Json).payload_fingerprint
    }
    switch ($global:__RemoteStagingFixtureState.Cfg.receiptFault) {
        'status' { $receipt.status = 'WRONG' }
        'transaction' { $receipt.transaction_id = 'wrong-transaction' }
        'fingerprint' { $receipt.payload_fingerprint = 'wrong-fingerprint' }
        'json' { return 'not a JSON receipt' }
    }
    return ($receipt | ConvertTo-Json -Compress)
}
function global:Remove-Item {
    param([string]$LiteralPath, [switch]$Recurse, [switch]$Force)
    $prefix = $global:__RemoteStagingFixtureState.Cfg.owned.TrimEnd('\') + '\'
    if (-not $LiteralPath.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) { throw 'Cleanup escaped the task root' }
    Write-RemoteStagingFixtureEvent @{kind='local-cleanup-intercepted'; path=$LiteralPath}
    # Retain every fixture. Never call the real Remove-Item.
}
function global:Assert-RemoteStagingFixtureTransportMock([string]$Name) {
    $command = Get-Command -Name $Name
    if ($command.CommandType -ne 'Function') { throw 'Native transport is not mocked' }
    # PS5.1 needs literal provider variables to retrieve the GLOBAL block.
    if ($Name -ceq 'ssh.exe') { $globalBlock = ${function:global:ssh.exe} }
    elseif ($Name -ceq 'scp.exe') { $globalBlock = ${function:global:scp.exe} }
    else { throw 'Unexpected transport mock name' }
    if ($null -eq $globalBlock -or $globalBlock -isnot [Management.Automation.ScriptBlock]) { throw 'Global transport mock is not a ScriptBlock' }
    if ($command -isnot [Management.Automation.FunctionInfo] -or $command.ScriptBlock -ne $globalBlock) { throw 'Transport function shadowed' }
}
foreach ($name in @('ssh.exe', 'scp.exe')) {
    Assert-RemoteStagingFixtureTransportMock $name
}
if ($env:PATH -cne $global:__RemoteStagingFixtureState.Cfg.emptyPath) { throw 'Child PATH is not isolated' }
Write-RemoteStagingFixtureEvent @{kind='mock-proof'; ssh='global Function'; scp='global Function'; path=$env:PATH}
$global:LASTEXITCODE = 0
$envelope = [ordered]@{ok=$false; data=@(); error=$null}
try {
    $envelope.data = @(& {
BODY
    })
    $envelope.ok = $true
} catch { $envelope.error = $_.Exception.Message }
finally {
    $envelope.fixtureState = [ordered]@{
        stateId = $global:__RemoteStagingFixtureState.InstanceId
        copyCount = $global:__RemoteStagingFixtureState.CopyCount
        queryText = $global:__RemoteStagingFixtureState.QueryText
        utf8CodePage = $global:__RemoteStagingFixtureState.Utf8.CodePage
        utf8PreambleLength = $global:__RemoteStagingFixtureState.Utf8.GetPreamble().Length
    }
    [IO.File]::WriteAllText($global:__RemoteStagingFixtureState.Cfg.result, ($envelope | ConvertTo-Json -Depth 20 -Compress), $global:__RemoteStagingFixtureState.Utf8)
}
"""


@unittest.skipUnless(os.name == "nt", "Windows PowerShell 5.1 integration tests")
class RemoteStagingPortabilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        owned = os.environ.get("TEAM_TEST_ROOT")
        if not owned:
            raise RuntimeError("TEAM_TEST_ROOT must identify the parent's task-owned test directory")
        cls.root = Path(owned).resolve() / ("staging-portability-" + uuid.uuid4().hex)
        cls.root.mkdir(parents=True)
        cls.ps = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        if not cls.ps.is_file():
            raise RuntimeError("Required Windows PowerShell executable is unavailable")
        cls.empty_path = cls.root / "no-native-transports"
        cls.empty_path.mkdir()
        # Forward only OS runtime settings, never arbitrary token/credential
        # environment variables. All profile and temp paths are task-owned.
        cls.child_base = {name: os.environ[name] for name in (
            "SystemRoot", "WINDIR", "ComSpec", "ProgramFiles", "ProgramFiles(x86)",
            "ProgramW6432", "PROCESSOR_ARCHITECTURE", "NUMBER_OF_PROCESSORS", "PATHEXT",
        ) if name in os.environ}
        cls.child_base.update(PATH=str(cls.empty_path), PYTHONDONTWRITEBYTECODE="1")
        bootstrap_profile = cls.root / "bootstrap-profile"
        bootstrap_temp = bootstrap_profile / "Temp"
        bootstrap_temp.mkdir(parents=True)
        cls.child_base.update(USERPROFILE=str(bootstrap_profile), TEMP=str(bootstrap_temp), TMP=str(bootstrap_temp),
                              APPDATA=str(bootstrap_profile / "AppData/Roaming"),
                              LOCALAPPDATA=str(bootstrap_profile / "AppData/Local"))
        cls.flags = [str(cls.ps), "-NoLogo", "-NoProfile", "-NonInteractive"]
        probe = "Get-ExecutionPolicy -List|ForEach-Object{[ordered]@{scope=$_.Scope.ToString();policy=$_.ExecutionPolicy.ToString()}}|ConvertTo-Json -Compress"
        process = subprocess.run(
            cls.flags + ["-EncodedCommand", base64.b64encode(probe.encode("utf-16le")).decode("ascii")],
            env=cls.child_base, cwd=cls.root, capture_output=True, timeout=30,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if process.returncode:
            raise RuntimeError("Execution-policy observation failed: " + process.stderr.decode(errors="replace"))
        cls.policy_rows = json.loads(process.stdout.decode("utf-8-sig"))
        if len(cls.policy_rows) != 5:
            raise RuntimeError("Execution-policy observation did not cover all five scopes")
        # Honor every explicit GPO/user/process policy. Only entirely Undefined
        # scopes allow process-only RemoteSigned for these locally authored tests.
        if all(row["policy"] == "Undefined" for row in cls.policy_rows):
            cls.flags += ["-ExecutionPolicy", "RemoteSigned"]

    def case(self, name=WRAPPERS[0], **settings):
        owned = self.root / uuid.uuid4().hex
        skill = owned / "skill"
        scripts = skill / "scripts"
        scripts.mkdir(parents=True)
        for source_name in (*WRAPPERS, "remote_staging.ps1"):
            (scripts / source_name).write_bytes((SKILL_ROOT / "scripts" / source_name).read_bytes())
        assets = skill / "assets/vm-runner/scripts"
        assets.mkdir(parents=True)
        for asset in ("configure-desktop.ps1", "configure-origin-uat.ps1", "install-vm.ps1"):
            (assets / asset).write_text("throw 'Inert synthetic asset must never execute'\n", encoding="ascii")
        profile = owned / "Synthetic Local Profile"
        temp = profile / "Temp"
        temp.mkdir(parents=True)
        cfg = dict(
            owned=str(owned), skill=str(skill), profile=str(profile), temp=str(temp),
            emptyPath=str(self.empty_path), events=str(owned / "events.jsonl"),
            result=str(owned / "result.json"), name=name,
            wrapper=str(scripts / name), baseline=BASELINE,
            snapshotExit=0, snapshotMissing=False, queryExit=0, queryMissingExit=False,
            queryLines=[base64.b64encode(s.encode("utf-8")).decode("ascii") for s in (GUEST_TEMP, GUEST_PROFILE)],
            copyFailureAt=0, copyMissingExit=False, remoteExit=0, cleanupExit=0,
            receiptFault="", receiver="", parameters={},
        )
        cfg.update(settings)
        return cfg

    def run_ps(self, cfg, body):
        encoded = base64.b64encode(json.dumps(cfg, ensure_ascii=True).encode("ascii")).decode("ascii")
        source = HARNESS.replace("SETTINGS", encoded).replace("BODY", body)
        script = Path(cfg["owned"]) / "harness.ps1"
        script.write_text(source, encoding="ascii")
        env = dict(self.child_base, USERPROFILE=cfg["profile"], TEMP=cfg["temp"], TMP=cfg["temp"],
                   APPDATA=str(Path(cfg["profile"]) / "AppData/Roaming"),
                   LOCALAPPDATA=str(Path(cfg["profile"]) / "AppData/Local"))
        process = subprocess.run(
            self.flags + ["-File", str(script)], cwd=cfg["owned"], env=env,
            capture_output=True, timeout=45, creationflags=subprocess.CREATE_NO_WINDOW,
        )
        diagnostic = process.stderr.decode("utf-8", errors="replace")[-3000:]
        self.assertEqual(process.returncode, 0, diagnostic)
        self.assertTrue(Path(cfg["result"]).is_file(), diagnostic)
        result = json.loads(Path(cfg["result"]).read_text(encoding="utf-8"))
        result["child_stdout"] = process.stdout.decode("utf-8", errors="replace")
        event_path = Path(cfg["events"])
        events = [json.loads(line) for line in event_path.read_text(encoding="utf-8").splitlines()] if event_path.exists() else []
        self.assertEqual(events[0]["kind"] if events else None, "mock-proof", diagnostic)
        state = result["fixtureState"]
        self.assertRegex(state["stateId"], r"\A[0-9a-f]{32}\Z")
        self.assertTrue(all(event["stateId"] == state["stateId"] for event in events))
        copies = self.of_kind(events, "scp")
        self.assertEqual(state["copyCount"], len(copies))
        self.assertEqual([copy["number"] for copy in copies], list(range(1, len(copies) + 1)))
        self.assertEqual(state["utf8CodePage"], 65001)
        self.assertEqual(state["utf8PreambleLength"], 0)
        return result, events

    def wrapper(self, name, parameters=None, **settings):
        cfg = self.case(name, parameters=parameters if parameters is not None else self.enable_args(name), **settings)
        body = "$parameters = @{}; foreach ($p in $cfg.parameters.psobject.Properties) { $parameters[$p.Name] = $p.Value }; & $cfg.wrapper @parameters"
        result, events = self.run_ps(cfg, body)
        return cfg, result, events

    @staticmethod
    def enable_args(name):
        if name == WRAPPERS[2]:
            return {}
        parameters = dict(Enable=True, RecoveryBaseline=BASELINE, ApprovalReference="synthetic-approval")
        if name == WRAPPERS[0]:
            parameters["DesktopUser"] = "SYNTHETIC\\DesktopUser"
        return parameters

    @staticmethod
    def rollback_args(name):
        prefix = {WRAPPERS[0]: "rcu-config", WRAPPERS[1]: "rcu-origin", WRAPPERS[2]: "rcu"}[name]
        return {"RollbackTransaction": prefix + "-20261027T120000Z-1234abcd"}

    def assert_success(self, result):
        self.assertTrue(result["ok"], result["error"])
        return json.loads("\n".join(result["data"]))

    @staticmethod
    def of_kind(events, kind):
        return [event for event in events if event["kind"] == kind]

    def assert_no_receipt(self, cfg):
        self.assertEqual(list(Path(cfg["profile"]).rglob("receipt.json")), [])
        self.assertEqual(list(Path(cfg["profile"]).rglob("rollback.json")), [])

    def test_global_transport_mock_proof_rejects_local_shadows(self):
        for transport in ("ssh.exe", "scp.exe"):
            with self.subTest(transport=transport):
                cfg = self.case(shadowTransport=transport)
                body = r"""
if ($cfg.shadowTransport -ceq 'ssh.exe') { $savedGlobal = ${function:global:ssh.exe} }
else { $savedGlobal = ${function:global:scp.exe} }
Assert-RemoteStagingFixtureTransportMock $cfg.shadowTransport
$report = & {
    if ($cfg.shadowTransport -ceq 'ssh.exe') {
        function local:ssh.exe { throw 'Local SSH shadow must never execute' }
    } else {
        function local:scp.exe { throw 'Local SCP shadow must never execute' }
    }
    $resolved = Get-Command -Name $cfg.shadowTransport
    $message = $null
    try { Assert-RemoteStagingFixtureTransportMock $cfg.shadowTransport } catch { $message = $_.Exception.Message }
    if ($cfg.shadowTransport -ceq 'ssh.exe') { $globalAfter = ${function:global:ssh.exe} }
    else { $globalAfter = ${function:global:scp.exe} }
    @{error=$message; resolvedIsFunction=($resolved -is [Management.Automation.FunctionInfo]); resolvedMatchesGlobal=($resolved.ScriptBlock -eq $savedGlobal); globalUnchanged=($globalAfter -eq $savedGlobal)}
}
Assert-RemoteStagingFixtureTransportMock $cfg.shadowTransport
$report
"""
                result, events = self.run_ps(cfg, body)
                self.assertTrue(result["ok"], result["error"])
                report = result["data"][0]
                self.assertEqual(report["error"], "Transport function shadowed")
                self.assertTrue(report["resolvedIsFunction"])
                self.assertFalse(report["resolvedMatchesGlobal"])
                self.assertTrue(report["globalUnchanged"])
                self.assertEqual([e["kind"] for e in events], ["mock-proof"])

    def test_staging_query_succeeds_in_powershell_51_with_bounded_command(self):
        cfg = self.case()
        body = r"""
$query = Get-RemoteStagingQuery
$tokens = $null; $errors = $null
[void][Management.Automation.Language.Parser]::ParseInput($query, [ref]$tokens, [ref]$errors)
@{version=$PSVersionTable.PSVersion.ToString(); edition=$PSEdition; query=$query; encoded=(ConvertTo-EncodedCommand $query); parseErrors=@($errors | ForEach-Object { $_.Message })}
"""
        result, events = self.run_ps(cfg, body)
        self.assertTrue(result["ok"], result["error"])
        report = result["data"][0]
        self.assertTrue(report["version"].startswith("5.1."), report["version"])
        self.assertEqual(report["edition"], "Desktop")
        self.assertEqual(report["parseErrors"], [])
        self.assertTrue(report["query"].isascii())
        self.assertGreater(len(report["encoded"]), 0)
        self.assertLessEqual(len(report["encoded"]), 7000)
        self.assertEqual(base64.b64decode(report["encoded"]).decode("utf-16le"), report["query"])
        self.assertEqual([e["kind"] for e in events], ["mock-proof"])

    def test_wrappers_use_current_remote_profile(self):
        # Parent replays THIS SAME test against real old wrappers with the new
        # helper and synthetic assets. Assert profile binding before success so
        # red evidence identifies the defect rather than a fixture/import error.
        for name in WRAPPERS:
            with self.subTest(wrapper=name):
                cfg, result, events = self.wrapper(name)
                copies = self.of_kind(events, "scp")
                self.assertTrue(copies, result)
                for copy in copies:
                    destination = copy["argv"][-1]
                    self.assertTrue(destination.startswith("ai-lab-uat-vm:" + GUEST_TEMP + "/"),
                                    "SCP destination is not bound to the authenticated profile: " + destination)
                    self.assertEqual(len(copy["argv"]), 3,
                                     "PS5.1 FUNCTION binding consumes --; source and destination must each stay one argument")
                receipt = self.assert_success(result)
                queries = self.of_kind(events, "query")
                self.assertEqual(len(queries), 1, "Query once and reuse for package and installer")
                # Events cross the real wrapper/helper script scopes. The final
                # state must retain the exact query and monotonically count SCP.
                self.assertEqual(queries[0]["text"], result["fixtureState"]["queryText"])
                self.assertEqual([copy["number"] for copy in copies], [1, 2] if name == WRAPPERS[2] else [1])
                invocations = self.of_kind(events, "invoke")
                self.assertEqual(len(invocations), 1)
                self.assertTrue(invocations[0]["path"].startswith(GUEST_TEMP + "/"))
                self.assertNotIn("-ExecutionPolicy", invocations[0]["argv"])
                self.assertIn("-EncodedCommand", invocations[0]["argv"])
                self.assertEqual(len(self.of_kind(events, "cleanup")), len(copies))
                state = Path(cfg["profile"]) / ".agents" / STATE_DIRS[name] / "transactions" / receipt["transaction_id"] / "receipt.json"
                self.assertEqual(json.loads(state.read_text(encoding="utf-8")), receipt)
                if name == WRAPPERS[2]:
                    self.assertEqual(invocations[0]["bound"]["PackageZip"], copies[0]["argv"][-1].split(":", 1)[1])
                    self.assertRegex(receipt["payload_fingerprint"], r"\A[0-9a-f]{64}\Z")
                    self.assertNotIn("AllowEnabledDesktopUpgrade", invocations[0]["bound"])
                    self.assertNotIn("ApplyTemplateConfiguration", invocations[0]["bound"])

    def test_native_scp_calls_keep_literal_end_of_options_marker(self):
        # This independent source proof never gates wrapper execution: old
        # wrappers must still reach the current-profile assertion above.
        for name in WRAPPERS:
            with self.subTest(wrapper=name):
                cfg = self.case(name)
                original = Path(cfg["wrapper"]).read_bytes()
                negative = original.replace(b"scp.exe -q -- ", b"scp.exe -q ")
                cfg["negativeWrapper"] = str(Path(cfg["owned"]) / "wrapper-without-scp-marker.ps1")
                Path(cfg["negativeWrapper"]).write_bytes(negative)
                body = r"""
function Get-RemoteStagingFixtureScpAst([string]$Path) {
    $tokens = $null; $errors = $null
    $ast = [Management.Automation.Language.Parser]::ParseFile($Path, [ref]$tokens, [ref]$errors)
    $commands = @($ast.FindAll({
        param($node)
        $node -is [Management.Automation.Language.CommandAst] -and $node.GetCommandName() -ceq 'scp.exe'
    }, $true))
    $calls = @(foreach ($command in $commands) {
        @{elements=@($command.CommandElements | ForEach-Object { @{type=$_.GetType().Name; text=$_.Extent.Text} })}
    })
    @{parseErrors=@($errors | ForEach-Object { $_.Message }); calls=$calls}
}
@{version=$PSVersionTable.PSVersion.ToString(); edition=$PSEdition; actual=(Get-RemoteStagingFixtureScpAst $cfg.wrapper); withoutMarker=(Get-RemoteStagingFixtureScpAst $cfg.negativeWrapper)}
"""
                result, events = self.run_ps(cfg, body)
                self.assertTrue(result["ok"], result["error"])
                report = result["data"][0]
                self.assertTrue(report["version"].startswith("5.1."), report["version"])
                self.assertEqual(report["edition"], "Desktop")
                expected_calls = (
                    (("$RemoteInstaller", '"${HostAlias}:$remoteInstallerPath"'),
                     ("$packageZip", '"${HostAlias}:$remotePackagePath"'))
                    if name == WRAPPERS[2] else
                    (("$RemoteScript", '"${HostAlias}:$remotePath"'),)
                )

                def assert_native_scp_syntax(parsed):
                    self.assertEqual(parsed["parseErrors"], [])
                    self.assertEqual(len(parsed["calls"]), len(expected_calls))
                    for call, (source, destination) in zip(parsed["calls"], expected_calls):
                        elements = [(part["type"], part["text"]) for part in call["elements"]]
                        self.assertGreaterEqual(len(elements), 3)
                        self.assertEqual(elements[2], ("CommandParameterAst", "--"),
                                         "Native SCP end-of-options marker must be literal --")
                        self.assertEqual(elements, [
                            ("StringConstantExpressionAst", "scp.exe"),
                            ("CommandParameterAst", "-q"),
                            ("CommandParameterAst", "--"),
                            ("VariableExpressionAst", source),
                            ("ExpandableStringExpressionAst", destination),
                        ])

                assert_native_scp_syntax(report["actual"])
                self.assertEqual(original.count(b"scp.exe -q -- "), len(expected_calls))
                self.assertNotEqual(negative, original)
                with self.assertRaisesRegex(AssertionError, "Native SCP end-of-options marker"):
                    assert_native_scp_syntax(report["withoutMarker"])
                self.assertEqual([e["kind"] for e in events], ["mock-proof"])

    def test_rollback_uses_one_query_and_retains_receipt(self):
        for name in WRAPPERS:
            with self.subTest(wrapper=name):
                args = self.rollback_args(name)
                cfg, result, events = self.wrapper(name, args)
                receipt = self.assert_success(result)
                self.assertEqual(receipt["status"], "ROLLED_BACK")
                self.assertEqual(receipt["transaction_id"], args["RollbackTransaction"])
                self.assertEqual(len(self.of_kind(events, "query")), 1)
                self.assertEqual(len(self.of_kind(events, "snapshot")), 0)
                self.assertEqual(len(self.of_kind(events, "scp")), 1)
                state = Path(cfg["profile"]) / ".agents" / STATE_DIRS[name] / "transactions" / args["RollbackTransaction"] / "rollback.json"
                self.assertEqual(json.loads(state.read_text(encoding="utf-8")), receipt)

    def test_dry_runs_add_no_guest_transport_or_local_receipts(self):
        for name in WRAPPERS:
            for rollback in (False, True):
                with self.subTest(wrapper=name, rollback=rollback):
                    args = self.rollback_args(name) if rollback else self.enable_args(name)
                    args["DryRun"] = True
                    cfg, result, events = self.wrapper(name, args)
                    receipt = self.assert_success(result)
                    self.assertEqual(receipt["status"], "DRY_RUN")
                    self.assertEqual(receipt["writes_performed"], 0)
                    self.assertEqual([e["kind"] for e in events if e["kind"] != "mock-proof"],
                                     ["snapshot"] if name != WRAPPERS[2] and not rollback else [])
                    self.assert_no_receipt(cfg)

    def test_bad_query_roots_fail_before_scp_or_cleanup_in_all_wrappers(self):
        bad = GUEST_PROFILE + "-other/Temp"
        lines = [base64.b64encode(s.encode()).decode() for s in (bad, GUEST_PROFILE)]
        for name in WRAPPERS:
            with self.subTest(wrapper=name):
                cfg, result, events = self.wrapper(name, queryLines=lines)
                self.assertFalse(result["ok"])
                self.assertIn("outside", result["error"])
                self.assertEqual(len(self.of_kind(events, "query")), 1)
                for kind in ("scp", "invoke", "cleanup"):
                    self.assertEqual(self.of_kind(events, kind), [])
                self.assert_no_receipt(cfg)

    def test_scp_and_remote_failures_propagate_without_success_receipts(self):
        scenarios = (
            ({"copyFailureAt": 1}, "Failed to copy", 0, 1),
            ({"remoteExit": 73, "cleanupExit": 91}, "synthetic-remote-failure", 1, None),
        )
        for name in WRAPPERS:
            for settings, error, invoke_count, cleanup_count in scenarios:
                with self.subTest(wrapper=name, settings=settings):
                    cfg, result, events = self.wrapper(name, **settings)
                    self.assertFalse(result["ok"])
                    self.assertIn(error, result["error"])
                    self.assertEqual(len(self.of_kind(events, "invoke")), invoke_count)
                    self.assertEqual(len(self.of_kind(events, "cleanup")), cleanup_count or len(self.of_kind(events, "scp")))
                    self.assert_no_receipt(cfg)

    def test_second_deploy_copy_failure_cleans_both_staged_files(self):
        cfg, result, events = self.wrapper(WRAPPERS[2], copyFailureAt=2)
        self.assertFalse(result["ok"])
        self.assertIn("Failed to copy the remote installer", result["error"])
        self.assertEqual([copy["number"] for copy in self.of_kind(events, "scp")], [1, 2])
        self.assertEqual(result["fixtureState"]["copyCount"], 2)
        self.assertEqual(len(self.of_kind(events, "query")), 1)
        self.assertEqual(len(self.of_kind(events, "cleanup")), 2)
        self.assertEqual(self.of_kind(events, "invoke"), [])
        self.assert_no_receipt(cfg)

    def test_missing_scp_exit_code_is_not_success(self):
        cfg, result, events = self.wrapper(WRAPPERS[2], copyMissingExit=True)
        self.assertFalse(result["ok"])
        self.assertIn("Failed to copy", result["error"])
        self.assertEqual(self.of_kind(events, "invoke"), [])
        self.assert_no_receipt(cfg)

    def test_receipt_status_and_deploy_fingerprint_gates(self):
        for name in WRAPPERS:
            for rollback in (False, True):
                faults = ("status", "transaction", "json")
                if name == WRAPPERS[2] and not rollback:
                    faults += ("fingerprint",)
                for fault in faults:
                    with self.subTest(wrapper=name, rollback=rollback, fault=fault):
                        args = self.rollback_args(name) if rollback else self.enable_args(name)
                        cfg, result, events = self.wrapper(name, args, receiptFault=fault)
                        self.assertFalse(result["ok"])
                        if fault != "json":
                            self.assertIn("receipt", result["error"].lower())
                        self.assertEqual(len(self.of_kind(events, "query")), 1)
                        self.assertEqual(len(self.of_kind(events, "invoke")), 1)
                        copies = 2 if name == WRAPPERS[2] and not rollback else 1
                        self.assertEqual(len(self.of_kind(events, "scp")), copies)
                        self.assertEqual(len(self.of_kind(events, "cleanup")), copies)
                        self.assert_no_receipt(cfg)

    def test_snapshot_and_public_parameter_gates_precede_staging(self):
        for name in WRAPPERS[:2]:
            with self.subTest(wrapper=name):
                cfg, result, events = self.wrapper(name, snapshotExit=7)
                self.assertFalse(result["ok"])
                self.assertIn("checkpoint", result["error"])
                self.assertEqual([e["kind"] for e in events], ["mock-proof", "snapshot"])
                self.assert_no_receipt(cfg)
        for name in WRAPPERS:
            with self.subTest(invalid_host=name):
                args = self.enable_args(name)
                args["HostAlias"] = "unapproved-host"
                cfg, result, events = self.wrapper(name, args)
                self.assertFalse(result["ok"])
                self.assertEqual([e["kind"] for e in events], ["mock-proof"])
                self.assert_no_receipt(cfg)

    def test_deploy_opt_in_flags_remain_named_switches(self):
        _, result, events = self.wrapper(WRAPPERS[2], dict(AllowEnabledDesktopUpgrade=True, ApplyTemplateConfiguration=True))
        self.assert_success(result)
        bound = self.of_kind(events, "invoke")[0]["bound"]
        self.assertIs(bound["AllowEnabledDesktopUpgrade"], True)
        self.assertIs(bound["ApplyTemplateConfiguration"], True)

    def test_guest_query_rejects_unsafe_candidates_before_any_metadata_probe(self):
        cfg = self.case()
        # Inject only the filesystem command sentinel. Execute the real query
        # text, not a Python reimplementation or a rewritten validator.
        body = r"""
$query = Get-RemoteStagingQuery
$cases = @(
    @('relative/Temp', 'R:/Profiles/Alpha'),
    @('R:Temp', 'R:/Profiles/Alpha'),
    @('\\example.invalid\share\Temp', 'R:/Profiles/Alpha'),
    @('\\?\R:\Profiles\Alpha\Temp', 'R:/Profiles/Alpha'),
    @('R:/Profiles/Alpha/../Other/Temp', 'R:/Profiles/Alpha'),
    @('R:/Profiles/Alpha/Temp:stream', 'R:/Profiles/Alpha'),
    @('R:/Profiles/Alpha//Temp', 'R:/Profiles/Alpha'),
    @('R:/Profiles/Alpha/Temp//', 'R:/Profiles/Alpha'),
    @('R:/Profiles/Alpha/CON.txt', 'R:/Profiles/Alpha'),
    @('R:/Profiles/Alpha/COM1', 'R:/Profiles/Alpha'),
    @('R:/Profiles/Alpha/Temp.', 'R:/Profiles/Alpha'),
    @('R:/Profiles/Alpha/ Temp', 'R:/Profiles/Alpha'),
    @('R:/Profiles/Alpha/Temp ', 'R:/Profiles/Alpha'),
    @('R:/Profiles/Alpha/Temp;throw', 'R:/Profiles/Alpha'),
    @('R:/Profiles/Alpha/$(throw)', 'R:/Profiles/Alpha'),
    @('R:/Profiles/Alpha/Temp&whoami', 'R:/Profiles/Alpha'),
    @("R:/Profiles/Alpha/Temp`nother", 'R:/Profiles/Alpha'),
    @("R:/Profiles/Alpha/Temp`r", 'R:/Profiles/Alpha'),
    @('S:/Outside/Temp', 'R:/Profiles/Alpha'),
    @('R:/Profiles/AlphaOther/Temp', 'R:/Profiles/Alpha'),
    @('R:/Profiles/Alpha', 'R:/Profiles/Alpha'),
    @('R:/Profiles/Alpha/Temp', 'relative/profile'),
    @('', 'R:/Profiles/Alpha')
)
$global:__RemoteStagingFixtureState.MetadataProbes = 0
function global:Get-Item {
    param([string]$LiteralPath, [switch]$Force, [string]$ErrorAction)
    $global:__RemoteStagingFixtureState.MetadataProbes++
    throw 'METADATA_PROBE_SENTINEL'
}
$rows = @()
foreach ($pair in $cases) {
    $env:TEMP = $pair[0]; $env:USERPROFILE = $pair[1]
    $message = $null
    try { & ([scriptblock]::Create($query)) | Out-Null } catch { $message = $_.Exception.Message }
    $rows += @{rejected=($null -ne $message); message=$message; probes=$global:__RemoteStagingFixtureState.MetadataProbes}
}
# A valid lexical pair MUST reach the sentinel, proving interception is live.
$env:TEMP = 'R:/Profiles/Alpha/Temp'; $env:USERPROFILE = 'R:/Profiles/Alpha'
$control = $null
try { & ([scriptblock]::Create($query)) | Out-Null } catch { $control = $_.Exception.Message }
@{rows=$rows; control=$control; probes=$global:__RemoteStagingFixtureState.MetadataProbes}
"""
        result, events = self.run_ps(cfg, body)
        self.assertTrue(result["ok"], result["error"])
        report = result["data"][0]
        for row in report["rows"]:
            self.assertTrue(row["rejected"], row)
            self.assertEqual(row["probes"], 0, row)
            self.assertNotEqual(row["message"], "METADATA_PROBE_SENTINEL")
        self.assertEqual(report["control"], "METADATA_PROBE_SENTINEL")
        self.assertEqual(report["probes"], 1)
        self.assertEqual([e["kind"] for e in events], ["mock-proof"])

    def test_query_wire_failures_and_native_status_are_rejected(self):
        cfg = self.case()
        body = r"""
$original = $cfg.queryLines
$cases = @(
    @{lines=@(); exit=0}, @{lines=@('only-one'); exit=0},
    @{lines=@('one','two','three'); exit=0},
    @{lines=@(42, $original[1]); exit=0},
    @{lines=@("YQ==`n", $original[1]); exit=0},
    @{lines=@('not-base64!', $original[1]); exit=0},
    @{lines=@('/w==', $original[1]); exit=0},
    @{lines=$original; exit=19}
)
$rows = @()
foreach ($item in $cases) {
    $cfg.queryLines = $item.lines; $cfg.queryExit = $item.exit
    $errorText = $null
    try { Get-RemoteStagingRoot 'ai-lab-uat-vm' | Out-Null } catch { $errorText = $_.Exception.Message }
    $rows += @{rejected=($null -ne $errorText); error=$errorText}
}
$cfg.queryLines=$original; $cfg.queryExit=0; $cfg.queryMissingExit=$true
$missing=$null
try { Get-RemoteStagingRoot 'ai-lab-uat-vm' | Out-Null } catch { $missing=$_.Exception.Message }
@{rows=$rows; missing=$missing}
"""
        result, events = self.run_ps(cfg, body)
        self.assertTrue(result["ok"], result["error"])
        for row in result["data"][0]["rows"]:
            self.assertTrue(row["rejected"], row)
        self.assertIn("query failed", result["data"][0]["missing"])
        self.assertFalse(self.of_kind(events, "scp"))

    def test_guest_query_wire_encoding_and_reparse_metadata_guard(self):
        cfg = self.case(guestProfile=GUEST_PROFILE, guestTemp=GUEST_TEMP)
        body = r"""
$query = Get-RemoteStagingQuery
$env:TEMP = $cfg.guestTemp; $env:USERPROFILE = $cfg.guestProfile
$global:__RemoteStagingFixtureState.ProbeMode = 'safe'
$global:__RemoteStagingFixtureState.MetadataProbes = 0
function global:Get-Item {
    param([string]$LiteralPath, [switch]$Force, [string]$ErrorAction)
    $global:__RemoteStagingFixtureState.MetadataProbes++
    # DirectoryInfo keeps the real type check; synthetic PS members prevent
    # OS metadata access and simulate a reparse ancestor without symlinks.
    $leaf = New-Object IO.DirectoryInfo $global:__RemoteStagingFixtureState.Cfg.temp
    $leaf | Add-Member -MemberType NoteProperty -Name Attributes -Value ([IO.FileAttributes]::Directory) -Force
    $leaf | Add-Member -MemberType NoteProperty -Name Parent -Value $null -Force
    if ($global:__RemoteStagingFixtureState.ProbeMode -eq 'reparse') {
        $ancestor = New-Object IO.DirectoryInfo $global:__RemoteStagingFixtureState.Cfg.profile
        $ancestor | Add-Member -MemberType NoteProperty -Name Attributes -Value ([IO.FileAttributes]::ReparsePoint) -Force
        $ancestor | Add-Member -MemberType NoteProperty -Name Parent -Value $null -Force
        $leaf | Add-Member -MemberType NoteProperty -Name Parent -Value $ancestor -Force
    }
    return $leaf
}
& ([scriptblock]::Create($query))
$safeProbes = $global:__RemoteStagingFixtureState.MetadataProbes
$global:__RemoteStagingFixtureState.ProbeMode = 'reparse'
$message = $null
try { & ([scriptblock]::Create($query)) } catch { $message = $_.Exception.Message }
@{safeProbes=$safeProbes; reparseError=$message; totalProbes=$global:__RemoteStagingFixtureState.MetadataProbes}
"""
        result, events = self.run_ps(cfg, body)
        self.assertTrue(result["ok"], result["error"])
        report = result["data"][0]
        self.assertEqual(report["safeProbes"], 2)
        self.assertEqual(report["totalProbes"], 3)
        self.assertIn("reparse point", report["reparseError"])
        wire = result["child_stdout"].splitlines()
        self.assertEqual([base64.b64decode(line).decode("utf-8") for line in wire], [GUEST_TEMP, GUEST_PROFILE])
        self.assertEqual([e["kind"] for e in events], ["mock-proof"])

    def test_literal_named_argument_binding_executes_only_inert_local_receiver(self):
        cfg = self.case()
        receiver = Path(cfg["owned"]) / "inert receiver.ps1"
        receiver.write_text(
            "param([string]$Text,[switch]$Enable,[string]$Empty,[string]$Tag)\n"
            "[ordered]@{Text=$Text;Enable=[bool]$Enable;Empty=$Empty;Tag=$Tag}|ConvertTo-Json -Compress\n",
            encoding="ascii",
        )
        cfg["receiver"] = str(receiver)
        body = r"""
$tag = 'Unicode ' + [char]0x017B + ' '' ; $(throw ''injection'') # & |'
$invocation = Invoke-RemoteStagedScript -HostAlias 'ai-lab-uat-vm' -ScriptPath $cfg.receiver.Replace('\','/') -Arguments @('-Text','-Enable','-Enable','-Empty','','-Tag',$tag)
@{exit=$invocation.ExitCode; actual=(($invocation.Output -join "`n") | ConvertFrom-Json); expectedTag=$tag}
"""
        result, events = self.run_ps(cfg, body)
        self.assertTrue(result["ok"], result["error"])
        report = result["data"][0]
        self.assertEqual(report["exit"], 0)
        self.assertEqual(report["actual"], dict(Text="-Enable", Enable=True, Empty="", Tag=report["expectedTag"]))
        self.assertIn("\u017b", report["actual"]["Tag"])
        invocation = self.of_kind(events, "invoke")
        self.assertEqual(len(invocation), 1)
        self.assertEqual(base64.b64decode(invocation[0]["argv"][-1]).decode("utf-16le"), invocation[0]["text"])
        self.assertFalse(self.of_kind(events, "query"))

    def test_argument_validation_precedes_remote_effects(self):
        cfg = self.case()
        body = r"""
$cases = @(
    { ConvertTo-RemoteBoundParameters @('-Text') },
    { ConvertTo-RemoteBoundParameters @('positional') },
    { ConvertTo-RemoteBoundParameters @('-Text','one','-Text','two') },
    { New-RemoteInvocationText 'R:/Profiles/Alpha/Temp/script.ps1' @{'bad;name'='value'} },
    { New-RemoteInvocationText 'R:/Profiles/Alpha/Temp/script.ps1' @{Text="one`ntwo"} },
    { Invoke-RemoteStagedScript -HostAlias "host`n" -ScriptPath 'R:/Profiles/Alpha/Temp/script.ps1' },
    { Invoke-RemoteStagedScript -HostAlias 'ai-lab-uat-vm' -ScriptPath 'R:/Profiles/../script.ps1' },
    { Invoke-RemoteStagedScript -HostAlias 'ai-lab-uat-vm' -ScriptPath 'R:/Profiles/Alpha/Temp/script.ps1' -Arguments @('-Enable') -BoundParameters @{Enable=$true} },
    { Join-RemoteStagingPath 'R:/Profiles/Alpha/Temp' '../script.ps1' }
)
foreach ($case in $cases) {
    $rejected = $false
    try { & $case | Out-Null } catch { $rejected = $true }
    @{rejected=$rejected}
}
"""
        result, events = self.run_ps(cfg, body)
        self.assertTrue(result["ok"], result["error"])
        self.assertTrue(all(row["rejected"] for row in result["data"]))
        self.assertEqual([e["kind"] for e in events], ["mock-proof"])


if __name__ == "__main__":
    unittest.main()
