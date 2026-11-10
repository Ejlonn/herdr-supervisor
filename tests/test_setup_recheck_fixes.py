"""Regressions for the scoped recheck of the installation manager: a completion record is authority only for the exact approved step
list, release ownership is proven by the receipt's content-addressed inventory (and a rollback binds the previous receipt it would
reinstate), the worker locks every worker uses for its own exclusion are held for the whole operation with the unit states
re-checked inside the mutation window, and a unit that is merely `linked` is not reboot persistence. Each test drives the real
planners/apply/readiness through the same fixtures as the nominal suites."""

from __future__ import annotations

import fcntl
import json
from pathlib import Path
from typing import Any

import herdr_codex_reset as hcr
import herdr_core as core
import herdr_installation as hi
import test_setup_review_fixes as earlier
from setup_fixtures import snapshot, touch_task_state
from test_setup_full import FullCase
from test_setup_review_fixes import InterruptedCase
from test_setup_update import UpdateCase


def plan_document(journal: dict[str, Any], where: Path) -> Path:
    where.write_text(json.dumps({"schema": 1, "plan": journal["plan"], "approval_hash": journal["approval_hash"]}))
    return where


class CompletionRecordTests(InterruptedCase):
    """CI1-R1: COMPLETE is accepted only when the record holds exactly the ordered step list the approved plan defines."""

    def journal_path(self, digest: str) -> Path:
        return self.layout.ops / f"{digest[:32]}.json"

    def test_a_truncated_completion_record_never_counts_as_the_operation_having_happened(self) -> None:
        path, digest = self.saved_plan()
        self.crash_at(path, digest, "after:generation")
        journal = self.journal_path(digest)
        data = json.loads(journal.read_text())
        data["status"] = "COMPLETE"
        data["steps"] = [{**data["steps"][0], "status": "DONE"}]  # only the first step, recorded done
        journal.write_text(json.dumps(data))
        before = snapshot(self.home)
        code, out, err = self.resume(path, digest)
        self.assertEqual(code, hi.EXIT_REFUSED, out + err)
        self.assertNotIn("ALREADY_APPLIED", out)
        self.assertEqual(self.active_generation(), self.old_generation, "the update never ran and the old release is still active")
        self.assertEqual(snapshot(self.home), before)

    def test_every_step_set_that_differs_from_the_approved_list_is_refused(self) -> None:
        path, digest = self.saved_plan()
        self.assertEqual(self.resume(path, digest)[0], 0)
        journal = self.journal_path(digest)
        pristine = journal.read_text()
        steps = json.loads(pristine)["steps"]
        self.assertGreater(len(steps), 4)
        variants: dict[str, list[dict[str, Any]]] = {
            "missing last step": steps[:-1], "missing first step": steps[1:], "missing middle step": steps[:2] + steps[3:],
            "reordered": [steps[1], steps[0], *steps[2:]], "extra step": [*steps, {"id": "bogus", "status": "DONE"}],
            "all-done subset": [{**step, "status": "DONE"} for step in steps[:3]], "renamed step": [{**steps[0], "id": "elsewhere"}, *steps[1:]],
        }
        for label, replaced in variants.items():
            with self.subTest(label):
                data = json.loads(pristine)
                data["steps"] = replaced
                journal.write_text(json.dumps(data))
                before = snapshot(self.home)
                code, out, err = self.resume(path, digest)
                self.assertEqual(code, hi.EXIT_REFUSED, f"{label}: {out}{err}")
                self.assertIn("not trusted", out + err)
                self.assertNotIn("ALREADY_APPLIED", out)
                self.assertEqual(snapshot(self.home), before)

    def test_a_genuinely_completed_operation_stays_idempotent_without_replaying_anything(self) -> None:
        path, digest = self.saved_plan()
        self.assertEqual(self.resume(path, digest)[0], 0)
        after_first = snapshot(self.home)
        self.reset_calls()
        code, out, err = self.resume(path, digest)
        self.assertEqual(code, 0, out + err)
        self.assertIn("ALREADY_APPLIED", out)
        self.assertEqual(snapshot(self.home), after_first)
        self.assertEqual(self.host.calls, [], "no side effect was repeated")
        self.assertEqual(self.host.service_calls(), [])


