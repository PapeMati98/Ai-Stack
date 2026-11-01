"""Mercury Graph boundary: no real credentials, processes or model calls."""
import json
import os
import unittest
from unittest.mock import patch
import todo_fleet
import history_scout
from test_todo_fleet import FakeFleet
import test_todo_fleet as fixtures


class MercuryBridgeTests(unittest.TestCase):
    def setUp(self):
        self.h=fixtures.TodoFleetTests();self.h.setUp();self.addCleanup(self.h.tearDown)
        self.addCleanup(self.h.doCleanups)
        self.f=FakeFleet()
        self.f.DEFAULT_PROFILES={'opencode-mercury':{
            'adapter':'opencode','supported_modes':['advisory','review','implement'],
            'models':{'low':'mercury-3','medium':'mercury-3','high':'mercury-3'}}}
        self.admissions=[]
        def admitted(profile,workspace):
            self.admissions.append(str(workspace));return workspace
        self.f.qualify_mercury_workspace=admitted
        self.f.result['usage_record']=str(self.h.write_usage())

    def run_case(self,extra=None,request_extra=None):
        task=self.h.task(candidates=[self.h.candidate('opencode-mercury','mercury-3')])
        task.update(extra or {})
        request=self.h.request(task=task);request.update(request_extra or {})
        return self.h.execute(request,self.f)

    def test_admitted_task_strips_office_and_uses_native_profile(self):
        self.run_case(request_extra={'office_parent':'codex-ailab-test','office_channel':'synthetic-test'})
        call=self.f.execute_calls[0]
        self.assertEqual(call['agent_id'],'opencode-mercury')
        self.assertNotIn('office_parent',call);self.assertNotIn('office_channels',call)
        payload=self.f.execute_args[0][0]
        self.assertNotIn('codex-ailab-test',payload)
        self.assertNotIn('synthetic-test',payload)
        self.assertTrue(self.admissions)

    def test_enriched_or_browser_tasks_fail_before_probes_and_dispatch(self):
        for extra in ({'knowledge':{'source':'synthetic'}},
                      {'text':history_scout.HISTORY_HEADER+'{}'},
                      {'web':'managed-chrome'}):
            with self.subTest(extra=extra):
                self.f.chosen.clear();self.f.quota_profiles.clear()
                self.run_case(extra)
                self.assertEqual(self.f.execute_calls,[])
                self.assertNotIn('opencode-mercury',self.f.chosen)
                self.assertNotIn('opencode-mercury',self.f.quota_profiles)

    def test_missing_or_refused_admission_fails_before_probes(self):
        def refuse(*args):raise ValueError('outside synthetic root')
        for predicate in (None,refuse):
            self.f.qualify_mercury_workspace=predicate
            self.run_case()
            self.assertEqual(self.f.execute_calls,[])
            self.assertNotIn('opencode-mercury',self.f.chosen)
            self.assertNotIn('opencode-mercury',self.f.quota_profiles)

    def test_worker_inputs_remain_local_and_hash_verified_without_mount(self):
        source=self.h.workspace/'source.txt';source.write_text('synthetic',encoding='utf8')
        self.run_case({'inputs':['source.txt']})
        self.assertNotIn('required_inputs_file',self.f.execute_calls[0])
        self.assertTrue((self.h.root/'out'/'required-inputs.json').exists())


if __name__=='__main__':unittest.main()
