"""Bounded task-start session selection and durable fresh-session preparation. A NEW fresh Codex is started directly as a new
Codex TUI in its fresh pane through the explicit, journaled pane-run recipe (pinned executable, explicit CODEX_HOME and working directory, the configured model and
approval arguments, `--no-daemon`); there is no pre-created thread and no `resume` argument, and the native session id is learned only from the owned pane/process.
A preparation journaled by an earlier library for the pre-created-thread handoff (`thread_*` fields) is still read and reconciled exactly as before, and is never converted
into a direct launch. Fresh Claude keeps its provider-specific path."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Callable

import herdr_helper_context as hctx
import herdr_startup as hst
from herdr_cli import help_has_lexeme, match_exact_session, session_identity
from herdr_core import (
    APPROVAL_MODES,
    PROVIDERS,
    READY_STATES,
    AbsoluteDeadlinePassed,
    BoundPreparationUnstarted,
    HerdrError,
    StartupNotReady,
    StartupWaitRequired,
    SupervisorError,
    approval_mode,
    approval_mode_args,
    atomic_write_json,
    canonical_json,
    iso_utc,
    load_json,
    startup_wait_settings,
    valid_pane_id,
)

SESSION_POLICIES = ("preserve", "fresh-codex", "fresh-claude", "fresh-all")
PANE_ID_MAX = 128
_PREPARATION_STATES = ("SESSION_SELECTION_AUTHORIZED", "BINDING", "OWNERSHIP_BOUND", "TASK_STARTED", "ABANDONED")
# BOOTSTRAP_* exist only for a provider that reports no native identity before its first turn (see _bootstrap).
BOOTSTRAP_STATES = ("BOOTSTRAP_INTENT", "BOOTSTRAP_SUBMITTED", "BOOTSTRAP_ACKNOWLEDGED")
# Explicit Codex launch (`session_start.codex_launch_method = explicit-pane-run`): the launch intent/attempt are journaled
# before the single `pane run`; admission is not execution; the expected process is observed and bound before the alias.
LAUNCH_STATES = ("LAUNCH_INTENT", "LAUNCH_SUBMITTED", "LAUNCH_ADMITTED", "PROCESS_BOUND")
LAUNCH_OBSERVE_ATTEMPTS = 10
# Agent states that may mean "waiting for a human at startup" (never "working", never an ambiguous or missing agent).
STARTUP_BLOCKED_STATUSES = ("blocked", "unknown")
# Only these provider phases may open a human startup wait: nothing has been submitted that could be uncertain (no LAUNCH_SUBMITTED/ADMITTED, no BOOTSTRAP_SUBMITTED/ACKNOWLEDGED).
STARTUP_WAIT_ELIGIBLE = ("PROCESS_BOUND", "AGENT_STARTING", "BOOTSTRAP_INTENT")
LAUNCH_OBSERVE_INTERVAL = 1.0
EXPLICIT_LAUNCH = hctx.EXPLICIT_LAUNCH_METHOD
STRICT_POLICY = hctx.STRICT_INPUT_POLICY
GUARDED_POLICY = hctx.GUARDED_INPUT_POLICY
GUARDED_WINDOW = hctx.GUARDED_WINDOW_SECONDS
_PROVIDER_STATES = ("AUTHORIZED", "THREAD_CREATING", "THREAD_CREATED", "PANE_CREATING", "FRESH_PANE_CREATED", *LAUNCH_STATES, "AGENT_STARTING", *BOOTSTRAP_STATES, "IDENTITY_VERIFIED")
_LABEL_STATUSES = ("NAMING_INTENT", "NAMED", "NAME_FAILED", "NAME_UNVERIFIED", "NAME_MISMATCH", "NAME_UNSUPPORTED")
BOOTSTRAP_ACK_MS = 60_000
BOOTSTRAP_WAIT_MS = 300_000
# Providers whose fresh session USED to be a pre-created durable thread resumed in the new pane (exact-id identity). Only journaled legacy items still follow that path;
# a NEW Codex item carries `fresh_launch = DIRECT_LAUNCH` and is launched directly (see `_direct`).
THREAD_PROVIDERS = ("codex",)
DIRECT_LAUNCH = "direct-cli"
_THREAD_PHASES = ("THREAD_CREATING", "THREAD_CREATED", "PANE_CREATING", "FRESH_PANE_CREATED", *LAUNCH_STATES, "AGENT_STARTING", *BOOTSTRAP_STATES, "IDENTITY_VERIFIED")
_PANE_PHASES = ("FRESH_PANE_CREATED", *LAUNCH_STATES, "AGENT_STARTING", *BOOTSTRAP_STATES, "IDENTITY_VERIFIED")
# Every surface Fresh Codex needs; each is verified read-only and reported by name (doctor, Telegram predicate).
CODEX_THREAD_COMPONENTS = ("herdr_pane_split", "herdr_agent_start", "herdr_identity_observation", "codex_app_server", "codex_thread_start", "codex_resume_session_id", "codex_pane_local_server", "codex_helper_ready")


def session_contract_supported(config: dict[str, Any], report: Any) -> bool:
    """One side-effect eligibility predicate shared by UI and execution."""
    split_name = "task-start pane split (--direction/--ratio/--cwd/--no-focus)"
    start_name = "agent start (--kind/--pane)"
    return bool(
        config["session_start"]["enabled"]
        and isinstance(report, dict)
        and report.get("compatible") is True
        and isinstance(report.get("required"), dict)
        and report["required"].get(start_name) is True
        and isinstance(report.get("optional"), dict)
        and report["optional"].get(split_name) is True
    )


def _valid_uuid(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return str(uuid.UUID(value)) == value
    except (ValueError, AttributeError):
        return False


def _valid_pane(value: Any) -> bool:
    return valid_pane_id(value)


def _valid_owner(value: Any) -> bool:
    return isinstance(value, dict) and set(value) == {"pane_id", "session_id"} and _valid_pane(value["pane_id"]) and _valid_uuid(value["session_id"])


def _direct(item: Any) -> bool:
    """True for a fresh-Codex item journaled as a direct CLI launch (no pre-created thread). Legacy items never carry the marker."""
    return isinstance(item, dict) and item.get("fresh_launch") == DIRECT_LAUNCH


def _item_cwd(item: dict[str, Any]) -> str:
    request: dict[str, Any] = (item.get("launch_request") if _direct(item) else item.get("thread_payload")) or {}
    return str(request["cwd"])


def fresh_providers(policy: str) -> tuple[str, ...]:
    if policy not in SESSION_POLICIES:
        raise SupervisorError(f"invalid session policy: {policy!r}")
    return tuple(provider for provider in PROVIDERS if policy in (f"fresh-{provider}", "fresh-all"))


def validate_selection(config: dict[str, Any], selection: Any) -> dict[str, Any]:
    if selection is None:
        selection = {"policy": "preserve", "profiles": {provider: "default" for provider in PROVIDERS}}
    if not isinstance(selection, dict):
        raise SupervisorError("session selection must be an object")
    policy = selection.get("policy", "preserve")
    if not isinstance(policy, str):
        raise SupervisorError("session policy must be a string")
    fresh = fresh_providers(policy)
    profiles = selection["profiles"] if "profiles" in selection else {}
    if not isinstance(profiles, dict) or any(key not in PROVIDERS for key in profiles):
        raise SupervisorError("session model profiles are invalid")
    normalized: dict[str, str] = {}
    for provider in PROVIDERS:
        profile = profiles.get(provider, "default")
        if not isinstance(profile, str):
            raise SupervisorError(f"{provider} model profile must be a string")
        normalized[provider] = profile
    configured = config["session_start"]["model_profiles"]
    for provider in PROVIDERS:
        profile = normalized[provider]
        if profile not in configured[provider]:
            raise SupervisorError(f"unknown {provider} model profile: {profile}")
        if provider not in fresh and profile != "default":
            raise SupervisorError(f"a model profile cannot be selected for preserved {provider}")
    return {"policy": policy, "profiles": normalized, "fresh_providers": list(fresh)}


def cli_request_envelope(
    config: dict[str, Any], *, task: Any, start: Any, task_reference: Any,
    workflow_policy: Any, codex_reset_authorization: Any, selection: Any,
) -> dict[str, Any]:
    """Return the sanitized effective CLI request that an initialized run must retain."""
    if not isinstance(task, str) or start not in PROVIDERS or not isinstance(workflow_policy, str):
        raise SupervisorError("CLI session preparation request is invalid")
    if task_reference is not None and not isinstance(task_reference, str):
        raise SupervisorError("CLI session preparation task reference is invalid")
    selected = validate_selection(config, selection)
    if codex_reset_authorization is None:
        reset = {"budget": 0, "available_count": None, "account_fingerprint": None}
    elif isinstance(codex_reset_authorization, dict):
        reset = {key: codex_reset_authorization.get(key) for key in ("budget", "available_count", "account_fingerprint")}
    else:
        raise SupervisorError("CLI session preparation reset authorization is invalid")
    budget, available, fingerprint = reset.values()
    valid_available = available is None or (type(available) is int and available >= 0)
    if type(budget) is not int or budget < 0 or not valid_available or (available is None and budget != 0) or (available is not None and budget > available):
        raise SupervisorError("CLI session preparation reset authorization is invalid")
    if fingerprint is not None and (not isinstance(fingerprint, str) or len(fingerprint) != 64 or any(c not in "0123456789abcdef" for c in fingerprint)):
        raise SupervisorError("CLI session preparation reset account identity is invalid")
    if budget > 0 and fingerprint is None:
        raise SupervisorError("CLI session preparation positive reset budget lacks account identity")
    return {
        "task_sha256": hashlib.sha256(task.encode()).hexdigest(),
        "start": start,
        "task_reference": task_reference,
        "workflow_policy": workflow_policy,
        "codex_reset_authorization": reset,
        "session_selection": selected,
    }


def validate_cli_request_envelope(config: dict[str, Any], value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "task_sha256", "start", "task_reference", "workflow_policy",
        "codex_reset_authorization", "session_selection",
    }:
        raise SupervisorError("CLI session preparation request envelope is invalid")
    task_sha = value.get("task_sha256")
    if not isinstance(task_sha, str) or len(task_sha) != 64 or any(c not in "0123456789abcdef" for c in task_sha):
        raise SupervisorError("CLI session preparation task digest is invalid")
    # Use a placeholder whose digest is replaced below so the shared constructor validates every
    # other primitive and normalizes the reset/selection fields without retaining task contents.
    normalized = cli_request_envelope(
        config, task="", start=value.get("start"), task_reference=value.get("task_reference"),
        workflow_policy=value.get("workflow_policy"),
        codex_reset_authorization=value.get("codex_reset_authorization"), selection=value.get("session_selection"),
    )
    normalized["task_sha256"] = task_sha
    if normalized != value:
        raise SupervisorError("CLI session preparation request envelope is not canonical")
    return normalized


def cli_request_binding(config: dict[str, Any], envelope: Any) -> str:
    return hashlib.sha256(canonical_json(validate_cli_request_envelope(config, envelope))).hexdigest()


def model_args(config: dict[str, Any], provider: str, profile_id: str) -> list[str]:
    model = config["session_start"]["model_profiles"][provider][profile_id].get("model")
    return [] if model is None else ["--model", model]


def approval_args(config: dict[str, Any], provider: str) -> list[str]:
    """The fixed launch arguments of the configured approval mode (empty for inherit). Never a raw argument list."""
    return approval_mode_args(provider, approval_mode(config, provider))


def assert_recorded_approval_modes(config: dict[str, Any], record: dict[str, Any], providers: Any = None) -> None:
    """Refuse when a journaled preparation's recorded approval mode differs from the configured one (both directions). Read-only: it runs before any
    owner, journal, launch or task write, so a refusal leaves every durable record untouched. A record without the key is an `inherit` record."""
    for provider in (record.get("providers") or {}) if providers is None else providers:
        item = (record.get("providers") or {}).get(provider)
        if not isinstance(item, dict):
            continue
        recorded_mode, configured_mode = item.get("approval_mode", "inherit"), approval_mode(config, provider)
        if recorded_mode != configured_mode:
            raise SupervisorError(f"{provider} approval mode changed since the preparation was authorized (recorded {recorded_mode}, configured {configured_mode}); nothing was repeated")


def probe_approval_capability(command: str, provider: str, mode: str, runner: Callable[..., Any] = subprocess.run) -> bool:
    """Read-only capability probe of a closed approval mode: the installed CLI's own help must offer the exact flag (and, for Claude, `auto` among its
    permission-mode choices). Help text is capability evidence only; the effective mode of a running session is a separate native observation."""
    args = approval_mode_args(provider, mode)
    if not args:
        return True
    text = _help_output([command, "resume", "--help"] if provider == "codex" else [command, "--help"], runner)
    if text is None or not help_has_lexeme(text, args[0]):
        return False
    if provider == "claude":
        start = text.index(args[0])  # help_has_lexeme proved the option is present
        return '"' + args[1] + '"' in text[start:start + 400]
    return True


def probe_model_flag(command: str, runner: Callable[..., Any] = subprocess.run) -> bool:
    """Read-only exact-lexeme capability probe. It never starts or contacts an agent session."""
    try:
        result = runner([command, "--help"], check=False, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and help_has_lexeme((result.stdout or "") + (result.stderr or ""), "--model")


def _help_output(command: list[str], runner: Callable[..., Any]) -> str | None:
    try:
        result = runner(command, check=False, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return ((result.stdout or "") + (result.stderr or "")) if result.returncode == 0 else None


def thread_schema_supports_start(client_request: Any, response_schema: Any) -> bool:
    """Pure policy over the generated app-server JSON schema: `thread/start` is a client request whose
    response requires the `thread` object, `model`, `modelProvider`, and `cwd` fields we consume."""
    methods: set[str] = set()
    if isinstance(client_request, dict):
        for variant in client_request.get("oneOf") or []:
            enum = ((variant.get("properties") or {}).get("method") or {}).get("enum") if isinstance(variant, dict) else None
            if isinstance(enum, list):
                methods.update(str(m) for m in enum)
    required = response_schema.get("required") if isinstance(response_schema, dict) else None
    return "thread/start" in methods and isinstance(required, list) and {"thread", "model", "modelProvider", "cwd"} <= set(required)


def probe_codex_thread_contract(command: str, runner: Callable[..., Any] = subprocess.run) -> dict[str, bool]:
    """Read-only Codex CLI probe of the Fresh-Codex chain: the app-server exists, its generated schema
    declares `thread/start` with the consumed response shape, and `codex resume` accepts a SESSION_ID. The
    schema is generated into a private temporary directory; nothing is started, prompted, or written to
    the user's Codex state."""
    app_server_help = _help_output([command, "app-server", "--help"], runner)
    resume_help = _help_output([command, "resume", "--help"], runner)
    app_server = app_server_help is not None and help_has_lexeme(app_server_help, "generate-json-schema")
    resume = resume_help is not None and help_has_lexeme(resume_help, "SESSION_ID")
    thread_start = False
    if app_server:
        with tempfile.TemporaryDirectory(prefix="herdr-codex-schema-") as tmp:
            if _help_output([command, "app-server", "generate-json-schema", "--out", tmp], runner) is not None:
                try:
                    client_request = json.loads((Path(tmp) / "ClientRequest.json").read_text())
                    response = json.loads((Path(tmp) / "v2" / "ThreadStartResponse.json").read_text())
                except (OSError, json.JSONDecodeError):
                    client_request, response = None, None
                thread_start = thread_schema_supports_start(client_request, response)
    return {"codex_app_server": app_server, "codex_thread_start": thread_start, "codex_resume_session_id": resume, "codex_pane_local_server": resume_help is not None and help_has_lexeme(resume_help, "--no-daemon")}


