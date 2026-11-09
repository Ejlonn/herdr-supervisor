# Security model

- All state-changing Telegram actions require the configured numeric owner and exact private chat.
- Tokens remain in mode-0600 files and are redacted from errors and logs.
- Callback IDs are opaque, expiring, state-bound, hash-bound, and one-time.
- Telegram exposes registered redacted artifacts only; symlinks, arbitrary paths, protected roots, and changed hashes fail closed.
- Supervisor task files and text are data, never shell command fragments.
- Query sessions are dedicated and launched with provider-supported read-only controls; main workflow sessions are excluded.
- Backup commands use fixed local argv. SSH destinations have a strict grammar, host-key verification cannot be disabled, and retention only accepts generated snapshot IDs beneath the configured root.
- Restores require an absent destination. Services, migrations, publication, and destructive cleanup remain human operations.

Report suspected vulnerabilities privately to the repository maintainer before public disclosure. Do not include live credentials, private task artifacts, or user/session IDs in a report.

## Installation manager (`herdr-setup`)

`herdr-setup` writes program files, so it is held to the same fail-closed rules as the controller:

- **Preview by default; approval bound to its inputs.** The approval hash covers the candidate file contents, the options, the resolved target identity, the exact preimage of every file that may change, the service actions and the running tool. A different candidate, option, target or preimage makes it stale. A saved plan is hash-checked against edits.
- **The payload is data.** It is read once through an explicit allowlist with no-follow opens; links, hard links, special files, unexpected names, unsafe modes and oversized or inconsistent trees are refused. It is never imported or executed to be inspected, and the bytes hashed are the bytes published.
- **Ownership is enumerated.** Only listed launchers, units, documents and examples are written, each backed up first and replaced by one rename; unknown, modified or symlinked targets block the operation. Credentials, provider authentication, configuration, native histories, registry bindings, gates, tasks, inbox, outbox and unlisted files are never touched. An existing credential file is never overwritten.
- **Secrets.** The Telegram token is read from a mode-0600 file or a non-echoed prompt, is never an argument, an environment value, a plan, a journal, a receipt or output, and is written only by the existing protected setup routine. Marketplace actions cannot collect secrets and run read-only previews only.
- **Quiet point.** An update or rollback proceeds only with no active worker, an unfinished task, unresolved gate/delivery/recovery, queued work or unknown service state; it holds the deployment lock and the worker lock while files change and never stops the headless Herdr server.
- **Uncertain outcomes are never repeated.** Steps are journaled before and after they run. An external step with an unrecorded outcome is checked read-only and either recorded or left `RECONCILING`; backups are retained.
- **No privilege or network by itself.** It refuses to run as root, never runs `sudo`, a package manager or a provider login, and contacts the network only when you apply the quota stage without a local lock-listed artifact. `loginctl enable-linger` remains an administrator instruction. The plugin `configure` action (provider hook edits and a Herdr server reload) is never run.
- **Unit validation** uses `systemd-analyze --user verify` on the exact candidate bytes under a scratch HOME before any target write.
