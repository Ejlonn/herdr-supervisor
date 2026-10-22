# Quota plugin dependency

The supervisor reads provider quota snapshots that the Herdr plugin `herdr-agent-quota` writes, and it rechecks quota through that plugin's non-LLM `refresh` action (`herdr plugin action invoke refresh --plugin herdr-agent-quota`). Nothing at run time contacts GitHub: once the plugin is installed and writing snapshots, a repository outage cannot stop a supervised task. The dependency matters when a **new project or user installs from scratch**, so this repository pins exactly one revision and ships a bounded tool to check and stage it. The controller, the snapshot contract and the refresh command are unchanged.

## The pinned dependency

The single source of truth is `config/quota-dependency.json` (installed beside the program files as `quota-dependency.json`). Do not edit it to follow a branch; a new revision needs a reviewed change to that file.

| Field | Value |
|---|---|
| Plugin ID | `herdr-agent-quota` |
| Repository (owned fork) | `https://github.com/Ejlonn/herdr-agent-quota` |
| Revision | `809749e585480047e502df072fb8ddccc4719d4e` (an exact commit; never a branch or tag) |
| Manifest version | 1.5.4 (needs Herdr 0.9.0 or newer) |
| Git tree | `d47a762885a8acefa85f362ce48cccbf1bb0389d` |
| License | MIT, `Copyright (c) 2026 Levi Qiao`; the `LICENSE` file is part of the pinned tree and is hash-pinned |
| Toolchain | Rust 1.95.0 (`rust-toolchain.toml`), `cargo build --release --locked` against the hash-pinned `Cargo.lock` |
| Upstream provenance | originally published at `levi-qiao/herdr-agent-quota` (that address no longer resolves); the project moved to `https://github.com/praxstack/herdr-agent-quota` |

The fork's `main` branch is far ahead of this revision (151 commits when pinned) and has not been reviewed; it is **not** a supported revision. This pin is the revision that was installed, clean and working when the dependency was frozen. Moving to a newer revision means reading its changes, updating the lock and the recovery hashes, and repeating the validation.

