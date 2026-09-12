"""Gate-preserving agent follow-up ("Ask agent"): durable state, staleness, one-shot delivery, the
follow-up frame parser, crash windows, failure recovery, session preservation, and revision semantics.
Fakes and fixtures only: no live agent, reset, service, commit, or push."""

# ruff: noqa: I001  (v2_fixtures must be imported first: it puts src/ on sys.path and this module sorts first in discovery)
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from v2_fixtures import CLAUDE_SESSION, CODEX_SESSION, NOW, FakeHerdr, V2Case, hs  # noqa: E402

import herdr_artifacts as ha  # noqa: E402
import herdr_protocol as hpr  # noqa: E402
import herdr_redaction as hred  # noqa: E402
import herdr_validation as hval  # noqa: E402
from test_v2_core import ProtocolWidthTests  # noqa: E402

RUN, TURN, DECISION = "20598ca2-d049-4cb9-967a-5652e7bf6eff", "9945228c-bb87-493a-96ea-14d190c49510", "b2d628ac-8558-433b-a284-0cb520aa971b"
SHA = "c" * 64


def frame(run: str = RUN, turn: str = TURN, decision: str = DECISION, path: str = "/reviews/x/AGENT_FOLLOWUP_99452281.md", sha: str = SHA, summary: str = "answered_no_change_needed", prefix: str = "") -> str:
    return FakeHerdr.followup_frame(run, turn, decision, path, sha, summary=summary, prefix=prefix)


class FollowupFrameParserTests(V2Case):
    def test_frame_is_parsed_exactly_and_bound_to_run_and_turn(self) -> None:
        got = hpr.parse_followup("prose\n" + frame() + "\n", RUN, TURN)
        self.assertEqual((got.run_id, got.turn_id, got.decision_id, got.response_path, got.response_sha256, got.summary), (RUN, TURN, DECISION, "/reviews/x/AGENT_FOLLOWUP_99452281.md", SHA, "answered_no_change_needed"))
        self.assertIsNone(hpr.parse_followup(frame(), RUN, DECISION), "another turn's frame is ignored")
        self.assertIsNone(hpr.parse_followup(frame(), DECISION, TURN), "another run's frame is ignored")
        for width in (40, 60, 80):
            wrapped = ProtocolWidthTests.wrap(("reply\n" + frame(prefix="  ") + "\n").splitlines(), width)
            self.assertEqual(hpr.parse_followup(wrapped, RUN, TURN), got, width)

    def test_adversarial_frames_are_rejected(self) -> None:
        cases = {
            "placeholder sha": frame(sha="<sha256 of the exact file bytes>"),
            "placeholder summary": frame(summary="<one line>"),
            "relative path": frame(path="reviews/x/answer.md"),
            "short sha": frame(sha="c" * 63),
            "uppercase sha": frame(sha="C" * 64),
            "non-uuid decision": frame(decision="not-a-uuid"),
            "empty summary": frame(summary=""),
            "summary too long": frame(summary="x" * 301),
            "version 2": frame().replace("HERDR_FOLLOWUP=1", "HERDR_FOLLOWUP=2"),
            "reordered keys": "\n".join(reversed(frame().splitlines())),
            "missing key": "\n".join(frame().splitlines()[:-1]),
        }
        for label, text in cases.items():
            with self.subTest(label):
                self.assertIsNone(hpr.parse_followup(text, RUN, TURN))
        self.assertEqual(hpr.find_followup_frames(frame(path="/x/y.md\x00")), [])

    def test_duplicate_equal_frames_pass_and_conflicting_frames_raise(self) -> None:
        self.assertIsNotNone(hpr.parse_followup(frame() + "\n\n" + frame(), RUN, TURN))
        with self.assertRaises(hs.SupervisorError):
            hpr.parse_followup(frame() + "\n\n" + frame(sha="d" * 64), RUN, TURN)

    def test_the_two_parsers_never_accept_each_others_frames(self) -> None:
        routing = FakeHerdr.block_v2(RUN, TURN, "plan", "human", "plan_approval", "/reviews/x/p.json", prefix="")
        self.assertEqual(hpr.find_followup_frames(routing), [], "the follow-up parser ignores a routing block")
        self.assertIsNone(hpr.parse_followup(routing, RUN, TURN))
        self.assertEqual(hs.find_protocol_blocks(frame()), [], "the workflow router ignores a follow-up frame")
        self.assertIsNone(hs.parse_protocol(frame(), RUN, TURN))
        # a transcript containing both: each parser sees only its own
        both = routing + "\n\n" + frame()
        self.assertEqual(hs.parse_protocol(both, RUN, TURN).next_agent, "human")
        self.assertEqual(hpr.parse_followup(both, RUN, TURN).decision_id, DECISION)


class FollowupCase(V2Case):
    """Drive a gated run to a suspendable decision, then exercise Ask agent through the durable inbox."""

    def counters(self) -> tuple[int, int, int]:
        return len(self.herdr.prompts), len(self.herdr.reads), len(self.herdr.waits)

    def snapshot(self, state: dict) -> dict:
        keys = ("pending_gate", "approvals", "gate_history", "gate_sequence", "approved_plan", "runtime_policy", "candidate_sha", "runtime_evidence",
                "push_approval", "phase", "task_text", "native_sessions", "missing_result", "operator_handoff_ready", "consecutive_auto_turns",
                "supervisor_state", "active_agent", "wait_user_reason", "wait_user_requires_action", "delivery", "turns_completed", "continuation")
        return {k: json.loads(json.dumps(state.get(k))) for k in keys}

    def to_plan_gate(self) -> dict:
        self.write_plan()
        self.assertEqual(self.start_gated([{"v2": ("plan", "human", "plan_approval", str(self.plan_payload()))}]), 4)
        return self.state()

    def to_question_gate(self) -> dict:
        self.write_plan()
        self.assertEqual(self.start_gated([{"v2": ("plan", "human", "generic_question", str(self.question_payload()))}]), 2)
        return self.state()

    def to_runtime_gate(self) -> dict:
        self.write_plan()
        self.start_gated([{"v2": ("plan", "human", "plan_approval", str(self.plan_payload()))}])
        self.approve_pending()
        self.resume_with([{"v2": ("brief", "claude")}, {"v2": ("implement", "codex")}, {"v2": ("review", "human", "runtime_validation", str(self.runtime_payload()))}])
        state = self.state()
        self.assertEqual(state["supervisor_state"], "WAIT_RUNTIME_VALIDATION")
        return state

    def to_push_gate(self) -> dict:
        self.to_runtime_gate()
        self.record_runtime("PASS")
        state = self.state()
        self.assertEqual(state["supervisor_state"], "WAIT_PUSH_APPROVAL")
        return state

    def to_missing_result(self) -> dict:
        self.write_plan()
        self.assertEqual(self.start_gated([{"output": "I explained things in prose and forgot the block.\n", "status": "idle"}]), 2)
        state = self.state()
        self.assertIsNotNone(state["missing_result"])
        return state

    def ask(self, question: str = "Why is the rebuild required?", *, request_id: str = "ask-0001", **overrides) -> Path:
        st = self.state()
        decision = self.sup.followup_decision(st)
        command = {"request_id": request_id, "action": "ask_agent", "run_id": st["run_id"], "question": question, "actor": "telegram:1", "chat_id": 9,
                   "decision_id": decision["decision_id"] if decision else None, "expected_state": st["supervisor_state"]}
        if decision and decision["kind"] == "gate":
            command.update(artifact_sha256=decision["artifact_sha256"], payload_sha256=decision["payload_sha256"])
        command.update(overrides)
        path = self.sup.enqueue_command(command)
        self.sup.process_inbox()  # applied under the transaction lock exactly as the worker would, before any delivery
        return path

    def command_results(self, action: str) -> list[dict]:
        return [e["data"] for e in self.events() if e["type"] == "COMMAND_RESULT" and e["data"]["action"] == action]

    def apply_direct(self, command: dict) -> dict:
        with self.sup.store.transaction():
            st = self.sup.store.read_state()
            return self.sup.apply_command(st, command)


