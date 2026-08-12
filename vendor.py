#!/usr/bin/env python3
# vendor v0.2 (2026-08-12) (56106c0979c0b6e7)
#
# Updates vendor dependencies via git subtrees
#
# REQUIRES: python git
import json
import os
import subprocess
import sys
import tempfile

from dataclasses import dataclass, asdict
from typing import List

VENDOR_JSON_PATH = "./vendor/vendor.json"


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

    args = parser.parse_args()

    try:
        vendor_configs = load_vendor_config()
    except (FileNotFoundError, ValueError) as e:
        print(e)
        sys.exit(1)

    print(f"Updating subtrees according to: '{VENDOR_JSON_PATH}'\n")
    for vendor in vendor_configs:
        manage_subtree(vendor.repo_url, vendor.name, vendor.rev)
