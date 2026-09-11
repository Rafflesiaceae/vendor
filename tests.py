#!/usr/bin/env python3
"""Integration tests for vendoring local C library repositories."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

from pathlib import Path


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

    def _run_vendor(self):
        """Invoke the copied script exactly as a consuming repository would."""
        return run([sys.executable, "vendor.py"], self.consumer)

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
