"""Correction round (CI1-CI6): the complete old-run / old-pair boundary before any effect, replay of a committed save-existing registration, the selected
context in real prompts and frozen instruction root, current context authorization for saved pairs, fail-closed query/helper isolation, and pending-start
writers that cannot overwrite a committed Start receipt. Fake Herdr/Telegram, temporary state, injected clock."""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path
from unittest import mock

import herdr_pairs as hpair
from pair_fixtures import ALPHA, BETA, hs
from test_named_pairs_start import StartCase
from test_named_pairs_telegram import TgPairCase
from v2_fixtures import FakeHerdr

OWN = {"claude": ("w6:p1", "dddddddd-2222-4222-8222-000000000001"), "codex": ("w6:p2", "dddddddd-2222-4222-8222-000000000002")}


class Boundary(StartCase):
    def add_free_pair(self, label: str = "free", spec: dict | None = None) -> dict:
        spec = spec or OWN
        for provider, (pane, session) in spec.items():
            self.herdr.agents[f"{label}-{provider}"] = FakeHerdr.agent(provider, f"{label}-{provider}", pane, session)
        return spec

    def finished_alpha(self) -> dict:
        self.begin(self.saved(self.alpha))
        self.finish()
        return self.state()

    def snapshot(self) -> dict:
        reg = hpair.registry_path(self.paths)
        return {"effects": self.effects(), "state": self.paths.state_file.read_bytes() if self.paths.state_file.exists() else None, "registry": reg.read_bytes() if reg.exists() else None, "owners": self.owners_unchanged()}

    def assert_refused_without_effect(self, selection: dict, pattern: str) -> None:
        before = self.snapshot()
        with self.assertRaisesRegex(hs.SupervisorError, pattern):
            self.start(selection)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.registry_doc()["reservations"], {})


class CompleteBoundaryTests(Boundary):
    UNRESOLVED = {
        "uncertain delivery": lambda s: s.update(delivery={"turn_id": str(uuid.uuid4()), "status": "uncertain", "agent": "codex"}),
        "interrupted delivery": lambda s: s.update(interrupted_delivery={"turn_id": str(uuid.uuid4())}),
        "continuation": lambda s: s.update(continuation={"kind": "x"}),
    }
    SHAPED = {  # states whose persisted shape is validated elsewhere: proved on the in-memory state through the SAME predicate every entry uses
        "owner recovery": lambda s: s.update(owner_recovery={"provider": "codex"}),
        "follow-up": lambda s: s.update(agent_followup={"status": "WAITING"}),
        "reset in flight": lambda s: s["codex_reset"].update(current_redemption_state="RESET_CONSUMING"),
    }

    def test_recovery_follow_up_and_reset_states_are_part_of_the_same_boundary_proof(self) -> None:
        import herdr_workflow

        base = self.finished_alpha()
        live = self.herdr.list_agents()
        self.assertIsNone(herdr_workflow.pair_switch_problem(self.paths, base, live))
        for label, mutate in self.SHAPED.items():
            with self.subTest(label):
                state = json.loads(json.dumps(base))
                mutate(state)
                self.assertIn("select the pair again", herdr_workflow.pair_switch_problem(self.paths, state, live))

    def test_every_pair_mode_refuses_an_unresolved_terminal_boundary_before_any_effect(self) -> None:
        self.finished_alpha()
        free = self.add_free_pair()
        for label, mutate in self.UNRESOLVED.items():
            state = self.state()
            mutate(state)
            self.paths.state_file.write_text(json.dumps(state))
            for mode, selection in (("saved", self.saved(self.beta)), ("save_existing", self.existing("Gamma", free)), ("create", self.creation("Fresh Team"))):
                with self.subTest(label=label, mode=mode):
                    self.assert_refused_without_effect(selection, "cannot switch pairs")
        self.assertEqual((len(self.herdr.splits), len(self.herdr.pane_runs)), (0, 0), "no pane was created for any unsafe boundary")

    def test_the_independent_case_an_uncertain_delivery_never_creates_panes_or_publishes_a_pair(self) -> None:
        legacy = {"policy": "preserve", "profiles": {"codex": "default", "claude": "default"}}
        with self.sup.store.transaction():
            state = self.sup.initialize("old", "codex", session_selection=legacy)
        state.update(supervisor_state="CANCELLED", worker_pid=None, delivery={"status": "uncertain", "agent": "codex", "turn_id": str(uuid.uuid4())})
        self.sup.store.write_state(state)
        before = self.effects()
        with self.assertRaisesRegex(hs.SupervisorError, "unresolved"):
            self.start(self.creation("Fresh unsafe"))
        self.assertEqual((self.effects(), len(hpair.list_entries(self.paths)) - 2), (before, 0))
        self.assertEqual(self.registry_doc()["reservations"], {})

    def test_an_open_startup_wait_or_another_unresolved_preparation_refuses_every_pair_mode(self) -> None:
        self.finished_alpha()
        free = self.add_free_pair()
        stray = str(uuid.uuid4())
        hs.atomic_write_json(self.paths.session_preparations_dir / f"{stray}.json", {"preparation_id": stray, "status": "BINDING"})
        for mode, selection in (("saved", self.saved(self.beta)), ("save_existing", self.existing("Gamma", free)), ("create", self.creation("Fresh Team"))):
            with self.subTest(mode=mode):
                self.assert_refused_without_effect(selection, "session preparation .* is unresolved")
        (self.paths.session_preparations_dir / f"{stray}.json").unlink()
        run_id = self.state()["run_id"]
        with mock.patch("herdr_workflow.hst.list_waits", lambda paths, **kw: [{"wait_id": "w"}] if kw.get("run_id") == run_id else []):
            self.assert_refused_without_effect(self.saved(self.beta), "open startup wait")

    def test_a_previous_pair_that_still_works_blocks_every_pair_mode_and_nothing_is_interrupted(self) -> None:
        self.finished_alpha()
        free = self.add_free_pair()
        self.herdr.agents["alpha-codex"]["agent_status"] = "working"
        for mode, selection in (("saved", self.saved(self.beta)), ("save_existing", self.existing("Gamma", free)), ("create", self.creation("Fresh Team"))):
            with self.subTest(mode=mode):
                self.assert_refused_without_effect(selection, "still 'working'")
        with self.assertRaisesRegex(hs.SupervisorError, "wait for its turn to finish"):
            self.start(self.saved(self.beta))
        self.assertEqual(self.herdr.sent_keys, [], "no interrupt was sent to the old session")
        self.herdr.agents["alpha-codex"]["agent_status"] = "idle"
        self.assertTrue(self.start(self.saved(self.beta))["ok"], "positive outcome once the old boundary genuinely settled")

    def test_a_previous_session_that_is_blocked_unknown_or_duplicated_also_blocks(self) -> None:
        self.finished_alpha()
        for label, mutate, pattern in (("blocked", lambda: self.herdr.agents["alpha-claude"].update(agent_status="blocked"), "still 'blocked'"),
                                       ("unknown", lambda: self.herdr.agents["alpha-claude"].update(agent_status="unknown"), "still 'unknown'"),
                                       ("duplicate", lambda: self.herdr.agents.update({"dup": FakeHerdr.agent("claude", "dup", "w7:p7", ALPHA["claude"][1])}), "not uniquely identifiable")):
            with self.subTest(label):
                saved = json.loads(json.dumps(self.herdr.agents))
                mutate()
                self.assert_refused_without_effect(self.saved(self.beta), pattern)
                self.herdr.agents = saved

    def test_a_legacy_previous_run_whose_session_still_works_blocks_a_pair_start_too(self) -> None:
        legacy = {"policy": "preserve", "profiles": {"codex": "default", "claude": "default"}}
        self.herdr.responses = self.plan_for(legacy)
        self.assertTrue(self.start(legacy)["ok"])
        self.sup.resume()
        self.finish()
        self.herdr.agents["codex-main"]["agent_status"] = "working"
        self.assert_refused_without_effect(self.saved(self.alpha), "codex session .* is still 'working'")

    def test_initialize_re_proves_the_old_boundary_whatever_the_caller_checked(self) -> None:
        self.finished_alpha()
        self.herdr.agents["alpha-codex"]["agent_status"] = "working"
        binding = hpair.binding_from_entry(self.beta, bound_at="2026-01-01T00:00:00Z")
        with self.assertRaisesRegex(hs.SupervisorError, "still 'working'"), self.sup.store.transaction():
            self.sup.initialize("task", "codex", workflow_policy="gated_v2", run_id=str(uuid.uuid4()), session_selection=self.saved(self.beta), session_pair=binding)

    def test_prepare_sessions_itself_refuses_a_creation_at_an_unsafe_boundary_before_reserving(self) -> None:
        self.finished_alpha()
        self.herdr.agents["alpha-codex"]["agent_status"] = "working"
        with self.assertRaisesRegex(hs.SupervisorError, "cannot create a named pair"):
            self.sup.prepare_task_sessions(self.creation("Fresh Team"), preparation_id=str(uuid.uuid4()))
        self.assertEqual((self.herdr.splits, self.herdr.pane_runs), ([], []))
        self.assertEqual(self.registry_doc()["reservations"], {})


