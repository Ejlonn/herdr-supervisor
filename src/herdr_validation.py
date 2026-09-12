"""State defaults, migration and validation, the durable StateStore that enforces them, and the safe-file, task, upload, gate-payload, runtime-evidence, and query-registry validators."""

from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import json
import os
import re
import subprocess
import uuid
from pathlib import Path
from typing import Any, Callable, Literal, overload

from herdr_cli import HerdrCli, session_identity
from herdr_core import (
    _STAGE_RE,
    _UUID_RE,
    COMPLETION_MODES,
    FOLLOWUP_DECISION_KINDS,
    FOLLOWUP_STATES,
    FOLLOWUP_UNRESOLVED,
    GATE_STATES,
    GATE_TYPES,
    HANDOFF_UNMET_KINDS,
    HUMAN_WAIT_STATES,
    MAX_FOLLOWUP_QUESTION_CHARS,
    MAX_FOLLOWUP_SUMMARY_CHARS,
    MAX_HANDOFF_NOTE_CHARS,
    OWNER_RECOVERY_CLASSIFICATIONS,
    PROVIDERS,
    RUNTIME_VALIDATION_MODES,
    SCHEMA_VERSION,
    SHA_RE,
    STATE_SCHEMA_VERSION,
    SUPERVISOR_STATES,
    TERMINAL_STATES,
    WORKFLOW_POLICIES,
    Paths,
    SupervisorError,
    _epoch,
    _number,
    atomic_write_json,
    iso_utc,
    load_json,
    sha256_bytes,
    valid_pane_id,
)
from herdr_quota import _parse_windows


