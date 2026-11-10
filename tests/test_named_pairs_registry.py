"""The named-pair registry and the closed selection/binding shapes: bounded names, canonical context inside administrator roots,
immutable entries, exact JSON types, malformed documents failing closed, and genuinely simultaneous registrations."""

from __future__ import annotations

import json
import threading
import uuid

import herdr_pairs as hpair
import herdr_sessions
from pair_fixtures import ALPHA, BETA, PairCase, hs, ids

PREP = "cccccccc-0000-4000-8000-0000000000aa"
PREP2 = "cccccccc-0000-4000-8000-0000000000bb"


class NameTests(PairCase):
    def test_names_are_bounded_generic_labels_not_paths(self) -> None:
        self.assertEqual(hpair.normalize_name("  My   Pair "), "My Pair")
        self.assertEqual(hpair.normalize_name("Example"), "Example")  # an example, never special
        for bad in ("", " ", "a/b", "a\\b", "a\x00b", "a\nb", "a‮b", "x" * 65, "<b>", "`x`", "it's", 7, None, b"x"):
            with self.subTest(bad=bad), self.assertRaises(hs.SupervisorError):
                hpair.normalize_name(bad)
        self.assertEqual(hpair.normalize_name("x" * 64), "x" * 64)

    def test_duplicate_names_differ_only_by_case_or_composition_are_refused(self) -> None:
        self.register("Team One", ALPHA)
        for clash in ("team one", "TEAM  ONE", "Team One"):
            with self.subTest(clash=clash), self.assertRaisesRegex(hs.SupervisorError, "already uses that name"):
                self.register(clash, BETA)
        self.assertEqual(len(hpair.list_entries(self.paths)), 1)


class ContextTests(PairCase):
    def check(self, **override: str) -> dict:
        return hpair.check_context(self.config, {**self.ctx(self.repo_a, self.review_a), **override})

    def test_context_must_be_real_directories_inside_the_administrator_roots(self) -> None:
        self.assertEqual(self.check()["repository"], str(self.repo_a))
        outside = self.project_root.parent / "outside-root"
        outside.mkdir()
        link = self.project_root / "link-to-alpha"
        link.symlink_to(self.repo_a)
        for label, override in (("outside", {"repository": str(outside)}), ("relative", {"repository": "repo-alpha"}), ("traversal", {"repository": str(self.repo_a) + "/../repo-beta"}),
                                ("symlink", {"repository": str(link)}), ("missing", {"review_base": str(self.project_root / "nope")}), ("file", {"review_base": str(self.project_root / "AGENTS.md")}),
                                ("instruction root", {"instruction_root": str(self.repo_a)}), ("nul", {"repository": str(self.repo_a) + "\x00"}), ("int", {"repository": 7})):
            with self.subTest(label), self.assertRaises(hs.SupervisorError):
                self.check(**override)

    def test_shape_is_exact_and_unconfigured_roots_make_the_feature_inert(self) -> None:
        for bad in ({}, {"repository": "/x"}, {**self.ctx(self.repo_a, self.review_a), "extra": "/y"}, None, [], "x"):
            with self.subTest(bad=bad), self.assertRaises(hs.SupervisorError):
                hpair.validate_context_shape(bad)
        self.assertTrue(hpair.pairs_enabled(self.config))
        self.config["session_pairs"] = {"enabled": True, "allowed_roots": [], "instruction_root": None}
        self.assertFalse(hpair.pairs_enabled(self.config))
        self.config["session_pairs"] = {"enabled": False, "allowed_roots": [str(self.project_root)], "instruction_root": None}
        self.assertFalse(hpair.pairs_enabled(self.config))

    def test_configuration_is_validated_with_exact_types(self) -> None:
        good = {"session_pairs": {"enabled": True, "allowed_roots": [str(self.project_root)], "instruction_root": str(self.project_root)}}
        config_file = self.paths.config_file
        for label, value in (("enabled str", {"enabled": "yes", "allowed_roots": []}), ("roots str", {"enabled": True, "allowed_roots": "/x"}), ("relative root", {"enabled": True, "allowed_roots": ["x"]}),
                             ("many roots", {"enabled": True, "allowed_roots": [f"/r{i}" for i in range(17)]}), ("unknown key", {"enabled": True, "allowed_roots": [], "extra": 1}),
                             ("non-canonical", {"enabled": True, "allowed_roots": ["/a/../b"]}), ("root int", {"enabled": True, "allowed_roots": [3]})):
            with self.subTest(label):
                config_file.write_text(json.dumps({"session_pairs": value}))
                with self.assertRaises(hs.SupervisorError):
                    hs.load_config(config_file)
        config_file.write_text(json.dumps(good))
        self.assertTrue(hs.load_config(config_file)["session_pairs"]["enabled"])


