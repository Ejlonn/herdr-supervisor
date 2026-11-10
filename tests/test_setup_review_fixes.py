"""Regressions for the adversarial review of the installation manager: an interrupted approval is not authority to change the
enforcing tool or ignore new conditions, persisted receipt/backup/generation metadata is untrusted, the quiet point is one rule
re-checked inside the mutation window, dependency inputs and external-step postconditions are bound exactly, readiness uses
exact live identities and durable enablement, and the installed tool finds its payload only when told. Each test drives the
real planners/apply/readiness through the same fixtures as the nominal suites."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import threading
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import herdr_installation as hi
import herdr_setup as hsetup
import herdr_telegram as htg
from setup_fixtures import FakeQuota, SetupCase, snapshot, touch_task_state
from test_setup_full import FullCase
from test_setup_update import UpdateCase, edit_unit
from v2_fixtures import FAKE_TOKEN


class InterruptedCase(UpdateCase):
    def saved_plan(self, *extra: str) -> tuple[Path, str]:
        path = self.tmp / f"plan-{len(list(self.tmp.glob('plan-*.json')))}.json"
        code, out, err = self.run_tool("update", "--payload", str(self.v2), "--plan-out", str(path), *extra)
        self.assertEqual(code, 0, out + err)
        return path, json.loads(path.read_text())["approval_hash"]

    def crash_at(self, path: Path, digest: str, point: str) -> None:
        def fault(name: str) -> None:
            if name == point:
                raise hi.Crash(point)

        env = hi.Env(self.layout, host=self.host, lock_wait=1.0, fault=fault)
        with self.assertRaises(hi.Crash):
            self.run_tool("update", "--apply", "--plan", str(path), "--approve", digest, env=env)

    def resume(self, path: Path, digest: str) -> tuple[int, str, str]:
        return self.run_tool("update", "--apply", "--plan", str(path), "--approve", digest)

    def journal(self, digest: str) -> dict[str, Any]:
        return json.loads((self.layout.ops / f"{digest[:32]}.json").read_text())  # type: ignore[no-any-return]


class ResumeAuthorityTests(InterruptedCase):
    """CI1: a journal is not authority to change the executing tool, the target, or to ignore conditions that arose afterwards."""

    def test_a_changed_enforcing_tool_cannot_finish_an_interrupted_approval(self) -> None:
        path, digest = self.saved_plan()
        self.crash_at(path, digest, "before:dirs")
        with mock.patch("herdr_setup.tool_identity", return_value="an-unreviewed-enforcer"):
            code, out, err = self.resume(path, digest)
        self.assertEqual(code, hi.EXIT_REFUSED, out + err)
        self.assertIn("tool changed since the approval", out + err)
        self.assertEqual(self.active_generation(), self.old_generation)
        self.assertNotEqual(self.journal(digest)["status"], "COMPLETE")
        code, out, err = self.resume(path, digest)
        self.assertEqual(code, 0, "the genuinely approved tool still completes it: " + out + err)

    def test_a_changed_target_or_option_set_cannot_continue(self) -> None:
        path, digest = self.saved_plan()
        self.crash_at(path, digest, "after:generation")
        moved = self.tmp / "elsewhere"
        with mock.patch.dict(os.environ, {"HERDR_SUPERVISOR_LIB": str(moved)}):
            env = hi.Env(hi.Layout.from_environment(self.home), host=self.host, lock_wait=1.0)
            code, out, err = self.run_tool("update", "--apply", "--plan", str(path), "--approve", digest, env=env)
        self.assertEqual(code, hi.EXIT_REFUSED, out + err)
        self.assertIn("target changed", out + err)
        self.assertFalse(moved.exists())

    def test_work_that_arrives_between_interruption_and_resume_stops_the_resume(self) -> None:
        path, digest = self.saved_plan()
        self.crash_at(path, digest, "after:generation")
        queued = self.layout.state / "inbox/pending/late.json"
        queued.parent.mkdir(parents=True, exist_ok=True)
        queued.write_text("{}")
        before = snapshot(self.home)
        code, out, err = self.resume(path, digest)
        self.assertEqual(code, hi.EXIT_REFUSED, out + err)
        self.assertIn("quiet point", out + err)
        self.assertEqual(snapshot(self.home), before, "a refused resume performs no further side effect")
        queued.unlink()
        self.assertEqual(self.resume(path, digest)[0], 0)

    def test_a_unit_that_started_running_in_the_meantime_stops_the_resume(self) -> None:
        path, digest = self.saved_plan()
        self.crash_at(path, digest, "after:generation")
        self.host.active["herdr-query-worker.service"] = "active"
        self.reset_calls()
        code, out, err = self.resume(path, digest)
        self.assertEqual(code, hi.EXIT_REFUSED, out + err)
        self.assertIn("became active outside this operation", out + err)
        self.assertEqual(self.host.service_calls(), [])

    def test_a_managed_file_changed_by_someone_else_in_the_meantime_stops_the_resume(self) -> None:
        path, digest = self.saved_plan()
        self.crash_at(path, digest, "after:backup")
        unit = self.layout.unit / "herdr-telegram.service"
        unit.write_text(unit.read_text() + "# a local change made while the operation was interrupted\n")
        code, out, err = self.resume(path, digest)
        self.assertEqual(code, hi.EXIT_REFUSED, out + err)
        self.assertIn("changed outside this operation", out + err)
        self.assertIn("a local change", unit.read_text())

    def test_journals_and_completion_markers_must_be_bound_to_the_approval(self) -> None:
        path, digest = self.saved_plan()
        self.crash_at(path, digest, "after:generation")
        journal_path = self.layout.ops / f"{digest[:32]}.json"
        pristine = journal_path.read_text()
        cases: dict[str, Any] = {
            "another kind": lambda j: j.update(kind="setup"),
            "another approval": lambda j: j.update(approval_hash="0" * 64),
            "edited plan": lambda j: j["plan"]["options"].update(migrate=True),
            "dropped step": lambda j: j.update(steps=j["steps"][:-1]),
            "renamed step": lambda j: j["steps"][0].update(id="something-else"),
            "bogus status": lambda j: j["steps"][0].update(status="MAYBE"),
            "forged completion": lambda j: j.update(status="COMPLETE"),
            "boolean schema": lambda j: j.update(schema=True),
        }
        for label, mutate in cases.items():
            with self.subTest(label):
                data = json.loads(pristine)
                mutate(data)
                journal_path.write_text(json.dumps(data))
                before = snapshot(self.home)
                code, out, err = self.resume(path, digest)
                self.assertEqual(code, hi.EXIT_REFUSED, f"{label}: {out}{err}")
                self.assertNotIn("ALREADY_APPLIED", out)
                self.assertEqual(snapshot(self.home), before)
        journal_path.write_text(pristine)
        self.assertEqual(self.resume(path, digest)[0], 0, "an honest journal still resumes")


class MetadataTrustTests(UpdateCase):
    """CI2: persisted receipt and backup metadata can never address a file the installation does not own."""

    def test_a_receipt_cannot_make_an_unowned_file_deletable(self) -> None:
        victim = self.home / "private-user-file.txt"
        victim.write_text("keep\n")
        receipt = self.receipt()
        receipt["assets"].append({"dest": str(victim), "role": "doc", "sha256": hashlib.sha256(victim.read_bytes()).hexdigest(), "mode": 0o644})
        self.layout.receipt.write_text(json.dumps(receipt))
        document = self.preview(self.v2)
        self.assertTrue(any("canonical managed destination" in item or "inconsistent" in item for item in document["blockers"]), document["blockers"])
        before = snapshot(self.home)
        code, out, err = self.update(self.v2)
        self.assertEqual(code, hi.EXIT_REFUSED, out + err)
        self.assertTrue(victim.exists())
        self.assertEqual(snapshot(self.home), before)

    def test_malformed_receipts_are_rejected_by_exact_type(self) -> None:
        base = self.receipt()
        bad: dict[str, Any] = {
            "boolean schema": {**base, "schema": True}, "string mode": {**base, "assets": [{**base["assets"][0], "mode": "420"}, *base["assets"][1:]]},
            "boolean mode": {**base, "assets": [{**base["assets"][0], "mode": True}, *base["assets"][1:]]}, "extra key": {**base, "assets": [{**base["assets"][0], "extra": 1}, *base["assets"][1:]]},
            "traversal": {**base, "assets": [{**base["assets"][0], "dest": str(self.layout.bin / ".." / "evil")}, *base["assets"][1:]]},
            "relative": {**base, "assets": [{**base["assets"][0], "dest": "bin/herdr-backup"}, *base["assets"][1:]]},
            "wrong role": {**base, "assets": [{**base["assets"][0], "role": "unit"}, *base["assets"][1:]]},
            "duplicate": {**base, "assets": [*base["assets"], base["assets"][0]]}, "bad hash": {**base, "assets": [{**base["assets"][0], "sha256": "xyz"}, *base["assets"][1:]]},
            "bad operation id": {**base, "operation_id": "short"}, "assets not a list": {**base, "assets": "none"}, "bad previous": {**base, "previous": {"kind": "other"}},
        }
        for label, value in bad.items():
            with self.subTest(label):
                self.layout.receipt.write_text(json.dumps(value))
                self.assertEqual(hi.inspect_install(self.layout).layout, "inconsistent", label)
                self.assert_refused(self.v2, "inconsistent")
        self.layout.receipt.write_text(json.dumps(base))
        self.assertEqual(hi.inspect_install(self.layout).layout, "managed", "the genuine receipt is accepted")

    def rollback_plan(self) -> tuple[Path, str]:
        self.assertEqual(self.update(self.v2)[0], 0)
        saved = self.tmp / "rollback-plan.json"
        code, out, err = self.run_tool("rollback", "--plan-out", str(saved))
        self.assertEqual(code, 0, out + err)
        return saved, json.loads(saved.read_text())["approval_hash"]

    def backup_manifest(self) -> tuple[Path, dict[str, Any]]:
        path = self.layout.backups / self.receipt()["backup"] / "manifest.json"
        return path, json.loads(path.read_text())  # type: ignore[return-value]

    def test_a_backup_record_cannot_point_outside_its_own_directory(self) -> None:
        saved, digest = self.rollback_plan()
        path, data = self.backup_manifest()
        outside = self.home / "private-source.txt"
        outside.write_text("private sentinel outside the managed backup\n")
        entry = next(item for item in data["entries"] if item["dest"].endswith("/INSTALL.md"))
        entry["file"] = str(outside)
        entry["before"]["sha256"] = hi.sha256_bytes(outside.read_bytes())
        path.write_text(json.dumps(data))
        installed = (self.layout.doc / "INSTALL.md").read_bytes()
        preview = self.plan_json("rollback")
        self.assertTrue(preview["blockers"], "the preview itself refuses a backup record that addresses an outside file")
        code, out, err = self.run_tool("rollback", "--apply", "--plan", str(saved), "--approve", digest)
        self.assertEqual(code, hi.EXIT_REFUSED, out + err)
        self.assertEqual((self.layout.doc / "INSTALL.md").read_bytes(), installed, "nothing from outside the backup reached a managed file")

    def test_backup_metadata_variants_are_rejected_without_writing_anything(self) -> None:
        self.rollback_plan()
        path, pristine = self.backup_manifest()
        variants: dict[str, Any] = {
            "dot-dot copy": lambda d: next(e for e in d["entries"] if e["file"]).update(file="../../etc/passwd"),
            "foreign destination": lambda d: d["entries"][0].update(dest=str(self.home / "elsewhere.txt")),
            "boolean mode": lambda d: next(e for e in d["entries"] if e["before"]["state"] == "file")["before"].update(mode=True),
            "boolean schema": lambda d: d.update(schema=True),
            "operation mismatch": lambda d: d.update(operation_id="0" * 32),
            "unknown role": lambda d: d["entries"][0].update(role="program"),
            "extra entry key": lambda d: d["entries"][0].update(extra=1),
            "legacy traversal": lambda d: d.update(legacy_lib={"files": {"../x.py": "0" * 64}}),
        }
        for label, mutate in variants.items():
            with self.subTest(label):
                data = json.loads(json.dumps(pristine))
                mutate(data)
                path.write_text(json.dumps(data))
                before = snapshot(self.home)
                code, out, err = self.run_tool("rollback", "--apply", "--approve-current")
                self.assertEqual(code, hi.EXIT_REFUSED, f"{label}: {out}{err}")
                self.assertEqual(snapshot(self.home), before)
        path.write_text(json.dumps(pristine))
        self.assertEqual(self.run_tool("rollback", "--apply", "--approve-current")[0], 0, "the genuine backup still rolls back")

    def test_backup_content_changed_after_the_preview_invalidates_the_approval(self) -> None:
        saved, digest = self.rollback_plan()
        path, data = self.backup_manifest()
        entry = next(item for item in data["entries"] if item["dest"].endswith("/INSTALL.md") and item["file"])
        copy = path.parent / entry["file"]
        copy.write_bytes(b"replaced backup content\n")
        entry["before"]["sha256"] = hi.sha256_bytes(copy.read_bytes())  # a self-consistent forgery
        path.write_text(json.dumps(data))
        installed = (self.layout.doc / "INSTALL.md").read_bytes()
        code, out, err = self.run_tool("rollback", "--apply", "--plan", str(saved), "--approve", digest)
        self.assertEqual(code, hi.EXIT_REFUSED, out + err)
        self.assertIn("stale", out + err)
        self.assertEqual((self.layout.doc / "INSTALL.md").read_bytes(), installed)


class GenerationAnchorTests(UpdateCase):
    """CI7: a generation is anchored to the identity it is named by and the identity recorded for it."""

    def rewrite(self, gid: str, *, module: str = "herdr_supervisor.py", chmod: bool = True) -> Path:
        directory = self.layout.generations / gid
        os.chmod(directory, 0o755)
        target = directory / module
        os.chmod(target, 0o644)
        target.write_bytes(target.read_bytes() + b"\n# changed after installation\n")
        manifest_path = directory / "GENERATION.json"
        manifest = json.loads(manifest_path.read_text())
        for item in manifest["files"]:
            if item["name"] == module:
                item.update(sha256=hi.sha256_bytes(target.read_bytes()), size=target.stat().st_size)
        manifest["program_id"] = hi.canonical_hash({"version": manifest["version"], "state_schema_version": manifest["state_schema_version"],
                                                    "files": [{"role": "program", **i} for i in sorted(manifest["files"], key=lambda i: i["name"])]})
        os.chmod(manifest_path, 0o644)
        manifest_path.write_text(json.dumps(manifest))
        if chmod:
            for path in (target, manifest_path):
                os.chmod(path, 0o444)
            os.chmod(directory, 0o555)
        return directory

    def test_modified_code_with_a_self_rehashed_manifest_is_not_accepted_under_the_old_name(self) -> None:
        self.rewrite(str(self.old_generation))
        state = hi.inspect_install(self.layout)
        self.assertEqual(state.layout, "inconsistent")
        self.assertIn("name", "; ".join(state.problems))
        self.assert_refused(self.v2, "inconsistent")
        code, out, err = self.run_tool("rollback", "--apply", "--approve-current")
        self.assertEqual(code, hi.EXIT_REFUSED)
        status = json.loads(self.run_tool("--json", "status", adapters=self.adapters(FakeQuota()))[1])
        self.assertEqual(status["overall"], "FAILED")

    def test_the_receipt_identity_must_match_the_generation_it_names(self) -> None:
        receipt = self.receipt()
        receipt["program_id"] = "0" * 64
        self.layout.receipt.write_text(json.dumps(receipt))
        state = hi.inspect_install(self.layout)
        self.assertEqual(state.layout, "inconsistent")
        self.assertIn("program identity", "; ".join(state.problems))

    def test_writable_or_loosened_modes_are_a_verification_failure(self) -> None:
        directory = self.layout.generations / str(self.old_generation)
        for label, path, mode in (("directory", directory, 0o755), ("module", directory / "herdr_cli.py", 0o644), ("manifest", directory / "GENERATION.json", 0o666)):
            with self.subTest(label):
                original = os.stat(path).st_mode & 0o777
                os.chmod(directory, 0o755)
                os.chmod(path, mode)
                manifest, problems = hi.verify_generation(directory, expect_id=str(self.old_generation))
                self.assertTrue([p for p in problems if "read-only" in p], (label, problems))
                os.chmod(path, original)
                os.chmod(directory, 0o555)
        self.assertEqual(hi.verify_generation(directory, expect_id=str(self.old_generation))[1], [])

    def test_manifest_fields_are_exactly_typed(self) -> None:
        directory = self.layout.generations / str(self.old_generation)
        pristine = (directory / "GENERATION.json").read_text()
        os.chmod(directory, 0o755)
        manifest_path = directory / "GENERATION.json"
        for label, mutate in (("boolean schema", lambda m: m.update(schema=True)), ("boolean state schema", lambda m: m.update(state_schema_version=True)),
                              ("bad version", lambda m: m.update(version="main")), ("bad file size", lambda m: m["files"][0].update(size="12")),
                              ("extra file key", lambda m: m["files"][0].update(extra=1)), ("non-hash digest", lambda m: m["files"][0].update(sha256="nothex"))):
            with self.subTest(label):
                data = json.loads(pristine)
                mutate(data)
                os.chmod(manifest_path, 0o644)
                manifest_path.write_text(json.dumps(data))
                os.chmod(manifest_path, 0o444)
                manifest, problems = hi.verify_generation(directory, expect_id=str(self.old_generation))
                self.assertTrue(manifest is None or problems, label)
        os.chmod(manifest_path, 0o644)
        manifest_path.write_text(pristine)
        os.chmod(manifest_path, 0o444)
        os.chmod(directory, 0o555)

    def test_an_auxiliary_only_change_reuses_the_same_verified_generation(self) -> None:
        docs_only = self.candidate("docs-only", "0.3.0-beta.1", lambda root: (root / "docs/INSTALL.md").write_text((root / "docs/INSTALL.md").read_text() + "\ndocs-only-change\n"))
        before = self.active_generation()
        code, out, err = self.update(docs_only)
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.active_generation(), before, "the program did not change, so the generation is reused, not republished")
        self.assertEqual(hi.inspect_install(self.layout).layout, "managed")
        self.assertIn("docs-only-change", (self.layout.doc / "INSTALL.md").read_text())
        self.assertEqual(self.receipt()["program_id"], hi.load_payload(docs_only).program_id)


class QuietPointRuleTests(UpdateCase):
    """CI3: one rule for every mutation, running jobs are never killed, and the predicate is re-evaluated inside the mutation window."""

    def test_a_running_job_service_blocks_instead_of_being_stopped(self) -> None:
        for unit in ("herdr-query-worker.service", "herdr-supervisor-worker.service", "herdr-codex-reset-worker.service", "herdr-backup.service"):
            with self.subTest(unit):
                self.host.active[unit] = "active"
                document = self.preview(self.v2)
                self.assertTrue(any(unit in item and "running a job" in item for item in document["blockers"]), document["blockers"])
                self.assertNotIn(unit, document["plan"]["services"]["stop"])
                self.assert_refused(self.v2, "running a job")
                self.host.active[unit] = "inactive"
        self.host.active["herdr-telegram.service"] = "active"
        self.assertEqual(self.preview(self.v2)["blockers"], [], "the Telegram bridge and triggers are restartable, not jobs")

    def test_setup_cannot_bypass_the_quiet_point_with_a_changed_unit(self) -> None:
        touch_task_state(self.layout, "WAIT_USER")
        same_program = self.candidate("same-program", "0.3.0-beta.1", edit_unit)
        unit_before = (self.layout.unit / "herdr-telegram.service").read_bytes()
        before = snapshot(self.home)
        code, out, err = self.run_tool("setup", "--profile", "minimal", "--payload", str(same_program), "--apply", "--approve-current")
        self.assertEqual(code, hi.EXIT_REFUSED, out + err)
        self.assertIn("quiet point", out + err)
        self.assertEqual((self.layout.unit / "herdr-telegram.service").read_bytes(), unit_before)
        self.assertEqual(snapshot(self.home), before)
        code, out, _err = self.run_tool("setup", "--profile", "minimal", "--payload", str(self.payload), "--apply", "--approve-current")
        self.assertEqual(code, 0, "a pure no-op needs no quiet point")
        self.assertIn("NOOP", out)
        self.host.active["herdr-telegram.service"] = "active"
        self.host.active["herdr-query-worker.service"] = "active"
        touch_task_state(self.layout, "DONE")
        code, out, err = self.run_tool("setup", "--profile", "minimal", "--payload", str(same_program), "--apply", "--approve-current")
        self.assertEqual(code, hi.EXIT_REFUSED, "setup follows the same service rule as update: " + out + err)
        self.assertIn("running a job", out + err)

    def late_work(self, point: str) -> hi.Env:
        def fault(name: str) -> None:
            if name == point:
                queued = self.layout.state / "inbox/pending/late.json"
                queued.parent.mkdir(parents=True, exist_ok=True)
                queued.write_text("{}")

        return hi.Env(self.layout, host=self.host, lock_wait=2.0, fault=fault)

    def test_a_command_that_arrives_after_planning_stops_the_mutation(self) -> None:
        self.host.active["herdr-telegram.service"] = "active"
        path = self.tmp / "plan.json"
        self.assertEqual(self.run_tool("update", "--payload", str(self.v2), "--plan-out", str(path))[0], 0)
        digest = json.loads(path.read_text())["approval_hash"]
        unit_before = (self.layout.unit / "herdr-telegram.service").read_bytes()
        code, out, err = self.run_tool("update", "--apply", "--plan", str(path), "--approve", digest, env=self.late_work("before:assets"))
        self.assertEqual(code, hi.EXIT_REFUSED, out + err)
        self.assertIn("work arrived after the plan was approved", out + err)
        self.assertEqual((self.layout.unit / "herdr-telegram.service").read_bytes(), unit_before, "no asset was replaced")
        self.assertEqual(self.active_generation(), self.old_generation)
        self.assertEqual(self.host.active["herdr-telegram.service"], "active", "the stopped bridge was started again")
        (self.layout.state / "inbox/pending/late.json").unlink()
        code, out, err = self.run_tool("update", "--apply", "--plan", str(path), "--approve", digest)
        self.assertEqual(code, 0, "once the work is gone the same approval resumes: " + out + err)

    def test_work_arriving_just_before_the_pointer_switch_also_stops_it(self) -> None:
        path = self.tmp / "plan.json"
        self.assertEqual(self.run_tool("update", "--payload", str(self.v2), "--plan-out", str(path))[0], 0)
        digest = json.loads(path.read_text())["approval_hash"]
        code, out, err = self.run_tool("update", "--apply", "--plan", str(path), "--approve", digest, env=self.late_work("before:switch"))
        self.assertEqual(code, hi.EXIT_REFUSED, out + err)
        self.assertEqual(self.active_generation(), self.old_generation, "the pointer never moved while a command was queued")

    def test_the_mutation_window_holds_the_controller_transaction_lock(self) -> None:
        import herdr_validation as validation

        path = self.tmp / "plan.json"
        self.assertEqual(self.run_tool("update", "--payload", str(self.v2), "--plan-out", str(path))[0], 0)
        digest = json.loads(path.read_text())["approval_hash"]
        done = threading.Event()
        results: list[int] = []

        def apply() -> None:
            env = hi.Env(self.layout, host=self.host, lock_wait=30.0)
            approved = json.loads(path.read_text())["plan"]
            adapters = hsetup.Adapters.for_env(env, approved["options"])
            import herdr_update as hu

            result = hu.apply_operation(env, "update", approved, digest, rebuild=lambda: hsetup.build_plan(env, "update", approved["options"], Path(approved["payload_root"]), adapters),
                                        make_steps=lambda ctx, payload: hu.file_steps(ctx) + hsetup.component_steps(ctx, adapters))
            results.append(0 if result.status == "COMPLETE" else 1)
            done.set()

        with validation.StateStore(self.layout.paths, time.time).transaction():
            thread = threading.Thread(target=apply)
            thread.start()
            self.assertFalse(done.wait(2.5), "the asset/pointer window waits for the controller's transaction lock")
        thread.join(60)
        self.assertEqual(results, [0])


class DependencyBindingTests(FullCase):
    """CI4: the selected dependency lock and local acquisition artifacts are part of the approval."""

    def lock_copy(self, name: str = "lock.json", **changes: Any) -> Path:
        data = json.loads((Path(__file__).resolve().parents[1] / "config/quota-dependency.json").read_text())
        data.update(changes)
        path = self.tmp / name
        path.write_text(json.dumps(data))
        return path

    def plan(self, lock: Path | None = None, *extra: str) -> dict[str, Any]:
        argv = ["setup", "--profile", "full", "--payload", str(self.payload), "--components", "quota", "--quota-stage-dir", str(self.stage), *extra]
        if lock is not None:
            argv += ["--quota-lock", str(lock)]
        return self.plan_json(*argv, adapters=self.adapters(FakeQuota()))

    def test_a_different_pin_or_artifact_is_a_different_approval(self) -> None:
        first = self.plan(self.lock_copy())
        second = self.plan(self.lock_copy("other.json", revision="1" * 40))
        self.assertNotEqual(first["approval_hash"], second["approval_hash"])
        self.assertEqual(first["plan"]["dependency"]["revision"], json.loads((Path(__file__).resolve().parents[1] / "config/quota-dependency.json").read_text())["revision"])
        archive = self.tmp / "source.tar.gz"
        archive.write_bytes(b"artifact one")
        a = self.plan(self.lock_copy("c.json"), "--quota-archive", str(archive))
        archive.write_bytes(b"artifact two")
        b = self.plan(self.lock_copy("c.json"), "--quota-archive", str(archive))
        self.assertNotEqual(a["approval_hash"], b["approval_hash"])
        self.assertEqual(a["plan"]["dependency"]["artifacts"]["archive"], hashlib.sha256(b"artifact one").hexdigest())

    def test_without_an_explicit_lock_the_payloads_own_lock_is_the_dependency(self) -> None:
        payload_lock = self.payload / "config/quota-dependency.json"
        first = self.plan()
        self.assertEqual((first["plan"]["dependency"]["source"], first["plan"]["dependency"]["lock_sha256"]), ("payload", hashlib.sha256(payload_lock.read_bytes()).hexdigest()))
        data = json.loads(payload_lock.read_text())
        data["revision"] = "2" * 40
        payload_lock.write_text(json.dumps(data))
        second = self.plan()
        self.assertEqual(second["plan"]["dependency"]["revision"], "2" * 40, "the reviewed payload's lock was used, not the running tool's own copy")
        self.assertNotEqual(first["approval_hash"], second["approval_hash"])

    def test_an_invalid_lock_is_a_blocker_not_a_silent_fallback(self) -> None:
        bad = self.tmp / "bad.json"
        bad.write_text(json.dumps({"schema_version": 1}))
        document = self.plan(bad)
        self.assertTrue(any("quota dependency lock" in item for item in document["blockers"]), document["blockers"])

    def test_changing_the_lock_after_an_interrupted_stage_cannot_continue(self) -> None:
        lock = self.lock_copy()
        quota = FakeQuota()
        adapters = self.adapters(quota)
        path = self.tmp / "dep-plan.json"
        argv = ["setup", "--profile", "full", "--payload", str(self.payload), "--components", "quota", "--quota-stage-dir", str(self.stage), "--quota-lock", str(lock)]
        self.assertEqual(self.run_tool(*argv, "--plan-out", str(path), adapters=adapters)[0], 0)
        digest = json.loads(path.read_text())["approval_hash"]

        def fault(name: str) -> None:
            if name == "after:quota_stage":
                raise hi.Crash(name)

        with self.assertRaises(hi.Crash):
            self.run_tool("setup", "--apply", "--plan", str(path), "--approve", digest, adapters=adapters, env=hi.Env(self.layout, host=self.host, lock_wait=1.0, fault=fault))
        lock.write_text(json.dumps({**json.loads(lock.read_text()), "revision": "3" * 40}))
        code, out, err = self.run_tool("setup", "--apply", "--plan", str(path), "--approve", digest, adapters=self.adapters(quota))
        self.assertEqual(code, hi.EXIT_REFUSED, out + err)
        self.assertIn("dependency changed since the approval", out + err)
        self.assertEqual((quota.count("link"), quota.count("enable")), (0, 0))


class ReconcilerSoundnessTests(FullCase):
    """CI5: an external step is done only when its complete postcondition is proven; conflicting or partial state is never repeated over."""

    def saved(self, *extra: str) -> tuple[Path, str]:
        path = self.tmp / f"full-plan-{len(list(self.tmp.glob('full-plan-*.json')))}.json"
        code, out, err = self.full(*self.selected(), *extra, "--plan-out", str(path))
        self.assertEqual(code, 0, out + err)
        return path, json.loads(path.read_text())["approval_hash"]

    def crash(self, path: Path, digest: str, point: str, adapters: hsetup.Adapters) -> None:
        def fault(name: str) -> None:
            if name == point:
                raise hi.Crash(point)

        with self.assertRaises(hi.Crash):
            self.run_tool("setup", "--apply", "--plan", str(path), "--approve", digest, env=hi.Env(self.layout, host=self.host, lock_wait=1.0, fault=fault), adapters=adapters)

    def resume(self, path: Path, digest: str, adapters: hsetup.Adapters) -> tuple[int, str, str]:
        return self.run_tool("setup", "--apply", "--plan", str(path), "--approve", digest, adapters=adapters)

    def test_a_conflicting_plugin_after_an_interrupted_link_is_never_enabled(self) -> None:
        quota = FakeQuota()
        adapters = self.adapters(quota)
        path, digest = self.saved()
        self.crash(path, digest, "after:quota_link", adapters)
        quota.state = {"state": "conflict", "detail": "different plugin revision", "enabled": False}
        code, out, err = self.resume(path, digest, adapters)
        self.assertEqual(code, hi.EXIT_RECONCILING, out + err)
        self.assertEqual(quota.count("enable"), 0, "enable was never issued against a conflicting plugin")

    def test_a_plugin_from_another_source_with_the_right_pin_is_not_the_approved_one(self) -> None:
        quota = FakeQuota()
        adapters = self.adapters(quota)
        path, digest = self.saved()
        self.crash(path, digest, "after:quota_link", adapters)
        quota.root = str(self.tmp / "somebody-elses-checkout")
        code, out, err = self.resume(path, digest, adapters)
        self.assertEqual(code, hi.EXIT_RECONCILING, out + err)
        self.assertEqual(quota.count("enable"), 0)

    def test_a_stage_that_does_not_verify_is_never_linked(self) -> None:
        quota = FakeQuota()
        adapters = self.adapters(quota)
        path, digest = self.saved()
        self.crash(path, digest, "after:quota_stage", adapters)
        quota.stage_tamper = True
        code, out, err = self.resume(path, digest, adapters)
        self.assertEqual(code, hi.EXIT_RECONCILING, out + err)
        self.assertEqual((quota.count("link"), quota.count("enable"), quota.count("bootstrap")), (0, 0, 1), "no link, no enable, and no second staging attempt")

    def test_an_exact_completed_link_is_still_recognised_without_repeating_it(self) -> None:
        quota = FakeQuota()
        adapters = self.adapters(quota)
        path, digest = self.saved()
        self.crash(path, digest, "after:quota_link", adapters)
        code, out, err = self.resume(path, digest, adapters)
        self.assertIn(code, (0, hi.EXIT_ACTION), out + err)
        self.assertEqual((quota.count("bootstrap"), quota.count("link"), quota.count("enable")), (1, 1, 1))

    def test_a_partially_written_telegram_setup_is_never_overwritten_on_recovery(self) -> None:
        quota = FakeQuota()
        adapters = self.adapters(quota)
        path, digest = self.saved()
        original = htg.hs.atomic_write_json

        def crash_before_config(target: Any, *args: Any, **kwargs: Any) -> None:
            if Path(target) == self.layout.tg_config:
                raise hi.Crash("token replaced; configuration not committed")
            original(target, *args, **kwargs)

        with mock.patch.object(htg.hs, "atomic_write_json", side_effect=crash_before_config), self.assertRaises(hi.Crash):
            self.run_tool("setup", "--apply", "--plan", str(path), "--approve", digest, adapters=adapters)
        stored = self.layout.tg_config.parent / "bot-token"
        before = stored.read_bytes()
        self.assertEqual(json.loads(self.layout.tg_config.read_text())["mode"], "unconfigured")
        self.token_file.write_text("123456789:AAAnotherTokenThatMustNeverReplaceTheStoredOne_1234\n")
        code, out, err = self.resume(path, digest, adapters)
        self.assertEqual(code, hi.EXIT_REFUSED if "stale" in out + err else hi.EXIT_RECONCILING, out + err)
        self.assertEqual(stored.read_bytes(), before, "the existing credential was not overwritten")
        self.token_file.write_text(FAKE_TOKEN + "\n")
        code, out, err = self.resume(path, digest, adapters)
        self.assertEqual(code, hi.EXIT_RECONCILING, "even with the original source restored, a partial state is left for a person: " + out + err)
        self.assertEqual(stored.read_bytes(), before)
        self.assertNotIn(FAKE_TOKEN, out + err)

    def test_a_token_source_changed_after_approval_is_stale(self) -> None:
        path, digest = self.saved()
        self.token_file.write_text("123456789:AAAnotherTokenChangedAfterApproval_abcdefghijk\n")
        before = snapshot(self.home)
        code, out, err = self.resume(path, digest, self.adapters(FakeQuota()))
        self.assertEqual(code, hi.EXIT_REFUSED, out + err)
        self.assertIn("stale", out + err)
        self.assertEqual(snapshot(self.home), before)

    def test_a_telegram_configuration_for_another_owner_is_not_recorded_as_done(self) -> None:
        quota = FakeQuota()
        adapters = self.adapters(quota)
        path, digest = self.saved()
        self.crash(path, digest, "after:telegram_setup", adapters)
        configured = json.loads(self.layout.tg_config.read_text())
        configured["owner_user_id"] = 999
        self.layout.tg_config.write_text(json.dumps(configured))
        code, out, err = self.resume(path, digest, adapters)
        self.assertEqual(code, hi.EXIT_RECONCILING, out + err)
        self.assertEqual(json.loads(self.layout.tg_config.read_text())["owner_user_id"], 999, "a configuration that differs from the approved outcome is left alone")


class ReadinessIdentityTests(FullCase):
    """CI6: READY requires exact live identities, a reachable server, running watcher/bridge; persistence counts durable enablement only."""

    CODEX = "11111111-1111-4111-8111-111111111111"
    CLAUDE = "22222222-2222-4222-8222-222222222222"

    def agent(self, provider: str, pane: str, session: str, name: str | None = None) -> dict[str, Any]:
        return {"name": name or f"{provider}-main", "agent": provider, "pane_id": pane, "agent_session": {"value": session}}

    def ready(self) -> None:
        self.host.agents = [self.agent("codex", "w1:p1", self.CODEX), self.agent("claude", "w1:p2", self.CLAUDE)]
        self.quota.state = {"state": "pinned", "detail": "ok", "enabled": True, "action": "none"}
        self.quota.fresh = True
        self.write_owners({"codex": {"pane_id": "w1:p1", "session_id": self.CODEX}, "claude": {"pane_id": "w1:p2", "session_id": self.CLAUDE}})
        self.full(*self.selected(), "--project-root", str(self.project), "--services", "worker,telegram", apply=True)
        for unit in ("herdr-supervisor-worker.path", "herdr-telegram.service", "herdr-server.service"):
            self.host.enabled_units[unit] = "enabled"
        self.host.linger = "yes"

    def write_owners(self, value: Any) -> None:
        self.layout.state.mkdir(parents=True, exist_ok=True)
        self.layout.paths.owners_file.write_text(json.dumps(value))

    def status(self) -> dict[str, Any]:
        return json.loads(self.run_tool("--json", "status", adapters=self.adapters(self.quota))[1])  # type: ignore[no-any-return]

    def sessions_item(self, report: dict[str, Any]) -> dict[str, str]:
        return next(item for item in report["items"] if item["id"] == "sessions")

    def test_a_valid_positive_control_is_ready(self) -> None:
        self.ready()
        report = self.status()
        self.assertEqual((report["overall"], self.sessions_item(report)["status"]), ("READY", "OK"))

    def test_every_session_ownership_failure_is_reported_and_blocks_ready(self) -> None:
        self.ready()
        cases = {
            "empty object": ({}, None, "malformed"),
            "missing provider": ({"codex": {"pane_id": "w1:p1", "session_id": self.CODEX}}, None, "malformed"),
            "non-canonical session": ({"codex": {"pane_id": "w1:p1", "session_id": "not-a-uuid"}, "claude": {"pane_id": "w1:p2", "session_id": self.CLAUDE}}, None, "malformed"),
            "boolean pane": ({"codex": {"pane_id": True, "session_id": self.CODEX}, "claude": {"pane_id": "w1:p2", "session_id": self.CLAUDE}}, None, "malformed"),
            "extra key": ({"codex": {"pane_id": "w1:p1", "session_id": self.CODEX, "x": 1}, "claude": {"pane_id": "w1:p2", "session_id": self.CLAUDE}}, None, "malformed"),
            "stale session": (None, [self.agent("codex", "w1:p1", "99999999-9999-4999-8999-999999999999"), self.agent("claude", "w1:p2", self.CLAUDE)], "not found among the live agents"),
            "duplicate identity": (None, [self.agent("codex", "w1:p1", self.CODEX), self.agent("codex", "w2:p1", self.CODEX, "codex-copy"), self.agent("claude", "w1:p2", self.CLAUDE)], "duplicate identity"),
            "another provider carries it": (None, [self.agent("claude", "w1:p1", self.CODEX), self.agent("claude", "w1:p2", self.CLAUDE)], "another provider"),
            "moved pane": (None, [self.agent("codex", "w9:p9", self.CODEX), self.agent("claude", "w1:p2", self.CLAUDE)], "live in pane w9:p9"),
            "alias only": (None, [{"name": "codex-main", "agent": "codex", "pane_id": "w1:p1"}, self.agent("claude", "w1:p2", self.CLAUDE)], "not found among the live agents"),
            "server unreachable": (None, "none", "no Herdr server answers"),
        }
        for label, (owners, agents, needle) in cases.items():
            with self.subTest(label):
                good = {"codex": {"pane_id": "w1:p1", "session_id": self.CODEX}, "claude": {"pane_id": "w1:p2", "session_id": self.CLAUDE}}
                self.write_owners(owners if owners is not None else good)
                self.host.agents = [self.agent("codex", "w1:p1", self.CODEX), self.agent("claude", "w1:p2", self.CLAUDE)] if agents is None else (None if agents == "none" else agents)
                kept = self.layout.paths.owners_file.read_bytes()
                report = self.status()
                item = self.sessions_item(report)
                self.assertEqual(item["status"], "ACTION_REQUIRED", label)
                self.assertIn(needle, item["detail"], label)
                self.assertNotIn(report["overall"], ("READY", "READY_NOW"), label)
                self.assertEqual(self.layout.paths.owners_file.read_bytes(), kept, "records are never repaired or rewritten by status")
        self.assertFalse([c for c in self.host.calls if Path(c[0]).name == "herdr" and c[1:2] not in (["--version"], ["agent"], ["plugin"])], "no session repair or creation command")

    def test_absent_owners_are_reported_as_not_enrolled(self) -> None:
        self.ready()
        self.layout.paths.owners_file.unlink()
        item = self.sessions_item(self.status())
        self.assertEqual((item["status"], "no session pair is enrolled yet" in item["detail"]), ("ACTION_REQUIRED", True))

    def test_a_missing_server_is_a_current_failure_even_with_everything_else_in_place(self) -> None:
        self.ready()
        self.host.agents = None
        report = self.status()
        server = next(item for item in report["items"] if item["id"] == "herdr-server")
        self.assertEqual(server["status"], "ACTION_REQUIRED")
        self.assertNotIn(report["overall"], ("READY", "READY_NOW"))

    def test_only_durable_enablement_counts_as_persistence(self) -> None:
        self.ready()
        for state, expected in (("enabled-runtime", False), ("disabled", False), ("static", False), ("linked", False), ("linked-runtime", False), ("alias", False), ("enabled", True)):
            with self.subTest(state):
                self.host.enabled_units["herdr-telegram.service"] = state
                self.assertEqual(self.status()["boot"]["enabled_units"], expected)
        self.host.enabled_units["herdr-telegram.service"] = "enabled"
        self.host.enabled_units.pop("herdr-server.service")
        report = self.status()
        self.assertEqual((report["overall"], report["boot"]["headless_server_enabled"]), ("READY_NOW", False), "a manually opened interactive server is usable now and honestly not reboot-persistent")


class InstalledToolPayloadTests(SetupCase):
    """CI8: the installed tool never guesses a payload; it says exactly what to pass and the documented commands work with it."""

    def run_installed(self, *argv: str, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
        env = {**os.environ, "HOME": str(self.home)}
        return subprocess.run([str(self.layout.bin / "herdr-setup"), *argv], env=env, input=stdin, capture_output=True, text=True, timeout=120)

    def test_the_installed_launcher_names_the_recorded_source_and_requires_payload(self) -> None:
        self.install()
        self.assertEqual(self.receipt()["payload_root"], str(self.payload))
        for argv in (("setup", "--profile", "full"), ("update",)):
            with self.subTest(argv):
                done = self.run_installed(*argv, stdin="")
                self.assertEqual(done.returncode, hi.EXIT_ACTION, done.stdout + done.stderr)
                self.assertIn("--payload", done.stderr)
                self.assertIn(str(self.payload), done.stderr, "the recorded source is shown but not used automatically")
                self.assertNotIn("Profile:", done.stdout, "no question was asked before the payload was known")

    def test_the_documented_installed_commands_work_with_an_explicit_payload(self) -> None:
        self.install()
        before = snapshot(self.home)
        full = self.run_installed("setup", "--profile", "full", "--payload", str(self.payload))
        self.assertEqual(full.returncode, 0, full.stdout + full.stderr)
        self.assertIn("Approval hash:", full.stdout)
        update = self.run_installed("update", "--payload", str(self.payload))
        self.assertEqual(update.returncode, 0, update.stdout + update.stderr)
        self.assertIn("Nothing to do", update.stdout)
        self.assertEqual(snapshot(self.home), before, "previews through the installed launcher write nothing")

    def test_interactive_mode_checks_the_payload_before_asking_anything(self) -> None:
        self.install()
        asked: list[str] = []
        with mock.patch("herdr_setup.default_payload_root", return_value=None):
            code, out, err = self.run_tool("setup", "--interactive", ask=lambda prompt: asked.append(prompt) or "")
        self.assertEqual(code, hi.EXIT_ACTION, out + err)
        self.assertEqual(asked, [])

    def test_install_script_output_shows_the_payload_flag(self) -> None:
        result = subprocess.run([str(Path(__file__).resolve().parents[1] / "install.sh")], cwd=Path(__file__).resolve().parents[1], env={**os.environ, "HOME": str(self.home)}, capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("No service was loaded, enabled, started, or restarted.", result.stdout)
        self.assertIn(f'herdr-setup setup --profile full --payload "{Path(__file__).resolve().parents[1]}"', result.stdout)


if __name__ == "__main__":
    unittest.main()
