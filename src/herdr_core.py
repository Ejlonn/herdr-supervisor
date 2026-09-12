"""Foundations of herdr-supervisor: constants, error types, paths, configuration, atomic JSON I/O, hashes, the worker lock, and bounded numeric helpers. Depends on nothing else in the project."""

from __future__ import annotations

import contextlib
import dataclasses
import datetime as dt
import fcntl
import hashlib
import json
import math
import os
import re
import shutil
import uuid
from pathlib import Path
from typing import Any

# Single source of the supervisor release identity. VERSION, pyproject.toml (normalized), README, and
# installed metadata are checked against it by the version-consistency tests and CI.
SUPERVISOR_VERSION = "0.3.0-beta.1"


def normalize_version(text: str) -> str:
    """Release string -> the PEP 440 form used by packaging metadata (`0.3.0-beta.1` -> `0.3.0b1`)."""
    value = text.strip()
    for word, short in (("-beta.", "b"), ("-alpha.", "a"), ("-rc.", "rc"), ("-beta", "b0"), ("-alpha", "a0"), ("-rc", "rc0")):
        if word in value:
            return value.replace(word, short)
    return value


SCHEMA_VERSION = 1  # V1 config/state schema (still readable)

STATE_SCHEMA_VERSION = 2

PROVIDERS = ("codex", "claude")

ROUTES = ("codex", "claude", "done", "human")

GATE_STATES = ("WAIT_PLAN_APPROVAL", "WAIT_RUNTIME_VALIDATION", "WAIT_PUSH_APPROVAL")

SUPERVISOR_STATES = ("RUNNING", "WAIT_QUOTA", "WAIT_USER", "PAUSED", "DONE", "ERROR", "CANCELLED", *GATE_STATES)

TERMINAL_STATES = {"DONE", "CANCELLED"}

HUMAN_WAIT_STATES = {"WAIT_USER", *GATE_STATES}

GATE_TYPES = ("plan_approval", "generic_question", "runtime_validation", "push_approval")

GATE_STATE_FOR = {
    "plan_approval": "WAIT_PLAN_APPROVAL",
    "generic_question": "WAIT_USER",
    "runtime_validation": "WAIT_RUNTIME_VALIDATION",
    "push_approval": "WAIT_PUSH_APPROVAL",
}

WORKFLOW_POLICIES = ("v1", "gated_v2")

EVENT_TYPES = (
    "TASK_STARTED", "PLAN_APPROVAL_REQUIRED", "PLAN_APPROVED", "PLAN_REJECTED", "REVISION_REQUESTED",
    "QUESTION_ASKED", "QUESTION_ANSWERED", "WAIT_USER", "WAIT_QUOTA", "QUOTA_RESUMED",
    "RUNTIME_VALIDATION_READY", "RUNTIME_VALIDATION_FAILED", "RUNTIME_VALIDATION_PASSED",
    "PUSH_APPROVAL_REQUIRED", "PUSH_APPROVED", "TASK_PAUSED", "TASK_RESUMED", "TASK_CANCELLED", "TASK_DONE",
    "TASK_ERROR", "RECOVERED_AFTER_RESTART", "COMMAND_RESULT", "QUERY_RESULT",
    "CODEX_RESET_AUTHORIZED", "CODEX_RESET_STARTED", "CODEX_RESET_VERIFIED", "CODEX_RESET_UNAVAILABLE",
    "TASK_HANDED_OFF", "AGENT_FOLLOWUP_READY",
)

# Gate-preserving follow-up ("Ask agent"): one bounded question to the same native session while a human
# decision (typed gate or missing-result wait) stays suspended and is restored unchanged afterwards.
FOLLOWUP_STATES = ("PREPARED", "DELIVERING", "WAITING", "RESPONSE_READY", "RESTORED", "FAILED", "ABANDONED")

FOLLOWUP_UNRESOLVED = frozenset({"PREPARED", "DELIVERING", "WAITING", "RESPONSE_READY", "FAILED"})

