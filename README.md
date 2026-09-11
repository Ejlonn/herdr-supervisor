# herdr-supervisor

`herdr-supervisor` is a durable human-in-the-loop workflow supervisor and remote-control layer for coding agents running through [Herdr](https://github.com/herdrdev/herdr). It coordinates persistent Codex and Claude sessions through deterministic state transitions, preserves native conversations, waits for provider quota without model polling, and places human approval gates around consequential stages.

This repository is an independent community companion project. It is not an official Herdr component and is not affiliated with or endorsed by Herdr, Anthropic, OpenAI, or Telegram.

The current candidate version is **0.3.0-beta.1**. Beta status does not relax authorization, redaction, callback replay, prompt-delivery, state-integrity, or approval-hash guarantees.

## Capabilities

- Durable Claude/Codex workflows with native session preservation
- Deterministic lifecycle orchestration and token-free quota waiting
- Five-hour and weekly quota handling with safe, periodic non-LLM rechecks
- Explicit per-task Codex banked-reset budgets with durable idempotent redemption
- Typed plan, question, runtime-validation, and push-approval gates
- Telegram control over outbound HTTPS long polling; no inbound listener or webhook
- Short task text plus confirmed `.md` and `.txt` task-file submissions
- Concise messages plus registered, redacted Markdown result documents
- Read-only `/ask` queries with deterministic answers and dedicated-session failover
- Restart recovery, durable notifications, one-time callbacks, and fail-closed ambiguity handling
- Optional deterministic backup health, snapshot, verification, and isolated restore tooling

It is not another AI agent, an API-model router, a provider-quota bypass, or a tool tied to one project. Telegram controls the supervisor state machine; it does not inject raw keys into agent panes.

## Requirements

- Linux with Python 3.11 or newer; release validation is performed on Python 3.13
- Herdr installed user-scoped with its executable at `~/.local/bin/herdr`; the installer checks this before writing anything, and the headless server unit runs that path
- The Herdr plugin `herdr-agent-quota`: it writes the five-hour, weekly, and context quota snapshots the supervisor reads, and provides the non-LLM `refresh` action used to recheck Codex quota without waking a model
- Codex and Claude CLIs with persistent sessions managed through Herdr; the optional Codex banked-reset feature additionally uses Codex's supported app-server reset-credit interface, which is separate from the quota plugin
- User systemd for detached workers and optional services
- Per feature, optional: outbound HTTPS to `api.telegram.org:443` (Telegram control), `git` (Git recovery assessment), `ssh` and `rsync` (SSH snapshot backups)

Routine installation does not require root. Keeping user services alive after logout may require an administrator to run `loginctl enable-linger USER` once; see [Installation](docs/INSTALL.md).

## Install

```sh
git clone YOUR_REPOSITORY_URL herdr-supervisor
cd herdr-supervisor
./install.sh --dry-run
./install.sh
herdr-supervisor doctor
```

The installer copies reviewed files into user-scoped paths, creates restrictive directories, and installs disabled unit definitions. It does not enable, start, reload, or restart services. Existing config, tokens, and state are preserved.

Configure project paths in `~/.config/herdr-supervisor/config.json`; no Python edit is needed. Configure Telegram interactively without echoing the token:

```sh
herdr-telegram setup --owner-user-id YOUR_NUMERIC_ID --chat-id YOUR_PRIVATE_CHAT_ID
herdr-telegram doctor --network
```

Setup begins in shadow mode. Enable actionable control only after reviewing the doctor result and following [Telegram setup](docs/TELEGRAM.md).

## Daily commands

```sh
herdr-supervisor status
herdr-supervisor run --policy gated_v2 task.md
herdr-supervisor run --policy gated_v2 --codex-reset-budget 1 task.md
herdr-supervisor pause
herdr-supervisor resume
herdr-supervisor cancel
herdr-supervisor logs
herdr-supervisor doctor
herdr-supervisor refresh-quota --run-id CURRENT_RUN_UUID
```

Telegram supports `/status`, `/task`, `.md`/`.txt` uploads with Start/Cancel confirmation, `/plan`, `/pending`, `/pause`, `/resume`, `/cancel`, `/logs`, `/doctor`, and read-only `/ask`.

## Backups

Backups are optional and disabled by default. A Git remote protects only commits and refs present on that remote; it does not protect modifications, untracked/ignored files, local-only commits, workflow artifacts, or runtime state. SSH snapshots explicitly exclude credentials by default and require later reauthentication.

```sh
herdr-backup setup --strategy ssh_snapshot \
  --source "$HOME/workspace" \
  --destination backup-user@example.net:/srv/backups/herdr-supervisor
# Review the proposed config, then repeat with --confirm.
herdr-backup status
herdr-backup verify
```

Enabling the timer is a separate operator action after configuration is confirmed. See [Backup and restore](docs/BACKUP.md).

## Development and release safety

Run the isolated suite from the source root:

```sh
python3 -m unittest discover -s tests -v
python3 -m compileall -q src tests
```

Do not copy a live home-directory installation into a release. Source trees must contain examples only. Before publication, scan filenames, working-tree contents, Git objects, and history for credentials, owner/chat/session identifiers, runtime state, and private workflow artifacts. See [Release checklist](docs/RELEASE.md).

## Uninstall

```sh
./uninstall.sh --dry-run
./uninstall.sh
```

The default preserves all configuration, credentials, and durable state. Purging them requires both `--purge` and `--confirm-purge`, refuses active services, and remains an explicit destructive operator action.

This is fully open-source software under Apache License 2.0 (`SPDX-License-Identifier: Apache-2.0`). There are no dual-license or commercial-use restrictions beyond that license. See [LICENSE](LICENSE).
