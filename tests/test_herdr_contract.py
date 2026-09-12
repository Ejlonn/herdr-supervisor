"""Recorded Herdr 0.9.0 CLI contract: version policy, semantic capability probes, exact adapter argument
construction, response/error envelope parsing, lifecycle states, and fixture privacy. Nothing here
touches a live Herdr."""

from __future__ import annotations

import json
import os
import re
import subprocess
import unittest
from pathlib import Path
from unittest import mock

import herdr_cli  # noqa: E402
import herdr_core  # noqa: E402
from v2_fixtures import hs  # noqa: E402,F401

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "herdr" / "0.9.0"
HELP = FIXTURES / "help"
RESPONSES = FIXTURES / "responses"
COMMANDS = {c for _, c, _ in herdr_cli.REQUIRED_CAPABILITIES} | {c for _, c, _ in herdr_cli.OPTIONAL_CAPABILITIES}


def help_texts() -> dict[tuple[str, ...], str | None]:
    return {command: (HELP / ("-".join(command) + ".txt")).read_text() for command in COMMANDS}


def response(name: str) -> dict:
    return json.loads((RESPONSES / f"{name}.json").read_text())


class RecordingCli(herdr_cli.HerdrCli):
    """Adapter whose subprocess layer is replaced by fixture playback; records exact argv."""

    def __init__(self, playback: dict[tuple[str, ...], dict] | None = None) -> None:
        super().__init__("/fake/herdr")
        self.calls: list[list[str]] = []
        self.playback = playback or {}

    def _run(self, args, *, timeout, json_result=True):  # type: ignore[override]
        argv = list(args)
        self.calls.append(argv)
        key = tuple(argv)
        fixture = self.playback.get(key) or self.playback.get(tuple(argv[:2])) or self.playback.get(tuple(argv[:3]))
        if fixture is None:
            return {"result": {}} if json_result else ""
        if "raw" in fixture:
            text = fixture["raw"]
        else:
            text = json.dumps(fixture["response"])
        if fixture.get("exit_code", 0) != 0:
            code = herdr_cli.command_error_code(text)
            raise herdr_core.HerdrError(f"herdr {' '.join(argv[:2])} failed: {code}", code=code, output=text)
        if not json_result:
            return text
        try:
            return json.loads(text)
        except json.JSONDecodeError as error:
            raise herdr_core.HerdrError("herdr returned invalid JSON", output=text) from error


