"""The Herdr CLI adapter: subprocess execution, error-code parsing, native session identity, and the read-only capability probe. Depends on herdr_core only."""

from __future__ import annotations

import dataclasses
import json
import re
import subprocess
from typing import Any, Iterable

from herdr_core import HerdrError


def command_error_code(output: str) -> str:
    for line in output.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        error = value.get("error") if isinstance(value, dict) else None
        if isinstance(error, dict) and isinstance(error.get("code"), str):
            return error["code"]
    lowered = output.lower()
    for known in ("agent_prompt_stalled", "agent_blocked", "agent_not_found", "agent_not_idle", "agent_not_ready", "timeout"):
        if known in lowered:
            return known
    return "command_error"

# ---- Herdr version policy and semantic capability contract
#
# Herdr 0.9.0 is the minimum fully tested CLI contract for this beta. A version at or above the minimum is
# necessary but not sufficient: the required commands and options below are probed read-only (`--help`)
# and are authoritative. Newer versions that keep the contract pass with their detected version reported.
MIN_HERDR_VERSION = (0, 9, 0)
MIN_HERDR_VERSION_TEXT = ".".join(map(str, MIN_HERDR_VERSION))
_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")
# (capability name, command tokens, help fragments that must all be present)
REQUIRED_CAPABILITIES: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
    ("agent list", ("agent", "list"), ()),
    ("agent get", ("agent", "get"), ()),
    ("agent read (source/format/lines)", ("agent", "read"), ("--source", "recent-unwrapped", "--format", "--lines")),
    ("agent prompt (--wait/--timeout)", ("agent", "prompt"), ("--wait", "--timeout")),
    ("agent wait (--until/--timeout)", ("agent", "wait"), ("--until", "--timeout")),
    ("agent send-keys", ("agent", "send-keys"), ()),
    ("agent start (--kind/--pane)", ("agent", "start"), ("--kind", "--pane")),
    ("pane get", ("pane", "get"), ()),
    ("workspace create (--cwd/--label/--no-focus)", ("workspace", "create"), ("--cwd", "--label", "--no-focus")),
    ("plugin action invoke (--plugin)", ("plugin", "action", "invoke"), ("--plugin",)),
)
# Optional acceleration, reported separately: lifecycle acknowledgement of a submission.
OPTIONAL_CAPABILITIES: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
    ("prompt lifecycle acknowledgement (--until + agent_prompt_stalled)", ("agent", "prompt"), ("--until", "agent_prompt_stalled")),
)


def help_has_lexeme(help_text: str | None, lexeme: str) -> bool:
    """Exact option/identifier presence in recorded help: the lexeme must stand alone, bounded by
    characters that cannot continue an option or identifier (`--wait` is not `--waiter` or `--wait-for`;
    `recent` is not `recent-unwrapped`; `agent_prompt_stalled` is not `agent_prompt_stalled_ms`)."""
    if not help_text:
        return False
    return re.search(r"(?<![\w-])" + re.escape(lexeme) + r"(?![\w-])", help_text) is not None


def parse_herdr_version(text: str) -> tuple[int, int, int] | None:
    """`herdr 0.9.0` -> (0, 9, 0); anything without a dotted triple is unknown (None)."""
    match = _VERSION_RE.search(text or "")
    return (int(match.group(1)), int(match.group(2)), int(match.group(3))) if match else None


def evaluate_contract(version_text: str | None, help_texts: dict[tuple[str, ...], str | None]) -> dict[str, Any]:
    """Pure policy: version floor AND required capabilities decide compatibility; optional ones only inform."""
    version = parse_herdr_version(version_text or "")
    version_ok = version is not None and version >= MIN_HERDR_VERSION
    required: dict[str, bool] = {}
    for name, command, fragments in REQUIRED_CAPABILITIES:
        text = help_texts.get(command)
        required[name] = text is not None and all(help_has_lexeme(text, fragment) for fragment in fragments)
    optional: dict[str, bool] = {}
    for name, command, fragments in OPTIONAL_CAPABILITIES:
        text = help_texts.get(command)
        optional[name] = text is not None and all(help_has_lexeme(text, fragment) for fragment in fragments)
    missing = [name for name, ok in required.items() if not ok]
    if version is None:
        version_detail = "herdr version is missing or malformed"
    elif not version_ok:
        version_detail = f"herdr {'.'.join(map(str, version))} is older than the minimum tested {'.'.join(map(str, MIN_HERDR_VERSION))}"
    else:
        version_detail = f"herdr {'.'.join(map(str, version))} satisfies the minimum {'.'.join(map(str, MIN_HERDR_VERSION))}"
    return {
        "detected_version": ".".join(map(str, version)) if version else None,
        "version_text": (version_text or "").strip() or None,
        "minimum_version": ".".join(map(str, MIN_HERDR_VERSION)),
        "version_ok": version_ok,
        "version_detail": version_detail,
        "required": required,
        "missing_capabilities": missing,
        "optional": optional,
        "compatible": version_ok and not missing,
        "prompt_ack_mode": "lifecycle" if optional.get(OPTIONAL_CAPABILITIES[0][0]) else "settle",
    }


