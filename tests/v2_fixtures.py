"""Shared V2 fixtures: temporary XDG roots, fake Herdr, realistic review artifacts. No live systems."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
LIB = _ROOT / "src" if (_ROOT / "src" / "herdr_supervisor.py").exists() else _ROOT  # source tree or installed layout
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

import herdr_supervisor as hs  # noqa: E402

NOW = 1_000_000.0
CODEX_SESSION = "11111111-1111-4111-8111-111111111111"
CLAUDE_SESSION = "22222222-2222-4222-8222-222222222222"
QUERY_SESSION = "33333333-3333-4333-8333-333333333333"
FAKE_TOKEN = "123456789:AAFakeTokenForTestsOnly_abcdefghijklmnopqrs"
SHA_A = "a" * 40
SHA_B = "b" * 40


class FakeClock:
    def __init__(self, current: float = NOW) -> None:
        self.current = current
        self.sleeps: list[float] = []

    def time(self) -> float:
        return self.current

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.current += seconds


_RESUMED = object()

import herdr_codex_reset as hcr  # noqa: E402


class FakeThreadJournal:
    """Scripted helper journal for the app-server operations the supervisor issues during a fresh Codex
    start. Inventory/consume go to the real on-disk JournalGateway of the fixture (those tests script their
    own gateway); thread/start is scripted here and records every request exactly once."""

    def __init__(self, case: "V2Case") -> None:
        self.case = case
        self.real = hcr.JournalGateway(case.paths.codex_reset_dir, timeout=1, sleeper=case.clock.sleep)
        self.requests: list[tuple[str, dict]] = []
        self.results: dict[str, dict] = {}
        self.next_thread_ids = ["44444444-4444-4444-8444-444444444444", "66666666-6666-4666-8666-666666666666", "77777777-7777-4777-8777-777777777777"]
        self.created = 0
        self.mode = "ok"  # ok | timeout | malformed | error | timeout_then_settled
        self.model_reported: str | None = None  # override the returned model (mismatch tests)

    def inventory(self, request_id: str):
        return self.real.inventory(request_id)

    def consume(self, key: str, request_id: str):
        return self.real.consume(key, request_id)

    def _make(self, payload: dict) -> dict:
        hcr.thread_start_params(payload)
        self.created += 1
        thread_id = self.next_thread_ids.pop(0) if self.next_thread_ids else f"{self.created:08d}-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        return {"thread_id": thread_id, "model": self.model_reported or payload.get("model") or "gpt-5-codex", "model_provider": "openai",
                "cwd": payload["cwd"], "cli_version": "0.154.0", "created_at": int(self.case.clock.current)}

    def thread_start(self, payload: dict, request_id: str) -> dict:
        self.requests.append((request_id, dict(payload)))
        if self.mode == "error":
            self.results[request_id] = {"request_id": request_id, "ok": False, "error": "helper failed"}
            raise hcr.ResetError("helper failed")
        if self.mode in ("timeout", "timeout_then_settled"):
            if self.mode == "timeout_then_settled":
                self.results[request_id] = {"request_id": request_id, "ok": True, "thread": self._make(payload)}
            raise hcr.ResetError("reset-helper result timed out; operation is uncertain")
        if self.mode == "malformed":
            raise hcr.ResetError("thread-start returned no canonical thread id")
        thread = self._make(payload)
        self.results[request_id] = {"request_id": request_id, "ok": True, "thread": thread}
        return thread

    def result_if_present(self, operation: str, payload: dict, request_id: str) -> dict | None:
        hcr.make_request(operation, payload, request_id)
        return self.results.get(request_id)


class FakeHerdr:
    """Scripted adapter. `responses` is consumed per prompt: {'v2': (stage, next, gate, payload)} or
    {'v1': next} or {'output': ..., 'status': ...} or {'error': code}."""

    binary = "/fake/herdr"

    def __init__(self) -> None:
        self.agents: dict[str, dict] = {
            "codex-main": self.agent("codex", "codex-main", "w3:p2", CODEX_SESSION),
            "claude-main": self.agent("claude", "claude-main", "w3:p1", CLAUDE_SESSION),
        }
        self.responses: list[dict] = []
        self.prompts: list[tuple[str, str]] = []
        self.reads: list[tuple[str, str]] = []
        self.waits: list[str] = []
        self.outputs: dict[str, str] = {}
        self.visible: dict[str, str] = {}
        self.sent_keys: list = []
        self.starts: list = []
        self.workspaces: list = []
        self.splits: list = []
        self.available_panes: set[str] = {"w3:p1", "w3:p2"}
        self.next_pane = "w9:p1"
        self.next_sessions = {"codex": "44444444-4444-4444-8444-444444444444", "claude": "55555555-5555-4555-8555-555555555555"}
        self.resume_reports: object = _RESUMED  # _RESUMED = report the resumed thread id; None = no identity; a str = that id
        self.refresh_commands: list = []
        self.on_wait = None
        self.on_refresh = None
        # Lifecycle acknowledgement (herdr >= 0.9 `--wait --until working --until blocked`). Set
        # ack_supported=False to exercise the settlement fallback.
        self.ack_supported = True
        self.ack_calls: list[tuple[str, int]] = []
        self.help_probes = 0
        self.targets: list[tuple[str, str]] = []  # (command, raw TARGET argument) for identity assertions

    @staticmethod
    def agent(provider: str, name: str, pane: str, session: str, status: str = "idle") -> dict:
        return {"agent": provider, "name": name, "pane_id": pane, "agent_status": status, "agent_session": {"value": session}}

    @staticmethod
    def ids(prompt: str) -> tuple[str, str]:
        run = next(l.split("=", 1)[1] for l in prompt.splitlines() if l.startswith("HERDR_RUN="))
        turn = next(l.split("=", 1)[1] for l in prompt.splitlines() if l.startswith(("HERDR_TURN=", "HERDR_FOLLOWUP_TURN=")))
        return run, turn

    @staticmethod
    def prompt_field(prompt: str, key: str) -> str:
        return next(l.split("=", 1)[1] for l in prompt.splitlines() if l.startswith(key + "="))

    @staticmethod
    def followup_frame(run: str, turn: str, decision: str, path: str, sha: str, summary: str = "answered_no_change_needed", prefix: str = "  ") -> str:
        lines = ["HERDR_FOLLOWUP=1", f"HERDR_RUN={run}", f"HERDR_FOLLOWUP_TURN={turn}", f"HERDR_DECISION={decision}", f"HERDR_RESPONSE={path}", f"HERDR_RESPONSE_SHA256={sha}", f"HERDR_SUMMARY={summary}"]
        return "\n".join(prefix + line for line in lines)

    @classmethod
    def followup_reply(cls, prompt: str, spec: dict) -> str:
        """Scripted follow-up answer: writes the Markdown response where the prompt asked (unless `path`
        overrides it, `skip_file` leaves it missing, or `sha` lies) and returns the frame text."""
        run, turn = cls.ids(prompt)
        path = spec.get("path") or cls.prompt_field(prompt, "HERDR_RESPONSE")
        decision = spec.get("decision") or cls.prompt_field(prompt, "HERDR_DECISION")
        content = spec.get("content", "# Answer\n\nThe widget plan is fine.\n\n# Recommendation\n\nApprove.\n\n# Change needed\n\nno\n\n# Next operator action\n\nApprove the plan.\n")
        raw = content.encode("utf-8")
        if not spec.get("skip_file"):
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).write_bytes(raw)
        import hashlib  # noqa: PLC0415

        sha = spec.get("sha") or hashlib.sha256(raw).hexdigest()
        return cls.followup_frame(run, turn, decision, path, sha, summary=spec.get("summary", "answered_no_change_needed"), prefix=spec.get("prefix", "  "))

    @staticmethod
    def block_v2(run: str, turn: str, stage: str, next_agent: str, gate: str = "none", payload: str = "-", handoff: str = "fixture handoff", prefix: str = "  ") -> str:
        lines = ["HERDR_PROTOCOL=2", f"HERDR_RUN={run}", f"HERDR_TURN={turn}", f"HERDR_STAGE={stage}", f"HERDR_NEXT={next_agent}", f"HERDR_GATE={gate}", f"HERDR_PAYLOAD={payload}", f"HERDR_HANDOFF={handoff}"]
        return "\n".join(prefix + line for line in lines)

    @staticmethod
    def block_v1(run: str, turn: str, next_agent: str, stage: str = "stage") -> str:
        return "\n".join(["HERDR_PROTOCOL=1", f"HERDR_RUN={run}", f"HERDR_TURN={turn}", f"HERDR_STAGE={stage}", f"HERDR_NEXT={next_agent}", "HERDR_HANDOFF=fixture handoff"])

    def list_agents(self) -> list[dict]:
        return list(self.agents.values())

    def capability_report(self) -> dict:
        self.help_probes += 1
        return {"detected_version":"0.9.0","minimum_version":"0.9.0","version_ok":True,"version_detail":"fixture",
                "required":{"agent start (--kind/--pane)":True,"agent get":True,"agent list":True},"missing_capabilities":[],"optional":{"prompt lifecycle acknowledgement (--until + agent_prompt_stalled)":self.ack_supported,
                "task-start pane split (--direction/--ratio/--cwd/--no-focus)":True},"compatible":True,"prompt_ack_mode":"lifecycle" if self.ack_supported else "settle"}

    def _resolve(self, target: str, command: str) -> str:
        """Herdr accepts an agent name or a pane id as TARGET. Records the raw target per command and
        returns the key of the fake's agent record (its name, or the pane id for a nameless record)."""
        self.targets.append((command, target))
        if target in self.agents:
            return target
        for key, agent in self.agents.items():
            if agent.get("pane_id") == target:
                return key
        raise hs.HerdrError("missing", code="agent_not_found")

    def get_agent(self, name: str) -> dict:
        return self.agents[self._resolve(name, "get")]

    def prompt(self, name: str, text: str, *, timeout_ms: int) -> dict:
        name = self._resolve(name, "prompt")
        self.prompts.append((name, text))
        if not self.responses:
            raise AssertionError(f"unexpected prompt to {name}: {text[:100]}")
        response = self.responses.pop(0)
        if callable(response.get("before")):
            response["before"](self, name, text)
        run, turn = self.ids(text)
        self.agents[name]["agent_status"] = response.get("status", "idle")
        if "output" in response:
            output = response["output"]
        elif "followup" in response:
            output = text + "\n\nreply\n" + self.followup_reply(text, response["followup"])
        elif "v2" in response:
            stage, nxt, gate, payload = (list(response["v2"]) + ["none", "-"])[:4]
            output = text + "\n\nreply\n" + self.block_v2(run, turn, stage, nxt, gate, payload, prefix=response.get("prefix", "  "))
        elif "v1" in response:
            output = text + "\n\nreply\n" + self.block_v1(run, turn, response["v1"])
        else:
            output = text + "\n"
        self.outputs[name] = output
        self.visible[name] = response.get("visible", output[-2000:])
        if "error" in response:
            raise hs.HerdrError(response["error"], code=response["error"])
        return {"result": {"agent": self.agents[name]}}

    def prompt_ack_supported(self) -> bool:
        self.help_probes += 1
        return self.ack_supported

    def prompt_ack(self, name: str, text: str, *, timeout_ms: int) -> dict:
        """Same scripted submission as prompt(); the acknowledgement reports the observed lifecycle
        (response key `ack_status`, default working) instead of the settled state."""
        self.ack_calls.append((name, timeout_ms))
        head = self.responses[0] if self.responses else None
        ack_status = head.get("ack_status", "working") if isinstance(head, dict) else "working"
        result = self.prompt(name, text, timeout_ms=timeout_ms)
        agent = (result.get("result") or {}).get("agent") if isinstance(result, dict) else None
        return {"result": {"agent": {**(agent or self.agents.get(name) or {}), "agent_status": ack_status}}}

    def read_agent(self, name: str, *, source: str, lines: int | None) -> str:
        name = self._resolve(name, "read")
        self.reads.append((name, source))
        if source == "visible":
            return self.visible.get(name, "")
        if self.agents[name]["agent_status"] == "working" and self.agents[name]["agent"] == "claude":
            raise hs.HerdrError("alternate screen", code="agent_not_idle")
        return self.outputs.get(name, "")

    def wait(self, name: str, *, timeout_ms: int) -> dict:
        name = self._resolve(name, "wait")
        self.waits.append(name)
        if self.on_wait is not None:
            self.on_wait(self, name)
        return {}

    def send_keys(self, name: str, keys: list[str]) -> dict:
        name = self._resolve(name, "send-keys")
        self.sent_keys.append((name, keys))
        return {}

    def start_agent(self, name: str, *, kind: str, pane_id: str, args: list[str]) -> dict:
        self.starts.append((name, kind, pane_id, args))
        if len(args) >= 2 and args[0] == "resume":
            # `codex resume <thread-id>`: Herdr observes exactly the resumed thread as the native session,
            # unless a test scripts a divergent report (missing identity, wrong thread).
            session = args[1] if self.resume_reports is _RESUMED else self.resume_reports
        else:
            session = self.next_sessions[kind] if "-run-" in name else {"codex":CODEX_SESSION,"claude":CLAUDE_SESSION}[kind]
        self.agents[name] = self.agent(kind, name, pane_id, session)
        if session is None:
            self.agents[name].pop("agent_session", None)
        return {}

    def split_pane(self, pane_id: str, *, direction: str, ratio: float, cwd: str) -> str | None:
        self.splits.append((pane_id,direction,ratio,cwd))
        pane=self.next_pane
        self.next_pane=f"w9:p{len(self.splits)+1}"
        self.available_panes.add(pane)
        return pane

    def pane_available(self, pane_id: str) -> bool:
        return pane_id in self.available_panes

    def create_workspace(self, *, label: str, cwd: str) -> str | None:
        self.workspaces.append((label, cwd))
        self.available_panes.add(self.next_pane)
        return self.next_pane

    def run_command(self, argv: list[str]) -> str:
        self.refresh_commands.append(argv)
        if self.on_refresh is not None:
            self.on_refresh(self)
        return "{}"


