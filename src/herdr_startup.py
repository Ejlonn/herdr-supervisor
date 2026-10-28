"""Human-handled startup waits: screen classification from retained captures, bounded evidence, durable wait records and their budget.

A freshly started provider can stop at a startup update prompt or notice before it is ready. The supervisor never presses a key, chooses
Update or Skip, updates a package or grants a permission: the human acts in the diagnostic/prepared pane and then confirms through an
authenticated continuation. This module holds only the pure parts:

* `classify_screen` — provider-specific recognition ONLY from retained captures (see tests/fixtures/startup/PROVENANCE.md). Anything else,
  including every Claude update/notice screen and an unavailable screen, is an explicit UNKNOWN: an actionable wait, never positive update recognition.
  A recognized Codex folder-trust dialog is a separate decision and never an update wait. The Claude folder-trust prompt (one retained capture) is a
  distinct, bounded HUMAN decision: it opens a wait only when the displayed folder is provably the authorized one (`trust_folder_binding`); the supervisor
  never presses a key, trusts a folder or touches Claude's configuration.
* bounded, redacted screen evidence and the executable/process records a continuation must find unchanged;
* the durable wait record (one 0600 file per wait below `Paths.startup_waits_dir`), its finite budget (a later wait gets only what is left, the
  optional `not_after_unix` ceiling is never extended) and the public view used by status, the CLI and Telegram.

Session-start/run integration (what opens, continues, cancels or expires a wait) lives in herdr_sessions, herdr_workflow and herdr_runtime. Depends on herdr_core and herdr_redaction only."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Any, Callable

from herdr_core import PROVIDERS, Paths, SupervisorError, atomic_write_json, iso_utc, load_json, valid_pane_id
from herdr_redaction import redact

WAIT_SCHEMA = 1
WAIT_CONTEXTS = ("preparation", "run")
WAIT_STATUSES = ("WAITING", "CONTINUING", "RESOLVED", "CANCELLED", "EXPIRED", "SUPERSEDED")
OPEN_STATUSES = ("WAITING", "CONTINUING")
WAIT_PHASES = ("PROCESS_BOUND", "AGENT_STARTING", "BOOTSTRAP_INTENT", "RUN_RESTORE")
WAIT_SITES = ("agent_start_not_ready", "launch_alias_unverified", "agent_not_ready", "bootstrap_target_not_ready", "run_restore_not_ready")
CLASSIFICATION_KINDS = ("codex_update_menu", "codex_update_banner", "unknown_screen", "screen_unavailable", "trust_dialog", "claude_trust_folder")
RECOGNIZED_KINDS = {"codex_update_menu": "codex", "codex_update_banner": "codex", "claude_trust_folder": "claude"}  # kind -> the only provider whose retained capture it comes from
EVIDENCE_MAX_LINES = 30
EVIDENCE_LINE_WIDTH = 160
HISTORY_MAX = 12
EVENTS_PER_WAIT = 100  # one wait owns the event-sequence block [ordinal*100, ordinal*100+99]; blocks of different waits can never collide
ORIGIN_COMMAND_MAX_BYTES = 24_000
VERSION_TIMEOUT_SECONDS = 10
HASH_CHUNK = 1 << 20

# Retained captures (tests/fixtures/startup/PROVENANCE.md). The Codex update MENU option text was retained by Herdr's pane topic in attempt 2
# ("1. Update now (runs `sh -c 'curl -fsSL https://chatgpt.com/codex/install.sh |"), the post-handling BANNER by the attempt-2 visible-screen
# capture ("Update available! 0.163.0 -> 0.164.0"), the Codex folder-trust option by the first attempt's topic ("1. Trust and continue").
# The Claude folder-trust prompt was retained once, by the native startup-wait diagnostic (r2 attempt, 2026-10-27): the visible screen of pane w7:p2, kept as
# `claude-trust-dialog.txt` (tests/fixtures/startup/PROVENANCE.md). No Claude UPDATE or notice screen was ever retained, so Claude has no pattern for those.
_OPTION_PREFIX = r"^[\s>›*•❯▶\-]{0,6}"
_UPDATE_MENU = re.compile(_OPTION_PREFIX + r"1\.\s+Update now\b", re.MULTILINE)
_UPDATE_BANNER = re.compile(r"Update available!\s+(\d+\.\d+\.\d+)\s*->\s*(\d+\.\d+\.\d+)")
_TRUST_MENU = re.compile(_OPTION_PREFIX + r"1\.\s+Trust and continue\b", re.MULTILINE)
_VERSION_LINE = re.compile(r"\d+\.\d+\.\d+[\w.+-]*")
# The Claude folder-trust prompt, every anchor from the retained capture: the workspace header, the safety sentence, and BOTH options (the default is the safe "No, exit").
_CLAUDE_WORKSPACE = re.compile(r"^\s*Accessing workspace:\s*$", re.MULTILINE)
# The sentence is matched word by word with whitespace (spaces or line breaks) between the exact words: a narrow pane wraps it (the native 63-column capture breaks it after "one you"). Only THIS anchor is
# reflow-tolerant, and it is matched on the screen text as captured, so the match offsets keep `_claude_trust`'s header-to-sentence slice: a wrapped FOLDER is still several lines and stays unprovable.
_CLAUDE_SAFETY = re.compile(r"\s+".join(re.escape(word) for word in "Quick safety check: Is this a project you created or one you trust?".split()))
_CLAUDE_TRUST_OPTION = re.compile(_OPTION_PREFIX + r"Yes, I trust this folder\s*$", re.MULTILINE)
_CLAUDE_EXIT_OPTION = re.compile(_OPTION_PREFIX + r"No, exit\s*$", re.MULTILINE)


def redacted_lines(lines: list[Any], *, max_lines: int = EVIDENCE_MAX_LINES, width: int = EVIDENCE_LINE_WIDTH) -> list[str]:
    """The last non-empty screen lines, redacted and bounded. Never raw terminal text."""
    kept = [redact(str(line).rstrip(), limit=width) for line in lines if str(line).strip()]
    return kept[-max_lines:]


def classify_screen(provider: str, lines: list[Any] | None) -> dict[str, Any]:
    """Recognition from retained captures only. `recognized` is True solely for a pattern that was actually retained for that provider."""
    if provider not in PROVIDERS:
        raise SupervisorError("unknown provider for screen classification")
    if lines is None:
        return {"kind": "screen_unavailable", "recognized": False, "pattern_id": None, "versions": None}
    text = "\n".join(str(line) for line in lines)
    if provider == "codex":
        if _TRUST_MENU.search(text):
            return {"kind": "trust_dialog", "recognized": True, "pattern_id": "codex-trust-option-1/attempt1-topic", "versions": None}
        if _UPDATE_MENU.search(text):
            banner = _UPDATE_BANNER.search(text)
            return {"kind": "codex_update_menu", "recognized": True, "pattern_id": "codex-update-option-1/attempt2-topic",
                    "versions": {"from": banner.group(1), "to": banner.group(2)} if banner else None}
        banner = _UPDATE_BANNER.search(text)
        if banner:
            return {"kind": "codex_update_banner", "recognized": True, "pattern_id": "codex-update-banner/attempt2-visible",
                    "versions": {"from": banner.group(1), "to": banner.group(2)}}
    if provider == "claude":
        trust = _claude_trust(text)
        if trust is not None:
            return {"kind": "claude_trust_folder", "recognized": True, "pattern_id": "claude-trust-folder/r2-attempt-visible", "versions": None, "folder": trust["folder"]}
    return {"kind": "unknown_screen", "recognized": False, "pattern_id": None, "versions": None}


def _claude_trust(text: str) -> dict[str, Any] | None:
    """The retained Claude folder-trust prompt, or None. All four anchors must be present; the displayed folder is the ONE line between the workspace header and the safety sentence
    and is reported only when it is a single absolute path (a wrapped, missing or ambiguous folder is `None`: the dialog is recognized, the folder is unprovable)."""
    header, safety = _CLAUDE_WORKSPACE.search(text), _CLAUDE_SAFETY.search(text)
    if not (header and safety and _CLAUDE_TRUST_OPTION.search(text) and _CLAUDE_EXIT_OPTION.search(text)) or safety.start() < header.end():
        return None
    between = [line.strip() for line in text[header.end():safety.start()].splitlines() if line.strip()]
    folder = between[0] if len(between) == 1 and between[0].startswith("/") and "\x00" not in between[0] else None
    return {"folder": folder}


def trust_folder_binding(classification: dict[str, Any], authorized: str, process_cwd: str | None) -> tuple[dict[str, Any] | None, str | None]:
    """(binding, None) only when the folder the Claude prompt DISPLAYS, the folder the launch was authorized for and the folder the owned process is actually in are provably the same
    absolute path; otherwise (None, reason). No default, no inference: a mismatched or unprovable folder never opens a trust wait."""
    displayed = classification.get("folder")
    if not isinstance(displayed, str) or not displayed.startswith("/"):
        return None, "the folder it displays could not be read as one absolute path (wrapped, missing or ambiguous)"
    if not isinstance(authorized, str) or not authorized.startswith("/") or not isinstance(process_cwd, str) or not process_cwd.startswith("/"):
        return None, "the authorized folder or the process folder is not an absolute path"
    shown, allowed, here = os.path.normpath(displayed), os.path.realpath(authorized), os.path.realpath(process_cwd)
    if shown != allowed or here != allowed:
        return None, f"the displayed folder {shown!r} is not the authorized folder {allowed!r} that the process runs in ({here!r})"
    return {"displayed": shown, "authorized": allowed, "process_cwd": here}, None


def screen_blocker(provider: str, wait: dict[str, Any], screen: dict[str, Any]) -> str | None:
    """Why the pane still blocks a continuation (None = no visible blocker). A still-visible update menu or trust prompt refuses even when the lifecycle says ready; for a wait
    opened at the Claude trust prompt the clearing must be PROVEN by an available capture (text that cannot be read is not text that disappeared)."""
    kind = evidence_classification(provider, screen)["kind"]
    if kind == "codex_update_menu":
        return "the update prompt option is still on screen"
    if kind == "trust_dialog":
        return "a folder-trust dialog is showing; that is a separate decision and is not handled here"
    if kind == "claude_trust_folder":
        return "the Claude folder-trust prompt is still on screen: choose Trust or Exit in that pane yourself first"
    if (wait.get("classification") or {}).get("kind") == "claude_trust_folder" and not screen.get("available"):
        return "the pane could not be read, so the Claude folder-trust prompt is not proven cleared"
    return None


def trust_folder_problem(wait: dict[str, Any], authorized_now: str) -> str | None:
    """A trust wait stays bound to the folder it was opened for: the launch's authorized folder must still be that one."""
    bound = wait.get("trust_folder")
    if bound is None:
        return None
    if not isinstance(bound, dict) or os.path.realpath(authorized_now) != bound.get("authorized"):
        return "the authorized folder is no longer the one the folder-trust wait was opened for"
    return None


