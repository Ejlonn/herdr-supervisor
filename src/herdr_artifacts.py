"""Protected artifact registry for Telegram document delivery.

Only registered artifacts can ever be sent. Registration accepts (a) a supervisor/agent-produced Markdown
or text file below an allowlisted root, or (b) generated text (reports, logs, query answers). The source
is read within a byte bound, screened against protected-path categories, redacted into an immutable
no-overwrite `.md` derivative under supervisor state, hashed, and recorded with its run/task/event identity.
The authoritative source is never modified. No command accepts an arbitrary filesystem path.
"""

from __future__ import annotations

import os
import re
import stat
import uuid
from pathlib import Path
from typing import Any

import herdr_present as hp
import herdr_redaction as hr
from herdr_supervisor import Paths, SupervisorError, atomic_write_json, iso_utc, load_json, sha256_bytes

CATEGORIES = ("plan", "brief", "review", "handoff", "report", "final_report", "query_answer", "logs", "details", "audit", "validation", "followup")
ALLOWED_SUFFIXES = (".md", ".txt")
PROTECTED_NAME_RE = re.compile(r"(?i)(^|[/\\])(\.env(\..*)?|bot-token|owners\.json|state\.json|control\.json|query-providers\.json|query-owner\.json|\.netrc|id_rsa.*|id_ed25519.*|.*\.pem|.*\.key|credentials.*|\.credentials\.json|config\.json|.*\.lock|.*\.sock|known_hosts|authorized_keys|\.git($|[/\\]).*)$")
PROTECTED_DIR_PARTS = {".ssh", ".gnupg", ".aws", ".config", ".claude", ".codex", ".herdr", "herdr-telegram", "herdr-telegram-locks", ".env.d", "secrets", "credentials", ".mozilla", ".chrome", "chromium", "wireguard", "openvpn"}
_ARTIFACT_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")


def artifacts_dir(paths: Paths) -> Path:
    return paths.state_dir / "artifacts"


def registry_dir(paths: Paths) -> Path:
    return artifacts_dir(paths) / "registry"


def outbound_dir(paths: Paths) -> Path:
    return artifacts_dir(paths) / "outbound"


def generated_dir(paths: Paths) -> Path:
    return artifacts_dir(paths) / "generated"


def _ensure_dirs(paths: Paths) -> None:
    for directory in (artifacts_dir(paths), registry_dir(paths), outbound_dir(paths), generated_dir(paths)):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(directory, 0o700)


def _protected(path: Path) -> bool:
    if PROTECTED_NAME_RE.search(str(path)):
        return True
    return any(part in PROTECTED_DIR_PARTS for part in path.parts)


