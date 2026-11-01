import json,sqlite3,tempfile,unittest,datetime as dt,os
from pathlib import Path
from unittest.mock import patch
from caretaker.audit import eligible_proposals,build_groups
from caretaker.publication import sha
from caretaker.pubstate import canonical_verdict
from caretaker.fleet_auditor import FleetReviewError
from caretaker.config import load_config
from curate_once import produce_reviewed_candidate
from maintenance import publication_drain
from maintenance import _audit_summary
from caretaker.pubstate import ensure_pub_tables,fingerprint,code_version
from .test_autonomous_loop import write_proposal,SequenceTransport,_valid_decision
from . import test_autonomous_loop as loop_helpers
from ._helpers import write_config

class ParentIntegrationTests(unittest.TestCase):
    def test_quarantine_does_not_trigger_fresh_small_batch_but_age_still_does(self):
        with tempfile.TemporaryDirectory() as d:
            v=Path(d)/'vault';db=sqlite3.connect(':memory:');ensure_pub_tables(db)
            policy={'min_ready_groups':10,'max_wait_hours':24,'max_batch':8}
            for i in range(10):write_proposal(v,f'prop-{i}','one',[f'src-{i}'])
            items,_=eligible_proposals(v);blocked=items[:9]
            ids=[x['proposal_id'] for x in blocked];paths=[x['path'] for x in blocked];hashes=[x['sha256'] for x in blocked]
            fp=fingerprint('one',paths,hashes+['missing_target'],policy,code_version())
            value={'project':'one','proposal_ids':ids,'paths':paths,'hashes':hashes}
            db.execute('INSERT INTO publications(batch,status,result,fingerprint) VALUES(?,?,?,?)',('test','QUARANTINED',json.dumps(value),fp));db.commit()
            now=dt.datetime.now(dt.timezone.utc)
            s=_audit_summary(v,policy,db,now);self.assertIsNone(s['trigger']);self.assertEqual(s['ready_group_count'],1);self.assertEqual(s['quarantined_proposals'],9)
            remaining=[x for x in items if x not in blocked][0];p=v/remaining['path'];old=now.timestamp()-25*3600;os.utime(p,(old,old))
            self.assertEqual(_audit_summary(v,policy,db,now)['trigger'],'age')
            page=v/'wiki/projects/one.md';page.parent.mkdir(parents=True);page.write_text('New reviewed target context')
            self.assertEqual(_audit_summary(v,policy,db,now)['quarantined_proposals'],0)
            db.close()
    def test_actual_gateway_session_header_groups_only_matching_integral_sources(self):
        with tempfile.TemporaryDirectory() as d:
            v=Path(d)/'vault'
            for i in range(2):
                s=v/f'raw/sources/2026/09/src-{i}';s.mkdir(parents=True)
                data=('# Agent session closeout\n\nAgent: codex\nProject: one\nSession SHA-256: '+'a'*64+'\n\nDated report.').encode()
                (s/'original.md').write_bytes(data)
                (s/'source.json').write_text(json.dumps({'source_id':f'src-{i}','original_file':'original.md','sha256':sha(data),'byte_length':len(data)}))
                write_proposal(v,f'prop-{i}','one',[f'src-{i}'])
            items,_=eligible_proposals(v);self.assertEqual(len(build_groups(items)),1)
            (s/'original.md').write_bytes(data+b'tampered')
            items,_=eligible_proposals(v);self.assertEqual(len(build_groups(items)),2)

    def proof(self, root):
        v=root/'vault';state=root/'state';c=state/'curation/run/candidate';j=c/'.curation-transactions/tx-123.json';j.parent.mkdir(parents=True)
        p='proposals/codex/a.md';pid='prop-a';h='a'*64;eid='evt-20260918-test'
        m={'proposal_ids':[pid]};mb=json.dumps(m).encode();(c/'publication.json').write_bytes(mb);digest=sha(mb)
        l=v/('governance/curations/'+eid+'.json');l.parent.mkdir(parents=True)
        l.write_text(json.dumps({'proposals':[{'proposal_id':pid,'path':p,'sha256':h}],'event_id':eid,'transaction':'tx-123','manifest_sha256':digest,'review':{'verdict':'PASS','manifest_sha256':digest}}))
        e=v/('events/2026/09/'+eid+'.json');e.parent.mkdir(parents=True);e.write_text(json.dumps({'event_id':eid,'action':'record-curation','proposal_ids':[pid]}))
        j.write_text(json.dumps({'status':'committed','event_id':eid,'vault':str(v),'manifest':str(c/'publication.json'),'manifest_sha256':digest,'files':[{'path':x.relative_to(v).as_posix(),'after':sha(x.read_bytes())} for x in (l,e)]}))
        row={'result':json.dumps({'proposal_ids':[pid],'paths':[p],'hashes':[h],'state_dir':str(state)})}
        return v,row,l,e,j

    def test_recovery_requires_complete_proof_and_survives_later_page_change(self):
        with tempfile.TemporaryDirectory() as d:
            v,row,l,e,j=self.proof(Path(d));self.assertEqual(canonical_verdict(v,row)[0],'committed')
            e.write_text(e.read_text()+' ');self.assertNotEqual(canonical_verdict(v,row)[0],'committed')
    def test_ledger_or_manifest_alone_cannot_prove_commit(self):
        with tempfile.TemporaryDirectory() as d:
            v,row,l,e,j=self.proof(Path(d));x=json.loads(j.read_text());x['status']='prepared';j.write_text(json.dumps(x))
            self.assertNotEqual(canonical_verdict(v,row)[0],'committed')
    def test_missing_proposal_hash_does_not_prove_commit(self):
        with tempfile.TemporaryDirectory() as d:
            v,row,l,e,j=self.proof(Path(d));x=json.loads(l.read_text());x['proposals'][0].pop('sha256');l.write_text(json.dumps(x))
            self.assertNotEqual(canonical_verdict(v,row)[0],'committed')

    def test_rotation_prevents_first_project_monopoly(self):
        with tempfile.TemporaryDirectory() as d:
            db=sqlite3.connect(':memory:');state=Path(d);calls=[]
            cycle={'id':'test','members':[{'project':'a'},{'project':'b'}]}
            control={'publication':{'enabled':True},'max_projects_per_tick':1}
            def tick(path,cfg,*a,**kw):calls.append(cfg['project_slug']);return {'status':'PUBLISHED','model_calls':1}
            with patch('caretaker.audit.summarize',return_value={}),patch('caretaker.audit.maybe_start_cycle',return_value=cycle),patch('maintenance._project_runtime_cfg',side_effect=lambda base,p,state:('',{'project_slug':p})),patch('maintenance.publication_tick',side_effect=tick):
                for i in range(2):publication_drain(control,{'hub':{'vault':str(state)}},db,state)
            self.assertEqual(calls,['a','b'])

    def test_default_review_uses_fleet_and_repair_gets_new_manifest(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);_,frozen=loop_helpers.FormatRepairTests()._vault(root);cfgdir=root/'cfg';cfgdir.mkdir();cfg=load_config(write_config(cfgdir));run=root/'run';run.mkdir()
            maker=SequenceTransport([json.dumps(_valid_decision()),json.dumps(_valid_decision())],'vllm','Qwen3.8-27B');seen=[]
            def review(c,**kw):
                seen.append(c)
                if len(seen)==1:raise FleetReviewError('independent_review_requires_repair',['Preserve uncertainty'])
                path=c/'review.json';path.write_text('{}');return path,{'model':'muse-spark-1.3'}
            with patch('caretaker.fleet_auditor.review',side_effect=review):
                result=produce_reviewed_candidate(frozen=frozen,run_root=run,observed='2026-09-18T00:00:00+00:00',cfg=cfg,rid='test',maker=maker)
            self.assertEqual(result['status'],'REVIEWED_CANDIDATE');self.assertEqual(len(seen),2);self.assertNotEqual(seen[0],seen[1]);self.assertEqual(result['review_calls'],2)

if __name__=='__main__':unittest.main()
