#!/usr/bin/env python3
# vendor v0.23 (2026-10-21) (255be66ac92076f5)
#
# Updates vendor dependencies via git subtrees or HTTP ZIP archives
#
# https://github.com/Rafflesiaceae/vendor
#
# Requirements:
#   Python standard library only; never require third-party Python packages.
#   - python >= 3.11
#   - git >= 2.22.0
#   - git-filter-repo >= 2.23.0
import filecmp
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import tomllib
import urllib.request
import urllib.parse
import zipfile

from contextlib import contextmanager
from dataclasses import dataclass, asdict, field
from typing import Dict, List

VENDOR_DIR = "./vendor"
VENDOR_JSON_PATH = "./vendor/vendor.json"
VENDOR_TOML_PATH = "./vendor/vendor.toml"
VENDOR_REPLACE_JSON_PATH = "./vendor/vendor_replace.json"

# Self-updates always follow the canonical script on the main branch.
UPDATE_URL = (
    "https://raw.githubusercontent.com/Rafflesiaceae/vendor/"
    "refs/heads/main/vendor.py"
)
VERSION_HEADER = re.compile(rb"^# vendor v([0-9]+(?:\.[0-9]+)*)\b", re.MULTILINE)

# Markers delimiting the section this script owns inside `.git/info/exclude`.
# Everything between them is rewritten on every run, everything outside of
# them is left untouched.
EXCLUDE_BEGIN = "# BEGIN vendor.py replace (managed section, do not edit)"
EXCLUDE_END = "# END vendor.py replace"


@dataclass
class VendorConfig:
    repo_url: str = None
    name: str = None
    rev: str = None
    exclude: List[str] = field(default_factory=list)
    http: str = None
    strip_root: bool = None

    def __post_init__(self):
        # Both source types share a mandatory destination and exclusion rules.
        if not isinstance(self.name, str) or not self.name:
            raise ValueError("Each vendor config requires a non-empty 'name'")
        if (self.repo_url is None) == (self.http is None):
            raise ValueError("Each vendor config requires exactly one of 'repo_url' or 'http'")
        if self.http is not None:
            if not isinstance(self.http, str) or urllib.parse.urlsplit(self.http).scheme not in ("http", "https"):
                raise ValueError("'http' must be an HTTP or HTTPS ZIP URL")
            if self.strip_root is not None and not isinstance(self.strip_root, bool):
                raise ValueError("'strip_root' must be a boolean")
            # Archive imports replace their destination, so never allow them
            # to address parent directories or the consumer's manifest files.
            if (
                any(character in self.name for character in "\\:*?[]")
                or any(part in ("", ".", "..", ".git") for part in self.name.split("/"))
                or self.name in ("vendor.json", "vendor.toml", "vendor_replace.json")
            ):
                raise ValueError("ZIP 'name' must be a relative directory below vendor/")
        elif not self.rev:
            raise ValueError("Git vendor configs require 'repo_url', 'name', and 'rev'")
        elif self.strip_root is not None:
            raise ValueError("'strip_root' is only supported for HTTP ZIP dependencies")
        # Each item represents exactly one gitignore rule, including escapes
        # and negation; reject separators that would inject additional rules.
        if not isinstance(self.exclude, list) or any(
            not isinstance(pattern, str)
            or any(separator in pattern for separator in ("\n", "\r", "\0"))
            for pattern in self.exclude
        ):
            raise ValueError(
                f"'exclude' for '{self.name}' must be a list of single-line strings"
            )


def run_cmd(cmd, cwd=None):
    try:
        result = subprocess.check_output(cmd, shell=False, cwd=cwd, text=True).strip()
        return result
    except subprocess.CalledProcessError as e:
        print(f"Command failed: {' '.join(e.cmd)}")
        sys.exit(1)


