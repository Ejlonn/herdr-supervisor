"""Quota dependency lock, read-only check and bounded bootstrap: pinned acquisition, verified local fallback,
moved or drifted sources, archive containment, preserved installations, and no activation. Local Git
repositories and fake commands only: no network, no Herdr server, no provider, no credentials."""

from __future__ import annotations

import contextlib
import copy
import gzip
import hashlib
import io
import json
import os
import re
import subprocess
import tarfile
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import herdr_quota_dependency as q  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "quota-dependency"
GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
    "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z", "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
}
PLUGIN_FILES = {
    "herdr-plugin.toml": 'id = "herdr-agent-quota"\nname = "Fixture"\nversion = "1.5.4"\nmin_herdr_version = "0.9.0"\n',
    "Cargo.toml": '[package]\nname = "herdr-agent-quota"\nversion = "1.5.4"\nedition = "2021"\n',
    "Cargo.lock": "# fixture lock\nversion = 3\n",
    "rust-toolchain.toml": '[toolchain]\nchannel = "1.95.0"\n',
    "LICENSE": "MIT License\n\nCopyright (c) 2026 Fixture Author\n",
    "AGENTS.md": "agents\n",
    "src/main.rs": "fn main() {}\n",
    "src/deep/mod.rs": "// nested\n",
    "scripts/run.sh": "#!/bin/sh\nexit 0\n",
}


def git(*args: str, cwd: Path, input_bytes: bytes | None = None) -> bytes:
    done = subprocess.run(["git", *args], cwd=cwd, env={**os.environ, **GIT_ENV}, input=input_bytes, capture_output=True, check=True)
    return done.stdout


def write_tree(root: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    (root / "scripts/run.sh").chmod(0o755)
    if not (root / "CLAUDE.md").is_symlink():
        (root / "CLAUDE.md").symlink_to("AGENTS.md")


def tar_bytes(members: list[dict[str, Any]], *, comment: str | None = None) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT, pax_headers={"comment": comment} if comment else None) as tar:
        for member in members:
            info = tarfile.TarInfo(member["name"])
            info.type = member.get("type", tarfile.REGTYPE)
            info.mode = member.get("mode", 0o644)
            data = member.get("data", b"")
            info.size = len(data) if info.type == tarfile.REGTYPE else 0
            if "linkname" in member:
                info.linkname = member["linkname"]
            tar.addfile(info, io.BytesIO(data) if info.type == tarfile.REGTYPE else None)
    return buffer.getvalue()


class Env:
    """A fixture repository shaped like the locked plugin, its lock, recovery artifacts and fake commands."""

    def __init__(self, test: unittest.TestCase) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="quota-dep-test-"))
        test.addCleanup(self.cleanup)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.repo = self.tmp / "plugin"
        self.repo.mkdir()
        write_tree(self.repo, PLUGIN_FILES)
        git("init", "-q", cwd=self.repo)
        git("config", "uploadpack.allowAnySHA1InWant", "true", cwd=self.repo)
        git("add", "-A", cwd=self.repo)
        git("commit", "-q", "-m", "fixture", cwd=self.repo)
        self.revision = git("rev-parse", "HEAD", cwd=self.repo).decode().strip()
        self.git_tree = git("rev-parse", "HEAD^{tree}", cwd=self.repo).decode().strip()
        self.archive = git("archive", "--format=tar", self.revision, cwd=self.repo)
        self.recovery = self.tmp / "recovery"
        self.recovery.mkdir()
        self.archive_path = self.recovery / "fixture-source.tar.gz"
        self.archive_path.write_bytes(gzip.compress(self.archive, mtime=0))
        self.bundle_path = self.recovery / "fixture.bundle"
        self.bundle_path.write_bytes(self.make_bundle())
        self.lock = self.make_lock()
        self.lock_path = self.tmp / "lock.json"
        self.write_lock(self.lock)
        self.fake_bin = self.tmp / "fake-bin"
        self.fake_bin.mkdir()
        self.log = self.tmp / "calls.log"
        self.inventory = self.tmp / "inventory.json"  # read by the fake herdr beside the fake-bin directory
        self.inventory.write_text((FIXTURES / "plugin-list-empty-0.9.5.json").read_text())
        self.install_fakes()
        self.stage = self.tmp / "stage"
        self.quota_dir = self.tmp / "quota"

    def cleanup(self) -> None:
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make_bundle(self) -> bytes:
        """A parent-less single-commit bundle, like the retained recovery bundle (objects of one commit only)."""
        objects = git("rev-list", "--objects", "--no-walk", self.revision, cwd=self.repo).decode().split("\n")
        names = "\n".join(line.split(" ", 1)[0] for line in objects if line) + "\n"
        pack = git("pack-objects", "--stdout", "-q", cwd=self.repo, input_bytes=names.encode())
        return f"# v2 git bundle\n{self.revision} HEAD\n\n".encode() + pack

    def make_lock(self) -> dict[str, Any]:
        with tarfile.open(self.archive_path) as tar, tempfile.TemporaryDirectory() as scratch:
            entries = q.extract_archive(tar, Path(scratch), {"CLAUDE.md": "AGENTS.md"})
        by = {e.path: e.sha256 for e in entries}
        return {
            "schema_version": 1, "dependency": "herdr-agent-quota", "plugin_id": "herdr-agent-quota",
            "repository": {"url": "https://github.com/example/herdr-agent-quota"},
            "upstream": {"url": "https://github.com/example/upstream", "relocated_from": "https://github.com/example/old"},
            "revision": self.revision, "tree_sha1": q.git_tree_sha1(entries), "manifest_version": "1.5.4", "min_herdr_version": "0.9.0",
            "license": {"spdx": "MIT", "file": "LICENSE", "sha256": by["LICENSE"], "copyright": "Copyright (c) 2026 Fixture Author"},
            "toolchain": {"channel": "1.95.0", "build_command": ["cargo", "build", "--release", "--locked"]},
            "critical_files": {n: by[n] for n in ("Cargo.lock", "Cargo.toml", "LICENSE", "herdr-plugin.toml", "rust-toolchain.toml")},
            "content_manifest_sha256": q.content_manifest_sha256(entries), "allowed_symlinks": {"CLAUDE.md": "AGENTS.md"},
            "recovery_artifacts": [
                {"kind": "source-archive", "file": self.archive_path.name, "sha256": q.sha256_file(self.archive_path), "note": "fixture"},
                {"kind": "git-bundle", "file": self.bundle_path.name, "sha256": q.sha256_file(self.bundle_path), "note": "fixture"},
            ],
            "provenance": {},
        }

    def write_lock(self, lock: dict[str, Any]) -> None:
        self.lock_path.write_text(json.dumps(lock))

    def add_artifact(self, kind: str, path: Path) -> None:
        self.lock["recovery_artifacts"].append({"kind": kind, "file": path.name, "sha256": q.sha256_file(path), "note": "fixture"})
        self.write_lock(self.lock)

    def install_fakes(self) -> None:
        # The build runs with a scrubbed environment, so the fakes take their log and switches from files beside them.
        # Like rustup, they select the toolchain from RUSTUP_TOOLCHAIN (an unset variable yields a release that is never the locked one).
        guard = ('D=$(dirname "$0")\n'
                 'if [ -e "$D/rustup-list" ] && ! grep -q "^${RUSTUP_TOOLCHAIN:-none}" "$D/rustup-list"; then echo "error: toolchain \'${RUSTUP_TOOLCHAIN:-none}\' is not installed" >&2; exit 1; fi\n')
        release = '$(cat "$D/%s-release" 2>/dev/null || echo "${RUSTUP_TOOLCHAIN:-99.0.0}")'
        staged = 'case "$PWD" in */herdr-agent-quota-*) [ -e "$D/staged-release" ] && REL=$(cat "$D/staged-release");; esac\n'  # a release that differs only inside the staged source
        scripts = {
            "herdr": '#!/bin/sh\nD=$(dirname "$0")\necho "herdr $*" >> "$D/../calls.log"\n[ "$1 $2 $3" = "plugin list --json" ] && { cat "$D/../inventory.json"; exit "$(cat "$D/inventory-exit" 2>/dev/null || echo 0)"; }\nexit 99\n',
            "cargo": '#!/bin/sh\n' + guard +
                     'echo "cargo $* cwd=$(basename "$PWD") rt=${RUSTUP_TOOLCHAIN:-unset} rustc=${RUSTC:-unset} flags=${RUSTFLAGS:-unset} wrap=${RUSTC_WRAPPER:-unset} tdir=${CARGO_TARGET_DIR:-unset} token=${CARGO_REGISTRY_TOKEN:-unset} gh=${GITHUB_TOKEN:-unset} auto=${RUSTUP_AUTO_INSTALL:-unset} home=${HOME:+set}" >> "$D/../calls.log"\n'
                     'REL=' + release % "cargo" + '\n' + staged + '[ "$1" = "-vV" ] && { printf "cargo %s (fake)\\nrelease: %s\\n" "$REL" "$REL"; exit 0; }\n'
                     '[ -e "$D/fail-cargo" ] && { echo "error: could not compile (fake)" >&2; exit 101; }\n'
                     'mkdir -p target/release && printf "binary" > target/release/herdr-agent-quota\n'
                     'INFO=$(cat "$D/info-release" 2>/dev/null || echo "${RUSTUP_TOOLCHAIN:-99.0.0}")\n'
                     '[ -e "$D/no-info" ] || printf \'{"rustc_fingerprint":1,"outputs":{"a":{"success":true,"status":"","code":0,"stdout":"rustc %s (fake)\\\\nbinary: rustc\\\\nrelease: %s\\\\n","stderr":""},"b":{"success":true,"stdout":"lib.rlib\\\\n","stderr":""}}}\' "$INFO" "$INFO" > target/.rustc_info.json\n'
                     'exit 0\n',
            "rustup": '#!/bin/sh\nD=$(dirname "$0")\n[ -e "$D/rustup-list" ] && cat "$D/rustup-list" || echo "1.95.0-x86_64-unknown-linux-gnu (default)"\n',
            "rustc": '#!/bin/sh\n' + guard + 'REL=' + release % "rustc" + '\n' + staged + 'printf "rustc %s (fake)\\nbinary: rustc\\nrelease: %s\\n" "$REL" "$REL"\n',
        }
        for name, body in scripts.items():
            path = self.fake_bin / name
            path.write_text(body)
            path.chmod(0o755)

    def switch(self, name: str, content: str = "1") -> None:
        (self.fake_bin / name).write_text(content)

    def environ(self, **extra: str) -> dict[str, str]:
        base = {"PATH": f"{self.fake_bin}:/usr/bin:/bin", "HOME": str(self.home)}
        base.update(extra)
        return base

    def run(self, *argv: str, **extra: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, self.environ(**extra), clear=True), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = q.main(["--lock", str(self.lock_path), *argv])
        return code, out.getvalue(), err.getvalue()

    def run_json(self, *argv: str, **extra: str) -> tuple[int, dict[str, Any]]:
        code, out, err = self.run("--json", *argv, **extra)
        return code, json.loads(out) if out.lstrip().startswith("{") else {"stderr": err, "stdout": out}

    def calls(self) -> list[str]:
        return self.log.read_text().splitlines() if self.log.exists() else []

    def set_inventory(self, fixture: str | None = None, mutate: Any = None) -> None:
        data = json.loads((FIXTURES / (fixture or "plugin-list-installed-quota-0.9.5.json")).read_text())
        if mutate:
            mutate(data["result"]["plugins"])
        self.inventory.write_text(json.dumps(data))

    def stage_is_untouched(self) -> bool:
        return not self.stage.exists() or not any(self.stage.iterdir())


