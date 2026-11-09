"""Guided full profile: reviewed component selection, protected token input, exact pinned quota staging/link/enable
order, opt-in service activation, honest ACTION_REQUIRED for everything that stays with the user, uncertain external
outcomes that are reconciled and never repeated, and an integrated readiness report."""

from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from typing import Any

import herdr_core as core
import herdr_installation as hi
import herdr_setup as hsetup
from setup_fixtures import FakeQuota, SetupCase, snapshot
from v2_fixtures import FAKE_TOKEN

STAGE_NAME = "herdr-agent-quota-809749e58548"


class FullCase(SetupCase):
    def setUp(self) -> None:
        super().setUp()
        self.quota = FakeQuota()
        self.token_file = self.tmp / "token-source"
        self.token_file.write_text(FAKE_TOKEN + "\n")
        self.token_file.chmod(0o600)
        self.project = self.tmp / "project"
        self.project.mkdir()
        (self.project / "AGENTS.md").write_text("agents\n")
        self.stage = self.tmp / "quota-stage"

    def full(self, *extra: str, apply: bool = False, adapters: hsetup.Adapters | None = None, ask: Any = None) -> tuple[int, str, str]:
        argv = ["setup", "--profile", "full", "--payload", str(self.payload), "--quota-stage-dir", str(self.stage), *extra]
        if apply:
            argv += ["--apply", "--approve-current"]
        return self.run_tool(*argv, adapters=adapters or self.adapters(self.quota), ask=ask)

    def selected(self) -> tuple[str, ...]:
        return ("--telegram-owner-id", "111", "--telegram-chat-id", "222", "--telegram-token-file", str(self.token_file))

    def external_commands(self) -> list[list[str]]:
        return [call for call in self.host.calls if Path(call[0]).name in ("herdr", "systemctl")]


class FullPreviewTests(FullCase):
    def test_full_preview_names_every_remaining_manual_step_and_writes_nothing(self) -> None:
        before = snapshot(self.home)
        self.host.present.discard("codex")
        code, out, err = self.full()
        self.assertEqual(code, 0, err)
        self.assertEqual(snapshot(self.home), before)
        self.assertFalse(self.stage.exists())
        self.assertEqual(self.quota.count("bootstrap"), 0, "a preview never stages, builds or downloads")
        self.assertEqual(self.quota.count("link") + self.quota.count("enable"), 0)
        self.assertEqual(self.host.service_calls(), [])
        self.assertIn("Remaining for you (ACTION_REQUIRED)", out)
        for needle in ("Telegram is not configured", "codex CLI", "log in yourself", "sessions"):
            self.assertIn(needle, out)
        self.assertNotIn("READY", out.replace("ACTION_REQUIRED", ""))
        read_only = [call for call in self.external_commands()]
        self.assertEqual([call for call in read_only if call[1:3] not in (["agent", "list"],)], [], "only the read-only agent listing reaches Herdr")

    def test_the_default_full_components_and_option_validation(self) -> None:
        document = self.plan_json("setup", "--profile", "full", "--payload", str(self.payload), adapters=self.adapters(self.quota))
        self.assertEqual(document["plan"]["options"]["components"], ["quota", "sessions", "telegram"])
        self.assertEqual(document["plan"]["options"]["services"], [])
        for bad in (("--components", "everything"), ("--services", "nginx")):
            code, _out, err = self.full(*bad)
            self.assertEqual(code, hi.EXIT_USAGE, err)
        code, _out, err = self.run_tool("setup", "--profile", "minimal", "--payload", str(self.payload), "--telegram-owner-id", "5")
        self.assertEqual(code, hi.EXIT_USAGE)
        self.assertIn("full", err)


