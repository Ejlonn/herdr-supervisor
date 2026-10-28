"""Issue 5 Telegram path: the real bridge handler with a fake Bot API and the real inbox/state/interaction
boundaries. Every selection button must give bounded visible feedback; stale, duplicate, foreign and unavailable
choices must neither enqueue a second task nor revert a newer choice. No network, no real owner session."""

from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path

from test_telegram import CHAT, OWNER, TelegramCase, callback, message

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "telegram" / "session_profile_callback.json"
TASK = "Issue 5 fixture task: inspect a read-only fixture and report."


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()[:16]


class Issue5TelegramCase(TelegramCase):
    def setUp(self) -> None:
        super().setUp()
        self.config["session_start"]["model_profiles"]["codex"]["large"] = {"label": "Large", "model": "gpt-approved"}
        self.config["session_start"]["model_profiles"]["claude"]["fast"] = {"label": "Fast", "model": "claude-fast"}
        self.bridge = self.make_bridge()
        # Hermetic: the default reader probes `claude --help` / `codex --help` on PATH, so these tests would depend on the host.
        self.bridge.model_capability_reader = lambda provider: True
        self.update_id = 1000

    def next_id(self) -> int:
        self.update_id += 1
        return self.update_id

    def interactions(self, action: str, *, newest: bool = True, **match: object) -> list[tuple[str, dict]]:
        """Live (unconsumed) tokens of one action; by default only those on the newest card (highest view revision)."""
        found = []
        for path in sorted(self.tg_paths.interactions_dir.glob("*.json")):
            record = json.loads(path.read_text())
            if record.get("action") == action and all(record.get(k) == v for k, v in match.items()) and not record.get("consumed") and not record.get("superseded"):
                found.append((path.stem, record))
        if newest and found:
            top = max(record.get("view_revision") or 0 for _, record in found)
            found = [(token, record) for token, record in found if (record.get("view_revision") or 0) == top]
        return found

    def start_card(self, policy: str = "fresh-all") -> str:
        """/task, then choose the fresh-session policy; returns the pending id."""
        self.bridge.handle_update(message(self.next_id(), "/task " + TASK))
        (token, record), = self.interactions("session_policy", policy=policy)
        self.tap(token)
        return record["pending_id"]

    def tap(self, token: str, **kwargs) -> dict:
        return self.bridge.handle_update(callback(self.next_id(), token, **kwargs))

    def acks(self) -> list[str]:
        return [payload["text"] for method, payload in self.api.calls if method == "answerCallbackQuery"]

    def pending(self, pending_id: str) -> dict:
        return json.loads((self.paths.pending_starts_dir / f"{pending_id}.json").read_text())

    def profile_token(self, provider: str, profile: str) -> str:
        (token, _), = self.interactions("session_profile", provider=provider, profile=profile)
        return token