class LockTests(unittest.TestCase):
    def test_shipped_lock_is_valid_exact_and_never_floats(self) -> None:
        lock = q.load_lock(str(ROOT / "config/quota-dependency.json"))
        self.assertEqual(lock["revision"], "809749e585480047e502df072fb8ddccc4719d4e")
        self.assertEqual(lock["manifest_version"], "1.5.4")
        self.assertEqual(lock["plugin_id"], "herdr-agent-quota")
        self.assertEqual(lock["repository"]["url"], "https://github.com/Ejlonn/herdr-agent-quota")
        self.assertEqual(lock["license"]["spdx"], "MIT")
        self.assertEqual(lock["toolchain"]["build_command"], ["cargo", "build", "--release", "--locked"])
        self.assertEqual(lock["recovery_artifacts"][0]["sha256"], "0f43db667d95b5ac79392a2bf561fad0fa6c6bb6cfd6da39e9191663a945aa00")
        self.assertEqual(lock["recovery_artifacts"][1]["sha256"], "6761d16b8546ba0c6353b943961ee71e9267d3b2076d21765bf9383fe0dfdba7")

    def test_malformed_or_floating_locks_are_refused(self) -> None:
        good = json.loads((ROOT / "config/quota-dependency.json").read_text())
        mutations: dict[str, Any] = {
            "branch revision": lambda d: d.update(revision="main"),
            "short revision": lambda d: d.update(revision=d["revision"][:12]),
            "uppercase revision": lambda d: d.update(revision=d["revision"].upper()),
            "ssh repository": lambda d: d["repository"].update(url="ssh://git@github.com/x/y"),
            "unknown key": lambda d: d.update(extra=1),
            "missing key": lambda d: d.pop("tree_sha1"),
            "bool schema": lambda d: d.update(schema_version=True),
            "unlocked build": lambda d: d["toolchain"].update(build_command=["cargo", "build", "--release"]),
            "floating channel": lambda d: d["toolchain"].update(channel="stable"),
            "missing cargo lock hash": lambda d: d["critical_files"].pop("Cargo.lock"),
            "license hash drift": lambda d: d["license"].update(sha256="0" * 64),
            "traversal symlink": lambda d: d["allowed_symlinks"].update({"x": "../etc/passwd"}),
            "no artifacts": lambda d: d.update(recovery_artifacts=[]),
            "bad artifact hash": lambda d: d["recovery_artifacts"][0].update(sha256="xyz"),
        }
        for label, mutate in mutations.items():
            with self.subTest(label), tempfile.TemporaryDirectory() as tmp:
                lock = copy.deepcopy(good)
                mutate(lock)
                path = Path(tmp) / "lock.json"
                path.write_text(json.dumps(lock))
                with self.assertRaises(q.DependencyError) as caught:
                    q.load_lock(str(path))
                self.assertEqual(caught.exception.code, q.EXIT_REFUSED)

    def test_tree_hash_matches_git_for_nested_dirs_modes_and_symlinks(self) -> None:
        env = Env(self)
        self.assertEqual(env.lock["tree_sha1"], env.git_tree, "the in-process tree id must equal git's own tree object")

    def test_missing_lock_is_an_action_not_a_traceback(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True), self.assertRaises(q.DependencyError) as caught:
            q.load_lock("/nonexistent/quota-dependency.json")
        self.assertEqual(caught.exception.code, q.EXIT_ACTION)


