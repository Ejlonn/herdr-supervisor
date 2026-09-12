"""Telegram bridge: auth/modes, commands, opaque callbacks, offset/journal idempotence, outbox delivery,
backoff, 409/webhook fail-closed, no listener, redaction, secrecy, doctor categories."""

from __future__ import annotations

import json
import os
import socket
import ssl
import urllib.error
from pathlib import Path

import herdr_present as hp  # noqa: E402
import herdr_query  # noqa: E402
import herdr_telegram as ht  # noqa: E402
import telegram_api as tg  # noqa: E402
from v2_fixtures import FAKE_TOKEN, NOW, FakeHerdr, V2Case, hs

OWNER = 424242
CHAT = 424242
BOT_ID = 123456789


class FakeApi:
    """Scripted Bot API. Records every outbound call; can inject failures per method."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.updates_batches: list[list[dict]] = []
        self.fail: dict[str, list] = {}
        self.webhook_url = ""
        self.sent: list[dict] = []
        self.documents: list[dict] = []
        self.message_id = 100

    def _maybe_fail(self, method: str) -> None:
        queue = self.fail.get(method)
        if queue:
            error = queue.pop(0)
            if error is not None:
                raise error

    def get_me(self) -> dict:
        self._maybe_fail("getMe")
        return {"id": BOT_ID, "username": "herdr_test_bot"}

    def get_webhook_info(self) -> dict:
        self._maybe_fail("getWebhookInfo")
        return {"url": self.webhook_url}

    def get_updates(self, *, offset: int, timeout_seconds: int, limit: int = 50) -> list[dict]:
        self.calls.append(("getUpdates", {"offset": offset, "timeout": timeout_seconds}))
        self._maybe_fail("getUpdates")
        if not self.updates_batches:
            return []
        return [u for u in self.updates_batches.pop(0) if u.get("update_id", -1) >= offset]

    def send_message(self, chat_id: int, text: str, *, reply_markup=None, parse_mode=None) -> dict:
        self.calls.append(("sendMessage", {"chat_id": chat_id, "text": text, "reply_markup": reply_markup, "parse_mode": parse_mode}))
        self._maybe_fail("sendMessage")
        self.message_id += 1
        self.sent.append({"chat_id": chat_id, "text": text, "reply_markup": reply_markup, "message_id": self.message_id, "parse_mode": parse_mode})
        return {"message_id": self.message_id}

    def send_document(self, chat_id: int, filename: str, data: bytes, *, caption=None, mime="text/markdown") -> dict:
        self.calls.append(("sendDocument", {"chat_id": chat_id, "filename": filename, "bytes": len(data), "caption": caption}))
        self._maybe_fail("sendDocument")
        self.message_id += 1
        self.documents.append({"chat_id": chat_id, "filename": filename, "data": bytes(data), "caption": caption, "message_id": self.message_id})
        return {"message_id": self.message_id, "document": {"file_id": f"doc{self.message_id}", "file_name": filename}}

    def answer_callback_query(self, callback_query_id: str, text: str) -> None:
        self.calls.append(("answerCallbackQuery", {"id": callback_query_id, "text": text}))

    def edit_reply_markup(self, chat_id: int, message_id: int) -> None:
        self.calls.append(("editMessageReplyMarkup", {"chat_id": chat_id, "message_id": message_id}))


def message(update_id: int, text: str, *, user=OWNER, chat=CHAT, chat_type="private") -> dict:
    return {"update_id": update_id, "message": {"message_id": update_id, "from": {"id": user, "is_bot": False, "first_name": "B"}, "chat": {"id": chat, "type": chat_type}, "date": 1, "text": text}}


def callback(update_id: int, data: str, *, user=OWNER, chat=CHAT, chat_type="private") -> dict:
    return {"update_id": update_id, "callback_query": {"id": f"cb{update_id}", "from": {"id": user, "is_bot": False}, "data": data, "message": {"message_id": 5, "chat": {"id": chat, "type": chat_type}}}}


class TelegramCase(V2Case):
    def setUp(self) -> None:
        super().setUp()
        root = Path(self.tmp.name)
        # The bot-identity lock (F8) lives under $HOME; keep every test inside the temporary root.
        self._home = os.environ.get("HOME")
        os.environ["HOME"] = self.tmp.name
        self.tg_paths = ht.TelegramPaths(config_file=root / "tg" / "config.json", state_dir=root / "tg-state")
        self.tg_paths.config_file.parent.mkdir(mode=0o700)
        token_file = root / "tg" / "bot-token"
        token_file.write_text(FAKE_TOKEN + "\n")
        os.chmod(token_file, 0o600)
        self.tg_config = {**ht.DEFAULT_TG_CONFIG, "mode": "actionable", "owner_user_id": OWNER, "chat_id": CHAT, "token_file": str(token_file), "backoff_base_seconds": 1.0, "backoff_cap_seconds": 8.0}
        hs.atomic_write_json(self.tg_paths.config_file, self.tg_config, mode=0o600)
        self.api = FakeApi()
        self.bridge = self.make_bridge()

    def tearDown(self) -> None:
        if self._home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self._home
        super().tearDown()

    def make_bridge(self, **overrides) -> ht.Bridge:
        config = {**self.tg_config, **overrides}
        return ht.Bridge(sup_paths=self.paths, sup_config=self.config, tg_paths=self.tg_paths, tg_config=config, api=self.api, bot_id=BOT_ID, status_reader=self.sup.status, clock=self.clock.time, sleeper=self.clock.sleep, rng=lambda: 0.0)

    def texts(self) -> list[str]:
        return [m["text"] for m in self.api.sent]

    def pending_inbox(self) -> list[dict]:
        pending = self.paths.inbox_dir / "pending"
        return [json.loads(p.read_text()) for p in sorted(pending.glob("*.json"))] if pending.exists() else []

    def plan_gate(self) -> dict:
        self.write_plan()
        self.start_gated([{"v2": ("plan", "human", "plan_approval", str(self.plan_payload()))}])
        return self.state()


class AuthAndModeTests(TelegramCase):
    def test_wrong_user_group_and_wrong_chat_are_rejected_without_details(self) -> None:
        self.plan_gate()
        for update in (message(1, "/status", user=999), message(2, "/status", chat_type="group"), message(3, "/status", chat=777)):
            result = self.bridge.handle_update(update)
            self.assertIn("rejected", result)
        self.assertEqual(self.api.sent, [], "unknown users/chats receive nothing")
        # callbacks from the wrong user are refused too
        result = self.bridge.handle_update(callback(4, "x" * 20, user=999))
        self.assertEqual(result["rejected"], "wrong_user")

    def test_unconfigured_and_shadow_modes(self) -> None:
        bridge = self.make_bridge(mode="unconfigured", owner_user_id=None, chat_id=None)
        self.assertIn("rejected", bridge.handle_update(message(1, "/status")))
        shadow = self.make_bridge(mode="shadow")
        shadow.handle_update(message(2, "/task build the thing"))
        self.assertIn("SHADOW", self.texts()[-1])
        self.assertEqual(self.pending_inbox(), [], "shadow mode enqueues nothing")
        shadow.handle_update(message(3, "/status"))
        self.assertIn("No active task", self.texts()[-1])
        self.assertFalse(self.paths.state_file.exists())

    def test_unknown_command_and_extra_args_produce_help_not_task(self) -> None:
        self.bridge.handle_update(message(1, "/deploy now please"))
        self.assertIn("remote control", self.texts()[-1])
        self.bridge.handle_update(message(2, "/status extra words"))
        self.assertIn("No active task", self.texts()[-1])
        self.assertEqual(self.pending_inbox(), [])


class CommandTests(TelegramCase):
    def test_task_enqueues_bounded_durable_file_and_rejects_while_active(self) -> None:
        hostile = "Fix $(rm -rf /) `x` ; echo\n--start claude"
        result = self.bridge.handle_update(message(1, "/task " + hostile))
        self.assertEqual(result["result"], "reset_budget_required")
        zero = next(p.stem for p in self.tg_paths.interactions_dir.glob("*.json") if (lambda r: r.get("action") == "reset_budget" and r.get("budget") == 0)(json.loads(p.read_text())))
        self.assertEqual(self.bridge.handle_update(callback(4, zero))["result"], "enqueued")
        pending = self.pending_inbox()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["task_text"], hostile)
        self.assertEqual(pending[0]["action"], "task")
        path = next((self.paths.inbox_dir / "pending").glob(f"*-{pending[0]['request_id']}.json"))
        self.assertEqual(oct(path.stat().st_mode & 0o777), "0o600")
        self.assertRegex(pending[0]["request_id"], r"^[0-9a-f]{32}$")
        # the worker starts it with codex under gated_v2 without any Telegram involvement
        self.write_plan()
        self.herdr.responses = [{"v2": ("plan", "human", "plan_approval", str(self.plan_payload()))}]
        self.assertEqual(self.sup.worker(), 4)
        self.assertEqual(self.state()["workflow_policy"], "gated_v2")
        self.bridge.handle_update(message(2, "/task second"))
        self.assertIn("the supervisor has no task queue", self.texts()[-1])
        self.assertEqual(len(self.pending_inbox()), 0)
        self.bridge.handle_update(message(3, "/task " + "x" * 5000))
        self.assertIn("exceeds", self.texts()[-1])

    def test_status_pending_plan_logs_doctor_render_safe_fields(self) -> None:
        self.plan_gate()
        for command in ("/status", "/pending", "/plan", "/logs", "/doctor", "/help"):
            self.bridge.handle_update(message(len(self.api.sent) + 1, command))
        joined = "\n".join(self.texts())
        self.assertIn("Plan awaiting your approval", joined)
        self.assertIn("Plan ready for approval", joined)
        self.assertIn("Plan fingerprint", joined)
        self.assertIn("PLAN_APPROVAL_REQUIRED", joined)
        self.assertNotIn("# CODEX_PLAN", joined, "no raw artifact dump in summaries")
        self.assertNotIn(FAKE_TOKEN, joined)
        # /plan details sends the validated plan as a registered Markdown document, not as chat text
        self.bridge.handle_update(message(50, "/plan details"))
        self.assertEqual(self.api.documents[-1]["data"].decode().splitlines()[0], "# CODEX_PLAN")
        self.assertNotIn("# CODEX_PLAN", "\n".join(self.texts()))

    def test_plan_details_refuse_when_redaction_hides_material_and_disables_remote_approval(self) -> None:
        self.write_plan("# CODEX_PLAN\n\napi_key=supersecretvalue123\n")
        self.start_gated([{"v2": ("plan", "human", "plan_approval", str(self.plan_payload()))}])
        self.bridge.deliver_outbox()
        approve_tokens = [p for p in self.tg_paths.interactions_dir.glob("*.json") if json.loads(p.read_text())["action"] == "approve"]
        self.assertEqual(len(approve_tokens), 1)
        self.bridge.handle_update(message(9, "/plan details"))
        self.assertIn("cannot be displayed safely", self.texts()[-1])
        self.assertNotIn("supersecretvalue123", "\n".join(self.texts()))
        self.assertTrue(json.loads(approve_tokens[0].read_text())["superseded"])
        token = approve_tokens[0].stem
        result = self.bridge.handle_update(callback(10, token))
        self.assertIn("rejected", result)
        self.assertEqual(self.pending_inbox(), [])

    def test_pause_resume_cancel_enqueue_control_commands(self) -> None:
        self.plan_gate()
        for index, command in enumerate(("/pause", "/resume", "/cancel"), start=1):
            self.bridge.handle_update(message(index, command))
        self.assertEqual([c["action"] for c in self.pending_inbox()], ["pause", "resume", "cancel"])  # FIFO
        results = self.sup.process_inbox()
        self.assertEqual([r["ok"] for r in results], [True, True, True])
        self.assertEqual(self.state()["supervisor_state"], "CANCELLED")
        history = [json.loads(p.read_text())["supervisor_state"] for p in sorted((self.paths.outbox_dir / "events").glob("*.json"))]
        self.assertIn("WAIT_PLAN_APPROVAL", history, "resume after pause returned to the pending gate, not RUNNING")
        self.assertIn("preserved", self.state()["cancel_note"])
        self.assertEqual(self.herdr.sent_keys, [])

    def test_revise_and_answer_paths_including_reply_intent(self) -> None:
        self.plan_gate()
        self.bridge.handle_update(message(1, "/revise"))
        self.assertIn("Reply with the revision note", self.texts()[-1])
        self.bridge.handle_update(message(2, "please split the plan"))
        pending = self.pending_inbox()
        self.assertEqual(pending[-1]["action"], "revise")
        self.assertEqual(pending[-1]["note"], "please split the plan")
        self.bridge.handle_update(message(3, "another plain message"))
        self.assertIn("Plain text is disabled", self.texts()[-1])
        self.assertEqual(len(self.pending_inbox()), 1, "intent is one-time")
        self.bridge.handle_update(message(4, "/revise direct note"))
        self.assertEqual(self.pending_inbox()[-1]["note"], "direct note")
        self.bridge.handle_update(message(5, "/answer enum"))
        self.assertIn("No pending question", self.texts()[-1])

    def test_action_required_wait_user_accepts_only_authenticated_run_bound_revision(self) -> None:
        self.plan_gate()
        with self.sup.store.transaction():
            state = self.sup.store.read_state()
            state["pending_gate"]["status"] = "rejected"
            state["supervisor_state"] = "WAIT_USER"
            state["wait_user_requires_action"] = True
            state["wait_user_reason"] = "implementation guidance required"
            self.sup.store.write_state(state)
        run_id = self.state()["run_id"]
        self.assertIn("rejected", self.bridge.handle_update(message(80, "/revise ignored", user=999)))
        self.assertEqual(self.pending_inbox(), [])
        self.assertEqual(self.bridge.handle_update(message(81, "/revise continue from the current files"))["result"], "enqueued")
        self.bridge.handle_update(message(81, "/revise continue from the current files"))
        pending = self.pending_inbox()
        self.assertEqual(len(pending), 1, "duplicate update maps to one durable command")
        self.assertEqual((pending[0]["run_id"], pending[0]["gate_id"], pending[0]["expected_state"]), (run_id, "-", "WAIT_USER"))
        result = self.sup.process_inbox()[0]
        self.assertTrue(result["ok"])
        state = self.state()
        self.assertEqual(state["supervisor_state"], "RUNNING")
        self.assertEqual(state["continuation"]["note"], "continue from the current files")

    def test_wait_user_revision_rejects_stale_run_and_wrong_state(self) -> None:
        self.plan_gate()
        with self.sup.store.transaction():
            state = self.sup.store.read_state()
            state["pending_gate"]["status"] = "rejected"
            state["supervisor_state"] = "WAIT_USER"
            state["wait_user_requires_action"] = True
            self.sup.store.write_state(state)
        self.bridge.handle_update(message(82, "/revise stale guidance"))
        with self.sup.store.transaction():
            state = self.sup.store.read_state()
            state["run_id"] = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
            self.sup.store.write_state(state)
        result = self.sup.process_inbox()[0]
        self.assertFalse(result["ok"])
        self.assertIn("run_id", result["message"])
        with self.sup.store.transaction():
            state = self.sup.store.read_state()
            state["supervisor_state"] = "RUNNING"
            state["wait_user_requires_action"] = False
            self.sup.store.write_state(state)
        self.assertEqual(self.bridge.handle_update(message(83, "/revise wrong state"))["result"], "none")
        self.assertEqual(self.pending_inbox(), [])


class CallbackTests(TelegramCase):
    @staticmethod
    def buttons_of(card: dict) -> dict[str, str]:
        return {b["text"]: b["callback_data"] for row in (card["reply_markup"] or {"inline_keyboard": []})["inline_keyboard"] for b in row}

    def test_action_required_wait_user_guidance_button_opens_bound_reply_intent(self) -> None:
        self.plan_gate()
        with self.sup.store.transaction():
            state = self.sup.store.read_state()
            state["pending_gate"] = None
            state["supervisor_state"] = "WAIT_USER"
            state["wait_user_reason"] = "gate rejected: unsafe payload path"
            state["wait_user_requires_action"] = True
            self.sup.store.write_state(state)
        self.bridge.handle_update(message(19, "/status"))
        card = self.api.sent[-1]
        buttons = {button["text"]: button["callback_data"] for row in card["reply_markup"]["inline_keyboard"] for button in row}
        self.assertEqual(set(buttons), {"Request revision", "Cancel task", "Status"})
        self.assertIn("Task stopped: your decision is needed", card["text"])
        self.assertNotIn("Your input needed", card["text"])
        # the WAIT_USER card leads with the outcome and explains each button; diagnostics stay out of it
        wait_card = hp.render_wait_user("gate rejected: unsafe payload path", self.sup.status(), "UTC", requires_action=True)
        self.assertIn("Task stopped: your decision is needed", wait_card.html)
        self.assertIn("No approval or next step was created", wait_card.html)
        self.assertIn("Request revision: reply with one note", wait_card.html)
        self.assertIn("Cancel task: end the run", wait_card.html)
        self.assertNotIn("Send Guidance", wait_card.html)
        self.assertEqual({label for row in wait_card.keyboard for label, _, _ in row}, {"Request revision", "Cancel task", "Status"})
        result = self.bridge.handle_update(callback(20, buttons["Request revision"]))
        self.assertEqual(result["callback"], "revise_intent")
        self.assertIn("Reply with the revision note", self.api.sent[-1]["text"])
        self.assertIn("voids the current result", self.api.sent[-1]["text"])
        intent = hs.load_json(self.bridge._intent_path(CHAT), label="reply intent")
        self.assertEqual((intent["run_id"], intent["gate_id"], intent["expected_state"]), (self.state()["run_id"], "-", "WAIT_USER"))

    def deliver_plan_card(self) -> tuple[dict, dict]:
        state = self.plan_gate()
        counts = self.bridge.deliver_outbox()
        self.assertEqual(counts["sent"], 2)  # TASK_STARTED + PLAN_APPROVAL_REQUIRED card
        card = self.api.sent[-1]
        self.assertIsNotNone(card["reply_markup"])
        buttons = {b["text"]: b["callback_data"] for row in card["reply_markup"]["inline_keyboard"] for b in row}
        return state, buttons

    def test_callback_tokens_are_opaque_short_and_one_time(self) -> None:
        state, buttons = self.deliver_plan_card()
        for data in buttons.values():
            self.assertLess(len(data.encode()), 64)
            self.assertNotIn(state["run_id"], data)
            self.assertNotIn(state["pending_gate"]["gate_id"], data)
        result = self.bridge.handle_update(callback(20, buttons["Approve"]))
        self.assertEqual(result["result"], "enqueued")
        command = self.pending_inbox()[-1]
        self.assertEqual(command["action"], "approve")
        self.assertEqual(command["artifact_sha256"], state["pending_gate"]["artifact_sha256"])
        self.assertEqual(command["expected_state"], "WAIT_PLAN_APPROVAL")
        self.assertEqual(command["actor"], f"telegram:{OWNER}")
        ack = [c for c in self.api.calls if c[0] == "answerCallbackQuery"][-1][1]["text"]
        self.assertIn("Request received", ack)
        self.assertNotIn("approved", ack.lower(), "acknowledgement never implies the gate passed")
        # replay of the same token is refused; the supervisor applies the command exactly once
        replay = self.bridge.handle_update(callback(21, buttons["Approve"]))
        self.assertIn("rejected", replay)
        self.assertEqual(len(self.pending_inbox()), 1)
        results = self.sup.process_inbox()
        self.assertTrue(results[0]["ok"])
        self.assertEqual(self.state()["supervisor_state"], "RUNNING")
        # the COMMAND_RESULT is delivered as a separate durable message
        self.bridge.deliver_outbox()
        self.assertIn("Approval accepted", self.texts()[-1])

    def test_changed_plan_and_state_mismatch_make_buttons_inert(self) -> None:
        state, buttons = self.deliver_plan_card()
        (self.review_dir / "CODEX_PLAN.md").write_text("changed\n")
        result = self.bridge.handle_update(callback(30, buttons["Approve"]))
        self.assertEqual(result["rejected"], hs.PLAN_CHANGED_MESSAGE)
        self.assertEqual(self.pending_inbox(), [])
        (self.review_dir / "CODEX_PLAN.md").write_text((self.review_dir / "CODEX_PLAN.md").read_text())
        with self.sup.store.transaction():
            st = self.sup.store.read_state()
            st["supervisor_state"] = "PAUSED"
            st["pending_gate"]["status"] = "superseded"
            self.sup.store.write_state(st)
        result = self.bridge.handle_update(callback(31, buttons["Reject"]))
        self.assertIn("rejected", result)
        self.assertEqual(self.pending_inbox(), [])

    def test_expired_and_unknown_tokens_and_wrong_chat(self) -> None:
        _, buttons = self.deliver_plan_card()
        self.clock.current += 8 * 86400
        self.assertIn("rejected", self.bridge.handle_update(callback(40, buttons["Approve"])))
        self.clock.current = NOW
        self.assertEqual(self.bridge.handle_update(callback(41, "unknowntoken_unknowntoken"))["rejected"], "unknown_token")
        self.assertEqual(self.bridge.handle_update(callback(42, "!!"))["rejected"], "malformed_callback")
        self.assertEqual(self.bridge.handle_update(callback(43, buttons["Approve"], chat=999))["rejected"], "wrong_chat")
        self.assertEqual(self.pending_inbox(), [])

    def test_shadow_callback_changes_nothing(self) -> None:
        _, buttons = self.deliver_plan_card()
        shadow = self.make_bridge(mode="shadow")
        result = shadow.handle_update(callback(50, buttons["Approve"]))
        self.assertEqual(result["result"], "shadow")
        self.assertEqual(self.pending_inbox(), [])
        self.assertEqual(self.state()["supervisor_state"], "WAIT_PLAN_APPROVAL")

    def test_question_choice_buttons_and_push_card(self) -> None:
        self.write_plan()
        self.start_gated([{"v2": ("plan", "human", "generic_question", str(self.question_payload()))}])
        self.bridge.deliver_outbox()
        card = self.api.sent[-1]
        buttons = {b["text"]: b["callback_data"] for row in card["reply_markup"]["inline_keyboard"] for b in row}
        self.assertIn("enum", buttons)
        self.bridge.handle_update(callback(60, buttons["enum"]))
        command = self.pending_inbox()[-1]
        self.assertEqual((command["action"], command["answer"]), ("answer", "enum"))
        self.sup.process_inbox()
        self.assertEqual(self.state()["continuation"]["note"], "enum")


class OffsetAndJournalTests(TelegramCase):
    def test_offset_monotonic_and_no_replay_after_restart(self) -> None:
        self.bridge.write_offset(10)
        self.api.updates_batches = [[message(10, "/status"), message(11, "/status"), message(9, "/status")]]
        self.assertEqual(self.bridge.poll_once(), 2, "ids below the offset are ignored")
        self.assertEqual(self.bridge.read_offset(), 12)
        self.api.updates_batches = [[message(10, "/status"), message(11, "/status")]]
        self.assertEqual(self.bridge.poll_once(), 0, "already-journaled updates are skipped")
        bridge2 = self.make_bridge()  # restart
        self.api.updates_batches = [[message(11, "/status"), message(12, "/status")]]
        self.assertEqual(bridge2.poll_once(), 1)
        self.assertEqual(bridge2.read_offset(), 13)
        bridge2.write_offset(3)
        self.assertEqual(bridge2.read_offset(), 13, "offset never moves backwards")

    def test_crash_between_command_write_and_offset_advance_is_idempotent(self) -> None:
        self.plan_gate()
        update = message(70, "/pause")
        original = hs.atomic_write_json

        def crash_after_journal(path, value, **kw):
            original(path, value, **kw)
            if path.name == "70.json":
                raise RuntimeError("power loss")

        ht.hs.atomic_write_json = crash_after_journal
        try:
            self.api.updates_batches = [[update]]
            with self.assertRaises(RuntimeError):
                self.bridge.poll_once()
        finally:
            ht.hs.atomic_write_json = original
        self.assertEqual(self.bridge.read_offset(), 0, "offset not advanced before the durable result")
        self.assertEqual(len(self.pending_inbox()), 1)
        self.api.updates_batches = [[update]]
        self.bridge.poll_once()
        self.assertEqual(len(self.pending_inbox()), 1, "re-delivered update maps to the same request_id")
        self.assertEqual(self.bridge.read_offset(), 71)


class OutboxDeliveryTests(TelegramCase):
    def test_actionable_card_not_resent_blindly_after_uncertain_send(self) -> None:
        self.plan_gate()
        self.api.fail["sendMessage"] = [None, tg.TelegramError("HTTPS_FAILURE", "timed out")]
        counts = self.bridge.deliver_outbox()
        self.assertEqual((counts["sent"], counts["uncertain"]), (1, 1))
        sidecars = {p.stem: json.loads(p.read_text()) for p in (self.paths.outbox_dir / "delivery").glob("*.json")}
        uncertain = [s for s in sidecars.values() if s["status"] == "delivery_uncertain"]
        self.assertEqual(len(uncertain), 1)
        gate_id = self.state()["pending_gate"]["gate_id"]
        old_tokens = {p.stem for p in self.tg_paths.interactions_dir.glob("*.json") if json.loads(p.read_text()).get("gate_id") == gate_id}
        self.assertTrue(old_tokens)
        counts = self.bridge.deliver_outbox()  # recovery: gate still pending -> one replacement card with fresh tokens
        self.assertEqual(counts["sent"], 1)
        for token in old_tokens:
            self.assertTrue(json.loads((self.tg_paths.interactions_dir / f"{token}.json").read_text())["superseded"])
        new_tokens = {p.stem for p in self.tg_paths.interactions_dir.glob("*.json")} - old_tokens
        self.assertTrue(new_tokens)
        self.assertEqual(self.bridge.deliver_outbox()["sent"], 0)

    def test_stale_actionable_events_are_superseded_and_gate_revalidated_before_delivery(self) -> None:
        self.plan_gate()
        self.approve_pending()  # gate consumed before the bridge ever delivered the card
        counts = self.bridge.deliver_outbox()
        self.assertEqual(counts["superseded"], 1)
        actionable_sent = [m for m in self.api.sent if m["reply_markup"] and any(b["text"] == "Approve" for row in m["reply_markup"]["inline_keyboard"] for b in row)]
        self.assertEqual(actionable_sent, [], "no approve card for a consumed gate")

    def test_outage_preserves_progress_and_collapses_old_informational(self) -> None:
        self.write_plan()
        self.start_gated([{"v2": ("plan", "human", "plan_approval", str(self.plan_payload()))}])
        with self.sup.store.transaction():
            st = self.sup.store.read_state()
            for _ in range(12):
                self.sup.emit_event(st, "WAIT_QUOTA", {"provider": "codex", "resume_at": NOW + 10, "windows": ["five_hour"]})
            self.sup.store.write_state(st)
        self.api.fail["getUpdates"] = [tg.TelegramError("DNS_FAILURE", "x")] * 3
        self.api.fail["sendMessage"] = [tg.TelegramError("HTTPS_FAILURE", "x")] * 200
        self.bridge.run(max_rounds=3)
        self.assertEqual(self.state()["supervisor_state"], "WAIT_PLAN_APPROVAL", "telegram outage never touches supervisor state")
        self.assertEqual(self.clock.sleeps, [1.0, 2.0, 4.0], "bounded exponential backoff")
        self.api.fail = {}
        self.bridge.deliver_outbox()
        statuses = [json.loads(p.read_text())["status"] for p in (self.paths.outbox_dir / "delivery").glob("*.json")]
        self.assertGreaterEqual(statuses.count("collapsed_summarized"), 8)
        self.assertIn("earlier informational events collapsed", "\n".join(self.texts()))
        self.assertTrue(any("Plan ready for approval" in t for t in self.texts()), "the current gate is preserved through the outage")

    def test_quota_events_render_configured_timezone_and_session_abbrev(self) -> None:
        # The generic default is UTC; an operator-configured zone must drive rendering.
        self.bridge.tz = "Europe/Istanbul"
        self.plan_gate()
        with self.sup.store.transaction():
            st = self.sup.store.read_state()
            self.sup.emit_event(st, "WAIT_QUOTA", {"provider": "claude", "resume_at": 1789082400, "windows": ["five_hour"]})
            self.sup.store.write_state(st)
        self.bridge.deliver_outbox()
        text = [t for t in self.texts() if "quota reached" in t][-1]
        self.assertIn("preserved", text)
        self.assertIn("+03", text)


class DaemonSafetyTests(TelegramCase):
    def test_webhook_conflict_stops_without_deleting(self) -> None:
        self.api.webhook_url = "https://example.invalid/hook"
        self.assertEqual(self.bridge.run(max_rounds=1), 2)
        self.assertEqual(self.bridge.stopped_reason, "WEBHOOK_CONFLICT")
        self.assertNotIn("deleteWebhook", [c[0] for c in self.api.calls])
        self.assertEqual([c[0] for c in self.api.calls if c[0] == "getUpdates"], [])
        report = ht.doctor_summary(network=False, tg_paths=self.tg_paths)
        self.assertEqual(report["status"], "WEBHOOK_CONFLICT")

    def test_http_409_is_poller_conflict_and_local_lock_refuses_second_daemon(self) -> None:
        self.api.fail["getUpdates"] = [tg.TelegramError("POLLER_CONFLICT", "409", http_status=409, error_code=409)]
        self.assertEqual(self.bridge.run(max_rounds=2), 2)
        self.assertEqual(self.bridge.stopped_reason, "POLLER_CONFLICT")
        self.assertEqual(ht.doctor_summary(network=False, tg_paths=self.tg_paths)["status"], "POLLER_CONFLICT")
        self.tg_paths.conflict_file.unlink()
        import fcntl
        with self.tg_paths.lock_file.open("a+") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            second = self.make_bridge()
            self.assertEqual(second.run(max_rounds=1), 3)
            self.assertEqual(second.stopped_reason, "POLLER_CONFLICT_LOCAL")
            self.assertEqual(ht.doctor_summary(network=False, tg_paths=self.tg_paths)["checks"]["local_daemon"], "running")

    def test_bridge_never_binds_or_listens_and_has_no_pane_control_path(self) -> None:
        original_bind, original_listen = socket.socket.bind, socket.socket.listen

        def forbidden(*_a, **_k):
            raise AssertionError("bridge attempted to bind/listen")

        socket.socket.bind = forbidden  # type: ignore[method-assign]
        socket.socket.listen = forbidden  # type: ignore[method-assign]
        try:
            self.plan_gate()
            self.api.updates_batches = [[message(1, "/status")]]
            self.bridge.run(max_rounds=1)
        finally:
            socket.socket.bind, socket.socket.listen = original_bind, original_listen
        source = Path(ht.__file__).read_text()
        for forbidden_text in ("send_keys", "send-keys", "send_text", "start_agent", ".prompt(", "create_workspace", "recover_agent", "subprocess", "os.system", "run_loop", "approve_gate(", "record_runtime_evidence", "iptables", "ssh ", "wg-quick", "deleteWebhook", "setWebhook"):
            self.assertNotIn(forbidden_text, source, forbidden_text)
        self.assertNotIn("import subprocess", Path(tg.__file__).read_text())

    def test_dns_and_https_failures_are_distinguished_and_daemon_keeps_backoff(self) -> None:
        self.api.fail["getUpdates"] = [tg.TelegramError("DNS_FAILURE", "x"), tg.TelegramError("HTTPS_FAILURE", "x"), None]
        self.assertEqual(self.bridge.run(max_rounds=3), 0)
        self.assertEqual(self.clock.sleeps, [1.0, 2.0])
        self.assertEqual(self.bridge.backoff, 0.0, "success resets backoff")


class ApiAndRedactionTests(TelegramCase):
    def test_bot_api_sanitizes_token_and_classifies_errors(self) -> None:
        def opener_factory(exc=None, status=200, body=b'{"ok":true,"result":{"id":1}}'):
            def opener(url, data, timeout):
                if exc:
                    raise exc
                return status, body
            return opener

        api = tg.BotApi(FAKE_TOKEN, opener=opener_factory())
        self.assertEqual(api.call("getMe"), {"id": 1})
        cases = [
            (urllib.error.URLError(socket.gaierror("nx")), "DNS_FAILURE"),
            (urllib.error.URLError(ssl.SSLError("bad cert")), "HTTPS_FAILURE"),
            (TimeoutError(), "HTTPS_FAILURE"),
            (ConnectionResetError(), "HTTPS_FAILURE"),
        ]
        for exc, category in cases:
            with self.assertRaises(tg.TelegramError) as caught:
                tg.BotApi(FAKE_TOKEN, opener=opener_factory(exc)).call("getMe")
            self.assertEqual(caught.exception.category, category)
            self.assertNotIn(FAKE_TOKEN, str(caught.exception))
        with self.assertRaises(tg.TelegramError) as caught:
            tg.BotApi(FAKE_TOKEN, opener=opener_factory(status=401, body=b'{"ok":false,"error_code":401,"description":"Unauthorized"}')).call("getMe")
        self.assertEqual(caught.exception.category, "AUTH_FAILURE")
        with self.assertRaises(tg.TelegramError) as caught:
            tg.BotApi(FAKE_TOKEN, opener=opener_factory(status=409, body=b'{"ok":false,"error_code":409,"description":"Conflict"}')).call("getUpdates")
        self.assertEqual(caught.exception.category, "POLLER_CONFLICT")
        with self.assertRaises(tg.TelegramError) as caught:
            tg.BotApi(FAKE_TOKEN, opener=opener_factory(body=b"<html>")).call("getMe")
        self.assertEqual(caught.exception.category, "MALFORMED")
        leaky = tg.BotApi(FAKE_TOKEN, opener=opener_factory(status=400, body=json.dumps({"ok": False, "error_code": 400, "description": f"bad https://api.telegram.org/bot{FAKE_TOKEN}/x"}).encode()))
        with self.assertRaises(tg.TelegramError) as caught:
            leaky.call("sendMessage")
        self.assertNotIn(FAKE_TOKEN, str(caught.exception))
        with self.assertRaises(tg.TelegramError):
            tg.BotApi("not-a-token")
        with self.assertRaises(tg.TelegramError):
            api.send_message(1, "x" * 5000)
        self.assertEqual(tg.sanitize(f"url https://api.telegram.org/bot{FAKE_TOKEN}/getMe and {FAKE_TOKEN}"), "url https://api.telegram.org/bot<token>/getMe and <token>")

    def test_redaction_and_hostile_unicode(self) -> None:
        samples = [
            f"token {FAKE_TOKEN}", "Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123", "api_key=abcdef123456", "password: hunter2hunter2",
            "-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----", "see /home/x/.env for values", "cookie: session=abcdef12345678",
        ]
        for sample in samples:
            out = ht.redact(sample)
            self.assertIn("[redacted]", out, sample)
        self.assertNotIn(FAKE_TOKEN, ht.redact(f"x {FAKE_TOKEN} y"))
        self.assertEqual(ht.redact("a‮b\x00c​d"), "abcd")
        self.assertEqual(len(ht.redact("y" * 10000)), ht.CHUNK)
        self.assertEqual(len(ht.chunks("z" * 9000)), 3)

    def test_no_secret_in_status_doctor_logs_or_exceptions(self) -> None:
        self.plan_gate()
        secret = "hunter2-injected-secret"
        (self.paths.logs_dir / "poison.txt").write_text(secret)
        self.bridge.handle_update(message(1, f"/task {FAKE_TOKEN} {secret}"))  # rejected: task active; text never rendered raw
        for command in ("/status", "/doctor", "/logs", "/pending"):
            self.bridge.handle_update(message(len(self.api.sent) + 2, command))
        rendered = "\n".join(self.texts()) + json.dumps(self.sup.status()) + json.dumps(self.sup.doctor()) + json.dumps(ht.doctor_summary(network=False, tg_paths=self.tg_paths))
        self.assertNotIn(FAKE_TOKEN, rendered)
        self.assertNotIn(secret, rendered)
        for path in Path(self.tg_paths.state_dir).rglob("*.json"):
            self.assertNotIn(FAKE_TOKEN, path.read_text())

    def test_doctor_categories_and_setup_permissions(self) -> None:
        report = ht.doctor_summary(network=False, tg_paths=ht.TelegramPaths(config_file=Path(self.tmp.name) / "none.json", state_dir=Path(self.tmp.name) / "none"))
        self.assertEqual(report["status"], "UNCONFIGURED")
        self.assertEqual(ht.doctor_summary(network=False, tg_paths=self.tg_paths)["status"], "CONFIGURED_NO_NETWORK_CHECK")
        os.chmod(Path(self.tg_config["token_file"]), 0o644)
        bad = ht.doctor_summary(network=False, tg_paths=self.tg_paths)
        self.assertEqual(bad["status"], "CONFIG_ERROR")
        self.assertIn("0600", bad["detail"])
        os.chmod(Path(self.tg_config["token_file"]), 0o600)
        for category in ("DNS_FAILURE", "HTTPS_FAILURE", "AUTH_FAILURE"):
            class FailingApi(FakeApi):
                def get_me(self, _c=category):
                    raise tg.TelegramError(_c, "x")
            self.assertEqual(ht.doctor_summary(network=True, tg_paths=self.tg_paths, api_factory=lambda t: FailingApi())["status"], category)
        healthy = ht.doctor_summary(network=True, tg_paths=self.tg_paths, api_factory=lambda t: FakeApi())
        self.assertEqual(healthy["status"], "HEALTHY")
        self.assertNotIn(FAKE_TOKEN, json.dumps(healthy))
        # setup: non-echoed token, 0600 files, shadow mode, never echoed
        fresh = ht.TelegramPaths(config_file=Path(self.tmp.name) / "fresh" / "config.json", state_dir=Path(self.tmp.name) / "fresh-state")
        path = ht.setup(fresh, owner_user_id=OWNER, chat_id=CHAT, timezone="Europe/Istanbul", token_source=None, ask_secret=lambda prompt: FAKE_TOKEN)
        config = json.loads(path.read_text())
        self.assertEqual(config["mode"], "shadow")
        self.assertEqual(oct(path.stat().st_mode & 0o777), "0o600")
        self.assertEqual(oct(Path(config["token_file"]).stat().st_mode & 0o777), "0o600")
        self.assertNotIn(FAKE_TOKEN, path.read_text())
        with self.assertRaises(hs.SupervisorError):
            ht.setup(fresh, owner_user_id=OWNER, chat_id=CHAT, timezone="Europe/Istanbul", token_source=None, ask_secret=lambda prompt: "junk")
        self.assertEqual(ht.load_telegram_config(path)["plain_text_queries"], False)
        with self.assertRaises(hs.SupervisorError):
            ht.load_telegram_config_from_dict({**config, "mode": "actionable", "chat_id": None})
        self.assertEqual(tg.network_probe(resolver=lambda *a, **k: (_ for _ in ()).throw(socket.gaierror()))["status"], "DNS_FAILURE")
        self.assertEqual(tg.network_probe(resolver=lambda *a, **k: [], connector=lambda *a: (_ for _ in ()).throw(ssl.SSLError()))["status"], "HTTPS_FAILURE")
        self.assertEqual(tg.network_probe(resolver=lambda *a, **k: [], connector=lambda *a: None)["status"], "HEALTHY")


class QueryViaTelegramTests(TelegramCase):
    def test_ask_state_questions_are_deterministic_and_zero_prompt(self) -> None:
        self.plan_gate()
        for question in ("/ask what is the current state?", "/ask quota reset?", "/ask which gate is pending", "/ask context usage", "/ask what happened last"):
            result = self.bridge.handle_update(message(len(self.api.sent) + 1, question))
            self.assertEqual(result["result"], "state", question)
        self.assertEqual(len(self.herdr.prompts), 1, "no agent prompt for state questions")
        self.assertIn("WAIT_PLAN_APPROVAL", "\n".join(self.texts()))
        self.assertFalse((self.paths.query_dir / "inbox").exists() and list((self.paths.query_dir / "inbox").glob("*.json")))

    def test_ask_refuses_actions_secrets_and_ambiguity_and_plain_text_default_off(self) -> None:
        self.plan_gate()
        for question in ("/ask approve the plan", "/ask deploy to TEST now", "/ask what is the bot token", "/ask cat .env", "/ask push it", "/ask widgets"):
            result = self.bridge.handle_update(message(len(self.api.sent) + 1, question))
            self.assertEqual(result["result"], "refused", question)
            self.assertIn("/task", self.texts()[-1])
        self.bridge.handle_update(message(90, "how does matching work?"))
        self.assertIn("Plain text is disabled", self.texts()[-1])
        self.assertEqual(self.pending_inbox(), [])
        self.assertEqual(len(self.herdr.prompts), 1)

    def test_ask_interpretation_enqueues_query_only_in_actionable_mode_and_rate_limits(self) -> None:
        self.plan_gate()
        shadow = self.make_bridge(mode="shadow")
        shadow.handle_update(message(1, "/ask how does the candidate retrieval step decide between lexical and CAS matches?"))
        self.assertIn("SHADOW", self.texts()[-1])
        self.assertIn("eligible configured read-only query provider", self.texts()[-1])
        self.assertNotIn("codex-query", self.texts()[-1].lower())
        self.assertNotIn("Codex quota", self.texts()[-1])
        result = self.bridge.handle_update(message(2, "/ask how does the candidate retrieval step decide between lexical and CAS matches?"))
        self.assertEqual(result["result"], "model_query_enqueued")
        requests = list((self.paths.query_dir / "inbox").glob("*.json"))
        self.assertEqual(len(requests), 1)
        self.assertEqual(oct(requests[0].stat().st_mode & 0o777), "0o600")
        self.assertIn("select a safe available query provider", self.texts()[-1])
        self.assertNotIn("codex-query", self.texts()[-1].lower())
        self.bridge.handle_update(message(5, "/help"))
        self.assertIn("eligible configured read-only query session", self.texts()[-1])
        self.assertNotIn("codex-query", self.texts()[-1].lower())
        self.assertEqual(self.pending_inbox(), [], "queries never enter the workflow inbox")
        plain = self.make_bridge(plain_text_queries=True)
        plain.handle_update(message(3, "explain the offer generation flow"))
        self.assertEqual(len(list((self.paths.query_dir / "inbox").glob("*.json"))), 2)
        plain.handle_update(message(4, "implement the offer generation flow"))
        self.assertIn("/task", self.texts()[-1])
        self.assertEqual(self.pending_inbox(), [], "plain text can never start a task")
        for index in range(25):
            self.bridge.handle_update(message(100 + index, "/ask why is retrieval hybrid?"))
        self.assertIn("rate limit", self.texts()[-1])


class ReviewRegressionTelegramTests(TelegramCase):
    """CODEX_REVIEW.md findings F5 (bridge side), F7, F8, F9."""

    def test_f7_every_callback_class_is_one_time(self) -> None:
        state, buttons = CallbackTests.deliver_plan_card(self)
        for label in ("View Plan", "Request Revision"):
            first = self.bridge.handle_update(callback(len(self.api.sent) + 10, buttons[label]))
            self.assertNotIn("rejected", first, label)
            second = self.bridge.handle_update(callback(len(self.api.sent) + 11, buttons[label]))
            self.assertIn("rejected", second, label)
            self.assertIn("Already used", second["rejected"], label)
            self.assertTrue(json.loads((self.tg_paths.interactions_dir / f"{buttons[label]}.json").read_text())["consumed"], label)
        self.assertEqual(len(self.api.documents), 1, "plan document sent exactly once")
        # informational controls (status / keep waiting) and shadow callbacks are one-time too
        with self.sup.store.transaction():
            st = self.sup.store.read_state()
            self.sup.emit_event(st, "WAIT_QUOTA", {"provider": "codex", "resume_at": NOW + 10, "windows": ["five_hour"]})
            self.sup.store.write_state(st)
        self.bridge.deliver_outbox()
        info = self.api.sent[-1]["reply_markup"]["inline_keyboard"][0]
        status_token = next(b["callback_data"] for b in info if b["text"] == "Status")
        self.assertEqual(self.bridge.handle_update(callback(200, status_token))["callback"], "status")
        self.assertIn("rejected", self.bridge.handle_update(callback(201, status_token)))
        shadow = self.make_bridge(mode="shadow")
        self.assertEqual(shadow.handle_update(callback(202, buttons["Approve"]))["result"], "shadow")
        self.assertIn("rejected", shadow.handle_update(callback(203, buttons["Approve"])))
        self.assertEqual(self.pending_inbox(), [])

    def test_f8_same_bot_identity_with_another_state_root_is_refused(self) -> None:
        import fcntl
        if True:
            self.assertEqual(self.bridge.run(max_rounds=1), 0)
            lock_path = ht.TelegramPaths.bot_lock_file(BOT_ID)
            self.assertNotIn(FAKE_TOKEN, str(lock_path))
            self.assertEqual(oct(lock_path.parent.stat().st_mode & 0o777), "0o700")
            with lock_path.open("a+") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)  # daemon 1 holds the bot lock
                other_root = ht.TelegramPaths(config_file=self.tg_paths.config_file, state_dir=Path(self.tmp.name) / "other-state")
                other_api = FakeApi()
                second = ht.Bridge(sup_paths=self.paths, sup_config=self.config, tg_paths=other_root, tg_config=self.tg_config, api=other_api, bot_id=BOT_ID, status_reader=self.sup.status, clock=self.clock.time, sleeper=self.clock.sleep, rng=lambda: 0.0)
                self.assertEqual(second.run(max_rounds=1), 3)
                self.assertEqual(second.stopped_reason, "POLLER_CONFLICT_LOCAL")
                self.assertEqual([c for c in other_api.calls if c[0] == "getUpdates"], [], "second daemon never polls")

    def test_f9_action_requests_never_reach_the_model(self) -> None:
        self.plan_gate()
        for question in ("/ask Can you create a file for this?", "/ask Could you add a regression test?", "/ask Please rename the module?", "/ask Would you copy the config?", "/ask let's move the service", "/ask I want you to generate a migration"):
            result = self.bridge.handle_update(message(len(self.api.sent) + 1, question))
            self.assertEqual(result["result"], "refused", question)
            self.assertEqual(self.texts()[-1], herdr_query.REFUSAL_TEXT, question)
        self.assertFalse((self.paths.query_dir / "inbox").exists() and list((self.paths.query_dir / "inbox").glob("*.json")))
        self.assertEqual(len(self.herdr.prompts), 1)
        result = self.bridge.handle_update(message(300, "/ask Why does this code create a file?"))
        self.assertEqual(result["result"], "model_query_enqueued")

    def test_f5_bridge_pause_reaches_an_active_worker_promptly(self) -> None:
        self.write_plan()
        ticks = {"n": 0}

        def pause_from_telegram(fake: FakeHerdr, name: str) -> None:
            ticks["n"] += 1
            if ticks["n"] == 1:
                self.bridge.handle_update(message(400, "/pause"))
            raise hs.HerdrError("timeout", code="timeout")

        self.herdr.on_wait = pause_from_telegram
        self.assertEqual(self.start_gated([{"error": "timeout", "status": "working"}]), 3)
        self.assertEqual(self.state()["supervisor_state"], "PAUSED")
        self.assertEqual(ticks["n"], 1)
        self.assertEqual(self.herdr.reads, [])
        self.bridge.deliver_outbox()
        self.assertIn("Pause accepted", "\n".join(self.texts()))


class F10CallbackBindingTests(TelegramCase):
    def test_f10_old_run_cancel_callback_is_inert_for_a_new_run(self) -> None:
        self.plan_gate()
        with self.sup.store.transaction():
            st = self.sup.store.read_state()
            self.sup.emit_event(st, "WAIT_USER", {"reason": "needs a decision"})
            self.sup.store.write_state(st)
        self.bridge.deliver_outbox()
        cancel_token = next(b["callback_data"] for row in self.api.sent[-1]["reply_markup"]["inline_keyboard"] for b in row if b["text"] == "Cancel")
        # run A ends; run B starts
        with self.sup.store.transaction():
            st = self.sup.store.read_state()
            st["supervisor_state"] = "CANCELLED"
            self.sup.store.write_state(st)
        self.herdr = FakeHerdr()
        self.sup = self.make_supervisor()
        self.bridge = self.make_bridge()
        self.start_gated([{"v2": ("plan", "human", "plan_approval", str(self.plan_payload()))}])
        run_b = self.state()["run_id"]
        result = self.bridge.handle_update(callback(500, cancel_token))
        self.assertIn("earlier run", result["rejected"])
        self.assertEqual(self.pending_inbox(), [])
        self.assertEqual(self.state()["supervisor_state"], "WAIT_PLAN_APPROVAL")
        self.assertEqual(self.state()["run_id"], run_b)
        self.assertFalse(json.loads((self.tg_paths.interactions_dir / f"{cancel_token}.json").read_text())["consumed"], "an inert token is not consumed (safe retry after a false rejection is impossible anyway)")

    def test_f10_slash_controls_without_task_queue_nothing_and_bind_the_run(self) -> None:
        for command in ("/cancel", "/pause", "/resume"):
            result = self.bridge.handle_update(message(len(self.api.sent) + 1, command))
            self.assertEqual(result["result"], "no_task", command)
            self.assertIn("nothing queued", self.texts()[-1])
        self.assertEqual(self.pending_inbox(), [])
        self.assertFalse(self.paths.state_file.exists())
        self.plan_gate()
        self.bridge.handle_update(message(600, "/cancel"))
        queued = self.pending_inbox()[-1]
        self.assertEqual(queued["run_id"], self.state()["run_id"])
        with self.sup.store.transaction():
            st = self.sup.store.read_state()
            st["supervisor_state"] = "CANCELLED"
            self.sup.store.write_state(st)
        self.bridge.handle_update(message(601, "/pause"))
        self.assertIn("No applicable task", self.texts()[-1])

    def test_f10_shadow_approval_is_live_revalidated_before_preview(self) -> None:
        state, buttons = CallbackTests.deliver_plan_card(self)
        (self.review_dir / "CODEX_PLAN.md").write_text("changed after presentation\n")
        shadow = self.make_bridge(mode="shadow")
        result = shadow.handle_update(callback(700, buttons["Approve"]))
        self.assertEqual(result["rejected"], hs.PLAN_CHANGED_MESSAGE)
        ack = [c for c in self.api.calls if c[0] == "answerCallbackQuery"][-1][1]["text"]
        self.assertNotIn("SHADOW", ack)
        self.assertEqual(self.pending_inbox(), [])
        self.assertEqual(self.state()["supervisor_state"], "WAIT_PLAN_APPROVAL")
        # stale gate for the status button of a superseded run/state is inert too
        self.assertIn("rejected", shadow.handle_update(callback(701, buttons["View Plan"])))


class F10FollowUpAndF11Tests(TelegramCase):
    def test_f10_informational_card_bound_to_state_is_inert_after_same_run_state_change(self) -> None:
        self.plan_gate()
        with self.sup.store.transaction():
            st = self.sup.store.read_state()
            self.sup.emit_event(st, "WAIT_QUOTA", {"provider": "codex", "resume_at": NOW + 10, "windows": ["five_hour"]})
            self.sup.store.write_state(st)
        self.bridge.deliver_outbox()
        tokens = {b["text"]: b["callback_data"] for row in self.api.sent[-1]["reply_markup"]["inline_keyboard"] for b in row}
        record = json.loads((self.tg_paths.interactions_dir / f"{tokens['Pause']}.json").read_text())
        self.assertEqual(record["expected_state"], "WAIT_PLAN_APPROVAL")
        # same run, different state: approve the plan -> RUNNING
        self.approve_pending()
        for label in ("Pause", "Status", "Refresh quota"):
            result = self.bridge.handle_update(callback(len(self.api.calls) + 10, tokens[label]))
            self.assertIn("State changed", result["rejected"], label)
        shadow = self.make_bridge(mode="shadow")
        self.assertIn("State changed", shadow.handle_update(callback(900, tokens["Pause"]))["rejected"])
        self.assertEqual(self.pending_inbox(), [])
        self.assertEqual(self.state()["supervisor_state"], "RUNNING")
        # /status stays the always-available fresh command
        self.assertEqual(self.bridge.handle_update(message(901, "/status"))["result"], "status")
        self.assertIn("Task in progress", self.texts()[-1])

    def test_f11_setup_and_prepare_create_the_0700_bot_lock_dir(self) -> None:
        lock_dir = ht.TelegramPaths.bot_lock_dir()
        self.assertTrue(str(lock_dir).startswith(self.tmp.name), "test HOME is the temporary root")
        self.assertFalse(lock_dir.exists())
        fresh = ht.TelegramPaths(config_file=Path(self.tmp.name) / "fresh" / "config.json", state_dir=Path(self.tmp.name) / "fresh-state")
        ht.setup(fresh, owner_user_id=OWNER, chat_id=CHAT, timezone="Europe/Istanbul", token_source=None, ask_secret=lambda prompt: FAKE_TOKEN)
        self.assertEqual(oct(lock_dir.stat().st_mode & 0o777), "0o700")
        lock_dir.rmdir()
        saved = {k: os.environ.get(k) for k in ("HERDR_TELEGRAM_CONFIG", "HERDR_TELEGRAM_STATE_DIR")}
        os.environ["HERDR_TELEGRAM_CONFIG"] = str(fresh.config_file)
        os.environ["HERDR_TELEGRAM_STATE_DIR"] = str(fresh.state_dir)
        try:
            self.assertEqual(ht.main(["prepare"]), 0)
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        self.assertEqual(oct(lock_dir.stat().st_mode & 0o777), "0o700")
        self.assertEqual(oct(fresh.state_dir.stat().st_mode & 0o777), "0o700")
        self.assertEqual([c[0] for c in self.api.calls], [], "prepare touches no network")


class OperatorHandoffTelegramTests(TelegramCase):
    FORBIDDEN_CLAIMS = ("PUSH READY", "push-ready", "pushed", "published", "deployed", "push approved", "runtime passed", "Task complete")

    def to_handoff_ready(self) -> dict:
        self.write_plan()
        self.start_gated([{"v2": ("plan", "human", "plan_approval", str(self.plan_payload(runtime_validation_required=False, rebuild_required=False)))}])
        self.approve_pending()
        self.assertEqual(self.resume_with([{"v2": ("brief", "claude")}, {"v2": ("implement", "codex")}, {"v2": ("review", "done")}]), 2)
        return self.state()

    def counters(self) -> tuple[int, int, int]:
        return (len(self.herdr.prompts), len(self.herdr.reads), len(self.herdr.waits))

    def buttons(self, index: int = -1) -> dict[str, str]:
        markup = self.api.sent[index]["reply_markup"] or {"inline_keyboard": []}
        return {b["text"]: b["callback_data"] for row in markup["inline_keyboard"] for b in row}

    def test_done_is_refused_and_hidden_while_a_gate_or_ordinary_wait_applies(self) -> None:
        self.plan_gate()
        self.bridge.handle_update(message(1, "/done"))
        self.assertIn("not available", self.texts()[-1])
        self.assertIn("typed gate is pending", self.texts()[-1])
        self.assertEqual(self.pending_inbox(), [])
        self.bridge.handle_update(message(2, "/status"))
        self.assertNotIn("Done (operator handoff)", self.buttons())
        with self.sup.store.transaction():
            st = self.sup.store.read_state()
            st["pending_gate"] = None
            st["supervisor_state"] = "WAIT_USER"
            st["wait_user_reason"] = "pane check"
            st["wait_user_requires_action"] = False
            self.sup.store.write_state(st)
        self.bridge.handle_update(message(3, "/status"))
        self.assertNotIn("Done (operator handoff)", self.buttons())
        self.bridge.handle_update(message(4, "/done"))
        self.assertIn("ordinary pause", self.texts()[-1])
        self.assertEqual(self.pending_inbox(), [])
        self.bridge.handle_update(message(5, "/help"))
        self.assertIn("/done", self.texts()[-1])
        self.assertIn("NOT verified", self.texts()[-1])

    def test_status_and_wait_card_offer_done_only_when_the_predicate_allows(self) -> None:
        state = self.to_handoff_ready()
        self.bridge.deliver_outbox()
        wait_card = next(m for m in reversed(self.api.sent) if "Task stopped: your decision is needed" in m["text"])
        self.assertIn("only your own actions remain", wait_card["text"])
        self.assertIn("push approval", wait_card["text"])
        buttons = {b["text"]: b["callback_data"] for row in wait_card["reply_markup"]["inline_keyboard"] for b in row}
        self.assertIn("Done (operator handoff)", buttons)
        record = json.loads((self.tg_paths.interactions_dir / f"{buttons['Done (operator handoff)']}.json").read_text())
        self.assertEqual((record["action"], record["run_id"], record["expected_state"], record["ready_turn_id"]), ("done", state["run_id"], "WAIT_USER", state["operator_handoff_ready"]["turn_id"]))
        self.bridge.handle_update(message(10, "/status"))
        self.assertIn("Done (operator handoff)", self.buttons())
        # the same predicate hides the button once the agent is working again
        self.herdr.agents["codex-main"]["agent_status"] = "working"
        self.bridge.handle_update(message(11, "/status"))
        self.assertNotIn("Done (operator handoff)", self.buttons())

    def test_done_command_closes_via_worker_with_zero_wakeups_and_unverified_wording(self) -> None:
        state = self.to_handoff_ready()
        before = self.counters()
        result = self.bridge.handle_update(message(20, "/done I will push it myself"))
        self.assertEqual(result["result"], "enqueued")
        pending = self.pending_inbox()
        self.assertEqual(len(pending), 1)
        self.assertEqual((pending[0]["action"], pending[0]["operator_handoff"], pending[0]["run_id"], pending[0]["ready_turn_id"], pending[0]["note"], pending[0]["actor"], pending[0]["chat_id"]),
                         ("done", True, state["run_id"], state["operator_handoff_ready"]["turn_id"], "I will push it myself", f"telegram:{OWNER}", CHAT))
        self.assertEqual(self.sup.worker(), 0)
        after = self.state()
        self.assertEqual(after["supervisor_state"], "DONE")
        self.assertEqual(after["completion"]["chat_id"], CHAT)
        self.assertIsNone(after["push_approval"])
        self.assertEqual(self.counters(), before, "no prompt, transcript read, or wake-up during /done")
        self.bridge.deliver_outbox()
        joined = "\n".join(self.texts())
        self.assertIn("Task closed by operator handoff", joined)
        self.assertIn("NOT verified by Supervisor: push approval", joined)
        self.assertIn("note: I will push it myself", joined)
        self.assertIn("task closed by operator handoff", joined.lower())
        for claim in self.FORBIDDEN_CLAIMS:
            self.assertNotIn(claim, joined)
        self.bridge.handle_update(message(21, "/status"))
        self.assertIn("Task closed by operator handoff", self.texts()[-1])
        self.assertIn("NOT verified", self.texts()[-1])
        self.assertNotIn("Done (operator handoff)", self.buttons())
        # already terminal: /done and a fresh command are inert
        self.bridge.handle_update(message(22, "/done"))
        self.assertIn("already DONE", self.texts()[-1])
        self.assertEqual(self.pending_inbox(), [])
        self.assertEqual(self.event_types().count("TASK_HANDED_OFF"), 1)
        self.assertEqual(self.counters(), before)

    def test_done_button_is_one_time_state_bound_and_actor_bound(self) -> None:
        self.to_handoff_ready()
        self.bridge.deliver_outbox()
        wait_card = next(m for m in reversed(self.api.sent) if "Task stopped: your decision is needed" in m["text"])
        token = {b["text"]: b["callback_data"] for row in wait_card["reply_markup"]["inline_keyboard"] for b in row}["Done (operator handoff)"]
        self.assertEqual(self.bridge.handle_update(callback(30, token, user=OWNER + 1))["rejected"], "wrong_user")
        self.assertEqual(self.bridge.handle_update(callback(31, token, chat=CHAT + 1))["rejected"], "wrong_chat")
        self.assertEqual(self.pending_inbox(), [])
        # guidance supersedes readiness: the offered button becomes inert without mutation
        with self.sup.store.transaction():
            st = self.sup.store.read_state()
            self.sup.guide(st, run_id=st["run_id"], actor="cli", note="retry")
        self.assertIsNone(self.state()["operator_handoff_ready"])
        result = self.bridge.handle_update(callback(32, token))
        self.assertIn("no longer available", result["rejected"])
        self.assertEqual(self.pending_inbox(), [])
        self.assertEqual(self.state()["supervisor_state"], "RUNNING")
        # a later, new readiness gets a new turn id; the old token stays inert (state binding), the new one works once
        self.assertEqual(self.resume_with([{"v2": ("review", "done")}]), 2)
        self.bridge.deliver_outbox()
        self.assertIn("no longer available", self.bridge.handle_update(callback(33, token))["rejected"])
        new_card = next(m for m in reversed(self.api.sent) if "Task stopped: your decision is needed" in m["text"])
        new_token = {b["text"]: b["callback_data"] for row in new_card["reply_markup"]["inline_keyboard"] for b in row}["Done (operator handoff)"]
        self.assertNotEqual(new_token, token)
        before = self.counters()
        first = self.bridge.handle_update(callback(34, new_token))
        self.assertEqual(first["result"], "enqueued")
        self.assertEqual(self.bridge.handle_update(callback(35, new_token))["rejected"], "Already used (one-time action).")
        self.assertEqual(len(self.pending_inbox()), 1)
        self.assertEqual(self.pending_inbox()[0]["ready_turn_id"], self.state()["operator_handoff_ready"]["turn_id"])
        self.assertEqual(self.sup.worker(), 0)
        self.assertEqual(self.state()["supervisor_state"], "DONE")
        self.assertEqual(self.event_types().count("TASK_HANDED_OFF"), 1)
        self.assertEqual(self.counters(), before)

    def test_shadow_mode_done_changes_nothing(self) -> None:
        self.to_handoff_ready()
        shadow = self.make_bridge(mode="shadow")
        self.assertEqual(shadow.handle_update(message(40, "/done"))["result"], "shadow")
        self.assertEqual(self.pending_inbox(), [])
        self.assertEqual(self.state()["supervisor_state"], "WAIT_USER")

    def test_done_command_and_callback_share_the_controller_predicate(self) -> None:
        """Both paths consult Supervisor.operator_handoff_eligibility; a monkeypatched refusal blocks both."""
        self.to_handoff_ready()
        self.bridge.deliver_outbox()
        wait_card = next(m for m in reversed(self.api.sent) if "Task stopped: your decision is needed" in m["text"])
        token = {b["text"]: b["callback_data"] for row in wait_card["reply_markup"]["inline_keyboard"] for b in row}["Done (operator handoff)"]
        calls: list[dict] = []
        original = type(self.bridge.enqueuer).operator_handoff_eligibility

        def refusing(self_, state, **kwargs):
            calls.append(kwargs)
            return False, "predicate says no"

        type(self.bridge.enqueuer).operator_handoff_eligibility = refusing  # type: ignore[method-assign]
        try:
            self.assertEqual(self.bridge.handle_update(message(50, "/done"))["result"], "ineligible")
            self.assertIn("predicate says no", self.texts()[-1])
            self.assertIn("predicate says no", self.bridge.handle_update(callback(51, token))["rejected"])
        finally:
            type(self.bridge.enqueuer).operator_handoff_eligibility = original  # type: ignore[method-assign]
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["inspect_agents"], False)
        self.assertEqual((calls[1]["inspect_agents"], calls[1]["ready_turn_id"]), (False, self.state()["operator_handoff_ready"]["turn_id"]))
        self.assertEqual(self.pending_inbox(), [])


class MissingResultRetryTelegramTests(TelegramCase):
    def to_missing_result(self) -> dict:
        self.write_plan()
        self.assertEqual(self.start_gated([{"output": "forgot the block\n", "status": "idle"}]), 2)
        state = self.state()
        self.assertIsNotNone(state["missing_result"])
        return state

    def buttons_of(self, card: dict) -> dict[str, str]:
        return {b["text"]: b["callback_data"] for row in (card["reply_markup"] or {"inline_keyboard": []})["inline_keyboard"] for b in row}

    def test_missing_result_card_explains_no_approval_and_offers_bound_retry(self) -> None:
        state = self.to_missing_result()
        self.bridge.deliver_outbox()
        card = next(m for m in reversed(self.api.sent) if "Agent finished without a workflow result" in m["text"])
        self.assertIn("The agent finished, but Supervisor did not receive the required workflow result. No approval or next step was created", card["text"])
        self.assertIn("Retry routing result: reread the same completed turn once. No message is sent to the agent.", card["text"])
        self.assertIn("Ask agent: send one question", card["text"])
        self.assertIn("Request revision: ask the agent for a replacement workflow result", card["text"])
        self.assertIn("Cancel task: end the run", card["text"])
        self.assertNotIn("Your input needed", card["text"])
        self.assertNotIn("Routing result not read", card["text"])
        for internal in ("settled transcript", "protocol block", "delivery", state["delivery"]["turn_id"][:8]):
            self.assertNotIn(internal, card["text"], internal)
        buttons = self.buttons_of(card)
        self.assertIn("Retry routing result", buttons)
        self.assertIn("Ask agent", buttons)
        self.assertIn("Request revision", buttons)
        self.assertIn("Cancel task", buttons)
        record = json.loads((self.tg_paths.interactions_dir / f"{buttons['Retry routing result']}.json").read_text())
        self.assertEqual((record["action"], record["run_id"], record["turn_id"], record["expected_state"]), ("retry_routing_result", state["run_id"], state["delivery"]["turn_id"], "WAIT_USER"))
        self.bridge.handle_update(message(5, "/status"))
        self.assertIn("did not receive the required workflow result", self.texts()[-1])
        self.assertIn("Retry routing result", self.buttons_of(self.api.sent[-1]))
        self.assertIn("Prompt deliveries prepared: 1", self.texts()[-1])
        self.assertNotIn("Supervisor prompts", self.texts()[-1])

    def test_retry_button_recovers_the_gate_once_with_zero_prompts_and_is_one_time(self) -> None:
        state = self.to_missing_result()
        self.bridge.deliver_outbox()
        card = next(m for m in reversed(self.api.sent) if "Agent finished without a workflow result" in m["text"])
        token = self.buttons_of(card)["Retry routing result"]
        self.assertEqual(self.bridge.handle_update(callback(10, token, user=OWNER + 1))["rejected"], "wrong_user")
        # the settled transcript now carries the (wrapped) result
        payload = str(self.plan_payload())
        block = FakeHerdr.block_v2(state["run_id"], state["delivery"]["turn_id"], "plan", "human", "plan_approval", payload, prefix="  ")
        self.herdr.outputs["codex-main"] = "reply\n\n" + block.replace(payload, payload[:20] + "\n  " + payload[20:]) + "\n"
        prompts_before = len(self.herdr.prompts)
        result = self.bridge.handle_update(callback(11, token))
        self.assertEqual(result["result"], "enqueued")
        self.assertEqual(self.bridge.handle_update(callback(12, token))["rejected"], "Already used (one-time action).")
        self.assertEqual(len(self.pending_inbox()), 1)
        self.assertEqual(self.pending_inbox()[0]["turn_id"], state["delivery"]["turn_id"])
        # asynchronous: before the worker resolves the command nothing claims success and no card exists
        self.bridge.deliver_outbox()
        pre = "\n".join(self.texts())
        self.assertNotIn("routing result recovered", pre)
        self.assertNotIn("Plan ready for approval", pre)
        self.assertEqual(self.state()["supervisor_state"], "WAIT_USER")
        self.assertEqual(self.event_types().count("PLAN_APPROVAL_REQUIRED"), 0)
        self.assertEqual(self.sup.worker(), 4)
        after = self.state()
        self.assertEqual(after["supervisor_state"], "WAIT_PLAN_APPROVAL")
        self.assertIsNone(after["missing_result"])
        self.assertEqual(len(self.herdr.prompts), prompts_before, "retry never prompts")
        counts = self.bridge.deliver_outbox()
        self.assertGreaterEqual(counts["sent"], 1)
        joined = "\n".join(self.texts())
        self.assertIn("routing result recovered", joined)
        self.assertIn("Plan ready for approval", joined)
        self.assertEqual(self.event_types().count("PLAN_APPROVAL_REQUIRED"), 1)
        # restart and a fresh retry after recovery: nothing changes
        self.assertEqual(self.make_supervisor().worker(), 4)
        self.assertEqual(self.event_types().count("PLAN_APPROVAL_REQUIRED"), 1)
        self.assertEqual(len(self.herdr.prompts), prompts_before)

    def test_retry_token_is_inert_after_guidance_and_reports_when_still_unreadable(self) -> None:
        self.to_missing_result()
        self.bridge.deliver_outbox()
        card = next(m for m in reversed(self.api.sent) if "Agent finished without a workflow result" in m["text"])
        token = self.buttons_of(card)["Retry routing result"]
        # still unreadable: the command applies, the wait stays, the human is told and nothing was resent
        self.assertEqual(self.bridge.handle_update(callback(20, token))["result"], "enqueued")
        self.assertEqual(self.sup.worker(), 2)
        self.assertEqual(self.state()["missing_result"]["attempts"], 1)
        self.bridge.deliver_outbox()
        self.assertIn("still no routing result", "\n".join(self.texts()))
        self.assertEqual(len(self.herdr.prompts), 1)
        # the reread is spent: /status explains it and offers no retry button; the old token is inert; a
        # replayed callback is rejected; guidance remains the recovery
        self.bridge.handle_update(message(21, "/status"))
        self.assertIn("one-time reread of that turn was already used", self.texts()[-1])
        self.assertNotIn("Retry routing result", self.buttons_of(self.api.sent[-1]))
        self.assertIn("Request revision", self.buttons_of(self.api.sent[-1]))
        self.assertIn("Ask agent", self.buttons_of(self.api.sent[-1]))
        self.assertEqual(self.bridge.handle_update(callback(22, token))["rejected"], "Already used (one-time action).")
        self.bridge.deliver_outbox()
        stale_cards = [m for m in self.api.sent if m["reply_markup"] and "Retry routing result" in self.buttons_of(m)]
        for card in stale_cards:
            self.assertIn("already used", self.bridge.handle_update(callback(23, self.buttons_of(card)["Retry routing result"]))["rejected"].lower())
        self.assertEqual(len(self.pending_inbox()), 0)
        self.assertEqual(self.make_supervisor().worker(), 2)
        self.assertEqual(self.state()["missing_result"]["attempts"], 1)
        with self.sup.store.transaction():
            st = self.sup.store.read_state()
            self.sup.guide(st, run_id=st["run_id"], actor="cli", note="emit the block")
        self.assertIsNone(self.state()["missing_result"])
        self.assertEqual(len(self.herdr.prompts), 1)


class AgentFollowupTelegramTests(TelegramCase):
    """Ask agent from Telegram: separate from Request revision, bound one-message intent, asynchronous
    acknowledgement, one verified answer + document, then the restored decision's own controls."""

    def buttons_of(self, card: dict) -> dict[str, str]:
        return {b["text"]: b["callback_data"] for row in (card["reply_markup"] or {"inline_keyboard": []})["inline_keyboard"] for b in row}

    def latest_card(self, needle: str) -> dict:
        return next(m for m in reversed(self.api.sent) if needle in m["text"])

    def deliver_plan_card(self) -> tuple[dict, dict]:
        state = self.plan_gate()
        self.bridge.deliver_outbox()
        card = self.latest_card("Plan ready for approval")
        return state, self.buttons_of(card)

    def test_ask_agent_button_is_separate_from_revision_and_opens_a_bound_intent(self) -> None:
        state, buttons = self.deliver_plan_card()
        self.assertIn("Ask agent", buttons)
        self.assertIn("Request Revision", buttons)
        self.assertNotEqual(buttons["Ask agent"], buttons["Request Revision"])
        record = json.loads((self.tg_paths.interactions_dir / f"{buttons['Ask agent']}.json").read_text())
        self.assertEqual((record["action"], record["decision_id"], record["artifact_sha256"], record["expected_state"]), ("ask_agent", state["pending_gate"]["gate_id"], state["pending_gate"]["artifact_sha256"], "WAIT_PLAN_APPROVAL"))
        self.assertEqual(self.bridge.handle_update(callback(10, buttons["Ask agent"], user=OWNER + 1))["rejected"], "wrong_user")
        result = self.bridge.handle_update(callback(11, buttons["Ask agent"]))
        self.assertEqual(result["callback"], "ask_agent_intent")
        self.assertIn("Reply with your question", self.texts()[-1])
        self.assertIn("decision stays as it is", self.texts()[-1])
        self.assertEqual(self.bridge.handle_update(callback(12, buttons["Ask agent"]))["rejected"], "Already used (one-time action).")
        intent = hs.load_json(self.bridge._intent_path(CHAT), label="reply intent")
        self.assertEqual((intent["kind"], intent["run_id"], intent["decision_id"], intent["artifact_sha256"]), ("ask_agent", state["run_id"], state["pending_gate"]["gate_id"], state["pending_gate"]["artifact_sha256"]))
        # the reply becomes one bound command; nothing claims success and no prompt exists until the worker runs
        self.assertEqual(self.bridge.handle_update(message(13, "Why is a rebuild required?"))["result"], "enqueued")
        queued = self.pending_inbox()
        self.assertEqual(len(queued), 1)
        self.assertEqual((queued[0]["action"], queued[0]["decision_id"], queued[0]["expected_state"], queued[0]["question"], queued[0]["chat_id"]), ("ask_agent", state["pending_gate"]["gate_id"], "WAIT_PLAN_APPROVAL", "Why is a rebuild required?", CHAT))
        self.assertNotIn("revise", queued[0]["action"])
        self.assertIn("Queued: ask the agent", self.texts()[-1])
        self.bridge.deliver_outbox()
        pre = "\n".join(self.texts())
        self.assertNotIn("answered", pre)
        self.assertEqual(self.api.documents, [])
        self.assertEqual(self.state()["supervisor_state"], "WAIT_PLAN_APPROVAL")
        self.assertEqual(len(self.herdr.prompts), 1)

    def test_answer_arrives_once_with_document_and_restored_gate_controls_that_still_work(self) -> None:
        state, buttons = self.deliver_plan_card()
        self.bridge.handle_update(callback(20, buttons["Ask agent"]))
        self.bridge.handle_update(message(21, "Is the migration flag right?"))
        self.herdr.responses = [{"followup": {"summary": "migration_flag_is_correct_no_change_needed", "content": "# Answer\n\nThe flag is right.\n\n# Recommendation\n\nApprove.\n\n# Change needed\n\nno\n\n# Next operator action\n\nApprove.\n"}}]
        self.assertEqual(self.sup.worker(), 4)
        after = self.state()
        self.assertEqual((after["supervisor_state"], after["pending_gate"]["gate_id"], after["pending_gate"]["status"]), ("WAIT_PLAN_APPROVAL", state["pending_gate"]["gate_id"], "pending"))
        self.assertEqual(len(self.herdr.prompts), 2)
        counts = self.bridge.deliver_outbox()
        self.assertGreaterEqual(counts["sent"], 1)
        joined = "\n".join(self.texts())
        self.assertIn("Question to the agent accepted", joined)
        card = self.latest_card("Codex answered")
        self.assertIn("The plan approval decision is unchanged. Nothing was approved, revised, or advanced.", card["text"])
        self.assertIn("migration_flag_is_correct_no_change_needed", card["text"])
        self.assertIn("Is the migration flag right?", card["text"])
        self.assertIn("full answer is attached", card["text"])
        self.assertEqual(len(self.api.documents), 1)
        self.assertIn(b"The flag is right.", self.api.documents[0]["data"])
        self.assertTrue(self.api.documents[0]["filename"].startswith("followup-"))
        restored = self.buttons_of(card)
        self.assertEqual(set(restored), {"View Plan", "Approve", "Request Revision", "Reject", "Ask agent"})
        record = json.loads((self.tg_paths.interactions_dir / f"{restored['Approve']}.json").read_text())
        self.assertEqual((record["gate_id"], record["artifact_sha256"], record["expected_state"]), (state["pending_gate"]["gate_id"], state["pending_gate"]["artifact_sha256"], "WAIT_PLAN_APPROVAL"))
        # exactly one answer card and one document, also after a restart and another delivery pass
        self.assertEqual(self.make_supervisor().worker(), 4)
        self.bridge.deliver_outbox()
        self.assertEqual(sum("Codex answered" in t for t in self.texts()), 1)
        self.assertEqual(len(self.api.documents), 1)
        # the restored Approve button approves the very same gate
        self.assertEqual(self.bridge.handle_update(callback(22, restored["Approve"]))["result"], "enqueued")
        self.assertEqual(self.sup.process_inbox()[-1]["ok"], True)
        self.assertEqual(self.state()["pending_gate"]["status"], "approved")
        self.assertEqual(self.state()["approved_plan"]["gate_id"], state["pending_gate"]["gate_id"])

    def test_ask_agent_command_and_stale_intent_handling(self) -> None:
        state = self.plan_gate()
        self.assertEqual(self.bridge.handle_update(message(30, "/ask-agent"))["result"], "intent")
        # the decision changed before the reply: nothing is sent
        self.approve_pending()
        self.assertEqual(self.bridge.handle_update(message(31, "still there?"))["result"], "stale_intent")
        self.assertIn("decision changed", self.texts()[-1])
        self.assertEqual(self.pending_inbox(), [])
        self.assertEqual(self.bridge.handle_update(message(32, "/ask-agent anything?"))["result"], "none")
        self.assertIn("needs a pending decision", self.texts()[-1])
        # /ask stays the independent read-only facility
        self.assertEqual(self.bridge.handle_update(message(33, "/ask what is the quota?"))["result"], "state")
        # a direct /ask-agent <question> at a gate enqueues one bound command
        self.reset_fixture()
        state = self.plan_gate()
        self.assertEqual(self.bridge.handle_update(message(34, "/ask-agent " + "q" * 1001))["result"], "too_long")
        self.assertEqual(self.bridge.handle_update(message(35, "/ask-agent Why this scope?"))["result"], "enqueued")
        self.assertEqual(self.pending_inbox()[0]["decision_id"], state["pending_gate"]["gate_id"])
        self.sup.process_inbox()
        self.assertEqual(self.state()["agent_followup"]["status"], "PREPARED")
        # an undelivered gate card is held, not superseded, while the question is out
        held = self.bridge.deliver_outbox()
        self.assertEqual(held["superseded"], 0)
        self.assertNotIn("Plan ready for approval", "\n".join(self.texts()))
        self.assertEqual(self.bridge.handle_update(message(36, "/ask-agent another?"))["result"], "followup_in_flight")
        self.assertEqual(self.bridge.handle_update(message(37, "/revise change it"))["result"], "enqueued")
        self.assertFalse(self.sup.process_inbox()[-1]["ok"], "a revision cannot slip in while the question is out")
        self.assertEqual(self.bridge.handle_update(message(39, "/status"))["result"], "status")
        self.assertIn("Question with the agent", self.texts()[-1])
        self.assertEqual(set(self.buttons_of(self.api.sent[-1])), {"Status", "Pause"})
        # the held gate card arrives once the decision is restored; gate buttons from before are inert while the question is out
        self.reset_fixture()
        state, plan_buttons = self.deliver_plan_card()
        self.assertEqual(self.bridge.handle_update(message(40, "/ask-agent Why this scope?"))["result"], "enqueued")
        self.sup.process_inbox()
        self.assertIn("in flight", self.bridge.handle_update(callback(41, plan_buttons["Approve"]))["rejected"])
        self.reset_fixture()
        shadow = self.make_bridge(mode="shadow")
        self.plan_gate()
        self.assertEqual(shadow.handle_update(message(42, "/ask-agent shadow?"))["result"], "shadow")
        self.assertEqual(self.pending_inbox(), [])

    def test_missing_result_ask_agent_returns_to_the_same_recovery_card(self) -> None:
        self.write_plan()
        self.assertEqual(self.start_gated([{"output": "prose only\n", "status": "idle"}]), 2)
        state = self.state()
        self.bridge.deliver_outbox()
        card = self.latest_card("Agent finished without a workflow result")
        buttons = self.buttons_of(card)
        record = json.loads((self.tg_paths.interactions_dir / f"{buttons['Ask agent']}.json").read_text())
        self.assertEqual((record["action"], record["turn_id"], record["decision_id"]), ("ask_agent", state["missing_result"]["turn_id"], state["missing_result"]["turn_id"]))
        self.assertEqual(self.bridge.handle_update(callback(50, buttons["Ask agent"]))["callback"], "ask_agent_intent")
        self.assertEqual(self.bridge.handle_update(message(51, "What did you conclude?"))["result"], "enqueued")
        self.herdr.responses = [{"followup": {"summary": "I_concluded_the_plan_is_ready"}}]
        self.assertEqual(self.sup.worker(), 2)
        after = self.state()
        self.assertEqual(after["missing_result"], state["missing_result"])
        self.bridge.deliver_outbox()
        answer = self.latest_card("Codex answered")
        self.assertIn("unread-result recovery is unchanged", answer["text"])
        restored = self.buttons_of(answer)
        self.assertEqual(set(restored), {"Retry routing result", "Request revision", "Cancel task", "Status", "Ask agent"})
        # the original retry still works from the restored card, with zero prompts
        self.herdr.outputs["codex-main"] = FakeHerdr.block_v2(state["run_id"], state["delivery"]["turn_id"], "plan", "human", "plan_approval", str(self.plan_payload()))
        prompts = len(self.herdr.prompts)
        self.assertEqual(self.bridge.handle_update(callback(52, restored["Retry routing result"]))["result"], "enqueued")
        self.assertEqual(self.sup.worker(), 4)
        self.assertEqual(self.state()["supervisor_state"], "WAIT_PLAN_APPROVAL")
        self.assertEqual(len(self.herdr.prompts), prompts)

    def test_unverified_answer_card_explains_and_every_button_works(self) -> None:
        state, buttons = self.deliver_plan_card()
        self.bridge.handle_update(callback(60, buttons["Ask agent"]))
        self.bridge.handle_update(message(61, "Why?"))
        self.herdr.responses = [{"output": "prose without the frame\n", "status": "idle"}]
        self.assertEqual(self.sup.worker(), 2)
        self.bridge.deliver_outbox()
        card = self.latest_card("Answer not verified")
        self.assertIn("may have reached Codex, but no verified answer came back", card["text"])
        self.assertIn("plan approval decision is unchanged: nothing was approved, revised, or advanced", card["text"])
        self.assertIn("Retry reading answer: read the agent's finished turn once more. No message is sent.", card["text"])
        self.assertIn("Return to decision: drop this question", card["text"])
        self.assertIn("Request revision: ask the agent for a replacement", card["text"])
        for internal in ("settled transcript", "delivery", "turn_id", state["run_id"][:8]):
            self.assertNotIn(internal, card["text"], internal)
        wait_buttons = self.buttons_of(card)
        self.assertEqual(set(wait_buttons), {"Retry reading answer", "Return to decision", "Request revision", "Status"})
        turn = self.state()["agent_followup"]["followup_turn_id"]
        for label in ("Retry reading answer", "Return to decision", "Request revision"):
            record = json.loads((self.tg_paths.interactions_dir / f"{wait_buttons[label]}.json").read_text())
            self.assertEqual(record["followup_turn_id"], turn, label)
        # /status shows the same explanation and controls; resume is refused
        self.bridge.handle_update(message(62, "/status"))
        self.assertIn("Answer not verified", self.texts()[-1])
        self.assertIn("Retry reading answer", self.buttons_of(self.api.sent[-1]))
        self.assertEqual(self.bridge.handle_update(message(63, "/resume"))["result"], "enqueued")
        self.assertFalse(self.sup.process_inbox()[-1]["ok"])
        # 1) retry reading: the transcript now carries the answer -> restored, once
        prompt = self.sup.build_followup_prompt(self.state(), turn)
        self.herdr.outputs["codex-main"] = "late\n" + FakeHerdr.followup_reply(prompt, {"summary": "late_but_verified"})
        prompts = len(self.herdr.prompts)
        self.assertEqual(self.bridge.handle_update(callback(64, wait_buttons["Retry reading answer"]))["result"], "enqueued")
        self.assertEqual(self.bridge.handle_update(callback(65, wait_buttons["Retry reading answer"]))["rejected"], "Already used (one-time action).")
        self.assertEqual(self.sup.worker(), 4)
        self.assertEqual(len(self.herdr.prompts), prompts)
        self.bridge.deliver_outbox()
        self.assertIn("late_but_verified", self.latest_card("Codex answered")["text"])
        self.assertIn("pending any more", self.bridge.handle_update(callback(66, wait_buttons["Return to decision"]))["rejected"].lower())
        # 2) return to decision (fresh failure): restored controls come with the confirmation
        self.reset_fixture()
        state, buttons = self.deliver_plan_card()
        self.bridge.handle_update(callback(70, buttons["Ask agent"]))
        self.bridge.handle_update(message(71, "Why?"))
        self.herdr.responses = [{"output": "prose\n", "status": "idle"}]
        self.assertEqual(self.sup.worker(), 2)
        self.bridge.deliver_outbox()
        wait_buttons = self.buttons_of(self.latest_card("Answer not verified"))
        self.assertEqual(self.bridge.handle_update(callback(72, wait_buttons["Return to decision"]))["result"], "enqueued")
        self.assertEqual(self.sup.worker(), 4)
        self.assertEqual(self.state()["agent_followup"]["status"], "ABANDONED")
        self.bridge.deliver_outbox()
        confirmation = self.latest_card("Return to decision accepted")
        self.assertIn("returned to the plan approval decision", confirmation["text"])
        self.assertEqual(set(self.buttons_of(confirmation)), {"View Plan", "Approve", "Request Revision", "Reject", "Ask agent"})
        self.assertNotIn("Codex answered", "\n".join(self.texts()))
        # 3) request revision from the wait: voids the gate through the ordinary revision path
        self.reset_fixture()
        state, buttons = self.deliver_plan_card()
        self.bridge.handle_update(callback(80, buttons["Ask agent"]))
        self.bridge.handle_update(message(81, "Why?"))
        self.herdr.responses = [{"output": "prose\n", "status": "idle"}]
        self.assertEqual(self.sup.worker(), 2)
        self.bridge.deliver_outbox()
        wait_buttons = self.buttons_of(self.latest_card("Answer not verified"))
        self.assertEqual(self.bridge.handle_update(callback(82, wait_buttons["Request revision"]))["callback"], "revise_intent")
        self.assertEqual(self.bridge.handle_update(message(83, "rewrite the scope"))["result"], "enqueued")
        self.assertEqual(self.pending_inbox()[0]["gate_id"], state["pending_gate"]["gate_id"])
        self.herdr.responses = [{"v2": ("plan", "human", "plan_approval", str(self.plan_payload()))}]
        self.assertEqual(self.sup.worker(), 4)
        after = self.state()
        self.assertEqual((after["agent_followup"]["status"], after["gate_history"][-1]["status"], after["pending_gate"]["sequence"]), ("ABANDONED", "revision_requested", state["pending_gate"]["sequence"] + 1))
        self.assertIn("rewrite the scope", self.herdr.prompts[-1][1])

    def test_f1_ask_before_the_gate_card_is_delivered_yields_exactly_one_decision_card(self) -> None:
        state = self.plan_gate()  # PLAN_APPROVAL_REQUIRED is still undelivered
        self.assertEqual(self.bridge.handle_update(message(90, "/ask-agent Why this scope?"))["result"], "enqueued")
        self.sup.process_inbox()
        self.assertEqual(self.bridge.deliver_outbox()["superseded"], 0)  # held, not superseded, while the question is out
        self.assertNotIn("Plan ready for approval", "\n".join(self.texts()))
        self.herdr.responses = [{"followup": {"summary": "scope_is_fine"}}]
        self.assertEqual(self.sup.worker(), 4)
        self.bridge.deliver_outbox()
        cards = [m for m in self.api.sent if m["reply_markup"] and "Approve" in self.buttons_of(m)]
        self.assertEqual(len(cards), 1, "exactly one live decision card after the answer")
        self.assertIn("Codex answered", cards[0]["text"])
        self.assertNotIn("Plan ready for approval", "\n".join(self.texts()))
        held = [hs.load_json(p, label="d") for p in (self.paths.outbox_dir / "delivery").glob("*.json")]
        self.assertEqual([d["status"] for d in held if d.get("reason") == "decision controls delivered with the answer card"], ["superseded"])
        # the answer card's controls are live and bound to the still-pending gate
        buttons = self.buttons_of(cards[0])
        self.assertEqual(self.bridge.handle_update(callback(91, buttons["Approve"]))["result"], "enqueued")
        self.assertTrue(self.sup.process_inbox()[-1]["ok"])
        self.assertEqual(self.state()["approved_plan"]["gate_id"], state["pending_gate"]["gate_id"])
        # a second delivery pass sends nothing more for that gate
        self.assertEqual(self.bridge.deliver_outbox()["superseded"], 0)
        self.assertEqual(len([m for m in self.api.sent if m["reply_markup"] and "Approve" in self.buttons_of(m)]), 1)

    def test_f1_missing_result_ask_before_delivery_yields_one_recovery_card(self) -> None:
        self.write_plan()
        self.assertEqual(self.start_gated([{"output": "prose only\n", "status": "idle"}]), 2)
        self.assertEqual(self.bridge.handle_update(message(95, "/ask-agent What happened?"))["result"], "enqueued")
        self.sup.process_inbox()
        self.herdr.responses = [{"followup": {"summary": "I_forgot_the_block"}}]
        self.assertEqual(self.sup.worker(), 2)
        self.bridge.deliver_outbox()
        cards = [m for m in self.api.sent if m["reply_markup"] and "Retry routing result" in self.buttons_of(m)]
        self.assertEqual(len(cards), 1)
        self.assertIn("Codex answered", cards[0]["text"])

    def test_f2_credential_questions_are_refused_before_the_journal_and_inbox(self) -> None:
        self.plan_gate()
        self.assertEqual(self.bridge.handle_update(message(96, "/ask-agent Authorization: Bearer sk-test-secret-material-1234567890 failed?"))["result"], "credential_refused")
        self.assertEqual(self.pending_inbox(), [])
        self.assertIn("Nothing was sent or stored", self.texts()[-1])
        self.assertEqual(self.bridge.handle_update(message(97, "/ask-agent"))["result"], "intent")
        self.assertEqual(self.bridge.handle_update(message(98, "postgres://app:s3cretpw@db/prod fails?"))["result"], "credential_refused")
        self.assertEqual(self.pending_inbox(), [])
        for path in list(self.tg_paths.updates_dir.glob("*.json")) + list(self.tg_paths.intents_dir.glob("*.json")):
            self.assertNotIn("s3cretpw", path.read_text())
            self.assertNotIn("sk-test-secret-material", path.read_text())
        self.assertEqual(self.bridge.handle_update(message(99, "/ask-agent how is the bot token read?"))["result"], "enqueued")
