"""Alias loss with an exact persisted native-session match continues through the verified pane target;
anything weaker fails closed. No command ever reaches a wrong target; owners.json identity never changes."""

from __future__ import annotations

import json
import unittest

import herdr_runtime  # noqa: E402
from v2_fixtures import CLAUDE_SESSION, CODEX_SESSION, FakeHerdr, V2Case, hs  # noqa: E402

OTHER = "0bbbbbbb-cccc-4ddd-8eee-ffffffffffff"


class AliasLossTests(V2Case):
    def drop_alias(self, provider: str = "codex", pane: str = "w3:p2") -> None:
        """Herdr kept the conversation (exact native session) but lost the display alias."""
        key = f"{provider}-main"
        record = self.herdr.agents.pop(key)
        self.herdr.agents[pane] = {**record, "name": None, "pane_id": pane}

    def owners_file_content(self) -> dict:
        return json.loads(self.paths.owners_file.read_text())

    def wrong_targets(self) -> list[tuple[str, str]]:
        valid = {"codex-main", "claude-main", "w3:p2", "w3:p1"}
        return [t for t in self.herdr.targets if t[1] not in valid]

    def test_initial_delivery_settled_read_wait_and_gate_with_missing_alias(self) -> None:
        self.drop_alias()
        self.write_plan()
        waits = {"n": 0}

        def settle_later(fake, name):
            waits["n"] += 1
            if waits["n"] == 2:
                fake.agents[name]["agent_status"] = "idle"

        self.herdr.on_wait = settle_later
        self.assertEqual(self.start_gated([{"v2": ("plan", "human", "plan_approval", str(self.plan_payload())), "status": "working"}]), 4)
        self.assertEqual(self.state()["supervisor_state"], "WAIT_PLAN_APPROVAL")
        commands = [c for c, _ in self.herdr.targets]
        self.assertIn("prompt", commands)
        self.assertIn("wait", commands)
        self.assertIn("read", commands)
        self.assertTrue(all(target == "w3:p2" for command, target in self.herdr.targets if command in ("prompt", "wait", "read")), self.herdr.targets)
        self.assertEqual(self.herdr.starts, [], "alias loss never creates or restores an agent")
        self.assertEqual(self.owners_file_content()["codex"], {"pane_id": "w3:p2", "session_id": CODEX_SESSION})
        self.assertEqual(self.wrong_targets(), [])

    def test_missing_result_retry_uses_the_pane_target(self) -> None:
        self.write_plan()
        self.assertEqual(self.start_gated([{"output": "no block\n", "status": "idle"}]), 2)
        state = self.state()
        self.drop_alias()
        self.herdr.outputs["w3:p2"] = self.herdr.outputs.pop("codex-main")  # the fake keys outputs by record key
        self.herdr.outputs["w3:p2"] = FakeHerdr.block_v2(state["run_id"], state["delivery"]["turn_id"], "plan", "human", "plan_approval", str(self.plan_payload()))
        self.herdr.targets.clear()
        with self.sup.store.transaction():
            st = self.sup.store.read_state()
            result = self.sup.apply_command(st, {"action": "retry_routing_result", "run_id": st["run_id"], "turn_id": st["missing_result"]["turn_id"]})
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.state()["supervisor_state"], "WAIT_PLAN_APPROVAL")
        self.assertEqual([t for t in self.herdr.targets], [("read", "w3:p2")], "one settled read against the verified pane, nothing else")
        self.assertEqual(self.herdr.starts, [])
        self.assertEqual(len(self.herdr.prompts), 1)

    def test_resume_restart_and_quota_reconciliation_with_missing_alias(self) -> None:
        self.write_plan()
        self.start_gated([{"v2": ("plan", "human", "plan_approval", str(self.plan_payload()))}])
        self.approve_pending()
        self.drop_alias()
        self.herdr.targets.clear()
        # resume after restart: the approval continuation is delivered to the pane target
        fresh = self.make_supervisor()
        self.herdr.responses = [{"v2": ("brief", "human", "generic_question", str(self.question_payload()))}]
        self.assertEqual(fresh.resume(), 2)
        self.assertEqual(self.state()["pending_gate"]["gate_type"], "generic_question")
        self.assertTrue(all(target == "w3:p2" for command, target in self.herdr.targets if command in ("prompt", "read", "wait")), self.herdr.targets)
        self.assertEqual(self.owners_file_content()["codex"]["session_id"], CODEX_SESSION)
        # quota reconciliation path: a blocking window during a working turn reads the visible screen of the pane target
        self.herdr.targets.clear()
        with self.sup.store.transaction():
            st = self.sup.store.read_state()
            self.sup.answer_gate(st, run_id=st["run_id"], gate_id=st["pending_gate"]["gate_id"], actor="cli", answer="enum")
        self.write_quota("codex", 0, self.clock.current + 3600, 60, self.clock.current + 86400)

        def blocked_screen(fake, name):
            fake.visible[name] = "You've hit your usage limit"
            fake.agents[name]["agent_status"] = "idle"

        self.herdr.on_wait = blocked_screen
        self.herdr.responses = [{"status": "working", "output": "no block yet\n", "visible": "You've hit your usage limit"}]
        code = self.sup.resume()
        self.assertIn(code, (2, 3))
        self.assertTrue(all(target == "w3:p2" for command, target in self.herdr.targets if command in ("prompt", "read", "wait")), self.herdr.targets)
        self.assertEqual(self.wrong_targets(), [])
        self.assertEqual(self.herdr.starts, [])

    def test_doctor_and_status_report_identity_match_without_alias(self) -> None:
        self.write_plan()
        self.start_gated([{"v2": ("plan", "human", "plan_approval", str(self.plan_payload()))}])
        self.drop_alias()
        report = self.sup.doctor()
        codex = report["agents"]["codex"]
        self.assertTrue(codex["detected"])
        self.assertTrue(codex["session_matches"])
        self.assertFalse(codex["alias_present"])
        self.assertEqual(codex["identity"], "exact_session_without_alias")
        self.assertEqual(codex["target"], "w3:p2")
        self.assertTrue(report["ok"], report.get("errors"))
        status = self.sup.status()
        self.assertEqual(status["agents"]["codex"]["identity"], "exact_session_without_alias")
        # a genuinely missing session is reported differently
        del self.herdr.agents["w3:p2"]
        report = self.sup.doctor()
        self.assertEqual(report["agents"]["codex"]["identity"], "missing")
        self.assertFalse(report["agents"]["codex"]["detected"])

    def test_adversarial_identities_fail_closed_without_touching_a_wrong_target(self) -> None:
        self.write_plan()
        self.start_gated([{"v2": ("plan", "human", "plan_approval", str(self.plan_payload()))}])
        self.approve_pending()
        owners_before = self.paths.owners_file.read_text()
        cases = {
            "same provider, different session, alias absent": ({"w9:p9": FakeHerdr.agent("codex", None, "w9:p9", OTHER)}, "missing"),
            "duplicate exact matches": ({"w3:p2": FakeHerdr.agent("codex", None, "w3:p2", CODEX_SESSION), "w5:p5": FakeHerdr.agent("codex", None, "w5:p5", CODEX_SESSION)}, "ambiguous"),
            "reused pane with a changed session": ({"w3:p2": FakeHerdr.agent("codex", None, "w3:p2", OTHER)}, "missing_pane"),
            "identity omitted": ({"w3:p2": {**FakeHerdr.agent("codex", None, "w3:p2", CODEX_SESSION), "agent_session": None}}, "missing_pane"),
            "exact session but a claude record": ({"w3:p2": FakeHerdr.agent("claude", None, "w3:p2", CODEX_SESSION)}, "conflict"),
            "alias reused by another session and nothing else": ({"codex-main": FakeHerdr.agent("codex", "codex-main", "w3:p2", OTHER)}, "conflict"),
        }
        for label, (agents, kind) in cases.items():
            with self.subTest(label):
                self.herdr.agents = {"claude-main": FakeHerdr.agent("claude", "claude-main", "w3:p1", CLAUDE_SESSION), **agents}
                self.herdr.available_panes = {"w3:p1", "w3:p2"} - {a.get("pane_id") for a in agents.values()}
                self.herdr.targets.clear()
                self.herdr.starts.clear()
                self.herdr.responses = []
                sup = self.make_supervisor()
                with self.assertRaises(hs.SupervisorError):
                    sup.resolve_target("codex")
                with self.assertRaises(hs.SupervisorError):
                    sup.ensure_agent("codex", allow_restore=False)
                self.assertEqual([t for t in self.herdr.targets if t[0] in ("prompt", "read", "wait", "send-keys")], [], label)
                self.assertEqual(self.paths.owners_file.read_text(), owners_before, "owners.json identity untouched")
                # the resume path: a genuinely missing session may be restored under the explicit policy (a start
                # with the exact persisted session id, never a prompt to a stranger); ambiguity and identity
                # conflicts stop in a wait. In every case no command reaches a record with another identity.
                self.herdr.responses = [{"v2": ("brief", "human", "generic_question", str(self.question_payload()))}] if kind == "missing" else []
                code = sup.resume()
                strangers = {key for key, agent in self.herdr.agents.items() if hs.session_identity(agent) != CODEX_SESSION and agent.get("agent") == "codex"} | {a.get("pane_id") for a in agents.values() if hs.session_identity(a) != CODEX_SESSION}
                touched = [t for t in self.herdr.targets if t[0] in ("prompt", "read", "wait", "send-keys") and t[1] in strangers]
                self.assertEqual(touched, [], f"{label}: a command reached a wrong identity")
                if kind == "missing":
                    self.assertEqual(len(self.herdr.starts), 1, "explicit restore policy: one start with the persisted session, in the recorded pane")
                    self.assertEqual(self.herdr.starts[0][3], ["resume", CODEX_SESSION])
                    self.assertEqual(self.herdr.starts[0][2], "w3:p2")
                    self.assertEqual(code, 2)
                elif kind == "missing_pane":
                    # the recorded pane is occupied by a stranger: never relocate, never create a workspace, never
                    # start elsewhere; stop in the structured owner-recovery wait
                    self.assertEqual((self.herdr.starts, self.herdr.workspaces, code), ([], [], 2), label)
                    recovery = self.state()["owner_recovery"]
                    self.assertEqual((recovery["classification"], recovery["provider"], recovery["recorded_pane"], recovery["session_id"]), ("missing_pane", "codex", "w3:p2", CODEX_SESSION))
                    self.assertIn("nothing was created, started, or resent", self.state()["wait_user_reason"])
                else:
                    self.assertEqual(self.herdr.starts, [], f"{label}: no restore from ambiguity or a conflicting identity")
                    self.assertEqual(code, 2)
                    self.assertIn("refusing", self.state()["wait_user_reason"])
                self.assertEqual(json.loads(self.paths.owners_file.read_text())["codex"]["session_id"], CODEX_SESSION)
                # reset the run to the approved wait for the next case
                with self.sup.store.transaction():
                    st = self.sup.store.read_state()
                    st.update(supervisor_state="RUNNING", wait_user_reason=None, wait_user_requires_action=False, delivery=None, pending_gate=None, owner_recovery=None,
                              continuation={"kind": "plan_approved", "gate_id": "g", "plan_sha256": st["approved_plan"]["plan_sha256"], "payload_sha256": "q" * 64})
                    self.sup.store.write_state(st)
                self.paths.owners_file.write_text(owners_before)

    def test_fresh_process_restart_into_a_live_delivery_targets_the_verified_pane(self) -> None:
        """The crash window the fix exists for: a new supervisor process resumes an accepted/uncertain
        working delivery after the alias disappeared. Every wait/read/key goes to the pane; nothing is replayed."""
        for status in ("accepted", "uncertain"):
            with self.subTest(delivery=status):
                self.reset_fixture()
                self.write_plan()
                payload = str(self.plan_payload())

                def die_midturn(fake, name):
                    raise KeyboardInterrupt

                self.herdr.on_wait = die_midturn
                self.assertEqual(self.start_gated([{"error": "timeout" if status == "uncertain" else None, "status": "working", "v2": ("plan", "human", "plan_approval", payload)}]), 3)
                persisted = self.state()
                self.assertEqual(persisted["supervisor_state"], "PAUSED")
                with self.sup.store.transaction():
                    st = self.sup.store.read_state()
                    st["delivery"]["status"] = status
                    st["delivery"].pop("accepted_at", None) if status == "uncertain" else None
                    self.sup.store.write_state(st)
                # new process: the alias is gone, the exact session lives nameless in its pane and is still working
                herdr2 = FakeHerdr()
                record = herdr2.agents.pop("codex-main")
                herdr2.agents["w3:p2"] = {**record, "name": None, "agent_status": "working"}
                herdr2.outputs["w3:p2"] = self.herdr.outputs.get("codex-main", "")
                waits = {"n": 0}

                def settle(fake, name):
                    waits["n"] += 1
                    if waits["n"] == 2:
                        fake.agents[name]["agent_status"] = "idle"
                        fake.outputs[name] = FakeHerdr.block_v2(persisted["run_id"], persisted["delivery"]["turn_id"], "plan", "human", "plan_approval", payload)

                herdr2.on_wait = settle
                sup2 = hs.Supervisor(self.paths, self.config, herdr2, clock=self.clock.time, sleeper=self.clock.sleep)
                sup2.head_resolver = self.head_resolver
                self.assertEqual(sup2.resume(), 4)
                self.assertEqual(self.state()["supervisor_state"], "WAIT_PLAN_APPROVAL")
                commands = [t for t in herdr2.targets if t[0] in ("wait", "read", "prompt", "send-keys")]
                self.assertTrue(commands, "the restart must have waited and read")
                self.assertTrue(all(target == "w3:p2" for _, target in commands), commands)
                self.assertEqual(herdr2.prompts, [], "the original task is never replayed")
                self.assertEqual(herdr2.starts, [])
                self.assertEqual(self.owners_file_content()["codex"]["session_id"], CODEX_SESSION)

    def test_fresh_process_restart_dismisses_a_blocked_quota_screen_on_the_verified_pane(self) -> None:
        self.config = hs.resolve_config_defaults(hs.deep_merge(self.config, {"quota_block_dismiss_keys": ["Enter"]}))
        self.sup = self.make_supervisor()
        self.write_plan()
        payload = str(self.plan_payload())

        def die_midturn(fake, name):
            raise KeyboardInterrupt

        self.herdr.on_wait = die_midturn
        self.assertEqual(self.start_gated([{"status": "working", "v2": ("plan", "human", "plan_approval", payload)}]), 3)
        persisted = self.state()
        # the persisted delivery is accepted; the pane now shows a provider quota dialog after the wait ended
        herdr2 = FakeHerdr()
        record = herdr2.agents.pop("codex-main")
        herdr2.agents["w3:p2"] = {**record, "name": None, "agent_status": "blocked"}
        herdr2.outputs["w3:p2"] = "You've hit your usage limit\n"
        herdr2.visible["w3:p2"] = "You've hit your usage limit"

        def after_keys(fake, name):
            fake.agents[name]["agent_status"] = "idle"
            fake.outputs[name] = FakeHerdr.block_v2(persisted["run_id"], persisted["delivery"]["turn_id"], "plan", "human", "plan_approval", payload)

        herdr2.on_wait = after_keys
        sup2 = hs.Supervisor(self.paths, self.config, herdr2, clock=self.clock.time, sleeper=self.clock.sleep)
        sup2.head_resolver = self.head_resolver
        with sup2.store.transaction():
            st = sup2.store.read_state()
            outcome = sup2.resume_interrupted_turn(st, "codex")
        self.assertEqual(outcome, "stop")
        self.assertEqual(self.state()["supervisor_state"], "WAIT_PLAN_APPROVAL")
        self.assertEqual([t for t in herdr2.targets if t[0] == "send-keys"], [("send-keys", "w3:p2")])
        self.assertTrue(all(target == "w3:p2" for command, target in herdr2.targets if command in ("wait", "read", "send-keys")), herdr2.targets)
        self.assertEqual(herdr2.prompts, [])

    def test_wrong_provider_carriers_never_become_targets_or_healthy_reports(self) -> None:
        """F1 matrix: alias+exact session but wrong provider, nameless wrong provider, alias plus a duplicate
        exact-session record — refused at task start, at every command path, and in doctor/status."""
        self.write_plan()
        self.start_gated([{"v2": ("plan", "human", "plan_approval", str(self.plan_payload()))}])
        self.approve_pending()
        owners_before = self.paths.owners_file.read_text()
        cases = {
            "alias + exact session + wrong provider": {"codex-main": FakeHerdr.agent("claude", "codex-main", "w3:p2", CODEX_SESSION)},
            "nameless wrong provider with codex's session": {"w3:p2": FakeHerdr.agent("claude", None, "w3:p2", CODEX_SESSION)},
            "alias exact plus a duplicate exact-session record": {"codex-main": FakeHerdr.agent("codex", "codex-main", "w3:p2", CODEX_SESSION), "w5:p5": FakeHerdr.agent("codex", None, "w5:p5", CODEX_SESSION)},
            "alias exact plus a wrong-provider duplicate": {"codex-main": FakeHerdr.agent("codex", "codex-main", "w3:p2", CODEX_SESSION), "w5:p5": FakeHerdr.agent("claude", None, "w5:p5", CODEX_SESSION)},
        }
        for label, agents in cases.items():
            with self.subTest(label):
                self.herdr.agents = {"claude-main": FakeHerdr.agent("claude", "claude-main", "w3:p1", CLAUDE_SESSION), **agents}
                self.herdr.targets.clear()
                self.herdr.starts.clear()
                self.herdr.responses = []
                sup = self.make_supervisor()
                for call in (lambda: sup.resolve_target("codex"), lambda: sup.ensure_agent("codex", allow_restore=False), lambda: sup.ensure_agent("codex", allow_restore=True), sup.verify_or_seed_owners):
                    with self.assertRaises(hs.SupervisorError, msg=label):
                        call()
                identity = hs.match_exact_session(list(self.herdr.agents.values()), "codex", CODEX_SESSION)
                self.assertFalse(identity.unique)
                self.assertIsNone(identity.record)
                report = sup.doctor()
                self.assertFalse(report["ok"], label)
                self.assertFalse(report["agents"]["codex"]["session_matches"], label)
                self.assertNotEqual(report["agents"]["codex"]["identity"], "exact_session_without_alias", label)
                self.assertNotEqual(report["agents"]["codex"]["identity"], "exact_session_with_alias", label)
                self.assertIsNone(report["agents"]["codex"]["target"], label)
                self.assertFalse(sup.status()["agents"]["codex"]["session_matches"], label)
                self.assertEqual(sup.resume(), 2)
                self.assertIn("refusing", self.state()["wait_user_reason"])
                self.assertEqual([t for t in self.herdr.targets if t[0] in ("prompt", "read", "wait", "send-keys")], [], label)
                self.assertEqual(self.herdr.starts, [], label)
                self.assertEqual(self.paths.owners_file.read_text(), owners_before, label)
                with self.sup.store.transaction():
                    st = self.sup.store.read_state()
                    st.update(supervisor_state="RUNNING", wait_user_reason=None, wait_user_requires_action=False, delivery=None, owner_recovery=None,
                              continuation={"kind": "plan_approved", "gate_id": "g", "plan_sha256": st["approved_plan"]["plan_sha256"], "payload_sha256": "q" * 64})
                    self.sup.store.write_state(st)
        # task start with a wrong-provider alias record and no owners file is refused too
        self.paths.owners_file.unlink()
        self.herdr.agents = {"claude-main": FakeHerdr.agent("claude", "claude-main", "w3:p1", CLAUDE_SESSION), "codex-main": FakeHerdr.agent("claude", "codex-main", "w3:p2", CODEX_SESSION)}
        with self.assertRaises(hs.SupervisorError):
            self.make_supervisor().verify_or_seed_owners()
        self.assertFalse(self.paths.owners_file.exists(), "nothing was seeded from a wrong-provider record")

    def test_exact_session_in_a_different_pane_fails_closed_everywhere(self) -> None:
        """F6: the unique provider+session record reports another pane than owners.json records. No path
        may adopt it, cache it, rewrite the locator, or issue a command; doctor/status report the conflict."""
        self.write_plan()
        self.start_gated([{"v2": ("plan", "human", "plan_approval", str(self.plan_payload()))}])
        self.approve_pending()
        owners_before = self.paths.owners_file.read_text()
        for label, agents in {
            "alias present, different pane": {"codex-main": FakeHerdr.agent("codex", "codex-main", "w9:p9", CODEX_SESSION)},
            "alias absent, different pane": {"w9:p9": FakeHerdr.agent("codex", None, "w9:p9", CODEX_SESSION)},
            "renamed, different pane, recorded pane reused by a stranger": {"codex-2": FakeHerdr.agent("codex", "codex-2", "w9:p9", CODEX_SESSION), "w3:p2": FakeHerdr.agent("codex", None, "w3:p2", OTHER)},
        }.items():
            with self.subTest(label):
                self.herdr.agents = {"claude-main": FakeHerdr.agent("claude", "claude-main", "w3:p1", CLAUDE_SESSION), **agents}
                self.herdr.available_panes = {"w3:p1"}
                self.herdr.targets.clear()
                self.herdr.starts.clear()
                self.herdr.responses = []
                sup = self.make_supervisor()
                for call in (lambda: sup.resolve_target("codex"), lambda: sup.ensure_agent("codex", allow_restore=False), lambda: sup.ensure_agent("codex", allow_restore=True),
                             sup.verify_or_seed_owners, lambda: sup.recover_agent("codex")):
                    with self.assertRaises(hs.SupervisorError, msg=label) as caught:
                        call()
                    self.assertNotIsInstance(caught.exception, herdr_runtime.MissingAgentError, f"{label}: a pane conflict is not a missing session")
                self.assertNotIn("codex", sup._live_names, f"{label}: nothing cached before refusal")
                report = sup.doctor()
                self.assertFalse(report["ok"], label)
                self.assertEqual(report["agents"]["codex"]["identity"], "pane_conflict", label)
                self.assertIsNone(report["agents"]["codex"]["target"], label)
                self.assertEqual(report["agents"]["codex"]["recorded_pane"], "w3:p2")
                self.assertTrue(any("pane conflict" in e or "refusing to target" in e for e in report["errors"]), report["errors"])
                self.assertEqual(sup.status()["agents"]["codex"]["identity"], "pane_conflict", label)
                self.assertEqual(sup.resume(), 2)
                self.assertIn("pane conflict", self.state()["wait_user_reason"])
                self.assertEqual([t for t in self.herdr.targets if t[0] in ("prompt", "read", "wait", "send-keys")], [], label)
                self.assertEqual(self.herdr.starts, [], f"{label}: no restore from a pane conflict")
                self.assertEqual(self.paths.owners_file.read_text(), owners_before, f"{label}: owners untouched")
                self.assertEqual(len(self.herdr.prompts), 1)
                with self.sup.store.transaction():
                    st = self.sup.store.read_state()
                    st.update(supervisor_state="RUNNING", wait_user_reason=None, wait_user_requires_action=False, delivery=None, owner_recovery=None,
                              continuation={"kind": "plan_approved", "gate_id": "g", "plan_sha256": st["approved_plan"]["plan_sha256"], "payload_sha256": "q" * 64})
                    self.sup.store.write_state(st)
        # the approved success path is untouched: alias absent, SAME pane
        self.herdr.agents = {"claude-main": FakeHerdr.agent("claude", "claude-main", "w3:p1", CLAUDE_SESSION), "w3:p2": FakeHerdr.agent("codex", None, "w3:p2", CODEX_SESSION)}
        sup = self.make_supervisor()
        self.assertEqual(sup.resolve_target("codex"), ("w3:p2", self.herdr.agents["w3:p2"]))
        self.assertEqual(sup.doctor()["agents"]["codex"]["identity"], "exact_session_without_alias")
        self.assertEqual(self.paths.owners_file.read_text(), owners_before)

    def test_missing_session_restore_stays_behind_the_explicit_policy(self) -> None:
        self.write_plan()
        self.start_gated([{"v2": ("plan", "human", "plan_approval", str(self.plan_payload()))}])
        del self.herdr.agents["codex-main"]
        sup = self.make_supervisor()
        with self.assertRaises(herdr_runtime.MissingAgentError):
            sup.ensure_agent("codex", allow_restore=False)
        self.assertEqual(self.herdr.starts, [])
        agent = sup.ensure_agent("codex", allow_restore=True)  # the existing explicit restore path, unchanged
        self.assertEqual(len(self.herdr.starts), 1)
        self.assertEqual(hs.session_identity(agent), CODEX_SESSION)


if __name__ == "__main__":
    unittest.main()
