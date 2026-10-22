"""Truthful subagent-role readiness (supervisor reliability, batch 4). Offline: temporary definitions directories, an injected version reader and a scripted
FakeHerdr. Nothing here invokes an agent; every case asserts zero prompts/reads/keys and that ownership files are untouched."""
from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path

import herdr_role_readiness as rr  # noqa: E402
from v2_fixtures import CLAUDE_SESSION, FakeHerdr, V2Case, hs  # noqa: I001 - first: puts the source or installed layout on sys.path

ROLES = ["prospera-tracer", "prospera-test-runner", "prospera-diff-auditor"]
VERSION = "2.1.306 (Claude Code)"


class RoleCase(V2Case):
    def setUp(self) -> None:
        super().setUp()
        self.agents_dir = Path(self.tmp.name) / "claude-agents"
        self.config["subagent_roles"] = {"claude": {"definitions_dir": str(self.agents_dir), "roles": ROLES}}
        self.version = VERSION
        self.sup.provider_version_reader = lambda provider: self.version

    def install(self, names=ROLES) -> None:
        self.agents_dir.mkdir(exist_ok=True)
        for name in names:
            (self.agents_dir / f"{name}.md").write_text(f"---\nname: {name}\n---\nrole text for {name}\n")

    def report(self) -> dict:
        return self.sup.role_readiness_report(self.herdr.list_agents(), self.sup.owners())

    def observe(self, kind: str = "agent_tool_catalog", roles=ROLES) -> dict:
        return self.sup.record_role_observation("claude", kind, list(roles), "cli:operator")

    def owners_digest(self) -> str:
        return hashlib.sha256(self.paths.owners_file.read_bytes()).hexdigest()

    def assert_inert(self, owners_before: str) -> None:
        self.assertEqual((self.herdr.prompts, self.herdr.reads, self.herdr.sent_keys, self.herdr.starts), ([], [], [], []))
        self.assertEqual(self.owners_digest(), owners_before)


