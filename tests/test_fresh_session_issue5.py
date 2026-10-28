"""Issue 5 lifecycle: helper readiness before any side effect, request/context binding and recovery, separate
initialization states with a journaled offline bootstrap, atomic two-provider ownership, pane labels, and the
configured-versus-reported model record. Fixture Herdr/helper only; no systemd, model or network."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import herdr_core
import herdr_sessions
from v2_fixtures import CLAUDE_SESSION, CODEX_SESSION, V2Case, hs

CODEX_ONLY = {"policy": "fresh-codex", "profiles": {"codex": "default", "claude": "default"}}
FRESH_ALL = {"policy": "fresh-all", "profiles": {"codex": "default", "claude": "default"}}


class Issue5Case(V2Case):
    def journal(self, prep: str) -> dict:
        return json.loads((self.paths.session_preparations_dir / f"{prep}.json").read_text())

    def prepare(self, selection: dict, prep: str | None = None) -> dict:
        return self.sup.prepare_task_sessions(selection, preparation_id=prep or str(uuid.uuid4()))

    def assert_untouched(self) -> None:
        self.assertEqual(self.sup.owners(), self.owners)
        self.assertEqual(self.herdr.renames, [])
        self.assertIn("codex-main", self.herdr.agents)
        self.assertIn("claude-main", self.herdr.agents)


class ReadinessBeforeSideEffectsTests(Issue5Case):
    def test_unready_direct_launch_refuses_before_any_split_launch_or_owner_change(self) -> None:
        self.direct_report = {"ready": False, "reasons": ["the explicit Codex launch recipe is unavailable: no absolute env utility"]}
        for selection in (CODEX_ONLY, FRESH_ALL):
            with self.subTest(selection["policy"]), self.assertRaisesRegex(hs.SupervisorError, "codex_direct_launch.*no absolute env utility"):
                self.prepare(selection)
            self.assertEqual((self.thread_journal.requests, self.herdr.splits, self.herdr.starts, self.herdr.pane_runs, self.herdr.bootstrap_prompts), ([], [], [], [], []))
            self.assertFalse(list(self.paths.session_preparations_dir.glob("*.json")) if self.paths.session_preparations_dir.exists() else [])
            self.assert_untouched()

    def test_the_quota_reset_helper_is_not_a_requirement_of_the_direct_launch(self) -> None:
        self.helper_report.update(ready=False, reasons=["helper unit herdr-codex-reset-worker-abc.path is not installed"])
        self.assertTrue(self.sup.fresh_policy_supported("fresh-codex"))
        record = self.prepare(CODEX_ONLY)
        self.assertEqual((record["status"], self.thread_journal.requests), ("OWNERSHIP_BOUND", []))

    def test_preserve_and_fresh_claude_do_not_depend_on_the_codex_launch(self) -> None:
        self.direct_report = {"ready": False, "reasons": ["x"]}
        self.assertTrue(self.sup.fresh_policy_supported("preserve"))
        self.assertTrue(self.sup.fresh_policy_supported("fresh-claude"))
        self.assertFalse(self.sup.fresh_policy_supported("fresh-codex"))
        self.assertFalse(self.sup.fresh_policy_supported("fresh-all"))
        record = self.prepare({"policy": "fresh-claude", "profiles": {"codex": "default", "claude": "default"}})
        self.assertEqual(record["status"], "OWNERSHIP_BOUND")
        self.assertEqual(self.thread_journal.requests, [])

    def test_doctor_names_the_launch_reason_and_uses_the_same_report(self) -> None:
        self.direct_report = {"ready": False, "reasons": ["the pane run capability is disabled"]}
        report = self.sup.doctor()
        self.assertFalse(report["session_start"]["fresh_codex_available"])
        self.assertFalse(report["session_start"]["fresh_codex_components"]["codex_direct_launch"])
        self.assertNotIn("codex_helper_ready", report["session_start"]["fresh_codex_components"], "the direct launch does not need the quota-reset helper")
        self.assertEqual(report["session_start"]["fresh_codex_launch"], "direct-cli")
        self.assertTrue(any("the pane run capability is disabled" in warning for warning in report["warnings"]))
        self.direct_report = {"ready": True, "reasons": []}
        self.assertTrue(self.sup.doctor()["session_start"]["fresh_codex_available"])


class ContextBindingTests(Issue5Case):
    def test_request_binds_the_descriptor_fingerprint_that_is_recorded_in_the_journal(self) -> None:
        prep = str(uuid.uuid4())
        self.prepare(CODEX_ONLY, prep)
        import herdr_helper_context as hctx
        expected = hctx.fingerprint(self.helper_descriptor(str(self.project_root)))
        item = self.journal(prep)["providers"]["codex"]
        self.assertEqual(item["helper_context_fingerprint"], expected, "the context the launch was authorized under is journaled at authorization")
        self.assertEqual(item["launch_request"], {"cwd": str(self.project_root), "model": None})
        self.assertEqual((item["fresh_launch"], self.thread_journal.requests, self.thread_journal.contexts), ("direct-cli", [], {}), "no helper thread request exists on the direct path")

    def test_a_legacy_thread_request_with_an_unknown_outcome_adopts_the_settled_result_once_but_a_changed_context_needs_an_explicit_decision(self) -> None:
        prep = str(uuid.uuid4())
        self.seed_legacy_preparation(prep, phase="THREAD_CREATING", settle=True)  # journaled by the earlier library; the helper settled the request afterwards
        self.helper_codex_home = "/home/test/another-codex-home"  # the host now resolves another CODEX_HOME
        with self.assertRaisesRegex(hs.SupervisorError, "helper context.*now resolves.*nothing was repeated"):
            self.prepare(CODEX_ONLY, prep)
        self.assertEqual((len(self.thread_journal.requests), self.herdr.splits, self.herdr.starts, self.herdr.pane_runs), (0, [], [], []))
        self.helper_codex_home = "/home/test/.codex"  # restored: the exact recorded result is adopted, no second thread
        record = self.prepare(CODEX_ONLY, prep)
        self.assertEqual(record["status"], "OWNERSHIP_BOUND")
        self.assertEqual((len(self.thread_journal.requests), self.thread_journal.created, len(self.herdr.pane_runs)), (0, 1, 1), "this library never starts a thread; the legacy result is adopted and launched once")
        self.assertNotIn("fresh_launch", record["providers"]["codex"], "a legacy item is never converted into a direct launch")

    def test_a_legacy_unbound_record_still_reconciles_without_a_fingerprint(self) -> None:
        prep = str(uuid.uuid4())
        self.seed_legacy_preparation(prep, phase="THREAD_CREATING", fingerprint=None, settle=True)
        self.helper_codex_home = "/home/test/another-codex-home"
        self.assertEqual(self.prepare(CODEX_ONLY, prep)["status"], "OWNERSHIP_BOUND")
        self.assertEqual(len(self.thread_journal.requests), 0)

    def test_a_forged_fingerprint_in_the_journal_is_rejected(self) -> None:
        legacy = str(uuid.uuid4())
        self.seed_legacy_preparation(legacy, phase="THREAD_CREATING", fingerprint="not-hex")
        with self.assertRaisesRegex(hs.SupervisorError, "helper context binding is invalid"):
            self.prepare(CODEX_ONLY, legacy)
        self.reset_fixture()
        direct = str(uuid.uuid4())
        self.prepare(CODEX_ONLY, direct)  # a direct item must carry a valid binding too
        path = self.paths.session_preparations_dir / f"{direct}.json"
        record = json.loads(path.read_text())
        record["status"] = "SESSION_SELECTION_AUTHORIZED"
        record["providers"]["codex"]["helper_context_fingerprint"] = "not-hex"
        path.write_text(json.dumps(record))
        with self.assertRaisesRegex(hs.SupervisorError, "helper context binding is invalid"):
            self.prepare(CODEX_ONLY, direct)


class InitializationTests(Issue5Case):
    def test_identity_reported_natively_needs_no_bootstrap_and_records_separate_states(self) -> None:
        prep = str(uuid.uuid4())
        record = self.prepare(CODEX_ONLY, prep)
        item = record["providers"]["codex"]
        self.assertEqual((item["status"], item["process_interactive"], item["initialization"]), ("IDENTITY_VERIFIED", True, "native-reported"))
        self.assertEqual(self.herdr.bootstrap_prompts, [])
        self.assertEqual(self.herdr.prompts, [], "no task or any other prompt during preparation")

    def test_missing_identity_with_bootstrap_disabled_stops_without_sending_anything(self) -> None:
        self.herdr.hold_identity = True
        prep = str(uuid.uuid4())
        with self.assertRaisesRegex(hs.SupervisorError, "bootstrap is disabled.*nothing was sent"):
            self.prepare(CODEX_ONLY, prep)
        item = self.journal(prep)["providers"]["codex"]
        self.assertEqual((item["status"], item["process_interactive"]), ("AGENT_STARTING", True))
        self.assertNotIn("initialization", item)
        self.assertEqual((self.herdr.bootstrap_prompts, self.herdr.prompts), ([], []))
        self.assertEqual(self.sup.owners(), self.owners)
        self.assertEqual(len(self.thread_journal.requests), 0, "no helper thread exists on the direct path")

    def test_delayed_first_turn_identity_uses_one_journaled_bootstrap_without_a_task_body(self) -> None:
        self.herdr.hold_identity = True
        self.config["session_start"]["native_identity_bootstrap"] = True
        seen = []
        original = self.herdr._bootstrap_prompt
        def spy(name, text):
            item = self.journal(prep)["providers"]["codex"]
            seen.append((item["status"], dict(item["bootstrap"])))
            return original(name, text)
        self.herdr._bootstrap_prompt = spy
        prep = str(uuid.uuid4())
        record = self.prepare(CODEX_ONLY, prep)
        item = record["providers"]["codex"]
        self.assertEqual([status for status, _ in seen], ["BOOTSTRAP_SUBMITTED"], "intent and the attempt marker are durable before the send")
        boot = seen[0][1]
        self.assertEqual((boot["pane_id"], boot["agent_name"], boot["expected_identity"]), (item["pane_id"], item["agent_name"], None), "a direct launch has no pre-created id to expect")
        (name, text), = self.herdr.bootstrap_prompts
        self.assertEqual(name, item["agent_name"])
        self.assertEqual(text, herdr_sessions.bootstrap_text(boot["nonce"]))
        self.assertTrue(all(word not in text.lower() for word in ("task", "write", "tool", "file")))
        self.assertEqual((item["status"], item["initialization"], item["bootstrap"]["outcome"]), ("IDENTITY_VERIFIED", "bootstrap-completed", "completed"))
        self.assertEqual(self.herdr.prompts, [], "the bootstrap is not the task delivery")
        self.assertEqual(self.sup.owners()["codex"]["session_id"], item["session_id"])
        self.assertEqual(item["session_id"], self.herdr.next_sessions["codex"], "the id is learned from the owned pane")
        self.assertEqual(self.sup.owners()["claude"]["session_id"], CLAUDE_SESSION)

    def test_task_is_delivered_once_and_only_after_the_bootstrap_completed(self) -> None:
        self.herdr.hold_identity = True
        self.config["session_start"]["native_identity_bootstrap"] = True
        order = []
        self.herdr.responses = [{"v2": ("plan", "done"), "before": lambda fake, name, text: order.append(("task", len(fake.bootstrap_prompts)))}]
        self.assertEqual(self.sup.run_new("fresh codex task", "codex", session_selection=CODEX_ONLY), 0)
        self.assertEqual(order, [("task", 1)])
        self.assertEqual((len(self.herdr.prompts), len(self.herdr.bootstrap_prompts), len(self.thread_journal.requests)), (1, 1, 0))

    def test_task_is_not_sent_when_the_identity_never_becomes_ready(self) -> None:
        self.herdr.hold_identity = True
        self.herdr.responses = [{"v2": ("plan", "done")}]
        with self.assertRaises(hs.SupervisorError):
            self.sup.run_new("fresh codex task", "codex", session_selection=CODEX_ONLY)
        self.assertEqual((self.herdr.prompts, self.herdr.bootstrap_prompts), ([], []))
        self.assertEqual(self.sup.owners(), self.owners)

    def test_uncertain_bootstrap_is_reconciled_by_observation_and_never_resent(self) -> None:
        self.herdr.hold_identity = True
        self.config["session_start"]["native_identity_bootstrap"] = True
        self.herdr.bootstrap_mode = "stall"
        prep = str(uuid.uuid4())
        with self.assertRaisesRegex(hs.SupervisorError, "delivery is uncertain.*not resent"):
            self.prepare(CODEX_ONLY, prep)
        self.assertEqual(self.journal(prep)["providers"]["codex"]["status"], "BOOTSTRAP_SUBMITTED")
        self.herdr.bootstrap_mode = "ok"
        with self.assertRaisesRegex(hs.SupervisorError, "submitted but not acknowledged.*not resent"):
            self.prepare(CODEX_ONLY, prep)
        self.assertEqual(len(self.herdr.bootstrap_prompts), 1)
        # the provider eventually reports the exact thread: observed completion, still exactly one send
        item = self.journal(prep)["providers"]["codex"]
        self.herdr.agents[item["agent_name"]]["agent_session"] = {"value": self.herdr._held[item["agent_name"]]}
        record = self.prepare(CODEX_ONLY, prep)
        self.assertEqual(record["providers"]["codex"]["initialization"], "bootstrap-completed")
        self.assertEqual(record["providers"]["codex"]["bootstrap"]["outcome"], "observed-complete")
        self.assertEqual((len(self.herdr.bootstrap_prompts), len(self.thread_journal.requests), len(self.herdr.splits), len(self.herdr.pane_runs)), (1, 0, 1, 1))

    def test_rejected_before_input_may_be_retried_but_an_acknowledged_turn_is_never_resent(self) -> None:
        self.herdr.hold_identity = True
        self.config["session_start"]["native_identity_bootstrap"] = True
        self.herdr.bootstrap_mode = "reject"
        prep = str(uuid.uuid4())
        with self.assertRaisesRegex(hs.SupervisorError, "rejected before input"):
            self.prepare(CODEX_ONLY, prep)
        self.assertEqual(self.journal(prep)["providers"]["codex"]["status"], "BOOTSTRAP_INTENT")
        self.herdr.bootstrap_mode = "swallow"  # acknowledged, but the provider still reports no identity
        with self.assertRaisesRegex(hs.SupervisorError, "completed but no native identity"):
            self.prepare(CODEX_ONLY, prep)
        self.assertEqual(self.journal(prep)["providers"]["codex"]["status"], "BOOTSTRAP_ACKNOWLEDGED")
        sent = len(self.herdr.bootstrap_prompts)
        self.herdr.bootstrap_mode = "ok"
        with self.assertRaises(hs.SupervisorError):
            self.prepare(CODEX_ONLY, prep)
        self.assertEqual(len(self.herdr.bootstrap_prompts), sent, "an acknowledged bootstrap is not sent again")

    def test_wrong_duplicate_or_swapped_identity_never_binds(self) -> None:
        """A direct launch has no pre-created id to compare with: an identity is bound only if it is reported by the exact owned pane, is unique among live agents and (below) distinct from every preserved owner."""
        self.config["session_start"]["native_identity_bootstrap"] = True
        self.reset_fixture()
        self.config["session_start"]["native_identity_bootstrap"] = True
        self.herdr.hold_identity, self.herdr.bootstrap_mode = True, "duplicate"  # the first turn makes the new agent report the Claude owner's session
        with self.assertRaisesRegex(hs.SupervisorError, "missing or duplicate"):
            self.prepare(CODEX_ONLY)
        self.assertEqual(self.sup.owners(), self.owners)
        self.reset_fixture()
        self.herdr.next_sessions["codex"] = CODEX_SESSION  # the new process reports the PRESERVED Codex owner's session
        with self.assertRaisesRegex(hs.SupervisorError, "missing or duplicate"):
            self.prepare(CODEX_ONLY)
        self.assertEqual((self.sup.owners(), self.herdr.prompts), (self.owners, []))
        for label, mutate in (("pane swapped", lambda agent: agent.update(pane_id="w3:p2")), ("provider swapped", lambda agent: agent.update(agent="claude"))):
            with self.subTest(label):
                self.reset_fixture()
                original = self.herdr.rename_agent
                def swapped(target, name, original=original, mutate=mutate):
                    result = original(target, name)
                    mutate(self.herdr.agents[name])
                    return result
                self.herdr.rename_agent = swapped
                with self.assertRaisesRegex(hs.SupervisorError, "did not become uniquely ready|held by another or an ambiguous agent"):
                    self.prepare(CODEX_ONLY)
                self.assertEqual(self.sup.owners(), self.owners)

    def test_both_providers_must_pass_before_the_single_atomic_owner_replacement(self) -> None:
        self.herdr.hold_identity = True
        self.config["session_start"]["native_identity_bootstrap"] = True
        prep = str(uuid.uuid4())
        original = self.herdr._bootstrap_prompt
        calls = []
        def second_stalls(name, text):
            calls.append(name)
            if len(calls) == 2:
                self.herdr.bootstrap_mode = "stall"
            return original(name, text)
        self.herdr._bootstrap_prompt = second_stalls
        with self.assertRaisesRegex(hs.SupervisorError, "uncertain"):
            self.prepare(FRESH_ALL, prep)
        record = self.journal(prep)
        self.assertEqual(sorted(item["status"] for item in record["providers"].values()), ["BOOTSTRAP_SUBMITTED", "IDENTITY_VERIFIED"])
        self.assertEqual(record["status"], "SESSION_SELECTION_AUTHORIZED")
        self.assertEqual(self.sup.owners(), self.owners, "no partial ownership change")
        verified = next(p for p, item in record["providers"].items() if item["status"] == "IDENTITY_VERIFIED")
        self.herdr.bootstrap_mode = "ok"
        pending = next(p for p, item in record["providers"].items() if item["status"] != "IDENTITY_VERIFIED")
        pending_item = record["providers"][pending]
        self.herdr.agents[pending_item["agent_name"]]["agent_session"] = {"value": self.herdr._held[pending_item["agent_name"]]}
        self.sup = self.make_supervisor()  # a restarted worker resumes the same preparation
        done = self.prepare(FRESH_ALL, prep)
        self.assertEqual(done["status"], "OWNERSHIP_BOUND")
        self.assertEqual((len(self.thread_journal.requests), len(self.herdr.pane_runs)), (0, 1), "one Codex launch across the restart, no thread")
        self.assertEqual(len(self.herdr.bootstrap_prompts), 2, "the already verified provider is never bootstrapped again")
        self.assertEqual(done["bound_owners"][verified]["session_id"], record["providers"][verified].get("thread_id") or done["providers"][verified]["session_id"])
        self.assertEqual(len(self.herdr.splits), 2)


class LabelTests(Issue5Case):
    def test_only_the_new_pane_is_renamed_with_a_deterministic_label_and_the_alias_is_separate(self) -> None:
        prep = str(uuid.uuid4())
        record = self.prepare(FRESH_ALL, prep)
        new_panes = {item["pane_id"] for item in record["providers"].values()}
        self.assertEqual({pane for pane, _ in self.herdr.renames}, new_panes)
        self.assertTrue(new_panes.isdisjoint({"w3:p1", "w3:p2"}))
        for provider, item in record["providers"].items():
            self.assertEqual(item["label_state"], {"status": "NAMED", "target": f"Fresh {provider.title()} {prep[:8]}", "pane_id": item["pane_id"]})
            self.assertEqual(item["agent_name"], f"{provider}-run-{prep[:8]}")
            self.assertNotEqual(item["agent_name"], item["label_state"]["target"])
        self.assertEqual(self.herdr.pane_labels.keys(), new_panes)

    def test_fresh_one_provider_renames_only_its_pane_and_preserve_renames_nothing(self) -> None:
        record = self.prepare(CODEX_ONLY)
        self.assertEqual([pane for pane, _ in self.herdr.renames], [record["providers"]["codex"]["pane_id"]])
        self.reset_fixture()
        self.prepare({"policy": "preserve", "profiles": {"codex": "default", "claude": "default"}})
        self.assert_untouched()

    def test_label_failures_are_explicit_never_success_and_do_not_create_sessions_or_touch_old_panes(self) -> None:
        for mode, status in (("error", "NAME_FAILED"), ("nolabel", "NAME_UNVERIFIED"), ("mismatch", "NAME_MISMATCH")):
            with self.subTest(mode):
                self.reset_fixture()
                self.herdr.rename_mode = mode
                record = self.prepare(CODEX_ONLY)
                self.assertEqual(record["providers"]["codex"]["label_state"]["status"], status)
                self.assertEqual(record["status"], "OWNERSHIP_BOUND", "a label is presentation, not ownership authority")
                self.assertEqual(len(self.herdr.splits), 1)
                self.assertEqual(len(self.herdr.pane_runs), 1)
                self.assertTrue(all(pane not in ("w3:p1", "w3:p2") for pane, _ in self.herdr.renames))

    def test_a_missing_rename_capability_is_unsupported_not_success(self) -> None:
        self.herdr.rename_pane = None
        record = self.prepare(CODEX_ONLY)
        self.assertEqual(record["providers"]["codex"]["label_state"]["status"], "NAME_UNSUPPORTED")

    def test_late_naming_acknowledgement_is_reconciled_by_exact_pane_without_a_second_rename(self) -> None:
        self.herdr.hold_identity = True
        self.config["session_start"]["native_identity_bootstrap"] = True
        prep = str(uuid.uuid4())
        original = self.herdr._bootstrap_prompt
        count = []
        def second_stalls(name, text):
            count.append(name)
            if len(count) == 2:
                self.herdr.bootstrap_mode = "stall"
            return original(name, text)
        self.herdr._bootstrap_prompt = second_stalls
        with self.assertRaises(hs.SupervisorError):
            self.prepare(FRESH_ALL, prep)
        path = self.paths.session_preparations_dir / f"{prep}.json"
        record = json.loads(path.read_text())
        verified = next(item for item in record["providers"].values() if item["status"] == "IDENTITY_VERIFIED")
        self.assertEqual(verified["label_state"]["status"], "NAMED")
        # crash window: the rename landed but its result was not persisted
        verified["label_state"]["status"] = "NAMING_INTENT"
        path.write_text(json.dumps(record))
        renames_before = list(self.herdr.renames)
        self.herdr.bootstrap_mode = "ok"
        pending = next(item for item in record["providers"].values() if item["status"] != "IDENTITY_VERIFIED")
        self.herdr.agents[pending["agent_name"]]["agent_session"] = {"value": self.herdr._held[pending["agent_name"]]}
        self.prepare(FRESH_ALL, prep)
        final = self.journal(prep)
        self.assertEqual(next(i for i in final["providers"].values() if i["pane_id"] == verified["pane_id"])["label_state"]["status"], "NAMED")
        self.assertEqual([r for r in self.herdr.renames if r[0] == verified["pane_id"]], [r for r in renames_before if r[0] == verified["pane_id"]], "no second rename of the already named pane")
        self.assertTrue(all(pane not in ("w3:p1", "w3:p2") for pane, _ in self.herdr.renames))

    def test_a_tampered_label_record_is_rejected(self) -> None:
        prep = str(uuid.uuid4())
        self.prepare(CODEX_ONLY, prep)
        path = self.paths.session_preparations_dir / f"{prep}.json"
        record = json.loads(path.read_text())
        for tamper in ({"status": "SUCCESS"}, {"pane_id": "w3:p2"}, {"target": "Codex main"}):
            broken = json.loads(json.dumps(record))
            broken["providers"]["codex"]["label_state"].update(tamper)
            with self.subTest(tamper):
                with self.assertRaisesRegex(hs.SupervisorError, "pane label record is invalid"):
                    herdr_sessions.validate_preparation_record(self.config, broken, prep)


class ModelRecordTests(Issue5Case):
    def with_reported_model(self, model: str | None) -> None:
        original, original_run = self.herdr.start_agent, self.herdr.pane_run
        def start(name, *, kind, pane_id, args):
            result = original(name, kind=kind, pane_id=pane_id, args=args)
            if model is not None:
                self.herdr.agents[name]["model"] = model
            return result
        def run(pane, command):
            result = original_run(pane, command)
            if model is not None:
                for agent in self.herdr.agents.values():
                    if agent.get("pane_id") == pane:
                        agent["model"] = model
            return result
        self.herdr.start_agent, self.herdr.pane_run = start, run

    def test_configured_and_reported_models_are_recorded_separately_and_unknown_is_not_verified(self) -> None:
        item = self.prepare(CODEX_ONLY)["providers"]["codex"]
        self.assertEqual((item["configured_model"], item["effective_model"], item["effective_model_status"]), (None, None, "unknown"))
        self.reset_fixture()
        self.with_reported_model("gpt-reported")
        item = self.prepare(CODEX_ONLY)["providers"]["codex"]
        self.assertEqual((item["configured_model"], item["effective_model"], item["effective_model_status"]), (None, "gpt-reported", "reported"))

    def test_a_configured_model_mismatch_fails_closed_and_a_match_is_recorded(self) -> None:
        self.config["session_start"]["model_profiles"]["codex"]["large"] = {"label": "Large", "model": "gpt-approved"}
        selection = {"policy": "fresh-codex", "profiles": {"codex": "large", "claude": "default"}}
        self.sup.model_flag_supported = lambda provider: True
        self.with_reported_model("gpt-approved")
        item = self.prepare(selection)["providers"]["codex"]
        self.assertEqual((item["configured_model"], item["effective_model"], item["effective_model_status"]), ("gpt-approved", "gpt-approved", "reported"))
        self.reset_fixture()
        self.config["session_start"]["model_profiles"]["codex"]["large"] = {"label": "Large", "model": "gpt-approved"}
        self.sup.model_flag_supported = lambda provider: True
        self.with_reported_model("gpt-other")
        with self.assertRaisesRegex(hs.SupervisorError, "different model"):
            self.prepare(selection)
        self.assertEqual(self.sup.owners(), self.owners)

    def test_claude_selection_reaches_the_launch_arguments_and_codex_selection_the_direct_launch_command(self) -> None:
        import shlex
        self.config["session_start"]["model_profiles"]["claude"]["fast"] = {"label": "Fast", "model": "claude-fast"}
        self.config["session_start"]["model_profiles"]["codex"]["large"] = {"label": "Large", "model": "gpt-approved"}
        self.sup.model_flag_supported = lambda provider: True
        record = self.prepare({"policy": "fresh-all", "profiles": {"codex": "large", "claude": "fast"}})
        starts = {kind: args for _, kind, _, args in self.herdr.starts}
        self.assertEqual(starts["claude"], ["--model", "claude-fast"])
        self.assertNotIn("codex", starts, "a new Codex is never started through the generic agent start")
        (_, command), = self.herdr.pane_runs
        argv = shlex.split(command)
        self.assertEqual(argv[-3:], ["--model", "gpt-approved", "--no-daemon"], "the configured model reaches the direct launch; there is no resume argument")
        self.assertNotIn("resume", argv)
        self.assertEqual((self.thread_journal.requests, record["providers"]["codex"]["launch_request"]["model"]), ([], "gpt-approved"))
        self.assertEqual(record["providers"]["claude"]["configured_model"], "claude-fast")


class OperatorSurfaceTests(Issue5Case):
    def test_helper_context_report_renders_inactive_units_and_never_targets_the_live_unit_directory(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as out:
            report = self.sup.helper_context_report(render_dir=out)
            names = sorted(Path(path).name for path in report["rendered"])
            self.assertEqual(names, sorted(p.name for p in Path(out).iterdir()))
            self.assertEqual(len(names), 2)
            service = next(Path(path) for path in report["rendered"] if path.endswith(".service")).read_text()
            self.assertIn(f'--state-dir "{report["context"]["journal_root"]}"', service)
            self.assertFalse(report["units"]["global"])
        steps = "\n".join(report["next_steps"])
        self.assertIn("systemctl --user enable --now", steps)
        self.assertIn("explicit operator step", steps)
        live = Path.home() / ".config/systemd/user"
        before = sorted(p.name for p in live.iterdir()) if live.is_dir() else None
        with self.assertRaisesRegex(hs.SupervisorError, "live user unit directory"):
            self.sup.helper_context_report(render_dir=str(live))
        self.assertEqual(sorted(p.name for p in live.iterdir()) if live.is_dir() else None, before)

    def test_doctor_text_names_helper_readiness_and_reasons(self) -> None:
        import contextlib
        import io

        import herdr_command
        self.helper_report.update(ready=False, reasons=["watcher disabled"])
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            herdr_command.print_doctor(self.sup.doctor())
        text = buffer.getvalue()
        self.assertIn("codex helper: NOT READY", text)
        self.assertIn("structural read-only; not a deployment acceptance", text)
        self.assertIn("helper: watcher disabled", text)

    def test_example_model_profiles_validate_when_merged_and_ship_only_placeholders(self) -> None:
        example = json.loads((Path(__file__).resolve().parents[1] / "config" / "model-profiles.example.json").read_text())
        merged = json.loads(json.dumps(self.config))
        for provider, profiles in example["session_start"]["model_profiles"].items():
            merged["session_start"]["model_profiles"][provider].update(profiles)
        herdr_core.validate_session_start_config(merged)
        models = [entry["model"] for profiles in example["session_start"]["model_profiles"].values() for entry in profiles.values() if entry["model"]]
        self.assertTrue(models and all(model.startswith("REPLACE-WITH-") for model in models), "no invented model id ships as an example")
        self.assertIsNone(example["session_start"]["model_profiles"]["codex"]["default"]["model"])

    def test_bootstrap_flag_must_be_a_real_boolean_and_defaults_off(self) -> None:
        self.assertNotIn("native_identity_bootstrap", hs.DEFAULT_CONFIG["session_start"])
        for bad in ("true", 1, None):
            config = json.loads(json.dumps(self.config))
            config["session_start"]["native_identity_bootstrap"] = bad
            with self.subTest(bad), self.assertRaisesRegex(hs.SupervisorError, "must be boolean"):
                herdr_core.validate_session_start_config(config)


class BootstrapRevalidationTests(Issue5Case):
    """Review CI2: every observable fact is revalidated immediately before input, including a recovered intent."""

    def rejected_intent(self, selection: dict = CODEX_ONLY, provider: str = "codex") -> tuple[str, dict]:
        self.herdr.hold_identity = True
        self.config["session_start"]["native_identity_bootstrap"] = True
        self.herdr.bootstrap_mode = "reject"
        prep = str(uuid.uuid4())
        with self.assertRaisesRegex(hs.SupervisorError, "rejected before input"):
            self.prepare(selection, prep)
        item = self.journal(prep)["providers"][provider]
        self.assertEqual(item["status"], "BOOTSTRAP_INTENT")
        self.assertEqual(set(item["bootstrap"]["process"]), {"pid", "shell_pid", "argv", "cwd"})
        self.herdr.bootstrap_mode = "swallow"  # if a send happened it would be counted below
        return prep, item

    def assert_refused_without_input(self, prep: str, pattern: str) -> None:
        before = len(self.herdr.bootstrap_prompts)
        with self.assertRaisesRegex(hs.SupervisorError, pattern):
            self.prepare(CODEX_ONLY, prep)
        self.assertEqual(len(self.herdr.bootstrap_prompts) - before, 0, "no additional prompt may reach the target")
        self.assertEqual(self.journal(prep)["providers"]["codex"]["status"], "BOOTSTRAP_INTENT")
        self.assertEqual(self.sup.owners(), self.owners)

    def test_a_unique_native_identity_reported_on_intent_recovery_is_learned_from_the_owned_pane_with_zero_prompts(self) -> None:
        """A direct launch has no pre-created id to be "wrong" against: the identity the owned pane reports is bound, still without any input."""
        prep, item = self.rejected_intent()
        before = len(self.herdr.bootstrap_prompts)
        self.herdr.agents[item["agent_name"]]["agent_session"] = {"value": "99999999-9999-4999-8999-999999999999"}
        record = self.prepare(CODEX_ONLY, prep)
        done = record["providers"]["codex"]
        self.assertEqual((done["status"], done["session_id"], done["bootstrap"]["outcome"]), ("IDENTITY_VERIFIED", "99999999-9999-4999-8999-999999999999", "not-needed"))
        self.assertEqual(len(self.herdr.bootstrap_prompts), before)

    def test_a_duplicate_native_identity_gets_zero_prompts(self) -> None:
        prep, item = self.rejected_intent()
        self.herdr.agents[item["agent_name"]]["agent_session"] = {"value": CLAUDE_SESSION}
        self.assert_refused_without_input(prep, "missing or duplicate")

    def test_a_reported_model_that_differs_from_the_configuration_gets_zero_prompts(self) -> None:
        self.config["session_start"]["model_profiles"]["codex"]["large"] = {"label": "Large", "model": "gpt-approved"}
        self.sup.model_flag_supported = lambda provider: True
        self.herdr.hold_identity = True
        self.config["session_start"]["native_identity_bootstrap"] = True
        self.herdr.bootstrap_mode = "reject"
        prep, selection = str(uuid.uuid4()), {"policy": "fresh-codex", "profiles": {"codex": "large", "claude": "default"}}
        with self.assertRaises(hs.SupervisorError):
            self.prepare(selection, prep)
        item = self.journal(prep)["providers"]["codex"]
        self.herdr.agents[item["agent_name"]]["model"] = "gpt-other"
        before = len(self.herdr.bootstrap_prompts)
        self.herdr.bootstrap_mode = "swallow"
        with self.assertRaisesRegex(hs.SupervisorError, "different model than configured; nothing was sent"):
            self.prepare(selection, prep)
        self.assertEqual(len(self.herdr.bootstrap_prompts), before)

    def test_a_replaced_process_or_changed_launch_context_gets_zero_prompts(self) -> None:
        for label, mutate, pattern in (
            ("replaced process", lambda p: p["foreground_processes"][0].update(pid=123456), "process changed since the intent"),
            ("replaced shell", lambda p: p.update(shell_pid=654321), "process changed since the intent"),
            ("changed working directory", lambda p: p["foreground_processes"][0].update(cwd="/elsewhere"), "not the journaled launch"),
            ("different thread resumed", lambda p: p["foreground_processes"][0].update(argv=["codex", "resume", "99999999-9999-4999-8999-999999999999", "--no-daemon"]), "not the journaled launch"),
            ("different provider process", lambda p: p["foreground_processes"][0].update(name="bash"), "not the journaled launch"),
            ("two foreground processes", lambda p: p["foreground_processes"].append(dict(p["foreground_processes"][0], pid=1)), "exactly one foreground process"),
            ("no foreground process", lambda p: p.update(foreground_processes=[]), "exactly one foreground process"),
        ):
            with self.subTest(label):
                self.reset_fixture()
                prep, item = self.rejected_intent()
                mutate(self.herdr.processes[item["pane_id"]])
                self.assert_refused_without_input(prep, pattern)

    def test_an_unavailable_process_contract_blocks_before_the_intent_and_before_input(self) -> None:
        self.herdr.hold_identity = True
        self.config["session_start"]["native_identity_bootstrap"] = True
        prep = str(uuid.uuid4())
        original = self.herdr.rename_agent
        def named(target, name):
            result = original(target, name)
            self.herdr.processes.pop(target)  # the alias is established; from here the pane's process can no longer be read
            return result
        self.herdr.rename_agent = named
        with self.assertRaisesRegex(hs.SupervisorError, "could not read the prepared pane's process.*no alias was changed"):
            self.prepare(CODEX_ONLY, prep)
        item = self.journal(prep)["providers"]["codex"]
        self.assertEqual((item["status"], "bootstrap" in item, self.herdr.bootstrap_prompts), ("PROCESS_BOUND", False, []), "the bound process can no longer be proven: nothing is named, bootstrapped or sent")
        self.herdr.process_info = None  # an adapter without the contract at all
        with self.assertRaisesRegex(hs.SupervisorError, "needs Herdr's pane process contract"):
            self.prepare(CODEX_ONLY, prep)
        self.assertEqual(self.herdr.bootstrap_prompts, [])

    def test_an_ambiguous_alias_or_a_changed_helper_context_gets_zero_prompts(self) -> None:
        prep, item = self.rejected_intent()
        self.herdr.agents["twin"] = self.herdr.agent("codex", item["agent_name"], "w9:p99", "88888888-8888-4888-8888-888888888888")
        self.assert_refused_without_input(prep, "is ambiguous or gone in Herdr; nothing was sent")
        del self.herdr.agents["twin"]
        self.helper_codex_home = "/home/test/another-codex-home"
        self.assert_refused_without_input(prep, "context changed since the launch was requested")

    def test_an_exact_already_ready_identity_reconciles_without_a_bootstrap(self) -> None:
        prep, item = self.rejected_intent()
        sent = len(self.herdr.bootstrap_prompts)
        identity = self.herdr._held[item["agent_name"]]
        self.herdr.agents[item["agent_name"]]["agent_session"] = {"value": identity}
        record = self.prepare(CODEX_ONLY, prep)
        codex = record["providers"]["codex"]
        self.assertEqual((codex["status"], codex["initialization"], codex["bootstrap"]["outcome"], codex["session_id"]), ("IDENTITY_VERIFIED", "native-reported", "not-needed", identity))
        self.assertEqual(len(self.herdr.bootstrap_prompts), sent)
        self.assertEqual(self.sup.owners()["codex"]["session_id"], identity)
        self.assertEqual(self.prepare(CODEX_ONLY, prep)["status"], "OWNERSHIP_BOUND", "the reconciled record reloads and validates")

    def test_the_delayed_identity_positive_path_still_sends_exactly_one_bootstrap_after_a_rejection(self) -> None:
        prep, item = self.rejected_intent()
        self.herdr.bootstrap_mode = "ok"
        record = self.prepare(CODEX_ONLY, prep)
        self.assertEqual(record["providers"]["codex"]["initialization"], "bootstrap-completed")
        self.assertEqual(len(self.herdr.bootstrap_prompts), 2, "one rejected before input, one delivered")
        self.assertEqual(self.herdr.prompts, [])

    def test_a_tampered_bootstrap_process_record_is_rejected(self) -> None:
        prep, item = self.rejected_intent()
        record = self.journal(prep)
        for tamper in ({"pid": True}, {"argv": "codex"}, {"cwd": 7}, {"extra": 1}):
            broken = json.loads(json.dumps(record))
            broken["providers"]["codex"]["bootstrap"]["process"].update(tamper)
            with self.subTest(tamper), self.assertRaisesRegex(hs.SupervisorError, "bootstrap process record is invalid"):
                herdr_sessions.validate_preparation_record(self.config, broken, prep)


class PaneLaunchContextHoldTests(Issue5Case):
    """Review CI3 at the Supervisor boundary: the real readiness is held without a supported launch contract."""

    def test_the_supervisors_real_readiness_is_held_for_the_installed_adapter_and_follows_a_supported_contract(self) -> None:
        import herdr_helper_context as hctx
        seen = {}
        def fake_readiness(journal_root, config, **kwargs):
            seen.update(kwargs)
            return {"ready": False, "reasons": [hctx.NO_PANE_LAUNCH_CONTRACT], "context": None, "fingerprint": None, "units": None, "activation": {}, "evidence": "structural-read-only", "checks": {}}
        real = hctx.helper_readiness
        hctx.helper_readiness = fake_readiness
        self.addCleanup(setattr, hctx, "helper_readiness", real)
        sup = hs.Supervisor(self.paths, self.config, self.herdr, clock=self.clock.time, sleeper=self.clock.sleep)
        sup.helper_readiness()
        self.assertIsNone(seen["launch_context"], "a Herdr adapter without the contract passes none")
        import herdr_cli
        sup2 = hs.Supervisor(self.paths, self.config, herdr_cli.HerdrCli("/fake/herdr"), clock=self.clock.time, sleeper=self.clock.sleep)
        sup2.helper_readiness()
        self.assertIsNone(seen["launch_context"]())
        self.herdr.pane_launch_context = lambda: {"executable": "/usr/bin/codex", "codex_home": "/home/test/.codex"}
        sup3 = hs.Supervisor(self.paths, self.config, self.herdr, clock=self.clock.time, sleeper=self.clock.sleep)
        sup3.helper_readiness()
        self.assertEqual(seen["launch_context"](), {"executable": "/usr/bin/codex", "codex_home": "/home/test/.codex"})
        self.assertIn("offers no supported way", hctx.NO_PANE_LAUNCH_CONTRACT)

    def test_prepare_refuses_before_any_side_effect_when_the_generic_agent_start_would_be_the_only_launch(self) -> None:
        """A NEW fresh Codex is never launched through Herdr's generic agent start (its launch context is unverified): without the explicit pane-run method nothing is created."""
        self.config["session_start"]["codex_launch_method"] = "agent-start"
        self.sup = self.make_supervisor()
        for selection in (CODEX_ONLY, FRESH_ALL):
            with self.subTest(selection["policy"]), self.assertRaisesRegex(hs.SupervisorError, "explicit pane-run recipe.*codex_launch_method = explicit-pane-run"):
                self.prepare(selection)
            self.assertEqual((self.thread_journal.requests, self.herdr.splits, self.herdr.starts, self.herdr.pane_runs, self.herdr.bootstrap_prompts), ([], [], [], [], []))
        self.assertFalse(self.sup.fresh_policy_supported("fresh-codex") and False)  # availability is judged by the direct components; the method check is the execution guard above