class FollowupGateTests(FollowupCase):
    def test_ask_at_every_gate_preserves_the_exact_gate_and_delivers_one_verified_answer(self) -> None:
        for kind, reach, exit_code in (("plan_approval", self.to_plan_gate, 4), ("generic_question", self.to_question_gate, 2), ("runtime_validation", self.to_runtime_gate, 4), ("push_approval", self.to_push_gate, 4)):
            with self.subTest(kind):
                self.reset_fixture()
                before = reach()
                self.assertEqual(before["pending_gate"]["gate_type"], kind)
                frozen = self.snapshot(before)
                provider = before["pending_gate"]["agent"]
                self.ask()
                prompts, reads, waits = self.counters()
                self.herdr.responses = [{"followup": {"summary": "rebuild_needed_because_the_image_copies_source"}}]
                self.assertEqual(self.sup.worker(), exit_code)
                after = self.state()
                self.assertEqual(self.snapshot(after), frozen, "the suspended decision is restored byte-for-byte")
                self.assertEqual(after["pending_gate"]["status"], "pending")
                followup = after["agent_followup"]
                self.assertEqual((followup["status"], followup["provider"], followup["decision"]["kind"], followup["decision"]["decision_id"]), ("RESTORED", provider, "gate", before["pending_gate"]["gate_id"]))
                self.assertEqual(followup["session_id"], after["native_sessions"][provider])
                self.assertEqual(len(self.herdr.prompts), prompts + 1, "exactly one follow-up prompt")
                name, prompt = self.herdr.prompts[-1]
                self.assertEqual(name, f"{provider}-main")
                self.assertNotIn("Task:\n", prompt, "the task body is never replayed")
                self.assertNotIn("Add the widget", prompt)
                self.assertNotIn("HERDR_PROTOCOL", prompt)
                self.assertIn(before["pending_gate"]["gate_id"], prompt)
                self.assertIn(followup["response_path"], prompt)
                self.assertEqual(hpr.find_followup_frames(prompt), [], "the prompt never parses as a response")
                self.assertEqual(hs.find_protocol_blocks(prompt), [])
                self.assertEqual(self.event_types().count("AGENT_FOLLOWUP_READY"), 1)
                event = [e for e in self.events() if e["type"] == "AGENT_FOLLOWUP_READY"][-1]
                self.assertEqual((event["data"]["summary"], event["data"]["decision_id"], event["data"]["restored_state"], event["data"]["gate_type"]), ("rebuild_needed_because_the_image_copies_source", before["pending_gate"]["gate_id"], before["supervisor_state"], kind))
                self.assertFalse(event["actionable"])
                record = ha.load_record(self.paths, event["data"]["artifact_id"])
                self.assertEqual((record["category"], record["event_id"], record["source_sha256"]), ("followup", followup["followup_turn_id"], followup["response"]["sha256"]))
                self.assertTrue(Path(followup["response_path"]).is_file())
                self.assertEqual(after["prompt_metrics"]["by_provider"][provider]["prompts"], before["prompt_metrics"]["by_provider"][provider]["prompts"] + 1, "visible in prompt metrics")
                # the restored gate still works exactly as before: hashes bound, approval history intact, no duplicate gate event
                self.assertEqual(len(after["approvals"]), len(before["approvals"]))
                self.assertEqual(self.event_types().count({"plan_approval": "PLAN_APPROVAL_REQUIRED", "generic_question": "QUESTION_ASKED", "runtime_validation": "RUNTIME_VALIDATION_READY", "push_approval": "PUSH_APPROVAL_REQUIRED"}[kind]), 1)
                # a restart after restoration changes nothing and sends nothing
                self.herdr.responses = []
                self.assertEqual(self.make_supervisor().worker(), exit_code)
                self.assertEqual(self.snapshot(self.state()), frozen)
                self.assertEqual(len(self.herdr.prompts), prompts + 1)
                self.assertEqual(self.event_types().count("AGENT_FOLLOWUP_READY"), 1)

    def test_answer_cannot_approve_revise_advance_or_complete(self) -> None:
        before = self.to_plan_gate()
        self.ask()
        # the agent tries to smuggle a routing block and an approval into the follow-up turn
        original = FakeHerdr.followup_reply

        def sneaky(prompt: str, spec: dict) -> str:
            run, turn = FakeHerdr.ids(prompt)
            return original(prompt, spec) + "\n" + FakeHerdr.block_v2(run, turn, "plan", "done") + "\n" + FakeHerdr.block_v2(run, before["pending_gate"]["turn_id"], "plan", "claude")

        FakeHerdr.followup_reply = classmethod(lambda cls, prompt, spec: sneaky(prompt, spec))  # type: ignore[method-assign]
        try:
            self.herdr.responses = [{"followup": {"summary": "apply_the_fix_now"}}]
            self.assertEqual(self.sup.worker(), 4)
        finally:
            FakeHerdr.followup_reply = original  # type: ignore[method-assign]
        after = self.state()
        self.assertEqual(after["supervisor_state"], "WAIT_PLAN_APPROVAL")
        self.assertEqual(after["pending_gate"]["status"], "pending")
        self.assertEqual(after["approvals"], before["approvals"])
        self.assertEqual(after["phase"], before["phase"])
        self.assertEqual(after["turns_completed"], before["turns_completed"])
        self.assertNotIn("TASK_DONE", self.event_types())
        self.assertEqual(after["agent_followup"]["status"], "RESTORED")

    def test_revision_after_a_follow_up_still_voids_the_gate_and_needs_a_new_block(self) -> None:
        before = self.to_plan_gate()
        self.ask()
        self.herdr.responses = [{"followup": {"summary": "recommend_changing_scope"}}]
        self.assertEqual(self.sup.worker(), 4)
        with self.sup.store.transaction():
            st = self.sup.store.read_state()
            result = self.sup.revise_gate(st, run_id=st["run_id"], gate_id=before["pending_gate"]["gate_id"], actor="cli", note="apply the recommendation")
        self.assertTrue(result["ok"])
        state = self.state()
        self.assertEqual(state["pending_gate"]["status"], "revision_requested")
        self.assertEqual(state["continuation"]["kind"], "revision")
        self.assertEqual(state["supervisor_state"], "RUNNING")
        # only a new routing block creates the successor gate; the follow-up answer never did
        self.herdr.responses = [{"v2": ("plan", "human", "plan_approval", str(self.plan_payload()))}]
        self.assertEqual(self.sup.resume(), 4)
        self.assertEqual(self.state()["pending_gate"]["sequence"], before["pending_gate"]["sequence"] + 1)
        self.assertIn("apply the recommendation", self.herdr.prompts[-1][1])

    def test_stale_or_unbound_ask_commands_are_rejected_before_any_prompt(self) -> None:
        before = self.to_plan_gate()
        other = "0bbbbbbb-cccc-4ddd-8eee-ffffffffffff"
        cases = {
            "wrong run": {"run_id": other}, "wrong decision": {"decision_id": other}, "wrong state": {"expected_state": "WAIT_USER"},
            "wrong artifact hash": {"artifact_sha256": "e" * 64}, "wrong payload hash": {"payload_sha256": "e" * 64},
            "empty question": {"question": "   "}, "long question": {"question": "q" * 1001},
        }
        for label, overrides in cases.items():
            with self.subTest(label):
                with self.assertRaises(hs.SupervisorError):
                    self.apply_direct({"action": "ask_agent", "run_id": before["run_id"], "question": "why?", "actor": "cli", "chat_id": 9, "decision_id": before["pending_gate"]["gate_id"],
                                       "expected_state": "WAIT_PLAN_APPROVAL", "artifact_sha256": before["pending_gate"]["artifact_sha256"], "payload_sha256": before["pending_gate"]["payload_sha256"], **overrides})
                self.assertIsNone(self.state()["agent_followup"])
        # the artifact changed on disk after the card was shown
        self.write_plan("# CODEX_PLAN\n\nchanged\n")
        with self.assertRaises(hs.SupervisorError):
            self.apply_direct({"action": "ask_agent", "run_id": before["run_id"], "question": "why?", "actor": "cli", "decision_id": before["pending_gate"]["gate_id"], "artifact_sha256": before["pending_gate"]["artifact_sha256"]})
        self.write_plan()
        # the decision changed while the command sat in the inbox: rejected at apply time, nothing prepared
        st = self.state()
        self.sup.enqueue_command({"request_id": "ask-late", "action": "ask_agent", "run_id": st["run_id"], "question": "late?", "actor": "telegram:1", "chat_id": 9, "decision_id": st["pending_gate"]["gate_id"], "expected_state": "WAIT_PLAN_APPROVAL"})
        self.approve_pending()
        self.herdr.responses = []
        with self.assertRaises(AssertionError):  # a prompt for the continuation would be attempted; the fake refuses
            self.sup.worker()
        result = self.command_results("ask_agent")[-1]
        self.assertFalse(result["ok"])
        self.assertIn("ask-agent refused", result["message"])
        self.assertIsNone(self.state()["agent_followup"])

    def test_second_follow_up_and_gate_actions_are_refused_while_one_is_unresolved(self) -> None:
        before = self.to_plan_gate()
        self.ask()
        state = self.state()
        self.assertEqual((state["agent_followup"]["status"], state["supervisor_state"]), ("PREPARED", "RUNNING"))
        with self.assertRaises(hs.SupervisorError) as caught:
            self.apply_direct({"action": "ask_agent", "run_id": state["run_id"], "question": "another?", "actor": "cli"})
        self.assertIn("already in flight", str(caught.exception))
        for action in ("approve", "reject", "revise", "answer"):
            with self.subTest(action):
                with self.assertRaises(hs.SupervisorError):
                    self.apply_direct({"action": action, "run_id": state["run_id"], "gate_id": before["pending_gate"]["gate_id"], "actor": "cli", "note": "n", "answer": "enum"})
        self.assertEqual(self.state()["pending_gate"]["status"], "pending")
        eligible, reason = self.sup.operator_handoff_eligibility(self.state(), inspect_agents=False)
        self.assertFalse(eligible)
        # duplicate request ids replay the recorded result; a fresh duplicate is refused; still one prompt in the end
        self.ask(request_id="ask-0001")
        self.ask(request_id="ask-0002")
        self.herdr.responses = [{"followup": {}}]
        self.assertEqual(self.sup.worker(), 4)
        results = self.command_results("ask_agent")
        self.assertEqual([r["ok"] for r in results], [True, False])
        self.assertEqual(len(self.herdr.prompts), 2)
        self.assertEqual(self.event_types().count("AGENT_FOLLOWUP_READY"), 1)