class FullCompletionTests(FullCase):
    """The full profile adds component steps, which are part of the approved list as well."""

    def test_a_completed_full_profile_record_is_bound_to_its_component_steps(self) -> None:
        code, out, err = self.full(*self.selected(), "--project-root", str(self.project), "--services", "worker,telegram", apply=True)
        self.assertIn(code, (0, hi.EXIT_ACTION), out + err)
        journals = sorted(self.layout.ops.glob("*.json"))
        self.assertEqual(len(journals), 1)
        pristine = journals[0].read_text()
        data = json.loads(pristine)
        self.assertEqual(data["status"], "COMPLETE")
        document = plan_document(data, self.tmp / "full-plan.json")
        steps = data["steps"]
        component = [index for index, step in enumerate(steps) if step["id"] in ("telegram_setup", "quota_stage", "quota_link", "quota_enable", "services")]
        self.assertTrue(component, "the full profile records component steps")
        for label, replaced in {"component step removed": steps[:component[0]] + steps[component[0] + 1:], "file steps only": steps[:component[0]]}.items():
            altered = json.loads(pristine)
            altered["steps"] = replaced
            journals[0].write_text(json.dumps(altered))
            before = snapshot(self.home)
            code, out, err = self.run_tool("setup", "--apply", "--plan", str(document), "--approve", data["approval_hash"], adapters=self.adapters(self.quota))
            self.assertEqual(code, hi.EXIT_REFUSED, f"{label}: {out}{err}")
            self.assertNotIn("ALREADY_APPLIED", out)
            self.assertEqual(snapshot(self.home), before)
        journals[0].write_text(pristine)
        code, out, err = self.run_tool("setup", "--apply", "--plan", str(document), "--approve", data["approval_hash"], adapters=self.adapters(self.quota))
        self.assertIn(code, (0, hi.EXIT_ACTION), "the recorded full operation is already applied (manual items may remain): " + out + err)
        self.assertIn("ALREADY_APPLIED", out)


