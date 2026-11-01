"""Synthetic Module 1 tests: no provider is ever executed, no network is used."""

from __future__ import annotations

import json
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from coordfs.cli_dispatch import CliDispatches
from coordfs.errors import CoordError


ALLOW = lambda record: None  # noqa: E731


@pytest.fixture
def tmp_path():
    # pytest's default temp root is unreadable in the isolated runner.
    # Keep synthetic artifacts under this worktree, with no cleanup in this run.
    path = Path(__file__).parent / "dispatch-test-data" / uuid.uuid4().hex
    path.mkdir(parents=True)
    return path


class Clock:
    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def binding(**overrides):
    base = {
        "binding_id": "bind-1",
        "version": 1,
        "execution_host": "host-a",
        "executor_agent": "agent-one",
        "driver": "codex-cli",
        "native_thread_id": "thread-1",
        "workspace": "C:\\work\\one",
        "workspace_flavor": "windows",
    }
    base.update(overrides)
    return base


def store(home: Path, clock: Clock, eligibility=ALLOW) -> CliDispatches:
    return CliDispatches(home, now=clock, eligibility=eligibility)


def enqueue(store_: CliDispatches, **overrides):
    kwargs = {"trigger": "message", "trigger_ref": "ref-1"}
    kwargs.update(overrides)
    return store_.enqueue(binding(), **kwargs)


def full_cycle(store_: CliDispatches, dispatch_id: str, request: str, lease: str):
    return store_.receipt(
        dispatch_id, peer_host="host-a", executor_agent="agent-one",
        lease_id=lease, status="accepted", exit_code=0,
    )


@pytest.mark.parametrize("trigger", ["message", "child-terminal", "schedule"])
def test_enqueue_pending_all_triggers(tmp_path, trigger) -> None:
    clock = Clock()
    dispatch = store(tmp_path, clock)
    row = dispatch.enqueue(binding(), trigger=trigger, trigger_ref="t-1",
                           correlation_id="corr-1")
    assert row["trigger"] == trigger
    assert row["state"] == "pending"
    assert row["correlation_id"] == "corr-1"
    assert set(row) == {
        "dispatch_id", "binding_id", "version", "execution_host",
        "executor_agent", "driver", "native_thread_id", "workspace",
        "workspace_flavor", "trigger", "trigger_ref", "correlation_id",
        "state", "enqueued_at", "expires_at",
    }
    rows = dispatch.pending(peer_host="host-a", executor_agent="agent-one")
    assert [r["dispatch_id"] for r in rows] == [row["dispatch_id"]]
    assert "prompt" not in json.dumps(row)


def test_posix_workspace_accepted(tmp_path) -> None:
    clock = Clock()
    dispatch = store(tmp_path, clock)
    row = dispatch.enqueue(
        binding(workspace="/srv/one", workspace_flavor="posix"),
        trigger="schedule", trigger_ref="s-1",
    )
    assert row["workspace"] == "/srv/one"


def test_wrong_host_and_actor_refused_with_known_id(tmp_path) -> None:
    clock = Clock()
    dispatch = store(tmp_path, clock)
    row = enqueue(dispatch)
    dispatch_id = row["dispatch_id"]
    assert dispatch.pending(peer_host="host-b", executor_agent="agent-one") == []
    assert dispatch.pending(peer_host="host-a", executor_agent="agent-two") == []
    with pytest.raises(CoordError):
        dispatch.pending(peer_host=None, executor_agent="agent-one")
    with pytest.raises(CoordError):
        dispatch.pending(peer_host="host-a", executor_agent=None)
    request = "a" * 32
    with pytest.raises(CoordError) as error:
        dispatch.claim(dispatch_id, peer_host="host-b",
                       executor_agent="agent-one", request_id=request)
    assert error.value.code == "forbidden"
    with pytest.raises(CoordError) as error:
        dispatch.claim(dispatch_id, peer_host="host-a",
                       executor_agent="agent-two", request_id=request)
    assert error.value.code == "forbidden"
    claim = dispatch.claim(dispatch_id, peer_host="host-a",
                           executor_agent="agent-one", request_id=request)
    with pytest.raises(CoordError) as error:
        dispatch.start(dispatch_id, peer_host="host-b",
                       executor_agent="agent-one",
                       lease_id=claim["lease_id"], request_id=request)
    assert error.value.code == "forbidden"
    with pytest.raises(CoordError):
        dispatch.receipt(dispatch_id, peer_host="host-a",
                         executor_agent="agent-two",
                         lease_id=claim["lease_id"], status="accepted")


