"""Direct fresh-Codex launch (no pre-created thread, no `resume` argument): the new recipe form, its authority and drift rules, the proof that the legacy form is untouched, the real-consumer
regression for the captured failure (a helper-created thread id that the CLI cannot resume), legacy-journal safety and the previous-reader rollback facts. Fixtures and fakes only: no real
Codex, no live Herdr, no provider call."""

from __future__ import annotations

import copy
import json
import os
import shlex
import subprocess
import sys
import textwrap
import unittest
import uuid
from pathlib import Path

import herdr_helper_context as hctx
import herdr_sessions
from test_fresh_session_issue5 import CODEX_ONLY, Issue5Case
from v2_fixtures import CLAUDE_SESSION, CODEX_SESSION, V2Case, hs

THREAD = "0199b3c8-1e2d-7c3a-9f4e-5a6b7c8d9e0f"
BEFORE_LIB = Path(os.environ.get("HERDR_PREVIOUS_LIBRARY", "/nonexistent/herdr-previous-library"))
HAS_PREVIOUS_LIBRARY = all((BEFORE_LIB / name).is_file() for name in ("herdr_sessions.py", "herdr_helper_context.py"))
SEAM = dict(executable_check=lambda path: True, chdir_probe=lambda path: True)


def run_in_before_library(script: str, payload: object) -> subprocess.CompletedProcess:
    """Run `script` in an isolated interpreter whose ONLY library is the frozen before tree (the reader a rollback would restore); `payload` goes in on stdin as JSON."""
    return subprocess.run([sys.executable, "-I", "-B", "-c", script, str(BEFORE_LIB)], input=json.dumps(payload), capture_output=True, text=True, timeout=120, cwd="/",
                          env={"PATH": "/usr/bin:/bin", "HOME": "/nonexistent", "PYTHONDONTWRITEBYTECODE": "1"}, check=False)


