import contextlib
import importlib.util
import json
import os
import subprocess
import sys
from datetime import date, datetime, timezone
from email.message import EmailMessage
from pathlib import Path
import tempfile
import unittest
import urllib.parse
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('mail_direct', Path(__file__).parents[1] / 'scripts' / 'mail_direct.py')
m = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(m)


def temporary():
    root = Path.home() / '.agents' / 'task-evidence' / 'email-direct-tests'
    root.mkdir(parents=True, exist_ok=True)
    # Keep synthetic artifacts; do not perform implicit filesystem deletion.
    return tempfile.TemporaryDirectory(dir=root, delete=False)


def message(subject='Akcja ExampleCo', day='Thu, 01 Oct 2026 10:00:00 +0200'):
    msg = EmailMessage()
    msg['Subject'], msg['Date'], msg['From'] = subject, day, 'demo@example.test'
    msg.set_content('Synthetic body only.')
    return msg.as_bytes()


class MailTests(unittest.TestCase):
    def test_dpapi_roundtrip_and_not_plaintext(self):
        raw = b'SYNTHETIC-secret-9382'
        encrypted = m.dpapi(raw)
        self.assertNotIn(raw, encrypted)
        self.assertEqual(m.dpapi(encrypted, decrypt=True), raw)

    def test_read_scope_date_and_fold(self):
        day = date(2026, 10, 1)
        self.assertTrue(m.matches(message('Re: Akcja   ORLEN'), ['akcja orlen'], day, day))
        self.assertFalse(m.matches(message(), ['unrelated'], day, day))
        self.assertFalse(m.matches(message(day='Wed, 30 Sep 2026 10:00:00 +0200'), ['ExampleCo'], day, day))
        self.assertFalse(m.matches(message(day='invalid'), ['ExampleCo'], day, day))

    def test_profile_tls_and_alias_isolation(self):
        good = {'kind':'pop3', 'email':'demo@example.test', 'host':'pop3.example-mail-one.invalid', 'port':995, 'ssl':True}
        m.validate_profile('example-pop3', good)
        for change in [{'ssl':False}, {'host':'attacker.test'}, {'kind':'imap'}]:
            with self.assertRaises(m.MailError):
                m.validate_profile('example-pop3', dict(good, **change))
        with self.assertRaises(m.MailError):
            m.load_profile('../other')

    def test_http_does_not_allow_credential_redirects_or_mail_writes(self):
        with self.assertRaises(m.MailError):
            m.NoRedirect().redirect_request(None, None, 302, '', {}, 'https://attacker.test')
        for url, form in [('http://graph.microsoft.com/v1.0/me',None), ('https://attacker.test',None), ('https://graph.microsoft.com/v1.0/me/sendMail', {'data':'synthetic'})]:
            with self.assertRaises(m.MailError):
                m.http(url, form=form)

    def test_pop_reads_only_matching_messages_without_dele(self):
        class FakePOP:
            calls=[]
            def stat(self): return (3,1000)
            def list(self): return (b'+OK',[b'1 200',b'2 200',b'3 200'],0)
            def top(self,n,_):
                self.calls.append(('TOP',n));return (b'+OK',message('Akcja ExampleCo' if n==2 else 'unrelated').splitlines(),0)
            def retr(self,n):
                self.calls.append(('RETR',n));return (b'+OK',message().splitlines(),0)
        client=FakePOP();day=date(2026,10,1)
        raw, stats=m.pop_messages(client,['ExampleCo'],day,day,3,40)
        self.assertEqual(len(raw),1)
        self.assertEqual([n for cmd,n in client.calls if cmd=='RETR'],[2])
        self.assertFalse(stats['scan_truncated'])

    def test_imap_body_peek_and_limit(self):
        class FakeIMAP:
            calls=[]
            def uid(self, command, *args):
                self.calls.append((command,args))
                if command=='search':return ('OK',[b'1 2'])
                if args[-1]=='(BODY.PEEK[HEADER] RFC822.SIZE)':return ('OK',[(b'2 (RFC822.SIZE 200)',message())])
                if args[-1]=='(BODY.PEEK[])':return ('OK',[(b'2',message())])
                raise AssertionError('Mutating IMAP command')
        client=FakeIMAP();day=date(2026,10,1)
        raw,stats=m.imap_messages(client,['ExampleCo'],day,day,5,1)
        self.assertEqual(len(raw),1)
        self.assertTrue(stats['limit_reached'])
        self.assertEqual([c for c,_ in client.calls],['search','fetch','fetch'])

    def test_attachments_cannot_escape_and_same_name_different_bytes_retained(self):
        msg=EmailMessage();msg['Subject']='Synthetic';msg.set_content('Body')
        msg.add_attachment(b'one',maintype='application',subtype='octet-stream',filename='../../evil.xlsx')
        msg.add_attachment(b'two',maintype='application',subtype='octet-stream',filename='../../evil.xlsx')
        def private(p, create=False):
            if create:
                p.mkdir(parents=True, exist_ok=True)
            return p
        with temporary() as tmp, patch.object(m,'private_directory',side_effect=private), patch.object(m,'ARTIFACT_ROOT',Path(tmp)):
            root=Path(tmp);entries,manifest=m.save_messages([msg.as_bytes()],root)
            paths=[Path(a['path']) for a in entries[0]['excel']]
            self.assertEqual(len(set(paths)),2)
            self.assertTrue(all(p.is_relative_to(root) for p in paths))
            self.assertEqual({p.read_bytes() for p in paths},{b'one',b'two'})

    def test_plaintext_secrets_not_in_error_output(self):
        with patch.object(m,'load_profile',side_effect=Exception('secret-value')), patch('sys.argv',['mail_direct','doctor','--alias','example-pop3']), patch('builtins.print') as output:
            self.assertEqual(m.main(),1)
            self.assertNotIn('secret-value',str(output.call_args))

    def test_identity_mismatch_stops_before_mail_read(self):
        p={'kind':'gmail','email':'expected@example.test'}
        with patch.object(m,'http',return_value={'emailAddress':'other@example.test'}),patch.object(m,'save_profile') as save:
            with self.assertRaises(m.MailError):m.check_identity('google-example-3',p,'synthetic')
            save.assert_not_called()

    def test_private_acl_rejects_unexpected_allow_and_reparse(self):
        with temporary() as tmp:
            class Fake:
                returncode=9
                stdout=''
            with patch.object(m,'hidden',return_value=Fake()):
                with self.assertRaises(m.MailError):m.private_directory(Path(tmp))

    def test_gmail_polish_subject_and_epoch_dates(self):
        raw=message('Doposażenie ekspresów');calls=[]
        def api(url,**kwargs):
            calls.append((url,kwargs))
            if '/messages?' in url:return {'messages':[{'id':'abc123'}]}
            if 'format=metadata' in url:return {'payload':{'headers':[{'name':'Subject','value':'Doposażenie ekspresów'},{'name':'Date','value':'Thu, 01 Oct 2026 10:00:00 +0200'}]}}
            if 'format=raw' in url:return {'raw':m.base64.urlsafe_b64encode(raw).decode().rstrip('=')}
            raise AssertionError(url)
        with patch.object(m,'oauth_token',return_value='synthetic'),patch.object(m,'check_identity'),patch.object(m,'http',side_effect=api):
            found,_=m.api_messages('google-example-3',{'kind':'gmail'},['doposażenie'],date(2026,10,1),date(2026,10,1),10,5)
        self.assertEqual(found,[raw])
        query=urllib.parse.parse_qs(urllib.parse.urlsplit(calls[0][0]).query)['q'][0]
        self.assertRegex(query,r'^after:\d+ before:\d+$')
        self.assertEqual(sum('format=raw' in u for u,_ in calls),1)
        self.assertTrue(all('form' not in k for _,k in calls))

    def test_graph_lower_bound_and_polish_subject_mime(self):
        calls=[];raw=message('Doposażenie ekspresów','Thu, 01 Oct 2026 00:30:00 +0200')
        def api(url,**kwargs):
            calls.append(url);return {'value':[{'id':'opaque/id=','subject':'Doposażenie ekspresów'}]}
        with patch.object(m,'oauth_token',return_value='synthetic'),patch.object(m,'check_identity'),patch.object(m,'http',side_effect=api),patch.object(m,'graph_mime',return_value=raw) as mime:
            found,_=m.api_messages('microsoft-example-1',{'kind':'graph'},['doposażenie'],date(2026,10,1),date(2026,10,1),10,5)
        self.assertEqual(found,[raw])
        filt=urllib.parse.parse_qs(urllib.parse.urlsplit(calls[0]).query)['$filter'][0]
        self.assertIn('ge 2026-09-30T00:00:00Z',filt)
        mime.assert_called_once_with('opaque%2Fid%3D','synthetic')

    def test_imap_local_midnight_prefilter_is_widened(self):
        class FakeIMAP:
            def uid(self,*args):self.args=args;return ('OK',[b''])
        client=FakeIMAP()
        m.imap_messages(client,['ExampleCo'],date(2026,10,1),date(2026,10,1),10,5)
        self.assertIn('30-Sep-2026',client.args)

    def test_cloud_sync_and_dotdot_outputs_rejected(self):
        for path in [Path('C:/Users/example/OneDrive - Example/a'),Path('E:/ExampleCo/a'),m.ARTIFACT_ROOT/'..'/'other']:
            with self.assertRaises(m.MailError):m.artifact_path(path)

    def test_real_windows_junction_rejected_without_following_target(self):
        with temporary() as tmp:
            root=Path(tmp);target=root/'target';target.mkdir();junction=root/'junction'
            p=m.hidden(f'cmd.exe /d /c mklink /J "{junction}" "{target}"',timeout=20)
            self.assertEqual(p.returncode,0)
            with self.assertRaisesRegex(m.MailError,'reparse_path_rejected'):
                m.no_links(junction/'child')
            self.assertFalse((target/'child').exists())

    def test_acl_inspection_error_rejects_even_private_text(self):
        class Fake:
            returncode=1
            stdout='PRIVATE'
        with temporary() as tmp,patch.object(m,'hidden',return_value=Fake()):
            with self.assertRaisesRegex(m.MailError,'nonprivate_acl_rejected'):
                m.private_directory(Path(tmp))

    def test_api_last_page_budget_reports_incomplete_coverage(self):
        for kind,alias in [('gmail','google-example-3'),('graph','microsoft-example-1')]:
            def api(url,**kwargs):
                if kind=='graph':return {'value':[{'id':str(i),'subject':'unrelated'} for i in range(5)]}
                if 'format=metadata' in url:return {'payload':{'headers':[{'name':'Subject','value':'unrelated'}]}}
                return {'messages':[{'id':str(i)} for i in range(5)]}
            with patch.object(m,'oauth_token',return_value='synthetic'),patch.object(m,'check_identity'),patch.object(m,'http',side_effect=api):
                found,stats=m.api_messages(alias,{'kind':kind},['ExampleCo'],date(2026,10,1),date(2026,10,1),3,40)
            self.assertEqual(found,[])
            self.assertEqual(stats['headers_scanned'],3)
            self.assertTrue(stats['scan_truncated'])

    def test_graph_null_subject_does_not_abort_read(self):
        with patch.object(m,'oauth_token',return_value='synthetic'),patch.object(m,'check_identity'),patch.object(m,'http',return_value={'value':[{'id':'abc','subject':None}]}):
            found,stats=m.api_messages('microsoft-example-1',{'kind':'graph'},['ExampleCo'],date(2026,10,1),date(2026,10,1),3,40)
        self.assertEqual(found,[])
        self.assertFalse(stats['scan_truncated'])

    def test_imap_size_after_header_literal_is_supported(self):
        class FakeIMAP:
            def uid(self,command,*args):
                if command=='search':return ('OK',[b'1'])
                if args[-1]=='(BODY.PEEK[HEADER] RFC822.SIZE)':return ('OK',[(b'1 (BODY[HEADER] {200}',message()),b' RFC822.SIZE 200)'])
                if args[-1]=='(BODY.PEEK[])':return ('OK',[(b'1',message())])
                raise AssertionError('Mutating command')
        found,_=m.imap_messages(FakeIMAP(),['ExampleCo'],date(2026,10,1),date(2026,10,1),3,40)
        self.assertEqual(len(found),1)


