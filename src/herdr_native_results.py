"""Bounded, read-only recovery of an exact primary session's final routing result from its provider-native transcript.

The supported terminal read stays the normal path. This module is consulted only when that read returned no valid
current routing frame for a settled, accepted turn. It never searches unrelated sessions, never follows a path from a
payload, never reads child transcripts, never writes, and never routes by itself: it returns the single eligible
`ProtocolBlock` (or a specific reason it could not), and the caller passes that block through the ordinary
route/gate/validation path after re-verifying ownership and settlement.

Identity is established from the transcript itself, never from the file name alone:
  claude  every user/assistant record must be primary (`isSidechain` is exactly false, no `agentId`) and carry
          `sessionId` equal to the owned session; an inconsistent, missing or child-marked record refuses.
  codex   the first record must be `session_meta` whose `id` (and `session_id`, when present) equal the owned session
          and whose `source` is a plain string (child/subagent sessions carry an object and a `parent_thread_id`).
Provider schemas verified on retained local primary transcripts (see CLAUDE_HANDOFF.md):
  claude  <root>/<project-dir>/<session-id>.jsonl   record type=assistant, isSidechain=false, message.stop_reason=end_turn
          (text blocks); delivered prompt = record type=user, text content (no tool_result).
  codex   <root>/YYYY/MM/DD/rollout-<ts>-<session-id>.jsonl   response_item/message role=assistant phase=final_answer
          (output_text blocks) completed by an event_msg/task_complete with the same turn id; delivered prompt =
          response_item/message role=user (input_text).
Any other provider, or a result-bearing record outside these shapes, is reported unsupported/uncertain and never accepted.
Result-bearing records are validated container by container BEFORE any text is extracted (a final whose content, phase, role, message or
turn-id shape is unsupported is a suspect record, never an empty or partial result); documented non-result records are listed explicitly.
The file extent sampled before reading is re-checked (size, identity, modification time) after the read AND the parse: growth, truncation,
replacement or an in-place rewrite makes the result `uncertain` (no unbounded read-until-stable; a later retry on the settled file decides).

Work bound: ONE budget (directory entries visited, wall-clock seconds, bytes, records) covers discovery, metadata
verification, window read and parsing; it is never reset between phases. Unread, malformed, oversized or partial
records after the delivered prompt prevent attribution: the result is `uncertain`, never the earlier frame."""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
import stat
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from herdr_core import SupervisorError
from herdr_protocol import ProtocolBlock, parse_protocol

# Hard bounds: bytes read per attempt, records parsed, size of one record, directory entries visited, elapsed seconds.
MAX_SCAN_BYTES = 8 * 1024 * 1024
MAX_RECORDS = 40000
MAX_LINE_BYTES = 1024 * 1024
MAX_DIR_ENTRIES = 4000
MAX_ELAPSED_SECONDS = 3.0
SUPPORTED_PROVIDERS = ("claude", "codex")

FOUND, NONE, CONFLICT, UNSUPPORTED, UNAVAILABLE, UNCERTAIN, IDENTITY = "found", "none", "conflict", "unsupported", "unavailable", "uncertain", "identity"

_SESSION_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_YEAR, _TWO = re.compile(r"^\d{4}$"), re.compile(r"^\d{2}$")
_CHILD_MARKERS = ("parent_thread_id", "agent_nickname", "agent_path")
# Codex top-level record types observed on retained local rollouts that carry no result; response_item/event_msg are inspected, anything else is unsupported.
_CODEX_METADATA_TYPES = frozenset({"session_meta", "turn_context", "compacted", "world_state", "token_usage_record", "inter_agent_communication_metadata"})


@dataclasses.dataclass(frozen=True)
class NativeResult:
    status: str
    reason: str
    block: ProtocolBlock | None = None
    provenance: dict[str, Any] = dataclasses.field(default_factory=dict)

    @property
    def recoverable(self) -> bool:
        return self.status == FOUND and self.block is not None


class _Exhausted(Exception):
    """The single work budget ran out (entries or time): refuse safely."""


