"""Human-handled startup update/notice waits (offline). A freshly started provider that is not ready at startup opens a DURABLE, bounded wait bound to the
preparation, provider, exact pane, launch journal and observed process; the human acts in that pane; an authenticated continuation reconciles the SAME process
and the unchanged contracts before the journaled machinery resumes. Isolated fakes only: no live Telegram, network, process, pane or model. The retained
fragments are native (tests/fixtures/startup/PROVENANCE.md); the Claude fixture is explicitly SYNTHETIC. Assertions are on invocation counts, phases and refusal
reasons, not only final status."""

from __future__ import annotations

import ast
import json
import uuid
from pathlib import Path

import herdr_core
import herdr_startup as hst
from test_explicit_launch import ExplicitCase
from test_fresh_session_issue5 import CODEX_ONLY, FRESH_ALL, Issue5Case
from v2_fixtures import V2Case, hs

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "startup"
FRESH_CLAUDE = {"policy": "fresh-claude", "profiles": {"codex": "default", "claude": "default"}}
EXE = {"path": "/fake/bin/codex", "size": 1234, "sha256": "a" * 64, "version": "codex-cli 0.163.0"}
CLAUDE_EXE = {"path": "/fake/bin/claude", "size": 4321, "sha256": "b" * 64, "version": "2.1.308 (Claude Code)"}


def banner() -> list[str]:
    return json.loads((FIXTURES / "codex_update_banner_attempt2_visible.json").read_text())["visible_lines"]


def menu_line() -> str:
    return (FIXTURES / "codex_update_menu_attempt2_topic.txt").read_text().strip()


def update_menu_screen() -> str:
    """Retained option-1 text inside clearly SYNTHETIC filler lines (the frame around the option was never retained)."""
    return "\n".join(["(synthetic filler) Codex needs your choice before it starts", "", "› " + menu_line(), "  2. (synthetic) the Skip option text was never retained"])


