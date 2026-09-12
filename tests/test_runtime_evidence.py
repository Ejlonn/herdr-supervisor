"""Runtime evidence ownership: collaborative proposals versus operator-confirmed records, automatic mode
only under an approved plan, exact bindings, stale/duplicate/unauthorized refusals, legacy pending evidence
as an unconfirmed proposal, and the status/doctor/Telegram presentation. Fixtures only."""

from __future__ import annotations

import json

from test_telegram import TelegramCase, callback, message  # noqa: E402
from v2_fixtures import NOW, SHA_A, SHA_B, V2Case, hs  # noqa: E402


class RuntimeEvidenceCase(V2Case):
    def to_runtime_gate(self, *, mode: str | None = None, push: bool = True) -> dict:
        self.write_plan()
        overrides = {"push_approval_required": push}
        if mode is not None:
            overrides["runtime_validation_mode"] = mode
        self.start_gated([{"v2": ("plan", "human", "plan_approval", str(self.plan_payload(**overrides)))}])
        self.approve_pending()
        self.resume_with([{"v2": ("brief", "claude")}, {"v2": ("implement", "codex")}, {"v2": ("review", "human", "runtime_validation", str(self.runtime_payload()))}])
        state = self.state()
        self.assertEqual(state["supervisor_state"], "WAIT_RUNTIME_VALIDATION")
        return state

    def propose(self, result: str, *, actor: str = "codex-runtime-review", evidence=None) -> dict:
        return self.record_runtime(result, evidence=evidence, actor=actor, confirm=False)

    def confirm(self, decision: str, *, gate_id=None, evidence_sha256=None, run_id=None, actor="operator-cli", chat_id=None, **extra) -> dict:
        st = self.state()
        proposal = st.get("runtime_proposal") or {}
        with self.sup.store.transaction():
            live = self.sup.store.read_state()
            return self.sup.confirm_runtime_evidence(live, run_id=run_id or st["run_id"], gate_id=gate_id or proposal.get("gate_id"),
                                                     evidence_sha256=evidence_sha256 or proposal.get("evidence_sha256"), decision=decision, actor=actor, chat_id=chat_id,
                                                     head_resolver=self.head_resolver, **extra)


