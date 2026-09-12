#!/usr/bin/env python3
"""Optional, deterministic backup and restore for herdr-supervisor installations.

Disabled by default; when enabled it never affects task progress. Strategies: SSH snapshots (fixed
`rsync`/`ssh` argv, key + host-key verification, staged upload → remote verification → completion marker →
latest pointer) and Git recovery assessment (reports what a remote protects vs. local-only work; optional
`git bundle`; never commits, pushes, merges, rebases, or rewrites history). Restores go only to absent
destinations. Secrets are excluded by default and never claimed recoverable. No LLM is ever involved.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from herdr_supervisor import Paths, SupervisorError, _number, atomic_write_json, iso_utc, load_json

VERSION = "0.3.0-beta.1"
STRATEGIES = ("ssh_snapshot", "git_recovery", "both")
DEFAULT_EXCLUSIONS = [
    "**/.env", "**/.env.*", "**/bot-token", "**/*.pem", "**/*.key", "**/id_rsa*", "**/id_ed25519*", "**/*.p12", "**/*.pfx",
    "**/.ssh/**", "**/.gnupg/**", "**/.aws/**", "**/.netrc", "**/credentials*", "**/.credentials.json", "**/cookies*",
    "**/.config/herdr-telegram/**", "**/herdr-telegram-locks/**", "**/.claude/.credentials.json", "**/.codex/auth.json",
    "**/.mozilla/**", "**/.config/google-chrome/**", "**/.config/chromium/**", "**/wireguard/**", "**/openvpn/**",
    "**/*.lock", "**/*.sock", "**/__pycache__/**", "**/node_modules/**", "**/.git/**",
    "**/herdr-supervisor/inbox/processing/**", "**/herdr-supervisor/outbox/delivery/**",
]
EXCLUDED_CATEGORIES = ["Telegram bot token", "OAuth/API tokens", "cookies", "SSH private keys", "VPN keys", ".env secrets", "Claude/Codex authentication credentials", "browser profiles", "supervisor live locks and transient delivery state"]
_DEST_RE = re.compile(r"^(?P<user>[a-z_][a-z0-9_.-]{0,31})@(?P<host>[A-Za-z0-9.-]{1,253}):(?P<path>/[A-Za-z0-9._/-]{1,400})$")
_BACKUP_ID_RE = re.compile(r"[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}")
_SECRET_CONTENT_RE = re.compile(
    rb"(?im)(?<![A-Za-z0-9_])(?:export\s+)?[\"']?(?:api[_-]?key|access[_-]?token|refresh[_-]?token|"
    rb"password|passwd|secret|client[_-]?secret|bot[_-]?token|aws[_-]?(?:access[_-]?key[_-]?id|"
    rb"secret[_-]?access[_-]?key)|database[_-]?url|credentials?)[\"']?\s*[:=]|"
    rb"\b(?:AKIA|ASIA|AIDA|AROA|AIPA|ANPA|ANVA|ASCA)[A-Z0-9]{16}\b|"
    rb"\b[a-z][a-z0-9+.-]{1,20}://[^\s/:@]+:[^\s/@]+@[^\s/]+|"
    rb"\b\d{6,12}:[A-Za-z0-9_-]{20,}\b|"
    rb"-----BEGIN (?:OPENSSH |RSA |EC |DSA )?PRIVATE KEY-----"
)


def backup_dir(paths: Paths) -> Path:
    return paths.state_dir / "backup"


def backup_state_file(paths: Paths) -> Path:
    return backup_dir(paths) / "state.json"


def backup_config_file(paths: Paths) -> Path:
    return paths.config_file.parent / "backup.json"


DEFAULT_BACKUP_CONFIG: dict[str, Any] = {
    "schema_version": 1,
    "enabled": False,
    "strategy": "ssh_snapshot",
    "sources": [],
    "exclusions": [],
    "destination": None,  # "user@host:/absolute/path" for ssh_snapshot
    "schedule": {"daily": True, "weekly": False, "interval_hours": None},
    "retention": {"daily": 7, "weekly": 4},
    "max_age_hours": 24,
    "notify_telegram": False,
    "git_repos": [],  # [{"path": "...", "remote": "origin", "branch": "main", "bundle": true}]
    "ssh_options": ["-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes"],
}

_SAFE_SSH_OPTIONS = {
    "BatchMode=yes",
    "StrictHostKeyChecking=yes",
}


def load_backup_config(path: Path) -> dict[str, Any]:
    config = json.loads(json.dumps(DEFAULT_BACKUP_CONFIG))
    if path.exists():
        raw = load_json(path, label="backup configuration")
        if not isinstance(raw, dict):
            raise SupervisorError("backup configuration must be a JSON object")
        unknown = set(raw) - set(DEFAULT_BACKUP_CONFIG)
        if unknown:
            raise SupervisorError(f"unknown backup configuration keys: {', '.join(sorted(unknown))}")
        config.update(raw)
    return validate_backup_config(config)


def parse_destination(text: Any) -> dict[str, str]:
    if not isinstance(text, str) or not (match := _DEST_RE.match(text)) or ".." in text or "$" in text or "`" in text or ";" in text:
        raise SupervisorError("destination must look like user@host:/absolute/path (no shell syntax, no traversal)")
    path = match.group("path")
    if path in ("/", "/root", "/home") or path.count("/") < 2:
        raise SupervisorError("destination path must be a dedicated directory at least two levels deep")
    return {"user": match.group("user"), "host": match.group("host"), "path": path.rstrip("/")}


def validate_backup_config(config: dict[str, Any]) -> dict[str, Any]:
    unknown = set(config) - set(DEFAULT_BACKUP_CONFIG)
    if unknown:
        raise SupervisorError(f"unknown backup configuration keys: {', '.join(sorted(unknown))}")
    if config.get("schema_version") != 1:
        raise SupervisorError("unsupported backup configuration schema")
    if not isinstance(config.get("enabled"), bool):
        raise SupervisorError("backup.enabled must be a boolean")
    if config.get("strategy") not in STRATEGIES:
        raise SupervisorError(f"backup.strategy must be one of {', '.join(STRATEGIES)}")
    sources = config.get("sources")
    if not isinstance(sources, list) or any(not isinstance(s, str) or not Path(s).is_absolute() for s in sources):
        raise SupervisorError("backup.sources must be a list of absolute paths")
    source_names = [Path(source).name for source in sources]
    if any(not name for name in source_names) or len(source_names) != len(set(source_names)):
        raise SupervisorError("backup sources must have unique, non-empty directory names")
    broad_sources = {Path("/"), Path("/home"), Path.home().resolve()}
    if any(Path(source).resolve() in broad_sources for source in sources):
        raise SupervisorError("backup sources must be selected paths, not /, /home, or the whole user home")
    if not isinstance(config.get("exclusions"), list) or any(not isinstance(x, str) for x in config["exclusions"]):
        raise SupervisorError("backup.exclusions must be a list of glob strings")
    schedule = config.get("schedule")
    if isinstance(schedule, dict) and set(schedule) - {"daily", "weekly", "interval_hours"}:
        raise SupervisorError("backup.schedule contains unknown keys")
    if not isinstance(schedule, dict) or not isinstance(schedule.get("daily"), bool) or not isinstance(schedule.get("weekly"), bool):
        raise SupervisorError("backup.schedule needs boolean daily/weekly")
    interval = schedule.get("interval_hours")
    if interval is not None and (_number(interval) is None or not 1 <= float(interval) <= 24 * 30):
        raise SupervisorError("backup.schedule.interval_hours must be 1-720 or null")
    if config["enabled"] and not (schedule["daily"] or schedule["weekly"] or interval):
        raise SupervisorError("an enabled backup needs daily, weekly, or an interval")
    retention = config.get("retention")
    if isinstance(retention, dict) and set(retention) - {"daily", "weekly"}:
        raise SupervisorError("backup.retention contains unknown keys")
    if not isinstance(retention, dict) or any((lambda n: n is None or n < 1)(_number(retention.get(k))) for k in ("daily", "weekly")):
        raise SupervisorError("backup.retention.daily/weekly must be positive numbers")
    if _number(config.get("max_age_hours")) is None or not 1 <= float(config["max_age_hours"]) <= 24 * 60:
        raise SupervisorError("backup.max_age_hours must be 1-1440")
    if not isinstance(config.get("notify_telegram"), bool):
        raise SupervisorError("backup.notify_telegram must be a boolean")
    if config["strategy"] in ("ssh_snapshot", "both"):
        if config["enabled"]:
            parse_destination(config.get("destination"))
            if not sources:
                raise SupervisorError("ssh_snapshot needs at least one source")
    repos = config.get("git_repos")
    if not isinstance(repos, list):
        raise SupervisorError("backup.git_repos must be a list")
    for repo in repos:
        if not isinstance(repo, dict) or not isinstance(repo.get("path"), str) or not repo["path"].startswith("/"):
            raise SupervisorError("each git repo needs an absolute path")
        if set(repo) - {"path", "remote", "branch", "bundle", "url"}:
            raise SupervisorError("git repo contains unknown keys")
        for key in ("remote", "branch"):
            if not isinstance(repo.get(key, "origin" if key == "remote" else "main"), str) or not re.fullmatch(r"[A-Za-z0-9._/-]{1,100}", str(repo.get(key, "origin"))):
                raise SupervisorError(f"git repo {key} is invalid")
        if "bundle" in repo and not isinstance(repo["bundle"], bool):
            raise SupervisorError("git repo bundle must be a boolean")
    if config["enabled"] and config["strategy"] in ("git_recovery", "both") and not repos:
        raise SupervisorError("git_recovery needs at least one configured repository")
    options = config.get("ssh_options")
    if not isinstance(options, list) or len(options) % 2 or any(options[i] != "-o" or options[i + 1] not in _SAFE_SSH_OPTIONS for i in range(0, len(options), 2)):
        raise SupervisorError("ssh_options may contain only BatchMode=yes and StrictHostKeyChecking=yes")
    return config


# --------------------------------------------------------------------------- state / health


def load_backup_state(paths: Paths) -> dict[str, Any]:
    path = backup_state_file(paths)
    if not path.exists():
        return {"schema_version": 1, "last_attempt": None, "last_success": None, "last_verified": None, "last_backup_id": None, "last_verified_id": None, "failure_reason": None, "history": []}
    value = load_json(path, label="backup state")
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise SupervisorError("backup state is corrupt")
    return value


def save_backup_state(paths: Paths, state: dict[str, Any]) -> None:
    backup_dir(paths).mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(backup_dir(paths), 0o700)
    atomic_write_json(backup_state_file(paths), state)


def _iso_to_unix(value: Any) -> float | None:
    if not isinstance(value, str):
        return None
    import datetime as dt  # noqa: PLC0415

    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def health(config: dict[str, Any], state: dict[str, Any], now: float) -> dict[str, Any]:
    """disabled | healthy | stale | failed | unverified — successful and verified are distinct; a Git remote
    alone never makes the workspace 'protected'."""
    if not config.get("enabled"):
        return {"status": "disabled", "strategy": None, "protects": [], "not_protected": []}
    strategy = config["strategy"]
    protects = []
    not_protected = list(EXCLUDED_CATEGORIES)
    if strategy in ("ssh_snapshot", "both"):
        protects.append("configured source snapshots (files present at backup time, secrets excluded)")
    if strategy in ("git_recovery", "both"):
        protects.append("committed refs that exist on the configured Git remotes")
        not_protected.append("uncommitted, untracked, ignored, and local-only Git work (unless included in a snapshot source)")
    verified_at = _iso_to_unix(state.get("last_verified"))
    success_at = _iso_to_unix(state.get("last_success"))
    max_age = float(config["max_age_hours"]) * 3600
    report = {"strategy": strategy, "destination_class": "ssh remote" if strategy != "git_recovery" else "git remotes", "last_attempt": state.get("last_attempt"), "last_success": state.get("last_success"), "last_verified": state.get("last_verified"), "last_backup_id": state.get("last_backup_id"), "last_verified_id": state.get("last_verified_id"), "failure_reason": state.get("failure_reason"), "max_age_hours": config["max_age_hours"], "protects": protects, "not_protected": not_protected}
    if verified_at is None:
        report["status"] = "failed" if state.get("failure_reason") and success_at is None else ("unverified" if success_at else "failed")
        report["age_hours"] = None if success_at is None else (now - success_at) / 3600
        return report
    age = now - verified_at
    report["age_hours"] = age / 3600
    if age > max_age:
        report["status"] = "stale"
    elif state.get("failure_reason") and (_iso_to_unix(state.get("last_attempt")) or 0) > verified_at:
        report["status"] = "failed"
    else:
        report["status"] = "healthy"
    return report


def due(config: dict[str, Any], state: dict[str, Any], now: float) -> bool:
    """Schedule policy evaluated by the hourly timer: daily, weekly, both, or a fixed interval."""
    if not config.get("enabled"):
        return False
    last = _iso_to_unix(state.get("last_success")) or 0
    schedule = config["schedule"]
    interval = schedule.get("interval_hours")
    if interval:
        return now - last >= float(interval) * 3600
    period = 86400 if schedule.get("daily") else 7 * 86400
    return now - last >= period


# --------------------------------------------------------------------------- transports


class Transport:
    """Remote store abstraction. Layout: <root>/incoming/<id>/ (staging), <root>/snapshots/<id>/, <root>/latest."""

    def upload(self, staging: Path, backup_id: str) -> None: raise NotImplementedError
    def remote_manifest_check(self, backup_id: str, evidence: dict[str, Any]) -> bool: raise NotImplementedError
    def complete(self, backup_id: str) -> None: raise NotImplementedError
    def list_snapshots(self) -> list[str]: raise NotImplementedError
    def download(self, backup_id: str, destination: Path) -> None: raise NotImplementedError
    def delete(self, backup_id: str) -> None: raise NotImplementedError
    def check(self) -> None: raise NotImplementedError
    describe = "abstract"


class SshRsyncTransport(Transport):
    """Fixed argv, no shell; `runner` is injectable (tests never contact a real host)."""

    describe = "ssh_rsync"

    def __init__(self, destination: dict[str, str], ssh_options: list[str], runner: Callable[..., Any] | None = None, timeout: int = 3600) -> None:
        self.dest = destination
        self.ssh_options = list(ssh_options)
        self.runner = runner or self._run
        self.timeout = timeout

    @staticmethod
    def _run(argv: list[str], *, timeout: int) -> subprocess.CompletedProcess:
        return subprocess.run(argv, check=False, capture_output=True, text=True, timeout=timeout)

    def _target(self) -> str:
        return f"{self.dest['user']}@{self.dest['host']}"

    def _ssh(self, *remote_argv: str) -> list[str]:
        return ["ssh", *self.ssh_options, self._target(), "--", *remote_argv]

    def _exec(self, argv: list[str]) -> subprocess.CompletedProcess:
        completed = self.runner(argv, timeout=self.timeout)
        if completed.returncode != 0:
            raise SupervisorError(f"{argv[0]} failed ({completed.returncode}): {(completed.stderr or '')[:200]}")
        return completed

    def check(self) -> None:
        self._exec(self._ssh("mkdir", "-p", f"{self.dest['path']}/incoming", f"{self.dest['path']}/snapshots"))

    def upload(self, staging: Path, backup_id: str) -> None:
        validate_backup_id(backup_id)
        self.check()
        self._exec(["rsync", "-a", "--checksum", "--delete", "-e", " ".join(["ssh", *self.ssh_options]), f"{staging}/", f"{self._target()}:{self.dest['path']}/incoming/{backup_id}/"])

    def remote_manifest_check(self, backup_id: str, evidence: dict[str, Any]) -> bool:
        validate_backup_id(backup_id)
        checksums_digest = evidence.get("checksums_sha256")
        manifest_digest = evidence.get("manifest_sha256")
        if not all(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) for value in (checksums_digest, manifest_digest)):
            raise SupervisorError("local backup verification envelope is invalid")
        # CHECKSUMS is first bound to the locally retained digest.  Only then may it define the
        # expected payload list.  The exact regular-file set is compared before publication, and
        # links/devices are rejected. Destination/path components have already passed strict syntax checks.
        command = (
            f"cd {self.dest['path']}/incoming/{backup_id} && "
            f"test \"$(sha256sum CHECKSUMS | cut -d' ' -f1)\" = {checksums_digest} && "
            f"test \"$(sha256sum MANIFEST.json | cut -d' ' -f1)\" = {manifest_digest} && "
            "sha256sum -c --strict --quiet CHECKSUMS && "
            "test -z \"$(find . \\( -type l -o \\( ! -type d ! -type f \\) \\) -print -quit)\" && "
            "test \"$(find . -type f -printf '%P\\n' | LC_ALL=C sort)\" = "
            "\"$({ sed -n 's/^[0-9a-f]\\{64\\}  //p' CHECKSUMS; printf '%s\\n' CHECKSUMS; } | LC_ALL=C sort)\" && "
            "sha256sum CHECKSUMS MANIFEST.json && echo VERIFIED"
        )
        completed = self._exec(self._ssh("sh", "-c", command))
        observed: dict[str, str] = {}
        for line in (completed.stdout or "").splitlines():
            match = re.fullmatch(r"([0-9a-f]{64})\s+(CHECKSUMS|MANIFEST\.json)", line.strip())
            if match:
                observed[match.group(2)] = match.group(1)
        return (
            "VERIFIED" in (completed.stdout or "").splitlines()
            and observed.get("CHECKSUMS") == evidence.get("checksums_sha256")
            and observed.get("MANIFEST.json") == evidence.get("manifest_sha256")
        )

    def complete(self, backup_id: str) -> None:
        validate_backup_id(backup_id)
        base = self.dest["path"]
        self._exec(self._ssh("sh", "-c", f"mv {base}/incoming/{backup_id} {base}/snapshots/{backup_id} && ln -sfn snapshots/{backup_id} {base}/latest.tmp && mv -T {base}/latest.tmp {base}/latest"))

    def list_snapshots(self) -> list[str]:
        completed = self._exec(self._ssh("ls", "-1", f"{self.dest['path']}/snapshots"))
        return sorted(line.strip() for line in (completed.stdout or "").splitlines() if re.fullmatch(r"[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}", line.strip()))

    def download(self, backup_id: str, destination: Path) -> None:
        validate_backup_id(backup_id)
        self._exec(["rsync", "-a", "-e", " ".join(["ssh", *self.ssh_options]), f"{self._target()}:{self.dest['path']}/snapshots/{backup_id}/", f"{destination}/"])

    def delete(self, backup_id: str) -> None:
        validate_backup_id(backup_id)
        self._exec(self._ssh("rm", "-rf", "--", f"{self.dest['path']}/snapshots/{backup_id}"))


class LocalDirTransport(Transport):
    """A local directory playing the remote (fixtures, verification rehearsals). Same layout and semantics."""

    describe = "local_dir"

    def __init__(self, root: Path) -> None:
        self.root = root

    def check(self) -> None:
        (self.root / "incoming").mkdir(parents=True, exist_ok=True)
        (self.root / "snapshots").mkdir(parents=True, exist_ok=True)

    def upload(self, staging: Path, backup_id: str) -> None:
        validate_backup_id(backup_id)
        self.check()
        target = self.root / "incoming" / backup_id
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(staging, target, symlinks=False)

    def remote_manifest_check(self, backup_id: str, evidence: dict[str, Any]) -> bool:
        validate_backup_id(backup_id)
        return verify_tree(
            self.root / "incoming" / backup_id,
            expected_manifest_sha256=evidence.get("manifest_sha256"),
            expected_checksums_sha256=evidence.get("checksums_sha256"),
        )

    def complete(self, backup_id: str) -> None:
        validate_backup_id(backup_id)
        src, dst = self.root / "incoming" / backup_id, self.root / "snapshots" / backup_id
        if dst.exists():
            raise SupervisorError("snapshot already exists; never replaced")
        os.rename(src, dst)
        latest_tmp = self.root / "latest.tmp"
        if latest_tmp.is_symlink() or latest_tmp.exists():
            latest_tmp.unlink()
        latest_tmp.symlink_to(f"snapshots/{backup_id}")
        os.replace(latest_tmp, self.root / "latest")

    def list_snapshots(self) -> list[str]:
        snapshots = self.root / "snapshots"
        return sorted(p.name for p in snapshots.iterdir() if p.is_dir() and _BACKUP_ID_RE.fullmatch(p.name)) if snapshots.exists() else []

    def download(self, backup_id: str, destination: Path) -> None:
        validate_backup_id(backup_id)
        shutil.copytree(self.root / "snapshots" / backup_id, destination, symlinks=True)

    def delete(self, backup_id: str) -> None:
        validate_backup_id(backup_id)
        target = (self.root / "snapshots" / backup_id).resolve()
        if self.root.resolve() not in target.parents:
            raise SupervisorError("refusing to delete outside the managed snapshots root")
        shutil.rmtree(target)


def validate_backup_id(backup_id: Any) -> str:
    if not isinstance(backup_id, str) or not _BACKUP_ID_RE.fullmatch(backup_id):
        raise SupervisorError("invalid backup id")
    return backup_id


def verify_tree(directory: Path, *, expected_manifest_sha256: str | None = None, expected_checksums_sha256: str | None = None) -> bool:
    checksums = directory / "CHECKSUMS"
    if not checksums.is_file() or checksums.is_symlink():
        return False
    root = directory.resolve()
    try:
        if expected_checksums_sha256 and hashlib.sha256(checksums.read_bytes()).hexdigest() != expected_checksums_sha256:
            return False
    except OSError:
        return False
    seen: set[str] = set()
    try:
        lines = checksums.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return False
    for line in lines:
        digest, _, rel = line.partition("  ")
        candidate = Path(rel)
        if not re.fullmatch(r"[0-9a-f]{64}", digest) or not rel or candidate.is_absolute() or ".." in candidate.parts or rel in seen:
            return False
        seen.add(rel)
        target = directory / candidate
        try:
            resolved = target.resolve(strict=True)
        except OSError:
            return False
        if root not in resolved.parents or target.is_symlink() or not target.is_file() or not stat.S_ISREG(target.lstat().st_mode) or hashlib.sha256(target.read_bytes()).hexdigest() != digest:
            return False
    if "MANIFEST.json" not in seen:
        return False
    if expected_manifest_sha256 and hashlib.sha256((directory / "MANIFEST.json").read_bytes()).hexdigest() != expected_manifest_sha256:
        return False
    actual: set[str] = set()
    try:
        for node in directory.rglob("*"):
            rel = node.relative_to(directory).as_posix()
            mode = node.lstat().st_mode
            if stat.S_ISLNK(mode) or (not stat.S_ISDIR(mode) and not stat.S_ISREG(mode)):
                return False
            if stat.S_ISREG(mode):
                actual.add(rel)
    except OSError:
        return False
    return actual == seen | {"CHECKSUMS"}


# --------------------------------------------------------------------------- snapshot creation


def _excluded(rel: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch("/" + rel, pat) for pat in patterns)


def _contains_secret(path: Path) -> bool:
    """Conservative content check for common plaintext assignments and private-key headers."""
    try:
        overlap = b""
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                sample = overlap + chunk
                if _SECRET_CONTENT_RE.search(sample) is not None:
                    return True
                overlap = sample[-256:]
        return False
    except OSError as error:
        raise SupervisorError(f"cannot inspect backup source file: {path}: {error}") from error


def git_assess(repo: Path, remote: str = "origin", branch: str = "main") -> dict[str, Any]:
    """Read-only Git status plus live remote-ref evidence; never fetches or mutates refs."""
    def git(*args: str) -> str:
        completed = subprocess.run(["git", "-C", str(repo), *args], check=False, capture_output=True, text=True, timeout=60)
        if completed.returncode != 0:
            raise SupervisorError(f"git {' '.join(args[:2])} failed: {(completed.stderr or '')[:120]}")
        return completed.stdout

    out: dict[str, Any] = {"path": str(repo), "remote": remote, "branch": branch}
    try:
        out["head"] = git("rev-parse", "HEAD").strip()
    except SupervisorError:
        out["head"] = None
    remotes = git("remote").split()
    out["remote_configured"] = remote in remotes
    out["remote_checked"] = False
    out["remote_head"] = None
    out["remote_check_error"] = None
    if out["remote_configured"]:
        completed = subprocess.run(
            ["git", "-C", str(repo), "ls-remote", "--exit-code", "--refs", remote, f"refs/heads/{branch}"],
            check=False, capture_output=True, text=True, timeout=60,
        )
        lines = (completed.stdout or "").splitlines()
        expected_ref = f"refs/heads/{branch}"
        matches = [line.split("\t", 1)[0] for line in lines if "\t" in line and line.split("\t", 1)[1] == expected_ref]
        if completed.returncode == 0 and len(matches) == 1 and re.fullmatch(r"[0-9a-f]{40,64}", matches[0]):
            out["remote_checked"] = True
            out["remote_head"] = matches[0]
        else:
            out["remote_check_error"] = "remote unavailable or configured branch is missing"
    status = git("status", "--porcelain=v1", "--ignored").splitlines()
    out["modified"] = sum(1 for l in status if l[:2] not in ("??", "!!") and l.strip())
    out["untracked"] = sum(1 for l in status if l.startswith("??"))
    out["ignored"] = sum(1 for l in status if l.startswith("!!"))
    out["clean"] = out["modified"] == 0 and out["untracked"] == 0
    out["ahead"] = out["behind"] = None
    out["local_only_branches"] = []
    if out["remote_configured"]:
        try:
            counts = git("rev-list", "--left-right", "--count", f"{remote}/{branch}...HEAD").split()
            out["behind"], out["ahead"] = int(counts[0]), int(counts[1])
        except (SupervisorError, ValueError, IndexError):
            out["ahead"] = out["behind"] = None
        remote_branches = {l.strip().replace(f"{remote}/", "", 1) for l in git("branch", "-r").splitlines() if l.strip() and "->" not in l}
        local_branches = [l.strip("* ").strip() for l in git("branch").splitlines() if l.strip()]
        out["local_only_branches"] = [b for b in local_branches if b not in remote_branches]
    out["remote_committed_state_protected"] = bool(out["remote_checked"] and out["head"] == out["remote_head"])
    out["local_unprotected_work"] = bool(out["modified"] or out["untracked"] or out["ignored"] or (out["ahead"] or 0) > 0 or out["local_only_branches"])
    return out


def git_bundle(repo: Path, destination: Path) -> Path:
    """Committed refs only; never touches history or remotes."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(["git", "-C", str(repo), "bundle", "create", str(destination), "--all"], check=False, capture_output=True, text=True, timeout=600)
    if completed.returncode != 0:
        raise SupervisorError(f"git bundle failed: {(completed.stderr or '')[:120]}")
    return destination


