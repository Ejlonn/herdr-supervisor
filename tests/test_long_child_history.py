"""Whole-history capacity regression tests; no real providers or session writes."""
import json
import os
from unittest import mock

import herdr_codex_children as cc
import herdr_native_results as nr
from test_codex_child_guard import RUN, TURN, Base, P, hp_frame, rec, rollout_path, write_lines
from test_helper_profile_policy import ProfileBuilder
from test_named_pair_child_guard import NamedPairChildGuardTests


def pad(path, mib=9):
    """Place benign large records near the end, leaving early policy evidence outside an 8MiB tail."""
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    padding = rec("event_msg", {"type": "token_count", "padding": "x" * (512 * 1024)}, None)
    rows[-2:-2] = [padding] * (mib * 2)
    write_lines(path, rows)
    assert path.stat().st_size > mib * 1024 * 1024


class LongHistories(ProfileBuilder, Base):
    def test_large_transparent_history_and_ordinary_budget_are_independent(self):
        made = self.world.build(spawn="none", child=False, mailbox="none")
        pad(made["parent"])
        self.assertEqual(cc.MAX_HISTORY_BYTES, 64 * 1024 * 1024)
        self.assertEqual(nr.MAX_SCAN_BYTES, 8 * 1024 * 1024)
        with mock.patch.object(nr, "MAX_SCAN_BYTES", 1):
            self.assertEqual(cc.verify(P, self.root).kind, cc.TRANSPARENT)

    def test_large_approved_profiles_native_final(self):
        for role, model, effort, after in (("repository_explorer", "gpt-6-luna", "max", True),
                                          ("repository_reviewer", "gpt-6.1-sol", "high", True)):
            with self.subTest(role=role, after=after):
                made = self.make(role, model, effort, after=after, prompt=f"Run {RUN} turn {TURN}", final=hp_frame())
                pad(made["parent"])
                pad(made["child"], 1)
                result = cc.verify(P, self.root, route=(RUN, TURN))
                self.assertEqual((result.kind, result.native.status), (cc.COMPLETE, nr.FOUND))
                self.assertEqual(result.native.block.run_id, RUN)
                self.assertEqual(result.native.provenance["offset_start"], 0)

    def test_second_early_spawn_cannot_hide_outside_tail(self):
        made = self.world.build(second_spawn=True)
        pad(made["parent"])
        result = cc.verify(P, self.root)
        self.assertEqual(result.kind, cc.VIOLATION)
        self.assertIn("2", result.detail)

    def test_early_failed_spawn_is_not_zero(self):
        made = self.world.build(spawn_output="refused", child=False, mailbox="none")
        pad(made["parent"])
        result = cc.verify(P, self.root)
        self.assertFalse(result.clear)
        self.assertEqual(result.evidence["spawn_calls"], 1)

    def test_early_malformed_or_unsupported_record_cannot_hide(self):
        for raw in (b"{invalid}\n", b'{"type":"unsupported","payload":{}}\n'):
            made = self.world.build()
            pad(made["parent"])
            body = made["parent"].read_bytes().split(b"\n", 1)
            made["parent"].write_bytes(body[0] + b"\n" + raw + body[1])
            self.assertEqual(cc.verify(P, self.root).kind, cc.UNKNOWN)

    def test_large_partial_or_oversized_line_is_unknown(self):
        made = self.world.build()
        pad(made["parent"])
        with made["parent"].open("ab") as stream:
            stream.write(b"{")
        self.assertEqual(cc.verify(P, self.root).kind, cc.UNKNOWN)
        made = self.world.build()
        pad(made["parent"])
        with made["parent"].open("ab") as stream:
            stream.write(b"x" * (nr.MAX_LINE_BYTES + 1) + b"\n")
        self.assertEqual(cc.verify(P, self.root).kind, cc.UNKNOWN)

    def test_large_history_clock_and_records_remain_bounded(self):
        made = self.world.build()
        pad(made["parent"])
        with mock.patch.object(nr, "MAX_RECORDS", 20):
            result = cc.verify(P, self.root)
            self.assertEqual(result.kind, cc.UNKNOWN)
            self.assertIn("record", result.detail)
        ticks = [0]
        def clock():
            ticks[0] += 1
            return ticks[0] * 0.2
        result = cc.verify(P, self.root, clock=clock)
        self.assertEqual(result.kind, cc.UNKNOWN)
        self.assertIn("seconds", result.detail)

    def test_large_changed_file_is_unknown(self):
        made = self.world.build()
        pad(made["parent"])
        original = cc._analyze_parent
        def change(*args, **kwargs):
            result = original(*args, **kwargs)
            os.utime(made["parent"], ns=(1, 1))
            return result
        with mock.patch.object(cc, "_analyze_parent", side_effect=change):
            self.assertEqual(cc.verify(P, self.root).kind, cc.UNKNOWN)


class LargePairStart(NamedPairChildGuardTests):
    def response(self, *args, **kwargs):
        result = super().response(*args, **kwargs)
        previous = result["before"]
        def large(fake, name, text):
            previous(fake, name, text)
            pad(rollout_path(self.codex_native_root, self.parent))
        result["before"] = large
        return result

    def test_repeated_resume_does_not_resend_accepted_turn(self):
        self.start_guarded()
        before = list(self.herdr.prompts)
        pending = self.state()["pending_gate"]
        self.sup.resume()
        self.assertEqual(self.herdr.prompts, before)
        self.assertEqual(self.state()["pending_gate"], pending)
        self.assertEqual(self.state()["session_pair"], self.bound)

    def test_uncertain_ack_with_large_native_result_never_resends(self):
        self.assertTrue(self.start(self.saved(self.alpha))["ok"])
        self.bound = self.state()["session_pair"]
        response = self.response()
        response["error"] = "timeout"
        self.herdr.responses = [response]
        self.sup.resume()
        self.assertEqual(len(self.herdr.prompts), 1)
        self.assertEqual(self.state()["pending_gate"]["gate_type"], "plan_approval")
        self.assertEqual(self.state()["pending_gate"]["status"], "pending")
        self.assertIsNone(self.state().get("approved_plan"), "native proof creates the gate, never approves it")
        self.assertEqual(self.state()["delivery"]["accepted_via"], "native_primary_final")
        self.assertEqual(len(self.herdr.prompts), 1)
        self.assertEqual(self.herdr.sent_keys, [])
        self.assertEqual(self.state()["session_pair"], self.bound)

    def test_oversize_guard_stops_before_delivery_and_repeats_same_reason(self):
        made = self.world.build(spawn="none", child=False, mailbox="none")
        pad(made["parent"])
        self.assertTrue(self.start(self.saved(self.alpha))["ok"])
        with mock.patch.object(cc, "MAX_HISTORY_BYTES", 8 * 1024 * 1024):
            for _ in range(2):
                self.sup.resume()
                state = self.state()
                self.assertEqual(state["supervisor_state"], "WAIT_USER")
                self.assertEqual(self.herdr.prompts, [])
                self.assertEqual(self.herdr.sent_keys, [])
                self.assertIsNone(state["pending_gate"])
                reason = state["wait_user_reason"]
                for wanted in ("8388608", state["run_id"], "Alpha", self.parent, "same stop repeats", "revise/cancel"):
                    self.assertIn(wanted, reason)