@pytest.mark.parametrize("workspace,flavor", [
    ("relative/one", "windows"),
    ("C:drive-relative", "windows"),
    ("C:\\a\\..\\b", "windows"),
    ("/srv/../x", "posix"),
    ("", "posix"),
    ("/", "posix"),
    ("C:\\", "windows"),
    ("C:\\bad\x00path", "windows"),
    ("/srv/bad\npath", "posix"),
    ("C:\\work\\one", "plan9"),
    ("/srv/one", "windows"),
    ("C:\\work\\one", "posix"),
])
def test_invalid_workspaces_refused(tmp_path, workspace, flavor) -> None:
    dispatch = store(tmp_path, Clock())
    with pytest.raises(CoordError) as error:
        dispatch.enqueue(binding(workspace=workspace, workspace_flavor=flavor),
                         trigger="message", trigger_ref="r-1")
    assert error.value.code == "invalid_workspace"


def test_stop_revoked_changed_expired_binding(tmp_path) -> None:
    clock = Clock()
    stopped = store(tmp_path, clock, eligibility=lambda record: "STOP")
    row = enqueue(store(tmp_path, clock))
    with pytest.raises(CoordError) as error:
        stopped.claim(row["dispatch_id"], peer_host="host-a",
                      executor_agent="agent-one", request_id="c" * 32)
    assert error.value.code == "refused"
    # Revoked binding: pending hides it, claim refuses it.
    revoked = store(tmp_path, clock, eligibility=lambda record: "revoked")
    assert revoked.pending(peer_host="host-a", executor_agent="agent-one") == []
    with pytest.raises(CoordError) as error:
        revoked.claim(row["dispatch_id"], peer_host="host-a",
                      executor_agent="agent-one", request_id="c" * 32)
    assert error.value.code == "refused"
    # STOP blocks start but leaves the claim intact and unauthorised.
    live = store(tmp_path, clock)
    claim = live.claim(row["dispatch_id"], peer_host="host-a",
                       executor_agent="agent-one", request_id="c" * 32)
    with pytest.raises(CoordError) as error:
        stopped.start(row["dispatch_id"], peer_host="host-a",
                      executor_agent="agent-one",
                      lease_id=claim["lease_id"], request_id="c" * 32)
    assert error.value.code == "refused"
    started = live.start(row["dispatch_id"], peer_host="host-a",
                         executor_agent="agent-one",
                         lease_id=claim["lease_id"], request_id="c" * 32)
    assert started == {
        "status": "authorized", "dispatch_id": row["dispatch_id"],
        "lease_id": claim["lease_id"], "first_start": True,
    }
    # Changed version is a different dispatch, never the same record.
    other = live.enqueue(binding(version=2), trigger="message", trigger_ref="ref-1")
    assert other["dispatch_id"] != row["dispatch_id"]
    # Expired dispatch is retained but not runnable.
    short = live.enqueue(binding(binding_id="short"), trigger="schedule",
                         trigger_ref="s-9", ttl_s=1)
    clock.advance(2)
    assert live.pending(peer_host="host-a", executor_agent="agent-one",
                        limit=20) != []
    with pytest.raises(CoordError) as error:
        live.claim(short["dispatch_id"], peer_host="host-a",
                   executor_agent="agent-one", request_id="d" * 32)
    assert error.value.code == "dispatch_expired"
    assert (tmp_path / "wake" / "cli-dispatches" / f"{short['dispatch_id']}.json").exists()


def test_duplicate_enqueue_returns_same_and_conflict_refuses(tmp_path) -> None:
    dispatch = store(tmp_path, Clock())
    first = enqueue(dispatch)
    again = enqueue(dispatch)
    assert again == first
    with pytest.raises(CoordError) as error:
        dispatch.enqueue(binding(driver="claude-code-cli"),
                         trigger="message", trigger_ref="ref-1")
    assert error.value.code == "dispatch_conflict"
    with pytest.raises(CoordError) as error:
        dispatch.enqueue(binding(), trigger="message", trigger_ref="ref-1",
                         correlation_id="other")
    assert error.value.code == "dispatch_conflict"


