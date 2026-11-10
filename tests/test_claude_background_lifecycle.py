"""Native pending jobs are not finished workflow turns; no real provider/process."""
import copy
import json
from unittest import mock

import herdr_native_results as nr
from test_native_results import PROMPT, RUN, TURN, NativeFlow, Roots, append_text, c_final, c_user, during_read, write_jsonl
from v2_fixtures import CLAUDE_SESSION, NOW, FakeHerdr, hs


def call(background=True):
    return c_final("", uuid="primary-bash-uuid", stop="tool_use", blocks=[{"type": "tool_use", "id": "bash_one", "name": "Bash",
                                                "input": {"command": "fixture", "run_in_background": background}}])


def result(task="job_one", ident="bash_one"):
    return c_user("", sourceToolAssistantUUID="primary-bash-uuid", toolUseResult={"backgroundTaskId": task}, message={"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": ident, "content": "background job started", "is_error": False}]})


def completion(task="job_one", ident="bash_one", **extra):
    return c_user(f"<task-notification><task-id>{task}</task-id><tool-use-id>{ident}</tool-use-id><status>completed</status></task-notification>",
                  origin={"kind": "task-notification", "producer": "session-task", "runId": "fixture-native-run"},
                  promptSource="system", turnOrigin="task_notification", **extra)


class JobReaderTests(Roots):
    def setUp(self):
        super().setUp()
        self.old = c_user("older conversation")
        write_jsonl(self.claude_file, [self.old])
        self.cursor = nr.capture_cursor("claude", CLAUDE_SESSION, self.roots["claude"])
        self.rows = [self.old, c_user(PROMPT), call(), result(), c_final("Waiting for the suite")]

    def check(self, rows=None, **kw):
        write_jsonl(self.claude_file, self.rows if rows is None else rows)
        return nr.claude_background_status(CLAUDE_SESSION, RUN, TURN, root=self.roots["claude"], cursor=self.cursor, **kw)

    def test_pending_then_complete_then_primary_continuation(self):
        self.assertEqual(self.check().kind, "pending")
        self.rows.append(completion())
        self.assertEqual(self.check().kind, "awaiting_final")
        self.rows.append(c_final("finished without a workflow block"))
        self.assertEqual(self.check().kind, "clear")  # genuine missing-result policy can apply now
        self.assertEqual(self.check(self.rows+[completion()]).kind,"clear")  # duplicate terminal is idempotent

    def test_automatic_backgrounding_without_request_flag(self):
        self.rows[2] = call(False)
        self.assertEqual(self.check().kind, "pending")

    def test_ordinary_user_xml_and_child_notifications_cannot_clear(self):
        forged = completion()
        forged.pop("origin")
        forged.pop("promptSource")
        forged.pop("turnOrigin")
        self.assertEqual(self.check(self.rows + [forged]).kind, "pending")
        self.assertEqual(self.check(self.rows + [completion(isSidechain=True)]).kind, "pending")

    def test_wrong_task_tool_identity_or_system_provenance_is_unknown(self):
        cases = [completion(task="different"), completion(ident="different"), completion(sessionId="33333333-3333-4333-8333-333333333333")]
        for field, value in (("promptSource", "user"), ("turnOrigin", "user"), ("origin", {"kind": "task-notification", "producer": "other", "runId": "x"})):
            row = completion();row[field] = value;cases.append(row)
        for row in cases:
            with self.subTest(row=row):self.assertEqual(self.check(self.rows + [row]).kind, "unknown")

    def test_tool_result_and_notification_require_native_source_bindings(self):
        rows=copy.deepcopy(self.rows);rows[3]['sourceToolAssistantUUID']='unrelated'
        self.assertEqual(self.check(rows).kind,'unknown')
        row=completion();row['type']='assistant';row['message']['role']='assistant'
        self.assertEqual(self.check(self.rows+[row]).kind,'unknown')
        row=completion();row['message']['role']='assistant'
        self.assertEqual(self.check(self.rows+[row]).kind,'unknown')
        row=completion();row['message']['content']=[{'type':'text','text':row['message']['content']},{'type':'tool_result','content':'extra'}]
        self.assertEqual(self.check(self.rows+[row]).kind,'unknown')

    def test_duplicate_binding_and_bad_primitive_flags_are_unknown(self):
        for extra in (result(), call(), result(ident=[])):
            self.assertEqual(self.check(self.rows + [extra]).kind, "unknown")
        for value in (1, "false", None):
            rows = copy.deepcopy(self.rows);rows[2]["message"]["content"][0]["input"]["run_in_background"] = value
            self.assertEqual(self.check(rows).kind, "unknown")

    def test_requested_job_with_no_result_stays_pending(self):
        self.assertEqual(self.check(self.rows[:3] + [c_final("waiting")]).kind, "pending")

    def test_unrelated_old_notification_never_adopts_a_job(self):
        self.assertEqual(self.check(self.rows + [completion(task="old", ident="old")]).kind, "pending")

    def test_malformed_xml_dtd_duplicates_and_terminal_status_are_unknown(self):
        for text in ('<broken>', '<!DOCTYPE x><task-notification/>',
                     '<task-notification><task-id>job_one</task-id><task-id>job_one</task-id><tool-use-id>bash_one</tool-use-id><status>completed</status></task-notification>',
                     '<task-notification><task-id>job_one</task-id><tool-use-id>bash_one</tool-use-id><status>unknown</status></task-notification>'):
            row = completion();row["message"]["content"] = text
            self.assertEqual(self.check(self.rows + [row]).kind, "unknown")

    def test_missing_prompt_partial_record_and_budgets_never_clear(self):
        self.assertEqual(self.check([self.old, c_user("other turn"), call(), result()]).kind, "unknown")
        write_jsonl(self.claude_file, self.rows)
        with self.claude_file.open("ab") as f:f.write(b"{")
        self.assertEqual(nr.claude_background_status(CLAUDE_SESSION,RUN,TURN,root=self.roots['claude'],cursor=self.cursor).kind,"unknown")
        for key, value in (("MAX_RECORDS", 2), ("MAX_LINE_BYTES", 10), ("MAX_SCAN_BYTES", 10)):
            with mock.patch.object(nr,key,value):self.assertEqual(self.check().kind,"unknown")
        ticks=[0]
        def clock():ticks[0]+=1;return ticks[0]
        self.assertEqual(self.check(clock=clock).kind,"unknown")

    def test_growth_is_a_retry_not_clear_or_missing_result(self):
        write_jsonl(self.claude_file,self.rows)
        with during_read(self.claude_file,append_text(self.claude_file,json.dumps(completion())+'\n')):
            verdict=nr.claude_background_status(CLAUDE_SESSION,RUN,TURN,root=self.roots['claude'],cursor=self.cursor)
        self.assertEqual(verdict.kind,"retry")