class FollowupMissingResultTests(FollowupCase):
    def test_ask_preserves_the_unread_turn_and_retry_count_and_returns_to_the_same_recovery(self) -> None:
        before = self.to_missing_result()
        frozen = self.snapshot(before)
        self.assertIsNone(self.sup.followup_decision(before).get("gate_type"))
        self.ask("What did you decide?")
        state = self.state()
        self.assertEqual((state["missing_result"], state["delivery"], state["supervisor_state"]), (None, None, "RUNNING"))
        self.assertEqual(state["agent_followup"]["suspended"]["missing_result"], before["missing_result"])
        self.assertEqual(state["agent_followup"]["suspended"]["delivery"], before["delivery"])
        self.assertEqual(state["agent_followup"]["response_path"], str(Path(self.config["review_root"]) / f"run-{state['run_id'][:8]}" / f"AGENT_FOLLOWUP_{state['agent_followup']['followup_turn_id'][:8]}.md"))
        # the routing retry is refused while the question is out (never silently applied to the wrong turn)
        with self.assertRaises(hs.SupervisorError):
            self.apply_direct({"action": "retry_routing_result", "run_id": state["run_id"], "turn_id": before["missing_result"]["turn_id"]})
        self.herdr.responses = [{"followup": {"summary": "I_recommended_approving_the_plan"}}]
        self.assertEqual(self.sup.worker(), 2)
        after = self.state()
        self.assertEqual(self.snapshot(after), frozen)
        self.assertEqual(after["missing_result"]["attempts"], 0)
        self.assertEqual(after["agent_followup"]["status"], "RESTORED")
        # the original recovery still works afterwards: one reread recovers the gate with zero prompts
        payload = str(self.plan_payload())
        self.herdr.outputs["codex-main"] = FakeHerdr.block_v2(after["run_id"], before["delivery"]["turn_id"], "plan", "human", "plan_approval", payload)
        prompts = len(self.herdr.prompts)
        result = self.apply_direct({"action": "retry_routing_result", "run_id": after["run_id"], "turn_id": before["missing_result"]["turn_id"]})
        self.assertTrue(result["ok"])
        self.assertEqual(self.state()["supervisor_state"], "WAIT_PLAN_APPROVAL")
        self.assertEqual(len(self.herdr.prompts), prompts)

    def test_guidance_after_a_follow_up_still_requests_a_replacement_result(self) -> None:
        self.to_missing_result()
        self.ask()
        self.herdr.responses = [{"followup": {}}]
        self.assertEqual(self.sup.worker(), 2)
        with self.sup.store.transaction():
            st = self.sup.store.read_state()
            self.sup.guide(st, run_id=st["run_id"], actor="cli", note="emit the block")
        state = self.state()
        self.assertEqual((state["continuation"]["kind"], state["missing_result"], state["supervisor_state"]), ("revision", None, "RUNNING"))


