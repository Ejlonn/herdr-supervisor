"""Correction batch CI1-CI3 (independent review of the startup-wait candidate), exercised through the REAL consumers: the supervisor's own event writer and the Telegram bridge
(CI1), the continuation / sweep / retry / effect boundaries and the transaction lock (CI2), and the original pending-start authorization, the inbox consumer and the worker (CI3).
Isolated fakes only: no live Telegram, network, pane, process or model; nothing here starts a real task."""

from __future__ import annotations

import json
import threading
import uuid

import herdr_command
import herdr_core
import herdr_sessions
import herdr_startup as hst
from test_command_cli import CliCase
from test_explicit_launch import ExplicitCase
from test_fresh_session_issue5 import CODEX_ONLY, FRESH_ALL
from test_fresh_session_telegram_issue5 import Issue5TelegramCase
from test_owner_recovery import OwnerRecoveryCase
from test_startup_wait import EXE, FRESH_CLAUDE, WaitCase, update_menu_screen
from test_startup_wait_surfaces import Seams
from test_telegram import OWNER, TelegramCase
from v2_fixtures import hs


class TgWaitCase(WaitCase, TelegramCase):
    """WaitCase (agent-start route) with the real bridge."""

    def setUp(self) -> None:
        super().setUp()
        self.bridge = self.make_bridge()  # status_reader bound to the supervisor WaitCase rebuilt with its seams

    def buttons_of(self, card: dict) -> dict[str, str]:
        return {b["text"]: b["callback_data"] for row in (card["reply_markup"] or {"inline_keyboard": []})["inline_keyboard"] for b in row}

    def cards(self, fragment: str) -> list[dict]:
        self.bridge.deliver_outbox()
        return [m for m in self.api.sent if fragment in m["text"]]

    def crash_after_marker(self) -> dict:
        wait = self.open_codex_wait()
        self.herdr.unblock("w9:p1")

        def crash(*args, **kwargs):
            raise RuntimeError("crash after CONTINUING persisted")

        self.sup.prepare_task_sessions = crash
        with self.assertRaises(RuntimeError):
            self.continue_(wait["wait_id"])
        self.assertEqual(hst.read_wait(self.paths, wait["wait_id"])["status"], "CONTINUING")
        self.sup = self.make_supervisor()
        return wait

    def event_files(self) -> dict[str, bytes]:
        directory = self.paths.outbox_dir / "events"
        return {p.name: p.read_bytes() for p in sorted(directory.glob("*.json"))} if directory.exists() else {}

    def sidecar(self, event_id: str) -> dict:
        return json.loads((self.paths.outbox_dir / "delivery" / f"{event_id}.json").read_text())


class TgExplicitCase(ExplicitCase, WaitCase, TelegramCase):
    """The explicit-launch route with the real bridge (the reviewer's collision reproduction)."""

    def runTest(self) -> None:
        pass

    def setUp(self) -> None:
        ExplicitCase.setUp(self)
        self.exe = dict(EXE)
        self.ticks = {}
        self.sup = self.make_supervisor()
        self.bridge = self.make_bridge()

    make_supervisor = WaitCase.make_supervisor
    buttons_of = TgWaitCase.buttons_of
    cards = TgWaitCase.cards
    event_files = TgWaitCase.event_files
    sidecar = TgWaitCase.sidecar

    def open_explicit_wait(self) -> dict:
        self.prep = str(uuid.uuid4())
        self.herdr.startup_block = "undetected"
        self.herdr.screens["w9:p1"] = update_menu_screen()
        with self.assertRaises(herdr_core.StartupWaitRequired) as caught:
            self.prepare(CODEX_ONLY, self.prep)
        return hst.read_wait(self.paths, caught.exception.wait_id)

    def block_again_after_alias(self) -> None:
        original = self.herdr.rename_agent

        def blocked(target: str, name: str):
            result = original(target, name)
            for agent in self.herdr.agents.values():
                if agent.get("pane_id") == target:
                    agent["agent_status"] = "blocked"
            self.herdr.screens[target] = update_menu_screen()
            return result

        self.herdr.rename_agent = blocked


class OutboxIdentityTests(TgExplicitCase):
    """CI1: every wait owns an event-sequence block; nothing an earlier wait wrote is ever overwritten or re-sent."""

    def test_a_second_wait_of_one_preparation_gets_its_own_immutable_event_and_is_delivered_after_the_first_was(self) -> None:
        first = self.open_explicit_wait()
        self.assertEqual(len(self.cards("start needs you")), 1)
        (first_event,) = self.startup_events("STARTUP_WAIT_REQUIRED")
        first_name = f"{first_event['event_id']}.json"
        first_bytes = self.event_files()[first_name]
        self.assertEqual(self.sidecar(first_event["event_id"])["status"], "delivered")
        self.herdr.unblock("w9:p1")
        self.block_again_after_alias()
        result = self.continue_(first["wait_id"])
        self.assertEqual((result["ok"], result["kind"]), (True, "waiting_again"), result)
        second = [w for w in self.waits() if w["status"] == "WAITING"][0]
        required = self.startup_events("STARTUP_WAIT_REQUIRED")
        self.assertEqual(len(required), 2, "two immutable REQUIRED events, not one overwritten file")
        self.assertEqual(len({e["event_id"] for e in required}), 2)
        self.assertEqual(self.event_files()[first_name], first_bytes, "the first event's bytes are untouched")
        self.assertEqual([e["data"]["wait"]["wait_id"] for e in required], [first["wait_id"], second["wait_id"]])
        newest = next(e for e in required if e["data"]["wait"]["wait_id"] == second["wait_id"])
        self.assertEqual(self.sidecar(newest["event_id"])["status"], "pending", "the second notification has its own, undelivered sidecar")
        self.assertEqual(self.sidecar(first_event["event_id"])["status"], "delivered")
        sent_before = len(self.api.sent)
        cards = self.cards(second["wait_id"][:8]) if False else self.cards("start needs you")
        self.assertEqual(len(cards), 2, "the bridge delivered a second card through the real outbox")
        self.assertIn(second["wait_id"], cards[-1]["text"])
        self.assertEqual(self.sidecar(newest["event_id"])["status"], "delivered")
        self.assertGreater(len(self.api.sent), sent_before)
        self.assertEqual(set(self.buttons_of(cards[-1])), {"I handled it — Continue", "Cancel start"})
        tokens = [json.loads(p.read_text()) for p in self.tg_paths.interactions_dir.glob("*.json")]
        self.assertEqual({t["wait_id"] for t in tokens if t["action"] == "startup_continue"}, {first["wait_id"], second["wait_id"]})

    def test_repeating_a_refusal_produces_distinct_result_events_that_are_each_delivered(self) -> None:
        wait = self.open_explicit_wait()
        self.assertEqual(len(self.cards("start needs you")), 1)
        for _ in range(3):
            self.assertEqual(self.continue_(wait["wait_id"])["kind"], "blocker_present")
        results = self.startup_events("STARTUP_WAIT_RESULT")
        self.assertEqual(len({e["event_id"] for e in results}), 3)
        self.assertEqual(len(self.cards("Startup not continued")), 3)
        self.assertEqual(len({e["sequence"] for e in self.sup.list_events() if e["run_id"] == self.prep}), len([e for e in self.sup.list_events() if e["run_id"] == self.prep]), "sequences are unique within the preparation")

    def test_a_crash_after_the_sequence_was_written_ahead_but_before_the_event_is_recovered_without_a_duplicate(self) -> None:
        self.prep = str(uuid.uuid4())
        self.herdr.startup_block = "undetected"
        self.herdr.screens["w9:p1"] = update_menu_screen()
        real = self.sup.emit_event
        calls = []

        def crash(state, event_type, data, **kwargs):
            calls.append(event_type)
            raise RuntimeError("worker killed between the write-ahead sequence and the event")

        self.sup.emit_event = crash
        with self.assertRaises(RuntimeError):
            self.prepare(CODEX_ONLY, self.prep)
        (wait,) = self.waits()
        self.assertEqual((wait["notified"], wait["required_event_sequence"] is not None, self.startup_events()), (False, True, []))
        self.sup.emit_event = real
        self.sup = self.make_supervisor()  # restart
        self.assertEqual(self.sup.sweep_startup_waits(), [wait["wait_id"]])
        (event,) = self.startup_events("STARTUP_WAIT_REQUIRED")
        self.assertEqual(event["sequence"], wait["required_event_sequence"], "the recorded sequence is reused, not a new one")
        self.assertTrue(hst.read_wait(self.paths, wait["wait_id"])["notified"])
        self.assertEqual(self.sup.sweep_startup_waits(), [], "idempotent")
        self.assertEqual(len(self.cards("start needs you")), 1)

    def test_a_crash_after_the_event_but_before_the_bookkeeping_re_announces_the_same_event_and_never_resends_a_delivered_card(self) -> None:
        wait = self.open_explicit_wait()
        self.assertEqual(len(self.cards("start needs you")), 1)
        (event,) = self.startup_events("STARTUP_WAIT_REQUIRED")
        stored = hst.read_wait(self.paths, wait["wait_id"])
        stored["notified"] = False  # the process died after writing the event, before recording it
        hst.write_wait(self.paths, stored)
        self.sup = self.make_supervisor()
        self.assertEqual(self.sup.sweep_startup_waits(), [wait["wait_id"]])
        self.assertEqual(len(self.startup_events("STARTUP_WAIT_REQUIRED")), 1, "same event id, one file")
        self.assertEqual(self.sidecar(event["event_id"])["status"], "delivered", "the delivered sidecar is not reset")
        sent = len(self.api.sent)
        self.bridge.deliver_outbox()
        self.assertEqual(len(self.api.sent), sent, "no second Telegram message")

    def test_a_wait_event_never_shares_an_id_with_another_event_of_the_same_run_sequence_and_type(self) -> None:
        wait = self.open_explicit_wait()
        (event,) = self.startup_events("STARTUP_WAIT_REQUIRED")
        pseudo = lambda: {"run_id": self.prep, "supervisor_state": "X", "event_sequence": event["sequence"] - 1, "pending_gate": None}  # noqa: E731
        stranger = self.sup.emit_event(pseudo(), "STARTUP_WAIT_REQUIRED", {"x": 1})
        self.assertNotEqual(stranger, event["event_id"], "same run id, sequence and type, but not this wait's event")
        again = self.sup.emit_event(pseudo(), "STARTUP_WAIT_REQUIRED", {"x": 1}, identity=f"{self.prep}:startup-wait:{wait['wait_id']}")
        self.assertEqual(again, event["event_id"], "the same wait, sequence and type is the same event (idempotent)")
        self.assertEqual(event["data"]["wait"]["wait_id"], wait["wait_id"])

    def test_two_providers_of_one_fresh_all_preparation_never_share_an_event(self) -> None:
        case = TgWaitCase()
        case.setUp()
        self.addCleanup(case.tearDown)
        case.prep = str(uuid.uuid4())
        case.herdr.startup_block, case.herdr.startup_block_for = "blocked_agent", "codex-run-"
        case.herdr.screens["w9:p1"] = update_menu_screen()
        with case.assertRaises(herdr_core.StartupWaitRequired):
            case.prepare(FRESH_ALL, case.prep)
        codex = case.only_wait()
        case.bridge.deliver_outbox()
        case.herdr.unblock("w9:p1")
        case.herdr.startup_block, case.herdr.startup_block_for = "start_error", "claude-run-"  # the second provider blocks when it is started
        case.herdr.screens["w9:p2"] = update_menu_screen()
        result = case.continue_(codex["wait_id"])
        self.assertEqual((result["ok"], result["kind"]), (True, "waiting_again"), result)
        waits = case.waits()
        self.assertEqual(sorted(w["provider"] for w in waits), ["claude", "codex"])
        required = case.startup_events("STARTUP_WAIT_REQUIRED")
        self.assertEqual(len({e["event_id"] for e in required}), 2)
        cards = case.cards("start needs you")
        self.assertEqual(sorted(("Claude" if "Claude start" in c["text"] else "Codex") for c in cards), ["Claude", "Codex"])
        self.assertEqual(len({w["ordinal"] for w in waits}), 2)


