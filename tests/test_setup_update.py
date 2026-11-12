"""Managed update, legacy adoption and rollback: preview-first, quiet-point refusals, immutable generations, an atomic
pointer that launchers resolve once, serialized and crash-safe application, service handling, and recovery. Every
refusal is asserted against a whole-tree snapshot and the recorded external commands."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from typing import Any

import herdr_core as core
import herdr_installation as hi
import herdr_setup as hsetup
import herdr_update as hu
from setup_fixtures import CURRENT_VERSION, SetupCase, copy_payload, snapshot, touch_task_state

OLD_LAUNCHER = '#!/bin/sh\n# legacy launcher\nexec python3 "${{HERDR_SUPERVISOR_LIB:-$HOME/.local/lib/herdr-supervisor}}/{module}" "$@"\n'
LAUNCHER_MODULES = {"herdr-supervisor": "herdr_supervisor.py", "herdr-telegram": "herdr_telegram.py", "herdr-query-worker": "herdr_query.py", "herdr-backup": "herdr_backup.py",
                    "herdr-codex-reset-worker": "herdr_codex_reset.py", "herdr-quota-dependency": "herdr_quota_dependency.py"}


def edit_unit(root: Path) -> None:
    unit = root / "systemd/herdr-telegram.service"
    unit.write_text(unit.read_text().replace("RestartSec=15", "RestartSec=20"))


def edit_docs_and_unit(root: Path) -> None:
    edit_unit(root)
    (root / "docs/INSTALL.md").write_text((root / "docs/INSTALL.md").read_text() + "\nrelease-two-marker\n")


class UpdateCase(SetupCase):
    def setUp(self) -> None:
        super().setUp()
        self.install()
        self.old_generation = self.active_generation()
        self.v2 = self.candidate("payload-2", "0.4.1", edit_docs_and_unit)

    def preview(self, payload: Path, *extra: str) -> dict[str, Any]:
        return self.plan_json("update", "--payload", str(payload), *extra)

    def assert_refused(self, payload: Path, *needles: str, extra: tuple[str, ...] = ()) -> None:
        before = snapshot(self.home)
        self.reset_calls()
        code, out, err = self.run_tool("update", "--payload", str(payload), "--apply", "--approve-current", *extra)
        self.assertEqual(code, hi.EXIT_REFUSED, out + err)
        for needle in needles:
            self.assertIn(needle, out + err)
        self.assertEqual(snapshot(self.home), before, "a refused update changes nothing")
        self.assertEqual(self.host.service_calls(), [], "a refused update performs no service action")


class UpdatePreviewTests(UpdateCase):
    def test_preview_is_read_only_and_names_the_change(self) -> None:
        before = snapshot(self.home)
        self.reset_calls()
        document = self.preview(self.v2)
        self.assertEqual(snapshot(self.home), before)
        self.assertEqual(self.host.service_calls(), [])
        self.assertEqual([call[2] for call in self.host.commands("systemctl")][:1], ["show-environment"])
        self.assertTrue(all(call[2] in ("show-environment", "is-active") for call in self.host.commands("systemctl")), "the preview only queries")
        self.assertEqual(document["plan"]["candidate"]["version"], "0.4.1")
        self.assertEqual(document["plan"]["installed"]["layout"], "managed")
        self.assertEqual(document["blockers"], [])
        titles = [action["title"] for action in document["plan"]["actions"]]
        self.assertIn("atomically activate the generation", titles)
        self.assertNotIn("create missing initial configuration files", titles, "an update never creates or edits configuration")
        again = self.preview(self.v2)
        self.assertEqual(again["approval_hash"], document["approval_hash"], "a stable no-work/quiet host yields the same plan")

    def test_the_same_release_is_a_stable_noop(self) -> None:
        document = self.preview(self.payload)
        self.assertTrue(document["plan"]["noop"])
        before = snapshot(self.home)
        code, out, _err = self.update(self.payload)
        self.assertEqual(code, 0)
        self.assertIn("NOOP", out)
        self.assertEqual(snapshot(self.home), before)
        self.assertEqual(self.host.service_calls(), [])

    def test_downgrade_is_refused_with_the_rollback_hint(self) -> None:
        older = self.candidate("payload-old", "0.2.0")
        self.assert_refused(older, "older", "rollback")


class UpdateApplyTests(UpdateCase):
    def test_update_publishes_a_new_generation_and_switches_one_pointer(self) -> None:
        code, out, err = self.update(self.v2)
        self.assertEqual(code, 0, out + err)
        new = hi.load_payload(self.v2).generation_id
        self.assertEqual(self.active_generation(), new)
        self.assertNotEqual(new, self.old_generation)
        old_manifest, problems = hi.verify_generation(self.layout.generations / str(self.old_generation))
        self.assertEqual(problems, [], "the previous generation is kept complete and verified for rollback")
        self.assertIsNotNone(old_manifest)
        receipt = self.receipt()
        self.assertEqual((receipt["kind"], receipt["generation_id"], receipt["previous"]["generation_id"]), ("update", new, self.old_generation))
        unit = self.layout.unit / "herdr-telegram.service"
        self.assertIn("RestartSec=20", unit.read_text())
        self.assertIn("release-two-marker", (self.layout.doc / "INSTALL.md").read_text())
        self.assertEqual([call[2] for call in self.host.service_calls()], ["daemon-reload"], "nothing is active, so the only service action is the one reload the changed unit file needs")
        launcher = subprocess.run([str(self.layout.bin / "herdr-supervisor"), "--help"], env={**os.environ, "HOME": str(self.home)}, capture_output=True, text=True, timeout=60)
        self.assertEqual(launcher.returncode, 0)
        self.assertEqual(hi.verify_generation(self.layout.generations / new)[1], [])

    def test_only_active_managed_units_are_stopped_and_restored_and_the_server_is_never_stopped(self) -> None:
        self.host.active.update({"herdr-telegram.service": "active", "herdr-supervisor-worker.path": "active", "herdr-server.service": "active"})
        self.host.enabled_units.update({"herdr-telegram.service": "enabled", "herdr-supervisor-worker.path": "enabled"})
        self.reset_calls()
        code, out, err = self.update(self.v2)
        self.assertEqual(code, 0, out + err)
        actions = [(call[2], call[3] if len(call) > 3 else None) for call in self.host.service_calls()]
        self.assertEqual(actions, [("stop", "herdr-supervisor-worker.path"), ("stop", "herdr-telegram.service"), ("daemon-reload", None),
                                   ("start", "herdr-telegram.service"), ("start", "herdr-supervisor-worker.path")],
                         "triggers stop first and start last; a changed unit needs one reload; nothing is enabled or disabled")
        self.assertEqual(self.host.active["herdr-server.service"], "active", "the headless server hosts native sessions and is never stopped")
        self.assertEqual({unit for unit, state in self.host.active.items() if state == "active"}, {"herdr-telegram.service", "herdr-supervisor-worker.path", "herdr-server.service"})
        self.assertEqual(self.host.enabled_units, {"herdr-telegram.service": "enabled", "herdr-supervisor-worker.path": "enabled"})

    def test_units_inactive_before_stay_inactive_after(self) -> None:
        self.host.active["herdr-query-worker.path"] = "failed"
        self.reset_calls()
        code, out, err = self.update(self.v2)
        self.assertEqual(code, 0, out + err)
        self.assertEqual([call for call in self.host.service_calls() if call[2] in ("start", "stop")], [])

    def test_a_failed_service_stop_stops_the_operation_before_files_change(self) -> None:
        self.host.active["herdr-telegram.service"] = "active"
        self.host.fail[("stop", "herdr-telegram.service")] = hi.CmdResult(1, "", "Access denied")
        code, out, err = self.update(self.v2)
        self.assertNotEqual(code, 0)
        self.assertEqual(self.active_generation(), self.old_generation, "the pointer was never switched")
        self.assertIn("RestartSec=15", (self.layout.unit / "herdr-telegram.service").read_text(), "no managed asset changed")
        self.assertIn("Access denied", out + err)
        self.assertEqual(self.host.active["herdr-telegram.service"], "active", "a unit that could not be stopped was not touched")
        journals = [json.loads(path.read_text()) for path in self.layout.ops.glob("*.json")]
        failed = [journal for journal in journals if journal["status"] == "FAILED"]
        self.assertEqual([step["id"] for step in failed[0]["steps"] if step["status"] == "FAILED"], ["quiesce"], "the journal names the step that failed")
        self.assertEqual([step["status"] for step in failed[0]["steps"] if step["id"] in ("assets", "switch", "receipt")], ["PENDING"] * 3)


class UpdateRefusalTests(UpdateCase):
    def test_every_non_terminal_task_state_blocks(self) -> None:
        for state in ("RUNNING", "WAIT_USER", "PAUSED", "ERROR"):
            with self.subTest(state):
                touch_task_state(self.layout, state)
                self.assert_refused(self.v2, "quiet point", state)
        for state in core.SUPERVISOR_STATES:
            expected = [] if state in core.TERMINAL_STATES else ["a supervised task is not finished (state %s)" % state]
            self.assertEqual([p for p in hi._state_problems({"supervisor_state": state}) if "not finished" in p], expected, f"the predicate covers {state}")
        for state in ("DONE", "CANCELLED"):
            touch_task_state(self.layout, state)
            self.assertEqual(self.preview(self.v2)["blockers"], [], f"a settled {state} task is a quiet point")

    def test_unresolved_work_inside_a_terminal_state_blocks(self) -> None:
        for label, fields, needle in (
            ("pending gate", {"pending_gate": {"gate_id": "g", "gate_type": "plan_approval", "status": "pending"}}, "decision"),
            ("continuation", {"continuation": {"x": 1}}, "continuation"),
            ("interrupted delivery", {"interrupted_delivery": {"turn_id": "t"}}, "interrupted"),
            ("uncertain delivery", {"delivery": {"turn_id": "t", "status": "uncertain", "kind": "initial", "prompt_sha256": "a" * 64}}, "uncertain"),
        ):
            with self.subTest(label):
                touch_task_state(self.layout, "DONE", **fields)
                self.assert_refused(self.v2, needle)

    def test_unresolved_records_the_reader_cannot_even_load_also_block(self) -> None:
        for label, fields in (("owner recovery", {"owner_recovery": {"x": 1}}), ("follow-up", {"agent_followup": {"state": "WAITING"}}),
                              ("missing result", {"missing_result": {"x": 1}}), ("runtime proposal", {"runtime_proposal": {"x": 1}})):
            with self.subTest(label):
                state = touch_task_state(self.layout, "DONE")
                state.update(fields)
                core.atomic_write_json(self.layout.paths.state_file, state)
                self.assert_refused(self.v2, "task state")
        problems = hi._state_problems({"supervisor_state": "DONE", "owner_recovery": {"x": 1}, "agent_followup": {"state": "WAITING"}, "missing_result": {"x": 1}, "runtime_proposal": {"x": 1}})
        self.assertEqual(len(problems), 4, "the predicate itself names each unresolved condition")

    def test_corrupt_or_unsupported_state_blocks_fail_closed(self) -> None:
        for content in ("{not json", json.dumps({"schema_version": 99, "supervisor_state": "DONE"}), "[]"):
            with self.subTest(content[:12]):
                self.layout.paths.state_file.parent.mkdir(parents=True, exist_ok=True)
                self.layout.paths.state_file.write_text(content)
                self.assert_refused(self.v2, "task state")

    def test_a_running_worker_blocks(self) -> None:
        with core.WorkerLock(self.layout.paths.lock_file):
            self.assert_refused(self.v2, "worker")

    def test_queued_work_blocks_in_every_queue(self) -> None:
        for relative in ("inbox/pending", "inbox/processing", "query/inbox", "codex-reset/pending", "codex-reset/processing", "pending-starts"):
            with self.subTest(relative):
                directory = self.layout.state / relative
                directory.mkdir(parents=True, exist_ok=True)
                item = directory / "request.json"
                item.write_text("{}")
                self.assert_refused(self.v2, "present")
                item.unlink()
        self.assertEqual(self.preview(self.v2)["blockers"], [])

    def test_an_unresolved_session_preparation_blocks(self) -> None:
        directory = self.layout.state / "session-preparations"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "p.json").write_text(json.dumps({"status": "BINDING"}))
        self.assert_refused(self.v2, "preparation")
        (directory / "p.json").write_text(json.dumps({"status": "TASK_STARTED"}))
        self.assertEqual(self.preview(self.v2)["blockers"], [])

    def test_unreachable_or_transitional_service_state_blocks(self) -> None:
        self.host.systemd_up = False
        self.assert_refused(self.v2, "systemd")
        self.host.systemd_up = True
        self.host.active["herdr-telegram.service"] = "activating"
        self.assert_refused(self.v2, "activating")

    def test_newer_state_than_the_candidate_supports_is_never_downgraded(self) -> None:
        touch_task_state(self.layout, "DONE")
        state = json.loads(self.layout.paths.state_file.read_text())
        state["schema_version"] = 3
        self.layout.paths.state_file.write_text(json.dumps(state))
        self.assert_refused(self.v2, "task state")
        self.assertEqual(hi.state_schema_on_disk(self.layout), 3)

    def test_locally_modified_managed_files_are_not_overwritten(self) -> None:
        unit = self.layout.unit / "herdr-telegram.service"
        unit.write_text(unit.read_text() + "# my local tweak\n")
        self.assert_refused(self.v2, "modified since it was installed")
        self.assertIn("my local tweak", unit.read_text())

    def test_symlink_in_place_of_a_managed_file_is_refused(self) -> None:
        doc = self.layout.doc / "INSTALL.md"
        doc.unlink()
        doc.symlink_to(self.tmp / "elsewhere")
        self.assert_refused(self.v2, "symlink")

    def test_unrecognized_or_inconsistent_installations_are_not_updated(self) -> None:
        receipt = self.layout.receipt
        saved = receipt.read_bytes()
        receipt.write_text("{broken")
        self.assert_refused(self.v2, "inconsistent")
        receipt.write_bytes(saved)
        manifest = self.layout.generations / str(self.old_generation) / "GENERATION.json"
        os.chmod(self.layout.generations / str(self.old_generation), 0o755)
        os.chmod(manifest, 0o644)
        manifest.write_text("{}")
        self.assert_refused(self.v2, "inconsistent")


class LegacyAdoptionTests(SetupCase):
    def make_legacy_tree(self, name: str = "legacy-source") -> Path:
        tree = copy_payload(self.tmp / name)
        for module in hi.MANAGER_MODULES:
            (tree / "src" / module).unlink()
        (tree / "bin/herdr-setup").unlink()
        (tree / "bin/herdr-native-identity").unlink()  # the earlier release shipped neither of these launchers
        for launcher, module in LAUNCHER_MODULES.items():
            (tree / "bin" / launcher).write_text(OLD_LAUNCHER.format(module=module))
        return tree

    def install_legacy(self, tree: Path) -> hi.Payload:
        """What the earlier installer left behind: flat program files, launchers, units, documents."""
        payload = hi.load_payload(tree, require_manager=False)
        self.layout.lib.mkdir(parents=True)
        for item in payload.of_role("program"):
            (self.layout.lib / item.name).write_bytes(payload.blobs[item.key])
        for asset in hi.assets_for(payload, self.layout):
            hi.write_file_atomic(asset.dest, payload.blobs[f"{asset.role}/{asset.name}"], asset.mode)
        for directory in (self.layout.sup_config.parent, self.layout.state):
            directory.mkdir(parents=True, exist_ok=True)
            directory.chmod(0o700)
        return payload

    def setUp(self) -> None:
        super().setUp()
        self.legacy_tree = self.make_legacy_tree()
        self.legacy = self.install_legacy(self.legacy_tree)

    def adopt(self, *extra: str, payload: Path | None = None) -> tuple[int, str, str]:
        return self.run_tool("update", "--payload", str(payload or self.payload), "--adopt-legacy-from", str(self.legacy_tree), "--apply", "--approve-current", *extra)

    def test_adopting_a_verified_legacy_install_keeps_a_complete_way_back(self) -> None:
        flat_before = {name: (self.layout.lib / name).read_bytes() for name in sorted(p.name for p in self.layout.lib.iterdir())}
        old_launcher = (self.layout.bin / "herdr-supervisor").read_bytes()
        code, out, err = self.adopt()
        self.assertEqual(code, 0, out + err)
        self.assertEqual(sorted(p.name for p in self.layout.lib.iterdir()), ["active", "generations", "herdr_native_identity.py"], "the flat program files were retired after the switch; only the documented hook path remains, as a link")
        self.assertTrue((self.layout.lib / "herdr_native_identity.py").is_symlink())
        receipt = self.receipt()
        self.assertEqual(receipt["previous"]["kind"], "legacy")
        backup = self.layout.backups / receipt["operation_id"]
        self.assertEqual({p.name: p.read_bytes() for p in (backup / "legacy-lib").iterdir()}, flat_before, "every legacy program file was backed up byte for byte first")
        self.assertNotEqual((self.layout.bin / "herdr-supervisor").read_bytes(), old_launcher)
        shown = subprocess.run([str(self.layout.bin / "herdr-quota-dependency"), "--json", "show-lock"], env={**os.environ, "HOME": str(self.home)}, capture_output=True, text=True, timeout=60)
        self.assertEqual(shown.returncode, 0, shown.stderr)
        rolled = self.run_tool("rollback", "--apply", "--approve-current")
        self.assertEqual(rolled[0], 0, rolled[1] + rolled[2])
        self.assertEqual({name: (self.layout.lib / name).read_bytes() for name in sorted(p.name for p in self.layout.lib.iterdir() if p.is_file())}, flat_before)
        self.assertFalse(self.layout.active.exists() or self.layout.active.is_symlink(), "the pointer is removed so the restored launchers use the flat layout again")
        self.assertEqual((self.layout.bin / "herdr-supervisor").read_bytes(), old_launcher)
        self.assertFalse((self.layout.bin / "herdr-setup").exists(), "an asset the legacy release never had is removed again")
        done = subprocess.run([str(self.layout.bin / "herdr-supervisor"), "--help"], env={**os.environ, "HOME": str(self.home)}, capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(hi.inspect_install(self.layout).layout, "legacy")

    def test_adoption_needs_the_explicit_flag_and_never_version_text(self) -> None:
        before = snapshot(self.home)
        code, out, err = self.run_tool("update", "--payload", str(self.payload), "--apply", "--approve-current")
        self.assertEqual(code, hi.EXIT_REFUSED)
        self.assertIn("--adopt-legacy-from", out + err)
        self.assertEqual(snapshot(self.home), before)

    def test_unknown_extra_or_changed_program_files_block_adoption(self) -> None:
        """The deployment this feature was written against has more modules than the reviewed checkout, and different bytes in shared ones."""
        mutations = {
            "private extra module": lambda: (self.layout.lib / "herdr_pairs.py").write_text("# private\n"),
            "six private modules": lambda: [(self.layout.lib / f"herdr_private{i}.py").write_text("# private\n") for i in range(6)],
            "changed shared module": lambda: (self.layout.lib / "herdr_core.py").write_text((self.layout.lib / "herdr_core.py").read_text() + "# private edit\n"),
            "missing module": lambda: (self.layout.lib / "herdr_cli.py").unlink(),
            "unreadable entry": lambda: (self.layout.lib / "nested").mkdir(),
        }
        for label, mutate in mutations.items():
            with self.subTest(label):
                snapshot_before = {path: path.read_bytes() for path in self.layout.lib.iterdir() if path.is_file()}
                mutate()
                before = snapshot(self.home)
                code, out, err = self.adopt()
                self.assertEqual(code, hi.EXIT_REFUSED, out + err)
                self.assertEqual(snapshot(self.home), before, "no file was written, replaced or imported")
                self.assertFalse(self.layout.generations.exists())
                for path in list(self.layout.lib.iterdir()):
                    if path not in snapshot_before:
                        path.unlink() if path.is_file() else path.rmdir()
                for path, data in snapshot_before.items():
                    path.write_bytes(data)
        code, out, err = self.run_tool("update", "--payload", str(self.payload), "--adopt-legacy-from", str(self.legacy_tree))
        self.assertEqual(code, 0, out + err)

    def test_a_changed_legacy_asset_or_the_wrong_reference_tree_blocks(self) -> None:
        unit = self.layout.unit / "herdr-backup.timer"
        unit.write_text(unit.read_text() + "# edited\n")
        code, out, err = self.adopt()
        self.assertEqual(code, hi.EXIT_REFUSED)
        self.assertIn("herdr-backup.timer", out + err)
        unit.write_text(unit.read_text().replace("# edited\n", ""))
        other = copy_payload(self.tmp / "unrelated-tree", mutate=lambda root: (root / "src/herdr_cli.py").write_text("# something else\n" + (root / "src/herdr_cli.py").read_text()))
        for module in hi.MANAGER_MODULES:
            (other / "src" / module).unlink()
        (other / "bin/herdr-setup").unlink()
        (other / "bin/herdr-native-identity").unlink()
        code, out, err = self.run_tool("update", "--payload", str(self.payload), "--adopt-legacy-from", str(other), "--apply", "--approve-current")
        self.assertEqual(code, hi.EXIT_REFUSED)
        self.assertIn("differs from the reviewed source", out + err)

    def test_a_candidate_older_than_the_legacy_install_is_refused(self) -> None:
        older = self.candidate("older", "0.2.0")
        code, out, err = self.adopt(payload=older)
        self.assertEqual(code, hi.EXIT_REFUSED)
        self.assertIn("older", out + err)

    def test_adopting_while_a_task_is_active_is_refused(self) -> None:
        touch_task_state(self.layout, "WAIT_USER")
        before = snapshot(self.home)
        code, out, err = self.adopt()
        self.assertEqual(code, hi.EXIT_REFUSED)
        self.assertIn("quiet point", out + err)
        self.assertEqual(snapshot(self.home), before)

    def test_status_never_calls_a_legacy_install_ready(self) -> None:
        code, out, _err = self.run_tool("--json", "status")
        report = json.loads(out)
        self.assertEqual(report["layout"], "legacy")
        self.assertNotEqual(report["overall"], "READY")


class RealLegacyLayoutTests(LegacyAdoptionTests):
    """The earlier PUBLIC release (2279f37) as it really was: 28 flat modules (none of the pair/child-guard/manager/quota-dependency ones), five
    launchers (no herdr-setup, herdr-native-identity or herdr-quota-dependency), and NO `config/quota-dependency.json`. The inherited
    fixture copies the new payload and so kept the lock and a launcher the old release never shipped, which hid both. Every inherited
    adoption/refusal test is re-run on the authentic layout, plus the cases below."""

    NEW_MODULES = ("herdr_codex_children.py", "herdr_pairs.py", "herdr_quota_dependency.py")
    OLD_LAUNCHERS = ("herdr-backup", "herdr-codex-reset-worker", "herdr-query-worker", "herdr-supervisor", "herdr-telegram")
    STUB = "import sys\nsys.exit(0 if '--help' in sys.argv else 2)\n"

    def make_legacy_tree(self, name: str = "legacy-source") -> Path:
        tree = copy_payload(self.tmp / name)
        for module in (*hi.MANAGER_MODULES, *self.NEW_MODULES):
            (tree / "src" / module).unlink()
        for launcher in sorted(path.name for path in (tree / "bin").iterdir()):
            (tree / "bin" / launcher).unlink()
        for launcher in self.OLD_LAUNCHERS:
            module = {"herdr-codex-reset-worker": "herdr_codex_reset.py", "herdr-query-worker": "herdr_query.py", "herdr-backup": "herdr_backup.py"}.get(launcher, launcher.replace("-", "_") + ".py")
            (tree / "bin" / launcher).write_text(OLD_LAUNCHER.format(module=module))
            (tree / "src" / module).write_text(self.STUB)  # the old modules' own code is not under test; each launcher must still start
        (tree / "config" / hi.QUOTA_LOCK).unlink()
        (tree / "docs" / "QUOTA_DEPENDENCY.md").unlink(missing_ok=True)
        (tree / "docs" / "RELEASE_NOTES.md").unlink(missing_ok=True)
        return tree

    def install_legacy(self, tree: Path) -> hi.Payload:
        payload = super().install_legacy(tree)
        for path in self.layout.lib.iterdir():
            path.chmod(0o644)  # the old installer used `install -m 0644`; the fixture's plain write would follow the test runner's umask
        return payload

    def test_the_fixture_is_the_authentic_old_inventory(self) -> None:
        names = {item.key for item in self.legacy.files}
        self.assertNotIn(f"program/{hi.QUOTA_LOCK}", names)
        self.assertEqual(sorted(item.name for item in self.legacy.of_role("launcher")), sorted(self.OLD_LAUNCHERS))
        self.assertFalse(any(name in names for name in (f"program/{m}" for m in (*hi.MANAGER_MODULES, *self.NEW_MODULES))))

    def test_a_pre_manager_source_without_the_lock_is_inspectable_but_a_managed_candidate_must_carry_it(self) -> None:
        self.assertEqual(hi.load_payload(self.legacy_tree, require_manager=False).version, self.legacy.version)
        with self.assertRaises(hi.InstallError):
            hi.load_payload(self.legacy_tree)  # the strict default still demands the manager and the lock
        candidate = copy_payload(self.tmp / "lockless-candidate")
        (candidate / "config" / hi.QUOTA_LOCK).unlink()
        with self.assertRaisesRegex(hi.InstallError, "no config/quota-dependency.json"):
            hi.load_payload(candidate)
        with self.assertRaisesRegex(hi.InstallError, "no config/quota-dependency.json"):
            hi.load_payload(candidate, require_manager=True)
        before = snapshot(self.home)
        code, out, err = self.adopt(payload=candidate)
        self.assertEqual(code, hi.EXIT_REFUSED, out + err)
        self.assertEqual(snapshot(self.home), before, "a candidate without the lock is refused with nothing written, even for a valid legacy source")

    def test_adoption_then_rollback_restores_every_legacy_asset_exactly(self) -> None:
        def inventory() -> dict[str, tuple]:
            result: dict[str, tuple] = {}
            for label, root in (("lib", self.layout.lib), ("bin", self.layout.bin), ("unit", self.layout.unit), ("doc", self.layout.doc)):
                result.update({f"{label}/{key}": value for key, value in snapshot(root).items() if not key.startswith(("generations", "active"))})
            return result

        before = inventory()
        self.assertTrue(before and {"lib/herdr_supervisor.py", "bin/herdr-supervisor"} <= set(before))
        code, out, err = self.adopt()
        self.assertEqual(code, 0, out + err)
        self.assertEqual(hi.inspect_install(self.layout).layout, "managed")
        self.assertTrue((self.layout.bin / "herdr-quota-dependency").exists() and (self.layout.bin / "herdr-setup").exists())
        rolled = self.run_tool("rollback", "--apply", "--approve-current")
        self.assertEqual(rolled[0], 0, rolled[1] + rolled[2])
        self.assertIn("Result: COMPLETE", rolled[1])
        self.assertEqual(inventory(), before, "flat modules, launchers, units and documents are byte, mode and link identical to the old install")
        for name in ("herdr-setup", "herdr-quota-dependency", "herdr-native-identity"):
            self.assertFalse((self.layout.bin / name).exists(), f"{name} did not exist in the old release")
        self.assertEqual(hi.inspect_install(self.layout).layout, "legacy")

    def test_the_smoke_check_of_a_restored_legacy_release_names_only_what_it_shipped(self) -> None:
        self.assertEqual(hu.LEGACY_SMOKE_LAUNCHERS, ("herdr-supervisor", "herdr-telegram"))
        self.assertTrue(set(hu.LEGACY_SMOKE_LAUNCHERS) <= set(self.OLD_LAUNCHERS))
        self.assertTrue(set(hu.LEGACY_SMOKE_LAUNCHERS) <= set(hu.SMOKE_LAUNCHERS))
        ctx = hu.Ctx(env=self.env, kind="rollback", plan={"candidate": {"kind": "legacy"}}, payload=None, operation_id="x", approval_hash="y")
        self.assertEqual(hu.smoke_check(ctx), [])
        (self.layout.bin / "herdr-telegram").unlink()
        self.assertEqual(hu.smoke_check(ctx), ["launcher herdr-telegram is missing"], "a launcher the old release DID ship is still required")


class RollbackTests(UpdateCase):
    def test_rollback_restores_the_previous_release_assets_and_services(self) -> None:
        self.host.active.update({"herdr-telegram.service": "active"})
        self.assertEqual(self.update(self.v2)[0], 0)
        self.assertEqual(self.host.active["herdr-telegram.service"], "active")
        self.reset_calls()
        before_unit = (self.layout.unit / "herdr-telegram.service").read_text()
        self.assertIn("RestartSec=20", before_unit)
        code, out, err = self.run_tool("rollback", "--apply", "--approve-current")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.active_generation(), self.old_generation)
        self.assertIn("RestartSec=15", (self.layout.unit / "herdr-telegram.service").read_text())
        self.assertNotIn("release-two-marker", (self.layout.doc / "INSTALL.md").read_text())
        receipt = self.receipt()
        self.assertEqual((receipt["kind"], receipt["generation_id"]), ("rollback", self.old_generation))
        self.assertEqual([call[2] for call in self.host.service_calls()], ["stop", "daemon-reload", "start"])
        self.assertEqual(self.host.active["herdr-telegram.service"], "active")
        self.assertEqual(hi.inspect_install(self.layout).layout, "managed")

    def test_a_rollback_can_itself_be_undone(self) -> None:
        self.assertEqual(self.update(self.v2)[0], 0)
        new = self.active_generation()
        self.assertEqual(self.run_tool("rollback", "--apply", "--approve-current")[0], 0)
        self.assertEqual(self.active_generation(), self.old_generation)
        self.assertEqual(self.run_tool("rollback", "--apply", "--approve-current")[0], 0)
        self.assertEqual(self.active_generation(), new)
        self.assertIn("RestartSec=20", (self.layout.unit / "herdr-telegram.service").read_text())

    def test_rollback_without_a_previous_release_is_refused(self) -> None:
        before = snapshot(self.home)
        code, out, err = self.run_tool("rollback", "--apply", "--approve-current")
        self.assertEqual(code, hi.EXIT_REFUSED)
        self.assertIn("no previous", out + err)
        self.assertEqual(snapshot(self.home), before)

    def test_rollback_needs_a_quiet_point_and_unmodified_files(self) -> None:
        self.assertEqual(self.update(self.v2)[0], 0)
        touch_task_state(self.layout, "WAIT_USER")
        before = snapshot(self.home)
        code, out, err = self.run_tool("rollback", "--apply", "--approve-current")
        self.assertEqual(code, hi.EXIT_REFUSED)
        self.assertIn("quiet point", out + err)
        self.assertEqual(snapshot(self.home), before)
        touch_task_state(self.layout, "DONE")
        unit = self.layout.unit / "herdr-telegram.service"
        unit.write_text(unit.read_text() + "# changed after the update\n")
        code, out, err = self.run_tool("rollback", "--apply", "--approve-current")
        self.assertEqual(code, hi.EXIT_REFUSED)
        self.assertIn("changed since the last operation", out + err)

    def test_rollback_refuses_when_the_previous_generation_or_backup_was_tampered_with(self) -> None:
        self.assertEqual(self.update(self.v2)[0], 0)
        previous = self.layout.generations / str(self.old_generation)
        os.chmod(previous, 0o755)
        target = previous / "herdr_cli.py"
        os.chmod(target, 0o644)
        target.write_text("# tampered\n")
        before = snapshot(self.home)
        code, out, err = self.run_tool("rollback", "--apply", "--approve-current")
        self.assertEqual(code, hi.EXIT_REFUSED)
        self.assertIn("does not verify", out + err)
        self.assertEqual(snapshot(self.home), before)

    def test_rollback_is_refused_when_state_is_newer_than_the_previous_release_supports(self) -> None:
        """The previous generation's own manifest (not a copy in the receipt) says which state schema it can read."""
        older = self.candidate("old-schema", CURRENT_VERSION, lambda root: (root / "src/herdr_core.py").write_text((root / "src/herdr_core.py").read_text().replace("STATE_SCHEMA_VERSION = 2", "STATE_SCHEMA_VERSION = 1")))
        self.assertEqual(self.update(older)[0], 0)
        self.assertEqual(hi.load_payload(older).state_schema_version, 1)
        self.assertEqual(self.update(self.payload)[0], 0, "moving to the schema-2 release is allowed")
        self.assertEqual(self.receipt()["previous"]["state_schema_version"], 1)
        touch_task_state(self.layout, "DONE")
        self.assertEqual(hi.state_schema_on_disk(self.layout), 2)
        before = snapshot(self.home)
        code, out, err = self.run_tool("rollback", "--apply", "--approve-current")
        self.assertEqual(code, hi.EXIT_REFUSED, out + err)
        self.assertIn("never rolled back to force old code to run", out + err)
        self.assertEqual(snapshot(self.home), before)
        self.layout.paths.state_file.unlink()
        self.assertEqual(self.run_tool("rollback", "--apply", "--approve-current")[0], 0, "with no state to protect the rollback proceeds")


