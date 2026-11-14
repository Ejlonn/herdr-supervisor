"""Delayed acknowledgements and fresh native consistency reads; no live providers."""
from __future__ import annotations

import json
from unittest import mock

import herdr_codex_children as cc
from test_native_results import NativeFlow, c_final, c_user
from v2_fixtures import FakeHerdr, V2Case, hs


class NativeStallTests(NativeFlow):
    def test_uncertain_ack_can_recover_the_exact_primary_native_final(self):
        self.assertEqual(self.claude_flow({'error': 'agent_prompt_stalled', 'status': 'idle', 'output': 'pane truncated', 'before': self.native_writer(path=self.claude_path)}), 2)
        state = self.state()
        self.assertEqual(state['delivery']['accepted_via'], 'native_primary_final')
        self.assertEqual(state['pending_gate']['gate_type'], 'generic_question')
        self.assert_nothing_sent_beyond_the_turn()

    def test_delayed_native_delivery_is_observed_without_second_submission(self):
        pending = {}
        sleep = self.sup.sleeper

        def capture(fake, name, text):
            if name == 'claude-main':
                pending.update(text=text, since=self.clock.time())

        def delayed(seconds):
            sleep(seconds)
            if pending and not pending.get('written') and self.clock.time() - pending['since'] >= 15:
                run, turn = FakeHerdr.ids(pending['text'])
                frame = FakeHerdr.block_v2(run, turn, 'brief', 'human', 'generic_question', str(self.question_payload()))
                with self.claude_path.open('a') as out:
                    for record in (c_user(pending['text']), c_final(frame)):
                        out.write(json.dumps(record) + '\n')
                pending['written'] = True

        self.sup.sleeper = delayed
        self.assertEqual(self.claude_flow({'error': 'agent_prompt_stalled', 'status': 'idle', 'output': '', 'before': capture}), 2)
        self.assertTrue(pending.get('written'))
        self.assertEqual(self.state()['delivery']['accepted_via'], 'native_primary_final')
        self.assert_nothing_sent_beyond_the_turn()
        self.assertTrue(self.events_named('stalled_delivery_settlement_wait'))

    def stopped(self):
        self.assertEqual(self.claude_flow({'error': 'agent_prompt_stalled', 'status': 'idle', 'output': ''}), 2)
        return self.state()

    def test_persistent_uncertainty_has_bounded_actionable_recovery(self):
        state = self.stopped()
        delivery = state['delivery']
        self.assertGreaterEqual(self.clock.time() - delivery['settlement_pending_since'], 60)
        self.assertLess(self.clock.time() - delivery['settlement_pending_since'], 90)
        self.assertEqual(state['missing_result']['turn_id'], delivery['turn_id'])
        self.assertNotIn('accepted_at', delivery)
        self.assert_nothing_sent_beyond_the_turn()

    def test_old_stopped_turn_recovers_with_no_resend_and_one_routing(self):
        state = self.stopped()
        state['missing_result'] = None  # persisted pre-fix stop did not offer a result retry
        self.sup.store.write_state(state)
        delivery = state['delivery']
        text = self.claude_prompts()[0][1]
        frame = FakeHerdr.block_v2(state['run_id'], delivery['turn_id'], 'fix', 'codex')
        with self.claude_path.open('a') as out:
            for record in (c_user(text), c_final(frame)):
                out.write(json.dumps(record) + '\n')
        result = self.sup.retry_routing_result(state, run_id=state['run_id'], turn_id=delivery['turn_id'], actor='operator')
        self.assertTrue(result['ok'], result)
        current = self.state()
        self.assertEqual(current['active_agent'], 'codex')
        self.assertEqual(current['native_recoveries'][-1]['turn_id'], delivery['turn_id'])
        self.assert_nothing_sent_beyond_the_turn()
        with self.assertRaises(hs.SupervisorError):
            self.sup.retry_routing_result(current, run_id=current['run_id'], turn_id=delivery['turn_id'], actor='operator')

    def test_old_stop_with_no_native_final_is_never_accepted(self):
        state = self.stopped()
        state['missing_result'] = None
        self.sup.store.write_state(state)
        result = self.sup.retry_routing_result(state, run_id=state['run_id'], turn_id=state['delivery']['turn_id'], actor='operator')
        self.assertFalse(result['ok'])
        self.assertNotIn('accepted_at', self.state()['delivery'])
        self.assertEqual(self.state()['supervisor_state'], 'WAIT_USER')
        self.assert_nothing_sent_beyond_the_turn()

    def test_uncertain_ack_with_conflicting_native_finals_never_accepts(self):
        writer = self.native_writer(path=self.claude_path, extra_final=lambda run, turn: [c_final(FakeHerdr.block_v2(run, turn, 'fix', 'codex'))])
        self.claude_flow({'error': 'agent_prompt_stalled', 'status': 'idle', 'output': '', 'before': writer})
        self.assertNotIn('accepted_at', self.state()['delivery'])
        self.assertIn('conflicting', self.state()['wait_user_reason'])
        self.assert_nothing_sent_beyond_the_turn()

    def test_uncertain_ack_with_only_a_child_final_never_accepts(self):
        def writer(fake, name, text):
            run, turn = FakeHerdr.ids(text)
            frame = FakeHerdr.block_v2(run, turn, 'fix', 'codex')
            with self.claude_path.open('a') as out:
                for record in (c_user(text), c_final(frame, sidechain=True)):
                    out.write(json.dumps(record) + '\n')
        self.claude_flow({'error': 'agent_prompt_stalled', 'status': 'idle', 'output': '', 'before': writer})
        self.assertNotIn('accepted_at', self.state()['delivery'])
        self.assertEqual(self.state()['supervisor_state'], 'WAIT_USER')
        self.assert_nothing_sent_beyond_the_turn()

    def test_current_stall_retry_requires_native_proof_even_if_pane_has_a_frame(self):
        state = self.stopped()
        turn = state['delivery']['turn_id']
        self.herdr.outputs['claude-main'] = FakeHerdr.block_v2(state['run_id'], turn, 'fix', 'codex')
        result = self.sup.retry_routing_result(state, run_id=state['run_id'], turn_id=turn, actor='operator')
        self.assertFalse(result['ok'])
        self.assertNotIn('accepted_at', self.state()['delivery'])
        self.assert_nothing_sent_beyond_the_turn()

    def test_native_none_and_unsupported_errors_never_prove_acceptance(self):
        for code in ('agent_blocked', 'permission_denied', None):
            self.assertFalse(self.sup._stalled_delivery({'status': 'uncertain', 'error_code': code}))
        self.assertFalse(self.sup._stalled_delivery({'status': 'completed', 'error_code': 'timeout', 'outcome': 'another_policy'}))

    def test_stalled_recovery_rejects_cross_run_and_cross_turn(self):
        state = self.stopped()
        state['missing_result'] = None
        for run, turn in (('another-run', state['delivery']['turn_id']), (state['run_id'], 'another-turn')):
            with self.assertRaises(hs.SupervisorError):
                self.sup.retry_routing_result(state, run_id=run, turn_id=turn, actor='operator')
        self.assert_nothing_sent_beyond_the_turn()


