"""Issue 5 trust/auto-review successor: the closed optional `session_start.approval_modes` extension (task-local copy of the installed library).

A mode maps to exactly one fixed provider flag (Codex `--approve-for-me`, Claude `--permission-mode auto`); `inherit` (the default) leaves every launch, recipe, journal and
digest exactly as installed. The controls assert the ACTUAL arguments, the recipe/journal authority and the absence of effects, not only messages. Fixtures and fake provider
executables only: no real Codex or Claude, no live Herdr, no model, no thread/start."""

from __future__ import annotations

import copy
import importlib.util
import json
import os
import shlex
import unittest
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import patch

import herdr_core
import herdr_helper_context as hctx
import herdr_sessions
from test_explicit_launch import ExplicitCase
from test_fresh_session_issue5 import CODEX_ONLY, FRESH_ALL
from v2_fixtures import hs

BEFORE_HCTX = Path(os.environ.get("HERDR_PRE_APPROVAL_LIBRARY", "/nonexistent/herdr-pre-approval-library")) / "herdr_helper_context.py"
HAS_PRE_APPROVAL_LIBRARY = BEFORE_HCTX.is_file()
DANGEROUS = ("--dangerously-bypass-approvals-and-sandbox", "--dangerously-bypass-hook-trust", "bypassPermissions", "dontAsk", "never", "danger-full-access", "full-auto", "yolo")


def before_hctx() -> Any:
    """The installed (unchanged) helper-context module, loaded under another name, as the oracle for the default behavior."""
    spec = importlib.util.spec_from_file_location("hctx_before_oracle", BEFORE_HCTX)
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def modes_config(**modes: str) -> dict[str, Any]:
    config = copy.deepcopy(herdr_core.DEFAULT_CONFIG)
    if modes:
        config["session_start"]["approval_modes"] = dict(modes)
    return config


class ClosedModeSetTests(unittest.TestCase):
    def test_the_two_modules_mirror_the_same_closed_set(self) -> None:
        self.assertEqual(herdr_core.APPROVAL_MODES, hctx.APPROVAL_MODES)
        self.assertEqual(herdr_core.APPROVAL_MODE_ARGS, hctx.APPROVAL_MODE_ARGS)
        self.assertEqual(herdr_core.APPROVAL_MODES, {"codex": ("inherit", "auto-review"), "claude": ("inherit", "auto")})
        self.assertEqual(herdr_core.APPROVAL_MODE_ARGS, {("codex", "auto-review"): ("--approve-for-me",), ("claude", "auto"): ("--permission-mode", "auto")})

    def test_valid_configurations_are_accepted_and_map_to_exactly_the_fixed_arguments(self) -> None:
        for config in (modes_config(), modes_config(codex="inherit"), modes_config(codex="auto-review"), modes_config(claude="auto"), modes_config(codex="auto-review", claude="auto"),
                       modes_config(codex="inherit", claude="inherit")):
            herdr_core.validate_session_start_config(config)
        self.assertEqual(herdr_core.approval_mode_args("codex", "auto-review"), ["--approve-for-me"])
        self.assertEqual(herdr_core.approval_mode_args("claude", "auto"), ["--permission-mode", "auto"])
        self.assertEqual((herdr_core.approval_mode_args("codex", "inherit"), herdr_core.approval_mode_args("claude", "inherit")), ([], []))
        self.assertEqual(herdr_sessions.approval_args(modes_config(codex="auto-review", claude="auto"), "codex"), ["--approve-for-me"])
        self.assertEqual(herdr_sessions.approval_args(modes_config(), "claude"), [])

    def test_unknown_malformed_cross_provider_and_bypass_inputs_are_all_rejected(self) -> None:
        bad: list[tuple[str, Any]] = [
            ("unknown provider", {"gemini": "auto"}), ("extra provider next to a good one", {"codex": "auto-review", "other": "auto"}),
            ("not an object (list)", ["auto"]), ("not an object (string)", "auto"), ("not an object (null)", None),
            ("codex gets the claude mode", {"codex": "auto"}), ("claude gets the codex mode", {"claude": "auto-review"}),
            ("raw flag as value", {"codex": "--approve-for-me"}), ("raw flag as value (claude)", {"claude": "--permission-mode auto"}),
            ("case variant", {"claude": "AUTO"}), ("case variant (codex)", {"codex": "Auto-Review"}), ("whitespace variant", {"claude": " auto"}),
            ("boolean", {"claude": True}), ("integer", {"codex": 1}), ("null value", {"codex": None}), ("list value", {"claude": ["auto"]}), ("empty string", {"codex": ""}),
            ("shell fragment", {"codex": "auto-review; rm -rf /"}), ("argument list object", {"codex": {"args": ["--approve-for-me"]}}),
        ] + [(f"bypass value {value}", {"codex": value}) for value in DANGEROUS] + [(f"bypass value {value} (claude)", {"claude": value}) for value in DANGEROUS]
        for label, value in bad:
            with self.subTest(label):
                config = copy.deepcopy(herdr_core.DEFAULT_CONFIG)
                config["session_start"]["approval_modes"] = value
                with self.assertRaises(hs.SupervisorError):
                    herdr_core.validate_session_start_config(config)

    def test_the_argument_mapper_refuses_everything_outside_the_closed_set(self) -> None:
        for provider, mode in (("codex", "auto"), ("claude", "auto-review"), ("codex", "bypass"), ("gemini", "auto"), ("codex", "--approve-for-me"), ("claude", "bypassPermissions")):
            with self.subTest((provider, mode)), self.assertRaises(hs.SupervisorError):
                herdr_core.approval_mode_args(provider, mode)
        for mode in (None, True, 1, "", "auto", "--approve-for-me", "bypass", ["auto-review"]):
            with self.subTest(mode), self.assertRaises(hctx.ContextError):
                hctx.codex_mode_args(mode)

    def test_the_default_configuration_digest_is_unchanged(self) -> None:
        self.assertNotIn("approval_modes", herdr_core.DEFAULT_CONFIG["session_start"])


