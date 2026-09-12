"""Fresh Codex identity: supported thread/start parsing, thread-before-pane ordering, exact resumed identity,
ambiguity without retry, crash windows, capability chain, and helper-journal semantics. Fixtures only."""

from __future__ import annotations

import json
import subprocess
import tempfile
import uuid
from pathlib import Path

import herdr_codex_reset as hcr  # noqa: E402
import herdr_sessions  # noqa: E402
from v2_fixtures import CLAUDE_SESSION, CODEX_SESSION, V2Case, hs  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "providers"
THREAD = "0199b3c8-1e2d-7c3a-9f4e-5a6b7c8d9e0f"


def thread_start_response(**overrides) -> dict:
    thread = {"id": THREAD, "cwd": "/work", "ephemeral": False, "cliVersion": "0.154.0", "createdAt": 1789200000, "modelProvider": "openai",
              "preview": "", "projectId": None, "sessionId": THREAD, "source": "cli", "status": {"type": "idle"}, "turns": [], "updatedAt": 1789200000,
              "path": "/never/persisted/rollout.jsonl", "gitInfo": {"branch": "main"}}
    response = {"approvalPolicy": "on-request", "approvalsReviewer": "user", "cwd": "/work", "model": "gpt-5-codex", "modelProvider": "openai",
                "sandbox": {"type": "workspaceWrite"}, "thread": thread, "instructionSources": [], "reasoningEffort": None, "serviceTier": None}
    response.update(overrides)
    return response


