"""The CLI dispatch (`herdr_command.main`) end to end with the fake adapter: exit codes, printed status/
doctor/events, control commands, gate commands, runtime evidence, done, migrate, logs, enqueue."""

from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path

import herdr_command  # noqa: E402
from v2_fixtures import SHA_A, V2Case, hs  # noqa: E402


class CliCase(V2Case):
    def setUp(self) -> None:
        super().setUp()
        self.paths.config_file.write_text(json.dumps({
            "schema_version": 1, "herdr_bin": os.sys.executable, "project_root": str(self.project_root), "review_root": str(self.review_root),
            "product_repo": str(self.product_repo), "quota_dir": str(self.quota_dir), "quota_safety_buffer_seconds": 60,
        }))
        self._env = {k: os.environ.get(k) for k in ("HERDR_SUPERVISOR_CONFIG", "HERDR_SUPERVISOR_STATE_DIR")}
        os.environ["HERDR_SUPERVISOR_CONFIG"] = str(self.paths.config_file)
        os.environ["HERDR_SUPERVISOR_STATE_DIR"] = str(self.state_dir)
        case = self

        class FakeCli:  # the CLI adapter factory used by main(); returns this test's scripted fake
            def __new__(cls, binary: str):
                case.herdr.binary = binary
                return case.herdr

        class TestSupervisor(hs.Supervisor):
            def __init__(self, paths, config, herdr, **kwargs):
                super().__init__(paths, config, herdr, clock=case.clock.time, sleeper=case.clock.sleep)
                self.head_resolver = case.head_resolver

        self._originals = (herdr_command.HerdrCli, herdr_command.Supervisor)
        herdr_command.HerdrCli = FakeCli  # type: ignore[misc,assignment]
        herdr_command.Supervisor = TestSupervisor  # type: ignore[misc,assignment]

    def tearDown(self) -> None:
        herdr_command.HerdrCli, herdr_command.Supervisor = self._originals  # type: ignore[misc]
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        super().tearDown()

    def run_cli(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = herdr_command.main(list(argv))
        return code, out.getvalue(), err.getvalue()


class CommandTests(CliCase):
    def test_status_doctor_events_and_logs_without_a_task(self) -> None:
        code, out, _ = self.run_cli("status")
        self.assertEqual(code, 0)
        self.assertIn("Supervisor: NO_TASK", out)
        code, out, _ = self.run_cli("status", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["supervisor_state"], "NO_TASK")
        code, out, _ = self.run_cli("doctor", "--json")
        report = json.loads(out)
        self.assertIn("herdr", report)
        self.assertEqual(report["supervisor_version"], hs.SUPERVISOR_VERSION)
        code, out, _ = self.run_cli("doctor")
        self.assertIn("versions:     supervisor", out)
        self.assertIn("prompt ack:", out)
        code, out, _ = self.run_cli("events")
        self.assertEqual((code, out), (0, ""))
        code, _, err = self.run_cli("logs")
        self.assertEqual(code, 2)
        self.assertIn("no supervised task", err)

    def test_gated_run_through_the_cli(self) -> None:
        self.write_plan()
        self.herdr.responses = [{"v2": ("plan", "human", "plan_approval", str(self.plan_payload()))}]
        code, out, _ = self.run_cli("run", "--policy", "gated_v2", "Add the widget")
        self.assertEqual(code, 4)
        state = self.state()
        gate = state["pending_gate"]
        code, out, _ = self.run_cli("status")
        self.assertIn("WAIT_PLAN_APPROVAL", out)
        self.assertIn("Prompt deliveries prepared: 1", out)
        code, out, _ = self.run_cli("events", "--json")
        self.assertIn("PLAN_APPROVAL_REQUIRED", [e["type"] for e in json.loads(out)])
        code, out, _ = self.run_cli("events")
        self.assertIn("PLAN_APPROVAL_REQUIRED", out)
        code, out, _ = self.run_cli("logs", "--lines", "5")
        self.assertEqual(code, 0)
        self.assertIn("gate_created", out)
        # wrong gate id, then a real revision, reject, and approve through the CLI
        code, _, err = self.run_cli("approve", "--run-id", state["run_id"], "--gate-id", "nope")
        self.assertEqual(code, 2)
        code, out, _ = self.run_cli("revise", "--run-id", state["run_id"], "--gate-id", gate["gate_id"], "tighten scope")
        self.assertEqual(code, 0)
        self.assertIn("revision requested", out)
        self.assertEqual(self.state()["supervisor_state"], "RUNNING")
        self.herdr.responses = [{"v2": ("plan", "human", "plan_approval", str(self.plan_payload()))}]
        self.assertEqual(self.run_cli("resume")[0], 4)
        gate = self.state()["pending_gate"]
        code, out, _ = self.run_cli("approve", "--run-id", self.state()["run_id"], "--gate-id", gate["gate_id"])
        self.assertEqual(code, 0)
        self.assertIn("approved", out)
        # pause/cancel controls without a worker apply directly
        code, out, _ = self.run_cli("pause")
        self.assertEqual(code, 0)
        self.assertIn("paused: applied", out)
        self.assertEqual(self.state()["supervisor_state"], "PAUSED")
        code, out, _ = self.run_cli("cancel")
        self.assertEqual(code, 0)
        self.assertEqual(self.state()["supervisor_state"], "CANCELLED")
        code, _, err = self.run_cli("cancel")
        self.assertEqual(code, 2)
        self.assertIn("already CANCELLED", err)
        code, _, err = self.run_cli("resume")
        self.assertEqual(code, 2)

    def test_runtime_evidence_done_and_question_commands(self) -> None:
        self.write_plan()
        self.herdr.responses = [{"v2": ("plan", "human", "plan_approval", str(self.plan_payload()))}]
        self.run_cli("run", "--policy", "gated_v2", "Add the widget")
        state = self.state()
        self.run_cli("approve", "--run-id", state["run_id"], "--gate-id", state["pending_gate"]["gate_id"])
        self.herdr.responses = [{"v2": ("brief", "claude")}, {"v2": ("implement", "human", "generic_question", str(self.question_payload()))}]
        self.assertEqual(self.run_cli("resume")[0], 2)
        gate = self.state()["pending_gate"]
        code, out, _ = self.run_cli("answer", "--run-id", state["run_id"], "--gate-id", gate["gate_id"], "enum")
        self.assertEqual(code, 0)
        self.herdr.responses = [{"v2": ("implement", "codex")}, {"v2": ("review", "human", "runtime_validation", str(self.runtime_payload(SHA_A)))}]
        self.assertEqual(self.run_cli("resume")[0], 4)
        evidence = self.evidence_file(SHA_A, "FAIL")
        code, out, _ = self.run_cli("runtime-fail", "--run-id", state["run_id"], "--candidate-sha", SHA_A, "--environment", "TEST", "--evidence-file", str(evidence))
        self.assertEqual(code, 0)
        evidence = self.evidence_file(SHA_A, "PASS")
        code, out, _ = self.run_cli("runtime-pass", "--run-id", state["run_id"], "--candidate-sha", SHA_A, "--environment", "TEST", "--evidence-file", str(evidence))
        self.assertEqual(code, 0)
        self.assertEqual(self.state()["supervisor_state"], "WAIT_PUSH_APPROVAL")
        code, _, err = self.run_cli("done", "--run-id", state["run_id"], "--operator-handoff")
        self.assertEqual(code, 2)
        self.assertIn("typed gate is pending", err)
        code, out, _ = self.run_cli("reject", "--run-id", state["run_id"], "--gate-id", self.state()["pending_gate"]["gate_id"], "--note", "not now")
        self.assertEqual(code, 0)

    def test_enqueue_refresh_register_query_and_migrate(self) -> None:
        command_file = Path(self.tmp.name) / "cmd.json"
        command_file.write_text(json.dumps({"request_id": "cli-enqueue-0001", "action": "pause", "run_id": "0bbbbbbb-cccc-4ddd-8eee-ffffffffffff"}))
        code, out, _ = self.run_cli("enqueue", str(command_file))
        self.assertEqual(code, 0)
        self.assertTrue(Path(out.strip()).is_file())
        command_file.write_text("[]")
        self.assertEqual(self.run_cli("enqueue", str(command_file))[0], 2)
        code, out, _ = self.run_cli("refresh-quota", "--run-id", "0bbbbbbb-cccc-4ddd-8eee-ffffffffffff")
        self.assertEqual(code, 0)
        self.assertIn("queued", out)
        code, _, err = self.run_cli("register-query", "--provider", "codex", "--agent-name", "codex-query")
        self.assertEqual(code, 2, "registration needs the explicit read-only acknowledgement")
        code, out, _ = self.run_cli("migrate")
        self.assertEqual(code, 0)
        self.assertIn("no task file", out)
        self.assertEqual(oct(self.state_dir.stat().st_mode & 0o777), "0o700")
        code, _, err = self.run_cli("logs", "--lines", "0")
        self.assertEqual(code, 2)
        self.assertIn("positive", err)
        code, _, err = self.run_cli("run", "--codex-reset-budget", "-1", "task")
        self.assertEqual(code, 2)
