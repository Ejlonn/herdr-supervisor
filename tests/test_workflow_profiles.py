"""General workflow profiles (`supervised_v2` vs the strict `gated_v2`): contract-driven routing,
optional plan gates, generalized plan-artifact naming, either-provider start, and the Telegram
workflow-choice card. Focused tests for the approved plan's eleven-item validation list; the full
suite (test_v2_core.py, test_telegram.py, test_modules.py, ...) proves gated_v2 and existing fixtures
are otherwise byte-for-byte unchanged."""

from __future__ import annotations

import json

import herdr_present as hp
from test_telegram import TelegramCase, callback, message  # noqa: E402  (shares the Telegram fixture)
from v2_fixtures import V2Case, hs

RUN = "20598ca2-d049-4cb9-967a-5652e7bf6eff"
TURN = "9945228c-bb87-493a-96ea-14d190c49510"


class ContractRoutingTests(V2Case):
    def test_1_supervised_v2_accepts_done_before_any_plan_exists(self) -> None:
        """The bug the plan names: a complete read-only report needs no plan to finish."""
        code = self.start_supervised([{"v2": ("investigate", "done")}])
        self.assertEqual(code, 0, "DONE is a terminal state")
        state = self.state()
        self.assertEqual(state["supervisor_state"], "DONE")
        self.assertIsNone(state["approved_plan"])
        self.assertEqual(state["workflow_policy"], "supervised_v2")
        self.assertEqual(state["workflow_contract"], hs.resolve_workflow_contract("supervised_v2"))

    def test_2_same_result_rejected_under_gated_v2_with_the_current_message(self) -> None:
        code = self.start_gated([{"v2": ("investigate", "done")}])
        self.assertEqual(code, 2, "an ordinary WAIT_USER, not a typed gate")
        state = self.state()
        self.assertEqual(state["supervisor_state"], "WAIT_USER")
        self.assertIn("done is not allowed before a plan approval exists", state["wait_user_reason"])
        self.assertTrue(state["wait_user_requires_action"])

    def test_3_declared_runtime_and_push_obligations_still_block_done_under_supervised_v2(self) -> None:
        """A voluntary plan gate that declares runtime/push requirements binds `done` exactly as gated_v2."""
        self.write_plan()
        code = self.start_supervised([{"v2": ("plan", "human", "plan_approval", str(self.plan_payload()))}], task="Add the widget")
        self.assertEqual(code, 4)
        self.assertEqual(self.state()["supervisor_state"], "WAIT_PLAN_APPROVAL")
        self.approve_pending()
        code = self.resume_with([{"v2": ("implement", "done")}])
        self.assertEqual(code, 2)
        state = self.state()
        self.assertEqual(state["supervisor_state"], "WAIT_USER")
        self.assertIn("runtime validation is required", state["wait_user_reason"])
        self.assertEqual(state["operator_handoff_ready"]["unmet"], ["runtime_validation_pass", "push_approval"])

    def test_4_general_plan_gate_accepts_any_safe_markdown_name_legacy_still_requires_codex_plan(self) -> None:
        named = self.review_dir / "PROPOSAL.md"
        named.write_text("# Proposal\n\nDo the thing.\n")
        payload = self.plan_payload(plan_path=str(named), runtime_validation_required=False, push_approval_required=False)
        code = self.start_supervised([{"v2": ("plan", "human", "plan_approval", str(payload))}], task="Do the thing")
        self.assertEqual(code, 4)
        self.assertEqual(self.state()["supervisor_state"], "WAIT_PLAN_APPROVAL")
        self.assertEqual(self.state()["pending_gate"]["artifact_path"], str(named))
        self.reset_fixture()
        self.write_plan()
        named = self.review_dir / "PROPOSAL.md"
        named.write_text("# Proposal\n\nDo the thing.\n")
        payload = self.plan_payload(plan_path=str(named))
        block = {"v2": ("plan", "human", "plan_approval", str(payload))}
        # A rejected plan_approval gate is repairable: the first bad submission gets one bound repair
        # continuation to the same session before a hard stop; both attempts use the same wrong filename.
        code = self.start_gated([block, block])
        self.assertEqual(code, 2)
        state = self.state()
        self.assertEqual(state["supervisor_state"], "WAIT_USER")
        self.assertIn("plan_approval artifact must be CODEX_PLAN.md", state["wait_user_reason"])
        self.assertEqual([n for n, _ in self.herdr.prompts], ["codex-main", "codex-main"])

    def test_4b_general_plan_gate_rejects_a_non_markdown_artifact(self) -> None:
        named = self.review_dir / "PROPOSAL.txt"
        named.write_text("Do the thing.\n")
        payload = self.plan_payload(plan_path=str(named))
        block = {"v2": ("plan", "human", "plan_approval", str(payload))}
        code = self.start_supervised([block, block], task="Do the thing")
        self.assertEqual(code, 2)
        state = self.state()
        self.assertEqual(state["supervisor_state"], "WAIT_USER")
        self.assertIn("must be a Markdown (.md) file", state["wait_user_reason"])

    def test_5_approval_continuation_does_not_mandate_a_brief_or_force_claude_under_supervised_v2(self) -> None:
        self.write_plan()
        code = self.start_supervised([{"v2": ("plan", "human", "plan_approval", str(self.plan_payload(runtime_validation_required=False, push_approval_required=False)))}], task="Add the widget")
        self.assertEqual(code, 4)
        self.approve_pending()
        code = self.resume_with([{"v2": ("plan", "done")}])  # codex finishes directly; Claude never appears
        self.assertEqual(code, 0)
        names = [n for n, _ in self.herdr.prompts]
        self.assertNotIn("claude-main", names)
        continuation_prompt = self.herdr.prompts[-1][1]
        self.assertNotIn("CODEX_BRIEF.md", continuation_prompt)
        self.assertIn("the human APPROVED the plan", continuation_prompt)
        self.assertIn("persisted workflow contract", continuation_prompt)
        self.assertEqual(self.state()["supervisor_state"], "DONE")

    def test_5b_gated_v2_approval_continuation_still_mandates_the_brief(self) -> None:
        self.write_plan()
        self.assertEqual(self.start_gated([{"v2": ("plan", "human", "plan_approval", str(self.plan_payload()))}]), 4)
        self.approve_pending()
        self.resume_with([{"v2": ("brief", "claude")}, {"v2": ("implement", "human", "generic_question", str(self.question_payload()))}])
        continuation_prompt = self.herdr.prompts[1][1]
        self.assertIn("CODEX_BRIEF.md", continuation_prompt)
        self.assertIn("route to the implementation agent", continuation_prompt)
        self.assertEqual([n for n, _ in self.herdr.prompts], ["codex-main", "codex-main", "claude-main"])

    def test_6_either_provider_may_start_supervised_v2_only_codex_may_start_gated_v2(self) -> None:
        with self.assertRaises(hs.SupervisorError):
            self.sup.run_new("t", "claude", workflow_policy="gated_v2")
        self.reset_fixture()
        code = self.start_supervised([{"v2": ("investigate", "done")}], start="claude")
        self.assertEqual(code, 0)
        state = self.state()
        self.assertEqual(state["start_agent"], "claude")
        self.assertEqual([n for n, _ in self.herdr.prompts], ["claude-main"])

    def test_9_gated_v2_fixtures_and_migrated_v1_state_remain_valid_without_migration(self) -> None:
        self.write_plan()
        state = self.start_gated([{"v2": ("plan", "human", "plan_approval", str(self.plan_payload()))}])
        self.assertEqual(state, 4)
        self.assertEqual(self.state()["workflow_contract"], hs.resolve_workflow_contract("gated_v2"))
        migrated = hs.migrate_state_v1({"schema_version": 1, "supervisor_state": "DONE", "task_text": "x"})
        self.assertEqual(migrated["workflow_policy"], "v1")
        self.assertIsNone(migrated["workflow_contract"])

    def test_10_prompt_echo_is_safe_and_workspace_specific_wording_stays_out_of_the_general_policy(self) -> None:
        for policy in ("gated_v2", "supervised_v2"):
            state = {"run_id": RUN, "task_text": "t", "active_agent": "codex", "workflow_policy": policy,
                     "workflow_contract": hs.resolve_workflow_contract(policy)}
            prompt = self.sup.build_prompt(state, TURN, "initial")
            self.assertIn("HERDR_PROTOCOL=2", prompt)
            self.assertEqual(hs.find_protocol_blocks(prompt), [], policy)
            if policy == "supervised_v2":
                self.assertNotIn("CODEX_PLAN.md", prompt)
                self.assertNotIn("CODEX_BRIEF.md", prompt)


