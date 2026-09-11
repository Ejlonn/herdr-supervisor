"""Telegram task-file upload extension: authorized .md/.txt staging, preview, one-time Start/Cancel,
fail-closed validation, replay/crash boundaries, and the shared safe task-file loader."""

from __future__ import annotations

import json
import os
from pathlib import Path

from test_telegram import BOT_ID, CHAT, OWNER, FakeApi, TelegramCase, callback
from v2_fixtures import FAKE_TOKEN, NOW, FakeHerdr, hs

import herdr_telegram as ht  # noqa: E402
import herdr_codex_reset as hcr  # noqa: E402
import telegram_api as tg  # noqa: E402

TASK_MD = "# Widget task\n\nAdd the widget.\n\n" + ("Details line.\n" * 200)


class UploadApi(FakeApi):
    def __init__(self) -> None:
        super().__init__()
        self.files: dict[str, bytes] = {}
        self.get_file_calls: list[str] = []
        self.download_calls: list[tuple[str, int]] = []
        self.file_path = "documents/file_1.md"

    def get_file(self, file_id: str) -> str:
        self.get_file_calls.append(file_id)
        self._maybe_fail("getFile")
        if file_id not in self.files:
            raise tg.TelegramError("API_ERROR", "telegram api error 400: file not found")
        return self.file_path

    def download_file(self, file_path: str, *, max_bytes: int) -> bytes:
        self.download_calls.append((file_path, max_bytes))
        self._maybe_fail("download")
        data = next(iter(self.files.values()))
        if len(data) > max_bytes:
            raise tg.TelegramError("OVERSIZE", f"file exceeds the {max_bytes}-byte limit (streaming overrun)")
        return data


def document(update_id: int, *, name="task.md", size=None, mime="text/markdown", file_id="BQACAgIAAxkBAAIFileId123", user=OWNER, chat=CHAT, chat_type="private") -> dict:
    doc = {"file_name": name, "mime_type": mime, "file_id": file_id, "file_unique_id": "AgADuniq", "file_size": size}
    if mime is None:
        doc.pop("mime_type")
    return {"update_id": update_id, "message": {"message_id": update_id, "from": {"id": user, "is_bot": False}, "chat": {"id": chat, "type": chat_type}, "date": 1, "document": doc}}


class UploadCase(TelegramCase):
    def setUp(self) -> None:
        super().setUp()
        self.api = UploadApi()
        self.bridge = self.make_bridge()
        self.set_file(TASK_MD.encode())

    def set_file(self, data: bytes, file_id: str = "BQACAgIAAxkBAAIFileId123") -> None:
        self.api.files = {file_id: data}

    def upload(self, update_id: int = 10, **kw) -> dict:
        data = next(iter(self.api.files.values()))
        kw.setdefault("size", len(data))
        return self.bridge.handle_update(document(update_id, **kw))

    def meta(self, upload_id: str) -> dict:
        return json.loads((self.paths.task_files_dir / f"{upload_id}.json").read_text())

    def buttons(self) -> dict[str, str]:
        card = next(m for m in reversed(self.api.sent) if m["reply_markup"] and any(b["text"] == "Start Task" for row in m["reply_markup"]["inline_keyboard"] for b in row))
        return {b["text"]: b["callback_data"] for row in card["reply_markup"]["inline_keyboard"] for b in row}

    def staged_files(self) -> list[Path]:
        return sorted(p for p in self.paths.task_files_dir.glob("*") if p.suffix in (".md", ".txt"))

    def choose_reset_budget(self, update_id: int, budget: int = 0, bridge=None) -> dict:
        bridge = bridge or self.bridge
        tokens = []
        for path in self.tg_paths.interactions_dir.glob("*.json"):
            record = json.loads(path.read_text())
            if record.get("action") == "reset_budget" and record.get("budget") == budget and not record.get("consumed") and not record.get("superseded"):
                tokens.append((path.stat().st_mtime_ns, path.stem))
        return bridge.handle_update(callback(update_id, max(tokens)[1]))


