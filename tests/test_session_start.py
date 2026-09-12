"""Fixture-only session/model selection, preparation, recovery, and rebind tests."""

from __future__ import annotations

import hashlib
import json
import subprocess
import uuid
from pathlib import Path

import herdr_cli
import herdr_core
import herdr_sessions
import herdr_supervisor as hs
from v2_fixtures import CLAUDE_SESSION, CODEX_SESSION, V2Case


class SessionSelectionTests(V2Case):
    def preparation_record(self, prep: str, selection: dict, *, status: str, provider_status: str,
                           pane: str | None = None, session: str | None = None) -> dict:
        item={"status":provider_status,"old_pane_id":"w3:p2","old_session_id":CODEX_SESSION,
              "profile":"default","preparation_id":prep}
        if provider_status != "AUTHORIZED":
            # Fresh Codex journals carry the pre-created thread (id persisted before any pane exists); the
            # identity Herdr must later observe is exactly that thread.
            item.update({"thread_request_id":f"thread-{prep}-codex","thread_payload":{"cwd":str(self.project_root),"model":None,"ephemeral":False}})
            if provider_status != "THREAD_CREATING":
                item.update({"thread_id":session or "44444444-4444-4444-8444-444444444444","thread_model":"gpt-5-codex","thread_provider":"openai"})
        if pane is not None: item["pane_id"]=pane
        if provider_status in ("AGENT_STARTING","IDENTITY_VERIFIED"): item["agent_name"]=f"codex-run-{prep[:8]}"
        if session is not None: item["session_id"]=session
        record={"schema_version":2,"preparation_id":prep,"status":status,"selection":selection,
                "created_at":"1970-01-12T13:46:40Z","origin":"supervisor","request_binding":None,
                "owners_before":json.loads(json.dumps(self.owners)),"providers":{"codex":item}}
        return record

    def test_preserve_is_default_and_has_no_side_effect(self) -> None:
        record=self.sup.prepare_task_sessions(None,preparation_id=str(uuid.uuid4()))
        self.assertEqual(record["selection"]["policy"],"preserve")
        self.assertEqual(self.herdr.splits,[]); self.assertEqual(self.herdr.starts,[])
        self.assertEqual(hs.load_json(self.paths.owners_file,label="owners"),self.owners)

    def test_selection_rejects_profile_for_preserved_provider(self) -> None:
        self.config["session_start"]["model_profiles"]["codex"]["large"]={"label":"Large","model":"gpt-approved"}
        with self.assertRaisesRegex(hs.SupervisorError,"preserved codex"):
            herdr_sessions.validate_selection(self.config,{"policy":"preserve","profiles":{"codex":"large","claude":"default"}})

    def test_fresh_codex_creates_new_pane_and_atomically_binds(self) -> None:
        prep=str(uuid.uuid4())
        seen=[]
        original=self.herdr.split_pane
        def split(*args,**kwargs):
            journal=hs.load_json(self.paths.session_preparations_dir/f"{prep}.json",label="prep")
            seen.append(journal["providers"]["codex"]["status"])
            return original(*args,**kwargs)
        self.herdr.split_pane=split
        record=self.sup.prepare_task_sessions({"policy":"fresh-codex","profiles":{"codex":"default","claude":"default"}},preparation_id=prep)
        owners=hs.load_json(self.paths.owners_file,label="owners")
        self.assertEqual(seen,["PANE_CREATING"])
        self.assertEqual(record["status"],"OWNERSHIP_BOUND")
        self.assertNotEqual(owners["codex"]["session_id"],CODEX_SESSION)
        self.assertEqual(owners["claude"]["session_id"],CLAUDE_SESSION)
        self.assertIn("codex-main",self.herdr.agents); self.assertIn("claude-main",self.herdr.agents)

    def test_fresh_run_binds_before_exactly_one_initial_prompt_and_preserves_old_agent(self) -> None:
        observed=[]
        def before_prompt(fake,name,text):
            owners=hs.load_json(self.paths.owners_file,label="owners at prompt")
            observed.append((owners["codex"]["session_id"],herdr_cli.session_identity(fake.agents[name])))
        self.herdr.responses=[{"v2":("plan","done"),"before":before_prompt}]
        selection={"policy":"fresh-codex","profiles":{"codex":"default","claude":"default"}}
        self.assertEqual(self.sup.run_new("fresh task","codex",session_selection=selection),0)
        state=self.state(); fresh=state["native_sessions"]["codex"]
        self.assertNotEqual(fresh,CODEX_SESSION)
        self.assertEqual(state["native_sessions"]["claude"],CLAUDE_SESSION)
        self.assertEqual(state["session_preparation"]["status"],"TASK_STARTED")
        self.assertEqual(len(self.herdr.prompts),1)
        self.assertEqual(observed,[(fresh,fresh)])
        self.assertTrue(self.herdr.prompts[0][0].startswith("codex-run-"))
        self.assertEqual(herdr_cli.session_identity(self.herdr.agents["codex-main"]),CODEX_SESSION)

    def test_nondefault_profile_uses_only_fixed_model_argv(self) -> None:
        self.config["session_start"]["model_profiles"]["codex"]["large"]={"label":"Large","model":"gpt-approved"}
        self.config["session_start"]["model_profiles"]["claude"]["opus"]={"label":"Opus","model":"claude-opus"}
        self.sup.model_flag_supported=lambda provider: provider=="claude"
        self.sup.prepare_task_sessions({"policy":"fresh-all","profiles":{"codex":"large","claude":"opus"}},preparation_id=str(uuid.uuid4()))
        starts={kind:args for _,kind,_,args in self.herdr.starts}
        # Codex: the configured model is set on the pre-created thread; the launch only resumes that exact thread.
        self.assertEqual(self.thread_journal.requests[0][1],{"cwd":str(self.project_root),"model":"gpt-approved","ephemeral":False})
        self.assertEqual(starts["codex"],["resume",self.thread_journal.requests[0][0] and self.herdr.starts[0][3][1]])
        self.assertEqual(starts["codex"][1],self.thread_journal.results[self.thread_journal.requests[0][0]]["thread"]["thread_id"])
        # Claude: unchanged provider-specific launch with the fixed --model argv only.
        self.assertEqual(starts["claude"],["--model","claude-opus"])

    def test_fresh_claude_and_fresh_all_success_preserve_every_old_agent(self) -> None:
        for policy, expected in (("fresh-claude", {"claude"}), ("fresh-all", {"codex","claude"})):
            with self.subTest(policy=policy):
                self.reset_fixture()
                old_agents=dict(self.herdr.agents)
                result=self.sup.prepare_task_sessions(
                    {"policy":policy,"profiles":{"codex":"default","claude":"default"}},
                    preparation_id=str(uuid.uuid4()),
                )
                self.assertEqual(result["status"],"OWNERSHIP_BOUND")
                self.assertEqual({entry[1] for entry in self.herdr.starts},expected)
                for name,record in old_agents.items():
                    self.assertEqual(self.herdr.agents[name],record)

    def test_reported_model_mismatch_never_changes_owners(self) -> None:
        self.config["session_start"]["model_profiles"]["codex"]["large"]={"label":"Large","model":"gpt-approved"}
        self.sup.model_flag_supported=lambda provider: True
        original=self.herdr.start_agent
        def start(name,*,kind,pane_id,args):
            original(name,kind=kind,pane_id=pane_id,args=args); self.herdr.agents[name]["model"]="other"
        self.herdr.start_agent=start
        with self.assertRaisesRegex(hs.SupervisorError,"different model"):
            self.sup.prepare_task_sessions({"policy":"fresh-codex","profiles":{"codex":"large","claude":"default"}},preparation_id=str(uuid.uuid4()))
        self.assertEqual(self.sup.owners(),self.owners)

    def test_interrupted_split_is_never_repeated(self) -> None:
        prep=str(uuid.uuid4()); path=self.paths.session_preparations_dir/f"{prep}.json"
        selection=herdr_sessions.validate_selection(self.config,{"policy":"fresh-codex","profiles":{"codex":"default","claude":"default"}})
        hs.atomic_write_json(path,self.preparation_record(prep,selection,status="SESSION_SELECTION_AUTHORIZED",provider_status="PANE_CREATING"))
        with self.assertRaisesRegex(hs.SupervisorError,"nothing was repeated"):
            self.sup.prepare_task_sessions(selection,preparation_id=prep)
        self.assertEqual(self.herdr.splits,[]); self.assertEqual(self.herdr.starts,[])
        self.assertEqual(self.sup.status()["session_preparation"]["preparation_id"],prep)
        abandoned=self.sup.abandon_session_preparation(prep)
        self.assertTrue(abandoned["abandoned"])
        self.assertEqual(hs.load_json(path,label="prep")["status"],"ABANDONED")

    def test_restart_before_split_runs_the_recorded_operation_once(self) -> None:
        prep=str(uuid.uuid4()); path=self.paths.session_preparations_dir/f"{prep}.json"
        selection=herdr_sessions.validate_selection(self.config,{"policy":"fresh-codex","profiles":{"codex":"default","claude":"default"}})
        hs.atomic_write_json(path,self.preparation_record(prep,selection,status="SESSION_SELECTION_AUTHORIZED",provider_status="AUTHORIZED"))
        result=self.sup.prepare_task_sessions(selection,preparation_id=prep)
        self.assertEqual(result["status"],"OWNERSHIP_BOUND"); self.assertEqual(len(self.herdr.splits),1); self.assertEqual(len(self.herdr.starts),1)

    def test_unresolved_other_preparation_blocks_new_start(self) -> None:
        first=str(uuid.uuid4()); second=str(uuid.uuid4())
        selection=herdr_sessions.validate_selection(self.config,{"policy":"fresh-codex","profiles":{"codex":"default","claude":"default"}})
        hs.atomic_write_json(self.paths.session_preparations_dir/f"{first}.json",self.preparation_record(first,selection,status="SESSION_SELECTION_AUTHORIZED",provider_status="PANE_CREATING"))
        with self.assertRaisesRegex(hs.SupervisorError,"unresolved"):
            self.sup.prepare_task_sessions(selection,preparation_id=second)
        self.assertEqual(self.herdr.splits,[])

    def test_partial_fresh_all_failure_does_not_change_owners(self) -> None:
        original=self.herdr.start_agent
        def fail(name,*,kind,pane_id,args):
            if kind=="claude": raise hs.HerdrError("timeout",code="timeout")
            return original(name,kind=kind,pane_id=pane_id,args=args)
        self.herdr.start_agent=fail
        with self.assertRaises(hs.HerdrError):
            self.sup.prepare_task_sessions({"policy":"fresh-all","profiles":{"codex":"default","claude":"default"}},preparation_id=str(uuid.uuid4()))
        self.assertEqual(hs.load_json(self.paths.owners_file,label="owners"),self.owners)

    def test_unsupported_pane_split_fails_before_side_effect(self) -> None:
        self.herdr.capability_report=lambda:{"optional":{}}
        with self.assertRaisesRegex(hs.SupervisorError,"contract was not verified"):
            self.sup.prepare_task_sessions({"policy":"fresh-codex","profiles":{"codex":"default","claude":"default"}},preparation_id=str(uuid.uuid4()))
        self.assertEqual(self.herdr.splits,[])

    def test_nonterminal_task_blocks_preparation_before_any_side_effect(self) -> None:
        self.sup.initialize("active task","codex")
        with self.assertRaisesRegex(hs.SupervisorError,"terminal task boundary"):
            self.sup.prepare_task_sessions(
                {"policy":"fresh-codex","profiles":{"codex":"default","claude":"default"}},
                preparation_id=str(uuid.uuid4()),
            )
        self.assertEqual(self.herdr.splits,[]); self.assertEqual(self.herdr.starts,[])

    def test_terminal_run_history_does_not_block_next_preserved_start(self) -> None:
        state=self.sup.initialize("finished task","codex")
        state["supervisor_state"]="DONE"
        state["delivery"]={"status":"completed","turn_id":str(uuid.uuid4())}
        self.sup.store.write_state(state)
        result=self.sup.prepare_task_sessions(None,preparation_id=str(uuid.uuid4()))
        self.assertEqual(result["status"],"OWNERSHIP_BOUND")
        self.assertEqual(self.herdr.splits,[]); self.assertEqual(self.herdr.starts,[])

    def test_existing_fresh_agent_name_blocks_before_pane_creation(self) -> None:
        prep=str(uuid.uuid4())
        name=f"codex-run-{prep[:8]}"
        self.herdr.agents[name]=self.herdr.agent("codex",name,"w8:p1","66666666-6666-4666-8666-666666666666")
        with self.assertRaisesRegex(hs.SupervisorError,"agent name already exists"):
            self.sup.prepare_task_sessions(
                {"policy":"fresh-codex","profiles":{"codex":"default","claude":"default"}},
                preparation_id=prep,
            )
        self.assertEqual(self.herdr.splits,[]); self.assertEqual(self.sup.owners(),self.owners)

    def test_nonempty_new_pane_never_starts_agent_or_changes_owners(self) -> None:
        self.herdr.pane_available=lambda pane: False
        with self.assertRaisesRegex(hs.SupervisorError,"not an empty shell pane"):
            self.sup.prepare_task_sessions(
                {"policy":"fresh-codex","profiles":{"codex":"default","claude":"default"}},
                preparation_id=str(uuid.uuid4()),
            )
        self.assertEqual(self.herdr.starts,[]); self.assertEqual(self.sup.owners(),self.owners)

    def test_returned_pane_is_durable_before_availability_failure(self) -> None:
        prep=str(uuid.uuid4()); self.herdr.pane_available=lambda pane: False
        with self.assertRaisesRegex(hs.SupervisorError,"not an empty shell"):
            self.sup.prepare_task_sessions(
                {"policy":"fresh-codex","profiles":{"codex":"default","claude":"default"}},preparation_id=prep)
        record=hs.load_json(self.paths.session_preparations_dir/f"{prep}.json",label="prep")
        self.assertEqual(record["providers"]["codex"]["status"],"FRESH_PANE_CREATED")
        self.assertEqual(record["providers"]["codex"]["pane_id"],"w9:p1")
        with self.assertRaisesRegex(hs.SupervisorError,"not an empty shell"):
            self.sup.prepare_task_sessions(record["selection"],preparation_id=prep)
        self.assertEqual(len(self.herdr.splits),1)

    def test_every_capability_is_preflighted_before_any_split(self) -> None:
        cases=(
            {"compatible":False,"required":{"agent start (--kind/--pane)":True},"optional":{"task-start pane split (--direction/--ratio/--cwd/--no-focus)":True}},
            {"compatible":True,"required":{"agent start (--kind/--pane)":False},"optional":{"task-start pane split (--direction/--ratio/--cwd/--no-focus)":True}},
        )
        for report in cases:
            with self.subTest(report=report):
                self.reset_fixture(); self.herdr.capability_report=lambda report=report:report
                with self.assertRaisesRegex(hs.SupervisorError,"contract was not verified"):
                    self.sup.prepare_task_sessions({"policy":"fresh-codex","profiles":{"codex":"default","claude":"default"}},preparation_id=str(uuid.uuid4()))
                self.assertEqual(self.herdr.splits,[]); self.assertEqual(self.herdr.starts,[])

    def test_all_model_profiles_are_preflighted_before_any_split(self) -> None:
        self.config["session_start"]["model_profiles"]["claude"]["large"]={"label":"Large","model":"claude-approved"}
        self.sup.model_flag_supported=lambda provider: provider=="codex"
        with self.assertRaisesRegex(hs.SupervisorError,"claude CLI"):
            self.sup.prepare_task_sessions({"policy":"fresh-all","profiles":{"codex":"default","claude":"large"}},preparation_id=str(uuid.uuid4()))
        self.assertEqual(self.herdr.splits,[]); self.assertEqual(self.herdr.starts,[])

    def test_preserved_owner_drift_fails_before_binding(self) -> None:
        original=self.herdr.start_agent
        def drift(name,*,kind,pane_id,args):
            original(name,kind=kind,pane_id=pane_id,args=args)
            changed=json.loads(json.dumps(self.owners)); changed["claude"]={"pane_id":"w8:p8","session_id":"77777777-7777-4777-8777-777777777777"}
            self.herdr.agents["external-claude"]=self.herdr.agent("claude","external-claude","w8:p8",changed["claude"]["session_id"])
            hs.atomic_write_json(self.paths.owners_file,changed,mode=0o600)
        self.herdr.start_agent=drift
        with self.assertRaisesRegex(hs.SupervisorError,"ownership changed"):
            self.sup.prepare_task_sessions({"policy":"fresh-codex","profiles":{"codex":"default","claude":"default"}},preparation_id=str(uuid.uuid4()))

    def test_preserved_owner_drift_during_binding_recovery_is_ambiguous(self) -> None:
        prep=str(uuid.uuid4()); pane="w8:p1"; fresh="66666666-6666-4666-8666-666666666666"
        selection=herdr_sessions.validate_selection(self.config,{"policy":"fresh-codex","profiles":{"codex":"default","claude":"default"}})
        self.herdr.agents[f"codex-run-{prep[:8]}"]=self.herdr.agent("codex",f"codex-run-{prep[:8]}",pane,fresh)
        record=self.preparation_record(prep,selection,status="BINDING",provider_status="IDENTITY_VERIFIED",pane=pane,session=fresh)
        hs.atomic_write_json(self.paths.session_preparations_dir/f"{prep}.json",record)
        changed=json.loads(json.dumps(self.owners)); changed["claude"]={"pane_id":"w8:p8","session_id":"77777777-7777-4777-8777-777777777777"}
        self.herdr.agents["external-claude"]=self.herdr.agent("claude","external-claude","w8:p8",changed["claude"]["session_id"])
        hs.atomic_write_json(self.paths.owners_file,changed,mode=0o600)
        with self.assertRaisesRegex(hs.SupervisorError,"matches neither side"):
            self.sup.prepare_task_sessions(selection,preparation_id=prep)

    def test_missing_or_reused_old_identity_fails_before_binding(self) -> None:
        original=self.herdr.start_agent
        def remove_old(name,*,kind,pane_id,args):
            original(name,kind=kind,pane_id=pane_id,args=args)
            del self.herdr.agents["codex-main"]
        self.herdr.start_agent=remove_old
        with self.assertRaisesRegex(hs.SupervisorError,"preparation codex owner"):
            self.sup.prepare_task_sessions({"policy":"fresh-codex","profiles":{"codex":"default","claude":"default"}},preparation_id=str(uuid.uuid4()))
        self.reset_fixture(); original=self.herdr.start_agent
        def reuse_old(name,*,kind,pane_id,args):
            original(name,kind=kind,pane_id=pane_id,args=args)
            del self.herdr.agents["codex-main"]
            self.herdr.agents[name]["agent_session"]["value"]=CODEX_SESSION
        self.herdr.start_agent=reuse_old
        with self.assertRaisesRegex(hs.SupervisorError,"instead of the pre-created thread|distinct"):
            self.sup.prepare_task_sessions({"policy":"fresh-codex","profiles":{"codex":"default","claude":"default"}},preparation_id=str(uuid.uuid4()))

    def test_moved_old_identity_fails_before_binding(self) -> None:
        original=self.herdr.start_agent
        def move_old(name,*,kind,pane_id,args):
            original(name,kind=kind,pane_id=pane_id,args=args); self.herdr.agents["codex-main"]["pane_id"]="w8:p7"
        self.herdr.start_agent=move_old
        with self.assertRaisesRegex(hs.SupervisorError,"preparation codex owner"):
            self.sup.prepare_task_sessions({"policy":"fresh-codex","profiles":{"codex":"default","claude":"default"}},preparation_id=str(uuid.uuid4()))

    def test_wrong_missing_duplicate_and_busy_fresh_identity_fail_closed(self) -> None:
        modes=("wrong","missing","duplicate","busy")
        for mode in modes:
            with self.subTest(mode=mode):
                self.reset_fixture(); original=self.herdr.start_agent
                def mutate(name,*,kind,pane_id,args,mode=mode):
                    original(name,kind=kind,pane_id=pane_id,args=args)
                    if mode=="wrong": self.herdr.agents[name]["agent"]="claude"
                    if mode=="missing": self.herdr.agents[name]["agent_session"]={}
                    if mode=="duplicate": self.herdr.agents["duplicate"]=dict(self.herdr.agents[name],name="duplicate",pane_id="w8:p9")
                    if mode=="busy": self.herdr.agents[name]["agent_status"]="working"
                self.herdr.start_agent=mutate
                with self.assertRaises(hs.SupervisorError):
                    self.sup.prepare_task_sessions({"policy":"fresh-codex","profiles":{"codex":"default","claude":"default"}},preparation_id=str(uuid.uuid4()))
                self.assertEqual(self.sup.owners(),self.owners)

    def test_actual_start_timeout_reconciles_without_second_start(self) -> None:
        original=self.herdr.start_agent; calls=[0]
        def timeout(name,*,kind,pane_id,args):
            calls[0]+=1; original(name,kind=kind,pane_id=pane_id,args=args)
            raise hs.HerdrError("timeout",code="timeout")
        self.herdr.start_agent=timeout; prep=str(uuid.uuid4()); selection={"policy":"fresh-codex","profiles":{"codex":"default","claude":"default"}}
        with self.assertRaises(hs.HerdrError): self.sup.prepare_task_sessions(selection,preparation_id=prep)
        self.herdr.start_agent=original
        self.assertEqual(self.sup.prepare_task_sessions(selection,preparation_id=prep)["status"],"OWNERSHIP_BOUND")
        self.assertEqual(calls[0],1); self.assertEqual(len(self.herdr.splits),1)

    def test_cli_post_bind_crash_reuses_same_preparation(self) -> None:
        selection={"policy":"fresh-codex","profiles":{"codex":"default","claude":"default"}}
        original=self.sup.initialize
        self.sup.initialize=lambda *a,**k: (_ for _ in ()).throw(RuntimeError("crash before initialize"))
        with self.assertRaisesRegex(RuntimeError,"crash before initialize"):
            self.sup.run_new("stable cli task","codex",session_selection=selection)
        self.sup.initialize=original
        with self.assertRaisesRegex(hs.SupervisorError,"unresolved"):
            self.sup.run_new("different cli task","codex",session_selection=selection)
        self.assertEqual(len(self.herdr.splits),1); self.assertEqual(len(self.herdr.starts),1)
        self.herdr.responses=[{"v1":"done"}]
        self.assertEqual(self.sup.run_new("stable cli task","codex",session_selection=selection),0)
        self.assertEqual(len(self.herdr.splits),1); self.assertEqual(len(self.herdr.starts),1); self.assertEqual(len(self.herdr.prompts),1)
        records=[json.loads(p.read_text()) for p in self.paths.session_preparations_dir.glob("*.json")]
        self.assertEqual(len(records),1); self.assertEqual(records[0]["status"],"TASK_STARTED")
        self.assertEqual(self.state()["session_preparation"]["status"],"TASK_STARTED")

    def test_cli_post_initialize_crash_is_finalized_before_exactly_one_delivery(self) -> None:
        selection={"policy":"fresh-codex","profiles":{"codex":"default","claude":"default"}}
        original=self.sup._mark_session_preparation_started
        self.sup._mark_session_preparation_started=lambda _prep: (_ for _ in ()).throw(RuntimeError("post-initialize crash"))
        with self.assertRaisesRegex(RuntimeError,"post-initialize crash"):
            self.sup.run_new("initialized cli task","codex",session_selection=selection)
        self.sup._mark_session_preparation_started=original
        record_path=next(self.paths.session_preparations_dir.glob("*.json"))
        run_id=self.state()["run_id"]
        self.assertEqual(json.loads(record_path.read_text())["status"],"OWNERSHIP_BOUND")
        self.assertEqual(self.herdr.prompts,[])
        self.herdr.responses=[{"v1":"done"}]
        self.assertEqual(self.sup.resume(),0)
        self.assertEqual(self.state()["run_id"],run_id)
        self.assertEqual(json.loads(record_path.read_text())["status"],"TASK_STARTED")
        self.assertEqual(len(self.herdr.splits),1); self.assertEqual(len(self.herdr.starts),1); self.assertEqual(len(self.herdr.prompts),1)
        self.herdr.next_sessions["codex"]="66666666-6666-4666-8666-666666666666"; self.herdr.responses=[{"v1":"done"}]
        self.assertEqual(self.sup.run_new("later cli task","codex",session_selection=selection),0)
        self.assertEqual(len(self.herdr.splits),2); self.assertEqual(len(self.herdr.starts),2); self.assertEqual(len(self.herdr.prompts),2)

    def test_cli_post_initialize_recovery_rejects_every_request_binding_mismatch(self) -> None:
        selection={"policy":"fresh-codex","profiles":{"codex":"default","claude":"default"}}
        authorization={"budget":1,"available_count":1,"account_fingerprint":"a"*64}

        def crash_after_initialize() -> tuple[Path, dict]:
            original=self.sup._mark_session_preparation_started
            self.sup._mark_session_preparation_started=lambda _prep: (_ for _ in ()).throw(RuntimeError("post-initialize crash"))
            try:
                with self.assertRaisesRegex(RuntimeError,"post-initialize crash"):
                    self.sup.run_new(
                        "authorized task","codex",task_reference="task.md",workflow_policy="v1",
                        codex_reset_authorization=authorization,session_selection=selection,
                    )
            finally:
                self.sup._mark_session_preparation_started=original
            return next(self.paths.session_preparations_dir.glob("*.json")),self.state()

        def changed_task(state: dict, _record: dict) -> None: state["task_text"]="different task"
        def changed_start(state: dict, _record: dict) -> None: state["start_agent"]="claude"
        def changed_reference(state: dict, _record: dict) -> None: state["task_reference"]="other.md"
        def changed_workflow(state: dict, _record: dict) -> None: state["workflow_policy"]="gated_v2"
        def changed_budget(state: dict, _record: dict) -> None: state["codex_reset"]["authorized_reset_budget"]=0
        def changed_account(state: dict, _record: dict) -> None: state["codex_reset"]["account_fingerprint"]="b"*64
        def changed_selection(state: dict, _record: dict) -> None:
            state["session_selection"]={"policy":"fresh-all","profiles":{"codex":"default","claude":"default"},"fresh_providers":["codex","claude"]}
        def changed_view_hash(state: dict, _record: dict) -> None: state["session_preparation"]["request_binding"]="b"*64
        def changed_native_session(state: dict, _record: dict) -> None: state["native_sessions"]["codex"]="77777777-7777-4777-8777-777777777777"
        def changed_owners(_state: dict, _record: dict) -> None:
            owners=self.sup.owners(); owners["codex"]["session_id"]="77777777-7777-4777-8777-777777777777"
            hs.atomic_write_json(self.paths.owners_file,owners,mode=0o600)
        def changed_self_consistent_digest(state: dict, record: dict) -> None:
            envelope=json.loads(json.dumps(record["request_envelope"]))
            envelope["task_sha256"]=hashlib.sha256(b"different task").hexdigest()
            binding=herdr_sessions.cli_request_binding(self.config,envelope)
            record.update({"request_envelope":envelope,"request_binding":binding})
            state["session_preparation"]["request_binding"]=binding  # the projection never carries the envelope

        mutations=(
            ("task",changed_task),("start",changed_start),("reference",changed_reference),
            ("workflow",changed_workflow),("budget",changed_budget),("account",changed_account),
            ("selection",changed_selection),("view_hash",changed_view_hash),
            ("native_session",changed_native_session),("owners",changed_owners),
            ("self_consistent_digest",changed_self_consistent_digest),
        )
        for name,mutate in mutations:
            with self.subTest(name=name):
                self.reset_fixture(); record_path,state=crash_after_initialize(); record=json.loads(record_path.read_text())
                mutate(state,record)
                hs.atomic_write_json(record_path,record)
                self.sup.store.write_state(state)
                with self.assertRaises(hs.SupervisorError):
                    self.sup.resume()
                self.assertEqual(self.herdr.prompts,[])
                self.assertEqual(json.loads(record_path.read_text())["status"],"OWNERSHIP_BOUND")

    def test_state_and_status_projection_omit_the_private_request_envelope(self) -> None:
        """R2: the journal keeps the canonical envelope; the state/status projection exposes only the
        abbreviated operational preparation fields and the opaque binding."""
        selection={"policy":"fresh-codex","profiles":{"codex":"default","claude":"default"}}
        authorization={"budget":1,"available_count":1,"account_fingerprint":"a"*64}
        self.herdr.responses=[{"v1":"done"}]
        self.assertEqual(self.sup.run_new("private envelope task","codex",task_reference="/private/task.md",codex_reset_authorization=authorization,session_selection=selection),0)
        record=json.loads(next(self.paths.session_preparations_dir.glob("*.json")).read_text())
        envelope=record["request_envelope"]
        self.assertEqual(envelope["task_sha256"],hashlib.sha256(b"private envelope task").hexdigest())
        self.assertEqual(envelope["task_reference"],"/private/task.md")
        self.assertEqual(envelope["codex_reset_authorization"]["account_fingerprint"],"a"*64)
        for label,view in (("state",self.state()["session_preparation"]),("status",self.sup.status()["session_preparation"])):
            with self.subTest(label):
                self.assertNotIn("request_envelope",view)
                text=json.dumps(view)
                self.assertNotIn(envelope["task_sha256"],text)
                self.assertNotIn("/private/task.md",text)
                self.assertNotIn("a"*64,text)
                self.assertNotIn("task_sha256",text)
                self.assertNotIn("account_fingerprint",text)
                self.assertEqual(set(view),{"preparation_id","status","origin","request_binding","policy","profiles","providers"})
                self.assertEqual(view["preparation_id"][:8],record["preparation_id"][:8])
                self.assertEqual((view["status"],view["origin"],view["policy"],view["profiles"]),("TASK_STARTED","cli","fresh-codex",{"codex":"default","claude":"default"}))
                self.assertEqual(view["request_binding"],record["request_binding"])
                codex=view["providers"]["codex"]
                self.assertEqual(set(codex),{"status","old_session","new_session","pane_id"})
                self.assertEqual((codex["status"],codex["old_session"],len(codex["new_session"])),("IDENTITY_VERIFIED",CODEX_SESSION[:8],8))
                self.assertEqual(codex["new_session"],record["providers"]["codex"]["session_id"][:8])
                self.assertEqual(codex["pane_id"],record["providers"]["codex"]["pane_id"])
        self.assertNotIn("request_envelope",self.paths.state_file.read_text())

    def test_cli_run_command_never_finalizes_an_already_initialized_preparation(self) -> None:
        selection={"policy":"fresh-codex","profiles":{"codex":"default","claude":"default"}}
        original=self.sup._mark_session_preparation_started
        self.sup._mark_session_preparation_started=lambda _prep: (_ for _ in ()).throw(RuntimeError("post-initialize crash"))
        with self.assertRaises(RuntimeError):
            self.sup.run_new("initialized task","codex",session_selection=selection)
        self.sup._mark_session_preparation_started=original
        state=self.state(); state["supervisor_state"]="DONE"; self.sup.store.write_state(state)
        with self.assertRaisesRegex(hs.SupervisorError,"already initialized"):
            self.sup.run_new("initialized task","codex",session_selection=selection)
        self.assertEqual(self.herdr.prompts,[])
        self.assertEqual(json.loads(next(self.paths.session_preparations_dir.glob("*.json")).read_text())["status"],"OWNERSHIP_BOUND")

    def test_telegram_post_initialize_crash_recovers_exact_start_without_repreparing(self) -> None:
        preparation_id=str(uuid.uuid4())
        selection=herdr_sessions.validate_selection(
            self.config,{"policy":"fresh-codex","profiles":{"codex":"default","claude":"default"}})
        authorization={"budget":0,"available_count":0,"account_fingerprint":None}
        command={
            "request_id":"exact-initialized-start","action":"task","task_text":"stable Telegram task","source":"telegram",
            "preallocated_run_id":preparation_id,"pending_start_id":preparation_id,
            "codex_reset_authorization":authorization,"session_selection":selection,
            "actor":"telegram:1","chat_id":5,
        }
        task_binding={"task_text":command["task_text"],"source":"telegram"}
        task_sha=hashlib.sha256(json.dumps(task_binding,sort_keys=True).encode()).hexdigest()
        command["pending_task_sha256"]=task_sha
        hs.atomic_write_json(self.paths.pending_starts_dir/f"{preparation_id}.json",{
            "pending_id":preparation_id,"owner_user_id":1,"chat_id":5,"request_id":command["request_id"],
            "task_sha256":task_sha,"status":"supervisor_consuming","budget":0,"available_count":0,
            "inventory_available_count":0,"account_fingerprint":None,"session_selection":selection,
            "expires_at_unix":self.sup.clock()-1,"supervisor_consuming_at":"1970-01-12T13:46:40Z",
        })
        preparation=self.sup.prepare_task_sessions(selection,preparation_id=preparation_id)
        state=self.sup.initialize(
            command["task_text"],"codex",workflow_policy="gated_v2",codex_reset_authorization=authorization,
            run_id=preparation_id,session_selection=selection,session_preparation=preparation)
        self.assertEqual(hs.load_json(self.paths.session_preparations_dir/f"{preparation_id}.json",label="prep")["status"],"OWNERSHIP_BOUND")
        splits=len(self.herdr.splits); starts=len(self.herdr.starts)

        result=self.sup.apply_command(state,command)

        self.assertTrue(result["ok"]); self.assertEqual(result["run_id"],preparation_id)
        self.assertEqual(len(self.herdr.splits),splits); self.assertEqual(len(self.herdr.starts),starts)
        recovered=self.state()
        self.assertEqual(recovered["processed_requests"][command["request_id"]]["run_id"],preparation_id)
        self.assertEqual(recovered["session_preparation"]["status"],"TASK_STARTED")
        self.assertEqual(hs.load_json(self.paths.session_preparations_dir/f"{preparation_id}.json",label="prep")["status"],"TASK_STARTED")
        self.assertEqual(self.herdr.prompts,[])

    def test_cli_validates_task_and_reset_authority_before_fresh_side_effects(self) -> None:
        selection={"policy":"fresh-codex","profiles":{"codex":"default","claude":"default"}}
        cases=(
            {"task":"x"*(int(self.config["max_task_chars"])+1)},
            {"task":"valid","codex_reset_authorization":{"budget":2,"available_count":1,"account_fingerprint":None}},
        )
        for kwargs in cases:
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(hs.SupervisorError):
                    self.sup.run_new(kwargs.pop("task"),"codex",session_selection=selection,**kwargs)
                self.assertEqual(self.herdr.splits,[]); self.assertEqual(self.herdr.starts,[])

    def test_completed_cli_preparation_does_not_block_or_get_abandoned_by_later_work(self) -> None:
        selection={"policy":"fresh-codex","profiles":{"codex":"default","claude":"default"}}
        self.herdr.responses=[{"v1":"done"}]
        self.assertEqual(self.sup.run_new("first cli task","codex",session_selection=selection),0)
        first=next(self.paths.session_preparations_dir.glob("*.json")); first_record=json.loads(first.read_text())
        self.assertEqual(first_record["status"],"TASK_STARTED")
        with self.assertRaisesRegex(hs.SupervisorError,"cannot be abandoned"):
            self.sup.abandon_session_preparation(first_record["preparation_id"])
        self.herdr.next_sessions["codex"]="66666666-6666-4666-8666-666666666666"; self.herdr.responses=[{"v1":"done"}]
        self.assertEqual(self.sup.run_new("second cli task","codex",session_selection=selection),0)
        self.assertEqual(len(self.herdr.splits),2); self.assertEqual(len(self.herdr.starts),2)
        self.assertEqual(json.loads(first.read_text())["status"],"TASK_STARTED")

    def test_rebind_and_abandon_refuse_while_worker_lock_is_held(self) -> None:
        lock=hs.WorkerLock(self.paths.lock_file); lock.acquire()
        try:
            with self.assertRaisesRegex(hs.SupervisorError,"another herdr-supervisor worker"):
                self.sup.rebind_sessions({"codex":"w3:p2"})
            with self.assertRaisesRegex(hs.SupervisorError,"another herdr-supervisor worker"):
                self.sup.abandon_session_preparation(str(uuid.uuid4()))
        finally:
            lock.release()

    def test_binding_state_cannot_be_abandoned_or_replayed(self) -> None:
        prep=str(uuid.uuid4()); path=self.paths.session_preparations_dir/f"{prep}.json"
        selection=herdr_sessions.validate_selection(self.config,{"policy":"fresh-codex","profiles":{"codex":"default","claude":"default"}})
        record=self.preparation_record(prep,selection,status="BINDING",provider_status="IDENTITY_VERIFIED",
                                       pane="w8:p1",session="66666666-6666-4666-8666-666666666666")
        hs.atomic_write_json(path,record)
        with self.assertRaisesRegex(hs.SupervisorError,"cannot verify prepared"):
            self.sup.prepare_task_sessions(selection,preparation_id=prep)
        with self.assertRaisesRegex(hs.SupervisorError,"cannot be abandoned"):
            self.sup.abandon_session_preparation(prep)
        self.assertEqual(self.herdr.splits,[]); self.assertEqual(self.herdr.starts,[])

    def test_interrupted_agent_start_reconciles_visible_identity_without_restart(self) -> None:
        prep=str(uuid.uuid4()); path=self.paths.session_preparations_dir/f"{prep}.json"
        name=f"codex-run-{prep[:8]}"; pane="w8:p1"; fresh="66666666-6666-4666-8666-666666666666"
        selection=herdr_sessions.validate_selection(self.config,{"policy":"fresh-codex","profiles":{"codex":"default","claude":"default"}})
        self.herdr.agents[name]=self.herdr.agent("codex",name,pane,fresh)
        record=self.preparation_record(prep,selection,status="SESSION_SELECTION_AUTHORIZED",provider_status="AGENT_STARTING",pane=pane)
        record["providers"]["codex"]["thread_id"]=fresh  # the resumed agent must report exactly the pre-created thread
        hs.atomic_write_json(path,record)
        result=self.sup.prepare_task_sessions(selection,preparation_id=prep)
        self.assertEqual(result["status"],"OWNERSHIP_BOUND")
        self.assertEqual(self.sup.owners()["codex"]["session_id"],fresh)
        self.assertEqual(self.herdr.splits,[]); self.assertEqual(self.herdr.starts,[])

    def test_restart_after_identity_verification_only_binds(self) -> None:
        prep=str(uuid.uuid4()); path=self.paths.session_preparations_dir/f"{prep}.json"; pane="w8:p1"; fresh="66666666-6666-4666-8666-666666666666"
        selection=herdr_sessions.validate_selection(self.config,{"policy":"fresh-codex","profiles":{"codex":"default","claude":"default"}})
        self.herdr.agents[f"codex-run-{prep[:8]}"]=self.herdr.agent("codex",f"codex-run-{prep[:8]}",pane,fresh)
        hs.atomic_write_json(path,self.preparation_record(prep,selection,status="SESSION_SELECTION_AUTHORIZED",provider_status="IDENTITY_VERIFIED",pane=pane,session=fresh))
        result=self.sup.prepare_task_sessions(selection,preparation_id=prep)
        self.assertEqual(result["status"],"OWNERSHIP_BOUND"); self.assertEqual(self.herdr.splits,[]); self.assertEqual(self.herdr.starts,[])

    def test_interrupted_post_owner_write_marks_bound_without_rewriting_owner(self) -> None:
        prep=str(uuid.uuid4()); path=self.paths.session_preparations_dir/f"{prep}.json"
        name=f"codex-run-{prep[:8]}"; pane="w8:p1"; fresh="66666666-6666-4666-8666-666666666666"
        selection=herdr_sessions.validate_selection(self.config,{"policy":"fresh-codex","profiles":{"codex":"default","claude":"default"}})
        self.herdr.agents[name]=self.herdr.agent("codex",name,pane,fresh)
        new_owners={"codex":{"pane_id":pane,"session_id":fresh},"claude":dict(self.owners["claude"])}
        hs.atomic_write_json(self.paths.owners_file,new_owners,mode=0o600)
        record=self.preparation_record(prep,selection,status="BINDING",provider_status="IDENTITY_VERIFIED",pane=pane,session=fresh)
        hs.atomic_write_json(path,record)
        before=self.paths.owners_file.stat().st_mtime_ns
        result=self.sup.prepare_task_sessions(selection,preparation_id=prep)
        self.assertEqual(result["status"],"OWNERSHIP_BOUND")
        self.assertEqual(self.paths.owners_file.stat().st_mtime_ns,before)
        self.assertEqual(self.herdr.splits,[]); self.assertEqual(self.herdr.starts,[])

    def test_rebind_preview_then_apply_and_nonterminal_refusal(self) -> None:
        fresh="66666666-6666-4666-8666-666666666666"
        self.herdr.agents["external"]=self.herdr.agent("codex","external","w8:p1",fresh)
        preview=self.sup.rebind_sessions({"codex":"w8:p1"})
        self.assertFalse(preview["applied"]); self.assertEqual(self.sup.owners(),self.owners)
        applied=self.sup.rebind_sessions({"codex":"w8:p1"},apply=True)
        self.assertTrue(applied["applied"]); self.assertEqual(self.sup.owners()["codex"]["session_id"],fresh)
        self.sup.initialize("task","codex")
        with self.assertRaisesRegex(hs.SupervisorError,"nonterminal"):
            self.sup.rebind_sessions({"claude":"w3:p1"})

    def test_cli_contract_defaults_and_explicit_options(self) -> None:
        parser=hs.build_parser()
        default=parser.parse_args(["run","task"])
        self.assertEqual((default.session_policy,default.codex_model_profile),("preserve","default"))
        explicit=parser.parse_args(["run","--session-policy","fresh-all","--codex-model-profile","large","task"])
        self.assertEqual((explicit.session_policy,explicit.codex_model_profile),("fresh-all","large"))