def test_exact_repeat_after_clock_advance_returns_same_record(tmp_path) -> None:
    clock = Clock()
    dispatch = store(tmp_path, clock)
    first = enqueue(dispatch)
    clock.advance(10)
    again = enqueue(dispatch)
    assert again == first
    # Same id but different ttl is a changed payload, never the same record.
    with pytest.raises(CoordError) as error:
        dispatch.enqueue(binding(), trigger="message", trigger_ref="ref-1",
                         ttl_s=60)
    assert error.value.code == "dispatch_conflict"


def test_claim_replay_and_conflict(tmp_path) -> None:
    dispatch = store(tmp_path, Clock())
    row = enqueue(dispatch)
    request = "e" * 32
    first = dispatch.claim(row["dispatch_id"], peer_host="host-a",
                           executor_agent="agent-one", request_id=request)
    assert first["replayed"] is False
    assert first["target"]["native_thread_id"] == "thread-1"
    assert "lease_id" in first and "deadline" in first
    replay = dispatch.claim(row["dispatch_id"], peer_host="host-a",
                            executor_agent="agent-one", request_id=request)
    assert replay["replayed"] is True
    assert replay["lease_id"] == first["lease_id"]
    with pytest.raises(CoordError) as error:
        dispatch.claim(row["dispatch_id"], peer_host="host-a",
                       executor_agent="agent-one", request_id="f" * 32)
    assert error.value.code == "claim_conflict"
    for bad in ["short", "F" * 32, "g" * 32, 123]:
        with pytest.raises(CoordError):
            dispatch.claim(row["dispatch_id"], peer_host="host-a",
                           executor_agent="agent-one", request_id=bad)


def test_two_executors_race_single_winner(tmp_path) -> None:
    dispatch = store(tmp_path, Clock())
    row = enqueue(dispatch)
    winners: list[str] = []
    failures: list[str] = []
    barrier = threading.Barrier(8)

    def attempt(index: int) -> None:
        barrier.wait()
        try:
            claim = dispatch.claim(row["dispatch_id"], peer_host="host-a",
                                   executor_agent="agent-one",
                                   request_id=f"{index:032x}")
            winners.append(claim["lease_id"])
        except CoordError as error:
            failures.append(error.code)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(attempt, range(8)))
    assert len(winners) == 1
    assert failures == ["claim_conflict"] * 7


def test_two_executors_distinct_dispatches(tmp_path) -> None:
    dispatch = store(tmp_path, Clock())
    first = dispatch.enqueue(binding(binding_id="r-a"), trigger="message",
                             trigger_ref="same")
    second = dispatch.enqueue(binding(binding_id="r-b"), trigger="message",
                              trigger_ref="same")
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(
            lambda item: dispatch.claim(item[0], peer_host="host-a",
                                        executor_agent="agent-one",
                                        request_id=item[1]),
            [(first["dispatch_id"], "1" * 32), (second["dispatch_id"], "2" * 32)],
        ))
    assert {c["dispatch_id"] for c in claims} == {
        first["dispatch_id"], second["dispatch_id"]}


def test_concurrent_duplicate_enqueue(tmp_path) -> None:
    dispatch = store(tmp_path, Clock())
    with ThreadPoolExecutor(max_workers=8) as pool:
        rows = list(pool.map(lambda _: enqueue(dispatch), range(8)))
    assert {row["dispatch_id"] for row in rows} == {rows[0]["dispatch_id"]}


def test_expired_lease_start_is_delivery_unknown(tmp_path) -> None:
    clock = Clock()
    dispatch = store(tmp_path, clock)
    row = enqueue(dispatch)
    request = "9" * 32
    claim = dispatch.claim(row["dispatch_id"], peer_host="host-a",
                           executor_agent="agent-one", request_id=request)
    clock.advance(61)
    outcome = dispatch.start(row["dispatch_id"], peer_host="host-a",
                             executor_agent="agent-one",
                             lease_id=claim["lease_id"], request_id=request)
    assert outcome["status"] == "delivery_unknown"
    assert outcome["first_start"] is False
    settled = dispatch.receipt(row["dispatch_id"], peer_host="host-a",
                               executor_agent="agent-one",
                               lease_id=claim["lease_id"],
                               status="delivery_unknown")
    assert settled["final"] is False


