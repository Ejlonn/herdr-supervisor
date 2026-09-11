# Optional backup and restore

Backup operation is disabled until a complete config is reviewed and `herdr-backup setup ... --confirm` succeeds. It never blocks a supervised coding task, contacts no host when disabled, and uses no LLM calls.

## Strategies

`ssh_snapshot` copies explicitly configured paths using `rsync` over SSH with batch authentication and strict host-key verification. It stages uploads under `incoming`, verifies checksums, moves a verified snapshot into `snapshots`, and atomically updates `latest`. A failed attempt preserves the last verified recovery point.

`git_recovery` reports whether committed refs exist at the configured remote and separately reports modified, untracked, ignored, ahead, and local-only state. It never commits or pushes. Optional Git bundles protect committed local refs but still omit uncommitted files.

`both` stores filesystem snapshots and Git metadata/bundles.

Secret paths and common plaintext credential assignments are excluded. Telegram, OAuth/API, cookie, SSH/VPN key, `.env`, agent-authentication, and browser-profile material require reauthentication or separate intentionally designed encrypted protection.

## Configure

```sh
herdr-backup setup --strategy both \
  --source /absolute/workspace/path \
  --git-repo /absolute/workspace/path/repository \
  --destination backup-user@example.net:/srv/backups/herdr-supervisor \
  --schedule both --retention-daily 7 --retention-weekly 4
```

This writes a disabled proposal. Review it, verify normal SSH key authentication and host keys, then repeat with `--confirm`. Only then may the operator explicitly enable `herdr-backup.timer`; the installer never enables it.

## Observe and verify

```sh
herdr-backup status
herdr-backup list
herdr-backup verify
herdr-backup git-check
```

Health distinguishes disabled, healthy, stale, failed, and unverified. “Verified” means the selected snapshot was downloaded into a temporary directory and its manifest/checksums passed.

## Restore

List recovery points, choose an ID, and restore into a path that does not exist:

```sh
herdr-backup restore --backup-id BACKUP_ID --destination /new/recovery/path
herdr-backup restore-git --repo /configured/repository/path --destination /new/repository/path
```

Restore never overwrites an existing path and never runs automatically after reboot. Verify the restored data before replacing any live workspace. Reinstall credentials separately because they are intentionally excluded.
