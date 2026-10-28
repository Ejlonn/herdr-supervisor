"""Issue 5 helper boundary: validated helper-context descriptor, request binding, per-project unit rendering,
shared read-only readiness, and the real journal writer / worker subprocess / fake stdio provider path under
a non-default root with spaces. No model, no network, no systemd activation: where the user manager is not
queried the activation evidence is reported as untested, never counted as service proof."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

import herdr_codex_reset as hcr
import herdr_helper_context as hctx

SRC = Path(__file__).resolve().parents[1] / "src"
CWD = "/work/project one"
PAYLOAD = {"cwd": CWD, "model": None, "ephemeral": False}

FAKE_CODEX = r'''#!/usr/bin/env python3
import json, os, sys, time, uuid
log = os.path.join(os.path.dirname(os.path.abspath(__file__)), "calls.log")
for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    with open(log, "a") as handle:
        handle.write(method + "\n")
    if method == "initialize":
        print(json.dumps({"id": message["id"], "result": {}}), flush=True)
    elif method == "thread/start":
        time.sleep(float(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "delay")).read() or 0) if os.path.exists(os.path.join(os.path.dirname(os.path.abspath(__file__)), "delay")) else 0)
        cwd = message["params"]["cwd"]
        thread = {"id": str(uuid.uuid4()), "ephemeral": False, "cwd": cwd, "cliVersion": "0.0.0", "createdAt": 1}
        print(json.dumps({"id": message["id"], "result": {"thread": thread, "model": "fake-model", "modelProvider": "fake", "cwd": cwd}}), flush=True)
'''


def thread_calls(codex: Path) -> int:
    log = codex.parent / "calls.log"
    return log.read_text().splitlines().count("thread/start") if log.exists() else 0


class Project:
    """One isolated project: home, state root with a space, CODEX_HOME, a fake stdio Codex, and the installed
    worker launcher/library (real copies from the source tree). `global_mode` uses the default journal root."""

    def __init__(self, base: Path, name: str, *, global_mode: bool = False) -> None:
        self.home = base / f"home {name}"
        self.root = self.home / ".local/state/herdr-supervisor/codex-reset" if global_mode else self.home / "state dir" / "codex-reset"
        self.codex_home = self.home / ".codex"
        self.bin_dir = base / f"bin {name}"
        for directory in (self.home / ".local/bin", self.home / ".local/lib/herdr-supervisor", self.codex_home, self.bin_dir, *(self.root / part for part in ("pending", "processing", "results"))):
            directory.mkdir(parents=True, exist_ok=True)
        self.codex = self.bin_dir / "codex"
        self.codex.write_text(FAKE_CODEX)
        self.codex.chmod(0o755)
        self.launcher = self.home / ".local/bin/herdr-codex-reset-worker"
        shutil.copy(SRC.parent / "bin/herdr-codex-reset-worker", self.launcher)
        self.launcher.chmod(0o755)
        for module in ("herdr_codex_reset.py", "herdr_helper_context.py"):
            shutil.copy(SRC / module, self.home / ".local/lib/herdr-supervisor" / module)
        self.env = {"HOME": str(self.home), "PATH": f"{self.bin_dir}:/usr/bin:/bin", "HERDR_SUPERVISOR_CONFIG": str(self.home / "absent.json")}
        if not global_mode:
            self.env.update({"CODEX_HOME": str(self.codex_home), "HERDR_CODEX_BIN": str(self.codex)})

    def context(self, **override: object) -> dict:
        environ = {**self.env, **{k: str(v) for k, v in override.items()}}
        return hctx.build_context(self.root, CWD, environ=environ, home=self.home)

    def worker(self, extra_env: dict | None = None) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(SRC / "herdr_codex_reset.py"), "--once", "--state-dir", str(self.root)],
                              env={**self.env, **(extra_env or {})}, capture_output=True, text=True, timeout=60, check=False)


class DescriptorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = Project(Path(self.tmp.name), "a")

    def test_descriptor_is_canonical_stable_and_every_field_changes_the_fingerprint(self) -> None:
        context = self.project.context()
        self.assertEqual(hctx.validate_context(context), context)
        self.assertEqual(context["journal_root"], os.path.realpath(self.project.root))
        self.assertIn(" ", context["journal_root"])
        self.assertEqual(hctx.fingerprint(context), hctx.fingerprint(self.project.context()))
        other_codex = self.project.bin_dir / "codex2"
        shutil.copy(self.project.codex, other_codex)
        other_home = self.project.home / "other codex"
        other_home.mkdir()
        variants = {"codex bin": self.project.context(HERDR_CODEX_BIN=other_codex), "codex home": self.project.context(CODEX_HOME=other_home),
                    "cwd": {**context, "working_directory": "/work/other"},
                    "root": {**context, "journal_root": str(self.project.home / "elsewhere"), "instance": hctx.instance_id(str(self.project.home / "elsewhere"))}}
        seen = {hctx.fingerprint(context)}
        for label, variant in variants.items():
            with self.subTest(label):
                fp = hctx.fingerprint(variant)
                self.assertNotIn(fp, seen)
                seen.add(fp)

    def test_untrusted_or_malformed_values_are_refused(self) -> None:
        context = self.project.context()
        for label, bad in {"relative root": {**context, "journal_root": "relative"}, "newline": {**context, "codex_home": "/a\n/b"},
                           "extra field": {**context, "env": {}}, "bool version": {**context, "version": True},
                           "wrong instance": {**context, "instance": "0" * 12}}.items():
            with self.subTest(label), self.assertRaises(hctx.ContextError):
                hctx.validate_context(bad)
        with self.assertRaises(hctx.ContextError):
            hctx.build_context(self.project.root, "relative/cwd", environ=self.project.env, home=self.project.home)
        with self.assertRaises(hctx.ContextError):
            hctx.build_context(self.project.root, CWD, environ={**self.project.env, "HERDR_CODEX_BIN": "/nonexistent/codex", "PATH": "/nonexistent"}, home=self.project.home / "nohome")

    def test_request_binds_the_fingerprint_only_for_thread_start_and_legacy_requests_stay_valid(self) -> None:
        fp = hctx.fingerprint(self.project.context())
        bound = hcr.make_request("thread_start", PAYLOAD, "thread-bound-1", fp)
        legacy = hcr.make_request("thread_start", PAYLOAD, "thread-bound-1")
        self.assertNotEqual(bound["request_digest"], legacy["request_digest"])
        self.assertEqual(hcr.validate_request(bound, "thread-bound-1"), bound)
        self.assertEqual(hcr.validate_request(legacy, "thread-bound-1"), legacy)
        forged = {**bound, "context_fingerprint": "0" * 64}
        with self.assertRaises(hcr.ResetError):
            hcr.validate_request(forged, "thread-bound-1")
        with self.assertRaises(hcr.ResetError):
            hcr.make_request("inventory", {}, "inventory-1", fp)
        with self.assertRaises(hcr.ResetError):
            hcr.make_request("thread_start", PAYLOAD, "thread-bound-2", "not-a-digest")


class RenderingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = Project(Path(self.tmp.name), "a")
        self.context = self.project.context()
        self.units = Path(self.tmp.name) / "units"
        self.units.mkdir()

    def install(self, mutate=lambda name, text: text) -> None:
        for name, text in hctx.render_units(self.context, self.project.home).items():
            (self.units / name).write_text(mutate(name, text))

    def test_rendered_units_agree_with_the_descriptor_and_never_broaden_the_write_scope(self) -> None:
        self.install()
        self.assertEqual(hctx.inspect_units(self.context, self.units, self.project.home), [])
        path_unit, service_unit, is_global = hctx.unit_names(self.context, self.project.home)
        self.assertFalse(is_global)
        text = (self.units / service_unit).read_text()
        self.assertIn("--once --state-dir", text)
        self.assertIn(self.context["journal_root"], text)
        self.assertNotIn(f"ReadWritePaths={self.project.home}\n", text)
        self.assertEqual((self.units / path_unit).read_text().count("DirectoryNotEmpty="), 1)
        self.assertIn(f"{self.context['journal_root']}/pending", (self.units / path_unit).read_text())
        with self.assertRaises(hctx.ContextError):
            hctx.render_units(self.project.context() | {"journal_root": hctx.default_journal_root(self.project.home), "instance": hctx.instance_id(hctx.default_journal_root(self.project.home))}, self.project.home)

    def test_inspection_rejects_each_disagreement(self) -> None:
        home = self.project.home
        cases = {
            "watches another journal": lambda n, t: t.replace(self.context["journal_root"] + "/pending", str(home / "elsewhere/pending")) if n.endswith(".path") else t,
            "no explicit state dir": lambda n, t: t.replace(" --state-dir ", " --other ") if n.endswith(".service") else t,
            "whole home writable": lambda n, t: t.replace("ReadWritePaths=", f'ReadWritePaths="{home}" ') if n.endswith(".service") else t,
            "wrong codex home": lambda n, t: t.replace("CODEX_HOME=", "CODEX_HOME=/elsewhere/") if n.endswith(".service") else t,
            "wrong binary pin": lambda n, t: t.replace("HERDR_CODEX_BIN=", "HERDR_CODEX_BIN=/elsewhere/") if n.endswith(".service") else t,
            "unpinned binary": lambda n, t: "\n".join(l for l in t.splitlines() if "HERDR_CODEX_BIN" not in l) + "\n" if n.endswith(".service") else t,
            "journal not writable": lambda n, t: t.replace(f'"{self.context["journal_root"]}"', '"/nonexistent"') if n.endswith(".service") and "ReadWritePaths" in t else t,
        }
        for label, mutate in cases.items():
            with self.subTest(label):
                self.install(mutate)
                problems = hctx.inspect_units(self.context, self.units, home)
                self.assertTrue(problems, label)
        for child in self.units.iterdir():
            child.unlink()
        self.assertTrue(any("not installed" in p for p in hctx.inspect_units(self.context, self.units, home)))

    def test_percent_and_quote_in_paths_are_escaped(self) -> None:
        odd = self.project.home / 'we"ird %h dir' / "codex-reset"
        context = hctx.build_context(odd, CWD, environ=self.project.env, home=self.project.home)
        units = hctx.render_units(context, self.project.home)
        for name, text in units.items():
            (self.units / name).write_text(text)
        self.assertEqual(hctx.inspect_units(context, self.units, self.project.home), [])

    @unittest.skipUnless(shutil.which("systemd-analyze") and os.environ.get("XDG_RUNTIME_DIR"), "systemd-analyze --user unavailable: unit syntax verification untested")
    def test_rendered_units_pass_systemd_analyze_verify(self) -> None:
        worker = self.project.home / ".local/bin/herdr-codex-reset-worker"
        worker.write_text("#!/bin/sh\n")
        worker.chmod(0o755)
        self.install()
        path_unit, service_unit, _ = hctx.unit_names(self.context, self.project.home)
        done = subprocess.run(["systemd-analyze", "--user", "verify", str(self.units / path_unit), str(self.units / service_unit)],
                              capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual(done.returncode, 0, done.stderr)


class LoadedUnits:
    """Scripted `systemctl --user` answers modelled on the real property formats captured read-only from
    systemd on this host (see the pane/systemd captures in the review folder). `props` and `env` are mutable."""

    def __init__(self, context: dict, units: Path, home: Path, *, is_global: bool) -> None:
        path_unit, service_unit, _ = hctx.unit_names(context, home)
        root, worker = context["journal_root"], str(home / ".local/bin/herdr-codex-reset-worker")
        argv = f"{worker} --once" if is_global else f"{worker} --once --state-dir {root}"
        common = {"LoadState": "loaded", "DropInPaths": "", "NeedDaemonReload": "no"}
        self.props = {
            path_unit: {**common, "FragmentPath": str(units / path_unit), "ActiveState": "active", "SubState": "waiting", "UnitFileState": "enabled",
                        "Paths": f"{root}/pending (DirectoryNotEmpty)", "Unit": service_unit},
            service_unit: {**common, "FragmentPath": str(units / service_unit), "ActiveState": "inactive", "Type": "oneshot",
                           "ExecStart": f"{{ path={worker} ; argv[]={argv} ; ignore_errors=no ; start_time=[n/a] ; stop_time=[n/a] ; pid=0 ; code=(null) ; status=0/0 }}",
                           "Environment": "" if is_global else f"CODEX_HOME={context['codex_home']} HERDR_CODEX_BIN={context['codex_bin']}",
                           "ReadWritePaths": f"{context['codex_home']} {root}", "ProtectSystem": "strict", "ProtectHome": "read-only", "NoNewPrivileges": "yes"},
        }
        self.env: dict | None = {"HOME": str(home), "PATH": "/usr/bin:/bin"}
        self.service, self.path = service_unit, path_unit
        self.unavailable: set[str] = set()

    def __call__(self, args: list[str]) -> str | None:
        if args[0] == "show-environment":
            return None if self.env is None else "".join(f"{k}={v}\n" for k, v in self.env.items())
        unit = args[1]
        if unit in self.unavailable:
            return None
        wanted = [args[i + 1] for i, a in enumerate(args) if a == "-p"]
        return "".join(f"{prop}={self.props[unit].get(prop, '')}\n" for prop in wanted)


class ReadinessTests(unittest.TestCase):
    GLOBAL = False

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = Project(Path(self.tmp.name), "a", global_mode=self.GLOBAL)
        if self.GLOBAL:
            self.project.env["PATH"] = f"{self.project.bin_dir}:/usr/bin:/bin"
        self.config = {"project_root": CWD}
        self.units = Path(self.tmp.name) / "units"
        self.units.mkdir()
        self.context = self.project.context()
        if self.GLOBAL:
            for unit in ("herdr-codex-reset-worker.path", "herdr-codex-reset-worker.service"):
                shutil.copy(SRC.parent / "systemd" / unit, self.units / unit)
        else:
            for name, text in hctx.render_units(self.context, self.project.home).items():
                (self.units / name).write_text(text)
        self.loaded = LoadedUnits(self.context, self.units, self.project.home, is_global=self.GLOBAL)
        self.loaded.env["PATH"] = self.project.env["PATH"]
        self.launch: object = lambda: {"executable": str(self.project.codex), "codex_home": str(self.project.codex_home)}

    def report(self, **overrides) -> dict:
        options = dict(environ=self.project.env, home=self.project.home, unit_dir=self.units, systemctl=self.loaded, launch_context=self.launch)
        options.update(overrides)
        return hctx.helper_readiness(self.project.root, self.config, **options)

    @property
    def worker_path(self) -> str:
        return str(self.project.home / ".local/bin/herdr-codex-reset-worker")

    def reasons(self, **overrides) -> str:
        report = self.report(**overrides)
        self.assertFalse(report["ready"], report["reasons"])
        return " | ".join(report["reasons"])

    def test_ready_when_the_loaded_configuration_matches_and_an_inactive_oneshot_is_normal(self) -> None:
        report = self.report()
        self.assertTrue(report["ready"], report["reasons"])
        self.assertEqual(self.loaded.props[self.loaded.service]["ActiveState"], "inactive")
        self.assertEqual(report["evidence"], "structural-read-only")
        self.assertEqual(report["fingerprint"], hctx.fingerprint(self.context))
        self.assertTrue(all(report["checks"].values()), report["checks"])

    def test_each_missing_or_mismatched_watcher_evidence_is_an_actionable_unavailable_reason(self) -> None:
        for label, mutate, needle in (
            ("disabled watcher", lambda: self.loaded.props[self.loaded.path].update(UnitFileState="disabled", ActiveState="inactive"), "enable it explicitly"),
            ("watcher inactive", lambda: self.loaded.props[self.loaded.path].update(ActiveState="inactive"), "enable it explicitly"),
            ("failed service", lambda: self.loaded.props[self.loaded.service].update(ActiveState="failed"), "failed"),
            ("user manager unavailable", lambda: setattr(self.loaded, "env", None), "could not be verified"),
            ("service query unavailable", lambda: self.loaded.unavailable.add(self.loaded.service), "could not be verified"),
            ("path query unavailable", lambda: self.loaded.unavailable.add(self.loaded.path), "could not be verified"),
            ("watcher watches another root", lambda: self.loaded.props[self.loaded.path].update(Paths="/elsewhere/pending (DirectoryNotEmpty)"), "watches"),
            ("watcher starts another service", lambda: self.loaded.props[self.loaded.path].update(Unit="other.service"), "starts"),
            ("units not loaded", lambda: self.loaded.props[self.loaded.service].update(LoadState="not-found"), "not loaded"),
            ("loaded from elsewhere", lambda: self.loaded.props[self.loaded.service].update(FragmentPath="/etc/systemd/user/x.service"), "loaded from"),
        ):
            with self.subTest(label):
                self.setUp()
                mutate()
                self.assertIn(needle, self.reasons())

    def test_loaded_service_drift_is_unavailable(self) -> None:
        service = lambda **kw: self.loaded.props[self.loaded.service].update(**kw)  # noqa: E731
        for label, mutate, needle in (
            ("wrong worker executable", lambda: service(ExecStart="{ path=/bin/true ; argv[]=/bin/true --once ; ignore_errors=no }"), "instead of the supported worker"),
            ("worker with extra argument", lambda: service(ExecStart=f"{{ path={self.worker_path} ; argv[]={self.worker_path} --once --state-dir {self.context['journal_root']} --evil ; ignore_errors=no }}"), "instead of the supported worker"),
            ("worker for another root", lambda: service(ExecStart=f"{{ path={self.worker_path} ; argv[]={self.worker_path} --once --state-dir /elsewhere ; ignore_errors=no }}"), "instead of the supported worker"),
            ("no ExecStart", lambda: service(ExecStart=""), "instead of the supported worker"),
            ("drop-in override", lambda: service(DropInPaths=str(self.units / "x.service.d/override.conf")), "unit overrides"),
            ("pending daemon-reload (loaded vs disk)", lambda: service(NeedDaemonReload="yes"), "daemon-reload"),
            ("CODEX_HOME override", lambda: service(Environment=f"CODEX_HOME=/different-account HERDR_CODEX_BIN={self.context['codex_bin']}"), "environment"),
            ("extra environment", lambda: service(Environment=self.loaded.props[self.loaded.service]["Environment"] + " EXTRA=1"), "environment"),
            ("home writable", lambda: service(ReadWritePaths=f"{self.project.home} {self.context['journal_root']}"), "write scope"),
            ("write scope widened", lambda: service(ReadWritePaths=self.loaded.props[self.loaded.service]["ReadWritePaths"] + " /tmp"), "write scope"),
            ("write scope missing root", lambda: service(ReadWritePaths=self.context["codex_home"]), "write scope"),
            ("home unprotected", lambda: service(ProtectHome="no"), "ProtectHome"),
            ("system unprotected", lambda: service(ProtectSystem="no"), "ProtectSystem"),
            ("privileges", lambda: service(NoNewPrivileges="no"), "NoNewPrivileges"),
            ("not oneshot", lambda: service(Type="simple"), "Type"),
            ("manager HOME differs", lambda: self.loaded.env.update(HOME="/other/home"), "HOME"),
        ):
            with self.subTest(label):
                self.setUp()
                mutate()
                self.assertIn(needle, self.reasons())

    def test_worker_launcher_and_installed_library_must_be_the_supported_ones(self) -> None:
        lib = lambda: self.project.home / ".local/lib/herdr-supervisor"  # noqa: E731 - re-resolved after each setUp
        for label, mutate, needle in (
            ("launcher replaced", lambda: self.project.launcher.write_text("#!/bin/sh\nexit 0\n"), "supported worker launcher"),
            ("launcher missing", lambda: self.project.launcher.unlink(), "supported worker launcher"),
            ("launcher not executable", lambda: self.project.launcher.chmod(0o644), "supported worker launcher"),
            ("old worker library without the protocol", lambda: (lib() / "herdr_codex_reset.py").write_text("# old worker\n"), "does not support context-bound requests"),
            ("library lacks the context module", lambda: (lib() / "herdr_helper_context.py").unlink(), "does not support context-bound requests"),
        ):
            with self.subTest(label):
                self.setUp()
                mutate()
                self.assertIn(needle, self.reasons())

    def test_launcher_hash_and_protocol_marker_match_the_shipped_files(self) -> None:
        import hashlib
        shipped = SRC.parent / "bin/herdr-codex-reset-worker"
        self.assertEqual(hashlib.sha256(shipped.read_bytes()).hexdigest(), hctx.WORKER_LAUNCHER_SHA256, "update WORKER_LAUNCHER_SHA256 deliberately when the launcher changes")
        self.assertIn(hctx.WORKER_PROTOCOL_MARKER, (SRC / "herdr_codex_reset.py").read_text().splitlines())

    def test_producer_and_worker_must_resolve_the_same_context(self) -> None:
        other = self.project.bin_dir / "producer-codex"
        shutil.copy(self.project.codex, other)
        other.chmod(0o755)
        wrong_home = self.project.home / "other codex"
        wrong_home.mkdir()
        # The producer binds a different executable / CODEX_HOME than the loaded worker configuration yields.
        self.assertIn("would resolve helper context", self.reasons(environ={**self.project.env, "HERDR_CODEX_BIN": str(other), "PATH": self.project.env["PATH"]}))
        self.assertIn("would resolve helper context", self.reasons(environ={**self.project.env, "CODEX_HOME": str(wrong_home)}))

    def test_pane_launch_context_requires_a_supported_contract_and_never_uses_the_supervisor_environment(self) -> None:
        self.assertIn("offers no supported way", self.reasons(launch_context=None))
        self.assertIn("offers no supported way", self.reasons(launch_context=lambda: None))
        self.assertIn("unusable", self.reasons(launch_context=lambda: {"executable": "codex"}))
        self.assertIn("failed", self.reasons(launch_context=lambda: (_ for _ in ()).throw(RuntimeError("boom"))))
        other = self.project.bin_dir / "launched-codex"
        shutil.copy(self.project.codex, other)
        self.assertIn("another executable or account", self.reasons(launch_context=lambda: {"executable": str(other), "codex_home": str(self.project.codex_home)}))
        self.assertIn("another executable or account", self.reasons(launch_context=lambda: {"executable": str(self.project.codex), "codex_home": str(self.project.home / "elsewhere")}))
        report = self.report(launch_context=lambda: {"executable": str(self.project.codex), "codex_home": str(self.project.codex_home)})
        self.assertTrue(report["ready"], report["reasons"])

    def test_real_herdr_adapter_reports_the_missing_launch_contract(self) -> None:
        import herdr_cli
        self.assertIsNone(herdr_cli.HerdrCli("/fake/herdr").pane_launch_context())
        self.assertIn("offers no supported way", self.reasons(launch_context=herdr_cli.HerdrCli("/fake/herdr").pane_launch_context))

    def test_missing_journal_directories_and_denied_writes_are_unavailable(self) -> None:
        shutil.rmtree(self.project.root / "results")
        self.assertIn("lacks results", self.reasons())

    @unittest.skipIf(os.geteuid() == 0, "root bypasses file permissions")
    def test_denied_journal_write_is_unavailable(self) -> None:
        self.project.root.chmod(stat.S_IRUSR | stat.S_IXUSR)
        self.addCleanup(lambda: self.project.root.chmod(0o700))
        self.assertIn("not writable", self.reasons())

    def test_unit_fragments_on_disk_must_still_agree(self) -> None:
        for child in self.units.iterdir():
            child.unlink()
        self.assertIn("not installed", self.reasons())

    def test_cli_capability_alone_never_makes_fresh_codex_available(self) -> None:
        import herdr_sessions
        herdr_report = {"compatible": True, "required": {"agent start (--kind/--pane)": True, "agent get": True, "agent list": True},
                        "optional": {"task-start pane split (--direction/--ratio/--cwd/--no-focus)": True}}
        contract = {"codex_pane_local_server": True, "codex_approval_flag": True}
        config = {"session_start": {"enabled": True, "codex_launch_method": "explicit-pane-run"}}
        seam = dict(executable_check=lambda p: True, chdir_probe=lambda p: True)
        caps = {"pane run": True, "pane process-info (--pane)": True, "agent rename": True, "pane empty interactive input proof": True}

        def direct(**overrides):
            options = dict(environ=self.project.env, home=self.project.home, launch_capabilities=caps, **seam)
            options.update(overrides)
            return hctx.direct_launch_readiness(self.project.root, self.config, **options)

        ready = direct()
        self.assertTrue(ready["ready"], ready["reasons"])
        self.assertEqual((ready["launch_form"], ready["fingerprint"], ready["evidence"]), ("direct", hctx.fingerprint(self.context), "structural-read-only"))
        self.assertTrue(herdr_sessions.fresh_policy_supported(config, "fresh-codex", herdr_report, contract, ready))
        for selected in ("agent-start", None):  # DR2: a selected method that cannot start a NEW fresh Codex makes the shared predicate false even with a ready launch chain
            other = {"session_start": {"enabled": True, **({} if selected is None else {"codex_launch_method": selected})}}
            self.assertFalse(herdr_sessions.fresh_policy_supported(other, "fresh-codex", herdr_report, contract, ready), selected)
        # the quota-reset helper's units are NOT part of the direct launch: an inactive watcher changes nothing here (it still blocks the helper's own report)
        self.loaded.props[self.loaded.path].update(ActiveState="inactive")
        self.assertFalse(self.report()["ready"])
        self.assertTrue(herdr_sessions.fresh_policy_supported(config, "fresh-codex", herdr_report, contract, direct()))
        # what the direct launch does need: the launch recipe and the Herdr capabilities it uses
        for label, broken in (("pane run missing", dict(caps, **{"pane run": False})), ("agent rename missing", dict(caps, **{"agent rename": False})),
                              ("process info missing", dict(caps, **{"pane process-info (--pane)": False})), ("no capability report", None)):
            with self.subTest(label):
                report = direct(launch_capabilities=broken)
                self.assertFalse(report["ready"])
                self.assertFalse(herdr_sessions.fresh_policy_supported(config, "fresh-codex", herdr_report, contract, report))
        missing_binary = hctx.direct_launch_readiness(self.project.root, self.config, environ={k: v for k, v in self.project.env.items() if k != hctx.CODEX_BIN_ENV}, home=Path(self.tmp.name) / "empty-home",
                                                      which=lambda *a, **k: None, launch_capabilities=caps, **seam)
        self.assertFalse(missing_binary["ready"], "an unresolvable pinned Codex executable holds the launch")
        self.assertFalse(herdr_sessions.fresh_policy_supported(config, "fresh-codex", herdr_report, contract, ready and {**ready, "ready": "yes"}), "readiness must be exactly True")


class GlobalReadinessTests(ReadinessTests):
    """Positive and negative controls for the shipped global units serving the default journal."""
    GLOBAL = True

    def test_global_default_is_ready_with_the_shipped_units_and_no_pin(self) -> None:
        report = self.report()
        self.assertTrue(report["ready"], report["reasons"])
        self.assertEqual(hctx.unit_names(self.context, self.project.home)[2], True)

    def test_global_worker_resolving_another_codex_is_unavailable(self) -> None:
        other = self.project.bin_dir.parent / "manager-bin"
        other.mkdir()
        shutil.copy(self.project.codex, other / "codex")
        (other / "codex").chmod(0o755)
        self.loaded.env["PATH"] = str(other)
        self.assertIn("would resolve helper context", self.reasons())

    def test_global_unit_must_set_no_environment(self) -> None:
        self.loaded.props[self.loaded.service].update(Environment="CODEX_HOME=/different-account")
        self.assertIn("sets environment", self.reasons())

    def test_global_codex_home_override_in_the_manager_is_detected(self) -> None:
        self.loaded.env["CODEX_HOME"] = str(self.project.home / "another-account")
        self.assertIn("would resolve helper context", self.reasons())


class CrossBoundaryTests(unittest.TestCase):
    """Real journal writer, real worker subprocess, fake stdio provider; same root and a second project."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.a = Project(Path(self.tmp.name), "a")
        self.b = Project(Path(self.tmp.name), "b")

    def submit(self, project: Project, request_id: str, fingerprint: str | None, results: list, timeout: float = 30) -> threading.Thread:
        gateway = hcr.JournalGateway(project.root, timeout=timeout)

        def run() -> None:
            try:
                results.append(gateway.thread_start(PAYLOAD, request_id, fingerprint))
            except hcr.ResetError as error:
                results.append(error)

        thread = threading.Thread(target=run)
        thread.start()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not (project.root / "pending" / f"{request_id}.json").exists():
            time.sleep(0.02)
        return thread

    def test_pending_processing_result_under_the_same_root_with_no_cross_project_reads(self) -> None:
        fp_a, fp_b = hctx.fingerprint(self.a.context()), hctx.fingerprint(self.b.context())
        self.assertNotEqual(fp_a, fp_b)
        results_a: list = []
        results_b: list = []
        thread_a = self.submit(self.a, "thread-proj-a-codex", fp_a, results_a)
        thread_b = self.submit(self.b, "thread-proj-b-codex", fp_b, results_b)
        self.assertTrue((self.a.root / "pending" / "thread-proj-a-codex.json").exists())
        done = self.a.worker()
        self.assertEqual(done.returncode, 0, done.stderr)
        thread_a.join(30)
        self.assertEqual(len(results_a), 1)
        self.assertIsInstance(results_a[0], dict, results_a)
        self.assertEqual(thread_calls(self.a.codex), 1)
        self.assertEqual(thread_calls(self.b.codex), 0, "project b's provider must not be called by project a's worker")
        self.assertTrue((self.b.root / "pending" / "thread-proj-b-codex.json").exists())
        self.assertEqual(list((self.b.root / "results").iterdir()), [])
        self.assertEqual(sorted(p.name for p in (self.a.root / "results").iterdir()), ["thread-proj-a-codex.json"])
        self.assertEqual(list((self.a.root / "pending").iterdir()) + list((self.a.root / "processing").iterdir()), [])
        # a result for project a is not readable as project b's request
        shutil.copy(self.a.root / "results" / "thread-proj-a-codex.json", self.b.root / "results" / "thread-proj-b-codex.json")
        with self.assertRaises(hcr.ResetError):
            hcr.JournalGateway(self.b.root).result_if_present("thread_start", PAYLOAD, "thread-proj-b-codex", fp_b)
        self.b.worker()
        thread_b.join(5)

    def test_worker_refuses_a_request_bound_to_another_context_without_calling_the_provider(self) -> None:
        wrong_home = self.a.home / "foreign codex"
        wrong_home.mkdir()
        fp = hctx.fingerprint(self.a.context())
        results: list = []
        thread = self.submit(self.a, "thread-ctx-mismatch", fp, results)
        done = self.a.worker({"CODEX_HOME": str(wrong_home)})
        self.assertEqual(done.returncode, 0, done.stderr)
        thread.join(30)
        self.assertIsInstance(results[0], hcr.ResetError)
        self.assertIn("helper context mismatch", str(results[0]))
        self.assertEqual(thread_calls(self.a.codex), 0)
        other = self.a.bin_dir / "codex-other"
        shutil.copy(self.a.codex, other)
        results = []
        thread = self.submit(self.a, "thread-ctx-mismatch-bin", fp, results)
        self.a.worker({"HERDR_CODEX_BIN": str(other)})
        thread.join(30)
        self.assertIn("helper context mismatch", str(results[0]))
        self.assertEqual(thread_calls(self.a.codex) + thread_calls(other), 0)

    def test_legacy_unbound_thread_start_is_quarantined_unexecuted_and_not_retriggering(self) -> None:
        request = hcr.make_request("thread_start", PAYLOAD, "thread-legacy-407d513d")
        hcr.atomic_json(self.a.root / "pending" / "thread-legacy-407d513d.json", request, replace=False)
        before = (self.a.root / "pending" / "thread-legacy-407d513d.json").read_bytes()
        done = self.a.worker()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(thread_calls(self.a.codex), 0)
        self.assertEqual(list((self.a.root / "pending").iterdir()), [], "a held request must not keep the path watcher firing")
        self.assertEqual(list((self.a.root / "results").iterdir()), [], "no result may be fabricated for a legacy request")
        self.assertEqual((self.a.root / "held" / "thread-legacy-407d513d.json").read_bytes(), before)

    def test_timeout_then_late_exact_result_creates_exactly_one_thread_and_unknown_requests_are_never_retried(self) -> None:
        fp = hctx.fingerprint(self.a.context())
        gateway = hcr.JournalGateway(self.a.root, timeout=0.3)
        with self.assertRaisesRegex(hcr.ResetError, "timed out"):
            gateway.thread_start(PAYLOAD, "thread-late-result", fp)
        self.assertEqual(thread_calls(self.a.codex), 0)
        self.assertIsNone(gateway.result_if_present("thread_start", PAYLOAD, "thread-late-result", fp))
        self.assertEqual(self.a.worker().returncode, 0)
        late = gateway.result_if_present("thread_start", PAYLOAD, "thread-late-result", fp)
        self.assertTrue(late and late["ok"])
        for _ in range(2):
            self.assertEqual(self.a.worker().returncode, 0)
        self.assertEqual(thread_calls(self.a.codex), 1)
        with self.assertRaises(hcr.ResetError):
            gateway.result_if_present("thread_start", {**PAYLOAD, "cwd": "/work/changed"}, "thread-late-result", fp)
        with self.assertRaises(hcr.ResetError):
            gateway.result_if_present("thread_start", PAYLOAD, "thread-late-result", hctx.fingerprint(self.b.context()))
        # an in-flight request found after a restart is recorded as uncertain and never resent
        request = hcr.make_request("thread_start", PAYLOAD, "thread-inflight-1", fp)
        hcr.atomic_json(self.a.root / "processing" / "thread-inflight-1.json", request, replace=False)
        self.a.worker()
        value = json.loads((self.a.root / "results" / "thread-inflight-1.json").read_text())
        self.assertFalse(value["ok"])
        self.assertIn("outcome is unknown", value["error"])
        self.assertEqual(thread_calls(self.a.codex), 1)

    def test_two_simultaneous_workers_claim_the_request_once(self) -> None:
        (self.a.bin_dir / "delay").write_text("1.0")
        fp = hctx.fingerprint(self.a.context())
        results: list = []
        thread = self.submit(self.a, "thread-two-workers", fp, results)
        outputs: list = []
        workers = [threading.Thread(target=lambda: outputs.append(self.a.worker().returncode)) for _ in range(2)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(60)
        thread.join(30)
        self.assertEqual(sorted(outputs)[0], 0)
        self.assertTrue(all(code in (0, 3) for code in outputs), outputs)
        self.assertEqual(thread_calls(self.a.codex), 1)
        self.assertEqual(len(list((self.a.root / "results").iterdir())), 1)
        self.assertIsInstance(results[0], dict)


if __name__ == "__main__":
    unittest.main()