class VersionPolicyTests(unittest.TestCase):
    def test_recorded_version_meets_the_minimum_and_every_required_capability_is_present(self) -> None:
        version = (FIXTURES / "version.txt").read_text()
        self.assertEqual(herdr_cli.parse_herdr_version(version), (0, 9, 0))
        contract = herdr_cli.evaluate_contract(version, help_texts())
        self.assertTrue(contract["compatible"], contract)
        self.assertEqual(contract["missing_capabilities"], [])
        self.assertEqual(contract["detected_version"], "0.9.0")
        self.assertEqual(contract["minimum_version"], "0.9.0")
        self.assertEqual(contract["prompt_ack_mode"], "lifecycle")
        self.assertTrue(all(contract["required"].values()))

    def test_version_floor_is_necessary_but_not_sufficient(self) -> None:
        texts = help_texts()
        for version_text, expected_ok in (("herdr 0.8.9", False), ("herdr 0.9.0", True), ("herdr 0.9.7", True), ("herdr 1.2.0", True), ("herdr", False), ("", False), ("herdr v0.9", False), (None, False)):
            with self.subTest(version=version_text):
                contract = herdr_cli.evaluate_contract(version_text, texts)
                self.assertEqual(contract["version_ok"], expected_ok)
                self.assertEqual(contract["compatible"], expected_ok)
        # a newer version that dropped a required option is incompatible; detected version still reported
        degraded = dict(texts)
        degraded[("agent", "read")] = degraded[("agent", "read")].replace("recent-unwrapped", "recent")
        contract = herdr_cli.evaluate_contract("herdr 1.4.0", degraded)
        self.assertFalse(contract["compatible"])
        self.assertEqual(contract["detected_version"], "1.4.0")
        self.assertEqual(contract["missing_capabilities"], ["agent read (source/format/lines)"])
        # a missing command is a missing capability
        absent = dict(texts)
        absent[("pane", "get")] = None
        self.assertIn("pane get", herdr_cli.evaluate_contract("herdr 0.9.0", absent)["missing_capabilities"])

    def test_optional_acknowledgement_is_reported_separately_from_required_failures(self) -> None:
        texts = help_texts()
        # dropping --until from prompt help removes only the optional acceleration; --wait/--timeout keep the contract
        without_until = dict(texts)
        without_until[("agent", "prompt")] = texts[("agent", "prompt")].replace("--until", "--unti1").replace("agent_prompt_stalled", "stalled")
        contract = herdr_cli.evaluate_contract("herdr 0.9.0", without_until)
        self.assertTrue(contract["compatible"])
        self.assertEqual(contract["prompt_ack_mode"], "settle")
        # dropping --wait is a required failure, not an optional one
        without_wait = dict(texts)
        without_wait[("agent", "prompt")] = texts[("agent", "prompt")].replace("--wait", "--wai7")
        self.assertFalse(herdr_cli.evaluate_contract("herdr 0.9.0", without_wait)["compatible"])
        only_optional_dropped = dict(texts)
        only_optional_dropped[("agent", "prompt")] = texts[("agent", "prompt")].replace("agent_prompt_stalled", "stalled")
        contract = herdr_cli.evaluate_contract("herdr 0.9.0", only_optional_dropped)
        self.assertTrue(contract["compatible"])
        self.assertEqual(contract["prompt_ack_mode"], "settle")
        self.assertEqual(contract["missing_capabilities"], [])

    def test_exact_lexemes_reject_prefix_and_suffix_decoys(self) -> None:
        texts = help_texts()
        intact = herdr_cli.evaluate_contract("herdr 0.9.0", texts)
        self.assertTrue(intact["compatible"])  # positive control on the untouched captures
        self.assertEqual(intact["prompt_ack_mode"], "lifecycle")
        decoys = {
            ("agent", "prompt"): [("--wait", "--waiter"), ("--timeout", "--timeout-ms"), ("--wait", "--wait-for")],
            ("agent", "read"): [("--source", "--source-kind"), ("--format", "--formatted"), ("--lines", "--linesize"), ("recent-unwrapped", "recent-unwrapped-x"), ("recent-unwrapped", "xrecent-unwrapped")],
            ("agent", "wait"): [("--until", "--until-state"), ("--timeout", "x--timeout")],
            ("agent", "start"): [("--kind", "--kinds"), ("--pane", "--pane-id")],
            ("workspace", "create"): [("--cwd", "--cwdir"), ("--label", "--labels"), ("--no-focus", "--no-focus-window")],
            ("plugin", "action", "invoke"): [("--plugin", "--plugins")],
        }
        for command, pairs in decoys.items():
            for real, decoy in pairs:
                with self.subTest(command=command, decoy=decoy):
                    mutated = dict(texts)
                    mutated[command] = texts[command].replace(real, decoy)
                    self.assertFalse(herdr_cli.help_has_lexeme(mutated[command], real), f"{decoy} must not satisfy {real}")
                    contract = herdr_cli.evaluate_contract("herdr 0.9.0", mutated)
                    self.assertFalse(contract["compatible"], f"{decoy} accepted for {real}")
                    self.assertTrue(contract["missing_capabilities"], decoy)
        # optional acknowledgement decoys degrade to the settle fallback while the baseline stays compatible
        for real, decoy in (("agent_prompt_stalled", "agent_prompt_stalled_ms"), ("--until", "--untill")):
            with self.subTest(optional=decoy):
                mutated = dict(texts)
                mutated[("agent", "prompt")] = texts[("agent", "prompt")].replace(real, decoy)
                contract = herdr_cli.evaluate_contract("herdr 0.9.0", mutated)
                self.assertEqual(contract["prompt_ack_mode"], "settle")
                self.assertTrue(contract["compatible"] if real != "--until" else contract["compatible"], decoy)
        # lexeme boundaries: punctuation and line ends count as boundaries, identifier characters do not
        self.assertTrue(herdr_cli.help_has_lexeme("[possible values: visible, recent, recent-unwrapped, detection]", "recent-unwrapped"))
        self.assertTrue(herdr_cli.help_has_lexeme("      --wait\n", "--wait"))
        self.assertFalse(herdr_cli.help_has_lexeme("--waiting --wait-for", "--wait"))
        self.assertFalse(herdr_cli.help_has_lexeme(None, "--wait"))

    def test_probe_is_read_only_and_cached(self) -> None:
        cli = RecordingCli({("--version",): {"response": None, "raw": "herdr 0.9.0\n"}, **{(*c, "--help"): {"raw": t} for c, t in help_texts().items()}})
        report = cli.capability_report()
        self.assertTrue(report["compatible"])
        calls = cli.calls
        self.assertIn(["--version"], calls)
        self.assertTrue(all(call == ["--version"] or call[-1] == "--help" for call in calls), calls)
        cli.capability_report()
        cli.prompt_ack_supported()
        self.assertEqual(len(cli.calls), len(calls), "one probe per process")


