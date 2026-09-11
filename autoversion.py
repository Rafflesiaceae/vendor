#!/usr/bin/env python3
"""Update a source-file version line when the rest of the file changes."""

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

# XXH64's primes and mask are fixed by the algorithm. Keeping the
# implementation here avoids adding a package dependency just for one hash.
MASK64 = (1 << 64) - 1
PRIME64_1 = 11_400_714_785_074_694_791
PRIME64_2 = 14_029_467_366_897_019_727
PRIME64_3 = 1_609_587_929_392_839_161
PRIME64_4 = 9_650_029_242_287_828_579
PRIME64_5 = 2_870_177_450_012_600_261


def rotate_left(value, count):
    """Rotate a 64-bit integer left by ``count`` bits."""
    return ((value << count) | (value >> (64 - count))) & MASK64


def xxh64_round(accumulator, lane):
    """Mix one eight-byte lane into an XXH64 accumulator."""
    accumulator = (accumulator + lane * PRIME64_2) & MASK64
    accumulator = rotate_left(accumulator, 31)
    return (accumulator * PRIME64_1) & MASK64


def xxh64(data, seed=0):
    """Return the XXH64 digest of ``data`` as an unsigned integer."""
    length = len(data)
    offset = 0

    if length >= 32:
        accumulators = [
            (seed + PRIME64_1 + PRIME64_2) & MASK64,
            (seed + PRIME64_2) & MASK64,
            seed & MASK64,
            (seed - PRIME64_1) & MASK64,
        ]

        # Four interleaved accumulators process each 32-byte stripe.
        stripe_end = length - 32
        while offset <= stripe_end:
            for index in range(4):
                lane = int.from_bytes(data[offset : offset + 8], "little")
                accumulators[index] = xxh64_round(accumulators[index], lane)
                offset += 8

        result = sum(
            rotate_left(accumulator, rotation)
            for accumulator, rotation in zip(accumulators, (1, 7, 12, 18))
        ) & MASK64

        # Fold each accumulator back into the combined state.
        for accumulator in accumulators:
            result ^= xxh64_round(0, accumulator)
            result = (result * PRIME64_1 + PRIME64_4) & MASK64
    else:
        result = (seed + PRIME64_5) & MASK64

    result = (result + length) & MASK64

    # Mix the remaining full words, optional half-word, and tail bytes.
    while offset + 8 <= length:
        lane = int.from_bytes(data[offset : offset + 8], "little")
        result ^= xxh64_round(0, lane)
        result = (rotate_left(result, 27) * PRIME64_1 + PRIME64_4) & MASK64
        offset += 8

    if offset + 4 <= length:
        lane = int.from_bytes(data[offset : offset + 4], "little")
        result = (result ^ lane * PRIME64_1) & MASK64
        result = (rotate_left(result, 23) * PRIME64_2 + PRIME64_3) & MASK64
        offset += 4

    while offset < length:
        result = (result ^ data[offset] * PRIME64_5) & MASK64
        result = (rotate_left(result, 11) * PRIME64_1) & MASK64
        offset += 1

    # Avalanche the state so every input bit affects the whole digest.
    result ^= result >> 33
    result = (result * PRIME64_2) & MASK64
    result ^= result >> 29
    result = (result * PRIME64_3) & MASK64
    result ^= result >> 32
    return result


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

    checksum = f"{xxh64(output):x}".encode("ascii")
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


def main(arguments=None):
    """Run the command-line interface."""
    arguments = sys.argv[1:] if arguments is None else arguments
    if not arguments or len(arguments) == 1 and arguments[0] in ("-h", "--help"):
        print(f"pass me a file, {HELP_TEXT}")
        return 0
    if len(arguments) > 1:
        print("autoversion: expected at most one path", file=sys.stderr)
        return 2

    try:
        autoversion_file(arguments[0])
    except (OSError, ValueError) as error:
        print(f"autoversion: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
