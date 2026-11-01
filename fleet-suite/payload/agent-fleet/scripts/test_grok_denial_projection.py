"""Synthetic checks: optional permission diagnostics survive the run audit safely."""
import json
import unittest

import fleet


def envelope(denials=None, *, include=True):
    meta = {"transport": "native-cli-acp-stdio", "tool_events": [], "tool_calls": 0,
            "mcp_catalog_observed": False, "windows_os_sandbox_enforced": False,
            "prompt_count": 1, "thought_chunks_discarded": 0,
            "permission_requests_approved": 0, "permission_requests_denied": 2,
            "reverse_requests_denied": 0, "ambient_mcp_servers": 0}
    if include:
        meta["permission_denials"] = denials
    return json.dumps({"_fleet": meta})


class GrokDenialProjectionTests(unittest.TestCase):
    def test_safe_diagnostics_preserved_and_payload_fields_excluded(self):
        row = {"reason": "writes_not_allowed_in_mode", "variant": "Write", "tool_kind": "edit",
               "path_shape": "project_relative", "project_relative_path": "project/synthetic.py",
               "raw_input_keys": ["content", "file_path", "variant"], "option_kinds": ["allow_once", "reject_once"],
               "content": "DO_NOT_COPY_FILE_CONTENT", "command": "DO_NOT_COPY_COMMAND"}
        audit = fleet.grok_provider_audit(envelope([row]))
        result = audit["permission_denials"][0]
        self.assertEqual(result["project_relative_path"], "project/synthetic.py")
        self.assertEqual(result["reason"], row["reason"])
        self.assertNotIn("content", result)
        self.assertNotIn("command", result)
        self.assertNotIn("DO_NOT_COPY", json.dumps(audit))
        self.assertEqual(audit["permission_denials_status"], "observed")
        self.assertEqual(audit["permission_requests_denied"], 2)

    def test_legacy_and_malformed_metadata_do_not_invent_observation(self):
        for text, status in [(envelope(include=False), "unavailable"), (envelope("bad"), "malformed")]:
            audit = fleet.grok_provider_audit(text)
            self.assertEqual(audit["permission_denials"], [])
            self.assertEqual(audit["permission_denials_status"], status)
            self.assertEqual(audit["permission_requests_denied"], 2)

    def test_details_bounded_without_changing_aggregate_count(self):
        audit = fleet.grok_provider_audit(envelope([{"reason": "unknown"}] * 21))
        self.assertEqual(len(audit["permission_denials"]), 20)
        self.assertEqual(audit["permission_denials_status"], "partial")
        audit = fleet.grok_provider_audit(envelope([None, {"reason": "x" * 81}, {"raw_input_keys": ["x"] * 21}]))
        self.assertEqual(audit["permission_denials"], [])
        self.assertEqual(audit["permission_denials_status"], "partial")

    def test_unsafe_paths_and_unknown_payloads_not_copied(self):
        for path in ["C:/restricted/file", "//server/restricted", "project/../restricted", "project/file:stream", "project\\file", "project/a\x00b"]:
            audit = fleet.grok_provider_audit(envelope([{"reason": "unknown", "project_relative_path": path,
                                                      "environment": {"secret": "DO_NOT_COPY"}}]))
            self.assertNotIn("project_relative_path", audit["permission_denials"][0])
            self.assertNotIn("environment", audit["permission_denials"][0])
            self.assertEqual(audit["permission_denials_status"], "partial")
