"""Shared fixtures for the setup/update manager suites: a temporary HOME, a recording fake host (systemd, Herdr,
loginctl) that refuses every command it was not told about, candidate payload trees, and whole-tree filesystem
snapshots. No real Herdr, systemd, provider, Telegram or network is ever contacted."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import shutil
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "src"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

import herdr_installation as hi  # noqa: E402
import herdr_setup as hsetup  # noqa: E402

PAYLOAD_DIRS = ("src", "bin", "systemd", "docs", "config")
PAYLOAD_FILES = ("README.md", "LICENSE", "VERSION", "MANIFEST.txt")
UNITS = ("herdr-server.service", "herdr-supervisor-worker.path", "herdr-supervisor-worker.service", "herdr-query-worker.path", "herdr-query-worker.service",
         "herdr-telegram.service", "herdr-backup.service", "herdr-backup.timer", "herdr-codex-reset-worker.path", "herdr-codex-reset-worker.service")


def copy_payload(destination: Path, *, version: str | None = None, mutate: Any = None) -> Path:
    """A candidate tree: the repository's own payload, optionally with another version and edited files."""
    destination.mkdir(parents=True)
    for name in PAYLOAD_DIRS:
        shutil.copytree(ROOT / name, destination / name, ignore=shutil.ignore_patterns("__pycache__", "*.egg-info", "images"))
    for name in PAYLOAD_FILES:
        shutil.copy2(ROOT / name, destination / name)
    if version is not None:
        core = destination / "src/herdr_core.py"
        core.write_text(core.read_text().replace('SUPERVISOR_VERSION = "0.3.0-beta.1"', f'SUPERVISOR_VERSION = "{version}"'))
        (destination / "VERSION").write_text(version + "\n")
    if mutate is not None:
        mutate(destination)
    return destination


def snapshot(root: Path) -> dict[str, tuple[Any, ...]]:
    """Every path below root with its type, mode and content hash (symlinks by target): equal snapshots mean no write happened."""
    result: dict[str, tuple[Any, ...]] = {}
    for path in sorted(root.rglob("*")):
        rel = str(path.relative_to(root))
        info = os.lstat(path)
        if stat.S_ISLNK(info.st_mode):
            result[rel] = ("link", os.readlink(path))
        elif stat.S_ISDIR(info.st_mode):
            result[rel] = ("dir", stat.S_IMODE(info.st_mode))
        else:
            result[rel] = ("file", stat.S_IMODE(info.st_mode), hashlib.sha256(path.read_bytes()).hexdigest())
    return result


