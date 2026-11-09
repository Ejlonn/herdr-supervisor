#!/usr/bin/env python3
"""herdr-setup: preview-first setup, update, status and rollback of a Herdr Supervisor installation.

    herdr-setup setup  [--profile minimal|full] ...   preview (default) | --apply with a saved plan and its hash
    herdr-setup update [--adopt-legacy-from DIR] ...  replace the installed release from a reviewed local tree
    herdr-setup status                                read-only: installation, readiness and what is left to do
    herdr-setup rollback                              undo the last operation (previous release and its assets)

The default invocation previews and writes nothing, contacts no network and activates nothing. A plan file is an
explicit output (`--plan-out`). `--apply` spends one approval hash that is bound to the candidate content, the options,
the resolved target and the exact preimages; any change since the preview makes it stale. Standard library only.
"""

from __future__ import annotations

import argparse
import collections
import dataclasses
import getpass
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import weakref
from collections.abc import Callable
from pathlib import Path
from typing import Any

import herdr_core as core
import herdr_quota_dependency as hq
import herdr_telegram as htg
from herdr_cli import match_exact_session
from herdr_installation import (
    EXIT_ACTION,
    EXIT_OK,
    EXIT_RECONCILING,
    EXIT_REFUSED,
    EXIT_USAGE,
    UNIT_FOR_SERVICE,
    Env,
    InstallError,
    Layout,
    Payload,
    Step,
    Systemd,
    create_file_exclusive,
    default_payload_root,
    describe,
    inspect_install,
    load_payload,
    read_bytes_nofollow,
    tool_identity,
    user_name,
    verify_units,
    workload_problems,
    write_file_atomic,
)
from herdr_session_catalog import canonical_identity
from herdr_update import (
    ApplyResult,
    Ctx,
    Plan,
    apply_operation,
    describe_steps,
    file_steps,
    load_plan_document,
    plan_installation,
    plan_rollback,
    preflight_units,
)

PROFILES = ("minimal", "full")
COMPONENTS = ("paths", "sessions", "telegram", "quota")
DEFAULT_FULL_COMPONENTS = ("sessions", "telegram", "quota")
SERVICES = tuple(UNIT_FOR_SERVICE)
PLUGIN_ID = "herdr-agent-quota"
MIN_PYTHON = (3, 11)
PATH_KEYS = ("project_root", "review_root", "product_repo")


# ------------------------------------------------------------------------------------------------ adapters


class HerdrProbe:
    """Read-only questions to the Herdr CLI, through the host adapter."""

    def __init__(self, env: Env, binary: str | None = None) -> None:
        self.env = env
        self.binary = binary or self._locate()

    def _locate(self) -> str | None:
        supported = self.env.layout.bin / "herdr"
        if supported.is_file() and os.access(supported, os.X_OK):
            return str(supported)
        return self.env.host.which("herdr")

    def version(self) -> str | None:
        if self.binary is None:
            return None
        result = self.env.host.run([self.binary, "--version"], timeout=30)
        return result.out.strip() or None if result.rc == 0 else None

    def agents(self) -> list[dict[str, Any]] | None:
        """The live agent list, or None when no server answers (never guessed)."""
        if self.binary is None:
            return None
        result = self.env.host.run([self.binary, "agent", "list"], timeout=30)
        if result.rc != 0:
            return None
        try:
            agents = json.loads(result.out)["result"]["agents"]
        except (ValueError, KeyError, TypeError):
            return None
        return [item for item in agents if isinstance(item, dict)] if isinstance(agents, list) else None


class QuotaAdapter:
    """The existing quota-dependency contract: check/bootstrap run through its own module, link/enable through the
    two Herdr commands whose help text is recorded in tests/fixtures/quota-dependency.

    The dependency lock is selected ONCE per plan (`bind_lock`): its exact bytes are validated, hashed into the approval and written
    to a private file that every later call uses, so a plan can never run against a lock other than the approved one."""

    def __init__(self, env: Env, probe: HerdrProbe, options: dict[str, Any]) -> None:
        self.env = env
        self.probe = probe
        self.options = options
        self._lock: dict[str, Any] | None = None
        self._lock_path: str | None = None
        self.lock_sha256: str | None = None

    def bind_lock(self, data: bytes) -> dict[str, Any]:
        lock = hq.validate_lock(json.loads(data.decode("utf-8")))
        directory = tempfile.mkdtemp(prefix="herdr-lock-")
        weakref.finalize(self, shutil.rmtree, directory, True)
        path = Path(directory) / hq.LOCK_NAME
        path.write_bytes(data)
        path.chmod(0o600)
        self._lock, self._lock_path, self.lock_sha256 = lock, str(path), hashlib.sha256(data).hexdigest()
        return lock

    def _common(self) -> list[str]:
        if self._lock_path:
            return ["--lock", self._lock_path]
        return ["--lock", str(self.options["lock"])] if self.options.get("lock") else []

    def _herdr(self) -> list[str]:
        return ["--herdr", self.probe.binary] if self.probe.binary else []

    def _lock_dict(self) -> dict[str, Any]:
        return self._lock if self._lock is not None else hq.load_lock(self.options.get("lock"))

    def check(self) -> tuple[int, dict[str, Any]]:
        parser = hq.build_parser()
        args = parser.parse_args([*self._common(), "check", *self._herdr()])
        try:
            return hq.command_check(args)
        except hq.DependencyError as error:
            return error.code, {"error": str(error), "action": error.action, "dependency": {"state": "unknown", "detail": str(error)}}

    def bootstrap(self, stage_dir: str, *, apply: bool) -> tuple[int, dict[str, Any]]:
        argv = [*self._common(), "bootstrap", "--stage-dir", stage_dir, *self._herdr()]
        if apply:
            argv.append("--apply")
        for flag, key in (("--from-archive", "archive"), ("--from-bundle", "bundle")):
            if self.options.get(key):
                argv += [flag, str(self.options[key])]
        if self.options.get("offline"):
            argv.append("--offline")
        args = hq.build_parser().parse_args(argv)
        try:
            return hq.command_bootstrap(args)
        except hq.DependencyError as error:
            return error.code, {"error": str(error), "action": error.action}

    def verify_stage(self, stage_dir: str) -> str:
        """absent | verified | unverified. `verified` means the staged tree matches the bound lock completely (critical files, manifest, git tree),
        the bootstrap receipt names that revision, and the build output exists; a receipt or directory name alone proves nothing."""
        stage = Path(stage_dir)
        try:
            lock = self._lock_dict()
            if not os.path.lexists(stage) or (stage.is_dir() and not any(stage.iterdir())):
                return "absent"
            staged = stage / f"herdr-agent-quota-{lock['revision'][:12]}"
            if staged.is_symlink() or not staged.is_dir():
                return "unverified"
            hq.verify_source(hq.scan_tree(staged), staged, lock)
            receipt = json.loads((stage / hq.RECEIPT_FILE).read_text(encoding="utf-8"))
            if not isinstance(receipt, dict) or receipt.get("revision") != lock["revision"] or receipt.get("plugin_id") != lock["plugin_id"]:
                return "unverified"
            return "verified" if (staged / "target/release/herdr-agent-quota").is_file() else "unverified"
        except (hq.DependencyError, OSError, ValueError):
            return "unverified"

    def plugin_state(self) -> dict[str, Any]:
        """The inventory's view of the plugin: classification against the bound lock, enablement, and the real path it is linked from."""
        try:
            lock = self._lock_dict()
            inventory = hq.read_inventory(self.probe.binary)
            dependency = hq.classify(inventory, lock)
        except (hq.DependencyError, OSError, KeyError, ValueError) as error:
            return {"state": "unknown", "enabled": None, "root": None, "detail": str(error)}
        matches = [item for item in inventory.get("plugins", []) if isinstance(item, dict) and item.get("plugin_id") == lock["plugin_id"]] if inventory.get("status") == "OK" else []
        root = matches[0].get("plugin_root") if len(matches) == 1 else None
        return {"state": dependency.get("state"), "enabled": dependency.get("enabled"), "root": os.path.realpath(root) if isinstance(root, str) else None}

    def link(self, directory: str) -> None:
        self._plugin(["link", "--disabled", directory])

    def enable(self) -> None:
        self._plugin(["enable", PLUGIN_ID])

    def _plugin(self, args: list[str]) -> None:
        if self.probe.binary is None:
            raise InstallError("the herdr executable is not available", code=EXIT_ACTION)
        result = self.env.host.run([self.probe.binary, "plugin", *args], timeout=120)
        if result.rc != 0:
            raise InstallError(f"`herdr plugin {args[0]}` failed (exit {result.rc}): {(result.err or result.out).strip()[-300:]}", action="inspect `herdr plugin list --json`; nothing was repeated")


