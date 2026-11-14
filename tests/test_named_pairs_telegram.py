"""Task setup in Telegram: use a saved pair, save an explicitly chosen existing pair, or create and name a fresh pair. Selection is read-only (zero
launch/prompt/rename/rebind calls), the final card shows name, repository, review base and both identities, the complete pair is fingerprinted into the
pending record, the card and the command, and a changed registry/selection/context invalidates old Start buttons. Fake Herdr and fake Bot API only."""

from __future__ import annotations

import json
import threading

import herdr_pairs as hpair
from pair_fixtures import ALPHA, BETA, PairCase, hs, ids
from test_telegram import callback, message
from v2_fixtures import FakeHerdr, write_codex_primary_rollout

TASK = "Pair fixture task: inspect a read-only fixture and report."
GAMMA = {"claude": ("w6:p1", "eeeeeeee-0000-4000-8000-000000000001"), "codex": ("w6:p2", "eeeeeeee-0000-4000-8000-000000000002")}


class TgPairCase(PairCase):
    def setUp(self) -> None:
        super().setUp()
        self.write_plan()
        self.n = 2000
        self.bridge.model_capability_reader = lambda provider: True
        for provider, (pane, session) in GAMMA.items():
            self.herdr.agents[f"gamma-{provider}"] = FakeHerdr.agent(provider, f"gamma-{provider}", pane, session)
        write_codex_primary_rollout(self.codex_native_root, GAMMA["codex"][1])
        self.sup.model_flag_supported = lambda provider: True

    def uid(self) -> int:
        self.n += 1
        return self.n

    def say(self, text: str, **kw) -> dict:
        return self.bridge.handle_update(message(self.uid(), text, **kw))

    def tap(self, token: str, **kw) -> dict:
        return self.bridge.handle_update(callback(self.uid(), token, **kw))

    current: str | None = None

    def tokens(self, action: str, **match) -> list[tuple[str, dict]]:
        if self.current is not None and "pending_id" not in match:
            match["pending_id"] = self.current  # older /task cards stay in the interaction store; only the newest card is addressed
        found = []
        for path in sorted(self.tg_paths.interactions_dir.glob("*.json")):
            record = json.loads(path.read_text())
            if record.get("action") == action and all(record.get(k) == v for k, v in match.items()) and not record.get("consumed") and not record.get("superseded"):
                found.append((path.stem, record))
        if found:
            top = max(r.get("view_revision") or 0 for _, r in found)
            found = [(t, r) for t, r in found if (r.get("view_revision") or 0) == top]
        return found

    def one(self, action: str, **match) -> str:
        (token, _), = self.tokens(action, **match)
        return token

    def pending(self, pending_id: str) -> dict:
        return json.loads((self.paths.pending_starts_dir / f"{pending_id}.json").read_text())

    def acks(self) -> list[str]:
        return [payload["text"] for method, payload in self.api.calls if method == "answerCallbackQuery"]

    def task_card(self) -> str:
        self.current = None
        self.say("/task " + TASK)
        newest = max(self.paths.pending_starts_dir.glob("*.json"), key=lambda p: p.stat().st_mtime_ns)
        self.current = newest.stem
        return self.current

    def choose_saved(self, entry: dict) -> str:
        pending_id = self.task_card()
        self.tap(self.one("pair_menu"))
        self.tap(self.one("pair_use", pair_id=entry["pair_id"]))
        return pending_id

    def card_buttons(self) -> list[str]:
        return [b["text"] for row in (self.api.sent[-1].get("reply_markup") or {}).get("inline_keyboard", []) for b in row]

    def run_started_command(self) -> dict:
        (command,) = self.pending_inbox()
        return command

    def process_and_run(self) -> None:
        self.herdr.responses = self.plan_for(self.run_started_command()["session_selection"])
        self.sup.process_inbox(actions={"task"})
        self.sup.resume()