class OmittedIdentityTests(Boundary):
    """CI1-R1: a previous session whose native identity is OMITTED while its recorded pane still reports an agent is uncertainty about an old native turn, never a
    settled or closed session. Nothing is adopted, relabelled or interrupted; a restored identity / settlement or a positively closed pane is the way forward."""

    def omit(self, name: str, status: str = "working") -> dict:
        agent = self.herdr.agents[name]
        agent["agent_status"] = status
        agent.pop("agent_session", None)
        return agent

    def selections(self):
        free = self.add_free_pair()
        return (("saved", self.saved(self.beta)), ("save_existing", self.existing("Gamma", free)), ("create", self.creation("Fresh Team")))

    def test_every_pair_mode_refuses_while_the_old_pane_reports_an_agent_without_identity(self) -> None:
        self.finished_alpha()
        for status in ("working", "blocked", "unknown", "idle"):
            for name in ("alpha-codex", "alpha-claude"):
                saved = json.loads(json.dumps(self.herdr.agents))
                self.omit(name, status)
                for mode, selection in self.selections():
                    with self.subTest(status=status, agent=name, mode=mode):
                        self.assert_refused_without_effect(selection, "no native identity")
                self.herdr.agents = saved
        self.assertEqual((self.herdr.splits, self.herdr.pane_runs, self.herdr.sent_keys, self.herdr.agent_renames), ([], [], [], []), "nothing was created, interrupted, adopted or relabelled")

    def test_the_reviewers_independent_case_and_the_ways_forward(self) -> None:
        self.finished_alpha()
        agent = self.omit("alpha-codex")
        self.assert_refused_without_effect(self.saved(self.beta), "no native identity")
        agent["agent_session"] = {"value": ALPHA["codex"][1]}  # identity restored but still working: the ordinary exact-identity rule blocks
        self.assert_refused_without_effect(self.saved(self.beta), "still 'working'")
        agent["agent_status"] = "idle"
        self.assertTrue(self.start(self.saved(self.beta))["ok"], "positive outcome once the identity is restored and the turn settled")

    def test_a_positively_closed_pane_or_a_different_settled_session_does_not_block(self) -> None:
        self.finished_alpha()
        self.herdr.agents.pop("alpha-codex")  # pane gone: positively absent
        self.herdr.agents.pop("alpha-claude")
        self.assertTrue(self.start(self.saved(self.beta))["ok"])

    def test_a_different_session_now_in_the_old_pane_blocks_only_while_it_is_not_settled(self) -> None:
        self.finished_alpha()
        agent = self.herdr.agents["alpha-codex"]
        agent["agent_session"] = {"value": "cccccccc-9999-4999-8999-000000000009"}  # the old session is no longer carried; another one sits in its pane
        agent["agent_status"] = "working"
        self.assert_refused_without_effect(self.saved(self.beta), "now carries session")
        agent["agent_status"] = "idle"
        self.assertTrue(self.start(self.saved(self.beta))["ok"])

    def test_a_legacy_previous_run_records_its_pane_through_owners_json(self) -> None:
        legacy = {"policy": "preserve", "profiles": {"codex": "default", "claude": "default"}}
        self.herdr.responses = self.plan_for(legacy)
        self.assertTrue(self.start(legacy)["ok"])
        self.sup.resume()
        self.finish()
        self.omit("codex-main")
        self.assert_refused_without_effect(self.saved(self.alpha), "no native identity")

    def test_initialize_and_prepare_sessions_re_prove_it_themselves(self) -> None:
        self.finished_alpha()
        self.omit("alpha-codex")
        binding = hpair.binding_from_entry(self.beta, bound_at="2026-01-01T00:00:00Z")
        with self.assertRaisesRegex(hs.SupervisorError, "no native identity"), self.sup.store.transaction():
            self.sup.initialize("task", "codex", workflow_policy="gated_v2", run_id=str(uuid.uuid4()), session_selection=self.saved(self.beta), session_pair=binding)
        with self.assertRaisesRegex(hs.SupervisorError, "cannot create a named pair"):
            self.sup.prepare_task_sessions(self.creation("Fresh Team"), preparation_id=str(uuid.uuid4()))
        self.assertEqual((self.herdr.splits, self.herdr.pane_runs), ([], []))