class RecipeAuthorityModeTests(ExplicitCase):
    def context(self) -> dict[str, Any]:
        return self.helper_descriptor(str(self.project_root))

    def recipe(self, mode: str = "inherit", thread: str | None = None) -> dict[str, Any]:
        return hctx.build_launch_recipe(self.context(), thread or str(uuid.uuid4()), executable_check=lambda path: True, chdir_probe=lambda path: True, approval_mode=mode)

    @unittest.skipUnless(HAS_PRE_APPROVAL_LIBRARY, "set HERDR_PRE_APPROVAL_LIBRARY to run the historical installed-reader oracle")
    def test_an_inherit_recipe_is_byte_for_byte_the_installed_recipe(self) -> None:
        thread = str(uuid.uuid4())
        installed = before_hctx().build_launch_recipe(self.context(), thread, executable_check=lambda path: True, chdir_probe=lambda path: True)
        self.assertEqual(self.recipe("inherit", thread), installed)
        self.assertEqual(hctx.build_launch_recipe(self.context(), thread, executable_check=lambda path: True, chdir_probe=lambda path: True), installed, "the mode defaults to inherit")
        self.assertNotIn("approval_mode", installed)
        self.assertEqual(hctx.validate_launch_recipe(installed), installed, "an installed-format recipe still validates")
        self.assertIsNone(hctx.recipe_authority_problem(installed, self.context(), thread, installed["env"]["path"]))
        self.assertIsNone(hctx.recipe_authority_problem(installed, self.context(), thread, installed["env"]["path"], "inherit"))

    def test_the_auto_review_recipe_places_the_one_fixed_flag_between_the_thread_and_no_daemon(self) -> None:
        thread = str(uuid.uuid4())
        recipe = self.recipe("auto-review", thread)
        self.assertEqual(recipe["argv"][-4:], ["resume", thread, "--approve-for-me", "--no-daemon"])
        self.assertEqual(recipe["approval_mode"], "auto-review")
        self.assertEqual(shlex.split(recipe["command"]), recipe["argv"])
        self.assertNotEqual(recipe["digest"], self.recipe("inherit", thread)["digest"])
        self.assertEqual(hctx.validate_launch_recipe(recipe), recipe)
        self.assertIsNone(hctx.recipe_authority_problem(recipe, self.context(), thread, recipe["env"]["path"], "auto-review"))
        self.assertIsNone(hctx.revalidate_launch_recipe(recipe, context=self.context(), thread_id=thread, env_utility=recipe["env"]["path"], executable_check=lambda p: True,
                                                        chdir_probe=lambda p: True, approval_mode="auto-review"))

    def test_unsupported_modes_never_build_a_recipe(self) -> None:
        for mode in ("auto", "bypass", "--approve-for-me", "", None, True, "inherit "):
            with self.subTest(mode), self.assertRaises(hctx.ContextError):
                self.recipe(mode)  # type: ignore[arg-type]

    def forge(self, recipe: dict[str, Any], argv_tail: list[str], **extra: Any) -> dict[str, Any]:
        """Internally CONSISTENT forgery: command, argv and the public digest are recomputed, so only the validators/authority can reject it."""
        forged = copy.deepcopy(recipe)
        forged.update(extra)
        forged["argv"] = [forged["env"]["path"], "--chdir", forged["cwd"], f"CODEX_HOME={forged['codex_home']}", forged["codex"]["path"], "resume", forged["thread_id"], *argv_tail]
        forged["command"] = shlex.join(forged["argv"])
        forged["digest"] = hctx._recipe_digest(forged)
        return forged

    def test_a_coherently_forged_flag_cannot_be_added_removed_or_substituted(self) -> None:
        thread = str(uuid.uuid4())
        inherit, auto = self.recipe("inherit", thread), self.recipe("auto-review", thread)
        cases: list[tuple[str, dict[str, Any], str]] = [
            ("bypass appended to an inherit recipe", self.forge(inherit, ["--dangerously-bypass-approvals-and-sandbox", "--no-daemon"]), "inherit"),
            ("bypass appended to an auto-review recipe", self.forge(auto, ["--approve-for-me", "--dangerously-bypass-approvals-and-sandbox", "--no-daemon"], approval_mode="auto-review"), "auto-review"),
            ("bypass substituted for the auto-review flag", self.forge(auto, ["--dangerously-bypass-approvals-and-sandbox", "--no-daemon"], approval_mode="auto-review"), "auto-review"),
            ("sandbox widened", self.forge(auto, ["--approve-for-me", "--sandbox", "danger-full-access", "--no-daemon"], approval_mode="auto-review"), "auto-review"),
            ("flag added without a recorded mode", self.forge(inherit, ["--approve-for-me", "--no-daemon"]), "inherit"),
            ("mode recorded but the flag removed", self.forge(auto, ["--no-daemon"], approval_mode="auto-review"), "auto-review"),
            ("inherit written as a value", self.forge(inherit, ["--no-daemon"], approval_mode="inherit"), "inherit"),
            ("unknown mode value", self.forge(inherit, ["--no-daemon"], approval_mode="bypass"), "inherit"),
            ("claude mode on a codex recipe", self.forge(inherit, ["--no-daemon"], approval_mode="auto"), "inherit"),
            ("non-string mode", self.forge(inherit, ["--no-daemon"], approval_mode=["auto-review"]), "inherit"),
        ]
        for label, forged, authorized in cases:
            with self.subTest(label):
                with self.assertRaises(hctx.ContextError):
                    hctx.validate_launch_recipe(forged)
                problem = hctx.recipe_authority_problem(forged, self.context(), thread, forged["env"]["path"], authorized)
                self.assertIsNotNone(problem)
                self.assertIn("not probed or run" if "malformed" not in problem else "malformed", problem)

    def test_a_consistent_recipe_under_another_mode_than_the_configured_one_is_refused(self) -> None:
        thread = str(uuid.uuid4())
        inherit, auto = self.recipe("inherit", thread), self.recipe("auto-review", thread)
        for label, recipe, configured, expected in (("auto recipe, inherit configured", auto, "inherit", "approval mode"), ("inherit recipe, auto configured", inherit, "auto-review", "approval mode")):
            with self.subTest(label):
                problem = hctx.recipe_authority_problem(recipe, self.context(), thread, recipe["env"]["path"], configured)
                self.assertIsNotNone(problem)
                self.assertIn(expected, problem)
                self.assertIsNotNone(hctx.revalidate_launch_recipe(recipe, context=self.context(), thread_id=thread, env_utility=recipe["env"]["path"], executable_check=lambda p: True,
                                                                  chdir_probe=lambda p: True, approval_mode=configured))
        self.assertIsNotNone(hctx.recipe_authority_problem(auto, self.context(), thread, auto["env"]["path"], "bypass"), "an unsupported authorized mode is itself refused")


