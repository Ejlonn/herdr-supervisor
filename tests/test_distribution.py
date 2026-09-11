"""Portable source packaging, isolated installation, rollback, and private-data scrub."""

from __future__ import annotations

import getpass
import hashlib
import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class DistributionTests(unittest.TestCase):
    def run_script(self, script: str, *args: str, home: Path, fake_bin: Path) -> subprocess.CompletedProcess[str]:
        env = {**os.environ, "HOME": str(home), "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}"}
        return subprocess.run([str(ROOT / script), *args], cwd=ROOT, env=env, capture_output=True, text=True, timeout=60, check=False)

    def fake_commands(self, root: Path) -> Path:
        fake_bin = root / "fake-bin"
        fake_bin.mkdir()
        # Unit validation is exercised without talking to the live user manager. Uninstall's read-only
        # state query reports an established "inactive" so the isolated prefix can be removed.
        for name, body in {
            "systemd-analyze": "#!/bin/sh\n[ \"$1\" = --user ] && [ \"$2\" = verify ]\n",
            "systemctl": "#!/bin/sh\necho inactive\nexit 3\n",
        }.items():
            path = fake_bin / name
            path.write_text(body)
            path.chmod(0o755)
        return fake_bin

    def fake_herdr(self, home: Path) -> Path:
        # The supported layout: Herdr's user-scoped executable at ~/.local/bin/herdr.
        path = home / ".local/bin/herdr"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\nexit 0\n")
        path.chmod(0o755)
        return path

    def fake_systemctl(self, fake_bin: Path, body: str) -> None:
        path = fake_bin / "systemctl"
        path.write_text("#!/bin/sh\n" + body)
        path.chmod(0o755)

    def test_source_layout_and_release_metadata(self) -> None:
        for path in (
            "README.md", "LICENSE", "VERSION", "MANIFEST.txt", "pyproject.toml", "install.sh", "uninstall.sh",
            "config/supervisor.example.json", "config/telegram.example.json", "config/backup.example.json",
            "docs/ARCHITECTURE.md", "docs/INSTALL.md", "docs/TELEGRAM.md", "docs/BACKUP.md",
            "docs/SECURITY.md", "docs/RECOVERY.md", "docs/RELEASE.md",
            "systemd/herdr-backup.service", "systemd/herdr-backup.timer", "bin/herdr-backup",
        ):
            self.assertTrue((ROOT / path).is_file(), path)
        self.assertEqual((ROOT / "VERSION").read_text().strip(), "0.3.0-beta.1")
        self.assertIn("Apache License", (ROOT / "LICENSE").read_text())
        readme = (ROOT / "README.md").read_text()
        metadata = (ROOT / "pyproject.toml").read_text()
        self.assertIn('license = "Apache-2.0"', metadata)
        self.assertIn("SPDX-License-Identifier: Apache-2.0", readme)
        self.assertNotIn("commercial license", metadata.lower())
        for phrase in ("human-in-the-loop", "not an official Herdr", "native session", "Telegram", "Backups", "Uninstall"):
            self.assertIn(phrase, readme)

    def test_examples_are_unconfigured_and_contain_no_credentials(self) -> None:
        supervisor = json.loads((ROOT / "config/supervisor.example.json").read_text())
        telegram = json.loads((ROOT / "config/telegram.example.json").read_text())
        backup = json.loads((ROOT / "config/backup.example.json").read_text())
        self.assertIsNone(supervisor["project_root"])
        self.assertEqual(telegram["mode"], "unconfigured")
        self.assertIsNone(telegram["owner_user_id"])
        self.assertIsNone(telegram["chat_id"])
        self.assertIsNone(telegram["token_file"])
        self.assertFalse(backup["enabled"])
        self.assertIsNone(backup["destination"])
        self.assertEqual(backup["sources"], [])

    def test_isolated_fresh_install_permissions_and_exact_sources(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            home.mkdir()
            self.fake_herdr(home)
            fake_bin = self.fake_commands(root)
            result = self.run_script("install.sh", home=home, fake_bin=fake_bin)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("No service was loaded", result.stdout)

            lib = home / ".local/lib/herdr-supervisor"
            for source in (ROOT / "src").glob("*.py"):
                installed = lib / source.name
                self.assertTrue(installed.is_file())
                self.assertEqual(hashlib.sha256(source.read_bytes()).digest(), hashlib.sha256(installed.read_bytes()).digest())
            for launcher in (ROOT / "bin").iterdir():
                self.assertEqual(stat.S_IMODE((home / ".local/bin" / launcher.name).stat().st_mode), 0o755)
            for directory in (
                home / ".config/herdr-supervisor", home / ".config/herdr-telegram",
                home / ".local/state/herdr-supervisor", home / ".local/state/herdr-telegram",
                home / ".local/state/herdr-telegram-locks",
                home / ".local/state/herdr-supervisor/query",
                home / ".local/state/herdr-supervisor/outbox",
            ):
                self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
            for config in (home / ".config/herdr-supervisor/config.json", home / ".config/herdr-supervisor/backup.json", home / ".config/herdr-telegram/config.json"):
                self.assertEqual(stat.S_IMODE(config.stat().st_mode), 0o600)

            doctor = subprocess.run(
                [str(home / ".local/bin/herdr-telegram"), "doctor", "--json"],
                env={**os.environ, "HOME": str(home)}, capture_output=True, text=True, timeout=30, check=False,
            )
            self.assertEqual(doctor.returncode, 0, doctor.stderr)
            self.assertEqual(json.loads(doctor.stdout)["status"], "UNCONFIGURED")

    def test_install_skips_unit_validation_without_user_runtime_dir(self) -> None:
        # Without XDG_RUNTIME_DIR, systemd-analyze --user aborts before reading any unit. The installer
        # must report that as a skipped check, not as a failed install, after all files are staged.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            home.mkdir()
            self.fake_herdr(home)
            fake_bin = self.fake_commands(root)
            analyze = fake_bin / "systemd-analyze"
            analyze.write_text("#!/bin/sh\n[ -n \"${XDG_RUNTIME_DIR:-}\" ] || { echo 'Failed to initialize manager' >&2; exit 1; }\n")
            env = {k: v for k, v in os.environ.items() if k != "XDG_RUNTIME_DIR"}
            env.update({"HOME": str(home), "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}"})
            result = subprocess.run([str(ROOT / "install.sh")], cwd=ROOT, env=env, capture_output=True, text=True, timeout=60, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Skipped unit validation", result.stderr)
            self.assertIn("No service was loaded", result.stdout)
            self.assertTrue((home / ".local/lib/herdr-supervisor/herdr_supervisor.py").is_file())

            # With a runtime directory the same fake runs, and a failing verification still fails the install.
            analyze.write_text("#!/bin/sh\necho 'unit has a bad unit file setting' >&2; exit 1\n")
            env["XDG_RUNTIME_DIR"] = str(root / "runtime")
            result = subprocess.run([str(ROOT / "install.sh")], cwd=ROOT, env=env, capture_output=True, text=True, timeout=60, check=False)
            self.assertEqual(result.returncode, 1, result.stdout)
            self.assertNotIn("No service was loaded", result.stdout)

    def test_install_preserves_existing_config_and_never_activates(self) -> None:
        script = (ROOT / "install.sh").read_text()
        for forbidden in ("systemctl --user enable", "systemctl --user start", "systemctl --user restart", "daemon-reload", "--now"):
            self.assertNotIn(forbidden, script)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            config_dir = home / ".config/herdr-supervisor"
            config_dir.mkdir(parents=True)
            config = config_dir / "config.json"
            config.write_text('{"human": "keep"}\n')
            config.chmod(0o600)
            self.fake_herdr(home)
            result = self.run_script("install.sh", home=home, fake_bin=self.fake_commands(root))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(config.read_text(), '{"human": "keep"}\n')

    def test_uninstall_dry_run_is_bounded_and_default_preserves_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            home.mkdir()
            self.fake_herdr(home)
            fake_bin = self.fake_commands(root)
            self.assertEqual(self.run_script("install.sh", home=home, fake_bin=fake_bin).returncode, 0)
            marker = home / ".local/state/herdr-supervisor/human-state.json"
            marker.write_text("keep\n")
            dry = self.run_script("uninstall.sh", "--dry-run", home=home, fake_bin=fake_bin)
            self.assertEqual(dry.returncode, 0, dry.stderr)
            self.assertTrue((home / ".local/bin/herdr-supervisor").exists())
            removed = self.run_script("uninstall.sh", home=home, fake_bin=fake_bin)
            self.assertEqual(removed.returncode, 0, removed.stderr)
            self.assertFalse((home / ".local/bin/herdr-supervisor").exists())
            self.assertTrue(marker.exists())
            refused = self.run_script("uninstall.sh", "--purge", home=home, fake_bin=fake_bin)
            self.assertEqual(refused.returncode, 2)
            self.assertTrue(marker.exists())

    def test_install_requires_herdr_executable_before_any_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            home.mkdir()
            fake_bin = self.fake_commands(root)
            # Missing: both dry run and real install refuse with exit 2 and create nothing.
            for args in (("--dry-run",), ()):
                result = self.run_script("install.sh", *args, home=home, fake_bin=fake_bin)
                self.assertEqual(result.returncode, 2, result.stdout)
                self.assertIn("Herdr executable not found", result.stderr)
                self.assertEqual(sorted(p.name for p in home.iterdir()), [], "install must not create anything")
            # Present but not executable is still unsupported.
            path = self.fake_herdr(home)
            path.chmod(0o644)
            result = self.run_script("install.sh", home=home, fake_bin=fake_bin)
            self.assertEqual(result.returncode, 2, result.stdout)
            self.assertFalse((home / ".local/lib").exists())
            # Supported layout succeeds and the dry run names the executable it found.
            path.chmod(0o755)
            dry = self.run_script("install.sh", "--dry-run", home=home, fake_bin=fake_bin)
            self.assertEqual(dry.returncode, 0, dry.stderr)
            self.assertIn("Found Herdr executable", dry.stdout)
            result = self.run_script("install.sh", home=home, fake_bin=fake_bin)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("No service was loaded", result.stdout)

    def test_uninstall_guard_refuses_active_transitioning_and_unknown_service_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            home.mkdir()
            self.fake_herdr(home)
            fake_bin = self.fake_commands(root)
            self.assertEqual(self.run_script("install.sh", home=home, fake_bin=fake_bin).returncode, 0)
            launcher = home / ".local/bin/herdr-supervisor"
            cases = {
                "active": ("echo active\nexit 0\n", "is active"),
                "activating": ("echo activating\nexit 3\n", "is activating"),
                "bus unreachable": ("echo 'Failed to connect to user scope bus' >&2\nexit 1\n", "Cannot establish the state"),
                "empty answer": ("exit 4\n", "Cannot establish the state"),
            }
            for label, (body, message) in cases.items():
                with self.subTest(label):
                    self.fake_systemctl(fake_bin, body)
                    result = self.run_script("uninstall.sh", home=home, fake_bin=fake_bin)
                    self.assertEqual(result.returncode, 2, f"{label}: {result.stdout}")
                    self.assertIn(message, result.stderr)
                    self.assertTrue(launcher.exists(), f"{label}: guard must remove nothing")
                    # --dry-run never queries service state and never removes anything.
                    dry = self.run_script("uninstall.sh", "--dry-run", home=home, fake_bin=fake_bin)
                    self.assertEqual(dry.returncode, 0, dry.stderr)
                    self.assertTrue(launcher.exists())
            # Established inactive/failed (including "inactive" for a not-found unit, exit 4) proceeds.
            for body in ("echo inactive\nexit 3\n", "echo failed\nexit 3\n", "echo inactive\nexit 4\n"):
                self.fake_systemctl(fake_bin, body)
                self.assertEqual(self.run_script("install.sh", home=home, fake_bin=fake_bin).returncode, 0)
                result = self.run_script("uninstall.sh", home=home, fake_bin=fake_bin)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertFalse(launcher.exists())

    def test_readme_names_required_integrations_and_plugin_role(self) -> None:
        readme = (ROOT / "README.md").read_text()
        requirements = readme.split("## Requirements", 1)[1].split("\n## ", 1)[0]
        self.assertIn("`~/.local/bin/herdr`", requirements)
        self.assertIn("`herdr-agent-quota`", requirements)
        for role in ("quota snapshots", "refresh"):
            self.assertIn(role, requirements)
        self.assertIn("app-server", requirements)
        self.assertIn("Codex and Claude CLIs", requirements)
        for optional in ("api.telegram.org:443", "`git`", "`ssh`", "`rsync`"):
            self.assertIn(optional, requirements)
        # The plugin the source actually invokes is the one the README names.
        source = (ROOT / "src/herdr_supervisor.py").read_text()
        self.assertIn('"--plugin", "herdr-agent-quota"', source)

    def test_shell_scripts_parse(self) -> None:
        for script in ("install.sh", "uninstall.sh", "bin/herdr-supervisor", "bin/herdr-telegram", "bin/herdr-query-worker", "bin/herdr-backup", "bin/herdr-codex-reset-worker"):
            result = subprocess.run(["sh", "-n", str(ROOT / script)], capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 0, f"{script}: {result.stderr}")

    def test_no_private_runtime_or_machine_specific_content(self) -> None:
        forbidden_names = {"owners.json", "state.json", "bot-token", "offset.json", "control.json"}
        # Generic markers derive from the packaging machine. A maintainer's private denylist (owner/chat
        # IDs, session IDs, private project names, hosts) must never be published, so it stays outside
        # the repository and is supplied as a file path, one marker per line, '#' comments allowed.
        forbidden_content = {"/home/" + getpass.getuser()}
        markers_file = os.environ.get("HERDR_RELEASE_PRIVATE_MARKERS")
        if markers_file:
            forbidden_content.update(
                line.strip() for line in Path(markers_file).read_text().splitlines()
                if line.strip() and not line.lstrip().startswith("#")
            )
        for path in ROOT.rglob("*"):
            if ".git" in path.parts or "__pycache__" in path.parts or not path.is_file():
                continue
            self.assertNotIn(path.name, forbidden_names, str(path))
            try:
                text = path.read_text()
            except UnicodeDecodeError:
                continue
            for marker in forbidden_content:
                self.assertNotIn(marker, text, f"{path}: private marker")

    def test_no_runtime_state_or_compiled_files_are_publishable(self) -> None:
        ignore_rules = (ROOT / ".gitignore").read_text().splitlines()
        for rule in ("__pycache__/", "*.py[cod]", "scripts/dev/", "build/", "dist/", "*.egg-info/"):
            self.assertIn(rule, ignore_rules, rule)
        if (ROOT / ".git").exists():  # an extracted sdist carries the rules but no repository
            ignored = subprocess.run(
                ["git", "check-ignore", "src/__pycache__/module.pyc", "tests/__pycache__/case.pyc", "scripts/dev/staging.py"],
                cwd=ROOT, capture_output=True, text=True, check=False,
            )
            self.assertEqual(ignored.returncode, 0, ignored.stderr)
        manifest = (ROOT / "MANIFEST.txt").read_text()
        self.assertNotIn("__pycache__", manifest)
        self.assertNotIn("scripts/dev", manifest)
        for name in ("outbox", "inbox", "logs", "task-files"):
            self.assertFalse((ROOT / name).exists(), name)


if __name__ == "__main__":
    unittest.main()
