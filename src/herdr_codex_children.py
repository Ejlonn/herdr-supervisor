"""Bounded, read-only verifier of a Codex parent's approved-helper lifetime history, from provider-native transcripts only.

It answers ONE question for the supervisor's delivery/routing fence: may input, a result read, a quota key or a route proceed for this exact Codex
parent session? It never writes, never resolves a payload-selected path, follows no symlink, adopts no "newest" file, uses no model and no service, and
does not import the frozen probe collector. It does not prevent a native spawn (it observes recorded actions afterwards).

One decision = one EVIDENCE SNAPSHOT: the parent's complete history is read once; its raw lines also feed the native routing-frame extraction of the SAME
decision (no second read, no second allowance); the child bodies are bound to the metadata seen at discovery; and before any clear verdict is returned
the complete evidence set (parent extent, every child extent, the discovery namespace) is revalidated. Any drift is `unknown`.

Evidence classes (retained mapping in the reviewed probe collector; shapes are the retained 0.163.0 ones):
  REAL      parent `response_item/function_call` (namespace `collaboration`, name `spawn_agent|wait_agent|list_agents`), `function_call_output`,
            `response_item/agent_message` mailbox FINAL_ANSWER, `event_msg/task_started|task_complete|turn_aborted`; child `session_meta`
            (`parent_thread_id`, `thread_source`, `agent_path`, `source.subagent.thread_spawn{depth, agent_role}`), `thread_settings_applied`,
            `task_started.turn_attribution{parent_turn_id, root_turn_id}`.
  SYNTHETIC the custom-role fields used by this repository's tests: they model the recorded shapes and are not a claim about any paid provider run.
Verdict kinds: transparent | complete | hold | unknown (missing/malformed/ambiguous/unsupported/stale/changed/budget-exhausted evidence: never zero) | violation | identity.

ONE shared budget covers discovery, every read and EVERY parse of a decision, native-frame extraction included: the installed native-result defaults
(3 s, 4000 directory entries, 40000 records, 1 MiB per line), with a guard-specific 64 MiB byte ceiling, are TOTALS, not per file or per phase. A history that does not fit is `unknown`."""
from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
import os
import re
import stat
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import herdr_native_results as nr
from herdr_core import SupervisorError
from herdr_protocol import parse_protocol

MAX_HISTORY_BYTES = 64 * 1024 * 1024  # total actual reads across one whole-history guard decision; ordinary result windows stay 8 MiB

APPROVED_ROLE = "repository_explorer"
APPROVED_MODEL = "gpt-6-luna"
APPROVED_EFFORT = "max"
APPROVED_PROFILES = {
    APPROVED_ROLE: (APPROVED_MODEL, APPROVED_EFFORT),
    "repository_reviewer": ("gpt-6.1-sol", "high"),
}
# These are the profiles of the optional shared helper definitions (agents/codex/*.toml); a spawn is approved only with exactly the listed role.
APPROVED_FORK = "none"
_ALLOWED_SPAWN_KEYS = frozenset({"agent_type", "task_name", "fork_turns", "message"})
_OBSERVE_ONLY = frozenset({"wait_agent", "list_agents"})  # collaboration tools that only observe; every other collaboration call is a prohibited interaction
_COLLAB_NAMES = frozenset({"spawn_agent", "wait_agent", "list_agents", "send_input", "resume_agent", "close_agent", "followup_task", "interrupt_agent"})
# Supported benign record types (observed on retained real rollouts). Anything else in a history is unsupported evidence: `unknown`, never silently dropped.
_BENIGN_RESPONSE = frozenset({"message", "reasoning", "compaction", "function_call_output", "custom_tool_call_output", "agent_message"})
_BENIGN_EVENT = frozenset({"item_completed", "token_count", "user_message", "agent_message", "error"})
_EMBEDDED_SPAWN = re.compile(r"(?:\b(?:tools|agents|collab\w*)\s*\.\s*spawn_agent\b)|(?:\bspawn_agent\s*\()")
_TASK = re.compile(r"[A-Za-z0-9_]{1,40}\Z")
_FINAL = re.compile(r"Message Type: FINAL_ANSWER\nTask name: /root\nSender: (/root/[A-Za-z0-9_]{1,40})\nPayload:\n", re.S)
_META_CHUNK = 8192      # discovery reads a candidate's first record in chunks this small: the unavoidable read-ahead past the newline stays tiny and is fully charged
_ROLLOUT = re.compile(r"rollout-(\d{4}-\d\d-\d\dT\d\d-\d\d-\d\d)-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl\Z")

TRANSPARENT, COMPLETE, HOLD, UNKNOWN, VIOLATION, IDENTITY = "transparent", "complete", "hold", "unknown", "violation", "identity"


@dataclasses.dataclass(frozen=True)
class Verdict:
    kind: str
    detail: str
    evidence: dict[str, Any] = dataclasses.field(default_factory=dict)
    native: Any = None      # nr.NativeResult extracted from the SAME snapshot when a route was requested (never from a second read)

    @property
    def clear(self) -> bool:
        return self.kind in (TRANSPARENT, COMPLETE)

    @property
    def spawned(self) -> bool:
        """True for a verified compliant child: from then on only the verified primary native frame is routing evidence (never a pane-only frame)."""
        return self.kind == COMPLETE


class _Unknown(Exception):
    """Evidence that cannot support a decision (missing, malformed, ambiguous, unsupported, changed while read)."""


class _Shared:
    """The single budget of one decision: entries/time from the installed native-result budget plus shared byte and record totals."""

    def __init__(self, clock: Callable[[], float]) -> None:
        self.walk = nr._Budget(clock)
        self.bytes_left = MAX_HISTORY_BYTES
        self.records_left = nr.MAX_RECORDS

    def spend_bytes(self, count: int) -> None:
        self.bytes_left -= count
        if self.bytes_left < 0:
            raise nr._Exhausted(f"the shared {MAX_HISTORY_BYTES}-byte read bound of this decision was exceeded")
        self.walk.check()

    def spend_records(self, count: int) -> None:
        self.records_left -= count
        if self.records_left < 0:
            raise nr._Exhausted(f"the shared {nr.MAX_RECORDS}-record bound of this decision was exceeded")
        self.walk.check()


