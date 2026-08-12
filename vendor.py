#!/usr/bin/env python3
# vendor v0.3 (2026-08-30) (cdc6dcbcfea94d00)
#
# Updates vendor dependencies via git subtrees
#
# REQUIRES: python git
import json
import os
import shutil
import subprocess
import sys
import tempfile

from dataclasses import dataclass, asdict
from typing import Dict, List

VENDOR_DIR = "./vendor"
VENDOR_JSON_PATH = "./vendor/vendor.json"
VENDOR_REPLACE_JSON_PATH = "./vendor/vendor_replace.json"

# Markers delimiting the section this script owns inside `.git/info/exclude`.
# Everything between them is rewritten on every run, everything outside of
# them is left untouched.
EXCLUDE_BEGIN = "# BEGIN vendor.py replace (managed section, do not edit)"
EXCLUDE_END = "# END vendor.py replace"


@dataclass
class VendorConfig:
    repo_url: str
    name: str
    rev: str


def run_cmd(cmd, cwd=None):
    try:
        result = subprocess.check_output(cmd, shell=False, cwd=cwd, text=True).strip()
        return result
    except subprocess.CalledProcessError as e:
        print(f"Command failed: {' '.join(e.cmd)}")
        sys.exit(1)


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


def is_subtree_up_to_date(target_path, rev):
    if not os.path.exists(target_path):
        return False
    try:
        last_commit = run_cmd(["git", "log", "-1", "--pretty=%B", target_path])
        return rev in last_commit
    except subprocess.CalledProcessError:
        return False


def manage_subtree(repo_url, name, rev):
    target_path = os.path.join("vendor", name)
    print(f"Adding/updating subtree at {target_path}...")

    if is_subtree_up_to_date(target_path, rev):
        print(
            f"Subtree at '{target_path}' is already at revision '{rev}', skipping update."
        )
        return

    if os.path.exists(target_path):
        subprocess.check_call(
            [
                "git",
                "subtree",
                "pull",
                "--prefix",
                target_path,
                repo_url,
                rev,
                "--squash",
            ]
        )
    else:
        subprocess.check_call(
            [
                "git",
                "subtree",
                "add",
                "--prefix",
                target_path,
                repo_url,
                rev,
                "--squash",
            ]
        )

    commit_message = f"vendor: Upgraded {name} to '{rev}'"

    with tempfile.NamedTemporaryFile(mode="w+", delete=False) as tmpfile:
        tmpfile.write(commit_message)
        tmpfile_path = tmpfile.name

    try:
        subprocess.check_call(
            ["git", "commit", "--amend", "--edit", "-F", tmpfile_path]
        )
    except subprocess.CalledProcessError:
        print(f"Failed to amend commit for subtree at {target_path}.")
        sys.exit(1)
    finally:
        os.unlink(tmpfile_path)

    print(f"Subtree at '{target_path}' is now at revision '{rev}'.\n")


def load_vendor_config() -> List[VendorConfig]:
    if not os.path.exists(VENDOR_JSON_PATH):
        raise FileNotFoundError(
            f"Vendor configuration file '{VENDOR_JSON_PATH}' not found."
        )
    with open(VENDOR_JSON_PATH, "r") as f:
        config_data = json.load(f)
        if not config_data:
            raise ValueError(
                f"Vendor configuration file '{VENDOR_JSON_PATH}' is empty."
            )
        configs = []
        for vendor in config_data:
            if not all(k in vendor for k in ("repo_url", "name", "rev")):
                raise ValueError(
                    "Each vendor config must include 'repo_url', 'name', and 'rev'"
                )
            configs.append(VendorConfig(**vendor))
        return configs


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Manage git subtree based on vendor.json"
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

    args = parser.parse_args()

    if args.status:
        print_replacement_status()
        sys.exit(0)

    if args.restore_replacements:
        restore_all_replacements()
        sys.exit(0)

    try:
        replacements = apply_replacements()
    except (ValueError, json.JSONDecodeError) as e:
        print(e)
        sys.exit(1)
    if replacements:
        print()

    try:
        vendor_configs = load_vendor_config()
    except (FileNotFoundError, ValueError) as e:
        print(e)
        sys.exit(1)

    print(f"Updating subtrees according to: '{VENDOR_JSON_PATH}'\n")
    for vendor in vendor_configs:
        # A replaced dependency is a symlink to a local directory, pulling a
        # subtree into it would write through the link and defeat the purpose.
        if vendor.name in replacements:
            print(
                f"Skipping '{vendor.name}', replaced by "
                f"'{replacements[vendor.name]}'.\n"
            )
            continue
        manage_subtree(vendor.repo_url, vendor.name, vendor.rev)