class CorrectionTests(V2Case):
    """CODEX_REVIEW.md F1–F3: historical schema-2 compatibility, the universal typed-gate boundary for
    human routes, and preserving the plan gate's originating agent through approval."""

    def test_f1_historical_schema2_gated_v2_state_missing_the_field_loads_writes_and_restarts_unchanged(self) -> None:
        self.write_plan()
        self.start_gated([{"v2": ("plan", "human", "plan_approval", str(self.plan_payload()))}])
        original = self.state()
        self.assertEqual(original["workflow_contract"], hs.resolve_workflow_contract("gated_v2"))
        # Simulate a state file written before this change: schema 2, workflow_policy present,
        # workflow_contract entirely absent (not merely None) — the exact shape a real installed
        # gated_v2 run would have on disk.
        raw = dict(original)
        del raw["workflow_contract"]
        hs.atomic_write_json(self.paths.state_file, raw, mode=0o600)
        loaded = self.sup.store.read_state()
        self.assertEqual(loaded["workflow_policy"], "gated_v2", "the recorded policy is never reinterpreted")
        self.assertEqual(loaded["workflow_contract"], hs.resolve_workflow_contract("gated_v2"))
        self.assertEqual(loaded["run_id"], original["run_id"])
        self.assertEqual(loaded["pending_gate"]["gate_id"], original["pending_gate"]["gate_id"])
        with self.sup.store.transaction():
            state = self.sup.store.read_state()
            self.assertEqual(state["workflow_contract"], hs.resolve_workflow_contract("gated_v2"))
            self.sup.store.write_state(state)  # must not raise
        # restart: a fresh read again resolves the same contract and identity, still gated_v2
        restarted = self.sup.store.read_state()
        self.assertEqual(restarted["workflow_policy"], "gated_v2")
        self.assertEqual(restarted["workflow_contract"], hs.resolve_workflow_contract("gated_v2"))
        self.assertEqual(restarted["run_id"], original["run_id"])
        self.assertNotEqual(restarted["workflow_contract"], hs.resolve_workflow_contract("supervised_v2"))

    def test_f1_historical_schema2_supervised_v2_state_missing_the_field_resolves_its_own_policy(self) -> None:
        self.start_supervised([{"v2": ("investigate", "done")}])
        original = self.state()
        raw = dict(original)
        del raw["workflow_contract"]
        hs.atomic_write_json(self.paths.state_file, raw, mode=0o600)
        loaded = self.sup.store.read_state()
        self.assertEqual(loaded["workflow_policy"], "supervised_v2")
        self.assertEqual(loaded["workflow_contract"], hs.resolve_workflow_contract("supervised_v2"))

    def test_f2_untyped_human_route_is_rejected_under_both_v2_policies_with_no_extra_prompt(self) -> None:
        for start_policy in (("gated_v2", self.start_gated), ("supervised_v2", self.start_supervised)):
            policy, starter = start_policy
            with self.subTest(policy=policy):
                self.reset_fixture()
                code = starter([{"v2": ("stage", "human")}])  # gate="none", payload="-" by default
                self.assertEqual(code, 2)
                state = self.state()
                self.assertEqual(state["supervisor_state"], "WAIT_USER")
                self.assertIn("requires a typed HERDR_GATE and HERDR_PAYLOAD", state["wait_user_reason"])
                self.assertTrue(state["wait_user_requires_action"])
                self.assertEqual(len(self.herdr.prompts), 1, "no extra prompt is sent for the rejection itself")

    def test_f2_a_valid_general_question_is_accepted_under_supervised_v2(self) -> None:
        code = self.start_supervised([{"v2": ("investigate", "human", "generic_question", str(self.question_payload()))}])
        self.assertEqual(code, 2, "generic_question maps to WAIT_USER, not a GATE_STATE")
        state = self.state()
        self.assertEqual(state["supervisor_state"], "WAIT_USER")
        self.assertEqual(state["pending_gate"]["gate_type"], "generic_question")
        self.assertEqual(state["pending_gate"]["status"], "pending")

    def test_f2_legacy_v1_task_keeps_its_free_text_human_route_unaffected(self) -> None:
        """v1 is outside the V2 contract family entirely; F2 must not touch its historical behavior."""
        self.herdr.responses = [{"v1": "human"}]
        code = self.sup.run_new("Add the widget", "codex", workflow_policy="v1")
        self.assertEqual(code, 2)
        self.assertEqual(self.state()["supervisor_state"], "WAIT_USER")

    def test_f3_claude_started_general_task_approval_returns_to_the_same_claude_session(self) -> None:
        self.write_plan()
        code = self.start_supervised(
            [{"v2": ("plan", "human", "plan_approval", str(self.plan_payload(runtime_validation_required=False, push_approval_required=False)))}],
            start="claude", task="Add the widget",
        )
        self.assertEqual(code, 4)
        state = self.state()
        self.assertEqual(state["start_agent"], "claude")
        self.assertEqual(state["pending_gate"]["agent"], "claude")
        self.approve_pending()
        self.assertEqual(self.state()["active_agent"], "claude", "F3: approval must not force codex")
        code = self.resume_with([{"v2": ("plan", "done")}])
        self.assertEqual(code, 0)
        self.assertEqual([n for n, _ in self.herdr.prompts], ["claude-main", "claude-main"], "the exact same Claude session, once")

    def test_f3_gated_v2_approval_still_returns_to_codex_byte_for_byte(self) -> None:
        self.write_plan()
        self.assertEqual(self.start_gated([{"v2": ("plan", "human", "plan_approval", str(self.plan_payload()))}]), 4)
        self.assertEqual(self.state()["pending_gate"]["agent"], "codex")
        self.approve_pending()
        self.assertEqual(self.state()["active_agent"], "codex")


