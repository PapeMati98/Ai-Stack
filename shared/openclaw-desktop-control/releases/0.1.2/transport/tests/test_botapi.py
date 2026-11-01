"""Bot API client: allowlist, redirects, bounds, env token, redaction."""

import json
import unittest

from openclaw_control import models
from openclaw_control.botapi import BotApi, BotApiError

TOKEN = "SECRET-TOKEN-123"


def _ok_opener(capture: dict):
    def opener(url: str, body: bytes, headers: dict, timeout: float):
        capture["url"] = url
        capture["body"] = body
        capture["headers"] = headers
        return 200, url, json.dumps(
            {"ok": True, "result": {"message_id": 7}}
        ).encode("utf-8")

    return opener


class BotApiTest(unittest.TestCase):
    def test_allowed_method_ok(self) -> None:
        seen: dict = {}
        api = BotApi({"TELEGRAM_BOT_TOKEN": TOKEN}, _ok_opener(seen))
        res = api.send_text(222, "cześć 🙂")
        self.assertEqual(res, {"message_id": 7})
        self.assertIn("/sendMessage", seen["url"])
        body = json.loads(seen["body"].decode("utf-8"))
        self.assertEqual(body["text"], "cześć 🙂")

    def test_get_me_and_get_updates_allowed(self) -> None:
        seen: dict = {}

        def opener(url: str, body: bytes, headers: dict, timeout: float):
            seen["url"] = url
            method = url.rsplit("/", 1)[-1]
            if method == "getMe":
                return 200, url, json.dumps(
                    {"ok": True, "result": {"id": 555, "is_bot": True}}
                ).encode("utf-8")
            return 200, url, json.dumps(
                {"ok": True, "result": [{"update_id": 1}]}
            ).encode("utf-8")

        api = BotApi({"TELEGRAM_BOT_TOKEN": TOKEN}, opener)
        me = api.get_me()
        self.assertEqual(me["id"], 555)
        updates = api.get_updates(offset=1, timeout=0, limit=10)
        self.assertEqual(updates[0]["update_id"], 1)
        self.assertNotIn("setWebhook", seen["url"])

    def test_disallowed_method_refused(self) -> None:
        seen: dict = {}
        api = BotApi({"TELEGRAM_BOT_TOKEN": TOKEN}, _ok_opener(seen))
        with self.assertRaises(BotApiError):
            api.call("setWebhook", {})
        with self.assertRaises(BotApiError):
            api.call("deleteWebhook", {})
        with self.assertRaises(BotApiError):
            api.call("sendPhoto", {})
        self.assertEqual(seen, {})

    def test_redirect_refused_and_redacted(self) -> None:
        def opener(url: str, body: bytes, headers: dict, timeout: float):
            return 200, url + "?moved", b'{"ok":true,"result":{}}'

        api = BotApi({"TELEGRAM_BOT_TOKEN": TOKEN}, opener)
        try:
            api.send_text(1, "hej")
            self.fail("expected redirect refusal")
        except BotApiError as exc:
            self.assertNotIn(TOKEN, str(exc))
            self.assertNotIn("api.telegram", str(exc))

    def test_oversize_bounded(self) -> None:
        big = b"x" * (2 * 1024 * 1024)

        def opener(url: str, body: bytes, headers: dict, timeout: float):
            return 200, url, big

        api = BotApi({"TELEGRAM_BOT_TOKEN": TOKEN}, opener)
        with self.assertRaises(BotApiError):
            api.send_text(1, "hej")

    def test_error_code_only_no_description_echo(self) -> None:
        raw = json.dumps({
            "ok": False,
            "error_code": 400,
            "description": "Bad Request: message text SECRET-LEAK",
        }).encode("utf-8")

        def opener(url: str, body: bytes, headers: dict, timeout: float):
            return 200, url, raw

        api = BotApi({"TELEGRAM_BOT_TOKEN": TOKEN}, opener)
        try:
            api.send_text(1, "hej")
            self.fail("expected not-ok")
        except BotApiError as exc:
            self.assertNotIn("SECRET-LEAK", str(exc))
            self.assertNotIn(TOKEN, str(exc))
            self.assertIn("400", str(exc))

    def test_transport_exception_redacted(self) -> None:
        def opener(url: str, body: bytes, headers: dict, timeout: float):
            raise RuntimeError("socket down")

        api = BotApi({"TELEGRAM_BOT_TOKEN": TOKEN}, opener)
        try:
            api.send_text(1, "hej")
            self.fail("expected failure")
        except BotApiError as exc:
            self.assertNotIn(TOKEN, str(exc))

    def test_missing_token(self) -> None:
        with self.assertRaises(BotApiError):
            BotApi({}, _ok_opener({}))

    def test_token_never_from_argv(self) -> None:
        # The Telegram bot token must never be accepted via argv. The
        # per-dispatch worker --claim-token is coordination state, not
        # a credential, and is the only option containing "token".
        from openclaw_control.cli import build_parser

        parser = build_parser()
        found = []
        for action in parser._actions:
            found.extend(getattr(action, "option_strings", []))
        for sub in parser._subparsers._group_actions:
            for choice in sub.choices.values():
                for action in choice._actions:
                    found.extend(
                        getattr(action, "option_strings", [])
                    )
        lowered = [opt.lower() for opt in found]
        self.assertNotIn("--token", lowered)
        for opt in lowered:
            self.assertNotIn("bot-token", opt)
            self.assertNotIn("api-token", opt)
            self.assertNotIn("secret", opt)
            self.assertNotIn("password", opt)

    def test_send_rejects_oversize_text_never_truncates(self) -> None:
        seen: dict = {}
        api = BotApi({"TELEGRAM_BOT_TOKEN": TOKEN}, _ok_opener(seen))
        with self.assertRaises(BotApiError):
            api.send_text(1, "y" * (models.TELEGRAM_TEXT_LIMIT + 1))
        self.assertEqual(seen, {})


if __name__ == "__main__":
    unittest.main()