class FakeHost(hi.Host):
    """Scripted systemd/loginctl/Herdr. Launchers inside the temporary HOME run for real (they are the code under test);
    every other command must be expected, or the test fails loudly instead of touching the machine."""

    def __init__(self, layout: hi.Layout) -> None:
        self.layout = layout
        self.calls: list[list[str]] = []
        self.systemd_up = True
        self.active: dict[str, str] = {}
        self.enabled_units: dict[str, str] = {}
        self.agents: list[dict[str, Any]] | None = None  # None: no Herdr server answers
        self.herdr_version = "herdr 0.9.5"
        self.linger = "no"
        self.analyze_rc = 0
        self.analyze_output = ""
        self.fail: dict[tuple[str, ...], hi.CmdResult] = {}
        self.present = {"systemctl", "systemd-analyze", "loginctl", "codex", "claude"}
        self.plugin_log: list[list[str]] = []

    def which(self, name: str) -> str | None:
        return f"/fake/bin/{name}" if name in self.present else None

    def commands(self, program: str) -> list[list[str]]:
        return [call for call in self.calls if Path(call[0]).name == program]

    def service_calls(self) -> list[list[str]]:
        return [call for call in self.commands("systemctl") if call[2] in ("daemon-reload", "enable", "start", "stop", "disable")]

    def run(self, argv: list[str], *, timeout: float = 60, env: dict[str, str] | None = None, cwd: Path | None = None) -> hi.CmdResult:
        self.calls.append(list(argv))
        key = (argv[2], *argv[3:4]) if Path(argv[0]).name == "systemctl" and len(argv) > 2 else tuple(argv[:3])
        if key in self.fail:
            return self.fail[key]
        program = Path(argv[0]).name
        if program == "systemctl":
            return self._systemctl(argv)
        if program == "systemd-analyze":
            return hi.CmdResult(self.analyze_rc, "", self.analyze_output)
        if program == "loginctl":
            return hi.CmdResult(0, self.linger + "\n")
        if program == "herdr":
            return self._herdr(argv)
        if Path(argv[0]).parent == self.layout.bin:
            return super().run(argv, timeout=timeout, env=env, cwd=cwd)
        raise AssertionError(f"unexpected external command: {argv}")

    def _systemctl(self, argv: list[str]) -> hi.CmdResult:
        verb = argv[2]
        if not self.systemd_up:
            return hi.CmdResult(1, "", "Failed to connect to user scope bus")
        if verb == "show-environment":
            return hi.CmdResult(0, "HOME=/fake\n")
        if verb == "is-active":
            state = self.active.get(argv[3], "inactive")
            return hi.CmdResult(0 if state == "active" else 3, state + "\n")
        if verb == "is-enabled":
            state = self.enabled_units.get(argv[3], "disabled")
            return hi.CmdResult(0 if state.startswith("enabled") else 1, state + "\n")
        if verb == "daemon-reload":
            return hi.CmdResult(0)
        if verb == "stop":
            for unit in argv[3:]:
                self.active[unit] = "inactive"
            return hi.CmdResult(0)
        if verb == "start":
            for unit in argv[3:]:
                self.active[unit] = "active"
            return hi.CmdResult(0)
        if verb == "enable":
            for unit in argv[3:]:
                self.enabled_units[unit] = "enabled"
            return hi.CmdResult(0)
        raise AssertionError(f"unexpected systemctl verb {argv}")

    def _herdr(self, argv: list[str]) -> hi.CmdResult:
        if argv[1:2] == ["--version"]:
            return hi.CmdResult(0, self.herdr_version + "\n")
        if argv[1:3] == ["agent", "list"]:
            if self.agents is None:
                return hi.CmdResult(1, "", "no server")
            return hi.CmdResult(0, json.dumps({"result": {"agents": self.agents}}))
        if argv[1] == "plugin" and argv[2] in ("link", "enable"):
            self.plugin_log.append(argv[2:])
            return hi.CmdResult(0, "{}")
        raise AssertionError(f"unexpected herdr command {argv}")


