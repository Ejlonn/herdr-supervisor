from __future__ import annotations

import json

from test_telegram import TelegramCase, callback, message


class SessionTelegramMockTests(TelegramCase):
    def test_mock_task_start_plan_revision_approval_and_sessions_round_trip(self):
        # Exercise real bridge/inbox/gates with fake Bot API and fake native transport.
        # No real owner, native session, credential or live Telegram offset is touched.
        self.write_plan()
        task = 'Mock readiness task: inspect a fixture read-only and return a short report.'
        result = self.bridge.handle_update(message(1, '/task ' + task))
        self.assertEqual(result['result'], 'reset_budget_required')
        token = next(p.stem for p in self.tg_paths.interactions_dir.glob('*.json')
                     if (lambda r: r.get('action') == 'reset_budget' and r.get('budget') == 0)(json.loads(p.read_text())))
        self.assertEqual(self.bridge.handle_update(callback(2, token))['result'], 'enqueued')
        payload = self.plan_payload(runtime_validation_required=False, rebuild_required=False, push_approval_required=False, rebuild_reason='read-only mock; no product changes')
        self.herdr.responses = [{'v2': ('plan', 'human', 'plan_approval', str(payload))}]
        self.assertEqual(self.sup.worker(), 4)
        self.assertEqual(self.state()['supervisor_state'], 'WAIT_PLAN_APPROVAL')
        self.bridge.deliver_outbox()
        before = self.paths.state_file.read_bytes()
        prompts = len(self.herdr.prompts)
        self.assertEqual(self.bridge.handle_update(message(3, '/sessions'))['result'], 'sessions')
        self.assertIn('Sessions and helpers', self.texts()[-1])
        self.assertEqual(self.paths.state_file.read_bytes(), before)
        self.assertEqual(len(self.herdr.prompts), prompts)
        self.bridge.handle_update(message(4, '/revise make the mock report shorter'))
        self.sup.process_inbox()
        self.herdr.responses = [{'v2': ('plan', 'human', 'plan_approval', str(payload))}]
        self.assertEqual(self.sup.worker(), 4)
        self.assertEqual(self.state()['supervisor_state'], 'WAIT_PLAN_APPROVAL')
        self.bridge.deliver_outbox()
        gate = self.state()['pending_gate']
        approve_token = next(p.stem for p in self.tg_paths.interactions_dir.glob('*.json')
                             if (lambda r: r.get('action') == 'approve' and r.get('gate_id') == gate['gate_id'])(json.loads(p.read_text())))
        self.assertEqual(self.bridge.handle_update(callback(5, approve_token))['result'], 'enqueued')
        self.sup.process_inbox()
        self.assertEqual(self.state()['supervisor_state'], 'RUNNING')
        self.assertIsNotNone(self.state()['approved_plan'])
        self.assertNotEqual(self.state()['pending_gate'], gate)
        self.herdr.responses = [{'v2': ('review', 'done', 'none', '-')}]
        self.assertEqual(self.sup.worker(), 0)
        self.assertEqual(self.state()['supervisor_state'], 'DONE')
        self.bridge.deliver_outbox()
        self.assertIn('Task complete', '\n'.join(self.texts()))

    def test_sessions_rejects_unauthenticated_operator_without_reply(self):
        self.assertIn('rejected', self.bridge.handle_update(message(10, '/sessions', user=999)))
        self.assertEqual(self.api.sent, [])