class LegacyLocatorTests(Boundary):
    """CI1-R2: a legacy previous run's pane is recorded only in the mutable global owners.json. When that locator is unreadable, no longer names the previous native
    session, or is missing, an omitted-identity agent of that kind is uncertainty about the old turn: the switch refuses (the replacement owner is never adopted as the
    old run, nothing is interrupted), while genuinely closed / settled sessions and a missing previous run still start."""

    LEGACY = {"policy": "preserve", "profiles": {"codex": "default", "claude": "default"}}

    def legacy_finished(self) -> None:
        self.herdr.responses = self.plan_for(self.LEGACY)
        self.assertTrue(self.start(self.LEGACY)["ok"])
        self.sup.resume()
        self.finish()

    def omit(self, name: str = "codex-main", status: str = "working") -> dict:
        agent = self.herdr.agents[name]
        agent["agent_status"] = status
        agent.pop("agent_session", None)
        return agent

    FAULTS = {
        "unreadable": lambda c: c.paths.owners_file.write_text("{"),
        "not an object": lambda c: c.paths.owners_file.write_text("[]"),
        "different native id": lambda c: c.paths.owners_file.write_text(json.dumps({**c.owners, "codex": {"pane_id": "w9:p9", "session_id": "cccccccc-1111-4111-8111-111111111111"}})),
        "record removed": lambda c: c.paths.owners_file.write_text(json.dumps({"claude": c.owners["claude"]})),
        "file removed": lambda c: c.paths.owners_file.unlink(),
    }

    def selections(self):
        free = self.add_free_pair()
        return (("saved", self.saved(self.alpha)), ("save_existing", self.existing("Gamma", free)), ("create", self.creation("Fresh Team")))

    def snapshot(self) -> dict:  # the global owner file is the thing under test here (it may be unreadable or absent), so it is not part of the effect snapshot
        reg = hpair.registry_path(self.paths)
        return {"effects": self.effects(), "state": self.paths.state_file.read_bytes() if self.paths.state_file.exists() else None, "registry": reg.read_bytes() if reg.exists() else None}

    def test_an_unavailable_or_nonmatching_locator_with_an_identity_less_old_agent_refuses_every_mode(self) -> None:
        self.legacy_finished()
        agent = self.omit()
        owners = self.paths.owners_file.read_text()
        for fault, apply in self.FAULTS.items():
            apply(self)
            for mode, selection in self.selections():
                with self.subTest(fault=fault, mode=mode):
                    before = self.snapshot()
                    with self.assertRaisesRegex(hs.SupervisorError, "recorded pane cannot be established"):
                        self.start(selection)
                    self.assertEqual(self.snapshot(), before)
                    self.assertEqual(self.registry_doc()["reservations"], {})
            self.paths.owners_file.write_text(owners)
        self.assertEqual((self.herdr.splits, self.herdr.pane_runs, self.herdr.sent_keys, self.herdr.agent_renames), ([], [], [], []))
        self.assertEqual(agent["agent_status"], "working", "the old session was neither adopted nor touched")

    def test_the_replacement_owner_is_never_adopted_as_the_old_run(self) -> None:
        self.legacy_finished()
        self.omit()
        self.FAULTS["different native id"](self)
        replaced = self.paths.owners_file.read_bytes()
        with self.assertRaisesRegex(hs.SupervisorError, "recorded pane cannot be established"):
            self.start(self.saved(self.alpha))
        self.assertEqual(self.paths.owners_file.read_bytes(), replaced, "immutable: the mutable owner file was not repaired or rewritten")

    def test_the_card_and_consumption_points_refuse_it_too(self) -> None:
        import herdr_workflow

        self.legacy_finished()
        self.omit()
        self.FAULTS["unreadable"](self)
        live = self.herdr.list_agents()
        self.assertIn("recorded pane cannot be established", herdr_workflow.pair_switch_problem(self.paths, self.state(), live))
        binding = hpair.binding_from_entry(self.alpha, bound_at="2026-01-01T00:00:00Z")
        with self.assertRaisesRegex(hs.SupervisorError, "recorded pane cannot be established"), self.sup.store.transaction():
            self.sup.initialize("task", "codex", workflow_policy="gated_v2", run_id=str(uuid.uuid4()), session_selection=self.saved(self.alpha), session_pair=binding)
        with self.assertRaisesRegex(hs.SupervisorError, "cannot create a named pair|unreadable or corrupt"):  # an unreadable owner file also stops the creation's own owner check
            self.sup.prepare_task_sessions(self.creation("Fresh Team"), preparation_id=str(uuid.uuid4()))
        self.assertEqual((self.herdr.splits, self.herdr.pane_runs), ([], []))

    def test_genuinely_closed_or_restored_and_settled_old_sessions_still_start(self) -> None:
        for label in ("closed", "restored identity and settled"):
            with self.subTest(label):
                self.setUp()
                self.legacy_finished()
                agent = self.omit()
                self.FAULTS["unreadable"](self)
                if label == "closed":
                    self.herdr.agents.pop("codex-main")  # positively absent: no live agent could be that session
                else:
                    agent["agent_session"] = {"value": self.owners["codex"]["session_id"]}
                    agent["agent_status"] = "idle"
                self.assertTrue(self.start(self.saved(self.alpha))["ok"])

    def test_an_identity_less_agent_of_another_kind_is_not_a_candidate_for_the_old_session(self) -> None:
        self.legacy_finished()
        self.herdr.agents.pop("codex-main")  # the old codex is positively closed
        self.herdr.agents["claude-main"]["agent_status"] = "idle"
        self.FAULTS["unreadable"](self)
        self.herdr.agents["stray"] = {"agent": "claude", "name": "stray", "pane_id": "w7:p8", "agent_status": "working"}  # another kind, no identity
        self.assertTrue(self.start(self.saved(self.alpha))["ok"], "a claude agent without identity cannot be the closed codex session; the claude session itself has an exact idle record")

    def test_a_healthy_legacy_locator_catches_a_different_session_that_now_works_in_the_old_pane(self) -> None:
        self.legacy_finished()
        agent = self.herdr.agents["codex-main"]
        agent["agent_session"] = {"value": "cccccccc-9999-4999-8999-000000000009"}
        agent["agent_status"] = "working"
        with self.assertRaisesRegex(hs.SupervisorError, "now carries session"):
            self.start(self.saved(self.alpha))
        agent["agent_status"] = "idle"
        self.assertTrue(self.start(self.saved(self.alpha))["ok"])

    def test_no_previous_run_and_no_global_owners_file_still_start(self) -> None:
        self.paths.owners_file.unlink()
        self.assertFalse(self.paths.state_file.exists())
        self.assertTrue(self.start(self.saved(self.alpha))["ok"])

    def test_a_previous_named_run_is_unaffected_because_its_locator_is_frozen(self) -> None:
        self.finished_alpha()
        self.paths.owners_file.write_text("{")
        self.omit("alpha-codex")
        with self.assertRaisesRegex(hs.SupervisorError, "no native identity"):
            self.start(self.saved(self.beta))