class _Budget:
    """One entry/time budget shared by every phase of a lookup or a recovery attempt. Constants are read at construction so tests may patch them."""

    def __init__(self, clock: Callable[[], float]) -> None:
        self.clock = clock
        self.deadline = clock() + MAX_ELAPSED_SECONDS
        self.entries = 0
        self.limit = MAX_DIR_ENTRIES

    def tick(self, count: int = 1) -> None:
        self.entries += count
        if self.entries > self.limit:
            raise _Exhausted(f"the bounded lookup visited more than {self.limit} directory entries")
        self.check()

    def check(self) -> None:
        if self.clock() > self.deadline:
            raise _Exhausted(f"the bounded native work exceeded {MAX_ELAPSED_SECONDS:g} seconds")


def default_roots() -> dict[str, Path]:
    home = Path(os.path.expanduser("~"))
    return {"claude": home / ".claude" / "projects", "codex": home / ".codex" / "sessions"}


def _valid_session(session_id: Any) -> bool:
    return isinstance(session_id, str) and _SESSION_RE.match(session_id) is not None


def _is_real_dir(entry: os.DirEntry[str]) -> bool:
    try:
        return entry.is_dir(follow_symlinks=False)
    except OSError:
        return False


def _single(paths: list[Path], what: str) -> tuple[Path | None, str | None]:
    if not paths:
        return None, f"{what} was not found under the provider's native-session root"
    if len(paths) > 1:
        return None, f"{what} exists in more than one location; refusing to choose"
    return paths[0], None


def _expected_relpath_ok(provider: str, relpath: Any, session_id: str) -> bool:
    if not isinstance(relpath, str) or not relpath or relpath.startswith("/") or "\x00" in relpath:
        return False
    parts = relpath.split("/")
    if any(part in ("", ".", "..") for part in parts):
        return False
    if provider == "claude":
        return len(parts) == 2 and parts[1] == f"{session_id}.jsonl"
    return (len(parts) == 4 and _YEAR.match(parts[0]) is not None and _TWO.match(parts[1]) is not None and _TWO.match(parts[2]) is not None
            and parts[3].startswith("rollout-") and parts[3].endswith(f"-{session_id}.jsonl"))


def _contained(root_real: Path, path: Path) -> bool:
    return str(os.path.realpath(path)).startswith(str(root_real) + os.sep)


def locate_session_file(provider: str, session_id: str, root: Path, budget: _Budget | None = None,
                        clock: Callable[[], float] = time.monotonic) -> tuple[Path | None, str | None]:
    """Identity-derived lookup: only a file whose NAME carries the exact session id, in a non-symlink directory directly below the configured
    root (claude: one project level; codex: YYYY/MM/DD). Every entry visited counts against ONE budget (also the elapsed-time deadline); budget
    exhaustion raises `_Exhausted`, which `resolve` turns into an `uncertain` result and `capture_cursor` into "no cursor"."""
    if provider not in SUPPORTED_PROVIDERS:
        return None, f"native result recovery is not supported for provider {provider!r}"
    if not _valid_session(session_id):
        return None, "the owned session id is not a canonical identity"
    budget = budget or _Budget(clock)
    try:
        root_real = Path(os.path.realpath(root))
        if not root_real.is_dir():
            return None, "the provider's native-session root is not available"
        found: list[Path] = []
        if provider == "claude":
            with os.scandir(root_real) as top:
                for entry in top:
                    budget.tick()
                    if not _is_real_dir(entry):
                        continue
                    candidate = Path(entry.path) / f"{session_id}.jsonl"
                    budget.tick()
                    if candidate.is_symlink():
                        continue
                    if candidate.is_file():
                        found.append(candidate)
        else:
            with os.scandir(root_real) as years:
                for year in years:
                    budget.tick()
                    if not (_is_real_dir(year) and _YEAR.match(year.name)):
                        continue
                    with os.scandir(year.path) as months:
                        for month in months:
                            budget.tick()
                            if not (_is_real_dir(month) and _TWO.match(month.name)):
                                continue
                            with os.scandir(month.path) as days:
                                for day in days:
                                    budget.tick()
                                    if not (_is_real_dir(day) and _TWO.match(day.name)):
                                        continue
                                    with os.scandir(day.path) as files:
                                        for item in files:
                                            budget.tick()
                                            if item.name.startswith("rollout-") and item.name.endswith(f"-{session_id}.jsonl") and item.is_file(follow_symlinks=False):
                                                found.append(Path(item.path))
        path, problem = _single(found, f"the {provider} primary transcript for session {session_id[:8]}")
        if path is not None and not _contained(root_real, path):
            return None, "the transcript path escapes the native-session root"
        return path, problem
    except OSError as error:
        return None, f"native-session storage is not accessible: {error.__class__.__name__}"