class PureTests(V2Case):
    """Classification, evidence, budget and record validation: no supervisor, no pane."""

    def test_retained_codex_fragments_are_recognized_and_nothing_else_is(self) -> None:
        menu = hst.classify_screen("codex", update_menu_screen().splitlines())
        self.assertEqual((menu["kind"], menu["recognized"], menu["pattern_id"]), ("codex_update_menu", True, "codex-update-option-1/attempt2-topic"))
        note = hst.classify_screen("codex", banner())
        self.assertEqual((note["kind"], note["recognized"], note["versions"]), ("codex_update_banner", True, {"from": "0.163.0", "to": "0.164.0"}))
        both = hst.classify_screen("codex", [*banner(), "", "› " + menu_line()])
        self.assertEqual((both["kind"], both["versions"]), ("codex_update_menu", {"from": "0.163.0", "to": "0.164.0"}))
        trust = hst.classify_screen("codex", ["Do you trust this folder?", "› 1. Trust and continue", "  2. (synthetic) Do not trust"])
        self.assertEqual((trust["kind"], trust["recognized"]), ("trust_dialog", True), "a recognized trust dialog is a separate decision")
        for lines in ([], ["codex@host:~$ "], ["Update available soon", "update now"], ["2. Update now"], ["x 1. Update nowhere"]):
            with self.subTest(lines=lines):
                result = hst.classify_screen("codex", lines)
                self.assertEqual((result["kind"], result["recognized"]), ("unknown_screen", False))

    def test_claude_never_matches_codex_patterns_and_an_unavailable_screen_is_never_recognized(self) -> None:
        synthetic = json.loads((FIXTURES / "claude_synthetic_unknown.json").read_text())
        self.assertTrue(synthetic["synthetic"])
        for lines in (synthetic["visible_lines"], banner(), update_menu_screen().splitlines(), ["› 1. Trust and continue"]):
            with self.subTest(first=lines[0]):
                result = hst.classify_screen("claude", lines)
                self.assertEqual((result["kind"], result["recognized"], result["pattern_id"]), ("unknown_screen", False, None))
        for provider in ("codex", "claude"):
            self.assertEqual(hst.classify_screen(provider, None), {"kind": "screen_unavailable", "recognized": False, "pattern_id": None, "versions": None})
        with self.assertRaisesRegex(hs.SupervisorError, "unknown provider"):
            hst.classify_screen("grok", [])

    def test_evidence_is_redacted_bounded_hashed_and_failures_are_recorded_as_unavailable(self) -> None:
        secret = "token=sk-" + "a1b2c3d4e5f6g7h8i9"
        raw = "\n".join([secret, *[f"line {n} " + "x" * 400 for n in range(80)]])
        evidence = hst.capture_screen(lambda pane: raw, "w9:p2", now=10.0)
        self.assertTrue(evidence["available"])
        self.assertLessEqual(len(evidence["lines"]), hst.EVIDENCE_MAX_LINES)
        self.assertTrue(all(len(line) <= hst.EVIDENCE_LINE_WIDTH for line in evidence["lines"]))
        self.assertNotIn("sk-a1b2", json.dumps(evidence))
        self.assertEqual((evidence["line_count"], len(evidence["sha256"])), (81, 64))
        self.assertIn("[redacted]", "\n".join(hst.redacted_lines([secret])))

        def boom(pane: str) -> str:
            raise hs.HerdrError("pane read failed", code="timeout")

        for read, reason in ((boom, "timeout"), (None, "unavailable"), (lambda pane: None, "no text")):
            with self.subTest(reason=reason):
                result = hst.capture_screen(read, "w9:p2", now=1.0)
                self.assertFalse(result["available"])
                self.assertEqual((result["lines"], result["sha256"], reason in result["reason"]), ([], None, True))
                self.assertEqual(hst.evidence_classification("codex", result)["kind"], "screen_unavailable")
        self.assertFalse(hst.capture_screen(lambda pane: "x", "not-a-pane", now=1.0)["available"])

    def test_budget_is_finite_never_extended_and_capped_by_the_callers_ceiling(self) -> None:
        settings = herdr_core.startup_wait_settings({"session_start": {}})
        self.assertEqual({k: settings[k] for k in ("max_seconds", "max_waits_per_provider", "max_continue_attempts")}, {"max_seconds": 900, "max_waits_per_provider": 2, "max_continue_attempts": 5})
        deadline, total, remaining = hst.wait_budget(settings, 1000.0, [])
        self.assertEqual((deadline, total, remaining), (1900.0, 900, 900))
        first = {"opened_at_unix": 1000.0, "deadline_unix": 1900.0, "closed_at_unix": 1300.0}
        deadline, _, remaining = hst.wait_budget(settings, 1400.0, [first])
        self.assertEqual((deadline, remaining), (2000.0, 600), "300 s were used by the first wait; the second gets only the rest")
        capped = {**settings, "not_after_unix": 1500.0}
        self.assertEqual(hst.wait_budget(capped, 1000.0, [])[0], 1500.0, "a diagnostic's total deadline is never extended by waiting")
        with self.assertRaisesRegex(hs.SupervisorError, "exhausted"):
            hst.wait_budget(capped, 1499.5, [])
        with self.assertRaisesRegex(hs.SupervisorError, "count"):
            hst.wait_budget(settings, 1400.0, [first, first])
        overdue = {"opened_at_unix": 0.0, "deadline_unix": 900.0, "closed_at_unix": None}
        with self.assertRaisesRegex(hs.SupervisorError, "exhausted"):
            hst.wait_budget({**settings, "max_waits_per_provider": 5}, 5000.0, [overdue])
        for bad in ({"max_seconds": True}, {"max_seconds": 5}, {"max_waits_per_provider": 0}, {"not_after_unix": -1}, {"not_after_unix": True}, {"extra": 1}, "x"):
            with self.subTest(bad=bad), self.assertRaises(hs.SupervisorError):
                herdr_core.startup_wait_settings({"session_start": {"startup_wait": bad}})

    def test_wait_records_reject_trust_dialogs_claude_recognition_and_malformed_bindings(self) -> None:
        good = hst.new_wait_record(context="preparation", provider="codex", pane_id="w9:p1", phase="AGENT_STARTING", site="agent_start_not_ready", cause_code="agent_not_ready", now=100.0,
                                   settings=herdr_core.startup_wait_settings({"session_start": {}}), prior=[], evidence=hst.capture_screen(lambda p: update_menu_screen(), "w9:p1", now=100.0),
                                   classification=hst.classify_screen("codex", update_menu_screen().splitlines()), executable=dict(EXE),
                                   process={"pid": 1, "shell_pid": 2, "argv": ["codex"], "cwd": "/x"}, preparation_id=str(uuid.uuid4()))
        hst.validate_wait_record(json.loads(json.dumps(good)))
        mutations = {
            "trust": lambda r: r["classification"].update(kind="trust_dialog"),
            "claude recognized": lambda r: r.update(provider="claude"),
            "both ids": lambda r: r.update(run_id=str(uuid.uuid4())),
            "no binding": lambda r: r.update(preparation_id=None),
            "bad phase": lambda r: r.update(phase="LAUNCH_SUBMITTED"),
            "run phase on preparation": lambda r: r.update(phase="RUN_RESTORE"),
            "bad status": lambda r: r.update(status="DONE"),
            "bool attempts": lambda r: r.update(continue_attempts=True),
            "deadline before open": lambda r: r.update(deadline_unix=r["opened_at_unix"]),
            "bad pane": lambda r: r.update(pane_id="pane-7"),
            "bad sha": lambda r: r["executable"].update(sha256="xyz"),
            "recognition mismatch": lambda r: r["classification"].update(recognized=False),
            "too many lines": lambda r: r["evidence"].update(lines=["x"] * 31),
            "bad wait id": lambda r: r.update(wait_id="nope"),
        }
        for label, mutate in mutations.items():
            with self.subTest(label):
                record = json.loads(json.dumps(good))
                mutate(record)
                with self.assertRaises(hs.SupervisorError):
                    hst.validate_wait_record(record)

    def test_process_and_executable_identity_comparisons_fail_closed(self) -> None:
        proc = {"pid": 7, "shell_pid": 3, "argv": ["codex", "resume", "t"], "cwd": "/w", "start_ticks": 55}
        self.assertIsNone(hst.process_problem(proc, dict(proc)))
        for key, value in (("pid", 8), ("shell_pid", 4), ("argv", ["codex"]), ("cwd", "/other"), ("start_ticks", 56)):
            with self.subTest(key):
                self.assertIn(key, hst.process_problem(proc, {**proc, key: value}))
        self.assertIn("could not be established", hst.process_problem(None, proc))
        self.assertIsNone(hst.executable_problem(EXE, dict(EXE)))
        self.assertIsNone(hst.executable_problem(EXE, {**EXE, "version": None}), "same bytes; a version that cannot be read this time does not prove a change")
        for key, value in (("sha256", "c" * 64), ("path", "/other/codex"), ("size", 1), ("version", "codex-cli 0.164.0")):
            with self.subTest(key):
                self.assertIn(key, hst.executable_problem(EXE, {**EXE, key: value}))
        for key in ("sha256", "path", "size"):
            with self.subTest(unknown=key):
                self.assertIn("not proven unchanged", hst.executable_problem({**EXE, key: None}, EXE))
        self.assertEqual(hst.process_start_ticks(1, proc_root="/definitely/not/proc"), None)

    def test_pane_read_is_one_read_only_visible_command_with_validated_input(self) -> None:
        import herdr_cli

        calls: list[list[str]] = []

        def fake_run(command, **kwargs):
            calls.append(list(command))

            class Done:
                returncode, stdout, stderr = 0, "screen text\n", ""

            return Done()

        adapter = herdr_cli.HerdrCli("/fake/herdr")
        original = herdr_cli.subprocess.run
        herdr_cli.subprocess.run = fake_run  # type: ignore[assignment]
        try:
            self.assertEqual(adapter.pane_read("w9:p1"), "screen text\n")
            self.assertEqual(calls, [["/fake/herdr", "pane", "read", "w9:p1", "--source", "visible", "--lines", "60"]])
            for pane, lines in (("pane-1", 60), ("w9:p1", 0), ("w9:p1", 201), ("w9:p1", True), ("w9:p1; reboot", 60)):
                with self.subTest(pane=pane, lines=lines), self.assertRaises(hs.HerdrError):
                    adapter.pane_read(pane, lines=lines)
            self.assertEqual(len(calls), 1, "invalid input never reaches the CLI")
        finally:
            herdr_cli.subprocess.run = original  # type: ignore[assignment]

    def test_the_optional_startup_wait_config_block_is_validated_with_the_rest_of_session_start(self) -> None:
        config = json.loads(json.dumps(self.config))
        herdr_core.validate_session_start_config(config)
        config["session_start"]["startup_wait"] = {"max_seconds": 120, "max_waits_per_provider": 3, "max_continue_attempts": 2, "not_after_unix": 1_000_900}
        herdr_core.validate_session_start_config(config)
        self.assertEqual(herdr_core.startup_wait_settings(config)["not_after_unix"], 1_000_900.0)
        for bad in ({"max_seconds": "900"}, {"max_continue_attempts": 21}, {"unknown": 1}, {"not_after_unix": 0}):
            broken = json.loads(json.dumps(config))
            broken["session_start"]["startup_wait"] = bad
            with self.subTest(bad), self.assertRaises(hs.SupervisorError):
                herdr_core.validate_session_start_config(broken)
        self.assertNotIn("startup_wait", hs.DEFAULT_CONFIG["session_start"], "absent = the finite defaults; the default configuration is unchanged")

    def test_the_new_module_sits_below_the_workflow_layer_and_depends_only_on_core_and_redaction(self) -> None:
        source = (Path(herdr_core.__file__).parent / "herdr_startup.py").read_text()
        imported = {n.module for n in ast.walk(ast.parse(source)) if isinstance(n, ast.ImportFrom) and n.module and n.module.startswith("herdr_")}
        self.assertEqual(imported, {"herdr_core", "herdr_redaction"})
        for consumer in ("herdr_sessions", "herdr_workflow", "herdr_runtime"):
            text = (Path(herdr_core.__file__).parent / f"{consumer}.py").read_text()
            self.assertIn("import herdr_startup as hst", text)
        workflow_imports = {n.module for n in ast.walk(ast.parse((Path(herdr_core.__file__).parent / "herdr_workflow.py").read_text())) if isinstance(n, ast.ImportFrom) and n.module}
        self.assertNotIn("herdr_sessions", workflow_imports, "the workflow layer must not import the sessions layer")