def update_self():
    """Atomically replace this script with the latest canonical version."""
    script_path = os.path.realpath(__file__)

    # Reject an unexpected response before it can replace a working script.
    with urllib.request.urlopen(UPDATE_URL, timeout=30) as response:
        updated_contents = response.read()
    version_match = VERSION_HEADER.search(updated_contents)
    if version_match is None:
        raise ValueError("downloaded script has no valid vendor version header")
    try:
        compile(updated_contents, UPDATE_URL, "exec")
    except SyntaxError as error:
        raise ValueError(f"downloaded script is not valid Python: {error}") from error

    version = version_match.group(1).decode("ascii")
    with open(script_path, "rb") as script:
        current_contents = script.read()
    if current_contents == updated_contents:
        print(f"Already updated to v{version}.")
        return

    update_action = "Updated"
    current_version_match = VERSION_HEADER.search(current_contents)
    if current_version_match is not None:
        # Compare numeric components and treat omitted trailing zeroes as
        # equivalent, so versions such as 1.10 and 1.9 sort correctly.
        updated_parts = tuple(int(part) for part in version.split("."))
        current_parts = tuple(
            int(part)
            for part in current_version_match.group(1).decode("ascii").split(".")
        )
        width = max(len(updated_parts), len(current_parts))
        updated_key = updated_parts + (0,) * (width - len(updated_parts))
        current_key = current_parts + (0,) * (width - len(current_parts))
        if updated_key < current_key:
            update_action = "Downgraded"

    temporary_path = None
    try:
        # A sibling temporary file keeps os.replace atomic on the target
        # filesystem, while copying the mode retains direct executability.
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=os.path.dirname(script_path),
            prefix=f".{os.path.basename(script_path)}.",
            delete=False,
        ) as temporary:
            temporary_path = temporary.name
            temporary.write(updated_contents)
            temporary.flush()
            os.fsync(temporary.fileno())
        mode = stat.S_IMODE(os.stat(script_path).st_mode)
        os.chmod(temporary_path, mode)
        os.replace(temporary_path, script_path)
        temporary_path = None
    finally:
        # Clean up a partial sibling file when writing or replacing fails.
        if temporary_path is not None and os.path.exists(temporary_path):
            os.unlink(temporary_path)

    print(f"{update_action} vendor.py to v{version}.")


# --- replacements ---------------------------------------------------------
#
# A replacement swaps the vendored subtree checkout at `vendor/<name>` for a
# symlink to a local directory, in the spirit of go mod's `replace` directive.
#
# All of this has to stay invisible to git, which takes three separate
# measures, because the swap would otherwise show up as three different kinds
# of change:
#
#   1. The tracked files of the subtree would show up as deleted, so they get
#      the `skip-worktree` index bit, which makes git assume they still match
#      the index no matter what the worktree looks like.
#   2. The symlink taking their place would show up as an untracked file, so
#      its path is listed in `.git/info/exclude` (which is per-clone and never
#      committed).
#   3. The subtree checkout itself must not be lost, so it is moved into the
#      git directory rather than deleted, and moved back on restore.
#
# The bookkeeping for all of that lives in the git directory as well, so no
# state of a replacement is ever visible in the worktree.


def git_dir():
    return run_cmd(["git", "rev-parse", "--absolute-git-dir"])


def replace_state_path():
    return os.path.join(git_dir(), "vendor_replace", "state.json")


def stash_path(name):
    """Location the real subtree checkout of `name` is parked at."""
    return os.path.join(git_dir(), "vendor_replace", "stash", name)


def load_replace_state() -> Dict[str, dict]:
    """Read the record of currently active replacements."""
    path = replace_state_path()
    if not os.path.exists(path):
        return {}
    with open(path, "r") as f:
        return json.load(f)


def save_replace_state(state: Dict[str, dict]):
    path = replace_state_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)
        f.write("\n")


def load_replace_config() -> Dict[str, str]:
    """Parse './vendor/vendor_replace.json' into a {name: path} mapping.

    The file is optional, a missing one simply means "no replacements". Both
    a plain object and a list of {"name", "path"} objects are accepted, so the
    file can either be written as a terse mapping or mirror the shape of
    vendor.json.
    """
    if not os.path.exists(VENDOR_REPLACE_JSON_PATH):
        return {}

    with open(VENDOR_REPLACE_JSON_PATH, "r") as f:
        data = json.load(f)

    if isinstance(data, dict):
        entries = data
    elif isinstance(data, list):
        entries = {}
        for entry in data:
            if not isinstance(entry, dict) or not all(
                k in entry for k in ("name", "path")
            ):
                raise ValueError(
                    f"Each entry in '{VENDOR_REPLACE_JSON_PATH}' must include "
                    "'name' and 'path'"
                )
            entries[entry["name"]] = entry["path"]
    else:
        raise ValueError(
            f"'{VENDOR_REPLACE_JSON_PATH}' must contain an object or a list"
        )

    for name, path in entries.items():
        if not isinstance(path, str) or not path:
            raise ValueError(
                f"Replacement path for '{name}' must be a non-empty string"
            )
    return entries


def resolve_replacement(name, configured_path):
    """Turn a configured replacement path into (symlink path, link target).

    Relative paths are interpreted as relative to './vendor', as documented,
    while the emitted link target is made relative to the directory holding
    the symlink so that nested names such as 'group/lib' keep working.
    """
    link_path = os.path.join("vendor", name)

    if os.path.isabs(configured_path):
        link_target = configured_path
        resolved = configured_path
    else:
        resolved = os.path.join(VENDOR_DIR, configured_path)
        link_target = os.path.relpath(resolved, os.path.dirname(link_path))

    if not os.path.isdir(resolved):
        raise ValueError(
            f"Replacement for '{name}' points at '{configured_path}', which is "
            f"not an existing directory (resolved to '{resolved}')"
        )
    return link_path, link_target