class FullApplyTests(FullCase):
    def test_selected_components_run_in_the_documented_order_with_exact_options(self) -> None:
        self.quota.dry_code = 0
        self.host.agents = []
        self.host.enabled_units.clear()
        code, out, err = self.full(*self.selected(), "--project-root", str(self.project), "--services", "worker,telegram", apply=True)
        self.assertEqual(code, hi.EXIT_ACTION, "a full setup that still needs the user reports ACTION_REQUIRED, never READY\n" + out + err)
        journal = json.loads(next(path for path in self.layout.ops.glob("*.json")).read_text())
        order = [step["id"] for step in journal["steps"]]
        self.assertEqual(order, ["dirs", "generation", "backup", "assets", "config", "switch", "compat_links", "receipt", "smoke", "config_paths", "telegram_setup", "quota_stage", "quota_link", "quota_enable",
                                 "activation_reload", "enable:herdr-telegram.service", "start:herdr-telegram.service", "enable:herdr-supervisor-worker.path", "start:herdr-supervisor-worker.path"])
        self.assertEqual({step["status"] for step in journal["steps"]}, {"DONE"})
        self.assertEqual([call for call in self.quota.calls if call[0] != "check"][-3:], [("bootstrap", str(self.stage), True), ("link", str(self.stage / STAGE_NAME)), ("enable",)],
                         "stage, then link the exact staged directory, then enable")
        self.assertEqual([call[2:] for call in self.host.service_calls()], [["daemon-reload"], ["enable", "herdr-telegram.service"], ["start", "herdr-telegram.service"],
                                                                         ["enable", "herdr-supervisor-worker.path"], ["start", "herdr-supervisor-worker.path"]])
        self.assertFalse([part for call in self.host.calls for part in call if part == "--now"])
        telegram = json.loads(self.layout.tg_config.read_text())
        self.assertEqual((telegram["mode"], telegram["owner_user_id"], telegram["chat_id"]), ("shadow", 111, 222), "Telegram starts in shadow mode")
        token_path = self.layout.tg_config.parent / "bot-token"
        self.assertEqual((token_path.read_text().strip(), stat.S_IMODE(token_path.stat().st_mode)), (FAKE_TOKEN, 0o600))
        self.assertEqual(json.loads(self.layout.sup_config.read_text())["project_root"], str(self.project))

    def test_the_token_never_appears_in_plans_journals_receipts_output_or_arguments(self) -> None:
        plan_file = self.tmp / "plan.json"
        code, out, err = self.full(*self.selected(), "--plan-out", str(plan_file))
        self.assertEqual(code, 0, err)
        code2, out2, err2 = self.full(*self.selected(), apply=True)
        self.assertNotEqual(code2, hi.EXIT_REFUSED, out2 + err2)
        haystacks = [out, err, out2, err2, plan_file.read_text(), json.dumps(self.host.calls), json.dumps(self.quota.calls)]
        for path in self.layout.manager.rglob("*"):
            if path.is_file():
                haystacks.append(path.read_text(errors="replace"))
        for path in (self.layout.sup_config, self.layout.backup_config, self.layout.receipt):
            haystacks.append(path.read_text())
        for index, text in enumerate(haystacks):
            self.assertNotIn(FAKE_TOKEN, text, f"secret leaked into artifact {index}")
        self.assertNotIn(FAKE_TOKEN.split(":")[1], "".join(haystacks))

    def test_repeating_a_full_setup_is_an_idempotent_noop(self) -> None:
        self.full(*self.selected(), "--services", "worker", apply=True)
        before = snapshot(self.home)
        self.reset_calls()
        quota_calls = len(self.quota.calls)
        code, out, _err = self.full(*self.selected(), "--services", "worker", apply=True)
        self.assertIn("NOOP", out)
        self.assertEqual(snapshot(self.home), before)
        self.assertEqual(self.host.service_calls(), [], "nothing is enabled or started twice")
        self.assertEqual(self.quota.count("bootstrap") + self.quota.count("link") + self.quota.count("enable"), 3, "no external quota action was repeated")
        self.assertGreaterEqual(len(self.quota.calls), quota_calls)
        self.assertIn(code, (0, hi.EXIT_ACTION))

    def ready_fixture(self, *, server_unit: bool = True, linger: str = "yes", enabled: str = "enabled") -> None:
        """A system where everything a full setup configures is genuinely in place: exact live identities, running watcher and bridge."""
        codex, claude = "11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"
        self.host.agents = [{"name": "codex-main", "agent": "codex", "pane_id": "w1:p1", "agent_session": {"value": codex}},
                            {"name": "claude-main", "agent": "claude", "pane_id": "w1:p2", "agent_session": {"value": claude}}]
        self.quota.state = {"state": "pinned", "detail": "installed at the locked revision", "enabled": True, "action": "none"}
        self.quota.fresh = True
        owners = self.layout.paths.owners_file
        owners.parent.mkdir(parents=True, exist_ok=True)
        owners.write_text(json.dumps({"codex": {"pane_id": "w1:p1", "session_id": codex}, "claude": {"pane_id": "w1:p2", "session_id": claude}}))
        self.host.linger = linger
        code, out, err = self.full(*self.selected(), "--project-root", str(self.project), "--services", "worker,telegram", apply=True)
        self.assertIn(code, (0, hi.EXIT_ACTION), out + err)
        for unit in ("herdr-supervisor-worker.path", "herdr-telegram.service"):
            self.assertEqual(self.host.active.get(unit), "active")
            self.host.enabled_units[unit] = enabled
        if server_unit:
            self.host.enabled_units["herdr-server.service"] = "enabled"

    def status(self) -> dict[str, Any]:
        return json.loads(self.run_tool("--json", "status", adapters=self.adapters(self.quota))[1])  # type: ignore[no-any-return]

    def test_everything_installed_and_present_reports_ready_only_when_every_check_passes(self) -> None:
        self.ready_fixture()
        report = self.status()
        items = {item["id"]: item["status"] for item in report["items"]}
        self.assertEqual(report["overall"], "READY", items)
        self.assertEqual((items["sessions"], items["herdr-server"], items["service-chain"], items["reboot-persistence"], items["telegram-network"]), ("OK", "OK", "OK", "OK", "NOT_VERIFIED"),
                         "READY covers local checks only; the unproven network health is listed, not hidden")
        self.assertFalse([call for call in self.host.calls if Path(call[0]).name == "herdr" and call[1:2] == ["plugin"] and call[2:3] == ["action"]])

    def test_an_interactive_only_server_is_ready_now_but_not_reboot_persistent(self) -> None:
        self.ready_fixture(server_unit=False)
        report = self.status()
        self.assertEqual(report["overall"], "READY_NOW")
        item = next(item for item in report["items"] if item["id"] == "reboot-persistence")
        self.assertEqual(item["status"], "ACTION_REQUIRED")
        self.assertIn("headless Herdr server unit enabled: False", item["detail"])
        self.assertIn("interactive Herdr is usable now but is not reboot-persistent", item["action"])
        self.assertFalse([call for call in self.host.service_calls() if call[2] == "start" and "server" in call[3]], "status never starts another server")

    def test_runtime_only_enablement_is_not_persistence(self) -> None:
        self.ready_fixture(enabled="enabled-runtime")
        report = self.status()
        self.assertEqual(report["overall"], "READY_NOW")
        self.assertFalse(report["boot"]["enabled_units"])

    def test_missing_boot_dependencies_are_each_reported(self) -> None:
        self.ready_fixture(linger="no")
        report = self.status()
        item = next(item for item in report["items"] if item["id"] == "reboot-persistence")
        self.assertEqual((report["overall"], item["status"]), ("READY_NOW", "ACTION_REQUIRED"))
        self.assertIn("linger: disabled", item["detail"])

    def test_missing_platform_prerequisites_are_guided_not_installed(self) -> None:
        self.host.present.clear()
        (self.layout.bin / "herdr").unlink()
        code, out, err = self.full(apply=True)
        self.assertEqual(code, hi.EXIT_ACTION, out + err)
        for needle in ("install Herdr user-scoped", "install the codex CLI and log in yourself", "install the claude CLI and log in yourself"):
            self.assertIn(needle, out)
        self.assertFalse([call for call in self.host.calls if Path(call[0]).name in ("sudo", "apt", "pip", "curl", "npm")], "no package manager, root or network command")
        self.assertTrue(self.layout.lib.exists(), "program files are still installed inactive; the rest is reported")