FOLLOWUP_DECISION_KINDS = ("gate", "missing_result")

MAX_FOLLOWUP_QUESTION_CHARS = 1000

MAX_FOLLOWUP_SUMMARY_CHARS = 300

FOLLOWUP_KEYS = ("HERDR_FOLLOWUP", "HERDR_RUN", "HERDR_FOLLOWUP_TURN", "HERDR_DECISION", "HERDR_RESPONSE", "HERDR_RESPONSE_SHA256", "HERDR_SUMMARY")

# Evidence a `done` route may lack while everything else is satisfied. Only these can make a run
# operator-handoff ready; the human may then close it without Supervisor verifying that evidence.
HANDOFF_UNMET_KINDS = ("runtime_validation_pass", "push_approval")

COMPLETION_MODES = ("operator_handoff",)

MAX_HANDOFF_NOTE_CHARS = 500

ACTIONABLE_EVENTS = {"PLAN_APPROVAL_REQUIRED", "PUSH_APPROVAL_REQUIRED", "QUESTION_ASKED"}

EVENT_NAMESPACE = uuid.UUID("6f1c1a2e-9b7d-4d0e-8c3a-5b2f7e9d1a44")

SHA_RE = re.compile(r"^[0-9a-f]{40}$")

PLAN_CHANGED_MESSAGE = "Plan changed after it was presented; review the updated plan."

LIFECYCLE_STATES = {"working", "idle", "done", "blocked", "unknown"}

READY_STATES = {"idle", "done"}

QUOTA_KINDS = ("five_hour", "weekly")

# Timestamps must be representable for local/UTC reporting (year 9999 upper bound).
MAX_EPOCH = 253402300799.0

# Leading decoration that terminal UIs add in front of response lines.
_LINE_PREFIX_RE = re.compile(r"^[\s>•*\-·│┃┆┊⏺❯]+")

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

_STAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:-]{0,79}$")

PROTOCOL_KEYS = ("HERDR_PROTOCOL", "HERDR_RUN", "HERDR_TURN", "HERDR_STAGE", "HERDR_NEXT", "HERDR_HANDOFF")

PROTOCOL_KEYS_V2 = ("HERDR_PROTOCOL", "HERDR_RUN", "HERDR_TURN", "HERDR_STAGE", "HERDR_NEXT", "HERDR_GATE", "HERDR_PAYLOAD", "HERDR_HANDOFF")