def fresh_codex_components(config: dict[str, Any], herdr_report: Any, codex_contract: Any, helper_readiness: Any = None) -> dict[str, bool]:
    """Per-component verdicts of the complete Fresh Codex capability chain (all must be True). CLI help success
    alone is not readiness: `helper_readiness` is the shared read-only helper report (units, journal, profile)."""
    split_name = "task-start pane split (--direction/--ratio/--cwd/--no-focus)"
    start_name = "agent start (--kind/--pane)"
    report = herdr_report if isinstance(herdr_report, dict) else {}
    contract = codex_contract if isinstance(codex_contract, dict) else {}
    compatible = bool(config["session_start"]["enabled"] and report.get("compatible") is True)
    return {
        "herdr_pane_split": compatible and (report.get("optional") or {}).get(split_name) is True,
        "herdr_agent_start": compatible and (report.get("required") or {}).get(start_name) is True,
        "herdr_identity_observation": compatible and (report.get("required") or {}).get("agent get") is True and (report.get("required") or {}).get("agent list") is True,
        "codex_app_server": contract.get("codex_app_server") is True,
        "codex_thread_start": contract.get("codex_thread_start") is True,
        "codex_resume_session_id": contract.get("codex_resume_session_id") is True,
        "codex_pane_local_server": contract.get("codex_pane_local_server") is True,
        "codex_helper_ready": isinstance(helper_readiness, dict) and helper_readiness.get("ready") is True,
    }


def probe_codex_direct_contract(command: str, mode: str = "inherit", runner: Callable[..., Any] = subprocess.run) -> dict[str, bool]:
    """Read-only probe of what a DIRECT fresh Codex launch (`codex [--model M] [approval args] --no-daemon`) needs from the installed CLI's top-level help: pane-local
    `--no-daemon` and the exact flag of the configured approval mode. It starts, prompts and contacts nothing."""
    text = _help_output([command, "--help"], runner)
    args = approval_mode_args("codex", mode)
    return {"codex_pane_local_server": text is not None and help_has_lexeme(text, "--no-daemon"),
            "codex_approval_flag": text is not None and all(help_has_lexeme(text, arg) for arg in args if arg.startswith("--"))}


def selected_launch_method(config: dict[str, Any]) -> str:
    return str(config["session_start"].get("codex_launch_method", "agent-start"))


def fresh_codex_direct_components(config: dict[str, Any], herdr_report: Any, codex_contract: Any, launch_readiness: Any = None, *, new_start: bool = True) -> dict[str, bool]:
    """Per-component verdicts of the DIRECT fresh-Codex capability chain (all must be True): the Herdr pane split and identity observation, the installed CLI's
    pane-local server and approval flag, and the explicit launch recipe with the Herdr capabilities it uses. No app-server, thread/start, resume or helper-journal
    component: this launch creates no helper thread.

    `new_start` (the default: doctor, Telegram eligibility, the preflight of a NEW preparation) also requires that the SELECTED launch method is `explicit-pane-run`, because a new fresh Codex is never
    started through the generic agent start. A journaled preparation keeps the method and policy it recorded (`new_start=False`): changing the configuration neither reinterprets nor replays it."""
    split_name = "task-start pane split (--direction/--ratio/--cwd/--no-focus)"
    report = herdr_report if isinstance(herdr_report, dict) else {}
    contract = codex_contract if isinstance(codex_contract, dict) else {}
    compatible = bool(config["session_start"]["enabled"] and report.get("compatible") is True)
    components = {
        "herdr_pane_split": compatible and (report.get("optional") or {}).get(split_name) is True,
        "herdr_identity_observation": compatible and (report.get("required") or {}).get("agent get") is True and (report.get("required") or {}).get("agent list") is True,
        "codex_pane_local_server": contract.get("codex_pane_local_server") is True,
        "codex_approval_flag": contract.get("codex_approval_flag") is True,
        "codex_direct_launch": isinstance(launch_readiness, dict) and launch_readiness.get("ready") is True,
    }
    if new_start:
        components["codex_launch_method_selected"] = selected_launch_method(config) == "explicit-pane-run"
    return components


def fresh_policy_supported(config: dict[str, Any], policy: str, herdr_report: Any, codex_contract: Any, launch_readiness: Any = None) -> bool:
    """One eligibility predicate for UI and execution: preserve needs nothing; a fresh Claude needs the Herdr
    pane/agent contract; any policy that creates a NEW fresh Codex needs the direct-launch chain."""
    if policy not in SESSION_POLICIES:
        return False
    fresh = fresh_providers(policy)
    if not fresh:
        return True
    if not session_contract_supported(config, herdr_report):
        return False
    if any(provider in THREAD_PROVIDERS for provider in fresh):
        return all(fresh_codex_direct_components(config, herdr_report, codex_contract, launch_readiness).values())
    return True


def thread_start_payload(config: dict[str, Any], provider: str, profile_id: str) -> dict[str, Any]:
    """Supervisor-owned thread/start request: the approved absolute workspace, the configured model or the
    provider default, and a durable thread."""
    root = str(config["project_root"])
    if not root.startswith("/"):
        raise SupervisorError("project_root must be absolute for a fresh thread")
    return {"cwd": root, "model": config["session_start"]["model_profiles"][provider][profile_id].get("model"), "ephemeral": False}


def launch_request_payload(config: dict[str, Any], provider: str, profile_id: str) -> dict[str, Any]:
    """Supervisor-owned description of a DIRECT launch: the approved absolute workspace and the configured model (None = the provider default)."""
    root = str(config["project_root"])
    if not root.startswith("/"):
        raise SupervisorError("project_root must be absolute for a fresh launch")
    return {"cwd": root, "model": config["session_start"]["model_profiles"][provider][profile_id].get("model")}


def thread_request_id(preparation_id: str, provider: str) -> str:
    return f"thread-{preparation_id}-{provider}"


def preparation_view(record: dict[str, Any] | None) -> dict[str, Any] | None:
    """Public projection of a preparation journal for state/status: operational fields and the opaque
    request binding only. The request envelope (task digest, task-reference path, reset account identity)
    stays in the private journal; recovery reconstructs it from authoritative run state and compares it there."""
    if not isinstance(record, dict):
        return None
    providers = record.get("providers") or {}
    return {
        "preparation_id": record.get("preparation_id"),
        "status": record.get("status"),
        "origin": record.get("origin"),
        "request_binding": record.get("request_binding"),
        "policy": (record.get("selection") or {}).get("policy"),
        "profiles": (record.get("selection") or {}).get("profiles"),
        "providers": {p: {"status": v.get("status"), "old_session": str(v.get("old_session_id") or "")[:8], "new_session": str(v.get("session_id") or "")[:8], "pane_id": v.get("pane_id"),
                          **({"launch_method": v["launch_method"]} if "launch_method" in v else {}), **({"input_policy": v["input_policy"]} if "input_policy" in v else {}),
                          **({"fresh_launch": v["fresh_launch"]} if "fresh_launch" in v else {})} for p, v in providers.items() if isinstance(v, dict)},
    }


def _valid_process_record(proc: Any) -> bool:
    return (isinstance(proc,dict) and set(proc)=={"pid","shell_pid","argv","cwd"} and type(proc["pid"]) is int and type(proc["shell_pid"]) is int
            and isinstance(proc["argv"],list) and all(isinstance(a,str) for a in proc["argv"]) and isinstance(proc["cwd"],str))


def validate_preparation_record(config: dict[str, Any], record: Any, preparation_id: str) -> dict[str, Any]:
    if not isinstance(record, dict) or type(record.get("schema_version")) is not int or record.get("schema_version") != 2 or record.get("preparation_id") != preparation_id:
        raise SupervisorError("session preparation record is invalid")
    if not _valid_uuid(preparation_id):
        raise SupervisorError("session preparation identity is invalid")
    selection=validate_selection(config,record.get("selection"))
    if selection != record.get("selection") or record.get("status") not in _PREPARATION_STATES:
        raise SupervisorError("session preparation selection or state is invalid")
    if record.get("origin") not in ("cli", "supervisor") or not isinstance(record.get("created_at"), str):
        raise SupervisorError("session preparation origin or timestamp is invalid")
    request_binding = record.get("request_binding")
    if request_binding is not None and (not isinstance(request_binding, str) or len(request_binding) != 64 or any(c not in "0123456789abcdef" for c in request_binding)):
        raise SupervisorError("session preparation request binding is invalid")
    if (record["origin"] == "cli") != (request_binding is not None):
        raise SupervisorError("session preparation origin is not bound to its request")
    request_envelope = record.get("request_envelope")
    if record["origin"] == "cli":
        envelope = validate_cli_request_envelope(config, request_envelope)
        if envelope.get("session_selection") != selection or cli_request_binding(config, envelope) != request_binding:
            raise SupervisorError("session preparation request does not match its binding")
    elif request_envelope is not None:
        raise SupervisorError("supervisor session preparation has a CLI request envelope")
    owners_before=record.get("owners_before")
    if not isinstance(owners_before,dict) or set(owners_before)!=set(PROVIDERS) or not all(_valid_owner(owners_before[p]) for p in PROVIDERS):
        raise SupervisorError("session preparation owner snapshot is invalid")
    providers=record.get("providers")
    if not isinstance(providers,dict) or set(providers)!=set(selection["fresh_providers"]):
        raise SupervisorError("session preparation provider set is invalid")
    for provider,item in providers.items():
        if not isinstance(item,dict) or item.get("status") not in _PROVIDER_STATES or item.get("profile")!=selection["profiles"][provider]:
            raise SupervisorError(f"{provider} session preparation record is invalid")
        if item.get("preparation_id") != preparation_id or item.get("old_pane_id") != owners_before[provider]["pane_id"] or item.get("old_session_id") != owners_before[provider]["session_id"]:
            raise SupervisorError(f"{provider} old owner binding is invalid")
        phase=item["status"]
        direct = _direct(item)
        threaded = provider in THREAD_PROVIDERS and not direct
        if not threaded and (phase in ("THREAD_CREATING","THREAD_CREATED") or any(key in item for key in ("thread_id","thread_request_id","thread_payload","thread_model","thread_provider"))):
            raise SupervisorError(f"{provider} preparation carries thread fields for a provider without a pre-created thread")
        if "fresh_launch" in item and (item["fresh_launch"] != DIRECT_LAUNCH or provider not in THREAD_PROVIDERS or item.get("launch_method") != EXPLICIT_LAUNCH or "launch_request" not in item):
            raise SupervisorError(f"{provider} direct launch record is invalid")
        if direct:
            request = item.get("launch_request")
            if (not isinstance(request, dict) or set(request) != {"cwd", "model"} or not isinstance(request["cwd"], str) or not request["cwd"].startswith("/") or not hctx.valid_model(request["model"])
                    or request["model"] != config["session_start"]["model_profiles"][provider][item["profile"]].get("model")):
                raise SupervisorError(f"{provider} direct launch request is invalid")
            bound_context = item.get("helper_context_fingerprint")
            if not isinstance(bound_context, str) or len(bound_context) != 64 or any(c not in "0123456789abcdef" for c in bound_context):
                raise SupervisorError(f"{provider} helper context binding is invalid")
        elif "launch_request" in item:
            raise SupervisorError(f"{provider} preparation carries a direct launch request without the direct launch marker")
        if threaded:
            if phase in _THREAD_PHASES:
                if item.get("thread_request_id") != thread_request_id(preparation_id, provider) or not isinstance(item.get("thread_payload"), dict) or set(item["thread_payload"]) != {"cwd","model","ephemeral"}:
                    raise SupervisorError(f"{provider} thread request binding is invalid")
                bound_context = item.get("helper_context_fingerprint")
                if bound_context is not None and (not isinstance(bound_context, str) or len(bound_context) != 64 or any(c not in "0123456789abcdef" for c in bound_context)):
                    raise SupervisorError(f"{provider} helper context binding is invalid")
            elif "helper_context_fingerprint" in item:
                raise SupervisorError(f"{provider} preparation fields do not match its state")
            if phase in _THREAD_PHASES[1:]:
                if not _valid_uuid(item.get("thread_id")) or not isinstance(item.get("thread_model"), str) or not isinstance(item.get("thread_provider"), str):
                    raise SupervisorError(f"{provider} pre-created thread identity is missing")
                if item["thread_payload"].get("model") is not None and item["thread_model"] != item["thread_payload"]["model"]:
                    raise SupervisorError(f"{provider} pre-created thread model does not match the request")
            if phase == "IDENTITY_VERIFIED" and item.get("session_id") != item.get("thread_id"):
                raise SupervisorError(f"{provider} verified identity is not the pre-created thread")
            if phase in ("AUTHORIZED","THREAD_CREATING") and "thread_id" in item:
                raise SupervisorError(f"{provider} preparation fields do not match its state")
        if phase in _PANE_PHASES and not _valid_pane(item.get("pane_id")):
            raise SupervisorError(f"{provider} prepared pane is missing")
        expected_name=f"{provider}-run-{preparation_id[:8]}"
        if phase in _PANE_PHASES[1:] and item.get("agent_name") != expected_name:
            raise SupervisorError(f"{provider} prepared agent name is invalid")
        if phase=="IDENTITY_VERIFIED" and not _valid_uuid(item.get("session_id")):
            raise SupervisorError(f"{provider} prepared identity is missing")
        if phase in ("AUTHORIZED","THREAD_CREATING","THREAD_CREATED","PANE_CREATING") and any(key in item for key in ("pane_id","agent_name","session_id")):
            raise SupervisorError(f"{provider} preparation fields do not match its state")
        if phase=="FRESH_PANE_CREATED" and any(key in item for key in ("agent_name","session_id")):
            raise SupervisorError(f"{provider} preparation fields do not match its state")
        if phase in (*LAUNCH_STATES, "AGENT_STARTING", *BOOTSTRAP_STATES) and "session_id" in item:
            raise SupervisorError(f"{provider} preparation fields do not match its state")
        method=item.get("launch_method")
        launch=item.get("launch")
        if method is not None and (provider not in THREAD_PROVIDERS or method != EXPLICIT_LAUNCH):
            raise SupervisorError(f"{provider} launch method record is invalid")
        if launch is not None and method is None:
            raise SupervisorError(f"{provider} preparation carries a launch journal without a launch method")
        recorded_mode=item.get("approval_mode")
        if recorded_mode is not None and (type(recorded_mode) is not str or recorded_mode == "inherit" or recorded_mode not in APPROVAL_MODES[provider]):
            raise SupervisorError(f"{provider} approval mode record is invalid")
        input_policy=item.get("input_policy")
        if input_policy is not None and (method != EXPLICIT_LAUNCH or input_policy not in hctx.CODEX_INPUT_POLICIES):
            raise SupervisorError(f"{provider} input policy record is invalid")
        needs_launch = method == EXPLICIT_LAUNCH and phase in (*LAUNCH_STATES, "AGENT_STARTING", *BOOTSTRAP_STATES, "IDENTITY_VERIFIED")
        if needs_launch != isinstance(launch, dict):
            raise SupervisorError(f"{provider} launch journal does not match its state")
        if isinstance(launch, dict):
            try:
                recipe=hctx.validate_launch_recipe(launch.get("recipe"))
            except hctx.ContextError as error:
                raise SupervisorError(f"{provider} launch recipe is invalid") from error
            if recipe.get("approval_mode", "inherit") != item.get("approval_mode", "inherit"):
                raise SupervisorError(f"{provider} launch recipe approval mode does not match the preparation record")
            if direct != (recipe.get("form") == hctx.DIRECT_FORM) or (direct and recipe["model"] != item["launch_request"]["model"]):
                raise SupervisorError(f"{provider} launch recipe form does not match the preparation record")
            if (set(launch) - {"recipe","pane_id","shell","process"} or launch.get("pane_id") != item.get("pane_id") or recipe.get("thread_id") != item.get("thread_id")
                    or recipe["cwd"] != _item_cwd(item) or not isinstance(launch.get("shell"),dict) or set(launch["shell"]) != {"shell_pid","cwd","name"}
                    or type(launch["shell"]["shell_pid"]) is not int or not isinstance(launch["shell"]["cwd"],str) or not isinstance(launch["shell"]["name"],str)):
                raise SupervisorError(f"{provider} launch journal is invalid")
            bound_phase = phase not in ("LAUNCH_INTENT","LAUNCH_SUBMITTED","LAUNCH_ADMITTED")
            if bound_phase != ("process" in launch) or ("process" in launch and not _valid_process_record(launch["process"])):
                raise SupervisorError(f"{provider} launch process binding does not match its state")
        boot=item.get("bootstrap")
        if phase in BOOTSTRAP_STATES and not isinstance(boot,dict):
            raise SupervisorError(f"{provider} bootstrap journal is missing")
        if boot is not None:
            if (not isinstance(boot,dict) or boot.get("pane_id")!=item.get("pane_id") or boot.get("agent_name")!=item.get("agent_name") or boot.get("provider")!=provider
                    or not isinstance(boot.get("nonce"),str) or len(boot["nonce"])!=32 or any(c not in "0123456789abcdef" for c in boot["nonce"])
                    or not isinstance(boot.get("text_sha256"),str) or len(boot["text_sha256"])!=64
                    or boot.get("expected_identity") not in (None,item.get("thread_id")) or set(boot)-{"nonce","pane_id","agent_name","provider","expected_identity","text_sha256","outcome","process"}
                    or boot.get("outcome") not in (None,"completed","observed-complete","not-needed")):
                raise SupervisorError(f"{provider} bootstrap journal is invalid")
            if provider in THREAD_PROVIDERS and boot.get("expected_identity")!=item.get("thread_id"):
                raise SupervisorError(f"{provider} bootstrap is not bound to the pre-created thread")
        proc=boot.get("process") if isinstance(boot,dict) else None
        if isinstance(boot,dict) and not _valid_process_record(proc):
            raise SupervisorError(f"{provider} bootstrap process record is invalid")
        completed=isinstance(boot,dict) and boot.get("outcome") in ("completed","observed-complete")
        if "initialization" in item and (phase!="IDENTITY_VERIFIED" or item["initialization"] not in ("native-reported","bootstrap-completed") or (item["initialization"]=="bootstrap-completed")!=completed
                                         or (item["initialization"]=="native-reported" and isinstance(boot,dict) and boot.get("outcome")!="not-needed")):
            raise SupervisorError(f"{provider} initialization record is invalid")
        if phase=="IDENTITY_VERIFIED" and "initialization" in item:
            if item.get("process_interactive") is not True or item.get("effective_model_status") not in ("reported","unknown") or (item.get("effective_model_status")=="reported")!=isinstance(item.get("effective_model"),str):
                raise SupervisorError(f"{provider} lifecycle record is invalid")
        label=item.get("label_state")
        if label is not None and (phase!="IDENTITY_VERIFIED" or not isinstance(label,dict) or label.get("status") not in _LABEL_STATUSES or label.get("pane_id")!=item.get("pane_id")
                                  or label.get("target")!=pane_label(provider,preparation_id)):
            raise SupervisorError(f"{provider} pane label record is invalid")
    occupied_panes={owners_before[provider]["pane_id"] for provider in PROVIDERS}
    for provider,item in providers.items():
        if item["status"] not in _PANE_PHASES:
            continue
        pane=item["pane_id"]
        if pane in occupied_panes:
            raise SupervisorError(f"{provider} prepared pane is not distinct from every existing or prepared owner pane")
        occupied_panes.add(pane)
    bound=record.get("bound_owners")
    if record["status"] in ("BINDING","OWNERSHIP_BOUND","TASK_STARTED") and any(item["status"] != "IDENTITY_VERIFIED" for item in providers.values()):
        raise SupervisorError("session ownership state has incomplete provider identities")
    if record["status"] in ("OWNERSHIP_BOUND","TASK_STARTED"):
        if not isinstance(bound,dict) or set(bound)!=set(PROVIDERS) or not all(_valid_owner(bound[p]) for p in PROVIDERS):
            raise SupervisorError("bound session owners are invalid")
        expected={p:dict(owners_before[p]) for p in PROVIDERS}
        for provider,item in providers.items():
            expected[provider]={"pane_id":item["pane_id"],"session_id":item["session_id"]}
        if bound != expected:
            raise SupervisorError("bound session owners do not match verified identities")
    elif bound is not None:
        raise SupervisorError("bound owners appeared before ownership binding")
    if record["status"] == "TASK_STARTED" and (record.get("started_run_id") != preparation_id or not isinstance(record.get("task_started_at"),str)):
        raise SupervisorError("session preparation task-start binding is invalid")
    return record