class SetupQuietPointTests(FullCase):
    def test_setup_that_would_change_something_refuses_while_a_worker_runs(self) -> None:
        self.install()
        with core.WorkerLock(self.layout.paths.lock_file):
            before = snapshot(self.home)
            code, out, err = self.full(*self.selected(), apply=True)
            self.assertEqual(code, hi.EXIT_REFUSED, out + err)
            self.assertIn("worker", out + err)
            self.assertEqual(snapshot(self.home), before)
            code, out, _err = self.run_tool("setup", "--payload", str(self.payload), "--apply", "--approve-current")
            self.assertEqual(code, 0, "an already-installed minimal setup is a no-op and needs no quiet point")
            self.assertIn("NOOP", out)


class TelegramComponentTests(FullCase):
    def test_noninteractive_runs_cannot_collect_a_secret(self) -> None:
        code, out, err = self.full("--telegram-owner-id", "111", apply=True)
        self.assertEqual(code, hi.EXIT_ACTION, out + err)
        self.assertIn("cannot collect a secret", out)
        self.assertEqual(json.loads(self.layout.tg_config.read_text())["mode"], "unconfigured")
        self.assertFalse((self.layout.tg_config.parent / "bot-token").exists())

    def test_the_token_source_file_must_be_private(self) -> None:
        self.token_file.chmod(0o644)
        before = snapshot(self.home)
        code, out, err = self.full(*self.selected(), apply=True)
        self.assertEqual(code, hi.EXIT_REFUSED, out + err)
        self.assertIn("0600", out + err)
        self.assertEqual(snapshot(self.home), before)

    def test_an_invalid_token_stops_before_anything_is_written(self) -> None:
        self.token_file.write_text("not-a-token\n")
        code, out, err = self.full(*self.selected(), apply=True)
        self.assertNotEqual(code, 0)
        self.assertFalse((self.layout.tg_config.parent / "bot-token").exists())
        self.assertEqual(json.loads(self.layout.tg_config.read_text())["mode"], "unconfigured")

    def test_an_existing_credential_is_never_overwritten(self) -> None:
        secret = self.layout.tg_config.parent / "bot-token"
        secret.parent.mkdir(parents=True)
        secret.write_text("existing-secret\n")
        secret.chmod(0o600)
        code, out, err = self.full(*self.selected(), apply=True)
        self.assertEqual(code, hi.EXIT_REFUSED, out + err)
        self.assertEqual(secret.read_text(), "existing-secret\n")

    def test_an_already_configured_bot_is_preserved(self) -> None:
        self.install()
        configured = {**json.loads(self.layout.tg_config.read_text()), "mode": "shadow", "owner_user_id": 9, "token_file": str(self.layout.tg_config.parent / "bot-token")}
        self.layout.tg_config.write_text(json.dumps(configured))
        before = self.layout.tg_config.read_bytes()
        code, out, err = self.full(*self.selected(), apply=True)
        self.assertEqual(self.layout.tg_config.read_bytes(), before)
        self.assertNotIn("telegram_setup", out)

    def test_interactive_setup_prompts_for_choices_and_collects_the_token_without_echo(self) -> None:
        asked: list[str] = []
        secrets: list[str] = []

        def make_ask(digest: str) -> Any:
            answers = {"Profile": "full", "Set up paths": "n", "Set up sessions": "y", "Set up telegram": "y", "Set up quota": "n", "Telegram owner": "111", "Telegram chat": "",
                       "Enable and start the worker": "y"}

            def ask(prompt: str) -> str:
                asked.append(prompt)
                if prompt.startswith("Type the first 12"):
                    return digest
                for key, value in answers.items():
                    if prompt.startswith(key):
                        return value
                return "n"
            return ask

        def secret(prompt: str) -> str:
            secrets.append(prompt)
            return FAKE_TOKEN

        adapters = self.adapters(self.quota, interactive=True, secret=secret)
        argv = ["setup", "--payload", str(self.payload), "--interactive", "--quota-stage-dir", str(self.stage)]
        code, out, err = self.run_tool(*argv, adapters=adapters, ask=make_ask("wrong-answer"))
        self.assertEqual(code, 0, out + err)
        self.assertIn("Stopped; nothing was changed", out)
        self.assertFalse(self.layout.lib.exists(), "anything but the exact hash prefix stops without a write")
        self.assertEqual(secrets, [], "no token was requested before approval")
        shown = [line for line in out.splitlines() if line.startswith("Approval hash:")][0].split()[-1]
        code, out, err = self.run_tool(*argv, adapters=adapters, ask=make_ask(shown[:12]))
        self.assertIn(code, (0, hi.EXIT_ACTION), out + err)
        self.assertEqual(secrets, ["Telegram bot token (not echoed): "], "the protected, non-echoed prompt appears only when the approved plan runs")
        self.assertEqual((self.layout.tg_config.parent / "bot-token").read_text().strip(), FAKE_TOKEN)
        self.assertEqual(json.loads(self.layout.tg_config.read_text())["owner_user_id"], 111)
        self.assertNotIn(FAKE_TOKEN, out + err)
        self.assertTrue(any(prompt.startswith("Profile") for prompt in asked))
        self.assertEqual([call[2:] for call in self.host.service_calls()][-2:], [["enable", "herdr-supervisor-worker.path"], ["start", "herdr-supervisor-worker.path"]])

    def test_interactive_needs_a_terminal(self) -> None:
        code, _out, err = self.run_tool("setup", "--interactive", "--payload", str(self.payload))
        self.assertEqual(code, hi.EXIT_USAGE)
        self.assertIn("terminal", err)


