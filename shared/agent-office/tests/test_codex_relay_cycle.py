"""Parent repair: protocol and safety regressions, no native sends or production."""
import io
import json
import threading
import time
import pytest
from coordfs import codex_relay_cycle as c
from test_codex_relay_cycle_product import App, PRIOR, OLDER, NEW, raw
from test_codex_desktop_remote import CODEX_UUID, CODEX_WORKSPACE


def test_pinned_bindings_and_host():
    assert c.BINDING_IDS == ('codex-core','codex-hub-root','codex-rozwoj')
    assert c.AGENT_ID == 'codex-ailab-01a10b42-94e2-78e1-a908-c6549d6b9668'
    assert c.build_identity()['session_id'] == '01a10b42-94e2-78e1-a908-c6549d6b9668'


def test_arguments_are_official_exact_shapes():
    assert c.send_arguments(thread_id=CODEX_UUID,prompt='x') == {'threadId':CODEX_UUID,'hostId':'local','prompt':'x'}
    assert c.read_arguments(CODEX_UUID) == {'threadId':CODEX_UUID,'hostId':'local','turnLimit':2,'includeOutputs':False,'maxOutputCharsPerItem':1}
    assert c.wait_arguments(thread_id=CODEX_UUID,after_cursor=None,timeout_ms=1000) == {'targets':[{'threadId':CODEX_UUID,'hostId':'local'}],'timeoutMs':1000}


@pytest.mark.parametrize('thread,prompt',[('title','x'),(CODEX_UUID,'x'*9000),(CODEX_UUID,'')])
def test_invalid_send_args_refused(thread,prompt):
    with pytest.raises(c.CycleError):c.send_arguments(thread_id=thread,prompt=prompt)


@pytest.mark.parametrize('payload',[{'threadId':'wrong'},{'threadId':CODEX_UUID,'turnId':NEW},{'started':True}])
def test_send_success_never_invents_a_turn(payload):
    with pytest.raises(c.CycleError):c.parse_send_result(raw(payload),expected_thread_id=CODEX_UUID)
    assert c.parse_send_result(raw({'threadId':CODEX_UUID}),expected_thread_id=CODEX_UUID)=={'threadId':CODEX_UUID}


def test_iserror_raw_result_is_refused():
    result={**raw({'threadId':CODEX_UUID}),'isError':True}
    with pytest.raises(c.CycleError):c.parse_send_result(result,expected_thread_id=CODEX_UUID)


def test_read_newest_first_and_old_idle_baseline():
    app=App(); token=app.write_call(c.READ_TOOL,c.read_arguments(CODEX_UUID))
    result=c.parse_read_result(app.read_result(token),expected_thread_id=CODEX_UUID,now=time.time())
    assert [t.turn_id for t in result.turns]==[PRIOR,OLDER]
    assert result.age_s==0
    assert not hasattr(result.turns[0],'items')


def test_wait_wrong_target_or_unknown_shape_refused():
    with pytest.raises(c.CycleError):c.parse_wait_result(raw({'ok':True}),expected_thread_id=CODEX_UUID)
    app=App();app.sent=True;app.turn_started=time.time()
    token=app.write_call(c.WAIT_TOOL,c.wait_arguments(thread_id=CODEX_UUID,after_cursor=None,timeout_ms=1000))
    result=app.read_result(token)
    with pytest.raises(c.CycleError):c.parse_wait_result(result,expected_thread_id='99999999-8888-4777-8666-555555555555')
    parsed=c.parse_wait_result(result,expected_thread_id=CODEX_UUID)
    assert parsed['polls'][0]['cursor']==CODEX_UUID+':1'
    assert set(parsed)=={'timedOut','polls'}


def test_checkout_match_requires_exact_project():
    assert not c._cwd_incompatible(CODEX_WORKSPACE.replace('\\','/').upper()+'/',CODEX_WORKSPACE)
    assert c._cwd_incompatible(CODEX_WORKSPACE+'\\other-project',CODEX_WORKSPACE)


def test_old_turn_and_ambiguous_new_turns_do_not_prove_delivery():
    old=c._ReadTurn(OLDER,'completed',100,101)
    new=c._ReadTurn(NEW,'completed',200,201)
    assert c._pick_new_turn((old,),PRIOR,fence=200) is None
    assert c._pick_new_turn((new,old),PRIOR,fence=200)==new
    with pytest.raises(c.CycleError):c._pick_new_turn((new,c._ReadTurn(PRIOR,'completed',200,201)),None,fence=200)


def test_token_mismatch_never_advances_host_protocol():
    host=c.HostIO(io.BytesIO(b'{"token":"other","result":{}}\n'),io.BytesIO(),time.monotonic)
    try:
        with pytest.raises(c.CycleError,match='stdin_token_mismatch'):host.read_result('expected')
    finally:host.close()


def test_open_stdin_obeys_common_deadline():
    event=threading.Event()
    class Blocked:
        def read(self,size):event.wait(3);return b''
    host=c.HostIO(Blocked(),io.BytesIO(),time.monotonic)
    host.set_deadline(time.monotonic()+0.2,45)
    start=time.monotonic()
    try:
        with pytest.raises(c.CycleError,match='stdin_deadline'):host.read_result('expected')
        assert time.monotonic()-start<1
    finally:
        host.close();event.set()


def test_main_ssh_factory_really_hides_child(monkeypatch,tmp_path):
    from coordfs.office_remote import OfficeRemoteConfig
    config=OfficeRemoteConfig(host='synthetic.invalid',user='agentoffice',identity_file=str(tmp_path/'key'),known_hosts_file=str(tmp_path/'hosts'),ssh_executable=str(tmp_path/'ssh.exe'))
    cfg=tmp_path/'remote.json';cfg.write_text('{}')
    monkeypatch.setattr(c,'load_remote_config',lambda p:config)
    seen=[]
    def fake_popen(argv,**kwargs):seen.append(kwargs);raise OSError('synthetic stopped before process creation')
    monkeypatch.setattr(c.subprocess,'Popen',fake_popen)
    def fake_run(self):self.client._start();return {'status':'no_work'}
    monkeypatch.setattr(c.CodexRelayCycle,'run',fake_run)
    monkeypatch.setattr(c.HostIOFactory,'from_stdio',lambda: (lambda: App()))
    assert c.main(['--remote-config',str(cfg)])==0
    assert seen and seen[0]['shell'] is False
    if c.os.name=='nt':assert seen[0]['creationflags'] & c.subprocess.CREATE_NO_WINDOW


@pytest.mark.parametrize('args',[['--deadline','241'],['--deadline','-1'],['--max-tool-wait','46']])
def test_unbounded_cycle_arguments_refused(args):
    with pytest.raises(SystemExit):c.main(args)