class SelectionFeedbackTests(Issue5TelegramCase):
    def test_every_selection_button_gets_a_specific_acknowledgement_and_a_refreshed_card(self) -> None:
        pending_id = self.start_card()
        sent_before = len(self.api.sent)
        result = self.tap(self.profile_token("codex", "large"))
        self.assertEqual(result["result"], "selection_updated")
        self.assertEqual(self.acks()[-1], "Codex model set to Large.")
        self.assertGreater(len(self.api.sent), sent_before, "the refreshed card is visible")
        self.assertIn("✓ Codex model: Large", json.dumps(self.api.sent[-1]["reply_markup"], ensure_ascii=False))
        self.assertEqual(self.pending(pending_id)["session_selection"]["profiles"], {"codex": "large", "claude": "default"})
        self.tap(self.profile_token("claude", "fast"))
        self.assertEqual(self.acks()[-1], "Claude model set to Fast.")
        self.assertEqual(self.pending(pending_id)["session_selection"]["profiles"], {"codex": "large", "claude": "fast"}, "the second choice keeps the first")

    def test_reselecting_the_same_choice_still_acknowledges_and_refreshes(self) -> None:
        pending_id = self.start_card()
        revision = self.pending(pending_id)["view_revision"]
        sent_before = len(self.api.sent)
        self.assertEqual(self.tap(self.profile_token("codex", "default"))["result"], "selection_updated")
        self.assertEqual(self.acks()[-1], "Codex model unchanged: Default (already selected).")
        self.assertEqual(self.pending(pending_id)["view_revision"], revision + 1)
        self.assertGreater(len(self.api.sent), sent_before)

    def test_only_default_configured_says_so_and_does_not_imply_a_known_model(self) -> None:
        del self.config["session_start"]["model_profiles"]["codex"]["large"]
        del self.config["session_start"]["model_profiles"]["claude"]["fast"]
        self.start_card()
        card = self.api.sent[-1]["text"]
        self.assertIn("Codex: only Default is configured (no alternative models)", card)
        self.assertIn("Claude: only Default is configured", card)
        self.assertIn("inherits the provider's own configuration", card)
        self.assertEqual(self.tap(self.profile_token("codex", "default"))["result"], "selection_updated")
        self.assertIn("unchanged", self.acks()[-1])

    def test_with_alternatives_configured_the_default_only_note_is_absent(self) -> None:
        self.start_card()
        self.assertNotIn("only Default is configured", self.api.sent[-1]["text"])

    def test_duplicate_and_stale_choices_cannot_revert_a_newer_selection_or_enqueue_a_task(self) -> None:
        pending_id = self.start_card()
        old_default = self.profile_token("codex", "default")
        large = self.profile_token("codex", "large")
        self.tap(large)
        self.assertEqual(self.pending(pending_id)["session_selection"]["profiles"]["codex"], "large")
        self.assertEqual(self.tap(large)["rejected"], "Already used (one-time action).")
        self.assertEqual(self.acks()[-1], "Already used (one-time action).")
        result = self.tap(old_default)
        self.assertIn("rejected", result)
        self.assertTrue(self.acks()[-1].startswith("Superseded") or "stale" in self.acks()[-1].lower() or "no longer" in self.acks()[-1].lower(), self.acks()[-1])
        self.assertEqual(self.pending(pending_id)["session_selection"]["profiles"]["codex"], "large", "a stale tap never reverts the newer choice")
        self.assertEqual(self.pending_inbox(), [])

    def test_foreign_and_malformed_callbacks_are_acknowledged_without_effect(self) -> None:
        pending_id = self.start_card()
        token = self.profile_token("codex", "large")
        before = self.pending(pending_id)
        cases = (
            ("wrong user", callback(self.next_id(), token, user=999), "Not authorized."),
            ("wrong chat", callback(self.next_id(), token, chat=777), "Not authorized."),
            ("group chat", callback(self.next_id(), token, chat_type="group"), "Not authorized."),
            ("malformed", callback(self.next_id(), "x"), "Unknown action."),
            ("unknown token", callback(self.next_id(), "A" * 32), "This button is no longer valid."),
        )
        for label, update, expected in cases:
            with self.subTest(label):
                self.bridge.handle_update(update)
                self.assertEqual(self.acks()[-1], expected)
        self.assertEqual(self.pending(pending_id), before)
        self.assertEqual(self.pending_inbox(), [])

    def test_unavailable_profile_and_failure_paths_give_visible_feedback(self) -> None:
        pending_id = self.start_card()
        token = self.profile_token("claude", "fast")
        self.bridge.model_capability_reader = lambda provider: False
        self.assertEqual(self.tap(token)["rejected"], "invalid_model_capability")
        self.assertEqual(self.acks()[-1], "That model capability is no longer available.")
        self.assertEqual(self.pending(pending_id)["session_selection"]["profiles"]["claude"], "default")
        self.bridge.model_capability_reader = lambda provider: True
        token = self.profile_token("claude", "fast")
        self.bridge._send_start_confirmation = lambda chat_id, record: (_ for _ in ()).throw(OSError("disk"))
        result = self.tap(token)
        self.assertEqual(result["rejected"], "selection_error")
        self.assertIn("could not be applied", self.acks()[-1])
        self.assertIn("nothing was started", self.acks()[-1])
        self.assertEqual(self.pending_inbox(), [])

    def test_the_default_model_probe_is_cached_so_acknowledgements_do_not_wait_on_repeated_spawns(self) -> None:
        calls = []
        import herdr_telegram
        original = herdr_telegram.probe_model_flag
        herdr_telegram.probe_model_flag = lambda command: calls.append(command) or True
        self.addCleanup(setattr, herdr_telegram, "probe_model_flag", original)
        bridge = self.make_bridge()
        bridge.model_capability_reader = bridge._cached_model_flag
        for _ in range(5):
            self.assertTrue(bridge.model_capability_reader("claude"))
        self.assertEqual(len(calls), 1)
        self.clock.sleep(bridge.MODEL_FLAG_TTL_SECONDS + 1)
        bridge.model_capability_reader("claude")
        self.assertEqual(len(calls), 2)


