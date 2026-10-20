"""Missing Claude statusLine limits at process startup are not a provider refusal."""
import json

import herdr_supervisor as hs
from v2_fixtures import NOW, V2Case


class ClaudeQuotaRestartTests(V2Case):
    def empty_snapshot(self, target_provider='claude', **overrides):
        provider = target_provider
        session = self.owners[provider]['session_id']
        payload = {'provider': provider, 'fetched_at_unix': NOW, 'windows': [],
                   'session_quota_only': True, 'session_windows': {session: []}}
        payload.update(overrides)
        self.sup.quota_path(provider).write_text(json.dumps(payload))

    def test_claude_first_turn_with_unknown_quota_is_submitted_once(self):
        self.empty_snapshot()
        self.assertEqual(self.start_supervised([{'v2': ('investigate', 'done')}], start='claude'), 0)
        self.assertEqual([n for n, _ in self.herdr.prompts], ['claude-main'])
        with self.assertRaisesRegex(hs.QuotaError, 'no windows'):
            self.sup.quota('claude')  # not fabricated as non-blocking quota evidence

    def test_unknown_codex_still_refuses_and_submits_nothing(self):
        self.empty_snapshot('codex')
        self.assertEqual(self.start_supervised([]), 2)
        self.assertEqual(self.herdr.prompts, [])

    def test_invalid_or_unscoped_empty_telemetry_refuses_before_prompt(self):
        cases = [{'provider': 'codex'}, {'fetched_at_unix': None},
                 {'fetched_at_unix': NOW + 1000}, {'session_windows': {}},
                 {'session_windows': {'different-session': []}}, {'session_quota_only': 'yes'},
                 {'session_quota_only': False, 'session_windows': {}},
                 {'session_windows': {self.owners['claude']['session_id']: None}}]
        for delta in cases:
            with self.subTest(delta=delta):
                self.empty_snapshot(**delta)
                with self.assertRaises(hs.QuotaError):
                    self.sup.startup_blocking_windows({}, 'claude')
        self.assertEqual(self.herdr.prompts, [])

    def test_unknown_cannot_clear_confirmed_quota_wait_or_interrupted_delivery(self):
        self.empty_snapshot()
        for state in ({'quota_wait': {'provider': 'claude'}}, {'interrupted_delivery': {'agent': 'claude'}}):
            with self.assertRaises(hs.QuotaError):
                self.sup.startup_blocking_windows(state, 'claude')
        usable, _, _ = self.sup._usable_quota_evidence({'workflow_policy': 'gated_v2'}, 'claude',
                                                     {'started_at_unix': NOW - 10, 'resume_at': NOW - 1})
        self.assertFalse(usable)

    def test_real_blocking_window_remains_blocking_at_startup(self):
        self.write_quota('claude', 0, NOW + 3600, 50, NOW + 86400)
        blocking = self.sup.startup_blocking_windows({}, 'claude')
        self.assertEqual(len(blocking), 1)
        self.assertEqual(blocking[0].kind, 'five_hour')

    def test_missing_telemetry_cannot_explain_a_missing_routing_result(self):
        self.empty_snapshot()
        self.assertEqual(self.start_supervised([{'output': 'No routing result'}], start='claude'), 2)
        self.assertEqual(self.state()['supervisor_state'], 'WAIT_USER')
        self.assertEqual(len(self.herdr.prompts), 1)
        self.assertTrue(self.state()['wait_user_requires_action'])

    def test_idle_empty_snapshot_age_never_becomes_positive_quota_evidence(self):
        self.empty_snapshot(fetched_at_unix=NOW - 10000)
        self.assertEqual(self.sup.startup_blocking_windows({}, 'claude'), ())
        with self.assertRaises(hs.QuotaError):
            self.sup.quota('claude')
