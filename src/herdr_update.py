"""Plan, apply, update and roll back a managed installation.

A *plan* is plain data: the candidate (by content hash), the options, the resolved target identity, the exact
preimage of every file that may change, the service actions, and the ordered action list. Its SHA-256 is the
approval hash. Applying recomputes the plan under the deployment lock and refuses if any input moved, so an
approval can never be spent on a different candidate, different options, a different target or different
preimages. Every mutation after that is a journaled, resumable step (see herdr_installation.run_steps).
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import os
import re
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Any

import herdr_core as core
from herdr_installation import (
    COMPAT_LINKS,
    EXIT_ACTION,
    EXIT_REFUSED,
    STEP_STATES,
    UNIT_FOR_SERVICE,
    Asset,
    DeployLock,
    Env,
    InstallError,
    InstallState,
    Journal,
    Layout,
    Payload,
    PayloadFile,
    Step,
    Systemd,
    asset_state,
    assets_for,
    canonical_hash,
    compat_link_state,
    create_file_exclusive,
    describe,
    ensure_compat_link,
    ensure_private_dir,
    fsync_dir,
    inspect_install,
    is_int,
    load_payload,
    managed_unit_names,
    publish_generation,
    read_active,
    read_bytes_nofollow,
    read_receipt,
    remove_compat_link,
    run_steps,
    sha256_bytes,
    state_schema_on_disk,
    switch_active,
    tool_identity,
    validate_backup_manifest,
    verify_generation,
    verify_units,
    version_key,
    worker_quiesced,
    workload_problems,
    write_file_atomic,
)
from herdr_validation import StateStore

PLAN_SCHEMA = 1
KINDS = ("setup", "update", "rollback")
SERVER_UNIT = "herdr-server.service"
SMOKE_LAUNCHERS = ("herdr-supervisor", "herdr-telegram", "herdr-quota-dependency", "herdr-setup")
_STATE_DIRS = ("query", "outbox", "codex-reset", "codex-reset/pending", "codex-reset/processing", "codex-reset/results", "pending-starts")
_INITIAL_CONFIG = (("sup_config", "supervisor.example.json"), ("backup_config", "backup.example.json"), ("tg_config", "telegram.example.json"))


@dataclasses.dataclass
class Plan:
    data: dict[str, Any]
    blockers: list[str] = dataclasses.field(default_factory=list)
    notes: list[str] = dataclasses.field(default_factory=list)
    prerequisites: list[dict[str, str]] = dataclasses.field(default_factory=list)

    @property
    def approval_hash(self) -> str:
        return canonical_hash(self.data)

    @property
    def noop(self) -> bool:
        return bool(self.data.get("noop"))

    def document(self) -> dict[str, Any]:
        return {"schema": PLAN_SCHEMA, "approval_hash": self.approval_hash, "plan": self.data}


def load_plan_document(path: Path) -> tuple[dict[str, Any], str]:
    try:
        value = json.loads(read_bytes_nofollow(path, 8 * 1024 * 1024))
    except (OSError, ValueError) as error:
        raise InstallError(f"saved plan {path} is unreadable: {error}", code=2) from error
    if not isinstance(value, dict) or value.get("schema") != PLAN_SCHEMA or not isinstance(value.get("plan"), dict) or not isinstance(value.get("approval_hash"), str):
        raise InstallError("saved plan has an unsupported shape", code=2)
    if canonical_hash(value["plan"]) != value["approval_hash"]:
        raise InstallError("saved plan does not match its own hash; it was edited after it was written")
    return value["plan"], value["approval_hash"]


# ------------------------------------------------------------------------------------------------ planning


def managed_directories(env: Env) -> list[tuple[Path, int]]:
    """Directories the installation creates when absent. Existing directories are never re-moded."""
    layout = env.layout
    dirs: list[tuple[Path, int]] = [
        (layout.lib, 0o755), (layout.generations, 0o755), (layout.bin, 0o755), (layout.unit, 0o755), (layout.doc, 0o755), (layout.examples, 0o755),
        (layout.sup_config.parent, 0o700), (layout.tg_config.parent, 0o700), (layout.state, 0o700), (layout.manager, 0o700), (layout.ops, 0o700), (layout.backups, 0o700),
        (layout.tg_state, 0o700), (layout.tg_locks, 0o700),
    ]
    dirs += [(layout.state / name, 0o700) for name in _STATE_DIRS]
    return dirs


def plan_notes_for_directories(env: Env) -> list[str]:
    notes = []
    for path, mode in managed_directories(env):
        if mode == 0o700 and path.is_dir() and not path.is_symlink() and stat.S_IMODE(path.stat().st_mode) & 0o077:
            notes.append(f"existing directory {path} is accessible to other users (mode {oct(stat.S_IMODE(path.stat().st_mode))}); consider `chmod 700` (not changed for you)")
    return notes


def restartable(unit: str) -> bool:
    """Triggers (path/timer units) and the Telegram long-poll bridge can be stopped and started again without losing work. Any other
    service (the supervisor, query, reset or backup worker) that is running is executing a bounded job: it is waited for, never killed."""
    return unit.endswith((".path", ".timer")) or unit == "herdr-telegram.service"


def _service_plan(env: Env, kind: str, installed_units: set[str], candidate_units: set[str], unit_changes: bool, *, existing: bool = True) -> tuple[dict[str, Any], list[str]]:
    """Which active units are stopped and restored around a replacement, and whether the manager must be reloaded.
    The headless server unit is never stopped: it hosts the user's native sessions and does not run program files."""
    layout = env.layout
    plan: dict[str, Any] = {"stop": [], "restore": [], "reload": False, "systemd": "not_needed"}
    if kind == "setup" and not existing:
        return plan, []
    present = sorted(unit for unit in installed_units | candidate_units if os.path.lexists(layout.unit / unit))
    if not present:
        return plan, []
    systemd = Systemd(env.host)
    if not systemd.available():
        return {**plan, "systemd": "unavailable"}, ["the user systemd manager cannot be reached, so the state of the installed units cannot be established; run from a session with a user systemd bus"]
    blockers = []
    active = []
    for unit in present:
        state = systemd.state(unit)
        if state in ("activating", "deactivating", "reloading", "refreshing", "unknown"):
            blockers.append(f"{unit} is {state}; wait for it to settle (or establish its state) and retry")
        elif state == "active" and unit != SERVER_UNIT:
            if restartable(unit):
                active.append(unit)
            else:
                blockers.append(f"{unit} is running a job; it is not stopped or restarted, so wait for it to finish and retry")
    stoppable = sorted(active, key=lambda unit: (not unit.endswith((".path", ".timer")), unit))  # triggers first, then services
    plan.update(stop=stoppable, restore=list(reversed(stoppable)), reload=unit_changes, systemd="available")
    return plan, blockers