@dataclasses.dataclass
class Adapters:
    probe: HerdrProbe
    quota: QuotaAdapter
    secret: Callable[[str], str] = getpass.getpass
    interactive: bool = False

    @classmethod
    def for_env(cls, env: Env, options: dict[str, Any], *, interactive: bool = False) -> Adapters:
        probe = HerdrProbe(env)
        return cls(probe=probe, quota=QuotaAdapter(env, probe, options.get("quota", {})), interactive=interactive)


# ------------------------------------------------------------------------------------------------ option handling


def _abs(value: str | None) -> str | None:
    return None if value is None else str(Path(value).expanduser().resolve(strict=False))


def split_list(value: str | None, allowed: tuple[str, ...], label: str) -> list[str]:
    if not value:
        return []
    items = sorted({part.strip() for part in value.split(",") if part.strip()})
    unknown = [item for item in items if item not in allowed]
    if unknown:
        raise InstallError(f"unknown {label} {', '.join(unknown)} (choose from {', '.join(allowed)})", code=EXIT_USAGE)
    return items


def options_from_args(args: argparse.Namespace, kind: str) -> dict[str, Any]:
    options: dict[str, Any] = {"migrate": bool(getattr(args, "migrate", False)), "adopt_legacy_from": _abs(getattr(args, "adopt_legacy_from", None))}
    if kind != "setup":
        return {**options, "profile": None, "components": [], "services": [], "paths": {}, "telegram": {}, "quota": {}, "interactive": False}
    profile = args.profile
    components = split_list(args.components, COMPONENTS, "component")
    if profile == "minimal" and (components or args.services or any(getattr(args, key) for key in PATH_KEYS) or args.telegram_owner_id is not None or args.telegram_token_file):
        raise InstallError("component, service, path and Telegram options belong to --profile full", code=EXIT_USAGE)
    paths = {key: _abs(getattr(args, key)) for key in PATH_KEYS if getattr(args, key)}
    if profile == "full" and not components:
        components = list(DEFAULT_FULL_COMPONENTS) + (["paths"] if paths else [])
    if paths and "paths" not in components:
        components = sorted({*components, "paths"})
    telegram = {"owner_user_id": args.telegram_owner_id, "chat_id": args.telegram_chat_id, "timezone": args.telegram_timezone, "token_file": _abs(args.telegram_token_file)}
    quota = {"stage_dir": _abs(args.quota_stage_dir), "archive": _abs(args.quota_archive), "bundle": _abs(args.quota_bundle), "offline": bool(args.quota_offline), "lock": _abs(args.quota_lock)}
    return {**options, "profile": profile, "components": sorted(set(components)), "services": split_list(args.services, SERVICES, "service"), "paths": paths, "telegram": telegram,
            "quota": quota, "interactive": bool(args.interactive)}


# ------------------------------------------------------------------------------------------------ prerequisites


def platform_prerequisites(env: Env, probe: HerdrProbe) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []

    def add(item: str, ok: bool, detail: str, action: str = "") -> None:
        items.append({"id": item, "status": "OK" if ok else "ACTION_REQUIRED", "detail": detail, "action": action})

    add("python", sys.version_info >= MIN_PYTHON, f"Python {sys.version_info.major}.{sys.version_info.minor}", "install Python 3.11 or newer" if sys.version_info < MIN_PYTHON else "")
    add("platform", sys.platform.startswith("linux"), sys.platform, "Herdr Supervisor supports Linux with user systemd")
    supported = env.layout.bin / "herdr"
    add("herdr", supported.is_file() and os.access(supported, os.X_OK), f"{supported}", f"install Herdr user-scoped at {supported} (the headless unit runs exactly that path); see the Herdr documentation")
    for provider in core.PROVIDERS:
        found = env.host.which(provider)
        add(f"{provider}-cli", found is not None, found or f"`{provider}` is not on PATH", f"install the {provider} CLI and log in yourself; Supervisor never installs or authenticates providers")
    return items


# ------------------------------------------------------------------------------------------------ plan assembly


def _example_json(payload: Payload, name: str) -> dict[str, Any]:
    value = json.loads(payload.blobs[f"example/{name}"])
    return value if isinstance(value, dict) else {}


def _read_json_file(path: Path) -> dict[str, Any] | None:
    state = describe(path)
    if state["state"] != "file":
        return None
    try:
        value = json.loads(read_bytes_nofollow(path))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _plan_paths(env: Env, plan: Plan, payload: Payload, options: dict[str, Any]) -> dict[str, Any] | None:
    wanted = options["paths"]
    if "paths" not in options["components"] or not wanted:
        return None
    layout = env.layout
    current = _read_json_file(layout.sup_config) or _example_json(payload, "supervisor.example.json")
    sets: dict[str, str] = {}
    for key, value in sorted(wanted.items()):
        if not Path(value).is_absolute() or not Path(value).is_dir():
            plan.blockers.append(f"{key} {value} is not an existing absolute directory")
            continue
        existing = current.get(key)
        if existing not in (None, value):
            plan.blockers.append(f"the configuration already sets {key} to {existing}; it is preserved (edit it yourself to change it)")
            continue
        if existing != value:
            sets[key] = value
    root = wanted.get("project_root") or current.get("project_root") or os.environ.get("HERDR_SUPERVISOR_PROJECT_ROOT")
    if root and not ((Path(root) / "AGENTS.md").is_file() or (Path(root) / "CLAUDE.md").is_file()):
        plan.prerequisites.append({"id": "project-instructions", "status": "ACTION_REQUIRED", "detail": f"{root} has no AGENTS.md or CLAUDE.md", "action": "add the repository instructions the agents read (doctor requires one of them)"})
    if not sets:
        return None
    plan.data.setdefault("config_edits", []).append(str(layout.sup_config))
    plan.data["preimages"][str(layout.sup_config)] = describe(layout.sup_config)
    return {"path": str(layout.sup_config), "set": sets}


def owners_report(layout: Layout, agents: list[dict[str, Any]] | None) -> dict[str, Any]:
    """Validate recorded native-session ownership with the supervisor's own identity rule (`match_exact_session`: exactly one live record
    carrying both the provider and the exact session id; aliases are labels and never match). status: absent | malformed |
    server_unreachable | stale | ok. Nothing is repaired, rebound or written."""
    state = describe(layout.paths.owners_file)
    if state["state"] == "absent":
        return {"status": "absent", "problems": ["no session pair is enrolled yet"]}
    owners = _read_json_file(layout.paths.owners_file)
    problems: list[str] = []
    for provider in core.PROVIDERS:
        record = (owners or {}).get(provider)
        if not (isinstance(record, dict) and set(record) == {"pane_id", "session_id"} and core.valid_pane_id(record["pane_id"]) and canonical_identity(record["session_id"])):
            problems.append(f"{provider}: the recorded identity is missing or malformed")
    if owners is None or problems:
        return {"status": "malformed", "problems": problems or ["owners.json is not a readable object"]}
    if agents is None:
        return {"status": "server_unreachable", "problems": ["no Herdr server answers, so the recorded sessions cannot be matched to live agents"]}
    for provider in core.PROVIDERS:
        record = owners[provider]
        match = match_exact_session(agents, provider, record["session_id"])
        if match.duplicates > 1:
            problems.append(f"{provider}: the recorded session is carried by {match.duplicates} live records (duplicate identity)")
        elif match.provider_conflicts:
            problems.append(f"{provider}: the recorded session is carried by another provider's agent")
        elif match.record is None:
            problems.append(f"{provider}: the recorded session was not found among the live agents")
        elif match.record.get("pane_id") != record["pane_id"]:
            problems.append(f"{provider}: the recorded session is live in pane {match.record.get('pane_id')}, not {record['pane_id']} (explicit pane repair is yours to run)")
    return {"status": "stale" if problems else "ok", "problems": problems}


