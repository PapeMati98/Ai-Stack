"""Planning intent is required at the live scheduler entry point."""
import copy
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from test_graph_runner import graph_runner, agent_graph


class PlanningDispatchTests(unittest.TestCase):
    def setUp(self):
        self.basis={'kind':'host-capacity-estimate','value':6,
                    'reason':'Measured independent workspaces and available host memory.',
                    'evidence':'host-capacity-fixture'}
        self.requirements={'status':'DECLARED','required_dispatch_node_ids':['node-work'],
                           'manual_declarations':{'node-human':{'mode':'manual'}},
                           'capacity_basis':self.basis}
        self.module=SimpleNamespace(dispatch_requirements=lambda c: self.requirements)
        self.state={'nodes':{'node-work':{'status':'pending'},'node-human':{'status':'pending'}}}
        self.dispatch={'schema_version':2,'max_parallel':6,'nodes':{'node-work':{}},
             'parallelism':{'policy':'estimate','reason':self.basis['reason'],
                           'evidence':{'planning_capacity_basis':self.basis}}}

    def validate(self):
        with patch.object(agent_graph,'load_linked_planning_contract',return_value=(self.module,{})):
            return graph_runner.validate_planning_dispatch(Path('.'),self.state,{},self.dispatch)

    def test_complete_intent_passes(self):
        self.assertEqual(self.validate()['status'],'DECLARED')

    def test_missing_adapter_refused_before_execution(self):
        self.dispatch['nodes']={}
        with self.assertRaisesRegex(agent_graph.GraphError,'Missing planned dispatch'):
            self.validate()

    def test_settled_work_can_omit_adapter(self):
        self.state['nodes']['node-work']['status']='accepted'
        self.dispatch['nodes']={}
        self.validate()

    def test_manual_technical_declaration_not_silently_dispatched(self):
        self.dispatch['nodes']['node-human']={}
        with self.assertRaisesRegex(agent_graph.GraphError,'Manual planning'):
            self.validate()

    def test_capacity_count_policy_reason_and_evidence_bound(self):
        for field,value in [('max_parallel',2),('schema_version',1),('policy','hard'),
                            ('reason','count providers'),('evidence',{'other':'value'})]:
            with self.subTest(field=field):
                before=copy.deepcopy(self.dispatch)
                if field in self.dispatch:
                    self.dispatch[field]=value
                else:
                    self.dispatch['parallelism'][field]=value
                with self.assertRaisesRegex(agent_graph.GraphError,'capacity differs'):
                    self.validate()
                self.dispatch=before

    def test_legacy_keeps_existing_partial_dispatch_contract(self):
        self.requirements={'status':'LEGACY_UNDECLARED'}
        self.dispatch={'schema_version':1,'max_parallel':1,'nodes':{}}
        self.assertEqual(self.validate()['status'],'LEGACY_UNDECLARED')


if __name__=='__main__':
    unittest.main()