class AvailabilityFeedbackTests(Issue5TelegramCase):
    def test_hidden_fresh_codex_explains_why_before_any_request(self) -> None:
        reasons = ["helper unit herdr-codex-reset-worker-abc.path is disabled; enable it explicitly"]
        self.bridge = self.make_bridge()
        self.bridge.fresh_policy_reader = lambda policy: policy == "preserve"
        self.bridge.fresh_reason_reader = lambda: reasons
        self.bridge.handle_update(message(self.next_id(), "/task " + TASK))
        card = self.api.sent[-1]
        self.assertIn("Fresh Codex is unavailable: helper unit", card["text"])
        self.assertEqual(self.interactions("session_policy", policy="fresh-codex"), [])
        self.assertEqual(self.interactions("session_policy", policy="fresh-all"), [])
        self.assertEqual(self.pending_inbox(), [])

    def test_a_fresh_choice_whose_capability_vanishes_is_refused_at_tap_time(self) -> None:
        self.bridge.handle_update(message(self.next_id(), "/task " + TASK))
        (token, record), = self.interactions("session_policy", policy="fresh-codex")
        self.bridge.fresh_policy_reader = lambda policy: policy == "preserve"
        result = self.tap(token)
        self.assertEqual(result["rejected"], "invalid_session_capability")
        self.assertEqual(self.acks()[-1], "Fresh session creation is no longer available.")
        self.assertEqual(self.pending(record["pending_id"])["session_selection"]["policy"], "preserve")

    def test_card_availability_uses_the_supervisors_shared_predicate(self) -> None:
        self.sup.direct_launch_readiness = lambda policy=None: {"ready": False, "reasons": ["pane run is not verified"], "fingerprint": None, "evidence": "structural-read-only"}
        self.assertFalse(self.sup.fresh_policy_supported("fresh-codex"))
        self.sup.direct_launch_readiness = lambda policy=None: {"ready": True, "reasons": [], "fingerprint": "a" * 64, "evidence": "structural-read-only"}
        self.assertTrue(self.sup.fresh_policy_supported("fresh-codex"))
        self.sup.helper_readiness = lambda *a, **k: {"ready": False, "reasons": ["the quota-reset watcher is disabled"], "fingerprint": None, "units": None, "activation": {}, "evidence": "structural-read-only"}
        self.assertTrue(self.sup.fresh_policy_supported("fresh-codex"), "the quota-reset helper is not part of the direct launch")
        self.config["session_start"]["codex_launch_method"] = "agent-start"  # DR2: the selected method cannot start a NEW fresh Codex, so the card must not offer it
        other = self.make_supervisor()
        other.direct_launch_readiness = lambda policy=None: {"ready": True, "reasons": [], "fingerprint": "a" * 64, "evidence": "structural-read-only"}
        self.assertFalse(other.fresh_policy_supported("fresh-codex") or other.fresh_policy_supported("fresh-all"))
        self.assertTrue(other.fresh_policy_supported("fresh-claude"))