class IdentityPending(SupervisorError):
    """The agent is uniquely ready in its prepared pane but has not reported a native session identity yet."""


def bootstrap_text(nonce: str) -> str:
    """The whole bootstrap turn: no task, tools, product instructions or write request (see _bootstrap)."""
    return f"herdr-supervisor initialization check {nonce}. Reply with the single word READY."


def pane_label(provider: str, preparation_id: str) -> str:
    """Deterministic presentation label of a newly created pane (distinct from the unique agent alias)."""
    return f"Fresh {provider.title()} {preparation_id[:8]}"


def _verify_started_identity(supervisor: Any, provider: str, item: dict[str, Any], live: list[dict[str, Any]]) -> str:
    name=item.get("agent_name") or f"{provider}-run-{item.get('preparation_id','')[:8]}"
    candidates=[a for a in live if a.get("name")==name and a.get("agent")==provider and a.get("pane_id")==item.get("pane_id")]
    if len(candidates)==1 and candidates[0].get("agent_status") in STARTUP_BLOCKED_STATUSES:
        # One agent, in its prepared pane, reported blocked/unknown (e.g. a startup prompt): a SIGNAL only; the gate in prepare_sessions decides whether it opens a human wait.
        raise StartupNotReady(f"fresh {provider} agent did not become uniquely ready in its prepared pane", site="agent_not_ready", pane_id=item.get("pane_id"), provider=provider)
    if len(candidates)!=1 or candidates[0].get("agent_status") not in READY_STATES:
        raise SupervisorError(f"fresh {provider} agent did not become uniquely ready in its prepared pane")
    identity=session_identity(candidates[0])
    if not identity:
        raise IdentityPending(f"fresh {provider} agent has a missing or duplicate native session identity (none reported yet)")
    if any(session_identity(a)==identity for a in live if a is not candidates[0]):
        raise SupervisorError(f"fresh {provider} agent has a missing or duplicate native session identity")
    if provider in THREAD_PROVIDERS and not _direct(item) and identity != item.get("thread_id"):
        raise SupervisorError(f"fresh {provider} agent reports native session {identity[:8]} instead of the pre-created thread {str(item.get('thread_id'))[:8]}; ownership was not changed")
    expected_model=supervisor.config["session_start"]["model_profiles"][provider][item["profile"]].get("model")
    reported_model=candidates[0].get("model")
    if expected_model is not None and isinstance(reported_model,str) and reported_model!=expected_model:
        raise SupervisorError(f"fresh {provider} agent reported a different model; ownership was not changed")
    return identity


def _adopt_thread(item: dict[str, Any], thread: dict[str, Any]) -> None:
    """Persist only the sanitized thread identity/launch metadata returned by the strict parser."""
    thread_id = thread.get("thread_id")
    if not _valid_uuid(thread_id):
        raise SupervisorError("pre-created thread id is not canonical")
    requested_model = item["thread_payload"].get("model")
    if requested_model is not None and thread.get("model") != requested_model:
        raise SupervisorError("pre-created thread model does not match the configured profile")
    item.update({"status": "THREAD_CREATED", "thread_id": thread_id, "thread_model": str(thread.get("model")), "thread_provider": str(thread.get("model_provider"))})


def _live_candidate(supervisor: Any, provider: str, item: dict[str, Any]) -> dict[str, Any] | None:
    live = supervisor.herdr.list_agents()
    found = [a for a in live if a.get("name") == item.get("agent_name") and a.get("agent") == provider and a.get("pane_id") == item.get("pane_id")]
    return found[0] if len(found) == 1 else None


def _model_record(supervisor: Any, provider: str, item: dict[str, Any], candidate: dict[str, Any] | None) -> dict[str, Any]:
    """Configured and provider-reported model are separate facts; unknown is never recorded as verified."""
    configured = supervisor.config["session_start"]["model_profiles"][provider][item["profile"]].get("model")
    reported = candidate.get("model") if isinstance(candidate, dict) and isinstance(candidate.get("model"), str) and candidate.get("model") else None
    return {"configured_model": configured, "effective_model": reported, "effective_model_status": "reported" if reported is not None else "unknown"}


def _settle_identity(supervisor: Any, provider: str, item: dict[str, Any], record: dict[str, Any], path: Path) -> None:
    """Separate facts: process-interactive (uniquely ready in its pane), native-identity-verified (exact id),
    initialization-completed (identity reported natively, or after the journaled bootstrap). Task delivery is
    a later, separate acknowledgement and is never implied here."""
    if item.get("status") in BOOTSTRAP_STATES:
        _bootstrap(supervisor, provider, item, record, path)
        return
    try:
        identity = _verify_started_identity(supervisor, provider, item, supervisor.herdr.list_agents())
    except IdentityPending as pending:
        if item.get("process_interactive") is not True:
            item["process_interactive"] = True
            atomic_write_json(path, record)
        if supervisor.config["session_start"].get("native_identity_bootstrap") is not True:
            raise SupervisorError(f"{pending}. The installed {provider} reported no native identity before a first turn and the identity bootstrap is disabled (session_start.native_identity_bootstrap); nothing was sent and ownership was not changed") from pending
        _bootstrap(supervisor, provider, item, record, path)
        return
    _launch_process_unchanged(supervisor, provider, item, "ownership was not changed")
    item.update({"status": "IDENTITY_VERIFIED", "session_id": identity, "process_interactive": True, "initialization": "native-reported",
                 **_model_record(supervisor, provider, item, _live_candidate(supervisor, provider, item))})
    atomic_write_json(path, record)


def _sleep(supervisor: Any, seconds: float) -> None:
    sleeper = getattr(supervisor, "sleeper", None)
    if callable(sleeper):
        sleeper(seconds)


def _idle_shell(supervisor: Any, provider: str, item: dict[str, Any], note: str) -> dict[str, Any]:
    """The newly created, preparation-owned pane must be an empty interactive shell: no agent, and Herdr's supported
    `pane process-info` shows the shell itself as the only foreground process in the approved working directory."""
    herdr, pane = supervisor.herdr, item["pane_id"]
    if not herdr.pane_available(pane):
        raise SupervisorError(f"{provider} launch pane {pane} is not an empty shell pane; {note}")
    probe = getattr(herdr, "process_info", None)
    if not callable(probe):
        raise SupervisorError(f"{provider} launch needs Herdr's pane process contract to prove the pane is an idle shell; {note}")
    try:
        info = probe(pane)
    except Exception as error:  # noqa: BLE001
        raise SupervisorError(f"{provider} launch could not read pane {pane}'s process ({getattr(error, 'code', None) or type(error).__name__}); {note}") from error
    processes = info.get("foreground_processes") if isinstance(info, dict) else None
    shell_pid = info.get("shell_pid") if isinstance(info, dict) else None
    if (type(shell_pid) is not int or not isinstance(processes, list) or len(processes) != 1 or not isinstance(processes[0], dict) or processes[0].get("pid") != shell_pid
            or not isinstance(processes[0].get("cwd"), str) or os.path.realpath(processes[0]["cwd"]) != os.path.realpath(str(supervisor.config["project_root"]))):
        raise SupervisorError(f"{provider} launch pane {pane} is not an idle interactive shell in the approved directory (busy, ambiguous or different); {note}")
    return {"shell_pid": shell_pid, "cwd": os.path.realpath(processes[0]["cwd"]), "name": str(processes[0].get("name"))}


_TOKEN_KEY = object()


class SubmissionToken:
    """Single-use, in-memory eligibility to queue the explicit launch command in a guarded pane.

    Issued ONLY by the invocation that performed the distinct `--no-focus` split (`prepare_sessions` below); bound to the
    preparation, the pane, the original shell PID and that invocation; valid for a fixed window from the split on an
    injectable monotonic clock. It cannot be constructed elsewhere, serialized, copied or reconstituted from the journal, a
    status read, an existing shell or an operator-supplied pane. It is NOT evidence about the line contents."""

    __slots__ = ("_preparation_id", "_pane_id", "_invocation", "_issued_at", "_shell_pid", "_consumed")

    def __init__(self, key: object, preparation_id: str, pane_id: str, invocation: object, issued_at: float) -> None:
        if key is not _TOKEN_KEY:
            raise TypeError("a submission token can only be issued by the split that created the pane")
        self._preparation_id, self._pane_id, self._invocation, self._issued_at = preparation_id, pane_id, invocation, float(issued_at)
        self._shell_pid: int | None = None
        self._consumed = False

    def __reduce_ex__(self, protocol: Any) -> Any:
        raise TypeError("submission tokens are in-memory only")

    def __copy__(self) -> Any:
        raise TypeError("submission tokens cannot be copied")

    def __deepcopy__(self, memo: Any) -> Any:
        raise TypeError("submission tokens cannot be copied")

    def __repr__(self) -> str:
        return "<SubmissionToken>"

    @property
    def consumed(self) -> bool:
        return self._consumed

    def bind_shell(self, pid: Any) -> bool:
        """Bind the ORIGINAL shell PID once; a later different value is refused."""
        if type(pid) is not int:
            return False
        if self._shell_pid is None:
            self._shell_pid = pid
        return self._shell_pid == pid

    def problem(self, now: float, preparation_id: str, pane_id: str, invocation: object, shell_pid: Any) -> str | None:
        if self._consumed:
            return "the single-use submission eligibility was already consumed"
        if self._preparation_id != preparation_id or self._pane_id != pane_id or self._invocation is not invocation:
            return "the submission eligibility belongs to another preparation, pane or invocation"
        if self._shell_pid is None or type(shell_pid) is not int or self._shell_pid != shell_pid:
            return "the pane's original shell is not the one the eligibility was bound to"
        elapsed = now - self._issued_at
        if elapsed < 0:
            return "the monotonic clock went backwards"
        if elapsed >= GUARDED_WINDOW:
            return f"the {int(GUARDED_WINDOW)}-second guarded window from the split has expired"
        return None

    def deadline_problem(self, now: float) -> str | None:
        """Expiry only (the token may already be consumed): used after the durable marker, immediately before the call."""
        elapsed = now - self._issued_at
        if elapsed < 0:
            return "the monotonic clock went backwards"
        if elapsed >= GUARDED_WINDOW:
            return f"the {int(GUARDED_WINDOW)}-second guarded window from the split has expired"
        return None

    def consume(self) -> None:
        self._consumed = True


def _now(supervisor: Any) -> float:
    reader = getattr(supervisor, "monotonic_now", None)
    return float(reader()) if callable(reader) else time.monotonic()


def _guard_hold(provider: str, item: dict[str, Any], record: dict[str, Any], detail: str) -> SupervisorError:
    return SupervisorError(f"{provider} guarded launch for preparation {record['preparation_id']}: {detail}; nothing was submitted, pane {item['pane_id']} is preserved and no replacement was created. Inspect the pane, or run `herdr-supervisor abandon-session-start --preparation-id {record['preparation_id']}`")


def _guard_require_token(provider: str, item: dict[str, Any], record: dict[str, Any], token: Any) -> SubmissionToken:
    if not isinstance(token, SubmissionToken):
        raise _guard_hold(provider, item, record, f"submission is eligible only inside the invocation that split the pane and only within {int(GUARDED_WINDOW)} seconds; this call does not hold that single-use eligibility (restart, later call or already stopped before submission)")
    return token


