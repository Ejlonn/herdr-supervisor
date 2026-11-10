"""Release-integration regressions that belong to no single feature suite.

The worker readiness of the helper context agrees with the managed-generation installer (launcher and library layout). Empty, whitespace-only or boolean-like systemctl answers are never read as a unit being active or enabled, and a status report built
from them never claims readiness or reboot persistence. The tests drive the real adapter and the real readiness report through the
same scripted host the setup suites use, so nothing touches the machine.
"""

from __future__ import annotations

import hashlib
import subprocess
import threading
import time
import unittest
from pathlib import Path

import herdr_helper_context as hctx
import herdr_installation as hi
import test_helper_context
import test_named_pairs_telegram
import test_setup_review_fixes as earlier
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
    """The worker readiness of the helper context and the managed-generation installer must agree: the shipped launcher resolves the
    program generation behind the `active` pointer, and an installation made before generations (flat library, earlier launcher)
    stays supported until its next reviewed update. Anything else is still refused."""

    def setUp(self) -> None:
        self.case = test_helper_context.ReadinessTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.home = self.case.project.home
        self.library = self.home / ".local/lib/herdr-supervisor"

    def adopt_generation(self, name: str = "generations/0.4.0-beta.1-0123456789abcdef") -> Path:
        generation = self.library / name
        generation.mkdir(parents=True)
        for module in ("herdr_codex_reset.py", "herdr_helper_context.py"):
            (self.library / module).rename(generation / module)
        (self.library / "active").symlink_to(name)
        return generation

    def problems(self) -> list[str]:
        return hctx.inspect_worker_runtime(self.home)

    def test_the_shipped_launcher_and_a_generation_library_are_supported(self) -> None:
        self.assertEqual(self.problems(), [])
        self.adopt_generation()
        self.assertEqual(hctx.installed_library(self.home), self.library / "generations/0.4.0-beta.1-0123456789abcdef")
        self.assertEqual(self.problems(), [])
        report = self.case.report()
        self.assertTrue(report["ready"], report["reasons"])

    def test_the_earlier_flat_launcher_stays_supported_with_a_flat_library(self) -> None:
        legacy = subprocess.run(["git", "show", "2279f373771346d27f9b68d25619c75224f35f88:bin/herdr-codex-reset-worker"], cwd=ROOT, capture_output=True, check=False)
        if legacy.returncode != 0:
            legacy_text = b'#!/bin/sh\nset -eu\nexec python3 "$HOME/.local/lib/herdr-supervisor/herdr_codex_reset.py" "$@"\n'
        else:
            legacy_text = legacy.stdout
        self.assertEqual(hashlib.sha256(legacy_text).hexdigest(), hctx.LEGACY_WORKER_LAUNCHER_SHA256)
        self.case.project.launcher.write_bytes(legacy_text)
        self.assertEqual(self.problems(), [])

    def test_unsupported_launchers_and_pointers_are_refused(self) -> None:
        self.case.project.launcher.write_text("#!/bin/sh\nexit 0\n")
        self.assertTrue(any("supported worker launcher" in item for item in self.problems()))
        self.setUp()
        # A pointer that is not exactly `generations/<name>` is never followed: the flat library (now empty of the worker) decides.
        generation = self.adopt_generation()
        (self.library / "active").unlink()
        (self.library / "active").symlink_to("../elsewhere")
        self.assertEqual(hctx.installed_library(self.home), self.library)
        self.assertTrue(any("does not support context-bound requests" in item for item in self.problems()))
        (self.library / "active").unlink()
        (self.library / "active").symlink_to("generations/a/b")
        self.assertEqual(hctx.installed_library(self.home), self.library)
        (self.library / "active").unlink()
        (self.library / "active").symlink_to(generation.name)
        self.assertEqual(hctx.installed_library(self.home), self.library)


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
