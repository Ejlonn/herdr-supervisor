"""The routing protocol: eight-key frame reconstruction across terminal wrapping, duplicate/conflict rules, and run/turn-bound parsing; plus the separate follow-up response frame that the workflow router never accepts."""

from __future__ import annotations

import dataclasses
import re

from herdr_core import (
    _LINE_PREFIX_RE,
    _STAGE_RE,
    _UUID_RE,
    FOLLOWUP_KEYS,
    GATE_TYPES,
    MAX_FOLLOWUP_SUMMARY_CHARS,
    PROTOCOL_KEYS,
    PROTOCOL_KEYS_V2,
    ROUTES,
    SupervisorError,
)


@dataclasses.dataclass(frozen=True)
class ProtocolBlock:
    run_id: str
    turn_id: str
    stage: str
    next_agent: str
    handoff: str
    version: int = 1
    gate: str = "none"
    payload: str = "-"


@dataclasses.dataclass(frozen=True)
class FollowupFrame:
    """The follow-up response frame: bound to a run, a follow-up turn, the suspended decision, and the
    exact response file (path + SHA-256). It carries no route, stage, or gate and is never a handoff."""

    run_id: str
    turn_id: str
    decision_id: str
    response_path: str
    response_sha256: str
    summary: str

def _normalize_line(line: str) -> str:
    return _LINE_PREFIX_RE.sub("", line.rstrip())

# A protocol value may be split over several physical rows by the terminal (hard wrap). Continuation
# rows are accepted only inside an expected ordered eight-key frame, bounded by the validated logical
# size of each value (not by a row count), and end at the next expected key (machine fields) or at a
# blank/decoration/HERDR_ row (the handoff, the last field).
_DECORATION_ROW_RE = re.compile(r"^[^A-Za-z0-9]*$")

_MACHINE_KEYS = frozenset({"HERDR_PROTOCOL", "HERDR_RUN", "HERDR_TURN", "HERDR_STAGE", "HERDR_NEXT", "HERDR_GATE", "HERDR_PAYLOAD"})

# Maximum accepted logical length per value (the parser's own limits); reconstruction stops as soon as
# the accumulated fragments exceed it, so no frame can grow past the validated bounds.
_FIELD_MAX_CHARS = {"HERDR_PROTOCOL": 1, "HERDR_RUN": 36, "HERDR_TURN": 36, "HERDR_STAGE": 80, "HERDR_NEXT": 6, "HERDR_GATE": 18, "HERDR_PAYLOAD": 1024, "HERDR_HANDOFF": 600}

# Follow-up frame: every field but the summary is machine text; the summary follows the handoff contract.
_FOLLOWUP_MACHINE_KEYS = frozenset({"HERDR_FOLLOWUP", "HERDR_RUN", "HERDR_FOLLOWUP_TURN", "HERDR_DECISION", "HERDR_RESPONSE", "HERDR_RESPONSE_SHA256"})

_FOLLOWUP_FIELD_MAX_CHARS = {"HERDR_FOLLOWUP": 1, "HERDR_RUN": 36, "HERDR_FOLLOWUP_TURN": 36, "HERDR_DECISION": 36, "HERDR_RESPONSE": 1024, "HERDR_RESPONSE_SHA256": 64, "HERDR_SUMMARY": 600}

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

_MAX_FRAME_ROWS = 160  # eight logical lines at 40 columns with maximum-length values stay well below this

_MAX_LEGACY_HANDOFF_ROWS = 6  # a spaced (pre-contract) handoff cannot be told from following prose; keep it short

def _frame_prefix(raw_line: str) -> str:
    """Decoration the terminal put in front of the frame's head row (gutter, indent); continuation rows
    carry the same prefix, which is stripped exactly so a leading '-' or '*' inside a value survives."""
    match = _LINE_PREFIX_RE.match(raw_line)
    return match.group(0) if match else ""

def _continuation_fragment(raw_line: str, prefix: str) -> str:
    rest = raw_line[len(prefix):] if prefix and raw_line.startswith(prefix) else raw_line.lstrip()
    return rest.rstrip()