def utc_ms(*parts):
    return round(datetime(*parts, tzinfo=timezone.utc).timestamp() * 1000)


def range_message(subject='Synthetic range item'):
    msg = EmailMessage()
    msg['Subject'], msg['From'] = subject, 'demo@example.test'
    msg['Date'] = 'Tue, 01 Sep 2026 08:00:00 +0000'
    msg.set_content('Synthetic range body only.')
    return msg.as_bytes()


def fake_private(path, create=False):
    path = Path(path)
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def default_item(internal=None):
    return {'internalDate': internal if internal is not None else utc_ms(2026, 9, 1, 12), 'raw': range_message()}


class RangeEnv:
    """Synthetic Gmail HTTP double plus module patches; no network, no real data."""

    def __init__(self, tmp, listing, items):
        self.tmp, self.items, self.calls, self.sleeps, self.listing_calls = Path(tmp), items, [], [], 0
        self.listing = listing

        def fake_http(url, **kwargs):
            self.calls.append((url, kwargs))
            meter = kwargs.get('meter')
            if 'format=raw' in url:
                mid = urllib.parse.urlsplit(url).path.rsplit('/', 1)[-1]
                item = items[mid]
                if isinstance(item, Exception):
                    raise item
                if meter is not None:
                    meter(item.get('meter', 0))
                return {'id': mid, 'threadId': 't-' + mid, 'internalDate': item['internalDate'],
                        'raw': m.base64.urlsafe_b64encode(item['raw']).decode().rstrip('=')}
            self.listing_calls += 1
            page = self.listing(urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
                                .get('pageToken', [None])[0], self.listing_calls)
            if meter is not None:
                meter(0)
            return page

        self.stack = contextlib.ExitStack()
        self.stack.enter_context(patch.object(m, 'ARTIFACT_ROOT', Path(tmp)))
        self.stack.enter_context(patch.object(m, 'private_directory', side_effect=fake_private))
        self.stack.enter_context(patch.object(m, 'load_profile',
                                              return_value={'kind': 'gmail', 'email': 'range@example.test'}))
        self.stack.enter_context(patch.object(m, 'oauth_token', return_value='synthetic'))
        self.stack.enter_context(patch.object(m, 'check_identity'))
        self.stack.enter_context(patch.object(m, 'http', side_effect=fake_http))

    def run(self, **kwargs):
        kwargs.setdefault('sleeper', self.sleeps.append)
        return m.range_run('google-example-1', date(2026, 9, 1), date(2026, 9, 2),
                           self.tmp / 'out', **kwargs)

    @property
    def manifest(self):
        return json.loads((self.tmp / 'out' / m.RANGE_MANIFEST_NAME).read_text(encoding='utf-8'))

    @property
    def checkpoint_files(self):
        return sorted((self.tmp / 'range-checkpoints').glob('*.json'))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return self.stack.__exit__(*exc)