class PathsComponentTests(FullCase):
    def test_requested_paths_are_set_narrowly_and_the_preimage_is_recorded(self) -> None:
        self.install()
        original = json.loads(self.layout.sup_config.read_text())
        original["custom_setting"] = {"keep": True}
        self.layout.sup_config.write_text(json.dumps(original))
        review = self.tmp / "reviews"
        review.mkdir()
        code, out, err = self.run_tool("setup", "--profile", "full", "--payload", str(self.payload), "--components", "paths", "--project-root", str(self.project), "--review-root", str(review),
                                       "--apply", "--approve-current", adapters=self.adapters(self.quota))
        self.assertIn(code, (0, hi.EXIT_ACTION), out + err)
        edited = json.loads(self.layout.sup_config.read_text())
        self.assertEqual((edited["project_root"], edited["review_root"], edited["custom_setting"]), (str(self.project), str(review), {"keep": True}))
        self.assertEqual({key for key in edited if edited[key] != original.get(key)}, {"project_root", "review_root"}, "nothing else in the configuration changed")
        self.assertEqual(stat.S_IMODE(self.layout.sup_config.stat().st_mode), 0o600)
        core.load_config(self.layout.sup_config)
        manifest = json.loads(next(self.layout.backups.glob("*/manifest.json")).read_text())
        entry = next(item for item in manifest["entries"] if item["role"] == "config")
        self.assertEqual(entry["before"]["sha256"], hi.sha256_bytes(json.dumps(original).encode()), "the protected preimage is recorded")

    def test_a_conflicting_existing_value_and_bad_paths_are_refused(self) -> None:
        self.install()
        existing = json.loads(self.layout.sup_config.read_text())
        existing["project_root"] = str(self.tmp / "somewhere-else")
        self.layout.sup_config.write_text(json.dumps(existing))
        before = snapshot(self.home)
        code, out, err = self.run_tool("setup", "--profile", "full", "--payload", str(self.payload), "--components", "paths", "--project-root", str(self.project), "--apply", "--approve-current",
                                       adapters=self.adapters(self.quota))
        self.assertEqual(code, hi.EXIT_REFUSED)
        self.assertIn("preserved", out + err)
        code, out, err = self.run_tool("setup", "--profile", "full", "--payload", str(self.payload), "--components", "paths", "--review-root", str(self.tmp / "missing"), "--apply", "--approve-current",
                                       adapters=self.adapters(self.quota))
        self.assertEqual(code, hi.EXIT_REFUSED)
        self.assertIn("existing absolute directory", out + err)
        self.assertEqual(snapshot(self.home), before)


