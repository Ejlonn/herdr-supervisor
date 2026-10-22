#!/usr/bin/env python3
"""Explicit, bounded dependency check and bootstrap for the `herdr-agent-quota` Herdr plugin.

The supervisor reads quota snapshots written by that plugin and rechecks quota through its non-LLM
`refresh` action. This tool never changes how snapshots are read. It answers two questions for a fresh
install and keeps the answer reproducible: which exact plugin revision is the supported dependency (the
lock record `quota-dependency.json`), and is it installed and producing snapshots.

`check` is read-only. `bootstrap` is a dry run unless `--apply` is given; with `--apply` it only stages
the locked source (from the lock's repository by exact commit, or from a hash-verified local recovery
bundle/archive) in a new directory, verifies it against the lock, and runs `cargo build --release --locked`
there. It never links, enables, configures, or installs anything into Herdr, never runs the upstream
installer (which edits hooks and settings), never reads credentials, and never calls a model or a quota
provider. Linking and configuration stay explicit operator actions printed as next steps.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import posixpath
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import tomllib
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_ACTION = 3  # something is missing or unknown; a specific recovery action is printed
EXIT_REFUSED = 4  # conflict, drift, hash mismatch or unsafe content; nothing was changed

LOCK_NAME = "quota-dependency.json"
MAX_MEMBERS = 2000
MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_ARTIFACT_BYTES = 128 * 1024 * 1024
MAX_PATH_BYTES = 1024
MAX_COMPONENT_BYTES = 255
DEFAULT_MAX_AGE_SECONDS = 900  # matches the supervisor's quota_snapshot_max_age_seconds default
DEFAULT_SNAPSHOT_FILES = {"claude": "claude-statusline.json", "codex": "codex-app-server.json"}
TAIL_CHARS = 4000

_HEX40 = re.compile(r"^[0-9a-f]{40}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_HTTPS_URL = re.compile(r"^https://[A-Za-z0-9.-]{1,253}(?::[0-9]{1,5})?/[A-Za-z0-9._/-]{1,300}$")
_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
_PLUGIN_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_ARTIFACT_KINDS = ("git-bundle", "source-archive")
_LOCK_KEYS = {
    "schema_version", "dependency", "plugin_id", "repository", "upstream", "revision", "tree_sha1", "manifest_version",
    "min_herdr_version", "license", "toolchain", "critical_files", "content_manifest_sha256", "allowed_symlinks", "recovery_artifacts", "provenance",
}
# RUSTUP_TOOLCHAIN is deliberately absent: the lock selects the toolchain, an inherited override never does.
_BUILD_ENV_KEYS = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "CARGO_HOME", "RUSTUP_HOME")
LOCK_FILE = ".bootstrap.lock"
RECEIPT_FILE = "BOOTSTRAP_RECEIPT.json"


class DependencyError(Exception):
    """A refusal or a missing prerequisite, with the exit code and the specific recovery action."""

    def __init__(self, message: str, *, code: int = EXIT_REFUSED, action: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.action = action


@dataclass(frozen=True)
class Entry:
    path: str
    mode: str  # git tree mode: 100644, 100755 or 120000
    blob_sha1: str
    sha256: str


# --------------------------------------------------------------------------------------------- lock record

def _require(condition: bool, message: str) -> None:
    if not condition:
        raise DependencyError(f"invalid dependency lock: {message}", action="restore quota-dependency.json from the reviewed source tree; do not edit it by hand")


def _is_str(value: Any) -> bool:
    return type(value) is str


def validate_lock(raw: Any) -> dict[str, Any]:
    """Strict shape check. A lock that is not exactly the reviewed shape is refused, never repaired."""
    _require(isinstance(raw, dict), "top level must be an object")
    _require(set(raw) == _LOCK_KEYS, f"keys must be exactly {sorted(_LOCK_KEYS)}")
    _require(type(raw["schema_version"]) is int and raw["schema_version"] == 1, "schema_version must be 1")
    _require(_is_str(raw["plugin_id"]) and _PLUGIN_ID.match(raw["plugin_id"]) is not None, "plugin_id shape")
    _require(raw["dependency"] == raw["plugin_id"], "dependency must equal plugin_id")
    repository = raw["repository"]
    _require(isinstance(repository, dict) and set(repository) == {"url"} and _is_str(repository["url"]) and _HTTPS_URL.match(repository["url"]) is not None, "repository.url must be an https URL")
    upstream = raw["upstream"]
    _require(isinstance(upstream, dict) and set(upstream) == {"url", "relocated_from"} and all(_is_str(v) and _HTTPS_URL.match(v) for v in upstream.values()), "upstream provenance URLs")
    _require(_is_str(raw["revision"]) and _HEX40.match(raw["revision"]) is not None, "revision must be a full 40-hex commit id (no branch or tag)")
    _require(_is_str(raw["tree_sha1"]) and _HEX40.match(raw["tree_sha1"]) is not None, "tree_sha1 must be 40 hex characters")
    _require(_is_str(raw["manifest_version"]) and _VERSION.match(raw["manifest_version"]) is not None, "manifest_version shape")
    _require(_is_str(raw["min_herdr_version"]) and _VERSION.match(raw["min_herdr_version"]) is not None, "min_herdr_version shape")
    license_ = raw["license"]
    _require(isinstance(license_, dict) and set(license_) == {"spdx", "file", "sha256", "copyright"}, "license keys")
    _require(license_["spdx"] == "MIT" and _is_str(license_["file"]) and _is_str(license_["copyright"]) and _is_str(license_["sha256"]) and _HEX64.match(license_["sha256"]) is not None, "license record")
    toolchain = raw["toolchain"]
    _require(isinstance(toolchain, dict) and set(toolchain) == {"channel", "build_command"}, "toolchain keys")
    _require(_is_str(toolchain["channel"]) and _VERSION.match(toolchain["channel"]) is not None, "toolchain channel must be an exact version")
    _require(toolchain["build_command"] == ["cargo", "build", "--release", "--locked"], "build_command must be exactly cargo build --release --locked")
    critical = raw["critical_files"]
    _require(isinstance(critical, dict) and {"Cargo.lock", "Cargo.toml", "herdr-plugin.toml", "rust-toolchain.toml", license_["file"]} <= set(critical), "critical_files must cover the manifest, Cargo files, toolchain and license")
    for name, digest in critical.items():
        _require(_is_str(name) and _is_str(digest) and _HEX64.match(digest) is not None and _safe_relpath_or_none(name) is not None, f"critical file {name!r}")
    _require(critical[license_["file"]] == license_["sha256"], "license hash must equal its critical file hash")
    _require(_is_str(raw["content_manifest_sha256"]) and _HEX64.match(raw["content_manifest_sha256"]) is not None, "content_manifest_sha256 shape")
    links = raw["allowed_symlinks"]
    _require(isinstance(links, dict) and all(_is_str(k) and _is_str(v) and _safe_relpath_or_none(k) and _safe_relpath_or_none(v) for k, v in links.items()), "allowed_symlinks must map relative paths to relative targets")
    artifacts = raw["recovery_artifacts"]
    _require(isinstance(artifacts, list) and len(artifacts) > 0, "recovery_artifacts must be a non-empty list")
    for artifact in artifacts:
        _require(isinstance(artifact, dict) and set(artifact) == {"kind", "file", "sha256", "note"}, "recovery artifact keys")
        _require(artifact["kind"] in _ARTIFACT_KINDS and _is_str(artifact["file"]) and "/" not in artifact["file"] and _is_str(artifact["note"]), "recovery artifact fields")
        _require(_is_str(artifact["sha256"]) and _HEX64.match(artifact["sha256"]) is not None, "recovery artifact sha256")
    _require(isinstance(raw["provenance"], dict), "provenance must be an object")
    result: dict[str, Any] = raw
    return result


def lock_candidates(explicit: str | None) -> list[Path]:
    if explicit:
        return [Path(explicit)]
    here = Path(__file__).resolve().parent
    return [here / LOCK_NAME, here.parent / "config" / LOCK_NAME]


def load_lock(explicit: str | None = None) -> dict[str, Any]:
    for path in lock_candidates(explicit):
        if path.is_file():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as error:
                raise DependencyError(f"cannot read dependency lock {path.name}: {error}", action="restore quota-dependency.json from the reviewed source tree") from error
            return validate_lock(raw)
    raise DependencyError(
        "dependency lock quota-dependency.json not found",
        code=EXIT_ACTION,
        action="run from a source checkout or an installed copy (install.sh places the lock beside the program files), or pass --lock PATH",
    )


# ---------------------------------------------------------------------------------------- path and content

def _safe_relpath_or_none(name: str) -> str | None:
    """Canonical tree-relative path, or None. Rejects absolute paths, traversal, empty components, control characters."""
    if not name or name.startswith("/") or "\\" in name or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in name):
        return None
    raw = name[:-1] if name.endswith("/") else name
    parts = raw.split("/")
    if any(part in ("", ".", "..") for part in parts):
        return None
    try:
        if len(raw.encode("utf-8")) > MAX_PATH_BYTES or any(len(part.encode("utf-8")) > MAX_COMPONENT_BYTES for part in parts):
            return None
    except UnicodeEncodeError:
        return None
    return raw


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path, limit: int = MAX_ARTIFACT_BYTES) -> str:
    size = path.stat().st_size
    if size > limit:
        raise DependencyError(f"{path.name} is larger than the {limit} byte limit", action="use the recovery artifact recorded in the dependency lock")
    return sha256_bytes(path.read_bytes())


def blob_sha1(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data, usedforsecurity=False).hexdigest()  # git object id, not a security hash on its own


def git_tree_sha1(entries: list[Entry]) -> str:
    """Git tree object id of the entries, computed without git so in-tree attributes or filters cannot alter it."""
    root: dict[str, Any] = {}
    for entry in entries:
        parts = entry.path.split("/")
        node = root
        for part in parts[:-1]:
            child = node.setdefault(part, {})
            if not isinstance(child, dict):
                raise DependencyError(f"{entry.path}: a file and a directory share a name", action="reject this source")
            node = child
        if parts[-1] in node:
            raise DependencyError(f"{entry.path}: duplicate entry", action="reject this source")
        node[parts[-1]] = entry

    def tree(node: dict[str, Any]) -> str:
        items: list[tuple[bytes, bytes]] = []
        for name, child in node.items():
            if isinstance(child, dict):
                key, mode, oid = name + "/", b"40000", bytes.fromhex(tree(child))
            else:
                key, mode, oid = name, child.mode.encode(), bytes.fromhex(child.blob_sha1)
            items.append((key.encode("utf-8"), mode + b" " + name.encode("utf-8") + b"\0" + oid))
        body = b"".join(item for _, item in sorted(items))
        return hashlib.sha1(b"tree %d\0" % len(body) + body, usedforsecurity=False).hexdigest()

    return tree(root)


def content_manifest_sha256(entries: list[Entry]) -> str:
    lines = "".join(f"{e.mode} {e.sha256} {e.path}\n" for e in sorted(entries, key=lambda e: e.path.encode("utf-8")))
    return sha256_bytes(lines.encode("utf-8"))


# --------------------------------------------------------------------------------------- safe extraction

def extract_archive(tar: tarfile.TarFile, dest: Path, allowed_symlinks: dict[str, str]) -> list[Entry]:
    """Extract only plain files, directories and the lock's exact symlinks beneath `dest` (an empty directory we created).

    Every other member type, any path that is absolute, contains traversal or collides, any member under a
    symlink, and any size or count above the limits stops the extraction. Modes are normalized (644/755).
    """
    dest = dest.resolve()
    entries: list[Entry] = []
    seen: set[str] = set()
    folded: set[str] = set()
    links: list[tuple[str, str]] = []
    total = 0
    count = 0
    for member in tar:
        count += 1
        if count > MAX_MEMBERS:
            raise DependencyError(f"archive has more than {MAX_MEMBERS} members", action="reject this source")
        rel = _safe_relpath_or_none(member.name)
        if rel is None:
            raise DependencyError(f"unsafe member path {member.name!r}", action="reject this source; it is not the locked source")
        if rel.casefold() in folded and member.type != tarfile.DIRTYPE:
            raise DependencyError(f"{rel}: duplicate or case-colliding member", action="reject this source")
        target = dest / rel
        if member.type == tarfile.DIRTYPE:
            try:
                target.mkdir(parents=True, exist_ok=True, mode=0o755)
            except OSError as error:
                raise DependencyError(f"{rel}: cannot create directory ({error.strerror})", action="reject this source") from error
            continue
        folded.add(rel.casefold())
        seen.add(rel)
        if member.type == tarfile.SYMTYPE:
            links.append((rel, member.linkname))
            continue
        if member.type not in (tarfile.REGTYPE, tarfile.AREGTYPE):
            raise DependencyError(f"{rel}: member type {member.type!r} is not allowed (only files, directories and the locked symlinks)", action="reject this source")
        if member.size > MAX_FILE_BYTES:
            raise DependencyError(f"{rel}: larger than the {MAX_FILE_BYTES} byte file limit", action="reject this source")
        total += member.size
        if total > MAX_TOTAL_BYTES:
            raise DependencyError(f"archive exceeds the {MAX_TOTAL_BYTES} byte total limit", action="reject this source")
        reader = tar.extractfile(member)
        data = reader.read() if reader is not None else b""
        if len(data) != member.size:
            raise DependencyError(f"{rel}: truncated member", action="reject this source")
        mode = "100755" if member.mode & 0o100 else "100644"
        try:
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except OSError as error:
            raise DependencyError(f"{rel}: cannot create file ({error.strerror})", action="reject this source") from error
        with os.fdopen(fd, "wb") as out:
            out.write(data)
        os.chmod(target, 0o755 if mode == "100755" else 0o644)
        entries.append(Entry(rel, mode, blob_sha1(data), sha256_bytes(data)))
    link_names = {name for name, _ in links}
    for rel, linkname in links:
        if allowed_symlinks.get(rel) != linkname:
            raise DependencyError(f"{rel}: symlink to {linkname!r} is not in the dependency lock", action="reject this source")
        resolved = posixpath.normpath(posixpath.join(posixpath.dirname(rel), linkname))
        if _safe_relpath_or_none(linkname) is None or resolved not in seen or resolved in link_names:
            raise DependencyError(f"{rel}: symlink target {linkname!r} is not a file of this source", action="reject this source")
    for rel in seen:
        if any(rel.startswith(name + "/") for name in link_names):
            raise DependencyError(f"{rel}: member below a symlink", action="reject this source")
    for rel, linkname in links:
        try:
            os.symlink(linkname, dest / rel)
        except OSError as error:
            raise DependencyError(f"{rel}: cannot create symlink ({error.strerror})", action="reject this source") from error
        entries.append(Entry(rel, "120000", blob_sha1(linkname.encode("utf-8")), sha256_bytes(linkname.encode("utf-8"))))
    return entries


def verify_source(entries: list[Entry], root: Path, lock: dict[str, Any], archive_commit: str | None = None) -> dict[str, str]:
    """Check an extracted tree against the lock: identity, manifest, license, Cargo files, then the authoritative tree id."""
    if archive_commit is not None and archive_commit != lock["revision"]:
        raise DependencyError(f"archive records commit {archive_commit[:12]}, the lock pins {lock['revision'][:12]}", action="use an artifact built from the locked revision; never accept a moved branch or tag")
    by_path = {e.path: e for e in entries}
    for name, digest in lock["critical_files"].items():
        entry = by_path.get(name)
        if entry is None or entry.mode == "120000":
            raise DependencyError(f"locked file {name} is missing from the source", action="use an artifact built from the locked revision")
        if entry.sha256 != digest:
            raise DependencyError(f"{name}: sha256 differs from the dependency lock", action="the source is not the locked revision; reject it")
    try:
        manifest = tomllib.loads((root / "herdr-plugin.toml").read_text(encoding="utf-8"))
        cargo = tomllib.loads((root / "Cargo.toml").read_text(encoding="utf-8"))
        toolchain = tomllib.loads((root / "rust-toolchain.toml").read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise DependencyError(f"cannot parse the plugin metadata: {error}", action="reject this source") from error
    if manifest.get("id") != lock["plugin_id"]:
        raise DependencyError(f"manifest plugin id {manifest.get('id')!r} differs from the lock ({lock['plugin_id']!r})", action="reject this source")
    if manifest.get("version") != lock["manifest_version"] or cargo.get("package", {}).get("version") != lock["manifest_version"]:
        raise DependencyError(f"manifest version differs from the locked {lock['manifest_version']}", action="reject this source; do not upgrade without a reviewed lock change")
    if manifest.get("min_herdr_version") != lock["min_herdr_version"]:
        raise DependencyError("min_herdr_version differs from the lock", action="reject this source")
    if toolchain.get("toolchain", {}).get("channel") != lock["toolchain"]["channel"]:
        raise DependencyError("rust-toolchain channel differs from the lock", action="reject this source")
    tree = git_tree_sha1(entries)
    if tree != lock["tree_sha1"]:
        raise DependencyError(f"source tree {tree[:12]} differs from the locked tree {lock['tree_sha1'][:12]}", action="the content is not the locked revision; reject it")
    digest = content_manifest_sha256(entries)
    if digest != lock["content_manifest_sha256"]:
        raise DependencyError("content manifest differs from the dependency lock", action="the content is not the locked revision; reject it")
    return {"tree_sha1": tree, "content_manifest_sha256": digest}


def scan_tree(root: Path) -> list[Entry]:
    """Entries of an installed source directory, hashed from what is actually on disk (top-level `target/` build
    output excluded; it is not part of the locked source). Bounded like extraction; anything that is not a plain
    file, directory or symlink is refused."""
    root = Path(root)
    entries: list[Entry] = []
    total = 0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        base = Path(dirpath)
        kept: list[str] = []
        for name in sorted(dirnames):
            full = base / name
            if full.is_symlink():
                filenames.append(name)  # a symlinked directory is a symlink entry, never descended into
            elif base == root and name == "target":
                continue
            else:
                kept.append(name)
        dirnames[:] = kept
        for name in sorted(filenames):
            full = base / name
            rel = _safe_relpath_or_none(full.relative_to(root).as_posix())
            if rel is None:
                raise DependencyError(f"unsafe path in the installed source: {full.name!r}", action="reject this installation")
            if len(entries) >= MAX_MEMBERS:
                raise DependencyError(f"the installed source has more than {MAX_MEMBERS} files", action="reject this installation")
            if full.is_symlink():
                target = os.readlink(full).encode("utf-8", "surrogateescape")
                entries.append(Entry(rel, "120000", blob_sha1(target), sha256_bytes(target)))
                continue
            info = full.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE_BYTES:
                raise DependencyError(f"{rel}: not a plain file within the size limit", action="reject this installation")
            total += info.st_size
            if total > MAX_TOTAL_BYTES:
                raise DependencyError("the installed source exceeds the total size limit", action="reject this installation")
            data = full.read_bytes()
            entries.append(Entry(rel, "100755" if info.st_mode & 0o100 else "100644", blob_sha1(data), sha256_bytes(data)))
    return entries


# ------------------------------------------------------------------------------------------ subprocesses

def scrubbed_env(home: Path | None = None, extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"), "LC_ALL": "C"}
    if home is not None:
        env["HOME"] = str(home)
    env.update(extra or {})
    return env


def git_env(home: Path) -> dict[str, str]:
    """No inherited git configuration, credential helpers, prompts, askpass programs or exotic transports."""
    return scrubbed_env(home, {
        "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0", "GIT_ALLOW_PROTOCOL": "https:file", "GIT_ASKPASS": "", "SSH_ASKPASS": "",
    })


def run(argv: list[str], *, env: dict[str, str], cwd: Path | None = None, timeout: float = 60, input_bytes: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(argv, cwd=cwd, env=env, input=input_bytes, capture_output=True, timeout=timeout, check=False)
    except FileNotFoundError as error:
        raise DependencyError(f"{argv[0]}: command not found", code=EXIT_ACTION, action=f"install {Path(argv[0]).name} and retry") from error
    except subprocess.TimeoutExpired as error:
        raise DependencyError(f"{Path(argv[0]).name} timed out after {int(timeout)} s", code=EXIT_ACTION, action="retry, or raise the timeout flag") from error


def tail(data: bytes) -> str:
    return data.decode("utf-8", "replace")[-TAIL_CHARS:].strip()


# ------------------------------------------------------------------------------------ source acquisition

def git_archive_bytes(gitdir: Path, revision: str, home: Path) -> bytes:
    env = git_env(home)
    kind = run(["git", f"--git-dir={gitdir}", "cat-file", "-t", revision], env=env)
    if kind.returncode != 0 or kind.stdout.strip() != b"commit":
        raise DependencyError(f"commit {revision[:12]} is not present in the supplied source", action="use an artifact that contains the locked commit")
    out = run(["git", f"--git-dir={gitdir}", "archive", "--format=tar", revision], env=env, timeout=120)
    if out.returncode != 0:
        raise DependencyError(f"git archive failed: {tail(out.stderr)}", action="the supplied source is incomplete; use the recovery archive")
    if len(out.stdout) > MAX_ARTIFACT_BYTES:
        raise DependencyError("source archive exceeds the size limit", action="reject this source")
    return out.stdout


def fetch_pinned_commit(url: str, revision: str, work: Path, timeout: float) -> bytes:
    """Fetch exactly `revision` (depth 1, no tags, no branch resolution) and return its tar archive."""
    home = work / "home"
    home.mkdir()
    gitdir = work / "fetch.git"
    env = git_env(home)
    init = run(["git", "init", "--bare", "-q", str(gitdir)], env=env)
    if init.returncode != 0:
        raise DependencyError(f"git init failed: {tail(init.stderr)}", code=EXIT_ACTION, action="check that git works")
    fetched = run(
        ["git", f"--git-dir={gitdir}", "-c", "credential.helper=", "fetch", "--quiet", "--depth=1", "--no-tags", "--no-recurse-submodules", url, revision],
        env=env, timeout=timeout,
    )
    if fetched.returncode != 0:
        raise DependencyError(f"cannot fetch commit {revision[:12]} from {url}: {tail(fetched.stderr)}", code=EXIT_ACTION,
                              action="check network access and that the repository still serves the locked commit, or pass --from-archive/--from-bundle/--fallback-archive/--fallback-bundle with a lock-listed recovery artifact")
    head = run(["git", f"--git-dir={gitdir}", "rev-parse", "FETCH_HEAD"], env=env)
    if head.stdout.strip().decode("ascii", "replace") != revision:
        raise DependencyError("the remote answered with a different commit than the locked revision", action="reject this source; no branch or tag is followed")
    return git_archive_bytes(gitdir, revision, home)


def bundle_archive_bytes(path: Path, revision: str, work: Path) -> bytes:
    """Read a git bundle without fetching history: validate the header, unpack its objects, archive the locked commit.

    The retained recovery bundle is a single-commit snapshot (its parent commits are absent), so history
    traversal (`git fetch`/`git bundle unbundle`) fails; the tree id check in verify_source is the authority.
    """
    data = path.read_bytes()
    split = data.find(b"\n\n")
    if split < 0:
        raise DependencyError(f"{path.name} is not a git bundle", action="use a lock-listed recovery artifact")
    header = data[:split].decode("utf-8", "replace").splitlines()
    if not header or header[0] not in ("# v2 git bundle", "# v3 git bundle"):
        raise DependencyError(f"{path.name} is not a git bundle", action="use a lock-listed recovery artifact")
    refs = []
    for line in header[1:]:
        if line.startswith("@"):
            if line != "@object-format=sha1":
                raise DependencyError(f"unsupported bundle capability {line!r}", action="use a lock-listed recovery artifact")
        elif line.startswith("-"):
            raise DependencyError("the bundle has prerequisite commits and is not self-contained", action="use a lock-listed recovery artifact")
        else:
            refs.append(line.split(" ", 1)[0])
    if revision not in refs:
        raise DependencyError(f"the bundle does not contain commit {revision[:12]}", action="use a lock-listed recovery artifact")
    home = work / "home"
    home.mkdir()
    gitdir = work / "bundle.git"
    env = git_env(home)
    init = run(["git", "init", "--bare", "-q", str(gitdir)], env=env)
    if init.returncode != 0:
        raise DependencyError(f"git init failed: {tail(init.stderr)}", code=EXIT_ACTION, action="check that git works")
    unpacked = run(["git", f"--git-dir={gitdir}", "unpack-objects", "-q"], env=env, input_bytes=data[split + 2:], timeout=120)
    if unpacked.returncode != 0:
        raise DependencyError(f"bundle objects could not be unpacked: {tail(unpacked.stderr)}", action="use a lock-listed recovery artifact")
    return git_archive_bytes(gitdir, revision, home)


def tar_commit_header(tar: tarfile.TarFile) -> str | None:
    comment = (tar.pax_headers or {}).get("comment")
    return comment if isinstance(comment, str) and _HEX40.match(comment) else None


@dataclass
class Acquired:
    source: str  # fork | archive | bundle | fallback-archive | fallback-bundle
    entries: list[Entry]
    verified: dict[str, str]
    artifact_sha256: str | None


def lock_artifact(lock: dict[str, Any], path: Path, kind: str) -> str:
    """The artifact must be one the lock lists, by exact sha256, before a single byte of it is interpreted."""
    try:
        digest = sha256_file(path)
    except OSError as error:
        raise DependencyError(f"cannot read {path.name}: {error.strerror}", code=EXIT_ACTION, action="check the path of the recovery artifact") from error
    if not any(a["kind"] == kind and a["sha256"] == digest for a in lock["recovery_artifacts"]):
        raise DependencyError(f"{path.name}: sha256 {digest[:12]}… is not a {kind} listed in the dependency lock", action="use the recovery artifact recorded in the lock (compare with its SHA256SUMS)")
    return digest


def acquire(lock: dict[str, Any], dest: Path, *, source: str, path: Path | None, url: str, fetch_timeout: float) -> Acquired:
    """Materialize the locked source into `dest` (empty, ours) and verify it. `source` is fork, archive or bundle."""
    revision = lock["revision"]
    digest: str | None = None
    with tempfile.TemporaryDirectory(prefix="herdr-quota-dep-") as scratch:
        work = Path(scratch)
        if source == "fork":
            payload = fetch_pinned_commit(url, revision, work, fetch_timeout)
        elif source == "bundle" and path is not None:
            digest = lock_artifact(lock, path, "git-bundle")
            payload = bundle_archive_bytes(path, revision, work)
        elif source == "archive" and path is not None:
            digest = lock_artifact(lock, path, "source-archive")
            payload = path.read_bytes()
        else:
            raise DependencyError("internal: unknown source", code=EXIT_USAGE)
        try:
            with tarfile.open(fileobj=io.BytesIO(payload), mode="r:*") as tar:
                entries = extract_archive(tar, dest, lock["allowed_symlinks"])
                commit = tar_commit_header(tar)
        except (tarfile.TarError, OSError, EOFError) as error:
            raise DependencyError(f"cannot read the source archive: {error}", action="use a lock-listed recovery artifact") from error
    verified = verify_source(entries, dest, lock, commit)
    return Acquired(source, entries, verified, digest)


# -------------------------------------------------------------------------------------- Herdr inventory

def resolve_herdr(explicit: str | None) -> str | None:
    for candidate in (explicit, os.environ.get("HERDR_BIN_PATH"), str(Path.home() / ".local/bin/herdr")):
        if candidate and os.access(candidate, os.X_OK) and Path(candidate).is_file():
            return candidate
    return shutil.which("herdr")


def read_inventory(herdr: str | None) -> dict[str, Any]:
    """`herdr plugin list --json`: read-only. Anything unreadable is UNKNOWN, never 'absent'."""
    if herdr is None:
        return {"status": "UNKNOWN", "detail": "Herdr executable not found", "plugins": []}
    try:
        done = run([herdr, "plugin", "list", "--json"], env={**os.environ}, timeout=30)
    except DependencyError as error:
        return {"status": "UNKNOWN", "detail": str(error), "plugins": []}
    if done.returncode != 0:
        return {"status": "UNKNOWN", "detail": f"`herdr plugin list --json` exited {done.returncode}: {tail(done.stderr)[:300]}", "plugins": []}
    try:
        payload = json.loads(done.stdout.decode("utf-8"))
        plugins = payload["result"]["plugins"]
    except (ValueError, KeyError, TypeError):
        return {"status": "UNKNOWN", "detail": "unrecognized `herdr plugin list --json` output", "plugins": []}
    if not isinstance(plugins, list) or not all(isinstance(p, dict) for p in plugins):
        return {"status": "UNKNOWN", "detail": "unrecognized plugin list shape", "plugins": []}
    return {"status": "OK", "detail": "", "plugins": plugins}


def classify(inventory: dict[str, Any], lock: dict[str, Any]) -> dict[str, Any]:
    """State of the dependency in the inventory. Read-only; no state here ever authorizes an overwrite."""
    if inventory["status"] != "OK":
        return {"state": "unknown", "detail": inventory["detail"], "action": "install Herdr user-scoped (`~/.local/bin/herdr`) and make `herdr plugin list --json` work, then re-run check"}
    matches = [p for p in inventory["plugins"] if p.get("plugin_id") == lock["plugin_id"]]
    if not matches:
        return {"state": "absent", "detail": f"plugin {lock['plugin_id']} is not installed", "action": "run `herdr-quota-dependency bootstrap --stage-dir DIR --dry-run`, review it, then repeat with --apply and link the staged source"}
    if len(matches) > 1:
        return {"state": "conflict", "detail": f"{len(matches)} plugins share the id {lock['plugin_id']}", "action": "a human must decide which one to keep; nothing was changed"}
    plugin = matches[0]
    source = plugin.get("source") if isinstance(plugin.get("source"), dict) else {}
    version = plugin.get("version")
    facts: dict[str, Any] = {"version": version, "enabled": plugin.get("enabled"), "source_kind": source.get("kind")}
    revision = source.get("resolved_commit") if source.get("kind") == "github" else None
    if source.get("kind") == "github":
        facts["origin"] = f"{source.get('owner')}/{source.get('repo')}"
    elif source.get("kind") == "local":
        revision, how = local_identity(plugin.get("plugin_root"), lock)
        facts["verified_by"] = how
        if revision is None:
            return {"state": "conflict", "detail": f"locally linked source does not match the lock: {how}", "action": "preserved: a human must decide whether this source is the locked revision; nothing was changed", **facts}
    if revision is None:
        return {"state": "conflict", "detail": "installed plugin's revision cannot be established", "action": "a human must verify it (compare its git HEAD with the lock) before it is used or replaced; nothing was changed", **facts}
    facts["revision"] = revision
    if revision != lock["revision"] or version != lock["manifest_version"]:
        return {"state": "conflict", "detail": f"installed revision {revision[:12]} version {version} differs from the lock ({lock['revision'][:12]} {lock['manifest_version']})", "action": "preserved: neither upgrade nor downgrade was performed; a human decides whether to change the lock or the installation", **facts}
    detail = "installed at the locked revision"
    expected_origin = "/".join(lock["repository"]["url"].split("/")[-2:])
    if facts.get("origin") not in (None, expected_origin):
        detail += f"; recorded origin {facts['origin']} differs from the lock's {expected_origin} (same commit, preserved)"
    if plugin.get("enabled") is False:
        detail += "; installed but disabled (enabling is an operator action)"
    return {"state": "pinned", "detail": detail, "action": "none; the existing installation is preserved", **facts}


def local_revision(plugin_root: Any) -> str | None:
    """Revision of a locally linked plugin: only a clean git checkout's HEAD counts."""
    if not isinstance(plugin_root, str) or not Path(plugin_root).is_dir():
        return None
    env = {**git_env(Path(tempfile.gettempdir())), "GIT_OPTIONAL_LOCKS": "0"}  # `git status` must not refresh (write) the plugin checkout's index
    safe = ["git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=" + os.devnull, "-C", plugin_root]  # a plugin checkout's own config must not run programs
    top = run([*safe, "rev-parse", "--show-toplevel"], env=env)
    if top.returncode != 0 or os.path.realpath(top.stdout.strip().decode("utf-8", "replace")) != os.path.realpath(plugin_root):
        return None  # not a checkout of its own (for example a directory merely inside some other repository)
    head = run([*safe, "rev-parse", "HEAD"], env=env)
    status = run([*safe, "status", "--porcelain", "--untracked-files=no"], env=env)
    if head.returncode != 0 or status.returncode != 0 or status.stdout.strip():
        return None
    value = head.stdout.strip().decode("ascii", "replace")
    return value if _HEX40.match(value) else None


