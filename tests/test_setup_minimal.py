"""Preview-first behaviour and the minimal inactive profile: zero writes/commands in a preview, honest inactive
result, preserved configuration/state, conflicts refused before any write, and approvals bound to the candidate,
options, target and preimages."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import unittest
from pathlib import Path
from unittest import mock

import herdr_installation as hi
from setup_fixtures import UNITS, SetupCase, copy_payload, snapshot

ROOT = Path(__file__).resolve().parents[1]


class PreviewTests(SetupCase):
    def test_default_invocation_writes_nothing_and_runs_no_command(self) -> None:
        before = snapshot(self.home)
        for argv in (("setup",), ("setup", "--profile", "minimal"), ("update",), ("rollback",)):
            with self.subTest(argv):
                code, out, err = self.run_tool(*argv, "--payload", str(self.payload)) if argv[0] != "rollback" else self.run_tool(*argv)
                self.assertIn(code, (0, 4), err)
                self.assertEqual(snapshot(self.home), before, "a preview must leave every target file untouched")
                self.assertEqual(self.host.calls, [], "a preview of the minimal profile runs no external command")
        code, out, _ = self.run_tool("setup", "--payload", str(self.payload))
        self.assertEqual(code, 0)
        self.assertIn("Approval hash:", out)
        self.assertIn("none are enabled, started or restarted", out)
        self.assertIn("nothing below has been done", out)

    def test_unit_validation_in_a_preview_is_the_only_command_and_reads_a_scratch_home(self) -> None:
        runtime = self.tmp / "runtime"
        runtime.mkdir()
        envs: list[dict[str, str] | None] = []
        original = self.host.run

        def spy(argv: list[str], **kwargs: object) -> hi.CmdResult:
            envs.append(kwargs.get("env"))  # type: ignore[arg-type]
            return original(argv, **kwargs)  # type: ignore[arg-type]

        self.host.run = spy  # type: ignore[method-assign]
        before = snapshot(self.home)
        with mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(runtime)}):
            code, out, err = self.run_tool("setup", "--payload", str(self.payload))
        self.assertEqual(code, 0, err)
        self.assertEqual([Path(call[0]).name for call in self.host.calls], ["systemd-analyze"])
        self.assertEqual(self.host.calls[0][1:3], ["--user", "verify"])
        self.assertEqual(len(self.host.calls[0]) - 3, len(UNITS), "every candidate unit is verified")
        env = envs[0] or {}
        self.assertNotEqual(env.get("HOME"), str(self.home), "validation stages units under a scratch HOME so the real one is not read or written")
        self.assertEqual(snapshot(self.home), before)
        self.assertEqual(self.host.service_calls(), [])

    def test_failed_unit_validation_refuses_before_any_write(self) -> None:
        runtime = self.tmp / "runtime"
        runtime.mkdir()
        self.host.analyze_rc, self.host.analyze_output = 1, "herdr-telegram.service: bad setting"
        before = snapshot(self.home)
        with mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(runtime)}):
            code, _out, err = self.run_tool("setup", "--payload", str(self.payload), "--apply", "--approve-current")
            self.assertEqual(code, hi.EXIT_REFUSED, err)
            previewed = self.plan_json("setup", "--payload", str(self.payload))
        self.assertTrue(any("unit validation failed" in item for item in previewed["blockers"]))
        self.assertEqual(snapshot(self.home), before, "a defective unit is found before the first target write, not after")

    def test_plan_out_is_a_separate_explicit_output(self) -> None:
        out_file = self.tmp / "plan.json"
        before = snapshot(self.home)
        code, out, err = self.run_tool("setup", "--payload", str(self.payload), "--plan-out", str(out_file))
        self.assertEqual(code, 0, err)
        self.assertEqual(snapshot(self.home), before)
        document = json.loads(out_file.read_text())
        self.assertEqual(stat.S_IMODE(out_file.stat().st_mode), 0o600)
        self.assertEqual(document["approval_hash"], hi.canonical_hash(document["plan"]))
        self.assertIn(document["approval_hash"], out)
        code, _out, err = self.run_tool("setup", "--payload", str(self.payload), "--plan-out", str(out_file))
        self.assertEqual(code, hi.EXIT_USAGE, "an existing plan file is never overwritten")
        self.assertIn("already exists", err)

    def test_apply_requires_an_approval(self) -> None:
        code, _out, err = self.run_tool("setup", "--payload", str(self.payload), "--apply")
        self.assertEqual(code, hi.EXIT_USAGE)
        self.assertIn("approval", err)
        self.assertFalse(self.layout.lib.exists())

    def test_refuses_to_run_as_root(self) -> None:
        with mock.patch("os.geteuid", return_value=0):
            code, _out, err = self.run_tool("setup", "--payload", str(self.payload))
        self.assertEqual(code, hi.EXIT_USAGE)
        self.assertIn("root", err)


class MinimalInstallTests(SetupCase):
    def test_minimal_install_is_inactive_complete_and_exact(self) -> None:
        self.install()
        payload = hi.load_payload(self.payload)
        generation = self.layout.generations / payload.generation_id
        self.assertEqual(os.readlink(self.layout.active), f"generations/{payload.generation_id}")
        for item in payload.of_role("program"):
            installed = generation / item.name
            self.assertEqual(installed.read_bytes(), payload.blobs[item.key])
            self.assertEqual(stat.S_IMODE(installed.stat().st_mode), 0o444, "generation files are read-only")
        self.assertEqual(stat.S_IMODE(generation.stat().st_mode), 0o555)
        for item in payload.files:
            if item.role == "program":
                continue
            target = self.layout.destination(item.role, item.name)
            self.assertEqual(target.read_bytes(), payload.blobs[item.key], item.key)
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), item.mode, item.key)
        for directory in (self.layout.sup_config.parent, self.layout.tg_config.parent, self.layout.state, self.layout.tg_state, self.layout.tg_locks, self.layout.state / "query", self.layout.state / "codex-reset/pending", self.layout.manager):
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700, str(directory))
        for config in (self.layout.sup_config, self.layout.backup_config, self.layout.tg_config):
            self.assertEqual(stat.S_IMODE(config.stat().st_mode), 0o600)
        self.assertEqual(json.loads(self.layout.tg_config.read_text())["mode"], "unconfigured")
        self.assertEqual(self.host.service_calls(), [], "minimal never reloads, enables, starts, stops or restarts anything")
        self.assertFalse([call for call in self.host.calls if Path(call[0]).name in ("systemctl", "herdr")], "no systemd or Herdr command at all")
        receipt = self.receipt()
        self.assertEqual((receipt["kind"], receipt["generation_id"], receipt["previous"]), ("setup", payload.generation_id, None))
        self.assertEqual(len(receipt["assets"]), len(payload.files) - len(payload.of_role("program")))
        journal = json.loads((self.layout.ops / f"{receipt['operation_id']}.json").read_text())
        self.assertEqual((journal["status"], {step["status"] for step in journal["steps"]}), ("COMPLETE", {"DONE"}))

    def test_launchers_resolve_the_active_generation_for_real(self) -> None:
        self.install()
        env = {**os.environ, "HOME": str(self.home)}
        for name in ("herdr-supervisor", "herdr-telegram", "herdr-quota-dependency", "herdr-setup", "herdr-backup", "herdr-query-worker", "herdr-codex-reset-worker"):
            done = subprocess.run([str(self.layout.bin / name), "--help"], env=env, capture_output=True, text=True, timeout=60)
            self.assertEqual(done.returncode, 0, f"{name}: {done.stderr}")
        shown = subprocess.run([str(self.layout.bin / "herdr-quota-dependency"), "--json", "show-lock"], env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(json.loads(shown.stdout)["lock"], json.loads((ROOT / "config/quota-dependency.json").read_text()), "the lock is found beside the generation's modules")

    def test_repeating_the_apply_is_a_noop_that_writes_nothing(self) -> None:
        self.install()
        before = snapshot(self.home)
        self.reset_calls()
        code, out, err = self.run_tool("setup", "--payload", str(self.payload), "--apply", "--approve-current")
        self.assertEqual(code, 0, err)
        self.assertIn("NOOP", out)
        self.assertEqual(snapshot(self.home), before)
        self.assertEqual(self.host.calls, [])

    def test_existing_configuration_credentials_and_state_are_preserved_byte_for_byte(self) -> None:
        keep = {
            self.layout.sup_config: b'{"human": "keep"}\n', self.layout.tg_config: b'{"schema_version": 1, "mode": "unconfigured"}\n',
            self.layout.sup_config.parent / "bot-token": b"1:secret\n", self.layout.state / "owners.json": b'{"codex": {}, "claude": {}}\n', self.layout.state / "state.json": b"{}\n",
            self.layout.state / "logs/run.jsonl": b"{}\n", self.layout.state / "inbox/pending/q.json": b"{}\n",
        }
        for path, data in keep.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            path.chmod(0o600)
        before = {path: (path.read_bytes(), stat.S_IMODE(path.stat().st_mode)) for path in keep}
        self.install()
        for path, (data, mode) in before.items():
            self.assertEqual((path.read_bytes(), stat.S_IMODE(path.stat().st_mode)), (data, mode), f"{path} must not change")
        self.assertTrue(self.layout.backup_config.is_file(), "only the absent configuration file was created")

    def test_conflicting_existing_files_refuse_the_whole_operation_before_any_write(self) -> None:
        unit = self.layout.unit / "herdr-telegram.service"
        unit.parent.mkdir(parents=True)
        unit.write_text("[Unit]\nDescription=someone else's unit\n")
        before = snapshot(self.home)
        code, out, err = self.run_tool("setup", "--payload", str(self.payload), "--apply", "--approve-current")
        self.assertEqual(code, hi.EXIT_REFUSED)
        self.assertIn("herdr-telegram.service", out + err)
        self.assertEqual(snapshot(self.home), before, "a refusal leaves nothing behind (no journal, no generation, no directories)")
        self.assertEqual(self.host.calls, [])

    def test_identical_existing_assets_are_adopted_without_conflict(self) -> None:
        payload = hi.load_payload(self.payload)
        item = next(item for item in payload.files if item.key == "launcher/herdr-telegram")
        target = self.layout.destination("launcher", "herdr-telegram")
        target.write_bytes(payload.blobs[item.key])
        target.chmod(0o755)
        self.install()
        self.assertEqual(target.read_bytes(), payload.blobs[item.key])

    def test_symlinked_and_special_targets_are_never_followed_or_replaced(self) -> None:
        elsewhere = self.tmp / "elsewhere"
        elsewhere.write_text("do not touch\n")
        launcher = self.layout.bin / "herdr-backup"
        launcher.symlink_to(elsewhere)
        before = snapshot(self.home)
        code, out, err = self.run_tool("setup", "--payload", str(self.payload), "--apply", "--approve-current")
        self.assertEqual(code, hi.EXIT_REFUSED)
        self.assertIn("symlink", out + err)
        self.assertEqual(elsewhere.read_text(), "do not touch\n")
        self.assertEqual(snapshot(self.home), before)

    def test_unknown_legacy_layout_refuses_instead_of_copying_over_it(self) -> None:
        self.layout.lib.mkdir(parents=True)
        for name in ("herdr_supervisor.py", "herdr_core.py", "herdr_private_extra.py"):
            (self.layout.lib / name).write_text("# unknown private code\n")
        before = snapshot(self.home)
        code, out, err = self.run_tool("setup", "--payload", str(self.payload), "--apply", "--approve-current")
        self.assertEqual(code, hi.EXIT_REFUSED)
        self.assertIn("legacy", out + err)
        self.assertEqual(snapshot(self.home), before)

    def test_missing_herdr_executable_refuses_the_minimal_profile(self) -> None:
        (self.layout.bin / "herdr").unlink()
        code, out, err = self.run_tool("setup", "--payload", str(self.payload), "--apply", "--approve-current")
        self.assertEqual(code, hi.EXIT_REFUSED)
        self.assertIn("Herdr executable not found", out + err)
        self.assertFalse(self.layout.lib.exists())


class ExplicitMigrationTests(SetupCase):
    def test_state_migration_runs_only_when_selected_and_after_the_smoke_check(self) -> None:
        self.install()
        self.assertFalse([call for call in self.host.calls if call[1:2] == ["migrate"]], "an install never migrates state implicitly")
        newer = self.candidate("migrating", "0.3.1")
        self.reset_calls()
        code, out, err = self.run_tool("update", "--payload", str(newer), "--apply", "--approve-current", "--migrate")
        self.assertEqual(code, 0, out + err)
        migrate = [call for call in self.host.calls if call[1:2] == ["migrate"]]
        self.assertEqual(len(migrate), 1)
        journal = max((json.loads(path.read_text()) for path in self.layout.ops.glob("*.json")), key=lambda item: item["started_at"])
        ids = [step["id"] for step in journal["steps"]]
        self.assertLess(ids.index("smoke"), ids.index("migrate"))
        self.assertTrue((self.layout.state / "state.json").exists() is False, "no task state existed, so none was created by the migration")


class ApprovalBindingTests(SetupCase):
    def saved(self, *extra: str, payload: Path | None = None) -> tuple[Path, str]:
        path = self.tmp / f"plan-{len(list(self.tmp.glob('plan-*.json')))}.json"
        code, out, err = self.run_tool("setup", "--payload", str(payload or self.payload), "--plan-out", str(path), *extra)
        self.assertEqual(code, 0, out + err)
        return path, json.loads(path.read_text())["approval_hash"]

    def apply(self, path: Path, digest: str) -> tuple[int, str, str]:
        return self.run_tool("setup", "--apply", "--plan", str(path), "--approve", digest)

    def test_exact_hash_applies_a_saved_plan(self) -> None:
        path, digest = self.saved()
        code, out, err = self.apply(path, digest)
        self.assertEqual(code, 0, out + err)
        self.assertIn("COMPLETE", out)
        self.assertEqual(self.receipt()["approval_hash"], digest)

    def test_a_different_hash_or_an_edited_plan_is_refused(self) -> None:
        path, digest = self.saved()
        before = snapshot(self.home)
        code, _out, err = self.apply(path, "0" * 64)
        self.assertEqual((code, "hash" in err), (hi.EXIT_REFUSED, True))
        document = json.loads(path.read_text())
        document["plan"]["options"]["migrate"] = True
        edited = self.tmp / "edited.json"
        edited.write_text(json.dumps(document))
        code, _out, err = self.apply(edited, digest)
        self.assertEqual((code, "edited" in err), (hi.EXIT_REFUSED, True))
        self.assertEqual(snapshot(self.home), before)

    def test_changed_candidate_content_invalidates_the_approval(self) -> None:
        path, digest = self.saved()
        (self.payload / "src/herdr_cli.py").write_text((self.payload / "src/herdr_cli.py").read_text() + "\n# swapped after review\n")
        before = snapshot(self.home)
        code, out, err = self.apply(path, digest)
        self.assertEqual(code, hi.EXIT_REFUSED, out + err)
        self.assertIn("stale", out + err)
        self.assertEqual(snapshot(self.home), before)

    def test_changed_preimage_invalidates_the_approval(self) -> None:
        path, digest = self.saved()
        target = self.layout.destination("launcher", "herdr-telegram")
        target.write_text("#!/bin/sh\n# appeared after the plan\n")
        before = snapshot(self.home)
        code, out, err = self.apply(path, digest)
        self.assertEqual(code, hi.EXIT_REFUSED, out + err)
        self.assertEqual(snapshot(self.home), before)

    def test_changed_target_identity_invalidates_the_approval(self) -> None:
        path, digest = self.saved()
        moved = self.tmp / "other-lib"
        with mock.patch.dict(os.environ, {"HERDR_SUPERVISOR_LIB": str(moved)}):
            env = hi.Env(hi.Layout.from_environment(self.home), host=self.host, lock_wait=1.0)
            code, out, err = self.run_tool("setup", "--apply", "--plan", str(path), "--approve", digest, env=env)
        self.assertEqual(code, hi.EXIT_REFUSED, out + err)
        self.assertFalse(moved.exists())
        self.assertFalse(self.layout.lib.exists())

    def test_the_running_tool_is_part_of_the_approval(self) -> None:
        path, digest = self.saved()
        with mock.patch("herdr_setup.tool_identity", return_value="another-tool"):
            code, out, err = self.apply(path, digest)
        self.assertEqual(code, hi.EXIT_REFUSED, out + err)
        self.assertIn("tool", out + err)

    def test_a_modified_dependency_lock_or_launcher_invalidates_the_approval(self) -> None:
        """Every executable or dependency input is inside the hashed payload: the pinned lock, a launcher, a unit."""
        for name in ("config/quota-dependency.json", "bin/herdr-telegram", "systemd/herdr-telegram.service"):
            with self.subTest(name):
                path, digest = self.saved()
                target = self.payload / name
                original = target.read_bytes()
                target.write_bytes(original + (b" " if name.endswith(".json") else b"\n# modified after review\n"))
                before = snapshot(self.home)
                code, out, err = self.apply(path, digest)
                self.assertEqual(code, hi.EXIT_REFUSED, out + err)
                self.assertIn("stale", out + err)
                self.assertEqual(snapshot(self.home), before)
                target.write_bytes(original)

    def test_options_change_the_hash(self) -> None:
        _path, plain = self.saved()
        _path, migrate = self.saved("--migrate")
        self.assertNotEqual(plain, migrate)

    def test_a_saved_plan_for_another_operation_is_not_accepted(self) -> None:
        path, digest = self.saved()
        code, _out, err = self.run_tool("update", "--apply", "--plan", str(path), "--approve", digest)
        self.assertEqual(code, hi.EXIT_USAGE)
        self.assertIn("setup", err)

    def test_the_same_plan_applied_twice_is_idempotent(self) -> None:
        path, digest = self.saved()
        self.assertEqual(self.apply(path, digest)[0], 0)
        before = snapshot(self.home)
        code, out, _err = self.apply(path, digest)
        self.assertEqual(code, 0)
        self.assertIn("ALREADY_APPLIED", out)
        self.assertEqual(snapshot(self.home), before)

    def test_blockers_make_a_plan_unappliable_even_with_its_hash(self) -> None:
        self.layout.lib.mkdir(parents=True)
        (self.layout.lib / "herdr_supervisor.py").write_text("x")
        path = self.tmp / "blocked-plan.json"
        code, _out, _err = self.run_tool("setup", "--payload", str(self.payload), "--plan-out", str(path))
        self.assertEqual(code, hi.EXIT_REFUSED, "the preview itself reports the refusal")
        digest = json.loads(path.read_text())["approval_hash"]
        code, out, err = self.apply(path, digest)
        self.assertEqual(code, hi.EXIT_REFUSED, out + err)
        self.assertFalse((self.layout.generations).exists())


class InstallScriptTests(SetupCase):
    """./install.sh and ./uninstall.sh keep their documented semantics, now delegating to the minimal profile."""

    def run_script(self, script: str, *args: str, home: Path | None = None) -> subprocess.CompletedProcess[str]:
        env = {**os.environ, "HOME": str(home or self.home), "PATH": f"{self.fake_bin()}:/usr/bin:/bin"}
        env.pop("XDG_RUNTIME_DIR", None)
        return subprocess.run([str(ROOT / script), *args], cwd=ROOT, env=env, capture_output=True, text=True, timeout=120, check=False)

    def fake_bin(self) -> Path:
        path = self.tmp / "fake-bin"
        if not path.exists():
            path.mkdir()
            (path / "systemctl").write_text("#!/bin/sh\necho inactive\nexit 3\n")
            (path / "systemctl").chmod(0o755)
        return path

    def test_install_script_installs_inactive_and_says_so(self) -> None:
        result = self.run_script("install.sh")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("No service was loaded, enabled, started, or restarted.", result.stdout)
        self.assertTrue((self.layout.lib / "active").is_symlink())
        self.assertTrue((self.layout.bin / "herdr-setup").is_file())
        again = self.run_script("install.sh")
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertIn("NOOP", again.stdout)

    def test_dry_run_previews_and_creates_nothing(self) -> None:
        before = snapshot(self.home)
        result = self.run_script("install.sh", "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Found Herdr executable", result.stdout)
        self.assertIn("Approval hash", result.stdout)
        self.assertEqual(snapshot(self.home), before)

    def test_install_refuses_a_busy_or_different_installation_instead_of_overwriting(self) -> None:
        self.assertEqual(self.run_script("install.sh").returncode, 0)
        newer = self.candidate("newer", "0.3.1")
        del newer
        self.layout.state.joinpath("inbox/pending").mkdir(parents=True)
        self.layout.state.joinpath("inbox/pending/x.json").write_text("{}")
        # the same payload is simply installed (nothing to do) even while work is queued; setup never updates
        again = self.run_script("install.sh")
        self.assertEqual(again.returncode, 0, again.stderr)

    def test_missing_herdr_is_refused_before_anything_is_created(self) -> None:
        (self.layout.bin / "herdr").unlink()
        before = snapshot(self.home)
        for args in (("--dry-run",), ()):
            result = self.run_script("install.sh", *args)
            self.assertEqual(result.returncode, 2)
            self.assertIn("Herdr executable not found", result.stderr)
        self.assertEqual(snapshot(self.home), before)

    def test_uninstall_removes_generations_and_preserves_state(self) -> None:
        self.assertEqual(self.run_script("install.sh").returncode, 0)
        marker = self.layout.state / "human-state.json"
        marker.write_text("keep\n")
        result = self.run_script("uninstall.sh")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.layout.lib.exists())
        self.assertFalse((self.layout.bin / "herdr-setup").exists())
        self.assertTrue(marker.exists(), "uninstall never removes state")
        self.assertTrue(self.layout.receipt.exists(), "the installation record is state and is preserved")


class CandidateHelperTests(SetupCase):
    def test_copy_payload_builds_an_independent_loadable_candidate(self) -> None:
        candidate = copy_payload(self.tmp / "cand", version="0.4.0")
        payload = hi.load_payload(candidate)
        self.assertEqual(payload.version, "0.4.0")
        self.assertNotEqual(payload.program_id, hi.load_payload(self.payload).program_id)


if __name__ == "__main__":
    unittest.main()
