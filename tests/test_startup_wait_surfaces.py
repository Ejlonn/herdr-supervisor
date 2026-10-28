"""The surfaces around a human startup wait, through the REAL consumers: the Telegram bridge writes bound commands, the worker's inbox consumer applies them
under the recorded identity, the CLI runs the same command handler under the worker lock, and a restored provider inside a running task waits the same way.
Isolated fakes only: no live Telegram, network, pane, process or model."""

from __future__ import annotations

import json
import uuid

import herdr_command
import herdr_core
import herdr_startup as hst
from test_command_cli import CliCase
from test_owner_recovery import OwnerRecoveryCase
from test_startup_wait import CLAUDE_EXE, EXE, update_menu_screen
from test_telegram import CHAT, OWNER, TelegramCase, callback, message
from v2_fixtures import CODEX_SESSION, V2Case, hs

CODEX_ONLY = {"policy": "fresh-codex", "profiles": {"codex": "default", "claude": "default"}}


class Seams:
    """Test seams on every supervisor of a case: provider executables and process start times are fixture values, never read from the host."""

    exe = dict(EXE)

    def make_supervisor(self) -> "hs.Supervisor":
        sup = super().make_supervisor()  # type: ignore[misc]
        sup.startup_executable_record = lambda command: dict(CLAUDE_EXE if "claude" in str(command) else self.exe)
        sup.startup_process_ticks = lambda pid: None
        sup.startup_codex_contract = lambda: dict(sup._codex_thread_contract)
        return sup