Extent = tuple[int, int, int, int]


def _stamp(record: dict[str, Any]) -> dt.datetime | None:
    value = record.get("timestamp")
    if not isinstance(value, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _read_history(path: Path, shared: _Shared) -> tuple[bytes, list[tuple[int, dict[str, Any]]], list[tuple[int, bytes]], Extent]:
    """The COMPLETE file: opened without following links, one regular file, within the shared byte total, whole records only, extent re-checked after the
    read and the parse. Returns (first line, [(ordinal, record)], [(byte offset, raw line)], extent)."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as error:
        raise _Unknown(f"a transcript could not be opened ({error.__class__.__name__})") from error
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise _Unknown("a transcript is not a regular file")
        shared.spend_bytes(info.st_size)
        chunks: list[bytes] = []
        read_size = 0
        while read_size < info.st_size:
            shared.walk.check()
            chunk = os.read(fd, min(1 << 20, info.st_size - read_size))
            if not chunk:
                break
            chunks.append(chunk)
            read_size += len(chunk)
        data = b"".join(chunks)
        del chunks
        meta = {"dev": info.st_dev, "ino": info.st_ino, "size": info.st_size, "mtime_ns": info.st_mtime_ns, "short_read": len(data) < info.st_size}
        if data and not data.endswith(b"\n"):
            raise _Unknown("a transcript ends in an unterminated (partial) record")
        records: list[tuple[int, dict[str, Any]]] = []
        lines: list[tuple[int, bytes]] = []
        first = b""
        position = 0
        for ordinal, raw in enumerate(data.split(b"\n")[:-1]):
            if ordinal == 0:
                first = raw
            if len(raw) > nr.MAX_LINE_BYTES:
                raise _Unknown("a transcript holds an oversized record")
            shared.spend_records(1)
            try:
                record = json.loads(raw)
            except ValueError as error:
                raise _Unknown("a transcript holds a malformed record") from error
            if not isinstance(record, dict):
                raise _Unknown("a transcript holds a non-object record")
            records.append((ordinal, record))
            lines.append((position, raw))
            position += len(raw) + 1
        problem = nr._extent_problem(fd, path, meta)
        if problem is not None:
            raise _Unknown(problem)
    finally:
        os.close(fd)
    return first, records, lines, (meta["dev"], meta["ino"], meta["size"], meta["mtime_ns"])


def _typed(record: dict[str, Any]) -> tuple[str, str | None, dict[str, Any]]:
    """(kind, payload type, payload) for a supported record; malformed or unsupported evidence raises `_Unknown` (never coerced, never dropped)."""
    kind = record.get("type")
    if not isinstance(kind, str):
        raise _Unknown("a record has no string type")
    if kind in nr._CODEX_METADATA_TYPES:
        return kind, None, {}
    if kind not in ("response_item", "event_msg"):
        raise _Unknown(f"a record of unsupported type {kind[:40]!r}")
    payload = record.get("payload")
    if not isinstance(payload, dict):
        raise _Unknown(f"a {kind} record whose payload is not an object")
    ptype = payload.get("type")
    if not isinstance(ptype, str):
        raise _Unknown(f"a {kind} record whose payload has no string type")
    return kind, ptype, payload


def _static_tool_argument(text: str, start: int) -> tuple[Any, int]:
    """JSON values, plus top-level object keys written as ASCII identifiers.

    Values remain JSON, never JavaScript expressions. Reject ambiguous duplicate
    or prototype-sensitive keys even though no decoded object is executed here.
    """
    def reject_constant(value: str) -> Any:
        raise ValueError("non-JSON constant")
    def object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result or key in ("__proto__", "constructor", "prototype"):
                raise ValueError("duplicate or prototype-sensitive key")
            result[key] = value
        return result
    decoder = json.JSONDecoder(parse_constant=reject_constant, object_pairs_hook=object_pairs)
    if start >= len(text) or text[start] != "{":
        return decoder.raw_decode(text, start)
    position = start + 1
    pairs: list[tuple[str, Any]] = []
    key_pattern = re.compile(r"[A-Za-z_][A-Za-z_0-9]*", re.ASCII)
    def whitespace(index: int) -> int:
        while index < len(text) and text[index] in " \t\r\n":
            index += 1
        return index
    position = whitespace(position)
    if position < len(text) and text[position] == "}":
        return {}, position + 1
    for _ in range(4096):
        if position < len(text) and text[position] == '"':
            key, position = decoder.raw_decode(text, position)
        else:
            match = key_pattern.match(text, position)
            if match is None:
                raise ValueError("not a static property name")
            key = match.group()
            position += len(key)
        position = whitespace(position)
        if position >= len(text) or text[position] != ":":
            raise ValueError("missing property colon")
        value, position = decoder.raw_decode(text, whitespace(position + 1))
        pairs.append((key, value))
        position = whitespace(position)
        if position < len(text) and text[position] == "}":
            return object_pairs(pairs), position + 1
        if position >= len(text) or text[position] != ",":
            raise ValueError("missing property separator")
        position = whitespace(position + 1)
    raise ValueError("too many static object properties")


def _literal_patch_spans(text: str) -> list[tuple[int, int]] | None:
    """Recognize ONLY complete static exec wrappers, never general JavaScript.

    Patch and shell-tool arguments are data in this native JavaScript wrapper,
    not executed native agent expressions. Shell code may have other effects;
    this verifier is not shell admission or a sandbox. Complete native records
    and descendant discovery remain mandatory, regardless of these spans.
    Static literal arguments avoid interpolation, comments and computed properties.
    Any unsupported syntax invalidates ALL exemptions, including earlier ones.
    """
    prefix = re.compile(r"text\s*\(\s*await\s+tools\.([A-Za-z_][A-Za-z_0-9]*)\s*\(\s*", re.ASCII)
    suffix = re.compile(r"\s*\)\s*\)\s*;", re.ASCII)
    spans: list[tuple[int, int]] = []
    position = 0
    for _ in range(4096):
        while position < len(text) and text[position] in " \t\r\n":
            position += 1
        if position == len(text):
            return spans
        match = prefix.match(text, position)
        if match is None:
            return None
        start = match.end()
        try:
            value, end = _static_tool_argument(text, start)
        except (ValueError, RecursionError):
            return None
        # The ordinary wrappers contain static arguments; never recognize a second
        # expression, spread, template, comment or dynamic argument after one.
        tail = suffix.match(text, end)
        if tail is None:
            return None
        if match.group(1) == "apply_patch":
            if not isinstance(value, str):
                return None
            spans.append((start, end))
        elif match.group(1) == "exec_command":
            # Only the known literal shell-tool contract, never a native agent
            # dispatcher, dynamic expression or unknown future argument shape.
            keys = {"cmd", "workdir", "yield_time_ms", "max_output_tokens", "sandbox_permissions", "justification", "prefix_rule", "shell", "login", "tty"}
            if not isinstance(value, dict) or not isinstance(value.get("cmd"), str) or not set(value) <= keys:
                return None
            spans.append((start, end))
        position = tail.end()
    return spans if not text[position:].strip(" \t\r\n") else None


def _custom_call(payload: dict[str, Any]) -> None:
    """A custom tool call can embed an agent action; it is benign only when it is clearly not one. An unsupported collaboration/embedded spawn is `unknown`."""
    name, namespace, text = payload.get("name"), payload.get("namespace"), payload.get("input")
    if name is not None and not isinstance(name, str):
        raise _Unknown("a custom tool call whose name is not a string")
    if namespace is not None and not isinstance(namespace, str):
        raise _Unknown("a custom tool call whose namespace is not a string")
    if text is not None and not isinstance(text, str):
        raise _Unknown("a custom tool call whose input is not a string")
    if namespace == "collaboration" or name in _COLLAB_NAMES:
        raise _Unknown("a custom tool call names a collaboration action: an unsupported action shape that cannot prove zero lifetime spawns")
    if text and _EMBEDDED_SPAWN.search(text):
        spans = _literal_patch_spans(text) if name == "exec" and namespace in (None, "functions") else None
        index = 0
        for item in _EMBEDDED_SPAWN.finditer(text):
            while spans is not None and index < len(spans) and spans[index][1] < item.start():
                index += 1
            if spans is None or index >= len(spans) or not (spans[index][0] <= item.start() and item.end() <= spans[index][1]):
                raise _Unknown("a custom tool call embeds a spawn_agent call outside a verified static tool argument: an unsupported action shape that cannot prove zero lifetime spawns")


def _tool_declarations(payload: dict[str, Any]) -> None:
    """Codex 0.166 records the model's tool definitions, not invocations.

    Accept only the observed developer declaration envelope and explicit tool
    kinds. Unknown/malformed declarations cannot establish a complete history.
    Tool names in this record (including spawn_agent) are not executed calls.
    """
    if payload.get("role") != "developer" or not isinstance(payload.get("tools"), list):
        raise _Unknown("additional_tools is not a developer tool declaration list")
    pending = [(tool, 0) for tool in payload["tools"]]
    while pending:
        tool, depth = pending.pop()
        if depth > 16 or not isinstance(tool, dict):
            raise _Unknown("additional_tools holds malformed or over-nested definitions")
        kind = tool.get("type")
        if kind not in ("namespace", "function", "custom") or not isinstance(tool.get("name"), str) or not tool["name"]:
            raise _Unknown("additional_tools holds an unsupported tool definition")
        if kind == "namespace":
            children = tool.get("tools")
            if not isinstance(children, list):
                raise _Unknown("additional_tools namespace has no tool list")
            pending.extend((child, depth + 1) for child in children)


def _dig(value: Any, *keys: str) -> Any:
    """Nested object access with exact containers: a missing key is None, a present non-object container is malformed evidence."""
    for key in keys:
        if value is None:
            return None
        if not isinstance(value, dict):
            raise _Unknown("a nested evidence field has an unsupported container type")
        value = value.get(key)
    return value


def _opt_str(value: Any, what: str) -> str | None:
    if value is not None and not isinstance(value, str):
        raise _Unknown(f"{what} is not a string")
    return value


@dataclasses.dataclass
class _Parent:
    spawns: list[dict[str, Any]]
    prohibited: list[str]
    outputs: dict[str, Any]
    mailbox: list[dict[str, Any]]
    finals: list[dict[str, Any]]
    turns: dict[str, dict[str, Any]]
    extent: Extent
    path: Path
    lines: list[tuple[int, bytes]]


def _analyze_parent(session_id: str, path: Path, first: bytes, records: list[tuple[int, dict[str, Any]]], lines: list[tuple[int, bytes]], extent: Extent) -> _Parent:
    spawns: list[dict[str, Any]] = []      # primary identity was proven (and its parse charged) by `verify` before this analysis
    prohibited: list[str] = []
    outputs: dict[str, Any] = {}
    seen_calls: set[str] = set()
    mailbox: list[dict[str, Any]] = []
    finals: list[dict[str, Any]] = []
    turns: dict[str, dict[str, Any]] = {}
    current: str | None = None
    for ordinal, record in records:
        kind, ptype, payload = _typed(record)
        if kind == "event_msg":
            if ptype == "task_started":
                turn, stamp = payload.get("turn_id"), _stamp(record)
                root = _opt_str(payload.get("root_turn_id"), "a parent task_started root_turn_id")
                if not isinstance(turn, str) or not turn or stamp is None or turn in turns:
                    raise _Unknown("a parent task_started record has no usable turn id/timestamp or repeats a turn id")
                turns[turn] = {"start": ordinal, "start_ts": stamp, "end": None, "end_ts": None, "aborted": False, "root": root or turn}
                current = turn
            elif ptype in ("task_complete", "turn_aborted"):
                turn, stamp = payload.get("turn_id"), _stamp(record)
                if not isinstance(turn, str) or turn not in turns or stamp is None or turns[turn]["end"] is not None:
                    raise _Unknown("a parent turn end record has no matching open turn or timestamp")
                turns[turn].update(end=ordinal, end_ts=stamp, aborted=ptype == "turn_aborted")
            elif ptype not in _BENIGN_EVENT and ptype != "thread_settings_applied":
                raise _Unknown(f"an event of unsupported type {str(ptype)[:40]!r}")
            continue
        if kind != "response_item":
            continue
        if ptype == "function_call":
            name, namespace, call_id = payload.get("name"), payload.get("namespace"), payload.get("call_id")
            if not isinstance(name, str) or not name or not isinstance(call_id, str) or not call_id or call_id in seen_calls:
                raise _Unknown("a parent function call has no name/call id or repeats a call id (duplicate call id)")
            _opt_str(namespace, "a function call namespace")
            seen_calls.add(call_id)
            if name == "spawn_agent":
                if namespace != "collaboration":
                    raise _Unknown("a spawn_agent call outside the collaboration namespace is an unsupported shape")
                stamp = _stamp(record)
                if stamp is None:
                    raise _Unknown("a spawn_agent call has no usable timestamp")
                spawns.append({"call_id": call_id, "ordinal": ordinal, "ts": stamp, "arguments": payload.get("arguments"), "turn": current})
            elif namespace == "collaboration" and name not in _OBSERVE_ONLY:
                prohibited.append(name[:60])
            elif name in _COLLAB_NAMES and namespace not in (None, "collaboration"):
                raise _Unknown("a collaboration-named call in another namespace is an unsupported shape")
        elif ptype == "additional_tools":
            _tool_declarations(payload)
        elif ptype == "custom_tool_call":
            _custom_call(payload)
        elif ptype == "function_call_output":
            call_id = payload.get("call_id")
            if not isinstance(call_id, str) or not call_id or call_id in outputs:
                raise _Unknown("a function_call_output has no call id or repeats one")
            outputs[call_id] = (payload.get("output"), _stamp(record))
        elif ptype == "agent_message":
            text = nr._text_of(payload.get("content"), ("input_text", "output_text", "text"))
            match = _FINAL.match(text)
            if match is not None:
                stamp = _stamp(record)
                if stamp is None:
                    raise _Unknown("a parent FINAL_ANSWER mailbox record has no usable timestamp")
                mailbox.append({"author": payload.get("author"), "recipient": payload.get("recipient"), "sender": match.group(1), "ts": stamp, "ordinal": ordinal})
        elif ptype == "message":
            role = payload.get("role")
            if not isinstance(role, str):
                raise _Unknown("a message record without a string role")
            if role == "assistant" and payload.get("phase") == "final_answer":
                stamp = _stamp(record)
                turn_id = _dig(payload, "internal_chat_message_metadata_passthrough", "turn_id")
                if stamp is None:
                    raise _Unknown("a parent assistant final has no usable timestamp")
                finals.append({"ordinal": ordinal, "ts": stamp, "turn": turn_id if isinstance(turn_id, str) and turn_id else current})
        elif ptype not in _BENIGN_RESPONSE:
            raise _Unknown(f"a response item of unsupported type {str(ptype)[:40]!r}")
    return _Parent(spawns, prohibited, outputs, mailbox, finals, turns, extent, path, lines)


@dataclasses.dataclass
class _Child:
    path: Path
    meta: dict[str, Any]
    first: bytes
    extent: Extent
    body: dict[str, Any] | None = None


def _date_dir(entry: os.DirEntry[str], pattern: re.Pattern[str]) -> bool:
    """A real YYYY / MM / DD directory. A date-named entry that is a symlink cannot be followed and cannot be skipped (it could hide a child): unknown."""
    if pattern.match(entry.name) is None:
        return False
    if entry.is_symlink():
        raise _Unknown("a date-named directory entry is a symlink, so a child below it cannot be excluded")
    return nr._is_real_dir(entry)


def _listing(root: Path, floor: tuple[str, ...], shared: _Shared) -> list[tuple[str, int, int, int, int]]:
    """The candidate namespace: every rollout file whose directory date is not earlier than the parent's, as (relative path, dev, ino, size, mtime_ns). Entries count
    against the shared budget. Taken twice per decision (before the first child read and at the consumption boundary); a difference is relationship-set drift."""
    out: list[tuple[str, int, int, int, int]] = []
    root_real = Path(os.path.realpath(root))
    with os.scandir(root_real) as years:
        for year in years:
            shared.walk.tick()
            if not _date_dir(year, nr._YEAR) or year.name < floor[0]:
                continue
            with os.scandir(year.path) as months:
                for month in months:
                    shared.walk.tick()
                    if not _date_dir(month, nr._TWO) or (year.name, month.name) < floor[:2]:
                        continue
                    with os.scandir(month.path) as days:
                        for day in days:
                            shared.walk.tick()
                            if not _date_dir(day, nr._TWO) or (year.name, month.name, day.name) < floor:
                                continue
                            with os.scandir(day.path) as files:
                                for item in files:
                                    shared.walk.tick()
                                    if not item.name.startswith("rollout-") or not item.name.endswith(".jsonl"):
                                        continue
                                    if item.is_symlink() or not item.is_file(follow_symlinks=False) or _ROLLOUT.match(item.name) is None:
                                        raise _Unknown("a non-regular or oddly named rollout entry cannot be excluded as a child")
                                    info = item.stat(follow_symlinks=False)
                                    out.append((os.path.relpath(item.path, root_real), info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns))
    return sorted(out)


def _first_line_charged(fd: int, shared: _Shared) -> bytes | None:
    """The first record, read in small chunks: every byte returned by the OS (the newline and any read-ahead beyond it included) is charged to the shared byte total
    BEFORE it is used, and a read is never larger than the allowance that is left. A line longer than MAX_LINE_BYTES is unreadable (None)."""
    os.lseek(fd, 0, os.SEEK_SET)
    data = b""
    while True:
        shared.walk.check()
        if shared.bytes_left <= 0:
            raise nr._Exhausted(f"the shared {MAX_HISTORY_BYTES}-byte read bound of this decision was exceeded")
        chunk = os.read(fd, min(_META_CHUNK, shared.bytes_left))
        shared.spend_bytes(len(chunk))
        if not chunk:
            return data or None
        cut = chunk.find(b"\n")
        if cut >= 0:
            return (data + chunk[:cut]) if len(data) + cut <= nr.MAX_LINE_BYTES else None
        data += chunk
        if len(data) > nr.MAX_LINE_BYTES:
            return None


def _first_meta(path: Path, shared: _Shared) -> tuple[dict[str, Any], bytes, Extent]:
    """The first record of a candidate rollout (bounded, regular file, no link following, counted against the shared total) with its extent."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as error:
        raise _Unknown(f"a rollout could not be opened while looking for children ({error.__class__.__name__})") from error
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise _Unknown("a rollout is not a regular file")
        line = _first_line_charged(fd, shared)
    finally:
        os.close(fd)
    if line is None:
        raise _Unknown("a rollout has no readable first record, so it cannot be excluded as a child")
    shared.spend_records(1)      # the metadata parse is a record parse like any other
    try:
        record = json.loads(line)
    except ValueError as error:
        raise _Unknown("a rollout's first record is not valid JSON, so it cannot be excluded as a child") from error
    payload = record.get("payload") if isinstance(record, dict) else None
    if not isinstance(record, dict) or record.get("type") != "session_meta" or not isinstance(payload, dict):
        raise _Unknown("a rollout does not start with session_meta, so it cannot be excluded as a child")
    return payload, line, (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def _declared_parents(meta: dict[str, Any]) -> set[str]:
    out: set[str] = set()
    parent = meta.get("parent_thread_id")
    if parent is not None:
        if not isinstance(parent, str):
            raise _Unknown("a rollout's parent_thread_id is not a string")
        out.add(parent)
    source = meta.get("source")
    if isinstance(source, dict):
        sub = source.get("subagent")
        if sub is not None and not isinstance(sub, dict):
            raise _Unknown("a rollout's source.subagent is not an object")
        spawn = _dig(sub, "thread_spawn")
        if spawn is not None and not isinstance(spawn, dict):
            raise _Unknown("a rollout's thread_spawn is not an object")
        declared = _dig(spawn, "parent_thread_id")
        if declared is not None:
            if not isinstance(declared, str):
                raise _Unknown("a rollout's thread_spawn parent_thread_id is not a string")
            out.add(declared)
    elif source is not None and not isinstance(source, str):
        raise _Unknown("a rollout's source is neither a string nor an object")
    return out


def _permission_review(meta: dict[str, Any]) -> bool:
    """Observed native automatic permission-review thread, not delegated work.

    Require the whole structural conjunction, not just a label. Its metadata
    remains in discovery/extent checks and its descendants are still traversed.
    A real parent spawn record is never subtracted by this classification.
    """
    return (
        meta.get("thread_source") == "guardian_review"
        and meta.get("source") == {"subagent": {"other": "guardian"}}
        and isinstance(meta.get("parent_thread_id"), str)
        and nr._valid_session(meta["parent_thread_id"])
        and meta.get("session_id") == meta["parent_thread_id"]
        and meta.get("id") != meta["parent_thread_id"]
        and meta.get("agent_path") is None
        and meta.get("agent_role") is None
    )


def _discover_descendants(root: Path, parent_path: Path, session_id: str, shared: _Shared) -> tuple[list[_Child], list[tuple[str, int, int, int, int]], tuple[str, ...]]:
    """Every rollout that names the parent (or a found descendant) as parent, plus the namespace listing it was taken from. Only files whose directory date is
    not earlier than the parent's are candidates (a child is created after its parent); every candidate's first record is read, so an unreadable one is `unknown`."""
    root_real = Path(os.path.realpath(root))
    parent_rel = os.path.relpath(parent_path, root_real).split("/")
    if len(parent_rel) != 4 or _ROLLOUT.match(parent_rel[3]) is None:
        raise _Unknown("the parent transcript path is not the supported YYYY/MM/DD/rollout-… layout")
    floor = tuple(parent_rel[:3])
    listing = _listing(root, floor, shared)
    metas: dict[str, list[_Child]] = {}
    for rel, _dev, _ino, _size, _mtime in listing:
        path = root_real / rel
        if path == parent_path:
            continue
        meta, first, extent = _first_meta(path, shared)
        ident = meta.get("id")
        if not isinstance(ident, str) or not nr._valid_session(ident):
            raise _Unknown("a rollout's session_meta has no canonical id, so it cannot be excluded as a child")
        metas.setdefault(ident, []).append(_Child(path, meta, first, extent))
    found: dict[str, _Child] = {}
    frontier = {session_id}
    while frontier:
        nxt: set[str] = set()
        for ident, entries in metas.items():
            for entry in entries:
                if _declared_parents(entry.meta) & frontier:
                    if ident in found or len(entries) > 1:
                        raise _Unknown("a child native identity appears more than once (duplicate rollout/identity)")
                    found[ident] = entry
                    nxt.add(ident)
        frontier = nxt
    return [entry for entry in found.values() if not _permission_review(entry.meta)], listing, floor


def _child_facts(child: _Child) -> dict[str, Any]:
    meta = child.meta
    source = meta.get("source")
    spawn = _dig(source, "subagent", "thread_spawn") if isinstance(source, dict) else None
    spawn = spawn if isinstance(spawn, dict) else {}
    return {"id": meta.get("id"), "parent": meta.get("parent_thread_id"), "spawn_parent": spawn.get("parent_thread_id"), "thread_source": meta.get("thread_source"),
            "agent_path": meta.get("agent_path"), "depth": spawn.get("depth"), "role": spawn.get("agent_role")}


def _analyze_child(child: _Child, shared: _Shared) -> dict[str, Any]:
    """The child's FULL body, bound to the metadata seen at discovery: the same first record and an unchanged extent, or `unknown`."""
    first, records, _lines, extent = _read_history(child.path, shared)
    if first != child.first or extent != child.extent:
        raise _Unknown("a child rollout changed between its discovery and its full read")
    settings: set[tuple[Any, Any]] = set()
    turns: dict[str, dict[str, Any]] = {}
    prohibited: list[str] = []
    adverse: list[str] = []
    for ordinal, record in records:
        kind, ptype, payload = _typed(record)
        if kind == "event_msg":
            if ptype == "thread_settings_applied":
                applied = payload.get("thread_settings")
                if not isinstance(applied, dict):
                    raise _Unknown("a child thread_settings_applied record has no settings object")
                model = applied.get("model")
                effort = _dig(applied, "collaboration_mode", "settings", "reasoning_effort")
                if not isinstance(model, str) or not isinstance(effort, str):
                    raise _Unknown("a child's applied model/effort is not a pair of strings")
                settings.add((model, effort))
            elif ptype == "task_started":
                turn, stamp = payload.get("turn_id"), _stamp(record)
                if not isinstance(turn, str) or not turn or stamp is None or turn in turns:
                    raise _Unknown("a child task_started record has no usable turn id/timestamp or repeats a turn id")
                attribution = payload.get("turn_attribution")
                if attribution is not None and not isinstance(attribution, dict):
                    raise _Unknown("a child turn_attribution is not an object")
                turns[turn] = {"start": ordinal, "start_ts": stamp, "complete": None, "aborted": False, "final": None, "end_ts": None,
                               "root": _opt_str(payload.get("root_turn_id"), "a child root_turn_id"),
                               "attr_parent": _opt_str((attribution or {}).get("parent_turn_id"), "a child attribution parent_turn_id"),
                               "attr_root": _opt_str((attribution or {}).get("root_turn_id"), "a child attribution root_turn_id")}
            elif ptype in ("task_complete", "turn_aborted"):
                turn, stamp = payload.get("turn_id"), _stamp(record)
                if not isinstance(turn, str) or turn not in turns or stamp is None or turns[turn]["end_ts"] is not None:
                    raise _Unknown("a child turn end record has no matching open turn or timestamp")
                final = payload.get("last_agent_message") if ptype == "task_complete" else None
                if final is not None and not isinstance(final, str):
                    raise _Unknown("a child task_complete final message is not a string")
                turns[turn].update(end_ts=stamp, complete=ordinal if ptype == "task_complete" else None, aborted=ptype == "turn_aborted", final=final)
            elif ptype == "error":
                adverse.append("an error event")
            elif ptype not in _BENIGN_EVENT:
                raise _Unknown(f"a child event of unsupported type {str(ptype)[:40]!r}")
        elif kind == "response_item":
            if ptype == "function_call":
                name, namespace = payload.get("name"), payload.get("namespace")
                if not isinstance(name, str):
                    raise _Unknown("a child function call has no string name")
                _opt_str(namespace, "a child function call namespace")
                if namespace == "collaboration" or name in _COLLAB_NAMES:
                    prohibited.append(name[:60])            # a child that spawns or interacts with agents: descendants/prohibited
            elif ptype == "additional_tools":
                _tool_declarations(payload)
            elif ptype == "custom_tool_call":
                _custom_call(payload)
            elif ptype == "message":
                if not isinstance(payload.get("role"), str):
                    raise _Unknown("a child message record without a string role")
            elif ptype not in _BENIGN_RESPONSE:
                raise _Unknown(f"a child response item of unsupported type {str(ptype)[:40]!r}")
    return {"settings": settings, "turns": turns, "prohibited": prohibited, "adverse": adverse, "extent": extent}


def _exact_success(output: Any, task: str) -> bool:
    try:
        value = json.loads(output) if isinstance(output, str) else None
    except ValueError:
        return False
    return isinstance(value, dict) and set(value) == {"task_name"} and value["task_name"] == "/root/" + task


def _decide(session_id: str, parent: _Parent, descendants: list[_Child], shared: _Shared, read_extents: dict[Path, Extent]) -> Verdict:
    ev: dict[str, Any] = {"parent": session_id, "spawn_calls": len(parent.spawns), "descendants": len(descendants), "prohibited": parent.prohibited[:5]}
    if parent.prohibited:
        return Verdict(VIOLATION, f"prohibited collaboration interaction(s) in the parent history: {sorted(set(parent.prohibited))}", ev)
    if len(parent.spawns) > 1:
        return Verdict(VIOLATION, f"{len(parent.spawns)} spawn calls in this parent's lifetime (at most one is allowed, failed or pending calls count)", ev)
    if not parent.spawns:
        if descendants:
            return Verdict(VIOLATION, f"{len(descendants)} child rollout(s) exist without a recorded spawn call in the parent history", ev)
        return Verdict(TRANSPARENT, "no spawn call and no delegated task child in the complete parent history", ev)
    spawn = parent.spawns[0]
    try:
        args = json.loads(spawn["arguments"]) if isinstance(spawn["arguments"], str) else None
    except ValueError:
        args = None
    if not isinstance(args, dict) or not isinstance(args.get("task_name"), str) or _TASK.match(args["task_name"]) is None:
        return Verdict(UNKNOWN, "the spawn call's arguments are not a supported shape (object with a valid task_name)", ev)
    task = args["task_name"]
    ev["task"] = task
    requested_role = args.get("agent_type")
    if not isinstance(requested_role, str) or requested_role not in APPROVED_PROFILES:
        return Verdict(VIOLATION, f"the spawn names unapproved role {requested_role!r} (model-only and other roles are never grandfathered)", ev)
    expected_profile = APPROVED_PROFILES[requested_role]
    ev["role"] = requested_role
    ev["expected_model"], ev["expected_effort"] = expected_profile
    if args.get("fork_turns") != APPROVED_FORK or not set(args) <= _ALLOWED_SPAWN_KEYS:
        return Verdict(VIOLATION, "the spawn arguments are not the approved shape (fork_turns must be exactly 'none'; no other options)", ev)
    turn_id = spawn["turn"]
    turn = parent.turns.get(turn_id) if turn_id else None
    if turn is None:
        return Verdict(UNKNOWN, "the spawn call is not inside a recorded parent turn", ev)
    finals = [f for f in parent.finals if f["turn"] == turn_id]
    if spawn["call_id"] not in parent.outputs:
        if finals:
            return Verdict(VIOLATION, "the parent produced its final answer before the spawn result was recorded (early parent final)", ev)
        return Verdict(HOLD, "the spawn call has no recorded output yet (pending)", ev) if turn["end"] is None else Verdict(UNKNOWN, "the parent turn ended with the spawn output missing", ev)
    output, _output_ts = parent.outputs[spawn["call_id"]]
    if not _exact_success(output, task):
        return Verdict(UNKNOWN, "the spawn output is not exactly {\"task_name\": \"/root/<task>\"} (a refused or unsupported spawn result); the lifetime spawn is used", ev)
    if len({d.meta.get("id") for d in descendants}) != len(descendants):
        return Verdict(UNKNOWN, "duplicate child identity", ev)
    direct = [d for d in descendants if _child_facts(d)["parent"] == session_id]
    if len(descendants) > 1 or len(direct) != len(descendants):
        return Verdict(VIOLATION, f"{len(descendants)} descendant rollouts ({len(direct)} direct): exactly one depth-1 child is allowed, no grandchildren, second child or other role", ev)
    if not descendants:
        if finals:
            return Verdict(VIOLATION, "the parent produced its final answer with no child rollout (early parent final)", ev)
        return Verdict(HOLD, "the spawn succeeded but its child rollout does not exist yet", ev) if turn["end"] is None else Verdict(UNKNOWN, "the spawn succeeded but no child rollout exists (missing child evidence)", ev)
    child = descendants[0]
    facts = _child_facts(child)
    ev["child"] = facts["id"]
    if facts["thread_source"] != "subagent" or type(facts["depth"]) is not int or facts["depth"] != 1 or facts["spawn_parent"] not in (None, session_id) or facts["agent_path"] != "/root/" + task:
        return Verdict(VIOLATION, "the child's identity does not match the spawned task (thread_source/depth/parent/agent_path)", ev)
    if facts["role"] is None:
        return Verdict(UNKNOWN, "the child rollout records no agent_role: custom-role evidence is missing", ev)
    if facts["role"] != requested_role:
        return Verdict(VIOLATION, f"the child records role {facts['role']!r}, not the exact requested role {requested_role!r}", ev)
    body = _analyze_child(child, shared)
    child.body = body
    read_extents[child.path] = body["extent"]
    if body["prohibited"]:
        return Verdict(VIOLATION, f"the child performed prohibited agent interaction(s): {sorted(set(body['prohibited']))}", ev)
    if body["adverse"]:
        return Verdict(UNKNOWN, f"the child history holds {body['adverse'][0]}", ev)
    if len(body["settings"]) != 1:
        return Verdict(UNKNOWN, "the child's applied model/effort is missing or inconsistent", ev)
    model, effort = next(iter(body["settings"]))
    if (model, effort) != expected_profile:
        return Verdict(VIOLATION, f"the child ran model/effort {(model, effort)!r}, not the role's approved {expected_profile!r}", ev)
    turns = body["turns"]
    if len(turns) > 1:
        return Verdict(VIOLATION, f"the child has {len(turns)} turns (a follow-up or second task is prohibited)", ev)
    if not turns:
        return Verdict(VIOLATION, "the parent produced its final answer while the child had no started turn (early parent final)", ev) if finals else Verdict(HOLD, "the child rollout has no started turn yet", ev)
    ((_cturn_id, cturn),) = turns.items()
    root = turn["root"]
    for label, value, expected in (("root_turn_id", cturn["root"], root), ("turn_attribution.root_turn_id", cturn["attr_root"], root), ("turn_attribution.parent_turn_id", cturn["attr_parent"], turn_id)):
        if value is not None and value != expected:
            return Verdict(VIOLATION, f"the child's recorded {label} {value!r} contradicts the invoking parent turn ({expected!r})", ev)
    if cturn["aborted"]:
        return Verdict(VIOLATION, "the child's turn was aborted (interrupted): not a successful terminal", ev)
    if cturn["complete"] is None:
        if finals:
            return Verdict(VIOLATION, "the parent produced its final answer before the child's terminal (early parent final)", ev)
        return Verdict(HOLD, "the child's turn has not completed (pending)", ev)
    if not isinstance(cturn["final"], str) or not cturn["final"].strip():
        return Verdict(VIOLATION, "the child completed without a non-empty final message", ev)
    if cturn["end_ts"] < cturn["start_ts"]:
        return Verdict(UNKNOWN, "the child's completion precedes its start", ev)
    mail = [m for m in parent.mailbox if m["sender"] == facts["agent_path"] and m["author"] == facts["agent_path"] and m["recipient"] == "/root"]
    others = [m for m in parent.mailbox if m not in mail]
    if others:
        return Verdict(VIOLATION, "the parent holds a FINAL_ANSWER mailbox record from another sender", ev)
    if len(mail) > 1:
        return Verdict(VIOLATION, "the parent holds more than one FINAL_ANSWER for the child (duplicate)", ev)
    if not mail:
        if finals or turn["end"] is not None:
            return Verdict(VIOLATION, "the parent turn that invoked the child ended or finalized before the child's FINAL_ANSWER reached the parent (early parent final)", ev)
        return Verdict(HOLD, "the child completed but its FINAL_ANSWER has not reached the parent yet", ev)
    if mail[0]["ts"] < cturn["end_ts"]:
        return Verdict(VIOLATION, "the parent's FINAL_ANSWER record precedes the child's task_complete", ev)
    boundary = max(cturn["end_ts"], mail[0]["ts"])
    for final in finals:        # the parent's own assistant final is judged on its OWN event, not on the later turn end
        if final["ordinal"] < mail[0]["ordinal"] or final["ts"] < boundary:
            return Verdict(VIOLATION, "the parent produced its assistant final before the verification boundary (early parent final), even though its turn ended later", ev)
    if turn["end"] is not None and turn["end_ts"] < boundary:
        return Verdict(VIOLATION, "the parent turn that invoked the child ended before the verification boundary (early parent final)", ev)
    if turn["end"] is None:
        return Verdict(HOLD, "the child is verified but the parent turn that invoked it has not ended", ev)
    if turn["aborted"]:
        return Verdict(VIOLATION, "the parent turn that invoked the child was aborted", ev)
    ev["boundary"] = boundary.isoformat()
    return Verdict(COMPLETE, "one compliant helper child: exact binding, approved role/model/effort/fork, successful terminal and ordered parent FINAL_ANSWER", ev)


def _native_result(parent: _Parent, session_id: str, run_id: str, turn_id: str, shared: _Shared) -> Any:
    """The eligible primary native routing frame for this run/turn, extracted from the SAME raw lines the verification already read (no second read, no fresh
    allowance: the parse is charged to the decision's shared record total). Same eligibility rules as `herdr_native_results.resolve`."""
    shared.spend_records(len(parent.lines))
    events, stats = nr.scan_codex(parent.lines, session_id, turn_id, shared.walk)
    prov: dict[str, Any] = {"provider": "codex", "metadata_verified": "session_meta id/source (single decision snapshot)", "dev": parent.extent[0], "ino": parent.extent[1],
                            "offset_start": 0, "offset_end": parent.extent[2], "scanned_bytes": parent.extent[2], "records": stats["records"], "cursor_used": False, "partial_tail_bytes": 0}
    if stats.get("exhausted"):
        return nr.NativeResult(nr.UNCERTAIN, "the bounded native scan ended (record limit) before the end of the window; result attribution is not established", provenance=prov)
    prompts = [e for e in events if e["kind"] == "prompt"]
    if not prompts:
        return nr.NativeResult(nr.UNCERTAIN, "the delivered prompt for this turn was not found in the parent history; attribution is not established", provenance=prov)
    first_prompt = prompts[0]
    unknown = [why for ordinal, why in stats["suspects"] if ordinal > first_prompt["ordinal"]]
    if unknown:
        prov["incomplete_evidence"] = unknown[:5]
        return nr.NativeResult(nr.UNCERTAIN, f"{len(unknown)} unreadable or incomplete record(s) follow the delivered prompt ({unknown[0]}); a competing result cannot be ruled out", provenance=prov)
    candidates = []
    for final in [e for e in events if e["kind"] == "final" and e["ordinal"] > first_prompt["ordinal"]]:
        try:
            block = parse_protocol(final["text"], run_id, turn_id)
        except SupervisorError as error:
            return nr.NativeResult(nr.CONFLICT, f"a native final contains conflicting routing frames: {error}", provenance=prov)
        if block is not None:
            candidates.append((final, block))
    if not candidates:
        return nr.NativeResult(nr.NONE, "no primary final after the delivered prompt contains a routing frame for this run and turn", provenance=prov)
    if len({(b.version, b.stage, b.next_agent, b.handoff, b.gate, b.payload) for _f, b in candidates}) != 1:
        return nr.NativeResult(nr.CONFLICT, "competing native finals carry different routing frames for this turn; refusing to choose", provenance=prov)
    final, block = candidates[-1]
    frame = json.dumps([block.version, block.stage, block.next_agent, block.handoff, block.gate, block.payload, block.run_id, block.turn_id], separators=(",", ":"))
    prov.update({"record_offset": final["offset"], "record_id": final["id"], "message_sha256": hashlib.sha256(final["text"].encode("utf-8")).hexdigest(),
                 "frame_sha256": hashlib.sha256(frame.encode("utf-8")).hexdigest(), "eligible_finals": len(candidates)})
    return nr.NativeResult(nr.FOUND, "recovered the exact primary final routing result from the verified decision snapshot", block=block, provenance=prov)


def verify(session_id: str, root: Path | None, *, clock: Callable[[], float] = time.monotonic, route: tuple[str, str] | None = None, native_always: bool = False) -> Verdict:
    """One decision. Never raises for evidence problems: they are `unknown`. Reads only beneath `root`; identity is the exact owned session id.
    `route=(run_id, turn_id)` also extracts the owned primary's eligible native routing frame from the same snapshot (always after a verified spawn;
    with `native_always` also for a transparent history). The result carries it as `.native`."""
    if root is None:
        return Verdict(UNKNOWN, "no Codex native-session root is configured")
    if not nr._valid_session(session_id):
        return Verdict(IDENTITY, "the owned session id is not a canonical identity")
    shared = _Shared(clock)
    try:
        path, problem = nr.locate_session_file("codex", session_id, Path(root), shared.walk)
        if path is None:
            return Verdict(UNKNOWN, str(problem))
        first, records, lines, extent = _read_history(path, shared)
        shared.spend_records(1)      # the primary-metadata check parses the first record once more: charged like every other record parse
        bad = nr.verify_codex_metadata(first, session_id)
        if bad is not None:
            return Verdict(IDENTITY, bad)
        parent = _analyze_parent(session_id, path, first, records, lines, extent)
        descendants, listing, floor = _discover_descendants(Path(root), path, session_id, shared)
        read_extents: dict[Path, Extent] = {path: extent}
        verdict = _decide(session_id, parent, descendants, shared, read_extents)
        if verdict.clear:
            native = None
            if route is not None and (verdict.spawned or native_always):
                native = _native_result(parent, session_id, route[0], route[1], shared)
            # consumption boundary: the complete evidence set must still be exactly what was read. Growth/replacement/rewrite of the parent or a child, or a different
            # relationship namespace, invalidates the decision (nothing is cached; the next consumer decides again).
            again = _listing(Path(root), floor, shared)
            if again != listing:
                raise _Unknown("the rollout namespace changed while the decision was made (relationship-set drift)")
            root_real = Path(os.path.realpath(root))
            current = {rel: (dev, ino, size, mtime) for rel, dev, ino, size, mtime in again}
            for evidence_path, seen in read_extents.items():
                if current.get(os.path.relpath(evidence_path, root_real)) != seen:
                    raise _Unknown("an evidence file changed after it was read (extent drift)")
            verdict = dataclasses.replace(verdict, native=native)
        return verdict
    except nr._Exhausted as error:
        return Verdict(UNKNOWN, f"{error}; the history cannot be fully verified, and a tail or a cursor is never read as the whole")
    except _Unknown as error:
        return Verdict(UNKNOWN, str(error))
    except OSError as error:
        return Verdict(UNKNOWN, f"native-session storage is not accessible: {error.__class__.__name__}")
    except (TypeError, ValueError, KeyError, AttributeError, IndexError) as error:     # safety net ONLY: unsupported shapes are classified unknown, never an unhandled error
        return Verdict(UNKNOWN, f"unsupported or malformed evidence shape ({error.__class__.__name__}); nothing was attributed")