class CollaborativeOwnershipTests(RuntimeEvidenceCase):
    def test_default_mode_is_collaborative_and_agent_submissions_only_propose(self) -> None:
        before = self.to_runtime_gate()
        self.assertEqual(before["runtime_policy"]["validation_mode"], "operator_collaborative")
        for result in ("PASS", "FAIL"):
            with self.subTest(result):
                outcome = self.propose(result, actor="telegram:9999999")  # an actor label claiming to be the operator changes nothing
                self.assertTrue(outcome["proposed"])
                state = self.state()
                self.assertIsNone(state["runtime_evidence"])
                self.assertEqual(state["supervisor_state"], "WAIT_RUNTIME_VALIDATION")
                self.assertEqual(state["pending_gate"]["gate_id"], before["pending_gate"]["gate_id"])
                self.assertEqual(state["pending_gate"]["status"], "pending")
                self.assertEqual((state["runtime_proposal"]["status"], state["runtime_proposal"]["result"], state["runtime_proposal"]["legacy"]), ("proposed", result, False))
                self.assertNotIn("RUNTIME_VALIDATION_PASSED", self.event_types()); self.assertNotIn("RUNTIME_VALIDATION_FAILED", self.event_types()); self.assertNotIn("PUSH_APPROVAL_REQUIRED", self.event_types())
        self.assertEqual(self.event_types().count("RUNTIME_EVIDENCE_PROPOSED"), 2)
        self.assertEqual(self.state()["runtime_proposal"]["result"], "FAIL", "the newer proposal supersedes the older one")
        # a re-proposal of identical bytes is idempotent (no third event)
        self.propose("FAIL")
        self.assertEqual(self.event_types().count("RUNTIME_EVIDENCE_PROPOSED"), 2)
        # done / resume / push are impossible from a proposal
        self.assertFalse(self.sup.operator_handoff_eligibility(self.state(), inspect_agents=False)[0])
        self.assertEqual(self.make_supervisor().worker(), 4)

    def test_operator_confirmation_records_pass_with_provenance_and_advances(self) -> None:
        before = self.to_runtime_gate()
        proposed = self.propose("PASS")
        result = self.confirm("PASS", actor="telegram:424242", chat_id=424242)
        self.assertTrue(result["ok"])
        state = self.state()
        evidence = state["runtime_evidence"]
        self.assertEqual((evidence["result"], evidence["candidate_sha"], evidence["evidence_sha256"]), ("PASS", SHA_A, proposed["evidence_sha256"]))
        self.assertEqual(evidence["provenance"]["mode"], "operator_collaborative")
        self.assertEqual((evidence["provenance"]["actor"], evidence["provenance"]["chat_id"], evidence["provenance"]["proposal_sha256"]), ("telegram:424242", 424242, proposed["evidence_sha256"]))
        self.assertEqual(state["runtime_proposal"]["status"], "accepted")
        self.assertEqual(state["supervisor_state"], "WAIT_PUSH_APPROVAL")
        self.assertEqual(state["gate_history"][-1]["gate_id"], before["pending_gate"]["gate_id"])
        self.assertEqual(self.event_types().count("RUNTIME_VALIDATION_PASSED"), 1)
        # a second confirmation of the same proposal is refused (already recorded)
        with self.assertRaisesRegex(hs.SupervisorError, "nothing to confirm"):
            self.confirm("PASS", gate_id=before["pending_gate"]["gate_id"], evidence_sha256=proposed["evidence_sha256"])
        self.assertTrue(self.sup._runtime_pass_current(self.state()))

    def test_confirmed_fail_keeps_waiting_with_actionable_status(self) -> None:
        self.to_runtime_gate()
        self.propose("FAIL")
        view = self.sup.runtime_validation_view(self.state())
        self.assertEqual((view["state"], view["result"], view["next_actor"]), ("proposed", "FAIL", "operator"))
        self.assertIn("nothing is recorded until you confirm", view["waiting"])
        self.confirm("FAIL")
        state = self.state()
        self.assertEqual(state["supervisor_state"], "WAIT_RUNTIME_VALIDATION")
        self.assertEqual(state["runtime_evidence"]["provenance"]["mode"], "operator_collaborative")
        self.assertEqual(self.event_types().count("RUNTIME_VALIDATION_FAILED"), 1)
        view = self.sup.runtime_validation_view(state)
        self.assertEqual((view["state"], view["result"], view["next_actor"]), ("accepted", "FAIL", "agent"))
        self.assertIn("must fix and propose new evidence", view["waiting"])
        self.assertFalse(self.sup._runtime_pass_current(state))
        # a new PASS proposal after the accepted FAIL is possible; PASS becomes current only when confirmed
        self.propose("PASS")
        self.assertEqual(self.state()["runtime_evidence"]["result"], "FAIL")
        self.confirm("PASS")
        self.assertEqual(self.state()["supervisor_state"], "WAIT_PUSH_APPROVAL")

    def test_confirmation_bindings_and_stale_or_wrong_decisions_fail_closed(self) -> None:
        before = self.to_runtime_gate()
        proposed = self.propose("PASS")
        other = "0bbbbbbb-cccc-4ddd-8eee-ffffffffffff"
        cases = {
            "wrong run": {"run_id": other}, "wrong gate": {"gate_id": other}, "wrong hash": {"evidence_sha256": "e" * 64},
            "relabel FAIL": {"decision": "FAIL"}, "wrong candidate": {"candidate_sha": SHA_B}, "wrong environment": {"environment": "PROD"},
            "bool chat": {"chat_id": True},
        }
        for label, overrides in cases.items():
            with self.subTest(label):
                decision = overrides.pop("decision", "PASS")
                with self.assertRaises(hs.SupervisorError):
                    self.confirm(decision, **overrides)
                self.assertIsNone(self.state()["runtime_evidence"])
                self.assertEqual(self.state()["runtime_proposal"]["status"], "proposed")
        # the evidence file changed after the proposal (same path): the bound hash no longer matches
        self.evidence_file(SHA_A, "PASS", commands=[{"command": "x", "result": "PASS (edited)"}])
        with self.assertRaisesRegex(hs.SupervisorError, "changed since it was proposed|does not match the proposed"):
            self.confirm("PASS", evidence_sha256=proposed["evidence_sha256"])
        self.assertIsNone(self.state()["runtime_evidence"])
        # HEAD moved: confirmation is refused like any exact-SHA action
        self.evidence_file(SHA_A, "PASS")
        fresh = self.propose("PASS")
        self.head = SHA_B
        with self.assertRaisesRegex(hs.SupervisorError, "no longer equals"):
            self.confirm("PASS", evidence_sha256=fresh["evidence_sha256"])
        self.head = SHA_A
        # after the gate is revised the proposal is stale
        with self.sup.store.transaction():
            st = self.sup.store.read_state()
            self.sup.revise_gate(st, run_id=st["run_id"], gate_id=before["pending_gate"]["gate_id"], actor="cli", note="fix it")
        with self.assertRaises(hs.SupervisorError):
            self.confirm("PASS", gate_id=before["pending_gate"]["gate_id"], evidence_sha256=fresh["evidence_sha256"])

    def test_proposal_survives_restart_and_inbox_confirmation_is_idempotent(self) -> None:
        self.to_runtime_gate()
        proposed = self.propose("PASS")
        self.assertEqual(self.make_supervisor().worker(), 4)
        state = self.state()
        self.assertEqual(state["runtime_proposal"]["status"], "proposed")
        command = {"request_id": "confirm-0001", "action": "runtime_confirm", "run_id": state["run_id"], "gate_id": state["pending_gate"]["gate_id"], "evidence_sha256": proposed["evidence_sha256"],
                   "decision": "PASS", "candidate_sha": SHA_A, "environment": "TEST", "actor": "telegram:424242", "chat_id": 424242}
        self.sup.enqueue_command(command)
        self.sup.enqueue_command(command)  # replay
        self.sup.enqueue_command({**command, "request_id": "confirm-0002"})  # fresh duplicate
        self.assertEqual(self.sup.worker(), 4)
        self.assertEqual(self.state()["supervisor_state"], "WAIT_PUSH_APPROVAL")
        results = [e["data"] for e in self.events() if e["type"] == "COMMAND_RESULT" and e["data"]["action"] == "runtime_confirm"]
        self.assertEqual([r["ok"] for r in results], [True, False])
        self.assertEqual(self.event_types().count("RUNTIME_VALIDATION_PASSED"), 1)
        self.assertEqual(self.event_types().count("PUSH_APPROVAL_REQUIRED"), 1)


