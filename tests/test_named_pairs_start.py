"""Named pairs through the real supervisor start path with a fake Herdr: a run reaches its chosen pair only, the pair and context are frozen for
the run, switching is terminal-only, and every refusal sends/launches/renames/rebinds nothing. No real provider, Herdr or Telegram."""

from __future__ import annotations

import json
import os
import threading
import uuid

import herdr_pairs as hpair
from pair_fixtures import ALPHA, BETA, PairCase, hs, ids
from v2_fixtures import CLAUDE_SESSION, CODEX_SESSION, FakeHerdr


class StartCase(PairCase):
    def setUp(self) -> None:
        super().setUp()
        self.write_plan()
        self.alpha = self.register("Alpha", ALPHA, self.repo_a, self.review_a)
        self.beta = self.register("Beta", BETA, self.repo_b, self.review_b)

    def plan_response(self, selection: dict | None = None) -> list[dict]:
        return self.plan_for(selection if selection is not None else self.saved(self.alpha))

    def begin(self, selection: dict, run_id: str | None = None) -> str:
        """Start through apply_command (as the inbox worker does), then run the loop until it parks at the plan gate."""
        result = self.start(selection, run_id)
        self.assertTrue(result["ok"], result)
        self.herdr.responses = self.plan_response(selection)
        self.sup.resume()
        return result["run_id"]

    def finish(self) -> None:
        """Genuinely terminate the current run (cancel) so the next pair may start."""
        state = self.state()
        self.sup.apply_command(state, {"request_id": uuid.uuid4().hex[:32], "action": "cancel", "run_id": state["run_id"], "actor": "cli"})


class RunReachesItsPairOnlyTests(StartCase):
    def test_a_saved_pair_run_prompts_only_its_own_sessions_and_never_touches_global_owners(self) -> None:
        before = self.effects()
        run_id = self.begin(self.saved(self.alpha))
        state = self.state()
        self.assertEqual(state["session_pair"]["pair_id"], self.alpha["pair_id"])
        self.assertEqual(state["session_pair"]["identities"], ids(ALPHA))
        self.assertEqual(state["native_sessions"], {"codex": ALPHA["codex"][1], "claude": ALPHA["claude"][1]})
        self.assertEqual(self.prompt_targets(), {"alpha-codex"}, "the first turn went to the chosen pair's codex session, exactly once")
        self.assertEqual(len(self.herdr.prompts), 1)
        self.assertEqual(self.effects()["starts"], before["starts"])
        self.assertEqual(self.effects()["splits"], before["splits"])
        self.assertTrue(self.owners_unchanged(), "owners.json is never rewritten for a named pair")
        self.assertEqual(state["run_id"], run_id)
        self.assertEqual(state["pending_gate"]["gate_type"], "plan_approval")

    def test_the_second_pair_runs_after_the_first_genuinely_terminates_and_receives_only_its_own_traffic(self) -> None:
        self.begin(self.saved(self.alpha))
        self.finish()
        self.assertEqual(self.state()["supervisor_state"], "CANCELLED")
        self.begin(self.saved(self.beta))
        self.assertEqual(self.state()["session_pair"]["pair_id"], self.beta["pair_id"])
        self.assertEqual([name for name, _ in self.herdr.prompts], ["alpha-codex", "beta-codex"])
        self.assertTrue(self.owners_unchanged())
        self.assertEqual(len(list((self.state_dir / "run-history").glob("*.json"))), 1)

    def test_terminal_state_snapshots_are_preserved_by_run_id_before_the_next_run_replaces_them(self) -> None:
        first = self.begin(self.saved(self.alpha))
        self.finish()
        first_state = self.state()
        self.assertFalse((self.state_dir / "run-history").exists(), "nothing is copied until the single current state is replaced")
        self.begin(self.saved(self.beta))
        archived = self.state_dir / "run-history" / f"{first}.json"
        self.assertTrue(archived.exists())
        self.assertEqual(oct(archived.stat().st_mode & 0o777), "0o600")
        kept = json.loads(archived.read_text())
        self.assertEqual((kept["run_id"], kept["supervisor_state"], kept["session_pair"]["name"]), (first, "CANCELLED", "Alpha"))
        self.assertEqual(kept["gate_history"], first_state["gate_history"])
        self.assertEqual(kept["pending_gate"], first_state["pending_gate"], "decisions and history are retained")
        self.assertNotEqual(self.state()["run_id"], first)

    def test_a_legacy_start_after_a_named_run_uses_the_global_owners_again(self) -> None:
        self.begin(self.saved(self.alpha))
        self.finish()
        self.assertEqual(self.sup.owners(), self.owners, "a terminal named run releases its binding")
        legacy = {"policy": "preserve", "profiles": {"codex": "default", "claude": "default"}}
        self.herdr.responses = self.plan_response(legacy)
        result = self.start(legacy)
        self.assertTrue(result["ok"])
        self.sup.resume()
        self.assertEqual(self.herdr.prompts[-1][0], "codex-main")
        self.assertNotIn("session_pair", self.state())
        self.assertTrue((self.state_dir / "run-history").exists(), "the finished named run was preserved when the legacy run replaced it")


