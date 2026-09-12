"""Release blockers 4 and 5: WAIT_USER/WAIT_QUOTA precedence with deferred anomaly reconciliation, and
deadline-plus-recheck early wake without any LLM involvement. All fixtures are mocks with a fake clock."""

from __future__ import annotations

import json
from pathlib import Path

from v2_fixtures import CLAUDE_SESSION, CODEX_SESSION, NOW, FakeHerdr, V2Case, hs, quota_json


class StateCase(V2Case):
    def setUp(self) -> None:
        super().setUp()
        self.write_plan()

    def write_quota_fresh(self, provider: str, five: float, five_reset: float, week: float, week_reset: float, *, fetched: float | None = None) -> None:
        """Snapshot with an explicit fetched_at (freshness matters for early wake)."""
        name = self.config["agents"][provider]["quota_file"]
        session = CLAUDE_SESSION if provider == "claude" else None
        payload = quota_json(five, five_reset, week, week_reset, provider=provider, session=session)
        payload["fetched_at_unix"] = self.clock.current if fetched is None else fetched
        (self.quota_dir / name).write_text(json.dumps(payload))

    def settled_without_protocol(self, *, five: float = 0, five_reset: float | None = None, week: float = 60, week_reset: float | None = None) -> int:
        """First Codex turn settles `done` with no protocol block; quota as given at settle time."""
        five_reset = NOW + 1800 if five_reset is None else five_reset
        week_reset = NOW + 86400 if week_reset is None else week_reset

        def exhaust(fake: FakeHerdr, name: str, text: str) -> None:
            self.write_quota_fresh("codex", five, five_reset, week, week_reset)

        return self.start_gated([{"status": "done", "output": "some work happened, then the turn ended\n", "before": exhaust}])

    def stop_when_waiting(self, *, after: float = 0.0) -> None:
        """Simulate the process stopping while in WAIT_QUOTA (pause request at the first checkpoint that
        observes WAIT_QUOTA and a clock at/after `after`)."""
        original_check = self.sup.check_control

        def stop(state):
            if state.get("supervisor_state") == "WAIT_QUOTA" and self.clock.current >= after:
                self.sup.store.write_control("paused")
            return original_check(state)

        self.sup.check_control = stop  # type: ignore[assignment]

    def refresh_restores(self, at_or_after: float) -> None:
        def restore(fake: FakeHerdr) -> None:
            if self.clock.current >= at_or_after:
                self.write_quota_fresh("codex", 100, self.clock.current + 18000, 60, NOW + 86400)
            else:
                self.write_quota_fresh("codex", 0, NOW + 1800, 60, NOW + 86400)

        self.herdr.on_refresh = restore