class AutomaticModeTests(RuntimeEvidenceCase):
    def test_automatic_mode_records_directly_only_when_the_approved_plan_declares_it(self) -> None:
        state = self.to_runtime_gate(mode="automatic_agent")
        self.assertEqual(state["runtime_policy"]["validation_mode"], "automatic_agent")
        result = self.record_runtime("PASS", actor="codex", confirm=False)
        self.assertFalse(result.get("proposed"))
        after = self.state()
        self.assertEqual(after["supervisor_state"], "WAIT_PUSH_APPROVAL")
        self.assertEqual(after["runtime_evidence"]["provenance"], {"mode": "automatic_agent", "actor": "codex", "chat_id": None, "at_unix": after["runtime_evidence"]["provenance"]["at_unix"]})
        self.assertIsNone(after["runtime_proposal"])

    def test_runtime_payload_cannot_escalate_to_automatic(self) -> None:
        self.write_plan()
        self.start_gated([{"v2": ("plan", "human", "plan_approval", str(self.plan_payload()))}])
        self.approve_pending()
        payload = self.runtime_payload(validation_mode="automatic_agent")  # the agent declares automatic; the approved plan did not
        self.resume_with([{"v2": ("brief", "claude")}, {"v2": ("implement", "codex")}, {"v2": ("review", "human", "runtime_validation", str(payload))}])
        self.assertEqual(self.state()["supervisor_state"], "WAIT_RUNTIME_VALIDATION")
        self.assertEqual(self.sup.runtime_validation_mode(self.state()), "operator_collaborative")
        self.assertTrue(self.record_runtime("PASS", confirm=False)["proposed"])
        self.assertIsNone(self.state()["runtime_evidence"])
        with self.assertRaises(hs.SupervisorError):
            hs.validate_gate_payload(json.loads(self.runtime_payload(validation_mode="whatever").read_text()), "runtime_validation", config=self.config)
        with self.assertRaises(hs.SupervisorError):
            hs.validate_gate_payload(json.loads(self.plan_payload(runtime_validation_mode="automatic").read_text()), "plan_approval", config=self.config)


