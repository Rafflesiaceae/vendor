#!/usr/bin/env python3
"""Tests for the local autoversion implementation."""

import io
import tempfile
import unittest

from contextlib import redirect_stdout
from pathlib import Path

import autoversion


class AutoversionTests(unittest.TestCase):
    """Verify compatibility with oat's autoversion behavior."""

    def test_xxh64_reference_vectors(self):
        """The bundled hasher matches canonical XXH64 seed-zero vectors."""
        vectors = {
            b"": 0xEF46DB3751D8E999,
            b"a": 0xD24EC4F1A98C6E5B,
            b"abc": 0x44BC2CF5AD770999,
            b"message digest": 0x066ED728FCEEB3BE,
            b"The quick brown fox jumps over the lazy dog\n": 0xC955CF3287BCA7E4,
        }
        for contents, expected in vectors.items():
            with self.subTest(contents=contents):
                self.assertEqual(autoversion.xxh64(contents), expected)

    def test_updates_then_recognizes_file_as_up_to_date(self):
        """A content change causes exactly one version bump."""
        with tempfile.TemporaryDirectory(prefix="autoversion-") as directory:
            path = Path(directory) / "sample.py"
            path.write_bytes(
                b"#!/usr/bin/env python3\r\n"
                b"# sample v1.9 (2020-01-02) (old) ->\r\n"
                b"print('hello')\r\n"
            )

            with redirect_stdout(io.StringIO()):
                self.assertTrue(autoversion.autoversion_file(path))
            updated = path.read_bytes()

            # Like bufio.Scanner, autoversion normalizes scanned lines to LF.
            payload = b"#!/usr/bin/env python3\nprint('hello')\n"
            checksum = f"{autoversion.xxh64(payload):x}".encode("ascii")
            expected_line = b"# sample v1.10 ("
            self.assertIn(expected_line, updated)
            self.assertIn(b") (" + checksum + b") ->\n", updated)

            with redirect_stdout(io.StringIO()):
                self.assertFalse(autoversion.autoversion_file(path))
            self.assertEqual(path.read_bytes(), updated)

    def test_rejects_multiple_version_lines_without_writing(self):
        """Ambiguous metadata leaves the source file untouched."""
        with tempfile.TemporaryDirectory(prefix="autoversion-") as directory:
            path = Path(directory) / "sample.py"
            original = (
                b"# first v1.0 (2020-01-01) (0)\n"
                b"# second v2.0 (2020-01-01) (0)\n"
            )
            path.write_bytes(original)

            with self.assertRaisesRegex(ValueError, "more than one version"):
                autoversion.autoversion_file(path)
            self.assertEqual(path.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
