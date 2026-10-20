from __future__ import annotations

import contextlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from herdr_core import HerdrError, SupervisorError
from herdr_helpers import HelperManager, job_prompt, parse_job_result, readonly_process

PARENT='11111111-1111-4111-8111-111111111111'
CHILD='22222222-2222-4222-8222-222222222222'
RUN='33333333-3333-4333-8333-333333333333'
TURN='44444444-4444-4444-8444-444444444444'

class HelpersTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup); self.root=Path(self.tmp.name)
        self.state={'supervisor_state':'PAUSED','run_id':RUN,'active_agent':'codex','delivery':{'turn_id':TURN},'pending_gate':{'gate_id':'untouched'}}
        self.owners={'codex':{'pane_id':'w1:p1','session_id':PARENT},'claude':{'pane_id':'w1:p9','session_id':'55555555-5555-4555-8555-555555555555'}}
        self.agents=[self.agent('w1:p1',PARENT),self.agent('w1:p2',CHILD)]
        self.herdr=Mock();self.herdr.list_agents.side_effect=lambda:self.agents
        self.herdr.process_info.side_effect=lambda pane:self.process(pane)
        self.herdr.prompt_ack_supported.return_value=True
        paths=SimpleNamespace(state_dir=self.root,query_registry_file=self.root/'queries.json',query_owner_file=self.root/'query.json')
        self.sup=SimpleNamespace(paths=paths,config={'helpers':{'enabled':True},'review_root':str(self.root)},herdr=self.herdr,owners=lambda:self.owners,clock=lambda:1000,
                                 store=SimpleNamespace(read_state=lambda:self.state,transaction=contextlib.nullcontext))
        self.manager=HelperManager(self.sup)
        self.instructions=self.root/'instructions.md';self.instructions.write_text('Inspect the test fixture, return a short summary. Do not edit files.')

    def agent(self,pane,session,status='idle'):
        return {'agent':'codex','pane_id':pane,'agent_status':status,'agent_session':{'agent':'codex','kind':'id','value':session}}

    def process(self,pane):
        return {'pane_id':pane,'foreground_processes':[{'name':'codex','pid':123,'cwd':str(self.root),'argv':['codex','--sandbox','read-only','--ask-for-approval','never','--model','gpt-6.1-sol']}]}

    def register(self):
        return self.manager.register('reader','w1:p2','gpt-6.1-sol',str(self.root),apply=True,expected_session=CHILD)

    def dispatch(self):
        self.register();return self.manager.dispatch('reader',self.instructions)

    def output(self,job):
        return json.dumps({'helper_result_version':1,**{k:job[k] for k in ('job_id','nonce','instruction_sha256')},'outcome':'done','summary':'Checked fixture.'})

    def test_explicit_registration_does_not_prompt_or_touch_parent(self):
        before=json.dumps([self.state,self.owners],sort_keys=True)
        preview=self.manager.register('reader','w1:p2','gpt-6.1-sol',str(self.root))
        self.assertFalse(preview['applied']);self.assertEqual(self.manager.registry(),{})
        self.register();self.herdr.prompt_ack.assert_not_called();self.herdr.read_agent.assert_not_called()
        self.assertEqual(json.dumps([self.state,self.owners],sort_keys=True),before)

    def test_dispatch_collect_at_most_once_and_parent_unchanged(self):
        before=json.dumps([self.state,self.owners],sort_keys=True);result=self.dispatch();job=self.manager._job(result['job_id'])
        self.assertEqual(result['status'],'ACCEPTED');self.herdr.prompt_ack.assert_called_once()
        self.herdr.read_agent.return_value=self.output(job)
        collected=self.manager.collect(job['job_id']);self.assertEqual(collected['status'],'COMPLETED')
        self.assertEqual(self.manager.collect(job['job_id'])['status'],'COMPLETED');self.herdr.read_agent.assert_called_once()
        self.assertEqual(json.dumps([self.state,self.owners],sort_keys=True),before)

    def test_uncertain_restart_never_resends(self):
        self.herdr.prompt_ack.side_effect=HerdrError('timeout')
        result=self.dispatch();self.assertEqual(result['status'],'UNCERTAIN')
        restored=HelperManager(self.sup);job=restored._job(result['job_id']);self.herdr.read_agent.return_value=self.output(job)
        self.assertEqual(restored.collect(job['job_id'])['status'],'COMPLETED');self.herdr.prompt_ack.assert_called_once()

    def test_crash_after_submit_leaves_submitting_and_rereads_only(self):
        self.herdr.prompt_ack.side_effect=KeyboardInterrupt
        self.register()
        with self.assertRaises(KeyboardInterrupt):self.manager.dispatch('reader',self.instructions)
        job=HelperManager(self.sup).jobs()[0];self.assertEqual(job['status'],'SUBMITTING')
        self.herdr.read_agent.return_value=self.output(job)
        self.assertEqual(HelperManager(self.sup).collect(job['job_id'])['status'],'COMPLETED');self.herdr.prompt_ack.assert_called_once()

    def test_prompt_echo_and_duplicate_results_do_not_parse(self):
        result=self.dispatch();job=self.manager._job(result['job_id'])
        with self.assertRaises(SupervisorError):parse_job_result(job_prompt(job,'brief'),job)
        with self.assertRaises(SupervisorError):parse_job_result(self.output(job)+'\n'+self.output(job),job)
        wrong=json.loads(self.output(job));wrong['nonce']='wrong'
        with self.assertRaises(SupervisorError):parse_job_result(json.dumps(wrong),job)

    def test_busy_blocked_missing_duplicate_moved_refuse_without_prompt(self):
        original=list(self.agents)
        for agents in ([original[0]], [original[0],self.agent('w1:p3',CHILD)], [*original,self.agent('w1:p3',CHILD)],
                       [original[0],self.agent('w1:p2',CHILD,'working')],[original[0],self.agent('w1:p2',CHILD,'blocked')]):
            self.agents=agents
            with self.assertRaises(SupervisorError):self.register()
        self.herdr.prompt_ack.assert_not_called();self.assertEqual(self.manager.registry(),{})

    def test_wrong_permissions_or_model_refuse(self):
        for args in (['codex'],['codex','--sandbox','workspace-write','--model','gpt-6.1-sol'],
                     ['codex','--sandbox','read-only','--ask-for-approval','never','--model','other']):
            info=self.process('w1:p2');info['foreground_processes'][0]['argv']=args
            self.assertFalse(readonly_process(info,'gpt-6.1-sol',str(self.root)))
        info=self.process('w1:p2');info['foreground_processes'][0]['argv'].append('--yolo')
        self.assertFalse(readonly_process(info,'gpt-6.1-sol',str(self.root)))

    def test_protected_owner_is_not_helper(self):
        with self.assertRaises(SupervisorError):self.manager.register('reader','w1:p1','gpt-6.1-sol',str(self.root),apply=True,expected_session=CHILD)

    def test_cancel_busy_keeps_slot_and_does_not_send_input(self):
        result=self.dispatch();self.agents[1]['agent_status']='working'
        self.assertEqual(self.manager.cancel(result['job_id'])['status'],'CANCEL_REQUESTED')
        with self.assertRaises(SupervisorError):self.manager.collect(result['job_id'])
        self.herdr.prompt_ack.assert_called_once();self.herdr.read_agent.assert_not_called()
        self.agents[1]['agent_status']='idle'
        self.assertEqual(self.manager.collect(result['job_id'])['status'],'CANCELLED')

    def test_stale_parent_artifact_and_deadline_refuse(self):
        result=self.dispatch();job_id=result['job_id'];self.herdr.read_agent.return_value='unused'
        self.state['delivery']['turn_id']=CHILD
        with self.assertRaisesRegex(SupervisorError,'parent changed'):self.manager.collect(job_id)
        self.state['delivery']['turn_id']=TURN;self.instructions.write_text('changed')
        with self.assertRaisesRegex(SupervisorError,'instructions changed'):self.manager.collect(job_id)
        job=self.manager._job(job_id);job['deadline']=999;self.manager._save(job)
        with self.assertRaisesRegex(SupervisorError,'deadline'):self.manager.collect(job_id)
        self.herdr.read_agent.assert_not_called()

    def test_disabled_and_active_parent_refuse(self):
        self.sup.config['helpers']['enabled']=False
        with self.assertRaises(SupervisorError):self.register()
        self.sup.config['helpers']['enabled']=True;self.register();self.state['supervisor_state']='RUNNING'
        with self.assertRaisesRegex(SupervisorError,'pause'):self.manager.dispatch('reader',self.instructions)
        self.herdr.prompt_ack.assert_not_called()

    def test_credentials_and_outside_instructions_refused(self):
        self.register();self.instructions.write_text('password=do-not-send-this')
        with self.assertRaises(SupervisorError):self.manager.dispatch('reader',self.instructions)
        self.herdr.prompt_ack.assert_not_called()

    def test_registration_apply_is_bound_to_preview_session(self):
        with self.assertRaisesRegex(SupervisorError, 'exact --session-id'):
            self.manager.register('reader','w1:p2','gpt-6.1-sol',str(self.root),apply=True,expected_session=PARENT)
        self.assertEqual(self.manager.registry(),{})

    def test_abandon_releases_slot_but_quarantines_busy_worker(self):
        result=self.dispatch();self.agents[1]['agent_status']='working'
        self.assertEqual(self.manager.cancel(result['job_id'],abandon=True)['status'],'ABANDONED')
        self.assertTrue(self.manager.registry()['reader']['quarantined'])
        with self.assertRaisesRegex(SupervisorError,'abandoned'):
            self.manager.dispatch('reader',self.instructions)
        with self.assertRaises(SupervisorError):self.register()
        self.agents[1]['agent_status']='idle';self.register()
        self.assertFalse(self.manager.registry()['reader'].get('quarantined',False))
        self.herdr.prompt_ack.assert_called_once()

    def test_two_jobs_cap_and_single_worker_job(self):
        self.register();self.manager.dispatch('reader',self.instructions)
        with self.assertRaises(SupervisorError):self.manager.dispatch('reader',self.instructions)
        self.herdr.prompt_ack.assert_called_once()

    def test_working_helper_does_not_poll_transcript_or_reprompt(self):
        result=self.dispatch();self.agents[1]['agent_status']='working'
        for _ in range(10):
            with self.assertRaises(SupervisorError):self.manager.collect(result['job_id'])
        self.herdr.prompt_ack.assert_called_once();self.herdr.read_agent.assert_not_called()

    def test_more_than_two_helpers_do_not_expand_active_budget(self):
        self.register();self.manager.dispatch('reader',self.instructions)
        for name, pane, session in [('reader2','w1:p3','66666666-6666-4666-8666-666666666666'),('reader3','w1:p4','77777777-7777-4777-8777-777777777777')]:
            self.agents.append(self.agent(pane,session))
            self.manager.register(name,pane,'gpt-6.1-sol',str(self.root),apply=True,expected_session=session)
        self.manager.dispatch('reader2',self.instructions)
        with self.assertRaisesRegex(SupervisorError,'slots'):
            self.manager.dispatch('reader3',self.instructions)
        self.assertEqual(self.herdr.prompt_ack.call_count,2)

    def test_collection_requires_logical_json_line_and_redacts_secrets(self):
        result=self.dispatch();job=self.manager._job(result['job_id'])
        output=json.loads(self.output(job));output['summary']='password=should-not-leak'
        collected=parse_job_result(json.dumps(output),job)
        self.assertNotIn('should-not-leak',collected['summary'])
        with self.assertRaises(SupervisorError):parse_job_result(json.dumps(output,indent=2),job)
        self.herdr.read_agent.return_value=self.output(job)
        self.manager.collect(job['job_id'])
        self.herdr.read_agent.assert_called_once_with('w1:p2',source='recent-unwrapped',lines=80)

    def test_cross_provider_identity_conflict_refuses_dispatch(self):
        self.register()
        conflicting=self.agent('w1:p3',CHILD);conflicting['agent']='claude';conflicting['agent_session']['agent']='claude'
        self.agents.append(conflicting)
        with self.assertRaises(SupervisorError):self.manager.dispatch('reader',self.instructions)
        self.herdr.prompt_ack.assert_not_called()

    def test_recorded_cli_contract_has_required_readonly_launch_flags(self):
        root=Path(__file__).parent/'fixtures/native'
        help_text=(root/'codex-0.160.1-help.txt').read_text()
        for token in ('--sandbox','read-only','--ask-for-approval','--model'):
            self.assertIn(token,help_text)
        self.assertIn('--pane',(root/'herdr-0.9.0-process-info-help.txt').read_text())

    def test_pane_local_no_daemon_flag_is_supported(self):
        info=self.process('w1:p2');info['foreground_processes'][0]['argv'].insert(1,'--no-daemon')
        self.assertTrue(readonly_process(info,'gpt-6.1-sol',str(self.root)))
        info['foreground_processes'][0]['argv'].append('--no-daemon')
        self.assertFalse(readonly_process(info,'gpt-6.1-sol',str(self.root)))