class CheckTests(unittest.TestCase):
    def test_absent_dependency_is_action_required_and_creates_nothing(self) -> None:
        env = Env(self)
        before = sorted(p.name for p in env.home.rglob("*"))
        code, report = env.run_json("check", "--quota-dir", str(env.quota_dir))
        self.assertEqual(code, q.EXIT_ACTION)
        self.assertEqual(report["dependency"]["state"], "absent")
        self.assertEqual(report["mutations"], "none")
        self.assertEqual(sorted(p.name for p in env.home.rglob("*")), before)
        self.assertFalse(env.quota_dir.exists())
        self.assertEqual([c for c in env.calls() if c.startswith("herdr")], ["herdr plugin list --json"], "only the read-only inventory may be invoked")

    def test_pinned_installation_with_old_origin_is_preserved(self) -> None:
        env = Env(self)
        env.set_inventory()
        env.lock["revision"] = json.loads((FIXTURES / "plugin-list-installed-quota-0.9.5.json").read_text())["result"]["plugins"][0]["source"]["resolved_commit"]
        env.write_lock(env.lock)
        code, report = env.run_json("check", "--quota-dir", str(env.quota_dir))
        self.assertEqual(code, q.EXIT_OK, report)
        self.assertEqual(report["dependency"]["state"], "pinned")
        self.assertIn("origin", report["dependency"]["detail"])
        self.assertIn("preserved", report["dependency"]["detail"])

    def test_conflicts_are_refused_never_overwritten(self) -> None:
        env = Env(self)
        installed = json.loads((FIXTURES / "plugin-list-installed-quota-0.9.5.json").read_text())["result"]["plugins"][0]
        env.lock["revision"] = installed["source"]["resolved_commit"]
        env.write_lock(env.lock)
        cases = {
            "other revision": lambda plugins: plugins[0]["source"].update(resolved_commit="b" * 40),
            "other version": lambda plugins: plugins[0].update(version="1.6.0"),
            "duplicate id": lambda plugins: plugins.append(copy.deepcopy(plugins[0])),
            "revision unknown": lambda plugins: plugins[0]["source"].pop("resolved_commit"),
            "local link unverifiable": lambda plugins: plugins[0].update(source={"kind": "local"}, plugin_root=str(env.tmp / "nowhere")),
        }
        for label, mutate in cases.items():
            with self.subTest(label):
                env.set_inventory(mutate=mutate)
                code, report = env.run_json("check", "--quota-dir", str(env.quota_dir))
                self.assertEqual(code, q.EXIT_REFUSED, report)
                self.assertEqual(report["dependency"]["state"], "conflict")
                self.assertTrue(report["dependency"]["action"])
        self.assertTrue(all(call == "herdr plugin list --json" for call in env.calls() if call.startswith("herdr")))

    def test_local_link_at_locked_clean_revision_is_pinned_and_dirty_is_not(self) -> None:
        env = Env(self)
        env.set_inventory(mutate=lambda plugins: plugins[0].update(source={"kind": "local"}, plugin_root=str(env.repo), version="1.5.4"))
        index = env.repo / ".git" / "index"
        before = (index.read_bytes(), index.stat().st_mtime_ns)
        code, report = env.run_json("check", "--quota-dir", str(env.quota_dir))
        self.assertEqual((code, report["dependency"]["state"]), (q.EXIT_OK, "pinned"))
        (env.repo / "src/main.rs").write_text("fn main() { /* edited */ }\n")
        stale = (index.read_bytes(), index.stat().st_mtime_ns)
        self.assertEqual(stale, before, "the read-only check must not rewrite the plugin checkout's git index")
        code, report = env.run_json("check", "--quota-dir", str(env.quota_dir))
        self.assertEqual((code, report["dependency"]["state"]), (q.EXIT_REFUSED, "conflict"))
        self.assertEqual((index.read_bytes(), index.stat().st_mtime_ns), before, "not even when the checkout is dirty")

    def test_inspecting_a_local_link_never_runs_programs_from_its_git_config(self) -> None:
        env = Env(self)
        sentinel = env.tmp / "fsmonitor-ran"
        hook = env.tmp / "fsmonitor.sh"
        hook.write_text(f"#!/bin/sh\ntouch '{sentinel}'\nprintf '\\0'\n")
        hook.chmod(0o755)
        git("config", "core.fsmonitor", str(hook), cwd=env.repo)
        env.set_inventory(mutate=lambda plugins: plugins[0].update(source={"kind": "local"}, plugin_root=str(env.repo), version="1.5.4"))
        code, report = env.run_json("check", "--quota-dir", str(env.quota_dir))
        self.assertEqual((code, report["dependency"]["state"]), (q.EXIT_OK, "pinned"))
        self.assertFalse(sentinel.exists(), "the plugin checkout's configured fsmonitor program must not be executed")

    def test_unreadable_inventory_is_unknown_not_absent(self) -> None:
        env = Env(self)
        for label in ("exit failure", "not json"):
            with self.subTest(label):
                if label == "exit failure":
                    env.switch("inventory-exit", "3")
                else:
                    env.switch("inventory-exit", "0")
                    env.inventory.write_text("not json")
                code, report = env.run_json("check", "--quota-dir", str(env.quota_dir))
                self.assertEqual(code, q.EXIT_ACTION)
                self.assertEqual(report["dependency"]["state"], "unknown")
        env.inventory.write_text(json.dumps({"id": "x", "result": {"plugins": "nope"}}))
        code, report = env.run_json("check", "--quota-dir", str(env.quota_dir))
        self.assertEqual((code, report["dependency"]["state"]), (q.EXIT_ACTION, "unknown"))

    def test_missing_herdr_is_unknown_with_an_install_action(self) -> None:
        env = Env(self)
        (env.fake_bin / "herdr").unlink()
        code, report = env.run_json("check", "--quota-dir", str(env.quota_dir))
        self.assertEqual(code, q.EXIT_ACTION)
        self.assertFalse(report["herdr"]["available"])
        self.assertIn("install Herdr", report["dependency"]["action"])

    def test_telemetry_states_are_unknown_never_zero(self) -> None:
        env = Env(self)
        env.set_inventory(mutate=lambda plugins: plugins[0]["source"].update(resolved_commit=env.revision))
        env.quota_dir.mkdir()
        fresh, stale = env.quota_dir / "claude-statusline.json", env.quota_dir / "codex-app-server.json"
        fresh.write_text('{"snapshot": {}}')
        stale.write_text('{"snapshot": {}}')
        old = time.time() - 5000
        os.utime(stale, (old, old))
        code, report = env.run_json("check", "--quota-dir", str(env.quota_dir))
        self.assertEqual(code, q.EXIT_OK)
        providers = report["telemetry"]["providers"]
        self.assertEqual(providers["claude"]["state"], "FRESH")
        self.assertEqual((providers["codex"]["state"], providers["codex"]["usage"]), ("STALE", "UNKNOWN"))
        self.assertNotIn("0%", json.dumps(report["telemetry"]), "no usage figure is ever fabricated")
        code, _ = env.run_json("check", "--quota-dir", str(env.quota_dir), "--require-telemetry")
        self.assertEqual(code, q.EXIT_ACTION)
        stale.write_text("{broken")
        _, report = env.run_json("check", "--quota-dir", str(env.quota_dir))
        self.assertEqual(report["telemetry"]["providers"]["codex"]["state"], "UNREADABLE")
        stale.unlink()
        _, report = env.run_json("check", "--quota-dir", str(env.quota_dir))
        self.assertEqual((report["telemetry"]["providers"]["codex"]["state"], report["telemetry"]["providers"]["codex"]["usage"]), ("MISSING", "UNKNOWN"))
        self.assertIn("not evaluated here", report["telemetry"]["identity"])
        self.assertIn("never borrowed", report["telemetry"]["identity"])

    def test_toolchain_prerequisite_is_reported_without_installing(self) -> None:
        env = Env(self)
        env.switch("rustup-list", "stable-x86_64-unknown-linux-gnu (default)\n")
        _, report = env.run_json("check", "--quota-dir", str(env.quota_dir))
        self.assertEqual(report["toolchain"]["status"], "MISSING")
        self.assertIn("rustup toolchain install 1.95.0", report["toolchain"]["action"])
        self.assertFalse(any("install" in call for call in env.calls() if call.startswith("cargo")))


