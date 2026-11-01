"""Synthetic tests for range mode over Graph (M2) and IMAP (M3). No network."""
import contextlib
import importlib.util
import json
import unittest
import urllib.parse
from datetime import date, timezone
from email.message import EmailMessage
from pathlib import Path
from unittest import mock

SPEC = importlib.util.spec_from_file_location(
    'mail_direct', Path(__file__).parents[1] / 'scripts' / 'mail_direct.py')
m = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(m)


def graph_mime(subject='Graph synthetic'):
    msg = EmailMessage()
    msg['Subject'] = subject
    msg['From'] = 'a@b.test'
    msg['Date'] = 'Wed, 02 Sep 2026 08:00:00 +0000'
    msg.set_content('Graph synthetic body.')
    return msg.as_bytes()


def imap_mime(subject='IMAP synthetic'):
    msg = EmailMessage()
    msg['Subject'] = subject
    msg['From'] = 'a@b.test'
    msg['Date'] = 'Thu, 03 Sep 2026 08:00:00 +0000'
    msg.set_content('IMAP synthetic body.')
    return msg.as_bytes()


def fake_private(path, create=False):
    path = Path(path)
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


class GraphRangeEnv:
    def __init__(self, tmp, pages, values, profile_kind='graph'):
        self.pages, self.values, self.calls = pages, values, []
        self.stack = contextlib.ExitStack()
        self.stack.enter_context(mock.patch.object(m, 'ARTIFACT_ROOT', Path(tmp)))
        self.stack.enter_context(mock.patch.object(m, 'private_directory', side_effect=fake_private))
        self.stack.enter_context(mock.patch.object(
            m, 'load_profile', return_value={'kind': profile_kind, 'email': 'g@x.test'}))
        self.stack.enter_context(mock.patch.object(m, 'oauth_token', return_value='synthetic'))
        self.stack.enter_context(mock.patch.object(m, 'check_identity'))
        self.stack.enter_context(mock.patch.object(m, 'http', side_effect=self._http))
        self.stack.enter_context(mock.patch.object(m, '_range_graph_value', side_effect=self._value))

    def _http(self, url, **kw):
        self.calls.append(url)
        if 'messages?' in url:
            key = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get('$skiptoken', [None])[0]
            return self.pages.get(key or '', {})
        return {'value': [], '@odata.nextLink': ''}

    def _value(self, state, url, token):
        self.value_urls = getattr(self, 'value_urls', [])
        self.value_urls.append(url)
        mid = url.split('/messages/')[1].split('/')[0]
        return self.values[mid]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.stack.close()
        return False


