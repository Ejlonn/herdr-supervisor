"""Creating and naming a fresh pair reuses the journaled fresh-all path with a named-registration destination: the name is reserved by the exact request,
the pair is published only after both fresh native identities verify, a crash/replay/second tap never creates another pair, partial creation preserves every
old session and every new pane, and the global owners.json is never written. Fake Herdr only; no real creation."""

from __future__ import annotations

import json
import uuid

import herdr_pairs as hpair
import herdr_sessions
from pair_fixtures import ALPHA, PairCase, hs, ids
from v2_fixtures import CLAUDE_SESSION, CODEX_SESSION


class CreateCase(PairCase):
    def setUp(self) -> None:
        super().setUp()
        self.write_plan()
        self.sup.model_flag_supported = lambda provider: True

    def prepare(self, selection: dict, prep: str | None = None) -> dict:
        return self.sup.prepare_task_sessions(selection, preparation_id=prep or str(uuid.uuid4()))

    def journal(self, prep: str) -> dict:
        return json.loads((self.paths.session_preparations_dir / f"{prep}.json").read_text())

    def assert_old_sessions_untouched(self) -> None:
        self.assertTrue(self.owners_unchanged())
        for name, session in (("codex-main", CODEX_SESSION), ("claude-main", CLAUDE_SESSION)):
            self.assertEqual(self.herdr.agents[name]["agent_session"]["value"], session)
        self.assertEqual([r for r in self.herdr.renames if r[0] in ("w3:p1", "w3:p2")], [], "no old pane was relabelled (only the NEW panes carry labels)")
        self.assertEqual([r for r in self.herdr.agent_renames if r[0] in ("w3:p1", "w3:p2", "codex-main", "claude-main")], [], "no old agent was renamed")


class SuccessTests(CreateCase):
    def test_a_created_pair_is_published_after_verification_and_owners_json_is_untouched(self) -> None:
        prep = str(uuid.uuid4())
        record = self.prepare(self.creation("Fresh Pair", self.repo_b, self.review_b), prep)
        self.assertEqual(record["status"], "OWNERSHIP_BOUND")
        self.assertEqual(record["destination"]["name"], "Fresh Pair")
        entry = hpair.entry_for_preparation(self.paths, prep)
        self.assertEqual(entry["source"], "created")
        self.assertEqual(entry["identities"], record["bound_owners"])
        self.assertNotEqual({p: v["session_id"] for p, v in entry["identities"].items()}, {"codex": CODEX_SESSION, "claude": CLAUDE_SESSION})
        self.assertEqual(entry["context"], self.ctx(self.repo_b, self.review_b))
        self.assertEqual(self.registry_doc()["reservations"], {}, "the reservation became the entry")
        self.assert_old_sessions_untouched()
        self.assertEqual(len(self.herdr.splits), 2, "one pane per provider, exactly once")
        self.assertEqual(self.herdr.prompts, [], "creation sends no task")

    def test_a_started_run_on_a_created_pair_reaches_only_the_new_sessions(self) -> None:
        selection = self.creation("Fresh Pair")
        result = self.start(selection)
        self.assertTrue(result["ok"], result)
        self.herdr.responses = self.plan_for(selection)
        self.sup.resume()
        state = self.state()
        self.assertEqual(state["session_pair"]["name"], "Fresh Pair")
        self.assertEqual(state["session_pair"]["source"], "created")
        self.assertEqual(state["session_preparation"]["status"], "TASK_STARTED")
        self.assertEqual([name for name, _ in self.herdr.prompts], [f"codex-run-{state['run_id'][:8]}"])
        self.assert_old_sessions_untouched()
        self.assertEqual(state["native_sessions"], {p: v["session_id"] for p, v in state["session_pair"]["identities"].items()})

    def test_the_legacy_fresh_all_still_replaces_owners_json_so_the_destination_is_the_only_difference(self) -> None:
        record = self.prepare({"policy": "fresh-all", "profiles": {"codex": "default", "claude": "default"}})
        self.assertNotIn("destination", record)
        self.assertFalse(self.owners_unchanged())
        self.assertEqual(self.sup.owners(), record["bound_owners"])
        self.assertFalse(hpair.registry_path(self.paths).exists())

    def test_replaying_a_finished_creation_returns_the_same_journal_and_creates_nothing_more(self) -> None:
        prep = str(uuid.uuid4())
        first = self.prepare(self.creation("Fresh Pair"), prep)
        counts = (len(self.herdr.splits), len(self.herdr.starts), len(self.herdr.pane_runs), self.registry_doc()["revision"])
        again = self.prepare(self.creation("Fresh Pair"), prep)
        self.assertEqual(again["bound_owners"], first["bound_owners"])
        self.assertEqual((len(self.herdr.splits), len(self.herdr.starts), len(self.herdr.pane_runs), self.registry_doc()["revision"]), counts)
        self.assertEqual(len(hpair.list_entries(self.paths)), 1)