class SessionComponentTests(FullCase):
    def test_existing_sessions_are_only_reported_never_created_restarted_or_rebound(self) -> None:
        self.host.agents = [{"name": "codex-main", "agent": "codex"}, {"name": "claude-main", "agent": "claude"}]
        code, out, err = self.full(apply=True)
        self.assertEqual(code, hi.EXIT_ACTION, out + err)
        self.assertIn("herdr-supervisor sessions", out)
        self.assertFalse(self.layout.paths.owners_file.exists(), "ownership is recorded by the supervised first task, never by setup")
        herdr_calls = [call[1:3] for call in self.host.calls if Path(call[0]).name == "herdr"]
        self.assertTrue(herdr_calls and all(call[0] in ("--version", "plugin") or call == ["agent", "list"] for call in herdr_calls), herdr_calls)
        self.assertFalse([call for call in herdr_calls if call[0] == "agent" and call[1] != "list"], "no agent start/prompt/rename/clear")

    def test_recorded_ownership_is_preserved(self) -> None:
        self.layout.state.mkdir(parents=True)
        owners = self.layout.paths.owners_file
        owners.write_text(json.dumps({"codex": {"pane_id": "w9:p9", "session_id": "keep"}, "claude": {"pane_id": "w9:p8", "session_id": "keep2"}}))
        before = owners.read_bytes()
        code, out, err = self.full(apply=True)
        self.assertEqual(owners.read_bytes(), before)
        self.assertIn("never rebound", out + err)

    def test_missing_agents_say_exactly_what_to_do(self) -> None:
        self.host.agents = []
        code, out, _err = self.full()
        self.assertIn("agents not found or ambiguous: codex-main, claude-main", out)
        self.assertIn("never creates or restarts a session", out)
        self.host.agents = [{"name": "codex-main", "agent": "codex"}, {"name": "codex-main", "agent": "codex"}, {"name": "claude-main", "agent": "claude"}]
        code, out, _err = self.full()
        self.assertIn("agents not found or ambiguous: codex-main", out, "a duplicated alias is ambiguity, never a match")


class QuotaComponentTests(FullCase):
    def test_a_conflicting_plugin_is_never_overwritten(self) -> None:
        self.quota.state = {"state": "conflict", "detail": "revision differs", "enabled": True, "action": "human decides"}
        before = snapshot(self.home)
        code, out, err = self.full(apply=True)
        self.assertEqual(code, hi.EXIT_REFUSED)
        self.assertIn("conflict", out + err)
        self.assertEqual(self.quota.count("bootstrap") + self.quota.count("link") + self.quota.count("enable"), 0)
        self.assertEqual(snapshot(self.home), before)

    def test_an_existing_matching_plugin_is_preserved_and_only_enabled_when_disabled(self) -> None:
        self.quota.state = {"state": "pinned", "detail": "installed", "enabled": True, "action": "none"}
        self.full(apply=True)
        self.assertEqual((self.quota.count("bootstrap"), self.quota.count("link"), self.quota.count("enable")), (0, 0, 0))
        self.quota.state = {"state": "pinned", "detail": "installed", "enabled": False, "action": "none"}
        self.layout.lib.parent.mkdir(parents=True, exist_ok=True)
        code, out, err = self.run_tool("setup", "--profile", "full", "--payload", str(self.payload), "--components", "quota", "--quota-stage-dir", str(self.stage), "--apply", "--approve-current",
                                       adapters=self.adapters(self.quota))
        self.assertEqual((self.quota.count("bootstrap"), self.quota.count("link"), self.quota.count("enable")), (0, 0, 1))

    def test_an_unbuildable_toolchain_is_action_required_not_a_fake_success(self) -> None:
        self.quota.toolchain = "MISSING"
        code, out, err = self.full(apply=True)
        self.assertEqual(code, hi.EXIT_ACTION, out + err)
        self.assertIn("pinned Rust toolchain", out)
        self.assertEqual(self.quota.count("bootstrap"), 0)

    def test_hooks_are_never_configured_by_the_tool(self) -> None:
        self.full(apply=True)
        plugin_calls = [call for call in self.host.calls if Path(call[0]).name == "herdr" and call[1:2] == ["plugin"]]
        self.assertFalse([call for call in plugin_calls if call[2:3] == ["action"]], "the plugin's configure action edits provider hooks and is a separate decision")
        code, out, _err = self.full()
        self.assertIn("never runs it", out)

    def test_an_unreadable_inventory_is_action_required(self) -> None:
        self.quota.state = {"state": "unknown", "detail": "herdr plugin list failed", "enabled": None, "action": "make `herdr plugin list --json` work"}
        code, out, _err = self.full()
        self.assertEqual(code, 0)
        self.assertIn("herdr plugin list failed", out)


