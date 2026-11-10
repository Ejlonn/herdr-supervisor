"""Reusable named Claude/Codex session pairs.

A pair is a per-installation, immutable record of one Claude and one Codex native session (provider, pane locator,
native session id) plus the repository/review context the pair works in. Names are display labels only: they are never
paths, never authority, and never part of identity. Everything that matters for routing is the exact native identity.

This module holds the registry (a bounded private JSON document under the supervisor state directory), the closed
selection shape carried inside `session_selection["pair"]`, the frozen per-run binding (`state["session_pair"]`) and the
read-only live checks shared by the Telegram card, the supervisor start path and the first delivery. It performs no
launch, prompt, rename, rebind or configuration change; it never edits `owners.json`.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import os
import re
import unicodedata
import uuid
from pathlib import Path
from typing import Any, Callable, Iterator

from herdr_cli import match_exact_session, session_identity
from herdr_core import (
    FOLLOWUP_UNRESOLVED,
    PROVIDERS,
    READY_STATES,
    SupervisorError,
    atomic_write_json,
    canonical_json,
    iso_utc,
    load_json,
    valid_pane_id,
)

REGISTRY_SCHEMA = 1
MAX_PAIRS = 64
MAX_NAME = 64
MAX_PATH = 1024
MAX_ROOTS = 16
PAIR_MODES = ("saved", "save_existing", "create")
SOURCES = ("existing", "created")
CONTEXT_KEYS = ("instruction_root", "repository", "review_base")
_ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def _uuid(value: Any) -> bool:
    return type(value) is str and _ID_RE.fullmatch(value) is not None and str(uuid.UUID(value)) == value


def valid_pane(value: Any) -> bool:
    return valid_pane_id(value)


def _hex64(value: Any) -> bool:
    return type(value) is str and _HEX64.fullmatch(value) is not None


# ----- names and context (pure; no file-system access)

def normalize_name(value: Any) -> str:
    """A bounded display label. Control/format characters, path separators and leading/trailing space are rejected;
    internal whitespace runs collapse to one space. The label is never used as a path or as authority."""
    if type(value) is not str:
        raise SupervisorError("pair name must be text")
    text = unicodedata.normalize("NFC", value)
    if any(unicodedata.category(ch)[0] == "C" for ch in text):
        raise SupervisorError("pair name must not contain control characters")
    text = " ".join(text.split())
    if not text or len(text) > MAX_NAME:
        raise SupervisorError(f"pair name must be 1-{MAX_NAME} characters")
    if any(ch in text for ch in "/\\<>`\"'"):
        raise SupervisorError("pair name must not contain path separators, quotes or markup characters")
    return text


def name_key(name: str) -> str:
    return unicodedata.normalize("NFC", name).casefold()


def _abs_text(value: Any, label: str) -> str:
    if type(value) is not str or not value or len(value) > MAX_PATH or "\x00" in value or not value.startswith("/"):
        raise SupervisorError(f"{label} must be an absolute path")
    if value != os.path.normpath(value) or value.startswith("//") or ".." in value.split("/"):
        raise SupervisorError(f"{label} must be a canonical absolute path")
    return value


def validate_context_shape(context: Any) -> dict[str, str]:
    """Exact keys and canonical absolute path text; file-system containment is checked by `check_context`."""
    if not isinstance(context, dict) or set(context) != set(CONTEXT_KEYS):
        raise SupervisorError("pair context must carry exactly instruction_root, repository and review_base")
    return {key: _abs_text(context[key], f"pair context {key}") for key in CONTEXT_KEYS}


def configured_roots(config: dict[str, Any]) -> list[str]:
    section = config.get("session_pairs") or {}
    return list(section.get("allowed_roots") or [])


def pairs_enabled(config: dict[str, Any]) -> bool:
    section = config.get("session_pairs")
    return isinstance(section, dict) and section.get("enabled") is True and bool(section.get("allowed_roots"))


def instruction_root(config: dict[str, Any]) -> str:
    section = config.get("session_pairs") or {}
    value = section.get("instruction_root") or config.get("project_root")
    return _abs_text(value, "session_pairs.instruction_root")


def check_context(config: dict[str, Any], context: Any) -> dict[str, str]:
    """Context must name existing real directories (no symlink components) inside the administrator-allowed roots.
    Telegram input never widens the roots; the instruction root is the configured one."""
    ctx = validate_context_shape(context)
    roots = []
    for root in configured_roots(config):
        try:
            roots.append(Path(root).resolve(strict=True))
        except OSError:
            continue
    if not roots:
        raise SupervisorError("no administrator-allowed pair roots are configured")
    for key in ("repository", "review_base"):
        path = Path(ctx[key])
        try:
            real = path.resolve(strict=True)
        except OSError as error:
            raise SupervisorError(f"pair context {key} does not exist") from error
        if str(real) != ctx[key] or not real.is_dir():
            raise SupervisorError(f"pair context {key} must be a real directory path without symlinks")
        if not any(real == root or real.is_relative_to(root) for root in roots):
            raise SupervisorError(f"pair context {key} is outside the administrator-allowed roots")
    if ctx["instruction_root"] != instruction_root(config):
        raise SupervisorError("pair context instruction_root must be the configured instruction root")
    return ctx


def validate_identities(value: Any) -> dict[str, dict[str, str]]:
    """Exact provider set; each record exactly {pane_id, session_id} with a canonical pane and UUID."""
    if not isinstance(value, dict) or set(value) != set(PROVIDERS):
        raise SupervisorError("pair identities must name exactly the codex and claude sessions")
    result: dict[str, dict[str, str]] = {}
    for provider in PROVIDERS:
        record = value[provider]
        if not isinstance(record, dict) or set(record) != {"pane_id", "session_id"} or not valid_pane_id(record["pane_id"]) or not _uuid(record["session_id"]):
            raise SupervisorError(f"{provider} pair identity must be a canonical pane and native session id")
        result[provider] = {"pane_id": record["pane_id"], "session_id": record["session_id"]}
    if len({result[p]["session_id"] for p in PROVIDERS}) != len(PROVIDERS) or len({result[p]["pane_id"] for p in PROVIDERS}) != len(PROVIDERS):
        raise SupervisorError("pair identities must be distinct sessions in distinct panes")
    return result


# ----- the closed selection shape: session_selection["pair"]

def validate_pair_selection(pair: Any, policy: str) -> dict[str, Any]:
    """Normalise the pair part of a task start selection. A named pair never combines with a partial fresh policy:
    saved/save_existing keep both sessions (`preserve`), create makes both (`fresh-all`)."""
    if not isinstance(pair, dict):
        raise SupervisorError("pair selection must be an object")
    mode = pair.get("mode")
    if mode not in PAIR_MODES:
        raise SupervisorError("pair selection mode is invalid")
    if mode == "saved":
        if set(pair) != {"mode", "pair_id", "fingerprint"} or not _uuid(pair["pair_id"]) or not _hex64(pair["fingerprint"]):
            raise SupervisorError("saved pair selection must carry exactly its pair id and fingerprint")
        if policy != "preserve":
            raise SupervisorError("a saved pair cannot be combined with a fresh session policy")
        return {"mode": "saved", "pair_id": pair["pair_id"], "fingerprint": pair["fingerprint"]}
    if mode == "save_existing":
        if set(pair) != {"mode", "name", "context", "identities"}:
            raise SupervisorError("save-existing pair selection must carry exactly its name, context and identities")
        if policy != "preserve":
            raise SupervisorError("an existing pair cannot be combined with a fresh session policy")
        return {"mode": mode, "name": normalize_name(pair["name"]), "context": validate_context_shape(pair["context"]),
                "identities": validate_identities(pair["identities"])}
    if set(pair) != {"mode", "name", "context"}:
        raise SupervisorError("create pair selection must carry exactly its name and context")
    if policy != "fresh-all":
        raise SupervisorError("creating a named pair requires the fresh-all session policy")
    return {"mode": mode, "name": normalize_name(pair["name"]), "context": validate_context_shape(pair["context"])}


def selection_digest(selection: dict[str, Any]) -> str:
    """Complete normalised selection (policy, profiles, pair, context) as one binding; the same value is carried by the
    pending card, the command and the preparation journal."""
    return hashlib.sha256(canonical_json(selection)).hexdigest()


# ----- the immutable entry, its fingerprint and the frozen per-run binding

def entry_fingerprint(entry: dict[str, Any]) -> str:
    material = {key: entry[key] for key in ("pair_id", "version", "name", "identities", "context")}
    return hashlib.sha256(canonical_json(material)).hexdigest()


def validate_entry(entry: Any) -> dict[str, Any]:
    keys = {"pair_id", "version", "name", "name_key", "source", "identities", "context", "created_at", "preparation_id"}
    if not isinstance(entry, dict) or not set(entry) <= keys or not keys - {"preparation_id"} <= set(entry):
        raise SupervisorError("pair registry entry is invalid")
    if not _uuid(entry["pair_id"]) or entry["version"] != 1 or type(entry["version"]) is not int or entry["source"] not in SOURCES or type(entry["created_at"]) is not str:
        raise SupervisorError("pair registry entry header is invalid")
    if normalize_name(entry["name"]) != entry["name"] or entry["name_key"] != name_key(entry["name"]):
        raise SupervisorError("pair registry entry name is invalid")
    validate_identities(entry["identities"])
    validate_context_shape(entry["context"])
    prep = entry.get("preparation_id")
    if (entry["source"] == "created") != (prep is not None) or (prep is not None and not _uuid(prep)):
        raise SupervisorError("pair registry entry origin is invalid")
    return entry


def identity_digest(binding: dict[str, Any]) -> str:
    """Digest of what can never change inside a run: the pair id, label, both native session ids and the context. Pane locators are excluded because
    the explicit, bound owner-recovery repair may re-record where the SAME session lives; the entry fingerprint (panes included) stays as bound."""
    material = {"pair_id": binding["pair_id"], "version": binding["version"], "name": binding["name"], "context": binding["context"],
                "sessions": {p: binding["identities"][p]["session_id"] for p in PROVIDERS}}
    return hashlib.sha256(canonical_json(material)).hexdigest()


def binding_from_entry(entry: dict[str, Any], *, bound_at: str) -> dict[str, Any]:
    """The immutable per-run snapshot written into `state["session_pair"]`."""
    binding = {"pair_id": entry["pair_id"], "version": entry["version"], "name": entry["name"], "fingerprint": entry_fingerprint(entry),
               "source": entry["source"], "identities": {p: dict(entry["identities"][p]) for p in PROVIDERS},
               "context": dict(entry["context"]), "bound_at": bound_at}
    binding["identity_digest"] = identity_digest(binding)
    return binding


def validate_binding(binding: Any) -> dict[str, Any]:
    keys = {"pair_id", "version", "name", "fingerprint", "source", "identities", "context", "bound_at", "identity_digest"}
    if not isinstance(binding, dict) or set(binding) != keys or not _uuid(binding["pair_id"]) or binding["version"] != 1 or type(binding["version"]) is not int:
        raise SupervisorError("run session pair binding is invalid")
    if binding["source"] not in SOURCES or type(binding["bound_at"]) is not str or normalize_name(binding["name"]) != binding["name"] or not _hex64(binding["fingerprint"]):
        raise SupervisorError("run session pair binding is invalid")
    validate_identities(binding["identities"])
    validate_context_shape(binding["context"])
    if binding["identity_digest"] != identity_digest(binding):
        raise SupervisorError("run session pair binding does not match its recorded sessions and context")
    return binding


def binding_view(binding: dict[str, Any]) -> str:
    """One bounded human line: the label, both identities and the context (never a path from the label)."""
    ids = binding["identities"]
    ctx = binding["context"]
    return (f"{binding['name']} · claude {ids['claude']['session_id'][:8]} ({ids['claude']['pane_id']}) · codex {ids['codex']['session_id'][:8]} ({ids['codex']['pane_id']})"
            f"\nRepository: {ctx['repository']}\nReview base: {ctx['review_base']}")


# ----- registry

def registry_path(paths: Any) -> Path:
    return paths.state_dir / "session-pairs.json"


def registry_lock_path(paths: Any) -> Path:
    return paths.state_dir / "session-pairs.lock"


@contextlib.contextmanager
def registry_lock(paths: Any) -> Iterator[None]:
    """Blocking exclusive lock held only around one registry read-modify-write (taken after, never before, the
    transaction lock; never held across a Herdr call)."""
    paths.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    with registry_lock_path(paths).open("a+", encoding="utf-8") as handle:
        os.chmod(registry_lock_path(paths), 0o600)
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _empty() -> dict[str, Any]:
    return {"schema_version": REGISTRY_SCHEMA, "revision": 0, "entries": {}, "reservations": {}}


def load_registry(paths: Any) -> dict[str, Any]:
    """Validated registry document. An absent file is an empty registry; a malformed one fails closed (never rewritten)."""
    path = registry_path(paths)
    if not path.exists():
        return _empty()
    if path.is_symlink() or not path.is_file():
        raise SupervisorError("pair registry must be a regular file")
    doc = load_json(path, label="named pair registry")
    if not isinstance(doc, dict) or set(doc) != {"schema_version", "revision", "entries", "reservations"} or doc["schema_version"] != REGISTRY_SCHEMA:
        raise SupervisorError("named pair registry has an unsupported shape")
    if type(doc["revision"]) is not int or doc["revision"] < 0 or not isinstance(doc["entries"], dict) or not isinstance(doc["reservations"], dict):
        raise SupervisorError("named pair registry has an unsupported shape")
    if len(doc["entries"]) + len(doc["reservations"]) > MAX_PAIRS:
        raise SupervisorError("named pair registry exceeds its entry bound")
    keys: set[str] = set()
    sessions: set[str] = set()
    for pair_id, entry in doc["entries"].items():
        validate_entry(entry)
        if entry["pair_id"] != pair_id or entry["name_key"] in keys:
            raise SupervisorError("named pair registry has an inconsistent or duplicate entry")
        keys.add(entry["name_key"])
        for provider in PROVIDERS:
            if entry["identities"][provider]["session_id"] in sessions:
                raise SupervisorError("named pair registry reuses a native session")
            sessions.add(entry["identities"][provider]["session_id"])
    for key, reservation in doc["reservations"].items():
        if (not isinstance(reservation, dict) or set(reservation) != {"name", "preparation_id", "binding", "reserved_at"} or key in keys
                or key != name_key(normalize_name(reservation["name"])) or not _uuid(reservation["preparation_id"]) or not _hex64(reservation["binding"])):
            raise SupervisorError("named pair registry has an invalid reservation")
        keys.add(key)
    return doc


def _store(paths: Any, doc: dict[str, Any], clock: Callable[[], float]) -> None:
    doc["revision"] += 1
    atomic_write_json(registry_path(paths), doc, mode=0o600)


def list_entries(paths: Any) -> list[dict[str, Any]]:
    doc = load_registry(paths)
    return sorted(doc["entries"].values(), key=lambda e: (e["name_key"], e["pair_id"]))


def get_entry(paths: Any, pair_id: Any) -> dict[str, Any] | None:
    if not _uuid(pair_id):
        return None
    return load_registry(paths)["entries"].get(pair_id)


def known_sessions(paths: Any) -> set[str]:
    """Every native session id that a saved pair owns (used to keep queries, helpers and new pairs off them).

    An unreadable or malformed registry raises: an unknown protected set must never read as an empty one."""
    doc = load_registry(paths)
    return {e["identities"][p]["session_id"] for e in doc["entries"].values() for p in PROVIDERS}


def _guard_new(doc: dict[str, Any], name: str, identities: dict[str, dict[str, str]] | None) -> None:
    if len(doc["entries"]) + len(doc["reservations"]) >= MAX_PAIRS:
        raise SupervisorError(f"the pair registry already holds {MAX_PAIRS} pairs")
    key = name_key(name)
    if key in doc["reservations"] or any(e["name_key"] == key for e in doc["entries"].values()):
        raise SupervisorError("a saved pair or an in-progress creation already uses that name")
    if identities is not None:
        taken = {e["identities"][p]["session_id"] for e in doc["entries"].values() for p in PROVIDERS}
        if any(identities[p]["session_id"] in taken for p in PROVIDERS):
            raise SupervisorError("a native session of this pair is already saved in another pair")


def register_existing(paths: Any, config: dict[str, Any], clock: Callable[[], float], *, name: str, context: dict[str, str], identities: dict[str, dict[str, str]],
                      pair_id: str | None = None) -> dict[str, Any]:
    """Save two verified existing sessions as one pair. Idempotent for the same pair id (a replayed start); a different
    request with a taken name or an already-saved session is refused. Callers verify the live sessions first."""
    name = normalize_name(name)
    ctx = check_context(config, context)
    ids = validate_identities(identities)
    new_id = pair_id or str(uuid.uuid4())
    with registry_lock(paths):
        doc = load_registry(paths)
        prior = doc["entries"].get(new_id) if pair_id else None
        if prior is not None:
            if prior["name"] != name or prior["identities"] != ids or prior["context"] != ctx:
                raise SupervisorError("this pair registration was already committed with different content")
            return prior
        _guard_new(doc, name, ids)
        entry = {"pair_id": new_id, "version": 1, "name": name, "name_key": name_key(name), "source": "existing", "identities": ids,
                 "context": ctx, "created_at": iso_utc(clock())}
        doc["entries"][new_id] = validate_entry(entry)
        _store(paths, doc, clock)
        return entry


def reserve_name(paths: Any, clock: Callable[[], float], *, name: str, preparation_id: str, binding: str) -> dict[str, Any]:
    """The name belongs to exactly one creation request (its preparation id + selection digest) until it is published or
    released. A replay of the same request is idempotent; any other request for the name is refused."""
    name = normalize_name(name)
    if not _uuid(preparation_id) or not _hex64(binding):
        raise SupervisorError("pair name reservation is invalid")
    key = name_key(name)
    with registry_lock(paths):
        doc = load_registry(paths)
        prior = doc["reservations"].get(key)
        if prior is not None:
            if prior["preparation_id"] == preparation_id and prior["binding"] == binding:
                return prior
            raise SupervisorError("that pair name is reserved by another creation request")
        published = next((e for e in doc["entries"].values() if e.get("preparation_id") == preparation_id), None)
        if published is not None:
            return {"name": published["name"], "preparation_id": preparation_id, "binding": binding, "reserved_at": published["created_at"], "published": True}
        _guard_new(doc, name, None)
        reservation = {"name": name, "preparation_id": preparation_id, "binding": binding, "reserved_at": iso_utc(clock())}
        doc["reservations"][key] = reservation
        _store(paths, doc, clock)
        return reservation


def reservation_for(paths: Any, preparation_id: str) -> dict[str, Any] | None:
    doc = load_registry(paths)
    return next((r for r in doc["reservations"].values() if r["preparation_id"] == preparation_id), None)


def entry_for_preparation(paths: Any, preparation_id: str) -> dict[str, Any] | None:
    doc = load_registry(paths)
    return next((e for e in doc["entries"].values() if e.get("preparation_id") == preparation_id), None)


def publish_created(paths: Any, config: dict[str, Any], clock: Callable[[], float], *, preparation_id: str, name: str, context: dict[str, str],
                    identities: dict[str, dict[str, str]]) -> dict[str, Any]:
    """Convert the reservation of this exact creation request into the saved pair (one atomic registry write). Called only
    after both fresh native identities are verified. Replaying after a crash returns the same entry; a mismatch refuses."""
    name = normalize_name(name)
    ctx = validate_context_shape(context)
    ids = validate_identities(identities)
    key = name_key(name)
    with registry_lock(paths):
        doc = load_registry(paths)
        done = next((e for e in doc["entries"].values() if e.get("preparation_id") == preparation_id), None)
        if done is not None:
            if done["name"] != name or done["identities"] != ids or done["context"] != ctx:
                raise SupervisorError("the pair for this preparation was already published with different content")
            return done
        reservation = doc["reservations"].get(key)
        if reservation is None or reservation["preparation_id"] != preparation_id:
            raise SupervisorError("the pair name is not reserved by this creation request")
        taken = {e["identities"][p]["session_id"] for e in doc["entries"].values() for p in PROVIDERS}
        if any(ids[p]["session_id"] in taken for p in PROVIDERS):
            raise SupervisorError("a fresh native session is already saved in another pair")
        pair_id = str(uuid.uuid5(uuid.UUID(preparation_id), "named-pair"))
        entry = {"pair_id": pair_id, "version": 1, "name": name, "name_key": key, "source": "created", "identities": ids, "context": ctx,
                 "created_at": iso_utc(clock()), "preparation_id": preparation_id}
        del doc["reservations"][key]
        doc["entries"][pair_id] = validate_entry(entry)
        _store(paths, doc, clock)
        return entry


def release_reservation(paths: Any, clock: Callable[[], float], preparation_id: str) -> bool:
    """Drop the reservation of an abandoned creation request (never an entry; entries are immutable)."""
    with registry_lock(paths):
        doc = load_registry(paths)
        for key, reservation in list(doc["reservations"].items()):
            if reservation["preparation_id"] == preparation_id:
                del doc["reservations"][key]
                _store(paths, doc, clock)
                return True
    return False


# ----- read-only live checks (identical for the card, the start and the first delivery)

def live_problems(live: list[dict[str, Any]], identities: dict[str, dict[str, str]], *, settled: bool) -> list[str]:
    """Every reason a pair is not exactly present now: missing, duplicated, carried by another provider, moved, or (when
    `settled`) not idle. Nothing is substituted, launched, renamed or rebound."""
    problems: list[str] = []
    for provider in PROVIDERS:
        want = identities[provider]
        match = match_exact_session(live, provider, want["session_id"])
        label = f"{provider} {want['session_id'][:8]}"
        if match.ambiguous:
            problems.append(f"{label} is live in more than one pane")
        elif match.provider_conflicts:
            problems.append(f"{label} is carried by a record of another provider")
        elif match.record is None:
            problems.append(f"{label} is not live")
        elif match.record.get("pane_id") != want["pane_id"]:
            problems.append(f"{label} moved from {want['pane_id']} to {match.record.get('pane_id')}")
        elif settled and match.record.get("agent_status") not in READY_STATES:
            problems.append(f"{label} is {match.record.get('agent_status')!r}, not settled")
        elif session_identity(match.record) != want["session_id"]:
            problems.append(f"{label} identity changed")
    return problems


def terminal_boundary_problem(state: dict[str, Any] | None) -> str | None:
    """No current run, or a DONE/CANCELLED run with its worker released and nothing unresolved. Anything else (WAIT_*, PAUSED,
    quota, ERROR, a delivery/follow-up/reset/recovery in flight) refuses switching and changes nothing. A gate left 'pending' on a DONE/CANCELLED run
    is history, not a live decision: no control can act on it any more."""
    if state is None:
        return None
    if state.get("supervisor_state") not in ("DONE", "CANCELLED"):
        return f"the current task is {state.get('supervisor_state')}; pairs can only be switched after it is DONE or CANCELLED"
    pid = state.get("worker_pid")
    if pid is not None and pid != os.getpid():  # this very process is the one deciding, under its locks: its own record is not a second worker
        try:
            os.kill(int(pid), 0)
        except (OSError, ValueError, TypeError):
            pass
        else:
            return "the previous task's worker is still running"
    delivery = state.get("delivery")
    if isinstance(delivery, dict) and delivery.get("status") in ("prepared", "accepted", "uncertain", "interrupted"):
        return "a delivery of the previous task is unresolved"
    if isinstance(state.get("interrupted_delivery"), dict) or state.get("continuation") is not None or state.get("owner_recovery") is not None:
        return "the previous task has an unresolved recovery"
    followup = state.get("agent_followup")
    if isinstance(followup, dict) and followup.get("status") in FOLLOWUP_UNRESOLVED:
        return "the previous task has an unresolved follow-up"
    if (state.get("codex_reset") or {}).get("current_redemption_state") not in (None, "IDLE", "RESET_VERIFIED"):
        return "the previous task has unresolved reset state"
    return None


def preserve_terminal_snapshot(paths: Any, state: dict[str, Any]) -> bool:
    """Keep a finished run's state by run id before the single current state file is replaced (write-once, 0600)."""
    run_id = state.get("run_id")
    if not _uuid(run_id) or state.get("supervisor_state") not in ("DONE", "CANCELLED"):
        return False
    path = paths.state_dir / "run-history" / f"{run_id}.json"
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    atomic_write_json(path, {key: value for key, value in state.items() if not key.startswith("_")}, mode=0o600)
    return True