def _guard_evidence(supervisor: Any, provider: str, item: dict[str, Any], record: dict[str, Any], token: SubmissionToken, invocation: object, shell: dict[str, Any], *, first: bool) -> None:
    """Observable pre-input checks of the guarded profile. They narrow the exposure; they do not prove an empty line."""
    pane = item["pane_id"]
    fetch = getattr(supervisor.herdr, "pane_metadata", None)
    if not callable(fetch):
        raise _guard_hold(provider, item, record, "the supported exact pane metadata contract is absent")
    try:
        meta = fetch(pane)
    except Exception as error:  # noqa: BLE001
        raise _guard_hold(provider, item, record, f"pane metadata could not be read ({getattr(error, 'code', None) or type(error).__name__})") from error
    if (not isinstance(meta, dict) or meta.get("pane_id") != pane or type(meta.get("focused")) is not bool or not (meta.get("agent") is None or isinstance(meta.get("agent"), str))
            or not (meta.get("cwd") is None or isinstance(meta.get("cwd"), str))):
        raise _guard_hold(provider, item, record, "pane metadata is missing, foreign or malformed")
    if meta["focused"] is not False:
        raise _guard_hold(provider, item, record, "the pane is focused (possible operator interference)")
    if meta.get("agent") not in (None, ""):
        raise _guard_hold(provider, item, record, "an agent is already detected in the pane")
    if not isinstance(meta.get("cwd"), str) or os.path.realpath(meta["cwd"]) != os.path.realpath(str(supervisor.config["project_root"])):
        raise _guard_hold(provider, item, record, "the pane's working directory is not the approved one")
    if first and not token.bind_shell(shell.get("shell_pid")):
        raise _guard_hold(provider, item, record, "the pane's shell could not be bound")
    problem = token.problem(_now(supervisor), record["preparation_id"], pane, invocation, shell.get("shell_pid"))
    if problem:
        raise _guard_hold(provider, item, record, problem)


def _guard_commit(supervisor: Any, provider: str, item: dict[str, Any], record: dict[str, Any], token: SubmissionToken, invocation: object, shell: dict[str, Any]) -> None:
    """The last gate: the deadline is evaluated AFTER every other check (a check that crosses it prevents submission), then
    the local eligibility is consumed before the durable attempt marker and the single submission."""
    problem = token.problem(_now(supervisor), record["preparation_id"], item["pane_id"], invocation, shell.get("shell_pid"))
    if problem:
        raise _guard_hold(provider, item, record, problem)
    token.consume()


def _assert_recipe_authority(supervisor: Any, provider: str, item: dict[str, Any], note: str) -> None:
    """The journaled recipe is untrusted: before ANY probe, observation, rename or submission it must equal what the
    unchanged helper context, the exact recorded thread and the supported env resolver permit."""
    try:
        context = supervisor.helper_context(_item_cwd(item))
    except SupervisorError as error:
        raise SupervisorError(f"{provider} launch authority unavailable ({error}); {note}") from error
    problem = hctx.recipe_authority_problem(item["launch"]["recipe"], context, item.get("thread_id"), supervisor.launch_env_utility(), approval_mode(supervisor.config, provider),
                                            model=(item.get("launch_request") or {}).get("model"))
    if problem:
        raise SupervisorError(f"{provider} launch {problem}; {note}")


def _assert_launch_binding(supervisor: Any, provider: str, item: dict[str, Any], note: str) -> None:
    """Helper context and the observed launch process must still be the journaled ones before anything mutates a pane/agent."""
    _helper_context_unchanged(supervisor, provider, item, note)
    _launch_process_unchanged(supervisor, provider, item, note)


def _empty_input_proof(supervisor: Any, provider: str, item: dict[str, Any], note: str) -> None:
    """Plan: refuse pre-existing input. Only a supported Herdr contract that proves the command line is empty counts; none
    exists in Herdr 0.9.5, so the route stays unavailable. A sole foreground shell is NOT such proof."""
    proof = getattr(supervisor.herdr, "pane_input_empty", None)
    if not callable(proof) or proof(item["pane_id"]) is not True:
        raise SupervisorError(f"{provider} launch pane {item['pane_id']} cannot be proven to have an empty command line: {hctx.NO_EMPTY_INPUT_CONTRACT}; {note}")


def _launch_hold(provider: str, item: dict[str, Any], record: dict[str, Any], detail: str) -> SupervisorError:
    return SupervisorError(f"{provider} explicit launch for preparation {record['preparation_id']}: {detail}; nothing was resubmitted and no second pane, thread or launch was created. Inspect pane {item['pane_id']}, retry the same task start to reconcile it, or run `herdr-supervisor abandon-session-start --preparation-id {record['preparation_id']}`")


def _observe_launch_process(supervisor: Any, provider: str, item: dict[str, Any], record: dict[str, Any]) -> dict[str, Any]:
    """Bounded deterministic observation of the SAME pane after admission. Only the expected provider process (the pinned
    executable, the exact thread, `--no-daemon`, the approved directory, the same shell) advances; an idle shell keeps
    waiting; anything else holds. Never submits anything."""
    launch, probe = item["launch"], getattr(supervisor.herdr, "process_info", None)
    if not callable(probe):
        raise _launch_hold(provider, item, record, "Herdr's pane process contract is unavailable")
    note = "it was not resubmitted"
    for attempt in range(LAUNCH_OBSERVE_ATTEMPTS):
        try:
            info = probe(item["pane_id"])
        except Exception as error:  # noqa: BLE001
            raise _launch_hold(provider, item, record, f"the pane's process could not be read ({getattr(error, 'code', None) or type(error).__name__})") from error
        processes = info.get("foreground_processes") if isinstance(info, dict) else None
        if info.get("shell_pid") != launch["shell"]["shell_pid"]:
            raise _launch_hold(provider, item, record, "the pane's shell was replaced")
        if isinstance(processes, list) and len(processes) == 1 and isinstance(processes[0], dict) and processes[0].get("pid") == info.get("shell_pid"):
            if attempt + 1 < LAUNCH_OBSERVE_ATTEMPTS:
                _sleep(supervisor, LAUNCH_OBSERVE_INTERVAL)
            continue  # admitted but not executing yet (or not executed): keep observing, never resubmit
        try:
            process = _owned_process(supervisor, provider, item, note)
        except SupervisorError as error:
            raise _launch_hold(provider, item, record, f"the pane runs a different or ambiguous process ({error})") from error
        argv0 = process["argv"][0] if process["argv"] else ""
        if os.path.realpath(argv0) != launch["recipe"]["codex"]["path"] or process["shell_pid"] != launch["shell"]["shell_pid"]:
            raise _launch_hold(provider, item, record, "the pane's provider process is not the pinned executable under the journaled shell")
        return process
    raise _launch_hold(provider, item, record, "the launch was admitted but the expected process was not observed in the pane")


class ForegroundProcessUnsettled(SupervisorError):
    """A read-only snapshot cannot yet prove one foreground process; never authority for mutation."""


def _establish_alias(supervisor: Any, provider: str, item: dict[str, Any], record: dict[str, Any], path: Path) -> None:
    """Name the detected agent of THIS pane (exact pane target) with the unique preparation alias and verify it. An alias
    problem is reconciled against the same recorded pane/process and never repaired by relaunching."""
    herdr, alias, pane = supervisor.herdr, item["agent_name"], item["pane_id"]
    renamed = False
    detected = False
    unsettled = False
    for attempt in range(LAUNCH_OBSERVE_ATTEMPTS):
        _deadline_guard(supervisor, record["preparation_id"], provider, "reconfirming the bound process before naming the agent")
        # Every iteration — resumed recovery, repeated observation after a wait, an already-matching alias — first proves the
        # bound process and context are unchanged, so a replacement is never renamed and never accepted.
        try:
            _assert_launch_binding(supervisor, provider, item, "no alias was changed")
        except ForegroundProcessUnsettled:
            unsettled = True
            if attempt + 1 < LAUNCH_OBSERVE_ATTEMPTS:
                _sleep(supervisor, LAUNCH_OBSERVE_INTERVAL)
            continue  # read-only observation of this same bound launch; no rename, launch or bootstrap
        unsettled = False
        live = herdr.list_agents()
        named = [agent for agent in live if agent.get("name") == alias]
        if named:
            if len(named) == 1 and named[0].get("agent") == provider and named[0].get("pane_id") == pane:
                break
            raise _launch_hold(provider, item, record, f"the alias {alias} is held by another or an ambiguous agent")
        on_pane = [agent for agent in live if agent.get("pane_id") == pane and agent.get("agent") == provider]
        if len(on_pane) > 1:
            raise _launch_hold(provider, item, record, "Herdr reports more than one agent in the pane")
        detected = detected or bool(on_pane)
        if len(on_pane) == 1 and not renamed:
            rename = getattr(herdr, "rename_agent", None)
            if not callable(rename):
                raise _launch_hold(provider, item, record, "the adapter cannot name an agent by its exact pane")
            try:
                _assert_launch_binding(supervisor, provider, item, "no alias was changed")  # again immediately before the mutation
            except ForegroundProcessUnsettled:
                unsettled = True
                if attempt + 1 < LAUNCH_OBSERVE_ATTEMPTS:
                    _sleep(supervisor, LAUNCH_OBSERVE_INTERVAL)
                continue  # the alias is still untouched; the SAME bounded observation budget applies
            _deadline_guard(supervisor, record["preparation_id"], provider, "naming the agent")
            try:
                rename(pane, alias)
            except Exception as error:  # noqa: BLE001
                raise _launch_hold(provider, item, record, f"naming the agent failed ({getattr(error, 'code', None) or type(error).__name__})") from error
            renamed = True
            continue
        _sleep(supervisor, LAUNCH_OBSERVE_INTERVAL)
    else:
        if unsettled:
            raise _launch_hold(provider, item, record, "the already-bound foreground process remained ambiguous during bounded observation; no further alias mutation was attempted")
        hold = _launch_hold(provider, item, record, "the alias could not be verified in the same pane")
        if not detected and not renamed:
            # The bound provider process is alive but Herdr never detected an agent in the pane within the bounded observation: a SIGNAL (e.g. a startup prompt) only.
            raise StartupNotReady(str(hold), site="launch_alias_unverified", pane_id=pane, provider=provider) from hold
        raise hold
    item["status"] = "AGENT_STARTING"
    atomic_write_json(path, record)


def _recipe_revalidation(supervisor: Any, item: dict[str, Any]) -> str | None:
    """Authority and byte-identity drift check of the journaled recipe in the form this item recorded."""
    recipe = item["launch"]["recipe"]
    if _direct(item):
        return supervisor.revalidate_launch_recipe(recipe, None, _item_cwd(item), model=item["launch_request"]["model"])
    return supervisor.revalidate_launch_recipe(recipe, item["thread_id"], item["thread_payload"]["cwd"])


def _launch_explicit(supervisor: Any, provider: str, item: dict[str, Any], record: dict[str, Any], path: Path,
                     token: Any = None, invocation: object = None) -> None:
    """At-most-once explicit launch: recipe + attempt journaled before the single `pane run`; reconcile by observing the
    same pane/process only. Ends at AGENT_STARTING (process bound, alias verified), where the reviewed identity,
    bootstrap and ownership machinery takes over."""
    herdr, alias = supervisor.herdr, f"{provider}-run-{record['preparation_id'][:8]}"
    guarded = item.get("input_policy", STRICT_POLICY) == GUARDED_POLICY
    if guarded and item["status"] in ("FRESH_PANE_CREATED", "LAUNCH_INTENT"):
        _guard_require_token(provider, item, record, token)  # before any probe: no eligibility, no further work
    if item["status"] != "FRESH_PANE_CREATED":
        _assert_recipe_authority(supervisor, provider, item, "nothing was submitted" if item["status"] == "LAUNCH_INTENT" else "it was not resubmitted")
    if item["status"] == "FRESH_PANE_CREATED":
        recipe = supervisor.direct_launch_recipe(item["launch_request"]["model"]) if _direct(item) else supervisor.launch_recipe(item["thread_id"])
        if recipe["cwd"] != _item_cwd(item):
            raise SupervisorError(f"{provider} launch recipe working directory differs from the {'launch' if _direct(item) else 'thread'} request; nothing was submitted")
        shell = _idle_shell(supervisor, provider, item, "nothing was submitted")
        if guarded:
            _guard_evidence(supervisor, provider, item, record, token, invocation, shell, first=True)
        else:
            _empty_input_proof(supervisor, provider, item, "nothing was submitted")
        if any(agent.get("name") == alias for agent in herdr.list_agents()):
            raise SupervisorError(f"fresh {provider} agent name already exists; nothing was submitted")
        item.update({"status": "LAUNCH_INTENT", "agent_name": alias, "launch": {"recipe": recipe, "pane_id": item["pane_id"], "shell": shell}})
        atomic_write_json(path, record)
    launch = item["launch"]
    if item["status"] == "LAUNCH_INTENT":
        # Revalidate everything observable immediately before the one submission.
        problem = _recipe_revalidation(supervisor, item)
        if problem:
            raise SupervisorError(f"{provider} launch not submitted: {problem}")
        _helper_context_unchanged(supervisor, provider, item, "nothing was submitted")
        current_shell = _idle_shell(supervisor, provider, item, "nothing was submitted")
        if current_shell != launch["shell"]:
            raise SupervisorError(f"{provider} launch pane changed since the intent was journaled; nothing was submitted")
        if guarded:
            _guard_evidence(supervisor, provider, item, record, token, invocation, current_shell, first=False)
        else:
            _empty_input_proof(supervisor, provider, item, "nothing was submitted")
        run = getattr(herdr, "pane_run", None)
        if not callable(run):
            raise SupervisorError(f"{provider} launch needs the pane run command; nothing was submitted")
        _deadline_guard(supervisor, record["preparation_id"], provider, "submitting its launch")
        if guarded:
            _guard_commit(supervisor, provider, item, record, token, invocation, current_shell)
        item["status"] = "LAUNCH_SUBMITTED"  # the attempt marker: from here the outcome is unknown until observed
        atomic_write_json(path, record)
        if guarded:
            # Persistence can block: re-evaluate the window immediately before the one call. The marker stays (never rolled back) and
            # the eligibility is already consumed, so an expiry here holds with zero submission and nothing can rearm or resend.
            expired = token.deadline_problem(_now(supervisor))
            if expired:
                raise _launch_hold(provider, item, record, f"{expired} after the attempt marker was persisted and before pane run was called; this call queued no command")
        # Persistence can block: the preparation/absolute bound is re-read immediately before the one external call. The marker stays consumed (the guard makes the preparation inert), so
        # nothing can rearm or resend; a persisted intent alone is not a submitted operation.
        _deadline_guard(supervisor, record["preparation_id"], provider, "submitting its launch")
        try:
            run(item["pane_id"], launch["recipe"]["command"])
        except Exception as error:  # noqa: BLE001 - no retained contract proves "no input was queued"
            raise _launch_hold(provider, item, record, f"submission outcome is uncertain ({getattr(error, 'code', None) or type(error).__name__})") from error
        item["status"] = "LAUNCH_ADMITTED"  # admission only: not execution, not readiness, not identity
        atomic_write_json(path, record)
    if item["status"] in ("LAUNCH_SUBMITTED", "LAUNCH_ADMITTED"):
        launch["process"] = _observe_launch_process(supervisor, provider, item, record)
        item["status"] = "PROCESS_BOUND"
        atomic_write_json(path, record)
    if item["status"] == "PROCESS_BOUND":
        _establish_alias(supervisor, provider, item, record, path)


def _launch_process_unchanged(supervisor: Any, provider: str, item: dict[str, Any], note: str) -> None:
    """For an explicit launch the observed process binding must still hold before any identity is adopted."""
    launch = item.get("launch")
    if isinstance(launch, dict) and _owned_process(supervisor, provider, item, note) != launch.get("process"):
        raise SupervisorError(f"{provider} process changed since the launch was bound; {note}")