class StateStore:
    def __init__(self, paths: Paths, clock: Callable[[], float]) -> None:
        self.paths = paths
        self.clock = clock

    @overload
    def read_state(self) -> dict[str, Any]: ...

    @overload
    def read_state(self, *, required: Literal[True]) -> dict[str, Any]: ...

    @overload
    def read_state(self, *, required: bool) -> dict[str, Any] | None: ...

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
            # Additive V2 evolution: old schema-2 records gain zero reset authority, zero submission
            # metrics, and a fresh (zero) consecutive-turn count in memory; nothing is replayed.
            defaults = new_v2_fields(str(value.get("workflow_policy") or "v1"))
            for key in ("codex_reset", "prompt_metrics", "consecutive_auto_turns", "agent_followup", "session_selection", "session_preparation", "runtime_proposal", "owner_recovery"):
                value.setdefault(key, defaults[key])
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
        # Durable agent proposal awaiting operator confirmation (collaborative runtime validation).
        "runtime_proposal": None,
        # Structured exact-session pane-ownership recovery condition (missing/moved/duplicate pane).
        "owner_recovery": None,
        "push_approval": None,
        "continuation": None,
        "processed_requests": {},
        "recovery_count": 0,
        "migrated_from_v1_at": None,
        "wait_user_requires_action": False,
        "deferred_anomaly": None,
        "final_report": None,
        "operator_handoff_ready": None,
        "completion": None,
        # Supervisor submission metrics (prompts prepared, characters submitted); never provider tokens.
        "prompt_metrics": {"prompts": 0, "chars": 0, "by_provider": {p: {"prompts": 0, "chars": 0} for p in PROVIDERS}},
        # Consecutive automatic turns routed to the same provider since the last human continuation
        # or cross-provider handoff.
        "consecutive_auto_turns": {"provider": None, "count": 0},
        # Bounded descriptor of a settled accepted turn whose routing result could not be read.
        "missing_result": None,
        # Optional gate-preserving follow-up ("Ask agent"): absent for existing runs; never migrated.
        "agent_followup": None,
        "session_selection": {"policy": "preserve", "profiles": {p: "default" for p in PROVIDERS}, "fresh_providers": []},
        "session_preparation": None,
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
    selection = state.get("session_selection")
    if not isinstance(selection, dict) or selection.get("policy") not in ("preserve", "fresh-codex", "fresh-claude", "fresh-all"):
        raise SupervisorError("state session_selection is invalid")
    profiles=selection.get("profiles")
    if not isinstance(profiles,dict) or set(profiles)!=set(PROVIDERS) or any(not isinstance(value,str) or not value for value in profiles.values()):
        raise SupervisorError("state session model profiles are invalid")
    preparation=state.get("session_preparation")
    if preparation is not None and (not isinstance(preparation,dict) or preparation.get("status") not in ("OWNERSHIP_BOUND","TASK_STARTED")):
        raise SupervisorError("state session_preparation is invalid")
    if state.get("workflow_policy") not in WORKFLOW_POLICIES:
        raise SupervisorError(f"state has an invalid workflow_policy: {state.get('workflow_policy')!r}")
    for key in ("gate_sequence", "event_sequence", "recovery_count"):
        if isinstance(state.get(key), bool) or not isinstance(state.get(key), int) or state[key] < 0:
            raise SupervisorError(f"state field {key} is invalid")
    reset = state.get("codex_reset")
    if not isinstance(reset, dict):
        raise SupervisorError("state codex_reset is invalid")
    budget, used = reset.get("authorized_reset_budget"), reset.get("used_reset_count")
    if not isinstance(budget, int) or not isinstance(used, int) or isinstance(budget, bool) or isinstance(used, bool) or budget < 0 or used < 0 or used > budget:
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
    phase = str(reset["current_redemption_state"])
    idem_key, block_id = reset.get("current_idempotency_key"), reset.get("blocking_event_id")
    if phase in {"RESET_PREPARED", "RESET_CONSUMING", "RESET_VERIFYING", "RESET_RECONCILING", "RESET_VERIFIED"}:
        if not isinstance(idem_key, str) or not 16 <= len(idem_key) <= 128 or not isinstance(block_id, str) or not re.fullmatch(r"[0-9a-f]{64}", block_id):
            raise SupervisorError("state codex in-flight reset identity is invalid")
        if not fingerprint:
            raise SupervisorError("state codex in-flight reset account identity is missing")
    elif phase == "IDLE" and idem_key is not None:
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
    _validate_prompt_metrics(state.get("prompt_metrics"))
    consecutive = state.get("consecutive_auto_turns")
    if not isinstance(consecutive, dict) or set(consecutive) != {"provider", "count"} or (consecutive["provider"] is not None and consecutive["provider"] not in PROVIDERS) or type(consecutive["count"]) is not int or not 0 <= consecutive["count"] <= 1_000_000:
        raise SupervisorError("state consecutive_auto_turns is invalid")
    missing = state.get("missing_result")
    if missing is not None:
        _validate_missing_result(missing, state)
    ready = state.get("operator_handoff_ready")
    if ready is not None:
        _validate_handoff_ready(ready, state)
    followup = state.get("agent_followup")
    if followup is not None:
        _validate_agent_followup(followup, state)
    proposal = state.get("runtime_proposal")
    if proposal is not None:
        _validate_runtime_proposal(proposal, state)
    recovery = state.get("owner_recovery")
    if recovery is not None:
        _validate_owner_recovery(recovery, state)
    evidence = state.get("runtime_evidence")
    if isinstance(evidence, dict) and evidence.get("provenance") is not None:
        _validate_evidence_provenance(evidence["provenance"])
    completion = state.get("completion")
    if completion is not None:
        _validate_completion(completion, state)
        if state["supervisor_state"] != "DONE":
            raise SupervisorError("state completion record requires the DONE state")
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

def _exact_str(value: Any, *, max_len: int, allow_empty: bool = False) -> bool:
    return type(value) is str and len(value) <= max_len and (allow_empty or bool(value.strip()))

_MAX_METRIC = 10**12

def _validate_prompt_metrics(metrics: Any) -> None:
    def counter(value: Any) -> bool:
        return type(value) is int and 0 <= value <= _MAX_METRIC

    if not isinstance(metrics, dict) or set(metrics) != {"prompts", "chars", "by_provider"} or not counter(metrics["prompts"]) or not counter(metrics["chars"]):
        raise SupervisorError("state prompt_metrics is invalid")
    by_provider = metrics["by_provider"]
    if not isinstance(by_provider, dict) or set(by_provider) != set(PROVIDERS):
        raise SupervisorError("state prompt_metrics providers are invalid")
    for entry in by_provider.values():
        if not isinstance(entry, dict) or set(entry) != {"prompts", "chars"} or not counter(entry["prompts"]) or not counter(entry["chars"]):
            raise SupervisorError("state prompt_metrics provider counters are invalid")
    if sum(e["prompts"] for e in by_provider.values()) != metrics["prompts"] or sum(e["chars"] for e in by_provider.values()) != metrics["chars"]:
        raise SupervisorError("state prompt_metrics totals do not match provider counters")

