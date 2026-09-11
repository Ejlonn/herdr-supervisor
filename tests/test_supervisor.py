"""Mock/fixture tests for herdr-supervisor. No real Herdr, Claude, or Codex is contacted."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = (_ROOT / "src" / "herdr_supervisor.py") if (_ROOT / "src" / "herdr_supervisor.py").exists() else (_ROOT / "herdr_supervisor.py")
if "herdr_supervisor" in sys.modules and getattr(sys.modules["herdr_supervisor"], "__file__", None) == str(MODULE_PATH):
    hs = sys.modules["herdr_supervisor"]  # V2 test modules may have imported the same file already
else:
    SPEC = importlib.util.spec_from_file_location("herdr_supervisor", MODULE_PATH)
    hs = importlib.util.module_from_spec(SPEC)
    assert SPEC.loader is not None
    sys.modules["herdr_supervisor"] = hs
    SPEC.loader.exec_module(hs)

NOW = 1_000_000.0
CODEX_SESSION = "11111111-1111-4111-8111-111111111111"
CLAUDE_SESSION = "22222222-2222-4222-8222-222222222222"


class FakeClock:
    def __init__(self, current: float = NOW) -> None:
        self.current = current
        self.sleeps: list[float] = []

    def time(self) -> float:
        return self.current

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.current += seconds


class FakeHerdr:
    """Scripted Herdr adapter. Each prompt consumes one scripted response."""

    binary = "/fake/herdr"

    def __init__(self) -> None:
        self.agents = {
            "codex-main": self._agent("codex", "codex-main", "w3:p2", CODEX_SESSION),
            "claude-main": self._agent("claude", "claude-main", "w3:p1", CLAUDE_SESSION),
        }
        self.responses: list[dict] = []
        self.prompts: list[tuple[str, str]] = []
        self.outputs = {"codex-main": "", "claude-main": ""}
        self.visible = {"codex-main": "", "claude-main": ""}
        self.refresh_commands: list[list[str]] = []
        self.sent_keys: list[tuple[str, list[str]]] = []
        self.starts: list[tuple[str, str, str, list[str]]] = []
        self.wait_calls = 0
        self.on_wait = None
        self.on_refresh = None

    @staticmethod
    def _agent(provider: str, name: str, pane: str, session: str) -> dict:
        return {"agent": provider, "name": name, "pane_id": pane, "agent_status": "idle", "agent_session": {"value": session}}

    @staticmethod
    def ids(prompt: str) -> tuple[str, str]:
        run = next(line.split("=", 1)[1] for line in prompt.splitlines() if line.startswith("HERDR_RUN="))
        turn = next(line.split("=", 1)[1] for line in prompt.splitlines() if line.startswith("HERDR_TURN="))
        return run, turn

    @staticmethod
    def block(run: str, turn: str, next_agent: str, stage: str = "stage-x", handoff: str = "fixture handoff", prefix: str = "") -> str:
        lines = [
            "HERDR_PROTOCOL=1",
            f"HERDR_RUN={run}",
            f"HERDR_TURN={turn}",
            f"HERDR_STAGE={stage}",
            f"HERDR_NEXT={next_agent}",
            f"HERDR_HANDOFF={handoff}",
        ]
        return "\n".join(prefix + line for line in lines)

    # --- adapter surface used by the supervisor
    def list_agents(self) -> list[dict]:
        return list(self.agents.values())

    def get_agent(self, name: str) -> dict:
        if name not in self.agents:
            raise hs.HerdrError("missing", code="agent_not_found")
        return self.agents[name]

    def prompt(self, name: str, text: str, *, timeout_ms: int) -> dict:
        self.prompts.append((name, text))
        if not self.responses:
            raise AssertionError(f"unexpected prompt to {name}: {text[:80]}")
        response = self.responses.pop(0)
        run, turn = self.ids(text)
        self.agents[name]["agent_status"] = response.get("status", "idle")
        if "output" in response:
            output = response["output"]
        elif "next" in response:
            # Echo of the prompt (as a terminal would show it) followed by the agent's reply.
            output = text + "\n\nagent reply...\n" + self.block(run, turn, response["next"], response.get("stage", "stage-x"), prefix=response.get("prefix", ""))
        else:
            output = text + "\n"
        self.outputs[name] = output
        self.visible[name] = response.get("visible", output[-2000:])
        if "error" in response:
            raise hs.HerdrError(response["error"], code=response["error"])
        return {"result": {"agent": self.agents[name]}}

    def read_agent(self, name: str, *, source: str, lines: int | None) -> str:
        if source == "visible":
            return self.visible[name]
        if self.agents[name]["agent_status"] == "working" and self.agents[name]["agent"] == "claude":
            raise hs.HerdrError("alternate screen", code="agent_not_idle")
        return self.outputs[name]

    def wait(self, name: str, *, timeout_ms: int) -> dict:
        self.wait_calls += 1
        if self.on_wait is not None:
            self.on_wait(self, name)
        return {"result": {"agent": self.agents[name]}}

    def send_keys(self, name: str, keys: list[str]) -> dict:
        self.sent_keys.append((name, keys))
        return {}

    def start_agent(self, name: str, *, kind: str, pane_id: str, args: list[str]) -> dict:
        self.starts.append((name, kind, pane_id, args))
        session = CODEX_SESSION if kind == "codex" else CLAUDE_SESSION
        self.agents[name] = self._agent(kind, name, pane_id, session)
        return {}

    # V2 adapter surface (recovery layer): the recorded pane is available; workspace creation is unused here.
    def pane_available(self, pane_id: str) -> bool:
        return True

    def create_workspace(self, *, label: str, cwd: str) -> str | None:
        raise AssertionError("V1 fixtures never create workspaces")

    def run_command(self, argv: list[str]) -> str:
        self.refresh_commands.append(argv)
        if self.on_refresh is not None:
            self.on_refresh(self)
        return "{}"


def quota_json(five_remaining: float, five_reset: float, week_remaining: float, week_reset: float, *, provider: str, session: str | None = None) -> dict:
    windows = [
        {"kind": "five_hour", "used_percent": 100 - five_remaining, "remaining_percent": five_remaining, "resets_at": five_reset},
        {"kind": "weekly", "used_percent": 100 - week_remaining, "remaining_percent": week_remaining, "resets_at": week_reset},
    ]
    payload = {"provider": provider, "fetched_at_unix": NOW - 60, "windows": windows, "context": {"used_percent": 42.0}}
    if session:
        payload["session_windows"] = {session: json.loads(json.dumps(windows))}
        payload["session_contexts"] = {session: {"used_percent": 8.0}}
    return payload


class SupervisorTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.quota_dir = root / "quota"
        self.quota_dir.mkdir()
        self.state_dir = root / "state"
        self.state_dir.mkdir()
        self.project_root = root / "project"
        self.project_root.mkdir()
        (self.project_root / "AGENTS.md").write_text("# rules\n")
        self.paths = hs.Paths(config_file=root / "config.json", state_dir=self.state_dir)
        self.config = hs.resolve_config_defaults(hs.deep_merge(
            hs.DEFAULT_CONFIG,
            {
                "project_root": str(self.project_root),
                "quota_dir": str(self.quota_dir),
                "quota_safety_buffer_seconds": 60,
                "poll_interval_seconds": 5,
            },
        ))
        self.owners = {"codex": {"pane_id": "w3:p2", "session_id": CODEX_SESSION}, "claude": {"pane_id": "w3:p1", "session_id": CLAUDE_SESSION}}
        hs.atomic_write_json(self.paths.owners_file, self.owners, mode=0o644)
        self.clock = FakeClock()
        self.herdr = FakeHerdr()
        self.write_quota("codex", 80, NOW + 3600, 60, NOW + 86400)
        self.write_quota("claude", 80, NOW + 3600, 60, NOW + 86400)
        self.supervisor = hs.Supervisor(self.paths, self.config, self.herdr, clock=self.clock.time, sleeper=self.clock.sleep)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def write_quota(self, provider: str, five_remaining: float, five_reset: float, week_remaining: float, week_reset: float) -> None:
        name = self.config["agents"][provider]["quota_file"]
        session = CLAUDE_SESSION if provider == "claude" else None
        (self.quota_dir / name).write_text(json.dumps(quota_json(five_remaining, five_reset, week_remaining, week_reset, provider=provider, session=session)))

    def state(self) -> dict:
        return json.loads(self.paths.state_file.read_text())

    def log_events(self) -> list[str]:
        run_id = self.state()["run_id"]
        path = self.paths.logs_dir / f"{run_id}.jsonl"
        return [json.loads(line)["event"] for line in path.read_text().splitlines()]


class RoutingTests(SupervisorTestCase):
    def test_a_codex_to_claude_to_done(self) -> None:
        self.herdr.responses = [
            {"next": "claude", "stage": "plan"},
            {"next": "codex", "stage": "implement", "prefix": "  • "},
            {"next": "done", "stage": "review"},
        ]
        code = self.supervisor.run_new("Build the feature", "codex")
        self.assertEqual(code, 0)
        state = self.state()
        self.assertEqual(state["supervisor_state"], "DONE")
        self.assertEqual([name for name, _ in self.herdr.prompts], ["codex-main", "claude-main", "codex-main"])
        self.assertEqual(state["turns_completed"], 3)
        self.assertEqual(state["last_successful_handoff"]["stage"], "review")
        self.assertEqual(state["native_sessions"], {"codex": CODEX_SESSION, "claude": CLAUDE_SESSION})
        # Second prompt carried the first handoff summary.
        self.assertIn("Handoff from previous agent: fixture handoff", self.herdr.prompts[1][1])
        # owners.json was preserved byte-for-byte semantically.
        self.assertEqual(json.loads(self.paths.owners_file.read_text()), self.owners)

    def test_start_with_claude_is_explicit(self) -> None:
        self.herdr.responses = [{"next": "done"}]
        self.assertEqual(self.supervisor.run_new("task", "claude"), 0)
        self.assertEqual(self.herdr.prompts[0][0], "claude-main")

    def test_route_to_human_enters_wait_user(self) -> None:
        self.herdr.responses = [{"next": "human", "stage": "plan"}]
        self.assertEqual(self.supervisor.run_new("task", "codex"), 2)
        state = self.state()
        self.assertEqual(state["supervisor_state"], "WAIT_USER")
        self.assertIn("routed to human", state["wait_user_reason"])

    def test_b_stale_markers_are_ignored(self) -> None:
        stale_run, stale_turn = "11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"

        # The pane already shows a complete, well-formed block from an earlier task, and the
        # current turn ends without a matching block.
        self.herdr.responses = [{"status": "idle", "output": FakeHerdr.block(stale_run, stale_turn, "done") + "\n\nold turn\n"}]
        code = self.supervisor.run_new("task", "codex")
        self.assertEqual(code, 2)
        state = self.state()
        self.assertEqual(state["supervisor_state"], "WAIT_USER")
        self.assertIn("without a protocol block", state["wait_user_reason"])
        self.assertEqual(len(self.herdr.prompts), 1)

    def test_b2_marker_with_current_run_but_old_turn_is_ignored(self) -> None:
        state_holder: dict = {}

        def first_prompt_output(name: str, text: str) -> str:
            run, turn = FakeHerdr.ids(text)
            state_holder["run"] = run
            return FakeHerdr.block(run, "33333333-3333-4333-8333-333333333333", "done")

        class Fake2(FakeHerdr):
            def prompt(self, name: str, text: str, *, timeout_ms: int) -> dict:
                self.prompts.append((name, text))
                self.agents[name]["agent_status"] = "idle"
                self.outputs[name] = first_prompt_output(name, text)
                self.visible[name] = self.outputs[name]
                return {}

        herdr = Fake2()
        supervisor = hs.Supervisor(self.paths, self.config, herdr, clock=self.clock.time, sleeper=self.clock.sleep)
        self.assertEqual(supervisor.run_new("task", "codex"), 2)
        self.assertEqual(self.state()["supervisor_state"], "WAIT_USER")
        self.assertEqual(len(herdr.prompts), 1)

    def test_echoed_prompt_template_never_matches(self) -> None:
        state = {"run_id": "11111111-1111-4111-8111-111111111111", "task_text": "t", "active_agent": "codex"}
        prompt = self.supervisor.build_prompt(state, "22222222-2222-4222-8222-222222222222", "initial")
        self.assertIsNone(hs.parse_protocol(prompt, state["run_id"], "22222222-2222-4222-8222-222222222222"))
        self.assertEqual(hs.find_protocol_blocks(prompt), [])

    def test_parser_tolerates_terminal_decoration_and_rejects_conflicts(self) -> None:
        run, turn = "11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"
        decorated = textwrap.dedent(
            f"""
            ⏺ Done. Summary follows.
              HERDR_PROTOCOL=1
              HERDR_RUN={run}
              HERDR_TURN={turn}
              HERDR_STAGE=implement
              HERDR_NEXT=codex
              HERDR_HANDOFF=implemented X; tests pass
            """
        )
        block = hs.parse_protocol(decorated, run, turn)
        self.assertIsNotNone(block)
        self.assertEqual((block.stage, block.next_agent, block.handoff), ("implement", "codex", "implemented X; tests pass"))
        conflicting = decorated + "\n" + FakeHerdr.block(run, turn, "done", stage="other")
        with self.assertRaises(hs.SupervisorError):
            hs.parse_protocol(conflicting, run, turn)
        # A block that is not contiguous is not a block.
        broken = decorated.replace("HERDR_STAGE=implement", "note\nHERDR_STAGE=implement")
        self.assertIsNone(hs.parse_protocol(broken, run, turn))


class DeliveryTests(SupervisorTestCase):
    def test_agent_not_found_is_definitively_unsent_and_safe_to_resume(self) -> None:
        self.herdr.responses = [{"error": "agent_not_found", "status": "idle"}]
        self.assertEqual(self.supervisor.run_new("task", "codex"), 2)
        state = self.state()
        self.assertEqual(state["supervisor_state"], "WAIT_USER")
        self.assertIsNone(state["delivery"])
        self.assertFalse(state["wait_user_requires_action"])
        self.assertIn("safe resume", state["wait_user_reason"])
        self.assertIn("prompt_rejected_before_delivery", self.log_events())

    def test_c_timeout_does_not_duplicate_submission(self) -> None:
        # Prompt call times out; the agent is actually working and later completes the same turn.
        herdr = self.herdr

        def finish_turn(fake: FakeHerdr, name: str) -> None:
            run, turn = fake.ids(fake.prompts[-1][1])
            fake.agents[name]["agent_status"] = "idle"
            fake.outputs[name] = fake.block(run, turn, "done")

        herdr.on_wait = finish_turn
        herdr.responses = [{"error": "timeout", "status": "working"}]
        self.assertEqual(self.supervisor.run_new("task", "codex"), 0)
        self.assertEqual(len(herdr.prompts), 1)
        state = self.state()
        self.assertEqual(state["supervisor_state"], "DONE")
        events = self.log_events()
        self.assertIn("prompt_delivery_uncertain", events)
        self.assertIn("prompt_accepted_by_activity", events)
        self.assertEqual(events.count("prompt_prepared"), 1)

    def test_c2_stalled_and_idle_without_block_fails_closed(self) -> None:
        self.herdr.responses = [{"error": "agent_prompt_stalled", "status": "idle"}]
        self.assertEqual(self.supervisor.run_new("task", "codex"), 2)
        self.assertEqual(len(self.herdr.prompts), 1)
        state = self.state()
        self.assertEqual(state["supervisor_state"], "WAIT_USER")
        self.assertEqual(state["delivery"]["status"], "uncertain")
        self.assertIn("not resent", state["wait_user_reason"])

    def test_c3_resume_after_wait_user_does_not_resend(self) -> None:
        self.herdr.responses = [{"error": "agent_prompt_stalled", "status": "idle"}]
        self.supervisor.run_new("task", "codex")
        # The operator resumes without changing anything; still no resend, still WAIT_USER.
        self.assertEqual(self.supervisor.resume(), 2)
        self.assertEqual(len(self.herdr.prompts), 1)
        self.assertEqual(self.state()["supervisor_state"], "WAIT_USER")
        # The agent then finishes the turn (e.g. operator nudged it); resume routes it.
        run, turn = FakeHerdr.ids(self.herdr.prompts[0][1])
        self.herdr.outputs["codex-main"] = FakeHerdr.block(run, turn, "done")
        self.assertEqual(self.supervisor.resume(), 0)
        self.assertEqual(len(self.herdr.prompts), 1)
        self.assertEqual(self.state()["supervisor_state"], "DONE")

    def test_busy_agent_is_never_stacked(self) -> None:
        self.herdr.agents["codex-main"]["agent_status"] = "working"

        def become_idle(fake: FakeHerdr, name: str) -> None:
            fake.agents[name]["agent_status"] = "idle"

        self.herdr.on_wait = become_idle
        self.herdr.responses = [{"next": "done"}]
        self.assertEqual(self.supervisor.run_new("task", "codex"), 0)
        self.assertIn("agent_busy_before_submit", self.log_events())
        self.assertEqual(len(self.herdr.prompts), 1)


class QuotaTests(SupervisorTestCase):
    def test_d_five_hour_exhaustion_waits_until_reset_plus_buffer(self) -> None:
        reset = NOW + 1800
        self.write_quota("codex", 0, reset, 60, NOW + 86400)

        def restore_quota(fake: FakeHerdr) -> None:
            self.write_quota("codex", 100, self.clock.current + 18000, 60, NOW + 86400)

        self.herdr.on_refresh = restore_quota
        self.herdr.responses = [{"next": "done"}]
        self.assertEqual(self.supervisor.run_new("task", "codex"), 0)
        self.assertEqual(len(self.herdr.prompts), 1)
        self.assertGreaterEqual(self.clock.current, reset + 60)
        self.assertGreaterEqual(len(self.herdr.refresh_commands), 1)  # V2 release: periodic non-LLM rechecks may add refreshes
        events = self.log_events()
        self.assertEqual(events.index("quota_wait") < events.index("prompt_prepared"), True)

    def test_e_weekly_exhaustion(self) -> None:
        reset = NOW + 4 * 86400
        self.write_quota("claude", 80, NOW + 3600, 0, reset)
        self.herdr.responses = [{"next": "done"}]
        self.assertEqual(self.supervisor.run_new("task", "claude"), 0)
        self.assertGreaterEqual(self.clock.current, reset + 60)
        # Claude has no independent refresh: no refresh command was invoked.
        self.assertEqual(self.herdr.refresh_commands, [])
        self.assertEqual(len(self.herdr.prompts), 1)

    def test_f_both_windows_wait_for_the_latest(self) -> None:
        five_reset, week_reset = NOW + 1800, NOW + 3 * 86400
        self.write_quota("codex", 0, five_reset, 0, week_reset)
        self.herdr.on_refresh = lambda fake: self.write_quota("codex", 100, self.clock.current + 18000, 100, self.clock.current + 7 * 86400)
        self.herdr.responses = [{"next": "done"}]
        self.assertEqual(self.supervisor.run_new("task", "codex"), 0)
        self.assertGreaterEqual(self.clock.current, week_reset + 60)
        wait_event = [json.loads(l) for l in (self.paths.logs_dir / f"{self.state()['run_id']}.jsonl").read_text().splitlines() if '"quota_wait"' in l][0]
        self.assertEqual(sorted(wait_event["windows"]), ["five_hour", "weekly"])
        self.assertEqual(wait_event["resume_at"], week_reset + 60)

    def test_g_expired_cached_zero_window_does_not_block(self) -> None:
        self.write_quota("claude", 0, NOW - 10, 0, NOW - 5)
        snapshot = self.supervisor.quota("claude")
        self.assertEqual(hs.blocking_windows(snapshot, NOW), ())
        self.herdr.responses = [{"next": "done"}]
        self.assertEqual(self.supervisor.run_new("task", "claude"), 0)
        self.assertEqual(self.clock.sleeps, [])
        self.assertNotIn("quota_wait", self.log_events())

    def test_h_midturn_exhaustion_resumes_same_session_with_continuation(self) -> None:
        reset = NOW + 900
        prompts_seen: list[str] = []

        class MidTurn(FakeHerdr):
            def prompt(self, name: str, text: str, *, timeout_ms: int) -> dict:
                self.prompts.append((name, text))
                prompts_seen.append(text)
                run, turn = self.ids(text)
                if len(self.prompts) == 1:
                    # The turn hit the limit: the agent settles idle with a limit screen, no block.
                    self.agents[name]["agent_status"] = "idle"
                    self.outputs[name] = text + "\n\nYou've hit your usage limit. Try again at 14:00.\n"
                    self.visible[name] = self.outputs[name]
                    return {}
                # Continuation turn completes normally.
                self.agents[name]["agent_status"] = "idle"
                self.outputs[name] = self.block(run, turn, "done")
                self.visible[name] = self.outputs[name]
                return {}

        herdr = MidTurn()
        supervisor = hs.Supervisor(self.paths, self.config, herdr, clock=self.clock.time, sleeper=self.clock.sleep)

        # Snapshot flips to exhausted once the first prompt has gone out.
        original_prompt = herdr.prompt

        def prompt_and_exhaust(name: str, text: str, *, timeout_ms: int) -> dict:
            result = original_prompt(name, text, timeout_ms=timeout_ms)
            if len(herdr.prompts) == 1:
                self.write_quota("codex", 0, reset, 60, NOW + 86400)
            return result

        herdr.prompt = prompt_and_exhaust  # type: ignore[assignment]

        def restore_after_reset(fake: FakeHerdr) -> None:  # a refresh only shows usable quota once the reset passed
            if self.clock.current >= reset:
                self.write_quota("codex", 100, self.clock.current + 18000, 60, NOW + 86400)

        herdr.on_refresh = restore_after_reset
        self.assertEqual(supervisor.run_new("task", "codex"), 0)
        self.assertEqual(len(herdr.prompts), 2)
        self.assertEqual(herdr.prompts[0][0], herdr.prompts[1][0], "same agent, same session")
        self.assertIn("usage-limit wait has ended", prompts_seen[1])
        self.assertIn("continue the interrupted stage", prompts_seen[1])
        first_run, first_turn = FakeHerdr.ids(prompts_seen[0])
        second_run, second_turn = FakeHerdr.ids(prompts_seen[1])
        self.assertEqual(first_run, second_run)
        self.assertNotEqual(first_turn, second_turn)
        self.assertIn(first_turn, prompts_seen[1], "continuation references the interrupted turn")
        self.assertGreaterEqual(self.clock.current, reset + 60)
        state = self.state()
        self.assertEqual(state["supervisor_state"], "DONE")
        self.assertEqual(state["interrupted_delivery"]["turn_id"], first_turn)
        self.assertEqual(state["interrupted_delivery"]["status"], "interrupted")

    def test_h2_midturn_detection_while_working_uses_visible_screen(self) -> None:
        reset = NOW + 600
        herdr = self.herdr

        def on_wait(fake: FakeHerdr, name: str) -> None:
            # While working, the visible screen shows the limit and the snapshot is exhausted.
            fake.visible[name] = "Claude usage limit reached. Your limit will reset at 3pm"
            self.write_quota("claude", 0, reset, 60, NOW + 86400)
            fake.on_wait = None

        herdr.on_wait = on_wait
        herdr.responses = [{"error": "timeout", "status": "working"}]
        # After the wait ends, the agent is idle; the continuation turn completes.
        herdr_original_prompt = herdr.prompt

        def second_prompt(name: str, text: str, *, timeout_ms: int) -> dict:
            if len(herdr.prompts) == 1:
                herdr.agents[name]["agent_status"] = "idle"
                self.write_quota("claude", 100, self.clock.current + 18000, 60, NOW + 86400)
                herdr.responses.append({"next": "done"})
            return herdr_original_prompt(name, text, timeout_ms=timeout_ms)

        herdr.prompt = second_prompt  # type: ignore[assignment]

        # When the wait ends, ensure_agent sees an idle agent with no block => continuation.
        original_sleep = self.clock.sleep

        def sleep_and_idle(seconds: float) -> None:
            original_sleep(seconds)
            if self.clock.current >= reset + 60:
                herdr.agents["claude-main"]["agent_status"] = "idle"
                herdr.outputs["claude-main"] = "limit reached earlier\n"

        self.supervisor.sleeper = sleep_and_idle
        self.assertEqual(self.supervisor.run_new("task", "claude"), 0)
        self.assertEqual(len(herdr.prompts), 2)
        self.assertIn("quota_wait", self.log_events())
        self.assertEqual(self.state()["supervisor_state"], "DONE")

    def test_l_missing_or_corrupt_quota_fails_safely(self) -> None:
        (self.quota_dir / "codex-app-server.json").unlink()
        self.assertEqual(self.supervisor.run_new("task", "codex"), 2)
        state = self.state()
        self.assertEqual(state["supervisor_state"], "WAIT_USER")
        self.assertIn("quota snapshot is missing", state["wait_user_reason"])
        self.assertEqual(self.herdr.prompts, [])
        # corrupt JSON
        (self.quota_dir / "codex-app-server.json").write_text("{not json")
        with self.assertRaises(hs.QuotaError):
            self.supervisor.quota("codex")
        # boolean/truthy coercion is rejected
        bad = quota_json(50, NOW + 10, 50, NOW + 20, provider="codex")
        bad["windows"][0]["remaining_percent"] = True
        (self.quota_dir / "codex-app-server.json").write_text(json.dumps(bad))
        with self.assertRaises(hs.QuotaError):
            self.supervisor.quota("codex")
        bad = quota_json(50, NOW + 10, 50, NOW + 20, provider="codex")
        bad["windows"] = []
        (self.quota_dir / "codex-app-server.json").write_text(json.dumps(bad))
        with self.assertRaises(hs.QuotaError):
            self.supervisor.quota("codex")
        bad = quota_json(50, NOW + 10, 50, NOW + 20, provider="claude")
        (self.quota_dir / "codex-app-server.json").write_text(json.dumps(bad))
        with self.assertRaises(hs.QuotaError):
            self.supervisor.quota("codex")

    def test_claude_snapshot_prefers_owned_session_windows(self) -> None:
        payload = quota_json(80, NOW + 3600, 60, NOW + 86400, provider="claude", session=CLAUDE_SESSION)
        payload["windows"][0]["remaining_percent"] = 0  # top-level says exhausted, our session does not
        (self.quota_dir / "claude-statusline.json").write_text(json.dumps(payload))
        snapshot = self.supervisor.quota("claude")
        self.assertEqual(snapshot.window_source, f"session_windows[{CLAUDE_SESSION}]")
        self.assertEqual(snapshot.windows[0].remaining_percent, 80)
        self.assertEqual(snapshot.context_used_percent, 8.0)

    def test_context_usage_is_informational_only(self) -> None:
        payload = quota_json(80, NOW + 3600, 60, NOW + 86400, provider="codex")
        payload["context"]["used_percent"] = 99.0
        (self.quota_dir / "codex-app-server.json").write_text(json.dumps(payload))
        self.herdr.responses = [{"next": "done"}]
        self.assertEqual(self.supervisor.run_new("task", "codex"), 0)
        self.assertEqual(self.herdr.starts, [])
        self.assertEqual(self.herdr.sent_keys, [])


class BlockedTests(SupervisorTestCase):
    def test_i_non_quota_block_enters_wait_user(self) -> None:
        self.herdr.responses = [{"status": "blocked", "output": "Allow Bash(rm -rf build)? (y/n)"}]
        self.assertEqual(self.supervisor.run_new("task", "claude"), 2)
        state = self.state()
        self.assertEqual(state["supervisor_state"], "WAIT_USER")
        self.assertIn("no key was pressed", state["wait_user_reason"])
        self.assertEqual(self.herdr.sent_keys, [])
        self.assertEqual(len(self.herdr.prompts), 1)

    def test_i2_blocked_with_quota_text_but_healthy_snapshot_still_waits_user(self) -> None:
        self.herdr.responses = [{"status": "blocked", "output": "usage limit reached"}]
        self.assertEqual(self.supervisor.run_new("task", "claude"), 2)
        self.assertEqual(self.state()["supervisor_state"], "WAIT_USER")
        self.assertEqual(self.herdr.sent_keys, [])

    def test_i3_pre_send_blocked_rejection_sends_nothing(self) -> None:
        self.herdr.agents["codex-main"]["agent_status"] = "blocked"
        self.herdr.outputs["codex-main"] = "Approve command? [y/N]"
        self.assertEqual(self.supervisor.run_new("task", "codex"), 2)
        self.assertEqual(self.herdr.prompts, [])
        self.assertEqual(self.state()["supervisor_state"], "WAIT_USER")
        self.assertIsNone(self.state()["delivery"])

    def test_unknown_lifecycle_is_not_success(self) -> None:
        self.herdr.responses = [{"status": "unknown", "output": "???"}]
        self.assertEqual(self.supervisor.run_new("task", "codex"), 2)
        state = self.state()
        self.assertEqual(state["supervisor_state"], "WAIT_USER")
        self.assertIn("unknown", state["wait_user_reason"])

    def test_still_blocked_after_quota_wait_presses_nothing_by_default(self) -> None:
        reset = NOW + 300
        self.herdr.on_refresh = lambda fake: self.write_quota("codex", 100, self.clock.current + 18000, 60, NOW + 86400)
        original_prompt = self.herdr.prompt

        def prompt_then_exhaust(name: str, text: str, *, timeout_ms: int) -> dict:
            result = original_prompt(name, text, timeout_ms=timeout_ms)
            self.write_quota("codex", 0, reset, 60, NOW + 86400)
            return result

        self.herdr.prompt = prompt_then_exhaust  # type: ignore[assignment]
        self.herdr.responses = [{"status": "blocked", "output": "You've hit your usage limit"}]
        self.assertEqual(self.supervisor.run_new("task", "codex"), 2)
        # Blocked was reached after our prompt; midturn wait; agent still blocked after reset.
        self.assertIn("quota_wait", self.log_events())
        self.assertEqual(self.herdr.sent_keys, [])
        self.assertIn("still blocked", self.state()["wait_user_reason"])


class RecoveryTests(SupervisorTestCase):
    def test_j_restart_recovers_state_without_resending(self) -> None:
        herdr = self.herdr

        def die_midturn(fake: FakeHerdr, name: str) -> None:
            raise KeyboardInterrupt

        herdr.on_wait = die_midturn
        herdr.responses = [{"error": "timeout", "status": "working"}]
        self.assertEqual(self.supervisor.run_new("task", "codex"), 3)
        persisted = self.state()
        self.assertEqual(persisted["supervisor_state"], "PAUSED")
        self.assertEqual(persisted["delivery"]["status"], "accepted")
        turn = persisted["delivery"]["turn_id"]
        # New supervisor process, fresh objects, same state dir.
        herdr2 = FakeHerdr()
        herdr2.agents["codex-main"]["agent_status"] = "idle"
        herdr2.outputs["codex-main"] = FakeHerdr.block(persisted["run_id"], turn, "done")
        supervisor2 = hs.Supervisor(self.paths, self.config, herdr2, clock=self.clock.time, sleeper=self.clock.sleep)
        self.assertEqual(supervisor2.resume(), 0)
        self.assertEqual(herdr2.prompts, [])
        state = self.state()
        self.assertEqual(state["supervisor_state"], "DONE")
        self.assertEqual(state["run_id"], persisted["run_id"])
        self.assertIn("delivery_recovered_uncertain", self.log_events())

    def test_j2_restart_during_quota_wait_keeps_waiting(self) -> None:
        reset = NOW + 3000
        self.write_quota("codex", 0, reset, 60, NOW + 86400)
        # Simulate a crash during the wait: write state as WAIT_QUOTA directly.
        state = self.supervisor.initialize("task", "codex")
        blocking = hs.blocking_windows(self.supervisor.quota("codex"), NOW)
        state["supervisor_state"] = "WAIT_QUOTA"
        state["quota_wait"] = {"provider": "codex", "midturn": False, "blocking_windows": [hs.dataclasses.asdict(w) for w in blocking], "resume_at": reset + 60}
        self.supervisor.store.write_state(state)
        herdr2 = FakeHerdr()
        herdr2.on_refresh = lambda fake: self.write_quota("codex", 100, self.clock.current + 18000, 60, NOW + 86400)
        herdr2.responses = [{"next": "done"}]
        supervisor2 = hs.Supervisor(self.paths, self.config, herdr2, clock=self.clock.time, sleeper=self.clock.sleep)
        self.assertEqual(supervisor2.resume(), 0)
        self.assertGreaterEqual(self.clock.current, reset + 60)
        self.assertEqual(len(herdr2.prompts), 1)
        self.assertIn("quota_wait_recovered", self.log_events())

    def test_j3_prepared_but_unsent_delivery_fails_closed_on_restart(self) -> None:
        state = self.supervisor.initialize("task", "codex")
        self.supervisor._new_delivery(state, "initial")  # process dies before prompt()
        herdr2 = FakeHerdr()
        supervisor2 = hs.Supervisor(self.paths, self.config, herdr2, clock=self.clock.time, sleeper=self.clock.sleep)
        self.assertEqual(supervisor2.resume(), 2)
        self.assertEqual(herdr2.prompts, [])
        self.assertEqual(self.state()["supervisor_state"], "WAIT_USER")

    def test_missing_agent_is_restored_in_same_pane_with_native_session(self) -> None:
        del self.herdr.agents["codex-main"]
        self.herdr.responses = [{"next": "done"}]
        # run_new verifies live agents first and must refuse when one is missing.
        self.assertIn("required live agent is missing", self.state_error())
        # But an in-flight task whose agent disappears is restored, not recreated.
        self.herdr.agents["codex-main"] = FakeHerdr._agent("codex", "codex-main", "w3:p2", CODEX_SESSION)
        state = self.supervisor.initialize("task", "codex")
        del self.herdr.agents["codex-main"]
        agent = self.supervisor.ensure_agent("codex")
        self.assertEqual(self.herdr.starts, [("codex-main", "codex", "w3:p2", ["resume", CODEX_SESSION])])
        self.assertEqual(hs.session_identity(agent), CODEX_SESSION)

    def state_error(self) -> str:
        try:
            self.supervisor.run_new("task", "codex")
        except hs.SupervisorError as error:
            return str(error)
        return ""

    def test_session_mismatch_is_refused(self) -> None:
        self.herdr.agents["codex-main"]["agent_session"]["value"] = "different-session"
        with self.assertRaises(hs.SupervisorError) as caught:
            self.supervisor.run_new("task", "codex")
        self.assertIn("differs from recorded", str(caught.exception))
        self.assertEqual(json.loads(self.paths.owners_file.read_text()), self.owners)

    def test_pause_and_cancel_preserve_sessions(self) -> None:
        self.herdr.responses = [{"next": "claude"}]
        # Pause is requested before the second turn is submitted.
        original_route = self.supervisor.route

        def route_then_pause(state: dict, block: hs.ProtocolBlock) -> str:
            result = original_route(state, block)
            self.supervisor.store.write_control("paused")
            return result

        self.supervisor.route = route_then_pause  # type: ignore[assignment]
        self.assertEqual(self.supervisor.run_new("task", "codex"), 3)
        self.assertEqual(self.state()["supervisor_state"], "PAUSED")
        self.assertEqual(len(self.herdr.prompts), 1)
        hs.request_control(self.supervisor, "cancelled")
        state = self.state()
        self.assertEqual(state["supervisor_state"], "CANCELLED")
        self.assertIn("preserved", state["cancel_note"])
        self.assertEqual(self.herdr.starts, [])


class ReviewRegressionTests(SupervisorTestCase):
    """Regressions for CODEX_REVIEW.md findings 1-6."""

    def test_r1_blocked_agent_with_valid_current_turn_block_is_not_routed(self) -> None:
        class BlockedWithMarker(FakeHerdr):
            def prompt(self, name: str, text: str, *, timeout_ms: int) -> dict:
                self.prompts.append((name, text))
                run, turn = self.ids(text)
                self.agents[name]["agent_status"] = "blocked"
                self.outputs[name] = self.block(run, turn, "done") + "\n\nAllow Bash(rm -rf build)? (y/n)\n"
                self.visible[name] = self.outputs[name]
                return {}

        herdr = BlockedWithMarker()
        supervisor = hs.Supervisor(self.paths, self.config, herdr, clock=self.clock.time, sleeper=self.clock.sleep)
        self.assertEqual(supervisor.run_new("task", "codex"), 2)
        state = self.state()
        self.assertEqual(state["supervisor_state"], "WAIT_USER")
        self.assertIn("no key was pressed", state["wait_user_reason"])
        self.assertEqual(state["turns_completed"], 0)
        self.assertIsNone(state["last_successful_handoff"])
        self.assertEqual(herdr.sent_keys, [])

    def test_r2_resume_restores_missing_active_agent_in_recorded_pane(self) -> None:
        state = self.supervisor.initialize("task", "codex")
        state["supervisor_state"] = "PAUSED"
        self.supervisor.store.write_state(state)
        herdr2 = FakeHerdr()
        del herdr2.agents["codex-main"]
        herdr2.responses = [{"next": "done"}]
        supervisor2 = hs.Supervisor(self.paths, self.config, herdr2, clock=self.clock.time, sleeper=self.clock.sleep)
        self.assertEqual(supervisor2.resume(), 0)
        self.assertEqual(herdr2.starts, [("codex-main", "codex", "w3:p2", ["resume", CODEX_SESSION])])
        self.assertEqual(len(herdr2.prompts), 1)
        self.assertEqual(self.state()["supervisor_state"], "DONE")

    def test_r2b_resume_refuses_persisted_session_mismatch(self) -> None:
        state = self.supervisor.initialize("task", "codex")
        state["supervisor_state"] = "PAUSED"
        state["native_sessions"]["codex"] = "another-conversation"
        self.supervisor.store.write_state(state)
        herdr2 = FakeHerdr()
        supervisor2 = hs.Supervisor(self.paths, self.config, herdr2, clock=self.clock.time, sleeper=self.clock.sleep)
        with self.assertRaises(hs.SupervisorError) as caught:
            supervisor2.resume()
        self.assertIn("refusing to adopt a different conversation", str(caught.exception))
        self.assertEqual(herdr2.prompts, [])
        self.assertEqual(herdr2.starts, [])
        self.assertEqual(self.state()["supervisor_state"], "PAUSED")

    def test_r2c_resume_restored_agent_with_wrong_session_fails_closed(self) -> None:
        state = self.supervisor.initialize("task", "codex")
        state["supervisor_state"] = "PAUSED"
        self.supervisor.store.write_state(state)

        class WrongRestore(FakeHerdr):
            def start_agent(self, name: str, *, kind: str, pane_id: str, args: list[str]) -> dict:
                self.starts.append((name, kind, pane_id, args))
                self.agents[name] = self._agent(kind, name, pane_id, "fresh-unrelated-session")
                return {}

        herdr2 = WrongRestore()
        del herdr2.agents["codex-main"]
        supervisor2 = hs.Supervisor(self.paths, self.config, herdr2, clock=self.clock.time, sleeper=self.clock.sleep)
        self.assertEqual(supervisor2.resume(), 2)
        self.assertEqual(herdr2.prompts, [])
        self.assertEqual(self.state()["supervisor_state"], "WAIT_USER")

    def test_r3_session_local_quota_never_borrows_other_windows(self) -> None:
        path = self.quota_dir / "claude-statusline.json"
        payload = quota_json(77, NOW + 3600, 60, NOW + 86400, provider="claude", session="other-session")
        payload["session_quota_only"] = True
        path.write_text(json.dumps(payload))
        with self.assertRaises(hs.QuotaError) as caught:
            self.supervisor.quota("claude")
        self.assertIn("no windows for session", str(caught.exception))
        # session-local without any known session id also fails closed
        with self.assertRaises(hs.QuotaError):
            hs.parse_quota_snapshot(path, "claude", session_id=None)
        # keyed maps present but unknown session: no top-level fallback even when not session-only
        payload["session_quota_only"] = False
        path.write_text(json.dumps(payload))
        with self.assertRaises(hs.QuotaError):
            self.supervisor.quota("claude")
        # scope mapping is honoured before session_windows
        payload = quota_json(50, NOW + 3600, 50, NOW + 86400, provider="claude", session=CLAUDE_SESSION)
        payload["session_quota_only"] = False
        payload["session_quota_scopes"] = {CLAUDE_SESSION: "scope-a"}
        payload["quota_scope_windows"] = {"scope-a": [{"kind": "five_hour", "remaining_percent": 12, "resets_at": NOW + 99}]}
        path.write_text(json.dumps(payload))
        snapshot = self.supervisor.quota("claude")
        self.assertEqual(snapshot.window_source, "quota_scope_windows[scope-a]")
        self.assertEqual(snapshot.windows[0].remaining_percent, 12)
        # the real Claude shape (session_quota_only=true with owned session) still parses
        payload = quota_json(80, NOW + 3600, 60, NOW + 86400, provider="claude", session=CLAUDE_SESSION)
        payload["session_quota_only"] = True
        path.write_text(json.dumps(payload))
        self.assertEqual(self.supervisor.quota("claude").window_source, f"session_windows[{CLAUDE_SESSION}]")
        # Codex account-level shape (no keyed maps) uses top-level windows
        self.assertEqual(self.supervisor.quota("codex").window_source, "windows")
        # run fails closed on the session-local mismatch
        payload = quota_json(77, NOW + 3600, 60, NOW + 86400, provider="claude", session="other-session")
        payload["session_quota_only"] = True
        path.write_text(json.dumps(payload))
        self.assertEqual(self.supervisor.run_new("task", "claude"), 2)
        self.assertEqual(self.herdr.prompts, [])
        self.assertEqual(self.state()["supervisor_state"], "WAIT_USER")

    def test_r4_failed_codex_refresh_enters_wait_user_without_prompting(self) -> None:
        reset = NOW + 600
        self.write_quota("codex", 0, reset, 60, NOW + 86400)

        def failing_refresh(fake: FakeHerdr) -> None:
            raise hs.HerdrError("plugin action failed", code="command_error")

        self.herdr.on_refresh = failing_refresh
        self.herdr.responses = [{"next": "done"}]
        self.assertEqual(self.supervisor.run_new("task", "codex"), 2)
        self.assertEqual(self.herdr.prompts, [])
        state = self.state()
        self.assertEqual(state["supervisor_state"], "WAIT_USER")
        self.assertIn("quota refresh failed", state["wait_user_reason"])
        self.assertGreaterEqual(self.clock.current, reset + 60)
        events = self.log_events()
        self.assertIn("codex_quota_refresh_failed", events)
        self.assertNotIn("prompt_prepared", events)

    def test_r5_lock_probe_does_not_rewrite_lock_file(self) -> None:
        self.paths.lock_file.write_text("stale-pid\n")
        self.assertFalse(hs.WorkerLock.is_held(self.paths.lock_file))
        self.assertEqual(self.paths.lock_file.read_text(), "stale-pid\n")
        self.supervisor.status()
        self.supervisor.doctor()
        self.assertEqual(self.paths.lock_file.read_text(), "stale-pid\n")
        self.assertFalse(hs.WorkerLock.is_held(Path(self.tmp.name) / "absent.lock"))
        self.assertFalse((Path(self.tmp.name) / "absent.lock").exists())

    def test_r6_task_file_reference_is_persisted(self) -> None:
        task_file = Path(self.tmp.name) / "task.md"
        task_file.write_text("Build the thing\n")
        self.herdr.responses = [{"next": "done"}]
        self.assertEqual(self.supervisor.run_new(task_file.read_text(), "codex", task_reference=str(task_file)), 0)
        state = self.state()
        self.assertEqual(state["task_reference"], str(task_file))
        self.assertEqual(state["task_text"], "Build the thing\n")
        self.assertIn("Build the thing", self.herdr.prompts[0][1])
        self.assertEqual(self.supervisor.status()["task_reference"], str(task_file))

    def test_r6b_cli_run_loads_task_file_and_keeps_reference(self) -> None:
        task_file = Path(self.tmp.name) / "task.md"
        task_file.write_text("CLI task\n")
        captured: dict = {}

        class Recorder(hs.Supervisor):
            def run_new(self, task: str, start: str, *, task_reference: str | None = None, **_: object) -> int:
                captured.update(task=task, start=start, task_reference=task_reference)
                return 0

        original = hs.Supervisor
        hs.Supervisor = Recorder  # type: ignore[misc]
        try:
            self.paths.config_file.write_text(json.dumps({"schema_version": 1, "herdr_bin": sys.executable}))
            os.environ["HERDR_SUPERVISOR_CONFIG"] = str(self.paths.config_file)
            os.environ["HERDR_SUPERVISOR_STATE_DIR"] = str(self.state_dir)
            self.assertEqual(hs.main(["run", "--start", "claude", str(task_file)]), 0)
        finally:
            hs.Supervisor = original  # type: ignore[misc]
            os.environ.pop("HERDR_SUPERVISOR_CONFIG", None)
            os.environ.pop("HERDR_SUPERVISOR_STATE_DIR", None)
        self.assertEqual(captured, {"task": "CLI task\n", "start": "claude", "task_reference": str(task_file.resolve())})


    def test_r7_non_finite_and_out_of_range_quota_numbers_fail_closed(self) -> None:
        path = self.quota_dir / "codex-app-server.json"
        cases = [
            ("remaining NaN", lambda d: d["windows"][0].__setitem__("remaining_percent", float("nan"))),
            ("remaining inf", lambda d: d["windows"][0].__setitem__("remaining_percent", float("inf"))),
            ("remaining 101", lambda d: d["windows"][0].__setitem__("remaining_percent", 101)),
            ("remaining -1", lambda d: d["windows"][1].__setitem__("remaining_percent", -1)),
            ("resets NaN", lambda d: d["windows"][0].__setitem__("resets_at", float("nan"))),
            ("resets inf", lambda d: d["windows"][0].__setitem__("resets_at", float("inf"))),
            ("resets year>9999", lambda d: d["windows"][0].__setitem__("resets_at", hs.MAX_EPOCH + 1)),
            ("resets zero", lambda d: d["windows"][0].__setitem__("resets_at", 0)),
            ("fetched NaN", lambda d: d.__setitem__("fetched_at_unix", float("nan"))),
            ("fetched huge", lambda d: d.__setitem__("fetched_at_unix", 1e30)),
        ]
        for label, mutate in cases:
            payload = quota_json(0, NOW + 3600, 50, NOW + 86400, provider="codex")
            mutate(payload)
            path.write_text(json.dumps(payload))  # json.dumps emits NaN/Infinity, which json.load accepts
            with self.assertRaises(hs.QuotaError, msg=label):
                self.supervisor.quota("codex")
        # An exhausted window whose sibling carries NaN must not run: fail closed before any prompt.
        payload = quota_json(0, NOW + 3600, 50, NOW + 86400, provider="codex")
        payload["windows"][1]["remaining_percent"] = float("nan")
        path.write_text(json.dumps(payload))
        self.herdr.responses = [{"next": "done"}]
        self.assertEqual(self.supervisor.run_new("task", "codex"), 2)
        self.assertEqual(self.herdr.prompts, [])
        self.assertEqual(self.state()["supervisor_state"], "WAIT_USER")
        # Boundary values remain valid.
        payload = quota_json(0, NOW + 3600, 100, NOW + 86400, provider="codex")
        path.write_text(json.dumps(payload))
        snapshot = self.supervisor.quota("codex")
        self.assertEqual([w.remaining_percent for w in snapshot.windows], [0.0, 100.0])
        self.assertEqual(hs._number(True), None)
        self.assertEqual(hs._number(float("nan")), None)
        self.assertEqual(hs._number(3), 3.0)

    def test_r7b_non_finite_config_numbers_are_rejected(self) -> None:
        path = Path(self.tmp.name) / "cfg.json"
        for key in ("quota_safety_buffer_seconds", "poll_interval_seconds", "prompt_wait_timeout_ms", "wait_timeout_ms", "read_lines"):
            for bad in (float("nan"), float("inf"), -float("inf"), -1, True, "5", None, 1e30):
                path.write_text(json.dumps({"schema_version": 1, key: bad}))
                with self.assertRaises(hs.SupervisorError, msg=f"{key}={bad!r}"):
                    hs.load_config(path)
        for key in ("read_lines", "prompt_wait_timeout_ms", "wait_timeout_ms"):
            path.write_text(json.dumps({"schema_version": 1, key: 0}))
            with self.assertRaises(hs.SupervisorError, msg=f"{key}=0"):
                hs.load_config(path)
        path.write_text(json.dumps({"schema_version": 1, "quota_safety_buffer_seconds": 0, "poll_interval_seconds": 0.5}))
        config = hs.load_config(path)
        self.assertEqual(config["quota_safety_buffer_seconds"], 0)
        self.assertEqual(config["poll_interval_seconds"], 0.5)


class LockTests(SupervisorTestCase):
    def test_k_second_worker_is_refused_before_prompting(self) -> None:
        env = dict(os.environ, HERDR_SUPERVISOR_STATE_DIR=str(self.state_dir), HERDR_SUPERVISOR_CONFIG=str(self.paths.config_file))
        holder = subprocess.Popen(
            [
                sys.executable,
                "-c",
                textwrap.dedent(
                    f"""
                    import fcntl, sys, time
                    h = open({str(self.paths.lock_file)!r}, "a+")
                    fcntl.flock(h.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    print("locked", flush=True)
                    time.sleep(30)
                    """
                ),
            ],
            stdout=subprocess.PIPE,
            text=True,
            env=env,
        )
        try:
            assert holder.stdout is not None
            self.assertEqual(holder.stdout.readline().strip(), "locked")
            self.herdr.responses = [{"next": "done"}]
            with self.assertRaises(hs.SupervisorError) as caught:
                self.supervisor.run_new("task", "codex")
            self.assertIn("another herdr-supervisor worker", str(caught.exception))
            self.assertEqual(self.herdr.prompts, [])
            self.assertFalse(self.paths.state_file.exists())
            self.assertTrue(hs.WorkerLock.is_held(self.paths.lock_file))
        finally:
            holder.kill()
            holder.wait()
            if holder.stdout is not None:
                holder.stdout.close()
        self.assertFalse(hs.WorkerLock.is_held(self.paths.lock_file))

    def test_run_refuses_while_a_task_is_active(self) -> None:
        self.herdr.responses = [{"next": "human"}]
        self.supervisor.run_new("task one", "codex")
        with self.assertRaises(hs.SupervisorError) as caught:
            self.supervisor.run_new("task two", "codex")
        self.assertIn("still WAIT_USER", str(caught.exception))


class ReportingTests(SupervisorTestCase):
    def test_doctor_and_status_reports(self) -> None:
        report = self.supervisor.doctor()
        self.assertTrue(report["ok"], report["errors"])
        self.assertTrue(report["agents"]["codex"]["session_matches"])
        self.assertTrue(report["agents"]["claude"]["session_matches"])
        self.assertTrue(report["quota"]["claude"]["ok"])
        self.assertIn("resets_at_local", report["quota"]["claude"]["windows"][0])
        status = self.supervisor.status()
        self.assertEqual(status["supervisor_state"], "NO_TASK")
        del self.herdr.agents["claude-main"]
        report = self.supervisor.doctor()
        self.assertFalse(report["ok"])
        self.assertIn("claude-main is not detected by herdr", report["errors"])

    def test_config_validation(self) -> None:
        path = Path(self.tmp.name) / "cfg.json"
        path.write_text(json.dumps({"schema_version": 1, "agents": {"codex": {"name": 5}}}))
        with self.assertRaises(hs.SupervisorError):
            hs.load_config(path)
        path.write_text(json.dumps({"schema_version": 1, "quota_safety_buffer_seconds": -1}))
        with self.assertRaises(hs.SupervisorError):
            hs.load_config(path)
        path.write_text(json.dumps({"schema_version": 1, "quota_screen_patterns": {"codex": ["("]}}))
        with self.assertRaises(hs.SupervisorError):
            hs.load_config(path)
        path.write_text(json.dumps({"schema_version": 1, "quota_safety_buffer_seconds": 30}))
        self.assertEqual(hs.load_config(path)["quota_safety_buffer_seconds"], 30)
        self.assertEqual(hs.load_config(Path(self.tmp.name) / "absent.json")["schema_version"], 1)

    def test_atomic_write_and_command_error_code(self) -> None:
        target = Path(self.tmp.name) / "nested" / "x.json"
        hs.atomic_write_json(target, {"a": 1})
        self.assertEqual(json.loads(target.read_text()), {"a": 1})
        self.assertEqual([p.name for p in target.parent.iterdir()], ["x.json"])
        self.assertEqual(hs.command_error_code('{"error":{"code":"agent_not_found","message":"x"},"id":"cli"}'), "agent_not_found")
        self.assertEqual(hs.command_error_code("garbage agent_prompt_stalled"), "agent_prompt_stalled")
        self.assertEqual(hs.command_error_code("nothing"), "command_error")


if __name__ == "__main__":
    unittest.main()