class IdentityShapeTests(PairCase):
    def test_identities_need_exact_keys_canonical_panes_uuids_and_distinct_members(self) -> None:
        good = ids(ALPHA)
        self.assertEqual(hpair.validate_identities(good), good)
        mutations = {
            "missing provider": lambda v: v.pop("claude"),
            "extra provider": lambda v: v.update(other={"pane_id": "w1:p1", "session_id": ALPHA["codex"][1]}),
            "extra key": lambda v: v["claude"].update(name="x"),
            "bad pane": lambda v: v["claude"].update(pane_id="pane-1"),
            "bad uuid": lambda v: v["claude"].update(session_id="not-a-uuid"),
            "uppercase uuid": lambda v: v["claude"].update(session_id=ALPHA["claude"][1].upper()),
            "same session": lambda v: v["claude"].update(session_id=v["codex"]["session_id"]),
            "same pane": lambda v: v["claude"].update(pane_id=v["codex"]["pane_id"]),
            "int pane": lambda v: v["claude"].update(pane_id=3),
        }
        for label, mutate in mutations.items():
            value = json.loads(json.dumps(good))
            mutate(value)
            with self.subTest(label), self.assertRaises(hs.SupervisorError):
                hpair.validate_identities(value)


class RegistryTests(PairCase):
    def test_entries_are_private_immutable_and_fingerprinted(self) -> None:
        entry = self.register("Alpha", ALPHA)
        path = hpair.registry_path(self.paths)
        self.assertEqual(oct(path.stat().st_mode & 0o777), "0o600")
        self.assertEqual(oct(hpair.registry_lock_path(self.paths).stat().st_mode & 0o777), "0o600")
        self.assertEqual(hpair.get_entry(self.paths, entry["pair_id"]), entry)
        before = hpair.entry_fingerprint(entry)
        other = self.register("Beta", BETA, self.repo_b, self.review_b)
        self.assertNotEqual(before, hpair.entry_fingerprint(other))
        self.assertEqual(hpair.entry_fingerprint(hpair.get_entry(self.paths, entry["pair_id"])), before, "a second registration never edits the first entry")
        context_changed = json.loads(json.dumps(entry))
        context_changed["context"]["repository"] = str(self.repo_b)
        self.assertNotEqual(hpair.entry_fingerprint(context_changed), before, "context is part of the fingerprint")

    def test_a_session_can_belong_to_only_one_saved_pair(self) -> None:
        self.register("Alpha", ALPHA)
        mixed = {"claude": ALPHA["claude"], "codex": BETA["codex"]}
        with self.assertRaisesRegex(hs.SupervisorError, "already saved in another pair"):
            self.register("Gamma", mixed)
        self.assertEqual(len(hpair.list_entries(self.paths)), 1)

    def test_registry_holds_at_most_sixty_four_pairs(self) -> None:
        for index in range(hpair.MAX_PAIRS):
            self.register(f"Pair {index}", {"claude": (f"w{index + 10}:p1", str(uuid.UUID(int=(index + 1) * 2))), "codex": (f"w{index + 10}:p2", str(uuid.UUID(int=(index + 1) * 2 + 1)))})
        with self.assertRaisesRegex(hs.SupervisorError, "already holds 64"):
            self.register("One too many", ALPHA)
        self.assertEqual(len(hpair.list_entries(self.paths)), 64)

    def test_malformed_registries_fail_closed_and_are_never_rewritten(self) -> None:
        self.register("Alpha", ALPHA)
        path = hpair.registry_path(self.paths)
        good = path.read_text()
        entry = json.loads(good)["entries"]
        pair_id = next(iter(entry))
        broken = {
            "not json": "{",
            "list": "[]",
            "extra key": json.dumps({**json.loads(good), "extra": 1}),
            "bool revision": json.dumps({**json.loads(good), "revision": True}),
            "wrong schema": json.dumps({**json.loads(good), "schema_version": 2}),
            "entry id mismatch": json.dumps({**json.loads(good), "entries": {str(uuid.uuid4()): entry[pair_id]}}),
            "version 2": json.dumps({**json.loads(good), "entries": {pair_id: {**entry[pair_id], "version": 2}}}),
            "bool version": json.dumps({**json.loads(good), "entries": {pair_id: {**entry[pair_id], "version": True}}}),
            "bad name key": json.dumps({**json.loads(good), "entries": {pair_id: {**entry[pair_id], "name_key": "other"}}}),
            "unknown entry key": json.dumps({**json.loads(good), "entries": {pair_id: {**entry[pair_id], "owner": "x"}}}),
            "created without prep": json.dumps({**json.loads(good), "entries": {pair_id: {**entry[pair_id], "source": "created"}}}),
        }
        for label, text in broken.items():
            path.write_text(text)
            for operation in (lambda: hpair.list_entries(self.paths), lambda: self.register("Beta", BETA), lambda: hpair.get_entry(self.paths, pair_id)):
                with self.subTest(label), self.assertRaises(hs.SupervisorError):
                    operation()
            self.assertEqual(path.read_text(), text, "a malformed registry is reported, never repaired or overwritten")
        path.write_text(good)

    def test_duplicate_names_and_reused_sessions_inside_a_document_are_rejected(self) -> None:
        a = self.register("Alpha", ALPHA)
        b = self.register("Beta", BETA, self.repo_b, self.review_b)
        doc = self.registry_doc()
        duplicate_name = json.loads(json.dumps(doc))
        duplicate_name["entries"][b["pair_id"]]["name"] = "Alpha"
        duplicate_name["entries"][b["pair_id"]]["name_key"] = "alpha"
        reused = json.loads(json.dumps(doc))
        reused["entries"][b["pair_id"]]["identities"]["claude"]["session_id"] = a["identities"]["claude"]["session_id"]
        for label, value in (("duplicate name", duplicate_name), ("reused session", reused)):
            hpair.registry_path(self.paths).write_text(json.dumps(value))
            with self.subTest(label), self.assertRaises(hs.SupervisorError):
                hpair.list_entries(self.paths)

    def test_a_symlinked_or_non_regular_registry_is_refused(self) -> None:
        self.register("Alpha", ALPHA)
        path = hpair.registry_path(self.paths)
        real = self.state_dir / "elsewhere.json"
        real.write_text(path.read_text())
        path.unlink()
        path.symlink_to(real)
        with self.assertRaisesRegex(hs.SupervisorError, "regular file"):
            hpair.list_entries(self.paths)

    def test_simultaneous_registrations_of_one_name_commit_exactly_one(self) -> None:
        barrier = threading.Barrier(6)
        outcomes: list = []

        def attempt(index: int) -> None:
            spec = {"claude": (f"w{40 + index}:p1", str(uuid.UUID(int=1000 + index * 2))), "codex": (f"w{40 + index}:p2", str(uuid.UUID(int=1001 + index * 2)))}
            barrier.wait()
            try:
                outcomes.append(self.register("Contended", spec))
            except hs.SupervisorError as error:
                outcomes.append(error)

        threads = [threading.Thread(target=attempt, args=(i,)) for i in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        winners = [o for o in outcomes if isinstance(o, dict)]
        self.assertEqual(len(winners), 1, outcomes)
        self.assertEqual(len(hpair.list_entries(self.paths)), 1)
        self.assertEqual(self.registry_doc()["revision"], 1, "one committed write")

    def test_replaying_a_registration_with_the_same_pair_id_is_idempotent_and_different_content_is_refused(self) -> None:
        pair_id = str(uuid.uuid4())
        first = self.register("Alpha", ALPHA, pair_id=pair_id)
        again = self.register("Alpha", ALPHA, pair_id=pair_id)
        self.assertEqual(first, again)
        self.assertEqual(self.registry_doc()["revision"], 1)
        with self.assertRaisesRegex(hs.SupervisorError, "different content"):
            self.register("Alpha", ALPHA, self.repo_b, self.review_b, pair_id=pair_id)


class ReservationTests(PairCase):
    def reserve(self, name: str = "Fresh", prep: str = PREP, binding: str = "a" * 64) -> dict:
        return hpair.reserve_name(self.paths, self.clock.time, name=name, preparation_id=prep, binding=binding)

    def test_a_name_belongs_to_exactly_one_creation_request(self) -> None:
        first = self.reserve()
        self.assertEqual(self.reserve(), first, "the same request is idempotent")
        for prep, binding in ((PREP2, "a" * 64), (PREP, "b" * 64)):
            with self.subTest(prep=prep, binding=binding[:2]), self.assertRaisesRegex(hs.SupervisorError, "reserved by another"):
                self.reserve(prep=prep, binding=binding)
        with self.assertRaisesRegex(hs.SupervisorError, "reserved by another|already uses"):
            self.reserve(name="FRESH", prep=PREP2)
        self.register("Taken", ALPHA)
        with self.assertRaisesRegex(hs.SupervisorError, "already uses that name"):
            self.reserve(name="taken", prep=PREP2)

    def test_publish_needs_the_reservation_and_is_replay_safe(self) -> None:
        with self.assertRaisesRegex(hs.SupervisorError, "not reserved"):
            hpair.publish_created(self.paths, self.config, self.clock.time, preparation_id=PREP, name="Fresh", context=self.ctx(self.repo_a, self.review_a), identities=ids(ALPHA))
        self.reserve()
        entry = hpair.publish_created(self.paths, self.config, self.clock.time, preparation_id=PREP, name="Fresh", context=self.ctx(self.repo_a, self.review_a), identities=ids(ALPHA))
        self.assertEqual((entry["source"], entry["preparation_id"]), ("created", PREP))
        self.assertEqual(self.registry_doc()["reservations"], {})
        revision = self.registry_doc()["revision"]
        again = hpair.publish_created(self.paths, self.config, self.clock.time, preparation_id=PREP, name="Fresh", context=self.ctx(self.repo_a, self.review_a), identities=ids(ALPHA))
        self.assertEqual(again, entry)
        self.assertEqual(self.registry_doc()["revision"], revision, "a replayed publish writes nothing")
        with self.assertRaisesRegex(hs.SupervisorError, "different content"):
            hpair.publish_created(self.paths, self.config, self.clock.time, preparation_id=PREP, name="Fresh", context=self.ctx(self.repo_a, self.review_a), identities=ids(BETA))
        self.assertEqual(self.reserve(), {"name": "Fresh", "preparation_id": PREP, "binding": "a" * 64, "reserved_at": entry["created_at"], "published": True})

    def test_release_frees_only_the_reservation_never_an_entry(self) -> None:
        self.reserve()
        self.assertTrue(hpair.release_reservation(self.paths, self.clock.time, PREP))
        self.assertFalse(hpair.release_reservation(self.paths, self.clock.time, PREP))
        self.reserve(prep=PREP2)
        entry = self.register("Kept", BETA, self.repo_b, self.review_b)
        self.assertFalse(hpair.release_reservation(self.paths, self.clock.time, entry["pair_id"]))
        self.assertIsNotNone(hpair.get_entry(self.paths, entry["pair_id"]))

    def test_simultaneous_reservations_of_one_name_leave_one_owner(self) -> None:
        barrier = threading.Barrier(5)
        outcomes: list = []

        def attempt(index: int) -> None:
            barrier.wait()
            try:
                outcomes.append(self.reserve(prep=str(uuid.UUID(int=500 + index)), binding=f"{index}" * 64))
            except hs.SupervisorError as error:
                outcomes.append(error)

        threads = [threading.Thread(target=attempt, args=(i,)) for i in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual(len([o for o in outcomes if isinstance(o, dict)]), 1, outcomes)
        self.assertEqual(len(self.registry_doc()["reservations"]), 1)


class SelectionShapeTests(PairCase):
    def test_legacy_selection_normalisation_is_unchanged_and_pair_is_additive(self) -> None:
        legacy = herdr_sessions.validate_selection(self.config, {"policy": "fresh-all", "profiles": {"codex": "default", "claude": "default"}})
        self.assertEqual(legacy, {"policy": "fresh-all", "profiles": {"codex": "default", "claude": "default"}, "fresh_providers": ["codex", "claude"]})
        self.assertNotIn("pair", legacy)
        entry = self.register("Alpha", ALPHA)
        selection = herdr_sessions.validate_selection(self.config, self.saved(entry))
        self.assertEqual(herdr_sessions.validate_selection(self.config, selection), selection, "normalisation is idempotent (journals compare equal)")
        self.assertEqual(selection["fresh_providers"], [])

    def test_pair_modes_have_exact_keys_types_and_policies(self) -> None:
        entry = self.register("Alpha", ALPHA)
        saved, existing, creation = self.saved(entry), self.existing("Gamma", BETA), self.creation("Delta")
        cases = {
            "saved with name": (saved, lambda s: s["pair"].update(name="x")),
            "saved bad fingerprint": (saved, lambda s: s["pair"].update(fingerprint="zz")),
            "saved bool id": (saved, lambda s: s["pair"].update(pair_id=True)),
            "saved with fresh policy": (saved, lambda s: s.update(policy="fresh-all")),
            "existing with fresh policy": (existing, lambda s: s.update(policy="fresh-codex")),
            "existing without identities": (existing, lambda s: s["pair"].pop("identities")),
            "existing extra key": (existing, lambda s: s["pair"].update(extra=1)),
            "create with preserve": (creation, lambda s: s.update(policy="preserve")),
            "create with partial fresh": (creation, lambda s: s.update(policy="fresh-claude")),
            "create with identities": (creation, lambda s: s["pair"].update(identities=ids(BETA))),
            "unknown mode": (saved, lambda s: s["pair"].update(mode="rename")),
            "pair not an object": (saved, lambda s: s.update(pair="Alpha")),
            "pair list": (saved, lambda s: s.update(pair=[])),
            "profile for a preserved provider": (saved, lambda s: s["profiles"].update(codex="large")),
        }
        for label, (base, mutate) in cases.items():
            value = json.loads(json.dumps(base))
            mutate(value)
            with self.subTest(label), self.assertRaises(hs.SupervisorError):
                herdr_sessions.validate_selection(self.config, value)
        for good in (saved, existing, creation):
            herdr_sessions.validate_selection(self.config, good)

    def test_state_validation_binds_the_frozen_pair_to_its_selection(self) -> None:
        entry = self.register("Alpha", ALPHA)
        binding = hpair.binding_from_entry(entry, bound_at="2026-01-01T00:00:00Z")
        self.assertEqual(hpair.validate_binding(binding), binding)
        moved = json.loads(json.dumps(binding))
        moved["identities"]["claude"]["pane_id"] = "w9:p9"
        self.assertEqual(hpair.validate_binding(moved), moved, "a locator re-recorded by the bound owner repair keeps the same sessions and context")
        for label, mutate in (("tampered session", lambda b: b["identities"]["claude"].update(session_id=str(uuid.uuid4()))), ("tampered context", lambda b: b["context"].update(repository="/x")),
                              ("extra key", lambda b: b.update(extra=1)), ("bool version", lambda b: b.update(version=True)), ("bad source", lambda b: b.update(source="other")),
                              ("name changed", lambda b: b.update(name="Other"))):
            value = json.loads(json.dumps(binding))
            mutate(value)
            with self.subTest(label), self.assertRaises(hs.SupervisorError):
                hpair.validate_binding(value)
        state = hs.new_v2_fields("gated_v2")
        state.update(schema_version=2, run_id=str(uuid.uuid4()), supervisor_state="RUNNING", session_selection=herdr_sessions.validate_selection(self.config, self.saved(entry)), session_pair=binding)
        hs.validate_state_v2(state)
        state["session_pair"] = {**binding, "pair_id": str(uuid.uuid4())}
        with self.assertRaises(hs.SupervisorError):
            hs.validate_state_v2(state)
        tampered = json.loads(json.dumps(binding))
        tampered["identities"]["codex"]["session_id"] = str(uuid.uuid4())
        state["session_pair"] = tampered
        with self.assertRaisesRegex(hs.SupervisorError, "does not match its recorded sessions"):
            hs.validate_state_v2(state)  # a hand-edited run state cannot silently retarget the run to another conversation
        state["session_pair"] = binding
        state["session_selection"] = herdr_sessions.validate_selection(self.config, {"policy": "preserve", "profiles": {"codex": "default", "claude": "default"}})
        with self.assertRaisesRegex(hs.SupervisorError, "does not match the pair selection"):
            hs.validate_state_v2(state)


if __name__ == "__main__":
    import unittest

    unittest.main()
