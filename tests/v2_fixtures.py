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
import herdr_helper_context as hctx  # noqa: E402


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
        self.contexts: dict[str, str | None] = {}  # request id -> bound helper-context fingerprint

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

    def thread_start(self, payload: dict, request_id: str, context_fingerprint: str | None = None) -> dict:
        self.requests.append((request_id, dict(payload)))
        self.contexts[request_id] = context_fingerprint
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

    def result_if_present(self, operation: str, payload: dict, request_id: str, context_fingerprint: str | None = None) -> dict | None:
        hcr.make_request(operation, payload, request_id, context_fingerprint)
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
        # Detector explanation (herdr agent explain), reduced: by default a quiet idle pane with no working rule matched.
        self.explains: list[str] = []  # raw TARGET of every explain call
        self.explain_matched: set[str] = set()  # rule ids reported as matched
        self.explain_state = "idle"
        self.explain_omit: set[str] = set()  # rule ids missing from the evaluated list
        self.explain_skipped = False
        self.explain_error: Exception | None = None
        self.on_explain = None  # callable(fake, target) run before each verdict (script indicators, advance a clock, ...)
        self.explain_raw: dict | None = None  # a raw `agent explain` payload that is run through the real adapter reducer (may raise HerdrError)
        # Issue 5: a provider that reports its native identity only after a first turn, and pane labels.
        self.hold_identity = False  # start_agent withholds agent_session until a bootstrap prompt arrives
        self.bootstrap_mode = "ok"  # ok | stall | reject | swallow | wrong | duplicate
        self.bootstrap_prompts: list[tuple[str, str]] = []
        self._held: dict[str, str] = {}
        self.pane_labels: dict[str, str] = {}
        self.renames: list[tuple[str, str]] = []
        self.rename_mode = "ok"  # ok | error | nolabel | mismatch | late
        self.pane_cwd: dict[str, str] = {}
        # Explicit Codex launch (`pane run`) simulation. run_mode: ok | late | lost | dropped | swallowed | wrong | busy
        self.run_mode = "ok"
        self.late_polls = 2
        self.pane_runs: list[tuple[str, str]] = []
        self.launch_env: dict[str, dict] = {}  # pane -> {"argv","cwd","CODEX_HOME","marker"} actually derived from the command
        self.rename_agent_mode = "ok"  # ok | error | noop
        self.agent_renames: list[tuple[str, str]] = []
        # The fixture models an adapter that HAS an empty-input contract (the real one has none: see test_explicit_launch).
        self.launch_caps: dict[str, bool] = {"pane run": True, "pane process-info (--pane)": True, "agent rename": True, "pane empty interactive input proof": True}
        self.input_empty_mode = "empty"  # empty | pending | unknown
        # Exact pane metadata for the guarded profile. metadata_mode: ok | foreign | string_bool | missing | agent | other_cwd | error
        self.metadata_mode = "ok"
        self.pane_focused: dict[str, bool] = {}
        self.metadata_reads: list[str] = []
        self.on_metadata = None
        self.input_checks: list[str] = []
        self._pending_spawn: dict[str, tuple[str, int]] = {}
        self.inherited_marker = "HERDR_PANE_MARKER=inherited"
        # Human-handled startup prompt fixtures (additive): scripted visible screens, a provider that starts but is not ready, and an undetected explicit launch.
        self.screens: dict[str, str] = {}
        self.pane_reads: list[str] = []
        self.pane_read_mode = "ok"  # ok | error | none
        self.startup_block: str | None = None  # None | "start_error" | "blocked_agent" | "undetected"
        self.startup_block_for = "-run-"  # agent-name fragment the block applies to
        self.start_error_code = "agent_not_ready"
        self._undetected: dict[str, tuple[str, dict]] = {}
        self.processes: dict[str, dict] = {}  # pane -> `pane process-info` result, recorded when an agent starts there
        self._pid = 9000

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

    RULE_IDS = ("osc_title_working", "live_turn_working", "background_agents_working", "background_mcp_task_working", "btw_overlay_working", "live_prompt_box")
    WORKING_RULE_IDS = ("osc_title_working", "live_turn_working", "background_agents_working", "background_mcp_task_working", "btw_overlay_working")

    def explain_agent(self, name: str) -> dict:
        key = self._resolve(name, "explain")
        self.explains.append(name)
        if self.on_explain is not None:
            self.on_explain(self, name)
        if self.explain_error is not None:
            raise self.explain_error
        if self.explain_raw is not None:
            import herdr_cli  # noqa: PLC0415

            return herdr_cli.HerdrCli.reduce_explain(self.explain_raw)
        rules = [{"id": rule, "matched": rule in self.explain_matched, "state": "working" if rule in self.WORKING_RULE_IDS else "idle"} for rule in self.RULE_IDS if rule not in self.explain_omit]
        matched = sorted(self.explain_matched)
        return {"state": self.explain_state, "matched_rule": matched[0] if matched else None, "rules": rules, "manifest_version": "fixture", "screen_detection_skipped": self.explain_skipped, "visible_blocker": self.agents[key].get("agent_status") == "blocked"}

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

    def _bootstrap_prompt(self, name: str, text: str) -> dict:
        self.bootstrap_prompts.append((name, text))
        agent = self.agents[name]
        if self.bootstrap_mode == "reject":
            raise hs.HerdrError("blocked", code="agent_blocked")
        if self.bootstrap_mode == "stall":
            raise hs.HerdrError("stalled", code="agent_prompt_stalled")
        held = self._held.get(name)
        if self.bootstrap_mode == "wrong":
            held = "99999999-9999-4999-8999-999999999999"
        if self.bootstrap_mode == "duplicate":
            held = CLAUDE_SESSION
        if self.bootstrap_mode != "swallow" and held:
            agent["agent_session"] = {"value": held}
        return {"result": {"agent": {**agent, "agent_status": "working"}}}

    def process_info(self, pane: str) -> dict:
        if pane in self._pending_spawn:
            command, remaining = self._pending_spawn[pane]
            if remaining <= 0:
                del self._pending_spawn[pane]
                self._spawn(pane, command)
            else:
                self._pending_spawn[pane] = (command, remaining - 1)
        if pane not in self.processes:
            raise hs.HerdrError("pane process contract unavailable")
        return json.loads(json.dumps(self.processes[pane]))

    def launch_capability_report(self) -> dict:
        return dict(self.launch_caps)

    def pane_metadata(self, pane: str) -> dict:
        self.metadata_reads.append(pane)
        if self.on_metadata is not None:
            self.on_metadata(self, pane)
        mode = self.metadata_mode
        if mode == "error":
            raise hs.HerdrError("pane get failed", code="pane_not_found")
        detected = next((a["agent"] for a in self.agents.values() if a.get("pane_id") == pane), None)
        meta = {"pane_id": pane, "focused": self.pane_focused.get(pane, False), "agent": detected,
                "cwd": "/elsewhere" if mode == "other_cwd" else self.pane_cwd.get(pane)}
        if mode == "foreign":
            meta["pane_id"] = "w3:p2"
        if mode == "string_bool":
            meta["focused"] = "false"
        if mode == "missing":
            del meta["focused"]
        return meta

    def pane_input_empty(self, pane: str) -> bool | None:
        self.input_checks.append(pane)
        return {"empty": True, "pending": False, "unknown": None}[self.input_empty_mode]

    def _spawn(self, pane: str, command: str, *, wrong: bool = False) -> None:
        """What the shell would do with the submitted command: `env --chdir DIR NAME=VALUE... PROGRAM ARGS...`."""
        import shlex
        tokens = shlex.split(command)
        assert tokens[1] == "--chdir", tokens
        cwd, rest = tokens[2], tokens[3:]
        env = {}
        while rest and "=" in rest[0] and not rest[0].startswith("/"):
            name, value = rest.pop(0).split("=", 1)
            env[name] = value
        program, args = rest[0], rest[1:]
        shell_pid = self.processes[pane]["shell_pid"]
        self._pid += 1
        argv = [program, *args] if not wrong else [program, "resume", "99999999-9999-4999-8999-999999999999", "--no-daemon"]
        self.processes[pane] = {"pane_id": pane, "shell_pid": shell_pid, "foreground_process_group_id": self._pid,
                                "foreground_processes": [{"argv": argv, "cmdline": " ".join(argv), "cwd": cwd, "name": "codex", "pid": self._pid}]}
        self.launch_env[pane] = {"argv": argv, "cwd": cwd, "CODEX_HOME": env.get("CODEX_HOME"), "marker": self.inherited_marker}
        # A legacy `codex resume <thread>` launch reports exactly that thread; a DIRECT launch (no `resume`) creates a NEW native session whose id the fixture scripts in `next_sessions`.
        thread = args[1] if args[:1] == ["resume"] and len(args) > 1 else self.next_sessions["codex"]
        name = f"codex-detected-{pane.replace(':', '-')}"
        agent = self.agent("codex", name, pane, thread)
        if self.hold_identity:
            self._held[name] = thread
            agent.pop("agent_session", None)
        elif self.resume_reports is not _RESUMED:
            if self.resume_reports is None:
                agent.pop("agent_session", None)
            else:
                agent["agent_session"] = {"value": self.resume_reports}
        if self.startup_block == "blocked_agent" and ("codex" in self.startup_block_for or "-run-" in self.startup_block_for):
            agent["agent_status"] = "blocked"  # the provider process runs and Herdr detects it, but it is waiting at a startup screen
        if self.startup_block == "undetected":
            self._undetected[pane] = (name, agent)  # the provider process runs but Herdr has not detected an agent yet (a startup prompt is up)
            return
        self.agents[name] = agent

    def pane_read(self, pane: str, *, lines: int = 60) -> str:
        self.pane_reads.append(pane)
        if self.pane_read_mode == "error":
            raise hs.HerdrError("pane read failed", code="command_error")
        return self.screens.get(pane, "")

    def unblock(self, pane: str, status: str = "idle") -> None:
        """The human handled the startup prompt in the pane: the agent is detected/ready and the prompt is no longer on screen."""
        if pane in self._undetected:
            name, agent = self._undetected.pop(pane)
            if self.hold_identity:
                self._held[name] = agent["agent_session"]["value"]
                agent.pop("agent_session", None)
            self.agents[name] = agent
        for agent in self.agents.values():
            if agent.get("pane_id") == pane:
                agent["agent_status"] = status
        self.screens.pop(pane, None)
        self.startup_block = None

    def pane_run(self, pane: str, command: str) -> dict:
        self.pane_runs.append((pane, command))
        mode = self.run_mode
        if mode == "dropped":
            raise hs.HerdrError("pane run failed", code="command_error")
        if mode == "late":
            self._pending_spawn[pane] = (command, self.late_polls)
        elif mode in ("ok", "lost", "wrong"):
            self._spawn(pane, command, wrong=(mode == "wrong"))
        if mode == "lost":
            raise hs.HerdrError("pane run exceeded the local timeout", code="timeout")
        return {}

    def rename_agent(self, target: str, name: str) -> dict:
        self.agent_renames.append((target, name))
        if self.rename_agent_mode == "error":
            raise hs.HerdrError("rename failed", code="agent_not_found")
        if self.rename_agent_mode == "noop":
            return {}
        for key, agent in list(self.agents.items()):
            if agent.get("pane_id") == target and key not in ("codex-main", "claude-main"):
                agent["name"] = name
                self.agents.pop(key)
                self.agents[name] = agent
                if key in self._held:
                    self._held[name] = self._held.pop(key)
                return {}
        raise hs.HerdrError("no agent in pane", code="agent_not_found")

    def rename_pane(self, pane_id: str, label: str) -> None:
        self.renames.append((pane_id, label))
        if self.rename_mode == "error":
            raise hs.HerdrError("rename failed", code="pane_not_found")
        if self.rename_mode == "mismatch":
            self.pane_labels[pane_id] = "something else"
        elif self.rename_mode not in ("nolabel", "late"):
            self.pane_labels[pane_id] = label

    def pane_label(self, pane_id: str) -> str | None:
        return self.pane_labels.get(pane_id)

    def prompt_ack(self, name: str, text: str, *, timeout_ms: int) -> dict:
        if name in self._held:
            return self._bootstrap_prompt(name, text)
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
        self._pid += 1
        self.processes[pane_id] = {"pane_id": pane_id, "shell_pid": self._pid, "foreground_process_group_id": self._pid + 1,
                                   "foreground_processes": [{"argv": [kind, *args], "cmdline": " ".join([kind, *args]), "cwd": self.pane_cwd.get(pane_id, "/"), "name": kind, "pid": self._pid + 1}]}
        self.agents[name] = self.agent(kind, name, pane_id, session)
        if session is None:
            self.agents[name].pop("agent_session", None)
        elif self.hold_identity and "-run-" in name:
            self._held[name] = session
            self.agents[name].pop("agent_session", None)
        if self.startup_block in ("start_error", "blocked_agent") and self.startup_block_for in name:
            self.agents[name]["agent_status"] = "blocked"
            if self.startup_block == "start_error":
                raise hs.HerdrError(f"agent {name} is blocked during startup", code=self.start_error_code)
        return {}

    def split_pane(self, pane_id: str, *, direction: str, ratio: float, cwd: str) -> str | None:
        self.splits.append((pane_id,direction,ratio,cwd))
        pane=self.next_pane
        self.pane_cwd[pane]=cwd
        self.next_pane=f"w9:p{len(self.splits)+1}"
        self.available_panes.add(pane)
        self._pid += 1
        self.processes[pane] = {"pane_id": pane, "shell_pid": self._pid, "foreground_process_group_id": self._pid,
                                "foreground_processes": [{"argv": ["bash"], "cmdline": "bash", "cwd": cwd, "name": "bash", "pid": self._pid}]}
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
            # A NEW fresh Codex is launched directly through the explicit pane-run recipe, so the fixture's default is that method. The fake adapter models an empty-input contract, so the default input
            # policy stays the strict one; the guarded profile is exercised by test_guarded_pane.
            "session_start": {"codex_launch_method": "explicit-pane-run"},
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
        self.helper_report = {"ready": True, "reasons": [], "context": None, "fingerprint": "ab" * 32,
                              "units": {"path": "herdr-codex-reset-worker.path", "service": "herdr-codex-reset-worker.service", "directory": "/unit-dir"},
                              "activation": {"unit_file_state": "enabled", "active_state": "active", "sub_state": "waiting"},
                              "evidence": "structural-read-only", "checks": {}}
        self.helper_codex_home = "/home/test/.codex"
        fake_bin = root / "fake-bin"
        fake_bin.mkdir()
        self.fake_codex = str(fake_bin / "codex")
        Path(self.fake_codex).write_text("#!/bin/sh\nexit 0\n")
        os.chmod(self.fake_codex, 0o755)
        self.sup = self.make_supervisor()

    def seed_legacy_preparation(self, prep: str, *, phase: str = "THREAD_CREATING", method: str = "explicit-pane-run", policy: str | None = None, fingerprint: str | None = "auto",
                                settle: bool = False) -> dict:
        """Journal a fresh-Codex preparation EXACTLY as the earlier (pre-created-thread) library wrote it at `phase`: thread fields, no direct-launch marker. Used to prove that such a
        journal is still read and reconciled by the legacy path and is never converted into a direct launch. `settle` also records the helper's settled thread result."""
        import herdr_sessions as sessions

        selection = sessions.validate_selection(self.config, {"policy": "fresh-codex", "profiles": {"codex": "default", "claude": "default"}})
        payload = {"cwd": str(self.project_root), "model": None, "ephemeral": False}
        item: dict = {"status": phase, "old_pane_id": self.owners["codex"]["pane_id"], "old_session_id": self.owners["codex"]["session_id"], "profile": "default", "preparation_id": prep}
        if method == "explicit-pane-run":
            item.update(launch_method="explicit-pane-run", input_policy=policy or "require-empty-proof")
        configured_mode = (self.config["session_start"].get("approval_modes") or {}).get("codex", "inherit")
        if configured_mode != "inherit":
            item["approval_mode"] = configured_mode
        if phase in ("THREAD_CREATING", "THREAD_CREATED"):
            item.update(thread_request_id=f"thread-{prep}-codex", thread_payload=payload)
            if fingerprint == "auto":
                item["helper_context_fingerprint"] = hctx.fingerprint(self.helper_descriptor(payload["cwd"]))
            elif fingerprint is not None:
                item["helper_context_fingerprint"] = fingerprint
        if phase == "THREAD_CREATED":
            item.update(thread_id="44444444-4444-4444-8444-444444444444", thread_model="gpt-5-codex", thread_provider="openai")
        record = {"schema_version": 2, "preparation_id": prep, "status": "SESSION_SELECTION_AUTHORIZED", "selection": selection, "created_at": hs.iso_utc(self.clock.current), "origin": "supervisor",
                  "request_binding": None, "request_envelope": None, "owners_before": {p: dict(self.owners[p]) for p in self.owners}, "providers": {"codex": item}}
        self.paths.session_preparations_dir.mkdir(parents=True, exist_ok=True)
        hs.atomic_write_json(self.paths.session_preparations_dir / f"{prep}.json", record)
        if settle:
            self.thread_journal.results[f"thread-{prep}-codex"] = {"request_id": f"thread-{prep}-codex", "ok": True, "thread": self.thread_journal._make(payload)}
        return record

    def helper_descriptor(self, cwd: str) -> dict:
        root = str(self.paths.codex_reset_dir)
        return {"version": 1, "instance": hctx.instance_id(root), "journal_root": root, "codex_bin": self.fake_codex,
                "codex_home": self.helper_codex_home, "working_directory": cwd}

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
        # The shared helper report and descriptor are injected like the Codex contract: fixtures never read the
        # real systemd user manager or the real home directory. Tests mutate `helper_report` to refuse.
        sup.helper_readiness = lambda method=None, policy=None: json.loads(json.dumps(self.helper_report))
        sup._monotonic = self.clock.time  # injectable monotonic time: the fake clock, never wall-clock sleeps
        # Explicit launch: production refuses scripts; this fixture's provider IS a script, so the executable check is a
        # test-only seam on the Supervisor instance (never a config or request value).
        sup.launch_executable_check = lambda path: True
        sup.helper_context = lambda cwd=None: self.helper_descriptor(cwd or str(self.config["project_root"]))
        sup._codex_thread_contract = dict(getattr(self, "codex_contract", {"codex_app_server": True, "codex_thread_start": True, "codex_resume_session_id": True, "codex_pane_local_server": True}))
        # The DIRECT fresh-Codex launch has its own read-only facts: the top-level CLI help (pane-local server, approval flag) and the launch readiness. Fixtures never run the real CLI;
        # `direct_contract` and `direct_report` are mutable per test (defaults: verified / ready).
        sup._codex_direct_contract = dict(getattr(self, "direct_contract", {"codex_pane_local_server": True, "codex_approval_flag": True}))
        sup.direct_launch_readiness = lambda policy=None: json.loads(json.dumps(getattr(self, "direct_report", {"ready": True, "reasons": []})))
        sup.startup_codex_direct_contract = lambda: dict(sup._codex_direct_contract)  # the uncached post-handling probe: a fixture value, never the host CLI
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

    def start_supervised(self, responses: list[dict], *, start: str = "codex", task: str = "Investigate the reboot") -> int:
        self.herdr.responses = responses
        return self.sup.run_new(task, start, workflow_policy="supervised_v2")

    def approve_pending(self, **kwargs) -> dict:
        with self.sup.store.transaction():
            state = self.sup.store.read_state()
            gate = state["pending_gate"]
            return self.sup.approve_gate(state, run_id=state["run_id"], gate_id=gate["gate_id"], actor=kwargs.pop("actor", "cli"), **kwargs)

    def resume_with(self, responses: list[dict]) -> int:
        self.herdr.responses = responses
        return self.sup.resume()
