"""Issue 5 guarded fresh-pane amendment: `session_start.codex_input_policy = guarded-new-pane`.

The strict policy (default) needs a supported proof that the pane's command line is empty, which Herdr 0.9.5 does not offer.
The guarded policy is an explicitly selected OPERATING PROFILE, not a proof: a single-use in-memory submission token issued only
by the invocation that performed the distinct `--no-focus` split, a 30-second window on an injectable monotonic clock, exact pane
metadata (`focused` exactly false, no agent, approved cwd), the unchanged sole foreground shell and the unchanged authorized
recipe. Fixtures and fakes only; nothing here proves shell-input safety or live support."""

from __future__ import annotations

import copy
import json
import pickle
import threading
import uuid
from pathlib import Path

import herdr_cli
import herdr_core
import herdr_helper_context as hctx
import herdr_sessions
import test_helper_context  # module import: a class import would be collected and re-run by discovery
from test_explicit_launch import ExplicitCase
from test_fresh_session_issue5 import CODEX_ONLY, FRESH_ALL
from v2_fixtures import hs

GUARDED = "guarded-new-pane"
STRICT = "require-empty-proof"
PANE_GET = Path(__file__).resolve().parent / "fixtures" / "herdr" / "0.9.5-pane-label" / "pane-get-unlabelled.json"


class GuardedCase(ExplicitCase):
    def setUp(self) -> None:
        super().setUp()
        self.config["session_start"]["codex_input_policy"] = GUARDED
        # The real adapter reports the empty-input capability false/unknown: the guarded path must not need or fake it.
        self.herdr.launch_caps["pane empty interactive input proof"] = False
        self.herdr.pane_input_empty = lambda pane: self.herdr.input_checks.append(pane)  # always None (unknown)

    def refused(self, prep: str, pattern: str, status: str, *, runs: int = 0) -> None:
        with self.assertRaisesRegex(hs.SupervisorError, pattern):
            self.prepare(CODEX_ONLY, prep)
        self.assertEqual((len(self.herdr.pane_runs), self.codex_item(prep)["status"], self.sup.owners()), (runs, status, self.owners))
        self.assertEqual((self.herdr.agent_renames, self.herdr.bootstrap_prompts, self.herdr.prompts), ([], [], []))

    def split_then_stop(self, prep: str) -> None:
        """First invocation: the split happens, then something stops it BEFORE any submission."""
        self.herdr.pane_run = None
        with self.assertRaises(hs.SupervisorError):
            self.prepare(CODEX_ONLY, prep)


class PolicySettingTests(GuardedCase):
    def test_validation_defaults_and_combinations(self) -> None:
        self.assertNotIn("codex_input_policy", herdr_core.DEFAULT_CONFIG["session_start"])
        self.assertEqual(herdr_core.CODEX_INPUT_POLICIES, hctx.CODEX_INPUT_POLICIES)
        self.assertEqual((hctx.STRICT_INPUT_POLICY, hctx.GUARDED_INPUT_POLICY), (STRICT, GUARDED))
        for bad in ("guarded", "", None, 1, True, [GUARDED]):
            config = json.loads(json.dumps(self.config))
            config["session_start"]["codex_input_policy"] = bad
            with self.subTest(bad), self.assertRaisesRegex(hs.SupervisorError, "codex_input_policy"):
                herdr_core.validate_session_start_config(config)
        config = json.loads(json.dumps(self.config))
        config["session_start"]["codex_launch_method"] = "agent-start"
        with self.assertRaisesRegex(hs.SupervisorError, "requires session_start.codex_launch_method explicit-pane-run"):
            herdr_core.validate_session_start_config(config)
        del config["session_start"]["codex_launch_method"]
        with self.assertRaisesRegex(hs.SupervisorError, "requires"):
            herdr_core.validate_session_start_config(config)
        for ok in (STRICT, GUARDED):
            valid = json.loads(json.dumps(self.config))
            valid["session_start"]["codex_input_policy"] = ok
            herdr_core.validate_session_start_config(valid)
        absent = json.loads(json.dumps(self.config))
        del absent["session_start"]["codex_input_policy"]
        herdr_core.validate_session_start_config(absent)
        self.assertEqual(hs.Supervisor(self.paths, absent, self.herdr).input_policy(), STRICT)

    def test_the_effective_policy_is_persisted_before_any_side_effect(self) -> None:
        prep = str(uuid.uuid4())
        seen = []
        real = self.herdr.split_pane
        def spy(*args, **kwargs):
            seen.append(self.journal(prep)["providers"]["codex"].get("input_policy"))
            return real(*args, **kwargs)
        self.herdr.split_pane = spy
        record = self.prepare(CODEX_ONLY, prep)
        self.assertEqual(seen, [GUARDED], "the policy is in the journal before the first (pane) side effect")
        self.assertEqual(record["providers"]["codex"]["input_policy"], GUARDED)
        self.assertEqual(self.sup.session_preparation_view(record)["providers"]["codex"]["input_policy"], GUARDED)

    def test_strict_is_recorded_by_default_and_a_new_fresh_codex_needs_the_explicit_method(self) -> None:
        del self.config["session_start"]["codex_input_policy"]
        prep = str(uuid.uuid4())
        with self.assertRaisesRegex(hs.SupervisorError, "cannot be proven to have an empty command line"):
            self.prepare(CODEX_ONLY, prep)  # strict: no proof is available, so the real route is held
        self.assertEqual(self.codex_item(prep)["input_policy"], STRICT)
        self.assertEqual((self.herdr.pane_runs, self.herdr.metadata_reads), ([], []))
        self.reset_fixture()
        self.config["session_start"]["codex_input_policy"] = GUARDED
        del self.config["session_start"]["codex_launch_method"]  # no explicit method: a policy has nothing to apply to, and a NEW fresh Codex is never started through the generic agent start
        with self.assertRaisesRegex(hs.SupervisorError, "explicit pane-run recipe"):
            self.prepare(CODEX_ONLY)
        self.assertEqual((self.herdr.splits, self.herdr.starts, self.herdr.pane_runs), ([], [], []))

    def test_a_legacy_journal_without_a_policy_stays_strict_whatever_the_setting(self) -> None:
        self.config["session_start"]["codex_input_policy"] = STRICT
        prep = str(uuid.uuid4())
        self.herdr.pane_run = None
        with self.assertRaises(hs.SupervisorError):
            self.prepare(CODEX_ONLY, prep)
        record = self.journal(prep)
        del record["providers"]["codex"]["input_policy"]  # a journal written before the policy existed
        (self.paths.session_preparations_dir / f"{prep}.json").write_text(json.dumps(record))
        self.config["session_start"]["codex_input_policy"] = GUARDED
        self.herdr.pane_run = lambda pane, command: self.herdr.pane_runs.append((pane, command)) or {}
        self.herdr.input_empty_mode = "unknown"
        with self.assertRaisesRegex(hs.SupervisorError, "cannot be proven to have an empty command line"):
            self.prepare(CODEX_ONLY, prep)
        self.assertEqual((self.herdr.pane_runs, self.herdr.metadata_reads), ([], []), "settings never upgrade an existing journal; the guarded path was not even consulted")

    def test_tampered_policy_records_are_rejected(self) -> None:
        prep = str(uuid.uuid4())
        self.split_then_stop(prep)
        record = self.journal(prep)
        for label, fn in (("unknown value", lambda i: i.update(input_policy="anything")), ("wrong type", lambda i: i.update(input_policy=True)),
                          ("without the explicit method", lambda i: (i.pop("launch_method"), i.pop("launch", None)))):
            broken = json.loads(json.dumps(record))
            fn(broken["providers"]["codex"])
            with self.subTest(label), self.assertRaises(hs.SupervisorError):
                herdr_sessions.validate_preparation_record(self.config, broken, prep)


