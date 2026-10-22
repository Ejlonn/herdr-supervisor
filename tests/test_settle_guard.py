"""Claude pane-mode settle guard (package 2b): a sampled quiet window gates every read of a settled Claude result and every prompt/key sent to a
Claude pane. Offline only: a scripted FakeHerdr and FakeClock, plus reduced real-detector verdicts retained from the BG-1 probe (no screen text)."""
from __future__ import annotations

import json
import unittest
from pathlib import Path

import herdr_cli  # noqa: E402
import herdr_runtime as rt  # noqa: E402
import herdr_supervisor as hs  # noqa: E402
from v2_fixtures import CLAUDE_SESSION, FakeHerdr, V2Case  # noqa: I001 - first: puts the source or installed layout on sys.path

FIXTURES = Path(__file__).resolve().parent / "fixtures"
BACKGROUND = "background_agents_working"


def rule(rule_id: str, matched: bool = False, state: str = "working") -> dict:
    return {"id": rule_id, "matched": matched, "state": state}


def verdict(*, matched: tuple[str, ...] = (), state: str = "idle", omit: tuple[str, ...] = (), skipped: bool = False, blocker: bool = False) -> dict:
    ids = [r for r in FakeHerdr.RULE_IDS if r not in omit]
    return {"state": state, "matched_rule": matched[0] if matched else None, "screen_detection_skipped": skipped, "visible_blocker": blocker,
            "rules": [rule(r, r in matched, "working" if r in FakeHerdr.WORKING_RULE_IDS else "idle") for r in ids]}


def raw_explain(*, matched: tuple[str, ...] = (), state: str = "idle", **overrides) -> dict:
    """A raw `herdr agent explain --json` payload in the real shape (screen-text previews included, to prove they are discarded)."""
    rules = [{"id": r, "matched": r in matched, "priority": 1, "region": "x", "state": "working" if r in FakeHerdr.WORKING_RULE_IDS else "idle",
              "evidence": {"region_preview": "SCREEN TEXT"}} for r in FakeHerdr.RULE_IDS]
    raw = {"state": state, "matched_rule": {"id": matched[0], "state": "working"} if matched else None, "evaluated_rules": rules,
           "manifest_version": "fixture", "screen_detection_skipped": False, "visible_blocker": False}
    raw.update(overrides)
    return raw


def mutate_rule(raw: dict, rule_id: str, **changes) -> dict:
    raw = json.loads(json.dumps(raw))
    for rule in raw["evaluated_rules"]:
        if rule["id"] == rule_id:
            rule.update(changes)
    return raw


MALFORMED_RAW = {
    "working rule with a string matched": mutate_rule(raw_explain(), "osc_title_working", matched="true"),
    "working rule with an integer matched": mutate_rule(raw_explain(), "live_turn_working", matched=1),
    "rule with a missing matched": {**raw_explain(), "evaluated_rules": [{"id": "background_agents_working", "state": "working"}]},
    "rule with a non-string state": mutate_rule(raw_explain(), "osc_title_working", state=None),
    "rule with an empty id": mutate_rule(raw_explain(), "osc_title_working", id=""),
    "non-dict rule record": {**raw_explain(), "evaluated_rules": raw_explain()["evaluated_rules"] + ["junk"]},
    "duplicate rule id": {**raw_explain(), "evaluated_rules": raw_explain()["evaluated_rules"] + [{"id": "osc_title_working", "matched": True, "state": "working"}]},
    "string screen_detection_skipped": raw_explain(screen_detection_skipped="false"),
    "missing visible_blocker": {k: v for k, v in raw_explain().items() if k != "visible_blocker"},
    "integer visible_blocker": raw_explain(visible_blocker=0),
    "missing state": {k: v for k, v in raw_explain().items() if k != "state"},
    "malformed matched_rule": raw_explain(matched_rule="osc_title_working"),
    "non-string manifest_version": raw_explain(manifest_version=7),
    "empty rule list": raw_explain(evaluated_rules=[]),
    # CI1-F: state values must be supported and consistent
    "matched working rule with a misspelt state": mutate_rule(raw_explain(), "osc_title_working", matched=True, state="workng"),
    "rule with an empty state": mutate_rule(raw_explain(), "osc_title_working", state=""),
    "rule with an unknown state word": mutate_rule(raw_explain(), "live_prompt_box", state="running"),
    "matched background rule contradicting its own state": mutate_rule(raw_explain(), "background_agents_working", matched=True, state="idle"),
    "unmatched background rule with a non-working state": mutate_rule(raw_explain(), "background_agents_working", state="idle"),
    "matched working-family rule with an idle state": mutate_rule(raw_explain(), "live_turn_working", matched=True, state="idle"),
    "overall state with an unsupported word": raw_explain(state="idel"),
}


