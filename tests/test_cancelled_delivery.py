"""Run-bound cancellation settlement: original identities, no resend and no fake PASS."""
from __future__ import annotations

import copy
import json
import os
from types import SimpleNamespace
from unittest.mock import patch

import herdr_pairs as hpair
from pair_fixtures import ALPHA, BETA, PairCase
from v2_fixtures import FakeHerdr, V2Case, hs


class CancelledDeliveryTests(V2Case):
    def setUp(self) -> None:
        super().setUp()
        self.st = self.sup.initialize('cancelled task', 'codex', workflow_policy='gated_v2')
        self.st['supervisor_state'] = 'CANCELLED'
        self.st['worker_pid'] = None
        self.st['delivery'] = {'turn_id': 'cancelled-turn', 'agent': 'codex', 'status': 'accepted', 'prompt_sha256': 'a' * 64, 'accepted_at': 'original'}
        self.sup.store.write_state(self.st)
        self.herdr.splits = []
        self.calls: list = []
        self.sup.settle_guard = self.clear_guard
        self.command = {'action': 'cancel', 'run_id': self.st['run_id'], 'turn_id': 'cancelled-turn', 'settle_delivery': True, 'actor': 'operator'}

    def clear_guard(self, state, provider, purpose, **kwargs):
        self.calls.append((provider, purpose, kwargs))
        return SimpleNamespace(kind='clear', detail='fixture settled')

    def apply(self):
        return self.sup.apply_command(self.st, self.command)

    def assert_refused(self, text=None):
        before = copy.deepcopy(self.st)
        disk = self.paths.state_file.read_bytes()
        with self.assertRaises(hs.SupervisorError) as caught:
            self.apply()
        if text:
            self.assertIn(text, str(caught.exception))
        self.assertEqual(self.st, before)
        self.assertEqual(self.paths.state_file.read_bytes(), disk)
        self.assertEqual(self.herdr.prompts, [])
        self.assertEqual(self.herdr.splits, [])

    def test_settlement_preserves_history_and_does_not_mark_complete(self):
        before = copy.deepcopy(self.st)
        self.assertTrue(self.apply()['ok'])
        after = self.sup.store.read_state()
        self.assertEqual(after['supervisor_state'], 'CANCELLED')
        d = after['delivery']
        self.assertEqual(d['status'], 'cancelled')
        self.assertNotIn('completed_at', d)
        original = dict(d)
        receipt = original.pop('cancellation_settlement')
        original['status'] = receipt['previous_status']
        self.assertEqual(original, before['delivery'])
        self.assertEqual(receipt['run_id'], before['run_id'])
        self.assertEqual(receipt['turn_id'], 'cancelled-turn')
        for key in ('approvals', 'pending_gate', 'runtime_validation', 'native_sessions', 'task_text'):
            self.assertEqual(after.get(key), before.get(key))
        self.assertEqual({x[0] for x in self.calls}, {'codex', 'claude'})
        self.assertTrue(all(x[2] == {'wait': True, 'patience': 15.0, 'control': False} for x in self.calls))
        self.assertEqual(self.herdr.prompts, [])
        self.assertEqual(self.herdr.splits, [])

    def test_all_unresolved_statuses_can_be_cancelled_explicitly(self):
        for status in ('prepared', 'accepted', 'uncertain', 'interrupted'):
            with self.subTest(status=status):
                self.st['delivery'] = {'turn_id': 'cancelled-turn', 'agent': 'codex', 'status': status}
                self.assertTrue(self.apply()['ok'])
                self.assertEqual(self.st['delivery']['cancellation_settlement']['previous_status'], status)

    def test_second_settlement_is_idempotent_without_second_guard(self):
        self.apply()
        count = len(self.calls)
        before = self.paths.state_file.read_bytes()
        self.assertTrue(self.apply()['ok'])
        self.assertEqual(len(self.calls), count)
        self.assertEqual(self.paths.state_file.read_bytes(), before)

    def test_normal_cancel_keeps_delivery_until_explicit_settlement(self):
        self.st['supervisor_state'] = 'WAIT_USER'
        self.command.pop('settle_delivery')
        self.assertTrue(self.apply()['ok'])
        self.assertEqual(self.st['delivery']['status'], 'accepted')
        self.assertEqual(self.calls, [])

    def test_cross_run_refused(self):
        self.command['run_id'] = 'another-run'
        self.assert_refused('current run')

    def test_wrong_turn_refused(self):
        self.command['turn_id'] = 'another-turn'
        self.assert_refused('exact delivery turn')

    def test_live_or_done_run_cannot_use_recovery(self):
        for status in ('WAIT_USER', 'PAUSED', 'DONE', 'ERROR', 'RUNNING'):
            with self.subTest(status=status):
                self.st['supervisor_state'] = status
                self.assert_refused('already CANCELLED')

    def test_live_worker_refused(self):
        self.st['worker_pid'] = os.getpid()
        self.assert_refused('live worker')

    def test_other_recoveries_not_discarded(self):
        for key, value in (('continuation', {}), ('interrupted_delivery', {}), ('owner_recovery', {}), ('codex_reset', {'current_redemption_state': 'RESET_REQUESTED'})):
            with self.subTest(key=key):
                self.st[key] = value
                self.assert_refused('another unresolved recovery')
                self.st.pop(key)

    def test_unresolved_followup_refused(self):
        with patch.object(self.sup, 'followup_unresolved', return_value={'status': 'WAITING'}):
            self.assert_refused('another unresolved recovery')

    def test_missing_guard_refused(self):
        self.sup.settle_guard = None
        self.assert_refused('verification is unavailable')

    def test_guard_stops_are_not_weakened(self):
        for provider in ('codex', 'claude'):
            for kind in ('hold', 'unknown', 'identity', 'patience', 'violation', 'stop'):
                with self.subTest(provider=provider, kind=kind):
                    self.sup.settle_guard = lambda state, p, purpose, **kw: SimpleNamespace(kind=kind if p == provider else 'clear', detail='adverse')
                    self.assert_refused('child settlement did not clear')

    def test_identity_or_busy_changes_refuse_before_guard(self):
        for provider in ('codex', 'claude'):
            for field, value in (('agent_status', 'working'), ('agent_status', 'unknown'), ('pane_id', 'w9:p9'), ('agent_session', {'value': 'replacement'}), ('agent', 'other')):
                with self.subTest(provider=provider, field=field):
                    agent = self.herdr.agents[self.config['agents'][provider]['name']]
                    original = copy.deepcopy(agent)
                    agent[field] = value
                    count = len(self.calls)
                    self.assert_refused('original session')
                    self.assertEqual(len(self.calls), count)
                    self.herdr.agents[self.config['agents'][provider]['name']] = original

    def test_missing_session_refused(self):
        self.herdr.agents.pop(self.config['agents']['codex']['name'])
        self.assert_refused('original session')

    def test_duplicate_native_identity_refused(self):
        original = self.herdr.list_agents
        with patch.object(self.herdr, 'list_agents', side_effect=lambda: original() + [copy.deepcopy(original()[0])]):
            self.assert_refused('original session')

    def test_ownership_drift_refused(self):
        owners = copy.deepcopy(self.owners)
        owners['codex']['session_id'] = 'replacement'
        hs.atomic_write_json(self.paths.owners_file, owners, mode=0o600)
        self.assert_refused('original ownership')

    def test_frozen_pair_drift_refused(self):
        self.st['session_pair'] = {'identities': copy.deepcopy(self.owners)}
        self.st['session_pair']['identities']['codex']['pane_id'] = 'w9:p9'
        # a hand-built, incomplete snapshot is no valid frozen binding: refused before any identity is consulted
        self.assert_refused('binding is invalid')

    def test_change_during_guard_refused(self):
        def guard(state, provider, purpose, **kwargs):
            self.herdr.agents[self.config['agents']['codex']['name']]['agent_status'] = 'working'
            return SimpleNamespace(kind='clear', detail='stale idle')
        self.sup.settle_guard = guard
        self.assert_refused('original session')

    def test_completed_and_malformed_records_not_relabelled(self):
        for record in (None, {}, {'turn_id': 'cancelled-turn', 'status': 'completed'}, {'turn_id': 'cancelled-turn', 'status': 'cancelled'}):
            with self.subTest(record=record):
                self.st['delivery'] = record
                self.assert_refused()

    def test_settled_cancellation_clears_named_pair_boundary(self):
        import herdr_pairs
        self.assertIn('delivery', herdr_pairs.terminal_boundary_problem(self.st))
        self.apply()
        self.assertIsNone(herdr_pairs.terminal_boundary_problem(self.st))

    def test_durable_command_records_exact_recovery_once(self):
        command = {**self.command, 'request_id': 'cancel-recovery-fixture'}
        self.sup.enqueue_command(command)
        outcomes = self.sup.process_inbox()
        self.assertTrue(outcomes[0]['ok'])
        count = len(self.calls)
        self.sup.enqueue_command(command)
        self.assertEqual(self.sup.process_inbox(), [])
        self.assertEqual(len(self.calls), count)
        stored = json.loads(self.paths.state_file.read_text())
        self.assertEqual(stored['delivery']['status'], 'cancelled')