class FrozenBindingTests(StartCase):
    def test_registry_config_global_owners_and_aliases_cannot_retarget_a_running_pair(self) -> None:
        self.begin(self.saved(self.alpha))
        # registry drift: the entry vanishes; configuration drift: roots/enabled change; global drift: owners.json now names another pair; alias drift
        hpair.registry_path(self.paths).write_text(json.dumps({"schema_version": 1, "revision": 9, "entries": {}, "reservations": {}}))
        self.config["session_pairs"] = {"enabled": False, "allowed_roots": [], "instruction_root": None}
        hs.atomic_write_json(self.paths.owners_file, ids(BETA), mode=0o600)
        self.herdr.agents["alpha-codex"]["name"] = "codex-main-renamed"
        self.herdr.agents["beta-codex"]["name"] = self.config["agents"]["codex"]["name"]  # beta now squats the configured legacy alias
        self.assertEqual(self.sup.owners(), ids(ALPHA))
        target, agent = self.sup.resolve_target("codex")
        self.assertEqual(agent["agent_session"]["value"], ALPHA["codex"][1])
        self.assertEqual(target, "w4:p2", "the exact session is addressed by its pane, never by an alias another agent holds")
        self.approve_pending()
        self.assertEqual(self.prompt_targets(), {"alpha-codex"}, "no later prompt, read, approval or resume reached another pair")

    def test_aliases_may_be_absent_when_the_same_exact_session_is_in_the_recorded_pane(self) -> None:
        for name in ("alpha-claude", "alpha-codex"):
            self.herdr.agents[name]["name"] = None
        self.begin(self.saved(self.alpha))
        self.assertEqual(self.prompt_targets(), {"alpha-codex"})
        self.assertIn(("prompt", "w4:p2"), [(c, t) for c, t in self.herdr.targets if c == "prompt"])

    def test_resume_after_a_restart_resolves_the_same_frozen_pair(self) -> None:
        self.begin(self.saved(self.alpha))
        restarted = self.make_supervisor()
        self.assertEqual(restarted.owners(), ids(ALPHA))
        restarted.verify_persisted_sessions(self.state())


class RefusalTests(StartCase):
    """Every unsafe shape refuses before anything is sent, launched, renamed or rebound, and leaves state, registry and owners alone."""

    def assert_refused(self, selection: dict, pattern: str) -> None:
        before_effects, before_state, before_registry = self.effects(), self.paths.state_file.read_bytes() if self.paths.state_file.exists() else None, hpair.registry_path(self.paths).read_bytes()
        with self.assertRaisesRegex(hs.SupervisorError, pattern):
            self.start(selection)
        self.assertEqual(self.effects(), before_effects)
        self.assertEqual(self.paths.state_file.read_bytes() if self.paths.state_file.exists() else None, before_state)
        self.assertEqual(hpair.registry_path(self.paths).read_bytes(), before_registry)
        self.assertTrue(self.owners_unchanged())

    def test_each_identity_failure_refuses_without_substitution(self) -> None:
        cases = {
            "busy": (lambda h: h.agents["alpha-claude"].update(agent_status="working"), "not settled"),
            "blocked": (lambda h: h.agents["alpha-codex"].update(agent_status="blocked"), "not settled"),
            "unknown": (lambda h: h.agents["alpha-codex"].update(agent_status="unknown"), "not settled"),
            "missing": (lambda h: h.agents.pop("alpha-claude"), "not live"),
            "moved": (lambda h: h.agents["alpha-codex"].update(pane_id="w9:p9"), "moved"),
            "duplicate": (lambda h: h.agents.update({"dup": FakeHerdr.agent("codex", "dup", "w7:p7", ALPHA["codex"][1])}), "more than one pane"),
            "provider conflict": (lambda h: h.agents["alpha-claude"].update(agent="codex"), "another provider"),
            "no native identity": (lambda h: h.agents["alpha-codex"].pop("agent_session"), "not live"),
        }
        for label, (mutate, pattern) in cases.items():
            with self.subTest(label):
                saved = json.loads(json.dumps(self.herdr.agents))
                mutate(self.herdr)
                self.assert_refused(self.saved(self.alpha), pattern)
                self.herdr.agents = saved

    def test_a_missing_member_is_never_replaced_by_the_legacy_agent_with_the_configured_alias(self) -> None:
        self.herdr.agents.pop("alpha-codex")
        self.assert_refused(self.saved(self.alpha), "not live")
        self.assertEqual(self.herdr.prompts, [])

    def test_a_changed_registry_entry_invalidates_a_selection_made_earlier(self) -> None:
        selection = self.saved(self.alpha)
        doc = self.registry_doc()
        doc["entries"][self.alpha["pair_id"]]["context"]["repository"] = str(self.repo_b)
        hpair.registry_path(self.paths).write_text(json.dumps(doc))
        self.assert_refused(selection, "changed after the Start card was shown")

    def test_a_missing_entry_and_disabled_feature_refuse(self) -> None:
        selection = self.saved(self.alpha)
        doc = self.registry_doc()
        del doc["entries"][self.alpha["pair_id"]]
        hpair.registry_path(self.paths).write_text(json.dumps(doc))
        self.assert_refused(selection, "no longer exists")
        self.config["session_pairs"]["enabled"] = False
        self.assert_refused(self.saved(self.beta), "not enabled")

    def test_the_legacy_selection_is_unaffected_by_an_unreadable_registry(self) -> None:
        hpair.registry_path(self.paths).write_text("{")
        legacy = {"policy": "preserve", "profiles": {"codex": "default", "claude": "default"}}
        self.herdr.responses = self.plan_response(legacy)
        self.assertTrue(self.start(legacy)["ok"])


