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

WORKFLOW_POLICIES = ("v1", "gated_v2", "supervised_v2")

ROLE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

# Transient read-only `agent.wait` transport failures (supervisor reliability, batch 2): at most WAIT_RECOVERY_MAX_ATTEMPTS recovery attempts within
# WAIT_RECOVERY_WINDOW_SECONDS per failure episode, paced; only a successful wait (or a verified settled/blocked transition) ends an episode.
WAIT_TRANSIENT_CODES = frozenset({"server_unavailable"})
WAIT_RECOVERY_MAX_ATTEMPTS = 3
WAIT_RECOVERY_WINDOW_SECONDS = 30.0
WAIT_RECOVERY_DELAYS = (1.0, 2.0, 4.0)

# Policies that use the V2 typed-gate protocol and carry a resolved workflow_contract. `v1` predates the
# V2 protocol entirely and is retained only for already-migrated historical state; it is deliberately
# excluded here and untouched by the contract machinery below.
V2_WORKFLOW_POLICIES = ("gated_v2", "supervised_v2")

# Immutable, resolved per-policy contract: what a run may and must do, decided once at task start and
# never reinterpreted from mutable configuration afterward (see herdr_validation.resolve_workflow_contract).
# `gated_v2` is Prospera's strict Codex-plans/Claude-implements procedure; every field below matches its
# current hardcoded behavior exactly. `supervised_v2` is the general policy: planning is optional, either
# provider may start or receive a handoff, and a voluntary plan gate accepts any safe Markdown artifact
# below the review root instead of the literal CODEX_PLAN.md filename. Both share the same protocol-safety
# machinery (typed gates, loop/auto-turn limits, delivery-certainty handling); only workflow procedure
# differs. A typed gate is mandatory for every HERDR_NEXT=human route under every V2 policy without
# exception — this is universal protocol safety, not a contract field: an untyped human wait is never
# permitted, so no policy may opt out of it (see route_v2 in herdr_workflow.py).
WORKFLOW_CONTRACTS: dict[str, dict[str, Any]] = {
    "gated_v2": {
        "workflow_contract_version": 1,
        "policy": "gated_v2",
        "protocol_version": 2,
        "plan_approval_required_for_handoff": True,
        "plan_approval_required_for_done": True,
        "allowed_start_agents": ["codex"],
        "plan_gate_restricted_to_agent": "codex",
        "plan_artifact_name": "CODEX_PLAN.md",
    },
    "supervised_v2": {
        "workflow_contract_version": 1,
        "policy": "supervised_v2",
        "protocol_version": 2,
        "plan_approval_required_for_handoff": False,
        "plan_approval_required_for_done": False,
        "allowed_start_agents": ["codex", "claude"],
        "plan_gate_restricted_to_agent": None,
        "plan_artifact_name": None,
    },
}

EVENT_TYPES = (
    "TASK_STARTED", "PLAN_APPROVAL_REQUIRED", "PLAN_APPROVED", "PLAN_REJECTED", "REVISION_REQUESTED",
    "QUESTION_ASKED", "QUESTION_ANSWERED", "WAIT_USER", "WAIT_QUOTA", "QUOTA_RESUMED",
    "RUNTIME_VALIDATION_READY", "RUNTIME_VALIDATION_FAILED", "RUNTIME_VALIDATION_PASSED",
    "PUSH_APPROVAL_REQUIRED", "PUSH_APPROVED", "TASK_PAUSED", "TASK_RESUMED", "TASK_CANCELLED", "TASK_DONE",
    "TASK_ERROR", "RECOVERED_AFTER_RESTART", "COMMAND_RESULT", "QUERY_RESULT",
    "CODEX_RESET_AUTHORIZED", "CODEX_RESET_STARTED", "CODEX_RESET_VERIFIED", "CODEX_RESET_UNAVAILABLE",
    "TASK_HANDED_OFF", "AGENT_FOLLOWUP_READY", "RUNTIME_EVIDENCE_PROPOSED", "OWNER_PANE_REPAIRED",
    "STARTUP_WAIT_REQUIRED", "STARTUP_WAIT_RESULT",
)