class ClassifierTests(unittest.TestCase):
    """Pure classification of the reduced detector verdict (lifecycle already idle/done)."""

    def kind(self, value) -> str:
        return rt.classify_detector_verdict(value).kind

    def test_idle_with_no_working_rule_is_clear(self) -> None:
        self.assertEqual(self.kind(verdict()), "clear")

    def test_any_matched_working_rule_holds_and_background_rules_are_named_indicators(self) -> None:
        for rule_id in FakeHerdr.WORKING_RULE_IDS:
            with self.subTest(rule_id):
                result = rt.classify_detector_verdict(verdict(matched=(rule_id,), state="working"))
                self.assertEqual(result.kind, "hold")
                self.assertIn(rule_id, result.detail)
                self.assertEqual("background indicator" in result.detail, rule_id in rt.BACKGROUND_INDICATOR_RULE_IDS)

    def test_unusable_or_ambiguous_evidence_is_unknown_never_clear(self) -> None:
        cases = {
            "not a mapping": None,
            "no rules": {"state": "idle", "rules": []},
            "rules missing": {"state": "idle"},
            "skipped screen detection": verdict(skipped=True),
            "background rule not evaluated": verdict(omit=(BACKGROUND,)),
            "explain state working with no rule": verdict(state="working"),
            "explain state unknown": verdict(state="unknown"),
            "explain state missing": {**verdict(), "state": None},
        }
        for label, value in cases.items():
            with self.subTest(label):
                self.assertEqual(self.kind(value), "unknown", label)

    def test_malformed_or_conflicting_reduced_evidence_is_unknown_never_clear(self) -> None:
        base = verdict()
        cases = {
            "string matched": {**base, "rules": [rule("osc_title_working", "true"), *base["rules"][1:]]},
            "missing matched": {**base, "rules": [{"id": "osc_title_working", "state": "working"}, *base["rules"][1:]]},
            "duplicate id": {**base, "rules": [*base["rules"], rule("osc_title_working", True)]},
            "string skipped flag": {**base, "screen_detection_skipped": "false"},
            "missing blocker flag": {k: v for k, v in base.items() if k != "visible_blocker"},
            "integer blocker flag": {**base, "visible_blocker": 0},
            "misspelt working state on a matched rule": {**base, "rules": [rule("osc_title_working", True, "workng"), *base["rules"][1:]]},
            "empty rule state": {**base, "rules": [rule("osc_title_working", False, ""), *base["rules"][1:]]},
            "empty rule id": {**base, "rules": [rule("", False, "working"), *base["rules"]]},
            "unsupported state on an unrelated rule": {**base, "rules": [*base["rules"], rule("unrelated_prompt_rule", False, "running")]},
            "empty state on an unrelated rule": {**base, "rules": [*base["rules"], rule("unrelated_prompt_rule", False, "")]},
            "matched background rule with an idle state": {**base, "rules": [rule(BACKGROUND, True, "idle"), *[r for r in base["rules"] if r["id"] != BACKGROUND]]},
            "unsupported overall state": {**base, "state": "idel"},
        }
        for label, value in cases.items():
            with self.subTest(label):
                self.assertEqual(self.kind(value), "unknown", label)
                self.assertEqual(rt.validate_detector_verdict(value).kind, "unknown", label)
        self.assertIsNone(rt.validate_detector_verdict(base))  # valid control
        # legitimate supported states and unrelated non-working rules are not over-constrained
        valid_mix = {**base, "rules": [*base["rules"], rule("live_blocked_form", False, "blocked"), rule("transcript_viewer", False, "unknown"), rule("osc_title_idle", True, "idle")]}
        self.assertIsNone(rt.validate_detector_verdict(valid_mix))
        self.assertEqual(self.kind(valid_mix), "clear")

    def test_blocked_screen_is_blocked(self) -> None:
        self.assertEqual(self.kind(verdict(state="blocked")), "blocked")
        self.assertEqual(self.kind(verdict(blocker=True)), "blocked")

    def test_retained_real_detector_verdicts(self) -> None:
        """Reduced `herdr agent explain --file` output on the retained BG-1 screens (manifest 2026.10.11.1)."""
        retained = json.loads((FIXTURES / "detector_verdicts_bg1.json").read_text())
        self.assertEqual(self.kind(retained["sample_00_idle"]), "clear")
        self.assertEqual(self.kind(retained["sample_22_done"]), "clear")
        self.assertEqual(self.kind(retained["sample_28_done"]), "clear")
        self.assertEqual(self.kind(retained["sample_01_working"]), "hold")
        # the background rule fires only when no notice row sits between "Waiting for N background agents" and the prompt box
        unblinded = rt.classify_detector_verdict(retained["sample_10_working.nonotice"])
        self.assertEqual(unblinded.kind, "hold")
        self.assertIn(BACKGROUND, unblinded.detail)
        # KNOWN LIMITATION, asserted so it stays visible: with the notice row the screen rules alone read idle. During BG-1 the pending
        # children were still caught because the lifecycle (terminal-title spinner) stayed working; the guard samples that too, but a
        # pane whose title is idle AND whose notice row hides the line is not detectable from screen rules.
        self.assertEqual(self.kind(retained["sample_10_working"]), "clear")
        for name, value in retained.items():
            self.assertNotIn("preview", json.dumps(value), name)