class SmokeFailureTests(UpdateCase):
    def test_a_candidate_that_fails_its_smoke_check_is_reverted_completely(self) -> None:
        self.host.active.update({"herdr-telegram.service": "active"})
        broken = self.candidate("broken", "0.4.1", lambda root: (root / "src/herdr_supervisor.py").write_text("raise SystemExit('broken candidate')\n"))
        before_units = {path.name: path.read_bytes() for path in self.layout.unit.iterdir()}
        receipt_before = self.layout.receipt.read_bytes()
        self.reset_calls()
        code, out, err = self.update(broken)
        self.assertEqual(code, hi.EXIT_REFUSED, out + err)
        self.assertIn("FAILED_REVERTED", out)
        self.assertEqual(self.active_generation(), self.old_generation)
        self.assertEqual({path.name: path.read_bytes() for path in self.layout.unit.iterdir()}, before_units)
        self.assertEqual(self.layout.receipt.read_bytes(), receipt_before, "the previous receipt is restored")
        self.assertEqual(self.host.active["herdr-telegram.service"], "active", "the unit that was active before is running again")
        working = subprocess.run([str(self.layout.bin / "herdr-supervisor"), "--help"], env={**os.environ, "HOME": str(self.home)}, capture_output=True, text=True, timeout=60)
        self.assertEqual(working.returncode, 0)
        self.assertTrue(list(self.layout.ops.glob("*.reverted-*")), "the reverted attempt is archived, not left to block a retry")
        code, out, err = self.update(self.v2)
        self.assertEqual(code, 0, "a later, good update is not blocked by the earlier failure: " + out + err)