class ReleaseOwnershipTests(UpdateCase):
    """CI2-R1: a canonical file name is not release ownership, and a rollback approval covers the previous receipt it consumes."""

    rollback_plan = earlier.MetadataTrustTests.rollback_plan
    backup_manifest = earlier.MetadataTrustTests.backup_manifest

    def unowned(self, name: str = "PERSONAL.md") -> Path:
        victim = self.layout.doc / name
        victim.write_text("a file the user made; no release ships it\n")
        return victim

    def entry_for(self, victim: Path) -> dict[str, Any]:
        data = victim.read_bytes()
        return {"role": "doc", "dest": str(victim), "sha256": hi.sha256_bytes(data), "mode": 0o644, "size": len(data)}

    def reseal(self, receipt: dict[str, Any]) -> None:
        """Make the forged inventory self-consistent: recompute the payload identity the receipt records."""
        manifest, problems = hi.verify_generation(self.layout.generations / str(receipt["generation_id"]))
        assert manifest is not None and not problems
        receipt["payload_id"] = hi.release_inventory_id(manifest, receipt["assets"])

    def test_a_canonical_looking_asset_the_release_never_shipped_cannot_be_deleted(self) -> None:
        victim = self.unowned()
        receipt = self.receipt()
        receipt["assets"].append(self.entry_for(victim))
        self.layout.receipt.write_text(json.dumps(receipt))
        document = self.preview(self.v2)
        self.assertTrue(any("asset inventory" in item for item in document["blockers"]), document["blockers"])
        before = snapshot(self.home)
        code, out, err = self.update(self.v2)
        self.assertEqual(code, hi.EXIT_REFUSED, out + err)
        self.assertTrue(victim.exists())
        self.assertEqual(snapshot(self.home), before)

    def test_a_forgery_that_also_recomputes_the_payload_identity_is_caught_by_the_approved_plan(self) -> None:
        victim = self.unowned()
        receipt = self.receipt()
        receipt["assets"].append(self.entry_for(victim))
        self.reseal(receipt)
        self.layout.receipt.write_text(json.dumps(receipt))
        state = hi.inspect_install(self.layout)
        self.assertEqual(state.layout, "inconsistent")
        self.assertIn("approved plan", " ".join(state.problems))
        self.assertEqual(self.update(self.v2)[0], hi.EXIT_REFUSED)
        self.assertTrue(victim.exists())

    def test_altered_omitted_or_unrecorded_inventory_is_inconsistent_and_the_genuine_receipt_is_managed(self) -> None:
        genuine = self.receipt()
        self.assertEqual(hi.inspect_install(self.layout).layout, "managed")
        variants: dict[str, dict[str, Any]] = {
            "hash altered": {**genuine, "assets": [{**genuine["assets"][0], "sha256": "0" * 64}, *genuine["assets"][1:]]},
            "size altered": {**genuine, "assets": [{**genuine["assets"][0], "size": genuine["assets"][0]["size"] + 1}, *genuine["assets"][1:]]},
            "mode altered": {**genuine, "assets": [{**genuine["assets"][0], "mode": 0o777}, *genuine["assets"][1:]]},
            "asset omitted": {**genuine, "assets": genuine["assets"][1:]},
            "payload identity removed": {key: value for key, value in genuine.items() if key != "payload_id"},
            "another payload identity": {**genuine, "payload_id": "1" * 64},
        }
        for label, value in variants.items():
            with self.subTest(label):
                self.layout.receipt.write_text(json.dumps(value))
                self.assertEqual(hi.inspect_install(self.layout).layout, "inconsistent", label)
                self.assert_refused(self.v2, "inconsistent")
        self.layout.receipt.write_text(json.dumps(genuine))
        self.assertEqual(hi.inspect_install(self.layout).layout, "managed")

    def test_assets_a_later_release_no_longer_ships_are_still_removed_when_owned(self) -> None:
        extra = self.candidate("payload-extra-doc", "0.3.1", lambda root: (root / "docs" / "EXTRA.md").write_text("shipped by one release only\n"))
        self.assertEqual(self.update(extra)[0], 0)
        self.assertTrue((self.layout.doc / "EXTRA.md").exists())
        self.assertEqual(hi.inspect_install(self.layout).layout, "managed")
        later = self.candidate("payload-later", "0.3.2")
        code, out, err = self.update(later)
        self.assertEqual(code, 0, out + err)
        self.assertFalse((self.layout.doc / "EXTRA.md").exists(), "a genuinely owned, unmodified asset of a removed release is retired")
        self.assertEqual(hi.inspect_install(self.layout).layout, "managed", "and the resulting inventory still proves its own release")

    def test_a_previous_receipt_injected_after_approval_invalidates_the_rollback(self) -> None:
        saved, digest = self.rollback_plan()
        path, data = self.backup_manifest()
        victim = self.unowned()
        data["previous_receipt"]["assets"].append(self.entry_for(victim))
        path.write_text(json.dumps(data))
        live_receipt = self.layout.receipt.read_bytes()
        before = snapshot(self.home)
        code, out, err = self.run_tool("rollback", "--apply", "--plan", str(saved), "--approve", digest)
        self.assertEqual(code, hi.EXIT_REFUSED, out + err)
        self.assertIn("stale", out + err)
        self.assertEqual(self.layout.receipt.read_bytes(), live_receipt, "no injected ownership reached the live receipt")
        self.assertEqual(snapshot(self.home), before, "nothing was restored")
        self.assertEqual(self.update(self.v2)[0], 0)
        self.assertTrue(victim.exists())

    def test_every_consumed_previous_receipt_field_is_part_of_the_approval(self) -> None:
        # the rollback receipt is `{**previous_receipt, ...}`, so a field that does not affect ownership still persists into the live record
        saved, digest = self.rollback_plan()
        path, data = self.backup_manifest()
        data["previous_receipt"]["source"] = {"kind": "tree", "note": "changed after the approval"}
        path.write_text(json.dumps(data))
        before = snapshot(self.home)
        code, out, err = self.run_tool("rollback", "--apply", "--plan", str(saved), "--approve", digest)
        self.assertEqual(code, hi.EXIT_REFUSED, out + err)
        self.assertIn("stale", out + err)
        self.assertEqual(snapshot(self.home), before)

    def test_a_previous_receipt_forged_before_the_preview_is_a_blocker_even_when_resealed(self) -> None:
        self.assertEqual(self.update(self.v2)[0], 0)
        path, data = self.backup_manifest()
        victim = self.unowned()
        forged = data["previous_receipt"]
        forged["assets"].append(self.entry_for(victim))
        path.write_text(json.dumps(data))
        document = self.plan_json("rollback")
        self.assertTrue(any("previous receipt" in item and "not trusted" in item for item in document["blockers"]), document["blockers"])
        self.assertEqual(self.run_tool("rollback", "--apply", "--approve-current")[0], hi.EXIT_REFUSED)
        self.assertTrue(victim.exists())
        self.assertEqual(self.active_generation(), hi.read_active(self.layout))

    def test_a_rollback_leaves_a_receipt_that_still_proves_the_restored_release(self) -> None:
        self.assertEqual(self.update(self.v2)[0], 0)
        self.assertEqual(self.run_tool("rollback", "--apply", "--approve-current")[0], 0)
        self.assertEqual(hi.inspect_install(self.layout).layout, "managed")
        self.assertEqual(self.active_generation(), self.old_generation)
        self.assertEqual(self.update(self.v2)[0], 0, "and an update from the restored release works")


