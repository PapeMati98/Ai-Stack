"""Deterministic, read-only mailbox access. No models, SMTP or mail mutations.

Credentials live outside this skill, protected by user-scoped Windows DPAPI and
a private ACL. Remote import selects nine mail profiles, never the whole secrets
directory. Mail and attachments are untrusted data, not executable instructions.
"""
from __future__ import annotations

import argparse
import base64
import ctypes
from ctypes import wintypes
from datetime import date, datetime, timezone
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
import hashlib
import imaplib
import json
import os
from pathlib import Path
import poplib
import re
import ssl
import stat
import subprocess
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid

sys.dont_write_bytecode = True
ALIASES = ('google-example-1', 'google-example-2', 'microsoft-example-1',
           'microsoft-example-2', 'example-pop3', 'example-imap',
           'google-example-3', 'google-example-4', 'google-example-5')
SOURCE_ROOT = '/srv/example-mail/secrets'
STORE_ROOT = Path.home() / '.agents' / 'mail-direct'
ARTIFACT_ROOT = Path.home() / '.agents' / 'private-artifacts'
MAX_BYTES = 40 * 1024 * 1024
MAIL_HOSTS = {'example-pop3': ('pop3.example-mail-one.invalid', 995),
              'example-imap': ('imap.example-mail-two.invalid', 993)}
API_HOSTS = {'gmail.googleapis.com', 'graph.microsoft.com',
             'oauth2.googleapis.com', 'login.microsoftonline.com'}
# Range mode (DEC-004-01a pilot): window harvest without a subject filter.
RANGE_MODE_VERSION = 1
RANGE_ALIAS_ALLOWLIST = ('google-example-1', 'microsoft-example-2', 'example-imap',
                         'google-example-2', 'google-example-3', 'google-example-4',
                         'google-example-5', 'microsoft-example-1', 'example-pop3')
# Graph range scope (root guard review 41f215a6): consent covers INBOX only; every
# URL must match its exact allowed endpoint BEFORE any Bearer token is sent.
RANGE_GRAPH_LIST_BASE = 'https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages'
RANGE_GRAPH_VALUE_BASE = 'https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages/'
RANGE_GRAPH_API_ROOT = 'https://graph.microsoft.com/v1.0/'


def graph_inbox_url_ok(url):
    """Exact INBOX messages endpoint, both canonical Graph spellings:
    me/mailFolders/inbox/messages and me/mailFolders('inbox')/messages.
    Anything else - other folders, hosts, schemes - is rejected."""
    if not url.startswith(RANGE_GRAPH_API_ROOT):
        return False
    path = url[len(RANGE_GRAPH_API_ROOT):].split('?', 1)[0]
    normalized = path.replace("mailFolders('inbox')", 'mailFolders/inbox')
    return normalized == 'me/mailFolders/inbox/messages'
RANGE_MANIFEST_NAME = 'range-manifest.json'
RANGE_ITEM_SKIP_CODES = frozenset({'response_too_large', 'message_too_large',
                                   'provider_http_403', 'provider_http_404'})
RANGE_DEFAULT_MAX_CALLS = 5200
RANGE_DEFAULT_MAX_BYTES = 256 * 1024 * 1024
RANGE_DEFAULT_MAX_SECONDS = 900


class MailError(Exception):
    """Only fixed error codes reach stdout; never provider response bodies."""


def hidden(args, **kwargs):
    return subprocess.run(args, capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW,
                          **kwargs)


def no_links(path: Path):
    for p in (path, *path.parents):
        try:
            metadata = p.lstat()  # stat()/exists() follow junctions and dangling links.
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(metadata.st_mode) or getattr(metadata, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise MailError('reparse_path_rejected')


def artifact_path(path: Path):
    normalized = Path(os.path.abspath(path))
    no_links(normalized)
    if not normalized.is_relative_to(ARTIFACT_ROOT):
        raise MailError('artifact_root_rejected')
    return normalized


def private_directory(path: Path, create=False):
    if os.name != 'nt':
        raise MailError('windows_required')
    path = path.absolute()
    if path.drive.startswith('\\'):
        raise MailError('network_storage_rejected')
    no_links(path)
    # Never use a repo, cloud-sync directory, or skill tree for real mail/secrets.
    if any('onedrive' in x.lower() or 'sharepoint' in x.lower() or x.lower() in {'vault', 'skills'} for x in path.parts):
        raise MailError('unsafe_storage_path')
    if any((p / '.git').exists() for p in (path, *path.parents)):
        raise MailError('repository_storage_rejected')
    if not path.exists():
        if not create:
            raise MailError('credentials_not_imported')
        path.mkdir(parents=True)
        sid = hidden(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
                      '[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value'],
                     text=True, timeout=20).stdout.strip()
        if not re.fullmatch(r'S-1-5-21-[0-9-]+', sid):
            raise MailError('owner_identity_unavailable')
        p = hidden(['icacls.exe', str(path), '/inheritance:r', '/grant:r',
                    f'*{sid}:(OI)(CI)F', '*S-1-5-18:(OI)(CI)F'], timeout=20)
        if p.returncode:
            raise MailError('private_acl_failed')
    # Validate actual ACL on every use; existing broad explicit grants fail closed.
    # WinPS 5.1 .NET ACL APIs avoid module autoload/execution-policy failures.
    # Any unexpected inspection error must reject access, never imply privacy.
    ps = """$ErrorActionPreference='Stop';
$p=[Environment]::GetEnvironmentVariable('MAIL_DIRECT_ACL_PATH');
$acl=if([System.IO.Directory]::Exists($p)){[System.IO.Directory]::GetAccessControl($p)}else{[System.IO.File]::GetAccessControl($p)};
$sid=[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value;
$bad=@($acl.Access | Where-Object { $_.AccessControlType -eq 'Allow' -and
 $_.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value -notin @($sid,'S-1-5-18') });
$owner=@($acl.Access | Where-Object { $_.AccessControlType -eq 'Allow' -and
 $_.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value -eq $sid -and
 ($_.FileSystemRights -band [System.Security.AccessControl.FileSystemRights]::FullControl) -eq [System.Security.AccessControl.FileSystemRights]::FullControl });
if($bad.Count -gt 0 -or $owner.Count -eq 0){exit 9}; Write-Output 'PRIVATE'"""
    env = dict(os.environ, MAIL_DIRECT_ACL_PATH=str(path))
    p = hidden(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', ps],
               env=env, text=True, timeout=20)
    if p.returncode or p.stdout.strip() != 'PRIVATE':
        raise MailError('nonprivate_acl_rejected')
    return path


class Blob(ctypes.Structure):
    _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_byte))]


def dpapi(data: bytes, decrypt=False) -> bytes:
    if os.name != 'nt':
        raise MailError('windows_required')
    buf = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_byte)))
    result = Blob()
    crypt = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    fn = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    fn.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                   ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    fn.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    if not fn(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result)):
        raise MailError('dpapi_failed')
    try:
        return ctypes.string_at(result.data, result.size)
    finally:
        ctypes.memset(result.data, 0, result.size)
        kernel.LocalFree(result.data)
        ctypes.memset(buf, 0, len(data))


def save_profile(alias, profile, root=STORE_ROOT):
    if alias not in ALIASES:
        raise MailError('unknown_alias')
    root = private_directory(root, create=True)
    target = root / (alias + '.dpapi')
    no_links(target)
    payload = dpapi(json.dumps(profile).encode('utf-8'))
    temp = root / (alias + '.' + uuid.uuid4().hex + '.encrypted')
    with temp.open('xb') as f:
        f.write(payload)
    os.replace(temp, target)


def load_profile(alias, root=STORE_ROOT):
    if alias not in ALIASES:
        raise MailError('unknown_alias')
    root = private_directory(root)
    path = root / (alias + '.dpapi')
    no_links(path)
    private_directory(path)  # Validate the actual file ACL, not just its parent.
    profile = json.loads(dpapi(path.read_bytes(), decrypt=True))
    validate_profile(alias, profile)
    return profile


def validate_profile(alias, profile):
    kind = profile.get('kind')
    expected = 'pop3' if alias == 'example-pop3' else 'imap' if alias == 'example-imap' else 'graph' if alias.startswith('microsoft-') else 'gmail'
    if kind != expected or not profile.get('email'):
        raise MailError('profile_identity_invalid')
    if kind in {'pop3', 'imap'}:
        if (profile.get('host'), profile.get('port')) != MAIL_HOSTS[alias] or profile.get('ssl') is not True:
            raise MailError('mail_tls_endpoint_rejected')
    else:
        if not profile.get('oauth', {}).get('refresh_token'):
            raise MailError('oauth_refresh_missing')
        if kind == 'graph' and not re.fullmatch(r'[0-9a-fA-F-]{36}', profile.get('tenant_id', '')):
            raise MailError('tenant_invalid')