def _validate_missing_result(missing: Any, state: dict[str, Any]) -> None:
    if not isinstance(missing, dict) or missing.get("schema_version") != 1 or type(missing.get("schema_version")) is not int:
        raise SupervisorError("state missing_result is invalid")
    for key in ("run_id", "turn_id"):
        if type(missing.get(key)) is not str or not _UUID_RE.fullmatch(missing[key]):
            raise SupervisorError(f"state missing_result {key} is invalid")
    if missing["run_id"] != state.get("run_id"):
        raise SupervisorError("state missing_result belongs to another run")
    if missing.get("agent") not in PROVIDERS:
        raise SupervisorError("state missing_result agent is invalid")
    if type(missing.get("attempts")) is not int or not 0 <= missing["attempts"] <= 1000:
        raise SupervisorError("state missing_result attempts is invalid")
    if type(missing.get("created_at_unix")) is bool or _epoch(missing.get("created_at_unix")) is None:
        raise SupervisorError("state missing_result timestamp is invalid")

_SHA256_STR_RE = re.compile(r"[0-9a-f]{64}")

def _validate_agent_followup(followup: Any, state: dict[str, Any]) -> None:
    """Fail closed on malformed, cross-run, cross-session, stale, or internally inconsistent records.
    Exact primitive types everywhere: a persisted record is data, never authority."""
    label = "state agent_followup"
    if not isinstance(followup, dict) or followup.get("schema_version") != 1 or type(followup.get("schema_version")) is not int:
        raise SupervisorError(f"{label} is invalid")
    for key in ("run_id", "followup_turn_id"):
        if type(followup.get(key)) is not str or not _UUID_RE.fullmatch(followup[key]):
            raise SupervisorError(f"{label} {key} is invalid")
    if followup["run_id"] != state.get("run_id"):
        raise SupervisorError(f"{label} belongs to another run")
    status = followup.get("status")
    if type(status) is not str or status not in FOLLOWUP_STATES:
        raise SupervisorError(f"{label} status is invalid")
    provider = followup.get("provider")
    if provider not in PROVIDERS or type(provider) is not str:
        raise SupervisorError(f"{label} provider is invalid")
    session = followup.get("session_id")
    if type(session) is not str or not session:
        raise SupervisorError(f"{label} session identity is invalid")
    if session != (state.get("native_sessions") or {}).get(provider):
        raise SupervisorError(f"{label} session identity does not match the run's native session")
    if not _exact_str(followup.get("actor"), max_len=200):
        raise SupervisorError(f"{label} actor is invalid")
    chat_id = followup.get("chat_id")
    if chat_id is not None and type(chat_id) is not int:
        raise SupervisorError(f"{label} chat_id is invalid")
    if not _exact_str(followup.get("question"), max_len=MAX_FOLLOWUP_QUESTION_CHARS):
        raise SupervisorError(f"{label} question is invalid")
    path = followup.get("response_path")
    if type(path) is not str or not path.startswith("/") or len(path) > 1024 or "\x00" in path or not path.endswith(".md"):
        raise SupervisorError(f"{label} response path is invalid")
    if type(followup.get("created_at_unix")) is bool or _epoch(followup.get("created_at_unix")) is None:
        raise SupervisorError(f"{label} timestamp is invalid")
    if type(followup.get("reread_attempts")) is not int or not 0 <= followup["reread_attempts"] <= 1000:
        raise SupervisorError(f"{label} reread_attempts is invalid")
    if type(followup.get("delivery_uncertain")) is not bool:
        raise SupervisorError(f"{label} delivery_uncertain flag is invalid")
    decision = followup.get("decision")
    if not isinstance(decision, dict) or decision.get("kind") not in FOLLOWUP_DECISION_KINDS or type(decision.get("kind")) is not str:
        raise SupervisorError(f"{label} decision is invalid")
    if type(decision.get("decision_id")) is not str or not _UUID_RE.fullmatch(decision["decision_id"]):
        raise SupervisorError(f"{label} decision identity is invalid")
    if decision.get("supervisor_state") not in HUMAN_WAIT_STATES or type(decision.get("supervisor_state")) is not str:
        raise SupervisorError(f"{label} decision state is invalid")
    if decision["kind"] == "gate":
        if decision.get("gate_type") not in GATE_TYPES or type(decision.get("gate_type")) is not str:
            raise SupervisorError(f"{label} decision gate type is invalid")
        for key in ("artifact_sha256", "payload_sha256"):
            if type(decision.get(key)) is not str or not _SHA256_STR_RE.fullmatch(decision[key]):
                raise SupervisorError(f"{label} decision {key} is invalid")
        gate = state.get("pending_gate")
        if status in FOLLOWUP_UNRESOLVED and (not isinstance(gate, dict) or gate.get("gate_id") != decision["decision_id"]):
            raise SupervisorError(f"{label} suspended gate is not the pending gate")
    else:
        if type(decision.get("attempts")) is not int or not 0 <= decision["attempts"] <= 1000:
            raise SupervisorError(f"{label} decision attempts is invalid")
        if decision["supervisor_state"] != "WAIT_USER":
            raise SupervisorError(f"{label} missing-result decision must suspend WAIT_USER")
    suspended = followup.get("suspended")
    if not isinstance(suspended, dict) or set(suspended) != {"supervisor_state", "active_agent", "wait_user_reason", "wait_user_requires_action", "delivery", "missing_result", "operator_handoff_ready"}:
        raise SupervisorError(f"{label} suspended descriptor is invalid")
    if suspended["supervisor_state"] != decision["supervisor_state"] or suspended["active_agent"] not in PROVIDERS:
        raise SupervisorError(f"{label} suspended descriptor does not match the decision")
    if type(suspended["wait_user_requires_action"]) is not bool:
        raise SupervisorError(f"{label} suspended wait flag is invalid")
    if suspended["wait_user_reason"] is not None and type(suspended["wait_user_reason"]) is not str:
        raise SupervisorError(f"{label} suspended reason is invalid")
    missing = suspended["missing_result"]
    if missing is not None:
        _validate_missing_result(missing, state)
    if decision["kind"] == "missing_result":
        if not isinstance(missing, dict) or missing.get("turn_id") != decision["decision_id"] or missing.get("attempts") != decision["attempts"]:
            raise SupervisorError(f"{label} suspended missing-result does not match the decision")
        delivery = suspended["delivery"]
        if not isinstance(delivery, dict) or delivery.get("turn_id") != decision["decision_id"]:
            raise SupervisorError(f"{label} suspended delivery does not match the missing-result turn")
    ready = suspended["operator_handoff_ready"]
    if ready is not None:
        _validate_handoff_ready(ready, state)
    response = followup.get("response")
    if status in ("RESPONSE_READY", "RESTORED"):
        if not isinstance(response, dict) or type(response.get("artifact_id")) is not str or not re.fullmatch(r"[0-9a-f]{32}", response["artifact_id"]):
            raise SupervisorError(f"{label} response artifact is invalid")
        if type(response.get("sha256")) is not str or not _SHA256_STR_RE.fullmatch(response["sha256"]):
            raise SupervisorError(f"{label} response hash is invalid")
        if type(response.get("bytes")) is not int or response["bytes"] < 1:
            raise SupervisorError(f"{label} response size is invalid")
        if not _exact_str(response.get("summary"), max_len=MAX_FOLLOWUP_SUMMARY_CHARS):
            raise SupervisorError(f"{label} response summary is invalid")
    elif response is not None:
        raise SupervisorError(f"{label} carries a response before one was verified")
    if status in ("FAILED", "ABANDONED", "RESTORED") and not _exact_str(followup.get("reason"), max_len=500, allow_empty=True) and followup.get("reason") is not None:
        raise SupervisorError(f"{label} reason is invalid")
    if status in FOLLOWUP_UNRESOLVED and status != "FAILED":
        if state.get("supervisor_state") not in ("RUNNING", "PAUSED", "ERROR", *TERMINAL_STATES):
            raise SupervisorError(f"{label} is in flight but the supervisor is {state.get('supervisor_state')}")
    if status == "FAILED" and state.get("supervisor_state") not in ("WAIT_USER", "PAUSED", "ERROR", *TERMINAL_STATES):
        raise SupervisorError(f"{label} failed without a follow-up wait")

