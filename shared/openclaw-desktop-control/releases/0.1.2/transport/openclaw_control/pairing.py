"""Local expiring one-use pairing nonces.

The parent shows the nonce to the owner out-of-band. The owner returns
it; the parent redeems it here with the observed (sender, private chat).
One use, bounded TTL, private chat only. No tokens or credentials are
stored -- only the random nonce and its redemption record.
"""

from __future__ import annotations

from typing import Any

from openclaw_control import models


def issue(store: Any, now: float, ttl: float = models.PAIRING_TTL) -> str:
    """Issue one pairing nonce (returned to the parent, never logged)."""
    return store.issue_nonce(now, ttl)


def redeem(
    store: Any,
    nonce: str,
    sender_id: int,
    chat_id: int,
    chat_type: str,
    now: float,
) -> dict:
    """Redeem a nonce for a verified private sender. Single-use."""
    return store.redeem_nonce(nonce, sender_id, chat_id, chat_type, now)