def import_server():
    # This operation is separate from mail reads; it transfers only credentials
    # explicitly authorized by the owner. SSH verifies its existing host key.
    script = r'''
from pathlib import Path
import json,base64
root=Path("/srv/example-mail/secrets")
aliases=ALIASES_PLACEHOLDER
result={}
for alias in aliases:
    folder=root/alias
    env={k.strip():v.strip().strip('"').strip("'") for line in (folder/(alias+'.env')).read_text().splitlines() if '=' in line and not line.startswith('#') for k,v in [line.split('=',1)]}
    if alias in ('example-pop3','example-imap'):
        prefix='NORTHWIND_' if alias=='example-pop3' else 'EXAMPLE_AUDIT_'
        kind='pop3' if alias=='example-pop3' else 'imap'
        result[alias]={'kind':kind,'email':env[prefix+'MAIL_USERNAME'],'host':env[prefix+kind.upper()+'_HOST'],'port':int(env[prefix+kind.upper()+'_PORT']),'ssl':env[prefix+kind.upper()+'_SSL'].lower()=='true','password':base64.b64decode(env[prefix+'MAIL_PASSWORD_B64']).decode()}
    elif alias.startswith('microsoft-'):
        tok=json.loads((folder/'graph_token.json').read_text())
        result[alias]={'kind':'graph','email':env['MSGRAPH_EXPECTED_EMAIL'],'tenant_id':env['MSGRAPH_TENANT_ID'],'client_id':env['MSGRAPH_CLIENT_ID'],'oauth':tok}
    else:
        tok=json.loads((folder/('gmail_token.json' if alias=='google-example-1' else 'google_token.json')).read_text())
        expected=tok.get('account') or next((v for k,v in env.items() if k.endswith('EXPECTED_EMAIL')),'')
        result[alias]={'kind':'gmail','email':expected or 'VERIFY_WITH_PROFILE','oauth':tok}
print(json.dumps(result))
'''.replace('ALIASES_PLACEHOLDER', repr(ALIASES))
    p = hidden(['ssh', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
                '-o', 'ConnectTimeout=10', 'mail-source.example.invalid', 'python3 -'],
               input=script.encode(), timeout=40)
    if p.returncode:
        raise MailError('server_import_failed')
    profiles = json.loads(p.stdout)
    if set(profiles) != set(ALIASES):
        raise MailError('incomplete_server_profiles')
    for alias, profile in profiles.items():
        validate_profile(alias, profile)
    for alias, profile in profiles.items():
        save_profile(alias, profile)
    return {'status': 'IMPORTED', 'accounts': len(profiles), 'store': str(STORE_ROOT),
            'protection': 'DPAPI_CURRENT_USER_AND_PRIVATE_ACL', 'source_changed': False}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise MailError('http_redirect_rejected')


def http(url, *, token=None, form=None, meter=None):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != 'https' or parsed.hostname not in API_HOSTS or parsed.port not in (None, 443) or parsed.username or parsed.password:
        raise MailError('http_endpoint_rejected')
    if form is not None and not (parsed.hostname == 'oauth2.googleapis.com' and parsed.path == '/token' or parsed.hostname == 'login.microsoftonline.com' and parsed.path.endswith('/oauth2/v2.0/token')):
        raise MailError('http_write_rejected')
    data = urllib.parse.urlencode(form).encode() if form is not None else None
    headers = {'Authorization': 'Bearer ' + token} if token else {}
    req = urllib.request.Request(url, data=data, headers=headers, method='POST' if form is not None else 'GET')
    opener = urllib.request.build_opener(NoRedirect(), urllib.request.HTTPSHandler(context=ssl.create_default_context()))
    try:
        with opener.open(req, timeout=30) as response:
            payload = response.read(MAX_BYTES + 1)
        if meter is not None:
            meter(len(payload))
        if len(payload) > MAX_BYTES:
            raise MailError('response_too_large')
        return json.loads(payload)
    except urllib.error.HTTPError as exc:
        raise MailError('provider_http_' + str(exc.code)) from None


def oauth_token(alias, profile):
    tok = profile['oauth']
    if profile['kind'] == 'gmail':
        fresh = http('https://oauth2.googleapis.com/token', form={
            'grant_type': 'refresh_token', 'refresh_token': tok['refresh_token'],
            'client_id': tok['client_id'], 'client_secret': tok['client_secret']})
        tok['token'] = fresh['access_token']
        token = tok['token']
    else:
        fresh = http(f"https://login.microsoftonline.com/{profile['tenant_id']}/oauth2/v2.0/token", form={
            'grant_type': 'refresh_token', 'refresh_token': tok['refresh_token'],
            'client_id': profile['client_id']})
        tok.update(fresh)
        token = tok['access_token']
    # Persist refreshed tokens only locally, under the same DPAPI protection.
    save_profile(alias, profile)
    return token


def check_identity(alias, profile, token):
    if profile['kind'] == 'gmail':
        actual = http('https://gmail.googleapis.com/gmail/v1/users/me/profile', token=token)['emailAddress']
    else:
        account = http('https://graph.microsoft.com/v1.0/me?$select=mail,userPrincipalName', token=token)
        actual = account.get('mail') or account.get('userPrincipalName')
    expected = profile['email']
    if expected == 'VERIFY_WITH_PROFILE':
        profile['email'] = actual
        save_profile(alias, profile)
    elif actual.casefold() != expected.casefold():
        raise MailError('mailbox_identity_mismatch')
    return actual


def connection(alias, profile):
    validate_profile(alias, profile)
    context = ssl.create_default_context()
    if profile['kind'] == 'pop3':
        client = poplib.POP3_SSL(profile['host'], profile['port'], timeout=25, context=context)
        try:
            client.user(profile['email'])
            client.pass_(profile['password'])
        except Exception:
            client.close()
            raise
    else:
        client = imaplib.IMAP4_SSL(profile['host'], profile['port'], timeout=25, ssl_context=context)
        try:
            client.login(profile['email'], profile['password'])
            if client.select('INBOX', readonly=True)[0] != 'OK':
                raise MailError('imap_inbox_unavailable')
        except Exception:
            client.logout()
            raise
    return client


def doctor(alias):
    profile = load_profile(alias)
    if profile['kind'] in {'gmail', 'graph'}:
        token = oauth_token(alias, profile)
        check_identity(alias, profile, token)
    else:
        client = connection(alias, profile)
        try:
            if profile['kind'] == 'pop3':
                client.stat()
            else:
                client.noop()
        finally:
            client.quit() if profile['kind'] == 'pop3' else client.logout()
    return {'alias': alias, 'kind': profile['kind'], 'status': 'AUTH_READ_OK',
            'mail_sent': False, 'mail_flags_changed': False}


def normalize(text):
    return ' '.join(unicodedata.normalize('NFKC', text).casefold().split())


def matches(raw, subjects, start, end):
    msg = BytesParser(policy=policy.default).parsebytes(raw, headersonly=True)
    return matches_fields(str(msg.get('Subject', '')), str(msg.get('Date', '')), subjects, start, end)


def matches_fields(subject, date_header, subjects, start, end):
    if not any(normalize(s) in normalize(subject) for s in subjects):
        return False
    try:
        dt = parsedate_to_datetime(date_header)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return start <= dt.astimezone().date() <= end
    except (ValueError, TypeError, OverflowError):
        return False


def bounded(raw):
    if len(raw) > MAX_BYTES:
        raise MailError('message_too_large')
    return raw


def pop_messages(client, subjects, start, end, scan, limit):
    count = client.stat()[0]
    sizes = {int(line.split()[0]): int(line.split()[1]) for line in client.list()[1]}
    found, inspected = [], 0
    for number in range(count, max(0, count - scan), -1):
        if len(found) >= limit:
            break
        header = b'\r\n'.join(client.top(number, 0)[1]) + b'\r\n\r\n'
        inspected += 1
        if matches(header, subjects, start, end):
            if sizes.get(number, MAX_BYTES + 1) > MAX_BYTES:
                raise MailError('message_too_large')
            raw = bounded(b'\r\n'.join(client.retr(number)[1]))
            # Check retrieved content as well, rather than trusting a stale TOP.
            if matches(raw, subjects, start, end):
                found.append(raw)
    return found, {'headers_scanned': inspected, 'mailbox_total': count,
                   'scan_truncated': inspected < count, 'limit_reached': len(found) >= limit}


def imap_messages(client, subjects, start, end, scan, limit):
    from datetime import timedelta
    fmt = '%d-%b-%Y'
    status, data = client.uid('search', None, 'SINCE', (start - timedelta(days=1)).strftime(fmt), 'BEFORE', (end + timedelta(days=2)).strftime(fmt))
    if status != 'OK':
        raise MailError('imap_search_failed')
    ids = data[0].split()
    found, inspected = [], 0
    for uid in reversed(ids[-scan:]):
        if len(found) >= limit:
            break
        status, header = client.uid('fetch', uid, '(BODY.PEEK[HEADER] RFC822.SIZE)')
        if status != 'OK':
            raise MailError('imap_fetch_failed')
        inspected += 1
        tuples = [v for v in header if isinstance(v, tuple)]
        if not tuples:
            raise MailError('imap_header_missing')
        if matches(tuples[0][1], subjects, start, end):
            # Servers may place RFC822.SIZE after the header literal. Inspect
            # protocol metadata only, never the untrusted literal header body.
            metadata = b' '.join(v[0] if isinstance(v, tuple) else v for v in header if isinstance(v, (tuple, bytes)))
            size = re.search(rb'RFC822.SIZE (\d+)', metadata)
            if not size or int(size[1]) > MAX_BYTES:
                raise MailError('message_too_large')
            status, body = client.uid('fetch', uid, '(BODY.PEEK[])')
            if status != 'OK':
                raise MailError('imap_fetch_failed')
            raw = bounded(next(v[1] for v in body if isinstance(v, tuple)))
            if matches(raw, subjects, start, end):
                found.append(raw)
    return found, {'headers_scanned': inspected, 'date_candidates': len(ids),
                   'scan_truncated': len(ids) > scan, 'limit_reached': len(found) >= limit}