def capture_screen(read: Callable[[str], str] | None, pane_id: str, *, now: float) -> dict[str, Any]:
    """Best-effort visible-screen evidence of one exact pane. A missing adapter method or any read failure is recorded as unavailable;
    it never blocks opening the wait and never counts as recognition."""
    base = {"source": "visible", "captured_at_unix": float(now)}
    if not callable(read) or not valid_pane_id(pane_id):
        return {**base, "available": False, "reason": "pane read contract unavailable", "line_count": 0, "sha256": None, "lines": []}
    try:
        raw = read(pane_id)
    except Exception as error:  # noqa: BLE001 - evidence is best effort; the class of failure is all that is kept
        return {**base, "available": False, "reason": f"pane read failed ({getattr(error, 'code', None) or type(error).__name__})"[:80], "line_count": 0, "sha256": None, "lines": []}
    if not isinstance(raw, str):
        return {**base, "available": False, "reason": "pane read returned no text", "line_count": 0, "sha256": None, "lines": []}
    raw_lines = raw.splitlines()
    return {**base, "available": True, "reason": None, "line_count": len(raw_lines), "sha256": hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest(), "lines": redacted_lines(raw_lines)}


def evidence_classification(provider: str, evidence: dict[str, Any], *, raw_lines: list[str] | None = None) -> dict[str, Any]:
    """Classify from the captured lines. The stored (redacted) lines are classified so the persisted record explains itself; the caller may pass the
    unredacted lines to avoid a redaction artefact hiding a retained pattern."""
    lines = raw_lines if raw_lines is not None else (evidence.get("lines") if evidence.get("available") else None)
    return classify_screen(provider, lines)


