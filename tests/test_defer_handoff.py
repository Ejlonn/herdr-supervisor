"""Explicit human defer-and-handoff of a settled run blocked only by human-owned runtime/push obligations (supervisor reliability, batch 5). Offline; the
fake agent and clock stand in for Herdr. Every refusal asserts nothing changed; every success asserts zero model turns, no PASS and no push authority."""
from __future__ import annotations

import copy
import json
import unittest

from v2_fixtures import SHA_A, V2Case, hs  # noqa: I001 - first: puts the source or installed layout on sys.path

BACKGROUND = "background_agents_working"


class DeferCase(V2Case):
    def to_runtime_gate(self, *, raiser: str = "codex", legacy: bool = False, **plan) -> dict:
        self.write_plan()
        self.start_gated([{"v2": ("plan", "human", "plan_approval", str(self.plan_payload(**plan)))}])
        self.approve_pending()
        payload = self.runtime_payload() if legacy else self.runtime_payload(decision_question="Run?", approval_consequence="c", decline_consequence="d", expected_output="o")
        if raiser == "claude":
            responses = [{"v2": ("brief", "claude")}, {"v2": ("implement", "human", "runtime_validation", str(payload))}]
        else:
            responses = [{"v2": ("brief", "claude")}, {"v2": ("implement", "codex")}, {"v2": ("review", "human", "runtime_validation", str(payload))}]
        self.resume_with(responses)
        state = self.state()
        self.assertEqual(state["pending_gate"]["gate_type"], "runtime_validation")
        return state

    def to_push_gate(self) -> dict:
        self.to_runtime_gate(legacy=True)  # a legacy runtime gate needs no separate execution authorization before evidence is recorded
        self.record_runtime("PASS")
        state = self.state()
        self.assertEqual((state["supervisor_state"], state["pending_gate"]["gate_type"]), ("WAIT_PUSH_APPROVAL", "push_approval"))
        return state

    def offer(self, state=None, **kw):
        return self.sup.defer_handoff_eligibility(state or self.state(), **kw)

    def confirm(self, offer: dict, **overrides) -> dict:
        args = {"run_id": offer["run_id"], "gate_id": offer["gate_id"], "turn_id": offer["turn_id"], "candidate": offer["candidate"], "unmet": offer["unmet"], "actor": "cli:operator"}
        args.update(overrides)
        with self.sup.store.transaction():
            return self.sup.defer_handoff(self.sup.store.read_state(), **args)

    def snapshot(self) -> str:
        return json.dumps(self.state(), sort_keys=True)

    def assert_refused(self, offer_args: dict, pattern: str) -> None:
        before = self.snapshot()
        ok, reason, offer = self.offer(**offer_args)
        self.assertFalse(ok, reason)
        self.assertIsNone(offer)
        self.assertRegex(reason, pattern)
        self.assertEqual(self.snapshot(), before, "a refusal changes nothing")