class ReadinessReportTests(RoleCase):
    def test_installed_definitions_without_evidence_are_unknown_not_ready_and_not_failed(self) -> None:
        self.install()
        owners = self.owners_digest()
        claude = self.report()["claude"]
        self.assertEqual(claude["definitions"]["status"], "installed")
        self.assertEqual([r["name"] for r in claude["definitions"]["roles"]], sorted(ROLES))
        self.assertTrue(all(len(r["sha256"]) == 64 for r in claude["definitions"]["roles"]))
        self.assertNotIn("role text", json.dumps(claude), "definition content is never copied into a report")
        self.assertEqual(claude["selectability"]["status"], "UNKNOWN")
        self.assertEqual(claude["selectability"]["authority"], "none")
        self.assertIn("role-observe", claude["selectability"]["action"])
        self.assertEqual(claude["menu_visibility"]["status"], "UNKNOWN")
        self.assertEqual((claude["parent"]["session_id"], claude["parent"]["provider_version"]), (CLAUDE_SESSION, VERSION))
        self.assertIn("not an independent sandbox", " ".join(claude["limitations"]))
        self.assertIn("R1", " ".join(claude["limitations"]))
        self.assert_inert(owners)

    def test_menu_visibility_alone_never_establishes_selectability(self) -> None:
        self.install()
        self.observe("menu_visible")
        claude = self.report()["claude"]
        self.assertEqual(claude["menu_visibility"]["status"], "operator_reported")
        self.assertEqual(claude["selectability"]["status"], "UNKNOWN")

    def test_a_current_operator_observation_of_the_agent_tool_catalog_is_operator_evidence_with_no_authority(self) -> None:
        self.install()
        record = self.observe()
        self.assertEqual((record["authority"], record["session_id"], record["provider_version"]), ("operator_evidence_only", CLAUDE_SESSION, VERSION))
        claude = self.report()["claude"]
        self.assertEqual(claude["selectability"]["status"], "OPERATOR_REPORTED_SELECTABLE")
        self.assertIn("not execution authority", claude["selectability"]["authority"])
        self.assertEqual(self.herdr.prompts, [])

    def test_an_observation_that_misses_an_expected_role_is_reported_not_selectable_with_the_gap(self) -> None:
        self.install()
        self.observe("agent_tool_catalog", ROLES[:2])
        claude = self.report()["claude"]
        self.assertEqual(claude["selectability"]["status"], "OPERATOR_REPORTED_NOT_SELECTABLE")
        self.assertEqual(claude["selectability"]["missing"], ["prospera-diff-auditor"])
        self.assertTrue(claude["selectability"]["action"])
        self.observe("agent_tool_catalog_absent", ["prospera-tracer"])
        self.assertEqual(self.report()["claude"]["selectability"]["status"], "OPERATOR_REPORTED_NOT_SELECTABLE")

    def test_session_or_version_change_invalidates_without_carrying_the_claim_forward(self) -> None:
        self.install()
        self.observe()
        self.version = "2.1.307 (Claude Code)"  # the CLI was updated
        stale = self.report()["claude"]
        self.assertEqual(stale["selectability"]["status"], "UNKNOWN")
        self.assertEqual([o["status"] for o in stale["observations"]], ["stale_version"])
        self.version = VERSION
        self.assertEqual(self.report()["claude"]["selectability"]["status"], "OPERATOR_REPORTED_SELECTABLE")
        # the owned session changes (restart, fresh conversation): the old observation names another session
        self.owners["claude"]["session_id"] = "33333333-3333-4333-8333-333333333333"
        hs.atomic_write_json(self.paths.owners_file, self.owners, mode=0o600)
        self.herdr.agents["claude-main"]["agent_session"] = {"value": "33333333-3333-4333-8333-333333333333"}
        changed = self.report()["claude"]
        self.assertEqual(changed["selectability"]["status"], "UNKNOWN")
        self.assertEqual([o["status"] for o in changed["observations"]], ["stale_session"])

    def test_absent_unreadable_and_unsupported_sources_are_actionable_not_errors(self) -> None:
        claude = self.report()["claude"]
        self.assertEqual((claude["definitions"]["status"], claude["definitions"]["missing"]), ("absent", sorted(ROLES)))
        self.assertEqual(claude["selectability"]["status"], "UNKNOWN")
        self.assertTrue(claude["selectability"]["action"])
        self.assertEqual(self.report()["codex"]["status"], "not_applicable")
        self.assertIn("disabled by policy", self.report()["codex"]["detail"])
        with self.assertRaises(hs.SupervisorError):
            self.sup.record_role_observation("codex", "agent_tool_catalog", ["x"], "cli:operator")

    def test_observation_input_is_strictly_validated_and_never_unbound(self) -> None:
        self.install()
        bad = (("not_a_kind", ROLES), ("agent_tool_catalog", []), ("agent_tool_catalog", ["../etc/passwd"]), ("agent_tool_catalog", "prospera-tracer"), ("agent_tool_catalog", ["a"] * 65))
        for kind, roles in bad:
            with self.subTest(kind=kind, roles=str(roles)[:30]), self.assertRaises(hs.SupervisorError):
                self.sup.record_role_observation("claude", kind, roles, "cli:operator")
        self.version = None  # the version cannot be established: an unbound observation would carry forward silently
        with self.assertRaises(hs.SupervisorError):
            self.observe()
        self.version = VERSION
        # a missing/changed live session refuses before anything is written
        del self.herdr.agents["claude-main"]
        with self.assertRaises((hs.SupervisorError, hs.HerdrError)):
            self.observe()
        self.assertFalse((self.paths.state_dir / "role_observations.json").exists())

    def test_malformed_stored_observations_are_ignored_not_trusted(self) -> None:
        self.install()
        path = self.paths.state_dir / "role_observations.json"
        good = {"provider": "claude", "kind": "agent_tool_catalog", "roles": ROLES, "session_id": CLAUDE_SESSION, "provider_version": VERSION, "actor": "cli:x", "at": "2026-10-21T00:00:00Z"}
        path.write_text(json.dumps({"schema_version": 1, "observations": [good, {**good, "kind": "invoked_successfully"}, {**good, "roles": "all"}, {**good, "provider": "codex"}, "junk"]}))
        self.assertEqual(len(rr.load_observations(path)), 1)
        path.write_text("{not json")
        self.assertEqual(rr.load_observations(path), [])
        path.write_text(json.dumps({"schema_version": 2, "observations": [good]}))
        self.assertEqual(rr.load_observations(path), [])


