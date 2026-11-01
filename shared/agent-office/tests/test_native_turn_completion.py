"""Strict native proof guards exercised through real remote Office framing."""
import time
import uuid
import pytest
from coordfs import codex_relay_cycle as c
from coordfs.wake import Wake
from test_codex_desktop_remote import CODEX_UUID, _client, _make_codex_binding, _make_codex_dispatch


@pytest.fixture(autouse=True)
def server_flavor(monkeypatch):
    monkeypatch.setattr('coordfs.wake._native_workspace_flavor', lambda: 'posix')


def rpc(client, op, **args):
    return client.request(op,c.build_identity(),{'driver':c.DRIVER,**args})


def prepared(tmp_path, prior='abcdefab-cdef-4abc-8def-1234567890aa'):
    home=tmp_path/'coord'
    _make_codex_binding(home,binding_id='codex-core')
    dispatch=_make_codex_dispatch(home,binding_id='codex-core')
    client=_client(home);did=dispatch['dispatch_id']
    assert rpc(client,'wake-relay-claim',dispatch_id=did,relay_agent=c.AGENT_ID)['status']=='ok'
    assert rpc(client,'wake-relay-native-begin',dispatch_id=did,relay_agent=c.AGENT_ID,
               prior_turn_id=prior,baseline_observed_at=time.time())['status']=='ok'
    turn=str(uuid.uuid4());stamp=time.time()
    assert rpc(client,'wake-relay-receipt',dispatch_id=did,status='unconfirmed',turn_id=turn)['status']=='ok'
    proof={'schema':'codex-native-turn/v1','source':c.READ_TOOL,'thread_id':CODEX_UUID,
           'turn_id':turn,'started_at':stamp,'completed_at':time.time(),
           'status':'completed','observed_at':time.time()}
    return home,client,did,proof


@pytest.mark.parametrize('prior',[None,'abcdefab-cdef-4abc-8def-1234567890aa'])
def test_native_completion_empty_or_existing_baseline_without_ack(tmp_path,prior):
    home,client,did,proof=prepared(tmp_path,prior)
    out=rpc(client,'wake-relay-observe',dispatch_id=did,stage='target_finished',native_proof=proof)
    assert out['status']=='ok',out
    assert out['dispatch']['reconciliation_outcome']=='delivered'
    assert rpc(client,'wake-relay-ack',dispatch_id=did)['acknowledged'] is False
    snap=rpc(client,'wake-relay-pending',binding_id='codex-core',include_owned=True,relay_agent=c.AGENT_ID)
    assert snap['owned_dispatches']==[]
    assert all('text' not in row for row in snap['owned_dispatches'])
    again=rpc(client,'wake-relay-observe',dispatch_id=did,stage='target_finished',native_proof=proof)
    assert again['status']=='ok'
    wake=Wake(home=home)
    assert len([r for r in wake._ledger_entries() if r.get('dispatch_id')==did and r.get('decision')=='reconciled'])==1
    assert wake._daily_attempts(wake._load_binding('codex-core'),wake._ledger_entries())==1


@pytest.mark.parametrize('change',[
    {'turn_id':'abcdefab-cdef-4abc-8def-1234567890aa'},
    {'thread_id':'99999999-8888-4777-8666-555555555555'},
    {'started_at':1,'completed_at':2},
    {'observed_at':1}, {'observed_at':time.time()+1000},
    {'completed_at':None}, {'status':'inProgress'}, {'extra':'not-allowed'},
    {'source':'model-invented'}, {'started_at':True},
])
def test_invalid_proof_cannot_settle(tmp_path,change):
    home,client,did,proof=prepared(tmp_path)
    out=rpc(client,'wake-relay-observe',dispatch_id=did,stage='target_finished',native_proof={**proof,**change})
    assert out['status']!='ok',out
    assert Wake(home=home)._load_dispatch(did).get('reconciliation_outcome') is None


def test_foreign_owner_cannot_enumerate_or_settle(tmp_path):
    home,client,did,proof=prepared(tmp_path)
    response=rpc(client,'wake-relay-pending',binding_id='codex-core',include_owned=True,
                 relay_agent='codex-ailab-99999999-8888-4777-8666-555555555555')
    assert response['status']!='ok'
    other={'agent_id':'codex-ailab-99999999-8888-4777-8666-555555555555'}
    response=client.request('wake-relay-observe',other,{'driver':c.DRIVER,'dispatch_id':did,
                            'stage':'target_finished','native_proof':proof})
    assert response['status']!='ok'
    snap=rpc(client,'wake-relay-pending',binding_id='codex-core',include_owned=True,relay_agent=c.AGENT_ID)
    assert len(snap['owned_dispatches'])==1 and 'text' not in snap['owned_dispatches'][0]


def test_invalid_proof_is_checked_even_after_same_observation(tmp_path):
    home,client,did,proof=prepared(tmp_path)
    assert rpc(client,'wake-relay-observe',dispatch_id=did,stage='target_finished')['status']=='ok'
    out=rpc(client,'wake-relay-observe',dispatch_id=did,stage='target_finished',native_proof={**proof,'thread_id':str(uuid.uuid4())})
    assert out['status']!='ok'
    assert Wake(home=home)._load_dispatch(did).get('reconciliation_outcome') is None
    out=rpc(client,'wake-relay-observe',dispatch_id=did,stage='target_finished',native_proof=proof)
    assert out['dispatch']['reconciliation_outcome']=='delivered'


def test_binding_version_changed_or_stop_refuses_proof(tmp_path,monkeypatch):
    home,client,did,proof=prepared(tmp_path)
    monkeypatch.setattr(Wake,'stop_status',lambda self:{'stop_active':True})
    out=rpc(client,'wake-relay-observe',dispatch_id=did,stage='target_finished',native_proof=proof)
    assert out['status']!='ok'
    assert Wake(home=home)._load_dispatch(did).get('reconciliation_outcome') is None