def test_lost_start_response_replay_never_relaunches(tmp_path) -> None:
    dispatch = store(tmp_path, Clock())
    row = enqueue(dispatch)
    request = "8" * 32
    claim = dispatch.claim(row["dispatch_id"], peer_host="host-a",
                           executor_agent="agent-one", request_id=request)
    first = dispatch.start(row["dispatch_id"], peer_host="host-a",
                           executor_agent="agent-one",
                           lease_id=claim["lease_id"], request_id=request)
    assert first["first_start"] is True
    # Lost reply retried with identical ids: authorized, but no second launch.
    replay = dispatch.start(row["dispatch_id"], peer_host="host-a",
                            executor_agent="agent-one",
                            lease_id=claim["lease_id"], request_id=request)
    assert replay == {**first, "first_start": False}
    with pytest.raises(CoordError) as error:
        dispatch.start(row["dispatch_id"], peer_host="host-a",
                       executor_agent="agent-one",
                       lease_id="0" * 32, request_id=request)
    assert error.value.code == "lease_mismatch"


def test_receipt_lifecycle_and_restart(tmp_path) -> None:
    clock = Clock()
    dispatch = store(tmp_path, clock)
    row = enqueue(dispatch)
    request = "7" * 32
    claim = dispatch.claim(row["dispatch_id"], peer_host="host-a",
                           executor_agent="agent-one", request_id=request)
    dispatch.start(row["dispatch_id"], peer_host="host-a",
                   executor_agent="agent-one",
                   lease_id=claim["lease_id"], request_id=request)
    with pytest.raises(CoordError) as error:
        dispatch.receipt(row["dispatch_id"], peer_host="host-a",
                         executor_agent="agent-one",
                         lease_id=claim["lease_id"], status="done")
    assert error.value.code == "invalid_receipt"
    accepted = full_cycle(dispatch, row["dispatch_id"], request, claim["lease_id"])
    assert accepted["status"] == "accepted"
    assert accepted["final"] is True
    # Restart: the retained terminal result replays without another launch.
    reopened = store(tmp_path, clock)
    replay = full_cycle(reopened, row["dispatch_id"], request, claim["lease_id"])
    assert replay["replayed"] is True
    assert replay["status"] == "accepted"
    with pytest.raises(CoordError) as error:
        reopened.receipt(row["dispatch_id"], peer_host="host-a",
                         executor_agent="agent-one",
                         lease_id=claim["lease_id"], status="rejected",
                         exit_code=1)
    assert error.value.code == "receipt_conflict"
    assert reopened.pending(peer_host="host-a", executor_agent="agent-one") == []
    record_path = tmp_path / "wake" / "cli-dispatches" / f"{row['dispatch_id']}.json"
    assert record_path.exists()


def test_late_receipt_settles_unknown_after_start_timeout(tmp_path) -> None:
    clock = Clock()
    dispatch = store(tmp_path, clock)
    row = enqueue(dispatch)
    request = "6" * 32
    claim = dispatch.claim(row["dispatch_id"], peer_host="host-a",
                           executor_agent="agent-one", request_id=request)
    dispatch.start(row["dispatch_id"], peer_host="host-a",
                   executor_agent="agent-one",
                   lease_id=claim["lease_id"], request_id=request)
    clock.advance(301)
    assert dispatch.pending(peer_host="host-a", executor_agent="agent-one") == []
    settled = dispatch.receipt(row["dispatch_id"], peer_host="host-a",
                               executor_agent="agent-one",
                               lease_id=claim["lease_id"], status="accepted",
                               exit_code=0)
    assert settled["status"] == "accepted"
    assert settled["final"] is True
    with pytest.raises(CoordError):
        dispatch.receipt(row["dispatch_id"], peer_host="host-a",
                         executor_agent="agent-one",
                         lease_id="1" * 32, status="accepted")