class PrecedenceTests(StateCase):
    def test_1_accepted_done_missing_protocol_blocking_five_hour_enters_wait_quota_immediately(self) -> None:
        self.stop_when_waiting()
        self.refresh_restores(at_or_after=float("inf"))
        code = self.settled_without_protocol(five=0, five_reset=NOW + 1800)
        self.assertEqual(code, 3)
        events = self.event_types()
        self.assertIn("WAIT_QUOTA", events)
        self.assertNotIn("WAIT_USER", events, "never entered sticky WAIT_USER first")
        wait_event = [e for e in self.events() if e["type"] == "WAIT_QUOTA"][-1]["data"]
        self.assertEqual(wait_event["deferred_anomaly"], "missing_protocol")
        self.assertTrue(wait_event["early_refresh_available"])
        state = self.state()
        anomaly = state["deferred_anomaly"]
        self.assertEqual(anomaly["kind"], "missing_protocol")
        self.assertEqual(anomaly["run_id"], state["run_id"])
        self.assertEqual(anomaly["provider"], "codex")
        self.assertEqual(anomaly["session_id"], CODEX_SESSION)
        self.assertEqual(anomaly["delivery_status"], "accepted")
        self.assertEqual(anomaly["continuation"], "pending")
        self.assertEqual(anomaly["turn_id"], state["delivery"]["turn_id"])
        self.assertEqual(state["delivery"]["status"], "interrupted")
        self.assertEqual(len(self.herdr.prompts), 1, "nothing resent")
        self.assertEqual(self.herdr.reads, [("codex-main", "recent-unwrapped")], "one settled read only")

    def test_2_blocking_weekly_enters_wait_quota(self) -> None:
        self.stop_when_waiting()
        self.herdr.on_refresh = lambda fake: None
        self.settled_without_protocol(five=50, five_reset=NOW + 1800, week=0, week_reset=NOW + 3 * 86400)
        self.assertIn("WAIT_QUOTA", self.event_types())
        wait_event = [e for e in self.events() if e["type"] == "WAIT_QUOTA"][-1]["data"]
        self.assertEqual(wait_event["windows"], ["weekly"])

    def test_3_no_blocking_quota_enters_wait_user(self) -> None:
        self.herdr.on_refresh = lambda fake: None
        code = self.settled_without_protocol(five=50)
        self.assertEqual(code, 2)
        state = self.state()
        self.assertEqual(state["supervisor_state"], "WAIT_USER")
        self.assertIn("no blocking quota window", state["wait_user_reason"])
        self.assertIsNone(state["deferred_anomaly"])
        self.assertNotIn("WAIT_QUOTA", self.event_types())
        self.assertEqual(len(self.herdr.refresh_commands), 1, "refresh was attempted once before classifying")
        self.assertTrue(state["wait_user_requires_action"], "an unresolved accepted turn needs guidance, not a blind resume")

    def test_provider_limit_text_overrides_rounded_positive_percentage_without_a_threshold(self) -> None:
        for remaining in (1, 2, 3):
            self.reset_fixture()
            self.stop_when_waiting()

            def exhaust(fake: FakeHerdr, name: str, text: str, value: float = remaining) -> None:
                self.write_quota_fresh("codex", value, NOW + 1800, 60, NOW + 86400)

            code = self.start_gated([{
                "status": "done",
                "output": "You've hit your session limit; reset is in the future.\n",
                "before": exhaust,
            }])
            self.assertEqual(code, 3)
            state = self.state()
            self.assertEqual(state["supervisor_state"], "PAUSED")
            self.assertTrue(state["quota_wait"]["provider_limit_inferred"])
            self.assertEqual(state["quota_wait"]["blocking_windows"][0]["remaining_percent"], remaining)
            self.assertEqual(len(self.herdr.prompts), 1)

    def test_unconfirmed_delivery_cannot_be_reclassified_as_quota(self) -> None:
        def lose_after_quota_changes(name: str, text: str, *, timeout_ms: int) -> None:
            # The pre-submit check must be usable so this test reaches an uncertain delivery.  The
            # blocking snapshot arrives only after submission, which must still not prove acceptance.
            self.write_quota_fresh("codex", 0, NOW + 1800, 60, NOW + 86400)
            raise hs.HerdrError("lost", code="timeout")

        self.herdr.prompt = lose_after_quota_changes  # type: ignore[assignment]
        self.herdr.on_wait = lambda fake, name: fake.agents[name].__setitem__("agent_status", "done")
        self.write_quota_fresh("codex", 100, NOW + 1800, 60, NOW + 86400)
        code = self.sup.run_new("Add the widget", "codex", workflow_policy="gated_v2")
        self.assertEqual(code, 2)
        state = self.state()
        self.assertEqual(state["supervisor_state"], "WAIT_USER")
        self.assertTrue(state["wait_user_requires_action"])
        self.assertIsNone(state["deferred_anomaly"])

    def test_malformed_deferred_or_quota_wait_state_is_rejected(self) -> None:
        self.stop_when_waiting()
        self.refresh_restores(at_or_after=float("inf"))
        self.settled_without_protocol(five=0, five_reset=NOW + 1800)
        valid = self.state()
        for mutate in (
            lambda state: state["deferred_anomaly"].__setitem__("continuation", "retry_forever"),
            lambda state: state["quota_wait"].__setitem__("blocking_windows", []),
            lambda state: state["quota_wait"].__setitem__("provider_limit_inferred", "yes"),
        ):
            candidate = json.loads(json.dumps(valid))
            mutate(candidate)
            self.paths.state_file.write_text(json.dumps(candidate))
            with self.assertRaises(hs.SupervisorError):
                self.sup.store.read_state()
        self.paths.state_file.write_text(json.dumps(valid))

    def test_4_valid_explicit_human_gate_wins_over_zero_quota(self) -> None:
        def exhaust(fake: FakeHerdr, name: str, text: str) -> None:
            self.write_quota_fresh("codex", 0, NOW + 1800, 60, NOW + 86400)

        code = self.start_gated([{"v2": ("plan", "human", "plan_approval", str(self.plan_payload())), "before": exhaust}])
        self.assertEqual(code, 4)
        state = self.state()
        self.assertEqual(state["supervisor_state"], "WAIT_PLAN_APPROVAL")
        self.assertNotIn("WAIT_QUOTA", self.event_types())
        self.assertIsNone(state["deferred_anomaly"])
        # the gate stays authoritative on resume too (worker never converts it to a quota wait)
        self.assertEqual(self.sup.worker(), 4)
        self.assertEqual(self.state()["supervisor_state"], "WAIT_PLAN_APPROVAL")
        # a generic question and a runtime gate behave the same
        self.reset_fixture()
        code = self.start_gated([{"v2": ("plan", "human", "generic_question", str(self.question_payload())), "before": exhaust}])
        self.assertEqual(code, 2)
        self.assertEqual(self.state()["pending_gate"]["gate_type"], "generic_question")
        self.assertNotIn("WAIT_QUOTA", self.event_types())

    def test_5_to_8_deferred_reset_gives_exactly_one_reconciliation_and_proceeds(self) -> None:
        self.refresh_restores(at_or_after=NOW + 1800)
        # after the wait, the reconciliation turn emits a valid plan gate
        self.herdr.responses = [None]  # placeholder, replaced below

        def reconcile_reply(name: str, text: str, *, timeout_ms: int) -> dict:
            self.herdr.prompts.append((name, text))
            run, turn = FakeHerdr.ids(text)
            self.herdr.agents[name]["agent_status"] = "idle"
            self.herdr.outputs[name] = text + "\n" + FakeHerdr.block_v2(run, turn, "plan", "human", "plan_approval", str(self.plan_payload()))
            return {}

        first = self.herdr.prompt
        calls = {"n": 0}

        def prompt(name: str, text: str, *, timeout_ms: int) -> dict:
            calls["n"] += 1
            if calls["n"] == 1:
                self.herdr.responses = [{"status": "done", "output": "work happened\n", "before": lambda f, n, t: self.write_quota_fresh("codex", 0, NOW + 1800, 60, NOW + 86400)}]
                return first(name, text, timeout_ms=timeout_ms)
            return reconcile_reply(name, text, timeout_ms=timeout_ms)

        self.herdr.prompt = prompt  # type: ignore[assignment]
        code = self.sup.run_new("Add the widget", "codex", workflow_policy="gated_v2")
        self.assertEqual(code, 4)
        state = self.state()
        self.assertEqual(state["supervisor_state"], "WAIT_PLAN_APPROVAL", "valid protocol after reconciliation proceeds normally")
        self.assertEqual(len(self.herdr.prompts), 2, "exactly one reconciliation turn")
        self.assertEqual(self.herdr.prompts[0][0], self.herdr.prompts[1][0], "same native session")
        original, reconciliation = self.herdr.prompts[0][1], self.herdr.prompts[1][1]
        self.assertNotEqual(original, reconciliation, "original prompt never replayed")
        self.assertNotIn("Task:\nAdd the widget", reconciliation)
        self.assertIn("Do not redo it", reconciliation)
        self.assertIn("settled without a valid routing result", reconciliation)
        first_run, first_turn = FakeHerdr.ids(original)
        second_run, second_turn = FakeHerdr.ids(reconciliation)
        self.assertEqual(first_run, second_run)
        self.assertNotEqual(first_turn, second_turn, "new turn UUID")
        self.assertIn(first_turn, reconciliation, "binds to the original accepted turn")
        self.assertEqual(state["deferred_anomaly"]["continuation"], "resolved")
        self.assertEqual(state["native_sessions"]["codex"], CODEX_SESSION)
        self.assertIn("QUOTA_RESUMED", self.event_types())

    def test_9_reconciliation_failure_is_wait_user_without_retry(self) -> None:
        for label, failure in (("timeout", {"error": "timeout", "status": "idle"}), ("stalled", {"error": "agent_prompt_stalled", "status": "idle"}), ("settled again without protocol", {"status": "done", "output": "still nothing\n"})):
            self.reset_fixture()
            self.refresh_restores(at_or_after=NOW + 1800)
            first = self.herdr.prompt
            calls = {"n": 0}

            def prompt(name: str, text: str, *, timeout_ms: int, _failure=failure) -> dict:
                calls["n"] += 1
                if calls["n"] == 1:
                    self.herdr.responses = [{"status": "done", "output": "work\n", "before": lambda f, n, t: self.write_quota_fresh("codex", 0, NOW + 1800, 60, NOW + 86400)}]
                else:
                    self.herdr.responses = [_failure]
                return first(name, text, timeout_ms=timeout_ms)

            self.herdr.prompt = prompt  # type: ignore[assignment]
            code = self.sup.run_new("Add the widget", "codex", workflow_policy="gated_v2")
            self.assertEqual(code, 2, label)
            state = self.state()
            self.assertEqual(state["supervisor_state"], "WAIT_USER", label)
            self.assertEqual(len(self.herdr.prompts), 2, f"{label}: no blind retry")
            self.assertEqual(state["deferred_anomaly"]["continuation"], "failed", label)
            # resume performs no further automatic reconciliation
            self.assertEqual(self.sup.resume(), 2, label)
            self.assertEqual(len(self.herdr.prompts), 2, label)

    def test_10_restart_during_deferred_wait_preserves_everything(self) -> None:
        self.stop_when_waiting()  # simulates the process dying mid-wait
        self.refresh_restores(at_or_after=float("inf"))
        self.settled_without_protocol(five=0, five_reset=NOW + 1800)
        persisted = self.state()
        self.assertEqual(persisted["supervisor_state"], "PAUSED")
        anomaly = persisted["deferred_anomaly"]
        wait = persisted["quota_wait"]
        self.assertEqual(wait["anomaly"], "missing_protocol")
        self.assertIn("next_recheck_unix", wait)
        # new process: the pause is lifted, the wait resumes from persisted timestamps; quota clears at reset
        herdr2 = FakeHerdr()

        def restore(fake: FakeHerdr) -> None:
            if self.clock.current >= NOW + 1800:
                self.write_quota_fresh("codex", 100, self.clock.current + 18000, 60, NOW + 86400)

        herdr2.on_refresh = restore

        def reconcile(name: str, text: str, *, timeout_ms: int) -> dict:
            herdr2.prompts.append((name, text))
            run, turn = FakeHerdr.ids(text)
            herdr2.agents[name]["agent_status"] = "idle"
            herdr2.outputs[name] = FakeHerdr.block_v2(run, turn, "plan", "human", "plan_approval", str(self.plan_payload()))
            return {}

        herdr2.prompt = reconcile  # type: ignore[assignment]
        sup2 = hs.Supervisor(self.paths, self.config, herdr2, clock=self.clock.time, sleeper=self.clock.sleep)
        sup2.head_resolver = self.head_resolver
        self.sup.store.write_control("running")
        with sup2.store.transaction():
            st = sup2.store.read_state()
            st["supervisor_state"] = "WAIT_QUOTA"  # the pause was the crash stand-in; the wait itself was persisted
            sup2.store.write_state(st)
        self.assertEqual(sup2.worker(), 4)
        self.assertEqual(len(herdr2.prompts), 1, "exactly one reconciliation after restart, no original replay")
        self.assertEqual(self.state()["deferred_anomaly"]["turn_id"], anomaly["turn_id"])
        self.assertEqual(self.state()["deferred_anomaly"]["continuation"], "resolved")
        self.assertIn("quota_wait_recovered", [json.loads(l)["event"] for l in (self.paths.logs_dir / f"{persisted['run_id']}.jsonl").read_text().splitlines()])

    def test_11_12_paused_and_cancelled_are_not_overridden(self) -> None:
        for control in ("paused", "cancelled"):
            self.reset_fixture()
            self.refresh_restores(at_or_after=float("inf"))
            self.write_quota_fresh("codex", 0, NOW + 1800, 60, NOW + 86400)
            self.start_gated([{"v2": ("plan", "human", "plan_approval", str(self.plan_payload()))}])
            with self.sup.store.transaction():
                st = self.sup.store.read_state()
                st["supervisor_state"] = "PAUSED" if control == "paused" else "CANCELLED"
                if st.get("pending_gate"):
                    st["pending_gate"]["status"] = "superseded"
                self.sup.store.write_state(st)
            self.sup.store.write_control(control)
            self.sup.worker()
            self.assertEqual(self.state()["supervisor_state"], "PAUSED" if control == "paused" else "CANCELLED", control)
            self.assertNotIn("WAIT_QUOTA", self.event_types()[2:], control)

    def test_13_simultaneous_windows_wait_for_the_latest(self) -> None:
        self.herdr.on_refresh = lambda fake: self.write_quota_fresh("codex", 100 if self.clock.current >= NOW + 3 * 86400 else 0, NOW + 1800, 100 if self.clock.current >= NOW + 3 * 86400 else 0, NOW + 3 * 86400)
        self.stop_when_waiting(after=NOW + 3 * 86400 - 300)
        self.settled_without_protocol(five=0, five_reset=NOW + 1800, week=0, week_reset=NOW + 3 * 86400)
        wait_event = [e for e in self.events() if e["type"] == "WAIT_QUOTA"][-1]["data"]
        self.assertEqual(sorted(wait_event["windows"]), ["five_hour", "weekly"])
        self.assertEqual(wait_event["resume_at"], NOW + 3 * 86400 + 60)


