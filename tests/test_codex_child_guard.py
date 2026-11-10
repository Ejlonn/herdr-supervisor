"""Codex transcript child guard: the bounded verifier (herdr_codex_children) and its REAL consumer wiring in the private runtime.

Offline only: synthetic native roots in temporary directories, a scripted FakeHerdr and FakeClock; no provider, no real session, no real HOME.
Evidence classes: record SHAPES are the retained 0.163.0 ones (parent function_call/output, mailbox FINAL_ANSWER, task_started/complete/aborted,
child session_meta/thread_settings_applied); the custom-role fields (`agent_type`/`agent_role` = repository_explorer) are SYNTHETIC — these older generated fixtures do not claim real custom-role execution; the separate0.166.0 fixtures carry observed custom-role evidence. Every adversary asserts counts (prompts, keys, routes, reads) and absence BEFORE settlement, not source text."""
from __future__ import annotations

import datetime as dt
import json
import os
import unittest
from pathlib import Path
from unittest import mock

import herdr_codex_children as cc  # noqa: E402
import herdr_native_results as nr  # noqa: E402
import herdr_protocol  # noqa: E402
import herdr_runtime as rt  # noqa: E402
from v2_fixtures import CODEX_SESSION, FakeHerdr, V2Case, hs  # noqa: I001 - first: puts the source layout on sys.path

P = CODEX_SESSION
C = "0a666666-6666-4666-8666-666666666666"
G = "0a777777-7777-4777-8777-777777777777"
C2 = "0a888888-8888-4888-8888-888888888888"
BASE = dt.datetime(2026, 11, 4, 10, 0, 0, tzinfo=dt.timezone.utc)
DAY = ("2026", "11", "04")
SRC = Path(__file__).resolve().parents[1] / "src"
RUN, TURN = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"      # the supervisor run/turn UUIDs (the parent's NATIVE turn id stays "t-1")


def hp_frame(run: str = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", turn: str = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb", gate: str = "none", stage: str = "brief", nxt: str = "claude", payload: str = "-") -> str:
    """A routing frame as a native final would carry it (no leading prose)."""
    return "\n".join(["HERDR_PROTOCOL=2", f"HERDR_RUN={run}", f"HERDR_TURN={turn}", f"HERDR_STAGE={stage}", f"HERDR_NEXT={nxt}", f"HERDR_GATE={gate}", f"HERDR_PAYLOAD={payload}", "HERDR_HANDOFF=fixture_handoff"])