DEFAULT_CONFIG: dict[str, Any] = {
    "schema_version": SCHEMA_VERSION,
    "project_root": None,  # resolved from HERDR_SUPERVISOR_PROJECT_ROOT or ~/workspace (see resolve_config_defaults)
    "herdr_bin": None,  # resolved from HERDR_BIN_PATH, then PATH
    "quota_dir": None,  # resolved to $XDG_STATE_HOME/herdr/plugins/herdr-agent-quota
    "quota_safety_buffer_seconds": 120,
    "poll_interval_seconds": 5,
    "prompt_wait_timeout_ms": 20000,
    # Lifecycle acknowledgement: `agent prompt --wait --until working --until blocked` returns as soon as
    # Herdr observes the agent working or blocked after submission (Herdr itself reports
    # agent_prompt_stalled after 5000 ms). The bound applies to the acknowledgement only, never to the turn.
    "prompt_ack_timeout_ms": 8000,
    # Circuit breaker: automatic same-provider routing chains stop before preparing turn limit+1.
    # Reset only by an authenticated human continuation or a cross-provider handoff; never by
    # polling, restart, or resume. A supervisor safety limit, not a provider quota estimate.
    "max_consecutive_auto_turns": 8,
    "wait_timeout_ms": 10000,
    "read_lines": 400,
    "agents": {
        "codex": {
            "name": "codex-main",
            "quota_file": "codex-app-server.json",
            "native_resume_args": ["resume", "{session_id}"],
        },
        "claude": {
            "name": "claude-main",
            "quota_file": "claude-statusline.json",
            "native_resume_args": ["--resume", "{session_id}"],
        },
    },
    # Only Codex quota can be refreshed independently; Claude quota comes from
    # the StatusLine hook and only changes after Claude activity.
    "codex_quota_refresh_command": [
        "{herdr}", "plugin", "action", "invoke", "refresh", "--plugin", "herdr-agent-quota",
    ],
    # Non-LLM quota refresh per provider. A provider without a verified independent refresh keeps the
    # cached-deadline wait and reports that early refresh is unavailable (never wakes that provider).
    "quota_refresh_commands": {"codex": "codex_quota_refresh_command", "claude": None},
    # WAIT_QUOTA is a deadline-plus-recheck wait: every quota_recheck_seconds the worker runs only the
    # configured refresh mechanism and re-reads the snapshot (zero LLM calls). 600 s = six plugin refreshes
    # per hour, discovering manual/provider corrections well before a multi-hour cached reset.
    "quota_recheck_seconds": 600,
    # Query routing and post-settle anomaly classification require recent provider evidence. This is
    # intentionally longer than the ten-minute recheck cadence, while still rejecting abandoned snapshots.
    "quota_snapshot_max_age_seconds": 900,
    "manual_quota_refresh_min_interval_seconds": 60,
    "codex_reset": {"enabled": True, "helper_timeout_seconds": 30, "inventory_max_age_seconds": 120,
                    "verification_attempts": 4, "verification_interval_seconds": 2},
    # Conservative provider-specific patterns. A quota wait is entered only when
    # the snapshot shows a blocking window AND one of these matches the screen.
    "quota_screen_patterns": {
        "codex": [
            r"usage limit",
            r"rate limit",
            r"you.?ve hit your (?:usage|weekly|5.hour|five.hour) limit",
            r"you.?ve hit your session limit",
            r"(?:five.hour|5.hour|weekly).*(?:limit|reset)",
        ],
        "claude": [
            r"usage limit",
            r"rate limit",
            r"you.?ve hit your (?:usage|weekly|5.hour|five.hour) limit",
            r"you.?ve hit your session limit",
            r"limit (?:reached|will reset|resets)",
            r"out of extra usage",
        ],
    },
    # Keys sent to dismiss a provider quota dialog after reset. Empty by default:
    # an agent still `blocked` after a quota reset goes to WAIT_USER.
    "quota_block_dismiss_keys": [],
    # ---- V2 (schema-1 compatible defaults; absent keys in a V1 config are filled in memory)
    "review_root": None,  # resolved to <project_root>/reviews
    "product_repo": None,  # resolved to <project_root>
    "runtime_environments": ["TEST"],
    "gate_expiry_seconds": 7 * 86400,
    "event_expiry_seconds": 7 * 86400,
    "max_payload_bytes": 65536,
    "max_artifact_bytes": 2 * 1024 * 1024,
    "max_task_chars": 8000,
    "max_task_file_bytes": 65536,  # single authority for uploaded/loaded task files (bridge + worker + CLI)
    "task_file_suffixes": [".md", ".txt"],
    "max_note_chars": 2000,
    "timezone": "UTC",
    "query": {
        # Legacy single-provider fields (still honoured for an existing query-owner.json):
        "agent_name": "codex-query",
        "kind": "codex",
        "native_resume_args": ["resume", "{session_id}"],
        "launch_args": ["--sandbox", "read-only", "--ask-for-approval", "never", "--profile", "herdr-query", "-C", "{project_root}"],
        # Versioned provider registry: separately provisioned read-only query sessions, never the
        # workflow sessions. Preference order decides failover; each provider needs a verified read-only
        # launch contract in the installed client.
        "preference": ["codex", "claude"],
        "providers": {
            "codex": {"agent_name": "codex-query", "kind": "codex", "launch_args": ["--sandbox", "read-only", "--ask-for-approval", "never", "--profile", "herdr-query", "-C", "{project_root}"], "read_only_contract": "codex --sandbox read-only --ask-for-approval never"},
            "claude": {"agent_name": "claude-query", "kind": "claude", "launch_args": ["--permission-mode", "plan"], "read_only_contract": "claude --permission-mode plan (read-only plan mode; no edits, no approvals)"},
        },
        "allowed_paths": ["{project_root}"],  # "{project_root}" expands to the configured project root
        "max_question_chars": 1000,
        "max_answer_chars": 3000,
        "rate_limit_per_hour": 20,
        "prompt_wait_timeout_ms": 180000,
    },
}