def _plan_sessions(env: Env, plan: Plan, adapters: Adapters) -> None:
    """Existing-session selection stays with the supported ownership mechanism; setup only reports what it can see, by exact identity."""
    layout = env.layout
    agents = adapters.probe.agents()
    report = owners_report(layout, agents)
    if report["status"] == "ok":
        plan.notes.append("native session ownership is recorded and matches exactly one live agent per provider; it is preserved and never rebound by setup")
        return
    if report["status"] in ("malformed", "stale", "server_unreachable"):
        plan.prerequisites.append({"id": "sessions", "status": "ACTION_REQUIRED", "detail": "; ".join(report["problems"]),
                                   "action": "inspect `herdr-supervisor sessions`; ownership records are preserved and never rebound, repaired or created by setup"})
        return
    names = {provider: core.DEFAULT_CONFIG["agents"][provider]["name"] for provider in core.PROVIDERS}
    config = _read_json_file(layout.sup_config) or {}
    for provider in core.PROVIDERS:
        configured = ((config.get("agents") or {}).get(provider) or {}).get("name")
        if isinstance(configured, str) and configured:
            names[provider] = configured
    if agents is None:
        detail = "no live Herdr server answered, so the existing agents cannot be listed"
    else:
        live = {agent.get("name"): agent.get("agent") for agent in agents}
        counts = collections.Counter(agent.get("name") for agent in agents)
        # Aliases are labels only: a name counts as visible only when exactly one live agent carries it and it is the expected provider.
        missing = [name for provider, name in names.items() if counts[name] != 1 or live.get(name) != provider]
        detail = "both agents are visible by name and will be enrolled by the first supervised task" if not missing else f"agents not found or ambiguous: {', '.join(missing)}"
        if not missing:
            plan.prerequisites.append({"id": "sessions", "status": "ACTION_REQUIRED", "detail": detail, "action": "confirm the pair with `herdr-supervisor sessions`; ownership is recorded when you start the first task (setup never creates, restarts or rebinds a session)"})
            return
    plan.prerequisites.append({"id": "sessions", "status": "ACTION_REQUIRED", "detail": detail,
                               "action": f"open Herdr and start your Codex ({names['codex']}) and Claude ({names['claude']}) sessions, log in, then check `herdr-supervisor sessions`; setup never creates or restarts a session"})


def token_fingerprint(text: str) -> str:
    """A short, salted digest that detects a changed token without exposing it (the token itself is never stored in a plan, journal or receipt)."""
    return hashlib.sha256(b"herdr-setup-token-v1:" + text.strip().encode("utf-8")).hexdigest()[:16]


def _plan_telegram(env: Env, plan: Plan, options: dict[str, Any], adapters: Adapters) -> dict[str, Any] | None:
    layout = env.layout
    current = _read_json_file(layout.tg_config)
    if current is not None and current.get("mode") not in (None, "unconfigured"):
        plan.notes.append("Telegram is already configured; the token, owner and mode are preserved")
        return None
    tg = options["telegram"]
    if tg.get("owner_user_id") is None:
        plan.prerequisites.append({"id": "telegram", "status": "ACTION_REQUIRED", "detail": "Telegram is not configured", "action": "create a bot with @BotFather, find your numeric owner id, then run setup with --telegram-owner-id (and --telegram-token-file, or run interactively)"})
        return None
    token_file = tg.get("token_file")
    fingerprint = None
    if token_file:
        try:
            htg.check_private_file(Path(token_file), "token source file")
            fingerprint = token_fingerprint(Path(token_file).read_text(encoding="utf-8"))
        except (core.SupervisorError, OSError) as error:
            plan.blockers.append(str(error))
            return None
        source = "file"
    elif adapters.interactive:
        source = "prompt"
    else:
        plan.prerequisites.append({"id": "telegram", "status": "ACTION_REQUIRED", "detail": "no token source: this noninteractive run cannot collect a secret", "action": "rerun with --telegram-token-file FILE (mode 0600), or run `herdr-setup setup --interactive`"})
        return None
    bot_token = layout.tg_config.parent / "bot-token"
    if describe(bot_token)["state"] != "absent":
        plan.blockers.append(f"{bot_token} already exists; it is a credential and is never overwritten (configure with `herdr-telegram setup` yourself or move it)")
        return None
    plan.data.setdefault("config_edits", []).append(str(layout.tg_config))
    plan.data["preimages"][str(layout.tg_config)] = describe(layout.tg_config)
    return {"owner_user_id": tg["owner_user_id"], "chat_id": tg.get("chat_id"), "timezone": tg.get("timezone") or htg.DEFAULT_TIMEZONE, "token_source": source, "token_file": token_file,
            "token_fingerprint": fingerprint, "stored_token_file": str(bot_token)}


def _dependency_identity(env: Env, plan: Plan, payload: Payload, options: dict[str, Any], adapters: Adapters) -> dict[str, Any] | None:
    """Select the dependency lock once (an explicit --quota-lock, else THIS payload's own lock, never the running tool's), validate it,
    and return its complete identity with the local acquisition artifacts, so a changed pin, source, artifact or stage needs a new approval."""
    quota = options["quota"]
    try:
        data = read_bytes_nofollow(Path(quota["lock"]), 1024 * 1024) if quota.get("lock") else payload.blobs["program/quota-dependency.json"]
        lock = adapters.quota.bind_lock(data)
        artifacts: dict[str, str | None] = {}
        for key in ("archive", "bundle"):
            artifacts[key] = hq.sha256_file(Path(quota[key])) if quota.get(key) else None
        identity = {"source": "explicit" if quota.get("lock") else "payload", "lock_sha256": hashlib.sha256(data).hexdigest(), "plugin_id": lock["plugin_id"], "revision": lock["revision"],
                    "tree_sha1": lock["tree_sha1"], "repository": lock["repository"]["url"], "toolchain": lock["toolchain"]["channel"], "artifacts": artifacts,
                    "stage_dir": quota.get("stage_dir"), "offline": bool(quota.get("offline"))}
    except (hq.DependencyError, OSError, ValueError, KeyError, TypeError) as error:
        plan.blockers.append(f"the quota dependency lock or artifact is not usable: {error}")
        return None
    return identity