class AcceptedPathTests(DeferCase):
    def test_a_pending_runtime_gate_closes_with_one_confirmation_and_zero_model_turns(self) -> None:
        state = self.to_runtime_gate()
        prompts, reads, waits = len(self.herdr.prompts), len(self.herdr.reads), len(self.herdr.waits)
        ok, reason, offer = self.offer()
        self.assertTrue(ok, reason)
        self.assertEqual((offer["gate_type"], offer["unmet"], offer["candidate"], offer["turn_id"]), ("runtime_validation", ["push_approval", "runtime_validation_pass"], SHA_A, state["pending_gate"]["turn_id"]))
        result = self.confirm(offer, note="TEST passed on my side; reviewing later")
        self.assertTrue(result["ok"])
        done = self.state()
        self.assertEqual(done["supervisor_state"], "DONE")
        completion = done["completion"]
        self.assertEqual((completion["mode"], completion["origin"], completion["verified_by_supervisor"]), ("operator_handoff", "defer_handoff", False))
        self.assertEqual(completion["unmet"], ["push_approval", "runtime_validation_pass"])
        self.assertEqual(completion["deferred_gate"]["gate_id"], state["pending_gate"]["gate_id"])
        self.assertEqual(done["pending_gate"]["status"], "superseded")
        self.assertIn("deferred by operator handoff", done["pending_gate"]["resolution_note"])
        self.assertEqual(done["gate_history"][-1]["status"], "superseded")
        self.assertEqual(done["approvals"][-1]["action"], "superseded")
        self.assertEqual((len(self.herdr.prompts), len(self.herdr.reads), len(self.herdr.waits)), (prompts, reads, waits), "no model turn, read or wake-up")
        self.assertEqual(self.herdr.sent_keys, [])
        self.assertIsNone(done.get("runtime_evidence"))
        self.assertIsNone(done.get("push_approval"), "no PASS and no push authority is synthesized")
        event = self.sup.list_events()[-1]
        self.assertEqual((event["type"], event["data"]["verified_by_supervisor"]), ("TASK_HANDED_OFF", False))
        self.make_supervisor().store.read_state()  # the closed run round-trips through the schema validators and survives a restart

    def test_a_pending_push_gate_lists_only_the_push_obligation(self) -> None:
        self.to_push_gate()
        ok, reason, offer = self.offer()
        self.assertTrue(ok, reason)
        self.assertEqual(offer["unmet"], ["push_approval"])
        self.confirm(offer)
        self.assertEqual(self.state()["completion"]["unmet"], ["push_approval"])

    def test_duplicate_confirmation_is_harmless_and_a_stale_or_cross_run_control_is_refused(self) -> None:
        self.to_runtime_gate()
        ok, reason, offer = self.offer()
        self.confirm(offer)
        after = self.snapshot()
        duplicate = self.confirm(offer)
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(self.snapshot(), after)
        with self.assertRaisesRegex(hs.SupervisorError, "already DONE"):
            self.confirm(offer, gate_id="00000000-0000-4000-8000-000000000000")
        with self.assertRaisesRegex(hs.SupervisorError, "run_id does not match"):
            self.confirm(offer, run_id="11111111-1111-4111-8111-111111111111")
        self.assertEqual(self.snapshot(), after)

    def test_the_worker_command_path_applies_the_same_full_check(self) -> None:
        self.to_runtime_gate()
        ok, reason, offer = self.offer()
        command = {"action": "defer_handoff", "run_id": offer["run_id"], "gate_id": offer["gate_id"], "turn_id": offer["turn_id"], "candidate": offer["candidate"], "unmet": offer["unmet"],
                   "actor": "telegram:1", "chat_id": 9, "request_id": "defer-0001"}
        self.herdr.agents["codex-main"]["agent_status"] = "working"  # settles differently by the time the worker applies it
        self.sup.enqueue_command(command)
        result = self.sup.process_inbox()[0]
        self.assertFalse(result["ok"])
        self.assertIn("not settled", result["message"])
        self.assertEqual(self.state()["supervisor_state"], "WAIT_RUNTIME_VALIDATION")
        self.herdr.agents["codex-main"]["agent_status"] = "idle"
        self.sup.enqueue_command({**command, "request_id": "defer-0002"})
        self.assertTrue(self.sup.process_inbox()[0]["ok"])
        self.assertEqual(self.state()["supervisor_state"], "DONE")
        for bad in ({"unmet": "all"}, {"gate_id": None}, {"run_id": ""}):
            with self.subTest(bad=bad):
                self.sup.enqueue_command({**command, "request_id": f"defer-bad-{len(bad)}-{list(bad)[0]}", **bad})
                self.assertFalse(self.sup.process_inbox()[0]["ok"])