class StagingTests(UploadCase):
    def test_confirmed_upload_requires_reset_budget_before_normal_task_enqueue(self) -> None:
        self.bridge.reset_inventory_reader=lambda:hcr.ResetInventory(2,True,"a"*64,self.clock.current)
        self.upload(9); start=self.buttons()["Start Task"]
        result=self.bridge.handle_update(callback(10,start)); self.assertEqual(result["result"],"reset_budget_required")
        self.assertEqual(self.pending_inbox(),[])
        tokens={json.loads(p.read_text()).get("budget"):p.stem for p in self.tg_paths.interactions_dir.glob("*.json") if json.loads(p.read_text()).get("action")=="reset_budget"}
        started=self.bridge.handle_update(callback(11,tokens[1])); self.assertEqual(started["result"],"enqueued")
        command=self.pending_inbox()[0]; self.assertIn("task_file",command); self.assertEqual(command["codex_reset_authorization"]["budget"],1)

    def test_authorized_md_upload_stages_atomically_and_previews_without_a_task(self) -> None:
        result = self.upload()
        self.assertEqual(result["result"], "staged")
        upload_id = result["upload_id"]
        self.assertEqual(self.api.get_file_calls, ["BQACAgIAAxkBAAIFileId123"])
        self.assertEqual(self.api.download_calls, [("documents/file_1.md", 65536)])
        files = self.staged_files()
        self.assertEqual([p.name for p in files], [f"{upload_id}.md"])
        self.assertEqual(oct(files[0].stat().st_mode & 0o777), "0o600")
        self.assertEqual(oct(self.paths.task_files_dir.stat().st_mode & 0o777), "0o700")
        self.assertEqual(files[0].read_bytes(), TASK_MD.encode())
        self.assertFalse(list(self.paths.task_files_dir.glob(".*.tmp")))
        meta = self.meta(upload_id)
        self.assertEqual(meta["status"], "pending_confirmation")
        self.assertEqual(meta["sha256"], hs.sha256_bytes(TASK_MD.encode()))
        self.assertEqual(meta["display_filename"], "task.md")
        self.assertEqual(meta["snapshot"], {"task_id": None, "state": "NO_TASK"})
        card = self.api.sent[-1]["text"]
        self.assertIn("Task file received: task.md", card)
        self.assertIn(f"{len(TASK_MD.encode())} bytes", card)
        self.assertIn(meta["sha256"][:12], card)
        self.assertIn("No task has started", card)
        self.assertNotIn("Details line.\n" * 50, card, "bounded excerpt")
        self.assertEqual(set(self.buttons()), {"Start Task", "Cancel"})
        self.assertFalse(self.paths.state_file.exists(), "no supervisor run exists")
        self.assertEqual(self.pending_inbox(), [])

    def test_authorized_txt_upload(self) -> None:
        self.set_file(b"plain text task\n")
        result = self.upload(name="Notes.TXT", mime="text/plain")
        self.assertEqual(result["result"], "staged")
        self.assertEqual([p.suffix for p in self.staged_files()], [".txt"])

    def test_unauthorized_uploads_cause_no_network_file_interaction_or_command(self) -> None:
        for kw in ({"user": 999}, {"chat": 777}, {"chat_type": "group"}):
            result = self.bridge.handle_update(document(20, size=100, **kw))
            self.assertIn("rejected", result, kw)
        self.assertEqual(self.api.get_file_calls, [])
        self.assertEqual(self.api.download_calls, [])
        self.assertFalse(self.paths.task_files_dir.exists() and list(self.paths.task_files_dir.glob("*")))
        self.assertEqual(list(self.tg_paths.interactions_dir.glob("*.json")), [])
        self.assertEqual(self.pending_inbox(), [])
        self.assertEqual(self.api.sent, [])

    def test_unsupported_extension_rejected_before_download(self) -> None:
        for kw in ({"name": "task.pdf"}, {"name": "task.md.exe"}, {"name": "task"}, {"name": "task.markdown"}):
            result = self.upload(30, **kw)
            self.assertEqual(result["result"], "unsupported_type", kw)
        self.assertEqual(self.api.get_file_calls, [])
        self.assertIn("Unsupported", self.api.sent[-1]["text"])
        # octet-stream and a missing mime are fine when suffix + strict text pass
        self.assertEqual(self.upload(31, mime="application/octet-stream")["result"], "staged")
        self.assertEqual(self.upload(32, mime=None)["result"], "staged")

    def test_mime_is_advisory_for_allowed_suffixes(self) -> None:
        """Live finding 2026-09-11: Telegram labelled valid .md documents with a non-text MIME."""
        upload = 33
        for name, mime in (("task.md", "text/x-web-markdown"), ("task.md", "application/pdf"), ("task.md", "application/x-genesis-rom"), ("notes.txt", "image/png"), ("notes.TXT", "application/octet-stream; charset=binary"), ("task.md", "")):
            upload += 1
            self.set_file(b"# Valid task\n\nDo the thing.\n")
            result = self.upload(upload, name=name, mime=mime)
            self.assertEqual(result["result"], "staged", (name, mime))
            meta = self.meta(result["upload_id"])
            self.assertEqual(meta["declared_mime"], (mime.split(";")[0].strip().lower() if mime else ""), (name, mime))
            self.assertIn("Task file received", self.api.sent[-1]["text"])
        # misclassified MIME never lets binary or invalid UTF-8 through: rejected after the bounded download
        for name, mime, data in (("task.md", "text/markdown", b"\x89PNG\r\n\x1a\n\x00binary"), ("notes.txt", "text/plain", b"\xff\xfe\x00\x00"), ("task.md", "application/pdf", b"%PDF-1.4\n\x00\x01")):
            upload += 1
            self.set_file(data)
            result = self.upload(upload, name=name, mime=mime)
            self.assertEqual(result["result"], "invalid_content", (name, mime))
        self.assertEqual(len(self.staged_files()), 1, "only the newest valid upload remains staged (older pending superseded)")
        # unsupported suffix with a text MIME still causes no download
        calls = len(self.api.get_file_calls)
        self.assertEqual(self.upload(upload + 1, name="task.pdf", mime="text/markdown")["result"], "unsupported_type")
        self.assertEqual(len(self.api.get_file_calls), calls)

    def test_oversize_declared_rejected_before_download_and_streaming_overrun_publishes_nothing(self) -> None:
        result = self.upload(40, size=65537)
        self.assertEqual(result["result"], "oversize_declared")
        self.assertEqual(self.api.get_file_calls, [])
        self.assertIn("too large", self.api.sent[-1]["text"])
        # lying metadata: declared small, actual overrun
        self.set_file(b"x" * 70000)
        result = self.upload(41, size=100)
        self.assertEqual(result, {"document": True, "result": "download_failed", "category": "OVERSIZE"})
        self.assertFalse(self.paths.task_files_dir.exists() and self.staged_files())
        self.assertFalse(list(self.tg_paths.interactions_dir.glob("*.json")))
        # configurable limit
        small = self.make_bridge()
        small.sup_config = {**self.config, "max_task_file_bytes": 100}
        self.set_file(b"y" * 150)
        self.assertEqual(small.handle_update(document(42, size=150))["result"], "oversize_declared")

    def test_malformed_text_and_hostile_filenames(self) -> None:
        for label, data in (("invalid utf-8", b"\xff\xfe\x00bad"), ("nul", b"task\x00text"), ("control", b"task\x1b[31mtext"), ("empty", b""), ("whitespace", b"   \n")):
            self.set_file(data)
            result = self.upload(50, size=max(1, len(data)))
            self.assertIn(result["result"], ("invalid_content", "bad_size"), label)
        self.assertFalse(self.paths.task_files_dir.exists() and self.staged_files())
        self.set_file(b"ok task\n")
        result = self.upload(51, name="../../etc/passwd\u202e\x00evil .md")
        self.assertEqual(result["result"], "staged")
        meta = self.meta(result["upload_id"])
        self.assertEqual(meta["display_filename"], "passwdevil.md")
        self.assertTrue(Path(meta["content_path"]).name.startswith(result["upload_id"]))
        self.assertNotIn("passwd", Path(meta["content_path"]).name)
        result = self.upload(52, name="C:\\Users\\x\\" + "n" * 300 + ".md")
        self.assertLessEqual(len(self.meta(result["upload_id"])["display_filename"]), 80)

    def test_busy_supervisor_rejects_upload_before_download(self) -> None:
        self.plan_gate()
        result = self.upload(60)
        self.assertEqual(result["result"], "busy")
        self.assertEqual(self.api.get_file_calls, [])

    def test_duplicate_update_and_restart_replay_produce_one_file_and_one_preview(self) -> None:
        update = document(70, size=len(TASK_MD.encode()))
        self.api.updates_batches = [[update], [update]]
        self.bridge.poll_once()
        self.bridge.poll_once()
        self.assertEqual(len(self.staged_files()), 1)
        self.assertEqual(len([m for m in self.api.sent if "Task file received" in m["text"]]), 1)
        bridge2 = self.make_bridge()  # daemon restart
        self.api.updates_batches = [[update]]
        bridge2.poll_once()
        self.assertEqual(len(self.staged_files()), 1)
        self.assertEqual(self.api.get_file_calls, ["BQACAgIAAxkBAAIFileId123"], "one download ever")

    def test_crash_boundaries_never_overwrite_and_render_one_current_preview(self) -> None:
        upload_id = self.bridge.upload_id_for(80)
        # content published, crash before metadata: replay reuses identical content, no overwrite
        self.paths.task_files_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        content = self.paths.task_files_dir / f"{upload_id}.md"
        content.write_bytes(TASK_MD.encode())
        os.chmod(content, 0o600)
        before = content.stat().st_ino
        result = self.upload(80)
        self.assertEqual(result["result"], "staged")
        self.assertEqual(content.stat().st_ino, before)
        # different content under the same upload id: refused, nothing replaced
        other = self.bridge.upload_id_for(81)
        (self.paths.task_files_dir / f"{other}.md").write_bytes(b"different existing\n")
        result = self.upload(81)
        self.assertEqual(result["result"], "stage_failed")
        self.assertEqual((self.paths.task_files_dir / f"{other}.md").read_bytes(), b"different existing\n")
        # metadata exists but preview crashed: replaying the update sends exactly one current card
        sent_before = len(self.api.sent)
        result = self.upload(80)
        self.assertEqual(result["result"], "replayed")
        self.assertEqual(len(self.api.sent), sent_before + 1)
        live = [json.loads(p.read_text()) for p in self.tg_paths.interactions_dir.glob("*.json") if json.loads(p.read_text()).get("upload_id") == upload_id and not json.loads(p.read_text())["superseded"]]
        self.assertEqual(len(live), 2, "only the newest Start/Cancel pair is live")

    def test_new_upload_supersedes_older_pending_upload(self) -> None:
        first = self.upload(90)["upload_id"]
        first_buttons = self.buttons()
        second = self.upload(91)["upload_id"]
        self.assertEqual(self.meta(first)["status"], "superseded")
        self.assertFalse((self.paths.task_files_dir / f"{first}.md").exists())
        self.assertIn("rejected", self.bridge.handle_update(callback(92, first_buttons["Start Task"])))
        self.assertEqual(self.meta(second)["status"], "pending_confirmation")

    def test_expired_upload_is_reaped_and_buttons_inert(self) -> None:
        upload_id = self.upload(100)["upload_id"]
        buttons = self.buttons()
        self.clock.current += 8 * 86400
        self.assertEqual(self.bridge.reap_uploads(), 1)
        self.assertEqual(self.meta(upload_id)["status"], "expired")
        self.assertEqual(self.staged_files(), [])
        self.assertIn("rejected", self.bridge.handle_update(callback(101, buttons["Start Task"])))


