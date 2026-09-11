#!/usr/bin/env python3
"""Update a source-file version line when the rest of the file changes."""

import hashlib
import re
import sys

from datetime import date
from pathlib import Path


HELP_TEXT = (
    "I look for a version-line like this:\n"
    "# some prefix v0.1 (2020-09-06) (45142f9a49d45793)"
)
VERSION_LINE = re.compile(
    rb"^(.*) v([\d.]+) \(([\d-]+)\) \(([\da-z]+)\)([ \-><]*)$"
)

# Resolve the target beside this utility so invocation does not depend on cwd.
VENDOR_PATH = Path(__file__).resolve().with_name("vendor.py")


def scanned_lines(contents):
    """Yield lines with the newline handling used by Go's bufio.Scanner."""
    offset = 0
    while offset < len(contents):
        newline = contents.find(b"\n", offset)
        if newline == -1:
            line = contents[offset:]
            offset = len(contents)
        else:
            line = contents[offset:newline]
            offset = newline + 1

        # bufio.ScanLines removes a carriage return immediately before LF.
        if line.endswith(b"\r"):
            line = line[:-1]
        yield line


def autoversion_file(path):
    """Update ``path`` and return whether its version line changed."""
    source_path = Path(path)
    output = bytearray()
    version_match = None
    version_line_number = None
    version_line_offset = None

    for line_number, line in enumerate(scanned_lines(source_path.read_bytes())):
        match = VERSION_LINE.fullmatch(line)
        if match is None:
            output.extend(line)
            output.extend(b"\n")
            continue

        if version_match is not None:
            raise ValueError(
                "more than one version line matched on lines "
                f"{version_line_number} and {line_number}"
            )

        # The version line is omitted from the content whose hash it stores.
        version_match = match
        version_line_number = line_number
        version_line_offset = len(output)

    if version_match is None:
        print(f"autoversion: no-version {path}\n\n{HELP_TEXT}")
        return False

    checksum = hashlib.sha256(output).hexdigest().encode("ascii")
    if version_match.group(4) == checksum:
        print(f"autoversion: up-to-date {path}")
        return False

    version_parts = version_match.group(2).split(b".")
    version_parts[-1] = str(int(version_parts[-1]) + 1).encode("ascii")
    new_version_line = b"".join(
        (
            version_match.group(1),
            b" v",
            b".".join(version_parts),
            f" ({date.today().isoformat()}) (".encode("ascii"),
            checksum,
            b")",
            version_match.group(5),
            b"\n",
        )
    )

    # Reinsert the refreshed metadata at its original byte offset.
    output[version_line_offset:version_line_offset] = new_version_line
    source_path.write_bytes(output)
    print(f"autoversion:    updated {path}")
    return True


def main():
    """Autoversion the repository's vendor.py."""
    try:
        autoversion_file(VENDOR_PATH)
    except (OSError, ValueError) as error:
        print(f"autoversion: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