class CancelledNamedPairTests(PairCase):
    """A cancelled NAMED run settles through its own frozen pair, never through the global owners its released binding would resolve.
    The legacy pair (codex-main/claude-main) stays live next to the named pairs and must play no part."""

    def setUp(self) -> None:
        super().setUp()
        self.entry = self.register("Alpha", ALPHA)
        self.assertTrue(self.start(self.saved(self.entry))["ok"])
        state = self.state()
        state["delivery"] = {"turn_id": "original-turn", "agent": "codex", "status": "accepted", "prompt_sha256": "a" * 64, "accepted_at": "original"}
        self.sup.store.write_state(state)
        with self.sup.store.transaction():
            self.sup.apply_command(state, {"action": "cancel", "run_id": state["run_id"], "actor": "operator"})
        self.cancelled = self.state()
        self.assertEqual(self.cancelled["supervisor_state"], "CANCELLED")
        self.seen: list = []
        self.guard_owners: list = []
        self.sup.settle_guard = self.guard
        self.command = {"action": "cancel", "run_id": self.cancelled["run_id"], "turn_id": "original-turn", "settle_delivery": True, "actor": "operator"}
        self.owners_before = self.paths.owners_file.read_bytes()

    def guard(self, state, provider, purpose, **kwargs):
        self.seen.append((provider, purpose, kwargs.get("control")))
        self.guard_owners.append(self.sup.owners())
        return SimpleNamespace(kind="clear", detail="fixture settled")

    def settle(self) -> dict:
        with self.sup.store.transaction():
            return self.sup.apply_command(self.state(), self.command)

    def assert_refused(self, text: str) -> None:
        disk, registry = self.paths.state_file.read_bytes(), hpair.registry_path(self.paths).read_bytes()
        with self.assertRaises(hs.SupervisorError) as caught:
            self.settle()
        self.assertIn(text, str(caught.exception))
        self.assertEqual(self.paths.state_file.read_bytes(), disk, "a refusal rewrites nothing")
        self.assertEqual(hpair.registry_path(self.paths).read_bytes(), registry)
        self.assertEqual(self.paths.owners_file.read_bytes(), self.owners_before)
        self.assertEqual(self.herdr.prompts, [])

    def frozen_ids(self) -> dict:
        return {provider: dict(self.cancelled["session_pair"]["identities"][provider]) for provider in ("claude", "codex")}

    def test_the_released_binding_resolves_global_owners_but_the_recovery_uses_the_frozen_pair(self) -> None:
        legacy = self.sup.owners()
        self.assertNotEqual({p: legacy[p]["session_id"] for p in legacy}, {p: v["session_id"] for p, v in self.frozen_ids().items()}, "setup: the global owners are another pair")
        self.assertTrue(self.settle()["ok"])
        after = self.state()
        self.assertEqual(after["supervisor_state"], "CANCELLED")
        self.assertEqual(after["delivery"]["status"], "cancelled")
        self.assertEqual(after["delivery"]["cancellation_settlement"]["identities"], {p: {"session_id": v["session_id"], "pane_id": v["pane_id"]} for p, v in self.frozen_ids().items()})
        self.assertEqual(sorted(provider for provider, _, _ in self.seen), ["claude", "codex"], "both frozen sessions were guarded")
        self.assertTrue(all(owners == self.frozen_ids() for owners in self.guard_owners), "inside the recovery every owner lookup is the frozen pair")
        self.assertEqual(self.paths.owners_file.read_bytes(), self.owners_before, "the global owners are neither adopted nor rewritten")
        self.assertEqual(self.herdr.prompts, [])
        self.assertEqual(self.sup.owners(), legacy, "outside the recovery the released binding still resolves the legacy owners")

    def test_repeating_the_settlement_is_idempotent_and_sends_nothing(self) -> None:
        self.assertTrue(self.settle()["ok"])
        calls = len(self.seen)
        again = self.settle()
        self.assertIn("already settled", again["message"])
        self.assertEqual(len(self.seen), calls)

    def test_registry_changes_cannot_retarget_a_frozen_run(self) -> None:
        hpair.registry_path(self.paths).write_text("{not json")
        self.assertTrue(self.settle()["ok"], "the run's own snapshot is authority, not the mutable registry")
        self.assertTrue(all(owners == self.frozen_ids() for owners in self.guard_owners))

    def test_a_missing_changed_duplicate_or_busy_original_session_is_refused_before_any_guard(self) -> None:
        name = "alpha-codex"
        original = dict(self.herdr.agents[name])
        cases = {
            "missing": (lambda: self.herdr.agents.pop(name), "original session is missing"),
            "changed session": (lambda: self.herdr.agents.update({name: FakeHerdr.agent("codex", name, original["pane_id"], "99999999-9999-4999-8999-999999999999")}), "original session is missing"),
            "moved pane": (lambda: self.herdr.agents.update({name: FakeHerdr.agent("codex", name, "w9:p9", ALPHA["codex"][1])}), "original session is missing"),
            "busy": (lambda: self.herdr.agents.update({name: FakeHerdr.agent("codex", name, original["pane_id"], ALPHA["codex"][1], "working")}), "not settled"),
            "duplicate carrier": (lambda: self.herdr.agents.update({"alpha-codex-copy": FakeHerdr.agent("codex", "alpha-codex-copy", "w8:p8", ALPHA["codex"][1])}), "original session is missing"),
        }
        for label, (mutate, text) in cases.items():
            with self.subTest(label):
                self.herdr.agents[name] = dict(original)
                self.herdr.agents.pop("alpha-codex-copy", None)
                mutate()
                self.assert_refused(text)
                self.assertEqual(self.seen, [], "no child guard is consulted for an unproven identity")

    def test_a_guard_that_does_not_clear_refuses_and_changes_nothing(self) -> None:
        self.sup.settle_guard = lambda *args, **kwargs: SimpleNamespace(kind="hold", detail="a helper is still running")
        self.assert_refused("child settlement did not clear")

    def test_a_snapshot_that_disagrees_with_the_recorded_native_sessions_is_refused(self) -> None:
        state = self.state()
        state["native_sessions"]["codex"] = BETA["codex"][1]
        self.sup.store.write_state(state)
        self.assert_refused("original ownership cannot be verified")
        self.assertEqual(self.seen, [])

    def test_another_pairs_live_sessions_cannot_stand_in_for_the_frozen_ones(self) -> None:
        self.herdr.agents.pop("alpha-codex")
        self.assert_refused("original session is missing")
        self.assertIn("beta-codex", self.herdr.agents, "the other pair is live and idle but is never adopted")