class RefusalBeforeSideEffectsTests(CreateCase):
    def assert_nothing_happened(self) -> None:
        self.assertEqual((self.herdr.splits, self.herdr.starts, self.herdr.pane_runs, self.herdr.prompts), ([], [], [], []))
        self.assertEqual(self.registry_doc()["reservations"] if hpair.registry_path(self.paths).exists() else {}, {})
        self.assert_old_sessions_untouched()
        self.assertFalse(list(self.paths.session_preparations_dir.glob("*.json")) if self.paths.session_preparations_dir.exists() else [])

    def test_disabled_context_outside_roots_and_taken_names_refuse_before_any_pane_exists(self) -> None:
        self.config["session_pairs"]["enabled"] = False
        with self.assertRaisesRegex(hs.SupervisorError, "not enabled"):
            self.prepare(self.creation("X"))
        self.config["session_pairs"]["enabled"] = True
        outside = self.creation("X")
        outside["pair"]["context"]["repository"] = str(self.project_root.parent)
        with self.assertRaisesRegex(hs.SupervisorError, "outside the administrator-allowed roots"):
            self.prepare(outside)
        self.assert_nothing_happened()
        self.register("Taken", ALPHA)
        with self.assertRaisesRegex(hs.SupervisorError, "already uses that name"):
            self.prepare(self.creation("taken"))
        self.assertEqual((self.herdr.splits, self.herdr.starts, self.herdr.pane_runs), ([], [], []))
        self.assertEqual(self.registry_doc()["reservations"], {})

    def test_creation_is_only_a_supervisor_flow_not_a_cli_flow(self) -> None:
        with self.assertRaisesRegex(hs.SupervisorError, "supervisor task flow"):
            self.sup.run_new("cli task", "codex", session_selection=self.creation("X"))
        with self.assertRaisesRegex(hs.SupervisorError, "supervisor task flow"):
            self.sup.run_new("cli task", "codex", session_selection=self.saved(self.register("Alpha", ALPHA)))
        self.assertEqual((self.herdr.splits, self.herdr.starts, self.herdr.prompts), ([], [], []))

    def test_an_unreadable_registry_stops_creation_before_a_pane_exists(self) -> None:
        hpair.registry_path(self.paths).write_text("{")
        with self.assertRaises(hs.SupervisorError):
            self.prepare(self.creation("X"))
        self.assertEqual((self.herdr.splits, self.herdr.starts, self.herdr.pane_runs), ([], [], []))