class FreshLaunchModeTests(ExplicitCase):
    def setUp(self) -> None:
        super().setUp()
        self.sup.approval_mode_supported = lambda provider, mode: True

    def configure(self, **modes: str) -> None:
        self.config["session_start"]["approval_modes"] = dict(modes)
        self.sup = self.make_supervisor()
        self.sup.approval_mode_supported = lambda provider, mode: True

    def effects(self) -> tuple[int, int, int, int]:
        return self.counts()

    def test_default_inherit_launches_are_exactly_the_installed_ones_for_seed_replacement_style_fresh_all(self) -> None:
        prep = str(uuid.uuid4())
        record = self.prepare(FRESH_ALL, prep)
        item = record["providers"]["codex"]
        self.assertEqual(shlex.split(self.herdr.pane_runs[0][1])[-2:], [os.path.realpath(self.fake_codex), "--no-daemon"], "inherit adds no flag; a new Codex has no resume argument")
        self.assertNotIn("resume", shlex.split(self.herdr.pane_runs[0][1]))
        self.assertEqual([args for _, kind, _, args in self.herdr.starts if kind == "claude"], [[]])
        for provider in ("codex", "claude"):
            self.assertNotIn("approval_mode", record["providers"][provider])
        self.assertNotIn("approval_mode", item["launch"]["recipe"])
        herdr_sessions.validate_preparation_record(self.config, self.journal(prep), prep)

    def test_auto_modes_reach_both_fresh_launches_and_the_process_authority_with_the_exact_arguments(self) -> None:
        self.configure(codex="auto-review", claude="auto")
        prep = str(uuid.uuid4())
        record = self.prepare(FRESH_ALL, prep)
        codex, claude = record["providers"]["codex"], record["providers"]["claude"]
        argv = shlex.split(self.herdr.pane_runs[0][1])
        self.assertEqual(argv[-3:], [os.path.realpath(self.fake_codex), "--approve-for-me", "--no-daemon"])
        self.assertEqual(codex["launch"]["recipe"]["approval_mode"], "auto-review")
        self.assertEqual(codex["launch"]["process"]["argv"][1:], ["--approve-for-me", "--no-daemon"], "the observed process carries the mode")
        starts = {kind: args for _, kind, _, args in self.herdr.starts}
        self.assertEqual(starts["claude"], ["--permission-mode", "auto"])
        self.assertEqual((codex["approval_mode"], claude["approval_mode"]), ("auto-review", "auto"))
        self.assertEqual((len(self.thread_journal.requests), len(self.herdr.splits), len(self.herdr.pane_runs), len(self.herdr.starts)), (0, 2, 1, 1), "no thread, two splits, one launch each")
        self.assertEqual(record["status"], "OWNERSHIP_BOUND")
        herdr_sessions.validate_preparation_record(self.config, self.journal(prep), prep)

    def test_each_provider_can_be_set_alone_and_the_other_stays_installed_behavior(self) -> None:
        self.configure(claude="auto")
        record = self.prepare(FRESH_ALL)
        self.assertEqual(shlex.split(self.herdr.pane_runs[0][1])[-1], "--no-daemon")
        self.assertNotIn("--approve-for-me", self.herdr.pane_runs[0][1])
        self.assertEqual([args for _, kind, _, args in self.herdr.starts if kind == "claude"], [["--permission-mode", "auto"]])
        self.assertNotIn("approval_mode", record["providers"]["codex"])
        self.reset_fixture()
        self.config["session_start"]["codex_launch_method"] = "explicit-pane-run"
        self.configure(codex="auto-review")
        record = self.prepare(FRESH_ALL)
        self.assertIn("--approve-for-me", self.herdr.pane_runs[0][1])
        self.assertEqual([args for _, kind, _, args in self.herdr.starts if kind == "claude"], [[]])
        self.assertNotIn("approval_mode", record["providers"]["claude"])

    def test_model_and_mode_compose_in_a_fixed_order_and_the_process_check_accepts_exactly_that(self) -> None:
        self.config["session_start"]["model_profiles"]["claude"]["fast"] = {"label": "Fast", "model": "claude-fast"}
        self.configure(codex="auto-review", claude="auto")
        self.sup.model_flag_supported = lambda provider: True
        record = self.prepare({"policy": "fresh-all", "profiles": {"codex": "default", "claude": "fast"}})
        self.assertEqual([args for _, kind, _, args in self.herdr.starts if kind == "claude"], [["--model", "claude-fast", "--permission-mode", "auto"]])
        self.assertEqual(record["status"], "OWNERSHIP_BOUND")

    def test_a_legacy_agent_start_journal_for_codex_still_carries_the_mode_on_its_resume(self) -> None:
        self.configure(codex="auto-review")
        prep = str(uuid.uuid4())
        self.seed_legacy_preparation(prep, phase="THREAD_CREATED", method="agent-start")
        self.prepare(CODEX_ONLY, prep)
        self.assertEqual([args for _, kind, _, args in self.herdr.starts if kind == "codex"][0][2:], ["--approve-for-me", "--no-daemon"])
        self.assertEqual(self.herdr.pane_runs, [], "the legacy journal keeps its generic agent start; it is not converted")

    def test_an_unsupported_cli_capability_refuses_before_any_split_launch_or_owner_change(self) -> None:
        for provider, mode, selection, pattern in (("codex", "auto-review", CODEX_ONLY, "unverified capability codex_approval_flag"),
                                                   ("claude", "auto", {"policy": "fresh-claude", "profiles": {"codex": "default", "claude": "default"}}, "does not expose the capability required by approval mode"),
                                                   ("codex", "auto-review", FRESH_ALL, "unverified capability codex_approval_flag"), ("claude", "auto", FRESH_ALL, "does not expose the capability required by approval mode")):
            with self.subTest((provider, selection["policy"])):
                self.reset_fixture()
                self.configure(**{provider: mode})
                self.sup.approval_mode_supported = lambda p, m, provider=provider: p != provider
                self.sup._codex_direct_contract = {"codex_pane_local_server": True, "codex_approval_flag": provider != "codex"}  # the top-level help of the direct launch lacks the flag
                with self.assertRaisesRegex(hs.SupervisorError, pattern):
                    self.prepare(selection)
                self.assertEqual((self.thread_journal.requests, self.herdr.splits, self.herdr.pane_runs, self.herdr.starts, self.herdr.prompts), ([], [], [], [], []))
                self.assertEqual(self.sup.owners(), self.owners)
                self.assertFalse(list(self.paths.session_preparations_dir.glob("*.json")) if self.paths.session_preparations_dir.exists() else [])

    def hold_at_intent(self, prep: str) -> Any:
        real = self.herdr.pane_run
        self.herdr.pane_run = None
        with self.assertRaises(hs.SupervisorError):
            self.prepare(CODEX_ONLY, prep)
        self.assertEqual(self.codex_item(prep)["status"], "LAUNCH_INTENT")
        return real

    def test_a_mode_change_invalidates_an_already_prepared_launch_in_both_directions(self) -> None:
        for before, after, label in (({}, {"codex": "auto-review"}, "inherit -> auto-review"), ({"codex": "auto-review"}, {}, "auto-review -> inherit")):
            with self.subTest(label):
                self.reset_fixture()
                self.config["session_start"]["codex_launch_method"] = "explicit-pane-run"
                self.configure(**before)
                prep = str(uuid.uuid4())
                real = self.hold_at_intent(prep)
                self.herdr.pane_run = real
                counts = self.counts()
                self.configure(**after)
                with self.assertRaisesRegex(hs.SupervisorError, "approval mode changed since the preparation was authorized"):
                    self.prepare(CODEX_ONLY, prep)
                self.assertEqual(self.counts(), counts, "nothing was repeated or submitted")
                self.assertEqual(self.herdr.pane_runs, [])
                self.assertEqual(self.codex_item(prep)["status"], "LAUNCH_INTENT")
                self.assertEqual(self.sup.owners(), self.owners)
                self.configure(**before)  # restoring the recorded mode lets the same preparation finish, once
                self.assertEqual(self.prepare(CODEX_ONLY, prep)["status"], "OWNERSHIP_BOUND")
                self.assertEqual(len(self.herdr.pane_runs), 1)

    # CI1: the configured-versus-recorded mode authority is enforced BEFORE binding recovery and before a bound preparation is accepted for dispatch.
    DRIFTS = (("inherit -> auto-review", {}, {"codex": "auto-review"}), ("auto-review -> inherit", {"codex": "auto-review"}, {}))

    def journal_file(self, prep: str) -> Path:
        return self.paths.session_preparations_dir / f"{prep}.json"

    def durable(self, prep: str) -> tuple[bytes, bytes, tuple[int, int, int, int]]:
        """Everything a refused resume must leave untouched: the owners file, the preparation journal and every launch/split/thread effect."""
        return self.paths.owners_file.read_bytes(), self.journal_file(prep).read_bytes(), self.counts()

    def stop_at_binding(self, prep: str, *, owners_durable: bool) -> None:
        """Interrupt a real preparation inside the BINDING phase: before the owners write, or after it and before the journal says OWNERSHIP_BOUND."""
        real = herdr_sessions.atomic_write_json

        def crash(path: Any, value: Any, *args: Any, **kwargs: Any) -> Any:
            if (not owners_durable and path == self.paths.owners_file) or (owners_durable and path == self.journal_file(prep) and value.get("status") == "OWNERSHIP_BOUND"):
                raise RuntimeError("simulated crash inside BINDING")
            return real(path, value, *args, **kwargs)

        with patch.object(herdr_sessions, "atomic_write_json", crash), self.assertRaises(RuntimeError):
            self.prepare(CODEX_ONLY, prep)
        self.assertEqual(self.journal(prep)["status"], "BINDING")

    def test_a_mode_change_is_refused_at_binding_on_both_sides_of_the_durable_owners_write_in_both_directions(self) -> None:
        for owners_durable in (False, True):
            for label, before, after in self.DRIFTS:
                with self.subTest(label, owners_already_replaced=owners_durable):
                    self.reset_fixture()
                    self.configure(**before)
                    prep = str(uuid.uuid4())
                    old_owners = self.sup.owners()
                    self.stop_at_binding(prep, owners_durable=owners_durable)
                    self.assertEqual(self.sup.owners() != old_owners, owners_durable)
                    snapshot = self.durable(prep)
                    self.configure(**after)
                    with self.assertRaisesRegex(hs.SupervisorError, "approval mode changed since the preparation was authorized"):
                        self.prepare(CODEX_ONLY, prep)
                    self.assertEqual(self.durable(prep), snapshot, "a refused binding recovery writes no owner, journal, launch, split or thread")
                    self.assertEqual(self.journal(prep)["status"], "BINDING")
                    self.configure(**before)  # the unchanged mode still completes the very same binding, once
                    self.assertEqual(self.prepare(CODEX_ONLY, prep)["status"], "OWNERSHIP_BOUND")
                    self.assertEqual(self.counts(), snapshot[2], "recovery launches nothing")
                    self.assertNotEqual(self.sup.owners(), old_owners)

    def test_a_bound_preparation_is_not_accepted_for_prepare_or_dispatch_under_another_mode_and_a_started_task_stays_history(self) -> None:
        for label, before, after in self.DRIFTS:
            with self.subTest(label):
                self.reset_fixture()
                self.configure(**before)
                prep = str(uuid.uuid4())
                bound = self.prepare(CODEX_ONLY, prep)
                self.assertEqual(bound["status"], "OWNERSHIP_BOUND")
                snapshot = self.durable(prep)
                self.configure(**after)
                with self.assertRaisesRegex(hs.SupervisorError, "approval mode changed since the preparation was authorized"):
                    self.prepare(CODEX_ONLY, prep)
                with self.assertRaisesRegex(hs.SupervisorError, "approval mode changed since the preparation was authorized"):
                    self.sup._mark_session_preparation_started(prep)
                self.assertEqual(self.durable(prep), snapshot)
                self.assertEqual(self.journal(prep)["status"], "OWNERSHIP_BOUND")
                self.configure(**before)  # unchanged mode: the bound preparation is accepted and dispatched as before
                self.assertEqual(self.prepare(CODEX_ONLY, prep)["status"], "OWNERSHIP_BOUND")
                self.assertEqual(self.sup._mark_session_preparation_started(prep)["status"], "TASK_STARTED")
                started = self.durable(prep)
                self.configure(**after)  # a dispatched task is history: a later configuration change neither refuses nor rewrites it
                self.assertEqual(self.prepare(CODEX_ONLY, prep)["status"], "TASK_STARTED")
                self.assertEqual(self.sup._mark_session_preparation_started(prep)["status"], "TASK_STARTED")
                self.assertEqual(self.durable(prep), started)
                self.assertEqual(self.journal(prep)["providers"]["codex"].get("approval_mode", "inherit"), next(iter(before.values()), "inherit"), "the recorded mode is never rewritten")

    def test_an_old_default_journal_without_the_field_stays_valid_and_resumes_under_inherit(self) -> None:
        prep = str(uuid.uuid4())
        real = self.hold_at_intent(prep)
        record = self.journal(prep)
        self.assertNotIn("approval_mode", record["providers"]["codex"])
        self.assertNotIn("approval_mode", record["providers"]["codex"]["launch"]["recipe"])
        herdr_sessions.validate_preparation_record(self.config, record, prep)
        self.herdr.pane_run = real
        self.restart()
        self.assertEqual(self.prepare(CODEX_ONLY, prep)["status"], "OWNERSHIP_BOUND")

    def test_a_forged_journal_recipe_cannot_add_remove_or_substitute_a_flag_even_with_a_recomputed_digest(self) -> None:
        self.configure(codex="auto-review")
        helper = RecipeAuthorityModeTests("test_unsupported_modes_never_build_a_recipe")
        for label, tail, extra in (
            ("bypass appended", ["--approve-for-me", "--dangerously-bypass-approvals-and-sandbox", "--no-daemon"], {"approval_mode": "auto-review"}),
            ("flag removed, mode kept", ["--no-daemon"], {"approval_mode": "auto-review"}),
            ("mode removed, flag kept", ["--approve-for-me", "--no-daemon"], {}),
        ):
            with self.subTest(label):
                self.reset_fixture()
                self.config["session_start"]["codex_launch_method"] = "explicit-pane-run"
                self.configure(codex="auto-review")
                prep = str(uuid.uuid4())
                real = self.hold_at_intent(prep)
                record = self.journal(prep)
                recipe = record["providers"]["codex"]["launch"]["recipe"]
                forged = copy.deepcopy(recipe)
                if "approval_mode" not in extra:
                    forged.pop("approval_mode", None)
                forged.update(extra)
                forged["argv"] = [forged["env"]["path"], "--chdir", forged["cwd"], f"CODEX_HOME={forged['codex_home']}", forged["codex"]["path"], *tail]
                forged["command"] = shlex.join(forged["argv"])
                forged["digest"] = hctx._recipe_digest(forged)
                record["providers"]["codex"]["launch"]["recipe"] = forged
                (self.paths.session_preparations_dir / f"{prep}.json").write_text(json.dumps(record))
                self.herdr.pane_run = real
                with self.assertRaises(hs.SupervisorError):
                    self.prepare(CODEX_ONLY, prep)
                self.assertEqual((self.herdr.pane_runs, self.herdr.agent_renames, self.herdr.bootstrap_prompts, self.herdr.prompts), ([], [], [], []))
                self.assertEqual(self.sup.owners(), self.owners)
        del helper

    def test_a_record_with_an_invalid_or_mismatched_mode_field_is_rejected(self) -> None:
        self.configure(codex="auto-review", claude="auto")
        prep = str(uuid.uuid4())
        self.prepare(FRESH_ALL, prep)
        good = self.journal(prep)
        for label, mutate in (
            ("inherit written", lambda r: r["providers"]["codex"].__setitem__("approval_mode", "inherit")),
            ("unknown value", lambda r: r["providers"]["claude"].__setitem__("approval_mode", "bypassPermissions")),
            ("cross-provider value", lambda r: r["providers"]["codex"].__setitem__("approval_mode", "auto")),
            ("non-string", lambda r: r["providers"]["claude"].__setitem__("approval_mode", True)),
            ("recipe and record disagree", lambda r: r["providers"]["codex"].pop("approval_mode")),
        ):
            with self.subTest(label):
                record = copy.deepcopy(good)
                mutate(record)
                with self.assertRaises(hs.SupervisorError):
                    herdr_sessions.validate_preparation_record(self.config, record, prep)

    def test_unchanged_no_daemon_and_ownership_rules_still_hold_with_the_mode_on(self) -> None:
        self.configure(codex="auto-review", claude="auto")
        self.herdr.run_mode = "wrong"  # the shell ran another thread: ownership must still be refused
        with self.assertRaises(hs.SupervisorError):
            self.prepare(CODEX_ONLY)
        self.assertEqual(self.sup.owners(), self.owners)

    def test_doctor_shows_the_modes_only_when_a_non_default_mode_is_configured(self) -> None:
        self.assertNotIn("approval_modes", self.sup.doctor()["session_start"])
        self.configure(codex="auto-review")
        self.assertEqual(self.sup.doctor()["session_start"]["approval_modes"], {"codex": "auto-review", "claude": "inherit"})


