import json,os
from test_model_tiers import Base,TodoFleetIntegrationTests,CATALOG

class OwnerSol6GuardTests(Base):
    def run_case(self,model,tier_mode,catalog_state):
        os.environ['AGENT_GRAPH_TIER_ROUTING']=tier_mode
        self.catalog_path.write_text(json.dumps(CATALOG),encoding='utf-8')
        if catalog_state=='corrupt':self.catalog_path.write_text('[]',encoding='utf-8')
        fleet=TodoFleetIntegrationTests.fleet(self,['codex'])
        candidate={'agent':'codex','model':model,'score':100,'reason':'synthetic transport test; never a real model invocation'}
        result=None
        try:result,_=TodoFleetIntegrationTests.run_node(self,[candidate],fleet)
        except (ValueError,AttributeError):
            if catalog_state!='corrupt':raise
        return result,fleet

    def test_retired_model_never_executes_with_valid_or_corrupt_catalog(self):
        for tier_mode in ('0','1'):
            for state in ('valid','corrupt'):
                for model in ('gpt-6-sol','OPENAI/GPT-6-SOL','gpt-6-sol-20261002'):
                    with self.subTest(mode=tier_mode,state=state,model=model):
                        result,fleet=self.run_case(model,tier_mode,state)
                        self.assertFalse(any(c['model_override'].rsplit('/',1)[-1].lower().startswith('gpt-6-sol') for c in fleet.execute_calls),'Forbidden model reached execution')
                        if result is not None and result['status']=='CANDIDATE':
                            self.assertTrue(fleet.execute_calls,'A legitimate qualified substitute must execute')

    def test_successor_is_not_denied_by_retired_model_guard(self):
        result,fleet=self.run_case('gpt-6.1-sol','0','valid')
        self.assertEqual(len(fleet.execute_calls),1)
        self.assertEqual(fleet.execute_calls[0]['model_override'],'gpt-6.1-sol')
        self.assertEqual(result['status'],'CANDIDATE')

    def test_existing_eligible_model_keeps_legacy_fallback(self):
        result,fleet=self.run_case('gpt-6-luna','0','corrupt')
        self.assertEqual(len(fleet.execute_calls),1)
        self.assertEqual(result['status'],'CANDIDATE')
