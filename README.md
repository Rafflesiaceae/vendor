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

## Replacing a dependency with a local checkout

Analogous to go mod's `replace` directive, a dependency can be pointed at a
local working copy while you develop against it. Create an optional
`vendor/vendor_replace.json` mapping dependency names to directories:

```json
{
  "libfoo": "../../checkouts/libfoo",
  "libbar": "/home/you/src/libbar"
}
```

Paths are resolved relative to `./vendor` (absolute paths are used as is), so
`../../checkouts/libfoo` refers to a `checkouts/` directory next to your
repository. On the next run, `vendor/libfoo` becomes a symlink to that
directory:

```sh
./vendor.py            # apply replacements, then update the remaining subtrees
./vendor.py --status   # show which dependencies are currently replaced
./vendor.py --restore-replacements   # put the vendored checkouts back
```

Replaced dependencies are skipped during the subtree update, since pulling
into a symlink would write straight into your local checkout.

Removing an entry from `vendor_replace.json` (or deleting the file) restores
the vendored checkout on the next run.

### Staying invisible to git

The replacement never shows up in `git status` and can never be committed by
accident. Three things would otherwise give it away, so the script handles
each of them:

| Would show up as                  | Handled by                                     |
| --------------------------------- | ---------------------------------------------- |
| the subtree files deleted         | the `skip-worktree` index bit on those files    |
| the symlink as an untracked file  | a managed section in `.git/info/exclude`        |
| the checkout being gone           | parking it inside `.git/`, restored on undo     |

Everything the feature needs to remember lives in `.git/vendor_replace/`
(state file plus the parked checkouts), and `.git/info/exclude` is per-clone
and never committed, so nothing of this leaks into the repository. Commits
made while a replacement is active still carry the original vendored content,
because the index is untouched. The `vendor_replace.json` file itself is
excluded too, since it holds machine-local paths — if you would rather track
it, `git add -f` it once and the exclude entry becomes a no-op.

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
- While a replacement is active, the vendored files carry the `skip-worktree`
  bit, so git refuses to overwrite them on operations such as `git checkout`
  of a branch that changes them. Run `./vendor.py --restore-replacements`
  first if a git command complains about those paths.
- `git clean -x` removes the ignored symlink. Re-running `./vendor.py`
  recreates it, and `--restore-replacements` still finds the parked checkout.