def process_start_ticks(pid: int, proc_root: str = "/proc") -> int | None:
    """Kernel start time (clock ticks) of a pid, defeating pid reuse; None where /proc is unavailable (recorded as None, compared as None)."""
    try:
        text = Path(proc_root, str(pid), "stat").read_text()
        return int(text.rsplit(")", 1)[1].split()[19])
    except (OSError, ValueError, IndexError):
        return None


def _sha256_file(path: str) -> str | None:
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(HASH_CHUNK), b""):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


def executable_record(command: str, *, runner: Callable[..., Any] = subprocess.run, which: Callable[[str], str | None] = shutil.which) -> dict[str, Any]:
    """Path, size, SHA-256 and `--version` text of one provider executable, read without any model turn (`<exe> --version`, bounded, no stdin).
    Unknown parts are None; a continuation never treats an unknown before/after pair as unchanged unless the bytes are provably equal."""
    resolved = command if command.startswith("/") else (which(command) or "")
    path = os.path.realpath(resolved) if resolved else ""
    record: dict[str, Any] = {"path": path or None, "size": None, "sha256": None, "version": None}
    if not path or not os.path.isfile(path):
        return record
    try:
        record["size"] = os.stat(path).st_size
    except OSError:
        return record
    record["sha256"] = _sha256_file(path)
    try:
        completed = runner([path, "--version"], check=False, capture_output=True, text=True, timeout=VERSION_TIMEOUT_SECONDS, stdin=subprocess.DEVNULL)
        first = ((completed.stdout or "").strip().splitlines() or [""])[0]
        if completed.returncode == 0 and _VERSION_LINE.search(first):
            record["version"] = redact(first, limit=80)
    except (OSError, subprocess.SubprocessError):
        pass
    return record