class PartialFailureTests(CreateCase):
    def test_a_failed_launch_publishes_nothing_keeps_the_name_reserved_and_preserves_everything_created(self) -> None:
        prep = str(uuid.uuid4())
        self.herdr.run_mode = "lost"  # the Codex process never binds: creation stops after the panes exist
        with self.assertRaises(hs.SupervisorError):
            self.prepare(self.creation("Fresh Pair"), prep)
        self.assertEqual(hpair.list_entries(self.paths), [], "no half pair is ever published")
        self.assertEqual(list(self.registry_doc()["reservations"]), ["fresh pair"])
        self.assertNotEqual(self.journal(prep)["status"], "OWNERSHIP_BOUND")
        self.assert_old_sessions_untouched()
        panes = len(self.herdr.splits)
        self.assertGreaterEqual(panes, 1, "the new pane is preserved, not cleaned up")
        with self.assertRaisesRegex(hs.SupervisorError, "is unresolved"):
            self.prepare(self.creation("fresh pair"), str(uuid.uuid4()))
        self.assertEqual(len(self.herdr.splits), panes, "another tap with the same name created nothing")
        with self.assertRaisesRegex(hs.SupervisorError, "reserved by another creation request"):
            hpair.reserve_name(self.paths, self.clock.time, name="fresh pair", preparation_id=str(uuid.uuid4()), binding="f" * 64)
        # a second tap of the SAME request resumes its own journal: it never splits a second pane for a provider that already has one
        self.herdr.run_mode = "ok"
        try:
            self.prepare(self.creation("Fresh Pair"), prep)
        except hs.SupervisorError:
            pass
        self.assertLessEqual(len(self.herdr.splits), 2, "at most one pane per provider for this journal")
        self.assertLessEqual(len(hpair.list_entries(self.paths)), 1)

    def test_abandoning_the_failed_attempt_returns_the_name_and_a_new_request_can_use_it(self) -> None:
        prep = str(uuid.uuid4())
        self.herdr.run_mode = "lost"
        with self.assertRaises(hs.SupervisorError):
            self.prepare(self.creation("Fresh Pair"), prep)
        self.assertEqual(len(self.registry_doc()["reservations"]), 1)
        self.sup.abandon_session_preparation(prep)
        self.assertEqual(self.registry_doc()["reservations"], {})
        self.assertEqual(self.journal(prep)["status"], "ABANDONED")
        self.herdr.run_mode = "ok"
        self.herdr.next_sessions = {"codex": "77777777-7777-4777-8777-777777777777", "claude": "88888888-8888-4888-8888-888888888888"}
        second = self.prepare(self.creation("Fresh Pair"), str(uuid.uuid4()))
        self.assertEqual(second["status"], "OWNERSHIP_BOUND")
        self.assertEqual([e["name"] for e in hpair.list_entries(self.paths)], ["Fresh Pair"])

    def test_a_crash_after_the_registry_publication_replays_to_the_same_single_pair(self) -> None:
        prep = str(uuid.uuid4())
        real = herdr_sessions.atomic_write_json

        def crash_when_bound(path, value, **kwargs):
            if isinstance(value, dict) and value.get("status") == "OWNERSHIP_BOUND" and value.get("preparation_id") == prep:
                raise OSError("simulated crash after the registry commit")
            return real(path, value, **kwargs)

        herdr_sessions.atomic_write_json = crash_when_bound
        try:
            with self.assertRaises(OSError):
                self.prepare(self.creation("Fresh Pair"), prep)
        finally:
            herdr_sessions.atomic_write_json = real
        self.assertEqual(self.journal(prep)["status"], "BINDING")
        (entry,) = hpair.list_entries(self.paths)
        counts = (len(self.herdr.splits), len(self.herdr.starts), len(self.herdr.pane_runs), self.registry_doc()["revision"])
        record = self.prepare(self.creation("Fresh Pair"), prep)
        self.assertEqual(record["status"], "OWNERSHIP_BOUND")
        self.assertEqual(record["bound_owners"], entry["identities"])
        self.assertEqual((len(self.herdr.splits), len(self.herdr.starts), len(self.herdr.pane_runs), self.registry_doc()["revision"]), counts)
        self.assertEqual(len(hpair.list_entries(self.paths)), 1)
        self.assert_old_sessions_untouched()

    def test_a_crash_before_the_registry_publication_replays_without_creating_another_pair(self) -> None:
        prep = str(uuid.uuid4())
        real = hpair.publish_created
        hpair.publish_created = lambda *a, **k: (_ for _ in ()).throw(OSError("simulated crash before the registry commit"))
        try:
            with self.assertRaises(OSError):
                self.prepare(self.creation("Fresh Pair"), prep)
        finally:
            hpair.publish_created = real
        self.assertEqual(self.journal(prep)["status"], "BINDING")
        self.assertEqual(hpair.list_entries(self.paths), [])
        self.assertTrue(self.owners_unchanged())
        counts = (len(self.herdr.splits), len(self.herdr.starts), len(self.herdr.pane_runs))
        record = self.prepare(self.creation("Fresh Pair"), prep)
        self.assertEqual(record["status"], "OWNERSHIP_BOUND")
        self.assertEqual((len(self.herdr.splits), len(self.herdr.starts), len(self.herdr.pane_runs)), counts, "the replay completed the binding; it created nothing")
        self.assertEqual(len(hpair.list_entries(self.paths)), 1)

    def test_a_changed_selection_cannot_reuse_the_journal_of_another_creation(self) -> None:
        prep = str(uuid.uuid4())
        self.prepare(self.creation("Fresh Pair"), prep)
        with self.assertRaisesRegex(hs.SupervisorError, "binding changed|destination|already"):
            self.prepare(self.creation("Other Name"), prep)
        self.assertEqual(len(hpair.list_entries(self.paths)), 1)

    def test_global_owners_that_change_during_creation_stop_publication(self) -> None:
        prep = str(uuid.uuid4())
        real = hpair.publish_created

        def drift_then_publish(*args, **kwargs):
            hs.atomic_write_json(self.paths.owners_file, ids(ALPHA), mode=0o600)
            return real(*args, **kwargs)

        hpair.publish_created = drift_then_publish
        try:
            self.prepare(self.creation("Fresh Pair"), prep)
        except hs.SupervisorError:
            pass
        finally:
            hpair.publish_created = real
        # the publication above happened inside the BINDING step; a replay must now notice the changed global owners and refuse to report a bound pair
        with self.assertRaisesRegex(hs.SupervisorError, "global owners changed|changed"):
            self.prepare(self.creation("Fresh Pair"), prep)