class SwitchingTests(StartCase):
    def test_every_nonterminal_or_unsettled_state_refuses_switching_and_changes_nothing(self) -> None:
        self.begin(self.saved(self.alpha))
        for label, mutate in (("plan approval wait", lambda s: None), ("wait user", lambda s: s.update(supervisor_state="WAIT_USER")), ("paused", lambda s: s.update(supervisor_state="PAUSED")),
                              ("quota wait", lambda s: s.update(supervisor_state="WAIT_QUOTA")), ("error", lambda s: s.update(supervisor_state="ERROR"))):
            with self.subTest(label):
                state = self.state()
                mutate(state)
                self.sup.store.write_state(state)
                before = (self.paths.state_file.read_bytes(), self.effects(), hpair.registry_path(self.paths).read_bytes())
                with self.assertRaisesRegex(hs.SupervisorError, "still|cannot|supervisor has no task queue"):
                    self.start(self.saved(self.beta))
                self.assertEqual((self.paths.state_file.read_bytes(), self.effects(), hpair.registry_path(self.paths).read_bytes()), before)
        self.assertEqual(self.state()["pending_gate"]["status"], "pending", "the pending decision and history are unchanged")

    def test_a_terminal_run_with_anything_unresolved_still_refuses(self) -> None:
        self.begin(self.saved(self.alpha))
        self.finish()
        base = self.state()
        unresolved = {
            "unresolved delivery": lambda s: s.update(delivery={"turn_id": str(uuid.uuid4()), "status": "uncertain", "agent": "codex"}),
            "interrupted delivery": lambda s: s.update(interrupted_delivery={"turn_id": str(uuid.uuid4())}),
            "recovery": lambda s: s.update(owner_recovery={"provider": "codex"}),
            "continuation": lambda s: s.update(continuation={"kind": "x"}),
            "reset in flight": lambda s: s["codex_reset"].update(current_redemption_state="RESET_CONSUMING"),
            "follow-up": lambda s: s.update(agent_followup={"status": "WAITING"}),
        }
        for label, mutate in unresolved.items():
            with self.subTest(label):
                self.assertIsNone(hpair.terminal_boundary_problem(base))
                state = json.loads(json.dumps(base))
                mutate(state)
                self.assertIsNotNone(hpair.terminal_boundary_problem(state), label)
        alive = json.loads(json.dumps(base))
        alive["worker_pid"] = os.getppid()
        self.assertEqual(hpair.terminal_boundary_problem(alive), "the previous task's worker is still running")

    def test_cancellation_alone_is_not_enough_while_a_native_turn_still_works(self) -> None:
        self.begin(self.saved(self.alpha))
        self.finish()
        self.herdr.agents["beta-codex"]["agent_status"] = "working"
        effects = self.effects()
        with self.assertRaisesRegex(hs.SupervisorError, "not settled"):
            self.start(self.saved(self.beta))
        self.assertEqual(self.effects(), effects)
        self.herdr.agents["beta-codex"]["agent_status"] = "idle"
        self.assertTrue(self.start(self.saved(self.beta))["ok"], "positive outcome once genuinely settled")

    def test_replayed_controls_of_the_finished_run_never_reach_the_next_pair(self) -> None:
        first = self.begin(self.saved(self.alpha))
        gate_id = self.state()["pending_gate"]["gate_id"]
        self.finish()
        self.begin(self.saved(self.beta))
        prompts_before = list(self.herdr.prompts)
        gate_before = json.dumps(self.state()["pending_gate"], sort_keys=True)
        for action, extra in (("approve", {"gate_id": gate_id}), ("cancel", {}), ("pause", {}), ("resume", {})):
            with self.subTest(action):
                state = self.state()
                with self.assertRaisesRegex(hs.SupervisorError, "earlier run|issued for run|ignored|no longer|belongs|does not|is not"):
                    self.sup.apply_command(state, {"request_id": uuid.uuid4().hex[:32], "action": action, "run_id": first, "actor": "cli", **extra})
        self.assertEqual(self.herdr.prompts, prompts_before)
        self.assertEqual(json.dumps(self.state()["pending_gate"], sort_keys=True), gate_before)
        self.assertEqual(self.state()["supervisor_state"], "WAIT_PLAN_APPROVAL")