class EarlyWakeTests(StateCase):
    def start_wait_with_reset_in(self, seconds: float) -> None:
        self.reset = NOW + seconds
        self.refresh_restores(at_or_after=float("inf"))

    def test_1_to_8_early_wake_after_a_later_recheck_without_human_resume_or_llm(self) -> None:
        reset = NOW + 3 * 3600
        rechecks: list[float] = []

        def refresh(fake: FakeHerdr) -> None:
            rechecks.append(self.clock.current)
            if len(rechecks) >= 4:  # first call = classification-time refresh; the third wait recheck (~30 min in) finds usable quota
                self.write_quota_fresh("codex", 90, self.clock.current + 18000, 60, NOW + 86400)
            else:
                self.write_quota_fresh("codex", 0, reset, 60, NOW + 86400)

        self.herdr.on_refresh = refresh
        first = self.herdr.prompt
        calls = {"n": 0}

        def prompt(name: str, text: str, *, timeout_ms: int) -> dict:
            calls["n"] += 1
            if calls["n"] == 1:
                self.herdr.responses = [{"status": "done", "output": "work\n", "before": lambda f, n, t: self.write_quota_fresh("codex", 0, reset, 60, NOW + 86400)}]
                return first(name, text, timeout_ms=timeout_ms)
            self.herdr.prompts.append((name, text))
            run, turn = FakeHerdr.ids(text)
            self.herdr.agents[name]["agent_status"] = "idle"
            self.herdr.outputs[name] = FakeHerdr.block_v2(run, turn, "plan", "human", "plan_approval", str(self.plan_payload()))
            return {}

        self.herdr.prompt = prompt  # type: ignore[assignment]
        code = self.sup.run_new("Add the widget", "codex", workflow_policy="gated_v2")
        self.assertEqual(code, 4)
        self.assertEqual(self.state()["supervisor_state"], "WAIT_PLAN_APPROVAL")
        self.assertLess(self.clock.current, reset, "resumed early, before the cached reset")
        self.assertGreaterEqual(rechecks[1], NOW + 600 - 10, "first wait recheck at the configured cadence")
        self.assertLess(rechecks[1], NOW + 700)
        self.assertEqual(len(rechecks), 4, "classification refresh + three wait rechecks")
        self.assertEqual(len(self.herdr.prompts), 2, "one reconciliation only; original not duplicated")
        self.assertEqual(self.herdr.prompts[0][0], self.herdr.prompts[1][0], "same session")
        # reads happen only at lifecycle transitions: the original settle, the post-wait inspection before
        # the reconciliation turn, and the reconciliation settle — none while waiting
        self.assertEqual(self.herdr.reads, [("codex-main", "recent-unwrapped")] * 3)
        resumed = [e for e in self.events() if e["type"] == "QUOTA_RESUMED"][-1]["data"]
        self.assertTrue(resumed["early"])
        self.assertEqual(resumed["rechecks"], 3)
        self.assertEqual(self.sup.store.read_control()["desired"], "running", "no human /resume was needed")

    def test_9_10_weekly_stays_blocking_when_five_hour_clears(self) -> None:
        week_reset = NOW + 3 * 86400
        self.stop_when_waiting(after=NOW + 7200)
        self.herdr.on_refresh = lambda fake: self.write_quota_fresh("codex", 100, self.clock.current + 18000, 0, week_reset)  # five-hour clears, weekly blocks
        self.settled_without_protocol(five=0, five_reset=NOW + 1800, week=0, week_reset=week_reset)
        state = self.state()
        self.assertEqual(state["supervisor_state"], "PAUSED")
        self.assertIn("weekly", [w["kind"] for w in state["quota_wait"]["blocking_windows"]])
        self.assertEqual(state["quota_wait"]["resume_at"], week_reset + 60)
        self.assertEqual(len(self.herdr.prompts), 1, "still waiting: no continuation while weekly blocks")
        self.assertNotIn("QUOTA_RESUMED", self.event_types())

    def test_11_stale_snapshot_does_not_cause_unsafe_resume(self) -> None:
        reset = NOW + 3 * 3600
        self.stop_when_waiting(after=NOW + 2000)
        # the classification refresh still shows 0%; later refreshes "succeed" but leave an old snapshot
        # (fetched before the wait began) that shows usable quota
        calls = {"n": 0}

        def stale_later(fake: FakeHerdr) -> None:
            calls["n"] += 1
            if calls["n"] == 1:
                self.write_quota_fresh("codex", 0, reset, 60, NOW + 86400)
            else:
                self.write_quota_fresh("codex", 100, self.clock.current + 18000, 60, NOW + 86400, fetched=NOW - 5000)

        self.herdr.on_refresh = stale_later
        self.settled_without_protocol(five=0, five_reset=reset)
        self.assertEqual(self.state()["supervisor_state"], "PAUSED")
        self.assertEqual(len(self.herdr.prompts), 1, "stale usable-looking snapshot did not resume work")
        log = [json.loads(l) for l in (self.paths.logs_dir / f"{self.state()['run_id']}.jsonl").read_text().splitlines()]
        self.assertTrue(any(e["event"] == "quota_recheck" and ("stale" in e.get("reason", "") or "current snapshot" in e.get("reason", "")) for e in log))

    def test_12_refresh_failure_leaves_state_safe(self) -> None:
        reset = NOW + 1800

        def failing(fake: FakeHerdr) -> None:
            raise hs.HerdrError("plugin unreachable", code="command_error")

        self.herdr.on_refresh = failing
        code = self.settled_without_protocol(five=0, five_reset=reset)
        self.assertEqual(code, 2)
        state = self.state()
        self.assertEqual(state["supervisor_state"], "WAIT_USER")
        self.assertIn("quota refresh failed", state["wait_user_reason"])
        self.assertEqual(len(self.herdr.prompts), 1, "never resumed on cached evidence")
        self.assertGreaterEqual(self.clock.current, reset + 60, "waited the full safe deadline before failing closed")

    def test_13_restart_preserves_next_recheck_semantics(self) -> None:
        reset = NOW + 3 * 3600
        self.stop_when_waiting(after=NOW + 700)
        self.herdr.on_refresh = lambda fake: self.write_quota_fresh("codex", 0, reset, 60, NOW + 86400)
        self.settled_without_protocol(five=0, five_reset=reset)
        persisted = self.state()["quota_wait"]
        self.assertEqual(persisted["recheck_count"], 1)
        next_recheck = persisted["next_recheck_unix"]
        self.assertGreater(next_recheck, self.clock.current)
        # restart: no burst polling; the next recheck happens at the persisted time, not immediately
        herdr2 = FakeHerdr()
        rechecks: list[float] = []

        def refresh2(fake: FakeHerdr) -> None:
            rechecks.append(self.clock.current)
            if len(rechecks) >= 2:
                self.write_quota_fresh("codex", 100, self.clock.current + 18000, 60, NOW + 86400)
            else:
                self.write_quota_fresh("codex", 0, reset, 60, NOW + 86400)

        herdr2.on_refresh = refresh2

        def reconcile(name: str, text: str, *, timeout_ms: int) -> dict:
            herdr2.prompts.append((name, text))
            run, turn = FakeHerdr.ids(text)
            herdr2.agents[name]["agent_status"] = "idle"
            herdr2.outputs[name] = FakeHerdr.block_v2(run, turn, "plan", "done")
            return {}

        herdr2.prompt = reconcile  # type: ignore[assignment]
        sup2 = hs.Supervisor(self.paths, self.config, herdr2, clock=self.clock.time, sleeper=self.clock.sleep)
        sup2.head_resolver = self.head_resolver
        sup2.store.write_control("running")
        with sup2.store.transaction():
            st = sup2.store.read_state()
            st["supervisor_state"] = "WAIT_QUOTA"
            sup2.store.write_state(st)
        sup2.worker()
        self.assertGreaterEqual(rechecks[0], next_recheck - 10, "first post-restart recheck honoured the persisted schedule")
        self.assertEqual(len(herdr2.prompts), 1)

    def test_manual_refresh_is_run_bound_rate_limited_and_never_resends(self) -> None:
        reset = NOW + 3 * 3600
        original_check = self.sup.check_control
        requested = {"done": False}

        def request_once(state):
            if state.get("supervisor_state") == "WAIT_QUOTA" and self.clock.current >= NOW + 100 and not requested["done"]:
                requested["done"] = True
                self.sup.enqueue_command({"request_id": "req-manualrefresh1", "action": "refresh_quota", "run_id": state["run_id"], "actor": "telegram:1"})
                self.sup.enqueue_command({"request_id": "req-manualrefresh2", "action": "refresh_quota", "run_id": state["run_id"], "actor": "telegram:1"})
                self.sup.enqueue_command({"request_id": "req-manualwrongrun", "action": "refresh_quota", "run_id": "0" * 8, "actor": "telegram:1"})
            if self.clock.current >= NOW + 400:
                self.sup.store.write_control("paused")
            return original_check(state)

        self.sup.check_control = request_once  # type: ignore[assignment]
        rechecks: list[float] = []
        self.herdr.on_refresh = lambda fake: (rechecks.append(self.clock.current), self.write_quota_fresh("codex", 0, reset, 60, NOW + 86400))
        self.settled_without_protocol(five=0, five_reset=reset)
        results = {json.loads(p.read_text())["request_id"]: json.loads(p.read_text()) for p in (self.paths.inbox_dir / "completed").glob("*.json")}
        self.assertTrue(results["req-manualrefresh1"]["ok"])
        self.assertFalse(results["req-manualrefresh2"]["ok"], "rate-limited")
        self.assertIn("already requested", results["req-manualrefresh2"]["message"])
        self.assertFalse(results["req-manualwrongrun"]["ok"], "run-bound")
        self.assertTrue(any(NOW + 100 <= t < NOW + 600 for t in rechecks), "manual request triggered an early recheck")
        self.assertEqual(len(self.herdr.prompts), 1, "manual refresh never resends")

    def test_resume_during_active_quota_wait_is_consumed_as_non_llm_refresh(self) -> None:
        reset = NOW + 3 * 3600
        original_check = self.sup.check_control
        requested = {"done": False}

        def request_once(state):
            if state.get("supervisor_state") == "WAIT_QUOTA" and self.clock.current >= NOW + 100 and not requested["done"]:
                requested["done"] = True
                self.sup.enqueue_command({"request_id": "req-resumequota1", "action": "resume", "run_id": state["run_id"], "actor": "telegram:1"})
            return original_check(state)

        self.sup.check_control = request_once  # type: ignore[assignment]
        def stop_before_llm(state, provider):
            state["supervisor_state"] = "PAUSED"
            self.sup.store.write_state(state)
            return "stop"
        self.sup.resume_interrupted_turn = stop_before_llm  # type: ignore[assignment]
        rechecks: list[float] = []
        def refresh(fake):
            rechecks.append(self.clock.current)
            self.write_quota_fresh("codex", 80 if requested["done"] else 0, reset, 60, NOW + 86400)
        self.herdr.on_refresh = refresh
        self.settled_without_protocol(five=0, five_reset=reset)
        results = {json.loads(path.read_text())["request_id"]: json.loads(path.read_text()) for path in (self.paths.inbox_dir / "completed").glob("*.json")}
        self.assertTrue(results["req-resumequota1"]["ok"], results["req-resumequota1"])
        self.assertIn("quota refresh requested", results["req-resumequota1"]["message"])
        self.assertTrue(any(NOW + 100 <= instant < NOW + 600 for instant in rechecks))
        self.assertEqual(len(self.herdr.prompts), 1, "resume performed only deterministic refresh; no prompt was replayed")

    def test_refresh_waits_for_asynchronous_quota_snapshot_publication(self) -> None:
        state = self.sup.initialize("task", "codex", workflow_policy="gated_v2")
        calls = {"sleep": 0}
        self.herdr.on_refresh = lambda fake: None
        def publish_after_three(_seconds):
            calls["sleep"] += 1
            if calls["sleep"] == 3:
                self.write_quota_fresh("codex", 100, NOW + 7200, 60, NOW + 86400, fetched=self.clock.current)
        self.sup.sleeper = publish_after_three
        self.assertEqual(self.sup.refresh_quota(state, "codex"), "ok")
        self.assertEqual(calls["sleep"], 3)
        self.assertEqual(self.sup.quota("codex").windows[0].remaining_percent, 100)

    def test_claude_without_independent_refresh_fails_closed_at_deadline_without_current_evidence(self) -> None:
        reset = NOW + 1800
        self.herdr.responses = []

        def prompt(name: str, text: str, *, timeout_ms: int) -> dict:
            self.herdr.prompts.append((name, text))
            self.herdr.agents[name]["agent_status"] = "done"
            self.herdr.outputs[name] = "work\n"
            return {}

        # a Claude-owned turn: approve a plan first so Claude is the active agent
        self.start_gated([{"v2": ("plan", "human", "plan_approval", str(self.plan_payload()))}])
        self.approve_pending()
        original_prompt = self.herdr.prompt
        stage = {"n": 0}

        def scripted(name: str, text: str, *, timeout_ms: int) -> dict:
            stage["n"] += 1
            if stage["n"] == 1:  # codex brief -> claude
                self.herdr.responses = [{"v2": ("brief", "claude")}]
                return original_prompt(name, text, timeout_ms=timeout_ms)
            if stage["n"] == 2:  # claude settles without protocol while claude quota is exhausted
                name_c = name
                self.write_quota("claude", 0, reset, 60, NOW + 86400)
                self.herdr.prompts.append((name, text))
                self.herdr.agents[name_c]["agent_status"] = "done"
                self.herdr.outputs[name_c] = "partial work\n"
                return {}
            self.herdr.prompts.append((name, text))
            run, turn = FakeHerdr.ids(text)
            self.herdr.agents[name]["agent_status"] = "idle"
            self.herdr.outputs[name] = FakeHerdr.block_v2(run, turn, "implement", "human", "generic_question", str(self.question_payload()))
            return {}

        self.herdr.prompt = scripted  # type: ignore[assignment]
        self.assertEqual(self.resume_with([]), 2)
        state = self.state()
        self.assertEqual(state["supervisor_state"], "WAIT_USER")
        self.assertTrue(
            "no snapshot updated at the safe deadline" in state["wait_user_reason"]
            or "quota snapshot is stale" in state["wait_user_reason"]
        )
        self.assertEqual([n for n, _ in self.herdr.prompts], ["codex-main", "codex-main", "claude-main"], "stale evidence cannot start reconciliation")
        wait_event = [e for e in self.events() if e["type"] == "WAIT_QUOTA"][-1]["data"]
        self.assertEqual(wait_event["provider"], "claude")
        self.assertFalse(wait_event["early_refresh_available"], "reported: no independent refresh for Claude")
        self.assertEqual(self.herdr.refresh_commands, [], "Claude is never refreshed through a command and never woken to poll")
        self.assertGreaterEqual(self.clock.current, reset + 60, "cached deadline wait retained")

    def test_deadline_rejects_a_stale_usable_snapshot(self) -> None:
        deadline = NOW + 1800
        self.clock.current = deadline
        self.write_quota_fresh("codex", 100, deadline + 18000, 60, NOW + 86400, fetched=NOW)
        state = {"workflow_policy": "gated_v2"}
        wait = {"started_at_unix": NOW, "resume_at": deadline, "blocking_windows": []}
        usable, blocking, reason = self.sup._usable_quota_evidence(state, "codex", wait, "ok", deadline)
        self.assertFalse(usable)
        self.assertIsNone(blocking)
        self.assertIn("stale", reason)

    def test_restart_at_deadline_applies_the_same_freshness_rule(self) -> None:
        deadline = NOW + 1800
        self.clock.current = deadline
        self.write_quota_fresh("codex", 100, deadline + 18000, 60, NOW + 86400, fetched=NOW)
        restored = hs.Supervisor(self.paths, self.config, FakeHerdr(), clock=self.clock.time, sleeper=self.clock.sleep)
        wait = {"started_at_unix": NOW, "resume_at": deadline, "blocking_windows": []}
        usable, _, reason = restored._usable_quota_evidence({"workflow_policy": "gated_v2"}, "codex", wait, "ok", deadline)
        self.assertFalse(usable)
        self.assertIn("stale", reason)

    def test_failed_refresh_at_deadline_never_trusts_cached_blocking_or_usable_data(self) -> None:
        deadline = NOW + 1800
        self.clock.current = deadline
        wait = {"started_at_unix": NOW, "resume_at": deadline, "blocking_windows": []}
        for remaining in (0, 100):
            self.write_quota_fresh("codex", remaining, deadline + 18000, 60, NOW + 86400)
            usable, blocking, reason = self.sup._usable_quota_evidence(
                {"workflow_policy": "gated_v2"}, "codex", wait, "failed", deadline
            )
            self.assertFalse(usable)
            self.assertIsNone(blocking)
            self.assertIn("refresh failed", reason)

    def test_successful_refresh_must_produce_snapshot_from_that_attempt(self) -> None:
        attempted = NOW + 600
        self.clock.current = attempted
        self.write_quota_fresh("codex", 100, attempted + 18000, 60, NOW + 86400, fetched=attempted - 30)
        wait = {"started_at_unix": NOW, "resume_at": NOW + 1800, "blocking_windows": []}
        usable, blocking, reason = self.sup._usable_quota_evidence(
            {"workflow_policy": "gated_v2"}, "codex", wait, "ok", attempted
        )
        self.assertFalse(usable)
        self.assertIsNone(blocking)
        self.assertIn("without producing a current snapshot", reason)