# Who may turn runtime evidence into a canonical PASS/FAIL. Collaborative (default): agents only propose;
# an authenticated operator confirmation records. Automatic: only when the approved plan payload says so.
RUNTIME_VALIDATION_MODES = ("operator_collaborative", "automatic_agent")

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
    # Default workflow policy for new tasks that do not name one explicitly (CLI `--policy`, Telegram task
    # start). Must be a V2_WORKFLOW_POLICIES member; `v1` cannot be the default going forward. A workspace
    # (e.g. Prospera's local config) may set this to `gated_v2` to keep its strict procedure as the default
    # without changing the marketplace plugin's own default.
    "default_workflow_policy": "supervised_v2",
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
    # New-task-only native-session policy. Model profile values are identifiers, never raw argv;
    # the fixed --model argv is constructed only after a read-only local capability probe.
    "helpers": {"enabled": False},
    "native_tracking": {"enabled": False, "roots": [], "files": []},
    "session_start": {
        "enabled": True, "direction": "right", "ratio": 0.5,
        "provider_commands": {"codex": "codex", "claude": "claude"},
        "model_profiles": {
            "codex": {"default": {"label": "Default", "model": None}},
            "claude": {"default": {"label": "Default", "model": None}},
        },
    },
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

class OwnerRecoveryNeeded(SupervisorError):
    """The exact persisted native session cannot be targeted from its recorded pane: the pane is unavailable
    (`missing_pane`), the session is live in another pane (`moved`), or in several panes (`duplicate`).
    Structured so the runtime can persist an operator-recoverable condition instead of a free-form reason."""

    def __init__(self, message: str, *, provider: str, classification: str, session_id: str, recorded_pane: str | None, live_panes: list[str]) -> None:
        super().__init__(message)
        self.provider = provider
        self.classification = classification
        self.session_id = session_id
        self.recorded_pane = recorded_pane
        self.live_panes = list(live_panes)


OWNER_RECOVERY_CLASSIFICATIONS = ("missing_pane", "moved", "duplicate")


class StartupNotReady(SupervisorError):
    """A just-started provider process is not ready for work and nothing was sent to it (Herdr `agent_not_ready` /
    `agent_blocked`, a pane that never became a detected agent, or an agent reported blocked/unknown). It is only a
    SIGNAL for the session-start machinery: the caller decides whether it opens a human startup wait. It carries no
    authority and never implies that a prompt, update or dialog is on screen."""

    def __init__(self, message: str, *, site: str, code: str | None = None, pane_id: str | None = None, provider: str | None = None) -> None:
        super().__init__(message)
        self.site = site
        self.code = code
        self.pane_id = pane_id
        self.provider = provider


class BoundPreparationUnstarted(SupervisorError):
    """Another fresh-session preparation is already bound to the current owners but its task was never started. A different or new task cannot start until that original start is
    applied through its supported action (or the sessions are explicitly rebound); abandon cannot release a bound preparation, so no generic abandon advice may be attached."""


class AbsoluteDeadlinePassed(SupervisorError):
    """The caller's configured absolute ceiling (`session_start.startup_wait.not_after_unix`, e.g. a diagnostic's total deadline) passed: no further effect of a fresh-session
    preparation may start, whatever any wait says. Completed history is kept; nothing is rewound."""


class StartupWaitRequired(SupervisorError):
    """A durable, human-handled startup wait was opened (or is still open) for a prepared provider pane. Nothing
    was sent; the wait record names where the human acts (the diagnostic pane only) and how to continue or cancel."""

    def __init__(self, message: str, *, wait_id: str, provider: str, pane_id: str | None, context: str, expired: bool = False) -> None:
        super().__init__(message)
        self.wait_id = wait_id
        self.provider = provider
        self.pane_id = pane_id
        self.context = context
        self.expired = expired

# The recorded Herdr pane locator contract (`w<digits>:p<digits>`, e.g. w3:p2, w12:p10). One validator for
# owner records, recovery state, CLI/callback input, and live carriers before any mutation.
PANE_ID_RE = re.compile(r"^w[0-9]{1,6}:p[0-9]{1,6}$")


