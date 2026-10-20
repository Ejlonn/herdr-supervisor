"""Gate-author mistakes never become a request for the operator to debug the schema."""
import copy
import json

import herdr_present as hp
from v2_fixtures import V2Case, hs


class GateRepairTests(V2Case):
    def manifest_gate(self):
        root = self.project_root / "immutable-candidate"
        root.mkdir()
        (root / "script.py").write_text("print(1)\n")
        manifest = self.review_dir / "manifest.json"
        manifest.write_text(json.dumps({"script.py": hs.sha256_file(root / "script.py")}))
        self.write_plan()
        plan = self.plan_payload(runtime_manifest_root=str(root), push_approval_required=False, rebuild_required=False)
        self.start_gated([{"v2": ("plan", "human", "plan_approval", str(plan))}])
        self.approve_pending()
        payload = self.runtime_payload(candidate_sha=None, repository=str(root), candidate_kind="manifest",
                                       candidate_digest=hs.sha256_file(manifest), candidate_manifest=str(manifest),
                                       decision_question="Run the test?", approval_consequence="Uses quota", decline_consequence="No live request",
                                       expected_output="A comparison report", rebuild_required=False)
        self.resume_with([{"v2": ("review", "human", "runtime_validation", str(payload))}])
        self.assertEqual(self.state()["supervisor_state"], "WAIT_RUNTIME_VALIDATION")
        return root, manifest

    def test_manifest_authorization_and_evidence_remain_separate(self):
        root, manifest = self.manifest_gate()
        state = self.state()
        digest = hs.sha256_file(manifest)
        evidence = self.evidence_file(candidate_sha=None, candidate_digest=digest)
        with self.assertRaisesRegex(hs.SupervisorError, "approve the runtime"):
            self.sup.record_runtime_evidence(state, run_id=state["run_id"], candidate_sha=digest, environment="TEST", evidence_file=str(evidence), result="PASS")
        self.approve_pending()
        state = self.state()
        self.assertIsNone(state["runtime_evidence"])
        self.assertIsNone(state["candidate_sha"])
        proposed = self.sup.record_runtime_evidence(state, run_id=state["run_id"], candidate_sha=digest, environment="TEST", evidence_file=str(evidence), result="PASS")
        state = self.sup.store.read_state()
        self.assertEqual(state["runtime_proposal"]["candidate_digest"], digest)
        self.sup.confirm_runtime_evidence(state, run_id=state["run_id"], gate_id=state["pending_gate"]["gate_id"], evidence_sha256=proposed["evidence_sha256"], decision="PASS", actor="operator")
        self.assertTrue(self.sup._runtime_pass_current(self.state()))
        (root / "script.py").write_text("print(2)\n")
        self.assertFalse(self.sup._runtime_pass_current(self.state()))

    def test_manifest_drift_blocks_authorization(self):
        root, _ = self.manifest_gate()
        (root / "extra").write_text("extra")
        with self.assertRaises(hs.SupervisorError):
            self.approve_pending()

    def test_invalid_gate_repairs_once_then_stops_without_scope_revision(self):
        payload = self.plan_payload()
        code = self.start_gated([{"v2": ("plan", "human", "plan_approval", str(payload))},
                                 {"v2": ("plan", "human", "plan_approval", str(payload))}])
        self.assertEqual(code, 2)
        state = self.state()
        self.assertEqual(len(self.herdr.prompts), 2)
        self.assertEqual(state["invalid_gate"]["attempts"], 1)
        self.assertIsNone(state["pending_gate"])
        self.assertIn("No human decision or approval exists", self.herdr.prompts[1][1])
        self.assertNotIn("Task:\n", self.herdr.prompts[1][1])
        card = hp.render_wait_user(state["wait_user_reason"], state, "UTC", requires_action=True)
        self.assertIn("There is no decision", card.html)
        labels = {label for row in card.keyboard for label, _, _ in row}
        self.assertIn("Repair approval request", labels)
        self.assertNotIn("Approve", labels)
        self.assertNotIn("Request revision", labels)

    def test_preflight_is_nonmutating_and_shares_validation(self):
        self.write_plan()
        payload = self.plan_payload()
        self.start_gated([{"v2": ("plan", "human", "plan_approval", str(payload))}])
        state = self.state()
        before = copy.deepcopy(state)
        disk = self.paths.state_file.read_bytes()
        events = copy.deepcopy(self.events())
        self.assertTrue(self.sup.gate_preflight(state, "plan_approval", str(payload))["ok"])
        bad = self.runtime_payload("a" * 64)
        result = self.sup.gate_preflight(state, "runtime_validation", str(bad))
        self.assertFalse(result["ok"])
        self.assertIn("candidate_sha", result["errors"][0]["message"])
        self.assertEqual(state, before)
        self.assertEqual(self.paths.state_file.read_bytes(), disk)
        self.assertEqual(self.events(), events)

    def test_stale_repair_is_rejected(self):
        self.write_plan()
        payload = self.plan_payload()
        self.start_gated([{"v2": ("plan", "human", "plan_approval", str(payload))}])
        with self.assertRaises(hs.SupervisorError):
            self.sup.repair_gate(self.state(), run_id=self.state()["run_id"], turn_id="stale")

    def test_manifest_explanation_required_and_gate_bytes_bound(self):
        self.manifest_gate()
        state = self.state()
        gate = state['pending_gate']
        raw = dict(gate['payload'])
        raw.pop('expected_output')
        with self.assertRaises(hs.SupervisorError):
            hs.validate_gate_payload(raw, 'runtime_validation', config=self.config)
        self.approve_pending()
        from pathlib import Path
        Path(gate['artifact_path']).write_text('{}')
        evidence = self.evidence_file(candidate_sha=None, candidate_digest=gate['candidate_digest'])
        with self.assertRaisesRegex(hs.SupervisorError, 'artifact changed'):
            self.sup.record_runtime_evidence(self.state(), run_id=state['run_id'], candidate_sha=gate['candidate_digest'], environment='TEST', evidence_file=str(evidence), result='PASS')

    def test_restart_retains_authorization_and_status_buttons(self):
        self.manifest_gate()
        state = self.sup.store.read_state()
        card = hp.keyboard_for_status(state)
        self.assertIn('approve', [action for row in card for _, action, _ in row])
        self.approve_pending()
        state = self.sup.store.read_state()
        self.assertNotIn('approve', [action for row in hp.keyboard_for_status(state) for _, action, _ in row])
        with self.assertRaisesRegex(hs.SupervisorError, 'already'):
            self.approve_pending()

    def test_manual_repair_is_bound_and_at_most_once(self):
        self.test_invalid_gate_repairs_once_then_stops_without_scope_revision()
        state = self.sup.store.read_state()
        run, turn = state['run_id'], state['invalid_gate']['turn_id']
        uncertain = copy.deepcopy(state)
        uncertain['delivery']['status'] = 'uncertain'
        with self.assertRaisesRegex(hs.SupervisorError, 'uncertain'):
            self.sup.repair_gate(uncertain, run_id=run, turn_id=turn)
        self.sup.repair_gate(state, run_id=run, turn_id=turn)
        restored = self.sup.store.read_state()
        self.assertEqual(restored['invalid_gate']['attempts'], 2)
        self.assertEqual(restored['continuation']['kind'], 'gate_repair')
        with self.assertRaises(hs.SupervisorError):
            self.sup.repair_gate(restored, run_id=run, turn_id=turn)
        self.assertEqual(len(self.herdr.prompts), 2)

    def test_manifest_cannot_be_git_push_and_symlinks_fail_closed(self):
        root, _ = self.manifest_gate()
        (root / 'alias').symlink_to(root / 'script.py')
        with self.assertRaises(hs.SupervisorError):
            self.approve_pending()
        state = self.state()
        self.assertIsNone(state['push_approval'])
        self.assertIsNone(state['candidate_sha'])

    def test_repair_success_returns_approval_without_replaying_task(self):
        bad = self.plan_payload()
        def repair(*_):
            self.write_plan()
        # First artifact is absent; the repair supplies it in the same session.
        self.start_gated([{'v2': ('plan', 'human', 'plan_approval', str(bad))},
                          {'v2': ('plan', 'human', 'plan_approval', str(bad)), 'before': repair}])
        state = self.state()
        self.assertEqual(state['supervisor_state'], 'WAIT_PLAN_APPROVAL')
        self.assertIsNone(state['invalid_gate'])
        self.assertEqual(len(self.herdr.prompts), 2)
        self.assertEqual(self.herdr.prompts[0][0], self.herdr.prompts[1][0])
        self.assertIsNone(state['approved_plan'])

    def test_manifest_adversarial_members(self):
        root, manifest = self.manifest_gate()
        original = manifest.read_text()
        payload = self.state()['pending_gate']['payload']
        from pathlib import Path

        from herdr_validation import verify_runtime_manifest
        for content in ('{"../escape":"' + 'a'*64 + '"}',
                        '{"script.py":"' + 'a'*64 + '","script.py":"' + 'a'*64 + '"}',
                        '{"./script.py":"' + 'a'*64 + '"}',
                        '{"script.py":true}', '{}'):
            with self.subTest(content=content):
                manifest.write_text(content)
                changed = {**payload, 'candidate_digest': hs.sha256_file(manifest)}
                with self.assertRaises(hs.SupervisorError):
                    verify_runtime_manifest(changed, approved_root=str(root), config=self.config)
        manifest.write_text(original)
        with self.assertRaises(hs.SupervisorError):
            verify_runtime_manifest(payload, approved_root=str(Path(root).parent), config=self.config)

    def test_resolved_previous_gate_does_not_block_repair(self):
        self.test_invalid_gate_repairs_once_then_stops_without_scope_revision()
        state = self.state()
        # Revision retains the previous decision for audit; it is no longer pending.
        self.write_plan()
        state['pending_gate'] = self.sup.create_gate(state, hs.ProtocolBlock(version=2, run_id=state['run_id'], turn_id=state['invalid_gate']['turn_id'], stage='plan', next_agent='human', gate='plan_approval', payload=str(self.plan_payload()), handoff='old'))
        state['pending_gate']['status'] = 'superseded'
        result = self.sup.repair_gate(state, run_id=state['run_id'], turn_id=state['invalid_gate']['turn_id'])
        self.assertTrue(result['ok'])
