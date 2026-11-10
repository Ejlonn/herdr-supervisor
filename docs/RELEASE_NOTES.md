# Herdr Supervisor 0.4.0-beta.1 — release notes

Beta status does not relax authorization, redaction, callback replay, prompt-delivery, state-integrity or approval-hash guarantees. These notes describe what the release source contains relative to the initial `0.3.0-beta.1` source release. Validation status is stated at the end; items marked **pending** have not been run and must not be read as passed.

## Installation and updates

- `herdr-setup`: guided minimal (inactive) installation and explicitly selected full setup components (Telegram bridge, quota plugin staging, session enrollment, services). Nothing is activated without a preview and an approval.
- Preview-first, content-bound approvals; verified immutable program generations under `~/.local/lib/herdr-supervisor/generations/` with one atomic `active` pointer; launchers resolve the pointer once per invocation.
- Quiet-point and worker-lock protection while program files change; crash-safe, replay-safe journals; backups; rollback, including rollback of a rollback.
- Ownership proofs: receipts and backup metadata cannot invent authority over user files; every receipt kind is proven by the approved operation that wrote it. Unknown or custom installations refuse automatic adoption.
- Readiness distinguishes installed, usable now and reboot-persistent. Only `enabled` units count as boot persistence (`linked` and runtime-only enablement do not); missing credentials, sessions or dependencies stay actionable rather than falsely ready; empty or unrecognized systemctl answers read as unknown.
- Worker readiness checks the library that the exact installed launcher variant would execute (managed generation for the current launcher; flat library for installations made before generations) and refuses a malformed `active` pointer.

## Sessions and project contexts

- Optional project onboarding and portable skill support (`herdr-supervisor project inspect|apply`), preserve-by-default.
- Explicit fresh-session starts with native identity verification and human-handled startup confirmations.
- **Named session pairs** (off by default; enable with `session_pairs` and administrator-allowed roots). A pair is a saved, immutable record of one Claude and one Codex native session plus the repository, review base and instruction root they work in. Telegram can choose or save a pair on the task Start card. A run keeps its original pair and context through later answers, approvals and follow-ups; selecting another pair is a boundary operation that refuses while the previous run or its sessions are not finished and settled. Missing, busy, duplicated or changed identities fail clearly; nothing is launched, renamed, rebound or sent to satisfy a selection.
- A cancelled named run recovers through its own frozen pair, never through global owners or the registry.

## Workflow and recovery

- Flexible workflow profiles (`supervised_v2` default, strict `gated_v2`, legacy `v1`) with separate scope, runtime and publication decisions; existing strict profiles retain their obligations.
- Native result recovery, interrupted wait and quota handling, explicit operator handoffs, durable task-start authorization, and one lock serializing every writer of a pending task start (Start commit, workflow/starting-agent selection, pair selection).
- Telegram shows every question choice and provides an explicit custom answer.
- **Codex native helper-history guard**: before input, a result read or routing, the exact Codex parent's complete native history is read once under one byte/entry/time budget. Evidence that is missing, malformed, unsupported, changed or over budget is never treated as "no helper"; one approved helper (`repository_explorer` or `repository_reviewer`) per parent lifetime is allowed, a running helper makes the supervisor wait (nothing is interrupted), and routing then uses the parent's own native final.
- **Claude background jobs and startup**: pending native background jobs hold routing without waking a model; a startup UNKNOWN quota snapshot is allowed only for the already accepted startup turn and is revoked once real telemetry is usable; transcript-only compaction summaries are data, never a new prompt or notification.
- **Cancelled-delivery settlement**: an explicit, run- and turn-bound command settles a cancelled run's delivery after both original sessions are proven present, idle and child-settled. It never accepts a result, marks completion, redeems a gate or sends input.

## Optional helpers and dependencies

- Portable repository explorer/reviewer (Codex) and tracer/test-runner/diff-auditor (Claude) definitions, installed explicitly with a shared sync tool. Definitions are optional evidence sources; installing them does not authorize delegation or restart a session.
- **Quota plugin**: the supervisor reads snapshots from, and refreshes through, the Herdr plugin `herdr-agent-usage` (formerly `herdr-agent-quota`). The dependency lock pins upstream release 1.6.3 (`cd6927b282d974764b66a08def017adc58d37399`, tree `89a62b19…`, MIT) of the owned fork, with locally verifiable recovery artifacts; nothing follows a branch. An installation under the earlier id is reported as a conflict and never replaced. Compared with the earlier pin this revision adds the `zstd` crate (C compiler needed) and Cursor and Muse providers (the Cursor provider reads the Cursor IDE's local access token and calls its API); the supervisor uses none of them, and the plugin's `configure` action (which edits provider hook files) stays a separate operator decision. See `docs/QUOTA_DEPENDENCY.md`.

## Upgrade notes

- Obtain the exact release source, run `herdr-setup` to preview against your installation, inspect the approval hash and apply that reviewed plan. The updater never pulls a branch and never replaces working conversations.
- Existing configuration, credentials, user-created files and native histories are preserved. Locally modified or unrecognized code requires an explicit adoption path; unknown installations are refused.
- Provider CLIs and login, dedicated Telegram credentials, Herdr 0.9.0 or newer, the pinned Rust toolchain (for the plugin build) and platform tools remain operator prerequisites. Hook configuration and service activation require explicit action.
- Named pairs, helpers and native tracking are off until configured.

## Validation status

Offline source validation (synthetic fixtures only, no provider, network or live-service calls): full test discovery, ruff, mypy, compile and shell syntax, source and wheel packaging with all entry points, a real `systemd-analyze --user verify` of the shipped units, offline verification of the plugin recovery artifacts against the lock, and a content scan. Counts and logs: the release handoff.

**Pending** (not run for this release; each needs its own authorization): CI on the supported Python 3.11 and 3.13 matrix; building, linking, enabling and running the pinned plugin and real quota snapshots; real Telegram, provider sessions and native hooks; fresh installation, update, worker lifecycle, rollback, legacy adoption and recovery on a disposable user; user-systemd activation, linger and reboot persistence; fresh remote comparison, final artifacts and checksums, tag and publication. Earlier core-installer runtime results are partial component evidence, not evidence for this combined candidate.

## Known limitations

- The inherited published history of the base branch contains project-specific names in a few earlier commits; this release's changes remove the operational mentions but do not rewrite history.
- The helper-history guard observes recorded actions and cannot prevent a native spawn.
- An attacker able to rewrite a receipt, its journal and approval consistently is beyond what file-level ownership proofs can stop.