class DirectRecipeTests(V2Case):
    def context(self) -> dict:
        return self.helper_descriptor(str(self.project_root))

    def recipe(self, model=None, mode="inherit") -> dict:
        return hctx.build_direct_launch_recipe(self.context(), model, approval_mode=mode, **SEAM)

    def test_shape_and_exact_argv_for_every_model_and_mode_combination(self) -> None:
        context = self.context()
        env = hctx.resolve_env_utility()
        for model, mode, tail in ((None, "inherit", []), ("gpt-x", "inherit", ["--model", "gpt-x"]), (None, "auto-review", ["--approve-for-me"]), ("gpt-x", "auto-review", ["--model", "gpt-x", "--approve-for-me"])):
            with self.subTest(model=model, mode=mode):
                recipe = self.recipe(model, mode)
                self.assertEqual(recipe["argv"], [os.path.realpath(env), "--chdir", context["working_directory"], f"CODEX_HOME={context['codex_home']}", context["codex_bin"], *tail, "--no-daemon"])
                self.assertEqual(recipe["command"], shlex.join(recipe["argv"]))
                self.assertEqual((recipe["form"], recipe["model"], "thread_id" in recipe, "resume" in recipe["argv"]), ("direct", model, False, False))
                self.assertEqual(("approval_mode" in recipe), mode != "inherit")
                self.assertEqual(hctx.validate_launch_recipe(copy.deepcopy(recipe)), recipe)
                self.assertIsNone(hctx.recipe_authority_problem(recipe, context, None, env, mode, model))

    def test_every_malformed_or_tampered_direct_recipe_is_rejected(self) -> None:
        good = self.recipe("gpt-x", "auto-review")

        def retampered(**changes):
            recipe = copy.deepcopy(good)
            recipe.update(changes)
            recipe["digest"] = hctx._recipe_digest(recipe)
            return recipe

        cases = {
            "argv edited": {**copy.deepcopy(good), "argv": [*good["argv"], "--dangerously-bypass-approvals-and-sandbox"]},
            "command edited": {**copy.deepcopy(good), "command": good["command"] + " --extra"},
            "digest edited": {**copy.deepcopy(good), "digest": "0" * 64},
            "thread id smuggled in": retampered(thread_id=THREAD),
            "resume form without thread": {k: v for k, v in good.items() if k != "model"},
            "mode value inherit": retampered(approval_mode="inherit"),
            "unknown mode": retampered(approval_mode="bypass"),
            "version as bool": retampered(version=True),
            "method changed": retampered(method="agent-start"),
            "relative cwd": retampered(cwd="work"),
        }
        for label, value in cases.items():
            with self.subTest(label), self.assertRaises(hctx.ContextError):
                hctx.validate_launch_recipe(value)
        for label, model in (("option-like", "--dangerously"), ("space", "a b"), ("empty", ""), ("too long", "m" * 129), ("integer", 5), ("bool", True), ("newline", "a\nb")):
            with self.subTest(label), self.assertRaises(hctx.ContextError):
                hctx.build_direct_launch_recipe(self.context(), model, **SEAM)

    def test_authority_is_rederived_in_both_forms_and_a_form_swap_is_refused(self) -> None:
        context, env = self.context(), hctx.resolve_env_utility()
        direct = self.recipe("gpt-x")
        self.assertIn("model", hctx.recipe_authority_problem(direct, context, None, env, "inherit", "gpt-other"))
        self.assertIn("approval mode", hctx.recipe_authority_problem(direct, context, None, env, "auto-review", "gpt-x"))
        self.assertIn("not of the form", hctx.recipe_authority_problem(direct, context, THREAD, env, "inherit", None), "a direct recipe is never accepted for a recorded thread")
        legacy = hctx.build_launch_recipe(context, THREAD, **SEAM)
        self.assertIn("not of the form", hctx.recipe_authority_problem(legacy, context, None, env, "inherit", None), "a legacy recipe is never accepted as a direct launch")
        self.assertIsNone(hctx.recipe_authority_problem(legacy, context, THREAD, env, "inherit"))
        for field, value in (("working_directory", "/elsewhere"), ("codex_home", "/home/test/other-account")):
            self.assertIn("differs", hctx.recipe_authority_problem(direct, {**context, field: value}, None, env, "inherit", "gpt-x"))

    def test_drift_between_intent_and_submission_is_refused_for_the_direct_form_too(self) -> None:
        context, env = self.context(), hctx.resolve_env_utility()
        recipe = self.recipe()
        self.assertIsNone(hctx.revalidate_launch_recipe(recipe, context=context, thread_id=None, env_utility=env, **SEAM))
        with open(self.fake_codex, "a") as handle:
            handle.write("# changed after the recipe was journaled\n")
        self.assertIn("changed since the launch recipe was journaled", hctx.revalidate_launch_recipe(recipe, context=context, thread_id=None, env_utility=env, **SEAM))

    @unittest.skipUnless(HAS_PREVIOUS_LIBRARY, "optional historical-reader oracle: set HERDR_PREVIOUS_LIBRARY to a frozen pre-direct-launch library")
    def test_the_legacy_form_is_byte_for_byte_what_the_installed_library_builds_and_reads(self) -> None:
        script = textwrap.dedent('''
            import json, sys
            sys.path.insert(0, sys.argv[1])
            import herdr_helper_context as old
            request = json.loads(sys.stdin.read())
            seam = dict(executable_check=lambda p: True, chdir_probe=lambda p: True)
            built = [old.build_launch_recipe(request["context"], request["thread"], approval_mode=mode, **seam) for mode in ("inherit", "auto-review")]
            verdicts = {}
            for label, recipe in request["recipes"].items():
                try:
                    old.validate_launch_recipe(recipe)
                    verdicts[label] = "accepted"
                except Exception as error:
                    verdicts[label] = type(error).__name__
            print(json.dumps({"built": built, "verdicts": verdicts}))
        ''')
        context = self.context()
        legacy = [hctx.build_launch_recipe(context, THREAD, approval_mode=mode, **SEAM) for mode in ("inherit", "auto-review")]
        direct = self.recipe("gpt-x", "auto-review")
        done = run_in_before_library(script, {"context": context, "thread": THREAD, "recipes": {"legacy": legacy[0], "legacy-auto": legacy[1], "direct": direct}})
        self.assertEqual(done.returncode, 0, done.stderr)
        result = json.loads(done.stdout)
        self.assertEqual(result["built"], legacy, "the legacy recipe (digest included) is unchanged by the amendment")
        self.assertEqual(result["verdicts"], {"legacy": "accepted", "legacy-auto": "accepted", "direct": "ContextError"}, "the installed reader reads legacy recipes and rejects the new form")