def static_pages(pages):
    """pages: dict pageToken-or-None -> page dict; reused verbatim on resume."""
    return lambda token, _: pages[token]


def page(ids, next_token=None):
    entry = {'messages': [{'id': i, 'threadId': 't-' + i} for i in ids]}
    if next_token is not None:
        entry['nextPageToken'] = next_token
    return entry


class RangeModeTests(unittest.TestCase):
    def test_rejects_unknown_aliases_before_any_io(self):
        # Owner decision 2026-10-04: range is open to all nine mailboxes;
        # only names outside the configured account set are refused.
        for alias in ('microsoft-umbrella', 'other'):
            with self.assertRaisesRegex(m.MailError, 'range_alias_unsupported'):
                m.range_run(alias, date(2026, 9, 1), date(2026, 9, 2), Path('unused') / 'out')
        for alias in ('example-pop3', 'google-example-3', 'google-example-2', 'microsoft-example-1'):
            try:
                m.range_run(alias, date(2026, 9, 1), date(2026, 9, 2), Path('unused') / 'out')
            except m.MailError as exc:
                self.assertNotEqual(str(exc), 'range_alias_unsupported',
                                    f'{alias} must no longer be allowlist-refused')

    def test_pop3_range_top_first_date_filter_retr_only_in_window(self):
        class FakePOP:
            calls = []

            def stat(self):
                return (3, 1000)

            def list(self):
                return (b'+OK', [b'1 200', b'2 200', b'3 200'], 0)

            def top(self, n, _):
                self.calls.append(('TOP', n))
                if n == 3:
                    body = message(day='Thu, 01 Oct 2026 10:00:00 +0200')
                elif n == 2:
                    body = message(day='Mon, 01 Sep 2025 10:00:00 +0200')  # outside window
                else:
                    body = b'Subject: no date header'
                return (b'+OK', body.splitlines(), 0)

            def retr(self, n):
                self.calls.append(('RETR', n))
                return (b'+OK', message(day='Thu, 01 Oct 2026 10:00:00 +0200').splitlines(), 0)

            def quit(self):
                self.calls.append(('QUIT',))

        client = FakePOP()
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / 'range-out'
            out.mkdir(parents=True)
            with patch.object(m, 'connection', return_value=client), \
                 patch.object(m, 'load_profile', return_value={'kind': 'pop3'}), \
                 patch.object(m, 'private_directory', side_effect=lambda p, create=False: Path(tmp) / 'priv'), \
                 patch.object(m, 'range_checkpoint_path', return_value=Path(tmp) / 'cp.json'), \
                 patch.object(m, 'private_directory'), \
                 patch.object(m, 'persist_message', side_effect=lambda root, raw: {
                     'subject': 'Synthetic', 'date': 'Thu, 01 Oct 2026', 'from': 'demo@example.test',
                     'message_id': f'<{len(raw)}@test>', 'eml': str(root), 'sha256': '0' * 64, 'excel': []}):
                result = m.range_run_pop3('example-pop3', {'kind': 'pop3'},
                                          date(2026, 9, 25), date(2026, 10, 5), out, Path(tmp) / 'cp.json',
                                          max_items=10, max_scan=10, max_calls=50,
                                          max_bytes=1024 * 1024, max_seconds=60,
                                          retry_attempts=2, retry_base_sleep=0.0,
                                          clock=lambda: 0.0, sleeper=lambda _: None)
        self.assertEqual(result['status'], 'RANGE_OK')
        retr = [n for cmd, n in client.calls if cmd == 'RETR']
        tops = [n for cmd, n in client.calls if cmd == 'TOP']
        self.assertEqual(retr, [3], 'only the in-window newest message may be fetched')
        self.assertEqual(tops, [3, 2, 1], 'walk is newest-first and scans all three')
        self.assertIn('date_outside_window', str(result['batch']['skipped']) + str(result['coverage']))

    def test_scope_validation_caps_match_read_limits(self):
        with temporary() as tmp, RangeEnv(tmp, static_pages({None: {'messages': []}}), {}) as env:
            for bad in ({'max_items': 501}, {'max_items': 0}, {'max_scan': 5001}, {'max_scan': 0},
                        {'max_calls': 0}, {'max_bytes': 0}, {'max_seconds': 0}, {'retry_attempts': 0}):
                with self.assertRaisesRegex(m.MailError, 'range_scope_invalid'):
                    env.run(**bad)
        with self.assertRaisesRegex(m.MailError, 'range_scope_invalid'):
            m.range_run('google-example-1', date(2026, 9, 2), date(2026, 9, 1), Path('unused') / 'out')

    def test_internal_date_as_digit_string_is_accepted(self):
        """Regression 2026-10-02: Gmail format=raw returns internalDate as STRING."""
        items = {'strdate': default_item(str(utc_ms(2026, 9, 1, 12, 0, 0)))}
        listing = static_pages({None: page(list(items))})
        with temporary() as tmp, RangeEnv(tmp, listing, items) as env:
            result = env.run()
        self.assertEqual(result['status'], 'RANGE_OK')
        self.assertEqual(result['batch']['fetched'], 1)
        self.assertEqual(result['batch']['skipped'], {})

    def test_utc_inclusive_boundaries_and_no_subject_or_thread_calls(self):
        items = {'in1': default_item(utc_ms(2026, 9, 1, 0, 0, 0)),
                 'out0': default_item(utc_ms(2026, 8, 31, 23, 59, 59, 999)),
                 'in2': default_item(utc_ms(2026, 9, 2, 23, 59, 59, 999)),
                 'out3': default_item(utc_ms(2026, 9, 3, 0, 0, 0))}
        listing = static_pages({None: page(list(items))})
        with temporary() as tmp, RangeEnv(tmp, listing, items) as env:
            result = env.run()
        self.assertEqual(result['exhausted'], True)
        self.assertEqual(result['status'], 'RANGE_OK')
        self.assertEqual(result['batch']['fetched'], 2)
        self.assertEqual(result['batch']['skipped'], {'date_outside_window': 2})
        lower = int(datetime(2026, 9, 1, tzinfo=timezone.utc).timestamp())
        upper = int(datetime(2026, 9, 3, tzinfo=timezone.utc).timestamp())
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(env.calls[0][0]).query)['q'][0]
        self.assertEqual(query, f'after:{lower} before:{upper}')
        self.assertRegex(query, r'^after:\d+ before:\d+$')  # no subject terms
        self.assertTrue(all('/threads' not in url for url, _ in env.calls))
        self.assertTrue(all('form' not in kw for _, kw in env.calls))
        self.assertIs(result['mail_sent'], False)
        self.assertIs(result['mail_flags_changed'], False)

    def test_pagination_over_500_and_resume_merges_without_duplicates(self):
        ids = [f'm{i:03d}' for i in range(600)]
        pages = {None: page(ids[:200], 't1'), 't1': page(ids[200:400], 't2'), 't2': page(ids[400:600])}
        with temporary() as tmp, RangeEnv(tmp, static_pages(pages), {i: default_item() for i in ids}) as env:
            first = env.run(max_items=500)
            second = env.run()
        self.assertEqual(first['batch']['fetched'], 500)
        self.assertFalse(first['exhausted'])
        self.assertEqual(first['stop_reason'], 'item_limit')
        self.assertEqual(second['batch']['fetched'], 100)
        self.assertTrue(second['exhausted'])
        for url, _ in env.calls:
            if 'format=raw' not in url:
                self.assertLessEqual(int(urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)['maxResults'][0]), 500)
        manifest = json.loads((env.tmp / 'out' / m.RANGE_MANIFEST_NAME).read_text(encoding='utf-8'))
        self.assertEqual(len(manifest['items']), 600)
        self.assertEqual(manifest['coverage']['fetched_total'], 600)
        self.assertEqual(manifest['coverage']['runs'], 2)
        self.assertTrue(all(v['status'] == 'fetched' and v['sha256'] for v in manifest['items'].values()))

    def test_interrupt_resume_mid_page_fetches_each_item_once(self):
        items = {k: default_item() for k in 'abcd'}
        pages = {None: page('abc', 't2'), 't2': page('d')}
        with temporary() as tmp, RangeEnv(tmp, static_pages(pages), items) as env:
            first = env.run(max_items=1)
            second = env.run(max_items=5)
        self.assertEqual(first['stop_reason'], 'item_limit')
        self.assertEqual((first['batch']['scanned'], first['batch']['fetched']), (1, 1))
        self.assertEqual((second['batch']['scanned'], second['batch']['fetched']), (3, 3))
        self.assertTrue(second['exhausted'])
        self.assertEqual(sum('format=raw' in url for url, _ in env.calls), 4)
        manifest = env.manifest
        self.assertEqual(set(manifest['items']), {'a', 'b', 'c', 'd'})
        self.assertEqual([i['run'] for i in manifest['items'].values()], [1, 2, 2, 2])  # earlier batch retained
        self.assertEqual(len(manifest['runs']), 2)
        cp = json.loads(env.checkpoint_files[0].read_text(encoding='utf-8'))
        self.assertEqual(cp['status'], 'exhausted')

    def test_duplicate_items_across_pages_counted_once(self):
        items = {k: default_item() for k in 'abc'}
        pages = {None: page('ab', 't1'), 't1': page('bc')}
        with temporary() as tmp, RangeEnv(tmp, static_pages(pages), items) as env:
            result = env.run()
        self.assertTrue(result['exhausted'])
        self.assertEqual(result['batch']['fetched'], 3)
        self.assertEqual(result['batch']['already_processed'], 1)
        self.assertEqual(set(env.manifest['items']), {'a', 'b', 'c'})

    def test_repeated_page_token_stops_cleanly_without_livelock(self):
        items = {'a': default_item(), 'b': default_item()}
        pages = {None: page('a', 't1'), 't1': page('b', 't1')}
        with temporary() as tmp, RangeEnv(tmp, static_pages(pages), items) as env:
            result = env.run()
        self.assertEqual(result['stop_reason'], 'pagination_loop')
        self.assertFalse(result['exhausted'])
        self.assertEqual(result['status'], 'RANGE_PARTIAL')
        cp = json.loads(env.checkpoint_files[0].read_text(encoding='utf-8'))
        self.assertIsNone(cp['next_page_token'])

    def test_retry_on_429_and_5xx_then_success(self):
        items = {'a': default_item()}
        for code in ('provider_http_429', 'provider_http_503'):
            def listing(token, call):
                if call == 1:
                    raise m.MailError(code)
                return page('a')
            with temporary() as tmp, RangeEnv(tmp, listing, items) as env:
                result = env.run(retry_attempts=4, retry_base_sleep=0.01)
            self.assertEqual(result['batch']['fetched'], 1)
            self.assertTrue(result['exhausted'])
            self.assertGreaterEqual(len(env.sleeps), 1)
            self.assertEqual(env.listing_calls, 2)

    def test_retry_exhaustion_checkpoints_error_for_resume(self):
        def listing(token, call):
            raise m.MailError('provider_http_429')
        with temporary() as tmp, RangeEnv(tmp, listing, {}) as env:
            with self.assertRaisesRegex(m.MailError, 'provider_http_429'):
                env.run(retry_attempts=2, retry_base_sleep=0.0)
        cp = json.loads(env.checkpoint_files[0].read_text(encoding='utf-8'))
        self.assertEqual(cp['status'], 'stopped_error')
        self.assertEqual(cp['stop_reason'], 'error')
        manifest = env.manifest
        self.assertFalse(manifest['coverage']['exhausted'])

    def test_listing_permission_error_aborts_but_checkpoints(self):
        def listing(token, call):
            raise m.MailError('provider_http_403')
        with temporary() as tmp, RangeEnv(tmp, listing, {}) as env:
            with self.assertRaisesRegex(m.MailError, 'provider_http_403'):
                env.run()
        cp = json.loads(env.checkpoint_files[0].read_text(encoding='utf-8'))
        self.assertEqual(cp['status'], 'stopped_error')

    def test_item_permission_error_skipped_with_reason_and_blocks_exhausted(self):
        items = {'a': m.MailError('provider_http_403'), 'b': default_item()}
        listing = static_pages({None: page('ab')})
        with temporary() as tmp, RangeEnv(tmp, listing, items) as env:
            result = env.run()
        self.assertEqual(result['batch']['fetched'], 1)
        self.assertEqual(result['batch']['skipped'], {'provider_http_403': 1})
        self.assertFalse(result['exhausted'])
        self.assertEqual(result['stop_reason'], 'item_errors')
        entry = env.manifest['items']['a']
        self.assertEqual((entry['status'], entry['skip_reason']), ('skipped', 'provider_http_403'))

    def test_bad_checkpoint_version_and_key_mismatch_rejected(self):
        items = {k: default_item() for k in 'ab'}
        listing = static_pages({None: page('ab')})
        with temporary() as tmp, RangeEnv(tmp, listing, items) as env:
            env.run(max_items=1)
            path = env.checkpoint_files[0]
            original = json.loads(path.read_text(encoding='utf-8'))
            stale = dict(original, checkpoint_version=99)
            path.write_text(json.dumps(stale), encoding='utf-8')
            with self.assertRaisesRegex(m.MailError, 'checkpoint_version_unsupported'):
                env.run()
            wrong_alias = dict(original, alias='google-example-3')
            path.write_text(json.dumps(wrong_alias), encoding='utf-8')
            with self.assertRaisesRegex(m.MailError, 'checkpoint_mismatch'):
                env.run()
            wrong_out = dict(original, out_dir='C:/elsewhere')
            path.write_text(json.dumps(wrong_out), encoding='utf-8')
            with self.assertRaisesRegex(m.MailError, 'checkpoint_mismatch'):
                env.run()
            path.write_text(json.dumps(dict(original, mode='read')), encoding='utf-8')
            with self.assertRaisesRegex(m.MailError, 'checkpoint_corrupt'):
                env.run()
            path.write_text(json.dumps(original), encoding='utf-8')  # restore: resume works again
            done = env.run()
        self.assertEqual(done['batch']['fetched'], 1)
        self.assertTrue(done['exhausted'])

    def test_expired_cursor_restarts_enumeration_without_loss_or_double_count(self):
        items = {k: default_item() for k in 'abcd'}
        pages = {None: page('a', 't1'), 't1': page('bcd')}
        with temporary() as tmp, RangeEnv(tmp, static_pages(pages), items) as env:
            env.run(max_items=2)
            path = env.checkpoint_files[0]
            cp = json.loads(path.read_text(encoding='utf-8'))
            self.assertEqual((cp['page_token_in_progress'], cp['page_position']), ('t1', 1))
            path.write_text(json.dumps(dict(cp, page_token_in_progress='expired-token')), encoding='utf-8')

            def relisting(token, call):
                if token == 'expired-token':
                    raise m.MailError('provider_http_400')
                return pages[token]
            env.listing = relisting
            result = env.run()
        self.assertEqual(result['batch']['restarts'], 1)
        self.assertEqual(result['batch']['fetched'], 2)
        self.assertEqual(result['batch']['already_processed'], 2)
        self.assertTrue(result['exhausted'])
        self.assertEqual(set(env.manifest['items']), {'a', 'b', 'c', 'd'})

    def test_run_budgets_stop_honestly(self):
        items = {'a': default_item(), 'b': default_item()}
        listing = static_pages({None: page('ab')})
        # Call budget: listing consumes the only call.
        with temporary() as tmp, RangeEnv(tmp, listing, items) as env:
            result = env.run(max_calls=1)
        self.assertEqual((result['stop_reason'], result['exhausted']), ('call_budget', False))
        # Byte budget: the item response itself exceeds it.
        heavy = {'a': {'internalDate': utc_ms(2026, 9, 1, 12), 'raw': range_message(), 'meter': 5000}}
        with temporary() as tmp, RangeEnv(tmp, listing, heavy) as env:
            result = env.run(max_bytes=1000)
        self.assertEqual((result['stop_reason'], result['exhausted']), ('byte_budget', False))
        # Time budget: clock advances 1000 s per read.
        tick = iter(range(0, 100000, 1000))
        with temporary() as tmp, RangeEnv(tmp, listing, items) as env:
            result = env.run(max_seconds=1500, clock=lambda: next(tick))
        self.assertEqual((result['stop_reason'], result['exhausted']), ('time_budget', False))
        for path in [env.checkpoint_files[0]]:
            cp = json.loads(path.read_text(encoding='utf-8'))
            self.assertEqual(cp['status'], 'stopped_limits')

    def test_out_path_traversal_rejected_and_state_under_artifact_root(self):
        items = {'a': default_item()}
        listing = static_pages({None: page('a')})
        with temporary() as tmp, RangeEnv(tmp, listing, items) as env:
            outside = Path(tmp).parent / 'escape'
            with self.assertRaisesRegex(m.MailError, 'artifact_root_rejected'):
                m.range_run('google-example-1', date(2026, 9, 1), date(2026, 9, 2), outside)
            result = env.run()
        self.assertTrue(result['checkpoint'].startswith(str(Path(tmp) / 'range-checkpoints')))
        self.assertTrue(Path(result['checkpoint']).is_relative_to(Path(tmp) / 'range-checkpoints'))

    def test_cli_range_help_smoke(self):
        script = Path(__file__).parents[1] / 'scripts' / 'mail_direct.py'
        flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        proc = subprocess.run([sys.executable, '-B', str(script), 'range', '--help'],
                              capture_output=True, text=True, timeout=120, creationflags=flags)
        self.assertEqual(proc.returncode, 0)
        for flag in ('--alias', '--since', '--until', '--out', '--max-items', '--max-scan',
                     '--max-calls', '--max-bytes', '--max-seconds', '--retry-attempts'):
            self.assertIn(flag, proc.stdout)
        self.assertNotIn('--subject', proc.stdout)


if __name__ == '__main__':
    unittest.main()