class BackupRunner:
    def __init__(self, paths: Paths, config: dict[str, Any], transport: Transport | None = None, *, clock: Callable[[], float] = time.time, machine: str | None = None) -> None:
        self.paths = paths
        self.config = config
        self.clock = clock
        self.machine = machine or socket.gethostname()
        self.transport = transport

    def _state(self) -> dict[str, Any]:
        return load_backup_state(self.paths)

    def build_staging(self, staging: Path, backup_id: str) -> dict[str, Any]:
        """Copy configured sources with exclusions into `staging`, write MANIFEST.json + CHECKSUMS."""
        patterns = DEFAULT_EXCLUSIONS + list(self.config.get("exclusions") or [])
        files: list[dict[str, Any]] = []
        source_digests: list[tuple[Path, str]] = []
        excluded = 0
        for source in self.config.get("sources") or []:
            src = Path(source)
            if not src.exists():
                raise SupervisorError(f"configured backup source is missing: {src}")
            if src.is_symlink():
                raise SupervisorError(f"configured backup source may not be a symlink: {src}")
            base = staging / "sources" / src.name
            if src.is_file():
                rel = src.name
                if _excluded(rel, patterns) or _contains_secret(src):
                    excluded += 1
                    continue
                base.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, base)
                if _contains_secret(base):
                    base.unlink()
                    excluded += 1
                    continue
                digest = hashlib.sha256(base.read_bytes()).hexdigest()
                files.append({"path": f"sources/{src.name}", "bytes": base.stat().st_size, "sha256": digest})
                source_digests.append((src, digest))
                continue
            for root, dirs, names in os.walk(src):
                rel_root = os.path.relpath(root, src)
                dirs[:] = [d for d in dirs if not _excluded(os.path.normpath(os.path.join(rel_root, d)) + "/", patterns) and not _excluded(os.path.normpath(os.path.join(rel_root, d)), patterns)]
                for name in names:
                    rel = os.path.normpath(os.path.join(rel_root, name))
                    full = Path(root) / name
                    if full.is_symlink() or not full.is_file() or _excluded(rel, patterns) or _excluded(name, patterns) or _contains_secret(full):
                        excluded += 1
                        continue
                    target = base / rel
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(full, target)
                    if _contains_secret(target):
                        target.unlink()
                        excluded += 1
                        continue
                    digest = hashlib.sha256(target.read_bytes()).hexdigest()
                    files.append({"path": f"sources/{src.name}/{rel}", "bytes": target.stat().st_size, "sha256": digest})
                    source_digests.append((full, digest))
        # Atomic source files are copied independently. Re-read them before publication so a task changing
        # a file during the snapshot makes this attempt fail instead of publishing a mixed-time copy.
        for source_file, copied_digest in source_digests:
            try:
                current_digest = hashlib.sha256(source_file.read_bytes()).hexdigest()
            except OSError as error:
                raise SupervisorError(f"backup source changed during snapshot: {source_file}") from error
            if current_digest != copied_digest:
                raise SupervisorError(f"backup source changed during snapshot: {source_file}")
        git_meta = []
        for repo in self.config.get("git_repos") or []:
            path = Path(repo["path"])
            if not path.exists():
                continue
            assessment = git_assess(path, repo.get("remote", "origin"), repo.get("branch", "main"))
            if repo.get("bundle") and assessment.get("head"):
                bundle = git_bundle(path, staging / "bundles" / f"{path.name}.bundle")
                files.append({"path": f"bundles/{path.name}.bundle", "bytes": bundle.stat().st_size, "sha256": hashlib.sha256(bundle.read_bytes()).hexdigest()})
                assessment["bundle"] = f"bundles/{path.name}.bundle"
            git_meta.append(assessment)
        manifest = {
            "schema_version": 1, "backup_id": backup_id, "created_at": iso_utc(self.clock()), "machine": self.machine,
            "strategy": self.config["strategy"], "sources": list(self.config.get("sources") or []), "exclusions": patterns,
            "excluded_categories": EXCLUDED_CATEGORIES, "file_count": len(files), "total_bytes": sum(f["bytes"] for f in files),
            "excluded_files": excluded, "git": git_meta, "tool": {"name": "herdr-backup", "version": VERSION, "python": sys.version.split()[0]},
        }
        staging.mkdir(parents=True, exist_ok=True)
        (staging / "MANIFEST.json").write_text(json.dumps(manifest, indent=2, sort_keys=True))
        (staging / "CHECKSUMS").write_text("".join(f"{f['sha256']}  {f['path']}\n" for f in files))
        manifest_digest = hashlib.sha256((staging / "MANIFEST.json").read_bytes()).hexdigest()
        with (staging / "CHECKSUMS").open("a") as handle:
            handle.write(f"{manifest_digest}  MANIFEST.json\n")
        return {
            "manifest": manifest,
            "manifest_sha256": manifest_digest,
            "checksums_sha256": hashlib.sha256((staging / "CHECKSUMS").read_bytes()).hexdigest(),
        }

    def run(self, *, scheduled: bool = False) -> dict[str, Any]:
        """create temporary snapshot → transfer → verify → mark complete → update latest. A failure never
        touches the last verified backup and never blocks coding work."""
        state = self._state()
        now = self.clock()
        if not self.config.get("enabled"):
            return {"ok": False, "skipped": "disabled"}
        if scheduled and not due(self.config, state, now):
            return {"ok": True, "skipped": "not due"}
        backup_id = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now)) + "-" + uuid.uuid4().hex[:8]
        state["last_attempt"] = iso_utc(now)
        state["failure_reason"] = None
        save_backup_state(self.paths, state)
        if self.config["strategy"] == "git_recovery":
            assessments = [git_assess(Path(r["path"]), r.get("remote", "origin"), r.get("branch", "main")) for r in self.config.get("git_repos") or [] if Path(r["path"]).exists()]
            committed_protected = all(a.get("remote_committed_state_protected") for a in assessments) if assessments else False
            local_work_protected = all(not a.get("local_unprotected_work") for a in assessments) if assessments else False
            recoverable_complete = committed_protected and local_work_protected
            state["last_success"] = iso_utc(now)
            state["last_verified"] = iso_utc(now) if recoverable_complete else state.get("last_verified")
            state["last_backup_id"] = backup_id
            state["git_assessments"] = assessments
            if not recoverable_complete:
                state["failure_reason"] = "local work not protected by the configured Git remotes (uncommitted/untracked/local-only)"
            save_backup_state(self.paths, state)
            return {
                "ok": True,
                "backup_id": backup_id,
                "git": assessments,
                "remote_committed_state_protected": committed_protected,
                "local_work_protected": local_work_protected,
                "recoverable_complete": recoverable_complete,
            }
        if self.transport is None:
            raise SupervisorError("no transport configured")
        staging_root = backup_dir(self.paths) / "staging"
        staging_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        staging = staging_root / backup_id
        try:
            evidence = self.build_staging(staging, backup_id)
            self.transport.upload(staging, backup_id)
            if not self.transport.remote_manifest_check(backup_id, evidence):
                raise SupervisorError("remote verification failed; snapshot not marked complete")
            self.transport.complete(backup_id)
        except Exception as error:  # noqa: BLE001 - any failure is recorded, last-good preserved
            state["failure_reason"] = f"{type(error).__name__}: {str(error)[:200]}"
            state.setdefault("history", []).append({"backup_id": backup_id, "at": iso_utc(now), "ok": False, "reason": state["failure_reason"]})
            del state["history"][:-50]
            save_backup_state(self.paths, state)
            return {"ok": False, "backup_id": backup_id, "reason": state["failure_reason"]}
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        state["last_success"] = iso_utc(now)
        state["last_verified"] = iso_utc(now)  # remote checksum verification passed
        state["last_backup_id"] = backup_id
        state["last_verified_id"] = backup_id
        manifest = evidence["manifest"]
        state["last_manifest"] = {k: manifest[k] for k in ("file_count", "total_bytes", "excluded_files", "strategy")}
        state["last_manifest"].update(manifest_sha256=evidence["manifest_sha256"], checksums_sha256=evidence["checksums_sha256"])
        state.setdefault("history", []).append({"backup_id": backup_id, "at": iso_utc(now), "ok": True, "verified": True})
        del state["history"][:-50]
        save_backup_state(self.paths, state)
        try:
            self.apply_retention(state)
        except SupervisorError as error:
            state["retention_warning"] = str(error)[:200]
            save_backup_state(self.paths, state)
        return {"ok": True, "backup_id": backup_id, "manifest": manifest, "verification": {k: evidence[k] for k in ("manifest_sha256", "checksums_sha256")}}

    def apply_retention(self, state: dict[str, Any]) -> list[str]:
        """Conservative: keep the newest `daily` and one-per-week `weekly` snapshots, and always the last
        verified one. Only names matching the managed pattern under the managed root are deleted."""
        if self.transport is None:
            return []
        snapshots = self.transport.list_snapshots()
        keep_daily = int(self.config["retention"]["daily"])
        keep_weekly = int(self.config["retention"]["weekly"])
        keep = set(snapshots[-keep_daily:])
        weeks: dict[str, str] = {}
        for name in snapshots:
            week = time.strftime("%G-%V", time.strptime(name[:15], "%Y%m%dT%H%M%S"))
            weeks[week] = name  # newest in each week
        keep.update(list(weeks.values())[-keep_weekly:])
        if state.get("last_verified_id"):
            keep.add(state["last_verified_id"])
        deleted = []
        for name in snapshots:
            if name not in keep:
                self.transport.delete(name)
                deleted.append(name)
        return deleted

    def verify(self, backup_id: str | None = None) -> dict[str, Any]:
        """Non-destructive restore rehearsal into a temporary directory."""
        state = self._state()
        target_id = backup_id or state.get("last_backup_id")
        if not target_id or self.transport is None:
            raise SupervisorError("nothing to verify")
        with tempfile.TemporaryDirectory(prefix="herdr-backup-verify-") as tmp:
            destination = Path(tmp) / "restore"
            self.transport.download(target_id, destination)
            ok = verify_tree(destination)
            try:
                manifest = json.loads((destination / "MANIFEST.json").read_text()) if (destination / "MANIFEST.json").exists() else {}
            except (OSError, json.JSONDecodeError, UnicodeDecodeError):
                manifest = {}
        if ok:
            state["last_verified"] = iso_utc(self.clock())
            state["last_verified_id"] = target_id
        else:
            state["failure_reason"] = f"verification failed for {target_id}"
        save_backup_state(self.paths, state)
        return {"ok": ok, "backup_id": target_id, "file_count": manifest.get("file_count"), "excluded_categories": EXCLUDED_CATEGORIES}

    def restore_snapshot(self, backup_id: str, destination: Path) -> dict[str, Any]:
        """Human-invoked. Destination must not exist; integrity is validated before reporting success."""
        if self.transport is None:
            raise SupervisorError("no transport configured")
        if destination.exists():
            raise SupervisorError(f"refusing to restore over an existing path: {destination}")
        validate_backup_id(backup_id)
        destination.parent.mkdir(parents=True, exist_ok=True)
        staging = destination.parent / f".{destination.name}.restore-{uuid.uuid4().hex}"
        try:
            self.transport.download(backup_id, staging)
            if not verify_tree(staging):
                raise SupervisorError("restored snapshot failed integrity verification")
            try:
                manifest = json.loads((staging / "MANIFEST.json").read_text())
            except (OSError, json.JSONDecodeError, UnicodeDecodeError) as error:
                raise SupervisorError("restored manifest is unreadable") from error
            if manifest.get("backup_id") != backup_id:
                raise SupervisorError("restored manifest does not match requested backup id")
            if not verify_tree(staging):
                raise SupervisorError("restored snapshot changed before publication")
            os.rename(staging, destination)
        finally:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
        return {"ok": True, "backup_id": backup_id, "destination": str(destination), "file_count": manifest.get("file_count"), "reauthentication_required": EXCLUDED_CATEGORIES}


