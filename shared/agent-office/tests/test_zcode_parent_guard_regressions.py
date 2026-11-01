"""Concrete missed lifecycle guards: no Desktop/network actions."""
from test_zcode_lease_lifecycle import make, prepared, DISPATCH, CALL, DIGEST_B

def test_external_submit_revalidates_lease_immediately_before_send():
    host, office, _ = make()
    prepared(host)
    office.lease_ok = False
    sends = []
    host.run_external_submit(DISPATCH, lambda intent: sends.append(intent), lambda: None)
    assert sends == [], 'Authoritative lease became unavailable after prepare'

def test_restore_failure_cannot_claim_foreground_restored_or_release():
    host, office, _ = make()
    prepared(host)
    def failed_restore():
        raise RuntimeError('synthetic original foreground unavailable')
    result = host.run_external_submit(DISPATCH, lambda intent: {'status':'queued','call_id':CALL}, failed_restore)
    assert 'foreground_restored' not in result.get('phases',[])
    assert office.releases == [], 'Keep own lease until confirmed restoration or natural expiry'

def test_restore_callback_required_before_external_action():
    host, office, _ = make()
    prepared(host)
    sends = []
    result = host.run_external_submit(DISPATCH, lambda intent: sends.append(intent))
    assert result['code'] == 'restore_required'
    assert sends == [] and office.releases == []

def test_settled_unknown_never_reclaims_or_submits():
    host, office, _ = make()
    prepared(host)
    host.settle(DISPATCH, None)
    before = list(office.calls)
    sends = []
    assert host.renew_lease(DISPATCH)['code'] == 'already_settled'
    result = host.run_external_submit(DISPATCH, lambda intent: sends.append(intent), lambda: None)
    assert result['code'] == 'duplicate_submit'
    assert sends == [] and office.calls == before

def test_rpc_delay_exhausting_deadline_blocks_submit():
    host, office, clock = make()
    prepared(host)
    original = office.request
    def delayed(op, *args, **kwargs):
        value = original(op, *args, **kwargs)
        if op == 'wake-desktop-input-claim':
            clock.now += 60
        return value
    office.request = delayed
    sends = []
    result = host.run_external_submit(DISPATCH, lambda intent: sends.append(intent), lambda: None)
    assert result['code'] == 'lease_expired' and sends == []

def test_release_error_is_not_reported_as_lease_released():
    host, office, _ = make()
    prepared(host)
    original = office.request
    def failed_release(op, *args, **kwargs):
        if op == 'wake-desktop-input-release':
            return {'status':'error','code':'synthetic_release_failure'}
        return original(op, *args, **kwargs)
    office.request = failed_release
    result = host.run_external_submit(DISPATCH, lambda intent: {'status':'queued','call_id':CALL}, lambda: None)
    assert 'lease_released' not in result['phases']
    assert result['phases'][-1] == 'lease_retained'

def test_same_holder_rotated_digest_is_retired_by_cas_without_send():
    for operation in ['renew','submit']:
        host, office, _ = make()
        prepared(host)
        office.digest = DIGEST_B
        sends = []
        result = host.renew_lease(DISPATCH) if operation == 'renew' else host.run_external_submit(
            DISPATCH, lambda intent: sends.append(intent), lambda: None)
        assert result['code'] == 'lease_drift' and sends == []
        assert len(office.releases) == 1
        assert office.releases[0]['expected_lease_digest'] == DIGEST_B
        count = len(office.calls)
        assert host.renew_lease(DISPATCH)['code'] == 'lease_drift'
        assert len(office.calls) == count, 'No reclaims after drift'

def test_rotated_own_release_failure_retains_handle_for_bounded_cleanup():
    host, office, clock = make()
    prepared(host)
    office.digest = DIGEST_B
    original = office.request
    def failure(op, *args, **kwargs):
        if op == 'wake-desktop-input-release':
            return {'status':'error','code':'temporary_release_failure'}
        return original(op, *args, **kwargs)
    office.request = failure
    assert host.renew_lease(DISPATCH)['code'] == 'lease_drift'
    assert office.releases == []
    office.request = original
    clock.now += 60
    host.renew_lease(DISPATCH)
    assert len(office.releases) == 1
    assert office.releases[0]['expected_lease_digest'] == DIGEST_B
