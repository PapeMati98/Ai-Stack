"""Unit tests for local Tone of Voice profile extractor and two-stage email send.

Tests:
1. build_tone_profile:
   - Loopback socket guard blocks any external host connection and allows loopback.
   - Analysis of local .eml files matching Example-Sender strips quotes and signatures.
   - Extracts greeting patterns, sign-offs, sentence lengths, and directness markers.
   - Sanitized output JSON contains zero raw email text, zero client PII, zero secrets.
   - Empty/missing directory handled gracefully.
2. mail_direct draft:
   - Creates draft .eml and draft-manifest.json under ARTIFACT_ROOT.
   - Computes subject_sha256, body_sha256, approval_digest, status=WAITING_OWNER_APPROVAL.
   - Offline operation with zero network calls.
   - Rejects unknown alias, missing recipient, missing subject.
3. mail_direct send-approved:
   - Fails before profile load/network when MAIL_DIRECT_ALLOW_SEND != '1'.
   - Fails before profile load/network when approved_by_owner is False.
   - Fails before profile load/network when approval_token does not match recomputed digest.
   - Fails when draft .eml is tampered on disk.
   - Rejects manifests outside ARTIFACT_ROOT.
   - Successfully dispatches via send_transport when all gates and digest match.
   - Marks manifest status='SENT' and records sent_at timestamp.
   - Rejects already-sent manifests.
"""
from __future__ import annotations

from datetime import datetime, timezone
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch, MagicMock

# Dynamically import scripts under test
EMAIL_DIR = Path(__file__).parents[1]
SCRIPTS_DIR = EMAIL_DIR / 'scripts'

SPEC_TONE = importlib.util.spec_from_file_location(
    'build_tone_profile', SCRIPTS_DIR / 'build_tone_profile.py'
)
tone_mod = importlib.util.module_from_spec(SPEC_TONE)
SPEC_TONE.loader.exec_module(tone_mod)

SPEC_MAIL = importlib.util.spec_from_file_location(
    'mail_direct', SCRIPTS_DIR / 'mail_direct.py'
)
m = importlib.util.module_from_spec(SPEC_MAIL)
SPEC_MAIL.loader.exec_module(m)


def fake_private(p, create=False):
    p = Path(p)
    if create:
        p.mkdir(parents=True, exist_ok=True)
    return p


def create_test_eml(from_addr: str, to_addr: str, subject: str, body: str) -> bytes:
    msg = EmailMessage()
    msg['From'] = from_addr
    msg['To'] = to_addr
    msg['Subject'] = subject
    msg['Date'] = datetime.now(timezone.utc).strftime('%a, %d %b %Y %H:%M:%S +0000')
    msg.set_content(body)
    return msg.as_bytes()