def test_corrupt_symlink_oversized_fail_closed(tmp_path) -> None:
    clock = Clock()
    dispatch = store(tmp_path, clock)
    row = enqueue(dispatch)
    root = tmp_path / "wake" / "cli-dispatches"
    (root / "active.json").write_text("{broken", encoding="utf-8")
    with pytest.raises(CoordError) as error:
        dispatch.pending(peer_host="host-a", executor_agent="agent-one")
    assert error.value.code == "corrupt_state"
    (root / "active.json").write_text('{"active":[]}', encoding="utf-8")
    target = root / f"{row['dispatch_id']}.json"
    payload = target.read_bytes()
    real_is_symlink = Path.is_symlink
    try:
        target.unlink()
        link = root / f"{row['dispatch_id']}.json"
        try:
            link.symlink_to(root / "active.json")
        except OSError:
            # No privilege to plant a real symlink: emulate reparse state at
            # the exact check the store performs, on this record only.
            target.write_bytes(payload)

            def fake_is_symlink(self, *, _link=link, _real=real_is_symlink):
                if self == _link:
                    return True
                return _real(self)

            Path.is_symlink = fake_is_symlink  # type: ignore[method-assign]
        with pytest.raises(CoordError) as error:
            dispatch.claim(row["dispatch_id"], peer_host="host-a",
                           executor_agent="agent-one", request_id="5" * 32)
        assert error.value.code == "corrupt_state"
    finally:
        Path.is_symlink = real_is_symlink  # type: ignore[method-assign]
        for stray in (root / f"{row['dispatch_id']}.json",):
            try:
                if stray.is_symlink() or stray.exists():
                    stray.unlink()
            except OSError:
                pass
    target.write_bytes(b"x" * (64 * 1024 + 1))
    with pytest.raises(CoordError) as error:
        dispatch.claim(row["dispatch_id"], peer_host="host-a",
                       executor_agent="agent-one", request_id="5" * 32)
    assert error.value.code == "corrupt_state"
    target.write_bytes(payload)


def test_queue_bounds(tmp_path) -> None:
    clock = Clock()
    dispatch = store(tmp_path, clock)
    with pytest.raises(CoordError):
        dispatch.pending(peer_host="host-a", executor_agent="agent-one", limit=0)
    with pytest.raises(CoordError):
        dispatch.pending(peer_host="host-a", executor_agent="agent-one", limit=21)
    ids: list[str] = []
    for index in range(256):
        row = dispatch.enqueue(binding(binding_id=f"q-{index}"),
                               trigger="message", trigger_ref="qb")
        ids.append(row["dispatch_id"])
    with pytest.raises(CoordError) as error:
        dispatch.enqueue(binding(binding_id="q-over"), trigger="message",
                         trigger_ref="qb")
    assert error.value.code == "queue_full"
    # A terminal receipt frees its slot without deleting the record.
    request = "4" * 32
    claim = dispatch.claim(ids[0], peer_host="host-a",
                           executor_agent="agent-one", request_id=request)
    dispatch.start(ids[0], peer_host="host-a", executor_agent="agent-one",
                   lease_id=claim["lease_id"], request_id=request)
    dispatch.receipt(ids[0], peer_host="host-a", executor_agent="agent-one",
                     lease_id=claim["lease_id"], status="rejected", exit_code=3)
    freed = dispatch.enqueue(binding(binding_id="q-over"), trigger="message",
                             trigger_ref="qb")
    assert freed["binding_id"] == "q-over"
    root = tmp_path / "wake" / "cli-dispatches"
    assert (root / f"{ids[0]}.json").exists()
    assert len(dispatch.pending(peer_host="host-a", executor_agent="agent-one",
                                limit=20)) == 20