def tracked_files(path):
    """Repository paths tracked below `path`, empty if it was never committed."""
    out = subprocess.check_output(["git", "ls-files", "-z", "--", path], text=True)
    return [entry for entry in out.split("\0") if entry]


def set_skip_worktree(paths, skip):
    """Set or clear the skip-worktree index bit on `paths`."""
    if not paths:
        return
    flag = "--skip-worktree" if skip else "--no-skip-worktree"
    subprocess.run(
        ["git", "update-index", flag, "-z", "--stdin"],
        input="\0".join(paths) + "\0",
        text=True,
        check=True,
    )


def write_exclude_block(names):
    """Rewrite the managed section of '.git/info/exclude' for `names`."""
    exclude_path = os.path.join(git_dir(), "info", "exclude")
    os.makedirs(os.path.dirname(exclude_path), exist_ok=True)

    lines = []
    if os.path.exists(exclude_path):
        with open(exclude_path, "r") as f:
            lines = f.read().splitlines()

    # Drop any previously written section before appending the current one.
    kept = []
    inside = False
    for line in lines:
        if line == EXCLUDE_BEGIN:
            inside = True
            continue
        if line == EXCLUDE_END:
            inside = False
            continue
        if not inside:
            kept.append(line)

    while kept and not kept[-1]:
        kept.pop()

    # The config describes machine local paths, so keep it out of git as well.
    # Should it be tracked deliberately, this entry is a no-op, since git
    # never ignores tracked files.
    patterns = sorted(f"/vendor/{name}" for name in names)
    if os.path.exists(VENDOR_REPLACE_JSON_PATH):
        patterns.insert(0, "/vendor/vendor_replace.json")

    if patterns:
        if kept:
            kept.append("")
        kept.append(EXCLUDE_BEGIN)
        # Anchored at the repository root so only the replaced paths are
        # ignored, not same named directories elsewhere in the tree.
        kept.extend(patterns)
        kept.append(EXCLUDE_END)

    with open(exclude_path, "w") as f:
        f.write("\n".join(kept))
        if kept:
            f.write("\n")


def apply_replacement(name, configured_path, state):
    """Put a symlink to a local directory in place of a vendored subtree."""
    link_path, link_target = resolve_replacement(name, configured_path)

    if os.path.islink(link_path):
        if os.readlink(link_path) == link_target and name in state:
            print(f"'{link_path}' is already replaced by '{configured_path}'.")
            return
        # Adopt or re-point a stale link, the parked checkout (if any) stays
        # where it is and is picked up again by the state below.
        os.unlink(link_path)
    elif os.path.exists(link_path):
        # A real checkout is in the way and has to be parked, its tracked
        # files keep their index entries and are hidden via skip-worktree.
        parked = stash_path(name)
        if os.path.exists(parked):
            raise ValueError(
                f"Cannot park the checkout of '{name}', '{parked}' already "
                "exists. Remove it by hand once you are sure it is stale."
            )
        set_skip_worktree(tracked_files(link_path), True)
        os.makedirs(os.path.dirname(parked), exist_ok=True)
        shutil.move(link_path, parked)
        state.setdefault(name, {})["stashed"] = True

    os.makedirs(os.path.dirname(link_path), exist_ok=True)
    os.symlink(link_target, link_path)

    entry = state.setdefault(name, {})
    entry["path"] = configured_path
    entry["link_target"] = link_target
    entry.setdefault("stashed", False)
    print(f"Replaced '{link_path}' with a symlink to '{configured_path}'.")


def restore_replacement(name, state):
    """Undo a replacement, putting the vendored subtree checkout back."""
    entry = state.pop(name, {})
    link_path = os.path.join("vendor", name)

    if os.path.islink(link_path):
        os.unlink(link_path)

    if entry.get("stashed"):
        parked = stash_path(name)
        if os.path.exists(parked):
            if os.path.exists(link_path):
                raise ValueError(
                    f"Cannot restore the checkout of '{name}', '{link_path}' "
                    "is in the way."
                )
            shutil.move(parked, link_path)
        else:
            print(f"Warning: parked checkout for '{name}' is gone, cannot restore.")

    # Only meaningful once the files are back, clearing the bit while they are
    # still missing would make git report them as deleted.
    set_skip_worktree(tracked_files(link_path), False)
    print(f"Restored '{link_path}'.")


def apply_replacements() -> Dict[str, str]:
    """Sync the worktree with vendor_replace.json, returning active names."""
    config = load_replace_config()
    state = load_replace_state()

    # Anything that dropped out of the config, or that now points somewhere
    # else, is restored first so the swap always starts from a clean state.
    for name in sorted(set(state) - set(config)):
        restore_replacement(name, state)
    for name in sorted(set(state) & set(config)):
        if state[name].get("path") != config[name]:
            restore_replacement(name, state)

    try:
        for name in sorted(config):
            apply_replacement(name, config[name], state)
    finally:
        # Persist even when an entry blows up halfway through, so the record
        # keeps describing what the worktree actually looks like.
        write_exclude_block(sorted(state))
        save_replace_state(state)

    return {name: entry["path"] for name, entry in state.items()}


