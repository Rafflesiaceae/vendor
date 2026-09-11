#!/usr/bin/env python3
"""Tests for the local autoversion implementation."""

import hashlib
import io
import tempfile
import unittest

from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import autoversion


class AutoversionTests(unittest.TestCase):
    """Verify the version-line update behavior."""

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
            checksum = hashlib.sha256(payload).hexdigest().encode("ascii")
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

    def test_main_always_targets_vendor_file(self):
        """The executable needs no path argument to update vendor.py."""
        with tempfile.TemporaryDirectory(prefix="autoversion-") as directory:
            path = Path(directory) / "vendor.py"
            path.write_bytes(b"# vendor v1.0 (2020-01-01) (old)\ncontents\n")

            with mock.patch.object(autoversion, "VENDOR_PATH", path):
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(autoversion.main(), 0)

            self.assertIn(b"# vendor v1.1 (", path.read_bytes())


if __name__ == "__main__":
    unittest.main()