class AdapterReductionTests(unittest.TestCase):
    def test_reduce_explain_discards_screen_text_and_keeps_rule_verdicts(self) -> None:
        reduced = herdr_cli.HerdrCli.reduce_explain(raw_explain(matched=("osc_title_working",), state="working"))
        self.assertNotIn("SCREEN TEXT", json.dumps(reduced))
        self.assertEqual({r["id"] for r in reduced["rules"]}, set(FakeHerdr.RULE_IDS))
        self.assertEqual([r["id"] for r in reduced["rules"] if r["matched"]], ["osc_title_working"])
        self.assertEqual((reduced["state"], reduced["matched_rule"], reduced["screen_detection_skipped"], reduced["visible_blocker"]), ("working", "osc_title_working", False, False))

    def test_supported_states_of_unrelated_rules_are_accepted(self) -> None:
        raw = raw_explain()
        raw["evaluated_rules"] += [{"id": "live_blocked_form", "matched": False, "state": "blocked"}, {"id": "transcript_viewer", "matched": False, "state": "unknown"}]
        reduced = herdr_cli.HerdrCli.reduce_explain(raw)
        self.assertEqual(rt.classify_detector_verdict(reduced).kind, "clear")
        # the real detector's recorded verdicts (every state word it emits) still reduce and classify
        retained = json.loads((FIXTURES / "detector_verdicts_bg1.json").read_text())
        self.assertEqual({r["state"] for v in retained.values() for r in v["rules"]} - herdr_cli.HerdrCli.DETECTOR_STATES, set())

    def test_malformed_relevant_evidence_is_rejected_not_dropped(self) -> None:
        for label, raw in MALFORMED_RAW.items():
            with self.subTest(label), self.assertRaises(herdr_cli.HerdrError):
                herdr_cli.HerdrCli.reduce_explain(raw)
        for raw in (None, [], {"state": "idle"}, {"evaluated_rules": "x"}):
            with self.subTest(raw=raw), self.assertRaises(herdr_cli.HerdrError):
                herdr_cli.HerdrCli.reduce_explain(raw)

    def test_reviewer_state_probes_never_clear(self) -> None:
        for label in ("matched working rule with a misspelt state", "matched background rule contradicting its own state"):
            with self.subTest(label), self.assertRaises(herdr_cli.HerdrError):
                herdr_cli.HerdrCli.reduce_explain(MALFORMED_RAW[label])

    def test_producer_reducer_consumer_never_turns_a_dropped_working_rule_into_clear(self) -> None:
        """The reviewed counterexample: a valid false background rule plus a working rule whose `matched` is the string "true"."""
        raw = mutate_rule(raw_explain(), "osc_title_working", matched="true")
        with self.assertRaises(herdr_cli.HerdrError):
            herdr_cli.HerdrCli.reduce_explain(raw)
        # valid control through the same path
        self.assertEqual(rt.classify_detector_verdict(herdr_cli.HerdrCli.reduce_explain(raw_explain())).kind, "clear")
        self.assertEqual(rt.classify_detector_verdict(herdr_cli.HerdrCli.reduce_explain(raw_explain(matched=("osc_title_working",), state="working"))).kind, "hold")


class GuardCase(V2Case):
    """Claude runs are driven through the gated workflow (Codex plans, approval hands Claude a brief turn that ends in a human question), the
    one policy both the maintained source and the deployed bytes support."""

    def claude_prompts(self) -> list:
        return [p for p in self.herdr.prompts if p[0] == "claude-main"]

    def claude_reads(self) -> list:
        return [r for r in self.herdr.reads if r[0] == "claude-main"]

    def claude_turn(self, *, working: bool = False, interrupt: bool = False) -> int:
        self.write_plan()
        self.assertEqual(self.start_gated([{"v2": ("plan", "human", "plan_approval", str(self.plan_payload()))}]), 4)
        self.approve_pending()
        reply = {"v2": ("brief", "human", "generic_question", str(self.question_payload()))}
        if working:
            reply["status"] = "working"
        if interrupt:
            def die(fake, name):
                if name == "claude-main":
                    raise KeyboardInterrupt

            self.herdr.on_wait = die
        return self.resume_with([{"v2": ("brief", "claude")}, reply])

    def claude_frame(self, state: dict) -> str:
        return FakeHerdr.block_v2(state["run_id"], state["delivery"]["turn_id"], "brief", "human", "generic_question", str(self.question_payload()))

    def log(self) -> list[dict]:
        path = self.paths.logs_dir / f"{self.state()['run_id']}.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()]

    def events_named(self, name: str) -> list[dict]:
        return [e for e in self.log() if e["event"] == name]

    def indicator_after_prompt(self, samples: int, *, then=None) -> list[float]:
        """Report the background indicator for `samples` explain calls once the prompt was sent; returns the clock readings per indicator call."""
        seen: list[float] = []

        def script(fake, name):
            if self.claude_prompts() and len(seen) < samples:
                fake.explain_matched = {BACKGROUND}
                fake.explain_state = "working"
                seen.append(self.clock.current)
            else:
                fake.explain_matched = set()
                fake.explain_state = "idle"
                if then is not None:
                    then(fake)

        self.herdr.on_explain = script
        return seen