Recovery artifacts the lock lists by SHA-256 (the files are distributed separately from this repository; see [Recovery package](#recovery-package)):

| Artifact | SHA-256 |
|---|---|
| `herdr-agent-quota-809749e-source.tar.gz` (tracked source of the locked commit, MIT license included) | `0f43db667d95b5ac79392a2bf561fad0fa6c6bb6cfd6da39e9191663a945aa00` |
| `herdr-agent-quota-809749e.bundle` (a **single-commit** snapshot: the commit's two parents are absent, so it cannot be fetched for history; the tool reads it by unpacking objects) | `6761d16b8546ba0c6353b943961ee71e9267d3b2076d21765bf9383fe0dfdba7` |

## Four separate questions

A fresh machine can fail at four independent points. `herdr-quota-dependency check` reports the first three and never claims the fourth; none of them implies another.

| Question | How it is established | When it is not established |
|---|---|---|
| Is Herdr available? | `herdr plugin list --json` runs | **UNKNOWN**: install Herdr user-scoped at `~/.local/bin/herdr`; absence of the plugin is never inferred from a failed inventory |
| Is the plugin installed at the locked revision? | the inventory entry for `herdr-agent-quota` has the locked commit and version (a locally linked plugin counts when it is a clean git checkout of that commit, **or** when its actual source content matches the lock: every file except top-level `target/` build output is hashed from disk and compared with the locked critical-file hashes, plugin id, versions, toolchain channel, content manifest and git tree id. No git metadata is needed, so the directory that `bootstrap` stages passes after it is linked; a receipt or claimed revision is never trusted) | `absent` (bootstrap), or `conflict` (a human decides; nothing is overwritten) |
| Is there a fresh snapshot, and is it this session's? | snapshot file present, valid JSON, younger than `--max-age` (default 900 s). **Identity** (the exact session binding) is judged by the supervisor, not by this tool: run `herdr-supervisor doctor` | usage is **UNKNOWN**, never 0%; another session's windows are never borrowed; a stale snapshot is never current usage |
| Are provider logins, the Claude StatusLine and Herdr session binding set up? | not read; these are separate operator steps (see the plugin's own documentation) | the tool says so and reads no credential |

## Check (read-only)

```sh
herdr-quota-dependency check            # human-readable
herdr-quota-dependency --json check     # machine-readable
herdr-quota-dependency check --require-telemetry   # also exit 3 unless every snapshot is FRESH
```

Exit codes: `0` installed at the locked revision (existing installation preserved, including one whose recorded origin is the old address, since only the commit matters); `3` action required (Herdr or plugin missing, inventory unreadable, build prerequisite missing, or telemetry required but not fresh); `4` conflict (another revision or version, duplicate plugin ids, or a local link whose source content differs from the lock in any way: edited, added, deleted or re-linked files, changed modes, other plugin id or version): preserved, nothing changed, a human decides. It reads the plugin inventory and snapshot file ages and validity only; it never starts a session, requests a refresh, reads a credential or calls a provider.

## Bootstrap (explicit; dry run by default)

```sh
# Review first. Offline, writes nothing; a lock-listed local artifact is fully verified in a removed temporary directory.
herdr-quota-dependency bootstrap --stage-dir "$HOME/quota-stage" --from-archive /path/to/herdr-agent-quota-809749e-source.tar.gz
# Then stage, verify and build (network only for the repository route):
herdr-quota-dependency bootstrap --apply --stage-dir "$HOME/quota-stage"                       # exact commit from the owned fork
herdr-quota-dependency bootstrap --apply --stage-dir "$HOME/quota-stage" --from-archive FILE   # lock-listed recovery archive (or --from-bundle)
herdr-quota-dependency bootstrap --apply --stage-dir "$HOME/quota-stage" --fallback-archive FILE   # fork first, archive only if the fork cannot supply the commit
```

Order of work, each step stopping before the next:

1. The plugin inventory is read first. Already installed at the locked revision: reported and preserved, nothing staged. A conflict or an unreadable inventory stops the run.
2. The `--stage-dir` must be absolute, new or empty, not a symlink, and not inside Herdr's configuration directory. An existing directory is never overwritten.
3. The build prerequisites are checked. **The lock, not your environment, selects the toolchain:** every toolchain command runs with `RUSTUP_TOOLCHAIN` set to the locked channel (1.95.0) and `RUSTUP_AUTO_INSTALL=0`; an inherited `RUSTUP_TOOLCHAIN`, `RUSTC`, `RUSTFLAGS`, `RUSTC_WRAPPER`, `CARGO_TARGET_DIR` and every other variable outside a short allowlist are dropped. `cargo -vV` and `rustc -vV` must each report release 1.95.0, otherwise the run stops before anything is staged.
4. Source is acquired: a recovery artifact must match a SHA-256 in the lock **before** any byte of it is interpreted; the repository route fetches exactly the locked commit (depth 1, no tags, with no inherited git configuration, credential helper or prompt, and only https or local paths) and follows no branch.
5. Extraction accepts only regular files, directories and the lock's exact `CLAUDE.md -> AGENTS.md` symlink. Absolute or traversal paths, other symlinks, hard links, devices, duplicate or case-colliding names, members below a symlink, and anything over 2,000 members or the size limits stop the run.
6. The tree is verified against the lock: critical-file hashes (`LICENSE`, `Cargo.lock`, `Cargo.toml`, `herdr-plugin.toml`, `rust-toolchain.toml`), plugin id, manifest and `Cargo.toml` versions, toolchain channel, the whole-tree content manifest and the git tree id.
7. `cargo build --release --locked` (plus `--offline` if requested) runs in the verified copy with a scrubbed environment (no registry or GitHub tokens).
8. After the build, `target/.rustc_info.json` (the compiler cargo itself recorded, whatever configuration chose it) must name release 1.95.0 and nothing else; an unknown or different compiler stops the run. Only then is `BOOTSTRAP_RECEIPT.json` written.

**Ownership.** Before any acquisition the run claims the stage directory exclusively: a lock file `.bootstrap.lock` created with `O_EXCL`, then a check that nothing else is in the directory. A second `--apply` on the same directory, even one that validated it as empty before the first wrote anything, is refused without touching the first one's output. The temporary directory, the final directory (created with `mkdir`, so a pre-existing one is refused) and the receipt (`O_EXCL`) are created exclusively, and a failure removes only what *this* invocation created and its own lock; foreign content that appears later is left alone and reported. On success the lock is removed and the stage directory holds the source directory and the receipt. A `.bootstrap.lock` left by a run that was killed makes the directory non-empty: confirm no bootstrap is running and choose a new `--stage-dir` (nothing in the old one is deleted for you). Processes that ignore the lock and write inside this run's private directories are out of scope.

Exit codes: `0` verified (or nothing to do); `3` something is missing (source, toolchain, network, build failed); `4` refused (conflict, hash or tree mismatch, unsafe archive). A verification failure of an artifact you supplied is never skipped past to another source.

**The tool never** runs `herdr plugin install`, `link`, `enable`, `update` or `uninstall`, runs the plugin's `configure` action, runs the upstream `install.sh` (which configures providers and edits hooks and settings), reads credentials, requests a quota refresh, or calls a provider or model. It prints the next steps instead.

### What you do afterwards (explicit operator steps)

These are printed by `bootstrap` and are **not** run by it. The live link and enable path below was **NOT VALIDATED** (it changes live Herdr plugin configuration, which was out of scope); validate it in a disposable Herdr environment first.

After the link, `check` verifies the linked directory's **content** against the lock (it does not need git metadata and does not trust the receipt); a changed source file turns it into a `conflict`. Top-level `target/` is build output and is not part of the locked source: the check proves the source, not the binary (the receipt records the binary's SHA-256 for your own comparison).

```sh
herdr plugin link --disabled "$HOME/quota-stage/herdr-agent-quota-809749e58548"   # registers without enabling (help: link --disabled)
herdr plugin enable herdr-agent-quota                                              # starts the plugin's startup and event hooks
herdr-quota-dependency check                                                       # confirm the locked revision and snapshot freshness
```

`herdr plugin action invoke configure --plugin herdr-agent-quota` is a separate, optional decision: the plugin's configure action edits provider hook and settings files. Read what it changes first; this repository never runs it. Whether `herdr plugin install Ejlonn/herdr-agent-quota --ref REVISION` honors a full commit id is **NOT VALIDATED** (the help lists only `--ref <REF>`); if you use it, run `check` afterwards and accept only the locked revision. The plugin's own manifest builds with `cargo build --release` (without `--locked`); the supported preparation here builds with `--locked`.

## Offline limits

A source archive is not an offline installation. A cargo configuration file in `CARGO_HOME` or a parent directory (for example `build.rustc`) is not neutralized, but a build that used another compiler is detected afterwards and nothing is published; use a clean `--cargo-home` to avoid it. The build needs the pinned **Rust 1.95.0 toolchain** and **the crates listed in `Cargo.lock`**: from a warm cargo home (`--cargo-home DIR --offline`), from crates.io, or from a vendored copy you prepare yourself. The recovery archive contains neither. If those are unavailable the build step fails, `bootstrap` reports it and stages nothing; that path is **NOT VALIDATED** for your machine until the build has run there. Normal supervisor operation needs none of this once the plugin is built and installed.

## Existing installations

An installation at the locked commit is preserved whatever its recorded origin. Do not run `herdr plugin update` for this plugin without reviewing it: it would move a GitHub-installed plugin along its repository, which is exactly what the pin prevents. An installation at another revision is reported as `conflict`; neither upgrade nor downgrade is performed. Re-pointing an existing install's recorded origin is a separate operator decision and is not needed to keep working.

## Rollback

`bootstrap --apply` writes only inside the new `--stage-dir` and only that directory needs removing. After checking that the path is the stage directory you created:

```sh
STAGE="$HOME/quota-stage"
rm -r -- "${STAGE:?}"
```

If you linked the staged copy, first run `herdr plugin unlink herdr-agent-quota` (an operator action). Nothing else is created or changed.

## Recovery package

Maintainers prepare the redistributable recovery set (source archive, bundle, `LICENSE`, `PROVENANCE.md`, `SHA256SUMS`) outside this repository tree and publish it as a release of the owned fork, together with the SHA-256 values already recorded in the lock. Publication is a separate human action; a local private copy is a recovery copy, not a delivered fallback. Verify any downloaded artifact against `SHA256SUMS` and the lock before use; `bootstrap` refuses an artifact whose hash is not in the lock. External hosts can still fail; the lock plus an independently retained artifact is what removes the dependence on one address.

## Provenance and license

`herdr-agent-quota` is MIT-licensed, Copyright (c) 2026 Levi Qiao. The pinned tree includes the license text and the lock hashes it. The supervisor itself is Apache-2.0; the plugin is a separately installed component and is not vendored into this repository.