class FollowupCrashAndFailureTests(FollowupCase):
    def test_restart_before_submit_delivers_once_and_reuses_the_same_turn(self) -> None:
        self.to_plan_gate()
        self.ask()
        turn = self.state()["agent_followup"]["followup_turn_id"]
        self.assertIsNone(self.state()["delivery"])
        # the worker that applied the command died before submitting: a fresh worker delivers the same turn once
        self.herdr.responses = [{"followup": {}}]
        self.assertEqual(self.make_supervisor().worker(), 4)
        self.assertEqual(self.state()["agent_followup"]["followup_turn_id"], turn)
        self.assertEqual(len(self.herdr.prompts), 2)
        self.assertEqual(FakeHerdr.ids(self.herdr.prompts[-1][1])[1], turn)

    def test_crash_after_preparation_never_resends_and_settles_into_the_follow_up_wait(self) -> None:
        self.to_plan_gate()
        self.ask()
        turn = self.state()["agent_followup"]["followup_turn_id"]
        # crash window: delivery prepared, prompt possibly sent, no acknowledgement recorded
        with self.sup.store.transaction():
            st = self.sup.store.read_state()
            st["delivery"] = {"turn_id": turn, "agent": "codex", "kind": "followup", "prompt_sha256": "0" * 64, "prompt_chars": 10, "status": "prepared", "prepared_at": hs.iso_utc(NOW)}
            st["agent_followup"]["status"] = "DELIVERING"
            self.sup.store.write_state(st)
        prompts = len(self.herdr.prompts)
        self.herdr.responses = []  # any prompt would raise
        self.assertEqual(self.make_supervisor().worker(), 2)
        state = self.state()
        self.assertEqual(len(self.herdr.prompts), prompts, "no second prompt")
        self.assertEqual((state["supervisor_state"], state["wait_user_requires_action"], state["agent_followup"]["status"], state["agent_followup"]["delivery_uncertain"]), ("WAIT_USER", True, "FAILED", True))
        self.assertIn("may have reached the agent", state["wait_user_reason"])
        self.assertEqual(state["pending_gate"]["status"], "pending", "the gate stays suspended and untouched")
        wait_event = [e for e in self.events() if e["type"] == "WAIT_USER"][-1]
        self.assertEqual(wait_event["data"]["followup"]["status"], "FAILED")
        # ordinary resume cannot bypass the unresolved follow-up
        with self.assertRaises(hs.SupervisorError):
            self.apply_direct({"action": "resume", "run_id": state["run_id"]})
        self.assertEqual(self.make_supervisor().worker(), 2)
        self.assertEqual(self.state()["supervisor_state"], "WAIT_USER")
        # Retry reading answer: once, no prompt; the transcript now carries the answer
        self.herdr.outputs["codex-main"] = "late reply\n" + FakeHerdr.followup_reply(self.sup.build_followup_prompt(state, turn), {})
        reads = len(self.herdr.reads)
        result = self.apply_direct({"action": "retry_followup_response", "run_id": state["run_id"], "followup_turn_id": turn})
        self.assertTrue(result["ok"], result)
        self.assertEqual(len(self.herdr.prompts), prompts)
        self.assertEqual(len(self.herdr.reads), reads + 1)
        after = self.state()
        self.assertEqual((after["supervisor_state"], after["agent_followup"]["status"], after["agent_followup"]["reread_attempts"]), ("WAIT_PLAN_APPROVAL", "RESTORED", 1))
        self.assertEqual(self.event_types().count("AGENT_FOLLOWUP_READY"), 1)

    def test_no_frame_then_reread_fails_once_then_return_to_decision_restores_without_contact(self) -> None:
        before = self.to_plan_gate()
        self.ask()
        self.herdr.responses = [{"output": "I answered in prose only.\n", "status": "idle"}]
        self.assertEqual(self.sup.worker(), 2)
        state = self.state()
        turn = state["agent_followup"]["followup_turn_id"]
        self.assertEqual(state["agent_followup"]["status"], "FAILED")
        prompts, reads, waits = self.counters()
        result = self.apply_direct({"action": "retry_followup_response", "run_id": state["run_id"], "followup_turn_id": turn})
        self.assertFalse(result["ok"])
        self.assertIn("Nothing was resent", result["message"])
        self.assertEqual(self.state()["agent_followup"]["reread_attempts"], 1)
        with self.assertRaises(hs.SupervisorError) as caught:
            self.apply_direct({"action": "retry_followup_response", "run_id": state["run_id"], "followup_turn_id": turn})
        self.assertIn("already used", str(caught.exception))
        for label, overrides in (("wrong run", {"run_id": "0bbbbbbb-cccc-4ddd-8eee-ffffffffffff"}), ("wrong turn", {"followup_turn_id": "0bbbbbbb-cccc-4ddd-8eee-ffffffffffff"})):
            with self.subTest(label):
                with self.assertRaises(hs.SupervisorError):
                    self.apply_direct({"action": "return_to_decision", "run_id": state["run_id"], "followup_turn_id": turn, **overrides})
        result = self.apply_direct({"action": "return_to_decision", "run_id": state["run_id"], "followup_turn_id": turn})
        self.assertTrue(result["ok"])
        after = self.state()
        self.assertEqual(after["agent_followup"]["status"], "ABANDONED")
        self.assertEqual(self.snapshot(after), self.snapshot(before))
        self.assertEqual(self.counters(), (prompts, reads + 1, waits), "return-to-decision reads and sends nothing")
        self.assertEqual(self.event_types().count("AGENT_FOLLOWUP_READY"), 0)
        # the restored gate is fully usable
        self.assertTrue(self.approve_pending()["ok"])

    def test_explicit_revision_from_the_follow_up_wait_voids_the_gate(self) -> None:
        before = self.to_plan_gate()
        self.ask()
        self.herdr.responses = [{"output": "prose\n", "status": "idle"}]
        self.assertEqual(self.sup.worker(), 2)
        result = self.apply_direct({"action": "revise", "run_id": before["run_id"], "gate_id": before["pending_gate"]["gate_id"], "expected_state": "WAIT_PLAN_APPROVAL", "note": "rewrite", "actor": "cli"})
        self.assertTrue(result["ok"])
        state = self.state()
        self.assertEqual((state["agent_followup"]["status"], state["pending_gate"]["status"], state["continuation"]["kind"]), ("ABANDONED", "revision_requested", "revision"))

    def test_verification_failures_fail_closed_without_registering_anything(self) -> None:
        cases = {
            "wrong hash": {"sha": "d" * 64},
            "wrong decision": {"decision": "0bbbbbbb-cccc-4ddd-8eee-ffffffffffff"},
            "missing file": {"skip_file": True},
            "empty file": {"content": "   \n"},
        }
        for label, spec in cases.items():
            with self.subTest(label):
                self.reset_fixture()
                before = self.to_plan_gate()
                self.ask()
                self.herdr.responses = [{"followup": spec}]
                self.assertEqual(self.sup.worker(), 2)
                state = self.state()
                self.assertEqual((state["agent_followup"]["status"], state["agent_followup"]["response"]), ("FAILED", None))
                self.assertEqual([r for r in ha.list_records(self.paths, state["run_id"]) if r["category"] == "followup"], [])
                self.assertEqual(state["pending_gate"], before["pending_gate"])
        # path escape / symlink / oversize / conflicting frames
        for label, mutate in {
            "path outside review root": lambda p, prompt: FakeHerdr.followup_reply(prompt, {"path": str(self.project_root / "outside.md")}),
            "symlink": lambda p, prompt: self._symlinked_reply(prompt),
            "oversized": lambda p, prompt: FakeHerdr.followup_reply(prompt, {"content": "x" * (int(self.config["max_artifact_bytes"]) + 1)}),
            "conflicting frames": lambda p, prompt: FakeHerdr.followup_reply(prompt, {}) + "\n" + FakeHerdr.followup_reply(prompt, {"summary": "other_summary"}),
        }.items():
            with self.subTest(label):
                self.reset_fixture()
                self.to_plan_gate()
                self.ask()
                self._run_with_transformed_output(mutate)
                state = self.state()
                self.assertEqual(state["agent_followup"]["status"], "FAILED", label)
                self.assertEqual([r for r in ha.list_records(self.paths, state["run_id"]) if r["category"] == "followup"], [], label)

    def _symlinked_reply(self, prompt: str) -> str:
        path = Path(FakeHerdr.prompt_field(prompt, "HERDR_RESPONSE"))
        target = self.project_root / "real-answer.md"
        target.write_text("# Answer\nvia symlink\n")
        path.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(target, path)
        return FakeHerdr.followup_frame(*FakeHerdr.ids(prompt), FakeHerdr.prompt_field(prompt, "HERDR_DECISION"), str(path), hashlib.sha256(target.read_bytes()).hexdigest())

    def _run_with_transformed_output(self, mutate) -> None:
        """Submit the follow-up and make the settled transcript what `mutate(fake, prompt)` returns."""
        fake = self.herdr

        def prompt(name: str, text: str, *, timeout_ms: int) -> dict:
            key = fake._resolve(name, "prompt")
            fake.prompts.append((key, text))
            fake.agents[key]["agent_status"] = "idle"
            fake.outputs[key] = text + "\nreply\n" + mutate(fake, text)
            return {"result": {"agent": fake.agents[key]}}

        fake.prompt = prompt  # type: ignore[method-assign]
        fake.ack_supported = False
        self.assertEqual(self.sup.worker(), 2)

    def test_response_ready_crash_completes_without_reread_or_second_artifact(self) -> None:
        self.to_plan_gate()
        self.ask()
        self.herdr.responses = [{"followup": {}}]
        # crash exactly after RESPONSE_READY was persisted: emulate by rewinding the restore
        original = type(self.sup)._complete_followup
        type(self.sup)._complete_followup = lambda self_, state: "stop"  # type: ignore[method-assign]
        try:
            self.sup.worker()
        finally:
            type(self.sup)._complete_followup = original  # type: ignore[method-assign]
        state = self.state()
        self.assertEqual((state["agent_followup"]["status"], state["supervisor_state"]), ("RESPONSE_READY", "RUNNING"))
        self.assertEqual(self.event_types().count("AGENT_FOLLOWUP_READY"), 0)
        prompts, reads, waits = self.counters()
        self.assertEqual(self.make_supervisor().worker(), 4)
        after = self.state()
        self.assertEqual((after["agent_followup"]["status"], after["supervisor_state"]), ("RESTORED", "WAIT_PLAN_APPROVAL"))
        self.assertEqual(self.counters(), (prompts, reads, waits), "recovery neither prompts nor rereads")
        self.assertEqual(self.event_types().count("AGENT_FOLLOWUP_READY"), 1)
        self.assertEqual(len([r for r in ha.list_records(self.paths, after["run_id"]) if r["category"] == "followup"]), 1)
        # a second worker pass after restoration is a pure no-op
        self.assertEqual(self.make_supervisor().worker(), 4)
        self.assertEqual(self.event_types().count("AGENT_FOLLOWUP_READY"), 1)

    def test_working_or_blocked_agent_before_delivery_fails_closed_without_sending(self) -> None:
        for status in ("working", "blocked", "unknown"):
            with self.subTest(status):
                self.reset_fixture()
                self.to_plan_gate()
                self.ask()
                self.herdr.agents["codex-main"]["agent_status"] = status
                self.herdr.responses = []
                self.assertEqual(self.sup.worker(), 2)
                state = self.state()
                self.assertEqual((state["agent_followup"]["status"], state["agent_followup"]["delivery_uncertain"]), ("FAILED", False))
                self.assertIn("was not sent", state["wait_user_reason"])
                with self.assertRaises(hs.SupervisorError):
                    self.apply_direct({"action": "retry_followup_response", "run_id": state["run_id"], "followup_turn_id": state["agent_followup"]["followup_turn_id"]})
                self.assertTrue(self.apply_direct({"action": "return_to_decision", "run_id": state["run_id"], "followup_turn_id": state["agent_followup"]["followup_turn_id"]})["ok"])
                self.assertEqual(self.state()["supervisor_state"], "WAIT_PLAN_APPROVAL")

    def test_pause_and_cancel_during_a_follow_up_wait_keep_the_decision(self) -> None:
        self.to_plan_gate()
        self.ask()
        self.herdr.responses = [{"output": "prose\n", "status": "idle"}]
        self.assertEqual(self.sup.worker(), 2)
        state = self.state()
        self.assertTrue(self.apply_direct({"action": "pause", "run_id": state["run_id"]})["ok"])
        self.assertEqual(self.state()["supervisor_state"], "PAUSED")
        result = self.apply_direct({"action": "resume", "run_id": state["run_id"]})
        self.assertIn("unresolved follow-up wait", result["message"])
        self.assertEqual((self.state()["supervisor_state"], self.state()["pending_gate"]["status"]), ("WAIT_USER", "pending"))
        self.assertEqual(self.make_supervisor().worker(), 2)
        self.assertTrue(self.apply_direct({"action": "cancel", "run_id": state["run_id"]})["ok"])
        self.assertEqual(self.state()["supervisor_state"], "CANCELLED")
        self.make_supervisor().store.read_state()  # the terminal record with an open follow-up still validates