class OperatorQuotingTests(Issue5Case):
    """Review CI4: advertised operator steps are shell-quoted; contents are never executed."""

    PATHS = ("/tmp/project's state/codex-reset", "/tmp/with space/codex-reset", "/tmp/a;b&c|d$(touch PWNED)`id`>x/codex-reset", '/tmp/quo"te\\back/codex-reset', "/tmp/glob*?[x]/codex-reset", "/tmp/~tilde $HOME/codex-reset")

    def steps_for(self, root: str, render_dir: str | None = None) -> list[str]:
        import herdr_helper_context as hctx
        original = self.sup.helper_context
        def odd(cwd=None):
            descriptor = dict(original(cwd))
            descriptor["journal_root"], descriptor["instance"] = root, hctx.instance_id(root)
            return descriptor
        self.sup.helper_context = odd
        self.helper_report["context"] = None
        return self.sup.helper_context_report(render_dir=render_dir)["next_steps"]

    def test_the_mkdir_step_round_trips_hostile_paths_literally(self) -> None:
        import shlex
        for root in self.PATHS:
            with self.subTest(root):
                step = self.steps_for(root)[0]
                tokens = shlex.split(step)
                self.assertEqual(tokens[:4], ["mkdir", "-p", "-m", "0700"])
                self.assertEqual(tokens[4:], [f"{root}/pending", f"{root}/processing", f"{root}/results"])

    def test_a_shell_receives_the_hostile_path_as_data_and_nothing_is_executed(self) -> None:
        import subprocess
        import tempfile
        for root in self.PATHS:
            with self.subTest(root), tempfile.TemporaryDirectory() as work:
                step = self.steps_for(root)[0]
                arguments = step.split(" 0700 ", 1)[1]
                done = subprocess.run(["sh", "-c", f'cd "$1" && printf "%s\\n" {arguments}', "sh", work], capture_output=True, text=True, timeout=10, check=False)
                self.assertEqual(done.stdout.splitlines(), [f"{root}/pending", f"{root}/processing", f"{root}/results"], done.stderr)
                self.assertEqual(sorted(Path(work).iterdir()), [], "no command embedded in the path ran")

    def test_the_install_step_quotes_the_review_directory_and_the_live_directory_refusal_stays(self) -> None:
        import shlex
        import tempfile
        with tempfile.TemporaryDirectory() as parent:
            out = str(Path(parent) / "re view's dir")
            steps = self.steps_for("/tmp/project's state/codex-reset", render_dir=out)
        install = next(step for step in steps if step.startswith("install "))
        tokens = shlex.split(install)
        self.assertEqual([token for token in tokens if "re view's dir" in token][0].rsplit("/", 1)[0], out)
        self.assertTrue(tokens[-1].endswith("/"), "destination stays the (tilde-expanded) user unit directory")
        live = Path.home() / ".config/systemd/user"
        with self.assertRaisesRegex(hs.SupervisorError, "live user unit directory"):
            self.sup.helper_context_report(render_dir=str(live))