def capture_cursor(provider: str, session_id: str, root: Path, clock: Callable[[], float] = time.monotonic) -> dict[str, Any] | None:
    """Source boundary recorded just before a prompt is delivered: file identity, current size and the root-relative path (so the later recovery
    needs no directory scan). Best effort and bounded by the same budget rules; never raises, and an exhausted budget simply means no cursor."""
    try:
        path, _problem = locate_session_file(provider, session_id, root, clock=clock)
    except _Exhausted:
        return None
    if path is None:
        return None
    try:
        info = os.lstat(path)
        relpath = os.path.relpath(path, os.path.realpath(root))
    except (OSError, ValueError):
        return None
    if not stat.S_ISREG(info.st_mode) or not _expected_relpath_ok(provider, relpath, session_id):
        return None
    return {"provider": provider, "dev": info.st_dev, "ino": info.st_ino, "offset": info.st_size, "size": info.st_size, "relpath": relpath}


def _path_from_cursor(provider: str, session_id: str, root: Path, cursor: Any) -> Path | None:
    """The cursor's root-relative path, re-validated (shape, containment, non-symlink regular file). Never trusted beyond that; a failure falls back to the bounded lookup."""
    if not isinstance(cursor, dict) or cursor.get("provider") != provider or not _expected_relpath_ok(provider, cursor.get("relpath"), session_id):
        return None
    try:
        root_real = Path(os.path.realpath(root))
        path = root_real / cursor["relpath"]
        info = os.lstat(path)
        if not stat.S_ISREG(info.st_mode) or not _contained(root_real, path):
            return None
        return path
    except OSError:
        return None


def _first_line(fd: int, budget: _Budget) -> bytes | None:
    """The first record, separately bounded by MAX_LINE_BYTES (a window that starts later still needs the file's identity record)."""
    data = b""
    os.lseek(fd, 0, os.SEEK_SET)
    while len(data) <= MAX_LINE_BYTES:
        budget.check()
        chunk = os.read(fd, 65536)
        if not chunk:
            return data or None
        cut = chunk.find(b"\n")
        if cut >= 0:
            return (data + chunk[:cut]) if len(data) + cut <= MAX_LINE_BYTES else None
        data += chunk
    return None


def verify_codex_metadata(first_line: bytes | None, session_id: str) -> str | None:
    """None when the first record proves this rollout is the owned PRIMARY session; otherwise the reason. Containers/primitives are type-checked before nested access."""
    if first_line is None:
        return "the rollout has no readable first record to verify its identity"
    try:
        record = json.loads(first_line)
    except ValueError:
        return "the rollout's first record is not valid JSON; identity is unverified"
    if not isinstance(record, dict) or record.get("type") != "session_meta" or not isinstance(record.get("payload"), dict):
        return "the rollout does not start with a session_meta record; identity is unverified"
    meta = record["payload"]
    if meta.get("id") != session_id:
        return "the rollout's session_meta id is not the owned session; the file name alone does not establish identity"
    if "session_id" in meta and meta["session_id"] != session_id:
        return "the rollout belongs to a child of another session (session_id differs from id)"
    if not isinstance(meta.get("source"), str):
        return "the rollout is not a primary session (source is not a plain string: subagent/child session)"
    if any(marker in meta for marker in _CHILD_MARKERS):
        return "the rollout carries child-session markers; it is not the owned primary session"
    return None


