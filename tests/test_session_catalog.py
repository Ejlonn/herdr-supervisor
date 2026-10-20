from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from herdr_core import SupervisorError
from herdr_session_catalog import native_meta, native_snapshot, read_native_file, refresh_native, session_catalog

PARENT = '11111111-1111-4111-8111-111111111111'
CHILD = '22222222-2222-4222-8222-222222222222'
FIXTURE = Path(__file__).parent / 'fixtures/native/codex-0.154.0-child.jsonl'

class CatalogTests(unittest.TestCase):
    def agent(self, pane='w1:p1', identity=PARENT, provider='codex', status='idle'):
        return {'agent':provider,'pane_id':pane,'agent_status':status,'agent_session':{'agent':provider,'kind':'id','value':identity}}

    def test_classifications_and_no_model_guess(self):
        a=self.agent(); owners={'codex':{'session_id':PARENT,'pane_id':'w1:p1'}}
        row=session_catalog([a],owners,{}, {})[0]
        self.assertEqual(row['role'],'workflow_owner'); self.assertTrue(row['usable']); self.assertIsNone(row['observed_model'])
        for agents, reason in (([a,self.agent('w1:p2')],'duplicate'),([self.agent(identity=None)],'unavailable'),([self.agent('w1:p2')],'moved'),([self.agent(status='working')],'not ready')):
            rows=session_catalog(agents,owners,{}, {})
            self.assertTrue(all(not r['usable'] for r in rows));self.assertIn(reason, rows[0]['reason'])

    def test_native_fixture_and_no_content_export(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); p=root/'child.jsonl';p.write_bytes(FIXTURE.read_bytes())
            first=read_native_file(p,[root],PARENT,{})
            self.assertEqual(first['summary']['session_id'],CHILD)
            self.assertEqual(first['summary']['model'],'gpt-6.1-sol')
            self.assertIsNone(first['summary']['pane_target']);self.assertIsNone(first['summary']['usage'])
            serialized=json.dumps(first)
            self.assertNotIn('SECRET_MESSAGE',serialized);self.assertNotIn('private',serialized)
            self.assertEqual(read_native_file(p,[root],PARENT,first), first)

    def test_partial_lines_rotation_and_truncation(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);p=root/'child.jsonl';p.write_bytes(FIXTURE.read_bytes())
            first=read_native_file(p,[root],PARENT,{})
            with p.open('ab') as f:f.write(b'{"type":"turn_context","payload":{"model":"other"}}')
            second=read_native_file(p,[root],PARENT,first)
            self.assertEqual(first['offset'],second['offset'])
            with p.open('ab') as f:f.write(b'\n')
            third=read_native_file(p,[root],PARENT,second)
            self.assertEqual(third['summary']['model'],'other')
            p.write_bytes(FIXTURE.read_bytes())
            self.assertEqual(read_native_file(p,[root],PARENT,third)['summary']['model'],'gpt-6.1-sol')

    def test_unsupported_relationship_and_version(self):
        meta=json.loads(FIXTURE.read_text().splitlines()[0])
        self.assertIsNone(native_meta(meta,CHILD))
        meta['payload']['cli_version']='9.9.9'; self.assertIsNone(native_meta(meta,PARENT))
        meta['payload']['cli_version']='0.154.0';meta['payload']['source']={'subagent':{'other':'guardian'}}
        self.assertIsNone(native_meta(meta,PARENT))

    def test_native_symlinks_and_containment_refused(self):
        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as other:
            root=Path(d);outside=Path(other)/'child.jsonl';outside.write_bytes(FIXTURE.read_bytes())
            link=root/'link';link.symlink_to(outside)
            for p in (outside,link):
                with self.assertRaises(SupervisorError):read_native_file(p,[root],PARENT,{})

    def test_refresh_explicit_parent_optin_dedup_and_private_errors(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);p=root/'child.jsonl';p.write_bytes(FIXTURE.read_bytes())
            config={'native_tracking':{'enabled':True,'roots':[d],'files':[{'path':str(p),'parent_session_id':PARENT}]*2}}
            report=refresh_native(root,config,{PARENT})
            self.assertEqual(len(report['children']),1);self.assertEqual(native_snapshot(root),report)
            report=refresh_native(root,config,set())
            self.assertEqual(report['children'],[]);self.assertNotIn(d,json.dumps(report))
            self.assertFalse(refresh_native(root,{},set())['enabled'])

    def test_size_limit_no_content_in_errors(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'child.jsonl';p.write_bytes(FIXTURE.read_bytes()+b'X'*70000+b'\n')
            result=read_native_file(p,[Path(d)],PARENT,{})
            self.assertEqual(result['summary']['session_id'],CHILD)
            self.assertNotIn('X'*100,json.dumps(result))
            p.write_bytes(b'X'*70000+b'\n')
            with self.assertRaisesRegex(SupervisorError,'metadata'):read_native_file(p,[Path(d)],PARENT,{})