def test_no_arbitrary_payload_accepted(tmp_path) -> None:
    dispatch = store(tmp_path, Clock())
    for extra in ["prompt", "message", "message_text", "argv", "env",
                  "environment", "binary", "command", "script"]:
        bad = binding()
        bad[extra] = "run anything"
        with pytest.raises(CoordError) as error:
            dispatch.enqueue(bad, trigger="message", trigger_ref="r-1")
        assert error.value.code == "forbidden_payload"
    unknown = binding()
    unknown["surprise"] = "x"
    with pytest.raises(CoordError) as error:
        dispatch.enqueue(unknown, trigger="message", trigger_ref="r-1")
    assert error.value.code == "invalid_binding"
    incomplete = binding()
    del incomplete["driver"]
    with pytest.raises(CoordError):
        dispatch.enqueue(incomplete, trigger="message", trigger_ref="r-1")
    for bad_binding in [binding(version=0), binding(version=True),
                        binding(driver="bash"), binding(driver="codex"),
                        binding(execution_host="HOST A"),
                        binding(native_thread_id="t\x00x")]:
        with pytest.raises(CoordError):
            dispatch.enqueue(bad_binding, trigger="message", trigger_ref="r-1")
    with pytest.raises(CoordError):
        dispatch.enqueue(binding(), trigger="webhook", trigger_ref="r-1")
    with pytest.raises(CoordError):
        dispatch.enqueue(binding(), trigger="message", trigger_ref="r-1", ttl_s=0)
    with pytest.raises(CoordError):
        dispatch.enqueue(binding(), trigger="message", trigger_ref="r-1", ttl_s=3601)
    with pytest.raises(CoordError):
        dispatch.enqueue(binding(), trigger="message", trigger_ref="r-1", ttl_s=True)


def test_no_eligibility_refuses_all_ops(tmp_path) -> None:
    clock = Clock()
    bare = CliDispatches(tmp_path, now=clock)
    seeded = store(tmp_path, clock)
    row = enqueue(seeded)
    with pytest.raises(CoordError) as error:
        bare.enqueue(binding(), trigger="message", trigger_ref="r-1")
    assert error.value.code == "eligibility_required"
    with pytest.raises(CoordError):
        bare.pending(peer_host="host-a", executor_agent="agent-one")
    with pytest.raises(CoordError):
        bare.claim(row["dispatch_id"], peer_host="host-a",
                   executor_agent="agent-one", request_id="3" * 32)
    with pytest.raises(CoordError):
        bare.start(row["dispatch_id"], peer_host="host-a",
                   executor_agent="agent-one",
                   lease_id="3" * 32, request_id="3" * 32)
    with pytest.raises(CoordError):
        bare.receipt(row["dispatch_id"], peer_host="host-a",
                     executor_agent="agent-one",
                     lease_id="3" * 32, status="accepted")


def test_exit_code_bool_and_range(tmp_path) -> None:
    dispatch = store(tmp_path, Clock())
    row = enqueue(dispatch)
    request = "2" * 32
    claim = dispatch.claim(row["dispatch_id"], peer_host="host-a",
                           executor_agent="agent-one", request_id=request)
    dispatch.start(row["dispatch_id"], peer_host="host-a",
                   executor_agent="agent-one",
                   lease_id=claim["lease_id"], request_id=request)
    for bad_code in [True, False, 2**31, -(2**31) - 1, "0"]:
        with pytest.raises(CoordError) as error:
            dispatch.receipt(row["dispatch_id"], peer_host="host-a",
                             executor_agent="agent-one",
                             lease_id=claim["lease_id"], status="accepted",
                             exit_code=bad_code)
        assert error.value.code == "invalid_receipt"
    ok_null = dispatch.receipt(row["dispatch_id"], peer_host="host-a",
                               executor_agent="agent-one",
                               lease_id=claim["lease_id"],
                               status="delivery_unknown")
    assert ok_null["exit_code"] is None


def test_crash_between_enqueue_and_claim_recovers(tmp_path) -> None:
    clock = Clock()
    first = store(tmp_path, clock)
    row = first.enqueue(binding(), trigger="child-terminal", trigger_ref="kid-7",
                        correlation_id="corr-7")
    # Simulated restart: a fresh instance over the same home continues.
    second = store(tmp_path, clock)
    rows = second.pending(peer_host="host-a", executor_agent="agent-one")
    assert [r["dispatch_id"] for r in rows] == [row["dispatch_id"]]
    request = "1" * 32
    owned = second.claim(row["dispatch_id"], peer_host="host-a",
                         executor_agent="agent-one", request_id=request)
    started = second.start(row["dispatch_id"], peer_host="host-a",
                           executor_agent="agent-one",
                           lease_id=owned["lease_id"], request_id=request)
    assert started["first_start"] is True


