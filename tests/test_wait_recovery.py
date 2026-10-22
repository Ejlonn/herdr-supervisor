"""Bounded recovery from transient read-only `agent.wait` failures (supervisor reliability, batch 2). Offline: scripted FakeHerdr and a FakeClock, so the
30-second window and the pacing are asserted by fake time, never by sleeping. Every case asserts zero prompts beyond the one delivered turn and zero keys."""
from __future__ import annotations

import json
import unittest

import herdr_core  # noqa: E402
import herdr_runtime as rt  # noqa: E402
from v2_fixtures import V2Case  # noqa: I001 - first: puts the source or installed layout on sys.path


def transient() -> herdr_core.HerdrError:
    return herdr_core.HerdrError("herdr agent wait failed: server_unavailable", code="server_unavailable")


class WaitCase(V2Case):
    """A Codex planning turn whose agent keeps `working` until the script settles it; the wait hook scripts each wait call."""

    def log(self) -> list[dict]:
        return [json.loads(line) for line in (self.paths.logs_dir / f"{self.state()['run_id']}.jsonl").read_text().splitlines()]

    def events_named(self, name: str) -> list[dict]:
        return [e for e in self.log() if e["event"] == name]

    def run_plan_turn(self, script, *, status: str = "working") -> int:
        """`script(fake, name, n)` is invoked on every wait (n counts from 1); it may raise, settle the agent, or change identity."""
        self.write_plan()
        calls = {"n": 0}

        def on_wait(fake, name):
            calls["n"] += 1
            script(fake, name, calls["n"])

        self.herdr.on_wait = on_wait
        self.herdr.responses = [{"v2": ("plan", "human", "plan_approval", str(self.plan_payload())), "status": status}]
        self.waits = calls
        return self.sup.run_new("Add the widget", "codex", workflow_policy="gated_v2")

    def settle(self, fake, name="codex-main") -> None:
        fake.agents[name]["agent_status"] = "idle"

    def assert_nothing_extra_sent(self) -> None:
        self.assertEqual(len(self.herdr.prompts), 1)
        self.assertEqual(self.herdr.sent_keys, [])
        self.assertEqual(self.herdr.starts, [], "recovery never restores, restarts or rebinds an agent")