class StartEnvelopeTests(Issue5TelegramCase):
    def test_selected_profiles_reach_the_start_envelope_helper_payload_and_claude_launch(self) -> None:
        pending_id = self.start_card()
        self.tap(self.profile_token("codex", "large"))
        self.tap(self.profile_token("claude", "fast"))
        (start, _), = self.interactions("reset_budget", budget=0)
        self.assertEqual(self.tap(start)["result"], "enqueued")
        (command,) = self.pending_inbox()
        self.assertEqual(command["session_selection"], {"policy": "fresh-all", "profiles": {"codex": "large", "claude": "fast"}})
        self.sup.model_flag_supported = lambda provider: True
        record = self.sup.prepare_task_sessions(command["session_selection"], preparation_id=command["preallocated_run_id"])
        self.assertEqual(record["providers"]["codex"]["launch_request"]["model"], "gpt-approved")
        self.assertEqual(self.thread_journal.requests, [])
        self.assertEqual(self.herdr.pane_runs[0][1].split()[-3:], ["--model", "gpt-approved", "--no-daemon"])
        starts = {kind: args for _, kind, _, args in self.herdr.starts}
        self.assertEqual(starts["claude"], ["--model", "claude-fast"])
        self.assertEqual(record["providers"]["claude"]["configured_model"], "claude-fast")
        self.assertEqual(record["providers"]["codex"]["configured_model"], "gpt-approved")
        self.assertEqual(pending_id, command["preallocated_run_id"])

    def test_duplicate_sequential_start_callbacks_enqueue_one_task(self) -> None:
        self.start_card()
        (start, _), = self.interactions("reset_budget", budget=0)
        self.assertEqual(self.tap(start)["result"], "enqueued")
        for _ in range(3):
            self.assertIn("rejected", self.tap(start))
        self.assertEqual(len(self.pending_inbox()), 1)

    def test_simultaneous_start_callbacks_and_enqueues_yield_exactly_one_command(self) -> None:
        self.start_card()
        (start, _), = self.interactions("reset_budget", budget=0)
        barrier = threading.Barrier(6)
        results: list = []
        # Widen the check-then-write window so an unserialised enqueue cannot pass by luck.
        import time as _time

        import herdr_workflow
        real_write = herdr_workflow.atomic_write_json
        herdr_workflow.atomic_write_json = lambda *args, **kwargs: (_time.sleep(0.15), real_write(*args, **kwargs))[1]
        self.addCleanup(setattr, herdr_workflow, "atomic_write_json", real_write)
        def tap(update_id: int) -> None:
            barrier.wait()
            try:
                results.append(self.bridge.handle_update(callback(update_id, start)))
            except Exception as error:  # noqa: BLE001 - recorded, the assertion below decides
                results.append(error)
        threads = [threading.Thread(target=tap, args=(self.next_id(),)) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual(len(self.pending_inbox()), 1, results)
        # whatever the interleaving, the worker creates the run exactly once
        self.herdr.responses = [{"v2": ("plan", "human", "plan_approval", "-")}]
        self.sup.process_inbox(actions={"task"})
        self.assertEqual(len([p for p in (self.paths.inbox_dir / "pending").glob("*.json")]), 0)

    def test_fixture_matches_the_live_callback_shape(self) -> None:
        self.start_card()
        (token, record), = self.interactions("session_profile", provider="codex", profile="large")
        captured = {
            "token": f"tok-{digest(token)}", "token_length": len(token),
            "action": record["action"], "provider": record["provider"], "profile": record["profile"], "view_revision": record["view_revision"],
            "owner_user_digest": digest(record["owner_user_id"]), "chat_digest": digest(record["chat_id"]),
            "pending_task_digest": digest(record["task_sha256"]), "pending_id_shape": "uuid" if len(record["pending_id"]) == 36 else "other",
            "record_keys": sorted(record),
        }
        self.assertEqual(sorted(json.loads(FIXTURE.read_text())["record_keys"]), captured["record_keys"], "the redacted fixture records the real interaction schema")
        fixture = json.loads(FIXTURE.read_text())
        for key in ("action", "provider", "profile", "view_revision", "token_length", "pending_id_shape"):
            self.assertEqual(fixture[key], captured[key], key)
        for key in ("token", "owner_user_digest", "chat_digest", "pending_task_digest"):
            self.assertRegex(fixture[key], r"^(tok-)?[0-9a-f]{16}$")
        self.assertNotIn(str(OWNER), json.dumps(fixture))
        self.assertNotIn(str(CHAT), json.dumps(fixture))