def _recorded_assets(receipt: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for item in (receipt or {}).get("assets", []):
        if isinstance(item, dict) and isinstance(item.get("dest"), str):
            out[item["dest"]] = item
    return out


def _asset_conflicts(assets: list[Asset], recorded: dict[str, dict[str, Any]], preimages: dict[str, Any]) -> tuple[list[str], bool]:
    blockers: list[str] = []
    changes = False
    for asset in assets:
        current = describe(asset.dest)
        preimages[str(asset.dest)] = current
        if current["state"] == "unsafe":
            blockers.append(f"{asset.dest} is a {current['kind']}; refusing to replace it")
            continue
        if current["state"] == "absent":
            changes = True
            continue
        if asset_state(asset) == "applied":
            continue
        changes = True
        owned = recorded.get(str(asset.dest))
        if owned is None or owned.get("sha256") != current["sha256"]:
            blockers.append(f"{asset.dest} exists with content this installation does not own ({'modified since it was installed' if owned else 'not installed by herdr-setup'}); move it aside or adopt it explicitly")
    return blockers, changes


def _removals(candidate: list[Asset], recorded: dict[str, dict[str, Any]], preimages: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    wanted = {str(asset.dest) for asset in candidate}
    removals: list[dict[str, Any]] = []
    blockers: list[str] = []
    for dest, item in sorted(recorded.items()):
        if dest in wanted:
            continue
        current = describe(Path(dest))
        preimages[dest] = current
        if current["state"] == "absent":
            continue
        if current["state"] == "file" and current["sha256"] == item.get("sha256"):
            removals.append({"dest": dest, "sha256": item["sha256"], "role": item.get("role")})
        else:
            blockers.append(f"{dest} is no longer part of the release but has been modified; it was not removed")
    return removals, blockers


def _verify_adoption(env: Env, legacy: Payload, installed: InstallState) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """A legacy flat installation is adoptable only when it is byte-for-byte the reviewed tree it claims to be."""
    blockers: list[str] = []
    expected = {item.name: item.sha256 for item in legacy.of_role("program")}
    actual = installed.legacy or {}
    for name in sorted(set(actual) - set(expected)):
        blockers.append(f"the installed program directory contains {name}, which the reviewed source does not (unknown or private code is never adopted or replaced)")
    for name in sorted(set(expected) - set(actual)):
        blockers.append(f"the installed program directory lacks {name} from the reviewed source")
    for name in sorted(set(expected) & set(actual)):
        if expected[name] != actual[name]:
            blockers.append(f"the installed {name} differs from the reviewed source")
    recorded: dict[str, dict[str, Any]] = {}
    for asset in assets_for(legacy, env.layout):
        state = describe(asset.dest)
        if state["state"] != "file" or state["sha256"] != asset.sha256:
            blockers.append(f"{asset.dest} is not the reviewed legacy file ({state.get('kind') or state['state']})")
        recorded[str(asset.dest)] = asset.entry()
    return recorded, blockers


def plan_installation(env: Env, kind: str, payload: Payload, options: dict[str, Any]) -> tuple[dict[str, Any], list[str], list[str]]:
    """The file-level portion of a setup or update plan: (sections, blockers, notes)."""
    layout = env.layout
    installed = inspect_install(layout)
    blockers: list[str] = []
    notes: list[str] = []
    recorded: dict[str, dict[str, Any]] = {}
    previous: dict[str, Any] | None = None
    legacy_files: dict[str, str] | None = None
    adopt = options.get("adopt_legacy_from")
    assets = assets_for(payload, layout)
    if kind == "setup":
        if installed.layout == "managed":
            manifest = installed.manifest or {}
            if manifest.get("program_id") != payload.program_id:
                blockers.append(f"version {manifest.get('version')} is already installed; use `herdr-setup update` (setup never replaces an installed release)")
            recorded = _recorded_assets(installed.receipt)
            previous = {"kind": "generation", "generation_id": installed.active_generation, "program_id": manifest.get("program_id"), "version": manifest.get("version"),
                        "state_schema_version": manifest.get("state_schema_version")}
        elif installed.layout == "legacy":
            blockers.append("a legacy (flat) installation exists; setup will not copy over it. Preview `herdr-setup update --adopt-legacy-from DIR` with the reviewed tree it was installed from")
        elif installed.layout != "absent":
            blockers.append(f"the existing installation is {installed.layout}: {'; '.join(installed.problems) or 'unrecognized'}")
    else:
        if installed.layout == "absent":
            blockers.append("nothing is installed; run `herdr-setup setup` first")
        elif installed.layout == "managed":
            recorded = _recorded_assets(installed.receipt)
            manifest = installed.manifest or {}
            previous = {"kind": "generation", "generation_id": installed.active_generation, "program_id": manifest.get("program_id"), "version": manifest.get("version"),
                        "state_schema_version": manifest.get("state_schema_version")}
            if version_key(payload.version) < version_key(str(manifest.get("version"))):
                blockers.append(f"the candidate {payload.version} is older than the installed {manifest.get('version')}; use `herdr-setup rollback` to return to a previous release")
        elif installed.layout == "legacy":
            if not adopt:
                blockers.append("a legacy (flat) installation exists; preview with --adopt-legacy-from DIR naming the reviewed tree it was installed from")
            else:
                try:
                    legacy_payload = load_payload(Path(adopt), require_manager=False)
                except InstallError as error:
                    blockers.append(f"the legacy source {adopt} is not acceptable: {error}")
                else:
                    recorded, adoption = _verify_adoption(env, legacy_payload, installed)
                    blockers += adoption
                    legacy_files = dict(installed.legacy or {})
                    previous = {"kind": "legacy", "version": legacy_payload.version, "state_schema_version": legacy_payload.state_schema_version, "source": legacy_payload.source, "payload_id": legacy_payload.payload_id}
                    if version_key(payload.version) < version_key(legacy_payload.version):
                        blockers.append(f"the candidate {payload.version} is older than the installed {legacy_payload.version}")
        else:
            blockers.append(f"the existing installation is {installed.layout}: {'; '.join(installed.problems) or 'unrecognized'}; it is not changed")
    preimages: dict[str, Any] = {}
    conflicts, changes = _asset_conflicts(assets, recorded, preimages)
    blockers += conflicts
    removals, removal_blockers = _removals(assets, recorded, preimages)
    blockers += removal_blockers
    compat = [name for name in COMPAT_LINKS if f"program/{name}" in {item.key for item in payload.files}]
    retiring = set((legacy_files or {}))
    for name in compat:
        if os.path.lexists(layout.lib / name) and compat_link_state(layout, name) != "ours" and name not in retiring:
            blockers.append(f"{layout.lib / name} exists and is not the compatibility link to the active generation; move it aside")
    unit_assets = {asset.name for asset in assets if asset.role == "unit"}
    unit_changes = any(asset_state(asset) != "applied" for asset in assets if asset.role == "unit") or any(item.get("role") == "unit" for item in removals)
    compat_missing = any(compat_link_state(layout, name) != "ours" for name in COMPAT_LINKS if f"program/{name}" in {item.key for item in payload.files})
    already = installed.layout == "managed" and bool(installed.manifest) and (installed.manifest or {}).get("program_id") == payload.program_id and not changes and not removals and not compat_missing
    existing = installed.layout == "managed"
    if (kind != "setup" or existing) and not already:  # one safety rule for every operation that would change an installed system
        blockers += [f"not at a quiet point: {problem}" for problem in workload_problems(layout, probe_lock=not env.worker_lock_held)]
        schema = state_schema_on_disk(layout)
        if schema == -1:
            blockers.append("the task-state file exists but its schema cannot be read; nothing is changed")
        elif schema is not None and schema > payload.state_schema_version:
            blockers.append(f"task state has schema {schema}, newer than the candidate supports ({payload.state_schema_version}); it is never downgraded")
    installed_units = {Path(dest).name for dest, item in recorded.items() if item.get("role") == "unit"}
    if already:  # a pure no-op asks the user manager nothing
        services: dict[str, Any] = {"stop": [], "restore": [], "reload": False, "systemd": "not_needed"}
    else:
        services, service_blockers = _service_plan(env, kind, installed_units, unit_assets, unit_changes, existing=existing)
        blockers += service_blockers
    notes += plan_notes_for_directories(env)
    sections = {
        "installed": installed.identity(), "candidate": payload.summary(), "assets": [asset.entry() for asset in assets], "removals": removals,
        "preimages": preimages, "services": services, "previous": previous, "legacy_program_files": legacy_files, "compat_links": compat,
        "files_needed": not already, "noop_files": already,
    }
    return sections, blockers, notes


def plan_rollback(env: Env, options: dict[str, Any]) -> tuple[dict[str, Any], list[str], list[str]]:
    """Undo the last operation: its previous generation (or legacy files) and its backed-up assets, after a drift check."""
    layout = env.layout
    installed = inspect_install(layout)
    blockers: list[str] = []
    notes: list[str] = []
    sections: dict[str, Any] = {"installed": installed.identity(), "files_needed": True, "noop_files": False}
    if installed.layout != "managed" or not installed.receipt:
        blockers.append(f"a rollback needs a verified managed installation; found {installed.layout}" + (f": {'; '.join(installed.problems)}" if installed.problems else ""))
        return sections, blockers, notes
    receipt = installed.receipt
    previous = receipt.get("previous")
    backup_name = receipt.get("backup")
    if not isinstance(previous, dict) or not isinstance(backup_name, str) or not re.fullmatch(r"[0-9a-f]{32}", backup_name):
        blockers.append("the last operation recorded no previous installation to return to")
        return sections, blockers, notes
    manifest, problem = load_backup_checked(layout, backup_name)
    if manifest is None:
        blockers.append(f"the backup record for the last operation ({backup_name[:8]}) cannot be used: {problem}; nothing can be restored safely")
        return sections, blockers, notes
    problems = verify_backup_files(layout, backup_name, manifest)
    if problems:
        blockers += problems
        return sections, blockers, notes
    sections["backup_identity"] = backup_identity(layout, backup_name, manifest)
    preimages: dict[str, Any] = {}
    target: dict[str, Any] = {"kind": previous.get("kind"), "version": previous.get("version"), "state_schema_version": previous.get("state_schema_version")}
    if previous.get("kind") == "generation":
        gid = previous.get("generation_id")
        gen_manifest, problems = verify_generation(layout.generations / str(gid), expect_id=str(gid), expect_program_id=previous.get("program_id"))
        if gen_manifest is None or problems:
            blockers.append(f"the previous generation {gid} is missing or does not verify: {'; '.join(problems)}")
        else:
            target.update(generation_id=gid, program_id=gen_manifest.get("program_id"), version=gen_manifest.get("version"), state_schema_version=gen_manifest.get("state_schema_version"))
    elif previous.get("kind") == "legacy":
        legacy = manifest.get("legacy_lib") or {}
        if not legacy.get("files"):
            blockers.append("the backup holds no legacy program files")
        target["legacy_files"] = legacy.get("files")
    else:
        blockers.append("the recorded previous installation is of an unknown kind")
    recorded = _recorded_assets(receipt)
    for entry in manifest.get("entries", []):
        if entry.get("role") == "config":
            continue
        dest = entry["dest"]
        current = describe(Path(dest))
        preimages[dest] = current
        after = entry.get("after")
        expected_after = after if after is not None else {"state": "absent"}
        matches = (current["state"] == "absent") if after is None else (current["state"] == "file" and current["sha256"] == after["sha256"])
        if not matches:
            blockers.append(f"{dest} changed since the last operation ({current.get('kind') or current['state']}); it will not be overwritten by a rollback")
        elif after is not None and recorded.get(dest, {}).get("sha256") not in (None, expected_after.get("sha256")):
            blockers.append(f"{dest} does not match the installation receipt")
    blockers += [f"not at a quiet point: {problem}" for problem in workload_problems(layout, probe_lock=not env.worker_lock_held)]
    schema = state_schema_on_disk(layout)
    max_schema = target.get("state_schema_version")
    if schema == -1:
        blockers.append("the task-state file exists but its schema cannot be read")
    elif schema is not None and type(max_schema) is int and schema > max_schema:
        blockers.append(f"task state has schema {schema}, newer than the previous release supports ({max_schema}); state is never rolled back to force old code to run")
    unit_dests = {entry["dest"] for entry in manifest.get("entries", []) if entry.get("role") == "unit"}
    units_now = {Path(dest).name for dest in unit_dests}
    services, service_blockers = _service_plan(env, "rollback", units_now, set(), bool(units_now))
    if previous.get("kind") == "generation" and not is_hex_id(previous.get("program_id")):
        blockers.append("the receipt records no program identity for the previous generation, so it cannot be anchored")
    blockers += service_blockers
    sections.update(candidate=target, backup=backup_name, preimages=preimages, services=services, previous={"kind": "generation", "generation_id": installed.active_generation,
                    "program_id": (installed.manifest or {}).get("program_id"), "version": (installed.manifest or {}).get("version"),
                    "state_schema_version": (installed.manifest or {}).get("state_schema_version")},
                    assets=[], removals=[], legacy_program_files=None)
    return sections, blockers, notes


# ------------------------------------------------------------------------------------------------ backups


def is_hex_id(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def load_backup_checked(layout: Layout, name: str) -> tuple[dict[str, Any] | None, str]:
    """The backup record with its exact-type schema enforced, or (None, why). The directory and the record must be real, not links."""
    base = layout.backups / name
    try:
        info = os.lstat(base)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            return None, "the backup directory is not a real directory"
        value = json.loads(read_bytes_nofollow(base / "manifest.json", 8 * 1024 * 1024))
        return validate_backup_manifest(layout, name, value), ""
    except (OSError, ValueError) as error:
        return None, f"unreadable ({error})"
    except InstallError as error:
        return None, str(error)


def load_backup_manifest(layout: Layout, name: str) -> dict[str, Any] | None:
    return load_backup_checked(layout, name)[0]


def verify_backup_files(layout: Layout, name: str, manifest: dict[str, Any]) -> list[str]:
    """Every stored copy is a regular no-follow file at its canonical location beneath THIS backup, and matches its recorded hash."""
    problems: list[str] = []
    base = layout.backups / name
    for entry in manifest.get("entries", []):
        before = entry["before"]
        if before["state"] == "file":
            state = describe(base / entry["file"])
            if state["state"] != "file" or state["sha256"] != before["sha256"]:
                problems.append(f"backup copy of {entry['dest']} is missing or altered")
    legacy = manifest.get("legacy_lib") or {}
    for fname, digest in (legacy.get("files") or {}).items():
        state = describe(base / "legacy-lib" / fname)
        if state["state"] != "file" or state["sha256"] != digest:
            problems.append(f"backup copy of legacy {fname} is missing or altered")
    return problems


def backup_identity(layout: Layout, name: str, manifest: dict[str, Any]) -> str:
    """Hash of the validated backup record together with the content hash of every stored copy: what a rollback approval binds."""
    copies = {entry["file"]: entry["before"]["sha256"] for entry in manifest["entries"] if entry["before"]["state"] == "file"}
    return canonical_hash({"manifest": {k: v for k, v in manifest.items() if k != "previous_receipt"}, "copies": copies, "legacy": (manifest.get("legacy_lib") or {}).get("files")})


# ------------------------------------------------------------------------------------------------ operation context


@dataclasses.dataclass
class Ctx:
    env: Env
    kind: str
    plan: dict[str, Any]
    payload: Payload | None
    operation_id: str
    approval_hash: str

    @property
    def layout(self) -> Layout:
        return self.env.layout

    @property
    def backup_dir(self) -> Path:
        return self.layout.backups / self.operation_id


def _ensure_dir(path: Path, mode: int, *, owned: bool = False) -> None:
    if os.path.lexists(path):
        if owned and path.is_symlink():
            raise InstallError(f"{path} is a symbolic link; refusing to use it as a managed directory")
        if not path.is_dir():
            raise InstallError(f"{path} exists and is not a directory")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    os.mkdir(path, mode)
    os.chmod(path, mode)


def step_dirs(ctx: Ctx) -> str:
    owned = {ctx.layout.lib, ctx.layout.generations, ctx.layout.manager, ctx.layout.ops, ctx.layout.backups}
    for path, mode in managed_directories(ctx.env):
        _ensure_dir(path, mode, owned=path in owned)
    return "directories present"


def step_generation(ctx: Ctx) -> str:
    assert ctx.payload is not None
    return f"generation {publish_generation(ctx.env, ctx.payload, ctx.operation_id)}"


def _copy_for_backup(ctx: Ctx, source: Path, target: Path) -> None:
    write_file_atomic(target, read_bytes_nofollow(source), 0o600)


def step_backup(ctx: Ctx) -> str:
    """Record and copy the preimage of everything this operation may replace, BEFORE the first replacement.
    Idempotent: an existing manifest is authoritative and is never rewritten (the targets may already have moved)."""
    base = ctx.backup_dir
    manifest_path = base / "manifest.json"
    if os.path.lexists(manifest_path):
        return "backup already recorded"
    for directory in (ctx.layout.backups, base, base / "files"):
        ensure_private_dir(directory)
    entries: list[dict[str, Any]] = []
    targets: list[tuple[str, str | None, dict[str, Any] | None]] = []
    wanted = {item["dest"]: item for item in ctx.plan.get("assets", [])}
    for dest, item in sorted(wanted.items()):
        targets.append((dest, item["role"], {"sha256": item["sha256"], "mode": item["mode"]}))
    for item in ctx.plan.get("removals", []):
        targets.append((item["dest"], item.get("role"), None))
    for dest in ctx.plan.get("config_edits", []) or []:
        targets.append((dest, "config", None))
    for index, (dest, role, after) in enumerate(targets):
        before = describe(Path(dest))
        recorded_before = ctx.plan.get("preimages", {}).get(dest)
        if recorded_before is not None and recorded_before != before:
            raise InstallError(f"{dest} changed after the plan was approved", code=EXIT_REFUSED)
        file_name = None
        if before["state"] == "file":
            file_name = f"files/{index}"
            _copy_for_backup(ctx, Path(dest), base / file_name)
        elif before["state"] == "unsafe":
            raise InstallError(f"{dest} is a {before['kind']}; it cannot be backed up")
        entries.append({"dest": dest, "role": role, "before": before, "file": file_name, "after": after})
    legacy_files = ctx.plan.get("legacy_program_files")
    legacy_lib = None
    if legacy_files:
        ensure_private_dir(base / "legacy-lib")
        for name in sorted(legacy_files):
            _copy_for_backup(ctx, ctx.layout.lib / name, base / "legacy-lib" / name)
        legacy_lib = {"files": dict(legacy_files)}
    record = {"schema": 1, "operation_id": ctx.operation_id, "kind": ctx.kind, "entries": entries, "legacy_lib": legacy_lib, "previous": ctx.plan.get("previous"),
              "previous_receipt": read_receipt(ctx.layout)}
    core.atomic_write_json(manifest_path, record, mode=0o600)
    return f"{len(entries)} files recorded"


def step_quiesce(ctx: Ctx) -> str:
    stop = ctx.plan["services"]["stop"]
    if not stop:
        return "no active units to stop"
    systemd = Systemd(ctx.env.host)
    for unit in stop:
        systemd.act("stop", unit)
    return f"stopped {', '.join(stop)}"


@contextlib.contextmanager
def critical(ctx: Ctx) -> Any:
    """The short window in which files, the pointer or the receipt change. The controller's transaction lock is held and the whole
    quiet-point predicate is re-evaluated INSIDE it, so work that arrived after planning (a command, a decision, a started job)
    stops the mutation instead of being overtaken by it. Nothing in the window waits on anything slow."""
    with StateStore(ctx.layout.paths, ctx.env.clock).transaction():
        if (ctx.plan.get("installed") or {}).get("layout") != "absent":
            problems = workload_problems(ctx.layout, probe_lock=False)
            if problems:
                raise InstallError("not at a quiet point (work arrived after the plan was approved): " + "; ".join(problems), code=EXIT_REFUSED)
        yield


def step_assets(ctx: Ctx) -> str:
    with critical(ctx):
        return _write_assets(ctx)


def _write_assets(ctx: Ctx) -> str:
    """Apply every enumerated auxiliary asset. Each is either at its approved preimage or already applied; anything else is drift."""
    changed = 0
    for item in ctx.plan.get("assets", []):
        dest = Path(item["dest"])
        current = describe(dest)
        if current["state"] == "file" and current["sha256"] == item["sha256"] and current["mode"] == item["mode"]:
            continue
        expected = ctx.plan["preimages"].get(item["dest"], {"state": "absent"})
        if current != expected:
            raise InstallError(f"{dest} changed after the plan was approved; nothing further was replaced", code=EXIT_REFUSED)
        assert ctx.payload is not None
        key = f"{item['role']}/{dest.name}"
        write_file_atomic(dest, ctx.payload.blobs[key], item["mode"])
        changed += 1
    for item in ctx.plan.get("removals", []):
        dest = Path(item["dest"])
        current = describe(dest)
        if current["state"] == "absent":
            continue
        if current["state"] != "file" or current["sha256"] != item["sha256"]:
            raise InstallError(f"{dest} changed after the plan was approved; it was not removed", code=EXIT_REFUSED)
        dest.unlink()
        fsync_dir(dest.parent)
        changed += 1
    return f"{changed} assets written or removed"


def step_initial_config(ctx: Ctx) -> str:
    """Create each configuration file only when absent (O_EXCL, mode 0600). An existing file is never read or changed."""
    assert ctx.payload is not None
    created = []
    for attr, example in _INITIAL_CONFIG:
        dest: Path = getattr(ctx.layout, attr)
        if create_file_exclusive(dest, ctx.payload.blobs[f"example/{example}"], 0o600):
            created.append(f"{dest.parent.name}/{dest.name}")
    return f"created {', '.join(created)}" if created else "existing configuration preserved"


def step_retire_legacy(ctx: Ctx) -> str:
    with critical(ctx):
        return _retire_legacy(ctx)


def _retire_legacy(ctx: Ctx) -> str:
    """Remove the legacy flat program files after the generation is active; each must still be exactly the backed-up bytes."""
    removed = 0
    for name, digest in sorted((ctx.plan.get("legacy_program_files") or {}).items()):
        path = ctx.layout.lib / name
        state = describe(path)
        if state["state"] == "absent":
            continue
        if state["state"] != "file" or state["sha256"] != digest:
            raise InstallError(f"legacy {name} changed; it was left in place", code=EXIT_REFUSED)
        path.unlink()
        removed += 1
    fsync_dir(ctx.layout.lib)
    return f"{removed} legacy files retired (backed up first)"


def step_compat_links(ctx: Ctx) -> str:
    for name in ctx.plan.get("compat_links") or []:
        ensure_compat_link(ctx.layout, name)
    return "documented program-directory paths link to the active generation"


def step_reload(ctx: Ctx) -> str:
    Systemd(ctx.env.host).act("daemon-reload")
    return "user manager reloaded"


def step_switch(ctx: Ctx) -> str:
    gid = ctx.plan["candidate"]["generation_id"]
    with critical(ctx):
        switch_active(ctx.env, gid, ctx.operation_id[:12])
    return f"active generation {gid}"


def build_receipt(ctx: Ctx) -> dict[str, Any]:
    candidate = ctx.plan["candidate"]
    return {
        "schema": 1, "operation_id": ctx.operation_id, "kind": ctx.kind, "approval_hash": ctx.approval_hash, "completed_at": core.iso_utc(ctx.env.clock()),
        "version": candidate.get("version"), "generation_id": candidate.get("generation_id"), "program_id": candidate.get("program_id"), "payload_id": candidate.get("payload_id"),
        "state_schema_version": candidate.get("state_schema_version"), "source": candidate.get("source"), "previous": ctx.plan.get("previous"),
        "assets": ctx.plan.get("receipt_assets", ctx.plan.get("assets", [])), "services": {"stopped": ctx.plan["services"]["stop"], "restored": ctx.plan["services"]["restore"]},
        "backup": ctx.operation_id, "options": ctx.plan.get("options"), "payload_root": ctx.plan.get("payload_root"),
    }


def step_receipt(ctx: Ctx) -> str:
    core.atomic_write_json(ctx.layout.receipt, build_receipt(ctx), mode=0o600)
    return "receipt written"


def smoke_check(ctx: Ctx) -> list[str]:
    """Each launcher must resolve the active generation and answer --help; the pointer must name the expected generation."""
    problems: list[str] = []
    expected = ctx.plan["candidate"].get("generation_id")
    try:
        active = read_active(ctx.layout)
    except InstallError as error:
        return [str(error)]
    if expected is not None and active != expected:
        problems.append(f"the active generation is {active}, expected {expected}")
    env = {**os.environ, "HOME": str(ctx.layout.home), "HERDR_SUPERVISOR_LIB": str(ctx.layout.lib)}
    for name in SMOKE_LAUNCHERS:
        launcher = ctx.layout.bin / name
        if not launcher.exists():
            if name == "herdr-setup" and ctx.plan["candidate"].get("kind") == "legacy":
                continue  # the legacy release never shipped this launcher
            problems.append(f"launcher {name} is missing")
            continue
        result = ctx.env.host.run([str(launcher), "--help"], timeout=60, env=env)
        if result.rc != 0:
            problems.append(f"{name} --help failed (exit {result.rc}): {(result.err or result.out).strip()[-200:]}")
    return problems


def step_smoke(ctx: Ctx) -> str:
    problems = smoke_check(ctx)
    if problems:
        raise InstallError("post-switch smoke check failed: " + "; ".join(problems))
    return "launchers resolve the active generation and answer --help"


def step_restore_services(ctx: Ctx) -> str:
    restore = ctx.plan["services"]["restore"]
    if not restore:
        return "no stopped units to restore"
    systemd = Systemd(ctx.env.host)
    for unit in restore:
        systemd.act("start", unit)
    return f"restored {', '.join(restore)}"


def step_migrate(ctx: Ctx) -> str:
    launcher = ctx.layout.bin / "herdr-supervisor"
    child_env = {**os.environ, "HOME": str(ctx.layout.home), "HERDR_SUPERVISOR_LIB": str(ctx.layout.lib)}
    child_env.setdefault("HERDR_BIN_PATH", str(ctx.layout.bin / "herdr"))  # the supported Herdr layout, so the supervisor's own resolver finds it
    result = ctx.env.host.run([str(launcher), "migrate"], timeout=300, env=child_env)
    if result.rc != 0:
        raise InstallError(f"`herdr-supervisor migrate` failed: {(result.err or result.out).strip()[-300:]}", action="the state backup it makes is kept; inspect it before retrying")
    return "state migration ran (explicitly selected)"


# ------------------------------------------------------------------------------------------------ restore (rollback and revert)


def restore_entries(ctx: Ctx, backup_name: str, manifest: dict[str, Any]) -> int:
    """Put every recorded asset back to its pre-operation state. Each is either at the operation's result (restored)
    or already restored (skipped); any other state is drift and stops the restore."""
    base = ctx.layout.backups / backup_name
    restored = 0
    for entry in manifest.get("entries", []):
        if entry.get("role") == "config":
            continue  # configuration edits are recovery records only; a rollback never rewrites the user's configuration
        dest = Path(entry["dest"])
        before = entry["before"]
        after = entry.get("after")
        current = describe(dest)
        if before["state"] == "absent":
            if current["state"] == "absent":
                continue
            if after is None or current["state"] != "file" or current["sha256"] != after["sha256"]:
                raise InstallError(f"{dest} changed; the restore stopped without overwriting it", code=EXIT_REFUSED)
            dest.unlink()
            fsync_dir(dest.parent)
            restored += 1
            continue
        if current["state"] == "file" and current["sha256"] == before["sha256"]:
            continue
        at_result = (current["state"] == "absent") if after is None else (current["state"] == "file" and current["sha256"] == after["sha256"])
        if not at_result:
            raise InstallError(f"{dest} changed; the restore stopped without overwriting it", code=EXIT_REFUSED)
        data = read_bytes_nofollow(base / entry["file"])
        if sha256_bytes(data) != before["sha256"]:
            raise InstallError(f"the backup copy of {dest} no longer matches its recorded hash; it was not written", code=EXIT_REFUSED)
        write_file_atomic(dest, data, int(before["mode"]))
        restored += 1
    return restored


def step_restore_assets(ctx: Ctx) -> str:
    name = ctx.plan["backup"]
    manifest = load_backup_manifest(ctx.layout, name)
    if manifest is None:
        raise InstallError("the backup record is missing; nothing was restored")
    problems = verify_backup_files(ctx.layout, name, manifest)
    if problems:
        raise InstallError("; ".join(problems))
    if backup_identity(ctx.layout, name, manifest) != ctx.plan.get("backup_identity"):
        raise InstallError("the backup changed after the rollback was approved; nothing was restored", code=EXIT_REFUSED)
    with critical(ctx):
        return f"{restore_entries(ctx, name, manifest)} assets restored"


def step_restore_pointer(ctx: Ctx) -> str:
    with critical(ctx):
        return _restore_pointer(ctx)


def _restore_pointer(ctx: Ctx) -> str:
    target = ctx.plan["candidate"]
    if target.get("kind") == "legacy":
        for name in COMPAT_LINKS:
            remove_compat_link(ctx.layout, name)
        base = ctx.layout.backups / ctx.plan["backup"] / "legacy-lib"
        for name, digest in sorted((target.get("legacy_files") or {}).items()):
            current = describe(ctx.layout.lib / name)
            if current["state"] == "file" and current["sha256"] == digest:
                continue
            if current["state"] != "absent":
                raise InstallError(f"{ctx.layout.lib / name} exists with other content; legacy files were not restored")
            write_file_atomic(ctx.layout.lib / name, read_bytes_nofollow(base / name), 0o644)
        with contextlib.suppress(FileNotFoundError):
            os.unlink(ctx.layout.active)
        fsync_dir(ctx.layout.lib)
        return "legacy program files restored; the active pointer was removed"
    switch_active(ctx.env, str(target["generation_id"]), ctx.operation_id[:12])
    return f"active generation {target['generation_id']}"


def step_rollback_receipt(ctx: Ctx) -> str:
    """The receipt after a rollback describes the restored release (its own earlier receipt) plus this operation."""
    target = ctx.plan["candidate"]
    old = load_backup_manifest(ctx.layout, ctx.plan["backup"]) or {}
    prior = old.get("previous_receipt") or {}
    receipt = {
        **prior, "schema": 1, "operation_id": ctx.operation_id, "kind": "rollback", "approval_hash": ctx.approval_hash, "completed_at": core.iso_utc(ctx.env.clock()),
        "version": target.get("version"), "generation_id": target.get("generation_id"), "program_id": target.get("program_id"),
        "state_schema_version": target.get("state_schema_version"), "previous": ctx.plan.get("previous"), "assets": prior.get("assets", []),
        "services": {"stopped": ctx.plan["services"]["stop"], "restored": ctx.plan["services"]["restore"]}, "backup": ctx.operation_id, "rolled_back_operation": ctx.plan["backup"],
    }
    core.atomic_write_json(ctx.layout.receipt, receipt, mode=0o600)
    return "receipt written"


def step_verify_rollback(ctx: Ctx) -> str:
    target = ctx.plan["candidate"]
    if target.get("kind") != "legacy":
        manifest, problems = verify_generation(ctx.layout.generations / str(target["generation_id"]), expect_id=str(target["generation_id"]), expect_program_id=target.get("program_id"))
        if manifest is None or problems:
            raise InstallError("the previous generation does not verify: " + "; ".join(problems))
    backup, why = load_backup_checked(ctx.layout, ctx.plan["backup"])
    if backup is None:
        raise InstallError(f"the backup record cannot be used: {why}")
    problems = verify_backup_files(ctx.layout, ctx.plan["backup"], backup)
    if problems:
        raise InstallError("; ".join(problems))
    if backup_identity(ctx.layout, ctx.plan["backup"], backup) != ctx.plan.get("backup_identity"):
        raise InstallError("the backup changed after the rollback was approved", code=EXIT_REFUSED)
    return "previous release and backups verified"


def step_rollback_backup(ctx: Ctx) -> str:
    """Record the state this rollback replaces, so it can itself be undone."""
    base = ctx.backup_dir
    manifest_path = base / "manifest.json"
    if os.path.lexists(manifest_path):
        return "backup already recorded"
    old = load_backup_manifest(ctx.layout, ctx.plan["backup"])
    assert old is not None
    for directory in (ctx.layout.backups, base, base / "files"):
        ensure_private_dir(directory)
    entries: list[dict[str, Any]] = []
    for entry in old["entries"]:
        if entry.get("role") == "config":
            continue
        index = len(entries)
        before = describe(Path(entry["dest"]))
        file_name = None
        if before["state"] == "file":
            file_name = f"files/{index}"
            _copy_for_backup(ctx, Path(entry["dest"]), base / file_name)
        restored = entry["before"]
        after = {"sha256": restored["sha256"], "mode": restored["mode"]} if restored["state"] == "file" else None
        entries.append({"dest": entry["dest"], "role": entry.get("role"), "before": before, "file": file_name, "after": after})
    core.atomic_write_json(manifest_path, {"schema": 1, "operation_id": ctx.operation_id, "kind": "rollback", "entries": entries, "legacy_lib": None, "previous": ctx.plan.get("previous"),
                                          "previous_receipt": read_receipt(ctx.layout)}, mode=0o600)
    return f"{len(entries)} files recorded"


# ------------------------------------------------------------------------------------------------ step lists


Describe = tuple[str, str, bool]  # (id, title, external)


def file_steps(ctx: Ctx) -> list[Step]:
    plan = ctx.plan
    steps: list[Step] = []
    if plan.get("kind") == "rollback":
        steps += [Step("verify", "verify the previous release and backups", lambda: step_verify_rollback(ctx))]
        steps += [Step("backup", "record the state this rollback replaces", lambda: step_rollback_backup(ctx))]
        if plan["services"]["stop"]:
            steps += [Step("quiesce", "stop the active managed units", lambda: step_quiesce(ctx))]
        steps += [Step("restore_assets", "restore backed-up assets", lambda: step_restore_assets(ctx))]
        if plan["services"]["reload"]:
            steps += [Step("reload", "reload the user manager", lambda: step_reload(ctx))]
        steps += [Step("switch", "reactivate the previous release", lambda: step_restore_pointer(ctx)), Step("receipt", "write the receipt", lambda: step_rollback_receipt(ctx)),
                  Step("smoke", "smoke-check the launchers", lambda: step_smoke(ctx))]
        if plan["services"]["restore"]:
            steps += [Step("restore_services", "restart the previously active units", lambda: step_restore_services(ctx))]
        return steps
    if not plan.get("files_needed"):
        if plan.get("config_edits"):  # configuration is edited only after its preimage is recorded
            steps.append(Step("backup", "record the configuration files that will be edited", lambda: step_backup(ctx)))
        return steps
    steps.append(Step("dirs", "create missing directories", lambda: step_dirs(ctx)))
    steps.append(Step("generation", "publish the immutable program generation", lambda: step_generation(ctx)))
    steps.append(Step("backup", "back up everything that will be replaced", lambda: step_backup(ctx)))
    if plan["services"]["stop"]:
        steps.append(Step("quiesce", "stop the active managed units", lambda: step_quiesce(ctx)))
    steps.append(Step("assets", "install launchers, units, documents and examples", lambda: step_assets(ctx)))
    if ctx.kind == "setup":
        steps.append(Step("config", "create missing initial configuration files", lambda: step_initial_config(ctx)))
    if plan["services"]["reload"]:
        steps.append(Step("reload", "reload the user manager", lambda: step_reload(ctx)))
    steps.append(Step("switch", "atomically activate the generation", lambda: step_switch(ctx)))
    if plan.get("legacy_program_files"):
        steps.append(Step("retire_legacy", "retire the backed-up legacy program files", lambda: step_retire_legacy(ctx)))
    if plan.get("compat_links"):
        steps.append(Step("compat_links", "keep the documented hook path working through a link to the active generation", lambda: step_compat_links(ctx)))
    steps.append(Step("receipt", "write the installation receipt", lambda: step_receipt(ctx)))
    steps.append(Step("smoke", "smoke-check the launchers", lambda: step_smoke(ctx)))
    if plan.get("options", {}).get("migrate"):
        steps.append(Step("migrate", "run the explicitly selected state migration", lambda: step_migrate(ctx)))
    if plan["services"]["restore"]:
        steps.append(Step("restore_services", "restart the previously active units", lambda: step_restore_services(ctx)))
    return steps


def describe_steps(steps: list[Step]) -> list[dict[str, Any]]:
    return [{"id": step.id, "title": step.title, "external": step.external} for step in steps]


# ------------------------------------------------------------------------------------------------ apply


@dataclasses.dataclass
class ApplyResult:
    status: str  # COMPLETE | NOOP | ALREADY_APPLIED | FAILED | FAILED_REVERTED | RECONCILING | REFUSED
    message: str = ""
    failed_step: str | None = None
    operation_id: str | None = None
    steps: list[dict[str, Any]] = dataclasses.field(default_factory=list)


def _diff_keys(approved: dict[str, Any], current: dict[str, Any]) -> list[str]:
    return sorted(key for key in set(approved) | set(current) if approved.get(key) != current.get(key))


def revert_failed_update(ctx: Ctx) -> str | None:
    """After a failed post-switch smoke check, put the previous release back. Returns an error text, or None on success."""
    previous = ctx.plan.get("previous")
    if not previous:
        return None
    try:
        manifest = load_backup_manifest(ctx.layout, ctx.operation_id)
        if manifest is None:
            return "the backup record is missing"
        problems = verify_backup_files(ctx.layout, ctx.operation_id, manifest)
        if problems:
            return "; ".join(problems)
        restore_entries(ctx, ctx.operation_id, manifest)
        if previous.get("kind") == "generation":
            switch_active(ctx.env, str(previous["generation_id"]), ctx.operation_id[:12])
            prior = manifest.get("previous_receipt")
            if isinstance(prior, dict):
                core.atomic_write_json(ctx.layout.receipt, prior, mode=0o600)
        else:
            plan = {"candidate": {"kind": "legacy", "legacy_files": (manifest.get("legacy_lib") or {}).get("files")}, "backup": ctx.operation_id}
            _restore_pointer(dataclasses.replace(ctx, plan=plan))
        if ctx.plan["services"]["reload"]:
            Systemd(ctx.env.host).act("daemon-reload")
        for unit in ctx.plan["services"]["restore"]:
            Systemd(ctx.env.host).act("start", unit)
    except InstallError as error:
        return str(error)
    return None


def _restart_after_early_failure(ctx: Ctx, journal: Journal) -> str:
    """A failure before the pointer switched leaves the old release active: put back the units this operation stopped, and mark
    the stop as not done so a resumed plan quiesces again before it touches any file."""
    stop = ctx.plan["services"]["stop"]
    if not stop or journal.step("quiesce")["status"] != "DONE" or journal.step("switch")["status"] != "PENDING":
        return ""
    restarted = []
    systemd = Systemd(ctx.env.host)
    for unit in ctx.plan["services"]["restore"]:
        try:
            systemd.act("start", unit)
            restarted.append(unit)
        except InstallError:
            pass
    journal.mark("quiesce", "PENDING", "units were restarted after a failed operation; resuming this plan stops them again first")
    return f"; restarted {', '.join(restarted)} (the previous release is still active)" if restarted else ""


def archive_reverted(layout: Layout, operation_id: str, clock: Callable[[], float]) -> None:
    """A reverted operation is closed: its journal and backup are renamed so the same approved plan can start a fresh attempt."""
    suffix = f".reverted-{int(clock())}"
    for path in (layout.ops / f"{operation_id}.json", layout.backups / operation_id):
        if os.path.lexists(path):
            os.rename(path, path.with_name(path.name + suffix))


def _journal_binding_problem(journal: Journal, operation_id: str, kind: str, approval_hash: str, *, complete: bool = False) -> str | None:
    """A journal is authority only when it is bound to exactly this approval: identity, kind, plan hash, and a well-formed step list.
    A completion marker is trusted only when every step is recorded DONE."""
    data = journal.data
    if not is_int(data.get("schema")) or data["schema"] != 1:
        return "an unsupported schema"
    if data.get("operation_id") != operation_id or data.get("kind") != kind or data.get("approval_hash") != approval_hash:
        return "another operation's identity"
    plan = data.get("plan")
    if not isinstance(plan, dict) or canonical_hash(plan) != approval_hash:
        return "a plan that does not match the approval"
    steps = data.get("steps")
    if not isinstance(steps, list) or not steps or any(not isinstance(item, dict) or not isinstance(item.get("id"), str) or item.get("status") not in STEP_STATES for item in steps):
        return "a malformed step list"
    if len({item["id"] for item in steps}) != len(steps):
        return "duplicate steps"
    if complete and (data.get("status") != "COMPLETE" or any(item["status"] != "DONE" for item in steps)):
        return "a completion marker without every step recorded done"
    return None


_IMMUTABLE_KEYS = ("kind", "tool", "target", "options", "payload_root", "candidate", "dependency")


def _resume_problems(env: Env, kind: str, plan_data: dict[str, Any], current: Plan, operation_id: str) -> list[str]:
    """Why an interrupted operation may not simply continue. The enforcing tool, target, options, candidate and dependency are the
    approved ones and cannot move; what the operation itself already changed (its own assets, pointer, receipt) is accepted only
    when it matches its recorded preimage or planned result; any other drift, a newly running unit, or newly arrived work refuses
    before another side effect."""
    layout = env.layout
    problems: list[str] = []
    keys = ("kind", "tool", "target", "options") if kind == "rollback" else _IMMUTABLE_KEYS
    for key in keys:
        if current.data.get(key) != plan_data.get(key):
            problems.append(f"{key} changed since the approval")
    preimages = plan_data.get("preimages") or {}
    for item in plan_data.get("assets") or []:
        found = describe(Path(item["dest"]))
        applied = found.get("state") == "file" and found.get("sha256") == item["sha256"] and found.get("mode") == item["mode"]
        if not applied and found != preimages.get(item["dest"], {"state": "absent"}):
            problems.append(f"{item['dest']} was changed outside this operation")
    for item in plan_data.get("removals") or []:
        found = describe(Path(item["dest"]))
        if found.get("state") != "absent" and found != preimages.get(item["dest"]):
            problems.append(f"{item['dest']} was changed outside this operation")
    if kind == "rollback":
        backup, why = load_backup_checked(layout, str(plan_data.get("backup")))
        if backup is None:
            problems.append(f"the backup cannot be used: {why}")
        else:
            for entry in backup["entries"]:
                if entry["role"] == "config":
                    continue
                found = describe(Path(entry["dest"]))
                restored = found == entry["before"] or (found.get("state") == "file" and entry["before"].get("state") == "file" and found.get("sha256") == entry["before"]["sha256"])
                if not restored and found != preimages.get(entry["dest"]):
                    problems.append(f"{entry['dest']} was changed outside this operation")
    previous = plan_data.get("previous") or {}
    allowed = {previous.get("generation_id"), (plan_data.get("candidate") or {}).get("generation_id")}
    try:
        if read_active(layout) not in allowed:
            problems.append("the active pointer names a release this operation does not involve")
        receipt = read_receipt(layout)
        installed_receipt = (plan_data.get("installed") or {}).get("receipt")
        if receipt is not None and receipt.get("operation_id") != operation_id and canonical_hash(receipt) != installed_receipt:
            problems.append("the installation receipt was replaced by another operation")
    except InstallError as error:
        problems.append(str(error))
    present = managed_unit_names(layout)
    if present:
        systemd = Systemd(env.host)
        if not systemd.available():
            problems.append("the user systemd manager cannot be reached, so unit states cannot be established")
        else:
            planned = set(plan_data["services"]["stop"]) | set((plan_data.get("components") or {}).get("activate") or [])
            for unit in present:
                state = systemd.state(unit)
                if state in ("activating", "deactivating", "reloading", "refreshing", "unknown"):
                    problems.append(f"{unit} is {state}")
                elif state == "active" and unit != SERVER_UNIT and unit not in planned:
                    problems.append(f"{unit} became active outside this operation")
    if (plan_data.get("installed") or {}).get("layout") != "absent":
        problems += [f"not at a quiet point: {item}" for item in workload_problems(layout, probe_lock=False)]
    return problems


def apply_operation(
    env: Env, kind: str, approved: dict[str, Any], approval_hash: str, *, rebuild: Callable[[], Plan], make_steps: Callable[[Ctx, Payload | None], list[Step]],
) -> ApplyResult:
    """Spend one approval. Serialized by the deployment lock; the operation identity is the approval hash, so a
    concurrent or repeated apply of the same plan joins the same journal instead of starting another operation.
    The worker lock is taken before the plan is re-evaluated, so the quiet point it proves cannot end underneath it."""
    layout = env.layout
    operation_id = approval_hash[:32]
    first = _fresh_refusal(approved, rebuild(), operation_id)
    if first is not None and first.status == "NOOP":
        return first  # nothing would change, so no lock is needed (and a busy worker does not matter)
    if first is not None and not layout.deploy_lock.exists():
        # No operation has ever run here, so no one can be mid-way: a refusal needs no lock and must not write anything.
        # (If another process creates the lock while this check runs, decide under the lock instead.)
        return first
    for directory in (layout.state, layout.manager, layout.ops):
        ensure_private_dir(directory)
    with DeployLock(layout.deploy_lock, wait=env.lock_wait):
        journal_path = layout.ops / f"{operation_id}.json"
        try:
            journal = Journal.load(journal_path, env.clock)
        except InstallError as error:
            return ApplyResult("RECONCILING", str(error), operation_id=operation_id)
        if journal is not None and journal.data.get("status") == "COMPLETE":
            bad = _journal_binding_problem(journal, operation_id, kind, approval_hash, complete=True)
            if bad is not None:
                return ApplyResult("REFUSED", f"the completed-operation record is not trusted: it has {bad}", operation_id=operation_id)
            return ApplyResult("ALREADY_APPLIED", "this approved plan was already applied; nothing was repeated", operation_id=operation_id, steps=journal.data["steps"])
        try:
            with worker_quiesced(layout):
                env.worker_lock_held = True
                try:
                    return _apply_locked(env, kind, approved, approval_hash, operation_id, journal, rebuild, make_steps)
                finally:
                    env.worker_lock_held = False
        except InstallError as error:
            return ApplyResult("REFUSED", str(error), operation_id=operation_id)


def _fresh_refusal(approved: dict[str, Any], current: Plan, operation_id: str) -> ApplyResult | None:
    """Why a never-started approval cannot be spent, or None. Pure: reads the already-built plan only."""
    if current.data != approved:
        return ApplyResult("REFUSED", "the approval is stale: " + ", ".join(_diff_keys(approved, current.data)) + " changed since the plan was written", operation_id=operation_id)
    if current.blockers:
        return ApplyResult("REFUSED", "; ".join(current.blockers), operation_id=operation_id)
    if current.noop:
        return ApplyResult("NOOP", "nothing to do: the installation already matches this plan", operation_id=operation_id)
    return None


def _apply_locked(env: Env, kind: str, approved: dict[str, Any], approval_hash: str, operation_id: str, journal: Journal | None, rebuild: Callable[[], Plan],
                  make_steps: Callable[[Ctx, Payload | None], list[Step]]) -> ApplyResult:
    layout = env.layout
    try:
        current = rebuild()
    except InstallError as error:
        return ApplyResult("REFUSED", f"the plan can no longer be evaluated: {error}", operation_id=operation_id)
    if journal is None:
        refused = _fresh_refusal(approved, current, operation_id)
        if refused is not None:
            return refused
        plan_data = current.data
    else:
        bad = _journal_binding_problem(journal, operation_id, kind, approval_hash)
        if bad is not None:
            return ApplyResult("REFUSED", f"the interrupted operation's record is not trusted: it has {bad}", operation_id=operation_id)
        plan_data = journal.data["plan"]
    payload: Payload | None = None
    if kind != "rollback":
        payload = load_payload(Path(plan_data["payload_root"]))
        if payload.summary() != plan_data["candidate"]:
            return ApplyResult("REFUSED", "the candidate payload changed after the plan was approved", operation_id=operation_id)
    ctx = Ctx(env, kind, plan_data, payload, operation_id, approval_hash)
    steps = make_steps(ctx, payload)
    if journal is not None:
        if [item["id"] for item in journal.data["steps"]] != [step.id for step in steps]:
            return ApplyResult("REFUSED", "the interrupted operation's step list does not match the approved plan", operation_id=operation_id)
        resume = _resume_problems(env, kind, plan_data, current, operation_id)
        if resume:
            return ApplyResult("REFUSED", "this interrupted operation cannot continue: " + "; ".join(resume), operation_id=operation_id)
    else:
        journal = Journal.create(layout.ops / f"{operation_id}.json", operation_id=operation_id, kind=kind, approval_hash=approval_hash, plan=plan_data,
                                 step_ids=[step.id for step in steps], clock=env.clock)
    outcome = run_steps(env, journal, steps)
    if outcome.status == "COMPLETE":
        return ApplyResult("COMPLETE", "applied", operation_id=operation_id, steps=journal.data["steps"])
    if outcome.status == "FAILED" and outcome.failed_step == "smoke" and kind in ("update", "rollback") and plan_data.get("previous"):
        problem = revert_failed_update(ctx)
        if problem is None:
            journal.finish("FAILED_REVERTED", {"step": "smoke", "error": outcome.message})
            archive_reverted(layout, operation_id, env.clock)
            return ApplyResult("FAILED_REVERTED", f"{outcome.message}; the previous release was restored", failed_step="smoke", operation_id=operation_id, steps=journal.data["steps"])
        journal.finish("RECONCILING", {"step": "smoke", "error": outcome.message, "revert_error": problem})
        return ApplyResult("RECONCILING", f"{outcome.message}; restoring the previous release also failed ({problem}); backups are kept", failed_step="smoke", operation_id=operation_id, steps=journal.data["steps"])
    message = outcome.message
    if outcome.status == "FAILED":
        message += _restart_after_early_failure(ctx, journal)
    return ApplyResult(outcome.status, message, failed_step=outcome.failed_step, operation_id=operation_id, steps=journal.data["steps"])


def preflight_units(env: Env, payload: Payload) -> dict[str, Any]:
    """Validate the exact candidate unit bytes before any target write. A genuine defect refuses the operation."""
    return verify_units(env, payload)


__all__ = [
    "EXIT_ACTION", "KINDS", "PLAN_SCHEMA", "UNIT_FOR_SERVICE", "ApplyResult", "Ctx", "Plan", "PayloadFile", "apply_operation", "describe_steps", "file_steps",
    "load_plan_document", "plan_installation", "plan_rollback", "preflight_units", "tool_identity",
]