class ControlIdentityRuleTests(RoleCase):
    """CI6: readiness verifies the parent with the exact predicate the control path uses (exact owned session in the recorded pane, no duplicate carrier)."""

    def test_current_owner_is_verified_and_every_weaker_situation_is_unverified_without_any_repair(self) -> None:
        self.install()
        self.observe()
        owners = self.owners_digest()
        current = self.report()["claude"]
        self.assertEqual(current["selectability"]["status"], "OPERATOR_REPORTED_SELECTABLE")
        self.assertTrue(current["parent"]["identity"].startswith("verified"))
        # the same session record moves to another pane while ownership keeps the recorded one: control refuses, so readiness must not claim the parent
        self.herdr.agents["claude-main"]["pane_id"] = "w3:p99"
        moved = self.report()["claude"]
        self.assertEqual((moved["selectability"]["status"], moved["parent"]["session_id"]), ("UNKNOWN", None))
        self.assertIn("pane conflict", moved["parent"]["identity"])
        self.assertTrue(moved["parent"]["identity"].startswith("unverified"))
        with self.assertRaises((hs.SupervisorError, hs.HerdrError)):
            self.sup._guard_agent("claude")  # the control predicate agrees
        with self.assertRaises((hs.SupervisorError, hs.HerdrError)):
            self.observe()  # and an observation cannot be recorded against an unverified parent
        # the same session live in two panes (duplicate carrier)
        self.herdr.agents["claude-main"]["pane_id"] = "w3:p1"
        self.herdr.agents["claude-copy"] = FakeHerdr.agent("claude", "claude-copy", "w3:p9", CLAUDE_SESSION)
        duplicate = self.report()["claude"]
        self.assertEqual((duplicate["selectability"]["status"], duplicate["parent"]["session_id"]), ("UNKNOWN", None))
        self.assertIn("more than one pane", duplicate["parent"]["identity"])
        del self.herdr.agents["claude-copy"]
        # identity temporarily unavailable
        saved = self.herdr.agents["claude-main"].pop("agent_session")
        unavailable = self.report()["claude"]
        self.assertEqual((unavailable["selectability"]["status"], unavailable["parent"]["session_id"]), ("UNKNOWN", None))
        self.herdr.agents["claude-main"]["agent_session"] = saved
        self.assertEqual(self.report()["claude"]["selectability"]["status"], "OPERATOR_REPORTED_SELECTABLE", "restored: the same observation is current again")
        self.assertEqual((self.herdr.prompts, self.herdr.sent_keys, self.herdr.starts), ([], [], []), "no restart, rebind or repair")
        self.assertEqual(self.owners_digest(), owners)

    def test_readiness_does_not_claim_to_detect_a_same_id_restart_or_resume(self) -> None:
        self.install()
        claude = self.report()["claude"]
        limitations = " ".join(claude["limitations"])
        self.assertIn("SAME session id with the SAME CLI version", limitations)
        self.assertIn("not detected", limitations)
        self.assertIn("does not prove the running session's version", claude["parent"]["version_basis"])