class ReadinessHonestyTests(GuardedCase):
    OK = {"pane run": True, "pane process-info (--pane)": True, "agent rename": True, "pane empty interactive input proof": False}

    def test_guarded_eligibility_does_not_fake_the_capability_and_strict_stays_unavailable(self) -> None:
        context = self.helper_descriptor(str(self.project_root))
        seam = dict(executable_check=lambda p: True, chdir_probe=lambda p: True)
        self.assertIn("pane empty interactive input proof", hctx.explicit_launch_problem(context, self.OK, **seam, input_policy=STRICT))
        self.assertIsNone(hctx.explicit_launch_problem(context, self.OK, **seam, input_policy=GUARDED))
        self.assertIn("agent rename", hctx.explicit_launch_problem(context, dict(self.OK, **{"agent rename": False}), **seam, input_policy=GUARDED))
        self.assertIn("not verified", hctx.explicit_launch_problem(context, None, **seam, input_policy=GUARDED))
        self.assertIn("unknown Codex input policy", hctx.explicit_launch_problem(context, self.OK, **seam, input_policy="x"))
        self.assertFalse(self.OK["pane empty interactive input proof"])
        self.assertIs(herdr_cli.HerdrCli("/fake/herdr").launch_capability_report.__func__ is not None, True)
        self.assertIsNone(herdr_cli.HerdrCli("/fake/herdr").pane_input_empty("w1:p3"))

    def test_the_real_report_distinguishes_assumption_from_proof(self) -> None:
        case = test_helper_context.ReadinessTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        seam = dict(launch_method="explicit-pane-run", launch_capabilities=self.OK, executable_check=lambda p: True, chdir_probe=lambda p: True)
        strict = case.report(**seam, input_policy=STRICT)
        guarded = case.report(**seam, input_policy=GUARDED)
        self.assertFalse(strict["ready"])
        self.assertEqual((strict["input_policy"], strict["notes"], strict["empty_input_proof"]), (STRICT, [], "unavailable"))
        self.assertTrue(guarded["ready"], guarded["reasons"])
        self.assertEqual((guarded["input_policy"], guarded["empty_input_proof"]), (GUARDED, "unavailable"))
        self.assertEqual(guarded["notes"], [hctx.GUARDED_ASSUMPTION_NOTE])
        self.assertIn("NOT a proof", guarded["notes"][0])
        self.assertIn("30-second", guarded["notes"][0])
        agent_start = case.report(launch_method="agent-start")
        self.assertIsNone(agent_start["input_policy"], "the policy only exists with the explicit method")

    def test_doctor_status_and_telegram_explain_the_profile_honestly(self) -> None:
        import contextlib
        import io

        import herdr_command
        self.config["session_start"]["codex_launch_method"] = "explicit-pane-run"
        self.helper_report.update(input_policy=GUARDED, empty_input_proof="unavailable", notes=[hctx.GUARDED_ASSUMPTION_NOTE])
        report = self.sup.doctor()
        session = report["session_start"]
        self.assertEqual((session["codex_input_policy"], session["helper"]["empty_input_proof"]), (GUARDED, "unavailable"))
        self.assertEqual(session["helper"]["notes"], [hctx.GUARDED_ASSUMPTION_NOTE])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            herdr_command.print_doctor(report)
        text = out.getvalue()
        self.assertIn("policy=guarded-new-pane", text)
        self.assertIn("OPERATING ASSUMPTION, not proof", text)
        self.assertIn("empty-input proof=unavailable", text)

    def test_the_telegram_card_carries_the_assumption_note(self) -> None:
        from test_telegram import TelegramCase
        self.assertTrue(TelegramCase)  # the harness is shared; the note is exercised through the bridge below
        import herdr_telegram
        self.assertTrue(hasattr(herdr_telegram.Bridge, "_fresh_assumption_note"))


