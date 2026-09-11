# Restart and machine recovery

Durable supervisor state records the active run, accepted delivery, current turn, native session identity, gates, quota waits, and pending notifications. Restart reconciliation never auto-continues `WAIT_USER`, `PAUSED`, or `CANCELLED`, never guesses from aliases, and never replays an uncertain prompt.

On a replacement machine:

1. Install Herdr and this package without starting services.
2. Restore repositories from their configured Git remotes into new directories.
3. Restore a chosen verified filesystem snapshot into a new directory.
4. Inspect manifests, checksums, HEADs, branch/dirty metadata, and exclusions.
5. Reauthenticate Telegram, Claude, Codex, SSH, and other services whose credentials were excluded.
6. Configure new paths and run read-only doctor checks.
7. Restore native agent sessions only when their durable identities can be verified.
8. Activate services individually after checking for conflicts.

Never restore over a live workspace automatically and never infer task completion from a Herdr lifecycle label alone.