class SelectionTests(TgPairCase):
    def test_the_feature_is_invisible_until_an_administrator_enables_it(self) -> None:
        self.config["session_pairs"] = {"enabled": False, "allowed_roots": [], "instruction_root": None}
        self.say("/task " + TASK)
        self.assertFalse(any("Named pair" in text for text in self.card_buttons()))
        self.assertEqual(self.tokens("pair_menu"), [])
        self.assertEqual(self.say("/pair-set A | /x | /y")["result"], "pairs_disabled")

    def test_the_menu_lists_saved_pairs_and_selection_has_zero_effects(self) -> None:
        self.register("Alpha", ALPHA)
        self.register("Beta", BETA, self.repo_b, self.review_b)
        before = self.effects()
        pending_id = self.task_card()
        self.assertIn("Named pair…", self.card_buttons())
        self.tap(self.one("pair_menu"))
        buttons = self.card_buttons()
        self.assertEqual(sorted(b for b in buttons if b.startswith("Use saved pair")), ["Use saved pair: Alpha", "Use saved pair: Beta"])
        self.assertTrue({"Save existing pair…", "Create and save pair…", "Back to current sessions"} <= set(buttons))
        self.tap(self.one("pair_use", pair_id=self.register_lookup("Beta")))
        self.assertEqual(self.effects(), before, "choosing a pair launches, prompts, renames and rebinds nothing")
        self.assertTrue(self.owners_unchanged())
        self.assertEqual(self.pending_inbox(), [])
        self.assertEqual(self.pending(pending_id)["session_selection"]["pair"]["mode"], "saved")

    def register_lookup(self, name: str) -> str:
        return next(e["pair_id"] for e in hpair.list_entries(self.paths) if e["name"] == name)

    def test_the_final_card_shows_name_context_and_both_identities_and_the_command_carries_the_fingerprint(self) -> None:
        entry = self.register("Alpha", ALPHA, self.repo_a, self.review_a)
        pending_id = self.choose_saved(entry)
        card = self.api.sent[-1]["text"]
        for expected in ("Alpha", str(self.repo_a), str(self.review_a), ALPHA["claude"][1][:8], ALPHA["codex"][1][:8], ALPHA["claude"][0], ALPHA["codex"][0]):
            self.assertIn(expected, card)
        self.assertIn("Start task · reset budget 0", self.card_buttons())
        selection = self.pending(pending_id)["session_selection"]
        self.assertEqual(selection["pair"], {"mode": "saved", "pair_id": entry["pair_id"], "fingerprint": hpair.entry_fingerprint(entry)})
        start = self.one("reset_budget", budget=0)
        self.assertEqual(self.tap(start)["result"], "enqueued")
        command = self.run_started_command()
        self.assertEqual(command["session_selection"], selection, "pending record, token and command carry the same complete selection")
        self.process_and_run()
        self.assertEqual(self.prompt_targets(), {"alpha-codex"})
        self.assertEqual(self.state()["session_pair"]["fingerprint"], hpair.entry_fingerprint(entry))
        self.assertEqual(self.pending(pending_id)["status"], "started")

    def test_a_changed_registry_entry_invalidates_the_start_button_before_anything_is_queued(self) -> None:
        entry = self.register("Alpha", ALPHA)
        self.choose_saved(entry)
        start = self.one("reset_budget", budget=0)
        doc = self.registry_doc()
        doc["entries"][entry["pair_id"]]["context"]["review_base"] = str(self.review_b)
        hpair.registry_path(self.paths).write_text(json.dumps(doc))
        result = self.tap(start)
        self.assertEqual(result["rejected"], "pair_not_startable")
        self.assertIn("changed after this card was shown", self.api.sent[-1]["text"])
        self.assertEqual(self.pending_inbox(), [])
        self.assertEqual(self.effects()["prompts"], [])

    def test_unsettled_missing_or_moved_members_withhold_the_start_button_and_refuse_a_tap(self) -> None:
        entry = self.register("Alpha", ALPHA)
        for label, mutate in (("busy", lambda: self.herdr.agents["alpha-claude"].update(agent_status="working")), ("missing", lambda: self.herdr.agents.pop("alpha-codex")),
                              ("moved", lambda: self.herdr.agents["alpha-claude"].update(pane_id="w9:p9"))):
            with self.subTest(label):
                saved = json.loads(json.dumps(self.herdr.agents))
                self.choose_saved(entry)
                start = self.one("reset_budget", budget=0)
                mutate()
                result = self.tap(start)
                self.assertEqual(result["rejected"], "pair_not_startable")
                self.assertEqual(self.pending_inbox(), [])
                self.say("/task " + TASK)  # a fresh card for the same broken pair shows the reason and no Start button
                self.herdr.agents = saved

    def test_a_card_for_a_broken_pair_says_why_and_offers_no_start(self) -> None:
        entry = self.register("Alpha", ALPHA)
        self.herdr.agents["alpha-claude"]["agent_status"] = "working"
        self.choose_saved(entry)
        card = self.api.sent[-1]["text"]
        self.assertIn("Cannot start:", card)
        self.assertIn("not settled", card)
        self.assertFalse(any(b.startswith("Start task") for b in self.card_buttons()))

    def test_switching_while_a_run_is_active_is_withheld_and_the_old_card_stays_inert(self) -> None:
        entry = self.register("Alpha", ALPHA)
        self.choose_saved(entry)
        start = self.one("reset_budget", budget=0)
        beta = self.saved(self.register("Beta", BETA, self.repo_b, self.review_b))
        self.herdr.responses = self.plan_for(beta)
        self.assertTrue(self.start(beta)["ok"])
        self.sup.resume()
        self.assertEqual(self.state()["supervisor_state"], "WAIT_PLAN_APPROVAL")
        result = self.tap(start)
        self.assertEqual(result["rejected"], "pair_not_startable")
        self.assertEqual(self.pending_inbox(), [])
        self.say("/task " + TASK)
        self.assertIn("A task is still", self.api.sent[-1]["text"])

    def test_back_to_current_sessions_restores_the_legacy_selection_and_drops_any_pair(self) -> None:
        entry = self.register("Alpha", ALPHA)
        pending_id = self.choose_saved(entry)
        self.assertIn("pair", self.pending(pending_id)["session_selection"])
        self.tap(self.one("pair_menu"))
        self.tap(self.one("pair_clear"))
        self.assertEqual(self.pending(pending_id)["session_selection"], {"policy": "preserve", "profiles": {"codex": "default", "claude": "default"}})
        self.assertNotIn("Named pair (saved)", self.api.sent[-1]["text"])
        self.assertIn("Start task · reset budget 0", self.card_buttons())
        self.task_card()
        self.assertEqual(self.pending(pending_id)["status"], "superseded", "a newer /task supersedes the older pending start")


