"""Shared installation logic for `herdr-setup`: a verified payload inventory, immutable program generations, one
atomic active pointer, a durable operation journal with a resumable step runner, quiescence checks, and the
command/service adapters the setup and update modules use.

Design rules (see docs/INSTALL.md):

* A payload is a reviewed local source or release tree. It is read once into memory through an explicit
  allowlist (never a glob over the tree), every file is hashed from those bytes, and the same bytes are what is
  published. Nothing in the tree is imported or executed while it is inspected.
* A program generation is a complete immutable directory `generations/<id>/`. The single active pointer is the
  symlink `active -> generations/<id>`; it is switched with one rename, and launchers resolve it exactly once
  per invocation, so a process never imports a mixed release.
* Files outside a generation (launchers, units, docs, examples) are owned only when enumerated by the payload.
  They are backed up before they change and restored from that backup on rollback.
* Nothing here talks to a network, a provider or a Telegram bot. Every external command goes through `Host`.
"""

from __future__ import annotations

import ast
import contextlib
import dataclasses
import fcntl
import getpass
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import herdr_core as core
from herdr_validation import StateStore

MANAGER_SCHEMA = 1
EXIT_OK = 0
EXIT_USAGE = 2
EXIT_ACTION = 3  # something remains for a human (prerequisite, credential, session, privilege)
EXIT_REFUSED = 4  # a safety condition holds: nothing was changed
EXIT_RECONCILING = 5  # an outcome is unknown or an operation is unfinished: nothing is repeated blindly

MAX_FILES = 256
MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_TOTAL_BYTES = 32 * 1024 * 1024
MAX_JSON_BYTES = 4 * 1024 * 1024
ROLES = ("program", "launcher", "unit", "doc", "example")
INSTALL_MODE = {"program": 0o644, "launcher": 0o755, "unit": 0o644, "doc": 0o644, "example": 0o644}
_MODULE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}\.py$")
_LAUNCHER_RE = re.compile(r"^herdr-[a-z0-9]+(?:-[a-z0-9]+){0,5}$")
_UNIT_RE = re.compile(r"^herdr-[a-z0-9]+(?:-[a-z0-9]+){0,6}\.(?:service|path|timer)$")
_DOC_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}\.md$")
_EXAMPLE_RE = re.compile(r"^[a-z][a-z0-9-]{0,63}\.example\.json$")
_VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:-(?:alpha|beta|rc)\.[0-9]+)?$")
_GENERATION_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._-]{0,79}$")
_ID_RE = re.compile(r"^[0-9a-f]{32}$")
ROOT_DOCS = ("README.md", "LICENSE", "VERSION", "MANIFEST.txt")
QUOTA_LOCK = "quota-dependency.json"
MANAGER_MODULES = ("herdr_installation.py", "herdr_setup.py", "herdr_update.py")
REQUIRED_PROGRAM = ("herdr_supervisor.py", "herdr_core.py")
UNIT_FOR_SERVICE = {
    "worker": "herdr-supervisor-worker.path",
    "telegram": "herdr-telegram.service",
    "query": "herdr-query-worker.path",
    "reset": "herdr-codex-reset-worker.path",
    "backup": "herdr-backup.timer",
    "server": "herdr-server.service",
}
_IGNORED_DIRS = {"src": {"__pycache__"}, "docs": {"images"}}
# Program-directory paths that predate generations and are documented in user-owned provider configuration (the Codex
# SessionStart hook). They stay valid as links to the active generation, which resolves the same way a launcher does.
COMPAT_LINKS = ("herdr_native_identity.py",)


