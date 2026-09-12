"""V2 workflow mixin: events, gates, inbox/outbox, approvals, operator handoff, missing-result retry, the gate-preserving agent follow-up, and deterministic recovery."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from herdr_cli import match_exact_session, session_identity
from herdr_core import (
    ACTIONABLE_EVENTS,
    EVENT_NAMESPACE,
    EVENT_TYPES,
    FOLLOWUP_UNRESOLVED,
    GATE_STATE_FOR,
    GATE_STATES,
    GATE_TYPES,
    HUMAN_WAIT_STATES,
    MAX_FOLLOWUP_QUESTION_CHARS,
    MAX_HANDOFF_NOTE_CHARS,
    PLAN_CHANGED_MESSAGE,
    PROVIDERS,
    READY_STATES,
    SHA_RE,
    TERMINAL_STATES,
    HerdrError,
    Paths,
    SupervisorError,
    WorkerLock,
    atomic_write_json,
    canonical_json,
    iso_utc,
    load_json,
    pid_alive,
    sha256_bytes,
    sha256_file,
)
from herdr_protocol import FollowupFrame, ProtocolBlock, parse_followup, parse_protocol
from herdr_redaction import credential_value_present
from herdr_validation import (
    _UPLOAD_ID_RE,
    FOLLOWUP_SECTIONS,
    StateStore,
    _bounded_str,
    check_safe_file,
    load_task_file,
    safe_gate_view,
    validate_followup_markdown,
    validate_gate_payload,
    validate_runtime_evidence,
    validate_upload_record,
)


class SupervisorV2Mixin:
    """V2 behaviour attached to Supervisor. Every method here is fail-closed and touches state only
    through the caller's transaction/worker context."""

    # Host contract: the coordinator class (herdr_runtime.Supervisor) provides these. Declared here so the
    # mixin type-checks on its own and the dependency stays one-way (workflow never imports runtime).
    store: StateStore
    clock: Callable[[], float]
    config: dict[str, Any]
    paths: Paths
    herdr: Any
    head_resolver: Callable[[Path], str]

    if TYPE_CHECKING:  # pragma: no cover - typing-only declarations of host methods

        def _log(self, state: dict[str, Any], event: str, **details: Any) -> None: ...

        def set_wait_user(self, state: dict[str, Any], reason: str, *, requires_action: bool = False) -> str: ...

        def owners(self) -> dict[str, dict[str, str]]: ...

        def agent_name(self, provider: str) -> str: ...

        def ensure_agent(self, provider: str, *, allow_restore: bool = True) -> dict[str, Any]: ...

        def read_output(self, provider: str, *, settled: bool) -> str: ...

        def report_live_agent(self, provider: str, live_agents: list[dict[str, Any]], owners: dict[str, Any] | None) -> tuple[dict[str, Any] | None, bool, bool]: ...

        def verify_or_seed_owners(self) -> dict[str, dict[str, str]]: ...

        def verify_persisted_sessions(self, state: dict[str, Any]) -> None: ...

        def codex_reset_inventory(self, request_id: str) -> Any: ...

        def initialize(self, task: str, start: str, *, task_reference: str | None = None, workflow_policy: str = "v1", char_limit: int | None = None, codex_reset_authorization: dict[str, Any] | None = None, run_id: str | None = None) -> dict[str, Any]: ...

        def route(self, state: dict[str, Any], block: ProtocolBlock) -> str: ...

        def run_loop(self, state: dict[str, Any], *, recovering: bool = False) -> int: ...

        @staticmethod
        def _exit_code(state: dict[str, Any]) -> int: ...

        def _guarded(self, state: dict[str, Any], body: Callable[[], int]) -> int: ...

        def prompt_ack_available(self) -> bool: ...

        @staticmethod
        def _ack_status(response: Any) -> str | None: ...

        def check_control(self, state: dict[str, Any]) -> bool: ...

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

    def protocol_instructions(self, state: dict[str, Any], turn_id: str) -> str:
        """The eight-line routing contract. Placeholders keep angle brackets so an echoed template can
        never parse as a response; the controller enforces every rule stated here."""
        return (
            "End your response with exactly one contiguous eight-line block, each line KEY=value, no markdown:\n"
            "HERDR_PROTOCOL=2\n"
            f"HERDR_RUN={state['run_id']}\n"
            f"HERDR_TURN={turn_id}\n"
            "HERDR_STAGE=<completed stage, e.g. plan, brief, implement, review, fix>\n"
            "HERDR_NEXT=<codex|claude|done|human>\n"
            "HERDR_GATE=<none|plan_approval|generic_question|runtime_validation|push_approval>\n"
            f"HERDR_PAYLOAD=<gate payload JSON path below {self.config['review_root']}, or - when HERDR_GATE=none>\n"
            "HERDR_HANDOFF=<one line under 300 characters, words_joined_with_underscores, no spaces or angle brackets>\n"
            "Rules: human needs a typed gate and payload; codex/claude/done need HERDR_GATE=none and HERDR_PAYLOAD=-; "
            "Claude implements only after the human approves the exact CODEX_PLAN.md; runtime and push gates are separate; "
            "never reuse an earlier block."
        )

    def build_prompt_v2(self, state: dict[str, Any], turn_id: str, kind: str) -> str:
        protocol = self.protocol_instructions(state, turn_id)
        run = state["run_id"]
        if kind.startswith("continuation:"):
            cont = state.get("continuation") or (state.get("delivery") or {}).get("continuation") or {}
            ckind = cont.get("kind")
            note = cont.get("note") or ""
            if ckind == "plan_approved":
                body = (f"Run {run}: the human APPROVED the plan (SHA-256 {cont.get('plan_sha256')}, payload {cont.get('payload_sha256')}). "
                        "Write or update CODEX_BRIEF.md from exactly that plan per AGENTS.md, then route to the implementation agent. Do not change the plan.")
            elif ckind == "revision":
                body = (f"Run {run}: the human requested a REVISION of the pending {cont.get('gate_type')}. Note:\n{note}\n\n"
                        "Update the artifact and payload and emit a new gate block; the prior gate and approval are void.")
            elif ckind == "answer":
                body = f"Run {run}: the human ANSWERED your question ({cont.get('gate_id')}).\nAnswer:\n{note}\n\nContinue the same stage with this answer."
            elif ckind == "runtime_passed":
                body = f"Run {run}: runtime validation PASS recorded for candidate {cont.get('candidate_sha')} on {cont.get('environment')}. Continue with the next stage."
            elif ckind == "push_approved":
                body = f"Run {run}: the human AUTHORIZED the push stage for candidate {cont.get('candidate_sha')} and performs the push/PR themselves; do not run git push. Continue the workflow."
            elif ckind == "reconcile":
                body = (f"Run {run}: a provider usage-limit wait has ended. Your accepted turn HERDR_TURN={cont.get('turn_id')} settled without a valid "
                        "routing result, most likely cut by quota. Do not redo it and do not assume unfinished work is complete: inspect this session and the "
                        "disk state, continue the interrupted stage from the current point, and emit this turn's routing result.")
            elif ckind == "quota":
                previous = state.get("interrupted_delivery") or {}
                body = f"Run {run}: a provider usage-limit wait has ended. Your turn HERDR_TURN={previous.get('turn_id')} was interrupted; continue that stage from where it stopped without redoing completed work."
            else:
                body = f"Run {run}: continue the current stage.\n{note}"
            return body + "\n\n" + protocol
        if kind == "continuation":  # V1 quota continuation path reused under gated_v2
            previous = state.get("interrupted_delivery") or {}
            return (f"Run {run}: a provider usage-limit wait has ended. Your turn HERDR_TURN={previous.get('turn_id')} in this session was interrupted; "
                    "continue that stage from where it stopped without redoing completed work.\n\n" + protocol)
        preamble = (f"You are the active agent for supervised run {run} (gated workflow). Follow CLAUDE.md / AGENTS.md and their context under "
                    f"{self.config['project_root']}; the supervisor only routes turns and enforces human gates. Do only the workflow stage that is correct now.")
        handoff = state.get("last_successful_handoff")
        handoff_text = ""
        if isinstance(handoff, dict):
            handoff_text = f"\nPrevious stage: {handoff.get('stage')} (by {handoff.get('from_agent')})\nHandoff: {handoff.get('summary')}\n"
        if kind == "initial":
            task_context = f"\n\nTask:\n{state['task_text']}"  # the task body is delivered exactly once, here
        elif state.get("task_reference"):
            task_context = f"\n\nAuthoritative task artifact: {state['task_reference']}"
        else:
            task_context = "\n\nThe task is preserved in this native conversation and supervisor state; continue from the handoff without replaying it."
        return f"{preamble}{task_context}\n{handoff_text}\n{protocol}"

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
                unmet: list[str] = []
                if block.next_agent == "done":
                    problem = self._approved_plan_problem(state)
                    if not problem:
                        problem, unmet = self._done_problem(state)
                else:
                    problem = self._routing_policy_problem(state, block.next_agent)
                if problem:
                    if unmet:
                        # The agent reports completion and nothing but human-owned evidence is missing.
                        # Record that fact durably so the human may close the run as an operator
                        # handoff; nothing is inferred from the reason text and nothing completes here.
                        state["operator_handoff_ready"] = {
                            "schema_version": 1, "run_id": state["run_id"], "turn_id": block.turn_id, "stage": block.stage,
                            "agent": source, "unmet": unmet, "created_at_unix": self.clock(), "handoff": block.handoff[:300],
                        }
                        problem += "; /done (operator handoff) closes the task if you perform the remaining actions yourself"
                    return self.set_wait_user(state, problem, requires_action=True)
                if block.next_agent == source and (block.stage == state.get("phase") or state.get("phase") == "initial"):
                    # No progress: the same agent at the same stage, or the first turn routing to itself
                    # instead of finishing a stage or handing off. One stop, never another wake.
                    return self.set_wait_user(
                        state,
                        f"{source} routed back to itself without changing stage ({block.stage}); request a revision (/revise) or cancel instead of repeatedly waking the same agent",
                        requires_action=True,
                    )
                if block.next_agent in PROVIDERS:
                    problem = self._auto_turn_limit_problem(state, source, block.next_agent)
                    if problem:
                        return self.set_wait_user(state, problem, requires_action=True)
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
            if policy == "gated_v2" and block.next_agent in PROVIDERS:
                self._count_auto_turn(state, source, block.next_agent)
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

    # ----- automatic-turn circuit breaker

    def _auto_turn_limit_problem(self, state: dict[str, Any], source: str, target: str) -> str | None:
        """Refuse to prepare automatic turn limit+1 for the same provider. Only same-provider automatic
        routes count; nothing here reads, prepares, or wakes anything."""
        if target != source:
            return None
        limit = int(self.config["max_consecutive_auto_turns"])
        current = state.get("consecutive_auto_turns") or {"provider": None, "count": 0}
        streak = int(current["count"]) if current.get("provider") == target else 0
        if streak >= limit:
            return (f"{target} has taken {streak} consecutive automatic turns (limit {limit}); the supervisor stopped before "
                    f"preparing another one. Request a revision with /revise to continue, or cancel.")
        return None

    def _count_auto_turn(self, state: dict[str, Any], source: str, target: str) -> None:
        """Same-provider automatic route: extend the streak. Cross-provider handoff: the streak is over."""
        if target != source:
            state["consecutive_auto_turns"] = {"provider": None, "count": 0}
            return
        current = state.get("consecutive_auto_turns") or {"provider": None, "count": 0}
        state["consecutive_auto_turns"] = {"provider": target, "count": (int(current["count"]) + 1) if current.get("provider") == target else 1}

    def _reset_auto_turns(self, state: dict[str, Any]) -> None:
        """A real authenticated human continuation gives the workflow new authority or information."""
        state["consecutive_auto_turns"] = {"provider": None, "count": 0}

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
        state["operator_handoff_ready"] = None
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
            return self._done_problem(state)[0]
        return None

    def _done_problem(self, state: dict[str, Any]) -> tuple[str | None, list[str]]:
        """(problem, evidence_unmet) for a `done` route. `evidence_unmet` is non-empty only when the
        approval is intact, the candidate still equals HEAD, and the sole blockers are human-owned
        runtime/push evidence — the one situation an operator may close by handoff."""
        if not isinstance(state.get("approved_plan"), dict):
            return "done is not allowed before a plan approval exists", []
        policy = state.get("runtime_policy") or {}
        if policy.get("runtime_validation_required") or policy.get("push_approval_required"):
            head_problem = self._candidate_head_problem(state)
            if head_problem:
                return "done is blocked: " + head_problem, []
        unmet = []
        if policy.get("runtime_validation_required") and not self._runtime_pass_current(state):
            unmet.append("runtime_validation_pass")
        if policy.get("push_approval_required") and not self._push_approval_current(state):
            unmet.append("push_approval")
        if "runtime_validation_pass" in unmet:
            return "done is blocked: runtime validation is required and no current exact-SHA PASS evidence exists", unmet
        if "push_approval" in unmet:
            return "done is blocked: push approval is required and has not been granted for the current candidate", unmet
        return None, []

    # ----- operator handoff: the one eligibility rule for CLI, Telegram, callbacks, and status

    def operator_handoff_eligibility(self, state: dict[str, Any] | None, *, run_id: str | None = None, ready_turn_id: str | None = None,
                                     live_agents: list[dict[str, Any]] | None = None, inspect_agents: bool = True) -> tuple[bool, str]:
        """Return (eligible, reason). Every rejection names its cause and mutates nothing. With
        inspect_agents the live Herdr lifecycle must show the active workflow agent settled; a caller
        without Herdr access (the Telegram bridge) gets the state-only answer and the applying worker
        re-runs the full check before anything changes."""
        if state is None:
            return False, "no supervised task exists"
        current = state.get("supervisor_state")
        if run_id is not None and run_id != state.get("run_id"):
            return False, "run_id does not match the current task"
        if current in TERMINAL_STATES:
            return False, f"task is already {current}"
        if (state.get("pending_gate") or {}).get("status") == "pending":
            return False, "a typed gate is pending; act on it instead"
        if current != "WAIT_USER":
            return False, f"task is {current}; operator handoff needs a settled action-required wait"
        if not state.get("wait_user_requires_action"):
            return False, "this WAIT_USER is an ordinary pause, not a completion handoff; use /resume"
        ready = state.get("operator_handoff_ready")
        if not isinstance(ready, dict):
            return False, "the agent has not reported completion blocked only by runtime/push evidence; nothing to hand off"
        if ready.get("run_id") != state.get("run_id"):
            return False, "handoff readiness belongs to another run"
        if ready_turn_id is not None and ready_turn_id != ready.get("turn_id"):
            return False, "handoff readiness changed since this action was offered; use the newest card or /status"
        if state.get("quota_wait") is not None:
            return False, "a quota wait is in flight"
        if self.followup_unresolved(state) is not None:
            return False, "a follow-up question is unresolved; return to the decision first"
        phase = (state.get("codex_reset") or {}).get("current_redemption_state")
        if phase not in ("IDLE", "RESET_VERIFIED"):
            return False, f"a Codex reset is being reconciled ({phase})"
        if state.get("continuation") is not None:
            return False, "a continuation is still pending delivery"
        if state.get("deferred_anomaly") is not None:
            return False, "an anomaly is deferred for reconciliation"
        delivery = state.get("delivery")
        if isinstance(delivery, dict) and delivery.get("status") != "completed":
            return False, f"prompt delivery is {delivery.get('status')}; wait for it to settle"
        if pid_alive(state.get("worker_pid")):
            return False, "a worker still owns the agents"
        if self.paths.lock_file.exists() and WorkerLock.is_held(self.paths.lock_file) and WorkerLock.holder_pid(self.paths.lock_file) != os.getpid():
            # Another process holds the agents. The worker applying an inbox command holds the lock itself
            # while no turn is active (worker_pid is None and delivery is settled), which is not a conflict.
            return False, "a worker still owns the agents"
        if inspect_agents:
            if self.herdr is None:
                return False, "agent lifecycle cannot be inspected from this control path"
            try:
                agents = live_agents if live_agents is not None else self.herdr.list_agents()
                owners = self.owners()
            except (HerdrError, SupervisorError) as error:
                return False, f"agent lifecycle could not be established: {error}"
            provider = str(state.get("active_agent"))
            live, ambiguous, identity_unavailable = self.report_live_agent(provider, agents, owners)
            if ambiguous or identity_unavailable or live is None:
                return False, f"{provider} session could not be located unambiguously; not settled"
            if live.get("agent_status") not in READY_STATES:
                return False, f"{provider} is still {live.get('agent_status')}; not settled"
        return True, "operator handoff available"

    def retry_routing_result(self, state: dict[str, Any], *, run_id: Any, turn_id: Any, actor: str) -> dict[str, Any]:
        """Reread and reparse the settled accepted turn whose result could not be read. Submits nothing,
        wakes nothing: one settled transcript read, then the ordinary route (a recovered gate is created
        once through the normal path). Bound to the exact run and turn; idempotent per request id."""
        missing = state.get("missing_result")
        delivery = state.get("delivery")
        if not isinstance(run_id, str) or run_id != state.get("run_id"):
            raise SupervisorError("retry must be bound to the current run")
        if self.followup_unresolved(state) is not None:
            raise SupervisorError("a follow-up question is unresolved; return to the decision before retrying the routing result")
        if state.get("supervisor_state") != "WAIT_USER" or not state.get("wait_user_requires_action") or not isinstance(missing, dict):
            raise SupervisorError("no unread routing result is pending for this run")
        if not isinstance(turn_id, str) or turn_id != missing.get("turn_id") or not isinstance(delivery, dict) or delivery.get("turn_id") != turn_id:
            raise SupervisorError("retry does not match the settled turn whose result is missing")
        if (state.get("pending_gate") or {}).get("status") == "pending":
            raise SupervisorError("a typed gate is pending; nothing to retry")
        if int(missing.get("attempts") or 0) != 0:
            raise SupervisorError("the one-time routing retry was already used for this turn; ask the agent, request a revision with /revise, or cancel")
        if self.herdr is None:
            raise SupervisorError("the transcript cannot be read from this control path")
        provider = missing["agent"]
        name = self.agent_name(provider)
        try:
            agent = self.ensure_agent(provider, allow_restore=False)
        except (SupervisorError, HerdrError) as error:
            raise SupervisorError(f"cannot inspect {name} for the retry: {error}") from error
        if agent.get("agent_status") not in READY_STATES:
            raise SupervisorError(f"{name} is {agent.get('agent_status')}; the settled result can only be reread once the agent is idle")
        try:
            output = self.read_output(provider, settled=True)
            block = parse_protocol(output, state["run_id"], turn_id)
        except HerdrError as error:
            raise SupervisorError(f"cannot read {name} output: {error}") from error
        missing["attempts"] = int(missing["attempts"]) + 1
        missing["last_attempt_unix"] = self.clock()
        self._log(state, "routing_result_retry", agent=provider, turn_id=turn_id, actor=actor, recovered=block is not None)
        if block is None:
            self.store.write_state(state)
            return {"ok": False, "message": f"still no routing result for turn {turn_id[:8]} in the settled transcript; no approval was created and nothing was resent. Ask the agent, request a revision with /revise, or cancel."}
        state["missing_result"] = None
        state["wait_user_reason"] = None
        state["wait_user_requires_action"] = False
        outcome = self.route(state, block)
        gate = state.get("pending_gate") or {}
        if gate.get("status") == "pending":
            message = f"routing result recovered for turn {turn_id[:8]}: {gate.get('gate_type')} gate created; act on it"
        elif outcome == "continue":
            # A recovered provider handoff is recorded but never continued by the retry: the next delivery
            # is a model wake-up and belongs to an explicit human /resume, not to a transcript reread.
            target = state.get("active_agent")
            self.set_wait_user(state, f"routing result recovered for turn {turn_id[:8]}: handoff to {target} recorded; /resume delivers that turn", requires_action=False)
            message = f"routing result recovered for turn {turn_id[:8]}: handoff to {target} recorded; nothing was sent — /resume continues in the same session"
        else:
            message = f"routing result recovered for turn {turn_id[:8]}: {state.get('supervisor_state')}"
        return {"ok": True, "message": message}

    def complete_operator_handoff(self, state: dict[str, Any], *, run_id: str, actor: str, chat_id: Any = None, note: str | None = None,
                                  ready_turn_id: str | None = None, live_agents: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """Close the run as an operator handoff. No prompt, read, wake-up, evidence, or approval is
        produced; the record states exactly which human-owned actions Supervisor did not verify."""
        ok, reason = self.operator_handoff_eligibility(state, run_id=run_id, ready_turn_id=ready_turn_id, live_agents=live_agents)
        if not ok:
            raise SupervisorError(f"operator handoff refused: {reason}")
        if chat_id is not None and type(chat_id) is not int:
            raise SupervisorError("chat_id must be an integer when present")
        bounded_note = _bounded_str(note, "note", max_len=MAX_HANDOFF_NOTE_CHARS) if note else None
        ready = state["operator_handoff_ready"]
        now = self.clock()
        state["completion"] = {
            "schema_version": 1, "mode": "operator_handoff", "run_id": state["run_id"], "stage": ready["stage"],
            "ready_turn_id": ready["turn_id"], "actor": _bounded_str(actor, "actor", max_len=200), "chat_id": chat_id,
            "at_unix": now, "at_utc": iso_utc(now), "note": bounded_note, "unmet": list(ready["unmet"]), "verified_by_supervisor": False,
        }
        state["supervisor_state"] = "DONE"
        state["completed_at"] = iso_utc(now)
        state["worker_pid"] = None
        state["wait_user_reason"] = None
        state["wait_user_requires_action"] = False
        state["continuation"] = None
        state["final_report"] = self.final_report_descriptor(state)
        event_data = {"stage": ready["stage"], "actor": state["completion"]["actor"], "unmet": list(ready["unmet"]), "note": bounded_note,
                      "handoff": ready.get("handoff"), "verified_by_supervisor": False}
        if state["final_report"] is not None:
            event_data["final_report"] = state["final_report"]
        self.emit_event(state, "TASK_HANDED_OFF", event_data)
        self.store.write_state(state)
        self._log(state, "operator_handoff", actor=state["completion"]["actor"], unmet=list(ready["unmet"]))
        return {"ok": True, "message": "task closed by operator handoff; Supervisor did not verify the remaining external actions (" + ", ".join(u.replace("_", " ") for u in ready["unmet"]) + ")"}

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
        if self.followup_unresolved(state) is not None:
            raise SupervisorError("a follow-up question is unresolved; wait for its answer or return to the decision before acting on the gate")
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
            self._reset_auto_turns(state)
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
            self._reset_auto_turns(state)
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
        if state.get("run_id") == run_id:
            self._abandon_followup_for_revision(state, actor)
        gate = self._load_pending_gate(state, run_id, gate_id, expected_state=expected_state, gate_types=GATE_TYPES)
        self._finish_gate(state, gate, "revision_requested", actor=actor, chat_id=chat_id, note=note)
        state["continuation"] = {"kind": "revision", "gate_id": gate_id, "gate_type": gate["gate_type"], "note": note}
        self._reset_auto_turns(state)
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
        self._abandon_followup_for_revision(state, actor)
        if state.get("supervisor_state") != "WAIT_USER" or not state.get("wait_user_requires_action"):
            raise SupervisorError("guidance is only accepted for an action-required WAIT_USER; use the pending gate's id otherwise")
        if (state.get("pending_gate") or {}).get("status") == "pending":
            raise SupervisorError("a typed gate is pending; act on it instead")
        state["continuation"] = {"kind": "revision", "gate_id": None, "gate_type": "wait_user", "note": note}
        state["supervisor_state"] = "RUNNING"
        state["wait_user_reason"] = None
        state["wait_user_requires_action"] = False
        state["operator_handoff_ready"] = None
        state["missing_result"] = None
        self._reset_auto_turns(state)
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
        self._reset_auto_turns(state)
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
        self._reset_auto_turns(state)
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

    # ----- gate-preserving agent follow-up ("Ask agent")
    #
    # One bounded question goes to the same active native session while a human decision (a pending typed
    # gate or an action-required missing-result wait) is suspended; the decision is persisted first and
    # restored unchanged after one verified response. Nothing here approves, revises, advances, replays the
    # task body, adopts a session, or applies a recommended fix.

    @staticmethod
    def followup_unresolved(state: dict[str, Any] | None) -> dict[str, Any] | None:
        followup = (state or {}).get("agent_followup")
        return followup if isinstance(followup, dict) and followup.get("status") in FOLLOWUP_UNRESOLVED else None

    def followup_decision(self, state: dict[str, Any] | None) -> dict[str, Any] | None:
        """The one suspendable decision in the current state (data only): a pending typed gate, or an
        action-required missing-result wait. Any other state has nothing an agent follow-up may preserve."""
        if not state or state.get("supervisor_state") in TERMINAL_STATES:
            return None
        gate = state.get("pending_gate")
        if isinstance(gate, dict) and gate.get("status") == "pending" and state.get("supervisor_state") == gate.get("expected_state"):
            return {"kind": "gate", "decision_id": gate["gate_id"], "gate_type": gate["gate_type"], "supervisor_state": gate["expected_state"],
                    "artifact_sha256": gate["artifact_sha256"], "payload_sha256": gate["payload_sha256"], "provider": gate["agent"]}
        missing = state.get("missing_result")
        if isinstance(missing, dict) and state.get("supervisor_state") == "WAIT_USER" and state.get("wait_user_requires_action"):
            return {"kind": "missing_result", "decision_id": missing["turn_id"], "supervisor_state": "WAIT_USER", "attempts": int(missing.get("attempts") or 0), "provider": missing["agent"]}
        return None

    @staticmethod
    def followup_decision_label(decision: dict[str, Any] | None) -> str:
        if not isinstance(decision, dict):
            return "decision"
        if decision.get("kind") == "gate":
            return f"{str(decision.get('gate_type') or 'gate').replace('_', ' ')} decision"
        return "unread-result recovery"

    def followup_review_directory(self, state: dict[str, Any]) -> Path:
        """Deterministic home for the response file: the pending gate's review directory, else the approved
        plan's, else a run-named directory below the review root. Always below the review root."""
        gate = state.get("pending_gate")
        if isinstance(gate, dict) and gate.get("status") == "pending":
            directory = (gate.get("payload") or {}).get("review_directory")
            if isinstance(directory, str) and directory.startswith("/"):
                return Path(directory)
        approved = state.get("approved_plan")
        if isinstance(approved, dict) and isinstance(approved.get("plan_path"), str):
            return Path(approved["plan_path"]).parent
        return Path(self.config["review_root"]) / f"run-{str(state['run_id'])[:8]}"

    def _followup_destination_problem(self, path_text: Any, *, require_absent: bool = False) -> str | None:
        """Why a (persisted or freshly computed) response destination must not be handed to an agent: it must be
        an absolute `.md` path whose parent is a real directory strictly below the configured review root with
        no symlink anywhere between them, and the file itself, if present, must not be a symlink. Data only:
        a tampered state record never becomes a write instruction."""
        if not isinstance(path_text, str) or not path_text.startswith("/") or "\x00" in path_text or not path_text.endswith(".md"):
            return "response destination must be an absolute .md path"
        path = Path(path_text)
        review_root = Path(self.config["review_root"])
        try:
            resolved_root = review_root.resolve(strict=True)
        except OSError:
            return f"review root is unavailable: {review_root}"
        parent = path.parent
        if not parent.is_dir() or os.path.islink(parent):
            return "response directory is missing or a symlink"
        try:
            resolved_parent = parent.resolve(strict=True)
        except OSError:
            return "response directory is unavailable"
        if resolved_parent != resolved_root and resolved_root not in resolved_parent.parents:
            return f"response destination is outside the review root {review_root}"
        if resolved_parent == resolved_root:
            return "response destination must be below a run or review directory, not the review root itself"
        walker = parent
        while walker != review_root and walker != walker.parent:
            if os.path.islink(walker):
                return f"response directory path contains a symlink: {walker}"
            walker = walker.parent
        if os.path.islink(path):
            return "response destination is a symlink"
        if require_absent and path.exists():
            return "response destination already exists"
        return None

    def _prepare_followup_destination(self, directory: Path) -> None:
        """Create the deterministic response directory (0700) only after proving that every existing
        ancestor between it and the review root is a real directory below that root; never follow symlinks."""
        review_root = Path(self.config["review_root"])
        try:
            resolved_root = review_root.resolve(strict=True)
        except OSError as error:
            raise SupervisorError(f"review root is unavailable: {review_root}") from error
        if resolved_root not in directory.resolve().parents:
            raise SupervisorError("response directory would fall outside the review root; refusing")
        walker = directory
        while walker != review_root and walker != walker.parent:
            if os.path.islink(walker):
                raise SupervisorError(f"response directory path contains a symlink: {walker}; refusing")
            walker = walker.parent
        if directory.exists() and not directory.is_dir():
            raise SupervisorError("response directory path exists and is not a directory")
        if not directory.exists():
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(directory, 0o700)

    def _followup_problem(self, state: dict[str, Any]) -> str | None:
        """Why a follow-up cannot start now (data checks only; the worker re-runs identity checks)."""
        if state.get("supervisor_state") in TERMINAL_STATES:
            return f"task is already {state['supervisor_state']}"
        if self.followup_unresolved(state) is not None:
            return "a follow-up question is already in flight; wait for its answer or return to the decision first"
        if state.get("quota_wait") is not None or state.get("supervisor_state") == "WAIT_QUOTA":
            return "a quota wait is in flight"
        phase = (state.get("codex_reset") or {}).get("current_redemption_state")
        if phase not in ("IDLE", "RESET_VERIFIED"):
            return f"a Codex reset is being reconciled ({phase})"
        if state.get("continuation") is not None:
            return "a continuation is still pending delivery"
        if state.get("deferred_anomaly") is not None:
            return "an anomaly is deferred for reconciliation"
        delivery = state.get("delivery")
        if isinstance(delivery, dict) and delivery.get("status") not in ("completed", None):
            return f"prompt delivery is {delivery.get('status')}; wait for it to settle"
        if pid_alive(state.get("worker_pid")):
            return "a worker still owns the agents"
        return None

    def ask_agent(self, state: dict[str, Any], *, run_id: Any, actor: str, question: Any, chat_id: Any = None, decision_id: Any = None,
                  expected_state: Any = None, artifact_sha256: Any = None, payload_sha256: Any = None) -> dict[str, Any]:
        """Persist the suspended decision and a PREPARED follow-up. Nothing is submitted here: the worker
        delivers exactly once from the durable record. Stale bindings are rejected before anything changes."""
        if not isinstance(run_id, str) or run_id != state.get("run_id"):
            raise SupervisorError("ask-agent must be bound to the current run")
        text = _bounded_str(question, "question", max_len=MAX_FOLLOWUP_QUESTION_CHARS)
        if credential_value_present(text):
            raise SupervisorError("ask-agent refused: the question appears to contain a credential value; questions are stored and forwarded verbatim, so remove the secret and ask again")
        if chat_id is not None and type(chat_id) is not int:
            raise SupervisorError("chat_id must be an integer when present")
        problem = self._followup_problem(state)
        if problem:
            raise SupervisorError(f"ask-agent refused: {problem}")
        decision = self.followup_decision(state)
        if decision is None:
            raise SupervisorError("ask-agent refused: no pending gate or unread-result wait to preserve; nothing to ask about")
        if decision_id is not None and decision_id != decision["decision_id"]:
            raise SupervisorError("ask-agent refused: the decision changed since this question was offered; use the newest card or /status")
        if expected_state is not None and expected_state != state.get("supervisor_state"):
            raise SupervisorError(f"ask-agent refused: state changed ({state.get('supervisor_state')}); use the newest card or /status")
        if decision["kind"] == "gate":
            gate = state["pending_gate"]
            if self.clock() > float(gate.get("expires_at_unix") or 0):
                raise SupervisorError("ask-agent refused: the gate has expired")
            self._check_gate_hashes(gate, artifact_sha256 if isinstance(artifact_sha256, str) else None, payload_sha256 if isinstance(payload_sha256, str) else None)
        provider = decision.pop("provider")
        session = (state.get("native_sessions") or {}).get(provider)
        try:
            recorded = self.owners()[provider]["session_id"]
        except SupervisorError as error:
            raise SupervisorError(f"ask-agent refused: native session ownership is unavailable: {error}") from error
        if not isinstance(session, str) or not session or session != recorded:
            raise SupervisorError(f"ask-agent refused: the recorded {provider} native session no longer matches this run; not contacting a different conversation")
        turn_id = str(uuid.uuid4())
        directory = self.followup_review_directory(state)
        self._prepare_followup_destination(directory)  # before PREPARED is persisted: a failure leaves the decision untouched
        response_path = str(directory / f"AGENT_FOLLOWUP_{turn_id[:8]}.md")
        destination_problem = self._followup_destination_problem(response_path, require_absent=True)
        if destination_problem:
            raise SupervisorError(f"ask-agent refused: {destination_problem}")
        state["agent_followup"] = {
            "schema_version": 1, "run_id": state["run_id"], "followup_turn_id": turn_id, "status": "PREPARED",
            "provider": provider, "session_id": session, "actor": _bounded_str(actor, "actor", max_len=200), "chat_id": chat_id,
            "question": text, "response_path": response_path,
            "created_at_unix": self.clock(), "reread_attempts": 0, "delivery_uncertain": False, "decision": decision,
            "suspended": {
                "supervisor_state": state["supervisor_state"], "active_agent": state["active_agent"],
                "wait_user_reason": state.get("wait_user_reason"), "wait_user_requires_action": bool(state.get("wait_user_requires_action")),
                "delivery": state.get("delivery"), "missing_result": state.get("missing_result"), "operator_handoff_ready": state.get("operator_handoff_ready"),
            },
            "response": None, "reason": None,
        }
        # The decision is persisted above; from here the run is the follow-up's until it is restored.
        state["supervisor_state"] = "RUNNING"
        state["active_agent"] = provider
        state["wait_user_reason"] = None
        state["wait_user_requires_action"] = False
        state["delivery"] = None
        state["missing_result"] = None
        state["operator_handoff_ready"] = None
        self.store.write_state(state)
        self._log(state, "followup_prepared", followup_turn_id=turn_id, provider=provider, decision=decision["kind"], actor=actor)
        label = self.followup_decision_label(decision)
        return {"ok": True, "message": f"question queued for {provider}; the {label} is preserved and returns unchanged after the answer", "followup_turn_id": turn_id}

    def _restore_suspended(self, state: dict[str, Any], followup: dict[str, Any], *, status: str, reason: str | None) -> None:
        """Put the exact suspended decision back. The gate record was never touched; approval history,
        retry count, stage, task, and native-session ownership are untouched by construction."""
        suspended = followup["suspended"]
        state["supervisor_state"] = suspended["supervisor_state"]
        state["active_agent"] = suspended["active_agent"]
        state["wait_user_reason"] = suspended["wait_user_reason"]
        state["wait_user_requires_action"] = suspended["wait_user_requires_action"]
        state["delivery"] = suspended["delivery"]
        state["missing_result"] = suspended["missing_result"]
        state["operator_handoff_ready"] = suspended["operator_handoff_ready"]
        state["worker_pid"] = None
        followup["status"] = status
        followup["reason"] = reason
        followup["restored_at_unix"] = self.clock()

    def _followup_failed(self, state: dict[str, Any], reason: str, *, delivery_uncertain: bool) -> str:
        """Follow-up-specific fail-closed wait: the decision stays suspended (never silently restored while
        an answer may still arrive); the human chooses Retry reading answer, Return to decision, or revision."""
        followup = state["agent_followup"]
        followup["status"] = "FAILED"
        followup["reason"] = reason[:500]
        followup["delivery_uncertain"] = bool(delivery_uncertain)
        state["supervisor_state"] = "WAIT_USER"
        state["wait_user_reason"] = ("The question may have reached the agent, but no verified answer was returned. " if delivery_uncertain else "The question was not sent. ") + reason
        state["wait_user_requires_action"] = True
        state["worker_pid"] = None
        self.emit_event(state, "WAIT_USER", {"reason": state["wait_user_reason"], "followup": self.followup_view(followup)})
        self.store.write_state(state)
        self._log(state, "followup_failed", followup_turn_id=followup["followup_turn_id"], reason=reason[:200], delivery_uncertain=delivery_uncertain)
        return "stop"

    @staticmethod
    def followup_view(followup: Any) -> dict[str, Any] | None:
        """Bounded, secret-free view for status/events/cards (no transcript, no path internals)."""
        if not isinstance(followup, dict):
            return None
        decision = followup.get("decision") or {}
        response = followup.get("response") or {}
        return {
            "followup_turn_id": followup.get("followup_turn_id"), "status": followup.get("status"), "provider": followup.get("provider"),
            "question": followup.get("question"), "decision_kind": decision.get("kind"), "decision_id": decision.get("decision_id"),
            "gate_type": decision.get("gate_type"), "suspended_state": decision.get("supervisor_state"), "reread_attempts": followup.get("reread_attempts"),
            "delivery_uncertain": followup.get("delivery_uncertain"), "reason": followup.get("reason"),
            "summary": response.get("summary"), "artifact_id": response.get("artifact_id"),
        }

    def return_to_decision(self, state: dict[str, Any], *, run_id: Any, followup_turn_id: Any, actor: str) -> dict[str, Any]:
        """Abandon only the unresolved follow-up and restore the suspended gate/wait. No approval, revision,
        routing retry, prompt, or wake-up. Refused while a worker still owns the follow-up."""
        followup = self.followup_unresolved(state)
        if not isinstance(run_id, str) or run_id != state.get("run_id"):
            raise SupervisorError("return-to-decision must be bound to the current run")
        if followup is None:
            raise SupervisorError("no unresolved follow-up exists; the decision is already in place")
        if followup_turn_id is not None and followup_turn_id != followup["followup_turn_id"]:
            raise SupervisorError("this return-to-decision belongs to an earlier follow-up; use the newest card")
        if pid_alive(state.get("worker_pid")):
            raise SupervisorError("a worker is still delivering the follow-up; wait for it to settle")
        if followup["status"] == "RESPONSE_READY":
            self._complete_followup(state)  # a verified answer was already durable: deliver it rather than discard it
            return {"ok": True, "message": "a verified answer was already registered; it is delivered and the decision is restored unchanged", "restored_state": state["supervisor_state"]}
        self._restore_suspended(state, followup, status="ABANDONED", reason=f"returned to the decision by {actor}")
        self.store.write_state(state)
        self._log(state, "followup_abandoned", followup_turn_id=followup["followup_turn_id"], actor=actor)
        label = self.followup_decision_label(followup.get("decision"))
        return {"ok": True, "message": f"returned to the {label}; it is unchanged and no answer was registered", "restored_state": state["supervisor_state"]}

    def _abandon_followup_for_revision(self, state: dict[str, Any], actor: str) -> None:
        """A revision or guidance request while a follow-up failed: the follow-up is abandoned first so
        the revision applies to the restored decision. An in-flight follow-up (worker active) is refused."""
        followup = self.followup_unresolved(state)
        if followup is None:
            return
        if followup["status"] != "FAILED" or pid_alive(state.get("worker_pid")):
            raise SupervisorError("a follow-up question is in flight; wait for its answer or return to the decision first")
        self._restore_suspended(state, followup, status="ABANDONED", reason=f"superseded by a revision request from {actor}")
        self._log(state, "followup_abandoned", followup_turn_id=followup["followup_turn_id"], actor=actor, cause="revision")

    def build_followup_prompt(self, state: dict[str, Any], turn_id: str) -> str:
        """Purpose-built, read-only prompt: the question, the exact response destination, the binding, and a
        compact result contract. It never contains the task body or a parseable frame (placeholders keep
        angle brackets), and it authorizes no code or artifact change."""
        followup = state["agent_followup"]
        decision = followup["decision"]
        label = self.followup_decision_label(decision)
        return (
            f"Supervised run {state['run_id']}: FOLLOW-UP QUESTION from the operator (read-only). The workflow is paused at the {label} "
            f"({decision['decision_id']}), which stays exactly as it is and returns to the operator unchanged after your answer. "
            "Do not change code, workflow artifacts, gates, approvals, or state, and do not implement anything: a fix you recommend "
            "starts only if the operator explicitly requests a revision or a new task.\n\n"
            f"Question:\n{followup['question']}\n\n"
            f"Write a short Markdown file at exactly {followup['response_path']} (its directory already exists) with exactly these four headings, "
            f"once each and in this order: {', '.join('## ' + name for name in FOLLOWUP_SECTIONS)}; each section must have text "
            "(Change needed is yes or no). Then end your response with exactly one contiguous seven-line block, "
            "each line KEY=value, no markdown:\n"
            "HERDR_FOLLOWUP=1\n"
            f"HERDR_RUN={state['run_id']}\n"
            f"HERDR_FOLLOWUP_TURN={turn_id}\n"
            f"HERDR_DECISION={decision['decision_id']}\n"
            f"HERDR_RESPONSE={followup['response_path']}\n"
            "HERDR_RESPONSE_SHA256=<sha256 hex of the exact file bytes>\n"
            "HERDR_SUMMARY=<one line under 300 characters, words_joined_with_underscores, no spaces or angle brackets>\n"
            "This is not a workflow turn: do not emit a workflow routing block."
        )

    def run_followup(self, state: dict[str, Any]) -> str:
        """Worker entry for an unresolved follow-up: deliver once, or resume the existing delivery after a
        restart (never a second prompt), then verify and restore. Returns 'stop'."""
        followup = self.followup_unresolved(state)
        if followup is None:
            return "continue"
        if followup["status"] == "RESPONSE_READY":
            return self._complete_followup(state)
        if followup["status"] == "FAILED":
            return "stop"  # a stable human wait; nothing automatic
        provider = followup["provider"]
        if (state.get("native_sessions") or {}).get(provider) != followup["session_id"]:
            return self._followup_failed(state, f"the {provider} native session recorded for this run changed; refusing to contact another conversation", delivery_uncertain=False)
        destination_problem = self._followup_destination_problem(followup.get("response_path"))
        if destination_problem:
            # The persisted destination is data. If it no longer proves to be below the review root it must never
            # become a write instruction for an agent: fail closed before any contact, whatever the delivery state.
            delivery = state.get("delivery")
            uncertain = isinstance(delivery, dict) and delivery.get("kind") == "followup" and delivery.get("status") in ("accepted", "uncertain")
            return self._followup_failed(state, f"persisted response destination rejected: {destination_problem}; nothing was sent", delivery_uncertain=bool(uncertain))
        try:
            self.verify_persisted_sessions(state)
            agent = self.ensure_agent(provider, allow_restore=False)
        except (SupervisorError, HerdrError) as error:
            delivery = state.get("delivery")
            return self._followup_failed(state, f"cannot locate the exact {provider} session: {error}", delivery_uncertain=isinstance(delivery, dict) and delivery.get("kind") == "followup")
        delivery = state.get("delivery")
        if isinstance(delivery, dict) and delivery.get("kind") == "followup" and delivery.get("turn_id") == followup["followup_turn_id"]:
            if delivery.get("status") == "prepared":
                # Crash between preparation and acknowledgement: the prompt may or may not have been sent.
                delivery["status"] = "uncertain"
                delivery["recovered_at"] = iso_utc(self.clock())
                followup["status"] = "WAITING"
                followup["delivery_uncertain"] = True
                self.store.write_state(state)
                self._log(state, "followup_delivery_recovered_uncertain", followup_turn_id=followup["followup_turn_id"])
            return self._monitor_followup(state)
        if followup["status"] != "PREPARED" or delivery is not None:
            return self._followup_failed(state, "the follow-up delivery record is inconsistent; nothing was resent", delivery_uncertain=True)
        if agent.get("agent_status") == "working":
            return self._followup_failed(state, f"{provider} is still working on something else; the question was not sent", delivery_uncertain=False)
        if agent.get("agent_status") not in READY_STATES:
            return self._followup_failed(state, f"{provider} is {agent.get('agent_status')}; the question was not sent", delivery_uncertain=False)
        return self._submit_followup(state)

    def _submit_followup(self, state: dict[str, Any]) -> str:
        followup = state["agent_followup"]
        provider = followup["provider"]
        name = self.agent_name(provider)
        turn_id = followup["followup_turn_id"]
        destination_problem = self._followup_destination_problem(followup.get("response_path"), require_absent=True)
        if destination_problem:
            return self._followup_failed(
                state,
                f"persisted response destination rejected before delivery: {destination_problem}; nothing was sent",
                delivery_uncertain=False,
            )
        prompt = self.build_followup_prompt(state, turn_id)
        followup["status"] = "DELIVERING"
        followup["prompt_sha256"] = sha256_bytes(prompt.encode("utf-8"))
        # One durable delivery record with the follow-up's own turn id (never a task turn id).
        state["delivery"] = {"turn_id": turn_id, "agent": provider, "kind": "followup", "prompt_sha256": followup["prompt_sha256"], "prompt_chars": len(prompt), "status": "prepared", "prepared_at": iso_utc(self.clock())}
        metrics = state["prompt_metrics"]
        metrics["prompts"] += 1
        metrics["chars"] += len(prompt)
        metrics["by_provider"][provider]["prompts"] += 1
        metrics["by_provider"][provider]["chars"] += len(prompt)
        self.store.write_state(state)
        self._log(state, "prompt_prepared", agent=provider, turn_id=turn_id, kind="followup")
        delivery = state["delivery"]
        ack = self.prompt_ack_available()
        delivery["ack_mode"] = "lifecycle" if ack else "settle"
        try:
            if ack:
                response = self.herdr.prompt_ack(name, prompt, timeout_ms=int(self.config["prompt_ack_timeout_ms"]))
                observed = self._ack_status(response) if response is not None else None
            else:
                self.herdr.prompt(name, prompt, timeout_ms=int(self.config["prompt_wait_timeout_ms"]))
                observed = None
        except HerdrError as error:
            if error.code in {"agent_blocked", "agent_not_found"}:
                state["delivery"] = None
                self._log(state, "prompt_rejected_before_delivery", agent=provider, turn_id=turn_id, error_code=error.code)
                return self._followup_failed(state, f"{name} rejected the prompt before delivery ({error.code}); the question was not sent", delivery_uncertain=False)
            delivery["status"] = "uncertain"
            delivery["error_code"] = error.code
            delivery["error"] = str(error)
            followup["status"] = "WAITING"
            followup["delivery_uncertain"] = True
            self.store.write_state(state)
            self._log(state, "prompt_delivery_uncertain", agent=provider, turn_id=turn_id, error_code=error.code)
            return self._monitor_followup(state)
        delivery["status"] = "accepted"
        delivery["accepted_at"] = iso_utc(self.clock())
        delivery["accepted_via"] = "lifecycle_ack" if ack else "settled"
        if observed is not None:
            delivery["ack_observed_status"] = observed
        followup["status"] = "WAITING"
        followup["delivery_uncertain"] = True  # from here on, an unverified outcome may still have reached the agent
        self.store.write_state(state)
        self._log(state, "prompt_accepted", agent=provider, turn_id=turn_id, ack_mode=delivery["ack_mode"], observed_status=observed, kind="followup")
        return self._monitor_followup(state)

    def _monitor_followup(self, state: dict[str, Any]) -> str:
        """Deterministic lifecycle wait for the follow-up turn: nothing is read while the agent works, no
        quota wait or reconciliation turn is ever started for a follow-up, and every non-success settles into
        the follow-up wait (never a workflow WAIT_USER, never a gate change)."""
        followup = state["agent_followup"]
        provider = followup["provider"]
        while True:
            if not self.check_control(state):
                return "stop"
            delivery = state.get("delivery")
            if not isinstance(delivery, dict) or delivery.get("kind") != "followup":
                return self._followup_failed(state, "the follow-up delivery record disappeared while waiting", delivery_uncertain=True)
            try:
                agent = self.ensure_agent(provider, allow_restore=False)
            except (SupervisorError, HerdrError) as error:
                return self._followup_failed(state, f"cannot safely inspect {self.agent_name(provider)}: {error}", delivery_uncertain=True)
            name = self.agent_name(provider)
            status = agent["agent_status"]
            if status == "working":
                if delivery["status"] == "uncertain":
                    delivery["status"] = "accepted"
                    delivery["accepted_at"] = iso_utc(self.clock())
                    self.store.write_state(state)
                try:
                    self.herdr.wait(name, timeout_ms=int(self.config["wait_timeout_ms"]))
                except HerdrError as error:
                    if error.code not in {"timeout", "agent_prompt_stalled"}:
                        return self._followup_failed(state, f"cannot wait for {name}: {error}", delivery_uncertain=True)
                continue
            if status not in READY_STATES:
                return self._followup_failed(state, f"{name} is {status}; no answer can be read and nothing was resent", delivery_uncertain=True)
            try:
                output = self.read_output(provider, settled=True)
            except HerdrError as error:
                return self._followup_failed(state, f"cannot read {name} output: {error}", delivery_uncertain=True)
            return self._settle_followup(state, output)

    def _settle_followup(self, state: dict[str, Any], output: str) -> str:
        """Parse only the exact current follow-up frame, verify the hash-bound response file, register it
        through the safe artifact path, then restore the decision. Any failure is the follow-up wait."""
        followup = state["agent_followup"]
        try:
            frame = parse_followup(output, state["run_id"], followup["followup_turn_id"])
            if frame is None:
                raise SupervisorError("no follow-up response frame for this follow-up turn in the settled transcript")
            followup["response"] = self._verify_followup_response(state, frame)
        except SupervisorError as error:
            return self._followup_failed(state, str(error), delivery_uncertain=True)
        followup["status"] = "RESPONSE_READY"
        if isinstance(state.get("delivery"), dict):
            state["delivery"]["status"] = "completed"
        self.store.write_state(state)  # the verified response is durable before the decision is restored
        return self._complete_followup(state)

    def _verify_followup_response(self, state: dict[str, Any], frame: FollowupFrame) -> dict[str, Any]:
        followup = state["agent_followup"]
        if frame.decision_id != followup["decision"]["decision_id"]:
            raise SupervisorError("the follow-up frame names a different decision than the suspended one")
        if frame.response_path != followup["response_path"]:
            raise SupervisorError("the follow-up frame names a different response file than the one requested")
        destination_problem = self._followup_destination_problem(frame.response_path)
        if destination_problem:
            raise SupervisorError(destination_problem)
        path = check_safe_file(frame.response_path, root=Path(self.config["review_root"]), max_bytes=int(self.config["max_artifact_bytes"]), label="follow-up response")
        content = path.read_bytes()
        if sha256_bytes(content) != frame.response_sha256:
            raise SupervisorError("follow-up response file does not match the hash in the frame")
        validate_followup_markdown(content, max_bytes=int(self.config["max_artifact_bytes"]))  # the four sections, once, in order, non-empty
        import herdr_artifacts as ha  # noqa: PLC0415 - companion module; imported lazily to keep the layering one-way

        existing = [record for record in ha.list_records(self.paths, state["run_id"])
                    if record.get("category") == "followup" and record.get("event_id") == followup["followup_turn_id"] and record.get("source_sha256") == frame.response_sha256]
        record = existing[-1] if existing else ha.register_file(
            self.paths, self.config, category="followup", source_path=str(path), run_id=state["run_id"], task_id=state.get("task_id"),
            event_id=followup["followup_turn_id"], title=f"Agent answer ({self.followup_decision_label(followup['decision'])})", expected_source_sha256=frame.response_sha256,
        )
        return {"artifact_id": record["artifact_id"], "sha256": frame.response_sha256, "bytes": len(content), "summary": frame.summary, "path": str(path)}

    def _complete_followup(self, state: dict[str, Any]) -> str:
        """RESPONSE_READY -> RESTORED: put the suspended decision back and emit exactly one deterministic
        AGENT_FOLLOWUP_READY event (replayable after a crash without a second prompt or artifact)."""
        followup = state["agent_followup"]
        if followup.get("status") != "RESPONSE_READY":
            raise SupervisorError("no verified follow-up response to deliver")
        self._restore_suspended(state, followup, status="RESTORED", reason=None)
        decision = followup["decision"]
        self.emit_event(state, "AGENT_FOLLOWUP_READY", {
            "followup_turn_id": followup["followup_turn_id"], "provider": followup["provider"], "question": followup["question"],
            "summary": followup["response"]["summary"], "artifact_id": followup["response"]["artifact_id"],
            "decision_kind": decision["kind"], "decision_id": decision["decision_id"], "gate_type": decision.get("gate_type"),
            "gate_id": decision["decision_id"] if decision["kind"] == "gate" else None, "restored_state": state["supervisor_state"],
            "chat_id": followup.get("chat_id"),
        })
        self.store.write_state(state)
        self._log(state, "followup_restored", followup_turn_id=followup["followup_turn_id"], restored_state=state["supervisor_state"])
        return "stop"

    def retry_followup_response(self, state: dict[str, Any], *, run_id: Any, followup_turn_id: Any, actor: str) -> dict[str, Any]:
        """One no-prompt reread of the settled transcript for a FAILED follow-up whose question may have
        reached the agent. Bound to the exact run and follow-up turn; never resends."""
        followup = self.followup_unresolved(state)
        if not isinstance(run_id, str) or run_id != state.get("run_id"):
            raise SupervisorError("retry must be bound to the current run")
        if followup is None or followup["status"] != "FAILED":
            raise SupervisorError("no unverified follow-up answer is pending for this run")
        if followup_turn_id != followup["followup_turn_id"]:
            raise SupervisorError("this retry belongs to an earlier follow-up; use the newest card")
        if not followup.get("delivery_uncertain"):
            raise SupervisorError("the question was never sent; there is no answer to read. Return to the decision or ask again")
        if int(followup.get("reread_attempts") or 0) != 0:
            raise SupervisorError("the one-time reread was already used for this follow-up; return to the decision or request a revision")
        if self.herdr is None:
            raise SupervisorError("the transcript cannot be read from this control path")
        provider = followup["provider"]
        name = self.agent_name(provider)
        try:
            agent = self.ensure_agent(provider, allow_restore=False)
        except (SupervisorError, HerdrError) as error:
            raise SupervisorError(f"cannot inspect {name} for the reread: {error}") from error
        if agent.get("agent_status") not in READY_STATES:
            raise SupervisorError(f"{name} is {agent.get('agent_status')}; the answer can only be reread once the agent is idle")
        try:
            output = self.read_output(provider, settled=True)
        except HerdrError as error:
            raise SupervisorError(f"cannot read {name} output: {error}") from error
        followup["reread_attempts"] = int(followup.get("reread_attempts") or 0) + 1
        self._log(state, "followup_reread", followup_turn_id=followup["followup_turn_id"], actor=actor)
        try:
            frame = parse_followup(output, state["run_id"], followup["followup_turn_id"])
            if frame is None:
                raise SupervisorError("still no follow-up response frame in the settled transcript")
            followup["response"] = self._verify_followup_response(state, frame)
        except SupervisorError as error:
            followup["reason"] = str(error)[:500]
            state["wait_user_reason"] = "The question may have reached the agent, but no verified answer was returned. " + str(error)
            self.store.write_state(state)
            return {"ok": False, "message": f"no verified answer yet: {error}. Nothing was resent; return to the decision or request a revision."}
        followup["status"] = "RESPONSE_READY"
        self._complete_followup(state)  # restores the decision and writes once; the answer and the event land together
        return {"ok": True, "message": f"answer verified on reread; the {self.followup_decision_label(followup['decision'])} is restored unchanged"}

    # ----- durable command inbox (Telegram bridge and CLI write here; only the worker consumes)

    def inbox_paths(self) -> dict[str, Path]:
        base = self.paths.inbox_dir
        return {name: base / name for name in ("pending", "processing", "completed")}

    def enqueue_command(self, command: dict[str, Any]) -> Path:
        request_id = command.get("request_id")
        if not isinstance(request_id, str) or not re.match(r"^[A-Za-z0-9_-]{8,128}$", request_id):
            raise SupervisorError("command request_id must be 8-128 URL-safe characters")
        if command.get("action") not in ("task", "approve", "reject", "revise", "answer", "pause", "resume", "cancel", "refresh_quota", "done", "retry_routing_result", "ask_agent", "retry_followup_response", "return_to_decision"):
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
        if action == "retry_routing_result":
            return self.retry_routing_result(state, run_id=command.get("run_id"), turn_id=command.get("turn_id"), actor=actor)
        if action == "ask_agent":
            return self.ask_agent(state, run_id=command.get("run_id"), actor=actor, question=command.get("question"), chat_id=chat_id if type(chat_id) is int else None,
                                  decision_id=command.get("decision_id"), expected_state=command.get("expected_state"),
                                  artifact_sha256=command.get("artifact_sha256"), payload_sha256=command.get("payload_sha256"))
        if action == "retry_followup_response":
            return self.retry_followup_response(state, run_id=command.get("run_id"), followup_turn_id=command.get("followup_turn_id"), actor=actor)
        if action == "return_to_decision":
            return self.return_to_decision(state, run_id=command.get("run_id"), followup_turn_id=command.get("followup_turn_id"), actor=actor)
        if action == "done":
            bound = command.get("run_id")
            if not isinstance(bound, str) or not bound:
                raise SupervisorError("done must be bound to a run id; unbound controls are refused")
            if command.get("operator_handoff") is not True:
                raise SupervisorError("done requires the explicit operator_handoff flag; verified completion comes only from the agent route")
            ready_turn = command.get("ready_turn_id")
            if ready_turn is not None and not isinstance(ready_turn, str):
                raise SupervisorError("ready_turn_id must be a string when present")
            return self.complete_operator_handoff(state, run_id=bound, actor=actor, chat_id=chat_id if type(chat_id) is int else None,
                                                  note=command.get("note") if isinstance(command.get("note"), str) else None, ready_turn_id=ready_turn)
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
            followup = self.followup_unresolved(state)
            if followup is not None and followup["status"] == "FAILED" and state["supervisor_state"] == "WAIT_USER":
                raise SupervisorError("a follow-up question has no verified answer; retry reading the answer, return to the decision, or request a revision")
            if followup is not None and followup["status"] == "FAILED" and state["supervisor_state"] in {"PAUSED", "ERROR"}:
                # Paused inside the follow-up wait: resume returns to that same wait, never to the gate behind it.
                self.store.write_control("running")
                state["supervisor_state"] = "WAIT_USER"
                self.emit_event(state, "TASK_RESUMED", {"actor": actor, "mode": "followup_wait"})
                self.store.write_state(state)
                return {"ok": True, "message": "resumed into the unresolved follow-up wait; the suspended decision is still preserved"}
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
        run_id, gate_id, expected_state = str(command.get("run_id")), str(command.get("gate_id")), command.get("expected_state")
        if action == "approve":
            return self.approve_gate(state, run_id=run_id, gate_id=gate_id, actor=actor, chat_id=chat_id, expected_state=expected_state, artifact_sha256=command.get("artifact_sha256"), payload_sha256=command.get("payload_sha256"))
        if action == "reject":
            return self.reject_gate(state, run_id=run_id, gate_id=gate_id, actor=actor, chat_id=chat_id, expected_state=expected_state, note=command.get("note"))
        if action == "revise":
            return self.revise_gate(state, run_id=run_id, gate_id=gate_id, actor=actor, chat_id=chat_id, expected_state=expected_state, note=str(command.get("note") or ""))
        if action == "answer":
            return self.answer_gate(state, run_id=run_id, gate_id=gate_id, actor=actor, chat_id=chat_id, expected_state=expected_state, answer=str(command.get("answer") or ""))
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
        """Locate a workflow agent by the identity rule (persisted provider AND exact native session);
        restore in the recorded pane; as a last resort create a non-focused workspace. Ambiguity, a
        wrong-provider carrier, or a wrong returned identity fails closed."""
        owners = self.owners()
        record = owners[provider]
        name = self.agent_name(provider)
        identity = match_exact_session(self.herdr.list_agents(), provider, record["session_id"])
        if identity.ambiguous:
            raise SupervisorError(f"{provider} native session {record['session_id'][:8]} is live in more than one pane; refusing")
        if identity.provider_conflicts:
            raise SupervisorError(f"{provider} native session {record['session_id'][:8]} is carried by a record of another provider; refusing")
        if identity.record is not None:
            agent = identity.record
            if agent.get("pane_id") != record["pane_id"]:
                raise SupervisorError(f"{provider} native session {record['session_id'][:8]} is live in pane {agent.get('pane_id')} but {record['pane_id']} is recorded; refusing (pane conflict)")
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
        if session_identity(agent) != record["session_id"] or agent.get("agent") != provider:
            raise SupervisorError(f"{name} restored with native session {session_identity(agent)!r} ({agent.get('agent')!r}), expected {record['session_id']} ({provider}); refusing")
        if pane_id != record["pane_id"]:
            self._log_owner_locator(provider, agent)
        return agent

    def _log_owner_locator(self, provider: str, agent: dict[str, Any]) -> None:
        """Update only the convenience locator (pane) of a verified exact session; the id never changes."""
        owners = self.owners()
        if session_identity(agent) != owners[provider]["session_id"]:
            raise SupervisorError("refusing to update a locator for a different native session")
        owners[provider] = {"pane_id": str(agent.get("pane_id")), "session_id": owners[provider]["session_id"]}
        atomic_write_json(self.paths.owners_file, owners, mode=0o600)