def test_index_survives_interruption_without_duplicate_launch(tmp_path) -> None:
    clock = Clock()
    dispatch = store(tmp_path, clock)
    ids: list[str] = []
    for index in range(3):
        row = dispatch.enqueue(binding(binding_id=f"i-{index}"),
                               trigger="schedule", trigger_ref="cron")
        ids.append(row["dispatch_id"])
    # Stray temp files from an interrupted write must not break the store.
    root = tmp_path / "wake" / "cli-dispatches"
    (root / ".tmp-stray.json").write_text("{}", encoding="utf-8")
    firsts: list[bool] = []
    for dispatch_id, index in zip(ids, range(3)):
        fresh = store(tmp_path, clock)
        request = f"{index + 10:032x}"
        claim = fresh.claim(dispatch_id, peer_host="host-a",
                            executor_agent="agent-one", request_id=request)
        started = fresh.start(dispatch_id, peer_host="host-a",
                              executor_agent="agent-one",
                              lease_id=claim["lease_id"], request_id=request)
        firsts.append(started["first_start"])
    assert firsts == [True, True, True]
    for dispatch_id, index in zip(ids, range(3)):
        fresh = store(tmp_path, clock)
        request = f"{index + 10:032x}"
        record = fresh._load_record(dispatch_id)
        replay = fresh.start(dispatch_id, peer_host="host-a",
                             executor_agent="agent-one",
                             lease_id=record["claim"]["lease_id"],
                             request_id=request)
        assert replay["first_start"] is False


def test_interrupted_enqueue_relinked_without_duplicate(tmp_path) -> None:
    clock = Clock()
    dispatch = store(tmp_path, clock)
    row = enqueue(dispatch)
    root = tmp_path / "wake" / "cli-dispatches"
    index_path = root / "active.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    assert row["dispatch_id"] in index["active"]
    # Simulate a crash after the record write but before the index write:
    # the retained record survives while the active index lost its entry.
    index_path.write_text(json.dumps({"active": []}), encoding="utf-8")
    assert dispatch.pending(peer_host="host-a", executor_agent="agent-one") == []
    again = enqueue(dispatch)
    assert again == row
    rows = dispatch.pending(peer_host="host-a", executor_agent="agent-one")
    assert [r["dispatch_id"] for r in rows] == [row["dispatch_id"]]
    request = "b" * 32
    claim = dispatch.claim(row["dispatch_id"], peer_host="host-a",
                           executor_agent="agent-one", request_id=request)
    started = dispatch.start(row["dispatch_id"], peer_host="host-a",
                             executor_agent="agent-one",
                             lease_id=claim["lease_id"], request_id=request)
    assert started["first_start"] is True


def test_state_files_stay_under_home_and_closed(tmp_path) -> None:
    dispatch = store(tmp_path, Clock())
    enqueue(dispatch)
    root = tmp_path / "wake" / "cli-dispatches"
    assert root.is_dir() and not root.is_symlink()
    for path in tmp_path.rglob("*"):
        assert root in path.parents or path == root or tmp_path in path.parents
        assert not path.is_symlink()
    for record_file in root.glob("*.json"):
        payload = json.loads(record_file.read_text(encoding="utf-8"))
        if "dispatch_id" not in payload:
            continue
        for forbidden in ["prompt", "argv", "env", "command", "script",
                          "transcript", "message"]:
            assert forbidden not in payload


def test_public_surface_has_no_rpc_or_wake_coupling() -> None:
    import subprocess
    import sys
    from pathlib import Path as _Path

    import coordfs.cli_dispatch as module

    assert module.CliDispatches is not None
    assert not hasattr(module.CliDispatches, "handle_request")
    assert "never exposed through remote RPC" in (module.__doc__ or "")
    # This suite legitimately imports coordfs.wake elsewhere during
    # collection, so the sys.modules boundary check must run in an isolated
    # interpreter that imports only the dispatch module.
    probe = (
        "import sys; "
        "import coordfs.cli_dispatch as module; "
        "assert module.CliDispatches is not None; "
        "assert not hasattr(module.CliDispatches, 'handle_request'); "
        "assert 'coordfs.wake' not in sys.modules, sorted(sys.modules); "
        "assert 'coordfs.office_ssh_gate' not in sys.modules, sorted(sys.modules); "
        "print('isolated-ok')"
    )
    completed = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True, text=True, timeout=60, check=False,
        cwd=_Path(__file__).parent,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "isolated-ok"