class QuietWindowTests(GuardCase):
    def test_idle_result_is_read_only_after_a_full_quiet_window(self) -> None:
        start = self.clock.current
        self.assertEqual(self.claude_turn(), 2)
        cleared = self.events_named("settle_guard_cleared")
        self.assertEqual([c["purpose"] for c in cleared], ["deliver_prompt", "read_result"])
        self.assertTrue(all(c["quiet_seconds"] >= rt.CLAUDE_QUIET_WINDOW_SECONDS for c in cleared), cleared)
        explained = {target for command, target in self.herdr.targets if command == "explain"}
        prompted = {target for command, target in self.herdr.targets if command == "prompt" and target == "claude-main"}
        self.assertEqual(explained, prompted, "the detector is asked about the same identity-safe target every other call uses")
        self.assertGreaterEqual(self.clock.current - start, 2 * rt.CLAUDE_QUIET_WINDOW_SECONDS)
        self.assertEqual(len(self.claude_prompts()), 1)

    def test_sample_pacing_never_exceeds_the_maximum_gap(self) -> None:
        self.claude_turn()
        self.assertLess(rt.CLAUDE_SAMPLE_SLEEP_SECONDS, rt.CLAUDE_MAX_SAMPLE_GAP_SECONDS)
        self.assertTrue(all(s <= rt.CLAUDE_MAX_SAMPLE_GAP_SECONDS for s in self.clock.sleeps), self.clock.sleeps)

    def test_background_indicator_holds_a_visible_early_frame_unread(self) -> None:
        """The pane already shows a complete routing frame, but the indicator is present: nothing is read or routed until it clears."""
        reads_at_hold: list[int] = []
        seen = self.indicator_after_prompt(4, then=None)
        original = self.herdr.on_explain

        def watching(fake, name):
            original(fake, name)
            if fake.explain_matched:
                reads_at_hold.append(len(self.claude_reads()))

        self.herdr.on_explain = watching
        self.assertEqual(self.claude_turn(), 2)
        self.assertEqual(len(seen), 4)
        self.assertEqual(reads_at_hold, [0, 0, 0, 0], "no read while the indicator is present")
        holds = self.events_named("settle_guard_hold")
        self.assertGreaterEqual(len(holds), 1)
        self.assertTrue(all(BACKGROUND in h["detail"] and "screen" not in h["detail"] for h in holds), holds)
        # the window restarts after the last indicator sample: the read happens at least a full window later
        read_purpose = [c for c in self.events_named("settle_guard_cleared") if c["purpose"] == "read_result"]
        self.assertEqual(len(read_purpose), 1)
        self.assertGreaterEqual(read_purpose[0]["quiet_seconds"], rt.CLAUDE_QUIET_WINDOW_SECONDS)
        self.assertGreaterEqual(self.clock.current - seen[-1], rt.CLAUDE_QUIET_WINDOW_SECONDS)
        self.assertEqual(len(self.claude_prompts()), 1)

    def test_indicator_reappearing_inside_the_window_restarts_it(self) -> None:
        phase = {"n": 0}
        times: list[tuple[int, float, bool]] = []

        def script(fake, name):
            if not self.claude_prompts():
                return
            phase["n"] += 1
            reappear = phase["n"] == 3  # two clear samples (5 s), then the indicator, then clear again
            fake.explain_matched = {BACKGROUND} if reappear else set()
            fake.explain_state = "working" if reappear else "idle"
            times.append((phase["n"], self.clock.current, reappear))

        self.herdr.on_explain = script
        self.assertEqual(self.claude_turn(), 2)
        indicator_time = next(t for n, t, r in times if r)
        self.assertGreaterEqual(self.clock.current - indicator_time, rt.CLAUDE_QUIET_WINDOW_SECONDS)
        self.assertEqual(self.claude_reads()[0][0], "claude-main")

    def test_a_sample_gap_longer_than_the_maximum_restarts_the_window(self) -> None:
        state = {"n": 0, "jump_at": None}

        def script(fake, name):
            if not self.claude_prompts():
                return
            state["n"] += 1
            if state["n"] == 3:
                self.clock.current += 10.0  # the controller stalled between samples
                state["jump_at"] = self.clock.current

        self.herdr.on_explain = script
        self.assertEqual(self.claude_turn(), 2)
        self.assertIsNotNone(state["jump_at"])
        self.assertGreaterEqual(self.clock.current - state["jump_at"], rt.CLAUDE_QUIET_WINDOW_SECONDS)

    def test_done_lifecycle_is_eligible_like_idle(self) -> None:
        self.claude_turn()
        self.herdr.agents["claude-main"]["agent_status"] = "done"
        result = self.sup.settle_guard(self.state(), "claude", "unit", control=False)
        # one paced sample returns hold only for non-clear verdicts; a clear sample on "done" accumulates toward the window
        self.assertIn(result.kind, ("clear", "hold"))
        self.assertNotEqual(result.kind, "unknown")
        window = self.sup.settle_guard(self.state(), "claude", "unit-wait", wait=True, control=False)
        self.assertEqual(window.kind, "clear")

    def test_unknown_evidence_stops_without_reading_or_routing(self) -> None:
        for label, arrange in {
            "explain unavailable": lambda f: setattr(f, "explain_error", hs.HerdrError("boom", code="boom")),
            "background rule not evaluated": lambda f: setattr(f, "explain_omit", {BACKGROUND}),
            "screen detection skipped": lambda f: setattr(f, "explain_skipped", True),
            "state not idle without a rule": lambda f: setattr(f, "explain_state", "unknown"),
        }.items():
            with self.subTest(label):
                self.reset_fixture()

                def script(fake, name, arrange=arrange):
                    if self.claude_prompts():
                        arrange(fake)

                self.herdr.on_explain = script
                self.assertEqual(self.claude_turn(), 2)
                self.assertEqual(self.claude_reads(), [], label)
                self.assertEqual(len(self.claude_prompts()), 1, label)
                self.assertEqual(self.state()["supervisor_state"], "WAIT_USER")
                self.assertTrue(self.state()["wait_user_requires_action"])
                self.assertIn("nothing was read, routed or resent", self.state()["wait_user_reason"])
                self.assertEqual([e["kind"] for e in self.events_named("settle_guard_stopped")], ["unknown"], label)

    def test_malformed_raw_detector_evidence_reads_and_sends_nothing(self) -> None:
        """Producer -> real reducer -> guard: every malformed raw payload leaves the pane unread and no further prompt is sent."""
        for label, raw in MALFORMED_RAW.items():
            with self.subTest(label):
                self.reset_fixture()

                def script(fake, name, raw=raw):
                    if self.claude_prompts():
                        fake.explain_raw = raw

                self.herdr.on_explain = script
                self.assertEqual(self.claude_turn(), 2)
                self.assertEqual(self.claude_reads(), [], label)
                self.assertEqual(len(self.claude_prompts()), 1, label)
                self.assertEqual([e["kind"] for e in self.events_named("settle_guard_stopped")], ["unknown"], label)
                self.assertTrue(self.state()["wait_user_requires_action"])
        # valid control: a well-formed idle payload is read
        self.reset_fixture()
        self.herdr.explain_raw = raw_explain()
        self.assertEqual(self.claude_turn(), 2)
        self.assertEqual(len(self.claude_reads()), 1)
        self.assertEqual(self.state()["pending_gate"]["gate_type"], "generic_question")

    def test_adapter_without_explain_is_unknown(self) -> None:
        class NoExplain(FakeHerdr):
            explain_agent = None  # type: ignore[assignment]

        self.herdr = NoExplain()
        self.sup = self.make_supervisor()
        self.herdr.responses = []
        result = self.sup.settle_guard({"run_id": "x"}, "claude", "unit", control=False)
        self.assertEqual(result.kind, "unknown")


