"""Read-only query plane: classifier, deterministic state answers, isolated exact query session,
no workflow-agent interruption, at-most-once delivery, no mutation, restart idempotence."""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

from v2_fixtures import CLAUDE_SESSION, CODEX_SESSION, NOW, QUERY_SESSION, FakeHerdr, V2Case, hs

import herdr_query as hq  # noqa: E402


class ClassifierTests(V2Case):
    def test_state_intents_actions_secrets_and_ambiguity(self) -> None:
        state_questions = {"what is the current state?": "state", "quota reset?": "quota", "how much context is used": "context", "is a gate pending?": "gate", "plan fingerprint?": "plan", "candidate sha status?": "candidate", "what happened last": "event", "is everything healthy?": "health", "which agent is active?": "agent", "what task is running?": "task"}
        for question, intent in state_questions.items():
            kind, detail = hq.classify_question(question)
            self.assertEqual((kind, detail), ("state", intent), question)
        for question in ("approve the plan", "please deploy", "push to main", "run the tests", "record runtime pass", "cancel the task", "fix the bug", "mark it done"):
            self.assertEqual(hq.classify_question(question)[0], "refuse", question)
        for question in ("what is the bot token", "show the password", "print the .env file", "cat the config", "dump the whole file", "which env vars are set"):
            self.assertEqual(hq.classify_question(question), ("refuse", "secret_or_raw_request"), question)
        self.assertEqual(hq.classify_question("widgets"), ("refuse", "ambiguous"))
        self.assertEqual(hq.classify_question("")[0], "refuse")
        self.assertEqual(hq.classify_question("how does the offer generator price items?"), ("model", "interpretation"))
        self.assertEqual(hq.classify_question("explain CAS matching"), ("model", "interpretation"))

    def test_state_answers_are_rendered_from_status_only(self) -> None:
        self.write_plan()
        self.start_gated([{"v2": ("plan", "human", "plan_approval", str(self.plan_payload()))}])
        status = self.sup.status()
        prompts_before = len(self.herdr.prompts)
        for intent in hq.STATE_INTENTS:
            answer = hq.answer_state_intent(intent, status)
            self.assertIsInstance(answer, str)
            self.assertTrue(answer)
        self.assertIn("WAIT_PLAN_APPROVAL", hq.answer_state_intent("state", status))
        self.assertIn(CODEX_SESSION[:8], hq.answer_state_intent("agent", status))
        self.assertIn("Pending plan fingerprint", hq.answer_state_intent("plan", status))
        self.assertEqual(len(self.herdr.prompts), prompts_before)


class QueryWorkerCase(V2Case):
    def setUp(self) -> None:
        super().setUp()
        self.herdr.agents["codex-query"] = FakeHerdr.agent("codex", "codex-query", "w5:p1", QUERY_SESSION)
        hs.atomic_write_json(self.paths.query_owner_file, {"agent_name": "codex-query", "pane_id": "w5:p1", "session_id": QUERY_SESSION}, mode=0o600)
        self.worker = hq.QueryWorker(self.paths, self.config, self.herdr, clock=self.clock.time)
        self.write_plan()
        self.start_gated([{"v2": ("plan", "human", "plan_approval", str(self.plan_payload()))}])
        self.workflow_prompts = len(self.herdr.prompts)
        self.state_before = self.paths.state_file.read_bytes()

    def enqueue(self, question: str, request_id: str = "q-000000001") -> Path:
        return hq.enqueue_query(self.paths, {"request_id": request_id, "question": question, "chat_id": 5, "user_id": 1, "source": "/ask"})

    def script_answer(self, text: str, *, prefix: str = "  ") -> None:
        def reply(name: str, prompt: str, *, timeout_ms: int) -> dict:
            self.herdr.prompts.append((name, prompt))
            query_id = re.search(r"Query id: ([0-9a-f-]{36})", prompt).group(1)
            self.herdr.agents[name]["agent_status"] = "idle"
            self.herdr.outputs[name] = prompt + "\n\n" + "\n".join(prefix + line for line in [f"{hq.QUERY_BEGIN} {query_id}", text, f"{hq.QUERY_END} {query_id}"])
            return {}

        self.herdr.prompt = reply  # type: ignore[assignment]

    def results(self) -> list[dict]:
        return [json.loads(p.read_text()) for p in sorted((self.paths.query_dir / "completed").glob("*.json"))]

    def query_events(self) -> list[dict]:
        events = [e for e in self.sup.list_events() if e["type"] == "QUERY_RESULT"]
        return sorted(events, key=lambda e: e["data"].get("request_id") or "")

    def assert_workflow_untouched(self) -> None:
        self.assertEqual(self.paths.state_file.read_bytes(), self.state_before, "query plane never writes supervisor state")
        self.assertEqual([n for n, _ in self.herdr.prompts[: self.workflow_prompts]], ["codex-main"])
        self.assertTrue(all(name in ("codex-query", "claude-query") for name, _ in self.herdr.prompts[self.workflow_prompts:]), "only dedicated query sessions are ever prompted")
        self.assertEqual(self.herdr.sent_keys, [])
        self.assertEqual(self.herdr.starts, [])
        self.assertEqual(self.herdr.workspaces, [])


