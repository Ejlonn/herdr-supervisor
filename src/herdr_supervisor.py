#!/usr/bin/env python3
"""herdr-supervisor: fail-closed orchestration of existing Herdr Claude/Codex sessions.

The supervisor routes turns between two already-running native agent sessions.
It never decides workflow stages itself: the active agent reads the repository
instructions and declares the next stage in a machine-readable protocol block.

Design invariants (see docs/ARCHITECTURE.md):

* at-most-once task submission: delivery state is persisted before any prompt
  is sent, and a stalled/timed-out prompt is never replayed automatically;
* completion markers are accepted only when they match the current run id AND
  the current turn nonce, so stale terminal scrollback cannot route work;
* provider quota exhaustion waits in the same native session until the latest
  blocking window resets plus a buffer, then continues the same stage;
* `blocked` and `unknown` lifecycle states fail closed to WAIT_USER unless a
  provider quota screen is positively identified and the snapshot agrees;
* context usage is displayed but never used to replace a conversation;
* one worker per state directory, enforced with an OS file lock.
"""

from __future__ import annotations

import argparse
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
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Iterable

import herdr_codex_reset as hcr

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
)
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


# --------------------------------------------------------------------------- paths / io


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
    for key in ("quota_safety_buffer_seconds", "poll_interval_seconds", "prompt_wait_timeout_ms", "wait_timeout_ms", "read_lines", "quota_recheck_seconds", "quota_snapshot_max_age_seconds", "manual_quota_refresh_min_interval_seconds"):
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
    if int(config["read_lines"]) < 1 or int(config["prompt_wait_timeout_ms"]) < 1 or int(config["wait_timeout_ms"]) < 1:
        raise SupervisorError("read_lines, prompt_wait_timeout_ms and wait_timeout_ms must be at least 1")
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


# --------------------------------------------------------------------------- durable state


class StateStore:
    def __init__(self, paths: Paths, clock: Callable[[], float]) -> None:
        self.paths = paths
        self.clock = clock

    def read_state(self, *, required: bool = True) -> dict[str, Any] | None:
        """Read V1 (schema 1) or V2 (schema 2) state without modifying it. V1 fields are
        filled in memory; the file is migrated only on the first V2 write."""
        if not self.paths.state_file.exists():
            if required:
                raise SupervisorError("no supervised task exists (run `herdr-supervisor run TASK` first)")
            return None
        value = load_json(self.paths.state_file, label="supervisor task state")
        if not isinstance(value, dict) or value.get("schema_version") not in (SCHEMA_VERSION, STATE_SCHEMA_VERSION):
            raise SupervisorError("supervisor task state has an unsupported schema")
        if value.get("supervisor_state") not in SUPERVISOR_STATES:
            raise SupervisorError(f"supervisor task state is invalid: {value.get('supervisor_state')!r}")
        if value["schema_version"] == SCHEMA_VERSION:
            value = migrate_state_v1(value)
            value["_loaded_schema"] = SCHEMA_VERSION
        else:
            # Additive V2 evolution: old schema-2 records gain zero reset authority in memory.
            value.setdefault("codex_reset", new_v2_fields(str(value.get("workflow_policy") or "v1"))["codex_reset"])
            validate_state_v2(value)
        return value

    def write_state(self, state: dict[str, Any]) -> None:
        """Write schema 2. If the on-disk file is still V1, keep a one-time protected backup first."""
        if state.pop("_loaded_schema", None) == SCHEMA_VERSION or self._disk_schema() == SCHEMA_VERSION:
            self.backup_v1_state()
            state["migrated_from_v1_at"] = iso_utc(self.clock())
        state["schema_version"] = STATE_SCHEMA_VERSION
        state["updated_at"] = iso_utc(self.clock())
        validate_state_v2(state)
        atomic_write_json(self.paths.state_file, state)

    def _disk_schema(self) -> int | None:
        if not self.paths.state_file.exists():
            return None
        try:
            with self.paths.state_file.open(encoding="utf-8") as handle:
                value = json.load(handle)
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return None
        return value.get("schema_version") if isinstance(value, dict) else None

    def backup_v1_state(self) -> Path | None:
        if self._disk_schema() != SCHEMA_VERSION:
            return None
        self.paths.backups_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = self.paths.backups_dir / f"state.v1.{int(self.clock())}.json"
        if target.exists():
            return target
        raw = self.paths.state_file.read_bytes()
        tmp = target.with_name(target.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
        return target

    @contextlib.contextmanager
    def transaction(self) -> Any:
        """Short-held exclusive lock for every state read-modify-write. Never request the worker
        lock while holding it (fixed order: transaction lock is acquired and released on its own)."""
        self.paths.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.paths.txn_lock_file.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def read_control(self) -> dict[str, Any]:
        if not self.paths.control_file.exists():
            return {"desired": "running", "request_id": None}
        value = load_json(self.paths.control_file, label="supervisor control file")
        if not isinstance(value, dict) or value.get("desired") not in {"running", "paused", "cancelled"}:
            raise SupervisorError("supervisor control file is invalid")
        return value

    def write_control(self, desired: str) -> None:
        if desired not in {"running", "paused", "cancelled"}:
            raise SupervisorError(f"invalid control intent: {desired}")
        atomic_write_json(
            self.paths.control_file,
            {"schema_version": SCHEMA_VERSION, "desired": desired, "request_id": str(uuid.uuid4()), "updated_at": iso_utc(self.clock())},
        )

    def append_log(self, state: dict[str, Any], event: str, **details: Any) -> None:
        run_id = state.get("run_id") or "unassigned"
        self.paths.logs_dir.mkdir(parents=True, exist_ok=True)
        path = self.paths.logs_dir / f"{run_id}.jsonl"
        record = {"time": iso_utc(self.clock()), "event": event, **details}
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())


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


# --------------------------------------------------------------------------- herdr adapter


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