class FailureHandlingTests(UpdateCase):
    def test_a_failure_before_the_switch_restarts_the_stopped_units_and_resumes_cleanly(self) -> None:
        self.host.active["herdr-telegram.service"] = "active"
        path = self.tmp / "plan.json"
        self.assertEqual(self.run_tool("update", "--payload", str(self.v2), "--plan-out", str(path))[0], 0)
        digest = json.loads(path.read_text())["approval_hash"]
        blocker = self.layout.destination("doc", "ARCHITECTURE.md")
        original = blocker.read_bytes()
        blocker.write_text("changed after approval, between the backup and the asset step")
        # the plan's preimage no longer matches: the operation is refused up front and nothing is stopped
        code, out, err = self.run_tool("update", "--apply", "--plan", str(path), "--approve", digest)
        self.assertEqual(code, hi.EXIT_REFUSED, out + err)
        self.assertEqual(self.host.active["herdr-telegram.service"], "active")
        blocker.write_bytes(original)
        # now fail inside the operation, after the unit was stopped, by changing the asset between backup and apply
        calls = {"n": 0}
        real_apply = hu.step_assets

        def failing(ctx: hu.Ctx) -> str:
            calls["n"] += 1
            if calls["n"] == 1:
                raise hi.InstallError("simulated drift while replacing assets")
            return real_apply(ctx)

        hu.step_assets = failing  # type: ignore[assignment]
        try:
            code, out, err = self.run_tool("update", "--apply", "--plan", str(path), "--approve", digest)
        finally:
            hu.step_assets = real_apply  # type: ignore[assignment]
        self.assertEqual(code, hi.EXIT_REFUSED, out + err)
        self.assertIn("restarted herdr-telegram.service", out)
        self.assertEqual(self.host.active["herdr-telegram.service"], "active", "an operation that failed before the switch does not leave the bridge stopped")
        self.assertEqual(self.active_generation(), self.old_generation)
        journal = json.loads((self.layout.ops / f"{digest[:32]}.json").read_text())
        self.assertEqual({step["id"]: step["status"] for step in journal["steps"]}["quiesce"], "PENDING")
        self.reset_calls()
        code, out, err = self.run_tool("update", "--apply", "--plan", str(path), "--approve", digest)
        self.assertEqual(code, 0, out + err)
        actions = [call[2] for call in self.host.service_calls()]
        self.assertEqual(actions[0], "stop", "resuming stops the unit again before any file changes")
        self.assertEqual(self.host.active["herdr-telegram.service"], "active")