class WorkflowChoiceTelegramTests(TelegramCase):
    def test_7_choice_is_visible_before_start_survives_callback_and_restart_and_reaches_the_run(self) -> None:
        self.bridge.handle_update(message(1, "/task investigate the reboot"))
        card = self.api.sent[-1]
        self.assertIn("Workflow: Supervised", card["text"])
        buttons = {b["text"]: b["callback_data"] for row in card["reply_markup"]["inline_keyboard"] for b in row}
        self.assertIn("✓ Workflow: Supervised", buttons)
        self.assertIn("Workflow: Plan approval", buttons)
        result = self.bridge.handle_update(callback(2, buttons["Workflow: Plan approval"]))
        self.assertEqual(result["result"], "selection_updated")
        pending_path = next(self.paths.pending_starts_dir.glob("*.json"))
        self.assertEqual(json.loads(pending_path.read_text())["workflow_policy"], "gated_v2")
        confirm = self.api.sent[-1]
        self.assertIn("Workflow: Plan approval", confirm["text"])
        confirm_buttons = {b["text"]: b["callback_data"] for row in confirm["reply_markup"]["inline_keyboard"] for b in row}
        self.assertIn("✓ Workflow: Plan approval", confirm_buttons)
        zero = next(b["callback_data"] for row in confirm["reply_markup"]["inline_keyboard"] for b in row if b["text"] == "Start task · reset budget 0")
        # a restarted bridge instance (independent process) reads the same durable pending-start record
        fresh_bridge = self.make_bridge()
        started = fresh_bridge.handle_update(callback(3, zero))
        self.assertEqual(started["result"], "enqueued")
        self.assertEqual(self.pending_inbox()[0]["workflow_policy"], "gated_v2")
        self.write_plan()
        self.herdr.responses = [{"v2": ("plan", "human", "plan_approval", str(self.plan_payload()))}]
        self.assertEqual(self.sup.worker(), 4)
        self.assertEqual(self.state()["workflow_policy"], "gated_v2")

    def test_8_a_stale_workflow_choice_callback_is_rejected_without_starting_or_changing_the_record(self) -> None:
        self.bridge.handle_update(message(1, "/task investigate"))
        card = self.api.sent[-1]
        buttons = {b["text"]: b["callback_data"] for row in card["reply_markup"]["inline_keyboard"] for b in row}
        stale_token = buttons["Workflow: Plan approval"]
        pending_path = next(self.paths.pending_starts_dir.glob("*.json"))
        before = json.loads(pending_path.read_text())
        # advance the pending record's view revision through a legitimate later action (session policy)
        self.assertEqual(self.bridge.handle_update(callback(2, buttons["Fresh Codex"]))["result"], "selection_updated")
        # the older card's Workflow token is now bound to a superseded view revision
        result = self.bridge.handle_update(callback(3, stale_token))
        self.assertEqual(result["rejected"], "stale_pending_start")
        after = json.loads(pending_path.read_text())
        self.assertEqual(after["workflow_policy"], before["workflow_policy"])  # unchanged by the rejected callback
        self.assertEqual(self.pending_inbox(), [])

    def test_8b_a_tampered_policy_value_is_rejected_without_starting(self) -> None:
        self.bridge.handle_update(message(1, "/task investigate"))
        card = self.api.sent[-1]
        token = next(b["callback_data"] for row in card["reply_markup"]["inline_keyboard"] for b in row if b["text"] == "Workflow: Plan approval")
        record_path = self.tg_paths.interactions_dir / f"{token}.json"
        record = json.loads(record_path.read_text())
        record["policy"] = "not-a-real-policy"
        hs.atomic_write_json(record_path, record)
        result = self.bridge.handle_update(callback(2, token))
        self.assertEqual(result["rejected"], "invalid_workflow_policy")
        self.assertEqual(self.pending_inbox(), [])
        self.assertFalse(self.paths.state_file.exists())

    def test_workflow_choice_binds_the_final_start_button_rejecting_a_choice_made_after_it_was_minted(self) -> None:
        """The reset-budget Start button itself is bound to the workflow choice shown when it was minted;
        changing the choice afterward makes the old Start button stale, not silently applied to a new policy."""
        self.bridge.handle_update(message(1, "/task investigate"))
        card = self.api.sent[-1]
        buttons = {b["text"]: b["callback_data"] for row in card["reply_markup"]["inline_keyboard"] for b in row}
        old_start = buttons["Start · 0"]
        self.assertEqual(self.bridge.handle_update(callback(2, buttons["Workflow: Plan approval"]))["result"], "selection_updated")
        result = self.bridge.handle_update(callback(3, old_start))
        self.assertEqual(result["rejected"], "task_binding_changed")
        self.assertEqual(self.pending_inbox(), [])