class StructuredStatusTests(StateCase):
    def test_status_includes_secret_safe_backup_health_consumed_by_renderer(self) -> None:
        import herdr_present as hp

        secret_destination = "backup-user@private.example:/srv/private/backup"
        backup_config = {
            "schema_version": 1, "enabled": True, "strategy": "ssh_snapshot",
            "sources": [str(self.project_root)], "exclusions": [], "destination": secret_destination,
            "schedule": {"daily": True, "weekly": False, "interval_hours": None},
            "retention": {"daily": 7, "weekly": 4}, "max_age_hours": 24,
            "notify_telegram": False, "git_repos": [],
            "ssh_options": ["-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes"],
        }
        self.paths.config_file.parent.mkdir(parents=True, exist_ok=True)
        (self.paths.config_file.parent / "backup.json").write_text(json.dumps(backup_config))
        report = self.sup.status()
        self.assertEqual(report["backup"]["status"], "failed")
        self.assertEqual(report["backup"]["destination_class"], "ssh remote")
        self.assertNotIn(secret_destination, json.dumps(report["backup"]))
        rendered = hp.render_status(report, "Europe/Istanbul").html
        self.assertIn("Backup: failed", rendered)
        self.assertNotIn(secret_destination, rendered)


class MonitoringInstrumentationTests(StateCase):
    def test_unchanged_working_lifecycle_and_quota_waits_never_touch_an_llm(self) -> None:
        """Architecture assertion: while another agent works or quota blocks, the supervisor issues only
        deterministic lifecycle waits and local quota inspection — zero prompts, reads, keys, or starts."""
        ticks = {"n": 0}

        def keep_working(fake: FakeHerdr, name: str) -> None:
            ticks["n"] += 1
            if ticks["n"] < 40:
                raise hs.HerdrError("timeout", code="timeout")
            run, turn = fake.ids(fake.prompts[-1][1])
            fake.agents[name]["agent_status"] = "idle"
            fake.outputs[name] = fake.block_v2(run, turn, "plan", "human", "plan_approval", str(self.plan_payload()))

        self.herdr.on_wait = keep_working
        self.assertEqual(self.start_gated([{"error": "timeout", "status": "working"}]), 4)
        self.assertEqual(len(self.herdr.prompts), 1)
        self.assertEqual(self.herdr.reads, [("codex-main", "recent-unwrapped")])
        self.assertEqual(self.herdr.sent_keys, [])
        self.assertEqual(self.herdr.starts, [])
        self.assertEqual(len(self.herdr.waits), 40)
        import herdr_runtime
        source = Path(herdr_runtime.__file__).read_text()  # the engine module owns wait_for_quota
        wait_body = source[source.index("    def wait_for_quota("):source.index("    def _snapshot_fetched_at(")]
        for forbidden in (".prompt(", "read_output(", "read_agent(", "send_keys(", "start_agent(", "herdr.wait("):
            self.assertNotIn(forbidden, wait_body, f"quota wait must not call {forbidden}")