class CapabilityProbeTests(unittest.TestCase):
    CODEX_HELP = "Usage: codex resume [OPTIONS]\n      --approve-for-me\n          Route approval requests through automatic review using the workspace-write sandbox\n      --no-daemon\n"
    CLAUDE_HELP = '  --permission-mode <mode>   Permission mode to use for the session\n                             (choices: "acceptEdits", "auto",\n                             "bypassPermissions", "manual")\n  --print\n'

    class Result:
        def __init__(self, text: str, code: int = 0) -> None:
            self.stdout, self.stderr, self.returncode = text, "", code

    def runner(self, text: str, code: int = 0) -> Any:
        calls: list[list[str]] = []

        def run(argv: list[str], **kwargs: Any) -> Any:
            calls.append(argv)
            return self.Result(text, code)
        run.calls = calls  # type: ignore[attr-defined]
        return run

    def test_codex_needs_the_exact_flag_in_the_resume_help_and_claude_needs_auto_among_the_choices(self) -> None:
        run = self.runner(self.CODEX_HELP)
        self.assertTrue(herdr_sessions.probe_approval_capability("codex", "codex", "auto-review", run))
        self.assertEqual(run.calls, [["codex", "resume", "--help"]], "help only; never a session")
        run = self.runner(self.CLAUDE_HELP)
        self.assertTrue(herdr_sessions.probe_approval_capability("claude", "claude", "auto", run))
        self.assertEqual(run.calls, [["claude", "--help"]])
        self.assertTrue(herdr_sessions.probe_approval_capability("codex", "codex", "inherit", self.runner("")), "inherit needs nothing")

    def test_missing_lookalike_or_failed_help_is_unsupported(self) -> None:
        self.assertFalse(herdr_sessions.probe_approval_capability("codex", "codex", "auto-review", self.runner("--approve-for-me-later\n")))
        self.assertFalse(herdr_sessions.probe_approval_capability("codex", "codex", "auto-review", self.runner("nothing here")))
        self.assertFalse(herdr_sessions.probe_approval_capability("codex", "codex", "auto-review", self.runner(self.CODEX_HELP, code=2)))
        self.assertFalse(herdr_sessions.probe_approval_capability("claude", "claude", "auto", self.runner('  --permission-mode <mode>   (choices: "acceptEdits", "plan")\n')))
        self.assertFalse(herdr_sessions.probe_approval_capability("claude", "claude", "auto", self.runner("  --print\n")))

        def broken(argv: list[str], **kwargs: Any) -> Any:
            raise OSError("not executable")
        self.assertFalse(herdr_sessions.probe_approval_capability("codex", "codex", "auto-review", broken))