class IdentityPreservationTests(UpdateCase):
    """Existing pane/native identities and pending decisions are byte-identical after both a refusal and a success."""

    def test_owners_and_state_survive_refusal_and_success_and_no_agent_command_is_issued(self) -> None:
        owners = self.layout.paths.owners_file
        owners.write_text(json.dumps({"codex": {"pane_id": "w1:p1", "session_id": "11111111-1111-4111-8111-111111111111"}, "claude": {"pane_id": "w1:p2", "session_id": "22222222-2222-4222-8222-222222222222"}}))
        touch_task_state(self.layout, "DONE", pending_gate={"gate_id": "g", "gate_type": "plan_approval", "status": "pending"})
        kept = {path: path.read_bytes() for path in (owners, self.layout.paths.state_file)}
        self.assert_refused(self.v2, "decision")
        self.assertEqual({path: path.read_bytes() for path in kept}, kept, "a pending decision and the session identities are untouched by a refusal")
        touch_task_state(self.layout, "DONE")
        kept = {path: path.read_bytes() for path in (owners, self.layout.paths.state_file)}
        self.reset_calls()
        code, out, err = self.update(self.v2)
        self.assertEqual(code, 0, out + err)
        self.assertEqual({path: path.read_bytes() for path in kept}, kept, "a successful update leaves identities and state byte-identical")
        self.assertEqual([call for call in self.host.calls if Path(call[0]).name == "herdr"], [], "an update never talks to Herdr, so no pane or session can change")