def _plan_quota(env: Env, plan: Plan, options: dict[str, Any], adapters: Adapters) -> list[str]:
    quota = adapters.quota
    code, report = quota.check()
    dependency = report.get("dependency") or {}
    state = dependency.get("state")
    if state == "conflict":
        plan.blockers.append(f"the quota plugin is in conflict: {dependency.get('detail')}; nothing is overwritten")
        return []
    if state == "unknown":
        plan.prerequisites.append({"id": "quota-plugin", "status": "ACTION_REQUIRED", "detail": str(dependency.get("detail") or "the plugin inventory cannot be read"), "action": str(dependency.get("action") or "install Herdr and retry")})
        return []
    steps: list[str] = []
    if state == "absent":
        toolchain = report.get("toolchain") or {}
        stage = options["quota"]["stage_dir"]
        dry_code, dry = quota.bootstrap(stage, apply=False)
        if dry_code != EXIT_OK or (dry.get("toolchain") or toolchain).get("status") == "MISSING":
            reason = (dry.get("toolchain") or toolchain).get("action") or dry.get("action") or dry.get("result") or dry.get("error") or "the pinned plugin cannot be prepared on this machine"
            plan.prerequisites.append({"id": "quota-plugin", "status": "ACTION_REQUIRED", "detail": "the pinned quota plugin cannot be staged and built yet", "action": str(reason)})
            return []
        steps = ["quota_stage", "quota_link", "quota_enable"]
    elif state == "pinned" and dependency.get("enabled") is False:
        steps = ["quota_enable"]
    providers = (report.get("telemetry") or {}).get("providers") or {}
    if not steps and providers and all(item.get("state") == "FRESH" for item in providers.values()):
        plan.notes.append("the quota plugin is installed at the locked revision and reports fresh snapshots")
    else:
        plan.prerequisites.append({"id": "quota-hooks", "status": "ACTION_REQUIRED", "detail": "fresh quota snapshots are not confirmed",
                                   "action": f"after reading docs/QUOTA_DEPENDENCY.md, run `herdr plugin action invoke configure --plugin {PLUGIN_ID}` yourself: it edits provider hook files and reloads the Herdr server, so Supervisor never runs it"})
    return steps


def _plan_services(env: Env, plan: Plan, options: dict[str, Any], adapters: Adapters, payload: Payload, telegram_planned: bool) -> list[str]:
    selected = options["services"]
    if not selected:
        return []
    systemd = Systemd(env.host)
    units: list[str] = []
    available = {item.name for item in payload.of_role("unit")}
    if not systemd.available():
        plan.prerequisites.append({"id": "systemd", "status": "ACTION_REQUIRED", "detail": "the user systemd manager cannot be reached", "action": "run this from a login session with a user systemd bus (`systemctl --user` must work), then repeat with --services"})
        return []
    config = _read_json_file(env.layout.sup_config) or _example_json(payload, "supervisor.example.json")
    tg = _read_json_file(env.layout.tg_config)
    for name in selected:
        unit = UNIT_FOR_SERVICE[name]
        if unit not in available:
            plan.blockers.append(f"the payload has no {unit}")
            continue
        reason = None
        if name == "telegram" and not (telegram_planned or (tg is not None and tg.get("mode") not in (None, "unconfigured"))):
            reason = "Telegram is not configured"
        elif name == "query" and not (env.layout.paths.query_registry_file.exists() or env.layout.paths.query_owner_file.exists()):
            reason = "no read-only query session is registered"
        elif name == "reset" and not ((config.get("codex_reset") or {}).get("enabled", True) and env.host.which("codex")):
            reason = "banked-reset automation is disabled in the configuration or the codex CLI is missing"
        elif name == "backup":
            backup = _read_json_file(env.layout.backup_config) or {}
            if not (backup.get("enabled") is True and backup.get("destination")):
                reason = "backup is not enabled and configured (run `herdr-backup setup --confirm`)"
        elif name == "server":
            agents = adapters.probe.agents()
            if agents is not None:
                plan.blockers.append("an interactive Herdr server is already running; the headless unit would conflict and is not started")
                continue
            if adapters.probe.binary is None:
                reason = "the herdr executable is not installed"
        if reason:
            plan.prerequisites.append({"id": f"service-{name}", "status": "ACTION_REQUIRED", "detail": f"{unit} was not activated: {reason}", "action": "configure it, then repeat setup with --services"})
            continue
        if os.path.lexists(env.layout.unit / unit) and systemd.enabled(unit) in ("enabled", "enabled-runtime") and systemd.state(unit) == "active":
            plan.notes.append(f"{unit} is already enabled and active")
            continue
        units.append(unit)
    return units


def build_plan(env: Env, kind: str, options: dict[str, Any], payload_root: Path | None, adapters: Adapters | None = None) -> Plan:
    """The single plan builder used for preview and for the stale-approval check at apply time."""
    adapters = adapters or Adapters.for_env(env, options, interactive=bool(options.get("interactive")))
    data: dict[str, Any] = {"schema": 1, "kind": kind, "tool": tool_identity(), "target": env.layout.identity(), "options": options,
                            "payload_root": str(payload_root) if payload_root is not None else None}
    plan = Plan(data=data)
    payload: Payload | None = None
    if kind == "rollback":
        sections, blockers, notes = plan_rollback(env, options)
    else:
        if payload_root is None:
            raise InstallError("no payload tree was found next to this tool", code=EXIT_ACTION,
                               action="run from a source checkout or release tree, or pass --payload DIR; a wheel contains the console tools but not the units, launchers and documents")
        payload = load_payload(payload_root)
        sections, blockers, notes = plan_installation(env, kind, payload, options)
    data.update(sections)
    data.setdefault("preimages", {})
    data.setdefault("services", {"stop": [], "restore": [], "reload": False, "systemd": "not_needed"})
    data.setdefault("candidate", {})
    data.setdefault("backup", None)
    plan.blockers += blockers
    plan.notes += notes
    if kind == "setup" and options["profile"] == "minimal":
        supported = env.layout.bin / "herdr"
        if not (supported.is_file() and os.access(supported, os.X_OK)):
            plan.blockers.append(f"Herdr executable not found at {supported}; install Herdr user-scoped first (the headless unit runs exactly that path)")
    components: dict[str, Any] = {"config_edit": None, "telegram": None, "quota": [], "activate": []}
    if kind == "setup" and options["profile"] == "full" and payload is not None:
        plan.prerequisites += platform_prerequisites(env, adapters.probe)
        selected = options["components"]
        if "paths" in selected:
            components["config_edit"] = _plan_paths(env, plan, payload, options)
        if "sessions" in selected:
            _plan_sessions(env, plan, adapters)
        if "telegram" in selected:
            components["telegram"] = _plan_telegram(env, plan, options, adapters)
        if "quota" in selected:
            options["quota"]["stage_dir"] = options["quota"].get("stage_dir") or str(env.layout.home / ".local/share/herdr-supervisor/quota-stage")
            data["dependency"] = _dependency_identity(env, plan, payload, options, adapters)
            if data["dependency"] is not None:
                components["quota"] = _plan_quota(env, plan, options, adapters)
        components["activate"] = _plan_services(env, plan, options, adapters, payload, components["telegram"] is not None)
        verdict = preflight_units(env, payload) if (data.get("files_needed") or components["activate"]) else {"status": "not needed"}
        if verdict["status"] == "failed":
            plan.blockers.append("unit validation failed: " + verdict["detail"])
        data["unit_validation"] = verdict["status"]
        if verdict["status"] == "skipped":
            plan.notes.append(f"Skipped unit validation: {verdict['detail']}")
    elif payload is not None and data.get("files_needed"):
        verdict = preflight_units(env, payload)
        if verdict["status"] == "failed":
            plan.blockers.append("unit validation failed: " + verdict["detail"])
        data["unit_validation"] = verdict["status"]
        if verdict["status"] == "skipped":
            plan.notes.append(f"Skipped unit validation: {verdict['detail']}")
    data["components"] = components
    ctx = Ctx(env, kind, data, payload, "0" * 32, "")
    unusable = kind == "rollback" and not data["backup"]  # nothing to roll back to: the refusal is the whole plan
    steps = [] if unusable else file_steps(ctx) + component_steps(ctx, adapters)
    data["actions"] = describe_steps(steps)
    files_noop = bool(data.get("noop_files")) if kind != "rollback" else False
    data["noop"] = files_noop and not component_steps(ctx, adapters)
    data["kind"] = kind
    if kind == "setup" and not data["noop"]:
        if data["installed"].get("layout") != "absent" and not data.get("files_needed"):
            # component-only changes to an installed system follow the same rule as any other mutation
            plan.blockers += [f"not at a quiet point: {problem}" for problem in workload_problems(env.layout, probe_lock=not env.worker_lock_held)]
        elif not env.worker_lock_held:
            try:
                held = core.WorkerLock.is_held(env.layout.paths.lock_file)
            except OSError:
                held = True
            if held:
                plan.blockers.append("not at a quiet point: a supervisor worker holds the worker lock; finish or pause the task, then retry")
    return plan