class SupervisorError(RuntimeError):
    """Expected, fail-closed supervisor error."""

class QuotaError(SupervisorError):
    """Quota evidence is missing, malformed, or contradictory."""

class HerdrError(SupervisorError):
    def __init__(self, message: str, *, code: str = "command_error", output: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.output = output

@dataclasses.dataclass(frozen=True)
class Paths:
    config_file: Path
    state_dir: Path

    @classmethod
    def from_environment(cls) -> "Paths":
        home = Path.home()
        config_file = Path(os.environ.get("HERDR_SUPERVISOR_CONFIG", home / ".config/herdr-supervisor/config.json"))
        state_dir = Path(os.environ.get("HERDR_SUPERVISOR_STATE_DIR", home / ".local/state/herdr-supervisor"))
        return cls(config_file=config_file, state_dir=state_dir)

    @property
    def state_file(self) -> Path:
        return self.state_dir / "state.json"

    @property
    def owners_file(self) -> Path:
        return self.state_dir / "owners.json"

    @property
    def control_file(self) -> Path:
        return self.state_dir / "control.json"

    @property
    def lock_file(self) -> Path:
        return self.state_dir / "worker.lock"

    @property
    def logs_dir(self) -> Path:
        return self.state_dir / "logs"

    # ---- V2
    @property
    def txn_lock_file(self) -> Path:
        return self.state_dir / "txn.lock"

    @property
    def backups_dir(self) -> Path:
        return self.state_dir / "backups"

    @property
    def inbox_dir(self) -> Path:
        return self.state_dir / "inbox"

    @property
    def outbox_dir(self) -> Path:
        return self.state_dir / "outbox"

    @property
    def query_dir(self) -> Path:
        return self.state_dir / "query"

    @property
    def query_owner_file(self) -> Path:
        return self.state_dir / "query-owner.json"

    @property
    def query_lock_file(self) -> Path:
        # Inside the query directory so the query unit only needs write access to existing protected
        # directories (query/, outbox/) — no bare lock-file ReadWritePaths entry that must pre-exist.
        return self.query_dir / "worker.lock"

    @property
    def query_registry_file(self) -> Path:
        return self.state_dir / "query-providers.json"

    @property
    def task_files_dir(self) -> Path:
        """Supervisor-owned root for uploaded task files (0700; content 0600; opaque names)."""
        return self.state_dir / "inbox" / "task-files"

    @property
    def codex_reset_dir(self) -> Path:
        return self.state_dir / "codex-reset"

    @property
    def pending_starts_dir(self) -> Path:
        return self.state_dir / "pending-starts"

def iso_utc(epoch: float) -> str:
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

def iso_local(epoch: float) -> str:
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).astimezone().isoformat(timespec="seconds")