class FinalReportProtocolTests(StateCase):
    def ready_state(self) -> dict:
        plan = self.write_plan()
        state = self.sup.initialize("Release the generic supervisor", "codex", workflow_policy="gated_v2")
        state["approved_plan"] = {
            "plan_path": str(plan), "plan_sha256": hs.sha256_file(plan), "payload_sha256": "a" * 64,
            "gate_id": "gate", "actor": "human", "chat_id": None, "at": hs.iso_utc(self.clock.time()),
        }
        state["runtime_policy"] = {"runtime_validation_required": False, "push_approval_required": False}
        turn = "33333333-3333-4333-8333-333333333333"
        state["delivery"] = {"turn_id": turn, "status": "accepted", "kind": "review", "prompt_sha256": "b" * 64}
        return state

    def test_done_registers_only_conventional_authoritative_report_descriptor(self) -> None:
        report = self.review_dir / "FINAL_REPORT.md"
        report.write_text("# Final report\n\nAll acceptance checks passed.\n")
        state = self.ready_state()
        block = hs.ProtocolBlock(state["run_id"], state["delivery"]["turn_id"], "final", "done", "Accepted", version=2)
        self.assertEqual(self.sup.route(state, block), "stop")
        persisted = self.state()
        descriptor = persisted["final_report"]
        self.assertEqual(descriptor["source_path"], str(report))
        self.assertEqual(descriptor["source_sha256"], hs.sha256_file(report))
        done = [event for event in self.events() if event["type"] == "TASK_DONE"][-1]
        self.assertEqual(done["data"]["final_report"], descriptor)

    def test_done_does_not_accept_a_handoff_path_or_require_a_missing_report(self) -> None:
        state = self.ready_state()
        outside = self.review_root / "secret.md"
        outside.write_text("must not attach")
        block = hs.ProtocolBlock(state["run_id"], state["delivery"]["turn_id"], "final", "done", f"See {outside}", version=2)
        self.assertEqual(self.sup.route(state, block), "stop")
        self.assertIsNone(self.state()["final_report"])
        done = [event for event in self.events() if event["type"] == "TASK_DONE"][-1]
        self.assertNotIn("final_report", done["data"])
