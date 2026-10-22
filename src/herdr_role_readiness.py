"""Truthful readiness of provider subagent roles (supervisor reliability, batch 4).

Files on disk and an operator-visible menu entry do not establish what a RUNNING parent's Agent tool can select. This module reports the
separate facts it can honestly know and marks the one it cannot as UNKNOWN: (1) definitions installed (names + hashes, never content), (2) the
parent's exact owned session identity and the provider CLI's reported version (an executable's version is not the running session's version),
(3) operator-reported observations, authenticated by the local operator's unix account, bound to that exact session AND version, and (4) whether
selectability is established. An operator observation is operator evidence only: it never becomes execution authority, never starts or invokes an
agent, and is invalidated by any session or version change. Nothing here restarts, rebinds, edits a definition or permission, or calls a model."""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Callable

from herdr_core import ROLE_NAME_RE, SupervisorError, atomic_write_json, iso_utc

OBSERVATION_KINDS = ("menu_visible", "agent_tool_catalog", "agent_tool_catalog_absent")
MAX_ROLES = 64
MAX_OBSERVATIONS = 20
_VERSION_RE = re.compile(r"^[ -~]{1,120}$")

LIMITATIONS = (
    "role definitions state their command limits as instructions; they are not an independent sandbox",
    "the settlement guard's known detector-layout blind spot (R1) is not fixed by role readiness",
    "absence of selectability evidence is UNKNOWN, not a failure claim",
    "a restart or resume of the SAME session id with the SAME CLI version changes none of the facts checked here, so such a restart is not detected; only a session-id or version change invalidates an observation",
)
HUMAN_ACTION = (
    "Confirm in the parent's own session that the Agent tool lists the role names (for example by asking the operator to read the live agent "
    "list from that session), then record it with `herdr-supervisor role-observe --kind agent_tool_catalog`; a restart or session change "
    "invalidates it. The supervisor will not invoke an agent to find out."
)


def installed_definitions(directory: Path) -> dict[str, Any]:
    """Names and SHA-256 of `*.md` definitions in the provider's agents directory. Read-only; never reads a definition into a report."""
    try:
        real = Path(os.path.realpath(directory))
        if not real.is_dir():
            return {"status": "absent", "directory": str(directory), "roles": []}
        roles = []
        for path in sorted(real.glob("*.md"))[:MAX_ROLES]:
            if path.is_symlink() or not path.is_file():
                continue
            roles.append({"name": path.stem, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "bytes": path.stat().st_size})
        return {"status": "installed" if roles else "absent", "directory": str(directory), "roles": roles}
    except OSError as error:
        return {"status": "unreadable", "directory": str(directory), "roles": [], "detail": error.__class__.__name__}


def _clean_roles(roles: Any) -> list[str]:
    if not isinstance(roles, list) or not 1 <= len(roles) <= MAX_ROLES or not all(isinstance(r, str) and ROLE_NAME_RE.match(r) for r in roles):
        raise SupervisorError("observed roles must be 1-64 plain role names")
    return sorted(set(roles))


def load_observations(path: Path) -> list[dict[str, Any]]:
    """Strictly validated durable operator observations (a malformed record is ignored, never trusted)."""
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError):
        return []
    items = raw.get("observations") if isinstance(raw, dict) and raw.get("schema_version") == 1 else None
    clean = []
    for item in items if isinstance(items, list) else []:
        try:
            if (isinstance(item, dict) and item.get("provider") == "claude" and item.get("kind") in OBSERVATION_KINDS
                    and isinstance(item.get("session_id"), str) and isinstance(item.get("provider_version"), str) and _VERSION_RE.match(item["provider_version"])
                    and isinstance(item.get("actor"), str) and isinstance(item.get("at"), str)):
                clean.append({**item, "roles": _clean_roles(item.get("roles"))})
        except SupervisorError:
            continue
    return clean