# ------------------------------------------------------------------------------------------------ component steps


def step_config_paths(ctx: Ctx) -> str:
    edit = ctx.plan["components"]["config_edit"]
    path = Path(edit["path"])
    assert ctx.payload is not None
    current = _read_json_file(path)
    if current is None:
        raise InstallError(f"{path} is missing or unreadable; paths were not set")
    merged = dict(current)
    for key, value in edit["set"].items():
        if merged.get(key) not in (None, value):
            raise InstallError(f"{key} changed in {path} after the plan was approved; it was not overwritten", code=EXIT_REFUSED)
        merged[key] = value
    if merged == current:
        return "paths already set"
    scratch = path.with_name(f".{path.name}.validate.{os.getpid()}")
    try:
        scratch.write_text(json.dumps(merged))
        core.load_config(scratch)
    except core.SupervisorError as error:
        raise InstallError(f"the edited configuration would not load: {error}") from error
    finally:
        scratch.unlink(missing_ok=True)
    write_file_atomic(path, (json.dumps(merged, indent=2, sort_keys=True) + "\n").encode("utf-8"), 0o600)
    return f"set {', '.join(sorted(edit['set']))}"


def _telegram_paths(layout: Layout) -> htg.TelegramPaths:
    return htg.TelegramPaths(config_file=layout.tg_config, state_dir=layout.tg_state)


def telegram_state(ctx: Ctx) -> str:
    """not_done | done | unknown, from the exact outcome the plan asked for. A token file or configuration that exists in any other
    combination (partial write, different owner/chat/timezone/path, changed token) is `unknown`: it is never overwritten or repeated over."""
    spec = ctx.plan["components"]["telegram"]
    config = _read_json_file(ctx.layout.tg_config)
    token = describe(Path(spec["stored_token_file"]))
    configured = config is not None and config.get("mode") not in (None, "unconfigured")
    if not configured and token["state"] == "absent":
        return "not_done"
    if not configured or token["state"] != "file":
        return "unknown"
    assert config is not None
    expected = {"owner_user_id": spec["owner_user_id"], "chat_id": spec["chat_id"], "timezone": spec["timezone"], "token_file": spec["stored_token_file"]}
    if any(config.get(key) != value for key, value in expected.items()):
        return "unknown"
    try:
        stored = htg.read_token(Path(spec["stored_token_file"]))
    except core.SupervisorError:
        return "unknown"
    if spec.get("token_fingerprint") and token_fingerprint(stored) != spec["token_fingerprint"]:
        return "unknown"
    return "done"


def step_telegram(ctx: Ctx, adapters: Adapters) -> str:
    spec = ctx.plan["components"]["telegram"]
    if telegram_state(ctx) != "not_done":
        raise InstallError("a Telegram token or configuration already exists in a state this plan did not approve; it was not overwritten", code=EXIT_REFUSED,
                           action="inspect ~/.config/herdr-telegram (nothing was changed); move aside what you do not want kept, then apply a new plan")
    token_source = Path(spec["token_file"]) if spec["token_source"] == "file" else None
    if token_source is None and not adapters.interactive:
        raise InstallError("a token is needed and this run cannot prompt for one", code=EXIT_ACTION, action="rerun with --telegram-token-file FILE, or interactively")
    if token_source is not None:
        try:
            changed = token_fingerprint(token_source.read_text(encoding="utf-8")) != spec.get("token_fingerprint")
        except OSError as error:
            raise InstallError(f"the token source file cannot be read: {error.strerror}") from error
        if changed:
            raise InstallError("the token source file changed after the plan was approved; nothing was written", code=EXIT_REFUSED)
    try:
        htg.setup(_telegram_paths(ctx.layout), owner_user_id=int(spec["owner_user_id"]), chat_id=spec["chat_id"], timezone=spec["timezone"], token_source=token_source, ask_secret=adapters.secret)
    except core.SupervisorError as error:
        raise InstallError(f"Telegram setup stopped: {error}", action="nothing was written for an invalid token") from error
    return "Telegram stored in shadow mode (token file 0600)"


def _bound_lock(ctx: Ctx, adapters: Adapters) -> dict[str, Any]:
    dependency = ctx.plan.get("dependency") or {}
    if adapters.quota.lock_sha256 != dependency.get("lock_sha256"):
        raise InstallError("the dependency lock in use is not the one that was approved", code=EXIT_REFUSED)
    return dependency


def component_steps(ctx: Ctx, adapters: Adapters) -> list[Step]:
    comp = ctx.plan.get("components") or {}
    steps: list[Step] = []
    if comp.get("config_edit"):
        steps.append(Step("config_paths", "set the requested project/review paths in the configuration", lambda: step_config_paths(ctx)))
    if comp.get("telegram"):
        steps.append(Step("telegram_setup", "store the Telegram bot token (protected input) and owner in shadow mode", lambda: step_telegram(ctx, adapters), external=True,
                          reconcile=lambda: telegram_state(ctx)))
    quota = comp.get("quota") or []
    dependency = ctx.plan.get("dependency") or {}
    stage = dependency.get("stage_dir")
    staged = os.path.realpath(Path(str(stage)) / f"herdr-agent-quota-{str(dependency.get('revision'))[:12]}") if stage else None
    expect_root = staged if "quota_link" in quota else None

    def ours(state: dict[str, Any]) -> bool:
        return state.get("state") == "pinned" and (expect_root is None or state.get("root") == expect_root)

    if "quota_stage" in quota:
        def stage_run() -> str:
            _bound_lock(ctx, adapters)
            if adapters.quota.verify_stage(str(stage)) != "absent":
                raise InstallError(f"the stage directory {stage} already has content; it is never reused or overwritten", code=EXIT_REFUSED)
            code, report = adapters.quota.bootstrap(str(stage), apply=True)
            if code != EXIT_OK:
                raise InstallError(f"quota staging stopped: {report.get('result') or report.get('error')}", code=EXIT_ACTION if code == EXIT_ACTION else EXIT_REFUSED, action=str(report.get("action") or ""))
            if adapters.quota.verify_stage(str(stage)) != "verified":
                raise InstallError("the staged plugin does not verify against the approved lock", code=EXIT_REFUSED)
            return str(report.get("result"))

        def stage_reconcile() -> str:
            _bound_lock(ctx, adapters)
            return {"verified": "done", "absent": "not_done"}.get(adapters.quota.verify_stage(str(stage)), "unknown")
        steps.append(Step("quota_stage", "stage, verify and build the exact pinned quota plugin (network only if no local artifact is given)", stage_run, idempotent=False, external=True, reconcile=stage_reconcile))
    if "quota_link" in quota:
        def link_run() -> str:
            _bound_lock(ctx, adapters)
            if adapters.quota.verify_stage(str(stage)) != "verified":
                raise InstallError("the staged plugin does not verify against the approved lock; it was not linked", code=EXIT_REFUSED)
            if adapters.quota.plugin_state().get("state") != "absent":
                raise InstallError("the plugin inventory no longer shows the plugin as absent; it was not linked", code=EXIT_REFUSED)
            adapters.quota.link(str(staged))
            return "linked (disabled)"

        def link_reconcile() -> str:
            _bound_lock(ctx, adapters)
            state = adapters.quota.plugin_state()
            if state.get("state") == "absent":
                return "not_done"
            return "done" if ours(state) else "unknown"
        steps.append(Step("quota_link", "link the staged plugin without enabling it", link_run, idempotent=False, external=True, reconcile=link_reconcile))
    if "quota_enable" in quota:
        def enable_run() -> str:
            _bound_lock(ctx, adapters)
            state = adapters.quota.plugin_state()
            if not ours(state):
                raise InstallError("the plugin in the inventory is not the approved, verified one; it was not enabled", code=EXIT_REFUSED)
            if state.get("enabled") is True:
                return "already enabled"
            adapters.quota.enable()
            return "enabled"

        def enable_reconcile() -> str:
            _bound_lock(ctx, adapters)
            state = adapters.quota.plugin_state()
            if not ours(state) or state.get("enabled") not in (True, False):
                return "unknown"
            return "done" if state["enabled"] else "not_done"
        steps.append(Step("quota_enable", "enable the linked plugin", enable_run, idempotent=False, external=True, reconcile=enable_reconcile))
    activate = comp.get("activate") or []
    if activate:
        def act(verb: str, unit: str | None = None) -> Callable[[], str]:
            def run() -> str:
                Systemd(ctx.env.host).act(verb, *([unit] if unit else []))
                return f"{verb} {unit or ''}".strip()
            return run
        steps.append(Step("activation_reload", "reload the user manager so it sees the installed units", act("daemon-reload")))
        for unit in activate:
            steps.append(Step(f"enable:{unit}", f"enable {unit}", act("enable", unit)))
            steps.append(Step(f"start:{unit}", f"start {unit}", act("start", unit)))
    return steps


