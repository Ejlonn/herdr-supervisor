"""Exact-session pane-ownership recovery for nonterminal runs: the live incident (same session in two panes,
owner drift, duplicate closed), structured recovery state, bound preview/apply, refusal matrix, restart
durability, and exactly-once continuation without the task body. Fixtures only."""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys

import herdr_core  # noqa: E402
from test_telegram import TelegramCase, callback, message  # noqa: E402
from v2_fixtures import CODEX_SESSION, FakeHerdr, V2Case, hs  # noqa: E402

OTHER = "99999999-9999-4999-8999-999999999999"


class OwnerRecoveryCase(V2Case):
    """A gated run whose plan was approved; the next worker pass must deliver the plan-approved continuation
    to Codex. That is the moment the identity rule runs and the incident surfaces."""

    def to_approved(self) -> dict:
        self.write_plan()
        self.start_gated([{"v2": ("plan", "human", "plan_approval", str(self.plan_payload()))}])
        self.approve_pending()
        return self.state()

    def snapshot(self, state: dict) -> dict:
        keys = ("run_id", "task_text", "phase", "approved_plan", "runtime_policy", "codex_reset", "native_sessions", "session_preparation", "delivery", "continuation", "prompt_metrics", "gate_history", "approvals")
        return {k: json.loads(json.dumps(state.get(k))) for k in keys}

    def duplicate_codex(self, second_pane: str = "w4:p1") -> None:
        """The incident: the same exact Codex session appears in a second pane (alias-less, idle)."""
        self.herdr.agents[second_pane] = FakeHerdr.agent("codex", None, second_pane, CODEX_SESSION)

    def worker(self, responses: list | None = None) -> int:
        self.herdr.responses = list(responses or [])  # with no responses any prompt raises inside the fake
        code = self.make_supervisor().worker()
        self._worker_exited()
        return code

    def _worker_exited(self) -> None:
        """The in-process fake worker keeps this test's live pid in state; a real worker process has exited."""
        raw = json.loads(self.paths.state_file.read_text())
        if raw.get("worker_pid") is not None:
            raw["worker_pid"] = None
            self.paths.state_file.write_text(json.dumps(raw))

    def stop_response(self) -> dict:
        return {"v2": ("brief", "human", "generic_question", str(self.question_payload()))}

    def preview(self, **overrides) -> dict:
        st = self.state()
        with self.sup.store.transaction():
            live = self.sup.store.read_state()
            return self.sup.owner_recovery_preview(live, run_id=overrides.pop("run_id", st["run_id"]), recovery_id=overrides.pop("recovery_id", (st.get("owner_recovery") or {}).get("recovery_id")), **overrides)

    def apply(self, pane: str = "w4:p1", **overrides) -> dict:
        st = self.state()
        with self.sup.store.transaction():
            live = self.sup.store.read_state()
            return self.sup.owner_recovery_apply(live, run_id=overrides.pop("run_id", st["run_id"]), recovery_id=overrides.pop("recovery_id", (st.get("owner_recovery") or {}).get("recovery_id")), pane=pane, actor=overrides.pop("actor", "operator-cli"), **overrides)


