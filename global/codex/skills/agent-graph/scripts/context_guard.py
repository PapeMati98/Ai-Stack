"""Bounded fail-closed guard for text crossing the maker/check -> reviewer boundary.

Reuses the Fleet redactor (``redact_sensitive_value``) and adds the
structure-aware passes a plain text redactor does not cover:

- JSON / JSONL values under secret-bearing keys (``password``, ``token``,
  ``authorization``, ``api_key``, ``private_key``, ...). Recognizable secret-bearing keys are redacted with collision-safe
  suffixes. Repeated JSON keys withhold the fragment explicitly; source data
  is retained and never silently collapsed.
- Recognizable incomplete PEM fragments (BEGIN without END, END without
  BEGIN), bounded to the base64 body run.
- Bounded base64-encoded recognizable secrets: a blob is redacted only when
  it decodes to a recognizable secret shape; benign encoded content stays.

Benign UUIDs, paths, Unicode and untouched JSON are returned byte-identical
where possible. Any bound breach or redactor failure raises ``GuardError``
without carrying fragment content, so callers withhold the fragment
(fail-closed). This is a bounded guard, not a full DLP guarantee.
"""
import base64
import binascii
import json
import re

MAX_TEXT_BYTES = 64 * 1024
MAX_JSON_DEPTH = 24
MAX_LINES = 8192
MAX_PEM_BODY_LINES = 64
MAX_B64_TOKEN_CHARS = 2048
MAX_B64_DECODE_BYTES = 1536
MAX_B64_TOKENS = 512
# Long blobs are scanned in overlapping 4-aligned decode windows, so a benign
# long run is preserved while an encoded secret anywhere inside is still seen.
_B64_WINDOW_CHARS = 2048
_B64_OVERLAP_CHARS = 344

REDACTED = "[REDACTED]"

SENSITIVE_KEY = re.compile(
    r"token|password|passwd|secret|authorization|cookie|credential"
    r"|api[-_]?key|private[-_]?key",
    re.IGNORECASE,
)

_PEM_LABEL = r"(?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY"
_PEM_COMPLETE = re.compile(
    r"-----BEGIN " + _PEM_LABEL + r"-----.*?-----END " + _PEM_LABEL + r"-----",
    re.IGNORECASE | re.DOTALL,
)
_PEM_BEGIN = re.compile(r"-----BEGIN " + _PEM_LABEL + r"-----", re.IGNORECASE)
_PEM_ORPHAN_END = re.compile(
    r"(?:^[ \t]*[A-Za-z0-9+/=]{1,128}[ \t]*\r?\n){1,"
    + str(MAX_PEM_BODY_LINES)
    + r"}[ \t]*-----END " + _PEM_LABEL + r"-----",
    re.IGNORECASE | re.MULTILINE,
)
_PEM_BODY_LINE = re.compile(r"[ \t]*\r?\n[ \t]*[A-Za-z0-9+/=]{1,128}[ \t]*(?=\r?\n|\Z)")

# High-confidence recognizable secret shapes, used on raw text and on decoded
# base64 blobs. Deliberately narrower than the Fleet pattern set: the final
# evidence validator must not fire on benign prose.
_RECOGNIZABLE_SECRET = re.compile(
    r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA|AIPA|ANPA|ANVA|ABIA|ACCA)[A-Z0-9]{16}\b"
    r"|\b(?:sk-(?:ant-|proj-|or-)?|xai-|ghp_|github_pat_)[A-Za-z0-9_-]{16,}"
    r"|\bBearer\s+[A-Za-z0-9._~+/=-]{16,}",
    re.IGNORECASE,
)

_B64_TOKEN = re.compile(r"(?<![A-Za-z0-9+/=])[A-Za-z0-9+/]{20,}={0,2}(?![A-Za-z0-9+/=])")

# Sensitive key/value pair in JSON-ish text, tolerating escaped quotes (JSON
# embedded in a larger string). The value is replaced, keys and quotes stay.
_SENSITIVE_PAIR_VALUE = re.compile(
    r'(\\?"[^"\\]*(?:token|password|passwd|secret|authorization|cookie|credential'
    r'|api[-_]?key|private[-_]?key)[^"\\]*\\?"\s*:\s*\\?")'
    r'(?:[^"\\]|\\.)*'
    r'(\\?")',
    re.IGNORECASE,
)

# Same pair shape for the final validator: fires only on a live value.
_LIVE_SENSITIVE_PAIR = re.compile(
    r'\\?"[^"\\]*(?:token|password|passwd|secret|authorization|cookie|credential'
    r'|api[-_]?key|private[-_]?key)[^"\\]*\\?"\s*:\s*\\?"(?!\[REDACTED\])(?:[^"\\]|\\.)*\\?"',
    re.IGNORECASE,
)