def _read_window(fd: int, cursor: Any, provider: str, budget: _Budget) -> tuple[list[tuple[int, bytes]], dict[str, Any], str | None]:
    """Read at most MAX_SCAN_BYTES of whole lines. With a valid cursor: from the cursor to EOF. Without (or when the cursor span exceeds the
    bound): the last MAX_SCAN_BYTES, aligned to a line start. A trailing partial line is NOT silently dropped: its size is reported so the
    caller can refuse (an unread tail may hold a competing result)."""
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        return [], {}, "the native transcript is not a regular file"
    size = info.st_size
    meta: dict[str, Any] = {"dev": info.st_dev, "ino": info.st_ino, "size": size, "mtime_ns": info.st_mtime_ns, "cursor_used": False}
    start = max(0, size - MAX_SCAN_BYTES)
    if isinstance(cursor, dict) and cursor.get("provider") == provider:
        same_file = cursor.get("dev") == info.st_dev and cursor.get("ino") == info.st_ino
        offset = cursor.get("offset")
        if not same_file or type(offset) is not int or offset < 0 or offset > size:
            return [], meta, "the native transcript was rotated, replaced or truncated since the prompt was delivered"
        if size - offset <= MAX_SCAN_BYTES:
            start, meta["cursor_used"] = offset, True
    if start > 0 and not meta["cursor_used"]:
        os.lseek(fd, start - 1, os.SEEK_SET)
        if os.read(fd, 1) != b"\n":  # land on a line start: skip the partial line we started inside
            skipped = 0
            while skipped <= MAX_LINE_BYTES:
                budget.check()
                position = os.lseek(fd, 0, os.SEEK_CUR)
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                cut = chunk.find(b"\n")
                if cut >= 0:
                    start = position + cut + 1
                    break
                skipped += len(chunk)
            else:
                return [], meta, "no line boundary within the bounded tail window"
    os.lseek(fd, start, os.SEEK_SET)
    want = min(MAX_SCAN_BYTES, max(0, size - start))  # exactly the sampled extent: bytes appended after the sample are never parsed as if they were checked
    data = b""
    while len(data) < want:
        budget.check()
        chunk = os.read(fd, min(1 << 20, want - len(data)))
        if not chunk:
            break
        data += chunk
    meta["short_read"] = len(data) < want
    meta["offset_start"], meta["scanned_bytes"] = start, len(data)
    last_newline = data.rfind(b"\n")
    complete = data[: last_newline + 1] if last_newline >= 0 else b""
    meta["partial_tail_bytes"] = len(data) - len(complete)
    meta["offset_end"] = start + len(complete)
    lines: list[tuple[int, bytes]] = []
    position = start
    for raw in complete.split(b"\n")[:-1]:
        lines.append((position, raw))
        position += len(raw) + 1
    return lines, meta, None


def _extent_problem(fd: int, path: Path, meta: dict[str, Any]) -> str | None:
    """None when the file still is what was sampled before the read; otherwise why the bytes just read cannot be called the file's current extent."""
    try:
        now = os.fstat(fd)
        on_disk = os.lstat(path)
    except OSError as error:
        return f"the native transcript could no longer be inspected after it was read ({error.__class__.__name__})"
    if (on_disk.st_dev, on_disk.st_ino) != (now.st_dev, now.st_ino) or (now.st_dev, now.st_ino) != (meta.get("dev"), meta.get("ino")):
        return "the native transcript was rotated or replaced while it was being read"
    if meta.get("short_read") or now.st_size < meta["size"]:
        return "the native transcript shrank while it was being read"
    if now.st_size > meta["size"]:
        return f"the native transcript grew from {meta['size']} to {now.st_size} bytes while it was being read; the newest records are not covered"
    if now.st_mtime_ns != meta.get("mtime_ns"):
        return "the native transcript was modified in place while it was being read"
    return None


