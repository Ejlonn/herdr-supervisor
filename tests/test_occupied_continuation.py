"""Real adapter predicate semantics through actual startup continuation; offline only."""
import unittest
from types import SimpleNamespace

from herdr_cli import HerdrCli
from herdr_core import HerdrError
from test_startup_wait import ExplicitWaitTests


class OccupiedContinuation(ExplicitWaitTests):
    def occupied(self):
        wait=self.open_explicit_wait()
        adapter=SimpleNamespace(_run=lambda args, **kw: {'result':{'pane':{'id':'w9:p1','agent':'codex'}}})
        self.assertFalse(HerdrCli.pane_available(adapter,'w9:p1'))
        self.herdr.pane_available=lambda pane: HerdrCli.pane_available(adapter,pane)
        return wait

    def test_occupied_exact_provider_continues_once(self):
        wait=self.occupied();self.herdr.unblock('w9:p1')
        result=self.continue_(wait['wait_id'])
        self.assertTrue(result['ok'],result)
        self.assertEqual(len(self.herdr.pane_runs),1)
        self.assertEqual(len(self.herdr.agent_renames),1)
        again=self.continue_(wait['wait_id'])
        self.assertFalse(again['ok'],again)
        self.assertEqual(len(self.herdr.pane_runs),1)
        self.assertEqual(len(self.herdr.agent_renames),1)

    def test_missing_pane_process_refuses_without_effects(self):
        wait=self.occupied();self.herdr.unblock('w9:p1')
        def missing(pane): raise HerdrError('pane missing',code='pane_not_found')
        self.herdr.process_info=missing
        self.assert_refused(wait)

    def test_ambiguous_process_refuses_without_effects(self):
        wait=self.occupied();self.herdr.unblock('w9:p1')
        original=self.herdr.process_info
        self.herdr.process_info=lambda pane:{**original(pane),'foreground_processes':[]}
        self.assert_refused(wait)

    def test_replacement_process_refuses_without_effects(self):
        wait=self.occupied();self.herdr.unblock('w9:p1')
        original=self.herdr.process_info
        def replaced(pane):
            info=original(pane)
            info['foreground_processes'][0]['pid']+=1
            return info
        self.herdr.process_info=replaced
        self.assert_refused(wait)

    def assert_refused(self,wait):
        before=(len(self.herdr.pane_runs),len(self.herdr.agent_renames),len(self.herdr.bootstrap_prompts))
        result=self.continue_(wait['wait_id'])
        self.assertFalse(result['ok'],result)
        self.assertEqual(result['kind'],'changed_process',result)
        self.assertEqual(before,(len(self.herdr.pane_runs),len(self.herdr.agent_renames),len(self.herdr.bootstrap_prompts)))
        self.assertEqual(self.sup.owners(),self.owners)

if __name__=='__main__':
    suite=unittest.TestSuite(OccupiedContinuation(n) for n in OccupiedContinuation.__dict__ if n.startswith('test_'))
    result=unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(not result.wasSuccessful())
