import json
import os
import shutil
import subprocess
import sys
import time
import uuid

import pytest

TOOL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, TOOL_DIR)
import sandbox_terminal as st  # noqa: E402

SCRIPT = os.path.join(TOOL_DIR, "sandbox_terminal.py")
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _docker(*args, **kw):
    return subprocess.run([st.docker_bin(), *args], stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, creationflags=NO_WINDOW, **kw)


def _docker_available():
    if shutil.which(st.docker_bin()) is None and not os.path.exists(st.docker_bin()):
        return False
    try:
        r = _docker("info", timeout=15)
        if r.returncode != 0:
            return False
        r = _docker("image", "inspect", st.image_name(), timeout=15)
        return r.returncode == 0
    except Exception:
        return False


DOCKER_OK = _docker_available()
requires_docker = pytest.mark.skipif(not DOCKER_OK, reason="local Docker/image unavailable")


# ---------------- unit: validation & paths (no Docker needed) ----------------
@pytest.mark.parametrize("bad", ["", "A", "UPPER", "a b", "a/b", "a_b", "..", "x" * 33, "a.b", "-ok-".upper()])
def test_invalid_session_names(bad):
    with pytest.raises(st.SandboxError):
        st.validate_session(bad)


@pytest.mark.parametrize("good", ["a", "abc-123", "x" * 32, "0-9"])
def test_valid_session_names(good):
    assert st.validate_session(good) == good


@pytest.mark.parametrize("bad", ["../etc/passwd", "/etc/passwd", "/workspace/../etc/passwd",
                                 "..", "/workspace/../../x", "a/../../b", "/tmp/x", ""])
def test_path_traversal_rejected(bad):
    with pytest.raises(st.SandboxError):
        st.validate_workspace_path(bad)


@pytest.mark.parametrize("given,expected", [
    ("/workspace/a.txt", "/workspace/a.txt"),
    ("a.txt", "/workspace/a.txt"),
    ("sub/dir/f.txt", "/workspace/sub/dir/f.txt"),
    ("/workspace/sub/./f.txt", "/workspace/sub/f.txt"),
])
def test_workspace_paths_ok(given, expected):
    assert st.validate_workspace_path(given) == expected


def test_truncation():
    data, trunc = st.truncate_output(b"A" * (st.MAX_OUTPUT + 10))
    assert trunc and len(data) == st.MAX_OUTPUT
    data, trunc = st.truncate_output(b"hi")
    assert not trunc and data == b"hi"


def _mcp_session(proc, requests):
    out = []
    for req in requests:
        proc.stdin.write(json.dumps(req) + "\n")
        proc.stdin.flush()
        if req.get("id") is not None:
            line = proc.stdout.readline()
            assert line, "MCP server closed stdout"
            out.append(json.loads(line))
    return out