class IncidentTests(OwnerRecoveryCase):
    def test_duplicate_session_stops_in_structured_wait_without_targeting_or_mutation(self) -> None:
        before = self.to_approved()
        owners_before = self.paths.owners_file.read_text()
        self.duplicate_codex()
        self.herdr.targets.clear()
        self.assertEqual(self.worker(), 2)
        state = self.state()
        recovery = state["owner_recovery"]
        self.assertEqual((state["supervisor_state"], state["wait_user_requires_action"]), ("WAIT_USER", True))
        self.assertEqual((recovery["classification"], recovery["provider"], recovery["session_id"], recovery["recorded_pane"], sorted(recovery["live_panes"])), ("duplicate", "codex", CODEX_SESSION, "w3:p2", ["w3:p2", "w4:p1"]))
        self.assertEqual(recovery["checkpoint"], {"phase": before["phase"], "active_agent": "codex", "delivery_turn_id": before["delivery"]["turn_id"], "delivery_status": "completed",
                                                  "delivery_kind": before["delivery"]["kind"], "delivery_prompt_sha256": before["delivery"]["prompt_sha256"], "continuation": before["continuation"],
                                                  "interrupted_turn_id": None, "turns_completed": before["turns_completed"], "gate_sequence": before["gate_sequence"]})
        self.assertEqual(recovery["owners_before"], json.loads(owners_before))
        self.assertEqual(self.paths.owners_file.read_text(), owners_before, "no owner drift")
        self.assertEqual([t for t in self.herdr.targets if t[0] in ("prompt", "read", "wait", "send-keys")], [], "nothing targeted")
        self.assertEqual((self.herdr.starts, self.herdr.workspaces, len(self.herdr.prompts)), ([], [], 1))
        self.assertEqual(self.snapshot(state), self.snapshot(before), "task, phase, approvals, reset counters, sessions, delivery unchanged")
        # a restart keeps the wait, still wakes nothing; plain resume and revision are refused
        self.assertEqual(self.worker(), 2)
        self.assertEqual(self.state()["owner_recovery"]["recovery_id"], recovery["recovery_id"])
        with self.sup.store.transaction():
            st = self.sup.store.read_state()
            with self.assertRaisesRegex(hs.SupervisorError, "pane-ownership repair"):
                self.sup.apply_command(st, {"action": "resume", "run_id": st["run_id"]})
            with self.assertRaisesRegex(hs.SupervisorError, "must be repaired"):
                self.sup.guide(st, run_id=st["run_id"], actor="cli", note="go on")
        self.assertEqual(len(self.herdr.prompts), 1)
        # while both panes carry the session: preview says close a duplicate first; apply is refused
        preview = self.preview()
        self.assertFalse(preview["eligible"]); self.assertIn("close the unwanted duplicate", preview["next_action"])
        with self.assertRaisesRegex(hs.SupervisorError, "repair refused"):
            self.apply("w4:p1")
        self.assertEqual(self.paths.owners_file.read_text(), owners_before)

    def test_duplicate_closed_and_session_stays_in_recorded_pane_needs_no_repair(self) -> None:
        self.to_approved()
        self.duplicate_codex()
        self.assertEqual(self.worker(), 2)
        del self.herdr.agents["w4:p1"]
        preview = self.preview()
        self.assertEqual((preview["eligible"], preview.get("in_place"), preview["new_pane"]), (False, True, "w3:p2"))
        with self.assertRaisesRegex(hs.SupervisorError, "repair refused"):
            self.apply("w3:p2")
        # the operator resumes: fresh live evidence shows the session back in its recorded pane, so the condition
        # clears without any owner change and the worker delivers the pending continuation exactly once in place
        with self.sup.store.transaction():
            st = self.sup.store.read_state()
            self.assertTrue(self.sup.apply_command(st, {"action": "resume", "run_id": st["run_id"]})["ok"])
        self.assertIsNone(self.state()["owner_recovery"])
        self.assertEqual(self.worker([self.stop_response()]), 2)
        self.assertEqual(len(self.herdr.prompts), 2)
        self.assertNotIn("Task:\n", self.herdr.prompts[-1][1])
        self.assertEqual(self.herdr.prompts[-1][0], "codex-main")
        self.assertEqual(self.paths.owners_file.read_text().count("w3:p2"), 1)

    def test_duplicate_closed_and_session_lives_elsewhere_preview_then_apply_changes_only_the_locator(self) -> None:
        before = self.to_approved()
        self.duplicate_codex()
        self.assertEqual(self.worker(), 2)
        del self.herdr.agents["codex-main"]  # the operator closed the original pane and kept w4:p1
        self.herdr.available_panes.discard("w3:p2")
        preview = self.preview()
        self.assertEqual((preview["eligible"], preview["new_pane"], preview["recorded_pane"]), (True, "w4:p1", "w3:p2"))
        owners_json_before = json.loads(self.paths.owners_file.read_text())
        result = self.apply("w4:p1", actor="telegram:424242")
        self.assertTrue(result["ok"])
        owners = json.loads(self.paths.owners_file.read_text())
        self.assertEqual(owners["codex"], {"pane_id": "w4:p1", "session_id": CODEX_SESSION})
        self.assertEqual(owners["claude"], owners_json_before["claude"])
        after = self.state()
        self.assertIsNone(after["owner_recovery"])
        self.assertEqual(after["supervisor_state"], "RUNNING")
        self.assertEqual(self.snapshot(after), self.snapshot(before))
        self.assertEqual(self.event_types().count("OWNER_PANE_REPAIRED"), 1)
        event = [e for e in self.events() if e["type"] == "OWNER_PANE_REPAIRED"][-1]["data"]
        self.assertEqual((event["old_pane"], event["new_pane"], event["session_abbrev"], event["actor"]), ("w3:p2", "w4:p1", CODEX_SESSION[:8], "telegram:424242"))
        # continuation: exactly one bounded plan-approved prompt to the repaired pane, no task body, no second write
        self.assertEqual(self.worker([self.stop_response()]), 2)
        self.assertEqual(len(self.herdr.prompts), 2)
        name, prompt = self.herdr.prompts[-1]
        self.assertEqual(name, "w4:p1")
        self.assertIn("APPROVED the plan", prompt); self.assertNotIn("Task:\n", prompt); self.assertNotIn(before["task_text"], prompt)
        self.assertEqual(json.loads(self.paths.owners_file.read_text())["codex"]["pane_id"], "w4:p1")
        # duplicate apply after success: refused, nothing changes
        with self.assertRaisesRegex(hs.SupervisorError, "no owner-recovery condition"):
            self.apply("w4:p1", recovery_id=before.get("run_id"))

    def test_accepted_turn_is_reread_after_repair_never_resubmitted(self) -> None:
        """The duplicate appears while a turn is accepted and working: after repair the worker reads that exact
        turn from the repaired pane and routes it; no replacement prompt is submitted."""
        self.write_plan()

        def die_midturn(fake, name):
            raise KeyboardInterrupt

        self.herdr.on_wait = die_midturn
        payload = str(self.plan_payload())
        self.assertEqual(self.start_gated([{"status": "working", "v2": ("plan", "human", "plan_approval", payload)}]), 3)
        turn = self.state()["delivery"]["turn_id"]
        self.herdr.on_wait = None
        self.duplicate_codex()
        self.herdr.agents["codex-main"]["agent_status"] = "working"
        self.herdr.responses = []
        self.assertEqual(self.make_supervisor().resume(), 2)
        self._worker_exited()
        recovery = self.state()["owner_recovery"]
        self.assertEqual((recovery["classification"], recovery["checkpoint"]["delivery_turn_id"], recovery["checkpoint"]["delivery_status"]), ("duplicate", turn, "uncertain"))
        # operator keeps w4:p1 (idle, settled output there), closes w3:p2
        output = self.herdr.outputs.pop("codex-main")
        del self.herdr.agents["codex-main"]; self.herdr.available_panes.discard("w3:p2")
        self.herdr.outputs["w4:p1"] = output
        self.assertTrue(self.apply("w4:p1")["ok"])
        self.assertEqual(self.state()["supervisor_state"], "RUNNING")
        prompts = len(self.herdr.prompts)
        self.assertEqual(self.worker(), 4)
        after = self.state()
        self.assertEqual((after["supervisor_state"], after["pending_gate"]["gate_type"], after["delivery"]["turn_id"], after["delivery"]["status"]), ("WAIT_PLAN_APPROVAL", "plan_approval", turn, "completed"))
        self.assertEqual(len(self.herdr.prompts), prompts, "the accepted turn was reread, not resubmitted")
        self.assertEqual([t for t in self.herdr.targets if t[0] == "read"][-1][1], "w4:p1")

    def test_moved_session_and_missing_pane_are_classified_and_recoverable(self) -> None:
        before = self.to_approved()
        record = self.herdr.agents.pop("codex-main")
        self.herdr.agents["w4:p1"] = {**record, "name": None, "pane_id": "w4:p1"}
        self.herdr.available_panes.discard("w3:p2")
        self.assertEqual(self.worker(), 2)
        self.assertEqual(self.state()["owner_recovery"]["classification"], "moved")
        self.assertEqual(self.herdr.starts, [])
        self.assertTrue(self.apply("w4:p1")["ok"])
        self.assertEqual(json.loads(self.paths.owners_file.read_text())["codex"], {"pane_id": "w4:p1", "session_id": CODEX_SESSION})
        self.assertEqual(self.snapshot(self.state()), self.snapshot(before))
        # missing pane, session not live: preview explains reopening; nothing eligible; a later reappearance elsewhere is repairable
        self.reset_fixture()
        self.to_approved()
        del self.herdr.agents["codex-main"]; self.herdr.available_panes.discard("w3:p2")
        self.assertEqual(self.worker(), 2)
        self.assertEqual(self.state()["owner_recovery"]["classification"], "missing_pane")
        preview = self.preview()
        self.assertFalse(preview["eligible"]); self.assertIn("reopen", preview["next_action"])
        self.herdr.agents["w5:p1"] = FakeHerdr.agent("codex", None, "w5:p1", CODEX_SESSION)
        self.assertTrue(self.preview()["eligible"])
        self.assertTrue(self.apply("w5:p1")["ok"])