class ThreadStartAdapterTests(V2Case):
    def test_params_accept_only_supervisor_owned_values(self) -> None:
        self.assertEqual(hcr.thread_start_params({"cwd": "/work", "model": None, "ephemeral": False}), {"cwd": "/work", "ephemeral": False})
        self.assertEqual(hcr.thread_start_params({"cwd": "/work", "model": "gpt-5-codex", "ephemeral": False}), {"cwd": "/work", "ephemeral": False, "model": "gpt-5-codex"})
        for bad in ({"cwd": "work", "model": None, "ephemeral": False}, {"cwd": "/work", "model": "bad model!", "ephemeral": False},
                    {"cwd": "/work", "model": None, "ephemeral": True}, {"cwd": "/work", "model": None}, {"cwd": "/work", "model": None, "ephemeral": False, "baseInstructions": "x"}):
            with self.subTest(bad=bad), self.assertRaises(hcr.ResetError):
                hcr.thread_start_params(bad)

    def test_response_parser_extracts_only_sanitized_identity(self) -> None:
        parsed = hcr.parse_thread_start(thread_start_response(), requested_cwd="/work", requested_model=None)
        self.assertEqual(parsed, {"thread_id": THREAD, "model": "gpt-5-codex", "model_provider": "openai", "cwd": "/work", "cli_version": "0.154.0", "created_at": 1789200000})
        self.assertNotIn("path", json.dumps(parsed)); self.assertNotIn("gitInfo", json.dumps(parsed))
        parsed = hcr.parse_thread_start(thread_start_response(model="gpt-approved"), requested_cwd="/work", requested_model="gpt-approved")
        self.assertEqual(parsed["model"], "gpt-approved")
        cases = {
            "not object": "x", "no thread": {**thread_start_response(), "thread": None}, "bad id": thread_start_response(thread={**thread_start_response()["thread"], "id": "thread-1"}),
            "ephemeral": thread_start_response(thread={**thread_start_response()["thread"], "ephemeral": True}), "cwd mismatch": thread_start_response(cwd="/elsewhere"),
            "model mismatch": thread_start_response(model="other"), "missing provider": {**thread_start_response(), "modelProvider": None},
            "no provenance": thread_start_response(thread={k: v for k, v in thread_start_response()["thread"].items() if k != "cliVersion"}),
        }
        for label, value in cases.items():
            with self.subTest(label), self.assertRaises(hcr.ResetError):
                hcr.parse_thread_start(value, requested_cwd="/work", requested_model="gpt-approved" if label == "model mismatch" else None)

    def test_client_sends_supported_thread_start_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "methods"; script = Path(tmp) / "fake.py"
            script.write_text(
                "import json,sys\n"
                f"log=open({str(log)!r},'a')\n"
                "for line in sys.stdin:\n"
                "    m=json.loads(line); log.write(json.dumps({'method':m.get('method'),'params':m.get('params')})+'\\n'); log.flush()\n"
                "    if m.get('id') is None: continue\n"
                "    if m['method']=='initialize': print(json.dumps({'id':m['id'],'result':{}}),flush=True)\n"
                f"    elif m['method']=='thread/start': print(json.dumps({{'id':m['id'],'result':json.loads({json.dumps(json.dumps(thread_start_response()))})}}),flush=True)\n"
            )
            client = hcr.AppServerClient([sys_executable(), str(script)], timeout=5)
            result = client.thread_start({"cwd": "/work", "model": None, "ephemeral": False})
            self.assertEqual(result["thread_id"], THREAD)
            calls = [json.loads(line) for line in log.read_text().splitlines()]
            self.assertEqual([c["method"] for c in calls], ["initialize", "initialized", "thread/start"])
            self.assertEqual(calls[-1]["params"], {"cwd": "/work", "ephemeral": False})

    def test_journal_request_binds_thread_start_payload_and_never_replays_an_interrupted_call(self) -> None:
        root = Path(self.tmp.name) / "journal"
        request = hcr.make_request("thread_start", {"cwd": "/work", "model": None, "ephemeral": False}, "thread-req-1")
        self.assertEqual(hcr.validate_request(request, "thread-req-1"), request)
        with self.assertRaises(hcr.ResetError):
            hcr.make_request("thread_start", {"cwd": "work", "model": None, "ephemeral": False}, "thread-req-2")

        class Client:
            calls = 0
            def thread_start(self, payload):
                Client.calls += 1
                return {"thread_id": THREAD, "model": "gpt-5-codex", "model_provider": "openai", "cwd": payload["cwd"], "cli_version": "0.154.0", "created_at": 1}

        # first run: pending -> processing -> result with the thread
        (root / "pending").mkdir(parents=True)
        hcr.atomic_json(root / "pending" / "thread-req-1.json", request)
        self.assertEqual(hcr.process_once(root, Client()), 0)
        result = json.loads((root / "results" / "thread-req-1.json").read_text())
        self.assertEqual((result["ok"], result["thread"]["thread_id"], Client.calls), (True, THREAD, 1))
        # interrupted run: a request found in processing/ after a restart is recorded as uncertain, not resent
        request2 = hcr.make_request("thread_start", {"cwd": "/work", "model": None, "ephemeral": False}, "thread-req-3")
        (root / "processing").mkdir(exist_ok=True)
        hcr.atomic_json(root / "processing" / "thread-req-3.json", request2)
        self.assertEqual(hcr.process_once(root, Client()), 0)
        result = json.loads((root / "results" / "thread-req-3.json").read_text())
        self.assertFalse(result["ok"]); self.assertIn("outcome is unknown", result["error"]); self.assertEqual(Client.calls, 1)
        gateway = hcr.JournalGateway(root, timeout=1, sleeper=self.clock.sleep)
        self.assertEqual(gateway.result_if_present("thread_start", {"cwd": "/work", "model": None, "ephemeral": False}, "thread-req-1")["thread"]["thread_id"], THREAD)
        self.assertIsNone(gateway.result_if_present("thread_start", {"cwd": "/work", "model": None, "ephemeral": False}, "thread-req-9"))
        with self.assertRaises(hcr.ResetError):  # a different payload for the same request id is an identity mismatch
            gateway.result_if_present("thread_start", {"cwd": "/other", "model": None, "ephemeral": False}, "thread-req-1")


def sys_executable() -> str:
    import sys
    return sys.executable