class BootstrapTests(unittest.TestCase):
    def apply_args(self, env: Env, *extra: str) -> tuple[str, ...]:
        return ("bootstrap", "--apply", "--stage-dir", str(env.stage), *extra)

    def test_dry_run_with_local_artifact_verifies_and_mutates_nothing(self) -> None:
        env = Env(self)
        code, report = env.run_json("bootstrap", "--dry-run", "--stage-dir", str(env.stage), "--from-archive", str(env.archive_path))
        self.assertEqual(code, q.EXIT_OK, report)
        self.assertEqual(report["mode"], "dry-run")
        self.assertEqual(report["source"]["tree_sha1"], env.git_tree)
        self.assertFalse(env.stage.exists())
        self.assertEqual([c for c in env.calls() if c.startswith("herdr")], ["herdr plugin list --json"])
        self.assertEqual(sorted(p.name for p in env.home.iterdir()), [])
        code, report = env.run_json("bootstrap", "--stage-dir", str(env.stage), "--from-bundle", str(env.bundle_path))
        self.assertEqual((code, report["mode"]), (q.EXIT_OK, "dry-run"), "dry run is the default")

    def test_dry_run_of_the_repository_route_makes_no_network_or_fetch_attempt(self) -> None:
        env = Env(self)
        code, report = env.run_json("bootstrap", "--dry-run", "--stage-dir", str(env.stage), "--fork-url", "/nonexistent/repository")
        self.assertEqual(code, q.EXIT_OK, report)
        self.assertTrue(any("not contacted" in a for a in report["attempts"]))
        self.assertFalse(env.stage.exists())

    def test_installed_dependency_is_preserved_and_nothing_is_staged(self) -> None:
        env = Env(self)
        env.set_inventory(mutate=lambda plugins: plugins[0]["source"].update(resolved_commit=env.revision))
        code, report = env.run_json(*self.apply_args(env, "--from-archive", str(env.archive_path)))
        self.assertEqual(code, q.EXIT_OK, report)
        self.assertIn("nothing to do", report["result"])
        self.assertFalse(env.stage.exists())
        env.set_inventory(mutate=lambda plugins: plugins[0]["source"].update(resolved_commit="c" * 40))
        code, report = env.run_json(*self.apply_args(env, "--from-archive", str(env.archive_path)))
        self.assertEqual(code, q.EXIT_REFUSED)
        self.assertFalse(env.stage.exists())
        self.assertEqual({c for c in env.calls() if c.startswith("herdr")}, {"herdr plugin list --json"})

    def test_apply_stages_verifies_builds_locked_and_activates_nothing(self) -> None:
        env = Env(self)
        secrets = {"CARGO_REGISTRY_TOKEN": "tok-should-not-leak", "GITHUB_TOKEN": "gh-should-not-leak"}
        code, report = env.run_json(*self.apply_args(env, "--from-archive", str(env.archive_path)), **secrets)
        self.assertEqual(code, q.EXIT_OK, report)
        final = env.stage / f"herdr-agent-quota-{env.revision[:12]}"
        self.assertTrue((final / "target/release/herdr-agent-quota").is_file())
        self.assertTrue((final / "CLAUDE.md").is_symlink())
        self.assertEqual(os.readlink(final / "CLAUDE.md"), "AGENTS.md")
        self.assertEqual((final / "scripts/run.sh").stat().st_mode & 0o777, 0o755)
        self.assertEqual((final / "src/main.rs").stat().st_mode & 0o777, 0o644)
        receipt = json.loads((env.stage / "BOOTSTRAP_RECEIPT.json").read_text())
        self.assertEqual(receipt["revision"], env.revision)
        self.assertEqual(receipt["build"]["command"], ["cargo", "build", "--release", "--locked"])
        self.assertEqual(receipt["build"]["binary_sha256"], hashlib.sha256(b"binary").hexdigest())
        cargo_calls = [c for c in env.calls() if c.startswith("cargo build")]
        self.assertEqual(len(cargo_calls), 1)
        self.assertIn("cargo build --release --locked cwd=herdr-agent-quota-", cargo_calls[0])
        self.assertIn("token=unset gh=unset auto=0", cargo_calls[0], "credential variables are scrubbed and toolchain auto-install is off")
        self.assertEqual([c for c in env.calls() if c.startswith("herdr")], ["herdr plugin list --json"], "no install, link, enable or configure is ever invoked")
        self.assertEqual(sorted(p.name for p in env.home.iterdir()), [])
        self.assertFalse([p for p in env.stage.iterdir() if p.name.startswith(".partial-")])
        self.assertTrue(any("herdr plugin link --disabled" in line for line in report["next_steps"]))

    def test_offline_flag_and_no_build(self) -> None:
        env = Env(self)
        code, _ = env.run_json(*self.apply_args(env, "--from-archive", str(env.archive_path), "--offline"))
        self.assertEqual(code, q.EXIT_OK)
        self.assertTrue(any(c.startswith("cargo build --release --locked --offline") for c in env.calls()))
        other = Env(self)
        code, report = other.run_json(*self.apply_args(other, "--from-bundle", str(other.bundle_path), "--no-build"))
        self.assertEqual(code, q.EXIT_OK, report)
        self.assertFalse(any(c.startswith("cargo build") for c in other.calls()))
        self.assertEqual(json.loads((other.stage / "BOOTSTRAP_RECEIPT.json").read_text())["build"], "skipped")

    def test_pinned_fork_fetch_by_exact_commit(self) -> None:
        env = Env(self)
        git("checkout", "-q", "-b", "moved", cwd=env.repo)
        (env.repo / "src/main.rs").write_text("fn main() { /* a later commit */ }\n")
        git("commit", "-q", "-am", "later", cwd=env.repo)
        code, report = env.run_json(*self.apply_args(env, "--fork-url", str(env.repo), "--no-build"))
        self.assertEqual(code, q.EXIT_OK, report)
        self.assertEqual(report["source"]["used"], "fork")
        self.assertEqual((env.stage / f"herdr-agent-quota-{env.revision[:12]}/src/main.rs").read_text(), "fn main() {}\n", "the exact locked commit is staged, not a moved branch tip")

    def test_a_remote_answering_with_another_commit_is_refused(self) -> None:
        def fake_run(argv: list[str], **_: Any) -> subprocess.CompletedProcess[bytes]:
            return subprocess.CompletedProcess(argv, 0, stdout=(b"e" * 40 + b"\n") if "rev-parse" in argv else b"", stderr=b"")

        with mock.patch.object(q, "run", fake_run), tempfile.TemporaryDirectory() as work, self.assertRaises(q.DependencyError) as caught:
            q.fetch_pinned_commit("/unused", "a" * 40, Path(work), 5)
        self.assertEqual(caught.exception.code, q.EXIT_REFUSED)
        self.assertIn("different commit", str(caught.exception))

    def test_repository_without_the_commit_stops_and_never_follows_a_branch(self) -> None:
        env = Env(self)
        other = env.tmp / "other-repo"
        other.mkdir()
        write_tree(other, {**PLUGIN_FILES, "src/main.rs": "fn main() { /* different */ }\n"})
        git("init", "-q", cwd=other)
        git("config", "uploadpack.allowAnySHA1InWant", "true", cwd=other)
        git("add", "-A", cwd=other)
        git("commit", "-q", "-m", "other", cwd=other)
        code, report = env.run_json(*self.apply_args(env, "--fork-url", str(other)))
        self.assertEqual(code, q.EXIT_ACTION, report)
        self.assertIn("nothing was staged", report["result"])
        self.assertTrue(env.stage_is_untouched())
        self.assertIn("--from-archive", report["action"])
        self.assertFalse(any(c.startswith("cargo build") for c in env.calls()))

    def test_upstream_unavailable_uses_verified_local_fallbacks(self) -> None:
        for kind, option, attr in (("archive", "--fallback-archive", "archive_path"), ("bundle", "--fallback-bundle", "bundle_path")):
            with self.subTest(kind):
                env = Env(self)
                code, report = env.run_json(*self.apply_args(env, "--fork-url", "/nonexistent/repo", option, str(getattr(env, attr)), "--no-build"))
                self.assertEqual(code, q.EXIT_OK, report)
                self.assertEqual(report["source"]["used"], kind)
                self.assertEqual(len(report["attempts"]), 2)
                self.assertIn("cannot fetch", report["attempts"][0])
                self.assertEqual(report["source"]["tree_sha1"], env.git_tree)

    def test_unlisted_or_corrupt_artifacts_are_refused_before_interpretation(self) -> None:
        env = Env(self)
        corrupt = env.recovery / "corrupt.tar.gz"
        corrupt.write_bytes(env.archive_path.read_bytes() + b"x")
        stranger = env.recovery / "stranger.bundle"
        stranger.write_bytes(env.bundle_path.read_bytes().replace(b"HEAD", b"HEAX", 1))
        for option, path in (("--from-archive", corrupt), ("--from-bundle", stranger)):
            with self.subTest(option):
                code, report = env.run_json(*self.apply_args(env, option, str(path)))
                self.assertEqual(code, q.EXIT_REFUSED, report)
                self.assertIn("not a", json.dumps(report["attempts"]))
                self.assertTrue(env.stage_is_untouched())
        # a verification failure of a supplied artifact is never skipped past to the next source
        code, report = env.run_json(*self.apply_args(env, "--fork-url", "/nonexistent/repo", "--fallback-archive", str(corrupt), "--fallback-bundle", str(env.bundle_path)))
        self.assertEqual(code, q.EXIT_REFUSED, report)
        self.assertTrue(env.stage_is_untouched())

    def test_missing_artifact_is_action_required(self) -> None:
        env = Env(self)
        code, report = env.run_json(*self.apply_args(env, "--from-archive", str(env.recovery / "absent.tar.gz")))
        self.assertEqual(code, q.EXIT_ACTION, report)
        self.assertTrue(env.stage_is_untouched())

    def listed_tar(self, env: Env, label: str, members: list[dict[str, Any]], comment: str | None, lock: dict[str, Any] | None = None) -> Path:
        path = env.recovery / f"{label.replace(' ', '-')}.tar"
        path.write_bytes(tar_bytes(members, comment=comment))
        lock = copy.deepcopy(lock or env.lock)
        lock["recovery_artifacts"].append({"kind": "source-archive", "file": path.name, "sha256": q.sha256_file(path), "note": "fixture"})
        env.write_lock(lock)
        return path

    def refused(self, env: Env, path: Path, expected: str, label: str) -> None:
        code, report = env.run_json(*self.apply_args(env, "--from-archive", str(path)))
        self.assertEqual(code, q.EXIT_REFUSED, (label, report))
        self.assertTrue(expected in " ".join(report["attempts"]), f"{label}: expected {expected!r} in {report['attempts']}")
        self.assertTrue(env.stage_is_untouched(), label)

    def test_moved_ref_or_content_drift_in_a_listed_archive_is_refused(self) -> None:
        env = Env(self)
        base = [
            {"name": name, "data": text.encode(), "mode": 0o755 if name == "scripts/run.sh" else 0o644} for name, text in PLUGIN_FILES.items()
        ] + [{"name": "CLAUDE.md", "type": tarfile.SYMTYPE, "linkname": "AGENTS.md"}]
        cases = {
            "edited source file": ([{**m, "data": b"fn main() { evil() }\n"} if m["name"] == "src/main.rs" else m for m in base], env.revision, "source tree"),
            "extra file": (base + [{"name": "extra.sh", "data": b"#!/bin/sh\n"}], env.revision, "source tree"),
            "other commit header": (base, "d" * 40, "archive records commit"),
            "edited manifest version": ([{**m, "data": PLUGIN_FILES["herdr-plugin.toml"].replace("1.5.4", "1.5.5").encode()} if m["name"] == "herdr-plugin.toml" else m for m in base], env.revision, "herdr-plugin.toml: sha256 differs"),
            "edited license": ([{**m, "data": b"proprietary\n"} if m["name"] == "LICENSE" else m for m in base], env.revision, "LICENSE: sha256 differs"),
            "missing locked file": ([m for m in base if m["name"] != "Cargo.lock"], env.revision, "locked file Cargo.lock is missing"),
        }
        for label, (members, comment, expected) in cases.items():
            with self.subTest(label):
                self.refused(env, self.listed_tar(env, label, members, comment), expected, label)
        self.assertFalse(any(c.startswith("cargo build") for c in env.calls()), "nothing unverified is ever built")

    def test_content_manifest_is_enforced_independently_of_the_tree_id(self) -> None:
        env = Env(self)
        lock = copy.deepcopy(env.lock)
        lock["content_manifest_sha256"] = "0" * 64
        env.write_lock(lock)
        self.refused(env, env.archive_path, "content manifest differs", "manifest hash")

    def test_metadata_drift_is_refused_even_when_every_hash_is_self_consistent(self) -> None:
        """A source whose own hashes match its (re-generated) lock still fails the identity checks the lock names."""
        env = Env(self)
        variants = {
            "manifest version": ("herdr-plugin.toml", PLUGIN_FILES["herdr-plugin.toml"].replace('version = "1.5.4"', 'version = "1.5.5"'), "manifest version differs"),
            "plugin id": ("herdr-plugin.toml", PLUGIN_FILES["herdr-plugin.toml"].replace("herdr-agent-quota", "other-plugin"), "manifest plugin id"),
            "min herdr version": ("herdr-plugin.toml", PLUGIN_FILES["herdr-plugin.toml"].replace("0.9.0", "0.9.9"), "min_herdr_version differs"),
            "cargo version": ("Cargo.toml", PLUGIN_FILES["Cargo.toml"].replace("1.5.4", "1.5.5"), "manifest version differs"),
            "toolchain channel": ("rust-toolchain.toml", PLUGIN_FILES["rust-toolchain.toml"].replace("1.95.0", "nightly"), "rust-toolchain channel differs"),
        }
        for label, (name, text, expected) in variants.items():
            with self.subTest(label):
                members = [{"name": n, "data": (text if n == name else t).encode()} for n, t in PLUGIN_FILES.items() if n != "CLAUDE.md"]
                members.append({"name": "CLAUDE.md", "type": tarfile.SYMTYPE, "linkname": "AGENTS.md"})
                path = env.recovery / f"selfconsistent-{label.replace(' ', '-')}.tar"
                path.write_bytes(tar_bytes(members, comment=env.revision))
                with tarfile.open(path) as tar, tempfile.TemporaryDirectory() as scratch:
                    entries = q.extract_archive(tar, Path(scratch), {"CLAUDE.md": "AGENTS.md"})
                by = {e.path: e.sha256 for e in entries}
                lock = copy.deepcopy(env.lock)
                lock["tree_sha1"] = q.git_tree_sha1(entries)
                lock["content_manifest_sha256"] = q.content_manifest_sha256(entries)
                lock["critical_files"] = {n: by[n] for n in lock["critical_files"]}
                lock["license"]["sha256"] = by["LICENSE"]
                lock["recovery_artifacts"].append({"kind": "source-archive", "file": path.name, "sha256": q.sha256_file(path), "note": "fixture"})
                env.write_lock(lock)
                self.refused(env, path, expected, label)

    def test_unsafe_archive_members_are_refused_and_nothing_escapes(self) -> None:
        env = Env(self)
        sentinel = env.tmp / "escaped.txt"
        good = [{"name": n, "data": t.encode()} for n, t in PLUGIN_FILES.items() if n != "CLAUDE.md"]
        link = {"name": "CLAUDE.md", "type": tarfile.SYMTYPE, "linkname": "AGENTS.md"}
        cases: dict[str, tuple[list[dict[str, Any]], str]] = {
            "parent traversal": (good + [{"name": "../escaped.txt", "data": b"x"}], "unsafe member path"),
            "nested traversal": (good + [{"name": "src/../../escaped.txt", "data": b"x"}], "unsafe member path"),
            "absolute path": (good + [{"name": str(sentinel), "data": b"x"}], "unsafe member path"),
            "backslash": (good + [{"name": "a\\b", "data": b"x"}], "unsafe member path"),
            "control character": (good + [{"name": "a\nb", "data": b"x"}], "unsafe member path"),
            "dot component": (good + [{"name": "./x", "data": b"x"}], "unsafe member path"),
            "symlink outside the lock": (good + [{"name": "evil", "type": tarfile.SYMTYPE, "linkname": "../escaped.txt"}], "not in the dependency lock"),
            "symlink lock mismatch": (good + [{"name": "CLAUDE.md", "type": tarfile.SYMTYPE, "linkname": "src/main.rs"}], "not in the dependency lock"),
            "file below a symlink": (good + [link, {"name": "CLAUDE.md/inner", "data": b"x"}], "below a symlink"),
            "hard link": (good + [{"name": "hard", "type": tarfile.LNKTYPE, "linkname": "AGENTS.md"}], "member type"),
            "device": (good + [{"name": "dev", "type": tarfile.CHRTYPE}], "member type"),
            "fifo": (good + [{"name": "pipe", "type": tarfile.FIFOTYPE}], "member type"),
            "duplicate name": (good + [link, {"name": "src/main.rs", "data": b"y"}], "duplicate or case-colliding"),
            "case collision": (good + [link, {"name": "SRC/MAIN.RS", "data": b"y"}], "case-colliding"),
            "file over directory": (good + [link, {"name": "src", "data": b"y"}], "cannot create file"),
            "oversized member": (good + [link, {"name": "big.bin", "data": b"x" * 64}], "file limit"),
        }
        for label, (members, expected) in cases.items():
            with self.subTest(label):
                path = self.listed_tar(env, f"unsafe-{label}", members, env.revision)
                patch = mock.patch.object(q, "MAX_FILE_BYTES", 32) if label == "oversized member" else contextlib.nullcontext()
                with patch:
                    self.refused(env, path, expected, label)
                self.assertFalse(sentinel.exists(), label)
        with mock.patch.object(q, "MAX_MEMBERS", 3):
            self.refused(env, self.listed_tar(env, "many", good + [link], env.revision), "more than 3 members", "member count")
        with mock.patch.object(q, "MAX_TOTAL_BYTES", 10):
            self.refused(env, env.archive_path, "total limit", "total size")

    def test_bundle_must_be_self_contained_and_contain_the_commit(self) -> None:
        env = Env(self)
        data = env.bundle_path.read_bytes()
        variants = {
            "prerequisite": data.replace(b"# v2 git bundle\n", b"# v2 git bundle\n-" + b"e" * 40 + b" parent\n", 1),
            "other commit": data.replace(env.revision.encode(), b"f" * 40, 1),
            "not a bundle": b"PACK" + data[40:],
            "capability": data.replace(b"# v2 git bundle\n", b"# v3 git bundle\n@object-format=sha256\n", 1),
            "truncated pack": data[: len(data) // 2],
        }
        for label, payload in variants.items():
            with self.subTest(label):
                path = env.recovery / f"bundle-{label.replace(' ', '-')}.bundle"
                path.write_bytes(payload)
                lock = copy.deepcopy(env.lock)
                lock["recovery_artifacts"].append({"kind": "git-bundle", "file": path.name, "sha256": q.sha256_file(path), "note": "fixture"})
                env.write_lock(lock)
                code, report = env.run_json(*self.apply_args(env, "--from-bundle", str(path)))
                self.assertEqual(code, q.EXIT_REFUSED, report)
                self.assertTrue(env.stage_is_untouched(), label)

    def test_build_failure_removes_everything_and_activates_nothing(self) -> None:
        env = Env(self)
        env.switch("fail-cargo")
        code, report = env.run_json(*self.apply_args(env, "--from-archive", str(env.archive_path)))
        self.assertEqual(code, q.EXIT_ACTION, report)
        self.assertIn("could not compile", report["error"])
        self.assertIn("nothing was staged", report["result"])
        self.assertTrue(env.stage_is_untouched())
        self.assertEqual([c for c in env.calls() if c.startswith("herdr")], ["herdr plugin list --json"])

    def test_missing_toolchain_stops_before_any_acquisition(self) -> None:
        env = Env(self)
        env.switch("rustup-list", "stable-x86_64-unknown-linux-gnu (default)\n")
        code, report = env.run_json(*self.apply_args(env, "--fork-url", str(env.repo)))
        self.assertEqual(code, q.EXIT_ACTION, report)
        self.assertIn("build prerequisites are missing", report["result"])
        self.assertIn("rustup toolchain install 1.95.0", report["toolchain"]["action"])
        self.assertFalse(env.stage.exists(), "no directory is created before the prerequisites are met")

    def test_stage_directory_guards(self) -> None:
        env = Env(self)
        occupied = env.tmp / "occupied"
        occupied.mkdir()
        (occupied / "keep.txt").write_text("keep")
        code, _, err = env.run("bootstrap", "--apply", "--stage-dir", str(occupied), "--from-archive", str(env.archive_path))
        self.assertEqual(code, q.EXIT_REFUSED, err)
        self.assertEqual((occupied / "keep.txt").read_text(), "keep")
        code, _, err = env.run("bootstrap", "--apply", "--stage-dir", "relative/dir", "--from-archive", str(env.archive_path))
        self.assertEqual(code, q.EXIT_USAGE, err)
        code, _, err = env.run("bootstrap", "--apply", "--from-archive", str(env.archive_path))
        self.assertEqual(code, q.EXIT_USAGE, err)
        inside = env.home / ".config" / "herdr" / "plugins" / "stage"
        inside.parent.mkdir(parents=True)
        code, _, err = env.run("bootstrap", "--apply", "--stage-dir", str(inside), "--from-archive", str(env.archive_path))
        self.assertEqual(code, q.EXIT_REFUSED, err)
        self.assertFalse(inside.exists())
        empty = env.tmp / "empty-target"
        empty.mkdir()
        link = env.tmp / "linked"
        link.symlink_to(empty)
        code, _, err = env.run("bootstrap", "--apply", "--stage-dir", str(link), "--from-archive", str(env.archive_path))
        self.assertEqual(code, q.EXIT_REFUSED, err)
        self.assertEqual(list(empty.iterdir()), [], "a symlinked stage directory is never written through")
        code, _, err = env.run("bootstrap", "--apply", "--dry-run", "--stage-dir", str(env.stage))
        self.assertEqual(code, q.EXIT_USAGE, err)
        for bad in ("ext::sh -c id", "ssh://git@example.invalid/x", "http://insecure.example/x", "relative/repo", "--upload-pack=id"):
            code, _, err = env.run("bootstrap", "--dry-run", f"--fork-url={bad}")
            self.assertEqual(code, q.EXIT_USAGE, (bad, err))

    def test_cleanup_only_removes_the_directory_this_run_created(self) -> None:
        env = Env(self)
        env.stage.mkdir()
        mine = env.stage / "mine"
        mine.mkdir()
        outside = env.tmp / ".partial-foreign"
        outside.mkdir()
        (outside / "keep").write_text("keep")
        foreign = env.stage / ".partial-foreign"  # named like a temporary, but this run never created it
        foreign.mkdir()
        (foreign / "keep").write_text("keep")
        link = env.stage / "link"
        link.symlink_to(outside)
        for bad in (outside, env.stage, env.stage / ".partial-", env.stage / "unrelated", foreign, link):
            with self.subTest(str(bad)), self.assertRaises(q.DependencyError):
                q.remove_owned_dir(env.stage, bad, {mine})
        with self.assertRaises(q.DependencyError):
            q.remove_owned_dir(env.stage, outside, {outside})  # recorded as owned but not directly inside the stage directory
        self.assertTrue((outside / "keep").exists() and (foreign / "keep").exists())
        q.remove_owned_dir(env.stage, mine, {mine})
        self.assertFalse(mine.exists())


def apply_argv(env: Env, *extra: str) -> list[str]:
    return ["bootstrap", "--apply", "--stage-dir", str(env.stage), *extra]


def staged_source(test: unittest.TestCase, env: Env, *extra: str) -> Path:
    code, report = env.run_json(*apply_argv(env, "--from-archive", str(env.archive_path), *extra))
    test.assertEqual(code, q.EXIT_OK, report)
    return env.stage / f"herdr-agent-quota-{env.revision[:12]}"


def link_local(env: Env, root: Path, version: str = "1.5.4") -> None:
    env.set_inventory(mutate=lambda plugins: plugins[0].update(source={"kind": "local"}, plugin_root=str(root), version=version))


class ProducerConsumerTests(unittest.TestCase):
    """CI1: what `bootstrap` stages must be what `check` recognizes after the documented local link, from its content."""

    def check(self, env: Env) -> tuple[int, dict[str, Any]]:
        return env.run_json("check", "--quota-dir", str(env.quota_dir))

    def test_staged_source_passes_the_local_check_without_git_metadata(self) -> None:
        env = Env(self)
        final = staged_source(self, env)
        self.assertFalse((final / ".git").exists(), "the staged source is an archive extract, not a checkout")
        link_local(env, final)
        code, report = self.check(env)
        self.assertEqual((code, report["dependency"]["state"]), (q.EXIT_OK, "pinned"), report)
        self.assertIn("content verified", report["dependency"]["verified_by"])
        self.assertEqual(report["dependency"]["revision"], env.revision)

    def test_built_source_passes_and_build_output_is_not_part_of_the_locked_content(self) -> None:
        env = Env(self)
        final = staged_source(self, env)
        self.assertTrue((final / "target/release/herdr-agent-quota").is_file())
        link_local(env, final)
        self.assertEqual(self.check(env)[0], q.EXIT_OK)
        (final / "target/release/extra-build-artifact").write_text("anything")
        self.assertEqual(self.check(env)[0], q.EXIT_OK, "top-level target/ is build output; the check verifies source, not binaries")

    def test_drift_wrong_identity_and_unexpected_entries_are_conflicts(self) -> None:
        def unlink(rel: str) -> Any:
            return lambda root: (root / rel).unlink()

        def replace_with_link(root: Path) -> None:
            (root / "src/main.rs").unlink()
            (root / "src/main.rs").symlink_to("../AGENTS.md")

        cases: dict[str, Any] = {
            "edited source": lambda r: (r / "src/main.rs").write_text("fn main() { evil(); }\n"),
            "added file": lambda r: (r / "extra.sh").write_text("#!/bin/sh\n"),
            "added nested file": lambda r: (r / "src/deep/new.rs").write_text("//\n"),
            "added hidden file": lambda r: (r / ".hidden").write_text("x"),
            "deleted source": unlink("src/main.rs"),
            "deleted locked file": unlink("Cargo.lock"),
            "exec bit changed": lambda r: (r / "src/main.rs").chmod(0o755),
            "file replaced by symlink": replace_with_link,
            "extra symlink": lambda r: (r / "evil").symlink_to("AGENTS.md"),
            "symlinked directory": lambda r: (r / "linkdir").symlink_to(r / "src"),
            "other plugin id": lambda r: (r / "herdr-plugin.toml").write_text(PLUGIN_FILES["herdr-plugin.toml"].replace("herdr-agent-quota", "other-plugin")),
            "other manifest version": lambda r: (r / "herdr-plugin.toml").write_text(PLUGIN_FILES["herdr-plugin.toml"].replace('version = "1.5.4"', 'version = "1.5.5"')),
            "target is a symlink": lambda r: (shutil_rmtree(r / "target"), (r / "target").symlink_to(r / "src")),
        }
        for label, mutate in cases.items():
            with self.subTest(label):
                env = Env(self)
                final = staged_source(self, env)
                link_local(env, final)
                self.assertEqual(self.check(env)[0], q.EXIT_OK)
                mutate(final)
                code, report = self.check(env)
                self.assertEqual((code, report["dependency"]["state"]), (q.EXIT_REFUSED, "conflict"), (label, report))
                self.assertIn("does not match the lock", report["dependency"]["detail"])
                self.assertIn("nothing was changed", report["dependency"]["action"])

    def test_a_receipt_or_claimed_revision_is_never_trusted(self) -> None:
        env = Env(self)
        final = staged_source(self, env)
        link_local(env, final)
        receipt = env.stage / "BOOTSTRAP_RECEIPT.json"
        receipt.write_text(json.dumps({"revision": "f" * 40, "tree_sha1": "0" * 40}))
        self.assertEqual(self.check(env)[0], q.EXIT_OK, "an edited receipt cannot make verified content wrong")
        (final / "src/main.rs").write_text("fn main() { drifted() }\n")
        receipt.write_text(json.dumps({"revision": env.revision, "tree_sha1": env.git_tree}))
        self.assertEqual(self.check(env)[0], q.EXIT_REFUSED, "a receipt cannot make drifted content right")

    def test_duplicate_plugin_ids_stay_a_conflict_with_a_verified_local_copy(self) -> None:
        env = Env(self)
        final = staged_source(self, env)
        link_local(env, final)
        env.set_inventory(mutate=lambda plugins: (plugins[0].update(source={"kind": "local"}, plugin_root=str(final), version="1.5.4"), plugins.append(copy.deepcopy(plugins[0]))))
        code, report = self.check(env)
        self.assertEqual((code, report["dependency"]["state"]), (q.EXIT_REFUSED, "conflict"))
        self.assertIn("share the id", report["dependency"]["detail"])

    def test_a_directory_inside_an_unrelated_git_repository_is_verified_by_content(self) -> None:
        env = Env(self)
        outer = env.tmp / "outer"
        outer.mkdir()
        git("init", "-q", cwd=outer)
        (outer / "note.txt").write_text("unrelated")
        git("add", "-A", cwd=outer)
        git("commit", "-q", "-m", "unrelated", cwd=outer)
        env.stage = outer / "stage"
        final = staged_source(self, env)
        link_local(env, final)
        code, report = self.check(env)
        self.assertEqual((code, report["dependency"]["state"]), (q.EXIT_OK, "pinned"), report)
        self.assertIn("content verified", report["dependency"]["verified_by"], "the enclosing repository's HEAD must not be mistaken for this plugin's revision")
        (final / "src/main.rs").write_text("fn main() { drifted() }\n")
        self.assertEqual(self.check(env)[0], q.EXIT_REFUSED)

    def test_the_installed_source_scan_is_bounded(self) -> None:
        env = Env(self)
        final = staged_source(self, env)
        link_local(env, final)
        with mock.patch.object(q, "MAX_MEMBERS", 3):
            code, report = self.check(env)
        self.assertEqual((code, report["dependency"]["state"]), (q.EXIT_REFUSED, "conflict"))
        self.assertIn("more than 3 files", report["dependency"]["detail"])

    def test_bootstrap_on_a_verified_local_link_changes_nothing(self) -> None:
        env = Env(self)
        final = staged_source(self, env)
        link_local(env, final)
        other = env.tmp / "second-stage"
        code, report = env.run_json("bootstrap", "--apply", "--stage-dir", str(other), "--from-archive", str(env.archive_path))
        self.assertEqual(code, q.EXIT_OK, report)
        self.assertIn("nothing to do", report["result"])
        self.assertFalse(other.exists())


def shutil_rmtree(path: Path) -> None:
    import shutil
    shutil.rmtree(path)


class ConcurrencyTests(unittest.TestCase):
    """CI2: simultaneous callers must never destroy each other's output, and cleanup removes only what the caller created."""

    def call(self, env: Env, *argv: str) -> tuple[int, dict[str, Any]]:
        args = q.build_parser().parse_args(["--lock", str(env.lock_path), *argv])
        try:
            return q.command_bootstrap(args)
        except q.DependencyError as error:
            return error.code, {"error": str(error)}

    def argv(self, env: Env) -> list[str]:
        return ["bootstrap", "--apply", "--no-build", "--stage-dir", str(env.stage), "--from-archive", str(env.archive_path)]

    def assert_one_intact_result(self, env: Env) -> None:
        final = env.stage / f"herdr-agent-quota-{env.revision[:12]}"
        self.assertEqual(sorted(p.name for p in env.stage.iterdir()), sorted([final.name, "BOOTSTRAP_RECEIPT.json"]), "only the winner's output remains: no lock, no temporary directory")
        self.assertEqual(q.local_identity(str(final), env.lock), (env.revision, "content verified against the lock (git metadata not required)"))
        self.assertEqual(json.loads((env.stage / "BOOTSTRAP_RECEIPT.json").read_text())["revision"], env.revision)

    def test_two_simultaneous_callers_exactly_one_wins_and_its_output_survives(self) -> None:
        for round_number in range(6):
            with self.subTest(round_number):
                env = Env(self)
                barrier = threading.Barrier(2)
                real_validate = q.validate_stage

                def synced(stage_arg: str | None, require: bool, real: Any = real_validate, gate: threading.Barrier = barrier) -> Path | None:
                    result = real(stage_arg, require)  # both callers see the same empty stage before either writes
                    gate.wait(timeout=15)
                    return result

                results: dict[str, tuple[int, dict[str, Any]]] = {}
                with mock.patch.dict(os.environ, env.environ(), clear=True), mock.patch.object(q, "validate_stage", synced):
                    threads = [threading.Thread(target=lambda n=name: results.__setitem__(n, self.call(env, *self.argv(env)))) for name in ("A", "B")]
                    for thread in threads:
                        thread.start()
                    for thread in threads:
                        thread.join(60)
                self.assertEqual(sorted(code for code, _ in results.values()), [q.EXIT_OK, q.EXIT_REFUSED], results)
                self.assert_one_intact_result(env)

    def test_a_caller_that_validated_early_and_claims_late_refuses_without_touching_the_winner(self) -> None:
        env = Env(self)
        b_validated, a_done = threading.Event(), threading.Event()
        real_validate, real_claim = q.validate_stage, q.StageClaim.claim
        results: dict[str, tuple[int, dict[str, Any]]] = {}

        def validate(stage_arg: str | None, require: bool) -> Path | None:
            result = real_validate(stage_arg, require)
            if threading.current_thread().name == "B":
                b_validated.set()
            return result

        def claim(self_: q.StageClaim) -> None:
            if threading.current_thread().name == "B":
                a_done.wait(timeout=30)  # the first caller has finished and released before the second claims
            real_claim(self_)

        with mock.patch.dict(os.environ, env.environ(), clear=True), mock.patch.object(q, "validate_stage", validate), mock.patch.object(q.StageClaim, "claim", claim):
            second = threading.Thread(target=lambda: results.__setitem__("B", self.call(env, *self.argv(env))), name="B")
            second.start()
            self.assertTrue(b_validated.wait(timeout=30))
            results["A"] = self.call(env, *self.argv(env))
            a_done.set()
            second.join(60)
        self.assertEqual((results["A"][0], results["B"][0]), (q.EXIT_OK, q.EXIT_REFUSED), results)
        self.assertIn("not this run's", results["B"][1]["error"])
        self.assert_one_intact_result(env)

    def test_late_foreign_final_directory_is_refused_and_never_removed(self) -> None:
        env = Env(self)
        real_acquire = q.acquire

        def acquire_then_foreign(*args: Any, **kwargs: Any) -> Any:
            acquired = real_acquire(*args, **kwargs)
            foreign = env.stage / f"herdr-agent-quota-{env.revision[:12]}"
            foreign.mkdir()
            (foreign / "sentinel.txt").write_text("foreign")
            return acquired

        with mock.patch.object(q, "acquire", acquire_then_foreign):
            code, report = env.run_json(*apply_argv(env, "--from-archive", str(env.archive_path), "--no-build"))
        self.assertEqual(code, q.EXIT_REFUSED, report)
        self.assertEqual([p.name for p in env.stage.iterdir()], [f"herdr-agent-quota-{env.revision[:12]}"], "our lock and temporary directory are gone; the foreign directory is the only thing left")
        self.assertEqual((env.stage / f"herdr-agent-quota-{env.revision[:12]}/sentinel.txt").read_text(), "foreign")

    def test_late_foreign_receipt_is_refused_and_never_removed_but_our_output_is(self) -> None:
        env = Env(self)
        real_publish = q.StageClaim.publish

        def publish_then_foreign_receipt(self_: q.StageClaim) -> Path:
            final = real_publish(self_)
            (env.stage / "BOOTSTRAP_RECEIPT.json").write_text("foreign receipt")
            return final

        with mock.patch.object(q.StageClaim, "publish", publish_then_foreign_receipt):
            code, report = env.run_json(*apply_argv(env, "--from-archive", str(env.archive_path), "--no-build"))
        self.assertEqual(code, q.EXIT_REFUSED, report)
        self.assertEqual([p.name for p in env.stage.iterdir()], ["BOOTSTRAP_RECEIPT.json"])
        self.assertEqual((env.stage / "BOOTSTRAP_RECEIPT.json").read_text(), "foreign receipt")

    def test_existing_lock_of_another_run_is_never_removed(self) -> None:
        env = Env(self)
        env.stage.mkdir()
        lock = env.stage / q.LOCK_FILE
        lock.write_text("someone-else\n")
        code, report = env.run_json(*apply_argv(env, "--from-archive", str(env.archive_path), "--no-build"))
        self.assertEqual(code, q.EXIT_REFUSED, report)
        with mock.patch.object(q, "validate_stage", lambda stage_arg, require: Path(stage_arg or "")):  # even if validation was passed earlier, the claim refuses
            code, report = env.run_json(*apply_argv(env, "--from-archive", str(env.archive_path), "--no-build"))
        self.assertEqual(code, q.EXIT_REFUSED, report)
        self.assertEqual(lock.read_text(), "someone-else\n")
        self.assertEqual([p.name for p in env.stage.iterdir()], [q.LOCK_FILE])

    def test_release_removes_only_a_lock_that_still_carries_our_token(self) -> None:
        env = Env(self)
        real_acquire = q.acquire

        def acquire_then_replace_lock(*args: Any, **kwargs: Any) -> Any:
            (env.stage / q.LOCK_FILE).write_text("foreign-token\n")
            return real_acquire(*args, **kwargs)

        with mock.patch.object(q, "acquire", acquire_then_replace_lock):
            code, report = env.run_json(*apply_argv(env, "--from-archive", str(env.archive_path), "--no-build"))
        self.assertEqual(code, q.EXIT_OK, report)
        self.assertEqual((env.stage / q.LOCK_FILE).read_text(), "foreign-token\n")

    def test_interruption_removes_only_this_runs_lock_and_temporary_directory(self) -> None:
        env = Env(self)

        def interrupted(*args: Any, **kwargs: Any) -> Any:
            raise KeyboardInterrupt

        with mock.patch.object(q, "acquire", interrupted), self.assertRaises(KeyboardInterrupt):
            env.run_json(*apply_argv(env, "--from-archive", str(env.archive_path), "--no-build"))
        self.assertTrue(env.stage_is_untouched())


class ToolchainTests(unittest.TestCase):
    """CI3: the lock, not the caller's environment, selects the toolchain, and the executed compiler is verified."""

    OVERRIDES = {"RUSTUP_TOOLCHAIN": "1.98.1", "RUSTC": "/bin/false", "RUSTFLAGS": "-Cevil", "RUSTC_WRAPPER": "/bin/false", "CARGO_TARGET_DIR": "/nonexistent/elsewhere", "CARGO_BUILD_RUSTC": "/bin/false"}

    def test_inherited_overrides_never_reach_the_build_and_the_locked_channel_is_forced(self) -> None:
        env = Env(self)
        code, report = env.run_json(*apply_argv(env, "--from-archive", str(env.archive_path)), **self.OVERRIDES)
        self.assertEqual(code, q.EXIT_OK, report)
        cargo_lines = [c for c in env.calls() if c.startswith("cargo")]
        build = [c for c in cargo_lines if c.startswith("cargo build")]
        self.assertEqual(len(build), 1)
        self.assertIn("cargo build --release --locked ", build[0])
        for line in cargo_lines:
            self.assertIn("rt=1.95.0 rustc=unset flags=unset wrap=unset tdir=unset", line, "every toolchain command runs with the locked channel and no inherited override")
            self.assertIn("auto=0", line)
        receipt = json.loads((env.stage / "BOOTSTRAP_RECEIPT.json").read_text())["build"]
        self.assertEqual((receipt["toolchain_env"], receipt["rustc_release"], receipt["cargo_release"], receipt["compiler_releases_used"]), ("1.95.0", "1.95.0", "1.95.0", ["1.95.0"]))

    def test_check_reports_the_selected_toolchain_not_the_override(self) -> None:
        env = Env(self)
        _, report = env.run_json("check", "--quota-dir", str(env.quota_dir), **self.OVERRIDES)
        self.assertEqual((report["toolchain"]["status"], report["toolchain"]["rustc_release"], report["toolchain"]["cargo_release"]), ("PRESENT", "1.95.0", "1.95.0"))

    def test_a_selected_toolchain_that_is_not_the_locked_one_is_refused_before_the_build(self) -> None:
        cases = {
            "system rustc ignores the channel": ("rustc-release", "1.98.1", "requires exactly 1.95.0"),
            "cargo reports another release": ("cargo-release", "1.98.1", "requires exactly 1.95.0"),
            "rustc release unverifiable": ("rustc-release", "", "rustc reports no verifiable release"),
            "cargo release unverifiable": ("cargo-release", "", "cargo reports no verifiable release"),
        }
        for label, (switch, value, expected) in cases.items():
            with self.subTest(label):
                env = Env(self)
                env.switch(switch, value)
                code, report = env.run_json(*apply_argv(env, "--from-archive", str(env.archive_path)), **self.OVERRIDES)
                self.assertEqual(code, q.EXIT_ACTION, report)
                self.assertTrue(expected in report["action"], (label, report["action"]))
                self.assertFalse(any(c.startswith("cargo build") for c in env.calls()), "nothing is built with an unverified toolchain")
                self.assertFalse(env.stage.exists(), "refused before the stage directory is claimed")
                dry_code, dry = env.run_json("check", "--quota-dir", str(env.quota_dir), **self.OVERRIDES)
                self.assertEqual(dry["toolchain"]["status"], "MISSING")

    def test_the_toolchain_is_verified_again_in_the_staged_source_before_building(self) -> None:
        env = Env(self)
        env.switch("staged-release", "1.98.1")  # the generic probe passes; the staged source's own selection does not
        code, report = env.run_json(*apply_argv(env, "--from-archive", str(env.archive_path)))
        self.assertEqual(code, q.EXIT_ACTION, report)
        self.assertIn("requires exactly 1.95.0", report["action"])
        self.assertFalse(any(c.startswith("cargo build") for c in env.calls()))
        self.assertTrue(env.stage_is_untouched())

    def test_the_compiler_cargo_recorded_must_be_the_locked_one_before_anything_is_published(self) -> None:
        for label, switch, value, expected in (
            ("config chose another compiler", "info-release", "1.98.1", "recorded using (1.98.1)"),
            ("no compiler record", "no-info", "1", "recorded using (unknown)"),
        ):
            with self.subTest(label):
                env = Env(self)
                env.switch(switch, value)
                code, report = env.run_json(*apply_argv(env, "--from-archive", str(env.archive_path)))
                self.assertEqual(code, q.EXIT_ACTION, report)
                self.assertTrue(expected in report["error"], (label, report["error"]))
                self.assertTrue(any(c.startswith("cargo build") for c in env.calls()))
                self.assertTrue(env.stage_is_untouched(), "no staged directory and no success receipt")


class StaticContractTests(unittest.TestCase):
    def test_tool_never_runs_install_link_enable_configure_or_the_upstream_installer(self) -> None:
        source = (ROOT / "src/herdr_quota_dependency.py").read_text()
        invoked = re.findall(r"""run\(\s*\[([^\]]*)\]""", source)
        flat = " ".join(invoked)
        for forbidden in ('"install"', '"link"', '"enable"', '"configure"', '"uninstall"', "install.sh", '"update"'):
            self.assertNotIn(forbidden, flat)
        self.assertNotIn("shell=True", source)
        self.assertNotIn("os.system", source)
        for forbidden in ("api.telegram", "anthropic", "openai", "refresh --provider", '"refresh"'):
            self.assertNotIn(forbidden, source)

    def test_supervisor_install_never_acquires_or_activates_the_dependency(self) -> None:
        script = (ROOT / "install.sh").read_text()
        for forbidden in ("herdr plugin install", "herdr plugin link", "herdr plugin enable", "action invoke", "plugin-agent", "curl", "wget", "git clone", "cargo"):
            self.assertNotIn(forbidden, script)
        self.assertIn("herdr-quota-dependency check", script, "the installer points at the explicit read-only check")

    def test_documentation_names_the_owned_fork_pin_and_no_obsolete_link(self) -> None:
        lock = json.loads((ROOT / "config/quota-dependency.json").read_text())
        obsolete = "github.com/levi-qiao/herdr-agent-quota"
        for name in ("README.md", "docs/INSTALL.md", "docs/QUOTA_DEPENDENCY.md", "docs/RECOVERY.md", "docs/ARCHITECTURE.md"):
            text = (ROOT / name).read_text()
            for match in re.finditer(r"\]\(([^)]*)\)|https?://\S+", text):
                self.assertNotIn(obsolete, match.group(0), f"{name} links the obsolete repository")
        readme = (ROOT / "README.md").read_text()
        self.assertIn(lock["repository"]["url"], readme)
        doc = (ROOT / "docs/QUOTA_DEPENDENCY.md").read_text()
        for needle in (lock["revision"], lock["manifest_version"], lock["upstream"]["url"], "MIT", "NOT VALIDATED", "single-commit", "--locked", "herdr-quota-dependency check", "herdr-quota-dependency bootstrap", "not** a supported revision", "content matches the lock", "RUSTUP_TOOLCHAIN", ".rustc_info.json", ".bootstrap.lock", "O_EXCL", "never trusted"):
            self.assertTrue(needle in doc, f"QUOTA_DEPENDENCY.md lacks {needle!r}")
        for needle in ("toolchain", "crates", "offline"):
            self.assertTrue(needle in doc.lower(), f"QUOTA_DEPENDENCY.md lacks {needle!r}")
        install = (ROOT / "docs/INSTALL.md").read_text()
        self.assertTrue("herdr-quota-dependency check" in install)
        self.assertNotRegex(readme + install + doc, r"/home/[A-Za-z0-9._-]+/")

    def test_launcher_entry_point_and_packaging_are_registered(self) -> None:
        launcher = ROOT / "bin/herdr-quota-dependency"
        self.assertTrue(os.access(launcher, os.X_OK))
        self.assertIn("herdr_quota_dependency.py", launcher.read_text())
        self.assertEqual(subprocess.run(["sh", "-n", str(launcher)], check=False).returncode, 0)
        pyproject = (ROOT / "pyproject.toml").read_text()
        self.assertIn('herdr-quota-dependency = "herdr_quota_dependency:main"', pyproject)
        self.assertIn("quota-dependency.json", (ROOT / "MANIFEST.txt").read_text())
        self.assertIn("herdr-quota-dependency", (ROOT / "uninstall.sh").read_text())
        help_text = subprocess.run(["python3", "-I", str(ROOT / "src/herdr_quota_dependency.py"), "--help"], capture_output=True, text=True, check=False)
        self.assertEqual(help_text.returncode, 0)
        self.assertIn("bootstrap", help_text.stdout)


if __name__ == "__main__":
    unittest.main()