def _join_wrapped_text(fragments: list[str], rows: list[str], width: int) -> str:
    """Re-join a hard-wrapped handoff. The emitted contract joins words with underscores and forbids
    spaces, so a compliant value contains no whitespace and a terminal can only split it on full rows:
    concatenation reproduces it exactly. A legacy value that already contains spaces is re-joined
    best-effort: a terminal drops the space at a word-boundary break, so one space is restored unless
    the previous row was full (a mid-token split of an over-long token)."""
    head = fragments[0].strip()
    legacy_spaced = " " in head
    text = head
    for previous_row, fragment in zip(rows, fragments[1:]):
        piece = fragment.strip()
        if not piece:
            continue
        if not legacy_spaced:
            text += piece
            continue
        glued = len(previous_row.rstrip()) >= width
        text = text + piece if glued else (text + " " + piece if text else piece)
    return text

def _reconstruct_frame(raw_lines: list[str], start: int, keys: tuple[str, ...], *, machine_keys: frozenset[str], max_chars: dict[str, int]) -> dict[str, str] | None:
    """Reconstruct one ordered key frame starting at `start` from physical rows into logical values. The
    frame's key order, the per-field logical bounds, and the wrap rules are the only inputs; the caller
    validates every identity, enum, placeholder, and length rule on the returned values."""
    prefix = _frame_prefix(raw_lines[start])
    rows_by_key: dict[str, list[str]] = {}  # physical rows (prefix stripped) per key, head row included
    index = start
    for position, key in enumerate(keys):
        if index >= len(raw_lines) or index - start >= _MAX_FRAME_ROWS:
            return None
        line = _normalize_line(raw_lines[index])
        found_key, separator, value = line.partition("=")
        if not separator or found_key.strip() != key:
            return None  # missing, reordered, or foreign key: not a frame
        rows = [_continuation_fragment(raw_lines[index], prefix)]
        accumulated = len(value.strip())
        index += 1
        next_key = keys[position + 1] if position + 1 < len(keys) else None
        compliant_handoff = next_key is None and " " not in value.strip()
        while index < len(raw_lines) and index - start < _MAX_FRAME_ROWS:
            normalized = _normalize_line(raw_lines[index])
            if next_key is not None and normalized.startswith(next_key + "="):
                break
            terminator = not normalized.strip() or normalized.startswith("HERDR_") or _DECORATION_ROW_RE.match(normalized) is not None
            if terminator:
                if next_key is None:
                    break  # the last field ends at the first blank, decoration, or HERDR_ row
                return None  # a machine field interrupted before its successor key: broken frame
            fragment = _continuation_fragment(raw_lines[index], prefix)
            if compliant_handoff and " " in fragment.strip():
                break  # a contract handoff has no whitespace: a row with spaces is text after the block
            if next_key is None and not compliant_handoff and len(rows) >= _MAX_LEGACY_HANDOFF_ROWS:
                return None
            accumulated += len(fragment.strip())
            if accumulated > max_chars[key]:
                return None  # more continuation text than any valid value can hold: not this frame
            rows.append(fragment)
            index += 1
        rows_by_key[key] = rows
    width = max(len(row) for rows in rows_by_key.values() for row in rows)  # the terminal's row width as observed in this frame
    values: dict[str, str] = {}
    for key, rows in rows_by_key.items():
        head_value = rows[0].partition("=")[2]
        if key in machine_keys:
            values[key] = "".join(fragment.strip() for fragment in (head_value, *rows[1:]))  # wrapped machine text carries no whitespace
        else:
            values[key] = _join_wrapped_text([head_value, *rows[1:]], rows, width)
    return values

def _parse_block(raw_lines: list[str], start: int) -> ProtocolBlock | None:
    """Parse one routing frame starting at `start`. Physical rows are reconstructed into logical values;
    every identity, enum, placeholder, payload, and length rule then applies to the reconstructed values."""
    head = _normalize_line(raw_lines[start])
    keys: tuple[str, ...]
    if head.startswith("HERDR_PROTOCOL=1"):
        keys, version = PROTOCOL_KEYS, 1
    elif head.startswith("HERDR_PROTOCOL=2"):
        keys, version = PROTOCOL_KEYS_V2, 2
    else:
        return None
    values = _reconstruct_frame(raw_lines, start, keys, machine_keys=_MACHINE_KEYS, max_chars=_FIELD_MAX_CHARS)
    if values is None:
        return None
    if values["HERDR_PROTOCOL"] != str(version):
        return None
    run_id, turn_id = values["HERDR_RUN"], values["HERDR_TURN"]
    if not _UUID_RE.match(run_id) or not _UUID_RE.match(turn_id):
        return None
    stage, next_agent, handoff = values["HERDR_STAGE"], values["HERDR_NEXT"], values["HERDR_HANDOFF"]
    if not _STAGE_RE.match(stage) or next_agent not in ROUTES:
        return None
    # Placeholders in the echoed prompt template contain angle brackets; never accept them.
    if not handoff or "<" in handoff or ">" in handoff or len(handoff) > 600:
        return None
    if version == 1:
        return ProtocolBlock(run_id, turn_id, stage, next_agent, handoff)
    gate, payload = values["HERDR_GATE"], values["HERDR_PAYLOAD"]
    if gate not in ("none", *GATE_TYPES):
        return None
    if payload != "-" and (not payload.startswith("/") or "<" in payload or ">" in payload or len(payload) > 1024):
        return None
    return ProtocolBlock(run_id, turn_id, stage, next_agent, handoff, version=2, gate=gate, payload=payload)

