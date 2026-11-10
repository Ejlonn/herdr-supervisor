"""Run-bound cancellation settlement: original identities, no resend and no fake PASS."""
from __future__ import annotations

import copy
import json
import os
from types import SimpleNamespace
from unittest.mock import patch

from v2_fixtures import V2Case, hs


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
        self.assert_refused('original ownership')

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