class CapabilityChainTests(V2Case):
    def test_recorded_codex_0_154_0_fixtures_verify_every_component(self) -> None:
        schema = json.loads((FIXTURES / "codex-0.154.0-thread-start-schema.json").read_text())
        self.assertTrue(herdr_sessions.thread_schema_supports_start(schema["ClientRequest"], schema["ThreadStartResponse"]))
        self.assertFalse(herdr_sessions.thread_schema_supports_start({"oneOf": [{"properties": {"method": {"enum": ["thread/resume"]}}}]}, schema["ThreadStartResponse"]))
        self.assertFalse(herdr_sessions.thread_schema_supports_start(schema["ClientRequest"], {"required": ["thread"]}))
        app_help = (FIXTURES / "codex-0.154.0-app-server-help.txt").read_text()
        resume_help = (FIXTURES / "codex-0.154.0-resume-help.txt").read_text()

        def runner(argv, **kwargs):
            if argv[1:3] == ["app-server", "--help"]:
                return subprocess.CompletedProcess(argv, 0, app_help, "")
            if argv[1:3] == ["resume", "--help"]:
                return subprocess.CompletedProcess(argv, 0, resume_help, "")
            if argv[1:3] == ["app-server", "generate-json-schema"]:
                out = Path(argv[argv.index("--out") + 1])
                (out / "v2").mkdir(parents=True)
                (out / "ClientRequest.json").write_text(json.dumps(schema["ClientRequest"]))
                (out / "v2" / "ThreadStartResponse.json").write_text(json.dumps(schema["ThreadStartResponse"]))
                return subprocess.CompletedProcess(argv, 0, "", "")
            raise AssertionError(argv)

        contract = herdr_sessions.probe_codex_thread_contract("codex", runner)
        self.assertEqual(contract, {"codex_app_server": True, "codex_thread_start": True, "codex_resume_session_id": True})
        self.assertEqual(herdr_sessions.probe_codex_thread_contract("codex", lambda argv, **k: subprocess.CompletedProcess(argv, 1, "", "")), {"codex_app_server": False, "codex_thread_start": False, "codex_resume_session_id": False})

    def test_fresh_codex_requires_every_component_and_fresh_claude_only_the_herdr_contract(self) -> None:
        report = self.herdr.capability_report()
        good = {"codex_app_server": True, "codex_thread_start": True, "codex_resume_session_id": True}
        self.assertTrue(herdr_sessions.fresh_policy_supported(self.config, "fresh-codex", report, good))
        self.assertTrue(herdr_sessions.fresh_policy_supported(self.config, "fresh-all", report, good))
        self.assertTrue(herdr_sessions.fresh_policy_supported(self.config, "fresh-claude", report, None))
        self.assertTrue(herdr_sessions.fresh_policy_supported(self.config, "preserve", None, None))
        for missing in good:
            with self.subTest(missing):
                self.assertFalse(herdr_sessions.fresh_policy_supported(self.config, "fresh-codex", report, {**good, missing: False}))
        crippled = json.loads(json.dumps(report)); crippled["required"]["agent get"] = False
        self.assertFalse(herdr_sessions.fresh_policy_supported(self.config, "fresh-codex", crippled, good))
        self.assertFalse(herdr_sessions.fresh_policy_supported(self.config, "fresh-codex", report, None), "static --model support alone never advertises Fresh Codex")
        # preparation refuses before any side effect when a component is missing; doctor names it
        self.codex_contract = {**good, "codex_thread_start": False}
        self.sup = self.make_supervisor()
        with self.assertRaisesRegex(hs.SupervisorError, "codex_thread_start"):
            self.sup.prepare_task_sessions({"policy": "fresh-codex", "profiles": {"codex": "default", "claude": "default"}}, preparation_id=str(uuid.uuid4()))
        self.assertEqual((self.thread_journal.requests, self.herdr.splits, self.herdr.starts), ([], [], []))
        doctor = self.sup.doctor()
        self.assertFalse(doctor["session_start"]["fresh_codex_available"])
        self.assertFalse(doctor["session_start"]["fresh_codex_components"]["codex_thread_start"])
        self.assertTrue(any("codex_thread_start" in w for w in doctor["warnings"]))