class GuardError(Exception):
    """Bounded failure signal; never carries fragment content."""


class _DuplicateKey(ValueError):
    pass


class _UnstructuredFallback(Exception):
    pass


def _unique_key_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKey()
        result[key] = value
    return result


def _check_json_depth(text):
    depth = 0
    quoted = escaped = False
    for char in text:
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in "{[":
            depth += 1
            if depth > MAX_JSON_DEPTH:
                raise GuardError("json-depth-bound")
        elif char in "}]":
            depth = max(0, depth - 1)


def _parse_json(text):
    _check_json_depth(text)
    try:
        return json.loads(text, object_pairs_hook=_unique_key_object)
    except _DuplicateKey:
        raise GuardError("duplicate-json-key") from None
    except RecursionError:
        raise GuardError("json-recursion-bound") from None


def _redact_pem_fragments(text):
    text = _PEM_COMPLETE.sub(REDACTED, text)
    # BEGIN without a matching END (truncated source): redact the marker and
    # the bounded base64 body run; when the run hits the cap, assume the key
    # material continues and withhold the rest of the fragment.
    out, pos = [], 0
    for match in _PEM_BEGIN.finditer(text):
        out.append(text[pos:match.start()])
        out.append(REDACTED)
        end, lines = match.end(), 0
        while lines < MAX_PEM_BODY_LINES:
            line = _PEM_BODY_LINE.match(text, end)
            if line is None:
                break
            end = line.end()
            lines += 1
        if _PEM_BODY_LINE.match(text, end):
            end = len(text)
        pos = end
    out.append(text[pos:])
    # Orphan END (truncated head): redact the bounded base64 run before it.
    return _PEM_ORPHAN_END.sub(REDACTED, "".join(out))


def _decoded_window_has_secret(chunk):
    try:
        decoded = base64.b64decode(chunk, validate=True)
    except (binascii.Error, ValueError):
        return False
    if len(decoded) > MAX_B64_DECODE_BYTES:
        raise GuardError("b64-decode-bound")
    return bool(_RECOGNIZABLE_SECRET.search(decoded.decode("utf-8", errors="ignore")))


def _decoded_recognizable_secret(token):
    if len(token) <= MAX_B64_TOKEN_CHARS:
        candidates = [token]
        padding = "=" * (-len(token) % 4)
        if padding:
            candidates.append(token + padding)
        return any(_decoded_window_has_secret(candidate) for candidate in candidates)
    body = token.rstrip("=")
    step = _B64_WINDOW_CHARS - _B64_OVERLAP_CHARS
    for start in range(0, len(body), step):
        chunk = body[start:start + _B64_WINDOW_CHARS]
        chunk += "=" * (-len(chunk) % 4)
        if _decoded_window_has_secret(chunk):
            return True
    return False


def _redact_b64_secrets(text):
    scanned = 0

    def replace(match):
        nonlocal scanned
        scanned += 1
        if scanned > MAX_B64_TOKENS:
            raise GuardError("b64-scan-bound")
        token = match.group(0)
        return REDACTED if _decoded_recognizable_secret(token) else token

    return _B64_TOKEN.sub(replace, text)


def _redact_string(value, redactor):
    redacted = redactor(value)
    if not isinstance(redacted, str):
        raise GuardError("redactor-malformed")
    redacted = _RECOGNIZABLE_SECRET.sub(REDACTED, redacted)
    redacted = _SENSITIVE_PAIR_VALUE.sub(
        lambda match: match.group(1) + REDACTED + match.group(2), redacted)
    return _redact_b64_secrets(_redact_pem_fragments(redacted))


def _redact_raw(text, redactor):
    return _redact_string(text, redactor)


def _redact_structure(node, redactor, depth):
    """Redact values and recognizable secret keys without key collisions."""
    if depth > MAX_JSON_DEPTH:
        raise GuardError("json-depth-bound")
    if isinstance(node, dict):
        changed = False
        result = {}
        occupied = set(node)
        for key, value in node.items():
            guarded_key = _redact_string(key, redactor)
            if guarded_key != key:
                base, suffix = guarded_key, 0
                while guarded_key in occupied or guarded_key in result:
                    suffix += 1
                    guarded_key = base + "#" + str(suffix)
                changed = True
            if SENSITIVE_KEY.search(key):
                result[guarded_key] = REDACTED
                changed = changed or value != REDACTED
            else:
                new_value, sub_changed = _redact_structure(value, redactor, depth + 1)
                result[guarded_key] = new_value
                changed = changed or sub_changed
        return result, changed
    if isinstance(node, list):
        changed = False
        result = []
        for item in node:
            new_item, sub_changed = _redact_structure(item, redactor, depth + 1)
            result.append(new_item)
            changed = changed or sub_changed
        return result, changed
    if isinstance(node, str):
        redacted = _redact_string(node, redactor)
        return redacted, redacted != node
    return node, False


