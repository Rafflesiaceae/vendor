# vendor

Updates vendor dependencies via git subtrees or HTTP ZIP archives.

`vendor.py` reads a `vendor/vendor.json` or `vendor/vendor.toml` manifest and imports each dependency under `vendor/<name>`, using Git subtrees or ZIP snapshots. Third-party sources are
checked into your repository at a pinned revision instead of being fetched at
build time.

## Requirements

- Python >= 3.11 (standard library only; no third-party Python packages)
- `git` (with `git subtree` available)
- `git-filter-repo` on `PATH` when any Git dependency uses `exclude`

## Usage

Copy `vendor.py` into the root of your repository and run it from there:

```sh
./vendor.py
```

The script must be run from the repository root, since both the manifest path
and the subtree prefixes are resolved relative to the current directory.

Update the copied script itself to the latest version from the project's main
branch with:

```sh
./vendor.py --update
```

The command reports whether the script was already current, updated, or
downgraded, along with the downloaded version. It exits without reading the
vendor manifest or changing any subtrees.

## Updating the script version

After changing `vendor.py`, refresh the version comment at the top of the
file with:

```sh
./autoversion.py
```

`autoversion.py` excludes the version comment from the SHA-256 checksum. When
the remaining content has changed, it increments the final version component
and records the current date and checksum; otherwise it leaves the file alone.

## Configuration

Create `vendor/vendor.json` containing a list of dependencies. Every Git entry
requires `repo_url`, `name`, and `rev`:

```json
[
  {
    "repo_url": "https://github.com/example/libfoo.git",
    "name": "libfoo",
    "rev": "v1.4.2",
    "exclude": ["docs/", "tests/", "*.pdf"]
  },
  {
    "repo_url": "git@github.com:example/libbar.git",
    "name": "libbar",
    "rev": "9f1c2ab"
  }
]
```

Alternatively, create `vendor/vendor.toml` using the dependency names as table
headers:

```toml
[libfoo]
repo_url = "https://github.com/example/libfoo.git"
rev = "v1.4.2"
exclude = ["docs/", "tests/", "*.pdf"]

[libbar]
repo_url = "git@github.com:example/libbar.git"
rev = "9f1c2ab"
```

Choose exactly one manifest format. If both `vendor/vendor.json` and
`vendor/vendor.toml` exist, the script exits with an error before applying
replacements or updating dependencies. TOML uses Python's standard library
`tomllib` module and requires Python >= 3.11; no Python packages are needed.
In TOML, the table header always supplies the dependency name; no `name` field
is needed. The other fields and exclusions work the same in both formats.
Historical import settings are read from either format, including after
switching formats.

| Field      | Description                                                       |
| ---------- | ----------------------------------------------------------------- |
| `repo_url` | Any URL `git` can clone from.                                     |
| `name`     | Directory name, created at `vendor/<name>`. JSON field or TOML table header. |
| `rev`      | Required for Git sources: a tag, branch, or commit-ish `git` understands. |
| `http`     | HTTP or HTTPS URL to a ZIP archive, used instead of `repo_url`. |
| `strip_root` | Optional ZIP-only boolean overriding the archive layout heuristic. |
| `exclude`  | Optional list of gitignore patterns to remove before importing. Defaults to `[]`. |

### ZIP dependencies

For ZIP archives, a name (JSON field or TOML table header) and `http` are mandatory. Set exactly one of
`repo_url` or `http` for each dependency; ZIP entries do not need `rev`:

```json
[
  {
    "name": "libfoo",
    "http": "https://example.com/releases/libfoo-1.4.2.zip"
  }
]
```

The equivalent TOML entry is:

```toml
[libfoo]
http = "https://example.com/releases/libfoo-1.4.2.zip"
```

By default, an archive containing only one top-level directory has that
directory unwrapped into `vendor/<name>`. If it contains multiple top-level
entries or a single file, all entries are enclosed in `vendor/<name>`.
For example, `release/src/lib.c` becomes `vendor/libfoo/src/lib.c` when
`release/` is the only top-level entry. An archive containing `src/`,
`include/`, and `README` keeps all three inside `vendor/libfoo/`.

Set `strip_root = false` in TOML or `"strip_root": false` in JSON to always
keep the archive layout. Set it to `true` to require and unwrap exactly one
top-level directory; any other layout causes an error. Omit it to use the
heuristic. Exclusion rules apply relative to the selected import root.

Each run downloads the archive and compares its contents and executable
permissions with the existing import. Changed files and deletions are
committed; identical snapshots create no commit. ZIP imports require a
clean tracked working tree and refuse to replace destinations containing
untracked or ignored local files. Local replacements work for ZIP sources
as well. Archive handling uses only Python's standard library, with no
`unzip` command or Python packages required.

### Excluding upstream paths

Set `exclude` independently for each dependency. Patterns follow
[Git's gitignore syntax](https://git-scm.com/docs/gitignore), relative to the
upstream repository root, rather than `vendor/<name>`:

```json
"exclude": ["/docs/", "**/tests/", "*.pdf", "!manual.pdf"]
```

A leading `/` anchors a pattern at the upstream root, a trailing `/` matches
directories, and `**` matches across directory levels. Later `!` rules can
keep matching files, subject to gitignore's rule that an excluded parent
directory must be included again before its children can be included. Each
list item is one gitignore line; backslashes must be escaped as required by the chosen format.
Only the manifest rules apply, independent of upstream `.gitignore` files
and your personal Git ignore settings.

The script fetches the selected revision's complete history into a temporary
repository and rewrites it with
[`git-filter-repo`](https://github.com/newren/git-filter-repo), then imports
the result with the usual squashed subtree operation. The upstream and
consumer histories are not rewritten. Reusing the rules reproduces the same
filtered ancestor commits, and excluded files are removed before merging,
so later upstream changes to those files cannot cause merge conflicts.
Local edits to retained files still participate in normal three-way merges
and can conflict with overlapping upstream edits.

Changing or removing `exclude` triggers an update even when `rev` stays the
same. Adding rules to an existing dependency removes matching vendored
files; removing rules restores upstream files. Local edits to files being
removed can require conflict resolution.

Successful imports remember their settings locally in
`.git/vendor_imports.json`, tied to each dependency's import commit. This
lets unchanged dependencies skip fetching and filtering even when the
manifest is untracked. Without local settings, the script reads
the JSON or TOML manifest at the import commit, or its first committed snapshot
afterward if that snapshot still names the imported revision.

With recorded settings, after a fresh clone or garbage collection, the
script can rebuild a missing filtered split from that revision and its
original rules, including before a downgrade or a rule change. Keep
revisions pinned to unchanged tags or commit hashes that remain fetchable.

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

1. The revision and exclusion list from the manifest at the latest import
   are checked. If both match the current manifest, the dependency is skipped.
   Unrecognized imports use the revision check on the last commit touching
   the subtree.
2. Otherwise upstream history is filtered if exclusions are configured, then
   `git subtree add --squash` is run for a new dependency, or
   `git subtree pull --squash` for an existing one. A fully excluded subtree
   has no directory; adding content again starts a new squash import.
3. The resulting commit is amended to the message
   `vendor: Upgraded <name> to '<rev>'`, which is what step 1 matches against
   on the next run.

Because the squashed subtree history is committed to your repository, the
vendored sources are available to anyone who clones it without any extra
fetch step.

## Notes

- Adding or updating a subtree requires a clean index and working tree because
  `git subtree` creates a commit for the imported snapshot. Commit or stash
  tracked changes first. Untracked files do not block an update.
- For imports without a recognized import message, the up-to-date check is a
  substring match on the last commit message, so
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