class TransientWaitRecoveryTests(WaitCase):
    def test_transient_failure_then_same_owner_progress_recovers_without_a_new_prompt(self) -> None:
        def script(fake, name, n):
            if n == 1:
                raise transient()
            self.settle(fake)

        self.assertEqual(self.run_plan_turn(script), 4)
        state = self.state()
        self.assertEqual(state["pending_gate"]["gate_type"], "plan_approval")
        self.assertIsNone(state.get("wait_recovery"), "a successful wait ends the episode")
        self.assertEqual(len(self.events_named("wait_recovery_attempt")), 1)
        self.assertEqual(len(self.events_named("wait_recovery_succeeded")), 1)
        self.assertEqual(self.herdr.waits, ["codex-main", "codex-main"])
        self.assertEqual(self.clock.sleeps, [rt.WAIT_RECOVERY_DELAYS[0]], "one paced delay by fake time, no real sleeping")
        self.assert_nothing_extra_sent()

    def test_repeated_failures_exhaust_the_bound_and_stop_with_a_supported_recovery(self) -> None:
        def script(fake, name, n):
            raise transient()

        self.assertEqual(self.run_plan_turn(script), 2)
        state = self.state()
        self.assertEqual(state["supervisor_state"], "WAIT_USER")
        self.assertFalse(state["wait_user_requires_action"], "/resume is the supported recovery action")
        self.assertIn("exhausted", state["wait_user_reason"])
        self.assertIn("server_unavailable", state["wait_user_reason"])
        self.assertEqual(state["wait_recovery"]["attempts"], rt.WAIT_RECOVERY_MAX_ATTEMPTS + 1)
        self.assertEqual(len(self.events_named("wait_recovery_attempt")), rt.WAIT_RECOVERY_MAX_ATTEMPTS)
        self.assertEqual(len(self.events_named("wait_recovery_exhausted")), 1)
        self.assertEqual(sum(self.clock.sleeps), sum(rt.WAIT_RECOVERY_DELAYS))
        self.assertLessEqual(sum(self.clock.sleeps), rt.WAIT_RECOVERY_WINDOW_SECONDS)
        self.assertEqual(len(self.herdr.waits), rt.WAIT_RECOVERY_MAX_ATTEMPTS + 1)
        self.assert_nothing_extra_sent()

    def test_the_budget_survives_a_restart_and_never_resets_without_a_successful_wait(self) -> None:
        def script(fake, name, n):
            raise transient()

        self.run_plan_turn(script)
        slept = list(self.clock.sleeps)
        waits = len(self.herdr.waits)
        for _ in range(3):  # operator resumes (or the worker is restarted) while Herdr is still failing
            self.assertEqual(self.make_supervisor().resume(), 2)
            self.assertEqual(self.state()["supervisor_state"], "WAIT_USER")
        self.assertEqual(self.clock.sleeps, slept, "an exhausted budget is not refilled by a restart: no further paced retries")
        self.assertEqual(len(self.herdr.waits), waits + 3, "exactly one wait attempt per resume")
        self.assert_nothing_extra_sent()

    def test_a_resume_after_herdr_recovers_succeeds_and_clears_the_episode(self) -> None:
        def script(fake, name, n):
            if n <= rt.WAIT_RECOVERY_MAX_ATTEMPTS + 1:
                raise transient()
            self.settle(fake)

        self.run_plan_turn(script)
        self.assertEqual(self.state()["supervisor_state"], "WAIT_USER")
        self.assertEqual(self.make_supervisor().resume(), 4)
        state = self.state()
        self.assertEqual(state["pending_gate"]["gate_type"], "plan_approval")
        self.assertIsNone(state.get("wait_recovery"))
        self.assert_nothing_extra_sent()

    def test_a_failure_window_that_has_elapsed_is_exhausted_even_with_attempts_left(self) -> None:
        def script(fake, name, n):
            if n == 2:
                self.clock.current += rt.WAIT_RECOVERY_WINDOW_SECONDS + 5  # the second failure arrives after the whole window has passed
            raise transient()

        self.run_plan_turn(script)
        state = self.state()
        self.assertEqual(state["supervisor_state"], "WAIT_USER")
        self.assertEqual(state["wait_recovery"]["attempts"], 2, "attempts were left (3 allowed) but the 30 s window was over")
        self.assertEqual(len(self.events_named("wait_recovery_attempt")), 1)
        exhausted = self.events_named("wait_recovery_exhausted")
        self.assertEqual((len(exhausted), exhausted[0]["within_window"]), (1, False))
        self.assert_nothing_extra_sent()

    def test_changed_or_missing_owner_stays_closed_and_is_never_restored(self) -> None:
        def changed(fake, name, n):
            fake.agents[name]["agent_session"] = {"value": "99999999-9999-4999-8999-999999999999"}
            raise transient()

        self.run_plan_turn(changed)
        self.assertEqual(self.state()["supervisor_state"], "WAIT_USER")
        self.assertIn("cannot safely inspect", self.state()["wait_user_reason"])
        self.assert_nothing_extra_sent()
        self.reset_fixture()

        def missing(fake, name, n):
            fake.agents.pop(name)
            raise transient()

        self.run_plan_turn(missing)
        self.assertEqual(self.state()["supervisor_state"], "WAIT_USER")
        self.assertIn("required live agent is missing", self.state()["wait_user_reason"])
        self.assert_nothing_extra_sent()

    def test_unknown_lifecycle_after_recovery_stays_closed(self) -> None:
        def script(fake, name, n):
            if n == 1:
                fake.agents[name]["agent_status"] = "unknown"
                raise transient()

        self.run_plan_turn(script)
        state = self.state()
        self.assertEqual(state["supervisor_state"], "WAIT_USER")
        self.assertIn("lifecycle is unknown", state["wait_user_reason"])
        self.assertFalse(state.get("pending_gate"), "unknown lifecycle never routes or creates a gate")
        self.assert_nothing_extra_sent()

    def test_blocked_dialog_after_recovery_is_surfaced_not_retried(self) -> None:
        def script(fake, name, n):
            if n == 1:
                fake.agents[name]["agent_status"] = "blocked"
                fake.outputs[name] = "Allow this tool?\n"
                fake.visible[name] = "Allow this tool?\n"
                raise transient()

        self.run_plan_turn(script)
        state = self.state()
        self.assertEqual(state["supervisor_state"], "WAIT_USER")
        self.assertIn("blocked", state["wait_user_reason"].lower())
        self.assertEqual(len(self.herdr.waits), 1)
        self.assert_nothing_extra_sent()

    def test_a_result_that_arrives_during_recovery_routes_exactly_once(self) -> None:
        def script(fake, name, n):
            self.settle(fake)  # the agent finished while the wait transport was failing
            raise transient()

        self.assertEqual(self.run_plan_turn(script), 4)
        state = self.state()
        self.assertEqual(state["pending_gate"]["gate_type"], "plan_approval")
        self.assertEqual(state["gate_sequence"], 1)
        self.assertEqual(len(state["gate_history"]) if state.get("gate_history") else 0, 0, "no earlier gate; this is the only one")
        self.assert_nothing_extra_sent()

    def test_pause_and_cancel_interrupt_the_recovery_pacing(self) -> None:
        for desired, final in (("paused", "PAUSED"), ("cancelled", "CANCELLED")):
            with self.subTest(desired):
                if desired == "cancelled":
                    self.reset_fixture()
                original = self.sup.sleeper

                def sleeper(seconds, original=original, desired=desired):
                    original(seconds)
                    self.sup.store.write_control(desired)

                self.sup.sleeper = sleeper

                def script(fake, name, n):
                    raise transient()

                self.run_plan_turn(script)
                self.assertEqual(self.state()["supervisor_state"], final)
                self.assertEqual(len(self.herdr.waits), 1, "no further wait after the control request")
                self.assert_nothing_extra_sent()

    def test_other_wait_errors_and_ordinary_timeouts_keep_their_existing_behavior(self) -> None:
        def unsupported(fake, name, n):
            raise herdr_core.HerdrError("boom", code="command_error")

        self.run_plan_turn(unsupported)
        self.assertEqual(self.state()["supervisor_state"], "WAIT_USER")
        self.assertIn("cannot wait for", self.state()["wait_user_reason"])
        self.assertEqual(self.events_named("wait_recovery_attempt"), [])
        self.assertEqual(self.clock.sleeps, [])
        self.reset_fixture()

        def timeouts_then_settle(fake, name, n):
            if n <= 3:
                raise herdr_core.HerdrError("timeout", code="timeout")
            self.settle(fake)

        self.assertEqual(self.run_plan_turn(timeouts_then_settle), 4)
        self.assertEqual(self.events_named("wait_recovery_attempt"), [])
        self.assertEqual(self.clock.sleeps, [])
        self.assert_nothing_extra_sent()

    def test_the_plain_text_server_unavailable_output_is_classified(self) -> None:
        import herdr_cli  # noqa: PLC0415

        self.assertEqual(herdr_cli.command_error_code("Error: server_unavailable while waiting"), "server_unavailable")
        self.assertEqual(herdr_cli.command_error_code('{"error": {"code": "server_unavailable", "message": "x"}}'), "server_unavailable")
        self.assertIn("server_unavailable", rt.WAIT_TRANSIENT_CODES)
        self.assertEqual(rt.WAIT_TRANSIENT_CODES, frozenset({"server_unavailable"}), "only the classified transient code is recoverable")


