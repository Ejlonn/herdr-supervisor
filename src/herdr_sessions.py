"""Bounded task-start session selection and durable fresh-session preparation. Fresh Codex pre-creates a
durable native thread through the supported app-server `thread/start` (via the user-scoped helper journal),
persists its id, and launches `codex resume <thread-id>`; Fresh Claude keeps its provider-specific path."""

from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any, Callable

from herdr_cli import help_has_lexeme, match_exact_session, session_identity
from herdr_core import PROVIDERS, READY_STATES, SupervisorError, atomic_write_json, canonical_json, iso_utc, load_json, valid_pane_id

SESSION_POLICIES = ("preserve", "fresh-codex", "fresh-claude", "fresh-all")
PANE_ID_MAX = 128
_PREPARATION_STATES = ("SESSION_SELECTION_AUTHORIZED", "BINDING", "OWNERSHIP_BOUND", "TASK_STARTED", "ABANDONED")
_PROVIDER_STATES = ("AUTHORIZED", "THREAD_CREATING", "THREAD_CREATED", "PANE_CREATING", "FRESH_PANE_CREATED", "AGENT_STARTING", "IDENTITY_VERIFIED")
# Providers whose fresh session is a pre-created durable thread resumed in the new pane (exact-id identity).
THREAD_PROVIDERS = ("codex",)
_THREAD_PHASES = ("THREAD_CREATING", "THREAD_CREATED", "PANE_CREATING", "FRESH_PANE_CREATED", "AGENT_STARTING", "IDENTITY_VERIFIED")
# Every surface Fresh Codex needs; each is verified read-only and reported by name (doctor, Telegram predicate).
CODEX_THREAD_COMPONENTS = ("herdr_pane_split", "herdr_agent_start", "herdr_identity_observation", "codex_app_server", "codex_thread_start", "codex_resume_session_id")


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
    return {"codex_app_server": app_server, "codex_thread_start": thread_start, "codex_resume_session_id": resume}


def fresh_codex_components(config: dict[str, Any], herdr_report: Any, codex_contract: Any) -> dict[str, bool]:
    """Per-component verdicts of the complete Fresh Codex capability chain (all must be True)."""
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
    }


def fresh_policy_supported(config: dict[str, Any], policy: str, herdr_report: Any, codex_contract: Any) -> bool:
    """One eligibility predicate for UI and execution: preserve needs nothing; a fresh Claude needs the Herdr
    pane/agent contract; any policy that creates a fresh Codex needs the complete thread chain."""
    if policy not in SESSION_POLICIES:
        return False
    fresh = fresh_providers(policy)
    if not fresh:
        return True
    if not session_contract_supported(config, herdr_report):
        return False
    if any(provider in THREAD_PROVIDERS for provider in fresh):
        return all(fresh_codex_components(config, herdr_report, codex_contract).values())
    return True


def thread_start_payload(config: dict[str, Any], provider: str, profile_id: str) -> dict[str, Any]:
    """Supervisor-owned thread/start request: the approved absolute workspace, the configured model or the
    provider default, and a durable thread."""
    root = str(config["project_root"])
    if not root.startswith("/"):
        raise SupervisorError("project_root must be absolute for a fresh thread")
    return {"cwd": root, "model": config["session_start"]["model_profiles"][provider][profile_id].get("model"), "ephemeral": False}


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
        "providers": {p: {"status": v.get("status"), "old_session": str(v.get("old_session_id") or "")[:8], "new_session": str(v.get("session_id") or "")[:8], "pane_id": v.get("pane_id")} for p, v in providers.items() if isinstance(v, dict)},
    }


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
        threaded = provider in THREAD_PROVIDERS
        if not threaded and (phase in ("THREAD_CREATING","THREAD_CREATED") or any(key in item for key in ("thread_id","thread_request_id","thread_payload","thread_model","thread_provider"))):
            raise SupervisorError(f"{provider} preparation carries thread fields for a non-thread provider")
        if threaded:
            if phase in _THREAD_PHASES:
                if item.get("thread_request_id") != thread_request_id(preparation_id, provider) or not isinstance(item.get("thread_payload"), dict) or set(item["thread_payload"]) != {"cwd","model","ephemeral"}:
                    raise SupervisorError(f"{provider} thread request binding is invalid")
            if phase in _THREAD_PHASES[1:]:
                if not _valid_uuid(item.get("thread_id")) or not isinstance(item.get("thread_model"), str) or not isinstance(item.get("thread_provider"), str):
                    raise SupervisorError(f"{provider} pre-created thread identity is missing")
                if item["thread_payload"].get("model") is not None and item["thread_model"] != item["thread_payload"]["model"]:
                    raise SupervisorError(f"{provider} pre-created thread model does not match the request")
            if phase == "IDENTITY_VERIFIED" and item.get("session_id") != item.get("thread_id"):
                raise SupervisorError(f"{provider} verified identity is not the pre-created thread")
            if phase in ("AUTHORIZED","THREAD_CREATING") and "thread_id" in item:
                raise SupervisorError(f"{provider} preparation fields do not match its state")
        if phase in ("FRESH_PANE_CREATED","AGENT_STARTING","IDENTITY_VERIFIED") and not _valid_pane(item.get("pane_id")):
            raise SupervisorError(f"{provider} prepared pane is missing")
        expected_name=f"{provider}-run-{preparation_id[:8]}"
        if phase in ("AGENT_STARTING","IDENTITY_VERIFIED") and item.get("agent_name") != expected_name:
            raise SupervisorError(f"{provider} prepared agent name is invalid")
        if phase=="IDENTITY_VERIFIED" and not _valid_uuid(item.get("session_id")):
            raise SupervisorError(f"{provider} prepared identity is missing")
        if phase in ("AUTHORIZED","THREAD_CREATING","THREAD_CREATED","PANE_CREATING") and any(key in item for key in ("pane_id","agent_name","session_id")):
            raise SupervisorError(f"{provider} preparation fields do not match its state")
        if phase=="FRESH_PANE_CREATED" and any(key in item for key in ("agent_name","session_id")):
            raise SupervisorError(f"{provider} preparation fields do not match its state")
        if phase=="AGENT_STARTING" and "session_id" in item:
            raise SupervisorError(f"{provider} preparation fields do not match its state")
    occupied_panes={owners_before[provider]["pane_id"] for provider in PROVIDERS}
    for provider,item in providers.items():
        if item["status"] not in ("FRESH_PANE_CREATED","AGENT_STARTING","IDENTITY_VERIFIED"):
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


