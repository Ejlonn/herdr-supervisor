"""Declaration records in transcripts and read-only reporting of Codex role definition files; synthetic fixtures only, never calls a model."""
import json
import tempfile
import unittest
from pathlib import Path

import herdr_codex_children as cc
import herdr_role_readiness as rr
from test_codex_child_guard import Base, P, rec, write_lines
from v2_fixtures import hs

DECL = {"type": "additional_tools", "role": "developer", "tools": [
    {"type": "namespace", "name": "collaboration", "tools": [
        {"type": "function", "name": "spawn_agent", "parameters": {"type": "object"}}]}]}


class DeclarationTests(Base):
    def add_declarations(self, payload=DECL, which="both"):
        made = self.world.build()
        for label, path in made.items():
            if which != "both" and label != which:
                continue
            rows = [json.loads(line) for line in path.read_text().splitlines()]
            rows.insert(1, rec("response_item", payload, 1))
            write_lines(path, rows)

    def test_definitions_in_parent_and_child_are_not_spawn_invocations(self):
        self.add_declarations()
        v = cc.verify(P, self.root)
        self.assertEqual(v.kind, cc.COMPLETE)
        self.assertEqual(v.evidence["spawn_calls"], 1)

    def test_malformed_or_unknown_declarations_never_clear(self):
        bad = [dict(DECL, role="assistant"), dict(DECL, tools={}), dict(DECL, tools=[None]),
               dict(DECL, tools=[{"type": "function_call", "name": "spawn_agent"}]),
               dict(DECL, tools=[{"type": "namespace", "name": "x", "tools": None}]),
               dict(DECL, tools=[{"type": "function", "name": True}])]
        for payload in bad:
            for which in ("parent", "child"):
                with self.subTest(payload=payload, which=which):
                    self.add_declarations(payload, which)
                    self.assertEqual(cc.verify(P, self.root).kind, cc.UNKNOWN)


class DefinitionReportingTests(unittest.TestCase):
    def test_toml_is_reported_without_changing_claude_md_inventory_or_claiming_pickup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "repository_explorer.toml").write_text('name="repository_explorer"\n')
            (root / "claude-role.md").write_text("instructions")
            (root / "linked.toml").symlink_to(root / "repository_explorer.toml")
            self.assertEqual([x["name"] for x in rr.installed_definitions(root)["roles"]], ["claude-role"])
            self.assertEqual([x["name"] for x in rr.installed_definitions(root, pattern="*.toml")["roles"]], ["repository_explorer"])
            with self.assertRaises(hs.SupervisorError):
                rr.installed_definitions(root, pattern="../*")