FOLLOWUP_SECTIONS = ("Answer", "Recommendation", "Change needed", "Next operator action")

_FOLLOWUP_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$")

def _followup_heading_name(line: str) -> str | None:
    """Heading text normalized to the section name: level ignored, trailing colon or parenthetical
    (e.g. `Change needed (yes or no)`) dropped, case-insensitive. Non-headings and other titles -> None."""
    match = _FOLLOWUP_HEADING_RE.match(line)
    if not match:
        return None
    title = re.sub(r"\s*(\(.*\)|:)\s*$", "", match.group(1)).strip().lower()
    for name in FOLLOWUP_SECTIONS:
        if title == name.lower():
            return name
    return None

def validate_followup_markdown(raw: bytes, *, max_bytes: int) -> str:
    """Deterministic structure check for an agent's follow-up answer: strict UTF-8 within the artifact
    bound, the four required headings exactly once, in order, each with a non-empty body. Anything else is
    refused before registration (the file is never modified)."""
    if not raw or not raw.strip():
        raise SupervisorError("follow-up response file is empty")
    if len(raw) > max_bytes:
        raise SupervisorError("follow-up response exceeds max_artifact_bytes")
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise SupervisorError("follow-up response is not UTF-8 text") from error
    if "\x00" in text:
        raise SupervisorError("follow-up response contains NUL bytes")
    found: list[str] = []
    bodies: dict[str, list[str]] = {}
    current: str | None = None
    for line in text.splitlines():
        name = _followup_heading_name(line)
        if name is not None:
            if name in found:
                raise SupervisorError(f"follow-up response repeats the {name!r} section")
            found.append(name)
            bodies[name] = []
            current = name
        elif current is not None:
            bodies[current].append(line)
    if tuple(found) != FOLLOWUP_SECTIONS:
        missing = [name for name in FOLLOWUP_SECTIONS if name not in found]
        if missing:
            raise SupervisorError(f"follow-up response lacks the required section(s): {', '.join(missing)}")
        raise SupervisorError(f"follow-up response sections are out of order; expected {', '.join(FOLLOWUP_SECTIONS)}")
    for name in FOLLOWUP_SECTIONS:
        if not "".join(bodies[name]).strip():
            raise SupervisorError(f"follow-up response section {name!r} is empty")
    return text