class TelegramStartupTests(Seams, TelegramCase):
    def setUp(self) -> None:
        super().setUp()
        self.prep = str(uuid.uuid4())

    def buttons_of(self, card: dict) -> dict[str, str]:
        return {b["text"]: b["callback_data"] for row in (card["reply_markup"] or {"inline_keyboard": []})["inline_keyboard"] for b in row}

    def open_wait(self) -> dict:
        self.herdr.startup_block = "blocked_agent"
        self.herdr.screens["w9:p1"] = update_menu_screen()
        with self.assertRaises(herdr_core.StartupWaitRequired):
            self.sup.prepare_task_sessions(CODEX_ONLY, preparation_id=self.prep)
        return hst.list_waits(self.paths)[0]

    def card(self, text: str) -> dict:
        self.bridge.deliver_outbox()
        return next(m for m in reversed(self.api.sent) if text in m["text"])

    def counts(self) -> dict:
        return {"starts": len(self.herdr.starts), "splits": len(self.herdr.splits), "prompts": len(self.herdr.prompts), "bootstrap": len(self.herdr.bootstrap_prompts), "keys": len(self.herdr.sent_keys), "runs": len(self.herdr.pane_runs)}

    def test_the_card_names_the_pane_states_what_is_never_done_and_offers_only_bound_controls(self) -> None:
        wait = self.open_wait()
        card = self.card("start needs you")
        self.assertIn("Codex start needs you in pane w9:p1", card["text"])
        self.assertIn("Handle it yourself in THAT pane", card["text"])
        self.assertIn("never presses a key, updates a package or grants a permission", card["text"])
        self.assertIn("recognized from a retained capture", card["text"])
        self.assertIn(f"cancel-startup --wait-id {wait['wait_id']}", card["text"])
        self.assertNotIn(wait["executable"]["sha256"], card["text"])
        buttons = self.buttons_of(card)
        self.assertEqual(set(buttons), {"I handled it — Continue", "Cancel start"})
        for label, action in (("I handled it — Continue", "startup_continue"), ("Cancel start", "startup_cancel")):
            record = json.loads((self.tg_paths.interactions_dir / f"{buttons[label]}.json").read_text())
            self.assertEqual((record["action"], record["wait_id"], record["preparation_id"], record["provider"], record["pane_id"]), (action, wait["wait_id"], self.prep, "codex", "w9:p1"))
            self.assertEqual((record["owner_user_id"], record["chat_id"], record["consumed"]), (OWNER, CHAT, False))
        self.assertEqual(len(self.pending_inbox()), 0, "showing the card sends nothing")
        self.bridge.handle_update(message(1, "/status"))
        self.assertIn("Startup wait: Codex pane w9:p1 WAITING", self.texts()[-1])

    def test_continue_runs_through_the_worker_consumer_under_the_recorded_identity_and_nothing_is_relaunched(self) -> None:
        wait = self.open_wait()
        buttons = self.buttons_of(self.card("start needs you"))
        before = self.counts()
        cont = buttons["I handled it — Continue"]
        # wrong user / wrong chat / malformed tokens never reach the queue
        self.assertEqual(self.bridge.handle_update(callback(10, cont, user=999))["rejected"], "wrong_user")
        self.assertEqual(self.bridge.handle_update(callback(11, cont, chat=777))["rejected"], "wrong_chat")
        self.assertEqual(self.bridge.handle_update(callback(12, "short"))["rejected"], "malformed_callback")
        self.assertEqual(len(self.pending_inbox()), 0)
        # tapped before the human handled the pane: queued, then refused by the consumer (still blocked), card offers a retry
        self.assertEqual(self.bridge.handle_update(callback(13, cont))["result"], "enqueued")
        command = self.pending_inbox()[0]
        self.assertEqual((command["action"], command["wait_id"], command["preparation_id"], command["actor"], command["chat_id"], command["source"]), ("startup_continue", wait["wait_id"], self.prep, f"telegram:{OWNER}", CHAT, "telegram"))
        self.assertEqual(self.bridge.handle_update(callback(14, cont))["rejected"], "Already used (one-time action).")
        results = self.sup.process_inbox()
        self.assertEqual([(r["ok"], r["kind"]) for r in results], [(False, "blocker_present")])
        self.assertEqual(self.counts(), before)
        retry = self.card("Startup not continued")
        self.assertIn("Nothing was sent", retry["text"])
        retry_buttons = self.buttons_of(retry)
        self.assertEqual(set(retry_buttons), {"Try Continue again", "Cancel start"})
        # the human handles the pane; the retry card continues the SAME process
        self.herdr.unblock("w9:p1")
        self.assertEqual(self.bridge.handle_update(callback(15, retry_buttons["Try Continue again"]))["result"], "enqueued")
        results = self.sup.process_inbox()
        self.assertEqual([(r["ok"], r["kind"]) for r in results], [(True, "resolved")])
        self.assertEqual(self.counts(), before, "zero launches, prompts, bootstraps or keys")
        final = hst.read_wait(self.paths, wait["wait_id"])
        self.assertEqual((final["status"], final["continued_by"]), ("RESOLVED", f"telegram:{OWNER}"))
        done = self.card("Startup handled")
        self.assertIn("No original start request is stored", done["text"])
        self.assertNotIn("Start the original task", " ".join(b["text"] for row in (done["reply_markup"] or {"inline_keyboard": []})["inline_keyboard"] for b in row), "no button is offered for a request that does not exist")
        completed = [json.loads(p.read_text()) for p in sorted((self.paths.inbox_dir / "completed").glob("*.json"))]
        self.assertEqual([c["kind"] for c in completed], ["blocker_present", "resolved"])
        # every earlier card is inert now
        for number, token in ((20, buttons["Cancel start"]), (21, retry_buttons["Cancel start"])):
            self.assertIn("resolved", self.bridge.handle_update(callback(number, token))["rejected"])
        self.assertEqual(len(self.pending_inbox()), 0)
        journal = json.loads((self.paths.session_preparations_dir / f"{self.prep}.json").read_text())
        self.assertEqual(journal["status"], "OWNERSHIP_BOUND")

    def test_cancel_through_the_card_makes_the_start_inert_and_a_late_continue_is_refused(self) -> None:
        wait = self.open_wait()
        buttons = self.buttons_of(self.card("start needs you"))
        before = self.counts()
        self.assertEqual(self.bridge.handle_update(callback(30, buttons["Cancel start"]))["result"], "enqueued")
        self.assertEqual([(r["ok"], r["kind"]) for r in self.sup.process_inbox()], [(True, "cancelled")])
        self.assertEqual(hst.read_wait(self.paths, wait["wait_id"])["status"], "CANCELLED")
        self.assertEqual(json.loads((self.paths.session_preparations_dir / f"{self.prep}.json").read_text())["status"], "ABANDONED")
        self.assertEqual(self.counts(), before)
        self.assertIn("cancelled", self.bridge.handle_update(callback(31, buttons["I handled it — Continue"]))["rejected"])
        self.assertIn("Startup wait cancelled", self.card("Startup wait cancelled")["text"])

    def test_a_card_for_another_start_or_an_expired_wait_is_inert_and_a_duplicate_update_queues_once(self) -> None:
        wait = self.open_wait()
        buttons = self.buttons_of(self.card("start needs you"))
        record_path = self.tg_paths.interactions_dir / f"{buttons['I handled it — Continue']}.json"
        record = json.loads(record_path.read_text())
        record["preparation_id"] = str(uuid.uuid4())  # a card from a different start
        record_path.write_text(json.dumps(record))
        self.assertIn("different start", self.bridge.handle_update(callback(40, buttons["I handled it — Continue"]))["rejected"])
        record["preparation_id"] = self.prep
        record_path.write_text(json.dumps(record))
        self.assertEqual(self.bridge.handle_update(callback(41, buttons["I handled it — Continue"]))["result"], "enqueued")
        self.assertEqual(self.bridge.handle_update(callback(41, buttons["I handled it — Continue"]))["rejected"], "Already used (one-time action).")
        self.assertEqual(len(self.pending_inbox()), 1)
        self.sup.process_inbox()
        # time passes beyond the deadline: the remaining card cannot revive the wait
        self.clock.current = wait["deadline_unix"] + 2
        self.assertIn("expired", self.bridge.handle_update(callback(42, buttons["Cancel start"]))["rejected"])
        self.assertEqual(len(self.pending_inbox()), 0)

    def test_shadow_mode_queues_nothing(self) -> None:
        wait = self.open_wait()
        buttons = self.buttons_of(self.card("start needs you"))
        shadow = self.make_bridge(mode="shadow")
        self.assertEqual(shadow.handle_update(callback(50, buttons["I handled it — Continue"]))["result"], "shadow")
        self.assertEqual(len(self.pending_inbox()), 0)
        self.assertEqual(hst.read_wait(self.paths, wait["wait_id"])["status"], "WAITING")

    def test_the_consumer_refuses_a_command_for_a_different_start_even_if_it_was_queued(self) -> None:
        wait = self.open_wait()
        self.sup.enqueue_command({"request_id": "forged-cross-start-1", "action": "startup_continue", "wait_id": wait["wait_id"], "preparation_id": str(uuid.uuid4()), "actor": f"telegram:{OWNER}"})
        results = self.sup.process_inbox()
        self.assertEqual((results[0]["ok"], results[0]["kind"]), (False, "cross_run"))
        self.assertEqual(hst.read_wait(self.paths, wait["wait_id"])["status"], "WAITING")
        for payload in ({"wait_id": 5}, {}):
            self.sup.enqueue_command({"request_id": f"forged-bad-{len(payload)}-1", "action": "startup_cancel", "actor": "x", **payload})
        self.assertEqual([r["ok"] for r in self.sup.process_inbox()], [False, False])
        self.assertEqual(hst.read_wait(self.paths, wait["wait_id"])["status"], "WAITING")

    def test_the_new_commands_are_only_accepted_with_a_valid_request_id(self) -> None:
        with self.assertRaises(hs.SupervisorError):
            self.sup.enqueue_command({"request_id": "x", "action": "startup_continue", "wait_id": str(uuid.uuid4())})
        with self.assertRaises(hs.SupervisorError):
            self.sup.enqueue_command({"request_id": "valid-id-12345", "action": "startup_launch", "wait_id": str(uuid.uuid4())})


