"""Product regressions found during real CLI/native integration; synthetic inputs only."""
import json
from pathlib import Path
import unittest
from unittest.mock import patch
import test_runtime as fixture
from openclaw_control import models
from openclaw_control.telegram import intake_update, OwnerConfig
from openclaw_control.store import ControlStore
from runtime.controller import Controller
from runtime.menus import keyboard
from runtime.run import _registry
from runtime.config import load_config, ConfigError
from runtime.registry import AdapterRegistry
from openclaw_control.adapters import DisabledAdapter
from pipe_discovery import discover_pipe


class IntegrationTests(unittest.TestCase):
    setUp = fixture.RuntimeTests.setUp

    def enqueue(self, text='żółć  e\u0301 🦞\nbez zmian', update=1):
        self.store.set_selection(fixture.CHAT, 't1', 1, 'codex-desktop', fixture.THREAD, 'a', 1)
        return intake_update(self.store, fixture._msg(update, text),
                             OwnerConfig((fixture.OWNER,), fixture.CHAT), self.ctrl.now())

    def native_event(self, row, turn='new-turn', *, when=None, complete=False, append=False):
        prefix = '<codex_delegation>\n  <source_thread_id>' + fixture.CALLER + '</source_thread_id>\n  <input>'
        entry = {'type': 'response_item', 'payload': {
            'type': 'function_call_output', 'name': 'send_message_to_thread',
            'output': prefix + row['raw_text'] + '</input>\n</codex_delegation>',
            'internal_chat_message_metadata_passthrough': {
                'turn_id': turn, 'create_time': when if when is not None else row['delivery_attempt_at'] + .1}}}
        records = [entry]
        if complete:
            records.append({'type': 'event_msg', 'payload': {'type': 'task_complete', 'turn_id': turn,
                                                            'last_agent_message': 'synthetic result'}})
        with self.rollout.open('a' if append else 'w', encoding='utf-8') as out:
            out.write('\n'.join(json.dumps(r, ensure_ascii=False) for r in records) + '\n')

    def test_real_factory_enables_pinned_native_adapter(self):
        self.cfg.codex.enabled = True
        self.cfg.codex.pipe_path = r'\\.\pipe\fixture'
        self.cfg.codex.sessions_root = str(self.root)
        self.cfg.targets[0].rollout_path = str(self.rollout)
        with patch('codex_desktop.pipe_available', return_value=True), \
             patch('codex_desktop.AppTools', return_value=self.app) as factory:
            registry = _registry(self.cfg, self.store)
        factory.assert_called_once()
        self.assertTrue(registry.meta('codex-desktop')['enabled'])

    def test_ack_then_late_native_start_and_completion_survive_restart(self):
        prompt = self.enqueue()
        self.ctrl.dispatch_once()
        row = self.store.require_dispatch(prompt['dispatch_id'])
        self.assertEqual(row['state'], models.DELIVERED)
        self.assertEqual(row['claim_started_at'], 0)
        self.assertGreater(row['delivery_attempt_at'], 0)
        self.native_event(row, turn='old', when=row['delivery_attempt_at'] - 1, complete=True)
        self.ctrl.observe_dispatches()
        self.assertEqual(self.store.require_dispatch(row['dispatch_id'])['state'], models.DELIVERED)
        self.native_event(row)
        reopened = ControlStore(self.cfg.authority_db)
        self.addCleanup(reopened.close)
        restarted = Controller(self.cfg, reopened, {fixture.TOKEN_ENV: fixture.TOKEN}, self.bot.opener,
                               self.registry, now=self.ctrl.now)
        restarted.recover()
        restarted.observe_dispatches()
        self.assertEqual(reopened.require_dispatch(row['dispatch_id'])['state'], models.RUNNING)
        self.native_event(row, complete=True)
        restarted.observe_dispatches()
        self.assertEqual(reopened.require_dispatch(row['dispatch_id'])['state'], models.COMPLETED)
        self.assertEqual(len(self.app.sent), 1)

    def test_unknown_blocks_second_prompt_to_same_native_task(self):
        self.app.lose_ack = True
        first = self.enqueue('pierwszy', 1)
        second = self.enqueue('drugi', 2)
        self.ctrl.dispatch_once()
        self.assertEqual(len(self.app.sent), 1)
        self.assertEqual(self.store.require_dispatch(first['dispatch_id'])['state'], models.DELIVERY_UNKNOWN)
        self.assertEqual(self.store.require_dispatch(second['dispatch_id'])['state'], models.QUEUED)
        self.ctrl.dispatch_once()
        self.assertEqual(len(self.app.sent), 1)

    def test_desktop_offline_keeps_unsent_prompt_for_reconnect(self):
        queued = self.enqueue()
        offline = AdapterRegistry()
        offline.register('codex-desktop', DisabledAdapter('codex-desktop'),
                         verified=True, reason='native-session-unavailable')
        self.ctrl.registry = offline
        self.ctrl.dispatch_once()
        self.assertEqual(self.store.require_dispatch(queued['dispatch_id'])['state'], models.QUEUED)
        self.assertEqual(self.app.sent, [])
        self.ctrl.registry = self.registry
        self.ctrl.dispatch_once()
        self.assertEqual(len(self.app.sent), 1)
    def test_disabled_controller_never_dispatches_existing_queue(self):
        self.enqueue()
        self.cfg.enabled = False
        self.ctrl.once()
        self.assertEqual(self.app.sent, [])
        self.assertEqual(self.bot.methods, [])

    def test_wrong_bot_identity_never_dispatches_existing_queue(self):
        self.enqueue()
        self.bot.me['id'] = 123
        self.ctrl.once()
        self.assertEqual(self.app.sent, [])

    def test_foreign_message_not_persisted_or_answered(self):
        self.bot.updates = [fixture._msg(55, 'foreign content', sender=999)]
        self.ctrl.poll_and_intake()
        self.assertEqual(self.bot.messages, [])
        self.assertEqual(self.store.conn.execute('SELECT count(*) FROM cc_inbox').fetchone()[0], 0)

    def callback(self, action, update):
        return {'update_id': update, 'callback_query': {'id': str(update),
            'from': {'id': fixture.OWNER, 'is_bot': False}, 'data': action,
            'message': {'message_id': 50, 'chat': {'id': fixture.CHAT, 'type': 'private'}}}}

    def test_unbound_prompt_menu_binds_that_exact_prompt(self):
        result = intake_update(self.store, fixture._msg(1, 'oryginał'),
                               OwnerConfig((fixture.OWNER,), fixture.CHAT), self.ctrl.now())
        menu = keyboard(self.store, self.cfg, self.ctrl.now(), dispatch_id=result['dispatch_id'])
        action = menu['inline_keyboard'][0][0]['callback_data']
        update = self.callback(action, 2)
        accepted = intake_update(self.store, update, OwnerConfig((fixture.OWNER,), fixture.CHAT), self.ctrl.now())
        self.assertEqual(accepted['kind'], 'target_selected')
        row = self.store.require_dispatch(result['dispatch_id'])
        self.assertEqual(row['native_thread'], fixture.THREAD)
        self.assertEqual(row['raw_text'], 'oryginał')
        self.ctrl.dispatch_once()
        self.assertEqual(self.app.sent[0]['prompt'], 'oryginał')

    def test_stop_resume_are_owner_callbacks_and_completion_still_observed(self):
        result = self.enqueue()
        self.ctrl.dispatch_once()
        row = self.store.require_dispatch(result['dispatch_id'])
        action = self.store.issue_callback('stop', '', '', '', 0, 'all', self.ctrl.now())
        update = self.callback(action, 40)
        self.bot.updates = [update]
        self.ctrl.poll_and_intake()
        self.assertTrue(self.store.is_stopped())
        self.assertEqual(self.store.require_dispatch(row['dispatch_id'])['state'], models.CANCEL_PENDING)
        self.native_event(row, complete=True)
        self.ctrl.observe_dispatches()
        self.assertEqual(self.store.require_dispatch(row['dispatch_id'])['state'], models.COMPLETED)
        action = self.store.issue_callback('resume', '', '', '', 0, 'all', self.ctrl.now())
        self.bot.updates = [self.callback(action, 41)]
        self.ctrl.poll_and_intake()
        self.assertFalse(self.store.is_stopped())
        self.assertEqual(len(self.app.sent), 1)

    def test_crash_after_menu_send_does_not_send_second_menu(self):
        calls = []
        def uncertain(*args, **kwargs):
            calls.append(1)
            raise OSError('lost acknowledgement')
        self.ctrl.api.send_text = uncertain
        self.ctrl._send_menu(self.ctrl.now())
        self.ctrl.recover()
        self.ctrl._send_menu(self.ctrl.now())
        self.assertEqual(calls, [1])

    def test_discovery_requires_unique_pinned_process(self):
        image = self.root / 'pinned.exe'
        image.write_bytes(b'synthetic')
        a = 'codex-browser-use-11111111-1111-1111-1111-111111111111'
        b = 'codex-browser-use-22222222-2222-2222-2222-222222222222'
        lookup = lambda pipe: (str(image), 12)
        self.assertEqual(discover_pipe(str(image), names=[a], inspect=lookup)[1], 12)
        self.assertEqual(discover_pipe(str(image), names=[a, b], inspect=lookup)[1], 12)
        with self.assertRaises(OSError):
            discover_pipe(str(image), names=[a, b], inspect=lambda p: (str(image), 12 if p.endswith(a) else 13))
        with self.assertRaises(OSError):
            discover_pipe(str(image), names=['other-pipe'], inspect=lookup)

    def test_nontelegram_host_rejected_before_credentials(self):
        doc = json.loads(Path(self.cfg_path).read_text(encoding='utf-8'))
        doc['telegram']['host'] = 'other.invalid'
        with open(self.cfg_path, 'w', encoding='utf-8') as out:
            json.dump(doc, out)
        with self.assertRaises(ConfigError):
            load_config(self.cfg_path)


if __name__ == '__main__':
    unittest.main()
