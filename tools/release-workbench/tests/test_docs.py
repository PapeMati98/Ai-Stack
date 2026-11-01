"""Offline documentation checks, including execution of the documented journey.

Only synthetic Git blobs are exported. No native provider, Office, Hub or RSI
process is invoked. Passing these checks supplies no native/public acceptance.
"""
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import unquote, urlsplit

from release_workbench.__main__ import main, parser
from release_workbench.configuration import validate_config
from release_workbench.exporter import validate_manifest
from release_workbench.privacy import validate_policy
from release_workbench.provenance import audit_provenance

REPO_ROOT = Path(__file__).resolve().parents[1]
SCOPE_DOCS = ("README.md", "docs/QUICKSTART.md", "docs/ARCHITECTURE.md",
              "docs/SECURITY.md", "docs/SUPPORT.md")
ESSENTIAL_NOTICES = ("private candidate", "no license grant or public publication approval",
                     "native integration evidence is pending")
_LINK_RE = re.compile(r"\[[^\]]*\]\((<[^>]+>|[^)\s]+)\)")


def extract_local_links(text):
    """Check inline file links without fetching any external resource."""
    text = re.sub(r"```[^\n]*\n.*?```", "", text, flags=re.DOTALL)
    targets = []
    for match in _LINK_RE.finditer(text):
        target = match.group(1).strip("<>")
        parsed = urlsplit(target)
        if not parsed.scheme and not parsed.netloc and parsed.path:
            targets.append(unquote(parsed.path))
    return targets


def find_missing_links(doc_path):
    doc_path = Path(doc_path)
    return [(target, str(doc_path.parent / target))
            for target in extract_local_links(doc_path.read_text(encoding="utf-8"))
            if not (doc_path.parent / target).is_file()]


def missing_notices(text, phrases=ESSENTIAL_NOTICES):
    normalized = " ".join(text.casefold().split())
    return [phrase for phrase in phrases if phrase.casefold() not in normalized]


class DocumentationTests(unittest.TestCase):
    def test_local_links_and_essential_gate_notices(self):
        for relative in SCOPE_DOCS:
            with self.subTest(document=relative):
                doc = REPO_ROOT / relative
                self.assertTrue(doc.is_file())
                self.assertEqual(find_missing_links(doc), [])
                self.assertEqual(missing_notices(doc.read_text(encoding="utf-8")), [])

    def test_checker_detects_missing_link_and_missing_gate(self):
        with tempfile.TemporaryDirectory(prefix="docs-check-", dir=REPO_ROOT) as scratch:
            root = Path(scratch)
            (root / "a file.md").write_text("Synthetic target.\n", encoding="utf-8")
            doc = root / "draft.md"
            doc.write_text("[ok](a%20file.md#section) [space](<a file.md>) "
                           "[gone](missing.md) [external](https://example.invalid/) "
                           "[anchor](#draft)\n", encoding="utf-8")
            self.assertEqual([row[0] for row in find_missing_links(doc)], ["missing.md"])
            self.assertEqual(missing_notices(doc.read_text(encoding="utf-8")),
                             list(ESSENTIAL_NOTICES))
            self.assertEqual(missing_notices("\n".join(ESSENTIAL_NOTICES)), [])

    def test_documented_command_flags_match_actual_parser(self):
        text = (REPO_ROOT / "docs/QUICKSTART.md").read_text(encoding="utf-8")
        blocks = re.findall(r"```console\n(.*?)```", text, re.DOTALL)
        seen = set()
        for block in blocks:
            for line in block.strip().splitlines():
                arguments = shlex.split(line)
                self.assertEqual(arguments[:4], ["python", "-B", "-m", "release_workbench"])
                parsed = parser().parse_args(arguments[4:])
                seen.add(parsed.command)
        self.assertEqual(seen, {"export", "verify", "scan", "config-check", "rights-check",
                                "bundle-check", "demo", "native-uat"})

    def test_actual_module_help_lists_documented_commands(self):
        process = subprocess.run([sys.executable, "-B", "-m", "release_workbench", "--help"],
                                 cwd=REPO_ROOT, capture_output=True, timeout=30, shell=False,
                                 creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        self.assertEqual(process.returncode, 0, process.stderr)
        for command in (b"export", b"verify", b"scan", b"config-check", b"rights-check",
                        b"bundle-check", b"demo", b"native-uat"):
            self.assertIn(command, process.stdout)

    def test_execute_quickstart_against_real_cli_and_synthetic_git_blobs(self):
        text = (REPO_ROOT / "docs/QUICKSTART.md").read_text(encoding="utf-8")
        blocks = re.findall(r"```python\n(.*?)```", text, re.DOTALL)
        self.assertEqual(len(blocks), 1, "Keep the executable journey in one complete block")
        namespace = {"__name__": "documented_quickstart"}
        # Patch only the working-directory lookup. CLI/library/process calls remain real.
        with patch.object(Path, "cwd", return_value=REPO_ROOT), redirect_stdout(io.StringIO()):
            exec(compile(blocks[0], "docs/QUICKSTART.md", "exec"), namespace)
        results = namespace["results"]
        self.assertEqual(set(results), {"demo", "export", "verify", "scan", "config", "rights",
                                        "bundle", "collision", "tampered_verify", "tampered_bundle"})
        self.assertEqual(results["demo"]["native_uat"], "NOT_RUN")
        self.assertEqual(results["demo"]["product_acceptance"], "UNKNOWN")
        self.assertEqual(results["rights"]["status"], "OPEN")
        self.assertEqual(results["bundle"]["status"], "PENDING")
        self.assertFalse(results["bundle"]["public_ready"])
        for negative in ("collision", "tampered_verify", "tampered_bundle"):
            self.assertEqual(results[negative]["status"], "FAIL")

    def test_rejected_old_documentation_schemas_stay_invalid(self):
        with tempfile.TemporaryDirectory(prefix="docs-schema-", dir=REPO_ROOT) as scratch:
            root = Path(scratch)
            with self.assertRaises(ValueError):
                validate_manifest({"schema_version": 1, "commit": "0" * 40,
                                   "files": [{"path": "README.md", "blob_sha": "ab" * 20}]})
            with self.assertRaises(ValueError):
                validate_policy({"extra_literals": ["synthetic-marker"]})
            with self.assertRaises(ValueError):
                validate_config({"paths": {"out": "build/out"}, "credentials": {}}, owned_root=root)
            manifest = {"version": 1, "sources": [{"id": "synthetic", "repo": str(root),
                        "commit": "0" * 40, "files": [{"path": "fixture.md", "target": "fixture.md",
                        "sha256": "0" * 64}]}]}
            with self.assertRaises(ValueError):
                audit_provenance(manifest, [{"path": "fixture.md", "source": "synthetic",
                                             "license": "UNKNOWN", "owner": "pending"}])

    def test_invalid_config_cli_rejects_without_echoing_synthetic_value(self):
        with tempfile.TemporaryDirectory(prefix="docs-negative-", dir=REPO_ROOT) as scratch:
            root = Path(scratch)
            config = root / "invalid-config.json"
            marker = "synthetic-unrecognized-input-783ab"
            config.write_text(json.dumps({"paths": {"out": marker}}), encoding="utf-8")
            stream = io.StringIO()
            with redirect_stdout(stream):
                code = main(["config-check", "--config", str(config), "--owned-root", str(root)])
            self.assertEqual(code, 1)
            self.assertEqual(json.loads(stream.getvalue())["status"], "FAIL")
            self.assertNotIn(marker, stream.getvalue())


if __name__ == "__main__":
    unittest.main()
