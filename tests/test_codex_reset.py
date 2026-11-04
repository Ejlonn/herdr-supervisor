"""Banked reset authorization, exactly-once state and supported protocol tests. No live redemption."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src"))
import herdr_codex_reset as hcr
import herdr_supervisor as hs
import herdr_telegram as ht
from test_telegram import TelegramCase, callback, message
from v2_fixtures import NOW, V2Case

FP="a"*64

def raw_inventory(count:int,allowed:bool|None=False,account:str="account-a",details=True):
    credits={"availableCount":count}
    if details: credits["credits"]=[{"id":"never-exposed","expiresAt":NOW+100,"status":"available"} for _ in range(count)]
    return {"accountId":account,"ordinaryUsageAllowed":allowed,"rateLimitResetCredits":credits,"rateLimits":{}}

class FakeGateway:
    def __init__(self,case:V2Case,count=3,consume="reset",account=FP):
        self.case,self.count,self.outcome,self.account=case,count,consume,account
        self.calls=[]; self.consumed=False; self.fail_inventory=False; self.fail_consume=False
    def inventory(self,request_id):
        self.calls.append(("inventory",request_id))
        if self.fail_inventory: raise hcr.ResetError("offline")
        return hcr.ResetInventory(self.count- (1 if self.consumed else 0), True if self.consumed else False,self.account,self.case.clock.current)
    def consume(self,key,request_id):
        self.calls.append(("consume",key,request_id))
        if self.fail_consume: raise hcr.ResetError("timeout")
        self.consumed=True
        self.case.write_quota_fresh("codex",80,NOW+3600,60,NOW+86400)
        return self.outcome

class InventoryTests(unittest.TestCase):
    def test_zero_one_many_and_aggregate_or_detail(self):
        for count in (0,1,7):
            for details in (False,True):
                inv=hcr.parse_inventory(raw_inventory(count,details=details),now=NOW)
                self.assertEqual(inv.available_count,count)
                self.assertEqual(len(inv.credits),count if details else 0)
                self.assertNotEqual(inv.account_fingerprint,"account-a")
                self.assertNotIn("id",json.dumps(inv.as_dict()))
    def test_unavailable_and_malformed_fail_closed(self):
        for value in (None,{}, {"rateLimitResetCredits":{}},{"rateLimitResetCredits":{"availableCount":-1}},{"rateLimitResetCredits":{"availableCount":True}}):
            with self.subTest(value=value),self.assertRaises(hcr.ResetError): hcr.parse_inventory(value)
    def test_consume_outcomes_are_closed_enum(self):
        expected={"reset":"reset","nothingToReset":"nothing_to_reset","noCredit":"no_credit","alreadyRedeemed":"already_redeemed"}
        for raw,want in expected.items(): self.assertEqual(hcr.parse_consume({"outcome":raw}),want)
        with self.assertRaises(hcr.ResetError): hcr.parse_consume({"outcome":"mystery"})
    def test_client_uses_only_supported_stdio_methods(self):
        with tempfile.TemporaryDirectory() as tmp:
            log=Path(tmp)/"methods"; script=Path(tmp)/"fake.py"
            script.write_text("""import json,sys,os