def _valid_owner_record(value: Any) -> bool:
    return isinstance(value, dict) and set(value) == {"pane_id", "session_id"} and valid_pane_id(value["pane_id"]) and type(value["session_id"]) is str and bool(value["session_id"])

def owner_recovery_checkpoint(state: dict[str, Any]) -> dict[str, Any]:
    """The operational checkpoint a pane repair must return the run to. Canonical (sorted, primitive) so it
    can be compared exactly and bound into the recovery identity."""
    delivery = state.get("delivery") if isinstance(state.get("delivery"), dict) else None
    continuation = state.get("continuation") if isinstance(state.get("continuation"), dict) else None
    interrupted = state.get("interrupted_delivery") if isinstance(state.get("interrupted_delivery"), dict) else None
    return {
        "phase": str(state.get("phase") or "initial"),
        "active_agent": state.get("active_agent"),
        "delivery_turn_id": delivery.get("turn_id") if delivery else None,
        "delivery_status": delivery.get("status") if delivery else None,
        "delivery_kind": delivery.get("kind") if delivery else None,
        "delivery_prompt_sha256": delivery.get("prompt_sha256") if delivery else None,
        "continuation": json.loads(json.dumps(continuation, sort_keys=True)) if continuation else None,
        "interrupted_turn_id": interrupted.get("turn_id") if interrupted else None,
        "turns_completed": int(state.get("turns_completed") or 0),
        "gate_sequence": int(state.get("gate_sequence") or 0),
    }

# Delivery transitions that may legitimately happen while the run waits (restart recovery marks an
# unacknowledged submission uncertain). Anything else is drift.
_CHECKPOINT_DELIVERY_TRANSITIONS = {("prepared", "uncertain"), ("accepted", "uncertain")}

def checkpoint_drift(recorded: Any, current: dict[str, Any]) -> str | None:
    """None when `current` matches the recorded checkpoint (modulo the modelled delivery transitions); otherwise
    the first field that drifted."""
    if not isinstance(recorded, dict):
        return "checkpoint"
    for key, value in current.items():
        if key == "delivery_status":
            recorded_status = recorded.get("delivery_status")
            if value != recorded_status and (recorded_status, value) not in _CHECKPOINT_DELIVERY_TRANSITIONS:
                return "delivery_status"
            continue
        if recorded.get(key) != value:
            return key
    return None