class CapturedFailureRegressionTests(Issue5Case):
    """The captured failure: the helper returned ok=true with a thread id, but `codex resume <id>` then exited 'No saved session found'. The new path has no such id to resume."""

    def forbid_the_helper(self) -> None:
        class Refuses:
            def __getattr__(self, name):
                raise AssertionError(f"the helper gateway must not be used by a direct fresh Codex start: {name}")

        self.sup.reset_gateway = Refuses()

    def test_a_cli_that_cannot_resume_any_helper_thread_does_not_matter_to_the_direct_start(self) -> None:
        self.forbid_the_helper()
        spawn = self.herdr._spawn
        def spawn_without_resume(pane, command, *, wrong=False):
            if " resume " in command:
                return  # 'No saved session found': the process exits at once and Herdr never detects an agent
            return spawn(pane, command, wrong=wrong)
        self.herdr._spawn = spawn_without_resume
        prep = str(uuid.uuid4())
        record = self.prepare(CODEX_ONLY, prep)
        item = record["providers"]["codex"]
        self.assertEqual(record["status"], "OWNERSHIP_BOUND")
        self.assertEqual(self.thread_journal.requests, [], "zero fresh-thread helper calls")
        self.assertEqual(len(self.herdr.pane_runs), 1)
        self.assertNotIn(" resume ", self.herdr.pane_runs[0][1])
        self.assertEqual(item["session_id"], self.herdr.next_sessions["codex"])
        self.assertFalse([key for key in item if key.startswith("thread")], "no thread field is journaled")
        self.assertEqual(self.herdr.prompts, [])

    def test_the_same_cli_failure_on_a_legacy_journal_is_contained_never_repeated_and_never_converted(self) -> None:
        spawn = self.herdr._spawn
        def spawn_without_resume(pane, command, *, wrong=False):
            if " resume " in command:
                return
            return spawn(pane, command, wrong=wrong)
        self.herdr._spawn = spawn_without_resume
        prep = str(uuid.uuid4())
        self.seed_legacy_preparation(prep, phase="THREAD_CREATED")
        with self.assertRaises(hs.SupervisorError):
            self.prepare(CODEX_ONLY, prep)
        self.assertEqual(len(self.herdr.pane_runs), 1, "the one legacy launch")
        self.assertIn(" resume ", self.herdr.pane_runs[0][1])
        for _ in range(2):
            with self.assertRaises(hs.SupervisorError):
                self.make_supervisor().prepare_task_sessions(CODEX_ONLY, preparation_id=prep)
        self.assertEqual(len(self.herdr.pane_runs), 1, "held: no second launch, no direct start")
        self.assertNotIn("fresh_launch", self.journal(prep)["providers"]["codex"])
        self.assertEqual(self.sup.owners(), self.owners)


class LegacyJournalSafetyTests(Issue5Case):
    def test_every_legacy_phase_is_read_and_none_is_ever_converted_into_a_direct_launch(self) -> None:
        for phase, method in (("THREAD_CREATING", "explicit-pane-run"), ("THREAD_CREATED", "explicit-pane-run"), ("THREAD_CREATED", "agent-start"), ("AUTHORIZED", "explicit-pane-run")):
            with self.subTest(phase=phase, method=method):
                self.reset_fixture()
                prep = str(uuid.uuid4())
                self.seed_legacy_preparation(prep, phase=phase, method=method)
                herdr_sessions.validate_preparation_record(self.config, self.journal(prep), prep)  # still a valid record for this reader
                try:
                    self.prepare(CODEX_ONLY, prep)
                except hs.SupervisorError:
                    pass
                item = self.journal(prep)["providers"]["codex"]
                self.assertNotIn("fresh_launch", item)
                self.assertNotIn("launch_request", item)
                self.assertEqual(self.thread_journal.requests, [], "this library never starts a thread")
                if phase in ("THREAD_CREATING", "AUTHORIZED"):
                    self.assertEqual((self.herdr.splits, self.herdr.pane_runs, self.herdr.starts), ([], [], []), "no effect for a journal that has not reached its pane")
                else:
                    self.assertEqual(len(self.herdr.pane_runs) + len(self.herdr.starts), 1, "the single launch the legacy journal's own method prescribes")

    def test_the_settings_cannot_turn_a_legacy_journal_into_a_direct_one_and_a_direct_one_keeps_its_policy(self) -> None:
        prep = str(uuid.uuid4())
        self.seed_legacy_preparation(prep, phase="THREAD_CREATING")
        self.config["session_start"]["codex_launch_method"] = "explicit-pane-run"
        self.config["session_start"]["codex_input_policy"] = "guarded-new-pane"
        with self.assertRaisesRegex(hs.SupervisorError, "still unresolved"):
            self.prepare(CODEX_ONLY, prep)
        item = self.journal(prep)["providers"]["codex"]
        self.assertEqual((item["status"], item["input_policy"], "fresh_launch" in item), ("THREAD_CREATING", "require-empty-proof", False))