class DeliveryTests(GuardCase):
    def test_a_pane_that_never_quiets_blocks_delivery_after_bounded_patience(self) -> None:
        def always(fake, name):
            fake.explain_matched = {BACKGROUND}
            fake.explain_state = "working"

        self.herdr.on_explain = always
        self.assertEqual(self.claude_turn(), 2)
        self.assertEqual(self.claude_prompts(), [], "submission count unchanged: no prompt was delivered")
        self.assertEqual(self.herdr.sent_keys, [])
        self.assertEqual(self.claude_reads(), [])
        self.assertEqual(self.state()["supervisor_state"], "WAIT_USER")
        self.assertIsNone(self.state().get("delivery"))
        self.assertEqual([e["event"] for e in self.log() if e["event"] == "settle_guard_patience"], ["settle_guard_patience"])

    def test_blocked_pane_prevents_delivery(self) -> None:
        self.herdr.agents["claude-main"]["agent_status"] = "blocked"
        self.herdr.visible["claude-main"] = "unrecognised dialog"
        self.herdr.outputs["claude-main"] = "unrecognised dialog"
        code = self.claude_turn()
        self.assertNotEqual(code, 0)
        self.assertEqual(self.claude_prompts(), [])
        self.assertEqual(self.herdr.sent_keys, [])

    def test_changed_or_missing_identity_stops_without_restore_or_send(self) -> None:
        self.claude_turn()
        prompts = len(self.herdr.prompts)
        cases = {
            "changed session": lambda: self.herdr.agents["claude-main"].update(agent_session={"value": "99999999-9999-4999-8999-999999999999"}),
            "missing agent": lambda: self.herdr.agents.pop("claude-main"),
            "duplicate carrier": lambda: self.herdr.agents.update({"w5:p5": FakeHerdr.agent("claude", None, "w5:p5", CLAUDE_SESSION)}),
        }
        for label, arrange in cases.items():
            with self.subTest(label):
                self.herdr.agents = {"claude-main": FakeHerdr.agent("claude", "claude-main", "w3:p1", CLAUDE_SESSION), "codex-main": FakeHerdr.agent("codex", "codex-main", "w3:p2", "11111111-1111-4111-8111-111111111111")}
                arrange()
                self.herdr.starts.clear()
                self.herdr.explains.clear()
                result = self.sup.settle_guard(self.state(), "claude", "unit", wait=True, control=False)
                self.assertEqual(result.kind, "identity", label)
                self.assertEqual(self.herdr.starts, [], label)
                self.assertEqual(self.herdr.explains, [], "no detector call before identity is proven")
                self.assertEqual(len(self.herdr.prompts), prompts)

    def test_codex_is_unaffected(self) -> None:
        self.write_plan()
        self.assertEqual(self.start_gated([{"v2": ("plan", "human", "plan_approval", str(self.plan_payload()))}]), 4)
        self.assertEqual(self.herdr.explains, [])
        self.assertEqual(self.events_named("settle_guard_cleared"), [])
        self.assertEqual(self.sup.settle_guard(self.state(), "codex", "unit").kind, "clear")