class HelperProducerCompatibilityTests(ExplicitCase):
    """The producer extension must not change what the UNCHANGED installed helper worker binds: context, fingerprint, units and the thread request."""

    @unittest.skipUnless(HAS_PRE_APPROVAL_LIBRARY, "set HERDR_PRE_APPROVAL_LIBRARY to run the historical installed-reader oracle")
    def test_context_fingerprint_units_and_the_thread_request_are_identical_to_the_installed_module(self) -> None:
        old = before_hctx()
        context = self.helper_descriptor(str(self.project_root))
        self.assertEqual(hctx.fingerprint(context), old.fingerprint(context))
        self.assertEqual(hctx.render_units(context, Path("/home/test")), old.render_units(context, Path("/home/test")))
        self.assertEqual(hctx.unit_names(context, Path("/home/test")), old.unit_names(context, Path("/home/test")))
        self.assertEqual(hctx.validate_context(dict(context)), old.validate_context(dict(context)))
        payload = herdr_sessions.thread_start_payload(self.config, "codex", "default")
        self.assertEqual(payload, {"cwd": str(self.config["project_root"]), "model": None, "ephemeral": False} | {k: v for k, v in payload.items() if k not in ("cwd", "model", "ephemeral")})
        self.config["session_start"]["approval_modes"] = {"codex": "auto-review", "claude": "auto"}
        self.assertEqual(herdr_sessions.thread_start_payload(self.config, "codex", "default"), payload, "the mode never enters the thread/start payload")
        self.assertEqual(herdr_sessions.thread_request_id("00000000-0000-4000-8000-000000000000", "codex"), "thread-00000000-0000-4000-8000-000000000000-codex")

    @unittest.skipUnless(HAS_PRE_APPROVAL_LIBRARY, "set HERDR_PRE_APPROVAL_LIBRARY to run the historical installed-reader oracle")
    def test_the_mode_is_not_part_of_the_helper_context(self) -> None:
        self.assertNotIn("approval", json.dumps(self.helper_descriptor(str(self.project_root))).lower())
        self.assertEqual(hctx.CONTEXT_KEYS if hasattr(hctx, "CONTEXT_KEYS") else None, before_hctx().CONTEXT_KEYS if hasattr(before_hctx(), "CONTEXT_KEYS") else None)


if __name__ == "__main__":
    unittest.main()
