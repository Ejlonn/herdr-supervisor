"""Release-integration regressions that belong to no single feature suite.

The worker readiness of the helper context agrees with the managed-generation installer (launcher and library layout). Empty, whitespace-only or boolean-like systemctl answers are never read as a unit being active or enabled, and a status report built
from them never claims readiness or reboot persistence. The tests drive the real adapter and the real readiness report through the
same scripted host the setup suites use, so nothing touches the machine.
"""

from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import threading
import time
import unittest
from pathlib import Path

import herdr_core as core
import herdr_helper_context as hctx
import herdr_installation as hi
import herdr_pairs as hpair
import herdr_quota_dependency as q
import test_helper_context
import test_named_pairs_telegram
import test_setup_review_fixes as earlier
from pair_fixtures import hs
from test_setup_full import FullCase

ROOT = Path(__file__).resolve().parents[1]

UNITS = ("herdr-supervisor-worker.path", "herdr-telegram.service", "herdr-server.service")


class EmptySystemctlAnswerTests(FullCase):
    CODEX = earlier.ReadinessIdentityTests.CODEX
    CLAUDE = earlier.ReadinessIdentityTests.CLAUDE
    agent = earlier.ReadinessIdentityTests.agent
    ready = earlier.ReadinessIdentityTests.ready
    write_owners = earlier.ReadinessIdentityTests.write_owners
    status = earlier.ReadinessIdentityTests.status

    def test_the_adapter_reads_no_usable_answer_as_unknown_never_as_a_state(self) -> None:
        systemd = hi.Systemd(self.host)
        unit = "herdr-telegram.service"
        for rc in (0, 1, 3, 4):
            for answer in ("", "\n", "   \n", "true", "0", "yes"):
                with self.subTest(verb="is-enabled", rc=rc, answer=answer):
                    self.host.fail[("is-enabled", unit)] = hi.CmdResult(rc, answer, "")
                    self.assertEqual(systemd.enabled(unit), "unknown")
        for rc in (1, 3, 4):
            for answer in ("", "\n", "true", "0", "up"):
                with self.subTest(verb="is-active", rc=rc, answer=answer):
                    self.host.fail[("is-active", unit)] = hi.CmdResult(rc, answer, "")
                    self.assertEqual(systemd.state(unit), "unknown")
        self.host.fail[("is-enabled", unit)] = hi.CmdResult(1, "", "Failed to connect to bus: No medium found")
        self.assertEqual(systemd.enabled(unit), "unknown")

    def test_positive_controls_keep_their_meaning(self) -> None:
        systemd = hi.Systemd(self.host)
        unit = "herdr-telegram.service"
        self.host.enabled_units[unit] = "enabled"
        self.host.active[unit] = "active"
        self.assertEqual((systemd.enabled(unit), systemd.state(unit)), ("enabled", "active"))
        self.host.enabled_units[unit] = "disabled"
        self.host.active[unit] = "inactive"
        self.assertEqual((systemd.enabled(unit), systemd.state(unit)), ("disabled", "inactive"))

    def test_status_never_claims_readiness_or_persistence_from_empty_answers(self) -> None:
        self.ready()
        self.assertEqual(self.status()["overall"], "READY")
        for unit in UNITS:
            with self.subTest(unit=unit, verb="is-enabled"):
                self.host.fail[("is-enabled", unit)] = hi.CmdResult(1, "", "")
                report = self.status()
                self.assertNotEqual(report["overall"], "READY")
                persistence = next(item for item in report["items"] if item["id"] == "reboot-persistence")
                self.assertEqual(persistence["status"], "ACTION_REQUIRED")
                del self.host.fail[("is-enabled", unit)]
        self.assertEqual(self.status()["overall"], "READY")
        # The headless server unit only matters for reboot persistence (a manually opened server is current readiness), so the
        # "active" answer is required for the two units that carry the workflow itself.
        for unit in UNITS[:2]:
            with self.subTest(unit=unit, verb="is-active"):
                self.host.fail[("is-active", unit)] = hi.CmdResult(3, "", "")
                self.assertNotEqual(self.status()["overall"], "READY")
                del self.host.fail[("is-active", unit)]
        self.assertEqual(self.status()["overall"], "READY")


