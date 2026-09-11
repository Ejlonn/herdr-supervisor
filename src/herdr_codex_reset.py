#!/usr/bin/env python3
"""Supported Codex app-server banked-reset adapter and durable helper journal."""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import re
import selectors
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


class ResetError(RuntimeError):
    pass


SAFE_ID = re.compile(r"[A-Za-z0-9_-]{2,128}")
SAFE_ENV = ("HOME", "PATH", "CODEX_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "LANG", "LC_ALL", "TERM")


def resolve_codex_bin() -> str:
    configured = None
    path = Path(os.environ.get("HERDR_SUPERVISOR_CONFIG", Path.home() / ".config/herdr-supervisor/config.json"))
    with contextlib.suppress(OSError, json.JSONDecodeError, TypeError, AttributeError):
        value = json.loads(path.read_text())
        configured = (value.get("codex_reset") or {}).get("codex_bin")
    for candidate in (configured, shutil.which("codex"), str(Path.home() / ".local/bin/codex")):
        if isinstance(candidate, str) and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return candidate
    raise ResetError("Codex executable not found; configure codex_reset.codex_bin")


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_json(path: Path, value: dict[str, Any], *, replace: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, sort_keys=True, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        if replace:
            os.replace(tmp, path)
        else:
            os.link(tmp, path)
            tmp.unlink()
        _fsync_dir(path.parent)
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()


def account_fingerprint(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return hashlib.sha256(("herdr-codex-account-v1\0" + value).encode()).hexdigest()


@dataclass(frozen=True)
class ResetInventory:
    available_count: int
    ordinary_usage_allowed: bool | None
    account_fingerprint: str | None
    fetched_at_unix: float
    credits: tuple[dict[str, Any], ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {"availableCount": self.available_count, "ordinaryUsageAllowed": self.ordinary_usage_allowed,
                "accountFingerprint": self.account_fingerprint, "fetchedAtUnix": self.fetched_at_unix,
                "credits": list(self.credits)}


def parse_inventory(result: Any, *, now: float | None = None) -> ResetInventory:
    if not isinstance(result, dict):
        raise ResetError("rate-limit response is not an object")
    inventory = result.get("rateLimitResetCredits")
    if not isinstance(inventory, dict):
        raise ResetError("rate-limit response has no reset-credit inventory")
    count = inventory.get("availableCount")
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise ResetError("reset-credit availableCount is invalid")
    allowed = result.get("ordinaryUsageAllowed")
    if allowed is not None and not isinstance(allowed, bool):
        raise ResetError("ordinaryUsageAllowed is invalid")
    rows = inventory.get("credits", [])
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ResetError("reset-credit details are invalid")
    public = tuple({key: row[key] for key in ("expiresAt", "grantedAt", "resetType", "status") if key in row} for row in rows)
    fingerprint = result.get("accountFingerprint")
    if fingerprint is not None and (not isinstance(fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", fingerprint)):
        raise ResetError("account fingerprint is invalid")
    account = result.get("accountId")
    if account is None and isinstance(result.get("account"), dict):
        account = result["account"].get("id")
    fetched = result.get("fetchedAtUnix", now if now is not None else time.time())
    if isinstance(fetched, bool) or not isinstance(fetched, (int, float)):
        raise ResetError("reset inventory timestamp is invalid")
    return ResetInventory(count, allowed, fingerprint or account_fingerprint(account), float(fetched), public)


def ensure_inventory_fresh(inventory: ResetInventory, *, now: float, max_age: float) -> ResetInventory:
    if inventory.fetched_at_unix > now + 60 or now - inventory.fetched_at_unix > max_age:
        raise ResetError("reset inventory is stale or future-dated")
    return inventory


def parse_consume(result: Any) -> str:
    if not isinstance(result, dict):
        raise ResetError("consume response is not an object")
    aliases = {"reset": "reset", "nothingToReset": "nothing_to_reset", "noCredit": "no_credit", "alreadyRedeemed": "already_redeemed"}
    outcome = result.get("outcome") or result.get("status")
    if outcome not in aliases:
        raise ResetError("consume response has an unknown outcome")
    return aliases[outcome]


class AppServerClient:
    def __init__(self, command: list[str] | None = None, *, timeout: float = 15,
                 clock: Callable[[], float] = time.time, extra_env: dict[str, str] | None = None):
        self.command = command or [resolve_codex_bin(), "app-server", "--stdio"]
        self.timeout = timeout
        self.clock = clock
        self.extra_env = dict(extra_env or {})

    def _call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        environment = {key: os.environ[key] for key in SAFE_ENV if key in os.environ}
        environment.update(self.extra_env)
        process = subprocess.Popen(self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                   text=True, bufsize=1, env=environment)
        assert process.stdin and process.stdout
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)

        def send(value: dict[str, Any]) -> None:
            process.stdin.write(json.dumps(value, separators=(",", ":")) + "\n")
            process.stdin.flush()

        def receive(wanted: int) -> Any:
            deadline = time.monotonic() + self.timeout
            while time.monotonic() < deadline:
                if not selector.select(max(0, deadline - time.monotonic())):
                    break
                line = process.stdout.readline()
                if not line:
                    break
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if message.get("id") == wanted:
                    if "error" in message:
                        raise ResetError("Codex app-server request failed")
                    return message.get("result")
            raise ResetError("Codex app-server request timed out")

        try:
            send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"clientInfo": {"name": "herdr-supervisor", "version": "0.3"}, "capabilities": {}}})
            receive(1)
            send({"jsonrpc": "2.0", "method": "initialized", "params": {}})
            send({"jsonrpc": "2.0", "id": 2, "method": method, "params": params or {}})
            return receive(2)
        finally:
            selector.close()
            with contextlib.suppress(OSError):
                process.stdin.close()
            with contextlib.suppress(ProcessLookupError):
                process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
            with contextlib.suppress(OSError):
                process.stdout.close()

    def inventory(self) -> ResetInventory:
        return parse_inventory(self._call("account/rateLimits/read"), now=self.clock())

    def consume(self, key: str) -> str:
        if not isinstance(key, str) or not 16 <= len(key) <= 128:
            raise ResetError("invalid idempotency key")
        return parse_consume(self._call("account/rateLimitResetCredit/consume", {"idempotencyKey": key}))


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def make_request(operation: str, payload: dict[str, Any], request_id: str) -> dict[str, Any]:
    if operation not in ("inventory", "consume") or not isinstance(request_id, str) or not SAFE_ID.fullmatch(request_id):
        raise ResetError("invalid reset-helper request")
    if not isinstance(payload, dict) or (operation == "inventory" and payload):
        raise ResetError("invalid reset-helper payload")
    if operation == "consume" and (set(payload) != {"idempotency_key"} or not isinstance(payload["idempotency_key"], str) or not 16 <= len(payload["idempotency_key"]) <= 128):
        raise ResetError("invalid reset-helper consume payload")
    core = {"schema_version": 2, "request_id": request_id, "operation": operation, "payload": payload}
    return {**core, "request_digest": hashlib.sha256(_canonical(core)).hexdigest()}


def validate_request(value: Any, filename_id: str) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("request_id") != filename_id:
        raise ResetError("reset-helper request identity mismatch")
    expected = make_request(value.get("operation"), value.get("payload"), filename_id)
    if value != expected:
        raise ResetError("reset-helper request digest or schema mismatch")
    return expected


class JournalGateway:
    def __init__(self, root: Path, *, timeout: float = 30, sleeper: Callable[[float], None] = time.sleep):
        self.root = root
        self.timeout = timeout
        self.sleeper = sleeper

    def request(self, operation: str, payload: dict[str, Any], request_id: str) -> dict[str, Any]:
        request = make_request(operation, payload, request_id)
        pending = self.root / "pending" / f"{request_id}.json"
        result = self.root / "results" / f"{request_id}.json"
        if pending.exists():
            try:
                existing = json.loads(pending.read_text())
            except (OSError, json.JSONDecodeError) as error:
                raise ResetError("reset-helper pending request is corrupt") from error
            if existing != request:
                raise ResetError("reset-helper request identity collision")
        elif not result.exists():
            try:
                atomic_json(pending, request, replace=False)
            except FileExistsError:
                existing = json.loads(pending.read_text())
                if existing != request:
                    raise ResetError("reset-helper request identity collision")
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            if result.exists():
                try:
                    value = json.loads(result.read_text())
                except (OSError, json.JSONDecodeError) as error:
                    raise ResetError("reset-helper result is corrupt") from error
                if not isinstance(value, dict) or value.get("request_id") != request_id or value.get("request_digest") != request["request_digest"]:
                    raise ResetError("reset-helper result identity mismatch")
                if not value.get("ok"):
                    raise ResetError(str(value.get("error") or "reset helper failed"))
                return value
            self.sleeper(0.1)
        raise ResetError("reset-helper result timed out; operation is uncertain")

    def inventory(self, request_id: str) -> ResetInventory:
        return parse_inventory(self.request("inventory", {}, request_id)["inventory"])

    def consume(self, key: str, request_id: str) -> str:
        return str(self.request("consume", {"idempotency_key": key}, request_id)["outcome"])


def process_once(root: Path, client: AppServerClient | None = None) -> int:
    client = client or AppServerClient()
    pending, processing, results = (root / name for name in ("pending", "processing", "results"))
    for directory in (pending, processing, results):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    while True:
        work = sorted(processing.glob("*.json")) + sorted(pending.glob("*.json"))
        if not work:
            return 0
        source = work[0]
        filename_id = source.stem
        target = processing / source.name
        if source.parent == pending:
            os.replace(source, target)
            _fsync_dir(pending)
            _fsync_dir(processing)
        request: dict[str, Any] | None = None
        try:
            if not SAFE_ID.fullmatch(filename_id):
                raise ResetError("unsafe reset-helper request filename")
            request = validate_request(json.loads(target.read_text()), filename_id)
            result_path = results / f"{filename_id}.json"
            if result_path.exists():
                existing = json.loads(result_path.read_text())
                if existing.get("request_digest") != request["request_digest"]:
                    raise ResetError("existing reset-helper result identity mismatch")
                target.unlink(missing_ok=True)
                continue
            if request["operation"] == "inventory":
                inventory = client.inventory()
                output = {"request_id": filename_id, "request_digest": request["request_digest"], "ok": True, "inventory": inventory.as_dict()}
            else:
                output = {"request_id": filename_id, "request_digest": request["request_digest"], "ok": True,
                          "outcome": client.consume(request["payload"]["idempotency_key"])}
        except Exception as error:
            existing_path = results / f"{filename_id}.json"
            if existing_path.exists():
                target.unlink(missing_ok=True)
                _fsync_dir(processing)
                continue
            digest = request.get("request_digest") if isinstance(request, dict) else None
            output = {"request_id": filename_id, "request_digest": digest, "ok": False, "error": str(error)[:200]}
        atomic_json(results / f"{filename_id}.json", output)
        target.unlink(missing_ok=True)
        _fsync_dir(processing)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", required=True)
    parser.add_argument("--state-dir", type=Path, default=Path.home() / ".local/state/herdr-supervisor/codex-reset")
    root = parser.parse_args(argv).state_dir
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (root / "worker.lock").open("a+") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 3
        return process_once(root)


if __name__ == "__main__":
    raise SystemExit(main())