@unittest.skipUnless(HAS_PREVIOUS_LIBRARY, "optional historical-reader oracle: set HERDR_PREVIOUS_LIBRARY to a frozen pre-direct-launch library")
class PreviousReaderRollbackFactsTests(Issue5Case):
    """What the library a rollback would restore (the frozen before tree) can and cannot do with the journals of this library. Run in an isolated interpreter against the real prior code. The maintained overlay
    tool already refuses a rollback while ANY retained direct record exists, in ANY status (activation/tests/test_direct_history_rollback.py runs the actual tool); abandoning or completing a direct preparation
    changes its status only and does NOT make it readable by the previous library."""

    VALIDATE = textwrap.dedent('''
        import json, sys
        sys.path.insert(0, sys.argv[1])
        import herdr_core, herdr_sessions
        payload = json.loads(sys.stdin.read())
        config = herdr_core.resolve_config_defaults(herdr_core.deep_merge(herdr_core.DEFAULT_CONFIG, payload["config"]))
        out = {}
        for label, record in payload["records"].items():
            try:
                herdr_sessions.validate_preparation_record(config, record, record["preparation_id"])
                out[label] = "accepted"
            except Exception as error:
                out[label] = str(error)[:120]
        print(json.dumps(out))
    ''')

    def test_the_previous_reader_reads_legacy_journals_and_rejects_direct_ones_so_a_rollback_needs_them_resolved(self) -> None:
        legacy_prep, direct_prep = str(uuid.uuid4()), str(uuid.uuid4())
        self.seed_legacy_preparation(legacy_prep, phase="THREAD_CREATED")
        legacy = self.journal(legacy_prep)
        self.reset_fixture()
        self.prepare(CODEX_ONLY, direct_prep)
        direct = self.journal(direct_prep)
        config = {"project_root": str(self.project_root), "review_root": str(self.review_root), "product_repo": str(self.product_repo), "session_start": {"codex_launch_method": "explicit-pane-run"}}
        done = run_in_before_library(self.VALIDATE, {"config": config, "records": {"legacy": legacy, "direct": direct}})
        self.assertEqual(done.returncode, 0, done.stderr)
        verdicts = json.loads(done.stdout)
        self.assertEqual(verdicts["legacy"], "accepted")
        self.assertNotEqual(verdicts["direct"], "accepted", "the previous reader cannot read the new journal form: retained direct records block a rollback")

    def test_the_previous_library_can_still_abandon_an_unresolved_direct_preparation_from_the_raw_record_but_that_does_not_make_it_readable(self) -> None:
        prep = str(uuid.uuid4())
        self.herdr.run_mode = "dropped"
        with self.assertRaises(hs.SupervisorError):
            self.prepare(CODEX_ONLY, prep)
        path = self.paths.session_preparations_dir / f"{prep}.json"
        script = textwrap.dedent('''
            import json, sys, os
            sys.path.insert(0, sys.argv[1])
            import herdr_supervisor as hs
            request = json.loads(sys.stdin.read())
            paths = hs.Paths(config_file=__import__("pathlib").Path(request["root"]) / "config.json", state_dir=__import__("pathlib").Path(request["state"]))
            config = hs.resolve_config_defaults(hs.deep_merge(hs.DEFAULT_CONFIG, request["config"]))
            sup = hs.Supervisor(paths, config, object())
            print(json.dumps(sup.abandon_session_preparation(request["prep"])))
        ''')
        config = {"project_root": str(self.project_root), "review_root": str(self.review_root), "product_repo": str(self.product_repo)}
        done = run_in_before_library(script, {"root": str(path.parent.parent), "state": str(self.state_dir), "config": config, "prep": prep})
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout)["abandoned"], True)
        record = json.loads(path.read_text())
        self.assertEqual(record["status"], "ABANDONED")
        self.assertEqual(record["providers"]["codex"]["fresh_launch"], "direct-cli", "the record is kept as evidence, only its status changed")
        # the status edit is all it does: the abandoned record is still a direct record that the previous reader rejects (see test_direct_history_rollback)
        again = run_in_before_library(self.VALIDATE, {"config": config | {"session_start": {"codex_launch_method": "explicit-pane-run"}}, "records": {"abandoned-direct": record}})
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertNotEqual(json.loads(again.stdout)["abandoned-direct"], "accepted")