def restore_all_replacements():
    """Undo every active replacement."""
    state = load_replace_state()
    if not state:
        print("No active replacements.")
        return
    for name in sorted(state):
        restore_replacement(name, state)
    write_exclude_block([])
    save_replace_state(state)


def print_replacement_status():
    state = load_replace_state()
    if not state:
        print("No active replacements.")
        return
    print("Active replacements:")
    for name in sorted(state):
        entry = state[name]
        link_path = os.path.join("vendor", name)
        details = []
        if not os.path.islink(link_path):
            details.append("symlink missing")
        elif not os.path.isdir(link_path):
            details.append("target missing")
        if entry.get("stashed"):
            details.append(f"checkout parked at {stash_path(name)}")
        suffix = f" ({', '.join(details)})" if details else ""
        print(f"  {name} -> {entry['path']}{suffix}")


def last_vendor_import_commit(name):
    """Find the latest import independently of excluded or empty paths."""
    return run_cmd(
        [
            "git",
            "log",
            "--first-parent",
            "-1",
            "--format=%H",
            "--extended-regexp",
            f"--grep=^vendor: Upgraded {re.escape(name)} to '",
            "HEAD",
            "--",
        ]
    )


def load_vendor_imports():
    """Read local import settings without requiring a tracked manifest."""
    path = os.path.join(git_dir(), "vendor_imports.json")
    try:
        with open(path) as state:
            return json.load(state)
    except (FileNotFoundError, json.JSONDecodeError):
        # This is a cache; a missing or interrupted write can be rebuilt.
        return {}


def remember_vendor_import(name, repo_url, rev, exclude):
    """Remember validated settings for this dependency's current import."""
    imports = load_vendor_imports()
    imports[name] = {
        "commit": last_vendor_import_commit(name),
        "settings": dict(name=name, repo_url=repo_url, rev=rev, exclude=exclude),
    }
    with open(os.path.join(git_dir(), "vendor_imports.json"), "w") as state:
        json.dump(imports, state, indent=2, sort_keys=True)
        state.write("\n")


def last_vendor_import(name):
    """Read import settings from local state or committed manifests."""
    commit = last_vendor_import_commit(name)
    if not commit:
        return None
    # Tie cached rules to the actual import, so checking out another history
    # cannot reuse settings belonging to a different dependency snapshot.
    cached = load_vendor_imports().get(name)
    if cached is not None and cached["commit"] == commit:
        return cached["settings"]
    manifest = committed_vendor_manifest(commit)
    if manifest is None:
        # Older consumers may have committed either format after importing.
        # Use the first snapshot, preserving later rule edits as updates.
        snapshots = run_cmd(
            [
                "git", "log", "--first-parent", "--reverse", "--format=%H",
                "--diff-filter=A", f"{commit}..HEAD", "--",
                VENDOR_JSON_PATH.removeprefix("./"),
                VENDOR_TOML_PATH.removeprefix("./"),
            ]
        ).splitlines()
        if not snapshots:
            return None
        manifest = committed_vendor_manifest(snapshots[0])
        subject = run_cmd(["git", "show", "-s", "--format=%s", commit])
        # A newer revision cannot describe settings used by an earlier import.
        manifest = [
            entry for entry in manifest
            if entry.get("repo_url")
            and subject == f"vendor: Upgraded {name} to '{entry['rev']}'"
        ]
    for entry in manifest:
        if entry["name"] == name:
            return dict(entry, exclude=entry.get("exclude", []))
    return None


def last_subtree_split(target_path):
    """Find the source commit referenced by the latest subtree squash."""
    message = run_cmd(
        [
            "git",
            "log",
            "-1",
            "--format=%B",
            "--extended-regexp",
            f"--grep=^git-subtree-dir: {re.escape(target_path)}/*$",
            "HEAD",
            "--",
        ]
    )
    for line in message.splitlines():
        if line.startswith("git-subtree-split: "):
            return line.split(": ", 1)[1]
    return None