# ------------------------------------------------------------------------------------------------ readiness


def readiness(env: Env, adapters: Adapters) -> dict[str, Any]:
    """What is installed and what is still missing, read-only. Three separate questions: is it installed and consistent, is the
    delivery chain usable NOW (a Herdr server answers, recorded sessions match live agents by exact identity, watcher and bridge
    run), and would it SURVIVE a reboot (durable enablement, lingering, a headless Herdr server). Network items stay NOT_VERIFIED."""
    layout = env.layout
    installed = inspect_install(layout)
    items: list[dict[str, str]] = []

    def add(item: str, status: str, detail: str, action: str = "") -> None:
        items.append({"id": item, "status": status, "detail": detail, "action": action})

    add("program", "OK" if installed.layout == "managed" else ("ACTION_REQUIRED" if installed.layout == "absent" else "FAILED"),
        f"{installed.layout}" + (f" generation {installed.active_generation}" if installed.active_generation else "") + (f": {'; '.join(installed.problems)}" if installed.problems else ""),
        "run `herdr-setup setup`" if installed.layout == "absent" else "")
    if installed.layout == "managed" and installed.receipt:
        drifted = [item["dest"] for item in installed.receipt.get("assets", []) if describe(Path(item["dest"]))["state"] != "file" or describe(Path(item["dest"])).get("sha256") != item["sha256"]]
        add("managed-files", "OK" if not drifted else "FAILED", "launchers, units and documents match the receipt" if not drifted else f"changed or missing: {', '.join(drifted[:5])}",
            "review the changes; `herdr-setup update` refuses to overwrite files it does not own" if drifted else "")
    for entry in platform_prerequisites(env, adapters.probe):
        items.append(entry)
    try:
        config = core.load_config(layout.sup_config)
        add("configuration", "OK", f"{layout.sup_config}")
        root = Path(config["project_root"])
        add("project-root", "OK" if (root / "AGENTS.md").is_file() or (root / "CLAUDE.md").is_file() else "ACTION_REQUIRED", str(root), "add AGENTS.md or CLAUDE.md to the project root")
    except core.SupervisorError as error:
        add("configuration", "ACTION_REQUIRED", str(error), "run `herdr-setup setup` or fix the configuration")
    telegram = htg.doctor_summary(network=False, tg_paths=_telegram_paths(layout))
    status = telegram.get("status")
    telegram_configured = status not in ("UNCONFIGURED", None)
    add("telegram", "OK" if status in ("CONFIGURED_NO_NETWORK_CHECK", "HEALTHY") else "ACTION_REQUIRED", f"{status}", "configure Telegram: see docs/TELEGRAM.md" if status == "UNCONFIGURED" else "")
    if status == "CONFIGURED_NO_NETWORK_CHECK":
        add("telegram-network", "NOT_VERIFIED", "setup never contacts Telegram, so the bot identity, webhook state and live bridge health are unproven", "run `herdr-telegram doctor --network` once the bridge runs (it calls the Telegram API)")
    code, report = adapters.quota.check()
    dependency = report.get("dependency") or {}
    add("quota-plugin", "OK" if dependency.get("state") == "pinned" and dependency.get("enabled") is not False else "ACTION_REQUIRED", f"{dependency.get('state')}: {dependency.get('detail')}", str(dependency.get("action") or ""))
    providers = (report.get("telemetry") or {}).get("providers") or {}
    fresh = bool(providers) and all(item.get("state") == "FRESH" for item in providers.values())
    add("quota-snapshots", "OK" if fresh else "ACTION_REQUIRED", "fresh" if fresh else "not fresh", "enable the plugin and run its configure action (read docs/QUOTA_DEPENDENCY.md)" if not fresh else "")
    agents = adapters.probe.agents()
    add("herdr-server", "OK" if agents is not None else "ACTION_REQUIRED", "a Herdr server answers" if agents is not None else "no Herdr server answers",
        "open Herdr (or start your headless server deliberately); Supervisor never starts a second server for you" if agents is None else "")
    owners = owners_report(layout, agents)
    add("sessions", "OK" if owners["status"] == "ok" else "ACTION_REQUIRED", "; ".join(owners["problems"]) if owners["problems"] else "each recorded session matches exactly one live agent of its provider",
        "" if owners["status"] == "ok" else "confirm or repair the pair with `herdr-supervisor sessions` / `repair-owner-pane`; setup never creates, restarts, rebinds or repairs sessions")
    systemd = Systemd(env.host)
    units: dict[str, dict[str, str]] = {}
    if installed.layout == "managed" and systemd.available():
        for name, unit in UNIT_FOR_SERVICE.items():
            if os.path.lexists(layout.unit / unit):
                units[name] = {"unit": unit, "active": systemd.state(unit), "enabled": systemd.enabled(unit)}
    elif installed.layout == "managed":
        add("systemd", "ACTION_REQUIRED", "the user systemd manager cannot be reached", "run from a login session with a user systemd bus")
    required = ["worker"] + (["telegram"] if telegram_configured else [])
    now_ok = all(units.get(name, {}).get("active") == "active" for name in required)
    add("service-chain", "OK" if units and now_ok else "ACTION_REQUIRED", ", ".join(f"{n}:{i['active']}/{i['enabled']}" for n, i in units.items()) or "no managed unit is active",
        "enable the worker watcher and the Telegram bridge with `herdr-setup setup --profile full --services worker,telegram`" if not (units and now_ok) else "")
    durable = {"enabled", "linked"}  # enabled-runtime (and anything else) does not survive a reboot
    units_durable = bool(units) and all(units.get(name, {}).get("enabled") in durable for name in required)
    server_durable = units.get("server", {}).get("enabled") in durable
    linger = systemd.linger(user_name()) if units else "unknown"
    persistent = units_durable and server_durable and linger == "enabled"
    add("reboot-persistence", "OK" if persistent else "ACTION_REQUIRED",
        f"watcher/bridge enabled durably: {units_durable}; headless Herdr server unit enabled: {server_durable}; linger: {linger}",
        "" if persistent else "to survive a reboot: enable the units durably (not enabled-runtime), have an administrator run `sudo loginctl enable-linger YOUR_USER` once (Supervisor never does), "
                              "and start Herdr at boot (enable the headless herdr-server.service deliberately, never beside an interactive server). A manually opened interactive Herdr is usable now but is not reboot-persistent. "
                              "Telegram online does not prove Herdr is running after a reboot")
    boot = {"enabled_units": units_durable, "headless_server_enabled": server_durable, "linger": linger}
    blocking = [item for item in items if item["status"] == "FAILED"]
    current_missing = [item for item in items if item["status"] == "ACTION_REQUIRED" and item["id"] != "reboot-persistence"]
    if blocking:
        overall = "FAILED"
    elif installed.layout == "absent":
        overall = "NOT_INSTALLED"
    elif current_missing:
        overall = "INSTALLED_INACTIVE" if not any(info["active"] == "active" for info in units.values()) and not any(
            i["id"] in ("telegram", "quota-plugin", "quota-snapshots", "sessions") and i["status"] == "OK" for i in items) else "ACTION_REQUIRED"
    else:
        overall = "READY" if persistent else "READY_NOW"
    return {"overall": overall, "layout": installed.layout, "generation": installed.active_generation, "items": items, "services": units, "boot": boot}