class FreshCodexPreparationTests(V2Case):
    SELECTION = {"policy": "fresh-codex", "profiles": {"codex": "default", "claude": "default"}}

    def journal(self, prep: str) -> dict:
        return json.loads((self.paths.session_preparations_dir / f"{prep}.json").read_text())

    def test_thread_is_created_and_persisted_before_the_pane_and_resumed_exactly(self) -> None:
        prep = str(uuid.uuid4())
        seen = []
        original = self.herdr.split_pane
        def split(*args, **kwargs):
            item = self.journal(prep)["providers"]["codex"]
            seen.append((item["status"], item.get("thread_id")))
            return original(*args, **kwargs)
        self.herdr.split_pane = split
        record = self.sup.prepare_task_sessions(self.SELECTION, preparation_id=prep)
        thread_id = record["providers"]["codex"]["thread_id"]
        self.assertEqual(seen, [("PANE_CREATING", thread_id)], "the thread id is durable before the split")
        self.assertEqual(self.thread_journal.requests, [(f"thread-{prep}-codex", {"cwd": str(self.project_root), "model": None, "ephemeral": False})])
        self.assertEqual(self.herdr.starts[0][3], ["resume", thread_id])
        self.assertEqual(self.herdr.starts[0][1], "codex")
        self.assertEqual(record["status"], "OWNERSHIP_BOUND")
        self.assertEqual(self.sup.owners()["codex"], {"pane_id": self.herdr.starts[0][2], "session_id": thread_id})
        self.assertEqual(self.sup.owners()["claude"]["session_id"], CLAUDE_SESSION)
        self.assertEqual(hs.session_identity(self.herdr.agents["codex-main"]), CODEX_SESSION, "the old Codex session is untouched")
        self.assertIn("codex-main", self.herdr.agents)

    def test_fresh_run_delivers_the_task_once_after_binding_to_the_thread(self) -> None:
        observed = []
        def before_prompt(fake, name, text):
            observed.append((json.loads(self.paths.owners_file.read_text())["codex"]["session_id"], hs.session_identity(fake.agents[name])))
        self.herdr.responses = [{"v2": ("plan", "done"), "before": before_prompt}]
        self.assertEqual(self.sup.run_new("fresh codex task", "codex", session_selection=self.SELECTION), 0)
        thread_id = self.thread_journal.results[self.thread_journal.requests[0][0]]["thread"]["thread_id"]
        state = self.state()
        self.assertEqual(state["native_sessions"]["codex"], thread_id)
        self.assertEqual(observed, [(thread_id, thread_id)])
        self.assertEqual(len(self.herdr.prompts), 1)
        self.assertEqual(len(self.thread_journal.requests), 1)
        view = state["session_preparation"]["providers"]["codex"]
        self.assertEqual((view["status"], view["new_session"]), ("IDENTITY_VERIFIED", thread_id[:8]))

    def test_configured_model_goes_into_thread_start_and_a_divergent_report_fails_closed(self) -> None:
        self.config["session_start"]["model_profiles"]["codex"]["large"] = {"label": "Large", "model": "gpt-approved"}
        selection = {"policy": "fresh-codex", "profiles": {"codex": "large", "claude": "default"}}
        self.sup.prepare_task_sessions(selection, preparation_id=str(uuid.uuid4()))
        self.assertEqual(self.thread_journal.requests[0][1]["model"], "gpt-approved")
        self.assertEqual(self.journal(self.thread_journal.requests[0][0].split("-", 1)[1].rsplit("-", 1)[0])["providers"]["codex"]["thread_model"], "gpt-approved")
        self.reset_fixture()
        self.config["session_start"]["model_profiles"]["codex"]["large"] = {"label": "Large", "model": "gpt-approved"}
        self.thread_journal.model_reported = "gpt-other"
        with self.assertRaisesRegex(hs.SupervisorError, "model does not match"):
            self.sup.prepare_task_sessions(selection, preparation_id=str(uuid.uuid4()))
        self.assertEqual((self.herdr.splits, self.herdr.starts, self.sup.owners()), ([], [], self.owners))

    def test_wrong_missing_or_duplicate_resumed_identity_never_binds(self) -> None:
        cases = {
            "no identity": None,
            "different thread": "99999999-9999-4999-8999-999999999999",
            "old session": CODEX_SESSION,
        }
        for label, reported in cases.items():
            with self.subTest(label):
                self.reset_fixture()
                self.herdr.resume_reports = reported
                with self.assertRaisesRegex(hs.SupervisorError, "instead of the pre-created thread|missing or duplicate"):
                    self.sup.prepare_task_sessions(self.SELECTION, preparation_id=str(uuid.uuid4()))
                self.assertEqual(self.sup.owners(), self.owners)
                self.assertEqual(len(self.herdr.starts), 1); self.assertEqual(len(self.thread_journal.requests), 1)
        self.reset_fixture()
        original = self.herdr.start_agent
        def duplicate(name, *, kind, pane_id, args):
            original(name, kind=kind, pane_id=pane_id, args=args)
            self.herdr.agents["dup"] = self.herdr.agent("codex", "codex-dup", "w7:p7", args[1])
        self.herdr.start_agent = duplicate
        with self.assertRaisesRegex(hs.SupervisorError, "missing or duplicate"):
            self.sup.prepare_task_sessions(self.SELECTION, preparation_id=str(uuid.uuid4()))
        self.assertEqual(self.sup.owners(), self.owners)

    def test_ambiguous_thread_creation_is_never_retried_and_needs_explicit_recovery(self) -> None:
        for mode in ("timeout", "error", "malformed"):
            with self.subTest(mode):
                self.reset_fixture()
                self.thread_journal.mode = mode
                prep = str(uuid.uuid4())
                with self.assertRaisesRegex(hs.SupervisorError, "unresolved"):
                    self.sup.prepare_task_sessions(self.SELECTION, preparation_id=prep)
                item = self.journal(prep)["providers"]["codex"]
                self.assertEqual(item["status"], "THREAD_CREATING")
                self.assertEqual(len(self.thread_journal.requests), 1)
                self.assertEqual((self.herdr.splits, self.herdr.starts, self.sup.owners()), ([], [], self.owners))
                # restart / retry of the same preparation: still no second thread/start, still unresolved
                self.thread_journal.mode = "ok"
                with self.assertRaisesRegex(hs.SupervisorError, "unresolved"):
                    self.make_supervisor().prepare_task_sessions(self.SELECTION, preparation_id=prep)
                self.assertEqual(len(self.thread_journal.requests), 1)
                # another task cannot start around it; explicit abandon frees the next sequence, which uses a new request id
                with self.assertRaisesRegex(hs.SupervisorError, "unresolved"):
                    self.sup.prepare_task_sessions(self.SELECTION, preparation_id=str(uuid.uuid4()))
                self.assertEqual(self.sup.abandon_session_preparation(prep)["abandoned"], True)
                new_prep = str(uuid.uuid4())
                self.assertEqual(self.sup.prepare_task_sessions(self.SELECTION, preparation_id=new_prep)["status"], "OWNERSHIP_BOUND")
                self.assertEqual([r[0] for r in self.thread_journal.requests], [f"thread-{prep}-codex", f"thread-{new_prep}-codex"])

    def test_settled_result_after_a_timeout_is_adopted_without_a_second_call(self) -> None:
        self.thread_journal.mode = "timeout_then_settled"
        prep = str(uuid.uuid4())
        with self.assertRaisesRegex(hs.SupervisorError, "unresolved"):
            self.sup.prepare_task_sessions(self.SELECTION, preparation_id=prep)
        self.thread_journal.mode = "ok"
        record = self.make_supervisor().prepare_task_sessions(self.SELECTION, preparation_id=prep)
        self.assertEqual(record["status"], "OWNERSHIP_BOUND")
        self.assertEqual(len(self.thread_journal.requests), 1, "the settled journal result was adopted; nothing was resent")
        self.assertEqual(record["providers"]["codex"]["thread_id"], self.thread_journal.results[f"thread-{prep}-codex"]["thread"]["thread_id"])
        self.assertEqual(self.herdr.starts[0][3], ["resume", record["providers"]["codex"]["thread_id"]])

    def test_crash_windows_reuse_the_persisted_thread_and_create_no_second_one(self) -> None:
        """Crash after the thread is persisted (THREAD_CREATED), after the pane split, after resume, and
        after identity verification: recovery continues from the journal with zero new thread/start calls."""
        for crash_after in ("THREAD_CREATED", "FRESH_PANE_CREATED", "resume_started", "IDENTITY_VERIFIED"):
            with self.subTest(crash_after):
                self.reset_fixture()
                prep = str(uuid.uuid4())
                path = self.paths.session_preparations_dir / f"{prep}.json"
                original_write = hs.atomic_write_json
                original_start = self.herdr.start_agent

                def crashing_write(target, value, **kwargs):
                    original_write(target, value, **kwargs)
                    if target == path and value["providers"]["codex"]["status"] == crash_after:
                        raise RuntimeError("crash")

                def crashing_start(name, *, kind, pane_id, args):
                    original_start(name, kind=kind, pane_id=pane_id, args=args)
                    raise RuntimeError("crash")  # the resume took effect, the process died before observing it
                herdr_sessions.atomic_write_json = crashing_write
                if crash_after == "resume_started":
                    self.herdr.start_agent = crashing_start
                try:
                    with self.assertRaises(RuntimeError):
                        self.sup.prepare_task_sessions(self.SELECTION, preparation_id=prep)
                finally:
                    herdr_sessions.atomic_write_json = original_write
                    self.herdr.start_agent = original_start
                thread_calls = len(self.thread_journal.requests)
                splits, starts = len(self.herdr.splits), len(self.herdr.starts)
                record = self.make_supervisor().prepare_task_sessions(self.SELECTION, preparation_id=prep)
                self.assertEqual(record["status"], "OWNERSHIP_BOUND", crash_after)
                self.assertEqual(len(self.thread_journal.requests), thread_calls, f"{crash_after}: no second thread")
                self.assertEqual(len(self.thread_journal.requests), 1)
                self.assertEqual(len(self.herdr.splits), 1, f"{crash_after}: exactly one split")
                self.assertEqual(len(self.herdr.starts), 1, f"{crash_after}: exactly one resume")
                self.assertEqual(record["providers"]["codex"]["session_id"], record["providers"]["codex"]["thread_id"])
                self.assertEqual(self.sup.owners()["codex"]["session_id"], record["providers"]["codex"]["thread_id"])
                self.assertGreaterEqual((splits, starts), (0, 0))

    def test_journal_validation_rejects_inconsistent_thread_fields(self) -> None:
        prep = str(uuid.uuid4())
        base = {"schema_version": 2, "preparation_id": prep, "status": "SESSION_SELECTION_AUTHORIZED",
                "selection": herdr_sessions.validate_selection(self.config, self.SELECTION), "created_at": "1970-01-12T13:46:40Z", "origin": "supervisor",
                "request_binding": None, "owners_before": json.loads(json.dumps(self.owners))}
        item = {"status": "THREAD_CREATED", "old_pane_id": "w3:p2", "old_session_id": CODEX_SESSION, "profile": "default", "preparation_id": prep,
                "thread_request_id": f"thread-{prep}-codex", "thread_payload": {"cwd": str(self.project_root), "model": None, "ephemeral": False},
                "thread_id": THREAD, "thread_model": "gpt-5-codex", "thread_provider": "openai"}
        herdr_sessions.validate_preparation_record(self.config, {**base, "providers": {"codex": dict(item)}}, prep)
        bad = {
            "wrong request id": {**item, "thread_request_id": "thread-other"},
            "thread id before creation": {**item, "status": "THREAD_CREATING"},
            "missing thread id": {k: v for k, v in item.items() if k != "thread_id"},
            "non-uuid thread": {**item, "thread_id": "abc"},
            "model drift": {**item, "thread_payload": {"cwd": str(self.project_root), "model": "gpt-approved", "ephemeral": False}},
            "pane before thread": {**item, "status": "FRESH_PANE_CREATED"},
            "verified with other identity": {**item, "status": "IDENTITY_VERIFIED", "pane_id": "w9:p1", "agent_name": f"codex-run-{prep[:8]}", "session_id": "99999999-9999-4999-8999-999999999999"},
        }
        for label, value in bad.items():
            with self.subTest(label), self.assertRaises(hs.SupervisorError):
                herdr_sessions.validate_preparation_record(self.config, {**base, "providers": {"codex": value}}, prep)
        claude_item = {"status": "THREAD_CREATED", "old_pane_id": "w3:p1", "old_session_id": CLAUDE_SESSION, "profile": "default", "preparation_id": prep}
        with self.assertRaisesRegex(hs.SupervisorError, "non-thread provider"):
            herdr_sessions.validate_preparation_record(self.config, {**base, "selection": herdr_sessions.validate_selection(self.config, {"policy": "fresh-claude", "profiles": {"codex": "default", "claude": "default"}}), "providers": {"claude": claude_item}}, prep)

    def test_fresh_claude_path_is_unchanged_and_fresh_all_orders_codex_thread_first(self) -> None:
        self.sup.model_flag_supported = lambda provider: True
        record = self.sup.prepare_task_sessions({"policy": "fresh-all", "profiles": {"codex": "default", "claude": "default"}}, preparation_id=str(uuid.uuid4()))
        self.assertEqual(record["status"], "OWNERSHIP_BOUND")
        starts = {kind: args for _, kind, _, args in self.herdr.starts}
        self.assertEqual(starts["codex"][0], "resume"); self.assertEqual(starts["claude"], [])
        self.assertNotIn("thread_id", record["providers"]["claude"])
        self.assertEqual(len(self.thread_journal.requests), 1)
        self.assertEqual(self.sup.owners()["claude"]["session_id"], "55555555-5555-4555-8555-555555555555")