class InstallError(Exception):
    """A refusal or failure with the next action a person can take. `code` is the process exit status."""

    def __init__(self, message: str, *, code: int = EXIT_REFUSED, action: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.action = action


class Crash(BaseException):
    """Raised only by test fault injection to model a process that dies mid-step; never caught by the runner."""


# ------------------------------------------------------------------------------------------------ hashing


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_hash(value: Any) -> str:
    return sha256_bytes(core.canonical_json(value))


def fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_file_atomic(path: Path, data: bytes, mode: int) -> None:
    """Replace `path` with `data` in one rename. A symlink or directory at the target is refused by the caller."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
        fsync_dir(path.parent)
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()


def create_file_exclusive(path: Path, data: bytes, mode: int) -> bool:
    """Create `path` only when absent (O_EXCL, never through a symlink). Returns False when it already exists."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    except FileExistsError:
        return False
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    fsync_dir(path.parent)
    return True


def ensure_private_dir(path: Path) -> None:
    """Create `path` (mode 0700) when absent. Missing ancestors get ordinary modes; an existing directory is left exactly as it is."""
    if os.path.lexists(path):
        if not path.is_dir():
            raise InstallError(f"{path} exists and is not a directory")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    os.mkdir(path, 0o700)
    os.chmod(path, 0o700)


def describe(path: Path) -> dict[str, Any]:
    """The exact current state of a target path, never following a symlink."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return {"state": "absent"}
    except OSError as error:
        return {"state": "unsafe", "kind": f"unreadable ({error.strerror})"}
    if stat.S_ISLNK(info.st_mode):
        return {"state": "unsafe", "kind": "symlink"}
    if stat.S_ISDIR(info.st_mode):
        return {"state": "unsafe", "kind": "directory"}
    if not stat.S_ISREG(info.st_mode):
        return {"state": "unsafe", "kind": "special file"}
    if info.st_size > MAX_FILE_BYTES:
        return {"state": "unsafe", "kind": "oversized file"}
    try:
        data = read_bytes_nofollow(path)
    except OSError as error:
        return {"state": "unsafe", "kind": f"unreadable ({error.strerror})"}
    return {"state": "file", "sha256": sha256_bytes(data), "mode": stat.S_IMODE(info.st_mode)}


def read_bytes_nofollow(path: Path, limit: int = MAX_FILE_BYTES) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise OSError("not a regular file")
        with os.fdopen(os.dup(fd), "rb") as handle:
            data = handle.read(limit + 1)
    finally:
        os.close(fd)
    if len(data) > limit:
        raise OSError("file exceeds the size limit")
    return data


# ------------------------------------------------------------------------------------------------ versions


def version_key(version: str) -> tuple[int, int, int, int, int]:
    """Orderable key for a normalized or release-style version; a final release sorts after its pre-releases."""
    text = core.normalize_version(version)
    match = re.fullmatch(r"([0-9]+)\.([0-9]+)\.([0-9]+)(?:(a|b|rc)([0-9]+))?", text)
    if match is None:
        raise InstallError(f"unsupported version {version!r}")
    rank = {"a": 0, "b": 1, "rc": 2, None: 3}[match.group(4)]
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)), rank, int(match.group(5) or 0))


# ------------------------------------------------------------------------------------------------ host adapter


@dataclasses.dataclass
class CmdResult:
    rc: int
    out: str = ""
    err: str = ""
    timed_out: bool = False


class Host:
    """The only door to external commands. Tests replace it with a recording fake; nothing else spawns a process."""

    def run(self, argv: list[str], *, timeout: float = 60, env: dict[str, str] | None = None, cwd: Path | None = None) -> CmdResult:
        try:
            done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False, env=env, cwd=cwd, stdin=subprocess.DEVNULL)
        except FileNotFoundError:
            return CmdResult(127, "", f"{argv[0]}: command not found")
        except subprocess.TimeoutExpired as error:
            def text(value: Any) -> str:
                return value.decode("utf-8", "replace") if isinstance(value, bytes) else (value or "")
            return CmdResult(124, text(error.stdout), text(error.stderr), timed_out=True)
        except OSError as error:
            return CmdResult(126, "", str(error))
        return CmdResult(done.returncode, done.stdout, done.stderr)

    def which(self, name: str) -> str | None:
        return shutil.which(name)


@dataclasses.dataclass
class Env:
    layout: Layout
    host: Host = dataclasses.field(default_factory=Host)
    clock: Callable[[], float] = time.time
    fault: Callable[[str], None] | None = None
    lock_wait: float = 30.0
    worker_lock_held: bool = False  # True while this process holds the worker lock, so planning does not mistake it for a running worker

    def at(self, point: str) -> None:
        if self.fault is not None:
            self.fault(point)


# ------------------------------------------------------------------------------------------------ layout


@dataclasses.dataclass(frozen=True)
class Layout:
    """Every location the manager reads or writes. Environment overrides match the ones the program itself honors."""

    home: Path
    lib: Path
    bin: Path
    unit: Path
    doc: Path
    sup_config: Path
    backup_config: Path
    tg_config: Path
    state: Path
    tg_state: Path
    tg_locks: Path

    @classmethod
    def from_environment(cls, home: Path | None = None) -> Layout:
        home = Path(home) if home is not None else Path.home()
        sup_config = Path(os.environ.get("HERDR_SUPERVISOR_CONFIG", home / ".config/herdr-supervisor/config.json"))
        return cls(
            home=home,
            lib=Path(os.environ.get("HERDR_SUPERVISOR_LIB", home / ".local/lib/herdr-supervisor")),
            bin=home / ".local/bin",
            unit=home / ".config/systemd/user",
            doc=home / ".local/share/doc/herdr-supervisor",
            sup_config=sup_config,
            backup_config=sup_config.parent / "backup.json",
            tg_config=Path(os.environ.get("HERDR_TELEGRAM_CONFIG", home / ".config/herdr-telegram/config.json")),
            state=Path(os.environ.get("HERDR_SUPERVISOR_STATE_DIR", home / ".local/state/herdr-supervisor")),
            tg_state=Path(os.environ.get("HERDR_TELEGRAM_STATE_DIR", home / ".local/state/herdr-telegram")),
            tg_locks=home / ".local/state/herdr-telegram-locks",
        )

    @property
    def paths(self) -> core.Paths:
        return core.Paths(config_file=self.sup_config, state_dir=self.state)

    @property
    def manager(self) -> Path:
        return self.state / "installation"

    @property
    def generations(self) -> Path:
        return self.lib / "generations"

    @property
    def active(self) -> Path:
        return self.lib / "active"

    @property
    def receipt(self) -> Path:
        return self.manager / "receipt.json"

    @property
    def deploy_lock(self) -> Path:
        return self.manager / "deploy.lock"

    @property
    def ops(self) -> Path:
        return self.manager / "ops"

    @property
    def backups(self) -> Path:
        return self.manager / "backups"

    @property
    def examples(self) -> Path:
        return self.doc / "examples"

    def destination(self, role: str, name: str) -> Path:
        return {"launcher": self.bin, "unit": self.unit, "doc": self.doc, "example": self.examples}[role] / name

    def identity(self) -> dict[str, Any]:
        """Resolved target identity that an approval is bound to: a swapped symlink or a changed override invalidates it."""
        def real(path: Path) -> str:
            return str(path.resolve(strict=False))
        return {
            "uid": os.getuid(), "home": real(self.home), "lib": real(self.lib), "bin": real(self.bin), "unit": real(self.unit), "doc": real(self.doc),
            "sup_config": real(self.sup_config), "backup_config": real(self.backup_config), "tg_config": real(self.tg_config),
            "state": real(self.state), "tg_state": real(self.tg_state), "tg_locks": real(self.tg_locks),
        }


# ------------------------------------------------------------------------------------------------ payload


@dataclasses.dataclass(frozen=True)
class PayloadFile:
    role: str
    name: str
    src: str
    sha256: str
    size: int
    mode: int

    @property
    def key(self) -> str:
        return f"{self.role}/{self.name}"

    def entry(self) -> dict[str, Any]:
        return {"role": self.role, "name": self.name, "sha256": self.sha256, "size": self.size, "mode": self.mode}


@dataclasses.dataclass(eq=False)
class Payload:
    root: Path
    version: str
    files: tuple[PayloadFile, ...]
    blobs: dict[str, bytes]
    state_schema_version: int
    source: dict[str, Any]

    def of_role(self, role: str) -> list[PayloadFile]:
        return [item for item in self.files if item.role == role]

    @property
    def payload_id(self) -> str:
        return canonical_hash({"version": self.version, "files": [item.entry() for item in self.files]})

    @property
    def program_id(self) -> str:
        return canonical_hash({"version": self.version, "state_schema_version": self.state_schema_version, "files": [item.entry() for item in self.of_role("program")]})

    @property
    def generation_id(self) -> str:
        return f"{self.version}-{self.program_id[:16]}"

    def summary(self) -> dict[str, Any]:
        return {"version": self.version, "payload_id": self.payload_id, "program_id": self.program_id, "generation_id": self.generation_id,
                "state_schema_version": self.state_schema_version, "source": self.source, "files": {role: len(self.of_role(role)) for role in ROLES}}


def _read_source(path: Path, label: str) -> tuple[bytes, int]:
    try:
        before = os.lstat(path)
    except OSError as error:
        raise InstallError(f"payload file {label} is not readable: {error.strerror}") from error
    if stat.S_ISLNK(before.st_mode):
        raise InstallError(f"payload entry {label} is a symbolic link; links are not accepted")
    if not stat.S_ISREG(before.st_mode):
        raise InstallError(f"payload entry {label} is not a regular file")
    if before.st_nlink != 1:
        raise InstallError(f"payload file {label} has more than one hard link")
    if before.st_mode & 0o7000 or before.st_mode & 0o002:
        raise InstallError(f"payload file {label} has unsafe permission bits")
    if before.st_size > MAX_FILE_BYTES:
        raise InstallError(f"payload file {label} exceeds {MAX_FILE_BYTES} bytes")
    try:
        data = read_bytes_nofollow(path)
        after = os.lstat(path)
    except OSError as error:
        raise InstallError(f"payload file {label} could not be read safely: {error}") from error
    if (after.st_ino, after.st_dev) != (before.st_ino, before.st_dev) or len(data) != before.st_size:
        raise InstallError(f"payload file {label} changed while it was read")
    return data, stat.S_IMODE(before.st_mode)


def _list_dir(root: Path, relative: str, pattern: re.Pattern[str], role: str, *, ignored: frozenset[str] = frozenset()) -> list[tuple[str, str, Path]]:
    directory = root / relative
    try:
        info = os.lstat(directory)
    except FileNotFoundError:
        raise InstallError(f"payload is missing the {relative}/ directory") from None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise InstallError(f"payload {relative}/ must be a real directory")
    found: list[tuple[str, str, Path]] = []
    with os.scandir(directory) as entries:
        for entry in sorted(entries, key=lambda item: item.name):
            label = f"{relative}/{entry.name}"
            if entry.is_symlink():
                raise InstallError(f"payload entry {label} is a symbolic link; links are not accepted")
            if entry.is_dir(follow_symlinks=False):
                if entry.name in ignored or (relative == "src" and entry.name.endswith(".egg-info")):  # build metadata, never part of a payload
                    continue
                raise InstallError(f"payload entry {label} is an unsupported directory")
            if not entry.is_file(follow_symlinks=False):
                raise InstallError(f"payload entry {label} is a special file")
            if not pattern.fullmatch(entry.name):
                raise InstallError(f"payload entry {label} is not an allowed {role} name")
            found.append((role, entry.name, Path(entry.path)))
    return found


def _declared_constants(source: bytes) -> dict[str, Any]:
    """Read two literal constants from herdr_core.py by parsing it, never by importing or executing it."""
    try:
        tree = ast.parse(source.decode("utf-8"))
    except (SyntaxError, UnicodeDecodeError, ValueError) as error:
        raise InstallError("payload herdr_core.py does not parse") from error
    wanted = {"SUPERVISOR_VERSION": str, "STATE_SCHEMA_VERSION": int}
    found: dict[str, Any] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name) and node.targets[0].id in wanted:
            if isinstance(node.value, ast.Constant) and type(node.value.value) is wanted[node.targets[0].id]:
                found[node.targets[0].id] = node.value.value
    if set(found) != set(wanted):
        raise InstallError("payload herdr_core.py does not declare SUPERVISOR_VERSION and STATE_SCHEMA_VERSION as literals")
    return found


def _git_head(root: Path) -> str | None:
    """Best-effort source identity read from plain files; no git process is started in an untrusted tree."""
    try:
        git = root / ".git"
        if git.is_symlink() or not git.is_dir():
            return None
        head = (git / "HEAD").read_text(encoding="utf-8").strip()
        if re.fullmatch(r"[0-9a-f]{40}", head):
            return head
        if head.startswith("ref: refs/") and ".." not in head:
            ref = head[5:]
            with contextlib.suppress(OSError):
                value = (git / ref).read_text(encoding="utf-8").strip()
                if re.fullmatch(r"[0-9a-f]{40}", value):
                    return value
            for line in (git / "packed-refs").read_text(encoding="utf-8").splitlines():
                parts = line.split(" ")
                if len(parts) == 2 and parts[1] == ref and re.fullmatch(r"[0-9a-f]{40}", parts[0]):
                    return parts[0]
    except (OSError, UnicodeDecodeError):
        return None
    return None


def load_payload(root: Path, *, require_manager: bool = True) -> Payload:
    """Read and verify a source or release tree. Raises InstallError before anything is interpreted or written."""
    root = Path(root)
    try:
        info = os.lstat(root)
    except OSError as error:
        raise InstallError(f"payload directory {root} is not readable: {error.strerror}", action="pass --payload DIR with a reviewed source or release tree") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise InstallError(f"payload {root} must be a real directory, not a link or file")
    listed: list[tuple[str, str, Path]] = []
    listed += _list_dir(root, "src", _MODULE_RE, "program", ignored=frozenset(_IGNORED_DIRS["src"]))
    listed += _list_dir(root, "bin", _LAUNCHER_RE, "launcher")
    listed += _list_dir(root, "systemd", _UNIT_RE, "unit")
    listed += _list_dir(root, "docs", _DOC_RE, "doc", ignored=frozenset(_IGNORED_DIRS["docs"]))
    config_dir = root / "config"
    configs = []
    with contextlib.suppress(FileNotFoundError):
        with os.scandir(config_dir) as entries:
            configs = sorted(entry.name for entry in entries)
    for name in configs:
        if name == QUOTA_LOCK:
            listed.append(("program", name, config_dir / name))
        elif _EXAMPLE_RE.fullmatch(name):
            listed.append(("example", name, config_dir / name))
        else:
            raise InstallError(f"payload entry config/{name} is not an allowed configuration file")
    for name in ROOT_DOCS:
        listed.append(("doc", name, root / name))
    if len(listed) > MAX_FILES:
        raise InstallError(f"payload lists more than {MAX_FILES} files")
    blobs: dict[str, bytes] = {}
    files: list[PayloadFile] = []
    total = 0
    seen: dict[str, str] = {}
    for role, name, path in listed:
        label = str(path.relative_to(root))
        data, _mode = _read_source(path, label)
        total += len(data)
        if total > MAX_TOTAL_BYTES:
            raise InstallError(f"payload exceeds {MAX_TOTAL_BYTES} bytes")
        key = f"{role}/{name}"
        folded = key.casefold()
        if key in blobs or folded in seen:
            raise InstallError(f"payload names {label} twice (duplicate or case-colliding destination)")
        seen[folded] = key
        blobs[key] = data
        files.append(PayloadFile(role=role, name=name, src=label, sha256=sha256_bytes(data), size=len(data), mode=INSTALL_MODE[role]))
    files.sort(key=lambda item: (ROLES.index(item.role), item.name))
    present = {item.key for item in files}
    for required in REQUIRED_PROGRAM:
        if f"program/{required}" not in present:
            raise InstallError(f"payload has no src/{required}")
    if require_manager:
        # A current managed candidate must carry the dependency lock. A reviewed pre-manager tree (inspected only to adopt an earlier flat
        # installation) predates it and legitimately has none; its identity is still verified byte for byte against the installed files.
        if f"program/{QUOTA_LOCK}" not in present:
            raise InstallError(f"payload has no config/{QUOTA_LOCK}")
        for required in MANAGER_MODULES:
            if f"program/{required}" not in present:
                raise InstallError(f"payload has no src/{required}; it cannot manage installations")
        if "launcher/herdr-setup" not in present:
            raise InstallError("payload has no bin/herdr-setup launcher")
    constants = _declared_constants(blobs["program/herdr_core.py"])
    version = blobs["doc/VERSION"].decode("utf-8", "replace").strip()
    if not _VERSION_RE.fullmatch(version):
        raise InstallError(f"payload VERSION {version!r} is not a supported release version")
    if version != constants["SUPERVISOR_VERSION"]:
        raise InstallError(f"payload VERSION {version} disagrees with SUPERVISOR_VERSION {constants['SUPERVISOR_VERSION']} in herdr_core.py")
    head = _git_head(root)
    source = {"kind": "git", "head": head} if head else {"kind": "tree"}
    return Payload(root=root, version=version, files=tuple(files), blobs=blobs, state_schema_version=int(constants["STATE_SCHEMA_VERSION"]), source=source)


def default_payload_root() -> Path | None:
    """The checkout or release tree this module was started from, when it has one (a wheel alone does not)."""
    here = Path(__file__).resolve().parent
    for candidate in (here.parent, here):
        if (candidate / "systemd").is_dir() and (candidate / "bin").is_dir() and (candidate / "src").is_dir():
            return candidate
    return None


def tool_identity() -> str:
    """Content hash of the running tool's own modules: an approval previewed by one tool is not applied by another."""
    here = Path(__file__).resolve().parent
    entries = []
    for path in sorted(here.glob("*.py")):
        with contextlib.suppress(OSError):
            entries.append((path.name, sha256_bytes(path.read_bytes())))
    return canonical_hash(entries)


# ------------------------------------------------------------------------------------------------ generations


def _generation_manifest(payload: Payload, operation_id: str) -> dict[str, Any]:
    return {
        "schema": MANAGER_SCHEMA, "generation_id": payload.generation_id, "version": payload.version, "payload_id": payload.payload_id, "program_id": payload.program_id,
        "state_schema_version": payload.state_schema_version, "source": payload.source, "created_by_operation": operation_id,
        "files": [{"name": item.name, "sha256": item.sha256, "size": item.size, "mode": item.mode} for item in payload.of_role("program")],
    }


_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_ASSET_ROLES = ("launcher", "unit", "doc", "example")


def is_int(value: Any) -> bool:
    return type(value) is int


def is_hex(value: Any, length: int) -> bool:
    return isinstance(value, str) and re.fullmatch(f"[0-9a-f]{{{length}}}", value) is not None


def asset_name_ok(role: str, name: str) -> bool:
    if role == "launcher":
        return _LAUNCHER_RE.fullmatch(name) is not None
    if role == "unit":
        return _UNIT_RE.fullmatch(name) is not None
    if role == "doc":
        return _DOC_RE.fullmatch(name) is not None or name in ROOT_DOCS
    if role == "example":
        return _EXAMPLE_RE.fullmatch(name) is not None
    return False


def verify_generation(directory: Path, *, expect_id: str | None = None, expect_program_id: str | None = None) -> tuple[dict[str, Any] | None, list[str]]:
    """Check a generation directory against its own manifest AND against the identity it is named by: exact manifest types, the
    label `<version>-<program id prefix>` must follow from the recomputed program id (so rewriting a module and its own
    manifest cannot keep the old name), exact file set and hashes, and the read-only modes the publisher set."""
    problems: list[str] = []
    try:
        info = os.lstat(directory)
    except OSError:
        return None, [f"generation directory {directory.name} is missing"]
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        return None, [f"generation {directory.name} is not a real directory"]
    if stat.S_IMODE(info.st_mode) != 0o555:
        problems.append(f"generation {directory.name} is not read-only (mode {oct(stat.S_IMODE(info.st_mode))})")
    try:
        manifest = json.loads(read_bytes_nofollow(directory / "GENERATION.json", MAX_JSON_BYTES))
    except (OSError, ValueError) as error:
        return None, [f"generation {directory.name} has no readable GENERATION.json ({error})"]
    if not isinstance(manifest, dict) or not is_int(manifest.get("schema")) or manifest["schema"] != MANAGER_SCHEMA or not isinstance(manifest.get("files"), list):
        return None, [f"generation {directory.name} has an unsupported manifest"]
    version, schema_version, program_claim = manifest.get("version"), manifest.get("state_schema_version"), manifest.get("program_id")
    if not isinstance(version, str) or not _VERSION_RE.fullmatch(version) or not is_int(schema_version) or not is_hex(program_claim, 64):
        return None, [f"generation {directory.name} manifest has invalid identity fields"]
    expected: dict[str, dict[str, Any]] = {}
    for item in manifest["files"]:
        if (not isinstance(item, dict) or set(item) != {"name", "sha256", "size", "mode"} or not isinstance(item["name"], str) or item["name"] in expected
                or not (_MODULE_RE.fullmatch(item["name"]) or item["name"] == QUOTA_LOCK) or not is_hex(item["sha256"], 64) or not is_int(item["size"]) or not is_int(item["mode"])):
            return None, [f"generation {directory.name} manifest lists an invalid file entry"]
        expected[item["name"]] = item
    gid = manifest.get("generation_id")
    if gid != directory.name or (expect_id is not None and gid != expect_id):
        problems.append(f"generation manifest names {gid!r}, not {expect_id or directory.name}")
    program_id = canonical_hash({"version": version, "state_schema_version": schema_version,
                                 "files": [{"role": "program", "name": i["name"], "sha256": i["sha256"], "size": i["size"], "mode": i["mode"]} for i in sorted(expected.values(), key=lambda x: x["name"])]})
    if program_id != program_claim:
        problems.append(f"generation {directory.name} manifest does not match its own program identity")
    if directory.name != f"{version}-{program_id[:16]}":
        problems.append(f"generation directory name {directory.name} does not follow from its recomputed program identity")
    if expect_program_id is not None and program_id != expect_program_id:
        problems.append(f"generation {directory.name} is not the program identity recorded for it")
    with os.scandir(directory) as entries:
        actual = {entry.name for entry in entries}
    for extra in sorted(actual - set(expected) - {"GENERATION.json"}):
        problems.append(f"generation {directory.name} contains an unexpected entry {extra}")
    for name, item in sorted(expected.items()):
        state = describe(directory / name)
        if state["state"] != "file":
            problems.append(f"generation file {name} is {state.get('kind') or 'missing'}")
        elif state["sha256"] != item["sha256"]:
            problems.append(f"generation file {name} differs from its recorded hash")
        elif state["mode"] != 0o444:
            problems.append(f"generation file {name} is not read-only (mode {oct(state['mode'])})")
    state = describe(directory / "GENERATION.json")
    if state["state"] == "file" and state["mode"] != 0o444:
        problems.append(f"generation manifest is not read-only (mode {oct(state['mode'])})")
    return manifest, problems


def _make_writable(path: Path) -> None:
    for current, dirs, files in os.walk(path):
        os.chmod(current, 0o755)
        for name in files:
            with contextlib.suppress(OSError):
                os.chmod(os.path.join(current, name), 0o644)
        for name in dirs:
            with contextlib.suppress(OSError):
                os.chmod(os.path.join(current, name), 0o755)


def remove_owned_stage(layout: Layout, stage: Path) -> None:
    """Remove one operation's own staging directory; refuses anything that is not a `.stage-*` child of generations/."""
    if stage.parent != layout.generations or not stage.name.startswith(".stage-"):
        raise InstallError(f"refusing to remove {stage}: not an operation staging directory")
    if os.path.lexists(stage) and not stage.is_symlink():
        _make_writable(stage)
        shutil.rmtree(stage)


def publish_generation(env: Env, payload: Payload, operation_id: str) -> str:
    """Stage, verify and rename one complete generation into place. Idempotent: an identical generation is reused."""
    layout = env.layout
    gid = payload.generation_id
    if not _GENERATION_RE.fullmatch(gid):
        raise InstallError(f"generation id {gid!r} is not a safe directory name")
    layout.generations.mkdir(parents=True, exist_ok=True, mode=0o755)
    if layout.generations.is_symlink():
        raise InstallError("the generations directory is a symbolic link")
    final = layout.generations / gid
    if os.path.lexists(final):
        manifest, problems = verify_generation(final, expect_id=gid)
        if problems or manifest is None or manifest.get("program_id") != payload.program_id:
            raise InstallError(f"generation {gid} already exists and does not verify: {'; '.join(problems) or 'different program'}", code=EXIT_REFUSED)
        return gid
    stage = layout.generations / f".stage-{operation_id}"
    remove_owned_stage(layout, stage)
    stage.mkdir(mode=0o755)
    for item in payload.of_role("program"):
        target = stage / item.name
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload.blobs[item.key])
            handle.flush()
            os.fsync(handle.fileno())
    manifest_bytes = json.dumps(_generation_manifest(payload, operation_id), indent=2, sort_keys=True).encode("utf-8") + b"\n"
    fd = os.open(stage / "GENERATION.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    with os.fdopen(fd, "wb") as handle:
        handle.write(manifest_bytes)
        handle.flush()
        os.fsync(handle.fileno())
    # The directory is verified exactly as it will be read, before it can ever be the active one.
    _manifest, problems = verify_generation_stage(stage, gid)
    if problems:
        remove_owned_stage(layout, stage)
        raise InstallError("staged generation failed verification: " + "; ".join(problems))
    for path in stage.iterdir():
        os.chmod(path, 0o444)
    os.chmod(stage, 0o555)
    fsync_dir(stage)
    env.at("generation:staged")
    os.rename(stage, final)
    fsync_dir(layout.generations)
    return gid


def verify_generation_stage(stage: Path, gid: str) -> tuple[dict[str, Any] | None, list[str]]:
    """verify_generation for a staging directory whose name is not yet the generation id."""
    try:
        manifest = json.loads(read_bytes_nofollow(stage / "GENERATION.json", MAX_JSON_BYTES))
    except (OSError, ValueError) as error:
        return None, [f"staged manifest unreadable ({error})"]
    if manifest.get("generation_id") != gid:
        return None, ["staged manifest has the wrong generation id"]
    problems: list[str] = []
    for item in manifest.get("files", []):
        state = describe(stage / item["name"])
        if state["state"] != "file" or state["sha256"] != item["sha256"]:
            problems.append(f"staged {item['name']} does not match its hash")
    return manifest, problems


def read_active(layout: Layout) -> str | None:
    """The active generation id, or None when no pointer exists. A pointer of any other shape is an error."""
    try:
        target = os.readlink(layout.active)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise InstallError(f"{layout.active} exists but is not the active-generation symlink ({error.strerror})") from error
    match = re.fullmatch(r"generations/([0-9A-Za-z][0-9A-Za-z._-]{0,79})", target)
    if match is None:
        raise InstallError(f"the active pointer targets {target!r}, which is not a managed generation")
    return match.group(1)


def switch_active(env: Env, generation_id: str, token: str) -> None:
    """Point `active` at a verified generation with one atomic rename; readers see the old or the new one, never neither."""
    layout = env.layout
    if not _GENERATION_RE.fullmatch(generation_id):
        raise InstallError("invalid generation id")
    manifest, problems = verify_generation(layout.generations / generation_id, expect_id=generation_id)
    if problems or manifest is None:
        raise InstallError("refusing to activate a generation that does not verify: " + "; ".join(problems))
    if os.path.lexists(layout.active) and not layout.active.is_symlink():
        raise InstallError(f"{layout.active} is not a symlink; refusing to replace it")
    tmp = layout.lib / f".active.{token}"
    with contextlib.suppress(FileNotFoundError):
        tmp.unlink()
    os.symlink(f"generations/{generation_id}", tmp)
    os.replace(tmp, layout.active)
    fsync_dir(layout.lib)


def managed_unit_names(layout: Layout) -> list[str]:
    """Every herdr-* unit file present in the user unit directory (workers and watchers alike), by name."""
    try:
        return sorted(entry.name for entry in os.scandir(layout.unit) if _UNIT_RE.fullmatch(entry.name) and entry.is_file(follow_symlinks=False))
    except OSError:
        return []


def compat_link_state(layout: Layout, name: str) -> str:
    """absent | ours (the exact link to the active generation) | other (a file, directory or different link)."""
    path = layout.lib / name
    try:
        target = os.readlink(path)
    except FileNotFoundError:
        return "absent"
    except OSError:
        return "other"
    return "ours" if target == f"active/{name}" else "other"


def ensure_compat_link(layout: Layout, name: str) -> None:
    state = compat_link_state(layout, name)
    if state == "ours":
        return
    if state != "absent" or os.path.lexists(layout.lib / name):
        raise InstallError(f"{layout.lib / name} exists and is not the compatibility link; it was left in place", code=EXIT_REFUSED)
    os.symlink(f"active/{name}", layout.lib / name)
    fsync_dir(layout.lib)


def remove_compat_link(layout: Layout, name: str) -> None:
    """Remove the link only when it is exactly ours; anything else at that path is never touched."""
    if compat_link_state(layout, name) == "ours":
        (layout.lib / name).unlink()
        fsync_dir(layout.lib)


# ------------------------------------------------------------------------------------------------ installation state


@dataclasses.dataclass
class InstallState:
    layout: str  # absent | managed | legacy | unknown | inconsistent
    active_generation: str | None = None
    manifest: dict[str, Any] | None = None
    receipt: dict[str, Any] | None = None
    legacy: dict[str, str] | None = None  # name -> sha256 of a flat legacy program directory
    problems: list[str] = dataclasses.field(default_factory=list)

    def identity(self) -> dict[str, Any]:
        if self.layout == "managed" and self.manifest and self.receipt:
            return {"layout": "managed", "generation": self.active_generation, "program_id": self.manifest.get("program_id"), "operation": self.receipt.get("operation_id"), "receipt": canonical_hash(self.receipt)}
        if self.layout == "legacy" and self.legacy is not None:
            return {"layout": "legacy", "digest": canonical_hash(self.legacy)}
        return {"layout": self.layout, "problems": sorted(self.problems)}


def validate_receipt(layout: Layout, value: Any) -> dict[str, Any]:
    """Exact-type schema for a persisted receipt; every asset must be a canonical destination for this layout, so a receipt
    can never be used to address (and delete or replace) a file the manager does not own."""
    if not isinstance(value, dict) or not is_int(value.get("schema")) or value["schema"] != MANAGER_SCHEMA:
        raise InstallError("installation receipt has an unsupported schema")
    if not is_hex(value.get("operation_id"), 32) or value.get("kind") not in ("setup", "update", "rollback"):
        raise InstallError("installation receipt has an invalid operation identity")
    for key in ("generation_id", "version"):
        if value.get(key) is not None and not isinstance(value[key], str):
            raise InstallError(f"installation receipt field {key} has an invalid type")
    if value.get("generation_id") is not None and not _GENERATION_RE.fullmatch(value["generation_id"]):
        raise InstallError("installation receipt names an invalid generation")
    if value.get("program_id") is not None and not is_hex(value["program_id"], 64):
        raise InstallError("installation receipt has an invalid program identity")
    if value.get("backup") is not None and not is_hex(value["backup"], 32):
        raise InstallError("installation receipt names an invalid backup")
    assets = value.get("assets")
    if not isinstance(assets, list):
        raise InstallError("installation receipt assets are not a list")
    seen: set[str] = set()
    for item in assets:
        if not isinstance(item, dict) or set(item) != {"role", "dest", "sha256", "mode", "size"} or item["role"] not in _ASSET_ROLES or not isinstance(item["dest"], str):
            raise InstallError("installation receipt lists a malformed asset")
        name = Path(item["dest"]).name
        if not asset_name_ok(item["role"], name) or item["dest"] != str(layout.destination(item["role"], name)) or item["dest"] in seen:
            raise InstallError(f"installation receipt lists {item['dest']!r}, which is not a canonical managed destination (it is not owned by this installation)")
        if not is_hex(item["sha256"], 64) or not is_int(item["mode"]) or not is_int(item["size"]) or item["size"] < 0:
            raise InstallError("installation receipt lists an asset with invalid hash, mode or size")
        seen.add(item["dest"])
    previous = value.get("previous")
    if previous is not None:
        if not isinstance(previous, dict) or previous.get("kind") not in ("generation", "legacy"):
            raise InstallError("installation receipt has an invalid previous-release record")
        if previous["kind"] == "generation" and not (isinstance(previous.get("generation_id"), str) and _GENERATION_RE.fullmatch(previous["generation_id"])):
            raise InstallError("installation receipt names an invalid previous generation")
    return value


def release_inventory_id(manifest: dict[str, Any], assets: list[dict[str, Any]]) -> str:
    """The payload identity a release's complete file inventory hashes to: the verified generation's program files plus the receipt's
    assets, in the order the payload loader defines. A receipt that lists any file the release did not ship (or omits one it did)
    cannot reproduce the payload identity it records."""
    files = [{"role": "program", "name": item["name"], "sha256": item["sha256"], "size": item["size"], "mode": item["mode"]} for item in sorted(manifest["files"], key=lambda x: x["name"])]
    owned = [{"role": item["role"], "name": Path(item["dest"]).name, "sha256": item["sha256"], "size": item["size"], "mode": item["mode"]} for item in assets]
    owned.sort(key=lambda item: (ROLES.index(item["role"]), item["name"]))
    return canonical_hash({"version": manifest["version"], "files": files + owned})


def backup_identity(layout: Layout, name: str, manifest: dict[str, Any]) -> str:
    """Hash of the validated backup record (including the previous receipt a rollback would reinstate, which decides what the manager
    owns afterwards) together with the content hash of every stored copy: what a rollback approval binds."""
    copies = {entry["file"]: entry["before"]["sha256"] for entry in manifest["entries"] if entry["before"]["state"] == "file"}
    return canonical_hash({"manifest": manifest, "copies": copies, "legacy": (manifest.get("legacy_lib") or {}).get("files")})


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


MAX_RECEIPT_CHAIN = 32


def _approved_plan(layout: Layout, receipt: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    """The approved plan that wrote this receipt, read from its operation journal and bound to the receipt by operation id, approval
    hash and the plan's own hash. The operation KIND is taken from that plan, and the receipt's label must agree with it, so editing
    the label can neither select nor disable a check."""
    try:
        journal = json.loads(read_bytes_nofollow(layout.ops / f"{receipt['operation_id']}.json", MAX_JSON_BYTES))
    except (OSError, ValueError):
        return None, "the approved plan of the operation that wrote this receipt is not available, so the files it claims to own cannot be proven"
    plan = journal.get("plan") if isinstance(journal, dict) else None
    if (not isinstance(plan, dict) or journal.get("operation_id") != receipt["operation_id"] or journal.get("approval_hash") != receipt.get("approval_hash")
            or canonical_hash(plan) != receipt.get("approval_hash") or plan.get("kind") not in ("setup", "update", "rollback") or plan["kind"] != receipt.get("kind")):
        return None, "the installation receipt is not the one its approved plan produced (identity, hash or operation kind differ)"
    return plan, None


def receipt_release_problem(layout: Layout, receipt: dict[str, Any], manifest: dict[str, Any], _chain: tuple[str, ...] = ()) -> str | None:
    """Ownership is not a file name pattern, a label or a self-computed hash. The receipt's exact role/name/hash/size/mode inventory must
    reproduce the payload identity it records, over the program files of the independently verified generation, and must then be
    proven by the approved plan of the operation that wrote it, whatever its kind:
      setup/update  the inventory equals the one the plan enumerated for its candidate;
      rollback      the plan names a backup whose record is bound into the approval; the inventory is exactly that backup's previous
                    receipt, which is proven in turn against its own approved operation (bounded, cycle-refusing)."""
    operation = receipt["operation_id"]
    if operation in _chain or len(_chain) >= MAX_RECEIPT_CHAIN:
        return "the installation receipt chain is cyclic or too long to prove"
    claimed = receipt.get("payload_id")
    if not is_hex(claimed, 64):
        return "the installation receipt records no payload identity, so the files it claims to own cannot be proven"
    if receipt.get("generation_id") != manifest.get("generation_id") or receipt.get("version") != manifest.get("version"):
        return "the installation receipt names a different release than the verified generation"
    if release_inventory_id(manifest, receipt["assets"]) != claimed:
        return "the installation receipt's asset inventory does not reproduce the payload identity it records (it lists, omits or alters files the release did not ship)"
    plan, why = _approved_plan(layout, receipt)
    if plan is None:
        return why
    if plan["kind"] in ("setup", "update"):
        if (plan.get("candidate") or {}).get("payload_id") != claimed or plan.get("receipt_assets", plan.get("assets")) != receipt["assets"]:
            return "the installation receipt's asset inventory is not the one its approved plan enumerated"
        return None
    target = plan.get("candidate") or {}
    backup_name = plan.get("backup")
    if target.get("generation_id") != receipt.get("generation_id") or target.get("program_id") != receipt.get("program_id") or receipt.get("rolled_back_operation") != backup_name:
        return "the rollback receipt does not describe the release its approved rollback restores"
    if not isinstance(backup_name, str) or not is_hex(backup_name, 32):
        return "the approved rollback names no valid backup"
    record, problem = load_backup_checked(layout, backup_name)
    if record is None:
        return f"the backup its approved rollback restored cannot be used to prove ownership: {problem}"
    if backup_identity(layout, backup_name, record) != plan.get("backup_identity"):
        return "the backup its rollback restored changed after the rollback was approved"
    prior = record.get("previous_receipt")
    if prior is None or prior.get("generation_id") != receipt.get("generation_id") or prior.get("payload_id") != claimed or prior["assets"] != receipt["assets"]:
        return "the rollback receipt's inventory is not the previous receipt its approved backup recorded"
    prior_manifest, problems = verify_generation(layout.generations / str(prior["generation_id"]), expect_id=str(prior["generation_id"]), expect_program_id=prior.get("program_id"))
    if prior_manifest is None or problems:
        return "the release the rollback restored does not verify: " + "; ".join(problems)
    return receipt_release_problem(layout, prior, prior_manifest, (*_chain, operation))


def read_receipt(layout: Layout) -> dict[str, Any] | None:
    try:
        value = json.loads(read_bytes_nofollow(layout.receipt, MAX_JSON_BYTES))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as error:
        raise InstallError(f"installation receipt {layout.receipt} is unreadable ({error})", action="leave it in place and report it; do not delete installation records") from error
    return validate_receipt(layout, value)


def validate_backup_manifest(layout: Layout, name: str, value: Any) -> dict[str, Any]:
    """Exact-type schema for a backup record. Every restored path must be a canonical managed destination (or one of the three
    configuration files), and every stored copy is addressed only as `files/<n>` or `legacy-lib/<module>` beneath this
    operation's own backup directory, never by a recorded absolute or relative path."""
    if not is_hex(name, 32):
        raise InstallError("invalid backup name")
    if not isinstance(value, dict) or not is_int(value.get("schema")) or value["schema"] != 1 or value.get("operation_id") != name or not isinstance(value.get("entries"), list):
        raise InstallError("backup record has an unsupported shape")
    configs = {str(layout.sup_config), str(layout.tg_config), str(layout.backup_config)}
    seen: set[str] = set()
    for index, entry in enumerate(value["entries"]):
        if not isinstance(entry, dict) or set(entry) != {"dest", "role", "before", "file", "after"} or not isinstance(entry["dest"], str) or entry["dest"] in seen:
            raise InstallError("backup record has a malformed or duplicate entry")
        role, dest = entry["role"], entry["dest"]
        if role == "config":
            if dest not in configs:
                raise InstallError(f"backup record names {dest!r}, which is not a configuration file of this installation")
        elif role in _ASSET_ROLES:
            if not asset_name_ok(role, Path(dest).name) or dest != str(layout.destination(role, Path(dest).name)):
                raise InstallError(f"backup record names {dest!r}, which is not a canonical managed destination")
        else:
            raise InstallError("backup record has an unknown role")
        before, after = entry["before"], entry["after"]
        if not isinstance(before, dict) or before.get("state") not in ("absent", "file"):
            raise InstallError("backup record has an invalid preimage")
        if before["state"] == "file":
            if set(before) != {"state", "sha256", "mode"} or not is_hex(before["sha256"], 64) or not is_int(before["mode"]) or entry["file"] != f"files/{index}":
                raise InstallError("backup record has an invalid stored-copy reference")
        elif set(before) != {"state"} or entry["file"] is not None:
            raise InstallError("backup record has an invalid absent preimage")
        if after is not None and (not isinstance(after, dict) or set(after) != {"sha256", "mode"} or not is_hex(after["sha256"], 64) or not is_int(after["mode"])):
            raise InstallError("backup record has an invalid result record")
        seen.add(dest)
    legacy = value.get("legacy_lib")
    if legacy is not None:
        files = legacy.get("files") if isinstance(legacy, dict) else None
        if not isinstance(files, dict) or any(not isinstance(k, str) or not (_MODULE_RE.fullmatch(k) or k == QUOTA_LOCK) or not is_hex(v, 64) for k, v in files.items()):
            raise InstallError("backup record has an invalid legacy program listing")
    if value.get("previous_receipt") is not None:
        validate_receipt(layout, value["previous_receipt"])
    return value


def scan_legacy(layout: Layout) -> tuple[dict[str, str] | None, list[str]]:
    """A flat program directory (the layout the earlier installer wrote): every entry must be a plain file."""
    legacy: dict[str, str] = {}
    problems: list[str] = []
    try:
        with os.scandir(layout.lib) as entries:
            for entry in sorted(entries, key=lambda item: item.name):
                if entry.name == "generations" and entry.is_dir(follow_symlinks=False):
                    continue  # retained program generations (for roll-forward) are the manager's own and are not legacy files
                if entry.name in COMPAT_LINKS and compat_link_state(layout, entry.name) == "ours":
                    continue  # the managed compatibility link is not a legacy file
                state = describe(Path(entry.path))
                if state["state"] != "file":
                    problems.append(f"{entry.name} is {state.get('kind') or 'missing'}")
                else:
                    legacy[entry.name] = state["sha256"]
    except FileNotFoundError:
        return {}, []
    except OSError as error:
        return None, [f"program directory unreadable: {error.strerror}"]
    return (legacy if not problems else None), problems


def inspect_install(layout: Layout) -> InstallState:
    """Classify what is installed, reading only. `managed` requires a verified active generation and a matching receipt."""
    try:
        active = read_active(layout)
    except InstallError as error:
        return InstallState("unknown", problems=[str(error)])
    if active is None:
        legacy, problems = scan_legacy(layout)
        if legacy is None:
            return InstallState("unknown", problems=problems)
        if not legacy:
            return InstallState("absent")
        if "herdr_supervisor.py" in legacy:
            return InstallState("legacy", legacy=legacy)
        return InstallState("unknown", problems=["the program directory has files but no recognizable installation"])
    manifest, problems = verify_generation(layout.generations / active, expect_id=active)
    if manifest is None or problems:
        return InstallState("inconsistent", active_generation=active, manifest=manifest, problems=problems)
    try:
        receipt = read_receipt(layout)
    except InstallError as error:
        return InstallState("inconsistent", active_generation=active, manifest=manifest, problems=[str(error)])
    if receipt is None or receipt.get("generation_id") != active:
        return InstallState("inconsistent", active_generation=active, manifest=manifest, receipt=receipt,
                            problems=["the installation receipt does not name the active generation (an interrupted operation needs reconciling)"])
    if receipt.get("program_id") != manifest.get("program_id") or receipt.get("version") != manifest.get("version"):
        return InstallState("inconsistent", active_generation=active, manifest=manifest, receipt=receipt,
                            problems=["the installation receipt's program identity does not match the active generation (the generation was changed after it was recorded)"])
    unproven = receipt_release_problem(layout, receipt, manifest)
    if unproven is not None:
        return InstallState("inconsistent", active_generation=active, manifest=manifest, receipt=receipt, problems=[unproven])
    return InstallState("managed", active_generation=active, manifest=manifest, receipt=receipt)


# ------------------------------------------------------------------------------------------------ assets


@dataclasses.dataclass(frozen=True)
class Asset:
    role: str
    name: str
    dest: Path
    sha256: str
    mode: int
    size: int

    def entry(self) -> dict[str, Any]:
        return {"role": self.role, "dest": str(self.dest), "sha256": self.sha256, "mode": self.mode, "size": self.size}


def assets_for(payload: Payload, layout: Layout) -> list[Asset]:
    return [Asset(item.role, item.name, layout.destination(item.role, item.name), item.sha256, item.mode, item.size) for item in payload.files if item.role != "program"]


def asset_state(asset: Asset) -> str:
    """pristine-absent | applied | other, judged against exactly the candidate's bytes and mode."""
    state = describe(asset.dest)
    if state["state"] == "absent":
        return "absent"
    if state["state"] == "file" and state["sha256"] == asset.sha256 and state["mode"] == asset.mode:
        return "applied"
    return "other"


# ------------------------------------------------------------------------------------------------ locks and journal


class DeployLock:
    """Exclusive, bounded-wait lock that serializes every installation operation for one state directory."""

    def __init__(self, path: Path, *, wait: float = 30.0, poll: float = 0.05) -> None:
        self.path = path
        self.wait = wait
        self.poll = poll
        self.handle: Any = None

    def __enter__(self) -> DeployLock:
        ensure_private_dir(self.path.parent)
        handle = os.fdopen(os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600), "r+")
        deadline = time.monotonic() + self.wait
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except (BlockingIOError, PermissionError):
                if time.monotonic() >= deadline:
                    handle.close()
                    raise InstallError("another installation operation holds the deployment lock", action="wait for it to finish, then run `herdr-setup status`") from None
                time.sleep(self.poll)
        handle.seek(0)
        handle.truncate()
        handle.write(f"{os.getpid()}\n")
        handle.flush()
        self.handle = handle
        return self

    def __exit__(self, *_: Any) -> None:
        if self.handle is not None:
            with contextlib.suppress(OSError):
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()
            self.handle = None


def worker_lock_paths(layout: Layout) -> list[tuple[str, Path]]:
    """Every worker that serializes itself with a lock of its own, as (label, lock file): the controller's task worker, the
    read-only query worker and the Codex reset worker. These are the workers' real start/exclusion mechanisms, so holding them
    keeps a late start from running while files change, and finding one held means bounded work is running."""
    paths = layout.paths
    return [("a supervisor worker", paths.lock_file), ("a read-only query worker", paths.query_lock_file), ("a Codex reset worker", paths.codex_reset_dir / "worker.lock")]


@contextlib.contextmanager
def worker_quiesced(layout: Layout) -> Iterator[None]:
    """Hold every worker's own lock for the whole operation, so no worker can start (or be running) while program files change.
    Refuses, and never stops anything, if a worker already holds its lock."""
    ensure_private_dir(layout.state)
    with contextlib.ExitStack() as stack:
        for label, path in worker_lock_paths(layout):
            ensure_private_dir(path.parent)
            lock = core.WorkerLock(path)
            try:
                lock.acquire()
            except core.SupervisorError as error:
                raise InstallError(f"{label} is running or holds its worker lock", action="let the running work finish, then retry; it is never stopped") from error
            stack.callback(lock.release)
        yield


STEP_STATES = ("PENDING", "RUNNING", "DONE", "FAILED", "UNKNOWN")


@dataclasses.dataclass
class Step:
    id: str
    title: str
    run: Callable[[], str | None]
    idempotent: bool = True
    external: bool = False
    # For a step that may have taken effect without being recorded: "done" | "not_done" | "unknown", read-only.
    reconcile: Callable[[], str] | None = None


class Journal:
    """Durable record of one operation. Written before the first mutation and after every step."""

    def __init__(self, path: Path, data: dict[str, Any], clock: Callable[[], float]) -> None:
        self.path = path
        self.data = data
        self.clock = clock

    @classmethod
    def load(cls, path: Path, clock: Callable[[], float]) -> Journal | None:
        try:
            value = json.loads(read_bytes_nofollow(path, MAX_JSON_BYTES))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as error:
            raise InstallError(f"operation journal {path.name} is unreadable ({error})", code=EXIT_RECONCILING, action="keep it; it records an unfinished operation") from error
        if not isinstance(value, dict) or value.get("schema") != MANAGER_SCHEMA or not isinstance(value.get("steps"), list):
            raise InstallError(f"operation journal {path.name} has an unsupported shape", code=EXIT_RECONCILING)
        return cls(path, value, clock)

    @classmethod
    def create(cls, path: Path, *, operation_id: str, kind: str, approval_hash: str, plan: dict[str, Any], step_ids: list[str], clock: Callable[[], float]) -> Journal:
        data = {"schema": MANAGER_SCHEMA, "operation_id": operation_id, "kind": kind, "approval_hash": approval_hash, "plan": plan, "status": "INTENT",
                "started_at": core.iso_utc(clock()), "steps": [{"id": sid, "status": "PENDING", "detail": None, "at": None} for sid in step_ids], "result": None}
        journal = cls(path, data, clock)
        journal.save()
        return journal

    def save(self) -> None:
        core.atomic_write_json(self.path, self.data, mode=0o600)

    def step(self, step_id: str) -> dict[str, Any]:
        for item in self.data["steps"]:
            if item["id"] == step_id:
                return item
        raise InstallError(f"operation journal has no step {step_id}", code=EXIT_RECONCILING)

    def mark(self, step_id: str, status: str, detail: str | None = None) -> None:
        item = self.step(step_id)
        item.update(status=status, detail=detail, at=core.iso_utc(self.clock()))
        self.save()

    def finish(self, status: str, result: dict[str, Any] | None = None) -> None:
        self.data.update(status=status, result=result, finished_at=core.iso_utc(self.clock()))
        self.save()


@dataclasses.dataclass
class RunOutcome:
    status: str  # COMPLETE | FAILED | RECONCILING
    failed_step: str | None = None
    message: str = ""


def run_steps(env: Env, journal: Journal, steps: list[Step]) -> RunOutcome:
    """Execute steps in order, durably. A completed step is skipped; an idempotent one interrupted mid-run is
    repeated; a non-idempotent or external one interrupted mid-run is reconciled read-only and is never blindly
    repeated; an unknown outcome stops the operation as RECONCILING."""
    journal.data["status"] = "RUNNING"
    journal.save()
    for step in steps:
        record = journal.step(step.id)
        status = record["status"]
        if status == "DONE":
            continue
        if status in ("RUNNING", "FAILED", "UNKNOWN") and (step.external or not step.idempotent):
            verdict = step.reconcile() if step.reconcile is not None else "unknown"
            if verdict == "done":
                journal.mark(step.id, "DONE", "recorded as complete after a read-only check")
                continue
            if verdict != "not_done":
                journal.mark(step.id, "UNKNOWN", "the outcome of an earlier attempt could not be established")
                journal.finish("RECONCILING", {"step": step.id})
                return RunOutcome("RECONCILING", step.id, f"the outcome of '{step.title}' is unknown; it was not repeated")
        env.at(f"before:{step.id}")
        journal.mark(step.id, "RUNNING")
        try:
            detail = step.run()
            env.at(f"after:{step.id}")
        except InstallError as error:
            journal.mark(step.id, "FAILED", str(error))
            code_unknown = error.code == EXIT_RECONCILING
            journal.finish("RECONCILING" if code_unknown else "FAILED", {"step": step.id, "error": str(error)})
            return RunOutcome("RECONCILING" if code_unknown else "FAILED", step.id, str(error))
        journal.mark(step.id, "DONE", detail)
        env.at(f"done:{step.id}")
    journal.finish("COMPLETE")
    return RunOutcome("COMPLETE")


# ------------------------------------------------------------------------------------------------ quiescence checks

_QUEUE_DIRECTORIES = (
    ("queued commands", ("inbox", "pending")),
    ("commands being processed", ("inbox", "processing")),
    ("queued read-only questions", ("query", "inbox")),
    ("queued reset requests", ("codex-reset", "pending")),
    ("reset requests being processed", ("codex-reset", "processing")),
    ("pending Telegram task starts", ("pending-starts",)),
)
_UNRESOLVED_DELIVERY = {"prepared", "accepted", "uncertain", "interrupted"}


def _visible_entries(directory: Path) -> list[str]:
    try:
        return sorted(entry.name for entry in os.scandir(directory) if not entry.name.startswith("."))
    except FileNotFoundError:
        return []
    except OSError as error:
        return [f"<unreadable: {error.strerror}>"]


def _state_problems(state: dict[str, Any] | None) -> list[str]:
    if state is None:
        return []
    problems: list[str] = []
    phase = state.get("supervisor_state")
    if phase not in core.TERMINAL_STATES:
        problems.append(f"a supervised task is not finished (state {phase})")
    gate = state.get("pending_gate")
    if isinstance(gate, dict) and gate.get("status") == "pending":
        problems.append("a human decision (gate) is pending")
    for key, text in (("continuation", "a continuation is pending"), ("owner_recovery", "a session-ownership recovery is unresolved"), ("interrupted_delivery", "an interrupted delivery is unresolved"),
                      ("missing_result", "a settled turn has an unread result"), ("runtime_proposal", "a runtime-evidence proposal awaits operator confirmation")):
        if state.get(key):
            problems.append(text)
    delivery = state.get("delivery")
    if isinstance(delivery, dict) and delivery.get("status") in _UNRESOLVED_DELIVERY:
        problems.append(f"a prompt delivery is {delivery.get('status')} (uncertain delivery is never replayed)")
    followup = state.get("agent_followup")
    if isinstance(followup, dict) and followup.get("state") in core.FOLLOWUP_UNRESOLVED:
        problems.append("an agent follow-up is unresolved")
    return problems


def workload_problems(layout: Layout, *, probe_lock: bool = True) -> list[str]:
    """Reasons the host is not at a quiet, terminal point. Empty means no active worker, no unresolved task, no queued work.
    Read-only; any unreadable record is itself a reason (fail closed)."""
    problems: list[str] = []
    paths = layout.paths
    if probe_lock:
        for label, lock_path in worker_lock_paths(layout):
            try:
                if core.WorkerLock.is_held(lock_path):
                    problems.append(f"{label} holds its worker lock")
            except OSError as error:
                problems.append(f"the {label} lock cannot be inspected ({error.strerror})")
    try:
        state = StateStore(paths, time.time).read_state(required=False)
    except (core.SupervisorError, OSError) as error:
        problems.append(f"the task state cannot be read or is unsupported ({error})")
    else:
        problems += _state_problems(state)
    for label, parts in _QUEUE_DIRECTORIES:
        names = _visible_entries(layout.state.joinpath(*parts))
        if names:
            problems.append(f"{label} are present ({len(names)} in {'/'.join(parts)})")
    preparations = layout.state / "session-preparations"
    for name in _visible_entries(preparations):
        try:
            record = json.loads((preparations / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            problems.append(f"session preparation {name} is unreadable")
            continue
        if isinstance(record, dict) and record.get("status") not in ("TASK_STARTED", "ABANDONED"):
            problems.append(f"a session preparation is unresolved ({name})")
    return problems


def state_schema_on_disk(layout: Layout) -> int | None:
    """The schema number of an existing task-state file, or None when there is none; -1 when it cannot be read."""
    path = layout.paths.state_file
    if not path.exists():
        return None
    try:
        value = json.loads(read_bytes_nofollow(path, MAX_JSON_BYTES))
    except (OSError, ValueError):
        return -1
    schema = value.get("schema_version") if isinstance(value, dict) else None
    return schema if type(schema) is int else -1


# ------------------------------------------------------------------------------------------------ services

_TRANSITIONAL = {"activating", "deactivating", "reloading", "refreshing"}


class Systemd:
    """The user systemd manager through `systemctl --user`. Every method is one supported command; state queries are
    read-only and fail closed ('unknown') when the manager cannot be reached."""

    def __init__(self, host: Host) -> None:
        self.host = host

    def available(self) -> bool:
        if self.host.which("systemctl") is None:
            return False
        return self.host.run(["systemctl", "--user", "show-environment"], timeout=15).rc == 0

    def state(self, unit: str) -> str:
        result = self.host.run(["systemctl", "--user", "is-active", unit], timeout=15)
        answer = result.out.strip()
        if result.rc == 0:
            return "active"
        if answer in ("inactive", "failed") or answer in _TRANSITIONAL:
            return answer
        return "unknown"

    def enabled(self, unit: str) -> str:
        result = self.host.run(["systemctl", "--user", "is-enabled", unit], timeout=15)
        answer = result.out.strip()
        return answer if answer in ("enabled", "enabled-runtime", "linked", "linked-runtime", "static", "indirect", "disabled", "masked", "not-found", "alias") else "unknown"

    def act(self, verb: str, *units: str) -> None:
        if verb not in ("daemon-reload", "stop", "start", "enable", "disable"):
            raise InstallError(f"unsupported systemctl action {verb}")
        result = self.host.run(["systemctl", "--user", verb, *units], timeout=120)
        if result.rc != 0:
            raise InstallError(f"systemctl --user {verb} {' '.join(units)} failed: {(result.err or result.out).strip()[:300]}", action="inspect the unit with `systemctl --user status`")

    def linger(self, user: str) -> str:
        if self.host.which("loginctl") is None:
            return "unknown"
        result = self.host.run(["loginctl", "show-user", user, "--property=Linger", "--value"], timeout=15)
        answer = result.out.strip()
        return {"yes": "enabled", "no": "disabled"}.get(answer, "unknown") if result.rc == 0 else "unknown"


def verify_units(env: Env, payload: Payload) -> dict[str, Any]:
    """Validate the exact candidate unit bytes with `systemd-analyze --user verify` BEFORE any target write. The units
    are staged under a throwaway HOME together with the launchers they name, so `%h` resolves without touching the
    real home. Returns {"status": "ok"|"skipped"|"failed", "detail": ...}."""
    host = env.host
    if host.which("systemd-analyze") is None:
        return {"status": "skipped", "detail": "systemd-analyze is not installed"}
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if not runtime:
        return {"status": "skipped", "detail": "XDG_RUNTIME_DIR is unset in this session (no user runtime directory)"}
    units = payload.of_role("unit")
    if not units:
        return {"status": "skipped", "detail": "the payload has no units"}
    with tempfile.TemporaryDirectory(prefix="herdr-verify-") as scratch:
        stage = Path(scratch)
        (stage / ".local/bin").mkdir(parents=True)
        (stage / ".config/systemd/user").mkdir(parents=True)
        for item in payload.of_role("launcher"):
            write_file_atomic(stage / ".local/bin" / item.name, payload.blobs[item.key], 0o755)
        write_file_atomic(stage / ".local/bin/herdr", b"#!/bin/sh\nexit 0\n", 0o755)  # stands in for the platform prerequisite
        paths = []
        for item in units:
            path = stage / ".config/systemd/user" / item.name
            write_file_atomic(path, payload.blobs[item.key], 0o644)
            paths.append(str(path))
        child_env = {**os.environ, "HOME": str(stage), "XDG_RUNTIME_DIR": runtime}
        result = host.run(["systemd-analyze", "--user", "verify", *paths], timeout=120, env=child_env)
    if result.rc == 0:
        return {"status": "ok", "detail": f"{len(units)} units verified"}
    return {"status": "failed", "detail": (result.err or result.out).strip()[-600:]}


def user_name() -> str:
    return getpass.getuser()
