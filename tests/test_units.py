"""User units and installer: inactive install, fixed commands, no task text, hardening, no --now."""

from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
LIB = _ROOT / "src" if (_ROOT / "src").is_dir() else _ROOT
UNITS = _ROOT / "systemd" if (_ROOT / "systemd").is_dir() else (_ROOT / "units" if (_ROOT / "units").is_dir() else Path.home() / ".config/systemd/user")
INSTALLER = _ROOT / "install.sh" if (_ROOT / "install.sh").exists() else _ROOT / "install-v2.sh"
MANIFEST = _ROOT / "MANIFEST.txt"
UNIT_NAMES = ("herdr-server.service", "herdr-supervisor-worker.service", "herdr-supervisor-worker.path", "herdr-query-worker.service", "herdr-query-worker.path", "herdr-telegram.service", "herdr-backup.service", "herdr-backup.timer", "herdr-codex-reset-worker.service", "herdr-codex-reset-worker.path")


class UnitFileTests(unittest.TestCase):
    def unit(self, name: str) -> str:
        path = UNITS / name
        self.assertTrue(path.exists(), path)
        return path.read_text()

    def test_units_have_fixed_commands_no_task_text_and_hardening(self) -> None:
        for name in UNIT_NAMES:
            text = self.unit(name)
            self.assertNotIn("Environment=", text, name)
            self.assertNotIn("%i", text, name)
            self.assertNotIn("$", text.replace("$HOME", ""), name)
            self.assertNotIn("--now", text)
            if name.endswith(".service"):
                self.assertIn("UMask=0077", text, name)
                self.assertIn("NoNewPrivileges=yes", text, name)
                self.assertIn("PrivateTmp=yes", text, name)
                exec_lines = [line for line in text.splitlines() if line.startswith("ExecStart=")]
                self.assertEqual(len(exec_lines), 1, name)
                self.assertNotIn("sh -c", exec_lines[0], name)
        worker = self.unit("herdr-supervisor-worker.service")
        self.assertIn("ExecStart=%h/.local/bin/herdr-supervisor worker", worker)
        self.assertIn("SuccessExitStatus=0 2 3 4", worker)
        self.assertIn("RestrictAddressFamilies=AF_UNIX", worker)
        self.assertIn("IPAddressDeny=any", worker)
        query = self.unit("herdr-query-worker.service")
        self.assertIn("ExecStart=%h/.local/bin/herdr-query-worker --once", query)
        self.assertIn("IPAddressDeny=any", query)
        reset = self.unit("herdr-codex-reset-worker.service")
        self.assertIn("ExecStart=%h/.local/bin/herdr-codex-reset-worker --once", reset)
        self.assertIn("AF_INET", reset)
        self.assertNotIn("IPAddressDeny=any", reset)
        reset_rw = next(line for line in reset.splitlines() if line.startswith("ReadWritePaths="))
        self.assertEqual(reset_rw.partition("=")[2].split(), ["%h/.codex", "%h/.local/state/herdr-supervisor/codex-reset"])
        self.assertNotIn("%h ", reset_rw + " ")
        telegram = self.unit("herdr-telegram.service")
        self.assertIn("ExecStart=%h/.local/bin/herdr-telegram daemon", telegram)
        rw = next(line for line in telegram.splitlines() if line.startswith("ReadWritePaths="))
        self.assertIn("%h/.local/state/herdr-telegram-locks", rw.split())
        self.assertNotIn("%h/.local/state ", rw + " ")
        self.assertNotIn("%h ", rw + " ")
        self.assertIn("ProtectHome=read-only", telegram)
        self.assertIn("RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6", telegram)
        self.assertNotIn("ListenStream", telegram)
        self.assertNotIn("Sockets=", telegram)
        self.assertIn("Restart=on-failure", telegram)
        for name in ("herdr-supervisor-worker.path", "herdr-query-worker.path", "herdr-codex-reset-worker.path"):
            path_unit = self.unit(name)
            self.assertIn("DirectoryNotEmpty=%h/.local/state/herdr-supervisor/", path_unit)
            self.assertIn("DirectoryMode=0700", path_unit)
        server = self.unit("herdr-server.service")
        self.assertIn("ExecStart=%h/.local/bin/herdr server", server)

    def test_public_units_and_defaults_are_generic(self) -> None:
        for unit in sorted(UNITS.glob("*")):
            self.assertNotIn("V2", unit.read_text(), unit.name)
        import herdr_present
        import herdr_telegram
        self.assertEqual(herdr_telegram.DEFAULT_TIMEZONE, "UTC")
        self.assertEqual(herdr_present.DEFAULT_TIMEZONE, "UTC")
        for example in ("supervisor.example.json", "telegram.example.json"):
            self.assertEqual(json.loads((_ROOT / "config" / example).read_text())["timezone"], "UTC")

    def test_installer_never_activates_services_or_touches_sessions(self) -> None:
        installer = INSTALLER.read_text()
        for forbidden in ("--now", "systemctl --user enable", "systemctl --user start", "systemctl --user restart", "systemctl --user stop", "daemon-reload", "herdr server stop", "herdr agent", "git ", "sudo"):
            self.assertNotIn(forbidden, installer, forbidden)
        self.assertIn("systemd-analyze --user verify", installer)
        self.assertIn('TG_LOCK_DIR="$HOME/.local/state/herdr-telegram-locks"', installer)
        self.assertIn('chmod 0700 "$SUP_CONFIG_DIR"', installer)
        manifest = MANIFEST.read_text()
        self.assertIn("~/.local/state/herdr-telegram-locks/", manifest)
        self.assertIn('"$BIN_DIR/herdr-supervisor" migrate', installer)

    @unittest.skipUnless((Path.home() / ".local/lib/herdr-supervisor/MANIFEST.txt").exists(), "V2 not installed")
    def test_installed_bot_lock_dir_exists_with_0700(self) -> None:
        lock_dir = Path.home() / ".local/state/herdr-telegram-locks"
        self.assertTrue(lock_dir.is_dir())
        self.assertEqual(oct(lock_dir.stat().st_mode & 0o777), "0o700")

    @unittest.skipUnless(shutil.which("systemd-analyze") and (Path.home() / ".local/bin/herdr-telegram").exists(), "launchers not installed yet")
    def test_units_verify_without_loading(self) -> None:
        completed = subprocess.run(["systemd-analyze", "--user", "verify", *[str(UNITS / n) for n in UNIT_NAMES]], capture_output=True, text=True, timeout=60)
        if "SO_PASSCRED failed: Operation not permitted" in completed.stderr:
            self.skipTest("sandbox does not permit systemd user-manager credential sockets")
        self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)

    def test_no_listener_no_vpn_no_firewall_code_in_v2_modules(self) -> None:
        for module in ("herdr_telegram.py", "telegram_api.py", "herdr_query.py", "herdr_supervisor.py", "herdr_core.py", "herdr_cli.py", "herdr_quota.py", "herdr_protocol.py", "herdr_validation.py", "herdr_workflow.py", "herdr_runtime.py", "herdr_command.py"):
            text = (LIB / module).read_text()
            for forbidden in (".bind(", ".listen(", "iptables", "nft ", "ufw ", "wg-quick", "openvpn", "ssh -R", "setWebhook", "deleteWebhook", "HTTPServer", "socketserver"):
                self.assertNotIn(forbidden, text, f"{module}: {forbidden}")


if __name__ == "__main__":
    unittest.main()