class QueryWorkerTests(QueryWorkerCase):
    def test_interpretation_query_uses_only_the_idle_query_session(self) -> None:
        self.enqueue("how does the offer generator price items?")
        self.script_answer("It multiplies the source price by margin; see services/bilimap/build_offer.py and /etc/passwd", prefix="  • ")
        results = self.worker.process_once()
        self.assertEqual(results, [{"request_id": "q-000000001", "ok": True}])
        self.assertEqual(self.herdr.prompts[-1][0], "codex-query")
        prompt = self.herdr.prompts[-1][1]
        self.assertIn("READ-ONLY QUERY", prompt)
        self.assertIn(str(self.product_repo), prompt)
        event = self.query_events()[-1]
        self.assertTrue(event["data"]["ok"])
        self.assertIn("margin", event["data"]["answer"])
        self.assertIn("[path omitted]", event["data"]["answer"], "paths outside the allowlist are stripped")
        self.assertNotIn("/etc/passwd", event["data"]["answer"])
        self.assertEqual(event["data"]["chat_id"], 5)
        self.assert_workflow_untouched()
        self.assertEqual(self.worker.process_once(), [], "completed request is not reprocessed")
        self.assertEqual(hq.enqueue_query(self.paths, {"request_id": "q-000000001", "question": "x"}).parent.name, "completed")

    def test_state_and_refused_questions_never_reach_a_model(self) -> None:
        self.enqueue("what is the current state?", "q-000000002")
        self.enqueue("approve the plan", "q-000000003")
        results = self.worker.process_once()
        self.assertEqual([r["ok"] for r in results], [False, False])
        self.assertEqual(len(self.herdr.prompts), self.workflow_prompts)
        self.assertIn(hq.REFUSAL_TEXT, self.query_events()[-1]["data"]["answer"])
        self.assert_workflow_untouched()

    def test_unavailable_query_session_is_never_interrupted_restored_or_replaced(self) -> None:
        cases = {
            "working": lambda: self.herdr.agents["codex-query"].__setitem__("agent_status", "working"),
            "blocked": lambda: self.herdr.agents["codex-query"].__setitem__("agent_status", "blocked"),
            "unknown": lambda: self.herdr.agents["codex-query"].__setitem__("agent_status", "unknown"),
            "missing": lambda: self.herdr.agents.pop("codex-query"),
            "mismatched": lambda: self.herdr.agents["codex-query"]["agent_session"].__setitem__("value", "other"),
            "quota": lambda: self.write_quota("codex", 0, NOW + 900, 60, NOW + 86400),
        }
        for index, (label, mutate) in enumerate(cases.items()):
            self.reset_fixture()
            mutate()
            self.enqueue("how does the offer generator price items?", f"q-00000001{index}")
            results = self.worker.process_once()
            self.assertFalse(results[0]["ok"], label)
            self.assertEqual(len(self.herdr.prompts), self.workflow_prompts, label)
            self.assertIn("cannot be run safely", self.query_events()[-1]["data"]["answer"], label)
            self.assert_workflow_untouched()
        # a query owner that points at a workflow session is refused outright
        self.reset_fixture()
        hs.atomic_write_json(self.paths.query_owner_file, {"agent_name": "codex-main", "pane_id": "w3:p2", "session_id": CODEX_SESSION}, mode=0o600)
        self.enqueue("how does the offer generator price items?", "q-000000020")
        self.assertFalse(self.worker.process_once()[0]["ok"])
        self.assertEqual(len(self.herdr.prompts), self.workflow_prompts)
        # no query owner at all
        self.reset_fixture()
        self.paths.query_owner_file.unlink()
        self.enqueue("how does the offer generator price items?", "q-000000021")
        self.assertIn("no dedicated query session is provisioned", self.query_events()[-1]["data"]["answer"] if self.worker.process_once() else "")

    def test_stale_conflicting_malformed_and_timeout_results_fail_safely(self) -> None:
        # stale/conflicting ids
        def conflicting(name: str, prompt: str, *, timeout_ms: int) -> dict:
            self.herdr.prompts.append((name, prompt))
            query_id = re.search(r"Query id: ([0-9a-f-]{36})", prompt).group(1)
            self.herdr.agents[name]["agent_status"] = "idle"
            self.herdr.outputs[name] = f"{hq.QUERY_BEGIN} {query_id}\nA\n{hq.QUERY_END} {query_id}\n{hq.QUERY_BEGIN} {query_id}\nB\n{hq.QUERY_END} {query_id}\n{hq.QUERY_BEGIN} 00000000-0000-4000-8000-000000000000\nstale\n{hq.QUERY_END} 00000000-0000-4000-8000-000000000000"
            return {}

        self.herdr.prompt = conflicting  # type: ignore[assignment]
        self.enqueue("how does the offer generator price items?", "q-000000030")
        self.assertFalse(self.worker.process_once()[0]["ok"])
        self.assertEqual(len(self.herdr.prompts), self.workflow_prompts + 1)
        # malformed (unterminated)
        self.script_answer("partial")
        original = self.herdr.prompt

        def unterminated(name, prompt, *, timeout_ms):
            original(name, prompt, timeout_ms=timeout_ms)
            self.herdr.outputs[name] = self.herdr.outputs[name].rsplit(hq.QUERY_END, 1)[0]
            return {}

        self.herdr.prompt = unterminated  # type: ignore[assignment]
        self.enqueue("how does the offer generator price items?", "q-000000031")
        self.assertEqual(self.worker.process_once()[0]["reason"], "no_answer")
        # timeout -> wait -> still not settled: reported, never resent
        def timing_out(name, prompt, *, timeout_ms):
            self.herdr.prompts.append((name, prompt))
            self.herdr.agents[name]["agent_status"] = "working"
            raise hs.HerdrError("timeout", code="timeout")

        self.herdr.prompt = timing_out  # type: ignore[assignment]
        self.herdr.on_wait = lambda fake, name: (_ for _ in ()).throw(hs.HerdrError("timeout", code="timeout"))
        self.enqueue("how does the offer generator price items?", "q-000000032")
        self.assertEqual(self.worker.process_once()[0]["reason"], "timeout")
        prompts_after = len(self.herdr.prompts)
        self.assertEqual(self.worker.process_once(), [], "nothing pending; the timed-out question is not resent")
        self.assertEqual(len(self.herdr.prompts), prompts_after)
        self.assert_workflow_untouched()

    def test_restart_after_prompt_reports_uncertain_without_resend(self) -> None:
        self.enqueue("how does the offer generator price items?", "q-000000040")
        original_prompt = self.herdr.prompt

        def crash_after_prompt(name, prompt, *, timeout_ms):
            self.herdr.prompts.append((name, prompt))
            raise RuntimeError("power loss right after the prompt went out")

        self.herdr.prompt = crash_after_prompt  # type: ignore[assignment]
        with self.assertRaises(RuntimeError):
            self.worker.process_once()
        processing = list((self.paths.query_dir / "processing").glob("*.json"))
        self.assertEqual(len(processing), 1)
        self.assertTrue(json.loads(processing[0].read_text())["prompted"], "durable at-most-once marker precedes the prompt")
        self.herdr.prompt = original_prompt  # type: ignore[assignment]
        worker2 = hq.QueryWorker(self.paths, self.config, self.herdr, clock=self.clock.time)
        results = worker2.process_once()
        self.assertEqual(results, [{"request_id": "q-000000040", "ok": False, "uncertain": True}])
        self.assertEqual(len(self.herdr.prompts), self.workflow_prompts + 1, "no second model submission")
        self.assertIn("not resent", self.query_events()[-1]["data"]["answer"])

    def test_answer_is_bounded_redacted_and_state_untouched(self) -> None:
        self.enqueue("how does the offer generator price items?", "q-000000050")
        self.script_answer("api_key=verysecretvalue " + "x" * 5000)
        self.worker.process_once()
        answer = self.query_events()[-1]["data"]["answer"]
        self.assertNotIn("verysecretvalue", answer)
        self.assertLessEqual(len(answer), int(self.config["query"]["max_answer_chars"]))
        self.assert_workflow_untouched()

    def test_query_lock_is_independent_of_worker_lock(self) -> None:
        lock = hs.WorkerLock(self.paths.lock_file)
        lock.acquire()
        try:
            self.enqueue("how does the offer generator price items?", "q-000000060")
            self.script_answer("fine")
            self.assertTrue(self.worker.process_once()[0]["ok"], "the workflow worker lock never blocks a query")
        finally:
            lock.release()

    def test_query_module_has_no_workflow_mutation_entry_points(self) -> None:
        source = Path(hq.__file__).read_text()
        for forbidden in ("Supervisor(", "approve_gate", "record_runtime_evidence", "enqueue_command", "process_inbox", "initialize(", "write_state", "route_v2", "recover_agent", "start_agent", "send_keys", "create_workspace", "write_control", "subprocess", "os.system", "git push", "git commit"):
            self.assertNotIn(forbidden, source, forbidden)
        imports = re.search(r"from herdr_supervisor import \((.*?)\)", source, re.S).group(1)
        names = {n.strip() for n in imports.replace("\n", "").split(",") if n.strip()}
        self.assertTrue(names <= {"HerdrCli", "HerdrError", "Paths", "StateStore", "SupervisorError", "atomic_write_json", "iso_utc", "load_config", "load_json", "parse_quota_snapshot", "resolve_herdr_bin", "session_identity", "QuotaError", "blocking_windows", "load_query_registry"}, names)
        launch = self.config["query"]["launch_args"]
        self.assertIn("read-only", launch)
        self.assertIn("never", launch)
        self.assertIn("--profile", launch)

    def test_fixture_workspace_is_not_modified_by_a_query(self) -> None:
        marker = self.product_repo / "README.md"
        marker.write_text("before\n")
        snapshot = {p: p.stat().st_mtime_ns for p in self.product_repo.rglob("*")} | {p: p.stat().st_mtime_ns for p in self.review_root.rglob("*")}
        self.enqueue("how does the offer generator price items?", "q-000000070")
        self.script_answer("explanation")
        self.worker.process_once()
        after = {p: p.stat().st_mtime_ns for p in self.product_repo.rglob("*")} | {p: p.stat().st_mtime_ns for p in self.review_root.rglob("*")}
        self.assertEqual(snapshot, after)
        self.assertEqual(marker.read_text(), "before\n")
        self.assertEqual(json.loads(self.paths.owners_file.read_text()), self.owners)