class AdapterArgumentTests(unittest.TestCase):
    def test_exact_argv_for_every_command(self) -> None:
        cli = RecordingCli({("agent", "list"): response("agent-list"), ("agent", "get"): response("agent-get"), ("pane", "get"): response("pane-get"), ("pane", "split"): response("pane-split")})
        cli.list_agents(); cli.get_agent("codex-main"); cli.read_agent("w1:p2", source="recent-unwrapped", lines=400); cli.read_agent("codex-main", source="visible", lines=None)
        cli.prompt("codex-main", "hi", timeout_ms=20000); cli.prompt_ack("w1:p2", "hi", timeout_ms=8000); cli.wait("codex-main", timeout_ms=10000)
        cli.send_keys("codex-main", ["Enter"]); cli.start_agent("codex-main", kind="codex", pane_id="w1:p2", args=["resume", "x"]); cli.pane_available("w1:p2")
        cli.create_workspace(label="herdr-supervisor codex recovery", cwd="/home/user/workspace")
        self.assertEqual(cli.split_pane("w1:p2",direction="right",ratio=0.5,cwd="/home/user/workspace"),"w1:p3")
        self.assertEqual(cli.calls, [
            ["agent", "list"], ["agent", "get", "codex-main"],
            ["agent", "read", "w1:p2", "--source", "recent-unwrapped", "--format", "text", "--lines", "400"],
            ["agent", "read", "codex-main", "--source", "visible", "--format", "text"],
            ["agent", "prompt", "codex-main", "hi", "--wait", "--timeout", "20000"],
            ["agent", "prompt", "w1:p2", "hi", "--wait", "--until", "working", "--until", "blocked", "--timeout", "8000"],
            ["agent", "wait", "codex-main", "--timeout", "10000"],
            ["agent", "send-keys", "codex-main", "Enter"],
            ["agent", "start", "codex-main", "--kind", "codex", "--pane", "w1:p2", "--", "resume", "x"],
            ["pane", "get", "w1:p2"],
            ["workspace", "create", "--cwd", "/home/user/workspace", "--label", "herdr-supervisor codex recovery", "--no-focus"],
            ["pane", "split", "w1:p2", "--direction", "right", "--ratio", "0.5", "--cwd", "/home/user/workspace", "--no-focus"],
        ])
        # every option the adapter emits is documented by the recorded help of that command
        texts = help_texts()
        for call in cli.calls:
            command = tuple(call[:3]) if tuple(call[:3]) in texts else tuple(call[:2])
            for token in call:
                if token.startswith("--") and token != "--":
                    self.assertIn(token, texts[command], f"{command}: {token} is not in the recorded help")

    def test_pane_and_agent_targets_are_both_accepted_verbatim(self) -> None:
        cli = RecordingCli({("agent", "get"): response("agent-get")})
        cli.get_agent("w1:p2")
        cli.wait("w1:p2", timeout_ms=5)
        self.assertEqual([c[2] for c in cli.calls], ["w1:p2", "w1:p2"])