class LifecycleFlowTests(NativeFlow):
    def test_pending_job_waits_without_model_wakeup_then_native_final_routes(self):
        stage={"start":None,"notification":False,"final":False}
        def writer(fake,name,prompt):
            with self.claude_path.open('a') as f:
                for row in (c_user(prompt),call(),result(),c_final("Waiting for tests")):f.write(json.dumps(row)+'\n')
            stage['start']=self.clock.time()
        original=self.sup.sleeper
        def sleep(seconds):
            original(seconds)
            if stage['start'] is None:return
            self.assertIsNone(self.state().get('missing_result'))
            elapsed=self.clock.time()-stage['start']
            rows=[]
            if elapsed>=40 and not stage['notification']:rows.append(completion());stage['notification']=True
            if elapsed>=65 and not stage['final']:
                run,turn=FakeHerdr.ids(self.claude_prompts()[0][1])
                rows.append(c_final(FakeHerdr.block_v2(run,turn,'implement','human','generic_question',str(self.question_payload()))));stage['final']=True
            with self.claude_path.open('a') as f:
                for row in rows:f.write(json.dumps(row)+'\n')
        self.sup.sleeper=sleep
        self.claude_flow({'output':'Waiting for tests','before':writer})
        self.assertTrue(stage['final']);self.assertEqual(self.pending()['gate_type'],'generic_question')
        self.assert_nothing_sent_beyond_the_turn()
        kinds={v.get('kind') for v in self.events_named('claude_background_wait')}
        self.assertTrue({'pending','awaiting_final'}<=kinds)

    def empty_quota(self):
        self.sup.quota_path('claude').write_text(json.dumps({'provider':'claude','fetched_at_unix':NOW,'windows':[],
            'session_quota_only':True,'session_windows':{CLAUDE_SESSION:[]}}))

    def test_working_startup_unknown_is_not_an_unsafe_quota_pause(self):
        self.empty_quota()
        def done(fake,name):
            if name=='claude-main':fake.agents[name]['agent_status']='idle'
        self.herdr.on_wait=done
        self.claude_flow({'status':'working','output':'no terminal frame','before':self.native_writer(path=self.claude_path)})
        self.assertEqual(self.pending()['gate_type'],'generic_question');self.assert_nothing_sent_beyond_the_turn()
        self.assertFalse(self.state()['delivery']['claude_startup_unknown']['usable_seen'])

    def test_unknown_allowance_is_revoked_after_usable_and_recovery_is_strict(self):
        state=self.sup.initialize('fixture','codex',workflow_policy='gated_v2')
        delivery={'turn_id':TURN,'agent':'claude','kind':'handoff','status':'accepted',
                  'claude_startup_unknown':{'session_id':CLAUDE_SESSION,'turn_id':TURN,'usable_seen':False}}
        state['delivery']=delivery;state['active_agent']='claude'
        self.empty_quota();self.assertEqual(self.sup._working_blocking_windows(state,'claude',delivery),())
        self.write_quota('claude',50,NOW+3600,50,NOW+86400)
        self.sup._working_blocking_windows(state,'claude',delivery);self.assertTrue(delivery['claude_startup_unknown']['usable_seen'])
        self.empty_quota()
        with self.assertRaises(hs.QuotaError):self.sup._working_blocking_windows(state,'claude',delivery)
        delivery['claude_startup_unknown']['usable_seen']=False
        for key in ('quota_wait','interrupted_delivery','deferred_anomaly'):
            state[key]={'provider':'claude'}
            with self.assertRaises(hs.QuotaError):self.sup._working_blocking_windows(state,'claude',delivery)
            state[key]=None
        delivery['claude_startup_unknown']['usable_seen']='false'
        with self.assertRaises(hs.QuotaError):self.sup._working_blocking_windows(state,'claude',delivery)

    def test_pending_job_wait_preserves_pause_and_never_creates_a_missing_result(self):
        def writer(fake,name,prompt):
            with self.claude_path.open('a') as f:
                for row in (c_user(prompt),call(),result(),c_final('waiting')):f.write(json.dumps(row)+'\n')
        original=self.sup.sleeper
        def pause(seconds):
            original(seconds)
            if len(self.claude_prompts())==1 and self.events_named('claude_background_wait'):
                self.sup.store.write_control('paused')
        self.sup.sleeper=pause
        self.claude_flow({'output':'waiting','before':writer})
        self.assertEqual(self.state()['supervisor_state'],'PAUSED')
        self.assertIsNone(self.state().get('missing_result'));self.assert_nothing_sent_beyond_the_turn()

    def test_pending_job_wait_preserves_cancel_and_never_creates_a_missing_result(self):
        def writer(fake,name,prompt):
            with self.claude_path.open('a') as f:
                for row in (c_user(prompt),call(),result(),c_final('waiting')):f.write(json.dumps(row)+'\n')
        original=self.sup.sleeper
        def pause(seconds):
            original(seconds)
            if len(self.claude_prompts())==1 and self.events_named('claude_background_wait'):
                self.sup.store.write_control('cancelled')
        self.sup.sleeper=pause
        self.claude_flow({'output':'waiting','before':writer})
        self.assertEqual(self.state()['supervisor_state'],'CANCELLED')
        self.assertIsNone(self.state().get('missing_result'));self.assert_nothing_sent_beyond_the_turn()

    def test_started_unknown_marker_never_covers_unscoped_or_malformed_data(self):
        state=self.sup.initialize('fixture','codex',workflow_policy='gated_v2')
        delivery={'turn_id':TURN,'agent':'claude','kind':'handoff','status':'accepted',
                  'claude_startup_unknown':{'session_id':CLAUDE_SESSION,'turn_id':TURN,'usable_seen':False}}
        state['delivery']=delivery
        for change in ({'provider':'codex'}, {'session_windows':{}}, {'fetched_at_unix':None}, {'fetched_at_unix':NOW+1000}):
            self.empty_quota();path=self.sup.quota_path('claude');value=json.loads(path.read_text());value.update(change);path.write_text(json.dumps(value))
            with self.subTest(change=change),self.assertRaises(hs.QuotaError):self.sup._working_blocking_windows(state,'claude',delivery)
        self.empty_quota();delivery['claude_startup_unknown']['session_id']='different'
        with self.assertRaises(hs.QuotaError):self.sup._working_blocking_windows(state,'claude',delivery)

    def test_valid_blocking_windows_still_win_over_the_startup_marker(self):
        state=self.sup.initialize('fixture','codex',workflow_policy='gated_v2')
        delivery={'turn_id':TURN,'agent':'claude','kind':'handoff','status':'accepted',
                  'claude_startup_unknown':{'session_id':CLAUDE_SESSION,'turn_id':TURN,'usable_seen':False}}
        state['delivery']=delivery
        self.write_quota('claude',0,NOW+3600,50,NOW+86400)
        self.assertTrue(self.sup._working_blocking_windows(state,'claude',delivery))
        self.assertTrue(delivery['claude_startup_unknown']['usable_seen'])

    def test_pending_retry_preserves_attempt_budget_and_sends_nothing(self):
        def writer(fake,name,prompt):
            with self.claude_path.open('a') as f:
                for row in (c_user(prompt),call(),result(),c_final('waiting')):f.write(json.dumps(row)+'\n')
        original=self.sup.sleeper
        def pause(seconds):
            original(seconds)
            if self.events_named('claude_background_wait'):self.sup.store.write_control('paused')
        self.sup.sleeper=pause
        self.claude_flow({'output':'waiting','before':writer})
        self.sup.store.write_control('running')
        state=self.state();state.update(supervisor_state='WAIT_USER',wait_user_requires_action=True)
        delivery=state['delivery']
        state['missing_result']={'schema_version':1,'run_id':state['run_id'],'turn_id':delivery['turn_id'],'agent':'claude','attempts':0,'created_at_unix':self.clock.time()}
        self.sup.store.write_state(state)
        with self.assertRaisesRegex(hs.SupervisorError,'retry was not consumed'):
            self.sup.retry_routing_result(state,run_id=state['run_id'],turn_id=delivery['turn_id'],actor='fixture')
        self.assertEqual(state['missing_result']['attempts'],0)
        self.assert_nothing_sent_beyond_the_turn()