def executable_problem(before: dict[str, Any], after: dict[str, Any]) -> str | None:
    """None only when the executable is provably the same: identical path and SHA-256 (the version text may then only be equal or unknown on one side)."""
    for key in ("path", "sha256", "size"):
        if before.get(key) is None or after.get(key) is None:
            return f"the executable {key} could not be established before or after, so it is not proven unchanged"
        if before[key] != after[key]:
            return f"the executable {key} changed ({str(before[key])[:16]} -> {str(after[key])[:16]})"
    if before.get("version") is not None and after.get("version") is not None and before["version"] != after["version"]:
        return f"the reported version changed ({before['version']} -> {after['version']})"
    return None


def process_problem(before: dict[str, Any] | None, after: dict[str, Any] | None) -> str | None:
    """The same launched process, not a replacement: pid, shell, argv, cwd and kernel start time all equal."""
    if not isinstance(before, dict) or not isinstance(after, dict):
        return "the launched process could not be established, so it is not proven to be the same one"
    for key in ("pid", "shell_pid", "argv", "cwd", "start_ticks"):
        if before.get(key) != after.get(key):
            return f"the pane's provider process changed ({key}); this is a replacement, not the process that was waiting"
    return None


# ---------------------------------------------------------------------------------------------- budget


def wait_budget(settings: dict[str, Any], now: float, prior: list[dict[str, Any]]) -> tuple[float, int, int]:
    """(deadline_unix, total_budget_seconds, remaining_seconds_at_open). The total human-wait budget of one preparation/run per provider is
    never extended: elapsed time of earlier waits is deducted, the optional `not_after_unix` ceiling caps the deadline, and a request that
    cannot get at least one second, or that would exceed the wait-count bound, is refused (fail closed)."""
    total = int(settings["max_seconds"])
    if len(prior) >= int(settings["max_waits_per_provider"]):
        raise SupervisorError(f"the startup wait count for this provider is exhausted ({len(prior)} earlier waits); nothing further is waited for")
    used = 0.0
    for record in prior:
        # The human's time ends when they confirmed (continued_at), else when the wait was closed, else at its deadline.
        stop = next((record[key] for key in ("continued_at_unix", "closed_at_unix") if isinstance(record.get(key), (int, float)) and not isinstance(record.get(key), bool)), record["deadline_unix"])
        used += max(0.0, min(float(stop), float(record["deadline_unix"])) - float(record["opened_at_unix"]))
    remaining = total - used
    ceiling = settings.get("not_after_unix")
    deadline = now + remaining
    if ceiling is not None:
        deadline = min(deadline, float(ceiling))
    if deadline - now < 1:
        raise SupervisorError("the startup wait budget (or the caller's total deadline) is exhausted; nothing further is waited for")
    return deadline, total, int(deadline - now)


# ---------------------------------------------------------------------------------------------- records


def _sha_ok(value: Any) -> bool:
    return value is None or (isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value))