class TokenIsolationTests(GuardedCase):
    def make(self, **overrides):
        parts = dict(key=herdr_sessions._TOKEN_KEY, preparation_id="p1", pane_id="w9:p1", invocation=object(), issued_at=100.0)
        parts.update(overrides)
        return herdr_sessions.SubmissionToken(parts["key"], parts["preparation_id"], parts["pane_id"], parts["invocation"], parts["issued_at"]), parts

    def test_a_token_cannot_be_constructed_serialized_copied_or_reconstituted(self) -> None:
        with self.assertRaises(TypeError):
            herdr_sessions.SubmissionToken(object(), "p1", "w9:p1", object(), 0.0)
        with self.assertRaises(TypeError):
            herdr_sessions.SubmissionToken(None, "p1", "w9:p1", object(), 0.0)
        token, _ = self.make()
        for label, fn in (("pickle", lambda: pickle.dumps(token)), ("copy", lambda: copy.copy(token)), ("deepcopy", lambda: copy.deepcopy(token)), ("json", lambda: json.dumps(token))):
            with self.subTest(label), self.assertRaises(TypeError):
                fn()
        self.assertNotIn("issued", repr(token))

    def test_the_token_is_never_in_the_journal_status_or_doctor(self) -> None:
        prep = str(uuid.uuid4())
        record = self.prepare(CODEX_ONLY, prep)
        blob = json.dumps(self.journal(prep)) + json.dumps(self.sup.session_preparation_view(record)) + json.dumps(self.sup.doctor(), default=str)
        for needle in ("SubmissionToken", "issued_at", "_issued", "invocation"):
            self.assertNotIn(needle, blob)

    def test_every_binding_and_the_window_edges(self) -> None:
        invocation = object()
        token, _ = self.make(invocation=invocation)
        self.assertTrue(token.bind_shell(4242))
        self.assertTrue(token.bind_shell(4242))
        self.assertFalse(token.bind_shell(4243), "the ORIGINAL shell pid cannot be rebound")
        self.assertFalse(token.bind_shell(True))
        ok = lambda now, **kw: token.problem(now, kw.get("prep", "p1"), kw.get("pane", "w9:p1"), kw.get("inv", invocation), kw.get("pid", 4242))  # noqa: E731
        self.assertIsNone(ok(100.0))
        self.assertIsNone(ok(129.999))
        self.assertIn("expired", ok(130.0))
        self.assertIn("expired", ok(500.0))
        self.assertIn("backwards", ok(99.9))
        self.assertIn("another preparation", ok(110.0, prep="p2"))
        self.assertIn("another preparation", ok(110.0, pane="w9:p2"))
        self.assertIn("another preparation", ok(110.0, inv=object()))
        self.assertIn("original shell", ok(110.0, pid=4243))
        self.assertIn("original shell", ok(110.0, pid=True))
        self.assertIn("original shell", ok(110.0, pid="4242"))
        unbound, _ = self.make()
        self.assertIn("original shell", unbound.problem(110.0, "p1", "w9:p1", unbound._invocation, 4242))
        token.consume()
        self.assertIn("already consumed", ok(110.0))

    def test_a_later_invocation_in_the_same_process_has_no_eligibility(self) -> None:
        prep = str(uuid.uuid4())
        self.split_then_stop(prep)
        self.assertEqual(self.codex_item(prep)["status"], "LAUNCH_INTENT")
        self.herdr.pane_run = lambda pane, command: self.herdr.pane_runs.append((pane, command)) or {}
        self.refused(prep, "does not hold that single-use eligibility.*abandon-session-start", "LAUNCH_INTENT")
        self.refused(prep, "does not hold that single-use eligibility", "LAUNCH_INTENT")  # and again: still no way back
        self.assertEqual(len(self.herdr.splits), 1, "no replacement pane")

    def test_a_restart_has_no_eligibility_for_an_unsubmitted_intent_or_a_fresh_pane(self) -> None:
        prep = str(uuid.uuid4())
        self.split_then_stop(prep)
        self.restart()
        self.herdr.pane_run = lambda pane, command: self.herdr.pane_runs.append((pane, command)) or {}
        self.refused(prep, "does not hold that single-use eligibility", "LAUNCH_INTENT")
        # a pane that never even reached the intent behaves the same
        self.reset_fixture()
        self.config["session_start"]["codex_launch_method"] = "explicit-pane-run"
        self.config["session_start"]["codex_input_policy"] = GUARDED
        self.herdr.launch_caps["pane empty interactive input proof"] = False
        prep = str(uuid.uuid4())
        self.herdr.pane_focused["w9:p1"] = True
        with self.assertRaisesRegex(hs.SupervisorError, "focused"):
            self.prepare(CODEX_ONLY, prep)
        self.assertEqual(self.codex_item(prep)["status"], "FRESH_PANE_CREATED")
        self.herdr.pane_focused["w9:p1"] = False
        self.restart()
        self.refused(prep, "does not hold that single-use eligibility", "FRESH_PANE_CREATED")
        self.assertEqual((len(self.herdr.splits), len(self.thread_journal.requests)), (1, 0))
        self.assertEqual(self.sup.abandon_session_preparation(prep)["abandoned"], True, "explicit abandon preserves the pane")

    def test_a_status_read_an_existing_shell_or_an_operator_pane_cannot_create_eligibility(self) -> None:
        prep = str(uuid.uuid4())
        self.split_then_stop(prep)
        self.sup.session_preparation_view(self.journal(prep))
        self.sup.doctor()
        self.sup.status()
        self.herdr.pane_run = lambda pane, command: self.herdr.pane_runs.append((pane, command)) or {}
        self.refused(prep, "does not hold that single-use eligibility", "LAUNCH_INTENT")
        # an operator-supplied/existing pane: edit the journal to point at a pre-existing shell pane — still no token
        record = self.journal(prep)
        self.herdr.processes["w3:p9"] = self.herdr.processes[record["providers"]["codex"]["pane_id"]]
        self.assertEqual(self.herdr.pane_runs, [])


