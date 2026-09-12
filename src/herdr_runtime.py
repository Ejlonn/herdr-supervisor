"""The supervisor engine: native-session identity, delivery lifecycle, quota/reset reconciliation, the run/resume/monitor loop, status and doctor data."""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, Callable

import herdr_codex_reset as hcr
from herdr_cli import MIN_HERDR_VERSION_TEXT, match_exact_session, session_identity
from herdr_core import (
    _UUID_RE,
    EVENT_NAMESPACE,
    GATE_STATES,
    HUMAN_WAIT_STATES,
    LIFECYCLE_STATES,
    PROVIDERS,
    READY_STATES,
    SCHEMA_VERSION,
    SUPERVISOR_VERSION,
    TERMINAL_STATES,
    WORKFLOW_POLICIES,
    HerdrError,
    Paths,
    QuotaError,
    SupervisorError,
    WorkerLock,
    atomic_write_json,
    iso_local,
    iso_utc,
    load_json,
)
from herdr_protocol import ProtocolBlock, parse_protocol
from herdr_quota import QuotaSnapshot, QuotaWindow, blocking_windows, parse_quota_snapshot, quota_as_dict, quota_resume_at
from herdr_validation import StateStore, git_head, new_v2_fields, query_owner_summary, safe_gate_view
from herdr_workflow import SupervisorV2Mixin


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