class UncertainOutcomeTests(FullCase):
    """An action whose outcome was not recorded is reconciled with a read-only check and is never blindly repeated."""

    def saved(self) -> tuple[Path, str]:
        path = self.tmp / "full-plan.json"
        code, out, err = self.full(*self.selected(), "--plan-out", str(path))
        self.assertEqual(code, 0, err)
        return path, json.loads(path.read_text())["approval_hash"]

    def crash(self, path: Path, digest: str, point: str, adapters: hsetup.Adapters) -> None:
        def fault(name: str) -> None:
            if name == point:
                raise hi.Crash(point)

        env = hi.Env(self.layout, host=self.host, lock_wait=1.0, fault=fault)
        with self.assertRaises(hi.Crash):
            self.run_tool("setup", "--apply", "--plan", str(path), "--approve", digest, env=env, adapters=adapters)

    def resume(self, path: Path, digest: str, adapters: hsetup.Adapters) -> tuple[int, str, str]:
        return self.run_tool("setup", "--apply", "--plan", str(path), "--approve", digest, adapters=adapters)

    def test_link_that_took_effect_before_the_crash_is_recorded_not_repeated(self) -> None:
        path, digest = self.saved()
        adapters = self.adapters(self.quota)
        self.crash(path, digest, "after:quota_link", adapters)
        self.assertEqual(self.quota.count("link"), 1)
        code, out, err = self.resume(path, digest, adapters)
        self.assertIn(code, (0, hi.EXIT_ACTION), out + err)
        self.assertEqual(self.quota.count("link"), 1, "the link was not issued a second time")
        self.assertEqual((self.quota.count("bootstrap"), self.quota.count("enable")), (1, 1))

    def test_a_crash_before_the_command_ran_reruns_it_exactly_once(self) -> None:
        path, digest = self.saved()
        adapters = self.adapters(self.quota)
        self.crash(path, digest, "before:quota_link", adapters)
        self.assertEqual(self.quota.count("link"), 0)
        self.resume(path, digest, adapters)
        self.assertEqual(self.quota.count("link"), 1)

    def test_an_outcome_that_cannot_be_established_stays_reconciling_and_is_not_retried(self) -> None:
        path, digest = self.saved()
        adapters = self.adapters(self.quota)
        self.crash(path, digest, "after:quota_link", adapters)
        self.quota.state = {"state": "unknown", "detail": "inventory unreadable", "enabled": None, "action": "fix herdr"}
        code, out, err = self.resume(path, digest, adapters)
        self.assertEqual(code, hi.EXIT_RECONCILING, out + err)
        self.assertIn("RECONCILING", out)
        self.assertIn("not repeated", out)
        self.assertEqual(self.quota.count("link"), 1)
        self.assertEqual(self.quota.count("enable"), 0, "nothing after the uncertain step ran")
        journal = json.loads((self.layout.ops / f"{digest[:32]}.json").read_text())
        self.assertEqual(journal["status"], "RECONCILING")
        again = self.resume(path, digest, adapters)
        self.assertEqual(again[0], hi.EXIT_RECONCILING, "retrying does not guess either")
        self.assertEqual(self.quota.count("link"), 1)

    def test_staging_that_completed_before_the_crash_is_not_staged_again(self) -> None:
        path, digest = self.saved()
        adapters = self.adapters(self.quota)
        self.crash(path, digest, "after:quota_stage", adapters)
        self.assertTrue((self.stage / STAGE_NAME).is_dir())
        self.resume(path, digest, adapters)
        self.assertEqual(self.quota.count("bootstrap"), 1, "the exclusive stage directory is never claimed twice")

    def test_telegram_setup_that_finished_before_the_crash_is_recorded_not_repeated(self) -> None:
        path, digest = self.saved()
        adapters = self.adapters(self.quota)
        self.crash(path, digest, "after:telegram_setup", adapters)
        stored = (self.layout.tg_config.parent / "bot-token").read_bytes()
        self.token_file.write_text("a-different-token-that-must-not-be-read\n")
        self.resume(path, digest, adapters)
        self.assertEqual((self.layout.tg_config.parent / "bot-token").read_bytes(), stored, "the credential was not rewritten on recovery")

    def test_a_failed_external_command_is_reported_with_its_step_and_never_labeled_complete(self) -> None:
        self.quota.fail_enable = True
        code, out, err = self.full(*self.selected(), apply=True)
        self.assertEqual(code, hi.EXIT_REFUSED, out + err)
        self.assertIn("FAILED", out)
        journal = json.loads(next(self.layout.ops.glob("*.json")).read_text())
        self.assertEqual(journal["status"], "FAILED")
        self.assertEqual([step["status"] for step in journal["steps"] if step["id"] == "quota_enable"], ["FAILED"])
        self.assertNotIn("READY", out.replace("ACTION_REQUIRED", ""))
        self.assertEqual(hi.inspect_install(self.layout).layout, "managed", "the program files that were installed stay valid and inactive")