class ReplayTests(Boundary):
    def test_a_save_existing_start_replayed_after_its_registration_committed_progresses_without_duplicates(self) -> None:
        free = self.add_free_pair()
        run_id = str(uuid.uuid4())
        command = self.command(self.existing("Gamma", free), run_id)
        with self.sup.store.transaction(), mock.patch.object(self.sup, "initialize", side_effect=OSError("crash between the registry commit and the run")):
            with self.assertRaises(OSError):
                self.sup.apply_command(None, command)
        self.assertEqual(len(hpair.list_entries(self.paths)), 3)
        self.assertFalse(self.paths.state_file.exists())
        revision = self.registry_doc()["revision"]
        with self.sup.store.transaction():
            result = self.sup.apply_command(None, command)
        self.assertTrue(result["ok"], result)
        self.assertEqual((len(hpair.list_entries(self.paths)), self.registry_doc()["revision"]), (3, revision), "the replay registered nothing more")
        self.herdr.responses = self.plan_for(self.existing("Gamma", free))
        self.sup.resume()
        self.assertEqual((self.state()["run_id"], self.state()["session_pair"]["name"], len(self.herdr.prompts)), (run_id, "Gamma", 1))
        self.assertEqual(self.prompt_targets(), {"free-codex"})

    def test_a_conflicting_replay_or_a_second_request_for_the_committed_sessions_still_refuses(self) -> None:
        free = self.add_free_pair()
        run_id = str(uuid.uuid4())
        with self.sup.store.transaction(), mock.patch.object(self.sup, "initialize", side_effect=OSError("crash")):
            with self.assertRaises(OSError):
                self.sup.apply_command(None, self.command(self.existing("Gamma", free), run_id))
        before = self.snapshot_registry()
        with self.assertRaisesRegex(hs.SupervisorError, "already committed with different content"):
            self.start(self.existing("Renamed", free), run_id)
        with self.assertRaisesRegex(hs.SupervisorError, "already used by a saved pair"):
            self.start(self.existing("Delta", free), str(uuid.uuid4()))
        self.assertEqual(self.snapshot_registry(), before)

    def snapshot_registry(self) -> bytes:
        return hpair.registry_path(self.paths).read_bytes()


