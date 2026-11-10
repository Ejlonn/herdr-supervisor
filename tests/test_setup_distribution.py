"""Packaging, boundaries and external contracts of the installation manager: the new modules are companions the
controller never imports, the console entry point and launcher ship, the marketplace actions stay read-only previews,
and the plugin commands the manager issues are exactly the ones whose help text is recorded as a fixture."""

from __future__ import annotations

import ast
import json
import re
import shutil
import subprocess
import tomllib
import unittest
from pathlib import Path

import herdr_installation as hi
from setup_fixtures import SetupCase

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures/quota-dependency"
MANAGER = ("herdr_installation", "herdr_update", "herdr_setup")


def imports_of(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


class BoundaryTests(unittest.TestCase):
    def test_the_controller_never_imports_the_manager(self) -> None:
        for path in sorted((ROOT / "src").glob("*.py")):
            if path.stem in MANAGER:
                continue
            self.assertFalse(imports_of(path) & set(MANAGER), f"{path.name} must not import the installation manager: the controller state machine is unchanged")

    def test_the_manager_layers_are_one_way(self) -> None:
        self.assertFalse(imports_of(ROOT / "src/herdr_installation.py") & {"herdr_update", "herdr_setup", "herdr_telegram", "herdr_quota_dependency"})
        self.assertFalse(imports_of(ROOT / "src/herdr_update.py") & {"herdr_setup", "herdr_telegram", "herdr_quota_dependency"})
        self.assertTrue({"herdr_installation", "herdr_update", "herdr_quota_dependency", "herdr_telegram"} <= imports_of(ROOT / "src/herdr_setup.py"))

    def test_the_manager_runs_no_process_except_through_the_host_adapter(self) -> None:
        for name in ("herdr_update", "herdr_setup"):
            self.assertNotIn("subprocess", imports_of(ROOT / "src" / f"{name}.py"), f"{name} must use Host, the one counted door to external commands")
        text = (ROOT / "src/herdr_installation.py").read_text()
        self.assertEqual(len(re.findall(r"subprocess\.run\(", text)), 1, "exactly one place spawns a process")
        for module in MANAGER:
            source = (ROOT / "src" / f"{module}.py").read_text()
            for forbidden in ("os.system(", "shell=True", "os.setuid", "os.execv"):
                self.assertNotIn(forbidden, source, f"{module}: {forbidden}")
            self.assertFalse({name.split(".")[0] for name in imports_of(ROOT / "src" / f"{module}.py")} & {"urllib", "http", "socket", "ssl", "requests", "ctypes", "smtplib"}, f"{module} imports no network or foreign-function module")

    def test_only_the_documented_service_verbs_exist_in_the_manager(self) -> None:
        text = "".join((ROOT / "src" / f"{module}.py").read_text() for module in MANAGER)
        self.assertEqual(sorted(set(re.findall(r'act\("([a-z-]+)"', text))), ["daemon-reload", "enable", "start", "stop"])
        self.assertNotIn('"restart"', text)
        self.assertNotIn('"disable"', text.replace('"daemon-reload", "stop", "start", "enable", "disable"', ""))


class PackagingTests(unittest.TestCase):
    def test_entry_point_launcher_and_modules_are_registered(self) -> None:
        project = tomllib.loads((ROOT / "pyproject.toml").read_text())
        self.assertEqual(project["project"]["scripts"]["herdr-setup"], "herdr_setup:main")
        for module in MANAGER:
            self.assertIn(module, project["tool"]["setuptools"]["py-modules"])
            self.assertIn(module, project["tool"]["ruff"]["lint"]["isort"]["known-first-party"])
        self.assertTrue((ROOT / "bin/herdr-setup").is_file())
        self.assertIn("herdr-setup", (ROOT / ".github/workflows/ci.yml").read_text())
        self.assertIn("systemd-analyze --user verify systemd/*", (ROOT / ".github/workflows/ci.yml").read_text())

    def test_marketplace_actions_stay_safe_and_only_preview(self) -> None:
        manifest = tomllib.loads((ROOT / "herdr-plugin.toml").read_text())
        actions = {action["id"]: action["command"] for action in manifest["actions"]}
        self.assertEqual(actions["setup"], ["sh", "install.sh"], "the existing setup entry keeps its minimal, inactive semantics")
        self.assertEqual(actions["setup-preview"], ["python3", "src/herdr_setup.py", "setup", "--profile", "full"])
        self.assertEqual(actions["update-preview"], ["python3", "src/herdr_setup.py", "update"])
        for name in ("setup-preview", "update-preview"):
            for flag in ("--apply", "--approve", "--approve-current", "--interactive", "--plan-out", "--services", "--telegram-token-file"):
                self.assertNotIn(flag, actions[name], f"{name} is read-only and cannot collect or spend anything")
        self.assertNotIn("build", manifest)
        self.assertNotIn("startup", manifest)

    def test_the_installed_launcher_answers_help_from_a_source_tree(self) -> None:
        done = subprocess.run([str(ROOT / "bin/herdr-setup"), "--help"], env={"PATH": "/usr/bin:/bin", "HOME": "/nonexistent", "HERDR_SUPERVISOR_LIB": str(ROOT / "src")}, capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("rollback", done.stdout)


class ExternalContractTests(SetupCase):
    """The commands the manager runs against Herdr must match recorded real help text; nothing is guessed."""

    def test_link_and_enable_arguments_match_the_recorded_help(self) -> None:
        link_help = (FIXTURES / "plugin-link-help-0.9.5.txt").read_text()
        enable_help = (FIXTURES / "plugin-enable-help-0.9.5.txt").read_text()
        self.assertIn("herdr plugin link [OPTIONS] <PATH>", link_help)
        self.assertIn("--disabled", link_help)
        self.assertIn("herdr plugin enable <PLUGIN_ID>", enable_help)
        import herdr_setup as hsetup

        probe = hsetup.HerdrProbe(self.env, binary=str(self.layout.bin / "herdr"))
        adapter = hsetup.QuotaAdapter(self.env, probe, {})
        adapter.link("/stage/dir")
        adapter.enable()
        self.assertEqual(self.host.plugin_log, [["link", "--disabled", "/stage/dir"], ["enable", "herdr-agent-usage"]])
        self.assertEqual(json.loads((ROOT / "config/quota-dependency.json").read_text())["plugin_id"], "herdr-agent-usage")

    def test_systemd_and_herdr_commands_issued_by_planning_are_read_only(self) -> None:
        self.install()
        self.reset_calls()
        self.run_tool("update", "--payload", str(self.payload))
        self.run_tool("rollback")
        self.run_tool("--json", "status")
        for call in self.host.calls:
            program = Path(call[0]).name
            if program == "systemctl":
                self.assertIn(call[2], ("show-environment", "is-active", "is-enabled"), call)
            elif program == "herdr":
                self.assertIn(call[1:3], (["--version"], ["agent", "list"]), call)
        self.assertEqual(self.host.service_calls(), [])

    def test_real_systemd_analyze_accepts_the_shipped_units_when_available(self) -> None:
        if shutil.which("systemd-analyze") is None:
            self.skipTest("systemd-analyze is not installed")
        import os
        import tempfile

        runtime = Path(tempfile.mkdtemp(prefix="rt-"))
        self.addCleanup(shutil.rmtree, runtime, True)
        runtime.chmod(0o700)
        payload = hi.load_payload(self.payload)
        env = hi.Env(self.layout, host=hi.Host())
        os.environ["XDG_RUNTIME_DIR"] = str(runtime)
        self.addCleanup(os.environ.pop, "XDG_RUNTIME_DIR", None)
        verdict = hi.verify_units(env, payload)
        if "SO_PASSCRED" in verdict["detail"]:
            self.skipTest("sandbox does not permit systemd user-manager credential sockets")
        self.assertEqual(verdict["status"], "ok", verdict["detail"])
        # and a genuinely defective unit is caught by the same real tool
        bad = Path(self.tmp / "bad-units")
        shutil.copytree(self.payload, bad)
        (bad / "systemd/herdr-telegram.service").write_text("[Unit]\nDescription=x\n[Service]\nExecStart=/nonexistent/definitely-not-here --flag\nBogusSetting=1\n")
        broken = hi.verify_units(env, hi.load_payload(bad))
        self.assertEqual(broken["status"], "failed", broken["detail"])


if __name__ == "__main__":
    unittest.main()