class ResponseParsingTests(unittest.TestCase):
    def test_recorded_list_get_and_pane_shapes(self) -> None:
        cli = RecordingCli({("agent", "list"): response("agent-list"), ("agent", "get"): response("agent-get"), ("pane", "get"): response("pane-get")})
        agents = cli.list_agents()
        self.assertEqual({a["agent"] for a in agents}, {"codex", "claude"})
        for agent in agents:
            self.assertIn(agent["agent_status"], herdr_core.LIFECYCLE_STATES)
            self.assertRegex(herdr_cli.session_identity(agent), r"^[0-9a-f-]{36}$")
            self.assertRegex(agent["pane_id"], r"^w\d+:p\d+$")
        got = cli.get_agent("w1:p2")
        self.assertEqual(herdr_cli.session_identity(got), "11111111-1111-4111-8111-111111111111")
        self.assertEqual(got["name"], "codex-main")
        self.assertFalse(cli.pane_available("w1:p2"), "a pane hosting an agent is not available for restore")
        empty = response("pane-get")
        empty["response"]["result"]["pane"]["agent"] = None
        cli = RecordingCli({("pane", "get"): empty})
        self.assertTrue(cli.pane_available("w1:p2"))

    def test_error_envelopes_map_to_codes(self) -> None:
        for name, code in (("error-agent-get-not-found", "agent_not_found"), ("error-agent-read-not-found", "agent_not_found"), ("synthetic-error-agent-blocked", "agent_blocked"),
                           ("synthetic-error-prompt-stalled", "agent_prompt_stalled"), ("synthetic-error-timeout", "timeout"), ("synthetic-error-agent-not-idle", "agent_not_idle")):
            with self.subTest(name):
                fixture = response(name)
                self.assertEqual(fixture["response"]["error"]["code"], code)
                self.assertEqual(herdr_cli.command_error_code(json.dumps(fixture["response"])), code)
                cli = RecordingCli({("agent", "get"): fixture, ("agent", "read"): fixture, ("agent", "prompt"): fixture, ("agent", "wait"): fixture})
                with self.assertRaises(herdr_core.HerdrError) as caught:
                    cli.get_agent("x")
                self.assertEqual(caught.exception.code, code)
        self.assertEqual(herdr_cli.command_error_code("garbage without a code"), "command_error")
        self.assertEqual(response("error-pane-get-not-found")["response"]["error"]["code"], "pane_not_found")
        cli = RecordingCli({("pane", "get"): response("error-pane-get-not-found")})
        self.assertFalse(cli.pane_available("w99:p99"))

    def test_malformed_and_unknown_data_fail_closed(self) -> None:
        cli = RecordingCli({("agent", "get"): response("synthetic-malformed-envelope")})
        with self.assertRaises(herdr_core.HerdrError):
            cli.get_agent("codex-main")
        cli = RecordingCli({("agent", "get"): response("synthetic-not-json")})
        with self.assertRaises(herdr_core.HerdrError):
            cli.get_agent("codex-main")
        unknown = response("synthetic-unknown-lifecycle")["response"]["result"]["agent"]
        self.assertNotIn(unknown["agent_status"], herdr_core.LIFECYCLE_STATES, "an unknown lifecycle word is not accepted")
        self.assertIsNone(herdr_cli.session_identity({"agent": "codex", "agent_session": {"kind": "id", "value": ""}}))
        self.assertIsNone(herdr_cli.session_identity({"agent": "codex"}))

    def test_local_timeout_becomes_a_timeout_error(self) -> None:
        cli = herdr_cli.HerdrCli("/fake/herdr")

        def expire(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd=args[0], timeout=1)

        with mock.patch.object(herdr_cli.subprocess, "run", side_effect=expire):
            with self.assertRaises(herdr_core.HerdrError) as caught:
                cli.wait("codex-main", timeout_ms=1)
        self.assertEqual(caught.exception.code, "timeout")