class WorkerSideModulesAndExplanationsTests(V2Case):
    def test_doctor_text_names_the_direct_launch_and_keeps_the_helper_line_for_quota_resets_only(self) -> None:
        import contextlib
        import io

        import herdr_command
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            herdr_command.print_doctor(self.sup.doctor())
        text = buffer.getvalue()
        self.assertIn("codex fresh:  direct-cli", text)
        self.assertIn("no pre-created thread, no resume", text)
        self.assertIn("quota-reset helper only; a fresh Codex start no longer needs it", text)

    def test_the_capability_explanation_names_the_missing_direct_component(self) -> None:
        self.sup._codex_direct_contract = {"codex_pane_local_server": False, "codex_approval_flag": True}
        report = self.sup.doctor()
        self.assertFalse(report["session_start"]["fresh_codex_available"])
        self.assertTrue(any("fresh Codex unavailable: unverified capability codex_pane_local_server" in w for w in report["warnings"]))
        self.assertEqual(report["session_start"]["fresh_codex_launch"], "direct-cli")

    def test_a_selected_method_other_than_explicit_pane_run_is_unavailable_everywhere_and_explained_and_changes_nothing(self) -> None:
        """DR2: doctor, the shared UI predicate and the execution refuse TOGETHER when the selected method cannot start a NEW fresh Codex; state is untouched."""
        self.config["session_start"]["codex_launch_method"] = "agent-start"
        self.sup = self.make_supervisor()
        owners, journal_dir = self.paths.owners_file.read_bytes(), self.paths.session_preparations_dir
        report = self.sup.doctor()
        self.assertIs(report["session_start"]["fresh_codex_available"], False)
        self.assertEqual({name for name, ok in report["session_start"]["fresh_codex_components"].items() if not ok}, {"codex_launch_method_selected"}, "every other direct component stays verified: only the selected method is wrong")
        self.assertTrue(any("session_start.codex_launch_method is agent-start" in w and "unavailable" in w for w in report["warnings"]), report["warnings"])
        for policy in ("fresh-codex", "fresh-all"):
            self.assertIs(self.sup.fresh_policy_supported(policy), False, policy)
            with self.assertRaisesRegex(hs.SupervisorError, "explicit pane-run recipe"):
                self.sup.prepare_task_sessions({"policy": policy, "profiles": {"codex": "default", "claude": "default"}}, preparation_id=str(uuid.uuid4()))
        self.assertTrue(self.sup.fresh_policy_supported("fresh-claude") and self.sup.fresh_policy_supported("preserve"), "unrelated policies are not affected")
        self.assertEqual((self.herdr.splits, self.herdr.pane_runs, self.herdr.starts, self.thread_journal.requests), ([], [], [], []))
        self.assertEqual((self.paths.owners_file.read_bytes(), list(journal_dir.glob("*.json")) if journal_dir.exists() else []), (owners, []), "no journal, no ownership change")
        self.assertEqual((self.sup.owners()["codex"]["session_id"], self.sup.owners()["claude"]["session_id"]), (CODEX_SESSION, CLAUDE_SESSION))

    def test_the_explicit_method_makes_the_same_predicates_true_and_execution_succeeds(self) -> None:
        report = self.sup.doctor()
        self.assertIs(report["session_start"]["fresh_codex_available"], True)
        self.assertTrue(report["session_start"]["fresh_codex_components"]["codex_launch_method_selected"])
        self.assertTrue(self.sup.fresh_policy_supported("fresh-codex") and self.sup.fresh_policy_supported("fresh-all"))
        record = self.sup.prepare_task_sessions({"policy": "fresh-codex", "profiles": {"codex": "default", "claude": "default"}}, preparation_id=str(uuid.uuid4()))
        self.assertEqual(record["status"], "OWNERSHIP_BOUND")

    def test_a_journaled_direct_preparation_keeps_its_method_when_the_setting_is_later_changed_and_a_legacy_one_its_own(self) -> None:
        prep = str(uuid.uuid4())
        self.herdr.run_mode = "dropped"
        with self.assertRaises(hs.SupervisorError):
            self.sup.prepare_task_sessions({"policy": "fresh-codex", "profiles": {"codex": "default", "claude": "default"}}, preparation_id=prep)
        journal = (self.paths.session_preparations_dir / f"{prep}.json").read_bytes()
        self.config["session_start"]["codex_launch_method"] = "agent-start"
        self.sup = self.make_supervisor()
        self.assertIs(self.sup.fresh_policy_supported("fresh-codex"), False, "NEW starts are unavailable")
        with self.assertRaisesRegex(hs.SupervisorError, "expected process was not observed"):  # the journaled direct preparation is still handled in its own recorded form
            self.sup.prepare_task_sessions({"policy": "fresh-codex", "profiles": {"codex": "default", "claude": "default"}}, preparation_id=prep)
        self.assertEqual(((self.paths.session_preparations_dir / f"{prep}.json").read_bytes(), len(self.herdr.pane_runs)), (journal, 1), "not reinterpreted, not replayed")
