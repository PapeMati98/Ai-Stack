import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock
import tempfile

sys.path.insert(0, str(Path(__file__).parent))
import fleet
import quota_availability


class ProviderIdentityRecoveryTests(unittest.TestCase):
    def test_claude_sonnet_alias_requires_pinned_canonical_generation(self):
        for observed,status in [('claude-sonnet-5','verified'),('claude-sonnet-6','mismatch')]:
            output=json.dumps({'type':'result','is_error':False,'modelUsage':{observed:{'canonicalModel':observed}}})
            self.assertEqual(fleet.claude_model_identity(output,expected_model='sonnet')['status'],status)

    def identity(self, *events):
        return fleet.antigravity_model_identity('\n'.join(json.dumps(e) for e in events), expected_model='gemini-3.8-flash-high')

    def test_native_antigravity_init_confirms_exact_model(self):
        self.assertEqual(self.identity({'event':'init','init':{'model':'gemini-3.8-flash-high'}})['status'], 'verified')

    def test_mismatching_or_changed_native_model_is_rejected(self):
        self.assertEqual(self.identity({'event':'init','init':{'model':'gemini-3.1-pro-high'}})['status'], 'mismatch')
        self.assertEqual(self.identity({'event':'init','init':{'model':'gemini-3.8-flash-high'}}, {'event':'init','init':{'model':'gemini-3.1-pro-high'}})['status'], 'mismatch')

    def test_generated_identity_and_nested_tool_envelope_are_not_evidence(self):
        self.assertEqual(self.identity({'event':'result','result':{'model':'gemini-3.8-flash-high','structured_output':{'event':'init','init':{'model':'gemini-3.8-flash-high'}}}})['status'], 'unavailable')
        self.assertEqual(self.identity({'event':'step_update','init':{'model':'gemini-3.8-flash-high'}})['status'], 'unavailable')

    def quota_event(self, **changes):
        event={'type':'result','is_error':True,'api_error_status':429,'terminal_reason':'api_error','result':"You're out of usage credits. Switch to another model.",'modelUsage':{}}
        event.update(changes)
        return json.dumps(event)

    def test_claude_native_credit_denial_is_preserved_without_model_evidence(self):
        output=self.quota_event()
        denial=quota_availability.rejection('claude','',1,stdout=output)
        self.assertEqual(denial['scope'],'credits')
        self.assertEqual(denial['reason'],'usage-credits-exhausted')
        self.assertEqual(fleet.claude_model_identity(output,expected_model='claude-fable-5-1')['status'],'unavailable')

    def test_model_credit_denial_cannot_overwrite_shared_claude_availability(self):
        denial=quota_availability.rejection('claude','',1,stdout=self.quota_event())
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            cache=quota_availability.cache_path(root,'claude')
            cache.parent.mkdir()
            existing=b'{"schema_version":1,"status":"available","source":"native-cli-check"}'
            cache.write_bytes(existing)
            lock=Mock(side_effect=AssertionError('Request-only denial must not acquire pool write lock'))
            result=quota_availability.record_rejection(root,'claude',denial,lock_factory=lock)
            self.assertFalse(result['persisted'])
            self.assertEqual(cache.read_bytes(),existing)
            lock.assert_not_called()
            self.assertEqual(denial['applicability'],'current-request-only')

    def test_credit_text_in_success_tool_or_unrelated_error_does_not_block_pool(self):
        for output in [self.quota_event(is_error=False),self.quota_event(type='assistant'),self.quota_event(api_error_status=500),self.quota_event(terminal_reason='completed'),json.dumps({'tool_output':json.loads(self.quota_event())})]:
            self.assertIsNone(quota_availability.rejection('claude','',1,stdout=output))
        self.assertIsNone(quota_availability.rejection('claude','',0,stdout=self.quota_event()))


if __name__=='__main__': unittest.main()