class FollowupSessionTests(FollowupCase):
    def test_same_provider_and_exact_session_are_used_and_alias_loss_is_tolerated(self) -> None:
        self.to_plan_gate()
        self.ask()
        # Herdr dropped the display alias but the exact session is live in the recorded pane
        record = self.herdr.agents.pop("codex-main")
        self.herdr.agents["w3:p2"] = {**record, "name": None}
        self.herdr.responses = [{"followup": {}}]
        self.assertEqual(self.sup.worker(), 4)
        self.assertEqual(self.state()["agent_followup"]["status"], "RESTORED")
        self.assertEqual(self.herdr.prompts[-1][0], "w3:p2")
        self.assertEqual(self.herdr.targets[-1][1] if self.herdr.targets[-1][0] != "prompt" else [t for t in self.herdr.targets if t[0] == "prompt"][-1][1], "w3:p2")

    def test_session_mismatch_or_ambiguity_fails_closed_without_contacting_anyone(self) -> None:
        for label, mutate in {
            "different session under the alias": lambda: self.herdr.agents["codex-main"].update(agent_session={"value": "99999999-9999-4999-8999-999999999999"}),
            "duplicate identity": lambda: self.herdr.agents.__setitem__("dup", FakeHerdr.agent("codex", "codex-dup", "w7:p1", CODEX_SESSION)),
            "session gone": lambda: self.herdr.agents.pop("codex-main"),
        }.items():
            with self.subTest(label):
                self.reset_fixture()
                before = self.to_plan_gate()
                self.ask()
                mutate()
                self.herdr.responses = []
                exit_code = self.make_supervisor().worker()
                self.assertEqual(exit_code, 2, label)
                state = self.state()
                self.assertEqual(len(self.herdr.prompts), 1, f"{label}: no prompt to a replacement identity")
                self.assertEqual(self.herdr.starts, [], f"{label}: nothing restored or adopted")
                self.assertEqual(state["native_sessions"], before["native_sessions"])
                followup = state["agent_followup"]
                self.assertEqual(followup["status"], "FAILED", label)
                self.assertFalse(followup["delivery_uncertain"], label)
                # return to the preserved decision works without contacting anyone
                sup = self.make_supervisor()
                with sup.store.transaction():
                    st = sup.store.read_state()
                    self.assertTrue(sup.return_to_decision(st, run_id=st["run_id"], followup_turn_id=followup["followup_turn_id"], actor="cli")["ok"])
                self.assertEqual(self.snapshot(self.state()), self.snapshot(before))

    def test_owner_record_drift_stops_the_worker_before_any_follow_up_contact(self) -> None:
        before = self.to_plan_gate()
        self.ask()
        hs.atomic_write_json(self.paths.owners_file, {**self.owners, "codex": {"pane_id": "w3:p2", "session_id": "99999999-9999-4999-8999-999999999999"}})
        self.herdr.responses = []
        with self.assertRaises(hs.SupervisorError):
            self.make_supervisor().worker()  # the run-wide session guard refuses before the follow-up path is reached
        self.assertEqual(len(self.herdr.prompts), 1)
        self.assertEqual(self.state()["agent_followup"]["status"], "PREPARED")
        with self.sup.store.transaction():  # the refused worker process has exited; its recorded pid is dead
            st = self.sup.store.read_state()
            st["worker_pid"] = None
            self.sup.store.write_state(st)
        hs.atomic_write_json(self.paths.owners_file, self.owners)
        sup = self.make_supervisor()
        with sup.store.transaction():
            st = sup.store.read_state()
            self.assertTrue(sup.return_to_decision(st, run_id=st["run_id"], followup_turn_id=st["agent_followup"]["followup_turn_id"], actor="cli")["ok"])
        self.assertEqual(self.snapshot(self.state()), self.snapshot(before))

    def test_ask_is_refused_when_the_recorded_session_already_differs(self) -> None:
        before = self.to_plan_gate()
        hs.atomic_write_json(self.paths.owners_file, {**self.owners, "codex": {"pane_id": "w3:p2", "session_id": "99999999-9999-4999-8999-999999999999"}})
        with self.assertRaises(hs.SupervisorError) as caught:
            self.apply_direct({"action": "ask_agent", "run_id": before["run_id"], "question": "why?", "actor": "cli"})
        self.assertIn("not contacting a different conversation", str(caught.exception))
        self.assertIsNone(self.state()["agent_followup"])