class ToneProfileTests(unittest.TestCase):
    def test_socket_guard_blocks_external_host_connections(self):
        # The guard must immediately block any non-loopback host
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            with self.assertRaises(PermissionError) as ctx:
                s.connect(('8.8.8.8', 53))
            self.assertIn('External network connection blocked', str(ctx.exception))

            with self.assertRaises(PermissionError) as ctx:
                s.connect(('example-mail-one.invalid', 80))
            self.assertIn('External network connection blocked', str(ctx.exception))

            with self.assertRaises(PermissionError) as ctx:
                s.connect_ex(('93.184.216.34', 443))
            self.assertIn('External network connection blocked', str(ctx.exception))

            with self.assertRaises(PermissionError) as ctx:
                socket.create_connection(('1.1.1.1', 80))
            self.assertIn('External network connection blocked', str(ctx.exception))
        finally:
            s.close()

    def test_socket_guard_allows_loopback_hosts(self):
        self.assertTrue(tone_mod.is_loopback('127.0.0.1'))
        self.assertTrue(tone_mod.is_loopback('::1'))
        self.assertTrue(tone_mod.is_loopback('localhost'))
        self.assertTrue(tone_mod.is_loopback('127.0.0.2'))
        self.assertFalse(tone_mod.is_loopback('8.8.8.8'))
        self.assertFalse(tone_mod.is_loopback('example-mail-one.invalid'))

        # Connecting to a loopback address where nothing listens should raise ConnectionRefusedError or OSError,
        # but NEVER PermissionError from the guard.
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            with self.assertRaises(OSError) as ctx:
                s.connect(('127.0.0.1', 59998))
            self.assertNotIsInstance(ctx.exception, PermissionError)
        finally:
            s.close()

    def test_build_tone_profile_extracts_metrics_and_strips_quotes_and_signatures(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp) / 'private-artifacts'
            tmp_root.mkdir(parents=True)

            # Email 1 from Example-Sender: contains greeting, imperatives, bullet points, signoff, signature, quoted reply
            eml1_body = (
                "Cześć,\n\n"
                "Proszę o przygotowanie raportu na wtorek.\n"
                "Sprawdź koniecznie poprawność obliczeń w załącznikach.\n\n"
                "- punkt pierwszy do wdrożenia\n"
                "- punkt drugi do weryfikacji\n\n"
                "Czy możesz to potwierdzić dzisiaj?\n\n"
                "Pozdrawiam,\n"
                "Example-Sender\n"
                "-- \n"
                "Example-Sender Name\n"
                "Example Role\n"
                "Tel: +1 202 555 0100\n"
                "example-sender.name@example-mail-one.invalid\n\n"
                "> W dniu wczorajszym klient napisał:\n"
                "> Dzień dobry, poproszę o wycenę."
            )
            eml1_bytes = create_test_eml(
                "Example-Sender Name <example-sender.name@example-mail-one.invalid>",
                "zespol@example-mail-one.invalid",
                "Raport tygodniowy",
                eml1_body
            )
            (tmp_root / 'msg1.eml').write_bytes(eml1_bytes)

            # Email 2 from Example-Sender: different greeting and signoff
            eml2_body = (
                "Dzień dobry,\n\n"
                "Przesyłam zaktualizowane zestawienie.\n"
                "Proszę o wdrożenie zmian do piątku.\n\n"
                "Dzięki,\n"
                "Example-Sender"
            )
            eml2_bytes = create_test_eml(
                "Example-Sender Name <example-sender@example-mail-two.invalid>",
                "finanse@example-mail-two.invalid",
                "Aktualizacja tabel",
                eml2_body
            )
            (tmp_root / 'msg2.eml').write_bytes(eml2_bytes)

            # Email 3 from someone else: must be skipped
            eml3_body = "Cześć Example-Sender, przesyłam fakturę."
            eml3_bytes = create_test_eml(
                "External Sender <klient@external.invalid>",
                "example-sender@example-mail-one.invalid",
                "Faktura",
                eml3_body
            )
            (tmp_root / 'msg3.eml').write_bytes(eml3_bytes)

            out_file = tmp_root / 'tone-of-voice' / 'example-sender-style-profile.json'
            profile = tone_mod.build_and_save_tone_profile(tmp_root, out_file, from_match='example-sender')

            self.assertTrue(out_file.exists())
            self.assertEqual(profile['sample_size']['emails_analyzed'], 2)
            self.assertIn('Cześć', profile['greetings'])
            self.assertIn('Dzień dobry', profile['greetings'])
            self.assertIn('Pozdrawiam', profile['sign_offs'])
            self.assertIn('Dzięki', profile['sign_offs'])

            # Sentence stats
            self.assertGreater(profile['sentence_statistics']['avg_words_per_sentence'], 0)
            self.assertGreater(profile['sentence_statistics']['median_words_per_sentence'], 0)

            # Directness & formatting
            self.assertGreater(profile['directness_markers']['imperative_sentences_count'], 0)
            self.assertEqual(profile['directness_markers']['bullet_points_count'], 2)
            self.assertEqual(profile['directness_markers']['questions_count'], 1)
            self.assertTrue(profile['formatting_style']['uses_bullet_points'])

            # Sanitization verification: NO raw email text, NO client data, NO secrets
            profile_json_text = out_file.read_text(encoding='utf-8')
            self.assertNotIn('Tel: +1 202 555 0100', profile_json_text)
            self.assertNotIn('example-sender.name@example-mail-one.invalid', profile_json_text)
            self.assertNotIn('Example Role', profile_json_text)
            self.assertNotIn('klient@external.invalid', profile_json_text)
            self.assertNotIn('W dniu wczorajszym klient napisał', profile_json_text)
            self.assertNotIn('Raport tygodniowy', profile_json_text)
            self.assertNotIn('Proszę o przygotowanie raportu na wtorek', profile_json_text)

    def test_empty_directory_handling(self):
        with tempfile.TemporaryDirectory() as tmp:
            empty_root = Path(tmp) / 'empty'
            empty_root.mkdir()
            out_file = Path(tmp) / 'out.json'
            profile = tone_mod.build_and_save_tone_profile(empty_root, out_file)
            self.assertEqual(profile['sample_size']['emails_analyzed'], 0)
            self.assertEqual(profile['sentence_statistics']['avg_words_per_sentence'], 0.0)


