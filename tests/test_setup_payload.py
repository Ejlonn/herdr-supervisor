"""Payload inventory: a reviewed local tree is read once through an explicit allowlist, hashed from those bytes,
and rejected before anything is interpreted when it contains links, special files, unexpected entries, unsafe
modes, inconsistent versions or missing manager pieces. Nothing in the tree is executed."""

from __future__ import annotations

import os
import shutil
import unittest
from pathlib import Path

import herdr_installation as hi
from setup_fixtures import CURRENT_VERSION, SetupCase, copy_payload


class PayloadInventoryTests(SetupCase):
    def test_inventory_is_explicit_complete_and_deterministic(self) -> None:
        first = hi.load_payload(self.payload)
        second = hi.load_payload(self.payload)
        self.assertEqual(first.payload_id, second.payload_id)
        self.assertEqual(first.generation_id, second.generation_id)
        self.assertEqual({role: len(first.of_role(role)) for role in hi.ROLES},
                         {"program": len(list((self.payload / "src").glob("*.py"))) + 1, "launcher": 8, "unit": 10, "doc": len(list((self.payload / "docs").glob("*.md"))) + 4, "example": 4})  # backup, model-profiles, supervisor, telegram
        self.assertIn("program/quota-dependency.json", {item.key for item in first.files})
        self.assertEqual(first.version, "0.4.0-beta.1")
        self.assertEqual(first.state_schema_version, 2)
        for item in first.files:
            self.assertEqual(item.sha256, hi.sha256_bytes(first.blobs[item.key]), "the inventory hashes the very bytes that are later published")
            self.assertEqual(item.mode, hi.INSTALL_MODE[item.role])

    def test_identity_separates_program_from_documents(self) -> None:
        base = hi.load_payload(self.payload)
        (self.payload / "docs/INSTALL.md").write_text((self.payload / "docs/INSTALL.md").read_text() + "\nedited\n")
        docs = hi.load_payload(self.payload)
        self.assertNotEqual(base.payload_id, docs.payload_id)
        self.assertEqual(base.program_id, docs.program_id, "a documentation change does not mint a new program generation")
        (self.payload / "src/herdr_cli.py").write_text((self.payload / "src/herdr_cli.py").read_text() + "\n# edited\n")
        code = hi.load_payload(self.payload)
        self.assertNotEqual(docs.program_id, code.program_id)
        self.assertNotEqual(docs.generation_id, code.generation_id)

    def test_build_metadata_is_ignored_but_never_other_entries(self) -> None:
        (self.payload / "src/__pycache__").mkdir()
        (self.payload / "src/__pycache__/x.cpython-313.pyc").write_bytes(b"\0")
        (self.payload / "src/herdr_supervisor.egg-info").mkdir()
        (self.payload / "src/herdr_supervisor.egg-info/PKG-INFO").write_text("x")
        (self.payload / "docs/images").mkdir(exist_ok=True)
        payload = hi.load_payload(self.payload)
        self.assertFalse([item for item in payload.files if "pycache" in item.name or "egg-info" in item.name])

    def test_unsafe_trees_are_refused_before_anything_is_read_or_executed(self) -> None:
        marker = self.tmp / "executed"

        def symlink_module(root: Path) -> None:
            (root / "src/herdr_extra.py").symlink_to(root / "src/herdr_core.py")

        def symlink_launcher(root: Path) -> None:
            (root / "bin/herdr-evil").symlink_to("/bin/sh")

        def fifo_unit(root: Path) -> None:
            os.mkfifo(root / "systemd/herdr-fifo.service")

        def hardlink(root: Path) -> None:
            os.link(root / "src/herdr_cli.py", root / "src/herdr_alias.py")

        def nested_dir(root: Path) -> None:
            (root / "src/subpackage").mkdir()

        def odd_doc(root: Path) -> None:
            (root / "docs/notes.txt").write_text("x")

        def odd_config(root: Path) -> None:
            (root / "config/secrets.json").write_text("{}")

        def huge(root: Path) -> None:
            (root / "src/herdr_huge.py").write_bytes(b"#" * (hi.MAX_FILE_BYTES + 1))

        def setuid(root: Path) -> None:
            (root / "bin/herdr-supervisor").chmod(0o4755)

        def writable(root: Path) -> None:
            (root / "src/herdr_cli.py").chmod(0o666)

        def version_skew(root: Path) -> None:
            (root / "VERSION").write_text("9.9.9\n")

        def bad_version(root: Path) -> None:
            (root / "VERSION").write_text("main\n")
            core = root / "src/herdr_core.py"
            core.write_text(core.read_text().replace(f'SUPERVISOR_VERSION = "{CURRENT_VERSION}"', 'SUPERVISOR_VERSION = "main"'))

        def no_manager(root: Path) -> None:
            (root / "src/herdr_update.py").unlink()

        def no_setup_launcher(root: Path) -> None:
            (root / "bin/herdr-setup").unlink()

        def no_lock(root: Path) -> None:
            (root / "config/quota-dependency.json").unlink()

        def upper_module(root: Path) -> None:
            (root / "src/Evil.py").write_text("x = 1\n")

        def traversal_name(root: Path) -> None:
            (root / "src/herdr_..py").write_text("x = 1\n")

        def computed_constant(root: Path) -> None:
            core = root / "src/herdr_core.py"
            core.write_text(core.read_text().replace(f'SUPERVISOR_VERSION = "{CURRENT_VERSION}"', 'SUPERVISOR_VERSION = "0.4.0" + "-beta.1"'))

        def import_side_effect(root: Path) -> None:
            (root / "src/herdr_trap.py").write_text(f"open({str(marker)!r}, 'w').write('ran')\n")

        for label, mutate, needle in (
            ("symlinked module", symlink_module, "symbolic link"), ("symlinked launcher", symlink_launcher, "symbolic link"), ("fifo unit", fifo_unit, "special file"),
            ("hard link", hardlink, "hard link"), ("nested directory", nested_dir, "unsupported directory"), ("unexpected document", odd_doc, "allowed doc name"),
            ("unexpected config", odd_config, "not an allowed configuration"), ("oversize file", huge, "exceeds"), ("setuid bit", setuid, "permission bits"),
            ("world-writable", writable, "permission bits"), ("version skew", version_skew, "disagrees"), ("unsupported version text", bad_version, "supported release version"),
            ("missing manager module", no_manager, "cannot manage"), ("missing setup launcher", no_setup_launcher, "herdr-setup"), ("missing quota lock", no_lock, "quota-dependency.json"),
            ("uppercase module", upper_module, "allowed program name"), ("dot-dot name", traversal_name, "allowed program name"), ("computed version constant", computed_constant, "literals"),
        ):
            with self.subTest(label):
                root = copy_payload(self.tmp / f"bad-{label.replace(' ', '-')}")
                mutate(root)  # type: ignore[operator]
                with self.assertRaises(hi.InstallError) as caught:
                    hi.load_payload(root)
                self.assertIn(needle, str(caught.exception))
        # An importable module with a side effect is inventoried like any other file and never run.
        root = copy_payload(self.tmp / "trap")
        import_side_effect(root)
        payload = hi.load_payload(root)
        self.assertIn("program/herdr_trap.py", {item.key for item in payload.files})
        self.assertFalse(marker.exists(), "loading a payload must not import or execute any of its files")

    def test_payload_root_must_be_a_real_directory(self) -> None:
        link = self.tmp / "payload-link"
        link.symlink_to(self.payload)
        with self.assertRaises(hi.InstallError):
            hi.load_payload(link)
        with self.assertRaises(hi.InstallError):
            hi.load_payload(self.tmp / "does-not-exist")
        file_root = self.tmp / "file"
        file_root.write_text("x")
        with self.assertRaises(hi.InstallError):
            hi.load_payload(file_root)

    def test_a_wheel_without_a_payload_tree_reports_the_gap_clearly(self) -> None:
        empty = self.tmp / "site-packages-only"
        (empty / "src").mkdir(parents=True)
        shutil.copy2(self.payload / "src/herdr_setup.py", empty / "src/herdr_setup.py")
        with self.assertRaises(hi.InstallError) as caught:
            hi.load_payload(empty)
        self.assertIn("missing", str(caught.exception))
        code, _out, err = self.run_tool("setup", "--payload", str(empty))
        self.assertEqual(code, hi.EXIT_REFUSED)
        self.assertIn("missing", err)

    def test_declared_constants_come_from_parsing_not_importing(self) -> None:
        source = b'SUPERVISOR_VERSION = "1.2.3"\nSTATE_SCHEMA_VERSION = 7\nraise RuntimeError("must not run")\n'
        self.assertEqual(hi._declared_constants(source), {"SUPERVISOR_VERSION": "1.2.3", "STATE_SCHEMA_VERSION": 7})
        with self.assertRaises(hi.InstallError):
            hi._declared_constants(b"STATE_SCHEMA_VERSION = True\nSUPERVISOR_VERSION = 'x'\n")
        with self.assertRaises(hi.InstallError):
            hi._declared_constants(b"def broken(:\n")


class VersionTests(unittest.TestCase):
    def test_version_ordering_treats_final_releases_after_prereleases(self) -> None:
        order = ["0.3.0-alpha.1", "0.3.0-beta.1", "0.3.0-beta.2", "0.3.0-rc.1", "0.3.0", "0.3.1", "0.10.0", "1.0.0"]
        keys = [hi.version_key(item) for item in order]
        self.assertEqual(keys, sorted(keys))
        self.assertEqual(hi.version_key("0.3.0-beta.1"), hi.version_key("0.3.0b1"))
        with self.assertRaises(hi.InstallError):
            hi.version_key("main")


if __name__ == "__main__":
    unittest.main()