class DeadlineTests(TgWaitCase):
    """CI2: the original deadline holds through slow checks, a crash after the CONTINUING marker, retries, contention and every new effect."""

    def slow_checks(self, seconds: float = 2) -> None:
        original = self.sup.startup_executable_record

        def slow(command):
            self.clock.current += seconds
            return original(command)

        self.sup.startup_executable_record = slow

    def test_checks_that_finish_after_the_deadline_expire_the_wait_and_start_nothing(self) -> None:
        wait = self.open_codex_wait()
        self.herdr.unblock("w9:p1")
        before = self.counts()
        owners = self.paths.owners_file.read_text()
        self.clock.current = wait["deadline_unix"] - 1
        self.slow_checks(2)
        result = self.continue_(wait["wait_id"])
        self.assertEqual((result["ok"], result["kind"]), (False, "expired"), result)
        self.assertGreater(self.clock.current, wait["deadline_unix"])
        final = hst.read_wait(self.paths, wait["wait_id"])
        self.assertEqual(final["status"], "EXPIRED")
        self.assertLessEqual(final["closed_at_unix"], final["deadline_unix"])
        self.assertNotIn("continuing", [h["action"] for h in final["history"]], "no CONTINUING marker is written after the deadline")
        self.assertIsNone(final.get("continued_at_unix"))
        self.assertIn("while the process and contract checks were running", result["message"])
        self.assertEqual(self.journal(self.prep)["status"], "ABANDONED")
        self.assertEqual((self.counts(), self.paths.owners_file.read_text()), (before, owners))
        self.assertEqual(self.continue_(wait["wait_id"])["kind"], "not_waiting")

    def test_the_same_slow_checks_for_a_claude_start_expire_too(self) -> None:
        self.prep = str(uuid.uuid4())
        self.herdr.startup_block = "start_error"
        self.herdr.screens["w9:p1"] = update_menu_screen()
        with self.assertRaises(herdr_core.StartupWaitRequired):
            self.prepare(FRESH_CLAUDE, self.prep)
        wait = self.only_wait()
        self.herdr.unblock("w9:p1")
        self.clock.current = wait["deadline_unix"] - 1
        self.slow_checks(2)
        result = self.continue_(wait["wait_id"])
        self.assertEqual((wait["provider"], result["kind"]), ("claude", "expired"))
        self.assertEqual(self.journal(self.prep)["status"], "ABANDONED")

    def test_exactly_at_the_deadline_is_accepted_and_the_next_instant_is_not(self) -> None:
        wait = self.open_codex_wait()
        self.herdr.unblock("w9:p1")
        self.clock.current = wait["deadline_unix"]
        self.assertTrue(self.continue_(wait["wait_id"])["ok"], "now == deadline is still inside the finite wait")
        case = TgWaitCase()
        case.setUp()
        self.addCleanup(case.tearDown)
        other = case.open_codex_wait()
        case.herdr.unblock("w9:p1")
        case.clock.current = other["deadline_unix"] + 0.001
        result = case.continue_(other["wait_id"])
        self.assertEqual(result["kind"], "not_waiting", "the sweep already expired it")
        self.assertEqual(hst.read_wait(case.paths, other["wait_id"])["status"], "EXPIRED")

    def test_the_continued_time_is_the_time_the_checks_finished_not_the_time_the_callback_arrived(self) -> None:
        wait = self.open_codex_wait()
        self.herdr.unblock("w9:p1")
        self.clock.current = wait["opened_at_unix"] + 100
        self.slow_checks(7)
        self.assertTrue(self.continue_(wait["wait_id"])["ok"])
        self.assertEqual(hst.read_wait(self.paths, wait["wait_id"])["continued_at_unix"], wait["opened_at_unix"] + 107)

    def test_a_crash_after_the_marker_then_a_retry_long_after_the_deadline_is_inert_with_or_without_the_sweep(self) -> None:
        for use_sweep in (True, False):
            with self.subTest(sweep=use_sweep):
                case = TgWaitCase()
                case.setUp()
                self.addCleanup(case.tearDown)
                wait = case.crash_after_marker()
                before, owners = case.counts(), case.paths.owners_file.read_text()
                case.clock.current = wait["deadline_unix"] + 100
                if use_sweep:
                    self.assertEqual(case.sup.sweep_startup_waits(), [wait["wait_id"]])
                    self.assertEqual(hst.read_wait(case.paths, wait["wait_id"])["status"], "EXPIRED")
                observed = []
                real_list = case.herdr.list_agents
                case.herdr.list_agents = lambda: observed.append(1) or real_list()
                with self.assertRaises(hs.SupervisorError) as caught:
                    case.prepare(CODEX_ONLY, case.prep)
                self.assertLessEqual(len(observed), 1, "the retry is stopped at the gate: only the owners preflight read happened; the started agent was not observed again, let alone adopted")
                self.assertTrue(isinstance(caught.exception, herdr_core.StartupWaitRequired) or "abandoned" in str(caught.exception))
                self.assertEqual(case.journal(case.prep)["status"], "ABANDONED")
                self.assertEqual(hst.read_wait(case.paths, wait["wait_id"])["status"], "EXPIRED")
                self.assertEqual((case.counts(), case.paths.owners_file.read_text()), (before, owners), "no launch, bootstrap, delivery or binding")

    def test_a_crash_after_the_marker_then_a_retry_inside_the_deadline_still_completes_the_original_work(self) -> None:
        wait = self.crash_after_marker()
        self.clock.current = wait["deadline_unix"] - 1
        before = self.counts()
        self.assertEqual(self.prepare(CODEX_ONLY, self.prep)["status"], "OWNERSHIP_BOUND")
        self.assertEqual(self.counts(), before)
        self.assertEqual(self.sup.sweep_startup_waits(), [wait["wait_id"]])
        self.assertEqual(hst.read_wait(self.paths, wait["wait_id"])["status"], "RESOLVED")

    def test_completed_history_is_never_rewound_by_a_late_sweep(self) -> None:
        wait = self.crash_after_marker()
        self.assertEqual(self.prepare(CODEX_ONLY, self.prep)["status"], "OWNERSHIP_BOUND")  # finished in time
        self.clock.current = wait["deadline_unix"] + 500
        self.sup.sweep_startup_waits()
        self.assertEqual(hst.read_wait(self.paths, wait["wait_id"])["status"], "RESOLVED")
        self.assertEqual(self.journal(self.prep)["status"], "OWNERSHIP_BOUND")

    def test_duplicate_and_contending_continuations_through_the_transaction_lock_apply_once(self) -> None:
        wait = self.open_codex_wait()
        self.herdr.unblock("w9:p1")
        before = self.counts()
        outcomes: list[dict] = []
        gate = threading.Barrier(4)

        def contender() -> None:
            supervisor = self.make_supervisor()
            gate.wait()
            with supervisor.store.transaction():
                outcomes.append(supervisor.continue_startup(wait["wait_id"], actor="telegram:1"))

        threads = [threading.Thread(target=contender) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        self.assertEqual(len(outcomes), 4)
        self.assertEqual(sorted((o["ok"], o["kind"]) for o in outcomes), [(False, "not_waiting")] * 3 + [(True, "resolved")])
        self.assertEqual(self.counts(), before)
        # the same through the real inbox consumer, one after the other
        self.sup.enqueue_command({"request_id": "dup-continue-0001", "action": "startup_continue", "wait_id": wait["wait_id"], "actor": f"telegram:{OWNER}"})
        self.sup.enqueue_command({"request_id": "dup-continue-0002", "action": "startup_continue", "wait_id": wait["wait_id"], "actor": f"telegram:{OWNER}"})
        self.assertEqual([r["kind"] for r in self.sup.process_inbox()], ["not_waiting", "not_waiting"])

    def effect_wait(self, phase_setup=None):
        case = TgWaitCase()
        case.setUp()
        self.addCleanup(case.tearDown)
        if phase_setup:
            phase_setup(case)
        wait = case.open_codex_wait()
        case.herdr.unblock("w9:p1")
        return case, wait

    def test_the_deadline_passing_before_the_identity_bootstrap_stops_that_send(self) -> None:
        def setup(case):
            case.herdr.hold_identity = True
            case.config["session_start"]["native_identity_bootstrap"] = True
            case.sup = case.make_supervisor()

        case, wait = self.effect_wait(setup)
        case.clock.current = wait["deadline_unix"] - 1
        original = case.herdr.prompt_ack_supported

        def late():
            case.clock.current = wait["deadline_unix"] + 5  # the clock passes the deadline just before the send decision
            return original()

        case.herdr.prompt_ack_supported = late
        result = case.continue_(wait["wait_id"])
        self.assertEqual((result["ok"], result["kind"]), (False, "expired"), result)
        self.assertEqual(case.herdr.bootstrap_prompts, [], "no identity bootstrap after the deadline")
        self.assertEqual((hst.read_wait(case.paths, wait["wait_id"])["status"], case.journal(case.prep)["status"]), ("EXPIRED", "ABANDONED"))

    def test_the_deadline_passing_before_the_alias_rename_stops_the_rename(self) -> None:
        case = TgExplicitCase()
        case.setUp()
        self.addCleanup(case.tearDown)
        wait = case.open_explicit_wait()
        case.herdr.unblock("w9:p1")
        case.clock.current = wait["deadline_unix"] - 1
        original = case.herdr.list_agents
        calls = {"n": 0}

        def late():
            calls["n"] += 1
            if calls["n"] == 3:  # call 1: the continuation's readiness check (in time); call 2: the preparation's preflight; call 3: the alias loop, right before the rename
                case.clock.current = wait["deadline_unix"] + 5
            return original()

        case.herdr.list_agents = late
        result = case.continue_(wait["wait_id"])
        self.assertEqual((result["ok"], result["kind"]), (False, "expired"), result)
        self.assertIn("before naming the agent", result["message"])
        self.assertEqual(case.herdr.agent_renames, [], "the alias was not renamed after the deadline")
        self.assertEqual(hst.read_wait(case.paths, wait["wait_id"])["status"], "EXPIRED")

    def test_the_deadline_passing_at_each_later_step_of_the_explicit_route_stops_exactly_that_step(self) -> None:
        for call, fragment, labelled in ((4, "before labelling the pane", 0), (7, "before binding ownership", 1)):
            with self.subTest(step=fragment):
                case = TgExplicitCase()
                case.setUp()
                self.addCleanup(case.tearDown)
                wait = case.open_explicit_wait()
                case.herdr.unblock("w9:p1")
                case.clock.current = wait["deadline_unix"] - 1
                original = case.herdr.list_agents
                calls = {"n": 0}

                def late(original=original, calls=calls, call=call, case=case, wait=wait):
                    calls["n"] += 1
                    if calls["n"] == call:
                        case.clock.current = wait["deadline_unix"] + 5
                    return original()

                case.herdr.list_agents = late
                result = case.continue_(wait["wait_id"])
                self.assertEqual((result["ok"], result["kind"]), (False, "expired"), result)
                self.assertIn(fragment, result["message"])
                self.assertEqual(len(case.herdr.agent_renames), 1, "the alias rename was in time; only the later step was stopped")
                self.assertEqual(len(case.herdr.renames), labelled, "a label applied in time is history; none is applied after the deadline")
                self.assertEqual(case.sup.owners(), case.owners, "ownership was never rebound")
                self.assertEqual(case.journal(case.prep)["status"], "ABANDONED")

    def test_the_deadline_passing_before_the_ownership_binding_leaves_the_owners_untouched(self) -> None:
        wait = self.open_codex_wait()
        self.herdr.unblock("w9:p1")
        owners = self.paths.owners_file.read_text()
        self.clock.current = wait["deadline_unix"] - 1
        original = self.herdr.rename_pane

        def late(pane, label):
            result = original(pane, label)  # the (allowed, in-time) label ...
            self.clock.current = wait["deadline_unix"] + 5  # ... and then time runs out before the binding
            return result

        self.herdr.rename_pane = late
        result = self.continue_(wait["wait_id"])
        self.assertEqual((result["ok"], result["kind"]), (False, "expired"), result)
        self.assertEqual(self.paths.owners_file.read_text(), owners)
        self.assertEqual(self.journal(self.prep)["status"], "ABANDONED")
        self.assertEqual(len(self.herdr.renames), 1, "only the in-time label happened; completed history is kept")

    def test_a_pane_label_is_not_applied_after_the_deadline(self) -> None:
        wait = self.open_codex_wait()
        self.herdr.unblock("w9:p1")
        path = self.paths.session_preparations_dir / f"{self.prep}.json"
        record = json.loads(path.read_text())
        item = record["providers"]["codex"]
        item.pop("label_state", None)
        stored = hst.read_wait(self.paths, wait["wait_id"])
        stored["status"], stored["continued_at_unix"] = "CONTINUING", wait["opened_at_unix"] + 1
        hst.write_wait(self.paths, stored)
        self.clock.current = wait["deadline_unix"] + 5
        with self.assertRaises(herdr_core.StartupWaitRequired) as caught:
            herdr_sessions._apply_label(self.sup, "codex", item, record, path)
        self.assertTrue(caught.exception.expired)
        self.assertEqual(self.herdr.renames, [])


class RunDeadlineTests(Seams, OwnerRecoveryCase):
    """CI2 for a restored provider inside a running task."""

    def to_restore_wait(self) -> dict:
        self.to_approved()
        del self.herdr.agents["codex-main"]
        self.herdr.startup_block, self.herdr.startup_block_for = "start_error", "codex-main"
        self.herdr.screens["w3:p2"] = update_menu_screen()
        self.assertEqual(self.worker(), 2)
        return self.state()

    def run_wait(self) -> dict:
        (wait,) = hst.list_waits(self.paths)
        return wait

    def command(self, action: str, wait_id: str, **extra) -> dict:
        with self.sup.store.transaction():
            state = self.sup.store.read_state()
            return self.sup.apply_command(state, {"action": action, "wait_id": wait_id, "actor": "telegram:1", **extra})

    def test_checks_that_finish_after_the_deadline_leave_the_run_stopped(self) -> None:
        state = self.to_restore_wait()
        wait = self.run_wait()
        self.herdr.unblock("w3:p2")
        self.clock.current = wait["deadline_unix"] - 1
        original = self.sup.startup_executable_record

        def slow(command):
            self.clock.current += 2
            return original(command)

        self.sup.startup_executable_record = slow
        prompts = len(self.herdr.prompts)
        result = self.command("startup_continue", wait["wait_id"], run_id=state["run_id"])
        self.assertEqual((result["ok"], result["kind"]), (False, "expired"), result)
        self.assertIn("while the checks were running", result["message"])
        after = self.state()
        self.assertEqual((after["supervisor_state"], after["wait_user_requires_action"]), ("WAIT_USER", True))
        self.assertEqual((hst.read_wait(self.paths, wait["wait_id"])["status"], len(self.herdr.prompts)), ("EXPIRED", prompts))
        with self.sup.store.transaction():
            live = self.sup.store.read_state()
            with self.assertRaisesRegex(hs.SupervisorError, "startup wait"):
                self.sup.guide(live, run_id=live["run_id"], actor="cli", note="go on anyway")
            with self.assertRaises(hs.SupervisorError):
                self.sup.apply_command(live, {"action": "resume", "run_id": live["run_id"]})
        self.assertEqual(len(self.herdr.prompts), prompts, "no revision or resume reached the provider after expiry")

    def test_exactly_at_the_deadline_is_accepted(self) -> None:
        state = self.to_restore_wait()
        wait = self.run_wait()
        self.herdr.unblock("w3:p2")
        self.clock.current = wait["deadline_unix"]
        result = self.command("startup_continue", wait["wait_id"], run_id=state["run_id"])
        self.assertEqual((result["ok"], result["kind"]), (True, "resolved"), result)

    def test_a_crash_after_the_resume_but_before_the_wait_is_closed_is_reconciled_by_the_sweep(self) -> None:
        state = self.to_restore_wait()
        wait = self.run_wait()
        self.herdr.unblock("w3:p2")
        real = hst.close_wait

        def crash(record, status, now, detail=""):
            if status == "RESOLVED":
                raise RuntimeError("crash after the resume was applied")
            return real(record, status, now, detail)

        hst.close_wait = crash
        try:
            with self.assertRaises(RuntimeError):
                self.command("startup_continue", wait["wait_id"], run_id=state["run_id"])
        finally:
            hst.close_wait = real
        self.assertEqual(self.state()["supervisor_state"], "RUNNING")
        self.assertEqual(hst.read_wait(self.paths, wait["wait_id"])["status"], "WAITING")
        self.assertEqual(self.sup.sweep_startup_waits(), [wait["wait_id"]])
        self.assertEqual(hst.read_wait(self.paths, wait["wait_id"])["status"], "RESOLVED")

    def test_an_overdue_run_wait_expires_from_the_files_and_state_alone_and_the_run_stays_stopped(self) -> None:
        self.to_restore_wait()
        wait = self.run_wait()
        self.herdr.unblock("w3:p2")
        self.clock.current = wait["deadline_unix"] + 40
        starts, prompts = len(self.herdr.starts), len(self.herdr.prompts)
        self.sup = self.make_supervisor()
        self.assertEqual(self.sup.sweep_startup_waits(), [wait["wait_id"]])
        self.assertEqual(hst.read_wait(self.paths, wait["wait_id"])["status"], "EXPIRED")
        self.assertEqual(self.state()["supervisor_state"], "WAIT_USER")
        self.assertEqual((len(self.herdr.starts), len(self.herdr.prompts)), (starts, prompts))
        self.assertEqual(len([e for e in self.events() if e["type"] == "STARTUP_WAIT_RESULT"]), 1)
        self.assertEqual(self.sup.sweep_startup_waits(), [])


class OriginalRequestTests(Seams, Issue5TelegramCase):
    """CI3: the original pending-start authorization survives a startup wait and is applied exactly once through a supported, authenticated action."""

    def setUp(self) -> None:
        super().setUp()
        self.sup = self.make_supervisor()
        self.bridge = self.make_bridge()
        self.bridge.model_capability_reader = lambda provider: True

    def buttons_of(self, card: dict) -> dict[str, str]:
        return {b["text"]: b["callback_data"] for row in (card["reply_markup"] or {"inline_keyboard": []})["inline_keyboard"] for b in row}

    def cards(self, fragment: str) -> list[dict]:
        self.bridge.deliver_outbox()
        return [m for m in self.api.sent if fragment in m["text"]]

    def counts(self) -> dict:
        return {"starts": len(self.herdr.starts), "splits": len(self.herdr.splits), "prompts": len(self.herdr.prompts), "bootstrap": len(self.herdr.bootstrap_prompts), "keys": len(self.herdr.sent_keys)}

    def journal(self, prep: str) -> dict:
        return json.loads((self.paths.session_preparations_dir / f"{prep}.json").read_text())

    def start_task(self, policy: str = "fresh-codex", text: str = "Add the widget") -> str:
        """The REAL path: /task, the session-policy button, the Start button of the confirmation card (reset budget 0) -> the original command in the inbox."""
        self.bridge.handle_update(self.message(f"/task {text}"))
        (token, record), = self.interactions("session_policy", policy=policy)
        self.tap(token)
        (zero, _), = [(t, r) for t, r in self.interactions("reset_budget") if r.get("budget") == 0]
        self.assertEqual(self.tap(zero)["result"], "enqueued")
        return record["pending_id"]

    def message(self, text: str) -> dict:
        from test_telegram import message as make

        return make(self.next_id(), text)

    def open_wait(self) -> tuple[str, dict]:
        self.herdr.startup_block = "blocked_agent"
        self.herdr.screens["w9:p1"] = update_menu_screen()
        pending_id = self.start_task()
        results = self.sup.process_inbox()
        self.assertEqual([r["ok"] for r in results], [False], results)
        (wait,) = hst.list_waits(self.paths)
        return pending_id, wait

    def test_the_original_authorization_is_kept_replayed_once_and_the_task_is_delivered_exactly_once(self) -> None:
        pending_id, wait = self.open_wait()
        origin = wait["origin_command"]
        self.assertEqual((origin["action"], origin["preallocated_run_id"], origin["pending_start_id"], origin["actor"]), ("task", pending_id, pending_id, f"telegram:{OWNER}"))
        self.assertEqual(wait["preparation_id"], pending_id)
        self.assertEqual(self.pending(pending_id)["status"], "supervisor_consuming")
        self.assertFalse(self.paths.state_file.exists(), "no run exists yet")
        self.assertEqual(self.herdr.prompts, [])
        continue_card = self.cards("start needs you")[-1]
        self.herdr.unblock("w9:p1")
        before = self.counts()
        self.tap(self.buttons_of(continue_card)["I handled it — Continue"])
        self.assertEqual([(r["ok"], r["kind"]) for r in self.sup.process_inbox()], [(True, "resolved")])
        done = self.cards("Startup handled")[-1]
        self.assertIn("Start the original task", done["text"] + " ".join(self.buttons_of(done)))
        self.assertNotIn("send the same task again", done["text"].lower())
        self.assertIn("A new /task is refused while this preparation is unresolved", done["text"])
        self.assertEqual(self.counts(), before)
        self.assertFalse(self.paths.state_file.exists(), "a continuation never starts the run")
        self.assertEqual(self.herdr.prompts, [], "the next actor is the human")
        start_token = self.buttons_of(done)["Start the original task"]
        record = json.loads((self.tg_paths.interactions_dir / f"{start_token}.json").read_text())
        self.assertEqual((record["action"], record["wait_id"], record["preparation_id"]), ("startup_start_original", wait["wait_id"], pending_id))
        self.assertEqual(self.tap(start_token)["result"], "enqueued")
        self.assertEqual(self.tap(start_token)["rejected"], "Already used (one-time action).")
        (result,) = self.sup.process_inbox()
        self.assertEqual((result["ok"], result["kind"], result["run_id"]), (True, "started", pending_id), result)
        state = self.state()
        self.assertEqual((state["run_id"], state["task_id"], state["session_selection"]["policy"]), (pending_id, pending_id, "fresh-codex"))
        self.assertIn(origin["request_id"], state["processed_requests"], "the ORIGINAL request identity is recorded as processed")
        self.assertEqual(state["task_command"]["request_id"], origin["request_id"])
        self.assertEqual(self.pending(pending_id)["status"], "started")
        self.assertEqual(self.journal(pending_id)["status"], "TASK_STARTED")
        self.assertEqual(self.sup.owners()["codex"]["pane_id"], "w9:p1")
        self.assertEqual(self.counts(), before, "initialization launched, bootstrapped and delivered nothing")
        # delivery: the worker's ordinary run loop sends the original task exactly once
        self.write_plan()
        self.herdr.responses = [{"v2": ("plan", "human", "plan_approval", str(self.plan_payload()))}]
        self.make_supervisor().worker()
        self.assertEqual(len(self.herdr.prompts), 1)
        self.assertIn("Add the widget", self.herdr.prompts[0][1])
        self.assertEqual(self.herdr.prompts[0][0], self.sup.agent_name("codex"))
        # restart and duplicate application: still one prompt, still one run
        self.make_supervisor().worker()
        forged = dict(origin)
        self.sup.enqueue_command({"request_id": "forged-start-original-1", "action": "startup_start_original", "wait_id": wait["wait_id"], "actor": f"telegram:{OWNER}"})
        (again,) = self.sup.process_inbox()
        self.assertEqual((again["ok"], again["kind"]), (False, "already_started"))
        self.sup.enqueue_command(forged)  # the original request id replayed from the inbox: the ledger answers, nothing is applied
        self.sup.process_inbox()
        self.assertEqual(len(self.herdr.prompts), 1)
        raw = json.loads(self.paths.state_file.read_text())
        raw["processed_requests"].pop(origin["request_id"])  # the ledger keeps only the newest 200 requests: the wait's own marker must still refuse a second application
        self.paths.state_file.write_text(json.dumps(raw))
        self.sup.enqueue_command({"request_id": "forged-start-original-2", "action": "startup_start_original", "wait_id": wait["wait_id"], "actor": f"telegram:{OWNER}"})
        (marked,) = self.sup.process_inbox()
        self.assertEqual((marked["ok"], marked["kind"]), (False, "already_started"))
        self.assertIn("already applied (run", marked["message"], "answered by the wait's start marker, not by the run ledger")
        self.assertEqual(len(self.herdr.prompts), 1)
        self.assertEqual(self.state()["run_id"], pending_id)
        marker = hst.read_wait(self.paths, wait["wait_id"])
        self.assertEqual((marker["original_run_id"], marker["status"]), (pending_id, "RESOLVED"))

    def prior_terminal_run(self, status: str) -> str:
        with self.sup.store.transaction():
            old = self.sup.initialize("Previous task", "codex", workflow_policy="gated_v2")
            old["supervisor_state"] = status
            self.sup.store.write_state(old)
        return old["run_id"]

    def finish_through_real_consumers(self, pending_id: str, wait: dict) -> dict:
        """Continue and Start-original through the real bridge buttons and the inbox consumer; returns the start-original result."""
        continue_card = self.cards("start needs you")[-1]
        self.herdr.unblock("w9:p1")
        self.tap(self.buttons_of(continue_card)["I handled it — Continue"])
        self.assertEqual([(r["ok"], r["kind"]) for r in self.sup.process_inbox()], [(True, "resolved")])
        done = self.cards("Startup handled")[-1]
        self.assertEqual(self.tap(self.buttons_of(done)["Start the original task"])["result"], "enqueued")
        (result,) = self.sup.process_inbox()
        return result

    def test_a_failed_original_start_under_a_prior_done_or_cancelled_run_does_not_count_as_started_and_the_new_run_starts_once(self) -> None:
        for prior_status in ("DONE", "CANCELLED"):
            with self.subTest(prior=prior_status):
                case = OriginalRequestTests("runTest")
                case.setUp()
                self.addCleanup(case.tearDown)
                prior_id = case.prior_terminal_run(prior_status)
                pending_id, wait = case.open_wait()
                origin = wait["origin_command"]
                ledger = case.state()["processed_requests"][origin["request_id"]]
                self.assertIs(ledger["ok"], False, "the original start FAILED and the failure is in the prior run's ledger")
                failure_events = {p.name: p.read_bytes() for p in (case.paths.outbox_dir / "events").glob("*.json")}
                result = case.finish_through_real_consumers(pending_id, wait)
                self.assertEqual((result["ok"], result["kind"], result["run_id"]), (True, "started", pending_id), result)
                self.assertNotEqual(pending_id, prior_id)
                state = case.state()
                self.assertEqual((state["run_id"], state["supervisor_state"] in ("DONE", "CANCELLED")), (pending_id, False), "the NEW run is current, not the prior terminal one")
                marker = hst.read_wait(case.paths, wait["wait_id"])
                self.assertEqual((marker["original_run_id"], marker["status"]), (pending_id, "RESOLVED"), "the marker names the new run, after actual success")
                self.assertIn(origin["request_id"], state["processed_requests"])
                self.assertIs(state["processed_requests"][origin["request_id"]]["ok"], True)
                self.assertEqual(case.pending(pending_id)["status"], "started")
                for name, content in failure_events.items():
                    self.assertEqual((case.paths.outbox_dir / "events" / name).read_bytes(), content, "the failure history is preserved byte for byte")
                self.assertEqual(len([h for h in marker["history"] if h["action"] == "original_started"]), 1)
                # delivered exactly once; a duplicate start-original does nothing
                case.write_plan()
                case.herdr.responses = [{"v2": ("plan", "human", "plan_approval", str(case.plan_payload()))}]
                case.make_supervisor().worker()
                self.assertEqual(len(case.herdr.prompts), 1)
                case.sup.enqueue_command({"request_id": f"dup-{prior_status}", "action": "startup_start_original", "wait_id": wait["wait_id"], "actor": f"telegram:{OWNER}"})
                (again,) = case.sup.process_inbox()
                self.assertEqual((again["ok"], again["kind"]), (False, "already_started"))
                self.assertEqual((len(case.herdr.prompts), case.state()["run_id"]), (1, pending_id))

    def test_a_ledger_entry_that_is_not_a_success_for_this_preparation_is_never_a_start(self) -> None:
        for label, entry in (("failed", {"ok": False}), ("truthy_not_true", {"ok": "yes", "run_id": "PENDING"}), ("missing_ok", {"run_id": "PENDING"})):
            with self.subTest(entry=label):
                case = OriginalRequestTests("runTest")
                case.setUp()
                self.addCleanup(case.tearDown)
                case.prior_terminal_run("DONE")
                pending_id, wait = case.open_wait()
                origin = wait["origin_command"]
                raw = json.loads(case.paths.state_file.read_text())
                raw["processed_requests"][origin["request_id"]] = {**raw["processed_requests"][origin["request_id"]], **{k: (pending_id if v == "PENDING" else v) for k, v in entry.items()}}
                if "ok" not in entry:
                    raw["processed_requests"][origin["request_id"]].pop("ok", None)
                case.paths.state_file.write_text(json.dumps(raw))
                result = case.finish_through_real_consumers(pending_id, wait)
                self.assertEqual((result["ok"], result["kind"]), (True, "started"), result)
                self.assertEqual(case.state()["run_id"], pending_id)

    def test_a_success_record_naming_another_run_is_refused_and_marks_nothing(self) -> None:
        case = self
        case.prior_terminal_run("DONE")
        pending_id, wait = case.open_wait()
        origin = wait["origin_command"]
        raw = json.loads(case.paths.state_file.read_text())
        raw["processed_requests"][origin["request_id"]] = {**raw["processed_requests"][origin["request_id"]], "ok": True, "run_id": str(uuid.uuid4())}
        case.paths.state_file.write_text(json.dumps(raw))
        before = case.counts()
        result = case.finish_through_real_consumers(pending_id, wait)
        self.assertEqual((result["ok"], result["kind"]), (False, "binding"), result)
        self.assertEqual(case.counts(), before)
        marker = hst.read_wait(case.paths, wait["wait_id"])
        self.assertIsNone(marker.get("original_started_at_unix"))
        self.assertIsNone(marker.get("original_run_id"))
        self.assertEqual(case.state()["run_id"] != pending_id, True, "no run was initialized for the preparation")

    def test_a_replay_that_did_not_start_the_prepared_run_is_never_recorded_as_started(self) -> None:
        for label, outcome in (("not_ok", {"ok": False, "message": "refused"}), ("other_run", {"ok": True, "run_id": "other-run", "message": "started"}), ("truthy", {"ok": 1, "run_id": None})):
            with self.subTest(outcome=label):
                case = OriginalRequestTests("runTest")
                case.setUp()
                self.addCleanup(case.tearDown)
                pending_id, wait = case.open_wait()
                continue_card = case.cards("start needs you")[-1]
                case.herdr.unblock("w9:p1")
                case.tap(case.buttons_of(continue_card)["I handled it — Continue"])
                case.sup.process_inbox()
                done = case.cards("Startup handled")[-1]
                real_apply = case.sup.apply_command
                case.sup.apply_command = lambda state, command, outcome=outcome, real_apply=real_apply: dict(outcome) if command.get("action") == "task" else real_apply(state, command)  # the replayed task "succeeds" without having started this preparation's run
                case.tap(case.buttons_of(done)["Start the original task"])
                (result,) = case.sup.process_inbox()
                self.assertEqual((result["ok"], result["kind"]), (False, "start_refused"), result)
                marker = hst.read_wait(case.paths, wait["wait_id"])
                self.assertEqual((marker.get("original_started_at_unix"), marker.get("original_run_id")), (None, None))
                self.assertTrue(marker["status"] in ("CONTINUING", "RESOLVED"), "still retryable")

    def test_a_crash_after_the_successful_start_before_the_marker_is_recovered_without_a_second_run(self) -> None:
        self.prior_terminal_run("CANCELLED")
        pending_id, wait = self.open_wait()
        continue_card = self.cards("start needs you")[-1]
        self.herdr.unblock("w9:p1")
        self.tap(self.buttons_of(continue_card)["I handled it — Continue"])
        self.sup.process_inbox()
        done = self.cards("Startup handled")[-1]
        original_write = hst.write_wait

        def crash(paths, record):
            if record.get("original_started_at_unix") is not None:
                raise RuntimeError("crash after the run started, before the wait marker")
            return original_write(paths, record)

        hst.write_wait = crash
        try:
            self.tap(self.buttons_of(done)["Start the original task"])
            with self.assertRaises(RuntimeError):
                self.sup.process_inbox()
        finally:
            hst.write_wait = original_write
        self.assertEqual(self.state()["run_id"], pending_id, "the run did start (ledger has the success)")
        self.assertIsNone(hst.read_wait(self.paths, wait["wait_id"]).get("original_started_at_unix"))
        self.sup = self.make_supervisor()
        self.sup.enqueue_command({"request_id": "recover-start-original", "action": "startup_start_original", "wait_id": wait["wait_id"], "actor": f"telegram:{OWNER}"})
        results = self.sup.process_inbox()  # the crashed command file is consumed again (answered by the ledger) and the recovery request is answered by it too
        self.assertEqual([(r["ok"], r["kind"]) for r in results], [(False, "already_started")] * 2, results)
        marker = hst.read_wait(self.paths, wait["wait_id"])
        self.assertEqual(marker["original_run_id"], pending_id, "the marker names the run that ACTUALLY started")
        self.assertEqual(self.state()["run_id"], pending_id)

    def test_a_fresh_or_different_task_cannot_reuse_the_bound_preparation_and_the_refusal_names_the_supported_action(self) -> None:
        pending_id, wait = self.open_wait()
        self.herdr.unblock("w9:p1")
        self.assertTrue(self.sup.continue_startup(wait["wait_id"], actor="telegram:1")["ok"])
        files = sorted(p.name for p in self.paths.session_preparations_dir.glob("*.json"))
        other = self.start_task(policy="fresh-codex", text="A completely different task")
        self.assertNotEqual(other, pending_id)
        (refused,) = self.sup.process_inbox()
        self.assertFalse(refused["ok"])
        self.assertIn("is unresolved: it is already bound but its task was never started", refused["message"])
        self.assertIn(f"start-original --wait-id {wait['wait_id']}", refused["message"])
        self.assertIn("abandon-session-start` cannot release a bound preparation", refused["message"])
        self.assertNotIn("send the same task again", refused["message"], "retyping a task is not a recovery and is no longer advertised as one")
        self.assertNotIn("Recovery:", refused["message"])
        self.assertNotIn("--preparation-id " + other, refused["message"], "no abandon advice for the refused new attempt")
        self.assertEqual(sorted(p.name for p in self.paths.session_preparations_dir.glob("*.json")), files, "no second preparation was created")
        self.assertFalse(self.paths.state_file.exists())
        # the original authorization is still intact and still works
        self.sup.enqueue_command({"request_id": "start-original-after-refusal", "action": "startup_start_original", "wait_id": wait["wait_id"], "actor": f"telegram:{OWNER}", "preparation_id": pending_id})
        (ok,) = self.sup.process_inbox()
        self.assertEqual((ok["ok"], ok["kind"]), (True, "started"))
        self.assertIn("Add the widget", self.state()["task_text"])

    def test_a_tampered_or_foreign_stored_request_is_refused_and_starts_nothing(self) -> None:
        pending_id, wait = self.open_wait()
        self.herdr.unblock("w9:p1")
        self.assertTrue(self.sup.continue_startup(wait["wait_id"], actor="telegram:1")["ok"])
        original = hst.read_wait(self.paths, wait["wait_id"])
        for label, mutate in (("different task text", lambda o: o.update(task_text="Delete everything")),
                              ("another preparation", lambda o: o.update(preallocated_run_id=str(uuid.uuid4()))),
                              ("another actor binding", lambda o: o.update(actor="telegram:1")),
                              ("request id changed", lambda o: o.update(request_id="f" * 32))):
            with self.subTest(label):
                tampered = json.loads(json.dumps(original))
                mutate(tampered["origin_command"])
                hst.write_wait(self.paths, tampered)
                self.sup.enqueue_command({"request_id": f"start-original-{abs(hash(label)) % 10**8}", "action": "startup_start_original", "wait_id": wait["wait_id"], "actor": f"telegram:{OWNER}"})
                (result,) = self.sup.process_inbox()
                self.assertFalse(result["ok"], result)
                if label == "another preparation":
                    self.assertEqual(result["kind"], "binding", "refused by the wait's own binding check before anything is applied")
                self.assertFalse(self.paths.state_file.exists())
                self.assertEqual(self.herdr.prompts, [])
                self.assertEqual(self.pending(pending_id)["status"], "supervisor_consuming")
        hst.write_wait(self.paths, original)
        self.sup.enqueue_command({"request_id": "start-original-restored-1", "action": "startup_start_original", "wait_id": wait["wait_id"], "actor": f"telegram:{OWNER}"})
        self.assertTrue(self.sup.process_inbox()[0]["ok"])

    def test_a_cancelled_pending_authorization_or_a_continuation_that_never_happened_cannot_start_the_task(self) -> None:
        pending_id, wait = self.open_wait()
        self.sup.enqueue_command({"request_id": "start-before-continue-1", "action": "startup_start_original", "wait_id": wait["wait_id"], "actor": f"telegram:{OWNER}"})
        (early,) = self.sup.process_inbox()
        self.assertEqual((early["ok"], early["kind"]), (False, "not_resolved"))
        self.assertFalse(self.paths.state_file.exists())
        self.assertEqual(self.herdr.prompts, [])

    def test_the_cli_start_original_uses_the_same_handler_and_a_wait_without_a_stored_request_names_the_honest_next_step(self) -> None:
        pending_id, wait = self.open_wait()
        self.herdr.unblock("w9:p1")
        result = self.sup.continue_startup(wait["wait_id"], actor="cli:me")
        self.assertIn("start-original", result["message"])
        with self.sup.store.transaction():
            started = self.sup.apply_command(self.sup.store.read_state(required=False), {"action": "startup_start_original", "wait_id": wait["wait_id"], "actor": "cli:me"})
        self.assertEqual((started["ok"], started["kind"]), (True, "started"))
        # a wait from a direct preparation has no stored request
        direct = WaitCase()
        direct.setUp()
        self.addCleanup(direct.tearDown)
        waiting = direct.open_codex_wait()
        direct.herdr.unblock("w9:p1")
        text = direct.continue_(waiting["wait_id"])["message"]
        self.assertIn("No original start request is stored", text)
        self.assertIn("rebind-sessions", text)
        refused = direct.sup.apply_command(None, {"action": "startup_start_original", "wait_id": waiting["wait_id"], "actor": "cli:me"})
        self.assertEqual(refused["kind"], "no_original_request")


class CliOriginTests(Seams, CliCase):
    """A CLI start (`run`) is recovered by running the same command again: the journal reuses the unfinished preparation."""

    def setUp(self) -> None:
        super().setUp()
        case = self
        herdr_command.Supervisor = lambda paths, config, herdr, **kwargs: case.make_supervisor()  # type: ignore[assignment]

    def test_run_wait_continue_and_the_same_run_command_again_starts_the_task_once(self) -> None:
        self.herdr.startup_block = "blocked_agent"
        self.herdr.screens["w9:p1"] = update_menu_screen()
        args = ("run", "--start", "codex", "--policy", "gated_v2", "--session-policy", "fresh-codex", "Add the widget")
        code, out, err = self.run_cli(*args)
        self.assertEqual(code, 2, out + err)
        (wait,) = hst.list_waits(self.paths)
        self.assertIsNone(wait["origin_command"], "a CLI start has no inbox command: it is repeated by running the same command")
        self.assertFalse(self.paths.state_file.exists())
        self.herdr.unblock("w9:p1")
        code, out, err = self.run_cli("continue-startup", "--wait-id", wait["wait_id"])
        self.assertEqual(code, 0, out + err)
        self.assertIn("run the same `herdr-supervisor run", out)
        self.assertNotIn("start-original", out)
        starts, prompts = len(self.herdr.starts), len(self.herdr.prompts)
        self.write_plan()
        self.herdr.responses = [{"v2": ("plan", "human", "plan_approval", str(self.plan_payload()))}]
        code, out, err = self.run_cli(*args)
        self.assertEqual(self.state()["supervisor_state"], "WAIT_PLAN_APPROVAL", (code, out, err))
        self.assertEqual((len(self.herdr.starts), len(self.herdr.prompts)), (starts, prompts + 1), "no second launch; the task was delivered once")
        self.assertEqual(self.state()["run_id"], wait["preparation_id"])


class PreparationWideDeadlineTests(TgWaitCase):
    """CI2-R1: the original bound (a wait's deadline AND the caller's absolute ceiling) stops EVERY new effect of the preparation, including another provider's launch."""

    def counts_and_state(self, case) -> tuple:
        return (case.counts(), case.paths.owners_file.read_text(), len(case.herdr.renames), [h for h in case.herdr.agent_renames])

    def test_a_wait_continued_just_in_time_cannot_launch_the_other_provider_after_its_deadline(self) -> None:
        wait = self.open_codex_wait(selection=FRESH_ALL)
        self.herdr.unblock("w9:p1")
        self.clock.current = wait["deadline_unix"] - 1
        original = self.herdr.rename_pane
        frozen: dict = {}

        def late(pane, label):
            result = original(pane, label)
            if pane == "w9:p1":  # the Codex label finishes inside the bound; the clock then passes it
                self.clock.current = wait["deadline_unix"] + 5
                frozen["state"] = self.counts_and_state(self)
                frozen["owners"] = self.paths.owners_file.read_bytes()
            return result

        self.herdr.rename_pane = late
        result = self.continue_(wait["wait_id"])
        self.assertEqual((result["ok"], result["kind"]), (False, "expired"), result)
        self.assertIn("creating its pane (claude)", result["message"], "the refused effect is the OTHER provider's first new effect")
        self.assertEqual(self.counts_and_state(self), frozen["state"], "no Claude thread, split, start, launch, bootstrap, label, ownership change after the deadline")
        self.assertEqual(self.herdr.starts and [x for x in self.herdr.starts if "claude" in str(x)], [], "Claude was never started")
        self.assertEqual((hst.read_wait(self.paths, wait["wait_id"])["status"], self.journal(self.prep)["status"]), ("EXPIRED", "ABANDONED"))
        self.assertEqual(self.paths.owners_file.read_bytes(), frozen["owners"], "ownership was never rebound")

    def test_every_provider_start_effect_obeys_the_original_bound_even_when_the_other_provider_has_the_open_wait(self) -> None:
        """The refusal comes from the guard of each effect site (the agent-start route): the wait's deadline passes before the Claude pane split, and before its agent start."""
        sites = (("split", "creating its pane"), ("start", "starting its agent"))
        for site, fragment in sites:
            with self.subTest(site=site):
                case = TgWaitCase()
                case.setUp()
                self.addCleanup(case.tearDown)
                wait = case.open_codex_wait(selection=FRESH_ALL)
                case.herdr.unblock("w9:p1")
                case.clock.current = wait["deadline_unix"] - 1
                if site == "split":
                    original_label = case.herdr.rename_pane

                    def label_then_late(pane, label, original_label=original_label, case=case, wait=wait):
                        result = original_label(pane, label)
                        case.clock.current = wait["deadline_unix"] + 5  # Codex's last in-time effect; Claude's pane split comes after the bound
                        return result

                    case.herdr.rename_pane = label_then_late
                else:
                    original_split = case.herdr.split_pane

                    def after_split(*args, original_split=original_split, case=case, wait=wait, **kwargs):
                        result = original_split(*args, **kwargs)
                        case.clock.current = wait["deadline_unix"] + 5  # the pane exists (in time); its agent start comes after the bound
                        return result

                    case.herdr.split_pane = after_split
                starts_before, splits_before = len(case.herdr.starts), len(case.herdr.splits)
                result = case.continue_(wait["wait_id"])
                self.assertEqual((result["ok"], result["kind"]), (False, "expired"), result)
                self.assertIn(fragment, result["message"])
                self.assertEqual(len(case.herdr.starts), starts_before, "no agent start after the deadline")
                self.assertEqual(len(case.herdr.splits), splits_before + (1 if site == "start" else 0))
                self.assertEqual(case.journal(case.prep)["status"], "ABANDONED")

    def test_the_callers_absolute_ceiling_stops_each_new_effect_even_when_no_wait_is_open(self) -> None:
        """No wait at all: `startup_wait.not_after_unix` alone forbids the next effect (pane split, pane run), makes the preparation inert and names the ceiling."""
        for site in ("split", "launch"):
            with self.subTest(site=site):
                case = TgExplicitCase()
                case.setUp()
                self.addCleanup(case.tearDown)
                case.config["session_start"]["startup_wait"] = {"not_after_unix": case.clock.current + 120}
                case.sup = case.make_supervisor()
                ceiling = case.clock.current + 120
                if site == "split":
                    case.clock.current = ceiling + 1
                else:
                    original_split = case.herdr.split_pane

                    def after_split(*args, original_split=original_split, case=case, ceiling=ceiling, **kwargs):
                        result = original_split(*args, **kwargs)
                        case.clock.current = ceiling + 1  # the pane exists; its launch comes after the ceiling
                        return result

                    case.herdr.split_pane = after_split
                prep = str(uuid.uuid4())
                owners_before = case.paths.owners_file.read_bytes()
                with self.assertRaises(herdr_core.AbsoluteDeadlinePassed) as caught:
                    case.prepare(FRESH_ALL, prep)
                self.assertIn("absolute deadline", str(caught.exception))
                self.assertEqual(case.journal(prep)["status"], "ABANDONED", "inert: nothing continues")
                expected = {"split": (0, 0, 0), "launch": (1, 0, 0)}[site]
                self.assertEqual((len(case.herdr.splits), len(case.herdr.pane_runs), len(case.thread_journal.requests)), expected)
                self.assertEqual(case.paths.owners_file.read_bytes(), owners_before, "ownership never changed")
                with self.assertRaises(hs.SupervisorError) as again:
                    case.prepare(FRESH_ALL, prep)
                self.assertIn("abandoned", str(again.exception))
                self.assertEqual((len(case.herdr.splits), len(case.herdr.pane_runs), len(case.thread_journal.requests)), expected, "the retry did nothing")


class ExplicitRouteDeadlineTests(TgExplicitCase):
    """CI2-R1 on the explicit pane-run route: the bound also stops the explicit launch submission and the other provider."""

    def test_the_other_providers_launch_is_refused_after_the_original_deadline(self) -> None:
        self.prep = str(uuid.uuid4())
        self.herdr.startup_block = "undetected"
        self.herdr.screens["w9:p1"] = update_menu_screen()
        with self.assertRaises(herdr_core.StartupWaitRequired) as caught:
            self.prepare(FRESH_ALL, self.prep)
        wait = hst.read_wait(self.paths, caught.exception.wait_id)
        self.herdr.unblock("w9:p1")
        self.clock.current = wait["deadline_unix"] - 1
        original = self.herdr.rename_pane
        frozen: dict = {}

        def late(pane, label):
            result = original(pane, label)
            if pane == "w9:p1":
                self.clock.current = wait["deadline_unix"] + 5
                frozen["counts"] = self.counts()
                frozen["owners"] = self.paths.owners_file.read_text()
            return result

        self.herdr.rename_pane = late
        result = self.continue_(wait["wait_id"])
        self.assertEqual((result["ok"], result["kind"]), (False, "expired"), result)
        self.assertEqual((self.counts(), self.paths.owners_file.read_text()), (frozen["counts"], frozen["owners"]), "no splits, threads, launches, starts after the deadline")
        self.assertEqual((hst.read_wait(self.paths, wait["wait_id"])["status"], self.journal(self.prep)["status"]), ("EXPIRED", "ABANDONED"))

    def test_the_explicit_pane_run_submission_obeys_the_callers_ceiling(self) -> None:
        self.config["session_start"]["startup_wait"] = {"not_after_unix": self.clock.current + 120}
        self.sup = self.make_supervisor()
        ceiling = self.clock.current + 120
        original = self.herdr.split_pane

        def after_split(*args, **kwargs):
            result = original(*args, **kwargs)
            self.clock.current = ceiling + 1  # the pane exists; the launch submission comes after the ceiling
            return result

        self.herdr.split_pane = after_split
        prep = str(uuid.uuid4())
        with self.assertRaises(herdr_core.AbsoluteDeadlinePassed) as caught:
            self.prepare(CODEX_ONLY, prep)
        self.assertIn("submitting its launch", str(caught.exception))
        self.assertEqual(self.counts(), (0, 1, 0, 0), "no thread; one split happened in time; no launch was submitted")
        self.assertEqual(self.herdr.pane_runs, [])
        self.assertEqual(self.journal(prep)["status"], "ABANDONED")


class BindingRecoveryDeadlineTests(TgWaitCase):
    """CI2-R2: a persisted BINDING marker is INTENT; the previously unperformed owners write obeys the original bound, a completed one is reconciled without replay."""

    def crash_in_binding(self, *, after_owners_write: bool) -> dict:
        wait = self.open_codex_wait()
        self.herdr.unblock("w9:p1")
        original = herdr_sessions.atomic_write_json
        journal_path = self.paths.session_preparations_dir / f"{self.prep}.json"

        def crash(path, value, *args, **kwargs):
            if after_owners_write:
                if path == journal_path and isinstance(value, dict) and value.get("status") == "OWNERSHIP_BOUND":
                    raise RuntimeError("crash after the owners write, before the journal says OWNERSHIP_BOUND")
            result = original(path, value, *args, **kwargs)
            if not after_owners_write and path == journal_path and isinstance(value, dict) and value.get("status") == "BINDING":
                raise RuntimeError("crash after the BINDING marker, before the owners write")
            return result

        herdr_sessions.atomic_write_json = crash
        try:
            with self.assertRaises(RuntimeError):
                self.continue_(wait["wait_id"])
        finally:
            herdr_sessions.atomic_write_json = original
        self.assertEqual(self.journal(self.prep)["status"], "BINDING")
        self.sup = self.make_supervisor()
        return wait

    def retry(self):
        return self.prepare(CODEX_ONLY, self.prep)

    def test_a_crash_before_the_owners_write_and_a_retry_after_the_deadline_changes_nothing_and_is_inert(self) -> None:
        wait = self.crash_in_binding(after_owners_write=False)
        owners = self.paths.owners_file.read_bytes()
        before = self.counts()
        self.clock.current = wait["deadline_unix"] + 100
        with self.assertRaises(herdr_core.StartupWaitRequired) as caught:
            self.retry()
        self.assertTrue(caught.exception.expired)
        self.assertIn("binding ownership", str(caught.exception))
        self.assertEqual(self.paths.owners_file.read_bytes(), owners, "the owners bytes are exactly the old ones")
        self.assertEqual(self.counts(), before, "zero new effects")
        self.assertEqual((self.journal(self.prep)["status"], hst.read_wait(self.paths, wait["wait_id"])["status"]), ("ABANDONED", "EXPIRED"))
        with self.assertRaises(hs.SupervisorError) as again:  # a valid recovery path: the journal is inert, so a later retry is refused plainly and still changes nothing
            self.retry()
        self.assertIn("abandoned", str(again.exception))
        self.assertEqual((self.paths.owners_file.read_bytes(), self.counts()), (owners, before))

    def test_a_crash_before_the_owners_write_and_a_late_sweep_alone_makes_the_binding_inert(self) -> None:
        wait = self.crash_in_binding(after_owners_write=False)
        owners = self.paths.owners_file.read_bytes()
        self.clock.current = wait["deadline_unix"] + 100
        self.assertEqual(self.sup.sweep_startup_waits(), [wait["wait_id"]])
        self.assertEqual((self.journal(self.prep)["status"], hst.read_wait(self.paths, wait["wait_id"])["status"]), ("ABANDONED", "EXPIRED"))
        self.assertEqual(self.paths.owners_file.read_bytes(), owners)

    def test_a_crash_before_the_owners_write_and_a_retry_inside_the_deadline_completes_the_binding(self) -> None:
        wait = self.crash_in_binding(after_owners_write=False)
        self.clock.current = wait["deadline_unix"] - 1
        before = self.counts()
        record = self.retry()
        self.assertEqual(record["status"], "OWNERSHIP_BOUND")
        self.assertEqual(self.sup.owners()["codex"]["pane_id"], "w9:p1")
        self.assertEqual(self.counts(), before, "the in-time recovery launched, bootstrapped and delivered nothing")
        self.sup.sweep_startup_waits()
        self.assertEqual(hst.read_wait(self.paths, wait["wait_id"])["status"], "RESOLVED")

    def test_a_crash_after_the_owners_write_is_reconciled_late_without_replay_or_rewind(self) -> None:
        wait = self.crash_in_binding(after_owners_write=True)
        owners = self.paths.owners_file.read_bytes()
        self.assertEqual(json.loads(owners.decode())["codex"]["pane_id"], "w9:p1", "the owners write DID happen before the crash")
        before = self.counts()
        self.clock.current = wait["deadline_unix"] + 100
        self.assertEqual(self.sup.sweep_startup_waits(), [], "a completed binding is not expired by the sweep")
        self.assertEqual((self.journal(self.prep)["status"], hst.read_wait(self.paths, wait["wait_id"])["status"]), ("BINDING", "CONTINUING"))
        record = self.retry()
        self.assertEqual(record["status"], "OWNERSHIP_BOUND")
        self.assertEqual(self.paths.owners_file.read_bytes(), owners, "no owners write was repeated or rewound")
        self.assertEqual(self.counts(), before)
        self.sup.sweep_startup_waits()
        self.assertEqual(hst.read_wait(self.paths, wait["wait_id"])["status"], "RESOLVED")

    def test_an_inert_rule_never_abandons_a_binding_whose_owners_were_already_written(self) -> None:
        wait = self.crash_in_binding(after_owners_write=True)
        self.assertFalse(herdr_sessions._make_preparation_inert(self.sup, self.prep, "test"))
        self.assertEqual(self.journal(self.prep)["status"], "BINDING")
        self.assertEqual(hst.read_wait(self.paths, wait["wait_id"])["status"], "CONTINUING")


class PersistenceBoundaryTests(TgWaitCase):
    """CI2-R1-P: persistence after a deadline check can finish AFTER the bound; the bound is re-read after each durable intent write, immediately before the external effect. The marker stays
    consumed (the preparation becomes inert, nothing rearms or resends) and the particular effect is never invoked; an identical in-time control performs it exactly once."""

    SITES = (
        # name, case class, setup, journal predicate (the intent record whose write finishes late), effect counter, message fragment, consumed status
        # (a NEW fresh Codex has no thread request and no generic agent start: its effects are the split, the single pane run, the bootstrap, the label and the binding)
        ("split", TgExplicitCase, None, lambda r: r["providers"]["codex"].get("status") == "PANE_CREATING", lambda c: len(c.herdr.splits), "creating its pane", ("providers", "codex", "status", "PANE_CREATING")),
        ("launch", TgExplicitCase, None, lambda r: r["providers"]["codex"].get("status") == "LAUNCH_SUBMITTED", lambda c: len(c.herdr.pane_runs), "submitting its launch", ("providers", "codex", "status", "LAUNCH_SUBMITTED")),
        ("bootstrap", TgExplicitCase, "bootstrap", lambda r: r["providers"]["codex"].get("status") == "BOOTSTRAP_SUBMITTED", lambda c: len(c.herdr.bootstrap_prompts), "the identity bootstrap", ("providers", "codex", "status", "BOOTSTRAP_SUBMITTED")),
        ("label", TgExplicitCase, None, lambda r: (r["providers"]["codex"].get("label_state") or {}).get("status") == "NAMING_INTENT", lambda c: len(c.herdr.renames), "labelling the pane", None),
        ("binding", TgExplicitCase, None, lambda r: r.get("status") == "BINDING", lambda c: 0 if c.paths.owners_file.read_bytes() == c.owners_bytes else 1, "binding ownership", ("status", None, None, "ABANDONED")),
    )

    # The previous durable write of each site: when THIS write finishes late the guard BEFORE the intent write refuses, so the intent marker is never even written.
    BEFORE = {
        "split": lambda r: r["providers"]["codex"].get("status") == "AUTHORIZED",
        "launch": lambda r: r["providers"]["codex"].get("status") == "LAUNCH_INTENT",
        "bootstrap": lambda r: r["providers"]["codex"].get("status") == "BOOTSTRAP_INTENT",
        "label": lambda r: r["providers"]["codex"].get("status") == "IDENTITY_VERIFIED" and "label_state" not in r["providers"]["codex"],
        "binding": lambda r: (r["providers"]["codex"].get("label_state") or {}).get("status") in ("NAMED", "NAME_FAILED", "NAME_UNVERIFIED", "NAME_MISMATCH", "NAME_UNSUPPORTED"),
    }

    def run_site(self, site, *, late: bool, before: bool = False):
        name, klass, setup, matches, effect, fragment, consumed = site
        trigger = self.BEFORE[name] if before else matches
        writes: list[dict] = []
        case = klass()
        case.setUp()
        self.addCleanup(case.tearDown)
        ceiling = case.clock.current + 120
        case.config["session_start"]["startup_wait"] = {"not_after_unix": ceiling}
        if setup == "bootstrap":
            case.herdr.hold_identity = True
            case.config["session_start"]["native_identity_bootstrap"] = True
        case.sup = case.make_supervisor()
        case.owners_bytes = case.paths.owners_file.read_bytes()
        prep = str(uuid.uuid4())
        journal_dir = case.paths.session_preparations_dir
        original = herdr_sessions.atomic_write_json
        hits: list[int] = []

        def persist(path, record, *args, **kwargs):
            result = original(path, record, *args, **kwargs)
            if getattr(path, "parent", None) == journal_dir and isinstance(record, dict) and "providers" in record:
                writes.append(json.loads(json.dumps(record)))
            if late and not hits and getattr(path, "parent", None) == journal_dir and isinstance(record, dict) and "providers" in record and trigger(record):
                hits.append(1)
                case.clock.current = ceiling + 1  # the durable write finished after the bound
            return result

        herdr_sessions.atomic_write_json = persist
        try:
            try:
                outcome = case.prepare(CODEX_ONLY, prep)
                error = None
            except hs.SupervisorError as caught:
                outcome, error = None, caught
        finally:
            herdr_sessions.atomic_write_json = original
        case.writes = writes
        return case, prep, outcome, error, hits

    def test_each_effect_is_refused_when_its_own_intent_write_finishes_after_the_bound(self) -> None:
        for site in self.SITES:
            name, _, _, _, effect, fragment, consumed = site
            with self.subTest(site=name):
                case, prep, outcome, error, hits = self.run_site(site, late=True)
                self.assertEqual(hits, [1], "the delayed durable write was reached")
                self.assertIsNone(outcome)
                self.assertIsInstance(error, herdr_core.AbsoluteDeadlinePassed, error)
                self.assertIn(f"before {fragment}", str(error))
                self.assertEqual(effect(case), 0, f"the {name} effect was never invoked")
                journal = case.journal(prep)
                self.assertEqual(journal["status"], "ABANDONED", "inert: nothing rearms or resends")
                if consumed and consumed[0] == "providers":
                    self.assertEqual(journal["providers"]["codex"]["status"], consumed[3], "the durable attempt marker stays consumed, never rolled back")
                if name == "label":
                    self.assertEqual(journal["providers"]["codex"]["label_state"]["status"], "NAMING_INTENT")
                frozen = (effect(case), case.paths.owners_file.read_bytes())
                with self.assertRaises(hs.SupervisorError) as again:
                    case.prepare(CODEX_ONLY, prep)
                self.assertIn("abandoned", str(again.exception))
                self.assertEqual((effect(case), case.paths.owners_file.read_bytes()), frozen, "a retry does nothing")

    def test_when_the_previous_write_finishes_late_the_intent_marker_is_never_written_and_the_effect_never_invoked(self) -> None:
        for site in self.SITES:
            name, _, _, matches, effect, fragment, _ = site
            with self.subTest(site=name):
                case, prep, outcome, error, hits = self.run_site(site, late=True, before=True)
                self.assertEqual(hits, [1])
                self.assertIsInstance(error, herdr_core.AbsoluteDeadlinePassed, error)
                self.assertIn(f"before {fragment}", str(error))
                self.assertEqual(effect(case), 0)
                self.assertEqual(case.journal(prep)["status"], "ABANDONED")
                self.assertFalse([w for w in case.writes if matches(w)], "the intent marker was refused BEFORE it was persisted")

    def test_the_same_boundaries_in_time_perform_each_effect_exactly_once(self) -> None:
        for site in self.SITES:
            name, _, _, _, effect, _, _ = site
            with self.subTest(site=name):
                case, prep, outcome, error, hits = self.run_site(site, late=False)
                self.assertIsNone(error)
                self.assertEqual(outcome["status"], "OWNERSHIP_BOUND")
                self.assertEqual(hits, [])
                if name != "binding":
                    self.assertEqual(effect(case), 1)
                else:
                    self.assertEqual(effect(case), 1, "owners were written exactly once")

    def test_an_operation_already_submitted_in_time_completes_and_the_next_effect_is_refused(self) -> None:
        """A submitted operation is never rewound: the launch is submitted in time, the clock then passes the bound, and only the NEXT effect (the alias rename) is refused."""
        name, klass, *_ = next(site for site in self.SITES if site[0] == "launch")
        case = klass()
        case.setUp()
        self.addCleanup(case.tearDown)
        ceiling = case.clock.current + 120
        case.config["session_start"]["startup_wait"] = {"not_after_unix": ceiling}
        case.sup = case.make_supervisor()
        original = case.herdr.pane_run

        def late_after_submit(pane, command):
            result = original(pane, command)
            case.clock.current = ceiling + 1
            return result

        case.herdr.pane_run = late_after_submit
        prep = str(uuid.uuid4())
        with self.assertRaises(herdr_core.AbsoluteDeadlinePassed) as caught:
            case.prepare(CODEX_ONLY, prep)
        self.assertIn("naming the agent", str(caught.exception))
        self.assertEqual(len(case.herdr.pane_runs), 1, "the in-time submission is history")
        self.assertEqual(case.journal(prep)["status"], "ABANDONED")


if __name__ == "__main__":
    import unittest

    unittest.main()