# ------------------------------------------------------------------------------------------------ rendering


def render_plan(plan: Plan) -> str:
    data = plan.data
    lines = [f"Operation: {data['kind']}" + (f" ({data['options'].get('profile')} profile)" if data["options"].get("profile") else "")]
    candidate = data.get("candidate") or {}
    if data["kind"] == "rollback":
        lines.append(f"Return to: {candidate.get('kind')} release {candidate.get('version')} {candidate.get('generation_id') or ''}".rstrip())
    else:
        lines.append(f"Candidate: version {candidate.get('version')} generation {candidate.get('generation_id')} payload {str(candidate.get('payload_id'))[:12]} source {candidate.get('source')}")
        lines.append(f"Payload:   {data.get('payload_root')}")
    lines.append(f"Installed: {data['installed'].get('layout')}" + (f" generation {data['installed'].get('generation')}" if data["installed"].get("generation") else ""))
    lines.append(f"Home:      {data['target']['home']}")
    if plan.noop:
        lines.append("Nothing to do: the installation already matches.")
    else:
        lines.append("Actions, in order (nothing below has been done):")
        for index, action in enumerate(data["actions"], 1):
            lines.append(f"  {index}. {action['title']}" + ("  [external command]" if action["external"] else ""))
        services = data.get("services") or {}
        if services.get("stop"):
            lines.append(f"  stopped and restored around the change: {', '.join(services['stop'])} (the headless Herdr server unit is never stopped)")
    activate = (data.get("components") or {}).get("activate") or []
    lines.append("Services: " + (f"enable and start {', '.join(activate)}" if activate else "none are enabled, started or restarted"))
    lines.append("Preserved: configuration, credentials, provider logins, task/gate/inbox/outbox state, native sessions, and every file this tool does not list.")
    for note in plan.notes:
        lines.append(f"Note: {note}")
    if plan.blockers:
        lines.append("REFUSED until resolved:")
        lines += [f"  - {item}" for item in plan.blockers]
    pending = [item for item in plan.prerequisites if item["status"] != "OK"]
    if pending:
        lines.append("Remaining for you (ACTION_REQUIRED):")
        lines += [f"  - {item['detail']}: {item['action']}" for item in pending]
    lines.append(f"Approval hash: {plan.approval_hash}")
    return "\n".join(lines)


def render_readiness(report: dict[str, Any]) -> str:
    lines = [f"Status: {report['overall']}  (layout {report['layout']}" + (f", generation {report['generation']}" if report.get("generation") else "") + ")"]
    for item in report["items"]:
        lines.append(f"  [{item['status']}] {item['id']}: {item['detail']}" + (f"\n      -> {item['action']}" if item.get("action") and item["status"] != "OK" else ""))
    if any(item["status"] == "NOT_VERIFIED" for item in report["items"]) and report["overall"] in ("READY", "READY_NOW"):
        lines.append("The status covers every local check; the items marked NOT_VERIFIED need the live services or a network call you choose to make.")
    if report["overall"] == "READY_NOW":
        lines.append("READY_NOW: usable at this moment, but it would not come back by itself after a reboot (see reboot-persistence).")
    return "\n".join(lines)


def render_result(result: ApplyResult) -> str:
    lines = [f"Result: {result.status}" + (f" - {result.message}" if result.message else "")]
    for step in result.steps:
        if step["status"] != "PENDING":
            lines.append(f"  {step['status']:<8} {step['id']}" + (f": {step['detail']}" if step.get("detail") else ""))
    return "\n".join(lines)


# ------------------------------------------------------------------------------------------------ command line


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="herdr-setup", description="Preview-first setup, update, status and rollback of a Herdr Supervisor installation.")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(command: argparse.ArgumentParser, *, payload: bool = True) -> None:
        if payload:
            command.add_argument("--payload", help="reviewed local source or release tree (default: the tree this tool runs from)")
        command.add_argument("--plan-out", help="also write the plan to this new file (an explicit output; refuses to overwrite)")
        command.add_argument("--apply", action="store_true", help="apply instead of previewing")
        command.add_argument("--plan", help="saved plan file to apply (with --approve)")
        command.add_argument("--approve", metavar="HASH", help="the approval hash printed with the plan")
        command.add_argument("--approve-current", action="store_true", help="compute the plan now and approve exactly it (what ./install.sh does)")

    setup = sub.add_parser("setup", help="install (minimal) or install and configure (full)")
    common(setup)
    setup.add_argument("--profile", choices=PROFILES, default="minimal", help="minimal: inactive program files only; full: guided component setup")
    setup.add_argument("--components", help=f"comma list from {','.join(COMPONENTS)} (full profile; default sessions,telegram,quota)")
    setup.add_argument("--services", help=f"comma list from {','.join(SERVICES)} to enable and start (default: none)")
    for key in PATH_KEYS:
        setup.add_argument(f"--{key.replace('_', '-')}", dest=key, help=f"set {key} in the configuration when it is not set yet")
    setup.add_argument("--telegram-owner-id", type=int)
    setup.add_argument("--telegram-chat-id", type=int)
    setup.add_argument("--telegram-timezone", default=htg.DEFAULT_TIMEZONE)
    setup.add_argument("--telegram-token-file", help="mode-0600 file holding the bot token (the token itself is never an argument)")
    setup.add_argument("--quota-stage-dir")
    setup.add_argument("--quota-archive", help="lock-listed source archive for an offline stage")
    setup.add_argument("--quota-bundle", help="lock-listed git bundle for an offline stage")
    setup.add_argument("--quota-offline", action="store_true", help="pass --offline to cargo (needs a populated cargo home)")
    setup.add_argument("--quota-lock", help="alternative dependency lock (testing)")
    setup.add_argument("--migrate", action="store_true", help="also run the explicit state migration after the files are in place")
    setup.add_argument("--interactive", action="store_true", help="ask for the choices and the token, then show the plan and ask for approval")
    update = sub.add_parser("update", help="replace the installed release with a reviewed local candidate")
    common(update)
    update.add_argument("--adopt-legacy-from", metavar="DIR", help="adopt a flat legacy installation that is byte-identical to this reviewed tree")
    update.add_argument("--migrate", action="store_true")
    sub.add_parser("status", help="read-only installation and readiness report")
    rollback = sub.add_parser("rollback", help="undo the last operation")
    common(rollback, payload=False)
    return parser


def _write_plan_out(path: str, plan: Plan) -> None:
    target = Path(path)
    data = (json.dumps(plan.document(), indent=2, sort_keys=True) + "\n").encode("utf-8")
    if not create_file_exclusive(target, data, 0o600):
        raise InstallError(f"{target} already exists; choose a new --plan-out path", code=EXIT_USAGE)


def _emit(args: argparse.Namespace, text: str, document: dict[str, Any]) -> None:
    print(json.dumps(document, indent=2, sort_keys=True) if args.json else text)


def _status_code(result: ApplyResult, plan: Plan | None = None) -> int:
    if result.status in ("COMPLETE", "NOOP", "ALREADY_APPLIED"):
        return EXIT_ACTION if plan is not None and any(item["status"] != "OK" for item in plan.prerequisites) else EXIT_OK
    if result.status in ("RECONCILING",):
        return EXIT_RECONCILING
    return EXIT_REFUSED


def _missing_payload_message(env: Env) -> str:
    """No payload was given and this tool is not running from one (an installed generation holds only the program files)."""
    recorded = None
    try:
        receipt = inspect_install(env.layout).receipt
        recorded = receipt.get("payload_root") if receipt else None
    except InstallError:
        recorded = None
    text = "no reviewed payload tree was given and this tool is not running from one"
    if isinstance(recorded, str):
        text += f"; this installation was installed from {recorded} (recorded in its receipt, not used automatically): pass --payload {recorded} to use it again"
    return text