def restore_git(repo_config: dict[str, Any], destination: Path) -> dict[str, Any]:
    """Clone the configured remote into an absent destination and report the restored HEAD."""
    if destination.exists():
        raise SupervisorError(f"refusing to restore over an existing path: {destination}")
    source = Path(repo_config["path"])
    remote = repo_config.get("remote", "origin")
    branch = repo_config.get("branch", "main")
    url = repo_config.get("url")
    if not url:
        completed = subprocess.run(["git", "-C", str(source), "remote", "get-url", remote], check=False, capture_output=True, text=True, timeout=60)
        if completed.returncode != 0:
            raise SupervisorError("remote URL unavailable; pass url explicitly")
        url = completed.stdout.strip()
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = destination.parent / f".{destination.name}.restore-{uuid.uuid4().hex}"
    try:
        completed = subprocess.run(["git", "clone", "--branch", branch, "--", url, str(staging)], check=False, capture_output=True, text=True, timeout=1800)
        if completed.returncode != 0:
            raise SupervisorError(f"git clone failed: {(completed.stderr or '')[:160]}")
        os.rename(staging, destination)
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
    head = subprocess.run(["git", "-C", str(destination), "rev-parse", "HEAD"], check=False, capture_output=True, text=True, timeout=60).stdout.strip()
    return {"ok": True, "destination": str(destination), "head": head, "branch": branch, "note": "uncommitted/untracked work is not in a Git remote; credentials require re-authentication"}