class ContextPromptTests(Boundary):
    def test_every_workflow_prompt_of_a_named_run_states_the_selected_context_and_the_task_body_once(self) -> None:
        self.begin(self.saved(self.alpha))
        first = self.herdr.prompts[0][1]
        for expected in (str(self.repo_a), str(self.review_a), str(self.project_root), "Selected pair context"):
            self.assertIn(expected, first)
        self.assertEqual(first.count("Investigate the widget"), 1)
        state = self.state()
        state["last_successful_handoff"] = {"stage": "plan", "from_agent": "codex", "summary": "handoff"}
        for kind in ("handoff", "continuation:plan_approved", "continuation"):
            with self.subTest(kind=kind):
                state["continuation"] = {"kind": "plan_approved", "plan_sha256": "a" * 64, "payload_sha256": "b" * 64}
                state["interrupted_delivery"] = {"turn_id": str(uuid.uuid4())}
                prompt = self.sup.build_prompt(state, str(uuid.uuid4()), kind)
                self.assertIn(str(self.repo_a), prompt)
                self.assertIn(str(self.review_a), prompt)
                self.assertNotIn("Investigate the widget", prompt, "the task body is never replayed")
        state["agent_followup"] = {"decision": {"decision_id": str(uuid.uuid4()), "gate_type": "plan_approval"}, "question": "why?", "response_path": str(self.review_a / "x.md")}
        with mock.patch.object(self.sup, "followup_decision_label", return_value="plan approval"):
            follow = self.sup.build_followup_prompt(state, str(uuid.uuid4()))
        self.assertIn(str(self.repo_a), follow)

    def test_the_instruction_root_is_frozen_with_the_run_through_configuration_drift_and_restart(self) -> None:
        self.begin(self.saved(self.alpha))
        frozen = str(self.project_root)
        other = self.project_root / "other-instructions"
        other.mkdir()
        self.config["project_root"] = str(other)
        restarted = self.make_supervisor()
        state = self.state()
        state["last_successful_handoff"] = {"stage": "plan", "from_agent": "codex", "summary": "h"}
        prompt = restarted.build_prompt(state, str(uuid.uuid4()), "handoff")
        self.assertIn(f"under {frozen};", prompt)
        self.assertNotIn(str(other), prompt)
        self.assertEqual(restarted.context_config(state)["project_root"], frozen)
        self.assertEqual(restarted.context_config()["project_root"], frozen, "without a state the active run's frozen root answers")

    def test_a_legacy_run_has_no_pair_context_and_keeps_the_configured_root(self) -> None:
        legacy = {"policy": "preserve", "profiles": {"codex": "default", "claude": "default"}}
        self.herdr.responses = self.plan_for(legacy)
        self.assertTrue(self.start(legacy)["ok"])
        self.sup.resume()
        prompt = self.herdr.prompts[0][1]
        self.assertNotIn("Selected pair context", prompt)
        self.assertIn(f"under {self.project_root};", prompt)
        self.assertIs(self.sup.context_config(self.state()), self.config)


class CurrentAuthorizationTests(Boundary):
    def refuse(self, pattern: str) -> None:
        self.assert_refused_without_effect(self.saved(self.alpha), pattern)

    def test_roots_that_no_longer_cover_a_saved_pair_refuse_its_new_start(self) -> None:
        self.config["session_pairs"]["allowed_roots"] = [str(self.repo_b), str(self.review_b)]
        self.refuse("outside the administrator-allowed roots")

    def test_directory_symlink_and_instruction_root_drift_refuse_a_new_start(self) -> None:
        moved = self.project_root / "repo-alpha-real"
        self.repo_a.rename(moved)
        self.repo_a.symlink_to(moved)
        self.refuse("without symlinks")
        self.repo_a.unlink()
        self.assertFalse(self.repo_a.exists())
        self.refuse("does not exist")
        moved.rename(self.repo_a)
        self.config["session_pairs"]["instruction_root"] = str(self.repo_b)
        self.refuse("instruction_root must be the configured instruction root")
        self.config["session_pairs"]["instruction_root"] = str(self.project_root)
        self.assertTrue(self.start(self.saved(self.alpha))["ok"], "positive outcome once the context is authorized again")

    def test_a_running_pair_keeps_its_bound_context_when_the_roots_change_later(self) -> None:
        self.begin(self.saved(self.alpha))
        self.config["session_pairs"]["allowed_roots"] = [str(self.repo_b)]
        self.assertEqual(self.sup.context_config(self.state())["review_root"], str(self.review_a))
        self.approve_pending()
        self.assertEqual(self.state()["session_pair"]["context"]["repository"], str(self.repo_a))

    def test_the_start_path_itself_refuses_before_initialize_is_reached(self) -> None:
        self.config["session_pairs"]["allowed_roots"] = [str(self.repo_b)]
        with self.assertRaisesRegex(hs.SupervisorError, "outside the administrator-allowed roots"):
            self.sup._start_pair_binding(self.saved(self.alpha), str(uuid.uuid4()))
        self.assertEqual(self.herdr.prompts, [])

    def test_initialize_re_checks_the_current_authorization_of_the_context_it_freezes(self) -> None:
        self.config["session_pairs"]["allowed_roots"] = [str(self.repo_b)]
        binding = hpair.binding_from_entry(self.alpha, bound_at="2026-01-01T00:00:00Z")
        with self.assertRaisesRegex(hs.SupervisorError, "outside the administrator-allowed roots"), self.sup.store.transaction():
            self.sup.initialize("task", "codex", workflow_policy="gated_v2", run_id=str(uuid.uuid4()), session_selection=self.saved(self.alpha), session_pair=binding)