def _owned_process(supervisor: Any, provider: str, item: dict[str, Any], note: str = "nothing was sent") -> dict[str, Any]:
    """The positively established preparation-owned process of the prepared pane, from Herdr's supported read-only
    `pane process-info` contract: exactly one foreground process, the provider, launched with exactly the arguments
    this preparation journaled, in the approved working directory. Alias plus pane alone is not this proof."""
    probe = getattr(supervisor.herdr, "process_info", None)
    if not callable(probe):
        raise SupervisorError(f"{provider} target check needs Herdr's pane process contract to prove the process is preparation-owned; {note}")
    try:
        info = probe(item["pane_id"])
    except Exception as error:  # noqa: BLE001 - any adapter failure is missing proof
        raise SupervisorError(f"{provider} target check could not read the prepared pane's process ({getattr(error, 'code', None) or type(error).__name__}); {note}") from error
    processes = info.get("foreground_processes") if isinstance(info, dict) else None
    if isinstance(processes, list) and len(processes) != 1:
        raise ForegroundProcessUnsettled(f"{provider} target pane does not have exactly one foreground process; {note}")
    if not isinstance(processes, list) or not isinstance(processes[0], dict):
        raise SupervisorError(f"{provider} target pane has a malformed foreground process report; {note}")
    process = processes[0]
    if _direct(item):
        model = item["launch_request"]["model"]
        expected_args = [*([] if model is None else ["--model", model]), *approval_args(supervisor.config, provider), "--no-daemon"]
    else:
        expected_args = (["resume", item.get("thread_id"), *approval_args(supervisor.config, provider), "--no-daemon"] if provider in THREAD_PROVIDERS
                         else [*model_args(supervisor.config, provider, item["profile"]), *approval_args(supervisor.config, provider)])
    argv, cwd = process.get("argv"), process.get("cwd")
    if (process.get("name") != provider or not isinstance(argv, list) or not all(isinstance(a, str) for a in argv) or argv[1:] != expected_args
            or not isinstance(cwd, str) or os.path.realpath(cwd) != os.path.realpath(str(supervisor.config["project_root"]))
            or type(process.get("pid")) is not int or type(info.get("shell_pid")) is not int):
        raise SupervisorError(f"{provider} target process is not the journaled launch of this preparation; {note}")
    return {"pid": process["pid"], "shell_pid": info["shell_pid"], "argv": list(argv), "cwd": os.path.realpath(cwd)}


def _helper_context_unchanged(supervisor: Any, provider: str, item: dict[str, Any], note: str = "nothing was sent") -> None:
    recorded = item.get("helper_context_fingerprint")
    context = getattr(supervisor, "helper_context", None)
    if recorded is None or not callable(context):
        return
    current = hctx.fingerprint(context(_item_cwd(item)))
    if current != recorded:
        raise SupervisorError(f"{provider} bootstrap context changed since the {'launch' if _direct(item) else 'thread'} was requested (recorded {recorded[:12]}, now {current[:12]}); {note}")


def _assert_bootstrap_binding(supervisor: Any, provider: str, item: dict[str, Any], boot: dict[str, Any], note: str) -> None:
    """The recorded preparation-owned binding (helper context + pane process) must still hold before this
    preparation may either send a bootstrap OR adopt a native identity that appeared during the bootstrap window.
    A replaced process or changed context is refused with old owners preserved; the same process reporting its
    identity late still passes."""
    _helper_context_unchanged(supervisor, provider, item, note)
    if _owned_process(supervisor, provider, item, note) != boot.get("process"):
        raise SupervisorError(f"{provider} bootstrap target process changed since the intent was journaled; {note}")


def _bootstrap(supervisor: Any, provider: str, item: dict[str, Any], record: dict[str, Any], path: Path) -> None:
    """Offline-tested minimal initialization turn for the newly created, preparation-owned pane only.

    BOOTSTRAP_INTENT (persisted before anything is sent) -> BOOTSTRAP_SUBMITTED (persisted immediately before the
    single send) -> BOOTSTRAP_ACKNOWLEDGED (Herdr observed the turn start) -> IDENTITY_VERIFIED. A submitted but
    unacknowledged turn is uncertain: it is reconciled by observation and never resent. It is a paid model turn and
    only runs when session_start.native_identity_bootstrap is explicitly enabled."""
    herdr = supervisor.herdr
    phase = item.get("status")
    if phase == "AGENT_STARTING":
        nonce = uuid.uuid4().hex
        text = bootstrap_text(nonce)
        _helper_context_unchanged(supervisor, provider, item)
        _launch_process_unchanged(supervisor, provider, item, "nothing was sent")
        item["bootstrap"] = {"nonce": nonce, "pane_id": item["pane_id"], "agent_name": item["agent_name"], "provider": provider,
                             "expected_identity": item.get("thread_id"), "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
                             "process": _owned_process(supervisor, provider, item)}
        item["status"] = phase = "BOOTSTRAP_INTENT"
        atomic_write_json(path, record)
    boot = item.get("bootstrap")
    if not isinstance(boot, dict) or boot.get("pane_id") != item.get("pane_id") or boot.get("agent_name") != item.get("agent_name"):
        raise SupervisorError(f"{provider} bootstrap journal does not match its prepared pane")
    candidate = _live_candidate(supervisor, provider, item)
    if candidate is None or (phase == "BOOTSTRAP_INTENT" and candidate.get("agent_status") not in READY_STATES):
        sent = "nothing was sent" if phase == "BOOTSTRAP_INTENT" else "it was not resent"
        if phase == "BOOTSTRAP_INTENT" and candidate is not None and candidate.get("agent_status") in STARTUP_BLOCKED_STATUSES:
            raise StartupNotReady(f"{provider} bootstrap target is not uniquely ready in its prepared pane; {sent}", site="bootstrap_target_not_ready", pane_id=item.get("pane_id"), provider=provider)
        raise SupervisorError(f"{provider} bootstrap target is not uniquely ready in its prepared pane; {sent}")
    if phase == "BOOTSTRAP_INTENT":
        # Everything observable is revalidated immediately before input, including when this is a recovered intent.
        live = herdr.list_agents()
        if sum(1 for agent in live if agent.get("name") == item["agent_name"]) != 1:
            raise SupervisorError(f"{provider} bootstrap alias {item['agent_name']} is ambiguous or gone in Herdr; nothing was sent")
        _assert_bootstrap_binding(supervisor, provider, item, boot, "nothing was sent")
        try:
            identity = _verify_started_identity(supervisor, provider, item, live)
        except IdentityPending:
            pass  # still no native identity: the only case a bootstrap may address
        else:
            boot["outcome"] = "not-needed"  # the exact intended identity is already ready: reconcile without a turn
            item.update({"status": "IDENTITY_VERIFIED", "session_id": identity, "process_interactive": True, "initialization": "native-reported",
                         **_model_record(supervisor, provider, item, _live_candidate(supervisor, provider, item))})
            atomic_write_json(path, record)
            return
        model = _model_record(supervisor, provider, item, candidate)
        if model["configured_model"] is not None and model["effective_model"] is not None and model["effective_model"] != model["configured_model"]:
            raise SupervisorError(f"{provider} bootstrap target reported a different model than configured; nothing was sent")
        ack_probe = getattr(herdr, "prompt_ack_supported", None)
        if not callable(ack_probe) or ack_probe() is not True:
            raise SupervisorError(f"{provider} bootstrap needs the lifecycle acknowledgement contract; nothing was sent")
        _deadline_guard(supervisor, record["preparation_id"], provider, "the identity bootstrap")
        item["status"] = "BOOTSTRAP_SUBMITTED"
        atomic_write_json(path, record)
        _deadline_guard(supervisor, record["preparation_id"], provider, "the identity bootstrap")  # re-read after the durable marker, immediately before the send; the marker stays consumed
        try:
            herdr.prompt_ack(item["agent_name"], bootstrap_text(boot["nonce"]), timeout_ms=BOOTSTRAP_ACK_MS)
        except Exception as error:  # noqa: BLE001 - classify by Herdr's documented codes only
            if getattr(error, "code", None) in ("agent_blocked", "agent_not_found"):
                item["status"] = "BOOTSTRAP_INTENT"  # rejected before any input reached the agent
                atomic_write_json(path, record)
                raise SupervisorError(f"{provider} bootstrap was rejected before input; nothing was sent") from error
            raise SupervisorError(f"{provider} bootstrap delivery is uncertain ({getattr(error, 'code', None) or type(error).__name__}); it was not resent. Inspect pane {item['pane_id']} or run `herdr-supervisor abandon-session-start --preparation-id {record['preparation_id']}`") from error
        item["status"] = phase = "BOOTSTRAP_ACKNOWLEDGED"
        atomic_write_json(path, record)
    elif phase == "BOOTSTRAP_SUBMITTED":
        # A crash or timeout left delivery unknown: observe only. Completion shows as the reported identity.
        _assert_bootstrap_binding(supervisor, provider, item, boot, "it was not resent")
        try:
            identity = _verify_started_identity(supervisor, provider, item, herdr.list_agents())
        except IdentityPending as pending:
            raise SupervisorError(f"{provider} bootstrap for preparation {record['preparation_id']} was submitted but not acknowledged and no identity is reported; it was not resent. Inspect pane {item['pane_id']} or run `herdr-supervisor abandon-session-start --preparation-id {record['preparation_id']}`") from pending
        boot["outcome"] = "observed-complete"
        item.update({"status": "IDENTITY_VERIFIED", "session_id": identity, "process_interactive": True, "initialization": "bootstrap-completed",
                     **_model_record(supervisor, provider, item, _live_candidate(supervisor, provider, item))})
        atomic_write_json(path, record)
        return
    herdr.wait(item["agent_name"], timeout_ms=BOOTSTRAP_WAIT_MS)
    _assert_bootstrap_binding(supervisor, provider, item, boot, "ownership was not changed and nothing was resent")
    try:
        identity = _verify_started_identity(supervisor, provider, item, herdr.list_agents())
    except IdentityPending as pending:
        raise SupervisorError(f"{provider} bootstrap completed but no native identity is reported; ownership was not changed and nothing was resent") from pending
    if boot.get("expected_identity") is not None and identity != boot["expected_identity"]:
        raise SupervisorError(f"{provider} bootstrap reported native session {identity[:8]} instead of {str(boot['expected_identity'])[:8]}; ownership was not changed")
    boot["outcome"] = "completed"
    item.update({"status": "IDENTITY_VERIFIED", "session_id": identity, "process_interactive": True, "initialization": "bootstrap-completed",
                 **_model_record(supervisor, provider, item, _live_candidate(supervisor, provider, item))})
    atomic_write_json(path, record)


def _apply_label(supervisor: Any, provider: str, item: dict[str, Any], record: dict[str, Any], path: Path) -> None:
    """Presentation only: rename exactly the newly created pane through the supported API, verify the result and
    persist the outcome. A failure or unverifiable result is recorded explicitly (never as success) and never
    blocks or repairs ownership. Reconciliation targets the same recorded pane; an old pane is never touched."""
    herdr = supervisor.herdr
    target = pane_label(provider, record["preparation_id"])
    state = item.get("label_state")
    if isinstance(state, dict) and state.get("status") in ("NAMED", "NAME_FAILED", "NAME_UNVERIFIED", "NAME_MISMATCH", "NAME_UNSUPPORTED"):
        return
    _deadline_guard(supervisor, record["preparation_id"], provider, "labelling the pane")
    rename, read = getattr(herdr, "rename_pane", None), getattr(herdr, "pane_label", None)
    pane = item["pane_id"]
    if not callable(rename):
        item["label_state"] = {"status": "NAME_UNSUPPORTED", "target": target, "pane_id": pane}
        atomic_write_json(path, record)
        return
    if isinstance(state, dict) and state.get("status") == "NAMING_INTENT" and callable(read):
        # Late acknowledgement: observe the exact pane before deciding whether the single rename already landed.
        try:
            if read(pane) == target:
                item["label_state"] = {"status": "NAMED", "target": target, "pane_id": pane}
                atomic_write_json(path, record)
                return
        except Exception:  # noqa: BLE001 - observation failure falls through to one idempotent rename
            pass
    item["label_state"] = {"status": "NAMING_INTENT", "target": target, "pane_id": pane}
    atomic_write_json(path, record)
    _deadline_guard(supervisor, record["preparation_id"], provider, "labelling the pane")  # re-read after the intent write, immediately before the rename
    status = "NAME_UNVERIFIED"
    try:
        rename(pane, target)
        if callable(read):
            observed = read(pane)
            status = "NAMED" if observed == target else ("NAME_UNVERIFIED" if observed is None else "NAME_MISMATCH")
    except Exception as error:  # noqa: BLE001
        status = "NAME_FAILED"
        item["label_state"] = {"status": status, "target": target, "pane_id": pane, "error": str(getattr(error, "code", None) or type(error).__name__)[:60]}
        atomic_write_json(path, record)
        return
    item["label_state"] = {"status": status, "target": target, "pane_id": pane}
    atomic_write_json(path, record)


def _verify_owner_set(live: list[dict[str, Any]], owners: dict[str, dict[str, str]], *, label: str) -> None:
    for provider in PROVIDERS:
        expected=owners[provider]
        match=match_exact_session(live,provider,expected["session_id"])
        if not match.unique or match.record is None or match.record.get("pane_id") != expected["pane_id"]:
            raise SupervisorError(f"cannot verify {label} {provider} owner in its exact pane")


# ----- human-handled startup waits (preparation context). The wait is a durable record (herdr_startup); the journal above is NOT changed: its phase stays
# exactly where the signal found it, so a continuation re-enters the same at-most-once machinery and can never launch, bootstrap or deliver more than the journal allows.


def _provider_command(supervisor: Any, provider: str, item: dict[str, Any]) -> str:
    launch = item.get("launch")
    if isinstance(launch, dict) and isinstance(launch.get("recipe"), dict):
        return str(launch["recipe"]["codex"]["path"])
    return str(supervisor.config["session_start"]["provider_commands"][provider])


def _executable_now(supervisor: Any, command: str) -> dict[str, Any]:
    return getattr(supervisor, "startup_executable_record", hst.executable_record)(command)


def _wait_process(supervisor: Any, provider: str, item: dict[str, Any], note: str = "nothing was sent") -> dict[str, Any]:
    process = _owned_process(supervisor, provider, item, note)
    ticks = getattr(supervisor, "startup_process_ticks", hst.process_start_ticks)
    return {**process, "start_ticks": ticks(process["pid"])}


def _wait_binding(item: dict[str, Any]) -> dict[str, Any]:
    launch, boot = item.get("launch"), item.get("bootstrap")
    return {"item_status": item.get("status"), "thread_id": item.get("thread_id"), "agent_name": item.get("agent_name"),
            "launch_digest": (launch.get("recipe") or {}).get("digest") if isinstance(launch, dict) else None,
            "bootstrap_nonce": boot.get("nonce") if isinstance(boot, dict) else None}


def _announce_wait(supervisor: Any, wait: dict[str, Any], event_type: str, **extra: Any) -> None:
    hst.announce_wait(supervisor.paths, getattr(supervisor, "emit_event", None), supervisor.clock(), wait, event_type, **extra)


