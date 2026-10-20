"""Recorded failure classes from October 2 storage exhaustion; all operations isolated."""
import errno
import io
import json
from contextlib import redirect_stderr
from unittest.mock import patch

import herdr_telegram as ht
import telegram_api as tg
from test_telegram import FakeApi, TelegramCase, message


class ResilienceTests(TelegramCase):
    def test_startup_webhook_network_retry_and_recovery(self):
        self.api.fail['getWebhookInfo'] = [tg.TelegramError('DNS_FAILURE', 'private'), None]
        with redirect_stderr(io.StringIO()) as err:
            self.assertEqual(self.bridge.run(max_rounds=2), 0)
        self.assertEqual(self.clock.sleeps, [1.0])
        self.assertNotIn('private', err.getvalue())
        self.assertEqual(ht.bridge_health(self.tg_paths, now=self.clock.time())['status'], 'HEALTHY')
        self.assertEqual(len(self.herdr.prompts), 0)

    def test_heartbeat_storage_failure_recovers_without_inbound_ack(self):
        original = self.bridge.heartbeat
        calls = []
        def heartbeat(note):
            calls.append(note)
            if len(calls) <= 2:
                raise OSError(errno.ENOSPC, 'private')
            original(note)
        self.api.updates_batches = [[message(1, '/status')]]
        with patch.object(self.bridge, 'heartbeat', side_effect=heartbeat), redirect_stderr(io.StringIO()) as err:
            self.bridge.run(max_rounds=2)
        self.assertEqual(self.clock.sleeps, [1.0])
        self.assertEqual(self.bridge.read_offset(), 2)
        self.assertEqual(len([c for c in self.api.calls if c[0] == 'getUpdates']), 1)
        self.assertNotIn('private', err.getvalue())

    def test_offset_failure_replays_journal_not_command(self):
        update = message(1, '/pause')
        self.plan_gate()
        self.api.updates_batches = [[update], [update]]
        original = self.bridge.write_offset
        count = 0
        def write(offset):
            nonlocal count
            count += 1
            if count == 1:
                raise OSError(errno.EACCES, 'private')
            original(offset)
        with patch.object(self.bridge, 'write_offset', side_effect=write), redirect_stderr(io.StringIO()):
            self.bridge.run(max_rounds=2)
        self.assertEqual(self.bridge.read_offset(), 2)
        self.assertEqual(len(self.pending_inbox()), 1)

    def test_update_journal_failure_preserves_offset_and_enqueue_identity(self):
        self.plan_gate()
        update = message(1, '/pause')
        self.api.updates_batches = [[update], [update]]
        original = ht.hs.atomic_write_json
        failed = False
        def write(path, value, **kwargs):
            nonlocal failed
            if path == self.bridge.journal_path(1) and not failed:
                failed = True
                self.assertEqual(self.bridge.read_offset(), 0)
                raise OSError(errno.ENOSPC, 'private')
            return original(path, value, **kwargs)
        with patch.object(ht.hs, 'atomic_write_json', side_effect=write), redirect_stderr(io.StringIO()):
            self.bridge.run(max_rounds=2)
        self.assertTrue(failed)
        self.assertEqual(self.bridge.read_offset(), 2)
        self.assertEqual(len(self.pending_inbox()), 1)

    def test_daemon_passes_bounded_outbound_batch_limits(self):
        with patch.object(self.bridge, 'deliver_outbox', return_value={}) as outbox, patch.object(self.bridge, 'deliver_document_queue', return_value={}) as documents:
            self.bridge.run(max_rounds=1)
        outbox.assert_called_once_with(limit=4)
        documents.assert_called_once_with(limit=2)

    def test_outbound_failure_cannot_starve_inbound(self):
        with patch.object(self.bridge, 'deliver_outbox', side_effect=tg.TelegramError('HTTPS_FAILURE', 'private')), redirect_stderr(io.StringIO()):
            self.bridge.run(max_rounds=3)
        self.assertEqual(len([c for c in self.api.calls if c[0] == 'getUpdates']), 3)
        self.assertEqual(ht.bridge_health(self.tg_paths, now=self.clock.time())['status'], 'BACKOFF')
        self.assertEqual(self.clock.sleeps, [1.0, 2.0, 4.0])

    def test_health_is_conservative_and_does_not_echo_untrusted_fields(self):
        for record, expected in [
            ({'at_unix': self.clock.time(), 'note': 'poll'}, 'UNKNOWN'),
            ({'at_unix': self.clock.time()-121}, 'STALE'),
            ({'health_version': 1, 'at_unix': self.clock.time(), 'phase': 'backoff', 'last_poll_success': self.clock.time()}, 'BACKOFF'),
            ({'health_version': 1, 'at_unix': self.clock.time(), 'phase': 'private'}, 'UNKNOWN'),
        ]:
            self.tg_paths.heartbeat_file.write_text(json.dumps(record))
            self.assertEqual(ht.bridge_health(self.tg_paths, now=self.clock.time()), {'status': expected})

    def test_programming_os_errors_are_not_swallowed(self):
        with patch.object(self.bridge, 'poll_once', side_effect=OSError(errno.EINVAL, 'bug')):
            with self.assertRaises(OSError):
                self.bridge.run(max_rounds=1)

    def test_boot_probe_is_read_only_and_conservative(self):
        report = ht.boot_readiness(home=self.tg_paths.state_dir)
        self.assertEqual(report['headless_autostart'], 'not_verified')
        self.assertIn('unknown', report['live_server'])
        self.assertIn('never start headless', report['guidance'])
        self.assertEqual(ht.boot_readiness(home=self.tg_paths.state_dir, server_probe=lambda: True)['live_server'], 'available')
        self.assertEqual(ht.boot_readiness(home=self.tg_paths.state_dir, server_probe=lambda: False)['live_server'], 'unavailable')

    def test_get_me_startup_retries_without_new_process(self):
        api = FakeApi()
        api.fail['getMe'] = [tg.TelegramError('DNS_FAILURE', 'private'), None]
        with patch.object(ht, 'load_telegram_config', return_value=self.tg_config), patch.object(ht.hs, 'load_config', return_value=self.config), patch.object(ht.time, 'sleep') as sleep, redirect_stderr(io.StringIO()) as err:
            bridge = ht.build_bridge(self.tg_paths, api=api)
        self.assertIs(bridge.api, api)
        sleep.assert_called_once_with(1.0)
        self.assertNotIn('private', err.getvalue())

    def test_invalid_health_types_are_unknown(self):
        self.tg_paths.heartbeat_file.write_text(json.dumps({'at_unix': self.clock.time(), 'health_version': 1, 'phase': {}}))
        self.assertEqual(ht.bridge_health(self.tg_paths, now=self.clock.time())['status'], 'UNKNOWN')

    def test_healthy_api_does_not_mask_bridge_failure(self):
        report = ht.doctor_summary(network=True, tg_paths=self.tg_paths, api_factory=lambda _: FakeApi())
        self.assertEqual(report['checks']['network_api'], 'healthy')
        self.assertEqual(report['status'], 'BRIDGE_NOT_HEALTHY')

    def test_f1_late_outbound_success_behind_four_failures_survives_restart(self):
        self.plan_gate()
        self.bridge.deliver_outbox()
        with self.sup.store.transaction():
            state = self.state()
            for i in range(5):
                self.sup.emit_event(state, 'COMMAND_RESULT', {'action': 'status', 'ok': True, 'message': f'queue-{i}'})
            self.sup.store.write_state(state)
        original = self.bridge.send
        attempts = []
        def send(chat, text, **kwargs):
            attempts.append(text)
            if any(f'queue-{i}' in text for i in range(4)):
                raise tg.TelegramError('DNS_FAILURE', 'private')
            return original(chat, text, **kwargs)
        self.bridge.send = send
        self.bridge.deliver_outbox(limit=4)
        self.assertEqual(len(attempts), 4)
        second = self.make_bridge()
        second.send = send
        second.deliver_outbox(limit=4)
        self.assertEqual(len(attempts), 8)
        self.assertTrue(any('queue-4' in text for text in self.texts()))

    def test_f1_document_queue_fairness_and_persistent_cursor(self):
        for i in range(3):
            path = self.tg_paths.document_deliveries_dir / f'{i}.json'
            path.write_text(json.dumps({'status': 'pending', 'artifact_id': f'fixture-{i}'}))
        attempts = []
        def attempt(path, delivery):
            attempts.append(path.name)
            return False
        with patch.object(self.bridge, '_attempt_queued_document', side_effect=attempt):
            self.bridge.deliver_document_queue(limit=2)
        second = self.make_bridge()
        with patch.object(second, '_attempt_queued_document', side_effect=attempt):
            second.deliver_document_queue(limit=2)
        self.assertEqual(attempts, ['0.json', '1.json', '2.json', '0.json'])
        self.assertEqual(json.loads((self.tg_paths.document_deliveries_dir/'0.json').read_text())['status'], 'pending')

    def test_f1_skipped_items_rotate_and_corrupt_cursor_is_advisory(self):
        self.assertEqual(self.bridge._fair_batch(['skip', 'late'], ['a', 'b'], 1, 'fixture'), ['skip'])
        self.assertEqual(self.make_bridge()._fair_batch(['skip', 'late'], ['a', 'b'], 1, 'fixture'), ['late'])
        (self.tg_paths.state_dir/'fixture-cursor.json').write_text('{')
        self.assertEqual(self.bridge._fair_batch(['skip', 'late'], ['a', 'b'], 1, 'fixture'), ['skip'])

    def test_f3_malformed_failure_times_never_prove_recovery(self):
        record = {'health_version': 1, 'at_unix': self.clock.time(), 'phase': 'healthy', 'last_poll_success': self.clock.time()}
        for value in [True, False, '100', {}, [], float('nan'), float('inf'), -1, 10**400]:
            self.tg_paths.heartbeat_file.write_text(json.dumps({**record, 'last_failure_at': value}))
            self.assertEqual(ht.bridge_health(self.tg_paths, now=self.clock.time())['status'], 'UNKNOWN')
        for value in [None, self.clock.time()-1]:
            self.tg_paths.heartbeat_file.write_text(json.dumps({**record, 'last_failure_at': value}))
            self.assertEqual(ht.bridge_health(self.tg_paths, now=self.clock.time())['status'], 'HEALTHY')

    def test_f4_terminal_refusal_survives_failed_diagnostic_writes(self):
        original = ht.hs.atomic_write_json
        for category in ['AUTH_FAILURE', 'POLLER_CONFLICT', 'WEBHOOK_CONFLICT']:
            bridge = self.make_bridge()
            self.api.calls = []
            self.api.fail = {}
            self.api.webhook_url = 'https://example.invalid' if category == 'WEBHOOK_CONFLICT' else ''
            if category != 'WEBHOOK_CONFLICT':
                self.api.fail['getUpdates'] = [tg.TelegramError(category, 'private')]
            def write(path, value, **kwargs):
                if path == self.tg_paths.conflict_file or (path == self.tg_paths.heartbeat_file and value.get('phase') == 'stopped'):
                    raise OSError(errno.ENOSPC, 'private')
                return original(path, value, **kwargs)
            with patch.object(ht.hs, 'atomic_write_json', side_effect=write), redirect_stderr(io.StringIO()) as err:
                self.assertEqual(bridge.run(max_rounds=3), 2)
            self.assertEqual(bridge.stopped_reason, category)
            self.assertNotIn('private', err.getvalue())
            polls = len([c for c in self.api.calls if c[0] == 'getUpdates'])
            self.assertEqual(polls, 0 if category == 'WEBHOOK_CONFLICT' else 1)