def new_wait_record(*, context: str, provider: str, pane_id: str | None, phase: str, site: str, cause_code: str | None, now: float, settings: dict[str, Any],
                    prior: list[dict[str, Any]], evidence: dict[str, Any], classification: dict[str, Any], executable: dict[str, Any], process: dict[str, Any] | None,
                    preparation_id: str | None = None, run_id: str | None = None, agent_name: str | None = None, binding: dict[str, Any] | None = None,
                    ordinal: int = 1, origin_command: dict[str, Any] | None = None, trust_folder: dict[str, Any] | None = None) -> dict[str, Any]:
    deadline, total, remaining = wait_budget(settings, now, prior)
    record: dict[str, Any] = {
        "schema_version": WAIT_SCHEMA, "wait_id": str(uuid.uuid4()), "context": context, "preparation_id": preparation_id, "run_id": run_id, "provider": provider,
        "pane_id": pane_id, "agent_name": agent_name, "phase": phase, "site": site, "cause_code": cause_code, "process": process, "binding": binding or {},
        "executable": executable, "evidence": evidence, "classification": classification, "opened_at_unix": float(now), "deadline_unix": float(deadline),
        "budget_total_seconds": total, "budget_remaining_at_open_seconds": remaining, "status": "WAITING", "continue_attempts": 0, "event_sequence": 0, "notified": False,
        "ordinal": ordinal, "required_event_sequence": None, "origin_command": origin_command, "original_started_at_unix": None, "original_run_id": None,
        "trust_folder": trust_folder, "closed_at_unix": None, "history": [{"at_unix": float(now), "action": "opened", "detail": f"{site}:{classification['kind']}"}],
    }
    validate_wait_record(record)
    return record


def validate_wait_record(record: Any) -> dict[str, Any]:
    if not isinstance(record, dict) or record.get("schema_version") != WAIT_SCHEMA or type(record["schema_version"]) is not int:
        raise SupervisorError("startup wait record is invalid")
    wait_id = record.get("wait_id")
    try:
        if not isinstance(wait_id, str) or str(uuid.UUID(wait_id)) != wait_id:
            raise ValueError
    except ValueError as error:
        raise SupervisorError("startup wait identity is invalid") from error
    context, provider = record.get("context"), record.get("provider")
    if context not in WAIT_CONTEXTS or provider not in PROVIDERS or record.get("status") not in WAIT_STATUSES:
        raise SupervisorError("startup wait context, provider or status is invalid")
    if (context == "preparation") != isinstance(record.get("preparation_id"), str) or (context == "run") != isinstance(record.get("run_id"), str):
        raise SupervisorError("startup wait is not bound to exactly one preparation or run")
    if record.get("phase") not in WAIT_PHASES or record.get("site") not in WAIT_SITES or (context == "run") != (record.get("phase") == "RUN_RESTORE"):
        raise SupervisorError("startup wait phase or site is invalid")
    if record.get("pane_id") is not None and not valid_pane_id(record["pane_id"]):
        raise SupervisorError("startup wait pane is not canonical")
    for key in ("opened_at_unix", "deadline_unix"):
        if isinstance(record.get(key), bool) or not isinstance(record.get(key), (int, float)):
            raise SupervisorError(f"startup wait {key} is invalid")
    if record["deadline_unix"] <= record["opened_at_unix"]:
        raise SupervisorError("startup wait deadline does not follow its opening")
    for key in ("budget_total_seconds", "budget_remaining_at_open_seconds", "continue_attempts", "event_sequence"):
        if type(record.get(key)) is not int or record[key] < 0:
            raise SupervisorError(f"startup wait {key} is invalid")
    if type(record.get("ordinal")) is not int or record["ordinal"] < 1 or record["event_sequence"] >= EVENTS_PER_WAIT:
        raise SupervisorError("startup wait ordinal or event sequence is invalid")
    required = record.get("required_event_sequence")
    if required is not None and (type(required) is not int or not record["ordinal"] * EVENTS_PER_WAIT < required < (record["ordinal"] + 1) * EVENTS_PER_WAIT):
        raise SupervisorError("startup wait notification sequence is outside its own block")
    origin = record.get("origin_command")
    if origin is not None and (not isinstance(origin, dict) or origin.get("action") != "task" or len(json.dumps(origin, sort_keys=True)) > ORIGIN_COMMAND_MAX_BYTES):
        raise SupervisorError("startup wait original request is invalid")
    started = record.get("original_started_at_unix")
    if started is not None and (isinstance(started, bool) or not isinstance(started, (int, float))):
        raise SupervisorError("startup wait original-start marker is invalid")
    if type(record.get("notified")) is not bool:
        raise SupervisorError("startup wait notification flag is invalid")
    classification, evidence, executable = record.get("classification"), record.get("evidence"), record.get("executable")
    if (not isinstance(classification, dict) or classification.get("kind") not in CLASSIFICATION_KINDS or type(classification.get("recognized")) is not bool
            or classification["kind"] == "trust_dialog"):
        raise SupervisorError("startup wait classification is invalid")  # a recognized Codex trust dialog never opens an update wait
    if classification["recognized"] != (classification["kind"] in RECOGNIZED_KINDS):
        raise SupervisorError("startup wait recognition flag does not match its classification")
    if classification["recognized"] and RECOGNIZED_KINDS[classification["kind"]] != record["provider"]:
        raise SupervisorError(f"only {RECOGNIZED_KINDS[classification['kind']].title()} screens have this retained recognition pattern")
    trust = record.get("trust_folder")
    if (classification["kind"] == "claude_trust_folder") != (trust is not None):
        raise SupervisorError("a Claude folder-trust wait must carry its folder binding, and no other wait may")
    if trust is not None:
        paths = (trust.get("displayed"), trust.get("authorized"), trust.get("process_cwd")) if isinstance(trust, dict) and set(trust) == {"displayed", "authorized", "process_cwd"} else ()
        if not paths or not all(isinstance(path, str) and path.startswith("/") for path in paths) or len(set(paths)) != 1 or classification.get("folder") != trust["displayed"]:
            raise SupervisorError("startup wait folder binding is invalid: the displayed, authorized and process folders must be one absolute path")
    if (not isinstance(evidence, dict) or evidence.get("source") != "visible" or type(evidence.get("available")) is not bool or not isinstance(evidence.get("lines"), list)
            or len(evidence["lines"]) > EVIDENCE_MAX_LINES or not all(isinstance(line, str) and len(line) <= EVIDENCE_LINE_WIDTH for line in evidence["lines"]) or not _sha_ok(evidence.get("sha256"))):
        raise SupervisorError("startup wait evidence is invalid")
    if not isinstance(executable, dict) or set(executable) != {"path", "size", "sha256", "version"} or not _sha_ok(executable.get("sha256")):
        raise SupervisorError("startup wait executable record is invalid")
    process = record.get("process")
    if process is not None and (not isinstance(process, dict) or not {"pid", "shell_pid", "argv", "cwd"} <= set(process) or type(process["pid"]) is not int or type(process["shell_pid"]) is not int
                                or not isinstance(process["argv"], list) or not isinstance(process["cwd"], str)):
        raise SupervisorError("startup wait process record is invalid")
    if not isinstance(record.get("binding"), dict) or not isinstance(record.get("history"), list) or len(record["history"]) > HISTORY_MAX:
        raise SupervisorError("startup wait binding or history is invalid")
    return record


