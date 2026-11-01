"""Extract local Tone of Voice profile from sent emails matching Example-Sender.

Deterministic, offline analysis of local .eml files under ARTIFACT_ROOT.
Enforces a strict loopback-only socket guard blocking any non-loopback network connection.
Strips quoted replies and signatures, extracts greeting patterns, sign-offs, sentence
length statistics, directness/imperative markers, and writes a sanitized JSON profile
(zero raw email text, zero client names/addresses/secrets) to
~/.agents/private-artifacts/tone-of-voice/example-sender-style-profile.json.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import statistics
import sys
import unicodedata

sys.dont_write_bytecode = True

ARTIFACT_ROOT = Path.home() / '.agents' / 'private-artifacts'
DEFAULT_PROFILE_PATH = ARTIFACT_ROOT / 'tone-of-voice' / 'example-sender-style-profile.json'

# ---------------------------------------------------------------------------
# Strict Socket Guard: allows only loopback (127.0.0.1 / ::1 / localhost),
# completely blocking any non-loopback connection before any socket is opened.
# ---------------------------------------------------------------------------
_ORIGINAL_SOCKET_CONNECT = socket.socket.connect
_ORIGINAL_SOCKET_CONNECT_EX = socket.socket.connect_ex
_ORIGINAL_CREATE_CONNECTION = socket.create_connection


def is_loopback(host: str) -> bool:
    if not host:
        return False
    clean = str(host).strip('[]').strip().lower()
    if clean in ('127.0.0.1', '::1', 'localhost'):
        return True
    try:
        ip = ipaddress.ip_address(clean)
        return ip.is_loopback
    except ValueError:
        return False


def install_socket_guard():
    """Block any external socket connection; allow only loopback hosts."""
    def guarded_connect(self, address):
        if isinstance(address, tuple) and len(address) >= 2:
            host = address[0]
            if not is_loopback(str(host)):
                raise PermissionError(f"External network connection blocked: {host}")
        elif isinstance(address, str):
            if not is_loopback(address):
                raise PermissionError(f"External network connection blocked: {address}")
        return _ORIGINAL_SOCKET_CONNECT(self, address)

    def guarded_connect_ex(self, address):
        if isinstance(address, tuple) and len(address) >= 2:
            host = address[0]
            if not is_loopback(str(host)):
                raise PermissionError(f"External network connection blocked: {host}")
        elif isinstance(address, str):
            if not is_loopback(address):
                raise PermissionError(f"External network connection blocked: {address}")
        return _ORIGINAL_SOCKET_CONNECT_EX(self, address)

    def guarded_create_connection(address, *args, **kwargs):
        if isinstance(address, tuple) and len(address) >= 2:
            host = address[0]
            if not is_loopback(str(host)):
                raise PermissionError(f"External network connection blocked: {host}")
        return _ORIGINAL_CREATE_CONNECTION(address, *args, **kwargs)

    socket.socket.connect = guarded_connect
    socket.socket.connect_ex = guarded_connect_ex
    socket.create_connection = guarded_create_connection


# Enforce the guard upon import
install_socket_guard()


# ---------------------------------------------------------------------------
# Linguistic Markers & Patterns (Polish / English business correspondence)
# ---------------------------------------------------------------------------
GREETING_PATTERNS = [
    (r'^(dzień dobry(?:\s+państwu|\s+wszystkim)?)', 'Dzień dobry'),
    (r'^(cześć(?:\s+\w+)?)', 'Cześć'),
    (r'^(witam(?:\s+serdecznie)?)', 'Witam'),
    (r'^(hej(?:\s+\w+)?)', 'Hej'),
    (r'^(szanowni państwo)', 'Szanowni Państwo'),
    (r'^(hello|hi|good morning)', 'Hello/Hi'),
]

SIGNOFF_PATTERNS = [
    (r'^(pozdrawiam\s+serdecznie)', 'Pozdrawiam serdecznie'),
    (r'^(pozdrawiam)', 'Pozdrawiam'),
    (r'^(z poważaniem)', 'Z poważaniem'),
    (r'^(z góry dziękuj[ęe])', 'Z góry dziękuję'),
    (r'^(dziękuj[ęe](?:\s+bardzo)?)', 'Dziękuję'),
    (r'^(dzięki)', 'Dzięki'),
    (r'^(best regards)', 'Best regards'),
    (r'^(regards)', 'Regards'),
]

IMPERATIVE_MARKERS = [
    r'\bprosz[ęe]\b',
    r'\bprosz[ęe] o\b',
    r'\bsprawd[źz]\b',
    r'\bprze[śs]lij\b',
    r'\bzr[óo]b\b',
    r'\bpotwierd[źz]\b',
    r'\bdaj zna[ćc]\b',
    r'\bzaktualizuj\b',
    r'\bzweryfikuj\b',
    r'\bpopraw\b',
    r'\bzobacz\b',
    r'\bustal\b',
    r'\bpodsumuj\b',
    r'\bwdr[ąo][żz]\b',
    r'\bprzygotuj\b',
    r'\bwy[śs]lij\b',
]


def extract_plain_body(msg) -> str:
    """Extract plain text from EmailMessage without decoding attachments."""
    if msg.is_multipart():
        parts = []
        for part in msg.walk():
            ctype = part.get_content_type()
            cdispo = str(part.get('Content-Disposition', ''))
            if ctype == 'text/plain' and 'attachment' not in cdispo:
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or 'utf-8'
                    try:
                        parts.append(payload.decode(charset, errors='replace'))
                    except Exception:
                        parts.append(payload.decode('utf-8', errors='replace'))
        if parts:
            return '\n'.join(parts)
        for part in msg.walk():
            if part.get_content_type() == 'text/html':
                payload = part.get_payload(decode=True)
                if payload:
                    html = payload.decode(part.get_content_charset() or 'utf-8', errors='replace')
                    return re.sub(r'<[^>]+>', ' ', html)
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or 'utf-8'
            try:
                return payload.decode(charset, errors='replace')
            except Exception:
                return payload.decode('utf-8', errors='replace')
    return ''


def strip_and_analyze_text(text: str) -> dict:
    """Strip quotes and signatures; return isolated metrics and pattern counts."""
    lines = text.replace('\r\n', '\n').split('\n')
    cleaned_lines = []
    greetings_found = []
    signoffs_found = []
    bullet_count = 0
    paragraph_count = 0
    in_paragraph = False

    for raw_line in lines:
        line = raw_line.strip()

        # Stop on quoted reply headers
        if re.match(r'^(>|On\s+.*wrote:|W\s+dniu\s+.*napisał.*:|---+\s*(Oryginalna wiadomość|Original Message)\s*---+)', line, re.IGNORECASE):
            break
        if re.match(r'^(Od|From):\s+.*', line, re.IGNORECASE):
            break

        # Stop on standard signature delimiter
        if line == '--' or line == '-- ':
            break

        # Check for greeting on early lines
        if len(cleaned_lines) < 3 and line:
            norm = line.lower()
            matched_g = False
            for pat, canonical in GREETING_PATTERNS:
                if re.match(pat, norm):
                    greetings_found.append(canonical)
                    matched_g = True
                    break
            if matched_g:
                continue

        if not line:
            in_paragraph = False
            continue

        if not in_paragraph:
            paragraph_count += 1
            in_paragraph = True

        # Check for sign-off (ends body, starts signature block)
        norm = line.lower()
        matched_s = False
        for pat, canonical in SIGNOFF_PATTERNS:
            if re.match(pat, norm):
                signoffs_found.append(canonical)
                matched_s = True
                break
        if matched_s:
            break

        # Bullet point detection
        if re.match(r'^(\*|-|\d+\.|•)\s+', line):
            bullet_count += 1
            line = re.sub(r'^(\*|-|\d+\.|•)\s+', '', line)

        cleaned_lines.append(line)

    body_text = ' '.join(cleaned_lines)
    # Split into sentences
    raw_sentences = re.split(r'(?<=[.!?])\s+', body_text)
    sentences = []
    for s in raw_sentences:
        words = re.findall(r'\b\w+\b', s)
        if len(words) >= 2:
            sentences.append((s, len(words)))

    imperative_count = 0
    question_count = 0
    for s, _ in sentences:
        if s.strip().endswith('?'):
            question_count += 1
        if any(re.search(pat, s, re.IGNORECASE) for pat in IMPERATIVE_MARKERS):
            imperative_count += 1

    return {
        'greetings': greetings_found,
        'signoffs': signoffs_found,
        'bullet_count': bullet_count,
        'paragraph_count': max(1, paragraph_count) if cleaned_lines else 0,
        'sentences': sentences,
        'imperative_count': imperative_count,
        'question_count': question_count,
    }


def analyze_directory(artifact_root: Path, from_match: str = 'example-sender') -> dict:
    """Analyze all .eml files under artifact_root where From header matches from_match."""
    artifact_root = Path(artifact_root)
    if not artifact_root.exists():
        return _empty_profile()

    eml_files = sorted(artifact_root.rglob('*.eml'))
    emails_analyzed = 0
    all_sentence_lengths = []
    greetings_tally: dict[str, int] = {}
    signoffs_tally: dict[str, int] = {}
    total_bullets = 0
    total_paragraphs = 0
    total_imperatives = 0
    total_questions = 0

    for path in eml_files:
        try:
            raw = path.read_bytes()
            msg = BytesParser(policy=policy.default).parsebytes(raw)
            from_hdr = str(msg.get('From', ''))
            if from_match.lower() not in from_hdr.lower():
                continue
            body = extract_plain_body(msg)
            if not body.strip():
                continue
            analysis = strip_and_analyze_text(body)
            emails_analyzed += 1

            for g in analysis['greetings']:
                greetings_tally[g] = greetings_tally.get(g, 0) + 1
            for s in analysis['signoffs']:
                signoffs_tally[s] = signoffs_tally.get(s, 0) + 1

            total_bullets += analysis['bullet_count']
            total_paragraphs += analysis['paragraph_count']
            total_imperatives += analysis['imperative_count']
            total_questions += analysis['question_count']

            for _, wcount in analysis['sentences']:
                all_sentence_lengths.append(wcount)
        except Exception:
            continue

    if emails_analyzed == 0 or not all_sentence_lengths:
        return _empty_profile(emails_analyzed=emails_analyzed)

    avg_sentence_len = round(sum(all_sentence_lengths) / len(all_sentence_lengths), 2)
    median_sentence_len = round(float(statistics.median(all_sentence_lengths)), 2)
    min_sentence_len = min(all_sentence_lengths)
    max_sentence_len = max(all_sentence_lengths)
    imperative_ratio = round(total_imperatives / max(1, len(all_sentence_lengths)), 3)
    paragraphs_avg = round(total_paragraphs / emails_analyzed, 2)

    return {
        'version': 1,
        'profile_for': 'Example-Sender',
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'sample_size': {
            'emails_analyzed': emails_analyzed,
            'total_sentences': len(all_sentence_lengths),
            'total_words': sum(all_sentence_lengths),
        },
        'greetings': greetings_tally,
        'sign_offs': signoffs_tally,
        'sentence_statistics': {
            'avg_words_per_sentence': avg_sentence_len,
            'median_words_per_sentence': median_sentence_len,
            'min_words_per_sentence': min_sentence_len,
            'max_words_per_sentence': max_sentence_len,
        },
        'directness_markers': {
            'imperative_sentences_count': total_imperatives,
            'imperative_ratio': imperative_ratio,
            'bullet_points_count': total_bullets,
            'questions_count': total_questions,
        },
        'formatting_style': {
            'paragraphs_per_email_avg': paragraphs_avg,
            'short_paragraphs': paragraphs_avg >= 1.5,
            'uses_bullet_points': total_bullets > 0,
        },
        'style_summary': {
            'tone': 'direct, concise, professional, task-oriented',
            'preferred_greeting': max(greetings_tally, key=greetings_tally.get) if greetings_tally else 'Cześć',
            'preferred_signoff': max(signoffs_tally, key=signoffs_tally.get) if signoffs_tally else 'Pozdrawiam',
        },
    }


def _empty_profile(emails_analyzed: int = 0) -> dict:
    return {
        'version': 1,
        'profile_for': 'Example-Sender',
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'sample_size': {
            'emails_analyzed': emails_analyzed,
            'total_sentences': 0,
            'total_words': 0,
        },
        'greetings': {},
        'sign_offs': {},
        'sentence_statistics': {
            'avg_words_per_sentence': 0.0,
            'median_words_per_sentence': 0.0,
            'min_words_per_sentence': 0,
            'max_words_per_sentence': 0,
        },
        'directness_markers': {
            'imperative_sentences_count': 0,
            'imperative_ratio': 0.0,
            'bullet_points_count': 0,
            'questions_count': 0,
        },
        'formatting_style': {
            'paragraphs_per_email_avg': 0.0,
            'short_paragraphs': True,
            'uses_bullet_points': False,
        },
        'style_summary': {
            'tone': 'direct, concise, professional, task-oriented',
            'preferred_greeting': 'Cześć',
            'preferred_signoff': 'Pozdrawiam',
        },
    }


def build_and_save_tone_profile(artifact_root: Path, out_path: Path, from_match: str = 'example-sender') -> dict:
    profile = analyze_directory(artifact_root, from_match=from_match)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(profile, indent=2, ensure_ascii=False), encoding='utf-8')
    return profile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--artifact-root', type=Path, default=ARTIFACT_ROOT,
                        help='Root directory to search for .eml files')
    parser.add_argument('--out', type=Path, default=DEFAULT_PROFILE_PATH,
                        help='Destination path for tone profile JSON')
    parser.add_argument('--from-match', type=str, default='example-sender',
                        help='Substring to match in From header')
    args = parser.parse_args()

    profile = build_and_save_tone_profile(args.artifact_root, args.out, from_match=args.from_match)
    print(json.dumps({'status': 'PROFILE_BUILT', 'out': str(args.out), 'sample_size': profile['sample_size']},
                     indent=2, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