class IsolationFailClosedTests(Boundary):
    def query_agent(self, session: str) -> None:
        self.herdr.agents["codex-query"] = FakeHerdr.agent("codex", "codex-query", "w8:p1", session)
        self.config["query"]["providers"] = {"codex": {"agent_name": "codex-query", "read_only_contract": "fixture contract"}}

    def register_query(self, session: str):
        self.query_agent(session)
        return hs.register_query_provider(self.paths, self.config, self.herdr, provider="codex", agent_name="codex-query", acknowledge_read_only_contract=True)

    def test_a_saved_pair_session_is_protected_without_any_legacy_owners_file(self) -> None:
        os.replace(self.paths.owners_file, self.paths.owners_file.with_name("owners-preserved.json"))
        with self.assertRaisesRegex(hs.SupervisorError, "reuses a workflow native session"):
            self.register_query(BETA["codex"][1])
        self.assertFalse(self.paths.query_registry_file.exists())

    def test_a_malformed_pair_registry_refuses_query_registration_instead_of_erasing_protection(self) -> None:
        hpair.registry_path(self.paths).write_text("{")
        with self.assertRaises(hs.SupervisorError):
            self.register_query(BETA["codex"][1])
        self.assertFalse(self.paths.query_registry_file.exists())

    def test_a_healthy_disjoint_session_still_registers_with_and_without_legacy_owners(self) -> None:
        os.replace(self.paths.owners_file, self.paths.owners_file.with_name("owners-preserved.json"))
        self.assertEqual(self.register_query(OWN["codex"][1])["session_id_abbrev"], OWN["codex"][1][:8])

    def test_query_use_sends_nothing_when_workflow_ownership_is_unknown(self) -> None:
        import herdr_query

        self.herdr.agents["codex-query"] = FakeHerdr.agent("codex", "codex-query", "w8:p1", OWN["codex"][1])
        hs.atomic_write_json(self.paths.query_owner_file, {"agent_name": "codex-query", "pane_id": "w8:p1", "session_id": OWN["codex"][1]}, mode=0o600)
        worker = herdr_query.QueryWorker(self.paths, self.config, self.herdr, clock=self.clock.time)
        self.assertEqual(worker.select_provider()[0], "codex", "healthy and disjoint: usable")
        hpair.registry_path(self.paths).write_text("{")
        provider, agent, report = worker.select_provider()
        self.assertIsNone(provider)
        self.assertEqual({v["reason"] for v in report["reasons"].values()}, {"workflow_ownership_unknown"})
        self.assertEqual({v["status"] for v in worker.availability_report().values()}, {"workflow_ownership_unknown"})
        self.assertEqual(self.herdr.prompts, [], "zero sends on uncertainty")

    def test_helper_registration_and_pair_start_fail_closed_on_unreadable_ownership(self) -> None:
        import herdr_helpers

        manager = herdr_helpers.HelperManager(self.sup)
        binding = {"provider": "codex", "session_id": ALPHA["codex"][1], "pane_id": "w4:p2", "model": "m", "cwd": str(self.project_root), "access": "read-only"}
        good = hpair.registry_path(self.paths).read_text()
        hpair.registry_path(self.paths).write_text("{")
        with self.assertRaises(hs.SupervisorError):
            manager._verify(binding, ready=False)
        hpair.registry_path(self.paths).write_text(good)
        free = self.add_free_pair()
        for label, path, text in (("helper registry", self.state_dir / "helpers" / "registry.json", "{"), ("helper registry shape", self.state_dir / "helpers" / "registry.json", "[]"),
                                  ("query registry", self.paths.query_registry_file, "[]"), ("query owner shape", self.paths.query_owner_file, "[]")):
            with self.subTest(label):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text)
                self.assert_refused_without_effect(self.existing("Gamma", free), "malformed|unreadable|corrupt")
                path.unlink()


class NestedOwnershipTests(Boundary):
    """CI5-R1: every record of every supported ownership registry is validated exactly; malformed, missing, non-object or wrong-type evidence is uncertainty (refusal),
    never a silently dropped entry. The files are never repaired or overwritten."""

    def write(self, kind: str, entry) -> "Path":
        if kind == "query":
            path, doc = self.paths.query_registry_file, {"schema_version": 1, "providers": {"codex": entry}}
        elif kind == "helper":
            path, doc = self.state_dir / "helpers" / "registry.json", {"schema_version": 1, "workers": {"worker": entry}}
        else:
            path, doc = self.paths.query_owner_file, entry
        path.parent.mkdir(parents=True, exist_ok=True)
        hs.atomic_write_json(path, doc, mode=0o600)
        return path

    def record(self, **over) -> dict:
        return {"agent_name": "some-query", "provider": "codex", "pane_id": OWN["codex"][0], "session_id": OWN["codex"][1], **over}

    def test_nested_malformed_records_refuse_an_actual_save_existing_start_and_are_left_untouched(self) -> None:
        free = self.add_free_pair()
        bad = {"list session id": self.record(session_id=[OWN["codex"][1]]), "missing session id": {k: v for k, v in self.record().items() if k != "session_id"},
               "empty session id": self.record(session_id=""), "int session id": self.record(session_id=7), "non-object": [OWN["codex"][1]],
               "string record": OWN["codex"][1], "null record": None, "pane wrong type": self.record(pane_id=3), "agent name wrong type": self.record(agent_name=["x"])}
        for kind in ("query", "helper", "query_owner"):
            for label, entry in bad.items():
                with self.subTest(kind=kind, case=label):
                    path = self.write(kind, entry)
                    before = path.read_bytes()
                    self.assert_refused_without_effect(self.existing("Gamma", free), "malformed|unreadable|corrupt")
                    self.assertEqual(path.read_bytes(), before, "a malformed registry is reported, never repaired or overwritten")
                    path.unlink()
        self.assertEqual(len(hpair.list_entries(self.paths)), 2, "no pair was registered for any malformed ownership record")

    def test_an_unknown_provider_key_in_the_query_registry_is_malformed(self) -> None:
        free = self.add_free_pair()
        path = self.paths.query_registry_file
        hs.atomic_write_json(path, {"schema_version": 1, "providers": {"gemini": self.record()}}, mode=0o600)
        self.assert_refused_without_effect(self.existing("Gamma", free), "malformed")

    def test_valid_empty_and_disjoint_registries_are_positives_through_a_real_registration(self) -> None:
        free = self.add_free_pair()
        hs.atomic_write_json(self.paths.query_registry_file, {"schema_version": 1, "providers": {}}, mode=0o600)
        hs.atomic_write_json(self.state_dir / "helpers" / "registry.json", {"schema_version": 1, "workers": {}}, mode=0o600)
        self.assertEqual(hpair.foreign_sessions(self.paths) - hpair.known_sessions(self.paths), set())
        other = "cccccccc-7777-4777-8777-000000000007"
        hs.atomic_write_json(self.paths.query_registry_file, {"schema_version": 1, "providers": {"codex": self.record(session_id=other, pane_id=None)}}, mode=0o600)
        hs.atomic_write_json(self.paths.query_owner_file, {"agent_name": "codex-query", "pane_id": "w9:p9", "session_id": "cccccccc-7777-4777-8777-000000000008"}, mode=0o600)
        self.assertTrue(self.start(self.existing("Gamma", free))["ok"], "valid, disjoint ownership records do not block")
        self.assertIn("Gamma", {e["name"] for e in hpair.list_entries(self.paths)})

    def test_a_valid_record_that_names_the_selected_session_still_refuses_as_owned(self) -> None:
        free = self.add_free_pair()
        self.write("query", self.record())
        self.assert_refused_without_effect(self.existing("Gamma", free), "already used by a saved pair")

    def test_helper_registration_refuses_on_a_malformed_query_record_and_does_not_refuse_its_own_registry(self) -> None:
        import herdr_helpers

        manager = herdr_helpers.HelperManager(self.sup)
        binding = {"provider": "codex", "session_id": OWN["codex"][1], "pane_id": OWN["codex"][0], "model": "m", "cwd": str(self.project_root), "access": "read-only"}
        self.add_free_pair()
        self.write("query", self.record(session_id=[OWN["codex"][1]]))
        with self.assertRaisesRegex(hs.SupervisorError, "malformed"):
            manager._verify(binding, ready=False)
        self.paths.query_registry_file.unlink()
        self.write("helper", {"provider": "codex", "pane_id": OWN["codex"][0], "session_id": OWN["codex"][1], "model": "m", "cwd": "/x", "access": "read-only"})
        with self.assertRaises(hs.SupervisorError) as caught:
            manager._verify(binding, ready=False)
        self.assertNotIn("workflow/query owners cannot be helper workers", str(caught.exception), "a registered helper is not refused for being in its own registry")