def wait_path(paths: Paths, wait_id: str) -> Path:
    try:
        if str(uuid.UUID(wait_id)) != wait_id:
            raise ValueError
    except (ValueError, AttributeError, TypeError) as error:
        raise SupervisorError("startup wait identity is invalid") from error
    return paths.startup_waits_dir / f"{wait_id}.json"


def write_wait(paths: Paths, record: dict[str, Any]) -> None:
    validate_wait_record(record)
    paths.startup_waits_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    atomic_write_json(wait_path(paths, record["wait_id"]), record)


def read_wait(paths: Paths, wait_id: str) -> dict[str, Any] | None:
    path = wait_path(paths, wait_id)
    if not path.exists():
        return None
    record = validate_wait_record(load_json(path, label="startup wait"))
    if record["wait_id"] != wait_id:
        raise SupervisorError("startup wait file does not match its identity")
    return record


def list_waits(paths: Paths, *, preparation_id: str | None = None, run_id: str | None = None, provider: str | None = None, statuses: tuple[str, ...] | None = None) -> list[dict[str, Any]]:
    directory = paths.startup_waits_dir
    if not directory.exists():
        return []
    found = []
    for path in sorted(directory.glob("*.json")):
        record = validate_wait_record(load_json(path, label="startup wait"))
        if ((preparation_id is None or record.get("preparation_id") == preparation_id) and (run_id is None or record.get("run_id") == run_id)
                and (provider is None or record["provider"] == provider) and (statuses is None or record["status"] in statuses)):
            found.append(record)
    found.sort(key=lambda r: (r["opened_at_unix"], r["wait_id"]))
    return found