def local_identity(plugin_root: Any, lock: dict[str, Any]) -> tuple[str | None, str]:
    """Identity of a locally linked plugin directory: the locked revision, and how it was established, or (None, reason).

    Either a clean git checkout whose HEAD is read, or, for the source tree that `bootstrap` stages (an archive
    without git metadata), the directory's actual content verified against the lock: critical-file hashes, plugin id,
    versions, toolchain channel, the whole-tree manifest and the git tree id. A receipt or claimed revision is never trusted.
    """
    if not isinstance(plugin_root, str) or not Path(plugin_root).is_dir():
        return None, "the plugin directory is not readable"
    revision = local_revision(plugin_root)
    if revision is not None:
        return revision, "clean git checkout"
    try:
        entries = scan_tree(Path(plugin_root))
        verify_source(entries, Path(plugin_root), lock)
    except (DependencyError, OSError) as error:
        return None, str(error)
    return lock["revision"], "content verified against the lock (git metadata not required)"


# ------------------------------------------------------------------------------------------- telemetry

def default_quota_dir() -> Path:
    state = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local/state")
    return Path(state) / "herdr" / "plugins" / "herdr-agent-quota"


def telemetry_report(quota_dir: Path, max_age: float, now: float | None = None) -> dict[str, Any]:
    """Presence and age of the snapshot files only. Values are never read into the report; identity (session
    binding) is judged by the supervisor itself (`herdr-supervisor doctor`). Missing or stale means UNKNOWN."""
    moment = time.time() if now is None else now
    providers: dict[str, Any] = {}
    for provider, name in DEFAULT_SNAPSHOT_FILES.items():
        path = quota_dir / name
        try:
            stat_result = path.stat()
        except OSError:
            providers[provider] = {"state": "MISSING", "usage": "UNKNOWN", "action": "start a session so the plugin writes a snapshot, then run `herdr plugin action invoke refresh --plugin herdr-agent-quota` (a non-LLM action)"}
            continue
        try:
            json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            providers[provider] = {"state": "UNREADABLE", "usage": "UNKNOWN", "action": "the snapshot is not valid JSON; re-run the plugin refresh action"}
            continue
        age = max(0.0, moment - stat_result.st_mtime)
        if age > max_age:
            providers[provider] = {"state": "STALE", "usage": "UNKNOWN", "age_seconds": int(age), "action": "run the plugin refresh action; a stale snapshot is never treated as current usage"}
        else:
            providers[provider] = {"state": "FRESH", "age_seconds": int(age), "action": "none"}
    return {"quota_dir": str(quota_dir), "max_age_seconds": int(max_age), "providers": providers,
            "identity": "not evaluated here: the supervisor binds each snapshot to the exact session (`herdr-supervisor doctor`); another session's windows are never borrowed",
            "credentials_and_sessions": "not read: provider login, StatusLine setup and Herdr session binding are separate operator steps (see docs/QUOTA_DEPENDENCY.md)"}