def _write_exclusive(path: Path, data: bytes) -> None:
    """No-overwrite creation (O_EXCL), fsync, 0600."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def allowed_roots(paths: Paths, config: dict[str, Any]) -> list[Path]:
    roots = [Path(config["review_root"]), generated_dir(paths)]
    extra = (config.get("telegram") or {}).get("artifact_roots") or config.get("artifact_roots") or []
    roots += [Path(str(r)) for r in extra if isinstance(r, str) and r.startswith("/")]
    return roots


def check_source(path_text: str, *, paths: Paths, config: dict[str, Any]) -> Path:
    """A regular, non-symlink, owner-owned .md/.txt file below an allowlisted root and outside every
    protected category; bounded by max_artifact_bytes."""
    if not isinstance(path_text, str) or not path_text.startswith("/") or "\x00" in path_text:
        raise SupervisorError("artifact path must be absolute")
    path = Path(path_text)
    try:
        info = path.lstat()
    except OSError as error:
        raise SupervisorError("artifact is missing") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise SupervisorError("artifact must be a regular, non-symlink file")
    if info.st_uid != os.getuid():
        raise SupervisorError("artifact must be owned by the current user")
    if path.suffix.lower() not in ALLOWED_SUFFIXES:
        raise SupervisorError("artifact must be a .md or .txt file")
    if info.st_size > int(config.get("max_artifact_bytes", 2 * 1024 * 1024)):
        raise SupervisorError("artifact exceeds max_artifact_bytes")
    if _protected(path):
        raise SupervisorError("artifact path is in a protected category")
    resolved = path.resolve()
    if not any(root.resolve() in resolved.parents for root in allowed_roots(paths, config) if root.exists()):
        raise SupervisorError("artifact is outside the allowlisted roots")
    return path


def _read_source(path: Path, *, paths: Paths, config: dict[str, Any]) -> bytes:
    """Read the validated file through a no-follow descriptor and detect path replacement.

    Validation and reading cannot be separate operations for an exposure boundary: a same-user process
    could otherwise replace the file with a symlink between ``lstat`` and ``read_bytes``.
    """
    max_bytes = int(config.get("max_artifact_bytes", 2 * 1024 * 1024))
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as error:
        raise SupervisorError("artifact could not be opened safely") from error
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or opened.st_uid != os.getuid():
            raise SupervisorError("artifact must be a regular file owned by the current user")
        if opened.st_size > max_bytes:
            raise SupervisorError("artifact exceeds max_artifact_bytes")
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining:
            chunk = os.read(fd, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > max_bytes:
            raise SupervisorError("artifact exceeds max_artifact_bytes")
        try:
            current = path.lstat()
        except OSError as error:
            raise SupervisorError("artifact changed while it was read") from error
        if stat.S_ISLNK(current.st_mode) or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
            raise SupervisorError("artifact changed while it was read")
        # Recheck the resolved location after the descriptor read to catch parent-directory replacement.
        resolved = path.resolve()
        if not any(root.resolve() in resolved.parents for root in allowed_roots(paths, config) if root.exists()):
            raise SupervisorError("artifact is outside the allowlisted roots")
        return raw
    finally:
        os.close(fd)


def _derive(raw: bytes, *, max_bytes: int) -> str:
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise SupervisorError("artifact is not UTF-8 text") from error
    if "\x00" in text:
        raise SupervisorError("artifact contains NUL bytes")
    redacted = hr.redact(text, limit=max_bytes)
    if hr.has_sensitive_remainder(redacted):
        raise SupervisorError("artifact may contain sensitive material that could not be classified safely")
    # Fail closed when redaction removed something we cannot classify as safe to summarize away entirely.
    if redacted.count("[redacted]") > 50:
        raise SupervisorError("artifact contains too much sensitive material to expose")
    return redacted


def register_file(paths: Paths, config: dict[str, Any], *, category: str, source_path: str, run_id: str | None, task_id: str | None = None, event_id: str | None = None, title: str | None = None, expected_source_sha256: str | None = None, expected_source_bytes: int | None = None) -> dict[str, Any]:
    if category not in CATEGORIES:
        raise SupervisorError(f"artifact category {category!r} is not allowed")
    source = check_source(source_path, paths=paths, config=config)
    raw = _read_source(source, paths=paths, config=config)
    actual_sha256 = sha256_bytes(raw)
    if expected_source_sha256 is not None and (not _SHA_RE.fullmatch(expected_source_sha256) or actual_sha256 != expected_source_sha256):
        raise SupervisorError("artifact changed after it was approved or registered")
    if expected_source_bytes is not None and (isinstance(expected_source_bytes, bool) or not isinstance(expected_source_bytes, int) or len(raw) != expected_source_bytes):
        raise SupervisorError("artifact byte count does not match its descriptor")
    return _register(paths, config, category=category, source=source, raw=raw, run_id=run_id, task_id=task_id, event_id=event_id, title=title or source.name)


def register_text(paths: Paths, config: dict[str, Any], *, category: str, text: str, name: str, run_id: str | None, task_id: str | None = None, event_id: str | None = None, title: str | None = None) -> dict[str, Any]:
    """Generated content (reports, logs, answers) becomes an immutable source under generated/ first."""
    if category not in CATEGORIES:
        raise SupervisorError(f"artifact category {category!r} is not allowed")
    if not isinstance(text, str) or not text.strip():
        raise SupervisorError("artifact text is empty")
    _ensure_dirs(paths)
    safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", name)[:60].strip("._") or "artifact"
    source = generated_dir(paths) / f"{safe_name}-{uuid.uuid4().hex[:12]}.md"
    raw = text.encode("utf-8")
    if len(raw) > int(config.get("max_artifact_bytes", 2 * 1024 * 1024)):
        raise SupervisorError("generated artifact exceeds max_artifact_bytes")
    _write_exclusive(source, raw)
    return _register(paths, config, category=category, source=source, raw=raw, run_id=run_id, task_id=task_id, event_id=event_id, title=title or safe_name)


def _register(paths: Paths, config: dict[str, Any], *, category: str, source: Path, raw: bytes, run_id: str | None, task_id: str | None, event_id: str | None, title: str) -> dict[str, Any]:
    _ensure_dirs(paths)
    max_bytes = int(config.get("max_artifact_bytes", 2 * 1024 * 1024))
    derivative_text = _derive(raw, max_bytes=max_bytes)
    artifact_id = uuid.uuid4().hex
    identity = hp.abbrev(event_id or task_id, 8) if (event_id or task_id) else "noevent"
    stem = f"{category}-{hp.abbrev(run_id) if run_id else 'norun'}-{identity}-{artifact_id[:8]}"
    derivative = outbound_dir(paths) / f"{stem}.md"
    derivative_bytes = derivative_text.encode("utf-8")
    if len(derivative_bytes) > max_bytes:
        raise SupervisorError("redacted artifact exceeds max_artifact_bytes")
    _write_exclusive(derivative, derivative_bytes)
    record = {
        "schema_version": 1, "artifact_id": artifact_id, "category": category, "title": hr.redact(title, limit=120),
        "run_id": run_id, "task_id": task_id, "event_id": event_id,
        "source_path": str(source), "source_sha256": sha256_bytes(raw), "source_bytes": len(raw),
        "derivative_path": str(derivative), "derivative_sha256": sha256_bytes(derivative_bytes), "derivative_bytes": len(derivative_bytes),
        "mime": "text/markdown", "display_name": f"{stem}.md", "exposure": "telegram_owner", "status": "registered",
        "created_at": iso_utc(_now()), "created_at_unix": _now(), "redactions": derivative_text.count("[redacted]"),
    }
    atomic_write_json(registry_dir(paths) / f"{artifact_id}.json", record)
    return record


_clock = None


def _now() -> float:
    import time  # noqa: PLC0415

    return float(_clock()) if _clock else time.time()


def load_record(paths: Paths, artifact_id: str) -> dict[str, Any]:
    if not isinstance(artifact_id, str) or not _ARTIFACT_ID_RE.match(artifact_id):
        raise SupervisorError("invalid artifact id")
    path = registry_dir(paths) / f"{artifact_id}.json"
    if not path.exists():
        raise SupervisorError("artifact is not registered")
    record = load_json(path, label="artifact record")
    if not isinstance(record, dict) or record.get("schema_version") != 1 or record.get("artifact_id") != artifact_id or record.get("category") not in CATEGORIES or record.get("status") != "registered":
        raise SupervisorError("artifact record is corrupt")
    for key in ("derivative_path", "derivative_sha256", "display_name", "status"):
        if not isinstance(record.get(key), str):
            raise SupervisorError("artifact record is corrupt")
    if not _SHA_RE.match(record["derivative_sha256"]) or Path(record["derivative_path"]).parent != outbound_dir(paths):
        raise SupervisorError("artifact record is corrupt")
    return record


def verify_for_send(paths: Paths, config: dict[str, Any], artifact_id: str) -> tuple[dict[str, Any], bytes]:
    """Immediately before a send: registered id, expected controlled path, regular owned file, bounded size,
    exact hash. Returns (record, derivative bytes)."""
    record = load_record(paths, artifact_id)
    derivative = Path(record["derivative_path"])
    max_bytes = int(config.get("max_artifact_bytes", 2 * 1024 * 1024))
    try:
        fd = os.open(derivative, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as error:
        raise SupervisorError("artifact derivative is missing or unsafe") from error
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or opened.st_uid != os.getuid() or opened.st_size > max_bytes:
            raise SupervisorError("artifact derivative is unsafe")
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining:
            chunk = os.read(fd, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        try:
            current = derivative.lstat()
        except OSError as error:
            raise SupervisorError("artifact derivative changed while it was read") from error
        if stat.S_ISLNK(current.st_mode) or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
            raise SupervisorError("artifact derivative changed while it was read")
    finally:
        os.close(fd)
    if len(data) > max_bytes or sha256_bytes(data) != record["derivative_sha256"] or len(data) != int(record.get("derivative_bytes") or -1):
        raise SupervisorError("artifact derivative changed; refusing to send")
    return record, data


def list_records(paths: Paths, run_id: str | None = None) -> list[dict[str, Any]]:
    directory = registry_dir(paths)
    if not directory.exists():
        return []
    out = []
    for path in sorted(directory.glob("*.json")):
        try:
            record = load_record(paths, path.stem)
        except SupervisorError:
            continue
        if run_id is None or record.get("run_id") == run_id:
            out.append(record)
    return sorted(out, key=lambda item: (str(item.get("created_at") or ""), str(item.get("artifact_id") or "")))
