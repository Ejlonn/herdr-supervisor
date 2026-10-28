"""Herdr 0.9.5 pane rename/label contract: recorded read-only captures (placeholders only), exact argv, and the
explicit unverified outcome when the label field has not been observed live."""

from __future__ import annotations

import getpass
import json
import unittest
from pathlib import Path

import herdr_cli
import herdr_core

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "herdr" / "0.9.5-pane-label"


class RecordingCli(herdr_cli.HerdrCli):
    def __init__(self, responses: dict[tuple[str, ...], object] | None = None) -> None:
        super().__init__("/fake/herdr")
        self.calls: list[list[str]] = []
        self.responses = responses or {}

    def _run(self, args, *, timeout, json_result=True):  # type: ignore[override]
        argv = list(args)
        self.calls.append(argv)
        value = self.responses.get(tuple(argv[:2]))
        if isinstance(value, Exception):
            raise value
        return value if json_result else (value or "")


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


class PaneLabelContractTests(unittest.TestCase):
    def test_rename_uses_the_documented_pane_command_with_one_exact_pane(self) -> None:
        cli = RecordingCli()
        cli.rename_pane("w1:p3", "Fresh Codex abcd1234")
        self.assertEqual(cli.calls, [["pane", "rename", "w1:p3", "Fresh Codex abcd1234"]])
        help_text = (FIXTURES / "pane-rename-help.txt").read_text()
        self.assertIn("herdr pane rename [OPTIONS] <PANE_ID> [LABEL]...", help_text)
        self.assertNotIn("--agent", help_text, "rename addresses a pane, not an agent alias")

    def test_an_unrenamed_live_pane_has_no_label_key_so_the_label_is_unverified_not_assumed(self) -> None:
        recorded = load("pane-get-unlabelled.json")
        self.assertNotIn("label", recorded["result"]["pane"])
        self.assertIn("agent", recorded["result"]["pane"])
        cli = RecordingCli({("pane", "get"): recorded})
        self.assertIsNone(cli.pane_label("w1:p3"))
        self.assertEqual(cli.calls, [["pane", "get", "w1:p3"]])

    def test_the_synthetic_labelled_shape_is_read_but_bad_values_are_ignored(self) -> None:
        cli = RecordingCli({("pane", "get"): load("pane-get-labelled-SYNTHETIC.json")})
        self.assertEqual(cli.pane_label("w1:p3"), "Fresh Codex abcd1234")
        for bad in (None, "", 7, ["x"]):
            payload = load("pane-get-unlabelled.json")
            payload["result"]["pane"]["label"] = bad
            self.assertIsNone(RecordingCli({("pane", "get"): payload}).pane_label("w1:p3"))
        self.assertIsNone(RecordingCli({("pane", "get"): {"result": {}}}).pane_label("w1:p3"))

    def test_a_missing_pane_is_an_error_not_a_label(self) -> None:
        cli = RecordingCli({("pane", "get"): herdr_core.HerdrError("pane get failed", code="pane_not_found")})
        with self.assertRaises(herdr_core.HerdrError):
            cli.pane_label("w9:p9")

    def test_agent_start_help_promises_process_readiness_only(self) -> None:
        text = (FIXTURES / "agent-start-help.txt").read_text()
        self.assertIn("ready for input", text)
        self.assertNotIn("session", text.lower().replace("same terminal", ""), "start does not promise a native session identity")

    def test_provenance_declares_read_only_capture_and_the_unobserved_field(self) -> None:
        provenance = load("provenance.json")
        self.assertTrue(provenance["capture_method"].startswith("read-only"))
        self.assertIn("NOT observed", provenance["limits"])
        self.assertEqual((FIXTURES / "version.txt").read_text().strip(), provenance["herdr_version"])
        for path in FIXTURES.glob("*.json"):
            text = path.read_text()
            self.assertNotIn("/home/" + getpass.getuser(), text, path.name)


if __name__ == "__main__":
    unittest.main()
