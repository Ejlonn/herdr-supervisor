"""Task startup checks selected providers; later routing retains exact-session safety."""
import uuid

import herdr_supervisor as hs
from v2_fixtures import V2Case


class ProviderStartAvailabilityTests(V2Case):
    def test_codex_only_cli_finishes_with_claude_absent_and_owners_unchanged(self):
        del self.herdr.agents['claude-main']
        before = self.paths.owners_file.read_bytes()
        self.assertEqual(self.start_supervised([{'v2': ('investigate', 'done')}]), 0)
        self.assertEqual([n for n, _ in self.herdr.prompts], ['codex-main'])
        self.assertEqual(self.paths.owners_file.read_bytes(), before)

    def test_gated_codex_plan_starts_with_signed_out_claude(self):
        self.herdr.agents['claude-main'].pop('agent_session')
        self.write_plan()
        self.assertEqual(self.start_gated([{'v2': ('plan', 'human', 'plan_approval', str(self.plan_payload()))}]), 4)
        self.assertEqual(self.state()['supervisor_state'], 'WAIT_PLAN_APPROVAL')
        self.assertEqual([n for n, _ in self.herdr.prompts], ['codex-main'])

    def test_claude_only_start_does_not_require_live_codex(self):
        del self.herdr.agents['codex-main']
        self.assertEqual(self.start_supervised([{'v2': ('investigate', 'done')}], start='claude'), 0)
        self.assertEqual([n for n, _ in self.herdr.prompts], ['claude-main'])

    def test_required_provider_missing_refuses_before_any_prompt(self):
        del self.herdr.agents['codex-main']
        with self.assertRaisesRegex(hs.SupervisorError, 'required live agent is missing'):
            self.sup.initialize('task', 'codex', workflow_policy='gated_v2')
        self.assertEqual(self.herdr.prompts, [])

    def test_inactive_changed_identity_is_not_adopted_and_handoff_refuses(self):
        self.herdr.agents['claude-main']['agent_session']['value'] = str(uuid.uuid4())
        before = self.paths.owners_file.read_bytes()
        self.sup.initialize('task', 'codex', workflow_policy='gated_v2')
        with self.assertRaisesRegex(hs.SupervisorError, 'differs from recorded'):
            self.sup.ensure_agent('claude', allow_restore=False)
        self.assertEqual(self.paths.owners_file.read_bytes(), before)
        self.assertEqual(self.herdr.prompts, [])

    def test_fresh_all_requires_claude_before_side_effects(self):
        del self.herdr.agents['claude-main']
        with self.assertRaisesRegex(hs.SupervisorError, 'required live agent is missing'):
            self.sup.prepare_task_sessions({'policy': 'fresh-all'}, preparation_id=str(uuid.uuid4()))
        self.assertEqual(self.thread_journal.requests, [])
        self.assertEqual(self.herdr.prompts, [])

    def test_telegram_codex_task_preparation_accepts_unavailable_claude(self):
        del self.herdr.agents['claude-main']
        command = {'action': 'task', 'task_text': 'Codex investigation', 'request_id': 'codex-only-start',
                   'source': 'telegram', 'preallocated_run_id': str(uuid.uuid4()),
                   'workflow_policy': 'gated_v2', 'starting_agent': 'codex',
                   'codex_reset_authorization': {'budget': 0, 'available_count': 0, 'account_fingerprint': None}}
        result = self.sup.apply_command(None, command)
        self.assertTrue(result['ok'])
        self.assertEqual(self.state()['active_agent'], 'codex')
        self.assertEqual(self.herdr.prompts, [])

    def test_handoff_to_changed_claude_stops_without_submission(self):
        self.herdr.agents['claude-main']['agent_session']['value'] = str(uuid.uuid4())
        self.assertEqual(self.start_supervised([{'v2': ('investigate', 'claude')}]), 2)
        self.assertEqual(self.state()['supervisor_state'], 'WAIT_USER')
        self.assertEqual([n for n, _ in self.herdr.prompts], ['codex-main'])
        self.assertIn('claude', self.state()['wait_user_reason'])

    def test_duplicate_starting_identity_still_refuses(self):
        self.herdr.agents['duplicate'] = {**self.herdr.agents['codex-main'], 'pane_id': 'w9:p9', 'name': 'duplicate'}
        with self.assertRaisesRegex(hs.SupervisorError, 'more than one pane'):
            self.sup.initialize('task', 'codex', workflow_policy='gated_v2')
        self.assertEqual(self.herdr.prompts, [])