def _text_of(blocks: Any, kinds: tuple[str, ...]) -> str:
    if isinstance(blocks, str):
        return blocks
    if not isinstance(blocks, list):
        return ""
    parts = [b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") in kinds and isinstance(b.get("text"), str)]
    return "\n".join(parts)


def _strict_text(blocks: Any, kinds: tuple[str, ...]) -> str | None:
    """Text of a RESULT-BEARING content value, or None when its shape is unsupported. Valid empty content ("" or []) is empty text; every block must be an
    object with a string type; blocks of the wanted kinds must carry string text; other documented kinds (thinking, tool_use, reasoning...) carry none."""
    if isinstance(blocks, str):
        return blocks
    if not isinstance(blocks, list):
        return None
    parts: list[str] = []
    for block in blocks:
        if not isinstance(block, dict) or not isinstance(block.get("type"), str):
            return None
        if block["type"] in kinds:
            if not isinstance(block.get("text"), str):
                return None
            parts.append(block["text"])
    return "\n".join(parts)


def _claude_record(record: dict[str, Any], session_id: str, turn_id: str) -> tuple[str, str] | str | None:
    """('prompt', '') / ('final', text) for eligible records; None for harmless records; a string for an identity violation or an unsupported
    result-bearing shape ("identity:<why>" / "shape:<why>")."""
    kind = record.get("type")
    if kind not in ("user", "assistant"):
        return None  # metadata/attachment/snapshot records carry no result
    side = record.get("isSidechain")
    if side is True:
        return None  # child output inside the file: documented harmless, never eligible
    if side is not False:
        return "shape:a user/assistant record does not state whether it is primary"
    if "agentId" in record:
        return "identity:a record marked primary also carries an agentId (child marker)"
    if record.get("sessionId") != session_id:
        return "identity:a primary record's sessionId is missing or not the owned session"
    message = record.get("message")
    if not isinstance(message, dict):
        return "shape:a primary record has no message object"
    if record.get("isApiErrorMessage") or record.get("error"):
        return None
    if kind == "user" and message.get("role") == "user" and not record.get("isMeta"):
        content = message.get("content")
        if isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            return None
        text = _text_of(content, ("text",))
        return ("prompt", "") if turn_id in text else None
    if kind == "assistant":
        stop = message.get("stop_reason")
        if message.get("role") != "assistant":
            return "shape:an assistant record's message role is not assistant"
        if stop is not None and not isinstance(stop, str):
            return "shape:an assistant record's stop_reason is not a string"
        if stop == "end_turn":
            final_text = _strict_text(message.get("content"), ("text",))
            if final_text is None:
                return "shape:an end_turn final has unsupported content (not a string or a list of typed blocks with string text)"
            return "final", final_text
    return None


def scan_claude(lines: list[tuple[int, bytes]], session_id: str, turn_id: str, budget: _Budget) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    events: list[dict[str, Any]] = []
    stats: dict[str, Any] = {"records": 0, "suspects": [], "identity": None}
    for ordinal, (offset, raw) in enumerate(lines):
        budget.check()
        if stats["records"] >= MAX_RECORDS:
            stats["exhausted"] = True
            break
        if len(raw) > MAX_LINE_BYTES:
            stats["suspects"].append((ordinal, "an oversized record"))
            continue
        try:
            record = json.loads(raw)
        except ValueError:
            stats["suspects"].append((ordinal, "a malformed record"))
            continue
        stats["records"] += 1
        if not isinstance(record, dict):
            stats["suspects"].append((ordinal, "a non-object record"))
            continue
        result = _claude_record(record, session_id, turn_id)
        if isinstance(result, str):
            why = result.partition(":")[2]
            if result.startswith("identity:"):
                stats["identity"] = why
                break
            stats["suspects"].append((ordinal, why))
        elif result is not None:
            events.append({"kind": result[0], "text": result[1], "offset": offset, "ordinal": ordinal, "id": str(record.get("uuid") or "")})
    return events, stats


def scan_codex(lines: list[tuple[int, bytes]], session_id: str, turn_id: str, budget: _Budget) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    events: list[dict[str, Any]] = []
    stats: dict[str, Any] = {"records": 0, "suspects": [], "identity": None}
    completed: dict[str, int] = {}
    aborted: set[str] = set()
    for ordinal, (offset, raw) in enumerate(lines):
        budget.check()
        if stats["records"] >= MAX_RECORDS:
            stats["exhausted"] = True
            break
        if len(raw) > MAX_LINE_BYTES:
            stats["suspects"].append((ordinal, "an oversized record"))
            continue
        try:
            record = json.loads(raw)
        except ValueError:
            stats["suspects"].append((ordinal, "a malformed record"))
            continue
        stats["records"] += 1
        if not isinstance(record, dict):
            stats["suspects"].append((ordinal, "a non-object record"))
            continue
        rtype = record.get("type")
        if not isinstance(rtype, str):
            stats["suspects"].append((ordinal, "a record without a string type"))
            continue
        if rtype in _CODEX_METADATA_TYPES:
            continue  # documented non-result records (session/turn context, usage, compaction, inter-agent metadata)
        payload = record.get("payload")
        if rtype not in ("response_item", "event_msg"):
            stats["suspects"].append((ordinal, f"a record of unsupported type {rtype[:40]!r}"))
            continue
        if not isinstance(payload, dict):
            stats["suspects"].append((ordinal, f"a {rtype} record whose payload is not an object"))
            continue
        ptype = payload.get("type")
        if not isinstance(ptype, str):
            stats["suspects"].append((ordinal, f"a {rtype} record whose payload has no string type"))
            continue
        if rtype == "response_item":
            if ptype != "message":
                continue  # reasoning, tool calls and their outputs, compaction items: documented, never a final answer
            role = payload.get("role")
            if not isinstance(role, str):
                stats["suspects"].append((ordinal, "a message without a role"))
            elif role == "user":
                if turn_id in _text_of(payload.get("content"), ("input_text",)):
                    events.append({"kind": "prompt", "text": "", "offset": offset, "ordinal": ordinal, "id": ""})
            elif role == "assistant":
                phase = payload.get("phase")
                if phase is not None and not isinstance(phase, str):
                    stats["suspects"].append((ordinal, "an assistant message whose phase is not a string"))
                elif phase == "final_answer":
                    passthrough = payload.get("internal_chat_message_metadata_passthrough")
                    internal = passthrough.get("turn_id") if isinstance(passthrough, dict) else None
                    text = _strict_text(payload.get("content"), ("output_text",))
                    if not isinstance(internal, str) or not internal:
                        stats["suspects"].append((ordinal, "a final_answer without a turn id"))
                    elif text is None:
                        stats["suspects"].append((ordinal, "a final_answer with unsupported content"))
                    else:
                        events.append({"kind": "final", "text": text, "offset": offset, "ordinal": ordinal,
                                       "id": str(payload.get("id") or ""), "turn": internal})
        elif ptype in ("task_complete", "turn_aborted"):
            internal = payload.get("turn_id")
            if not isinstance(internal, str) or not internal:
                stats["suspects"].append((ordinal, f"a {ptype} event without a turn id"))
            elif ptype == "task_complete":
                completed.setdefault(internal, ordinal)
            else:
                aborted.add(internal)
    # A Codex final is eligible only when its own turn completed after it and was not aborted (a final alone may be partial).
    eligible = []
    for event in events:
        if event["kind"] != "final":
            eligible.append(event)
        elif event["turn"] not in aborted and completed.get(event["turn"], -1) > event["ordinal"]:
            eligible.append(event)
    return eligible, stats


def resolve(provider: str, session_id: str, run_id: str, turn_id: str, *, roots: dict[str, Path] | None = None, cursor: dict[str, Any] | None = None,
            clock: Callable[[], float] = time.monotonic) -> NativeResult:
    """The one eligible routing block for the exact owned primary session and accepted turn, or the specific reason there is none.

    Eligibility: primary final (never user/tool/commentary/child/partial output) AFTER the delivered prompt record that carries
    this turn id (independent proof of attribution), parsed by the ordinary strict protocol parser for exactly this run and
    turn, in a transcript whose own metadata proves it is the owned primary session, read completely after the prompt (a partial,
    malformed or oversized record there makes the result `uncertain`). Two eligible finals with different frames are a conflict.
    Nothing here is a settlement or delivery proof."""
    if provider not in SUPPORTED_PROVIDERS:
        return NativeResult(UNSUPPORTED, f"native result recovery is not supported for provider {provider!r}")
    root = (roots or default_roots()).get(provider)
    if root is None:
        return NativeResult(UNAVAILABLE, f"no native-session root is configured for {provider}")
    if not _valid_session(session_id):
        return NativeResult(UNAVAILABLE, "the owned session id is not a canonical identity")
    budget = _Budget(clock)
    provenance: dict[str, Any] = {"provider": provider}
    try:
        path = _path_from_cursor(provider, session_id, Path(root), cursor)
        problem = None
        if path is None:
            path, problem = locate_session_file(provider, session_id, Path(root), budget)
        if path is None:
            return NativeResult(UNAVAILABLE if "more than one" not in str(problem) else CONFLICT, str(problem))
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except OSError as error:
            return NativeResult(UNAVAILABLE, f"the native transcript could not be opened: {error.__class__.__name__}")
        try:
            if provider == "codex":
                bad = verify_codex_metadata(_first_line(fd, budget), session_id)
                if bad is not None:
                    return NativeResult(IDENTITY, bad, provenance=provenance)
                provenance["metadata_verified"] = "session_meta id/source"
            else:
                provenance["metadata_verified"] = "per-record sessionId/isSidechain"
            lines, meta, problem = _read_window(fd, cursor, provider, budget)
            if problem is not None:
                return NativeResult(UNAVAILABLE, problem, provenance={**provenance, **{k: v for k, v in meta.items() if k in ("size", "cursor_used")}})
            scan = scan_claude if provider == "claude" else scan_codex
            events, stats = scan(lines, session_id, turn_id, budget)
            budget.check()
            changed = _extent_problem(fd, path, meta)  # after the read AND the parse: the evidence returned is the evidence checked
        finally:
            os.close(fd)
    except _Exhausted as error:
        return NativeResult(UNCERTAIN, f"{error}; nothing was attributed or routed", provenance=provenance)
    provenance.update({"dev": meta.get("dev"), "ino": meta.get("ino"), "offset_start": meta.get("offset_start"), "offset_end": meta.get("offset_end"),
                       "scanned_bytes": meta.get("scanned_bytes"), "records": stats["records"], "cursor_used": meta.get("cursor_used"),
                       "partial_tail_bytes": meta.get("partial_tail_bytes")})
    if changed is not None:
        provenance["extent_changed"] = changed
        return NativeResult(UNCERTAIN, f"{changed}; nothing was attributed or routed, and a later retry on the settled file decides", provenance=provenance)
    if stats.get("identity"):
        return NativeResult(IDENTITY, f"{stats['identity']}; the transcript cannot be attributed to the owned primary session", provenance=provenance)
    if stats.get("exhausted"):
        return NativeResult(UNCERTAIN, "the bounded native scan ended (record limit) before the end of the window; result attribution is not established", provenance=provenance)
    prompts = [e for e in events if e["kind"] == "prompt"]
    if not prompts:
        return NativeResult(UNCERTAIN, "the delivered prompt for this turn was not found in the bounded native window; attribution is not established", provenance=provenance)
    first_prompt = prompts[0]
    # Anything unread, malformed, oversized, partial or of an unsupported result-bearing shape AFTER the prompt could hold a competing result.
    unknown = [why for ordinal, why in stats["suspects"] if ordinal > first_prompt["ordinal"]]
    tail = int(meta.get("partial_tail_bytes") or 0)
    if tail:
        unknown.append(f"an unterminated trailing record of {tail} bytes")
    if unknown:
        provenance["incomplete_evidence"] = unknown[:5]
        return NativeResult(UNCERTAIN, f"{len(unknown)} unreadable or incomplete record(s) follow the delivered prompt ({unknown[0]}); a competing result cannot be ruled out, so nothing was routed", provenance=provenance)
    finals = [e for e in events if e["kind"] == "final" and e["ordinal"] > first_prompt["ordinal"]]
    candidates: list[tuple[dict[str, Any], ProtocolBlock]] = []
    for final in finals:
        try:
            block = parse_protocol(final["text"], run_id, turn_id)
        except SupervisorError as error:
            return NativeResult(CONFLICT, f"a native final contains conflicting routing frames: {error}", provenance=provenance)
        if block is not None:
            candidates.append((final, block))
    if not candidates:
        return NativeResult(NONE, "no primary final after the delivered prompt contains a routing frame for this run and turn", provenance=provenance)
    distinct = {(b.version, b.stage, b.next_agent, b.handoff, b.gate, b.payload) for _f, b in candidates}
    if len(distinct) != 1:
        return NativeResult(CONFLICT, "competing native finals carry different routing frames for this turn; refusing to choose", provenance=provenance)
    final, block = candidates[-1]
    frame = json.dumps([block.version, block.stage, block.next_agent, block.handoff, block.gate, block.payload, block.run_id, block.turn_id], separators=(",", ":"))
    provenance.update({"record_offset": final["offset"], "record_id": final["id"], "message_sha256": hashlib.sha256(final["text"].encode("utf-8")).hexdigest(),
                       "frame_sha256": hashlib.sha256(frame.encode("utf-8")).hexdigest(), "eligible_finals": len(candidates)})
    return NativeResult(FOUND, "recovered the exact primary final routing result from the native transcript", block=block, provenance=provenance)
