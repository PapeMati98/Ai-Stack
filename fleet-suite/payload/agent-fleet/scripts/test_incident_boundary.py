"""Exercise the real producer/collector contract with synthetic metadata only."""
import json
from pathlib import Path
import tempfile
import unittest

import fleet


class IncidentBoundaryTests(unittest.TestCase):
    def test_all_lifecycle_producers_reach_real_collector(self):
        record = {'run_id':'run-synthetic-001', 'agent':'opencode',
            'model':'glm-5.3-flash', 'adapter':'opencode',
            'model_provider_id':'zai', 'duration_seconds':1.5,
            'completion_reason':'completed', 'execution_profile_fingerprint':'a'*64,
            'token_usage':{'status':'unknown'}}
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            for code, outcome in [('run-completed','completed'), ('run-partial','partial'),
                ('run-failed','failed'), ('run-timeout','partial'), ('run-accepted','accepted'),
                ('run-rejected','rejected'), ('possible-regression','investigate')]:
                event=fleet.run_incident_event(record,stage='provider-run',code=code,outcome=outcome)
                self.assertEqual(event['duration_ms'],1500)
                self.assertIsNone(event['release_hash'])
                self.assertEqual(event['recovery_outcome'],'unknown')
                self.assertIsNone(event['total_tokens'])
                result=fleet.record_incident_event(root,event)
                self.assertNotEqual(result.get('status'),'WARNING',result)
            for code, outcome in [('auditor-transport','blocked'), ('gate-completed','repair'),
                                  ('group-accepted','accepted'), ('group-rejected','rejected')]:
                event=fleet.gate_incident_event(gate_id='gate-synthetic-001',group_id='group-synthetic-001',
                    gate_kind='security',code=code,outcome=outcome,attempt_index=1)
                result=fleet.record_incident_event(root,event)
                self.assertNotEqual(result.get('status'),'WARNING',result)
            result=fleet.record_incident_event(root,fleet.refused_incident_event('run',ValueError('missing private data')))
            self.assertNotEqual(result.get('status'),'WARNING',result)
            files=list((root/'incidents/events').glob('*.json'))
            self.assertEqual(len(files),12)
            for path in files:
                self.assertNotIn('private data',path.read_text())
            self.assertNotEqual(fleet.incident_report(root).get('status'),'UNAVAILABLE')

    def test_missing_provider_is_not_cli_name(self):
        event=fleet.run_incident_event({'adapter':'opencode','duration_seconds':float('nan')},
            stage='acceptance',code='run-rejected',outcome='rejected')
        self.assertIsNone(event['provider'])
        self.assertIsNone(event['duration_ms'])
        self.assertEqual(event['classification'],'unknown')


if __name__=='__main__':
    unittest.main()
