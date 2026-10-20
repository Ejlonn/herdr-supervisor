"""Read-only live session catalog and opt-in, bounded native child observations."""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
from collections import Counter
from pathlib import Path
from typing import Any

from herdr_cli import match_exact_session, session_identity
from herdr_core import SupervisorError, atomic_write_json, iso_utc, load_json, valid_pane_id

NATIVE_VERSIONS = {"0.154.0"}  # recorded native parent/child contract; unknown versions fail closed
MAX_NATIVE_BYTES = 1024 * 1024
MAX_LINE_BYTES = 65536


def canonical_identity(value: Any) -> bool:
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value
    except (ValueError, AttributeError):
        return False


def session_catalog(live: list[dict[str, Any]], owners: dict[str, Any], queries: dict[str, Any], helpers: dict[str, Any]) -> list[dict[str, Any]]:
    counts = Counter((a.get("agent"), session_identity(a)) for a in live if session_identity(a))
    rows = []
    for agent in live:
        provider, pane, identity = agent.get("agent"), agent.get("pane_id"), session_identity(agent)
        role, expected = "unmanaged", None
        for kind, registry in (("workflow_owner", owners), ("query", queries), ("helper", helpers)):
            for key, binding in registry.items():
                if binding.get("pane_id") == pane or (identity and binding.get("session_id") == identity and binding.get("provider", key) == provider):
                    role, expected = kind, dict(binding, provider=binding.get("provider", key))
                    break
            if expected:
                break
        reason = None
        if provider not in ("codex", "claude") or not valid_pane_id(pane):
            reason = "unsupported provider or pane"
        elif not canonical_identity(identity):
            reason = "native identity unavailable; verify the native session integration"
        elif match_exact_session(live, provider, identity).provider_conflicts:
            reason = "native identity carried by a different provider; inspect conflicting panes"
        elif counts[provider, identity] != 1:
            reason = "duplicate native identity; inspect duplicate panes"
        elif expected and expected.get("provider") != provider:
            reason = "registered pane hosts a different provider; explicit recovery required"
        elif expected and expected.get("session_id") != identity:
            reason = "session differs from registered owner; explicit rebind required"
        elif expected and expected.get("pane_id") != pane:
            reason = "registered session moved; explicit pane repair required"
        verified = reason is None
        if verified and agent.get("agent_status") not in ("idle", "done"):
            reason = "agent is not ready; wait for settlement"
        rows.append({"provider": provider, "pane_id": pane, "session_id": identity, "role": role,
                     "identity_verified": verified, "lifecycle": agent.get("agent_status"), "usable": reason is None, "reason": reason,
                     "configured_model": expected.get("model") if expected else None,
                     "observed_model": None, "model_evidence": "not supplied by verified inventory"})
    return rows


def native_meta(record: Any, parent: str) -> dict[str, Any] | None:
    if not isinstance(record, dict) or record.get("type") != "session_meta":
        return None
    p = record.get("payload")
    if not isinstance(p, dict) or p.get("cli_version") not in NATIVE_VERSIONS or not canonical_identity(p.get("id")):
        return None
    source = p.get("source")
    sub = source.get("subagent") if isinstance(source, dict) else None
    spawn = sub.get("thread_spawn") if isinstance(sub, dict) else None
    if not isinstance(spawn, dict) or spawn.get("parent_thread_id") != parent or p["id"] == parent:
        return None
    depth = spawn.get("depth")
    if type(depth) is not int or not 1 <= depth <= 8:
        return None
    return {"provider": "codex", "session_id": p["id"], "parent_session_id": parent, "depth": depth,
            "schema": "codex-session-meta/" + p["cli_version"], "model": None,
            "lifecycle": "observed", "usage": None, "usage_relation": "unknown; not added to parent totals",
            "pane_target": None}