class FirstDeliveryTests(StartCase):
    def test_a_member_that_becomes_busy_after_start_blocks_the_first_delivery(self) -> None:
        self.assertTrue(self.start(self.saved(self.alpha))["ok"])
        self.herdr.agents["alpha-claude"]["agent_status"] = "working"
        self.herdr.responses = self.plan_response()
        self.sup.resume()
        self.assertEqual(self.herdr.prompts, [], "nothing was sent to a half-settled pair")
        state = self.state()
        self.assertEqual(state["supervisor_state"], "WAIT_USER")
        self.assertIn("not exactly present and settled", state["wait_user_reason"])
        self.herdr.agents["alpha-claude"]["agent_status"] = "idle"
        self.herdr.responses = self.plan_response()
        self.sup.apply_command(self.state(), {"request_id": uuid.uuid4().hex[:32], "action": "resume", "run_id": state["run_id"], "actor": "cli"})
        self.sup.resume()
        self.assertEqual([name for name, _ in self.herdr.prompts], ["alpha-codex"])

    def test_a_missing_member_mid_run_is_never_restarted_or_replaced(self) -> None:
        self.begin(self.saved(self.alpha))
        self.herdr.agents.pop("alpha-codex")
        self.herdr.responses = []
        starts = list(self.herdr.starts)
        with self.assertRaises(hs.SupervisorError):
            self.sup.recover_agent("codex")
        self.assertEqual(self.herdr.starts, starts, "no agent was launched, renamed or substituted for a saved pair member")
        self.assertEqual(self.herdr.agent_renames, [])