def _validate_owner_recovery(recovery: Any, state: dict[str, Any]) -> None:
    label = "state owner_recovery"
    if not isinstance(recovery, dict) or recovery.get("schema_version") != 1 or type(recovery.get("schema_version")) is not int:
        raise SupervisorError(f"{label} is invalid")
    for key in ("run_id", "recovery_id"):
        if type(recovery.get(key)) is not str or not _UUID_RE.fullmatch(recovery[key]):
            raise SupervisorError(f"{label} {key} is invalid")
    if recovery["run_id"] != state.get("run_id"):
        raise SupervisorError(f"{label} belongs to another run")
    provider = recovery.get("provider")
    if provider not in PROVIDERS or type(provider) is not str:
        raise SupervisorError(f"{label} provider is invalid")
    session = recovery.get("session_id")
    if type(session) is not str or not session or session != (state.get("native_sessions") or {}).get(provider):
        raise SupervisorError(f"{label} session identity does not match the run's native session")
    if recovery.get("classification") not in OWNER_RECOVERY_CLASSIFICATIONS or type(recovery.get("classification")) is not str:
        raise SupervisorError(f"{label} classification is invalid")
    if not valid_pane_id(recovery.get("recorded_pane")):
        raise SupervisorError(f"{label} recorded pane is not a canonical Herdr pane id")
    owners_before = recovery.get("owners_before")
    if not isinstance(owners_before, dict) or set(owners_before) != set(PROVIDERS) or not all(_valid_owner_record(owners_before[p]) for p in PROVIDERS):
        raise SupervisorError(f"{label} owner snapshot is invalid")
    if owners_before[provider] != {"pane_id": recovery["recorded_pane"], "session_id": session}:
        raise SupervisorError(f"{label} owner snapshot does not match the affected owner")
    for other in PROVIDERS:
        if owners_before[other]["session_id"] != (state.get("native_sessions") or {}).get(other):
            raise SupervisorError(f"{label} owner snapshot does not match the run's native sessions")
    panes = recovery.get("live_panes")
    if not isinstance(panes, list) or len(panes) > 16 or any(not valid_pane_id(item) for item in panes):
        raise SupervisorError(f"{label} live panes are invalid")
    if type(recovery.get("created_at_unix")) is bool or _epoch(recovery.get("created_at_unix")) is None:
        raise SupervisorError(f"{label} timestamp is invalid")
    checkpoint = recovery.get("checkpoint")
    if not isinstance(checkpoint, dict) or set(checkpoint) != set(owner_recovery_checkpoint(state)):
        raise SupervisorError(f"{label} checkpoint is invalid")
    if type(checkpoint["phase"]) is not str or not _STAGE_RE.fullmatch(checkpoint["phase"]) or checkpoint["active_agent"] not in PROVIDERS:
        raise SupervisorError(f"{label} checkpoint phase/agent is invalid")
    turn = checkpoint["delivery_turn_id"]
    if turn is not None and (type(turn) is not str or not _UUID_RE.fullmatch(turn)):
        raise SupervisorError(f"{label} checkpoint turn is invalid")
    if (turn is None) != (checkpoint["delivery_status"] is None) or (checkpoint["delivery_status"] is not None and checkpoint["delivery_status"] not in ("prepared", "accepted", "uncertain", "interrupted", "completed")):
        raise SupervisorError(f"{label} checkpoint delivery is invalid")
    if checkpoint["continuation"] is not None and (not isinstance(checkpoint["continuation"], dict) or not _exact_str(checkpoint["continuation"].get("kind"), max_len=40)):
        raise SupervisorError(f"{label} checkpoint continuation is invalid")
    for key in ("turns_completed", "gate_sequence"):
        if type(checkpoint[key]) is not int or checkpoint[key] < 0:
            raise SupervisorError(f"{label} checkpoint {key} is invalid")
    if state.get("supervisor_state") not in ("WAIT_USER", "PAUSED", "ERROR", *TERMINAL_STATES):
        raise SupervisorError(f"{label} requires an owner-recovery wait")
    if state.get("supervisor_state") not in TERMINAL_STATES:
        drift = checkpoint_drift(checkpoint, owner_recovery_checkpoint(state))
        if drift is not None:
            raise SupervisorError(f"{label} checkpoint no longer matches the run ({drift} changed)")