# --------------------------------------------------------------------------- Telegram / request hooks


def emit_health_event(paths: Paths, config: dict[str, Any], now: float) -> str | None:
    """Informational BACKUP_HEALTH event into the supervisor outbox (rendered by the bridge)."""
    if not config.get("notify_telegram"):
        return None
    report = health(config, load_backup_state(paths), now)
    events_dir = paths.outbox_dir / "events"
    delivery_dir = paths.outbox_dir / "delivery"
    events_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    delivery_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    event_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"herdr-backup:{report.get('last_attempt')}:{report['status']}"))
    atomic_write_json(events_dir / f"{event_id}.json", {"schema_version": 1, "event_id": event_id, "type": "BACKUP_HEALTH", "run_id": "backup", "sequence": 0, "gate_id": None, "actionable": False, "at_unix": now, "at_utc": iso_utc(now), "expires_at_unix": now + 86400, "supervisor_state": None, "data": report})
    sidecar = delivery_dir / f"{event_id}.json"
    if not sidecar.exists():
        atomic_write_json(sidecar, {"event_id": event_id, "status": "pending", "attempts": 0, "updated_at": iso_utc(now)})
    return event_id


def request_file(paths: Paths) -> Path:
    return backup_dir(paths) / "run-requested"


def request_backup(paths: Paths) -> Path:
    backup_dir(paths).mkdir(parents=True, exist_ok=True, mode=0o700)
    marker = request_file(paths)
    atomic_write_json(marker, {"requested_at": iso_utc(time.time())})
    return marker