def _parse_followup_frame(raw_lines: list[str], start: int) -> FollowupFrame | None:
    """Parse one follow-up response frame. A routing block never starts with HERDR_FOLLOWUP, and a
    follow-up frame never contains HERDR_PROTOCOL/HERDR_NEXT/HERDR_GATE, so neither parser can accept the
    other's frame. Placeholders (angle brackets) from the echoed prompt are rejected in every field."""
    if not _normalize_line(raw_lines[start]).startswith("HERDR_FOLLOWUP=1"):
        return None
    values = _reconstruct_frame(raw_lines, start, FOLLOWUP_KEYS, machine_keys=_FOLLOWUP_MACHINE_KEYS, max_chars=_FOLLOWUP_FIELD_MAX_CHARS)
    if values is None or values["HERDR_FOLLOWUP"] != "1":
        return None
    if any("<" in value or ">" in value for value in values.values()):
        return None
    run_id, turn_id, decision = values["HERDR_RUN"], values["HERDR_FOLLOWUP_TURN"], values["HERDR_DECISION"]
    if not _UUID_RE.match(run_id) or not _UUID_RE.match(turn_id) or not _UUID_RE.match(decision):
        return None
    path, digest, summary = values["HERDR_RESPONSE"], values["HERDR_RESPONSE_SHA256"], values["HERDR_SUMMARY"]
    if not path.startswith("/") or len(path) > 1024 or "\x00" in path or not _SHA256_RE.match(digest):
        return None
    if not summary or len(summary) > MAX_FOLLOWUP_SUMMARY_CHARS:
        return None
    return FollowupFrame(run_id, turn_id, decision, path, digest, summary)

def find_followup_frames(text: str) -> list[FollowupFrame]:
    raw_lines = text.splitlines()
    frames: list[FollowupFrame] = []
    for index, raw in enumerate(raw_lines):
        if _normalize_line(raw).startswith("HERDR_FOLLOWUP="):
            frame = _parse_followup_frame(raw_lines, index)
            if frame is not None:
                frames.append(frame)
    return frames

def parse_followup(text: str, run_id: str, turn_id: str) -> FollowupFrame | None:
    """Return the follow-up frame for the current run + follow-up turn, or None. Frames for other runs or
    turns are ignored; conflicting frames for this turn are an error (never routed, never registered)."""
    matching = [frame for frame in find_followup_frames(text) if frame.run_id == run_id and frame.turn_id == turn_id]
    if not matching:
        return None
    if len({(f.decision_id, f.response_path, f.response_sha256, f.summary) for f in matching}) != 1:
        raise SupervisorError("conflicting follow-up frames match the current follow-up turn; refusing to accept a response")
    return matching[-1]

def find_protocol_blocks(text: str) -> list[ProtocolBlock]:
    raw_lines = text.splitlines()
    blocks: list[ProtocolBlock] = []
    for index, raw in enumerate(raw_lines):
        if _normalize_line(raw).startswith("HERDR_PROTOCOL="):
            block = _parse_block(raw_lines, index)
            if block is not None:
                blocks.append(block)
    return blocks

def parse_protocol(text: str, run_id: str, turn_id: str) -> ProtocolBlock | None:
    """Return the block for the current run+turn, or None. Conflicting blocks are an error."""
    matching = [block for block in find_protocol_blocks(text) if block.run_id == run_id and block.turn_id == turn_id]
    if not matching:
        return None
    if len({(b.version, b.stage, b.next_agent, b.handoff, b.gate, b.payload) for b in matching}) != 1:
        raise SupervisorError("conflicting protocol blocks match the current turn; refusing to route")
    return matching[-1]