def read_native_file(path: Path, roots: list[Path], parent: str, cursor: dict[str, Any]) -> dict[str, Any]:
    """An explicit file only; no global newest-session search or transcript body retained."""
    resolved = path.resolve(strict=True)
    if path.is_symlink() or not any(resolved.is_relative_to(root.resolve(strict=True)) for root in roots):
        raise SupervisorError("native transcript is outside the configured roots or is a symlink")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        stat = os.fstat(fd)
        opened = Path(os.readlink(f"/proc/self/fd/{fd}")).resolve(strict=True)
        if not any(opened.is_relative_to(root.resolve(strict=True)) for root in roots) or opened != resolved:
            raise SupervisorError("native transcript changed during open; retry after verifying the path")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            head = handle.readline(MAX_LINE_BYTES + 1)
            if len(head) > MAX_LINE_BYTES or not head.endswith(b'\n'):
                raise SupervisorError("native metadata is incomplete or oversized; retry after a complete record")
            try:
                meta = native_meta(json.loads(head), parent)
            except (ValueError, UnicodeError):
                meta = None
            if meta is None:
                raise SupervisorError("native relationship/version is unsupported or does not match the parent")
            key = hashlib.sha256(head).hexdigest()
            unchanged = cursor.get("inode") == [stat.st_dev, stat.st_ino] and cursor.get("head") == key
            offset = cursor.get("offset", 0) if unchanged else 0
            if type(offset) is not int or not len(head) <= offset <= stat.st_size:
                offset = len(head)
            previous = cursor.get("summary")
            if unchanged and cursor.get('offset') == offset and offset >= len(head) and isinstance(previous, dict):
                meta["model"] = previous.get("model")
            handle.seek(offset)
            data = handle.read(MAX_NATIVE_BYTES)
            last = data.rfind(b'\n')
            if last >= 0:
                complete = data[:last + 1]
                for line in complete.splitlines():
                    if len(line) > MAX_LINE_BYTES:
                        # Large message/tool bodies are irrelevant to child metadata.
                        # Ignore them within the already bounded chunk; never export them.
                        continue
                    try:
                        record = json.loads(line)
                    except (ValueError, UnicodeError):
                        continue
                    if not isinstance(record, dict):
                        continue
                    payload = record.get("payload")
                    if record.get("type") == "turn_context" and isinstance(payload, dict):
                        model = payload.get("model")
                        if isinstance(model, str) and re.fullmatch(r'[A-Za-z0-9_.:/-]{1,80}', model):
                            meta["model"] = model
                offset += len(complete)
            elif len(data) == MAX_NATIVE_BYTES:
                raise SupervisorError("native record exceeded bounded read limit")
            return {"inode": [stat.st_dev, stat.st_ino], "head": key, "offset": offset, "summary": meta}
    finally:
        os.close(fd)


def refresh_native(state_dir: Path, config: dict[str, Any], known_parents: set[str]) -> dict[str, Any]:
    section = config.get("native_tracking", {})
    if not section.get("enabled"):
        return {"enabled": False, "children": [], "reason": "native tracking is opt-in"}
    cache_path = state_dir / 'native-children.json'
    cache = load_json(cache_path, label='native child cursors') if cache_path.exists() else {}
    roots = [Path(p) for p in section.get("roots", [])]
    entries = section.get("files", [])
    if not isinstance(entries, list) or len(entries) > 32:
        raise SupervisorError("native tracking accepts at most 32 explicitly registered files")
    cursors: dict[str, Any] = {}
    children: dict[str, Any] = {}
    errors: list[str] = []
    conflicts: set[str] = set()
    for entry in entries:
        parent = entry.get('parent_session_id') if isinstance(entry, dict) else None
        path = entry.get('path') if isinstance(entry, dict) else None
        key = hashlib.sha256(str(path).encode()).hexdigest()
        if parent not in known_parents or not isinstance(path, str):
            errors.append('native parent is not a verified registered/live session')
            continue
        try:
            observation = read_native_file(Path(path), roots, parent, cache.get('cursors', {}).get(key, {}))
            cursors[key] = observation
            summary = observation['summary']
            identity = summary['session_id']
            if identity in conflicts:
                raise SupervisorError('conflicting native child records')
            if identity in children and children[identity] != summary:
                conflicts.add(identity)
                del children[identity]
                raise SupervisorError('conflicting native child records')
            children[identity] = summary
        except (OSError, SupervisorError, ValueError):
            errors.append('native relationship unavailable; check registered root, file, schema and parent')
    report = {'enabled': True, 'children': list(children.values()), 'errors': errors, 'refreshed_at': iso_utc(time.time()), 'evidence': 'cached metadata, not current native lifecycle'}
    atomic_write_json(cache_path, {'schema_version': 1, 'cursors': cursors, 'report': report})
    return report


def native_snapshot(state_dir: Path) -> dict[str, Any]:
    path = state_dir / 'native-children.json'
    if not path.exists():
        return {'children': [], 'reason': 'not refreshed; use sessions --refresh-native after opting in'}
    value = load_json(path, label='native child summary')
    return value.get('report', {'children': []})