def _verify_started_identity(supervisor: Any, provider: str, item: dict[str, Any], live: list[dict[str, Any]]) -> str:
    name=item.get("agent_name") or f"{provider}-run-{item.get('preparation_id','')[:8]}"
    candidates=[a for a in live if a.get("name")==name and a.get("agent")==provider and a.get("pane_id")==item.get("pane_id")]
    if len(candidates)!=1 or candidates[0].get("agent_status") not in READY_STATES:
        raise SupervisorError(f"fresh {provider} agent did not become uniquely ready in its prepared pane")
    identity=session_identity(candidates[0])
    if not identity or any(session_identity(a)==identity for a in live if a is not candidates[0]):
        raise SupervisorError(f"fresh {provider} agent has a missing or duplicate native session identity")
    if provider in THREAD_PROVIDERS and identity != item.get("thread_id"):
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


def _verify_owner_set(live: list[dict[str, Any]], owners: dict[str, dict[str, str]], *, label: str) -> None:
    for provider in PROVIDERS:
        expected=owners[provider]
        match=match_exact_session(live,provider,expected["session_id"])
        if not match.unique or match.record is None or match.record.get("pane_id") != expected["pane_id"]:
            raise SupervisorError(f"cannot verify {label} {provider} owner in its exact pane")


def prepare_sessions(
    supervisor: Any,
    selection: Any,
    *,
    preparation_id: str,
    journal_path: Path | None = None,
    origin: str = "supervisor",
    request_binding: str | None = None,
    request_envelope: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Prepare selected fresh sessions exactly once, then atomically replace ownership.

    An interrupted pane-create or agent-start call is intentionally ambiguous and fails closed. The
    operation is never repeated automatically because Herdr has no idempotency key for either action.
    """
    selected = validate_selection(supervisor.config, selection)
    fresh = tuple(selected["fresh_providers"])
    valid_binding = isinstance(request_binding,str) and len(request_binding)==64 and all(c in "0123456789abcdef" for c in request_binding)
    valid_envelope = False
    if origin == "cli" and isinstance(request_envelope, dict):
        valid_envelope = cli_request_binding(supervisor.config, request_envelope) == request_binding
    if origin not in ("cli","supervisor") or (origin=="cli") != valid_binding or (origin=="cli") != valid_envelope or (origin=="supervisor" and (request_binding is not None or request_envelope is not None)):
        raise SupervisorError("session preparation origin or request binding is invalid")
    if origin == "cli" and validate_cli_request_envelope(supervisor.config, request_envelope).get("session_selection") != selected:
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
                raise SupervisorError(f"session preparation {other.stem} is unresolved; inspect it or run `herdr-supervisor abandon-session-start --preparation-id {other.stem}`")
    owners = supervisor.verify_or_seed_owners()
    if not fresh:
        return {"status": "OWNERSHIP_BOUND", "selection": selected, "owners": owners, "providers": {}}
    contract_probe = getattr(supervisor.herdr, "capability_report", None)
    contract = contract_probe() if callable(contract_probe) else None
    if not session_contract_supported(supervisor.config, contract):
        raise SupervisorError("fresh session creation unavailable: the supported Herdr pane split and agent-start contract was not verified")
    # Every read-only provider preflight completes before the first thread/pane/session side effect. This also
    # prevents fresh-all from partially preparing one provider before discovering the other's capability is absent.
    if any(provider in THREAD_PROVIDERS for provider in fresh):
        components = fresh_codex_components(supervisor.config, contract, supervisor.codex_thread_contract())
        missing = [name for name, ok in components.items() if not ok]
        if missing:
            raise SupervisorError("fresh Codex unavailable: unverified capability " + ", ".join(missing))
    for provider in fresh:
        if provider in THREAD_PROVIDERS:
            continue  # the model is set on the pre-created thread, never through a launch flag
        if model_args(supervisor.config,provider,selected["profiles"][provider]) and not supervisor.model_flag_supported(provider):
            raise SupervisorError(f"local {provider} CLI does not expose the required --model capability")
    path = journal_path or supervisor.paths.session_preparations_dir / f"{preparation_id}.json"
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
            "providers": {p: {"status": "AUTHORIZED", "old_pane_id": owners[p]["pane_id"], "old_session_id": owners[p]["session_id"], "profile": selected["profiles"][p], "preparation_id": preparation_id} for p in fresh},
        }
        atomic_write_json(path, record)
    owners_before=record["owners_before"]
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
        if phase == "AUTHORIZED" and provider in THREAD_PROVIDERS:
            # Persist the intent (request id + exact payload) before the non-idempotent thread/start call.
            payload = thread_start_payload(supervisor.config, provider, item["profile"])
            item.update({"status": "THREAD_CREATING", "thread_request_id": thread_request_id(preparation_id, provider), "thread_payload": payload})
            atomic_write_json(path, record)
            phase = "THREAD_CREATING"
            try:
                thread = supervisor.create_codex_thread(item["thread_payload"], item["thread_request_id"])
            except SupervisorError as error:
                raise SupervisorError(f"{provider} thread creation for preparation {preparation_id} is unresolved ({error}); nothing was repeated and no pane was created. Inspect the helper journal, retry the same task start to reconcile a settled result, or run `herdr-supervisor abandon-session-start --preparation-id {preparation_id}`") from error
            _adopt_thread(item, thread)
            atomic_write_json(path, record)
            phase = "THREAD_CREATED"
        elif phase == "THREAD_CREATING":
            # Recovery: reconcile a settled helper result for this exact request; an unknown outcome stays
            # reconciling until the operator explicitly abandons. Never a second thread/start.
            try:
                settled = supervisor.settled_codex_thread(item["thread_payload"], item["thread_request_id"])
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
        if phase == "AGENT_STARTING":
            identity=_verify_started_identity(supervisor,provider,item,supervisor.herdr.list_agents())
            item.update({"status":"IDENTITY_VERIFIED","session_id":identity})
            atomic_write_json(path,record)
            phase="IDENTITY_VERIFIED"
        if phase == "AUTHORIZED":
            intended_name=f"{provider}-run-{preparation_id[:8]}"
            if any(a.get("name")==intended_name for a in supervisor.herdr.list_agents()):
                raise SupervisorError(f"fresh {provider} agent name already exists; no pane was created")
            item["status"] = "PANE_CREATING"
            atomic_write_json(path, record)
            pane = supervisor.herdr.split_pane(
                owners_before[provider]["pane_id"], direction=supervisor.config["session_start"]["direction"],
                ratio=float(supervisor.config["session_start"]["ratio"]), cwd=supervisor.config["project_root"],
            )
            occupied={owners_before[p]["pane_id"] for p in PROVIDERS}
            occupied.update(
                other["pane_id"] for other in record["providers"].values()
                if other is not item and other.get("status") in ("FRESH_PANE_CREATED","AGENT_STARTING","IDENTITY_VERIFIED")
            )
            if not _valid_pane(pane) or pane in occupied:
                raise SupervisorError(f"Herdr did not return a canonical distinct fresh {provider} pane")
            # The returned identity is durable before any further probe. A crash or failed availability
            # check can now reconcile this exact pane and can never cause another split.
            item.update({"pane_id": pane, "status": "FRESH_PANE_CREATED"})
            atomic_write_json(path, record)
            phase = "FRESH_PANE_CREATED"
        if phase == "FRESH_PANE_CREATED":
            if not supervisor.herdr.pane_available(item["pane_id"]):
                raise SupervisorError(f"fresh {provider} pane is not an empty shell pane; no agent was started")
            profile = item["profile"]
            if provider in THREAD_PROVIDERS:
                args = ["resume", item["thread_id"]]  # the pre-created durable thread; the model is already set on it
            else:
                args = model_args(supervisor.config, provider, profile)
            name = f"{provider}-run-{preparation_id[:8]}"
            item.update({"status": "AGENT_STARTING", "agent_name": name})
            atomic_write_json(path, record)
            supervisor.herdr.start_agent(name, kind=provider, pane_id=item["pane_id"], args=args)
            identity=_verify_started_identity(supervisor,provider,item,supervisor.herdr.list_agents())
            item.update({"status": "IDENTITY_VERIFIED", "session_id": identity})
            atomic_write_json(path, record)
        elif phase != "IDENTITY_VERIFIED":
            raise SupervisorError(f"invalid {provider} session preparation state: {phase}")
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
    record["status"] = "BINDING"
    atomic_write_json(path, record)
    atomic_write_json(supervisor.paths.owners_file, new_owners, mode=0o600)
    record.update({"status": "OWNERSHIP_BOUND", "bound_at": iso_utc(supervisor.clock()), "bound_owners": new_owners})
    atomic_write_json(path, record)
    supervisor._live_names.clear()
    return record
