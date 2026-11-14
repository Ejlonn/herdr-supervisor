"""Automatic permission-review threads are not task helpers; keep traversal."""

import herdr_codex_children as cc
from test_codex_child_guard import Base, C, G, P, rec, rollout_path, write_lines


class SystemReviewDiscovery(Base):
    def guardian(self, ident=C, parent=P, **changes):
        meta = {"id": ident, "session_id": parent, "parent_thread_id": parent,
                "thread_source": "guardian_review", "source": {"subagent": {"other": "guardian"}}}
        meta.update(changes)
        write_lines(rollout_path(self.root, ident), [rec("session_meta", meta, 1)])

    def test_observed_system_shape_does_not_consume_task_child_allowance(self):
        self.world.build(spawn="none", child=False, mailbox="none")
        self.guardian()
        self.guardian(G)
        result = cc.verify(P, self.root)
        self.assertEqual(result.kind, cc.TRANSPARENT)
        self.assertEqual((result.evidence["spawn_calls"], result.evidence["descendants"]), (0, 0))

    def test_real_task_child_still_counts_with_system_review(self):
        self.world.build()
        self.guardian(G)
        result = cc.verify(P, self.root)
        self.assertEqual(result.kind, cc.COMPLETE)
        self.assertEqual((result.evidence["spawn_calls"], result.evidence["descendants"]), (1, 1))

    def test_guardian_label_cannot_erase_recorded_spawn(self):
        self.world.build(child=False, mailbox="none")
        self.guardian()
        result = cc.verify(P, self.root)
        self.assertFalse(result.clear)
        self.assertEqual(result.evidence["spawn_calls"], 1)

    def test_real_child_under_system_review_is_still_discovered(self):
        self.world.build(spawn="none", mailbox="none", grandchild=True)
        self.guardian()
        result = cc.verify(P, self.root)
        self.assertEqual(result.kind, cc.VIOLATION)
        self.assertEqual(result.evidence["descendants"], 1)

    def test_partial_or_contradictory_system_markers_are_never_excluded(self):
        cases = ({"thread_source": "other_review"}, {"source": {"subagent": {"other": "other"}}},
                 {"source": {"subagent": {"other": "guardian", "thread_spawn": {"parent_thread_id": P}}}},
                 {"session_id": C}, {"agent_path": "/root/explorer"}, {"agent_role": "repository_explorer"})
        for changes in cases:
            with self.subTest(changes=changes):
                self.world.build(spawn="none", child=False, mailbox="none")
                self.guardian(**changes)
                result = cc.verify(P, self.root)
                self.assertEqual(result.kind, cc.VIOLATION)
                self.assertEqual(result.evidence["descendants"], 1)

    def test_unreadable_system_metadata_still_stops(self):
        self.world.build(spawn="none", child=False, mailbox="none")
        self.guardian()
        rollout_path(self.root, C).write_text('{"type":"session_meta"\n')
        self.assertEqual(cc.verify(P, self.root).kind, cc.UNKNOWN)