def api_messages(alias, profile, subjects, start, end, scan, limit):
    from datetime import timedelta
    token = oauth_token(alias, profile)
    check_identity(alias, profile, token)
    found, inspected, has_more = [], 0, False
    if profile['kind'] == 'gmail':
        # A date bound limits provider results; exact subject matching stays local.
        lower = int(datetime.combine(start - timedelta(days=1), datetime.min.time(), timezone.utc).timestamp())
        upper = int(datetime.combine(end + timedelta(days=2), datetime.min.time(), timezone.utc).timestamp())
        query = f'after:{lower} before:{upper}'
        endpoint = 'https://gmail.googleapis.com/gmail/v1/users/me/messages?'
        params = {'q': query, 'maxResults': min(scan, 500)}
        while inspected < scan and len(found) < limit:
            page = http(endpoint + urllib.parse.urlencode(params), token=token)
            items = page.get('messages', [])
            page_start = inspected
            for item in items:
                if inspected >= scan or len(found) >= limit:
                    break
                message_id = item['id']
                if not re.fullmatch(r'[0-9a-zA-Z_-]+', message_id):
                    raise MailError('provider_id_invalid')
                base = 'https://gmail.googleapis.com/gmail/v1/users/me/messages/' + message_id
                meta = http(base + '?format=metadata&metadataHeaders=Subject&metadataHeaders=Date', token=token)
                headers = {h['name'].casefold(): h['value'] for h in meta.get('payload', {}).get('headers', [])}
                inspected += 1
                if matches_fields(headers.get('subject', ''), headers.get('date', ''), subjects, start, end):
                    data = http(base + '?format=raw', token=token)['raw']
                    raw = bounded(base64.urlsafe_b64decode(data + '=' * (-len(data) % 4)))
                    if matches(raw, subjects, start, end):
                        found.append(raw)
            next_token = page.get('nextPageToken')
            has_more = bool(next_token) or inspected - page_start < len(items)
            if not next_token:
                break
            params['pageToken'] = next_token
    else:
        # Graph MIME $value is a GET; no isRead/flag PATCH or folder mutation.
        params = {'$filter': f"receivedDateTime ge {(start-timedelta(days=1)).isoformat()}T00:00:00Z and receivedDateTime lt {(end+timedelta(days=2)).isoformat()}T00:00:00Z",
                  '$select': 'id,subject,receivedDateTime', '$top': min(scan, 500)}
        url = 'https://graph.microsoft.com/v1.0/me/messages?' + urllib.parse.urlencode(params)
        while url and inspected < scan and len(found) < limit:
            page = http(url, token=token)
            items = page.get('value', [])
            page_start = inspected
            for item in items:
                if inspected >= scan or len(found) >= limit:
                    break
                inspected += 1
                if any(normalize(s) in normalize(item.get('subject') or '') for s in subjects):
                    message_id = urllib.parse.quote(item['id'], safe='')
                    raw = graph_mime(message_id, token)
                    if matches(raw, subjects, start, end):
                        found.append(raw)
            url = page.get('@odata.nextLink')
            if url and not url.startswith('https://graph.microsoft.com/v1.0/me/messages?'):
                raise MailError('provider_pagination_rejected')
            has_more = bool(url) or inspected - page_start < len(items)
    return found, {'headers_scanned': inspected, 'scan_truncated': has_more,
                   'limit_reached': len(found) >= limit}


def graph_mime(message_id, token):
    req = urllib.request.Request('https://graph.microsoft.com/v1.0/me/messages/' + message_id + '/$value',
                                 headers={'Authorization': 'Bearer ' + token}, method='GET')
    opener = urllib.request.build_opener(NoRedirect(), urllib.request.HTTPSHandler(context=ssl.create_default_context()))
    try:
        with opener.open(req, timeout=30) as response:
            return bounded(response.read(MAX_BYTES + 1))
    except urllib.error.HTTPError as exc:
        raise MailError('provider_http_' + str(exc.code)) from None


def persist_message(root, raw):
    msg = BytesParser(policy=policy.default).parsebytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    folder = root / digest[:20]
    no_links(folder)
    folder = private_directory(folder, create=True)
    target = folder / 'message.eml'
    no_links(target)
    if target.exists():
        private_directory(target)
    if not target.exists():
        target.write_bytes(raw)
    elif target.read_bytes() != raw:
        raise MailError('artifact_collision')
    attachments = []
    for part in msg.walk():
        name = part.get_filename()
        ext = Path(name or '').suffix.lower()
        if ext not in {'.xlsx', '.xls', '.xlsm', '.xlsb', '.csv'}:
            continue
        data = part.get_payload(decode=True)
        if not isinstance(data, bytes):
            continue
        sha = hashlib.sha256(data).hexdigest()
        attachment = folder / (sha[:20] + ext)
        no_links(attachment)
        if attachment.exists():
            private_directory(attachment)
        if not attachment.exists():
            attachment.write_bytes(data)
        elif attachment.read_bytes() != data:
            raise MailError('artifact_collision')
        attachments.append({'original_name': name, 'path': str(attachment), 'sha256': sha, 'bytes': len(data)})
    return {'subject': str(msg.get('Subject', '')), 'date': str(msg.get('Date', '')),
            'from': str(msg.get('From', '')), 'message_id': str(msg.get('Message-ID', '')),
            'eml': str(target), 'sha256': digest, 'excel': attachments}


def save_messages(messages, out):
    root = private_directory(artifact_path(out), create=True)
    entries = [persist_message(root, raw) for raw in messages]
    manifest = root / 'messages.json'
    no_links(manifest)
    if manifest.exists():
        private_directory(manifest)
    manifest.write_text(json.dumps({'messages': entries}, ensure_ascii=False, indent=2), encoding='utf-8')
    return entries, manifest


def read_mail(alias, subjects, start, end, out, scan=1000, limit=40):
    if not 1 <= limit <= 500 or not 1 <= scan <= 5000 or start > end or not subjects or any(not s.strip() or len(s) > 300 for s in subjects):
        raise MailError('read_scope_invalid')
    profile = load_profile(alias)
    # Establish private destination before any content leaves the mailbox.
    out = private_directory(artifact_path(out), create=True)
    if profile['kind'] in {'gmail', 'graph'}:
        messages, stats = api_messages(alias, profile, subjects, start, end, scan, limit)
    else:
        client = connection(alias, profile)
        try:
            if profile['kind'] == 'pop3':
                messages, stats = pop_messages(client, subjects, start, end, scan, limit)
            else:
                messages, stats = imap_messages(client, subjects, start, end, scan, limit)
        finally:
            client.quit() if profile['kind'] == 'pop3' else client.logout()
    entries, manifest = save_messages(messages, out)
    return {'status': 'READ_OK', 'alias': alias, 'messages': len(entries),
            'excel_attachments': sum(len(m['excel']) for m in entries), 'manifest': str(manifest),
            'coverage': stats, 'mail_sent': False, 'mail_flags_changed': False}


def draft_mail(alias: str, to: str, subject: str, body: str, out: Path | str, cc: str | None = None) -> dict:
    if alias not in ALIASES:
        raise MailError('unknown_alias')
    if not to or not to.strip():
        raise MailError('recipient_missing')
    if not subject or not subject.strip():
        raise MailError('subject_missing')

    out_dir = private_directory(artifact_path(Path(out)), create=True)
    body_normalized = body.replace('\r\n', '\n')
    draft_id = uuid.uuid4().hex
    subject_sha256 = hashlib.sha256(subject.encode('utf-8')).hexdigest()
    body_sha256 = hashlib.sha256(body_normalized.encode('utf-8')).hexdigest()
    approval_digest = hashlib.sha256(f'{draft_id}:{alias}:{to}:{body_sha256}'.encode('utf-8')).hexdigest()

    sender = alias
    try:
        prof = load_profile(alias)
        sender = prof.get('email', alias)
    except Exception:
        pass

    msg = EmailMessage()
    msg['Subject'] = subject
    msg['From'] = sender
    msg['To'] = to
    if cc:
        msg['Cc'] = cc
    msg['Date'] = datetime.now(timezone.utc).strftime('%a, %d %b %Y %H:%M:%S +0000')
    msg['Message-ID'] = f'<{draft_id}@{alias}.draft>'
    msg.set_content(body_normalized)
    eml_bytes = msg.as_bytes()

    eml_path = out_dir / 'draft.eml'
    no_links(eml_path)
    eml_path.write_bytes(eml_bytes)
    private_directory(eml_path)

    manifest_path = out_dir / 'draft-manifest.json'
    no_links(manifest_path)
    manifest_data = {
        'draft_id': draft_id,
        'alias': alias,
        'to': to,
        'cc': cc,
        'subject_sha256': subject_sha256,
        'body_sha256': body_sha256,
        'approval_digest': approval_digest,
        'status': 'WAITING_OWNER_APPROVAL',
        'created_at': datetime.now(timezone.utc).isoformat(),
        'eml_file': 'draft.eml',
        'eml_path': str(eml_path),
    }
    manifest_path.write_text(json.dumps(manifest_data, indent=2, ensure_ascii=False), encoding='utf-8')
    private_directory(manifest_path)

    return {
        'status': 'DRAFT_CREATED',
        'draft_id': draft_id,
        'manifest': str(manifest_path),
        'approval_digest': approval_digest,
        'mail_sent': False,
    }