class FollowupStateValidationTests(FollowupCase):
    def test_absent_default_is_compatible_and_malformed_records_fail_closed(self) -> None:
        self.to_plan_gate()
        state = self.state()
        self.assertIsNone(state["agent_followup"])
        del state["agent_followup"]
        hs.atomic_write_json(self.paths.state_file, state)
        self.assertIsNone(self.sup.store.read_state()["agent_followup"], "old schema-2 records gain the default in memory")
        self.ask()
        good = self.state()
        other = "0bbbbbbb-cccc-4ddd-8eee-ffffffffffff"
        def mutated(**changes) -> dict:
            st = json.loads(json.dumps(good))
            for key, value in changes.items():
                target = st["agent_followup"]
                parts = key.split(".")
                for part in parts[:-1]:
                    target = target[part]
                target[parts[-1]] = value
            return st
        cases = {
            "cross-run": mutated(run_id=other), "cross-session": mutated(session_id=CLAUDE_SESSION), "bad status": mutated(status="DONE"),
            "bool attempts": mutated(reread_attempts=True), "string flag": mutated(delivery_uncertain="false"), "gate id drift": mutated(**{"decision.decision_id": other}),
            "bad kind": mutated(**{"decision.kind": "wait"}), "suspended state drift": mutated(**{"suspended.supervisor_state": "WAIT_USER"}),
            "response before verification": mutated(response={"artifact_id": "a" * 32, "sha256": "b" * 64, "bytes": 1, "summary": "x"}),
            "question too long": mutated(question="q" * 1001), "relative path": mutated(response_path="answer.md"), "nested unmet": mutated(**{"suspended.operator_handoff_ready": {"unmet": [{}]}}),
        }
        for label, st in cases.items():
            with self.subTest(label):
                with self.assertRaises(hs.SupervisorError):
                    hs.validate_state_v2(st)
        hs.validate_state_v2(good)