class RefusalMatrixTests(OwnerRecoveryCase):
    def to_recovery(self) -> dict:
        self.to_approved()
        self.duplicate_codex()
        self.assertEqual(self.worker(), 2)
        del self.herdr.agents["codex-main"]; self.herdr.available_panes.discard("w3:p2")
        self.assertTrue(self.preview()["eligible"])
        return self.state()

    def test_every_mismatch_makes_no_mutation(self) -> None:
        base = self.to_recovery()
        owners_before = self.paths.owners_file.read_text()
        state_before = self.paths.state_file.read_text()

        def carrier_mutation(fn):
            return fn

        cases = {
            "wrong run": lambda: self.apply("w4:p1", run_id=OTHER),
            "stale token": lambda: self.apply("w4:p1", recovery_id=OTHER),
            "wrong pane": lambda: self.apply("w9:p9"),
            "recorded pane": lambda: self.apply("w3:p2"),
            "invalid pane": lambda: self.apply("bad pane"),
        }
        for label, action in cases.items():
            with self.subTest(label):
                with self.assertRaises(hs.SupervisorError):
                    action()
                self.assertEqual(self.paths.owners_file.read_text(), owners_before)
                self.assertEqual(self.paths.state_file.read_text(), state_before)
        live_cases = {
            "wrong provider carrier": lambda: self.herdr.agents.__setitem__("w4:p1", FakeHerdr.agent("claude", None, "w4:p1", CODEX_SESSION)),
            "wrong session": lambda: self.herdr.agents["w4:p1"].__setitem__("agent_session", {"value": OTHER}),
            "identity-less record": lambda: self.herdr.agents["w4:p1"].pop("agent_session"),
            "non-ready carrier": lambda: self.herdr.agents["w4:p1"].__setitem__("agent_status", "working"),
            "duplicate carrier": lambda: self.herdr.agents.__setitem__("w6:p1", FakeHerdr.agent("codex", None, "w6:p1", CODEX_SESSION)),
            "pane recorded for claude": lambda: self.herdr.agents["w4:p1"].__setitem__("pane_id", "w3:p1"),
        }
        for label, mutate in live_cases.items():
            with self.subTest(label):
                saved = json.loads(json.dumps(self.herdr.agents))
                mutate()
                self.assertFalse(self.preview()["eligible"], label)
                with self.assertRaises(hs.SupervisorError):
                    self.apply(self.herdr.agents.get("w4:p1", {}).get("pane_id", "w4:p1"))
                self.assertEqual(self.paths.owners_file.read_text(), owners_before)
                self.assertEqual(self.paths.state_file.read_text(), state_before)
                self.herdr.agents = saved
        # state conditions: held worker lock, pending gate, in-flight prompt, in-flight reset, unresolved follow-up, changed owners
        state_cases = {
            "in-flight reset": {"codex_reset": {**base["codex_reset"], "current_redemption_state": "RESET_RECONCILING", "current_idempotency_key": "k" * 20, "blocking_event_id": "b" * 64, "account_fingerprint": "a" * 64}},
            "quota wait": {"quota_wait": {"provider": "codex", "midturn": False, "blocking_windows": [{"kind": "five_hour", "used_percent": 100.0, "remaining_percent": 0.0, "resets_at": 10.0**9}], "resume_at": 10.0**9}},
            "not the recovery wait": {"supervisor_state": "PAUSED"},
        }
        for label, patch in state_cases.items():
            with self.subTest(label):
                with self.sup.store.transaction():
                    st = self.sup.store.read_state()
                    st.update(patch)
                    self.sup.store.write_state(st)
                with self.assertRaises(hs.SupervisorError):
                    self.apply("w4:p1")
                self.assertEqual(self.paths.owners_file.read_text(), owners_before)
                self.paths.state_file.write_text(state_before)
        # checkpoint drift (C1): a run whose phase/continuation/delivery no longer matches the recorded checkpoint
        # cannot even be loaded, so no preview/apply can run and nothing is mutated
        drift_cases = {
            "prepared delivery": {"delivery": {"turn_id": base["run_id"], "agent": "codex", "kind": "handoff", "prompt_sha256": "0" * 64, "prompt_chars": 1, "status": "prepared", "prepared_at": hs.iso_utc(0)}},
            "phase changed": {"phase": "review"},
            "continuation replaced": {"continuation": {"kind": "revision", "gate_id": None, "gate_type": "wait_user", "note": "injected"}},
            "continuation removed": {"continuation": None},
            "active agent changed": {"active_agent": "claude"},
        }
        for label, patch in drift_cases.items():
            with self.subTest(label):
                raw = json.loads(state_before); raw.update(patch)
                self.paths.state_file.write_text(json.dumps(raw))
                with self.assertRaisesRegex(hs.SupervisorError, "checkpoint no longer matches"):
                    self.make_supervisor().store.read_state()
                with self.assertRaises(hs.SupervisorError):
                    self.apply("w4:p1")
                self.assertEqual(self.paths.owners_file.read_text(), owners_before)
                self.paths.state_file.write_text(state_before)
        with self.subTest("held worker lock"):
            lock = hs.WorkerLock(self.paths.lock_file)
            lock.acquire()
            try:
                other = hs.Supervisor(self.paths, self.config, self.herdr, clock=self.clock.time)
                with other.store.transaction():
                    st = other.store.read_state()
                    st["worker_pid"] = 1  # a foreign live process id
                    other.store.write_state(st)
                with self.assertRaises(hs.SupervisorError):
                    self.apply("w4:p1")
            finally:
                lock.release()
                self.paths.state_file.write_text(state_before)
            self.assertEqual(self.paths.owners_file.read_text(), owners_before)
        with self.subTest("owners changed since the condition"):
            owners = json.loads(owners_before); owners["codex"]["pane_id"] = "w8:p8"
            hs.atomic_write_json(self.paths.owners_file, owners, mode=0o600)
            with self.assertRaisesRegex(hs.SupervisorError, "owners.json changed"):
                self.apply("w4:p1")
            self.assertEqual(json.loads(self.paths.owners_file.read_text())["codex"]["pane_id"], "w8:p8")
            self.paths.owners_file.write_text(owners_before)
        with self.subTest("unresolved follow-up"):
            # a follow-up cannot even start from this wait; a synthetic unresolved record is refused by the guard
            with self.sup.store.transaction():
                st = self.sup.store.read_state()
                with self.assertRaises(hs.SupervisorError):
                    self.sup.ask_agent(st, run_id=st["run_id"], actor="cli", question="why?")
        # after all refusals the valid apply still works exactly once
        self.assertTrue(self.apply("w4:p1")["ok"])
        with self.assertRaises(hs.SupervisorError):
            self.apply("w4:p1")

    def test_terminal_rebind_stays_separate_and_refuses_nonterminal_runs(self) -> None:
        self.to_recovery()
        with self.assertRaisesRegex(hs.SupervisorError, "nonterminal"):
            self.sup.rebind_sessions({"codex": "w4:p1"}, apply=True)
        self.assertEqual(json.loads(self.paths.owners_file.read_text())["codex"]["pane_id"], "w3:p2")

    def test_state_validation_rejects_malformed_recovery_records(self) -> None:
        good = self.to_recovery()
        def mutated(**changes):
            st = json.loads(json.dumps(good))
            for key, value in changes.items():
                if key == "supervisor_state":
                    st[key] = value
                else:
                    st["owner_recovery"][key] = value
            return st
        cases = {
            "cross run": mutated(run_id=OTHER), "other session": mutated(session_id=OTHER), "bad class": mutated(classification="lost"),
            "bad pane": mutated(recorded_pane="w3 p2"), "too many panes": mutated(live_panes=["p"] * 17), "bool time": mutated(created_at_unix=True),
            "bad checkpoint": mutated(checkpoint={"phase": "brief"}), "running with condition": mutated(supervisor_state="RUNNING"),
        }
        for label, st in cases.items():
            with self.subTest(label), self.assertRaises(hs.SupervisorError):
                hs.validate_state_v2(st)
        hs.validate_state_v2(good)