class JournalValidationTests(CreateCase):
    def test_destination_must_match_the_pair_selection(self) -> None:
        prep = str(uuid.uuid4())
        self.prepare(self.creation("Fresh Pair"), prep)
        self.sup._mark_session_preparation_started(prep)
        record = self.journal(prep)
        for label, mutate in (("dropped", lambda r: r.pop("destination")), ("renamed", lambda r: r["destination"].update(name="Other")),
                              ("bad binding", lambda r: r["destination"].update(binding="0" * 64)), ("extra key", lambda r: r["destination"].update(extra=1)),
                              ("wrong kind", lambda r: r["destination"].update(kind="owners"))):
            value = json.loads(json.dumps(record))
            mutate(value)
            with self.subTest(label), self.assertRaises(hs.SupervisorError):
                herdr_sessions.validate_preparation_record(self.config, value, prep)
        legacy = {"policy": "fresh-all", "profiles": {"codex": "default", "claude": "default"}}
        self.herdr.next_sessions = {"codex": "77777777-7777-4777-8777-777777777777", "claude": "88888888-8888-4888-8888-888888888888"}
        legacy_prep = str(uuid.uuid4())
        self.prepare(legacy, legacy_prep)
        with_dest = self.journal(legacy_prep)
        with_dest["destination"] = {"kind": "named", "name": "x", "binding": "0" * 64}
        with self.assertRaises(hs.SupervisorError):
            herdr_sessions.validate_preparation_record(self.config, with_dest, legacy_prep)


if __name__ == "__main__":
    import unittest

    unittest.main()