class WaitCase(Issue5Case):
    """Preparation-context waits through the agent-start path (the installed default)."""

    def runTest(self) -> None:  # lets helpers build an independent instance of a case class (own temporary roots) without naming a test
        pass

    def setUp(self) -> None:
        super().setUp()
        self.exe = dict(EXE)
        self.ticks = {}
        self.sup = self.make_supervisor()

    def make_supervisor(self) -> "hs.Supervisor":
        sup = super().make_supervisor()
        sup.startup_executable_record = lambda command: dict(CLAUDE_EXE if "claude" in str(command) else self.exe) if hasattr(self, "exe") else dict(EXE)
        sup.startup_process_ticks = lambda pid: self.ticks.get(pid) if hasattr(self, "ticks") else None
        sup.startup_codex_contract = lambda: dict(sup._codex_thread_contract)  # the uncached post-handling probe: a fixture value, never the host CLI
        sup.startup_codex_direct_contract = lambda: dict(sup._codex_direct_contract)  # the same for the DIRECT-launch facts (top-level help)
        return sup

    # ----- helpers
    def waits(self, **filters: object) -> list[dict]:
        return hst.list_waits(self.paths, **filters)

    def only_wait(self) -> dict:
        found = self.waits()
        self.assertEqual(len(found), 1, found)
        return found[0]

    def block_codex(self, screen: str | None = None) -> None:
        self.herdr.startup_block = "blocked_agent"

    def pane_of(self, provider: str) -> str:
        return next(item["pane_id"] for p, item in self.journal(self.prep)["providers"].items() if p == provider)

    def startup_events(self, kind: str | None = None) -> list[dict]:
        found = [e for e in self.sup.list_events() if e["type"] in ("STARTUP_WAIT_REQUIRED", "STARTUP_WAIT_RESULT")]
        return [e for e in found if kind is None or e["type"] == kind]

    def counts(self) -> dict:
        return {"starts": len(self.herdr.starts), "splits": len(self.herdr.splits), "prompts": len(self.herdr.prompts), "bootstrap": len(self.herdr.bootstrap_prompts),
                "keys": len(self.herdr.sent_keys), "threads": len(self.thread_journal.requests), "runs": len(self.herdr.pane_runs)}

    def open_codex_wait(self, *, screen: str | None = None, selection: dict | None = None) -> dict:
        """Fresh Codex whose directly launched process runs but is not ready (the agent is detected as blocked): the retained menu option is on screen. Returns the wait record."""
        self.prep = str(uuid.uuid4())
        self.herdr.startup_block = "blocked_agent"
        self.herdr.screens["w9:p1"] = screen if screen is not None else update_menu_screen()
        with self.assertRaises(herdr_core.StartupWaitRequired) as caught:
            self.prepare(selection or CODEX_ONLY, self.prep)
        self.assertEqual(caught.exception.provider, "codex")
        wait = self.only_wait()
        self.assertEqual(caught.exception.wait_id, wait["wait_id"])
        return wait

    def continue_(self, wait_id: str, *, actor: str = "telegram:1") -> dict:
        with self.sup.store.transaction():
            return self.sup.continue_startup(wait_id, actor=actor)

    def cancel_(self, wait_id: str, *, actor: str = "telegram:1") -> dict:
        with self.sup.store.transaction():
            return self.sup.cancel_startup(wait_id, actor=actor)


def _fresh_case(parent: "WaitCase") -> "WaitCase":
    """An independent case instance (own temporary roots) for a subTest loop; cleaned up with the parent test."""
    case = type(parent)(parent._testMethodName)
    case.setUp()
    parent.addCleanup(case.tearDown)
    return case