def release_of(output: bytes) -> str | None:
    """The `release:` line of `rustc -vV` / `cargo -vV`."""
    match = re.search(r"^release: (\S+)$", output.decode("utf-8", "replace"), re.MULTILINE)
    return match.group(1) if match else None


def tool_release(tool: str, env: dict[str, str], cwd: Path | None) -> str | None:
    try:
        done = run([tool, "-vV"], env=env, cwd=cwd, timeout=20)
    except DependencyError:
        return None
    return release_of(done.stdout) if done.returncode == 0 else None


def toolchain_report(lock: dict[str, Any], env: dict[str, str], cwd: Path | None = None) -> dict[str, Any]:
    """The toolchain the build would actually execute, selected by the lock (never by an inherited override) and
    verified by the release each executable reports. Anything unverifiable is MISSING, not assumed."""
    channel = lock["toolchain"]["channel"]
    report: dict[str, Any] = {"channel": channel, "cargo_release": tool_release("cargo", env, cwd), "rustc_release": tool_release("rustc", env, cwd), "channel_installed": None}
    try:
        toolchains = run(["rustup", "toolchain", "list"], env=env, timeout=20)
        if toolchains.returncode == 0:
            report["channel_installed"] = any(line.split("-", 1)[0].split(" ", 1)[0] == channel for line in toolchains.stdout.decode("utf-8", "replace").splitlines())
    except DependencyError:
        pass
    install = f"rustup toolchain install {channel} --profile minimal (the build never auto-installs a toolchain)"
    for tool in ("cargo", "rustc"):
        release = report[f"{tool}_release"]
        if release is None:
            return {**report, "status": "MISSING", "action": install if report["channel_installed"] is not True else f"{tool} reports no verifiable release even though {channel} is installed; check the rustup/cargo home"}
        if release != channel:
            return {**report, "status": "MISSING", "action": f"{tool} reports release {release} but the lock requires exactly {channel}; inherited overrides are ignored, so install that toolchain with rustup ({install})"}
    return {**report, "status": "PRESENT", "action": "none"}