class HerdrCli:
    """Thin adapter over the `herdr` CLI. Every method maps to one supported command."""

    def __init__(self, binary: str) -> None:
        self.binary = binary

    def _run(self, args: Iterable[str], *, timeout: float, json_result: bool = True) -> Any:
        command = [self.binary, *args]
        try:
            completed = subprocess.run(command, check=False, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired as error:
            output = (error.stdout or "") + (error.stderr or "")
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
        return self._run(
            ["agent", "prompt", name, text, "--wait", "--timeout", str(timeout_ms)],
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


def session_identity(agent: dict[str, Any] | None) -> str | None:
    if not isinstance(agent, dict):
        return None
    session = agent.get("agent_session")
    if isinstance(session, dict) and isinstance(session.get("value"), str) and session["value"]:
        return session["value"]
    return None


# --------------------------------------------------------------------------- quota


@dataclasses.dataclass(frozen=True)
class QuotaWindow:
    kind: str
    remaining_percent: float
    resets_at: float

    def blocks_at(self, now: float) -> bool:
        return self.remaining_percent <= 0 and self.resets_at > now


@dataclasses.dataclass(frozen=True)
class QuotaSnapshot:
    provider: str
    fetched_at: float | None
    windows: tuple[QuotaWindow, ...]
    context_used_percent: float | None
    window_source: str


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


def _parse_windows(raw_windows: Any, provider: str) -> tuple[QuotaWindow, ...]:
    if not isinstance(raw_windows, list) or not raw_windows:
        raise QuotaError(f"{provider} quota snapshot has no windows")
    parsed: dict[str, QuotaWindow] = {}
    for raw in raw_windows:
        if not isinstance(raw, dict):
            raise QuotaError(f"{provider} quota window is not an object")
        kind = raw.get("kind")
        if kind not in QUOTA_KINDS:
            continue
        remaining = _number(raw.get("remaining_percent"))
        resets_at = _epoch(raw.get("resets_at"))
        if remaining is None or not 0 <= remaining <= 100:
            raise QuotaError(f"{provider} {kind} remaining_percent is invalid or outside 0-100")
        if resets_at is None:
            raise QuotaError(f"{provider} {kind} resets_at is invalid")
        if kind in parsed:
            raise QuotaError(f"{provider} quota snapshot repeats window {kind}")
        parsed[kind] = QuotaWindow(kind, remaining, resets_at)
    if not parsed:
        raise QuotaError(f"{provider} quota snapshot has no five_hour/weekly windows")
    return tuple(parsed[kind] for kind in QUOTA_KINDS if kind in parsed)


def _select_windows(value: dict[str, Any], provider: str, session_id: str | None) -> tuple[Any, str]:
    """Mirror the quota plugin's `windows_for_session` order; never borrow another session's windows."""
    session_only = value.get("session_quota_only")
    if session_only is not None and not isinstance(session_only, bool):
        raise QuotaError(f"{provider} session_quota_only is invalid")
    session_windows = value.get("session_windows") if isinstance(value.get("session_windows"), dict) else {}
    scopes = value.get("session_quota_scopes") if isinstance(value.get("session_quota_scopes"), dict) else {}
    scope_windows = value.get("quota_scope_windows") if isinstance(value.get("quota_scope_windows"), dict) else {}
    if session_only:
        if not session_id:
            raise QuotaError(f"{provider} quota is session-local but no owned session id is known")
        if session_id not in session_windows:
            raise QuotaError(f"{provider} quota is session-local and has no windows for session {session_id}")
        return session_windows[session_id], f"session_windows[{session_id}]"
    if not session_id:
        return value.get("windows"), "windows"
    scope = scopes.get(session_id)
    if isinstance(scope, str) and scope in scope_windows:
        return scope_windows[scope], f"quota_scope_windows[{scope}]"
    if session_id in session_windows:
        return session_windows[session_id], f"session_windows[{session_id}]"
    if not session_windows and not scopes and not scope_windows:
        return value.get("windows"), "windows"
    raise QuotaError(f"{provider} quota snapshot has keyed windows but none for session {session_id}")


def parse_quota_snapshot(path: Path, provider: str, *, session_id: str | None = None) -> QuotaSnapshot:
    """Parse one plugin snapshot. Fails closed on anything missing or malformed."""
    try:
        with path.open(encoding="utf-8") as handle:
            value = json.load(handle)
    except FileNotFoundError as error:
        raise QuotaError(f"{provider} quota snapshot is missing: {path}") from error
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as error:
        raise QuotaError(f"{provider} quota snapshot is corrupt: {path}: {error}") from error
    if not isinstance(value, dict):
        raise QuotaError(f"{provider} quota snapshot must be a JSON object")
    found_provider = value.get("provider")
    if found_provider not in (None, provider):
        raise QuotaError(f"{provider} quota snapshot belongs to provider {found_provider!r}")
    raw_windows, window_source = _select_windows(value, provider, session_id)
    windows = _parse_windows(raw_windows, provider)
    fetched = value.get("fetched_at_unix")
    fetched_at = _epoch(fetched)
    if fetched is not None and fetched_at is None:
        raise QuotaError(f"{provider} fetched_at_unix is invalid")
    context_percent: float | None = None
    context = value.get("context")
    if session_id and isinstance(value.get("session_contexts"), dict) and isinstance(value["session_contexts"].get(session_id), dict):
        context = value["session_contexts"][session_id]
    if isinstance(context, dict):
        context_percent = _number(context.get("used_percent"))
    return QuotaSnapshot(provider, fetched_at, windows, context_percent, window_source)


def blocking_windows(snapshot: QuotaSnapshot, now: float) -> tuple[QuotaWindow, ...]:
    return tuple(window for window in snapshot.windows if window.blocks_at(now))


def quota_resume_at(blocking: Iterable[QuotaWindow], buffer_seconds: float) -> float:
    return max(window.resets_at for window in blocking) + float(buffer_seconds)


def quota_as_dict(snapshot: QuotaSnapshot, now: float) -> dict[str, Any]:
    return {
        "ok": True,
        "provider": snapshot.provider,
        "fetched_at_unix": snapshot.fetched_at,
        "fetched_at_local": iso_local(snapshot.fetched_at) if snapshot.fetched_at else None,
        "window_source": snapshot.window_source,
        "context_used_percent": snapshot.context_used_percent,
        "windows": [
            {
                "kind": window.kind,
                "remaining_percent": window.remaining_percent,
                "resets_at": window.resets_at,
                "resets_at_utc": iso_utc(window.resets_at),
                "resets_at_local": iso_local(window.resets_at),
                "blocking": window.blocks_at(now),
            }
            for window in snapshot.windows
        ],
    }


# --------------------------------------------------------------------------- protocol


@dataclasses.dataclass(frozen=True)
class ProtocolBlock:
    run_id: str
    turn_id: str
    stage: str
    next_agent: str
    handoff: str
    version: int = 1
    gate: str = "none"
    payload: str = "-"


def _normalize_line(line: str) -> str:
    return _LINE_PREFIX_RE.sub("", line.rstrip())


def _parse_block(lines: list[str], start: int) -> ProtocolBlock | None:
    head = lines[start]
    if head.startswith("HERDR_PROTOCOL=1"):
        keys, version = PROTOCOL_KEYS, 1
    elif head.startswith("HERDR_PROTOCOL=2"):
        keys, version = PROTOCOL_KEYS_V2, 2
    else:
        return None
    if start + len(keys) > len(lines):
        return None
    values: dict[str, str] = {}
    for offset, key in enumerate(keys):
        line = lines[start + offset]
        if "=" not in line:
            return None
        found_key, _, value = line.partition("=")
        if found_key.strip() != key:
            return None
        values[key] = value.strip()
    if values["HERDR_PROTOCOL"] != str(version):
        return None
    run_id, turn_id = values["HERDR_RUN"], values["HERDR_TURN"]
    if not _UUID_RE.match(run_id) or not _UUID_RE.match(turn_id):
        return None
    stage, next_agent, handoff = values["HERDR_STAGE"], values["HERDR_NEXT"], values["HERDR_HANDOFF"]
    if not _STAGE_RE.match(stage) or next_agent not in ROUTES:
        return None
    # Placeholders in the echoed prompt template contain angle brackets; never accept them.
    if not handoff or "<" in handoff or ">" in handoff or len(handoff) > 600:
        return None
    if version == 1:
        return ProtocolBlock(run_id, turn_id, stage, next_agent, handoff)
    gate, payload = values["HERDR_GATE"], values["HERDR_PAYLOAD"]
    if gate not in ("none", *GATE_TYPES):
        return None
    if payload != "-" and (not payload.startswith("/") or "<" in payload or ">" in payload or len(payload) > 1024):
        return None
    return ProtocolBlock(run_id, turn_id, stage, next_agent, handoff, version=2, gate=gate, payload=payload)


def find_protocol_blocks(text: str) -> list[ProtocolBlock]:
    lines = [_normalize_line(line) for line in text.splitlines()]
    blocks: list[ProtocolBlock] = []
    for index, line in enumerate(lines):
        if line.startswith("HERDR_PROTOCOL="):
            block = _parse_block(lines, index)
            if block is not None:
                blocks.append(block)
    return blocks


def parse_protocol(text: str, run_id: str, turn_id: str) -> ProtocolBlock | None:
    """Return the block for the current run+turn, or None. Conflicting blocks are an error."""
    matching = [block for block in find_protocol_blocks(text) if block.run_id == run_id and block.turn_id == turn_id]
    if not matching:
        return None
    if len({(b.version, b.stage, b.next_agent, b.handoff, b.gate, b.payload) for b in matching}) != 1:
        raise SupervisorError("conflicting protocol blocks match the current turn; refusing to route")
    return matching[-1]


# --------------------------------------------------------------------------- V2: state schema 2


def new_v2_fields(workflow_policy: str) -> dict[str, Any]:
    return {
        "workflow_policy": workflow_policy,
        "gate_sequence": 0,
        "event_sequence": 0,
        "last_event": None,
        "pending_gate": None,
        "gate_history": [],
        "approvals": [],
        "approved_plan": None,
        "runtime_policy": None,
        "candidate_sha": None,
        "runtime_evidence": None,
        "push_approval": None,
        "continuation": None,
        "processed_requests": {},
        "recovery_count": 0,
        "migrated_from_v1_at": None,
        "wait_user_requires_action": False,
        "deferred_anomaly": None,
        "final_report": None,
        "codex_reset": {
            "authorized_reset_budget": 0, "used_reset_count": 0,
            "authorization_available_count": None, "authorization_timestamp": None,
            "account_fingerprint": None, "current_reset_sequence": 0,
            "current_redemption_state": "IDLE", "current_idempotency_key": None,
            "blocking_event_id": None, "quota_snapshot_before": None, "quota_snapshot_after": None,
            "last_verified_blocking_event_id": None,
            "reset_inventory_before": None, "reset_inventory_after": None,
            "redemption_started_at": None, "redemption_verified_at": None,
            "quota_block_sequence": 0, "quota_was_usable": True,
            "last_verified_delivery_turn_id": None,
            "last_attempted_blocking_event_id": None,
            "last_attempted_delivery_turn_id": None,
        },
    }


def migrate_state_v1(value: dict[str, Any]) -> dict[str, Any]:
    """In-memory V1 -> V2 view. V1 tasks keep the `v1` policy and all V1 semantics."""
    migrated = dict(value)
    for key, default in new_v2_fields("v1").items():
        migrated.setdefault(key, default)
    reset_defaults = new_v2_fields("v1")["codex_reset"]
    if isinstance(migrated.get("codex_reset"), dict):
        for key, default in reset_defaults.items():
            migrated["codex_reset"].setdefault(key, default)
    return migrated


def validate_state_v2(state: dict[str, Any]) -> None:
    if state.get("workflow_policy") not in WORKFLOW_POLICIES:
        raise SupervisorError(f"state has an invalid workflow_policy: {state.get('workflow_policy')!r}")
    for key in ("gate_sequence", "event_sequence", "recovery_count"):
        if isinstance(state.get(key), bool) or not isinstance(state.get(key), int) or state[key] < 0:
            raise SupervisorError(f"state field {key} is invalid")
    reset = state.get("codex_reset")
    if not isinstance(reset, dict):
        raise SupervisorError("state codex_reset is invalid")
    budget, used = reset.get("authorized_reset_budget"), reset.get("used_reset_count")
    if any(isinstance(x, bool) or not isinstance(x, int) or x < 0 for x in (budget, used)) or used > budget:
        raise SupervisorError("state codex reset budget is invalid")
    if reset.get("current_redemption_state") not in ("IDLE", "RESET_PREPARED", "RESET_CONSUMING", "RESET_VERIFYING", "RESET_RECONCILING", "RESET_VERIFIED"):
        raise SupervisorError("state codex reset redemption state is invalid")
    sequence = reset.get("current_reset_sequence")
    block_sequence = reset.get("quota_block_sequence")
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in (sequence, block_sequence)):
        raise SupervisorError("state codex reset sequence is invalid")
    fingerprint = reset.get("account_fingerprint")
    if fingerprint is not None and (not isinstance(fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", fingerprint)):
        raise SupervisorError("state codex account fingerprint is invalid")
    if not isinstance(reset.get("quota_was_usable"), bool):
        raise SupervisorError("state codex quota transition flag is invalid")
    for key in ("last_verified_delivery_turn_id", "last_attempted_delivery_turn_id"):
        value = reset.get(key)
        if value is not None and (not isinstance(value, str) or not _UUID_RE.match(value)):
            raise SupervisorError(f"state codex reset {key} is invalid")
    attempted_block = reset.get("last_attempted_blocking_event_id")
    if attempted_block is not None and (not isinstance(attempted_block, str) or not re.fullmatch(r"[0-9a-f]{64}", attempted_block)):
        raise SupervisorError("state codex attempted blocking event is invalid")
    phase = reset["current_redemption_state"]
    key, block_id = reset.get("current_idempotency_key"), reset.get("blocking_event_id")
    if phase in {"RESET_PREPARED", "RESET_CONSUMING", "RESET_VERIFYING", "RESET_RECONCILING", "RESET_VERIFIED"}:
        if not isinstance(key, str) or not 16 <= len(key) <= 128 or not isinstance(block_id, str) or not re.fullmatch(r"[0-9a-f]{64}", block_id):
            raise SupervisorError("state codex in-flight reset identity is invalid")
        if not fingerprint:
            raise SupervisorError("state codex in-flight reset account identity is missing")
    elif phase == "IDLE" and key is not None:
        raise SupervisorError("state codex idle reset retains an idempotency key")
    gate = state.get("pending_gate")
    if gate is not None:
        if not isinstance(gate, dict) or gate.get("gate_type") not in GATE_TYPES or gate.get("status") not in ("pending", "approved", "rejected", "revision_requested", "answered", "superseded", "failed"):
            raise SupervisorError("state pending_gate is invalid")
    if state["supervisor_state"] in GATE_STATES and not (isinstance(gate, dict) and gate.get("status") == "pending"):
        raise SupervisorError(f"state {state['supervisor_state']} requires a pending gate")
    wait = state.get("quota_wait")
    if wait is not None:
        if not isinstance(wait, dict) or wait.get("provider") not in PROVIDERS or not isinstance(wait.get("midturn"), bool):
            raise SupervisorError("state quota_wait identity is invalid")
        windows = wait.get("blocking_windows")
        if not isinstance(windows, list) or not windows:
            raise SupervisorError("state quota_wait must preserve blocking windows")
        _parse_windows(windows, str(wait["provider"]))
        for key in ("resume_at",):
            if _epoch(wait.get(key)) is None:
                raise SupervisorError(f"state quota_wait {key} is invalid")
        for key in ("started_at_unix", "next_recheck_unix"):
            if key in wait and _epoch(wait.get(key)) is None:
                raise SupervisorError(f"state quota_wait {key} is invalid")
        for key in ("early_refresh_available", "provider_limit_inferred"):
            if key in wait and not isinstance(wait.get(key), bool):
                raise SupervisorError(f"state quota_wait {key} is invalid")
    anomaly = state.get("deferred_anomaly")
    if anomaly is not None:
        required_text = ("run_id", "turn_id", "provider", "session_id", "delivery_status", "prompt_sha256", "kind", "continuation")
        if not isinstance(anomaly, dict) or any(not isinstance(anomaly.get(key), str) or not anomaly[key] for key in required_text):
            raise SupervisorError("state deferred_anomaly is incomplete")
        if anomaly["run_id"] != state.get("run_id") or not _UUID_RE.fullmatch(anomaly["run_id"]) or not _UUID_RE.fullmatch(anomaly["turn_id"]):
            raise SupervisorError("state deferred_anomaly run/turn identity is invalid")
        if anomaly["provider"] not in PROVIDERS or anomaly["delivery_status"] not in ("accepted", "uncertain"):
            raise SupervisorError("state deferred_anomaly provider/delivery is invalid")
        if not re.fullmatch(r"[0-9a-f]{64}", anomaly["prompt_sha256"]):
            raise SupervisorError("state deferred_anomaly prompt hash is invalid")
        if anomaly["kind"] != "missing_protocol" or anomaly["continuation"] not in ("pending", "submitted", "resolved", "failed"):
            raise SupervisorError("state deferred_anomaly kind/continuation is invalid")
        if not isinstance(anomaly.get("blocking_windows"), list) or not anomaly["blocking_windows"]:
            raise SupervisorError("state deferred_anomaly blocking windows are invalid")
        _parse_windows(anomaly["blocking_windows"], anomaly["provider"])
        for key in ("next_recheck_unix", "reset_deadline_unix"):
            if _epoch(anomaly.get(key)) is None:
                raise SupervisorError(f"state deferred_anomaly {key} is invalid")
    report = state.get("final_report")
    if report is not None:
        if not isinstance(report, dict) or report.get("schema_version") != 1:
            raise SupervisorError("state final_report is invalid")
        if not isinstance(report.get("source_path"), str) or not report["source_path"].endswith("/FINAL_REPORT.md"):
            raise SupervisorError("state final_report source path is invalid")
        if not isinstance(report.get("source_bytes"), int) or isinstance(report.get("source_bytes"), bool) or report["source_bytes"] < 1:
            raise SupervisorError("state final_report size is invalid")
        if not re.fullmatch(r"[0-9a-f]{64}", str(report.get("source_sha256") or "")):
            raise SupervisorError("state final_report hash is invalid")
        if not isinstance(report.get("title"), str) or not report["title"] or len(report["title"]) > 200:
            raise SupervisorError("state final_report title is invalid")


def safe_gate_view(gate: Any) -> dict[str, Any] | None:
    if not isinstance(gate, dict):
        return None
    keys = ("gate_id", "gate_type", "status", "run_id", "turn_id", "created_at", "expires_at", "expected_state",
            "artifact_path", "artifact_sha256", "payload_sha256", "candidate_sha", "summary_fields", "agent")
    return {key: gate.get(key) for key in keys}


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


# --------------------------------------------------------------------------- V2: safe files and payload schemas


def check_safe_file(path_text: str, *, root: Path, max_bytes: int, label: str) -> Path:
    """A regular, non-symlink, owner-owned, bounded file strictly below `root` (no traversal)."""
    if not isinstance(path_text, str) or not path_text.startswith("/") or "\x00" in path_text:
        raise SupervisorError(f"{label} path must be absolute")
    path = Path(path_text)
    try:
        resolved_root = root.resolve(strict=True)
    except OSError as error:
        raise SupervisorError(f"{label} root is unavailable: {root}") from error
    try:
        info = path.lstat()
    except OSError as error:
        raise SupervisorError(f"{label} is missing: {path}") from error
    if os.path.islink(path):
        raise SupervisorError(f"{label} must not be a symlink: {path}")
    if not (info.st_mode & 0o170000) == 0o100000:
        raise SupervisorError(f"{label} must be a regular file: {path}")
    if info.st_uid != os.getuid():
        raise SupervisorError(f"{label} must be owned by the current user: {path}")
    if info.st_size > max_bytes:
        raise SupervisorError(f"{label} exceeds {max_bytes} bytes: {path}")
    resolved = path.resolve()
    if resolved == resolved_root or resolved_root not in resolved.parents:
        raise SupervisorError(f"{label} must be below {root}: {path}")
    # Every directory between the root and the file must be a real directory (no symlinked parents).
    parent = path.parent
    while True:
        if os.path.islink(parent):
            raise SupervisorError(f"{label} path contains a symlink: {parent}")
        try:
            if parent.resolve() == resolved_root:
                break
        except OSError as error:
            raise SupervisorError(f"{label} parent is unavailable: {parent}") from error
        if parent == parent.parent:
            raise SupervisorError(f"{label} must be below {root}: {path}")
        parent = parent.parent
    return path


def _bounded_str(value: Any, label: str, *, max_len: int = 2000, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not value and not allow_empty) or len(value) > max_len:
        raise SupervisorError(f"payload field {label} must be a non-empty string of at most {max_len} characters")
    if any(ord(ch) < 32 and ch not in "\n\t" for ch in value):
        raise SupervisorError(f"payload field {label} contains control characters")
    return value


def _bounded_list(value: Any, label: str, *, max_items: int = 50, max_len: int = 500) -> list[str]:
    if not isinstance(value, list) or len(value) > max_items:
        raise SupervisorError(f"payload field {label} must be a list of at most {max_items} items")
    return [_bounded_str(item, f"{label}[{index}]", max_len=max_len) for index, item in enumerate(value)]


def _bool(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise SupervisorError(f"payload field {label} must be a boolean")
    return value


def validate_gate_payload(raw: Any, gate_type: str, *, config: dict[str, Any]) -> dict[str, Any]:
    """Return the validated, bounded payload subset (agent data, never authority)."""
    if not isinstance(raw, dict):
        raise SupervisorError("gate payload must be a JSON object")
    if raw.get("schema_version") != 1:
        raise SupervisorError("gate payload schema_version must be 1")
    if raw.get("gate_type") != gate_type:
        raise SupervisorError(f"gate payload gate_type {raw.get('gate_type')!r} does not match marker {gate_type}")
    review_root = Path(config["review_root"])
    out: dict[str, Any] = {
        "schema_version": 1,
        "gate_type": gate_type,
        "task_title": _bounded_str(raw.get("task_title"), "task_title", max_len=200),
        "summary": _bounded_str(raw.get("summary"), "summary"),
        "review_directory": _bounded_str(raw.get("review_directory"), "review_directory", max_len=1024),
    }
    review_dir = Path(out["review_directory"])
    if not out["review_directory"].startswith("/") or not review_dir.is_dir() or os.path.islink(review_dir) or review_root.resolve() not in review_dir.resolve().parents:
        raise SupervisorError("review_directory must be an existing directory below the review root")
    if gate_type == "plan_approval":
        out.update({
            "scope": _bounded_str(raw.get("scope"), "scope"),
            "intended_changes": _bounded_list(raw.get("intended_changes"), "intended_changes"),
            "risk_summary": _bounded_str(raw.get("risk_summary"), "risk_summary"),
            "affected_components": _bounded_list(raw.get("affected_components"), "affected_components"),
            "migration_required": _bool(raw.get("migration_required"), "migration_required"),
            "migration_explanation": _bounded_str(raw.get("migration_explanation"), "migration_explanation", allow_empty=True),
            "runtime_validation_required": _bool(raw.get("runtime_validation_required"), "runtime_validation_required"),
            "rebuild_required": _bool(raw.get("rebuild_required"), "rebuild_required"),
            "rebuild_reason": _bounded_str(raw.get("rebuild_reason"), "rebuild_reason", allow_empty=True),
            "push_approval_required": _bool(raw.get("push_approval_required"), "push_approval_required"),
            "plan_path": _bounded_str(raw.get("plan_path"), "plan_path", max_len=1024),
        })
    elif gate_type == "generic_question":
        mode = raw.get("answer_mode")
        if mode not in ("text", "choice"):
            raise SupervisorError("generic_question answer_mode must be text or choice")
        out["question"] = _bounded_str(raw.get("question"), "question", max_len=1000)
        out["answer_mode"] = mode
        out["context"] = _bounded_str(raw.get("context", ""), "context", allow_empty=True)
        if mode == "choice":
            choices = _bounded_list(raw.get("choices"), "choices", max_items=10, max_len=80)
            if len(choices) < 2 or len(set(choices)) != len(choices):
                raise SupervisorError("generic_question choices must be 2-10 distinct entries")
            out["choices"] = choices
        limit = raw.get("max_answer_chars", 500)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 2000:
            raise SupervisorError("generic_question max_answer_chars must be 1-2000")
        out["max_answer_chars"] = limit
    elif gate_type == "runtime_validation":
        sha = _bounded_str(raw.get("candidate_sha"), "candidate_sha", max_len=40)
        if not SHA_RE.match(sha):
            raise SupervisorError("candidate_sha must be a full 40-hex commit SHA")
        repository = _bounded_str(raw.get("repository"), "repository", max_len=1024)
        if Path(repository).resolve() != Path(config["product_repo"]).resolve():
            raise SupervisorError("runtime_validation repository must be the configured product repository")
        if raw.get("runtime_validation_missing") is not True:
            raise SupervisorError("runtime_validation payload must state runtime_validation_missing=true")
        if raw.get("local_gate_result") not in ("PASS", "FAIL"):
            raise SupervisorError("local_gate_result must be PASS or FAIL")
        if raw.get("codex_review_status") not in ("APPROVED", "CHANGES_REQUIRED", "PENDING"):
            raise SupervisorError("codex_review_status must be APPROVED, CHANGES_REQUIRED or PENDING")
        out.update({
            "repository": repository,
            "candidate_sha": sha,
            "prepared_commits": _bounded_list(raw.get("prepared_commits", []), "prepared_commits", max_items=50, max_len=200),
            "affected_services": _bounded_list(raw.get("affected_services", []), "affected_services", max_items=30, max_len=100),
            "rebuild_required": _bool(raw.get("rebuild_required"), "rebuild_required"),
            "rebuild_reason": _bounded_str(raw.get("rebuild_reason"), "rebuild_reason", allow_empty=True),
            "local_evidence_summary": _bounded_str(raw.get("local_evidence_summary"), "local_evidence_summary"),
            "codex_review_status": raw["codex_review_status"],
            "local_gate_result": raw["local_gate_result"],
            "runtime_validation_missing": True,
        })
    elif gate_type == "push_approval":
        sha = _bounded_str(raw.get("candidate_sha"), "candidate_sha", max_len=40)
        if not SHA_RE.match(sha):
            raise SupervisorError("candidate_sha must be a full 40-hex commit SHA")
        if raw.get("local_gate_result") != "PASS":
            raise SupervisorError("push_approval requires local_gate_result=PASS")
        if raw.get("codex_review_status") != "APPROVED":
            raise SupervisorError("push_approval requires codex_review_status=APPROVED")
        out.update({
            "candidate_sha": sha,
            "approved_plan_sha256": _bounded_str(raw.get("approved_plan_sha256", ""), "approved_plan_sha256", max_len=64, allow_empty=True),
            "local_gate_result": "PASS",
            "codex_review_status": "APPROVED",
            "prepared_commits": _bounded_list(raw.get("prepared_commits", []), "prepared_commits", max_items=50, max_len=200),
            "affected_services": _bounded_list(raw.get("affected_services", []), "affected_services", max_items=30, max_len=100),
            "runtime_evidence_status": _bounded_str(raw.get("runtime_evidence_status", ""), "runtime_evidence_status", max_len=40, allow_empty=True),
        })
    else:
        raise SupervisorError(f"unknown gate type {gate_type}")
    return out


def validate_runtime_evidence(raw: Any, *, candidate_sha: str, environment: str, now: float) -> dict[str, Any]:
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise SupervisorError("runtime evidence must be a JSON object with schema_version 1")
    if raw.get("candidate_sha") != candidate_sha:
        raise SupervisorError("runtime evidence candidate_sha does not match the pending candidate")
    if raw.get("environment") != environment:
        raise SupervisorError("runtime evidence environment does not match the command")
    if raw.get("result") not in ("PASS", "FAIL"):
        raise SupervisorError("runtime evidence result must be PASS or FAIL")
    commands = raw.get("commands")
    if not isinstance(commands, list) or not commands or len(commands) > 100:
        raise SupervisorError("runtime evidence must list 1-100 commands")
    parsed_commands = []
    for index, item in enumerate(commands):
        if not isinstance(item, dict):
            raise SupervisorError(f"runtime evidence command {index} must be an object")
        parsed_commands.append({
            "command": _bounded_str(item.get("command"), f"commands[{index}].command", max_len=1000),
            "result": _bounded_str(item.get("result"), f"commands[{index}].result", max_len=1000),
        })
    timestamp = raw.get("timestamp")
    if not isinstance(timestamp, str):
        raise SupervisorError("runtime evidence timestamp must be an ISO-8601 string")
    try:
        moment = dt.datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError as error:
        raise SupervisorError("runtime evidence timestamp is not ISO-8601") from error
    if moment.tzinfo is None:
        raise SupervisorError("runtime evidence timestamp must carry a timezone")
    epoch = moment.timestamp()
    if epoch > now + 300 or epoch < now - 30 * 86400:
        raise SupervisorError("runtime evidence timestamp is in the future or older than 30 days")
    return {"schema_version": 1, "candidate_sha": candidate_sha, "environment": environment, "result": raw["result"], "commands": parsed_commands, "timestamp": timestamp}


def git_head(repo: Path) -> str:
    try:
        completed = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=False, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise SupervisorError(f"cannot read repository HEAD: {error}") from error
    head = (completed.stdout or "").strip()
    if completed.returncode != 0 or not SHA_RE.match(head):
        raise SupervisorError("cannot read repository HEAD")
    return head


_TASK_TEXT_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def validate_task_text(raw: bytes, *, max_bytes: int, label: str = "task file") -> str:
    """Strict UTF-8 text, bounded, no NUL/disallowed control characters, non-empty. Data only."""
    if not raw:
        raise SupervisorError(f"{label} is empty")
    if len(raw) > max_bytes:
        raise SupervisorError(f"{label} exceeds max_task_file_bytes ({max_bytes})")
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise SupervisorError(f"{label} is not strict UTF-8 text") from error
    if text.startswith("\ufeff"):
        text = text[1:]
    if _TASK_TEXT_CONTROL_RE.search(text):
        raise SupervisorError(f"{label} contains NUL or disallowed control characters")
    if not text.strip():
        raise SupervisorError(f"{label} contains no task text")
    return text


def load_task_file(path_text: str, *, config: dict[str, Any], root: Path | None, expected_sha256: str | None = None) -> tuple[str, str, str]:
    """Shared task-file loader for `run <task-file>` and the durable inbox command.

    Returns (decoded text, resolved reference path, sha256). The file must be an absolute, regular,
    non-symlink, current-user-owned file with an allowed suffix, within `max_task_file_bytes`, strict
    UTF-8, and (when given) the exact expected hash. `root` (the supervisor task-file inbox) is mandatory
    for inbox commands; the foreground CLI passes an operator-supplied path with the same checks.
    """
    max_bytes = int(config["max_task_file_bytes"])
    if root is not None:
        path = check_safe_file(path_text, root=root, max_bytes=max_bytes, label="task file")
    else:
        if not isinstance(path_text, str) or not path_text.startswith("/") or "\x00" in path_text:
            raise SupervisorError("task file path must be absolute")
        path = Path(path_text)
        try:
            info = path.lstat()
        except OSError as error:
            raise SupervisorError(f"task file is missing: {path}") from error
        if os.path.islink(path) or not (info.st_mode & 0o170000) == 0o100000:
            raise SupervisorError(f"task file must be a regular, non-symlink file: {path}")
        if info.st_uid != os.getuid():
            raise SupervisorError("task file must be owned by the current user")
        if info.st_size > max_bytes:
            raise SupervisorError(f"task file exceeds max_task_file_bytes ({max_bytes})")
    if path.suffix.lower() not in [x.lower() for x in config["task_file_suffixes"]]:
        raise SupervisorError(f"task file suffix {path.suffix!r} is not allowed (allowed: {', '.join(config['task_file_suffixes'])})")
    with path.open("rb") as handle:
        raw = handle.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise SupervisorError(f"task file exceeds max_task_file_bytes ({max_bytes})")
    digest = sha256_bytes(raw)
    if expected_sha256 is not None and digest != expected_sha256:
        raise SupervisorError("task file content hash does not match the expected hash; refusing")
    text = validate_task_text(raw, max_bytes=max_bytes)
    return text, str(path.resolve()), digest


UPLOAD_STATUSES = ("pending_confirmation", "reset_authorization", "start_enqueued", "started", "cancelled", "expired", "failed", "superseded")
_UPLOAD_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_UPLOAD_KEYS_REQUIRED = {"schema_version", "upload_id", "update_id", "owner_user_id", "chat_id", "file_id", "file_unique_id", "display_filename", "suffix", "declared_size", "bytes", "chars", "sha256", "content_path", "status", "created_at", "expires_at_unix", "snapshot", "interactions", "request_id"}
_UPLOAD_KEYS_OPTIONAL = {"excerpt", "finished_at", "start_enqueued_at", "started_at", "run_id", "declared_mime"}


def validate_upload_record(meta: Any, *, expected_upload_id: str, task_files_dir: Path, config: dict[str, Any]) -> dict[str, Any]:
    """Strict schema + binding check for a persisted upload record. The record's internal id must equal
    the trusted id (filename stem / interaction id) and its content path must be exactly
    `<task_files_dir>/<id><suffix>`. Nothing derived from an unvalidated record may name a path."""
    if not isinstance(meta, dict):
        raise SupervisorError("upload record is not an object")
    keys = set(meta)
    if not _UPLOAD_KEYS_REQUIRED <= keys or not keys <= (_UPLOAD_KEYS_REQUIRED | _UPLOAD_KEYS_OPTIONAL):
        raise SupervisorError("upload record has missing or unknown fields")
    if meta["schema_version"] != 1:
        raise SupervisorError("upload record schema is unsupported")
    upload_id = meta["upload_id"]
    if not isinstance(upload_id, str) or not _UPLOAD_ID_RE.match(upload_id) or upload_id != expected_upload_id:
        raise SupervisorError("upload record id does not match its trusted identity")
    if meta["status"] not in UPLOAD_STATUSES:
        raise SupervisorError("upload record status is invalid")
    for key in ("update_id", "owner_user_id", "chat_id", "declared_size", "bytes", "chars"):
        value = meta[key]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise SupervisorError(f"upload record field {key} is invalid")
    max_bytes = int(config["max_task_file_bytes"])
    if not 1 <= meta["bytes"] <= max_bytes or meta["chars"] < 1 or meta["chars"] > meta["bytes"]:
        raise SupervisorError("upload record size fields are out of bounds")
    for key in ("file_id", "display_filename", "created_at", "content_path"):
        if not isinstance(meta[key], str) or not meta[key] or len(meta[key]) > 1024:
            raise SupervisorError(f"upload record field {key} is invalid")
    if meta["file_unique_id"] is not None and not isinstance(meta["file_unique_id"], str):
        raise SupervisorError("upload record file_unique_id is invalid")
    if meta["request_id"] is not None and (not isinstance(meta["request_id"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", meta["request_id"])):
        raise SupervisorError("upload record request_id is invalid")
    if len(meta["display_filename"]) > 80:
        raise SupervisorError("upload record display_filename is too long")
    suffix = meta["suffix"]
    if not isinstance(suffix, str) or suffix not in [x.lower() for x in config["task_file_suffixes"]]:
        raise SupervisorError("upload record suffix is not allowed")
    if not isinstance(meta["sha256"], str) or not _SHA256_RE.match(meta["sha256"]):
        raise SupervisorError("upload record hash is invalid")
    if meta["content_path"] != str(task_files_dir / f"{upload_id}{suffix}"):
        raise SupervisorError("upload record content path is not the expected controlled path")
    if _number(meta["expires_at_unix"]) is None:
        raise SupervisorError("upload record expiry is invalid")
    snapshot = meta["snapshot"]
    if not isinstance(snapshot, dict) or set(snapshot) != {"task_id", "state"} or not isinstance(snapshot["state"], str) or (snapshot["task_id"] is not None and not isinstance(snapshot["task_id"], str)):
        raise SupervisorError("upload record snapshot is invalid")
    interactions = meta["interactions"]
    if not isinstance(interactions, dict) or not all(isinstance(k, str) and isinstance(v, str) and re.fullmatch(r"[A-Za-z0-9_-]{16,63}", v) for k, v in interactions.items()):
        raise SupervisorError("upload record interactions are invalid")
    if "excerpt" in meta and not isinstance(meta["excerpt"], str):
        raise SupervisorError("upload record excerpt is invalid")
    if "declared_mime" in meta and meta["declared_mime"] is not None and (not isinstance(meta["declared_mime"], str) or len(meta["declared_mime"]) > 80):
        raise SupervisorError("upload record declared_mime is invalid")
    return meta


def telegram_doctor_summary() -> dict[str, Any]:
    """Read-only Telegram subsection for the supervisor doctor; unconfigured is not an error."""
    try:
        import herdr_telegram  # noqa: PLC0415 - optional module, same directory
    except Exception as error:  # noqa: BLE001
        return {"status": "UNAVAILABLE", "detail": f"herdr_telegram module not importable: {type(error).__name__}"}
    try:
        return herdr_telegram.doctor_summary(network=False)
    except Exception as error:  # noqa: BLE001
        return {"status": "ERROR", "detail": type(error).__name__}


def load_query_registry(paths: Paths) -> dict[str, dict[str, Any]]:
    """Versioned registry of separately provisioned query sessions {provider: {agent_name, pane_id,
    session_id}}. A legacy query-owner.json is read as the codex entry. Malformed entries are dropped."""
    providers: dict[str, dict[str, Any]] = {}
    if paths.query_registry_file.exists():
        value = load_json(paths.query_registry_file, label="query provider registry")
        if isinstance(value, dict) and value.get("schema_version") == 1 and isinstance(value.get("providers"), dict):
            for provider, entry in value["providers"].items():
                if provider in PROVIDERS and isinstance(entry, dict) and isinstance(entry.get("session_id"), str) and entry["session_id"] and isinstance(entry.get("agent_name"), str):
                    providers[provider] = {"agent_name": entry["agent_name"], "pane_id": entry.get("pane_id"), "session_id": entry["session_id"]}
    if "codex" not in providers and paths.query_owner_file.exists():
        value = load_json(paths.query_owner_file, label="query owner")
        if isinstance(value, dict) and isinstance(value.get("session_id"), str) and value["session_id"]:
            providers["codex"] = {"agent_name": value.get("agent_name") or "codex-query", "pane_id": value.get("pane_id"), "session_id": value["session_id"]}
    return providers


def query_owner_summary(paths: Paths) -> dict[str, Any]:
    try:
        registry = load_query_registry(paths)
    except SupervisorError as error:
        return {"provisioned": False, "error": str(error), "providers": {}}
    summary = {"provisioned": bool(registry), "providers": {p: {"agent_name": e["agent_name"], "session_id_abbrev": e["session_id"][:8], "pane_id": e.get("pane_id")} for p, e in registry.items()}}
    if "codex" in registry:  # legacy fields kept for existing consumers
        summary.update(agent_name=registry["codex"]["agent_name"], session_id_abbrev=registry["codex"]["session_id"][:8], pane_id=registry["codex"].get("pane_id"))
    return summary


def register_query_provider(
    paths: Paths,
    config: dict[str, Any],
    herdr: HerdrCli,
    *,
    provider: str,
    agent_name: str,
    acknowledge_read_only_contract: bool,
) -> dict[str, Any]:
    """Register an already-provisioned, idle native query session without starting or restoring it."""
    providers = config.get("query", {}).get("providers", {})
    spec = providers.get(provider) if isinstance(providers, dict) else None
    if provider not in PROVIDERS or not isinstance(spec, dict):
        raise SupervisorError("query provider is not configured")
    if not acknowledge_read_only_contract:
        raise SupervisorError(
            "refusing registration until --acknowledge-read-only-contract confirms the configured "
            f"launch contract: {spec.get('read_only_contract', 'unavailable')}"
        )
    expected_name = spec.get("agent_name")
    if not isinstance(expected_name, str) or agent_name != expected_name:
        raise SupervisorError(f"query agent name must match configured alias {expected_name!r}")
    workflow_names = {config["agents"][item]["name"] for item in PROVIDERS}
    if agent_name in workflow_names:
        raise SupervisorError("query agent alias collides with a workflow agent")
    matches = [item for item in herdr.list_agents() if item.get("name") == agent_name]
    if len(matches) != 1:
        raise SupervisorError("query agent must exist exactly once; it is never created or restored automatically")
    live = matches[0]
    if live.get("agent") not in (None, provider):
        raise SupervisorError("query agent kind does not match the requested provider")
    if live.get("agent_status") not in ("idle", "done"):
        raise SupervisorError("query agent must be idle before registration")
    identity = session_identity(live)
    if not identity:
        raise SupervisorError("query agent has no stable native session identity")
    if paths.owners_file.exists():
        owners = load_json(paths.owners_file, label="native session owners")
        if not isinstance(owners, dict):
            raise SupervisorError("native session owners file is invalid")
        workflow_sessions = {
            entry.get("session_id") for entry in owners.values()
            if isinstance(entry, dict)
        }
        if identity in workflow_sessions:
            raise SupervisorError("query agent reuses a workflow native session")
    registry = load_query_registry(paths)
    registry[provider] = {"agent_name": agent_name, "pane_id": live.get("pane_id"), "session_id": identity}
    paths.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    paths.query_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    paths.outbox_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    atomic_write_json(paths.query_registry_file, {"schema_version": 1, "providers": registry})
    return {
        "provider": provider,
        "agent_name": agent_name,
        "pane_id": live.get("pane_id"),
        "session_id_abbrev": identity[:8],
        "read_only_contract": spec.get("read_only_contract"),
    }


# --------------------------------------------------------------------------- V2: supervisor mixin (events, gates, actions, inbox, recovery)


class SupervisorV2Mixin:
    """V2 behaviour attached to Supervisor. Every method here is fail-closed and touches state only
    through the caller's transaction/worker context."""

    # ----- events / outbox

    def emit_event(self, state: dict[str, Any], event_type: str, data: dict[str, Any]) -> str:
        if event_type not in EVENT_TYPES:
            raise SupervisorError(f"unknown event type {event_type}")
        seq = int(state.get("event_sequence") or 0) + 1
        run_id = state.get("run_id") or "unassigned"
        event_id = str(uuid.uuid5(EVENT_NAMESPACE, f"{run_id}:{seq}:{event_type}"))
        now = self.clock()
        event = {
            "schema_version": 1,
            "event_id": event_id,
            "type": event_type,
            "run_id": run_id,
            "sequence": seq,
            "gate_id": (state.get("pending_gate") or {}).get("gate_id") if event_type in ACTIONABLE_EVENTS else None,
            "actionable": event_type in ACTIONABLE_EVENTS,
            "at_unix": now,
            "at_utc": iso_utc(now),
            "expires_at_unix": now + float(self.config.get("event_expiry_seconds", 7 * 86400)),
            "supervisor_state": state.get("supervisor_state"),
            "data": data,
        }
        events_dir = self.paths.outbox_dir / "events"
        delivery_dir = self.paths.outbox_dir / "delivery"
        events_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        delivery_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        atomic_write_json(events_dir / f"{event_id}.json", event)
        sidecar = delivery_dir / f"{event_id}.json"
        if not sidecar.exists():
            atomic_write_json(sidecar, {"event_id": event_id, "status": "pending", "attempts": 0, "updated_at": iso_utc(now)})
        state["event_sequence"] = seq
        state["last_event"] = {"event_id": event_id, "type": event_type, "sequence": seq, "at_utc": iso_utc(now)}
        return event_id

    def list_events(self) -> list[dict[str, Any]]:
        events_dir = self.paths.outbox_dir / "events"
        if not events_dir.exists():
            return []
        out = []
        for path in sorted(events_dir.glob("*.json")):
            try:
                out.append(load_json(path, label="event"))
            except SupervisorError:
                continue
        return sorted(out, key=lambda e: (e.get("run_id", ""), e.get("sequence", 0)))

    def reconcile_outbox(self, state: dict[str, Any]) -> None:
        """Deterministic event ids make recovery idempotent: an event written before an uncommitted
        transition is superseded, a committed transition with a missing event file is recreated."""
        committed = int(state.get("event_sequence") or 0)
        delivery_dir = self.paths.outbox_dir / "delivery"
        for event in self.list_events():
            if event.get("run_id") != state.get("run_id"):
                continue
            if int(event.get("sequence", 0)) > committed:
                sidecar = delivery_dir / f"{event['event_id']}.json"
                record = load_json(sidecar, label="delivery") if sidecar.exists() else {"event_id": event["event_id"], "attempts": 0}
                if record.get("status") != "superseded":
                    record.update(status="superseded", updated_at=iso_utc(self.clock()), reason="uncommitted transition")
                    atomic_write_json(sidecar, record)
        last = state.get("last_event")
        if isinstance(last, dict) and not (self.paths.outbox_dir / "events" / f"{last['event_id']}.json").exists():
            state["event_sequence"] = committed - 1
            self.emit_event(state, last["type"], {"recreated": True})

    # ----- V2 prompts

    def build_prompt_v2(self, state: dict[str, Any], turn_id: str, kind: str) -> str:
        protocol = (
            "When you finish this turn, end your response with exactly one contiguous eight-line block, "
            "each line as KEY=value with no markdown formatting around it:\n"
            "HERDR_PROTOCOL=2\n"
            f"HERDR_RUN={state['run_id']}\n"
            f"HERDR_TURN={turn_id}\n"
            "HERDR_STAGE=<short stage name you completed, e.g. plan, brief, implement, review, fix>\n"
            "HERDR_NEXT=<one of: codex, claude, done, human>\n"
            "HERDR_GATE=<none | plan_approval | generic_question | runtime_validation | push_approval>\n"
            f"HERDR_PAYLOAD=<absolute path of the gate payload JSON below {self.config['review_root']}, or - when HERDR_GATE=none>\n"
            "HERDR_HANDOFF=<one line for the next actor, under 300 characters, no angle brackets>\n"
            "Rules enforced by the supervisor: HERDR_NEXT=human requires a typed HERDR_GATE and a payload file; "
            "codex/claude/done routing requires HERDR_GATE=none and HERDR_PAYLOAD=-. Claude cannot receive an "
            "implementation turn until the human approves the exact CODEX_PLAN.md through a plan_approval gate; "
            "runtime validation and push approval are separate human gates when the approved plan requires them. "
            "Choose the stage from the repository's own workflow instructions. Never reuse a block from an earlier turn."
        )
        if kind.startswith("continuation:"):
            cont = state.get("continuation") or (state.get("delivery") or {}).get("continuation") or {}
            ckind = cont.get("kind")
            note = cont.get("note") or ""
            if ckind == "plan_approved":
                body = (
                    f"Supervised run {state['run_id']}: the human APPROVED the plan with SHA-256 {cont.get('plan_sha256')} "
                    f"(payload {cont.get('payload_sha256')}). Create or update CODEX_BRIEF.md from exactly that approved plan "
                    "per AGENTS.md, then route to the implementation agent with a normal routing block. Do not change the plan."
                )
            elif ckind == "revision":
                body = (
                    f"Supervised run {state['run_id']}: the human requested a REVISION of the pending gate "
                    f"({cont.get('gate_type')}). Human note:\n{note}\n\nUpdate the artifact and payload accordingly and emit a new "
                    "gate block; the prior gate and any prior approval are void."
                )
            elif ckind == "answer":
                body = (
                    f"Supervised run {state['run_id']}: the human ANSWERED your question ({cont.get('gate_id')}).\n"
                    f"Answer:\n{note}\n\nContinue the same stage using this answer."
                )
            elif ckind == "runtime_passed":
                body = (
                    f"Supervised run {state['run_id']}: the human recorded runtime validation PASS for candidate "
                    f"{cont.get('candidate_sha')} on {cont.get('environment')}. Continue the workflow's next stage."
                )
            elif ckind == "push_approved":
                body = (
                    f"Supervised run {state['run_id']}: the human AUTHORIZED the push stage for candidate {cont.get('candidate_sha')}. "
                    "Continue the existing workflow (the human performs the push/PR themselves); do not run git push."
                )
            elif ckind == "reconcile":
                body = (
                    f"Supervised run {state['run_id']}: a provider usage-limit wait has ended. Your previous accepted turn "
                    f"(HERDR_TURN={cont.get('turn_id')}) settled before emitting a valid supervisor protocol result, most likely "
                    "because the provider quota was exhausted. Do NOT redo the previous task and do not assume unfinished work is "
                    "complete. Inspect the work and state already produced in this session and on disk, continue the interrupted "
                    "stage from the current point, and emit the required current supervisor routing result for this turn."
                )
            elif ckind == "quota":
                previous = state.get("interrupted_delivery") or {}
                body = (
                    f"Supervised run {state['run_id']}: a provider usage-limit wait has ended. Your previous turn "
                    f"(HERDR_TURN={previous.get('turn_id')}) was interrupted. Continue the interrupted stage from where it stopped; "
                    "do not redo completed work."
                )
            else:
                body = f"Supervised run {state['run_id']}: continue the current stage.\n{note}"
            return body + "\n\n" + protocol
        if kind == "continuation":  # V1 quota continuation path reused under gated_v2
            previous = state.get("interrupted_delivery") or {}
            return (
                f"Supervised run {state['run_id']}: a provider usage-limit wait has ended. Your previous turn "
                f"(HERDR_TURN={previous.get('turn_id')}) in this same session was interrupted. Continue the interrupted stage "
                "from where it stopped; do not redo completed work.\n\n" + protocol
            )
        handoff = state.get("last_successful_handoff")
        handoff_text = ""
        if isinstance(handoff, dict):
            handoff_text = f"\nPrevious stage: {handoff.get('stage')} (by {handoff.get('from_agent')})\nHandoff from previous agent: {handoff.get('summary')}\n"
        task_context = ""
        if kind == "initial":
            task_context = f"\n\nTask:\n{state['task_text']}"
        elif state.get("task_reference"):
            task_context = f"\n\nAuthoritative task artifact: {state['task_reference']}"
        else:
            task_context = "\n\nThe authoritative task is preserved in this native conversation and supervisor state; continue from the prior handoff without replaying it."
        return (
            f"You are the active agent for supervised run {state['run_id']} (gated workflow). Follow the instructions, role model, "
            f"and workflow rooted at {self.config['project_root']} (CLAUDE.md / AGENTS.md and the context they load). The supervisor "
            "only routes turns and enforces human gates; you decide which workflow stage is correct now and perform only that stage."
            f"{task_context}\n{handoff_text}\n{protocol}"
        )

    # ----- V2 routing and gate creation

    def route_v2(self, state: dict[str, Any], block: ProtocolBlock) -> str:
        source = state["active_agent"]
        policy = state.get("workflow_policy")
        if policy == "gated_v2" and block.version != 2:
            return self.set_wait_user(state, "gated_v2 task received a protocol-1 block; a V2 block is required", requires_action=True)
        if policy != "gated_v2" and block.gate != "none":
            return self.set_wait_user(state, "typed gates require a gated_v2 task; nothing was inferred", requires_action=True)
        if block.next_agent != "human":
            if block.gate != "none" or block.payload != "-":
                return self.set_wait_user(state, f"routing to {block.next_agent} must use HERDR_GATE=none and HERDR_PAYLOAD=-", requires_action=True)
            if policy == "gated_v2":
                problem = self._routing_policy_problem(state, block.next_agent)
                if problem:
                    return self.set_wait_user(state, problem, requires_action=True)
                if block.next_agent == source and block.stage == state.get("phase"):
                    return self.set_wait_user(
                        state,
                        f"{source} routed back to itself without changing stage ({block.stage}); send guidance or cancel instead of repeatedly waking the same agent",
                        requires_action=True,
                    )
            state["last_successful_handoff"] = {"from_agent": source, "to": block.next_agent, "stage": block.stage, "summary": block.handoff, "turn_id": block.turn_id, "at": iso_utc(self.clock())}
            state["phase"] = block.stage
            state["turns_completed"] = int(state.get("turns_completed") or 0) + 1
            if isinstance(state.get("delivery"), dict):
                state["delivery"]["status"] = "completed"
            self._log(state, "protocol_accepted", from_agent=source, next=block.next_agent, stage=block.stage, turn_id=block.turn_id, version=2)
            if block.next_agent == "done":
                state["supervisor_state"] = "DONE"
                state["worker_pid"] = None
                state["completed_at"] = iso_utc(self.clock())
                state["final_report"] = self.final_report_descriptor(state)
                event_data = {"stage": block.stage, "handoff": block.handoff}
                if state["final_report"] is not None:
                    event_data["final_report"] = state["final_report"]
                self.emit_event(state, "TASK_DONE", event_data)
                self.store.write_state(state)
                return "stop"
            state["active_agent"] = block.next_agent
            state["delivery"] = None
            state["supervisor_state"] = "RUNNING"
            self.store.write_state(state)
            return "continue"
        # human route: a typed gate is mandatory
        if block.gate == "none" or block.payload == "-":
            if block.version == 1 or policy != "gated_v2":
                return self.set_wait_user(state, f"{source} routed to human: {block.handoff}")
            return self.set_wait_user(state, "HERDR_NEXT=human requires a typed HERDR_GATE and HERDR_PAYLOAD; nothing was inferred", requires_action=True)
        try:
            gate = self.create_gate(state, block)
        except SupervisorError as error:
            return self.set_wait_user(state, f"gate rejected: {error}", requires_action=True)
        state["phase"] = block.stage
        state["turns_completed"] = int(state.get("turns_completed") or 0) + 1
        if isinstance(state.get("delivery"), dict):
            state["delivery"]["status"] = "completed"
        state["pending_gate"] = gate
        state["supervisor_state"] = gate["expected_state"]
        state["wait_user_reason"] = gate["summary_fields"].get("summary") if gate["expected_state"] == "WAIT_USER" else None
        state["worker_pid"] = None
        event_type = {"plan_approval": "PLAN_APPROVAL_REQUIRED", "generic_question": "QUESTION_ASKED", "runtime_validation": "RUNTIME_VALIDATION_READY", "push_approval": "PUSH_APPROVAL_REQUIRED"}[gate["gate_type"]]
        self.emit_event(state, event_type, {"gate": safe_gate_view(gate), "handoff": block.handoff, "runtime_not_run": gate["gate_type"] == "runtime_validation", "push_blocked": gate["gate_type"] == "runtime_validation"})
        self.store.write_state(state)
        self._log(state, "gate_created", gate_id=gate["gate_id"], gate_type=gate["gate_type"], turn_id=block.turn_id)
        return "stop"

    def final_report_descriptor(self, state: dict[str, Any]) -> dict[str, Any] | None:
        """Return a trusted completion-report descriptor from the approved review directory convention.

        Agents cannot put paths in a done routing block. Only FINAL_REPORT.md beside the still-approved plan
        is eligible; absence or unsafe content leaves Telegram on its bounded summary fallback.
        """
        approved = state.get("approved_plan")
        if not isinstance(approved, dict) or not isinstance(approved.get("plan_path"), str):
            return None
        plan_path = Path(approved["plan_path"])
        review_dir = plan_path.parent
        report_path = review_dir / "FINAL_REPORT.md"
        if not report_path.exists():
            return None
        try:
            safe = check_safe_file(
                str(report_path), root=review_dir, max_bytes=int(self.config["max_artifact_bytes"]), label="final report"
            )
            content = safe.read_bytes()
            if not content:
                raise SupervisorError("final report is empty")
        except (OSError, SupervisorError) as error:
            self._log(state, "final_report_omitted", reason=type(error).__name__)
            return None
        task_title = next((line.strip().lstrip("# ") for line in str(state.get("task_text") or "").splitlines() if line.strip()), "Task")
        return {
            "schema_version": 1,
            "source_path": str(safe),
            "source_sha256": sha256_bytes(content),
            "source_bytes": len(content),
            "title": (task_title[:180] + " final report")[:200],
        }

    def _approved_plan_problem(self, state: dict[str, Any]) -> str | None:
        """F1: the approved plan artifact must still hash to the approved value before any
        policy-sensitive continuation. A mismatch supersedes the approval and everything downstream."""
        approved = state.get("approved_plan")
        if not isinstance(approved, dict):
            return None
        path = Path(str(approved.get("plan_path")))
        current = sha256_file(path) if path.is_file() else None
        if current == approved.get("plan_sha256"):
            return None
        self._invalidate_approval(state, reason=PLAN_CHANGED_MESSAGE)
        return PLAN_CHANGED_MESSAGE

    def _invalidate_approval(self, state: dict[str, Any], *, reason: str) -> None:
        state["superseded_plan"] = {**(state.get("approved_plan") or {}), "superseded_at": iso_utc(self.clock()), "reason": reason}
        state["approved_plan"] = None
        state["runtime_policy"] = None
        state["candidate_sha"] = None
        state["runtime_evidence"] = None
        state["push_approval"] = None
        state["continuation"] = None
        gate = state.get("pending_gate")
        if isinstance(gate, dict) and gate.get("status") == "pending":
            gate["status"] = "superseded"
            gate["resolution_note"] = reason
        self._log(state, "approval_invalidated", reason=reason)

    def _candidate_head_problem(self, state: dict[str, Any]) -> str | None:
        """F4: exact-SHA evidence/approval is only meaningful while repository HEAD equals the candidate."""
        candidate = state.get("candidate_sha")
        if not candidate:
            return None
        try:
            head = self.head_resolver(Path(self.config["product_repo"]))
        except SupervisorError as error:
            return f"repository HEAD unavailable: {error}"
        if head == candidate:
            return None
        self._invalidate_candidate(state, reason=f"repository HEAD {head[:12]} no longer equals candidate {candidate[:12]}")
        return f"candidate changed: repository HEAD {head[:12]} no longer equals candidate {candidate[:12]}; new runtime validation is required"

    def _invalidate_candidate(self, state: dict[str, Any], *, reason: str) -> None:
        state["runtime_evidence"] = None
        state["push_approval"] = None
        state["candidate_sha"] = None
        gate = state.get("pending_gate")
        if isinstance(gate, dict) and gate.get("status") == "pending" and gate.get("gate_type") in ("push_approval", "runtime_validation"):
            gate["status"] = "superseded"
            gate["resolution_note"] = reason
        self._log(state, "candidate_invalidated", reason=reason)

    def _routing_policy_problem(self, state: dict[str, Any], target: str) -> str | None:
        problem = self._approved_plan_problem(state)
        if problem and target in ("claude", "done"):
            return problem
        approved = state.get("approved_plan")
        if target == "claude" and not approved:
            return "claude cannot receive an implementation turn before the human approves an unchanged Codex plan"
        if target == "done":
            if not approved:
                return "done is not allowed before a plan approval exists"
            policy = state.get("runtime_policy") or {}
            if policy.get("runtime_validation_required") or policy.get("push_approval_required"):
                head_problem = self._candidate_head_problem(state)
                if head_problem:
                    return "done is blocked: " + head_problem
            if policy.get("runtime_validation_required") and not self._runtime_pass_current(state):
                return "done is blocked: runtime validation is required and no current exact-SHA PASS evidence exists"
            if policy.get("push_approval_required") and not self._push_approval_current(state):
                return "done is blocked: push approval is required and has not been granted for the current candidate"
        return None

    def _runtime_pass_current(self, state: dict[str, Any]) -> bool:
        evidence = state.get("runtime_evidence")
        return isinstance(evidence, dict) and evidence.get("result") == "PASS" and evidence.get("candidate_sha") == state.get("candidate_sha") and bool(state.get("candidate_sha"))

    def _push_approval_current(self, state: dict[str, Any]) -> bool:
        approval = state.get("push_approval")
        return isinstance(approval, dict) and approval.get("candidate_sha") == state.get("candidate_sha") and bool(state.get("candidate_sha"))

    def create_gate(self, state: dict[str, Any], block: ProtocolBlock) -> dict[str, Any]:
        gate_type = block.gate
        review_root = Path(self.config["review_root"])
        payload_path = check_safe_file(block.payload, root=review_root, max_bytes=int(self.config["max_payload_bytes"]), label="gate payload")
        raw_payload = load_json(payload_path, label="gate payload")
        payload = validate_gate_payload(raw_payload, gate_type, config=self.config)
        if payload_path.resolve().parent != Path(payload["review_directory"]).resolve() and Path(payload["review_directory"]).resolve() not in payload_path.resolve().parents:
            raise SupervisorError("gate payload must live inside its review_directory")
        payload_sha = sha256_bytes(canonical_json(payload))
        if gate_type == "plan_approval":
            artifact = check_safe_file(payload["plan_path"], root=review_root, max_bytes=int(self.config["max_artifact_bytes"]), label="plan artifact")
            if artifact.name != "CODEX_PLAN.md":
                raise SupervisorError("plan_approval artifact must be CODEX_PLAN.md")
            artifact_path, artifact_sha = str(artifact), sha256_file(artifact)
        else:
            artifact_path, artifact_sha = str(payload_path), sha256_file(payload_path)
        candidate = None
        if gate_type in ("runtime_validation", "push_approval"):
            plan_problem = self._approved_plan_problem(state)
            if plan_problem:
                raise SupervisorError(plan_problem)
            if not state.get("approved_plan"):
                raise SupervisorError(f"{gate_type} gate requires an approved plan")
            if payload.get("local_gate_result") != "PASS":
                raise SupervisorError(f"{gate_type} requires local_gate_result=PASS")
            if payload.get("codex_review_status") != "APPROVED":
                raise SupervisorError(f"{gate_type} requires codex_review_status=APPROVED")
        if gate_type == "runtime_validation":
            approved_policy = state.get("runtime_policy") or {}
            if approved_policy.get("rebuild_required") and not payload["rebuild_required"]:
                raise SupervisorError("payload weakens the approved rebuild requirement; a new plan approval is required")
            if not approved_policy.get("runtime_validation_required"):
                raise SupervisorError("approved plan does not require runtime validation; nothing to validate")
            candidate = payload["candidate_sha"]
            if state.get("candidate_sha") and state["candidate_sha"] != candidate:
                state["runtime_evidence"] = None
                state["push_approval"] = None
                self._log(state, "candidate_changed", previous=state["candidate_sha"], current=candidate)
            state["candidate_sha"] = candidate
        elif gate_type == "push_approval":
            approved_policy = state.get("runtime_policy") or {}
            candidate = payload["candidate_sha"]
            if not approved_policy.get("runtime_validation_required"):
                # F2: no runtime gate seeds the candidate; bind it to the repository's current exact HEAD.
                head = self.head_resolver(Path(self.config["product_repo"]))
                if head != candidate:
                    raise SupervisorError(f"push approval candidate {candidate[:12]} does not equal repository HEAD {head[:12]}")
                if state.get("candidate_sha") not in (None, candidate):
                    self._invalidate_candidate(state, reason="candidate replaced by current HEAD")
                state["candidate_sha"] = candidate
            problem = self._push_prerequisite_problem(state, candidate)
            if problem:
                raise SupervisorError(problem)
        elif gate_type == "plan_approval" and state.get("active_agent") != "codex":
            raise SupervisorError("plan_approval gates may only be raised by codex")
        state["gate_sequence"] = int(state.get("gate_sequence") or 0) + 1
        now = self.clock()
        summary_keys = ("task_title", "summary", "scope", "risk_summary", "intended_changes", "affected_components", "migration_required", "runtime_validation_required", "rebuild_required", "rebuild_reason", "push_approval_required", "question", "answer_mode", "choices", "max_answer_chars", "candidate_sha", "prepared_commits", "affected_services", "local_evidence_summary", "codex_review_status", "local_gate_result", "review_directory")
        return {
            "gate_id": str(uuid.uuid4()),
            "gate_type": gate_type,
            "status": "pending",
            "run_id": state["run_id"],
            "turn_id": block.turn_id,
            "agent": state["active_agent"],
            "sequence": state["gate_sequence"],
            "created_at": iso_utc(now),
            "expires_at_unix": now + float(self.config["gate_expiry_seconds"]),
            "expires_at": iso_utc(now + float(self.config["gate_expiry_seconds"])),
            "expected_state": GATE_STATE_FOR[gate_type],
            "payload_path": str(payload_path),
            "payload_sha256": payload_sha,
            "artifact_path": artifact_path,
            "artifact_sha256": artifact_sha,
            "candidate_sha": candidate,
            "payload": payload,
            "summary_fields": {key: payload[key] for key in summary_keys if key in payload},
            "workflow_policy": state.get("workflow_policy"),
        }

    def _push_prerequisite_problem(self, state: dict[str, Any], candidate_sha: str) -> str | None:
        if not state.get("approved_plan"):
            return "push approval requires an approved plan"
        policy = state.get("runtime_policy") or {}
        if not policy.get("push_approval_required"):
            return "approved plan does not require push approval"
        if state.get("candidate_sha") != candidate_sha:
            return "push approval candidate SHA does not match the supervisor's current candidate"
        head_problem = self._candidate_head_problem(state)
        if head_problem:
            return head_problem
        if policy.get("runtime_validation_required") and not self._runtime_pass_current(state):
            return "push approval cannot be presented before current exact-SHA runtime PASS evidence exists"
        return None

    # ----- gate actions (CLI or Telegram-originated, always applied under the transaction lock)

    def _load_pending_gate(self, state: dict[str, Any], run_id: str, gate_id: str, *, expected_state: str | None, gate_types: tuple[str, ...]) -> dict[str, Any]:
        if state.get("run_id") != run_id:
            raise SupervisorError("run_id does not match the current task")
        gate = state.get("pending_gate")
        if not isinstance(gate, dict) or gate.get("gate_id") != gate_id:
            raise SupervisorError("gate_id does not match the pending gate (superseded or unknown)")
        if gate.get("status") != "pending":
            raise SupervisorError(f"gate is already {gate.get('status')} (one-time action)")
        if gate.get("gate_type") not in gate_types:
            raise SupervisorError(f"action not valid for gate type {gate.get('gate_type')}")
        if expected_state is not None and state.get("supervisor_state") != expected_state:
            raise SupervisorError(f"expected supervisor state {expected_state} but found {state.get('supervisor_state')}")
        if state.get("supervisor_state") != gate.get("expected_state"):
            raise SupervisorError("supervisor state no longer matches the gate")
        if self.clock() > float(gate.get("expires_at_unix") or 0):
            raise SupervisorError("gate has expired")
        return gate

    def _check_gate_hashes(self, gate: dict[str, Any], artifact_sha256: str | None, payload_sha256: str | None) -> None:
        current = sha256_file(Path(gate["artifact_path"])) if Path(gate["artifact_path"]).exists() else None
        if current != gate["artifact_sha256"]:
            raise SupervisorError(PLAN_CHANGED_MESSAGE if gate["gate_type"] == "plan_approval" else "gate artifact changed after it was presented")
        if artifact_sha256 is not None and artifact_sha256 != gate["artifact_sha256"]:
            raise SupervisorError(PLAN_CHANGED_MESSAGE if gate["gate_type"] == "plan_approval" else "presented artifact hash does not match the pending gate")
        if payload_sha256 is not None and payload_sha256 != gate["payload_sha256"]:
            raise SupervisorError("presented payload hash does not match the pending gate")

    def _finish_gate(self, state: dict[str, Any], gate: dict[str, Any], status: str, *, actor: str, chat_id: Any, note: str | None = None) -> None:
        gate["status"] = status
        gate["resolved_at"] = iso_utc(self.clock())
        gate["resolved_by"] = {"actor": actor, "chat_id": chat_id}
        if note is not None:
            gate["resolution_note"] = note
        history = state.setdefault("gate_history", [])
        history.append(safe_gate_view(gate))
        del history[:-50]
        state["approvals"].append({"gate_id": gate["gate_id"], "gate_type": gate["gate_type"], "action": status, "actor": actor, "chat_id": chat_id, "at": iso_utc(self.clock()), "artifact_sha256": gate["artifact_sha256"], "payload_sha256": gate["payload_sha256"], "candidate_sha": gate.get("candidate_sha")})
        del state["approvals"][:-200]
        state["pending_gate"] = gate

    def approve_gate(self, state: dict[str, Any], *, run_id: str, gate_id: str, actor: str, chat_id: Any = None, expected_state: str | None = None, artifact_sha256: str | None = None, payload_sha256: str | None = None) -> dict[str, Any]:
        gate = self._load_pending_gate(state, run_id, gate_id, expected_state=expected_state, gate_types=("plan_approval", "push_approval"))
        try:
            self._check_gate_hashes(gate, artifact_sha256, payload_sha256)
        except SupervisorError as error:
            if str(error) == PLAN_CHANGED_MESSAGE:
                self._finish_gate(state, gate, "superseded", actor=actor, chat_id=chat_id, note=PLAN_CHANGED_MESSAGE)
                state["supervisor_state"] = "WAIT_USER"
                state["wait_user_reason"] = PLAN_CHANGED_MESSAGE
                self.emit_event(state, "WAIT_USER", {"reason": PLAN_CHANGED_MESSAGE, "gate_id": gate_id})
                self.store.write_state(state)
            raise
        if gate["gate_type"] == "plan_approval":
            payload = gate["payload"]
            state["approved_plan"] = {"plan_path": gate["artifact_path"], "plan_sha256": gate["artifact_sha256"], "payload_sha256": gate["payload_sha256"], "gate_id": gate_id, "actor": actor, "chat_id": chat_id, "at": iso_utc(self.clock())}
            state["runtime_policy"] = {key: payload[key] for key in ("runtime_validation_required", "rebuild_required", "rebuild_reason", "push_approval_required", "migration_required")}
            state["continuation"] = {"kind": "plan_approved", "gate_id": gate_id, "plan_sha256": gate["artifact_sha256"], "payload_sha256": gate["payload_sha256"]}
            state["active_agent"] = "codex"
            event = "PLAN_APPROVED"
        else:
            problem = self._approved_plan_problem(state) or self._push_prerequisite_problem(state, gate["candidate_sha"])
            if problem:
                if gate.get("status") == "superseded":
                    self._finish_gate(state, gate, "superseded", actor=actor, chat_id=chat_id, note=problem)
                    state["supervisor_state"] = "WAIT_USER"
                    state["wait_user_reason"] = problem
                    state["wait_user_requires_action"] = True
                    self.emit_event(state, "WAIT_USER", {"reason": problem, "gate_id": gate_id})
                    self.store.write_state(state)
                raise SupervisorError(problem)
            evidence = state.get("runtime_evidence") or {}
            state["push_approval"] = {"gate_id": gate_id, "candidate_sha": gate["candidate_sha"], "plan_sha256": state["approved_plan"]["plan_sha256"], "evidence_sha256": evidence.get("evidence_sha256"), "actor": actor, "chat_id": chat_id, "at": iso_utc(self.clock())}
            state["continuation"] = {"kind": "push_approved", "gate_id": gate_id, "candidate_sha": gate["candidate_sha"]}
            event = "PUSH_APPROVED"
        self._finish_gate(state, gate, "approved", actor=actor, chat_id=chat_id)
        state["supervisor_state"] = "RUNNING"
        state["wait_user_reason"] = None
        self.emit_event(state, event, {"gate_id": gate_id, "actor": actor, "artifact_sha256": gate["artifact_sha256"]})
        self.store.write_state(state)
        self._log(state, "gate_approved", gate_id=gate_id, actor=actor)
        return {"ok": True, "message": f"{gate['gate_type']} approved; the supervisor will continue in the same {state['active_agent']} session", "gate_id": gate_id}

    def reject_gate(self, state: dict[str, Any], *, run_id: str, gate_id: str, actor: str, chat_id: Any = None, expected_state: str | None = None, note: str | None = None) -> dict[str, Any]:
        gate = self._load_pending_gate(state, run_id, gate_id, expected_state=expected_state, gate_types=("plan_approval", "push_approval", "runtime_validation"))
        self._finish_gate(state, gate, "rejected", actor=actor, chat_id=chat_id, note=note)
        state["supervisor_state"] = "WAIT_USER"
        state["wait_user_reason"] = f"{gate['gate_type']} rejected by {actor}"
        state["wait_user_requires_action"] = True
        state["continuation"] = None
        self.emit_event(state, "PLAN_REJECTED" if gate["gate_type"] == "plan_approval" else "WAIT_USER", {"gate_id": gate_id, "actor": actor, "artifact_sha256": gate["artifact_sha256"], "reason": state["wait_user_reason"]})
        self.store.write_state(state)
        self._log(state, "gate_rejected", gate_id=gate_id, actor=actor)
        return {"ok": True, "message": f"{gate['gate_type']} rejected; task is in WAIT_USER", "gate_id": gate_id}

    def revise_gate(self, state: dict[str, Any], *, run_id: str, gate_id: str, actor: str, note: str, chat_id: Any = None, expected_state: str | None = None) -> dict[str, Any]:
        note = _bounded_str(note, "revision note", max_len=int(self.config["max_note_chars"]))
        if gate_id == "-":
            return self.guide(state, run_id=run_id, actor=actor, note=note, chat_id=chat_id)
        gate = self._load_pending_gate(state, run_id, gate_id, expected_state=expected_state, gate_types=GATE_TYPES)
        self._finish_gate(state, gate, "revision_requested", actor=actor, chat_id=chat_id, note=note)
        state["continuation"] = {"kind": "revision", "gate_id": gate_id, "gate_type": gate["gate_type"], "note": note}
        state["active_agent"] = gate["agent"]
        state["supervisor_state"] = "RUNNING"
        state["wait_user_reason"] = None
        self.emit_event(state, "REVISION_REQUESTED", {"gate_id": gate_id, "actor": actor, "gate_type": gate["gate_type"]})
        self.store.write_state(state)
        self._log(state, "gate_revision", gate_id=gate_id, actor=actor)
        return {"ok": True, "message": f"revision requested; {gate['agent']} will continue in the same session", "gate_id": gate_id}

    def guide(self, state: dict[str, Any], *, run_id: str, actor: str, note: str, chat_id: Any = None) -> dict[str, Any]:
        """Human guidance for an action-required WAIT_USER without a pending gate (rejected/invalid gate,
        policy violation): a revision continuation to the active agent in the same session."""
        if state.get("run_id") != run_id:
            raise SupervisorError("run_id does not match the current task")
        if state.get("supervisor_state") != "WAIT_USER" or not state.get("wait_user_requires_action"):
            raise SupervisorError("guidance is only accepted for an action-required WAIT_USER; use the pending gate's id otherwise")
        if (state.get("pending_gate") or {}).get("status") == "pending":
            raise SupervisorError("a typed gate is pending; act on it instead")
        state["continuation"] = {"kind": "revision", "gate_id": None, "gate_type": "wait_user", "note": note}
        state["supervisor_state"] = "RUNNING"
        state["wait_user_reason"] = None
        state["wait_user_requires_action"] = False
        self.emit_event(state, "REVISION_REQUESTED", {"gate_id": None, "actor": actor, "gate_type": "wait_user"})
        self.store.write_state(state)
        self._log(state, "guidance", actor=actor)
        return {"ok": True, "message": f"guidance recorded; {state['active_agent']} will continue in the same session", "gate_id": None}

    def answer_gate(self, state: dict[str, Any], *, run_id: str, gate_id: str, actor: str, answer: str, chat_id: Any = None, expected_state: str | None = None) -> dict[str, Any]:
        gate = self._load_pending_gate(state, run_id, gate_id, expected_state=expected_state, gate_types=("generic_question",))
        payload = gate["payload"]
        answer = _bounded_str(answer, "answer", max_len=int(payload["max_answer_chars"]))
        if payload["answer_mode"] == "choice" and answer not in payload["choices"]:
            raise SupervisorError("answer must be one of the allowlisted choices")
        self._finish_gate(state, gate, "answered", actor=actor, chat_id=chat_id, note=answer)
        state["continuation"] = {"kind": "answer", "gate_id": gate_id, "note": answer}
        state["active_agent"] = gate["agent"]
        state["supervisor_state"] = "RUNNING"
        state["wait_user_reason"] = None
        self.emit_event(state, "QUESTION_ANSWERED", {"gate_id": gate_id, "actor": actor})
        self.store.write_state(state)
        return {"ok": True, "message": "answer recorded; the agent will continue in the same session", "gate_id": gate_id}

    def record_runtime_evidence(self, state: dict[str, Any], *, run_id: str, candidate_sha: str, environment: str, evidence_file: str, result: str, actor: str = "cli", head_resolver: Callable[[Path], str] | None = None) -> dict[str, Any]:
        if state.get("run_id") != run_id:
            raise SupervisorError("run_id does not match the current task")
        gate = state.get("pending_gate")
        if not isinstance(gate, dict) or gate.get("gate_type") != "runtime_validation" or gate.get("status") != "pending":
            raise SupervisorError("no pending runtime_validation gate")
        if state.get("supervisor_state") != "WAIT_RUNTIME_VALIDATION":
            raise SupervisorError("supervisor is not waiting for runtime validation")
        plan_problem = self._approved_plan_problem(state)
        if plan_problem:
            state["supervisor_state"] = "WAIT_USER"
            state["wait_user_reason"] = plan_problem
            state["wait_user_requires_action"] = True
            self.emit_event(state, "WAIT_USER", {"reason": plan_problem})
            self.store.write_state(state)
            raise SupervisorError(plan_problem)
        if not state.get("approved_plan"):
            raise SupervisorError("no approved plan")
        if not SHA_RE.match(candidate_sha or ""):
            raise SupervisorError("candidate SHA must be a full 40-hex commit SHA")
        if candidate_sha != gate.get("candidate_sha") or candidate_sha != state.get("candidate_sha"):
            raise SupervisorError("candidate SHA does not match the pending candidate")
        if environment not in self.config["runtime_environments"]:
            raise SupervisorError(f"unknown runtime environment {environment!r}")
        head = (head_resolver or self.head_resolver)(Path(self.config["product_repo"]))
        if head != candidate_sha:
            raise SupervisorError(f"repository HEAD {head[:12]} no longer equals the candidate SHA")
        review_dir = Path(gate["payload"]["review_directory"])
        path = check_safe_file(evidence_file, root=review_dir, max_bytes=int(self.config["max_payload_bytes"]), label="runtime evidence")
        raw = load_json(path, label="runtime evidence")
        evidence = validate_runtime_evidence(raw, candidate_sha=candidate_sha, environment=environment, now=self.clock())
        if evidence["result"] != result:
            raise SupervisorError(f"evidence result {evidence['result']} does not match the {result} command")
        record = {**evidence, "evidence_path": str(path), "evidence_sha256": sha256_file(path), "received_at": iso_utc(self.clock()), "gate_id": gate["gate_id"], "actor": actor}
        state["runtime_evidence"] = record
        if result == "FAIL":
            gate["last_failure"] = {"at": record["received_at"], "evidence_sha256": record["evidence_sha256"]}
            state["pending_gate"] = gate
            self.emit_event(state, "RUNTIME_VALIDATION_FAILED", {"gate_id": gate["gate_id"], "candidate_sha": candidate_sha, "environment": environment})
            self.store.write_state(state)
            return {"ok": True, "message": "runtime validation FAIL recorded; task remains blocked at WAIT_RUNTIME_VALIDATION", "gate_id": gate["gate_id"]}
        self._finish_gate(state, gate, "approved", actor=actor, chat_id=None, note="runtime PASS recorded")
        self.emit_event(state, "RUNTIME_VALIDATION_PASSED", {"gate_id": gate["gate_id"], "candidate_sha": candidate_sha, "environment": environment, "evidence_sha256": record["evidence_sha256"]})
        policy = state.get("runtime_policy") or {}
        if policy.get("push_approval_required"):
            push_gate = self._synthesize_push_gate(state, gate)
            state["pending_gate"] = push_gate
            state["supervisor_state"] = "WAIT_PUSH_APPROVAL"
            state["wait_user_reason"] = None
            self.emit_event(state, "PUSH_APPROVAL_REQUIRED", {"gate": safe_gate_view(push_gate)})
            self.store.write_state(state)
            return {"ok": True, "message": "runtime PASS recorded; push approval is now pending", "gate_id": push_gate["gate_id"]}
        state["continuation"] = {"kind": "runtime_passed", "candidate_sha": candidate_sha, "environment": environment}
        state["supervisor_state"] = "RUNNING"
        state["wait_user_reason"] = None
        self.store.write_state(state)
        return {"ok": True, "message": "runtime PASS recorded; the workflow continues", "gate_id": gate["gate_id"]}

    def _synthesize_push_gate(self, state: dict[str, Any], runtime_gate: dict[str, Any]) -> dict[str, Any]:
        if runtime_gate["payload"].get("local_gate_result") != "PASS" or runtime_gate["payload"].get("codex_review_status") != "APPROVED":
            raise SupervisorError("push gate requires local PASS and an APPROVED Codex review")
        state["gate_sequence"] = int(state.get("gate_sequence") or 0) + 1
        now = self.clock()
        payload = {"schema_version": 1, "gate_type": "push_approval", "task_title": runtime_gate["payload"]["task_title"], "summary": "push stage authorization (synthesized from validated supervisor state)", "review_directory": runtime_gate["payload"]["review_directory"], "candidate_sha": state["candidate_sha"], "approved_plan_sha256": state["approved_plan"]["plan_sha256"], "local_gate_result": "PASS", "codex_review_status": "APPROVED", "runtime_evidence_status": "PASS"}
        evidence = state["runtime_evidence"]
        return {
            "gate_id": str(uuid.uuid4()), "gate_type": "push_approval", "status": "pending", "run_id": state["run_id"], "turn_id": runtime_gate["turn_id"], "agent": runtime_gate["agent"], "sequence": state["gate_sequence"],
            "created_at": iso_utc(now), "expires_at_unix": now + float(self.config["gate_expiry_seconds"]), "expires_at": iso_utc(now + float(self.config["gate_expiry_seconds"])), "expected_state": "WAIT_PUSH_APPROVAL",
            "payload_path": evidence["evidence_path"], "payload_sha256": sha256_bytes(canonical_json(payload)), "artifact_path": evidence["evidence_path"], "artifact_sha256": evidence["evidence_sha256"], "candidate_sha": state["candidate_sha"], "payload": payload,
            "summary_fields": {"task_title": payload["task_title"], "summary": payload["summary"], "candidate_sha": payload["candidate_sha"], "local_gate_result": payload["local_gate_result"], "review_directory": payload["review_directory"], "runtime_evidence_status": "PASS", "prepared_commits": runtime_gate["payload"].get("prepared_commits", []), "affected_services": runtime_gate["payload"].get("affected_services", [])},
            "workflow_policy": state.get("workflow_policy"),
        }

    # ----- durable command inbox (Telegram bridge and CLI write here; only the worker consumes)

    def inbox_paths(self) -> dict[str, Path]:
        base = self.paths.inbox_dir
        return {name: base / name for name in ("pending", "processing", "completed")}

    def enqueue_command(self, command: dict[str, Any]) -> Path:
        request_id = command.get("request_id")
        if not isinstance(request_id, str) or not re.match(r"^[A-Za-z0-9_-]{8,128}$", request_id):
            raise SupervisorError("command request_id must be 8-128 URL-safe characters")
        if command.get("action") not in ("task", "approve", "reject", "revise", "answer", "pause", "resume", "cancel", "refresh_quota"):
            raise SupervisorError(f"unsupported command action {command.get('action')!r}")
        dirs = self.inbox_paths()
        for path in dirs.values():
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
        for path in dirs.values():
            existing = sorted(path.glob(f"*-{request_id}.json"))
            if existing:
                return existing[0]
        # FIFO order comes from a real monotonic-ish nanosecond prefix; identity from the request id.
        target = dirs["pending"] / f"{time.time_ns():020d}-{request_id}.json"
        atomic_write_json(target, {**command, "enqueued_at": iso_utc(self.clock())})
        return target

    def process_inbox(self, *, actions: set[str] | None = None, live_state: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Consume commands under the transaction lock, idempotent per request_id. `processing/` is
        reconciled first (F6): a request stranded by a crash is completed from the state journal when
        its result is already recorded, otherwise applied exactly once now."""
        dirs = self.inbox_paths()
        results: list[dict[str, Any]] = []
        if not dirs["pending"].exists() and not dirs["processing"].exists():
            return results
        candidates = sorted(dirs["processing"].glob("*.json")) + sorted(dirs["pending"].glob("*.json"))
        for source in candidates:
            if actions is not None:
                try:
                    peek = load_json(source, label="inbox command")
                except SupervisorError:
                    peek = {}
                if not isinstance(peek, dict) or peek.get("action") not in actions:
                    continue
            processing = dirs["processing"] / source.name
            if source.parent != dirs["processing"]:
                try:
                    os.rename(source, processing)
                except FileNotFoundError:
                    continue
            with self.store.transaction():
                result = self._apply_command_file(processing, live_state=live_state)
            atomic_write_json(dirs["completed"] / source.name, result)
            with contextlib.suppress(FileNotFoundError):
                processing.unlink()
            results.append(result)
        return results

    def _apply_command_file(self, path: Path, *, live_state: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            command = load_json(path, label="inbox command")
        except SupervisorError as error:
            return {"request_id": path.stem.split("-", 1)[-1], "ok": False, "message": str(error)}
        request_id = command.get("request_id") or path.stem.split("-", 1)[-1]
        state = live_state if live_state is not None else self.store.read_state(required=False)
        if state is not None and request_id in (state.get("processed_requests") or {}):
            prior = dict(state["processed_requests"][request_id])
            if prior.get("notified") is False:
                # bound by apply_command but the crash came before the result was published: publish once now
                prior["notified"] = True
                state["processed_requests"][request_id] = prior
                self.emit_event(state, "COMMAND_RESULT", {"request_id": request_id, "action": command.get("action"), "ok": prior.get("ok"), "message": prior.get("message"), "chat_id": command.get("chat_id"), "callback_query_id": command.get("callback_query_id")})
                self.store.write_state(state)
                if command.get("action") == "task" and command.get("upload_id"):
                    self._mark_upload_started(command, str(prior.get("run_id")))
                if command.get("action") == "task" and command.get("pending_start_id"):
                    self._mark_pending_start_started(command, str(prior.get("run_id")))
            return {**prior, "replayed": True}
        try:
            result = self.apply_command(state, command)
        except SupervisorError as error:
            result = {"ok": False, "message": str(error)}
        result = {"request_id": request_id, "action": command.get("action"), "at": iso_utc(self.clock()), **result}
        state = live_state if live_state is not None else self.store.read_state(required=False)
        if state is not None:
            processed = state.setdefault("processed_requests", {})
            processed[request_id] = {**result, "notified": True}
            if len(processed) > 200:
                for key in sorted(processed, key=lambda k: processed[k].get("at", ""))[:-200]:
                    del processed[key]
            self.emit_event(state, "COMMAND_RESULT", {"request_id": request_id, "action": command.get("action"), "ok": result["ok"], "message": result["message"], "chat_id": command.get("chat_id"), "callback_query_id": command.get("callback_query_id")})
            self.store.write_state(state)
        return result

    def _validate_pending_reset_authorization(self, command: dict[str, Any], task: dict[str, Any]) -> Path | None:
        """Make the supervisor, rather than Telegram, the final authority for a task reset budget."""
        authorization = command.get("codex_reset_authorization")
        if not isinstance(authorization, dict):
            return None
        budget = authorization.get("budget")
        pending_id = command.get("pending_start_id")
        if command.get("source") not in ("telegram", "telegram-upload"):
            return None
        if pending_id is None:
            if budget == 0:
                return None  # explicit fail-closed fallback when inventory was unavailable
            raise SupervisorError("positive Telegram reset budget requires a pending authorization")
        if not isinstance(pending_id, str) or not re.fullmatch(r"[0-9a-f-]{36}", pending_id):
            raise SupervisorError("pending reset authorization identity is invalid")
        path = self.paths.pending_starts_dir / f"{pending_id}.json"
        record = load_json(path, label="pending reset authorization")
        if record.get("status") not in ("start_enqueued", "supervisor_consuming"):
            raise SupervisorError("pending reset authorization is not executable")
        expected_actor = f"telegram:{record.get('owner_user_id')}"
        task_sha = hashlib.sha256(json.dumps(task, sort_keys=True).encode()).hexdigest()
        checks = (
            record.get("pending_id") == pending_id,
            record.get("request_id") == command.get("request_id"),
            record.get("chat_id") == command.get("chat_id"),
            command.get("actor") == expected_actor,
            record.get("task_sha256") == task_sha == command.get("pending_task_sha256"),
            command.get("preallocated_run_id") == pending_id,
            record.get("budget") == budget,
            record.get("available_count") == authorization.get("available_count"),
            record.get("account_fingerprint") == authorization.get("account_fingerprint"),
            self.clock() <= float(record.get("expires_at_unix") or 0),
        )
        if not all(checks):
            raise SupervisorError("pending reset authorization binding is stale or invalid")
        if isinstance(budget, int) and not isinstance(budget, bool) and budget > 0:
            current = self.codex_reset_inventory(f"start-auth-{pending_id}-{uuid.uuid4().hex}")
            if current.account_fingerprint != record.get("account_fingerprint") or current.available_count != record.get("inventory_available_count"):
                raise SupervisorError("Codex account or reset inventory changed after authorization")
        record["status"] = "supervisor_consuming"
        record["supervisor_consuming_at"] = iso_utc(self.clock())
        atomic_write_json(path, record)
        return path

    def _mark_pending_start_started(self, command: dict[str, Any], run_id: str) -> None:
        """Finish pending-start bookkeeping after the run journal is authoritative.

        This is deliberately replayable: a crash after the new run and processed request were persisted
        must not leave the human authorization record indefinitely in ``supervisor_consuming``.
        """
        pending_id = command.get("pending_start_id")
        if not isinstance(pending_id, str) or not re.fullmatch(r"[0-9a-f-]{36}", pending_id):
            return
        path = self.paths.pending_starts_dir / f"{pending_id}.json"
        try:
            record = load_json(path, label="pending reset authorization")
        except SupervisorError:
            return
        if (record.get("request_id") != command.get("request_id")
                or record.get("status") not in ("supervisor_consuming", "started")):
            return
        record.update({"status": "started", "run_id": run_id, "started_at": record.get("started_at") or iso_utc(self.clock())})
        atomic_write_json(path, record)

    def apply_command(self, state: dict[str, Any] | None, command: dict[str, Any]) -> dict[str, Any]:
        action = command.get("action")
        actor = str(command.get("actor") or "unknown")
        chat_id = command.get("chat_id")
        if action == "task":
            if state is not None and state.get("supervisor_state") not in TERMINAL_STATES:
                raise SupervisorError(f"a task is still {state['supervisor_state']}; the supervisor has no task queue")
            text = command.get("task_text")
            task_file = command.get("task_file")
            if (text is None) == (task_file is None):
                raise SupervisorError("task command must carry exactly one of task_text or task_file")
            char_limit = None
            reference = command.get("task_reference")
            if task_file is not None:
                # Uploaded task file: a supervisor-controlled reference below the task-file inbox, read as
                # data through the shared loader with the exact expected hash. Never a shell argument.
                expected = command.get("task_file_sha256")
                if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
                    raise SupervisorError("task_file_sha256 is required for a task file")
                text, reference, _digest = load_task_file(str(task_file), config=self.config, root=self.paths.task_files_dir, expected_sha256=expected)
                char_limit = int(self.config["max_task_file_bytes"])
            if not isinstance(text, str) or not text.strip():
                raise SupervisorError("task_text is required")
            self.verify_or_seed_owners()
            authorization = command.get("codex_reset_authorization")
            task_binding = {key: command[key] for key in ("task_text", "task_file", "task_reference", "task_file_sha256", "upload_id", "source") if key in command}
            pending_authorization = self._validate_pending_reset_authorization(command, task_binding)
            new_state = self.initialize(text, "codex", task_reference=reference, workflow_policy="gated_v2", char_limit=char_limit,
                                        codex_reset_authorization=authorization if isinstance(authorization, dict) else None,
                                        run_id=command.get("preallocated_run_id"))
            result = {"ok": True, "message": f"task started with codex (run {new_state['run_id'][:8]})", "run_id": new_state["run_id"]}
            request_id = command.get("request_id")
            if isinstance(request_id, str) and request_id:
                # F3: bind the accepted request to the new run durably BEFORE any fallible bookkeeping, so a
                # crash here replays as the same successful start and can never start a second run.
                new_state.setdefault("processed_requests", {})[request_id] = {"request_id": request_id, "action": "task", "at": iso_utc(self.clock()), "notified": False, **result}
                new_state["task_command"] = {"request_id": request_id, "upload_id": command.get("upload_id"), "task_reference": reference, "task_file_sha256": command.get("task_file_sha256")}
                self.store.write_state(new_state)
            if pending_authorization is not None:
                self._mark_pending_start_started(command, new_state["run_id"])
            if task_file is not None:
                self._mark_upload_started(command, new_state["run_id"])
            return result
        if state is None:
            raise SupervisorError("no supervised task exists")
        if action == "refresh_quota":
            bound = command.get("run_id")
            if not isinstance(bound, str) or bound != state.get("run_id"):
                raise SupervisorError("refresh_quota must be bound to the current run")
            wait = state.get("quota_wait")
            if state.get("supervisor_state") != "WAIT_QUOTA" or not isinstance(wait, dict):
                raise SupervisorError("no quota wait is active; nothing to refresh")
            last = wait.get("last_manual_refresh_unix")
            minimum = float(self.config["manual_quota_refresh_min_interval_seconds"])
            if last is not None and self.clock() - float(last) < minimum:
                raise SupervisorError(f"quota refresh was already requested {int(self.clock() - float(last))}s ago; wait {int(minimum)}s between manual refreshes")
            wait["manual_refresh_requested"] = True  # the waiting worker rechecks at its next checkpoint; never resends
            wait["last_manual_refresh_unix"] = self.clock()
            self.store.write_state(state)
            return {"ok": True, "message": f"quota recheck requested for {wait.get('provider')}; the worker refreshes without waking any agent" + ("" if wait.get("early_refresh_available") else " (no independent refresh for this provider; cached deadline applies)")}
        if action in ("pause", "cancel", "resume"):
            # F10: controls are bound to the run observed when they were issued and revalidated here,
            # at durable apply time, so a command from an earlier run or from a no-task window can never
            # touch a later task.
            bound = command.get("run_id")
            if not isinstance(bound, str) or not bound:
                raise SupervisorError(f"{action} must be bound to a run id; unbound controls are refused")
            if bound != state.get("run_id"):
                raise SupervisorError(f"{action} was issued for run {bound[:8]} but the current run is {str(state.get('run_id'))[:8]}; ignored")
        if action in ("pause", "cancel"):
            desired = "paused" if action == "pause" else "cancelled"
            if state["supervisor_state"] in TERMINAL_STATES:
                raise SupervisorError(f"task is already {state['supervisor_state']}")
            self.store.write_control(desired)
            state["supervisor_state"] = "PAUSED" if action == "pause" else "CANCELLED"
            state["worker_pid"] = None
            if action == "cancel":
                state["cancel_note"] = "supervision stopped; native Claude/Codex sessions and history preserved"
            self.emit_event(state, "TASK_PAUSED" if action == "pause" else "TASK_CANCELLED", {"actor": actor})
            self.store.write_state(state)
            return {"ok": True, "message": f"{action} applied; native sessions preserved"}
        if action == "resume":
            if state["supervisor_state"] in TERMINAL_STATES:
                raise SupervisorError(f"task is already {state['supervisor_state']}")
            if state["supervisor_state"] in GATE_STATES:
                raise SupervisorError(f"task is waiting at {state['supervisor_state']}; resolve the gate instead")
            if state["supervisor_state"] == "WAIT_USER" and state.get("wait_user_requires_action") and not isinstance(state.get("continuation"), dict):
                raise SupervisorError("this WAIT_USER needs a decision: revise (with a note), answer, or cancel")
            self.store.write_control("running")
            reset = state.get("codex_reset") or {}
            if state["supervisor_state"] == "WAIT_USER" and reset.get("current_redemption_state") == "RESET_RECONCILING":
                state["supervisor_state"] = "RUNNING"
                state["wait_user_reason"] = None
                state["wait_user_requires_action"] = False
                reset["reconcile_requested"] = True
                self.emit_event(state, "TASK_RESUMED", {"actor": actor, "mode": "reset_reconcile"})
                self.store.write_state(state)
                return {"ok": True, "message": "the existing Codex reset operation will be reconciled with its original idempotency key"}
            if state["supervisor_state"] == "WAIT_QUOTA":
                wait = state.get("quota_wait")
                if not isinstance(wait, dict):
                    raise SupervisorError("WAIT_QUOTA state has no quota wait record")
                wait["manual_refresh_requested"] = True
                wait["last_manual_refresh_unix"] = self.clock()
                self.emit_event(state, "TASK_RESUMED", {"provider": wait.get("provider"), "actor": actor, "mode": "quota_refresh"})
                self.store.write_state(state)
                return {"ok": True, "message": "quota refresh requested; the current worker will revalidate without replaying the prompt"}
            gate = state.get("pending_gate") or {}
            if gate.get("status") == "pending":
                state["supervisor_state"] = gate["expected_state"]  # resume returns to the waiting gate
            elif state["supervisor_state"] in {"PAUSED", "WAIT_USER", "ERROR"}:
                state["supervisor_state"] = "RUNNING"
                state["wait_user_reason"] = None
            self.emit_event(state, "TASK_RESUMED", {"actor": actor})
            self.store.write_state(state)
            return {"ok": True, "message": "resume requested; the worker continues in the same sessions"}
        kwargs = {"run_id": str(command.get("run_id")), "gate_id": str(command.get("gate_id")), "actor": actor, "chat_id": chat_id, "expected_state": command.get("expected_state")}
        if action == "approve":
            return self.approve_gate(state, artifact_sha256=command.get("artifact_sha256"), payload_sha256=command.get("payload_sha256"), **kwargs)
        if action == "reject":
            return self.reject_gate(state, note=command.get("note"), **kwargs)
        if action == "revise":
            return self.revise_gate(state, note=str(command.get("note") or ""), **kwargs)
        if action == "answer":
            return self.answer_gate(state, answer=str(command.get("answer") or ""), **kwargs)
        raise SupervisorError(f"unsupported action {action!r}")

    def _mark_upload_started(self, command: dict[str, Any], run_id: str) -> None:
        """Best-effort audit of the upload record's status. Never raises: the run is already bound to
        the request; a failure here is reconciled later by `reconcile_upload_audit`."""
        try:
            self.reconcile_upload_audit(command.get("upload_id"), run_id)
        except Exception as error:  # noqa: BLE001 - audit only
            self._log({"run_id": run_id}, "upload_audit_failed", error=str(error)[:200])

    def reconcile_upload_audit(self, upload_id: Any, run_id: str) -> bool:
        """Move a validated `start_enqueued` record to `started` (idempotent). Returns True when done."""
        if not isinstance(upload_id, str) or not _UPLOAD_ID_RE.match(upload_id):
            return False
        meta_path = self.paths.task_files_dir / f"{upload_id}.json"
        if not meta_path.exists():
            return False
        meta = validate_upload_record(load_json(meta_path, label="upload record"), expected_upload_id=upload_id, task_files_dir=self.paths.task_files_dir, config=self.config)
        if meta["status"] == "started":
            return True
        if meta["status"] != "start_enqueued":
            return False
        meta["status"] = "started"
        meta["run_id"] = run_id
        meta["started_at"] = iso_utc(self.clock())
        meta.pop("excerpt", None)
        atomic_write_json(meta_path, meta)
        return True

    # ----- detached worker entry point (fixed command for the systemd unit)

    def worker(self) -> int:
        """Consume the inbox, reconcile, then continue only states that may safely auto-continue."""
        try:
            lock = WorkerLock(self.paths.lock_file)
            lock.acquire()
        except SupervisorError:
            return 3  # another worker owns the agents; intentional no-op
        try:
            self.process_inbox()
            state = self.store.read_state(required=False)
            if state is None:
                return 0
            if state["supervisor_state"] in TERMINAL_STATES or state["supervisor_state"] in {"PAUSED", "ERROR"}:
                return 0
            with self.store.transaction():
                state = self.store.read_state()
                self.reconcile_outbox(state)
                if not pid_alive(state.get("worker_pid")):
                    state["recovery_count"] = int(state.get("recovery_count") or 0) + 1
                    self.emit_event(state, "RECOVERED_AFTER_RESTART", {"state": state["supervisor_state"], "recovery_count": state["recovery_count"]})
                state["worker_pid"] = os.getpid()
                self.store.write_state(state)
            reset_phase = (state.get("codex_reset") or {}).get("current_redemption_state")
            if state["supervisor_state"] in HUMAN_WAIT_STATES and reset_phase not in {"RESET_PREPARED", "RESET_CONSUMING", "RESET_VERIFYING", "RESET_RECONCILING"}:
                return self._exit_code(state)
            if state["supervisor_state"] == "WAIT_USER" and reset_phase in {"RESET_PREPARED", "RESET_CONSUMING", "RESET_VERIFYING", "RESET_RECONCILING"}:
                state["supervisor_state"] = "RUNNING"
                state["wait_user_reason"] = None
                state["wait_user_requires_action"] = False
                self.store.write_state(state)
            self.verify_persisted_sessions(state)
            return self._guarded(state, lambda: self.run_loop(state, recovering=True))
        finally:
            lock.release()

    # ----- exact native-session recovery

    def locate_by_session(self, session_id: str) -> list[dict[str, Any]]:
        return [agent for agent in self.herdr.list_agents() if session_identity(agent) == session_id]

    def recover_agent(self, provider: str) -> dict[str, Any]:
        """Locate a workflow agent by exact native session id; restore in the recorded pane; as a last
        resort create a non-focused workspace. Ambiguity or a wrong returned identity fails closed."""
        owners = self.owners()
        record = owners[provider]
        name = self.agent_name(provider)
        matches = self.locate_by_session(record["session_id"])
        if len(matches) > 1:
            raise SupervisorError(f"{provider} native session {record['session_id'][:8]} is live in more than one pane; refusing")
        if len(matches) == 1:
            agent = matches[0]
            if agent.get("name") != name or agent.get("pane_id") != record["pane_id"]:
                self._log_owner_locator(provider, agent)
            return agent
        args = [part.format(session_id=record["session_id"]) for part in self.config["agents"][provider]["native_resume_args"]]
        pane_id = record["pane_id"]
        if not self.herdr.pane_available(pane_id):
            created = self.herdr.create_workspace(label=f"herdr-supervisor {provider} recovery", cwd=self.config["project_root"])
            if not isinstance(created, str) or not created:
                raise SupervisorError("workspace creation returned no pane id; refusing to guess")
            pane_id = created
        self.herdr.start_agent(name, kind=provider, pane_id=pane_id, args=args)
        agent = self.herdr.get_agent(name)
        if session_identity(agent) != record["session_id"]:
            raise SupervisorError(f"{name} restored with native session {session_identity(agent)!r}, expected {record['session_id']}; refusing")
        if pane_id != record["pane_id"]:
            self._log_owner_locator(provider, agent)
        return agent

    def _log_owner_locator(self, provider: str, agent: dict[str, Any]) -> None:
        """Update only the convenience locator (pane) of a verified exact session; the id never changes."""
        owners = self.owners()
        if session_identity(agent) != owners[provider]["session_id"]:
            raise SupervisorError("refusing to update a locator for a different native session")
        owners[provider] = {"pane_id": agent.get("pane_id"), "session_id": owners[provider]["session_id"]}
        atomic_write_json(self.paths.owners_file, owners, mode=0o600)


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


# --------------------------------------------------------------------------- supervisor


class Supervisor(SupervisorV2Mixin):
    def __init__(
        self,
        paths: Paths,
        config: dict[str, Any],
        herdr: Any,
        *,
        clock: Callable[[], float] = time.time,
        sleeper: Callable[[float], None] = time.sleep,
        reset_gateway: Any | None = None,
    ) -> None:
        self.paths = paths
        self.config = config
        self.herdr = herdr
        self.clock = clock
        self.sleeper = sleeper
        self.store = StateStore(paths, clock)
        self._live_names: dict[str, str] = {}
        self.head_resolver: Callable[[Path], str] = git_head  # tests inject a fake
        self.reset_gateway = reset_gateway or hcr.JournalGateway(
            paths.codex_reset_dir,
            timeout=float(config["codex_reset"]["helper_timeout_seconds"]),
            sleeper=sleeper,
        )

    def codex_reset_inventory(self, request_id: str) -> hcr.ResetInventory:
        if not self.config["codex_reset"]["enabled"]:
            raise SupervisorError("automatic Codex reset redemption is disabled")
        try:
            inventory = self.reset_gateway.inventory(request_id)
            return hcr.ensure_inventory_fresh(
                inventory,
                now=self.clock(),
                max_age=float(self.config["codex_reset"]["inventory_max_age_seconds"]),
            )
        except hcr.ResetError as error:
            raise SupervisorError(f"Codex reset inventory unavailable: {error}") from error

    def _reset_block_id(self, state: dict[str, Any], blocking: tuple[QuotaWindow, ...]) -> str:
        """Return the durable usable-to-blocked transition identity.

        Window reset timestamps are deliberately excluded: providers may correct them while the same
        quota event remains continuously blocking, which must never authorize another credit.
        """
        reset = state["codex_reset"]
        active = reset.get("blocking_event_id")
        if isinstance(active, str):
            return active
        delivery_turn = (state.get("delivery") or {}).get("turn_id")
        if (reset.get("quota_was_usable") and isinstance(reset.get("last_verified_blocking_event_id"), str)
                and reset.get("last_verified_delivery_turn_id") == delivery_turn):
            return reset["last_verified_blocking_event_id"]
        if (reset.get("quota_was_usable") and isinstance(reset.get("last_attempted_blocking_event_id"), str)
                and reset.get("last_attempted_delivery_turn_id") == delivery_turn):
            return reset["last_attempted_blocking_event_id"]
        sequence = int(reset.get("quota_block_sequence") or 0) + 1
        kinds = ",".join(sorted({window.kind for window in blocking}))
        block_id = hashlib.sha256(f"{state['run_id']}:quota-block:{sequence}:{kinds}".encode()).hexdigest()
        reset.update({"quota_block_sequence": sequence, "quota_was_usable": False, "blocking_event_id": block_id})
        self.store.write_state(state)
        return block_id

    def try_authorized_codex_reset(self, state: dict[str, Any], blocking: tuple[QuotaWindow, ...], *, midturn: bool,
                                   allow_uncertain_retry: bool = False) -> str | None:
        """Use at most one authorized credit for one authoritative blocking event."""
        reset = state["codex_reset"]
        if reset["used_reset_count"] >= reset["authorized_reset_budget"]:
            return None
        block_id = self._reset_block_id(state, blocking)
        if reset.get("last_verified_blocking_event_id") == block_id:
            return None
        if reset.get("last_attempted_blocking_event_id") == block_id:
            return None
        unresolved = {"RESET_PREPARED", "RESET_CONSUMING", "RESET_VERIFYING", "RESET_RECONCILING"}
        reconciling = reset.get("blocking_event_id") == block_id and reset.get("current_redemption_state") in unresolved
        if reset.get("current_redemption_state") == "RESET_RECONCILING" and not allow_uncertain_retry:
            return self.set_wait_user(state, "Codex reset outcome remains uncertain; the same operation requires explicit reconciliation and no credit was retried", requires_action=True)
        if reset.get("blocking_event_id") not in (None, block_id) and reset.get("current_redemption_state") in unresolved:
            return self.set_wait_user(state, "a prior Codex reset is unresolved; no additional credit was consumed", requires_action=True)
        sequence = int(reset.get("current_reset_sequence") or 0) + (0 if reconciling else 1)
        key = reset.get("current_idempotency_key") if reconciling else None
        try:
            before = self.codex_reset_inventory(f"inventory-{state['run_id']}-{sequence}-{uuid.uuid4().hex}")
        except SupervisorError:
            self.emit_event(state, "CODEX_RESET_UNAVAILABLE", {"reason": "inventory unavailable"})
            self.store.write_state(state)
            if allow_uncertain_retry and reconciling:
                return self.set_wait_user(state, "Codex reset reconciliation cannot verify current inventory; the existing operation remains unresolved and no new credit was attempted", requires_action=True)
            return None
        if not reset.get("account_fingerprint") or before.account_fingerprint != reset["account_fingerprint"]:
            return self.set_wait_user(state, "Codex account identity changed or is unavailable; automatic reset refused", requires_action=True)
        if before.available_count < 1 and not reconciling:
            self.emit_event(state, "CODEX_RESET_UNAVAILABLE", {"reason": "no banked resets available"})
            self.store.write_state(state)
            return None
        if before.ordinary_usage_allowed is not False and not reconciling:
            return self.set_wait_user(state, "Codex reset eligibility is not authoritatively confirmed; no credit was consumed", requires_action=True)
        if not key:
            key = str(uuid.uuid5(EVENT_NAMESPACE, f"codex-reset:{state['run_id']}:{sequence}:{block_id}"))
            reset.update({"blocking_event_id": block_id, "current_reset_sequence": sequence,
                          "current_idempotency_key": key, "current_redemption_state": "RESET_PREPARED",
                          "quota_snapshot_before": [dataclasses.asdict(w) for w in blocking]})
            self.store.write_state(state)  # intent immediately precedes the irreversible request
        reset.update({"reset_inventory_before": before.as_dict(), "current_redemption_state": "RESET_CONSUMING",
                      "redemption_started_at": iso_utc(self.clock())})
        self.emit_event(state, "CODEX_RESET_STARTED", {"sequence": sequence, "authorized": reset["authorized_reset_budget"], "used": reset["used_reset_count"]})
        self.store.write_state(state)
        try:
            outcome = self.reset_gateway.consume(key, f"consume-{state['run_id']}-{sequence}")
        except hcr.ResetError:
            reset["current_redemption_state"] = "RESET_RECONCILING"
            self.store.write_state(state)
            return self.set_wait_user(state, "Codex reset outcome is uncertain; reconcile the same operation before any further reset", requires_action=True)
        if outcome == "no_credit":
            reset.update({"current_redemption_state": "IDLE", "current_idempotency_key": None,
                          "last_attempted_blocking_event_id": block_id,
                          "last_attempted_delivery_turn_id": (state.get("delivery") or {}).get("turn_id")})
            self.store.write_state(state)
            return None
        if outcome not in ("reset", "already_redeemed", "nothing_to_reset"):
            reset["current_redemption_state"] = "RESET_RECONCILING"
            self.store.write_state(state)
            return self.set_wait_user(state, "Codex reset returned an unsafe outcome; no further credit will be attempted", requires_action=True)
        reset["current_redemption_state"] = "RESET_VERIFYING"
        self.store.write_state(state)
        verified: tuple[hcr.ResetInventory, QuotaSnapshot] | None = None
        for attempt in range(int(self.config["codex_reset"]["verification_attempts"])):
            refresh_started = self.clock()
            refresh_result = self.refresh_quota(state, "codex")
            try:
                after = self.codex_reset_inventory(f"verify-{state['run_id']}-{sequence}-{attempt}-{uuid.uuid4().hex}")
                snapshot = self.quota("codex")
                max_age = float(self.config["quota_snapshot_max_age_seconds"])
                fresh_quota = (
                    refresh_result == "ok"
                    and snapshot.fetched_at is not None
                    and snapshot.fetched_at >= refresh_started - 1
                    and snapshot.fetched_at <= self.clock() + 60
                    and self.clock() - snapshot.fetched_at <= max_age
                )
                if (fresh_quota and after.fetched_at_unix >= refresh_started - 1
                        and after.account_fingerprint == reset["account_fingerprint"]
                        and after.ordinary_usage_allowed is True
                        and not blocking_windows(snapshot, self.clock())):
                    verified = (after, snapshot)
                    break
            except (SupervisorError, QuotaError):
                pass
            self.sleeper(float(self.config["codex_reset"]["verification_interval_seconds"]))
        if verified is None:
            reset["current_redemption_state"] = "RESET_RECONCILING"
            self.store.write_state(state)
            return self.set_wait_user(state, "Codex reset may have been consumed but quota recovery could not be verified; no second reset will be used", requires_action=True)
        after, snapshot = verified
        consumed = outcome in ("reset", "already_redeemed")
        reset.update({"used_reset_count": reset["used_reset_count"] + (1 if consumed else 0), "current_redemption_state": "RESET_VERIFIED",
                      "reset_inventory_after": after.as_dict(), "quota_snapshot_after": quota_as_dict(snapshot, self.clock()),
                      "redemption_verified_at": iso_utc(self.clock()), "last_verified_blocking_event_id": block_id,
                      "last_verified_delivery_turn_id": (state.get("delivery") or {}).get("turn_id")})
        self.emit_event(state, "CODEX_RESET_VERIFIED", {"sequence": sequence, "authorized": reset["authorized_reset_budget"], "used": reset["used_reset_count"], "available": after.available_count})
        self.store.write_state(state)
        reset.update({"current_redemption_state": "IDLE", "current_idempotency_key": None,
                      "blocking_event_id": None, "quota_was_usable": True})
        self.store.write_state(state)
        return self.resume_interrupted_turn(state, "codex") if midturn else "continue"

    # ----- configuration helpers

    def agent_name(self, provider: str) -> str:
        """Configured alias, or the live alias under which the exact native session was found."""
        return self._live_names.get(provider) or self.config["agents"][provider]["name"]

    def quota_path(self, provider: str) -> Path:
        return Path(self.config["quota_dir"]) / self.config["agents"][provider]["quota_file"]

    def quota(self, provider: str, *, session_id: str | None = None) -> QuotaSnapshot:
        if session_id is None:
            with contextlib.suppress(SupervisorError):
                session_id = self.owners()[provider]["session_id"]
        return parse_quota_snapshot(self.quota_path(provider), provider, session_id=session_id)

    def quota_refresh_command(self, provider: str) -> list[str] | None:
        ref = (self.config.get("quota_refresh_commands") or {}).get(provider)
        if ref is None:
            return None
        command = self.config.get(ref) if isinstance(ref, str) else ref
        return list(command) if isinstance(command, list) else None

    def refresh_quota(self, state: dict[str, Any], provider: str) -> str:
        """Run the provider's configured NON-LLM refresh mechanism: 'ok', 'failed', or 'unavailable'.
        Never prompts, reads, or wakes an agent; a provider without a refresh command is left alone."""
        command = self.quota_refresh_command(provider)
        if command is None:
            self._log(state, "quota_refresh_unavailable", provider=provider)
            return "unavailable"
        quota_path = self.quota_path(provider)
        try:
            before_marker = (quota_path.stat().st_mtime_ns, self._snapshot_fetched_at(provider))
        except OSError:
            before_marker = (None, None)
        try:
            refresh_started = self.clock()
            self.herdr.run_command(command)
            # Herdr command acceptance and plugin file publication are separate lifecycle events. Wait a
            # bounded two seconds for the atomic snapshot to change; otherwise every recheck can race the
            # writer and reject the newly published evidence forever. This is deterministic and uses no LLM.
            for _ in range(20):
                try:
                    marker = (quota_path.stat().st_mtime_ns, self._snapshot_fetched_at(provider))
                except OSError:
                    marker = (None, None)
                if marker != before_marker and marker[1] is not None and marker[1] >= refresh_started - 1:
                    break
                self.sleeper(0.1)
            self._log(state, "quota_refreshed", provider=provider)
            return "ok"
        except HerdrError as error:
            self._log(state, "quota_refresh_failed", provider=provider, error=str(error)[:200])
            if provider == "codex":
                self._log(state, "codex_quota_refresh_failed", error=str(error)[:200])  # V1 log name kept
            return "failed"

    def quota_screen_confirmed(self, provider: str, output: str) -> bool:
        patterns = self.config.get("quota_screen_patterns", {}).get(provider, [])
        return any(re.search(pattern, output, re.IGNORECASE) for pattern in patterns)

    def inferred_provider_limit_windows(self, snapshot: QuotaSnapshot, output: str) -> tuple[QuotaWindow, ...]:
        """Infer the exhausted window when the provider itself rejected a settled accepted turn but its
        percentage is rounded above zero. Explicit five-hour/weekly wording wins; a generic session/usage
        limit binds to the lowest remaining future window. This uses no fixed percentage threshold."""
        now = self.clock()
        future = tuple(window for window in snapshot.windows if window.resets_at > now)
        if not future or not self.quota_screen_confirmed(snapshot.provider, output):
            return ()
        lowered = output.lower()
        if "weekly" in lowered:
            named = tuple(window for window in future if window.kind == "weekly")
            if named:
                return named
        if re.search(r"(?:five.hour|5.hour|5h|session limit)", lowered):
            named = tuple(window for window in future if window.kind == "five_hour")
            if named:
                return named
        minimum = min(window.remaining_percent for window in future)
        return tuple(window for window in future if window.remaining_percent == minimum)

    # ----- native session ownership

    def owners(self) -> dict[str, dict[str, str]]:
        value = load_json(self.paths.owners_file, label="native session ownership (owners.json)")
        if not isinstance(value, dict):
            raise SupervisorError("owners.json must be an object")
        for provider in PROVIDERS:
            record = value.get(provider)
            if not isinstance(record, dict) or not isinstance(record.get("pane_id"), str) or not isinstance(record.get("session_id"), str):
                raise SupervisorError(f"owners.json is missing a valid {provider} record")
        return value

    def verify_or_seed_owners(self) -> dict[str, dict[str, str]]:
        """Verify live agents against owners.json; seed the file only when it does not exist."""
        live_agents = self.herdr.list_agents()
        existing = self.owners() if self.paths.owners_file.exists() else None
        resolved: dict[str, dict[str, str]] = {}
        for provider in PROVIDERS:
            name = self.agent_name(provider)
            if existing is not None:
                expected = existing[provider]["session_id"]
                matches = [item for item in live_agents if session_identity(item) == expected]
                if len(matches) > 1:
                    raise SupervisorError(f"{name} native session is present in more than one pane; refusing")
                agent = matches[0] if len(matches) == 1 else None
                if agent is None:
                    pane_id = existing[provider]["pane_id"]
                    transient = [
                        item for item in live_agents
                        if item.get("pane_id") == pane_id and item.get("agent") == provider
                        and item.get("agent_status") == "working" and session_identity(item) is None
                    ]
                    if len(transient) == 1:
                        raise SupervisorError(
                            f"{name} is still working in its recorded pane and Herdr temporarily omitted its native identity; retry Start Task after that turn settles"
                        )
                    aliases = [item for item in live_agents if item.get("name") == name]
                    if len(aliases) == 1:
                        alias_identity = session_identity(aliases[0])
                        if alias_identity and alias_identity != expected:
                            raise SupervisorError(
                                f"{name} native session {alias_identity} differs from recorded {expected}; "
                                "refusing to silently adopt a different conversation"
                            )
                    raise SupervisorError(
                        f"required live agent is missing: {name} (recorded native session was not detected; refusing to adopt another conversation)"
                    )
            else:
                aliases = [item for item in live_agents if item.get("name") == name]
                if len(aliases) != 1:
                    raise SupervisorError(f"required live agent is missing or ambiguous: {name}")
                agent = aliases[0]
            identity = session_identity(agent)
            pane_id = agent.get("pane_id")
            if not identity or not isinstance(pane_id, str):
                raise SupervisorError(f"{name} has no native session identity or pane; refusing to supervise it")
            if existing is not None:
                if existing[provider]["session_id"] != identity:
                    raise SupervisorError(
                        f"{name} native session {identity} differs from recorded {existing[provider]['session_id']}; "
                        "refusing to silently adopt a different conversation"
                    )
                if existing[provider]["pane_id"] != pane_id:
                    raise SupervisorError(f"{name} pane {pane_id} differs from recorded {existing[provider]['pane_id']}")
            # Herdr accepts either an agent alias or a pane id as TARGET. During an active native session
            # the convenience alias can be absent even though the exact persisted session is healthy.
            # Retain a usable live target so later get/read/wait/prompt calls do not fall back to that
            # missing alias after ownership verification succeeded.
            self._live_names[provider] = agent.get("name") or pane_id
            resolved[provider] = {"pane_id": pane_id, "session_id": identity}
        if existing is None:
            atomic_write_json(self.paths.owners_file, resolved, mode=0o644)
        return existing if existing is not None else resolved

    def verify_persisted_sessions(self, state: dict[str, Any]) -> None:
        """On resume: owners.json must match the in-flight task's native sessions. Live agents are
        not required here; a missing one is restored later by ensure_agent in its recorded pane."""
        owners = self.owners()
        persisted = state.get("native_sessions")
        if not isinstance(persisted, dict):
            raise SupervisorError("task state has no persisted native_sessions; refusing to resume")
        for provider in PROVIDERS:
            expected = persisted.get(provider)
            if not isinstance(expected, str) or not expected:
                raise SupervisorError(f"task state has no persisted {provider} native session; refusing to resume")
            if owners[provider]["session_id"] != expected:
                raise SupervisorError(
                    f"owners.json {provider} session {owners[provider]['session_id']} differs from the in-flight task's "
                    f"{expected}; refusing to adopt a different conversation"
                )

    def ensure_agent(self, provider: str, *, allow_restore: bool = True) -> dict[str, Any]:
        """Return the live agent, verified against the recorded native session id."""
        name = self.agent_name(provider)
        owners = self.owners()
        record = owners[provider]
        try:
            agent = self.herdr.get_agent(name)
        except HerdrError as error:
            if error.code != "agent_not_found" or not allow_restore:
                raise
            agent = self.recover_agent(provider)
        identity = session_identity(agent)
        if identity != record["session_id"]:
            # The alias may have been reused: the exact native session is the identity, not the name.
            matches = self.locate_by_session(record["session_id"]) if allow_restore else []
            if len(matches) != 1:
                raise SupervisorError(f"{name} native session is {identity!r}, expected {record['session_id']}; refusing to continue")
            agent = matches[0]
            self._log_owner_locator(provider, agent)
        if isinstance(agent.get("name"), str) and agent["name"]:
            self._live_names[provider] = agent["name"]
        else:
            self._live_names[provider] = record["pane_id"]
        status = agent.get("agent_status")
        if status not in LIFECYCLE_STATES:
            raise SupervisorError(f"{name} reported an invalid lifecycle state: {status!r}")
        return agent

    def read_output(self, provider: str, *, settled: bool) -> str:
        """Full unwrapped history when settled; visible screen while working (alternate screen)."""
        name = self.agent_name(provider)
        if settled:
            try:
                return self.herdr.read_agent(name, source="recent-unwrapped", lines=int(self.config["read_lines"]))
            except HerdrError as error:
                if error.code != "agent_not_idle":
                    raise
        return self.herdr.read_agent(name, source="visible", lines=None)

    # ----- state transitions

    def _log(self, state: dict[str, Any], event: str, **details: Any) -> None:
        self.store.append_log(state, event, **details)

    def set_wait_user(self, state: dict[str, Any], reason: str, *, requires_action: bool = False) -> str:
        """requires_action=True: a plain `resume` must not clear this wait; the human must revise/answer/cancel."""
        state["supervisor_state"] = "WAIT_USER"
        state["wait_user_reason"] = reason
        state["wait_user_requires_action"] = requires_action
        if requires_action and isinstance(state.get("delivery"), dict) and state["delivery"].get("status") in ("accepted", "uncertain"):
            state["delivery"]["status"] = "completed"
            state["delivery"]["outcome"] = "rejected_by_policy"
        state["worker_pid"] = None
        self.emit_event(state, "WAIT_USER", {"reason": reason})
        self.store.write_state(state)
        self._log(state, "wait_user", reason=reason)
        return "stop"

    def check_control(self, state: dict[str, Any]) -> bool:
        # F5: the active worker consumes durable pause/cancel requests itself at every deterministic
        # checkpoint (no second worker, no pane input); other commands wait for the next worker start.
        with contextlib.suppress(SupervisorError):
            self.process_inbox(actions={"pause", "cancel", "resume", "refresh_quota"}, live_state=state)
        desired = self.store.read_control()["desired"]
        if desired == "running":
            return True
        target = "PAUSED" if desired == "paused" else "CANCELLED"
        if state["supervisor_state"] == target:
            return False
        state["supervisor_state"] = target
        state["worker_pid"] = None
        if desired == "cancelled":
            state["cancel_note"] = "supervision stopped; native Claude/Codex sessions and history preserved"
        self.emit_event(state, "TASK_PAUSED" if desired == "paused" else "TASK_CANCELLED", {})
        self.store.write_state(state)
        self._log(state, desired)
        return False

    # ----- prompts

    def build_prompt(self, state: dict[str, Any], turn_id: str, kind: str) -> str:
        if state.get("workflow_policy") == "gated_v2":
            return self.build_prompt_v2(state, turn_id, kind)
        protocol = (
            "When you finish this turn, end your response with exactly one contiguous six-line block, "
            "each line as KEY=value with no markdown formatting around it:\n"
            "HERDR_PROTOCOL=1\n"
            f"HERDR_RUN={state['run_id']}\n"
            f"HERDR_TURN={turn_id}\n"
            "HERDR_STAGE=<short stage name you completed, e.g. plan, implement, review, fix>\n"
            "HERDR_NEXT=<one of: codex, claude, done, human>\n"
            "HERDR_HANDOFF=<one line for the next actor, under 300 characters, no angle brackets>\n"
            "Choose HERDR_NEXT from the repository's own workflow instructions and the actual task state: "
            "'human' for an approval, an unresolved decision, or unsafe ambiguity; 'done' only when the whole task "
            "is complete under that workflow. Never reuse a block from an earlier turn."
        )
        if kind == "continuation:reconcile":
            cont = state.get("continuation") or (state.get("delivery") or {}).get("continuation") or {}
            return (
                f"Supervised run {state['run_id']}: a provider usage-limit wait has ended. Your previous accepted turn "
                f"(HERDR_TURN={cont.get('turn_id')}) settled before emitting a valid supervisor protocol result, most likely "
                "because the provider quota was exhausted. Do NOT redo the previous task and do not assume unfinished work is "
                "complete. Inspect the work already produced, continue the interrupted stage from the current point, and emit "
                "the required supervisor block for this turn.\n\n" + protocol
            )
        if kind == "continuation":
            previous = state.get("interrupted_delivery") or {}
            return (
                f"Supervised run {state['run_id']}: a provider usage-limit wait has ended. Your previous turn "
                f"(HERDR_TURN={previous.get('turn_id')}) in this same session was interrupted. Inspect the conversation "
                "and the filesystem, then continue the interrupted stage from where it stopped. Do not redo completed "
                "work and do not restart the task. Follow the repository's existing workflow.\n\n" + protocol
            )
        handoff = state.get("last_successful_handoff")
        handoff_text = ""
        if isinstance(handoff, dict):
            handoff_text = (
                f"\nPrevious stage: {handoff.get('stage')} (by {handoff.get('from_agent')})\n"
                f"Handoff from previous agent: {handoff.get('summary')}\n"
            )
        return (
            f"You are the active agent for supervised run {state['run_id']}. Follow the instructions, role model, and "
            f"workflow rooted at {self.config['project_root']} (CLAUDE.md / AGENTS.md and the context they load). "
            "The supervisor only routes turns between codex and claude; you decide which workflow stage is correct now "
            "and perform only that stage.\n\n"
            f"Task:\n{state['task_text']}\n{handoff_text}\n{protocol}"
        )

    def _new_delivery(self, state: dict[str, Any], kind: str) -> tuple[str, str]:
        turn_id = str(uuid.uuid4())
        prompt = self.build_prompt(state, turn_id, kind)
        state["delivery"] = {
            "turn_id": turn_id,
            "agent": state["active_agent"],
            "kind": kind,
            "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            "status": "prepared",
            "prepared_at": iso_utc(self.clock()),
        }
        state["supervisor_state"] = "RUNNING"
        self.store.write_state(state)
        self._log(state, "prompt_prepared", agent=state["active_agent"], turn_id=turn_id, kind=kind)
        return turn_id, prompt

    def submit(self, state: dict[str, Any], kind: str) -> str:
        provider = state["active_agent"]
        name = self.agent_name(provider)
        turn_id, prompt = self._new_delivery(state, kind)
        delivery = state["delivery"]
        if kind.startswith("continuation:") and isinstance(state.get("continuation"), dict):
            delivery["continuation"] = state["continuation"]
            state["continuation"] = None
            self.store.write_state(state)
        try:
            self.herdr.prompt(name, prompt, timeout_ms=int(self.config["prompt_wait_timeout_ms"]))
        except HerdrError as error:
            if error.code in {"agent_blocked", "agent_not_found"}:
                # Both responses reject the request before any input is sent. They are safe to distinguish
                # from timeout/stall outcomes, where delivery may have occurred and must remain uncertain.
                state["delivery"] = None
                self.store.write_state(state)
                self._log(state, "prompt_rejected_before_delivery", agent=provider, turn_id=turn_id, error_code=error.code)
                if error.code == "agent_not_found":
                    return self.set_wait_user(state, f"prompt target {name} disappeared before delivery; native session preserved and safe resume is allowed")
                return self.handle_blocked(state, provider, sent=False)
            delivery["status"] = "uncertain"
            delivery["error_code"] = error.code
            delivery["error"] = str(error)
            self.store.write_state(state)
            self._log(state, "prompt_delivery_uncertain", agent=provider, turn_id=turn_id, error_code=error.code)
            return self.monitor(state)
        delivery["status"] = "accepted"
        delivery["accepted_at"] = iso_utc(self.clock())
        self.store.write_state(state)
        self._log(state, "prompt_accepted", agent=provider, turn_id=turn_id)
        return self.monitor(state)

    # ----- routing

    def route(self, state: dict[str, Any], block: ProtocolBlock) -> str:
        anomaly = state.get("deferred_anomaly")
        if isinstance(anomaly, dict) and anomaly.get("continuation") == "submitted":
            anomaly["continuation"] = "resolved"
            anomaly["resolved_at"] = iso_utc(self.clock())
        if state.get("workflow_policy") == "gated_v2" or block.version == 2:
            return self.route_v2(state, block)
        source = state["active_agent"]
        state["last_successful_handoff"] = {
            "from_agent": source,
            "to": block.next_agent,
            "stage": block.stage,
            "summary": block.handoff,
            "turn_id": block.turn_id,
            "at": iso_utc(self.clock()),
        }
        state["phase"] = block.stage
        state["turns_completed"] = int(state.get("turns_completed") or 0) + 1
        if isinstance(state.get("delivery"), dict):
            state["delivery"]["status"] = "completed"
        self._log(state, "protocol_accepted", from_agent=source, next=block.next_agent, stage=block.stage, turn_id=block.turn_id)
        if block.next_agent == "done":
            state["supervisor_state"] = "DONE"
            state["worker_pid"] = None
            state["completed_at"] = iso_utc(self.clock())
            self.emit_event(state, "TASK_DONE", {"stage": block.stage, "handoff": block.handoff})
            self.store.write_state(state)
            return "stop"
        if block.next_agent == "human":
            return self.set_wait_user(state, f"{source} routed to human: {block.handoff}")
        state["active_agent"] = block.next_agent
        state["delivery"] = None
        state["supervisor_state"] = "RUNNING"
        self.store.write_state(state)
        return "continue"

    def monitor(self, state: dict[str, Any]) -> str:
        """Watch the active agent until its current turn yields a protocol block or a fail-closed stop."""
        while True:
            if not self.check_control(state):
                return "stop"
            provider = state["active_agent"]
            name = self.agent_name(provider)
            delivery = state.get("delivery")
            if not isinstance(delivery, dict):
                return self.set_wait_user(state, "delivery record disappeared while monitoring; inspect state.json")
            try:
                agent = self.ensure_agent(provider)
            except (SupervisorError, HerdrError) as error:
                return self.set_wait_user(state, f"cannot safely inspect {name}: {error}")
            status = agent["agent_status"]
            if status == "working":
                if delivery["status"] == "uncertain":
                    delivery["status"] = "accepted"
                    delivery["accepted_at"] = iso_utc(self.clock())
                    self.store.write_state(state)
                    self._log(state, "prompt_accepted_by_activity", agent=provider, turn_id=delivery["turn_id"])
                # Deterministic lifecycle wait: while the lifecycle stays `working`, nothing is read and no
                # LLM is awakened. The quota snapshot (a local file) is the only thing checked; only a
                # blocking snapshot counts as a transition worth reading the visible screen for.
                try:
                    blocking = blocking_windows(self.quota(provider), self.clock())
                except QuotaError as error:
                    return self.set_wait_user(state, f"quota evidence for {provider} is unsafe: {error}")
                if blocking:
                    try:
                        screen = self.read_output(provider, settled=False)
                    except HerdrError as error:
                        return self.set_wait_user(state, f"cannot read {name} while working: {error}")
                    if self.quota_screen_confirmed(provider, screen):
                        return self.wait_for_quota(state, provider, blocking, midturn=True)
                try:
                    self.herdr.wait(name, timeout_ms=int(self.config["wait_timeout_ms"]))
                except HerdrError as error:
                    if error.code not in {"timeout", "agent_prompt_stalled"}:
                        return self.set_wait_user(state, f"cannot wait for {name}: {error}")
                continue
            if status == "unknown":
                return self.set_wait_user(state, f"{name} lifecycle is unknown; completion is not assumed and nothing was resent")
            if status == "blocked":
                # An approval/question dialog is never routed, even if a current-turn block is visible.
                return self.handle_blocked(state, provider, sent=True)
            # idle or done: inspect the settled output.
            try:
                output = self.read_output(provider, settled=True)
                block = parse_protocol(output, state["run_id"], delivery["turn_id"])
            except HerdrError as error:
                return self.set_wait_user(state, f"cannot read {name} output: {error}")
            except SupervisorError as error:
                return self.set_wait_user(state, str(error))
            if block is not None:
                return self.route(state, block)
            # Settled without a matching block (Herdr `done` is never semantic success). A reconciliation
            # turn that fails again is a hard stop: no automatic retry.
            if str(delivery.get("kind", "")).startswith("continuation:reconcile"):
                anomaly = state.get("deferred_anomaly") or {}
                anomaly["continuation"] = "failed"
                state["deferred_anomaly"] = anomaly
                return self.set_wait_user(state, f"{name} settled ({status}) without a protocol block after the one-time reconciliation turn; no further retry. Inspect the pane and revise.", requires_action=True)
            # Precedence: refresh/reconcile quota through the non-LLM mechanism, then validate the snapshot
            # with the existing stale/malformed safeguards. A provably blocking future window means the turn
            # was most likely cut by quota: enter deferred WAIT_QUOTA (no screen-string match required).
            gated_v2 = state.get("workflow_policy") == "gated_v2"
            accepted = delivery.get("status") == "accepted" or (
                delivery.get("status") == "uncertain" and isinstance(delivery.get("accepted_at"), str)
            )
            if gated_v2 and not accepted:
                return self.set_wait_user(
                    state,
                    f"{name} settled ({status}) without a protocol block, but delivery was never confirmed accepted; "
                    "quota cannot explain uncertain delivery and nothing was resent.",
                    requires_action=True,
                )
            self.refresh_quota(state, provider)
            try:
                snapshot = self.quota(provider)
            except QuotaError as error:
                return self.set_wait_user(state, f"{name} settled without a protocol block and quota evidence is unsafe: {error}", requires_action=gated_v2)
            max_age = float(self.config["quota_snapshot_max_age_seconds"])
            if snapshot.fetched_at is None or snapshot.fetched_at > self.clock() + 60 or self.clock() - snapshot.fetched_at > max_age:
                return self.set_wait_user(state, f"{name} settled without a protocol block and quota evidence is stale or undated", requires_action=gated_v2)
            blocking = blocking_windows(snapshot, self.clock())
            inferred = False
            if not blocking:
                blocking = self.inferred_provider_limit_windows(snapshot, output) if gated_v2 else ()
                inferred = bool(blocking)
            if blocking:
                return self.wait_for_quota(state, provider, blocking, midturn=True, anomaly="missing_protocol", provider_limit_inferred=inferred)
            return self.set_wait_user(
                state,
                f"{name} settled ({status}) without a protocol block for turn {delivery['turn_id']} "
                f"(delivery={delivery['status']}) and no blocking quota window exists. The prompt was not resent; inspect the pane and provide guidance.",
                requires_action=gated_v2,
            )

    def handle_blocked(self, state: dict[str, Any], provider: str, *, sent: bool, output: str | None = None) -> str:
        name = self.agent_name(provider)
        if output is None:
            try:
                output = self.read_output(provider, settled=True)
            except HerdrError as error:
                return self.set_wait_user(state, f"{name} is blocked and its screen cannot be read: {error}")
        try:
            blocking = blocking_windows(self.quota(provider), self.clock())
        except QuotaError as error:
            return self.set_wait_user(state, f"{name} is blocked and quota evidence is unsafe: {error}")
        if blocking and self.quota_screen_confirmed(provider, output):
            return self.wait_for_quota(state, provider, blocking, midturn=sent)
        return self.set_wait_user(
            state,
            f"{name} is blocked and was not positively identified as a provider quota interruption "
            f"(quota screen pattern matched={self.quota_screen_confirmed(provider, output)}, "
            f"snapshot blocking={bool(blocking)}); no key was pressed. Resolve it in the pane, then `herdr-supervisor resume`.",
        )

    # ----- quota waits

    def _usable_quota_evidence(
        self,
        state: dict[str, Any],
        provider: str,
        wait: dict[str, Any],
        refresh_result: str = "unavailable",
        refresh_attempted_at: float | None = None,
    ) -> tuple[bool, tuple[QuotaWindow, ...] | None, str]:
        """(usable, blocking windows, reason). Usable only with fresh, non-blocking, valid evidence: a snapshot
        fetched before the wait started cannot prove early availability; a failed refresh is never trusted;
        after the safe reset deadline the cached windows are expired by the existing rules."""
        strict_v2 = state.get("workflow_policy") == "gated_v2"
        if strict_v2 and refresh_result == "failed":
            return False, None, "quota refresh failed; cached evidence is not trusted"
        try:
            snapshot = self.quota(provider)
        except QuotaError as error:
            return False, None, f"quota evidence unsafe: {error}"
        now = self.clock()
        started = float(wait.get("started_at_unix") or 0)
        deadline = float(wait.get("resume_at") or 0)
        if strict_v2:
            fetched = snapshot.fetched_at
            max_age = float(self.config["quota_snapshot_max_age_seconds"])
            if fetched is None:
                return False, None, "quota snapshot is undated"
            if fetched > now + 60:
                return False, None, "quota snapshot is future-dated"
            if now - fetched > max_age:
                return False, None, "quota snapshot is stale"
            if refresh_result == "ok" and refresh_attempted_at is not None and fetched < refresh_attempted_at - 1:
                return False, None, "quota refresh reported success without producing a current snapshot"
            if not snapshot.windows or any(window.resets_at <= now for window in snapshot.windows):
                return False, None, "quota snapshot does not contain current future reset evidence"
            if refresh_result == "unavailable":
                if now < deadline:
                    return False, None, "provider has no independent refresh; retaining the cached safe deadline"
                if fetched < deadline:
                    return False, None, "provider has no independent refresh and no snapshot updated at the safe deadline"
        blocking = blocking_windows(snapshot, now)
        if blocking:
            return False, blocking, "still blocking"
        if refresh_result == "failed":
            return False, None, "quota refresh failed; cached evidence is not trusted"
        if now < deadline and refresh_result == "unavailable":
            return False, None, "provider has no independent refresh; retaining the cached safe deadline"
        if now < deadline and (snapshot.fetched_at is None or snapshot.fetched_at < started):
            return False, None, "snapshot predates the wait; not trusting it for an early wake"
        if wait.get("provider_limit_inferred"):
            original = {item["kind"]: item for item in wait.get("blocking_windows", []) if isinstance(item, dict)}
            inferred_still = tuple(
                window for window in snapshot.windows
                if window.kind in original and window.resets_at > now
                and window.remaining_percent <= float(original[window.kind]["remaining_percent"])
            )
            if inferred_still:
                return False, inferred_still, "provider-limit evidence remains unchanged"
        return True, (), "usable"

    def wait_for_quota(self, state: dict[str, Any], provider: str, blocking: tuple[QuotaWindow, ...], *, midturn: bool, anomaly: str | None = None, resume: bool = False, provider_limit_inferred: bool = False) -> str:
        """Deterministic deadline-plus-recheck wait. Every `quota_recheck_seconds` (or on a run-bound manual
        refresh request) the worker runs only the provider's non-LLM refresh and re-reads the snapshot; usable
        fresh evidence wakes the workflow early, blocking evidence keeps waiting, and refresh failures or
        stale evidence never resume work. No LLM is prompted, read, or woken while waiting."""
        reset_state = (state.get("codex_reset") or {}).get("current_redemption_state")
        if provider == "codex" and (not resume or reset_state in {"RESET_PREPARED", "RESET_CONSUMING", "RESET_VERIFYING", "RESET_RECONCILING"}):
            reset_outcome = self.try_authorized_codex_reset(state, blocking, midturn=midturn)
            if reset_outcome is not None:
                return reset_outcome
        buffer_seconds = float(self.config["quota_safety_buffer_seconds"])
        recheck_seconds = float(self.config["quota_recheck_seconds"])
        now = self.clock()
        existing = state.get("quota_wait") if resume and isinstance(state.get("quota_wait"), dict) else None
        resume_at = quota_resume_at(blocking, buffer_seconds)
        wait = existing or {
            "provider": provider,
            "midturn": midturn,
            "anomaly": anomaly,
            "blocking_windows": [dataclasses.asdict(window) for window in blocking],
            "resume_at": resume_at,
            "resume_at_utc": iso_utc(resume_at),
            "resume_at_local": iso_local(resume_at),
            "started_at": iso_utc(now),
            "started_at_unix": now,
            "next_recheck_unix": min(now + recheck_seconds, resume_at),
            "recheck_count": 0,
            "manual_refresh_requested": False,
            "last_manual_refresh_unix": None,
            "early_refresh_available": self.quota_refresh_command(provider) is not None,
            "provider_limit_inferred": provider_limit_inferred,
        }
        if existing:
            wait.setdefault("started_at_unix", now)  # a pre-recovery snapshot cannot prove early availability
            wait.setdefault("next_recheck_unix", min(now + recheck_seconds, float(wait.get("resume_at") or resume_at)))
            wait.setdefault("recheck_count", 0)
            wait.setdefault("manual_refresh_requested", False)
            wait.setdefault("last_manual_refresh_unix", None)
            wait.setdefault("anomaly", anomaly)
            wait.setdefault("early_refresh_available", self.quota_refresh_command(provider) is not None)
            wait.setdefault("provider_limit_inferred", provider_limit_inferred)
        state["supervisor_state"] = "WAIT_QUOTA"
        state["quota_wait"] = wait
        if not existing:
            accepted_delivery_status = (state.get("delivery") or {}).get("status")
            if midturn and isinstance(state.get("delivery"), dict) and state["delivery"].get("status") != "completed":
                state["delivery"]["status"] = "interrupted"
            if anomaly and isinstance(state.get("delivery"), dict):
                delivery = state["delivery"]
                state["deferred_anomaly"] = {
                    "run_id": state["run_id"], "turn_id": delivery.get("turn_id"), "provider": provider,
                    "session_id": (state.get("native_sessions") or {}).get(provider), "delivery_status": accepted_delivery_status,
                    "prompt_sha256": delivery.get("prompt_sha256"), "kind": anomaly,
                    "blocking_windows": wait["blocking_windows"], "snapshot_fetched_at": self._snapshot_fetched_at(provider),
                    "next_recheck_unix": wait["next_recheck_unix"], "reset_deadline_unix": resume_at,
                    "continuation": "pending", "created_at": iso_utc(now),
                }
            self.emit_event(state, "WAIT_QUOTA", {"provider": provider, "resume_at": resume_at, "windows": [w.kind for w in blocking], "midturn": midturn, "deferred_anomaly": anomaly, "next_recheck": wait["next_recheck_unix"], "early_refresh_available": wait["early_refresh_available"], "provider_limit_inferred": provider_limit_inferred})
            self.store.write_state(state)
            self._log(state, "quota_wait", provider=provider, resume_at=resume_at, windows=[w.kind for w in blocking], midturn=midturn, anomaly=anomaly)
        else:
            self.store.write_state(state)
            self._log(state, "quota_wait_recovered", provider=provider, next_recheck=wait.get("next_recheck_unix"))
        early = False
        while True:
            target = min(float(wait["next_recheck_unix"]), float(wait["resume_at"]))
            while self.clock() < target and not wait.get("manual_refresh_requested"):
                if not self.check_control(state):
                    return "stop"
                wait = state["quota_wait"]  # a manual refresh request may have been recorded by check_control
                self.sleeper(min(max(float(self.config["poll_interval_seconds"]), 0.01), max(target - self.clock(), 0.01)))
            wait["manual_refresh_requested"] = False
            wait["recheck_count"] = int(wait.get("recheck_count") or 0) + 1
            at_deadline = self.clock() >= float(wait["resume_at"])
            refresh_attempted_at = self.clock()
            refresh_result = self.refresh_quota(state, provider)
            usable, still_blocking, reason = self._usable_quota_evidence(
                state, provider, wait, refresh_result, refresh_attempted_at
            )
            if usable:
                early = not at_deadline
                break
            if at_deadline:
                if still_blocking:
                    # the cached reset passed but fresh evidence still blocks: compute the new safe deadline
                    wait["blocking_windows"] = [dataclasses.asdict(window) for window in still_blocking]
                    wait["resume_at"] = quota_resume_at(still_blocking, buffer_seconds)
                    wait["resume_at_utc"], wait["resume_at_local"] = iso_utc(wait["resume_at"]), iso_local(wait["resume_at"])
                    self._log(state, "quota_still_blocking", provider=provider, windows=[w.kind for w in still_blocking], new_resume_at=wait["resume_at"])
                else:
                    return self.set_wait_user(state, f"{provider} quota evidence is not usable after the safe reset deadline ({reason}); nothing was resent")
            else:
                self._log(state, "quota_recheck", provider=provider, reason=reason, count=wait["recheck_count"])
            wait["next_recheck_unix"] = min(self.clock() + recheck_seconds, float(wait["resume_at"]))
            if isinstance(state.get("deferred_anomaly"), dict):
                state["deferred_anomaly"]["next_recheck_unix"] = wait["next_recheck_unix"]
                state["deferred_anomaly"]["reset_deadline_unix"] = wait["resume_at"]
            self.store.write_state(state)
        state["supervisor_state"] = "RUNNING"
        state["quota_wait"] = None
        if provider == "codex":
            reset = state.get("codex_reset") or {}
            if reset.get("current_redemption_state") == "IDLE":
                reset.update({"blocking_event_id": None, "quota_was_usable": True})
        self.emit_event(state, "QUOTA_RESUMED", {"provider": provider, "early": early, "deferred_anomaly": wait.get("anomaly"), "rechecks": wait.get("recheck_count")})
        self.store.write_state(state)
        self._log(state, "quota_wait_ended", provider=provider, early=early)
        if not midturn:
            return "continue"
        return self.resume_interrupted_turn(state, provider)

    def _snapshot_fetched_at(self, provider: str) -> float | None:
        try:
            return self.quota(provider).fetched_at
        except QuotaError:
            return None

    def resume_interrupted_turn(self, state: dict[str, Any], provider: str) -> str:
        name = self.agent_name(provider)
        delivery = state.get("delivery")
        try:
            agent = self.ensure_agent(provider)
        except (SupervisorError, HerdrError) as error:
            return self.set_wait_user(state, f"cannot inspect {name} after the quota wait: {error}")
        status = agent["agent_status"]
        if status == "blocked":
            try:
                output = self.read_output(provider, settled=True)
            except HerdrError as error:
                return self.set_wait_user(state, f"{name} is blocked after the quota wait and cannot be read: {error}")
            keys = list(self.config.get("quota_block_dismiss_keys") or [])
            if not keys or not self.quota_screen_confirmed(provider, output):
                return self.set_wait_user(state, f"{name} is still blocked after the quota wait; no key was pressed")
            try:
                self.herdr.send_keys(name, keys)
                self._log(state, "quota_screen_dismissed", agent=provider, keys=keys)
                self.herdr.wait(name, timeout_ms=int(self.config["wait_timeout_ms"]))
            except HerdrError as error:
                if error.code not in {"timeout", "agent_prompt_stalled"}:
                    return self.set_wait_user(state, f"could not dismiss the quota screen on {name}: {error}")
            try:
                agent = self.ensure_agent(provider)
            except (SupervisorError, HerdrError) as error:
                return self.set_wait_user(state, f"cannot inspect {name} after dismissing the quota screen: {error}")
            status = agent["agent_status"]
        if status == "working":
            if isinstance(delivery, dict):
                delivery["status"] = "accepted"
                self.store.write_state(state)
            return self.monitor(state)
        if status in READY_STATES:
            if isinstance(delivery, dict):
                try:
                    output = self.read_output(provider, settled=True)
                    block = parse_protocol(output, state["run_id"], delivery["turn_id"])
                except HerdrError as error:
                    return self.set_wait_user(state, f"cannot read {name} after the quota wait: {error}")
                except SupervisorError as error:
                    return self.set_wait_user(state, str(error))
                if block is not None:
                    return self.route(state, block)
            state["interrupted_delivery"] = delivery
            state["delivery"] = None
            anomaly = state.get("deferred_anomaly")
            if isinstance(anomaly, dict) and anomaly.get("continuation") == "pending":
                # exactly one purpose-built reconciliation turn, same native session, new turn UUID
                if anomaly.get("session_id") != (state.get("native_sessions") or {}).get(provider):
                    return self.set_wait_user(state, f"{name} native session differs from the deferred turn's session; refusing to reconcile", requires_action=True)
                anomaly["continuation"] = "submitted"
                state["continuation"] = {"kind": "reconcile", "turn_id": anomaly.get("turn_id"), "anomaly": anomaly.get("kind")}
                self.store.write_state(state)
                return self.submit(state, "continuation:reconcile")
            self.store.write_state(state)
            return self.submit(state, "continuation")
        return self.set_wait_user(state, f"{name} is {status} after the quota wait; nothing was resent")

    # ----- run loop

    def initialize(self, task: str, start: str, *, task_reference: str | None = None, workflow_policy: str = "v1", char_limit: int | None = None, codex_reset_authorization: dict[str, Any] | None = None, run_id: str | None = None) -> dict[str, Any]:
        if start not in PROVIDERS:
            raise SupervisorError(f"invalid starting agent: {start}")
        if workflow_policy not in WORKFLOW_POLICIES:
            raise SupervisorError(f"invalid workflow policy: {workflow_policy}")
        if workflow_policy == "gated_v2" and start != "codex":
            raise SupervisorError("gated_v2 tasks must start with codex")
        limit = int(char_limit) if char_limit is not None else int(self.config["max_task_chars"])
        if len(task) > limit:
            raise SupervisorError("task text exceeds max_task_chars" if char_limit is None else "task file exceeds the file character ceiling")
        existing = self.store.read_state(required=False)
        if existing and existing.get("supervisor_state") not in TERMINAL_STATES:
            raise SupervisorError(
                f"task {existing.get('task_id')} is still {existing.get('supervisor_state')}; "
                "use `resume` to continue it or `cancel` before starting another task"
            )
        owners = self.verify_or_seed_owners()
        if run_id is not None and (not isinstance(run_id, str) or not _UUID_RE.fullmatch(run_id)):
            raise SupervisorError("preallocated run id is invalid")
        run_id = run_id or str(uuid.uuid4())
        state: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "run_id": run_id,
            "task_id": run_id,
            "task_text": task,
            "task_reference": task_reference,
            "phase": "initial",
            "active_agent": start,
            "start_agent": start,
            "supervisor_state": "RUNNING",
            "worker_pid": os.getpid(),
            "native_sessions": {provider: owners[provider]["session_id"] for provider in PROVIDERS},
            "last_successful_handoff": None,
            "quota_wait": None,
            "delivery": None,
            "interrupted_delivery": None,
            "turns_completed": 0,
            "wait_user_reason": None,
            "last_error": None,
            "created_at": iso_utc(self.clock()),
            **new_v2_fields(workflow_policy),
        }
        if codex_reset_authorization is not None:
            budget = codex_reset_authorization.get("budget")
            available = codex_reset_authorization.get("available_count")
            fingerprint = codex_reset_authorization.get("account_fingerprint")
            if isinstance(budget, bool) or not isinstance(budget, int) or budget < 0 or isinstance(available, bool) or not isinstance(available, int) or budget > available:
                raise SupervisorError("Codex reset authorization is invalid")
            if budget > 0 and (not isinstance(fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", fingerprint)):
                raise SupervisorError("positive Codex reset budget requires a verified account identity")
            state["codex_reset"].update({"authorized_reset_budget": budget, "authorization_available_count": available,
                                         "authorization_timestamp": iso_utc(self.clock()), "account_fingerprint": fingerprint})
        self.store.write_control("running")
        self.emit_event(state, "TASK_STARTED", {"task": task[:500], "start": start, "policy": workflow_policy})
        if state["codex_reset"]["authorization_timestamp"]:
            self.emit_event(state, "CODEX_RESET_AUTHORIZED", {"available": state["codex_reset"]["authorization_available_count"], "authorized": state["codex_reset"]["authorized_reset_budget"]})
        self.store.write_state(state)
        self._log(state, "task_started", start=start, task=task, task_reference=task_reference, workflow_policy=workflow_policy)
        return state

    def run_loop(self, state: dict[str, Any], *, recovering: bool = False) -> int:
        state["worker_pid"] = os.getpid()
        gate_pending = (state.get("pending_gate") or {}).get("status") == "pending"
        needs_action = bool(state.get("wait_user_requires_action")) and not isinstance(state.get("continuation"), dict)
        if gate_pending and state["supervisor_state"] not in TERMINAL_STATES:
            state["supervisor_state"] = state["pending_gate"]["expected_state"]  # a paused/errored gate wait stays a gate wait
        elif state["supervisor_state"] in {"PAUSED", "WAIT_USER", "ERROR"} and not gate_pending and not needs_action:
            state["supervisor_state"] = "RUNNING"
            state["wait_user_reason"] = None
            state["wait_user_requires_action"] = False
            state["last_error"] = None
        self.store.write_state(state)
        while True:
            if not self.check_control(state):
                return 3 if state["supervisor_state"] == "PAUSED" else 0
            if state["supervisor_state"] in TERMINAL_STATES:
                return 0
            if state["supervisor_state"] in HUMAN_WAIT_STATES:
                # A typed gate or WAIT_USER is pending: nothing to do until a human action arrives.
                state["worker_pid"] = None
                self.store.write_state(state)
                return self._exit_code(state)
            provider = state["active_agent"]
            delivery = state.get("delivery")
            quota_wait = state.get("quota_wait")
            if recovering:
                recovering = False
                reset = state.get("codex_reset") or {}
                reset_phase = reset.get("current_redemption_state")
                if reset_phase == "RESET_VERIFIED":
                    reset.update({"current_redemption_state": "IDLE", "current_idempotency_key": None,
                                  "blocking_event_id": None, "quota_was_usable": True})
                    self.store.write_state(state)
                    if isinstance(delivery, dict) and delivery.get("status") in {"accepted", "uncertain", "interrupted"}:
                        outcome = self.resume_interrupted_turn(state, "codex")
                        if outcome == "stop":
                            return self._exit_code(state)
                    continue
                if reset_phase in {"RESET_PREPARED", "RESET_CONSUMING", "RESET_VERIFYING", "RESET_RECONCILING"} and state["supervisor_state"] != "WAIT_QUOTA":
                    raw_windows = reset.get("quota_snapshot_before")
                    if not isinstance(raw_windows, list) or not raw_windows:
                        return self._exit_code_after_wait_user(state, "Codex reset recovery lacks its original quota evidence")
                    try:
                        windows = tuple(QuotaWindow(**window) for window in raw_windows)
                    except (TypeError, ValueError):
                        return self._exit_code_after_wait_user(state, "Codex reset recovery quota evidence is corrupt")
                    outcome = self.try_authorized_codex_reset(
                        state, windows,
                        midturn=bool(isinstance(delivery, dict) and delivery.get("status") in {"accepted", "uncertain", "interrupted"}),
                        allow_uncertain_retry=True,
                    )
                    reset.pop("reconcile_requested", None)
                    self.store.write_state(state)
                    if outcome == "stop":
                        return self._exit_code(state)
                    if outcome is None:
                        outcome = self.wait_for_quota(
                            state, "codex", windows,
                            midturn=bool(isinstance(delivery, dict) and delivery.get("status") in {"accepted", "uncertain", "interrupted"}),
                        )
                        if outcome == "stop":
                            return self._exit_code(state)
                    continue
                if isinstance(quota_wait, dict) and state["supervisor_state"] == "WAIT_QUOTA":
                    windows = tuple(QuotaWindow(**w) for w in quota_wait.get("blocking_windows", []))
                    self._log(state, "quota_wait_recovered", provider=quota_wait.get("provider"))
                    outcome = self.wait_for_quota(state, quota_wait["provider"], windows, midturn=bool(quota_wait.get("midturn")), anomaly=quota_wait.get("anomaly"), resume=True)
                    if outcome == "stop":
                        return self._exit_code(state)
                    continue
                if isinstance(delivery, dict) and delivery.get("status") in {"prepared", "accepted", "uncertain"}:
                    delivery["status"] = "uncertain"
                    delivery["recovered_at"] = iso_utc(self.clock())
                    self.store.write_state(state)
                    self._log(state, "delivery_recovered_uncertain", turn_id=delivery.get("turn_id"))
            if isinstance(delivery, dict) and delivery.get("status") in {"accepted", "uncertain"}:
                outcome = self.monitor(state)
            elif isinstance(delivery, dict) and delivery.get("status") == "interrupted":
                outcome = self.resume_interrupted_turn(state, provider)
            else:
                try:
                    agent = self.ensure_agent(provider)
                    blocking = blocking_windows(self.quota(provider), self.clock())
                except (SupervisorError, HerdrError) as error:
                    self.set_wait_user(state, str(error))
                    return 2
                if blocking:
                    outcome = self.wait_for_quota(state, provider, blocking, midturn=False)
                elif agent["agent_status"] == "working":
                    # Someone else's turn is running in this session: never stack a prompt on it.
                    try:
                        self.herdr.wait(self.agent_name(provider), timeout_ms=int(self.config["wait_timeout_ms"]))
                    except HerdrError as error:
                        if error.code not in {"timeout", "agent_prompt_stalled"}:
                            self.set_wait_user(state, f"cannot wait for busy {self.agent_name(provider)}: {error}")
                            return 2
                    self._log(state, "agent_busy_before_submit", agent=provider)
                    continue
                elif agent["agent_status"] == "blocked":
                    outcome = self.handle_blocked(state, provider, sent=False)
                elif agent["agent_status"] == "unknown":
                    outcome = self.set_wait_user(state, f"{self.agent_name(provider)} lifecycle is unknown before submission")
                elif isinstance(state.get("continuation"), dict):
                    outcome = self.submit(state, "continuation:" + str(state["continuation"].get("kind")))
                else:
                    outcome = self.submit(state, "handoff" if state.get("last_successful_handoff") else "initial")
            if outcome == "stop":
                return self._exit_code(state)

    @staticmethod
    def _exit_code(state: dict[str, Any]) -> int:
        """0 terminal, 2 WAIT_USER, 3 PAUSED, 4 typed human gate. All are intentional stops."""
        if state["supervisor_state"] in TERMINAL_STATES:
            return 0
        if state["supervisor_state"] in GATE_STATES:
            return 4
        return 3 if state["supervisor_state"] == "PAUSED" else 2

    def _exit_code_after_wait_user(self, state: dict[str, Any], reason: str) -> int:
        self.set_wait_user(state, reason, requires_action=True)
        return self._exit_code(state)

    def _guarded(self, state: dict[str, Any], body: Callable[[], int]) -> int:
        try:
            return body()
        except KeyboardInterrupt:
            state["supervisor_state"] = "PAUSED"
            state["worker_pid"] = None
            self.store.write_state(state)
            self._log(state, "interrupted_by_operator")
            return 3
        except Exception as error:  # noqa: BLE001 - persist before propagating
            state["supervisor_state"] = "ERROR"
            state["last_error"] = str(error)
            state["worker_pid"] = None
            self.emit_event(state, "TASK_ERROR", {"error": str(error)[:500]})
            self.store.write_state(state)
            self._log(state, "error", error=str(error))
            raise

    def run_new(self, task: str, start: str, *, task_reference: str | None = None, workflow_policy: str = "v1", char_limit: int | None = None, codex_reset_authorization: dict[str, Any] | None = None) -> int:
        with WorkerLock(self.paths.lock_file):
            with self.store.transaction():
                state = self.initialize(task, start, task_reference=task_reference, workflow_policy=workflow_policy, char_limit=char_limit,
                                        codex_reset_authorization=codex_reset_authorization)
            return self._guarded(state, lambda: self.run_loop(state))

    def resume(self) -> int:
        with WorkerLock(self.paths.lock_file):
            state = self.store.read_state()
            if state["supervisor_state"] in TERMINAL_STATES:
                raise SupervisorError(f"task is already {state['supervisor_state']}; start a new one with `run`")
            self.store.write_control("running")
            self.verify_persisted_sessions(state)
            self.reconcile_outbox(state)
            return self._guarded(state, lambda: self.run_loop(state, recovering=True))

    # ----- reporting

    def report_live_agent(
        self, provider: str, live_agents: list[dict[str, Any]], owners: dict[str, Any] | None
    ) -> tuple[dict[str, Any] | None, bool, bool]:
        """Locate reporting targets by durable native identity; aliases are only a fallback label."""
        owner = owners.get(provider, {}) if isinstance(owners, dict) else {}
        expected = owner.get("session_id")
        if isinstance(expected, str) and expected:
            matches = [agent for agent in live_agents if session_identity(agent) == expected]
            if len(matches) == 1:
                return matches[0], False, False
            if len(matches) > 1:
                return None, True, False
            # Herdr can temporarily omit Codex's native-session field while the CLI is actively working.
            # A unique same-provider match at the persisted pane is reported as detected but explicitly
            # unverified; it is never treated as an identity match by execution/recovery code.
            pane_id = owner.get("pane_id")
            pane_matches = [
                agent for agent in live_agents
                if pane_id and agent.get("pane_id") == pane_id and agent.get("agent") == provider
                and session_identity(agent) is None
            ]
            if len(pane_matches) == 1:
                return pane_matches[0], False, True
        aliases = [agent for agent in live_agents if agent.get("name") == self.agent_name(provider)]
        return (aliases[0], False, False) if len(aliases) == 1 else (None, len(aliases) > 1, False)

    def agent_report(self, provider: str, live: dict[str, Any] | None, owners: dict[str, Any] | None, *, ambiguous: bool = False, identity_unavailable: bool = False) -> dict[str, Any]:
        name = self.agent_name(provider)
        identity = session_identity(live)
        expected = owners.get(provider, {}).get("session_id") if isinstance(owners, dict) else None
        return {
            "name": name,
            "live_name": live.get("name") if live else None,
            "detected": live is not None,
            "ambiguous": ambiguous,
            "identity_unavailable": identity_unavailable,
            "lifecycle": live.get("agent_status") if live else None,
            "pane_id": live.get("pane_id") if live else None,
            "native_session_id": identity,
            "recorded_session_id": expected,
            "session_matches": bool(identity and expected and identity == expected),
        }

    def query_report(self) -> dict[str, Any]:
        """Read-only query-provider readiness; failures degrade this subsection only."""
        summary = query_owner_summary(self.paths)
        if not summary.get("provisioned"):
            return summary
        try:
            import herdr_query  # noqa: PLC0415 - optional companion module

            summary["availability"] = herdr_query.QueryWorker(
                self.paths, self.config, self.herdr, clock=self.clock
            ).availability_report()
        except Exception as error:  # noqa: BLE001 - doctor/status must remain available
            summary["availability_error"] = type(error).__name__
        return summary

    def doctor(self) -> dict[str, Any]:
        now = self.clock()
        report: dict[str, Any] = {
            "ok": True,
            "checked_at_utc": iso_utc(now),
            "checked_at_local": iso_local(now),
            "herdr_env": os.environ.get("HERDR_ENV") == "1",
            "herdr_bin": getattr(self.herdr, "binary", None),
            "config_file": str(self.paths.config_file),
            "state_dir": str(self.paths.state_dir),
            "project_root": self.config["project_root"],
            "worker_lock_held": False,
            "agents": {},
            "quota": {},
            "errors": [],
            "warnings": [],
        }
        if not report["herdr_env"]:
            report["warnings"].append("HERDR_ENV is not 1 (not running inside a Herdr pane)")
        if self.paths.lock_file.exists():
            try:
                report["worker_lock_held"] = WorkerLock.is_held(self.paths.lock_file)
            except OSError as error:
                report["errors"].append(f"worker lock is not accessible: {error}")
        if not os.access(self.paths.state_dir, os.W_OK):
            report["errors"].append(f"state directory is not writable: {self.paths.state_dir}")
        owners: dict[str, Any] | None = None
        try:
            owners = self.owners()
        except SupervisorError as error:
            report["errors"].append(str(error))
        live_agents: list[dict[str, Any]] = []
        try:
            live_agents = self.herdr.list_agents()
        except HerdrError as error:
            report["errors"].append(f"herdr agent list failed: {error}")
        for provider in PROVIDERS:
            live, ambiguous, identity_unavailable = self.report_live_agent(provider, live_agents, owners)
            entry = self.agent_report(provider, live, owners, ambiguous=ambiguous, identity_unavailable=identity_unavailable)
            report["agents"][provider] = entry
            if entry["ambiguous"]:
                report["errors"].append(f"{entry['name']} native session is present in more than one pane")
            elif not entry["detected"]:
                report["errors"].append(f"{entry['name']} is not detected by herdr")
            elif entry["identity_unavailable"]:
                report["warnings"].append(
                    f"{entry['name']} is working in its recorded pane, but Herdr temporarily omitted its native session identity; execution remains fail-closed"
                )
            elif not entry["session_matches"]:
                report["errors"].append(f"{entry['name']} native session does not match owners.json")
            try:
                session = owners[provider]["session_id"] if owners else None
                report["quota"][provider] = quota_as_dict(self.quota(provider, session_id=session), now)
            except QuotaError as error:
                report["quota"][provider] = {"ok": False, "error": str(error)}
                report["errors"].append(str(error))
        root = Path(self.config["project_root"])
        if not root.is_dir() or not ((root / "AGENTS.md").is_file() or (root / "CLAUDE.md").is_file()):
            report["errors"].append(f"project root {root} lacks AGENTS.md/CLAUDE.md")
        try:
            state = self.store.read_state(required=False)
            report["task_state"] = None if state is None else state.get("supervisor_state")
            report["state_schema"] = None if state is None else self.store._disk_schema()
        except SupervisorError as error:
            report["errors"].append(str(error))
        report["telegram"] = telegram_doctor_summary()
        report["query_session"] = self.query_report()
        report["backup"] = backup_doctor_summary(self.paths, now)
        report["ok"] = not report["errors"]
        return report

    def status(self) -> dict[str, Any]:
        now = self.clock()
        state = self.store.read_state(required=False)
        report: dict[str, Any] = {
            "checked_at_utc": iso_utc(now),
            "checked_at_local": iso_local(now),
            "supervisor_state": "NO_TASK" if state is None else state["supervisor_state"],
            "worker_lock_held": WorkerLock.is_held(self.paths.lock_file) if self.paths.lock_file.exists() else False,
            "task_id": state.get("task_id") if state else None,
            "task": state.get("task_text") if state else None,
            "task_reference": state.get("task_reference") if state else None,
            "phase": state.get("phase") if state else None,
            "active_agent": state.get("active_agent") if state else None,
            "turns_completed": state.get("turns_completed") if state else None,
            "delivery": state.get("delivery") if state else None,
            "wait_user_reason": state.get("wait_user_reason") if state else None,
            "wait_user_requires_action": bool(state.get("wait_user_requires_action")) if state else None,
            "last_error": state.get("last_error") if state else None,
            "quota_wait": state.get("quota_wait") if state else None,
            "last_successful_handoff": state.get("last_successful_handoff") if state else None,
            "workflow_policy": state.get("workflow_policy") if state else None,
            "pending_gate": safe_gate_view(state.get("pending_gate")) if state else None,
            "approved_plan": state.get("approved_plan") if state else None,
            "runtime_policy": state.get("runtime_policy") if state else None,
            "candidate_sha": state.get("candidate_sha") if state else None,
            "runtime_evidence": state.get("runtime_evidence") if state else None,
            "push_approval": state.get("push_approval") if state else None,
            "final_report": state.get("final_report") if state else None,
            "codex_reset": ({
                "available_at_authorization": state["codex_reset"].get("authorization_available_count"),
                "authorized_for_run": state["codex_reset"].get("authorized_reset_budget", 0),
                "used_this_run": state["codex_reset"].get("used_reset_count", 0),
                "budget_remaining": max(0, state["codex_reset"].get("authorized_reset_budget", 0) - state["codex_reset"].get("used_reset_count", 0)),
                "redemption_state": state["codex_reset"].get("current_redemption_state"),
                "automatic_redemption_available": bool(state["codex_reset"].get("account_fingerprint")),
            } if state else None),
            "last_event": state.get("last_event") if state else None,
            "event_sequence": state.get("event_sequence") if state else None,
            "native_sessions_abbrev": {k: str(v)[:8] for k, v in (state.get("native_sessions") or {}).items()} if state else None,
            "agents": {},
            "quota": {},
            "query_session": self.query_report(),
            "backup": backup_doctor_summary(self.paths, now),
            "errors": [],
        }
        owners: dict[str, Any] | None = None
        try:
            owners = self.owners()
        except SupervisorError as error:
            report["errors"].append(str(error))
        live_agents: list[dict[str, Any]] = []
        try:
            live_agents = self.herdr.list_agents()
        except HerdrError as error:
            report["errors"].append(f"herdr agent list failed: {error}")
        for provider in PROVIDERS:
            live, ambiguous, identity_unavailable = self.report_live_agent(provider, live_agents, owners)
            report["agents"][provider] = self.agent_report(
                provider, live, owners, ambiguous=ambiguous, identity_unavailable=identity_unavailable
            )
            try:
                session = owners[provider]["session_id"] if owners else None
                report["quota"][provider] = quota_as_dict(self.quota(provider, session_id=session), now)
            except QuotaError as error:
                report["quota"][provider] = {"ok": False, "error": str(error)}
                report["errors"].append(str(error))
        return report


# --------------------------------------------------------------------------- CLI


def _print_quota(report: dict[str, Any]) -> None:
    for provider in ("claude", "codex"):
        quota = report["quota"].get(provider, {})
        print(f"{provider.title()} quota:")
        if not quota.get("ok"):
            print(f"  ERROR: {quota.get('error')}")
            continue
        for window in quota["windows"]:
            flag = "  <-- BLOCKING" if window["blocking"] else ""
            print(f"  {window['kind']:<10} {window['remaining_percent']:5.1f}% remaining  resets {window['resets_at_local']} ({window['resets_at_utc']}){flag}")
        context = quota.get("context_used_percent")
        print(f"  context    {'unknown' if context is None else f'{context:.1f}% used'} (informational only)")
        if quota.get("fetched_at_local"):
            print(f"  snapshot   {quota['fetched_at_local']}")


def print_status(report: dict[str, Any]) -> None:
    print(f"Supervisor: {report['supervisor_state']}{' (worker running)' if report['worker_lock_held'] else ''}")
    if report.get("task_id"):
        print(f"Task ID:      {report['task_id']}")
        print(f"Task:         {report['task']}")
        if report.get("task_reference"):
            print(f"Task file:    {report['task_reference']}")
        print(f"Phase:        {report['phase']}")
        print(f"Active agent: {report['active_agent']}  (turns completed: {report.get('turns_completed')})")
        delivery = report.get("delivery")
        if isinstance(delivery, dict):
            print(f"Delivery:     {delivery.get('status')} turn={delivery.get('turn_id')} kind={delivery.get('kind')}")
    if report.get("wait_user_reason"):
        print(f"WAIT_USER:    {report['wait_user_reason']}")
    if report.get("last_error"):
        print(f"Last error:   {report['last_error']}")
    wait = report.get("quota_wait")
    if isinstance(wait, dict):
        print(f"Quota wait:   {wait.get('provider')} until {wait.get('resume_at_local')} ({wait.get('resume_at_utc')}) midturn={wait.get('midturn')}")
    handoff = report.get("last_successful_handoff")
    if isinstance(handoff, dict):
        print(f"Last handoff: {handoff.get('from_agent')} -> {handoff.get('to')} [{handoff.get('stage')}] {handoff.get('summary')}")
    for provider in ("claude", "codex"):
        agent = report["agents"].get(provider, {})
        print(
            f"{provider.title():<7} agent: {agent.get('name')} detected={agent.get('detected')} lifecycle={agent.get('lifecycle')} "
            f"pane={agent.get('pane_id')} session_match={agent.get('session_matches')}"
        )
    _print_quota(report)
    for error in report.get("errors", []):
        print(f"ERROR: {error}")


def backup_doctor_summary(paths: Paths, now: float) -> dict[str, Any]:
    """Optional backup health for doctor/status; disabled installations report 'disabled' and never error."""
    try:
        import herdr_backup  # noqa: PLC0415

        config = herdr_backup.load_backup_config(herdr_backup.backup_config_file(paths))
        return herdr_backup.health(config, herdr_backup.load_backup_state(paths), now)
    except (SupervisorError, OSError, ValueError, ImportError) as error:
        return {"status": "failed", "failure_reason": f"backup configuration unreadable: {error}"}


def print_doctor(report: dict[str, Any]) -> None:
    print(f"Doctor: {'PASS' if report['ok'] else 'FAIL'}  ({report['checked_at_local']})")
    print(f"herdr:        {report.get('herdr_bin')}  HERDR_ENV={'yes' if report['herdr_env'] else 'no'}")
    print(f"config:       {report['config_file']}")
    print(f"state dir:    {report['state_dir']}  worker_lock_held={report['worker_lock_held']}")
    print(f"project root: {report['project_root']}")
    print(f"task state:   {report.get('task_state') or 'NO_TASK'}")
    for provider in ("claude", "codex"):
        agent = report["agents"].get(provider, {})
        print(
            f"{provider.title():<7} agent: {agent.get('name')} detected={agent.get('detected')} lifecycle={agent.get('lifecycle')} "
            f"pane={agent.get('pane_id')} native_session={agent.get('native_session_id')} match={agent.get('session_matches')}"
        )
    _print_quota(report)
    backup = report.get("backup") or {}
    print(f"backup:       {backup.get('status', 'disabled')}" + (f"  last_verified={backup.get('last_verified')}" if backup.get("last_verified") else "") + (f"  reason={backup.get('failure_reason')}" if backup.get("failure_reason") else ""))
    for warning in report.get("warnings", []):
        print(f"WARNING: {warning}")
    for error in report.get("errors", []):
        print(f"ERROR: {error}")


def request_control(supervisor: Supervisor, desired: str) -> int:
    state = supervisor.store.read_state()
    if state["supervisor_state"] in TERMINAL_STATES:
        raise SupervisorError(f"task is already {state['supervisor_state']}")
    supervisor.store.write_control(desired)
    if not WorkerLock.is_held(supervisor.paths.lock_file):
        # No worker will observe the control file; apply the transition directly.
        state["supervisor_state"] = "PAUSED" if desired == "paused" else "CANCELLED"
        state["worker_pid"] = None
        if desired == "cancelled":
            state["cancel_note"] = "supervision stopped; native Claude/Codex sessions and history preserved"
        supervisor.store.write_state(state)
        supervisor.store.append_log(state, desired, applied_by="control command")
        print(f"{desired}: applied (no worker running)")
    else:
        print(f"{desired}: requested; the running worker will apply it at its next safe checkpoint")
    return 0


def migrate_install(supervisor: Supervisor) -> int:
    """One-time local migration: protected V1 backup, schema-2 rewrite, 0700/0600 tightening.
    Never touches services, sessions, or the product repository."""
    paths = supervisor.paths
    paths.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(paths.state_dir, 0o700)
    for sub in (paths.logs_dir, paths.backups_dir, paths.inbox_dir, paths.outbox_dir, paths.query_dir):
        sub.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(sub, 0o700)
    with supervisor.store.transaction():
        if paths.state_file.exists():
            state = supervisor.store.read_state()
            if state.get("supervisor_state") in GATE_STATES and supervisor.store._disk_schema() == STATE_SCHEMA_VERSION:
                pass  # already V2; nothing to migrate
            supervisor.store.write_state(state)
            print(f"state: schema {STATE_SCHEMA_VERSION} ({paths.state_file})")
        else:
            print("state: no task file (nothing to migrate)")
    for path in paths.state_dir.iterdir():
        if path.is_file():
            os.chmod(path, 0o600)
    for path in paths.backups_dir.glob("*.json"):
        print(f"backup: {path}")
    print(f"permissions: {oct(paths.state_dir.stat().st_mode & 0o777)} {paths.state_dir}; files 0600")
    return 0


def show_logs(paths: Paths, lines: int) -> int:
    state = StateStore(paths, time.time).read_state()
    path = paths.logs_dir / f"{state['run_id']}.jsonl"
    if not path.exists():
        raise SupervisorError(f"no log exists for run {state['run_id']}")
    with path.open(encoding="utf-8") as handle:
        for line in handle.readlines()[-lines:]:
            print(line, end="")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="herdr-supervisor", description="Supervise existing Herdr Claude/Codex sessions without replacing their workflow.")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="start a new supervised task (foreground worker)")
    run.add_argument("--start", choices=PROVIDERS, default="codex", help="entry agent (default: codex)")
    run.add_argument("--policy", choices=WORKFLOW_POLICIES, default="v1", help="v1 (default) or gated_v2 human-gate workflow")
    run.add_argument("--codex-reset-budget", type=int, default=0, help="explicit per-run automatic Codex banked-reset budget (default: 0)")
    run.add_argument("task", help="task text or path to a task file")
    status = sub.add_parser("status", help="show task, agents, quota, and context state")
    status.add_argument("--json", action="store_true")
    sub.add_parser("pause", help="pause the supervisor at its next safe checkpoint")
    sub.add_parser("resume", help="resume the persisted task in the same native sessions (foreground worker)")
    sub.add_parser("cancel", help="stop supervising; native agent sessions and history are preserved")
    logs = sub.add_parser("logs", help="show the current run's structured log")
    logs.add_argument("--lines", type=int, default=100)
    doctor = sub.add_parser("doctor", help="read-only live environment check")
    doctor.add_argument("--json", action="store_true")
    # ---- V2 gate and worker commands
    for name, help_text in (("approve", "approve the pending plan/push gate (one-time, hash-bound)"), ("reject", "reject the pending gate"), ("revise", "request a revision of the pending gate with a note"), ("answer", "answer the pending generic question")):
        cmd = sub.add_parser(name, help=help_text)
        cmd.add_argument("--run-id", required=True)
        cmd.add_argument("--gate-id", required=True)
        cmd.add_argument("--actor", default="cli")
        if name == "revise":
            cmd.add_argument("note")
        if name == "answer":
            cmd.add_argument("answer")
        if name == "reject":
            cmd.add_argument("--note", default=None)
    for name in ("runtime-pass", "runtime-fail"):
        cmd = sub.add_parser(name, help=f"record runtime validation {name.split('-')[1].upper()} evidence for the exact candidate SHA")
        cmd.add_argument("--run-id", required=True)
        cmd.add_argument("--candidate-sha", required=True)
        cmd.add_argument("--environment", required=True)
        cmd.add_argument("--evidence-file", required=True)
        cmd.add_argument("--actor", default="cli")
    sub.add_parser("worker", help="detached worker entry point: consume the command inbox, reconcile, continue safe states")
    refresh = sub.add_parser("refresh-quota", help="run-bound, rate-limited request for the waiting worker to recheck quota (non-LLM; never resends)")
    refresh.add_argument("--run-id", required=True)
    register_query = sub.add_parser("register-query", help="bind an existing idle read-only query agent to its exact native session")
    register_query.add_argument("--provider", choices=PROVIDERS, required=True)
    register_query.add_argument("--agent-name", required=True)
    register_query.add_argument("--acknowledge-read-only-contract", action="store_true")
    sub.add_parser("migrate", help="back up V1 state, write schema 2, tighten state permissions (no service activation)")
    events = sub.add_parser("events", help="list durable outbox events for the current run")
    events.add_argument("--json", action="store_true")
    enqueue = sub.add_parser("enqueue", help="write a JSON command file into the worker inbox (used by the Telegram bridge)")
    enqueue.add_argument("command_file")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    paths = Paths.from_environment()
    try:
        config = load_config(paths.config_file)
        herdr = HerdrCli(resolve_herdr_bin(config.get("herdr_bin")))
        supervisor = Supervisor(paths, config, herdr)
        if args.command == "run":
            task, reference, char_limit = args.task, None, None
            if Path(task).is_file():
                task, reference, _digest = load_task_file(str(Path(task).resolve()), config=config, root=None)
                char_limit = int(config["max_task_file_bytes"])
            if args.codex_reset_budget < 0:
                raise SupervisorError("--codex-reset-budget cannot be negative")
            authorization = None
            if args.codex_reset_budget:
                inventory = supervisor.codex_reset_inventory(f"cli-inventory-{uuid.uuid4()}")
                if args.codex_reset_budget > inventory.available_count:
                    raise SupervisorError("requested Codex reset budget exceeds current available inventory")
                authorization = {"budget": args.codex_reset_budget, "available_count": inventory.available_count,
                                 "account_fingerprint": inventory.account_fingerprint}
            kwargs = {"task_reference": reference, "workflow_policy": args.policy, "char_limit": char_limit}
            if authorization is not None:
                kwargs["codex_reset_authorization"] = authorization
            return supervisor.run_new(task, args.start, **kwargs)
        if args.command in ("approve", "reject", "revise", "answer"):
            with supervisor.store.transaction():
                state = supervisor.store.read_state()
                common = {"run_id": args.run_id, "gate_id": args.gate_id, "actor": args.actor}
                if args.command == "approve":
                    result = supervisor.approve_gate(state, **common)
                elif args.command == "reject":
                    result = supervisor.reject_gate(state, note=args.note, **common)
                elif args.command == "revise":
                    result = supervisor.revise_gate(state, note=args.note, **common)
                else:
                    result = supervisor.answer_gate(state, answer=args.answer, **common)
            print(result["message"])
            print("next: herdr-supervisor resume   (or let the detached worker pick it up)")
            return 0
        if args.command in ("runtime-pass", "runtime-fail"):
            with supervisor.store.transaction():
                state = supervisor.store.read_state()
                result = supervisor.record_runtime_evidence(state, run_id=args.run_id, candidate_sha=args.candidate_sha, environment=args.environment, evidence_file=args.evidence_file, result="PASS" if args.command == "runtime-pass" else "FAIL", actor=args.actor)
            print(result["message"])
            return 0
        if args.command == "refresh-quota":
            path = supervisor.enqueue_command({"request_id": f"refresh-{args.run_id[:8]}-{int(time.time())}", "action": "refresh_quota", "run_id": args.run_id, "actor": "cli"})
            print(f"queued: {path.name}")
            return 0
        if args.command == "register-query":
            result = register_query_provider(
                paths, config, herdr, provider=args.provider, agent_name=args.agent_name,
                acknowledge_read_only_contract=args.acknowledge_read_only_contract,
            )
            print(
                f"registered {result['provider']} query agent {result['agent_name']} "
                f"session {result['session_id_abbrev']} under: {result['read_only_contract']}"
            )
            return 0
        if args.command == "worker":
            return supervisor.worker()
        if args.command == "migrate":
            return migrate_install(supervisor)
        if args.command == "events":
            events = supervisor.list_events()
            if args.json:
                print(json.dumps(events, indent=2, sort_keys=True))
            else:
                for event in events:
                    print(f"{event.get('at_utc')} #{event.get('sequence')} {event.get('type')} {json.dumps(event.get('data'), ensure_ascii=False)[:160]}")
            return 0
        if args.command == "enqueue":
            command = load_json(Path(args.command_file), label="command file")
            if not isinstance(command, dict):
                raise SupervisorError("command file must be a JSON object")
            print(str(supervisor.enqueue_command(command)))
            return 0
        if args.command == "resume":
            return supervisor.resume()
        if args.command == "pause":
            return request_control(supervisor, "paused")
        if args.command == "cancel":
            return request_control(supervisor, "cancelled")
        if args.command == "logs":
            if args.lines <= 0:
                raise SupervisorError("--lines must be positive")
            return show_logs(paths, args.lines)
        if args.command == "status":
            report = supervisor.status()
            if args.json:
                print(json.dumps(report, indent=2, sort_keys=True))
            else:
                print_status(report)
            return 0
        if args.command == "doctor":
            report = supervisor.doctor()
            if args.json:
                print(json.dumps(report, indent=2, sort_keys=True))
            else:
                print_doctor(report)
            return 0 if report["ok"] else 1
    except SupervisorError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