class WorkerLauncherLayoutTests(unittest.TestCase):
    """The worker readiness of the helper context must agree with what the EXACT installed launcher variant would execute. The current
    launcher resolves the generation behind `active` (and refuses a malformed pointer with exit 70, never falling back); the earlier
    launcher always executes the flat library. Each case runs the real launcher against synthetic worker modules and compares."""

    GENERATION = "generations/0.4.0-beta.1-0123456789abcdef"
    LEGACY = subprocess.run(["git", "show", "2279f373771346d27f9b68d25619c75224f35f88:bin/herdr-codex-reset-worker"], cwd=ROOT, capture_output=True, check=False).stdout or (
        b'#!/bin/sh\nset -eu\nexec python3 "$HOME/.local/lib/herdr-supervisor/herdr_codex_reset.py" "$@"\n')

    def setUp(self) -> None:
        self.case = test_helper_context.ReadinessTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.home = self.case.project.home
        self.library = self.home / ".local/lib/herdr-supervisor"
        self.launcher = self.case.project.launcher
        self.modules = ("herdr_codex_reset.py", "herdr_helper_context.py")
        # Synthetic worker modules: the launcher executes the first one; the checker reads the protocol marker and the second one's presence.
        (self.library / "herdr_codex_reset.py").write_text(f"{hctx.WORKER_PROTOCOL_MARKER}\nprint('worker ran')\n")
        (self.library / "herdr_helper_context.py").write_text("# synthetic\n")

    def install_variant(self, variant: str) -> None:
        if variant == "legacy":
            self.launcher.write_bytes(self.LEGACY)
            self.launcher.chmod(0o755)
        self.assertEqual(hashlib.sha256(self.launcher.read_bytes()).hexdigest(), hctx.WORKER_LAUNCHER_SHA256 if variant == "current" else hctx.LEGACY_WORKER_LAUNCHER_SHA256)

    def move_modules_into_generation(self) -> Path:
        generation = self.library / self.GENERATION
        generation.mkdir(parents=True)
        for module in self.modules:
            (self.library / module).rename(generation / module)
        return generation

    def point(self, target: str) -> None:
        (self.library / "active").symlink_to(target)

    def runs(self) -> bool:
        done = subprocess.run([str(self.launcher)], env={"HOME": str(self.home), "PATH": "/usr/bin:/bin"}, capture_output=True, text=True, timeout=60, check=False)
        return done.returncode == 0 and "worker ran" in done.stdout

    def assert_parity(self, label: str, expected: bool) -> None:
        problems = hctx.inspect_worker_runtime(self.home)
        self.assertEqual(self.runs(), expected, f"{label}: the launcher outcome differs from the expectation")
        self.assertEqual(problems == [], expected, f"{label}: readiness must agree with execution, got {problems}")

    def test_current_launcher_flat_and_managed_layouts_are_ready_and_run(self) -> None:
        self.install_variant("current")
        self.assert_parity("current launcher, flat library", True)
        self.move_modules_into_generation()
        self.point(self.GENERATION)
        self.assert_parity("current launcher, managed generation", True)

    def test_legacy_launcher_with_a_flat_library_is_ready_and_runs(self) -> None:
        self.install_variant("legacy")
        self.assert_parity("legacy launcher, flat library", True)

    def test_legacy_launcher_with_modules_only_in_a_managed_generation_is_not_ready_because_it_cannot_run(self) -> None:
        self.install_variant("legacy")
        self.move_modules_into_generation()
        self.point(self.GENERATION)
        self.assert_parity("legacy launcher, generation-only modules", False)

    def test_current_launcher_with_a_malformed_pointer_is_not_ready_even_with_a_valid_flat_library(self) -> None:
        self.install_variant("current")
        for target in ("../elsewhere", "/abs/path", "generations/a/b", "generations/", "generations/a..b", "other"):
            with self.subTest(target=target):
                (self.library / "active").unlink(missing_ok=True)
                self.point(target)
                self.assert_parity(f"current launcher, pointer {target}", False)
                self.assertIn("refuses to run", " ".join(hctx.inspect_worker_runtime(self.home)))

    def test_a_pointer_to_a_generation_without_the_worker_is_not_ready_for_the_current_launcher(self) -> None:
        self.install_variant("current")
        (self.library / self.GENERATION).mkdir(parents=True)
        self.point(self.GENERATION)
        self.assert_parity("current launcher, empty generation", False)

    def test_an_unsupported_launcher_is_refused(self) -> None:
        self.launcher.write_text("#!/bin/sh\nexit 0\n")
        self.assertTrue(any("supported worker launcher" in item for item in hctx.inspect_worker_runtime(self.home)))

    def test_the_full_readiness_report_uses_the_same_layout_rule(self) -> None:
        self.install_variant("current")
        self.move_modules_into_generation()
        self.point(self.GENERATION)
        self.assertTrue(self.case.report()["ready"], self.case.report()["reasons"])
        (self.library / "active").unlink()
        self.point("../elsewhere")
        report = self.case.report()
        self.assertFalse(report["ready"])
        self.assertTrue(any("refuses to run" in reason for reason in report["reasons"]))