class ServiceSelectionTests(FullCase):
    def test_unconfigured_optional_services_are_not_activated(self) -> None:
        code, out, err = self.full("--services", "telegram,query,backup", apply=True)
        self.assertEqual(code, hi.EXIT_ACTION, out + err)
        self.assertEqual(self.host.service_calls(), [], "nothing was enabled or started")
        for name in ("Telegram is not configured", "no read-only query session is registered", "backup is not enabled and configured"):
            self.assertIn(name, out)

    def test_reset_automation_needs_an_enabled_policy_and_the_codex_cli(self) -> None:
        self.host.present.discard("codex")
        code, out, _err = self.full("--services", "reset", apply=True)
        self.assertEqual(self.host.service_calls(), [])
        self.assertIn("banked-reset automation is disabled in the configuration or the codex CLI is missing", out)
        self.host.present.add("codex")
        self.full("--services", "reset", apply=True)
        self.assertEqual([call[2:] for call in self.host.service_calls()][-2:], [["enable", "herdr-codex-reset-worker.path"], ["start", "herdr-codex-reset-worker.path"]], "an explicit selection with the prerequisites present activates it")

    def test_the_headless_server_is_never_started_next_to_a_live_server(self) -> None:
        self.host.agents = []
        before = snapshot(self.home)
        code, out, err = self.full("--services", "server", apply=True)
        self.assertEqual(code, hi.EXIT_REFUSED, out + err)
        self.assertIn("interactive Herdr server", out + err)
        self.assertEqual(self.host.service_calls(), [])
        self.assertEqual(snapshot(self.home), before)

    def test_an_unreachable_user_manager_is_action_required(self) -> None:
        self.host.systemd_up = False
        code, out, _err = self.full("--services", "worker", apply=True)
        self.assertEqual(code, hi.EXIT_ACTION)
        self.assertIn("user systemd manager cannot be reached", out)
        self.assertEqual(self.host.service_calls(), [])

    def test_services_that_are_already_enabled_and_active_are_skipped(self) -> None:
        self.install()
        self.host.enabled_units["herdr-supervisor-worker.path"] = "enabled"
        self.host.active["herdr-supervisor-worker.path"] = "active"
        self.reset_calls()
        self.full("--services", "worker", apply=True)
        self.assertEqual(self.host.service_calls(), [])

    def test_a_failed_enable_stops_before_start_and_is_reported(self) -> None:
        self.host.fail[("enable", "herdr-supervisor-worker.path")] = hi.CmdResult(1, "", "Unit is masked")
        code, out, err = self.full("--services", "worker", apply=True)
        self.assertEqual(code, hi.EXIT_REFUSED, out + err)
        self.assertIn("Unit is masked", out + err)
        self.assertEqual([call[2] for call in self.host.service_calls()], ["daemon-reload", "enable"])