class CliStartupTests(Seams, CliCase):
    def setUp(self) -> None:
        super().setUp()
        case = self
        herdr_command.Supervisor = lambda paths, config, herdr, **kwargs: case.make_supervisor()  # type: ignore[assignment]
        self.prep = str(uuid.uuid4())

    def open_wait(self, selection: dict = CODEX_ONLY) -> dict:
        self.herdr.startup_block = "blocked_agent"
        self.herdr.screens["w9:p1"] = update_menu_screen()
        with self.assertRaises(herdr_core.StartupWaitRequired):
            self.sup.prepare_task_sessions(selection, preparation_id=self.prep)
        return hst.list_waits(self.paths)[0]

    def test_status_names_the_wait_and_both_commands_report_exit_codes_and_change_only_what_they_say(self) -> None:
        wait = self.open_wait()
        code, out, _ = self.run_cli("status")
        self.assertEqual(code, 0)
        self.assertIn("Startup wait: codex pane w9:p1 WAITING (codex_update_menu, recognized)", out)
        self.assertIn(f"continue-startup --wait-id {wait['wait_id']}", out)
        self.assertIn("Nothing is pressed or sent for you", out)
        code, out, _ = self.run_cli("status", "--json")
        self.assertEqual(json.loads(out)["startup_waits"][0]["wait_id"], wait["wait_id"])
        starts = len(self.herdr.starts)
        code, out, err = self.run_cli("continue-startup", "--wait-id", wait["wait_id"])
        self.assertEqual(code, 2, out + err)
        self.assertIn("blocker_present", out)
        self.assertEqual(len(self.herdr.starts), starts)
        self.herdr.unblock("w9:p1")
        code, out, err = self.run_cli("continue-startup", "--wait-id", wait["wait_id"])
        self.assertEqual(code, 0, out + err)
        self.assertIn("No original start request is stored", out)
        final = hst.read_wait(self.paths, wait["wait_id"])
        self.assertEqual(final["status"], "RESOLVED")
        self.assertTrue(final["continued_by"].startswith("cli:"))
        self.assertEqual(len(self.herdr.starts), starts)
        code, out, _ = self.run_cli("continue-startup", "--wait-id", wait["wait_id"])
        self.assertEqual(code, 2)
        self.assertIn("RESOLVED", out)

    def test_cancel_startup_and_unknown_ids(self) -> None:
        wait = self.open_wait()
        code, out, _ = self.run_cli("continue-startup", "--wait-id", str(uuid.uuid4()))
        self.assertEqual(code, 2)
        self.assertIn("unknown startup wait", out)
        code, out, _ = self.run_cli("continue-startup", "--wait-id", "not-a-uuid")
        self.assertEqual(code, 2)
        code, out, _ = self.run_cli("cancel-startup", "--wait-id", wait["wait_id"])
        self.assertEqual(code, 0)
        self.assertIn("no pane, session or process was closed", out)
        self.assertEqual(hst.read_wait(self.paths, wait["wait_id"])["status"], "CANCELLED")
        code, out, _ = self.run_cli("cancel-startup", "--wait-id", wait["wait_id"])
        self.assertEqual(code, 2)

    def test_a_cli_run_that_meets_a_not_ready_start_exits_with_the_wait_and_the_exact_next_commands(self) -> None:
        self.herdr.startup_block = "blocked_agent"
        self.herdr.screens["w9:p1"] = update_menu_screen()
        self.herdr.responses = []
        code, out, err = self.run_cli("run", "--start", "codex", "--session-policy", "fresh-codex", "Add the widget")
        self.assertEqual(code, 2, out + err)
        self.assertIn("Codex started in pane", err)
        self.assertIn("continue-startup --wait-id", err)
        self.assertIn("cancel-startup --wait-id", err)
        waits = hst.list_waits(self.paths)
        self.assertEqual(len(waits), 1)
        self.assertEqual(waits[0]["status"], "WAITING")
        self.assertEqual(self.herdr.prompts, [], "no task was delivered")
        self.assertEqual(len(self.herdr.sent_keys), 0)