class RefusalTests(DeferCase):
    def test_scope_plan_and_unresolved_decisions_never_defer(self) -> None:
        self.write_plan()
        self.start_gated([{"v2": ("plan", "human", "plan_approval", str(self.plan_payload()))}])
        self.assert_refused({}, "plan_approval decision is pending")
        self.reset_fixture()
        self.write_plan()
        self.start_gated([{"v2": ("plan", "human", "generic_question", str(self.question_payload()))}])
        self.assert_refused({}, "generic_question decision is pending")
        self.reset_fixture()
        self.write_plan()
        self.start_gated([{"output": "prose, no block", "status": "idle"}])
        self.assert_refused({}, "no pending runtime or push gate")

    def test_stale_binding_identifiers_refuse(self) -> None:
        self.to_runtime_gate()
        self.assert_refused({"run_id": "11111111-1111-4111-8111-111111111111"}, "run_id does not match")
        self.assert_refused({"gate_id": "x"}, "gate_id differs")
        self.assert_refused({"turn_id": "x"}, "turn_id differs")
        self.assert_refused({"candidate": "c" * 40}, "candidate differs")
        self.assert_refused({"unmet": ["push_approval"]}, "unmet obligations changed")

    def test_a_changed_plan_candidate_or_repository_binding_refuses_with_the_predicate(self) -> None:
        state = self.to_runtime_gate()
        self.head = "b" * 40
        self.assert_refused({}, "binding changed: repository_head: expected a{40}, observed b{40}")
        self.head = SHA_A
        ok, reason, offer = self.offer()
        self.assertTrue(ok, reason)
        from pathlib import Path  # noqa: PLC0415

        Path(state["approved_plan"]["plan_path"]).write_text("changed\n")
        self.assert_refused({}, "binding changed: approved_plan_hash")

    def test_a_working_blocked_unknown_or_unlocated_agent_refuses(self) -> None:
        self.to_runtime_gate()
        for status in ("working", "blocked", "unknown"):
            with self.subTest(status):
                self.herdr.agents["codex-main"]["agent_status"] = status
                self.assert_refused({}, "not settled")
        self.herdr.agents["codex-main"]["agent_status"] = "idle"
        self.herdr.agents["codex-main"]["agent_session"] = {"value": "99999999-9999-4999-8999-999999999999"}
        self.assert_refused({}, "could not be located|not settled|not confirmed settled")

    def test_a_claude_pane_with_a_pending_background_indicator_refuses(self) -> None:
        self.to_runtime_gate(raiser="claude")
        ok, reason, offer = self.offer()
        self.assertTrue(ok, reason)
        self.herdr.explain_matched = {BACKGROUND}
        self.herdr.explain_state = "working"
        self.assert_refused({}, "background children may still be running|not settled")
        self.herdr.explain_matched = set()
        self.herdr.explain_state = "idle"
        self.assertTrue(self.offer()[0])

    def test_uncertain_work_refuses_through_the_shared_in_flight_rule(self) -> None:
        self.to_runtime_gate()
        for label, mutate in {
            "uncertain delivery": lambda s: s["delivery"].update(status="uncertain"),
            "quota wait": lambda s: s.update(quota_wait={"provider": "codex"}),
            "pending continuation": lambda s: s.update(continuation={"kind": "x"}),
            "deferred anomaly": lambda s: s.update(deferred_anomaly={"x": 1}),
            "missing result": lambda s: s.update(missing_result={"turn_id": "t"}),
            "unconfirmed runtime proposal": lambda s: s.update(runtime_proposal={"status": "proposed"}),
        }.items():
            with self.subTest(label):
                shadow = copy.deepcopy(self.state())
                mutate(shadow)
                ok, reason, offer = self.offer(shadow)
                self.assertFalse(ok, label)
                self.assertIsNone(offer)

    def test_ordinary_done_stays_strict_and_the_two_closures_are_separate(self) -> None:
        self.to_runtime_gate()
        with self.assertRaisesRegex(hs.SupervisorError, "operator handoff refused"):
            with self.sup.store.transaction():
                self.sup.complete_operator_handoff(self.sup.store.read_state(), run_id=self.state()["run_id"], actor="cli")
        self.assertEqual(self.state()["supervisor_state"], "WAIT_RUNTIME_VALIDATION")
        self.assertFalse(self.sup.operator_handoff_eligibility(self.state())[0])


class StatusTests(DeferCase):
    def test_status_exposes_the_offer_state_only(self) -> None:
        self.to_runtime_gate()
        status = self.sup.status()
        self.assertEqual((status["defer_handoff"]["available"], status["defer_handoff"]["unmet"]), (True, ["push_approval", "runtime_validation_pass"]))
        self.herdr.responses = []
        done_before = len(self.herdr.prompts)
        self.sup.status()
        self.assertEqual(len(self.herdr.prompts), done_before)


if __name__ == "__main__":
    unittest.main()
