"""V2 supervisor core: protocol V2, payload validation, gates, approvals, runtime/push, migration,
events/outbox, inbox, worker, exact-session recovery, deterministic lifecycle waits."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

from v2_fixtures import CLAUDE_SESSION, CODEX_SESSION, NOW, PLAN_TEXT, SHA_A, SHA_B, FakeHerdr, V2Case, hs

RUN = "11111111-1111-4111-8111-111111111111"
TURN = "22222222-2222-4222-8222-222222222222"


class ProtocolV2Tests(V2Case):
    def test_v2_block_parses_with_decoration_and_v1_stays_accepted(self) -> None:
        text = "⏺ done\n" + FakeHerdr.block_v2(RUN, TURN, "plan", "human", "plan_approval", "/x/p.json", prefix="  • ")
        block = hs.parse_protocol(text, RUN, TURN)
        self.assertEqual((block.version, block.gate, block.payload, block.next_agent), (2, "plan_approval", "/x/p.json", "human"))
        v1 = hs.parse_protocol(FakeHerdr.block_v1(RUN, TURN, "claude"), RUN, TURN)
        self.assertEqual((v1.version, v1.gate, v1.payload), (1, "none", "-"))

    def test_v2_rejects_unknown_version_gate_payload_and_conflicts(self) -> None:
        bad_version = FakeHerdr.block_v2(RUN, TURN, "plan", "human").replace("HERDR_PROTOCOL=2", "HERDR_PROTOCOL=3")
        self.assertIsNone(hs.parse_protocol(bad_version, RUN, TURN))
        self.assertIsNone(hs.parse_protocol(FakeHerdr.block_v2(RUN, TURN, "plan", "human", "mystery", "/x"), RUN, TURN))
        self.assertIsNone(hs.parse_protocol(FakeHerdr.block_v2(RUN, TURN, "plan", "human", "plan_approval", "relative/p.json"), RUN, TURN))
        self.assertIsNone(hs.parse_protocol(FakeHerdr.block_v2(RUN, TURN, "plan", "human", "plan_approval", "/x/<p>.json"), RUN, TURN))
        stale = FakeHerdr.block_v2(RUN, "33333333-3333-4333-8333-333333333333", "plan", "done")
        self.assertIsNone(hs.parse_protocol(stale, RUN, TURN))
        two = FakeHerdr.block_v2(RUN, TURN, "plan", "done") + "\n" + FakeHerdr.block_v2(RUN, TURN, "plan", "human", "plan_approval", "/x/p.json")
        with self.assertRaises(hs.SupervisorError):
            hs.parse_protocol(two, RUN, TURN)
        # a V1 and a V2 block for the same turn conflict too
        mixed = FakeHerdr.block_v1(RUN, TURN, "done") + "\n" + FakeHerdr.block_v2(RUN, TURN, "plan", "done")
        with self.assertRaises(hs.SupervisorError):
            hs.parse_protocol(mixed, RUN, TURN)

    def test_echoed_v2_template_never_matches(self) -> None:
        state = {"run_id": RUN, "task_text": "t", "active_agent": "codex", "workflow_policy": "gated_v2"}
        prompt = self.sup.build_prompt(state, TURN, "initial")
        self.assertIn("HERDR_PROTOCOL=2", prompt)
        self.assertEqual(hs.find_protocol_blocks(prompt), [])

    def test_later_stage_prompt_does_not_replay_full_task(self) -> None:
        task = "UNIQUE-LONG-TASK-BODY"
        state = self.sup.initialize(task, "codex", workflow_policy="gated_v2")
        state["last_successful_handoff"] = {"stage": "fix", "from_agent": "codex", "summary": "Use CODEX_CORRECTION_HANDOFF.md"}
        prompt = self.sup.build_prompt(state, TURN, "normal")
        self.assertNotIn(task, prompt)
        self.assertIn("Use CODEX_CORRECTION_HANDOFF.md", prompt)
        self.assertIn("without replaying", prompt)

    def test_later_uploaded_task_prompt_uses_reference_not_body(self) -> None:
        task = "UNIQUE-UPLOADED-TASK-BODY"
        reference = str(self.review_dir / "task.md")
        state = self.sup.initialize(task, "codex", task_reference=reference, workflow_policy="gated_v2")
        state["last_successful_handoff"] = {"stage": "implement", "from_agent": "codex", "summary": "Review the handoff"}
        prompt = self.sup.build_prompt(state, TURN, "normal")
        self.assertNotIn(task, prompt)
        self.assertIn(reference, prompt)


class PayloadValidationTests(V2Case):
    def test_safe_file_rules(self) -> None:
        good = self.plan_payload()
        self.assertEqual(hs.check_safe_file(str(good), root=self.review_root, max_bytes=65536, label="p"), good)
        with self.assertRaises(hs.SupervisorError):
            hs.check_safe_file("relative.json", root=self.review_root, max_bytes=65536, label="p")
        outside = Path(self.tmp.name) / "outside.json"
        outside.write_text("{}")
        with self.assertRaises(hs.SupervisorError):
            hs.check_safe_file(str(outside), root=self.review_root, max_bytes=65536, label="p")
        with self.assertRaises(hs.SupervisorError):
            hs.check_safe_file(str(self.review_root / ".." / "outside.json"), root=self.review_root, max_bytes=65536, label="p")
        link = self.review_dir / "link.json"
        link.symlink_to(outside)
        with self.assertRaises(hs.SupervisorError) as caught:
            hs.check_safe_file(str(link), root=self.review_root, max_bytes=65536, label="p")
        self.assertIn("symlink", str(caught.exception))
        linked_dir = self.review_root / "linked"
        linked_dir.symlink_to(Path(self.tmp.name))
        with self.assertRaises(hs.SupervisorError):
            hs.check_safe_file(str(linked_dir / "outside.json"), root=self.review_root, max_bytes=65536, label="p")
        big = self.review_dir / "big.json"
        big.write_text("x" * 70000)
        with self.assertRaises(hs.SupervisorError):
            hs.check_safe_file(str(big), root=self.review_root, max_bytes=65536, label="p")
        with self.assertRaises(hs.SupervisorError):
            hs.check_safe_file(str(self.review_dir), root=self.review_root, max_bytes=65536, label="p")
        with self.assertRaises(hs.SupervisorError):
            hs.check_safe_file(str(self.review_root), root=self.review_root, max_bytes=65536, label="p")

    def test_payload_schema_failures(self) -> None:
        base = json.loads(self.plan_payload().read_text())
        cases = [
            ("schema", {"schema_version": 2}), ("type", {"gate_type": "push_approval"}), ("title", {"task_title": ""}),
            ("bool", {"rebuild_required": "yes"}), ("list", {"intended_changes": "x"}), ("control", {"summary": "a\x00b"}),
            ("review_dir", {"review_directory": str(self.tmp.name)}), ("long", {"summary": "x" * 3000}),
        ]
        for label, override in cases:
            with self.assertRaises(hs.SupervisorError, msg=label):
                hs.validate_gate_payload({**base, **override}, "plan_approval", config=self.config)
        with self.assertRaises(hs.SupervisorError):
            hs.validate_gate_payload("not an object", "plan_approval", config=self.config)
        question = json.loads(self.question_payload().read_text())
        with self.assertRaises(hs.SupervisorError):
            hs.validate_gate_payload({**question, "choices": ["only"]}, "generic_question", config=self.config)
        with self.assertRaises(hs.SupervisorError):
            hs.validate_gate_payload({**question, "answer_mode": "free"}, "generic_question", config=self.config)
        runtime = json.loads(self.runtime_payload().read_text())
        with self.assertRaises(hs.SupervisorError):
            hs.validate_gate_payload({**runtime, "candidate_sha": "abc"}, "runtime_validation", config=self.config)
        with self.assertRaises(hs.SupervisorError):
            hs.validate_gate_payload({**runtime, "repository": str(self.tmp.name)}, "runtime_validation", config=self.config)
        with self.assertRaises(hs.SupervisorError):
            hs.validate_gate_payload({**runtime, "runtime_validation_missing": False}, "runtime_validation", config=self.config)
        ok = hs.validate_gate_payload(runtime, "runtime_validation", config=self.config)
        self.assertEqual(ok["candidate_sha"], SHA_A)


class PlanApprovalTests(V2Case):
    """Plan approval A–I, revision, unchanged hash, one-time replay rejection."""

    def start_to_plan_gate(self) -> dict:
        self.write_plan()
        payload = self.plan_payload()
        code = self.start_gated([{"v2": ("plan", "human", "plan_approval", str(payload))}])
        self.assertEqual(code, 4)
        state = self.state()
        self.assertEqual(state["supervisor_state"], "WAIT_PLAN_APPROVAL")
        return state

    def test_a_codex_plan_creates_wait_plan_approval_with_trusted_hashes(self) -> None:
        state = self.start_to_plan_gate()
        gate = state["pending_gate"]
        self.assertEqual(gate["gate_type"], "plan_approval")
        self.assertEqual(gate["artifact_sha256"], hs.sha256_file(self.review_dir / "CODEX_PLAN.md"))
        self.assertEqual(gate["status"], "pending")
        self.assertEqual(self.event_types()[-1], "PLAN_APPROVAL_REQUIRED")
        self.assertEqual(state["workflow_policy"], "gated_v2")
        self.assertEqual(len(self.herdr.prompts), 1, "only the Codex planning turn was sent")
        self.assertEqual(self.herdr.prompts[0][0], "codex-main")
        # summary fields for the Telegram card contain the plan facts, not the terminal
        self.assertEqual(gate["summary_fields"]["rebuild_required"], True)
        self.assertEqual(gate["summary_fields"]["task_title"], "Add widget")

    def test_b_gated_task_must_start_with_codex_and_no_claude_before_approval(self) -> None:
        with self.assertRaises(hs.SupervisorError):
            self.sup.run_new("t", "claude", workflow_policy="gated_v2")
        # Codex tries to hand implementation to Claude before any approval -> WAIT_USER, no Claude prompt.
        self.assertEqual(self.start_gated([{"v2": ("plan", "claude")}]), 2)
        state = self.state()
        self.assertEqual(state["supervisor_state"], "WAIT_USER")
        self.assertIn("before the human approves", state["wait_user_reason"])
        self.assertEqual([n for n, _ in self.herdr.prompts], ["codex-main"])

    def test_c_unchanged_approval_succeeds_once_and_replay_fails(self) -> None:
        state = self.start_to_plan_gate()
        result = self.approve_pending()
        self.assertTrue(result["ok"])
        after = self.state()
        self.assertEqual(after["supervisor_state"], "RUNNING")
        self.assertEqual(after["approved_plan"]["plan_sha256"], state["pending_gate"]["artifact_sha256"])
        self.assertEqual(after["runtime_policy"], {"runtime_validation_required": True, "rebuild_required": True, "rebuild_reason": "backend image contains the source", "push_approval_required": True, "migration_required": False, "validation_mode": "operator_collaborative"})
        self.assertEqual(after["continuation"]["kind"], "plan_approved")
        self.assertEqual(after["pending_gate"]["status"], "approved")
        self.assertIn("PLAN_APPROVED", self.event_types())
        with self.assertRaises(hs.SupervisorError) as caught:
            self.approve_pending()
        self.assertIn("one-time", str(caught.exception))

    def test_d_changed_plan_invalidates_approval_with_exact_message(self) -> None:
        state = self.start_to_plan_gate()
        (self.review_dir / "CODEX_PLAN.md").write_text(PLAN_TEXT + "\n## Extra\nmore\n")
        with self.assertRaises(hs.SupervisorError) as caught:
            self.approve_pending()
        self.assertEqual(str(caught.exception), hs.PLAN_CHANGED_MESSAGE)
        after = self.state()
        self.assertEqual(after["supervisor_state"], "WAIT_USER")
        self.assertEqual(after["pending_gate"]["status"], "superseded")
        self.assertIsNone(after["approved_plan"])
        self.assertEqual(len(self.herdr.prompts), 1, "no agent resumed")
        # a presented (stale) hash is also refused even if the file were restored
        (self.review_dir / "CODEX_PLAN.md").write_text(PLAN_TEXT)
        with self.assertRaises(hs.SupervisorError):
            with self.sup.store.transaction():
                st = self.sup.store.read_state()
                self.sup.approve_gate(st, run_id=st["run_id"], gate_id=state["pending_gate"]["gate_id"], actor="cli")

    def test_e_after_approval_codex_continues_then_claude_implements(self) -> None:
        self.start_to_plan_gate()
        self.approve_pending()
        code = self.resume_with([{"v2": ("brief", "claude")}, {"v2": ("implement", "codex")}, {"v2": ("review", "human", "runtime_validation", str(self.runtime_payload()))}])
        self.assertEqual(code, 4)
        names = [n for n, _ in self.herdr.prompts]
        self.assertEqual(names, ["codex-main", "codex-main", "claude-main", "codex-main"])
        self.assertIn("APPROVED the plan (SHA-256", self.herdr.prompts[1][1])
        self.assertIn("CODEX_BRIEF.md", self.herdr.prompts[1][1])
        self.assertEqual(self.state()["supervisor_state"], "WAIT_RUNTIME_VALIDATION")

    def test_f_reject_enters_wait_user_and_records_hash(self) -> None:
        state = self.start_to_plan_gate()
        with self.sup.store.transaction():
            st = self.sup.store.read_state()
            self.sup.reject_gate(st, run_id=st["run_id"], gate_id=state["pending_gate"]["gate_id"], actor="telegram:1", chat_id=5, note="not now")
        after = self.state()
        self.assertEqual(after["supervisor_state"], "WAIT_USER")
        self.assertEqual(after["approvals"][-1]["action"], "rejected")
        self.assertEqual(after["approvals"][-1]["artifact_sha256"], state["pending_gate"]["artifact_sha256"])
        self.assertIn("PLAN_REJECTED", self.event_types())
        self.assertEqual(self.sup.resume(), 2, "a rejected plan never continues implementation")
        self.assertEqual(len(self.herdr.prompts), 1)
        self.assertTrue(self.state()["wait_user_requires_action"])

    def test_g_revision_returns_to_same_codex_session_with_new_gate(self) -> None:
        state = self.start_to_plan_gate()
        old_gate = state["pending_gate"]["gate_id"]
        with self.sup.store.transaction():
            st = self.sup.store.read_state()
            self.sup.revise_gate(st, run_id=st["run_id"], gate_id=old_gate, actor="telegram:1", note="split into two stages")
        self.assertEqual(self.state()["supervisor_state"], "RUNNING")

        def rewrite_plan(fake, name, text):
            (self.review_dir / "CODEX_PLAN.md").write_text(PLAN_TEXT + "\n## Stage 2\n")

        code = self.resume_with([{"v2": ("plan", "human", "plan_approval", str(self.review_dir / "plan_payload.json")), "before": rewrite_plan}])
        self.assertEqual(code, 4)
        after = self.state()
        self.assertEqual(after["supervisor_state"], "WAIT_PLAN_APPROVAL")
        self.assertNotEqual(after["pending_gate"]["gate_id"], old_gate)
        self.assertNotEqual(after["pending_gate"]["artifact_sha256"], state["pending_gate"]["artifact_sha256"])
        self.assertEqual(self.herdr.prompts[1][0], "codex-main")
        self.assertIn("split into two stages", self.herdr.prompts[1][1])
        # the old gate id can no longer be approved
        with self.assertRaises(hs.SupervisorError):
            with self.sup.store.transaction():
                st = self.sup.store.read_state()
                self.sup.approve_gate(st, run_id=st["run_id"], gate_id=old_gate, actor="cli")

    def test_h_expired_gate_and_wrong_run_are_refused(self) -> None:
        state = self.start_to_plan_gate()
        self.clock.current += 8 * 86400
        with self.assertRaises(hs.SupervisorError) as caught:
            self.approve_pending()
        self.assertIn("expired", str(caught.exception))
        with self.assertRaises(hs.SupervisorError):
            with self.sup.store.transaction():
                st = self.sup.store.read_state()
                self.sup.approve_gate(st, run_id="00000000-0000-4000-8000-000000000000", gate_id=state["pending_gate"]["gate_id"], actor="cli")

    def test_i_agent_done_cannot_bypass_gates_and_human_without_gate_fails_closed(self) -> None:
        self.write_plan()
        self.assertEqual(self.start_gated([{"v2": ("plan", "done")}]), 2)
        self.assertIn("plan approval", self.state()["wait_user_reason"])
        # human without typed gate under gated_v2
        self.sup.store.write_control("cancelled")
        with self.sup.store.transaction():
            st = self.sup.store.read_state()
            st["supervisor_state"] = "CANCELLED"
            self.sup.store.write_state(st)
        self.herdr = FakeHerdr()
        self.sup = self.make_supervisor()
        self.assertEqual(self.start_gated([{"v2": ("plan", "human")}]), 2)
        self.assertIn("typed HERDR_GATE", self.state()["wait_user_reason"])

    def test_v1_block_in_gated_task_and_typed_gate_in_v1_task_fail_closed(self) -> None:
        self.assertEqual(self.start_gated([{"v1": "claude"}]), 2)
        self.assertIn("protocol-1", self.state()["wait_user_reason"])
        self.herdr.responses = []
        with self.sup.store.transaction():
            st = self.sup.store.read_state()
            st["supervisor_state"] = "CANCELLED"
            self.sup.store.write_state(st)
        self.write_plan()
        payload = self.plan_payload()
        self.herdr = FakeHerdr()
        self.sup = self.make_supervisor()
        self.herdr.responses = [{"v2": ("plan", "human", "plan_approval", str(payload))}]
        self.assertEqual(self.sup.run_new("v1 task", "codex", workflow_policy="v1"), 2)
        self.assertIn("gated_v2", self.state()["wait_user_reason"])

    def test_generic_question_answer_flow(self) -> None:
        self.write_plan()
        payload = self.question_payload()
        self.assertEqual(self.start_gated([{"v2": ("plan", "human", "generic_question", str(payload))}]), 2)
        state = self.state()
        self.assertEqual(state["supervisor_state"], "WAIT_USER")
        self.assertEqual(state["pending_gate"]["gate_type"], "generic_question")
        self.assertEqual(self.sup.resume(), 2, "resume does not skip a pending question")
        with self.assertRaises(hs.SupervisorError):
            with self.sup.store.transaction():
                st = self.sup.store.read_state()
                self.sup.answer_gate(st, run_id=st["run_id"], gate_id=st["pending_gate"]["gate_id"], actor="cli", answer="maybe")
        with self.sup.store.transaction():
            st = self.sup.store.read_state()
            self.sup.answer_gate(st, run_id=st["run_id"], gate_id=st["pending_gate"]["gate_id"], actor="cli", answer="enum")
        self.assertEqual(self.resume_with([{"v2": ("plan", "human", "plan_approval", str(self.plan_payload()))}]), 4)
        self.assertIn("Answer:\nenum", self.herdr.prompts[1][1])


class RuntimePushTests(V2Case):
    """Runtime/push A–I with exact-SHA invalidation."""

    def to_runtime_gate(self, sha: str = SHA_A) -> dict:
        self.write_plan()
        self.start_gated([{"v2": ("plan", "human", "plan_approval", str(self.plan_payload()))}])
        self.approve_pending()
        self.resume_with([{"v2": ("brief", "claude")}, {"v2": ("implement", "codex")}, {"v2": ("review", "human", "runtime_validation", str(self.runtime_payload(sha)))}])
        state = self.state()
        self.assertEqual(state["supervisor_state"], "WAIT_RUNTIME_VALIDATION")
        return state

    def record(self, result: str, *, sha: str = SHA_A, environment: str = "TEST", evidence: Path | None = None) -> dict:
        evidence = evidence or self.evidence_file(sha, "PASS" if result == "PASS" else "FAIL")
        return self.record_runtime(result, sha=sha, environment=environment, evidence=evidence)

    def test_a_runtime_required_stops_and_notifies_push_blocked(self) -> None:
        state = self.to_runtime_gate()
        event = [e for e in self.events() if e["type"] == "RUNTIME_VALIDATION_READY"][-1]
        self.assertTrue(event["data"]["runtime_not_run"])
        self.assertTrue(event["data"]["push_blocked"])
        self.assertEqual(state["candidate_sha"], SHA_A)
        self.assertEqual(self.sup.resume(), 4, "resume keeps waiting for runtime evidence")

    def test_b_done_and_push_gate_refused_without_runtime_evidence(self) -> None:
        self.to_runtime_gate()
        with self.sup.store.transaction():
            st = self.sup.store.read_state()
            st["pending_gate"]["status"] = "superseded"
            st["supervisor_state"] = "RUNNING"
            self.sup.store.write_state(st)
        self.assertEqual(self.resume_with([{"v2": ("fix", "done")}]), 2)
        self.assertIn("runtime validation is required", self.state()["wait_user_reason"])

    def test_c_wrong_sha_evidence_rejected(self) -> None:
        self.to_runtime_gate()
        with self.assertRaises(hs.SupervisorError):
            self.record("PASS", sha=SHA_B, evidence=self.evidence_file(SHA_B))
        with self.assertRaises(hs.SupervisorError):
            self.record("PASS", evidence=self.evidence_file(SHA_B))  # file says B, command says A
        with self.assertRaises(hs.SupervisorError):
            self.record("PASS", sha="abc")
        self.assertIsNone(self.state()["runtime_evidence"])

    def test_d_head_mismatch_env_schema_path_timestamp_failures(self) -> None:
        self.to_runtime_gate()
        self.head = SHA_B
        with self.assertRaises(hs.SupervisorError) as caught:
            self.record("PASS")
        self.assertIn("HEAD", str(caught.exception))
        self.head = SHA_A
        with self.assertRaises(hs.SupervisorError):
            self.record("PASS", environment="PROD")
        with self.assertRaises(hs.SupervisorError):
            self.record("PASS", evidence=self.evidence_file(environment="PROD"))
        with self.assertRaises(hs.SupervisorError):
            self.record("PASS", evidence=self.evidence_file(timestamp="yesterday"))
        with self.assertRaises(hs.SupervisorError):
            self.record("PASS", evidence=self.evidence_file(timestamp=hs.iso_utc(NOW + 3600)))
        with self.assertRaises(hs.SupervisorError):
            self.record("PASS", evidence=self.evidence_file(commands=[]))
        outside = Path(self.tmp.name) / "ev.json"
        outside.write_text(self.evidence_file().read_text())
        with self.assertRaises(hs.SupervisorError):
            self.record("PASS", evidence=outside)
        with self.assertRaises(hs.SupervisorError):
            self.record("PASS", evidence=self.evidence_file(result="FAIL"))  # PASS command with FAIL file
        self.assertEqual(self.state()["supervisor_state"], "WAIT_RUNTIME_VALIDATION")

    def test_e_valid_pass_advances_to_push_approval_and_local_tests_alone_never_finish(self) -> None:
        self.to_runtime_gate()
        result = self.record("PASS")
        self.assertTrue(result["ok"])
        state = self.state()
        self.assertEqual(state["supervisor_state"], "WAIT_PUSH_APPROVAL")
        self.assertEqual(state["runtime_evidence"]["result"], "PASS")
        self.assertEqual(state["runtime_evidence"]["evidence_sha256"], hs.sha256_file(self.review_dir / "RUNTIME_EVIDENCE.json"))
        self.assertEqual(state["pending_gate"]["gate_type"], "push_approval")
        self.assertIn("PUSH_APPROVAL_REQUIRED", self.event_types())
        self.assertNotEqual(state["supervisor_state"], "DONE")

    def test_f_fail_stays_blocked_and_emits_failed(self) -> None:
        self.to_runtime_gate()
        self.record("FAIL")
        state = self.state()
        self.assertEqual(state["supervisor_state"], "WAIT_RUNTIME_VALIDATION")
        self.assertEqual(state["runtime_evidence"]["result"], "FAIL")
        self.assertEqual(self.event_types()[-1], "RUNTIME_VALIDATION_FAILED")
        self.assertEqual(self.sup.resume(), 4)

    def test_g_candidate_change_invalidates_evidence(self) -> None:
        self.to_runtime_gate()
        self.record("PASS")
        # a later runtime gate with another SHA (e.g. after a fix) invalidates evidence and push state
        with self.sup.store.transaction():
            st = self.sup.store.read_state()
            self.sup.revise_gate(st, run_id=st["run_id"], gate_id=st["pending_gate"]["gate_id"], actor="cli", note="found a bug; new commit")
        self.head = SHA_B
        self.resume_with([{"v2": ("fix", "human", "runtime_validation", str(self.runtime_payload(SHA_B)))}])
        state = self.state()
        self.assertEqual(state["supervisor_state"], "WAIT_RUNTIME_VALIDATION")
        self.assertEqual(state["candidate_sha"], SHA_B)
        self.assertIsNone(state["runtime_evidence"])
        self.assertIsNone(state["push_approval"])

    def test_h_push_approval_one_time_and_bound(self) -> None:
        self.to_runtime_gate()
        self.record("PASS")
        state = self.state()
        self.assertEqual(state["pending_gate"]["gate_type"], "push_approval")
        with self.assertRaises(hs.SupervisorError):
            self.approve_pending(artifact_sha256="0" * 64)
        result = self.approve_pending(actor="telegram:1", chat_id=5)
        self.assertTrue(result["ok"])
        after = self.state()
        self.assertEqual(after["push_approval"]["candidate_sha"], SHA_A)
        self.assertEqual(after["push_approval"]["evidence_sha256"], state["runtime_evidence"]["evidence_sha256"])
        self.assertEqual(after["push_approval"]["plan_sha256"], state["approved_plan"]["plan_sha256"])
        self.assertEqual(after["continuation"]["kind"], "push_approved")
        with self.assertRaises(hs.SupervisorError):
            self.approve_pending()
        self.assertEqual(self.resume_with([{"v2": ("push", "done")}]), 0)
        self.assertEqual(self.state()["supervisor_state"], "DONE")
        self.assertIn("do not run git push", self.herdr.prompts[-1][1])

    def test_i_weakened_rebuild_requirement_and_push_without_pass_fail_closed(self) -> None:
        self.write_plan()
        self.start_gated([{"v2": ("plan", "human", "plan_approval", str(self.plan_payload()))}])
        self.approve_pending()
        self.assertEqual(self.resume_with([{"v2": ("brief", "claude")}, {"v2": ("implement", "codex")}, {"v2": ("review", "human", "runtime_validation", str(self.runtime_payload(rebuild_required=False)))}]), 2)
        self.assertIn("weakens", self.state()["wait_user_reason"])
        self.assertEqual(self.sup.resume(), 2, "action-required WAIT_USER is not cleared by resume")
        self.assertEqual(len(self.herdr.prompts), 4)
        # agent-raised push gate without runtime PASS (after human guidance)
        with self.sup.store.transaction():
            st = self.sup.store.read_state()
            st["candidate_sha"] = SHA_A
            self.sup.store.write_state(st)
            self.sup.revise_gate(st, run_id=st["run_id"], gate_id="-", actor="cli", note="keep rebuild=true")
        push_payload = self.review_dir / "push.json"
        push_payload.write_text(json.dumps({"schema_version": 1, "gate_type": "push_approval", "task_title": "Add widget", "summary": "push", "review_directory": str(self.review_dir), "candidate_sha": SHA_A, "local_gate_result": "PASS", "codex_review_status": "APPROVED"}))
        self.assertEqual(self.resume_with([{"v2": ("review", "human", "push_approval", str(push_payload))}]), 2)
        self.assertIn("runtime PASS", self.state()["wait_user_reason"])

    def test_runtime_not_required_continues_after_pass(self) -> None:
        self.write_plan()
        self.start_gated([{"v2": ("plan", "human", "plan_approval", str(self.plan_payload(push_approval_required=False)))}])
        self.approve_pending()
        self.resume_with([{"v2": ("brief", "claude")}, {"v2": ("implement", "codex")}, {"v2": ("review", "human", "runtime_validation", str(self.runtime_payload()))}])
        self.record("PASS")
        self.assertEqual(self.state()["supervisor_state"], "RUNNING")
        self.assertEqual(self.resume_with([{"v2": ("finish", "done")}]), 0)
        self.assertIn("runtime validation PASS", self.herdr.prompts[-1][1])


class StateMigrationAndEventsTests(V2Case):
    def test_v1_state_read_without_write_then_backup_and_migrate(self) -> None:
        v1 = {"schema_version": 1, "run_id": RUN, "task_id": RUN, "task_text": "old", "phase": "initial", "active_agent": "codex", "start_agent": "codex", "supervisor_state": "WAIT_USER", "worker_pid": None, "native_sessions": {"codex": CODEX_SESSION, "claude": CLAUDE_SESSION}, "last_successful_handoff": None, "quota_wait": None, "delivery": None, "wait_user_reason": "x", "last_error": None, "created_at": "t", "turns_completed": 0}
        self.paths.state_file.write_text(json.dumps(v1))
        before = self.paths.state_file.read_bytes()
        self.sup.status()
        self.sup.doctor()
        self.assertEqual(self.paths.state_file.read_bytes(), before, "status/doctor never write V1 state")
        state = self.sup.store.read_state()
        self.assertEqual(state["workflow_policy"], "v1")
        self.assertEqual(state["_loaded_schema"], 1)
        self.sup.store.write_state(state)
        migrated = self.state()
        self.assertEqual(migrated["schema_version"], 2)
        self.assertIsNotNone(migrated["migrated_from_v1_at"])
        backups = list(self.paths.backups_dir.glob("state.v1.*.json"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), before)
        self.assertEqual(oct(backups[0].stat().st_mode & 0o777), "0o600")
        self.assertEqual(len(list(self.paths.backups_dir.glob("*.json"))), 1)
        self.sup.store.write_state(self.sup.store.read_state())
        self.assertEqual(len(list(self.paths.backups_dir.glob("*.json"))), 1, "backup is one-time")

    def test_migrate_install_tightens_permissions_and_preserves_owners(self) -> None:
        os.chmod(self.state_dir, 0o775)
        os.chmod(self.paths.owners_file, 0o664)
        before = self.paths.owners_file.read_bytes()
        self.assertEqual(hs.migrate_install(self.sup), 0)
        self.assertEqual(oct(self.state_dir.stat().st_mode & 0o777), "0o700")
        self.assertEqual(oct(self.paths.owners_file.stat().st_mode & 0o777), "0o600")
        self.assertEqual(self.paths.owners_file.read_bytes(), before)

    def test_events_are_deterministic_and_reconciled(self) -> None:
        self.write_plan()
        self.start_gated([{"v2": ("plan", "human", "plan_approval", str(self.plan_payload()))}])
        state = self.state()
        events = self.events()
        self.assertEqual([e["type"] for e in events], ["TASK_STARTED", "PLAN_APPROVAL_REQUIRED"])
        expected = str(hs.uuid.uuid5(hs.EVENT_NAMESPACE, f"{state['run_id']}:2:PLAN_APPROVAL_REQUIRED"))
        self.assertEqual(events[1]["event_id"], expected)
        self.assertTrue(events[1]["actionable"])
        self.assertEqual(events[1]["gate_id"], state["pending_gate"]["gate_id"])
        # an event written for an uncommitted transition (seq 3) is superseded on recovery
        orphan = {**events[1], "event_id": "orphan", "sequence": 3, "type": "WAIT_USER"}
        hs.atomic_write_json(self.paths.outbox_dir / "events" / "orphan.json", orphan)
        # a committed event whose file vanished is recreated with the same id
        (self.paths.outbox_dir / "events" / f"{expected}.json").unlink()
        with self.sup.store.transaction():
            st = self.sup.store.read_state()
            self.sup.reconcile_outbox(st)
            self.sup.store.write_state(st)
        sidecar = json.loads((self.paths.outbox_dir / "delivery" / "orphan.json").read_text())
        self.assertEqual(sidecar["status"], "superseded")
        self.assertTrue((self.paths.outbox_dir / "events" / f"{expected}.json").exists())
        self.assertEqual(self.state()["event_sequence"], 2)


class InboxWorkerRecoveryTests(V2Case):
    def test_task_command_starts_gated_run_idempotently_and_hostile_text_is_data(self) -> None:
        hostile = "Fix it; $(rm -rf /) `echo pwned` \n--start claude\x00"
        command = {"request_id": "req-0000000001", "action": "task", "task_text": hostile, "actor": "telegram:1", "chat_id": 5}
        path = self.sup.enqueue_command(command)
        self.assertEqual(oct(path.stat().st_mode & 0o777), "0o600")
        self.assertEqual(self.sup.enqueue_command(command), path, "same request id is not enqueued twice")
        results = self.sup.process_inbox()
        self.assertTrue(results[0]["ok"], results)
        state = self.state()
        self.assertEqual(state["workflow_policy"], "gated_v2")
        self.assertEqual(state["active_agent"], "codex")
        self.assertEqual(state["task_text"], hostile)
        self.assertEqual(self.herdr.prompts, [], "enqueue/process never prompts; the worker does")
        # replaying the same request returns the prior result and does not start a second task
        self.sup.enqueue_command(command)
        self.assertEqual(self.sup.process_inbox(), [], "completed request file exists; nothing pending")
        next((self.paths.inbox_dir / "completed").glob("*-req-0000000001.json")).unlink()
        self.sup.enqueue_command(command)
        replay = self.sup.process_inbox()
        self.assertTrue(replay[0].get("replayed"))
        self.assertEqual(self.state()["run_id"], state["run_id"])
        second = {"request_id": "req-0000000002", "action": "task", "task_text": "another", "actor": "telegram:1", "chat_id": 5}
        self.sup.enqueue_command(second)
        result = self.sup.process_inbox()[0]
        self.assertFalse(result["ok"])
        self.assertIn("the supervisor has no task queue", result["message"])
        self.assertEqual(self.event_types()[-1], "COMMAND_RESULT")

    def test_worker_runs_gated_task_from_inbox_and_stops_at_gate(self) -> None:
        self.write_plan()
        self.sup.enqueue_command({"request_id": "req-0000000003", "action": "task", "task_text": "Add the widget", "actor": "cli"})
        self.herdr.responses = [{"v2": ("plan", "human", "plan_approval", str(self.plan_payload()))}]
        self.assertEqual(self.sup.worker(), 4)
        self.assertEqual(self.state()["supervisor_state"], "WAIT_PLAN_APPROVAL")
        # approval arrives via the inbox; the next worker run continues in the same Codex session
        state = self.state()
        self.sup.enqueue_command({"request_id": "req-0000000004", "action": "approve", "run_id": state["run_id"], "gate_id": state["pending_gate"]["gate_id"], "expected_state": "WAIT_PLAN_APPROVAL", "artifact_sha256": state["pending_gate"]["artifact_sha256"], "payload_sha256": state["pending_gate"]["payload_sha256"], "actor": "telegram:1", "chat_id": 5})
        self.herdr.responses = [{"v2": ("brief", "claude")}, {"v2": ("implement", "human", "generic_question", str(self.question_payload()))}]
        self.assertEqual(self.sup.worker(), 2)
        self.assertEqual([n for n, _ in self.herdr.prompts], ["codex-main", "codex-main", "claude-main"])
        self.assertEqual(self.state()["pending_gate"]["gate_type"], "generic_question")

    def test_worker_boot_matrix_never_continues_human_paused_cancelled_done_error(self) -> None:
        self.write_plan()
        self.start_gated([{"v2": ("plan", "human", "plan_approval", str(self.plan_payload()))}])
        base = self.state()
        for supervisor_state, expect_prompt in (("WAIT_PLAN_APPROVAL", False), ("WAIT_USER", False), ("PAUSED", False), ("CANCELLED", False), ("DONE", False), ("ERROR", False)):
            with self.sup.store.transaction():
                st = self.sup.store.read_state()
                st["supervisor_state"] = supervisor_state
                if supervisor_state not in hs.GATE_STATES:
                    st["pending_gate"] = {**base["pending_gate"], "status": "superseded"}
                st["worker_pid"] = None
                self.sup.store.write_state(st)
            self.herdr.responses = [{"v2": ("x", "done")}]
            self.sup.worker()
            self.assertEqual(len(self.herdr.prompts), 1, f"{supervisor_state} must not prompt")
        # RUNNING with a completed delivery and a continuation continues; RECOVERED event emitted once per occurrence
        with self.sup.store.transaction():
            st = self.sup.store.read_state()
            st["supervisor_state"] = "RUNNING"
            st["continuation"] = {"kind": "revision", "note": "go", "gate_type": "plan_approval", "gate_id": "x"}
            st["pending_gate"] = {**base["pending_gate"], "status": "superseded"}
            self.sup.store.write_state(st)
        self.herdr.responses = [{"v2": ("plan", "human", "plan_approval", str(self.plan_payload()))}]
        self.assertEqual(self.sup.worker(), 4)
        self.assertEqual(len(self.herdr.prompts), 2)
        self.assertGreaterEqual(self.event_types().count("RECOVERED_AFTER_RESTART"), 1)

    def test_worker_refuses_when_another_worker_holds_the_lock(self) -> None:
        env = dict(os.environ)
        holder = subprocess.Popen([sys.executable, "-c", textwrap.dedent(f"""
            import fcntl, time
            h = open({str(self.paths.lock_file)!r}, "a+")
            fcntl.flock(h.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            print("locked", flush=True)
            time.sleep(30)
        """)], stdout=subprocess.PIPE, text=True, env=env)
        try:
            self.assertEqual(holder.stdout.readline().strip(), "locked")
            self.sup.enqueue_command({"request_id": "req-0000000009", "action": "task", "task_text": "t", "actor": "cli"})
            self.assertEqual(self.sup.worker(), 3)
            self.assertTrue(list((self.paths.inbox_dir / "pending").glob("*-req-0000000009.json")), "inbox untouched by the refused worker")
            self.assertEqual(self.herdr.prompts, [])
        finally:
            holder.kill()
            holder.wait()
            holder.stdout.close()

    def test_recovery_finds_exact_session_under_new_alias_in_the_recorded_pane_and_refuses_a_pane_change(self) -> None:
        self.write_plan()
        self.start_gated([{"v2": ("plan", "human", "plan_approval", str(self.plan_payload()))}])
        self.approve_pending()
        owners_before = self.paths.owners_file.read_text()
        # The same native session lives under another alias in its recorded pane; alias 'codex-main' is gone.
        renamed = FakeHerdr.agent("codex", "codex-2", "w3:p2", CODEX_SESSION)
        del self.herdr.agents["codex-main"]
        self.herdr.agents["codex-2"] = renamed
        self.assertEqual(self.resume_with([{"v2": ("brief", "human", "generic_question", str(self.question_payload()))}]), 2)
        self.assertEqual(self.herdr.prompts[-1][0], "codex-2", "prompted the exact session in its recorded pane (pane target)")
        self.assertEqual(self.herdr.targets[-1][1] if self.herdr.targets[-1][0] == "read" else "w3:p2", "w3:p2")
        self.assertEqual(self.herdr.starts, [])
        self.assertEqual(self.paths.owners_file.read_text(), owners_before, "identity and locator unchanged")
        # The exact session reporting a DIFFERENT pane is a pane conflict: fail closed, no command, no owner change.
        with self.sup.store.transaction():
            st = self.sup.store.read_state()
            self.sup.answer_gate(st, run_id=st["run_id"], gate_id=st["pending_gate"]["gate_id"], actor="cli", answer="enum")
        self.herdr.agents["codex-2"] = FakeHerdr.agent("codex", "codex-2", "w7:p3", CODEX_SESSION)
        self.herdr.targets.clear()
        self.assertEqual(self.resume_with([]), 2)
        self.assertIn("pane conflict", self.state()["wait_user_reason"])
        self.assertEqual([t for t in self.herdr.targets if t[0] in ("prompt", "read", "wait", "send-keys")], [])
        self.assertEqual(self.herdr.starts, [])
        self.assertEqual(self.paths.owners_file.read_text(), owners_before, "a pane conflict never rewrites the locator")

    def test_recovery_alias_reused_by_other_session_is_not_trusted(self) -> None:
        self.write_plan()
        self.start_gated([{"v2": ("plan", "human", "plan_approval", str(self.plan_payload()))}])
        self.approve_pending()
        owners_before = self.paths.owners_file.read_text()
        self.herdr.agents["codex-main"] = FakeHerdr.agent("codex", "codex-main", "w3:p2", "another-session")
        self.assertEqual(self.resume_with([]), 2)
        self.assertEqual(self.herdr.prompts[-1][0], "codex-main")  # only the original planning prompt exists
        self.assertEqual(len(self.herdr.prompts), 1)
        self.assertIn("refusing", self.state()["wait_user_reason"])
        # the exact session live under another alias in ANOTHER pane while the recorded pane is reused:
        # a pane conflict, refused without adopting either record
        self.herdr.agents["codex-real"] = FakeHerdr.agent("codex", "codex-real", "w8:p1", CODEX_SESSION)
        self.herdr.targets.clear()
        self.assertEqual(self.resume_with([]), 2)
        self.assertIn("pane conflict", self.state()["wait_user_reason"])
        self.assertEqual(len(self.herdr.prompts), 1)
        self.assertEqual([t for t in self.herdr.targets if t[0] in ("prompt", "read", "wait", "send-keys")], [])
        self.assertEqual(self.paths.owners_file.read_text(), owners_before)

    def test_recovery_missing_pane_never_relocates_or_rewrites_ownership(self) -> None:
        """The recorded pane is gone: no replacement workspace, no start elsewhere, no owner write, no prompt —
        a structured owner-recovery wait instead (the pane-recovery correction plan)."""
        self.write_plan()
        self.start_gated([{"v2": ("plan", "human", "plan_approval", str(self.plan_payload()))}])
        self.approve_pending()
        del self.herdr.agents["codex-main"]
        self.herdr.available_panes.discard("w3:p2")
        owners_before = self.paths.owners_file.read_text()
        prompts = len(self.herdr.prompts)
        self.assertEqual(self.resume_with([{"v2": ("brief", "human", "generic_question", str(self.question_payload()))}]), 2)
        self.assertEqual((self.herdr.workspaces, self.herdr.starts, len(self.herdr.prompts)), ([], [], prompts))
        self.assertEqual(self.paths.owners_file.read_text(), owners_before)
        state = self.state()
        self.assertEqual((state["supervisor_state"], state["wait_user_requires_action"], state["owner_recovery"]["classification"]), ("WAIT_USER", True, "missing_pane"))

    def test_recovery_wrong_returned_session_or_ambiguity_fails_closed(self) -> None:
        self.write_plan()
        self.start_gated([{"v2": ("plan", "human", "plan_approval", str(self.plan_payload()))}])
        self.approve_pending()
        original_start = self.herdr.start_agent

        def wrong_start(name, *, kind, pane_id, args):
            self.herdr.starts.append((name, kind, pane_id, args))
            self.herdr.agents[name] = FakeHerdr.agent(kind, name, pane_id, "fresh-session")
            return {}

        self.herdr.start_agent = wrong_start
        del self.herdr.agents["codex-main"]
        self.assertEqual(self.resume_with([]), 2)
        self.assertIn("refusing", self.state()["wait_user_reason"])
        self.assertEqual(len(self.herdr.prompts), 1)
        # ambiguity: two live panes with the same native session
        self.herdr.start_agent = original_start
        del self.herdr.agents["codex-main"]
        self.herdr.agents["codex-a"] = FakeHerdr.agent("codex", "codex-a", "w1:p1", CODEX_SESSION)
        self.herdr.agents["codex-b"] = FakeHerdr.agent("codex", "codex-b", "w1:p2", CODEX_SESSION)
        self.assertEqual(self.resume_with([]), 2)
        self.assertIn("more than one pane", self.state()["wait_user_reason"])
        self.assertEqual(len(self.herdr.prompts), 1)


class LifecycleWaitTests(V2Case):
    def test_no_output_read_or_wakeup_while_agent_stays_working(self) -> None:
        """Amendment: unchanged `working` state consumes no reads and no LLM turns; timeouts are reissued."""
        self.write_plan()
        ticks = {"n": 0}

        def keep_working_then_finish(fake: FakeHerdr, name: str) -> None:
            ticks["n"] += 1
            if ticks["n"] < 5:
                raise hs.HerdrError("timeout", code="timeout")
            run, turn = fake.ids(fake.prompts[-1][1])
            fake.agents[name]["agent_status"] = "idle"
            fake.outputs[name] = fake.block_v2(run, turn, "plan", "human", "plan_approval", str(self.plan_payload()))

        self.herdr.on_wait = keep_working_then_finish
        self.assertEqual(self.start_gated([{"error": "timeout", "status": "working"}]), 4)
        self.assertEqual(len(self.herdr.prompts), 1, "no LLM wakeup while working")
        self.assertEqual(self.herdr.reads, [("codex-main", "recent-unwrapped")], "output read exactly once, after the transition to idle")
        self.assertEqual(len(self.herdr.waits), 5)
        self.assertEqual(self.event_types(), ["TASK_STARTED", "PLAN_APPROVAL_REQUIRED"], "no progress events while working")

    def test_blocking_quota_snapshot_is_the_only_midturn_read_trigger(self) -> None:
        reset = NOW + 600

        def flip_quota(fake: FakeHerdr, name: str) -> None:
            fake.visible[name] = "You've hit your usage limit"
            self.write_quota("codex", 0, reset, 60, NOW + 86400)
            fake.on_wait = None

        self.herdr.on_wait = flip_quota
        self.herdr.on_refresh = lambda fake: self.write_quota_fresh(
            "codex", 100 if self.clock.current >= reset + 60 else 0,
            self.clock.current + 9999, 60, NOW + 86400,
        )
        original_sleep = self.clock.sleep

        def sleep_and_idle(seconds: float) -> None:
            original_sleep(seconds)
            if self.clock.current >= reset + 60:
                self.herdr.agents["codex-main"]["agent_status"] = "idle"
                self.herdr.outputs["codex-main"] = "limit earlier\n"

        self.sup.sleeper = sleep_and_idle
        self.herdr.responses = [{"error": "timeout", "status": "working"}, {"v2": ("plan", "done")}]
        self.assertEqual(self.start_gated(self.herdr.responses), 2)  # done before approval -> WAIT_USER (policy), but continuation happened
        self.assertEqual(self.herdr.reads[0], ("codex-main", "visible"))
        self.assertIn("WAIT_QUOTA", self.event_types())
        self.assertIn("QUOTA_RESUMED", self.event_types())
        self.assertEqual(len(self.herdr.prompts), 2)
        self.assertIn("usage-limit wait has ended", self.herdr.prompts[1][1])
        self.assertIn("HERDR_PROTOCOL=2", self.herdr.prompts[1][1])


class NewTaskOwnershipTests(V2Case):
    def test_admission_uses_existing_native_identity_without_alias(self) -> None:
        codex = self.herdr.agents.pop("codex-main")
        codex.pop("name", None)
        self.herdr.agents["unnamed-codex"] = codex
        owners = self.sup.verify_or_seed_owners()
        self.assertEqual(owners["codex"]["session_id"], CODEX_SESSION)
        self.assertEqual(self.sup.agent_name("codex"), "w3:p2")

    def test_new_task_prompts_exact_session_by_pane_when_alias_is_absent(self) -> None:
        codex = self.herdr.agents.pop("codex-main")
        codex.pop("name", None)
        self.herdr.agents["w3:p2"] = codex
        self.herdr.responses = [{"v2": ("plan", "done")}]
        self.assertEqual(self.sup.run_new("task", "codex", workflow_policy="gated_v2"), 2)
        self.assertEqual(self.herdr.prompts[0][0], "w3:p2")

    def test_admission_defers_working_pane_when_identity_is_temporarily_omitted(self) -> None:
        codex = self.herdr.agents["codex-main"]
        codex.pop("name", None)
        codex.pop("agent_session", None)
        codex["agent_status"] = "working"
        with self.assertRaisesRegex(hs.SupervisorError, "retry Start Task after that turn settles"):
            self.sup.verify_or_seed_owners()


class StatusDoctorTests(V2Case):
    def test_doctor_and_status_locate_agents_by_native_identity_when_alias_is_absent(self) -> None:
        codex = self.herdr.agents.pop("codex-main")
        codex.pop("name", None)
        self.herdr.agents["unnamed-codex-pane"] = codex
        doctor = self.sup.doctor()
        self.assertTrue(doctor["agents"]["codex"]["detected"])
        self.assertTrue(doctor["agents"]["codex"]["session_matches"])
        self.assertIsNone(doctor["agents"]["codex"]["live_name"])
        self.assertFalse(any("codex-main is not detected" in error for error in doctor["errors"]))
        status = self.sup.status()
        self.assertTrue(status["agents"]["codex"]["detected"])
        self.assertTrue(status["agents"]["codex"]["session_matches"])

    def test_doctor_reports_transient_working_identity_omission_as_warning_not_match(self) -> None:
        codex = self.herdr.agents["codex-main"]
        codex.pop("name", None)
        codex.pop("agent_session", None)
        codex["agent_status"] = "working"
        doctor = self.sup.doctor()
        entry = doctor["agents"]["codex"]
        self.assertTrue(entry["detected"])
        self.assertTrue(entry["identity_unavailable"])
        self.assertFalse(entry["session_matches"], "reporting must not fabricate an identity match")
        self.assertFalse(any("codex-main" in error for error in doctor["errors"]))
        self.assertTrue(any("temporarily omitted" in warning for warning in doctor["warnings"]))

    def test_status_json_exposes_stable_safe_fields_and_doctor_reports_telegram_unconfigured(self) -> None:
        self.write_plan()
        self.start_gated([{"v2": ("plan", "human", "plan_approval", str(self.plan_payload()))}])
        status = self.sup.status()
        for key in ("workflow_policy", "pending_gate", "approved_plan", "runtime_policy", "candidate_sha", "runtime_evidence", "push_approval", "last_event", "native_sessions_abbrev"):
            self.assertIn(key, status)
        self.assertEqual(status["pending_gate"]["gate_type"], "plan_approval")
        self.assertNotIn("payload", status["pending_gate"])
        self.assertEqual(status["native_sessions_abbrev"]["codex"], CODEX_SESSION[:8])
        json.dumps(status)
        report = self.sup.doctor()
        self.assertTrue(report["ok"], report["errors"])
        self.assertEqual(report["telegram"]["status"], "UNCONFIGURED")
        self.assertEqual(report["query_session"], {"provisioned": False, "providers": {}})
        self.assertEqual(report["task_state"], "WAIT_PLAN_APPROVAL")


class ReviewRegressionTests(V2Case):
    """CODEX_REVIEW.md findings F1–F6."""

    def approved_plan_state(self, **plan_overrides) -> None:
        self.write_plan()
        self.start_gated([{"v2": ("plan", "human", "plan_approval", str(self.plan_payload(**plan_overrides)))}])
        self.approve_pending()

    def test_same_agent_same_stage_self_route_stops_instead_of_waking_again(self) -> None:
        # The historical 59-turn loop: the first turn routed plan -> codex (itself). A self-route from the
        # initial turn is no handoff at all, so it stops after ONE submission; a later same-stage
        # self-route stops as before.
        self.write_plan()
        self.herdr.responses = [{"v2": ("plan", "codex")}, {"v2": ("plan", "codex")}]
        self.assertEqual(self.sup.run_new("task", "codex", workflow_policy="gated_v2"), 2)
        state = self.state()
        self.assertEqual(state["supervisor_state"], "WAIT_USER")
        self.assertTrue(state["wait_user_requires_action"])
        self.assertIn("without changing stage", state["wait_user_reason"])
        self.assertEqual(len(self.herdr.prompts), 1)
        self.assertEqual(state["delivery"]["outcome"], "rejected_by_policy")
        self.assertEqual(state["prompt_metrics"]["prompts"], 1)

    def test_v2_prompt_names_configured_review_root(self) -> None:
        state = self.sup.initialize("task", "codex", workflow_policy="gated_v2")
        prompt = self.sup.build_prompt_v2(state, "11111111-1111-4111-8111-111111111111", "initial")
        self.assertIn(f"below {self.config['review_root']}", prompt)

    def test_f1_plan_changed_after_approval_never_reaches_claude(self) -> None:
        self.approved_plan_state()
        (self.review_dir / "CODEX_PLAN.md").write_text(PLAN_TEXT + "\n## silently changed after approval\n")
        code = self.resume_with([{"v2": ("brief", "claude")}])
        self.assertEqual(code, 2)
        self.assertEqual([n for n, _ in self.herdr.prompts], ["codex-main", "codex-main"], "no Claude prompt")
        state = self.state()
        self.assertEqual(state["supervisor_state"], "WAIT_USER")
        self.assertTrue(state["wait_user_requires_action"])
        self.assertEqual(state["wait_user_reason"], hs.PLAN_CHANGED_MESSAGE)
        self.assertIsNone(state["approved_plan"])
        self.assertIsNone(state["runtime_policy"])
        self.assertEqual(state["superseded_plan"]["reason"], hs.PLAN_CHANGED_MESSAGE)
        self.assertEqual(self.event_types()[-1], "WAIT_USER")
        # plain resume does not continue; only revision guidance re-plans
        self.assertEqual(self.sup.resume(), 2)
        self.assertEqual(len(self.herdr.prompts), 2)

    def test_f1_plan_change_blocks_runtime_gate_evidence_push_and_done(self) -> None:
        self.approved_plan_state()

        def tamper(fake, name, text):
            (self.review_dir / "CODEX_PLAN.md").write_text("tampered\n")

        self.assertEqual(self.resume_with([{"v2": ("brief", "claude")}, {"v2": ("implement", "codex")}, {"v2": ("review", "human", "runtime_validation", str(self.runtime_payload())), "before": tamper}]), 2)
        self.assertIn(hs.PLAN_CHANGED_MESSAGE, self.state()["wait_user_reason"])
        self.assertIsNone(self.state()["approved_plan"])
        # evidence recording against a tampered plan (fresh scenario)
        self.reset_fixture()
        self.approved_plan_state()
        self.resume_with([{"v2": ("brief", "claude")}, {"v2": ("implement", "codex")}, {"v2": ("review", "human", "runtime_validation", str(self.runtime_payload()))}])
        (self.review_dir / "CODEX_PLAN.md").write_text("tampered\n")
        with self.assertRaises(hs.SupervisorError) as caught:
            self.record_runtime("PASS")
        self.assertEqual(str(caught.exception), hs.PLAN_CHANGED_MESSAGE)
        self.assertEqual(self.state()["supervisor_state"], "WAIT_USER")
        self.assertIsNone(self.state()["runtime_evidence"])

    def test_f2_no_runtime_path_reaches_push_approval_bound_to_head(self) -> None:
        self.approved_plan_state(runtime_validation_required=False, push_approval_required=True)
        push_payload = self.review_dir / "push.json"
        push_payload.write_text(json.dumps({"schema_version": 1, "gate_type": "push_approval", "task_title": "Add widget", "summary": "ready", "review_directory": str(self.review_dir), "candidate_sha": SHA_A, "local_gate_result": "PASS", "codex_review_status": "APPROVED", "prepared_commits": ["aaaa add widget"]}))
        self.head = SHA_B  # agent asserts A but HEAD is B -> refused
        self.assertEqual(self.resume_with([{"v2": ("brief", "claude")}, {"v2": ("implement", "codex")}, {"v2": ("review", "human", "push_approval", str(push_payload))}]), 2)
        self.assertIn("does not equal repository HEAD", self.state()["wait_user_reason"])
        self.assertIsNone(self.state()["candidate_sha"])
        # exact current HEAD -> WAIT_PUSH_APPROVAL without runtime evidence
        self.head = SHA_A
        with self.sup.store.transaction():
            st = self.sup.store.read_state()
            self.sup.revise_gate(st, run_id=st["run_id"], gate_id="-", actor="cli", note="retry with the real HEAD")
        self.assertEqual(self.resume_with([{"v2": ("review", "human", "push_approval", str(push_payload))}]), 4)
        state = self.state()
        self.assertEqual(state["supervisor_state"], "WAIT_PUSH_APPROVAL")
        self.assertEqual(state["candidate_sha"], SHA_A)
        self.assertIsNone(state["runtime_evidence"])
        self.assertEqual(state["pending_gate"]["gate_type"], "push_approval")
        result = self.approve_pending()
        self.assertTrue(result["ok"])
        self.assertEqual(self.state()["push_approval"]["candidate_sha"], SHA_A)
        self.assertEqual(self.resume_with([{"v2": ("push", "done")}]), 0)

    def test_f3_failed_local_gate_or_unapproved_review_never_reach_runtime_or_push(self) -> None:
        for label, override in (("local FAIL", {"local_gate_result": "FAIL"}), ("review PENDING", {"codex_review_status": "PENDING"}), ("review CHANGES_REQUIRED", {"codex_review_status": "CHANGES_REQUIRED"})):
            self.reset_fixture()
            self.approved_plan_state()
            self.assertEqual(self.resume_with([{"v2": ("brief", "claude")}, {"v2": ("implement", "codex")}, {"v2": ("review", "human", "runtime_validation", str(self.runtime_payload(**override)))}]), 2, label)
            state = self.state()
            self.assertEqual(state["supervisor_state"], "WAIT_USER", label)
            self.assertIn("requires", state["wait_user_reason"], label)
            self.assertIsNone(state["candidate_sha"], label)
            # no-runtime push path with the same defects
            self.reset_fixture()
            self.approved_plan_state(runtime_validation_required=False, push_approval_required=True)
            push_payload = self.review_dir / "push.json"
            push_payload.write_text(json.dumps({"schema_version": 1, "gate_type": "push_approval", "task_title": "t", "summary": "s", "review_directory": str(self.review_dir), "candidate_sha": SHA_A, "local_gate_result": "PASS", "codex_review_status": "APPROVED", **override}))
            self.assertEqual(self.resume_with([{"v2": ("brief", "claude")}, {"v2": ("implement", "codex")}, {"v2": ("review", "human", "push_approval", str(push_payload))}]), 2, label)
            self.assertEqual(self.state()["supervisor_state"], "WAIT_USER", label)
            self.assertIsNone(self.state()["push_approval"], label)

    def test_f4_head_change_after_runtime_pass_invalidates_push(self) -> None:
        self.approved_plan_state()
        self.resume_with([{"v2": ("brief", "claude")}, {"v2": ("implement", "codex")}, {"v2": ("review", "human", "runtime_validation", str(self.runtime_payload()))}])
        self.record_runtime("PASS")
        self.assertEqual(self.state()["supervisor_state"], "WAIT_PUSH_APPROVAL")
        self.head = SHA_B  # a new commit lands after the runtime PASS
        with self.assertRaises(hs.SupervisorError) as caught:
            self.approve_pending()
        self.assertIn("no longer equals candidate", str(caught.exception))
        state = self.state()
        self.assertEqual(state["supervisor_state"], "WAIT_USER")
        self.assertTrue(state["wait_user_requires_action"])
        self.assertIsNone(state["runtime_evidence"])
        self.assertIsNone(state["push_approval"])
        self.assertIsNone(state["candidate_sha"])
        self.assertEqual(state["pending_gate"]["status"], "superseded")
        self.assertEqual(self.sup.resume(), 2, "stale push gate cannot be resumed into")
        # done with a stale candidate is also blocked
        self.reset_fixture()
        self.approved_plan_state(push_approval_required=False)
        self.resume_with([{"v2": ("brief", "claude")}, {"v2": ("implement", "codex")}, {"v2": ("review", "human", "runtime_validation", str(self.runtime_payload()))}])
        self.record_runtime("PASS")
        self.head = SHA_B
        self.assertEqual(self.resume_with([{"v2": ("finish", "done")}]), 2)
        self.assertIn("no longer equals candidate", self.state()["wait_user_reason"])
        self.assertNotEqual(self.state()["supervisor_state"], "DONE")

    def test_f5_active_worker_consumes_pause_and_cancel_at_lifecycle_checkpoints(self) -> None:
        """An active worker (holding worker.lock) observes durable pause/cancel at its deterministic
        wait checkpoints; a second worker never takes ownership; lifecycle waits stay token-free."""
        self.write_plan()
        checkpoints = {"n": 0}

        def enqueue_pause_on_second_wait(fake: FakeHerdr, name: str) -> None:
            checkpoints["n"] += 1
            if checkpoints["n"] == 2:
                self.sup.enqueue_command({"request_id": "req-0000000pause", "action": "pause", "run_id": self.state()["run_id"], "actor": "telegram:1", "chat_id": 5})
            raise hs.HerdrError("timeout", code="timeout")  # agent keeps working

        self.herdr.on_wait = enqueue_pause_on_second_wait
        code = self.start_gated([{"error": "timeout", "status": "working"}])
        self.assertEqual(code, 3)
        state = self.state()
        self.assertEqual(state["supervisor_state"], "PAUSED")
        self.assertEqual(self.sup.store.read_control()["desired"], "paused")
        self.assertEqual(checkpoints["n"], 2, "pause took effect at the very next checkpoint")
        self.assertEqual(self.herdr.reads, [], "no output read while working")
        self.assertEqual(len(self.herdr.prompts), 1, "no LLM wakeup")
        self.assertIn("req-0000000pause", state["processed_requests"])
        self.assertEqual(self.event_types().count("TASK_PAUSED"), 1)
        self.assertTrue(list((self.paths.inbox_dir / "completed").glob("*-req-0000000pause.json")))
        # idempotent replay of the same request id changes nothing
        self.sup.enqueue_command({"request_id": "req-0000000pause", "action": "pause", "run_id": self.state()["run_id"], "actor": "telegram:1", "chat_id": 5})
        self.assertEqual(self.event_types().count("TASK_PAUSED"), 1)
        # cancel while a (second) worker is refused by the lock: the active worker applies it itself
        self.herdr.on_wait = None
        env = dict(os.environ)
        holder = subprocess.Popen([sys.executable, "-c", textwrap.dedent(f"""
            import fcntl, time
            h = open({str(self.paths.lock_file)!r}, "a+")
            fcntl.flock(h.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            print("locked", flush=True)
            time.sleep(30)
        """)], stdout=subprocess.PIPE, text=True, env=env)
        try:
            self.assertEqual(holder.stdout.readline().strip(), "locked")
            self.sup.enqueue_command({"request_id": "req-000000cancel", "action": "cancel", "run_id": self.state()["run_id"], "actor": "telegram:1", "chat_id": 5})
            self.assertEqual(self.sup.worker(), 3, "second worker refuses; does not take ownership")
            self.assertTrue(list((self.paths.inbox_dir / "pending").glob("*-req-000000cancel.json")), "left for the active worker")
            with self.sup.store.transaction():
                st = self.sup.store.read_state()
            st["supervisor_state"] = "RUNNING"
            self.assertFalse(self.sup.check_control(st), "the active worker's checkpoint consumes cancel")
            self.assertEqual(st["supervisor_state"], "CANCELLED")
            self.assertEqual(self.state()["supervisor_state"], "CANCELLED")
            self.assertIn("preserved", self.state()["cancel_note"])
            self.assertEqual(self.herdr.sent_keys, [])
        finally:
            holder.kill()
            holder.wait()
            holder.stdout.close()

    def test_f6_commands_stranded_in_processing_are_recovered_exactly_once(self) -> None:
        self.write_plan()
        self.start_gated([{"v2": ("plan", "human", "plan_approval", str(self.plan_payload()))}])
        state = self.state()
        gate = state["pending_gate"]
        dirs = self.sup.inbox_paths()
        # boundary 1: renamed into processing/, crashed before the action was applied
        path = self.sup.enqueue_command({"request_id": "req-0000000orph1", "action": "pause", "run_id": state["run_id"], "actor": "cli"})
        os.rename(path, dirs["processing"] / path.name)
        results = self.sup.process_inbox()
        self.assertEqual([(r["request_id"], r["ok"], r.get("replayed", False)) for r in results], [("req-0000000orph1", True, False)])
        self.assertEqual(self.state()["supervisor_state"], "PAUSED")
        self.assertFalse(list(dirs["processing"].glob("*.json")))
        self.assertTrue(list(dirs["completed"].glob("*-req-0000000orph1.json")))
        # boundary 2: state mutated and journaled, crashed before the completed result file existed
        path = self.sup.enqueue_command({"request_id": "req-0000000orph2", "action": "approve", "run_id": state["run_id"], "gate_id": gate["gate_id"], "expected_state": "WAIT_PLAN_APPROVAL", "actor": "telegram:1", "chat_id": 5})
        os.rename(path, dirs["processing"] / path.name)
        with self.sup.store.transaction():
            st = self.sup.store.read_state()
            st["supervisor_state"] = "WAIT_PLAN_APPROVAL"
            result = self.sup.apply_command(st, {"action": "approve", "run_id": state["run_id"], "gate_id": gate["gate_id"], "expected_state": "WAIT_PLAN_APPROVAL", "actor": "telegram:1", "chat_id": 5})
            st["processed_requests"]["req-0000000orph2"] = {"request_id": "req-0000000orph2", "action": "approve", "at": "t", **result}
            self.sup.store.write_state(st)
        approvals_before = len(self.state()["approvals"])
        results = self.sup.process_inbox()
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]["replayed"])
        self.assertTrue(results[0]["ok"])
        self.assertEqual(len(self.state()["approvals"]), approvals_before, "the approval was not applied twice")
        self.assertEqual(self.state()["supervisor_state"], "RUNNING")
        self.assertTrue(list(dirs["completed"].glob("*-req-0000000orph2.json")))
        self.assertFalse(list(dirs["processing"].glob("*.json")))


class F10RunBindingTests(V2Case):
    """F10: controls are run-bound at enqueue and revalidated at durable apply time."""

    def test_f10_control_queued_without_task_cannot_hit_a_later_task(self) -> None:
        # queued while no task exists (unbound), then a task starts before inbox consumption
        self.sup.enqueue_command({"request_id": "req-00000nobind", "action": "cancel", "actor": "telegram:1", "chat_id": 5})
        self.write_plan()
        self.start_gated([{"v2": ("plan", "human", "plan_approval", str(self.plan_payload()))}])
        self.sup.process_inbox()
        # the active worker's first checkpoint (F5) already consumed and refused the unbound control
        result = json.loads(next((self.paths.inbox_dir / "completed").glob("*-req-00000nobind.json")).read_text())
        self.assertFalse(result["ok"])
        self.assertIn("must be bound to a run id", result["message"])
        self.assertEqual(self.state()["supervisor_state"], "WAIT_PLAN_APPROVAL")

    def test_f10_run_change_between_enqueue_and_apply_is_refused(self) -> None:
        self.write_plan()
        self.start_gated([{"v2": ("plan", "human", "plan_approval", str(self.plan_payload()))}])
        run_a = self.state()["run_id"]
        self.sup.enqueue_command({"request_id": "req-0000000runA", "action": "cancel", "run_id": run_a, "actor": "telegram:1", "chat_id": 5})
        # run A ends and run B starts before the queued control is applied
        with self.sup.store.transaction():
            st = self.sup.store.read_state()
            st["supervisor_state"] = "CANCELLED"
            self.sup.store.write_state(st)
        self.herdr = FakeHerdr()
        self.sup = self.make_supervisor()
        self.start_gated([{"v2": ("plan", "human", "plan_approval", str(self.plan_payload()))}])
        run_b = self.state()["run_id"]
        self.assertNotEqual(run_a, run_b)
        self.sup.process_inbox()
        result = json.loads(next((self.paths.inbox_dir / "completed").glob("*-req-0000000runA.json")).read_text())
        self.assertFalse(result["ok"])
        self.assertIn("issued for run", result["message"])
        self.assertEqual(self.state()["supervisor_state"], "WAIT_PLAN_APPROVAL")
        self.assertEqual(self.state()["run_id"], run_b)
        # F5 preserved: a correctly bound pause for run B still takes effect at the next checkpoint
        self.sup.enqueue_command({"request_id": "req-0000000runB", "action": "pause", "run_id": run_b, "actor": "telegram:1", "chat_id": 5})
        with self.sup.store.transaction():
            st = self.sup.store.read_state()
        self.assertFalse(self.sup.check_control(st))
        self.assertEqual(self.state()["supervisor_state"], "PAUSED")


class OperatorHandoffTests(V2Case):
    """Operator-handoff completion: one controller predicate, durable readiness/completion records,
    zero LLM prompts/reads/wake-ups, and every advertised rejection."""

    def to_handoff_ready(self) -> dict:
        """Approved plan requiring push approval only; Codex routes done; nothing but push approval is missing."""
        self.write_plan()
        self.start_gated([{"v2": ("plan", "human", "plan_approval", str(self.plan_payload(runtime_validation_required=False, rebuild_required=False)))}])
        self.approve_pending()
        self.assertEqual(self.resume_with([{"v2": ("brief", "claude")}, {"v2": ("implement", "codex")}, {"v2": ("review", "done")}]), 2)
        state = self.state()
        self.assertEqual(state["supervisor_state"], "WAIT_USER")
        return state

    def counters(self) -> tuple[int, int, int, int]:
        return (len(self.herdr.prompts), len(self.herdr.reads), len(self.herdr.waits), len(self.herdr.starts))

    def complete(self, **kwargs) -> dict:
        with self.sup.store.transaction():
            state = self.sup.store.read_state()
            return self.sup.complete_operator_handoff(state, run_id=kwargs.pop("run_id", state["run_id"]), actor=kwargs.pop("actor", "cli"), **kwargs)

    def assert_refused(self, expected_reason: str, **kwargs) -> None:
        """The predicate and the completion path refuse for the same reason, mutate nothing, emit nothing."""
        before_state = self.paths.state_file.read_text()
        before_events = self.event_types()
        before = self.counters()
        state = self.sup.store.read_state(required=False)
        ok, reason = self.sup.operator_handoff_eligibility(state, run_id=kwargs.get("run_id"), ready_turn_id=kwargs.get("ready_turn_id"))
        self.assertFalse(ok, reason)
        self.assertIn(expected_reason, reason)
        with self.assertRaises(hs.SupervisorError) as caught:
            self.complete(**kwargs)
        self.assertIn(expected_reason, str(caught.exception))
        self.assertEqual(self.paths.state_file.read_text(), before_state, "refusal must not mutate state")
        self.assertEqual(self.event_types(), before_events, "refusal must not emit events")
        self.assertEqual(self.counters(), before, "refusal must not touch agents")

    def test_agent_done_blocked_only_by_push_policy_records_bound_readiness(self) -> None:
        state = self.to_handoff_ready()
        self.assertTrue(state["wait_user_requires_action"])
        self.assertIn("push approval is required", state["wait_user_reason"])
        self.assertIn("/done", state["wait_user_reason"])
        ready = state["operator_handoff_ready"]
        self.assertEqual(ready["run_id"], state["run_id"])
        self.assertEqual(ready["turn_id"], state["delivery"]["turn_id"])
        self.assertEqual((ready["stage"], ready["agent"], ready["unmet"]), ("review", "codex", ["push_approval"]))
        self.assertEqual(state["delivery"]["outcome"], "rejected_by_policy")
        self.assertIsNone(state["completion"])
        self.assertNotIn("TASK_DONE", self.event_types())
        self.assertNotIn("TASK_HANDED_OFF", self.event_types())
        ok, reason = self.sup.operator_handoff_eligibility(self.sup.store.read_state())
        self.assertTrue(ok, reason)
        # Text is never the trigger: the same wait without the marker is not eligible.
        with self.sup.store.transaction():
            st = self.sup.store.read_state()
            st["operator_handoff_ready"] = None
            self.sup.store.write_state(st)
        self.assert_refused("has not reported completion")

    def test_done_blocked_by_deeper_problems_records_no_readiness(self) -> None:
        # no approval at all
        self.write_plan()
        self.assertEqual(self.start_gated([{"v2": ("plan", "done")}]), 2)
        self.assertIsNone(self.state()["operator_handoff_ready"])
        self.assert_refused("has not reported completion")
        # HEAD drift with runtime policy: candidate invalidated, no readiness
        self.reset_fixture()
        self.write_plan()
        self.start_gated([{"v2": ("plan", "human", "plan_approval", str(self.plan_payload()))}])
        self.approve_pending()
        self.resume_with([{"v2": ("brief", "claude")}, {"v2": ("implement", "codex")}, {"v2": ("review", "human", "runtime_validation", str(self.runtime_payload(SHA_A)))}])
        with self.sup.store.transaction():
            st = self.sup.store.read_state()
            st["pending_gate"]["status"] = "superseded"
            st["supervisor_state"] = "RUNNING"
            self.sup.store.write_state(st)
        self.head = SHA_B
        self.assertEqual(self.resume_with([{"v2": ("fix", "done")}]), 2)
        self.assertIn("candidate changed", self.state()["wait_user_reason"])
        self.assertIsNone(self.state()["operator_handoff_ready"])

    def test_operator_done_closes_exact_state_without_evidence_approval_or_wakeup(self) -> None:
        state = self.to_handoff_ready()
        before = self.counters()
        events_before = len(self.events())
        result = self.complete(actor="telegram:7", chat_id=5, note="I will push manually")
        self.assertTrue(result["ok"])
        self.assertIn("did not verify", result["message"])
        after = self.state()
        self.assertEqual(after["supervisor_state"], "DONE")
        completion = after["completion"]
        self.assertEqual((completion["mode"], completion["run_id"], completion["stage"], completion["actor"], completion["chat_id"]), ("operator_handoff", state["run_id"], "review", "telegram:7", 5))
        self.assertEqual(completion["ready_turn_id"], state["operator_handoff_ready"]["turn_id"])
        self.assertEqual(completion["unmet"], ["push_approval"])
        self.assertIs(completion["verified_by_supervisor"], False)
        self.assertEqual(completion["note"], "I will push manually")
        self.assertIsNone(after["push_approval"], "no approval is synthesized")
        self.assertIsNone(after["runtime_evidence"], "no evidence is synthesized")
        self.assertIsNone(after["continuation"])
        self.assertFalse(after["wait_user_requires_action"])
        self.assertIsNone(after["wait_user_reason"])
        self.assertEqual(after["operator_handoff_ready"]["turn_id"], completion["ready_turn_id"], "readiness is preserved as history")
        self.assertEqual(self.event_types().count("TASK_HANDED_OFF"), 1)
        self.assertEqual(len(self.events()), events_before + 1)
        self.assertNotIn("TASK_DONE", self.event_types())
        handed = [e for e in self.events() if e["type"] == "TASK_HANDED_OFF"][0]
        self.assertIs(handed["data"]["verified_by_supervisor"], False)
        self.assertEqual(handed["data"]["unmet"], ["push_approval"])
        self.assertEqual(self.counters(), before, "completion submits no prompt, reads no transcript, wakes nothing")
        # already terminal: idempotent refusal
        self.assert_refused("already DONE")

    def test_normal_verified_done_is_unchanged(self) -> None:
        self.write_plan()
        self.start_gated([{"v2": ("plan", "human", "plan_approval", str(self.plan_payload(push_approval_required=False, runtime_validation_required=False, rebuild_required=False)))}])
        self.approve_pending()
        self.assertEqual(self.resume_with([{"v2": ("brief", "claude")}, {"v2": ("implement", "codex")}, {"v2": ("review", "done")}]), 0)
        state = self.state()
        self.assertEqual(state["supervisor_state"], "DONE")
        self.assertIsNone(state["completion"])
        self.assertIsNone(state["operator_handoff_ready"])
        self.assertEqual(self.event_types().count("TASK_DONE"), 1)
        self.assertNotIn("TASK_HANDED_OFF", self.event_types())

    def test_pending_gates_reject_operator_handoff(self) -> None:
        self.write_plan()
        self.start_gated([{"v2": ("plan", "human", "plan_approval", str(self.plan_payload()))}])
        self.assert_refused("typed gate is pending")
        self.approve_pending()
        self.resume_with([{"v2": ("brief", "human", "generic_question", str(self.question_payload()))}])
        self.assertEqual(self.state()["supervisor_state"], "WAIT_USER")
        self.assert_refused("typed gate is pending")
        self.reset_fixture()
        self.write_plan()
        self.start_gated([{"v2": ("plan", "human", "plan_approval", str(self.plan_payload()))}])
        self.approve_pending()
        self.resume_with([{"v2": ("brief", "claude")}, {"v2": ("implement", "codex")}, {"v2": ("review", "human", "runtime_validation", str(self.runtime_payload()))}])
        self.assertEqual(self.state()["supervisor_state"], "WAIT_RUNTIME_VALIDATION")
        self.assert_refused("typed gate is pending")
        self.record_runtime("PASS")
        self.assertEqual(self.state()["supervisor_state"], "WAIT_PUSH_APPROVAL")
        self.assert_refused("typed gate is pending")

    def test_lifecycle_delivery_quota_reset_and_continuation_states_reject(self) -> None:
        self.to_handoff_ready()

        def with_state(mutate) -> None:
            with self.sup.store.transaction():
                st = self.sup.store.read_state()
                mutate(st)
                self.sup.store.write_state(st)

        snapshot = self.paths.state_file.read_text()

        def restore() -> None:
            self.paths.state_file.write_text(snapshot)

        cases = [
            ("ordinary WAIT_USER", lambda st: st.update(wait_user_requires_action=False), "ordinary pause"),
            ("RUNNING", lambda st: st.update(supervisor_state="RUNNING"), "task is RUNNING"),
            ("PAUSED", lambda st: st.update(supervisor_state="PAUSED"), "task is PAUSED"),
            ("WAIT_QUOTA", lambda st: st.update(supervisor_state="WAIT_QUOTA", quota_wait={"provider": "codex", "midturn": False, "resume_at": NOW + 100, "blocking_windows": [{"kind": "five_hour", "used_percent": 100, "remaining_percent": 0, "resets_at": NOW + 100}]}), "task is WAIT_QUOTA"),
            ("quota wait record under WAIT_USER", lambda st: st.update(quota_wait={"provider": "codex", "midturn": False, "resume_at": NOW + 100, "blocking_windows": [{"kind": "five_hour", "used_percent": 100, "remaining_percent": 0, "resets_at": NOW + 100}]}), "quota wait is in flight"),
            ("pending continuation", lambda st: st.update(continuation={"kind": "revision", "gate_id": None, "gate_type": "wait_user", "note": "x"}), "continuation is still pending"),
            ("uncertain delivery", lambda st: st["delivery"].update(status="uncertain"), "delivery is uncertain"),
            ("prepared delivery", lambda st: st["delivery"].update(status="prepared"), "delivery is prepared"),
            ("live worker pid", lambda st: st.update(worker_pid=os.getpid()), "worker still owns"),
            ("in-flight reset", lambda st: st["codex_reset"].update(current_redemption_state="RESET_CONSUMING", current_idempotency_key="k" * 32, blocking_event_id="c" * 64, account_fingerprint="d" * 64), "reset is being reconciled"),
            ("wrong run marker", lambda st: st.update(run_id="0bbbbbbb-cccc-4ddd-8eee-ffffffffffff", operator_handoff_ready={**st["operator_handoff_ready"], "run_id": "0bbbbbbb-cccc-4ddd-8eee-ffffffffffff"}, delivery=None, last_successful_handoff=None), None),
        ]
        for label, mutate, expected in cases:
            with self.subTest(label):
                restore()
                with_state(mutate)
                if expected is None:
                    self.assert_refused("run_id does not match", run_id=json.loads(snapshot)["run_id"])
                else:
                    self.assert_refused(expected)
        restore()
        self.assert_refused("run_id does not match", run_id="0bbbbbbb-cccc-4ddd-8eee-ffffffffffff")
        self.assert_refused("readiness changed", ready_turn_id="0bbbbbbb-cccc-4ddd-8eee-ffffffffffff")

    def test_active_or_unlocatable_agent_and_missing_herdr_reject(self) -> None:
        self.to_handoff_ready()
        self.herdr.agents["codex-main"]["agent_status"] = "working"
        self.assert_refused("still working")
        self.herdr.agents["codex-main"]["agent_status"] = "idle"
        self.herdr.agents["codex-twin"] = FakeHerdr.agent("codex", "codex-twin", "w3:p9", CODEX_SESSION)
        self.assert_refused("not settled")
        del self.herdr.agents["codex-twin"]
        del self.herdr.agents["codex-main"]
        self.assert_refused("not settled")
        # Telegram-side supervisor has no Herdr access: state-only answer, full check refused.
        bridge_side = hs.Supervisor(self.paths, self.config, herdr=None, clock=self.clock.time)
        self.herdr.agents["codex-main"] = FakeHerdr.agent("codex", "codex-main", "w3:p2", CODEX_SESSION)
        state = self.sup.store.read_state()
        self.assertTrue(bridge_side.operator_handoff_eligibility(state, inspect_agents=False)[0])
        ok, reason = bridge_side.operator_handoff_eligibility(state)
        self.assertFalse(ok)
        self.assertIn("cannot be inspected", reason)
        with self.assertRaises(hs.SupervisorError):
            with bridge_side.store.transaction():
                bridge_side.complete_operator_handoff(bridge_side.store.read_state(), run_id=state["run_id"], actor="x")
        self.assertEqual(self.state()["supervisor_state"], "WAIT_USER")

    def test_another_worker_holding_the_lock_rejects_but_the_applying_worker_may_complete(self) -> None:
        self.to_handoff_ready()
        other = hs.WorkerLock(self.paths.lock_file)
        other.acquire()
        try:
            self.paths.lock_file.write_text("999999\n")  # simulate a different holder pid
            self.assert_refused("worker still owns")
        finally:
            other.release()
        # The detached worker applies the inbox command while holding its own lock; no turn is active.
        self.sup.enqueue_command({"request_id": "done-request-0001", "action": "done", "operator_handoff": True, "run_id": self.state()["run_id"], "actor": "telegram:1", "chat_id": 9})
        before = self.counters()
        self.assertEqual(self.sup.worker(), 0)
        self.assertEqual(self.state()["supervisor_state"], "DONE")
        self.assertEqual(self.state()["completion"]["actor"], "telegram:1")
        self.assertEqual(self.counters(), before)
        result = [e for e in self.events() if e["type"] == "COMMAND_RESULT"][-1]
        self.assertTrue(result["data"]["ok"])
        # replaying the same request or a new done: idempotent, no second event
        self.sup.enqueue_command({"request_id": "done-request-0002", "action": "done", "operator_handoff": True, "run_id": self.state()["run_id"], "actor": "telegram:1"})
        self.assertEqual(self.sup.worker(), 0)
        self.assertEqual(self.event_types().count("TASK_HANDED_OFF"), 1)
        self.assertIn("already DONE", [e for e in self.events() if e["type"] == "COMMAND_RESULT"][-1]["data"]["message"])

    def test_inbox_done_requires_binding_and_explicit_flag(self) -> None:
        self.to_handoff_ready()
        run_id = self.state()["run_id"]
        for label, command in (
            ("unbound", {"action": "done", "operator_handoff": True}),
            ("no flag", {"action": "done", "run_id": run_id}),
            ("truthy string flag", {"action": "done", "run_id": run_id, "operator_handoff": "yes"}),
            ("stale ready turn", {"action": "done", "run_id": run_id, "operator_handoff": True, "ready_turn_id": "0bbbbbbb-cccc-4ddd-8eee-ffffffffffff"}),
        ):
            with self.subTest(label):
                with self.sup.store.transaction():
                    st = self.sup.store.read_state()
                    with self.assertRaises(hs.SupervisorError):
                        self.sup.apply_command(st, command)
                self.assertEqual(self.state()["supervisor_state"], "WAIT_USER")
        self.assertNotIn("TASK_HANDED_OFF", self.event_types())

    def test_restart_preserves_readiness_and_completion_without_events_or_prompts(self) -> None:
        self.to_handoff_ready()
        before = self.counters()
        # worker restart while handoff-ready: stays waiting, no wake-up, readiness intact
        self.herdr.responses = []
        self.assertEqual(self.sup.worker(), 2)
        self.assertIsNotNone(self.state()["operator_handoff_ready"])
        self.assertEqual(self.counters(), before)
        # The worker stamped its pid before stopping at the human wait; in the test that pid is this live
        # process, so the predicate fails closed until the worker process is actually gone.
        self.assertEqual(self.state()["worker_pid"], os.getpid())
        self.assert_refused("worker still owns")
        with self.sup.store.transaction():
            st = self.sup.store.read_state()
            st["worker_pid"] = None  # what the OS reports once the detached worker has exited
            self.sup.store.write_state(st)
        self.complete()
        events = self.event_types()
        fresh = self.make_supervisor()
        self.assertEqual(fresh.worker(), 0)
        with self.assertRaises(hs.SupervisorError):
            fresh.resume()  # a handed-off task cannot be reopened
        self.assertEqual(self.event_types(), events, "restart emits no duplicate TASK_HANDED_OFF or continuation")
        self.assertEqual(self.state()["completion"]["mode"], "operator_handoff")
        self.assertEqual(self.counters(), before)
        with self.sup.store.transaction():
            st = self.sup.store.read_state()
            with self.assertRaises(hs.SupervisorError):
                self.sup.apply_command(st, {"action": "resume", "run_id": st["run_id"]})

    def test_status_uses_the_same_predicate_and_never_claims_push_ready(self) -> None:
        self.write_plan()
        self.start_gated([{"v2": ("plan", "human", "plan_approval", str(self.plan_payload(runtime_validation_required=False, rebuild_required=False)))}])
        report = self.sup.status()
        self.assertFalse(report["operator_handoff"]["available"])
        self.assertIn("typed gate is pending", report["operator_handoff"]["reason"])
        self.approve_pending()
        self.resume_with([{"v2": ("brief", "claude")}, {"v2": ("implement", "codex")}, {"v2": ("review", "done")}])
        report = self.sup.status()
        self.assertTrue(report["operator_handoff"]["available"], report["operator_handoff"]["reason"])
        self.assertEqual(report["operator_handoff_ready"]["unmet"], ["push_approval"])
        self.herdr.agents["codex-main"]["agent_status"] = "working"
        self.assertFalse(self.sup.status()["operator_handoff"]["available"])
        self.herdr.agents["codex-main"]["agent_status"] = "idle"
        self.complete(actor="cli")
        report = self.sup.status()
        self.assertEqual(report["completion"]["mode"], "operator_handoff")
        self.assertFalse(report["operator_handoff"]["available"])
        import contextlib as _ctx
        import io
        buffer = io.StringIO()
        with _ctx.redirect_stdout(buffer):
            hs.print_status(report)
        text = buffer.getvalue()
        self.assertIn("NOT verified by Supervisor: push_approval", text)
        for forbidden in ("PUSH READY", "pushed", "published", "deployed", "push approved", "runtime passed"):
            self.assertNotIn(forbidden, text)

    def test_schema_rejects_malformed_readiness_and_completion_records(self) -> None:
        self.to_handoff_ready()
        good = self.state()
        ready = good["operator_handoff_ready"]
        run_id = good["run_id"]
        bad_ready = [
            {**ready, "schema_version": True}, {**ready, "run_id": "0bbbbbbb-cccc-4ddd-8eee-ffffffffffff"}, {**ready, "turn_id": "x"},
            {**ready, "stage": "bad stage!"}, {**ready, "agent": "human"}, {**ready, "unmet": []}, {**ready, "unmet": ["push_approval", "push_approval"]},
            {**ready, "unmet": ["git_push"]}, {**ready, "unmet": [True]}, {**ready, "unmet": [{}]}, {**ready, "unmet": [["push_approval"]]}, {**ready, "unmet": {"push_approval": 1}},
            {**ready, "unmet": ["push_approval", {"x": 1}]}, {**ready, "created_at_unix": True}, {**ready, "created_at_unix": "now"},
            {**ready, "handoff": 5}, {**ready, "handoff": "h" * 301}, "ready", ["x"],
        ]
        for index, value in enumerate(bad_ready):
            with self.subTest(f"ready {index}"):
                st = json.loads(self.paths.state_file.read_text())
                st["operator_handoff_ready"] = value
                with self.assertRaises(hs.SupervisorError):
                    hs.validate_state_v2(st)
        completion = {"schema_version": 1, "mode": "operator_handoff", "run_id": run_id, "stage": "review", "ready_turn_id": ready["turn_id"], "actor": "cli", "chat_id": None,
                      "at_unix": NOW, "at_utc": hs.iso_utc(NOW), "note": None, "unmet": ["push_approval"], "verified_by_supervisor": False}
        st = json.loads(self.paths.state_file.read_text())
        st["supervisor_state"] = "DONE"
        st["completion"] = completion
        hs.validate_state_v2(st)  # the well-formed record is accepted
        st["supervisor_state"] = "WAIT_USER"
        with self.assertRaises(hs.SupervisorError):
            hs.validate_state_v2(st)  # a completion record outside DONE is invalid
        st["supervisor_state"] = "DONE"
        bad_completion = [
            {**completion, "mode": "verified"}, {**completion, "mode": 1}, {**completion, "run_id": "0bbbbbbb-cccc-4ddd-8eee-ffffffffffff"},
            {**completion, "chat_id": True}, {**completion, "chat_id": "5"}, {**completion, "chat_id": 5.0}, {**completion, "at_unix": True}, {**completion, "at_unix": "1"},
            {**completion, "actor": ""}, {**completion, "actor": 7}, {**completion, "note": "n" * 501}, {**completion, "note": 3},
            {**completion, "unmet": []}, {**completion, "unmet": "push_approval"}, {**completion, "unmet": [{}]}, {**completion, "unmet": [["push_approval"]]},
            {**completion, "unmet": ["push_approval", "push_approval"]}, {**completion, "unmet": [None]}, {**completion, "ready_turn_id": "0bbbbbbb-cccc-4ddd-8eee-ffffffffffff"},
            {**completion, "verified_by_supervisor": True},
            {**completion, "verified_by_supervisor": 0}, {**completion, "verified_by_supervisor": None}, {**completion, "ready_turn_id": None}, {**completion, "schema_version": 1.0},
        ]
        for index, value in enumerate(bad_completion):
            with self.subTest(f"completion {index}"):
                st["completion"] = value
                with self.assertRaises(hs.SupervisorError):  # never a raw TypeError/KeyError
                    hs.validate_state_v2(st)
        # cross-field: a completion may only cite the persisted readiness event; without a marker the
        # UUID-shaped ready_turn_id is accepted on its own (legacy-safe), with a marker it must match
        st["completion"] = completion
        st["operator_handoff_ready"] = {**ready, "turn_id": "0bbbbbbb-cccc-4ddd-8eee-ffffffffffff"}
        with self.assertRaises(hs.SupervisorError):
            hs.validate_state_v2(st)
        st["operator_handoff_ready"] = None
        hs.validate_state_v2(st)
        # every malformed case surfaces as SupervisorError through the store as well
        st["operator_handoff_ready"] = {**ready, "unmet": [{"nested": True}]}
        self.paths.state_file.write_text(json.dumps(st))
        with self.assertRaises(hs.SupervisorError):
            self.sup.store.read_state()
        # existing states load with no completion authority
        self.assertIsNone(hs.new_v2_fields("gated_v2")["completion"])
        self.assertIsNone(hs.migrate_state_v1({"schema_version": 1, "supervisor_state": "DONE"})["operator_handoff_ready"])

    def test_cli_done_requires_explicit_operator_handoff_flag(self) -> None:
        self.to_handoff_ready()
        run_id = self.state()["run_id"]
        self.paths.config_file.write_text(json.dumps({"schema_version": 1, "herdr_bin": sys.executable, "project_root": str(self.project_root), "review_root": str(self.review_root), "product_repo": str(self.product_repo), "quota_dir": str(self.quota_dir)}))
        os.environ["HERDR_SUPERVISOR_CONFIG"] = str(self.paths.config_file)
        os.environ["HERDR_SUPERVISOR_STATE_DIR"] = str(self.state_dir)
        try:
            import contextlib as _ctx
            import io
            err = io.StringIO()
            with _ctx.redirect_stderr(err):
                self.assertEqual(hs.main(["done", "--run-id", run_id]), 2)
            self.assertIn("requires --operator-handoff", err.getvalue())
            self.assertEqual(self.state()["supervisor_state"], "WAIT_USER")
            with self.assertRaises(SystemExit):
                hs.build_parser().parse_args(["done", "--operator-handoff"])  # --run-id is mandatory
            # with the flag, the real CLI runs the full predicate; this Python binary is not Herdr, so the
            # lifecycle cannot be established and the run stays open (fail closed, no mutation)
            err = io.StringIO()
            with _ctx.redirect_stderr(err):
                self.assertEqual(hs.main(["done", "--run-id", run_id, "--operator-handoff"]), 2)
            self.assertIn("operator handoff refused", err.getvalue())
            self.assertEqual(self.state()["supervisor_state"], "WAIT_USER")
        finally:
            os.environ.pop("HERDR_SUPERVISOR_CONFIG", None)
            os.environ.pop("HERDR_SUPERVISOR_STATE_DIR", None)
        # the same call through the supervisor object with a settled fake Herdr completes
        self.assertTrue(self.complete(note="x")["ok"])


class PromptAcknowledgementTests(V2Case):
    """Delivery is acknowledged by an observed lifecycle transition (`--wait --until working|blocked`),
    never by elapsed time or terminal text; the settlement wait stays as the bounded fallback."""

    FIXTURE = Path(__file__).resolve().parent / "fixtures" / "herdr-0.9.0-agent-prompt-help.txt"

    def log_events(self) -> list[dict]:
        return [json.loads(line) for line in (self.paths.logs_dir / f"{self.state()['run_id']}.jsonl").read_text().splitlines()]

    def test_capability_parser_uses_the_captured_help_contract(self) -> None:
        help_text = self.FIXTURE.read_text()
        self.assertIn("--until <STATUS>", help_text)
        self.assertIn("agent_prompt_stalled", help_text)
        self.assertTrue(hs.HerdrCli.prompt_help_supports_ack(help_text))
        older = help_text.replace("--until", "--unti1")
        self.assertFalse(hs.HerdrCli.prompt_help_supports_ack(older))
        self.assertFalse(hs.HerdrCli.prompt_help_supports_ack(""))

    def test_adapter_argv_and_single_cached_probe(self) -> None:
        cli = hs.HerdrCli("/fake/herdr")
        calls: list[list[str]] = []

        def fake_run(args, *, timeout, json_result=True):
            calls.append(list(args))
            if list(args) == ["agent", "prompt", "--help"]:
                return self.FIXTURE.read_text()
            return {"result": {"agent": {"name": "codex-main", "agent_status": "working"}}}

        cli._run = fake_run  # type: ignore[method-assign]
        self.assertTrue(cli.prompt_ack_supported())
        self.assertTrue(cli.prompt_ack_supported())
        self.assertEqual(calls.count(["agent", "prompt", "--help"]), 1, "one read-only probe per process")
        response = cli.prompt_ack("codex-main", "hello", timeout_ms=8000)
        self.assertEqual(calls[-1], ["agent", "prompt", "codex-main", "hello", "--wait", "--until", "working", "--until", "blocked", "--timeout", "8000"])
        self.assertEqual(response["result"]["agent"]["agent_status"], "working")
        # the settlement path is untouched
        cli.prompt("codex-main", "hello", timeout_ms=20000)
        self.assertEqual(calls[-1], ["agent", "prompt", "codex-main", "hello", "--wait", "--timeout", "20000"])
        # a failing probe means fallback, not an error
        broken = hs.HerdrCli("/fake/herdr")

        def failing_run(args, *, timeout, json_result=True):
            raise hs.HerdrError("no such option", code="command_error")

        broken._run = failing_run  # type: ignore[method-assign]
        self.assertFalse(broken.prompt_ack_supported())
        sup = hs.Supervisor(self.paths, self.config, broken, clock=self.clock.time, sleeper=self.clock.sleep)
        self.assertFalse(sup.prompt_ack_available())
        self.assertEqual(sup.doctor()["prompt_ack_mode"], "settle")

    def test_lifecycle_ack_accepts_immediately_with_exactly_one_submission(self) -> None:
        self.write_plan()
        self.assertEqual(self.start_gated([{"v2": ("plan", "human", "plan_approval", str(self.plan_payload()))}]), 4)
        self.assertEqual(len(self.herdr.prompts), 1)
        self.assertEqual(len(self.herdr.ack_calls), 1)
        self.assertEqual(self.herdr.ack_calls[0][1], 8000)
        delivery = self.state()["delivery"]
        self.assertEqual((delivery["ack_mode"], delivery["accepted_via"], delivery["ack_observed_status"], delivery["status"]), ("lifecycle", "lifecycle_ack", "working", "completed"))
        accepted = [e for e in self.log_events() if e["event"] == "prompt_accepted"][0]
        self.assertEqual((accepted["ack_mode"], accepted["latency_ms"], accepted["observed_status"]), ("lifecycle", 0, "working"))
        self.assertNotIn("prompt_accepted_by_activity", [e["event"] for e in self.log_events()])
        self.assertEqual(self.sup.doctor()["prompt_ack_mode"], "lifecycle")

    def test_settlement_fallback_when_cli_lacks_acknowledgement_measures_the_bound(self) -> None:
        self.herdr.ack_supported = False
        self.write_plan()

        def twenty_seconds(fake, name, text):
            self.clock.current += 20.0  # the settlement wait consumes its full bound before `timeout`

        payload = str(self.plan_payload())
        self.assertEqual(self.start_gated([{"v2": ("plan", "human", "plan_approval", payload), "error": "timeout", "before": twenty_seconds}]), 4)
        self.assertEqual(len(self.herdr.prompts), 1)
        self.assertEqual(self.herdr.ack_calls, [])
        events = self.log_events()
        prepared = next(e for e in events if e["event"] == "prompt_prepared")
        accepted = next(e for e in events if e["event"] == "gate_created")  # the turn was routed
        self.assertEqual(self.state()["delivery"]["ack_mode"], "settle")
        self.assertIn("prompt_delivery_uncertain", [e["event"] for e in events])
        # latency evidence: prepared -> routed took the whole 20 s bound on the settle path
        import datetime as dt
        gap = (dt.datetime.fromisoformat(accepted["time"].replace("Z", "+00:00")) - dt.datetime.fromisoformat(prepared["time"].replace("Z", "+00:00"))).total_seconds()
        self.assertEqual(gap, 20.0)

    def test_lifecycle_ack_prepared_to_routed_latency_is_zero_on_the_same_fixture(self) -> None:
        self.write_plan()
        self.assertEqual(self.start_gated([{"v2": ("plan", "human", "plan_approval", str(self.plan_payload()))}]), 4)
        events = self.log_events()
        import datetime as dt
        prepared = next(e for e in events if e["event"] == "prompt_prepared")
        accepted = next(e for e in events if e["event"] == "gate_created")
        gap = (dt.datetime.fromisoformat(accepted["time"].replace("Z", "+00:00")) - dt.datetime.fromisoformat(prepared["time"].replace("Z", "+00:00"))).total_seconds()
        self.assertEqual(gap, 0.0)

    def test_slow_working_turn_after_ack_waits_deterministically_without_reads_or_resend(self) -> None:
        self.write_plan()
        payload = str(self.plan_payload())
        waits = {"n": 0}

        def settle_later(fake, name):
            waits["n"] += 1
            if waits["n"] == 3:
                fake.agents[name]["agent_status"] = "idle"

        self.herdr.on_wait = settle_later
        self.assertEqual(self.start_gated([{"v2": ("plan", "human", "plan_approval", payload), "status": "working"}]), 4)
        self.assertEqual(len(self.herdr.prompts), 1)
        self.assertEqual(waits["n"], 3)
        self.assertEqual([src for _, src in self.herdr.reads], ["recent-unwrapped"], "nothing is read while working; one settled read at the end")
        events = [e["event"] for e in self.log_events()]
        self.assertIn("prompt_accepted", events)
        self.assertNotIn("prompt_accepted_by_activity", events)
        self.assertNotIn("prompt_delivery_uncertain", events)

    def test_stalled_ack_stays_uncertain_then_activity_confirms(self) -> None:
        self.write_plan()
        payload = str(self.plan_payload())
        waits = {"n": 0}

        def settle_later(fake, name):
            waits["n"] += 1
            if waits["n"] == 2:
                fake.agents[name]["agent_status"] = "idle"

        self.herdr.on_wait = settle_later
        self.assertEqual(self.start_gated([{"v2": ("plan", "human", "plan_approval", payload), "error": "agent_prompt_stalled", "status": "working"}]), 4)
        self.assertEqual(len(self.herdr.prompts), 1, "a stalled acknowledgement is never resent")
        events = [e["event"] for e in self.log_events()]
        self.assertIn("prompt_delivery_uncertain", events)
        self.assertIn("prompt_accepted_by_activity", events)
        self.assertEqual(self.state()["delivery"]["ack_mode"], "lifecycle")

    def test_timeout_or_stalled_ack_with_idle_agent_and_no_block_fails_closed(self) -> None:
        for code in ("agent_prompt_stalled", "timeout"):
            with self.subTest(code):
                self.reset_fixture()
                self.write_plan()
                self.assertEqual(self.start_gated([{"output": "nothing that parses\n", "error": code, "status": "idle"}]), 2)
                state = self.state()
                self.assertEqual(state["supervisor_state"], "WAIT_USER")
                self.assertTrue(state["wait_user_requires_action"])
                self.assertIn("never confirmed accepted", state["wait_user_reason"])
                self.assertEqual(state["delivery"]["status"], "completed")
                self.assertEqual(state["delivery"]["error_code"], code)
                self.assertEqual(len(self.herdr.prompts), 1)

    def test_rejected_before_input_paths_are_unchanged_under_ack(self) -> None:
        self.write_plan()
        self.assertEqual(self.start_gated([{"error": "agent_blocked", "status": "blocked", "output": "Allow? (y/n)"}]), 2)
        self.assertIsNone(self.state()["delivery"], "rejected before input: no delivery record survives")
        self.assertEqual(len(self.herdr.prompts), 1)
        self.assertIn("prompt_rejected_before_delivery", [e["event"] for e in self.log_events()])

    def test_restart_after_uncertain_ack_recovers_without_a_second_submission(self) -> None:
        self.write_plan()

        def die_midturn(fake, name):
            raise KeyboardInterrupt

        self.herdr.on_wait = die_midturn
        self.assertEqual(self.start_gated([{"error": "agent_prompt_stalled", "status": "working"}]), 3)
        persisted = self.state()
        self.assertEqual(persisted["supervisor_state"], "PAUSED")
        self.assertEqual(persisted["delivery"]["status"], "accepted")  # activity confirmed it before the crash
        self.assertEqual(len(self.herdr.prompts), 1)
        herdr2 = FakeHerdr()
        herdr2.agents["codex-main"]["agent_status"] = "idle"
        herdr2.outputs["codex-main"] = FakeHerdr.block_v2(persisted["run_id"], persisted["delivery"]["turn_id"], "plan", "human", "plan_approval", str(self.plan_payload()))
        sup2 = hs.Supervisor(self.paths, self.config, herdr2, clock=self.clock.time, sleeper=self.clock.sleep)
        sup2.head_resolver = self.head_resolver
        self.assertEqual(sup2.resume(), 4)
        self.assertEqual(herdr2.prompts, [])
        self.assertEqual(herdr2.ack_calls, [])
        self.assertEqual(self.state()["supervisor_state"], "WAIT_PLAN_APPROVAL")

    def test_config_rejects_invalid_ack_timeout(self) -> None:
        for bad in (0, "soon", -5, True):
            with self.subTest(bad):
                self.paths.config_file.write_text(json.dumps({"schema_version": 1, "prompt_ack_timeout_ms": bad, "project_root": str(self.project_root)}))
                with self.assertRaises(hs.SupervisorError):
                    hs.load_config(self.paths.config_file)
        self.paths.config_file.write_text(json.dumps({"schema_version": 1, "project_root": str(self.project_root)}))
        self.assertEqual(hs.load_config(self.paths.config_file)["prompt_ack_timeout_ms"], 8000)


class ProtocolWidthTests(V2Case):
    """Terminal width is part of the protocol: a valid result wrapped below its logical line length
    must recover the same bound block; anything structurally outside the frame stays rejected."""

    FIXTURES = Path(__file__).resolve().parent / "fixtures"
    RUN = "20598ca2-d049-4cb9-967a-5652e7bf6eff"
    TURN = "9945228c-bb87-493a-96ea-14d190c49510"
    PAYLOAD = "/home/user/workspace/reviews/hs-token-cost-start-buttons/plan-approval.json"
    HANDOFF = "Approve the reissued plan to resume; the technical scope is unchanged from the previous token-cost and Telegram button proposal."  # legacy spaced text as captured live
    HANDOFF_COMPLIANT = "Approve_the_reissued_plan_to_resume;_the_technical_scope_is_unchanged_from_the_previous_token-cost_and_Telegram_button_proposal."

    def logical(self) -> list[str]:
        return ["HERDR_PROTOCOL=2", f"HERDR_RUN={self.RUN}", f"HERDR_TURN={self.TURN}", "HERDR_STAGE=plan", "HERDR_NEXT=human",
                "HERDR_GATE=plan_approval", f"HERDR_PAYLOAD={self.PAYLOAD}", f"HERDR_HANDOFF={self.HANDOFF}"]

    @staticmethod
    def wrap(lines: list[str], width: int, *, prefix: str = "  ") -> str:
        """Emulate a terminal: machine rows hard-wrap at `width`; free text wraps at whitespace (the
        space is dropped) and a token longer than the remaining row is split mid-token on a full row."""
        rows = []
        budget = width - len(prefix)
        for line in lines:
            if line.startswith("HERDR_HANDOFF="):
                current = ""
                for word in line.split(" "):
                    while len(word) > budget:  # over-long token: hard split on full rows
                        if current:
                            rows.append(prefix + current)
                            current = ""
                        rows.append(prefix + word[:budget])
                        word = word[budget:]
                    candidate = word if not current else current + " " + word
                    if len(candidate) > budget and current:
                        rows.append(prefix + current)
                        current = word
                    else:
                        current = candidate
                rows.append(prefix + current)
            else:
                for start in range(0, len(line), budget):
                    rows.append(prefix + line[start:start + budget])
        return "\n".join(rows) + "\n"

    def test_exact_captured_wrapped_result_recovers_the_bound_block(self) -> None:
        text = (self.FIXTURES / "codex-recent-unwrapped-wrapped-result.txt").read_text()
        block = hs.parse_protocol(text, self.RUN, self.TURN)
        self.assertIsNotNone(block)
        self.assertEqual((block.stage, block.next_agent, block.gate, block.payload, block.handoff), ("plan", "human", "plan_approval", self.PAYLOAD, self.HANDOFF))
        self.assertIsNone(hs.parse_protocol(text, self.RUN, "0bbbbbbb-cccc-4ddd-8eee-ffffffffffff"), "exact turn binding survives reconstruction")

    def test_captured_prompt_echo_and_wrapped_template_never_parse(self) -> None:
        echo = (self.FIXTURES / "codex-recent-unwrapped-prompt-echo.txt").read_text()
        self.assertEqual(hs.find_protocol_blocks(echo), [])
        template = self.sup.protocol_instructions({"run_id": self.RUN}, self.TURN)
        for width in (40, 60, 80, 200):
            self.assertEqual(hs.find_protocol_blocks(self.wrap(template.splitlines(), width)), [], width)

    def test_40_60_80_column_variants_recover_the_same_block(self) -> None:
        lines = self.logical()
        lines[7] = f"HERDR_HANDOFF={self.HANDOFF_COMPLIANT}"  # the emitted contract: words joined with underscores, no spaces
        reference = hs.parse_protocol("\n".join(lines), self.RUN, self.TURN)
        for width in (40, 60, 80, 120):
            with self.subTest(width=width):
                for prefix in ("", "  ", "│ "):
                    block = hs.parse_protocol(self.wrap(lines, width, prefix=prefix), self.RUN, self.TURN)
                    self.assertIsNotNone(block, (width, prefix))
                    self.assertEqual((block.run_id, block.turn_id, block.stage, block.next_agent, block.gate, block.payload), (reference.run_id, reference.turn_id, "plan", "human", "plan_approval", self.PAYLOAD))
                    self.assertEqual(block.handoff, self.HANDOFF_COMPLIANT)
        # legacy spaced text is re-joined best-effort: exact whenever no row is filled to the terminal width
        # (the captured live case); a word ending exactly at the row edge is the one boundary no unframed
        # transcript can disambiguate, which is why the contract now forbids spaces
        legacy = hs.parse_protocol(self.wrap(self.logical(), 60), self.RUN, self.TURN)
        self.assertIsNotNone(legacy)
        self.assertEqual(legacy.handoff.replace(" ", ""), self.HANDOFF.replace(" ", ""))

    def test_leading_dash_in_a_wrapped_payload_fragment_survives(self) -> None:
        lines = self.logical()
        lines[6] = "HERDR_PAYLOAD=/home/user/workspace/reviews/hs-token/plan"
        text = "  HERDR_PROTOCOL=2\n" + "\n".join("  " + l for l in lines[1:6]) + "\n  HERDR_PAYLOAD=/home/user/workspace/reviews/hs-token/plan\n  -approval.json\n  " + lines[7] + "\n"
        block = hs.parse_protocol(text, self.RUN, self.TURN)
        self.assertEqual(block.payload, "/home/user/workspace/reviews/hs-token/plan-approval.json")

    def test_40_column_length_edges_preserve_content_and_enforce_bounds(self) -> None:
        def frame(payload: str, handoff: str) -> list[str]:
            return ["HERDR_PROTOCOL=2", f"HERDR_RUN={self.RUN}", f"HERDR_TURN={self.TURN}", "HERDR_STAGE=plan", "HERDR_NEXT=human", "HERDR_GATE=plan_approval", f"HERDR_PAYLOAD={payload}", f"HERDR_HANDOFF={handoff}"]

        def words(n: int) -> str:  # a compliant handoff of exactly n characters (words joined with underscores)
            text = ""
            i = 0
            while len(text) < n:
                text += ("" if not text else "_") + f"w{i}"
                i += 1
            return text[:n]

        for width in (40, 60, 80):
            for label, payload, handoff, ok in (
                ("emitted maximum handoff", "/p/plan-approval.json", words(299), True),
                ("parser maximum handoff", "/p/plan-approval.json", words(600), True),
                ("one over the handoff limit", "/p/plan-approval.json", words(601), False),
                ("500-char payload", "/" + "d" * 499, "ok", True),
                ("maximum payload", "/" + "d" * 1023, "ok", True),
                ("one over the payload limit", "/" + "d" * 1024, "ok", False),
            ):
                with self.subTest(width=width, case=label):
                    block = hs.parse_protocol(self.wrap(frame(payload, handoff), width), self.RUN, self.TURN)
                    if ok:
                        self.assertIsNotNone(block, label)
                        self.assertEqual(block.payload, payload)
                        self.assertEqual(block.handoff, handoff)
                    else:
                        self.assertIsNone(block, label)
        # an excessive transcript: thousands of continuation rows never build a value or exhaust the parser
        excessive = "\n".join(frame("/p/x", "start")[:7]) + "\n" + "\n".join(["y" * 38] * 5000)
        self.assertIsNone(hs.parse_protocol(excessive, self.RUN, self.TURN))

    def test_mid_token_and_whitespace_wraps_reconstruct_the_exact_handoff(self) -> None:
        cases = [
            "Review_/home/user/workspace/reviews/hs-token-cost-start-buttons/CLAUDE_HANDOFF.md_then_run_python3_-m_unittest_discover_-s_tests",
            "supercalifragilisticexpialidocious_is_longer_than_a_forty_column_row_and_must_stay_one_token",
            "id=0bbbbbbbcccc4ddd8eeeffffffffffff-0bbbbbbbcccc4ddd8eeeffffffffffff-0bbbbbbbcccc4ddd8eeeffffffffffff_ok",
            "short_words_only,_wrapped_anywhere,_exactly_reproduced_across_every_width",
            "x" * 299,  # the maximum emitted handoff as one token
        ]
        for handoff in cases:
            for width in (40, 60, 80):
                with self.subTest(width=width, handoff=handoff[:24]):
                    lines = self.logical()
                    lines[7] = f"HERDR_HANDOFF={handoff}"
                    block = hs.parse_protocol(self.wrap(lines, width), self.RUN, self.TURN)
                    self.assertIsNotNone(block)
                    self.assertEqual(block.handoff, handoff)
                    self.assertEqual(block.payload, self.PAYLOAD)

    def test_nearby_text_truncation_reorder_duplicates_and_conflicts_are_rejected(self) -> None:
        base = self.logical()
        # neighboring HERDR_ text and prose around a valid frame do not leak into values
        around = "HERDR_HANDOFF=stale from earlier\nsome prose\n" + "\n".join(base) + "\n\nHERDR_PAYLOAD=/evil\nmore prose\n"
        block = hs.parse_protocol(around, self.RUN, self.TURN)
        self.assertEqual((block.payload, block.handoff), (self.PAYLOAD, self.HANDOFF))
        # prose directly after a legacy spaced handoff (no blank line) is bounded by the row cap, never merged into a long value
        trailing = "\n".join(base) + "\n" + "\n".join(["filler prose line that is not part of the block"] * 8)
        self.assertIsNone(hs.parse_protocol(trailing, self.RUN, self.TURN))
        # prose after a contract (underscore-joined) handoff ends the value exactly where the whitespace begins
        compliant = base[:7] + [f"HERDR_HANDOFF={self.HANDOFF_COMPLIANT}"]
        trailing = "\n".join(compliant) + "\n" + "\n".join(["filler prose line that is not part of the block"] * 8)
        block = hs.parse_protocol(trailing, self.RUN, self.TURN)
        self.assertEqual(block.handoff, self.HANDOFF_COMPLIANT)
        wrapped_then_prose = self.wrap(compliant, 40) + "\n".join(["prose right after the wrapped block"] * 3)
        self.assertEqual(hs.parse_protocol(wrapped_then_prose, self.RUN, self.TURN).handoff, self.HANDOFF_COMPLIANT)
        # truncated frames (any missing key) are not blocks
        for cut in range(1, 8):
            self.assertIsNone(hs.parse_protocol("\n".join(base[:cut]), self.RUN, self.TURN), cut)
        # a blank row inside the machine fields breaks the frame
        broken = base[:4] + [""] + base[4:]
        self.assertIsNone(hs.parse_protocol("\n".join(broken), self.RUN, self.TURN))
        # reordered keys
        reordered = base[:5] + [base[6], base[5], base[7]]
        self.assertIsNone(hs.parse_protocol("\n".join(reordered), self.RUN, self.TURN))
        # a wrapped value that swallows a foreign HERDR_ row is rejected, not concatenated
        foreign = base[:6] + ["HERDR_PAYLOAD=/home/user/x/", "HERDR_STAGE=fix", "approval.json", base[7]]
        self.assertIsNone(hs.parse_protocol("\n".join(foreign), self.RUN, self.TURN))
        # identical duplicate frames agree; conflicting frames are an error
        self.assertIsNotNone(hs.parse_protocol("\n".join(base) + "\n\n" + self.wrap(base, 60), self.RUN, self.TURN))
        other = list(base)
        other[4] = "HERDR_NEXT=codex"
        other[5] = "HERDR_GATE=none"
        other[6] = "HERDR_PAYLOAD=-"
        with self.assertRaises(hs.SupervisorError):
            hs.parse_protocol("\n".join(base) + "\n\n" + self.wrap(other, 60), self.RUN, self.TURN)
        # continuation text beyond the value's validated size is malformed, however many rows it takes
        too_much = base[:6] + ["HERDR_PAYLOAD=/a"] + ["b" * 200] * 6 + [base[7]]
        self.assertIsNone(hs.parse_protocol("\n".join(too_much), self.RUN, self.TURN))


class TokenCostGuardrailTests(V2Case):
    """Durable prompt accounting, the consecutive same-provider circuit breaker, compact prompts, and
    the missing-result retry — all with zero prompts, reads, or wakes outside accepted routes."""

    def counters(self) -> tuple[int, int, int]:
        return (len(self.herdr.prompts), len(self.herdr.reads), len(self.herdr.waits))

    def test_accounting_counts_each_prepared_prompt_once_and_never_on_restart(self) -> None:
        self.write_plan()
        self.start_gated([{"v2": ("plan", "human", "plan_approval", str(self.plan_payload()))}])
        self.approve_pending()
        self.resume_with([{"v2": ("brief", "claude")}, {"v2": ("implement", "codex")}, {"v2": ("review", "human", "runtime_validation", str(self.runtime_payload()))}])
        metrics = self.state()["prompt_metrics"]
        self.assertEqual(metrics["prompts"], 4)
        self.assertEqual((metrics["by_provider"]["codex"]["prompts"], metrics["by_provider"]["claude"]["prompts"]), (3, 1))
        self.assertEqual(metrics["chars"], sum(len(text) for _, text in self.herdr.prompts))
        self.assertEqual(metrics["chars"], metrics["by_provider"]["codex"]["chars"] + metrics["by_provider"]["claude"]["chars"])
        # restart at a human gate: no prompt, no count
        before = self.counters()
        self.assertEqual(self.make_supervisor().worker(), 4)
        self.assertEqual(self.state()["prompt_metrics"]["prompts"], 4)
        self.assertEqual(self.counters(), before)
        report = self.sup.status()
        self.assertEqual(report["prompt_metrics"]["prompts"], 4)
        self.assertEqual(report["auto_turn_limit"], 8)
        import contextlib as _ctx
        import io
        buffer = io.StringIO()
        with _ctx.redirect_stdout(buffer):
            hs.print_status(report)
        self.assertIn("Prompt deliveries prepared: 4 (", buffer.getvalue())
        self.assertIn("codex 3", buffer.getvalue())
        self.assertIn("claude 1", buffer.getvalue())
        self.assertNotIn("sent", buffer.getvalue().split("Prompt deliveries")[1].splitlines()[0])
        self.assertIn("not provider tokens", buffer.getvalue())

    def test_accounting_crash_windows_never_double_count(self) -> None:
        self.write_plan()

        # (a) crash after the prepared record, before submission: restart fails closed and prepares nothing new
        def die_before_submit(fake, name, text):
            raise KeyboardInterrupt

        self.herdr.responses = [{"before": die_before_submit}]
        self.assertEqual(self.sup.run_new("task", "codex", workflow_policy="gated_v2"), 3)
        prepared = self.state()
        self.assertEqual(prepared["prompt_metrics"]["prompts"], 1)
        self.assertEqual(prepared["delivery"]["status"], "prepared")
        sup2 = self.make_supervisor()
        self.assertEqual(sup2.resume(), 2)
        self.assertEqual(self.state()["prompt_metrics"]["prompts"], 1)
        self.assertEqual(len(self.herdr.prompts), 1)
        # F6: the record counts a PREPARED delivery; status wording must not claim it was sent
        import contextlib as _ctx
        import io
        buffer = io.StringIO()
        with _ctx.redirect_stdout(buffer):
            hs.print_status(sup2.status())
        line = next(l for l in buffer.getvalue().splitlines() if l.startswith("Prompt deliveries prepared:"))
        self.assertIn("prepared: 1 (", line)
        self.assertIn("characters prepared", line)
        self.assertNotIn("sent", line)
        self.assertNotIn("submitted", line)
        # (b) crash during uncertain delivery (stalled ack, agent working) then restart: reused record, no recount
        self.reset_fixture()
        self.write_plan()

        def die_midturn(fake, name):
            raise KeyboardInterrupt

        self.herdr.on_wait = die_midturn
        self.assertEqual(self.start_gated([{"error": "agent_prompt_stalled", "status": "working"}]), 3)
        persisted = self.state()
        self.assertEqual(persisted["prompt_metrics"]["prompts"], 1)
        herdr2 = FakeHerdr()
        herdr2.agents["codex-main"]["agent_status"] = "idle"
        herdr2.outputs["codex-main"] = FakeHerdr.block_v2(persisted["run_id"], persisted["delivery"]["turn_id"], "plan", "human", "plan_approval", str(self.plan_payload()))
        sup3 = hs.Supervisor(self.paths, self.config, herdr2, clock=self.clock.time, sleeper=self.clock.sleep)
        sup3.head_resolver = self.head_resolver
        self.assertEqual(sup3.resume(), 4)
        self.assertEqual(self.state()["prompt_metrics"]["prompts"], 1)
        self.assertEqual(herdr2.prompts, [])
        # (c) after routing: a plain resume at the gate adds nothing
        self.assertEqual(sup3.resume(), 4)
        self.assertEqual(self.state()["prompt_metrics"]["prompts"], 1)

    def test_metrics_and_breaker_state_validation(self) -> None:
        self.write_plan()
        self.start_gated([{"v2": ("plan", "human", "plan_approval", str(self.plan_payload()))}])
        good = json.loads(self.paths.state_file.read_text())
        m = good["prompt_metrics"]
        bad_metrics = [
            None, [], {**m, "prompts": True}, {**m, "prompts": -1}, {**m, "chars": "9"}, {**m, "prompts": 10**13},
            {**m, "by_provider": {"codex": m["by_provider"]["codex"]}}, {**m, "by_provider": {**m["by_provider"], "claude": {"prompts": 1.0, "chars": 0}}},
            {**m, "prompts": m["prompts"] + 1},  # totals must equal provider counters
        ]
        for index, value in enumerate(bad_metrics):
            with self.subTest(f"metrics {index}"):
                st = json.loads(self.paths.state_file.read_text())
                st["prompt_metrics"] = value
                with self.assertRaises(hs.SupervisorError):
                    hs.validate_state_v2(st)
        for value in ({"provider": "human", "count": 0}, {"provider": "codex", "count": -1}, {"provider": "codex", "count": True}, {"count": 1}, {"provider": None, "count": 1_000_001}):
            with self.subTest(f"breaker {value}"):
                st = json.loads(self.paths.state_file.read_text())
                st["consecutive_auto_turns"] = value
                with self.assertRaises(hs.SupervisorError):
                    hs.validate_state_v2(st)
        for value in ({"schema_version": 1}, {"schema_version": 1, "run_id": good["run_id"], "turn_id": "x", "agent": "codex", "attempts": 0, "created_at_unix": NOW},
                      {"schema_version": 1, "run_id": "0bbbbbbb-cccc-4ddd-8eee-ffffffffffff", "turn_id": good["delivery"]["turn_id"], "agent": "codex", "attempts": 0, "created_at_unix": NOW},
                      {"schema_version": 1, "run_id": good["run_id"], "turn_id": good["delivery"]["turn_id"], "agent": "codex", "attempts": True, "created_at_unix": NOW}):
            with self.subTest(f"missing {value}"):
                st = json.loads(self.paths.state_file.read_text())
                st["missing_result"] = value
                with self.assertRaises(hs.SupervisorError):
                    hs.validate_state_v2(st)
        # old schema-2 state without the fields loads with zero metrics and a fresh count
        st = json.loads(self.paths.state_file.read_text())
        for key in ("prompt_metrics", "consecutive_auto_turns", "missing_result"):
            st.pop(key, None)
        self.paths.state_file.write_text(json.dumps(st))
        loaded = self.sup.store.read_state()
        self.assertEqual(loaded["prompt_metrics"]["prompts"], 0)
        self.assertEqual(loaded["consecutive_auto_turns"], {"provider": None, "count": 0})
        self.assertIsNone(loaded.get("missing_result"))
        # config bounds
        for bad in (0, 1001, "8", True, 2.0):
            with self.subTest(f"config {bad!r}"):
                self.paths.config_file.write_text(json.dumps({"schema_version": 1, "max_consecutive_auto_turns": bad, "project_root": str(self.project_root)}))
                with self.assertRaises(hs.SupervisorError):
                    hs.load_config(self.paths.config_file)

    def test_historical_loop_stops_after_one_submission_even_across_restart_and_resume(self) -> None:
        self.write_plan()
        self.herdr.responses = [{"v2": ("plan", "codex")}] + [{"v2": ("plan", "codex")}] * 58
        self.assertEqual(self.sup.run_new("historical loop", "codex", workflow_policy="gated_v2"), 2)
        self.assertEqual(len(self.herdr.prompts), 1)
        state = self.state()
        self.assertEqual((state["supervisor_state"], state["wait_user_requires_action"]), ("WAIT_USER", True))
        for attempt in range(3):
            self.assertEqual(self.make_supervisor().worker(), 2)
            with self.sup.store.transaction():
                st = self.sup.store.read_state()
                with self.assertRaises(hs.SupervisorError):
                    self.sup.apply_command(st, {"action": "resume", "run_id": st["run_id"]})
        self.assertEqual(len(self.herdr.prompts), 1, "restart and resume never wake the agent again")
        self.assertEqual(self.state()["prompt_metrics"]["prompts"], 1)

    def test_stage_changing_loop_stops_before_limit_plus_one_and_human_revision_continues_once(self) -> None:
        limit = 3
        self.config = hs.resolve_config_defaults(hs.deep_merge(self.config, {"max_consecutive_auto_turns": limit}))
        self.sup = self.make_supervisor()
        self.write_plan()
        self.start_gated([{"v2": ("plan", "human", "plan_approval", str(self.plan_payload()))}])
        self.approve_pending()
        stages = [f"stage{i}" for i in range(1, 20)]
        # the approval continuation carries human authority; every following codex -> codex route changes stage
        self.assertEqual(self.resume_with([{"v2": (stage, "codex")} for stage in stages]), 2)
        # initial + approval continuation (both human authority) + `limit` automatic turns; turn limit+1 is never prepared
        self.assertEqual(len(self.herdr.prompts), 2 + limit)
        state = self.state()
        self.assertEqual(state["consecutive_auto_turns"], {"provider": "codex", "count": limit})
        self.assertEqual(state["prompt_metrics"]["prompts"], 2 + limit)
        self.assertTrue(state["wait_user_requires_action"])
        self.assertIn(f"limit {limit}", state["wait_user_reason"])
        self.assertIn("/revise", state["wait_user_reason"])
        self.assertIsNone(state["missing_result"])
        # restart/resume do not reset or bypass the limit
        self.assertEqual(self.make_supervisor().worker(), 2)
        with self.sup.store.transaction():
            st = self.sup.store.read_state()
            with self.assertRaises(hs.SupervisorError):
                self.sup.apply_command(st, {"action": "resume", "run_id": st["run_id"]})
        self.assertEqual(self.state()["consecutive_auto_turns"]["count"], limit)
        self.assertEqual(len(self.herdr.prompts), 2 + limit)
        # an authenticated human revision resets the count and continues exactly once, then the chain is bounded again
        with self.sup.store.transaction():
            st = self.sup.store.read_state()
            self.sup.guide(st, run_id=st["run_id"], actor="telegram:1", note="carry on")
        self.assertEqual(self.state()["consecutive_auto_turns"], {"provider": None, "count": 0})
        self.assertEqual(self.sup.resume(), 2)
        self.assertEqual(len(self.herdr.prompts), 2 + limit + 1 + limit, "one continuation, then at most `limit` automatic turns")
        self.assertEqual(self.state()["consecutive_auto_turns"]["count"], limit)

    def test_cross_provider_handoff_resets_the_streak_and_polling_never_changes_it(self) -> None:
        self.config = hs.resolve_config_defaults(hs.deep_merge(self.config, {"max_consecutive_auto_turns": 2}))
        self.sup = self.make_supervisor()
        self.write_plan()
        self.start_gated([{"v2": ("plan", "human", "plan_approval", str(self.plan_payload(runtime_validation_required=False, rebuild_required=False)))}])
        self.approve_pending()
        # codex: a1, a2 (2 automatic) -> claude (cross-provider: streak cleared) -> claude: b1 (1) -> codex (cleared) -> c1, c2, then blocked
        observed: list[dict] = []

        def snapshot(fake, name, text):
            observed.append(dict(self.state()["consecutive_auto_turns"]))

        responses = [{"v2": ("a1", "codex")}, {"v2": ("a2", "codex")}, {"v2": ("brief", "claude")}, {"v2": ("b1", "claude")}, {"v2": ("impl", "codex")},
                     {"v2": ("c1", "codex")}, {"v2": ("c2", "codex")}, {"v2": ("c3", "codex")}]
        self.assertEqual(self.resume_with([{**r, "before": snapshot} for r in responses]), 2)
        # streak observed at the moment each prompt was prepared (i.e. after the previous route was recorded)
        self.assertEqual(observed, [
            {"provider": None, "count": 0},      # approval continuation (human authority)
            {"provider": "codex", "count": 1},   # after a1 -> codex
            {"provider": "codex", "count": 2},   # after a2 -> codex
            {"provider": None, "count": 0},      # after brief -> claude (cross-provider handoff clears the streak)
            {"provider": "claude", "count": 1},  # after b1 -> claude
            {"provider": None, "count": 0},      # after impl -> codex (cross-provider)
            {"provider": "codex", "count": 1},   # after c1 -> codex
            {"provider": "codex", "count": 2},   # after c2 -> codex; c3 -> codex is then refused
        ])
        self.assertEqual(self.state()["consecutive_auto_turns"], {"provider": "codex", "count": 2})
        self.assertIn("consecutive automatic turns", self.state()["wait_user_reason"])
        # quota refresh requests and repeated worker invocations do not touch the streak or prompts
        before = self.counters()
        with self.sup.store.transaction():
            st = self.sup.store.read_state()
            with self.assertRaises(hs.SupervisorError):
                self.sup.apply_command(st, {"action": "refresh_quota", "run_id": st["run_id"]})  # no quota wait: refused, unchanged
        self.assertEqual(self.make_supervisor().worker(), 2)
        self.assertEqual(self.state()["consecutive_auto_turns"], {"provider": "codex", "count": 2})
        self.assertEqual(self.counters(), before)

    def test_task_sentinel_only_in_initial_prompt_and_boilerplate_ceiling(self) -> None:
        sentinel = "SENTINEL-" + "7c1e5a2f" * 3
        self.write_plan()
        self.herdr.responses = [{"v2": ("plan", "human", "plan_approval", str(self.plan_payload()))}]
        self.assertEqual(self.sup.run_new(f"Do the thing {sentinel} carefully", "codex", workflow_policy="gated_v2"), 4)
        self.approve_pending()
        self.resume_with([{"v2": ("brief", "claude")}, {"v2": ("implement", "human", "generic_question", str(self.question_payload()))}])
        with self.sup.store.transaction():
            st = self.sup.store.read_state()
            self.sup.answer_gate(st, run_id=st["run_id"], gate_id=st["pending_gate"]["gate_id"], actor="cli", answer="enum")
        self.resume_with([{"v2": ("implement", "codex")}, {"v2": ("review", "human", "generic_question", str(self.question_payload()))}])
        texts = [text for _, text in self.herdr.prompts]
        self.assertGreaterEqual(len(texts), 4)
        self.assertIn(sentinel, texts[0])
        for later in texts[1:]:
            self.assertNotIn(sentinel, later)
            self.assertNotIn("Task:\n", later)
        # fixed boilerplate ceiling (paths excluded); the previous builder produced ~1,600-1,700 characters
        review_root, project_root = self.config["review_root"], self.config["project_root"]
        state = self.state()
        for kind, cont in (("continuation:revision", {"kind": "revision", "gate_type": "plan_approval", "note": ""}), ("continuation:answer", {"kind": "answer", "gate_id": "g", "note": ""}),
                           ("continuation:plan_approved", {"kind": "plan_approved", "plan_sha256": "p" * 64, "payload_sha256": "q" * 64}), ("handoff", None)):
            state["continuation"] = cont
            prompt = self.sup.build_prompt_v2(state, "0bbbbbbb-cccc-4ddd-8eee-ffffffffffff", kind)
            fixed = len(prompt) - len(review_root) - len(project_root)
            self.assertLessEqual(fixed, 1300, (kind, fixed))  # previous builder: 1,419 (continuation) and 1,718 (handoff) with the same inputs
            self.assertEqual(hs.find_protocol_blocks(prompt), [], f"{kind} prompt must not parse as a response")
            for width in (40, 60, 80):
                self.assertEqual(hs.find_protocol_blocks(ProtocolWidthTests.wrap(prompt.splitlines(), width)), [], (kind, width))
        for required in ("HERDR_PROTOCOL=2", f"HERDR_RUN={state['run_id']}", "HERDR_TURN=", "HERDR_STAGE=", "HERDR_NEXT=", "HERDR_GATE=", "HERDR_PAYLOAD=", "HERDR_HANDOFF=", "CODEX_PLAN.md", "push"):
            self.assertIn(required, prompt)

    def to_missing_result(self, *, after_approval: bool = False) -> dict:
        self.write_plan()
        silent = {"output": "The plan is at reviews/feature-widget but I forgot the block.\n", "status": "idle"}
        if after_approval:
            self.start_gated([{"v2": ("plan", "human", "plan_approval", str(self.plan_payload(runtime_validation_required=False, rebuild_required=False)))}])
            self.approve_pending()
            self.assertEqual(self.resume_with([silent]), 2)
        else:
            self.assertEqual(self.start_gated([silent]), 2)
        state = self.state()
        self.assertEqual((state["supervisor_state"], state["wait_user_requires_action"]), ("WAIT_USER", True))
        self.assertEqual(state["missing_result"]["turn_id"], state["delivery"]["turn_id"])
        self.assertEqual(state["missing_result"]["attempts"], 0)
        self.assertNotEqual((state["pending_gate"] or {}).get("status"), "pending")
        return state

    def retry(self, **overrides) -> dict:
        with self.sup.store.transaction():
            st = self.sup.store.read_state()
            command = {"action": "retry_routing_result", "run_id": st["run_id"], "turn_id": st["missing_result"]["turn_id"] if st.get("missing_result") else None, **overrides}
            return self.sup.apply_command(st, command)

    def test_failed_retry_is_spent_and_never_repeats(self) -> None:
        state = self.to_missing_result()
        before = self.counters()
        result = self.retry()
        self.assertFalse(result["ok"])
        self.assertIn("no approval was created", result["message"])
        after = self.state()
        self.assertEqual((after["missing_result"]["attempts"], after["supervisor_state"], after["wait_user_requires_action"]), (1, "WAIT_USER", True))
        self.assertEqual(len(self.herdr.prompts), before[0])
        self.assertEqual([src for _, src in self.herdr.reads][-1], "recent-unwrapped")
        # the one-time reread is spent: later commands are refused without mutation, even if the transcript now parses
        self.herdr.outputs["codex-main"] = FakeHerdr.block_v2(state["run_id"], state["delivery"]["turn_id"], "plan", "human", "plan_approval", str(self.plan_payload()))
        snapshot = self.paths.state_file.read_text()
        reads = len(self.herdr.reads)
        with self.assertRaises(hs.SupervisorError) as caught:
            self.retry()
        self.assertIn("already used", str(caught.exception))
        self.assertEqual(self.paths.state_file.read_text(), snapshot)
        self.assertEqual(len(self.herdr.reads), reads, "a spent retry does not even read")
        self.assertEqual(self.make_supervisor().worker(), 2)
        self.assertEqual(self.state()["missing_result"]["attempts"], 1, "restart preserves the spent descriptor")
        report = self.sup.status()
        self.assertEqual(report["missing_result"]["attempts"], 1)
        self.assertNotIn("Retry routing result", [b[0] for row in __import__("herdr_present").keyboard_for_status(report) for b in row])
        # guidance remains the recovery
        with self.sup.store.transaction():
            st = self.sup.store.read_state()
            self.sup.guide(st, run_id=st["run_id"], actor="cli", note="emit the block")
        self.assertIsNone(self.state()["missing_result"])

    def test_retry_rereads_settled_turn_and_creates_the_gate_once_without_prompts(self) -> None:
        state = self.to_missing_result()
        before = self.counters()
        # the transcript now shows the wrapped result (as the live pane did): the single retry recovers the gate
        payload = str(self.plan_payload())
        wrapped = ProtocolWidthTests.wrap(FakeHerdr.block_v2(state["run_id"], state["delivery"]["turn_id"], "plan", "human", "plan_approval", payload, prefix="").splitlines(), 60)
        self.herdr.outputs["codex-main"] = "reply text\n\n" + wrapped
        result = self.retry()
        self.assertTrue(result["ok"], result)
        after = self.state()
        self.assertEqual(after["supervisor_state"], "WAIT_PLAN_APPROVAL")
        self.assertEqual(after["pending_gate"]["gate_type"], "plan_approval")
        self.assertIsNone(after["missing_result"])
        self.assertEqual(self.event_types().count("PLAN_APPROVAL_REQUIRED"), 1)
        self.assertEqual(len(self.herdr.prompts), before[0], "retry submits nothing")
        self.assertEqual(len(self.herdr.waits), before[2], "retry wakes nothing")
        # duplicate retry after recovery, restart, and resume: no second gate, no prompt
        with self.assertRaises(hs.SupervisorError):
            self.retry(turn_id=state["delivery"]["turn_id"])
        self.assertEqual(self.make_supervisor().worker(), 4)
        self.assertEqual(self.event_types().count("PLAN_APPROVAL_REQUIRED"), 1)
        self.assertEqual(len(self.herdr.prompts), before[0])

    def test_retry_is_zero_prompt_for_every_recovered_route_across_the_whole_worker_call(self) -> None:
        """A recovered human gate is created; a recovered provider or done route is recorded but the retry
        worker never prepares, submits, reads more than the settled transcript, or waits for an agent."""
        cases = {
            "human": ("plan", "human", "plan_approval", None, "WAIT_PLAN_APPROVAL", False),
            "codex": ("brief", "codex", "none", "-", "WAIT_USER", True),
            "claude": ("brief", "claude", "none", "-", "WAIT_USER", True),
            "done": ("plan", "done", "none", "-", "WAIT_USER", False),
        }
        for route, (stage, nxt, gate, payload, expected_state, after_approval) in cases.items():
            with self.subTest(route=route):
                self.reset_fixture()
                state = self.to_missing_result(after_approval=after_approval)
                turn = state["delivery"]["turn_id"]
                payload_path = str(self.plan_payload()) if payload is None else payload
                self.herdr.outputs["codex-main"] = FakeHerdr.block_v2(state["run_id"], turn, stage, nxt, gate, payload_path)
                self.sup.enqueue_command({"request_id": f"retry-{route}-0001", "action": "retry_routing_result", "run_id": state["run_id"], "turn_id": turn, "actor": "telegram:1"})
                prompts, reads, waits = self.counters()
                self.herdr.responses = []  # any prompt would raise inside the fake
                exit_code = self.sup.worker()
                after = self.state()
                self.assertEqual(after["supervisor_state"], expected_state, route)
                self.assertEqual(len(self.herdr.prompts), prompts, f"{route}: retry worker must not prepare or submit a prompt")
                self.assertEqual(len(self.herdr.waits), waits, f"{route}: retry worker must not wait on an agent")
                self.assertEqual(len(self.herdr.reads), reads + 1, f"{route}: exactly one settled transcript read")
                self.assertEqual(after["prompt_metrics"]["prompts"], 2 if after_approval else 1, route)
                self.assertIsNone(after["missing_result"], route)
                result = [e for e in self.events() if e["type"] == "COMMAND_RESULT" and e["data"]["action"] == "retry_routing_result"][-1]["data"]
                self.assertTrue(result["ok"], result)
                if route == "human":
                    self.assertEqual(exit_code, 4)
                    self.assertEqual(after["pending_gate"]["gate_type"], "plan_approval")
                elif route == "done":
                    self.assertEqual(exit_code, 2)
                    self.assertIn("done is not allowed before a plan approval", after["wait_user_reason"])
                else:
                    self.assertEqual(exit_code, 2)
                    self.assertFalse(after["wait_user_requires_action"], "a recovered handoff waits for a plain /resume")
                    self.assertEqual(after["active_agent"], route)
                    self.assertIsNone(after["delivery"])
                    self.assertIn("/resume delivers that turn", after["wait_user_reason"])
                    self.assertIn("nothing was sent", result["message"])
                    # a restart still does not deliver it; an explicit resume does, exactly once
                    self.assertEqual(self.make_supervisor().worker(), 2)
                    self.assertEqual(len(self.herdr.prompts), prompts)
                    self.herdr.responses = [{"v2": ("next", "human", "generic_question", str(self.question_payload()))}]
                    self.assertEqual(self.sup.resume(), 2)  # the delivered turn ends at a question gate (WAIT_USER)
                    self.assertEqual(len(self.herdr.prompts), prompts + 1)
                    self.assertEqual(self.herdr.prompts[-1][0], f"{route}-main")
                    self.assertEqual(self.state()["pending_gate"]["gate_type"], "generic_question")

    def test_retry_is_bound_and_refuses_unsafe_states(self) -> None:
        state = self.to_missing_result()
        for label, overrides in (("wrong run", {"run_id": "0bbbbbbb-cccc-4ddd-8eee-ffffffffffff"}), ("wrong turn", {"turn_id": "0bbbbbbb-cccc-4ddd-8eee-ffffffffffff"}), ("no turn", {"turn_id": None})):
            with self.subTest(label):
                with self.assertRaises(hs.SupervisorError):
                    self.retry(**overrides)
        self.herdr.agents["codex-main"]["agent_status"] = "working"
        with self.assertRaises(hs.SupervisorError):
            self.retry()
        self.herdr.agents["codex-main"]["agent_status"] = "idle"
        # bridge-side supervisor without Herdr cannot read
        bridge_side = hs.Supervisor(self.paths, self.config, herdr=None, clock=self.clock.time)
        with self.assertRaises(hs.SupervisorError):
            with bridge_side.store.transaction():
                bridge_side.apply_command(bridge_side.store.read_state(), {"action": "retry_routing_result", "run_id": state["run_id"], "turn_id": state["delivery"]["turn_id"]})
        self.assertEqual(self.state()["missing_result"]["attempts"], 0)
        # guidance clears the descriptor; a later retry has nothing to do
        with self.sup.store.transaction():
            st = self.sup.store.read_state()
            self.sup.guide(st, run_id=st["run_id"], actor="cli", note="add the block")
        self.assertIsNone(self.state()["missing_result"])
        with self.assertRaises(hs.SupervisorError):
            self.retry(turn_id=state["delivery"]["turn_id"])
        self.assertEqual(len(self.herdr.prompts), 1)

    def test_retry_via_inbox_is_idempotent_per_request(self) -> None:
        state = self.to_missing_result()
        payload = str(self.plan_payload())
        self.herdr.outputs["codex-main"] = FakeHerdr.block_v2(state["run_id"], state["delivery"]["turn_id"], "plan", "human", "plan_approval", payload)
        command = {"request_id": "retry-request-0001", "action": "retry_routing_result", "run_id": state["run_id"], "turn_id": state["delivery"]["turn_id"], "actor": "telegram:1", "chat_id": 9}
        self.sup.enqueue_command(command)
        self.assertEqual(self.sup.worker(), 4)
        self.assertEqual(self.state()["supervisor_state"], "WAIT_PLAN_APPROVAL")
        self.sup.enqueue_command(command)  # replay of the same request id
        self.sup.enqueue_command({**command, "request_id": "retry-request-0002"})  # a fresh duplicate
        self.assertEqual(self.sup.worker(), 4)
        self.assertEqual(self.event_types().count("PLAN_APPROVAL_REQUIRED"), 1)
        self.assertEqual(len(self.herdr.prompts), 1)
        results = [e for e in self.events() if e["type"] == "COMMAND_RESULT" and e["data"]["action"] == "retry_routing_result"]
        self.assertTrue(results[0]["data"]["ok"])
        self.assertFalse(results[-1]["data"]["ok"])
