"""Actual preparation/alias/wait consumer over isolated fakes; never a provider call."""
import json
import unittest
import uuid

import herdr_core
from test_fresh_session_issue5 import CODEX_ONLY
from test_startup_wait import ExplicitWaitTests, update_menu_screen


class AliasSettlement(ExplicitWaitTests):
    def inject(self,shape='empty', *, occurrence=1,persistent=False,changed_pid=False):
        original=self.herdr.process_info
        seen=[]
        def read(pane):
            info=original(pane)
            prep=getattr(self,'prep',None)
            path=self.paths.session_preparations_dir/f'{prep}.json'
            if not path.exists() or self.journal(prep)['providers']['codex'].get('status')!='PROCESS_BOUND':return info
            seen.append(len(info.get('foreground_processes',[])))
            if len(seen)==occurrence or (persistent and len(seen)>=occurrence):
                self.assertEqual(self.herdr.agent_renames,[],'no mutation while the snapshot is uncertain')
                if shape=='malformed':return {**info,'foreground_processes':None}
                if shape=='multi':return {**info,'foreground_processes':info['foreground_processes']*2}
                return {**info,'foreground_processes':[]}
            if changed_pid and len(seen)>occurrence:
                info=json.loads(json.dumps(info));info['foreground_processes'][0]['pid']+=100
            return info
        self.herdr.process_info=read
        return seen

    def no_effects_after_launch(self):
        self.assertEqual(len(self.herdr.pane_runs),1)
        self.assertEqual(self.herdr.agent_renames,[])
        self.assertEqual(len(self.herdr.bootstrap_prompts),0)
        self.assertEqual(self.sup.owners(),self.owners)

    def test_empty_snapshot_then_same_process_opens_update_wait_and_continues_once(self):
        self.inject()
        wait=self.open_explicit_wait(update_menu_screen())
        self.assertEqual((wait['phase'],wait['site']),('PROCESS_BOUND','launch_alias_unverified'))
        self.assertEqual(self.journal(self.prep)['providers']['codex']['status'],'PROCESS_BOUND')
        self.no_effects_after_launch()
        self.herdr.unblock('w9:p1')
        result=self.continue_(wait['wait_id']);self.assertTrue(result['ok'],result)
        self.assertEqual(len(self.herdr.pane_runs),1)
        self.assertEqual(len(self.herdr.agent_renames),1)

    def test_multiple_snapshot_then_same_process_opens_update_wait_without_ignoring_other_processes(self):
        self.inject('multi')
        wait=self.open_explicit_wait();self.assertEqual(wait['phase'],'PROCESS_BOUND')
        self.no_effects_after_launch()

    def test_ambiguity_at_the_final_check_before_rename_delays_that_mutation(self):
        self.prep=str(uuid.uuid4());self.inject(occurrence=2)
        result=self.prepare(CODEX_ONLY,self.prep)
        self.assertEqual(result['status'],'OWNERSHIP_BOUND')
        self.assertEqual((len(self.herdr.pane_runs),len(self.herdr.agent_renames)),(1,1))

    def test_persistent_empty_report_holds_without_creating_a_falsely_bound_wait(self):
        seen=self.inject(persistent=True)
        with self.assertRaisesRegex(herdr_core.SupervisorError,'remained ambiguous'):
            self.open_explicit_wait()
        self.assertEqual(len(seen),10)
        self.assertEqual(self.waits(),[]);self.no_effects_after_launch()

    def test_persistent_multiple_report_holds_without_picking_a_process(self):
        self.inject('multi',persistent=True)
        with self.assertRaisesRegex(herdr_core.SupervisorError,'remained ambiguous'):
            self.open_explicit_wait()
        self.assertEqual(self.waits(),[]);self.no_effects_after_launch()

    def test_new_pid_after_a_temporary_gap_is_never_adopted(self):
        self.inject(changed_pid=True)
        with self.assertRaisesRegex(herdr_core.SupervisorError,'process changed'):
            self.open_explicit_wait()
        self.assertEqual(self.waits(),[]);self.no_effects_after_launch()

    def test_malformed_report_is_not_classified_as_transient(self):
        seen=self.inject('malformed')
        with self.assertRaisesRegex(herdr_core.SupervisorError,'malformed foreground'):
            self.open_explicit_wait()
        self.assertEqual(len(seen),1);self.no_effects_after_launch()

    def test_temporary_gap_while_capturing_wait_is_observed_without_binding_another_process(self):
        self.inject(occurrence=11)  # ten alias observations, then the wait's own process capture
        wait=self.open_explicit_wait()
        self.assertEqual(wait['phase'],'PROCESS_BOUND')
        self.no_effects_after_launch()

    def test_process_replacement_during_wait_capture_does_not_open_a_wait_for_the_replacement(self):
        self.inject(occurrence=11,changed_pid=True)
        with self.assertRaisesRegex(herdr_core.SupervisorError,'process changed'):
            self.open_explicit_wait()
        self.assertEqual(self.waits(),[]);self.no_effects_after_launch()

    def test_permanent_ambiguity_during_wait_capture_never_invents_a_process(self):
        self.inject(occurrence=11,persistent=True)
        with self.assertRaisesRegex(herdr_core.SupervisorError,'could not be proven'):
            self.open_explicit_wait()
        self.assertEqual(self.waits(),[]);self.no_effects_after_launch()

    def test_deadline_expiry_during_wait_capture_keeps_the_wait_unopened(self):
        self.config['session_start']['startup_wait']={'not_after_unix':self.clock.time()+11}
        self.sup=self.make_supervisor();self.inject(occurrence=11,persistent=True)
        with self.assertRaises(herdr_core.SupervisorError):self.open_explicit_wait()
        self.assertEqual(self.journal(self.prep)['status'],'ABANDONED')
        self.assertEqual(self.waits(),[]);self.no_effects_after_launch()

    def test_deadline_expiry_during_observation_stops_without_renaming(self):
        self.config['session_start']['startup_wait']={'not_after_unix':self.clock.time()+2}
        self.sup=self.make_supervisor();self.inject(persistent=True)
        with self.assertRaises(herdr_core.SupervisorError):self.open_explicit_wait()
        self.assertEqual(self.journal(self.prep)['status'],'ABANDONED')
        self.no_effects_after_launch()

if __name__=='__main__':
    names=[n for n in AliasSettlement.__dict__ if n.startswith('test_')]
    result=unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite(AliasSettlement(n) for n in names))
    raise SystemExit(not result.wasSuccessful())