class PendingStartLockTests(TgPairCase):
    def selection_thread(self, token: str) -> threading.Thread:
        thread = threading.Thread(target=lambda: self.tap(token))
        thread.start()
        return thread

    def paused_selection(self, token: str):
        """A selection that is inside its commit (holding the pending lock) until `release` is set."""
        entered, release = threading.Event(), threading.Event()
        original = self.bridge._pair_commit

        def delayed(*args, **kwargs):
            entered.set()
            assert release.wait(15)
            return original(*args, **kwargs)

        patch = mock.patch.object(self.bridge, "_pair_commit", delayed)
        patch.start()
        self.addCleanup(patch.stop)
        thread = self.selection_thread(token)
        self.assertTrue(entered.wait(10))
        return thread, release

    def two_pairs(self):
        alpha = self.register("Alpha", ALPHA)
        beta = self.register("Beta", BETA, self.repo_b, self.review_b)
        pending = self.choose_saved(alpha)
        start = self.one("reset_budget", budget=0)
        self.tap(self.one("pair_menu"))
        return alpha, beta, pending, start, self.one("pair_use", pair_id=beta["pair_id"])

    def test_selection_in_flight_makes_a_concurrent_start_wait_and_then_see_the_new_selection(self) -> None:
        alpha, beta, pending, start, choose_beta = self.two_pairs()
        thread, release = self.paused_selection(choose_beta)
        results: list = []
        starter = threading.Thread(target=lambda: results.append(self.tap(start)))
        starter.start()
        time.sleep(0.4)
        self.assertEqual(results, [], "the Start is serialised behind the selection, not interleaved with it")
        release.set()
        thread.join(10)
        starter.join(10)
        self.assertFalse(thread.is_alive() or starter.is_alive())
        self.assertEqual(self.pending_inbox(), [], "the Start button of the old selection was invalidated by the committed selection")
        self.assertEqual(self.pending(pending)["session_selection"]["pair"]["pair_id"], beta["pair_id"])
        self.assertEqual(self.pending(pending)["status"], "pending")

    def test_a_selection_that_arrives_after_a_start_cannot_overwrite_the_receipt(self) -> None:
        alpha, beta, pending, start, choose_beta = self.two_pairs()
        self.assertEqual(self.tap(start)["result"], "enqueued")
        for stage in ("start_enqueued", "started"):
            record = self.pending(pending)
            frozen = json.dumps(record, sort_keys=True)
            self.assertEqual(record["status"], stage)
            self.assertIn("rejected", self.tap(choose_beta))
            self.assertEqual(json.dumps(self.pending(pending), sort_keys=True), frozen, f"a late selection must not rewrite a {stage} receipt")
            self.say(f"/pair-set Late | {self.repo_b} | {self.review_b}")
            self.assertEqual(json.dumps(self.pending(pending), sort_keys=True), frozen)
            if stage == "start_enqueued":
                self.process_and_run()
        self.assertEqual(self.state()["session_pair"]["pair_id"], alpha["pair_id"])
        self.assertEqual(self.prompt_targets(), {"alpha-codex"})

    def test_genuine_interleaving_selection_paused_in_commit_while_start_is_already_applied(self) -> None:
        """The reviewer's case: whichever gets the lock first, the committed receipt is never overwritten and the run matches the receipt."""
        alpha, beta, pending, start, choose_beta = self.two_pairs()
        thread, release = self.paused_selection(choose_beta)
        starter = threading.Thread(target=lambda: self.tap(start))
        starter.start()
        release.set()
        thread.join(10)
        starter.join(10)
        self.assertFalse(thread.is_alive() or starter.is_alive())
        record = self.pending(pending)
        inbox = self.pending_inbox()
        if inbox:  # the Start won: the receipt names Alpha and nothing later changed it
            self.assertEqual(inbox[0]["session_selection"]["pair"]["pair_id"], alpha["pair_id"])
            self.assertEqual(record["session_selection"]["pair"]["pair_id"], alpha["pair_id"])
            self.assertIn(record["status"], ("start_enqueued", "supervisor_consuming", "started"))
            self.process_and_run()
            self.assertEqual(self.state()["session_pair"]["pair_id"], alpha["pair_id"])
        else:  # the selection won: Beta is selected, nothing was started
            self.assertEqual(record["status"], "pending")
            self.assertEqual(record["session_selection"]["pair"]["pair_id"], beta["pair_id"])

    def test_policy_and_profile_choices_that_could_drop_the_pair_cannot_touch_a_committed_start(self) -> None:
        alpha = self.register("Alpha", ALPHA)
        pending = self.choose_saved(alpha)
        stale_policy = self.tokens("session_policy", policy="fresh-all")
        self.assertEqual(self.tap(self.one("reset_budget", budget=0))["result"], "enqueued")
        frozen = json.dumps(self.pending(pending), sort_keys=True)
        for token, _ in stale_policy:
            self.assertIn("rejected", self.tap(token))
        self.assertEqual(json.dumps(self.pending(pending), sort_keys=True), frozen)

    def test_a_restart_reconciles_the_receipt_and_a_late_selection_is_still_refused(self) -> None:
        alpha, beta, pending, start, choose_beta = self.two_pairs()
        self.tap(start)
        restarted = self.make_bridge()
        restarted.model_capability_reader = lambda provider: True
        self.assertIn("rejected", restarted.handle_update(__import__("test_telegram").callback(self.uid(), choose_beta)))
        again = restarted.handle_update(__import__("test_telegram").callback(self.uid(), start))
        self.assertIn("rejected", again)
        self.assertEqual(len(self.pending_inbox()), 1, "no second command")
        self.process_and_run()
        self.assertEqual(self.pending(pending)["status"], "started")
        self.assertEqual(self.state()["session_pair"]["pair_id"], alpha["pair_id"])

    def lock_probe(self):
        acquired = threading.Event()

        def probe() -> None:
            with self.bridge._pending_lock():
                acquired.set()

        thread = threading.Thread(target=probe)
        thread.start()
        return acquired, thread

    def assert_writer_holds_the_lock(self, patch_target, patch_name, trigger, blocks_when=lambda *a, **k: True) -> None:
        entered, release = threading.Event(), threading.Event()
        original = getattr(patch_target, patch_name)

        def blocked(*args, **kwargs):
            if blocks_when(*args, **kwargs):
                entered.set()
                assert release.wait(15)
            return original(*args, **kwargs)

        with mock.patch.object(patch_target, patch_name, blocked):
            writer = threading.Thread(target=trigger)
            writer.start()
            self.assertTrue(entered.wait(10), "the writer reached its commit")
            acquired, probe = self.lock_probe()
            time.sleep(0.4)
            held = not acquired.is_set()
            release.set()
            writer.join(10)
            probe.join(10)
        self.assertTrue(held, "another pending-start writer was NOT excluded while this one was mid-commit")
        self.assertTrue(acquired.is_set())

    def test_policy_and_profile_choices_pair_set_and_supersede_each_hold_the_pending_start_lock_while_they_write(self) -> None:
        self.task_card()
        policy = self.one("session_policy", policy="fresh-all")
        self.assert_writer_holds_the_lock(self.bridge, "_send_start_confirmation", lambda: self.tap(policy))
        self.task_card()
        self.tap(self.one("pair_menu"))
        self.tap(self.one("pair_create"))
        self.assert_writer_holds_the_lock(self.bridge, "_send_start_confirmation", lambda: self.say(f"/pair-set Fresh Team | {self.repo_a} | {self.review_a}"))
        self.task_card()
        self.assert_writer_holds_the_lock(hs, "atomic_write_json", lambda: self.say("/task a newer task"),
                                          blocks_when=lambda path, value, **kw: isinstance(value, dict) and value.get("status") == "superseded")

    def test_a_newer_task_cannot_supersede_over_a_start_in_flight(self) -> None:
        alpha = self.register("Alpha", ALPHA)
        pending = self.choose_saved(alpha)
        self.tap(self.one("reset_budget", budget=0))
        status = self.pending(pending)["status"]
        self.say("/task another task")
        self.assertEqual(self.pending(pending)["status"], status, "an enqueued Start is not 'superseded' by a later /task")