class MissingAgentError(SupervisorError):
    """The persisted native session has no live record at all (restorable only under allow_restore)."""


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
        before_marker: tuple[int | None, float | None]
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
                marker: tuple[int | None, float | None]
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
                identity = match_exact_session(live_agents, provider, expected)
                if identity.ambiguous:
                    raise SupervisorError(f"{name} native session is present in more than one pane; refusing")
                if identity.provider_conflicts:
                    raise SupervisorError(f"{name} recorded native session is carried by a record of another provider; refusing")
                agent = identity.record
                if agent is None:
                    recorded_pane: str = existing[provider]["pane_id"]
                    transient = [
                        item for item in live_agents
                        if item.get("pane_id") == recorded_pane and item.get("agent") == provider
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
                if agent.get("agent") != provider:
                    raise SupervisorError(f"{name} is a {agent.get('agent')!r} agent, expected {provider}; refusing to seed it")
            live_session = session_identity(agent)
            pane_id = agent.get("pane_id")
            if not live_session or not isinstance(pane_id, str):
                raise SupervisorError(f"{name} has no native session identity or pane; refusing to supervise it")
            if existing is not None:
                if existing[provider]["session_id"] != live_session:
                    raise SupervisorError(
                        f"{name} native session {live_session} differs from recorded {existing[provider]['session_id']}; "
                        "refusing to silently adopt a different conversation"
                    )
                if existing[provider]["pane_id"] != pane_id:
                    raise SupervisorError(f"{name} pane {pane_id} differs from recorded {existing[provider]['pane_id']}")
            # Herdr accepts either an agent alias or a pane id as TARGET. During an active native session
            # the convenience alias can be absent even though the exact persisted session is healthy.
            # Retain a usable live target so later get/read/wait/prompt calls do not fall back to that
            # missing alias after ownership verification succeeded.
            self._live_names[provider] = agent.get("name") or pane_id
            resolved[provider] = {"pane_id": pane_id, "session_id": live_session}
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

    def resolve_target(self, provider: str, *, live_agents: list[dict[str, Any]] | None = None) -> tuple[str, dict[str, Any]]:
        """Identity-safe command target for every get/read/wait/prompt/retry/reconciliation call.

        The configured alias is used only while it names the persisted native session. When the alias is
        absent (Herdr keeps the conversation but drops the display name after logout/login) the target is
        the pane of the single live record carrying the persisted provider AND the exact persisted native
        session id, in the recorded pane. Provider kind, pane, title, recency, or being the only agent never
        authorize a target; zero, multiple, identity-less, changed-identity, or different-pane records fail
        closed before any cache, owner, or command change. Nothing is renamed, adopted, or replaced here.
        """
        record = self.owners()[provider]
        expected, recorded_pane = record["session_id"], record.get("pane_id")
        alias = self.config["agents"][provider]["name"]
        agents = live_agents if live_agents is not None else self.herdr.list_agents()
        identity = match_exact_session(agents, provider, expected)  # the one identity rule
        if identity.ambiguous:
            raise SupervisorError(f"{provider} native session {expected[:8]} is live in more than one pane; refusing")
        if identity.provider_conflicts:
            raise SupervisorError(f"the live record for native session {expected[:8]} is not a {provider} agent; refusing")
        by_alias = [agent for agent in agents if agent.get("name") == alias]
        if identity.record is None:
            if len(by_alias) == 1 and session_identity(by_alias[0]) not in (None, expected):
                raise SupervisorError(f"{alias} native session {session_identity(by_alias[0])} differs from recorded {expected}; refusing to silently adopt a different conversation")
            raise MissingAgentError(f"required live agent is missing: {alias} (recorded native session {expected[:8]} was not detected; refusing to adopt another conversation)")
        agent = identity.record
        pane = agent.get("pane_id")
        if not isinstance(pane, str) or not pane:
            raise SupervisorError(f"{provider} native session {expected[:8]} has no pane; refusing to target it")
        if isinstance(recorded_pane, str) and recorded_pane and pane != recorded_pane:
            # Pane conflict: the exact session reports a pane other than the persisted locator (with or
            # without its alias). The only approved automatic acceptance is alias loss in the SAME pane;
            # anything else fails closed before the live-target cache or owners.json changes and before
            # any command is issued.
            raise SupervisorError(f"{provider} native session {expected[:8]} reports pane {pane} but {recorded_pane} is recorded; refusing (pane conflict)")
        if agent.get("name") == alias:
            # alias fast path: only after the same rule proved the alias record is the unique exact match in the recorded pane
            self._live_names[provider] = alias
            return alias, agent
        self._live_names[provider] = pane
        return pane, agent

    def ensure_agent(self, provider: str, *, allow_restore: bool = True) -> dict[str, Any]:
        """Return the live agent, verified against the recorded native session id, using the identity-safe
        target. A missing session is restored in its recorded pane only under the explicit restore policy."""
        record = self.owners()[provider]
        name = self.agent_name(provider)
        try:
            _target, agent = self.resolve_target(provider)
        except MissingAgentError:
            if not allow_restore:
                raise
            agent = self.recover_agent(provider)
            self._live_names[provider] = agent.get("name") or record["pane_id"]
        identity = session_identity(agent)
        if identity != record["session_id"]:
            raise SupervisorError(f"{name} native session is {identity!r}, expected {record['session_id']}; refusing to continue")
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
        state["operator_handoff_ready"] = None  # any new agent turn supersedes a recorded completion claim
        state["missing_result"] = None
        state["delivery"] = {
            "turn_id": turn_id,
            "agent": state["active_agent"],
            "kind": kind,
            "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            "prompt_chars": len(prompt),
            "status": "prepared",
            "prepared_at": iso_utc(self.clock()),
        }
        # Exactly-once accounting: it lives in the same durable write as the delivery record, so a
        # restart that reuses this record never counts the prompt again.
        metrics = state.setdefault("prompt_metrics", new_v2_fields("v1")["prompt_metrics"])
        metrics["prompts"] += 1
        metrics["chars"] += len(prompt)
        metrics["by_provider"][state["active_agent"]]["prompts"] += 1
        metrics["by_provider"][state["active_agent"]]["chars"] += len(prompt)
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
        ack = self.prompt_ack_available()
        delivery["ack_mode"] = "lifecycle" if ack else "settle"
        prepared_unix = self.clock()
        try:
            if ack:
                response = self.herdr.prompt_ack(name, prompt, timeout_ms=int(self.config["prompt_ack_timeout_ms"]))
                observed = (self._ack_status(response) if response is not None else None)
            else:
                self.herdr.prompt(name, prompt, timeout_ms=int(self.config["prompt_wait_timeout_ms"]))
                observed = None
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
        delivery["accepted_via"] = "lifecycle_ack" if ack else "settled"
        if observed is not None:
            delivery["ack_observed_status"] = observed
        self.store.write_state(state)
        self._log(state, "prompt_accepted", agent=provider, turn_id=turn_id, ack_mode=delivery["ack_mode"],
                  latency_ms=int(round((self.clock() - prepared_unix) * 1000)), observed_status=observed)
        return self.monitor(state)

    def prompt_ack_available(self) -> bool:
        """Lifecycle acknowledgement is used only when the adapter implements it AND the installed CLI
        advertises it; otherwise the bounded settlement wait remains the (slower, equally safe) path."""
        supported = getattr(self.herdr, "prompt_ack_supported", None)
        if not callable(supported) or not callable(getattr(self.herdr, "prompt_ack", None)):
            return False
        try:
            return bool(supported())
        except (HerdrError, SupervisorError):
            return False

    @staticmethod
    def _ack_status(response: Any) -> str | None:
        try:
            status = response["result"]["agent"]["agent_status"]
        except (KeyError, TypeError):
            return None
        return status if isinstance(status, str) else None

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
            delivery = state.get("delivery")
            if not isinstance(delivery, dict):
                return self.set_wait_user(state, "delivery record disappeared while monitoring; inspect state.json")
            try:
                agent = self.ensure_agent(provider)
            except (SupervisorError, HerdrError) as error:
                return self.set_wait_user(state, f"cannot safely inspect {self.agent_name(provider)}: {error}")
            name = self.agent_name(provider)  # the verified target resolved by ensure_agent, never a stale alias
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
            if gated_v2:
                state["missing_result"] = {"schema_version": 1, "run_id": state["run_id"], "turn_id": delivery["turn_id"], "agent": provider,
                                           "attempts": 0, "created_at_unix": self.clock()}
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
        delivery = state.get("delivery")
        try:
            agent = self.ensure_agent(provider)
        except (SupervisorError, HerdrError) as error:
            return self.set_wait_user(state, f"cannot inspect {self.agent_name(provider)} after the quota wait: {error}")
        name = self.agent_name(provider)  # verified target (alias or pane) resolved by ensure_agent
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
        followup = self.followup_unresolved(state)
        if followup is not None and state["supervisor_state"] not in TERMINAL_STATES:
            if followup["status"] == "FAILED":
                # A stable follow-up wait: the suspended decision stays suspended until the human acts.
                state["supervisor_state"] = "WAIT_USER" if state["supervisor_state"] in {"PAUSED", "ERROR", "WAIT_USER"} else state["supervisor_state"]
                state["worker_pid"] = None
                self.store.write_state(state)
                return self._exit_code(state)
            self.store.write_state(state)
            self.run_followup(state)
            state["worker_pid"] = None
            self.store.write_state(state)
            return self._exit_code(state)
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
                outcome: str | None
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
            identity = match_exact_session(live_agents, provider, expected)
            if identity.unique:
                return identity.record, False, False
            if identity.ambiguous or identity.provider_conflicts:
                return None, True, False  # duplicates or a wrong-provider carrier: never a usable, never a healthy match
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
        alias = self.config["agents"][provider]["name"]
        matches = bool(identity and expected and identity == expected and live is not None and live.get("agent") == provider)
        alias_present = bool(live and live.get("name") == alias)
        recorded_pane = owners.get(provider, {}).get("pane_id") if isinstance(owners, dict) else None
        pane_conflict = bool(matches and live is not None and isinstance(recorded_pane, str) and recorded_pane and live.get("pane_id") != recorded_pane)
        if ambiguous:
            classification = "ambiguous"
        elif live is None:
            classification = "missing"
        elif identity_unavailable:
            classification = "identity_unavailable"
        elif pane_conflict:
            classification = "pane_conflict"  # exact session, but not in the recorded pane: never a target
        elif matches and alias_present:
            classification = "exact_session_with_alias"
        elif matches:
            classification = "exact_session_without_alias"  # usable through the pane target; not a missing session
        else:
            classification = "identity_mismatch"
        return {
            "name": name,
            "live_name": live.get("name") if live else None,
            "alias_present": alias_present,
            "identity": classification,
            "target": (alias if alias_present else live.get("pane_id")) if matches and live and not pane_conflict else None,
            "detected": live is not None,
            "ambiguous": ambiguous,
            "identity_unavailable": identity_unavailable,
            "lifecycle": live.get("agent_status") if live else None,
            "pane_id": live.get("pane_id") if live else None,
            "native_session_id": identity,
            "recorded_session_id": expected,
            "recorded_pane": recorded_pane,
            "session_matches": matches,
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
            elif entry["identity"] == "pane_conflict":
                report["errors"].append(f"{entry['name']} exact native session is in pane {entry['pane_id']} but owners.json records {report.get('agents', {}).get(provider, {}).get('recorded_pane') or 'another pane'}; refusing to target it")
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
        report["supervisor_version"] = SUPERVISOR_VERSION
        report["herdr"] = self.herdr_contract()
        contract = report["herdr"]
        if contract is None:
            report["warnings"].append("herdr version/capability probe is unavailable on this adapter")
        elif not contract["compatible"]:
            problems = [contract["version_detail"]] if not contract["version_ok"] else []
            problems += [f"missing required capability: {name}" for name in contract["missing_capabilities"]]
            report["errors"].append("herdr contract incompatible: " + "; ".join(problems))
        elif contract["prompt_ack_mode"] != "lifecycle":
            report["warnings"].append("herdr lacks the optional prompt lifecycle acknowledgement; the bounded settlement fallback is used")
        report["prompt_ack_mode"] = "lifecycle" if self.prompt_ack_available() else "settle"
        report["ok"] = not report["errors"]
        return report

    def herdr_contract(self) -> dict[str, Any] | None:
        """Read-only, cached version/capability contract of the installed Herdr CLI (None without a probe)."""
        probe = getattr(self.herdr, "capability_report", None)
        if not callable(probe):
            return None
        try:
            return probe()
        except (HerdrError, SupervisorError) as error:
            return {"detected_version": None, "minimum_version": MIN_HERDR_VERSION_TEXT, "version_ok": False, "version_detail": f"probe failed: {error}",
                    "required": {}, "missing_capabilities": ["probe failed"], "optional": {}, "compatible": False, "prompt_ack_mode": "settle"}

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
            "operator_handoff_ready": state.get("operator_handoff_ready") if state else None,
            "completion": state.get("completion") if state else None,
            "prompt_metrics": state.get("prompt_metrics") if state else None,
            "consecutive_auto_turns": state.get("consecutive_auto_turns") if state else None,
            "auto_turn_limit": int(self.config["max_consecutive_auto_turns"]),
            "missing_result": state.get("missing_result") if state else None,
            "agent_followup": self.followup_view(state.get("agent_followup")) if state else None,
            "followup_decision": self.followup_decision(state) if state else None,
            "operator_handoff": {"available": False, "reason": "no supervised task exists"},
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
        available, reason = self.operator_handoff_eligibility(state, live_agents=live_agents)
        report["operator_handoff"] = {"available": available, "reason": reason}
        return report

def backup_doctor_summary(paths: Paths, now: float) -> dict[str, Any]:
    """Optional backup health for doctor/status; disabled installations report 'disabled' and never error."""
    try:
        import herdr_backup  # noqa: PLC0415

        config = herdr_backup.load_backup_config(herdr_backup.backup_config_file(paths))
        return herdr_backup.health(config, herdr_backup.load_backup_state(paths), now)
    except (SupervisorError, OSError, ValueError, ImportError) as error:
        return {"status": "failed", "failure_reason": f"backup configuration unreadable: {error}"}
