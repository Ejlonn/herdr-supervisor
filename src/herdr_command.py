"""CLI: argument parser, terminal output, control commands, and the main dispatch."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from herdr_cli import HerdrCli
from herdr_core import (
    GATE_STATES,
    MAX_HANDOFF_NOTE_CHARS,
    PROVIDERS,
    STATE_SCHEMA_VERSION,
    TERMINAL_STATES,
    WORKFLOW_POLICIES,
    Paths,
    SupervisorError,
    WorkerLock,
    load_config,
    load_json,
    resolve_herdr_bin,
    valid_pane_id,
)
from herdr_runtime import Supervisor
from herdr_validation import StateStore, load_task_file, register_query_provider


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
        selection=report.get("session_selection") or {}
        print(f"Sessions:     {selection.get('policy','preserve')} profiles={selection.get('profiles') or {}}")
        preparation=report.get("session_preparation")
        if isinstance(preparation,dict):
            print(f"Session prep: {preparation.get('status')} id={preparation.get('preparation_id') or 'unknown'}")
    if report.get("wait_user_reason"):
        print(f"WAIT_USER:    {report['wait_user_reason']}")
    recovery = report.get("owner_recovery")
    if isinstance(recovery, dict):
        what = {"duplicate": "the same saved session is live in more than one pane", "moved": "the saved session is live in a different pane than recorded", "missing_pane": "the recorded pane is gone and the session is not live"}.get(str(recovery.get("classification")), "pane ownership needs repair")
        print(f"Owner repair: {recovery.get('provider')} session {recovery.get('session_abbrev')} — {what} (recorded {recovery.get('recorded_pane')}, live {', '.join(recovery.get('live_panes') or []) or 'none'}). No other conversation was adopted; nothing was resent.")
        print(f"              next: herdr-supervisor repair-owner-pane --run-id {report.get('task_id')} --recovery-id {recovery.get('recovery_id')}   (close duplicate panes first if any; --apply after the preview)")
    runtime = report.get("runtime_validation") or {}
    if runtime.get("gate_pending") or runtime.get("state") in ("proposed", "accepted"):
        line = f"Runtime:      {runtime.get('state')}" + (f" {runtime.get('result')}" if runtime.get("result") else "") + f" (mode {runtime.get('mode')})"
        if runtime.get("at_utc"):
            line += f" at {runtime['at_utc']}"
        if runtime.get("actor"):
            line += f" by {runtime['actor']}"
        print(line)
        if runtime.get("reason"):
            print(f"              reason: {runtime['reason']}")
        if runtime.get("waiting"):
            print(f"              waiting: {runtime['waiting']} (next: {runtime.get('next_actor')})")
    followup = report.get("agent_followup")
    if isinstance(followup, dict) and followup.get("status") in ("PREPARED", "DELIVERING", "WAITING", "RESPONSE_READY", "FAILED"):
        print(f"Follow-up:    {followup.get('status')} question to {followup.get('provider')}; the {str(followup.get('gate_type') or followup.get('decision_kind') or 'decision').replace('_', ' ')} is preserved" + (f" — {followup.get('reason')}" if followup.get("reason") else ""))
        if followup.get("status") == "FAILED":
            print("              next: `herdr-supervisor retry-answer --run-id RUN` (once, no prompt), `return-to-decision --run-id RUN`, or `revise`")
    elif report.get("followup_decision"):
        print("Ask agent:    `herdr-supervisor ask-agent --run-id RUN \"question\"` sends one read-only question and returns to this decision unchanged")
    metrics = report.get("prompt_metrics")
    if isinstance(metrics, dict):
        per = ", ".join(f"{p} {v.get('prompts', 0)}" for p, v in (metrics.get("by_provider") or {}).items())
        print(f"Prompt deliveries prepared: {metrics.get('prompts')} ({per}), {metrics.get('chars')} characters prepared by the supervisor (not provider tokens)")
    streak = report.get("consecutive_auto_turns")
    if isinstance(streak, dict) and streak.get("provider"):
        print(f"Auto turns:   {streak.get('count')} consecutive for {streak.get('provider')} (limit {report.get('auto_turn_limit')})")
    handoff_state = report.get("operator_handoff") or {}
    if handoff_state.get("available"):
        print("Operator handoff available: `herdr-supervisor done --run-id RUN --operator-handoff` closes the task without Supervisor verifying the remaining actions")
    completion = report.get("completion")
    if isinstance(completion, dict):
        print(f"Completion:   {completion.get('mode')} by {completion.get('actor')} at {completion.get('at_utc')}; NOT verified by Supervisor: {', '.join(completion.get('unmet') or [])}")
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
            f"pane={agent.get('pane_id')} session_match={agent.get('session_matches')} identity={agent.get('identity')}"
        )
    _print_quota(report)
    for error in report.get("errors", []):
        print(f"ERROR: {error}")

def print_doctor(report: dict[str, Any]) -> None:
    print(f"Doctor: {'PASS' if report['ok'] else 'FAIL'}  ({report['checked_at_local']})")
    print(f"herdr:        {report.get('herdr_bin')}  HERDR_ENV={'yes' if report['herdr_env'] else 'no'}")
    print(f"prompt ack:   {report.get('prompt_ack_mode')}  (lifecycle = acknowledged when Herdr observes the agent start; settle = bounded turn-settlement wait)")
    contract = report.get("herdr") or {}
    print(f"versions:     supervisor {report.get('supervisor_version')}; herdr {contract.get('detected_version') or 'unknown'} (minimum {contract.get('minimum_version')}); "
          f"contract {'compatible' if contract.get('compatible') else 'INCOMPATIBLE'}"
          + (f"; missing: {', '.join(contract.get('missing_capabilities') or [])}" if contract.get('missing_capabilities') else ""))
    print(f"config:       {report['config_file']}")
    print(f"state dir:    {report['state_dir']}  worker_lock_held={report['worker_lock_held']}")
    print(f"project root: {report['project_root']}")
    print(f"task state:   {report.get('task_state') or 'NO_TASK'}")
    runtime = report.get("runtime_validation") or {}
    if runtime.get("gate_pending") or runtime.get("state") != "none":
        print(f"runtime:      {runtime.get('state')} {runtime.get('result') or ''} mode={runtime.get('mode')} next={runtime.get('next_actor') or '-'}")
    repo = report.get("product_repo") or {}
    print(f"product repo: {repo.get('path')} valid={repo.get('valid')}" + (f" head={str(repo.get('head'))[:12]}" if repo.get("head") else f" ({repo.get('detail')})" if repo.get("detail") else ""))
    session = report.get("session_start") or {}
    if session:
        components = session.get("fresh_codex_components") or {}
        print(f"fresh codex:  {'available' if session.get('fresh_codex_available') else 'unavailable'}" + ("" if session.get("fresh_codex_available") else " (missing: " + ", ".join(n for n, ok in components.items() if not ok) + ")"))
    session_start=report.get("session_start") or {}
    print(f"fresh start:  {'available' if session_start.get('fresh_available') else 'unavailable'} enabled={session_start.get('enabled')} pane_split={session_start.get('pane_split')} agent_start={session_start.get('agent_start')} model_flags={session_start.get('model_flag')}")
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
    if state is None:  # pragma: no cover - read_state(required=True) raises instead
        raise SupervisorError("no supervised task exists")
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
    run.add_argument("--session-policy", choices=("preserve", "fresh-codex", "fresh-claude", "fresh-all"), default="preserve", help="new-task session policy (default: preserve)")
    run.add_argument("--codex-model-profile", default="default", help="configured Codex launch profile (fresh Codex only)")
    run.add_argument("--claude-model-profile", default="default", help="configured Claude launch profile (fresh Claude only)")
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
        cmd = sub.add_parser(name, help=f"submit runtime validation {name.split('-')[1].upper()} evidence for the exact candidate SHA: a PROPOSAL awaiting operator confirmation (default), or the canonical record only under an approved automatic_agent plan")
        cmd.add_argument("--run-id", required=True)
        cmd.add_argument("--candidate-sha", required=True)
        cmd.add_argument("--environment", required=True)
        cmd.add_argument("--evidence-file", required=True)
        cmd.add_argument("--actor", default="cli")
    ask_agent = sub.add_parser("ask-agent", help="send one read-only follow-up question to the active agent while the pending gate or unread-result wait is preserved and restored unchanged")
    ask_agent.add_argument("--run-id", required=True)
    ask_agent.add_argument("--actor", default="cli")
    ask_agent.add_argument("question")
    retry_answer = sub.add_parser("retry-answer", help="reread the settled transcript once for an unverified follow-up answer (no prompt, no wake-up)")
    retry_answer.add_argument("--run-id", required=True)
    retry_answer.add_argument("--actor", default="cli")
    return_dec = sub.add_parser("return-to-decision", help="abandon an unresolved follow-up and restore the suspended gate or wait unchanged")
    return_dec.add_argument("--run-id", required=True)
    return_dec.add_argument("--actor", default="cli")
    confirm = sub.add_parser("runtime-confirm", help="operator confirmation of the proposed runtime evidence (gate-bound, non-interactive): records the proposed PASS/FAIL canonically")
    confirm.add_argument("--run-id", required=True)
    confirm.add_argument("--gate-id", required=True)
    confirm.add_argument("--evidence-sha256", required=True, help="exact hash of the proposed evidence file (from status or the proposal card)")
    confirm.add_argument("--decision", choices=("PASS", "FAIL"), required=True)
    confirm.add_argument("--actor", default="operator-cli")
    done = sub.add_parser("done", help="close a handoff-ready task as an operator handoff (records that remaining actions are yours and unverified)")
    done.add_argument("--run-id", required=True)
    done.add_argument("--operator-handoff", action="store_true", help="required: acknowledge that Supervisor will not verify runtime/push actions you perform")
    done.add_argument("--note", default=None, help=f"optional bounded note (max {MAX_HANDOFF_NOTE_CHARS} chars)")
    done.add_argument("--actor", default="cli")
    sub.add_parser("worker", help="detached worker entry point: consume the command inbox, reconcile, continue safe states")
    refresh = sub.add_parser("refresh-quota", help="run-bound, rate-limited request for the waiting worker to recheck quota (non-LLM; never resends)")
    refresh.add_argument("--run-id", required=True)
    register_query = sub.add_parser("register-query", help="bind an existing idle read-only query agent to its exact native session")
    register_query.add_argument("--provider", choices=PROVIDERS, required=True)
    register_query.add_argument("--agent-name", required=True)
    register_query.add_argument("--acknowledge-read-only-contract", action="store_true")
    rebind = sub.add_parser("rebind-sessions", help="preview or atomically bind explicitly selected external native sessions (terminal runs only)")
    rebind.add_argument("--codex-pane")
    rebind.add_argument("--claude-pane")
    rebind.add_argument("--apply", action="store_true", help="apply the fully validated preview")
    repair = sub.add_parser("repair-owner-pane", help="nonterminal exact-session pane-locator repair: preview the eligible pane from live evidence, then --apply to record it (session id never changes)")
    repair.add_argument("--run-id", required=True)
    repair.add_argument("--recovery-id", default=None, help="recovery id from status (required with --apply)")
    repair.add_argument("--pane", default=None, help="the previewed live pane (required with --apply)")
    repair.add_argument("--apply", action="store_true")
    repair.add_argument("--actor", default="operator-cli")
    abandon = sub.add_parser("abandon-session-start", help="mark a failed pre-binding session preparation inert; created panes/sessions remain alive")
    abandon.add_argument("--preparation-id", required=True)
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
            if args.session_policy != "preserve" or args.codex_model_profile != "default" or args.claude_model_profile != "default":
                kwargs["session_selection"] = {"policy": args.session_policy, "profiles": {"codex": args.codex_model_profile, "claude": args.claude_model_profile}}
            if authorization is not None:
                kwargs["codex_reset_authorization"] = authorization
            return supervisor.run_new(task, args.start, **kwargs)
        if args.command == "rebind-sessions":
            panes = {provider: pane for provider, pane in (("codex", args.codex_pane), ("claude", args.claude_pane)) if pane}
            result = supervisor.rebind_sessions(panes, apply=args.apply)
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0
        if args.command == "repair-owner-pane":
            if not args.apply:
                with supervisor.store.transaction():
                    state = supervisor.store.read_state()
                    preview = supervisor.owner_recovery_preview(state, run_id=args.run_id, recovery_id=args.recovery_id)
                print(json.dumps(preview, indent=2, sort_keys=True))
                if preview["eligible"]:
                    print(f"next: herdr-supervisor repair-owner-pane --run-id {args.run_id} --recovery-id {preview['recovery_id']} --pane {preview['new_pane']} --apply")
                return 0
            if not args.recovery_id or not args.pane:
                raise SupervisorError("--apply requires --recovery-id and --pane from the preview")
            if not valid_pane_id(args.pane):
                raise SupervisorError("--pane must be a canonical Herdr pane id (w<n>:p<n>) taken from the preview")
            with WorkerLock(supervisor.paths.lock_file):
                with supervisor.store.transaction():
                    state = supervisor.store.read_state()
                    result = supervisor.owner_recovery_apply(state, run_id=args.run_id, recovery_id=args.recovery_id, pane=args.pane, actor=args.actor)
            print(result["message"])
            return 0
        if args.command == "abandon-session-start":
            print(json.dumps(supervisor.abandon_session_preparation(args.preparation_id),indent=2,sort_keys=True))
            return 0
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
        if args.command == "ask-agent":
            with supervisor.store.transaction():
                state = supervisor.store.read_state()
                result = supervisor.ask_agent(state, run_id=args.run_id, actor=args.actor, question=args.question)
            print(result["message"])
            print("next: herdr-supervisor resume   (or let the detached worker deliver the question)")
            return 0
        if args.command in ("retry-answer", "return-to-decision"):
            with supervisor.store.transaction():
                state = supervisor.store.read_state()
                if WorkerLock.is_held(supervisor.paths.lock_file):
                    raise SupervisorError("a worker owns the task; wait for it to settle before acting on the follow-up")
                followup = supervisor.followup_unresolved(state)
                turn = followup.get("followup_turn_id") if followup else None
                if args.command == "retry-answer":
                    result = supervisor.retry_followup_response(state, run_id=args.run_id, followup_turn_id=turn, actor=args.actor)
                else:
                    result = supervisor.return_to_decision(state, run_id=args.run_id, followup_turn_id=turn, actor=args.actor)
            print(result["message"])
            return 0 if result.get("ok") else 1
        if args.command == "done":
            if not args.operator_handoff:
                raise SupervisorError("done requires --operator-handoff; Supervisor never marks a task complete on its own authority")
            with supervisor.store.transaction():
                state = supervisor.store.read_state()
                result = supervisor.complete_operator_handoff(state, run_id=args.run_id, actor=args.actor, note=args.note)
            print(result["message"])
            return 0
        if args.command in ("runtime-pass", "runtime-fail"):
            with supervisor.store.transaction():
                state = supervisor.store.read_state()
                result = supervisor.record_runtime_evidence(state, run_id=args.run_id, candidate_sha=args.candidate_sha, environment=args.environment, evidence_file=args.evidence_file, result="PASS" if args.command == "runtime-pass" else "FAIL", actor=args.actor)
            print(result["message"])
            if result.get("proposed"):
                print(f"next: herdr-supervisor runtime-confirm --run-id {args.run_id} --gate-id {result['gate_id']} --evidence-sha256 {result.get('evidence_sha256')} --decision {'PASS' if args.command == 'runtime-pass' else 'FAIL'}   (operator only)")
            return 0
        if args.command == "runtime-confirm":
            with supervisor.store.transaction():
                state = supervisor.store.read_state()
                result = supervisor.confirm_runtime_evidence(state, run_id=args.run_id, gate_id=args.gate_id, evidence_sha256=args.evidence_sha256, decision=args.decision, actor=args.actor)
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