def atomic_write_json(path: Path, value: Any, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    payload = json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()

def load_json(path: Path, *, label: str) -> Any:
    try:
        with path.open(encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError as error:
        raise SupervisorError(f"{label} is missing: {path}") from error
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as error:
        raise SupervisorError(f"{label} is unreadable or corrupt: {path}: {error}") from error

def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = json.loads(json.dumps(base))
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result

def resolve_herdr_bin(configured: Any) -> str:
    candidates = [configured, os.environ.get("HERDR_BIN_PATH"), shutil.which("herdr")]
    for candidate in candidates:
        if isinstance(candidate, str) and candidate and Path(candidate).is_file():
            return candidate
    raise SupervisorError("herdr binary not found (set herdr_bin in config or HERDR_BIN_PATH)")

def xdg_state_home() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME") or (Path.home() / ".local/state"))

def resolve_config_defaults(config: dict[str, Any]) -> dict[str, Any]:
    """Fill machine-neutral defaults from the environment: nothing here is tied to a person or project."""
    root = config.get("project_root") or os.environ.get("HERDR_SUPERVISOR_PROJECT_ROOT") or str(Path.home() / "workspace")
    config["project_root"] = str(root)
    config["quota_dir"] = config.get("quota_dir") or str(xdg_state_home() / "herdr" / "plugins" / "herdr-agent-quota")
    config["review_root"] = config.get("review_root") or str(Path(root) / "reviews")
    config["product_repo"] = config.get("product_repo") or str(root)
    query = config.setdefault("query", {})
    query["launch_args"] = [str(part).replace("{project_root}", str(root)) for part in query.get("launch_args", [])]
    for entry in (query.get("providers") or {}).values():
        if isinstance(entry, dict):
            entry["launch_args"] = [str(part).replace("{project_root}", str(root)) for part in entry.get("launch_args", [])]
    query["allowed_paths"] = [str(part).replace("{project_root}", str(root)) for part in query.get("allowed_paths", [])]
    return config

def load_config(path: Path) -> dict[str, Any]:
    raw: dict[str, Any] = {}
    if path.exists():
        raw = load_json(path, label="supervisor configuration")
        if not isinstance(raw, dict):
            raise SupervisorError("supervisor configuration must be a JSON object")
    config = resolve_config_defaults(deep_merge(DEFAULT_CONFIG, raw))
    if config.get("schema_version") != SCHEMA_VERSION:
        raise SupervisorError(f"unsupported configuration schema_version: {config.get('schema_version')!r}")
    agents = config.get("agents")
    if not isinstance(agents, dict) or set(agents) != set(PROVIDERS):
        raise SupervisorError("configuration must define exactly the codex and claude agents")
    for provider, settings in agents.items():
        if not isinstance(settings, dict) or not isinstance(settings.get("name"), str) or not settings["name"]:
            raise SupervisorError(f"invalid agent configuration for {provider}")
        if not isinstance(settings.get("quota_file"), str):
            raise SupervisorError(f"agents.{provider}.quota_file must be a string")
        args = settings.get("native_resume_args")
        if not isinstance(args, list) or not all(isinstance(item, str) for item in args):
            raise SupervisorError(f"agents.{provider}.native_resume_args must be a list of strings")
    file_limit = _number(config.get("max_task_file_bytes"))
    if file_limit is None or not 1 <= file_limit <= 8 * 1024 * 1024:
        raise SupervisorError("max_task_file_bytes must be a finite number between 1 and 8388608")
    suffixes = config.get("task_file_suffixes")
    if not isinstance(suffixes, list) or not suffixes or any(not isinstance(x, str) or not re.fullmatch(r"\.[a-z0-9]{1,8}", x) for x in suffixes):
        raise SupervisorError("task_file_suffixes must be a non-empty list of lowercase dotted suffixes")
    for key in ("quota_safety_buffer_seconds", "poll_interval_seconds", "prompt_wait_timeout_ms", "prompt_ack_timeout_ms", "wait_timeout_ms", "read_lines", "quota_recheck_seconds", "quota_snapshot_max_age_seconds", "manual_quota_refresh_min_interval_seconds"):
        value = _number(config.get(key))
        if value is None or value < 0 or value > MAX_EPOCH:
            raise SupervisorError(f"configuration value {key} must be a finite non-negative number")
    if not 30 <= float(config["quota_recheck_seconds"]) <= 86400:
        raise SupervisorError("quota_recheck_seconds must be between 30 and 86400 seconds")
    if not 30 <= float(config["quota_snapshot_max_age_seconds"]) <= 86400:
        raise SupervisorError("quota_snapshot_max_age_seconds must be between 30 and 86400 seconds")
    refreshes = config.get("quota_refresh_commands")
    if not isinstance(refreshes, dict) or set(refreshes) != set(PROVIDERS):
        raise SupervisorError("quota_refresh_commands must name exactly the codex and claude providers")
    for provider, ref in refreshes.items():
        if ref is None:
            continue
        command = config.get(ref) if isinstance(ref, str) else ref
        if not isinstance(command, list) or not command or not all(isinstance(x, str) for x in command):
            raise SupervisorError(f"quota_refresh_commands.{provider} must be null or name a list-of-strings command")
    if int(config["read_lines"]) < 1 or int(config["prompt_wait_timeout_ms"]) < 1 or int(config["prompt_ack_timeout_ms"]) < 1 or int(config["wait_timeout_ms"]) < 1:
        raise SupervisorError("read_lines, prompt_wait_timeout_ms, prompt_ack_timeout_ms and wait_timeout_ms must be at least 1")
    limit = config.get("max_consecutive_auto_turns")
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise SupervisorError("max_consecutive_auto_turns must be an integer between 1 and 1000")
    if not isinstance(config.get("quota_block_dismiss_keys"), list):
        raise SupervisorError("quota_block_dismiss_keys must be a list")
    patterns = config.get("quota_screen_patterns")
    if not isinstance(patterns, dict):
        raise SupervisorError("quota_screen_patterns must be an object")
    for provider in PROVIDERS:
        for pattern in patterns.get(provider, []):
            try:
                re.compile(pattern)
            except re.error as error:
                raise SupervisorError(f"invalid quota_screen_patterns.{provider} entry {pattern!r}: {error}") from error
    reset = config.get("codex_reset")
    if not isinstance(reset, dict) or not isinstance(reset.get("enabled"), bool):
        raise SupervisorError("codex_reset configuration is invalid")
    for key in ("helper_timeout_seconds", "inventory_max_age_seconds", "verification_attempts", "verification_interval_seconds"):
        value = _number(reset.get(key))
        if value is None or value <= 0:
            raise SupervisorError(f"codex_reset.{key} must be positive")
    return config

class WorkerLock:
    """Non-blocking exclusive flock; released automatically when the process exits."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.handle: Any = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, PermissionError) as error:
            handle.close()
            raise SupervisorError("another herdr-supervisor worker already owns this task; refusing to start a second one") from error
        handle.seek(0)
        handle.truncate()
        handle.write(f"{os.getpid()}\n")
        handle.flush()
        self.handle = handle

    def release(self) -> None:
        if self.handle is not None:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()
            self.handle = None

    def __enter__(self) -> "WorkerLock":
        self.acquire()
        return self

    def __exit__(self, *_: Any) -> None:
        self.release()

    @classmethod
    def holder_pid(cls, path: Path) -> int | None:
        """Pid recorded by the current holder (None when unreadable)."""
        try:
            text = path.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        return int(text) if text.isdigit() else None

    @classmethod
    def is_held(cls, path: Path) -> bool:
        """Probe the flock without truncating or writing the lock file."""
        if not path.exists():
            return False
        with path.open("r", encoding="utf-8") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (BlockingIOError, PermissionError):
                return True
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            return False

def _number(value: Any) -> float | None:
    """A finite JSON number, or None. Rejects bool and NaN/Infinity (Python's JSON parser accepts them)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number):
        return None
    return number

def _epoch(value: Any) -> float | None:
    number = _number(value)
    if number is None or number <= 0 or number > MAX_EPOCH:
        return None
    return number

def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")

def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())

def pid_alive(pid: Any) -> bool:
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