class EpisodeBudgetTests(WaitCase):
    """CI4: a failed wait episode is refilled only by a wait that returned, never by a handled timeout; the remaining budget also bounds the waits themselves."""

    def timeouts_recorded(self) -> list[int]:
        seen: list[int] = []
        original = self.herdr.wait

        def wait(name, *, timeout_ms):
            seen.append(timeout_ms)
            return original(name, timeout_ms=timeout_ms)

        self.herdr.wait = wait
        return seen

    def test_alternating_transient_failures_and_timeouts_still_exhaust_the_episode(self) -> None:
        def script(fake, name, n):
            raise (transient() if n % 2 else herdr_core.HerdrError("timeout", code="timeout"))

        self.assertEqual(self.run_plan_turn(script), 2)
        state = self.state()
        self.assertEqual(state["supervisor_state"], "WAIT_USER")
        self.assertEqual(len(self.events_named("wait_recovery_exhausted")), 1)
        self.assertEqual(self.events_named("wait_recovery_succeeded"), [], "no handled timeout refilled the budget")
        self.assertEqual(state["wait_recovery"]["attempts"], rt.WAIT_RECOVERY_MAX_ATTEMPTS + 1)
        self.assert_nothing_extra_sent()

    def test_the_reviewer_script_four_failures_with_four_timeouts_never_reaches_settlement(self) -> None:
        def script(fake, name, n):
            if n <= 8:
                raise (transient() if n % 2 else herdr_core.HerdrError("timeout", code="timeout"))
            self.settle(fake)

        self.run_plan_turn(script)
        self.assertEqual(self.state()["supervisor_state"], "WAIT_USER")
        self.assertLess(len(self.herdr.waits), 9, "the episode ended before a ninth wait call")

    def test_a_returned_wait_ends_the_episode_and_a_later_failure_gets_a_fresh_budget(self) -> None:
        def script(fake, name, n):
            if n in (1, 2, 4, 5):  # two failures, a wait that returns, then two more failures: neither burst alone exhausts
                raise transient()
            if n == 6:
                self.settle(fake)

        self.assertEqual(self.run_plan_turn(script), 4)
        self.assertEqual(len(self.events_named("wait_recovery_succeeded")), 2)
        self.assertEqual(self.events_named("wait_recovery_exhausted"), [])

    def test_the_episode_survives_handled_timeouts_and_closes_only_after_a_failure_free_window(self) -> None:
        def script(fake, name, n):
            if n == 1:
                raise transient()
            self.clock.current += 12.0  # each healthy wait takes a while
            if n == 2:
                raise herdr_core.HerdrError("timeout", code="timeout")
            if n == 3:
                raise herdr_core.HerdrError("agent_prompt_stalled", code="agent_prompt_stalled")
            if n >= 5:
                self.settle(fake)
            if n == 4:
                raise herdr_core.HerdrError("timeout", code="timeout")

        self.run_plan_turn(script)
        closed = self.events_named("wait_recovery_closed")
        self.assertEqual(len(closed), 1, "closed once, by a full window of failure-free responses, not by a timeout alone")
        self.assertEqual(self.state()["pending_gate"]["gate_type"], "plan_approval")
        self.assert_nothing_extra_sent()

    def test_waits_inside_an_episode_never_outlast_the_remaining_budget(self) -> None:
        seen = self.timeouts_recorded()

        def script(fake, name, n):
            if n == 1:
                raise transient()
            self.clock.current += 12.0
            if n < 4:
                raise herdr_core.HerdrError("timeout", code="timeout")
            self.settle(fake)

        self.run_plan_turn(script)
        configured = int(self.config["wait_timeout_ms"])
        self.assertEqual(seen[0], configured, "outside an episode the configured timeout is used unchanged")
        inside = seen[1:]
        self.assertTrue(all(1000 <= ms <= configured for ms in inside), inside)
        self.assertLess(min(inside), configured, "the budget shrinks as the episode ages, so a later wait is shorter than a full configured wait")
        self.assertEqual(inside, sorted(inside, reverse=True), "never longer than an earlier wait of the same episode")
        self.assert_nothing_extra_sent()

    def test_an_exhausted_budget_survives_a_restart_even_with_handled_timeouts_in_between(self) -> None:
        def script(fake, name, n):
            raise (transient() if n % 2 else herdr_core.HerdrError("timeout", code="timeout"))

        self.run_plan_turn(script)
        waits = len(self.herdr.waits)
        self.assertEqual(self.make_supervisor().resume(), 2)
        self.assertEqual(self.state()["supervisor_state"], "WAIT_USER")
        self.assertLessEqual(len(self.herdr.waits) - waits, 2)
        self.assert_nothing_extra_sent()