@contextmanager
def filtered_repository(repo_url, rev, patterns):
    """Filter a disposable upstream history, yielding repo, ref, source SHA."""
    if not patterns:
        yield repo_url, rev, None
        return
    if shutil.which("git-filter-repo") is None:
        raise ValueError("'exclude' requires git-filter-repo on PATH")
    # Local URLs are resolved from the consumer, before changing cwd to the
    # temporary bare repository used for fetching and filtering.
    if os.path.exists(repo_url):
        repo_url = os.path.abspath(repo_url)

    with tempfile.TemporaryDirectory(prefix="vendor-filter-") as temporary:
        repository = os.path.join(temporary, "upstream.git")
        subprocess.check_call(["git", "init", "--bare", "--quiet", repository])
        subprocess.check_call(
            [
                "git",
                "-C",
                repository,
                "fetch",
                "--no-tags",
                repo_url,
                rev,
            ]
        )
        source = run_cmd(
            [
                "git",
                "rev-parse",
                "FETCH_HEAD^{commit}",
            ],
            cwd=repository,
        )
        subprocess.check_call(
            [
                "git",
                "-C",
                repository,
                "update-ref",
                "refs/heads/vendor",
                source,
            ]
        )
        subprocess.check_call(
            [
                "git",
                "-C",
                repository,
                "symbolic-ref",
                "HEAD",
                "refs/heads/vendor",
            ]
        )

        # Ask Git to interpret the manifest as the only ignore file in an
        # empty worktree. Upstream .gitignores and user exclusions must not
        # influence filtering. Checking every historical filename also drops
        # deleted paths and keeps directory-only rules distinct from files.
        matcher = os.path.join(temporary, "matcher")
        subprocess.check_call(["git", "init", "--quiet", matcher])
        with open(os.path.join(matcher, ".git", "info", "exclude"), "w") as rules:
            rules.write("\n".join(patterns) + "\n")
        paths = subprocess.check_output(
            [
                "git",
                "log",
                "--format=",
                "--name-only",
                "-z",
                "--no-renames",
                "-m",
                "--root",
                "HEAD",
                "--",
            ],
            cwd=repository,
        )
        paths = b"\0".join(sorted(set(paths.split(b"\0")) - {b""}))
        matches = subprocess.run(
            [
                "git",
                "-C",
                matcher,
                "-c",
                f"core.excludesFile={os.devnull}",
                "-c",
                "core.ignoreCase=false",
                "check-ignore",
                "--no-index",
                "--stdin",
                "-z",
            ],
            input=paths + b"\0" if paths else b"",
            stdout=subprocess.PIPE,
        )
        if matches.returncode not in (0, 1):
            matches.check_returncode()
        excluded_path = os.path.join(temporary, "excluded-paths")
        with open(excluded_path, "wb") as excluded:
            excluded.write(matches.stdout)

        # Load the NUL-delimited byte paths once inside filter-repo. This
        # handles unusual filenames and avoids command-line length limits.
        callback = (
            "global vendor_excluded_paths\n"
            "try:\n"
            "    vendor_excluded_paths\n"
            "except NameError:\n"
            f"    with open({excluded_path!r}, 'rb') as paths:\n"
            "        vendor_excluded_paths = set(paths.read().split(b'\\0'))\n"
            "return None if filename in vendor_excluded_paths else filename\n"
        )
        # Preserve topology and messages so the same ancestor hashes are
        # reproduced when filtering another revision or rebuilding a clone.
        # --force applies exclusively to the disposable repository above.
        subprocess.check_call(
            [
                "git",
                "-C",
                repository,
                "filter-repo",
                "--force",
                "--prune-empty",
                "never",
                "--prune-degenerate",
                "never",
                "--preserve-commit-hashes",
                "--filename-callback",
                callback,
            ]
        )
        yield repository, "refs/heads/vendor", source