class PreparationWaitTests(WaitCase):
    def test_a_start_that_is_not_ready_opens_one_durable_wait_bound_to_the_exact_target_and_sends_nothing(self) -> None:
        owners_before = self.paths.owners_file.read_text()
        wait = self.open_codex_wait()
        item = self.journal(self.prep)["providers"]["codex"]
        self.assertEqual((wait["context"], wait["provider"], wait["pane_id"], wait["phase"], wait["site"], wait["cause_code"], wait["status"]),
                         ("preparation", "codex", "w9:p1", "AGENT_STARTING", "agent_not_ready", None, "WAITING"))
        self.assertEqual((wait["preparation_id"], wait["agent_name"], wait["binding"]["thread_id"], wait["binding"]["item_status"]), (self.prep, f"codex-run-{self.prep[:8]}", None, "AGENT_STARTING"))
        self.assertEqual(item["status"], "AGENT_STARTING", "the journal phase is exactly where the signal found it")
        self.assertEqual((wait["classification"]["kind"], wait["classification"]["recognized"]), ("codex_update_menu", True))
        self.assertEqual((wait["executable"], wait["process"]["argv"][1:3], wait["process"]["cwd"]), (EXE, ["--no-daemon"], str(self.project_root)))
        self.assertTrue(wait["evidence"]["available"])
        self.assertIn(menu_line()[:40], "\n".join(wait["evidence"]["lines"]))
        self.assertGreater(wait["deadline_unix"], wait["opened_at_unix"])
        self.assertEqual(wait["budget_remaining_at_open_seconds"], 900)
        self.assertEqual(self.counts(), {"starts": 0, "splits": 1, "prompts": 0, "bootstrap": 0, "keys": 0, "threads": 0, "runs": 1}, "one direct launch, no thread, never a key, a prompt or a bootstrap")
        self.assertEqual(self.paths.owners_file.read_text(), owners_before, "ownership untouched")
        self.assertEqual(self.herdr.renames, [])
        events = self.startup_events("STARTUP_WAIT_REQUIRED")
        self.assertEqual(len(events), 1)
        data = events[0]["data"]
        self.assertEqual((data["wait"]["wait_id"], data["wait"]["pane_id"], data["wait"]["kind"], data["wait"]["recognized"]), (wait["wait_id"], "w9:p1", "codex_update_menu", True))
        self.assertTrue(any("Handle it yourself in THAT pane" in line for line in data["instructions"]))
        self.assertTrue(any("never presses a key" in line for line in data["instructions"]))
        self.assertEqual(events[0]["run_id"], self.prep)
        self.assertFalse((self.paths.state_dir / "state.json").exists(), "no run state was created by a preparation wait")
        mode = oct((self.paths.startup_waits_dir / f"{wait['wait_id']}.json").stat().st_mode & 0o777)
        self.assertEqual(mode, "0o600")

    def test_a_retry_of_the_same_task_start_cannot_bypass_the_wait_and_causes_no_launch_prompt_or_second_event(self) -> None:
        wait = self.open_codex_wait()
        before, events = self.counts(), len(self.startup_events())
        for _ in range(3):
            with self.assertRaises(herdr_core.StartupWaitRequired) as caught:
                self.prepare(CODEX_ONLY, self.prep)
            self.assertEqual(caught.exception.wait_id, wait["wait_id"])
            self.assertFalse(caught.exception.expired)
            self.assertIn("still waiting", str(caught.exception))
        self.assertEqual(self.counts(), before)
        self.assertEqual(len(self.startup_events()), events, "no repeated notification")
        self.assertEqual(self.only_wait()["status"], "WAITING")

    def test_skip_then_continue_reconciles_the_same_process_adopts_the_identity_and_never_relaunches(self) -> None:
        wait = self.open_codex_wait()
        before = self.counts()
        self.herdr.unblock("w9:p1")  # the human chose Skip in the diagnostic pane
        result = self.continue_(wait["wait_id"])
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["kind"], "resolved")
        self.assertIn("No original start request is stored", result["message"], "a direct preparation has no stored original request: the honest next step is named")
        self.assertIn("rebind-sessions", result["message"])
        self.assertEqual(self.counts(), before, "zero additional starts, splits, prompts, bootstraps or keys")
        record = self.journal(self.prep)
        self.assertEqual(record["status"], "OWNERSHIP_BOUND")
        self.assertEqual(record["providers"]["codex"]["initialization"], "native-reported")
        self.assertEqual(self.sup.owners()["codex"]["pane_id"], "w9:p1")
        final = hst.read_wait(self.paths, wait["wait_id"])
        self.assertEqual((final["status"], final["continue_attempts"]), ("RESOLVED", 1))
        self.assertEqual([h["action"] for h in final["history"]], ["opened", "continuing", "resolved"])
        results = self.startup_events("STARTUP_WAIT_RESULT")
        self.assertEqual((len(results), results[0]["data"]["ok"], results[0]["data"]["outcome"]), (1, True, "resolved"))
        again = self.prepare(CODEX_ONLY, self.prep)  # the human's "send the same task again" adopts the bound preparation
        self.assertEqual(again["status"], "OWNERSHIP_BOUND")
        self.assertEqual(self.counts(), before)

    def test_duplicate_stale_and_late_continuations_do_nothing_a_second_time(self) -> None:
        wait = self.open_codex_wait()
        self.herdr.unblock("w9:p1")
        self.assertTrue(self.continue_(wait["wait_id"])["ok"])
        settled = self.counts(), self.journal(self.prep), len(self.startup_events())
        for label, wait_id in (("duplicate", wait["wait_id"]), ("unknown", str(uuid.uuid4()))):
            with self.subTest(label):
                result = self.continue_(wait_id)
                self.assertFalse(result["ok"])
                self.assertIn(result["kind"], ("not_waiting", "unknown_wait"))
        result = self.continue_("not-a-uuid")
        self.assertEqual((result["ok"], result["kind"]), (False, "unknown_wait"))
        self.assertEqual((self.counts(), self.journal(self.prep), len(self.startup_events())), settled)
        self.assertEqual(hst.read_wait(self.paths, wait["wait_id"])["status"], "RESOLVED")

    def test_a_concurrent_second_continuation_during_the_machinery_sees_the_durable_continuing_marker(self) -> None:
        wait = self.open_codex_wait()
        self.herdr.unblock("w9:p1")
        seen: list[dict] = []
        original = self.sup.prepare_task_sessions

        def probe(*args, **kwargs):
            seen.append(self.sup.continue_startup(wait["wait_id"], actor="telegram:1"))  # a duplicate callback arriving while the first one is mid-flight (already inside the transaction)
            return original(*args, **kwargs)

        self.sup.prepare_task_sessions = probe
        self.assertTrue(self.continue_(wait["wait_id"])["ok"])
        self.assertEqual(len(seen), 1)
        self.assertEqual((seen[0]["ok"], seen[0]["kind"]), (False, "not_waiting"))
        self.assertIn("already continued", seen[0]["message"])
        self.assertEqual(self.counts()["runs"], 1)

    def test_continue_while_the_blocker_is_still_present_changes_nothing_and_is_bounded(self) -> None:
        wait = self.open_codex_wait()
        before = self.counts()
        settings = herdr_core.startup_wait_settings(self.config)
        for attempt in range(1, settings["max_continue_attempts"] + 1):
            result = self.continue_(wait["wait_id"])
            self.assertEqual((result["ok"], result["kind"]), (False, "blocker_present"), result)
            self.assertIn("blocked", result["message"])
            self.assertEqual(hst.read_wait(self.paths, wait["wait_id"])["continue_attempts"], attempt)
        self.assertEqual(self.counts(), before)
        self.assertEqual(self.journal(self.prep)["providers"]["codex"]["status"], "AGENT_STARTING")
        exhausted = self.continue_(wait["wait_id"])
        self.assertEqual((exhausted["ok"], exhausted["kind"]), (False, "cancelled"))
        self.assertEqual(hst.read_wait(self.paths, wait["wait_id"])["status"], "CANCELLED")
        self.assertEqual(self.journal(self.prep)["status"], "ABANDONED", "the exhausted wait leaves an inert preparation, never a live half-start")
        self.assertEqual(self.counts(), before)

    def test_the_prompt_still_on_screen_refuses_even_if_the_agent_already_reports_ready(self) -> None:
        wait = self.open_codex_wait()
        for agent in self.herdr.agents.values():
            if agent.get("pane_id") == "w9:p1":
                agent["agent_status"] = "idle"  # status says ready, but the retained option text is still displayed
        result = self.continue_(wait["wait_id"])
        self.assertEqual((result["ok"], result["kind"]), (False, "blocker_present"))
        self.assertIn("still on screen", result["message"])
        self.herdr.unblock("w9:p1")
        self.assertTrue(self.continue_(wait["wait_id"])["ok"])

    def test_pre_identity_wait_keeps_the_single_allowed_bootstrap_and_does_not_pretend_identity_exists(self) -> None:
        self.herdr.hold_identity = True  # this provider reports its native identity only after a first turn
        self.config["session_start"]["native_identity_bootstrap"] = True
        self.sup = self.make_supervisor()
        wait = self.open_codex_wait()
        item = self.journal(self.prep)["providers"]["codex"]
        self.assertEqual((item["status"], "session_id" in item, self.counts()["bootstrap"]), ("AGENT_STARTING", False, 0))
        self.herdr.unblock("w9:p1")
        result = self.continue_(wait["wait_id"])
        self.assertTrue(result["ok"], result)
        self.assertEqual(len(self.herdr.bootstrap_prompts), 1, "exactly the one identity-only bootstrap")
        done = self.journal(self.prep)["providers"]["codex"]
        self.assertEqual((done["status"], done["initialization"]), ("IDENTITY_VERIFIED", "bootstrap-completed"))
        self.assertEqual(self.counts()["runs"], 1)
        self.assertEqual(self.counts()["keys"], 0)

    def test_a_bootstrap_target_that_is_blocked_before_input_opens_the_wait_at_bootstrap_intent_and_resumes_the_same_intent(self) -> None:
        self.herdr.hold_identity = True
        self.config["session_start"]["native_identity_bootstrap"] = True
        self.sup = self.make_supervisor()
        self.prep = str(uuid.uuid4())
        armed = {"done": False}
        original_start, original_list = self.herdr.pane_run, self.herdr.list_agents

        def started(*args, **kwargs):
            return original_start(*args, **kwargs)

        def listing():
            agents = original_list()
            path = self.paths.session_preparations_dir / f"{self.prep}.json"
            if not armed["done"] and path.exists() and json.loads(path.read_text())["providers"]["codex"]["status"] == "BOOTSTRAP_INTENT":
                armed["done"] = True  # the readiness read inside the bootstrap intent: the agent now shows a startup screen
                for agent in self.herdr.agents.values():
                    if agent.get("pane_id") == "w9:p1":
                        agent["agent_status"] = "blocked"
                self.herdr.screens["w9:p1"] = update_menu_screen()
            return agents

        self.herdr.pane_run, self.herdr.list_agents = started, listing
        with self.assertRaises(herdr_core.StartupWaitRequired):
            self.prepare(CODEX_ONLY, self.prep)
        wait = self.only_wait()
        self.assertEqual((wait["phase"], wait["site"], wait["status"]), ("BOOTSTRAP_INTENT", "bootstrap_target_not_ready", "WAITING"))
        item = self.journal(self.prep)["providers"]["codex"]
        nonce = item["bootstrap"]["nonce"]
        self.assertEqual((item["status"], wait["binding"]["bootstrap_nonce"], len(self.herdr.bootstrap_prompts)), ("BOOTSTRAP_INTENT", nonce, 0), "nothing was sent")
        self.herdr.pane_run, self.herdr.list_agents = original_start, original_list
        self.herdr.unblock("w9:p1")
        self.assertTrue(self.continue_(wait["wait_id"])["ok"])
        done = self.journal(self.prep)["providers"]["codex"]
        self.assertEqual(done["bootstrap"]["nonce"], nonce, "the SAME journaled intent was used; no second nonce")
        self.assertEqual((done["status"], len(self.herdr.bootstrap_prompts)), ("IDENTITY_VERIFIED", 1), "exactly one bootstrap")

    def test_update_that_replaces_the_process_is_refused_and_never_adopted(self) -> None:
        wait = self.open_codex_wait()
        owners_before = self.paths.owners_file.read_text()
        before = self.counts()
        # "Update now": the old process exits and a NEW one (other pid, other version) runs in the same pane
        old = self.herdr.processes["w9:p1"]
        new_pid = old["foreground_processes"][0]["pid"] + 100
        self.herdr.processes["w9:p1"] = {**old, "foreground_processes": [{**old["foreground_processes"][0], "pid": new_pid}]}
        self.herdr.unblock("w9:p1")
        result = self.continue_(wait["wait_id"])
        self.assertEqual((result["ok"], result["kind"]), (False, "changed_process"), result)
        self.assertIn("replacement", result["message"])
        self.assertIn("explicit successor/restart authority", result["message"])
        self.assertIn("Deployed contracts after the change: verified", result["message"], "Update with the same supported contract is still a HOLD: the contract result is only recorded")
        self.assertEqual(hst.read_wait(self.paths, wait["wait_id"])["contracts_after_change"], "verified")
        self.assertEqual(self.counts(), before)
        self.assertEqual(self.paths.owners_file.read_text(), owners_before)
        self.assertEqual(self.journal(self.prep)["providers"]["codex"]["status"], "AGENT_STARTING")
        self.assertEqual(hst.read_wait(self.paths, wait["wait_id"])["status"], "WAITING", "still cancellable; never silently adopted")
        self.assertEqual(self.cancel_(wait["wait_id"])["kind"], "cancelled")

    def test_an_update_that_also_breaks_a_contract_records_that_the_new_cli_is_not_verified(self) -> None:
        wait = self.open_codex_wait()
        old = self.herdr.processes["w9:p1"]
        self.herdr.processes["w9:p1"] = {**old, "foreground_processes": [{**old["foreground_processes"][0], "pid": old["foreground_processes"][0]["pid"] + 100}]}
        self.herdr.unblock("w9:p1")
        self.sup._codex_direct_contract["codex_pane_local_server"] = False
        result = self.continue_(wait["wait_id"])
        self.assertEqual((result["ok"], result["kind"]), (False, "changed_process"))
        self.assertIn("NOT verified", result["message"])
        self.assertIn("codex_pane_local_server", hst.read_wait(self.paths, wait["wait_id"])["contracts_after_change"])

    def test_the_executable_is_recorded_before_and_after_even_when_the_continuation_is_refused(self) -> None:
        wait = self.open_codex_wait()
        self.assertEqual(wait["executable"]["version"], "codex-cli 0.163.0")
        self.herdr.unblock("w9:p1")
        self.exe = {**EXE, "version": "codex-cli 0.164.0", "sha256": "c" * 64}
        result = self.continue_(wait["wait_id"])
        self.assertEqual(result["kind"], "changed_executable")
        record = hst.read_wait(self.paths, wait["wait_id"])
        self.assertEqual((record["executable"]["version"], record["executable_after"]["version"], record["executable_after"]["sha256"]), ("codex-cli 0.163.0", "codex-cli 0.164.0", "c" * 64))
        self.exe = dict(EXE)
        self.assertTrue(self.continue_(wait["wait_id"])["ok"])
        self.assertEqual(hst.read_wait(self.paths, wait["wait_id"])["executable_after"]["version"], "codex-cli 0.163.0")

    def test_a_waiting_wait_whose_journal_was_made_inert_elsewhere_is_closed_by_the_sweep(self) -> None:
        wait = self.open_codex_wait()
        path = self.paths.session_preparations_dir / f"{self.prep}.json"
        journal = json.loads(path.read_text())
        journal["status"] = "ABANDONED"  # e.g. an explicit rebind marks every unresolved preparation inert
        path.write_text(json.dumps(journal))
        self.assertEqual(self.sup.sweep_startup_waits(), [wait["wait_id"]])
        self.assertEqual(hst.read_wait(self.paths, wait["wait_id"])["status"], "CANCELLED")

    def test_a_changed_executable_or_version_is_refused_even_when_the_process_looks_unchanged(self) -> None:
        wait = self.open_codex_wait()
        self.herdr.unblock("w9:p1")
        for label, change in (("bytes", {"sha256": "c" * 64}), ("version", {"version": "codex-cli 0.164.0"}), ("path", {"path": "/elsewhere/codex"})):
            with self.subTest(label):
                self.exe = {**EXE, **change}
                result = self.continue_(wait["wait_id"])
                self.assertEqual((result["ok"], result["kind"]), (False, "changed_executable"), result)
        self.exe = dict(EXE)
        self.assertTrue(self.continue_(wait["wait_id"])["ok"], "unchanged bytes and version continue (same-process Skip)")

    def test_a_changed_deployed_contract_is_refused_not_assumed_compatible(self) -> None:
        wait = self.open_codex_wait()
        self.herdr.unblock("w9:p1")
        original_contract = dict(self.sup._codex_direct_contract)
        self.sup._codex_direct_contract["codex_pane_local_server"] = False
        result = self.continue_(wait["wait_id"])
        self.assertEqual((result["ok"], result["kind"]), (False, "changed_contract"), result)
        self.assertIn("codex_pane_local_server", result["message"])
        self.sup._codex_direct_contract = original_contract
        self.direct_report = {"ready": False, "reasons": ["the explicit launch recipe is gone"]}
        result = self.continue_(wait["wait_id"])
        self.assertEqual((result["ok"], result["kind"]), (False, "changed_contract"))
        self.assertIn("codex_direct_launch", result["message"])
        self.direct_report = {"ready": True, "reasons": []}
        self.helper_codex_home = "/home/test/another-codex-home"
        result = self.continue_(wait["wait_id"])
        self.assertEqual((result["ok"], result["kind"]), (False, "changed_contract"))
        self.assertIn("context changed", result["message"])
        self.assertEqual(self.counts()["runs"], 1)
        self.helper_codex_home = "/home/test/.codex"
        self.assertTrue(self.continue_(wait["wait_id"])["ok"])

    def test_wrong_pane_provider_and_cross_preparation_bindings_are_refused(self) -> None:
        wait = self.open_codex_wait()
        self.herdr.unblock("w9:p1")
        # the live agent in the recorded pane is the wrong provider
        agent = next(a for a in self.herdr.agents.values() if a.get("pane_id") == "w9:p1")
        agent["agent"] = "claude"
        result = self.continue_(wait["wait_id"])
        self.assertEqual((result["ok"], result["kind"]), (False, "blocker_present"))
        agent["agent"] = "codex"
        # the journal moved on (pane changed): a stale wait cannot act on it
        path = self.paths.session_preparations_dir / f"{self.prep}.json"
        journal = json.loads(path.read_text())
        journal["providers"]["codex"]["pane_id"] = "w9:p7"
        path.write_text(json.dumps(journal))
        result = self.continue_(wait["wait_id"])
        self.assertEqual(result["ok"], False)
        self.assertIn(result["kind"], ("journal_moved",))
        journal["providers"]["codex"]["pane_id"] = "w9:p1"
        path.write_text(json.dumps(journal))
        self.assertEqual(self.counts()["runs"], 1)
        self.assertTrue(self.continue_(wait["wait_id"])["ok"])

    def test_cancel_makes_the_preparation_inert_preserves_every_pane_and_session_and_is_idempotent(self) -> None:
        wait = self.open_codex_wait()
        before = self.counts()
        result = self.cancel_(wait["wait_id"])
        self.assertEqual((result["ok"], result["kind"]), (True, "cancelled"))
        self.assertIn("no pane, session or process was closed", result["message"])
        self.assertEqual(self.journal(self.prep)["status"], "ABANDONED")
        self.assertIn("w9:p1", self.herdr.available_panes)
        self.assertIn(f"codex-run-{self.prep[:8]}", self.herdr.agents)
        self.assertEqual(self.counts(), before)
        again = self.cancel_(wait["wait_id"])
        self.assertEqual((again["ok"], again["kind"]), (False, "not_waiting"))
        late = self.continue_(wait["wait_id"])
        self.assertEqual((late["ok"], late["kind"]), (False, "not_waiting"))
        self.assertIn("CANCELLED", late["message"])
        with self.assertRaisesRegex(hs.SupervisorError, "abandoned"):
            self.prepare(CODEX_ONLY, self.prep)
        self.assertEqual(self.counts(), before)

    def test_abandon_session_start_closes_the_open_wait_too(self) -> None:
        wait = self.open_codex_wait()
        self.sup.abandon_session_preparation(self.prep)
        self.assertEqual(hst.read_wait(self.paths, wait["wait_id"])["status"], "CANCELLED")

    def test_expiry_is_durable_bounded_and_permits_no_continuation(self) -> None:
        wait = self.open_codex_wait()
        before = self.counts()
        reads = len(self.herdr.pane_reads)
        self.clock.current = wait["deadline_unix"] - 1
        self.assertEqual(self.sup.sweep_startup_waits(), [], "not yet expired")
        self.clock.current = wait["deadline_unix"] + 1
        self.herdr.unblock("w9:p1")  # even a perfectly handled pane cannot revive an expired wait
        # a restarted worker process observes the same deadline from the file alone
        self.sup = self.make_supervisor()
        self.assertEqual(self.sup.sweep_startup_waits(), [wait["wait_id"]])
        final = hst.read_wait(self.paths, wait["wait_id"])
        self.assertEqual(final["status"], "EXPIRED")
        self.assertLessEqual(final["closed_at_unix"], final["deadline_unix"], "expiry never extends the wait")
        self.assertEqual(self.journal(self.prep)["status"], "ABANDONED")
        late = self.continue_(wait["wait_id"])
        self.assertEqual((late["ok"], late["kind"]), (False, "not_waiting"))
        self.assertEqual(self.counts(), before, "no launch, prompt, bootstrap or key after expiry")
        self.assertEqual(len(self.herdr.pane_reads), reads, "no polling: expiry reads files only")
        events = self.startup_events("STARTUP_WAIT_RESULT")
        self.assertEqual((len(events), events[-1]["data"]["outcome"]), (1, "expired"))
        self.assertEqual(self.sup.sweep_startup_waits(), [], "idempotent")

    def test_an_expired_wait_found_by_a_retried_start_is_closed_and_reported_without_continuing(self) -> None:
        wait = self.open_codex_wait()
        self.clock.current = wait["deadline_unix"] + 5
        before = self.counts()
        with self.assertRaises(herdr_core.StartupWaitRequired) as caught:
            self.prepare(CODEX_ONLY, self.prep)
        self.assertTrue(caught.exception.expired)
        self.assertEqual(hst.read_wait(self.paths, wait["wait_id"])["status"], "EXPIRED")
        self.assertEqual(self.counts(), before)

    def test_waiting_costs_no_model_wakeups_no_prompts_and_no_polling(self) -> None:
        wait = self.open_codex_wait()
        before = self.counts()
        reads, sleeps = len(self.herdr.pane_reads), len(self.clock.sleeps)
        self.clock.current += 600  # ten idle minutes pass
        self.sup.sweep_startup_waits()
        self.assertEqual((self.counts(), len(self.herdr.pane_reads), len(self.clock.sleeps)), (before, reads, sleeps))
        self.assertEqual(self.herdr.prompts, [])
        self.assertEqual(hst.read_wait(self.paths, wait["wait_id"])["status"], "WAITING")

    def test_the_callers_total_deadline_caps_every_wait(self) -> None:
        self.config["session_start"]["startup_wait"] = {"not_after_unix": self.clock.current + 120}
        self.sup = self.make_supervisor()
        wait = self.open_codex_wait()
        self.assertEqual(wait["deadline_unix"], self.clock.current + 120)
        self.assertEqual(wait["budget_remaining_at_open_seconds"], 120)

    def test_a_trust_dialog_is_a_separate_decision_and_never_an_update_wait(self) -> None:
        self.prep = str(uuid.uuid4())
        self.herdr.startup_block = "blocked_agent"
        self.herdr.screens["w9:p1"] = "\n".join(["(synthetic) Do you trust the files in this folder?", "› 1. Trust and continue", "  2. (synthetic) No"])
        with self.assertRaises(hs.SupervisorError) as caught:
            self.prepare(CODEX_ONLY, self.prep)
        self.assertNotIsInstance(caught.exception, herdr_core.StartupWaitRequired)
        self.assertIn("separate decision", str(caught.exception))
        self.assertEqual(self.waits(), [])
        self.assertEqual(self.startup_events(), [])
        self.assertEqual(self.counts()["keys"], 0)

    def assert_unknown_wait(self, mode: str, screen: str, kind: str, evidence_available: bool) -> None:
        self.herdr.pane_read_mode = mode
        if mode == "none":
            self.herdr.pane_read = None  # type: ignore[assignment]
        wait = self.open_codex_wait(screen=screen)
        self.assertEqual((wait["classification"]["recognized"], wait["classification"]["kind"]), (False, kind))
        self.assertEqual(wait["evidence"]["available"], evidence_available)
        view = hst.wait_view(wait, self.clock.current)
        self.assertTrue(any("does not recognize" in line or "could not be read" in line for line in hst.instructions(view)))
        self.assertTrue(any("folder-trust or permission dialog" in line for line in hst.instructions(view)))
        self.herdr.unblock("w9:p1")
        self.assertTrue(self.continue_(wait["wait_id"])["ok"], "unknown waits use the same identity checks and continue the same way")

    def test_an_unreadable_screen_is_an_actionable_unknown_wait_not_recognition(self) -> None:
        self.assert_unknown_wait("error", "", "screen_unavailable", False)

    def test_a_missing_pane_read_contract_is_an_actionable_unknown_wait_not_recognition(self) -> None:
        self.assert_unknown_wait("none", "", "screen_unavailable", False)

    def test_an_unrecognized_screen_is_an_actionable_unknown_wait_not_recognition(self) -> None:
        self.assert_unknown_wait("ok", "(synthetic) something this supervisor has never seen\nprompt>", "unknown_screen", True)

    def test_only_a_single_blocked_or_unknown_agent_is_a_startup_signal(self) -> None:
        for status, waits in (("blocked", True), ("unknown", True), ("working", False)):
            with self.subTest(status):
                case = _fresh_case(self)
                case.prep = str(uuid.uuid4())
                original = case.herdr.pane_run

                def started(*args, __status=status, __case=case, __original=original, **kwargs):
                    result = __original(*args, **kwargs)
                    for agent in __case.herdr.agents.values():
                        if agent.get("pane_id") == "w9:p1":
                            agent["agent_status"] = __status
                    return result

                case.herdr.pane_run = started
                case.herdr.screens["w9:p1"] = update_menu_screen()
                with case.assertRaises(hs.SupervisorError) as caught:
                    case.prepare(CODEX_ONLY, case.prep)
                self.assertEqual(isinstance(caught.exception, herdr_core.StartupWaitRequired), waits)
                self.assertEqual(len(case.waits()), 1 if waits else 0)
                self.assertEqual(case.counts()["keys"], 0)

    def test_claude_startup_screens_stay_unknown_but_use_the_same_checks(self) -> None:
        self.prep = str(uuid.uuid4())
        self.herdr.startup_block = "start_error"
        synthetic = json.loads((FIXTURES / "claude_synthetic_unknown.json").read_text())
        self.herdr.screens["w9:p1"] = "\n".join(synthetic["visible_lines"])  # SYNTHETIC; contains Codex's option words on purpose
        with self.assertRaises(herdr_core.StartupWaitRequired):
            self.prepare(FRESH_CLAUDE, self.prep)
        wait = self.only_wait()
        self.assertEqual((wait["provider"], wait["classification"]["kind"], wait["classification"]["recognized"]), ("claude", "unknown_screen", False))
        self.assertEqual(wait["executable"], CLAUDE_EXE)
        self.assertEqual((wait["phase"], wait["process"]["argv"][0]), ("AGENT_STARTING", "claude"))
        before = self.counts()
        self.herdr.unblock("w9:p1")
        self.exe = dict(EXE)
        self.sup.startup_executable_record = lambda command: {**CLAUDE_EXE, "sha256": "d" * 64}  # Claude was updated in place
        result = self.continue_(wait["wait_id"])
        self.assertEqual((result["ok"], result["kind"]), (False, "changed_executable"))
        self.sup.startup_executable_record = lambda command: dict(CLAUDE_EXE)
        result = self.continue_(wait["wait_id"])
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.counts(), before)

    def test_fresh_all_waits_for_the_blocked_provider_without_touching_the_other_or_any_owner(self) -> None:
        self.prep = str(uuid.uuid4())
        self.herdr.startup_block = "blocked_agent"
        self.herdr.startup_block_for = "codex-run-"
        with self.assertRaises(herdr_core.StartupWaitRequired) as caught:
            self.prepare(FRESH_ALL, self.prep)
        self.assertEqual(caught.exception.provider, "codex")
        journal = self.journal(self.prep)
        self.assertEqual(journal["providers"]["codex"]["status"], "AGENT_STARTING")
        self.assertNotIn("claude", {name.split("-run-")[0] for name in self.herdr.agents if "-run-" in name}, "Claude is only started after Codex is through")
        self.assertEqual(self.sup.owners(), self.owners)
        self.herdr.unblock("w9:p1")
        self.assertTrue(self.continue_(self.only_wait()["wait_id"])["ok"])
        self.assertEqual(self.journal(self.prep)["status"], "OWNERSHIP_BOUND")
        self.assertEqual({p: v["status"] for p, v in self.journal(self.prep)["providers"].items()}, {"codex": "IDENTITY_VERIFIED", "claude": "IDENTITY_VERIFIED"})

    def test_uncertain_submissions_never_open_a_wait_and_stay_observation_only(self) -> None:
        # a bootstrap that was submitted but never acknowledged is uncertain: no wait, no resend, only observation
        self.herdr.hold_identity = True
        self.config["session_start"]["native_identity_bootstrap"] = True
        self.sup = self.make_supervisor()
        self.herdr.bootstrap_mode = "swallow"
        self.prep = str(uuid.uuid4())
        with self.assertRaises(hs.SupervisorError) as caught:
            self.prepare(CODEX_ONLY, self.prep)
        self.assertNotIsInstance(caught.exception, herdr_core.StartupWaitRequired)
        self.assertEqual(self.waits(), [])
        sent = len(self.herdr.bootstrap_prompts)
        with self.assertRaises(hs.SupervisorError):
            self.prepare(CODEX_ONLY, self.prep)
        self.assertEqual(len(self.herdr.bootstrap_prompts), sent, "never resent")
        self.assertEqual(self.waits(), [])

    def test_crash_and_restart_around_the_continuation_markers_never_repeat_a_side_effect(self) -> None:
        wait = self.open_codex_wait()
        self.herdr.unblock("w9:p1")
        before = self.counts()
        self.sup = self.make_supervisor()  # worker restart: only the files survive
        self.assertEqual(hst.read_wait(self.paths, wait["wait_id"])["status"], "WAITING")
        calls = []

        def crash(*args, **kwargs):
            calls.append(hst.read_wait(self.paths, wait["wait_id"])["status"])
            raise RuntimeError("worker killed after the durable CONTINUING marker")

        self.sup.prepare_task_sessions = crash
        with self.assertRaises(RuntimeError):
            self.continue_(wait["wait_id"])
        self.assertEqual(calls, ["CONTINUING"], "the marker is durable BEFORE the machinery runs")
        self.sup = self.make_supervisor()
        duplicate = self.continue_(wait["wait_id"])
        self.assertEqual((duplicate["ok"], duplicate["kind"]), (False, "not_waiting"))
        self.assertEqual(self.counts(), before)
        # the human's retry of the same task start reconciles the journaled phase; the sweeper then resolves the wait
        self.assertEqual(self.prepare(CODEX_ONLY, self.prep)["status"], "OWNERSHIP_BOUND")
        self.assertEqual(self.sup.sweep_startup_waits(), [wait["wait_id"]])
        self.assertEqual(hst.read_wait(self.paths, wait["wait_id"])["status"], "RESOLVED")
        self.assertEqual(self.counts(), before)

    def test_a_continuation_while_another_task_is_running_is_refused_before_anything_is_checked(self) -> None:
        wait = self.open_codex_wait()
        self.herdr.unblock("w9:p1")
        with self.sup.store.transaction():  # a live task, however it came to exist: fresh preparation is only allowed at a terminal boundary
            state = self.sup.initialize("Add the widget", "codex", workflow_policy="gated_v2")
        self.assertNotIn(state["supervisor_state"], ("DONE", "CANCELLED"))
        before, reads = self.counts(), len(self.herdr.pane_reads)
        result = self.continue_(wait["wait_id"])
        self.assertEqual((result["ok"], result["kind"]), (False, "task_boundary"))
        self.assertEqual((self.counts(), len(self.herdr.pane_reads)), (before, reads), "refused before any live check")
        self.assertEqual(hst.read_wait(self.paths, wait["wait_id"])["status"], "WAITING")