def run_command(args: argparse.Namespace, env: Env, adapters: Adapters | None = None, *, ask: Callable[[str], str] = input) -> int:
    if args.command == "status":
        status_options: dict[str, Any] = {"quota": {}}
        adapters = adapters or Adapters.for_env(env, status_options)
        report = readiness(env, adapters)
        _emit(args, render_readiness(report), report)
        return {"READY": EXIT_OK, "READY_NOW": EXIT_OK, "INSTALLED_INACTIVE": EXIT_OK, "NOT_INSTALLED": EXIT_ACTION, "FAILED": EXIT_REFUSED}.get(report["overall"], EXIT_ACTION)
    kind = args.command
    if os.geteuid() == 0:
        raise InstallError("refusing to run as root; run as the target user", code=EXIT_USAGE)
    if args.plan or args.approve:
        if not args.apply:
            raise InstallError("--plan and --approve are used with --apply", code=EXIT_USAGE)
        if not (args.plan and args.approve):
            raise InstallError("--apply of a saved plan needs both --plan FILE and --approve HASH", code=EXIT_USAGE)
        approved, approval_hash = load_plan_document(Path(args.plan))
        if approval_hash != args.approve:
            raise InstallError("the approval hash does not match the saved plan", code=EXIT_REFUSED)
        if approved.get("kind") != kind:
            raise InstallError(f"the saved plan is for `{approved.get('kind')}`, not `{kind}`", code=EXIT_USAGE)
        options = approved["options"]
        payload_root = Path(approved["payload_root"]) if approved.get("payload_root") else None
    else:
        options = options_from_args(args, kind)
        payload_root = None
        if kind != "rollback":
            payload_root = Path(args.payload).expanduser().resolve() if args.payload else default_payload_root()
            if payload_root is None:
                raise InstallError(_missing_payload_message(env), code=EXIT_ACTION,
                                   action="pass --payload DIR naming the reviewed source checkout or release tree (nothing is guessed from the current directory); a wheel alone contains the console tools but not the units, launchers and documents")
        if getattr(args, "interactive", False):
            options = interactive_options(args, options, ask)
        approved = None
        approval_hash = ""
    adapters = adapters or Adapters.for_env(env, options, interactive=bool(options.get("interactive")))
    plan = build_plan(env, kind, options, payload_root, adapters)
    if args.plan_out:
        _write_plan_out(args.plan_out, plan)
    if not args.apply:
        if options.get("interactive"):
            print(render_plan(plan))
            if plan.blockers:
                return EXIT_REFUSED
            answer = ask(f"Type the first 12 characters of the approval hash ({plan.approval_hash[:12]}) to apply, anything else to stop: ").strip()
            if answer != plan.approval_hash[:12]:
                print("Stopped; nothing was changed.")
                return EXIT_OK
            args.approve_current = True
            approved, approval_hash = plan.data, plan.approval_hash
        else:
            _emit(args, render_plan(plan), {**plan.document(), "blockers": plan.blockers, "notes": plan.notes, "prerequisites": plan.prerequisites})
            return EXIT_REFUSED if plan.blockers else EXIT_OK
    if approved is None:
        if not args.approve_current:
            raise InstallError("--apply needs an approval: --plan FILE --approve HASH, or --approve-current", code=EXIT_USAGE)
        approved, approval_hash = plan.data, plan.approval_hash
    if kind == "rollback":
        rebuild: Callable[[], Plan] = lambda: build_plan(env, kind, options, None, adapters)
    else:
        rebuild = lambda: build_plan(env, kind, options, payload_root, adapters)
    result = apply_operation(env, kind, approved, approval_hash, rebuild=rebuild, make_steps=lambda ctx, payload: file_steps(ctx) + component_steps(ctx, adapters))
    for note in plan.notes:
        print(note, file=sys.stderr)
    verdict: dict[str, Any] = {"result": dataclasses.asdict(result), "prerequisites": plan.prerequisites}
    text = render_result(result)
    pending = [item for item in plan.prerequisites if item["status"] != "OK"]
    if pending and result.status in ("COMPLETE", "NOOP", "ALREADY_APPLIED"):
        text += "\nRemaining for you (ACTION_REQUIRED):\n" + "\n".join(f"  - {item['detail']}: {item['action']}" for item in pending)
    code = _status_code(result, plan)
    if result.status in ("COMPLETE", "NOOP", "ALREADY_APPLIED") and options.get("profile") == "full":
        report = readiness(env, adapters)
        verdict["readiness"] = report
        text += "\n" + render_readiness(report)
        code = EXIT_OK if report["overall"] in ("READY", "READY_NOW") else EXIT_ACTION
    elif result.status in ("COMPLETE", "NOOP", "ALREADY_APPLIED"):
        text += "\nInstalled inactive: no service was loaded, enabled, started or restarted." if kind == "setup" else ""
    _emit(args, text, verdict)
    return code


def interactive_options(args: argparse.Namespace, options: dict[str, Any], ask: Callable[[str], str]) -> dict[str, Any]:
    """Collect the choices a person would otherwise pass as flags. The token is never asked for here: the protected
    prompt appears only when the approved plan runs."""
    profile = ask("Profile: minimal (inactive program files only) or full (guided setup) [minimal]: ").strip() or "minimal"
    if profile not in PROFILES:
        raise InstallError(f"unknown profile {profile}", code=EXIT_USAGE)
    options["profile"] = profile
    options["interactive"] = True
    if profile == "minimal":
        return options
    components = [name for name in COMPONENTS if ask(f"Set up {name}? [{'Y' if name in DEFAULT_FULL_COMPONENTS else 'n'}]: ").strip().lower() in (("", "y", "yes") if name in DEFAULT_FULL_COMPONENTS else ("y", "yes"))]
    options["components"] = sorted(components)
    if "paths" in components:
        for key in PATH_KEYS:
            value = ask(f"{key} (blank to skip): ").strip()
            if value:
                options["paths"][key] = _abs(value)
    if "telegram" in components:
        owner = ask("Telegram owner user id (blank to skip): ").strip()
        if owner.isdigit():
            options["telegram"]["owner_user_id"] = int(owner)
            chat = ask("Telegram chat id (blank = same as owner): ").strip()
            options["telegram"]["chat_id"] = int(chat) if re.fullmatch(r"-?[0-9]+", chat) else None
    options["services"] = sorted(name for name in SERVICES if ask(f"Enable and start the {name} service now? [n]: ").strip().lower() in ("y", "yes"))
    return options


def main(argv: list[str] | None = None, *, env: Env | None = None, adapters: Adapters | None = None, ask: Callable[[str], str] = input) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    env = env or Env(Layout.from_environment())
    if getattr(args, "interactive", False) and not sys.stdin.isatty() and ask is input:
        print("error: --interactive needs a terminal", file=sys.stderr)
        return EXIT_USAGE
    try:
        return run_command(args, env, adapters, ask=ask)
    except InstallError as error:
        print(f"{'REFUSED' if error.code == EXIT_REFUSED else 'error'}: {error}" + (f"\naction: {error.action}" if error.action else ""), file=sys.stderr)
        return error.code
    except (core.SupervisorError, hq.DependencyError) as error:
        print(f"error: {error}", file=sys.stderr)
        return EXIT_USAGE
    except Exception as error:  # noqa: BLE001 - a defect must not look like success or leave a traceback as the only record
        print(f"unexpected error ({type(error).__name__}): {error}\nThe operation journal under installation/ops records the step as running; applying the same plan again reconciles it without repeating an external action.", file=sys.stderr)
        return EXIT_RECONCILING


__all__ = ["Adapters", "build_parser", "build_plan", "main", "readiness", "run_command", "verify_units"]

if __name__ == "__main__":
    raise SystemExit(main())
