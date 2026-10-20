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


class ExcludeConfigTests(unittest.TestCase):
    """Validate optional exclusion rules before any subtree operation."""

    def test_default_exclusions_are_independent_empty_lists(self):
        """Existing configurations need no extra field or shared defaults."""
        first = vendor_module.VendorConfig("url", "first", "v1")
        second = vendor_module.VendorConfig("url", "second", "v1")
        first.exclude.append("docs/")
        self.assertEqual(second.exclude, [])

    def test_invalid_exclusions_are_rejected(self):
        """Reject non-lists, non-strings, and injected gitignore lines."""
        for patterns in (None, "docs/", {}, [1], ["docs/\n*.c"], ["a\rb"], ["a\0b"]):
            with self.subTest(patterns=patterns):
                with self.assertRaisesRegex(ValueError, "list of single-line strings"):
                    vendor_module.VendorConfig("url", "library", "v1", patterns)

    def test_missing_filter_repo_reports_requirement(self):
        """A configured filter fails clearly without an optional tool."""
        with mock.patch.object(vendor_module.shutil, "which", return_value=None):
            with self.assertRaisesRegex(ValueError, "requires git-filter-repo"):
                with vendor_module.filtered_repository("url", "v1", ["docs/"]):
                    self.fail("filtering must not proceed without git-filter-repo")


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

    def _write_exclusion_manifest(self, revision, patterns):
        """Commit rules as a consumer would before invoking vendor.py."""
        self._write_manifest(revision)
        manifest_path = self.consumer / "vendor/vendor.json"
        manifest = json.loads(manifest_path.read_text())
        manifest[0]["exclude"] = patterns
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
        commit_all(self.consumer, f"Configure {revision} exclusions")

    def _prepare_exclusion_source(self):
        """Create releases with kept files, ignored files, and excluded-only edits."""
        repository = self.sources["libanswer"]
        self.sources = {"libanswer": repository}
        files = {
            "docs/guide.txt": "Documentation v1\n",
            "nested/docs/guide.txt": "Nested documentation\n",
            "root-only.txt": "Root file\n",
            "nested/root-only.txt": "Nested file stays\n",
            "remove.tmp": "Temporary file\n",
            "nested/line\nbreak.tmp": "Unusual filename\n",
            "keep.tmp": "Negated exclusion\n",
            "assets/generated1.c": "Generated file\n",
            "assets/deep/generated2.h": "Nested generated file\n",
            "assets/generated1.cc": "Different extension stays\n",
            "samples/remove.c": "Sample\n",
            "samples/keep.c": "Kept sample\n",
            "#notes": "Escaped comment\n",
            "!notice": "Escaped negation\n",
            "trailing ": "Significant trailing space\n",
            ".gitignore": "ignored-by-upstream.txt\n",
            "ignored-by-upstream.txt": "Tracked upstream despite ignore\n",
            "case-only": "Case-sensitive patterns\n",
        }
        for relative, contents in files.items():
            path = repository / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(contents)
        git(repository, "add", "-f", "ignored-by-upstream.txt")
        commit_all(repository, "Add filtering fixture")
        git(repository, "tag", "filtered-v1")

        # Keep local edits on a separate line from the upstream change so a
        # proper three-way subtree merge can preserve both without conflicts.
        (repository / "libanswer.c").write_text(
            (repository / "libanswer.c").read_text().replace("return 42;", "return 43;")
        )
        (repository / "docs/guide.txt").write_text("Documentation v2\n")
        (repository / "docs/new.txt").write_text("New documentation\n")
        (repository / "remove.tmp").unlink()
        commit_all(repository, "Update retained and excluded paths")
        git(repository, "tag", "filtered-v2")

        (repository / "docs/guide.txt").write_text("Documentation v3\n")
        commit_all(repository, "Change excluded documentation only")
        git(repository, "tag", "filtered-v3")
        return repository

    def _clone_consumer(self):
        """Clone without borrowing unreachable split objects from the source."""
        destination = self.root / "fresh-consumer"
        run(["git", "clone", "--no-local", self.consumer, destination], self.root)
        self.consumer = destination

    @unittest.skipUnless(shutil.which("git-filter-repo"), "requires git-filter-repo")
    def test_gitignore_exclusions_and_filtered_updates_preserve_local_edits(self):
        """Use real gitignore semantics and preserve edits across filtered pulls."""
        repository = self._prepare_exclusion_source()
        upstream_head = git(repository, "rev-parse", "HEAD")
        patterns = [
            "docs/", "/root-only.txt", "*.tmp", "!keep.tmp",
            "/assets/**/generated?.[ch]", "/samples/*", "!/samples/keep.c",
            r"\#notes", r"\!notice", "trailing\\ ", "CASE-ONLY",
        ]
        self._write_exclusion_manifest("filtered-v1", patterns)
        # User ignore settings must not change filtered history or strip
        # tracked headers; the manifest defines a reproducible filter.
        user_ignores = self.root / "user-ignore"
        user_ignores.write_text("*.h\n")
        with mock.patch.dict(COMMAND_ENV, {
            "GIT_CONFIG_COUNT": "2",
            "GIT_CONFIG_KEY_0": "core.excludesFile",
            "GIT_CONFIG_VALUE_0": str(user_ignores),
            "GIT_CONFIG_KEY_1": "core.ignoreCase",
            "GIT_CONFIG_VALUE_1": "true",
        }):
            self._run_vendor()
        self.assertEqual(
            git(self.consumer, "log", "-1", "--format=%B"),
            "vendor: Upgraded libanswer to 'filtered-v1'",
        )
        first_split = git(
            self.consumer, "log", "-1", "--format=%B",
            "--grep=^git-subtree-dir: vendor/libanswer$",
        ).split("git-subtree-split: ")[1].splitlines()[0]
        subtree = self.consumer / "vendor/libanswer"
        for relative in (
            "docs", "nested/docs", "root-only.txt", "remove.tmp",
            "nested/line\nbreak.tmp", "assets/generated1.c",
            "assets/deep/generated2.h", "samples/remove.c", "#notes", "!notice",
            "trailing ",
        ):
            with self.subTest(excluded=relative):
                self.assertFalse((subtree / relative).exists())
        for relative in (
            "nested/root-only.txt", "keep.tmp", "assets/generated1.cc",
            "samples/keep.c", "ignored-by-upstream.txt", "case-only", "libanswer.h",
        ):
            with self.subTest(retained=relative):
                self.assertTrue((subtree / relative).is_file())

        # A local header edit must survive an upstream implementation update.
        header = subtree / "libanswer.h"
        header.write_text(header.read_text() + "\n/* Consumer customization. */\n")
        commit_all(self.consumer, "Customize vendored header")
        for revision in ("filtered-v2", "filtered-v3"):
            self._write_exclusion_manifest(revision, patterns)
            self._run_vendor()
            self.assertEqual(
                git(self.consumer, "log", "-1", "--format=%B"),
                f"vendor: Upgraded libanswer to '{revision}'",
            )
            self.assertFalse((subtree / "docs").exists())
            self.assertIn("return 43;", (subtree / "libanswer.c").read_text())
            self.assertIn("Consumer customization", header.read_text())

        # The filtered history itself retains the same original ancestor,
        # rather than merely producing the right worktree by replacing files.
        latest_split = git(
            self.consumer, "log", "-1", "--format=%B",
            "--grep=^git-subtree-dir: vendor/libanswer$",
        ).split("git-subtree-split: ")[1].splitlines()[0]
        git(self.consumer, "merge-base", "--is-ancestor", first_split, latest_split)

        head = git(self.consumer, "rev-parse", "HEAD")
        self.assertIn("already at revision", self._run_vendor())
        self.assertEqual(git(self.consumer, "rev-parse", "HEAD"), head)
        self.assertEqual(git(self.consumer, "status", "--porcelain"), "")
        self.assertEqual(git(repository, "rev-parse", "HEAD"), upstream_head)
        self.assertTrue((repository / "docs/guide.txt").is_file())

    @unittest.skipUnless(shutil.which("git-filter-repo"), "requires git-filter-repo")
    def test_filtered_upgrade_and_downgrade_after_fresh_clone(self):
        """Reconstruct missing split commits for upgrades and reverse updates."""
        self._prepare_exclusion_source()
        self._write_exclusion_manifest("filtered-v1", ["docs/"])
        self._run_vendor()
        self._clone_consumer()
        self._write_exclusion_manifest("filtered-v2", ["docs/"])
        self._run_vendor()
        self.assertIn("return 43;", (self.consumer / "vendor/libanswer/libanswer.c").read_text())

        # A different destination guarantees the downgrade starts without
        # unreachable filtered objects left by any prior invocation.
        previous_consumer = self.consumer
        self.consumer = self.root / "downgrade-consumer"
        run(["git", "clone", "--no-local", previous_consumer, self.consumer], self.root)
        self._write_exclusion_manifest("filtered-v1", ["docs/"])
        self._run_vendor()
        self.assertIn("return 42;", (self.consumer / "vendor/libanswer/libanswer.c").read_text())
        self.assertFalse((self.consumer / "vendor/libanswer/docs").exists())
        self.assertEqual(git(self.consumer, "status", "--porcelain"), "")

    @unittest.skipUnless(shutil.which("git-filter-repo"), "requires git-filter-repo")
    def test_changing_rules_at_same_revision_including_empty_subtree(self):
        """Add, change, and remove rules without losing subtree ancestry."""
        self._prepare_exclusion_source()
        for patterns in ([], ["docs/"], ["*.tmp"], ["*"], [], ["absent-path"]):
            with self.subTest(patterns=patterns):
                self._write_exclusion_manifest("filtered-v1", patterns)
                manifest_commit = git(self.consumer, "rev-parse", "HEAD")
                self._run_vendor()
                head = git(self.consumer, "rev-parse", "HEAD")
                self.assertNotEqual(head, manifest_commit)
                self.assertEqual(
                    git(self.consumer, "log", "-1", "--format=%B"),
                    "vendor: Upgraded libanswer to 'filtered-v1'",
                )
                subtree = self.consumer / "vendor/libanswer"
                if patterns == ["*"]:
                    self.assertEqual(git(self.consumer, "ls-files", "vendor/libanswer"), "")
                else:
                    self.assertEqual((subtree / "docs/guide.txt").exists(), "docs/" not in patterns)
                self.assertIn("already at revision", self._run_vendor())
                self.assertEqual(git(self.consumer, "rev-parse", "HEAD"), head)
                self.assertEqual(git(self.consumer, "status", "--porcelain"), "")

    @unittest.skipUnless(shutil.which("git-filter-repo"), "requires git-filter-repo")
    def test_rule_change_after_clone_rebuilds_previous_filter(self):
        """A different rule set must not prevent restoring the old merge base."""
        self._prepare_exclusion_source()
        self._write_exclusion_manifest("filtered-v1", ["docs/"])
        self._run_vendor()
        self._clone_consumer()
        self._write_exclusion_manifest("filtered-v1", ["*.tmp"])
        self._run_vendor()
        subtree = self.consumer / "vendor/libanswer"
        self.assertTrue((subtree / "docs/guide.txt").is_file())
        self.assertFalse((subtree / "remove.tmp").exists())
        self.assertEqual(git(self.consumer, "status", "--porcelain"), "")

    @unittest.skipUnless(shutil.which("git-filter-repo"), "requires git-filter-repo")
    def test_existing_import_accepts_exclusions_with_relative_upstream_url(self):
        """Filter an existing unfiltered import with a relative upstream URL."""
        repository = self._prepare_exclusion_source()
        self.sources["libanswer"] = Path(os.path.relpath(repository, self.consumer))
        self._write_exclusion_manifest("filtered-v1", [])
        self._run_vendor()

        # Clone so the original unfiltered split object is absent locally.
        self._clone_consumer()
        self._write_exclusion_manifest("filtered-v1", ["docs/"])
        self._run_vendor()
        subtree = self.consumer / "vendor/libanswer"
        self.assertTrue((subtree / "libanswer.c").is_file())
        self.assertFalse((subtree / "docs").exists())
        self.assertEqual(git(self.consumer, "status", "--porcelain"), "")

    @unittest.skipUnless(shutil.which("git-filter-repo"), "requires git-filter-repo")
    def test_untracked_manifest_allows_repeated_updates(self):
        """An untracked manifest must support both plain and filtered imports."""
        repository = self._prepare_exclusion_source()
        # A second name for the same commit still needs its own import
        # message, even though git subtree has no new content to merge.
        git(repository, "tag", "filtered-alias", "filtered-v2")
        # Leave the manifest on disk without including it in import history,
        # matching consumers that keep their vendoring configuration local.
        git(self.consumer, "rm", "--cached", "vendor/vendor.json")
        git(self.consumer, "commit", "-m", "Keep the vendor manifest local")

        for patterns in ([], ["docs/"]):
            for revision in ("filtered-v1", "filtered-v2", "filtered-alias"):
                with self.subTest(patterns=patterns, revision=revision):
                    self._write_manifest(revision)
                    manifest_path = self.consumer / "vendor/vendor.json"
                    manifest = json.loads(manifest_path.read_text())
                    manifest[0]["exclude"] = patterns
                    manifest_path.write_text(json.dumps(manifest) + "\n")
                    self._run_vendor()
                    self.assertEqual(
                        git(self.consumer, "log", "-1", "--format=%B"),
                        f"vendor: Upgraded libanswer to '{revision}'",
                    )
                    subtree = self.consumer / "vendor/libanswer"
                    self.assertEqual((subtree / "docs").exists(), not patterns)
                    implementation = "return 42;" if revision == "filtered-v1" else "return 43;"
                    self.assertIn(implementation, (subtree / "libanswer.c").read_text())

                    head = git(self.consumer, "rev-parse", "HEAD")
                    if not patterns and revision == "filtered-v1":
                        original_import = head
                    # An unchanged import must not fetch or filter upstream
                    # again, even when the manifest was never committed.
                    offline = repository.with_name(repository.name + "-offline")
                    repository.rename(offline)
                    try:
                        self._run_vendor()
                    finally:
                        offline.rename(repository)
                    self.assertEqual(git(self.consumer, "rev-parse", "HEAD"), head)
                    self.assertEqual(
                        git(self.consumer, "status", "--porcelain"),
                        "?? vendor/vendor.json",
                    )

        # Switching history must invalidate the latest cached import rather
        # than skipping updates to the older files now in the worktree.
        git(self.consumer, "checkout", "--detach", original_import)
        self._run_vendor()
        subtree = self.consumer / "vendor/libanswer"
        self.assertIn("return 43;", (subtree / "libanswer.c").read_text())
        self.assertFalse((subtree / "docs").exists())

    @unittest.skipUnless(shutil.which("git-filter-repo"), "requires git-filter-repo")
    def test_manifest_committed_after_import_skips_upstream(self):
        """Use the first tracked manifest for imports predating that file."""
        repository = self._prepare_exclusion_source()
        git(self.consumer, "rm", "--cached", "vendor/vendor.json")
        git(self.consumer, "commit", "-m", "Keep the vendor manifest local")
        self._write_manifest("filtered-v1")
        manifest_path = self.consumer / "vendor/vendor.json"
        manifest = json.loads(manifest_path.read_text())
        manifest[0]["exclude"] = ["docs/"]
        manifest_path.write_text(json.dumps(manifest) + "\n")
        self._run_vendor()
        commit_all(self.consumer, "Track the vendor manifest after importing")
        self._clone_consumer()

        # A fresh clone has no local import cache or upstream split objects.
        # Taking upstream offline proves the skip happens before filtering.
        head = git(self.consumer, "rev-parse", "HEAD")
        offline = repository.with_name(repository.name + "-offline")
        repository.rename(offline)
        try:
            self.assertIn("already at revision", self._run_vendor())
        finally:
            offline.rename(repository)
        self.assertEqual(git(self.consumer, "rev-parse", "HEAD"), head)

        # Later manifest edits must still be compared with the first snapshot
        # rather than being mistaken for settings used by the old import.
        self._write_exclusion_manifest("filtered-v1", ["*.tmp"])
        self._run_vendor()
        subtree = self.consumer / "vendor/libanswer"
        self.assertTrue((subtree / "docs/guide.txt").is_file())
        self.assertFalse((subtree / "remove.tmp").exists())

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
        self.assertEqual(
            git(self.consumer, "log", "--first-parent", "-2", "--format=%B"),
            "vendor: Upgraded libgreet to 'v1.0.0'\n\n"
            "vendor: Upgraded libanswer to 'v1.0.0'",
        )
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