def _validate_evidence_provenance(provenance: Any) -> None:
    if not isinstance(provenance, dict) or provenance.get("mode") not in RUNTIME_VALIDATION_MODES or type(provenance.get("mode")) is not str:
        raise SupervisorError("state runtime evidence provenance is invalid")
    if not _exact_str(provenance.get("actor"), max_len=200):
        raise SupervisorError("state runtime evidence provenance actor is invalid")
    chat_id = provenance.get("chat_id")
    if chat_id is not None and type(chat_id) is not int:
        raise SupervisorError("state runtime evidence provenance chat_id is invalid")
    if type(provenance.get("at_unix")) is bool or _epoch(provenance.get("at_unix")) is None:
        raise SupervisorError("state runtime evidence provenance timestamp is invalid")
    if provenance["mode"] == "operator_collaborative" and not _SHA256_STR_RE.fullmatch(str(provenance.get("proposal_sha256") or "")):
        raise SupervisorError("state runtime evidence provenance lacks the confirmed proposal hash")

def _validate_runtime_proposal(proposal: Any, state: dict[str, Any]) -> None:
    label = "state runtime_proposal"
    if not isinstance(proposal, dict) or proposal.get("schema_version") != 1 or type(proposal.get("schema_version")) is not int:
        raise SupervisorError(f"{label} is invalid")
    for key in ("run_id", "gate_id"):
        if type(proposal.get(key)) is not str or not _UUID_RE.fullmatch(proposal[key]):
            raise SupervisorError(f"{label} {key} is invalid")
    if proposal["run_id"] != state.get("run_id"):
        raise SupervisorError(f"{label} belongs to another run")
    if proposal.get("status") not in ("proposed", "accepted", "superseded") or type(proposal.get("status")) is not str:
        raise SupervisorError(f"{label} status is invalid")
    if proposal.get("result") not in ("PASS", "FAIL") or type(proposal.get("result")) is not str:
        raise SupervisorError(f"{label} result is invalid")
    if type(proposal.get("candidate_sha")) is not str or not SHA_RE.match(proposal["candidate_sha"]):
        raise SupervisorError(f"{label} candidate is invalid")
    if not _exact_str(proposal.get("environment"), max_len=40):
        raise SupervisorError(f"{label} environment is invalid")
    if type(proposal.get("evidence_path")) is not str or not proposal["evidence_path"].startswith("/"):
        raise SupervisorError(f"{label} evidence path is invalid")
    if type(proposal.get("evidence_sha256")) is not str or not _SHA256_STR_RE.fullmatch(proposal["evidence_sha256"]):
        raise SupervisorError(f"{label} evidence hash is invalid")
    if not _exact_str(proposal.get("proposed_by"), max_len=200):
        raise SupervisorError(f"{label} proposer is invalid")
    if type(proposal.get("proposed_at_unix")) is bool or _epoch(proposal.get("proposed_at_unix")) is None:
        raise SupervisorError(f"{label} timestamp is invalid")
    if type(proposal.get("legacy")) is not bool:
        raise SupervisorError(f"{label} legacy flag is invalid")
    summary = proposal.get("summary")
    if summary is not None and not _exact_str(summary, max_len=600, allow_empty=True):
        raise SupervisorError(f"{label} summary is invalid")

def _validate_unmet(unmet: Any, label: str) -> None:
    """Non-empty, duplicate-free allowlist of exact strings. Element types are checked before any
    hashing so a nested JSON value (dict/list) fails as SupervisorError, never as a raw TypeError."""
    if not isinstance(unmet, list) or not unmet:
        raise SupervisorError(f"{label} unmet requirements are invalid")
    if any(type(item) is not str or item not in HANDOFF_UNMET_KINDS for item in unmet):
        raise SupervisorError(f"{label} unmet requirements are invalid")
    if len(set(unmet)) != len(unmet):
        raise SupervisorError(f"{label} unmet requirements contain duplicates")