def valid_pane_id(value: Any) -> bool:
    return type(value) is str and PANE_ID_RE.fullmatch(value) is not None


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

    @property
    def session_preparations_dir(self) -> Path:
        return self.state_dir / "session-preparations"

    @property
    def startup_waits_dir(self) -> Path:
        """Durable human-handled startup waits (one 0600 record per wait; see herdr_startup)."""
        return self.state_dir / "startup-waits"

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
    if config.get("default_workflow_policy") not in V2_WORKFLOW_POLICIES:
        raise SupervisorError(f"default_workflow_policy must be one of {V2_WORKFLOW_POLICIES}")
    if not isinstance(config.get("quota_block_dismiss_keys"), list):
        raise SupervisorError("quota_block_dismiss_keys must be a list")
    roles = config.get("subagent_roles")
    if roles is not None:
        # Container types are checked before any nested access: a null/list/string/boolean setting is an actionable error, never an AttributeError.
        if not isinstance(roles, dict) or not set(roles) <= {"claude"}:
            raise SupervisorError("subagent_roles must be an object that maps claude to its settings")
        for provider, settings in roles.items():
            if not isinstance(settings, dict) or not set(settings) <= {"definitions_dir", "roles"}:
                raise SupervisorError(f"subagent_roles.{provider} must be an object with only definitions_dir and roles")
            names, directory = settings.get("roles"), settings.get("definitions_dir")
            if names is not None and (not isinstance(names, list) or not 1 <= len(names) <= 64 or not all(isinstance(n, str) and ROLE_NAME_RE.match(n) for n in names)):
                raise SupervisorError(f"subagent_roles.{provider}.roles must be 1-64 plain role names")
            if directory is not None and (not isinstance(directory, str) or not directory.startswith("/") or "\x00" in directory):
                raise SupervisorError(f"subagent_roles.{provider}.definitions_dir must be an absolute path")
    roots = config.get("native_session_roots")
    if roots is not None:
        # Optional override of the provider-native session roots used by the read-only result recovery; absolute paths only.
        if not isinstance(roots, dict) or not set(roots) <= set(PROVIDERS) or not all(isinstance(v, str) and v.startswith("/") and "\x00" not in v for v in roots.values()):
            raise SupervisorError("native_session_roots must map codex/claude to absolute paths")
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
    validate_session_start_config(config)
    for section in ("helpers", "native_tracking"):
        value = config.get(section)
        if not isinstance(value, dict) or type(value.get("enabled")) is not bool:
            raise SupervisorError(f"{section}.enabled must be boolean")
    tracking = config["native_tracking"]
    if not isinstance(tracking.get("roots"), list) or len(tracking["roots"]) > 8 or any(not isinstance(p, str) or not Path(p).is_absolute() for p in tracking["roots"]):
        raise SupervisorError("native_tracking.roots must be at most eight absolute paths")
    if not isinstance(tracking.get("files"), list) or len(tracking["files"]) > 32:
        raise SupervisorError("native_tracking.files must be at most 32 explicit registrations")

    return config

# Optional `session_start.codex_launch_method` (absent = agent-start): how a fresh Codex pane is launched. Mirrored by
# herdr_helper_context.CODEX_LAUNCH_METHODS (a test pins them equal). Inactive until an operator sets it.
CODEX_LAUNCH_METHODS = ("agent-start", "explicit-pane-run")
# Optional `session_start.codex_input_policy` (absent = require-empty-proof): the safety premise for queueing the explicit launch
# command. `guarded-new-pane` replaces the (unavailable) empty-input proof by an explicitly selected operating profile and is
# valid only with the explicit pane-run method. Mirrored by herdr_helper_context.CODEX_INPUT_POLICIES (a test pins them equal).
CODEX_INPUT_POLICIES = ("require-empty-proof", "guarded-new-pane")
# Optional `session_start.approval_modes` (absent = `inherit` for both providers): a closed per-provider choice of the automatic permission-review
# mode of FRESH launches. It maps to exactly one fixed provider flag and never to a raw argument; `inherit` keeps today's launch unchanged.
# Mirrored by herdr_helper_context.APPROVAL_MODES / APPROVAL_MODE_ARGS (a test pins them equal).
APPROVAL_MODES = {"codex": ("inherit", "auto-review"), "claude": ("inherit", "auto")}
APPROVAL_MODE_ARGS = {("codex", "auto-review"): ("--approve-for-me",), ("claude", "auto"): ("--permission-mode", "auto")}


def approval_mode(config: dict[str, Any], provider: str) -> str:
    """The configured (validated) approval mode of a provider's fresh launches; absent = inherit."""
    return str((config["session_start"].get("approval_modes") or {}).get(provider, "inherit"))


def approval_mode_args(provider: str, mode: str) -> list[str]:
    """The fixed launch arguments of a mode: none for inherit; any other value is refused, never passed through."""
    if mode == "inherit":
        return []
    if (provider, mode) not in APPROVAL_MODE_ARGS:
        raise SupervisorError(f"unsupported approval mode {mode!r} for {provider}")
    return list(APPROVAL_MODE_ARGS[(provider, mode)])


STARTUP_WAIT_DEFAULTS = {"max_seconds": 900, "max_waits_per_provider": 2, "max_continue_attempts": 5}