def send_approved_mail(draft_manifest: Path | str, approval_token: str, approved_by_owner: bool) -> dict:
    if os.environ.get('MAIL_DIRECT_ALLOW_SEND') != '1' or not approved_by_owner:
        raise MailError('owner_approval_required')

    manifest_path = artifact_path(Path(draft_manifest))
    if not manifest_path.exists():
        raise MailError('manifest_not_found')

    try:
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    except Exception:
        raise MailError('manifest_corrupt')

    if not isinstance(manifest, dict):
        raise MailError('manifest_corrupt')

    eml_file = manifest.get('eml_file', 'draft.eml')
    eml_path = manifest_path.parent / eml_file
    eml_path = artifact_path(eml_path)
    if not eml_path.exists():
        raise MailError('draft_eml_not_found')

    raw_eml = eml_path.read_bytes()
    try:
        parsed_msg = BytesParser(policy=policy.default).parsebytes(raw_eml)
        recomputed_body = parsed_msg.get_content().replace('\r\n', '\n')
    except Exception:
        raise MailError('draft_eml_corrupt')

    expected_body_sha = manifest.get('body_sha256')
    recomputed_body_sha256 = hashlib.sha256(recomputed_body.encode('utf-8')).hexdigest()
    if recomputed_body_sha256 != expected_body_sha and recomputed_body.endswith('\n'):
        stripped_candidate = recomputed_body[:-1]
        if hashlib.sha256(stripped_candidate.encode('utf-8')).hexdigest() == expected_body_sha:
            recomputed_body_sha256 = expected_body_sha

    draft_id = manifest.get('draft_id', '')
    alias = manifest.get('alias', '')
    to = manifest.get('to', '')
    recomputed_digest = hashlib.sha256(f'{draft_id}:{alias}:{to}:{recomputed_body_sha256}'.encode('utf-8')).hexdigest()

    if not approval_token or approval_token != recomputed_digest or approval_token != manifest.get('approval_digest'):
        raise MailError('approval_token_mismatch')

    if manifest.get('status') == 'SENT':
        raise MailError('draft_already_sent')

    profile = load_profile(alias)
    send_transport(alias, profile, raw_eml, manifest)

    manifest['status'] = 'SENT'
    manifest['sent_at'] = datetime.now(timezone.utc).isoformat()
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding='utf-8')
    private_directory(manifest_path)

    return {
        'status': 'SENT',
        'draft_id': draft_id,
        'alias': alias,
        'to': to,
        'mail_sent': True,
        'manifest': str(manifest_path),
    }


def send_transport(alias: str, profile: dict, raw_eml: bytes, manifest: dict) -> dict:
    kind = profile.get('kind')
    if kind == 'gmail':
        token = oauth_token(alias, profile)
        check_identity(alias, profile, token)
        url = 'https://gmail.googleapis.com/gmail/v1/users/me/messages/send'
        b64_raw = base64.urlsafe_b64encode(raw_eml).decode().rstrip('=')
        payload = json.dumps({'raw': b64_raw}).encode('utf-8')
        req = urllib.request.Request(
            url, data=payload,
            headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'},
            method='POST',
        )
        opener = urllib.request.build_opener(NoRedirect(), urllib.request.HTTPSHandler(context=ssl.create_default_context()))
        try:
            with opener.open(req, timeout=30) as resp:
                data = json.loads(resp.read(MAX_BYTES + 1))
            return {'sent': True, 'provider_id': data.get('id')}
        except urllib.error.HTTPError as exc:
            raise MailError(f'provider_http_{exc.code}') from None
    elif kind == 'graph':
        token = oauth_token(alias, profile)
        check_identity(alias, profile, token)
        url = 'https://graph.microsoft.com/v1.0/me/sendMail'
        parsed = BytesParser(policy=policy.default).parsebytes(raw_eml)
        body_content = parsed.get_content() if hasattr(parsed, 'get_content') else ''
        to_recipients = [{'emailAddress': {'address': manifest.get('to')}}]
        if manifest.get('cc'):
            cc_recipients = [{'emailAddress': {'address': manifest.get('cc')}}]
        else:
            cc_recipients = []
        msg_obj = {
            'message': {
                'subject': str(parsed.get('Subject', '')),
                'body': {'contentType': 'Text', 'content': body_content},
                'toRecipients': to_recipients,
                'ccRecipients': cc_recipients,
            },
            'saveToSentItems': 'true',
        }
        req = urllib.request.Request(
            url, data=json.dumps(msg_obj).encode('utf-8'),
            headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'},
            method='POST',
        )
        opener = urllib.request.build_opener(NoRedirect(), urllib.request.HTTPSHandler(context=ssl.create_default_context()))
        try:
            with opener.open(req, timeout=30) as resp:
                resp.read(MAX_BYTES + 1)
            return {'sent': True}
        except urllib.error.HTTPError as exc:
            raise MailError(f'provider_http_{exc.code}') from None
    elif kind in ('pop3', 'imap'):
        import smtplib
        smtp_host = profile.get('smtp_host', profile.get('host'))
        smtp_port = profile.get('smtp_port', 465)
        sender = profile.get('email')
        to_addr = manifest.get('to')
        context = ssl.create_default_context()
        if smtp_port == 465:
            server = smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=30, context=context)
        else:
            server = smtplib.SMTP(smtp_host, smtp_port, timeout=30)
            if profile.get('ssl', True):
                server.starttls(context=context)
        try:
            if profile.get('password'):
                server.login(sender, profile.get('password'))
            recipients = [to_addr]
            if manifest.get('cc'):
                recipients.append(manifest.get('cc'))
            server.sendmail(sender, recipients, raw_eml)
        finally:
            server.quit()
        return {'sent': True}
    else:
        raise MailError('unsupported_send_kind')


