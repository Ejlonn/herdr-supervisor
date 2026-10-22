# Installation and rollback

Requires Herdr 0.9.0 or newer (the minimum tested CLI contract); `herdr-supervisor doctor` fails with an error when the version or a required command/option is missing and warns when only the optional prompt acknowledgement is absent. Run `./install.sh --dry-run` first, then `./install.sh` as the target non-root user. Both refuse to proceed, before creating any file, unless the Herdr executable exists at `~/.local/bin/herdr`. The source tree is copied to `~/.local/lib/herdr-supervisor`, launchers to `~/.local/bin`, examples and documentation to `~/.local/share/doc/herdr-supervisor`, and inactive units to `~/.config/systemd/user`.

The installer creates configuration only when the target file is absent. It never overwrites a bot token, config, owner record, task, event, log, lock, or state file. It validates units when `systemd-analyze` is available and `XDG_RUNTIME_DIR` is set (otherwise it reports the check as skipped) and never calls `daemon-reload`, `enable`, `start`, `restart`, or `--now`.

State schema migration is separate and explicit:

```sh
./install.sh --migrate
```

Run this only after reviewing the candidate and making a state backup. It does not activate services.

## Quota plugin dependency

The supervisor needs the `herdr-agent-quota` Herdr plugin at one pinned revision (`config/quota-dependency.json`, installed beside the program files). `install.sh` does not acquire, link, enable, or configure it, and does not run the plugin's own installer, which edits provider hooks and settings. Check and stage it explicitly, in this order:

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

For rollback, stop only affected services explicitly, run `./uninstall.sh`, and restore the prior manifest-hashed program files. The uninstall removes files only after every supervisor unit reports an established `inactive` or `failed` state; an active or transitioning unit, or a session where the user systemd manager cannot be queried, makes it stop without removing anything. The uninstall preserves configuration and durable state. If old code cannot read newer state, leave services inactive and recover manually; do not delete state to force startup.