class LegacyEvidenceTests(RuntimeEvidenceCase):
    def legacy_pending_fail(self) -> dict:
        """A run recorded before provenance existed: FAIL evidence on the still-pending gate."""
        self.to_runtime_gate()
        evidence = self.evidence_file(SHA_A, "FAIL")
        with self.sup.store.transaction():
            st = self.sup.store.read_state()
            st["runtime_evidence"] = {"schema_version": 1, "candidate_sha": SHA_A, "environment": "TEST", "result": "FAIL", "commands": json.loads(evidence.read_text())["commands"],
                                      "timestamp": hs.iso_utc(NOW - 60), "evidence_path": str(evidence), "evidence_sha256": hs.sha256_file(evidence), "received_at": hs.iso_utc(NOW),
                                      "gate_id": st["pending_gate"]["gate_id"], "actor": "codex-runtime-review"}
            st.pop("runtime_proposal", None)
            self.sup.store.write_state(st)
        return self.state()

    def test_legacy_pending_evidence_is_an_unconfirmed_proposal_without_rewriting_history(self) -> None:
        self.legacy_pending_fail()
        raw_before = self.paths.state_file.read_text()
        loaded = self.make_supervisor().store.read_state()
        self.assertIsNone(loaded["runtime_proposal"], "the default is filled in memory; nothing is manufactured on disk")
        view = self.sup.runtime_validation_view(loaded)
        self.assertEqual((view["state"], view["result"], view["legacy"], view["next_actor"]), ("proposed", "FAIL", True, "operator"))
        self.assertFalse(self.sup.evidence_accepted(loaded, loaded["runtime_evidence"]))
        self.assertEqual(self.paths.state_file.read_text(), raw_before)
        status = self.sup.status()
        self.assertEqual(status["runtime_validation"]["state"], "proposed")
        self.assertEqual(status["runtime_proposal"]["legacy"], True)
        # the operator can confirm the legacy proposal (bound to the same bytes); that records provenance
        proposal = status["runtime_proposal"]
        self.confirm("FAIL", gate_id=proposal["gate_id"], evidence_sha256=proposal["evidence_sha256"])
        after = self.state()
        self.assertEqual(after["runtime_evidence"]["provenance"]["mode"], "operator_collaborative")
        self.assertEqual(after["runtime_proposal"]["status"], "accepted")
        # completed history stays history: a PASS whose gate already closed keeps counting without provenance
        self.reset_fixture()
        self.to_runtime_gate(push=False)
        self.record_runtime("PASS")
        with self.sup.store.transaction():
            st = self.sup.store.read_state()
            st["runtime_evidence"].pop("provenance")
            st["runtime_proposal"] = None
            self.sup.store.write_state(st)
        loaded = self.sup.store.read_state()
        self.assertTrue(self.sup.evidence_accepted(loaded, loaded["runtime_evidence"]))
        self.assertTrue(self.sup._runtime_pass_current(loaded))
        self.assertEqual(self.sup.runtime_validation_view(loaded)["state"], "accepted")


