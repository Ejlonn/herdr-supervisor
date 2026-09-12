"""Telegram presentation layer: typed, reusable renderers with centralized HTML escaping and redaction.

Every normal message answers, in order where relevant: what happened, whether the task is safe, whether the
human must act, what is blocked, what happens next, which evidence matters. Renderers are pure functions of
typed data (no I/O); the bridge turns keyboard specs into opaque one-time tokens. Short text uses Telegram
HTML (`<b>`, `<code>`); long bodies are handed back as Markdown for document delivery.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import html
import json
import zoneinfo
from typing import Any

from herdr_redaction import CHUNK, CONTROL_RE, has_sensitive_remainder, redact  # noqa: F401  (single redaction policy; re-exported for existing callers)

DEFAULT_TIMEZONE = "UTC"
LONG_MESSAGE_CHARS = 2500  # above this, output becomes summary + Markdown document

def fmt_local(epoch: Any, timezone: str = DEFAULT_TIMEZONE) -> str:
    try:
        number = float(epoch)
    except (TypeError, ValueError):
        return "unknown"
    if number != number or number <= 0 or number > 253402300799.0:
        return "unknown"
    try:
        zone = zoneinfo.ZoneInfo(timezone)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        zone = zoneinfo.ZoneInfo(DEFAULT_TIMEZONE)
    return dt.datetime.fromtimestamp(number, dt.timezone.utc).astimezone(zone).strftime("%Y-%m-%d %H:%M %Z")


def fmt_iso_local(iso: Any, timezone: str = DEFAULT_TIMEZONE) -> str:
    if not isinstance(iso, str):
        return "unknown"
    try:
        moment = dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return "unknown"
    return fmt_local(moment.timestamp(), timezone)


def chunks(text: str) -> list[str]:
    out: list[str] = []
    while len(text) > CHUNK:
        cut = text.rfind("\n", 0, CHUNK)
        cut = cut if cut > CHUNK // 2 else CHUNK
        out.append(text[:cut])
        text = text[cut:].lstrip("\n")
    out.append(text)
    return out


def esc(value: Any, *, limit: int = 600) -> str:
    """Redact, bound, then HTML-escape. The single escaping point for Telegram HTML messages."""
    return html.escape(redact(value, limit=limit), quote=False)


def b(value: Any, *, limit: int = 200) -> str:
    return f"<b>{esc(value, limit=limit)}</b>"


def code(value: Any, *, limit: int = 120) -> str:
    return f"<code>{esc(value, limit=limit)}</code>"


def abbrev(value: Any, n: int = 8) -> str:
    text = str(value or "")
    return text[:n] if text else "?"


def title_line(text: str) -> str:
    return b(text, limit=120)


@dataclasses.dataclass
class Rendered:
    """One Telegram message: HTML text, optional keyboard rows of (label, action, extra), optional long
    Markdown body to be delivered as a document by the bridge (summary stays in `html`)."""

    html: str
    keyboard: list[list[tuple[str, str, dict[str, Any]]]] = dataclasses.field(default_factory=list)
    document_markdown: str | None = None
    document_name: str | None = None
    document_title: str | None = None


def is_long(text: str, threshold: int = LONG_MESSAGE_CHARS) -> bool:
    return len(text) > threshold or text.count("\n") > 60


# --------------------------------------------------------------------------- shared fragments


def _task_line(status: dict[str, Any]) -> str:
    task = status.get("task") or status.get("task_text") or ""
    first = task.strip().splitlines()[0] if task.strip() else "(untitled task)"
    return f"Task: {esc(first, limit=90)}"


def _gate_fields(gate: dict[str, Any]) -> dict[str, Any]:
    return gate.get("summary_fields") or {}


def _services(fields: dict[str, Any]) -> str:
    services = fields.get("affected_services") or []
    if not services:
        return "none"
    return ", ".join(esc(s, limit=40) for s in services[:8]) + (" …" if len(services) > 8 else "")


def _yes_no(value: Any) -> str:
    return "yes" if value else "no"


def _quota_windows_lines(quota: dict[str, Any], tz: str) -> list[str]:
    lines = []
    if not quota.get("ok"):
        return [f"quota unavailable ({esc(quota.get('error'), limit=80)})"]
    for window in quota.get("windows", []):
        label = "5-hour" if window["kind"] == "five_hour" else "weekly"
        flag = " (BLOCKING)" if window.get("blocking") else ""
        lines.append(f"{label}: {window['remaining_percent']:.0f}% left, resets {fmt_local(window['resets_at'], tz)}{flag}")
    return lines


# --------------------------------------------------------------------------- renderers


def render_task_started(event: dict[str, Any], tz: str) -> Rendered:
    data = event.get("data") or {}
    return Rendered("\n".join([
        title_line("Task started"),
        f"Task: {esc(data.get('task'), limit=90)}",
        f"Planner: {esc((data.get('start') or 'codex').title())}",
        "",
        "Codex will plan first. Nothing is implemented before you approve the plan.",
        f"Run {code(abbrev(event.get('run_id')))}",
    ]), keyboard=[[("Status", "status", {})]])


def render_status(status: dict[str, Any], tz: str) -> Rendered:
    state = status.get("supervisor_state") or "NO_TASK"
    completion = status.get("completion") if isinstance(status.get("completion"), dict) else None
    title = {"NO_TASK": "No active task", "RUNNING": "Task in progress", "WAIT_QUOTA": "Waiting for quota", "WAIT_USER": "Your input needed", "WAIT_PLAN_APPROVAL": "Plan awaiting your approval", "WAIT_RUNTIME_VALIDATION": "Runtime validation required", "WAIT_PUSH_APPROVAL": "Push approval required", "PAUSED": "Task paused", "DONE": "Task complete", "CANCELLED": "Task cancelled", "ERROR": "Supervisor error"}.get(state, state)
    if state == "DONE" and completion and completion.get("mode") == "operator_handoff":
        title = "Task closed by operator handoff"
    open_followup = status.get("agent_followup") if isinstance(status.get("agent_followup"), dict) else None
    if state == "WAIT_USER":
        if open_followup and open_followup.get("status") == "FAILED":
            title = "Answer not verified"
        else:
            title = "Task stopped: your decision is needed" if status.get("wait_user_requires_action") else "Task paused safely"
    elif open_followup and open_followup.get("status") in FOLLOWUP_OPEN and state == "RUNNING":
        title = "Question with the agent"
    lines = [title_line(title)]
    if completion and completion.get("mode") == "operator_handoff":
        lines.append(_handoff_unverified_line(completion.get("unmet")))
    if status.get("task_id"):
        lines.append(_task_line(status))
        lines.append(f"Stage: {esc(status.get('phase') or 'starting', limit=40)} · Active agent: {esc((status.get('active_agent') or '-').title(), limit=20)}")
    else:
        lines.append("Send /task or upload a .md/.txt task file to start.")
    if state == "WAIT_QUOTA" and isinstance(status.get("quota_wait"), dict):
        wait = status["quota_wait"]
        lines.append(f"Blocked by: {esc(wait.get('provider', '').title())} quota, resets {fmt_local(wait.get('resume_at'), tz)}")
        lines.append("Task and native session are preserved; the original prompt is not replayed.")
    if status.get("wait_user_reason"):
        lines.append(f"Needs you: {esc(status['wait_user_reason'], limit=300)}")
    followup = status.get("agent_followup")
    if isinstance(followup, dict) and followup.get("status") in FOLLOWUP_OPEN:
        lines.extend(_followup_status_lines(followup))
    elif isinstance(status.get("missing_result"), dict) and status.get("wait_user_requires_action"):
        lines.extend(_missing_result_lines(status["missing_result"]))
    metrics = status.get("prompt_metrics")
    if isinstance(metrics, dict):
        per = ", ".join(f"{p} {v.get('prompts', 0)}" for p, v in (metrics.get("by_provider") or {}).items())
        lines.append(f"Prompt deliveries prepared: {esc(metrics.get('prompts'))} ({esc(per)}), {esc(metrics.get('chars'))} characters prepared (not provider tokens)")
    streak = status.get("consecutive_auto_turns")
    if isinstance(streak, dict) and streak.get("provider"):
        lines.append(f"Automatic turns: {esc(streak.get('count'))} in a row for {esc(str(streak.get('provider')).title())} (limit {esc(status.get('auto_turn_limit'))})")
    if (status.get("operator_handoff") or {}).get("available"):
        lines.append(_handoff_offer_line(status.get("operator_handoff_ready")))
    gate = status.get("pending_gate")
    if gate and gate.get("status") == "pending":
        lines.append(f"Pending gate: {esc(gate.get('gate_type', '').replace('_', ' '))}")
    if status.get("candidate_sha"):
        evidence = status.get("runtime_evidence") or {}
        push = status.get("push_approval") or {}
        lines.append(f"Candidate {code(abbrev(status['candidate_sha'], 7))}: runtime {esc(evidence.get('result') or 'not run')}, push {'approved' if push.get('candidate_sha') == status['candidate_sha'] else 'not approved'}")
    reset=status.get("codex_reset")
    if isinstance(reset,dict):
        available=reset.get("available_at_authorization")
        lines.append(f"Codex banked resets at authorization: {esc('unknown' if available is None else available)} · authorized {esc(reset.get('authorized_for_run',0))} · used {esc(reset.get('used_this_run',0))} · remaining {esc(reset.get('budget_remaining',0))}")
    for provider in ("codex", "claude"):
        agent = (status.get("agents") or {}).get(provider) or {}
        quota = (status.get("quota") or {}).get(provider) or {}
        lifecycle = agent.get("lifecycle") or "n/a"
        session = (status.get("native_sessions_abbrev") or {}).get(provider) or abbrev(agent.get("native_session_id"))
        lines.append(f"{provider.title()}: {esc(lifecycle)} · session {code(session)}")
        for line in _quota_windows_lines(quota, tz):
            lines.append("  " + esc(line, limit=120))
    backup = status.get("backup")
    if isinstance(backup, dict) and backup.get("status") and backup["status"] != "disabled":
        lines.append(f"Backup: {esc(backup['status'])}" + (f" (last verified {fmt_iso_local(backup.get('last_verified'), tz)})" if backup.get("last_verified") else ""))
    lines.append(f"As of {fmt_local(status.get('checked_at_unix'), tz) if status.get('checked_at_unix') else esc(status.get('checked_at_local', ''))}")
    keyboard = keyboard_for_status(status)
    return Rendered("\n".join(lines), keyboard=keyboard)


def render_status_raw(status: dict[str, Any]) -> Rendered:
    """Sanitized, bounded technical view (never secrets)."""
    keys = ("supervisor_state", "task_id", "phase", "active_agent", "workflow_policy", "delivery", "pending_gate", "candidate_sha", "runtime_evidence", "push_approval", "codex_reset", "last_event", "event_sequence", "quota_wait", "deferred_anomaly", "native_sessions_abbrev", "errors")
    view = {k: status.get(k) for k in keys if status.get(k) is not None}
    text = redact(json.dumps(view, indent=1, sort_keys=True, default=str), limit=3500)
    return Rendered(f"{title_line('Raw status (sanitized)')}\n<pre>{html.escape(text, quote=False)}</pre>")


def handoff_text(value: Any) -> str:
    """Display form of a routing handoff: the exact stored value. The emitted contract joins words with
    underscores so terminal wrapping cannot lose word boundaries; an underscore may also be part of a
    literal identifier or path, so nothing is inferred and nothing is rewritten for display."""
    return "" if value is None else str(value)


def _unmet_words(unmet: Any) -> str:
    items = [str(u).replace("_", " ") for u in unmet] if isinstance(unmet, list) and unmet else ["runtime validation", "push approval"]
    return ", ".join(items)


def _handoff_unverified_line(unmet: Any) -> str:
    return f"Remaining actions were handed to the operator and are NOT verified by Supervisor: {esc(_unmet_words(unmet))}. No push approval or runtime evidence was recorded."


def _handoff_offer_line(ready: Any) -> str:
    ready = ready if isinstance(ready, dict) else {}
    return (f"The agent reports its work complete; only your own actions remain ({esc(_unmet_words(ready.get('unmet')))}). "
            "Tap Done or send /done to close the task as an operator handoff — Supervisor will not verify those actions.")


Keyboard = list[list[tuple[str, str, dict[str, Any]]]]


FOLLOWUP_OPEN = ("PREPARED", "DELIVERING", "WAITING", "RESPONSE_READY", "FAILED")


def _ask_agent_button(status: dict[str, Any]) -> tuple[str, str, dict[str, Any]] | None:
    """Offered only where the controller reports a suspendable decision; the token binds that decision."""
    decision = status.get("followup_decision")
    if not isinstance(decision, dict) or not decision.get("decision_id"):
        return None
    extra: dict[str, Any] = {"decision_id": decision["decision_id"]}
    if decision.get("kind") == "missing_result":
        extra["turn_id"] = decision["decision_id"]
    return ("Ask agent", "ask_agent", extra)


def _followup_wait_keyboard(followup: dict[str, Any]) -> Keyboard:
    turn = followup.get("followup_turn_id")
    rows: Keyboard = []
    if followup.get("delivery_uncertain") and not followup.get("reread_attempts"):
        rows.append([("Retry reading answer", "retry_followup_response", {"followup_turn_id": turn})])
    rows.append([("Return to decision", "return_to_decision", {"followup_turn_id": turn})])
    # The revision token binds the suspended decision (gate id and its wait state), not the follow-up wait,
    # because the revision applies to the restored decision once the follow-up is abandoned.
    rows.append([("Request revision", "revise", {"followup_turn_id": turn, "gate_id": followup.get("decision_id") if followup.get("decision_kind") == "gate" else "-", "expected_state": followup.get("suspended_state")}), ("Status", "status", {})])
    return rows


def _with_ask(rows: Keyboard, status: dict[str, Any]) -> Keyboard:
    button = _ask_agent_button(status)
    if button is None:
        return rows
    return [*rows, [button]] if len(rows[-1]) >= 2 else [*rows[:-1], [*rows[-1], button]]


def keyboard_for_status(status: dict[str, Any]) -> Keyboard:
    state = status.get("supervisor_state")
    followup = status.get("agent_followup")
    if isinstance(followup, dict) and followup.get("status") == "FAILED" and state == "WAIT_USER":
        return _followup_wait_keyboard(followup)
    if isinstance(followup, dict) and followup.get("status") in FOLLOWUP_OPEN and state not in ("DONE", "CANCELLED"):
        return [[("Status", "status", {}), ("Pause", "pause", {})]]
    if state in (None, "NO_TASK", "DONE", "CANCELLED"):
        return [[("Status", "status", {})]] if state in ("DONE", "CANCELLED") else []
    if state == "WAIT_PLAN_APPROVAL":
        return _with_ask([[("View Plan", "view_details", {}), ("Approve", "approve", {})], [("Request Revision", "revise", {}), ("Reject", "reject", {})]], status)
    if state == "WAIT_QUOTA":
        return [[("Status", "status", {}), ("Pause", "pause", {})], [("Refresh quota", "refresh_quota", {})]]
    if state == "WAIT_RUNTIME_VALIDATION":
        return _with_ask([[("View Report", "view_details", {}), ("Status", "status", {})]], status)
    if state == "WAIT_PUSH_APPROVAL":
        return _with_ask([[("Approve Push Stage", "approve", {})], [("Keep Waiting", "keep_waiting", {}), ("Cancel", "cancel", {})]], status)
    if state == "PAUSED":
        return [[("Resume", "resume", {}), ("Cancel", "cancel", {})]]
    if state == "WAIT_USER":
        if status.get("wait_user_requires_action"):
            rows: Keyboard = [[("Request revision", "revise", {"gate_id": "-"}), ("Cancel task", "cancel", {})], [("Status", "status", {})]]
            missing = status.get("missing_result")
            if isinstance(missing, dict) and missing.get("turn_id") and not missing.get("attempts"):
                # One-time, run/turn-bound reread of the settled transcript; never a prompt; gone once spent.
                rows.insert(0, [("Retry routing result", "retry_routing_result", {"turn_id": missing.get("turn_id")})])
            if isinstance(missing, dict) and missing.get("turn_id"):
                rows = _with_ask(rows, status)
            ready = status.get("operator_handoff_ready")
            if (status.get("operator_handoff") or {}).get("available") and isinstance(ready, dict):
                # Only the controller's predicate can offer Done; the token is bound to the exact readiness turn.
                rows.insert(0, [("Done (operator handoff)", "done", {"ready_turn_id": ready.get("turn_id")})])
            return rows
        return [[("Status", "status", {}), ("Pause", "pause", {})], [("Cancel", "cancel", {})]]
    return [[("Status", "status", {}), ("Pause", "pause", {})]]


def render_plan_approval(gate: dict[str, Any], status: dict[str, Any], tz: str) -> Rendered:
    f = _gate_fields(gate)
    risk = f.get("risk_summary") or ""
    lines = [
        title_line("Plan ready for approval"),
        f"Task: {esc(f.get('task_title'), limit=90)}",
        f"Planner: {esc((gate.get('agent') or 'codex').title())}",
        f"Risk: {esc(risk, limit=160)}" if risk else "Risk: not stated",
        f"Runtime validation: {'Required' if f.get('runtime_validation_required') else 'Not required'} · Rebuild: {_yes_no(f.get('rebuild_required'))} · Migration: {_yes_no(f.get('migration_required'))} · Push approval: {_yes_no(f.get('push_approval_required'))}",
        "",
        esc(f.get("summary"), limit=400),
        "",
        "Implementation has not started. Approving lets Codex write the brief and hand implementation to Claude.",
        f"Plan fingerprint {code(abbrev(gate.get('artifact_sha256'), 12))} · expires {fmt_local(gate.get('expires_at_unix'), tz)}",
    ]
    return Rendered("\n".join(lines), keyboard=[[("View Plan", "view_details", {}), ("Approve", "approve", {})], [("Request Revision", "revise", {}), ("Reject", "reject", {})], [("Ask agent", "ask_agent", {"decision_id": gate.get("gate_id")})]])


def render_revision_requested(event: dict[str, Any], tz: str) -> Rendered:
    data = event.get("data") or {}
    return Rendered("\n".join([
        title_line("Revision requested"),
        f"The {esc((data.get('gate_type') or 'gate').replace('_', ' '))} goes back to {esc((data.get('agent') or 'the planner').title())} with your note.",
        "No approval is in effect; a new gate will arrive when the revision is ready.",
    ]), keyboard=[[("Status", "status", {})]])


def render_question(gate: dict[str, Any], tz: str) -> Rendered:
    f = _gate_fields(gate)
    lines = [title_line("Question from the agent"), f"Task: {esc(f.get('task_title'), limit=90)}", "", esc(f.get("question"), limit=800)]
    if f.get("context"):
        lines += ["", esc(f.get("context"), limit=400)]
    keyboard: list[list[tuple[str, str, dict[str, Any]]]] = []
    if f.get("answer_mode") == "choice":
        keyboard = [[(str(choice)[:40], "answer", {"choice": choice})] for choice in (f.get("choices") or [])[:4]]
        lines.append("")
        lines.append("Pick one below. The task waits until you answer.")
    else:
        lines += ["", f"Reply with /answer &lt;text&gt; (max {esc(f.get('max_answer_chars'))} chars). The task waits until you answer."]
    keyboard.append([("Request Revision", "revise", {}), ("Ask agent", "ask_agent", {"decision_id": gate.get("gate_id")})])
    return Rendered("\n".join(lines), keyboard=keyboard)


def _missing_result_lines(missing: dict[str, Any]) -> list[str]:
    """Plain outcome first; every offered action explained; no turn ids or delivery internals here."""
    lines = [
        "The agent finished, but Supervisor did not receive the required workflow result. No approval or next step was created, and nothing was resent.",
        "",
    ]
    if missing.get("attempts"):
        lines.append("• The one-time reread of that turn was already used and found no result.")
    else:
        lines.append("• Retry routing result: reread the same completed turn once. No message is sent to the agent.")
    lines.append("• Ask agent: send one question to the agent and get its answer back here; this decision stays as it is.")
    lines.append("• Request revision: ask the agent for a replacement workflow result (a new turn).")
    lines.append("• Cancel task: end the run; the agent sessions are kept.")
    return lines


def _followup_status_lines(followup: dict[str, Any]) -> list[str]:
    provider = str(followup.get("provider") or "the agent").title()
    label = _followup_label(followup)
    status = followup.get("status")
    if status == "FAILED":
        return [f"Your question to {provider} has no verified answer yet. The {label} is still preserved and unchanged."]
    return [f"Your question is with {provider}. The {label} is preserved and returns unchanged once the answer is verified."]


def _followup_label(view: dict[str, Any]) -> str:
    if view.get("decision_kind") == "gate":
        return f"{str(view.get('gate_type') or 'gate').replace('_', ' ')} decision"
    return "unread-result recovery"


def render_followup_wait(reason: str, followup: dict[str, Any], tz: str) -> Rendered:
    """Follow-up-specific fail-closed wait. Says what is missing, that nothing changed, and what each button does."""
    provider = str(followup.get("provider") or "the agent").title()
    label = _followup_label(followup)
    lines = [title_line("Answer not verified")]
    if followup.get("delivery_uncertain"):
        lines.append(f"Your question may have reached {provider}, but no verified answer came back.")
    else:
        lines.append(f"Your question was not sent to {provider}.")
    lines += [f"The {label} is unchanged: nothing was approved, revised, or advanced.", ""]  # diagnostics live in /status raw and the logs
    if followup.get("delivery_uncertain") and not followup.get("reread_attempts"):
        lines.append("• Retry reading answer: read the agent's finished turn once more. No message is sent.")
    elif followup.get("delivery_uncertain"):
        lines.append("• The one-time reread was already used.")
    lines.append(f"• Return to decision: drop this question and show the {label} again.")
    lines.append("• Request revision: ask the agent for a replacement workflow result instead.")
    return Rendered("\n".join(lines), keyboard=_followup_wait_keyboard(followup))


def render_wait_user(reason: str, status: dict[str, Any] | None, tz: str, *, requires_action: bool = False, followup: dict[str, Any] | None = None) -> Rendered:
    status = status or {}
    open_followup = followup if isinstance(followup, dict) else status.get("agent_followup")
    if isinstance(open_followup, dict) and open_followup.get("status") == "FAILED" and requires_action:
        return render_followup_wait(reason, open_followup, tz)
    missing = status.get("missing_result") if requires_action else None
    if isinstance(missing, dict) and missing.get("turn_id"):
        lines = [title_line("Agent finished without a workflow result"), *_missing_result_lines(missing)]
        return Rendered("\n".join(lines), keyboard=keyboard_for_status({"supervisor_state": "WAIT_USER", "wait_user_requires_action": True, "missing_result": missing, "followup_decision": status.get("followup_decision")}))
    if requires_action:
        lines = [title_line("Task stopped: your decision is needed"), "Supervisor did not get a result it could act on, so the task stopped safely. No approval or next step was created, and nothing was resent.", f"Reason: {esc(reason, limit=400)}", ""]
        if (status.get("operator_handoff") or {}).get("available"):
            lines.append(_handoff_offer_line(status.get("operator_handoff_ready")))
        lines.append("• Request revision: reply with one note; the agent gets it and must produce a replacement workflow result.")
        lines.append("• Cancel task: end the run; the agent sessions are kept.")
    else:
        lines = [title_line("Task paused safely"), f"Reason: {esc(reason, limit=400)}", "", "Nothing was resent and no decision is pending. Check the pane if needed, then /resume to continue in the same sessions."]
    return Rendered("\n".join(lines), keyboard=keyboard_for_status({"supervisor_state": "WAIT_USER", "wait_user_requires_action": requires_action, "operator_handoff": status.get("operator_handoff"), "operator_handoff_ready": status.get("operator_handoff_ready")}))


def render_agent_followup_ready(event: dict[str, Any], status: dict[str, Any] | None, tz: str) -> Rendered:
    """The verified answer: who answered, that the decision is unchanged, the bounded summary, and the
    controls of the restored decision (derived from live status, never from the answer)."""
    data = event.get("data") or {}
    status = status or {}
    provider = str(data.get("provider") or "The agent").title()
    label = _followup_label(data)
    lines = [
        title_line(f"{provider} answered"),
        f"The {label} is unchanged. Nothing was approved, revised, or advanced.",
        "",
        f"Question: {esc(data.get('question'), limit=300)}",
        f"Answer summary: {esc(handoff_text(data.get('summary')), limit=320)}",
        "",
        "The full answer is attached as a document. A recommended change starts only if you request a revision or a new task.",
    ]
    return Rendered("\n".join(lines), keyboard=keyboard_for_status(status))


def render_task_handed_off(event: dict[str, Any], status: dict[str, Any] | None, tz: str) -> Rendered:
    data = event.get("data") or {}
    lines = [title_line("Task closed by operator handoff"), _task_line(status or {}) if status else "", ""]
    lines.append(f"• final stage: {esc(data.get('stage'), limit=60)}")
    if data.get("handoff"):
        lines.append(f"• {esc(handoff_text(data.get('handoff')), limit=160)}")
    if data.get("note"):
        lines.append(f"• note: {esc(str(data.get('note')), limit=200)}")
    lines += ["", _handoff_unverified_line(data.get("unmet")), "No pending human gates; the native sessions are preserved."]
    keyboard: Keyboard = [[("Open Full Report", "view_report", {}), ("Status", "status", {})]] if data.get("final_report") else [[("Status", "status", {})]]
    return Rendered("\n".join(l for l in lines if l is not None), keyboard=keyboard)


def render_quota_wait(event: dict[str, Any], status: dict[str, Any] | None, tz: str) -> Rendered:
    data = event.get("data") or {}
    provider = str(data.get("provider") or "provider").title()
    windows = data.get("windows") or []
    labels = ", ".join("5-hour" if w == "five_hour" else "weekly" for w in windows) or "quota"
    lines = [title_line(f"{provider} quota reached"), _task_line(status or {}) if status else "", f"Stage: {esc((status or {}).get('phase') or 'current', limit=40)}", "", f"{labels} quota resets at: {fmt_local(data.get('resume_at'), tz)}"]
    if data.get("deferred_anomaly"):
        lines.append("The last turn ended without a result, most likely because the quota ran out. It is not treated as complete.")
        lines.append("After the reset one reconciliation turn continues in the same session — the original prompt is not replayed.")
    else:
        lines.append("The current task and native session are preserved. No original prompt will be replayed while waiting.")
    if data.get("early_refresh_available"):
        lines.append("Quota is rechecked every few minutes without waking any agent; work resumes early if it clears.")
    else:
        lines.append(f"{provider} has no independent quota refresh: the safe reset time applies.")
    return Rendered("\n".join(line for line in lines if line is not None), keyboard=[[("Status", "status", {}), ("Pause", "pause", {})], [("Refresh quota", "refresh_quota", {})]])


def render_quota_resumed(event: dict[str, Any], tz: str) -> Rendered:
    data = event.get("data") or {}
    provider = str(data.get("provider") or "provider").title()
    lines = [title_line(f"{provider} quota available again")]
    if data.get("early"):
        lines.append(f"Quota cleared earlier than the cached reset (found on recheck #{esc(data.get('rechecks'))}). Work resumes now.")
    else:
        lines.append("The reset time passed and fresh quota evidence is usable. Work resumes now.")
    if data.get("deferred_anomaly"):
        lines.append("The interrupted turn is reconciled with one continuation in the same session; nothing is replayed.")
    else:
        lines.append("The same native session continues; nothing is replayed.")
    return Rendered("\n".join(lines), keyboard=[[("Status", "status", {})]])


def render_runtime_required(gate: dict[str, Any], tz: str) -> Rendered:
    f = _gate_fields(gate)
    lines = [
        title_line("Runtime validation required"),
        "Local implementation and review are complete.",
        f"Candidate: {code(abbrev(f.get('candidate_sha'), 7))}",
        "",
        f"Local checks: {'✓' if f.get('local_gate_result') == 'PASS' else '✗'} tests/build · {'✓' if f.get('codex_review_status') == 'APPROVED' else '✗'} review",
        "Runtime validation: NOT RUN",
        "Push: BLOCKED",
        f"Affected services: {_services(f)}",
        f"Rebuild: {_yes_no(f.get('rebuild_required'))}" + (f" ({esc(f.get('rebuild_reason'), limit=100)})" if f.get("rebuild_reason") else ""),
        "",
        "Exact-SHA runtime validation must be recorded from the operator CLI before push approval becomes available. Telegram cannot record it.",
    ]
    return Rendered("\n".join(lines), keyboard=[[("View Report", "view_details", {}), ("Status", "status", {})], [("Ask agent", "ask_agent", {"decision_id": gate.get("gate_id")})]])


def render_runtime_failed(event: dict[str, Any], tz: str) -> Rendered:
    data = event.get("data") or {}
    return Rendered("\n".join([
        title_line("Runtime validation failed"),
        f"Candidate {code(abbrev(data.get('candidate_sha'), 7))} on {esc(data.get('environment'))} did not pass.",
        "Push stays blocked. Send a revision note so the agent can fix and re-run, or cancel.",
    ]), keyboard=[[("Request Revision", "revise", {}), ("Status", "status", {})]])


def render_push_approval(gate: dict[str, Any], tz: str) -> Rendered:
    f = _gate_fields(gate)
    lines = [
        title_line("Push stage ready for approval"),
        f"Candidate: {code(abbrev(f.get('candidate_sha'), 7))}",
        f"Local checks: {'✓' if f.get('local_gate_result') == 'PASS' else '✗'} · Review: {'✓' if f.get('codex_review_status') == 'APPROVED' else '✗'} · Runtime: {'✓ PASS' if f.get('runtime_evidence_status') == 'PASS' else esc(f.get('runtime_evidence_status') or 'not required')}",
        f"Affected services: {_services(f)}",
        "",
        "Approving authorizes the push stage only; you perform the push/PR yourself. Nothing is pushed automatically.",
        f"Expires {fmt_local(gate.get('expires_at_unix'), tz)}",
    ]
    return Rendered("\n".join(lines), keyboard=[[("Approve Push Stage", "approve", {})], [("Keep Waiting", "keep_waiting", {}), ("Cancel", "cancel", {})], [("Ask agent", "ask_agent", {"decision_id": gate.get("gate_id")})]])


def render_paused(event: dict[str, Any], tz: str) -> Rendered:
    return Rendered("\n".join([title_line("Task paused"), "Nothing is running. The native sessions are preserved.", "Use /resume to continue exactly where it stopped."]), keyboard=[[("Resume", "resume", {}), ("Cancel", "cancel", {})]])


def render_cancelled(event: dict[str, Any], tz: str) -> Rendered:
    return Rendered("\n".join([title_line("Task cancelled"), "Supervision stopped. Claude/Codex conversation history is preserved.", "Send /task or upload a task file to start a new task."]), keyboard=[[("Status", "status", {})]])


def render_error(event: dict[str, Any], tz: str) -> Rendered:
    data = event.get("data") or {}
    return Rendered("\n".join([title_line("Supervisor error"), esc(data.get("error"), limit=400), "", "The task is stopped; nothing was resent. Check the pane and /resume, or /cancel."]), keyboard=[[("Status", "status", {}), ("Cancel", "cancel", {})]])


def render_task_done(event: dict[str, Any], status: dict[str, Any] | None, tz: str, *, highlights: list[str] | None = None) -> Rendered:
    data = event.get("data") or {}
    lines = [title_line("Task complete"), _task_line(status or {}) if status else "", ""]
    for item in (highlights or [f"final stage: {data.get('stage')}", handoff_text(data.get("handoff") or "")[:200]]):
        lines.append(f"• {esc(item, limit=120)}")
    lines += ["", "No pending human gates."]
    return Rendered("\n".join(l for l in lines if l is not None), keyboard=[[("Open Full Report", "view_report", {}), ("Status", "status", {})]])


def render_recovered(event: dict[str, Any], tz: str) -> Rendered:
    data = event.get("data") or {}
    return Rendered("\n".join([title_line("Supervisor restarted"), f"Recovered in state {esc((data.get('state') or '').replace('_', ' ').lower())}. Persisted turn, gate, quota and session identities were kept; nothing was replayed."]), keyboard=[[("Status", "status", {})]])


def render_command_result(event: dict[str, Any], tz: str, status: dict[str, Any] | None = None) -> Rendered:
    data = event.get("data") or {}
    ok = bool(data.get("ok"))
    verb = {"task": "Task start", "approve": "Approval", "reject": "Rejection", "revise": "Revision", "answer": "Answer", "pause": "Pause", "resume": "Resume", "cancel": "Cancel", "refresh_quota": "Quota refresh", "ask_agent": "Question to the agent", "retry_followup_response": "Answer reread", "return_to_decision": "Return to decision", "retry_routing_result": "Routing result retry"}.get(str(data.get("action")), str(data.get("action") or "Command").title())
    keyboard: Keyboard = [[("Status", "status", {})]] if not ok else []
    if ok and data.get("action") == "return_to_decision" and status is not None:
        keyboard = keyboard_for_status(status)  # the restored decision's own controls
    return Rendered("\n".join([title_line(f"{verb} {'accepted' if ok else 'not applied'}"), esc(data.get("message"), limit=400)]), keyboard=keyboard)


def render_ask_answer(event: dict[str, Any], tz: str, *, long_threshold: int = LONG_MESSAGE_CHARS) -> Rendered:
    data = event.get("data") or {}
    answer = str(data.get("answer") or "")
    provider = data.get("provider")
    header = title_line("Answer" if data.get("ok") else "Query not answered")
    note = ""
    if provider and data.get("failover"):
        reason = data.get("failover_reason") or {}
        preferred = (data.get("selection") or {}).get("preferred") or "the preferred provider"
        why = {"quota_blocked": "quota-blocked", "busy": "busy", "owns_active_turn": "working on the active task", "missing_or_ambiguous": "not available", "not_provisioned": "not provisioned"}.get(str(reason.get("reason")), "unavailable")
        until = f" until {fmt_local(reason.get('resets_at'), tz)}" if reason.get("resets_at") else ""
        note = f"\nAnswered by {esc(str(provider).title())} because the preferred {esc(str(preferred).title())} query session is {why}{until}."
    elif provider:
        note = f"\nAnswered by {esc(str(provider).title())} (read-only query session)."
    if is_long(answer, long_threshold):
        return Rendered(f"{header}\n{esc(answer[:600], limit=600)}…\n\nFull answer attached as Markdown.{note}", document_markdown=answer, document_name="answer", document_title="Query answer")
    return Rendered(f"{header}\n{esc(answer, limit=3500)}{note}")


def render_long_output_notice(title: str, summary: str, artifact_name: str) -> Rendered:
    return Rendered("\n".join([title_line(title), esc(summary, limit=1500), "", f"Full report attached: {code(artifact_name)}"]))


def render_backup_health(health: dict[str, Any], tz: str) -> Rendered:
    status = str(health.get("status") or "disabled")
    if status == "disabled":
        return Rendered("\n".join([title_line("Backups disabled"), "No backup destination is configured. Normal operation continues without backups."]))
    lines = [title_line({"healthy": "Backup healthy", "stale": "Backup stale", "failed": "Backup failed", "unverified": "Backup unverified"}.get(status, f"Backup {status}"))]
    if health.get("last_verified"):
        lines.append(f"Last verified backup: {fmt_iso_local(health['last_verified'], tz)}")
    elif health.get("last_success"):
        lines.append(f"Last successful backup: {fmt_iso_local(health['last_success'], tz)} (not yet verified)")
    else:
        lines.append("No successful backup yet.")
    lines.append(f"Strategy: {esc(health.get('strategy') or 'n/a')} · Destination: {esc(health.get('destination_class') or 'configured')}")
    if health.get("age_hours") is not None:
        lines.append(f"Age: {esc(round(float(health['age_hours']), 1))}h" + (f" (max {esc(health.get('max_age_hours'))}h)" if health.get("max_age_hours") else ""))
    if health.get("failure_reason"):
        lines.append(f"Last failure: {esc(health['failure_reason'], limit=200)}")
    if health.get("protects"):
        lines.append("Protects: " + esc(", ".join(health["protects"]), limit=200))
    if health.get("not_protected"):
        lines.append("Not protected: " + esc(", ".join(health["not_protected"]), limit=200))
    keyboard: Keyboard = [[("Retry Backup", "backup_now", {})]] if status in ("stale", "failed", "unverified") else []
    return Rendered("\n".join(lines), keyboard=keyboard)


def render_event(event: dict[str, Any], status: dict[str, Any] | None, gate: dict[str, Any] | None, tz: str, *, long_threshold: int = LONG_MESSAGE_CHARS) -> Rendered:
    """Dispatch by event type. Unknown types get a safe generic line."""
    kind = event.get("type")
    if kind == "TASK_STARTED":
        return render_task_started(event, tz)
    if kind == "PLAN_APPROVAL_REQUIRED" and gate:
        return render_plan_approval(gate, status or {}, tz)
    if kind == "QUESTION_ASKED" and gate:
        return render_question(gate, tz)
    if kind == "REVISION_REQUESTED":
        return render_revision_requested(event, tz)
    if kind == "WAIT_USER":
        data = event.get("data") or {}
        return render_wait_user(str(data.get("reason") or ""), status, tz, requires_action=bool((status or {}).get("wait_user_requires_action")), followup=data.get("followup") if isinstance(data.get("followup"), dict) else None)
    if kind == "AGENT_FOLLOWUP_READY":
        return render_agent_followup_ready(event, status, tz)
    if kind == "WAIT_QUOTA":
        return render_quota_wait(event, status, tz)
    if kind == "QUOTA_RESUMED":
        return render_quota_resumed(event, tz)
    if kind == "RUNTIME_VALIDATION_READY" and gate:
        return render_runtime_required(gate, tz)
    if kind == "RUNTIME_VALIDATION_FAILED":
        return render_runtime_failed(event, tz)
    if kind == "PUSH_APPROVAL_REQUIRED" and gate:
        return render_push_approval(gate, tz)
    if kind == "TASK_PAUSED":
        return render_paused(event, tz)
    if kind == "TASK_CANCELLED":
        return render_cancelled(event, tz)
    if kind == "TASK_ERROR":
        return render_error(event, tz)
    if kind == "TASK_DONE":
        return render_task_done(event, status, tz)
    if kind == "TASK_HANDED_OFF":
        return render_task_handed_off(event, status, tz)
    if kind == "RECOVERED_AFTER_RESTART":
        return render_recovered(event, tz)
    if kind == "COMMAND_RESULT":
        return render_command_result(event, tz, status)
    if kind == "QUERY_RESULT":
        return render_ask_answer(event, tz, long_threshold=long_threshold)
    if kind == "CODEX_RESET_AUTHORIZED":
        data=event.get("data") or {}
        return Rendered(f"{title_line('Codex reset policy recorded')}\nBanked resets available: {esc(data.get('available'))}\nAuthorized for this task: {esc(data.get('authorized'))}")
    if kind == "CODEX_RESET_STARTED":
        data=event.get("data") or {}
        return Rendered(f"{title_line('Codex usage limit reached')}\nUsing authorized banked reset {esc(data.get('sequence'))} of {esc(data.get('authorized'))}. The same task and session are preserved.")
    if kind == "CODEX_RESET_VERIFIED":
        data=event.get("data") or {}
        return Rendered(f"{title_line('Codex reset applied')}\nUsed this task: {esc(data.get('used'))} / {esc(data.get('authorized'))}\nBanked resets currently available: {esc(data.get('available'))}\nQuota recovery verified. Continuing the same Codex session.")
    if kind == "CODEX_RESET_UNAVAILABLE":
        data=event.get("data") or {}
        return Rendered(f"{title_line('Codex reset unavailable')}\n{esc(data.get('reason'))}. The task remains preserved under normal quota handling.")
    if kind in ("PLAN_APPROVED", "PUSH_APPROVED", "QUESTION_ANSWERED", "TASK_RESUMED", "RUNTIME_VALIDATION_PASSED"):
        data = event.get("data") or {}
        titles = {"PLAN_APPROVED": "Plan approved", "PUSH_APPROVED": "Push stage approved", "QUESTION_ANSWERED": "Answer recorded", "TASK_RESUMED": "Task resumed", "RUNTIME_VALIDATION_PASSED": "Runtime validation passed"}
        follow = {"PLAN_APPROVED": "Codex writes the brief; implementation follows in the same sessions.", "PUSH_APPROVED": "The workflow continues; you perform the push/PR yourself.", "QUESTION_ANSWERED": "The agent continues with your answer.", "TASK_RESUMED": "Continuing where it stopped; nothing replayed.", "RUNTIME_VALIDATION_PASSED": f"Candidate {abbrev(data.get('candidate_sha'), 7)} validated on {data.get('environment')}."}
        return Rendered("\n".join([title_line(titles[kind]), esc(follow[kind], limit=200)]))
    if kind == "BACKUP_HEALTH":
        return render_backup_health(event.get("data") or {}, tz)
    return Rendered("\n".join([title_line(str(kind or "Event").replace("_", " ").title()), esc(json.dumps(event.get("data") or {}, ensure_ascii=False, default=str), limit=400)]))


# --------------------------------------------------------------------------- final report (Markdown)


def render_final_report_markdown(fields: dict[str, Any]) -> str:
    """The twenty-item final report as a Markdown document. `fields` holds one entry per item; missing
    items are rendered as 'not available' rather than omitted."""
    items = [
        ("What was changed", "changed"), ("Files created/changed", "files"), ("Test counts/results", "tests"),
        ("WAIT_USER/WAIT_QUOTA bug root cause", "root_cause"), ("Exact implemented precedence behavior", "precedence"),
        ("Post-quota reconciliation behavior", "reconciliation"), ("Early-wake quota behavior and polling cadence", "early_wake"),
        ("Proof polling does not wake LLM agents", "no_llm_polling"), ("/ask failover behavior", "ask_failover"),
        ("Telegram UX improvements", "ux"), ("Long input-file test result", "long_input"), ("Long output-file test result", "long_output"),
        ("Real Telegram tests performed", "live_tests"), ("Live tests intentionally NOT performed", "live_not_performed"),
        ("Security/secret-scrub findings", "secret_scan"), ("Generic GitHub repository readiness", "repo_readiness"),
        ("Known limitations", "limitations"), ("Release recommendation (stable, beta, or not ready)", "release"),
        ("Exact remaining human actions", "human_actions"), ("Commit/push/publication actions still requiring approval", "approval_actions"),
    ]
    out = [f"# {redact(fields.get('title') or 'Final task report', limit=120)}", ""]
    if fields.get("run_id"):
        out.append(f"Run `{abbrev(fields.get('run_id'))}` · generated {redact(fields.get('generated_at') or '', limit=40)}")
        out.append("")
    for index, (label, key) in enumerate(items, start=1):
        value = fields.get(key)
        out.append(f"## {index}. {label}")
        out.append("")
        if isinstance(value, list):
            out.extend(f"- {redact(v, limit=500)}" for v in value) if value else out.append("not available")
        else:
            out.append(redact(value, limit=4000) if value else "not available")
        out.append("")
    return "\n".join(out)
