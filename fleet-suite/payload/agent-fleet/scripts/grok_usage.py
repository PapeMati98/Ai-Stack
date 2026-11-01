"""Pure native Grok run-usage projection.

Maps a top-level native envelope to Fleet usage keys. Does not price tokens,
infer fresh-input semantics, or read nested modelUsage mirrors.
"""

_TOKEN_FIELDS = (
    ("inputTokens", "input_tokens"),
    ("outputTokens", "output_tokens"),
    ("cachedReadTokens", "cache_read_tokens"),
    ("cacheCreationTokens", "cache_write_tokens"),
    ("reasoningTokens", "reasoning_tokens"),
    ("totalTokens", "total_tokens"),
)


def _valid_counter(value):
    return type(value) is int and value >= 0


def _unknown_projection():
    return {
        "status": "unknown",
        "source": "provider-json",
        "confidence": 0.0,
        "adapter": "grok",
        "input_tokens": None,
        "output_tokens": None,
        "cache_read_tokens": None,
        "cache_write_tokens": None,
        "reasoning_tokens": None,
        "total_tokens": None,
        "fresh_input_tokens": None,
        "cost_usd": None,
        "total_derived": False,
        "fresh_input_semantics": "unverified",
    }


def _recognized_envelope(value):
    if type(value) is not dict:
        return False
    session_id = value.get("sessionId")
    model_id = value.get("modelId")
    if type(session_id) is not str or session_id == "":
        return False
    if type(model_id) is not str or not model_id.startswith("grok-"):
        return False
    return True


def extract_native_grok_usage(value):
    """Project one native Grok usage envelope, or None if it is not native."""
    if not _recognized_envelope(value):
        return None

    usage = value.get("usage")
    if type(usage) is not dict:
        return _unknown_projection()

    projected = _unknown_projection()
    total_key_present = "totalTokens" in usage
    for source_name, fleet_name in _TOKEN_FIELDS:
        if source_name not in usage:
            continue
        raw = usage[source_name]
        if _valid_counter(raw):
            projected[fleet_name] = raw

    input_tokens = projected["input_tokens"]
    output_tokens = projected["output_tokens"]
    both_io = input_tokens is not None and output_tokens is not None
    reported_total = projected["total_tokens"]
    if (
        total_key_present
        and reported_total is not None
        and both_io
        and reported_total < input_tokens + output_tokens
    ):
        projected["total_tokens"] = None
    elif (not total_key_present) and both_io:
        projected["total_tokens"] = input_tokens + output_tokens
        projected["total_derived"] = True

    known = any(
        projected[name] is not None
        for _, name in _TOKEN_FIELDS
    )
    if known and projected["total_derived"]:
        projected["status"] = "known"
        projected["confidence"] = 0.8
    elif known:
        projected["status"] = "known"
        projected["confidence"] = 1.0
    return projected
