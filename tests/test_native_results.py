"""Exact-primary native result recovery (supervisor reliability, batch 1). Offline only: synthetic provider-shaped transcripts in temporary roots
(constructed fixtures, not retained captures), a scripted FakeHerdr and FakeClock. Every adversary asserts that nothing is routed, approved,
sent or woken, and that the native files are never modified."""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path
from unittest import mock

import herdr_native_results as nr  # noqa: E402
import herdr_protocol  # noqa: E402
from v2_fixtures import CLAUDE_SESSION, CODEX_SESSION, FakeHerdr, V2Case  # noqa: I001 - first: puts the source or installed layout on sys.path

RUN = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
TURN = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
OTHER_TURN = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
OTHER_RUN = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
PROMPT = f"You are the active agent.\nHERDR_RUN={RUN}\nHERDR_TURN={TURN}\n"


def frame(run: str = RUN, turn: str = TURN, nxt: str = "codex", stage: str = "fix", handoff: str = "Done_see_handoff") -> str:
    return "\n".join(["HERDR_PROTOCOL=2", f"HERDR_RUN={run}", f"HERDR_TURN={turn}", f"HERDR_STAGE={stage}", f"HERDR_NEXT={nxt}", "HERDR_GATE=none", "HERDR_PAYLOAD=-", f"HERDR_HANDOFF={handoff}"])


# ----- provider-shaped record builders (claude primary JSONL / codex rollout)

def c_user(text: str, session: str = CLAUDE_SESSION, **extra) -> dict:
    return {"type": "user", "isSidechain": False, "sessionId": session, "uuid": os.urandom(4).hex(), "message": {"role": "user", "content": text}, **extra}


def c_tool_result(text: str, session: str = CLAUDE_SESSION) -> dict:
    return {"type": "user", "isSidechain": False, "sessionId": session, "uuid": os.urandom(4).hex(),
            "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": text}]}}


def c_final(text: str, session: str = CLAUDE_SESSION, *, sidechain: bool = False, stop="end_turn", blocks=None, **extra) -> dict:
    content = blocks if blocks is not None else [{"type": "text", "text": text}]
    return {"type": "assistant", "isSidechain": sidechain, "sessionId": session, "uuid": os.urandom(4).hex(), "message": {"role": "assistant", "stop_reason": stop, "content": content}, **extra}


def x_meta(session: str = CODEX_SESSION, **overrides) -> dict:
    """A PRIMARY Codex session_meta as observed locally: id == session_id, source a plain string, no child markers."""
    payload = {"id": session, "session_id": session, "source": "cli", "thread_source": "user", "cli_version": "0.160.1"}
    payload.update(overrides)
    return {"type": "session_meta", "payload": payload}


def x_user(text: str) -> dict:
    return {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}}


def x_msg(text: str, *, phase: str = "final_answer", turn: str = "t-1", role: str = "assistant") -> dict:
    return {"type": "response_item", "payload": {"type": "message", "id": os.urandom(4).hex(), "role": role, "phase": phase,
                                                  "content": [{"type": "output_text", "text": text}], "internal_chat_message_metadata_passthrough": {"turn_id": turn}}}