class SaveExistingTests(TgPairCase):
    def begin_existing(self) -> str:
        pending_id = self.task_card()
        self.tap(self.one("pair_menu"))
        self.tap(self.one("pair_existing"))
        return pending_id

    def pick(self, provider: str, spec: dict) -> dict:
        return self.tap(self.one("pair_pick", provider=provider, session_id=spec[provider][1]))

    def test_two_explicitly_chosen_verified_sessions_are_saved_and_run(self) -> None:
        before = self.effects()
        pending_id = self.begin_existing()
        self.assertTrue(any("Choose the existing Claude session" in m["text"] for m in self.api.sent))
        self.pick("claude", GAMMA)
        self.assertTrue(any("Choose the existing Codex session" in m["text"] for m in self.api.sent))
        self.pick("codex", GAMMA)
        self.assertIn("/pair-set NAME | REPOSITORY | REVIEW_BASE", self.api.sent[-1]["text"])
        self.assertNotIn("pair", self.pending(pending_id)["session_selection"])
        self.say(f"/pair-set Gamma Team | {self.repo_b} | {self.review_b}")
        selection = self.pending(pending_id)["session_selection"]
        self.assertEqual(selection["pair"]["name"], "Gamma Team")
        self.assertEqual(selection["pair"]["identities"], ids(GAMMA))
        card = self.api.sent[-1]["text"]
        for expected in ("Gamma Team", GAMMA["claude"][1][:8], GAMMA["codex"][1][:8], str(self.repo_b), str(self.review_b), "save the chosen existing sessions"):
            self.assertIn(expected, card)
        self.assertEqual(self.effects(), before)
        self.assertEqual(hpair.list_entries(self.paths), [], "nothing is registered by choosing")
        self.assertEqual(self.tap(self.one("reset_budget", budget=0))["result"], "enqueued")
        self.process_and_run()
        (entry,) = hpair.list_entries(self.paths)
        self.assertEqual((entry["name"], entry["source"], entry["identities"]), ("Gamma Team", "existing", ids(GAMMA)))
        self.assertEqual(self.prompt_targets(), {"gamma-codex"})
        self.assertTrue(self.owners_unchanged())

    def test_candidates_are_verified_settled_and_not_already_owned_by_a_saved_pair_or_query(self) -> None:
        self.register("Alpha", ALPHA)
        hs.atomic_write_json(self.paths.query_registry_file, {"schema_version": 1, "providers": {"codex": {"agent_name": "codex-query", "pane_id": "w6:p2", "session_id": GAMMA["codex"][1]}}})
        self.herdr.agents["beta-claude"]["agent_status"] = "working"
        self.begin_existing()
        sessions = {record["session_id"] for _, record in self.tokens("pair_pick", provider="claude")}
        self.assertIn(GAMMA["claude"][1], sessions)
        self.assertNotIn(ALPHA["claude"][1], sessions, "an already-saved session is not offered")
        self.assertNotIn(BETA["claude"][1], sessions, "a busy session is not offered")

    def test_a_pick_whose_session_stopped_being_a_candidate_is_refused(self) -> None:
        self.begin_existing()
        token = self.one("pair_pick", provider="claude", session_id=GAMMA["claude"][1])
        self.herdr.agents["gamma-claude"]["agent_status"] = "working"
        result = self.tap(token)
        self.assertEqual(result["rejected"], "invalid_pick")
        self.assertIn("no longer a verified", self.acks()[-1])

    def test_pair_set_validates_context_name_and_arity_without_widening_roots(self) -> None:
        pending_id = self.begin_existing()
        self.pick("claude", GAMMA)
        self.pick("codex", GAMMA)
        outside = self.project_root.parent
        before = json.dumps(self.pending(pending_id), sort_keys=True)
        cases = (("outside roots", f"G | {outside} | {self.review_b}", "outside the administrator-allowed roots"), ("missing dir", f"G | {self.project_root}/nope | {self.review_b}", "does not exist"),
                 ("relative", f"G | repo | {self.review_b}", "absolute"), ("bad name", f"a/b | {self.repo_b} | {self.review_b}", "path separators"),
                 ("long name", f"{'x' * 65} | {self.repo_b} | {self.review_b}", "1-64"), ("arity", f"G | {self.repo_b}", "Usage"), ("empty", " | | ", "Usage"))
        for label, argument, expected in cases:
            with self.subTest(label):
                self.say("/pair-set " + argument)
                self.assertIn(expected, self.api.sent[-1]["text"])
                self.assertEqual(json.dumps(self.pending(pending_id), sort_keys=True), before, "a rejected /pair-set changes nothing")
        self.assertEqual(self.pending_inbox(), [])

    def test_pair_set_without_a_waiting_choice_or_from_another_user_does_nothing(self) -> None:
        self.assertEqual(self.say(f"/pair-set G | {self.repo_b} | {self.review_b}")["result"], "none")
        self.begin_existing()
        result = self.say(f"/pair-set G | {self.repo_b} | {self.review_b}", user=999)
        self.assertIn("rejected", result)

    def test_a_name_that_is_already_saved_cannot_reach_a_startable_card(self) -> None:
        self.register("Gamma Team", ALPHA)
        pending_id = self.begin_existing()
        self.pick("claude", GAMMA)
        self.pick("codex", GAMMA)
        self.say(f"/pair-set gamma team | {self.repo_b} | {self.review_b}")
        self.assertIn("name is already used", self.api.sent[-1]["text"])
        self.assertFalse(any(b.startswith("Start task") for b in self.card_buttons()))
        self.assertEqual(self.pending(pending_id)["status"], "pending")