class CrashRecoveryTests(UpdateCase):
    """The process is made to die (a BaseException the runner never catches) before, inside and after every step; the
    same approved plan is then applied again and must finish exactly as an uninterrupted run would."""

    def reference(self) -> dict[str, Any]:
        self.assertEqual(self.update(self.v2)[0], 0)
        receipt = self.receipt()
        return {"generation": self.active_generation(), "assets": receipt["assets"], "unit": (self.layout.unit / "herdr-telegram.service").read_bytes(), "doc": (self.layout.doc / "INSTALL.md").read_bytes()}

    def crash_at(self, point: str) -> None:
        def fault(name: str) -> None:
            if name == point:
                raise hi.Crash(point)

        crashing = hi.Env(self.layout, host=self.host, lock_wait=1.0, fault=fault)
        with self.assertRaises(hi.Crash):
            self.run_tool("update", "--payload", str(self.v2), "--apply", "--approve-current", env=crashing)

    def saved_plan(self) -> tuple[Path, str]:
        path = self.tmp / "update-plan.json"
        path.unlink(missing_ok=True)
        code, out, err = self.run_tool("update", "--payload", str(self.v2), "--plan-out", str(path))
        self.assertEqual(code, 0, out + err)
        return path, json.loads(path.read_text())["approval_hash"]

    def test_a_crash_at_any_step_boundary_is_recovered_by_reapplying_the_same_plan(self) -> None:
        expected_dir = self.tmp / "reference-home"
        del expected_dir
        steps = ["dirs", "generation", "backup", "assets", "switch", "receipt", "smoke"]
        steps = [step["id"] for step in self.preview(self.v2)["plan"]["actions"]]
        reference = None
        for step in steps:
            for phase in ("before", "after", "done"):
                with self.subTest(f"{phase}:{step}"):
                    self.setUp_fresh()
                    path, digest = self.saved_plan()
                    point = f"{phase}:{step}"
                    crashing = hi.Env(self.layout, host=self.host, lock_wait=1.0, fault=lambda name, point=point: (_ for _ in ()).throw(hi.Crash(point)) if name == point else None)
                    with self.assertRaises(hi.Crash):
                        self.run_tool("update", "--apply", "--plan", str(path), "--approve", digest, env=crashing)
                    code, out, err = self.run_tool("update", "--apply", "--plan", str(path), "--approve", digest)
                    self.assertEqual(code, 0, f"{point}: {out}{err}")
                    result = {"generation": self.active_generation(), "assets": self.receipt()["assets"], "unit": (self.layout.unit / "herdr-telegram.service").read_bytes(),
                              "doc": (self.layout.doc / "INSTALL.md").read_bytes()}
                    if reference is None:
                        reference = result
                    self.assertEqual(result, reference, f"{point}: recovery must reach the same final state")
                    self.assertEqual(hi.inspect_install(self.layout).layout, "managed")
                    journal = json.loads((self.layout.ops / f"{digest[:32]}.json").read_text())
                    self.assertEqual(journal["status"], "COMPLETE")
                    self.assertEqual(hi.verify_generation(self.layout.generations / str(self.active_generation()))[1], [])

    def setUp_fresh(self) -> None:
        """Reset the host to the freshly installed v1 state for the next crash case."""
        import shutil

        for path in self.layout.generations.rglob("*"):
            os.chmod(path, 0o755)
        os.chmod(self.layout.generations, 0o755)
        shutil.rmtree(self.layout.lib)
        shutil.rmtree(self.layout.manager)
        for directory in (self.layout.bin, self.layout.unit, self.layout.doc):
            for path in list(directory.rglob("*")):
                if path.name != "herdr" and path.is_file():
                    path.unlink()
        for config in (self.layout.sup_config, self.layout.backup_config, self.layout.tg_config):
            config.unlink(missing_ok=True)
        self.host.calls.clear()
        self.install()
        self.old_generation = self.active_generation()

    def test_a_crash_between_the_switch_and_the_receipt_is_reported_and_completed(self) -> None:
        path, digest = self.saved_plan()
        self.crash_at("done:switch")
        state = hi.inspect_install(self.layout)
        self.assertEqual(state.layout, "inconsistent")
        self.assertIn("receipt", "; ".join(state.problems))
        code, out, _err = self.run_tool("--json", "status")
        self.assertEqual(json.loads(out)["overall"], "FAILED")
        other = self.candidate("other", "0.4.2")
        self.assert_refused(other, "inconsistent")
        code, out, err = self.run_tool("update", "--apply", "--plan", str(path), "--approve", digest)
        self.assertEqual(code, 0, out + err)
        self.assertEqual(hi.inspect_install(self.layout).layout, "managed")

    def test_interrupted_publication_leaves_the_active_release_untouched_and_is_cleaned(self) -> None:
        path, digest = self.saved_plan()
        self.crash_at("generation:staged")
        self.assertTrue(list(self.layout.generations.glob(".stage-*")), "the crash left a complete but unpublished staging directory")
        self.assertEqual(self.active_generation(), self.old_generation)
        launcher = subprocess.run([str(self.layout.bin / "herdr-supervisor"), "--help"], env={**os.environ, "HOME": str(self.home)}, capture_output=True, text=True, timeout=60)
        self.assertEqual(launcher.returncode, 0, "launchers keep resolving the complete previous generation while publication is pending")
        code, out, err = self.run_tool("update", "--apply", "--plan", str(path), "--approve", digest)
        self.assertEqual(code, 0, out + err)
        self.assertEqual([p.name for p in self.layout.generations.iterdir() if p.name.startswith(".stage-")], [])
        self.assertEqual(hi.verify_generation(self.layout.generations / str(self.active_generation()))[1], [])

    def test_a_crash_in_the_middle_of_the_auxiliary_assets_is_resumed_and_still_rolls_back_cleanly(self) -> None:
        path, digest = self.saved_plan()
        original = hi.write_file_atomic
        calls = {"n": 0}

        def flaky(target: Path, data: bytes, mode: int) -> None:
            if str(target).startswith(str(self.layout.unit)) or str(target).startswith(str(self.layout.doc)):
                calls["n"] += 1
                if calls["n"] == 2:
                    raise hi.Crash("mid-assets")
            original(target, data, mode)

        import herdr_update

        herdr_update.write_file_atomic = flaky  # type: ignore[attr-defined]
        try:
            with self.assertRaises(hi.Crash):
                self.run_tool("update", "--apply", "--plan", str(path), "--approve", digest)
        finally:
            herdr_update.write_file_atomic = original  # type: ignore[attr-defined]
        code, out, err = self.run_tool("update", "--apply", "--plan", str(path), "--approve", digest)
        self.assertEqual(code, 0, out + err)
        self.assertIn("RestartSec=20", (self.layout.unit / "herdr-telegram.service").read_text())
        self.assertEqual(self.run_tool("rollback", "--apply", "--approve-current")[0], 0)
        self.assertIn("RestartSec=15", (self.layout.unit / "herdr-telegram.service").read_text(), "the backup was recorded before the first replacement and survived the crash")