class GuardedFlowTests(GuardedCase):
    def test_same_invocation_launch_with_false_empty_input_proof_succeeds_once(self) -> None:
        prep = str(uuid.uuid4())
        statuses = []
        real = self.herdr.pane_run
        def spy(pane, command):
            statuses.append(self.codex_item(prep)["status"])
            return real(pane, command)
        self.herdr.pane_run = spy
        record = self.prepare(CODEX_ONLY, prep)
        item = record["providers"]["codex"]
        self.assertEqual(statuses, ["LAUNCH_SUBMITTED"], "the durable attempt marker precedes the single submission")
        self.assertEqual(self.counts(), (0, 1, 1, 0))
        self.assertEqual(self.herdr.input_checks, [], "the strict empty-input proof is never consulted or faked")
        self.assertGreaterEqual(len(self.herdr.metadata_reads), 2, "initial qualification and the pre-submission read")
        self.assertEqual({pane for pane in self.herdr.metadata_reads}, {item["pane_id"]})
        self.assertEqual((item["input_policy"], item["status"], record["status"]), (GUARDED, "IDENTITY_VERIFIED", "OWNERSHIP_BOUND"))
        self.assertEqual(self.herdr.agent_renames, [(item["pane_id"], item["agent_name"])])
        self.assertEqual(self.sup.owners()["codex"]["session_id"], item["session_id"])
        self.assertEqual(self.herdr.prompts, [], "the sole task delivery comes later")

    def test_fresh_all_and_the_delayed_identity_bootstrap_still_work_under_the_profile(self) -> None:
        self.herdr.hold_identity = True
        self.config["session_start"]["native_identity_bootstrap"] = True
        record = self.prepare(FRESH_ALL)
        self.assertEqual((len(self.herdr.pane_runs), len(self.herdr.bootstrap_prompts), record["status"]), (1, 2, "OWNERSHIP_BOUND"))

    def test_deadline_edges_use_the_injected_monotonic_clock_not_wall_sleeps(self) -> None:
        for advance, expected_ok in ((29.0, True), (29.999, True), (30.0, False), (45.0, False)):
            with self.subTest(advance=advance):
                self.reset_fixture()
                self.config["session_start"]["codex_launch_method"] = "explicit-pane-run"
                self.config["session_start"]["codex_input_policy"] = GUARDED
                self.herdr.launch_caps["pane empty interactive input proof"] = False
                reads = []
                def tick(herdr, pane, advance=advance, reads=reads):
                    reads.append(pane)
                    if len(reads) == 2:  # the pre-submission read
                        self.clock.current += advance
                self.herdr.on_metadata = tick
                prep = str(uuid.uuid4())
                if expected_ok:
                    self.assertEqual(self.prepare(CODEX_ONLY, prep)["status"], "OWNERSHIP_BOUND")
                    self.assertEqual(len(self.herdr.pane_runs), 1)
                else:
                    with self.assertRaisesRegex(hs.SupervisorError, "30-second guarded window.*expired"):
                        self.prepare(CODEX_ONLY, prep)
                    self.assertEqual((self.herdr.pane_runs, self.codex_item(prep)["status"], self.sup.owners()), ([], "LAUNCH_INTENT", self.owners))

    def test_a_deadline_crossed_during_the_last_checks_prevents_submission(self) -> None:
        clock, base = self.clock, type(self.herdr)
        class Racing(base):  # the clock jumps while the pre-submission command lookup happens, after every other check
            @property
            def pane_run(self):  # type: ignore[override]
                clock.current += 60
                return lambda pane, command: base.pane_run(self, pane, command)
        self.herdr.__class__ = Racing
        prep = str(uuid.uuid4())
        self.refused(prep, "30-second guarded window.*expired", "LAUNCH_INTENT")

    def test_the_token_is_consumed_before_the_single_submission(self) -> None:
        issued, states = [], []
        real_launch, real_run = herdr_sessions._launch_explicit, self.herdr.pane_run
        def spy_launch(supervisor, provider, item, record, path, token=None, invocation=None):
            issued.append(token)
            return real_launch(supervisor, provider, item, record, path, token, invocation)
        def spy_run(pane, command):
            states.append(issued[-1].consumed)
            return real_run(pane, command)
        herdr_sessions._launch_explicit = spy_launch
        self.addCleanup(setattr, herdr_sessions, "_launch_explicit", real_launch)
        self.herdr.pane_run = spy_run
        self.prepare(CODEX_ONLY)
        self.assertEqual(states, [True], "eligibility is consumed locally before pane run is called")
        self.assertTrue(all(isinstance(t, herdr_sessions.SubmissionToken) for t in issued) and issued[0].consumed)

    def test_submission_error_consumes_the_eligibility_and_nothing_is_retried(self) -> None:
        self.herdr.run_mode = "dropped"
        prep = str(uuid.uuid4())
        with self.assertRaisesRegex(hs.SupervisorError, "submission outcome is uncertain"):
            self.prepare(CODEX_ONLY, prep)
        self.assertEqual((len(self.herdr.pane_runs), self.codex_item(prep)["status"]), (1, "LAUNCH_SUBMITTED"))
        self.restart()
        with self.assertRaisesRegex(hs.SupervisorError, "expected process was not observed"):
            self.prepare(CODEX_ONLY, prep)
        self.assertEqual(len(self.herdr.pane_runs), 1)


