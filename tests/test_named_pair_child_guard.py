"""Combined release tests: exact named parent, native route provenance and bound decisions.

All Herdr/provider activity is fake. Custom explorer-role evidence is synthetic,
as in the retained guard suite; no live custom-role support is claimed.
"""
import json
from unittest import mock

import herdr_codex_children as cc
import herdr_pairs as pairs
from pair_fixtures import ALPHA, BETA, ids
from test_codex_child_guard import World, hp_frame, rec, rollout_path, write_lines
from test_named_pairs_start import StartCase
from v2_fixtures import FakeHerdr


class NamedPairChildGuardTests(StartCase):
    def setUp(self):
        super().setUp()
        self.parent = ALPHA["codex"][1]
        self.world = World(self.codex_native_root, self.parent)
        self.plan = self.pair_plan_payload(self.review_a)
        question = self.question_payload(review_directory=str(self.plan.parent))
        self.question = self.plan.parent / "question_payload.json"
        self.question.write_bytes(question.read_bytes())
        self.bound = None
        self.turns = 0

    def response(self, gate="plan_approval", **options):
        payload = self.plan if gate == "plan_approval" else self.question

        def native(fake, name, text):
            self.assertEqual(name, "alpha-codex")
            run, turn = FakeHerdr.ids(text)
            final = hp_frame(run, turn, gate, "plan", "human", str(payload))
            if self.turns == 0:
                self.world.build(prompt=text, final=final, **options)
            else:
                # Preserve the original spawn/history across approvals and answers.
                path = rollout_path(self.codex_native_root, self.parent)
                records = [json.loads(line) for line in path.read_text().splitlines()]
                native_turn = f"continuation-{self.turns}"
                at = 30 * self.turns
                records += [
                    rec("event_msg", {"type": "task_started", "turn_id": native_turn}, at),
                    rec("response_item", {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}, at + 1),
                    rec("response_item", {"type": "message", "role": "assistant", "phase": "final_answer",
                                          "content": [{"type": "output_text", "text": final}],
                                          "internal_chat_message_metadata_passthrough": {"turn_id": native_turn}}, at + 2),
                    rec("event_msg", {"type": "task_complete", "turn_id": native_turn}, at + 3),
                ]
                write_lines(path, records)
            self.turns += 1

        # The pane proposes a DIFFERENT gate; only the exact parent's native final
        # may decide which gate is opened once a spawn has been recorded.
        wrong = "generic_question" if gate == "plan_approval" else "plan_approval"
        wrong_payload = self.question if wrong == "generic_question" else self.plan
        return {"v2": ("plan", "human", wrong, str(wrong_payload)), "before": native}

    def start_guarded(self, **options):
        self.assertTrue(self.start(self.saved(self.alpha))["ok"])
        self.bound = self.state()["session_pair"]
        self.herdr.responses = [self.response(**options)]
        self.sup.resume()

    def test_selected_parent_native_final_wins_over_another_pane_gate(self):
        with mock.patch.object(cc, "verify", wraps=cc.verify) as checks:
            self.start_guarded()
        self.assertEqual({call.args[0] for call in checks.call_args_list}, {self.parent})
        self.assertEqual(self.state()["pending_gate"]["gate_type"], "plan_approval")
        self.assertEqual(self.prompt_targets(), {"alpha-codex"})
        self.assertEqual(len(self.herdr.prompts), 1)
        self.assertEqual(self.herdr.sent_keys, [])
        self.assertEqual(self.sup.owners(), ids(ALPHA))
        self.assertEqual(cc.verify(self.parent, self.codex_native_root).kind, cc.COMPLETE)

    def test_other_presets_completed_child_cannot_clear_selected_pending_parent(self):
        # A fully compliant sibling preset must not be mistaken for our parent.
        World(self.codex_native_root, BETA["codex"][1]).build()
        # Keep the selected parent's pending child distinct from Beta's child.
        self.start_guarded(child=False, spawn_output="none", parent_end="open", mailbox="none")
        state = self.state()
        self.assertEqual(state["supervisor_state"], "WAIT_USER")
        self.assertIsNone(state["pending_gate"])
        self.assertEqual(self.prompt_targets(), {"alpha-codex"})
        self.assertEqual(len(self.herdr.prompts), 1)
        self.assertEqual(self.herdr.sent_keys, [])
        self.assertFalse([name for name, _ in self.herdr.reads if name == "alpha-codex"])
        self.assertEqual(state["session_pair"], self.bound)

    def test_missing_selected_history_refuses_before_prompt_even_with_complete_other_preset(self):
        World(self.codex_native_root, BETA["codex"][1]).build()
        rollout_path(self.codex_native_root, self.parent).unlink()
        self.assertTrue(self.start(self.saved(self.alpha))["ok"])
        self.herdr.responses = [self.response()]
        self.sup.resume()
        self.assertEqual(self.herdr.prompts, [])
        self.assertEqual(self.herdr.sent_keys, [])
        self.assertEqual(self.state()["supervisor_state"], "WAIT_USER")
        self.assertIsNone(self.state()["pending_gate"])

    def test_approval_and_answer_keep_original_pair_despite_registry_owner_and_alias_drift(self):
        self.start_guarded()
        pairs.registry_path(self.paths).write_text(json.dumps({"schema_version": 1, "revision": 99, "entries": {}, "reservations": {}}))
        self.paths.owners_file.write_text(json.dumps(ids(BETA)))
        self.herdr.agents["alpha-codex"]["name"] = None
        self.herdr.agents["beta-codex"]["name"] = self.config["agents"]["codex"]["name"]
        self.approve_pending()
        self.herdr.responses = [self.response("generic_question")]
        self.sup.resume()
        state = self.state()
        self.assertEqual(state["pending_gate"]["gate_type"], "generic_question")
        self.assertEqual(len(self.herdr.prompts), 2)
        with self.sup.store.transaction():
            state = self.sup.store.read_state()
            result = self.sup.answer_gate(state, run_id=state["run_id"], gate_id=state["pending_gate"]["gate_id"], actor="cli", answer="enum")
            self.assertTrue(result["ok"])
        self.herdr.responses = [self.response("generic_question")]
        self.sup.resume()
        self.assertEqual(self.state()["session_pair"], self.bound)
        self.assertEqual(self.prompt_targets(), {"alpha-codex"})
        self.assertEqual(len(self.herdr.prompts), 3)
        self.assertEqual(self.herdr.sent_keys, [])
        self.assertEqual(self.sup.owners(), ids(ALPHA))
        self.assertEqual(self.state()["pending_gate"]["gate_type"], "generic_question")
        self.assertEqual(cc.verify(self.parent, self.codex_native_root).kind, cc.COMPLETE)