class StartAgentTelegramTests(TelegramCase):
    """CODEX_REVIEW.md F4: Telegram start-agent choice under supervised_v2 — restart, staleness,
    tampering, both providers, gated_v2 normalization, and refusal of invalid input."""

    def test_choice_is_offered_only_under_supervised_v2_binds_through_start_and_survives_restart(self) -> None:
        self.bridge.handle_update(message(1, "/task investigate the reboot"))
        card = self.api.sent[-1]
        buttons = {b["text"]: b["callback_data"] for row in card["reply_markup"]["inline_keyboard"] for b in row}
        self.assertIn("✓ Start with: Codex", buttons)
        self.assertIn("Start with: Claude", buttons)
        result = self.bridge.handle_update(callback(2, buttons["Start with: Claude"]))
        self.assertEqual(result["result"], "selection_updated")
        pending_path = next(self.paths.pending_starts_dir.glob("*.json"))
        self.assertEqual(json.loads(pending_path.read_text())["starting_agent"], "claude")
        confirm = self.api.sent[-1]
        self.assertIn("Start agent: Claude", confirm["text"])
        confirm_buttons = {b["text"]: b["callback_data"] for row in confirm["reply_markup"]["inline_keyboard"] for b in row}
        self.assertIn("✓ Start with: Claude", confirm_buttons)
        zero = next(b["callback_data"] for row in confirm["reply_markup"]["inline_keyboard"] for b in row if b["text"] == "Start task · reset budget 0")
        fresh_bridge = self.make_bridge()  # a restarted bridge instance reads the same durable record
        started = fresh_bridge.handle_update(callback(3, zero))
        self.assertEqual(started["result"], "enqueued")
        self.assertEqual(self.pending_inbox()[0]["starting_agent"], "claude")
        self.write_plan()
        self.herdr.responses = [{"v2": ("investigate", "done")}]
        self.assertEqual(self.sup.worker(), 0)
        state = self.state()
        self.assertEqual(state["start_agent"], "claude")
        self.assertEqual([n for n, _ in self.herdr.prompts], ["claude-main"])

    def test_both_providers_can_be_selected_and_reach_the_started_run(self) -> None:
        for agent, expected_name in (("codex", "codex-main"), ("claude", "claude-main")):
            with self.subTest(agent=agent):
                self.reset_fixture(); self.bridge = self.make_bridge()
                self.bridge.handle_update(message(1, "/task investigate"))
                card = self.api.sent[-1]
                buttons = {b["text"]: b["callback_data"] for row in card["reply_markup"]["inline_keyboard"] for b in row}
                if agent == "claude":
                    self.assertEqual(self.bridge.handle_update(callback(2, buttons["Start with: Claude"]))["result"], "selection_updated")
                    card = self.api.sent[-1]
                    buttons = {b["text"]: b["callback_data"] for row in card["reply_markup"]["inline_keyboard"] for b in row}
                    start_button = buttons["Start task · reset budget 0"]
                else:
                    start_button = buttons["Start · 0"]
                self.assertEqual(self.bridge.handle_update(callback(3, start_button))["result"], "enqueued")
                self.herdr.responses = [{"v2": ("investigate", "done")}]
                self.assertEqual(self.sup.worker(), 0)
                self.assertEqual(self.state()["start_agent"], agent)
                self.assertEqual([n for n, _ in self.herdr.prompts], [expected_name])

    def test_gated_v2_normalizes_to_codex_and_the_choice_disappears(self) -> None:
        self.bridge.handle_update(message(1, "/task investigate"))
        card = self.api.sent[-1]
        buttons = {b["text"]: b["callback_data"] for row in card["reply_markup"]["inline_keyboard"] for b in row}
        self.assertEqual(self.bridge.handle_update(callback(2, buttons["Start with: Claude"]))["result"], "selection_updated")
        pending_path = next(self.paths.pending_starts_dir.glob("*.json"))
        self.assertEqual(json.loads(pending_path.read_text())["starting_agent"], "claude")
        confirm = self.api.sent[-1]
        buttons = {b["text"]: b["callback_data"] for row in confirm["reply_markup"]["inline_keyboard"] for b in row}
        self.assertEqual(self.bridge.handle_update(callback(3, buttons["Workflow: Plan approval"]))["result"], "selection_updated")
        # F4: switching to gated_v2 normalizes the recorded start agent back to codex...
        self.assertEqual(json.loads(pending_path.read_text())["starting_agent"], "codex")
        normalized = self.api.sent[-1]
        self.assertNotIn("Start with:", normalized["text"])
        row_texts = [b["text"] for row in normalized["reply_markup"]["inline_keyboard"] for b in row]
        self.assertFalse(any(t.startswith("Start with:") for t in row_texts), "...and the choice is removed from the card")

    def test_start_agent_choice_rejected_while_policy_is_gated_v2_no_task_created(self) -> None:
        self.bridge.handle_update(message(1, "/task investigate"))
        card = self.api.sent[-1]
        buttons = {b["text"]: b["callback_data"] for row in card["reply_markup"]["inline_keyboard"] for b in row}
        self.assertEqual(self.bridge.handle_update(callback(2, buttons["Workflow: Plan approval"]))["result"], "selection_updated")
        confirm = self.api.sent[-1]
        self.assertFalse(any(b["text"].startswith("Start with:") for row in confirm["reply_markup"]["inline_keyboard"] for b in row))
        pending_id = json.loads(next(self.paths.pending_starts_dir.glob("*.json")).read_text())["pending_id"]
        # Adversarial: mint a start_agent token by hand against the record while it is gated_v2 (the UI
        # never offers this button under gated_v2; simulate a forged/duplicated one anyway).
        pseudo = {"event_id": f"reset-auth:{pending_id}", "run_id": pending_id, "supervisor_state": "PENDING_START"}
        token = self.bridge.new_interaction(action="starting_agent", event=pseudo, gate=None, chat_id=self.tg_config["chat_id"],
                                            extra={"pending_id": pending_id, "agent": "claude", "task_sha256": confirm["text"] and json.loads(next(self.paths.pending_starts_dir.glob("*.json")).read_text())["task_sha256"], "view_revision": 2})
        result = self.bridge.handle_update(callback(3, token))
        self.assertEqual(result["rejected"], "starting_agent_not_selectable")
        self.assertEqual(json.loads(next(self.paths.pending_starts_dir.glob("*.json")).read_text())["starting_agent"], "codex")
        self.assertEqual(self.pending_inbox(), [])
        self.assertFalse(self.paths.state_file.exists())

    def test_invalid_start_agent_value_is_rejected_no_task_created(self) -> None:
        self.bridge.handle_update(message(1, "/task investigate"))
        pending_id = json.loads(next(self.paths.pending_starts_dir.glob("*.json")).read_text())["pending_id"]
        task_sha = json.loads(next(self.paths.pending_starts_dir.glob("*.json")).read_text())["task_sha256"]
        pseudo = {"event_id": f"reset-auth:{pending_id}", "run_id": pending_id, "supervisor_state": "PENDING_START"}
        token = self.bridge.new_interaction(action="starting_agent", event=pseudo, gate=None, chat_id=self.tg_config["chat_id"],
                                            extra={"pending_id": pending_id, "agent": "root", "task_sha256": task_sha, "view_revision": 1})
        result = self.bridge.handle_update(callback(2, token))
        self.assertEqual(result["rejected"], "invalid_starting_agent")
        self.assertEqual(json.loads(next(self.paths.pending_starts_dir.glob("*.json")).read_text())["starting_agent"], "codex")
        self.assertEqual(self.pending_inbox(), [])

    def test_stale_start_agent_callback_is_rejected_and_does_not_change_the_record(self) -> None:
        self.bridge.handle_update(message(1, "/task investigate"))
        card = self.api.sent[-1]
        buttons = {b["text"]: b["callback_data"] for row in card["reply_markup"]["inline_keyboard"] for b in row}
        stale_token = buttons["Start with: Claude"]
        pending_path = next(self.paths.pending_starts_dir.glob("*.json"))
        # advance the revision through a legitimate later action
        self.assertEqual(self.bridge.handle_update(callback(2, buttons["Fresh Codex"]))["result"], "selection_updated")
        result = self.bridge.handle_update(callback(3, stale_token))
        self.assertEqual(result["rejected"], "stale_pending_start")
        self.assertEqual(json.loads(pending_path.read_text())["starting_agent"], "codex")
        self.assertEqual(self.pending_inbox(), [])

    def test_tampered_start_agent_binding_on_the_final_start_button_is_rejected(self) -> None:
        """The Start button was minted while start_agent=claude; the record is then changed back to codex
        (e.g. by a concurrent workflow-policy switch to gated_v2) before the Start button is tapped."""
        self.bridge.handle_update(message(1, "/task investigate"))
        card = self.api.sent[-1]
        buttons = {b["text"]: b["callback_data"] for row in card["reply_markup"]["inline_keyboard"] for b in row}
        self.assertEqual(self.bridge.handle_update(callback(2, buttons["Start with: Claude"]))["result"], "selection_updated")
        confirm = self.api.sent[-1]
        confirm_buttons = {b["text"]: b["callback_data"] for row in confirm["reply_markup"]["inline_keyboard"] for b in row}
        claude_start = confirm_buttons["Start task · reset budget 0"]
        self.assertEqual(self.bridge.handle_update(callback(3, confirm_buttons["Workflow: Plan approval"]))["result"], "selection_updated")
        result = self.bridge.handle_update(callback(4, claude_start))
        self.assertEqual(result["rejected"], "task_binding_changed")
        self.assertEqual(self.pending_inbox(), [])
        self.assertFalse(self.paths.state_file.exists())


class WorkflowMessageTests(TelegramCase):
    def test_general_start_message_does_not_promise_codex_planning(self):
        text = hp.render_task_started({"type": "TASK_STARTED", "data": {"task": "Read report", "start": "claude", "policy": "supervised_v2"}}, "UTC").html
        self.assertIn("Starting agent: Claude", text)
        self.assertNotIn("Codex will plan first", text)
        strict = hp.render_task_started({"data": {"start": "codex", "policy": "gated_v2"}}, "UTC").html
        self.assertIn("Codex will plan first", strict)

    def test_approval_and_upload_feedback_do_not_override_selected_workflow(self):
        text = hp.render_event({"type": "PLAN_APPROVED", "data": {}}, {}, None, "UTC").html
        self.assertIn("approved plan", text)
        self.assertNotIn("Codex writes the brief", text)
        preview = self.bridge.render_upload_preview({"display_filename": "task.md"})
        self.assertIn("confirm Start on that card", preview)
        self.assertNotIn("gated task", preview)
