"""Focused prompt-contract regression tests for the OpenCode Beam profile."""

import json
import unittest
from pathlib import Path

from fleet import build_worker_prompt


class BeamToolContractTests(unittest.TestCase):
    def setUp(self):
        self.schema = {
            "type": "object",
            "required": ["status", "summary"],
            "properties": {
                "status": {"type": "string"},
                "summary": {"type": "string"},
            },
        }

    def prompt(self, *, agent_id="opencode-beam", mode="implement", adapter_id="opencode"):
        return build_worker_prompt(
            "Implement the bounded change.",
            agent_id=agent_id,
            adapter_id=adapter_id,
            mode=mode,
            workspace=Path("D:/synthetic/beam-root"),
            kind="implementation",
            complexity="low",
            native_subagent_limit=0,
            enable_web=False,
            knowledge_snapshot=None,
            schema=self.schema,
        )

    def test_beam_implement_names_exposed_file_tools_and_parent_verification(self):
        prompt = self.prompt()

        self.assertIn(
            "Use only the actually exposed native file tools: read, glob, grep, edit, and write.",
            prompt,
        )
        self.assertIn("The parent runner executes commands and tests.", prompt)
        self.assertIn("do not run commands or tests because the parent performs verification", prompt)
        self.assertNotIn("may run focused tests", prompt)

    def test_beam_read_only_modes_deny_edit_and_write(self):
        for mode in ("review", "advisory"):
            with self.subTest(mode=mode):
                prompt = self.prompt(mode=mode)
                self.assertIn(
                    "Use only the actually exposed native read-only file tools: read, glob, and grep.",
                    prompt,
                )
                self.assertIn("Edit and write are denied in this mode.", prompt)
                self.assertNotIn(
                    "actually exposed native file tools: read, glob, grep, edit, and write",
                    prompt,
                )

    def test_beam_runtime_policy_stays_denied_and_final_is_assistant_text(self):
        for mode in ("implement", "review", "advisory"):
            with self.subTest(mode=mode):
                prompt = self.prompt(mode=mode)
                self.assertIn(
                    "Shell/bash, command execution, tests, network, LSP, subagents, skills, MCP, web, questions, and sharing are denied by runtime policy.",
                    prompt,
                )
                self.assertIn("Never call final, invalid, submit, bash, or shell tools.", prompt)
                self.assertIn(
                    "Return exactly one filled JSON value matching the required schema in the final assistant text; the schema is not a tool definition or signature.",
                    prompt,
                )
                self.assertIn(
                    "Handoff schema: "
                    + json.dumps(self.schema, ensure_ascii=True, separators=(",", ":")),
                    prompt,
                )

    def test_non_beam_opencode_review_keeps_existing_security_prompt(self):
        prompt = self.prompt(agent_id="opencode-reviewer", mode="review")

        self.assertIn("OpenCode security-review rules:", prompt)
        self.assertIn(
            "Native policy allows read, glob, grep, and list only. Edit, write, shell, LSP, subagents, skills, MCP, web, questions, external directories, and sharing are denied.",
            prompt,
        )
        self.assertNotIn("OpenCode Beam native-tool rules:", prompt)
        self.assertNotIn("filled JSON value matching the required schema in the final assistant text", prompt)


if __name__ == "__main__":
    unittest.main()