class FollowupPromptTests(FollowupCase):
    def test_follow_up_prompt_is_read_only_bounded_and_never_a_task_or_routing_turn(self) -> None:
        self.to_plan_gate()
        self.ask("Is the rebuild really needed? <script>")
        state = self.state()
        prompt = self.sup.build_followup_prompt(state, state["agent_followup"]["followup_turn_id"])
        self.assertIn("read-only", prompt)
        self.assertIn("Do not change code", prompt)
        self.assertIn("do not emit a workflow routing block", prompt)
        self.assertNotIn("Task:\n", prompt)
        self.assertNotIn(state["task_text"], prompt)
        self.assertNotIn("HERDR_NEXT", prompt)
        self.assertEqual(hpr.find_followup_frames(prompt), [])
        self.assertEqual(hs.find_protocol_blocks(prompt), [])
        for width in (40, 60, 80):
            wrapped = ProtocolWidthTests.wrap(prompt.splitlines(), width)
            self.assertEqual(hpr.find_followup_frames(wrapped), [], width)
            self.assertEqual(hs.find_protocol_blocks(wrapped), [], width)
        self.assertLess(len(prompt), 1800)
        # the task sentinel appears only in the initial task prompt
        initial = self.herdr.prompts[0][1]
        self.assertIn("Task:\n", initial)
        self.assertEqual([p for _, p in self.herdr.prompts if "Task:\n" in p], [initial])

    def test_status_reports_the_follow_up_without_secrets(self) -> None:
        self.to_plan_gate()
        report = self.sup.status()
        self.assertEqual(report["followup_decision"]["kind"], "gate")
        self.assertIsNone(report["agent_followup"])
        self.ask()
        report = self.sup.status()
        self.assertEqual((report["agent_followup"]["status"], report["agent_followup"]["decision_kind"]), ("PREPARED", "gate"))
        self.assertNotIn("suspended", report["agent_followup"])
        self.assertNotIn("session_id", report["agent_followup"])