class SetupCase(unittest.TestCase):
    """A temporary HOME with a supported Herdr executable, a FakeHost, and a pristine candidate payload."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="setup-test-"))
        self.addCleanup(self._cleanup)
        self.home = self.tmp / "home"
        (self.home / ".local/bin").mkdir(parents=True)
        herdr = self.home / ".local/bin/herdr"
        herdr.write_text("#!/bin/sh\nexit 0\n")
        herdr.chmod(0o755)
        clean = {key: value for key, value in os.environ.items() if not key.startswith(("HERDR_", "XDG_"))}
        clean.update(HOME=str(self.home), PATH=f"{Path(sys.executable).parent}:/usr/bin:/bin")  # launchers run under the interpreter running the tests
        patcher = mock.patch.dict(os.environ, clean, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.layout = hi.Layout.from_environment(self.home)
        self.host = FakeHost(self.layout)
        self.env = hi.Env(self.layout, host=self.host, lock_wait=1.0)
        self.payload = copy_payload(self.tmp / "payload-1")

    def _cleanup(self) -> None:
        for generations in self.tmp.rglob("generations"):
            for path in generations.rglob("*"):
                with contextlib.suppress(OSError):
                    os.chmod(path, 0o755)
            with contextlib.suppress(OSError):
                os.chmod(generations, 0o755)
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ----- running the tool
    def run_tool(self, *argv: str, adapters: hsetup.Adapters | None = None, ask: Any = None, env: hi.Env | None = None) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        kwargs: dict[str, Any] = {"env": env or self.env, "adapters": adapters}
        if ask is not None:
            kwargs["ask"] = ask
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = hsetup.main(list(argv), **kwargs)
        return code, out.getvalue(), err.getvalue()

    def plan_json(self, *argv: str, **kwargs: Any) -> dict[str, Any]:
        code, out, err = self.run_tool("--json", *argv, **kwargs)
        self.assertIn(code, (0, 4), err)
        return json.loads(out)  # type: ignore[no-any-return]

    def install(self, payload: Path | None = None, *extra: str) -> None:
        code, out, err = self.run_tool("setup", "--profile", "minimal", "--payload", str(payload or self.payload), "--apply", "--approve-current", *extra)
        self.assertEqual(code, 0, out + err)

    def update(self, payload: Path, *extra: str) -> tuple[int, str, str]:
        return self.run_tool("update", "--payload", str(payload), "--apply", "--approve-current", *extra)

    def candidate(self, name: str, version: str, mutate: Any = None) -> Path:
        return copy_payload(self.tmp / name, version=version, mutate=mutate)

    def receipt(self) -> dict[str, Any]:
        return json.loads(self.layout.receipt.read_text())  # type: ignore[no-any-return]

    def active_generation(self) -> str | None:
        return hi.read_active(self.layout)

    def reset_calls(self) -> None:
        self.host.calls.clear()

    def adapters(self, quota: Any = None, *, interactive: bool = False, secret: Any = None) -> hsetup.Adapters:
        options: dict[str, Any] = {"quota": {}}
        adapters = hsetup.Adapters.for_env(self.env, options, interactive=interactive)
        if quota is not None:
            adapters.quota = quota
        if secret is not None:
            adapters.secret = secret
        return adapters


def touch_task_state(layout: hi.Layout, supervisor_state: str = "DONE", **fields: Any) -> dict[str, Any]:
    """Write a task state the real reader accepts (built the way Supervisor.initialize builds one), with optional overrides."""
    import herdr_core as core
    import herdr_validation as validation

    run_id = "11111111-1111-4111-8111-111111111111"
    state: dict[str, Any] = {
        "schema_version": core.SCHEMA_VERSION, "run_id": run_id, "task_id": run_id, "task_text": "fixture task", "task_reference": None, "phase": "initial", "active_agent": "codex",
        "start_agent": "codex", "supervisor_state": supervisor_state, "worker_pid": None,
        "native_sessions": {"codex": "33333333-3333-4333-8333-333333333333", "claude": "44444444-4444-4444-8444-444444444444"}, "last_successful_handoff": None, "quota_wait": None,
        "delivery": None, "interrupted_delivery": None, "turns_completed": 0, "wait_user_reason": None, "last_error": None, "created_at": "2026-01-01T00:00:00Z",
        **validation.new_v2_fields("supervised_v2"),
    }
    state.update(fields)
    layout.state.mkdir(parents=True, exist_ok=True)
    validation.StateStore(layout.paths, lambda: 1_000_000.0).write_state(state)
    return state


class FakeQuota:
    """A scripted stand-in for QuotaAdapter that records every call. The real adapter is exercised separately against the
    quota-dependency fixtures (see test_setup_full.RealQuotaAdapterTests)."""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.state: dict[str, Any] = {"state": "absent", "detail": "plugin herdr-agent-quota is not installed", "enabled": None, "action": "bootstrap"}
        self.fresh = False
        self.toolchain = "PRESENT"
        self.dry_code = 0
        self.stage_code = 0
        self.fail_link = False
        self.fail_enable = False

    def check(self) -> tuple[int, dict[str, Any]]:
        self.calls.append(("check",))
        providers = {p: {"state": "FRESH" if self.fresh else "ABSENT"} for p in ("claude", "codex")}
        return 0, {"dependency": dict(self.state), "telemetry": {"providers": providers}, "toolchain": {"status": self.toolchain, "action": "install the pinned Rust toolchain"}}

    def bootstrap(self, stage_dir: str, *, apply: bool) -> tuple[int, dict[str, Any]]:
        self.calls.append(("bootstrap", stage_dir, apply))
        if not apply:
            return self.dry_code, {"toolchain": {"status": self.toolchain, "action": "install the pinned Rust toolchain"}, "result": "dry run", "action": "install the pinned Rust toolchain"}
        if self.stage_code:
            return self.stage_code, {"result": "stopped", "action": "retry"}
        staged = Path(stage_dir) / "herdr-agent-quota-809749e58548"
        staged.mkdir(parents=True)
        (Path(stage_dir) / "BOOTSTRAP_RECEIPT.json").write_text("{}")
        return 0, {"result": "staged and verified"}

    def link(self, directory: str) -> None:
        self.calls.append(("link", directory))
        if self.fail_link:
            raise hi.InstallError("`herdr plugin link` failed (exit 1)")
        self.state = {"state": "pinned", "detail": "installed", "enabled": False, "action": "none"}

    def enable(self) -> None:
        self.calls.append(("enable",))
        if self.fail_enable:
            raise hi.InstallError("`herdr plugin enable` failed (exit 1)")
        self.state = {**self.state, "enabled": True}

    def count(self, name: str) -> int:
        return len([call for call in self.calls if call[0] == name and (name != "bootstrap" or call[2])])
