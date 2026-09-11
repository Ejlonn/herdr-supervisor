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