def quota_json(five: float, five_reset: float, week: float, week_reset: float, *, provider: str, session: str | None = None) -> dict:
    windows = [
        {"kind": "five_hour", "used_percent": 100 - five, "remaining_percent": five, "resets_at": five_reset},
        {"kind": "weekly", "used_percent": 100 - week, "remaining_percent": week, "resets_at": week_reset},
    ]
    payload: dict = {"provider": provider, "fetched_at_unix": NOW - 60, "windows": windows, "context": {"used_percent": 42.0}, "session_quota_only": session is not None}
    if session:
        payload["session_windows"] = {session: json.loads(json.dumps(windows))}
        payload["session_contexts"] = {session: {"used_percent": 8.0}}
    return payload


PLAN_TEXT = "# CODEX_PLAN\n\nStatus: draft\n\n## Scope\nAdd the widget.\n"


class V2Case(unittest.TestCase):
    """Temporary XDG roots for supervisor + review root + product repo stub; a fake Herdr."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        # Isolation: never read the machine's real Telegram config/state from any test (the live
        # bridge may be configured on the host); restored in tearDown.
        self._saved_env = {k: os.environ.get(k) for k in ("HERDR_TELEGRAM_CONFIG", "HERDR_TELEGRAM_STATE_DIR")}
        os.environ["HERDR_TELEGRAM_CONFIG"] = str(root / "tg-absent" / "config.json")
        os.environ["HERDR_TELEGRAM_STATE_DIR"] = str(root / "tg-absent-state")
        self.quota_dir = root / "quota"
        self.quota_dir.mkdir()
        self.state_dir = root / "state"
        self.state_dir.mkdir(mode=0o700)
        self.project_root = root / "workspace"
        self.project_root.mkdir()
        (self.project_root / "AGENTS.md").write_text("# rules\n")
        self.review_root = self.project_root / "engineering" / "reviews"
        self.review_root.mkdir(parents=True)
        self.review_dir = self.review_root / "feature-widget"
        self.review_dir.mkdir()
        self.product_repo = self.project_root / "product"
        self.product_repo.mkdir()
        self.paths = hs.Paths(config_file=root / "config.json", state_dir=self.state_dir)
        self.config = hs.resolve_config_defaults(hs.deep_merge(hs.DEFAULT_CONFIG, {
            "project_root": str(self.project_root),
            "quota_dir": str(self.quota_dir),
            "review_root": str(self.review_root),
            "product_repo": str(self.product_repo),
            "quota_safety_buffer_seconds": 60,
            "poll_interval_seconds": 5,
            "query": {"allowed_paths": [str(self.product_repo), str(self.project_root / "AGENTS.md"), str(self.review_root)], "launch_args": ["--sandbox", "read-only", "--ask-for-approval", "never", "--profile", "herdr-query", "-C", str(self.project_root)]},
        }))
        self.owners = {"codex": {"pane_id": "w3:p2", "session_id": CODEX_SESSION}, "claude": {"pane_id": "w3:p1", "session_id": CLAUDE_SESSION}}
        hs.atomic_write_json(self.paths.owners_file, self.owners, mode=0o600)
        self.clock = FakeClock()
        self.herdr = FakeHerdr()
        self.write_quota("codex", 80, NOW + 3600, 60, NOW + 86400)
        self.write_quota("claude", 80, NOW + 3600, 60, NOW + 86400)
        self.head = SHA_A
        self.head_resolver = lambda repo: self.head
        self.thread_journal = FakeThreadJournal(self)
        self.sup = self.make_supervisor()

    def reset_fixture(self) -> None:
        """Replace this test's temporary fixture without leaking the previous TemporaryDirectory."""
        self.tearDown()
        self.setUp()

    def make_supervisor(self) -> "hs.Supervisor":
        sup = hs.Supervisor(self.paths, self.config, self.herdr, clock=self.clock.time, sleeper=self.clock.sleep)
        sup.head_resolver = self.head_resolver
        # Fresh Codex fixtures: the thread journal is shared across supervisors of one case (restart tests)
        # and the read-only Codex thread contract is verified unless a test says otherwise.
        sup.reset_gateway = self.thread_journal
        sup._codex_thread_contract = dict(getattr(self, "codex_contract", {"codex_app_server": True, "codex_thread_start": True, "codex_resume_session_id": True}))
        return sup

    def tearDown(self) -> None:
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()

    def write_quota(self, provider: str, five: float, five_reset: float, week: float, week_reset: float) -> None:
        name = self.config["agents"][provider]["quota_file"]
        session = CLAUDE_SESSION if provider == "claude" else None
        (self.quota_dir / name).write_text(json.dumps(quota_json(five, five_reset, week, week_reset, provider=provider, session=session)))

    def write_quota_fresh(self, provider: str, five: float, five_reset: float, week: float, week_reset: float, *, fetched: float | None = None) -> None:
        name = self.config["agents"][provider]["quota_file"]
        session = CLAUDE_SESSION if provider == "claude" else None
        payload = quota_json(five, five_reset, week, week_reset, provider=provider, session=session)
        payload["fetched_at_unix"] = self.clock.current if fetched is None else fetched
        (self.quota_dir / name).write_text(json.dumps(payload))

    def state(self) -> dict:
        return json.loads(self.paths.state_file.read_text())

    def events(self) -> list[dict]:
        return self.sup.list_events()

    def event_types(self) -> list[str]:
        return [e["type"] for e in self.events()]

    # ----- review artifacts
    def write_plan(self, text: str = PLAN_TEXT) -> Path:
        path = self.review_dir / "CODEX_PLAN.md"
        path.write_text(text)
        return path

    def plan_payload(self, **overrides) -> Path:
        payload = {
            "schema_version": 1, "gate_type": "plan_approval", "task_title": "Add widget", "summary": "Plan for the widget",
            "review_directory": str(self.review_dir), "scope": "backend only", "intended_changes": ["add widget model", "add endpoint"],
            "risk_summary": "low", "affected_components": ["backend"], "migration_required": False, "migration_explanation": "",
            "runtime_validation_required": True, "rebuild_required": True, "rebuild_reason": "backend image contains the source",
            "push_approval_required": True, "plan_path": str(self.review_dir / "CODEX_PLAN.md"),
        }
        payload.update(overrides)
        path = self.review_dir / "plan_payload.json"
        path.write_text(json.dumps(payload))
        return path

    def question_payload(self, **overrides) -> Path:
        payload = {"schema_version": 1, "gate_type": "generic_question", "task_title": "Add widget", "summary": "need a decision", "review_directory": str(self.review_dir), "question": "Use Postgres enum or text?", "answer_mode": "choice", "choices": ["enum", "text"], "max_answer_chars": 200}
        payload.update(overrides)
        path = self.review_dir / "question_payload.json"
        path.write_text(json.dumps(payload))
        return path

    def runtime_payload(self, sha: str = SHA_A, **overrides) -> Path:
        payload = {
            "schema_version": 1, "gate_type": "runtime_validation", "task_title": "Add widget", "summary": "local work complete", "review_directory": str(self.review_dir),
            "repository": str(self.product_repo), "candidate_sha": sha, "prepared_commits": [f"{sha[:12]} add widget"], "affected_services": ["backend"],
            "rebuild_required": True, "rebuild_reason": "backend image", "local_evidence_summary": "unit tests pass; lint pass", "codex_review_status": "APPROVED",
            "runtime_validation_missing": True, "local_gate_result": "PASS",
        }
        payload.update(overrides)
        path = self.review_dir / "runtime_payload.json"
        path.write_text(json.dumps(payload))
        return path

    def evidence_file(self, sha: str = SHA_A, result: str = "PASS", environment: str = "TEST", name: str = "RUNTIME_EVIDENCE.json", **overrides) -> Path:
        evidence = {"schema_version": 1, "candidate_sha": sha, "environment": environment, "result": result, "commands": [{"command": "runtime-test.sh backend", "result": "PASS"}], "timestamp": hs.iso_utc(NOW - 60)}
        evidence.update(overrides)
        path = self.review_dir / name
        path.write_text(json.dumps(evidence))
        return path

    def record_runtime(self, result: str, *, sha: str = SHA_A, environment: str = "TEST", evidence: Path | None = None, actor: str = "agent", confirm: bool = True, operator: str = "operator-cli") -> dict:
        """Collaborative runtime evidence in two authenticated steps: the agent proposes, the operator confirms.
        With confirm=False only the proposal exists (nothing recorded)."""
        evidence = evidence or self.evidence_file(sha, result, environment=environment)
        with self.sup.store.transaction():
            st = self.sup.store.read_state()
            proposed = self.sup.record_runtime_evidence(st, run_id=st["run_id"], candidate_sha=sha, environment=environment, evidence_file=str(evidence), result=result, actor=actor, head_resolver=self.head_resolver)
        if not confirm or not proposed.get("proposed"):
            return proposed
        with self.sup.store.transaction():
            st = self.sup.store.read_state()
            return self.sup.confirm_runtime_evidence(st, run_id=st["run_id"], gate_id=proposed["gate_id"], evidence_sha256=proposed["evidence_sha256"], decision=result, actor=operator, head_resolver=self.head_resolver)

    # ----- drive a gated run to a given point
    def start_gated(self, responses: list[dict]) -> int:
        self.herdr.responses = responses
        return self.sup.run_new("Add the widget", "codex", workflow_policy="gated_v2")

    def approve_pending(self, **kwargs) -> dict:
        with self.sup.store.transaction():
            state = self.sup.store.read_state()
            gate = state["pending_gate"]
            return self.sup.approve_gate(state, run_id=state["run_id"], gate_id=gate["gate_id"], actor=kwargs.pop("actor", "cli"), **kwargs)

    def resume_with(self, responses: list[dict]) -> int:
        self.herdr.responses = responses
        return self.sup.resume()