class FollowupCorrectionTests(FollowupCase):
    """Codex review corrections F2–F5: credential refusal, persisted-destination revalidation, response
    structure, and fallback-directory preparation."""

    CREDENTIALS = {
        "bearer": "Authorization: Bearer sk-test-secret-material-1234567890 — why did this fail?",
        "key value": "why does api_key=AbCdEf1234567890xyz not work?",
        "header": "x-api-key: abcdefghij1234567890 rejected?",
        "private key": "-----BEGIN RSA PRIVATE KEY-----\nMIIEow\n-----END RSA PRIVATE KEY-----\nis this key format ok?",
        "dsn": "postgres://app:s3cretpw1234567890@db.internal/prod fails, why?",
        "bot token": "token 123456789:AAFakeTokenForTestsOnly_abcdefghijklmnopqrs valid?",
        "cloud key id": "AKIAABCDEFGHIJKLMNOP appears in the log, why?",
    }
    PROSE = ("how is the bot token read?", "why does the password rotation policy matter?", "Is the token file mode 0600?", "what does /ask do with secrets?")

    def test_f2_credential_values_are_refused_before_persistence_and_never_forwarded(self) -> None:
        before = self.to_plan_gate()
        for label, question in self.CREDENTIALS.items():
            with self.subTest(label):
                with self.assertRaises(hs.SupervisorError) as caught:
                    self.apply_direct({"action": "ask_agent", "run_id": before["run_id"], "question": question, "actor": "cli"})
                self.assertIn("credential", str(caught.exception))
                self.assertIsNone(self.state()["agent_followup"])
        # the enqueued (Telegram-shaped) path is refused at apply time too; the worker then prompts nothing
        self.sup.enqueue_command({"request_id": "ask-cred", "action": "ask_agent", "run_id": before["run_id"], "question": self.CREDENTIALS["bearer"], "actor": "telegram:1", "chat_id": 9})
        self.herdr.responses = []
        self.assertEqual(self.sup.worker(), 4)
        self.assertFalse(self.command_results("ask_agent")[-1]["ok"])
        self.assertIsNone(self.state()["agent_followup"])
        self.assertEqual(len(self.herdr.prompts), 1)
        secret_fragments = ("sk-test-secret-material", "AbCdEf1234567890xyz", "MIIEow", "s3cretpw", "AAFakeTokenForTestsOnly", "abcdefghij1234567890", "AKIAABCDEFGHIJKLMNOP")
        haystacks = {"state": self.paths.state_file.read_text(), "events": json.dumps(self.events()), "log": (self.paths.logs_dir / f"{before['run_id']}.jsonl").read_text(), "prompts": "\n".join(p for _, p in self.herdr.prompts)}
        for where, text in haystacks.items():
            for fragment in secret_fragments:
                self.assertNotIn(fragment, text, f"{fragment} leaked into {where}")
        for question in self.PROSE:
            with self.subTest(question):
                self.assertFalse(hred.credential_value_present(question))
        with self.sup.store.transaction():  # the in-process worker above has "exited": clear its recorded pid as the real process would
            st = self.sup.store.read_state()
            st["worker_pid"] = None
            self.sup.store.write_state(st)
        result = self.apply_direct({"action": "ask_agent", "run_id": before["run_id"], "question": self.PROSE[0], "actor": "cli"})
        self.assertTrue(result["ok"], "prose about secret handling stays askable")

    def test_f3_tampered_persisted_destination_never_becomes_a_prompt(self) -> None:
        outside = self.project_root / "outside-review.md"
        symlinked_parent = self.review_root / "linked"
        symlinked_parent.symlink_to(self.project_root)
        cases = {
            "outside root": str(outside),
            "review root itself": str(self.review_root / "AGENT_FOLLOWUP_deadbeef.md"),
            "symlinked parent": str(symlinked_parent / "AGENT_FOLLOWUP_deadbeef.md"),
            "relative": "answer.md",
            "not markdown": str(self.review_dir / "AGENT_FOLLOWUP_deadbeef.txt"),
        }
        for label, path in cases.items():
            with self.subTest(label):
                self.reset_fixture()
                (self.review_root / "linked").symlink_to(self.project_root)
                before = self.to_plan_gate()
                self.ask()
                with self.sup.store.transaction():
                    st = self.sup.store.read_state()
                    st["agent_followup"]["response_path"] = path
                    if label in ("relative", "not markdown"):
                        st["agent_followup"]["response_path"] = str(self.review_dir / "AGENT_FOLLOWUP_deadbeef.md")  # write must validate; tamper the file directly
                    self.sup.store.write_state(st)
                if label in ("relative", "not markdown"):
                    raw = json.loads(self.paths.state_file.read_text())
                    raw["agent_followup"]["response_path"] = path
                    self.paths.state_file.write_text(json.dumps(raw))
                    with self.assertRaises(hs.SupervisorError):
                        self.make_supervisor().store.read_state()  # a malformed path never loads at all
                    continue
                self.herdr.responses = []
                self.assertEqual(self.make_supervisor().worker(), 2, label)
                state = self.state()
                self.assertEqual(len(self.herdr.prompts), 1, f"{label}: zero follow-up prompts")
                self.assertEqual((state["agent_followup"]["status"], state["agent_followup"]["delivery_uncertain"]), ("FAILED", False), label)
                self.assertIn("response destination rejected", state["wait_user_reason"])
                self.assertEqual(state["pending_gate"], before["pending_gate"])
                # recovery with an accepted delivery record is refused the same way, still without contact
                self.assertTrue(self.apply_direct({"action": "return_to_decision", "run_id": state["run_id"], "followup_turn_id": state["agent_followup"]["followup_turn_id"]})["ok"])
                self.assertEqual(self.snapshot(self.state()), self.snapshot(before))

    def test_f3_destination_is_revalidated_on_recovery_of_an_accepted_delivery(self) -> None:
        self.to_plan_gate()
        self.ask()
        with self.sup.store.transaction():
            st = self.sup.store.read_state()
            turn = st["agent_followup"]["followup_turn_id"]
            st["agent_followup"]["status"] = "WAITING"
            st["agent_followup"]["delivery_uncertain"] = True
            st["agent_followup"]["response_path"] = str(self.project_root / "outside.md")
            st["delivery"] = {"turn_id": turn, "agent": "codex", "kind": "followup", "prompt_sha256": "0" * 64, "prompt_chars": 10, "status": "accepted", "prepared_at": hs.iso_utc(NOW)}
            self.sup.store.write_state(st)
        self.herdr.responses = []
        reads = len(self.herdr.reads)
        self.assertEqual(self.make_supervisor().worker(), 2)
        state = self.state()
        self.assertEqual((state["agent_followup"]["status"], state["agent_followup"]["delivery_uncertain"]), ("FAILED", True))
        self.assertEqual(len(self.herdr.reads), reads, "no transcript read either")
        self.assertEqual(len(self.herdr.prompts), 1)

    def test_f3_existing_in_root_file_is_never_given_to_the_agent_as_a_write_destination(self) -> None:
        before = self.to_plan_gate()
        self.ask()
        existing = self.review_dir / "existing-review.md"
        existing.write_text("must remain unchanged\n")
        with self.sup.store.transaction():
            state = self.sup.store.read_state()
            state["agent_followup"]["response_path"] = str(existing)
            self.sup.store.write_state(state)
        self.herdr.responses = []
        self.assertEqual(self.make_supervisor().worker(), 2)
        state = self.state()
        self.assertEqual((state["agent_followup"]["status"], state["agent_followup"]["delivery_uncertain"]), ("FAILED", False))
        self.assertIn("response destination already exists", state["wait_user_reason"])
        self.assertEqual(existing.read_text(), "must remain unchanged\n")
        self.assertEqual(len(self.herdr.prompts), 1, "the initial task prompt is the only prompt")
        self.assertEqual(state["pending_gate"], before["pending_gate"])

    VALID = "## Answer\n\nYes.\n\n## Recommendation\n\nApprove.\n\n## Change needed\n\nno\n\n## Next operator action\n\nApprove the plan.\n"

    def test_f4_response_structure_is_required_before_registration(self) -> None:
        bad = {
            "prose only": "hello only\n",
            "missing section": "## Answer\n\nYes.\n\n## Recommendation\n\nApprove.\n\n## Next operator action\n\nApprove.\n",
            "duplicate section": self.VALID + "\n## Answer\n\nAgain.\n",
            "reordered": "## Recommendation\n\nApprove.\n\n## Answer\n\nYes.\n\n## Change needed\n\nno\n\n## Next operator action\n\nApprove.\n",
            "empty section": "## Answer\n\nYes.\n\n## Recommendation\n\n## Change needed\n\nno\n\n## Next operator action\n\nApprove.\n",
            "text before headings only": "Answer: yes\nRecommendation: approve\nChange needed: no\nNext operator action: approve\n",
        }
        for label, content in bad.items():
            with self.subTest(label):
                with self.assertRaises(hs.SupervisorError):
                    hval.validate_followup_markdown(content.encode(), max_bytes=1 << 20)
                self.reset_fixture()
                before = self.to_plan_gate()
                self.ask()
                self.herdr.responses = [{"followup": {"content": content}}]
                self.assertEqual(self.sup.worker(), 2, label)
                state = self.state()
                self.assertEqual((state["agent_followup"]["status"], state["agent_followup"]["response"]), ("FAILED", None), label)
                self.assertEqual([r for r in ha.list_records(self.paths, state["run_id"]) if r["category"] == "followup"], [], label)
                self.assertEqual(state["pending_gate"], before["pending_gate"])
        good = {
            "level-2 headings": self.VALID,
            "level-1 with parenthetical and colon": "# Answer:\nYes.\n# Recommendation\nApprove.\n# Change needed (yes or no)\nno\n# Next operator action\nApprove.\n",
            "mixed case, preamble text": "Intro line ignored.\n\n### ANSWER\ntext\n### recommendation\ntext\n### Change Needed\nno\n### next operator action\ntext\n",
        }
        for label, content in good.items():
            with self.subTest(label):
                hval.validate_followup_markdown(content.encode(), max_bytes=1 << 20)
                self.reset_fixture()
                self.to_plan_gate()
                self.ask()
                self.herdr.responses = [{"followup": {"content": content}}]
                self.assertEqual(self.sup.worker(), 4, label)
                self.assertEqual(self.state()["agent_followup"]["status"], "RESTORED", label)
        with self.assertRaises(hs.SupervisorError):
            hval.validate_followup_markdown(self.VALID.encode(), max_bytes=10)

    def test_f5_fallback_directory_is_prepared_safely_before_prepared_is_persisted(self) -> None:
        before = self.to_missing_result()
        expected_dir = Path(self.config["review_root"]) / f"run-{before['run_id'][:8]}"
        self.assertFalse(expected_dir.exists())
        self.ask()
        self.assertTrue(expected_dir.is_dir())
        self.assertEqual(expected_dir.stat().st_mode & 0o777, 0o700)
        self.assertEqual(Path(self.state()["agent_followup"]["response_path"]).parent, expected_dir)
        self.assertIn("directory already exists", self.sup.build_followup_prompt(self.state(), self.state()["agent_followup"]["followup_turn_id"]))
        # restart before submit: the directory persists and one delivery lands in it
        self.herdr.responses = [{"followup": {}}]
        self.assertEqual(self.make_supervisor().worker(), 2)
        self.assertEqual(self.state()["agent_followup"]["status"], "RESTORED")
        self.assertTrue(Path(self.state()["agent_followup"]["response_path"]).is_file())
        # unsafe parent: the run directory name already exists as a symlink pointing outside -> refused, decision untouched, no prompt
        self.reset_fixture()
        before = self.to_missing_result()
        (Path(self.config["review_root"]) / f"run-{before['run_id'][:8]}").symlink_to(self.project_root)
        frozen = self.snapshot(before)
        with self.assertRaises(hs.SupervisorError) as caught:
            self.apply_direct({"action": "ask_agent", "run_id": before["run_id"], "question": "why?", "actor": "cli"})
        self.assertIn("outside the review root", str(caught.exception))
        self.assertIsNone(self.state()["agent_followup"])
        self.assertEqual(self.snapshot(self.state()), frozen)
        self.assertEqual(len(self.herdr.prompts), 1)
        self.assertFalse(any((self.project_root / n).exists() for n in ("AGENT_FOLLOWUP_",)))
        # an unavailable review root refuses as well
        self.reset_fixture()
        before = self.to_missing_result()
        os.rename(self.review_root, self.review_root.with_name("gone"))
        with self.assertRaises(hs.SupervisorError):
            self.apply_direct({"action": "ask_agent", "run_id": before["run_id"], "question": "why?", "actor": "cli"})
        self.assertIsNone(self.state()["agent_followup"])