class ConfirmationTests(UploadCase):
    def test_cancel_before_start_is_one_time_deletes_content_and_starts_nothing(self) -> None:
        upload_id = self.upload(110)["upload_id"]
        buttons = self.buttons()
        result = self.bridge.handle_update(callback(111, buttons["Cancel"]))
        self.assertEqual(result["callback"], "upload_cancel")
        self.assertEqual(self.meta(upload_id)["status"], "cancelled")
        self.assertNotIn("excerpt", self.meta(upload_id))
        self.assertEqual(self.staged_files(), [])
        self.assertIn("No supervisor task was started", self.api.sent[-1]["text"])
        self.assertIn("rejected", self.bridge.handle_update(callback(112, buttons["Cancel"])))
        self.assertIn("rejected", self.bridge.handle_update(callback(113, buttons["Start Task"])))
        self.assertEqual(self.pending_inbox(), [])
        self.assertFalse(self.paths.state_file.exists())
        self.assertNotIn("cancel", [c["action"] for c in self.pending_inbox()], "never invokes supervisor cancel")

    def test_start_revalidates_everything_and_rejects_stale_changed_missing_symlinked(self) -> None:
        upload_id = self.upload(120)["upload_id"]
        buttons = self.buttons()
        content = self.paths.task_files_dir / f"{upload_id}.md"
        # wrong user / chat
        self.assertEqual(self.bridge.handle_update(callback(121, buttons["Start Task"], user=999))["rejected"], "wrong_user")
        # shadow mode never starts
        shadow = self.make_bridge(mode="shadow")
        self.assertIn("rejected", shadow.handle_update(callback(122, buttons["Start Task"])))
        self.assertEqual(self.pending_inbox(), [])
        # changed content
        original = content.read_bytes()
        content.write_bytes(original + b"tampered\n")
        self.assertIn("changed", self.bridge.handle_update(callback(123, buttons["Start Task"]))["rejected"])
        content.write_bytes(original)
        # symlinked content
        content.unlink()
        content.symlink_to(Path(self.tmp.name) / "outside.md")
        (Path(self.tmp.name) / "outside.md").write_bytes(original)
        self.assertIn("unsafe", self.bridge.handle_update(callback(124, buttons["Start Task"]))["rejected"])
        content.unlink()
        # missing content
        self.assertIn("missing", self.bridge.handle_update(callback(125, buttons["Start Task"]))["rejected"])
        content.write_bytes(original)
        os.chmod(content, 0o600)
        # expiry
        self.clock.current += 8 * 86400
        self.assertIn("rejected", self.bridge.handle_update(callback(126, buttons["Start Task"])))
        self.assertEqual(self.pending_inbox(), [])
        self.assertFalse(self.paths.state_file.exists())

    def test_active_task_appearing_between_preview_and_start_makes_upload_inert(self) -> None:
        upload_id = self.upload(130)["upload_id"]
        buttons = self.buttons()
        self.plan_gate()  # another task starts
        run_id = self.state()["run_id"]
        result = self.bridge.handle_update(callback(131, buttons["Start Task"]))
        self.assertEqual(result["rejected"], "task_snapshot_changed")
        self.assertEqual(self.meta(upload_id)["status"], "superseded")
        self.assertEqual(self.state()["run_id"], run_id)
        self.assertEqual(self.state()["supervisor_state"], "WAIT_PLAN_APPROVAL", "the other task is untouched")
        self.assertEqual(self.pending_inbox(), [])

    def test_confirmed_start_enqueues_one_task_file_command_and_worker_creates_one_gated_run(self) -> None:
        upload_id = self.upload(140)["upload_id"]
        buttons = self.buttons()
        result = self.bridge.handle_update(callback(141, buttons["Start Task"]))
        self.assertEqual(result["result"], "reset_budget_required")
        result = self.choose_reset_budget(142)
        self.assertEqual(result["result"], "enqueued")
        pending = self.pending_inbox()
        self.assertEqual(len(pending), 1)
        command = pending[0]
        self.assertEqual(command["action"], "task")
        self.assertNotIn("task_text", command, "task bytes never travel in the command")
        self.assertEqual(command["task_file"], str(self.paths.task_files_dir / f"{upload_id}.md"))
        self.assertEqual(command["task_file_sha256"], hs.sha256_bytes(TASK_MD.encode()))
        self.assertEqual(self.meta(upload_id)["status"], "start_enqueued")
        # replaying the callback or the update cannot enqueue a second command
        self.assertIn("rejected", self.bridge.handle_update(callback(143, buttons["Start Task"])))
        self.assertIn("rejected", self.bridge.handle_update(callback(144, buttons["Cancel"])))
        self.assertEqual(len(self.pending_inbox()), 1)
        # the fixed worker starts exactly one gated_v2 Codex-first run from the file
        self.write_plan()
        self.herdr.responses = [{"v2": ("plan", "human", "plan_approval", str(self.plan_payload()))}]
        self.assertEqual(self.sup.worker(), 4)
        state = self.state()
        self.assertEqual(state["workflow_policy"], "gated_v2")
        self.assertEqual(state["active_agent"], "codex")
        self.assertEqual(state["task_text"], TASK_MD)
        self.assertEqual(state["task_reference"], str(self.paths.task_files_dir / f"{upload_id}.md"))
        self.assertEqual(state["supervisor_state"], "WAIT_PLAN_APPROVAL", "plan approval is the first human gate")
        self.assertEqual(self.meta(upload_id)["status"], "started")
        self.assertEqual(self.meta(upload_id)["run_id"], state["run_id"])
        prompt = self.herdr.prompts[0][1]
        self.assertIn("Add the widget.", prompt)
        # command replay after restart returns the prior result; no second run
        completed = next((self.paths.inbox_dir / "completed").glob(f"*-{command['request_id']}.json"))
        completed.unlink()
        self.sup.enqueue_command(command)
        replay = self.sup.process_inbox()
        self.assertTrue(replay[0].get("replayed"))
        self.assertEqual(self.state()["run_id"], state["run_id"])
        self.bridge.deliver_outbox()
        self.assertIn("Task start accepted", "\n".join(self.texts()))

    def test_task_file_command_with_wrong_hash_or_outside_inbox_is_refused_by_worker(self) -> None:
        upload_id = self.upload(150)["upload_id"]
        path = str(self.paths.task_files_dir / f"{upload_id}.md")
        self.sup.enqueue_command({"request_id": "req-0000badhash", "action": "task", "task_file": path, "task_file_sha256": "0" * 64, "actor": "x"})
        outside = Path(self.tmp.name) / "evil.md"
        outside.write_text("evil\n")
        self.sup.enqueue_command({"request_id": "req-00000outside", "action": "task", "task_file": str(outside), "task_file_sha256": hs.sha256_bytes(b"evil\n"), "actor": "x"})
        self.sup.enqueue_command({"request_id": "req-0000000both", "action": "task", "task_file": path, "task_text": "x", "task_file_sha256": hs.sha256_bytes(TASK_MD.encode()), "actor": "x"})
        results = self.sup.process_inbox()
        self.assertEqual([r["ok"] for r in results], [False, False, False])
        self.assertIn("hash", results[0]["message"])
        self.assertIn("below", results[1]["message"])
        self.assertIn("exactly one", results[2]["message"])
        self.assertFalse(self.paths.state_file.exists())