class CreateTests(TgPairCase):
    def test_create_needs_name_and_context_before_a_start_button_exists_and_then_creates_once(self) -> None:
        before = self.effects()
        pending_id = self.task_card()
        self.tap(self.one("pair_menu"))
        self.tap(self.one("pair_create"))
        self.assertEqual(self.pending(pending_id)["session_selection"]["policy"], "fresh-all")
        self.assertIn("/pair-set NAME | REPOSITORY | REVIEW_BASE", self.api.sent[-1]["text"])
        self.assertFalse(any(b.startswith("Start task") for b in self.card_buttons()))
        self.say(f"/pair-set Fresh Team | {self.repo_a} | {self.review_a}")
        self.assertIn("fresh sessions are created", self.api.sent[-1]["text"])
        self.assertIn("Fresh Team", self.api.sent[-1]["text"])
        self.assertEqual(self.effects(), before, "nothing is created until Start")
        self.assertEqual(self.tap(self.one("reset_budget", budget=0))["result"], "enqueued")
        command = self.run_started_command()
        self.assertEqual(command["session_selection"]["pair"], {"mode": "create", "name": "Fresh Team", "context": self.ctx(self.repo_a, self.review_a)})
        self.process_and_run()
        (entry,) = hpair.list_entries(self.paths)
        self.assertEqual((entry["name"], entry["source"]), ("Fresh Team", "created"))
        self.assertEqual(len(self.herdr.splits), 2)
        self.assertEqual(self.state()["session_pair"]["pair_id"], entry["pair_id"])
        self.assertTrue(self.owners_unchanged())

    def test_create_is_not_offered_when_fresh_sessions_are_unavailable(self) -> None:
        self.bridge.fresh_policy_reader = lambda policy: policy == "preserve"
        self.task_card()
        self.tap(self.one("pair_menu"))
        self.assertNotIn("Create and save pair…", self.card_buttons())

    def test_choosing_a_plain_policy_afterwards_abandons_the_half_made_pair_choice(self) -> None:
        pending_id = self.task_card()
        self.tap(self.one("pair_menu"))
        self.tap(self.one("pair_create"))
        self.assertIn("pair_draft", self.pending(pending_id))
        self.assertIn("Change pair…", self.card_buttons())
        self.tap(self.one("pair_menu"))
        self.tap(self.one("pair_clear"))
        self.assertNotIn("pair_draft", self.pending(pending_id))
        self.assertEqual(self.pending(pending_id)["session_selection"], {"policy": "preserve", "profiles": {"codex": "default", "claude": "default"}})
        self.assertIn("Start task · reset budget 0", self.card_buttons())