class ReadinessTests(FullCase):
    def test_minimal_install_reports_installed_but_inactive(self) -> None:
        self.install()
        code, out, _err = self.run_tool("--json", "status", adapters=self.adapters(self.quota))
        report = json.loads(out)
        self.assertEqual(report["overall"], "INSTALLED_INACTIVE")
        self.assertEqual(code, 0)
        self.assertEqual(self.host.service_calls(), [])

    def test_no_installation_is_not_installed(self) -> None:
        code, out, _err = self.run_tool("--json", "status", adapters=self.adapters(self.quota))
        self.assertEqual(json.loads(out)["overall"], "NOT_INSTALLED")
        self.assertEqual(code, hi.EXIT_ACTION)

    def test_current_readiness_and_reboot_persistence_are_reported_separately(self) -> None:
        self.install()
        self.host.active.update({"herdr-supervisor-worker.path": "active", "herdr-telegram.service": "active"})
        self.host.enabled_units.update({"herdr-supervisor-worker.path": "enabled"})
        self.host.linger = "no"
        report = json.loads(self.run_tool("--json", "status", adapters=self.adapters(self.quota))[1])
        items = {item["id"]: item for item in report["items"]}
        self.assertEqual(items["reboot-persistence"]["status"], "ACTION_REQUIRED")
        self.assertIn("linger: disabled", items["reboot-persistence"]["detail"])
        self.assertIn("enable-linger", items["reboot-persistence"]["action"])
        self.assertIn("never does", items["reboot-persistence"]["action"])
        self.assertIn("does not prove Herdr is running", items["reboot-persistence"]["action"])
        self.assertFalse([call for call in self.host.calls if Path(call[0]).name == "sudo"], "linger stays an explicit administrator instruction")
        self.assertEqual(self.host.service_calls(), [])

    def test_modified_managed_files_make_the_status_fail(self) -> None:
        self.install()
        (self.layout.unit / "herdr-backup.timer").write_text("# changed\n")
        report = json.loads(self.run_tool("--json", "status", adapters=self.adapters(self.quota))[1])
        self.assertEqual(report["overall"], "FAILED")


class RealQuotaAdapterTests(FullCase):
    """The real adapter drives the real quota-dependency module against its own fixtures (fake herdr/cargo, local lock and
    archive): no network, and link/enable go through the recording host."""

    def setUp(self) -> None:
        super().setUp()
        from test_quota_dependency import Env as QuotaEnv

        self.qenv = QuotaEnv(self)
        self.herdr_fixture = str(self.qenv.fake_bin / "herdr")
        os.environ["PATH"] = f"{self.qenv.fake_bin}:/usr/bin:/bin"
        self.host.plugin_hook = self.follow_inventory

    def follow_inventory(self, verb: str, argv: list[str]) -> None:
        """The fixture inventory follows the plugin commands the way a real Herdr's would (link --disabled, then enable)."""
        if verb == "link":
            self.qenv.set_inventory(mutate=lambda plugins: plugins[0].update(source={"kind": "local"}, plugin_root=argv[-1], enabled=False, version="1.5.4", plugin_id="herdr-agent-quota"))
        else:
            data = json.loads(self.qenv.inventory.read_text())
            data["result"]["plugins"][0]["enabled"] = True
            self.qenv.inventory.write_text(json.dumps(data))

    def real_adapters(self) -> hsetup.Adapters:
        options = {"quota": {"lock": str(self.qenv.lock_path), "archive": str(self.qenv.archive_path)}}
        probe = hsetup.HerdrProbe(self.env, binary=self.herdr_fixture)
        return hsetup.Adapters(probe=probe, quota=hsetup.QuotaAdapter(self.env, probe, options["quota"]))

    def test_stage_link_enable_use_the_real_module_and_the_supported_commands(self) -> None:
        stage = self.tmp / "real-stage"
        argv = ["setup", "--profile", "full", "--payload", str(self.payload), "--components", "quota", "--quota-stage-dir", str(stage), "--quota-lock", str(self.qenv.lock_path),
                "--quota-archive", str(self.qenv.archive_path)]
        preview_code, out, err = self.run_tool(*argv, adapters=self.real_adapters())
        self.assertEqual(preview_code, 0, out + err)
        self.assertFalse(stage.exists(), "a preview verifies the local archive in a removed temporary directory and stages nothing")
        self.assertNotIn("https", "".join(self.qenv.calls()))
        code, out, err = self.run_tool(*argv, "--apply", "--approve-current", adapters=self.real_adapters())
        self.assertIn(code, (0, hi.EXIT_ACTION), out + err)
        staged = stage / f"herdr-agent-quota-{self.qenv.revision[:12]}"
        self.assertTrue(staged.is_dir() and (stage / "BOOTSTRAP_RECEIPT.json").is_file())
        plugin = [call[1:] for call in self.host.calls if Path(call[0]).name == "herdr" and call[1:2] == ["plugin"]]
        self.assertEqual(plugin, [["plugin", "link", "--disabled", str(staged)], ["plugin", "enable", "herdr-agent-quota"]], "exactly the two supported commands, in order")
        calls = self.qenv.calls()
        self.assertTrue(all(line.startswith(("herdr plugin list --json", "cargo", "rustc", "rustup")) for line in calls), calls)
        self.assertFalse([line for line in calls if "install" in line or "configure" in line or "action" in line])


class PlatformTests(unittest.TestCase):
    def test_python_floor_and_platform_are_checked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / ".local/bin").mkdir(parents=True)
            layout = hi.Layout.from_environment(home)
            env = hi.Env(layout)
            items = hsetup.platform_prerequisites(env, hsetup.HerdrProbe(env, binary=None))
            self.assertEqual({item["id"] for item in items}, {"python", "platform", "herdr", "codex-cli", "claude-cli"})


if __name__ == "__main__":
    unittest.main()