class ConcurrencyTests(UpdateCase):
    def apply_saved(self, path: Path, digest: str) -> hu.ApplyResult:
        """Apply a saved plan through the same entry the CLI uses, returning the structured result (stdout capture is not thread-safe)."""
        env = hi.Env(self.layout, host=self.host, lock_wait=60.0)
        approved = json.loads(path.read_text())["plan"]
        options, kind, root = approved["options"], approved["kind"], Path(approved["payload_root"])
        adapters = hsetup.Adapters.for_env(env, options)
        return hu.apply_operation(env, kind, approved, digest, rebuild=lambda: hsetup.build_plan(env, kind, options, root, adapters),
                                  make_steps=lambda ctx, payload: hu.file_steps(ctx) + hsetup.component_steps(ctx, adapters))

    def test_simultaneous_applies_of_one_plan_run_once_and_converge(self) -> None:
        self.host.active["herdr-telegram.service"] = "active"
        path = self.tmp / "plan.json"
        self.assertEqual(self.run_tool("update", "--payload", str(self.v2), "--plan-out", str(path))[0], 0)
        digest = json.loads(path.read_text())["approval_hash"]
        results: list[hu.ApplyResult] = []
        threads = [threading.Thread(target=lambda: results.append(self.apply_saved(path, digest))) for _ in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(180)
        self.assertEqual(sorted(result.status for result in results), ["ALREADY_APPLIED", "ALREADY_APPLIED", "COMPLETE"])
        self.assertEqual(len(list(self.layout.ops.glob("*.json"))), 2, "one operation journal for the install and one for the update")
        self.assertEqual([call[2] for call in self.host.service_calls()].count("stop"), 1, "the single operation stopped the active unit once")
        self.assertEqual(self.host.active["herdr-telegram.service"], "active")
        self.assertEqual(hi.verify_generation(self.layout.generations / str(self.active_generation()))[1], [])

    def test_two_different_candidates_serialize_and_the_second_approval_goes_stale(self) -> None:
        other = self.candidate("payload-3", "0.4.2")
        first, second = self.tmp / "p1.json", self.tmp / "p2.json"
        self.assertEqual(self.run_tool("update", "--payload", str(self.v2), "--plan-out", str(first))[0], 0)
        self.assertEqual(self.run_tool("update", "--payload", str(other), "--plan-out", str(second))[0], 0)
        digests = [json.loads(p.read_text())["approval_hash"] for p in (first, second)]
        results: dict[int, str] = {}
        threads = [threading.Thread(target=lambda i=i, plan=plan: results.__setitem__(i, self.apply_saved(plan, digests[i]).status)) for i, plan in enumerate((first, second))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(180)
        self.assertEqual(sorted(results.values()), ["COMPLETE", "REFUSED"], "exactly one wins; the other approval is stale against the new installation")
        self.assertEqual(hi.inspect_install(self.layout).layout, "managed")


class LauncherConsistencyTests(SetupCase):
    def test_a_running_launcher_never_mixes_two_releases_across_a_generation_switch(self) -> None:
        probe = (
            "import os, sys, time\n"
            "if '--help' in sys.argv[1:]:\n    raise SystemExit(0)\n"
            "here = os.path.dirname(os.path.abspath(__file__))\n"
            "print(here, flush=True)\n"
            "while not os.path.exists(os.environ['BARRIER']):\n    time.sleep(0.05)\n"
            "import herdr_present\n"
            "print(os.path.dirname(os.path.abspath(herdr_present.__file__)), flush=True)\n"
        )
        first = self.candidate("probe-1", CURRENT_VERSION, lambda root: (root / "src/herdr_supervisor.py").write_text(probe))
        self.install(first)
        generation_one = self.active_generation()
        barrier = self.tmp / "barrier"
        process = subprocess.Popen([str(self.layout.bin / "herdr-supervisor")], env={**os.environ, "HOME": str(self.home), "BARRIER": str(barrier)}, stdout=subprocess.PIPE, text=True)
        assert process.stdout is not None
        started = process.stdout.readline().strip()
        second = self.candidate("probe-2", "0.4.1", lambda root: (root / "src/herdr_supervisor.py").write_text(probe))
        code, out, err = self.update(second)
        self.assertEqual(code, 0, out + err)
        self.assertNotEqual(self.active_generation(), generation_one)
        barrier.write_text("go")
        finished = process.stdout.readline().strip()
        process.wait(timeout=30)
        process.stdout.close()
        self.assertEqual(started, str(self.layout.generations / str(generation_one)))
        self.assertEqual(finished, started, "a process that started on one generation imports every module from that same generation")

    def test_launchers_refuse_a_pointer_that_is_not_a_managed_generation(self) -> None:
        self.install()
        for bad in ("../../etc", "/etc", "generations/../x", "generations/a/b", "other"):
            with self.subTest(bad):
                self.layout.active.unlink()
                os.symlink(bad, self.layout.active)
                done = subprocess.run([str(self.layout.bin / "herdr-supervisor"), "--help"], env={**os.environ, "HOME": str(self.home)}, capture_output=True, text=True, timeout=60)
                self.assertEqual(done.returncode, 70)
                self.assertIn("not a managed generation", done.stderr)

    def test_every_launcher_carries_the_identical_resolution_block(self) -> None:
        root = Path(__file__).resolve().parents[1] / "bin"
        blocks = set()
        for launcher in sorted(root.iterdir()):
            text = launcher.read_text()
            start, end = text.index("LIB="), text.index("exec python3")
            blocks.add(text[start:end])
            self.assertEqual(stat.S_IMODE(launcher.stat().st_mode), 0o755)
            self.assertEqual(text.count("readlink"), 1, "the pointer is read exactly once per invocation")
        self.assertEqual(len(blocks), 1)
        self.assertEqual(len(list(root.iterdir())), 8)


class CompatibilityLinkTests(SetupCase):
    """The documented Codex hook path predates generations; configured hooks must keep working across install, update, adoption and rollback."""

    HOOK = "herdr_native_identity.py"

    def probe_candidate(self, name: str, version: str) -> Path:
        return self.candidate(name, version, lambda root: (root / "src" / self.HOOK).write_text("import sys\nprint(sys.path[0])\n"))

    def run_hook(self) -> str:
        done = subprocess.run(["python3", str(self.layout.lib / self.HOOK)], env={**os.environ, "HOME": str(self.home)}, capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stdout.strip()

    def test_the_old_hook_path_resolves_the_active_generation_even_after_updates(self) -> None:
        first = self.probe_candidate("probe-a", CURRENT_VERSION)
        self.install(first)
        link = self.layout.lib / self.HOOK
        self.assertEqual(os.readlink(link), f"active/{self.HOOK}")
        self.assertEqual(self.run_hook(), str((self.layout.generations / str(self.active_generation())).resolve()), "the script directory is the concrete generation, resolved once")
        second = self.probe_candidate("probe-b", "0.4.1")
        self.assertEqual(self.update(second)[0], 0)
        self.assertEqual(self.run_hook(), str((self.layout.generations / str(self.active_generation())).resolve()))
        self.assertTrue(link.is_symlink())

    def test_the_new_launcher_runs_the_same_hook(self) -> None:
        self.install()
        done = subprocess.run([str(self.layout.bin / "herdr-native-identity")], env={**os.environ, "HOME": str(self.home)}, input="", capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)

    def test_a_foreign_file_at_the_hook_path_blocks_instead_of_being_replaced(self) -> None:
        self.layout.lib.mkdir(parents=True)
        (self.layout.lib / self.HOOK).write_text("# somebody's own file\n")
        before = snapshot(self.home)
        code, out, err = self.run_tool("setup", "--payload", str(self.payload), "--apply", "--approve-current")
        self.assertEqual(code, hi.EXIT_REFUSED, out + err)
        self.assertEqual(snapshot(self.home), before)

    def test_adoption_swaps_the_flat_hook_for_the_link_and_rollback_restores_the_flat_file(self) -> None:
        legacy = LegacyAdoptionTests("test_adopting_a_verified_legacy_install_keeps_a_complete_way_back")
        legacy.tmp, legacy.home, legacy.layout, legacy.host, legacy.env, legacy.payload = self.tmp, self.home, self.layout, self.host, self.env, self.payload
        tree = legacy.make_legacy_tree()
        legacy.install_legacy(tree)
        flat = (self.layout.lib / self.HOOK).read_bytes()
        code, out, err = self.run_tool("update", "--payload", str(self.payload), "--adopt-legacy-from", str(tree), "--apply", "--approve-current")
        self.assertEqual(code, 0, out + err)
        self.assertTrue((self.layout.lib / self.HOOK).is_symlink(), "the flat copy was retired and replaced by the link")
        self.assertEqual(self.run_tool("rollback", "--apply", "--approve-current")[0], 0)
        restored = self.layout.lib / self.HOOK
        self.assertFalse(restored.is_symlink())
        self.assertEqual(restored.read_bytes(), flat, "the legacy hook file is back, byte for byte")
        self.assertEqual(hi.inspect_install(self.layout).layout, "legacy")


class ToolStillWorksFromTheInstalledGenerationTests(SetupCase):
    def test_the_installed_tool_reports_status_and_refuses_to_update_without_a_payload_tree(self) -> None:
        self.install()
        env = {**os.environ, "HOME": str(self.home)}
        done = subprocess.run([str(self.layout.bin / "herdr-setup"), "update"], env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, hi.EXIT_ACTION, done.stdout + done.stderr)
        self.assertIn("payload", done.stderr)
        self.assertIn("wheel", done.stderr)
        self.assertTrue(sys.version_info >= (3, 11))
        time.sleep(0)


if __name__ == "__main__":
    unittest.main()