def startup_wait_settings(config: dict[str, Any]) -> dict[str, Any]:
    """Optional `session_start.startup_wait` (absent = the finite defaults): `max_seconds` bounds the TOTAL human wait of one
    preparation/run per provider (a later wait receives only what is left), `max_waits_per_provider` bounds how many waits one
    preparation may open, `max_continue_attempts` bounds refused continuations of one wait, and the optional `not_after_unix`
    is a hard wall-clock ceiling a caller with its own total deadline (a diagnostic) sets once; no wait extends past it."""
    section = (config.get("session_start") or {}).get("startup_wait", {})
    if not isinstance(section, dict) or set(section) - {*STARTUP_WAIT_DEFAULTS, "not_after_unix"}:
        raise SupervisorError("session_start.startup_wait must be an object with only max_seconds, max_waits_per_provider, max_continue_attempts, not_after_unix")
    merged: dict[str, Any] = {**STARTUP_WAIT_DEFAULTS, **{key: value for key, value in section.items() if key != "not_after_unix"}}
    for key, (low, high) in {"max_seconds": (60, 86400), "max_waits_per_provider": (1, 5), "max_continue_attempts": (1, 20)}.items():
        value = merged[key]
        if type(value) is not int or not low <= value <= high:
            raise SupervisorError(f"session_start.startup_wait.{key} must be an integer between {low} and {high}")
    ceiling = section.get("not_after_unix")
    if ceiling is not None and (isinstance(ceiling, bool) or not isinstance(ceiling, (int, float)) or ceiling <= 0):
        raise SupervisorError("session_start.startup_wait.not_after_unix must be a positive epoch number or absent")
    merged["not_after_unix"] = None if ceiling is None else float(ceiling)
    return merged


def validate_session_start_config(config: dict[str, Any]) -> None:
    section = config.get("session_start")
    if not isinstance(section, dict) or not isinstance(section.get("enabled"), bool):
        raise SupervisorError("session_start configuration is invalid")
    if section.get("direction") not in ("right", "down"):
        raise SupervisorError("session_start.direction must be right or down")
    # Optional key (absent = off): a paid minimal first turn that makes a provider report its native identity.
    if type(section.get("native_identity_bootstrap", False)) is not bool:
        raise SupervisorError("session_start.native_identity_bootstrap must be boolean")
    if section.get("codex_launch_method", "agent-start") not in CODEX_LAUNCH_METHODS:
        raise SupervisorError("session_start.codex_launch_method must be one of " + ", ".join(CODEX_LAUNCH_METHODS))
    policy = section.get("codex_input_policy", "require-empty-proof")
    if policy not in CODEX_INPUT_POLICIES:
        raise SupervisorError("session_start.codex_input_policy must be one of " + ", ".join(CODEX_INPUT_POLICIES))
    if policy == "guarded-new-pane" and section.get("codex_launch_method", "agent-start") != "explicit-pane-run":
        raise SupervisorError("session_start.codex_input_policy guarded-new-pane requires session_start.codex_launch_method explicit-pane-run")
    startup_wait_settings(config)  # optional block; raises on any invalid value
    modes = section.get("approval_modes", {})
    if not isinstance(modes, dict) or set(modes) - set(APPROVAL_MODES):
        raise SupervisorError("session_start.approval_modes must be an object keyed only by " + ", ".join(APPROVAL_MODES))
    for provider, mode in modes.items():
        if type(mode) is not str or mode not in APPROVAL_MODES[provider]:
            raise SupervisorError(f"session_start.approval_modes.{provider} must be one of " + ", ".join(APPROVAL_MODES[provider]))
    ratio = section.get("ratio")
    if isinstance(ratio, bool) or not isinstance(ratio, (int, float)) or not 0.1 <= float(ratio) <= 0.9:
        raise SupervisorError("session_start.ratio must be between 0.1 and 0.9")
    commands, profiles = section.get("provider_commands"), section.get("model_profiles")
    if not isinstance(commands, dict) or set(commands) != set(PROVIDERS) or not isinstance(profiles, dict) or set(profiles) != set(PROVIDERS):
        raise SupervisorError("session_start must configure both providers")
    profile_re = re.compile(r"^[a-z0-9][a-z0-9._-]{0,39}$")
    for provider in PROVIDERS:
        if not isinstance(commands[provider], str) or not commands[provider]:
            raise SupervisorError(f"session_start.provider_commands.{provider} must be a command name")
        entries = profiles[provider]
        if not isinstance(entries, dict) or "default" not in entries:
            raise SupervisorError(f"session_start.model_profiles.{provider} must include default")
        for profile_id, entry in entries.items():
            if not isinstance(profile_id, str) or not profile_re.fullmatch(profile_id) or not isinstance(entry, dict):
                raise SupervisorError(f"invalid {provider} model profile")
            label, model = entry.get("label"), entry.get("model")
            model_ok = model is None or (isinstance(model,str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,99}",model) is not None)
            if not isinstance(label, str) or not label or len(label) > 60 or not model_ok:
                raise SupervisorError(f"invalid {provider} model profile {profile_id}")
            if profile_id == "default" and model is not None:
                raise SupervisorError(f"{provider} default model profile must not override the model")

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
