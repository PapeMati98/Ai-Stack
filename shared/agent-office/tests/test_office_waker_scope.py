"""R6 metadata/wildcard trust; isolated fake driver only, never native wake."""
import uuid
import unittest
from unittest import mock
from coordfs.errors import CoordError
from coordfs.wake import _host_workspace, _normalize_addresses, _waker_allowed
import test_wake as wake_support


class WakerScopeTests(unittest.TestCase):
    # Reuse setup/helpers only. Do not duplicate the inherited historical suite.
    setUp = wake_support.WakeTests.setUp
    tearDown = wake_support.WakeTests.tearDown
    wake = wake_support.WakeTests.wake
    enroll = wake_support.WakeTests.enroll
    reasons = staticmethod(wake_support.WakeTests.reasons)
    def test_r6_absolute_drive_root_lexical(self):
        self.assertEqual(_host_workspace('C:\\', 'windows'), 'C:\\')
        for value in ('C:', 'C:relative', 'C:\\a\\..\\b', '\\\\?\\C:\\', '\\\\.\\C:\\', 'C:\\a:stream'):
            with self.assertRaises(CoordError, msg=value):
                _host_workspace(value, 'windows')

    def test_r6_wildcard_registered_native_only(self):
        allowed = _normalize_addresses(['*', 'codex-host-a', '*'])
        self.assertEqual(allowed, ['*', 'codex-host-a'])
        self.assertTrue(_waker_allowed(self.home, 'codex-host-a', allowed))
        markers = self.home/'office'/'known-agents'; markers.mkdir(parents=True)
        for tool in ('claude', 'codex', 'zcode'):
            sender = tool+'-ailab-'+('sess_' if tool=='zcode' else '')+str(uuid.uuid4())
            self.assertFalse(_waker_allowed(self.home, sender, ['*']))
            if sender != '*': (markers/sender).touch()
            self.assertTrue(_waker_allowed(self.home, sender, ['*']))
            self.assertFalse(_waker_allowed(self.home, sender[:-6], ['*']))
        for sender in ('codex-ailab-process-12-abc', 'codex-ailab-run-job', '*',
                       'codex-ailab-codex-ailab-'+str(uuid.uuid4()), 'claude-ailab-local_'+str(uuid.uuid4())):
            if sender != '*': (markers/sender).touch()
            self.assertFalse(_waker_allowed(self.home, sender, ['*']), sender)

    def test_r6_wildcard_dispatch_keeps_daily_budget_and_actual_sender(self):
        from coordfs.office import Office
        from coordfs.wake import Wake
        sender = 'codex-ailab-'+str(uuid.uuid4())
        receiver = 'claude-ailab-'+str(uuid.uuid4())
        writer = Office(agent_id=sender, home=self.home, rate_limit=1000, now=lambda:self.clock[0])
        writer.presence(ttl_s=900, status='busy', workspace=str(self.workspace))
        wake = self.wake()
        binding = self.enroll(wake, 'wildcard-uat', receiver, ['*'], budget=10)
        # Actual driver is the existing synthetic Python fake, no Desktop/API.
        for i in range(11):
            sent = writer.send(receiver, 'synthetic '+str(i), kind='question', new_recipient=True)
            self.assertEqual(sent['status'], 'ok', sent)
            result = wake.tick()
            if i < 10:
                self.assertEqual(result['counts']['woken'], 1, result)
            else:
                self.assertIn('daily_budget_exhausted', self.reasons(result))
        ledger = wake.ledger()['entries']
        dispatched = [row for row in ledger if row.get('decision')=='woken']
        self.assertEqual(len(dispatched), 10)
        self.assertTrue(all(row.get('trigger_from')==sender for row in dispatched))
