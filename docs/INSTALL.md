# Installation, update and rollback

Requires Linux with user systemd, Python 3.11 or newer, and Herdr 0.9.0 or newer installed user-scoped at `~/.local/bin/herdr` (the minimum tested CLI contract). `herdr-supervisor doctor` fails with an error when the version or a required command/option is missing and warns when only the optional prompt acknowledgement is absent. Herdr itself, the Codex and Claude CLIs and their account logins, a Telegram bot token and your owner id, and the choice of your existing Codex/Claude session pair are **your** steps: `herdr-setup` guides them and never performs them (no root, no package manager, no model prompt, no provider login, no session creation).

## Quick start

```sh
./install.sh --dry-run          # preview: writes nothing, runs no service command
./install.sh                    # minimal, inactive installation of the program files
herdr-setup status              # what is installed and what is still missing
```

`./install.sh` (and the marketplace `setup` action, which runs it) is the **minimal profile**: it installs the program files, launchers, disabled unit definitions, documents and examples, and creates a configuration file only when it is absent. It never loads, enables, starts or restarts a service and never replaces an installed release: an existing legacy or different installation is refused, with the next command to run. The result is honestly *installed but inactive*.

## `herdr-setup`

One standard-library command with four operations. **Every operation previews by default**: it writes no target file, contacts no download endpoint and activates nothing. A plan file is an explicit extra output (`--plan-out FILE`, created exclusively, mode 0600).

| Command | What it does |
|---|---|
| `herdr-setup setup [--profile minimal\|full]` | Install (minimal) or install and configure selected components (full) |
| `herdr-setup update [--adopt-legacy-from DIR]` | Replace the installed release with a reviewed local tree |
| `herdr-setup status` | Read-only installation and readiness report |
| `herdr-setup rollback` | Undo the last operation (previous release and its assets) |

A preview ends with an **approval hash**. The hash covers the candidate's file contents, the options, the resolved target (home, directories, uid), the exact preimage of every file that may change, the service actions and the ordered action list, and the running tool itself. Apply spends exactly one approval:

```sh
herdr-setup update --payload /path/to/reviewed/tree --plan-out update.plan   # review the printed actions
herdr-setup update --apply --plan update.plan --approve HASH                 # non-interactive apply
herdr-setup setup --interactive                                              # asks, shows one complete preview, asks for the hash prefix
```

If the candidate, an option, the target or any preimage changed after the preview, the approval is stale and nothing is written. `--approve-current` computes and approves the plan in one step (that is what `./install.sh` does).

### Payload

`--payload DIR` names a source checkout or a complete source release. The tool reads only an explicit allowlist, hashes the very bytes it later publishes, and refuses symbolic links, hard links, special files, unexpected names or directories, unsafe permission bits, more than 256 files, oversized files, or a `VERSION` that disagrees with `herdr_core.py`. Nothing in the payload is imported or run while it is inspected. A wheel alone contains the console tools but not the units, launchers and documents, so `herdr-setup` reports a missing payload instead of pretending; there is no remote branch following, no curl-to-shell, no background updater and no automatic dependency-pin movement.

### Layout and atomicity

```
~/.local/lib/herdr-supervisor/active -> generations/<version>-<program id>   (one pointer, one rename)
~/.local/lib/herdr-supervisor/generations/<version>-<program id>/            (complete, read-only, verified by GENERATION.json)
~/.local/bin/herdr-*  ~/.config/systemd/user/herdr-*  ~/.local/share/doc/herdr-supervisor/   (enumerated assets)
~/.local/state/herdr-supervisor/installation/   (receipt.json, ops/ journals, backups/, deploy.lock)
```

Every launcher resolves `active` exactly once and runs the concrete directory, so concurrent invocations during an update each use one complete release. The previous generation is kept for rollback. Generations are not pruned automatically. The earlier documented hook path `~/.local/lib/herdr-supervisor/herdr_native_identity.py` is kept as a link to the active generation, so a provider hook configured with it keeps working; new hooks should use `~/.local/bin/herdr-native-identity` (see [sessions](SESSIONS.md)).