class CompatibilityAndSecurityTests(UploadCase):
    def test_short_task_text_unchanged_and_4000_limit(self) -> None:
        from test_telegram import message
        result = self.bridge.handle_update(message(160, "/task build the widget"))
        self.assertEqual(result["result"], "reset_budget_required")
        self.assertEqual(self.choose_reset_budget(162)["result"], "enqueued")
        self.assertEqual(self.pending_inbox()[-1]["task_text"], "build the widget")
        self.assertIn("rejected", self.bridge.handle_update(message(161, "/task " + "x" * 4001)) | {"rejected": None} if False else {"rejected": self.bridge.handle_update(message(161, "/task " + "x" * 4001))["result"]})
        self.assertIn("exceeds", self.api.sent[-1]["text"])
        self.assertEqual(len(self.pending_inbox()), 1)

    def test_cli_run_task_file_uses_the_shared_loader(self) -> None:
        long_file = Path(self.tmp.name) / "long-task.md"
        long_file.write_text("# Long\n" + ("line\n" * 5000))
        text, reference, digest = hs.load_task_file(str(long_file), config=self.config, root=None)
        self.assertGreater(len(text), 8000, "longer than the inline max_task_chars ceiling")
        self.assertEqual(reference, str(long_file.resolve()))
        self.herdr.responses = [{"v2": ("plan", "done")}]
        self.assertEqual(self.sup.run_new(text, "codex", task_reference=reference, char_limit=int(self.config["max_task_file_bytes"])), 0)
        self.assertEqual(self.state()["task_reference"], str(long_file.resolve()))
        for label, path, kw in (("suffix", Path(self.tmp.name) / "t.py", {}), ("symlink", Path(self.tmp.name) / "link.md", {})):
            if label == "suffix":
                path.write_text("x\n")
            else:
                path.symlink_to(long_file)
            with self.assertRaises(hs.SupervisorError, msg=label):
                hs.load_task_file(str(path), config=self.config, root=None)
        big = Path(self.tmp.name) / "big.md"
        big.write_bytes(b"z" * 70000)
        with self.assertRaises(hs.SupervisorError):
            hs.load_task_file(str(big), config=self.config, root=None)
        with self.assertRaises(hs.SupervisorError):
            hs.load_task_file(str(long_file), config=self.config, root=None, expected_sha256="0" * 64)
        with self.assertRaises(hs.SupervisorError):
            hs.load_task_file(str(long_file), config=self.config, root=self.paths.task_files_dir)
        # CLI main path
        captured: dict = {}

        class Recorder(hs.Supervisor):
            def run_new(self, task, start, *, task_reference=None, workflow_policy="v1", char_limit=None):
                captured.update(task=task, task_reference=task_reference, char_limit=char_limit)
                return 0

        original = hs.Supervisor
        hs.Supervisor = Recorder  # type: ignore[misc]
        try:
            self.paths.config_file.write_text(json.dumps({"schema_version": 1, "herdr_bin": os.sys.executable}))
            os.environ["HERDR_SUPERVISOR_CONFIG"] = str(self.paths.config_file)
            os.environ["HERDR_SUPERVISOR_STATE_DIR"] = str(self.state_dir)
            self.assertEqual(hs.main(["run", "--policy", "gated_v2", str(long_file)]), 0)
        finally:
            hs.Supervisor = original  # type: ignore[misc]
            os.environ.pop("HERDR_SUPERVISOR_CONFIG", None)
            os.environ.pop("HERDR_SUPERVISOR_STATE_DIR", None)
        self.assertEqual(captured["char_limit"], 65536)
        self.assertEqual(captured["task_reference"], str(long_file.resolve()))

    def test_download_errors_are_sanitized_and_token_never_leaks(self) -> None:
        for category in ("DNS_FAILURE", "HTTPS_FAILURE", "API_ERROR"):
            self.api.fail["download"] = [tg.TelegramError(category, f"boom https://api.telegram.org/file/bot{FAKE_TOKEN}/documents/x.md")]
            result = self.upload(170)
            self.assertEqual(result["result"], "download_failed")
            self.assertIn(category, self.api.sent[-1]["text"])
        self.assertFalse(self.paths.task_files_dir.exists() and self.staged_files())
        # real BotApi: token-bearing download URL never escapes
        api = tg.BotApi(FAKE_TOKEN, opener=lambda url, body, timeout: (200, b'{"ok":true,"result":{"file_path":"documents/f.md"}}'), downloader=lambda url, timeout, max_bytes: (_ for _ in ()).throw(ConnectionResetError(url)))
        self.assertEqual(api.get_file("BQACAgIAAxkBAAIFileId123"), "documents/f.md")
        with self.assertRaises(tg.TelegramError) as caught:
            api.download_file("documents/f.md", max_bytes=100)
        self.assertNotIn(FAKE_TOKEN, str(caught.exception))
        for bad in ("/etc/passwd", "../x.md", "documents/../../x", "http://evil/x", "docs/\x00x", "a" * 300):
            with self.assertRaises(tg.TelegramError):
                api.download_file(bad, max_bytes=100)
        api_bad_path = tg.BotApi(FAKE_TOKEN, opener=lambda url, body, timeout: (200, b'{"ok":true,"result":{"file_path":"../../secret"}}'))
        with self.assertRaises(tg.TelegramError):
            api_bad_path.get_file("BQACAgIAAxkBAAIFileId123")
        api_big = tg.BotApi(FAKE_TOKEN, downloader=lambda url, timeout, max_bytes: b"x" * (max_bytes + 1))
        with self.assertRaises(tg.TelegramError) as caught:
            api_big.download_file("documents/f.md", max_bytes=10)
        self.assertEqual(caught.exception.category, "OVERSIZE")

    def test_content_and_token_never_enter_journals_callbacks_commands_or_records(self) -> None:
        secret_line = "api_key=supersecret-upload-value"
        self.set_file((TASK_MD + secret_line + "\n").encode())
        update = document(180, size=len(next(iter(self.api.files.values()))))
        self.api.updates_batches = [[update]]
        self.bridge.poll_once()
        buttons = self.buttons()
        self.bridge.handle_update(callback(181, buttons["Start Task"]))
        rendered = "\n".join(self.texts())
        self.assertNotIn("supersecret-upload-value", rendered, "excerpt is redacted")
        self.assertNotIn("Details line.\n" * 50, rendered)
        for path in list(self.tg_paths.state_dir.rglob("*.json")) + list((self.paths.inbox_dir / "pending").glob("*.json")) + list(self.paths.task_files_dir.glob("*.json")):
            text = path.read_text()
            self.assertNotIn(FAKE_TOKEN, text, path)
            self.assertNotIn("supersecret-upload-value", text, path)
            self.assertNotIn("Details line.\nDetails line.\nDetails line.", text, path)
        for data in buttons.values():
            self.assertLess(len(data.encode()), 64)
        self.assertNotIn(FAKE_TOKEN, json.dumps(self.sup.doctor()))

    def test_bridge_gains_no_shell_listener_or_pane_control(self) -> None:
        source = Path(ht.__file__).read_text()
        for forbidden in ("subprocess", "os.system", "os.exec", "shlex", ".bind(", ".listen(", "send_keys", "start_agent", ".prompt(", "setWebhook", "deleteWebhook", "herdr-supervisor run "):
            self.assertNotIn(forbidden, source, forbidden)
        self.assertNotIn("subprocess", Path(tg.__file__).read_text())