class SaveExistingTests(StartCase):
    def test_two_explicitly_chosen_existing_sessions_are_saved_once_and_run(self) -> None:
        gamma = {"claude": ("w6:p1", "cccccccc-1111-4111-8111-000000000001"), "codex": ("w6:p2", "cccccccc-1111-4111-8111-000000000002")}
        for provider, (pane, session) in gamma.items():
            self.herdr.agents[f"gamma-{provider}"] = FakeHerdr.agent(provider, f"gamma-{provider}", pane, session)
        run_id = str(uuid.uuid4())
        before = self.effects()
        self.begin(self.existing("Gamma", gamma, self.repo_b, self.review_b), run_id)
        entries = {e["name"]: e for e in hpair.list_entries(self.paths)}
        self.assertEqual(entries["Gamma"]["identities"], ids(gamma))
        self.assertEqual(entries["Gamma"]["source"], "existing")
        self.assertEqual(self.state()["session_pair"]["pair_id"], entries["Gamma"]["pair_id"])
        self.assertEqual(self.prompt_targets(), {"gamma-codex"})
        self.assertEqual(self.effects()["starts"], before["starts"])
        # replay of the same start (crash after the registry commit) registers the same entry and creates no second one
        self.finish()
        again = hpair.register_existing(self.paths, self.config, self.clock.time, name="Gamma", context=self.ctx(self.repo_b, self.review_b), identities=ids(gamma), pair_id=entries["Gamma"]["pair_id"])
        self.assertEqual(again, entries["Gamma"])
        self.assertEqual(len(hpair.list_entries(self.paths)), 3)

    def test_collisions_and_foreign_sessions_refuse_before_anything_is_registered(self) -> None:
        free = {"claude": ("w6:p1", "dddddddd-1111-4111-8111-000000000001"), "codex": ("w6:p2", "dddddddd-1111-4111-8111-000000000002")}
        for provider, (pane, session) in free.items():
            self.herdr.agents[f"free-{provider}"] = FakeHerdr.agent(provider, f"free-{provider}", pane, session)
        revision = self.registry_doc()["revision"]
        with self.assertRaisesRegex(hs.SupervisorError, "already uses that name"):
            self.start(self.existing("alpha", free))
        with self.assertRaisesRegex(hs.SupervisorError, "already used by a saved pair"):
            self.start(self.existing("Delta", ALPHA))
        hs.atomic_write_json(self.paths.query_registry_file, {"schema_version": 1, "providers": {"codex": {"agent_name": "codex-query", "pane_id": "w6:p2", "session_id": free["codex"][1]}}})
        with self.assertRaisesRegex(hs.SupervisorError, "already used by a saved pair"):
            self.start(self.existing("Delta", free))
        context_outside = self.existing("Delta", free)
        context_outside["pair"]["context"]["repository"] = str(self.project_root.parent)
        with self.assertRaisesRegex(hs.SupervisorError, "outside the administrator-allowed roots"):
            self.start(context_outside)
        self.assertEqual(self.registry_doc()["revision"], revision)
        self.assertEqual(self.effects()["prompts"], [])

    def test_the_legacy_owner_pair_can_be_saved_as_a_named_pair(self) -> None:
        run_id = self.begin(self.existing("Current", {"claude": ("w3:p1", CLAUDE_SESSION), "codex": ("w3:p2", CODEX_SESSION)}))
        self.assertEqual(self.prompt_targets(), {"codex-main"})
        self.assertEqual(self.state()["session_pair"]["name"], "Current")
        self.assertTrue(self.owners_unchanged())
        self.assertEqual(self.state()["run_id"], run_id)