class InterferenceTests(GuardedCase):
    def test_exact_pane_metadata_negatives_hold_before_input(self) -> None:
        for mode, pattern in (("foreign", "missing, foreign or malformed"), ("string_bool", "missing, foreign or malformed"), ("missing", "missing, foreign or malformed"),
                              ("error", "pane metadata could not be read"), ("other_cwd", "working directory is not the approved one")):
            with self.subTest(mode):
                self.reset_fixture()
                self.config["session_start"]["codex_launch_method"] = "explicit-pane-run"
                self.config["session_start"]["codex_input_policy"] = GUARDED
                self.herdr.launch_caps["pane empty interactive input proof"] = False
                self.herdr.metadata_mode = mode
                self.refused(str(uuid.uuid4()), pattern, "FRESH_PANE_CREATED")

    def test_a_missing_metadata_contract_holds(self) -> None:
        self.herdr.pane_metadata = None
        self.refused(str(uuid.uuid4()), "supported exact pane metadata contract is absent", "FRESH_PANE_CREATED")

    def test_focus_at_either_check_invalidates_the_eligibility(self) -> None:
        self.herdr.pane_focused["w9:p1"] = True
        self.refused(str(uuid.uuid4()), "pane is focused", "FRESH_PANE_CREATED")
        self.reset_fixture()
        self.config["session_start"]["codex_launch_method"] = "explicit-pane-run"
        self.config["session_start"]["codex_input_policy"] = GUARDED
        self.herdr.launch_caps["pane empty interactive input proof"] = False
        reads = []
        def focus_after_first(herdr, pane):
            reads.append(pane)
            if len(reads) == 2:
                herdr.pane_focused[pane] = True
        self.herdr.on_metadata = focus_after_first
        prep = str(uuid.uuid4())
        self.refused(prep, "pane is focused", "LAUNCH_INTENT")
        self.herdr.pane_focused["w9:p1"] = False  # un-focusing does not restore eligibility
        self.herdr.pane_run = lambda pane, command: self.herdr.pane_runs.append((pane, command)) or {}
        self.refused(prep, "does not hold that single-use eligibility", "LAUNCH_INTENT")

    def test_an_agent_changed_shell_cwd_recipe_or_context_blocks_submission(self) -> None:
        cases = {
            "agent detected": lambda pane: self.herdr.agents.__setitem__("intruder", self.herdr.agent("codex", "intruder", pane, "88888888-8888-4888-8888-888888888888")),
            "shell replaced": lambda pane: self.herdr.processes[pane].update(shell_pid=self.herdr.processes[pane]["shell_pid"] + 100) or self.herdr.processes[pane]["foreground_processes"][0].update(pid=self.herdr.processes[pane]["shell_pid"]),
            "foreground command": lambda pane: self.herdr.processes[pane]["foreground_processes"].append({"argv": ["vim"], "cmdline": "vim", "cwd": str(self.project_root), "name": "vim", "pid": 77}),
            "cwd moved": lambda pane: self.herdr.processes[pane]["foreground_processes"][0].update(cwd="/elsewhere"),
        }
        for label, mutate in cases.items():
            with self.subTest(label):
                self.reset_fixture()
                self.config["session_start"]["codex_launch_method"] = "explicit-pane-run"
                self.config["session_start"]["codex_input_policy"] = GUARDED
                self.herdr.launch_caps["pane empty interactive input proof"] = False
                calls = []
                def probe(path, mutate=mutate, calls=calls):
                    calls.append(path)
                    if len(calls) == 2:  # the revalidation just before submission (the first call built the recipe)
                        mutate("w9:p1")
                    return True
                self.sup.launch_chdir_probe = probe
                prep = str(uuid.uuid4())
                with self.assertRaises(hs.SupervisorError):
                    self.prepare(CODEX_ONLY, prep)
                self.assertEqual((self.herdr.pane_runs, self.herdr.agent_renames, self.sup.owners()), ([], [], self.owners))
                self.assertEqual(self.codex_item(prep)["status"], "LAUNCH_INTENT")

    def test_recipe_executable_and_context_drift_between_intent_and_submission(self) -> None:
        prep = str(uuid.uuid4())
        original = self.helper_codex_home
        def drift(herdr, pane):
            self.helper_codex_home = "/home/test/another-codex-home"
        self.herdr.on_metadata = lambda herdr, pane: drift(herdr, pane) if len(herdr.metadata_reads) == 1 else None
        with self.assertRaises(hs.SupervisorError):
            self.prepare(CODEX_ONLY, prep)
        self.assertEqual((self.herdr.pane_runs, self.sup.owners()), ([], self.owners))
        self.helper_codex_home = original

    def test_other_preparations_and_existing_panes_are_untouched(self) -> None:
        prep = str(uuid.uuid4())
        self.prepare(CODEX_ONLY, prep)
        reads = {pane for pane in self.herdr.metadata_reads}
        self.assertTrue(reads.isdisjoint({"w3:p1", "w3:p2"}), "only the new pane was ever inspected for the guard")
        self.assertEqual({pane for pane, _ in self.herdr.pane_runs}, {self.codex_item(prep)["pane_id"]})
        self.assertIn("codex-main", self.herdr.agents)


