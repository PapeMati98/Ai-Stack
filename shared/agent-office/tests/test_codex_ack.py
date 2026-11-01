from __future__ import annotations
import sys
from pathlib import Path
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from coordfs.codex_ack import owned_codex_ack
from coordfs.errors import CoordError
from coordfs.office import Office
from coordfs.wake import CODEX_RELAY_DRIVER, Wake

THREAD='a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d'
TARGET='codex-ailab-'+THREAD
RELAY='codex-ailab-11111111-2222-4333-8444-555555555555'
SENDER='codex-ailab-99999999-8888-4777-8666-555555555555'

def setup_store(home):
 w=Wake(home=home)
 w.enroll(binding_id='codex-ack-test',office_agent=TARGET,driver=CODEX_RELAY_DRIVER,
   native_thread_id=THREAD,workspace='D:\\',enrolled_by='synthetic-owner',owner_confirm=True,
   allowed_wakers=[SENDER],allowed_triggers=['message'],wake_kinds=['question'],
   expires_at='2030-01-01T00:00:00Z')
 w.verify('codex-ack-test',owner_confirm=True)
 sent=Office(agent_id=SENDER,home=home).send(TARGET,'Synthetic task, body must never be returned',
   kind='question',new_recipient=True)
 tick=w.tick(binding_id='codex-ack-test')
 dispatch=tick['decisions'][0]['dispatch_id']
 w.relay_claim(dispatch,relay_agent=RELAY,driver=CODEX_RELAY_DRIVER)
 w.relay_native_begin(dispatch,relay_agent=RELAY,driver=CODEX_RELAY_DRIVER)
 return w,dispatch,sent

def test_owned_lookup_finds_ack_in_original_senders_inbox(tmp_path):
 home=tmp_path/'coord';w,dispatch,sent=setup_store(home)
 assert owned_codex_ack(home,dispatch,RELAY)['acknowledged'] is False
 ack=Office(agent_id=TARGET,home=home).ack(sent['id'],note='Private body must never be exposed')
 result=owned_codex_ack(home,dispatch,RELAY)
 assert result['acknowledged'] is True
 assert result['ack_id']==ack['id']
 assert result['trigger_ref']==sent['id']
 assert 'body' not in str(result).lower()

def test_foreign_actor_cannot_probe_ack(tmp_path):
 home=tmp_path/'coord';w,dispatch,sent=setup_store(home)
 with pytest.raises(CoordError) as error: owned_codex_ack(home,dispatch,SENDER)
 assert error.value.code=='identity_mismatch'

def test_other_message_and_other_ack_do_not_settle_this_dispatch(tmp_path):
 home=tmp_path/'coord';w,dispatch,sent=setup_store(home)
 other=Office(agent_id=SENDER,home=home).send(TARGET,'Unrelated synthetic task',kind='question',new_recipient=True)
 Office(agent_id=TARGET,home=home).ack(other['id'])
 assert owned_codex_ack(home,dispatch,RELAY)['acknowledged'] is False

def test_pending_dispatch_ack_metadata_is_not_exposed(tmp_path):
 home=tmp_path/'coord';w,dispatch,sent=setup_store(home)
 row=w._load_dispatch(dispatch)
 row['status']='pending'
 w._atomic_replace_json(w._dispatch_path(dispatch),row)
 with pytest.raises(CoordError) as error:owned_codex_ack(home,dispatch,RELAY)
 assert error.value.code=='relay_dispatch_fenced'

def test_real_gate_settlement_after_native_fence_requires_owner_turn_and_ack(tmp_path):
 from tests.test_codex_desktop_remote import _client
 home=tmp_path/'coord';w,dispatch,sent=setup_store(home)
 client=_client(home)
 args={'dispatch_id':dispatch,'driver':CODEX_RELAY_DRIVER,'status':'delivered',
       'turn_id':'abcdefab-cdef-4abc-8def-1234567890ac',
       'detail':'correlated_office_ack_and_new_native_turn'}
 assert client.request('wake-relay-receipt',{'agent_id':SENDER},args)['code']=='identity_mismatch'
 assert client.request('wake-relay-receipt',{'agent_id':RELAY},args)['code']=='relay_ack_missing'
 assert w._load_dispatch(dispatch)['status']=='delivery_unknown'
 ack=Office(agent_id=TARGET,home=home).ack(sent['id'])
 proof=client.request('wake-relay-ack',{'agent_id':RELAY},{'dispatch_id':dispatch,'driver':CODEX_RELAY_DRIVER})
 assert proof['ack_id']==ack['id'] and proof['acknowledged'] is True
 result=client.request('wake-relay-receipt',{'agent_id':RELAY},args)
 assert result['code']=='wake_relay_receipt'
 assert result['dispatch']['status']=='delivered'
 assert 'receipt_message_id' not in result['dispatch']
 assert client.request('wake-relay-receipt',{'agent_id':RELAY},args)['idempotent'] is True
 assert w.relay_pending(binding_id='codex-ack-test',driver=CODEX_RELAY_DRIVER)['count']==0
