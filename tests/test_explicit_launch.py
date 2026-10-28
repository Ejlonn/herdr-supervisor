"""Issue 5 launch-context amendment: the explicit Codex launch method (`session_start.codex_launch_method =
explicit-pane-run`). The supervisor builds ONE deterministic command from the validated helper context and the exact
helper-created thread, journals the recipe and the attempt, submits it once through `pane run`, then observes the same
pane/process and names the detected agent through the exact pane. Fixtures and fake provider executables only: no real
Codex, no live Herdr. The inherited `agent start` context stays unverified and is not touched."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import tempfile
import threading
import uuid
from pathlib import Path

import herdr_cli
import herdr_core
import herdr_helper_context as hctx
import herdr_sessions
from test_fresh_session_issue5 import CODEX_ONLY, FRESH_ALL, Issue5Case
from v2_fixtures import CLAUDE_SESSION, CODEX_SESSION, hs

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "herdr" / "0.9.5-launch"
FRESH_CLAUDE = {"policy": "fresh-claude", "profiles": {"codex": "default", "claude": "default"}}


class ExplicitCase(Issue5Case):
    def setUp(self) -> None:
        super().setUp()
        self.config["session_start"]["codex_launch_method"] = "explicit-pane-run"

    def codex_item(self, prep: str) -> dict:
        return self.journal(prep)["providers"]["codex"]

    def counts(self) -> tuple[int, int, int, int]:
        return (len(self.thread_journal.requests), len(self.herdr.splits), len(self.herdr.pane_runs), len(self.herdr.starts))

    def restart(self) -> None:
        self.sup = self.make_supervisor()


class ExplicitLaunchFlowTests(ExplicitCase):
    def test_success_launches_once_binds_the_process_names_the_pane_agent_and_reuses_ownership(self) -> None:
        prep = str(uuid.uuid4())
        statuses = []
        original = self.herdr.pane_run
        def spy(pane, command):
            item = self.codex_item(prep)
            statuses.append((item["status"], item["launch"]["recipe"]["command"] == command, "process" in item["launch"]))
            return original(pane, command)
        self.herdr.pane_run = spy
        record = self.prepare(CODEX_ONLY, prep)
        item = record["providers"]["codex"]
        self.assertEqual(statuses, [("LAUNCH_SUBMITTED", True, False)], "recipe and attempt are journaled before the single submission")
        self.assertEqual(self.counts(), (0, 1, 1, 0), "no thread, one split, one launch, no `agent start`")
        pane, command = self.herdr.pane_runs[0]
        self.assertEqual(pane, item["pane_id"])
        argv = shlex.split(command)
        self.assertEqual(argv[1:3], ["--chdir", str(self.project_root)])
        self.assertEqual(argv[3], f"CODEX_HOME={self.helper_codex_home}")
        self.assertEqual(argv[-2:], [os.path.realpath(self.fake_codex), "--no-daemon"], "a NEW Codex: pinned executable and --no-daemon only, no resume argument")
        self.assertNotIn("resume", argv)
        self.assertEqual(argv[4], os.path.realpath(self.fake_codex))
        derived = self.herdr.launch_env[pane]
        self.assertEqual((derived["cwd"], derived["CODEX_HOME"], derived["marker"]), (str(self.project_root), self.helper_codex_home, "HERDR_PANE_MARKER=inherited"))
        self.assertEqual(item["launch_method"], "explicit-pane-run")
        self.assertEqual(item["launch"]["process"]["argv"], [os.path.realpath(self.fake_codex), "--no-daemon"])
        self.assertEqual(self.herdr.agent_renames, [(pane, f"codex-run-{prep[:8]}")], "the alias is set through the exact pane, once")
        self.assertEqual((item["status"], item["initialization"]), ("IDENTITY_VERIFIED", "native-reported"))
        self.assertEqual(record["status"], "OWNERSHIP_BOUND")
        self.assertEqual(self.sup.owners()["codex"], {"pane_id": pane, "session_id": item["session_id"]})
        self.assertEqual(item["session_id"], self.herdr.next_sessions["codex"], "learned from the owned pane")
        self.assertEqual(self.herdr.prompts, [], "no task delivery from preparation")
        self.assertEqual(item["label_state"]["status"], "NAMED", "display label stays a separate step")
        herdr_sessions.validate_preparation_record(self.config, self.journal(prep), prep)

    def test_a_restart_after_success_launches_nothing_more(self) -> None:
        prep = str(uuid.uuid4())
        self.prepare(CODEX_ONLY, prep)
        self.restart()
        self.assertEqual(self.prepare(CODEX_ONLY, prep)["status"], "OWNERSHIP_BOUND")
        self.assertEqual(self.counts(), (0, 1, 1, 0))
        self.assertEqual(len(self.herdr.agent_renames), 1)

    def test_a_late_process_is_observed_without_a_second_launch(self) -> None:
        self.herdr.run_mode = "late"
        self.herdr.late_polls = 3
        record = self.prepare(CODEX_ONLY)
        self.assertEqual(record["status"], "OWNERSHIP_BOUND")
        self.assertEqual(len(self.herdr.pane_runs), 1)

    def test_fresh_all_uses_the_explicit_method_for_codex_only(self) -> None:
        record = self.prepare(FRESH_ALL)
        self.assertEqual(len(self.herdr.pane_runs), 1)
        self.assertEqual([kind for _, kind, _, _ in self.herdr.starts], ["claude"], "Claude keeps `agent start`")
        self.assertNotIn("launch_method", record["providers"]["claude"])
        self.assertEqual(record["status"], "OWNERSHIP_BOUND")

    def test_claude_only_and_preserve_modes_are_unaffected(self) -> None:
        self.prepare(FRESH_CLAUDE)
        self.assertEqual((self.herdr.pane_runs, self.thread_journal.requests), ([], []))
        self.reset_fixture()
        self.config["session_start"]["codex_launch_method"] = "explicit-pane-run"
        self.prepare({"policy": "preserve", "profiles": {"codex": "default", "claude": "default"}})
        self.assertEqual((self.herdr.pane_runs, self.herdr.splits, self.herdr.agent_renames), ([], [], []))
        self.assertEqual(self.sup.owners(), self.owners)


class PreSubmissionRefusalTests(ExplicitCase):
    def test_only_an_idle_preparation_owned_shell_is_eligible(self) -> None:
        for label, mutate in (
            ("foreground command running", lambda p: p["foreground_processes"].__setitem__(0, dict(p["foreground_processes"][0], pid=p["shell_pid"] + 500))),
            ("two foreground processes", lambda p: p["foreground_processes"].append(dict(p["foreground_processes"][0]))),
            ("different working directory", lambda p: p["foreground_processes"][0].update(cwd="/elsewhere")),
            ("no process contract", None),
        ):
            with self.subTest(label):
                self.reset_fixture()
                self.config["session_start"]["codex_launch_method"] = "explicit-pane-run"
                original = self.herdr.split_pane
                def split(*args, original=original, mutate=mutate, **kwargs):
                    pane = original(*args, **kwargs)
                    if mutate is None:
                        self.herdr.processes.pop(pane)
                    else:
                        mutate(self.herdr.processes[pane])
                    return pane
                self.herdr.split_pane = split
                prep = str(uuid.uuid4())
                with self.assertRaisesRegex(hs.SupervisorError, "not an idle interactive shell|could not read pane|needs Herdr's pane process contract"):
                    self.prepare(CODEX_ONLY, prep)
                self.assertEqual(self.herdr.pane_runs, [])
                self.assertEqual(self.codex_item(prep)["status"], "FRESH_PANE_CREATED")
                self.assertEqual(self.sup.owners(), self.owners)

    def test_an_occupied_pane_or_existing_alias_refuses_before_the_intent(self) -> None:
        original = self.herdr.split_pane
        def split(*args, **kwargs):
            pane = original(*args, **kwargs)
            self.herdr.available_panes.discard(pane)  # Herdr reports an agent already in the pane
            return pane
        self.herdr.split_pane = split
        prep = str(uuid.uuid4())
        with self.assertRaisesRegex(hs.SupervisorError, "not an empty shell pane"):
            self.prepare(CODEX_ONLY, prep)
        self.assertEqual((self.herdr.pane_runs, self.codex_item(prep)["status"]), ([], "FRESH_PANE_CREATED"))
        self.herdr.split_pane = original
        self.reset_fixture()
        self.config["session_start"]["codex_launch_method"] = "explicit-pane-run"
        prep = str(uuid.uuid4())
        self.herdr.agents[f"codex-run-{prep[:8]}"] = self.herdr.agent("codex", f"codex-run-{prep[:8]}", "w5:p5", "88888888-8888-4888-8888-888888888888")
        with self.assertRaisesRegex(hs.SupervisorError, "name already exists"):
            self.prepare(CODEX_ONLY, prep)
        self.assertEqual(self.herdr.pane_runs, [])

    def test_missing_pane_run_keeps_the_intent_without_submitting_and_a_later_retry_submits_once(self) -> None:
        prep = str(uuid.uuid4())
        real = self.herdr.pane_run
        self.herdr.pane_run = None
        with self.assertRaisesRegex(hs.SupervisorError, "needs the pane run command; nothing was submitted"):
            self.prepare(CODEX_ONLY, prep)
        self.assertEqual(self.codex_item(prep)["status"], "LAUNCH_INTENT")
        self.herdr.pane_run = real
        self.assertEqual(self.prepare(CODEX_ONLY, prep)["status"], "OWNERSHIP_BOUND")
        self.assertEqual(len(self.herdr.pane_runs), 1)

    def test_a_pane_that_changed_to_another_idle_shell_since_the_intent_is_not_launched_into(self) -> None:
        prep = str(uuid.uuid4())
        real = self.herdr.pane_run
        self.herdr.pane_run = None
        with self.assertRaises(hs.SupervisorError):
            self.prepare(CODEX_ONLY, prep)
        pane = self.codex_item(prep)["pane_id"]
        replaced = self.herdr.processes[pane]
        replaced["shell_pid"] += 100
        replaced["foreground_processes"][0]["pid"] = replaced["shell_pid"]  # still an idle shell, but not the journaled one
        self.herdr.pane_run = real
        with self.assertRaisesRegex(hs.SupervisorError, "pane changed since the intent was journaled; nothing was submitted"):
            self.prepare(CODEX_ONLY, prep)
        self.assertEqual((self.herdr.pane_runs, self.codex_item(prep)["status"]), ([], "LAUNCH_INTENT"))

    def test_executable_drift_between_intent_and_submission_blocks_the_launch(self) -> None:
        prep = str(uuid.uuid4())
        real = self.herdr.pane_run
        self.herdr.pane_run = None
        with self.assertRaises(hs.SupervisorError):
            self.prepare(CODEX_ONLY, prep)
        with open(self.fake_codex, "a") as handle:
            handle.write("# changed after the recipe was journaled\n")
        self.herdr.pane_run = real
        with self.assertRaisesRegex(hs.SupervisorError, "changed since the launch recipe was journaled"):
            self.prepare(CODEX_ONLY, prep)
        self.assertEqual((self.herdr.pane_runs, self.codex_item(prep)["status"]), ([], "LAUNCH_INTENT"))

    def test_helper_context_drift_between_intent_and_submission_blocks_the_launch(self) -> None:
        prep = str(uuid.uuid4())
        real = self.herdr.pane_run
        self.herdr.pane_run = None
        with self.assertRaises(hs.SupervisorError):
            self.prepare(CODEX_ONLY, prep)
        self.herdr.pane_run = real
        self.helper_codex_home = "/home/test/another-codex-home"
        with self.assertRaisesRegex(hs.SupervisorError, "differs from the authorized helper context|context changed since the thread was requested"):
            self.prepare(CODEX_ONLY, prep)
        self.assertEqual(self.herdr.pane_runs, [])

    def test_a_script_launcher_is_refused_by_the_production_check_despite_the_test_seam(self) -> None:
        self.assertFalse(hs.Supervisor.launch_executable_check(self.fake_codex), "the fixture provider is a script")
        self.assertTrue(hs.Supervisor.launch_executable_check(os.path.realpath(__import__("sys").executable)), "a native ELF passes")
        with self.assertRaisesRegex(hctx.ContextError, "not a directly executable native binary"):
            hctx.build_launch_recipe(self.helper_descriptor(str(self.project_root)), "11111111-1111-4111-8111-111111111111")
        # a fresh Supervisor (no seam) refuses the very same context
        fresh = hs.Supervisor(self.paths, self.config, self.herdr, clock=self.clock.time, sleeper=self.clock.sleep)
        fresh.helper_context = lambda cwd=None: self.helper_descriptor(cwd or str(self.project_root))
        with self.assertRaisesRegex(hs.SupervisorError, "not a directly executable native binary"):
            fresh.launch_recipe("11111111-1111-4111-8111-111111111111")


class UncertaintyAndReconciliationTests(ExplicitCase):
    def test_a_lost_admission_response_reconciles_by_observing_the_same_pane(self) -> None:
        self.herdr.run_mode = "lost"  # the command executed but the response was lost
        prep = str(uuid.uuid4())
        with self.assertRaisesRegex(hs.SupervisorError, "submission outcome is uncertain.*nothing was resubmitted"):
            self.prepare(CODEX_ONLY, prep)
        self.assertEqual(self.codex_item(prep)["status"], "LAUNCH_SUBMITTED")
        self.assertEqual(self.sup.owners(), self.owners)
        self.restart()
        record = self.prepare(CODEX_ONLY, prep)
        self.assertEqual(record["status"], "OWNERSHIP_BOUND")
        self.assertEqual(self.counts(), (0, 1, 1, 0), "no second launch or split, and never a thread")

    def test_a_dropped_submission_holds_without_a_second_launch_and_can_be_abandoned(self) -> None:
        self.herdr.run_mode = "dropped"
        prep = str(uuid.uuid4())
        with self.assertRaisesRegex(hs.SupervisorError, "submission outcome is uncertain"):
            self.prepare(CODEX_ONLY, prep)
        slept = self.clock.time()
        for _ in range(2):
            self.restart()
            with self.assertRaisesRegex(hs.SupervisorError, "expected process was not observed.*abandon-session-start"):
                self.prepare(CODEX_ONLY, prep)
        self.assertGreater(self.clock.time(), slept, "bounded deterministic waits, not a busy loop")
        self.assertEqual((self.counts(), self.codex_item(prep)["status"]), ((0, 1, 1, 0), "LAUNCH_SUBMITTED"))
        self.assertEqual(self.sup.owners(), self.owners)
        self.assertEqual(self.sup.abandon_session_preparation(prep)["abandoned"], True)
        self.assertEqual(self.herdr.pane_runs[0][0], self.codex_item(prep)["pane_id"], "the pane is preserved")

    def test_an_admitted_but_never_executed_launch_holds_and_never_resubmits(self) -> None:
        self.herdr.run_mode = "swallowed"
        self.herdr.pane_run = lambda pane, command: self.herdr.pane_runs.append((pane, command)) or {}
        prep = str(uuid.uuid4())
        for _ in range(3):
            with self.assertRaisesRegex(hs.SupervisorError, "expected process was not observed"):
                self.prepare(CODEX_ONLY, prep)
        self.assertEqual((len(self.herdr.pane_runs), self.codex_item(prep)["status"]), (1, "LAUNCH_ADMITTED"))
        self.assertEqual((self.sup.owners(), self.herdr.prompts, self.herdr.agent_renames), (self.owners, [], []))

    def test_a_wrong_replaced_or_unknown_process_holds(self) -> None:
        for label, run_mode, mutate, pattern in (
            ("a resumed session instead of a new one", "wrong", None, "different or ambiguous process"),
            ("shell replaced", "ok", lambda pane, thread: self.herdr.processes[pane].update(shell_pid=777777), "shell was replaced"),
            ("different executable", "ok", lambda pane, thread: self.herdr.processes[pane]["foreground_processes"][0].update(argv=["/bin/true", "--no-daemon"]), "not the pinned executable"),
            ("different cwd", "ok", lambda pane, thread: self.herdr.processes[pane]["foreground_processes"][0].update(cwd="/elsewhere"), "different or ambiguous process"),
        ):
            with self.subTest(label):
                self.reset_fixture()
                self.config["session_start"]["codex_launch_method"] = "explicit-pane-run"
                self.herdr.run_mode = run_mode
                prep = str(uuid.uuid4())
                real = self.herdr.pane_run
                def run(pane, command, real=real, mutate=mutate):
                    result = real(pane, command)
                    if mutate:
                        mutate(pane, shlex.split(command)[-2])
                    return result
                self.herdr.pane_run = run
                with self.assertRaisesRegex(hs.SupervisorError, pattern):
                    self.prepare(CODEX_ONLY, prep)
                self.assertEqual((len(self.herdr.pane_runs), self.sup.owners(), self.herdr.agent_renames), (1, self.owners, []))
                self.assertIn(self.codex_item(prep)["status"], ("LAUNCH_ADMITTED", "LAUNCH_SUBMITTED"))

    def test_alias_failures_are_reconciled_on_the_same_pane_never_by_relaunch(self) -> None:
        for mode in ("error", "noop"):
            with self.subTest(mode):
                self.reset_fixture()
                self.config["session_start"]["codex_launch_method"] = "explicit-pane-run"
                self.herdr.rename_agent_mode = mode
                prep = str(uuid.uuid4())
                with self.assertRaisesRegex(hs.SupervisorError, "naming the agent failed|alias could not be verified"):
                    self.prepare(CODEX_ONLY, prep)
                item = self.codex_item(prep)
                self.assertEqual(item["status"], "PROCESS_BOUND")
                self.assertIn("process", item["launch"])
                self.herdr.rename_agent_mode = "ok"
                self.restart()
                self.assertEqual(self.prepare(CODEX_ONLY, prep)["status"], "OWNERSHIP_BOUND")
                self.assertEqual(self.counts(), (0, 1, 1, 0), "alias repair never relaunches")

    def test_an_alias_held_by_another_pane_is_refused_without_renaming(self) -> None:
        prep = str(uuid.uuid4())
        alias = f"codex-run-{prep[:8]}"
        real = self.herdr.pane_run
        def run(pane, command):
            result = real(pane, command)
            self.herdr.agents[alias] = self.herdr.agent("codex", alias, "w7:p7", "99999999-9999-4999-8999-999999999999")
            return result
        self.herdr.pane_run = run
        # the intent step also refuses a pre-existing alias, so the squatter appears only after submission
        with self.assertRaisesRegex(hs.SupervisorError, "held by another or an ambiguous agent"):
            self.prepare(CODEX_ONLY, prep)
        self.assertEqual((self.herdr.agent_renames, len(self.herdr.pane_runs), self.codex_item(prep)["status"]), ([], 1, "PROCESS_BOUND"))

    def test_two_agents_in_the_pane_hold(self) -> None:
        real = self.herdr.pane_run
        def run(pane, command):
            result = real(pane, command)
            self.herdr.agents["extra"] = self.herdr.agent("codex", "extra", pane, "99999999-9999-4999-8999-999999999999")
            return result
        self.herdr.pane_run = run
        with self.assertRaisesRegex(hs.SupervisorError, "more than one agent in the pane"):
            self.prepare(CODEX_ONLY)
        self.assertEqual(self.herdr.agent_renames, [])

    def test_concurrent_preparations_under_the_worker_lock_launch_once(self) -> None:
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
                outcomes.append(str(error)[:40])
        threads = [threading.Thread(target=attempt) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual(len(self.herdr.pane_runs), 1)
        self.assertIn("OWNERSHIP_BOUND", outcomes)
        self.assertEqual(len(self.thread_journal.requests), 0)


class IdentityAndOwnershipAfterLaunchTests(ExplicitCase):
    def test_wrong_identity_or_model_never_binds(self) -> None:
        for label, setup, pattern in (
            ("duplicate of the old owner", lambda: setattr(self.herdr, "next_sessions", {**self.herdr.next_sessions, "codex": CODEX_SESSION}), "missing or duplicate"),
            ("duplicate of the Claude owner", lambda: setattr(self.herdr, "next_sessions", {**self.herdr.next_sessions, "codex": CLAUDE_SESSION}), "missing or duplicate"),
        ):
            with self.subTest(label):
                self.reset_fixture()
                setup()
                with self.assertRaisesRegex(hs.SupervisorError, pattern):
                    self.prepare(CODEX_ONLY)
                self.assertEqual((self.sup.owners(), len(self.herdr.pane_runs)), (self.owners, 1))

    def test_a_replaced_process_after_binding_is_not_adopted(self) -> None:
        prep = str(uuid.uuid4())
        self.herdr.rename_agent_mode = "error"
        with self.assertRaises(hs.SupervisorError):
            self.prepare(CODEX_ONLY, prep)
        item = self.codex_item(prep)
        self.herdr.processes[item["pane_id"]]["foreground_processes"][0]["pid"] += 1
        self.herdr.rename_agent_mode = "ok"
        with self.assertRaisesRegex(hs.SupervisorError, "process changed since the launch was bound"):
            self.prepare(CODEX_ONLY, prep)
        self.assertEqual((self.sup.owners(), len(self.herdr.pane_runs)), (self.owners, 1))

    def test_delayed_native_identity_with_the_bootstrap_uses_the_launch_bound_process(self) -> None:
        self.herdr.hold_identity = True
        self.config["session_start"]["native_identity_bootstrap"] = True
        prep = str(uuid.uuid4())
        record = self.prepare(CODEX_ONLY, prep)
        item = record["providers"]["codex"]
        self.assertEqual((item["initialization"], len(self.herdr.bootstrap_prompts), len(self.herdr.pane_runs)), ("bootstrap-completed", 1, 1))
        self.assertEqual(item["bootstrap"]["process"], item["launch"]["process"], "the bootstrap target is the launch-bound process")
        self.assertEqual(self.herdr.prompts, [], "the task is delivered later, once")

    def test_delayed_identity_without_the_bootstrap_stops_with_nothing_sent(self) -> None:
        self.herdr.hold_identity = True
        with self.assertRaisesRegex(hs.SupervisorError, "bootstrap is disabled.*nothing was sent"):
            self.prepare(CODEX_ONLY)
        self.assertEqual((self.herdr.bootstrap_prompts, self.sup.owners(), len(self.herdr.pane_runs)), ([], self.owners, 1))

    def test_a_replacement_process_with_a_new_identity_is_refused_on_a_recovered_bootstrap_intent(self) -> None:
        self.herdr.hold_identity = True
        self.config["session_start"]["native_identity_bootstrap"] = True
        self.herdr.bootstrap_mode = "reject"
        prep = str(uuid.uuid4())
        with self.assertRaisesRegex(hs.SupervisorError, "rejected before input"):
            self.prepare(CODEX_ONLY, prep)
        item = self.codex_item(prep)
        self.herdr.processes[item["pane_id"]]["foreground_processes"][0]["pid"] += 1
        self.herdr.agents[item["agent_name"]]["agent_session"] = {"value": str(uuid.uuid4())}  # an identity alone is never proof of the process
        before = len(self.herdr.bootstrap_prompts)
        with self.assertRaisesRegex(hs.SupervisorError, "process changed"):
            self.prepare(CODEX_ONLY, prep)
        self.assertEqual((len(self.herdr.bootstrap_prompts), self.sup.owners()), (before, self.owners))


class CompatibilityTests(ExplicitCase):
    def test_a_journaled_agent_start_preparation_keeps_its_method_when_the_setting_changes(self) -> None:
        """A LEGACY journal (earlier library, generic agent start, pre-created thread): it keeps its own method and thread, the setting cannot convert it, and it is never relaunched as a direct start."""
        prep = str(uuid.uuid4())
        self.seed_legacy_preparation(prep, phase="THREAD_CREATED", method="agent-start")
        self.herdr.hold_identity = True
        with self.assertRaisesRegex(hs.SupervisorError, "bootstrap is disabled"):
            self.prepare(CODEX_ONLY, prep)
        self.assertNotIn("launch_method", self.codex_item(prep))
        self.assertNotIn("fresh_launch", self.codex_item(prep))
        self.config["session_start"]["codex_launch_method"] = "explicit-pane-run"
        item = self.codex_item(prep)
        self.herdr.agents[item["agent_name"]]["agent_session"] = {"value": item["thread_id"]}
        record = self.prepare(CODEX_ONLY, prep)
        self.assertEqual((record["status"], len(self.herdr.pane_runs), len(self.herdr.starts)), ("OWNERSHIP_BOUND", 0, 1), "exactly the one legacy agent start; no direct launch")

    def test_a_journaled_explicit_preparation_is_gated_by_its_own_method_after_the_setting_reverts(self) -> None:
        prep = str(uuid.uuid4())
        self.herdr.run_mode = "dropped"
        with self.assertRaises(hs.SupervisorError):
            self.prepare(CODEX_ONLY, prep)
        seen = []
        self.sup.direct_launch_readiness = lambda policy=None: (seen.append(policy), {"ready": True, "reasons": []})[1]
        self.config["session_start"]["codex_launch_method"] = "agent-start"
        self.config["session_start"]["codex_input_policy"] = "guarded-new-pane"  # the setting moved; the journal keeps its own policy
        with self.assertRaisesRegex(hs.SupervisorError, "expected process was not observed"):
            self.prepare(CODEX_ONLY, prep)
        self.assertEqual(seen, ["require-empty-proof"], "readiness follows the journaled policy, never the changed setting")
        self.assertEqual(len(self.herdr.pane_runs), 1)

    def test_the_launch_method_setting_is_validated_and_inactive_by_default(self) -> None:
        self.assertNotIn("codex_launch_method", herdr_core.DEFAULT_CONFIG["session_start"])
        self.assertEqual(herdr_core.CODEX_LAUNCH_METHODS, hctx.CODEX_LAUNCH_METHODS)
        for bad in ("pane-run", "", None, 1, ["explicit-pane-run"]):
            config = json.loads(json.dumps(self.config))
            config["session_start"]["codex_launch_method"] = bad
            with self.subTest(bad), self.assertRaisesRegex(hs.SupervisorError, "codex_launch_method"):
                herdr_core.validate_session_start_config(config)
        config = json.loads(json.dumps(self.config))
        del config["session_start"]["codex_launch_method"]
        herdr_core.validate_session_start_config(config)
        self.assertEqual(hs.Supervisor(self.paths, config, self.herdr).launch_method(), "agent-start")

    def test_tampered_launch_journals_are_rejected(self) -> None:
        prep = str(uuid.uuid4())
        self.herdr.run_mode = "dropped"
        with self.assertRaises(hs.SupervisorError):
            self.prepare(CODEX_ONLY, prep)
        record = self.journal(prep)
        def mutated(fn):
            broken = json.loads(json.dumps(record))
            fn(broken["providers"]["codex"])
            return broken
        cases = {
            "command edited": lambda i: i["launch"]["recipe"].update(command="rm -rf /"),
            "argv edited": lambda i: i["launch"]["recipe"]["argv"].append("--evil"),
            "launch removed": lambda i: i.pop("launch"),
            "process before observation": lambda i: i["launch"].update(process={"pid": 1, "shell_pid": 2, "argv": [], "cwd": "/"}),
            "foreign pane": lambda i: i["launch"].update(pane_id="w9:p99"),
            "other thread": lambda i: i["launch"]["recipe"].update(thread_id="99999999-9999-4999-8999-999999999999"),
            "launch on a legacy item": lambda i: i.pop("launch_method"),
            "unknown method": lambda i: i.update(launch_method="shell"),
            "session id too early": lambda i: i.update(session_id="99999999-9999-4999-8999-999999999999"),
        }
        for label, fn in cases.items():
            with self.subTest(label), self.assertRaises(hs.SupervisorError):
                herdr_sessions.validate_preparation_record(self.config, mutated(fn), prep)
        herdr_sessions.validate_preparation_record(self.config, record, prep)


class RecipeAndShellTests(ExplicitCase):
    """The constructed command is executed against a harmless task-owned fixture executable (a test-only seam)."""

    RECORDER = '#!/bin/sh\nd=$(dirname "$0")\nprintf \'%s\\0\' "$@" > "$d/argv.bin"\npwd -P > "$d/cwd.txt"\nprintf \'%s\' "$CODEX_HOME" > "$d/home.txt"\nprintf \'%s\' "${HERDR_PANE_MARKER:-}" > "$d/marker.txt"\n'

    def test_hostile_paths_are_literal_data_and_the_environment_is_exactly_specified(self) -> None:
        hostile = ["it's spaced", "a;b&c|d", "$(touch PWNED)", "`touch PWNED2`", 'q"uote\\back', "glob*?[x]", "new line"[:3] + "~tilde"]
        for name in hostile:
            with self.subTest(name), tempfile.TemporaryDirectory() as root:
                base = Path(root) / name
                work, home, bin_dir = base / "work dir", base / ".codex home", base / "bin dir"
                for directory in (work, home, bin_dir):
                    directory.mkdir(parents=True)
                recorder = bin_dir / "codex"
                recorder.write_text(self.RECORDER)
                recorder.chmod(0o755)
                context = {"version": 1, "instance": hctx.instance_id(str(base / "j")), "journal_root": str(base / "j"), "codex_bin": str(recorder),
                           "codex_home": str(home), "working_directory": str(work)}
                thread = "11111111-1111-4111-8111-111111111111"
                recipe = hctx.build_launch_recipe(context, thread, executable_check=lambda path: True)
                self.assertEqual(shlex.split(recipe["command"]), recipe["argv"], "shlex round-trips every argument literally")
                done = subprocess.run(["sh", "-c", recipe["command"]], cwd=root, env={**os.environ, "HERDR_PANE_MARKER": "inherited"}, capture_output=True, text=True, timeout=20, check=False)
                self.assertEqual(done.returncode, 0, done.stderr)
                self.assertEqual((bin_dir / "argv.bin").read_bytes().split(b"\0")[:-1], [b"resume", thread.encode(), b"--no-daemon"])
                self.assertEqual((bin_dir / "cwd.txt").read_text().strip(), os.path.realpath(work))
                self.assertEqual((bin_dir / "home.txt").read_text(), str(home))
                self.assertEqual((bin_dir / "marker.txt").read_text(), "inherited", "integration variables are inherited, not cleared")
                self.assertEqual([p for p in Path(root).rglob("PWNED*")], [], "no embedded payload ran")

    def test_recipe_inputs_come_only_from_the_context_and_the_thread(self) -> None:
        context = self.helper_descriptor(str(self.project_root))
        for bad_thread in ("not-a-uuid", "11111111-1111-4111-8111-11111111111Z", "", None, "11111111-1111-4111-8111-111111111111; rm -rf /"):
            with self.subTest(bad_thread), self.assertRaises(hctx.ContextError):
                hctx.build_launch_recipe(context, bad_thread, executable_check=lambda path: True)
        for field in ("codex_bin", "codex_home", "working_directory"):
            hostile = dict(context, **{field: "relative/path"})
            with self.subTest(field), self.assertRaises(hctx.ContextError):
                hctx.build_launch_recipe(hostile, "11111111-1111-4111-8111-111111111111", executable_check=lambda path: True)
        recipe = hctx.build_launch_recipe(context, "11111111-1111-4111-8111-111111111111", executable_check=lambda path: True)
        self.assertEqual(hctx.validate_launch_recipe(json.loads(json.dumps(recipe))), recipe)
        self.assertEqual(recipe["basis"], "context-specified-by-command")

    def test_executable_and_env_drift_and_unsupported_formats_are_detected(self) -> None:
        context, thread = self.helper_descriptor(str(self.project_root)), "11111111-1111-4111-8111-111111111111"
        with tempfile.TemporaryDirectory() as root:
            env = Path(root) / "env"
            env.write_text("#!/bin/sh\nexit 0\n")
            env.chmod(0o755)
            recipe = hctx.build_launch_recipe(context, thread, env_utility=str(env), executable_check=lambda path: True, chdir_probe=lambda path: True)
            auth = dict(context=context, thread_id=thread, env_utility=str(env))
            seam = dict(executable_check=lambda path: True, chdir_probe=lambda path: True)
            self.assertIsNone(hctx.revalidate_launch_recipe(recipe, **auth, **seam))
            env.write_text("#!/bin/sh\nexit 1\n")
            self.assertIn("env executable", hctx.revalidate_launch_recipe(recipe, **auth, **seam))
            env.write_text("#!/bin/sh\nexit 0\n")
            self.assertIsNone(hctx.revalidate_launch_recipe(recipe, **auth, **seam))
            self.assertIn("no longer supports --chdir", hctx.revalidate_launch_recipe(recipe, **auth, executable_check=lambda p: True, chdir_probe=lambda p: False))
            self.assertIn("no longer a directly executable", hctx.revalidate_launch_recipe(recipe, **auth, executable_check=lambda p: False, chdir_probe=lambda p: True))
            with self.assertRaisesRegex(hctx.ContextError, "does not support --chdir"):
                hctx.build_launch_recipe(context, thread, env_utility=str(env), executable_check=lambda path: True, chdir_probe=lambda path: False)
            missing = dict(context, codex_bin=str(Path(root) / "gone"))
            with self.assertRaisesRegex(hctx.ContextError, "missing or not executable"):
                hctx.build_launch_recipe(missing, thread, executable_check=lambda path: True)

    def test_two_projects_get_different_recipes_and_no_cross_reads(self) -> None:
        a = hctx.build_launch_recipe(self.helper_descriptor(str(self.project_root)), "11111111-1111-4111-8111-111111111111", executable_check=lambda p: True)
        other = dict(self.helper_descriptor("/tmp/other-project"), codex_home="/home/test/other-codex")
        b = hctx.build_launch_recipe(other, "11111111-1111-4111-8111-111111111111", executable_check=lambda p: True)
        self.assertNotEqual((a["digest"], a["cwd"], a["codex_home"]), (b["digest"], b["cwd"], b["codex_home"]))
        self.assertNotIn("other-codex", a["command"])


class AdapterBoundaryTests(ExplicitCase):
    class Recording(herdr_cli.HerdrCli):
        def __init__(self, helps: dict | None = None) -> None:
            super().__init__("/fake/herdr")
            self.calls: list[list[str]] = []
            self.helps = helps or {}

        def _run(self, args, *, timeout, json_result=True):  # type: ignore[override]
            self.calls.append(list(args))
            return {} if json_result else ""

        def help_text(self, command):  # type: ignore[override]
            return self.helps.get(command)

    def test_exact_argv_for_pane_run_and_agent_rename(self) -> None:
        cli = self.Recording()
        command = "/usr/bin/env --chdir '/a b' CODEX_HOME='/c' /x/codex resume 11111111-1111-4111-8111-111111111111 --no-daemon"
        cli.pane_run("w1:p3", command)
        cli.rename_agent("w1:p3", "codex-run-abcd1234")
        self.assertEqual(cli.calls, [["pane", "run", "w1:p3", command], ["agent", "rename", "w1:p3", "codex-run-abcd1234"]])
        helps = {key: (FIXTURES / name).read_text() for key, name in
                 {("pane", "run"): "pane-run-help.txt", ("pane", "process-info"): "pane-process-info-help.txt", ("agent", "rename"): "agent-rename-help.txt"}.items()}
        for key, text in helps.items():
            for call in cli.calls:
                if tuple(call[:2]) == key:
                    for token in call:
                        if token.startswith("--") and token != "--":
                            self.assertIn(token, text)

    def test_capability_probe_uses_recorded_help_and_fails_closed(self) -> None:
        helps = {("pane", "run"): (FIXTURES / "pane-run-help.txt").read_text(), ("pane", "process-info"): (FIXTURES / "pane-process-info-help.txt").read_text(),
                 ("agent", "rename"): (FIXTURES / "agent-rename-help.txt").read_text()}
        self.assertEqual(self.Recording(helps).launch_capability_report(), {"pane run": True, "pane process-info (--pane)": True, "agent rename": True, "pane empty interactive input proof": False},
                         "every command is present, but no supported contract proves an empty command line (LC3)")
        partial = dict(helps)
        del partial[("agent", "rename")]
        self.assertFalse(self.Recording(partial).launch_capability_report()["agent rename"])
        self.assertEqual(set(self.Recording({}).launch_capability_report().values()), {False})
        self.assertIsNone(self.Recording(helps).pane_input_empty("w1:p3"), "unknown is never an assumption of emptiness")

    def test_provenance_declares_read_only_capture(self) -> None:
        provenance = json.loads((FIXTURES / "provenance.json").read_text())
        self.assertTrue(provenance["capture_method"].startswith("read-only"))
        self.assertIn("admission", provenance["limits"])


class ReadinessLaunchMethodTests(ExplicitCase):
    def test_explicit_readiness_uses_the_same_recipe_constructor_and_capabilities(self) -> None:
        context = self.helper_descriptor(str(self.project_root))
        seam = dict(executable_check=lambda p: True, chdir_probe=lambda p: True)
        ok = {"pane run": True, "pane process-info (--pane)": True, "agent rename": True, "pane empty interactive input proof": True}
        self.assertIsNone(hctx.explicit_launch_problem(context, ok, **seam))
        no_proof = hctx.explicit_launch_problem(context, dict(ok, **{"pane empty interactive input proof": False}), **seam)
        self.assertIn("pane empty interactive input proof", no_proof)
        self.assertIn("does not cover an earlier incomplete line", no_proof)
        self.assertIn("not verified: agent rename", hctx.explicit_launch_problem(context, dict(ok, **{"agent rename": False}), **seam))
        self.assertIn("not verified", hctx.explicit_launch_problem(context, None, **seam))
        self.assertIn("not verified", hctx.explicit_launch_problem(context, {}, **seam))
        self.assertIsNone(hctx.explicit_launch_problem(context, lambda: ok, **seam))
        self.assertIn("native binary", hctx.explicit_launch_problem(context, ok))
        self.assertIn("does not support --chdir", hctx.explicit_launch_problem(context, ok, executable_check=lambda p: True, chdir_probe=lambda p: False))
        self.assertIn("missing or not executable", hctx.explicit_launch_problem(dict(context, codex_bin="/nonexistent/codex"), ok, **seam))

    def test_the_supervisor_passes_the_selected_method_and_the_adapter_capabilities(self) -> None:
        seen = {}
        real = hctx.helper_readiness
        hctx.helper_readiness = lambda root, config, **kw: (seen.update(kw), {"ready": False, "reasons": [], "context": None, "fingerprint": None, "units": None, "activation": {}, "evidence": "x", "checks": {}})[1]
        self.addCleanup(setattr, hctx, "helper_readiness", real)
        sup = hs.Supervisor(self.paths, self.config, self.herdr, clock=self.clock.time, sleeper=self.clock.sleep)
        sup.helper_readiness()
        self.assertEqual(seen["launch_method"], "explicit-pane-run")
        self.assertEqual(seen["launch_capabilities"](), self.herdr.launch_capabilities if hasattr(self.herdr, "launch_capabilities") else self.herdr.launch_capability_report())
        self.assertIs(seen["executable_check"], hs.Supervisor.launch_executable_check, "no executable-check seam leaks into production wiring")

    def test_agent_start_stays_held_and_is_not_made_ready_by_the_explicit_method(self) -> None:
        context = self.helper_descriptor(str(self.project_root))
        self.assertIn("offers no supported way", hctx.pane_launch_problem(context, herdr_cli.HerdrCli("/fake/herdr").pane_launch_context))
        self.assertIsNone(herdr_cli.HerdrCli("/fake/herdr").pane_launch_context(), "no invented observation")

    def test_a_claude_only_start_never_asks_for_codex_readiness(self) -> None:
        self.helper_report.update(ready=False, reasons=["held"])
        self.assertTrue(self.sup.fresh_policy_supported("fresh-claude"))
        self.assertEqual(self.prepare(FRESH_CLAUDE)["status"], "OWNERSHIP_BOUND")
        self.assertEqual(self.sup.owners()["claude"]["session_id"] != CLAUDE_SESSION, True)


def coherent_forgery(recipe: dict, **parts) -> dict:
    """Edit execution-affecting parts of a journaled recipe and recompute argv, command and the public digest so the
    result is internally CONSISTENT: only the authority relationship can reject it."""
    import copy
    forged = copy.deepcopy(recipe)
    for key, value in parts.items():
        forged[key] = value
    if forged.get("form") == "direct":
        forged["argv"] = [forged["env"]["path"], "--chdir", forged["cwd"], f"CODEX_HOME={forged['codex_home']}", forged["codex"]["path"], *([] if forged["model"] is None else ["--model", forged["model"]]),
                          *hctx.codex_mode_args(forged.get("approval_mode", "inherit")), "--no-daemon"]
    else:
        forged["argv"] = [forged["env"]["path"], "--chdir", forged["cwd"], f"CODEX_HOME={forged['codex_home']}", forged["codex"]["path"], "resume", forged["thread_id"], "--no-daemon"]
    forged["command"] = shlex.join(forged["argv"])
    forged["digest"] = hctx._recipe_digest(forged)
    return forged


class RecipeAuthorityTests(ExplicitCase):
    """Review LC1: a recovered recipe is untrusted metadata. Authority is rederived from the unchanged helper context, the
    exact recorded thread and the supported env resolver BEFORE any journal-named executable is probed or run."""

    def at_intent(self) -> tuple[str, dict]:
        prep = str(uuid.uuid4())
        self.real_run = self.herdr.pane_run
        self.herdr.pane_run = None
        with self.assertRaises(hs.SupervisorError):
            self.prepare(CODEX_ONLY, prep)
        self.assertEqual(self.codex_item(prep)["status"], "LAUNCH_INTENT")
        return prep, self.journal(prep)

    def write_recipe(self, prep: str, record: dict, recipe: dict) -> None:
        record["providers"]["codex"]["launch"]["recipe"] = recipe
        (self.paths.session_preparations_dir / f"{prep}.json").write_text(json.dumps(record))
        self.herdr.pane_run = self.real_run

    def assert_untouched(self, prep: str, status: str = "LAUNCH_INTENT") -> None:
        self.assertEqual((self.herdr.pane_runs, self.herdr.agent_renames, self.herdr.bootstrap_prompts, self.herdr.prompts), ([], [], [], []))
        self.assertEqual(self.sup.owners(), self.owners)
        self.assertEqual(self.codex_item(prep)["status"], status)

    def test_a_coherently_forged_home_is_refused_before_any_launch(self) -> None:
        prep, record = self.at_intent()
        recipe = record["providers"]["codex"]["launch"]["recipe"]
        self.write_recipe(prep, record, coherent_forgery(recipe, codex_home="/home/test/different-account"))
        self.assertEqual(hctx.validate_launch_recipe(self.codex_item(prep)["launch"]["recipe"])["codex_home"], "/home/test/different-account", "the forgery is internally consistent")
        with self.assertRaisesRegex(hs.SupervisorError, r"differs from the authorized helper context.*CODEX_HOME.*not probed or run"):
            self.prepare(CODEX_ONLY, prep)
        self.assert_untouched(prep)

    def test_a_coherently_forged_codex_path_is_refused_even_with_matching_identity_metadata(self) -> None:
        prep, record = self.at_intent()
        other = Path(self.fake_codex).with_name("other-codex")
        other.write_text("#!/bin/sh\nexit 0\n")
        other.chmod(0o755)
        recipe = record["providers"]["codex"]["launch"]["recipe"]
        forged = coherent_forgery(recipe, codex=hctx._identity(str(other), content=True))
        self.write_recipe(prep, record, forged)
        with self.assertRaisesRegex(hs.SupervisorError, r"differs from the authorized helper context.*Codex executable path"):
            self.prepare(CODEX_ONLY, prep)
        self.assert_untouched(prep)

    def test_a_coherently_forged_env_utility_is_never_executed(self) -> None:
        prep, record = self.at_intent()
        with tempfile.TemporaryDirectory() as root:
            log = Path(root) / "invoked.log"
            recorder = Path(root) / "env"
            recorder.write_text(f"#!/bin/sh\necho invoked >> {shlex.quote(str(log))}\necho '--chdir'\n")
            recorder.chmod(0o755)
            recipe = record["providers"]["codex"]["launch"]["recipe"]
            self.write_recipe(prep, record, coherent_forgery(recipe, env=hctx._identity(str(recorder), content=True)))
            with self.assertRaisesRegex(hs.SupervisorError, r"differs from the authorized helper context.*env utility path"):
                self.prepare(CODEX_ONLY, prep)
            self.assertFalse(log.exists(), "the journal-named env executable must never be probed or run")
        self.assert_untouched(prep)

    def test_forged_thread_cwd_or_a_plain_command_edit_are_refused(self) -> None:
        for label, parts in (("model", {"model": "gpt-forged"}), ("cwd", {"cwd": "/elsewhere"})):
            with self.subTest(label):
                self.reset_fixture()
                self.config["session_start"]["codex_launch_method"] = "explicit-pane-run"
                prep, record = self.at_intent()
                self.write_recipe(prep, record, coherent_forgery(record["providers"]["codex"]["launch"]["recipe"], **parts))
                with self.assertRaises(hs.SupervisorError):
                    self.prepare(CODEX_ONLY, prep)
                self.assert_untouched(prep)

    def test_a_forged_recipe_is_also_refused_when_the_submission_outcome_is_unknown(self) -> None:
        self.herdr.run_mode = "dropped"
        prep = str(uuid.uuid4())
        with self.assertRaises(hs.SupervisorError):
            self.prepare(CODEX_ONLY, prep)
        for state in ("LAUNCH_SUBMITTED",):
            record = self.journal(prep)
            self.assertEqual(record["providers"]["codex"]["status"], state)
            record["providers"]["codex"]["launch"]["recipe"] = coherent_forgery(record["providers"]["codex"]["launch"]["recipe"], codex_home="/home/test/different-account")
            (self.paths.session_preparations_dir / f"{prep}.json").write_text(json.dumps(record))
        runs, polls = len(self.herdr.pane_runs), self.clock.time()
        with self.assertRaisesRegex(hs.SupervisorError, "differs from the authorized helper context"):
            self.prepare(CODEX_ONLY, prep)
        self.assertEqual((len(self.herdr.pane_runs), self.herdr.agent_renames, self.sup.owners(), self.clock.time()), (runs, [], self.owners, polls), "no observation loop, no mutation")

    def test_original_byte_identities_are_preserved_and_drift_is_refused_not_reauthorized(self) -> None:
        prep, record = self.at_intent()
        before = json.loads(json.dumps(record["providers"]["codex"]["launch"]["recipe"]))
        with open(self.fake_codex, "a") as handle:
            handle.write("# executable changed after the recipe was journaled\n")
        self.herdr.pane_run = self.real_run
        with self.assertRaisesRegex(hs.SupervisorError, "changed since the launch recipe was journaled"):
            self.prepare(CODEX_ONLY, prep)
        self.assertEqual(self.codex_item(prep)["launch"]["recipe"], before, "the journal is never regenerated")
        self.assert_untouched(prep)

    def test_same_context_recovery_still_launches_once(self) -> None:
        prep, _ = self.at_intent()
        self.herdr.pane_run = self.real_run
        self.restart()
        self.assertEqual(self.prepare(CODEX_ONLY, prep)["status"], "OWNERSHIP_BOUND")
        self.assertEqual(len(self.herdr.pane_runs), 1)

    def test_revalidation_performs_no_io_on_unauthorized_paths(self) -> None:
        context, thread = self.helper_descriptor(str(self.project_root)), "11111111-1111-4111-8111-111111111111"
        recipe = hctx.build_launch_recipe(context, thread, executable_check=lambda p: True)
        probed = []
        forged = coherent_forgery(recipe, codex_home="/home/test/other")
        problem = hctx.revalidate_launch_recipe(forged, context=context, thread_id=thread, env_utility=recipe["env"]["path"],
                                                executable_check=lambda p: probed.append(p) or True, chdir_probe=lambda p: probed.append(p) or True)
        self.assertIn("differs from the authorized helper context", problem)
        self.assertEqual(probed, [], "no executable probe before authority")
        self.assertIn("no supported env utility", hctx.recipe_authority_problem(recipe, context, thread, None))


class AliasBindingTests(ExplicitCase):
    """Review LC2: the persisted process/context binding is proven before any alias mutation or alias success."""

    def process_bound(self) -> tuple[str, dict]:
        prep = str(uuid.uuid4())
        self.herdr.rename_agent_mode = "error"
        with self.assertRaises(hs.SupervisorError):
            self.prepare(CODEX_ONLY, prep)
        item = self.codex_item(prep)
        self.assertEqual(item["status"], "PROCESS_BOUND")
        self.herdr.agent_renames.clear()
        self.herdr.rename_agent_mode = "ok"
        return prep, item

    def assert_no_alias_change(self, prep: str, pattern: str) -> None:
        with self.assertRaisesRegex(hs.SupervisorError, pattern):
            self.prepare(CODEX_ONLY, prep)
        self.assertEqual(self.herdr.agent_renames, [], "no rename of a replaced or changed target")
        self.assertEqual((self.sup.owners(), self.codex_item(prep)["status"], len(self.herdr.pane_runs)), (self.owners, "PROCESS_BOUND", 1))

    def test_a_replaced_process_is_not_renamed_on_recovery(self) -> None:
        prep, item = self.process_bound()
        self.herdr.processes[item["pane_id"]]["foreground_processes"][0]["pid"] += 1
        self.assert_no_alias_change(prep, "process changed since the launch was bound.*no alias was changed")

    def test_a_replaced_shell_or_changed_argv_or_cwd_is_not_renamed(self) -> None:
        for label, mutate in (("shell", lambda p: p.update(shell_pid=p["shell_pid"] + 1)),
                              ("argv", lambda p: p["foreground_processes"][0].update(argv=p["foreground_processes"][0]["argv"] + ["--x"])),
                              ("cwd", lambda p: p["foreground_processes"][0].update(cwd="/elsewhere"))):
            with self.subTest(label):
                self.reset_fixture()
                self.config["session_start"]["codex_launch_method"] = "explicit-pane-run"
                prep, item = self.process_bound()
                mutate(self.herdr.processes[item["pane_id"]])
                self.assert_no_alias_change(prep, "process changed since the launch was bound|not the journaled launch|no alias was changed")

    def test_a_changed_helper_context_is_not_renamed(self) -> None:
        prep, _ = self.process_bound()
        self.helper_codex_home = "/home/test/another-codex-home"
        self.assert_no_alias_change(prep, "differs from the authorized helper context|context changed since the thread was requested")

    def test_an_already_matching_alias_is_not_accepted_for_a_replaced_process(self) -> None:
        prep, item = self.process_bound()
        alias = item["agent_name"]
        detected = next(name for name, agent in self.herdr.agents.items() if agent.get("pane_id") == item["pane_id"])
        agent = self.herdr.agents.pop(detected)
        agent["name"] = alias
        self.herdr.agents[alias] = agent  # Herdr already shows the alias
        self.herdr.processes[item["pane_id"]]["foreground_processes"][0]["pid"] += 1
        self.assert_no_alias_change(prep, "process changed since the launch was bound")

    def test_a_replacement_during_repeated_observation_stops_further_alias_changes(self) -> None:
        prep = str(uuid.uuid4())
        self.herdr.rename_agent_mode = "noop"  # the rename is accepted but never takes effect, so the loop keeps observing
        real_sleep, state = self.clock.sleep, {"done": False}
        def sleep(seconds):
            real_sleep(seconds)
            if not state["done"] and self.herdr.agent_renames:
                state["done"] = True
                pane = self.herdr.agent_renames[0][0]
                self.herdr.processes[pane]["foreground_processes"][0]["pid"] += 1
        self.clock.sleep = sleep
        self.sup = self.make_supervisor()
        with self.assertRaisesRegex(hs.SupervisorError, "process changed since the launch was bound"):
            self.prepare(CODEX_ONLY, prep)
        self.assertEqual(len(self.herdr.agent_renames), 1, "exactly the pre-replacement rename; none after the replacement")
        self.assertEqual((self.sup.owners(), self.codex_item(prep)["status"]), (self.owners, "PROCESS_BOUND"))

    def test_a_replacement_between_the_check_and_the_rename_is_caught_by_the_second_check(self) -> None:
        prep, item = self.process_bound()
        real_list, state = self.herdr.list_agents, {"armed": True}
        def racing_list():
            result = real_list()
            if state["armed"] and __import__("sys")._getframe(1).f_code.co_name == "_establish_alias":
                state["armed"] = False
                self.herdr.processes[item["pane_id"]]["foreground_processes"][0]["pid"] += 1  # replaced right after the first check
            return result
        self.herdr.list_agents = racing_list
        self.assert_no_alias_change(prep, "process changed since the launch was bound")

    def test_the_same_process_still_reconciles_its_alias_without_relaunch(self) -> None:
        prep, item = self.process_bound()
        self.assertEqual(self.prepare(CODEX_ONLY, prep)["status"], "OWNERSHIP_BOUND")
        self.assertEqual(self.herdr.agent_renames, [(item["pane_id"], item["agent_name"])])
        self.assertEqual(len(self.herdr.pane_runs), 1)


class EmptyInputContractTests(ExplicitCase):
    """Review LC3: the amendment requires refusing pre-existing input. Without a supported contract that proves an empty
    command line the route is UNAVAILABLE; a sole foreground shell is not that proof."""

    def test_no_installed_herdr_contract_proves_an_empty_command_line(self) -> None:
        excerpt = (FIXTURES / "skill-pane-run-excerpt.txt").read_text()
        self.assertIn("with the shell itself in the foreground", excerpt)
        self.assertIn("not an earlier incomplete line", excerpt)
        self.assertIsNone(herdr_cli.HerdrCli("/fake/herdr").pane_input_empty("w1:p3"))
        provenance = json.loads((FIXTURES / "provenance.json").read_text())
        self.assertIn("NOT found", provenance["limits"])

    def test_the_real_adapter_keeps_the_route_unavailable_even_when_every_command_exists(self) -> None:
        helps = {("pane", "run"): (FIXTURES / "pane-run-help.txt").read_text(), ("pane", "process-info"): (FIXTURES / "pane-process-info-help.txt").read_text(),
                 ("agent", "rename"): (FIXTURES / "agent-rename-help.txt").read_text()}
        cli = AdapterBoundaryTests.Recording(helps)
        problem = hctx.explicit_launch_problem(self.helper_descriptor(str(self.project_root)), cli.launch_capability_report, executable_check=lambda p: True, chdir_probe=lambda p: True)
        self.assertIn("pane empty interactive input proof", problem)
        self.assertIn("does not cover an earlier incomplete line", problem)

    def test_a_missing_or_unknown_or_pending_proof_refuses_before_the_intent(self) -> None:
        for label, setup in (("pending input", lambda: setattr(self.herdr, "input_empty_mode", "pending")),
                             ("unknown", lambda: setattr(self.herdr, "input_empty_mode", "unknown")),
                             ("no contract at all", lambda: setattr(self.herdr, "pane_input_empty", None))):
            with self.subTest(label):
                self.reset_fixture()
                self.config["session_start"]["codex_launch_method"] = "explicit-pane-run"
                setup()
                prep = str(uuid.uuid4())
                with self.assertRaisesRegex(hs.SupervisorError, "cannot be proven to have an empty command line.*nothing was submitted"):
                    self.prepare(CODEX_ONLY, prep)
                self.assertEqual((self.herdr.pane_runs, self.codex_item(prep)["status"], self.sup.owners()), ([], "FRESH_PANE_CREATED", self.owners))
                self.assertNotIn("launch", self.codex_item(prep))

    def test_input_that_appears_after_the_intent_blocks_the_submission(self) -> None:
        prep = str(uuid.uuid4())
        real = self.herdr.pane_run
        self.herdr.pane_run = None
        with self.assertRaises(hs.SupervisorError):
            self.prepare(CODEX_ONLY, prep)
        self.herdr.input_empty_mode = "pending"
        self.herdr.pane_run = real
        with self.assertRaisesRegex(hs.SupervisorError, "cannot be proven to have an empty command line"):
            self.prepare(CODEX_ONLY, prep)
        self.assertEqual((self.herdr.pane_runs, self.codex_item(prep)["status"]), ([], "LAUNCH_INTENT"))

    def test_a_sole_foreground_idle_shell_alone_does_not_pass(self) -> None:
        self.herdr.input_empty_mode = "unknown"
        prep = str(uuid.uuid4())
        with self.assertRaises(hs.SupervisorError):
            self.prepare(CODEX_ONLY, prep)
        pane = self.codex_item(prep)["pane_id"]
        info = self.herdr.process_info(pane)
        self.assertEqual(info["foreground_processes"][0]["pid"], info["shell_pid"], "the idle-shell facts held")
        self.assertEqual(self.herdr.pane_runs, [])