class CapabilityTests(V2Case):
    def test_session_contract_requires_enabled_compatible_split_and_start(self) -> None:
        report=self.herdr.capability_report()
        self.assertTrue(herdr_sessions.session_contract_supported(self.config,report))
        disabled=json.loads(json.dumps(self.config)); disabled["session_start"]["enabled"]=False
        self.assertFalse(herdr_sessions.session_contract_supported(disabled,report))
        for key in ("compatible","required","optional"):
            broken=json.loads(json.dumps(report))
            if key=="compatible": broken[key]=False
            elif key=="required": broken[key]["agent start (--kind/--pane)"]=False
            else: broken[key]["task-start pane split (--direction/--ratio/--cwd/--no-focus)"]=False
            self.assertFalse(herdr_sessions.session_contract_supported(self.config,broken))

    def test_model_probe_requires_exact_flag_lexeme(self) -> None:
        def result(text):
            return lambda *a,**k: subprocess.CompletedProcess([],0,text,"")
        self.assertTrue(herdr_sessions.probe_model_flag("codex",runner=result("usage: codex --model NAME")))
        self.assertFalse(herdr_sessions.probe_model_flag("codex",runner=result("usage: codex --models NAME")))

    def test_recorded_provider_versions_expose_exact_model_flag(self) -> None:
        fixtures=Path(__file__).parent/"fixtures"/"providers"
        for filename in ("codex-0.154.0-help.txt","claude-2.1.269-help.txt"):
            text=(fixtures/filename).read_text()
            self.assertTrue(herdr_cli.help_has_lexeme(text,"--model"),filename)

    def test_config_rejects_raw_or_missing_default_profile(self) -> None:
        broken=json.loads(json.dumps(self.config)); broken["session_start"]["model_profiles"]["codex"]={"x":{"label":"X","model":"x"}}
        with self.assertRaisesRegex(hs.SupervisorError,"include default"):
            herdr_core.validate_session_start_config(broken)
        raw=json.loads(json.dumps(self.config)); raw["session_start"]["model_profiles"]["codex"]["bad"]={"label":"Bad","model":"--danger"}
        with self.assertRaisesRegex(hs.SupervisorError,"invalid codex model profile"):
            herdr_core.validate_session_start_config(raw)

    def test_selection_and_journal_reject_noncanonical_primitives(self) -> None:
        with self.assertRaisesRegex(hs.SupervisorError,"must be a string"):
            herdr_sessions.validate_selection(self.config,{"policy":"fresh-codex","profiles":{"codex":1,"claude":"default"}})
        for profiles in (False,0,"",None):
            with self.subTest(profiles=profiles), self.assertRaisesRegex(hs.SupervisorError,"profiles are invalid"):
                herdr_sessions.validate_selection(self.config,{"policy":"preserve","profiles":profiles})
        prep=str(uuid.uuid4()); selection=herdr_sessions.validate_selection(self.config,{"policy":"fresh-codex","profiles":{"codex":"default","claude":"default"}})
        record={"schema_version":2,"preparation_id":prep,"status":"SESSION_SELECTION_AUTHORIZED","selection":selection,
                "created_at":"1970-01-12T13:46:40Z","origin":"supervisor","request_binding":None,
                "owners_before":json.loads(json.dumps(self.owners)),
                "providers":{"codex":{"status":"PANE_CREATING","old_pane_id":"w3:p2","old_session_id":CODEX_SESSION,
                                      "profile":"default","preparation_id":prep}}}
        corruptions=(
            ("schema",lambda value:value.update(schema_version=True)),
            ("owner",lambda value:value["owners_before"]["codex"].update(session_id="not-a-uuid")),
            ("phase",lambda value:value["providers"]["codex"].update(pane_id="w9:p1")),
            ("name",lambda value:(value["providers"]["codex"].update(status="AGENT_STARTING",pane_id="w9:p1",agent_name="other"))),
        )
        for name,change in corruptions:
            with self.subTest(name=name):
                candidate=json.loads(json.dumps(record)); change(candidate)
                with self.assertRaises(hs.SupervisorError):
                    herdr_sessions.validate_preparation_record(self.config,candidate,prep)

    def test_invalid_preparation_ids_fail_before_any_fresh_side_effect(self) -> None:
        selection={"policy":"fresh-codex","profiles":{"codex":"default","claude":"default"}}
        invalid=(None,True,"None","NOT-A-UUID",str(uuid.uuid4()).upper())
        for preparation_id in invalid:
            with self.subTest(preparation_id=preparation_id):
                self.reset_fixture()
                with self.assertRaisesRegex(hs.SupervisorError,"canonical UUID"):
                    self.sup.prepare_task_sessions(selection,preparation_id=preparation_id)
                self.assertEqual(self.herdr.splits,[]); self.assertEqual(self.herdr.starts,[])
                self.assertEqual(self.sup.owners(),self.owners)
                self.assertEqual(list(self.paths.session_preparations_dir.glob("*.json")),[])
        for origin,binding in (("cli",None),("supervisor","a"*64),("unknown",None)):
            with self.subTest(origin=origin,binding=binding):
                self.reset_fixture()
                with self.assertRaisesRegex(hs.SupervisorError,"origin or request binding"):
                    self.sup.prepare_task_sessions(selection,preparation_id=str(uuid.uuid4()),origin=origin,request_binding=binding)
                self.assertEqual(self.herdr.splits,[]); self.assertEqual(self.herdr.starts,[])
                self.assertEqual(list(self.paths.session_preparations_dir.glob("*.json")),[])

    def test_split_result_and_recovered_journal_require_globally_distinct_panes(self) -> None:
        selection={"policy":"fresh-codex","profiles":{"codex":"default","claude":"default"}}
        for pane in ("w3:p2","w3:p1",True,17,""):
            with self.subTest(pane=pane):
                self.reset_fixture(); self.herdr.next_pane=pane
                with self.assertRaisesRegex(hs.SupervisorError,"canonical distinct"):
                    self.sup.prepare_task_sessions(selection,preparation_id=str(uuid.uuid4()))
                self.assertEqual(self.herdr.starts,[]); self.assertEqual(self.sup.owners(),self.owners)
        prep=str(uuid.uuid4())
        normalized=herdr_sessions.validate_selection(
            self.config,{"policy":"fresh-all","profiles":{"codex":"default","claude":"default"}})
        providers={}
        for provider in ("codex","claude"):
            old=self.owners[provider]
            providers[provider]={"status":"FRESH_PANE_CREATED","old_pane_id":old["pane_id"],"old_session_id":old["session_id"],
                                 "profile":"default","preparation_id":prep,"pane_id":"w9:p1"}
        providers["codex"].update({"thread_request_id":f"thread-{prep}-codex","thread_payload":{"cwd":str(self.project_root),"model":None,"ephemeral":False},
                                   "thread_id":"44444444-4444-4444-8444-444444444444","thread_model":"gpt-5-codex","thread_provider":"openai"})
        record={"schema_version":2,"preparation_id":prep,"status":"SESSION_SELECTION_AUTHORIZED","selection":normalized,
                "created_at":"1970-01-12T13:46:40Z","origin":"supervisor","request_binding":None,
                "owners_before":json.loads(json.dumps(self.owners)),"providers":providers}
        with self.assertRaisesRegex(hs.SupervisorError,"not distinct"):
            herdr_sessions.validate_preparation_record(self.config,record,prep)


if __name__ == "__main__":
    import unittest
    unittest.main()