def _ownership_record(entry: Any, label: str, *, provider: str | None = None) -> str:
    """One supported ownership record, validated EXACTLY: an object whose native `session_id` is a non-empty text, whose `pane_id` (when present) is text or null and
    whose `provider`/`agent_name` (when present) are text. Anything else raises: ownership of that record cannot be established, so it must not be filtered out."""
    if (not isinstance(entry, dict) or type(entry.get("session_id")) is not str or not entry["session_id"]
            or (entry.get("pane_id") is not None and type(entry["pane_id"]) is not str)
            or any(key in entry and type(entry[key]) is not str for key in ("provider", "agent_name"))):
        raise SupervisorError(f"a {label} record is malformed; ownership of that session is unknown")
    return entry["session_id"]


def foreign_sessions(paths: Any, *, helpers: bool = True) -> set[str]:
    """Native sessions a NEW pair must not take: every saved pair's sessions, the registered query sessions and (unless `helpers=False`) the registered helper
    workers (read-only). Every record of every supported registry is validated exactly; any registry or record that exists but cannot be read exactly raises:
    uncertainty about ownership refuses, it never reads as 'not taken'. A valid empty registry is simply empty. Nothing is repaired or rewritten."""
    taken = set(known_sessions(paths))
    if paths.query_registry_file.exists():
        value = load_json(paths.query_registry_file, label="query provider registry")
        providers = value.get("providers") if isinstance(value, dict) and value.get("schema_version") == 1 else None
        if not isinstance(providers, dict) or any(key not in PROVIDERS for key in providers):
            raise SupervisorError("the query provider registry is malformed; ownership is unknown")
        taken.update(_ownership_record(entry, "query provider") for entry in providers.values())
    if paths.query_owner_file.exists():
        value = load_json(paths.query_owner_file, label="query owner")
        taken.add(_ownership_record(value, "legacy query owner"))
    registry = paths.state_dir / "helpers" / "registry.json"
    if helpers and registry.exists():
        value = load_json(registry, label="helper registry")
        workers = value.get("workers") if isinstance(value, dict) and value.get("schema_version") == 1 else None
        if not isinstance(workers, dict):
            raise SupervisorError("the helper registry is malformed; ownership is unknown")
        taken.update(_ownership_record(entry, "helper worker") for entry in workers.values())
    return taken
