"""Minimal Telegram HTTPS Bot API client (stdlib only, no network in tests).

- Transport is an INJECTED opener: ``opener(url, body, headers, timeout)``
  returns ``(status_code, final_url, body_bytes)``. Tests inject a fake;
  production wires the provided no-redirect urllib opener.
- Only allowlisted methods. Redirects refused. Responses bounded.
- The token comes from a CALLER-INJECTED environment mapping under the
  key ``TELEGRAM_BOT_TOKEN`` -- never from argv, never logged, never
  stored. Every exception is redacted (no token, no URL, no body echo).
"""

from __future__ import annotations

import json
from typing import Any, Callable, Mapping

ALLOWED_METHODS = frozenset(
    {
        "sendMessage",
        "editMessageText",
        "answerCallbackQuery",
        "getMe",
        "getWebhookInfo",
        "getUpdates",
    }
)
# Webhook takeover is forbidden: never setWebhook / deleteWebhook.

TOKEN_ENV_KEY = "TELEGRAM_BOT_TOKEN"
DEFAULT_HOST = "api.telegram.org"
DEFAULT_TIMEOUT = 15.0
MAX_RESPONSE_BYTES = 1_048_576


class BotApiError(Exception):
    """Redacted client error: status/reason only, never secrets or bodies."""


# opener(url, body, headers, timeout) -> (status, final_url, body_bytes)
Opener = Callable[[str, bytes, dict, float], tuple]


def _redacted_error(reason: str, status: Any = None) -> BotApiError:
    if status is None:
        return BotApiError("telegram api failed: %s" % reason)
    return BotApiError(
        "telegram api failed: %s (http %s)" % (reason, status)
    )


def urllib_no_redirect_opener(
    url: str, body: bytes, headers: dict, timeout: float
) -> tuple:
    """Default opener. Redirects raise; callers must pass this explicitly."""
    import urllib.request

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(
            self, req: Any, fp: Any, code: Any, msg: Any,
            headers: Any, newurl: Any,
        ) -> Any:
            raise _redacted_error("redirect refused")

    opener = urllib.request.build_opener(_NoRedirect())
    request = urllib.request.Request(
        url, data=body, headers=dict(headers), method="POST"
    )
    try:
        with opener.open(request, timeout=timeout) as resp:
            status = int(resp.status)
            final_url = str(resp.geturl())
            raw = resp.read(MAX_RESPONSE_BYTES + 1)
    except BotApiError:
        raise
    except Exception as exc:
        raise _redacted_error(type(exc).__name__) from None
    if len(raw) > MAX_RESPONSE_BYTES:
        raise _redacted_error("response too large", status)
    return status, final_url, raw


class BotApi:
    def __init__(
        self,
        env: Mapping[str, str],
        opener: Opener,
        host: str = DEFAULT_HOST,
        timeout: float = DEFAULT_TIMEOUT,
        max_bytes: int = MAX_RESPONSE_BYTES,
    ) -> None:
        token = env.get(TOKEN_ENV_KEY) if env is not None else None
        if not token:
            raise _redacted_error("missing token")
        self._token = str(token)
        self._opener = opener
        self._host = str(host)
        self._timeout = float(timeout)
        self._max_bytes = int(max_bytes)

    def call(self, method: str, params: dict) -> dict:
        if method not in ALLOWED_METHODS:
            raise _redacted_error("method not allowed")
        if not isinstance(params, dict):
            raise _redacted_error("bad params")
        url = "https://%s/bot%s/%s" % (self._host, self._token, method)
        try:
            body = json.dumps(params, ensure_ascii=False).encode("utf-8")
        except Exception:
            raise _redacted_error("bad params") from None
        try:
            status, final_url, raw = self._opener(
                url, body, {"Content-Type": "application/json"}, self._timeout
            )
        except BotApiError:
            raise
        except Exception as exc:
            raise _redacted_error(type(exc).__name__) from None
        if final_url != url:
            raise _redacted_error("redirect refused", status)
        if status != 200:
            raise _redacted_error("bad status", status)
        if len(raw) > self._max_bytes:
            raise _redacted_error("response too large", status)
        try:
            doc = json.loads(raw.decode("utf-8"))
        except Exception:
            raise _redacted_error("bad response", status) from None
        if not isinstance(doc, dict) or doc.get("ok") is not True:
            code = None
            if isinstance(doc, dict) and isinstance(
                doc.get("error_code"), int
            ):
                code = doc.get("error_code")
            # Only the numeric code: descriptions may echo message text.
            raise _redacted_error("not ok", code)
        result = doc.get("result")
        if method == "getUpdates":
            if not isinstance(result, list):
                raise _redacted_error("bad response", status)
            return result
        return result if isinstance(result, dict) else {"value": result}

    def get_me(self) -> dict:
        return self.call("getMe", {})

    def get_webhook_info(self) -> dict:
        return self.call('getWebhookInfo', {})

    def get_updates(
        self,
        offset: Any = None,
        timeout: float = 0.0,
        limit: int = 100,
    ) -> list:
        timeout = min(max(float(timeout), 0.0), 25.0)
        limit = min(max(int(limit), 1), 100)
        params: dict[str, Any] = {
            "timeout": int(timeout),
            "limit": limit,
            "allowed_updates": ["message", "edited_message", "callback_query"],
        }
        if offset is not None:
            params["offset"] = int(offset)
        result = self.call("getUpdates", params)
        return result if isinstance(result, list) else []

    def send_text(
        self, chat_id: int, text: str, reply_markup: Any = None
    ) -> dict:
        from openclaw_control import models

        if not isinstance(text, str) or text == "":
            raise _redacted_error("empty text")
        if len(text) > models.TELEGRAM_TEXT_LIMIT:
            raise _redacted_error("text too large")
        params: dict[str, Any] = {"chat_id": int(chat_id), "text": text}
        if reply_markup is not None:
            params["reply_markup"] = reply_markup
        return self.call("sendMessage", params)

    def edit_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        reply_markup: Any = None,
    ) -> dict:
        from openclaw_control import models

        if not isinstance(text, str) or text == "":
            raise _redacted_error("empty text")
        if len(text) > models.TELEGRAM_TEXT_LIMIT:
            raise _redacted_error("text too large")
        params: dict[str, Any] = {
            "chat_id": int(chat_id),
            "message_id": int(message_id),
            "text": text,
        }
        if reply_markup is not None:
            params["reply_markup"] = reply_markup
        return self.call("editMessageText", params)

    def answer_callback(
        self, callback_query_id: str, text: str = ""
    ) -> dict:
        params: dict[str, Any] = {"callback_query_id": callback_query_id}
        if text:
            params["text"] = text[:200]
        return self.call("answerCallbackQuery", params)
