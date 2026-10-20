"""Task reports describe the current task without inventing execution evidence."""
import unittest

import herdr_present as hp
import herdr_telegram as ht


class TaskReportReadabilityTests(unittest.TestCase):
    def report(self, state, event, events=None, artifacts=None):
        return hp.render_final_report_markdown(ht.build_final_report_fields(state, event, events or [], artifacts=artifacts))

    def test_operator_handoff_is_readable_and_lists_actual_remaining_work(self):
        note = 'Local_commit_abc123_reviewed_TEST_and_push_still_pending'
        state = {'run_id': 'run-a', 'task_text': 'Fix the 4/25 source warning shown in bildirim.jpeg',
                 'completion': {'mode': 'operator_handoff', 'unmet': ['runtime_validation_pass', 'push_approval']},
                 'approvals': [{'gate_type': 'plan_approval', 'action': 'approved', 'actor': 'private-operator'}]}
        event = {'run_id': 'run-a', 'type': 'TASK_HANDED_OFF', 'data': {'handoff': note}}
        text = self.report(state, event, [{'type': 'COMMAND_RESULT', 'data': {'action': 'approve', 'ok': True}}])
        self.assertIn('Closed by you', text)
        self.assertIn('Run and record the remaining TEST validation', text)
        self.assertIn('Obtain push approval', text)
        self.assertIn('Local commit abc123 reviewed TEST and push still pending', text)
        self.assertNotIn(note, text)
        for unwanted in ('WAIT_QUOTA', 'polling cadence', '/ask', 'Telegram tests', 'see the implementation handoff', 'private-operator', 'approve: ok', 'always require'):
            self.assertNotIn(unwanted, text)
        self.assertEqual(event['data']['handoff'], note)

    def test_readonly_completion_does_not_claim_implementation_or_tests(self):
        text = self.report({'run_id': 'run-a', 'task_text': 'Investigate tender feedback',
                            'runtime_policy': {'runtime_validation_required': False}},
                           {'run_id': 'run-a', 'type': 'TASK_DONE', 'data': {'handoff': 'Investigation_complete'}})
        self.assertIn('Investigation complete', text)
        self.assertIn('not required by the approved task profile', text)
        self.assertIn('local test results are not independently verified', text)
        self.assertNotIn('Tests passed', text)

    def test_historical_event_never_borrows_current_task_evidence(self):
        text = self.report({'run_id': 'new', 'task_text': 'Other private task',
                            'runtime_evidence': {'result': 'PASS', 'environment': 'PROD', 'candidate_sha': 'other'}},
                           {'run_id': 'old', 'type': 'TASK_DONE', 'data': {}})
        for unwanted in ('Other private task', 'PROD', 'Recorded runtime result: PASS'):
            self.assertNotIn(unwanted, text)
        self.assertIn('Task details were not retained', text)

    def test_reports_are_run_scoped_and_no_final_report_self_reference(self):
        text = self.report({'run_id': 'run-a', 'task_text': 'Current task'},
                           {'run_id': 'run-a', 'type': 'TASK_DONE', 'data': {}}, artifacts=[
                               {'run_id': 'run-a', 'category': 'handoff', 'display_name': 'handoff.md'},
                               {'run_id': 'run-a', 'category': 'handoff', 'display_name': 'handoff.md'},
                               {'run_id': 'other', 'category': 'report', 'display_name': 'private-other.md'},
                               {'run_id': 'run-a', 'category': 'final_report', 'display_name': 'old-generated.md'}])
        self.assertEqual(text.count('handoff.md'), 1)
        self.assertNotIn('private-other', text)
        self.assertNotIn('old-generated', text)

    def test_proposed_runtime_is_not_reported_as_accepted(self):
        text = self.report({'run_id': 'run-a', 'runtime_evidence_proposal': {'result': 'PASS'}},
                           {'run_id': 'run-a', 'type': 'TASK_DONE', 'data': {}})
        self.assertIn('has not recorded a runtime validation result', text)
        self.assertNotIn('Recorded runtime result: PASS', text)