def compiler_releases_used(source: Path) -> list[str]:
    """Releases of the compiler(s) cargo itself recorded in target/.rustc_info.json, whatever configuration chose them."""
    try:
        outputs = json.loads((source / "target" / ".rustc_info.json").read_text(encoding="utf-8")).get("outputs", {})
        found = [release_of(item["stdout"].encode("utf-8")) for item in outputs.values() if isinstance(item, dict) and isinstance(item.get("stdout"), str)]
    except (OSError, ValueError, AttributeError):
        return []
    return [release for release in found if release is not None]


# --------------------------------------------------------------------------------------------- commands

def build_env(args: argparse.Namespace, lock: dict[str, Any]) -> dict[str, str]:
    """Scrubbed environment for every toolchain command: allowlisted variables only, the locked channel forced as the
    toolchain, toolchain auto-install off. RUSTC, RUSTFLAGS, wrappers, target-dir and toolchain overrides never pass."""
    env = {k: os.environ[k] for k in _BUILD_ENV_KEYS if k in os.environ}
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    env["RUSTUP_AUTO_INSTALL"] = "0"
    env["RUSTUP_TOOLCHAIN"] = lock["toolchain"]["channel"]
    if getattr(args, "cargo_home", None):
        env["CARGO_HOME"] = str(Path(args.cargo_home).resolve())
    if getattr(args, "rustup_home", None):
        env["RUSTUP_HOME"] = str(Path(args.rustup_home).resolve())
    return env


