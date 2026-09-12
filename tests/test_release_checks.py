"""Release consistency: one supervisor version everywhere, no secret shapes or private markers in shipped
files, CI stays least-privilege and offline, and every shipped module is packaged and documented."""

from __future__ import annotations

import importlib.metadata
import json
import re
import unittest
from pathlib import Path

import herdr_core  # noqa: E402
from v2_fixtures import hs  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SHIPPED_MODULES = ["herdr_supervisor", "herdr_core", "herdr_cli", "herdr_quota", "herdr_protocol", "herdr_redaction", "herdr_validation", "herdr_workflow", "herdr_runtime", "herdr_command", "herdr_telegram", "herdr_query", "herdr_backup", "herdr_artifacts", "herdr_present", "herdr_codex_reset", "telegram_api"]
SECRET_SHAPES = [
    re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b"),  # Telegram bot token
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),
    re.compile(r"\bxox[bpsa]-[A-Za-z0-9-]{20,}\b"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
]
ALLOWED_SECRET_FIXTURES = {"tests/v2_fixtures.py", "tests/test_redaction.py", "tests/test_release_output.py", "tests/test_telegram.py", "tests/test_release_backup.py", "tests/test_release_checks.py", "tests/test_agent_followup.py"}


def shipped_files() -> list[Path]:
    skipped = {".git", "__pycache__", ".mypy_cache", ".ruff_cache", "build", "dist"}
    return [p for p in ROOT.rglob("*") if p.is_file() and not (skipped & set(p.relative_to(ROOT).parts))]


class VersionConsistencyTests(unittest.TestCase):
    def test_single_version_source(self) -> None:
        version = herdr_core.SUPERVISOR_VERSION
        self.assertEqual((ROOT / "VERSION").read_text().strip(), version)
        self.assertEqual(hs.normalize_version(version), "0.3.0b1")
        pyproject = (ROOT / "pyproject.toml").read_text()
        self.assertIn(f'version = "{hs.normalize_version(version)}"', pyproject)
        self.assertIn(f"**{version}**", (ROOT / "README.md").read_text())
        try:
            installed = importlib.metadata.version("herdr-supervisor")
        except importlib.metadata.PackageNotFoundError:
            installed = None
        if installed is not None:
            self.assertEqual(installed, hs.normalize_version(version), "installed metadata drifted from the source version")
        provenance = json.loads((ROOT / "tests/fixtures/herdr/0.9.0/provenance.json").read_text())
        self.assertEqual(provenance["herdr_version"], f"herdr {hs.MIN_HERDR_VERSION_TEXT}", "fixture version equals the minimum tested contract")
        self.assertEqual(hs.MIN_HERDR_VERSION_TEXT, "0.9.0")
        docs = (ROOT / "docs/INSTALL.md").read_text() + (ROOT / "README.md").read_text()
        self.assertIn("0.9.0", docs, "documentation names the minimum tested Herdr version")

    def test_normalize_version_forms(self) -> None:
        for raw, expected in (("1.2.3", "1.2.3"), ("1.2.3-beta.2", "1.2.3b2"), ("1.2.3-alpha.1", "1.2.3a1"), ("1.2.3-rc.1", "1.2.3rc1"), (" 0.3.0-beta.1\n", "0.3.0b1")):
            self.assertEqual(hs.normalize_version(raw), expected)


class ShippedFileTests(unittest.TestCase):
    def test_every_shipped_module_is_packaged_installed_and_documented(self) -> None:
        pyproject = (ROOT / "pyproject.toml").read_text()
        for module in SHIPPED_MODULES:
            self.assertTrue((ROOT / "src" / f"{module}.py").is_file(), module)
            self.assertIn(f'"{module}"', pyproject, f"{module} missing from py-modules")
        self.assertEqual(sorted(p.stem for p in (ROOT / "src").glob("*.py")), sorted(SHIPPED_MODULES), "unlisted module under src/")
        manifest = (ROOT / "MANIFEST.txt").read_text()
        self.assertIn("src/*.py", manifest)
        architecture = (ROOT / "docs/ARCHITECTURE.md").read_text()
        for module in SHIPPED_MODULES:
            self.assertIn(f"`{module}.py`", architecture, f"{module} not described in docs/ARCHITECTURE.md")
        manifest_in = (ROOT / "MANIFEST.in").read_text()
        for line in ("recursive-include tests *.py", "recursive-include tests/fixtures", "recursive-include .github *.yml"):
            self.assertIn(line, manifest_in)

    def test_no_secret_shapes_in_shipped_files(self) -> None:
        for path in shipped_files():
            rel = str(path.relative_to(ROOT))
            if path.suffix in (".png", ".jpg", ".gz", ".whl"):
                continue
            try:
                text = path.read_text()
            except UnicodeDecodeError:
                continue
            for pattern in SECRET_SHAPES:
                hits = pattern.findall(text)
                if hits and rel not in ALLOWED_SECRET_FIXTURES:
                    self.fail(f"{rel}: secret-shaped text {hits[0][:12]}…")
                if hits and rel in ALLOWED_SECRET_FIXTURES:
                    for hit in hits:
                        self.assertTrue(any(marker in hit for marker in ("Fake", "ABCDEFGHIJKLMNOP", "ZQX9", "1234567890", "abcdefghij", "PRIVATE KEY")), f"{rel}: {hit[:20]} is not an obvious test value")

    def test_ci_workflow_is_least_privilege_and_offline(self) -> None:
        workflow = (ROOT / ".github/workflows/ci.yml").read_text()
        self.assertIn("permissions:\n  contents: read", workflow)
        self.assertIn("persist-credentials: false", workflow)
        self.assertNotIn("secrets.", workflow)
        self.assertNotIn("herdr agent", workflow)
        self.assertNotIn("api.telegram.org", workflow)
        for action in re.findall(r"uses: ([^\s]+)", workflow):
            self.assertRegex(action, r"@v\d+$", f"{action} must pin a major version")
        self.assertIn('python: ["3.11", "3.13"]', workflow)
        self.assertIn("--fail-under=80", workflow)
        self.assertIn("ruff check src tests", workflow)
        self.assertIn("mypy", workflow)
        self.assertIn("python -m build", workflow)
        pyproject = (ROOT / "pyproject.toml").read_text()
        self.assertIn("fail_under = 80", pyproject)
        self.assertIn("branch = true", pyproject)
        self.assertIn("check_untyped_defs = true", pyproject)
        self.assertNotIn("ignore_errors", pyproject)
        for pin in ("ruff==", "mypy==", "coverage[toml]==", "build=="):
            self.assertIn(pin, pyproject)


if __name__ == "__main__":
    unittest.main()
