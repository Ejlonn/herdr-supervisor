"""Release blocker 2: quota-aware, read-only `/ask` provider selection with visible failover."""

from __future__ import annotations

import json
import re
from pathlib import Path

import herdr_query as hq  # noqa: E402
from test_query import QueryWorkerCase
from v2_fixtures import CLAUDE_SESSION, CODEX_SESSION, NOW, QUERY_SESSION, FakeHerdr, hs

CLAUDE_QUERY_SESSION = "0bbbbbbb-cccc-4ddd-8eee-ffffffffffff"


class QueryRegistrationTests(QueryWorkerCase):
    def setUp(self) -> None:
        super().setUp()
        self.paths.query_owner_file.unlink()

    def test_registration_binds_exact_existing_idle_session_and_prepares_spools(self) -> None:
        result = hs.register_query_provider(
            self.paths, self.config, self.herdr,
            provider="codex", agent_name="codex-query", acknowledge_read_only_contract=True,
        )
        self.assertEqual(result["session_id_abbrev"], QUERY_SESSION[:8])
        self.assertEqual(hs.load_query_registry(self.paths)["codex"]["session_id"], QUERY_SESSION)
        self.assertTrue(self.paths.query_dir.is_dir())
        self.assertTrue(self.paths.outbox_dir.is_dir())

    def test_registration_requires_contract_and_rejects_workflow_session(self) -> None:
        with self.assertRaisesRegex(hs.SupervisorError, "acknowledge-read-only-contract"):
            hs.register_query_provider(
                self.paths, self.config, self.herdr,
                provider="codex", agent_name="codex-query", acknowledge_read_only_contract=False,
            )
        self.herdr.agents["codex-query"]["agent_session"]["value"] = CODEX_SESSION
        with self.assertRaisesRegex(hs.SupervisorError, "workflow native session"):
            hs.register_query_provider(
                self.paths, self.config, self.herdr,
                provider="codex", agent_name="codex-query", acknowledge_read_only_contract=True,
            )


class ProviderCase(QueryWorkerCase):
    def setUp(self) -> None:
        super().setUp()
        # replace the legacy owner with a two-provider registry
        self.paths.query_owner_file.unlink()
        self.herdr.agents["claude-query"] = FakeHerdr.agent("claude", "claude-query", "w5:p2", CLAUDE_QUERY_SESSION)
        hs.atomic_write_json(self.paths.query_registry_file, {"schema_version": 1, "providers": {
            "codex": {"agent_name": "codex-query", "pane_id": "w5:p1", "session_id": QUERY_SESSION},
            "claude": {"agent_name": "claude-query", "pane_id": "w5:p2", "session_id": CLAUDE_QUERY_SESSION},
        }}, mode=0o600)
        self.worker = hq.QueryWorker(self.paths, self.config, self.herdr, clock=self.clock.time)

    def script_answer_any(self, text: str) -> None:
        def reply(name: str, prompt: str, *, timeout_ms: int) -> dict:
            self.herdr.prompts.append((name, prompt))
            query_id = re.search(r"Query id: ([0-9a-f-]{36})", prompt).group(1)
            self.herdr.agents[name]["agent_status"] = "idle"
            self.herdr.outputs[name] = f"{hq.QUERY_BEGIN} {query_id}\n{text}\n{hq.QUERY_END} {query_id}"
            return {}

        self.herdr.prompt = reply  # type: ignore[assignment]

    def ask(self, request_id: str = "q-000000101") -> dict:
        self.enqueue("how does the offer generator price items?", request_id)
        self.script_answer_any("an explanation")
        results = self.worker.process_once()
        event = [e for e in self.query_events() if e["data"]["request_id"] == request_id][0]["data"]
        return {"result": results[0], "event": event}

    def block_quota(self, provider: str, reset: float) -> None:
        self.write_quota(provider, 0, reset, 60, NOW + 86400)