def _redact_json_lines(text, redactor):
    """Structured redaction for lines that are complete JSON values."""
    lines = text.split("\n")
    if len(lines) > MAX_LINES:
        raise GuardError("line-bound")
    for index, line in enumerate(lines):
        core, ending = (line[:-1], "\r") if line.endswith("\r") else (line, "")
        stripped = core.strip()
        if stripped[:1] not in ("{", "["):
            continue
        try:
            parsed = _parse_json(stripped)
        except (ValueError, RecursionError):
            continue
        if not isinstance(parsed, (dict, list)):
            continue
        try:
            redacted, changed = _redact_structure(parsed, redactor, 0)
        except _UnstructuredFallback:
            continue
        if changed:
            lines[index] = json.dumps(redacted, ensure_ascii=False, separators=(",", ":")) + ending
    return "\n".join(lines)


def _redact_embedded_json(text, redactor):
    """Decode complete embedded JSON containers with bounded parse attempts."""
    decoder = json.JSONDecoder(object_pairs_hook=_unique_key_object)
    pieces, copied, cursor, attempts = [], 0, 0, 0
    while cursor < len(text):
        starts = [p for p in (text.find("{", cursor), text.find("[", cursor)) if p >= 0]
        if not starts:
            break
        start = min(starts)
        attempts += 1
        if attempts > MAX_B64_TOKENS:
            raise GuardError("json-parse-attempt-bound")
        try:
            parsed, consumed = decoder.raw_decode(text[start:])
        except (_DuplicateKey, RecursionError):
            raise GuardError("embedded-json-parser-bound") from None
        except ValueError:
            cursor = start + 1
            continue
        if not isinstance(parsed, (dict, list)):
            cursor = start + max(consumed, 1)
            continue
        segment = text[start:start + consumed]
        _check_json_depth(segment)
        transformed, changed = _redact_structure(parsed, redactor, 0)
        pieces.append(text[copied:start])
        pieces.append(json.dumps(transformed, ensure_ascii=False) if changed else segment)
        copied = cursor = start + consumed
    pieces.append(text[copied:])
    return "".join(pieces)


def guard_text(value, redactor):
    """Return the guarded text, or raise GuardError so the caller withholds it.

    ``redactor`` is the Fleet ``redact_sensitive_value`` callable; it stays the
    first-line text redactor for raw runs and individual JSON string leaves.
    """
    if not isinstance(value, str):
        raise GuardError("non-string")
    if len(value.encode("utf-8")) > MAX_TEXT_BYTES:
        raise GuardError("oversize")
    if not callable(redactor):
        raise GuardError("redactor-unavailable")
    stripped = value.strip()
    if stripped[:1] in ("{", "["):
        try:
            parsed = _parse_json(stripped)
        except (ValueError, RecursionError):
            parsed = None
        if isinstance(parsed, (dict, list)):
            try:
                redacted, changed = _redact_structure(parsed, redactor, 0)
            except _UnstructuredFallback:
                pass
            else:
                if not changed:
                    return _redact_raw(value, redactor)
                return json.dumps(redacted, ensure_ascii=False, separators=(",", ":"))
    return _redact_raw(_redact_embedded_json(value, redactor), redactor)


def contains_recognizable_secret(text):
    """Fail-closed check of the final serialized evidence for leftover secrets."""
    if not isinstance(text, str):
        return True
    if len(text.encode("utf-8")) > MAX_TEXT_BYTES:
        return True
    try:
        if _PEM_BEGIN.search(text) or re.search(r"-----END " + _PEM_LABEL + r"-----", text, re.IGNORECASE):
            return True
        if _RECOGNIZABLE_SECRET.search(text):
            return True
        if _LIVE_SENSITIVE_PAIR.search(text):
            return True
        return _b64_secret_present(text)
    except GuardError:
        return True


def _b64_secret_present(text):
    scanned = 0
    for match in _B64_TOKEN.finditer(text):
        scanned += 1
        if scanned > MAX_B64_TOKENS:
            raise GuardError("b64-scan-bound")
        if _decoded_recognizable_secret(match.group(0)):
            return True
    return False