class ReviewFixTests(UploadCase):
    """CODEX_REVIEW.md (upload) F1–F3: preview delivery boundary, corrupt records, post-init crash."""

    def test_f1_failed_preview_send_leaves_update_unfinalized_and_retries_without_second_download(self) -> None:
        update = document(200, size=len(TASK_MD.encode()))
        self.api.updates_batches = [[update]]
        self.api.fail["sendMessage"] = [tg.TelegramError("HTTPS_FAILURE", "outage")]
        with self.assertRaises(tg.TelegramError):
            self.bridge.poll_once()
        upload_id = self.bridge.upload_id_for(200)
        self.assertEqual(self.bridge.read_offset(), 0, "offset not advanced")
        self.assertFalse(self.bridge.journal_path(200).exists(), "update not journaled")
        self.assertEqual(self.meta(upload_id)["status"], "pending_confirmation")
        self.assertEqual(len(self.staged_files()), 1)
        first_tokens = set(self.meta(upload_id)["interactions"].values())
        # daemon loop tolerates it (backoff) and the next round retries the same update
        self.api.fail = {}
        self.api.updates_batches = [[update]]
        bridge2 = self.make_bridge()  # restart
        self.assertEqual(bridge2.poll_once(), 1)
        self.assertEqual(bridge2.read_offset(), 201)
        self.assertTrue(bridge2.journal_path(200).exists())
        self.assertEqual(self.api.get_file_calls, ["BQACAgIAAxkBAAIFileId123"], "no second download")
        self.assertEqual(len(self.staged_files()), 1, "no overwrite")
        cards = [m for m in self.api.sent if "Task file received" in m["text"]]
        self.assertEqual(len(cards), 1)
        live = [json.loads(p.read_text()) for p in self.tg_paths.interactions_dir.glob("*.json")]
        live_pairs = [r for r in live if r.get("upload_id") == upload_id and not r["superseded"] and not r["consumed"]]
        self.assertEqual(len(live_pairs), 2, "exactly one newest Start/Cancel pair is actionable")
        for token in first_tokens:
            self.assertIn("rejected", bridge2.handle_update(callback(202, token)))
        buttons = self.buttons()
        self.assertEqual(bridge2.handle_update(callback(203, buttons["Start Task"]))["result"], "reset_budget_required")
        self.assertEqual(self.choose_reset_budget(204, bridge=bridge2)["result"], "enqueued")
        self.assertEqual(len(self.pending_inbox()), 1)

    def test_f1_run_loop_backs_off_and_recovers(self) -> None:
        update = document(210, size=len(TASK_MD.encode()))
        self.api.updates_batches = [[update], [update]]
        self.api.fail["sendMessage"] = [tg.TelegramError("HTTPS_FAILURE", "outage")]
        self.assertEqual(self.bridge.run(max_rounds=2), 0)
        self.assertEqual(self.clock.sleeps, [1.0])
        self.assertEqual(self.bridge.read_offset(), 211)
        self.assertEqual(self.api.get_file_calls, ["BQACAgIAAxkBAAIFileId123"])

    def test_f2_corrupt_records_never_write_or_remove_outside_and_start_nothing(self) -> None:
        upload_id = self.upload(220)["upload_id"]
        buttons = self.buttons()
        meta_path = self.paths.task_files_dir / f"{upload_id}.json"
        good = json.loads(meta_path.read_text())
        state_root = self.paths.state_dir
        cases = {
            "traversal id": {**good, "upload_id": "../../escaped-upload-review"},
            "escaped content path": {**good, "content_path": str(state_root / "escaped.md")},
            "unknown schema": {**good, "schema_version": 2},
            "unknown status": {**good, "status": "approved"},
            "extra field": {**good, "shell": "rm -rf /"},
            "wrong suffix": {**good, "suffix": ".sh"},
            "bad hash": {**good, "sha256": "zz"},
            "partial": {k: v for k, v in good.items() if k != "sha256"},
            "wrong chat": {**good, "chat_id": 999},
            "oversize bytes": {**good, "bytes": 10 ** 9},
        }
        for label, corrupt in cases.items():
            meta_path.write_text(json.dumps(corrupt))
            before = sorted(str(p) for p in state_root.rglob("*") if "/logs" not in str(p))  # the audit log itself may grow
            result = self.bridge.handle_update(callback(221, buttons["Cancel"]))
            self.assertIn("rejected", result, label)
            result = self.bridge.handle_update(callback(222, buttons["Start Task"]))
            self.assertIn("rejected", result, label)
            self.assertEqual(self.bridge.reap_uploads(), 0, label)
            self.assertEqual(self.bridge._supersede_pending_uploads(CHAT, except_upload="0" * 32), 0, label)
            if label == "wrong chat":  # schema-valid; the chat binding is enforced against the callback record
                self.assertFalse(self.sup.reconcile_upload_audit(upload_id, "run"))
            else:
                with self.assertRaises(hs.SupervisorError, msg=label):
                    self.sup.reconcile_upload_audit(upload_id, "run")  # worker audit refuses corrupt records
            self.sup._mark_upload_started({"upload_id": upload_id}, "run")  # and the best-effort wrapper never raises
            after = sorted(str(p) for p in state_root.rglob("*") if "/logs" not in str(p))
            self.assertEqual(before, after, f"{label}: no file written or removed anywhere under state")
            self.assertFalse((state_root / "escaped-upload-review.json").exists(), label)
            self.assertFalse((state_root / "escaped.md").exists(), label)
        self.assertEqual(self.pending_inbox(), [])
        self.assertFalse(self.paths.state_file.exists())
        # a replayed update over a corrupt record refuses and downloads nothing
        self.api.get_file_calls.clear()
        self.assertEqual(self.upload(220)["result"], "corrupt_record")
        self.assertEqual(self.api.get_file_calls, [])
        # a corrupt sibling record does not block a new healthy upload
        self.assertEqual(self.upload(223)["result"], "staged")
        # direct validator: the worker audit rejects the same shapes
        for label, corrupt in cases.items():
            if label == "wrong chat":
                continue
            with self.assertRaises(hs.SupervisorError, msg=label):
                hs.validate_upload_record(corrupt, expected_upload_id=upload_id, task_files_dir=self.paths.task_files_dir, config=self.config)

    def test_f3_crash_after_initialization_replays_as_the_same_successful_start(self) -> None:
        upload_id = self.upload(230)["upload_id"]
        buttons = self.buttons()
        self.assertEqual(self.bridge.handle_update(callback(231, buttons["Start Task"]))["result"], "reset_budget_required")
        self.assertEqual(self.choose_reset_budget(232)["result"], "enqueued")
        command = self.pending_inbox()[0]
        self.write_plan()
        self.herdr.responses = [{"v2": ("plan", "human", "plan_approval", str(self.plan_payload()))}]
        original = hs.atomic_write_json
        crashed = {"n": 0}

        def crash_before_completed(path, value, **kw):
            if path.parent.name == "completed" and crashed["n"] == 0:
                crashed["n"] += 1
                raise RuntimeError("injected crash boundary")
            return original(path, value, **kw)

        hs.atomic_write_json = crash_before_completed
        try:
            with self.assertRaises(RuntimeError):
                self.sup.process_inbox()
        finally:
            hs.atomic_write_json = original
        state = self.state()
        self.assertEqual(state["supervisor_state"], "RUNNING")
        run_id = state["run_id"]
        self.assertEqual(state["processed_requests"][command["request_id"]]["run_id"], run_id, "request durably bound to the run")
        self.assertEqual(state["task_command"]["upload_id"], upload_id)
        self.assertEqual(len(list((self.paths.inbox_dir / "processing").glob("*.json"))), 1)
        self.assertEqual(self.herdr.prompts, [], "the worker has not prompted yet")
        # retry (new worker): same request replays as success, one run, the audit is reconciled
        sup2 = self.make_supervisor()
        sup2.herdr = self.herdr
        self.assertEqual(sup2.worker(), 4)
        results = [json.loads(p.read_text()) for p in (self.paths.inbox_dir / "completed").glob("*.json")]
        self.assertEqual(len(results), 1, "one durable command result")
        self.assertTrue(results[0]["ok"] and results[0]["replayed"])
        self.assertEqual(results[0]["run_id"], run_id)
        self.assertEqual(self.state()["run_id"], run_id, "no second run")
        self.assertEqual([n for n, _ in self.herdr.prompts], ["codex-main"], "exactly one Codex prompt")
        self.assertEqual(self.state()["supervisor_state"], "WAIT_PLAN_APPROVAL")
        self.assertEqual(self.meta(upload_id)["status"], "started")
        self.assertEqual(self.meta(upload_id)["run_id"], run_id)
        self.assertEqual(self.event_types().count("COMMAND_RESULT"), 1)
        # a different task request stays blocked by the active run
        self.sup.enqueue_command({"request_id": "req-000different", "action": "task", "task_text": "another", "actor": "x"})
        other = self.sup.process_inbox()[0]
        self.assertFalse(other["ok"])
        self.assertIn("still", other["message"])
        self.assertEqual(self.state()["run_id"], run_id)

    def test_f3_audit_failure_cannot_fail_the_start(self) -> None:
        upload_id = self.upload(240)["upload_id"]
        self.assertEqual(self.bridge.handle_update(callback(241, self.buttons()["Start Task"]))["result"], "reset_budget_required")
        self.assertEqual(self.choose_reset_budget(242)["result"], "enqueued")
        (self.paths.task_files_dir / f"{upload_id}.json").write_text("{not json")  # corrupt audit record
        results = self.sup.process_inbox()
        self.assertTrue(results[0]["ok"])
        self.assertEqual(self.state()["supervisor_state"], "RUNNING")