class HerdrCli:
    """Thin adapter over the `herdr` CLI. Every method maps to one supported command."""

    # Lifecycle states that acknowledge an accepted submission: Herdr observed the agent start (or hit a
    # dialog) AFTER the input was sent. Neither elapsed time nor terminal text is involved.
    PROMPT_ACK_STATES = ("working", "blocked")

    def __init__(self, binary: str) -> None:
        self.binary = binary
        self._prompt_ack_supported: bool | None = None
        self._contract: dict[str, Any] | None = None

    # ----- read-only version/capability probe (cached per process; never prompts or waits)

    def version_text(self) -> str | None:
        try:
            return str(self._run(["--version"], timeout=30, json_result=False))
        except HerdrError:
            return None

    def help_text(self, command: tuple[str, ...]) -> str | None:
        try:
            return str(self._run([*command, "--help"], timeout=30, json_result=False))
        except HerdrError:
            return None

    def capability_report(self) -> dict[str, Any]:
        if self._contract is None:
            commands = {c for _, c, _ in REQUIRED_CAPABILITIES} | {c for _, c, _ in OPTIONAL_CAPABILITIES}
            self._contract = evaluate_contract(self.version_text(), {c: self.help_text(c) for c in sorted(commands)})
        return self._contract

    def _run(self, args: Iterable[str], *, timeout: float, json_result: bool = True) -> Any:
        command = [self.binary, *args]
        try:
            completed = subprocess.run(command, check=False, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired as error:
            def text_of(value: Any) -> str:
                return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else (value or "")
            output = text_of(error.stdout) + text_of(error.stderr)
            raise HerdrError(f"herdr {' '.join(list(args)[:2])} exceeded the local timeout", code="timeout", output=output) from error
        output = (completed.stdout or "") + (completed.stderr or "")
        if completed.returncode != 0:
            code = command_error_code(output)
            raise HerdrError(f"herdr {' '.join(list(args)[:2])} failed: {code}", code=code, output=output)
        if not json_result:
            return completed.stdout
        try:
            return json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            raise HerdrError("herdr returned invalid JSON", output=output) from error

    @staticmethod
    def _agent(response: Any) -> dict[str, Any]:
        try:
            agent = response["result"]["agent"]
        except (KeyError, TypeError) as error:
            raise HerdrError("herdr response did not contain an agent") from error
        if not isinstance(agent, dict):
            raise HerdrError("herdr agent payload is malformed")
        return agent

    def list_agents(self) -> list[dict[str, Any]]:
        response = self._run(["agent", "list"], timeout=30)
        try:
            agents = response["result"]["agents"]
        except (KeyError, TypeError) as error:
            raise HerdrError("herdr agent list is malformed") from error
        if not isinstance(agents, list):
            raise HerdrError("herdr agent list is not a list")
        return [item for item in agents if isinstance(item, dict)]

    def get_agent(self, name: str) -> dict[str, Any]:
        return self._agent(self._run(["agent", "get", name], timeout=30))

    def read_agent(self, name: str, *, source: str, lines: int | None) -> str:
        args = ["agent", "read", name, "--source", source, "--format", "text"]
        if lines is not None:
            args += ["--lines", str(lines)]
        return self._run(args, timeout=30, json_result=False)

    def prompt(self, name: str, text: str, *, timeout_ms: int) -> Any:
        """Settlement wait (compatibility path): returns when the turn settles or the bound expires."""
        return self._run(
            ["agent", "prompt", name, text, "--wait", "--timeout", str(timeout_ms)],
            timeout=timeout_ms / 1000 + 30,
        )

    @staticmethod
    def prompt_help_supports_ack(help_text: str) -> bool:
        """The installed CLI acknowledges a submission by lifecycle transition only if `--until` exists
        and the stalled outcome is documented; anything less keeps the settlement fallback."""
        return "--until" in help_text and "agent_prompt_stalled" in help_text

    def prompt_ack_supported(self) -> bool:
        """Optional acknowledgement capability from the cached read-only contract probe; a failed probe
        means unsupported (settlement fallback) — never a failure of the required contract by itself."""
        if self._prompt_ack_supported is None:
            self._prompt_ack_supported = self.capability_report()["prompt_ack_mode"] == "lifecycle"
        return self._prompt_ack_supported

    def prompt_ack(self, name: str, text: str, *, timeout_ms: int) -> Any:
        """Submit once and return as soon as Herdr observes working/blocked after submission. Outcomes:
        agent_blocked/agent_not_found = rejected before input; agent_prompt_stalled/timeout = input may
        have been sent and no transition was observed (delivery stays uncertain, never resent)."""
        return self._run(
            ["agent", "prompt", name, text, "--wait", *(arg for state in self.PROMPT_ACK_STATES for arg in ("--until", state)), "--timeout", str(timeout_ms)],
            timeout=timeout_ms / 1000 + 30,
        )

    def wait(self, name: str, *, timeout_ms: int) -> Any:
        return self._run(["agent", "wait", name, "--timeout", str(timeout_ms)], timeout=timeout_ms / 1000 + 30)

    def send_keys(self, name: str, keys: list[str]) -> Any:
        return self._run(["agent", "send-keys", name, *keys], timeout=30)

    def start_agent(self, name: str, *, kind: str, pane_id: str, args: list[str]) -> Any:
        return self._run(["agent", "start", name, "--kind", kind, "--pane", pane_id, "--", *args], timeout=330)

    def pane_available(self, pane_id: str) -> bool:
        """True when the recorded pane exists and hosts no agent (the shell prompt is expected)."""
        try:
            response = self._run(["pane", "get", pane_id], timeout=30)
        except HerdrError:
            return False
        pane = response.get("result", {}).get("pane") if isinstance(response, dict) else None
        return isinstance(pane, dict) and pane.get("agent") in (None, "")

    def create_workspace(self, *, label: str, cwd: str) -> str | None:
        """Create a non-focused workspace through the installed API and return its pane id, or None."""
        response = self._run(["workspace", "create", "--cwd", cwd, "--label", label, "--no-focus"], timeout=60)

        def find_pane(value: Any) -> str | None:
            if isinstance(value, dict):
                candidate = value.get("pane_id")
                if isinstance(candidate, str) and candidate:
                    return candidate
                for item in value.values():
                    found = find_pane(item)
                    if found:
                        return found
            if isinstance(value, list):
                for item in value:
                    found = find_pane(item)
                    if found:
                        return found
            return None

        return find_pane(response.get("result") if isinstance(response, dict) else None)

    def run_command(self, argv: list[str]) -> str:
        argv = [part.replace("{herdr}", self.binary) for part in argv]
        try:
            completed = subprocess.run(argv, check=False, capture_output=True, text=True, timeout=120)
        except subprocess.TimeoutExpired as error:
            raise HerdrError("quota refresh command timed out", code="timeout") from error
        if completed.returncode != 0:
            raise HerdrError(f"quota refresh command failed ({completed.returncode})", output=(completed.stdout or "") + (completed.stderr or ""))
        return completed.stdout

@dataclasses.dataclass(frozen=True)
class IdentityMatch:
    """Result of the one identity rule: `record` is the single live record carrying BOTH the persisted
    provider and the exact native session id; `duplicates` counts every live record with that session id
    (any provider); `provider_conflicts` are records with the session id but another provider."""

    record: dict[str, Any] | None
    duplicates: int
    provider_conflicts: int

    @property
    def unique(self) -> bool:
        return self.record is not None and self.duplicates == 1 and self.provider_conflicts == 0

    @property
    def ambiguous(self) -> bool:
        return self.duplicates > 1


def match_exact_session(agents: list[dict[str, Any]], provider: str, session_id: str | None) -> IdentityMatch:
    """Apply the identity rule to a live agent list. A session id shared by two records, or carried by a
    record of another provider, never yields a usable record."""
    if not isinstance(session_id, str) or not session_id:
        return IdentityMatch(None, 0, 0)
    with_session = [agent for agent in agents if session_identity(agent) == session_id]
    conflicts = [agent for agent in with_session if agent.get("agent") != provider]
    if len(with_session) != 1 or conflicts:
        return IdentityMatch(None, len(with_session), len(conflicts))
    return IdentityMatch(with_session[0], 1, 0)


def session_identity(agent: dict[str, Any] | None) -> str | None:
    if not isinstance(agent, dict):
        return None
    session = agent.get("agent_session")
    if isinstance(session, dict) and isinstance(session.get("value"), str) and session["value"]:
        return session["value"]
    return None