def command_check(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    lock = load_lock(args.lock)
    herdr = resolve_herdr(args.herdr)
    inventory = read_inventory(herdr)
    dependency = classify(inventory, lock)
    quota_dir = Path(args.quota_dir) if args.quota_dir else default_quota_dir()
    report: dict[str, Any] = {
        "command": "check", "mutations": "none",
        "lock": {"plugin_id": lock["plugin_id"], "repository": lock["repository"]["url"], "revision": lock["revision"], "manifest_version": lock["manifest_version"]},
        "herdr": {"path": herdr, "available": herdr is not None},
        "dependency": dependency,
        "telemetry": telemetry_report(quota_dir, float(args.max_age)),
        "toolchain": toolchain_report(lock, build_env(args, lock)),
    }
    state = dependency["state"]
    if state == "pinned":
        code = EXIT_OK
    elif state == "conflict":
        code = EXIT_REFUSED
    else:
        code = EXIT_ACTION
    if code == EXIT_OK and args.require_telemetry and any(p["state"] != "FRESH" for p in report["telemetry"]["providers"].values()):
        code = EXIT_ACTION
    return code, report


def validate_stage(stage_arg: str | None, require: bool) -> Path | None:
    if stage_arg is None:
        if require:
            raise DependencyError("--stage-dir is required with --apply", code=EXIT_USAGE, action="choose a new directory outside the Herdr plugin directory")
        return None
    stage = Path(stage_arg)
    if not stage.is_absolute():
        raise DependencyError("--stage-dir must be an absolute path", code=EXIT_USAGE, action="pass an absolute directory")
    herdr_plugins = (Path.home() / ".config" / "herdr").resolve()
    resolved = stage.resolve()
    if resolved == herdr_plugins or herdr_plugins in resolved.parents:
        raise DependencyError("--stage-dir is inside Herdr's configuration directory", code=EXIT_REFUSED, action="stage elsewhere; linking is a separate explicit step")
    if stage.is_symlink():
        raise DependencyError("--stage-dir is a symlink", code=EXIT_REFUSED, action="choose a real directory")
    if stage.exists():
        if not stage.is_dir() or any(stage.iterdir()):
            raise DependencyError("--stage-dir exists and is not an empty directory", code=EXIT_REFUSED, action="choose a new or empty directory; an existing one is never overwritten")
    elif not stage.parent.is_dir():
        raise DependencyError("the parent of --stage-dir does not exist", code=EXIT_ACTION, action="create the parent directory first")
    return stage


def remove_owned_dir(stage: Path, path: Path, owned: set[Path]) -> None:
    """Delete a directory only if this invocation created it: exact path recorded at creation, directly inside the
    stage directory, a real directory (never a symlink)."""
    if path not in owned or path.parent != stage or path.is_symlink() or not path.is_dir():
        raise DependencyError("refusing to remove a path this run did not create", action="inspect the stage directory manually")
    shutil.rmtree(path)


class StageClaim:
    """Exclusive ownership of the stage directory for one `bootstrap --apply` invocation.

    Ownership is established by creating things exclusively, never by inspecting names: a lock file (O_EXCL), a
    private temporary directory (mkdtemp), the final directory (mkdir) and the receipt (O_EXCL). Only what this
    invocation created is ever removed. A second invocation (even one that validated the empty directory before
    the first wrote anything) finds the lock or the first one's output and refuses without touching it.
    """

    def __init__(self, stage: Path, final_name: str) -> None:
        self.stage = stage
        self.final_name = final_name
        self.token = uuid.uuid4().hex
        self.lock_path = stage / LOCK_FILE
        self.partial: Path | None = None
        self.final: Path | None = None
        self.receipt: Path | None = None
        self.held = False

    def claim(self) -> None:
        self.stage.mkdir(mode=0o700, exist_ok=True)
        try:
            fd = os.open(self.lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except FileExistsError as error:
            raise DependencyError("another bootstrap owns this stage directory (or was interrupted and left its lock)", action="wait for it to finish; if none is running, choose a new --stage-dir (nothing in this one was touched)") from error
        except OSError as error:
            raise DependencyError(f"cannot claim the stage directory: {error.strerror}", code=EXIT_ACTION, action="check its permissions") from error
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(self.token + "\n")
        self.held = True
        foreign = sorted(entry.name for entry in self.stage.iterdir() if entry.name != LOCK_FILE)
        if foreign:
            self.release()
            raise DependencyError("--stage-dir holds content that is not this run's (another bootstrap finished first, or something else was written)", action="choose a new or empty directory; the existing content was not touched")

    def new_partial(self) -> Path:
        self.drop_partial()
        self.partial = Path(tempfile.mkdtemp(prefix=".partial-", dir=self.stage))
        return self.partial

    def drop_partial(self) -> None:
        if self.partial is not None and self.partial.exists():
            remove_owned_dir(self.stage, self.partial, {self.partial})
        self.partial = None

    def publish(self) -> Path:
        """Create the final directory exclusively, then move the verified source onto it."""
        final = self.stage / self.final_name
        try:
            os.mkdir(final, 0o700)
        except FileExistsError as error:
            raise DependencyError(f"{self.final_name} already exists in the stage directory", action="choose a new or empty directory; the existing one was not touched") from error
        self.final = final
        assert self.partial is not None
        try:
            os.rename(self.partial, final)  # replaces only the empty directory just created above
        except OSError as error:
            raise DependencyError(f"cannot move the verified source into place: {error.strerror}", action="the stage directory was changed by something else; nothing foreign was removed") from error
        self.partial = None
        return final

    def write_receipt(self, receipt: dict[str, Any]) -> Path:
        path = self.stage / RECEIPT_FILE
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
        except FileExistsError as error:
            raise DependencyError(f"{RECEIPT_FILE} already exists in the stage directory", action="choose a new or empty directory; the existing file was not touched") from error
        self.receipt = path
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
        return path

    def release(self) -> None:
        """Remove the lock, only if it still carries this invocation's token."""
        if self.held and self.lock_path.is_file() and not self.lock_path.is_symlink():
            try:
                if self.lock_path.read_text(encoding="utf-8").strip() == self.token:
                    self.lock_path.unlink()
            except OSError:
                pass
        self.held = False

    def abandon(self) -> None:
        """Failure path: remove exactly what this invocation created, then release the lock."""
        try:
            self.drop_partial()
            if self.final is not None and self.final.exists():
                remove_owned_dir(self.stage, self.final, {self.final})
            if self.receipt is not None and self.receipt.is_file() and not self.receipt.is_symlink():
                self.receipt.unlink()
        finally:
            self.release()


def source_plan(args: argparse.Namespace, lock: dict[str, Any]) -> list[tuple[str, Path | None, str]]:
    """Ordered acquisition attempts: (kind, artifact path, human label)."""
    chosen = [(label, kind, value) for label, kind, value in (("--from-archive", "archive", args.from_archive), ("--from-bundle", "bundle", args.from_bundle)) if value]
    if len(chosen) > 1:
        raise DependencyError("choose one of --from-archive and --from-bundle", code=EXIT_USAGE)
    if chosen:
        label, kind, value = chosen[0]
        return [(kind, Path(value), f"{label} {Path(value).name}")]
    plan: list[tuple[str, Path | None, str]] = [("fork", None, f"locked repository {args.fork_url or lock['repository']['url']} (exact commit)")]
    for kind, value in (("archive", args.fallback_archive), ("bundle", args.fallback_bundle)):
        if value:
            plan.append((kind, Path(value), f"fallback {kind} {Path(value).name}"))
    return plan


def validate_url(url: str) -> str:
    if _HTTPS_URL.match(url) or (url.startswith("file:///") and re.match(r"^file:///[A-Za-z0-9._/-]+$", url)) or (url.startswith("/") and re.match(r"^/[A-Za-z0-9._/-]+$", url)):
        return url
    raise DependencyError("--fork-url must be an https URL or a local path", code=EXIT_USAGE, action="other transports (ssh, ext) are refused")


def command_bootstrap(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    if args.apply and args.dry_run:
        raise DependencyError("--apply and --dry-run are mutually exclusive", code=EXIT_USAGE)
    apply = bool(args.apply)
    lock = load_lock(args.lock)
    url = validate_url(args.fork_url or lock["repository"]["url"])
    args.fork_url = url
    stage = validate_stage(args.stage_dir, require=apply)
    plan = source_plan(args, lock)
    report: dict[str, Any] = {"command": "bootstrap", "mode": "apply" if apply else "dry-run", "lock": {"plugin_id": lock["plugin_id"], "revision": lock["revision"], "manifest_version": lock["manifest_version"]},
                              "mutations": "none outside the new stage directory" if apply else "none", "never_performed": ["herdr plugin install/link/enable", "configure", "upstream install.sh", "settings or hook changes", "provider or model calls"]}
    herdr = resolve_herdr(args.herdr)
    dependency = classify(read_inventory(herdr), lock)
    report["dependency"] = dependency
    if dependency["state"] == "pinned":
        report["result"] = "nothing to do: the dependency is already installed at the locked revision and is preserved"
        return EXIT_OK, report
    if dependency["state"] == "conflict":
        report["result"] = "stopped: a human decision is required"
        return EXIT_REFUSED, report
    if dependency["state"] == "unknown":
        report["result"] = "stopped: the plugin inventory could not be read, so absence is not established"
        return EXIT_ACTION, report
    report["offline_note"] = "a source archive does not make the build offline: the pinned Rust toolchain and the crates in Cargo.lock must be installed or cached (use --offline with a populated cargo home)"
    env = build_env(args, lock)
    if args.no_build:
        report["build"] = "skipped (--no-build): the toolchain was not checked"
    else:
        toolchain = toolchain_report(lock, env)
        report["toolchain"] = toolchain
        if toolchain["status"] == "MISSING":
            report["result"] = "stopped: the build prerequisites are missing"
            report["action"] = toolchain["action"]
            return EXIT_ACTION, report
    final_name = f"herdr-agent-quota-{lock['revision'][:12]}"
    report["stage"] = {"directory": str(stage) if stage else None, "source_directory": f"{final_name}", "plan": [label for _, _, label in plan]}
    claim: StageClaim | None = None
    if apply and stage is not None:
        claim = StageClaim(stage, final_name)
        claim.claim()  # exclusive ownership before any acquisition work; a contender is refused here, untouched
    try:
        return run_bootstrap(args, lock, report, plan, url, stage, claim, final_name, env, apply)
    except BaseException:
        if claim is not None:
            claim.abandon()
        raise


def run_bootstrap(args: argparse.Namespace, lock: dict[str, Any], report: dict[str, Any], plan: list[tuple[str, Path | None, str]], url: str,
                  stage: Path | None, claim: StageClaim | None, final_name: str, env: dict[str, str], apply: bool) -> tuple[int, dict[str, Any]]:
    with tempfile.TemporaryDirectory(prefix="herdr-quota-dep-check-") as scratch_name:
        scratch = Path(scratch_name)
        attempts: list[str] = []
        acquired: Acquired | None = None
        failure: DependencyError | None = None
        for kind, path, label in plan:
            if kind == "fork" and not apply:
                attempts.append(f"{label}: not contacted in a dry run (network is used only with --apply)")
                continue
            if claim is not None:
                target = claim.new_partial()
            else:
                target = scratch / f"verify-{len(attempts)}"
                target.mkdir()
            try:
                acquired = acquire(lock, target, source=kind, path=path, url=url, fetch_timeout=float(args.fetch_timeout))
                attempts.append(f"{label}: verified against the lock")
                break
            except DependencyError as error:
                failure = error
                attempts.append(f"{label}: {error}")
                if error.code == EXIT_REFUSED and kind != "fork":
                    break  # a lock-listed artifact that fails verification is never skipped past silently
        report["attempts"] = attempts
        if acquired is None:
            if claim is not None:
                claim.abandon()
            if not apply:
                report["result"] = "dry run: nothing verified yet; only a lock-listed local artifact can be verified without the network" if failure is None or failure.code != EXIT_REFUSED else "stopped: verification failed"
                return (EXIT_REFUSED if failure is not None and failure.code == EXIT_REFUSED else EXIT_OK), report
            code = failure.code if failure is not None else EXIT_ACTION
            report["result"] = "stopped: no verified source; nothing was staged"
            report["action"] = failure.action if failure is not None else "supply a recovery artifact"
            return code, report
        report["source"] = {"used": acquired.source, "tree_sha1": acquired.verified["tree_sha1"], "content_manifest_sha256": acquired.verified["content_manifest_sha256"], "artifact_sha256": acquired.artifact_sha256}
        if claim is None or stage is None:
            report["result"] = "dry run: the local artifact verifies against the lock; nothing was written outside a removed temporary directory"
            report["next_steps"] = next_steps(f"{stage or '<stage-dir>'}/{final_name}", lock, dry=True)
            return EXIT_OK, report
        try:
            final = claim.publish()  # the build runs in the final location so its cached paths stay valid
            receipt: dict[str, Any] = {"plugin_id": lock["plugin_id"], "revision": lock["revision"], "tree_sha1": acquired.verified["tree_sha1"], "source": acquired.source, "build": "skipped"}
            if not args.no_build:
                receipt["build"] = build_locked(args, lock, env, final)
            receipt_path = claim.write_receipt(receipt)
        except OSError as error:
            claim.abandon()
            raise DependencyError(f"cannot write the staged result: {error.strerror}", code=EXIT_ACTION, action="check the stage directory permissions and space") from error
        except DependencyError as error:
            claim.abandon()
            report["result"] = "stopped: nothing was staged"
            report["action"] = error.action
            report["error"] = str(error)
            return error.code, report
        claim.release()
        report["result"] = f"staged and verified {final_name}"
        report["receipt"] = str(receipt_path)
        report["next_steps"] = next_steps(str(final), lock, dry=False)
    return EXIT_OK, report


def build_locked(args: argparse.Namespace, lock: dict[str, Any], env: dict[str, str], source: Path) -> dict[str, Any]:
    """Run the locked build and prove, before anything is published, that the locked toolchain actually executed it."""
    channel = lock["toolchain"]["channel"]
    selected = toolchain_report(lock, env, source)
    if selected["status"] != "PRESENT":
        raise DependencyError(f"the toolchain selected for the staged source is not the locked {channel}: {selected['action']}", code=EXIT_ACTION, action=selected["action"])
    build_args = list(lock["toolchain"]["build_command"]) + (["--offline"] if args.offline else [])
    built = run(build_args, env=env, cwd=source, timeout=float(args.build_timeout))
    if built.returncode != 0:
        raise DependencyError(f"`{' '.join(build_args)}` failed (exit {built.returncode}): {tail(built.stderr or built.stdout)}", code=EXIT_ACTION,
                              action="fix the toolchain/crate availability (see offline_note) and re-run; the unverified build output was removed")
    binary = source / "target" / "release" / "herdr-agent-quota"
    if not binary.is_file():
        raise DependencyError("the build finished without the expected plugin binary", code=EXIT_ACTION, action="inspect the build output; nothing was staged")
    used = compiler_releases_used(source)
    if not used or any(release != channel for release in used):
        raise DependencyError(f"the compiler cargo recorded using ({', '.join(sorted(set(used))) or 'unknown'}) is not the locked {channel}", code=EXIT_ACTION,
                              action="a cargo configuration file or wrapper selected another compiler; use a clean --cargo-home and re-run; the build output was removed")
    return {"command": build_args, "binary_sha256": sha256_file(binary), "toolchain_env": channel, "cargo_release": selected["cargo_release"], "rustc_release": selected["rustc_release"], "compiler_releases_used": sorted(set(used))}


def next_steps(directory: str, lock: dict[str, Any], dry: bool) -> list[str]:
    plugin = lock["plugin_id"]
    return [
        "Review the verified source and BOOTSTRAP_RECEIPT.json; these commands were NOT run:" if not dry else "These commands are not run by this tool:",
        f"herdr plugin link --disabled {directory}   # registers the staged plugin without enabling it (live link NOT VALIDATED here)",
        f"herdr plugin enable {plugin}   # explicit operator step; starts the plugin's startup/event hooks",
        f"herdr plugin action invoke configure --plugin {plugin}   # OPTIONAL and separate: edits provider hooks/settings; read docs/QUOTA_DEPENDENCY.md first",
        "herdr-quota-dependency check   # verify the installed revision and snapshot freshness afterwards",
    ]


def command_show_lock(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    lock = load_lock(args.lock)
    return EXIT_OK, {"command": "show-lock", "lock": lock}


# ------------------------------------------------------------------------------------------- rendering

def render(report: dict[str, Any]) -> str:
    lines = [f"herdr-quota-dependency {report['command']}" + (f" ({report['mode']})" if "mode" in report else "")]
    if report["command"] == "show-lock":
        lines.append(json.dumps(report["lock"], indent=2, sort_keys=True))
        return "\n".join(lines)
    lock = report["lock"]
    lines.append(f"locked: {lock['plugin_id']} {lock['manifest_version']} @ {lock['revision']}")
    if "herdr" in report:
        lines.append(f"herdr: {report['herdr']['path'] or 'NOT FOUND'}")
    dependency = report["dependency"]
    lines.append(f"dependency: {dependency['state'].upper()} - {dependency['detail']}")
    if dependency["action"] != "none; the existing installation is preserved" and not (report["command"] == "bootstrap" and dependency["state"] == "absent"):
        lines.append(f"  action: {dependency['action']}")
    if "telemetry" in report:
        telemetry = report["telemetry"]
        for provider, item in telemetry["providers"].items():
            age = f" ({item['age_seconds']} s old)" if "age_seconds" in item else ""
            lines.append(f"telemetry {provider}: {item['state']}{age}" + (f" - usage {item['usage']}; {item['action']}" if item["state"] != "FRESH" else ""))
        lines.append(f"  identity: {telemetry['identity']}")
        lines.append(f"  credentials/sessions: {telemetry['credentials_and_sessions']}")
    if "toolchain" in report:
        toolchain = report["toolchain"]
        lines.append(f"toolchain {toolchain['channel']}: {toolchain['status']}" + ("" if toolchain["action"] == "none" else f" - {toolchain['action']}"))
    if "offline_note" in report:
        lines.append(f"note: {report['offline_note']}")
    if "build" in report:
        lines.append(f"build: {report['build']}")
    for attempt in report.get("attempts", []):
        lines.append(f"source: {attempt}")
    if "source" in report:
        lines.append(f"verified: tree {report['source']['tree_sha1']} via {report['source']['used']}")
    if "result" in report:
        lines.append(f"result: {report['result']}")
    if "error" in report:
        lines.append(f"error: {report['error']}")
    if "action" in report:
        lines.append(f"action: {report['action']}")
    lines.extend(report.get("next_steps", []))
    lines.append(f"mutations: {report['mutations']}")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="herdr-quota-dependency", description="Check or stage the pinned herdr-agent-quota plugin dependency (never installs, links, configures or contacts a provider).")
    parser.add_argument("--lock", help="dependency lock path (default: beside the program files or in the source tree)")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check", help="read-only: dependency state, snapshot freshness, build prerequisites")
    check.add_argument("--herdr", help="Herdr executable (default: HERDR_BIN_PATH, ~/.local/bin/herdr, PATH)")
    check.add_argument("--quota-dir", help="snapshot directory (default: $XDG_STATE_HOME/herdr/plugins/herdr-agent-quota)")
    check.add_argument("--max-age", type=float, default=DEFAULT_MAX_AGE_SECONDS, help="seconds before a snapshot is stale")
    check.add_argument("--require-telemetry", action="store_true", help="exit 3 unless every provider snapshot is FRESH")
    check.add_argument("--cargo-home", help=argparse.SUPPRESS)
    check.add_argument("--rustup-home", help=argparse.SUPPRESS)
    boot = sub.add_parser("bootstrap", help="stage and verify the locked source in a new directory (dry run unless --apply)")
    boot.add_argument("--stage-dir", help="new or empty absolute directory for the staged source")
    boot.add_argument("--dry-run", action="store_true", help="explicit dry run (the default)")
    boot.add_argument("--apply", action="store_true", help="stage, verify and build")
    boot.add_argument("--from-archive", help="use this lock-listed source archive instead of the repository")
    boot.add_argument("--from-bundle", help="use this lock-listed git bundle instead of the repository")
    boot.add_argument("--fallback-archive", help="used only if the repository cannot supply the locked commit")
    boot.add_argument("--fallback-bundle", help="used only if the repository cannot supply the locked commit")
    boot.add_argument("--fork-url", help="alternative https URL or local path serving the locked commit (content is still verified against the lock)")
    boot.add_argument("--no-build", action="store_true", help="stage and verify only")
    boot.add_argument("--offline", action="store_true", help="pass --offline to cargo (requires cached crates)")
    boot.add_argument("--cargo-home", help="cargo home for the build (default: inherited)")
    boot.add_argument("--rustup-home", help="rustup home for the build (default: inherited)")
    boot.add_argument("--fetch-timeout", type=float, default=120, help="seconds allowed for the repository fetch")
    boot.add_argument("--build-timeout", type=float, default=1800, help="seconds allowed for the build")
    boot.add_argument("--herdr", help="Herdr executable (default: HERDR_BIN_PATH, ~/.local/bin/herdr, PATH)")
    sub.add_parser("show-lock", help="print the validated dependency lock")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handlers = {"check": command_check, "bootstrap": command_bootstrap, "show-lock": command_show_lock}
    try:
        code, report = handlers[args.command](args)
    except DependencyError as error:
        message = {"command": args.command, "error": str(error), "action": error.action, "mutations": "none"}
        print(json.dumps(message, indent=2) if args.json else f"error: {error}" + (f"\naction: {error.action}" if error.action else ""), file=sys.stderr)
        return error.code
    print(json.dumps(report, indent=2, sort_keys=True) if args.json else render(report))
    return code


if __name__ == "__main__":
    sys.exit(main())