# --------------------------------------------------------------------------- CLI


def build_transport(config: dict[str, Any]) -> Transport | None:
    if config["strategy"] == "git_recovery":
        return None
    return SshRsyncTransport(parse_destination(config["destination"]), config.get("ssh_options") or [])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="herdr-backup", description="Optional deterministic backup/restore for herdr-supervisor (no LLM, no history rewriting).")
    sub = parser.add_subparsers(dest="command", required=True)
    setup = sub.add_parser("setup", help="write a validated backup configuration (dry-run connection check; nothing is enabled until you confirm)")
    setup.add_argument("--strategy", choices=STRATEGIES, required=True)
    setup.add_argument("--source", action="append", default=[], help="absolute path to protect (repeatable)")
    setup.add_argument("--destination", default=None, help="user@host:/absolute/path (ssh_snapshot/both)")
    setup.add_argument("--git-repo", action="append", default=[], help="absolute repo path (repeatable)")
    setup.add_argument("--schedule", choices=("daily", "weekly", "both"), default="daily")
    setup.add_argument("--interval-hours", type=int, default=None)
    setup.add_argument("--retention-daily", type=int, default=7)
    setup.add_argument("--retention-weekly", type=int, default=4)
    setup.add_argument("--max-age-hours", type=int, default=24)
    setup.add_argument("--notify-telegram", action="store_true")
    setup.add_argument("--confirm", action="store_true", help="enable after reviewing the dry run")
    sub.add_parser("status", help="backup health (also exposed by doctor and Telegram /backup)")
    run = sub.add_parser("run", help="create a snapshot now (or only when due with --scheduled)")
    run.add_argument("--scheduled", action="store_true")
    verify = sub.add_parser("verify", help="non-destructive restore rehearsal into a temporary directory")
    verify.add_argument("--backup-id", default=None)
    sub.add_parser("list", help="list recovery points")
    restore = sub.add_parser("restore", help="restore a verified snapshot into a NEW directory")
    restore.add_argument("--backup-id", required=True)
    restore.add_argument("--destination", required=True)
    gitres = sub.add_parser("restore-git", help="clone a configured repository's remote into a NEW directory")
    gitres.add_argument("--repo", required=True)
    gitres.add_argument("--destination", required=True)
    sub.add_parser("git-check", help="report what the configured Git remotes protect")
    args = parser.parse_args(argv)
    paths = Paths.from_environment()
    try:
        config_path = backup_config_file(paths)
        if args.command == "setup":
            config = json.loads(json.dumps(DEFAULT_BACKUP_CONFIG))
            config.update({"enabled": bool(args.confirm), "strategy": args.strategy, "sources": args.source, "destination": args.destination, "git_repos": [{"path": p, "remote": "origin", "branch": "main", "bundle": True} for p in args.git_repo], "schedule": {"daily": args.schedule in ("daily", "both"), "weekly": args.schedule in ("weekly", "both"), "interval_hours": args.interval_hours}, "retention": {"daily": args.retention_daily, "weekly": args.retention_weekly}, "max_age_hours": args.max_age_hours, "notify_telegram": args.notify_telegram})
            validate_backup_config({**config, "enabled": True} if args.strategy != "git_recovery" or args.confirm else config)
            transport = build_transport(config)
            if transport is not None and args.confirm:
                transport.check()  # dry-run connectivity/host-key check before enabling
            config_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            atomic_write_json(config_path, config)
            print(f"backup configuration written: {config_path} (enabled={config['enabled']}){'' if args.confirm else ' — rerun with --confirm to enable'}")
            return 0
        config = load_backup_config(config_path)
        runner = BackupRunner(paths, config, build_transport(config))
        if args.command == "status":
            print(json.dumps(health(config, load_backup_state(paths), time.time()), indent=2, sort_keys=True))
            return 0
        if args.command == "run":
            requested = request_file(paths).exists()
            if requested:
                request_file(paths).unlink()
            result = runner.run(scheduled=args.scheduled and not requested)
            emit_health_event(paths, config, time.time())
            print(json.dumps({k: v for k, v in result.items() if k != "manifest"}, indent=2, sort_keys=True, default=str))
            return 0 if result.get("ok") or result.get("skipped") else 1
        if args.command == "verify":
            print(json.dumps(runner.verify(args.backup_id), indent=2))
            return 0
        if args.command == "list":
            print("\n".join(runner.transport.list_snapshots()) if runner.transport else "git_recovery strategy has no snapshots")
            return 0
        if args.command == "restore":
            print(json.dumps(runner.restore_snapshot(args.backup_id, Path(args.destination)), indent=2))
            return 0
        if args.command == "restore-git":
            repo = next((r for r in config.get("git_repos") or [] if r["path"] == args.repo), None)
            if repo is None:
                raise SupervisorError("repository is not configured for backup")
            print(json.dumps(restore_git(repo, Path(args.destination)), indent=2))
            return 0
        if args.command == "git-check":
            print(json.dumps([git_assess(Path(r["path"]), r.get("remote", "origin"), r.get("branch", "main")) for r in config.get("git_repos") or []], indent=2))
            return 0
    except SupervisorError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