def next_event_sequence(record: dict[str, Any]) -> int:
    """The next event sequence of THIS wait: a block of 100 numbers owned by its ordinal within the preparation, so the outbox event ids (run id + sequence + type) of different
    waits of one preparation, or of its two providers, can never be equal. The caller persists the record BEFORE emitting (write-ahead): a crash can leave a gap, never a reuse."""
    local = record["event_sequence"] + 1
    if local >= EVENTS_PER_WAIT:
        raise SupervisorError("the notification budget of this startup wait is exhausted")
    record["event_sequence"] = local
    return record["ordinal"] * EVENTS_PER_WAIT + local


def emit_wait_event(emit: Callable[..., Any] | None, record: dict[str, Any], event_type: str, data: dict[str, Any], *, state: dict[str, Any] | None = None, sequence: int | None = None) -> bool:
    """Publish one outbox event for a wait through the supervisor's own event writer. A preparation has no run state yet, so its events use a minimal pseudo-state keyed by the
    preparation id and the wait's own sequence block (`sequence`, allocated and persisted by the caller); a run wait uses the real run state. Re-emitting the SAME sequence is idempotent
    (same event id); a different wait or event can never reuse it. Returns False when no writer exists."""
    if not callable(emit):
        return False
    identity = f"{record.get('preparation_id') or record.get('run_id')}:startup-wait:{record['wait_id']}"  # the event id names this wait, never only (run, sequence, type)
    if state is not None:
        emit(state, event_type, data, identity=identity)
        return True
    if type(sequence) is not int or not record["ordinal"] * EVENTS_PER_WAIT < sequence < (record["ordinal"] + 1) * EVENTS_PER_WAIT:
        raise SupervisorError("a preparation event needs a sequence from its own wait's block")
    pseudo = {"run_id": record.get("preparation_id") or record.get("run_id"), "supervisor_state": "STARTUP_WAIT", "event_sequence": sequence - 1, "pending_gate": None}
    emit(pseudo, event_type, data, identity=identity)
    return True


def announce_wait(paths: Paths, emit: Callable[..., Any] | None, now: float, record: dict[str, Any], event_type: str, **extra: Any) -> None:
    """Publish one event of a PREPARATION wait with a sequence from the wait's own block, persisted BEFORE the event is written (write-ahead): a crash leaves a gap, never a reused id,
    so no event of another wait (or provider) of the same preparation can be overwritten or inherit its delivery sidecar. The REQUIRED notification keeps ONE fixed sequence, so
    re-announcing it after a restart is idempotent (same event id, same sidecar)."""
    if event_type == "STARTUP_WAIT_REQUIRED" and record.get("required_event_sequence") is not None:
        sequence = record["required_event_sequence"]
    else:
        sequence = next_event_sequence(record)
        if event_type == "STARTUP_WAIT_REQUIRED":
            record["required_event_sequence"] = sequence
        write_wait(paths, record)
    view = wait_view(record, now)
    emit_wait_event(emit, record, event_type, {"wait": view, "instructions": instructions(view), **extra}, sequence=sequence)
    if event_type == "STARTUP_WAIT_REQUIRED":
        record["notified"] = True
    write_wait(paths, record)


def wait_refusal(record: dict[str, Any] | None, wait_id: str) -> str | None:
    """Why a continuation must do nothing at all for this wait (None = it may be examined). Duplicate, late, stale and cross-run callbacks all end here."""
    if record is None:
        return f"unknown startup wait {str(wait_id)[:8]}; nothing was changed"
    if record["status"] == "CONTINUING":
        return (f"startup wait {wait_id[:8]} was already continued (a duplicate or late callback); nothing was done again. If the start did not finish, use the next step named on the "
                "result card (`start-original`, or re-running the same CLI `run` command) or cancel the start")
    if record["status"] != "WAITING":
        return f"startup wait {wait_id[:8]} is {record['status']}; it cannot be continued"
    return None


def add_history(record: dict[str, Any], now: float, action: str, detail: str = "") -> None:
    record["history"] = [*record["history"][-(HISTORY_MAX - 1):], {"at_unix": float(now), "action": action[:40], "detail": redact(detail, limit=160)}]


def close_wait(record: dict[str, Any], status: str, now: float, detail: str = "") -> None:
    if status not in ("RESOLVED", "CANCELLED", "EXPIRED", "SUPERSEDED"):
        raise SupervisorError("a startup wait can only be closed as resolved, cancelled, expired or superseded")
    record["status"] = status
    record["closed_at_unix"] = float(min(now, record["deadline_unix"])) if status == "EXPIRED" else float(now)
    add_history(record, now, status.lower(), detail)