class CallbackSafetyTests(TgPairCase):
    def test_foreign_stale_and_replayed_pair_buttons_are_refused_without_effect(self) -> None:
        entry = self.register("Alpha", ALPHA)
        pending_id = self.task_card()
        menu = self.one("pair_menu")
        before = json.dumps(self.pending(pending_id), sort_keys=True)
        for label, update in (("wrong user", callback(self.uid(), menu, user=999)), ("wrong chat", callback(self.uid(), menu, chat=777)), ("group", callback(self.uid(), menu, chat_type="group"))):
            with self.subTest(label):
                self.bridge.handle_update(update)
                self.assertEqual(self.acks()[-1], "Not authorized.")
        self.assertEqual(json.dumps(self.pending(pending_id), sort_keys=True), before)
        self.tap(menu)
        use = self.one("pair_use", pair_id=entry["pair_id"])
        self.tap(use)
        self.assertEqual(self.tap(use)["rejected"], "Already used (one-time action).")
        old_menu = menu
        self.assertEqual(self.tap(old_menu)["rejected"], "Already used (one-time action).")
        self.assertEqual(self.pending_inbox(), [])

    def test_a_stale_button_cannot_revert_a_newer_pair_choice(self) -> None:
        alpha = self.register("Alpha", ALPHA)
        beta = self.register("Beta", BETA, self.repo_b, self.review_b)
        pending_id = self.task_card()
        self.tap(self.one("pair_menu"))
        use_alpha, use_beta = self.one("pair_use", pair_id=alpha["pair_id"]), self.one("pair_use", pair_id=beta["pair_id"])
        self.tap(use_alpha)
        result = self.tap(use_beta)
        self.assertIn("rejected", result)
        self.assertEqual(self.pending(pending_id)["session_selection"]["pair"]["pair_id"], alpha["pair_id"])

    def test_a_forged_pick_for_a_provider_or_session_never_creates_a_pair(self) -> None:
        pending_id = self.task_card()
        self.tap(self.one("pair_menu"))
        self.tap(self.one("pair_existing"))
        token = self.one("pair_pick", provider="claude", session_id=GAMMA["claude"][1])
        path = self.tg_paths.interactions_dir / f"{token}.json"
        record = json.loads(path.read_text())
        record["provider"] = "unknown"
        path.write_text(json.dumps(record))
        self.assertEqual(self.tap(token)["rejected"], "invalid_pick")
        self.assertEqual(self.pending(pending_id)["pair_draft"], {"mode": "save_existing", "identities": {}})


class StartContentionTests(TgPairCase):
    def test_simultaneous_start_taps_with_a_pair_enqueue_exactly_one_command_and_one_run(self) -> None:
        entry = self.register("Alpha", ALPHA)
        self.choose_saved(entry)
        start = self.one("reset_budget", budget=0)
        barrier = threading.Barrier(6)
        results: list = []

        def tap(update_id: int) -> None:
            barrier.wait()
            try:
                results.append(self.bridge.handle_update(callback(update_id, start)))
            except Exception as error:  # noqa: BLE001 - recorded; the assertion below decides
                results.append(error)

        threads = [threading.Thread(target=tap, args=(self.uid(),)) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual(len(self.pending_inbox()), 1, results)
        self.process_and_run()
        self.assertEqual(len(self.herdr.prompts), 1)
        self.assertEqual(self.prompt_targets(), {"alpha-codex"})


if __name__ == "__main__":
    import unittest

    unittest.main()