first=json.loads(sys.stdin.readline()); print(json.dumps({'jsonrpc':'2.0','id':1,'result':{}}),flush=True)
json.loads(sys.stdin.readline()); req=json.loads(sys.stdin.readline())
open(os.environ['METHOD_LOG'],'a').write(req['method']+':'+os.environ.get('SECRET_SENTINEL','absent')+'\\n')
result={'accountId':'acct','ordinaryUsageAllowed':False,'rateLimitResetCredits':{'availableCount':2},'rateLimits':{}} if req['method']=='account/rateLimits/read' else {'outcome':'reset'}
print(json.dumps({'jsonrpc':'2.0','id':2,'result':result}),flush=True)
""")
            old=os.environ.get("METHOD_LOG"); old_secret=os.environ.get("SECRET_SENTINEL"); os.environ["METHOD_LOG"]=str(log); os.environ["SECRET_SENTINEL"]="must-not-leak"
            try:
                client=hcr.AppServerClient([sys.executable,str(script)],timeout=2,extra_env={"METHOD_LOG":str(log)})
                self.assertEqual(client.inventory().available_count,2)
                self.assertEqual(client.consume("stable-idempotency-key"),"reset")
            finally:
                if old is None: os.environ.pop("METHOD_LOG",None)
                else: os.environ["METHOD_LOG"]=old
                if old_secret is None: os.environ.pop("SECRET_SENTINEL",None)
                else: os.environ["SECRET_SENTINEL"]=old_secret
            self.assertEqual(log.read_text().splitlines(),["account/rateLimits/read:absent","account/rateLimitResetCredit/consume:absent"])

    def test_inventory_freshness_and_fingerprint_are_strict(self):
        with self.assertRaises(hcr.ResetError):
            hcr.ensure_inventory_fresh(hcr.parse_inventory(raw_inventory(1),now=NOW-500),now=NOW,max_age=120)
        with self.assertRaises(hcr.ResetError):
            hcr.ensure_inventory_fresh(hcr.parse_inventory(raw_inventory(1),now=NOW+61),now=NOW,max_age=120)
        with self.assertRaises(hcr.ResetError):
            hcr.parse_inventory({**raw_inventory(1),"accountFingerprint":"z"*64},now=NOW)

class StateAndRuntimeTests(V2Case):
    def initialized(self,budget=2,count=3,gateway=None):
        gateway=gateway or FakeGateway(self,count=count)
        self.sup.reset_gateway=gateway
        state=self.sup.initialize("task","codex",workflow_policy="gated_v2",codex_reset_authorization={"budget":budget,"available_count":count,"account_fingerprint":gateway.account})
        return state,gateway
    def blocked(self,reset=NOW+3600): return (hs.QuotaWindow("five_hour",0,reset),)
    def test_default_is_zero_and_legacy_schema2_migrates_zero(self):
        state=self.sup.initialize("task","codex")
        self.assertEqual(state["codex_reset"]["authorized_reset_budget"],0)
        state.pop("codex_reset"); state["schema_version"]=2; hs.atomic_write_json(self.paths.state_file,state)
        self.assertEqual(self.sup.store.read_state()["codex_reset"]["used_reset_count"],0)
    def test_authorization_bounds_and_account_required(self):
        for auth in ({"budget":-1,"available_count":2,"account_fingerprint":FP},{"budget":3,"available_count":2,"account_fingerprint":FP},{"budget":1,"available_count":2,"account_fingerprint":None}):
            self.reset_fixture()
            with self.assertRaises(hs.SupervisorError): self.sup.initialize("task","codex",codex_reset_authorization=auth)
    def test_success_persists_before_consume_verifies_and_counts_once(self):
        state,g=self.initialized(); outcome=self.sup.try_authorized_codex_reset(state,self.blocked(),midturn=False)
        self.assertEqual(outcome,"continue"); self.assertEqual(state["codex_reset"]["used_reset_count"],1)
        self.assertEqual(state["codex_reset"]["current_redemption_state"],"IDLE")
        consume=[x for x in g.calls if x[0]=="consume"]; self.assertEqual(len(consume),1)
        self.assertTrue(state["codex_reset"]["redemption_verified_at"])
        self.assertIn("CODEX_RESET_VERIFIED",self.event_types())
    def test_budget_exhausted_never_reads_or_consumes(self):
        state,g=self.initialized(budget=1); state["codex_reset"]["used_reset_count"]=1
        self.assertIsNone(self.sup.try_authorized_codex_reset(state,self.blocked(),midturn=False)); self.assertEqual(g.calls,[])
    def test_inventory_refreshed_and_external_exhaustion_falls_back(self):
        state,g=self.initialized(); g.count=0
        self.assertIsNone(self.sup.try_authorized_codex_reset(state,self.blocked(),midturn=False))
        self.assertEqual([x[0] for x in g.calls],["inventory"])
        self.assertEqual(state["codex_reset"]["current_redemption_state"],"IDLE")
        self.assertIsNone(state["codex_reset"]["current_idempotency_key"])
    def test_account_mismatch_fails_closed_before_consume(self):
        state,g=self.initialized(); g.account="b"*64
        self.assertEqual(self.sup.try_authorized_codex_reset(state,self.blocked(),midturn=False),"stop")
        self.assertEqual(state["supervisor_state"],"WAIT_USER"); self.assertFalse(any(x[0]=="consume" for x in g.calls))
    def test_uncertain_consume_keeps_stable_key_and_blocks_second_credit(self):
        state,g=self.initialized(); g.fail_consume=True
        self.assertEqual(self.sup.try_authorized_codex_reset(state,self.blocked(),midturn=False),"stop")
        key=state["codex_reset"]["current_idempotency_key"]
        self.assertEqual(state["codex_reset"]["current_redemption_state"],"RESET_RECONCILING")
        self.assertEqual(self.sup.try_authorized_codex_reset(state,self.blocked(NOW+7200),midturn=False),"stop")
        self.assertEqual(state["codex_reset"]["current_idempotency_key"],key)
        self.assertEqual(len([x for x in g.calls if x[0]=="consume"]),1)
        g.fail_consume=False
        self.assertEqual(self.sup.try_authorized_codex_reset(state,self.blocked(),midturn=False,allow_uncertain_retry=True),"continue")
        self.assertEqual([x[1] for x in g.calls if x[0]=="consume"],[key,key])
        self.assertEqual(state["codex_reset"]["used_reset_count"],1)
    def test_delayed_or_failed_verification_never_burns_next(self):
        state,g=self.initialized(); g.consumed=True
        g.inventory=lambda request_id: hcr.ResetInventory(2,False,FP,self.clock.current)
        g.consume=lambda key,request_id:"already_redeemed"
        self.assertEqual(self.sup.try_authorized_codex_reset(state,self.blocked(),midturn=False),"stop")
        self.assertEqual(state["codex_reset"]["used_reset_count"],0)
        self.assertEqual(state["codex_reset"]["current_redemption_state"],"RESET_RECONCILING")
    def test_no_credit_does_not_increment(self):
        state,g=self.initialized(gateway=FakeGateway(self,consume="no_credit"))
        state["delivery"]={"turn_id":"11111111-1111-4111-8111-111111111111","status":"accepted"}
        self.assertIsNone(self.sup.try_authorized_codex_reset(state,self.blocked(),midturn=False)); self.assertEqual(state["codex_reset"]["used_reset_count"],0)
        calls=len(g.calls)
        self.assertIsNone(self.sup.try_authorized_codex_reset(state,self.blocked(NOW+7200),midturn=False))
        self.assertEqual(len(g.calls),calls,"a definite no-credit result must close this blocking event")
    def test_three_distinct_blocks_respect_budget(self):
        state,g=self.initialized(budget=3,count=5)
        for n in range(3):
            g.consumed=False
            state["delivery"]={"turn_id":f"0000000{n+1}-0000-4000-8000-00000000000{n+1}","status":"accepted"}
            self.assertEqual(self.sup.try_authorized_codex_reset(state,self.blocked(NOW+3600+n),midturn=False),"continue")
        self.assertEqual(state["codex_reset"]["used_reset_count"],3)
        g.calls.clear(); self.assertIsNone(self.sup.try_authorized_codex_reset(state,self.blocked(NOW+9999),midturn=False)); self.assertEqual(g.calls,[])
    def test_restart_reconciles_inflight_with_same_key_and_never_replays_block(self):
        state,g=self.initialized(); block=self.blocked(); block_id=self.sup._reset_block_id(state,block)
        key="stable-restart-idempotency-key"
        state["codex_reset"].update({"blocking_event_id":block_id,"current_reset_sequence":1,"current_idempotency_key":key,"current_redemption_state":"RESET_CONSUMING"})
        self.sup.store.write_state(state)
        recovered=self.sup.store.read_state(); self.assertEqual(self.sup.try_authorized_codex_reset(recovered,block,midturn=False),"continue")
        self.assertEqual([x[1] for x in g.calls if x[0]=="consume"],[key])
        g.calls.clear(); self.assertIsNone(self.sup.try_authorized_codex_reset(recovered,block,midturn=False)); self.assertEqual(g.calls,[])

    def test_reconciliation_inventory_failure_stays_wait_user_with_same_operation(self):
        state,g=self.initialized(); block=self.blocked(); block_id=self.sup._reset_block_id(state,block)
        key="stable-restart-idempotency-key"
        state["codex_reset"].update({"blocking_event_id":block_id,"current_reset_sequence":1,
            "current_idempotency_key":key,"current_redemption_state":"RESET_RECONCILING"})
        g.fail_inventory=True
        self.assertEqual(self.sup.try_authorized_codex_reset(state,block,midturn=True,allow_uncertain_retry=True),"stop")
        self.assertEqual(state["supervisor_state"],"WAIT_USER")
        self.assertEqual(state["codex_reset"]["current_idempotency_key"],key)
        self.assertFalse(any(call[0]=="consume" for call in g.calls))

    def test_restart_after_verified_state_resumes_without_consuming_again(self):
        state,g=self.initialized(); block=self.blocked(); block_id=self.sup._reset_block_id(state,block)
        turn="11111111-1111-4111-8111-111111111111"
        state["delivery"]={"turn_id":turn,"status":"accepted"}
        state["codex_reset"].update({"blocking_event_id":block_id,"current_reset_sequence":1,
            "current_idempotency_key":"stable-restart-idempotency-key","current_redemption_state":"RESET_VERIFIED",
            "used_reset_count":1,"last_verified_blocking_event_id":block_id,
            "last_verified_delivery_turn_id":turn,"redemption_verified_at":hs.iso_utc(self.clock.current)})
        self.sup.store.write_state(state)
        resumed=[]
        self.sup.resume_interrupted_turn=lambda current,provider: resumed.append((current["run_id"],provider)) or "stop"
        self.assertEqual(self.sup.run_loop(self.sup.store.read_state(),recovering=True),2)
        self.assertEqual(resumed,[(state["run_id"],"codex")])
        recovered=self.sup.store.read_state()["codex_reset"]
        self.assertEqual(recovered["current_redemption_state"],"IDLE")
        self.assertIsNone(recovered["current_idempotency_key"])
        self.assertFalse(any(call[0]=="consume" for call in g.calls))

    def test_reset_timestamp_change_does_not_create_a_second_block(self):
        state,g=self.initialized(budget=2,count=3)
        first=self.blocked(NOW+3600)
        block_id=self.sup._reset_block_id(state,first)
        self.assertEqual(self.sup._reset_block_id(state,self.blocked(NOW+7200)),block_id)

    def test_stale_inventory_and_stale_quota_cannot_verify(self):
        self.reset_fixture(); state,g=self.initialized()
        g.inventory=lambda request_id:hcr.ResetInventory(2 if g.consumed else 3,True if g.consumed else False,FP,self.clock.current-500)
        self.assertIsNone(self.sup.try_authorized_codex_reset(state,self.blocked(),midturn=False))
        self.assertFalse(any(call[0]=="consume" for call in g.calls))
        self.reset_fixture(); state,g=self.initialized()
        original=g.consume
        def consume_without_fresh_quota(key,request_id):
            result=original(key,request_id)
            self.write_quota_fresh("codex",80,NOW+3600,60,NOW+86400,fetched=NOW-500)
            return result
        g.consume=consume_without_fresh_quota
        self.assertEqual(self.sup.try_authorized_codex_reset(state,self.blocked(),midturn=False),"stop")
        self.assertEqual(state["codex_reset"]["current_redemption_state"],"RESET_RECONCILING")
    def test_status_separates_account_snapshot_and_run_budget_without_secrets(self):
        state,g=self.initialized(); state["codex_reset"]["used_reset_count"]=1; self.sup.store.write_state(state)
        view=self.sup.status()["codex_reset"]
        self.assertEqual(view["authorized_for_run"],2); self.assertEqual(view["budget_remaining"],1)
        self.assertNotIn("fingerprint",json.dumps(view)); self.assertNotIn("idempotency",json.dumps(view))

    def test_corrupt_inflight_state_is_rejected(self):
        state,g=self.initialized()
        state["codex_reset"].update({"current_redemption_state":"RESET_CONSUMING","current_idempotency_key":None})
        hs.atomic_write_json(self.paths.state_file,state)
        with self.assertRaises(hs.SupervisorError): self.sup.store.read_state()

class HelperJournalTests(unittest.TestCase):
    class Client:
        def __init__(self): self.consume_keys=[]
        def inventory(self): return hcr.ResetInventory(2,False,FP,NOW)
        def consume(self,key): self.consume_keys.append(key); return "reset"
    def test_helper_inventory_and_same_request_are_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); client=self.Client()
            hcr.atomic_json(root/"pending/r1.json",hcr.make_request("inventory",{},"r1"))
            self.assertEqual(hcr.process_once(root,client),0)
            result=json.loads((root/"results/r1.json").read_text()); self.assertTrue(result["ok"]); self.assertEqual(result["inventory"]["availableCount"],2)
            self.assertEqual(hcr.JournalGateway(root,timeout=.01,sleeper=lambda _:None).inventory("r1").available_count,2)
            hcr.atomic_json(root/"pending/c1.json",hcr.make_request("consume",{"idempotency_key":"stable-key-123456"},"c1"))
            hcr.process_once(root,client); hcr.atomic_json(root/"pending/c1.json",hcr.make_request("consume",{"idempotency_key":"different-key-123"},"c1")); hcr.process_once(root,client)
            self.assertEqual(client.consume_keys,["stable-key-123456"])

    def test_helper_rejects_body_filename_mismatch_without_path_escape(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); outside=root/"escaped.json"
            bad=hcr.make_request("inventory",{},"safe")
            bad["request_id"]="../escaped"
            hcr.atomic_json(root/"pending/safe.json",bad)
            hcr.process_once(root,self.Client())
            self.assertFalse(outside.exists())
            result=json.loads((root/"results/safe.json").read_text())
            self.assertFalse(result["ok"])

    def test_gateway_refuses_result_for_different_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); request=hcr.make_request("consume",{"idempotency_key":"stable-key-123456"},"same")
            hcr.atomic_json(root/"results/same.json",{"request_id":"same","request_digest":request["request_digest"],"ok":True,"outcome":"reset"})
            gateway=hcr.JournalGateway(root,timeout=.01,sleeper=lambda _:None)
            with self.assertRaises(hcr.ResetError):
                gateway.consume("different-key-123","same")

    def test_telegram_inventory_uses_journal_worker_with_unique_read_requests(self):
        calls=[]
        class Gateway:
            def __init__(self,root,*,timeout,sleeper=None): calls.append(("init",root,timeout))
            def inventory(self,request_id):
                calls.append(("inventory",request_id))
                return hcr.ResetInventory(3,True,FP,NOW)
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(ht.hcr,"JournalGateway",Gateway):
            root=Path(tmp)
            paths=hs.Paths(config_file=root/"config.json",state_dir=root/"state")
            config={"codex_reset":{"helper_timeout_seconds":17}}
            reader=ht.journal_reset_inventory_reader(paths,config)
            self.assertEqual(reader().available_count,3)
            self.assertEqual(reader().available_count,3)
        self.assertEqual(calls[0],("init",paths.codex_reset_dir,17.0))
        request_ids=[item[1] for item in calls[1:]]
        self.assertEqual(len(request_ids),2)
        self.assertNotEqual(request_ids[0],request_ids[1])
        self.assertTrue(all(item.startswith("telegram-inventory-") for item in request_ids))

class TelegramAuthorizationTests(TelegramCase):
    def enabled_bridge(self,count=3,account=FP):
        self.bridge.reset_inventory_reader=lambda:hcr.ResetInventory(count,True,account,self.clock.current)
        return self.bridge
    def budget_tokens(self):
        return {json.loads(p.read_text())["budget"]:p.stem for p in self.tg_paths.interactions_dir.glob("*.json") if json.loads(p.read_text()).get("action")=="reset_budget"}
    def test_session_policy_callback_is_task_bound_and_leads_to_final_start(self):
        self.enabled_bridge(2).handle_update(message(490,"/task rotate codex"))
        initial=self.api.sent[-1]
        choices={b["text"]:b["callback_data"] for row in initial["reply_markup"]["inline_keyboard"] for b in row}
        result=self.bridge.handle_update(callback(491,choices["Fresh Codex"]))
        self.assertEqual(result["result"],"selection_updated")
        final=self.api.sent[-1]
        self.assertIn("Task:\nrotate codex",final["text"]); self.assertIn("Sessions: fresh-codex",final["text"])
        self.assertIn("Existing sessions stay preserved",final["text"]); self.assertIn("only when first prompted",final["text"])
        starts={b["text"]:b["callback_data"] for row in final["reply_markup"]["inline_keyboard"] for b in row if b["text"].startswith("Start task")}
        self.assertEqual(self.bridge.handle_update(callback(492,starts["Start task · reset budget 1"]))["result"],"enqueued")
        command=self.pending_inbox()[0]
        self.assertEqual(command["session_selection"]["policy"],"fresh-codex")
        self.assertEqual(command["codex_reset_authorization"]["budget"],1)
        self.assertIn("rejected",self.bridge.handle_update(callback(493,choices["Preserve sessions (default)"])))
    def test_nondefault_model_is_not_offered_without_local_capability(self):
        self.config["session_start"]["model_profiles"]["codex"]["large"]={"label":"Large","model":"gpt-approved"}
        self.bridge.model_capability_reader=lambda provider:False
        self.enabled_bridge(1).handle_update(message(494,"/task capability check"))
        choices={b["text"]:b["callback_data"] for row in self.api.sent[-1]["reply_markup"]["inline_keyboard"] for b in row}
        self.bridge.handle_update(callback(495,choices["Fresh Codex"]))
        labels={b["text"] for row in self.api.sent[-1]["reply_markup"]["inline_keyboard"] for b in row}
        self.assertIn("✓ Codex model: Default",labels)
        self.assertNotIn("Codex model: Large",labels)
    def test_model_callback_rechecks_capability_before_mutation(self):
        self.config["session_start"]["model_profiles"]["codex"]["large"]={"label":"Large","model":"gpt-approved"}
        available=[True]; self.bridge.model_capability_reader=lambda provider:available[0]
        self.enabled_bridge(1).handle_update(message(496,"/task capability changes"))
        choices={b["text"]:b["callback_data"] for row in self.api.sent[-1]["reply_markup"]["inline_keyboard"] for b in row}
        self.bridge.handle_update(callback(497,choices["Fresh Codex"]))
        profiles={b["text"]:b["callback_data"] for row in self.api.sent[-1]["reply_markup"]["inline_keyboard"] for b in row}
        available[0]=False
        result=self.bridge.handle_update(callback(498,profiles["Codex model: Large"]))
        self.assertEqual(result["rejected"],"invalid_model_capability")

    def test_fresh_policies_are_hidden_or_rejected_when_contract_is_unavailable(self):
        capability=[False]; self.bridge.session_capability_reader=lambda:capability[0]
        self.enabled_bridge(1).handle_update(message(499,"/task preserve only"))
        labels={b["text"] for row in self.api.sent[-1]["reply_markup"]["inline_keyboard"] for b in row}
        self.assertIn("Preserve sessions (default)",labels); self.assertNotIn("Fresh Codex",labels)
        capability[0]=True
        self.bridge.handle_update(message(500,"/task capability changes"))
        choices={b["text"]:b["callback_data"] for row in self.api.sent[-1]["reply_markup"]["inline_keyboard"] for b in row}
        capability[0]=False
        result=self.bridge.handle_update(callback(501,choices["Fresh Codex"]))
        self.assertEqual(result["rejected"],"invalid_session_capability")
    def test_task_waits_for_explicit_bound_budget_then_enqueues_once(self):
        bridge=self.enabled_bridge(3)
        result=bridge.handle_update(message(501,"/task important task")); self.assertEqual(result["result"],"reset_budget_required")
        self.assertEqual(self.pending_inbox(),[]); self.assertIn("Banked resets available: 3",self.texts()[-1]); self.assertIn("weekly",self.texts()[-1])
        self.assertIn("Start · 0",{b["text"] for row in self.api.sent[-1]["reply_markup"]["inline_keyboard"] for b in row})
        token=self.budget_tokens()[2]
        started=bridge.handle_update(callback(502,token)); self.assertEqual(started["result"],"enqueued")
        command=self.pending_inbox()[0]; self.assertEqual(command["codex_reset_authorization"]["budget"],2)
        self.assertIn("Authorized for this task: 2",self.texts()[-1])
        self.assertIn("rejected",bridge.handle_update(callback(503,token))); self.assertEqual(len(self.pending_inbox()),1)
    def test_stale_inventory_wrong_actor_and_oversized_numeric_are_refused(self):
        current=[3]
        bridge=self.bridge; bridge.reset_inventory_reader=lambda:hcr.ResetInventory(current[0],True,FP,self.clock.current)
        bridge.handle_update(message(510,"/task task")); token=self.budget_tokens()[3]
        self.assertIn("rejected",bridge.handle_update(callback(511,token,user=999)))
        current[0]=2; self.assertEqual(bridge.handle_update(callback(512,token))["rejected"],"stale_inventory")
        bridge.handle_update(message(513,"/reset-budget 9")); self.assertEqual(self.pending_inbox(),[])
    def test_zero_budget_and_uploaded_task_share_normal_task_command(self):
        bridge=self.enabled_bridge(1); bridge.handle_update(message(520,"/task zero")); token=self.budget_tokens()[0]
        bridge.handle_update(callback(521,token)); self.assertEqual(self.pending_inbox()[0]["codex_reset_authorization"]["budget"],0)

    def test_unavailable_inventory_requires_explicit_zero_budget_start(self):
        def unavailable(): raise hcr.ResetError("helper unavailable")
        self.bridge.reset_inventory_reader=unavailable
        result=self.bridge.handle_update(message(522,"/task preserved"))
        self.assertEqual(result["result"],"reset_budget_required")
        self.assertIsNone(result["available"])
        self.assertEqual(self.pending_inbox(),[])
        card=self.api.sent[-1]
        self.assertIn("Banked resets available: unavailable",card["text"])
        buttons={b["text"]:b["callback_data"] for row in card["reply_markup"]["inline_keyboard"] for b in row}
        self.assertEqual(set(buttons),{"✓ Workflow: Supervised","Workflow: Plan approval","✓ Start with: Codex","Start with: Claude","Start · 0","Preserve sessions (default)","Fresh Codex","Fresh Claude + Codex","Cancel"})
        started=self.bridge.handle_update(callback(523,buttons["Start · 0"]))
        self.assertEqual(started["result"],"enqueued")
        self.assertEqual(self.pending_inbox()[0]["codex_reset_authorization"],{"budget":0,"available_count":0,"account_fingerprint":None})

    def test_pending_reset_start_can_be_cancelled_before_run(self):
        self.enabled_bridge(2).handle_update(message(524,"/task cancel me"))
        card=self.api.sent[-1]
        cancel_token=next(b["callback_data"] for row in card["reply_markup"]["inline_keyboard"] for b in row if b["text"]=="Cancel")
        result=self.bridge.handle_update(callback(525,cancel_token))
        self.assertEqual(result["result"],"cancelled")
        self.assertEqual(self.pending_inbox(),[])
        pending=next(self.paths.pending_starts_dir.glob("*.json"))
        self.assertEqual(json.loads(pending.read_text())["status"],"cancelled")

    def test_supervisor_consumes_bound_authorization_and_rejects_forgery(self):
        bridge=self.enabled_bridge(3); bridge.handle_update(message(530,"/task bound")); token=self.budget_tokens()[2]
        bridge.handle_update(callback(531,token))
        gateway=FakeGateway(self,count=3); self.sup.reset_gateway=gateway
        result=self.sup.process_inbox()[0]
        self.assertTrue(result["ok"])
        pending=list(self.paths.pending_starts_dir.glob("*.json"))
        self.assertEqual(json.loads(pending[0].read_text())["status"],"started")

        self.reset_fixture(); bridge=self.enabled_bridge(3); bridge.handle_update(message(532,"/task forged")); token=self.budget_tokens()[2]
        bridge.handle_update(callback(533,token))
        command_path=next((self.paths.inbox_dir/"pending").glob("*.json")); command=json.loads(command_path.read_text())
        command["codex_reset_authorization"]["budget"]=3
        hs.atomic_write_json(command_path,command)
        self.sup.reset_gateway=FakeGateway(self,count=3)
        result=self.sup.process_inbox()[0]
        self.assertFalse(result["ok"]); self.assertIn("binding",result["message"])

    def test_enqueued_authorization_expiry_is_rejected_before_task_start(self):
        bridge=self.enabled_bridge(1); bridge.handle_update(message(534,"/task expires before worker"))
        bridge.handle_update(callback(535,self.budget_tokens()[0]))
        pending_path=next(self.paths.pending_starts_dir.glob("*.json"))
        expires=float(json.loads(pending_path.read_text())["expires_at_unix"])
        self.clock.current=expires+1
        result=self.sup.process_inbox()[0]
        self.assertFalse(result["ok"]); self.assertIn("stale or invalid",result["message"])
        self.assertEqual(json.loads(pending_path.read_text())["status"],"start_enqueued")
        self.assertFalse(self.paths.state_file.exists())

    def test_consuming_authorization_reconciles_after_expiry_without_new_inventory_gate(self):
        bridge=self.enabled_bridge(2); bridge.handle_update(message(536,"/task consuming recovery"))
        bridge.handle_update(callback(537,self.budget_tokens()[1]))
        pending_path=next(self.paths.pending_starts_dir.glob("*.json"))
        record=json.loads(pending_path.read_text())
        record.update({"status":"supervisor_consuming","supervisor_consuming_at":hs.iso_utc(self.clock.current)})
        hs.atomic_write_json(pending_path,record)
        self.clock.current=float(record["expires_at_unix"])+1
        gateway=FakeGateway(self,count=0); self.sup.reset_gateway=gateway
        result=self.sup.process_inbox()[0]
        self.assertTrue(result["ok"])
        self.assertEqual(gateway.calls,[])
        self.assertEqual(self.state()["codex_reset"]["authorized_reset_budget"],1)
        self.assertEqual(json.loads(pending_path.read_text())["status"],"started")

    def test_pending_authorization_bookkeeping_recovers_after_run_was_persisted(self):
        bridge=self.enabled_bridge(2); bridge.handle_update(message(540,"/task recover bookkeeping")); token=self.budget_tokens()[1]
        bridge.handle_update(callback(541,token))
        command_path=next((self.paths.inbox_dir/"pending").glob("*.json")); command=json.loads(command_path.read_text())
        pending_path=self.paths.pending_starts_dir/f"{command['pending_start_id']}.json"
        record=json.loads(pending_path.read_text()); record["status"]="supervisor_consuming"; hs.atomic_write_json(pending_path,record)
        state=self.sup.initialize("recover bookkeeping","codex",workflow_policy="gated_v2",
            codex_reset_authorization=command["codex_reset_authorization"],run_id=command["preallocated_run_id"])
        state["processed_requests"][command["request_id"]]={"request_id":command["request_id"],"action":"task",
            "at":hs.iso_utc(self.clock.current),"notified":False,"ok":True,"message":"started","run_id":state["run_id"]}
        self.sup.store.write_state(state)
        result=self.sup._apply_command_file(command_path)
        self.assertTrue(result["replayed"])
        self.assertEqual(json.loads(pending_path.read_text())["status"],"started")

    # ----- task-start publication race: the authorization must be executable before its command is visible

    def immediate_worker(self,bridge):
        """Fast worker: consumes the queue synchronously inside the bridge's own enqueue call."""
        original=bridge.enqueuer.enqueue_command; results=[]
        def publish(command):
            path=original(command); results.extend(self.sup.process_inbox()); return path
        bridge.enqueuer.enqueue_command=publish
        return results
    def pending_record(self):
        return json.loads(next(self.paths.pending_starts_dir.glob("*.json")).read_text())
    def stage(self,update_id,budget,count=3):
        bridge=self.enabled_bridge(count); bridge.handle_update(message(update_id,"/task race task"))
        return bridge,self.budget_tokens()[budget]

    def test_fast_worker_sees_executable_authorization_and_nothing_downgrades_it(self):
        for budget in (0,1):
            with self.subTest(budget=budget):
                self.reset_fixture(); self.sup.reset_gateway=FakeGateway(self,count=3)
                bridge,token=self.stage(600+budget,budget); results=self.immediate_worker(bridge)
                outcome=bridge.handle_update(callback(610+budget,token))
                self.assertEqual(outcome["result"],"enqueued")
                self.assertEqual(len(results),1); self.assertTrue(results[0]["ok"],results[0])
                record=self.pending_record()
                self.assertEqual(record["status"],"started"); self.assertEqual(record["run_id"],results[0]["run_id"])
                self.assertEqual(self.pending_inbox(),[])
    def test_receipt_is_durable_before_the_command_is_visible(self):
        bridge,token=self.stage(620,1); seen=[]
        original=bridge.enqueuer.enqueue_command
        def inspect(command):
            seen.append(self.pending_record()); return original(command)
        bridge.enqueuer.enqueue_command=inspect
        bridge.handle_update(callback(621,token))
        self.assertEqual([r["status"] for r in seen],["start_enqueued"])
        self.assertEqual((seen[0]["budget"],seen[0]["request_id"]),(1,self.pending_inbox()[0]["request_id"]))
    def test_publication_failure_is_reported_and_the_same_decision_recovers(self):
        bridge,token=self.stage(630,1); original=bridge.enqueuer.enqueue_command; calls=[]
        def flaky(command):
            calls.append(command["request_id"])
            if len(calls)==1: raise OSError("disk full")
            return original(command)
        bridge.enqueuer.enqueue_command=flaky
        failed=bridge.handle_update(callback(631,token))
        self.assertEqual(failed["rejected"],"publication_failed")
        self.assertEqual(self.pending_inbox(),[]); self.assertEqual(self.pending_record()["status"],"start_enqueued")
        self.assertIn("could not be queued",self.texts()[-1]); self.assertNotIn("Task start received",self.texts()[-1])
        self.assertFalse(json.loads((self.tg_paths.interactions_dir/f"{token}.json").read_text())["consumed"])
        recovered=bridge.handle_update(callback(632,token))
        self.assertEqual(recovered["result"],"enqueued"); self.assertEqual(calls[0],calls[1])
        self.assertEqual(len(self.pending_inbox()),1)
        self.sup.reset_gateway=FakeGateway(self,count=3)
        self.assertTrue(self.sup.process_inbox()[0]["ok"]); self.assertEqual(self.pending_record()["status"],"started")
    def test_crash_after_publication_before_token_consumption_republishes_nothing(self):
        bridge,token=self.stage(640,0); original=bridge._consume; calls=[]
        def crashing(*a,**kw):
            calls.append(1)
            if len(calls)==1: raise RuntimeError("bridge died")
            return original(*a,**kw)
        bridge._consume=crashing
        with self.assertRaises(RuntimeError): bridge.handle_update(callback(641,token))
        self.assertEqual(len(self.pending_inbox()),1)
        again=bridge.handle_update(callback(642,token))
        self.assertEqual(again["result"],"enqueued"); self.assertEqual(len(self.pending_inbox()),1)
        self.assertTrue(json.loads((self.tg_paths.interactions_dir/f"{token}.json").read_text())["consumed"])
    def test_replay_after_worker_completed_reconciles_without_new_run_or_prompt(self):
        bridge,token=self.stage(650,0); original=bridge._consume; calls=[]
        def crashing(*a,**kw):
            calls.append(1)
            if len(calls)==1: raise RuntimeError("bridge died")
            return original(*a,**kw)
        bridge._consume=crashing
        with self.assertRaises(RuntimeError): bridge.handle_update(callback(651,token))
        first=self.sup.process_inbox(); self.assertTrue(first[0]["ok"])
        run_id=self.state()["run_id"]; prompts=len(self.herdr.prompts); sent=len(self.texts())
        replay=bridge.handle_update(callback(652,token))
        self.assertEqual(replay["result"],"already_accepted")
        self.assertEqual(self.pending_inbox(),[]); self.assertEqual(self.sup.process_inbox(),[])
        self.assertEqual(self.state()["run_id"],run_id); self.assertEqual(len(self.herdr.prompts),prompts)
        self.assertEqual(self.pending_record()["status"],"started"); self.assertEqual(len(self.texts()),sent)
    def test_committed_authorization_cannot_be_reused_for_another_choice_or_after_expiry(self):
        bridge,token=self.stage(660,1)
        def refuse(command): raise OSError("down")
        bridge.enqueuer.enqueue_command=refuse
        self.assertEqual(bridge.handle_update(callback(661,token))["rejected"],"publication_failed")
        other=self.budget_tokens()[0]
        self.assertEqual(bridge.handle_update(callback(662,other))["rejected"],"stale_pending_start")
        self.assertEqual(bridge.handle_update(message(663,"/reset-budget 2"))["rejected"],"stale_pending_start")
        self.assertIn("rejected",bridge.handle_update(callback(664,token,user=999)))
        self.assertEqual(self.pending_record()["budget"],1)
        self.clock.current=float(self.pending_record()["expires_at_unix"])+1
        self.assertIn("rejected",bridge.handle_update(callback(665,token)))
        self.assertEqual(self.pending_inbox(),[])
    def test_simultaneous_taps_cannot_write_a_stale_copy_over_worker_state(self):
        bridge,token=self.stage(690,1); self.sup.reset_gateway=FakeGateway(self,count=3)
        results=self.immediate_worker(bridge); done=threading.Event(); entered=[]
        def reader():
            entered.append(1)
            if len(entered)==1: done.wait(1.5)  # the first tap loaded `pending` and stalls before committing
            return hcr.ResetInventory(3,True,FP,self.clock.current)
        bridge.reset_inventory_reader=reader
        outcomes=[]
        def tap(update_id):
            outcomes.append(bridge.handle_update(callback(update_id,token)))
            done.set()
        threads=[threading.Thread(target=tap,args=(691+i,)) for i in range(2)]
        threads[0].start()
        while not entered: threading.Event().wait(0.01)
        threads[1].start()
        for thread in threads: thread.join(10)
        self.assertEqual(len(results),1); self.assertTrue(results[0]["ok"],results[0])
        self.assertEqual(self.pending_record()["status"],"started",outcomes)
        self.assertEqual(sorted(o["result"] for o in outcomes),["already_accepted","enqueued"])
        self.assertEqual(self.pending_inbox(),[])
        self.assertEqual(len(list((self.paths.inbox_dir/"completed").glob("*.json"))),1)

    def test_cancelled_or_superseded_pending_start_is_never_resumed(self):
        bridge,token=self.stage(670,0)
        path=next(self.paths.pending_starts_dir.glob("*.json")); record=json.loads(path.read_text())
        for status in ("cancelled","superseded","expired","failed"):
            record["status"]=status; hs.atomic_write_json(path,record)
            self.assertEqual(bridge.handle_update(callback(671,token))["rejected"],"stale_pending_start")
        self.assertEqual(self.pending_inbox(),[])
    def test_shadow_mode_persists_nothing(self):
        bridge,token=self.stage(680,0); self.config["mode"]="shadow"
        bridge.config["mode"]="shadow"
        bridge.handle_update(callback(681,token))
        self.assertEqual(self.pending_record()["status"],"pending"); self.assertEqual(self.pending_inbox(),[])

    def test_reset_card_is_explicitly_a_task_start_action(self):
        bridge=self.enabled_bridge(2); bridge.handle_update(message(530,"/task start wording"))
        card=self.api.sent[-1]; text=card["text"]
        self.assertTrue(text.startswith("Start this task?"), text)
        self.assertIn("Task to start:\nstart wording\n", text, "the typed task is in the message body")
        self.assertLess(text.index("Task to start:"), text.index("Banked resets available"), "task before reset policy")
        self.assertIn("Workflow: Supervised", text)
        self.assertIn("starts this task now as ONE run", text)
        self.assertIn("(0 = none, the default)", text)
        self.assertIn("Full Reset", text)
        self.assertLess(text.count("\n"), 16, "warning stays concise")
        rows=card["reply_markup"]["inline_keyboard"]
        self.assertEqual([[b["text"] for b in row] for row in rows], [["✓ Workflow: Supervised"],["Workflow: Plan approval"],["✓ Start with: Codex"],["Start with: Claude"],["Start · 0","Start · 1"],["Start · 2"],["Preserve sessions (default)"],["Fresh Codex"],["Fresh Claude + Codex"],["Cancel"]])
        buttons={b["text"]:b["callback_data"] for row in rows for b in row}
        # binding checks are unchanged: another actor and a stale inventory are refused, zero starts once
        self.assertIn("rejected",bridge.handle_update(callback(531,buttons["Start · 0"],user=999)))
        self.assertEqual(bridge.handle_update(callback(532,buttons["Start · 0"]))["result"],"enqueued")
        self.assertEqual(self.pending_inbox()[0]["codex_reset_authorization"]["budget"],0)
        self.assertIn("rejected",bridge.handle_update(callback(533,buttons["Start · 2"]))); self.assertEqual(len(self.pending_inbox()),1)

    def test_start_card_layout_across_inventory_sizes_and_long_text(self):
        for count, expected_rows in ((0,[["Start · 0"],["Cancel"]]),(1,[["Start · 0","Start · 1"],["Cancel"]]),(3,[["Start · 0","Start · 1"],["Start · 2","Start · 3"],["Cancel"]]),
                                     (8,[["Start · 0","Start · 1"],["Start · 2","Start · 3"],["Start · 4","Start · 5"],["Start · 6","Start · 7"],["Start · 8"],["Cancel"]]),
                                     (11,[["Start · 0","Start · 1"],["Start · 2","Start · 3"],["Start · 4","Start · 5"],["Start · 6","Start · 7"],["Start · 8"],["Cancel"]])):
            with self.subTest(count=count):
                bridge=self.enabled_bridge(count); bridge.handle_update(message(600+count,f"/task size {count}"))
                card=self.api.sent[-1]
                expected_rows=[["✓ Workflow: Supervised"],["Workflow: Plan approval"],["✓ Start with: Codex"],["Start with: Claude"]]+expected_rows[:-1]+[["Preserve sessions (default)"],["Fresh Codex"],["Fresh Claude + Codex"],expected_rows[-1]]
                self.assertEqual([[b["text"] for b in row] for row in card["reply_markup"]["inline_keyboard"]], expected_rows)
                self.assertTrue(all(len(row)<=2 for row in card["reply_markup"]["inline_keyboard"][:-1]))
                self.assertIn(f"Banked resets available: {count}", card["text"])
                self.assertEqual("/reset-budget N" in card["text"], count>8)
                self.assertEqual(json.loads((self.tg_paths.interactions_dir/f"{card['reply_markup']['inline_keyboard'][4][0]['callback_data']}.json").read_text())["budget"],0)
        # a long typed task is chunked by the normal sender; the keyboard rides only on the final chunk and the
        # callback records never carry the task text
        long_task = "L" * 3900  # within max_task_chars; the card body exceeds one Telegram message
        self.api.sent.clear()
        bridge=self.enabled_bridge(1); bridge.handle_update(message(700,"/task "+long_task))
        cards=[m for m in self.api.sent if "Start this task?" in m["text"] or long_task[:100] in m["text"]]
        self.assertGreaterEqual(len(cards),2)
        self.assertIsNone(cards[0]["reply_markup"]); self.assertIsNotNone(cards[-1]["reply_markup"])
        for record_path in self.tg_paths.interactions_dir.glob("*.json"):
            self.assertNotIn(long_task[:64], record_path.read_text())

if __name__=="__main__": unittest.main()