class PairCardParityTests(TgPairCase):
    def test_the_card_and_old_start_refuse_an_old_pane_that_reports_an_agent_without_identity(self) -> None:
        alpha = self.register("Alpha", ALPHA)
        beta = self.register("Beta", BETA, self.repo_b, self.review_b)
        selection = self.saved(alpha)
        self.herdr.responses = self.plan_for(selection)
        self.assertTrue(self.start(selection)["ok"])
        self.sup.resume()
        state = self.state()
        self.sup.apply_command(state, {"request_id": uuid.uuid4().hex[:32], "action": "cancel", "run_id": state["run_id"], "actor": "cli"})
        pending = self.choose_saved(beta)
        start = self.one("reset_budget", budget=0)
        agent = self.herdr.agents["alpha-codex"]
        agent["agent_status"] = "working"
        agent.pop("agent_session")
        self.assertEqual(self.tap(start)["rejected"], "pair_not_startable")
        self.assertIn("no native identity", self.api.sent[-1]["text"])
        self.assertEqual(self.pending_inbox(), [])
        self.choose_saved(beta)
        self.assertIn("Cannot start:", self.api.sent[-1]["text"])
        self.assertFalse(any(b.startswith("Start task") for b in self.card_buttons()))
        agent["agent_session"] = {"value": ALPHA["codex"][1]}
        agent["agent_status"] = "idle"
        self.choose_saved(beta)
        self.assertIn("Start task · reset budget 0", self.card_buttons())
        self.assertEqual(self.pending(pending)["status"], "superseded")

    def test_the_card_and_old_start_refuse_the_same_unsettled_old_boundary_and_current_authorization(self) -> None:
        alpha = self.register("Alpha", ALPHA)
        beta = self.register("Beta", BETA, self.repo_b, self.review_b)
        self.begin_alpha = None
        self.choose_saved(beta)
        start = self.one("reset_budget", budget=0)
        # previous pair still working (the previous state is terminal): build it through the real supervisor path
        selection = self.saved(alpha)
        self.herdr.responses = self.plan_for(selection)
        self.assertTrue(self.start(selection)["ok"])
        self.sup.resume()
        state = self.state()
        self.sup.apply_command(state, {"request_id": uuid.uuid4().hex[:32], "action": "cancel", "run_id": state["run_id"], "actor": "cli"})
        self.herdr.agents["alpha-codex"]["agent_status"] = "working"
        result = self.tap(start)
        self.assertEqual(result["rejected"], "pair_not_startable")
        self.assertIn("still 'working'", self.api.sent[-1]["text"])
        self.assertEqual(self.pending_inbox(), [])
        self.herdr.agents["alpha-codex"]["agent_status"] = "idle"
        self.config["session_pairs"]["allowed_roots"] = [str(self.repo_a)]
        self.choose_saved(beta)
        self.assertIn("Cannot start:", self.api.sent[-1]["text"])
        self.assertIn("outside the administrator-allowed roots", self.api.sent[-1]["text"])


if __name__ == "__main__":
    import unittest

    unittest.main()