class NativeConsistencyTests(V2Case):
    def setUp(self):
        super().setUp()
        self.sup.codex_child_guard_enabled = True

    def check(self, sequence):
        with mock.patch.object(cc, 'verify', side_effect=sequence) as verify:
            result = self.sup._codex_child_check('codex')[0]
            return result, verify.call_count

    def test_transient_deadline_gets_one_fresh_successful_decision(self):
        result, calls = self.check([cc.Verdict(cc.UNKNOWN, 'the bounded native work exceeded 3 seconds'), cc.Verdict(cc.TRANSPARENT, 'complete no-child history')])
        self.assertEqual((result.kind, calls), ('clear', 2))
        self.assertEqual(self.herdr.prompts, [])

    def test_transient_extent_race_then_policy_violation_is_not_cleared(self):
        result, calls = self.check([cc.Verdict(cc.UNKNOWN, 'an evidence file changed after it was read (extent drift)'), cc.Verdict(cc.VIOLATION, 'second child')])
        self.assertEqual((result.kind, calls), ('violation', 2))

    def test_persistent_deadline_stops_at_three_complete_decisions(self):
        result, calls = self.check([cc.Verdict(cc.UNKNOWN, 'the bounded native work exceeded 3 seconds')] * 3)
        self.assertEqual((result.kind, calls), ('unknown', 3))
        self.assertEqual(self.herdr.prompts, [])

    def test_hard_limits_and_unsupported_evidence_are_not_retried(self):
        for detail in ('shared byte bound exceeded', 'record limit exceeded', 'unsupported record', 'primary identity changed'):
            with self.subTest(detail=detail):
                result, calls = self.check([cc.Verdict(cc.UNKNOWN, detail)])
                self.assertEqual((result.kind, calls), ('unknown', 1))

    def test_identity_change_during_backoff_stops_before_second_read(self):
        sleep = self.sup.sleeper

        def changed(seconds):
            sleep(seconds)
            self.herdr.agents['codex-main']['agent_session']['value'] = 'changed'

        self.sup.sleeper = changed
        result, calls = self.check([cc.Verdict(cc.UNKNOWN, 'the bounded native work exceeded 3 seconds')])
        self.assertEqual((result.kind, calls), ('identity', 1))