class MailDirectDraftTests(unittest.TestCase):
    def test_draft_creates_manifest_and_eml(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(m, 'ARTIFACT_ROOT', Path(tmp)), \
                 patch.object(m, 'private_directory', side_effect=fake_private):
                out_dir = Path(tmp) / 'drafts' / 'd1'
                body = "Cześć,\nProszę o potwierdzenie stanu zamówienia.\nPozdrawiam"
                res = m.draft_mail(
                    alias='example-pop3',
                    to='zamowienia@example-mail-one.invalid',
                    subject='Status zamówienia #102',
                    body=body,
                    out=out_dir,
                    cc='kopia@example-mail-one.invalid'
                )

                self.assertEqual(res['status'], 'DRAFT_CREATED')
                self.assertFalse(res['mail_sent'])
                manifest_path = Path(res['manifest'])
                self.assertTrue(manifest_path.exists())
                self.assertTrue(manifest_path.is_relative_to(Path(tmp)))

                manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
                self.assertEqual(manifest['alias'], 'example-pop3')
                self.assertEqual(manifest['to'], 'zamowienia@example-mail-one.invalid')
                self.assertEqual(manifest['cc'], 'kopia@example-mail-one.invalid')
                self.assertEqual(manifest['status'], 'WAITING_OWNER_APPROVAL')

                draft_id = manifest['draft_id']
                expected_subj_sha = hashlib.sha256('Status zamówienia #102'.encode('utf-8')).hexdigest()
                expected_body_sha = hashlib.sha256(body.encode('utf-8')).hexdigest()
                expected_approval = hashlib.sha256(
                    f'{draft_id}:example-pop3:zamowienia@example-mail-one.invalid:{expected_body_sha}'.encode('utf-8')
                ).hexdigest()

                self.assertEqual(manifest['subject_sha256'], expected_subj_sha)
                self.assertEqual(manifest['body_sha256'], expected_body_sha)
                self.assertEqual(manifest['approval_digest'], expected_approval)
                self.assertEqual(res['approval_digest'], expected_approval)

                # Verify draft .eml
                eml_path = out_dir / 'draft.eml'
                self.assertTrue(eml_path.exists())
                raw_bytes = eml_path.read_bytes()
                self.assertIn(b'Status', raw_bytes)
                parsed_back = BytesParser(policy=policy.default).parsebytes(raw_bytes)
                self.assertEqual(str(parsed_back['Subject']), 'Status zamówienia #102')
                self.assertIn(b'zamowienia@example-mail-one.invalid', raw_bytes)
                self.assertIn(b'kopia@example-mail-one.invalid', raw_bytes)

    def test_draft_offline_no_network_or_profile_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(m, 'ARTIFACT_ROOT', Path(tmp)), \
                 patch.object(m, 'private_directory', side_effect=fake_private), \
                 patch.object(m, 'http', side_effect=AssertionError("Network called during draft")), \
                 patch.object(m, 'connection', side_effect=AssertionError("Connection opened during draft")):
                res = m.draft_mail(
                    alias='microsoft-example-1',
                    to='jan@example-mail-two.invalid',
                    subject='Spotkanie',
                    body='Dzień dobry, potwierdzam.',
                    out=Path(tmp) / 'drafts' / 'offline'
                )
                self.assertEqual(res['status'], 'DRAFT_CREATED')

    def test_draft_validation_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(m, 'ARTIFACT_ROOT', Path(tmp)), \
                 patch.object(m, 'private_directory', side_effect=fake_private):
                with self.assertRaisesRegex(m.MailError, 'unknown_alias'):
                    m.draft_mail('unsupported-alias', 'a@example-mail-one.invalid', 'Subj', 'Body', Path(tmp) / 'out')
                with self.assertRaisesRegex(m.MailError, 'recipient_missing'):
                    m.draft_mail('example-pop3', '', 'Subj', 'Body', Path(tmp) / 'out')
                with self.assertRaisesRegex(m.MailError, 'subject_missing'):
                    m.draft_mail('example-pop3', 'a@example-mail-one.invalid', '   ', 'Body', Path(tmp) / 'out')


class MailDirectSendApprovedTests(unittest.TestCase):
    def setUp(self):
        self.original_env = os.environ.get('MAIL_DIRECT_ALLOW_SEND')

    def tearDown(self):
        if self.original_env is not None:
            os.environ['MAIL_DIRECT_ALLOW_SEND'] = self.original_env
        else:
            os.environ.pop('MAIL_DIRECT_ALLOW_SEND', None)

    def test_send_approved_blocks_when_env_gate_disabled(self):
        os.environ['MAIL_DIRECT_ALLOW_SEND'] = '0'
        with tempfile.TemporaryDirectory() as tmp:
            manifest_path = Path(tmp) / 'draft-manifest.json'
            with self.assertRaisesRegex(m.MailError, 'owner_approval_required'):
                m.send_approved_mail(manifest_path, 'token', approved_by_owner=True)

    def test_send_approved_blocks_when_approved_by_owner_is_false(self):
        os.environ['MAIL_DIRECT_ALLOW_SEND'] = '1'
        with tempfile.TemporaryDirectory() as tmp:
            manifest_path = Path(tmp) / 'draft-manifest.json'
            with self.assertRaisesRegex(m.MailError, 'owner_approval_required'):
                m.send_approved_mail(manifest_path, 'token', approved_by_owner=False)

    def test_send_approved_blocks_on_token_mismatch_before_load_profile_or_network(self):
        os.environ['MAIL_DIRECT_ALLOW_SEND'] = '1'
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(m, 'ARTIFACT_ROOT', Path(tmp)), \
                 patch.object(m, 'private_directory', side_effect=fake_private), \
                 patch.object(m, 'load_profile', side_effect=AssertionError("load_profile called prematurely")), \
                 patch.object(m, 'send_transport', side_effect=AssertionError("send_transport called prematurely")):
                out_dir = Path(tmp) / 'draft-out'
                res = m.draft_mail('example-pop3', 'a@example-mail-one.invalid', 'Subj', 'Body text', out_dir)
                manifest_path = Path(res['manifest'])

                with self.assertRaisesRegex(m.MailError, 'approval_token_mismatch'):
                    m.send_approved_mail(manifest_path, 'wrong-token', approved_by_owner=True)

    def test_send_approved_blocks_on_tampered_eml_before_load_profile_or_network(self):
        os.environ['MAIL_DIRECT_ALLOW_SEND'] = '1'
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(m, 'ARTIFACT_ROOT', Path(tmp)), \
                 patch.object(m, 'private_directory', side_effect=fake_private), \
                 patch.object(m, 'load_profile', side_effect=AssertionError("load_profile called prematurely")), \
                 patch.object(m, 'send_transport', side_effect=AssertionError("send_transport called prematurely")):
                out_dir = Path(tmp) / 'draft-out'
                res = m.draft_mail('example-pop3', 'a@example-mail-one.invalid', 'Subj', 'Original body', out_dir)
                manifest_path = Path(res['manifest'])
                valid_token = res['approval_digest']

                # Tamper with draft.eml on disk
                eml_path = out_dir / 'draft.eml'
                tampered_bytes = create_test_eml('sender@test', 'a@example-mail-one.invalid', 'Subj', 'Tampered modified body')
                eml_path.write_bytes(tampered_bytes)

                with self.assertRaisesRegex(m.MailError, 'approval_token_mismatch'):
                    m.send_approved_mail(manifest_path, valid_token, approved_by_owner=True)

    def test_send_approved_rejects_manifest_outside_artifact_root(self):
        os.environ['MAIL_DIRECT_ALLOW_SEND'] = '1'
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(m, 'ARTIFACT_ROOT', Path(tmp) / 'allowed-root'):
                (Path(tmp) / 'allowed-root').mkdir(parents=True)
                outside = Path(tmp) / 'outside' / 'draft-manifest.json'
                outside.parent.mkdir(parents=True)
                outside.write_text('{}')

                with self.assertRaisesRegex(m.MailError, 'artifact_root_rejected'):
                    m.send_approved_mail(outside, 'token', approved_by_owner=True)

    def test_send_approved_success_and_updates_manifest(self):
        os.environ['MAIL_DIRECT_ALLOW_SEND'] = '1'
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(m, 'ARTIFACT_ROOT', Path(tmp)), \
                 patch.object(m, 'private_directory', side_effect=fake_private), \
                 patch.object(m, 'load_profile', return_value={'kind': 'pop3', 'email': 'demo@example.test', 'host': 'pop3.example-mail-one.invalid', 'port': 995, 'ssl': True}), \
                 patch.object(m, 'send_transport', return_value={'sent': True}) as mock_send:

                out_dir = Path(tmp) / 'draft-out'
                res = m.draft_mail('example-pop3', 'target@example.test', 'Zlecenie', 'Wysyłam potwierdzenie.', out_dir)
                manifest_path = Path(res['manifest'])
                token = res['approval_digest']

                send_res = m.send_approved_mail(manifest_path, token, approved_by_owner=True)
                self.assertEqual(send_res['status'], 'SENT')
                self.assertTrue(send_res['mail_sent'])
                mock_send.assert_called_once()

                # Verify manifest updated
                updated_manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
                self.assertEqual(updated_manifest['status'], 'SENT')
                self.assertIn('sent_at', updated_manifest)

                # Attempting to re-send must fail
                with self.assertRaisesRegex(m.MailError, 'draft_already_sent'):
                    m.send_approved_mail(manifest_path, token, approved_by_owner=True)

    def test_send_transport_supported_kinds(self):
        with patch.object(m, 'oauth_token', return_value='token123'), \
             patch.object(m, 'check_identity', return_value='me@test'), \
             patch('urllib.request.build_opener') as mock_opener_cls:

            # Fake HTTP opener
            mock_opener = MagicMock()
            mock_response = MagicMock()
            mock_response.read.return_value = json.dumps({'id': 'gmail_msg_123'}).encode('utf-8')
            mock_opener.open.return_value.__enter__.return_value = mock_response
            mock_opener_cls.return_value = mock_opener

            # Test Gmail dispatch
            res_gmail = m.send_transport(
                'google-example-3',
                {'kind': 'gmail'},
                b'From: me@test\r\n\r\nHello',
                {'to': 'client@test'}
            )
            self.assertTrue(res_gmail['sent'])
            self.assertEqual(res_gmail['provider_id'], 'gmail_msg_123')

            # Test Graph dispatch
            mock_response.read.return_value = b'{}'
            res_graph = m.send_transport(
                'microsoft-example-1',
                {'kind': 'graph'},
                b'From: me@test\r\nSubject: Hi\r\n\r\nHello',
                {'to': 'client@test'}
            )
            self.assertTrue(res_graph['sent'])

        # Test SMTP dispatch for pop3/imap profiles
        with patch('smtplib.SMTP_SSL') as mock_smtp_ssl:
            mock_server = MagicMock()
            mock_smtp_ssl.return_value = mock_server

            res_smtp = m.send_transport(
                'example-pop3',
                {'kind': 'pop3', 'host': 'pop3.example-mail-one.invalid', 'email': 'me@example-mail-one.invalid', 'password': 'pass', 'ssl': True},
                b'From: me@example-mail-one.invalid\r\n\r\nHello',
                {'to': 'client@test'}
            )
            self.assertTrue(res_smtp['sent'])
            mock_server.sendmail.assert_called_once()
            mock_server.quit.assert_called_once()


class CliSmokeTests(unittest.TestCase):
    def test_cli_draft_help_smoke(self):
        script = SCRIPTS_DIR / 'mail_direct.py'
        flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        proc = subprocess.run([sys.executable, '-B', str(script), 'draft', '--help'],
                              capture_output=True, text=True, timeout=120, creationflags=flags)
        self.assertEqual(proc.returncode, 0)
        for flag in ('--alias', '--to', '--subject', '--out'):
            self.assertIn(flag, proc.stdout)

    def test_cli_send_approved_help_smoke(self):
        script = SCRIPTS_DIR / 'mail_direct.py'
        flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        proc = subprocess.run([sys.executable, '-B', str(script), 'send-approved', '--help'],
                              capture_output=True, text=True, timeout=120, creationflags=flags)
        self.assertEqual(proc.returncode, 0)
        for flag in ('--draft-manifest', '--approval-token', '--approved-by-owner'):
            self.assertIn(flag, proc.stdout)

    def test_cli_tone_profile_help_smoke(self):
        script = SCRIPTS_DIR / 'build_tone_profile.py'
        flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        proc = subprocess.run([sys.executable, '-B', str(script), '--help'],
                              capture_output=True, text=True, timeout=120, creationflags=flags)
        self.assertEqual(proc.returncode, 0)
        for flag in ('--artifact-root', '--out', '--from-match'):
            self.assertIn(flag, proc.stdout)


if __name__ == '__main__':
    unittest.main()