### Updating

`herdr-setup update` requires a **quiet point** and refuses (changing nothing) while any of these holds: a supervised task is not DONE or CANCELLED, a gate/continuation/owner recovery/interrupted or uncertain delivery/follow-up is unresolved, a worker holds the worker lock, a command, query, reset or pending-start item is queued, a session preparation is unresolved, a service is activating/deactivating or its state cannot be established, task state is unreadable or newer than the candidate supports, or a managed file was modified by someone else. Running tasks are never self-updated.

When it applies, it takes the deployment lock (concurrent applies of one plan join one journal; a different plan goes stale), the worker lock, backs up every asset it will replace, stops only the managed units that are active (never `herdr-server.service`, which hosts your native sessions), replaces assets, reloads the user manager once if a unit changed, switches the pointer, writes the receipt, runs a launcher smoke check, and restarts only the units it stopped. A failed smoke check restores the previous release automatically. It never changes configuration, credentials, provider auth, native histories, registry bindings, gates, tasks, inbox, outbox or any file it does not list.

An interrupted operation is resumed by applying the same plan again: finished steps are skipped, idempotent steps repeat, and a step whose effect on the outside world is unknown (a plugin link, a Telegram setup) is checked read-only and never repeated blindly. Unknown outcomes stay `RECONCILING` (exit 5); backups are retained.

### Legacy installations

A flat installation from the earlier installer is adopted only explicitly and only when it is byte-for-byte the reviewed tree you name:

```sh
herdr-setup update --payload NEW_TREE --adopt-legacy-from OLD_TREE   # preview shows every difference that blocks adoption
```

Version text is never proof. An unknown extra, missing or changed program file blocks adoption and every other change, and its code is never read or imported; a deployment that has modules the reviewed tree does not have (for example private additions) is not replaced. Adoption backs up the flat files and launchers first and `herdr-setup rollback` restores them exactly.

### Rollback

`herdr-setup rollback` returns to the previous generation (or restores the legacy flat files) and the backed-up assets, but only at a quiet point, only when the current assets are exactly what the last operation wrote, only when the previous generation and backups verify, and only when existing task state is not newer than the previous release can read (state is never rolled back to force old code to run). A rollback can itself be rolled back. The schema migration (`./install.sh --migrate` or `herdr-setup ... --migrate`) is a separate, explicit selection and is never implied by an update.

### Full profile

`herdr-setup setup --profile full` automates the supported stack after a reviewed selection (default components `sessions,telegram,quota`; `paths` is added when you pass `--project-root`, `--review-root` or `--product-repo`). Each component uses an existing contract and reports `ACTION_REQUIRED` with the exact remaining step when something is missing; a full setup is never labelled ready while anything is.

| Component | What it does | What stays yours |
|---|---|---|
| `paths` | Sets only the requested keys that are unset; an existing different value is preserved and refuses | Choosing the repositories |
| `sessions` | Reports whether `codex-main`/`claude-main` (or your configured names) are visible and whether ownership is recorded | Starting, logging in and confirming the pair (`herdr-supervisor sessions`); never created, restarted or rebound |
| `telegram` | Runs the protected `herdr-telegram setup` (token from `--telegram-token-file` or a non-echoed prompt in `--interactive`; never an argument; starts in shadow mode); an existing credential or configuration is never overwritten | The bot (@BotFather), your numeric ids |
| `quota` | Stages, verifies and builds the exact pinned plugin through `herdr-quota-dependency`, then `herdr plugin link --disabled DIR` and `herdr plugin enable herdr-agent-quota`; a matching plugin is preserved, a conflicting one stops the run | Rust 1.95.0 and the crates (a source archive does not make the build offline), and the plugin's `configure` action that edits provider hooks (read [Quota plugin dependency](QUOTA_DEPENDENCY.md); Supervisor never runs it) |

