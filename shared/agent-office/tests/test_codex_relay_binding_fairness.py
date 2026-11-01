"""A busy Core must not starve a different ready approved binding."""
import json
import pytest
from coordfs import codex_relay_cycle as cycle
from test_codex_desktop_remote import CODEX_UUID, _client, _make_codex_binding, _make_codex_dispatch
from test_codex_relay_cycle_product import App
from coordfs.wake import Wake

HUB_UUID = '11111111-2222-4333-8444-555555555555'

class TwoThreads:
    def __init__(self):
        self.apps = {CODEX_UUID: App(state='active'), HUB_UUID: App()}
        self.calls, self.tokens, self.output = [], {}, []

    def write_call(self, tool, args):
        target = args['targets'][0]['threadId'] if tool == cycle.WAIT_TOOL else args['threadId']
        encoded = json.dumps(args).replace(target, CODEX_UUID)
        token = self.apps[target].write_call(tool, json.loads(encoded))
        self.tokens[token] = target
        self.calls.append((tool, args))
        return token

    def read_result(self, token):
        target = self.tokens[token]
        raw = self.apps[target].read_result(token)
        return json.loads(json.dumps(raw).replace(CODEX_UUID, target))

    def write_json_line(self, value): self.output.append(value)
    def close(self): pass

@pytest.fixture(autouse=True)
def linux_gate(monkeypatch):
    monkeypatch.setattr('coordfs.wake._native_workspace_flavor', lambda: 'posix')

def test_busy_core_does_not_starve_ready_hub(tmp_path):
    home = tmp_path / 'coord'
    _make_codex_binding(home, binding_id='codex-core')
    _make_codex_dispatch(home, binding_id='codex-core')
    _make_codex_binding(home, binding_id='codex-hub-root', native_thread_id=HUB_UUID,
                       agent='codex-ailab-11111111-2222-4333-8444-555555555555')
    _make_codex_dispatch(home, binding_id='codex-hub-root')
    app = TwoThreads()
    result = cycle.CodexRelayCycle(_client(home), binding_ids=('codex-core','codex-hub-root'),
                                   host_factory=lambda: app).run()
    assert result['status'] == 'delivered', result
    sent = [args for tool,args in app.calls if tool == cycle.SEND_TOOL]
    assert len(sent) == 1 and sent[0]['threadId'] == HUB_UUID

@pytest.mark.parametrize('reason',['owned_fence_unverifiable','owned_turn_unconfirmed','owned_checkout_mismatch'])
def test_old_unknown_fences_only_its_binding(tmp_path,reason):
    home=tmp_path/'coord'
    _make_codex_binding(home,binding_id='codex-core')
    core=_make_codex_dispatch(home,binding_id='codex-core')
    client=_client(home)
    first=App(lose_send=True)
    assert cycle.CodexRelayCycle(client,binding_ids=('codex-core',),host_factory=lambda:first).run()['status']=='unknown'
    _make_codex_binding(home,binding_id='codex-hub-root',native_thread_id=HUB_UUID,agent='codex-ailab-'+HUB_UUID)
    _make_codex_dispatch(home,binding_id='codex-hub-root')
    class Projection:
        def request(self,op,identity,args,**kwargs):
            snap=client.request(op,identity,args,**kwargs)
            if reason=='owned_fence_unverifiable' and op=='wake-relay-pending' and args.get('binding_id')=='codex-core':
                for row in snap.get('owned_dispatches',[]):row.pop('native_baseline_observed_at',None)
            return snap
    app=TwoThreads();app.apps[CODEX_UUID]=App(cwd='wrong-project' if reason=='owned_checkout_mismatch' else first.cwd)
    result=cycle.CodexRelayCycle(Projection(),binding_ids=('codex-core','codex-hub-root'),host_factory=lambda:app).run()
    assert result['status']=='delivered',result
    assert result['blocked_bindings']==[{'binding_id':'codex-core','reason':reason}]
    sent=[args for tool,args in app.calls if tool==cycle.SEND_TOOL]
    assert len(sent)==1 and sent[0]['threadId']==HUB_UUID
    assert Wake(home=home)._load_dispatch(core['dispatch_id']).get('reconciliation_outcome') is None

def test_unknown_after_current_send_never_sends_to_another_binding(tmp_path):
    home=tmp_path/'coord'
    _make_codex_binding(home,binding_id='codex-core');_make_codex_dispatch(home,binding_id='codex-core')
    _make_codex_binding(home,binding_id='codex-hub-root',native_thread_id=HUB_UUID,agent='codex-ailab-'+HUB_UUID)
    _make_codex_dispatch(home,binding_id='codex-hub-root')
    app=TwoThreads();app.apps[CODEX_UUID]=App(lose_send=True)
    result=cycle.CodexRelayCycle(_client(home),binding_ids=('codex-core','codex-hub-root'),host_factory=lambda:app).run()
    assert result['status']=='unknown'
    sent=[args for tool,args in app.calls if tool==cycle.SEND_TOOL]
    assert len(sent)==1 and sent[0]['threadId']==CODEX_UUID