class RollbackReceiptOwnershipTests(UpdateCase):
    """CI2-R2: every receipt kind is proven by the approved operation that wrote it; the kind is taken from that plan, not from the label."""

    unowned = ReleaseOwnershipTests.unowned
    entry_for = ReleaseOwnershipTests.entry_for
    reseal = ReleaseOwnershipTests.reseal

    def records(self) -> dict[str, bytes]:
        """Every journal and backup record, byte for byte."""
        return {str(path.relative_to(self.layout.manager)): path.read_bytes() for path in sorted(self.layout.manager.rglob("*")) if path.is_file()}

    def forge(self, **changes: Any) -> Path:
        victim = self.unowned()
        receipt = self.receipt()
        receipt.update(changes)
        receipt["assets"].append(self.entry_for(victim))
        self.reseal(receipt)
        self.layout.receipt.write_text(json.dumps(receipt))
        return victim

    def assert_unowned_file_is_safe(self, victim: Path, needle: str) -> None:
        records = self.records()
        state = hi.inspect_install(self.layout)
        self.assertEqual(state.layout, "inconsistent", state.problems)
        self.assertIn(needle, " ".join(state.problems))
        self.assertTrue(any("inconsistent" in item for item in self.preview(self.v2)["blockers"]))
        code, out, err = self.update(self.v2)
        self.assertEqual(code, hi.EXIT_REFUSED, out + err)
        self.assertTrue(victim.exists(), "the user's file was not deleted")
        self.assertEqual(self.records(), records, "no journal or backup was changed")

    def test_a_forged_rollback_receipt_cannot_grant_ownership(self) -> None:
        self.assertEqual(self.update(self.v2)[0], 0)
        self.assertEqual(self.run_tool("rollback", "--apply", "--approve-current")[0], 0)
        self.assertEqual(self.receipt()["kind"], "rollback")
        victim = self.forge()
        self.assert_unowned_file_is_safe(victim, "previous receipt its approved backup recorded")

    def test_relabelling_a_receipt_cannot_disable_its_check(self) -> None:
        for label in ("rollback", "update", "setup", "other"):
            with self.subTest(label):
                self.layout.receipt.write_text(json.dumps(self.receipt()))
                original = self.receipt()
                victim = self.unowned()
                forged = {**original, "kind": label}
                forged["assets"] = [*original["assets"], self.entry_for(victim)]
                self.reseal(forged)
                self.layout.receipt.write_text(json.dumps(forged))
                records = self.records()
                self.assertEqual(hi.inspect_install(self.layout).layout, "inconsistent")
                self.assertEqual(self.update(self.v2)[0], hi.EXIT_REFUSED)
                self.assertTrue(victim.exists())
                self.assertEqual(self.records(), records)
                victim.unlink()
                self.layout.receipt.write_text(json.dumps(original))
        self.assertEqual(hi.inspect_install(self.layout).layout, "managed")

    def test_a_relabelled_receipt_that_changes_nothing_else_is_still_refused(self) -> None:
        genuine = self.receipt()
        self.layout.receipt.write_text(json.dumps({**genuine, "kind": "rollback"}))
        state = hi.inspect_install(self.layout)
        self.assertEqual(state.layout, "inconsistent")
        self.assertIn("operation kind", " ".join(state.problems))

    def test_genuine_rollback_rollback_of_rollback_and_updates_stay_provable(self) -> None:
        self.assertEqual(hi.inspect_install(self.layout).layout, "managed")
        self.assertEqual(self.update(self.v2)[0], 0)
        self.assertEqual(self.run_tool("rollback", "--apply", "--approve-current")[0], 0)
        self.assertEqual((self.receipt()["kind"], hi.inspect_install(self.layout).layout), ("rollback", "managed"))
        self.assertEqual(self.active_generation(), self.old_generation)
        self.assertEqual(self.run_tool("rollback", "--apply", "--approve-current")[0], 0, "a rollback can itself be rolled back")
        self.assertEqual((self.receipt()["kind"], hi.inspect_install(self.layout).layout), ("rollback", "managed"))
        self.assertNotEqual(self.active_generation(), self.old_generation)
        self.assertEqual(self.run_tool("rollback", "--apply", "--approve-current")[0], 0)
        self.assertEqual(hi.inspect_install(self.layout).layout, "managed")
        later = self.candidate("payload-after-rollbacks", "0.3.2")
        self.assertEqual(self.update(later)[0], 0, "an update from a rolled-back release works")
        self.assertEqual(hi.inspect_install(self.layout).layout, "managed")

    def test_an_auxiliary_only_update_after_a_rollback_and_owned_asset_retirement_still_work(self) -> None:
        extra = self.candidate("payload-extra-doc-2", "0.3.1", lambda root: (root / "docs" / "EXTRA.md").write_text("shipped by one release only\n"))
        self.assertEqual(self.update(extra)[0], 0)
        self.assertEqual(self.run_tool("rollback", "--apply", "--approve-current")[0], 0)
        self.assertFalse((self.layout.doc / "EXTRA.md").exists(), "the rolled-back release's own asset is removed again")
        self.assertEqual(self.update(extra)[0], 0)
        self.assertTrue((self.layout.doc / "EXTRA.md").exists())
        later = self.candidate("payload-later-2", "0.3.2")
        self.assertEqual(self.update(later)[0], 0)
        self.assertFalse((self.layout.doc / "EXTRA.md").exists(), "a genuinely removed-release asset is still retired")
        self.assertEqual(hi.inspect_install(self.layout).layout, "managed")

    def test_the_evidence_a_rollback_receipt_depends_on_must_be_present_and_unchanged(self) -> None:
        self.assertEqual(self.update(self.v2)[0], 0)
        self.assertEqual(self.run_tool("rollback", "--apply", "--approve-current")[0], 0)
        receipt = self.receipt()
        journal = self.layout.ops / f"{receipt['operation_id']}.json"
        backup = self.layout.backups / receipt["rolled_back_operation"] / "manifest.json"
        for label, target in (("journal", journal), ("backup record", backup)):
            with self.subTest(f"{label} missing"):
                kept = target.read_bytes()
                target.unlink()
                self.assertEqual(hi.inspect_install(self.layout).layout, "inconsistent")
                target.write_bytes(kept)
                self.assertEqual(hi.inspect_install(self.layout).layout, "managed")
        with self.subTest("plan altered without its hash"):
            kept = journal.read_bytes()
            data = json.loads(kept)
            data["plan"]["backup_identity"] = "0" * 64
            journal.write_text(json.dumps(data))
            self.assertEqual(hi.inspect_install(self.layout).layout, "inconsistent")
            journal.write_bytes(kept)
        with self.subTest("backup record altered"):
            kept = backup.read_bytes()
            data = json.loads(kept)
            data["previous_receipt"]["assets"].append(self.entry_for(self.unowned()))
            backup.write_text(json.dumps(data))
            state = hi.inspect_install(self.layout)
            self.assertEqual(state.layout, "inconsistent")
            self.assertIn("changed after the rollback was approved", " ".join(state.problems))
            backup.write_bytes(kept)
        self.assertEqual(hi.inspect_install(self.layout).layout, "managed")

    def test_the_proof_chain_is_bounded_and_refuses_cycles(self) -> None:
        self.assertEqual(self.update(self.v2)[0], 0)
        self.assertEqual(self.run_tool("rollback", "--apply", "--approve-current")[0], 0)
        receipt = self.receipt()
        manifest, problems = hi.verify_generation(self.layout.generations / receipt["generation_id"])
        self.assertIsNotNone(manifest)
        assert manifest is not None and not problems
        self.assertIsNone(hi.receipt_release_problem(self.layout, receipt, manifest))
        self.assertIn("cyclic", hi.receipt_release_problem(self.layout, receipt, manifest, (receipt["operation_id"],)) or "")
        self.assertIn("too long", hi.receipt_release_problem(self.layout, receipt, manifest, tuple(f"{n:032x}" for n in range(hi.MAX_RECEIPT_CHAIN))) or "")