def expired(record: dict[str, Any], now: float) -> bool:
    return record["status"] in OPEN_STATUSES and now > float(record["deadline_unix"])


def wait_view(record: dict[str, Any], now: float) -> dict[str, Any]:
    """Bounded, secret-free projection for status, the CLI, Telegram and the event payload. Evidence lines are the already-redacted excerpt."""
    classification = record["classification"]
    return {
        "wait_id": record["wait_id"], "context": record["context"], "preparation_id": record.get("preparation_id"), "run_id": record.get("run_id"), "provider": record["provider"],
        "pane_id": record.get("pane_id"), "phase": record["phase"], "site": record["site"], "cause_code": record.get("cause_code"), "status": record["status"],
        "kind": classification["kind"], "recognized": classification["recognized"], "pattern_id": classification.get("pattern_id"), "versions": classification.get("versions"),
        "evidence_available": record["evidence"]["available"], "evidence_sha256": record["evidence"].get("sha256"), "evidence_excerpt": record["evidence"]["lines"][-6:],
        "executable_version": record["executable"].get("version"), "opened_at": iso_utc(record["opened_at_unix"]), "deadline_at": iso_utc(record["deadline_unix"]),
        "remaining_seconds": max(0, int(record["deadline_unix"] - now)) if record["status"] in OPEN_STATUSES else 0,
        "budget_total_seconds": record["budget_total_seconds"], "continue_attempts": record["continue_attempts"],
        "trust_folder": (record.get("trust_folder") or {}).get("authorized"),
        "start_original_available": bool(isinstance(record.get("origin_command"), dict) and record.get("original_started_at_unix") is None and record["status"] in ("CONTINUING", "RESOLVED")),
    }


def instructions(view: dict[str, Any]) -> list[str]:
    """Plain, secret-free operator text shared by the CLI error, status and Telegram. It states where the human acts and what the supervisor never does."""
    provider = view["provider"].title()
    continue_line = ("Then continue (Telegram button, or `herdr-supervisor continue-startup --wait-id %s`). The same process, its executable and the deployed contracts are re-verified first; "
                     "a replaced process or changed contract holds for a new decision." % view["wait_id"])
    end_line = (f"This wait ends at {view['deadline_at']} ({view['remaining_seconds']} s left); after that nothing continues. "
                f"To release the start now: `herdr-supervisor cancel-startup --wait-id {view['wait_id']}`.")
    if view["kind"] == "claude_trust_folder":
        return [f"{provider} started in pane {view['pane_id']} but is not ready; it asks whether you trust the folder {view.get('trust_folder')} (recognized from a retained capture; this is the folder this start was authorized for).",
                "Review that folder yourself. Trusting it lets Claude Code read, edit and execute files there. Choose 'Yes, I trust this folder' or 'No, exit' in THAT pane. "
                "The supervisor never presses a key, trusts a folder or changes a permission, and sends nothing to your other sessions.",
                "If you chose Exit the Claude process ends: do not continue, cancel instead. After Trust, Claude may show a further screen (for example an update); if it does, this start opens one more wait for it inside the same budget.",
                continue_line, end_line]
    what = {"codex_update_menu": "a Codex update prompt (the 'Update now' option was recognized from a retained capture)",
            "codex_update_banner": "a Codex update notice (recognized from a retained capture; it may no longer be blocking)",
            "unknown_screen": "a screen this supervisor does not recognize (no retained capture matches)",
            "screen_unavailable": "a screen that could not be read"}.get(view["kind"], "an unrecognized screen")
    lines = [f"{provider} started in pane {view['pane_id']} but is not ready; it shows {what}.",
             "Handle it yourself in THAT pane (for an update prompt choose Update or Skip). The supervisor never presses a key, updates a package or grants a permission, and sends nothing to your other sessions.",
             continue_line, end_line]
    if view["kind"] in ("unknown_screen", "screen_unavailable"):
        lines.append("If the pane shows a folder-trust or permission dialog instead, that is a separate decision: cancel and ask for it explicitly.")
    return lines


__all__ = ["add_history", "announce_wait", "capture_screen", "classify_screen", "close_wait", "emit_wait_event", "evidence_classification", "executable_problem", "executable_record", "expired", "instructions", "list_waits",
           "new_wait_record", "next_event_sequence", "process_problem", "process_start_ticks", "read_wait", "redacted_lines", "screen_blocker", "trust_folder_binding", "trust_folder_problem", "validate_wait_record", "wait_budget", "wait_refusal", "wait_path", "wait_view", "write_wait"]