class RunStartupTests(Seams, OwnerRecoveryCase):
    """A restored provider inside a RUNNING task: the same wait, bound to the run, the recorded pane and the recorded native session."""

    def to_restore_wait(self) -> dict:
        self.to_approved()
        del self.herdr.agents["codex-main"]  # the session is gone from Herdr; its recorded pane is free: the supervisor restores it in place
        self.herdr.startup_block = "start_error"
        self.herdr.startup_block_for = "codex-main"
        self.herdr.screens["w3:p2"] = update_menu_screen()
        self.assertEqual(self.worker(), 2)
        return self.state()

    def run_wait(self) -> dict:
        waits = hst.list_waits(self.paths)
        self.assertEqual(len(waits), 1)
        return waits[0]

    def command(self, action: str, wait_id: str, **extra) -> dict:
        with self.sup.store.transaction():
            state = self.sup.store.read_state()
            return self.sup.apply_command(state, {"action": action, "wait_id": wait_id, "actor": "telegram:1", **extra})

    def test_a_restored_provider_that_is_not_ready_waits_for_the_human_and_wakes_nothing(self) -> None:
        before_prompts = None
        self.to_approved()
        before_prompts = len(self.herdr.prompts)
        del self.herdr.agents["codex-main"]
        self.herdr.startup_block, self.herdr.startup_block_for = "start_error", "codex-main"
        self.herdr.screens["w3:p2"] = update_menu_screen()
        self.assertEqual(self.worker(), 2)
        state, wait = self.state(), self.run_wait()
        self.assertEqual((state["supervisor_state"], state["wait_user_requires_action"]), ("WAIT_USER", True))
        self.assertEqual((wait["context"], wait["run_id"], wait["provider"], wait["pane_id"], wait["phase"], wait["site"], wait["status"]), ("run", state["run_id"], "codex", "w3:p2", "RUN_RESTORE", "run_restore_not_ready", "WAITING"))
        self.assertEqual((wait["binding"]["session_id"], wait["classification"]["kind"]), (CODEX_SESSION, "codex_update_menu"))
        self.assertIn(wait["wait_id"], state["wait_user_reason"])
        self.assertEqual((len(self.herdr.prompts), len(self.herdr.starts), len(self.herdr.sent_keys)), (before_prompts, 1, 0))
        self.assertEqual(len([e for e in self.events() if e["type"] == "STARTUP_WAIT_REQUIRED"]), 1)
        self.assertEqual(json.loads(self.paths.owners_file.read_text())["codex"], {"pane_id": "w3:p2", "session_id": CODEX_SESSION})
        # a restart keeps the wait, wakes nothing and restores nothing a second time
        starts = len(self.herdr.starts)
        self.assertEqual(self.worker(), 2)
        self.assertEqual((len(self.herdr.starts), len(self.herdr.prompts)), (starts, before_prompts))
        # a plain resume cannot skip the identity and contract checks
        with self.sup.store.transaction():
            st = self.sup.store.read_state()
            with self.assertRaisesRegex(hs.SupervisorError, "startup handling"):
                self.sup.apply_command(st, {"action": "resume", "run_id": st["run_id"]})
        self.assertEqual(hst.read_wait(self.paths, wait["wait_id"])["status"], "WAITING")

    def test_continue_after_skip_resumes_the_same_run_session_without_a_second_restore(self) -> None:
        state = self.to_restore_wait()
        wait = self.run_wait()
        starts = len(self.herdr.starts)
        # the wrong run id, or an unknown wait, changes nothing
        self.assertEqual(self.command("startup_continue", wait["wait_id"], run_id=str(uuid.uuid4()))["kind"], "cross_run")
        self.assertEqual(self.command("startup_continue", str(uuid.uuid4()), run_id=state["run_id"])["kind"], "unknown_wait")
        refused = self.command("startup_continue", wait["wait_id"], run_id=state["run_id"])
        self.assertEqual((refused["ok"], refused["kind"]), (False, "blocker_present"), refused)
        self.assertEqual(self.state()["supervisor_state"], "WAIT_USER")
        self.herdr.unblock("w3:p2")
        done = self.command("startup_continue", wait["wait_id"], run_id=state["run_id"])
        self.assertEqual((done["ok"], done["kind"]), (True, "resolved"), done)
        after = self.state()
        self.assertEqual((after["supervisor_state"], after["wait_user_requires_action"]), ("RUNNING", False))
        self.assertEqual(len(self.herdr.starts), starts, "the session is not restored again")
        self.assertEqual(hst.read_wait(self.paths, wait["wait_id"])["status"], "RESOLVED")
        self.assertEqual(self.command("startup_continue", wait["wait_id"], run_id=state["run_id"])["kind"], "not_waiting")
        self.assertEqual(len([e for e in self.events() if e["type"] == "STARTUP_WAIT_RESULT"]), 1)

    def test_a_replaced_process_or_executable_or_native_session_is_never_adopted(self) -> None:
        state = self.to_restore_wait()
        wait = self.run_wait()
        self.herdr.unblock("w3:p2")
        process = self.herdr.processes["w3:p2"]["foreground_processes"][0]
        process["pid"] += 77
        replaced = self.command("startup_continue", wait["wait_id"], run_id=state["run_id"])
        self.assertEqual((replaced["ok"], replaced["kind"]), (False, "changed_process"))
        self.assertIn("explicit restart authority", replaced["message"])
        process["pid"] -= 77
        self.exe = {**EXE, "sha256": "e" * 64}
        self.assertEqual(self.command("startup_continue", wait["wait_id"], run_id=state["run_id"])["kind"], "changed_executable")
        self.exe = dict(EXE)
        self.herdr.agents["codex-main"]["agent_session"] = {"value": "99999999-9999-4999-8999-999999999999"}
        other = self.command("startup_continue", wait["wait_id"], run_id=state["run_id"])
        self.assertEqual((other["ok"], other["kind"]), (False, "blocker_present"), "a different native session is refused by the exact-identity rule")
        self.assertEqual(self.state()["supervisor_state"], "WAIT_USER")
        self.assertEqual(hst.read_wait(self.paths, wait["wait_id"])["status"], "WAITING")

    def test_cancel_stops_supervision_and_preserves_the_session(self) -> None:
        state = self.to_restore_wait()
        wait = self.run_wait()
        result = self.command("startup_cancel", wait["wait_id"], run_id=state["run_id"])
        self.assertEqual((result["ok"], result["kind"]), (True, "cancelled"))
        self.assertEqual(hst.read_wait(self.paths, wait["wait_id"])["status"], "CANCELLED")
        self.assertEqual(self.state()["supervisor_state"], "CANCELLED")
        self.assertIn("codex-main", self.herdr.agents)

    def test_the_run_wait_expires_and_then_only_cancellation_remains(self) -> None:
        state = self.to_restore_wait()
        wait = self.run_wait()
        self.herdr.unblock("w3:p2")
        self.clock.current = wait["deadline_unix"] + 3
        result = self.command("startup_continue", wait["wait_id"], run_id=state["run_id"])
        self.assertEqual((result["ok"], result["kind"]), (False, "expired"))
        self.assertEqual(hst.read_wait(self.paths, wait["wait_id"])["status"], "EXPIRED")
        self.assertEqual(self.state()["supervisor_state"], "WAIT_USER")
        self.assertEqual(self.command("startup_continue", wait["wait_id"], run_id=state["run_id"])["kind"], "not_waiting")

    def test_a_trust_dialog_in_a_restored_pane_is_a_separate_decision(self) -> None:
        self.to_approved()
        del self.herdr.agents["codex-main"]
        self.herdr.startup_block, self.herdr.startup_block_for = "start_error", "codex-main"
        self.herdr.screens["w3:p2"] = "(synthetic) Do you trust this folder?\n› 1. Trust and continue\n  2. (synthetic) No"
        self.assertEqual(self.worker(), 2)
        state = self.state()
        self.assertEqual((state["supervisor_state"], state["wait_user_requires_action"]), ("WAIT_USER", True))
        self.assertIn("separate decision", state["wait_user_reason"])
        self.assertEqual(hst.list_waits(self.paths), [])
        self.assertEqual(len(self.herdr.sent_keys), 0)


class WorkerSweepTests(Seams, V2Case):
    def test_the_worker_start_sweep_expires_overdue_waits_from_files_alone(self) -> None:
        self.herdr.startup_block = "blocked_agent"
        self.herdr.screens["w9:p1"] = update_menu_screen()
        prep = str(uuid.uuid4())
        with self.assertRaises(herdr_core.StartupWaitRequired):
            self.sup.prepare_task_sessions(CODEX_ONLY, preparation_id=prep)
        wait = hst.list_waits(self.paths)[0]
        self.clock.current = wait["deadline_unix"] + 1
        reads, prompts, starts = len(self.herdr.pane_reads), len(self.herdr.prompts), len(self.herdr.starts)
        self.assertEqual(self.make_supervisor().worker(), 0)
        self.assertEqual(hst.read_wait(self.paths, wait["wait_id"])["status"], "EXPIRED")
        self.assertEqual(json.loads((self.paths.session_preparations_dir / f"{prep}.json").read_text())["status"], "ABANDONED")
        self.assertEqual((len(self.herdr.pane_reads), len(self.herdr.prompts), len(self.herdr.starts)), (reads, prompts, starts))


if __name__ == "__main__":
    import unittest

    unittest.main()
