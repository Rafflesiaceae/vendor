#!/usr/bin/env python3
"""Integration tests for vendoring local C library repositories."""

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

import vendor as vendor_module


VENDOR_SCRIPT = Path(__file__).resolve().with_name("vendor.py")

# Fix commit metadata and disable the editor so the fixture history is
# deterministic and vendor.py can run unattended.
COMMAND_ENV = os.environ.copy()
COMMAND_ENV.update(
    {
        "GIT_AUTHOR_NAME": "Vendor integration tests",
        "GIT_AUTHOR_EMAIL": "vendor-tests@example.invalid",
        "GIT_COMMITTER_NAME": "Vendor integration tests",
        "GIT_COMMITTER_EMAIL": "vendor-tests@example.invalid",
        "GIT_EDITOR": "true",
        "GIT_TERMINAL_PROMPT": "0",
        "LC_ALL": "C",
    }
)


class DownloadResponse:
    """Minimal context-managed response returned by the mocked downloader."""

    def __init__(self, contents):
        self.contents = contents

    def __enter__(self):
        return self

    def __exit__(self, exception_type, exception, traceback):
        return False

    def read(self):
        """Return the complete mocked response body."""
        return self.contents


class SelfUpdateTests(unittest.TestCase):
    """Exercise self-updates without accessing the network."""

    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory(
            prefix="vendor-self-update-"
        )
        self.script_path = Path(self.temporary_directory.name) / "vendor.py"
        self.script_path.write_bytes(b"#!/usr/bin/env python3\nprint('old')\n")
        self.script_path.chmod(0o751)
        self.updated_contents = (
            b"#!/usr/bin/env python3\n"
            b"# vendor v9.8.7 (2026-09-13) (0123456789abcdef)\n"
            b"print('new')\n"
        )

    def tearDown(self):
        self.temporary_directory.cleanup()

    def _run_update(self, contents=None):
        """Run update_self against the fixture script and capture output."""
        response = DownloadResponse(
            self.updated_contents if contents is None else contents
        )
        output = StringIO()
        with (
            mock.patch.object(vendor_module, "__file__", str(self.script_path)),
            mock.patch.object(
                vendor_module.urllib.request,
                "urlopen",
                return_value=response,
            ) as urlopen,
            redirect_stdout(output),
        ):
            vendor_module.update_self()
        return output.getvalue(), urlopen

    def test_update_replaces_script_and_preserves_mode(self):
        """A valid newer script atomically replaces the running file."""
        output, urlopen = self._run_update()

        self.assertEqual(self.script_path.read_bytes(), self.updated_contents)
        self.assertEqual(stat.S_IMODE(self.script_path.stat().st_mode), 0o751)
        self.assertEqual(output, "Updated vendor.py to v9.8.7.\n")
        urlopen.assert_called_once_with(vendor_module.UPDATE_URL, timeout=30)

    def test_current_script_reports_already_updated(self):
        """An identical download leaves the script in place and says so."""
        self.script_path.write_bytes(self.updated_contents)

        output, _ = self._run_update()

        self.assertEqual(output, "Already updated to v9.8.7.\n")

    def test_older_download_reports_downgrade(self):
        """Replacing a newer local version identifies the downgrade."""
        current_contents = self.updated_contents.replace(b"v9.8.7", b"v10.0.0")
        self.script_path.write_bytes(current_contents)

        output, _ = self._run_update()

        self.assertEqual(self.script_path.read_bytes(), self.updated_contents)
        self.assertEqual(output, "Downgraded vendor.py to v9.8.7.\n")

    def test_invalid_download_does_not_replace_script(self):
        """Content without the canonical version header is rejected."""
        original_contents = self.script_path.read_bytes()

        with self.assertRaisesRegex(ValueError, "no valid vendor version header"):
            self._run_update(b"<!doctype html>upstream failure")

        self.assertEqual(self.script_path.read_bytes(), original_contents)

    def test_update_flag_exits_before_vendor_operations(self):
        """The update flag does not require a repository or manifest."""
        with mock.patch.object(vendor_module, "update_self") as update_self:
            result = vendor_module.main(["--update"])

        self.assertEqual(result, 0)
        update_self.assert_called_once_with()


