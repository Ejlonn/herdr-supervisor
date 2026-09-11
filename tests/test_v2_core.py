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
        self.assertEqual(after["runtime_policy"], {"runtime_validation_required": True, "rebuild_required": True, "rebuild_reason": "backend image contains the source", "push_approval_required": True, "migration_required": False})
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
        self.assertIn("APPROVED the plan with SHA-256", self.herdr.prompts[1][1])
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
        with self.sup.store.transaction():
            st = self.sup.store.read_state()
            return self.sup.record_runtime_evidence(st, run_id=st["run_id"], candidate_sha=sha, environment=environment, evidence_file=str(evidence), result=result, head_resolver=self.head_resolver)

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
        gate = state["pending_gate"]
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

    def test_recovery_finds_exact_session_under_new_alias_and_pane(self) -> None:
        self.write_plan()
        self.start_gated([{"v2": ("plan", "human", "plan_approval", str(self.plan_payload()))}])
        self.approve_pending()
        # After a reboot the same native session lives under another alias/pane; alias 'codex-main' is gone.
        moved = FakeHerdr.agent("codex", "codex-2", "w7:p3", CODEX_SESSION)
        del self.herdr.agents["codex-main"]
        self.herdr.agents["codex-2"] = moved
        self.assertEqual(self.resume_with([{"v2": ("brief", "human", "generic_question", str(self.question_payload()))}]), 2)
        self.assertEqual(self.herdr.prompts[-1][0], "codex-2", "prompted the exact session under its live alias")
        self.assertEqual(self.herdr.starts, [])
        owners = json.loads(self.paths.owners_file.read_text())
        self.assertEqual(owners["codex"], {"pane_id": "w7:p3", "session_id": CODEX_SESSION}, "only the locator changed")

    def test_recovery_alias_reused_by_other_session_is_not_trusted(self) -> None:
        self.write_plan()
        self.start_gated([{"v2": ("plan", "human", "plan_approval", str(self.plan_payload()))}])
        self.approve_pending()
        self.herdr.agents["codex-main"] = FakeHerdr.agent("codex", "codex-main", "w3:p2", "another-session")
        self.assertEqual(self.resume_with([]), 2)
        self.assertEqual(self.herdr.prompts[-1][0], "codex-main")  # only the original planning prompt exists
        self.assertEqual(len(self.herdr.prompts), 1)
        self.assertIn("refusing", self.state()["wait_user_reason"])
        # exact session live under another alias while the alias is reused: the exact session wins
        self.herdr.agents["codex-real"] = FakeHerdr.agent("codex", "codex-real", "w8:p1", CODEX_SESSION)
        self.assertEqual(self.resume_with([{"v2": ("brief", "human", "generic_question", str(self.question_payload()))}]), 2)
        self.assertEqual(self.herdr.prompts[-1][0], "codex-real")

    def test_recovery_missing_pane_creates_nonfocused_workspace_and_verifies_id(self) -> None:
        self.write_plan()
        self.start_gated([{"v2": ("plan", "human", "plan_approval", str(self.plan_payload()))}])
        self.approve_pending()
        del self.herdr.agents["codex-main"]
        self.herdr.available_panes.discard("w3:p2")
        self.assertEqual(self.resume_with([{"v2": ("brief", "human", "generic_question", str(self.question_payload()))}]), 2)
        self.assertEqual(self.herdr.workspaces, [("herdr-supervisor codex recovery", str(self.project_root))])
        self.assertEqual(self.herdr.starts, [("codex-main", "codex", "w9:p1", ["resume", CODEX_SESSION])])
        self.assertEqual(json.loads(self.paths.owners_file.read_text())["codex"]["pane_id"], "w9:p1")

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
        self.write_plan()
        self.herdr.responses = [{"v2": ("plan", "codex")}, {"v2": ("plan", "codex")}]
        self.assertEqual(self.sup.run_new("task", "codex", workflow_policy="gated_v2"), 2)
        state = self.state()
        self.assertEqual(state["supervisor_state"], "WAIT_USER")
        self.assertTrue(state["wait_user_requires_action"])
        self.assertIn("without changing stage", state["wait_user_reason"])
        self.assertEqual(len(self.herdr.prompts), 2)
        self.assertEqual(state["delivery"]["outcome"], "rejected_by_policy")

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
            with self.sup.store.transaction():
                st = self.sup.store.read_state()
                self.sup.record_runtime_evidence(st, run_id=st["run_id"], candidate_sha=SHA_A, environment="TEST", evidence_file=str(self.evidence_file()), result="PASS", head_resolver=self.head_resolver)
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
        with self.sup.store.transaction():
            st = self.sup.store.read_state()
            self.sup.record_runtime_evidence(st, run_id=st["run_id"], candidate_sha=SHA_A, environment="TEST", evidence_file=str(self.evidence_file()), result="PASS", head_resolver=self.head_resolver)
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
        with self.sup.store.transaction():
            st = self.sup.store.read_state()
            self.sup.record_runtime_evidence(st, run_id=st["run_id"], candidate_sha=SHA_A, environment="TEST", evidence_file=str(self.evidence_file()), result="PASS", head_resolver=self.head_resolver)
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