def restore_subtree_split(repo_url, previous, split, filtered=None):
    """Rebuild an unreferenced split object lost during clone or Git GC."""
    if not split:
        return
    exists = subprocess.run(
        ["git", "cat-file", "-e", f"{split}^{{commit}}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if exists.returncode == 0:
        return
    if filtered is not None:
        # A normal upgrade with unchanged rules already contains the old
        # filtered ancestor. The upcoming subtree fetch will restore it, so
        # avoid fetching and filtering the same upstream history twice.
        exists = subprocess.run(
            ["git", "-C", filtered, "cat-file", "-e", f"{split}^{{commit}}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if exists.returncode == 0:
            return

    if previous and previous["exclude"]:
        with filtered_repository(
            previous["repo_url"], previous["rev"], previous["exclude"]
        ) as (repository, ref, _):
            rebuilt = run_cmd(["git", "rev-parse", ref], cwd=repository)
            if rebuilt != split:
                raise ValueError("could not reproduce the previous filtered subtree")
            subprocess.check_call(["git", "fetch", "--no-tags", repository, ref])
    else:
        # Older imports and imports without exclusions reference the original
        # upstream commit, which can be fetched directly without filtering.
        subprocess.check_call(["git", "fetch", "--no-tags", repo_url, split])


def is_subtree_up_to_date(target_path, rev, exclude=None):
    previous = last_vendor_import(os.path.relpath(target_path, "vendor"))
    if previous is not None:
        return previous["rev"] == rev and previous["exclude"] == (exclude or [])
    # Existing manifests and pre-feature imports retain their old skip check;
    # adding any exclusion must still perform the first filtered import.
    if exclude:
        return False
    if not os.path.exists(target_path):
        return False
    try:
        last_commit = run_cmd(["git", "log", "-1", "--pretty=%B", target_path])
        return rev in last_commit
    except subprocess.CalledProcessError:
        return False


def require_clean_worktree_for_subtree(target_path, operation="git subtree"):
    """Exit cleanly when tracked changes would make git subtree refuse."""
    # Match git subtree's cleanliness checks: tracked index and worktree
    # changes matter, while unrelated untracked files are safe to leave alone.
    result = subprocess.run(
        ["git", "status", "--short", "--untracked-files=no"],
        text=True,
        stdout=subprocess.PIPE,
        check=True,
    )
    if not result.stdout:
        return

    print(
        f"Cannot update dependency at '{target_path}': {operation} requires "
        "a clean index and working tree.",
        file=sys.stderr,
    )
    print(result.stdout, end="", file=sys.stderr)
    print(
        "Commit or stash these tracked changes, then run vendor.py again.",
        file=sys.stderr,
    )
    raise SystemExit(1)


def manage_subtree(repo_url, name, rev, exclude=None):
    target_path = os.path.join("vendor", name)
    print(f"Adding/updating subtree at {target_path}...")

    exclude = exclude or []
    if is_subtree_up_to_date(target_path, rev, exclude):
        print(
            f"Subtree at '{target_path}' is already at revision '{rev}', skipping update."
        )
        return

    require_clean_worktree_for_subtree(target_path)

    previous = last_vendor_import(name)
    split = last_subtree_split(target_path)
    head_before = run_cmd(["git", "rev-parse", "HEAD"])
    try:
        with filtered_repository(repo_url, rev, exclude) as (repository, ref, _):
            restore_subtree_split(
                repo_url, previous, split, repository if exclude else None
            )
            # An empty import has no tracked prefix. Start a new squash when
            # adding content again: pulling into a missing tree can make Git
            # put upstream files at the consumer root instead of the prefix.
            if split and os.path.isdir(target_path) and not os.listdir(target_path):
                os.rmdir(target_path)
            operation = "pull" if os.path.exists(target_path) else "add"
            subprocess.check_call(
                [
                    "git",
                    "subtree",
                    operation,
                    "--prefix",
                    target_path,
                    repository,
                    ref,
                    "--squash",
                ]
            )
    except ValueError as error:
        print(f"Cannot update subtree at '{target_path}': {error}", file=sys.stderr)
        raise SystemExit(1) from None
    except subprocess.CalledProcessError as error:
        # Git has already printed the actionable cause; suppress Python's
        # implementation-level traceback and add only the affected subtree.
        print(
            f"Failed to update subtree at '{target_path}' "
            f"(git exited with status {error.returncode}).",
            file=sys.stderr,
        )
        raise SystemExit(1) from None

    changed = run_cmd(["git", "rev-parse", "HEAD"]) != head_before
    # Without historical settings, filtered imports are checked by subtree
    # itself. An unchanged import has nothing new to record on repeated runs.
    if not changed and previous is None:
        last_message = run_cmd(
            [
                "git",
                "log",
                "--first-parent",
                "-1",
                "--format=%s",
                "--extended-regexp",
                f"--grep=^vendor: Upgraded {re.escape(name)} to '",
                "HEAD",
                "--",
            ]
        )
        if last_message == f"vendor: Upgraded {name} to '{rev}'":
            remember_vendor_import(name, repo_url, rev, exclude)
            print(
                f"Subtree at '{target_path}' is already at revision '{rev}', skipping update."
            )
            return

    commit_message = f"vendor: Upgraded {name} to '{rev}'\n"

    with tempfile.NamedTemporaryFile(mode="w+", delete=False) as tmpfile:
        tmpfile.write(commit_message)
        tmpfile_path = tmpfile.name

    try:
        # A rule change may produce the same filtered commit. Record it in a
        # new empty commit rather than amending an unrelated existing commit.
        subprocess.check_call(
            [
                "git",
                "commit",
                "--amend" if changed else "--allow-empty",
                "--edit",
                "-F",
                tmpfile_path,
            ]
        )
    except subprocess.CalledProcessError:
        print(f"Failed to amend commit for subtree at {target_path}.")
        sys.exit(1)
    finally:
        os.unlink(tmpfile_path)

    remember_vendor_import(name, repo_url, rev, exclude)
    print(f"Subtree at '{target_path}' is now at revision '{rev}'.\n")


def extract_zip(archive_path, destination, strip_root=None):
    """Validate an archive before extracting and select its import root."""
    with zipfile.ZipFile(archive_path) as archive:
        members = archive.infolist()
        for member in members:
            # Reject traversal, repository metadata, and symlinks rather than
            # allowing archive contents to escape or control the destination.
            parts = member.filename.rstrip("/").split("/")
            mode = member.external_attr >> 16
            if (
                "\\" in member.filename or ":" in parts[0]
                or any(part in ("", ".", "..", ".git") for part in parts)
                or stat.S_ISLNK(mode)
            ):
                raise ValueError(f"Unsafe ZIP entry: {member.filename!r}")
        archive.extractall(destination)
        for member in members:
            # zipfile does not restore executable bits; retain those needed
            # by vendored scripts while excluding special permission bits.
            if not member.is_dir():
                mode = 0o755 if (member.external_attr >> 16) & 0o111 else 0o644
                os.chmod(os.path.join(destination, member.filename), mode)

    entries = os.listdir(destination)
    single_directory = (
        len(entries) == 1 and os.path.isdir(os.path.join(destination, entries[0]))
    )
    if strip_root is True and not single_directory:
        raise ValueError("strip_root=true requires exactly one top-level directory")
    if single_directory and strip_root is not False:
        return os.path.join(destination, entries[0])
    return destination


def exclude_zip_paths(source, patterns, temporary):
    """Apply the same Git ignore rules as subtree imports to ZIP contents."""
    if not patterns:
        return
    matcher = os.path.join(temporary, "matcher")
    subprocess.check_call(["git", "init", "--quiet", matcher])
    with open(os.path.join(matcher, ".git", "info", "exclude"), "w") as rules:
        rules.write("\n".join(patterns) + "\n")
    paths = [
        os.path.relpath(os.path.join(directory, filename), source)
        for directory, _, filenames in os.walk(source) for filename in filenames
    ]
    result = subprocess.run(
        ["git", "-C", matcher, "-c", f"core.excludesFile={os.devnull}",
         "-c", "core.ignoreCase=false", "check-ignore", "--no-index", "--stdin", "-z"],
        input=b"\0".join(os.fsencode(path) for path in paths) + (b"\0" if paths else b""),
        stdout=subprocess.PIPE,
    )
    if result.returncode not in (0, 1):
        result.check_returncode()
    for path in result.stdout.split(b"\0"):
        if path:
            os.unlink(os.path.join(source, os.fsdecode(path)))


def zip_contents_match(source, target):
    """Compare files and executable bits; Git does not track empty directories."""
    def files_below(root):
        return {
            os.path.relpath(os.path.join(directory, filename), root)
            for directory, _, filenames in os.walk(root) for filename in filenames
        }

    paths = files_below(source)
    if paths != files_below(target):
        return False
    for path in paths:
        source_file = os.path.join(source, path)
        target_file = os.path.join(target, path)
        if (
            os.path.islink(target_file)
            or (
                bool(os.stat(source_file).st_mode & 0o111)
                != bool(os.stat(target_file).st_mode & 0o111)
            )
            or not filecmp.cmp(source_file, target_file, shallow=False)
        ):
            return False
    return True


def manage_zip(config):
    """Download a ZIP snapshot and commit only changes below its destination."""
    target = os.path.join("vendor", config.name)
    require_clean_worktree_for_subtree(target, operation="ZIP import")
    # Replacing a directory must not discard local ignored or untracked files,
    # or write through a symlink left outside the replacement mechanism.
    current = target
    while current:
        if os.path.islink(current):
            raise ValueError(f"ZIP destination contains a symlink: {current}")
        current = os.path.dirname(current)
    if os.path.exists(target) and not os.path.isdir(target):
        raise ValueError(f"ZIP destination is not a directory: {target}")
    local_files = run_cmd([
        "git", "status", "--porcelain", "--untracked-files=all", "--ignored", "--", target,
    ])
    if local_files:
        raise ValueError(f"ZIP destination contains local changes or untracked/ignored files: {target}")

    with tempfile.TemporaryDirectory(prefix="vendor-zip-") as temporary:
        archive_path = os.path.join(temporary, "download.zip")
        # Download and validate everything before touching an existing import.
        with urllib.request.urlopen(config.http, timeout=30) as response:
            with open(archive_path, "wb") as archive:
                shutil.copyfileobj(response, archive)
        extracted = os.path.join(temporary, "extracted")
        os.mkdir(extracted)
        source = extract_zip(archive_path, extracted, config.strip_root)
        exclude_zip_paths(source, config.exclude, temporary)
        if zip_contents_match(source, target):
            print(f"ZIP dependency '{config.name}' is unchanged, skipping update.")
            return

        backup = os.path.join(temporary, "previous")
        if os.path.exists(target):
            shutil.move(target, backup)
        try:
            shutil.copytree(source, target)
        except OSError:
            # Retain the old checkout if writing the new snapshot fails.
            if os.path.exists(target):
                shutil.rmtree(target)
            if os.path.exists(backup):
                shutil.move(backup, target)
            raise
        # Imported files belong to the snapshot even if an upstream or local
        # .gitignore would hide them; only explicit exclude rules remove them.
        subprocess.check_call(["git", "add", "--force", "--all", "--", target])
        changed = subprocess.run(["git", "diff", "--cached", "--quiet", "--", target])
        if changed.returncode == 1:
            subprocess.check_call([
                "git", "commit", "--edit", "-m",
                f"vendor: Updated {config.name} from '{config.http}'",
            ])
        elif changed.returncode != 0:
            changed.check_returncode()
    print(f"ZIP dependency '{config.name}' updated at '{target}'.")


def vendor_manifest_path():
    """Select exactly one supported manifest before modifying the worktree."""
    paths = [
        path for path in (VENDOR_JSON_PATH, VENDOR_TOML_PATH) if os.path.exists(path)
    ]
    if len(paths) > 1:
        raise ValueError(
            f"Both '{VENDOR_JSON_PATH}' and '{VENDOR_TOML_PATH}' exist. "
            "Choose one format and remove the other file."
        )
    if not paths:
        raise FileNotFoundError(
            f"Vendor configuration not found: create either '{VENDOR_JSON_PATH}' "
            f"or '{VENDOR_TOML_PATH}'."
        )
    return paths[0]


def parse_vendor_manifest(contents, path):
    """Decode either format using only the Python standard library."""
    if path.endswith(".toml"):
        config_data = tomllib.loads(contents).get("vendor", [])
    else:
        config_data = json.loads(contents)
    if not isinstance(config_data, list):
        raise ValueError(f"Vendor configuration '{path}' must contain a list of entries.")
    if not config_data:
        raise ValueError(f"Vendor configuration file '{path}' is empty.")
    for entry in config_data:
        if not isinstance(entry, dict) or "name" not in entry:
            raise ValueError(
                "Each vendor config must include 'name' and a Git or HTTP source"
            )
    return config_data


def committed_vendor_manifest(commit):
    """Read the format present at an import, independent of today's format."""
    manifests = []
    for path in (VENDOR_JSON_PATH, VENDOR_TOML_PATH):
        result = subprocess.run(
            ["git", "show", f"{commit}:{path.removeprefix('./')}"],
            encoding="utf-8", stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        if result.returncode == 0:
            manifests.append((path, result.stdout))
    if len(manifests) > 1:
        raise ValueError(
            f"Both vendor.json and vendor.toml exist at commit {commit}. "
            "Choose one manifest format."
        )
    if not manifests:
        return None
    path, contents = manifests[0]
    return parse_vendor_manifest(contents, path)


def load_vendor_config(path=None) -> List[VendorConfig]:
    """Load JSON entries or TOML [[vendor]] tables with the same fields."""
    if path is None:
        path = vendor_manifest_path()
    with open(path, "r", encoding="utf-8") as manifest:
        config_data = parse_vendor_manifest(manifest.read(), path)
    return [VendorConfig(**entry) for entry in config_data]


def main(argv=None):
    """Parse command-line arguments and run the requested operation."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Manage git subtree based on vendor.json or vendor.toml"
    )
    parser.add_argument(
        "--update",
        action="store_true",
        help="update vendor.py itself from the latest version and exit",
    )
    parser.add_argument(
        "--restore-replacements",
        action="store_true",
        help=(
            "undo every replacement from vendor_replace.json, putting the "
            "vendored subtree checkouts back in place, and exit"
        ),
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="list the currently active replacements and exit",
    )

    args = parser.parse_args(argv)

    if args.update:
        try:
            update_self()
        except (OSError, ValueError) as error:
            print(f"Cannot update vendor.py: {error}", file=sys.stderr)
            return 1
        return 0

    if args.status:
        print_replacement_status()
        return 0

    if args.restore_replacements:
        restore_all_replacements()
        return 0

    # Reject ambiguous or invalid manifests before applying local replacements.
    try:
        manifest_path = vendor_manifest_path()
        vendor_configs = load_vendor_config(manifest_path)
    except (FileNotFoundError, ValueError) as e:
        print(e)
        return 1

    try:
        replacements = apply_replacements()
    except (ValueError, json.JSONDecodeError) as e:
        print(e)
        return 1
    if replacements:
        print()

    print(f"Updating subtrees according to: '{manifest_path}'\n")
    for vendor in vendor_configs:
        # A replaced dependency is a symlink to a local directory, pulling a
        # subtree into it would write through the link and defeat the purpose.
        if vendor.name in replacements:
            print(
                f"Skipping '{vendor.name}', replaced by "
                f"'{replacements[vendor.name]}'.\n"
            )
            continue
        if vendor.http is not None:
            try:
                manage_zip(vendor)
            except (OSError, ValueError, zipfile.BadZipFile, subprocess.CalledProcessError) as error:
                print(f"Cannot update ZIP dependency '{vendor.name}': {error}", file=sys.stderr)
                return 1
        else:
            manage_subtree(vendor.repo_url, vendor.name, vendor.rev, vendor.exclude)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