class QuotaRecoveryTests(GuardCase):
    """resume_interrupted_turn: dismissal keys and the READY-branch read are guarded like ordinary submission."""

    def interrupted(self, *, indicator: bool, status: str):
        self.config = hs.resolve_config_defaults(hs.deep_merge(self.config, {"quota_block_dismiss_keys": ["Enter"]}))
        self.sup = self.make_supervisor()
        self.assertEqual(self.claude_turn(working=True, interrupt=True), 3)
        persisted = self.state()
        herdr2 = FakeHerdr()
        record = herdr2.agents.pop("claude-main")
        herdr2.agents["w3:p1"] = {**record, "name": None, "agent_status": status}
        herdr2.outputs["w3:p1"] = "You've hit your usage limit\n"
        herdr2.visible["w3:p1"] = "You've hit your usage limit"
        if indicator:
            herdr2.explain_matched = {BACKGROUND}
            herdr2.explain_state = "working"
        sup2 = hs.Supervisor(self.paths, self.config, herdr2, clock=self.clock.time, sleeper=self.clock.sleep)
        sup2.head_resolver = self.head_resolver
        return persisted, herdr2, sup2

    def test_dismissal_keys_are_not_sent_while_a_background_indicator_is_present(self) -> None:
        persisted, herdr2, sup2 = self.interrupted(indicator=True, status="blocked")
        with sup2.store.transaction():
            outcome = sup2.resume_interrupted_turn(sup2.store.read_state(), "claude")
        self.assertEqual(outcome, "stop")
        self.assertEqual(herdr2.sent_keys, [])
        self.assertEqual(herdr2.prompts, [])
        self.assertEqual(self.state()["supervisor_state"], "WAIT_USER")
        self.assertIn("press the configured dismissal keys", self.state()["wait_user_reason"])

    def test_dismissal_keys_need_valid_detector_evidence(self) -> None:
        """Skipped detection, malformed raw evidence and an unavailable explanation stop the key path; send_keys count stays zero."""
        cases = {
            "screen detection skipped, false background rule, blocked state": lambda f: (setattr(f, "explain_skipped", True), setattr(f, "explain_state", "blocked")),
            "malformed raw evidence": lambda f: setattr(f, "explain_raw", MALFORMED_RAW["working rule with a string matched"]),
            "misspelt working state": lambda f: setattr(f, "explain_raw", MALFORMED_RAW["matched working rule with a misspelt state"]),
            "background rule contradicting its state": lambda f: setattr(f, "explain_raw", MALFORMED_RAW["matched background rule contradicting its own state"]),
            "explanation unavailable": lambda f: setattr(f, "explain_error", hs.HerdrError("boom", code="boom")),
            "state not a string": lambda f: setattr(f, "explain_state", None),
        }
        for label, arrange in cases.items():
            with self.subTest(label):
                self.reset_fixture()
                persisted, herdr2, sup2 = self.interrupted(indicator=False, status="blocked")
                arrange(herdr2)
                with sup2.store.transaction():
                    outcome = sup2.resume_interrupted_turn(sup2.store.read_state(), "claude")
                self.assertEqual(outcome, "stop", label)
                self.assertEqual(herdr2.sent_keys, [], label)
                self.assertEqual(herdr2.prompts, [], label)

    def test_dismissal_keys_are_held_by_any_matched_working_rule(self) -> None:
        for rule_id in FakeHerdr.WORKING_RULE_IDS:
            with self.subTest(rule_id):
                self.reset_fixture()
                persisted, herdr2, sup2 = self.interrupted(indicator=False, status="blocked")
                herdr2.explain_matched = {rule_id}
                with sup2.store.transaction():
                    outcome = sup2.resume_interrupted_turn(sup2.store.read_state(), "claude")
                self.assertEqual(outcome, "stop")
                self.assertEqual(herdr2.sent_keys, [], rule_id)

    def test_dismissal_keys_need_the_background_rule_to_be_evaluated(self) -> None:
        persisted, herdr2, sup2 = self.interrupted(indicator=False, status="blocked")
        herdr2.explain_omit = {BACKGROUND}
        with sup2.store.transaction():
            outcome = sup2.resume_interrupted_turn(sup2.store.read_state(), "claude")
        self.assertEqual(outcome, "stop")
        self.assertEqual(herdr2.sent_keys, [])

    def test_dismissal_keys_still_sent_when_no_indicator_positive_control(self) -> None:
        persisted, herdr2, sup2 = self.interrupted(indicator=False, status="blocked")

        def after_keys(fake, name):
            fake.agents[name]["agent_status"] = "idle"
            fake.outputs[name] = self.claude_frame(persisted)

        herdr2.on_wait = after_keys
        with sup2.store.transaction():
            sup2.resume_interrupted_turn(sup2.store.read_state(), "claude")
        self.assertEqual(herdr2.sent_keys, [("w3:p1", ["Enter"])])
        self.assertEqual(herdr2.prompts, [])

    def test_ready_branch_does_not_read_or_route_while_an_indicator_holds(self) -> None:
        persisted, herdr2, sup2 = self.interrupted(indicator=False, status="idle")
        herdr2.outputs["w3:p1"] = self.claude_frame(persisted)
        counter = {"n": 0}
        reads_during: list[int] = []

        def script(fake, name):
            counter["n"] += 1
            holding = counter["n"] <= 3
            fake.explain_matched = {BACKGROUND} if holding else set()
            fake.explain_state = "working" if holding else "idle"
            if holding:
                reads_during.append(len(fake.reads))

        herdr2.on_explain = script
        with sup2.store.transaction():
            state = sup2.store.read_state()
            sup2.resume_interrupted_turn(state, "claude")
        self.assertEqual(reads_during, [0, 0, 0])
        self.assertEqual(herdr2.prompts, [], "the frame is routed from the original delivery, no continuation is sent")


