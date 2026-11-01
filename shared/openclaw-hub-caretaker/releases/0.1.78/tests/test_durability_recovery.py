"""Crash/restart behavior using a deterministic simulated canonical server."""
def _redacted_fixture(kind, ordinal, binary=False):
    """Return a deterministic public synthetic fixture value.

    The result is newly authored public data derived only from the pinned
    rule label and a per-file ordinal. It is never derived from any original
    value. Runtime values retain the original detector category.
    """
    alphabet = 'abcdefghijklmnopqrstuvwxyz0123456789'
    seed = (len(kind) * 131 + int(ordinal) * 7919 + 17) & 0x7FFFFFFF
    chars = []
    for _ in range(40):
        seed = (seed * 1103515245 + 12345) & 0x7FFFFFFF
        chars.append(alphabet[seed % 36])
    body = ''.join(chars)
    if kind == 'vendor-token':
        body = 'gh' + 'p_' + body
    elif kind == 'aws-access-key-id':
        body = 'AK' + 'IA' + body[:16].upper()
    elif kind == 'private-key':
        marker = 'PRIVATE' + ' KEY'
        body = ('-----' + 'BEGIN ' + marker + '-----' + chr(10) + body + chr(10)
                + '-----' + 'END ' + marker + '-----')
    if binary:
        return body.encode('ascii')
    return body


import hashlib,json,tempfile,unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from caretaker.config import load_config,InputSpec
from caretaker.hub import (FakeKnowledgeGateway, RecordHandoffResult,
                            InventoryItem, WikiExcerpt)
from caretaker.pipeline import PipelineDeps,run_pipeline
from caretaker.store import Store
from caretaker.transports import FakeTransport,Envelope
from ._helpers import write_config
from .test_pipeline import _office_connector
class SimulatedCrash(BaseException):pass
class IdempotentGateway(FakeKnowledgeGateway):
    def __init__(self):super().__init__();self.remote={};self.calls=0;self.crash_once=False
    def record_handoff(self,**request):
        self.calls+=1;digest=hashlib.sha256(Path(request['path']).read_bytes()).hexdigest();duplicate=digest in self.remote
        self.remote.setdefault(digest,request.copy())
        if self.crash_once:self.crash_once=False;raise SimulatedCrash()
        return RecordHandoffResult(status='deduplicated' if duplicate else 'recorded',receipt_id='prop-'+digest,receipt_status='COMPLETE',source_id='src-'+digest,proposal_id='prop-'+digest,event_ids=('event-stage-'+digest,'event-proposal-'+digest),checkpoint_status='CHECKPOINTED',synthetic=False)
class DurabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.input=self.root/'input.jsonl'
        self.input.write_text(json.dumps({'native_session_id':'native-1','cursor':'cursor-1','observed_at':'2026-09-17T10:00:00Z','title':'Durable fact','excerpt':'A verified application capability was restored.','classification':'internal'})+'\n',encoding='utf-8')
        self.cfg=replace(load_config(write_config(self.root)),inputs=[InputSpec(str(self.input),'normalized_events','codex')])
        self.db=self.root/'state.sqlite';self.store=Store(str(self.db));self.gateway=IdempotentGateway()
    def tearDown(self):self.store.close();self.temp.cleanup()
    def restart(self):self.store.close();self.store=Store(str(self.db))
    def deps(self,offline=False):
        final=json.dumps({'decision':'new','synthesis':'A verified application capability was restored.','source_refs':['codex:native-1:cursor-1'],'rationale':'new supported fact'})
        model=FakeTransport(responses=[] if offline else [Envelope(ok=True,status='ok',final=final,model='Qwen3.8-27B',provider='vllm')],raise_on=[0] if offline else [])
        return PipelineDeps(model,None,self.gateway,_office_connector(),worktree_root='')
    def test_offline_queue_drains_after_reopen_and_success(self):
        run_pipeline(self.cfg,self.deps(True),store=self.store);self.assertTrue(self.store.outbox_pending())
        self.restart();d=self.deps();run_pipeline(self.cfg,d,store=self.store)
        self.assertEqual(self.store.outbox_pending(),[]);self.assertEqual(len(self.gateway.remote),1)
        self.restart();d=self.deps();r=run_pipeline(self.cfg,d,store=self.store)
        self.assertEqual(len(d.primary.calls),0);self.assertEqual(r.proposals_duplicate,1);self.assertEqual(len(self.gateway.remote),1)
    def test_gateway_error_text_never_enters_durable_outbox(self):
        secret=('Authorization: Bearer ' + _redacted_fixture('bearer-token', 1))
        failure=RecordHandoffResult(status='offline',reason='canonical gateway exit 1: '+secret)
        with patch.object(self.gateway,'record_handoff',return_value=failure):
            result=run_pipeline(self.cfg,self.deps(),store=self.store)
        self.assertIn('hub_offline',result.incidents)
        self.restart()
        rows=self.store._conn.execute('SELECT reason,request_json FROM outbox').fetchall()
        self.assertTrue(rows)
        self.assertTrue(any(row['reason']=='handoff_failed:gateway_offline' for row in rows))
        for row in rows:
            self.assertNotIn(secret,str(tuple(row)))
        self.assertNotIn(secret,'\n'.join(self.store._conn.iterdump()))
    def test_crash_after_remote_success_retries_same_note_without_model(self):
        self.gateway.crash_once=True
        with self.assertRaises(SimulatedCrash):run_pipeline(self.cfg,self.deps(),store=self.store)
        self.assertEqual(len(self.gateway.remote),1);self.assertTrue(self.store.outbox_pending())
        self.restart();self.input.write_text('',encoding='utf-8');d=self.deps();run_pipeline(self.cfg,d,store=self.store)
        self.assertEqual(len(d.primary.calls),0);self.assertEqual(self.gateway.calls,2);self.assertEqual(len(self.gateway.remote),1);self.assertEqual(self.store.outbox_pending(),[])
        self.restart();rows=self.store._conn.execute('SELECT request_json,receipt_id FROM outbox').fetchall()
        self.assertTrue(rows)
        for row in rows:
            proof=json.loads(row['request_json'])['receipt'];self.assertEqual(proof['receipt_status'],'COMPLETE');self.assertTrue(proof['event_ids']);self.assertTrue(row['receipt_id'])
    def test_cursor_event_crash_rolls_back_both_and_replays(self):
        with patch.object(self.store,'insert_event',side_effect=SimulatedCrash()):
            with self.assertRaises(SimulatedCrash):run_pipeline(self.cfg,self.deps(),store=self.store)
        self.restart()
        for table in ('cursors','events'):self.assertEqual(self.store._conn.execute('SELECT COUNT(*) FROM '+table).fetchone()[0],0)
        run_pipeline(self.cfg,self.deps(),store=self.store)
        for table in ('cursors','events'):self.assertEqual(self.store._conn.execute('SELECT COUNT(*) FROM '+table).fetchone()[0],1)

    def test_cursor_commit_before_pending_queue_crash_recovers_event(self):
        # The event/cursor transaction has committed, but the process dies
        # before the later synthesis_pending write.  Recovery must use the
        # sanitized durable event even when the source has rotated away.
        with patch('caretaker.pipeline._queue_pending_records',
                   side_effect=SimulatedCrash()):
            with self.assertRaises(SimulatedCrash):
                run_pipeline(self.cfg, self.deps(True), store=self.store)
        self.assertEqual(self.store._conn.execute(
            'SELECT COUNT(*) FROM events').fetchone()[0], 1)
        self.assertEqual(self.store._conn.execute(
            'SELECT COUNT(*) FROM cursors').fetchone()[0], 1)
        self.restart()
        self.input.write_text('', encoding='utf-8')
        deps = self.deps()
        run_pipeline(self.cfg, deps, store=self.store)
        self.assertEqual(len(deps.primary.calls), 1)
        self.assertEqual(self.store.outbox_pending(), [])
        self.assertEqual(len(self.gateway.remote), 1)

    def test_contradiction_stays_queued_until_safe_receipt(self):
        final = json.dumps({
            'decision': 'contradiction',
            'synthesis': 'Two cited observations disagree; clarification is pending.',
            'source_refs': ['codex:native-1:cursor-1'],
            'rationale': 'conflicting candidate evidence',
        })
        primary = FakeTransport(
            responses=[Envelope(ok=True, status='ok', final=final,
                                model='Qwen3.8-27B', provider='vllm')],
            expected_identity=('vllm', 'Qwen3.8-27B'),
        )
        deps = PipelineDeps(primary, None, self.gateway,
                            _office_connector(), worktree_root='')
        result = run_pipeline(self.cfg, deps, store=self.store)
        self.assertEqual(result.proposals_contradiction, 1)
        pending = self.store.outbox_pending()
        self.assertTrue(pending)
        self.assertTrue(any(row.reason == 'model_contradiction'
                            for row in pending))

        self.restart()
        self.input.write_text('', encoding='utf-8')
        retry = self.deps()
        run_pipeline(self.cfg, retry, store=self.store)
        self.assertEqual(len(retry.primary.calls), 1)
        self.assertEqual(self.store.outbox_pending(), [])

    def _merge_deps(self, target='wiki/projects/p.md'):
        self.gateway.inventory_items = [InventoryItem(
            page='wiki/projects/p.md', title='P', summary='canonical',
            classification='internal')]
        self.gateway.wiki_pages['wiki/projects/p.md'] = WikiExcerpt(
            page='wiki/projects/p.md', title='P', excerpt='Canonical target',
            status='verified', classification='internal')
        final = json.dumps({
            'decision': 'merge',
            'synthesis': 'Candidate belongs with the canonical target.',
            'source_refs': ['codex:native-1:cursor-1'],
            'merge_target': target,
            'rationale': 'same capability',
            'evidence_pages': ['wiki/projects/p.md'],
        })
        primary = FakeTransport(
            responses=[Envelope(ok=True, status='ok', final=final,
                                model='Qwen3.8-27B', provider='vllm')],
            expected_identity=('vllm', 'Qwen3.8-27B'),
        )
        deps = PipelineDeps(primary, None, self.gateway,
                            _office_connector(), worktree_root='')
        return deps

    def test_rejected_decision_cannot_shadow_later_duplicate_on_restart(self):
        rejected = self._merge_deps()
        rejected.primary = FakeTransport(responses=[Envelope(
            ok=True, status='ok', final='invalid JSON',
            model='Qwen3.8-27B', provider='vllm')])
        result = run_pipeline(self.cfg, rejected, store=self.store)
        self.assertIn('invalid_decision', result.incidents)
        self.assertTrue(all(r.reason == 'invalid_decision'
                            for r in self.store.outbox_pending()))
        self.restart()
        final = json.dumps({'decision':'duplicate','synthesis':'',
                            'source_refs':['codex:native-1:cursor-1'],
                            'evidence_pages':['wiki/projects/p.md'],
                            'rationale':'Already covered by canonical evidence'})
        duplicate = self._merge_deps()
        duplicate.primary = FakeTransport(responses=[Envelope(
            ok=True, status='ok', final=final,
            model='Qwen3.8-27B', provider='vllm')])
        result = run_pipeline(self.cfg, duplicate, store=self.store)
        self.assertEqual(result.proposals_duplicate, 1)
        self.assertFalse(self.store.outbox_pending())
        self.assertEqual(self.gateway.calls, 0)
        self.restart()
        replay = self.deps()
        result = run_pipeline(self.cfg, replay, store=self.store)
        self.assertEqual(result.proposals_duplicate, 1)
        self.assertEqual(len(replay.primary.calls), 0)
        self.assertEqual(self.gateway.calls, 0)

    def test_merge_uses_canonical_handoff_and_keeps_counts(self):
        deps = self._merge_deps()
        result = run_pipeline(self.cfg, deps, store=self.store)
        self.assertEqual(result.proposals_merge, 1)
        self.assertEqual(result.proposals_new, 0)
        self.assertEqual(self.store.outbox_pending(), [])
        self.assertEqual(len(deps.primary.calls), 1)
        self.assertEqual(len(self.gateway.remote), 1)
        handoff = next(iter(self.gateway.remote.values()))
        self.assertEqual(handoff['target_page'], 'wiki/projects/p.md')

        self.restart()
        self.input.write_text('', encoding='utf-8')
        retry = self.deps()
        run_pipeline(self.cfg, retry, store=self.store)
        self.assertEqual(len(retry.primary.calls), 0)
        self.assertEqual(self.store.outbox_pending(), [])

    def test_merge_books_only_prompt_selected_records(self):
        self.input.write_text(
            self.input.read_text(encoding='utf-8') + json.dumps({
                'native_session_id': 'native-2',
                'cursor': 'cursor-2',
                'observed_at': '2026-09-17T10:01:00Z',
                'title': 'Second durable fact',
                'excerpt': 'A second capability is awaiting its own batch.',
                'classification': 'internal',
            }) + '\n',
            encoding='utf-8',
        )
        deps = self._merge_deps()
        result = run_pipeline(self.cfg, deps, store=self.store,
                              max_prompt_bytes=1350)
        self.assertEqual(result.proposals_merge, 1)
        self.assertEqual(result.proposals_new, 0)
        handoff = next(iter(self.gateway.remote.values()))
        self.assertEqual(handoff['source_refs'],
                         ['codex:native-1:cursor-1'])
        self.assertNotIn('cursor-2', handoff['source_refs'])
        pending = self.store.outbox_pending_requests(
            kind='synthesis_pending')
        self.assertEqual(len(pending), 1)
        self.assertTrue(pending[0][1]['source_ref'].endswith(':cursor-2'))

    def test_invalid_merge_target_is_queued_without_hub_write(self):
        deps = self._merge_deps(target='wiki/../outside.md')
        result = run_pipeline(self.cfg, deps, store=self.store)
        self.assertIn('merge_target_invalid', result.incidents)
        self.assertEqual(result.proposals_merge, 0)
        self.assertEqual(result.proposals_contradiction, 1)
        self.assertEqual(self.gateway.calls, 0)
        pending = self.store.outbox_pending_requests(
            kind='synthesis_pending')
        self.assertTrue(pending)
        self.assertTrue(any(row[0].reason == 'merge_target_invalid'
                            for row in pending))

    def test_merge_handoff_retries_after_rotation_without_model(self):
        deps = self._merge_deps()
        self.gateway.crash_once = True
        with self.assertRaises(SimulatedCrash):
            run_pipeline(self.cfg, deps, store=self.store)
        self.assertEqual(len(deps.primary.calls), 1)
        self.assertEqual(len(self.gateway.remote), 1)
        self.assertTrue(self.store.outbox_pending())

        self.restart()
        self.input.write_text('', encoding='utf-8')
        retry = self.deps()
        run_pipeline(self.cfg, retry, store=self.store)
        self.assertEqual(len(retry.primary.calls), 0)
        self.assertEqual(self.gateway.calls, 2)
        self.assertEqual(len(self.gateway.remote), 1)
        self.assertEqual(self.store.outbox_pending(), [])
    def test_draft_before_reservation_is_not_treated_as_published(self):
        original=self.store.outbox_put
        def crash(**kw):
            if kw.get('status')=='pending':raise SimulatedCrash()
            return original(**kw)
        with patch.object(self.store,'outbox_put',side_effect=crash):
            with self.assertRaises(SimulatedCrash):run_pipeline(self.cfg,self.deps(),store=self.store)
        self.assertEqual(len(self.gateway.remote),0);self.restart();d=self.deps();run_pipeline(self.cfg,d,store=self.store)
        self.assertEqual(len(d.primary.calls),1);self.assertEqual(len(self.gateway.remote),1);self.assertEqual(self.store.outbox_pending(),[])
    def test_explicit_inventory_hash_suppresses_model_without_title_hashing(self):
        from caretaker.ingest import read_inputs
        from caretaker.filter import filter_records
        records,_,_=read_inputs([(str(self.input),'normalized_events','codex')]);digest=filter_records(records,allowlist_tools=['codex'],max_body_bytes=8192,max_excerpt_bytes=400)[0].content_hash
        self.gateway.inventory_items=[InventoryItem(page='wiki/projects/p.md',title='Unrelated label',summary='Different metadata',classification='internal',content_hash=digest)]
        d=self.deps();r=run_pipeline(self.cfg,d,store=self.store);self.assertEqual(r.proposals_duplicate,1);self.assertEqual(len(d.primary.calls),0)
    def test_deleted_record_never_reaches_model_or_hub(self):
        data=json.loads(self.input.read_text(encoding='utf-8'));data['deleted']=True;self.input.write_text(json.dumps(data),encoding='utf-8')
        d=self.deps();r=run_pipeline(self.cfg,d,store=self.store);self.assertEqual(r.records_admitted,0);self.assertEqual(len(d.primary.calls),0);self.assertEqual(self.gateway.calls,0)
if __name__=='__main__':unittest.main()