class ContentionTests(StartCase):
    def test_simultaneous_starts_on_different_pairs_admit_exactly_one_run(self) -> None:
        results: list = []
        barrier = threading.Barrier(2)

        def attempt(selection: dict) -> None:
            barrier.wait()
            try:
                with self.sup.store.transaction():
                    results.append(self.sup.apply_command(self.sup.store.read_state(required=False), self.command(selection)))
            except hs.SupervisorError as error:
                results.append(error)

        threads = [threading.Thread(target=attempt, args=(self.saved(pair),)) for pair in (self.alpha, self.beta)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual(len([r for r in results if isinstance(r, dict)]), 1, results)
        self.assertEqual(self.state()["session_pair"]["pair_id"] in (self.alpha["pair_id"], self.beta["pair_id"]), True)
        self.assertEqual(self.herdr.prompts, [])


class StatusAndProtectionTests(StartCase):
    def test_status_and_query_registration_know_the_pair(self) -> None:
        self.begin(self.saved(self.alpha))
        status = self.sup.status()
        self.assertEqual(status["session_pair"]["name"], "Alpha")
        self.assertEqual(status["session_pair"]["context"]["repository"], str(self.repo_a))
        import herdr_present

        rendered = herdr_present.render_status(status, "UTC")
        self.assertIn("Pair: Alpha", rendered.html)
        self.assertIn(ALPHA["claude"][1][:8], rendered.html)
        self.assertIn(str(self.repo_a), rendered.html)
        legacy = {**status, "session_pair": None}
        self.assertNotIn("Pair:", herdr_present.render_status(legacy, "UTC").html)
        self.assertEqual(self.sup.owners(), ids(ALPHA))
        for session in (ALPHA["claude"][1], BETA["codex"][1]):
            self.assertIn(session, hpair.known_sessions(self.paths))
        query = FakeHerdr.agent("codex", "codex-query", "w8:p1", BETA["codex"][1])
        self.herdr.agents["codex-query"] = query
        self.config["query"]["providers"] = {"codex": {"agent_name": "codex-query", "read_only_contract": "fixture contract"}}
        with self.assertRaisesRegex(hs.SupervisorError, "reuses a workflow native session"):
            hs.register_query_provider(self.paths, self.config, self.herdr, provider="codex", agent_name="codex-query", acknowledge_read_only_contract=True)


class OwnerRecoveryTests(StartCase):
    """A moved pane inside a running named pair is repaired in THAT run only: same native session, new locator; owners.json and the saved entry stay."""

    def to_approved(self) -> None:
        self.begin(self.saved(self.alpha))
        self.approve_pending()

    def worker(self) -> int:
        self.herdr.responses = []
        code = self.make_supervisor().worker()
        raw = json.loads(self.paths.state_file.read_text())
        if raw.get("worker_pid") is not None:
            raw["worker_pid"] = None
            self.paths.state_file.write_text(json.dumps(raw))
        return code

    def test_a_moved_member_is_repaired_in_the_run_snapshot_only_and_the_same_session_continues(self) -> None:
        self.to_approved()
        self.herdr.agents["alpha-codex"]["pane_id"] = "w7:p7"
        self.herdr.available_panes.add("w7:p7")
        self.assertEqual(self.worker(), 2)
        state = self.state()
        recovery = state["owner_recovery"]
        self.assertEqual((recovery["classification"], recovery["provider"], recovery["recorded_pane"]), ("moved", "codex", "w4:p2"))
        self.assertEqual(self.herdr.prompts[-1][0], "alpha-codex", "nothing was sent to any other session while it waited")
        sent = len(self.herdr.prompts)
        registry_before = hpair.registry_path(self.paths).read_bytes()
        with self.sup.store.transaction():
            live = self.sup.store.read_state()
            result = self.sup.owner_recovery_apply(live, run_id=state["run_id"], recovery_id=recovery["recovery_id"], pane="w7:p7", actor="operator-cli")
        self.assertTrue(result["ok"], result)
        repaired = self.state()
        self.assertEqual(repaired["session_pair"]["identities"]["codex"], {"pane_id": "w7:p7", "session_id": ALPHA["codex"][1]})
        self.assertEqual(repaired["session_pair"]["identities"]["claude"], ids(ALPHA)["claude"])
        self.assertEqual(repaired["session_pair"]["fingerprint"], hpair.entry_fingerprint(self.alpha), "the bound entry fingerprint is the one the run started from")
        self.assertEqual(hpair.validate_binding(repaired["session_pair"]), repaired["session_pair"])
        self.assertEqual(self.sup.owners()["codex"], {"pane_id": "w7:p7", "session_id": ALPHA["codex"][1]})
        self.assertTrue(self.owners_unchanged(), "the global owners.json is never edited by a named run's repair")
        self.assertEqual(hpair.registry_path(self.paths).read_bytes(), registry_before, "the saved pair entry is immutable")
        self.assertEqual(len(self.herdr.prompts), sent)
        self.assertEqual(self.herdr.starts, [])


class InitializeReprovesTests(StartCase):
    """`initialize` is the last gate before a run exists: it re-proves the terminal boundary and the exact, settled pair itself, whatever the caller checked."""

    def initialize(self, selection: dict, binding: dict) -> dict:
        with self.sup.store.transaction():
            return self.sup.initialize("task", "codex", workflow_policy="gated_v2", run_id=str(uuid.uuid4()), session_selection=selection, session_pair=binding)

    def binding(self, entry: dict) -> dict:
        return hpair.binding_from_entry(entry, bound_at="2026-01-01T00:00:00Z")

    def test_a_pair_that_became_busy_after_the_caller_checked_never_gets_a_run(self) -> None:
        self.herdr.agents["alpha-claude"]["agent_status"] = "working"
        with self.assertRaisesRegex(hs.SupervisorError, "not settled"):
            self.initialize(self.saved(self.alpha), self.binding(self.alpha))
        self.assertFalse(self.paths.state_file.exists())
        self.assertEqual(self.effects()["prompts"], [])

    def test_a_nonterminal_current_run_refuses_before_the_pair_is_even_looked_at(self) -> None:
        self.begin(self.saved(self.alpha))
        state_bytes = self.paths.state_file.read_bytes()
        with self.assertRaisesRegex(hs.SupervisorError, "still"):
            self.initialize(self.saved(self.beta), self.binding(self.beta))
        self.assertEqual(self.paths.state_file.read_bytes(), state_bytes)

    def test_a_binding_without_a_matching_pair_selection_or_a_selection_without_a_binding_is_refused(self) -> None:
        legacy = {"policy": "preserve", "profiles": {"codex": "default", "claude": "default"}}
        with self.assertRaisesRegex(hs.SupervisorError, "requires a pair selection"):
            self.initialize(legacy, self.binding(self.alpha))
        with self.assertRaisesRegex(hs.SupervisorError, "supervisor task flow"):
            with self.sup.store.transaction():
                self.sup.initialize("task", "codex", workflow_policy="gated_v2", session_selection=self.saved(self.alpha))
        self.assertFalse(self.paths.state_file.exists())

    def test_a_valid_binding_starts_exactly_one_run_bound_to_the_pair(self) -> None:
        state = self.initialize(self.saved(self.alpha), self.binding(self.alpha))
        self.assertEqual(state["session_pair"]["pair_id"], self.alpha["pair_id"])
        self.assertEqual(self.state()["native_sessions"], {"codex": ALPHA["codex"][1], "claude": ALPHA["claude"][1]})
        self.assertTrue(self.owners_unchanged())


class QueryFallbackTests(StartCase):
    def test_the_claude_quota_fallback_names_the_running_pairs_member_not_the_global_owner(self) -> None:
        import herdr_query

        worker = herdr_query.QueryWorker(self.paths, self.config, self.herdr, clock=self.clock.time)
        self.assertEqual(worker._workflow_session_for("claude"), self.owners["claude"]["session_id"], "no run: the installation's owner")
        self.begin(self.saved(self.alpha))
        self.assertEqual(worker._workflow_session_for("claude"), ALPHA["claude"][1])
        self.assertEqual(worker._workflow_session_for("codex"), ALPHA["codex"][1])
        self.finish()
        self.assertEqual(worker._workflow_session_for("claude"), self.owners["claude"]["session_id"], "a terminal named run no longer speaks for the workflow")


class HelperIsolationTests(StartCase):
    def test_helper_workers_and_saved_pairs_cannot_share_a_session_in_either_direction(self) -> None:
        helper = {"claude": ("w6:p1", "ffffffff-0000-4000-8000-000000000001"), "codex": ("w6:p2", "ffffffff-0000-4000-8000-000000000002")}
        for provider, (pane, session) in helper.items():
            self.herdr.agents[f"free-{provider}"] = FakeHerdr.agent(provider, f"free-{provider}", pane, session)
        hs.atomic_write_json(self.state_dir / "helpers" / "registry.json", {"schema_version": 1, "workers": {"worker": {"provider": "codex", "session_id": helper["codex"][1], "pane_id": "w6:p2", "model": "m", "cwd": "/x", "access": "read-only"}}})
        self.assertIn(helper["codex"][1], hpair.foreign_sessions(self.paths))
        with self.assertRaisesRegex(hs.SupervisorError, "already used by a saved pair"):
            self.start(self.existing("Gamma", helper))
        # the other direction: a saved pair's session is a protected workflow session for the helper manager
        import herdr_helpers

        manager = herdr_helpers.HelperManager(self.sup)
        binding = {"provider": "codex", "session_id": ALPHA["codex"][1], "pane_id": "w4:p2", "model": "m", "cwd": str(self.project_root), "access": "read-only"}
        with self.assertRaisesRegex(hs.SupervisorError, "workflow/query owners cannot be helper workers"):
            manager._verify(binding, ready=False)


class FrozenContextTests(StartCase):
    """The pair's repository and review base replace product_repo/review_root for that run only: the same frozen context is used by the prompt, gate
    validation, candidate head checks and follow-up destinations, in preview and in execution; later configuration cannot change it."""

    def setUp(self) -> None:
        super().setUp()
        self.review_dir_a = self.review_a / "feature-alpha"
        self.review_dir_a.mkdir()
        (self.review_dir_a / "CODEX_PLAN.md").write_text("# plan\n")
        self.heads: list = []
        self.sup.head_resolver = lambda repo: (self.heads.append(str(repo)), self.head)[1]

    def plan_payload_in(self, review_dir=None, **overrides):
        review_dir = review_dir or self.review_dir_a
        payload = {"schema_version": 1, "gate_type": "plan_approval", "task_title": "Add widget", "summary": "Plan", "review_directory": str(review_dir), "scope": "backend",
                   "intended_changes": ["x"], "risk_summary": "low", "affected_components": ["backend"], "migration_required": False, "migration_explanation": "",
                   "runtime_validation_required": True, "rebuild_required": True, "rebuild_reason": "image", "push_approval_required": True, "plan_path": str(review_dir / "CODEX_PLAN.md"), **overrides}
        path = review_dir / "plan_payload.json"
        path.write_text(json.dumps(payload))
        return path

    def run_to_gate(self, selection: dict, review_dir=None):
        self.assertTrue(self.start(selection)["ok"])
        self.herdr.responses = [{"v2": ("plan", "human", "plan_approval", str(self.plan_payload_in(review_dir)))}]
        self.sup.resume()

    def test_a_plan_below_the_pairs_review_base_is_accepted_and_the_prompt_names_that_base(self) -> None:
        self.run_to_gate(self.saved(self.alpha))
        state = self.state()
        self.assertEqual(state["pending_gate"]["gate_type"], "plan_approval")
        self.assertEqual(state["pending_gate"]["payload"]["review_directory"], str(self.review_dir_a))
        prompt = self.herdr.prompts[0][1]
        self.assertIn(f"JSON path below {self.review_a}", prompt)
        self.assertNotIn(str(self.review_root), prompt, "the installation's default review root is not offered to a pair's agent")

    def test_a_plan_outside_the_pairs_review_base_is_refused_even_if_it_is_inside_the_global_review_root(self) -> None:
        selection = self.saved(self.alpha)
        self.assertTrue(self.start(selection)["ok"])
        # the first reply's review_dir is below the GLOBAL review_root, not below Alpha's base; the supervisor refuses it and the agent must repair
        self.herdr.responses = [{"v2": ("plan", "human", "plan_approval", str(self.plan_payload()))}, *self.plan_for(selection)]
        self.sup.resume()
        self.assertEqual(len(self.herdr.prompts), 2)
        repair = self.herdr.prompts[1][1]
        self.assertIn("rejected your approval request", repair)
        self.assertIn("review", repair.lower())
        state = self.state()
        self.assertEqual(state["pending_gate"]["payload"]["review_directory"], str(self.review_a / "feature-widget"), "only the payload inside the frozen base became a gate")
        self.assertEqual(state["supervisor_state"], "WAIT_PLAN_APPROVAL")

    def test_a_legacy_run_keeps_using_the_configured_review_root_and_repository(self) -> None:
        self.assertTrue(self.start({"policy": "preserve", "profiles": {"codex": "default", "claude": "default"}})["ok"])
        self.herdr.responses = [{"v2": ("plan", "human", "plan_approval", str(self.plan_payload()))}]
        self.sup.resume()
        self.assertEqual(self.state()["pending_gate"]["gate_type"], "plan_approval")
        self.assertIn(f"JSON path below {self.review_root}", self.herdr.prompts[0][1])

    def test_candidate_and_head_checks_use_the_pairs_repository_and_ignore_later_configuration_changes(self) -> None:
        self.run_to_gate(self.saved(self.alpha))
        self.approve_pending()
        self.config["product_repo"] = str(self.repo_b)
        self.config["review_root"] = str(self.review_b)
        self.assertEqual(self.sup.context_config(self.state())["product_repo"], str(self.repo_a))
        self.assertEqual(self.sup.context_config()["review_root"], str(self.review_a), "without a state the active run's frozen context answers")
        view = self.sup.binding_view(self.state())
        self.assertEqual((view["product_repo"], view["review_root"]), (str(self.repo_a), str(self.review_a)))
        self.assertTrue(all(h == str(self.repo_a) for h in self.heads), self.heads)

    def test_a_finished_run_still_answers_with_its_own_context_and_the_next_run_with_its_own(self) -> None:
        self.run_to_gate(self.saved(self.alpha))
        finished = self.state()
        self.finish()
        self.assertEqual(self.sup.context_config(self.state())["review_root"], str(self.review_a), "a terminal named run keeps its context for reports and history")
        self.assertEqual(self.sup.context_config()["review_root"], str(self.review_root), "with no active named run the installation's configured context applies")
        self.assertIs(self.sup.context_config(), self.config, "legacy callers get the very same configuration object")
        self.assertEqual(finished["session_pair"]["context"]["review_base"], str(self.review_a))

    def test_follow_up_destinations_are_proved_against_the_frozen_review_base(self) -> None:
        self.run_to_gate(self.saved(self.alpha))
        state = self.state()
        directory = self.sup.followup_review_directory(state)
        self.assertEqual(directory, self.review_dir_a)
        inside, outside = str(self.review_dir_a / "AGENT_FOLLOWUP_x.md"), str(self.review_dir / "AGENT_FOLLOWUP_x.md")
        self.assertIsNone(self.sup._followup_destination_problem(inside, state=state))
        self.assertIn("outside the review root", self.sup._followup_destination_problem(outside, state=state))

    def test_telegram_may_attach_documents_from_a_saved_pairs_review_base_only_when_pairs_are_enabled(self) -> None:
        import herdr_artifacts as ha

        document = self.review_dir_a / "CODEX_PLAN.md"
        self.assertIn(self.review_a, ha.allowed_roots(self.paths, self.config))
        self.assertEqual(ha.check_source(str(document), paths=self.paths, config=self.config), document)
        self.config["session_pairs"]["enabled"] = False
        self.assertNotIn(self.review_a, ha.allowed_roots(self.paths, self.config))
        with self.assertRaisesRegex(hs.SupervisorError, "outside the allowlisted roots"):
            ha.check_source(str(document), paths=self.paths, config=self.config)


if __name__ == "__main__":
    import unittest

    unittest.main()