class ExplicitWaitTests(ExplicitCase, WaitCase):
    """The explicit-launch route: the provider process runs but Herdr has not detected an agent (a startup prompt is up)."""

    def setUp(self) -> None:
        ExplicitCase.setUp(self)
        self.exe = dict(EXE)
        self.ticks = {}
        self.sup = self.make_supervisor()

    def make_supervisor(self) -> "hs.Supervisor":
        return WaitCase.make_supervisor(self)

    def open_explicit_wait(self, screen: str | None = None) -> dict:
        self.prep = str(uuid.uuid4())
        self.herdr.startup_block = "undetected"
        self.herdr.screens["w9:p1"] = screen if screen is not None else update_menu_screen()
        with self.assertRaises(herdr_core.StartupWaitRequired) as caught:
            self.prepare(CODEX_ONLY, self.prep)
        return hst.read_wait(self.paths, caught.exception.wait_id)

    def test_an_undetected_agent_after_the_bound_launch_opens_a_wait_at_process_bound_and_continues_without_relaunch(self) -> None:
        wait = self.open_explicit_wait()
        item = self.journal(self.prep)["providers"]["codex"]
        self.assertEqual((wait["phase"], wait["site"], wait["status"]), ("PROCESS_BOUND", "launch_alias_unverified", "WAITING"))
        self.assertEqual((item["status"], wait["binding"]["launch_digest"], wait["process"]["argv"][1:]), ("PROCESS_BOUND", item["launch"]["recipe"]["digest"], ["--no-daemon"]))
        self.assertEqual(wait["executable"], EXE)
        self.assertEqual(len(self.herdr.pane_runs), 1, "the single `pane run` was submitted before the wait and is never repeated")
        before = self.counts()
        self.herdr.unblock("w9:p1")
        result = self.continue_(wait["wait_id"])
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.counts(), before)
        done = self.journal(self.prep)
        self.assertEqual((done["status"], done["providers"]["codex"]["status"]), ("OWNERSHIP_BOUND", "IDENTITY_VERIFIED"))
        self.assertEqual(self.herdr.agent_renames, [("w9:p1", f"codex-run-{self.prep[:8]}")], "the alias is established once, after the human handled the pane")

    def test_a_replaced_launch_process_is_refused_for_the_explicit_route_too(self) -> None:
        wait = self.open_explicit_wait()
        process = self.herdr.processes["w9:p1"]["foreground_processes"][0]
        process["pid"] += 50
        self.herdr.unblock("w9:p1")
        result = self.continue_(wait["wait_id"])
        self.assertEqual((result["ok"], result["kind"]), (False, "changed_process"))
        self.assertEqual(len(self.herdr.pane_runs), 1)
        self.assertEqual(self.herdr.agent_renames, [])

    def test_a_second_wait_gets_only_the_unspent_budget_and_the_wait_count_is_bounded(self) -> None:
        self.config["session_start"]["startup_wait"] = {"max_seconds": 600, "max_waits_per_provider": 2}
        self.sup = self.make_supervisor()
        first = self.open_explicit_wait()
        self.clock.current += 400  # 400 of the 600 seconds were spent waiting
        self.herdr.unblock("w9:p1")
        original = self.herdr.rename_agent

        def blocked_again(target: str, name: str):
            result = original(target, name)
            for agent in self.herdr.agents.values():
                if agent.get("pane_id") == target:
                    agent["agent_status"] = "blocked"  # the provider then shows another startup screen before reporting ready
            self.herdr.screens[target] = update_menu_screen()
            return result

        self.herdr.rename_agent = blocked_again
        result = self.continue_(first["wait_id"])
        self.assertEqual((result["ok"], result["kind"]), (True, "waiting_again"), result)
        waits = self.waits()
        self.assertEqual([w["status"] for w in waits], ["SUPERSEDED", "WAITING"])
        second = waits[1]
        self.assertEqual((second["phase"], second["site"], second["budget_remaining_at_open_seconds"]), ("AGENT_STARTING", "agent_not_ready", 200), "only the unspent part of the same budget")
        self.assertLessEqual(second["deadline_unix"], second["opened_at_unix"] + 200)
        self.assertEqual(len(self.herdr.pane_runs), 1)
        # a third block in the same preparation exceeds the count bound (2): the signal stays a plain, fail-closed error
        calls = {"n": 0}
        listed = self.herdr.list_agents

        def flaky():
            calls["n"] += 1
            if calls["n"] >= 2:  # call 1 is the continuation's own readiness check; the next belongs to the resumed machinery
                for agent in self.herdr.agents.values():
                    if agent.get("pane_id") == "w9:p1":
                        agent["agent_status"] = "blocked"
            return listed()

        self.herdr.unblock("w9:p1")
        self.herdr.list_agents = flaky
        third = self.continue_(second["wait_id"])
        self.assertEqual((third["ok"], third["kind"]), (False, "preparation_stopped"), third)
        self.assertIn("count", third["message"])
        self.assertEqual(len(self.waits()), 2)
        self.assertEqual(len(self.herdr.pane_runs), 1)

    def test_a_launch_that_is_only_admitted_or_lost_is_never_a_wait(self) -> None:
        self.prep = str(uuid.uuid4())
        self.herdr.run_mode = "lost"  # pane run timed out: the outcome of the submission is uncertain
        with self.assertRaises(hs.SupervisorError) as caught:
            self.prepare(CODEX_ONLY, self.prep)
        self.assertNotIsInstance(caught.exception, herdr_core.StartupWaitRequired)
        self.assertEqual(self.waits(), [])
        self.assertEqual(len(self.herdr.pane_runs), 1)
        try:
            self.prepare(CODEX_ONLY, self.prep)  # the existing reconcile path: it may now observe the process; it must never resubmit
        except hs.SupervisorError as error:
            self.assertNotIsInstance(error, herdr_core.StartupWaitRequired)
        self.assertEqual(len(self.herdr.pane_runs), 1, "observation-only: zero resend")
        self.assertEqual(self.waits(), [])


if __name__ == "__main__":
    import unittest

    unittest.main()