def iso(offset: float) -> str:
    return (BASE + dt.timedelta(seconds=offset)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def rec(kind: str, payload: dict, at: float | None) -> dict:
    out = {"type": kind, "payload": payload}
    if at is not None:
        out["timestamp"] = iso(at)
    return out


def write_lines(path: Path, records: list, *, newline: bool = True, raw_tail: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "".join(json.dumps(r) + "\n" for r in records)
    path.write_text(body + raw_tail if newline else body.rstrip("\n"))


def rollout_path(root: Path, session: str) -> Path:
    return root.joinpath(*DAY) / f"rollout-2026-11-04T10-00-00-{session}.jsonl"


class World:
    """Builds a parent (and child) history under `root`. All timing is explicit offsets so ordering cases are exact."""

    T = dict(start=10, spawn=11, spawn_out=11.5, child_start=12, settings=12.1, child_done=14, mail=15, final=16, end=17)

    def __init__(self, root: Path, parent: str = P) -> None:
        self.root, self.parent = root, parent

    def parent_meta(self, **over) -> dict:
        return rec("session_meta", {"id": self.parent, "session_id": self.parent, "source": "cli", "thread_source": "user", "cli_version": "0.163.0", **over}, 0)

    def child_meta(self, child: str = C, *, parent: str | None = None, path: str = "/root/explorer", depth=1, role="repository_explorer", thread_source="subagent", **over) -> dict:
        spawn = {"parent_thread_id": parent or self.parent, "depth": depth, "agent_role": role}
        payload = {"id": child, "parent_thread_id": parent or self.parent, "thread_source": thread_source, "agent_path": path, "cli_version": "0.163.0", "source": {"subagent": {"thread_spawn": spawn}}}
        payload.update(over)
        return rec("session_meta", payload, 0.5)

    def build(self, *, prompt: str | None = None, final: str | None = None, turn: str = "t-1", **o) -> dict:
        """Writes the parent (and child) files. Options (defaults = the one compliant completed child):
        spawn: ok|none|model_only|wrong_role|bad_fork|extra_arg|bad_args|dup_call, spawn_output: ok|refused|none, second_spawn, prohibited, child (bool), child_complete, child_abort,
        child_error, child_final, child_turns, settings: ok|missing|wrong_model|wrong_effort|two, role, depth, path, grandchild, second_child, child_spawns, mailbox: ok|none|dup|other|early,
        parent_end: ok|early|open, drop_ts (record kinds without a timestamp), t (overrides)."""
        t = {**self.T, **o.get("t", {})}
        spawn_mode = o.get("spawn", "ok")
        parent = [self.parent_meta(), rec("event_msg", {"type": "task_started", "turn_id": turn, "root_turn_id": turn}, t["start"])]
        if prompt is not None:
            parent.append(rec("response_item", {"type": "message", "role": "user", "content": [{"type": "input_text", "text": prompt}]}, t["start"] + 0.1))
        args = {"agent_type": "repository_explorer", "task_name": "explorer", "fork_turns": "none", "message": "m"}
        if spawn_mode == "model_only":
            args = {"model": "gpt-6-luna", "reasoning_effort": "max", "task_name": "explorer", "fork_turns": "none", "message": "m"}
        elif spawn_mode == "no_role":
            del args["agent_type"]
        elif spawn_mode == "wrong_role":
            args["agent_type"] = "repository_review_lens"
        elif spawn_mode == "bad_fork":
            args["fork_turns"] = "all"
        elif spawn_mode == "extra_arg":
            args["model"] = "gpt-6.1-sol"
        call = {"type": "function_call", "namespace": "collaboration", "name": "spawn_agent", "arguments": json.dumps(args) if spawn_mode != "bad_args" else "{not json", "call_id": "call-1"}
        if spawn_mode != "none":
            parent.append(rec("response_item", call, t["spawn"]))
            if o.get("spawn_output", "ok") == "ok":
                parent.append(rec("response_item", {"type": "function_call_output", "call_id": "call-1", "output": json.dumps({"task_name": "/root/explorer"})}, t["spawn_out"]))
            elif o.get("spawn_output") == "refused":
                parent.append(rec("response_item", {"type": "function_call_output", "call_id": "call-1", "output": "collab spawn failed: agent thread limit reached"}, t["spawn_out"]))
        if spawn_mode == "dup_call":
            parent.append(rec("response_item", {**call, "name": "list_agents"}, t["spawn"] + 0.2))
        if o.get("second_spawn"):
            parent.append(rec("response_item", {**call, "call_id": "call-2"}, t["spawn"] + 0.3))
        if o.get("prohibited"):
            parent.append(rec("response_item", {"type": "function_call", "namespace": "collaboration", "name": o["prohibited"], "arguments": "{}", "call_id": "call-p"}, t["spawn"] + 0.4))
        parent.append(rec("response_item", {"type": "function_call", "namespace": "collaboration", "name": "wait_agent", "arguments": "{}", "call_id": "call-w"}, t["spawn"] + 0.5))
        parent.append(rec("response_item", {"type": "function_call_output", "call_id": "call-w", "output": json.dumps({"message": "Wait completed.", "timed_out": False})}, t["child_done"] + 0.2))
        mailbox = o.get("mailbox", "ok")
        text = "Message Type: FINAL_ANSWER\nTask name: /root\nSender: /root/explorer\nPayload:\nsynthetic result"
        msg = {"type": "agent_message", "author": "/root/explorer", "recipient": "/root", "content": [{"type": "input_text", "text": text}]}
        mail_at = t["mail"] if mailbox != "early" else t["child_done"] - 1
        if mailbox in ("ok", "dup", "early"):
            parent.append(rec("response_item", msg, mail_at))
        if mailbox == "dup":
            parent.append(rec("response_item", msg, mail_at + 0.1))
        if mailbox == "other":
            parent.append(rec("response_item", {**msg, "author": "/root/other", "content": [{"type": "input_text", "text": text.replace("/root/explorer", "/root/other")}]}, mail_at))
        if final is not None:
            parent.append(rec("response_item", {"type": "message", "id": "m1", "role": "assistant", "phase": "final_answer", "content": [{"type": "output_text", "text": final}],
                                                "internal_chat_message_metadata_passthrough": {"turn_id": turn}}, t["final"]))
        end = o.get("parent_end", "ok")
        if end == "ok":
            parent.append(rec("event_msg", {"type": "task_complete", "turn_id": turn}, t["end"]))
        elif end == "early":
            parent.insert(4 if prompt is not None else 3, rec("event_msg", {"type": "task_complete", "turn_id": turn}, t["spawn_out"] + 0.2))
        elif end == "aborted":
            parent.append(rec("event_msg", {"type": "turn_aborted", "turn_id": turn}, t["end"]))
        for extra in o.get("extra_parent", []):
            parent.insert(len(parent) - 1 if end == "ok" else len(parent), extra)
        for index, r in enumerate(parent):
            if isinstance(r.get("payload"), dict) and r["payload"].get("type") in o.get("drop_ts", ()):
                parent[index] = {k: v for k, v in r.items() if k != "timestamp"}
        write_lines(rollout_path(self.root, self.parent), parent)
        made = {"parent": rollout_path(self.root, self.parent)}
        if o.get("child", True) and spawn_mode != "none":
            made["child"] = self.write_child(C, o, t)
        elif o.get("child") is True and spawn_mode == "none":
            made["child"] = self.write_child(C, o, t)
        if o.get("grandchild"):
            made["grandchild"] = self.write_child(G, o, t, parent=C, path="/root/explorer/deep", depth=2)
        if o.get("second_child"):
            made["second_child"] = self.write_child(C2, o, t, path="/root/explorer2")
        return made

    def write_child(self, child: str, o: dict, t: dict, *, parent: str | None = None, path: str | None = None, depth: int | None = None) -> Path:
        meta = self.child_meta(child, parent=parent, path=path or o.get("path", "/root/explorer"), depth=depth if depth is not None else o.get("depth", 1), role=o.get("role", "repository_explorer"))
        records = [meta]
        settings = o.get("settings", "ok")
        model, effort = {"ok": ("gpt-6-luna", "max"), "wrong_model": ("gpt-6.1-sol", "max"), "wrong_effort": ("gpt-6-luna", "low")}.get(settings, ("gpt-6-luna", "max"))
        if settings != "missing":
            applied = {"type": "thread_settings_applied", "thread_settings": {"model": model, "collaboration_mode": {"settings": {"reasoning_effort": effort}}}}
            records.append(rec("event_msg", applied, t["settings"]))
            if settings == "two":
                records.append(rec("event_msg", {"type": "thread_settings_applied", "thread_settings": {"model": "gpt-6.1-sol", "collaboration_mode": {"settings": {"reasoning_effort": "high"}}}}, t["settings"] + 0.05))
        for turn_no in range(int(o.get("child_turns", 1))):
            turn = f"ct-{turn_no}"
            started = {"type": "task_started", "turn_id": turn, "root_turn_id": "t-1", "turn_attribution": {"parent_turn_id": "t-1", "root_turn_id": "t-1"}}      # the retained real shape
            attr = o.get("attr", "ok")
            if attr == "wrong":
                started["root_turn_id"], started["turn_attribution"] = "unrelated", {"parent_turn_id": "unrelated", "root_turn_id": "unrelated"}
            elif attr == "wrong_parent_turn":
                started["turn_attribution"]["parent_turn_id"] = "unrelated"
            elif attr == "wrong_root":
                started["root_turn_id"] = "unrelated"
            elif attr == "absent":
                del started["root_turn_id"], started["turn_attribution"]
            elif attr == "bad_type":
                started["turn_attribution"] = ["parent"]
            records.append(rec("event_msg", started, t["child_start"] + turn_no))
            if o.get("child_spawns"):
                records.append(rec("response_item", {"type": "function_call", "namespace": "collaboration", "name": "spawn_agent", "arguments": "{}", "call_id": "child-call"}, t["child_start"] + 0.5))
            if o.get("child_error"):
                records.append(rec("event_msg", {"type": "error", "message": "synthetic"}, t["child_start"] + 0.6))
            if turn_no == int(o.get("child_turns", 1)) - 1:
                if o.get("child_abort"):
                    records.append(rec("event_msg", {"type": "turn_aborted", "turn_id": turn, "reason": "interrupted"}, t["child_done"]))
                elif o.get("child_complete", True):
                    records.append(rec("event_msg", {"type": "task_complete", "turn_id": turn, "last_agent_message": o.get("child_final", "synthetic result")}, t["child_done"]))
            else:
                records.append(rec("event_msg", {"type": "task_complete", "turn_id": turn, "last_agent_message": "first"}, t["child_start"] + turn_no + 0.5))
        for extra in o.get("extra_child", []) if child == C else []:
            records.insert(2, extra)
        out = rollout_path(self.root, child)
        write_lines(out, records)
        return out


def measure(call):
    """Run `call()` while counting the bytes ACTUALLY returned by os.read and the records ACTUALLY parsed (json.loads on raw bytes: every transcript line and every
    metadata first line; the spawn-argument parse takes a str and is not a record). Returns (value, bytes_read, records_parsed)."""
    reads, parses = [0], [0]
    real_read, real_loads = os.read, json.loads

    def counting_read(fd, size):
        data = real_read(fd, size)
        reads[0] += len(data)
        return data

    def counting_loads(text, *args, **kwargs):
        if isinstance(text, (bytes, bytearray)):
            parses[0] += 1
        return real_loads(text, *args, **kwargs)
    with mock.patch.object(os, "read", counting_read), mock.patch.object(json, "loads", counting_loads):
        value = call()
    return value, reads[0], parses[0]


class Base(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "codex"
        self.world = World(self.root)

    def verify(self, **o) -> cc.Verdict:
        self.world.build(**o)
        return cc.verify(P, self.root)

    def expect(self, kind: str, **o) -> cc.Verdict:
        verdict = self.verify(**o)
        self.assertEqual(verdict.kind, kind, (o, verdict))
        return verdict


class ModulePathTests(unittest.TestCase):
    def test_the_repository_source_modules_are_the_ones_loaded_never_installed_bytes(self) -> None:
        for module in (cc, rt, nr, herdr_protocol, hs):
            path = Path(module.__file__).resolve()
            self.assertEqual(path.parent, SRC.resolve(), module.__name__)
            self.assertNotIn(".local/lib/herdr-supervisor", str(path))


class VerifierPositiveTests(Base):
    def test_no_spawn_and_no_child_is_transparent(self) -> None:
        verdict = self.expect(cc.TRANSPARENT, spawn="none", child=False, mailbox="none")
        self.assertTrue(verdict.clear and not verdict.spawned)

    def test_one_compliant_completed_child_is_complete_with_ordered_evidence(self) -> None:
        verdict = self.expect(cc.COMPLETE)
        self.assertTrue(verdict.clear and verdict.spawned)
        self.assertEqual((verdict.evidence["child"], verdict.evidence["spawn_calls"], verdict.evidence["descendants"]), (C, 1, 1))

    def test_unrelated_sessions_other_days_and_other_parents_do_not_count(self) -> None:
        self.world.build(spawn="none", child=False, mailbox="none")
        other = World(self.root, parent="99999999-9999-4999-8999-999999999999")
        other.build()                                                       # another parent's compliant child under the same root
        old = self.root / "2026" / "11" / "03"
        write_lines(old / f"rollout-2026-11-03T10-00-00-{C2}.jsonl", [self.world.child_meta(C2)])    # an older day than the parent's: cannot be its child
        self.assertEqual(cc.verify(P, self.root).kind, cc.TRANSPARENT)


class VerifierPendingAndFailureTests(Base):
    def test_pending_child_holds(self) -> None:
        self.expect(cc.HOLD, child_complete=False, mailbox="none", parent_end="open")

    def test_aborted_child_is_a_violation_never_completion(self) -> None:
        self.expect(cc.VIOLATION, child_abort=True, mailbox="none")

    def test_errored_child_is_unknown(self) -> None:
        self.expect(cc.UNKNOWN, child_error=True)

    def test_missing_child_rollout_is_unknown_when_the_parent_turn_ended_and_a_hold_while_it_is_open(self) -> None:
        self.expect(cc.UNKNOWN, child=False, mailbox="none")
        self.expect(cc.HOLD, child=False, mailbox="none", parent_end="open")

    def test_child_completed_but_final_not_yet_delivered_holds_only_while_the_parent_turn_is_open(self) -> None:
        self.expect(cc.HOLD, mailbox="none", parent_end="open")
        self.expect(cc.VIOLATION, mailbox="none")                          # the parent turn ended before the child's FINAL_ANSWER arrived

    def test_spawn_output_missing_holds_then_unknown_and_a_refused_spawn_is_unknown(self) -> None:
        self.expect(cc.HOLD, spawn_output="none", child=False, mailbox="none", parent_end="open")
        self.expect(cc.UNKNOWN, spawn_output="none", child=False, mailbox="none")
        self.expect(cc.UNKNOWN, spawn_output="refused", child=False, mailbox="none")

    def test_early_parent_final_is_adverse(self) -> None:
        self.expect(cc.VIOLATION, parent_end="early")
        self.expect(cc.VIOLATION, mailbox="early")                         # FINAL_ANSWER before the child's task_complete
        self.expect(cc.VIOLATION, t={"end": 14.5})                         # the parent turn ended after completion but before the mailbox boundary
        self.expect(cc.VIOLATION, parent_end="aborted")

    def test_child_without_final_text_or_with_a_second_turn_is_a_violation(self) -> None:
        self.expect(cc.VIOLATION, child_final="   ")
        self.expect(cc.VIOLATION, child_turns=2)


class VerifierIdentityAndRoleTests(Base):
    def test_model_only_spawn_is_blocked_never_grandfathered_and_other_roles_violate(self) -> None:
        self.expect(cc.VIOLATION, spawn="model_only")
        self.expect(cc.VIOLATION, spawn="no_role")                         # no role selector at all, no other option: still blocked while the child claims the approved role
        self.expect(cc.VIOLATION, spawn="wrong_role", role="repository_review_lens")
        self.expect(cc.VIOLATION, spawn="bad_fork")
        self.expect(cc.VIOLATION, spawn="extra_arg")

    def test_missing_custom_role_evidence_is_unknown_and_wrong_role_or_model_or_effort_violate(self) -> None:
        self.expect(cc.UNKNOWN, role=None)
        self.expect(cc.VIOLATION, role="repository_review_lens")
        self.expect(cc.VIOLATION, settings="wrong_model")
        self.expect(cc.VIOLATION, settings="wrong_effort")
        self.expect(cc.UNKNOWN, settings="missing")
        self.expect(cc.UNKNOWN, settings="two")

    def test_unsupported_argument_shapes_are_unknown(self) -> None:
        self.expect(cc.UNKNOWN, spawn="bad_args")

    def test_child_identity_must_match_the_spawned_task_depth_and_parent(self) -> None:
        self.expect(cc.VIOLATION, path="/root/other")
        self.expect(cc.VIOLATION, depth=2)
        self.expect(cc.VIOLATION, depth=True)                              # a bool is not the integer 1

    def test_wrong_parent_child_is_not_counted_and_leaves_the_spawn_without_a_child(self) -> None:
        made = self.world.build()
        meta = self.world.child_meta(C, parent="99999999-9999-4999-8999-999999999999")
        write_lines(made["child"], [meta])
        self.assertEqual(cc.verify(P, self.root).kind, cc.UNKNOWN)

    def test_duplicate_parent_rollout_and_duplicate_child_identity_are_unknown(self) -> None:
        self.world.build()
        dup = self.root / "2026" / "11" / "05" / f"rollout-2026-11-05T10-00-00-{P}.jsonl"
        write_lines(dup, [self.world.parent_meta()])
        self.assertEqual(cc.verify(P, self.root).kind, cc.UNKNOWN)
        dup.unlink()
        twin = self.root / "2026" / "11" / "05" / f"rollout-2026-11-05T10-00-01-{C}.jsonl"
        write_lines(twin, [self.world.child_meta(C)])
        self.assertEqual(cc.verify(P, self.root).kind, cc.UNKNOWN)

    def test_parent_metadata_must_prove_the_owned_primary_session(self) -> None:
        made = self.world.build(spawn="none", child=False, mailbox="none")
        for label, meta in {"another session": self.world.parent_meta(id=C, session_id=C), "a child marker": self.world.parent_meta(parent_thread_id=C),
                            "an object source": self.world.parent_meta(source={"subagent": {}})}.items():
            write_lines(made["parent"], [meta])
            self.assertEqual(cc.verify(P, self.root).kind, cc.IDENTITY, label)

    def test_non_canonical_owned_id_is_an_identity_result_and_no_root_is_unknown(self) -> None:
        self.assertEqual(cc.verify("../etc", self.root).kind, cc.IDENTITY)
        self.assertEqual(cc.verify(P, None).kind, cc.UNKNOWN)
        self.assertEqual(cc.verify(P, self.root / "absent").kind, cc.UNKNOWN)


class VerifierPolicyTests(Base):
    def test_unexpected_descendants_are_violations(self) -> None:
        self.expect(cc.VIOLATION, grandchild=True)
        self.expect(cc.VIOLATION, second_child=True)
        self.expect(cc.VIOLATION, child_spawns=True)                        # the child itself spawning

    def test_a_child_rollout_without_a_recorded_spawn_is_a_violation_never_transparent(self) -> None:
        self.expect(cc.VIOLATION, spawn="none", child=True, mailbox="none")

    def test_a_second_lifetime_spawn_counts_even_when_failed_or_pending_and_prohibited_interactions_violate(self) -> None:
        self.expect(cc.VIOLATION, second_spawn=True)
        for name in ("send_input", "resume_agent", "close_agent", "followup_task", "interrupt_agent"):
            with self.subTest(name):
                self.expect(cc.VIOLATION, prohibited=name)
        self.expect(cc.COMPLETE)                                            # wait_agent/list_agents alone stay allowed (positive control)

    def test_duplicate_call_ids_and_missing_timestamps_are_unknown(self) -> None:
        self.expect(cc.UNKNOWN, spawn="dup_call")
        self.expect(cc.UNKNOWN, drop_ts=("function_call",))
        self.expect(cc.UNKNOWN, drop_ts=("task_started",))
        self.expect(cc.UNKNOWN, mailbox="ok", drop_ts=("agent_message",))

    def test_forged_mailbox_records_do_not_complete_a_child(self) -> None:
        self.expect(cc.VIOLATION, mailbox="other")
        self.expect(cc.VIOLATION, mailbox="dup")


class VerifierEvidenceIntegrityTests(Base):
    def test_malformed_partial_oversized_and_non_object_records_are_unknown_never_zero(self) -> None:
        made = self.world.build(spawn="none", child=False, mailbox="none")
        good = made["parent"].read_text()
        for label, body in {"malformed json": good + "{not json\n", "non-object record": good + "[1]\n", "unterminated tail": good + '{"type":"event_msg"'}.items():
            made["parent"].write_text(body)
            self.assertEqual(cc.verify(P, self.root).kind, cc.UNKNOWN, label)
        made["parent"].write_text(good)
        with mock.patch.object(nr, "MAX_LINE_BYTES", 40):
            self.assertEqual(cc.verify(P, self.root).kind, cc.UNKNOWN, "oversized record")
        self.assertEqual(cc.verify(P, self.root).kind, cc.TRANSPARENT, "positive control")

    def test_symlinks_are_never_followed_or_skipped(self) -> None:
        made = self.world.build()
        real = made["child"].with_name("real-child.jsonl")
        made["child"].rename(real)
        os.symlink(real, made["child"])
        self.assertEqual(cc.verify(P, self.root).kind, cc.UNKNOWN, "a symlinked rollout cannot be excluded as a child")
        made["child"].unlink()
        real.rename(made["child"])
        moved = self.root / "2026" / "11" / "04-real"
        (self.root / "2026" / "11" / "04").rename(moved)
        os.symlink(moved, self.root / "2026" / "11" / "04")
        self.assertEqual(cc.verify(P, self.root).kind, cc.UNKNOWN, "a symlinked date directory (the parent cannot even be located)")

    def test_a_symlinked_date_directory_hiding_a_child_is_unknown(self) -> None:
        self.world.build(spawn="none", child=False, mailbox="none")
        hidden = self.root / "elsewhere"
        write_lines(hidden / f"rollout-2026-11-05T10-00-00-{C}.jsonl", [self.world.child_meta(C)])
        os.symlink(hidden, self.root / "2026" / "11" / "05")
        self.assertEqual(cc.verify(P, self.root).kind, cc.UNKNOWN)

    def test_growth_replacement_and_in_place_rewrite_during_the_read_are_unknown(self) -> None:
        made = self.world.build()
        path = made["parent"]

        def during(mutate):
            real, inode, state = os.read, os.stat(path).st_ino, {"done": False}

            def wrapper(fd, size):
                data = real(fd, size)
                if data and not state["done"] and os.fstat(fd).st_ino == inode:
                    state["done"] = True
                    mutate()
                return data
            return mock.patch.object(os, "read", wrapper)
        def grow():
            with path.open("a") as handle:
                handle.write(json.dumps(rec("event_msg", {"type": "token_count"}, 30)) + "\n")
        with during(grow):
            self.assertEqual(cc.verify(P, self.root).kind, cc.UNKNOWN, "growth")
        text = path.read_text()
        def swap():
            tmp = path.with_name("swap.jsonl"); tmp.write_text(text); os.replace(tmp, path)
        with during(swap):
            self.assertEqual(cc.verify(P, self.root).kind, cc.UNKNOWN, "replacement")
        write_lines(path, [json.loads(l) for l in text.splitlines()])
        with during(lambda: os.utime(path, ns=(1, 1))):
            self.assertEqual(cc.verify(P, self.root).kind, cc.UNKNOWN, "in-place change (mtime)")
        write_lines(path, [json.loads(l) for l in text.splitlines()])
        self.assertEqual(cc.verify(P, self.root).kind, cc.COMPLETE, "positive control after a settled rewrite")

    def test_a_long_history_is_never_inferred_from_its_tail(self) -> None:
        made = self.world.build()
        text = made["parent"].read_text()
        with mock.patch.object(cc, "MAX_HISTORY_BYTES", len(text.encode()) - 1):
            verdict = cc.verify(P, self.root)
        self.assertEqual(verdict.kind, cc.UNKNOWN)
        self.assertIn("never read as the whole", verdict.detail)

    def test_the_budget_is_one_shared_total_across_parent_discovery_and_child(self) -> None:
        """Accounting equals REALITY: the limit that exactly covers the bytes actually returned by os.read and the records actually parsed passes, one less fails, so
        nothing is undercounted (limit == actual passes) and nothing is overcounted (limit == actual - 1 fails)."""
        self.world.build()
        verdict, actual_bytes, actual_records = measure(lambda: cc.verify(P, self.root))
        self.assertEqual(verdict.kind, cc.COMPLETE)
        with mock.patch.object(cc, "MAX_HISTORY_BYTES", actual_bytes):
            self.assertEqual(cc.verify(P, self.root).kind, cc.COMPLETE, "the shared byte total that equals the actual reads fits exactly")
        with mock.patch.object(cc, "MAX_HISTORY_BYTES", actual_bytes - 1):
            self.assertEqual(cc.verify(P, self.root).kind, cc.UNKNOWN, "one byte under the actual reads (discovery read-ahead and newline included) fails")
        with mock.patch.object(nr, "MAX_RECORDS", actual_records):
            self.assertEqual(cc.verify(P, self.root).kind, cc.COMPLETE, "the record total that equals the actual parses (metadata records included) fits exactly")
        with mock.patch.object(nr, "MAX_RECORDS", actual_records - 1):
            self.assertEqual(cc.verify(P, self.root).kind, cc.UNKNOWN, "one record under the actual parses fails")
    def test_entry_and_time_budgets_exhaust_to_unknown(self) -> None:
        self.world.build()
        with mock.patch.object(nr, "MAX_DIR_ENTRIES", 3):
            self.assertEqual(cc.verify(P, self.root).kind, cc.UNKNOWN)
        ticks = iter(range(0, 1000, 2))
        self.assertEqual(cc.verify(P, self.root, clock=lambda: float(next(ticks))).kind, cc.UNKNOWN, "elapsed-time bound on an injected clock")
        self.assertEqual(cc.verify(P, self.root).kind, cc.COMPLETE)

    def test_no_decision_is_cached_between_calls(self) -> None:
        made = self.world.build()
        self.assertEqual(cc.verify(P, self.root).kind, cc.COMPLETE)
        write_lines(made["parent"], [json.loads(l) for l in made["parent"].read_text().splitlines()] + [rec("response_item", {"type": "function_call", "namespace": "collaboration", "name": "spawn_agent",
                                                                                                                      "arguments": "{}", "call_id": "late"}, 30)])
        self.assertEqual(cc.verify(P, self.root).kind, cc.VIOLATION, "a later spawn is seen on the very next decision")

    def test_the_verifier_is_read_only(self) -> None:
        made = self.world.build()
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in made.values()}
        cc.verify(P, self.root)
        self.assertEqual(before, {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in made.values()})


class SnapshotConsistencyTests(Base):
    """CI1: metadata and body are one evidence set; drift between any two reads, or at the consumption boundary, is `unknown` (interleaved mutations, no cached authority)."""

    def during_discovery(self, mutate, **o):
        made = self.world.build(**o)
        real = cc._discover_descendants

        def wrapper(*args, **kwargs):
            value = real(*args, **kwargs)
            mutate(made)
            return value
        with mock.patch.object(cc, "_discover_descendants", wrapper):
            return cc.verify(P, self.root)

    @staticmethod
    def edit(path: Path, change) -> None:
        records = [json.loads(line) for line in path.read_text().splitlines()]
        change(records)
        write_lines(path, records)

    def test_positive_control_without_interleaving(self) -> None:
        self.assertEqual(self.verify().kind, cc.COMPLETE)

    def test_a_second_spawn_appended_to_the_parent_after_it_was_read_cannot_clear(self) -> None:
        late = rec("response_item", {"type": "function_call", "namespace": "collaboration", "name": "spawn_agent", "arguments": "{}", "call_id": "late"}, 30)
        verdict = self.during_discovery(lambda m: self.edit(m["parent"], lambda rs: rs.append(late)))
        self.assertEqual(verdict.kind, cc.UNKNOWN, verdict)
        self.assertIn("changed", verdict.detail)

    def test_child_metadata_changed_after_discovery_cannot_clear(self) -> None:
        def change(made):
            self.edit(made["child"], lambda rs: rs[0]["payload"]["source"]["subagent"]["thread_spawn"].__setitem__("agent_role", "repository_review_lens"))
        verdict = self.during_discovery(change)
        self.assertEqual(verdict.kind, cc.UNKNOWN, verdict)
        self.assertIn("changed between its discovery and its full read", verdict.detail)

    def test_a_new_rollout_appearing_during_the_decision_invalidates_the_relationship_set(self) -> None:
        def add(made):
            write_lines(rollout_path(self.root, C2), [self.world.child_meta(C2, path="/root/explorer2")])
        verdict = self.during_discovery(add)
        self.assertEqual(verdict.kind, cc.UNKNOWN, verdict)
        self.assertIn("relationship-set drift", verdict.detail)

    def test_a_parent_changed_between_its_read_and_the_discovery_is_caught_by_binding_the_read_extent(self) -> None:
        made = self.world.build()
        real = cc._discover_descendants
        late = rec("response_item", {"type": "function_call", "namespace": "collaboration", "name": "spawn_agent", "arguments": "{}", "call_id": "late"}, 30)

        def wrapper(*args, **kwargs):
            self.edit(made["parent"], lambda rs: rs.append(late))      # after the parent was read, BEFORE the namespace is listed: both listings agree on the new bytes
            return real(*args, **kwargs)
        with mock.patch.object(cc, "_discover_descendants", wrapper):
            verdict = cc.verify(P, self.root)
        self.assertEqual(verdict.kind, cc.UNKNOWN, verdict)
        self.assertIn("extent drift", verdict.detail)

    def test_a_child_body_rewritten_in_place_after_its_read_is_caught_at_the_consumption_boundary(self) -> None:
        made = self.world.build()
        real = cc._decide

        def wrapper(*args, **kwargs):
            value = real(*args, **kwargs)
            os.utime(made["child"], ns=(1, 1))
            return value
        with mock.patch.object(cc, "_decide", wrapper):
            verdict = cc.verify(P, self.root)
        self.assertEqual(verdict.kind, cc.UNKNOWN, verdict)

    def test_a_replaced_parent_after_the_decision_is_caught_at_the_consumption_boundary(self) -> None:
        made = self.world.build()
        text = made["parent"].read_text()
        real = cc._decide

        def wrapper(*args, **kwargs):
            value = real(*args, **kwargs)
            tmp = made["parent"].with_name("swap.jsonl"); tmp.write_text(text); os.replace(tmp, made["parent"])
            return value
        with mock.patch.object(cc, "_decide", wrapper):
            self.assertEqual(cc.verify(P, self.root).kind, cc.UNKNOWN)


class MalformedEvidenceTests(Base):
    """CI2: exact nested types; unsupported action histories are never zero; everything is a verdict, never an unhandled exception."""

    def test_non_object_payloads_and_unsupported_record_types_are_unknown(self) -> None:
        for label, record in {"unsupported kind with a benign-looking payload": {"type": "mystery", "payload": {"type": "message", "role": "user"}},
                              "list payload": {"type": "response_item", "payload": []}, "string payload": {"type": "event_msg", "payload": "x"}, "no payload type": {"type": "response_item", "payload": {}},
                              "numeric payload type": {"type": "event_msg", "payload": {"type": 7}}, "unsupported record type": {"type": "mystery", "payload": {}}, "non-string record type": {"type": 5},
                              "unsupported response item": {"type": "response_item", "payload": {"type": "mystery_item"}}, "unsupported event": {"type": "event_msg", "payload": {"type": "mystery_event"}}}.items():
            with self.subTest(label):
                self.expect(cc.UNKNOWN, spawn="none", child=False, mailbox="none", extra_parent=[record])

    def test_unsupported_embedded_or_custom_spawn_histories_cannot_prove_zero(self) -> None:
        custom = lambda **p: rec("response_item", {"type": "custom_tool_call", "call_id": "x1", **p}, 12)
        for label, record in {"custom spawn_agent": custom(name="spawn_agent", input="{}"), "custom collaboration namespace": custom(name="x", namespace="collaboration", input="{}"),
                              "exec embedding tools.spawn_agent": custom(name="exec", input="await tools.spawn_agent({task_name: 'x'})"), "exec embedding a bare call": custom(name="exec", input="spawn_agent({})"),
                              "non-string input": custom(name="exec", input=["x"]), "non-string name": custom(name=3, input="ls")}.items():
            with self.subTest(label):
                self.expect(cc.UNKNOWN, spawn="none", child=False, mailbox="none", extra_parent=[record])
        # supported benign custom tool use (a plain command that merely MENTIONS the word) stays transparent: the heuristic keys on call syntax
        self.expect(cc.TRANSPARENT, spawn="none", child=False, mailbox="none", extra_parent=[custom(name="exec", input="rg spawn_agent src/")])

    def test_malformed_nested_child_settings_are_unknown_not_an_exception(self) -> None:
        def settings(value):
            return rec("event_msg", {"type": "thread_settings_applied", "thread_settings": value}, 12.2)
        for label, value in {"dict model": {"model": {"not": "a string"}, "collaboration_mode": {"settings": {"reasoning_effort": "high"}}}, "list thread_settings": ["x"],
                             "effort not a string": {"model": "gpt-6-luna", "collaboration_mode": {"settings": {"reasoning_effort": 3}}}, "collaboration_mode a list": {"model": "gpt-6-luna", "collaboration_mode": []}}.items():
            with self.subTest(label):
                made = self.world.build(settings="missing")
                records = [json.loads(line) for line in made["child"].read_text().splitlines()]
                records.insert(1, settings(value))
                write_lines(made["child"], records)
                self.assertEqual(cc.verify(P, self.root).kind, cc.UNKNOWN, label)

    def test_malformed_spawn_function_and_attribution_shapes_are_unknown(self) -> None:
        self.expect(cc.UNKNOWN, attr="bad_type")
        self.expect(cc.UNKNOWN, spawn="none", child=False, mailbox="none", extra_parent=[rec("response_item", {"type": "function_call", "name": 5, "call_id": "z"}, 12)])
        self.expect(cc.UNKNOWN, spawn="none", child=False, mailbox="none", extra_parent=[rec("response_item", {"type": "message"}, 12)])
        self.expect(cc.UNKNOWN, extra_child=[rec("response_item", {"type": "message"}, 12.3)])
        self.expect(cc.UNKNOWN, extra_child=[rec("event_msg", {"type": "mystery_event"}, 12.3)])
        self.expect(cc.UNKNOWN, extra_child=[rec("response_item", {"type": "custom_tool_call", "name": "exec", "input": "tools.spawn_agent({})", "call_id": "c"}, 12.3)])

    def test_the_verifier_never_raises(self) -> None:
        made = self.world.build()
        garbage = [b"\xff\xfe\n", b"[]\n", b"{\"type\": null}\n", b"{\"type\": \"response_item\", \"payload\": {\"type\": \"function_call\", \"name\": [], \"call_id\": {}}}\n"]
        for blob in garbage:
            with self.subTest(blob=blob[:20]):
                made["parent"].write_bytes(made["parent"].read_bytes().split(b"\n", 1)[0] + b"\n" + blob)
                self.assertIn(cc.verify(P, self.root).kind, (cc.UNKNOWN, cc.IDENTITY))


class ChronologyAndAttributionTests(Base):
    """CI3: the parent's own assistant final is judged on its own event; recorded root/parent-turn attribution must match the invoking parent turn."""

    def test_an_early_parent_assistant_final_is_adverse_even_if_the_turn_ends_later(self) -> None:
        self.expect(cc.VIOLATION, final="parent final before child completion", t={"final": 13})
        self.expect(cc.VIOLATION, final="parent final between the child's completion and the mailbox", t={"final": 14.5})
        self.expect(cc.VIOLATION, final="parent final before the child even started", t={"final": 11.8})

    def test_a_parent_final_before_a_pending_child_terminal_is_adverse_and_without_one_it_holds(self) -> None:
        self.expect(cc.VIOLATION, final="early", child_complete=False, mailbox="none", parent_end="open", t={"final": 13})
        self.expect(cc.HOLD, child_complete=False, mailbox="none", parent_end="open")

    def test_compliant_chronology_with_a_final_after_the_boundary_stays_complete(self) -> None:
        verdict = self.expect(cc.COMPLETE, final="the parent's own final after the mailbox", t={"final": 16})
        self.assertTrue(verdict.spawned)

    def test_contradictory_recorded_attribution_cannot_clear_while_absent_attribution_is_allowed(self) -> None:
        for attr in ("wrong", "wrong_parent_turn", "wrong_root"):
            with self.subTest(attr):
                verdict = self.expect(cc.VIOLATION, attr=attr)
                self.assertIn("contradicts the invoking parent turn", verdict.detail)
        self.expect(cc.COMPLETE, attr="absent")
        self.expect(cc.COMPLETE, attr="ok")


class SharedAccountingTests(Base):
    """CI5: bytes, records, entries and time are cumulative across discovery, reads, parses and native extraction; each phase may fit alone while the sum does not."""

    def counts(self, **kw):
        ticks, calls = [0], [0]
        real_tick = nr._Budget.tick

        def tick(self_, count=1):
            ticks[0] += count
            return real_tick(self_, count)

        def clock():
            calls[0] += 1
            return calls[0] * 0.0001
        with mock.patch.object(nr._Budget, "tick", tick):
            verdict = cc.verify(P, self.root, clock=clock, **kw)
        return verdict, ticks[0], calls[0]

    def test_records_parsed_for_the_native_frame_count_against_the_same_total(self) -> None:
        self.world.build(prompt=f"Run {RUN} turn {TURN}", final="Plan\n" + hp_frame())
        plain, _b, plain_records = measure(lambda: cc.verify(P, self.root))
        routed, _b2, routed_records = measure(lambda: cc.verify(P, self.root, route=(RUN, TURN)))
        self.assertEqual((plain.kind, routed.kind, routed.native.status), (cc.COMPLETE, cc.COMPLETE, nr.FOUND))
        self.assertGreater(routed_records, plain_records, "the native extraction parses the parent again")
        with mock.patch.object(nr, "MAX_RECORDS", plain_records):
            self.assertEqual(cc.verify(P, self.root).kind, cc.COMPLETE, "verification alone fits exactly")
            self.assertEqual(cc.verify(P, self.root, route=(RUN, TURN)).kind, cc.UNKNOWN, "verification + native extraction exceed the SAME total")
        with mock.patch.object(nr, "MAX_RECORDS", routed_records):
            verdict = cc.verify(P, self.root, route=(RUN, TURN))
            self.assertEqual((verdict.kind, verdict.native.status), (cc.COMPLETE, nr.FOUND), "the exact cumulative total of the actual parses fits")
        with mock.patch.object(nr, "MAX_RECORDS", routed_records - 1):
            self.assertEqual(cc.verify(P, self.root, route=(RUN, TURN)).kind, cc.UNKNOWN)
    def test_directory_entries_of_both_namespace_listings_count_against_the_same_total(self) -> None:
        self.world.build(prompt=f"Run {RUN} turn {TURN}", final="Plan\n" + hp_frame())
        _verdict, entries, _calls = self.counts()
        self.assertEqual(entries, 15, "pinned accounting: parent lookup 5 + two namespace listings of 5 (year, month, day, parent file, child file)")
        with mock.patch.object(nr, "MAX_DIR_ENTRIES", entries):
            self.assertEqual(cc.verify(P, self.root).kind, cc.COMPLETE, "the exact cumulative entry total fits")
        with mock.patch.object(nr, "MAX_DIR_ENTRIES", entries - 1):
            self.assertEqual(cc.verify(P, self.root).kind, cc.UNKNOWN, "one entry over the cumulative total (each listing alone would fit)")
        with mock.patch.object(nr, "MAX_DIR_ENTRIES", entries // 2 + 1):
            self.assertEqual(cc.verify(P, self.root).kind, cc.UNKNOWN)

    def test_the_elapsed_time_bound_spans_verification_and_native_extraction(self) -> None:
        self.world.build(prompt=f"Run {RUN} turn {TURN}", final="Plan\n" + hp_frame())
        _v, _e, plain = self.counts()
        _v, _e, routed = self.counts(route=(RUN, TURN))
        self.assertGreater(routed, plain)
        step = 3.0 / (plain + (routed - plain) / 2)
        calls = [0]

        def clock():
            calls[0] += 1
            return calls[0] * step
        self.assertEqual(cc.verify(P, self.root, clock=clock).kind, cc.COMPLETE, "verification alone is inside 3 s")
        calls[0] = 0
        self.assertEqual(cc.verify(P, self.root, clock=clock, route=(RUN, TURN)).kind, cc.UNKNOWN, "the sum of the phases exceeds the single 3 s allowance")

    def test_bytes_are_one_total_across_the_parent_and_every_child_read(self) -> None:
        self.world.build()
        _v, actual, _r = measure(lambda: cc.verify(P, self.root))
        with mock.patch.object(cc, "MAX_HISTORY_BYTES", actual):
            self.assertEqual(cc.verify(P, self.root).kind, cc.COMPLETE)
        with mock.patch.object(cc, "MAX_HISTORY_BYTES", actual - 1):
            self.assertEqual(cc.verify(P, self.root).kind, cc.UNKNOWN)

    def test_discovery_charges_every_returned_byte_and_never_reads_beyond_the_allowance(self) -> None:
        """CI5-R1: unrelated benign rollouts are read for their metadata in bounded chunks; the discarded read-ahead and the newline are charged; under a limit no read
        returns more than the allowance that was left, even on the failing run."""
        made = self.world.build()
        for index in range(3):      # unrelated sessions dated later: benign metadata with a large (multi-chunk) first record
            other = f"0b{index}00000-0000-4000-8000-00000000000{index}"
            big = rec("session_meta", {"id": other, "source": "cli", "cwd": "/unrelated", "instructions": "x" * (20000 + index)}, 0)
            write_lines(self.root / "2026" / "11" / "05" / f"rollout-2026-11-05T10-00-0{index}-{other}.jsonl", [big, rec("event_msg", {"type": "token_count"}, 1)])
        verdict, actual, records = measure(lambda: cc.verify(P, self.root))
        self.assertEqual(verdict.kind, cc.COMPLETE)
        self.assertGreater(actual, made["parent"].stat().st_size + made["child"].stat().st_size + 3 * 20000, "the unrelated metadata reads are part of the actual total")
        with mock.patch.object(cc, "MAX_HISTORY_BYTES", actual):
            self.assertEqual(cc.verify(P, self.root).kind, cc.COMPLETE, "exact cumulative bytes including every benign rollout's metadata read")
        for limit in (actual - 1, actual - 9000, actual // 2):
            with mock.patch.object(cc, "MAX_HISTORY_BYTES", limit):
                value, read_bytes, _r = measure(lambda: cc.verify(P, self.root))
                self.assertEqual(value.kind, cc.UNKNOWN, limit)
                self.assertLessEqual(read_bytes, limit, "no read returned more than the allowance that was left (no uncharged read-ahead)")
        with mock.patch.object(nr, "MAX_RECORDS", records):
            self.assertEqual(cc.verify(P, self.root).kind, cc.COMPLETE, "each benign rollout's metadata parse is one charged record")
        with mock.patch.object(nr, "MAX_RECORDS", records - 1):
            self.assertEqual(cc.verify(P, self.root).kind, cc.UNKNOWN)

# ----------------------------------------------------------------- real consumer wiring (private runtime, fake Herdr, injected clocks)

PLAN_GATE = ("plan", "human", "plan_approval")


class Flow(V2Case):
    """A Codex-parent run through the gated workflow. The parent transcript is written at prompt time by a hook, exactly like the provider would."""

    def setUp(self) -> None:
        super().setUp()
        self.world = World(self.codex_native_root)

    def codex_prompts(self) -> list:
        return [p for p in self.herdr.prompts if p[0] == "codex-main"]

    def codex_reads(self) -> list:
        return [r for r in self.herdr.reads if r[0] == "codex-main"]

    def native_frame(self, run: str, turn: str) -> str:
        return FakeHerdr.block_v2(run, turn, "plan", "human", "plan_approval", str(self.plan_payload()), prefix="")

    def writer(self, *, native_frame=True, pane_frame: str | None = None, **opts):
        """Prompt-time hook: the provider records prompt, optional spawn/child, its final (with the routing frame) and the turn end in its native transcript."""
        def before(fake, name, text):
            run, turn = FakeHerdr.ids(text)
            final = ("Plan ready\n" + self.native_frame(run, turn)) if native_frame else None
            self.world.build(prompt=text, final=final, **opts)
        return before

    def run_codex(self, *, pane_output="no frame", status=None, pane_v2=None, **opts) -> int:
        self.write_plan()
        response = {"v2": pane_v2, "before": self.writer(**opts)} if pane_v2 else {"output": pane_output, "before": self.writer(**opts)}
        if status:
            response["status"] = status
        self.herdr.responses = [response]
        return self.sup.run_new("Add the widget", "codex", workflow_policy="gated_v2")

    def pending(self) -> dict:
        return self.state().get("pending_gate") or {}

    def log_events(self, name: str) -> list:
        path = self.paths.logs_dir / f"{self.state()['run_id']}.jsonl"
        return [e for e in (json.loads(line) for line in path.read_text().splitlines()) if e["event"] == name]


class ConsumerWiringTests(Flow):
    def test_no_child_run_is_transparent_and_routes_the_pane_frame_unchanged(self) -> None:
        self.write_plan()
        self.herdr.responses = [{"v2": PLAN_GATE + (str(self.plan_payload()),)}]
        self.assertEqual(self.sup.run_new("Add the widget", "codex", workflow_policy="gated_v2"), 4)
        self.assertEqual((self.pending().get("gate_type"), len(self.codex_prompts()), self.herdr.sent_keys), ("plan_approval", 1, []))

    def test_a_verified_child_routes_only_the_primary_native_frame_and_never_the_forged_pane_frame(self) -> None:
        self.write_plan()
        forged = ("plan", "human", "generic_question", str(self.question_payload()))      # a valid-looking pane frame for this run/turn that names a DIFFERENT gate
        self.herdr.responses = [{"v2": forged, "before": self.writer()}]
        with mock.patch.object(cc, "verify", wraps=cc.verify) as decisions:
            self.assertEqual(self.sup.run_new("Add the widget", "codex", workflow_policy="gated_v2"), 4)
        self.assertGreaterEqual(decisions.call_count, 4, "settle_guard, the result read, the settlement recheck and the route fence each took a FRESH decision (no cached clear)")
        self.assertEqual(self.pending().get("gate_type"), "plan_approval", "the native frame won; the pane frame (generic_question) was never parsed")
        self.assertEqual(len(self.state()["native_recoveries"]), 1)
        self.assertEqual((len(self.codex_prompts()), self.herdr.sent_keys), (1, []))
        self.assertEqual(len(self.log_events("codex_child_guard_cleared")), 1)

    def test_pane_only_frame_after_a_spawn_is_never_routed_when_the_native_frame_is_unavailable(self) -> None:
        self.write_plan()
        pane = ("plan", "human", "plan_approval", str(self.plan_payload()))
        self.herdr.responses = [{"v2": pane, "before": self.writer(native_frame=False)}]
        self.sup.run_new("Add the widget", "codex", workflow_policy="gated_v2")
        state = self.state()
        self.assertEqual((state["supervisor_state"], self.pending().get("status")), ("WAIT_USER", None))
        self.assertNotIn("native_recoveries", state)
        self.assertEqual((len(self.codex_prompts()), self.herdr.sent_keys), (1, []))

    def test_pending_child_with_an_early_idle_pane_frame_is_never_read_routed_or_sent_and_becomes_a_stable_wait(self) -> None:
        self.write_plan()
        pane = ("plan", "human", "plan_approval", str(self.plan_payload()))
        self.herdr.responses = [{"v2": pane, "before": self.writer(native_frame=False, child_complete=False, mailbox="none", parent_end="open")}]
        self.sup.run_new("Add the widget", "codex", workflow_policy="gated_v2")
        state = self.state()
        self.assertEqual(state["supervisor_state"], "WAIT_USER")
        self.assertIn("child-settlement guard", state["wait_user_reason"])
        self.assertIn("resume", state["wait_user_reason"])
        self.assertEqual((self.pending().get("status"), self.codex_reads(), self.herdr.sent_keys, len(self.codex_prompts())), (None, [], [], 1), "no read, route, key or second prompt before settlement")
        self.assertGreaterEqual(self.clock.current - 1_000_000.0, rt.CODEX_CHILD_HOLD_PATIENCE_SECONDS, "the bounded patience elapsed on the injected clock, not wall-clock sleeps")
        for _ in range(2):                                                     # the advertised recovery command: resume re-reads and repeats the same stable stop
            self.sup.resume()
            self.assertEqual((self.state()["supervisor_state"], len(self.codex_prompts()), self.herdr.sent_keys, self.pending().get("status")), ("WAIT_USER", 1, [], None))

    def test_the_child_completing_later_lets_the_next_decision_route_with_no_cached_authority(self) -> None:
        run_holder: dict = {}
        def pane(fake, name, text):
            run_holder["ids"] = FakeHerdr.ids(text)
            self.writer(native_frame=False, child_complete=False, mailbox="none", parent_end="open")(fake, name, text)
        self.write_plan()
        self.herdr.responses = [{"output": "no frame", "before": pane}]
        self.sup.run_new("Add the widget", "codex", workflow_policy="gated_v2")
        self.assertEqual(self.state()["supervisor_state"], "WAIT_USER")
        run, turn = run_holder["ids"]
        prompt_text = self.codex_prompts()[0][1]
        self.world.build(prompt=prompt_text, final="Plan ready\n" + self.native_frame(run, turn))      # the provider finished the child and the turn meanwhile
        self.assertEqual(self.sup.resume(), 4)
        self.assertEqual((self.pending().get("gate_type"), len(self.codex_prompts()), self.herdr.sent_keys), ("plan_approval", 1, []))

    def test_violation_and_unknown_histories_block_input_before_the_first_prompt(self) -> None:
        for label, build in {"second spawn": dict(second_spawn=True), "model-only spawn": dict(spawn="model_only"), "descendant": dict(grandchild=True)}.items():
            with self.subTest(label):
                self.reset_fixture(); self.world = World(self.codex_native_root)
                self.world.build(**build)                                       # the parent's history already violates the lifetime policy
                self.write_plan()
                self.herdr.responses = [{"v2": PLAN_GATE + (str(self.plan_payload()),)}]
                self.sup.run_new("Add the widget", "codex", workflow_policy="gated_v2")
                state = self.state()
                self.assertEqual((self.herdr.prompts, self.herdr.sent_keys, self.codex_reads()), ([], [], []), "no input, key or read")
                self.assertEqual((state["supervisor_state"], state["wait_user_requires_action"]), ("WAIT_USER", True))
                self.sup.resume()
                self.assertEqual((self.state()["supervisor_state"], self.herdr.prompts), ("WAIT_USER", []), "resume cannot clear a policy violation")
        self.reset_fixture()
        self.herdr.responses = [{"v2": PLAN_GATE + ("-",)}]

    def test_an_unreadable_or_missing_history_is_unknown_never_zero(self) -> None:
        self.codex_native_root_file = next(self.codex_native_root.rglob("rollout-*.jsonl"))
        self.codex_native_root_file.write_text("{broken\n")
        self.write_plan()
        self.herdr.responses = [{"v2": PLAN_GATE + (str(self.plan_payload()),)}]
        self.sup.run_new("Add the widget", "codex", workflow_policy="gated_v2")
        state = self.state()
        self.assertEqual((self.herdr.prompts, state["supervisor_state"], state["wait_user_requires_action"]), ([], "WAIT_USER", False))
        self.assertIn("unknown", state["wait_user_reason"])

    def test_the_route_fence_stops_any_pane_first_route_after_a_late_violation(self) -> None:
        self.write_plan()
        self.herdr.responses = [{"v2": PLAN_GATE + (str(self.plan_payload()),)}]
        self.assertEqual(self.sup.run_new("Add the widget", "codex", workflow_policy="gated_v2"), 4)
        with self.sup.store.transaction():
            state = self.sup.store.read_state()
            self.world.build(second_spawn=True)
            run, turn = state["run_id"], state["delivery"]["turn_id"]
            block = herdr_protocol.parse_protocol(FakeHerdr.block_v2(run, turn, "plan", "human", "generic_question", str(self.question_payload())), run, turn)
            before_gates = len(state.get("gates") or [])
            gate_before = json.dumps(state.get("pending_gate"), sort_keys=True)
            self.assertEqual(self.sup.route(state, block), "stop")
            self.assertEqual(state["supervisor_state"], "WAIT_USER")
            self.assertIn("child-settlement guard", state["wait_user_reason"], "the FENCE stopped it (not a downstream gate decision)")
            self.assertEqual(json.dumps(state.get("pending_gate"), sort_keys=True), gate_before, "no gate was created or changed")
            self.assertEqual(len(state.get("gates") or []), before_gates)

    def test_the_settlement_recheck_takes_a_fresh_child_decision_directly(self) -> None:
        self.write_plan()
        self.herdr.responses = [{"v2": PLAN_GATE + (str(self.plan_payload()),)}]
        self.assertEqual(self.sup.run_new("Add the widget", "codex", workflow_policy="gated_v2"), 4)
        state = self.state()
        for label, build, kind in (("violation", dict(second_spawn=True), "violation"), ("pending", dict(child_complete=False, mailbox="none", parent_end="open"), "hold"),
                                   ("unknown", dict(child_error=True), "unknown"), ("verified", {}, "clear")):
            with self.subTest(label):
                self.world.build(**build)
                self.assertEqual(self.sup._settlement_recheck(state, "codex").kind, kind)

    def test_diagnostics_use_the_same_predicate_as_the_guard(self) -> None:
        for label, build in {"transparent": dict(spawn="none", child=False, mailbox="none"), "complete": {}, "pending": dict(child_complete=False, mailbox="none", parent_end="open"),
                             "violation": dict(second_spawn=True)}.items():
            with self.subTest(label):
                self.world.build(**build)
                guard, verdict = self.sup._codex_child_check("codex")
                status = self.sup.codex_child_status()
                self.assertEqual((status["guard"], status["verdict"]), (guard.kind, verdict.kind))
                self.assertEqual(self.sup.settle_guard(self.state_or_empty(), "codex", "unit", wait=False, control=False).kind, guard.kind if guard.kind != "hold" else "hold")

    def state_or_empty(self) -> dict:
        return {"run_id": "r", "delivery": {"turn_id": "t"}}

    def test_claude_behaviour_is_unchanged_and_never_consults_the_codex_verifier(self) -> None:
        with mock.patch.object(cc, "verify", side_effect=AssertionError("the Codex verifier must not run for Claude")) as verifier:
            guard = self.sup.settle_guard(self.state_or_empty(), "claude", "unit", control=False)
            self.assertIn(guard.kind, ("clear", "hold", "unknown", "blocked", "identity", "patience"))
            self.assertEqual(self.sup._background_indicator_check("claude").kind in ("clear", "hold", "unknown", "identity"), True)
        self.assertEqual(verifier.call_count, 0)

    def test_the_test_only_seam_is_not_reachable_from_configuration_or_state(self) -> None:
        self.assertNotIn("codex_child_guard_enabled", json.dumps(hs.DEFAULT_CONFIG))
        self.assertTrue(self.sup.codex_child_guard_enabled)
        hostile = hs.resolve_config_defaults(hs.deep_merge(self.config, {"codex_child_guard_enabled": False}))
        sup = hs.Supervisor(self.paths, hostile, self.herdr, clock=self.clock.time, sleeper=self.clock.sleep)
        self.assertTrue(sup.codex_child_guard_enabled, "a config key cannot disable the guard")


class RouteProvenanceTests(Flow):
    """CI4: after a spawn, EVERY final routing entry accepts only the owned primary's eligible native final (fresh ownership and source stability); CI5 at the real collection path."""

    def to_native_gate(self) -> dict:
        self.assertEqual(self.run_codex(), 4)
        state = self.state()
        self.assertEqual(self.pending().get("gate_type"), "plan_approval")
        return state

    def forged_block(self, state: dict, *, gate="generic_question", turn: str | None = None):
        run, native_turn = state["run_id"], turn or state["delivery"]["turn_id"]
        payload = str(self.question_payload()) if gate == "generic_question" else str(self.plan_payload())
        return herdr_protocol.parse_protocol(FakeHerdr.block_v2(run, native_turn, "plan", "human", gate, payload), run, native_turn)

    def snapshot(self, state: dict) -> str:
        return json.dumps({k: state.get(k) for k in ("pending_gate", "delivery", "last_successful_handoff", "active_agent", "phase")}, sort_keys=True, default=str)

    def test_a_caller_supplied_frame_for_the_current_run_and_turn_is_refused_at_every_route_entry(self) -> None:
        state0 = self.to_native_gate()
        for entry in ("route", "route_v2"):
            with self.subTest(entry):
                with self.sup.store.transaction():
                    state = self.sup.store.read_state()
                    before = self.snapshot(state)
                    outcome = getattr(self.sup, entry)(state, self.forged_block(state0))
                    self.assertEqual(outcome, "stop")
                    self.assertEqual(self.snapshot(state), before, "no gate, handoff or delivery changed")
                    self.assertIn("not the owned primary's eligible native final", state["wait_user_reason"])
                    self.assertEqual(self.pending().get("gate_type") if state is not None else None, "plan_approval")
        self.assertEqual((len(self.codex_prompts()), self.herdr.sent_keys), (1, []))

    def test_a_frame_for_another_turn_or_one_that_differs_in_any_field_is_refused(self) -> None:
        state0 = self.to_native_gate()
        run, turn = state0["run_id"], state0["delivery"]["turn_id"]
        other_turn = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
        variants = {"another turn": herdr_protocol.parse_protocol(FakeHerdr.block_v2(run, other_turn, "plan", "human", "plan_approval", str(self.plan_payload())), run, other_turn),
                    "same gate, different handoff text": herdr_protocol.parse_protocol(FakeHerdr.block_v2(run, turn, "plan", "human", "plan_approval", str(self.plan_payload()), handoff="different words"), run, turn),
                    "same gate, different stage": herdr_protocol.parse_protocol(FakeHerdr.block_v2(run, turn, "brief", "human", "plan_approval", str(self.plan_payload())), run, turn)}
        for label, block in variants.items():
            with self.subTest(label), self.sup.store.transaction():
                state = self.sup.store.read_state()
                before = self.snapshot(state)
                self.assertEqual(self.sup.route(state, block), "stop")
                self.assertEqual(self.snapshot(state), before)

    def test_a_previously_valid_frame_is_refused_once_the_native_final_changes_or_conflicts(self) -> None:
        state0 = self.to_native_gate()
        run, turn = state0["run_id"], state0["delivery"]["turn_id"]
        genuine = herdr_protocol.parse_protocol("Plan ready\n" + self.native_frame(run, turn), run, turn)
        records = [json.loads(line) for line in next(self.codex_native_root.rglob(f"rollout-*-{P}.jsonl")).read_text().splitlines()]
        records.insert(len(records) - 1, rec("response_item", {"type": "message", "id": "m2", "role": "assistant", "phase": "final_answer", "content": [{"type": "output_text", "text": FakeHerdr.block_v2(run, turn, "plan", "human", "generic_question", str(self.question_payload()), prefix="")}],
                                                              "internal_chat_message_metadata_passthrough": {"turn_id": "t-1"}}, 16.5))
        write_lines(next(self.codex_native_root.rglob(f"rollout-*-{P}.jsonl")), records)
        with self.sup.store.transaction():
            state = self.sup.store.read_state()
            before = self.snapshot(state)
            self.assertEqual(self.sup.route(state, genuine), "stop", "competing native finals are a conflict: the earlier genuine frame no longer authorizes")
            self.assertEqual(self.snapshot(state), before)

    def test_the_fence_needs_fresh_exact_ownership(self) -> None:
        state0 = self.to_native_gate()
        genuine = herdr_protocol.parse_protocol("Plan ready\n" + self.native_frame(state0["run_id"], state0["delivery"]["turn_id"]), state0["run_id"], state0["delivery"]["turn_id"])
        self.herdr.agents["codex-main"]["agent_session"] = {"value": "99999999-9999-4999-8999-999999999999"}
        with self.sup.store.transaction():
            state = self.sup.store.read_state()
            before = self.snapshot(state)
            self.assertEqual(self.sup.route(state, genuine), "stop")
            self.assertEqual(self.snapshot(state), before)

    def test_the_real_result_collection_takes_one_decision_one_allowance_and_never_a_second_lookup(self) -> None:
        self.to_native_gate()
        constructed = []
        real_init = nr._Budget.__init__

        def counting(self_, clock):
            constructed.append(1)
            return real_init(self_, clock)
        with self.sup.store.transaction():
            state = self.sup.store.read_state()
            with mock.patch.object(nr._Budget, "__init__", counting), mock.patch.object(nr, "resolve", side_effect=AssertionError("a second, independent native lookup must not run")):
                block, _output, native = self.sup.collect_routing_result(state, "codex", state["delivery"], purpose="unit")
        self.assertIsNone(block, "after a verified spawn the pane frame is not parsed")
        self.assertTrue(native is not None and native.recoverable)
        self.assertEqual(len(constructed), 1, "ONE allowance for verification, child reads and native extraction")

    def test_cumulative_limits_stop_result_collection_with_nothing_routed(self) -> None:
        self.to_native_gate()
        with self.sup.store.transaction():
            state = self.sup.store.read_state()
            (block, _o, native), _bytes, records = measure(lambda: self.sup.collect_routing_result(state, "codex", state["delivery"], purpose="unit"))
            self.assertTrue(native.recoverable and block is None)
            with mock.patch.object(nr, "MAX_RECORDS", records - 1):          # each phase alone fits; verification + metadata + native extraction do not
                with self.assertRaises(hs.SupervisorError) as caught:
                    self.sup.collect_routing_result(state, "codex", state["delivery"], purpose="unit")
            self.assertIn("child-settlement guard did not clear", str(caught.exception))
            with mock.patch.object(nr, "MAX_RECORDS", records):
                _b, _o, native = self.sup.collect_routing_result(state, "codex", state["delivery"], purpose="unit")
            self.assertTrue(native.recoverable, "positive control: the exact cumulative total of the actual parses fits")

class RouteEntryScopeTests(Flow):
    """CI4-R1/R2: the nested route -> route_v2 exemption lives only inside the active invocation; the frame is bound to the CURRENT run and accepted Codex delivery."""

    to_native_gate = RouteProvenanceTests.to_native_gate
    snapshot = RouteProvenanceTests.snapshot

    def genuine(self, state0: dict):
        return herdr_protocol.parse_protocol("Plan ready\n" + self.native_frame(state0["run_id"], state0["delivery"]["turn_id"]), state0["run_id"], state0["delivery"]["turn_id"])

    def add_second_spawn(self) -> None:
        path = next(self.codex_native_root.rglob(f"rollout-*-{P}.jsonl"))
        records = [json.loads(line) for line in path.read_text().splitlines()]
        records.append(rec("response_item", {"type": "function_call", "namespace": "collaboration", "name": "spawn_agent", "arguments": "{}", "call_id": "second"}, 30))
        write_lines(path, records)

    def test_a_direct_route_v2_call_leaves_no_reusable_marker_and_the_same_object_is_rechecked_every_time(self) -> None:
        state0 = self.to_native_gate()
        genuine = self.genuine(state0)
        with self.sup.store.transaction():
            state = self.sup.store.read_state()
            with mock.patch.object(cc, "verify", wraps=cc.verify) as first:
                self.sup.route_v2(state, genuine)                       # accepted (the genuine native final)
            self.assertGreaterEqual(first.call_count, 1)
            self.assertIsNone(self.sup._route_nested, "no marker survives a direct accepted call")
            self.add_second_spawn()
            gate_before = json.dumps(state.get("pending_gate"), sort_keys=True, default=str)
            with mock.patch.object(cc, "verify", wraps=cc.verify) as second:
                outcome = self.sup.route_v2(state, genuine)             # the SAME object, after the evidence changed
            self.assertGreaterEqual(second.call_count, 1, "an independent entry always takes a fresh decision")
            self.assertEqual(outcome, "stop")
            self.assertEqual(json.dumps(state.get("pending_gate"), sort_keys=True, default=str), gate_before, "no gate was recreated")
            self.assertIsNone(self.sup._route_nested)

    def test_every_exit_of_route_clears_the_nested_scope_including_an_exception(self) -> None:
        state0 = self.to_native_gate()
        genuine = self.genuine(state0)
        with self.sup.store.transaction():
            state = self.sup.store.read_state()
            with mock.patch.object(self.sup, "_route_fenced", side_effect=RuntimeError("boom")):
                with self.assertRaises(RuntimeError):
                    self.sup.route(state, genuine)
            self.assertIsNone(self.sup._route_nested, "the exception exit cleared the scope")
            self.add_second_spawn()
            with mock.patch.object(cc, "verify", wraps=cc.verify) as check:
                self.assertEqual(self.sup.route_v2(state, genuine), "stop")
            self.assertGreaterEqual(check.call_count, 1)

    def test_a_refused_entry_leaves_no_marker_and_the_nested_hop_inside_route_is_exempt_only_while_active(self) -> None:
        state0 = self.to_native_gate()
        genuine = self.genuine(state0)
        seen = []
        real = self.sup._route_fenced

        def spy(state, block):
            seen.append(self.sup._route_nested is block)
            return real(state, block)
        with self.sup.store.transaction():
            state = self.sup.store.read_state()
            with mock.patch.object(self.sup, "_route_fenced", spy):
                self.sup.route(state, genuine)
            self.assertEqual(seen, [True], "inside the active route() the nested hop is exempt")
            self.assertIsNone(self.sup._route_nested, "and the scope ends with the invocation")
            self.add_second_spawn()
            for entry in ("route", "route_v2"):
                self.assertEqual(getattr(self.sup, entry)(state, genuine), "stop", entry)
                self.assertIsNone(self.sup._route_nested)

    def test_a_genuinely_recorded_old_frame_is_refused_against_a_newer_delivery_at_both_entries(self) -> None:
        state0 = self.to_native_gate()
        old_turn = state0["delivery"]["turn_id"]
        genuine = self.genuine(state0)
        for entry in ("route", "route_v2"):
            with self.subTest(entry), self.sup.store.transaction():
                state = self.sup.store.read_state()
                state["delivery"] = {**state["delivery"], "turn_id": "cccccccc-cccc-4ccc-8ccc-cccccccccccc"}
                before = self.snapshot(state)
                self.assertEqual(getattr(self.sup, entry)(state, genuine), "stop")
                self.assertEqual(self.snapshot(state).replace(old_turn, "T"), before.replace(old_turn, "T"), "gate, handoff and delivery unchanged")
                self.assertEqual(state["pending_gate"]["turn_id"], old_turn, "the plan gate for the OLD turn was not recreated")
                self.assertIn("not bound to the current run and accepted Codex delivery", state["wait_user_reason"])

    def test_a_run_mismatch_and_a_never_accepted_delivery_are_refused_and_the_current_delivery_is_the_positive(self) -> None:
        state0 = self.to_native_gate()
        genuine = self.genuine(state0)
        other_run = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
        with self.sup.store.transaction():
            state = self.sup.store.read_state()
            for label, mutate in {"another current run": lambda s: s.__setitem__("run_id", other_run),
                                  "delivery not confirmed accepted": lambda s: s["delivery"].update(status="uncertain", accepted_at=None),
                                  "delivery for another agent": lambda s: s["delivery"].update(agent="claude")}.items():
                copy = json.loads(json.dumps(state))
                mutate(copy)
                before = self.snapshot(copy)
                for entry in ("route", "route_v2"):
                    self.assertEqual(getattr(self.sup, entry)(copy, genuine), "stop", (label, entry))
                    self.assertEqual(self.snapshot(copy), before, (label, entry))
            with mock.patch.object(cc, "verify", wraps=cc.verify) as check:
                outcome = self.sup.route_v2(json.loads(json.dumps(state)), genuine)
            self.assertGreaterEqual(check.call_count, 1)
            self.assertNotIn("not bound to the current run", json.dumps(self.state().get("wait_user_reason")), "positive control: the current accepted delivery passes the binding")
            self.assertIsInstance(outcome, str)


class MalformedHistoryConsumerTests(Flow):
    """CI2 at the consumers: malformed or unsupported histories become the existing stable operator stop, never an exception, a prompt, a read or a route."""

    def run_with_history(self, history_records) -> None:
        path = next(self.codex_native_root.rglob(f"rollout-*-{P}.jsonl"))
        records = [json.loads(line) for line in path.read_text().splitlines()] + history_records
        write_lines(path, records)
        self.write_plan()
        self.herdr.responses = [{"v2": PLAN_GATE + (str(self.plan_payload()),)}]
        self.sup.run_new("Add the widget", "codex", workflow_policy="gated_v2")

    def test_unsupported_and_malformed_histories_stop_before_any_input(self) -> None:
        cases = {"list payload": [{"type": "response_item", "payload": []}],
                 "custom spawn": [rec("response_item", {"type": "custom_tool_call", "name": "spawn_agent", "input": "{}", "call_id": "x"}, 5)],
                 "embedded spawn in exec": [rec("response_item", {"type": "custom_tool_call", "name": "exec", "input": "await tools.spawn_agent({})", "call_id": "x"}, 5)]}
        for label, history in cases.items():
            with self.subTest(label):
                self.reset_fixture(); self.world = World(self.codex_native_root)
                self.run_with_history(history)
                state = self.state()
                self.assertEqual((self.herdr.prompts, self.herdr.sent_keys, self.codex_reads(), state["supervisor_state"]), ([], [], [], "WAIT_USER"))
                self.assertIn("child-settlement guard", state["wait_user_reason"])


class FollowupRetryAndQuotaTests(Flow):
    def to_codex_decision(self, **opts) -> dict:
        self.assertEqual(self.run_codex(**opts), 4)
        state = self.state()
        self.assertEqual(state["active_agent"], "codex")
        return state

    def ask(self, question: str = "Why?") -> None:
        st = self.state()
        decision = self.sup.followup_decision(st)
        command = {"request_id": "ask-0001", "action": "ask_agent", "run_id": st["run_id"], "question": question, "actor": "telegram:1", "chat_id": 9,
                   "decision_id": decision["decision_id"], "expected_state": st["supervisor_state"], "artifact_sha256": decision["artifact_sha256"], "payload_sha256": decision["payload_sha256"]}
        self.sup.enqueue_command(command)
        self.sup.process_inbox()

    def test_followup_delivery_is_refused_after_a_late_violation_and_sent_after_a_clean_history(self) -> None:
        self.to_codex_decision(spawn="none", child=False, mailbox="none", native_frame=False, pane_v2=PLAN_GATE + (str(self.plan_payload()),))
        self.ask()
        self.herdr.responses = [{"followup": {"summary": "answered"}}]
        self.sup.worker()
        self.assertEqual(len(self.codex_prompts()), 2, "positive control: a clean (childless) history delivers the follow-up")
        self.reset_fixture(); self.world = World(self.codex_native_root)
        self.to_codex_decision(spawn="none", child=False, mailbox="none", native_frame=False, pane_v2=PLAN_GATE + (str(self.plan_payload()),))
        self.world.build(second_spawn=True)
        self.ask()
        self.herdr.responses = []
        self.sup.worker()
        self.assertEqual(len(self.codex_prompts()), 1, "the follow-up prompt was never delivered while the history violates the policy")
        self.assertEqual(self.state()["agent_followup"]["status"], "FAILED")
        self.assertEqual(self.herdr.sent_keys, [])

    def test_followup_read_waits_for_a_pending_child_and_reads_nothing(self) -> None:
        self.to_codex_decision(spawn="none", child=False, mailbox="none", native_frame=False, pane_v2=PLAN_GATE + (str(self.plan_payload()),))
        reads_before = len(self.codex_reads())
        verdict = self.sup.settle_guard(self.state(), "codex", "followup_read", control=False)
        self.assertEqual(verdict.kind, "clear")
        self.world.build(child_complete=False, mailbox="none", parent_end="open")
        held = self.sup.settle_guard(self.state(), "codex", "followup_read", control=False)
        self.assertEqual(held.kind, "hold")
        self.assertEqual(len(self.codex_reads()), reads_before, "a hold reads nothing")
        reread = self.sup.settle_guard(self.state(), "codex", "followup_reread", wait=True, patience=30.0, control=False)
        self.assertEqual(reread.kind, "patience", "bounded patience on the injected clock")

    def test_retry_of_a_missing_routing_result_is_refused_without_consuming_the_one_time_retry(self) -> None:
        self.write_plan()
        self.herdr.responses = [{"output": "prose only, no block\n", "status": "idle", "before": self.writer(native_frame=False, spawn="none", child=False, mailbox="none")}]
        self.sup.run_new("Add the widget", "codex", workflow_policy="gated_v2")
        state = self.state()
        self.assertIsNotNone(state["missing_result"])
        self.world.build(second_spawn=True)
        reads_before = len(self.codex_reads())
        with self.assertRaises(hs.SupervisorError) as caught:
            self.sup.retry_routing_result(self.sup.store.read_state(), run_id=state["run_id"], turn_id=state["delivery"]["turn_id"], actor="cli")
        self.assertIn("codex child guard violation", str(caught.exception))
        self.assertEqual((len(self.codex_reads()), self.state()["missing_result"]["attempts"], len(self.codex_prompts())), (reads_before, 0, 1))

    def quota_run(self):
        self.config = hs.resolve_config_defaults(hs.deep_merge(self.config, {"quota_block_dismiss_keys": ["Enter"], "quota_screen_patterns": {"codex": ["usage limit"], "claude": ["usage limit"]}}))
        self.sup = self.make_supervisor()
        self.write_plan()
        self.herdr.responses = [{"status": "working", "output": "working", "before": self.writer(native_frame=False, spawn="none", child=False, mailbox="none", parent_end="open")}]

        def die(fake, name):
            if name == "codex-main":
                raise KeyboardInterrupt
        self.herdr.on_wait = die
        self.assertEqual(self.sup.run_new("Add the widget", "codex", workflow_policy="gated_v2"), 3)
        herdr2 = FakeHerdr()
        record = herdr2.agents.pop("codex-main")
        herdr2.agents["w3:p2"] = {**record, "name": None, "agent_status": "blocked"}
        herdr2.outputs["w3:p2"] = "You've hit your usage limit\n"
        herdr2.visible["w3:p2"] = "You've hit your usage limit"
        sup2 = hs.Supervisor(self.paths, self.config, herdr2, clock=self.clock.time, sleeper=self.clock.sleep)
        sup2.head_resolver = self.head_resolver
        sup2.native_roots = {"codex": self.codex_native_root}
        return herdr2, sup2

    def test_quota_dismissal_keys_need_a_clear_child_decision(self) -> None:
        herdr2, sup2 = self.quota_run()                                         # positive control first: a childless history lets the key through
        persisted = self.state()

        def after_keys(fake, name):
            fake.agents[name]["agent_status"] = "idle"
            fake.outputs[name] = FakeHerdr.block_v2(persisted["run_id"], persisted["delivery"]["turn_id"], "plan", "human", "plan_approval", str(self.plan_payload()))
        herdr2.on_wait = after_keys
        with sup2.store.transaction():
            sup2.resume_interrupted_turn(sup2.store.read_state(), "codex")
        self.assertEqual(herdr2.sent_keys, [("w3:p2", ["Enter"])], "positive control: the keys are sent with no spawn in the history")
        self.reset_fixture(); self.world = World(self.codex_native_root)
        herdr3, sup3 = self.quota_run()
        self.world.build(child_complete=False, mailbox="none", parent_end="open")      # a pending child appears before the dismissal decision
        with sup3.store.transaction():
            outcome = sup3.resume_interrupted_turn(sup3.store.read_state(), "codex")
        self.assertEqual((outcome, herdr3.sent_keys, herdr3.prompts), ("stop", [], []))
        self.assertIn("press the configured dismissal keys", self.state()["wait_user_reason"])

    def test_the_ready_branch_after_the_quota_wait_never_parses_a_pane_frame_after_a_spawn(self) -> None:
        herdr2, sup2 = self.quota_run()
        herdr2.agents["w3:p2"]["agent_status"] = "idle"
        state = self.state()
        herdr2.outputs["w3:p2"] = FakeHerdr.block_v2(state["run_id"], state["delivery"]["turn_id"], "plan", "human", "plan_approval", str(self.plan_payload()))
        self.world.build(prompt=self.codex_prompts()[0][1])                    # a verified compliant child exists: only the native frame may route
        herdr2.responses = [{"output": "continuing"}]
        with sup2.store.transaction():
            sup2.resume_interrupted_turn(sup2.store.read_state(), "codex")
        self.assertEqual((self.state().get("pending_gate") or {}).get("status"), None, "the pane-only frame was not routed")
        self.assertEqual(len(herdr2.prompts), 1, "the unverified pane frame was ignored; the existing no-frame path sends exactly one continuation, guarded like every input")


class StableOperatorWaitTests(Flow):
    def test_every_guard_stop_kind_yields_a_wait_user_state_not_a_silent_resume(self) -> None:
        cases = {"violation": dict(second_spawn=True), "unknown": dict(child_error=True), "pending": dict(child_complete=False, mailbox="none", parent_end="open")}
        for label, build in cases.items():
            with self.subTest(label):
                self.reset_fixture(); self.world = World(self.codex_native_root)
                self.herdr.responses = [{"v2": PLAN_GATE + ("-",)}]
                self.write_plan()
                self.sup.run_new("Add the widget", "codex", workflow_policy="gated_v2")
                self.world.build(**build)
                with self.sup.store.transaction():
                    state = self.sup.store.read_state()
                    guard = self.sup.settle_guard(state, "codex", "deliver_prompt", wait=True, patience=10.0, control=False)
                    self.assertNotEqual(guard.kind, "stop", "'stop' is reserved for a control request and would silently resume")
                    self.assertNotEqual(guard.kind, "clear")
                    outcome = self.sup._guard_stop(state, "codex", guard, sent=False, action="deliver the prompt")
                    self.assertEqual((outcome, state["supervisor_state"]), ("stop", "WAIT_USER"))
                    self.assertIn("child-settlement guard", state["wait_user_reason"])

    def state_or_none(self) -> dict:
        return {"run_id": "r", "delivery": {"turn_id": "t"}}

    def test_control_request_during_a_hold_is_the_only_silent_stop(self) -> None:
        self.world.build(child_complete=False, mailbox="none", parent_end="open")
        with mock.patch.object(self.sup, "check_control", return_value=False):
            self.assertEqual(self.sup.settle_guard(self.state_or_none(), "codex", "read_result").kind, "stop")


if __name__ == "__main__":
    unittest.main()