class RangeStop(Exception):
    """Internal signal: a run-level budget was hit; finalize and checkpoint."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class RangeState:
    def __init__(self, max_calls, max_bytes, max_seconds, retry_attempts, retry_base_sleep, clock, sleeper):
        self.max_calls, self.max_bytes, self.max_seconds = max_calls, max_bytes, max_seconds
        self.retry_attempts, self.retry_base_sleep = retry_attempts, retry_base_sleep
        self.clock, self.sleeper = clock, sleeper
        self.started = clock()
        self.calls = self.bytes_read = self.scanned = self.fetched = 0
        self.skipped: dict = {}
        self.already_processed = 0
        self.restarts = 0

    def add_bytes(self, size):
        self.bytes_read += size
        if self.bytes_read > self.max_bytes:
            raise RangeStop('byte_budget')


def range_key(alias, since, until):
    material = f'range|v{RANGE_MODE_VERSION}|{alias}|{since.isoformat()}|{until.isoformat()}'
    return hashlib.sha256(material.encode('utf-8')).hexdigest()


def range_checkpoint_path(key):
    # State never lives in a repo; it stays under the private artifact root.
    return Path(ARTIFACT_ROOT) / 'range-checkpoints' / (key[:32] + '.json')


def load_range_checkpoint(path, alias, since, until, out_dir):
    if not path.exists():
        return None
    no_links(path)
    private_directory(path.parent)
    try:
        cp = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        raise MailError('checkpoint_corrupt') from None
    if not isinstance(cp, dict) or cp.get('mode') != 'range':
        raise MailError('checkpoint_corrupt') from None
    if cp.get('checkpoint_version') != RANGE_MODE_VERSION:
        raise MailError('checkpoint_version_unsupported') from None
    expected = {'alias': alias, 'since': since.isoformat(), 'until': until.isoformat(),
                'key': range_key(alias, since, until), 'out_dir': str(out_dir)}
    if any(cp.get(k) != v for k, v in expected.items()):
        raise MailError('checkpoint_mismatch') from None
    return cp


def save_range_checkpoint(path, checkpoint):
    private_directory(path.parent, create=True)
    no_links(path)
    temp = path.parent / (path.name + '.' + uuid.uuid4().hex + '.tmp')
    temp.write_text(json.dumps(checkpoint, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temp, path)


def load_range_manifest(out_dir, alias, since, until):
    path = out_dir / RANGE_MANIFEST_NAME
    if not path.exists():
        return {'mode': 'range', 'mode_version': RANGE_MODE_VERSION, 'alias': alias,
                'since': since.isoformat(), 'until': until.isoformat(),
                'window': {'since': since.isoformat(), 'until': until.isoformat(),
                           'tz': 'UTC', 'inclusive': True},
                'items': {}, 'runs': [], 'runs_completed': 0,
                'totals': {'scanned_total': 0, 'already_processed_total': 0}}
    no_links(path)
    try:
        manifest = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        raise MailError('manifest_corrupt') from None
    if (not isinstance(manifest, dict) or manifest.get('mode') != 'range'
            or manifest.get('alias') != alias or manifest.get('since') != since.isoformat()
            or manifest.get('until') != until.isoformat() or not isinstance(manifest.get('items'), dict)):
        raise MailError('manifest_mismatch') from None
    manifest.setdefault('runs', [])
    manifest.setdefault('runs_completed', 0)
    manifest.setdefault('totals', {'scanned_total': 0, 'already_processed_total': 0})
    return manifest


def range_write_manifest(out_dir, manifest):
    path = out_dir / RANGE_MANIFEST_NAME
    no_links(path)
    temp = out_dir / (RANGE_MANIFEST_NAME + '.' + uuid.uuid4().hex + '.tmp')
    temp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temp, path)


def range_flush(out_dir, manifest, fetched_batch, run_index):
    # Merge by provider message id: idempotent resume, no duplicated entries.
    for mid, tid, internal_iso, raw in fetched_batch:
        entry = persist_message(out_dir, raw)
        existing = manifest['items'].get(mid)
        if existing and existing.get('status') == 'fetched' and existing.get('sha256') not in (None, entry['sha256']):
            raise MailError('manifest_id_conflict') from None
        manifest['items'][mid] = {'provider_message_id': mid, 'thread_id': tid,
                                  'internal_date': internal_iso, 'status': 'fetched',
                                  'run': run_index, **entry}
    range_write_manifest(out_dir, manifest)


def range_provider_call(state, url, token):
    attempt = 0
    while True:
        if state.calls >= state.max_calls:
            raise RangeStop('call_budget')
        if state.clock() - state.started >= state.max_seconds:
            raise RangeStop('time_budget')
        state.calls += 1
        try:
            return http(url, token=token, meter=state.add_bytes)
        except MailError as exc:
            code = str(exc)
            attempt += 1
            if (code == 'provider_http_429' or code == 'provider_http_403'
                    or code.startswith('provider_http_5')) and attempt < state.retry_attempts:
                state.sleeper(state.retry_base_sleep * (2 ** min(attempt - 1, 5)))
                continue
            raise


def range_run(alias, since, until, out, *, max_items=200, max_scan=1000,
              max_calls=RANGE_DEFAULT_MAX_CALLS, max_bytes=RANGE_DEFAULT_MAX_BYTES,
              max_seconds=RANGE_DEFAULT_MAX_SECONDS, retry_attempts=4, retry_base_sleep=0.5,
              clock=None, sleeper=None, imap_factory=None):
    """Resumable window harvest: Gmail only, no subject filter, GET only.

    Dates are UTC calendar dates, both ends inclusive. exhausted=true only
    means the window query ran out of pages; it is never a mailbox inventory.
    """
    from datetime import timedelta
    if alias not in RANGE_ALIAS_ALLOWLIST:
        raise MailError('range_alias_unsupported') from None
    if (not 1 <= max_items <= 500 or not 1 <= max_scan <= 5000 or since > until
            or max_calls < 1 or max_bytes < 1 or max_seconds < 1 or retry_attempts < 1):
        raise MailError('range_scope_invalid') from None
    profile = load_profile(alias)
    out_dir = private_directory(artifact_path(out), create=True)
    cp_path = range_checkpoint_path(range_key(alias, since, until))
    if profile.get('kind') == 'graph':
        return range_run_graph(alias, profile, since, until, out_dir, cp_path,
                               max_items=max_items, max_scan=max_scan, max_calls=max_calls,
                               max_bytes=max_bytes, max_seconds=max_seconds,
                               retry_attempts=retry_attempts, retry_base_sleep=retry_base_sleep,
                               clock=clock, sleeper=sleeper)
    if profile.get('kind') == 'imap':
        return range_run_imap(alias, profile, since, until, out_dir, cp_path,
                              max_items=max_items, max_scan=max_scan, max_calls=max_calls,
                              max_bytes=max_bytes, max_seconds=max_seconds,
                              retry_attempts=retry_attempts, retry_base_sleep=retry_base_sleep,
                              clock=clock, sleeper=sleeper, imap_factory=imap_factory)
    if profile.get('kind') == 'pop3':
        return range_run_pop3(alias, profile, since, until, out_dir, cp_path,
                              max_items=max_items, max_scan=max_scan, max_calls=max_calls,
                              max_bytes=max_bytes, max_seconds=max_seconds,
                              retry_attempts=retry_attempts, retry_base_sleep=retry_base_sleep,
                              clock=clock, sleeper=sleeper)
    if profile.get('kind') != 'gmail':
        raise MailError('range_alias_unsupported') from None
    checkpoint = load_range_checkpoint(cp_path, alias, since, until, out_dir)
    manifest = load_range_manifest(out_dir, alias, since, until)
    run_index = manifest['runs_completed'] + 1
    state = RangeState(max_calls, max_bytes, max_seconds, retry_attempts, retry_base_sleep,
                       clock or time.monotonic, sleeper or time.sleep)
    token = oauth_token(alias, profile)
    check_identity(alias, profile, token)
    # Transient skips (provider_http_403/5xx throttling) must be retried on a
    # later run; only fetched items and terminal skips count as known.
    known = {mid for mid, item in manifest['items'].items()
             if isinstance(item, dict)
             and (item.get('status') == 'fetched'
                  or item.get('skip_reason') in ('date_outside_window',
                                                 'message_too_large',
                                                 'provider_http_404'))}
    lower = int(datetime.combine(since, datetime.min.time(), timezone.utc).timestamp())
    upper = int(datetime.combine(until + timedelta(days=1), datetime.min.time(), timezone.utc).timestamp())
    endpoint = 'https://gmail.googleapis.com/gmail/v1/users/me/messages'
    next_token = checkpoint.get('next_page_token') if checkpoint else None
    progress_token = checkpoint.get('page_token_in_progress') if checkpoint else None
    position = int(checkpoint.get('page_position') or 0) if checkpoint else 0
    page_ids = list(checkpoint.get('page_ids') or []) if checkpoint else []
    prior_restarts = int(checkpoint.get('restarts') or 0) if checkpoint else 0
    fetched_batch: list = []
    stop_reason, error_code, exhausted = None, None, False
    cur_items, cur_token, cur_position = [], None, 0
    try:
        while True:
            params = {'q': f'after:{lower} before:{upper}',
                      'maxResults': min(500, max(1, max_scan - state.scanned))}
            use_token = progress_token if progress_token is not None else next_token
            if use_token:
                params['pageToken'] = use_token
            try:
                page = range_provider_call(state, endpoint + '?' + urllib.parse.urlencode(params), token)
            except MailError as exc:
                code = str(exc)
                if use_token and code in {'provider_http_400', 'provider_http_410'} and not state.restarts:
                    # Expired cursor: restart enumeration; manifest merge keeps items unique.
                    state.restarts += 1
                    next_token = progress_token = None
                    position, page_ids = 0, []
                    continue
                stop_reason, error_code = 'error', code
                break
            items = page.get('messages') or []
            new_token = page.get('nextPageToken')
            if use_token is not None and new_token == use_token:
                stop_reason, error_code = 'pagination_loop', 'provider_page_token_repeated'
                break
            prefix = set(page_ids[:position])
            cur_items, cur_token, cur_position = items, use_token, 0
            interrupted = False
            for index, entry in enumerate(items):
                cur_position = index
                mid, tid = entry.get('id') or '', entry.get('threadId') or ''
                if not re.fullmatch(r'[0-9a-zA-Z_-]+', mid) or not re.fullmatch(r'[0-9a-zA-Z_-]+', tid):
                    stop_reason, error_code, interrupted = 'error', 'provider_id_invalid', True
                    break
                if index < position and mid in prefix:
                    continue  # Processed before the interruption; not re-counted.
                if mid in known:
                    state.already_processed += 1
                    continue
                if state.scanned >= max_scan:
                    stop_reason, interrupted = 'scan_limit', True
                    break
                if state.fetched >= max_items:
                    stop_reason, interrupted = 'item_limit', True
                    break
                state.scanned += 1
                internal_iso, skip = None, None
                try:
                    data = range_provider_call(state, f'{endpoint}/{mid}?format=raw', token)
                except MailError as exc:
                    code = str(exc)
                    if code in RANGE_ITEM_SKIP_CODES:
                        skip = code
                    else:
                        stop_reason, error_code, interrupted = 'error', code, True
                        break
                else:
                    if data.get('id') not in (None, mid):
                        stop_reason, error_code, interrupted = 'error', 'provider_id_mismatch', True
                        break
                    in_window = False
                    internal_ms = data.get('internalDate')
                    if isinstance(internal_ms, str) and internal_ms.isdigit():
                        internal_ms = int(internal_ms)
                    if isinstance(internal_ms, int):
                        moment = datetime.fromtimestamp(internal_ms / 1000, tz=timezone.utc)
                        internal_iso = moment.isoformat()
                        in_window = since <= moment.date() <= until
                    if not in_window:
                        skip = 'date_outside_window'
                    else:
                        encoded = data.get('raw') or ''
                        try:
                            raw = bounded(base64.urlsafe_b64decode(encoded + '=' * (-len(encoded) % 4)))
                        except MailError as exc:
                            if str(exc) == 'message_too_large':
                                skip = 'message_too_large'
                            else:
                                raise
                        else:
                            fetched_batch.append((mid, tid, internal_iso, raw))
                            state.fetched += 1
                            known.add(mid)
                if skip is not None:
                    manifest['items'][mid] = {'provider_message_id': mid, 'thread_id': tid,
                                              'internal_date': internal_iso, 'status': 'skipped',
                                              'skip_reason': skip, 'run': run_index}
                    known.add(mid)
                    state.skipped[skip] = state.skipped.get(skip, 0) + 1
            if interrupted:
                break
            range_flush(out_dir, manifest, fetched_batch, run_index)
            fetched_batch = []
            position, page_ids, progress_token = 0, [], None
            cur_items, cur_token, cur_position = [], None, 0
            next_token = new_token
            if not new_token:
                exhausted, stop_reason = True, 'complete'
                break
            if state.scanned >= max_scan:
                stop_reason = 'scan_limit'
                break
            if state.fetched >= max_items:
                stop_reason = 'item_limit'
                break
    except RangeStop as exc:
        stop_reason = exc.reason
    # Persist whatever was fetched before the stop; merge without duplicates.
    range_flush(out_dir, manifest, fetched_batch, run_index)
    if stop_reason == 'pagination_loop':
        next_token = None  # Avoid a livelock on the repeated token; restart clean.
    if not exhausted:
        page_ids = [i.get('id', '') for i in cur_items]
        progress_token = cur_token
        position = cur_position
    provider_error_skips = any(
        isinstance(item, dict) and str(item.get('skip_reason', '')).startswith('provider_http_')
        for item in manifest['items'].values())
    if exhausted and provider_error_skips:
        exhausted, stop_reason = False, 'item_errors'
    run_summary = {'run': run_index, 'stop_reason': stop_reason, 'exhausted': exhausted,
                   'error_code': error_code, 'scanned': state.scanned, 'fetched': state.fetched,
                   'skipped': state.skipped, 'already_processed': state.already_processed,
                   'provider_calls': state.calls, 'bytes_read': state.bytes_read,
                   'restarts': state.restarts}
    manifest['runs'].append(run_summary)
    manifest['runs_completed'] = run_index
    manifest['totals']['scanned_total'] += state.scanned
    manifest['totals']['already_processed_total'] += state.already_processed
    skipped_total: dict = {}
    for item in manifest['items'].values():
        if item.get('status') == 'skipped':
            reason = item.get('skip_reason') or 'unknown'
            skipped_total[reason] = skipped_total.get(reason, 0) + 1
    manifest['coverage'] = {'window': {'since': since.isoformat(), 'until': until.isoformat(),
                                       'tz': 'UTC', 'inclusive': True},
                           'fetched_total': sum(1 for i in manifest['items'].values() if i.get('status') == 'fetched'),
                           'skipped_total': skipped_total,
                           **manifest['totals'], 'runs': run_index,
                           'exhausted': exhausted, 'stop_reason': stop_reason,
                           'note': 'exhausted=true means the window query ran out of pages; not a full mailbox inventory'}
    range_write_manifest(out_dir, manifest)
    status = 'exhausted' if exhausted else ('stopped_error' if stop_reason == 'error' else 'stopped_limits')
    save_range_checkpoint(cp_path, {'checkpoint_version': RANGE_MODE_VERSION, 'mode': 'range',
                                    'mode_version': RANGE_MODE_VERSION, 'alias': alias,
                                    'since': since.isoformat(), 'until': until.isoformat(),
                                    'key': range_key(alias, since, until), 'out_dir': str(out_dir),
                                    'next_page_token': next_token,
                                    'page_token_in_progress': None if exhausted else progress_token,
                                    'page_position': 0 if exhausted else cur_position,
                                    'page_ids': [] if exhausted else page_ids,
                                    'restarts': prior_restarts + state.restarts,
                                    'runs_completed': run_index, 'status': status,
                                    'stop_reason': stop_reason,
                                    'updated_at': datetime.now(timezone.utc).isoformat()})
    if stop_reason == 'error':
        raise MailError(error_code or 'range_run_failed') from None
    return {'status': 'RANGE_OK' if exhausted else 'RANGE_PARTIAL', 'alias': alias,
            'since': since.isoformat(), 'until': until.isoformat(), 'tz': 'UTC', 'inclusive': True,
            'batch': {k: run_summary[k] for k in ('fetched', 'scanned', 'skipped', 'already_processed',
                                                  'provider_calls', 'bytes_read', 'restarts')},
            'coverage': manifest['coverage'], 'exhausted': exhausted, 'stop_reason': stop_reason,
            'manifest': str(out_dir / RANGE_MANIFEST_NAME), 'checkpoint': str(cp_path),
            'mail_sent': False, 'mail_flags_changed': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('import-server')
    sub.add_parser('accounts')
    doc = sub.add_parser('doctor')
    doc.add_argument('--alias', choices=(*ALIASES, 'all'), required=True)
    rd = sub.add_parser('read')
    rd.add_argument('--alias', choices=ALIASES, required=True)
    rd.add_argument('--subject', action='append', required=True)
    rd.add_argument('--since', type=date.fromisoformat, required=True)
    rd.add_argument('--until', type=date.fromisoformat, required=True)
    rd.add_argument('--out', type=Path, required=True)
    rd.add_argument('--max-items', type=int, default=40)
    rd.add_argument('--max-scan', type=int, default=1000)
    # Range mode: resumable window harvest, Gmail pilot, no subject filter.
    # No `choices` on purpose: non-pilot aliases must fail with a clear code.
    rg = sub.add_parser('range')
    rg.add_argument('--alias', required=True)
    rg.add_argument('--since', type=date.fromisoformat, required=True)
    rg.add_argument('--until', type=date.fromisoformat, required=True)
    rg.add_argument('--out', type=Path, required=True)
    rg.add_argument('--max-items', type=int, default=200)
    rg.add_argument('--max-scan', type=int, default=1000)
    rg.add_argument('--max-calls', type=int, default=RANGE_DEFAULT_MAX_CALLS)
    rg.add_argument('--max-bytes', type=int, default=RANGE_DEFAULT_MAX_BYTES)
    rg.add_argument('--max-seconds', type=int, default=RANGE_DEFAULT_MAX_SECONDS)
    rg.add_argument('--retry-attempts', type=int, default=4)
    # Draft mode: prepare offline draft and approval digest under ARTIFACT_ROOT
    dr = sub.add_parser('draft')
    dr.add_argument('--alias', choices=ALIASES, required=True)
    dr.add_argument('--to', required=True)
    dr.add_argument('--subject', required=True)
    body_group = dr.add_mutually_exclusive_group(required=True)
    body_group.add_argument('--body-file', type=Path)
    body_group.add_argument('--body-stdin', action='store_true')
    dr.add_argument('--out', type=Path, required=True)
    dr.add_argument('--cc', default=None)
    # Send-approved mode: dispatch strictly after owner verification and token match
    sa = sub.add_parser('send-approved')
    sa.add_argument('--draft-manifest', type=Path, required=True)
    sa.add_argument('--approval-token', required=True)
    sa.add_argument('--approved-by-owner', action='store_true', default=False)
    args = parser.parse_args()
    try:
        if args.command == 'import-server':
            result = import_server()
        elif args.command == 'accounts':
            result = {'accounts': [{'alias': a, 'configured': (STORE_ROOT / (a + '.dpapi')).is_file()} for a in ALIASES]}
        elif args.command == 'doctor':
            results = []
            for alias in ALIASES if args.alias == 'all' else [args.alias]:
                try:
                    results.append(doctor(alias))
                except MailError as exc:
                    results.append({'alias': alias, 'status': 'ERROR', 'code': str(exc)})
                except Exception:
                    results.append({'alias': alias, 'status': 'ERROR', 'code': 'transport_or_auth_failed'})
            result = {'accounts': results}
            if any(r['status'] == 'ERROR' for r in results):
                print(json.dumps(result)); return 1
        elif args.command == 'draft':
            body = args.body_file.read_text(encoding='utf-8') if args.body_file else sys.stdin.read()
            result = draft_mail(args.alias, args.to, args.subject, body, args.out, cc=args.cc)
        elif args.command == 'send-approved':
            result = send_approved_mail(args.draft_manifest, args.approval_token, args.approved_by_owner)
        elif args.command == 'range':
            result = range_run(args.alias, args.since, args.until, args.out,
                               max_items=args.max_items, max_scan=args.max_scan,
                               max_calls=args.max_calls, max_bytes=args.max_bytes,
                               max_seconds=args.max_seconds, retry_attempts=args.retry_attempts)
        else:
            result = read_mail(args.alias, args.subject, args.since, args.until,
                               args.out, args.max_scan, args.max_items)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except MailError as exc:
        print(json.dumps({'status': 'ERROR', 'code': str(exc)}))
        return 1
    except Exception:
        print(json.dumps({'status': 'ERROR', 'code': 'transport_or_auth_failed'}))
        return 1


def _range_graph_value(state, url, token):
    """GET the Graph MIME $value endpoint with the shared retry/budget policy."""
    attempt = 0
    while True:
        if state.calls >= state.max_calls:
            raise RangeStop('call_budget')
        if state.clock() - state.started >= state.max_seconds:
            raise RangeStop('time_budget')
        state.calls += 1
        request = urllib.request.Request(url, headers={'Authorization': 'Bearer ' + token}, method='GET')
        opener = urllib.request.build_opener(NoRedirect(),
                                             urllib.request.HTTPSHandler(context=ssl.create_default_context()))
        try:
            with opener.open(request, timeout=30) as response:
                payload = response.read(MAX_BYTES + 1)
            state.add_bytes(len(payload))
            if len(payload) > MAX_BYTES:
                raise MailError('response_too_large')
            return payload
        except urllib.error.HTTPError as exc:
            code = f'provider_http_{exc.code}'
        except MailError:
            raise
        except Exception:
            code = 'provider_http_5xx'
        attempt += 1
        if (code == 'provider_http_429' or code == 'provider_http_403'
                or code.startswith('provider_http_5')) and attempt < state.retry_attempts:
            state.sleeper(state.retry_base_sleep * (2 ** min(attempt - 1, 5)))
            continue
        raise MailError(code) from None


def _range_finish(alias, since, until, out_dir, cp_path, checkpoint, manifest, state,
                  run_index, stop_reason, error_code, exhausted, extra_checkpoint):
    """Shared finish for non-gmail range transports (manifest/coverage/checkpoint)."""
    run_summary = {'run': run_index, 'stop_reason': stop_reason, 'exhausted': exhausted,
                   'error_code': error_code, 'scanned': state.scanned, 'fetched': state.fetched,
                   'skipped': state.skipped, 'already_processed': state.already_processed,
                   'provider_calls': state.calls, 'bytes_read': state.bytes_read,
                   'restarts': state.restarts}
    manifest['runs'].append(run_summary)
    manifest['runs_completed'] = run_index
    manifest['totals']['scanned_total'] += state.scanned
    manifest['totals']['already_processed_total'] += state.already_processed
    skipped_total: dict = {}
    for item in manifest['items'].values():
        if item.get('status') == 'skipped':
            reason = item.get('skip_reason') or 'unknown'
            skipped_total[reason] = skipped_total.get(reason, 0) + 1
    provider_error_skips = any(
        isinstance(item, dict) and str(item.get('skip_reason', '')).startswith('provider_http_')
        for item in manifest['items'].values())
    if exhausted and provider_error_skips:
        exhausted, stop_reason = False, 'item_errors'
    manifest['coverage'] = {'window': {'since': since.isoformat(), 'until': until.isoformat(),
                                       'tz': 'UTC', 'inclusive': True},
                           'fetched_total': sum(1 for i in manifest['items'].values() if i.get('status') == 'fetched'),
                           'skipped_total': skipped_total,
                           **manifest['totals'], 'runs': run_index,
                           'exhausted': exhausted, 'stop_reason': stop_reason,
                           'note': 'exhausted=true means the window enumeration ran out; not a full mailbox inventory'}
    range_write_manifest(out_dir, manifest)
    status = 'exhausted' if exhausted else ('stopped_error' if stop_reason == 'error' else 'stopped_limits')
    payload = {'checkpoint_version': RANGE_MODE_VERSION, 'mode': 'range',
               'mode_version': RANGE_MODE_VERSION, 'alias': alias,
               'since': since.isoformat(), 'until': until.isoformat(),
               'key': range_key(alias, since, until), 'out_dir': str(out_dir),
               'restarts': int((checkpoint or {}).get('restarts') or 0) + state.restarts,
               'runs_completed': run_index, 'status': status, 'stop_reason': stop_reason,
               'updated_at': datetime.now(timezone.utc).isoformat()}
    payload['folder_scope'] = 'inbox'
    payload.update(extra_checkpoint if not exhausted else {k: (None if k == 'graph_next_url' else 0)
                                                           for k in extra_checkpoint})
    save_range_checkpoint(cp_path, payload)
    if stop_reason == 'error':
        raise MailError(error_code or 'range_run_failed') from None
    return {'status': 'RANGE_OK' if exhausted else 'RANGE_PARTIAL', 'alias': alias,
            'since': since.isoformat(), 'until': until.isoformat(), 'tz': 'UTC', 'inclusive': True,
            'batch': {k: run_summary[k] for k in ('fetched', 'scanned', 'skipped', 'already_processed',
                                                  'provider_calls', 'bytes_read', 'restarts')},
            'coverage': manifest['coverage'], 'exhausted': exhausted, 'stop_reason': stop_reason,
            'manifest': str(out_dir / RANGE_MANIFEST_NAME), 'checkpoint': str(cp_path),
            'mail_sent': False, 'mail_flags_changed': False}


def range_run_graph(alias, profile, since, until, out_dir, cp_path, *, max_items, max_scan,
                    max_calls, max_bytes, max_seconds, retry_attempts, retry_base_sleep,
                    clock=None, sleeper=None):
    """Range harvest over Microsoft Graph: $filter window + $orderby asc, MIME via $value."""
    token = oauth_token(alias, profile)
    check_identity(alias, profile, token)
    checkpoint = load_range_checkpoint(cp_path, alias, since, until, out_dir)
    manifest = load_range_manifest(out_dir, alias, since, until)
    run_index = manifest['runs_completed'] + 1
    state = RangeState(max_calls, max_bytes, max_seconds, retry_attempts, retry_base_sleep,
                       clock or time.monotonic, sleeper or time.sleep)
    known = {mid for mid, item in manifest['items'].items()
             if isinstance(item, dict)
             and (item.get('status') == 'fetched'
                  or item.get('skip_reason') in ('date_outside_window', 'message_too_large',
                                                 'provider_http_404'))}
    base_params = {'$filter': f"receivedDateTime ge {since.isoformat()}T00:00:00Z and "
                              f"receivedDateTime le {until.isoformat()}T23:59:59Z",
                  '$select': 'id,conversationId,receivedDateTime', '$orderby': 'receivedDateTime asc',
                  '$top': min(500, max(1, max_scan))}
    base_url = RANGE_GRAPH_LIST_BASE + '?' + urllib.parse.urlencode(base_params)
    checkpoint = checkpoint if (checkpoint or {}).get('folder_scope') == 'inbox' else None
    next_url = (checkpoint or {}).get('graph_next_url')
    if not next_url:
        next_url = base_url
    fetched_batch: list = []
    stop_reason, error_code, exhausted = None, None, False
    try:
        while next_url:
            if not graph_inbox_url_ok(next_url):
                stop_reason, error_code = 'error', 'checkpoint_url_rejected'
                break
            try:
                page = range_provider_call(state, next_url, token)
            except MailError as exc:
                if str(exc) == 'provider_http_400' and next_url != base_url and not state.restarts:
                    # Stale or poisoned nextLink from an older run: restart
                    # enumeration from the base URL; manifest merge keeps ids unique.
                    state.restarts += 1
                    next_url = base_url
                    continue
                stop_reason, error_code = 'error', str(exc)
                break
            items = page.get('value') or []
            follow = page.get('@odata.nextLink') or ''
            for entry in items:
                mid = str(entry.get('id') or '')
                if not re.fullmatch(r'[0-9A-Za-z=_-]{16,200}', mid):
                    stop_reason, error_code = 'error', 'provider_id_invalid'
                    break
                if mid in known:
                    state.already_processed += 1
                    continue
                if state.scanned >= max_scan:
                    stop_reason = 'scan_limit'
                    break
                if state.fetched >= max_items:
                    stop_reason = 'item_limit'
                    break
                state.scanned += 1
                tid = str(entry.get('conversationId') or mid)
                received = str(entry.get('receivedDateTime') or '')
                try:
                    moment = datetime.fromisoformat(received.replace('Z', '+00:00'))
                except ValueError:
                    stop_reason, error_code = 'error', 'provider_date_invalid'
                    break
                in_window = since <= moment.date() <= until
                internal_iso = moment.astimezone(timezone.utc).isoformat()
                skip = None
                raw = b''
                if in_window:
                    value_url = RANGE_GRAPH_VALUE_BASE + urllib.parse.quote(mid, safe='') + '/$value'
                    if not value_url.startswith(RANGE_GRAPH_VALUE_BASE):
                        stop_reason, error_code = 'error', 'checkpoint_url_rejected'
                        break
                    try:
                        payload = _range_graph_value(state, value_url, token)
                        raw = bounded(payload)
                    except MailError as exc:
                        if str(exc) in ('response_too_large', 'message_too_large'):
                            skip = 'message_too_large'
                        elif str(exc).startswith('provider_http_'):
                            skip = str(exc)
                        else:
                            raise
                else:
                    skip = 'date_outside_window'
                if skip is not None:
                    manifest['items'][mid] = {'provider_message_id': mid, 'thread_id': tid,
                                              'internal_date': internal_iso, 'status': 'skipped',
                                              'skip_reason': skip, 'run': run_index}
                    known.add(mid)
                    state.skipped[skip] = state.skipped.get(skip, 0) + 1
                else:
                    fetched_batch.append((mid, tid, internal_iso, raw))
                    state.fetched += 1
                    known.add(mid)
            if stop_reason:
                break
            next_url = follow
            if not follow:
                exhausted, stop_reason = True, 'complete'
                break
            range_flush(out_dir, manifest, fetched_batch, run_index)
            fetched_batch = []
    except RangeStop as exc:
        stop_reason = exc.reason
    range_flush(out_dir, manifest, fetched_batch, run_index)
    return _range_finish(alias, since, until, out_dir, cp_path, checkpoint, manifest, state,
                         run_index, stop_reason, error_code, exhausted,
                         {'graph_next_url': next_url})


def range_run_imap(alias, profile, since, until, out_dir, cp_path, *, max_items, max_scan,
                   max_calls, max_bytes, max_seconds, retry_attempts, retry_base_sleep,
                   clock=None, sleeper=None, imap_factory=None):
    """Range harvest over IMAP: EXAMINE INBOX, UID SEARCH window, BODY.PEEK[] fetches.

    IMAP SINCE/BEFORE have day granularity, so the search window is widened by
    one day on both sides and each message is verified against its INTERNALDATE.
    The cursor is the last processed UID (ascending); resume skips <= last_uid.
    """
    from datetime import timedelta
    client = (imap_factory or connection)(alias, profile)
    status, _ = client.select('INBOX', readonly=True)
    if status != 'OK':
        raise MailError('imap_select_failed') from None
    fmt = '%d-%b-%Y'
    status, data = client.uid('search', None,
                              'SINCE', (since - timedelta(days=1)).strftime(fmt),
                              'BEFORE', (until + timedelta(days=2)).strftime(fmt))
    if status != 'OK' or not data or not data[0]:
        raise MailError('imap_search_failed') from None
    uids = sorted(int(u) for u in data[0].split())
    checkpoint = load_range_checkpoint(cp_path, alias, since, until, out_dir)
    manifest = load_range_manifest(out_dir, alias, since, until)
    run_index = manifest['runs_completed'] + 1
    state = RangeState(max_calls, max_bytes, max_seconds, retry_attempts, retry_base_sleep,
                       clock or time.monotonic, sleeper or time.sleep)
    state.calls += 1  # the UID SEARCH above
    known = {mid for mid, item in manifest['items'].items()
             if isinstance(item, dict)
             and (item.get('status') == 'fetched'
                  or item.get('skip_reason') in ('date_outside_window', 'message_too_large',
                                                 'provider_http_404'))}
    last_uid = int((checkpoint or {}).get('imap_last_uid') or 0)
    fetched_batch: list = []
    stop_reason, error_code, exhausted = None, None, False
    last_processed = last_uid
    try:
        for uid in uids:
            if uid <= last_uid:
                continue
            if state.calls >= state.max_calls:
                raise RangeStop('call_budget')
            if state.clock() - state.started >= state.max_seconds:
                raise RangeStop('time_budget')
            mid = f'imap:INBOX:{uid}'
            if mid in known:
                state.already_processed += 1
                last_processed = uid
                continue
            if state.scanned >= max_scan:
                stop_reason = 'scan_limit'
                break
            if state.fetched >= max_items:
                stop_reason = 'item_limit'
                break
            state.scanned += 1
            state.calls += 1
            status, chunks = client.uid('fetch', str(uid), '(BODY.PEEK[] INTERNALDATE RFC822.SIZE)')
            if status != 'OK':
                stop_reason, error_code = 'error', 'imap_fetch_failed'
                break
            tuples = [v for v in chunks if isinstance(v, tuple)]
            metadata = b' '.join(v[0] for v in tuples) + b' ' + b' '.join(
                v for v in chunks if isinstance(v, bytes))
            size = re.search(rb'RFC822.SIZE (\d+)', metadata)
            internal = re.search(rb'INTERNALDATE "(?P<stamp>[^"]+)"', metadata)
            internal_iso = None
            moment = None
            if internal:
                stamp = internal.group('stamp').decode('ascii', 'replace')
                try:
                    moment = datetime.strptime(stamp, '%d-%b-%Y %H:%M:%S %z')
                except ValueError:
                    moment = None
            if moment is not None:
                internal_iso = moment.astimezone(timezone.utc).isoformat()
            in_window = moment is not None and since <= moment.date() <= until
            skip = None
            raw = b''
            if size and int(size.group(1)) > MAX_BYTES:
                skip = 'message_too_large'
            elif not in_window or not tuples:
                skip = 'date_outside_window' if moment is not None else 'provider_date_invalid'
            else:
                try:
                    raw = bounded(tuples[0][1])
                except MailError as exc:
                    skip = 'message_too_large' if str(exc) in ('message_too_large', 'response_too_large') else None
                    if skip is None:
                        raise
            if skip == 'provider_date_invalid':
                stop_reason, error_code = 'error', skip
                break
            if skip is not None:
                manifest['items'][mid] = {'provider_message_id': mid, 'thread_id': None,
                                          'internal_date': internal_iso, 'status': 'skipped',
                                          'skip_reason': skip, 'run': run_index}
                known.add(mid)
                state.skipped[skip] = state.skipped.get(skip, 0) + 1
            else:
                state.add_bytes(len(raw))
                fetched_batch.append((mid, None, internal_iso, raw))
                state.fetched += 1
                known.add(mid)
            last_processed = uid
        else:
            exhausted, stop_reason = True, 'complete'
    except RangeStop as exc:
        stop_reason = exc.reason
    range_flush(out_dir, manifest, fetched_batch, run_index)
    return _range_finish(alias, since, until, out_dir, cp_path, checkpoint, manifest, state,
                         run_index, stop_reason, error_code, exhausted,
                         {'imap_last_uid': last_processed})


def range_run_pop3(alias, profile, since, until, out_dir, cp_path, *, max_items, max_scan,
                   max_calls, max_bytes, max_seconds, retry_attempts, retry_base_sleep,
                   clock=None, sleeper=None):
    """Range harvest over POP3: TOP-first date filter, RETR only inside the window.

    POP3 has no server-side date search and no folder semantics, so messages
    are walked newest-first by number. The cursor is the last processed number
    (descending); the header Date alone decides window membership before any
    body is fetched. exhausted=true only means the walk reached message 1 or
    the configured scan budget; it is never a mailbox inventory claim.
    """
    client = connection(alias, profile)
    count = client.stat()[0]
    checkpoint = load_range_checkpoint(cp_path, alias, since, until, out_dir)
    manifest = load_range_manifest(out_dir, alias, since, until)
    run_index = manifest['runs_completed'] + 1
    state = RangeState(max_calls, max_bytes, max_seconds, retry_attempts, retry_base_sleep,
                       clock or time.monotonic, sleeper or time.sleep)
    state.calls += 2  # STAT + implicit LIST from the initial handshake bookkeeping
    known = {mid for mid, item in manifest['items'].items()
             if isinstance(item, dict)
             and (item.get('status') == 'fetched'
                  or item.get('skip_reason') in ('date_outside_window', 'message_too_large'))}
    last_number = int((checkpoint or {}).get('pop3_last_number') or count + 1)
    fetched_batch: list = []
    stop_reason, error_code, exhausted = None, None, False
    last_processed = last_number
    try:
        walked = False
        for number in range(min(count, last_number - 1), 0, -1):
            walked = True
            if state.calls >= state.max_calls:
                raise RangeStop('call_budget')
            if state.clock() - state.started >= state.max_seconds:
                raise RangeStop('time_budget')
            mid = f'pop3:INBOX:{number}'
            if mid in known:
                state.already_processed += 1
                last_processed = number
                continue
            if state.scanned >= max_scan:
                stop_reason = 'scan_limit'
                break
            if state.fetched >= max_items:
                stop_reason = 'item_limit'
                break
            state.scanned += 1
            state.calls += 1
            header = b'\r\n'.join(client.top(number, 0)[1]) + b'\r\n'
            msg = BytesParser(policy=policy.default).parsebytes(header, headersonly=True)
            internal_iso = None
            moment = None
            try:
                moment = parsedate_to_datetime(str(msg.get('Date', '')))
                if moment.tzinfo is None:
                    moment = moment.replace(tzinfo=timezone.utc)
            except (ValueError, TypeError, OverflowError):
                moment = None
            if moment is not None:
                internal_iso = moment.astimezone(timezone.utc).isoformat()
            in_window = moment is not None and since <= moment.astimezone().date() <= until
            if not in_window:
                reason = 'date_outside_window' if moment is not None else 'provider_date_invalid'
                manifest['items'][mid] = {'provider_message_id': mid, 'thread_id': None,
                                          'internal_date': internal_iso, 'status': 'skipped',
                                          'skip_reason': reason, 'run': run_index}
                known.add(mid)
                state.skipped[reason] = state.skipped.get(reason, 0) + 1
                last_processed = number
                continue
            state.calls += 1
            raw = bounded(b'\r\n'.join(client.retr(number)[1]))
            state.add_bytes(len(raw))
            fetched_batch.append((mid, None, internal_iso, raw))
            state.fetched += 1
            known.add(mid)
            last_processed = number
        if walked and last_processed <= 1:
            exhausted, stop_reason = True, 'complete'
        elif stop_reason is None:
            stop_reason = 'scan_limit'
    except RangeStop as exc:
        stop_reason = exc.reason
    range_flush(out_dir, manifest, fetched_batch, run_index)
    return _range_finish(alias, since, until, out_dir, cp_path, checkpoint, manifest, state,
                         run_index, stop_reason, error_code, exhausted,
                         {'pop3_last_number': last_processed})


if __name__ == '__main__':
    raise SystemExit(main())
