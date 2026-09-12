"""Provider quota snapshots: parsing, blocking-window selection, resume timing, and safe report conversion."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any, Iterable

from herdr_core import QUOTA_KINDS, QuotaError, _epoch, _number, iso_local, iso_utc


@dataclasses.dataclass(frozen=True)
class QuotaWindow:
    kind: str
    remaining_percent: float
    resets_at: float

    def blocks_at(self, now: float) -> bool:
        return self.remaining_percent <= 0 and self.resets_at > now

@dataclasses.dataclass(frozen=True)
class QuotaSnapshot:
    provider: str
    fetched_at: float | None
    windows: tuple[QuotaWindow, ...]
    context_used_percent: float | None
    window_source: str

def _parse_windows(raw_windows: Any, provider: str) -> tuple[QuotaWindow, ...]:
    if not isinstance(raw_windows, list) or not raw_windows:
        raise QuotaError(f"{provider} quota snapshot has no windows")
    parsed: dict[str, QuotaWindow] = {}
    for raw in raw_windows:
        if not isinstance(raw, dict):
            raise QuotaError(f"{provider} quota window is not an object")
        kind = raw.get("kind")
        if kind not in QUOTA_KINDS:
            continue
        remaining = _number(raw.get("remaining_percent"))
        resets_at = _epoch(raw.get("resets_at"))
        if remaining is None or not 0 <= remaining <= 100:
            raise QuotaError(f"{provider} {kind} remaining_percent is invalid or outside 0-100")
        if resets_at is None:
            raise QuotaError(f"{provider} {kind} resets_at is invalid")
        if kind in parsed:
            raise QuotaError(f"{provider} quota snapshot repeats window {kind}")
        parsed[kind] = QuotaWindow(kind, remaining, resets_at)
    if not parsed:
        raise QuotaError(f"{provider} quota snapshot has no five_hour/weekly windows")
    return tuple(parsed[kind] for kind in QUOTA_KINDS if kind in parsed)

def _select_windows(value: dict[str, Any], provider: str, session_id: str | None) -> tuple[Any, str]:
    """Mirror the quota plugin's `windows_for_session` order; never borrow another session's windows."""
    session_only = value.get("session_quota_only")
    if session_only is not None and not isinstance(session_only, bool):
        raise QuotaError(f"{provider} session_quota_only is invalid")
    raw_session_windows, raw_scopes, raw_scope_windows = value.get("session_windows"), value.get("session_quota_scopes"), value.get("quota_scope_windows")
    session_windows: dict[str, Any] = raw_session_windows if isinstance(raw_session_windows, dict) else {}
    scopes: dict[str, Any] = raw_scopes if isinstance(raw_scopes, dict) else {}
    scope_windows: dict[str, Any] = raw_scope_windows if isinstance(raw_scope_windows, dict) else {}
    if session_only:
        if not session_id:
            raise QuotaError(f"{provider} quota is session-local but no owned session id is known")
        if session_id not in session_windows:
            raise QuotaError(f"{provider} quota is session-local and has no windows for session {session_id}")
        return session_windows[session_id], f"session_windows[{session_id}]"
    if not session_id:
        return value.get("windows"), "windows"
    scope = scopes.get(session_id)
    if isinstance(scope, str) and scope in scope_windows:
        return scope_windows[scope], f"quota_scope_windows[{scope}]"
    if session_id in session_windows:
        return session_windows[session_id], f"session_windows[{session_id}]"
    if not session_windows and not scopes and not scope_windows:
        return value.get("windows"), "windows"
    raise QuotaError(f"{provider} quota snapshot has keyed windows but none for session {session_id}")

def parse_quota_snapshot(path: Path, provider: str, *, session_id: str | None = None) -> QuotaSnapshot:
    """Parse one plugin snapshot. Fails closed on anything missing or malformed."""
    try:
        with path.open(encoding="utf-8") as handle:
            value = json.load(handle)
    except FileNotFoundError as error:
        raise QuotaError(f"{provider} quota snapshot is missing: {path}") from error
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as error:
        raise QuotaError(f"{provider} quota snapshot is corrupt: {path}: {error}") from error
    if not isinstance(value, dict):
        raise QuotaError(f"{provider} quota snapshot must be a JSON object")
    found_provider = value.get("provider")
    if found_provider not in (None, provider):
        raise QuotaError(f"{provider} quota snapshot belongs to provider {found_provider!r}")
    raw_windows, window_source = _select_windows(value, provider, session_id)
    windows = _parse_windows(raw_windows, provider)
    fetched = value.get("fetched_at_unix")
    fetched_at = _epoch(fetched)
    if fetched is not None and fetched_at is None:
        raise QuotaError(f"{provider} fetched_at_unix is invalid")
    context_percent: float | None = None
    context = value.get("context")
    if session_id and isinstance(value.get("session_contexts"), dict) and isinstance(value["session_contexts"].get(session_id), dict):
        context = value["session_contexts"][session_id]
    if isinstance(context, dict):
        context_percent = _number(context.get("used_percent"))
    return QuotaSnapshot(provider, fetched_at, windows, context_percent, window_source)

def blocking_windows(snapshot: QuotaSnapshot, now: float) -> tuple[QuotaWindow, ...]:
    return tuple(window for window in snapshot.windows if window.blocks_at(now))

def quota_resume_at(blocking: Iterable[QuotaWindow], buffer_seconds: float) -> float:
    return max(window.resets_at for window in blocking) + float(buffer_seconds)

def quota_as_dict(snapshot: QuotaSnapshot, now: float) -> dict[str, Any]:
    return {
        "ok": True,
        "provider": snapshot.provider,
        "fetched_at_unix": snapshot.fetched_at,
        "fetched_at_local": iso_local(snapshot.fetched_at) if snapshot.fetched_at else None,
        "window_source": snapshot.window_source,
        "context_used_percent": snapshot.context_used_percent,
        "windows": [
            {
                "kind": window.kind,
                "remaining_percent": window.remaining_percent,
                "resets_at": window.resets_at,
                "resets_at_utc": iso_utc(window.resets_at),
                "resets_at_local": iso_local(window.resets_at),
                "blocking": window.blocks_at(now),
            }
            for window in snapshot.windows
        ],
    }
