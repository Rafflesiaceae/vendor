# vendor

Updates vendor dependencies via git subtrees.

`vendor.py` reads a `vendor/vendor.json` manifest and adds or updates a git
subtree under `vendor/<name>` for every entry, so third-party sources are
checked into your repository at a pinned revision instead of being fetched at
build time.

## Requirements

- Python 3 (standard library only)
- `git` (with `git subtree` available)

## Usage

Copy `vendor.py` into the root of your repository and run it from there:

```sh
./vendor.py
```

The script must be run from the repository root, since both the manifest path
and the subtree prefixes are resolved relative to the current directory.

## Configuration

Create `vendor/vendor.json` containing a list of dependencies. Every entry
requires all three fields:

```json
[
  {
    "repo_url": "https://github.com/example/libfoo.git",
    "name": "libfoo",
    "rev": "v1.4.2"
  },
  {
    "repo_url": "git@github.com:example/libbar.git",
    "name": "libbar",
    "rev": "9f1c2ab"
  }
]
```

| Field      | Description                                                       |
| ---------- | ----------------------------------------------------------------- |
| `repo_url` | Any URL `git` can clone from.                                     |
| `name`     | Directory name of the subtree, created at `vendor/<name>`.        |
| `rev`      | Revision to pull — a tag, branch, or commit-ish `git` understands. |

## How it works

For each entry in the manifest:

1. The last commit touching `vendor/<name>` is inspected. If its message
   mentions `rev`, the dependency is considered up to date and skipped.
2. Otherwise `git subtree add --squash` is run for a new dependency, or
   `git subtree pull --squash` for an existing one.
3. The resulting commit is amended to the message
   `vendor: Upgraded <name> to '<rev>'`, which is what step 1 matches against
   on the next run.

Because the squashed subtree history is committed to your repository, the
vendored sources are available to anyone who clones it without any extra
fetch step.

## Notes

- The up-to-date check is a substring match on the last commit message, so
  short revisions that are prefixes of one another can collide. Prefer full
  commit hashes or unambiguous tags.
- Pinning to a branch name works, but the check will consider the subtree up
  to date as long as the previous commit message mentions that branch. Use a
  tag or commit hash when you want reproducible updates.
- The commit amend step opens your editor (`--edit`), giving you a chance to
  review or extend the generated message.