def _open_startup_wait(supervisor: Any, provider: str, item: dict[str, Any], record: dict[str, Any], path: Path, signal: StartupNotReady) -> Exception:
    """The exception to raise for a not-ready signal: a StartupWaitRequired with a durable record, or the unchanged signal when no wait may open
    (uncertain submission phases, an unprovable process, a recognized trust dialog, an exhausted budget). Nothing is sent, pressed or changed."""
    phase = item.get("status")
    boot = item.get("bootstrap")
    if phase not in STARTUP_WAIT_ELIGIBLE or (phase == "BOOTSTRAP_INTENT" and isinstance(boot, dict) and boot.get("outcome") is not None):
        return signal
    note = "no startup wait was opened"
    launch = item.get("launch")
    bound = launch.get("process") if isinstance(launch, dict) else None
    # A wait cannot adopt a different process. Only an ALREADY bound explicit launch may settle a transient read.
    for attempt in range(LAUNCH_OBSERVE_ATTEMPTS if isinstance(bound, dict) else 1):
        try:
            if isinstance(bound, dict):
                _deadline_guard(supervisor, record["preparation_id"], provider, "capturing the bound startup-wait process")
            process = _wait_process(supervisor, provider, item)
            if isinstance(bound, dict) and {key: process.get(key) for key in bound} != bound:
                raise SupervisorError(f"{provider} process changed since the launch was bound; no startup wait was opened")
            break
        except ForegroundProcessUnsettled as error:
            if not isinstance(bound, dict) or attempt + 1 == LAUNCH_OBSERVE_ATTEMPTS:
                return SupervisorError(f"{signal}; {note} because the preparation-owned process could not be proven ({error})")
            _sleep(supervisor, LAUNCH_OBSERVE_INTERVAL)  # observe only; no alias, input, launch or wait yet
        except SupervisorError as error:
            return SupervisorError(f"{signal}; {note} because the preparation-owned process could not be proven ({error})")
    now, pane = supervisor.clock(), item["pane_id"]
    evidence = hst.capture_screen(getattr(supervisor.herdr, "pane_read", None), pane, now=now)
    classification = hst.evidence_classification(provider, evidence)
    if classification["kind"] == "trust_dialog":
        return SupervisorError(f"{signal}; {provider} is showing a folder-trust dialog in pane {pane}, which is a separate decision and never an update wait: {note}, nothing was sent. "
                               f"Inspect the pane, or run `herdr-supervisor abandon-session-start --preparation-id {record['preparation_id']}`")
    trust = None
    if classification["kind"] == "claude_trust_folder":
        # A distinct, bounded HUMAN decision, but only for the folder this start was authorized for: the displayed folder, the configured project root and the owned process's own cwd must be one path.
        trust, why = hst.trust_folder_binding(classification, str(supervisor.config["project_root"]), process.get("cwd"))
        if trust is None:
            return SupervisorError(f"{signal}; Claude is showing a folder-trust prompt in pane {pane} but {why}: {note}, nothing was sent, and you must not trust it on this basis. "
                                   f"Inspect the pane, or run `herdr-supervisor abandon-session-start --preparation-id {record['preparation_id']}`")
    everything = hst.list_waits(supervisor.paths, preparation_id=record["preparation_id"])  # both providers: the ordinal must be unique within the preparation
    prior = [w for w in everything if w["provider"] == provider]
    origin = next((w["origin_command"] for w in reversed(everything) if isinstance(w.get("origin_command"), dict)), None)  # the original authorized request travels with every later wait
    try:
        wait = hst.new_wait_record(context="preparation", provider=provider, pane_id=pane, phase=str(phase), site=signal.site, cause_code=signal.code, now=now, ordinal=len(everything) + 1, origin_command=origin,
                                   settings=startup_wait_settings(supervisor.config), prior=prior, evidence=evidence, classification=classification,
                                   executable=_executable_now(supervisor, _provider_command(supervisor, provider, item)), process=process,
                                   preparation_id=record["preparation_id"], agent_name=item.get("agent_name"), binding=_wait_binding(item), trust_folder=trust)
    except SupervisorError as error:
        return SupervisorError(f"{signal}; {note}: {error}. Inspect pane {pane} or run `herdr-supervisor abandon-session-start --preparation-id {record['preparation_id']}`")
    for old in prior:
        if old["status"] in hst.OPEN_STATUSES:
            hst.close_wait(old, "SUPERSEDED", now, f"replaced by {wait['wait_id'][:8]}")
            hst.write_wait(supervisor.paths, old)
    hst.write_wait(supervisor.paths, wait)
    _announce_wait(supervisor, wait, "STARTUP_WAIT_REQUIRED")
    view = hst.wait_view(wait, now)
    text = "No input was sent and your other sessions are untouched.\n" + "\n".join(hst.instructions(view)) + f"\n(underlying: {signal})"
    return StartupWaitRequired(text, wait_id=wait["wait_id"], provider=provider, pane_id=pane, context="preparation")


@contextlib.contextmanager
def _startup_gate(supervisor: Any, provider: str, item: dict[str, Any], record: dict[str, Any], path: Path) -> Any:
    try:
        yield
    except StartupNotReady as signal:
        outcome = _open_startup_wait(supervisor, provider, item, record, path, signal)
        if outcome is signal:
            raise
        raise outcome from signal


def _open_wait_gate(supervisor: Any, record: dict[str, Any], provider: str) -> None:
    """A WAITING wait stops every further progress of that provider until an authenticated continuation, cancellation or expiry; a retry of the same task start can neither
    bypass nor extend it. A CONTINUING wait lets the sanctioned reconcile run, but only inside its ORIGINAL deadline: past it (including after a crash between the durable
    marker and the resume) the work is inert."""
    waits = hst.list_waits(supervisor.paths, preparation_id=record["preparation_id"], provider=provider, statuses=hst.OPEN_STATUSES)
    if not waits:
        return
    wait, now = waits[-1], supervisor.clock()
    if hst.expired(wait, now):
        _expire_wait(supervisor, wait, now)
        raise StartupWaitRequired(f"The {provider} startup wait {wait['wait_id'][:8]} expired at {iso_utc(wait['deadline_unix'])}; nothing continues. The preparation was made inert and all panes and sessions were preserved; "
                                  "start again with a new task start when ready.", wait_id=wait["wait_id"], provider=provider, pane_id=wait.get("pane_id"), context="preparation", expired=True)
    if wait["status"] == "CONTINUING":
        return
    view = hst.wait_view(wait, now)
    raise StartupWaitRequired("No input was sent. The start is still waiting for you.\n" + "\n".join(hst.instructions(view)),
                              wait_id=wait["wait_id"], provider=provider, pane_id=wait.get("pane_id"), context="preparation")


def _deadline_guard(supervisor: Any, preparation_id: str, provider: str, what: str) -> None:
    """Called immediately before EVERY new effect of a fresh-session preparation: a thread request, a pane split, an agent start or pane run, an alias rename, an identity bootstrap send,
    a pane label and the ownership binding. It is preparation-wide: ANY unfinished wait of this preparation (either provider) past its original deadline forbids the effect, expires
    that wait and makes the preparation inert, and so does the caller's absolute ceiling (`startup_wait.not_after_unix`) even when no wait is open. Effects already completed in time are
    kept, never rewound, and the clock is never reset."""
    now = supervisor.clock()
    for wait in hst.list_waits(supervisor.paths, preparation_id=preparation_id, statuses=hst.OPEN_STATUSES):
        if hst.expired(wait, now):
            _expire_wait(supervisor, wait, now)
            raise StartupWaitRequired(f"The {wait['provider']} startup wait {wait['wait_id'][:8]} expired at {iso_utc(wait['deadline_unix'])} before {what} ({provider}); that step was not done and nothing further continues. "
                                      "The preparation was made inert and all panes and sessions were preserved.", wait_id=wait["wait_id"], provider=wait["provider"], pane_id=wait.get("pane_id"), context="preparation", expired=True)
    ceiling = startup_wait_settings(supervisor.config)["not_after_unix"]
    if ceiling is not None and now > ceiling:
        _make_preparation_inert(supervisor, preparation_id, "absolute_deadline_passed")
        raise AbsoluteDeadlinePassed(f"The caller's absolute deadline ({iso_utc(ceiling)}) passed before {what} ({provider}); that step was not done and the preparation was made inert. "
                                     "Panes and sessions that already exist were preserved and nothing is rewound.")


def _make_preparation_inert(supervisor: Any, preparation_id: str, reason: str) -> bool:
    """ABANDONED = inert; no pane, session or process is closed. Never touches a bound or started preparation."""
    path = supervisor.paths.session_preparations_dir / f"{preparation_id}.json"
    if not path.exists():
        return False
    raw = load_json(path, label="session preparation")
    if isinstance(raw, dict) and raw.get("status") == "BINDING":
        # BINDING is an INTENT: inert only while owners.json still equals the pre-operation owners (the mutation was never performed). If the owners were already written, history
        # completed and is reconciled by the next retry (never rewound, never replayed).
        try:
            if supervisor.owners() != raw.get("owners_before"):
                return False
        except SupervisorError:
            return False
    elif not isinstance(raw, dict) or raw.get("status") in ("OWNERSHIP_BOUND", "TASK_STARTED", "ABANDONED"):
        return False
    raw.update({"status": "ABANDONED", "abandoned_at": iso_utc(supervisor.clock()), "resolved_by": reason})
    atomic_write_json(path, raw)
    return True


def _expire_wait(supervisor: Any, wait: dict[str, Any], now: float) -> None:
    hst.close_wait(wait, "EXPIRED", now, "deadline reached")
    inert = _make_preparation_inert(supervisor, wait["preparation_id"], "startup_wait_expired") if wait["context"] == "preparation" else False
    _announce_wait(supervisor, wait, "STARTUP_WAIT_RESULT", ok=False, outcome="expired", message=(
        f"The {wait['provider'].title()} startup wait expired; nothing continues. " + ("The preparation was made inert; panes and sessions were preserved. " if inert else "")
        + "Only capture and cleanup of the exact panes remain possible; start again with a new task start when ready."))


def sweep_startup_waits(supervisor: Any) -> list[str]:
    """Reconcile open preparation waits with their journal and expire overdue ones. Reads and writes files only: no Herdr call, no polling, no model wake-up.
    Called at worker start, before a continuation or cancellation, and by the gate."""
    changed: list[str] = []
    now = supervisor.clock()
    for wait in hst.list_waits(supervisor.paths, statuses=hst.OPEN_STATUSES):
        if wait["context"] != "preparation":
            continue
        path = supervisor.paths.session_preparations_dir / f"{wait['preparation_id']}.json"
        prep = load_json(path, label="session preparation") if path.exists() else None
        status = prep.get("status") if isinstance(prep, dict) else None
        if status in ("OWNERSHIP_BOUND", "TASK_STARTED"):
            hst.close_wait(wait, "RESOLVED", now, "preparation bound")
        elif status == "ABANDONED" or prep is None:
            hst.close_wait(wait, "CANCELLED", now, "preparation abandoned")
        elif status == "BINDING" and hst.expired(wait, now) and supervisor.owners() != prep.get("owners_before"):
            continue  # the owners were already written before the crash: completed history, reconciled by the next retry, not expired or rewound
        elif hst.expired(wait, now):  # WAITING or CONTINUING: the original deadline holds across a worker restart and a crash after the durable marker
            _expire_wait(supervisor, wait, now)
            changed.append(wait["wait_id"])
            continue
        elif wait["status"] == "WAITING" and not wait["notified"]:
            _announce_wait(supervisor, wait, "STARTUP_WAIT_REQUIRED")  # a crash between the wait record and its notification: same sequence, same event id, no duplicate
            changed.append(wait["wait_id"])
            continue
        else:
            continue
        hst.write_wait(supervisor.paths, wait)
        changed.append(wait["wait_id"])
    return changed


def startup_wait_views(supervisor: Any, *, preparation_id: str | None = None) -> list[dict[str, Any]]:
    """Read-only projection of open waits for status/doctor (expiry is shown virtually; nothing is written)."""
    now = supervisor.clock()
    views = []
    pending_original = [w for w in hst.list_waits(supervisor.paths, preparation_id=preparation_id, statuses=("RESOLVED",)) if hst.wait_view(w, now)["start_original_available"]]
    for wait in [*hst.list_waits(supervisor.paths, preparation_id=preparation_id, statuses=hst.OPEN_STATUSES), *pending_original]:
        view = hst.wait_view(wait, now)
        if hst.expired(wait, now):
            view = {**view, "status": "EXPIRED", "remaining_seconds": 0}
        views.append(view)
    return views


def _continuation_problem(supervisor: Any, wait: dict[str, Any], item: dict[str, Any], record: dict[str, Any], executable_after: Callable[[], dict[str, Any]]) -> tuple[str, str] | None:
    """(kind, reason) of the first failed check, else None. Read-only: no key, prompt, launch or bootstrap. Kinds: journal_moved, changed_process, blocker_present,
    changed_executable, changed_contract. Skip and Update are not told apart by the human's word: only the process, the executable and the deployed contracts decide."""
    provider, pane = wait["provider"], wait["pane_id"]
    if item.get("status") != wait["phase"] or item.get("pane_id") != pane or _wait_binding(item) != wait["binding"]:
        return "journal_moved", "the preparation journal no longer matches the waiting state; nothing was changed"
    # pane_available proves an EMPTY shell, so it is false for the provider we must verify here.
    # The exact pane/process read below fails closed on absence, ambiguity or replacement.
    try:
        process = _wait_process(supervisor, provider, item, "nothing was sent")
    except SupervisorError as error:
        return "changed_process", str(error)
    problem = hst.process_problem(wait["process"], process)
    if problem:
        return "changed_process", problem
    launch = item.get("launch")
    if isinstance(launch, dict) and isinstance(launch.get("process"), dict) and _owned_process(supervisor, provider, item) != launch["process"]:
        return "changed_process", "the pane's process is no longer the launch the journal bound"
    live = supervisor.herdr.list_agents()
    # Before the alias exists (PROCESS_BOUND) the exact pane is the only key; afterwards the preparation alias must also match.
    here = [a for a in live if a.get("pane_id") == pane and a.get("agent") == provider and (item.get("status") == "PROCESS_BOUND" or a.get("name") == item.get("agent_name"))]
    if len(here) != 1:
        return "blocker_present", f"Herdr does not report exactly one {provider} agent in pane {pane} yet ({len(here)} found)"
    if here[0].get("agent_status") not in READY_STATES:
        return "blocker_present", f"the {provider} agent in pane {pane} still reports {here[0].get('agent_status')!r}"
    screen = hst.capture_screen(getattr(supervisor.herdr, "pane_read", None), pane, now=supervisor.clock())
    blocker = hst.screen_blocker(provider, wait, screen)  # a visible update menu / trust prompt refuses even though the lifecycle says ready; a trust wait needs the clearing PROVEN
    if blocker:
        return "blocker_present", blocker
    problem = hst.executable_problem(wait["executable"], executable_after())
    if problem:
        return "changed_executable", problem
    return _contract_problem(supervisor, provider, item, record)


def _contract_problem(supervisor: Any, provider: str, item: dict[str, Any], record: dict[str, Any]) -> tuple[str, str] | None:
    """The deployed contracts the journaled start depends on, probed read-only and WITHOUT any cache (an update may have just changed the CLI): helper context,
    recorded approval mode and its CLI capability, the explicit-launch recipe's byte identities, the direct-launch chain (a legacy item: the Codex app-server/thread/resume
    chain with the shared helper readiness), or the model flag. No model turn is ever made."""
    launch = item.get("launch")
    try:
        _helper_context_unchanged(supervisor, provider, item, "nothing was sent")
        assert_recorded_approval_modes(supervisor.config, record, (provider,))
        mode = approval_mode(supervisor.config, provider)
        if mode != "inherit" and not supervisor.approval_mode_supported(provider, mode):
            return "changed_contract", f"the {provider} CLI no longer exposes the capability required by approval mode {mode}"
        if isinstance(launch, dict):
            issue = _recipe_revalidation(supervisor, item)
            if issue:
                return "changed_contract", issue
        if _direct(item):
            readiness = getattr(supervisor, "direct_launch_readiness", None)
            launch_ready: dict[str, Any] = {"ready": True, "reasons": []} if not callable(readiness) else readiness(item.get("input_policy", STRICT_POLICY))
            report = getattr(supervisor.herdr, "capability_report", None)
            contract = getattr(supervisor, "startup_codex_direct_contract", supervisor.codex_direct_contract)()
            missing = [name for name, ok in fresh_codex_direct_components(supervisor.config, report() if callable(report) else None, contract, launch_ready, new_start=False).items() if not ok]
            if missing:
                return "changed_contract", "unverified Codex capability after the wait: " + ", ".join(missing)
            if model_args(supervisor.config, provider, item["profile"]) and not supervisor.model_flag_supported(provider):
                return "changed_contract", f"the local {provider} CLI no longer exposes the required --model capability"
        elif provider in THREAD_PROVIDERS:
            method, policy = item.get("launch_method", "agent-start"), item.get("input_policy", STRICT_POLICY)
            readiness = getattr(supervisor, "helper_readiness", None)
            helper: dict[str, Any] = {"ready": True, "reasons": []} if not callable(readiness) else readiness(method, policy)
            report = getattr(supervisor.herdr, "capability_report", None)
            contract = getattr(supervisor, "startup_codex_contract", supervisor.codex_thread_contract)()
            missing = [name for name, ok in fresh_codex_components(supervisor.config, report() if callable(report) else None, contract, helper).items() if not ok]
            if missing:
                return "changed_contract", "unverified Codex capability after the wait: " + ", ".join(missing)
        elif model_args(supervisor.config, provider, item["profile"]) and not supervisor.model_flag_supported(provider):
            return "changed_contract", f"the local {provider} CLI no longer exposes the required --model capability"
    except SupervisorError as error:
        return "changed_contract", str(error)
    return None