class TelegramStartFailureTests(V2Case):
    def test_worker_start_failure_states_no_delivery_preserved_sessions_and_recovery(self) -> None:
        self.thread_journal.mode = "timeout"
        prep = str(uuid.uuid4())
        command = {"request_id": "start-0001", "action": "task", "task_text": "fresh via telegram", "actor": "telegram:1", "chat_id": 9, "source": "telegram",
                   "preallocated_run_id": prep, "session_selection": {"policy": "fresh-codex", "profiles": {"codex": "default", "claude": "default"}},
                   "codex_reset_authorization": {"budget": 0, "available_count": None, "account_fingerprint": None}}
        with self.sup.store.transaction():
            with self.assertRaises(hs.SupervisorError) as caught:
                self.sup.apply_command(None, command)
        text = str(caught.exception)
        self.assertTrue(text.startswith("No task was delivered."))
        self.assertIn(f"codex {CODEX_SESSION[:8]}", text); self.assertIn(f"claude {CLAUDE_SESSION[:8]}", text)
        self.assertIn(f"abandon-session-start --preparation-id {prep}", text)
        self.assertIn("Preserve sessions", text)
        self.assertNotIn(CODEX_SESSION, text, "only the abbreviated identity is shown")
        self.assertEqual((self.herdr.prompts, self.herdr.splits, self.sup.owners()), ([], [], self.owners))
        self.assertIsNone(self.sup.store.read_state(required=False))