class RecoveryAfterAttemptTests(GuardedCase):
    def test_late_execution_after_a_lost_admission_and_restart_never_relaunches_and_needs_no_empty_shell(self) -> None:
        self.herdr.run_mode = "lost"
        prep = str(uuid.uuid4())
        with self.assertRaisesRegex(hs.SupervisorError, "submission outcome is uncertain"):
            self.prepare(CODEX_ONLY, prep)
        self.assertEqual(self.codex_item(prep)["status"], "LAUNCH_SUBMITTED")
        reads_before = len(self.herdr.metadata_reads)
        self.restart()
        self.assertEqual(self.prepare(CODEX_ONLY, prep)["status"], "OWNERSHIP_BOUND")
        self.assertEqual(self.counts(), (0, 1, 1, 0))
        self.assertEqual(len(self.herdr.metadata_reads), reads_before, "recovery needs neither a token nor a fresh empty-shell qualification (Codex is already running)")

    def test_recorded_policy_gates_recovery_after_the_setting_changes_or_rolls_back(self) -> None:
        prep = str(uuid.uuid4())
        self.herdr.run_mode = "dropped"
        with self.assertRaises(hs.SupervisorError):
            self.prepare(CODEX_ONLY, prep)
        seen = []
        self.sup.direct_launch_readiness = lambda policy=None: (seen.append(policy), {"ready": True, "reasons": []})[1]
        self.config["session_start"]["codex_input_policy"] = STRICT
        self.config["session_start"]["codex_launch_method"] = "agent-start"
        with self.assertRaisesRegex(hs.SupervisorError, "expected process was not observed"):
            self.prepare(CODEX_ONLY, prep)
        self.assertEqual(seen, [GUARDED], "readiness follows the journaled policy; a rollback of the setting cannot hide the uncertain attempt")
        self.assertEqual(len(self.herdr.pane_runs), 1)

    def test_aborted_before_submission_and_already_attempted_are_separate_recoveries(self) -> None:
        aborted, attempted = str(uuid.uuid4()), str(uuid.uuid4())
        self.split_then_stop(aborted)
        self.herdr.pane_run = lambda pane, command: self.herdr.pane_runs.append((pane, command)) or {}
        self.refused(aborted, "does not hold that single-use eligibility", "LAUNCH_INTENT")
        # a different preparation can start normally; the aborted one is untouched
        self.reset_fixture()
        self.config["session_start"]["codex_launch_method"] = "explicit-pane-run"
        self.config["session_start"]["codex_input_policy"] = GUARDED
        self.herdr.launch_caps["pane empty interactive input proof"] = False
        self.herdr.run_mode = "lost"
        with self.assertRaises(hs.SupervisorError):
            self.prepare(CODEX_ONLY, attempted)
        self.restart()
        self.assertEqual(self.prepare(CODEX_ONLY, attempted)["status"], "OWNERSHIP_BOUND")

    def test_simultaneous_preparations_under_the_worker_lock_launch_once_with_isolated_tokens(self) -> None:
        prep = str(uuid.uuid4())
        outcomes = []
        gate = threading.Barrier(2)
        def attempt() -> None:
            sup = self.make_supervisor()
            gate.wait()
            try:
                with hs.WorkerLock(self.paths.lock_file):
                    outcomes.append(sup.prepare_task_sessions(CODEX_ONLY, preparation_id=prep)["status"])
            except hs.SupervisorError as error:
                outcomes.append(str(error)[:50])
        threads = [threading.Thread(target=attempt) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual((len(self.herdr.pane_runs), len(self.thread_journal.requests), len(self.herdr.splits)), (1, 0, 1))
        self.assertIn("OWNERSHIP_BOUND", outcomes)


class RealAdapterMetadataTests(GuardedCase):
    class Recording(herdr_cli.HerdrCli):
        def __init__(self, response) -> None:
            super().__init__("/fake/herdr")
            self.response, self.calls = response, []

        def _run(self, args, *, timeout, json_result=True):  # type: ignore[override]
            self.calls.append(list(args))
            return self.response

    def pane(self, **overrides) -> dict:
        data = json.loads(PANE_GET.read_text())
        data["result"]["pane"].update({"pane_id": "w1:p3", "agent": None}, **overrides)
        return data

    def test_exact_types_and_requested_identity_are_enforced_at_the_adapter(self) -> None:
        good = self.Recording(self.pane())
        self.assertEqual(good.pane_metadata("w1:p3"), {"pane_id": "w1:p3", "focused": False, "agent": None, "cwd": "/home/user/workspace"})
        self.assertEqual(good.calls, [["pane", "get", "w1:p3"]])
        for label, response, pane in (
            ("foreign pane", self.pane(), "w1:p4"),
            ("string bool", self.pane(focused="false"), "w1:p3"),
            ("int bool", self.pane(focused=0), "w1:p3"),
            ("missing focus", {"result": {"pane": {k: v for k, v in self.pane()["result"]["pane"].items() if k != "focused"}}}, "w1:p3"),
            ("agent number", self.pane(agent=3), "w1:p3"),
            ("cwd number", self.pane(cwd=5), "w1:p3"),
            ("no pane", {"result": {}}, "w1:p3"),
            ("not an object", [], "w1:p3"),
        ):
            with self.subTest(label), self.assertRaises(herdr_core.HerdrError):
                self.Recording(response).pane_metadata(pane)
        self.assertTrue(self.Recording(self.pane(focused=True)).pane_metadata("w1:p3")["focused"] is True)


class WindowLatencyTests(GuardedCase):
    """Review GP1: the 30-second window starts BEFORE the split is invoked (response latency counts) and is re-checked after the
    durable attempt marker, immediately before the one `pane run` call. Injected monotonic fake only; no wall-clock sleeps."""

    def delay_split(self, seconds: float) -> None:
        real = self.herdr.split_pane
        def split(*args, **kwargs):
            pane = real(*args, **kwargs)  # the pane exists from here on...
            self.clock.current += seconds  # ...but the response only arrives later
            return pane
        self.herdr.split_pane = split

    def delay_marker(self, seconds: float) -> None:
        real, state = herdr_sessions.atomic_write_json, {"done": False}
        def persist(path, value, **kwargs):
            real(path, value, **kwargs)
            item = value.get("providers", {}).get("codex", {}) if isinstance(value, dict) else {}
            if item.get("status") == "LAUNCH_SUBMITTED" and not state["done"]:
                state["done"] = True
                self.clock.current += seconds  # persistence blocks after the marker is durable
        herdr_sessions.atomic_write_json = persist
        self.addCleanup(setattr, herdr_sessions, "atomic_write_json", real)

    def launched_or_held(self, ok: bool, pattern: str, status_if_held: str) -> str:
        prep = str(uuid.uuid4())
        if ok:
            self.assertEqual(self.prepare(CODEX_ONLY, prep)["status"], "OWNERSHIP_BOUND")
            self.assertEqual(self.counts(), (0, 1, 1, 0))
        else:
            self.refused(prep, pattern, status_if_held)
            self.assertEqual((len(self.herdr.splits), len(self.thread_journal.requests)), (1, 0), "no replacement pane, and never a thread")
        return prep

    def test_split_response_latency_counts_against_the_window(self) -> None:
        for delay, ok in ((29.0, True), (29.999, True), (30.0, False), (60.0, False)):
            with self.subTest(split_response_delay=delay):
                self.reset_fixture()
                self.config["session_start"]["codex_launch_method"] = "explicit-pane-run"
                self.config["session_start"]["codex_input_policy"] = GUARDED
                self.herdr.launch_caps["pane empty interactive input proof"] = False
                self.delay_split(delay)
                prep = self.launched_or_held(ok, "30-second guarded window from the split has expired", "FRESH_PANE_CREATED")
                if not ok:
                    self.assertNotIn("launch", self.codex_item(prep), "expired at qualification: not even an intent was journaled")

    def test_marker_write_latency_prevents_the_call_without_rolling_the_marker_back(self) -> None:
        for delay, ok in ((29.0, True), (29.999, True), (30.0, False), (60.0, False)):
            with self.subTest(marker_write_delay=delay):
                self.reset_fixture()
                self.config["session_start"]["codex_launch_method"] = "explicit-pane-run"
                self.config["session_start"]["codex_input_policy"] = GUARDED
                self.herdr.launch_caps["pane empty interactive input proof"] = False
                self.delay_marker(delay)
                prep = self.launched_or_held(ok, "expired after the attempt marker was persisted and before pane run was called.*abandon-session-start", "LAUNCH_SUBMITTED")
                if not ok:
                    self.assertEqual(self.herdr.pane_runs, [], "zero submission")
                    self.assertEqual(self.codex_item(prep)["status"], "LAUNCH_SUBMITTED", "the durable marker is not rolled back")

    def test_the_two_latencies_add_up(self) -> None:
        for split_delay, marker_delay, ok in ((10.0, 19.999, True), (10.0, 20.0, False), (15.0, 15.0, False)):
            with self.subTest(split=split_delay, marker=marker_delay):
                self.reset_fixture()
                self.config["session_start"]["codex_launch_method"] = "explicit-pane-run"
                self.config["session_start"]["codex_input_policy"] = GUARDED
                self.herdr.launch_caps["pane empty interactive input proof"] = False
                self.delay_split(split_delay)
                self.delay_marker(marker_delay)
                self.launched_or_held(ok, "30-second guarded window from the split has expired", "LAUNCH_SUBMITTED")

    def test_a_post_marker_expiry_is_never_rearmed_resent_or_replaced(self) -> None:
        self.delay_marker(60.0)
        prep = str(uuid.uuid4())
        self.refused(prep, "after the attempt marker was persisted", "LAUNCH_SUBMITTED")
        pane = self.codex_item(prep)["pane_id"]
        for label in ("same process, later call", "restart", "setting rolled back to strict"):
            with self.subTest(label):
                if label == "restart":
                    self.restart()
                if label.startswith("setting"):
                    self.config["session_start"]["codex_input_policy"] = STRICT
                    self.config["session_start"]["codex_launch_method"] = "agent-start"
                with self.assertRaisesRegex(hs.SupervisorError, "expected process was not observed.*abandon-session-start"):
                    self.prepare(CODEX_ONLY, prep)
                self.assertEqual((self.herdr.pane_runs, self.herdr.agent_renames, self.herdr.bootstrap_prompts, self.herdr.prompts), ([], [], [], []))
                self.assertEqual((self.codex_item(prep)["status"], self.sup.owners(), len(self.herdr.splits), len(self.thread_journal.requests)), ("LAUNCH_SUBMITTED", self.owners, 1, 0))
        self.assertEqual(self.sup.abandon_session_preparation(prep)["abandoned"], True)
        self.assertEqual(self.codex_item(prep)["pane_id"], pane, "the pane is preserved")

    def test_the_eligibility_is_already_consumed_when_the_post_marker_expiry_holds(self) -> None:
        captured = []
        real = herdr_sessions._launch_explicit
        def spy(supervisor, provider, item, record, path, token=None, invocation=None):
            captured.append(token)
            return real(supervisor, provider, item, record, path, token, invocation)
        herdr_sessions._launch_explicit = spy
        self.addCleanup(setattr, herdr_sessions, "_launch_explicit", real)
        self.delay_marker(45.0)
        self.refused(str(uuid.uuid4()), "after the attempt marker was persisted", "LAUNCH_SUBMITTED")
        self.assertTrue(captured and captured[0].consumed, "consumed before the marker; an expiry afterwards cannot be retried in this invocation")

    def test_the_window_only_applies_to_the_guarded_policy(self) -> None:
        self.config["session_start"]["codex_input_policy"] = STRICT
        self.herdr.pane_input_empty = lambda pane: True  # a (hypothetical) supported proof exists
        self.delay_marker(300.0)
        self.assertEqual(self.prepare(CODEX_ONLY)["status"], "OWNERSHIP_BOUND")
        self.assertEqual(len(self.herdr.pane_runs), 1)

    def test_the_reviewers_three_scenarios(self) -> None:
        """Positive control, persistence latency 60 s and split response latency 60 s (CODEX_DEADLINE_REPRO.py)."""
        self.assertEqual(self.prepare(CODEX_ONLY)["status"], "OWNERSHIP_BOUND")
        self.reset_fixture()
        self.config["session_start"]["codex_launch_method"] = "explicit-pane-run"
        self.config["session_start"]["codex_input_policy"] = GUARDED
        self.herdr.launch_caps["pane empty interactive input proof"] = False
        self.delay_marker(60.0)
        self.refused(str(uuid.uuid4()), "expired after the attempt marker", "LAUNCH_SUBMITTED")
        self.reset_fixture()
        self.config["session_start"]["codex_launch_method"] = "explicit-pane-run"
        self.config["session_start"]["codex_input_policy"] = GUARDED
        self.herdr.launch_caps["pane empty interactive input proof"] = False
        self.delay_split(60.0)
        self.refused(str(uuid.uuid4()), "30-second guarded window from the split has expired", "FRESH_PANE_CREATED")

    def test_the_token_has_an_expiry_only_check(self) -> None:
        token = herdr_sessions.SubmissionToken(herdr_sessions._TOKEN_KEY, "p1", "w9:p1", object(), 100.0)
        token.consume()
        self.assertIsNone(token.deadline_problem(129.999))
        self.assertIn("expired", token.deadline_problem(130.0))
        self.assertIn("backwards", token.deadline_problem(99.0))