class RestartTests(OwnerRecoveryCase):
    def test_restarts_before_preview_between_preview_and_apply_after_owner_write_and_before_continuation(self) -> None:
        before = self.to_approved()
        self.duplicate_codex()
        self.assertEqual(self.worker(), 2)
        recovery_id = self.state()["owner_recovery"]["recovery_id"]
        # restart before preview: the condition and its id survive; nothing wakes
        self.assertEqual(self.worker(), 2)
        self.assertEqual(self.state()["owner_recovery"]["recovery_id"], recovery_id)
        del self.herdr.agents["codex-main"]; self.herdr.available_panes.discard("w3:p2")
        preview = self.make_supervisor().owner_recovery_preview(self.sup.store.read_state(), run_id=before["run_id"], recovery_id=recovery_id)
        self.assertTrue(preview["eligible"])
        # restart between preview and apply: a fresh process applies the same bound preview
        sup2 = self.make_supervisor()
        with sup2.store.transaction():
            st = sup2.store.read_state()
            self.assertTrue(sup2.owner_recovery_apply(st, run_id=before["run_id"], recovery_id=recovery_id, pane="w4:p1", actor="cli")["ok"])
        # crash after the owner write but before the state write: the next process sees the repaired owner and the
        # still-recorded condition; preview reports in place (no locator change needed), apply refuses harmlessly,
        # and a resume clears the condition without a second write
        self.reset_fixture()
        before = self.to_approved(); self.duplicate_codex(); self.assertEqual(self.worker(), 2)
        del self.herdr.agents["codex-main"]; self.herdr.available_panes.discard("w3:p2")
        recovery_id = self.state()["owner_recovery"]["recovery_id"]
        owners = json.loads(self.paths.owners_file.read_text()); owners["codex"]["pane_id"] = "w4:p1"
        hs.atomic_write_json(self.paths.owners_file, owners, mode=0o600)  # the write landed, the process died
        sup3 = self.make_supervisor()
        with sup3.store.transaction():
            st = sup3.store.read_state()
            preview = sup3.owner_recovery_preview(st, run_id=before["run_id"], recovery_id=recovery_id)
            self.assertEqual((preview["eligible"], preview.get("in_place")), (False, True))
            with self.assertRaises(hs.SupervisorError):
                sup3.owner_recovery_apply(st, run_id=before["run_id"], recovery_id=recovery_id, pane="w4:p1", actor="cli")
            self.assertTrue(sup3.apply_command(st, {"action": "resume", "run_id": before["run_id"]})["ok"])
        self.assertEqual(json.loads(self.paths.owners_file.read_text())["codex"]["pane_id"], "w4:p1")
        self.assertIsNone(self.state()["owner_recovery"])
        # restart before continuation: after a completed apply the worker delivers the continuation exactly once
        self.reset_fixture()
        before = self.to_approved(); self.duplicate_codex(); self.assertEqual(self.worker(), 2)
        del self.herdr.agents["codex-main"]; self.herdr.available_panes.discard("w3:p2")
        self.assertTrue(self.apply("w4:p1")["ok"])
        self.assertEqual(len(self.herdr.prompts), 1)
        self.assertEqual(self.worker([self.stop_response()]), 2)
        self.assertEqual(len(self.herdr.prompts), 2)
        self.assertEqual(self.worker(), 2)
        self.assertEqual(len(self.herdr.prompts), 2)