class FollowupAndRetryTests(GuardCase):
    def to_claude_decision(self) -> dict:
        """gated_v2 starts with Codex; after plan approval Claude holds a pending human question, so Claude is the active agent."""
        self.write_plan()
        self.assertEqual(self.start_gated([{"v2": ("plan", "human", "plan_approval", str(self.plan_payload()))}]), 4)
        self.approve_pending()
        self.assertEqual(self.resume_with([{"v2": ("brief", "claude")}, {"v2": ("brief", "human", "generic_question", str(self.question_payload()))}]), 2)
        state = self.state()
        self.assertEqual(state["active_agent"], "claude")
        return state

    def to_claude_missing_result(self) -> dict:
        self.write_plan()
        self.assertEqual(self.start_gated([{"v2": ("plan", "human", "plan_approval", str(self.plan_payload()))}]), 4)
        self.approve_pending()
        self.assertEqual(self.resume_with([{"v2": ("brief", "claude")}, {"output": "prose only, no block\n", "status": "idle"}]), 2)
        state = self.state()
        self.assertIsNotNone(state["missing_result"])
        self.assertEqual(state["active_agent"], "claude")
        return state

    def ask(self, question: str = "Why?") -> None:
        st = self.state()
        decision = self.sup.followup_decision(st)
        command = {"request_id": "ask-0001", "action": "ask_agent", "run_id": st["run_id"], "question": question, "actor": "telegram:1", "chat_id": 9,
                   "decision_id": decision["decision_id"], "expected_state": st["supervisor_state"], "artifact_sha256": decision["artifact_sha256"], "payload_sha256": decision["payload_sha256"]}
        self.sup.enqueue_command(command)
        self.sup.process_inbox()

    def test_followup_is_delivered_only_after_the_window_and_answer_read_after_a_second_window(self) -> None:
        self.to_claude_decision()
        self.ask()
        self.herdr.responses = [{"followup": {"summary": "answered"}}]
        before = len(self.herdr.prompts)
        self.assertEqual(self.sup.worker(), 2)
        self.assertEqual(len(self.herdr.prompts), before + 1)
        purposes = [c["purpose"] for c in self.events_named("settle_guard_cleared")]
        self.assertIn("followup_delivery", purposes)
        self.assertIn("followup_read", purposes)

    def test_followup_with_an_indicator_present_is_not_sent(self) -> None:
        self.to_claude_decision()
        self.ask()
        self.herdr.on_explain = lambda fake, name: (setattr(fake, "explain_matched", {BACKGROUND}), setattr(fake, "explain_state", "working"))
        self.herdr.responses = []
        prompts = len(self.herdr.prompts)
        self.assertEqual(self.sup.worker(), 2)
        self.assertEqual(len(self.herdr.prompts), prompts)
        state = self.state()
        self.assertEqual((state["agent_followup"]["status"], state["agent_followup"]["delivery_uncertain"]), ("FAILED", False))
        self.assertIn("quiet settled state", state["wait_user_reason"])

    def test_followup_answer_is_not_read_while_an_indicator_is_present(self) -> None:
        self.to_claude_decision()
        self.ask()
        reads_before = len(self.herdr.reads)
        n = {"i": 0}

        def script(fake, name):
            if not fake.prompts or len(fake.prompts) < 2:
                return
            n["i"] += 1
            on = n["i"] <= 3
            fake.explain_matched = {BACKGROUND} if on else set()
            fake.explain_state = "working" if on else "idle"

        self.herdr.on_explain = script
        self.herdr.responses = [{"followup": {"summary": "answered"}}]
        self.assertEqual(self.sup.worker(), 2)
        self.assertGreater(n["i"], 3)
        self.assertEqual(len(self.herdr.reads), reads_before + 1, "exactly one settled read, after the indicator cleared")

    def test_followup_reread_waits_for_a_quiet_pane_and_keeps_the_one_time_reread(self) -> None:
        self.to_claude_decision()
        self.ask()
        self.herdr.responses = [{"output": "I answered in prose only.\n", "status": "idle"}]
        self.assertEqual(self.sup.worker(), 2)
        state = self.state()
        turn = state["agent_followup"]["followup_turn_id"]
        self.assertEqual((state["agent_followup"]["status"], state["agent_followup"]["delivery_uncertain"]), ("FAILED", True))
        command = {"action": "retry_followup_response", "run_id": state["run_id"], "followup_turn_id": turn}
        self.herdr.explain_matched, self.herdr.explain_state = {BACKGROUND}, "working"
        reads = len(self.herdr.reads)
        with self.sup.store.transaction():
            with self.assertRaisesRegex(hs.SupervisorError, "background children"):
                self.sup.apply_command(self.sup.store.read_state(), command)
        self.assertEqual(len(self.herdr.reads), reads, "nothing read while the indicator is present")
        self.assertEqual(self.state()["agent_followup"]["reread_attempts"], 0, "a refused reread does not consume the one-time reread")
        self.herdr.explain_matched, self.herdr.explain_state = set(), "idle"
        with self.sup.store.transaction():
            result = self.sup.apply_command(self.sup.store.read_state(), command)
        self.assertFalse(result["ok"])
        self.assertEqual(self.state()["agent_followup"]["reread_attempts"], 1)
        self.assertEqual(len(self.herdr.reads), reads + 1)

    def test_retry_routing_result_does_not_read_while_an_indicator_is_present(self) -> None:
        state = self.to_claude_missing_result()
        self.herdr.outputs["claude-main"] = FakeHerdr.block_v2(state["run_id"], state["delivery"]["turn_id"], "brief", "human", "generic_question", str(self.question_payload()))
        self.herdr.on_explain = lambda fake, name: (setattr(fake, "explain_matched", {BACKGROUND}), setattr(fake, "explain_state", "working"))
        reads = len(self.herdr.reads)
        with self.sup.store.transaction():
            st = self.sup.store.read_state()
            with self.assertRaisesRegex(hs.SupervisorError, "background children"):
                self.sup.apply_command(st, {"action": "retry_routing_result", "run_id": st["run_id"], "turn_id": st["missing_result"]["turn_id"]})
        self.assertEqual(len(self.herdr.reads), reads, "nothing read, routed or resent")
        prompts_after_hold = len(self.herdr.prompts)
        # positive control: with no indicator the same retry recovers the gate
        self.herdr.on_explain = None
        self.herdr.explain_matched, self.herdr.explain_state = set(), "idle"
        with self.sup.store.transaction():
            st = self.sup.store.read_state()
            result = self.sup.apply_command(st, {"action": "retry_routing_result", "run_id": st["run_id"], "turn_id": st["missing_result"]["turn_id"]})
        self.assertTrue(result["ok"], result)
        self.assertEqual(len(self.herdr.prompts), prompts_after_hold)
        recovered = self.state()
        self.assertIsNone(recovered["missing_result"])
        self.assertEqual(recovered["pending_gate"]["gate_type"], "generic_question")


if __name__ == "__main__":
    unittest.main()