def x_complete(turn: str = "t-1") -> dict:
    return {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": turn}}


def x_abort(turn: str = "t-1") -> dict:
    return {"type": "event_msg", "payload": {"type": "turn_aborted", "turn_id": turn}}


def write_jsonl(path: Path, records: list[dict], *, newline: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(json.dumps(r) for r in records) + ("\n" if newline else "")
    path.write_text(body)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def during_read(path: Path, mutate: Callable[[], None], *, skip: int = 0):
    """Patch `os.read` so that `mutate()` runs once, right after the (skip+1)-th read of `path` returned data: a provider flushing, truncating or
    replacing its transcript concurrently with the supervisor's bounded read. (The Codex lookup's metadata read is the skipped first read.)"""
    real, inode, state = os.read, os.stat(path).st_ino, {"seen": 0, "done": False}

    def wrapper(fd: int, size: int) -> bytes:
        data = real(fd, size)
        if data and not state["done"] and os.fstat(fd).st_ino == inode:
            if state["seen"] >= skip:
                state["done"] = True
                mutate()
            state["seen"] += 1
        return data
    return mock.patch.object(os, "read", wrapper)


def append_text(path: Path, text: str) -> Callable[[], None]:
    def mutate() -> None:
        with path.open("a") as handle:
            handle.write(text)
    return mutate


class Roots(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.roots = {"claude": self.base / "claude" / "projects", "codex": self.base / "codex" / "sessions"}
        self.claude_file = self.roots["claude"] / "-proj" / f"{CLAUDE_SESSION}.jsonl"
        self.codex_file = self.roots["codex"] / "2026" / "10" / "21" / f"rollout-2026-10-21T00-00-00-{CODEX_SESSION}.jsonl"

    def claude(self, **kw) -> nr.NativeResult:
        return nr.resolve("claude", CLAUDE_SESSION, RUN, TURN, roots=self.roots, **kw)

    def codex(self, **kw) -> nr.NativeResult:
        return nr.resolve("codex", CODEX_SESSION, RUN, TURN, roots=self.roots, **kw)


class ClaudeResolverTests(Roots):
    def test_primary_final_after_the_delivered_prompt_is_recovered_and_the_file_is_untouched(self) -> None:
        write_jsonl(self.claude_file, [c_user("older"), c_final("older reply"), c_user(PROMPT), c_tool_result("ls"), c_final("work notes", stop="tool_use"), c_final("Result\n" + frame())])
        before = digest(self.claude_file)
        result = self.claude()
        self.assertEqual((result.status, result.block.next_agent, result.block.run_id, result.block.turn_id), (nr.FOUND, "codex", RUN, TURN))
        self.assertEqual({"provider", "records", "frame_sha256", "message_sha256", "record_offset", "offset_end"} - set(result.provenance), set())
        self.assertEqual(digest(self.claude_file), before)

    def test_wrapped_frame_in_a_native_final_uses_the_existing_strict_parser(self) -> None:
        wrapped = "\n".join("  " + line for line in frame().splitlines())
        write_jsonl(self.claude_file, [c_user(PROMPT), c_final(wrapped)])
        self.assertTrue(self.claude().recoverable)
        broken = frame().replace("HERDR_TURN=" + TURN, "HERDR_TURN=" + TURN[:10])  # truncated identity never parses
        write_jsonl(self.claude_file, [c_user(PROMPT), c_final(broken)])
        self.assertEqual(self.claude().status, nr.NONE)

    def test_cursor_bounds_the_scan_and_a_rotated_or_truncated_file_is_refused(self) -> None:
        write_jsonl(self.claude_file, [c_user("old turn"), c_final("old " + frame(turn=OTHER_TURN))])
        cursor = nr.capture_cursor("claude", CLAUDE_SESSION, self.roots["claude"])
        self.assertEqual(cursor["offset"], self.claude_file.stat().st_size)
        with self.claude_file.open("a") as handle:
            handle.write(json.dumps(c_user(PROMPT)) + "\n" + json.dumps(c_final(frame())) + "\n")
        result = self.claude(cursor=cursor)
        self.assertTrue(result.recoverable)
        self.assertTrue(result.provenance["cursor_used"])
        self.assertEqual(result.provenance["offset_start"], cursor["offset"])
        # rotated: same path, different inode
        replacement = self.claude_file.with_suffix(".new")
        replacement.write_bytes(self.claude_file.read_bytes())
        os.replace(replacement, self.claude_file)
        self.assertEqual(self.claude(cursor=cursor).status, nr.UNAVAILABLE)
        # truncated below the cursor
        cursor2 = nr.capture_cursor("claude", CLAUDE_SESSION, self.roots["claude"])
        self.claude_file.write_text(self.claude_file.read_text()[:20])
        self.assertEqual(self.claude(cursor=cursor2).status, nr.UNAVAILABLE)

    def test_not_a_routing_source_is_never_accepted(self) -> None:
        cases = {
            "frame only in the delivered prompt (echo)": [c_user(PROMPT + frame())],
            "frame in a tool result": [c_user(PROMPT), c_tool_result(frame())],
            "child (sidechain) final": [c_user(PROMPT), c_final(frame(), sidechain=True)],
            "final without an explicit primary flag": [c_user(PROMPT), {k: v for k, v in c_final(frame()).items() if k != "isSidechain"}],
            "assistant partway through a tool call": [c_user(PROMPT), c_final(frame(), stop="tool_use")],
            "assistant still streaming": [c_user(PROMPT), c_final(frame(), stop=None)],
            "thinking block only": [c_user(PROMPT), c_final("", blocks=[{"type": "thinking", "thinking": frame()}])],
            "api error record": [c_user(PROMPT), c_final(frame(), isApiErrorMessage=True)],
            "frame for another turn": [c_user(PROMPT), c_final(frame(turn=OTHER_TURN))],
            "frame for another run": [c_user(PROMPT), c_final(frame(run=OTHER_RUN))],
            "final placed before the prompt": [c_final(frame()), c_user(PROMPT)],
            "record claiming another session": [c_user(PROMPT), c_final(frame(), session=CODEX_SESSION)],
            "meta user record": [c_user(PROMPT, isMeta=True), c_final(frame())],
        }
        for label, records in cases.items():
            with self.subTest(label):
                write_jsonl(self.claude_file, records)
                result = self.claude()
                self.assertFalse(result.recoverable, (label, result))
                self.assertIsNone(result.block)

    def test_prompt_attribution_is_required(self) -> None:
        write_jsonl(self.claude_file, [c_user("unrelated prompt"), c_final(frame())])
        self.assertEqual(self.claude().status, nr.UNCERTAIN)

    def test_competing_finals_are_a_conflict_but_identical_repeats_are_not(self) -> None:
        write_jsonl(self.claude_file, [c_user(PROMPT), c_final(frame(nxt="codex")), c_final(frame(nxt="human").replace("HERDR_GATE=none", "HERDR_GATE=none"))])
        self.assertEqual(self.claude().status, nr.CONFLICT)
        write_jsonl(self.claude_file, [c_user(PROMPT), c_final(frame()), c_tool_result("x"), c_final("again\n" + frame())])
        repeated = self.claude()
        self.assertTrue(repeated.recoverable)
        self.assertEqual(repeated.provenance["eligible_finals"], 2)
        write_jsonl(self.claude_file, [c_user(PROMPT), c_final(frame() + "\n" + frame(nxt="claude"))])
        self.assertEqual(self.claude().status, nr.CONFLICT)

    def test_a_partial_trailing_write_is_uncertain_until_complete(self) -> None:
        write_jsonl(self.claude_file, [c_user(PROMPT), c_final(frame())], newline=False)
        partial = self.claude()
        self.assertEqual(partial.status, nr.UNCERTAIN, "an unterminated trailing record may still change the result")
        self.assertIn("unterminated trailing record", partial.reason)
        with self.claude_file.open("a") as handle:
            handle.write("\n")
        self.assertTrue(self.claude().recoverable, "a later successful flush completes the same frame")

    def test_child_transcripts_below_the_session_directory_are_never_read(self) -> None:
        write_jsonl(self.claude_file, [c_user(PROMPT)])
        write_jsonl(self.claude_file.with_suffix("") / "subagents" / "agent-1.jsonl", [c_user(PROMPT), c_final(frame())])
        self.assertEqual(self.claude().status, nr.NONE)

    def test_lookup_is_identity_derived_and_contained(self) -> None:
        self.assertEqual(self.claude().status, nr.UNAVAILABLE)  # nothing exists
        write_jsonl(self.claude_file, [c_user(PROMPT), c_final(frame())])
        self.assertTrue(self.claude().recoverable)
        # a duplicate under a second project directory is ambiguity, not a choice
        write_jsonl(self.roots["claude"] / "-other" / f"{CLAUDE_SESSION}.jsonl", [c_user(PROMPT), c_final(frame())])
        self.assertEqual(self.claude().status, nr.CONFLICT)
        os.remove(self.roots["claude"] / "-other" / f"{CLAUDE_SESSION}.jsonl")
        # a symlinked transcript or project directory is not followed
        target = self.base / "elsewhere.jsonl"
        write_jsonl(target, [c_user(PROMPT), c_final(frame())])
        self.claude_file.unlink()
        self.claude_file.symlink_to(target)
        self.assertEqual(self.claude().status, nr.UNAVAILABLE)
        self.claude_file.unlink()
        (self.base / "realdir").mkdir()
        write_jsonl(self.base / "realdir" / f"{CLAUDE_SESSION}.jsonl", [c_user(PROMPT), c_final(frame())])
        (self.roots["claude"] / "-linked").symlink_to(self.base / "realdir")
        self.assertEqual(self.claude().status, nr.UNAVAILABLE)
        # a session id that is not a canonical identity never reaches the filesystem
        self.assertEqual(nr.resolve("claude", "../../etc/passwd", RUN, TURN, roots=self.roots).status, nr.UNAVAILABLE)
        # the other provider's identity does not locate a claude transcript
        self.assertEqual(nr.resolve("claude", CODEX_SESSION, RUN, TURN, roots=self.roots).status, nr.UNAVAILABLE)
        self.assertEqual(nr.resolve("gemini", CLAUDE_SESSION, RUN, TURN, roots=self.roots).status, nr.UNSUPPORTED)

    def test_scan_is_bounded_by_bytes_records_line_size_and_time(self) -> None:
        pad = [c_user("x" * 200) for _ in range(60)]
        write_jsonl(self.claude_file, pad + [c_user(PROMPT), c_final(frame())])
        saved = (nr.MAX_SCAN_BYTES, nr.MAX_RECORDS, nr.MAX_LINE_BYTES)
        self.addCleanup(lambda: setattr(nr, "MAX_SCAN_BYTES", saved[0]) or setattr(nr, "MAX_RECORDS", saved[1]) or setattr(nr, "MAX_LINE_BYTES", saved[2]))
        nr.MAX_SCAN_BYTES = 3000  # the tail window holds the prompt and the final but not the padding
        tail = self.claude()
        self.assertTrue(tail.recoverable)
        self.assertLessEqual(tail.provenance["scanned_bytes"], 3000)
        self.assertGreater(self.claude_file.stat().st_size, 3000 * 2)
        nr.MAX_SCAN_BYTES = saved[0]
        nr.MAX_RECORDS = 5
        self.assertEqual(self.claude().status, nr.UNCERTAIN, "record bound exhausted before the end of the window")
        nr.MAX_RECORDS = saved[1]
        nr.MAX_LINE_BYTES = 150  # the final is larger than the per-record bound and is skipped, never parsed
        self.assertFalse(self.claude().recoverable)
        nr.MAX_LINE_BYTES = saved[2]
        ticks = iter(range(0, 1000))
        self.assertEqual(self.claude(clock=lambda: float(next(ticks))).status, nr.UNCERTAIN, "elapsed-time bound")
        # prompt older than the window: attribution not established
        nr.MAX_SCAN_BYTES = 1200
        write_jsonl(self.claude_file, [c_user(PROMPT)] + pad + [c_final(frame())])
        self.assertEqual(self.claude().status, nr.UNCERTAIN)


class RetainedIncidentTests(unittest.TestCase):
    """Redacted extract of the two REAL primary records of the reported incident: the delivered prompt and the primary `end_turn` final, with every original
    key (parentUuid, userType, slug, requestId, …), the message text reduced to the routing identifiers and the frame, and every machine/session/run/turn
    identifier replaced by a synthetic one (the original-identifier extract is kept only in the private review folder). The terminal capture of that turn
    contained zero HERDR lines (retained metadata only: 1,184 characters, no frame)."""

    SESSION = "5e551011-0000-4000-8000-000000000001"
    RUN_ID = "00000000-0000-4000-8000-0000000000a1"
    TURN_ID = "00000000-0000-4000-8000-0000000000b1"
    FIXTURE = Path(__file__).resolve().parent / "fixtures" / "claude_primary_final_redacted.jsonl"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "projects"
        self.path = self.root / "-workspace-redacted" / f"{self.SESSION}.jsonl"
        self.path.parent.mkdir(parents=True)
        self.path.write_bytes(self.FIXTURE.read_bytes())

    def test_the_real_record_shapes_recover_the_exact_routing_result_exactly_once(self) -> None:
        before = digest(self.path)
        result = nr.resolve("claude", self.SESSION, self.RUN_ID, self.TURN_ID, roots={"claude": self.root})
        self.assertEqual((result.status, result.block.stage, result.block.next_agent, result.block.gate), (nr.FOUND, "implement", "codex", "none"))
        self.assertEqual(result.provenance["eligible_finals"], 1)
        self.assertEqual(digest(self.path), before)
        again = nr.resolve("claude", self.SESSION, self.RUN_ID, self.TURN_ID, roots={"claude": self.root})
        self.assertEqual(again.provenance["frame_sha256"], result.provenance["frame_sha256"], "the source fingerprint is stable across repeated recovery")

    def test_other_turns_and_runs_of_the_same_real_session_do_not_match(self) -> None:
        self.assertEqual(nr.resolve("claude", self.SESSION, self.RUN_ID, OTHER_TURN, roots={"claude": self.root}).status, nr.UNCERTAIN)
        self.assertFalse(nr.resolve("claude", self.SESSION, OTHER_RUN, self.TURN_ID, roots={"claude": self.root}).recoverable)

    def test_a_narrow_truncated_terminal_capture_has_no_frame_while_the_native_record_does(self) -> None:
        final = json.loads(self.FIXTURE.read_text().splitlines()[1])["message"]["content"][0]["text"]
        rows = [line for line in final.splitlines() if line.startswith("HERDR_")]
        self.assertEqual(len(rows), 8)
        for width in (12, 24, 40):  # rows cut (not wrapped) at the terminal width, as a narrow capture can drop the logical lines
            captured = "\n".join(row[:width] for row in rows)
            self.assertIsNone(herdr_protocol.parse_protocol(captured, self.RUN_ID, self.TURN_ID), width)
        self.assertTrue(nr.resolve("claude", self.SESSION, self.RUN_ID, self.TURN_ID, roots={"claude": self.root}).recoverable)


class CodexResolverTests(Roots):
    def test_final_answer_completed_by_task_complete_is_recovered(self) -> None:
        write_jsonl(self.codex_file, [x_meta(), x_user(PROMPT), x_msg("thinking aloud", phase="commentary"), x_msg("Result\n" + frame()), x_complete()])
        before = digest(self.codex_file)
        result = self.codex()
        self.assertEqual((result.status, result.block.next_agent), (nr.FOUND, "codex"))
        self.assertEqual(digest(self.codex_file), before)

    def test_not_a_routing_source_is_never_accepted(self) -> None:
        cases = {
            "commentary phase": [x_meta(), x_user(PROMPT), x_msg(frame(), phase="commentary"), x_complete()],
            "final without task_complete (partial turn)": [x_meta(), x_user(PROMPT), x_msg(frame())],
            "aborted turn": [x_meta(), x_user(PROMPT), x_msg(frame()), x_abort(), x_complete()],
            "completion of a different turn": [x_meta(), x_user(PROMPT), x_msg(frame(), turn="t-1"), x_complete("t-2")],
            "completion before the final": [x_meta(), x_user(PROMPT), x_complete(), x_msg(frame())],
            "frame in the prompt": [x_meta(), x_user(PROMPT + frame()), x_complete()],
            "frame in a user message after the prompt": [x_meta(), x_user(PROMPT), x_msg(frame(), role="user"), x_complete()],
            "frame for another turn": [x_meta(), x_user(PROMPT), x_msg(frame(turn=OTHER_TURN)), x_complete()],
            "final before the prompt": [x_meta(), x_msg(frame()), x_complete(), x_user(PROMPT)],
        }
        for label, records in cases.items():
            with self.subTest(label):
                write_jsonl(self.codex_file, records)
                result = self.codex()
                self.assertFalse(result.recoverable, (label, result))

    def test_conflict_and_lookup_rules(self) -> None:
        write_jsonl(self.codex_file, [x_meta(), x_user(PROMPT), x_msg(frame(nxt="codex"), turn="t-1"), x_complete("t-1"), x_msg(frame(nxt="claude"), turn="t-2"), x_complete("t-2")])
        self.assertEqual(self.codex().status, nr.CONFLICT)
        write_jsonl(self.codex_file, [x_meta(), x_user(PROMPT), x_msg(frame()), x_complete()])
        self.assertTrue(self.codex().recoverable)
        twin = self.roots["codex"] / "2026" / "10" / "22" / f"rollout-2026-10-22T00-00-00-{CODEX_SESSION}.jsonl"
        write_jsonl(twin, [x_meta(), x_user(PROMPT), x_msg(frame()), x_complete()])
        self.assertEqual(self.codex().status, nr.CONFLICT)
        twin.unlink()
        self.assertEqual(nr.resolve("codex", CLAUDE_SESSION, RUN, TURN, roots=self.roots).status, nr.UNAVAILABLE)


class PromptEchoTests(V2Case):
    def test_the_generated_prompt_never_contains_a_parser_acceptable_current_frame(self) -> None:
        self.write_plan()
        self.herdr.responses = [{"output": "no frame yet", "status": "idle"}]
        self.sup.run_new("Add the widget", "codex", workflow_policy="gated_v2")
        state = self.state()
        prompt = self.herdr.prompts[0][1]
        run, turn = FakeHerdr.ids(prompt)
        self.assertIsNone(herdr_protocol.parse_protocol(prompt, run, turn))
        self.assertEqual(herdr_protocol.find_protocol_blocks(prompt), [])
        self.assertEqual(state["delivery"]["turn_id"], turn) if isinstance(state.get("delivery"), dict) else None


class NativeFlow(V2Case):
    """Controller-level consumers. Claude runs go through the gated workflow like the guard tests (Codex plans, approval hands Claude a brief turn)."""

    def make_supervisor(self):
        sup = super().make_supervisor()
        root = Path(self.tmp.name) / "native"
        sup.native_roots = {"claude": root / "claude", "codex": root / "codex"}
        return sup

    @property
    def claude_path(self) -> Path:
        return Path(self.tmp.name) / "native" / "claude" / "-proj" / f"{CLAUDE_SESSION}.jsonl"

    @property
    def codex_path(self) -> Path:
        return Path(self.tmp.name) / "native" / "codex" / "2026" / "10" / "21" / f"rollout-2026-10-21T00-00-00-{CODEX_SESSION}.jsonl"

    def claude_prompts(self) -> list:
        return [p for p in self.herdr.prompts if p[0] == "claude-main"]

    def log(self) -> list[dict]:
        path = self.paths.logs_dir / f"{self.state()['run_id']}.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()]

    def events_named(self, name: str) -> list[dict]:
        return [e for e in self.log() if e["event"] == name]

    def native_writer(self, *, path: Path, provider: str = "claude", complete: bool = True, final_text=None, extra_final=None):
        """A prompt-time hook: appends the delivered prompt and the primary final (with a routing frame) to the native transcript, as the provider would."""
        def before(fake, name, text):
            run, turn = FakeHerdr.ids(text)
            frame_text = final_text if final_text is not None else FakeHerdr.block_v2(run, turn, "brief", "human", "generic_question", str(self.question_payload()), prefix="")
            path.parent.mkdir(parents=True, exist_ok=True)
            records = [c_user(text), c_final("Result\n" + frame_text)] if provider == "claude" else [x_user(text), x_msg("Result\n" + frame_text)] + ([x_complete()] if complete else [])
            if extra_final is not None:
                records += extra_final(run, turn)
            with path.open("a") as handle:
                for record in records:
                    handle.write(json.dumps(record) + "\n")
        return before

    def claude_flow(self, claude_reply: dict) -> int:
        self.write_plan()
        write_jsonl(self.claude_path, [c_user("earlier conversation"), c_final("earlier reply")])
        self.assertEqual(self.start_gated([{"v2": ("plan", "human", "plan_approval", str(self.plan_payload()))}]), 4)
        self.approve_pending()
        return self.resume_with([{"v2": ("brief", "claude")}, claude_reply])

    def assert_nothing_sent_beyond_the_turn(self) -> None:
        self.assertEqual(len(self.claude_prompts()), 1)
        self.assertEqual(self.herdr.sent_keys, [])

    def pending(self) -> dict:
        return self.state().get("pending_gate") or {}


class ClaudeControllerTests(NativeFlow):
    def test_terminal_missing_native_valid_recovers_exactly_once_through_the_ordinary_route(self) -> None:
        reply = {"output": "wrapped screen without any frame", "before": self.native_writer(path=self.claude_path)}
        self.assertEqual(self.claude_flow(reply), 2)
        state = self.state()
        self.assertEqual((self.pending().get("gate_type"), self.pending().get("status")), ("generic_question", "pending"))
        self.assertEqual(len(state["native_recoveries"]), 1)
        self.assertEqual(len(self.events_named("native_result_recovered")), 1)
        self.assertEqual(self.events_named("native_result_lookup")[0]["status"], nr.FOUND)
        self.assertIsNotNone(state["delivery"]["native_cursor"], "a source boundary was recorded for the new delivery")
        self.assert_nothing_sent_beyond_the_turn()
        self.assertIsNone(state.get("missing_result"))
        gates = [e for e in self.log() if e["event"] == "gate_created"]
        self.assertLessEqual(len(gates), 2)  # the plan gate and this one; never a second generic_question gate

    def test_terminal_success_is_unchanged_and_never_consults_the_native_transcript(self) -> None:
        reply = {"v2": ("brief", "human", "generic_question", str(self.question_payload()))}
        self.assertEqual(self.claude_flow(reply), 2)
        self.assertEqual(self.events_named("native_result_lookup"), [])
        self.assertNotIn("native_recoveries", self.state())
        self.assert_nothing_sent_beyond_the_turn()

    def test_terminal_conflict_is_not_masked_by_a_clean_native_result(self) -> None:
        def both_frames(fake, name, text):
            run, turn = FakeHerdr.ids(text)
            self.native_writer(path=self.claude_path)(fake, name, text)
            fake.outputs  # terminal output is set from the 'output' key below after this hook

        conflict = lambda run, turn: FakeHerdr.block_v2(run, turn, "brief", "human", "generic_question", str(self.question_payload())) + "\n" + FakeHerdr.block_v2(run, turn, "brief", "claude", "none", "-")  # noqa: E731
        self.write_plan()
        write_jsonl(self.claude_path, [c_user("earlier"), c_final("earlier reply")])
        self.start_gated([{"v2": ("plan", "human", "plan_approval", str(self.plan_payload()))}])
        self.approve_pending()
        original_prompt = self.herdr.prompt

        def prompt(name, text, *, timeout_ms):
            result = original_prompt(name, text, timeout_ms=timeout_ms)
            if name == "claude-main":
                run, turn = FakeHerdr.ids(text)
                self.herdr.outputs[name] = text + "\n" + conflict(run, turn)
            return result

        self.herdr.prompt = prompt
        self.assertEqual(self.resume_with([{"v2": ("brief", "claude")}, {"output": "x", "before": self.native_writer(path=self.claude_path)}]), 2)
        self.assertEqual(self.state()["supervisor_state"], "WAIT_USER")
        self.assertEqual(self.events_named("native_result_lookup"), [])
        self.assertNotEqual(self.pending().get("gate_type"), "generic_question")

    def test_a_hold_between_the_lookup_and_the_route_defers_routing_and_still_recovers_once(self) -> None:
        reply = {"output": "no frame", "before": self.native_writer(path=self.claude_path)}
        calls = {"n": 0}

        def script(fake, name):
            if not self.claude_prompts():
                return
            calls["n"] += 1
            late = calls["n"] == 6  # the first explain after the quiet window cleared is the recheck before routing
            fake.explain_matched = {"background_agents_working"} if late else set()
            fake.explain_state = "working" if late else "idle"

        self.herdr.on_explain = script
        self.assertEqual(self.claude_flow(reply), 2)
        self.assertEqual(len(self.events_named("native_route_deferred")), 1)
        self.assertEqual(len(self.events_named("native_result_lookup")), 2, "found once, deferred by the recheck, found again after the next quiet window")
        self.assertEqual(len(self.events_named("native_result_recovered")), 1)
        self.assertEqual(len(self.state()["native_recoveries"]), 1)
        self.assert_nothing_sent_beyond_the_turn()
        self.assertEqual(self.pending().get("gate_type"), "generic_question")

    def test_a_held_pane_is_never_searched_natively(self) -> None:
        reply = {"output": "no frame", "before": self.native_writer(path=self.claude_path)}
        state = {"holding": 3}

        def script(fake, name):
            if not self.claude_prompts():
                return
            hold = state["holding"] > 0
            state["holding"] -= 1 if hold else 0
            fake.explain_matched = {"background_agents_working"} if hold else set()
            fake.explain_state = "working" if hold else "idle"

        self.herdr.on_explain = script
        self.claude_flow(reply)
        lookups = self.events_named("native_result_lookup")
        holds = self.events_named("settle_guard_hold")
        self.assertEqual(len(holds), 3, "one held sample per scripted indicator sample")
        self.assertEqual(len(lookups), 1, "the lookup happened once, after the guard cleared")

    def test_native_conflict_stops_with_an_actionable_wait_and_sends_nothing(self) -> None:
        def two(run, turn):
            return [c_final(FakeHerdr.block_v2(run, turn, "brief", "claude", "none", "-", prefix=""))]

        reply = {"output": "no frame", "before": self.native_writer(path=self.claude_path, extra_final=two)}
        self.assertEqual(self.claude_flow(reply), 2)
        state = self.state()
        self.assertEqual((state["supervisor_state"], state["wait_user_requires_action"]), ("WAIT_USER", True))
        self.assertIn("conflicting routing results", state["wait_user_reason"])
        self.assertNotEqual(self.pending().get("gate_type"), "generic_question")
        self.assertNotIn("native_recoveries", state)
        self.assert_nothing_sent_beyond_the_turn()

    def test_unavailable_native_transcript_keeps_the_existing_missing_result_recovery_and_names_the_reason(self) -> None:
        reply = {"output": "no frame"}
        self.write_plan()
        self.assertEqual(self.start_gated([{"v2": ("plan", "human", "plan_approval", str(self.plan_payload()))}]), 4)
        self.approve_pending()
        self.assertEqual(self.resume_with([{"v2": ("brief", "claude")}, reply]), 2)
        state = self.state()
        self.assertEqual(state["supervisor_state"], "WAIT_USER")
        self.assertEqual(state["missing_result"]["native"]["status"], nr.UNAVAILABLE)
        self.assertIn("Native transcript recovery: unavailable", state["wait_user_reason"])
        self.assert_nothing_sent_beyond_the_turn()

    def test_uncertain_delivery_is_never_resolved_from_a_native_result(self) -> None:
        reply = {"output": "no frame", "error": "agent_prompt_stalled", "before": self.native_writer(path=self.claude_path)}
        self.claude_flow(reply)
        state = self.state()
        self.assertEqual(state["supervisor_state"], "WAIT_USER")
        self.assertEqual(self.events_named("native_result_lookup"), [])
        self.assertNotEqual(self.pending().get("gate_type"), "generic_question")
        self.assert_nothing_sent_beyond_the_turn()

    def test_explicit_retry_uses_the_same_resolver_once_and_sends_nothing(self) -> None:
        self.write_plan()
        write_jsonl(self.claude_path, [c_user("earlier"), c_final("earlier reply")])
        self.assertEqual(self.start_gated([{"v2": ("plan", "human", "plan_approval", str(self.plan_payload()))}]), 4)
        self.approve_pending()
        self.assertEqual(self.resume_with([{"v2": ("brief", "claude")}, {"output": "no frame"}]), 2)
        state = self.state()
        turn = state["missing_result"]["turn_id"]
        prompts_before = len(self.herdr.prompts)
        # the provider's transcript becomes readable (e.g. the file was only flushed later)
        run = state["run_id"]
        with self.claude_path.open("a") as handle:
            frame_text = FakeHerdr.block_v2(run, turn, "brief", "human", "generic_question", str(self.question_payload()), prefix="")
            handle.write(json.dumps(c_user(self.herdr.prompts[-1][1])) + "\n" + json.dumps(c_final(frame_text)) + "\n")
        result = self.sup.retry_routing_result(state, run_id=run, turn_id=turn, actor="test")
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.pending().get("gate_type"), "generic_question")
        self.assertEqual(len(self.state()["native_recoveries"]), 1)
        self.assertEqual(len(self.herdr.prompts), prompts_before)
        self.assertEqual(self.herdr.sent_keys, [])
        with self.assertRaises(hs_error()):
            self.sup.retry_routing_result(self.state(), run_id=run, turn_id=turn, actor="test")

    def test_a_second_recovery_for_the_same_turn_is_refused_or_harmless(self) -> None:
        reply = {"output": "no frame", "before": self.native_writer(path=self.claude_path)}
        self.claude_flow(reply)
        state = self.state()
        delivery_like = {"turn_id": state["native_recoveries"][0]["turn_id"]}
        same = nr.NativeResult(nr.FOUND, "x", block=None, provenance={"frame_sha256": state["native_recoveries"][0]["frame_sha256"]})
        self.assertIsNone(self.sup._accept_native_block(state, "claude", delivery_like, same, purpose="monitor"))  # duplicate: harmless, creates nothing
        different = nr.NativeResult(nr.FOUND, "x", block=None, provenance={"frame_sha256": "0" * 64})
        with self.assertRaises(hs_error()):
            self.sup._accept_native_block(state, "claude", delivery_like, different, purpose="monitor")
        self.assertEqual(len(state["native_recoveries"]), 1)


def hs_error():
    import herdr_core  # noqa: PLC0415

    return herdr_core.SupervisorError


class CodexControllerTests(NativeFlow):
    def test_codex_final_recovers_through_the_monitor_without_a_second_prompt(self) -> None:
        self.write_plan()
        write_jsonl(self.codex_path, [x_meta(), x_user("earlier"), x_msg("earlier reply"), x_complete("t-0")])

        def before(fake, name, text):
            run, turn = FakeHerdr.ids(text)
            frame_text = FakeHerdr.block_v2(run, turn, "plan", "human", "plan_approval", str(self.plan_payload()), prefix="")
            with self.codex_path.open("a") as handle:
                for record in (x_user(text), x_msg("Plan ready\n" + frame_text, turn="t-1"), x_complete("t-1")):
                    handle.write(json.dumps(record) + "\n")

        self.herdr.responses = [{"output": "no frame", "before": before}]
        self.assertEqual(self.sup.run_new("Add the widget", "codex", workflow_policy="gated_v2"), 4)
        self.assertEqual(self.pending().get("gate_type"), "plan_approval")
        self.assertEqual(len(self.state()["native_recoveries"]), 1)
        self.assertEqual(len([p for p in self.herdr.prompts if p[0] == "codex-main"]), 1)
        self.assertEqual(self.herdr.sent_keys, [])

    def test_codex_final_without_task_complete_is_not_routed(self) -> None:
        self.write_plan()
        self.herdr.responses = [{"output": "no frame", "before": self.native_writer(path=self.codex_path, provider="codex", complete=False,
                                                                                     final_text=FakeHerdr.block_v2(RUN, TURN, "plan", "human", "plan_approval", "-", prefix=""))}]
        self.sup.run_new("Add the widget", "codex", workflow_policy="gated_v2")
        state = self.state()
        self.assertEqual(state["supervisor_state"], "WAIT_USER")
        self.assertNotIn("native_recoveries", state)
        self.assertEqual(self.pending().get("status"), None)
        self.assertEqual(len(self.herdr.prompts), 1)


class IdentityTests(Roots):
    """CI1: the file name alone never authenticates a transcript. Provider metadata and strict record shapes must establish the owned PRIMARY session."""

    def test_codex_metadata_must_prove_the_owned_primary_session(self) -> None:
        body = [x_user(PROMPT), x_msg(frame()), x_complete()]
        cases = {
            "metadata id is another session": [x_meta("33333333-3333-4333-8333-333333333333", session_id="33333333-3333-4333-8333-333333333333"), *body],
            "id matches but session_id points at a parent": [x_meta(session_id="33333333-3333-4333-8333-333333333333"), *body],
            "subagent source object": [x_meta(source={"subagent": {"thread_spawn": {"parent_thread_id": "x"}}}), *body],
            "child markers on a string source": [x_meta(parent_thread_id="01a0", agent_nickname="guardian"), *body],
            "source missing": [{"type": "session_meta", "payload": {"id": CODEX_SESSION, "session_id": CODEX_SESSION}}, *body],
            "first record is not session_meta": [{"type": "turn_context", "payload": {}}, *body],
            "payload is not an object": [{"type": "session_meta", "payload": ["id"]}, *body],
            "metadata record is a list": [["session_meta"], *body],
            "no metadata at all": body,
        }
        for label, records in cases.items():
            with self.subTest(label):
                write_jsonl(self.codex_file, records)
                result = self.codex()
                self.assertEqual(result.status, nr.IDENTITY, (label, result))
                self.assertFalse(result.recoverable)
        write_jsonl(self.codex_file, [x_meta(), *body])
        self.assertTrue(self.codex().recoverable, "positive control: the same transcript with proven primary metadata")
        self.codex_file.write_text("{not json\n" + self.codex_file.read_text().split("\n", 1)[1])
        self.assertEqual(self.codex().status, nr.IDENTITY)

    def test_codex_metadata_is_verified_even_when_the_window_starts_after_a_cursor_or_in_a_bounded_tail(self) -> None:
        write_jsonl(self.codex_file, [x_meta("33333333-3333-4333-8333-333333333333", session_id="33333333-3333-4333-8333-333333333333"), x_user("old"), x_msg("old", turn="t-0"), x_complete("t-0")])
        cursor = nr.capture_cursor("codex", CODEX_SESSION, self.roots["codex"])
        with self.codex_file.open("a") as handle:
            for record in (x_user(PROMPT), x_msg(frame()), x_complete()):
                handle.write(json.dumps(record) + "\n")
        self.assertEqual(self.codex(cursor=cursor).status, nr.IDENTITY, "metadata sits before the cursor but is still checked")
        saved = nr.MAX_SCAN_BYTES
        self.addCleanup(lambda: setattr(nr, "MAX_SCAN_BYTES", saved))
        nr.MAX_SCAN_BYTES = 400
        self.assertEqual(self.codex().status, nr.IDENTITY, "bounded tail window")
        # positive controls with correct metadata through both window kinds
        write_jsonl(self.codex_file, [x_meta(), x_user("old" * 40), x_msg("old", turn="t-0"), x_complete("t-0")])
        cursor = nr.capture_cursor("codex", CODEX_SESSION, self.roots["codex"])
        with self.codex_file.open("a") as handle:
            for record in (x_user(PROMPT), x_msg(frame()), x_complete()):
                handle.write(json.dumps(record) + "\n")
        nr.MAX_SCAN_BYTES = saved
        self.assertTrue(self.codex(cursor=cursor).recoverable)
        nr.MAX_SCAN_BYTES = sum(len(json.dumps(r)) + 1 for r in (x_user(PROMPT), x_msg(frame()), x_complete())) + 60  # tail window holds only the last turn
        self.assertTrue(self.codex().recoverable)

    def test_claude_records_must_all_carry_the_owned_session_and_a_primary_marker(self) -> None:
        def strip(record: dict, key: str) -> dict:
            return {k: v for k, v in record.items() if k != key}

        cases = {
            "sessionId removed from both records": [strip(c_user(PROMPT), "sessionId"), strip(c_final(frame()), "sessionId")],
            "sessionId removed from the final": [c_user(PROMPT), strip(c_final(frame()), "sessionId")],
            "another session on the prompt": [c_user(PROMPT, session=CODEX_SESSION), c_final(frame())],
            "agentId marks a child record as primary": [c_user(PROMPT), {**c_final(frame()), "agentId": "a1"}],
            "primary marker removed": [c_user(PROMPT), strip(c_final(frame()), "isSidechain")],
            "sessionId is not a string": [c_user(PROMPT), {**c_final(frame()), "sessionId": [CLAUDE_SESSION]}],
        }
        for label, records in cases.items():
            with self.subTest(label):
                write_jsonl(self.claude_file, records)
                result = self.claude()
                self.assertFalse(result.recoverable, (label, result))
                self.assertIn(result.status, (nr.IDENTITY, nr.UNCERTAIN), label)
        write_jsonl(self.claude_file, [c_user(PROMPT), c_final(frame())])
        self.assertTrue(self.claude().recoverable, "positive control")

    def test_malformed_nested_metadata_is_an_attributed_refusal_never_an_exception(self) -> None:
        for label, record in {"final with a non-dict passthrough": {"type": "response_item", "payload": {"type": "message", "role": "assistant", "phase": "final_answer", "content": [], "internal_chat_message_metadata_passthrough": ["t"]}},
                              "message role is a list": {"type": "response_item", "payload": {"type": "message", "role": ["assistant"], "content": "x"}},
                              "payload is a string": {"type": "response_item", "payload": "x"}}.items():
            with self.subTest(label):
                write_jsonl(self.codex_file, [x_meta(), x_user(PROMPT), record, x_msg(frame()), x_complete()])
                result = self.codex()
                self.assertIn(result.status, (nr.UNCERTAIN, nr.FOUND, nr.NONE), label)  # no exception; result-bearing shapes after the prompt are uncertain
        for label, records in {"message is a list": [c_user(PROMPT), {**c_final(frame()), "message": ["end_turn"]}],
                               "content is a number": [c_user(PROMPT), c_final("x", blocks=None) | {"message": {"role": "assistant", "stop_reason": "end_turn", "content": 7}}]}.items():
            with self.subTest(label):
                write_jsonl(self.claude_file, records)
                self.assertFalse(self.claude().recoverable, label)


class CompletenessTests(Roots):
    """CI2: unread, malformed, oversized, partial or unsupported result-bearing records AFTER the prompt prevent attribution."""

    def test_unknown_or_incomplete_evidence_after_the_prompt_is_uncertain_never_the_earlier_frame(self) -> None:
        prompt, final = c_user(PROMPT), c_final(frame())
        write_jsonl(self.claude_file, [prompt, final])
        with self.claude_file.open("a") as handle:
            handle.write("{this is not json\n")
        malformed = self.claude()
        self.assertEqual(malformed.status, nr.UNCERTAIN)
        self.assertIn("malformed record", malformed.reason)
        write_jsonl(self.claude_file, [prompt, final])
        competing = json.dumps(c_final(frame(nxt="claude")))
        with self.claude_file.open("a") as handle:
            handle.write(competing[: len(competing) - 5])  # a different, complete frame inside a record that lacks its end and newline
        partial = self.claude()
        self.assertEqual(partial.status, nr.UNCERTAIN)
        self.assertGreater(partial.provenance["partial_tail_bytes"], 0)
        with self.claude_file.open("a") as handle:  # the later successful flush now shows the competing result
            handle.write(competing[len(competing) - 5:] + "\n")
        self.assertEqual(self.claude().status, nr.CONFLICT)
        write_jsonl(self.claude_file, [prompt, final])
        saved = nr.MAX_LINE_BYTES
        self.addCleanup(lambda: setattr(nr, "MAX_LINE_BYTES", saved))
        limit = max(len(json.dumps(prompt)), len(json.dumps(final))) + 20  # large enough for the real records, too small for the injected one
        with self.claude_file.open("a") as handle:
            handle.write(json.dumps(c_user("x" * (limit * 2))) + "\n")
        nr.MAX_LINE_BYTES = limit
        oversized = self.claude()
        self.assertEqual(oversized.status, nr.UNCERTAIN)
        self.assertIn("oversized record", oversized.reason)

    def test_harmless_metadata_and_pre_prompt_noise_do_not_block_recovery(self) -> None:
        noise = [{"type": "attachment", "attachment": {"x": 1}}, {"type": "queue-operation", "operation": "enqueue"}, {"type": "file-history-snapshot", "snapshot": {}}]
        with self.claude_file.open("w") if False else open(os.devnull, "w"):
            pass
        self.claude_file.parent.mkdir(parents=True, exist_ok=True)
        self.claude_file.write_text("{garbage before the prompt\n" + "\n".join(json.dumps(r) for r in [*noise, c_user(PROMPT), *noise, c_final(frame()), *noise]) + "\n")
        self.assertTrue(self.claude().recoverable, "unreadable records BEFORE the prompt and non-result metadata AFTER it are not evidence about the result")

    def test_unsupported_result_bearing_shapes_after_the_prompt_are_uncertain(self) -> None:
        write_jsonl(self.claude_file, [c_user(PROMPT), c_final(frame()), {"type": "assistant", "isSidechain": False, "sessionId": CLAUDE_SESSION, "message": "unexpected shape"}])
        self.assertEqual(self.claude().status, nr.UNCERTAIN)
        write_jsonl(self.codex_file, [x_meta(), x_user(PROMPT), x_msg(frame()), x_complete(), {"type": "response_item", "payload": {"type": "message", "phase": "final_answer", "content": []}}])
        self.assertEqual(self.codex().status, nr.UNCERTAIN)

    def test_unsupported_result_bearing_content_is_uncertain_for_both_providers_and_valid_controls_still_recover(self) -> None:
        """CI2 follow-up A: after a valid prompt and final, any final whose containers or primitives cannot be understood is evidence we cannot
        read, never an empty result. Valid empty content, thinking/tool blocks and documented metadata stay harmless."""
        claude_bad = {
            "text block with a number": c_final("", blocks=[{"type": "text", "text": 17}]),
            "text block without text": c_final("", blocks=[{"type": "text"}]),
            "block that is not an object": c_final("", blocks=["text"]),
            "block without a type": c_final("", blocks=[{"text": "x"}]),
            "content is a number": c_final("", blocks=7),
            "content is an object": c_final("", blocks={"type": "text", "text": "x"}),
            "stop_reason is not a string": c_final("", blocks=[]) | {"message": {"role": "assistant", "stop_reason": ["end_turn"], "content": []}},
            "role is not assistant": c_final("", blocks=[]) | {"message": {"role": "user", "stop_reason": "end_turn", "content": []}},
        }
        claude_ok = {
            "empty content list": c_final("", blocks=[]),
            "empty string": c_final("", blocks=[{"type": "text", "text": ""}]),
            "thinking and tool blocks": c_final("", blocks=[{"type": "thinking", "thinking": "..."}, {"type": "tool_use", "id": "t", "name": "n", "input": {}}]),
            "interim tool-use turn": c_final("", stop="tool_use", blocks=[{"type": "text", "text": 17}]),  # not a final: its text is never read
            "partial streaming record": c_final("", stop=None, blocks=7),
        }
        for label, bad in claude_bad.items():
            with self.subTest("claude bad: " + label):
                write_jsonl(self.claude_file, [c_user(PROMPT), c_final(frame()), bad])
                result = self.claude()
                self.assertEqual(result.status, nr.UNCERTAIN, label)
                self.assertIsNone(result.block)
                write_jsonl(self.claude_file, [c_user(PROMPT), bad, c_final(frame())])  # also when the unreadable record comes first
                self.assertEqual(self.claude().status, nr.UNCERTAIN, label)
        for label, good in claude_ok.items():
            with self.subTest("claude ok: " + label):
                write_jsonl(self.claude_file, [c_user(PROMPT), c_final(frame()), good])
                self.assertTrue(self.claude().recoverable, label)
        write_jsonl(self.claude_file, [c_user(PROMPT), c_final(frame()), c_final("", blocks=[{"type": "text", "text": 17}]) | {"isSidechain": True}])
        self.assertTrue(self.claude().recoverable, "a child record is never eligible, so its odd shape is not evidence")

        valid = [x_meta(), x_user(PROMPT), x_msg(frame()), x_complete()]
        bad_item = lambda **p: {"type": "response_item", "payload": p}  # noqa: E731
        codex_bad = {
            "response_item payload is a list": {"type": "response_item", "payload": ["message"]},
            "event_msg payload is a string": {"type": "event_msg", "payload": "task_complete"},
            "response_item without a payload": {"type": "response_item"},
            "record without a type": {"payload": {"type": "message"}},
            "record type is a number": {"type": 3, "payload": {}},
            "unsupported record type": {"type": "response_items", "payload": {"type": "message"}},
            "payload without a type": bad_item(role="assistant"),
            "payload type is a number": bad_item(type=5),
            "message without a role": bad_item(type="message", phase="final_answer", content=[]),
            "assistant phase is a number": bad_item(type="message", role="assistant", phase=3, content=[]),
            "final content is a number": x_msg("") | {"payload": {**x_msg("")["payload"], "content": 5}},
            "final text block without text": x_msg("") | {"payload": {**x_msg("")["payload"], "content": [{"type": "output_text"}]}},
            "final text is a number": x_msg("") | {"payload": {**x_msg("")["payload"], "content": [{"type": "output_text", "text": 9}]}},
            "final block is not an object": x_msg("") | {"payload": {**x_msg("")["payload"], "content": ["x"]}},
            "task_complete without a turn id": {"type": "event_msg", "payload": {"type": "task_complete"}},
            "turn_aborted turn id is a number": {"type": "event_msg", "payload": {"type": "turn_aborted", "turn_id": 4}},
        }
        codex_ok = {
            "reasoning item": bad_item(type="reasoning", summary=[]),
            "tool call item": bad_item(type="function_call", name="n", arguments="{}"),
            "commentary message": x_msg("progress", phase="commentary"),
            "developer message": x_msg("rules", phase="x", role="developer"),
            "valid empty final": x_msg("", turn="t-2") | {"payload": {**x_msg("", turn="t-2")["payload"], "content": []}},
            "token usage": {"type": "token_usage_record", "payload": {"total": 1}},
            "turn context": {"type": "turn_context", "payload": {"cwd": "/x"}},
            "event without special meaning": {"type": "event_msg", "payload": {"type": "token_count", "info": None}},
        }
        for label, bad in codex_bad.items():
            with self.subTest("codex bad: " + label):
                write_jsonl(self.codex_file, [*valid, bad])
                result = self.codex()
                self.assertEqual(result.status, nr.UNCERTAIN, label)
                self.assertIsNone(result.block)
        for label, good in codex_ok.items():
            with self.subTest("codex ok: " + label):
                write_jsonl(self.codex_file, [*valid, good])
                self.assertTrue(self.codex().recoverable, label)

    def test_the_review_probes_for_unsupported_shapes_are_uncertain(self) -> None:
        write_jsonl(self.codex_file, [x_meta(), x_user(PROMPT), x_msg(frame()), x_complete(), {"type": "response_item", "payload": [{"type": "message", "role": "assistant"}]}])
        self.assertEqual(self.codex().status, nr.UNCERTAIN)
        write_jsonl(self.claude_file, [c_user(PROMPT), c_final(frame()), c_final("", blocks=[{"type": "text", "text": 17}]) | {"message": {"role": "assistant", "stop_reason": "end_turn", "content": [{"type": "text", "text": 17}]}}])
        self.assertEqual(self.claude().status, nr.UNCERTAIN)

    def test_a_record_appended_while_the_bounded_window_is_read_is_uncertain_then_the_settled_file_decides(self) -> None:
        """CI2 follow-up B: the sampled extent is re-checked after the read and parse. The bound is the sampled size and is never extended to chase a writer."""
        competing = json.dumps(c_final(frame(nxt="claude"))) + "\n"
        for label, size_bound in (("at the byte limit", True), ("within the byte limit", False)):
            with self.subTest(label):
                write_jsonl(self.claude_file, [c_user(PROMPT), c_final(frame())])
                size = self.claude_file.stat().st_size
                saved = nr.MAX_SCAN_BYTES
                self.addCleanup(lambda saved=saved: setattr(nr, "MAX_SCAN_BYTES", saved))
                if size_bound:
                    nr.MAX_SCAN_BYTES = size  # the review's probe: the window ends exactly at the sampled size
                with during_read(self.claude_file, append_text(self.claude_file, competing)):
                    result = self.claude()
                self.assertEqual(result.status, nr.UNCERTAIN, label)
                self.assertIn("grew from", result.reason)
                self.assertIsNone(result.block)
                self.assertEqual(result.provenance["scanned_bytes"], size, "the unchecked newest bytes were never parsed")
                nr.MAX_SCAN_BYTES = saved
                self.assertEqual(self.claude().status, nr.CONFLICT, "the settled file now shows the competing result; no provider turn was needed")
        write_jsonl(self.claude_file, [c_user(PROMPT), c_final(frame())])
        with during_read(self.claude_file, append_text(self.claude_file, json.dumps({"type": "attachment", "attachment": {}}) + "\n")):
            self.assertEqual(self.claude().status, nr.UNCERTAIN, "even a non-result append is refused rather than guessed harmless")
        self.assertTrue(self.claude().recoverable, "the settled file recovers the single frame on retry")
        write_jsonl(self.claude_file, [c_user(PROMPT), c_final(frame())])
        self.assertTrue(self.claude().recoverable, "positive control: an unchanged file")

        write_jsonl(self.codex_file, [x_meta(), x_user(PROMPT), x_msg(frame()), x_complete()])
        later = json.dumps(x_msg(frame(nxt="claude"), turn="t-2")) + "\n" + json.dumps(x_complete("t-2")) + "\n"
        with during_read(self.codex_file, append_text(self.codex_file, later), skip=1):
            result = self.codex()
        self.assertEqual((result.status, result.block), (nr.UNCERTAIN, None))
        self.assertEqual(self.codex().status, nr.CONFLICT)

    def test_a_change_during_parsing_is_caught_too_so_the_returned_evidence_is_the_evidence_checked(self) -> None:
        write_jsonl(self.claude_file, [c_user(PROMPT), c_final(frame())])
        real_scan = nr.scan_claude

        def scan_then_append(*args, **kwargs):
            outcome = real_scan(*args, **kwargs)
            append_text(self.claude_file, json.dumps(c_final(frame(nxt="claude"))) + "\n")()
            return outcome

        with mock.patch.object(nr, "scan_claude", scan_then_append):
            result = self.claude()
        self.assertEqual((result.status, result.block), (nr.UNCERTAIN, None))
        self.assertIn("grew from", result.reason)

    def test_truncation_replacement_and_in_place_rewrite_during_the_read_are_uncertain(self) -> None:
        records = [c_user(PROMPT), c_final(frame())]
        write_jsonl(self.claude_file, records)
        size = self.claude_file.stat().st_size

        def truncate() -> None:
            os.truncate(self.claude_file, size - 20)

        def replace() -> None:
            other = self.claude_file.with_name("other.tmp")
            other.write_bytes(self.claude_file.read_bytes())
            os.replace(other, self.claude_file)

        def rewrite() -> None:
            with self.claude_file.open("r+b") as handle:
                handle.seek(2)
                handle.write(b"X")

        for label, mutate, expected in (("truncated", truncate, "shrank"), ("replaced", replace, "rotated or replaced"), ("rewritten in place", rewrite, "modified in place")):
            with self.subTest(label):
                write_jsonl(self.claude_file, records)
                os.utime(self.claude_file, ns=(1_000_000_000, 1_000_000_000))  # an old mtime: the rewrite below is a visible change at any clock granularity
                with during_read(self.claude_file, mutate):
                    result = self.claude()
                self.assertEqual(result.status, nr.UNCERTAIN, label)
                self.assertIn(expected, result.reason)
                self.assertIsNone(result.block)

    def test_growth_and_truncation_between_delivery_and_read(self) -> None:
        write_jsonl(self.claude_file, [c_user("earlier"), c_final("earlier")])
        cursor = nr.capture_cursor("claude", CLAUDE_SESSION, self.roots["claude"])
        with self.claude_file.open("a") as handle:
            handle.write(json.dumps(c_user(PROMPT)) + "\n")
        self.assertEqual(self.claude(cursor=cursor).status, nr.NONE, "the turn is still running: prompt present, no final yet")
        with self.claude_file.open("a") as handle:
            handle.write(json.dumps(c_final(frame())) + "\n")
        self.assertTrue(self.claude(cursor=cursor).recoverable)
        self.claude_file.write_text(self.claude_file.read_text()[: cursor["offset"] - 10])
        self.assertEqual(self.claude(cursor=cursor).status, nr.UNAVAILABLE)


class BudgetTests(Roots):
    """CI3: one budget covers discovery, metadata, window read and parsing; nothing resets between phases."""

    def setUp(self) -> None:
        super().setUp()
        self._saved = (nr.MAX_DIR_ENTRIES, nr.MAX_ELAPSED_SECONDS)
        self.addCleanup(lambda: (setattr(nr, "MAX_DIR_ENTRIES", self._saved[0]), setattr(nr, "MAX_ELAPSED_SECONDS", self._saved[1])))

    def test_every_visited_entry_counts_and_a_large_directory_refuses_safely(self) -> None:
        write_jsonl(self.codex_file, [x_meta(), x_user(PROMPT), x_msg(frame()), x_complete()])
        for index in range(20):  # 20 unrelated rollouts in the same date directory
            (self.codex_file.parent / f"rollout-2026-10-21T00-00-{index:02d}-aaaaaaaa-aaaa-4aaa-8aaa-{index:012d}.jsonl").write_text("{}\n")
        nr.MAX_DIR_ENTRIES = 1
        refused = self.codex()
        self.assertEqual(refused.status, nr.UNCERTAIN)
        self.assertIn("directory entries", refused.reason)
        self.assertIsNone(nr.capture_cursor("codex", CODEX_SESSION, self.roots["codex"]), "cursor capture honours the same bound and simply records no cursor")
        nr.MAX_DIR_ENTRIES = 4000
        self.assertTrue(self.codex().recoverable)
        write_jsonl(self.claude_file, [c_user(PROMPT), c_final(frame())])
        for index in range(30):
            (self.roots["claude"] / f"-other-{index}").mkdir()
        nr.MAX_DIR_ENTRIES = 5
        self.assertEqual(self.claude().status, nr.UNCERTAIN)

    def test_file_entries_inside_the_date_directory_count_even_when_the_directories_themselves_fit(self) -> None:
        write_jsonl(self.codex_file, [x_meta(), x_user(PROMPT), x_msg(frame()), x_complete()])
        for index in range(20):
            (self.codex_file.parent / f"rollout-2026-10-21T00-00-{index:02d}-cccccccc-cccc-4ccc-8ccc-{index:012d}.jsonl").write_text("{}\n")
        nr.MAX_DIR_ENTRIES = 3  # exactly year + month + day: the 21 files below must be what exceeds the budget
        refused = self.codex()
        self.assertEqual(refused.status, nr.UNCERTAIN)
        self.assertIn("directory entries", refused.reason)
        nr.MAX_DIR_ENTRIES = 3 + 21
        self.assertTrue(self.codex().recoverable)

    def test_a_late_match_is_found_at_the_limit_and_refused_one_entry_beyond(self) -> None:
        write_jsonl(self.codex_file, [x_meta(), x_user(PROMPT), x_msg(frame()), x_complete()])
        counted = {"n": 0}
        original = nr._Budget.tick

        def counting(self, count=1):
            counted["n"] += count
            return original(self, count)

        nr._Budget.tick = counting
        self.addCleanup(lambda: setattr(nr, "_Budget", nr._Budget) or setattr(nr._Budget, "tick", original))
        self.assertTrue(self.codex().recoverable)
        needed = counted["n"]
        nr.MAX_DIR_ENTRIES = needed
        self.assertTrue(self.codex().recoverable, "the match is the last entry the budget allows")
        nr.MAX_DIR_ENTRIES = needed - 1
        self.assertEqual(self.codex().status, nr.UNCERTAIN, "one entry less and the lookup refuses before matching")

    def test_the_elapsed_deadline_starts_before_discovery_and_is_never_reset_between_phases(self) -> None:
        write_jsonl(self.claude_file, [c_user(PROMPT), c_final(frame())])
        ticks = {"n": 0}

        def slow_clock() -> float:
            ticks["n"] += 1
            return ticks["n"] * 0.5

        nr.MAX_ELAPSED_SECONDS = 1000.0
        self.assertTrue(self.claude(clock=slow_clock).recoverable)
        calls = ticks["n"]
        self.assertGreater(calls, 4, "discovery, window read and parsing all consult the same clock")
        # the same transcript with a deadline that fits discovery alone but not discovery + read + parse
        ticks["n"] = 0
        nr.MAX_ELAPSED_SECONDS = 0.5 * (calls - 2)
        slow = self.claude(clock=slow_clock)
        self.assertEqual(slow.status, nr.UNCERTAIN)
        self.assertIn("seconds", slow.reason)

    def test_the_cursor_carries_a_validated_relative_path_so_recovery_needs_no_scan(self) -> None:
        write_jsonl(self.codex_file, [x_meta(), x_user("old"), x_msg("old", turn="t-0"), x_complete("t-0")])
        cursor = nr.capture_cursor("codex", CODEX_SESSION, self.roots["codex"])
        self.assertEqual(cursor["relpath"], f"2026/10/21/{self.codex_file.name}")
        with self.codex_file.open("a") as handle:
            for record in (x_user(PROMPT), x_msg(frame()), x_complete()):
                handle.write(json.dumps(record) + "\n")
        for index in range(25):
            (self.codex_file.parent / f"rollout-2026-10-21T00-00-{index:02d}-bbbbbbbb-bbbb-4bbb-8bbb-{index:012d}.jsonl").write_text("{}\n")
        nr.MAX_DIR_ENTRIES = 0  # any directory scan would refuse
        self.assertTrue(self.codex(cursor=cursor).recoverable)
        for bad in ("../../etc/passwd", "/abs/path", "2026/10/21/rollout-x-other.jsonl", 7, None, "2026/10/21/../21/" + self.codex_file.name):
            with self.subTest(relpath=str(bad)):
                tampered = {**cursor, "relpath": bad}
                self.assertEqual(self.codex(cursor=tampered).status, nr.UNCERTAIN, "a tampered path is ignored and the bounded lookup (now refused by the budget) decides")


class ConsumerAdversaryTests(NativeFlow):
    """CI1/CI2 through the real consumers (monitor, explicit retry): every unattributable or incomplete transcript leaves no gate, no route, no prompt, no key."""

    def appender(self, path: Path, lines):
        """A prompt-time hook appending raw lines (strings, kept verbatim) computed from the delivered prompt's run/turn."""
        def before(fake, name, text):
            run, turn = FakeHerdr.ids(text)
            frame_text = FakeHerdr.block_v2(run, turn, "brief", "human", "generic_question", str(self.question_payload()), prefix="")
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a") as handle:
                handle.write("".join(lines(text, run, turn, frame_text)))
        return before

    def assert_refused_everywhere(self, status: str) -> None:
        state = self.state()
        self.assertEqual((state["supervisor_state"], state["wait_user_requires_action"]), ("WAIT_USER", True))
        self.assertEqual(state["missing_result"]["native"]["status"], status)
        self.assertIn(f"Native transcript recovery: {status}", state["wait_user_reason"])
        self.assertNotIn("native_recoveries", state)
        self.assertNotEqual(self.pending().get("gate_type"), "generic_question")
        self.assert_nothing_sent_beyond_the_turn()

    def test_claude_records_without_the_owned_session_id_are_refused_through_monitor_and_retry(self) -> None:
        def lines(text, run, turn, frame_text):
            records = [c_user(text), c_final("Result\n" + frame_text)]
            return [json.dumps({k: v for k, v in r.items() if k != "sessionId"}) + "\n" for r in records]

        self.claude_flow({"output": "no frame", "before": self.appender(self.claude_path, lines)})
        self.assert_refused_everywhere(nr.IDENTITY)
        state = self.state()
        result = self.sup.retry_routing_result(state, run_id=state["run_id"], turn_id=state["missing_result"]["turn_id"], actor="test")
        self.assertFalse(result["ok"])
        self.assertIn("Native transcript recovery: identity", result["message"])
        self.assertNotEqual(self.pending().get("gate_type"), "generic_question")
        self.assert_nothing_sent_beyond_the_turn()

    def test_a_codex_rollout_whose_metadata_names_another_or_a_child_session_is_refused(self) -> None:
        for label, meta in {"another session id": x_meta("33333333-3333-4333-8333-333333333333", session_id="33333333-3333-4333-8333-333333333333"),
                            "subagent source": x_meta(source={"subagent": {"other": "guardian"}}, parent_thread_id="p")}.items():
            with self.subTest(label):
                self.reset_fixture()
                self.write_plan()
                write_jsonl(self.codex_path, [meta, x_user("earlier"), x_msg("earlier", turn="t-0"), x_complete("t-0")])

                def lines(text, run, turn, frame_text):
                    return [json.dumps(r) + "\n" for r in (x_user(text), x_msg("Plan\n" + frame_text, turn="t-1"), x_complete("t-1"))]

                self.herdr.responses = [{"output": "no frame", "before": self.appender(self.codex_path, lines)}]
                self.sup.run_new("Add the widget", "codex", workflow_policy="gated_v2")
                state = self.state()
                self.assertEqual((state["supervisor_state"], state["missing_result"]["native"]["status"]), ("WAIT_USER", nr.IDENTITY), label)
                self.assertNotIn("native_recoveries", state)
                self.assertFalse(state.get("pending_gate"))
                self.assertEqual((len(self.herdr.prompts), self.herdr.sent_keys), (1, []))

    def test_incomplete_tail_is_uncertain_then_the_later_flush_recovers_through_retry_without_any_prompt(self) -> None:
        def lines(text, run, turn, frame_text):
            return [json.dumps(c_user(text)) + "\n", json.dumps(c_final("Result\n" + frame_text))]  # the final record is complete JSON but lacks its newline

        self.claude_flow({"output": "no frame", "before": self.appender(self.claude_path, lines)})
        self.assert_refused_everywhere(nr.UNCERTAIN)
        self.assertIn("unterminated trailing record", self.state()["missing_result"]["native"]["reason"])
        with self.claude_path.open("a") as handle:
            handle.write("\n")  # the provider's later successful flush
        state = self.state()
        result = self.sup.retry_routing_result(state, run_id=state["run_id"], turn_id=state["missing_result"]["turn_id"], actor="test")
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.pending().get("gate_type"), "generic_question")
        self.assert_nothing_sent_beyond_the_turn()

    def test_a_malformed_record_after_the_final_stops_recovery_in_the_monitor(self) -> None:
        def lines(text, run, turn, frame_text):
            return [json.dumps(c_user(text)) + "\n", json.dumps(c_final("Result\n" + frame_text)) + "\n", "{not json\n"]

        self.claude_flow({"output": "no frame", "before": self.appender(self.claude_path, lines)})
        self.assert_refused_everywhere(nr.UNCERTAIN)
        self.assertIn("malformed record", self.state()["missing_result"]["native"]["reason"])

    def test_an_unsupported_final_shape_after_the_valid_frame_is_uncertain_through_monitor_and_retry(self) -> None:
        def lines(text, run, turn, frame_text):
            unreadable = c_final("", blocks=[{"type": "text", "text": 17}])
            return [json.dumps(c_user(text)) + "\n", json.dumps(c_final("Result\n" + frame_text)) + "\n", json.dumps(unreadable) + "\n"]

        self.claude_flow({"output": "no frame", "before": self.appender(self.claude_path, lines)})
        self.assert_refused_everywhere(nr.UNCERTAIN)
        self.assertIn("unsupported content", self.state()["missing_result"]["native"]["reason"])
        state = self.state()
        result = self.sup.retry_routing_result(state, run_id=state["run_id"], turn_id=state["missing_result"]["turn_id"], actor="test")
        self.assertFalse(result["ok"])
        self.assertIn("Native transcript recovery: uncertain", result["message"])
        self.assertNotEqual(self.pending().get("gate_type"), "generic_question")
        self.assert_nothing_sent_beyond_the_turn()

    def test_a_codex_payload_of_the_wrong_container_type_after_the_valid_frame_is_uncertain_in_the_monitor(self) -> None:
        self.write_plan()
        write_jsonl(self.codex_path, [x_meta(), x_user("earlier"), x_msg("earlier", turn="t-0"), x_complete("t-0")])

        def lines(text, run, turn, frame_text):
            return [json.dumps(r) + "\n" for r in (x_user(text), x_msg("Plan\n" + frame_text, turn="t-1"), x_complete("t-1"), {"type": "response_item", "payload": ["message"]})]

        self.herdr.responses = [{"output": "no frame", "before": self.appender(self.codex_path, lines)}]
        self.sup.run_new("Add the widget", "codex", workflow_policy="gated_v2")
        state = self.state()
        self.assertEqual((state["supervisor_state"], state["missing_result"]["native"]["status"]), ("WAIT_USER", nr.UNCERTAIN))
        self.assertNotIn("native_recoveries", state)
        self.assertFalse(state.get("pending_gate"))
        self.assertEqual((len(self.herdr.prompts), self.herdr.sent_keys), (1, []))

    def test_a_competing_final_appended_during_the_native_read_is_uncertain_then_a_retry_on_the_settled_file_reports_the_conflict(self) -> None:
        def competing_final() -> None:
            text = self.claude_path.read_text()
            run, turn = re.search(r"HERDR_RUN=([0-9a-f-]{36})", text).group(1), re.search(r"HERDR_TURN=([0-9a-f-]{36})", text).group(1)
            other = FakeHerdr.block_v2(run, turn, "brief", "claude", "none", "-", prefix="")
            append_text(self.claude_path, json.dumps(c_final("Result\n" + other)) + "\n")()

        def lines(text, run, turn, frame_text):
            return [json.dumps(c_user(text)) + "\n", json.dumps(c_final("Result\n" + frame_text)) + "\n"]

        write_jsonl(self.claude_path, [c_user("earlier conversation"), c_final("earlier reply")])
        with during_read(self.claude_path, competing_final):
            self.claude_flow({"output": "no frame", "before": self.appender(self.claude_path, lines)})
        self.assert_refused_everywhere(nr.UNCERTAIN)
        self.assertIn("grew from", self.state()["missing_result"]["native"]["reason"])
        state = self.state()
        result = self.sup.retry_routing_result(state, run_id=state["run_id"], turn_id=state["missing_result"]["turn_id"], actor="test")
        self.assertFalse(result["ok"])
        self.assertIn("Native transcript recovery: conflict", result["message"])
        self.assertNotEqual(self.pending().get("gate_type"), "generic_question")
        self.assertNotIn("native_recoveries", self.state())
        self.assert_nothing_sent_beyond_the_turn()

    def test_native_lookup_budget_exhaustion_is_an_actionable_missing_result_in_the_monitor(self) -> None:
        saved = nr.MAX_DIR_ENTRIES
        self.addCleanup(lambda: setattr(nr, "MAX_DIR_ENTRIES", saved))
        for index in range(30):
            (self.claude_path.parent.parent / f"-other-{index}").mkdir(parents=True, exist_ok=True)

        def lines(text, run, turn, frame_text):
            return [json.dumps(c_user(text)) + "\n", json.dumps(c_final("Result\n" + frame_text)) + "\n"]

        self.write_plan()
        write_jsonl(self.claude_path, [c_user("earlier conversation"), c_final("earlier reply")])
        self.start_gated([{"v2": ("plan", "human", "plan_approval", str(self.plan_payload()))}])
        self.approve_pending()
        nr.MAX_DIR_ENTRIES = 5  # the cursor could not be captured either: no relpath, and the bounded lookup refuses
        self.resume_with([{"v2": ("brief", "claude")}, {"output": "no frame", "before": self.appender(self.claude_path, lines)}])
        self.assertIsNone(self.state()["delivery"].get("native_cursor"), "no cursor was recorded under an exhausted budget")
        self.assert_refused_everywhere(nr.UNCERTAIN)


class ContentionAndRestartTests(NativeFlow):
    """Native duplicate recovery under simultaneous consumers and across a restart (the sequential helper alone does not prove atomicity)."""

    def to_flushable_missing_result(self) -> dict:
        def before(fake, name, text):
            run, turn = FakeHerdr.ids(text)
            frame_text = FakeHerdr.block_v2(run, turn, "brief", "human", "generic_question", str(self.question_payload()), prefix="")
            with self.claude_path.open("a") as handle:
                handle.write(json.dumps(c_user(text)) + "\n" + json.dumps(c_final("Result\n" + frame_text)))  # no trailing newline yet

        self.claude_flow({"output": "no frame", "before": before})
        self.assertEqual(self.state()["missing_result"]["native"]["status"], nr.UNCERTAIN)
        with self.claude_path.open("a") as handle:
            handle.write("\n")
        return self.state()

    def test_two_simultaneous_retries_recover_exactly_once_under_the_real_state_lock(self) -> None:
        state = self.to_flushable_missing_result()
        run_id, turn_id = state["run_id"], state["missing_result"]["turn_id"]
        before_prompts = len(self.herdr.prompts)
        children = []
        for index in range(2):
            read_end, write_end = os.pipe()
            pid = os.fork()
            if pid == 0:  # child: its own controller over the same paths, as a second control process would be
                code = 0
                try:
                    os.close(read_end)
                    sup = self.make_supervisor()
                    with sup.store.transaction():
                        try:
                            outcome = sup.retry_routing_result(sup.store.read_state(), run_id=run_id, turn_id=turn_id, actor=f"child-{index}")
                        except hs_error() as error:
                            outcome = {"ok": False, "error": str(error)}
                    os.write(write_end, json.dumps(outcome).encode())
                except BaseException:  # noqa: BLE001 - a crashed child must still exit without running the parent's test machinery
                    code = 1
                finally:
                    os._exit(code)
            os.close(write_end)
            children.append((pid, read_end))
        outcomes = []
        for pid, read_end in children:
            data = b""
            while chunk := os.read(read_end, 65536):
                data += chunk
            os.close(read_end)
            _pid, status = os.waitpid(pid, 0)
            self.assertEqual(os.waitstatus_to_exitcode(status), 0)
            outcomes.append(json.loads(data))
        self.assertEqual(sorted(bool(o["ok"]) for o in outcomes), [False, True], outcomes)
        final = self.state()
        self.assertEqual(len(final["native_recoveries"]), 1)
        self.assertEqual(final["pending_gate"]["gate_type"], "generic_question")
        self.assertEqual(final["gate_sequence"], 2, "the approval gate and exactly one recovered question gate")
        self.assertEqual(len(self.herdr.prompts), before_prompts, "no child sent anything")

    def test_a_restart_after_recovery_neither_routes_nor_sends_again(self) -> None:
        state = self.to_flushable_missing_result()
        self.sup.retry_routing_result(state, run_id=state["run_id"], turn_id=state["missing_result"]["turn_id"], actor="test")
        recovered = self.state()
        prompts, gates = len(self.herdr.prompts), recovered["gate_sequence"]
        for _ in range(2):
            self.make_supervisor().resume()
        after = self.state()
        self.assertEqual((len(self.herdr.prompts), after["gate_sequence"], len(after["native_recoveries"])), (prompts, gates, 1))
        self.assertEqual(after["pending_gate"]["gate_id"], recovered["pending_gate"]["gate_id"])
        self.assertEqual(self.herdr.sent_keys, [])


if __name__ == "__main__":
    unittest.main()