class GraphRangeTests(unittest.TestCase):
    def _pages(self):
        return {
            '': {'value': [{'id': 'A' * 32, 'conversationId': 'T' * 32,
                            'receivedDateTime': '2026-09-02T10:00:00Z'},
                           {'id': 'B' * 32, 'conversationId': 'U' * 32,
                            'receivedDateTime': '2026-08-01T10:00:00Z'}],
                 '@odata.nextLink': 'https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages?$skiptoken=pg2'},
            'pg2': {'value': [{'id': 'C' * 32, 'conversationId': 'V' * 32,
                               'receivedDateTime': '2026-09-03T10:00:00Z'}],
                    '@odata.nextLink': ''},
        }

    def test_pages_window_and_exhaustion(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            values = {'A' * 32: graph_mime(), 'B' * 32: graph_mime(), 'C' * 32: graph_mime()}
            with GraphRangeEnv(tmp, self._pages(), values) as env:
                result = m.range_run('microsoft-globex', date(2026, 9, 1), date(2026, 9, 30),
                                     Path(tmp) / 'out', max_items=10, max_scan=10)
            self.assertEqual(result['exhausted'], True)
            self.assertEqual(result['batch']['fetched'], 2)  # B is outside the window
            manifest = json.loads((Path(tmp) / 'out' / 'range-manifest.json').read_text())
            self.assertEqual(manifest['items']['B' * 32]['skip_reason'], 'date_outside_window')
            self.assertEqual(len([v for v in manifest['items'].values() if v.get('status') == 'fetched']), 2)

    def test_resume_with_next_url(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / 'out'
            out.mkdir(parents=True)
            values = {'C' * 32: graph_mime()}
            with GraphRangeEnv(tmp, self._pages(), values):
                (out / 'range-manifest.json').write_text(json.dumps({
                    'mode': 'range', 'mode_version': 1, 'alias': 'microsoft-globex',
                    'since': '2026-09-01', 'until': '2026-09-30',
                    'window': {'since': '2026-09-01', 'until': '2026-09-30', 'tz': 'UTC', 'inclusive': True},
                    'items': {}, 'runs': [], 'runs_completed': 0,
                    'totals': {'scanned_total': 0, 'already_processed_total': 0}}), encoding='utf-8')
                key = m.range_key('microsoft-globex', date(2026, 9, 1), date(2026, 9, 30))
                cp = m.range_checkpoint_path(key)
                cp.parent.mkdir(parents=True, exist_ok=True)
                cp.write_text(json.dumps({
                    'checkpoint_version': 1, 'mode': 'range', 'mode_version': 1,
                    'alias': 'microsoft-globex', 'since': '2026-09-01', 'until': '2026-09-30',
                    'key': key, 'out_dir': str(out), 'graph_next_url':
                        'https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages?$skiptoken=pg2',
                        'folder_scope': 'inbox',
                    'restarts': 0, 'runs_completed': 0, 'status': 'stopped_limits',
                    'stop_reason': 'item_limit', 'updated_at': '2026-10-02T00:00:00+00:00'}), encoding='utf-8')
                result = m.range_run('microsoft-globex', date(2026, 9, 1), date(2026, 9, 30),
                                     out, max_items=10, max_scan=10)
            self.assertEqual(result['exhausted'], True)
            self.assertEqual(result['batch']['fetched'], 1)

    def test_too_large_is_terminal_skip(self):
        import tempfile
        pages = {'': {'value': [{'id': 'D' * 32, 'conversationId': 'W' * 32,
                                 'receivedDateTime': '2026-09-02T10:00:00Z'}], '@odata.nextLink': ''}}
        with tempfile.TemporaryDirectory() as tmp:
            with GraphRangeEnv(tmp, pages, {'D' * 32: b'x' * (m.MAX_BYTES + 1)}):
                result = m.range_run('microsoft-globex', date(2026, 9, 1), date(2026, 9, 30),
                                     Path(tmp) / 'out', max_items=5, max_scan=5)
            self.assertEqual(result['batch']['skipped'].get('message_too_large', 0)
                             + result['batch']['skipped'].get('response_too_large', 0), 1)


class FakeImap:
    def __init__(self, uids, messages):
        self.uids, self.messages = uids, messages

    def select(self, folder, readonly=True):
        return 'OK', [b'1']

    def uid(self, command, *args):
        if command == 'search':
            return 'OK', [b' '.join(str(u).encode() for u in self.uids)]
        spec = args[-1]
        uid = int(args[0])
        if uid not in self.messages:
            return 'OK', [b'NOTFOUND']
        raw, stamp, size = self.messages[uid]
        meta = f'{uid} (INTERNALDATE "{stamp}" RFC822.SIZE {size})'.encode()
        return 'OK', [(f'{uid} (INTERNALDATE "{stamp}" RFC822.SIZE {size}) BODY[] {{{len(raw)}}}'.encode(), raw), meta]


class ImapRangeTests(unittest.TestCase):
    def _env(self, tmp, client):
        return contextlib.ExitStack()  # placeholder, patches applied inline below

    def _run(self, tmp, client, **kw):
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(m, 'ARTIFACT_ROOT', Path(tmp)))
            stack.enter_context(mock.patch.object(m, 'private_directory', side_effect=fake_private))
            stack.enter_context(mock.patch.object(
                m, 'load_profile', return_value={'kind': 'imap', 'host': 'h.test', 'port': 993}))
            stack.enter_context(mock.patch.object(m, 'connection', side_effect=lambda a, p: client))
            return m.range_run('example-audit-mail', kw.pop('since', date(2026, 9, 1)),
                               kw.pop('until', date(2026, 9, 30)), Path(tmp) / 'out',
                               imap_factory=lambda a, p: client, **kw)

    def test_window_filter_and_exhaustion(self):
        import tempfile
        messages = {
            101: (imap_mime(), '02-Sep-2026 10:00:00 +0200', 100),
            102: (imap_mime(), '15-Aug-2026 10:00:00 +0200', 100),   # outside window
            103: (imap_mime(), '30-Sep-2026 23:30:00 +0200', 100),
        }
        with tempfile.TemporaryDirectory() as tmp:
            result = self._run(tmp, FakeImap([101, 102, 103], messages), max_items=10, max_scan=10)
            self.assertEqual(result['exhausted'], True)
            self.assertEqual(result['batch']['fetched'], 2)
            manifest = json.loads((Path(tmp) / 'out' / 'range-manifest.json').read_text())
            self.assertEqual(manifest['items']['imap:INBOX:102']['skip_reason'], 'date_outside_window')

    def test_too_large_skip(self):
        import tempfile
        messages = {201: (imap_mime(), '02-Sep-2026 10:00:00 +0200', m.MAX_BYTES + 1)}
        with tempfile.TemporaryDirectory() as tmp:
            result = self._run(tmp, FakeImap([201], messages), max_items=5, max_scan=5)
            self.assertEqual(result['batch']['skipped'].get('message_too_large', 0), 1)

    def test_resume_from_last_uid(self):
        import tempfile
        messages = {
            301: (imap_mime(), '02-Sep-2026 10:00:00 +0200', 100),
            302: (imap_mime(), '03-Sep-2026 10:00:00 +0200', 100),
        }
        with tempfile.TemporaryDirectory() as tmp:
            result = self._run(tmp, FakeImap([301, 302], messages), max_items=1, max_scan=5)
            self.assertEqual(result['exhausted'], False)
            self.assertEqual(result['batch']['fetched'], 1)
            result2 = self._run(tmp, FakeImap([301, 302], messages), max_items=1, max_scan=5)
            self.assertEqual(result2['exhausted'], True)
            manifest = json.loads((Path(tmp) / 'out' / 'range-manifest.json').read_text())
            self.assertEqual(len(manifest['items']), 2)


class GraphGuardTests(unittest.TestCase):
    """Root guard review 41f215a6: INBOX scope, URL allowlist before Bearer,
    exhausted must respect stale unreconciled provider skips."""

    def _base_pages(self):
        return {'': {'value': [{'id': 'A' * 32, 'conversationId': 'T' * 32,
                                'receivedDateTime': '2026-09-02T10:00:00Z'}],
                     '@odata.nextLink': ''}}

    def test_listing_uses_inbox_endpoint_only(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            with GraphRangeEnv(tmp, self._base_pages(), {'A' * 32: graph_mime()}) as env:
                result = m.range_run('microsoft-globex', date(2026, 9, 1), date(2026, 9, 30),
                                     Path(tmp) / 'out', max_items=5, max_scan=5)
            self.assertEqual(result['exhausted'], True)
            listing = [u for u in env.calls if 'messages?' in u]
            value_urls = getattr(env, 'value_urls', [])
            self.assertTrue(value_urls, 'value fetch expected')
            self.assertTrue(all(u.startswith(m.RANGE_GRAPH_VALUE_BASE) for u in value_urls),
                            f'MIME fetch must stay inside the INBOX folder path, got {value_urls}')
            self.assertTrue(listing, "listing call expected")
            self.assertTrue(all(u.startswith(m.RANGE_GRAPH_LIST_BASE + '?') for u in listing),
                            f"listing must use the INBOX endpoint, got {listing}")

    def test_foreign_nextlink_rejected_before_bearer(self):
        import tempfile
        pages = {'': {'value': [{'id': 'A' * 32, 'conversationId': 'T' * 32,
                                 'receivedDateTime': '2026-09-02T10:00:00Z'}],
                      '@odata.nextLink': 'https://evil.example/messages?$skiptoken=x'}}
        with tempfile.TemporaryDirectory() as tmp:
            with GraphRangeEnv(tmp, pages, {'A' * 32: graph_mime()}) as env:
                with self.assertRaises(Exception) as ctx:
                    m.range_run('microsoft-globex', date(2026, 9, 1), date(2026, 9, 30),
                                Path(tmp) / 'out', max_items=5, max_scan=5)
                self.assertIn('checkpoint_url_rejected', str(ctx.exception))
            self.assertFalse(any('evil.example' in u for u in env.calls),
                             "no request may reach a foreign host")

    def test_stale_provider_skip_blocks_exhausted(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / 'out'
            with GraphRangeEnv(tmp, self._base_pages(), {'A' * 32: graph_mime()}):
                stale_id = 'OLD' * 10 + 'X'
                manifest = {'mode': 'range', 'mode_version': 1, 'alias': 'microsoft-globex',
                            'since': '2026-09-01', 'until': '2026-09-30',
                            'window': {'since': '2026-09-01', 'until': '2026-09-30',
                                       'tz': 'UTC', 'inclusive': True},
                            'items': {stale_id: {'provider_message_id': stale_id,
                                                 'thread_id': None, 'internal_date': None,
                                                 'status': 'skipped',
                                                 'skip_reason': 'provider_http_403', 'run': 1}},
                            'runs': [], 'runs_completed': 1,
                            'totals': {'scanned_total': 1, 'already_processed_total': 0}}
                out.mkdir(parents=True, exist_ok=True)
                (out / 'range-manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
                result = m.range_run('microsoft-globex', date(2026, 9, 1), date(2026, 9, 30),
                                     out, max_items=5, max_scan=5)
            self.assertEqual(result['exhausted'], False)
            self.assertEqual(result['stop_reason'], 'item_errors')


class InboxSpellingGuardTests(unittest.TestCase):
    """Graph returns nextLink as mailFolders('inbox')/messages - the second
    canonical spelling of the SAME INBOX endpoint; both pass, all else fails."""

    def test_both_spellings_accepted(self):
        self.assertTrue(m.graph_inbox_url_ok(
            'https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages?$top=3'))
        self.assertTrue(m.graph_inbox_url_ok(
            "https://graph.microsoft.com/v1.0/me/mailFolders('inbox')/messages?%24skiptoken=abc"))

    def test_other_folders_and_hosts_rejected(self):
        rejected = [
            "https://graph.microsoft.com/v1.0/me/mailFolders('archive')/messages",
            'https://graph.microsoft.com/v1.0/me/mailFolders/archive/messages',
            'https://graph.microsoft.com/v1.0/me/messages',
            'https://evil.example/me/mailFolders/inbox/messages',
            'http://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages',
        ]
        for url in rejected:
            self.assertFalse(m.graph_inbox_url_ok(url), url)

    def test_roundtrip_with_parenthesized_nextlink(self):
        import tempfile
        pages = {'': {'value': [{'id': 'A' * 32, 'conversationId': 'T' * 32,
                                 'receivedDateTime': '2026-09-02T10:00:00Z'}],
                      "@odata.nextLink": "https://graph.microsoft.com/v1.0/me/mailFolders('inbox')/messages?%24skiptoken=p2"},
                 'p2': {'value': [], '@odata.nextLink': ''}}
        with tempfile.TemporaryDirectory() as tmp:
            with GraphRangeEnv(tmp, pages, {'A' * 32: graph_mime()}):
                result = m.range_run('microsoft-globex', date(2026, 9, 1), date(2026, 9, 30),
                                     Path(tmp) / 'out', max_items=5, max_scan=5)
            self.assertEqual(result['exhausted'], True)
            self.assertEqual(result['batch']['fetched'], 1)


if __name__ == '__main__':
    unittest.main()

    unittest.main()