def _validate_handoff_ready(ready: Any, state: dict[str, Any]) -> None:
    """Machine-readable marker: a `done` route was refused only for missing runtime/push evidence."""
    if not isinstance(ready, dict) or ready.get("schema_version") != 1 or type(ready.get("schema_version")) is not int:
        raise SupervisorError("state operator_handoff_ready is invalid")
    for key in ("run_id", "turn_id"):
        if type(ready.get(key)) is not str or not _UUID_RE.fullmatch(ready[key]):
            raise SupervisorError(f"state operator_handoff_ready {key} is invalid")
    if ready["run_id"] != state.get("run_id"):
        raise SupervisorError("state operator_handoff_ready belongs to another run")
    if type(ready.get("stage")) is not str or not _STAGE_RE.fullmatch(ready["stage"]):
        raise SupervisorError("state operator_handoff_ready stage is invalid")
    if ready.get("agent") not in PROVIDERS:
        raise SupervisorError("state operator_handoff_ready agent is invalid")
    _validate_unmet(ready.get("unmet"), "state operator_handoff_ready")
    if _epoch(ready.get("created_at_unix")) is None or type(ready.get("created_at_unix")) is bool:
        raise SupervisorError("state operator_handoff_ready timestamp is invalid")
    if not _exact_str(ready.get("handoff"), max_len=300, allow_empty=True):
        raise SupervisorError("state operator_handoff_ready handoff text is invalid")

def _validate_completion(completion: Any, state: dict[str, Any]) -> None:
    """Durable terminal record. `mode` says how the run ended; operator_handoff never implies verification."""
    if not isinstance(completion, dict) or completion.get("schema_version") != 1 or type(completion.get("schema_version")) is not int:
        raise SupervisorError("state completion record is invalid")
    if completion.get("mode") not in COMPLETION_MODES or type(completion.get("mode")) is not str:
        raise SupervisorError("state completion mode is invalid")
    for key in ("run_id", "ready_turn_id"):
        if type(completion.get(key)) is not str or not _UUID_RE.fullmatch(completion[key]):
            raise SupervisorError(f"state completion {key} is invalid")
    if completion["run_id"] != state.get("run_id"):
        raise SupervisorError("state completion belongs to another run")
    if type(completion.get("stage")) is not str or not _STAGE_RE.fullmatch(completion["stage"]):
        raise SupervisorError("state completion stage is invalid")
    if not _exact_str(completion.get("actor"), max_len=200):
        raise SupervisorError("state completion actor is invalid")
    chat_id = completion.get("chat_id")
    if chat_id is not None and type(chat_id) is not int:
        raise SupervisorError("state completion chat_id is invalid")
    if type(completion.get("at_unix")) is bool or _epoch(completion.get("at_unix")) is None:
        raise SupervisorError("state completion timestamp is invalid")
    note = completion.get("note")
    if note is not None and not _exact_str(note, max_len=MAX_HANDOFF_NOTE_CHARS):
        raise SupervisorError("state completion note is invalid")
    _validate_unmet(completion.get("unmet"), "state completion")
    if completion.get("verified_by_supervisor") is not False:
        raise SupervisorError("state completion must record that Supervisor did not verify the handed-off actions")
    ready = state.get("operator_handoff_ready")
    if isinstance(ready, dict) and completion["ready_turn_id"] != ready.get("turn_id"):
        raise SupervisorError("state completion does not match the persisted readiness marker")

def safe_gate_view(gate: Any) -> dict[str, Any] | None:
    if not isinstance(gate, dict):
        return None
    keys = ("gate_id", "gate_type", "status", "run_id", "turn_id", "created_at", "expires_at", "expected_state",
            "artifact_path", "artifact_sha256", "payload_sha256", "candidate_sha", "summary_fields", "agent")
    return {key: gate.get(key) for key in keys}

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
        mode = raw.get("runtime_validation_mode", "operator_collaborative")
        if mode not in RUNTIME_VALIDATION_MODES or type(mode) is not str:
            raise SupervisorError("runtime_validation_mode must be operator_collaborative or automatic_agent")
        out["runtime_validation_mode"] = mode
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
        declared_mode = raw.get("validation_mode")
        if declared_mode is not None and (type(declared_mode) is not str or declared_mode not in RUNTIME_VALIDATION_MODES):
            raise SupervisorError("validation_mode must be operator_collaborative or automatic_agent when present")
        if declared_mode is not None:
            out["validation_mode"] = declared_mode  # informational; the approved plan policy decides
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