class LateWorkerTests(UpdateCase):
    """CI3-R1: every worker's own lock is held for the whole operation, running bounded work is never stopped, and units are re-checked in the window."""

    def planned(self) -> tuple[Path, str]:
        path = self.tmp / "plan.json"
        self.assertEqual(self.run_tool("update", "--payload", str(self.v2), "--plan-out", str(path))[0], 0)
        return path, json.loads(path.read_text())["approval_hash"]

    def with_fault(self, point: str, action: Any) -> hi.Env:
        def fault(name: str) -> None:
            if name == point:
                action()

        return hi.Env(self.layout, host=self.host, lock_wait=2.0, fault=fault)

    def test_a_worker_service_that_starts_after_approval_stops_the_mutation_and_is_never_stopped(self) -> None:
        path, digest = self.planned()
        unit_before = (self.layout.unit / "herdr-telegram.service").read_bytes()
        env = self.with_fault("before:assets", lambda: self.host.active.__setitem__("herdr-query-worker.service", "active"))
        code, out, err = self.run_tool("update", "--apply", "--plan", str(path), "--approve", digest, env=env)
        self.assertEqual(code, hi.EXIT_REFUSED, out + err)
        self.assertIn("herdr-query-worker.service became active outside this operation", out + err)
        self.assertEqual((self.layout.unit / "herdr-telegram.service").read_bytes(), unit_before, "no asset was replaced")
        self.assertEqual(self.active_generation(), self.old_generation, "the pointer never moved")
        self.assertEqual(self.host.active["herdr-query-worker.service"], "active", "the running job was left alone")
        self.assertFalse([call for call in self.host.service_calls() if call[2] == "stop" and call[-1] == "herdr-query-worker.service"], "the running job was never stopped")
        self.host.active["herdr-query-worker.service"] = "inactive"
        self.assertEqual(self.run_tool("update", "--apply", "--plan", str(path), "--approve", digest)[0], 0, "once the job has finished the same approval resumes")

    def test_a_worker_that_starts_before_the_pointer_switch_also_stops_it(self) -> None:
        path, digest = self.planned()
        env = self.with_fault("before:switch", lambda: self.host.active.__setitem__("herdr-codex-reset-worker.service", "active"))
        code, out, err = self.run_tool("update", "--apply", "--plan", str(path), "--approve", digest, env=env)
        self.assertEqual(code, hi.EXIT_REFUSED, out + err)
        self.assertEqual(self.active_generation(), self.old_generation)

    def test_a_running_query_or_reset_worker_that_holds_its_lock_blocks_preview_and_apply(self) -> None:
        for label, lock_path in hi.worker_lock_paths(self.layout)[1:]:
            with self.subTest(label):
                lock_path.parent.mkdir(parents=True, exist_ok=True)
                with lock_path.open("a+") as handle:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    document = self.preview(self.v2)
                    self.assertTrue(any(label in item and "worker lock" in item for item in document["blockers"]), document["blockers"])
                    self.assert_refused(self.v2, "worker lock")
                    self.assertEqual(self.active_generation(), self.old_generation)
                self.assertEqual(self.preview(self.v2)["blockers"], [])

    def test_a_worker_that_holds_its_lock_after_planning_refuses_before_any_change(self) -> None:
        path, digest = self.planned()
        query_lock = hi.worker_lock_paths(self.layout)[1][1]
        query_lock.parent.mkdir(parents=True, exist_ok=True)
        with query_lock.open("a+") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            before = snapshot(self.home)
            code, out, err = self.run_tool("update", "--apply", "--plan", str(path), "--approve", digest)
            self.assertEqual(code, hi.EXIT_REFUSED, out + err)
            self.assertIn("read-only query worker", out + err)
            self.assertEqual(snapshot(self.home), before)
            self.assertEqual(self.host.service_calls(), [])

    def test_a_real_worker_start_during_the_operation_finds_its_lock_held(self) -> None:
        path, digest = self.planned()
        seen: dict[str, Any] = {}

        def compete() -> None:
            reset_dir = self.layout.paths.codex_reset_dir
            seen["reset_exit"] = hcr.main(["--once", "--state-dir", str(reset_dir)])  # the shipped worker entry point, not a stand-in
            handle = self.layout.paths.query_lock_file.open("a+")
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)  # the primitive QueryWorker.process_once uses
                seen["query_start"] = "acquired"
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except (BlockingIOError, PermissionError):
                seen["query_start"] = "blocked"
            finally:
                handle.close()
            try:
                core.WorkerLock(self.layout.paths.lock_file).acquire()
                seen["supervisor"] = "acquired"
            except core.SupervisorError:
                seen["supervisor"] = "blocked"

        env = self.with_fault("before:assets", compete)
        code, out, err = self.run_tool("update", "--apply", "--plan", str(path), "--approve", digest, env=env)
        self.assertEqual(code, 0, out + err)
        self.assertEqual(seen, {"reset_exit": 3, "query_start": "blocked", "supervisor": "blocked"})
        for _label, lock_path in hi.worker_lock_paths(self.layout):
            with lock_path.open("a+") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)  # every lock is released when the operation ends
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def test_arrived_work_and_pure_noop_behavior_are_unchanged(self) -> None:
        touch_task_state(self.layout, "WAIT_USER")
        self.assert_refused(self.v2, "quiet point")
        touch_task_state(self.layout, "DONE")
        self.assertEqual(self.update(self.v2)[0], 0)
        lock_path = hi.worker_lock_paths(self.layout)[1][1]
        with lock_path.open("a+") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            code, out, err = self.update(self.v2)
            self.assertEqual(code, 0, "a no-op changes nothing, so a busy worker does not matter: " + out + err)
            self.assertIn("NOOP", out)