class AliasLossStillWorksTests(OwnerRecoveryCase):
    def test_same_pane_alias_loss_and_same_pane_restoration_are_unchanged(self) -> None:
        self.to_approved()
        record = self.herdr.agents.pop("codex-main")
        self.herdr.agents["w3:p2"] = {**record, "name": None}
        self.assertEqual(self.worker([self.stop_response()]), 2)
        self.assertIsNone(self.state()["owner_recovery"])
        self.assertEqual(self.herdr.prompts[-1][0], "w3:p2")
        self.reset_fixture()
        self.to_approved()
        del self.herdr.agents["codex-main"]  # session gone, recorded pane free: restored in place
        self.assertEqual(self.worker([self.stop_response()]), 2)
        self.assertEqual(self.herdr.starts, [("codex-main", "codex", "w3:p2", ["resume", CODEX_SESSION])])
        self.assertIsNone(self.state()["owner_recovery"])
        self.assertEqual(json.loads(self.paths.owners_file.read_text())["codex"]["pane_id"], "w3:p2")


class OwnerRecoveryTelegramTests(TelegramCase):
    def worker(self, responses: list | None = None) -> int:
        self.herdr.responses = list(responses or [])
        code = self.make_supervisor().worker()
        raw = json.loads(self.paths.state_file.read_text())
        if raw.get("worker_pid") is not None:
            raw["worker_pid"] = None
            self.paths.state_file.write_text(json.dumps(raw))
        return code

    def buttons_of(self, card: dict) -> dict[str, str]:
        return {b["text"]: b["callback_data"] for row in (card["reply_markup"] or {"inline_keyboard": []})["inline_keyboard"] for b in row}

    def to_recovery_wait(self) -> dict:
        self.write_plan()
        self.start_gated([{"v2": ("plan", "human", "plan_approval", str(self.plan_payload()))}])
        self.approve_pending()
        self.bridge.deliver_outbox()
        self.herdr.agents["w4:p1"] = FakeHerdr.agent("codex", None, "w4:p1", CODEX_SESSION)
        self.assertEqual(self.worker(), 2)
        return self.state()

    def test_wait_card_explains_incident_and_offers_only_safe_actions(self) -> None:
        state = self.to_recovery_wait()
        self.bridge.deliver_outbox()
        card = next(m for m in reversed(self.api.sent) if "Session pane ownership needs repair" in m["text"])
        self.assertIn(f"same saved Codex session ({CODEX_SESSION[:8]}) is live in more than one pane", card["text"])
        self.assertIn("No different conversation was adopted, nothing was created, and no prompt was resent", card["text"])
        self.assertIn("Close the unwanted duplicate pane(s) yourself", card["text"])
        self.assertNotIn(CODEX_SESSION, card["text"])
        buttons = self.buttons_of(card)
        self.assertEqual(set(buttons), {"Refresh repair preview", "Status", "Cancel task"})
        self.assertNotIn("Request revision", buttons); self.assertNotIn("Approve", buttons)
        record = json.loads((self.tg_paths.interactions_dir / f"{buttons['Refresh repair preview']}.json").read_text())
        self.assertEqual((record["action"], record["recovery_id"], record["run_id"]), ("owner_recovery_preview", state["owner_recovery"]["recovery_id"], state["run_id"]))
        self.bridge.handle_update(message(1, "/status"))
        self.assertIn("Session pane ownership needs repair", self.texts()[-1])
        self.assertEqual(set(self.buttons_of(self.api.sent[-1])), {"Refresh repair preview", "Status", "Cancel task"})
        self.assertEqual(self.bridge.handle_update(message(2, "/revise anything"))["result"], "enqueued")
        self.assertFalse(self.sup.process_inbox()[-1]["ok"], "a revision cannot bypass the repair")

    def test_preview_then_apply_through_bound_callbacks_and_restart_safe_inbox(self) -> None:
        state = self.to_recovery_wait()
        self.bridge.deliver_outbox()
        wait_buttons = self.buttons_of(next(m for m in reversed(self.api.sent) if "needs repair" in m["text"]))
        # duplicates still present: preview explains what to close; no apply button
        self.assertEqual(self.bridge.handle_update(callback(10, wait_buttons["Refresh repair preview"]))["result"], "enqueued")
        self.assertEqual(self.worker(), 2)
        self.bridge.deliver_outbox()
        card = next(m for m in reversed(self.api.sent) if "Pane repair not possible yet" in m["text"])
        self.assertIn("close the unwanted duplicate pane", card["text"])
        self.assertNotIn("Apply pane repair", self.buttons_of(card))
        # the operator closes the original pane; refresh offers the bound apply
        del self.herdr.agents["codex-main"]; self.herdr.available_panes.discard("w3:p2")
        self.assertEqual(self.bridge.handle_update(callback(11, self.buttons_of(card)["Refresh repair preview"]))["result"], "enqueued")
        self.assertEqual(self.worker(), 2)
        self.bridge.deliver_outbox()
        card = next(m for m in reversed(self.api.sent) if "Pane repair is possible" in m["text"])
        self.assertIn("pane w4:p1", card["text"])
        apply_token = self.buttons_of(card)["Apply pane repair"]
        record = json.loads((self.tg_paths.interactions_dir / f"{apply_token}.json").read_text())
        self.assertEqual((record["action"], record["pane"], record["recovery_id"]), ("owner_recovery_apply", "w4:p1", state["owner_recovery"]["recovery_id"]))
        self.assertEqual(self.bridge.handle_update(callback(12, apply_token, user=999))["rejected"], "wrong_user")
        self.assertEqual(self.bridge.handle_update(callback(13, apply_token, chat=777))["rejected"], "wrong_chat")
        self.assertEqual(self.bridge.handle_update(callback(14, apply_token))["result"], "enqueued")
        self.assertEqual(self.bridge.handle_update(callback(15, apply_token))["rejected"], "Already used (one-time action).")
        self.assertEqual(json.loads(self.paths.owners_file.read_text())["codex"]["pane_id"], "w3:p2", "nothing changes before the worker applies it")
        self.assertEqual(self.worker([{"v2": ("brief", "human", "generic_question", str(self.question_payload()))}]), 2)
        self.assertEqual(json.loads(self.paths.owners_file.read_text())["codex"], {"pane_id": "w4:p1", "session_id": CODEX_SESSION})
        self.assertEqual(len(self.herdr.prompts), 2)
        self.bridge.deliver_outbox()
        joined = "\n".join(self.texts())
        self.assertIn("Pane ownership repaired", joined); self.assertIn("w3:p2 → w4:p1", joined); self.assertIn("session id is unchanged", joined)
        # stale cards after the repair are inert
        self.assertIn("No pane-ownership repair is pending", self.bridge.handle_update(callback(16, self.buttons_of(card)["Refresh repair preview"]))["rejected"])
        self.assertEqual(len(self.pending_inbox()), 0)

    def test_stale_preview_card_after_a_new_condition_is_refused(self) -> None:
        state = self.to_recovery_wait()
        self.bridge.deliver_outbox()
        old = self.buttons_of(next(m for m in reversed(self.api.sent) if "needs repair" in m["text"]))
        with self.sup.store.transaction():
            st = self.sup.store.read_state()
            st["owner_recovery"]["recovery_id"] = "0bbbbbbb-cccc-4ddd-8eee-ffffffffffff"  # a newer condition replaced the old one
            self.sup.store.write_state(st)
        self.assertIn("stale", self.bridge.handle_update(callback(20, old["Refresh repair preview"]))["rejected"])
        self.assertEqual(self.pending_inbox(), [])
        self.assertEqual(state["run_id"], self.state()["run_id"])


