"""Real sync script, disposable homes and no provider/session calls."""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/sync-agents.sh"


class SharedAgentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.home.mkdir()

    def destination(self, provider: str) -> Path:
        return self.home / (".claude" if provider == "claude" else ".codex") / "agents"

    def run_sync(self, provider: str, *args: str, cwd: Path | None = None, dest: Path | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(SCRIPT), "--provider", provider, "--dest", str(dest or self.destination(provider)), *args],
            cwd=cwd or self.root, env={"PATH": "/usr/bin:/bin", "HOME": str(self.home), "LC_ALL": "C"},
            capture_output=True, text=True, timeout=15, check=False,
        )

    def source_copy(self, provider: str) -> Path:
        src = self.root / (provider + "-source")
        shutil.copytree(ROOT / "agents" / provider, src)
        return src

    def assert_parity(self, provider: str) -> None:
        for src in (ROOT / "agents" / provider).iterdir():
            self.assertEqual((self.destination(provider) / src.name).read_bytes(), src.read_bytes())

    def test_default_sources_are_independent_of_two_project_checkouts(self) -> None:
        for name in ("application checkout", "supervisor checkout"):
            checkout = self.root / name
            subprocess.run(["git", "init", "--quiet", str(checkout)], check=True, capture_output=True)
            for provider in ("claude", "codex"):
                result = self.run_sync(provider, cwd=checkout)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assert_parity(provider)
            self.assertEqual(sorted(p.name for p in checkout.iterdir()), [".git"], "setup must not materialize project-local state")

    def test_claude_default_destination_is_the_shared_user_directory(self) -> None:
        result = subprocess.run(["bash", str(SCRIPT)], cwd=self.root, env={"PATH": "/usr/bin:/bin", "HOME": str(self.home)}, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_parity("claude")

    def test_codex_requires_an_explicit_destination(self) -> None:
        result = subprocess.run(["bash", str(SCRIPT), "--provider", "codex"], cwd=self.root, env={"PATH": "/usr/bin:/bin", "HOME": str(self.home)}, capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.destination("codex").exists())

    def test_dry_run_never_creates_even_destination_parents(self) -> None:
        for provider in ("claude", "codex"):
            result = self.run_sync(provider, "--dry-run")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("would INSTALL", result.stdout)
            self.assertFalse(self.destination(provider).parent.exists())

    def test_repeat_is_idempotent_and_preserves_unrelated_roles_and_orphans(self) -> None:
        for provider, orphan in (("claude", "repository-old.md"), ("codex", "repository_old.toml")):
            self.assertEqual(self.run_sync(provider).returncode, 0)
            dest = self.destination(provider)
            (dest / "another-role.txt").write_text("keep")
            (dest / orphan).write_text("preserve orphan")
            before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in dest.iterdir()}
            result = self.run_sync(provider)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("changes=0", result.stdout)
            self.assertIn("ORPHAN", result.stdout)
            self.assertEqual(before, {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in dest.iterdir()})

    def test_conflict_requires_update_and_dry_update_still_writes_nothing(self) -> None:
        for provider in ("claude", "codex"):
            dest = self.destination(provider)
            dest.mkdir(parents=True)
            first = next((ROOT / "agents" / provider).iterdir())
            target = dest / first.name
            target.write_text("previous installed content")
            result = self.run_sync(provider)
            self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
            self.assertEqual(list(dest.iterdir()), [target], "a conflict must not install other files")
            self.assertEqual(self.run_sync(provider, "--update", "--dry-run").returncode, 0)
            self.assertEqual(target.read_text(), "previous installed content")
            self.assertEqual(self.run_sync(provider, "--update").returncode, 0)
            self.assert_parity(provider)

    def test_invalid_metadata_refuses_the_entire_inventory(self) -> None:
        for provider in ("claude", "codex"):
            src = self.source_copy(provider)
            name = "repository-invalid.md" if provider == "claude" else "repository_explorer.toml"
            (src / name).write_text("---\nname: wrong-name\n---\n" if provider == "claude" else 'name="wrong_name"\n')
            result = self.run_sync(provider, "--src", str(src))
            self.assertEqual(result.returncode, 4, result.stdout + result.stderr)
            self.assertFalse(self.destination(provider).exists())

    def test_codex_real_toml_semantics_and_inventory_are_enforced(self) -> None:
        src = self.source_copy("codex")
        role = src / "repository_explorer.toml"
        for bad in ('name="repository_explorer"\nname="repository_explorer"\n',
                    'name="repository_explorer\\n"\n', 'name=true\n', 'not toml'):
            role.write_text(bad)
            self.assertEqual(self.run_sync("codex", "--src", str(src)).returncode, 4)
            self.assertFalse(self.destination("codex").exists())
        role.write_bytes((ROOT / "agents/codex/repository_explorer.toml").read_bytes())
        (src / "repository_review_lens.toml").write_text('name="repository_review_lens"\n')
        self.assertEqual(self.run_sync("codex", "--src", str(src)).returncode, 4)
        self.assertFalse(self.destination("codex").exists())

    def test_source_symlinks_refuse_for_both_providers(self) -> None:
        for provider in ("claude", "codex"):
            src = self.source_copy(provider)
            role = next(src.iterdir())
            original = self.root / (provider + "-original")
            role.rename(original)
            role.symlink_to(original)
            self.assertEqual(self.run_sync(provider, "--src", str(src)).returncode, 4)
            self.assertFalse(self.destination(provider).exists())

    def test_destination_symlinks_never_overwrite_the_referent_even_with_update(self) -> None:
        for provider in ("claude", "codex"):
            dest = self.destination(provider)
            dest.mkdir(parents=True)
            referent = self.root / (provider + "-referent")
            referent.write_text("do not touch")
            first = next((ROOT / "agents" / provider).iterdir())
            target = dest / first.name
            target.symlink_to(referent)
            result = self.run_sync(provider, "--update")
            self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
            self.assertTrue(target.is_symlink())
            self.assertEqual(referent.read_text(), "do not touch")

    def test_linked_ancestry_and_dot_dot_refuse_without_materializing_destinations(self) -> None:
        real = self.root / "real"
        real.mkdir()
        link = self.root / "linked"
        link.symlink_to(real, target_is_directory=True)
        for provider in ("claude", "codex"):
            result = self.run_sync(provider, dest=link / provider / "agents")
            self.assertEqual(result.returncode, 4)
            self.assertFalse((real / provider).exists())
            result = self.run_sync(provider, dest=real / ".." / "escape")
            self.assertEqual(result.returncode, 2)
            self.assertFalse((self.root / "escape").exists())

    def test_nonregular_target_refuses_without_blocking_or_overwriting(self) -> None:
        for provider in ("claude", "codex"):
            dest = self.destination(provider)
            dest.mkdir(parents=True)
            first = next((ROOT / "agents" / provider).iterdir())
            target = dest / first.name
            os.mkfifo(target)
            self.assertEqual(self.run_sync(provider, "--update").returncode, 3)
            self.assertTrue(target.is_fifo())

    def test_names_models_and_checkout_contract_are_stable_and_repository_neutral(self) -> None:
        role = tomllib.loads((ROOT / "agents/codex/repository_explorer.toml").read_text())
        self.assertEqual((role["name"], role["model"], role["model_reasoning_effort"]), ("repository_explorer", "gpt-6-luna", "high"))
        models = {"repository-tracer": "haiku", "repository-test-runner": "haiku", "repository-diff-auditor": "sonnet"}
        for path in (ROOT / "agents/claude").glob("*.md"):
            text = path.read_text()
            self.assertIn("name: " + path.stem, text)
            self.assertIn("model: " + models[path.stem], text)
            self.assertIn("CHECKOUT=<absolute path>", text)
        for path in (ROOT / "agents").rglob("*"):
            if path.is_file():
                self.assertNotIn("/home/", path.read_text())
        self.assertIn("context files explicitly supplied by the parent", role["developer_instructions"])