def test_mcp_initialize_and_tools_list():
    proc = subprocess.Popen([sys.executable, SCRIPT, "mcp"], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, creationflags=NO_WINDOW)
    try:
        resp = _mcp_session(proc, [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2024-11-05"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        ])
        assert resp[0]["result"]["serverInfo"]["name"] == "sandbox-terminal"
        names = {t["name"] for t in resp[1]["result"]["tools"]}
        assert {"create", "exec", "write_file", "read_file", "status", "destroy", "list"} <= names
    finally:
        proc.stdin.close()
        proc.terminate()
        proc.wait(timeout=10)


# ---------------- integration on real Docker ----------------
@pytest.fixture()
def session():
    name = "t" + uuid.uuid4().hex[:12]
    created = False
    try:
        st.create(name)
        created = True
        yield name
    finally:
        # Clean up only this test's uniquely named resources.
        try:
            if created or st._inspect(st.container_name(name)):
                st.destroy(name)
        except Exception:
            _docker("rm", "-f", st.container_name(name))
            _docker("volume", "rm", "-f", st.volume_name(name))


@requires_docker
def test_container_flags_inspected(session):
    info = st._inspect(st.container_name(session))
    assert info["Config"]["Labels"]["agent-stack.sandbox-terminal"] == "1"
    assert info["Config"]["User"] == "10001:10001"
    assert info["Config"]["Image"] == st.image_name()
    hc = info["HostConfig"]
    assert hc["NetworkMode"] == "none"
    assert hc["ReadonlyRootfs"] is True
    assert "ALL" in (hc.get("CapDrop") or [])
    assert any("no-new-privileges" in s for s in (hc.get("SecurityOpt") or []))
    assert hc["PidsLimit"] == 128
    assert hc["Memory"] == 512 * 1024 * 1024
    assert hc["NanoCpus"] == 1_000_000_000
    mounts = info.get("Mounts") or []
    assert len(mounts) == 1
    assert mounts[0]["Type"] == "volume"
    assert mounts[0]["Name"] == st.volume_name(session)
    assert mounts[0]["Destination"] == "/workspace"
    assert not hc.get("Binds")
    tmpfs = hc.get("Tmpfs") or {}
    assert "/tmp" in tmpfs and "size=64m" in tmpfs["/tmp"]


@requires_docker
def test_no_network_and_uid(session):
    r = st.exec_command(session, ["python3", "-c",
                                  "import socket; socket.create_connection(('1.1.1.1', 53), timeout=3)"])
    assert r["exit_code"] not in (0, None)
    r = st.exec_command(session, ["id", "-u"])
    assert r["stdout"].strip() == "10001"


def _labeled_volume(labels=None):
    if labels is None:
        labels = {st.LABEL_KEY: "1"}
    return json.dumps([{"Name": "v", "Labels": labels}]).encode()


def _volume_fake(monkeypatch, inspect_results, create_rc=0):
    """inspect_results: CompletedProcess-like (rc, stdout, stderr) per volume inspect call."""
    calls, queue = [], list(inspect_results)

    def fake_docker(args, **kwargs):
        calls.append(list(args))
        if list(args[:2]) == ["volume", "inspect"]:
            rc, out, err = queue.pop(0)
            return subprocess.CompletedProcess(args, rc, out, err)
        if list(args[:2]) == ["volume", "create"]:
            return subprocess.CompletedProcess(args, create_rc, b"", b"")
        raise AssertionError(f"unexpected docker call {args}")

    monkeypatch.setattr(st, "_docker", fake_docker)
    return calls


def test_volume_ambiguous_inspect_error_fails_closed(monkeypatch):
    calls = _volume_fake(monkeypatch, [(1, b"", b"Cannot connect to the Docker daemon")])
    with pytest.raises(st.SandboxError):
        st._ensure_workspace_volume("amb1")
    assert not any(c[:2] == ["volume", "create"] for c in calls)


@pytest.mark.parametrize("inspect_result", [(1, b'[{"Labels": {}}]', b"no such volume"),
                                            (1, b"", b"no such volume")])
def test_volume_inconsistent_missing_report_fails_closed(monkeypatch, inspect_result):
    calls = _volume_fake(monkeypatch, [inspect_result])
    with pytest.raises(st.SandboxError):
        st._ensure_workspace_volume("amb2")
    assert not any(c[:2] == ["volume", "create"] for c in calls)


def test_volume_created_concurrently_by_someone_else_is_not_adopted(monkeypatch):
    # `volume create` is idempotent: a foreign unlabeled volume appearing between
    # inspect and create must be refused, never reported as created by us.
    _volume_fake(monkeypatch, [(1, b"[]", b"Error: No such volume: sbx-race1-ws"),
                               (0, _labeled_volume({}), b"")])
    with pytest.raises(st.SandboxError, match="unlabeled"):
        st._ensure_workspace_volume("race1")


def test_volume_created_here_is_reported_created(monkeypatch):
    _volume_fake(monkeypatch, [(1, b"[]", b"Error: No such volume: sbx-new1-ws"), (0, _labeled_volume(), b"")])
    assert st._ensure_workspace_volume("new1") is True


def test_existing_labeled_volume_is_reused_not_created(monkeypatch):
    calls = _volume_fake(monkeypatch, [(0, _labeled_volume(), b"")])
    assert st._ensure_workspace_volume("old1") is False
    assert not any(c[:2] == ["volume", "create"] for c in calls)


@pytest.mark.parametrize("payload", [b"not json", b"{}", b"[]", b'[{"Labels": ["a"]}]',
                                     b'[{"Labels": "x"}]', b'[{"Labels": 1}]', b'["x"]',
                                     b'[{"Labels": {}}, {"Labels": {}}]'])
def test_volume_malformed_inspect_payload_fails_closed(monkeypatch, payload):
    _volume_fake(monkeypatch, [(0, payload, b"")])
    with pytest.raises(st.SandboxError):
        st._ensure_workspace_volume("bad1")


def _destroy_fake(monkeypatch, inspect_result):
    calls = []

    def fake_docker(args, **kwargs):
        calls.append(list(args))
        if list(args[:2]) == ["volume", "inspect"]:
            rc, out, err = inspect_result
            return subprocess.CompletedProcess(args, rc, out, err)
        return subprocess.CompletedProcess(args, 0, b"", b"")

    monkeypatch.setattr(st, "_docker", fake_docker)
    monkeypatch.setattr(st, "_require_labeled", lambda session: {})
    return calls


def _removals(calls):
    return [c for c in calls if c[:1] == ["rm"] or c[:2] == ["volume", "rm"]]


@pytest.mark.parametrize("inspect_result", [
    (0, _labeled_volume({}), b""),                          # same-named foreign volume
    (0, b"[{\"Labels\": [1]}]", b""),                       # malformed labels
    (1, b"", b"Cannot connect to the Docker daemon"),       # ambiguous inspect
    (1, b'[{"Labels": {}}]', b"no such volume"),            # "missing" text but a volume in stdout
    (1, b"", b"no such volume"),                            # "missing" text without Docker's []
    (1, b"garbage", b"no such volume"),
])
def test_destroy_refuses_before_any_removal(monkeypatch, inspect_result):
    calls = _destroy_fake(monkeypatch, inspect_result)
    with pytest.raises(st.SandboxError):
        st.destroy("dst1")
    assert _removals(calls) == []


def test_destroy_removes_container_only_when_volume_already_gone(monkeypatch):
    calls = _destroy_fake(monkeypatch, (1, b"[]", b"Error: No such volume: sbx-dst2-ws"))
    result = st.destroy("dst2")
    assert _removals(calls) == [["rm", "-f", "sbx-dst2"]]
    assert result["volume_removed"] is None


def test_destroy_removes_own_labeled_volume(monkeypatch):
    calls = _destroy_fake(monkeypatch, (0, _labeled_volume(), b""))
    result = st.destroy("dst3")
    assert _removals(calls) == [["rm", "-f", "sbx-dst3"], ["volume", "rm", "sbx-dst3-ws"]]
    assert result["volume_removed"] == "sbx-dst3-ws"


def test_create_chowns_volume_after_run(monkeypatch):
    # Wiring guard: create() must chown the volume to 10001:10001 (one-off,
    # network-less helper with only CAP_CHOWN) AFTER the session container
    # is created -- an earlier chown is undone when Docker populates the
    # still-empty volume from the image at container creation.
    calls = []

    def fake_docker(args, **kwargs):
        calls.append(list(args))
        if list(args[:2]) == ["volume", "inspect"]:
            if not any(c[:2] == ["volume", "create"] for c in calls):
                return subprocess.CompletedProcess(args, 1, b"[]", b"Error: No such volume: x")
            return subprocess.CompletedProcess(args, 0, _labeled_volume(), b"")
        return subprocess.CompletedProcess(args, 0, b"", b"")

    monkeypatch.setattr(st, "_docker", fake_docker)
    monkeypatch.setattr(st, "_inspect", lambda name: None)
    monkeypatch.setattr(st, "status", lambda session: {"session": session})
    assert st.create("wiring1") == {"session": "wiring1"}
    flat = [" ".join(c) for c in calls]
    init_idx = next(i for i, c in enumerate(flat) if "chown" in c and "10001:10001" in c)
    run_idx = next(i for i, c in enumerate(flat) if "--name sbx-wiring1" in c)
    assert run_idx < init_idx
    init = calls[init_idx]
    assert "--pull" in init and "never" in init
    assert "--network" in init and "none" in init
    assert "--cap-add" in init and "CHOWN" in init
    assert any("type=volume,source=sbx-wiring1-ws,target=/workspace" in a for a in init)


@requires_docker
def test_workspace_owned_by_sandbox_user(session):
    # Regression: a fresh named volume is root-owned, so create() must hand
    # /workspace over to the sandbox uid or every workspace write fails.
    r = st.exec_command(session, ["stat", "-c", "%u:%g", "/workspace"])
    assert r["exit_code"] == 0 and r["stdout"].strip() == "10001:10001"
    r = st.exec_command(session, ["touch", "/workspace/.writable"])
    assert r["exit_code"] == 0


@requires_docker
def test_no_write_outside_workspace_and_tmp(session):
    r = st.exec_command(session, "touch /etc/pwned; echo rc=$?")
    assert "rc=0" not in r["stdout"]
    r = st.exec_command(session, "touch /usr/pwned; echo rc=$?")
    assert "rc=0" not in r["stdout"]
    r = st.exec_command(session, "echo ok > /workspace/a.txt && echo ok > /tmp/b.txt && cat /workspace/a.txt")
    assert r["exit_code"] == 0 and "ok" in r["stdout"]


@requires_docker
def test_timeout_kills(session):
    start = time.time()
    r = st.exec_command(session, ["sleep", "10"], timeout=1)
    elapsed = time.time() - start
    assert r["timed_out"] is True
    assert elapsed < 8
    # Container still usable after timeout.
    r = st.exec_command(session, ["echo", "alive"])
    assert "alive" in r["stdout"]


@requires_docker
def test_output_truncation(session):
    r = st.exec_command(session, ["python3", "-c", "print('A' * 200000, end='')"])
    assert r["truncated"] is True
    assert len(r["stdout"].encode()) <= st.MAX_OUTPUT


@requires_docker
def test_file_roundtrip_and_traversal(session):
    st.write_file(session, "hello.txt", "hello sandbox")
    out = st.read_file(session, "/workspace/hello.txt")
    assert out["content"] == "hello sandbox"
    with pytest.raises(st.SandboxError):
        st.write_file(session, "../evil.txt", "x")
    with pytest.raises(st.SandboxError):
        st.read_file(session, "/etc/passwd")
    with pytest.raises(st.SandboxError):
        st.write_file(session, "big.bin", "x" * (st.MAX_FILE + 1))


@requires_docker
def test_destroy_removes_only_own():
    victim = "t" + uuid.uuid4().hex[:12]
    name = st.container_name(victim)
    # Unlabeled container occupying the would-be sandbox name (created by this test).
    r = _docker("run", "-d", "--name", name, st.image_name(), "sleep", "infinity")
    assert r.returncode == 0
    try:
        with pytest.raises(st.SandboxError):
            st.destroy(victim)
        assert st._inspect(name) is not None  # untouched
    finally:
        _docker("rm", "-f", name)
    # Labeled session destroys container + volume.
    own = "t" + uuid.uuid4().hex[:12]
    st.create(own)
    st.destroy(own)
    assert st._inspect(st.container_name(own)) is None
    rv = _docker("volume", "inspect", st.volume_name(own))
    assert rv.returncode != 0


@requires_docker
def test_create_refuses_unlabeled_same_named_volume():
    victim = "t" + uuid.uuid4().hex[:12]
    vol = st.volume_name(victim)
    # Unlabeled volume occupying the would-be workspace name (created by this test).
    assert _docker("volume", "create", vol).returncode == 0
    try:
        with pytest.raises(st.SandboxError):
            st.create(victim)
        assert st._inspect(st.container_name(victim)) is None
        assert _docker("volume", "inspect", vol).returncode == 0  # untouched
    finally:
        _docker("volume", "rm", "-f", vol)


@requires_docker
def test_mcp_tools_call_exec(session):
    proc = subprocess.Popen([sys.executable, SCRIPT, "mcp"], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, creationflags=NO_WINDOW)
    try:
        resp = _mcp_session(proc, [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "exec", "arguments": {
                 "session": session, "command": ["echo", "mcp-hello"]}}},
        ])
        payload = json.loads(resp[-1]["result"]["content"][0]["text"])
        assert resp[-1]["result"]["isError"] is False
        assert "mcp-hello" in payload["stdout"]
    finally:
        proc.stdin.close()
        proc.terminate()
        proc.wait(timeout=10)


@requires_docker
def test_list_includes_session(session):
    assert session in st.list_sessions()["sessions"]
