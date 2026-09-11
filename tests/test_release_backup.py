"""Stage D — optional backup/restore: disabled by default, deterministic, never blocks work, restores only
into absent destinations, secrets excluded, Git assessment without history mutation, mocked SSH transport."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import herdr_backup as hb  # noqa: E402
import herdr_present as hp  # noqa: E402
import herdr_supervisor as hs  # noqa: E402
from v2_fixtures import V2Case  # noqa: E402


class FakeClock:
    def __init__(self, now: float = 1_800_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class BackupBase(V2Case):
    def setUp(self) -> None:
        super().setUp()
        self.root = Path(self.tmp.name)
        self.clock = FakeClock()
        self.remote = self.root / "remote"
        self.src = self.root / "project"
        (self.src / "docs").mkdir(parents=True)
        (self.src / "docs" / "a.md").write_text("alpha\n")
        (self.src / "code.py").write_text("print(1)\n")
        (self.src / ".env").write_text("TOKEN=secret\n")
        (self.src / "id_rsa").write_text("PRIVATE\n")
        (self.src / ".config").mkdir()
        (self.src / ".config" / "herdr-telegram").mkdir()
        (self.src / ".config" / "herdr-telegram" / "bot-token").write_text("123:abc\n")
        self.config = hb.validate_backup_config({**json.loads(json.dumps(hb.DEFAULT_BACKUP_CONFIG)), "enabled": True, "sources": [str(self.src)], "destination": "backup@example-host:/srv/backups/herdr"})

    def runner(self, config=None):
        return hb.BackupRunner(self.paths, config or self.config, hb.LocalDirTransport(self.remote), clock=self.clock, machine="test-machine")


class ConfigTests(BackupBase):
    def test_disabled_by_default_and_health_disabled(self):
        config = hb.load_backup_config(hb.backup_config_file(self.paths))
        self.assertFalse(config["enabled"])
        self.assertEqual(hb.health(config, hb.load_backup_state(self.paths), self.clock())["status"], "disabled")
        self.assertFalse(hb.due(config, hb.load_backup_state(self.paths), self.clock()))
        self.assertEqual(self.runner(config).run(), {"ok": False, "skipped": "disabled"})

    def test_validation_rejects_shell_and_hostkey_bypass(self):
        base = json.loads(json.dumps(hb.DEFAULT_BACKUP_CONFIG))
        with self.assertRaises(hs.SupervisorError):
            hb.validate_backup_config({**base, "enabled": True, "sources": ["/x"], "destination": "u@h:/srv/x; rm -rf /"})
        with self.assertRaises(hs.SupervisorError):
            hb.validate_backup_config({**base, "enabled": True, "sources": ["/x"], "destination": "u@h:/srv/../x"})
        with self.assertRaises(hs.SupervisorError):
            hb.validate_backup_config({**base, "enabled": True, "sources": ["/x"], "destination": "u@h:/"})
        with self.assertRaises(hs.SupervisorError):
            hb.validate_backup_config({**base, "ssh_options": ["-o", "StrictHostKeyChecking=no"]})
        with self.assertRaises(hs.SupervisorError):
            hb.validate_backup_config({**base, "enabled": True, "sources": [], "destination": "u@h:/srv/x"})
        with self.assertRaises(hs.SupervisorError):
            hb.validate_backup_config({**base, "strategy": "ftp"})
        with self.assertRaises(hs.SupervisorError):
            hb.validate_backup_config({**base, "ssh_options": ["-o", "ProxyCommand=touch /tmp/owned"]})
        with self.assertRaises(hs.SupervisorError):
            hb.validate_backup_config({**base, "sources": ["/one/project", "/two/project"]})

    def test_setup_cli_writes_disabled_config_without_confirm(self):
        env = {**os.environ, "HERDR_SUPERVISOR_CONFIG": str(self.paths.config_file), "HERDR_SUPERVISOR_STATE_DIR": str(self.paths.state_dir)}
        completed = subprocess.run([sys.executable, str(_ROOT / "src" / "herdr_backup.py"), "setup", "--strategy", "ssh_snapshot", "--source", str(self.src), "--destination", "backup@example-host:/srv/backups/herdr"], env=env, capture_output=True, text=True, check=False)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        config = hb.load_backup_config(hb.backup_config_file(self.paths))
        self.assertFalse(config["enabled"])
        self.assertIn("--confirm", completed.stdout)
        status = subprocess.run([sys.executable, str(_ROOT / "src" / "herdr_backup.py"), "status"], env=env, capture_output=True, text=True, check=False)
        self.assertEqual(json.loads(status.stdout)["status"], "disabled")

    def test_daily_weekly_both_and_custom_schedules_validate(self):
        base = json.loads(json.dumps(hb.DEFAULT_BACKUP_CONFIG))
        for schedule in (
            {"daily": True, "weekly": False, "interval_hours": None},
            {"daily": False, "weekly": True, "interval_hours": None},
            {"daily": True, "weekly": True, "interval_hours": None},
            {"daily": False, "weekly": False, "interval_hours": 6},
        ):
            validated = hb.validate_backup_config({**base, "schedule": schedule})
            self.assertEqual(validated["schedule"], schedule)


class SnapshotTests(BackupBase):
    def test_snapshot_excludes_secrets_and_verifies(self):
        (self.src / "innocent-name.md").write_text("api_key = should-not-leave\n")
        result = self.runner().run()
        self.assertTrue(result["ok"], result)
        snap = self.remote / "snapshots" / result["backup_id"]
        self.assertTrue((snap / "sources" / "project" / "docs" / "a.md").exists())
        self.assertTrue((snap / "sources" / "project" / "code.py").exists())
        for forbidden in (".env", "id_rsa", ".config/herdr-telegram/bot-token"):
            self.assertFalse((snap / "sources" / "project" / forbidden).exists(), forbidden)
        self.assertNotIn("secret", (snap / "CHECKSUMS").read_text())
        manifest = json.loads((snap / "MANIFEST.json").read_text())
        self.assertEqual(manifest["file_count"], 2)
        self.assertIn("Telegram bot token", manifest["excluded_categories"])
        self.assertTrue(hb.verify_tree(snap))
        self.assertEqual(os.readlink(self.remote / "latest"), f"snapshots/{result['backup_id']}")
        state = hb.load_backup_state(self.paths)
        self.assertEqual(state["last_verified_id"], result["backup_id"])
        self.assertFalse((hb.backup_dir(self.paths) / "staging" / result["backup_id"]).exists())
        self.assertFalse((snap / "sources" / "project" / "innocent-name.md").exists())

    def test_structured_credentials_are_excluded_through_the_real_snapshot_path(self):
        fixtures = {
            "aws.txt": "AWS_ACCESS_KEY_ID=AKIAABCDEFGHIJKLMNOP\n",
            "database.txt": "DATABASE_URL=postgresql://admin:supersecret@example.invalid/db\n",
            "json.txt": '{"client_secret": "should-not-leave"}\n',
        }
        for name, content in fixtures.items():
            (self.src / name).write_text(content)
        result = self.runner().run()
        self.assertTrue(result["ok"], result)
        snap_root = self.remote / "snapshots" / result["backup_id"] / "sources" / "project"
        for name in fixtures:
            self.assertFalse((snap_root / name).exists(), name)

    def test_verify_rejects_unlisted_regular_nested_symlink_and_special_nodes(self):
        result = self.runner().run()
        snap = self.remote / "snapshots" / result["backup_id"]
        extra = snap / "extra.txt"
        extra.write_text("extra\n")
        self.assertFalse(hb.verify_tree(snap))
        extra.unlink()

        nested = snap / "nested" / "extra.txt"
        nested.parent.mkdir()
        nested.write_text("extra\n")
        self.assertFalse(hb.verify_tree(snap))
        nested.unlink()
        nested.parent.rmdir()

        link = snap / "escape-link"
        link.symlink_to("/etc/passwd")
        self.assertFalse(hb.verify_tree(snap))
        link.unlink()

        fifo = snap / "special-fifo"
        os.mkfifo(fifo)
        self.assertFalse(hb.verify_tree(snap))
        fifo.unlink()

        self.assertTrue(hb.verify_tree(snap))

    def test_source_change_during_snapshot_fails_without_replacing_last_good(self):
        first = self.runner().run()
        real_copy = hb.shutil.copy2

        def mutate_after_copy(source, target, *args, **kwargs):
            result = real_copy(source, target, *args, **kwargs)
            if Path(source).name == "code.py":
                Path(source).write_text("changed during backup\n")
            return result

        self.clock.now += 3600
        with mock.patch.object(hb.shutil, "copy2", side_effect=mutate_after_copy):
            failed = self.runner().run()
        self.assertFalse(failed["ok"])
        self.assertIn("changed during snapshot", failed["reason"])
        self.assertEqual(hb.load_backup_state(self.paths)["last_verified_id"], first["backup_id"])

    def test_missing_source_fails_safely(self):
        config = {**self.config, "sources": [str(self.root / "missing-project")]}
        result = self.runner(config).run()
        self.assertFalse(result["ok"])
        self.assertIn("source is missing", result["reason"])

    def test_health_transitions_healthy_stale_failed(self):
        self.runner().run()
        state = hb.load_backup_state(self.paths)
        self.assertEqual(hb.health(self.config, state, self.clock())["status"], "healthy")
        self.clock.now += 25 * 3600
        self.assertEqual(hb.health(self.config, state, self.clock())["status"], "stale")
        self.clock.now = 1_800_000_000.0 + 60
        state["last_attempt"] = hs.iso_utc(self.clock())
        state["failure_reason"] = "boom"
        self.assertEqual(hb.health(self.config, state, self.clock())["status"], "failed")

    def test_failed_transfer_preserves_last_verified_and_never_raises(self):
        first = self.runner().run()
        self.clock.now += 3600

        class BrokenTransport(hb.LocalDirTransport):
            def upload(self, staging, backup_id):
                raise OSError("network down")

        runner = hb.BackupRunner(self.paths, self.config, BrokenTransport(self.remote), clock=self.clock)
        result = runner.run()
        self.assertFalse(result["ok"])
        self.assertIn("network down", result["reason"])
        state = hb.load_backup_state(self.paths)
        self.assertEqual(state["last_verified_id"], first["backup_id"])
        self.assertEqual(hb.health(self.config, state, self.clock())["status"], "failed")
        self.assertTrue((self.remote / "snapshots" / first["backup_id"]).exists())
        self.assertEqual(hb.LocalDirTransport(self.remote).list_snapshots(), [first["backup_id"]])

    def test_verification_failure_does_not_complete_snapshot(self):
        class LyingTransport(hb.LocalDirTransport):
            def remote_manifest_check(self, backup_id, manifest):
                return False

        result = hb.BackupRunner(self.paths, self.config, LyingTransport(self.remote), clock=self.clock).run()
        self.assertFalse(result["ok"])
        self.assertEqual(hb.LocalDirTransport(self.remote).list_snapshots(), [])
        self.assertFalse((self.remote / "latest").exists())
        self.assertEqual(hb.health(self.config, hb.load_backup_state(self.paths), self.clock())["status"], "failed")

    def test_scheduled_run_respects_due_policy(self):
        runner = self.runner()
        self.assertTrue(runner.run(scheduled=True)["ok"])
        self.clock.now += 3600
        self.assertEqual(runner.run(scheduled=True), {"ok": True, "skipped": "not due"})
        self.clock.now += 23 * 3600
        self.assertNotIn("skipped", runner.run(scheduled=True))
        interval = {**self.config, "schedule": {"daily": False, "weekly": False, "interval_hours": 2}}
        state = hb.load_backup_state(self.paths)
        self.assertFalse(hb.due(interval, state, self.clock() + 3600))
        self.assertTrue(hb.due(interval, state, self.clock() + 2 * 3600 + 1))

    def test_retention_keeps_recent_weekly_and_last_verified(self):
        runner = self.runner({**self.config, "retention": {"daily": 2, "weekly": 1}})
        ids = []
        for _ in range(5):
            ids.append(runner.run()["backup_id"])
            self.clock.now += 86400
        remaining = hb.LocalDirTransport(self.remote).list_snapshots()
        self.assertIn(ids[-1], remaining)
        self.assertIn(ids[-2], remaining)
        self.assertLessEqual(len(remaining), 3)
        self.assertIn(hb.load_backup_state(self.paths)["last_verified_id"], remaining)

    def test_retention_never_deletes_outside_managed_root(self):
        transport = hb.LocalDirTransport(self.remote)
        with self.assertRaises(hs.SupervisorError):
            transport.delete("../../etc")
        ssh = hb.SshRsyncTransport({"user": "u", "host": "h", "path": "/srv/x"}, [], runner=lambda argv, timeout: subprocess.CompletedProcess(argv, 0, "", ""))
        with self.assertRaises(hs.SupervisorError):
            ssh.delete("latest")


class RestoreTests(BackupBase):
    def test_restore_into_new_directory_and_refuses_existing(self):
        backup_id = self.runner().run()["backup_id"]
        dest = Path(self.root) / "restored"
        result = self.runner().restore_snapshot(backup_id, dest)
        self.assertTrue(result["ok"])
        self.assertEqual((dest / "sources" / "project" / "docs" / "a.md").read_text(), "alpha\n")
        self.assertIn("Telegram bot token", result["reauthentication_required"])
        with self.assertRaises(hs.SupervisorError):
            self.runner().restore_snapshot(backup_id, dest)
        with self.assertRaises(hs.SupervisorError):
            self.runner().restore_snapshot("bad id", Path(self.root) / "other")

    def test_restore_rejects_corrupted_snapshot(self):
        backup_id = self.runner().run()["backup_id"]
        (self.remote / "snapshots" / backup_id / "sources" / "project" / "code.py").write_text("tampered\n")
        dest = Path(self.root) / "restored2"
        with self.assertRaises(hs.SupervisorError):
            self.runner().restore_snapshot(backup_id, dest)
        self.assertFalse(dest.exists())

    def test_checksum_path_traversal_and_symlink_are_rejected(self):
        backup_id = self.runner().run()["backup_id"]
        snap = self.remote / "snapshots" / backup_id
        outside = self.root / "outside.txt"
        outside.write_text("outside\n")
        digest = hb.hashlib.sha256(outside.read_bytes()).hexdigest()
        (snap / "CHECKSUMS").write_text(f"{digest}  ../../outside.txt\n")
        self.assertFalse(hb.verify_tree(snap))
        link = snap / "escape"
        link.symlink_to(outside)
        (snap / "CHECKSUMS").write_text(f"{digest}  escape\n")
        self.assertFalse(hb.verify_tree(snap))

    def test_verify_rehearsal_updates_last_verified(self):
        backup_id = self.runner().run()["backup_id"]
        self.clock.now += 7200
        result = self.runner().verify()
        self.assertTrue(result["ok"])
        self.assertEqual(result["backup_id"], backup_id)
        self.assertEqual(hb.load_backup_state(self.paths)["last_verified"], hs.iso_utc(self.clock()))


class SshTransportTests(BackupBase):
    def test_fixed_argv_no_shell_and_no_real_contact(self):
        calls = []
        uploaded = {"staging": None}

        def fake_run(argv, *, timeout):
            calls.append(argv)
            if argv[0] == "rsync":
                uploaded["staging"] = Path(argv[-2].rstrip("/"))
            out = ""
            if "sha256sum" in " ".join(argv):
                staging = uploaded["staging"]
                self.assertIsNotNone(staging)
                out = (
                    f"{hb.hashlib.sha256((staging / 'CHECKSUMS').read_bytes()).hexdigest()}  CHECKSUMS\n"
                    f"{hb.hashlib.sha256((staging / 'MANIFEST.json').read_bytes()).hexdigest()}  MANIFEST.json\n"
                    "VERIFIED\n"
                )
            return subprocess.CompletedProcess(argv, 0, out, "")

        transport = hb.SshRsyncTransport(hb.parse_destination("backup@example-host:/srv/backups/herdr"), ["-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes"], runner=fake_run)
        result = hb.BackupRunner(self.paths, self.config, transport, clock=self.clock).run()
        self.assertTrue(result["ok"], result)
        programs = [argv[0] for argv in calls]
        self.assertEqual(set(programs), {"ssh", "rsync"})
        for argv in calls:
            self.assertIsInstance(argv, list)
            self.assertNotIn("StrictHostKeyChecking=no", " ".join(argv))
        rsync = next(a for a in calls if a[0] == "rsync")
        self.assertIn("--checksum", rsync)
        self.assertTrue(rsync[-1].startswith("backup@example-host:/srv/backups/herdr/incoming/"))
        verify_call = next(a for a in calls if a[0] == "ssh" and "sha256sum -c" in " ".join(a))
        self.assertIn("find . -type f", " ".join(verify_call), "remote publication checks the exact file set")
        self.assertTrue(any("mv /srv/backups/herdr/incoming/" in " ".join(a) for a in calls if a[0] == "ssh"))

    def test_ssh_failure_recorded_without_exception(self):
        transport = hb.SshRsyncTransport(hb.parse_destination("backup@example-host:/srv/backups/herdr"), [], runner=lambda argv, timeout: subprocess.CompletedProcess(argv, 255, "", "Host key verification failed."))
        result = hb.BackupRunner(self.paths, self.config, transport, clock=self.clock).run()
        self.assertFalse(result["ok"])
        self.assertIn("Host key verification failed", result["reason"])

    def test_remote_self_attested_tamper_cannot_replace_local_envelope(self):
        forged = "f" * 64

        def fake_run(argv, *, timeout):
            output = f"{forged}  CHECKSUMS\n{forged}  MANIFEST.json\nVERIFIED\n" if "sha256sum" in " ".join(argv) else ""
            return subprocess.CompletedProcess(argv, 0, output, "")

        transport = hb.SshRsyncTransport(
            hb.parse_destination("backup@example-host:/srv/backups/herdr"),
            ["-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes"],
            runner=fake_run,
        )
        result = hb.BackupRunner(self.paths, self.config, transport, clock=self.clock).run()
        self.assertFalse(result["ok"])
        self.assertIn("remote verification failed", result["reason"])


class GitTests(BackupBase):
    def _git(self, *args, cwd):
        return subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True, env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}).stdout

    def _repo_with_remote(self):
        remote = Path(self.root) / "origin.git"
        self._git("init", "--bare", "-q", "-b", "main", str(remote), cwd=self.root)
        repo = Path(self.root) / "repo"
        self._git("init", "-q", "-b", "main", str(repo), cwd=self.root)
        (repo / "f.txt").write_text("1\n")
        self._git("add", ".", cwd=repo)
        self._git("commit", "-q", "-m", "one", cwd=repo)
        self._git("remote", "add", "origin", str(remote), cwd=repo)
        self._git("push", "-q", "origin", "main", cwd=repo)
        return repo, remote

    def test_git_assessment_distinguishes_protected_from_local_work(self):
        repo, _ = self._repo_with_remote()
        assessment = hb.git_assess(repo)
        self.assertTrue(assessment["remote_committed_state_protected"])
        self.assertFalse(assessment["local_unprotected_work"])
        (repo / "g.txt").write_text("2\n")
        self._git("add", "g.txt", cwd=repo)
        self._git("commit", "-q", "-m", "two", cwd=repo)
        (repo / "untracked.txt").write_text("u\n")
        before = self._git("rev-parse", "HEAD", cwd=repo)
        assessment = hb.git_assess(repo)
        self.assertEqual(assessment["ahead"], 1)
        self.assertEqual(assessment["untracked"], 1)
        self.assertFalse(assessment["remote_committed_state_protected"])
        self.assertTrue(assessment["local_unprotected_work"])
        # Read-only: no commit, push, or history change happened.
        self.assertEqual(before, self._git("rev-parse", "HEAD", cwd=repo))
        self.assertEqual(hb.git_assess(repo)["ahead"], 1)

    def test_git_recovery_strategy_never_claims_protection_for_local_work(self):
        repo, _ = self._repo_with_remote()
        (repo / "dirty.txt").write_text("d\n")
        config = hb.validate_backup_config({**json.loads(json.dumps(hb.DEFAULT_BACKUP_CONFIG)), "enabled": True, "strategy": "git_recovery", "git_repos": [{"path": str(repo), "remote": "origin", "branch": "main", "bundle": False}]})
        result = hb.BackupRunner(self.paths, config, None, clock=self.clock).run()
        self.assertTrue(result["ok"])
        self.assertTrue(result["remote_committed_state_protected"])
        self.assertFalse(result["local_work_protected"])
        self.assertFalse(result["recoverable_complete"])
        health = hb.health(config, hb.load_backup_state(self.paths), self.clock())
        self.assertIn(health["status"], ("failed", "unverified"))
        self.assertTrue(any("uncommitted" in item for item in health["not_protected"]))

    def test_deleted_remote_is_unverified_and_does_not_advance_last_verified(self):
        repo, remote = self._repo_with_remote()
        config = hb.validate_backup_config({
            **json.loads(json.dumps(hb.DEFAULT_BACKUP_CONFIG)),
            "enabled": True,
            "strategy": "git_recovery",
            "git_repos": [{"path": str(repo), "remote": "origin", "branch": "main", "bundle": False}],
        })
        first = hb.BackupRunner(self.paths, config, None, clock=self.clock).run()
        self.assertTrue(first["recoverable_complete"])
        verified = hb.load_backup_state(self.paths)["last_verified"]
        hb.shutil.rmtree(remote)
        self.clock.now += 3600
        second = hb.BackupRunner(self.paths, config, None, clock=self.clock).run()
        self.assertFalse(second["remote_committed_state_protected"])
        self.assertFalse(second["recoverable_complete"])
        self.assertEqual(hb.load_backup_state(self.paths)["last_verified"], verified)

    def test_missing_or_divergent_remote_branch_is_not_protected(self):
        repo, remote = self._repo_with_remote()
        missing = hb.git_assess(repo, branch="missing")
        self.assertFalse(missing["remote_checked"])
        self.assertFalse(missing["remote_committed_state_protected"])

        other = Path(self.root) / "other"
        self._git("clone", "-q", str(remote), str(other), cwd=self.root)
        (other / "f.txt").write_text("remote changed\n")
        self._git("add", "f.txt", cwd=other)
        self._git("commit", "-q", "-m", "remote change", cwd=other)
        self._git("push", "-q", "origin", "main", cwd=other)
        changed = hb.git_assess(repo)
        self.assertTrue(changed["remote_checked"])
        self.assertNotEqual(changed["head"], changed["remote_head"])
        self.assertFalse(changed["remote_committed_state_protected"])

    def test_ignored_files_are_reported_as_unprotected(self):
        repo, _ = self._repo_with_remote()
        (repo / ".gitignore").write_text("ignored.dat\n")
        self._git("add", ".gitignore", cwd=repo)
        self._git("commit", "-q", "-m", "ignore fixture", cwd=repo)
        self._git("push", "-q", "origin", "main", cwd=repo)
        (repo / "ignored.dat").write_text("local only\n")
        assessment = hb.git_assess(repo)
        self.assertEqual(assessment["ignored"], 1)
        self.assertTrue(assessment["remote_committed_state_protected"])
        self.assertTrue(assessment["local_unprotected_work"])

    def test_git_bundle_included_in_snapshot_and_restore_git_into_new_dir(self):
        repo, remote = self._repo_with_remote()
        config = hb.validate_backup_config({**self.config, "git_repos": [{"path": str(repo), "remote": "origin", "branch": "main", "bundle": True}]})
        result = hb.BackupRunner(self.paths, config, hb.LocalDirTransport(self.remote), clock=self.clock).run()
        self.assertTrue(result["ok"], result)
        bundle = self.remote / "snapshots" / result["backup_id"] / "bundles" / "repo.bundle"
        self.assertTrue(bundle.exists())
        self._git("bundle", "verify", str(bundle), cwd=repo)  # raises on a corrupt bundle
        dest = Path(self.root) / "repo-restored"
        restored = hb.restore_git({"path": str(repo), "remote": "origin", "branch": "main"}, dest)
        self.assertTrue(restored["ok"])
        self.assertEqual(restored["head"], self._git("rev-parse", "HEAD", cwd=repo).strip())
        with self.assertRaises(hs.SupervisorError):
            hb.restore_git({"path": str(repo)}, dest)


class IntegrationTests(BackupBase):
    def test_doctor_reports_backup_and_never_errors_when_disabled(self):
        summary = hs.backup_doctor_summary(self.paths, self.clock())
        self.assertEqual(summary["status"], "disabled")
        hb.backup_config_file(self.paths).write_text("{not json")
        summary = hs.backup_doctor_summary(self.paths, self.clock())
        self.assertEqual(summary["status"], "failed")
        self.assertIn("unreadable", summary["failure_reason"])

    def test_health_event_rendered_for_telegram_and_stale_offers_retry(self):
        config = {**self.config, "notify_telegram": True}
        self.runner(config).run()
        self.clock.now += 48 * 3600
        event_id = hb.emit_health_event(self.paths, config, self.clock())
        event = json.loads((self.paths.outbox_dir / "events" / f"{event_id}.json").read_text())
        self.assertEqual(event["type"], "BACKUP_HEALTH")
        self.assertFalse(event["actionable"])
        rendered = hp.render_event(event, None, None, "UTC")
        self.assertIn("Backup stale", rendered.html)
        self.assertEqual(rendered.keyboard[0][0][1], "backup_now")
        self.assertIsNone(hb.emit_health_event(self.paths, {**config, "notify_telegram": False}, self.clock()))

    def test_backup_source_never_uses_llm_or_task_state(self):
        text = (_ROOT / "src" / "herdr_backup.py").read_text()
        for forbidden in ("herdr agent", ".prompt(", "send_keys", "read_state(", "write_state(", "StateStore", "shell=True"):
            self.assertNotIn(forbidden, text, forbidden)
        for forbidden_call in ('"commit"', '"push"', '"merge"', '"rebase"', '"reset"', '"checkout"'):
            self.assertNotRegex(text, rf"subprocess\.run\([^\n]+{forbidden_call}")
        for network_mutation in ("iptables", "nft ", "ufw ", "wg-quick", "openvpn", ".bind(", ".listen("):
            self.assertNotRegex(text, rf"subprocess\.run\([^\n]+{re.escape(network_mutation)}")

    def test_units_and_installer_do_not_enable_backup_timer(self):
        service = (_ROOT / "systemd" / "herdr-backup.service").read_text()
        timer = (_ROOT / "systemd" / "herdr-backup.timer").read_text()
        self.assertIn("run --scheduled", service)
        self.assertIn("ExecStart=/usr/bin/env %h/.local/bin/herdr-backup run --scheduled", service)
        self.assertIn("OnCalendar=hourly", timer)
        self.assertIn("Persistent=true", timer)
        self.assertIn("RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6", service)
        self.assertIn("ConditionPathExists=%h/.config/herdr-supervisor/backup.json", service)
        installer = _ROOT / "install.sh"
        if installer.exists():
            text = installer.read_text()
            self.assertNotRegex(text, r"systemctl[^\n]*(enable|start)[^\n]*backup")


if __name__ == "__main__":
    unittest.main()