def record_observation(path: Path, *, provider: str, session_id: str, provider_version: str | None, kind: str, roles: Any, actor: str, now: float) -> dict[str, Any]:
    """Append an operator observation bound to the exact live session and the version read at this moment. The caller supplies the live
    session id it just verified against owners.json; an unknown version refuses (an unbound observation would carry forward silently)."""
    if provider != "claude":
        raise SupervisorError("role observations are only supported for claude (Codex subagents are disabled)")
    if kind not in OBSERVATION_KINDS:
        raise SupervisorError(f"observation kind must be one of {', '.join(OBSERVATION_KINDS)}")
    if not isinstance(session_id, str) or not session_id:
        raise SupervisorError("the exact live session identity is required")
    if not isinstance(provider_version, str) or not _VERSION_RE.match(provider_version):
        raise SupervisorError("the provider version could not be established; refusing an unbound observation")
    if not isinstance(actor, str) or not 1 <= len(actor) <= 200:
        raise SupervisorError("an authenticated actor is required")
    record = {"provider": provider, "kind": kind, "roles": _clean_roles(roles), "session_id": session_id, "provider_version": provider_version,
              "actor": actor, "at": iso_utc(now), "authority": "operator_evidence_only"}
    existing = load_observations(path)
    existing.append(record)
    atomic_write_json(path, {"schema_version": 1, "observations": existing[-MAX_OBSERVATIONS:]}, mode=0o600)
    return record


def readiness(provider: str, *, definitions_dir: Path, live_session_id: str | None, provider_version: str | None, observations: list[dict[str, Any]],
              expected_roles: list[str] | None = None, identity_note: str | None = None) -> dict[str, Any]:
    """The separate facts, each with its provenance. `selectability` is UNKNOWN unless a CURRENT operator observation of the Agent tool catalog is
    bound to this exact session and version; even then it is operator-reported, carries no execution authority and is never a probe result."""
    definitions = installed_definitions(definitions_dir)
    installed = {r["name"] for r in definitions["roles"]}
    wanted = sorted(set(expected_roles)) if expected_roles else sorted(installed)
    mine = [o for o in observations if o["provider"] == provider]

    def status_of(observation: dict[str, Any]) -> str:
        if live_session_id is None or observation["session_id"] != live_session_id:
            return "stale_session"
        if provider_version is None or observation["provider_version"] != provider_version:
            return "stale_version"
        return "current"

    views = [{"kind": o["kind"], "roles": o["roles"], "session_id": o["session_id"], "provider_version": o["provider_version"], "actor": o["actor"], "at": o["at"],
              "status": status_of(o), "authority": "operator_evidence_only"} for o in mine]
    current = [v for v in views if v["status"] == "current"]
    menu = [v for v in current if v["kind"] == "menu_visible"]
    catalog = [v for v in current if v["kind"] in ("agent_tool_catalog", "agent_tool_catalog_absent")]
    selectability: dict[str, Any] = {"status": "UNKNOWN", "basis": "no current operator observation of the parent's Agent tool catalog", "authority": "none", "action": HUMAN_ACTION}
    if catalog:
        latest = catalog[-1]
        listed = set(latest["roles"]) if latest["kind"] == "agent_tool_catalog" else set()
        missing = [r for r in wanted if r not in listed]
        if latest["kind"] == "agent_tool_catalog" and not missing and wanted:
            selectability = {"status": "OPERATOR_REPORTED_SELECTABLE", "basis": "operator observed the roles in the parent's Agent tool for this exact session and version",
                             "authority": "none (operator evidence; not an invocation, not execution authority)", "observed_at": latest["at"], "actor": latest["actor"]}
        else:
            selectability = {"status": "OPERATOR_REPORTED_NOT_SELECTABLE", "basis": "operator observation of the parent's Agent tool catalog does not list every expected role",
                             "missing": missing or wanted, "authority": "none", "action": HUMAN_ACTION, "observed_at": latest["at"], "actor": latest["actor"]}
    return {
        "provider": provider,
        "definitions": {**definitions, "expected_roles": wanted, "missing": [r for r in wanted if r not in installed]},
        "parent": {"session_id": live_session_id, "provider_version": provider_version,
                   "identity": "verified: the exact owned session in its recorded pane (the control path's rule)" if live_session_id else f"unverified: {identity_note or 'no live owned session'}",
                   "version_basis": "provider CLI --version of the configured command; an executable's version does not prove the running session's version"},
        "menu_visibility": {"status": "operator_reported" if menu else "UNKNOWN", "observations": menu, "authority": "none"},
        "selectability": selectability,
        "observations": views,
        "limitations": list(LIMITATIONS),
    }


def read_provider_version(command: str, runner: Callable[..., Any]) -> str | None:
    """`<command> --version`, bounded and read-only. Returns the first printable line or None."""
    try:
        result = runner([command, "--version"], check=False, capture_output=True, text=True, timeout=10)
    except (OSError, ValueError, subprocess.SubprocessError):  # a missing binary or timeout is "unknown", never a crash
        return None
    if result.returncode != 0:
        return None
    for line in (result.stdout or "").splitlines():
        line = line.strip()
        if line and _VERSION_RE.match(line):
            return line
    return None