class FollowupWaitTests(WaitCase):
    """The same episode rules apply to the follow-up monitor, whose failures settle into the follow-up wait (never a workflow WAIT_USER gate change)."""

    def ask(self) -> None:
        self.write_plan()
        self.assertEqual(self.start_gated([{"v2": ("plan", "human", "plan_approval", str(self.plan_payload()))}]), 4)
        state = self.state()
        decision = self.sup.followup_decision(state)
        command = {"request_id": "ask-0001", "action": "ask_agent", "run_id": state["run_id"], "question": "Why is the rebuild required?", "actor": "telegram:1", "chat_id": 9,
                   "decision_id": decision["decision_id"], "expected_state": state["supervisor_state"], "artifact_sha256": decision["artifact_sha256"], "payload_sha256": decision["payload_sha256"]}
        self.sup.enqueue_command(command)
        self.sup.process_inbox()

    def test_mixed_errors_exhaust_the_follow_up_episode_and_send_nothing_more(self) -> None:
        self.ask()
        calls = {"n": 0}

        def on_wait(fake, name):
            calls["n"] += 1
            raise (transient() if calls["n"] % 2 else herdr_core.HerdrError("timeout", code="timeout"))

        self.herdr.on_wait = on_wait
        self.herdr.responses = [{"output": "prose, no answer frame", "status": "working"}]
        prompts_before = len(self.herdr.prompts)
        self.sup.worker()
        state = self.state()
        self.assertEqual(state["agent_followup"]["status"], "FAILED")
        self.assertIn("exhausted", state["agent_followup"]["reason"])
        self.assertEqual(state["pending_gate"]["status"], "pending", "the decision is preserved untouched")
        self.assertEqual(len(self.herdr.prompts) - prompts_before, 1, "only the one follow-up question was ever delivered")
        self.assertEqual((self.herdr.sent_keys, self.herdr.starts), ([], []))
        self.assertEqual(self.events_named("wait_recovery_succeeded"), [])


if __name__ == "__main__":
    unittest.main()
