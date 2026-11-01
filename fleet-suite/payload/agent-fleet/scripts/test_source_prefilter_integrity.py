"""Pure read-only integrity test for the pinned TypeScript vendor bundle.

Checks the exact raw SHA-256 of the bundled vendor bytes and the exact parser
pin used by source_references.cjs. The test never writes, repairs or
regenerates the artifact, and it makes no global line-ending assertion: the
pinned bundle intentionally contains a small number of CRLF sequences inside
JavaScript template literals, so the exact raw digest is authoritative.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
import unittest

HERE = Path(__file__).resolve().parent
VENDOR_TYPESCRIPT = HERE / "vendor" / "typescript" / "typescript.js"
SOURCE_REFERENCES = HERE / "source_references.cjs"
PINNED_SHA256 = "3ae902c92cc44dace175c0e69e13a4b0899f6983c6121d76b9ab8dd5795e7675"


class SourcePrefilterIntegrityTests(unittest.TestCase):
    def test_vendor_typescript_raw_sha256(self):
        self.assertTrue(VENDOR_TYPESCRIPT.is_file(), f"missing vendor file: {VENDOR_TYPESCRIPT}")
        digest = hashlib.sha256(VENDOR_TYPESCRIPT.read_bytes()).hexdigest()
        self.assertEqual(
            digest,
            PINNED_SHA256,
            f"pinned vendor sha256 mismatch: got {digest}, expected {PINNED_SHA256}",
        )

    def test_parser_pin_is_exact(self):
        self.assertTrue(SOURCE_REFERENCES.is_file(), f"missing parser file: {SOURCE_REFERENCES}")
        self.assertIn(
            PINNED_SHA256,
            SOURCE_REFERENCES.read_text(encoding="utf-8"),
            f"source_references.cjs does not pin {PINNED_SHA256}",
        )


if __name__ == "__main__":
    unittest.main()