Services are opt-in: `--services worker,telegram,query,reset,backup,server` runs `systemctl --user daemon-reload`, then `enable` and `start` for each selected unit, in that order, and nothing else (no `--now`). Query, reset and backup are activated only when configured (a registered query session, an enabled reset policy with the codex CLI, an enabled backup destination); Telegram only when configured; the headless `server` unit is refused while an interactive Herdr server answers. If the user systemd manager cannot be reached the selection is reported as `ACTION_REQUIRED` and no unit is touched.

`herdr-setup status` distinguishes *installed*, *active now* and *survives a reboot*: boot persistence needs enabled units and lingering, which an administrator enables once (`sudo loginctl enable-linger YOUR_USER`; the software never does). Telegram online does not prove Herdr is running after a reboot: open Herdr or configure headless startup explicitly, and never start the headless unit next to an interactive server. `READY` covers every local check; network-dependent items are listed as `NOT_VERIFIED` (run `herdr-telegram doctor --network` yourself).

### Unit validation

Candidate units are validated with `systemd-analyze --user verify` **before** any target write, from the exact bytes that will be installed, staged under a scratch HOME together with the launchers they name. A genuine defect refuses the operation. Without a user runtime directory (`XDG_RUNTIME_DIR`) the check is reported as skipped, never as passed.

## Manual and legacy equivalents

The minimal profile replaces the old copy script. To install by hand you would place the files exactly as listed in `MANIFEST.txt`; prefer `herdr-setup`, which also records what it did.

## Quota plugin dependency

The supervisor needs the `herdr-agent-quota` Herdr plugin at one pinned revision (`config/quota-dependency.json`, installed beside the program files). `install.sh` does not acquire, link, enable, or configure it, and does not run the plugin's own installer, which edits provider hooks and settings. The full profile (`herdr-setup setup --profile full`) automates the stage/link/enable below after a preview and an approval; to do it yourself, check and stage it explicitly, in this order:

```sh
herdr-quota-dependency check                                   # read-only: Herdr, plugin revision, snapshot freshness, build prerequisites
herdr-quota-dependency bootstrap --stage-dir "$HOME/quota-stage"   # dry run
herdr-quota-dependency bootstrap --apply --stage-dir "$HOME/quota-stage"   # stage, verify against the lock, build with --locked
```

An existing installation at the locked revision is preserved; any other revision, duplicate, or unverifiable installation is reported and left alone. Building needs Rust 1.95.0 and the crates in the plugin's `Cargo.lock`; a source archive alone does not make the build offline. The commands that register and enable the staged plugin are printed, not run. Read [Quota plugin dependency](QUOTA_DEPENDENCY.md) for the pin, the offline limits, the operator steps, and rollback.

Configure the workspace and Herdr path in `~/.config/herdr-supervisor/config.json`, then run:

```sh
herdr-supervisor doctor
herdr-telegram doctor
```

After reviewing service conflicts, the operator may reload and enable selected user units. The headless Herdr unit must not be started while an interactive Herdr server is already running.

For service persistence after logout or reboot, an administrator may perform this one-time root action:

```sh
sudo loginctl enable-linger YOUR_USER
```

The software never performs that action itself.

The inactive `herdr-codex-reset-worker.path` and `.service` units support optional banked-reset automation. Review them and the reset policy before enabling the path unit. The helper has outbound network access only because the existing supervisor worker remains network denied; it starts Codex app-server over stdio and never handles task text, Telegram tokens, or raw authentication data. Installing files does not enable or start this unit.

For rollback use `herdr-setup rollback` (see above). Leave services you did not stop alone; a rollback restarts only the units it stopped. If old code cannot read newer state, leave services inactive and recover manually; do not delete state to force startup. `./uninstall.sh` removes only the enumerated program files (including every generation and the pointer) after every supervisor unit reports an established `inactive` or `failed` state; an active or transitioning unit, or a session where the user systemd manager cannot be queried, makes it stop without removing anything. It preserves configuration and durable state, including the installation receipts and backups.