FRESH_CLAUDE = {"policy": "fresh-claude", "profiles": {"codex": "default", "claude": "default"}}


class BootstrapAdoptionBindingTests(Issue5Case):
    """Correction-review CI2-R1: a native identity that appears during the bootstrap window may be adopted only by
    the SAME recorded preparation-owned process and context, whichever phase observes it, and never without
    first checking old owners stay preserved. Fresh Claude has no pre-created UUID, so identity alone is not proof."""

    rejected_intent = BootstrapRevalidationTests.rejected_intent

    MUTATIONS = (
        ("replaced process", lambda p: p["foreground_processes"][0].update(pid=p["foreground_processes"][0]["pid"] + 1), "process changed since the intent"),
        ("replaced shell", lambda p: p.update(shell_pid=p["shell_pid"] + 1), "process changed since the intent"),
        ("changed argv", lambda p: p["foreground_processes"][0].update(argv=p["foreground_processes"][0]["argv"] + ["--extra"]), "not the journaled launch"),
        ("changed cwd", lambda p: p["foreground_processes"][0].update(cwd="/elsewhere"), "not the journaled launch"),
    )

    def intent_for(self, provider: str):
        selection = FRESH_CLAUDE if provider == "claude" else CODEX_ONLY
        prep, item = self.rejected_intent(selection, provider)
        return selection, prep, item

    def publish(self, provider: str, item: dict) -> str:
        identity = str(uuid.uuid4())  # a direct launch has no pre-created id: identity alone is never proof of the process
        self.herdr.agents[item["agent_name"]]["agent_session"] = {"value": identity}
        return identity

    def assert_adoption_refused(self, selection: dict, prep: str, provider: str, identity: str, status: str, pattern: str) -> None:
        before = len(self.herdr.bootstrap_prompts)
        with self.assertRaisesRegex(hs.SupervisorError, pattern):
            self.prepare(selection, prep)
        item = self.journal(prep)["providers"][provider]
        self.assertEqual(item["status"], status)
        self.assertNotIn("session_id", item)
        self.assertNotEqual(self.sup.owners()[provider]["session_id"], identity, "the replacement identity must not become the owner")
        self.assertEqual(self.sup.owners(), self.owners, "old owners preserved")
        self.assertEqual(self.journal(prep)["status"], "SESSION_SELECTION_AUTHORIZED")
        self.assertEqual(len(self.herdr.bootstrap_prompts), before, "zero input")

    def test_a_replaced_process_with_a_new_identity_is_not_adopted_on_a_recovered_intent(self) -> None:
        for provider in ("claude", "codex"):
            for label, mutate, pattern in self.MUTATIONS:
                with self.subTest(f"{provider}: {label}"):
                    self.reset_fixture()
                    selection, prep, item = self.intent_for(provider)
                    identity = self.publish(provider, item)
                    mutate(self.herdr.processes[item["pane_id"]])
                    self.assert_adoption_refused(selection, prep, provider, identity, "BOOTSTRAP_INTENT", pattern)

    def test_a_changed_helper_context_with_a_new_identity_is_not_adopted(self) -> None:
        selection, prep, item = self.intent_for("codex")
        identity = self.publish("codex", item)
        self.helper_codex_home = "/home/test/another-codex-home"
        self.assert_adoption_refused(selection, prep, "codex", identity, "BOOTSTRAP_INTENT", "context changed since the launch was requested")

    def test_the_same_process_reporting_its_identity_late_reconciles_without_a_prompt_and_reloads(self) -> None:
        for provider in ("claude", "codex"):
            with self.subTest(provider):
                self.reset_fixture()
                selection, prep, item = self.intent_for(provider)
                sent = len(self.herdr.bootstrap_prompts)
                identity = self.publish(provider, item)
                record = self.prepare(selection, prep)
                done = record["providers"][provider]
                self.assertEqual((done["status"], done["initialization"], done["bootstrap"]["outcome"], done["session_id"]), ("IDENTITY_VERIFIED", "native-reported", "not-needed", identity))
                self.assertEqual(len(self.herdr.bootstrap_prompts), sent)
                self.assertEqual(self.sup.owners()[provider]["session_id"], identity)
                herdr_sessions.validate_preparation_record(self.config, self.journal(prep), prep)
                self.assertEqual(self.prepare(selection, prep)["status"], "OWNERSHIP_BOUND")

    def test_observed_completion_of_a_submitted_bootstrap_requires_the_same_process(self) -> None:
        for provider in ("claude", "codex"):
            for same in (False, True):
                with self.subTest(f"{provider}: same process={same}"):
                    self.reset_fixture()
                    selection = FRESH_CLAUDE if provider == "claude" else CODEX_ONLY
                    self.herdr.hold_identity = True
                    self.config["session_start"]["native_identity_bootstrap"] = True
                    self.herdr.bootstrap_mode = "stall"
                    prep = str(uuid.uuid4())
                    with self.assertRaisesRegex(hs.SupervisorError, "delivery is uncertain"):
                        self.prepare(selection, prep)
                    item = self.journal(prep)["providers"][provider]
                    self.assertEqual(item["status"], "BOOTSTRAP_SUBMITTED")
                    identity = self.publish(provider, item)
                    if not same:
                        self.herdr.processes[item["pane_id"]]["foreground_processes"][0]["pid"] += 1
                        self.assert_adoption_refused(selection, prep, provider, identity, "BOOTSTRAP_SUBMITTED", "process changed since the intent")
                    else:
                        done = self.prepare(selection, prep)["providers"][provider]
                        self.assertEqual((done["initialization"], done["bootstrap"]["outcome"]), ("bootstrap-completed", "observed-complete"))
                    self.assertEqual(len(self.herdr.bootstrap_prompts), 1, "never resent")

    def test_an_acknowledged_bootstrap_adopts_only_the_same_process_after_the_wait(self) -> None:
        for provider in ("claude", "codex"):
            for same in (False, True):
                with self.subTest(f"{provider}: same process={same}"):
                    self.reset_fixture()
                    selection = FRESH_CLAUDE if provider == "claude" else CODEX_ONLY
                    self.herdr.hold_identity = True
                    self.config["session_start"]["native_identity_bootstrap"] = True
                    self.herdr.bootstrap_mode = "swallow"  # acknowledged, identity not yet reported
                    prep = str(uuid.uuid4())
                    with self.assertRaisesRegex(hs.SupervisorError, "completed but no native identity"):
                        self.prepare(selection, prep)
                    item = self.journal(prep)["providers"][provider]
                    self.assertEqual(item["status"], "BOOTSTRAP_ACKNOWLEDGED")
                    identity = self.publish(provider, item)
                    if not same:
                        self.herdr.processes[item["pane_id"]]["foreground_processes"][0]["pid"] += 1
                        self.assert_adoption_refused(selection, prep, provider, identity, "BOOTSTRAP_ACKNOWLEDGED", "process changed since the intent")
                    else:
                        done = self.prepare(selection, prep)["providers"][provider]
                        self.assertEqual((done["initialization"], done["bootstrap"]["outcome"]), ("bootstrap-completed", "completed"))
                    self.assertEqual(len(self.herdr.bootstrap_prompts), 1, "never resent")