class OptionalSettingsValidationTests(RoleCase):
    """CI7: container types are checked before nested access: every wrong shape is an actionable SupervisorError, never an AttributeError/TypeError."""

    def load(self, **extra):
        self.paths.config_file.write_text(json.dumps({"schema_version": 1, "project_root": str(self.project_root), "review_root": str(self.review_root), "product_repo": str(self.product_repo),
                                                      "quota_dir": str(self.quota_dir), "quota_safety_buffer_seconds": 60, **extra}))
        return hs.load_config(self.paths.config_file)

    def test_wrong_shapes_for_subagent_roles_are_actionable_errors(self) -> None:
        bad = {"null provider settings": {"claude": None}, "list provider settings": {"claude": []}, "string provider settings": {"claude": "x"}, "boolean provider settings": {"claude": True},
               "list container": [], "string container": "claude", "boolean container": True, "number container": 3, "unknown provider": {"codex": {}},
               "unknown key": {"claude": {"extra": 1}}, "roles not a list": {"claude": {"roles": "a"}}, "roles empty": {"claude": {"roles": []}},
               "role with a path": {"claude": {"roles": ["../x"]}}, "role not a string": {"claude": {"roles": [7]}}, "relative directory": {"claude": {"definitions_dir": "rel/dir"}},
               "directory not a string": {"claude": {"definitions_dir": 7}}, "directory with NUL": {"claude": {"definitions_dir": "/a\x00b"}}}
        for label, value in bad.items():
            with self.subTest(label), self.assertRaises(hs.SupervisorError):
                self.load(subagent_roles=value)

    def test_wrong_shapes_for_native_session_roots_are_actionable_errors(self) -> None:
        for label, value in {"list": [], "string": "x", "boolean": True, "null provider": {"claude": None}, "relative": {"claude": "rel"}, "number": {"codex": 4}, "unknown provider": {"gemini": "/x"}, "NUL": {"claude": "/a\x00"}}.items():
            with self.subTest(label), self.assertRaises(hs.SupervisorError):
                self.load(native_session_roots=value)

    def test_valid_settings_and_absence_are_accepted(self) -> None:
        self.load()
        self.load(subagent_roles=None, native_session_roots=None)
        config = self.load(subagent_roles={"claude": {"definitions_dir": "/srv/agents", "roles": ["prospera-tracer", "a.b-c_d"]}}, native_session_roots={"claude": "/srv/claude", "codex": "/srv/codex"})
        self.assertEqual(config["subagent_roles"]["claude"]["roles"], ["prospera-tracer", "a.b-c_d"])
        self.load(subagent_roles={"claude": {}})


class DoctorAndStatusTests(RoleCase):
    def test_doctor_warns_unknown_selectability_and_status_is_compact_and_never_invokes_anything(self) -> None:
        self.install()
        owners = self.owners_digest()
        report = self.sup.doctor()
        self.assertIn("subagent_roles", report)
        self.assertTrue(any("UNKNOWN" in w and "role definitions" in w for w in report["warnings"]), report["warnings"])
        self.assertNotIn("selectability in the running parent session is UNKNOWN", " ".join(report["errors"]))
        status = self.sup.status()
        self.assertEqual(status["subagent_roles"]["claude"], {"definitions": "installed", "selectability": "UNKNOWN", "session_bound": True})
        self.assertEqual(status["subagent_roles"]["codex"], "not_applicable")
        self.assertEqual((self.herdr.prompts, self.herdr.sent_keys, self.herdr.starts), ([], [], []))
        self.assertEqual(self.owners_digest(), owners)

    def test_the_cached_version_probe_does_not_spawn_per_status_poll(self) -> None:
        calls = []
        import herdr_role_readiness as module  # noqa: PLC0415

        original = module.read_provider_version
        module.read_provider_version = lambda command, runner: calls.append(command) or VERSION
        self.addCleanup(lambda: setattr(module, "read_provider_version", original))
        sup = self.make_supervisor()  # default (real, cached) reader path
        for _ in range(3):
            sup.status()
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