class RuntimeEvidenceTelegramTests(TelegramCase):
    def to_proposal(self, result: str = "PASS") -> tuple[dict, dict]:
        self.write_plan()
        self.start_gated([{"v2": ("plan", "human", "plan_approval", str(self.plan_payload()))}])
        self.approve_pending()
        self.resume_with([{"v2": ("brief", "claude")}, {"v2": ("implement", "codex")}, {"v2": ("review", "human", "runtime_validation", str(self.runtime_payload()))}])
        self.bridge.deliver_outbox()
        proposed = self.record_runtime(result, actor="codex-runtime-review", confirm=False)
        return self.state(), proposed

    def buttons_of(self, card: dict) -> dict[str, str]:
        return {b["text"]: b["callback_data"] for row in (card["reply_markup"] or {"inline_keyboard": []})["inline_keyboard"] for b in row}

    def test_runtime_gate_card_explains_collaborative_ownership(self) -> None:
        self.write_plan()
        self.start_gated([{"v2": ("plan", "human", "plan_approval", str(self.plan_payload()))}])
        self.approve_pending()
        self.resume_with([{"v2": ("brief", "claude")}, {"v2": ("implement", "codex")}, {"v2": ("review", "human", "runtime_validation", str(self.runtime_payload()))}])
        self.bridge.deliver_outbox()
        card = next(m for m in reversed(self.api.sent) if "Runtime validation required" in m["text"])
        self.assertIn("the agent runs the checks and proposes evidence; you record PASS or FAIL", card["text"])
        self.assertNotIn("Telegram cannot record it", card["text"])
        self.bridge.handle_update(message(1, "/status"))
        self.assertIn("runtime not run", self.texts()[-1])
        self.assertIn("no runtime attempt has been recorded yet", self.texts()[-1])

    def test_proposal_card_offers_only_the_bound_record_action_and_confirmation_is_asynchronous(self) -> None:
        state, proposed = self.to_proposal("PASS")
        self.bridge.deliver_outbox()
        card = next(m for m in reversed(self.api.sent) if "Runtime PASS proposed" in m["text"])
        self.assertIn("your confirmation is needed", card["text"])
        self.assertIn("Nothing is recorded yet", card["text"])
        self.assertIn("proposed by codex-runtime-review", card["text"])
        buttons = self.buttons_of(card)
        self.assertEqual(set(buttons), {"Record PASS", "Request Revision", "Ask agent", "Status"})
        record = json.loads((self.tg_paths.interactions_dir / f"{buttons['Record PASS']}.json").read_text())
        self.assertEqual((record["action"], record["gate_id"], record["evidence_sha256"], record["decision"], record["candidate_sha"], record["environment"]),
                         ("runtime_confirm", state["pending_gate"]["gate_id"], proposed["evidence_sha256"], "PASS", SHA_A, "TEST"))
        self.bridge.handle_update(message(2, "/status"))
        self.assertIn("PASS proposed (unconfirmed)", self.texts()[-1])
        self.assertIn("nothing is recorded until you confirm", self.texts()[-1])
        self.assertNotIn("PASS accepted", self.texts()[-1])
        # wrong user / chat, then the owner: enqueued, nothing recorded until the worker runs
        self.assertEqual(self.bridge.handle_update(callback(3, buttons["Record PASS"], user=999))["rejected"], "wrong_user")
        result = self.bridge.handle_update(callback(4, buttons["Record PASS"]))
        self.assertEqual(result["result"], "enqueued")
        self.assertEqual(self.bridge.handle_update(callback(5, buttons["Record PASS"]))["rejected"], "Already used (one-time action).")
        queued = self.pending_inbox()[0]
        self.assertEqual((queued["action"], queued["decision"], queued["evidence_sha256"], queued["actor"]), ("runtime_confirm", "PASS", proposed["evidence_sha256"], f"telegram:{424242}"))
        self.bridge.deliver_outbox()
        self.assertIsNone(self.state()["runtime_evidence"])
        self.assertNotIn("Runtime validation passed", "\n".join(self.texts()))
        self.assertEqual(self.sup.worker(), 4)
        after = self.state()
        self.assertEqual(after["supervisor_state"], "WAIT_PUSH_APPROVAL")
        self.assertEqual(after["runtime_evidence"]["provenance"]["actor"], "telegram:424242")
        self.bridge.deliver_outbox()
        joined = "\n".join(self.texts())
        self.assertIn("Runtime result confirmation accepted", joined)
        self.assertIn("Runtime validation passed", joined)
        self.assertIn("recorded by telegram:424242", joined)
        self.assertIn("Push stage ready for approval", joined)

    def test_stale_replaced_or_cross_run_proposal_buttons_are_inert(self) -> None:
        state, proposed = self.to_proposal("FAIL")
        self.bridge.deliver_outbox()
        first = self.buttons_of(next(m for m in reversed(self.api.sent) if "Runtime FAIL proposed" in m["text"]))
        self.assertIn("Record FAIL", first); self.assertNotIn("Record PASS", first)
        # a newer proposal replaces the older: the old button is refused before any command exists
        self.evidence_file(SHA_A, "PASS")
        self.record_runtime("PASS", confirm=False)
        self.assertIn("replaced", self.bridge.handle_update(callback(10, first["Record FAIL"]))["rejected"])
        self.assertEqual(self.pending_inbox(), [])
        self.bridge.deliver_outbox()
        second = self.buttons_of(next(m for m in reversed(self.api.sent) if "Runtime PASS proposed" in m["text"]))
        # the gate was revised meanwhile: the proposal is no longer pending
        with self.sup.store.transaction():
            st = self.sup.store.read_state()
            self.sup.revise_gate(st, run_id=st["run_id"], gate_id=state["pending_gate"]["gate_id"], actor="cli", note="again")
        self.assertIn("No runtime proposal is pending", self.bridge.handle_update(callback(11, second["Record PASS"]))["rejected"])
        self.assertEqual(self.pending_inbox(), [])

    def test_doctor_reports_runtime_state_repo_validity_and_fresh_codex_components(self) -> None:
        self.to_proposal("FAIL")
        report = self.sup.doctor()
        self.assertEqual((report["runtime_validation"]["state"], report["runtime_validation"]["result"], report["runtime_validation"]["next_actor"]), ("proposed", "FAIL", "operator"))
        self.assertEqual((report["product_repo"]["valid"], report["product_repo"]["head"]), (True, SHA_A))
        self.assertTrue(report["session_start"]["fresh_codex_available"])
        self.assertEqual(set(report["session_start"]["fresh_codex_components"]), {"herdr_pane_split", "herdr_agent_start", "herdr_identity_observation", "codex_app_server", "codex_thread_start", "codex_resume_session_id"})
        def broken(repo):
            raise hs.SupervisorError("cannot read repository HEAD")
        self.sup.head_resolver = broken
        report = self.sup.doctor()
        self.assertFalse(report["product_repo"]["valid"])
        self.assertTrue(any("HEAD is unavailable" in w for w in report["warnings"]))