class FixturePrivacyTests(unittest.TestCase):
    """Every fixture file may contain only the declared placeholder identities. Failures name the file and
    the category, never the leaked value."""

    PLACEHOLDER_SESSIONS = {"11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222", "00000000-0000-4000-8000-000000000000"}
    PLACEHOLDER_PANES = {"w1:p1", "w1:p2", "w1:p3"}
    PROBE_PANES = {"w99:p99"}  # the deliberately non-existent pane used for the recorded pane_not_found probe
    UUID_RE = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b")
    PANE_RE = re.compile(r"\bw\d+:p\d+\b")
    TAB_RE = re.compile(r"\bw\d+:t\d+\b")
    HOME_RE = re.compile(r"/home/[A-Za-z0-9._-]+")
    TERM_RE = re.compile(r"\bterm_[0-9a-f]+\b")

    def fixture_files(self) -> list[Path]:
        return sorted(p for p in FIXTURES.rglob("*") if p.is_file())

    def test_provenance_declares_the_capture_and_only_placeholders(self) -> None:
        provenance = json.loads((FIXTURES / "provenance.json").read_text())
        self.assertEqual(provenance["herdr_version"], "herdr 0.9.0")
        self.assertIn("No prompt", provenance["capture_method"])
        self.assertIn("not recorded", provenance["statement"])
        self.assertEqual(set(provenance["placeholders"]["panes"]), self.PLACEHOLDER_PANES)
        self.assertEqual(set(provenance["placeholders"]["session_ids"].values()) <= self.PLACEHOLDER_SESSIONS, True)
        for name in provenance["recorded_responses"]:
            self.assertTrue((RESPONSES / f"{name}.json").is_file(), name)
        for name in provenance["synthetic_responses"]:
            self.assertTrue(json.loads((RESPONSES / f"{name}.json").read_text())["synthetic"], name)

    def test_every_fixture_file_contains_only_declared_placeholders(self) -> None:
        markers: list[str] = []
        marker_file = os.environ.get("HERDR_RELEASE_PRIVATE_MARKERS")
        if marker_file and Path(marker_file).is_file():
            markers = [w.strip() for w in Path(marker_file).read_text().splitlines() if w.strip() and not w.startswith("#")]
        for path in self.fixture_files():
            rel = path.relative_to(FIXTURES)
            text = path.read_text()
            with self.subTest(file=str(rel)):
                # boolean assertions only: a failure names the file and category, never the offending value
                self.assertTrue(not (set(self.UUID_RE.findall(text)) - self.PLACEHOLDER_SESSIONS), f"{rel}: undeclared UUID-shaped identifier")
                self.assertTrue(not (set(self.PANE_RE.findall(text)) - self.PLACEHOLDER_PANES - self.PROBE_PANES), f"{rel}: undeclared pane locator")
                self.assertTrue(not (set(self.TAB_RE.findall(text)) - {"w1:t1"}), f"{rel}: undeclared tab locator")
                self.assertTrue(not (set(self.HOME_RE.findall(text)) - {"/home/user"}), f"{rel}: undeclared home path")
                self.assertTrue(not (set(self.TERM_RE.findall(text)) - {"term_000000000000"}), f"{rel}: undeclared terminal id")
                for category, needle in (("live transcript text", "Supervised run"), ("live transcript text", "Smoke test"), ("model name", "gpt-"), ("model name", "Opus"), ("quota glyph", "▰")):
                    self.assertTrue(needle not in text, f"{rel}: {category} present")
                self.assertTrue(not any(word in text for word in markers), f"{rel}: configured private marker present")
        # the sanitized tokens block carries no live quota strings at all
        listing = json.loads((RESPONSES / "agent-list.json").read_text())
        for agent in listing["response"]["result"]["agents"]:
            self.assertTrue(all(v == "sanitized" for v in agent["tokens"].values()))
            self.assertEqual(agent["cwd"], "/home/user/workspace")
            self.assertEqual(agent["terminal_title"], "example terminal title")


if __name__ == "__main__":
    unittest.main()