class SelectionTests(ProviderCase):
    def test_codex_usable_answers_with_codex(self) -> None:
        out = self.ask()
        self.assertTrue(out["result"]["ok"])
        self.assertEqual(out["event"]["provider"], "codex")
        self.assertFalse(out["event"]["failover"])
        self.assertEqual(self.herdr.prompts[-1][0], "codex-query")
        self.assert_workflow_untouched()

    def test_codex_blocked_claude_usable_fails_over_visibly(self) -> None:
        self.block_quota("codex", NOW + 3600)
        out = self.ask()
        self.assertTrue(out["result"]["ok"])
        self.assertEqual(out["event"]["provider"], "claude")
        self.assertTrue(out["event"]["failover"])
        self.assertEqual(out["event"]["failover_reason"]["reason"], "quota_blocked")
        self.assertEqual(out["event"]["failover_reason"]["resets_at"], NOW + 3600)
        self.assertEqual(self.herdr.prompts[-1][0], "claude-query")
        self.assertIn("READ-ONLY QUERY", self.herdr.prompts[-1][1])
        self.assert_workflow_untouched()

    def test_claude_blocked_codex_usable_answers_with_codex(self) -> None:
        self.block_quota("claude", NOW + 3600)
        out = self.ask()
        self.assertEqual(out["event"]["provider"], "codex")
        self.assertFalse(out["event"]["failover"])

    def test_both_blocked_returns_clear_unavailable(self) -> None:
        self.block_quota("codex", NOW + 3600)
        self.block_quota("claude", NOW + 7200)
        out = self.ask()
        self.assertFalse(out["result"]["ok"])
        self.assertIn("cannot be run safely", out["event"]["answer"])
        self.assertIn("Codex: quota-blocked until", out["event"]["answer"])
        self.assertIn("Claude: quota-blocked until", out["event"]["answer"])
        self.assertEqual(len(self.herdr.prompts), self.workflow_prompts, "no session prompted")
        self.assertIsNone(out["event"]["provider"])

    def test_preferred_busy_is_not_interrupted_and_alternate_answers(self) -> None:
        self.herdr.agents["codex-query"]["agent_status"] = "working"
        out = self.ask()
        self.assertEqual(out["event"]["provider"], "claude")
        self.assertEqual(out["event"]["failover_reason"]["reason"], "busy")
        self.assertEqual([n for n, _ in self.herdr.prompts[self.workflow_prompts:]], ["claude-query"])
        self.assertEqual(self.herdr.sent_keys, [])

    def test_alternate_busy_is_not_interrupted_when_preferred_blocked(self) -> None:
        self.block_quota("codex", NOW + 3600)
        self.herdr.agents["claude-query"]["agent_status"] = "working"
        out = self.ask()
        self.assertFalse(out["result"]["ok"])
        self.assertIn("Claude: query session is busy", out["event"]["answer"])
        self.assertEqual(len(self.herdr.prompts), self.workflow_prompts)

    def test_provider_owning_the_active_supervised_turn_is_skipped(self) -> None:
        # the supervised task is mid-turn on codex (accepted delivery): codex quota must not be competed for
        with self.sup.store.transaction():
            st = self.sup.store.read_state()
            st["supervisor_state"] = "RUNNING"
            st["pending_gate"]["status"] = "superseded"
            st["active_agent"] = "codex"
            st["delivery"] = {"turn_id": "t", "agent": "codex", "kind": "initial", "prompt_sha256": "x", "status": "accepted", "prepared_at": "t"}
            self.sup.store.write_state(st)
        self.state_before = self.paths.state_file.read_bytes()
        out = self.ask()
        self.assertEqual(out["event"]["provider"], "claude")
        self.assertEqual(out["event"]["failover_reason"]["reason"], "owns_active_turn")
        self.assertEqual(self.paths.state_file.read_bytes(), self.state_before)
        self.assertNotIn("codex-main", [n for n, _ in self.herdr.prompts[self.workflow_prompts:]])

    def test_workflow_sessions_are_never_used_even_when_registered(self) -> None:
        hs.atomic_write_json(self.paths.query_registry_file, {"schema_version": 1, "providers": {
            "codex": {"agent_name": "codex-main", "pane_id": "w3:p2", "session_id": CODEX_SESSION},
            "claude": {"agent_name": "claude-main", "pane_id": "w3:p1", "session_id": CLAUDE_SESSION},
        }}, mode=0o600)
        out = self.ask()
        self.assertFalse(out["result"]["ok"])
        self.assertIn("registry entry points at a workflow session", out["event"]["answer"])
        self.assertEqual(len(self.herdr.prompts), self.workflow_prompts)

    def test_deterministic_status_wording_uses_no_llm(self) -> None:
        for question in ("how far are we?", "is it blocked?", "what happens next", "do you need anything from me?", "are we out of quota?", "what are you working on", "when can codex continue?", "why did it stop?"):
            kind, intent = hq.classify_question(question)
            self.assertEqual(kind, "state", question)
        self.enqueue("is it blocked?", "q-000000102")
        self.worker.process_once()
        self.assertEqual(len(self.herdr.prompts), self.workflow_prompts)

    def test_read_only_prompt_contract_and_mutation_refusal_per_provider(self) -> None:
        contracts = self.config["query"]["providers"]
        self.assertIn("read-only", " ".join(contracts["codex"]["launch_args"]))
        self.assertIn("plan", contracts["claude"]["launch_args"])
        for question in ("Implement Card 21", "can you deploy this?", "approve the pending plan", "I want you to push this", "go ahead and restart it"):
            self.enqueue(question, f"q-0000001{abs(hash(question)) % 100:02d}")
        results = self.worker.process_once()
        self.assertTrue(all(not r["ok"] for r in results))
        self.assertEqual(len(self.herdr.prompts), self.workflow_prompts)
        self.assert_workflow_untouched()

    def test_why_stopped_answer_includes_the_deterministic_wait_reason(self) -> None:
        status = self.sup.status()
        status["supervisor_state"] = "WAIT_USER"
        status["wait_user_reason"] = "agent settled without a protocol block"
        kind, intent = hq.classify_question("why did it stop?")
        self.assertEqual((kind, intent), ("state", "event"))
        answer = hq.answer_state_intent(intent, status)
        self.assertIn("settled without a protocol block", answer)
        self.assertEqual(len(self.herdr.prompts), self.workflow_prompts)

    def test_registry_legacy_owner_still_works_and_doctor_reports_providers(self) -> None:
        self.paths.query_registry_file.unlink()
        hs.atomic_write_json(self.paths.query_owner_file, {"agent_name": "codex-query", "pane_id": "w5:p1", "session_id": QUERY_SESSION}, mode=0o600)
        self.assertEqual(set(hs.load_query_registry(self.paths)), {"codex"})
        out = self.ask()
        self.assertEqual(out["event"]["provider"], "codex")
        summary = hs.query_owner_summary(self.paths)
        self.assertTrue(summary["provisioned"])
        self.assertEqual(summary["providers"]["codex"]["session_id_abbrev"], QUERY_SESSION[:8])
        self.assertEqual(hs.query_owner_summary(hs.Paths(config_file=self.paths.config_file, state_dir=Path(self.tmp.name) / "empty")), {"provisioned": False, "providers": {}})

    def test_doctor_and_status_report_each_query_provider_readiness(self) -> None:
        doctor = self.sup.doctor()
        availability = doctor["query_session"]["availability"]
        self.assertEqual(availability["codex"]["status"], "ready")
        self.assertEqual(availability["claude"]["status"], "ready")
        self.herdr.agents["codex-query"]["agent_status"] = "working"
        status = self.sup.status()
        self.assertEqual(status["query_session"]["availability"]["codex"]["status"], "busy")

    def test_query_lock_lives_under_the_query_directory(self) -> None:
        self.assertEqual(self.paths.query_lock_file, self.paths.query_dir / "worker.lock")
        self.ask()
        self.assertTrue(self.paths.query_lock_file.exists())

    def test_stale_or_future_quota_snapshot_is_ineligible(self) -> None:
        for index, fetched in enumerate((NOW - 901, NOW + 61)):
            self.reset_fixture()
            self.script_answer_any("must not run")
            for provider in ("codex", "claude"):
                path = self.quota_dir / self.config["agents"][provider]["quota_file"]
                payload = json.loads(path.read_text())
                payload["fetched_at_unix"] = fetched
                path.write_text(json.dumps(payload))
            self.enqueue("explain the workflow architecture", f"q-stale-{index:08d}")
            result = self.worker.process_once()[0]
            self.assertFalse(result["ok"])
            self.assertIn("quota evidence is unsafe", result["reason"])
            self.assertEqual(len(self.herdr.prompts), self.workflow_prompts)

    def test_accepted_query_that_remains_working_uses_one_lifecycle_wait(self) -> None:
        self.enqueue("explain the workflow architecture", "q-working-00001")

        def prompt(name: str, text: str, *, timeout_ms: int) -> dict:
            self.herdr.prompts.append((name, text))
            self.herdr.agents[name]["agent_status"] = "working"
            self.herdr.outputs[name] = text
            return {}

        def settle(fake: FakeHerdr, name: str) -> None:
            query_id = re.search(r"Query id: ([0-9a-f-]{36})", fake.prompts[-1][1]).group(1)
            fake.agents[name]["agent_status"] = "idle"
            fake.outputs[name] += f"\n{hq.QUERY_BEGIN} {query_id}\nanswer\n{hq.QUERY_END} {query_id}\n"

        self.herdr.prompt = prompt  # type: ignore[assignment]
        self.herdr.on_wait = settle
        self.assertTrue(self.worker.process_once()[0]["ok"])
        self.assertEqual(len(self.herdr.prompts), self.workflow_prompts + 1)
        self.assertEqual(self.herdr.waits[-1], "codex-query")