class CorrectionRoundTests(OwnerRecoveryCase):
    """Codex review C1–C5: isolated valid-state refusals with state and owners.json proven byte-identical."""

    def to_recovery(self) -> dict:
        self.to_approved()
        self.duplicate_codex()
        self.assertEqual(self.worker(), 2)
        del self.herdr.agents["codex-main"]; self.herdr.available_panes.discard("w3:p2")
        self.assertTrue(self.preview()["eligible"])
        return self.state()

    def assert_untouched(self, owners_before: str, state_before: str) -> None:
        self.assertEqual(self.paths.owners_file.read_text(), owners_before)
        self.assertEqual(self.paths.state_file.read_text(), state_before)

    def test_pending_gate_refuses_repair_without_mutation(self) -> None:
        base = self.to_recovery()
        gate = {"gate_id": "0bbbbbbb-cccc-4ddd-8eee-ffffffffffff", "gate_type": "generic_question", "status": "pending", "run_id": base["run_id"], "turn_id": base["delivery"]["turn_id"],
                "agent": "codex", "sequence": base["gate_sequence"], "created_at": hs.iso_utc(0), "expires_at_unix": 10.0**10, "expires_at": hs.iso_utc(10.0**10), "expected_state": "WAIT_USER",
                "payload_path": "/x", "payload_sha256": "0" * 64, "artifact_path": "/x", "artifact_sha256": "0" * 64, "candidate_sha": None,
                "payload": {"question": "q", "answer_mode": "text", "max_answer_chars": 100}, "summary_fields": {}, "workflow_policy": "gated_v2"}
        with self.sup.store.transaction():
            st = self.sup.store.read_state(); st["pending_gate"] = gate; self.sup.store.write_state(st)
        owners_before, state_before = self.paths.owners_file.read_text(), self.paths.state_file.read_text()
        with self.assertRaisesRegex(hs.SupervisorError, "typed gate is pending"):
            self.preview()
        with self.assertRaisesRegex(hs.SupervisorError, "typed gate is pending"):
            self.apply("w4:p1")
        self.assert_untouched(owners_before, state_before)

    def test_unresolved_followup_refuses_repair_without_mutation(self) -> None:
        base = self.to_recovery()
        gate_id = "0bbbbbbb-cccc-4ddd-8eee-ffffffffffff"
        gate = {"gate_id": gate_id, "gate_type": "generic_question", "status": "pending", "run_id": base["run_id"], "turn_id": base["delivery"]["turn_id"],
                "agent": "codex", "sequence": base["gate_sequence"], "created_at": hs.iso_utc(0), "expires_at_unix": 10.0**10, "expires_at": hs.iso_utc(10.0**10), "expected_state": "WAIT_USER",
                "payload_path": "/x", "payload_sha256": "0" * 64, "artifact_path": "/x", "artifact_sha256": "0" * 64, "candidate_sha": None,
                "payload": {"question": "q", "answer_mode": "text", "max_answer_chars": 100}, "summary_fields": {}, "workflow_policy": "gated_v2"}
        followup = {"schema_version": 1, "run_id": base["run_id"], "followup_turn_id": "1ccccccc-cccc-4ddd-8eee-ffffffffffff", "status": "FAILED", "provider": "codex",
                    "session_id": CODEX_SESSION, "actor": "cli", "chat_id": None, "question": "why?", "response_path": "/r/x.md", "created_at_unix": 1.0, "reread_attempts": 0,
                    "delivery_uncertain": True, "decision": {"kind": "gate", "decision_id": gate_id, "gate_type": "generic_question", "supervisor_state": "WAIT_USER", "artifact_sha256": "0" * 64, "payload_sha256": "0" * 64},
                    "suspended": {"supervisor_state": "WAIT_USER", "active_agent": "codex", "wait_user_reason": None, "wait_user_requires_action": True, "delivery": None, "missing_result": None, "operator_handoff_ready": None},
                    "response": None, "reason": "no frame"}
        with self.sup.store.transaction():
            st = self.sup.store.read_state(); st["pending_gate"] = gate; st["agent_followup"] = followup; self.sup.store.write_state(st)
        owners_before, state_before = self.paths.owners_file.read_text(), self.paths.state_file.read_text()
        with self.assertRaises(hs.SupervisorError):
            self.preview()
        with self.assertRaises(hs.SupervisorError):
            self.apply("w4:p1")
        self.assert_untouched(owners_before, state_before)

    def test_foreign_held_lock_alone_refuses_apply(self) -> None:
        self.to_recovery()
        owners_before, state_before = self.paths.owners_file.read_text(), self.paths.state_file.read_text()
        self.assertIsNone(json.loads(state_before)["worker_pid"])
        holder = subprocess.Popen([sys.executable, "-c", (
            "import fcntl,os,sys,time\n"
            f"f=open({str(self.paths.lock_file)!r},'a+'); fcntl.flock(f.fileno(), fcntl.LOCK_EX); f.seek(0); f.truncate(); f.write(str(os.getpid())); f.flush()\n"
            "sys.stdout.write('held\\n'); sys.stdout.flush(); time.sleep(60)")], stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(holder.stdout.readline().strip(), "held")
            self.assertTrue(hs.WorkerLock.is_held(self.paths.lock_file))
            with self.assertRaisesRegex(hs.SupervisorError, "another worker holds the agents"):
                self.apply("w4:p1")
        finally:
            holder.terminate(); holder.wait(timeout=10); holder.stdout.close()
        self.assert_untouched(owners_before, state_before)
        self.assertTrue(self.apply("w4:p1")["ok"], "the same apply succeeds once the foreign lock is gone")

    def test_unaffected_owner_drift_invalidates_the_recovery(self) -> None:
        for label, mutate in {
            "claude pane moved live and in owners": lambda: (self.herdr.agents["claude-main"].__setitem__("pane_id", "w9:p1"),
                                                             hs.atomic_write_json(self.paths.owners_file, {**json.loads(self.paths.owners_file.read_text()), "claude": {"pane_id": "w9:p1", "session_id": "22222222-2222-4222-8222-222222222222"}}, mode=0o600)),
            "claude pane moved live only": lambda: self.herdr.agents["claude-main"].__setitem__("pane_id", "w9:p1"),
            "claude session replaced live": lambda: self.herdr.agents["claude-main"]["agent_session"].__setitem__("value", OTHER),
            "claude carrier busy": lambda: self.herdr.agents["claude-main"].__setitem__("agent_status", "working"),
            "claude carrier missing": lambda: self.herdr.agents.pop("claude-main"),
            "claude carrier duplicated": lambda: self.herdr.agents.__setitem__("dup", FakeHerdr.agent("claude", None, "w7:p7", "22222222-2222-4222-8222-222222222222")),
        }.items():
            with self.subTest(label):
                self.reset_fixture()
                self.to_recovery()
                owners_before, state_before = self.paths.owners_file.read_text(), self.paths.state_file.read_text()
                mutate()
                if "in owners" in label:
                    owners_before = self.paths.owners_file.read_text()
                preview = self.preview()
                self.assertFalse(preview["eligible"], label); self.assertIn("claude", preview["reason"])
                with self.assertRaises(hs.SupervisorError):
                    self.apply("w4:p1")
                self.assert_untouched(owners_before, state_before)

    def test_changed_condition_replaces_the_token_and_makes_old_cards_inert(self) -> None:
        self.to_approved()
        self.duplicate_codex()
        self.assertEqual(self.worker(), 2)
        first = self.state()["owner_recovery"]
        # exact re-observation (same authority + checkpoint): same id, live panes refreshed
        self.assertEqual(self.worker(), 2)
        self.assertEqual(self.state()["owner_recovery"]["recovery_id"], first["recovery_id"])
        # the condition changes (duplicate closed, original pane gone -> the session is now "moved"): new id
        del self.herdr.agents["codex-main"]; self.herdr.available_panes.discard("w3:p2")
        with self.sup.store.transaction():
            st = self.sup.store.read_state()
            self.sup.enter_owner_recovery(st, herdr_core.OwnerRecoveryNeeded("moved", provider="codex", classification="moved", session_id=CODEX_SESSION, recorded_pane="w3:p2", live_panes=["w4:p1"]))
        second = self.state()["owner_recovery"]
        self.assertNotEqual(second["recovery_id"], first["recovery_id"])
        self.assertEqual(second["classification"], "moved")
        owners_before, state_before = self.paths.owners_file.read_text(), self.paths.state_file.read_text()
        with self.assertRaisesRegex(hs.SupervisorError, "earlier condition"):
            self.apply("w4:p1", recovery_id=first["recovery_id"])
        self.assert_untouched(owners_before, state_before)
        self.assertTrue(self.apply("w4:p1", recovery_id=second["recovery_id"])["ok"])

    def test_canonical_pane_ids_everywhere(self) -> None:
        for good in ("w3:p2", "w12:p10", "w999999:p1"):
            self.assertTrue(herdr_core.valid_pane_id(good), good)
        for bad in ("../../x", "None", "", "w3:p", "w:p2", "W3:P2", "w3:p2 ", " w3:p2", "w3-p2", "w3:p2\n", "w3:p2/../w9:p9", 3, None, True, "w1234567:p1"):
            self.assertFalse(herdr_core.valid_pane_id(bad), repr(bad))
        base = self.to_recovery()
        owners_before, state_before = self.paths.owners_file.read_text(), self.paths.state_file.read_text()
        for bad in ("../../x", "None", "w4:p", "w4:p1 "):
            with self.subTest(bad=bad), self.assertRaisesRegex(hs.SupervisorError, "canonical"):
                self.apply(bad)
        self.assert_untouched(owners_before, state_before)
        # a live carrier without a canonical pane is never a repair target and is not stringified into the record
        self.herdr.agents["w4:p1"]["pane_id"] = None
        preview = self.preview()
        self.assertFalse(preview["eligible"]); self.assertEqual(preview["live_panes"], [])
        self.assert_untouched(owners_before, state_before)
        # persisted non-canonical panes never load
        raw = json.loads(state_before); raw["owner_recovery"]["recorded_pane"] = "../../x"
        self.paths.state_file.write_text(json.dumps(raw))
        with self.assertRaisesRegex(hs.SupervisorError, "canonical"):
            self.make_supervisor().store.read_state()
        raw = json.loads(state_before); raw["owner_recovery"]["live_panes"] = ["w4:p1", "None"]
        self.paths.state_file.write_text(json.dumps(raw))
        with self.assertRaises(hs.SupervisorError):
            self.make_supervisor().store.read_state()
        raw = json.loads(state_before); raw["owner_recovery"]["owners_before"]["claude"]["pane_id"] = "w3 p1"
        self.paths.state_file.write_text(json.dumps(raw))
        with self.assertRaises(hs.SupervisorError):
            self.make_supervisor().store.read_state()
        self.paths.state_file.write_text(state_before)
        # CLI input is checked before any state access or lock
        env = {k: os.environ.get(k) for k in ("HERDR_SUPERVISOR_CONFIG", "HERDR_SUPERVISOR_STATE_DIR")}
        os.environ["HERDR_SUPERVISOR_CONFIG"] = str(self.paths.config_file); os.environ["HERDR_SUPERVISOR_STATE_DIR"] = str(self.paths.state_dir)
        hs.atomic_write_json(self.paths.config_file, {"schema_version": 1, "project_root": str(self.project_root), "quota_dir": str(self.quota_dir), "review_root": str(self.review_root), "product_repo": str(self.product_repo), "herdr_bin": "/bin/sh"})
        err = io.StringIO()
        try:
            with contextlib.redirect_stderr(err):
                code = hs.main(["repair-owner-pane", "--run-id", base["run_id"], "--recovery-id", base["owner_recovery"]["recovery_id"], "--pane", "../x", "--apply"])
        finally:
            for k, v in env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        self.assertEqual(code, 2); self.assertIn("canonical", err.getvalue())
        self.assert_untouched(owners_before, state_before)
