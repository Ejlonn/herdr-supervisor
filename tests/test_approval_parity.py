"""Preflight and approval share their read-only binding predicates (supervisor reliability, batch 3). Memory-only fixtures: a fake HEAD resolver stands in for the
configured product repository, so the former create-passes/approve-fails stale-worktree case is reproduced and the earlier, precise failure is asserted."""
from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path

from v2_fixtures import SHA_A, V2Case, hs  # noqa: I001 - first: puts the source or installed layout on sys.path

SHA_B = "b" * 40


class ParityCase(V2Case):
    def tree_digest(self) -> dict[str, str]:
        """Every file below the supervisor state/log/inbox roots, hashed: preflight must leave all of it byte-identical."""
        roots = [self.paths.state_dir]
        digest: dict[str, str] = {}
        for root in roots:
            for path in sorted(p for p in root.rglob("*") if p.is_file()):
                digest[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
        return digest

    def to_runtime_ready(self, **plan) -> None:
        """Plan approved (runtime + push required); the agent is about to propose a runtime gate."""
        self.write_plan()
        self.start_gated([{"v2": ("plan", "human", "plan_approval", str(self.plan_payload(**plan)))}])
        self.approve_pending()
        self.herdr.responses = [{"v2": ("brief", "claude")}, {"v2": ("implement", "codex")}]
        # stop right before the runtime gate: the controller is left in an approved, runtime-pending state through the normal route
        self.resume_with([{"v2": ("brief", "claude")}, {"v2": ("implement", "codex")}, {"v2": ("review", "human", "runtime_validation", str(self.runtime_payload(decision_question="Run?", approval_consequence="c", decline_consequence="d", expected_output="o")))}])

    def gate_state(self) -> dict:
        state = self.state()
        self.assertEqual(state["pending_gate"]["gate_type"], "runtime_validation")
        return state


class PreflightParityTests(ParityCase):
    def runtime_gate_inputs(self):
        """An approved plan and the payload for a runtime gate, without creating the gate."""
        self.write_plan()
        self.start_gated([{"v2": ("plan", "human", "plan_approval", str(self.plan_payload()))}])
        self.approve_pending()
        return self.state(), self.runtime_payload(decision_question="Run?", approval_consequence="c", decline_consequence="d", expected_output="o")

    def test_unchanged_binding_passes_preflight_and_the_real_approval(self) -> None:
        state, payload = self.runtime_gate_inputs()
        result = self.sup.gate_preflight(state, "runtime_validation", str(payload))
        self.assertEqual((result["ok"], result["errors"]), (True, []))
        self.assertEqual(result["binding"]["repository_head"], SHA_A)
        self.assertEqual(result["binding"]["product_repo"], str(self.product_repo))
        self.assertEqual(result["binding"]["config_file"], str(self.paths.config_file))
        self.resume_with([{"v2": ("brief", "claude")}, {"v2": ("implement", "codex")}, {"v2": ("review", "human", "runtime_validation", str(payload))}])
        self.assertTrue(self.approve_pending()["ok"])

    def test_the_former_stale_worktree_case_fails_early_with_expected_observed_and_path(self) -> None:
        state, payload = self.runtime_gate_inputs()
        self.head = SHA_B  # the configured product repository points at a different worktree/HEAD than the candidate
        before = self.tree_digest()
        result = self.sup.gate_preflight(state, "runtime_validation", str(payload))
        self.assertFalse(result["ok"])
        problem = result["errors"][0]
        self.assertEqual((problem["predicate"], problem["expected"], problem["observed"], problem["path"]), ("repository_head", SHA_A, SHA_B, str(self.product_repo)))
        self.assertIn("approval would be refused", problem["message"])
        self.assertIn(str(self.product_repo), problem["message"])
        self.assertEqual(result["binding"]["repository_head"], SHA_B)
        self.assertEqual(self.tree_digest(), before, "preflight wrote no state, event, log or inbox file")
        self.assertEqual(self.sup.store.read_state()["approved_plan"]["plan_sha256"], state["approved_plan"]["plan_sha256"], "no approval was invalidated or superseded")
        # the same predicate is what the receiving controller refuses with, with the same expected/observed values
        self.head = SHA_A
        self.resume_with([{"v2": ("brief", "claude")}, {"v2": ("implement", "codex")}, {"v2": ("review", "human", "runtime_validation", str(payload))}])
        self.head = SHA_B
        with self.assertRaisesRegex(hs.SupervisorError, r"runtime candidate or approved plan changed: repository_head: expected a{40}, observed b{40} \(path .*product\)"):
            self.approve_pending()

    def test_changed_after_preflight_is_refused_at_approval_time(self) -> None:
        state, payload = self.runtime_gate_inputs()
        self.assertTrue(self.sup.gate_preflight(state, "runtime_validation", str(payload))["ok"])
        self.resume_with([{"v2": ("brief", "claude")}, {"v2": ("implement", "codex")}, {"v2": ("review", "human", "runtime_validation", str(payload))}])
        self.head = SHA_B  # HEAD moves after the (passing) preflight and after the gate was created
        with self.assertRaises(hs.SupervisorError):
            self.approve_pending()
        self.assertEqual(self.state()["pending_gate"]["status"], "pending")
        self.assertNotIn("runtime_authorization", self.state()["pending_gate"], "no runtime execution was authorized")

    def test_a_push_approval_refusal_also_names_the_stale_binding(self) -> None:
        self.write_plan()
        self.start_gated([{"v2": ("plan", "human", "plan_approval", str(self.plan_payload()))}])
        self.approve_pending()
        self.resume_with([{"v2": ("brief", "claude")}, {"v2": ("implement", "codex")}, {"v2": ("review", "human", "runtime_validation", str(self.runtime_payload()))}])
        self.record_runtime("PASS")
        self.assertEqual(self.state()["pending_gate"]["gate_type"], "push_approval")
        self.assertEqual(self.sup.approval_readiness(self.state(), "push_approval"), [])
        self.head = SHA_B
        with self.assertRaisesRegex(hs.SupervisorError, r"repository_head: expected a{40}, observed b{40} \(path .*product\)"):
            self.approve_pending()

    def test_changed_plan_fails_preflight_with_hash_expected_observed_and_path(self) -> None:
        state, payload = self.runtime_gate_inputs()
        plan_path = Path(state["approved_plan"]["plan_path"])
        plan_path.write_text("changed after approval\n")
        result = self.sup.gate_preflight(state, "runtime_validation", str(payload))
        self.assertFalse(result["ok"])
        self.assertTrue(any(e.get("predicate") == "approved_plan_hash" for e in result["errors"]), result)
        problem = next(e for e in result["errors"] if e.get("predicate") == "approved_plan_hash")
        self.assertEqual((problem["expected"], problem["path"]), (state["approved_plan"]["plan_sha256"], str(plan_path)))
        self.assertNotEqual(problem["observed"], problem["expected"])

    def test_manifest_binding_is_checked_by_both_and_a_drifted_tree_is_named(self) -> None:
        root = self.project_root / "immutable-candidate"
        root.mkdir()
        (root / "script.py").write_text("print(1)\n")
        manifest = self.review_dir / "manifest.json"
        manifest.write_text(json.dumps({"script.py": hs.sha256_file(root / "script.py")}))
        self.write_plan()
        self.start_gated([{"v2": ("plan", "human", "plan_approval", str(self.plan_payload(runtime_manifest_root=str(root), push_approval_required=False, rebuild_required=False)))}])
        self.approve_pending()
        payload = self.runtime_payload(candidate_sha=None, repository=str(root), candidate_kind="manifest", candidate_digest=hs.sha256_file(manifest), candidate_manifest=str(manifest),
                                       decision_question="Run?", approval_consequence="c", decline_consequence="d", expected_output="o", rebuild_required=False)
        state = self.state()
        self.assertTrue(self.sup.gate_preflight(state, "runtime_validation", str(payload))["ok"])
        self.resume_with([{"v2": ("review", "human", "runtime_validation", str(payload))}])
        (root / "extra").write_text("x")  # drift after the gate was created and preflight passed
        problems = self.sup.approval_readiness(self.state(), "runtime_validation")
        self.assertEqual([p["predicate"] for p in problems], ["runtime_manifest"])
        self.assertEqual(problems[0]["expected"], str(root))
        with self.assertRaisesRegex(hs.SupervisorError, "runtime_manifest"):
            self.approve_pending()

    def test_the_approved_manifest_root_is_preserved_even_when_runtime_validation_is_not_required(self) -> None:
        root = self.project_root / "immutable-candidate"
        root.mkdir()
        self.write_plan()
        self.start_gated([{"v2": ("plan", "human", "plan_approval", str(self.plan_payload(runtime_manifest_root=str(root), runtime_validation_required=False, push_approval_required=False, rebuild_required=False)))}])
        self.approve_pending()
        policy = self.state()["runtime_policy"]
        self.assertEqual((policy["runtime_manifest_root"], policy["runtime_validation_required"]), (str(root), False))

    def test_preflight_never_mutates_the_controller_state_object_or_files(self) -> None:
        state, payload = self.runtime_gate_inputs()
        snapshot = json.dumps(state, sort_keys=True)
        before = self.tree_digest()
        for head in (SHA_A, SHA_B):
            self.head = head
            self.sup.gate_preflight(state, "runtime_validation", str(payload))
            self.sup.approval_readiness(state, "runtime_validation")
        self.assertEqual(json.dumps(state, sort_keys=True), snapshot)
        self.assertEqual(self.tree_digest(), before)

    def test_cli_and_worker_paths_agree_on_the_same_decision_inputs(self) -> None:
        state, payload = self.runtime_gate_inputs()
        worker = self.make_supervisor()  # a second controller from the same paths and configuration, as the worker process builds it
        self.assertEqual(self.sup.binding_view(state), worker.binding_view(state))
        self.head = SHA_B
        cli_problems = self.sup.approval_readiness(state, "runtime_validation")
        worker_problems = worker.approval_readiness(state, "runtime_validation")
        self.assertEqual(cli_problems, worker_problems)
        self.assertEqual(self.sup.gate_preflight(state, "runtime_validation", str(payload))["errors"][0]["predicate"], "repository_head")
        self.assertEqual(worker.gate_preflight(state, "runtime_validation", str(payload))["errors"][0]["predicate"], "repository_head")


class PlanReplacementParityTests(ParityCase):
    """CI5: the shared readiness validates the state a PLAN approval would produce, including a retained manifest runtime candidate and its approved root."""

    def manifest_candidate_run(self) -> Path:
        root = self.project_root / "immutable-candidate"
        root.mkdir()
        (root / "script.py").write_text("print(1)\n")
        manifest = self.review_dir / "manifest.json"
        manifest.write_text(json.dumps({"script.py": hs.sha256_file(root / "script.py")}))
        self.write_plan()
        self.start_gated([{"v2": ("plan", "human", "plan_approval", str(self.plan_payload(runtime_manifest_root=str(root), push_approval_required=False, rebuild_required=False)))}])
        self.approve_pending()
        payload = self.runtime_payload(candidate_sha=None, repository=str(root), candidate_kind="manifest", candidate_digest=hs.sha256_file(manifest), candidate_manifest=str(manifest),
                                       decision_question="Run?", approval_consequence="c", decline_consequence="d", expected_output="o", rebuild_required=False)
        self.resume_with([{"v2": ("review", "human", "runtime_validation", str(payload))}])
        state = self.state()
        self.assertEqual((state["supervisor_state"], bool(state["runtime_candidate"])), ("WAIT_RUNTIME_VALIDATION", True))
        return root

    def replacement_payload(self, root: Path | None, other: Path, *, required: bool, variant: str) -> Path:
        overrides = {"runtime_validation_required": required, "push_approval_required": False, "rebuild_required": False}
        if variant == "unchanged":
            overrides["runtime_manifest_root"] = str(root)
        elif variant == "differing":
            overrides["runtime_manifest_root"] = str(other)
        return self.plan_payload(**overrides)

    def raise_replacement_gate(self, payload: Path) -> dict:
        """Persist a pending replacement plan gate the way the controller would hold it (no approval yet)."""
        with self.sup.store.transaction():
            state = self.sup.store.read_state()
            block = hs.ProtocolBlock(version=2, run_id=state["run_id"], turn_id="00000000-0000-4000-8000-0000000000c1", stage="plan", next_agent="human", gate="plan_approval", payload=str(payload), handoff="replacement")
            state["pending_gate"] = self.sup.create_gate(state, block)
            state["supervisor_state"] = "WAIT_PLAN_APPROVAL"
            self.sup.store.write_state(state)
        return self.state()

    def test_omitted_and_differing_roots_fail_early_in_preflight_and_leave_the_approval_unchanged(self) -> None:
        for required in (False, True):
            for variant in ("omitted", "differing"):
                with self.subTest(required=required, variant=variant):
                    self.reset_fixture()
                    root = self.manifest_candidate_run()
                    other = self.project_root / "another-root"
                    other.mkdir()
                    payload = self.replacement_payload(root, other, required=required, variant=variant)
                    state = self.state()
                    result = self.sup.gate_preflight(state, "plan_approval", str(payload))
                    self.assertFalse(result["ok"], result)
                    problem = next(e for e in result["errors"] if e.get("predicate") == "plan_policy_state")
                    self.assertEqual(problem["expected"], str(root))
                    self.assertEqual(problem["observed"], None if variant == "omitted" else str(other))
                    self.assertIn("manifest root is not approved", problem["detail"])
                    # the receiving controller refuses the same way, before any change
                    pending = self.raise_replacement_gate(payload)
                    before, digest_before = json.dumps(self.state(), sort_keys=True), self.tree_digest()
                    self.assertEqual(self.sup.approval_readiness(pending, "plan_approval")[0]["predicate"], "plan_policy_state")
                    with self.assertRaisesRegex(hs.SupervisorError, r"plan approval refused: .*manifest root is not approved.*plan_policy_state: expected .*immutable-candidate"):
                        self.approve_pending()
                    self.assertEqual(json.dumps(self.state(), sort_keys=True), before, "state and evidence are unchanged by the refusal")
                    self.assertEqual(self.tree_digest(), digest_before)
                    self.assertTrue(self.state()["runtime_candidate"], "the retained candidate was not cleared and no root was invented")

    def test_an_unchanged_root_replacement_passes_preflight_and_the_real_approval(self) -> None:
        for required in (False, True):
            with self.subTest(required=required):
                self.reset_fixture()
                root = self.manifest_candidate_run()
                payload = self.replacement_payload(root, root, required=required, variant="unchanged")
                result = self.sup.gate_preflight(self.state(), "plan_approval", str(payload))
                self.assertEqual((result["ok"], result["errors"]), (True, []), result)
                self.raise_replacement_gate(payload)
                self.assertTrue(self.approve_pending()["ok"])
                self.assertEqual(self.state()["runtime_policy"]["runtime_manifest_root"], str(root))

    def test_a_plan_with_no_retained_candidate_is_unaffected(self) -> None:
        self.write_plan()
        self.start_gated([{"v2": ("plan", "human", "plan_approval", str(self.plan_payload(runtime_validation_required=False, push_approval_required=False, rebuild_required=False)))}])
        self.assertEqual(self.sup.approval_readiness(self.state(), "plan_approval"), [])
        self.assertTrue(self.approve_pending()["ok"])


if __name__ == "__main__":
    unittest.main()