class PolicySelectionLockTests(test_named_pairs_telegram.TgPairCase):
    """The workflow-policy choice is a writer of the pending-start record like the pair choice and the Start commit, so it takes the same
    lock: a selection can never interleave with a Start (or overwrite a receipt) and a Start never reads a half-applied selection."""

    test_the_feature_is_invisible_until_an_administrator_enables_it = None

    def test_a_policy_selection_waits_for_the_pending_lock_and_then_applies(self) -> None:
        pending = self.task_card()
        token = self.one("workflow_policy", policy="gated_v2")
        holding, release = threading.Event(), threading.Event()

        def hold() -> None:
            with self.bridge._pending_lock():
                holding.set()
                release.wait(15)

        holder = threading.Thread(target=hold)
        holder.start()
        self.assertTrue(holding.wait(10))
        results: list = []
        tapper = threading.Thread(target=lambda: results.append(self.tap(token)))
        tapper.start()
        time.sleep(0.5)
        self.assertEqual(results, [], "the selection is serialised behind the lock holder")
        self.assertEqual(self.pending(pending).get("workflow_policy"), "supervised_v2", "nothing was applied while the lock was held")
        release.set()
        holder.join(10)
        tapper.join(10)
        self.assertFalse(holder.is_alive() or tapper.is_alive())
        self.assertEqual(results[0].get("result"), "selection_updated")
        self.assertEqual(self.pending(pending)["workflow_policy"], "gated_v2")


class QuotaPluginIdentityTests(unittest.TestCase):
    """The plugin the dependency lock pins is the plugin the supervisor's defaults read and refresh. An installation under the pre-rename
    id is never mistaken for the locked one (and never replaced)."""

    FIXTURES = ROOT / "tests/fixtures/quota-dependency"

    def lock(self) -> dict:
        return q.load_lock(str(ROOT / "config/quota-dependency.json"))

    def inventory(self, **changes) -> dict:
        data = json.loads((self.FIXTURES / "plugin-list-installed-quota-0.9.5.json").read_text())
        plugin = data["result"]["plugins"][0]
        plugin.update(changes)
        return {"status": "OK", "detail": "", "plugins": [plugin]}

    def test_the_lock_the_defaults_and_the_dependency_tool_name_one_plugin(self) -> None:
        plugin_id = self.lock()["plugin_id"]
        config = core.resolve_config_defaults(copy.deepcopy(core.DEFAULT_CONFIG))
        self.assertEqual(Path(config["quota_dir"]).name, plugin_id)
        command = core.DEFAULT_CONFIG["codex_quota_refresh_command"]
        self.assertEqual(command[command.index("--plugin") + 1], plugin_id)
        self.assertEqual(q.default_quota_dir().name, plugin_id)
        self.assertEqual(q.staged_name(self.lock()), f"{plugin_id}-{self.lock()['revision'][:12]}")

    def test_the_recorded_pre_rename_installation_is_a_conflict_never_pinned(self) -> None:
        recorded = self.inventory()
        self.assertEqual(recorded["plugins"][0]["plugin_id"], "herdr-agent-quota", "the fixture is the real earlier plugin")
        state = q.classify(recorded, self.lock())
        self.assertEqual(state["state"], "conflict")
        self.assertIn("pre-rename id herdr-agent-quota", state["detail"])
        self.assertIn("neither replaced nor adopted", state["action"])

    def test_the_locked_plugin_under_the_new_id_is_pinned_and_a_wrong_revision_is_not(self) -> None:
        lock = self.lock()
        source = {"kind": "github", "owner": "Ejlonn", "repo": "herdr-agent-quota", "resolved_commit": lock["revision"]}
        good = self.inventory(plugin_id=lock["plugin_id"], version=lock["manifest_version"], source=source)
        self.assertEqual(q.classify(good, lock)["state"], "pinned")
        for label, changes in (("other revision", {"source": {**source, "resolved_commit": "0" * 40}}), ("other version", {"version": "1.5.4"})):
            with self.subTest(label):
                bad = self.inventory(plugin_id=lock["plugin_id"], version=lock["manifest_version"], source=source)
                bad["plugins"][0].update(changes)
                self.assertEqual(q.classify(bad, lock)["state"], "conflict")

    def test_the_legacy_id_list_is_validated_as_part_of_the_lock(self) -> None:
        raw = json.loads((ROOT / "config/quota-dependency.json").read_text())
        for label, value in (("missing", None), ("not a list", "herdr-agent-quota"), ("own id", ["herdr-agent-usage"]), ("duplicate", ["a-b", "a-b"]), ("bad shape", ["../x"]), ("too many", ["a1", "a2", "a3", "a4", "a5"])):
            with self.subTest(label):
                broken = copy.deepcopy(raw)
                if value is None:
                    del broken["legacy_plugin_ids"]
                else:
                    broken["legacy_plugin_ids"] = value
                with self.assertRaises(q.DependencyError):
                    q.validate_lock(broken)