def continue_startup_wait(supervisor: Any, wait_id: str, *, actor: str) -> dict[str, Any]:
    """Authenticated continuation (the caller holds the worker lock and the command identity). Reconciles the SAME launched process before doing more work,
    then re-enters the journaled preparation, which can launch/bootstrap/deliver nothing beyond what its markers allow."""
    now = supervisor.clock()
    sweep_startup_waits(supervisor)
    try:
        wait = hst.read_wait(supervisor.paths, wait_id)
    except SupervisorError as error:
        return {"ok": False, "kind": "unknown_wait", "message": f"{error}; nothing was changed"}
    refusal = hst.wait_refusal(wait, wait_id)
    if refusal or wait is None:
        return {"ok": False, "kind": "not_waiting", "wait_id": wait_id, "message": refusal}
    if wait["context"] != "preparation":
        return {"ok": False, "kind": "wrong_context", "wait_id": wait_id, "message": "this wait belongs to a running task; continue it from that run"}
    settings = startup_wait_settings(supervisor.config)
    wait["continue_attempts"] += 1
    if wait["continue_attempts"] > settings["max_continue_attempts"]:
        return _cancel_open_wait(supervisor, wait, actor=actor, reason="continuation attempts exhausted", ok=False)
    path = supervisor.paths.session_preparations_dir / f"{wait['preparation_id']}.json"
    state = supervisor.store.read_state(required=False)
    if state and state.get("supervisor_state") not in ("DONE", "CANCELLED"):
        hst.add_history(wait, now, "refused", "task boundary")
        hst.write_wait(supervisor.paths, wait)
        return {"ok": False, "kind": "task_boundary", "wait_id": wait_id, "message": "fresh session preparation is allowed only at a terminal task boundary; nothing was continued"}
    try:
        record = validate_preparation_record(supervisor.config, load_json(path, label="session preparation"), wait["preparation_id"])
    except SupervisorError as error:
        return {"ok": False, "kind": "journal_moved", "wait_id": wait_id, "message": f"{error}; nothing was continued"}
    item = (record.get("providers") or {}).get(wait["provider"])
    if record.get("status") != "SESSION_SELECTION_AUTHORIZED" or not isinstance(item, dict):
        hst.add_history(wait, now, "refused", "journal")
        hst.write_wait(supervisor.paths, wait)
        return {"ok": False, "kind": "journal_moved", "wait_id": wait_id, "message": "the preparation is no longer waiting at this provider; nothing was continued"}
    seen: dict[str, dict[str, Any]] = {}

    def executable_after() -> dict[str, Any]:
        if "value" not in seen:
            seen["value"] = _executable_now(supervisor, _provider_command(supervisor, wait["provider"], item))
        return seen["value"]

    problem = _continuation_problem(supervisor, wait, item, record, executable_after)
    checked = supervisor.clock()  # the checks above can be slow (hashing, CLI probes): the ORIGINAL deadline is re-evaluated against the time they actually finished
    if "value" in seen:  # executable/version before (at the wait) and after (now) are both on the record
        wait["executable_after"] = {**seen["value"], "checked_at_unix": float(checked)}
    if hst.expired(wait, checked):
        _expire_wait(supervisor, wait, checked)
        return {"ok": False, "kind": "expired", "wait_id": wait_id, "message": f"The startup wait expired at {iso_utc(wait['deadline_unix'])} while the process and contract checks were running; nothing was continued, started or bound. "
                                                                              "The preparation was made inert and all panes and sessions were preserved."}
    if problem is not None:
        kind, reason = problem
        after_note = ""
        if kind in ("changed_process", "changed_executable"):
            # The change itself already holds the start; the contract result is recorded so a successor/restart decision knows what the new CLI still satisfies. Nothing is adopted.
            contracts = _contract_problem(supervisor, wait["provider"], item, record)
            after_note = " Deployed contracts after the change: " + ("verified (read-only probes; the replacement process is still not adopted)." if contracts is None else f"NOT verified ({contracts[1]}).")
            wait["contracts_after_change"] = "verified" if contracts is None else f"not verified: {contracts[1]}"[:200]
        hst.add_history(wait, now, "refused", f"{kind}: {reason}")
        hst.write_wait(supervisor.paths, wait)
        hold = ("The process or its contract changed, so this start holds for explicit successor/restart authority (cancel it). " if kind in ("changed_process", "changed_executable", "changed_contract", "journal_moved") else "Handle the pane, then continue again. ")
        message = f"Not continued ({kind}): {reason}.{after_note} Nothing was sent. {hold}Cancel: `herdr-supervisor cancel-startup --wait-id {wait_id}`."
        _announce_wait(supervisor, wait, "STARTUP_WAIT_RESULT", ok=False, outcome=kind, message=message)
        return {"ok": False, "kind": kind, "wait_id": wait_id, "message": message}
    wait["status"] = "CONTINUING"
    wait["contracts_after_change"] = "unchanged: all deployed contracts re-verified"
    wait["continued_at_unix"] = float(checked)  # the human's waiting time ends when the checks finished; a later wait is charged only for what is left
    wait["continued_by"] = actor[:80]
    hst.add_history(wait, checked, "continuing", actor[:60])
    hst.write_wait(supervisor.paths, wait)  # durable BEFORE the machinery runs: a duplicate or late callback now sees CONTINUING and does nothing
    envelope = record.get("request_envelope")
    try:
        prepared = supervisor.prepare_task_sessions(record["selection"], preparation_id=wait["preparation_id"], origin=record["origin"], request_binding=record.get("request_binding"),
                                                    request_envelope=envelope, start_provider=(envelope.get("start") if record["origin"] == "cli" and isinstance(envelope, dict) else "codex") or "codex")
    except StartupWaitRequired as again:
        if again.expired:  # the deadline passed mid-way: the guard stopped the next effect and already made the preparation inert
            return {"ok": False, "kind": "expired", "wait_id": wait_id, "message": str(again)}
        return {"ok": True, "kind": "waiting_again", "wait_id": again.wait_id, "message": str(again)}
    except SupervisorError as error:
        message = f"The wait was continued but the preparation stopped: {error}. Nothing was resent. {_next_step(supervisor, wait, record, bound=False)}"
        _announce_wait(supervisor, wait, "STARTUP_WAIT_RESULT", ok=False, outcome="preparation_stopped", message=message, start_original_available=isinstance(wait.get("origin_command"), dict))
        return {"ok": False, "kind": "preparation_stopped", "wait_id": wait_id, "message": message}
    current = hst.read_wait(supervisor.paths, wait_id) or wait
    bound = prepared.get("status") in ("OWNERSHIP_BOUND", "TASK_STARTED")
    hst.close_wait(current, "RESOLVED", supervisor.clock(), "preparation bound")
    message = (f"Fresh sessions are prepared and bound after your startup handling. No task was started or delivered. {_next_step(supervisor, current, record, bound=True)}"
               if bound else "Preparation continued.")
    _announce_wait(supervisor, current, "STARTUP_WAIT_RESULT", ok=True, outcome="resolved", message=message, start_original_available=bool(bound and isinstance(current.get("origin_command"), dict)))
    return {"ok": True, "kind": "resolved", "wait_id": wait_id, "message": message}


def _next_step(supervisor: Any, wait: dict[str, Any], record: dict[str, Any], *, bound: bool) -> str:
    """Who acts next and with which SUPPORTED action. Retyping a task in Telegram creates a fresh pending authorization and is refused while this preparation is unresolved, so
    that is never offered."""
    if record.get("origin") == "cli":
        return ("Next (you): run the same `herdr-supervisor run …` command again (same task text and options); it adopts this preparation and launches nothing again." if bound
                else "Next (you): run the same `herdr-supervisor run …` command again to reconcile it (nothing is resent), or `herdr-supervisor cancel-startup --wait-id %s`." % wait["wait_id"])
    if isinstance(wait.get("origin_command"), dict):
        if bound:
            return (f"Next (you, a separate step): tap 'Start the original task' on the Telegram card or run `herdr-supervisor start-original --wait-id {wait['wait_id']}`; it re-applies the ORIGINAL "
                    "authorized start once (same request, preparation and authority; no new approval). A new /task is refused while this preparation is unresolved.")
        return (f"Next (you): `herdr-supervisor start-original --wait-id {wait['wait_id']}` retries the original request and reconciles the journaled phase (nothing is resent), "
                f"or `herdr-supervisor cancel-startup --wait-id {wait['wait_id']}`.")
    return ("No original start request is stored for this preparation. Adopt the prepared sessions with an explicit `herdr-supervisor rebind-sessions --codex-pane … --apply` (it makes the "
            "unresolved preparation inert), or `herdr-supervisor cancel-startup --wait-id %s`." % wait["wait_id"])


def _cancel_open_wait(supervisor: Any, wait: dict[str, Any], *, actor: str, reason: str, ok: bool) -> dict[str, Any]:
    now = supervisor.clock()
    hst.close_wait(wait, "CANCELLED", now, f"{reason} ({actor[:40]})")
    inert = _make_preparation_inert(supervisor, wait["preparation_id"], "startup_wait_cancelled") if wait["context"] == "preparation" else False
    message = (f"The {wait['provider'].title()} startup wait was cancelled ({reason}). " + ("The preparation is inert; " if inert else "")
               + "no pane, session or process was closed, started or sent anything. Close the exact diagnostic pane yourself (or by the diagnostic cleanup step) and start again when ready.")
    _announce_wait(supervisor, wait, "STARTUP_WAIT_RESULT", ok=ok, outcome="cancelled", message=message)
    return {"ok": ok, "kind": "cancelled", "wait_id": wait["wait_id"], "message": message}


def cancel_startup_wait(supervisor: Any, wait_id: str, *, actor: str) -> dict[str, Any]:
    sweep_startup_waits(supervisor)
    try:
        wait = hst.read_wait(supervisor.paths, wait_id)
    except SupervisorError as error:
        return {"ok": False, "kind": "unknown_wait", "message": f"{error}; nothing was changed"}
    if wait is None or wait["status"] not in hst.OPEN_STATUSES:
        return {"ok": False, "kind": "not_waiting", "wait_id": wait_id, "message": f"startup wait {str(wait_id)[:8]} is not open; nothing was changed"}
    if wait["context"] != "preparation":
        return {"ok": False, "kind": "wrong_context", "wait_id": wait_id, "message": "this wait belongs to a running task; cancel it from that run"}
    return _cancel_open_wait(supervisor, wait, actor=actor, reason="cancelled by the operator", ok=True)


def _direct_authorization(supervisor: Any, provider: str, profile: str) -> dict[str, Any]:
    """The fields a NEW fresh-Codex item carries at authorization: the direct-launch marker, the exact launch request and the helper context it was authorized under."""
    request = launch_request_payload(supervisor.config, provider, profile)
    context = getattr(supervisor, "helper_context", None)
    fields: dict[str, Any] = {"fresh_launch": DIRECT_LAUNCH, "launch_request": request}
    fields["helper_context_fingerprint"] = hctx.fingerprint(context(request["cwd"])) if callable(context) else "0" * 64
    return fields


