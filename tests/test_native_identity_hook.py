from __future__ import annotations

import json
import unittest
from pathlib import Path

from herdr_native_identity import native_hook_params

SESSION='11111111-1111-4111-8111-111111111111'

class NativeIdentityHookTests(unittest.TestCase):
    def setUp(self):
        self.event={'hook_event_name':'SessionStart','source':'resume','session_id':SESSION,'transcript_path':None,'cwd':'/private-not-exported'}
        self.env={'HERDR_ENV':'1','HERDR_PANE_ID':'w1:p2','HERDR_SOCKET_PATH':'/tmp/native-test.sock'}
        self.process={'pane_id':'w1:p2','foreground_processes':[{'argv':['codex','resume',SESSION,'--no-daemon']}]}

    def test_null_transcript_is_valid_only_for_pane_local_verified_process(self):
        result=native_hook_params(self.event,self.env,self.process)
        self.assertEqual(result['agent_session_id'],SESSION)
        self.assertEqual(result['pane_id'],'w1:p2')
        self.assertNotIn('transcript',json.dumps(result));self.assertNotIn('private',json.dumps(result))

    def test_shared_daemon_remote_process_and_wrong_pane_refuse(self):
        for argv in (['codex','resume',SESSION],['bash'],['codex','--no-daemon','--remote','unix:///tmp/other']):
            process={'pane_id':'w1:p2','foreground_processes':[{'argv':argv}]}
            self.assertIsNone(native_hook_params(self.event,self.env,process))
        self.process['pane_id']='w1:p3'
        self.assertIsNone(native_hook_params(self.event,self.env,self.process))

    def test_other_events_subagents_invalid_identity_and_source_refuse(self):
        for delta in ({'hook_event_name':'SubagentStart'},{'agent_id':'child'},{'source':'other'},{'session_id':'bad'}):
            self.assertIsNone(native_hook_params({**self.event,**delta},self.env,self.process))
        self.assertIsNone(native_hook_params(self.event,{**self.env,'HERDR_ENV':'0'},self.process))

    def test_current_recorded_resume_help_supports_local_server_switch(self):
        help_text=(Path(__file__).parent/'fixtures/providers/codex-0.160.1-resume-help.txt').read_text()
        self.assertIn('--no-daemon',help_text)
        self.assertIn('SESSION_ID',help_text)