class MissingSnapshotHintTests(unittest.TestCase):
    """The recovery action of a missing quota snapshot names the plugin the bound lock checks (no live refresh is ever run here)."""

    def test_the_missing_snapshot_action_follows_the_lock_plugin_id(self) -> None:
        import tempfile
        lock = q.load_lock(str(ROOT / "config/quota-dependency.json"))
        with tempfile.TemporaryDirectory() as empty:
            for plugin_id in (lock["plugin_id"], "custom-quota-plugin"):
                with self.subTest(plugin_id=plugin_id):
                    report = q.telemetry_report(Path(empty), 300, plugin_id=plugin_id)
                    for provider, item in report["providers"].items():
                        self.assertEqual(item["state"], "MISSING", provider)
                        self.assertIn(f"--plugin {plugin_id}`", item["action"])
                        self.assertNotIn("herdr-agent-quota", item["action"])

    def test_the_check_report_uses_the_checked_lock(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as home:
            args = q.build_parser().parse_args(["check", "--quota-dir", home, "--herdr", "/nonexistent/herdr"])
            code, report = q.command_check(args)
        self.assertIn(code, (0, 3, 4), "an unreadable inventory is UNKNOWN, never a crash")
        self.assertEqual(report["lock"]["plugin_id"], "herdr-agent-usage")
        self.assertTrue(all("--plugin herdr-agent-usage`" in item["action"] for item in report["telemetry"]["providers"].values()))


class PairRegistrySchemaTypeTests(test_named_pairs_telegram.TgPairCase):
    """The closed integer schema of every registry the pair boundary reads: a JSON boolean or float is not version 1."""

    test_the_feature_is_invisible_until_an_administrator_enables_it = None

    def write(self, path: Path, doc: dict) -> bytes:
        path.write_text(json.dumps(doc))
        return path.read_bytes()

    def test_a_boolean_float_or_string_schema_version_is_refused_and_integer_one_is_accepted(self) -> None:
        self.register("Alpha", test_named_pairs_telegram.ALPHA)
        path = hpair.registry_path(self.paths)
        good = json.loads(path.read_text())
        self.assertEqual(hpair.load_registry(self.paths)["schema_version"], 1, "positive control")
        for label, value in (("true", True), ("float", 1.0), ("string", "1"), ("none", None), ("two", 2)):
            with self.subTest(label):
                before = self.write(path, {**good, "schema_version": value})
                with self.assertRaises(hs.SupervisorError):
                    hpair.load_registry(self.paths)
                with self.assertRaises(hs.SupervisorError):
                    hpair.register_existing(self.paths, self.config, self.clock.time, name="Gamma", context=self.ctx(self.repo_b, self.review_b),
                                            identities=test_named_pairs_telegram.ids(test_named_pairs_telegram.GAMMA))
                self.assertEqual(path.read_bytes(), before, "a refused registry is never repaired or rewritten")
        self.write(path, good)
        self.assertEqual(hpair.load_registry(self.paths)["schema_version"], 1)

    def test_query_and_helper_registries_need_an_exact_integer_schema_too(self) -> None:
        for label, file, key in (("query", self.paths.query_registry_file, "providers"), ("helper", self.paths.state_dir / "helpers" / "registry.json", "workers")):
            file.parent.mkdir(parents=True, exist_ok=True)
            for value, ok in ((1, True), (True, False), (1.0, False)):
                with self.subTest(registry=label, schema=value):
                    self.write(file, {"schema_version": value, key: {}})
                    if ok:
                        hpair.foreign_sessions(self.paths)
                    else:
                        with self.assertRaises(hs.SupervisorError):
                            hpair.foreign_sessions(self.paths)
            file.unlink()


class ReleaseNotesTests(unittest.TestCase):
    """The tracked release notes describe the shipped source: its version, its exact quota pin, and what is still pending."""

    def test_notes_name_the_version_the_pin_and_the_pending_validation(self) -> None:
        notes = (ROOT / "docs/RELEASE_NOTES.md").read_text()
        version = (ROOT / "VERSION").read_text().strip()
        lock = q.load_lock(str(ROOT / "config/quota-dependency.json"))
        self.assertTrue(notes.startswith(f"# Herdr Supervisor {version} "))
        for needle in (lock["revision"], lock["plugin_id"], lock["tree_sha1"][:8], "**Pending**", "Python 3.11", "named session pairs", "helper-history guard"):
            self.assertIn(needle.lower(), notes.lower(), needle)
        self.assertNotIn("TODO", notes)
        self.assertNotIn("placeholder", notes.lower())
        self.assertIn("RELEASE_NOTES.md", (ROOT / "docs/RELEASE.md").read_text())