def prepare_sessions(
    supervisor: Any,
    selection: Any,
    *,
    preparation_id: str,
    journal_path: Path | None = None,
    origin: str = "supervisor",
    request_binding: str | None = None,
    request_envelope: dict[str, Any] | None = None,
    start_provider: str = "codex",
) -> dict[str, Any]:
    """Prepare selected fresh sessions exactly once, then atomically replace ownership.

    An interrupted pane-create or agent-start call is intentionally ambiguous and fails closed. The
    operation is never repeated automatically because Herdr has no idempotency key for either action.
    """
    if start_provider not in PROVIDERS:
        raise SupervisorError("invalid starting provider for session preparation")
    selected = validate_selection(supervisor.config, selection)
    fresh = tuple(selected["fresh_providers"])
    valid_binding = isinstance(request_binding,str) and len(request_binding)==64 and all(c in "0123456789abcdef" for c in request_binding)
    valid_envelope = False
    if origin == "cli" and isinstance(request_envelope, dict):
        valid_envelope = cli_request_binding(supervisor.config, request_envelope) == request_binding
    if origin not in ("cli","supervisor") or (origin=="cli") != valid_binding or (origin=="cli") != valid_envelope or (origin=="supervisor" and (request_binding is not None or request_envelope is not None)):
        raise SupervisorError("session preparation origin or request binding is invalid")
    if origin == "cli" and (validate_cli_request_envelope(supervisor.config, request_envelope).get("session_selection") != selected or validate_cli_request_envelope(supervisor.config, request_envelope).get("start") != start_provider):
        raise SupervisorError("session preparation request selection changed")
    if fresh and not _valid_uuid(preparation_id):
        raise SupervisorError("fresh session preparation requires a canonical UUID")
    state=supervisor.store.read_state(required=False)
    if state and state.get("supervisor_state") not in ("DONE","CANCELLED"):
        raise SupervisorError("fresh session preparation is allowed only at a terminal task boundary")
    reset_phase = (state.get("codex_reset") or {}).get("current_redemption_state") if state else None
    if reset_phase not in (None,"IDLE","RESET_VERIFIED"):
        raise SupervisorError("session preparation is blocked by unresolved reset state")
    if supervisor.paths.session_preparations_dir.exists():
        for other in supervisor.paths.session_preparations_dir.glob("*.json"):
            if other.stem==preparation_id:
                continue
            prior=load_json(other,label="session preparation")
            if isinstance(prior,dict) and prior.get("status") not in ("TASK_STARTED","ABANDONED"):
                if prior.get("status") == "OWNERSHIP_BOUND":
                    held = next((w for w in reversed(hst.list_waits(supervisor.paths, preparation_id=other.stem)) if isinstance(w.get("origin_command"), dict)), None)
                    route = (f"start it with its stored authorization (Telegram 'Start the original task' or `herdr-supervisor start-original --wait-id {held['wait_id']}`)" if held is not None
                             else "re-run the same `herdr-supervisor run …` command (CLI start) or adopt it with an explicit `herdr-supervisor rebind-sessions --apply`")
                    raise BoundPreparationUnstarted(f"session preparation {other.stem} is unresolved: it is already bound but its task was never started, so a different or new task cannot be started: {route}. "
                                          "`abandon-session-start` cannot release a bound preparation")
                raise SupervisorError(f"session preparation {other.stem} is unresolved; inspect it or run `herdr-supervisor abandon-session-start --preparation-id {other.stem}`")
    invocation = object()  # this call only: the identity guarded submission eligibility is bound to
    tokens: dict[str, SubmissionToken] = {}
    owners = supervisor.verify_or_seed_owners(tuple(p for p in PROVIDERS if p == start_provider or p in fresh))
    if not fresh:
        return {"status": "OWNERSHIP_BOUND", "selection": selected, "owners": owners, "providers": {}}
    contract_probe = getattr(supervisor.herdr, "capability_report", None)
    contract = contract_probe() if callable(contract_probe) else None
    if not session_contract_supported(supervisor.config, contract):
        raise SupervisorError("fresh session creation unavailable: the supported Herdr pane split and agent-start contract was not verified")
    # Every read-only provider preflight completes before the first thread/pane/session side effect. This also
    # prevents fresh-all from partially preparing one provider before discovering the other's capability is absent.
    path = journal_path or supervisor.paths.session_preparations_dir / f"{preparation_id}.json"
    config_method = supervisor.launch_method() if callable(getattr(supervisor, "launch_method", None)) else "agent-start"
    config_policy = supervisor.input_policy() if callable(getattr(supervisor, "input_policy", None)) else STRICT_POLICY
    method, policy = config_method, (config_policy if config_method == EXPLICIT_LAUNCH else STRICT_POLICY)
    codex_form: str | None = None  # "direct" (a NEW launch or a journaled direct item) | "legacy" (a journaled pre-created-thread item) | None (no fresh Codex)
    if any(provider in THREAD_PROVIDERS for provider in fresh):
        codex_form = "direct"
    if path.exists():
        # A journaled operation keeps the launch method it recorded; changing settings cannot convert it.
        prior_record = load_json(path, label="session preparation")
        prior_codex = ((prior_record or {}).get("providers") or {}).get("codex") if isinstance(prior_record, dict) else None
        if isinstance(prior_codex, dict):
            method = prior_codex.get("launch_method", "agent-start")
            policy = prior_codex.get("input_policy", STRICT_POLICY)  # a journal without a policy is strict; settings never upgrade it
            if not _direct(prior_codex):
                if prior_codex.get("status") == "AUTHORIZED" and not any(key in prior_codex for key in ("thread_id", "thread_request_id", "thread_payload")):
                    raise SupervisorError(f"codex session preparation {preparation_id} was authorized by an earlier library for the pre-created-thread handoff and nothing was done for it yet; "
                                          f"it is not converted into a direct launch. Run `herdr-supervisor abandon-session-start --preparation-id {preparation_id}` and start the task again")
                codex_form = "legacy"
        elif codex_form == "direct":
            codex_form = None  # a journal without a Codex item (fresh-claude): nothing Codex-specific to verify
    elif codex_form == "direct" and (config_method != EXPLICIT_LAUNCH):
        raise SupervisorError("fresh Codex unavailable: a new fresh Codex is launched directly through the explicit pane-run recipe, which needs session_start.codex_launch_method = explicit-pane-run "
                              "(the generic agent start does not bind the launch context)")
    if codex_form == "direct":
        readiness = getattr(supervisor, "direct_launch_readiness", None)
        launch_ready: dict[str, Any] = ({"ready": True, "reasons": []} if not callable(readiness)  # fakes without the shared report
                                        else readiness() if policy == config_policy else readiness(policy))
        components = fresh_codex_direct_components(supervisor.config, contract, supervisor.codex_direct_contract(), launch_ready, new_start=not path.exists())
        missing = [name for name, ok in components.items() if not ok]
        if missing:
            detail = "; ".join(launch_ready.get("reasons") or []) if "codex_direct_launch" in missing else ""
            raise SupervisorError("fresh Codex unavailable: unverified capability " + ", ".join(missing) + (f" ({detail})" if detail else ""))
    elif codex_form == "legacy":
        readiness = getattr(supervisor, "helper_readiness", None)
        helper: dict[str, Any] = ({"ready": True, "reasons": []} if not callable(readiness)  # fakes without the shared report
                                  else readiness() if (method == config_method and policy == config_policy) else readiness(method, policy))
        components = fresh_codex_components(supervisor.config, contract, supervisor.codex_thread_contract(), helper)
        missing = [name for name, ok in components.items() if not ok]
        if missing:
            detail = "; ".join(helper.get("reasons") or []) if "codex_helper_ready" in missing else ""
            raise SupervisorError("fresh Codex unavailable: unverified capability " + ", ".join(missing) + (f" ({detail})" if detail else ""))
    for provider in fresh:
        if provider in THREAD_PROVIDERS and codex_form == "legacy":
            continue  # a legacy item's model is set on its pre-created thread, never through a launch flag
        if model_args(supervisor.config,provider,selected["profiles"][provider]) and not supervisor.model_flag_supported(provider):
            raise SupervisorError(f"local {provider} CLI does not expose the required --model capability")
    for provider in fresh:
        mode = approval_mode(supervisor.config, provider)
        if provider in THREAD_PROVIDERS and codex_form == "direct":
            continue  # the direct chain above probed the exact approval flag of the top-level CLI that is launched
        if mode != "inherit" and not supervisor.approval_mode_supported(provider, mode):
            raise SupervisorError(f"local {provider} CLI does not expose the capability required by approval mode {mode}; nothing was created")
    if path.exists():
        record = validate_preparation_record(supervisor.config,load_json(path, label="session preparation"),preparation_id)
        if record.get("selection") != selected or record.get("origin") != origin or record.get("request_binding") != request_binding or record.get("request_envelope") != request_envelope:
            raise SupervisorError("session preparation binding changed")
    else:
        record = {
            "schema_version": 2, "preparation_id": preparation_id, "status": "SESSION_SELECTION_AUTHORIZED",
            "selection": selected, "created_at": iso_utc(supervisor.clock()), "origin": origin,
            "request_binding": request_binding, "request_envelope": request_envelope,
            "owners_before": {p:dict(owners[p]) for p in PROVIDERS},
            "providers": {p: {"status": "AUTHORIZED", "old_pane_id": owners[p]["pane_id"], "old_session_id": owners[p]["session_id"], "profile": selected["profiles"][p], "preparation_id": preparation_id,
                              **({"launch_method": method, "input_policy": policy} if p in THREAD_PROVIDERS and method == EXPLICIT_LAUNCH else {}),
                              **(_direct_authorization(supervisor, p, selected["profiles"][p]) if p in THREAD_PROVIDERS else {}),
                              **({"approval_mode": approval_mode(supervisor.config, p)} if approval_mode(supervisor.config, p) != "inherit" else {})} for p in fresh},
        }
        atomic_write_json(path, record)
    owners_before=record["owners_before"]
    if record.get("status") not in ("TASK_STARTED","ABANDONED"):
        # The configured mode must still equal the recorded one BEFORE any binding recovery or acceptance of a bound preparation (a started task is history).
        assert_recorded_approval_modes(supervisor.config, record, fresh)
    if record.get("status") in ("OWNERSHIP_BOUND","TASK_STARTED"):
        bound = record.get("bound_owners")
        if bound != supervisor.owners():
            raise SupervisorError("session preparation was bound but owners.json changed")
        return record
    if record.get("status") == "ABANDONED":
        raise SupervisorError("session preparation was abandoned and cannot be resumed")
    if record.get("status") == "BINDING":
        old_owners={p:dict(owners_before[p]) for p in PROVIDERS}
        new_owners={p:dict(owners_before[p]) for p in PROVIDERS}
        for provider in fresh:
            item=record["providers"][provider]
            if item.get("status")!="IDENTITY_VERIFIED":
                raise SupervisorError("session ownership binding journal is incomplete")
            new_owners[provider]={"pane_id":item["pane_id"],"session_id":item["session_id"]}
        live=supervisor.herdr.list_agents()
        if any(new_owners[p]["session_id"] in {old_owners[q]["session_id"] for q in PROVIDERS} for p in fresh):
            raise SupervisorError("fresh session identity is not distinct from the preserved owner set")
        _verify_owner_set(live,old_owners,label="preparation")
        _verify_owner_set(live,new_owners,label="prepared")
        if owners==old_owners:
            for provider in fresh:  # a persisted BINDING marker is INTENT: the previously unperformed owners write is a new effect and obeys the original bound
                _deadline_guard(supervisor, preparation_id, provider, "binding ownership")
            atomic_write_json(supervisor.paths.owners_file,new_owners,mode=0o600)
        elif owners!=new_owners:
            raise SupervisorError("session ownership binding is ambiguous; owners.json matches neither side")
        record.update({"status":"OWNERSHIP_BOUND","bound_at":iso_utc(supervisor.clock()),"bound_owners":new_owners})
        atomic_write_json(path,record)
        supervisor._live_names.clear()
        return record
    for provider in fresh:
        item = record["providers"][provider]
        phase = item.get("status")
        if phase != "IDENTITY_VERIFIED":
            _open_wait_gate(supervisor, record, provider)  # an open human startup wait stops this provider until an authenticated continuation, cancellation or expiry
        if phase == "AUTHORIZED" and provider in THREAD_PROVIDERS and not _direct(item):
            raise SupervisorError(f"{provider} preparation {preparation_id} predates the direct launch and has not started; it is not converted. Run `herdr-supervisor abandon-session-start --preparation-id {preparation_id}` and start again")
        if phase == "THREAD_CREATING":
            # LEGACY recovery only (a preparation journaled by an earlier library): this library never starts a thread.
            # Recovery: reconcile a settled helper result for this exact request; an unknown outcome stays
            # reconciling until the operator explicitly abandons. Never a second thread/start.
            recorded = item.get("helper_context_fingerprint")
            if recorded is not None:
                context = getattr(supervisor, "helper_context", None)
                current = hctx.fingerprint(context(item["thread_payload"]["cwd"])) if callable(context) else recorded
                if current != recorded:
                    raise SupervisorError(f"{provider} thread creation for preparation {preparation_id} was recorded under helper context {recorded[:12]} but this host now resolves {current[:12]}; nothing was repeated. Restore the original Codex executable, CODEX_HOME and journal root, or run `herdr-supervisor abandon-session-start --preparation-id {preparation_id}`")
            try:
                settled = supervisor.settled_codex_thread(item["thread_payload"], item["thread_request_id"], *( [recorded] if recorded is not None else [] ))
            except SupervisorError as error:
                raise SupervisorError(f"{provider} thread creation for preparation {preparation_id} is unresolved ({error}); nothing was repeated. Inspect the helper journal or run `herdr-supervisor abandon-session-start --preparation-id {preparation_id}`") from error
            if settled is None:
                raise SupervisorError(f"{provider} thread creation for preparation {preparation_id} is still unresolved; nothing was repeated. Inspect the helper journal or run `herdr-supervisor abandon-session-start --preparation-id {preparation_id}`")
            _adopt_thread(item, settled)
            atomic_write_json(path, record)
            phase = "THREAD_CREATED"
        if phase == "THREAD_CREATED":
            phase = "AUTHORIZED"  # the pane/launch sequence below is shared; the thread id is already durable
        if phase == "PANE_CREATING":
            raise SupervisorError(f"{provider} session preparation {preparation_id} is ambiguous after an interrupted {phase.lower()}; nothing was repeated. Inspect it, then explicitly rebind or run `herdr-supervisor abandon-session-start --preparation-id {preparation_id}`")
        if phase == "AGENT_STARTING" or phase in BOOTSTRAP_STATES:
            with _startup_gate(supervisor, provider, item, record, path):
                _settle_identity(supervisor,provider,item,record,path)
            phase="IDENTITY_VERIFIED"
        if phase == "AUTHORIZED":
            intended_name=f"{provider}-run-{preparation_id[:8]}"
            if any(a.get("name")==intended_name for a in supervisor.herdr.list_agents()):
                raise SupervisorError(f"fresh {provider} agent name already exists; no pane was created")
            _deadline_guard(supervisor, preparation_id, provider, "creating its pane")
            item["status"] = "PANE_CREATING"
            atomic_write_json(path, record)
            _deadline_guard(supervisor, preparation_id, provider, "creating its pane")  # re-read after the intent write, immediately before the split; the marker stays consumed
            split_at = _now(supervisor)  # the guarded window starts BEFORE the split is invoked: response latency counts against it
            pane = supervisor.herdr.split_pane(
                owners_before[provider]["pane_id"], direction=supervisor.config["session_start"]["direction"],
                ratio=float(supervisor.config["session_start"]["ratio"]), cwd=supervisor.config["project_root"],
            )
            occupied={owners_before[p]["pane_id"] for p in PROVIDERS}
            occupied.update(
                other["pane_id"] for other in record["providers"].values()
                if other is not item and other.get("status") in _PANE_PHASES
            )
            if not _valid_pane(pane) or pane in occupied:
                raise SupervisorError(f"Herdr did not return a canonical distinct fresh {provider} pane")
            # The returned identity is durable before any further probe. A crash or failed availability
            # check can now reconcile this exact pane and can never cause another split.
            item.update({"pane_id": pane, "status": "FRESH_PANE_CREATED"})
            atomic_write_json(path, record)
            if item.get("input_policy") == GUARDED_POLICY:
                tokens[provider] = SubmissionToken(_TOKEN_KEY, preparation_id, pane, invocation, split_at)  # only a split made by THIS call
            phase = "FRESH_PANE_CREATED"
        if item.get("launch_method") == EXPLICIT_LAUNCH and (phase == "FRESH_PANE_CREATED" or phase in LAUNCH_STATES):
            item["status"] = phase
            with _startup_gate(supervisor, provider, item, record, path):
                _launch_explicit(supervisor, provider, item, record, path, tokens.get(provider), invocation)
                _settle_identity(supervisor, provider, item, record, path)
        elif phase == "FRESH_PANE_CREATED":
            if not supervisor.herdr.pane_available(item["pane_id"]):
                raise SupervisorError(f"fresh {provider} pane is not an empty shell pane; no agent was started")
            profile = item["profile"]
            if provider in THREAD_PROVIDERS:
                args = ["resume", item["thread_id"], *approval_args(supervisor.config, provider), "--no-daemon"]  # pane-local hooks; exact pre-created thread and model retained
            else:
                args = [*model_args(supervisor.config, provider, profile), *approval_args(supervisor.config, provider)]
            name = f"{provider}-run-{preparation_id[:8]}"
            _deadline_guard(supervisor, preparation_id, provider, "starting its agent")
            item.update({"status": "AGENT_STARTING", "agent_name": name})
            atomic_write_json(path, record)
            _deadline_guard(supervisor, preparation_id, provider, "starting its agent")  # re-read after the intent write, immediately before the start; the marker stays consumed
            with _startup_gate(supervisor, provider, item, record, path):
                try:
                    supervisor.herdr.start_agent(name, kind=provider, pane_id=item["pane_id"], args=args)
                except HerdrError as error:
                    if error.code in ("agent_not_ready", "agent_blocked"):
                        raise StartupNotReady(f"fresh {provider} agent start reported {error.code}: the process was started but is not ready", site="agent_start_not_ready", code=error.code, pane_id=item["pane_id"], provider=provider) from error
                    raise
                _settle_identity(supervisor,provider,item,record,path)
        elif phase != "IDENTITY_VERIFIED":
            raise SupervisorError(f"invalid {provider} session preparation state: {phase}")
        if item.get("status") == "IDENTITY_VERIFIED":
            _apply_label(supervisor,provider,item,record,path)
    new_owners = {p: dict(owners_before[p]) for p in PROVIDERS}
    for provider in fresh:
        item = record["providers"][provider]
        new_owners[provider] = {"pane_id": item["pane_id"], "session_id": item["session_id"]}
    # Re-read live state and validate the complete pair before the single ownership mutation.
    live = supervisor.herdr.list_agents()
    if owners != owners_before or supervisor.owners() != owners_before:
        raise SupervisorError("session ownership changed after preparation authorization")
    if any(new_owners[p]["session_id"] in {owners_before[q]["session_id"] for q in PROVIDERS} for p in fresh):
        raise SupervisorError("fresh session identity is not distinct from the preserved owner set")
    _verify_owner_set(live,owners_before,label="preparation")
    _verify_owner_set(live,new_owners,label="prepared")
    for provider in fresh:
        _deadline_guard(supervisor, preparation_id, provider, "binding ownership")
    record["status"] = "BINDING"
    atomic_write_json(path, record)
    for provider in fresh:  # re-read after the intent write, immediately before the owners write: with owners still unchanged the BINDING intent is made inert, never replayed
        _deadline_guard(supervisor, preparation_id, provider, "binding ownership")
    atomic_write_json(supervisor.paths.owners_file, new_owners, mode=0o600)
    record.update({"status": "OWNERSHIP_BOUND", "bound_at": iso_utc(supervisor.clock()), "bound_owners": new_owners})
    atomic_write_json(path, record)
    supervisor._live_names.clear()
    return record