def run(command, cwd):
    """Run a fixture command and include all output in any failure."""
    result = subprocess.run(
        [str(argument) for argument in command],
        cwd=cwd,
        env=COMMAND_ENV,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode:
        rendered_command = " ".join(str(argument) for argument in command)
        raise RuntimeError(
            f"Command failed in {cwd}: {rendered_command}\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )
    return result.stdout


def git(repository, *arguments):
    """Run Git in a fixture repository and return stripped stdout."""
    return run(["git", *arguments], repository).strip()


def commit_all(repository, message):
    """Commit the complete, deliberately small fixture worktree."""
    git(repository, "add", "--all")
    git(repository, "commit", "-m", message)


class VendorIntegrationTests(unittest.TestCase):
    """Exercise vendor.py against real Git repositories and subtrees."""

    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory(
            prefix="vendor-integration-"
        )
        self.root = Path(self.temporary_directory.name)

        # Each source is a genuine two-release repository so subtree pulls
        # exercise Git's real upgrade and reverse-update behavior.
        self.sources = {
            "libanswer": self._create_library(
                "libanswer",
                "int answer(void)",
                {"v1.0.0": "return 41;", "v2.0.0": "return 42;"},
            ),
            "libgreet": self._create_library(
                "libgreet",
                "const char *greeting(void)",
                {
                    "v1.0.0": 'return "hello";',
                    "v2.0.0": 'return "hello, world";',
                },
            ),
        }

        self.consumer = self.root / "consumer"
        self.consumer.mkdir()
        git(self.consumer, "init", "--initial-branch=main")
        shutil.copy2(VENDOR_SCRIPT, self.consumer / "vendor.py")
        self._write_manifest("v1.0.0")
        commit_all(self.consumer, "Set up vendor fixture")

    def tearDown(self):
        self.temporary_directory.cleanup()

    def _create_library(self, name, declaration, releases):
        """Create a tiny tagged C library, returning its repository path."""
        repository = self.root / name
        repository.mkdir()
        git(repository, "init", "--initial-branch=main")

        header_guard = f"{name.upper()}_H"
        (repository / f"{name}.h").write_text(
            f"#ifndef {header_guard}\n"
            f"#define {header_guard}\n\n"
            f"{declaration};\n\n"
            f"#endif\n"
        )

        # Tags are created in insertion order, making v2 a descendant of v1.
        for tag, implementation in releases.items():
            (repository / f"{name}.c").write_text(
                f'#include "{name}.h"\n\n'
                f"{declaration}\n"
                "{\n"
                f"    {implementation}\n"
                "}\n"
            )
            commit_all(repository, f"Release {name} {tag}")
            git(repository, "tag", tag)

        return repository

    def _write_manifest(self, revision):
        """Point every fixture dependency at the requested release."""
        vendor_directory = self.consumer / "vendor"
        vendor_directory.mkdir(exist_ok=True)
        manifest = [
            {
                "repo_url": str(repository),
                "name": name,
                "rev": revision,
            }
            for name, repository in sorted(self.sources.items())
        ]
        (vendor_directory / "vendor.json").write_text(
            json.dumps(manifest, indent=2) + "\n"
        )

    def _create_local_checkout(self, name, source_text):
        """Create a local checkout whose content differs from every release."""
        checkout = self.root / f"{name}-checkout"
        checkout.mkdir()
        shutil.copy2(self.sources[name] / f"{name}.h", checkout / f"{name}.h")
        (checkout / f"{name}.c").write_text(source_text)
        return checkout

    def _write_replacement_config(self, replacements):
        """Configure local checkouts with paths relative to vendor/."""
        vendor_directory = self.consumer / "vendor"
        config = {
            name: os.path.relpath(checkout, vendor_directory)
            for name, checkout in replacements.items()
        }
        (vendor_directory / "vendor_replace.json").write_text(
            json.dumps(config, indent=2) + "\n"
        )
        return config

    def _run_vendor(self, *arguments):
        """Invoke the copied script exactly as a consuming repository would."""
        return run([sys.executable, "vendor.py", *arguments], self.consumer)

    def _run_vendor_result(self, *arguments):
        """Invoke the script while preserving an expected failure result."""
        return subprocess.run(
            [sys.executable, "vendor.py", *arguments],
            cwd=self.consumer,
            env=COMMAND_ENV,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def _assert_release(self, revision):
        """Check that both vendored source files match a tagged release."""
        expected_fragments = {
            "v1.0.0": {
                "libanswer": "return 41;",
                "libgreet": 'return "hello";',
            },
            "v2.0.0": {
                "libanswer": "return 42;",
                "libgreet": 'return "hello, world";',
            },
        }
        for name, fragment in expected_fragments[revision].items():
            with self.subTest(library=name, revision=revision):
                source = self.consumer / "vendor" / name / f"{name}.c"
                self.assertIn(fragment, source.read_text())

    def test_vendors_multiple_c_libraries_and_is_idempotent(self):
        """A fresh run vendors every library and a repeated run is a no-op."""
        self._run_vendor()
        self._assert_release("v1.0.0")
        head_after_first_run = git(self.consumer, "rev-parse", "HEAD")

        output = self._run_vendor()

        self.assertEqual(
            head_after_first_run, git(self.consumer, "rev-parse", "HEAD")
        )
        self.assertEqual(output.count("already at revision 'v1.0.0'"), 2)
        self.assertEqual(git(self.consumer, "status", "--porcelain"), "")

    def test_dirty_worktree_reports_clean_error_without_traceback(self):
        """Tracked changes explain why subtree cannot create its commit."""
        # Dirty a tracked file without breaking the copy of vendor.py that the
        # integration fixture is about to execute.
        with (self.consumer / "vendor.py").open("a") as script:
            script.write("\n# Local fixture change.\n")

        result = self._run_vendor_result()

        self.assertEqual(result.returncode, 1)
        self.assertIn(
            "git subtree requires a clean index and working tree", result.stderr
        )
        self.assertIn(" M vendor.py", result.stderr)
        self.assertIn("Commit or stash these tracked changes", result.stderr)
        self.assertNotIn("Traceback", result.stdout + result.stderr)
        self.assertNotIn("fatal: working tree has modifications", result.stderr)

    def test_untracked_files_do_not_block_subtree_updates(self):
        """Untracked files remain allowed, matching git subtree itself."""
        (self.consumer / "local-notes.txt").write_text("Keep this untracked.\n")

        self._run_vendor()

        self._assert_release("v1.0.0")
        self.assertEqual(
            git(self.consumer, "status", "--porcelain"), "?? local-notes.txt"
        )

    def test_upgrade_and_downgrade_leave_readable_history(self):
        """Changing pins in either direction updates content and clear history."""
        self._run_vendor()

        self._write_manifest("v2.0.0")
        commit_all(self.consumer, "Pin libraries to v2.0.0")
        self._run_vendor()
        self._assert_release("v2.0.0")

        self._write_manifest("v1.0.0")
        commit_all(self.consumer, "Pin libraries to v1.0.0")
        self._run_vendor()
        self._assert_release("v1.0.0")

        # First-parent history is what a maintainer normally reads. It should
        # contain one clearly named merge per dependency transition, without
        # exposing git-subtree's implementation-oriented default subjects.
        subjects = git(
            self.consumer, "log", "--first-parent", "--format=%s"
        ).splitlines()
        self.assertEqual(
            subjects,
            [
                "vendor: Upgraded libgreet to 'v1.0.0'",
                "vendor: Upgraded libanswer to 'v1.0.0'",
                "Pin libraries to v1.0.0",
                "vendor: Upgraded libgreet to 'v2.0.0'",
                "vendor: Upgraded libanswer to 'v2.0.0'",
                "Pin libraries to v2.0.0",
                "vendor: Upgraded libgreet to 'v1.0.0'",
                "vendor: Upgraded libanswer to 'v1.0.0'",
                "Set up vendor fixture",
            ],
        )

        # Squashed subtree operations should remain merge commits, preserving
        # an inspectable connection to the imported snapshots.
        merge_commits = git(
            self.consumer, "rev-list", "--first-parent", "--merges", "HEAD"
        ).splitlines()
        self.assertEqual(len(merge_commits), 6)
        self.assertEqual(git(self.consumer, "status", "--porcelain"), "")

    def test_replacement_is_invisible_and_idempotent(self):
        """A replacement swaps in local code without dirtying the repository."""
        self._run_vendor()
        checkout = self._create_local_checkout(
            "libanswer",
            '#include "libanswer.h"\n\n'
            "int answer(void)\n"
            "{\n"
            "    return 9001;\n"
            "}\n",
        )
        config = self._write_replacement_config({"libanswer": checkout})
        head_before_replacement = git(self.consumer, "rev-parse", "HEAD")

        output = self._run_vendor()

        vendored_checkout = self.consumer / "vendor" / "libanswer"
        self.assertTrue(vendored_checkout.is_symlink())
        self.assertEqual(vendored_checkout.resolve(), checkout.resolve())
        self.assertIn("return 9001;", (vendored_checkout / "libanswer.c").read_text())
        self.assertIn("Skipping 'libanswer'", output)
        self.assertEqual(git(self.consumer, "status", "--porcelain"), "")
        self.assertEqual(
            git(
                self.consumer,
                "check-ignore",
                "--no-index",
                "vendor/vendor_replace.json",
                "vendor/libanswer",
            ).splitlines(),
            ["vendor/vendor_replace.json", "vendor/libanswer"],
        )

        # The tracked checkout remains in the index while the local symlink is
        # active, so commits cannot accidentally capture replacement content.
        index_entry = git(
            self.consumer,
            "ls-files",
            "-v",
            "--",
            "vendor/libanswer/libanswer.c",
        )
        self.assertTrue(index_entry.startswith("S "))
        committed_source = git(
            self.consumer, "show", "HEAD:vendor/libanswer/libanswer.c"
        )
        self.assertIn("return 41;", committed_source)

        status = self._run_vendor("--status")
        self.assertIn(f"libanswer -> {config['libanswer']}", status)
        repeated_output = self._run_vendor()
        self.assertIn("is already replaced", repeated_output)
        self.assertEqual(
            head_before_replacement, git(self.consumer, "rev-parse", "HEAD")
        )
        self.assertEqual(git(self.consumer, "status", "--porcelain"), "")

    def test_removed_replacement_restores_and_catches_up(self):
        """Removing a replacement restores its subtree and applies a new pin."""
        self._run_vendor()
        checkout = self._create_local_checkout(
            "libanswer",
            '#include "libanswer.h"\n\n'
            "int answer(void)\n"
            "{\n"
            "    return 9001;\n"
            "}\n",
        )
        self._write_replacement_config({"libanswer": checkout})
        self._run_vendor()

        # While libanswer is replaced it must be skipped, but unrelated
        # dependencies should still follow the updated manifest.
        self._write_manifest("v2.0.0")
        commit_all(self.consumer, "Pin libraries to v2.0.0")
        output = self._run_vendor()
        self.assertIn("Skipping 'libanswer'", output)
        self.assertIn(
            'return "hello, world";',
            (self.consumer / "vendor/libgreet/libgreet.c").read_text(),
        )
        self.assertIn("return 9001;", (checkout / "libanswer.c").read_text())

        (self.consumer / "vendor/vendor_replace.json").unlink()
        output = self._run_vendor()

        restored_checkout = self.consumer / "vendor" / "libanswer"
        self.assertIn("Restored 'vendor/libanswer'", output)
        self.assertFalse(restored_checkout.is_symlink())
        self._assert_release("v2.0.0")
        index_entry = git(
            self.consumer,
            "ls-files",
            "-v",
            "--",
            "vendor/libanswer/libanswer.c",
        )
        self.assertFalse(index_entry.startswith("S "))
        self.assertEqual(
            self._run_vendor("--status").strip(), "No active replacements."
        )
        self.assertEqual(git(self.consumer, "status", "--porcelain"), "")

    def test_restore_replacements_command_restores_parked_checkout(self):
        """The explicit restore command recovers a parked vendored subtree."""
        self._run_vendor()
        checkout = self._create_local_checkout(
            "libanswer",
            '#include "libanswer.h"\n\n'
            "int answer(void)\n"
            "{\n"
            "    return 9001;\n"
            "}\n",
        )
        self._write_replacement_config({"libanswer": checkout})
        self._run_vendor()

        # The command must restore immediately even while the replacement is
        # still configured; a future normal run may choose to apply it again.
        output = self._run_vendor("--restore-replacements")

        self.assertIn("Restored 'vendor/libanswer'", output)
        self.assertFalse((self.consumer / "vendor/libanswer").is_symlink())
        self._assert_release("v1.0.0")
        self.assertEqual(
            self._run_vendor("--status").strip(), "No active replacements."
        )

        # Discard the local-only config after inspecting the restored state so
        # the fixture finishes with the same clean worktree it started with.
        (self.consumer / "vendor/vendor_replace.json").unlink()
        self.assertEqual(git(self.consumer, "status", "--porcelain"), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