class LinkedUnitReadinessTests(FullCase):
    """CI6-R1: `linked` only makes an external unit file discoverable; it is not boot-target enablement."""

    CODEX = earlier.ReadinessIdentityTests.CODEX
    CLAUDE = earlier.ReadinessIdentityTests.CLAUDE
    agent = earlier.ReadinessIdentityTests.agent
    ready = earlier.ReadinessIdentityTests.ready
    write_owners = earlier.ReadinessIdentityTests.write_owners
    status = earlier.ReadinessIdentityTests.status

    def test_linked_units_are_usable_now_but_not_reboot_persistent(self) -> None:
        self.ready()
        control = self.status()
        self.assertEqual((control["overall"], control["boot"]), ("READY", {"enabled_units": True, "headless_server_enabled": True, "linger": "enabled"}))
        for unit in ("herdr-supervisor-worker.path", "herdr-telegram.service", "herdr-server.service"):
            self.host.enabled_units[unit] = "linked"
        report = self.status()
        self.assertEqual(report["overall"], "READY_NOW")
        self.assertEqual(report["boot"], {"enabled_units": False, "headless_server_enabled": False, "linger": "enabled"})
        persistence = next(item for item in report["items"] if item["id"] == "reboot-persistence")
        self.assertEqual(persistence["status"], "ACTION_REQUIRED")
        self.assertIn("linked", persistence["action"])

    def test_each_required_unit_must_be_enabled_and_runtime_enablement_stays_negative(self) -> None:
        self.ready()
        for unit in ("herdr-supervisor-worker.path", "herdr-telegram.service", "herdr-server.service"):
            for state in ("linked", "linked-runtime", "enabled-runtime"):
                with self.subTest(unit=unit, state=state):
                    self.host.enabled_units[unit] = state
                    self.assertEqual(self.status()["overall"], "READY_NOW")
            self.host.enabled_units[unit] = "enabled"
        self.assertEqual(self.status()["overall"], "READY")
        self.host.linger = "no"
        self.assertEqual(self.status()["overall"], "READY_NOW", "the missing boot chain link is still reported")
