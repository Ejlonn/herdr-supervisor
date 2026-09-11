"""Release blockers 1 and 3: long Markdown outputs as registered documents with durable part-level
delivery, protected artifact access, and the human-readable presentation layer (renderer snapshots)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest import mock

from test_telegram import CHAT, OWNER, FakeApi, TelegramCase, callback, message
from v2_fixtures import FAKE_TOKEN, NOW, SHA_A, FakeHerdr, hs

import herdr_artifacts as ha  # noqa: E402
import herdr_present as hp  # noqa: E402
import telegram_api as tg  # noqa: E402

LONG_MD = "# Big report\n\n" + "\n".join(f"- line {i} with **bold** and `code`" for i in range(400)) + "\n\n## Section\n\ntext\n"


class OutputCase(TelegramCase):
    def emit(self, kind: str, data: dict) -> dict:
        with self.sup.store.transaction():
            st = self.sup.store.read_state()
            event_id = self.sup.emit_event(st, kind, data)
            self.sup.store.write_state(st)
        return json.loads((self.paths.outbox_dir / "events" / f"{event_id}.json").read_text())

    def sidecar(self, event_id: str) -> dict:
        return json.loads((self.paths.outbox_dir / "delivery" / f"{event_id}.json").read_text())


class LongOutputTests(OutputCase):
    def test_bot_api_builds_bounded_multipart_document_request(self) -> None:
        captured: dict = {}

        def opener(url, body, timeout, content_type="application/json"):
            captured.update(url=url, body=body, timeout=timeout, content_type=content_type)
            return 200, json.dumps({"ok": True, "result": {"message_id": 7, "document": {"file_id": "f"}}}).encode()

        api = tg.BotApi(FAKE_TOKEN, opener=opener)
        result = api.send_document(CHAT, "report.md", b"# Heading\n\n**body**\n", caption="Review report")
        self.assertEqual(result["message_id"], 7)
        self.assertTrue(captured["content_type"].startswith("multipart/form-data; boundary="))
        self.assertIn(b'name="chat_id"', captured["body"])
        self.assertIn(b'filename="report.md"', captured["body"])
        self.assertIn(b"Content-Type: text/markdown", captured["body"])
        self.assertIn(b"# Heading\n\n**body**\n", captured["body"])
        self.assertNotIn(FAKE_TOKEN.encode(), captured["body"])

    def test_short_output_is_one_html_message(self) -> None:
        self.plan_gate()
        self.bridge.handle_update(message(1, "/status"))
        sent = self.api.sent[-1]
        self.assertEqual(sent["parse_mode"], "HTML")
        self.assertIn("<b>Plan awaiting your approval</b>", sent["text"])
        self.assertEqual(self.api.documents, [])

    def test_long_ask_answer_is_summary_plus_markdown_document(self) -> None:
        self.plan_gate()
        self.emit("QUERY_RESULT", {"request_id": "q-1", "chat_id": CHAT, "source": "/ask", "ok": True, "answer": LONG_MD, "provider": "codex", "failover": False})
        counts = self.bridge.deliver_outbox()
        self.assertEqual(counts["uncertain"], 0)
        doc = self.api.documents[-1]
        self.assertTrue(doc["filename"].endswith(".md"))
        self.assertTrue(doc["filename"].startswith("query_answer-"))
        self.assertIn(hp.abbrev(self.state()["run_id"]), doc["filename"], "artifact name is bound to the run")
        self.assertEqual(doc["data"].decode(), LONG_MD, "Markdown structure preserved byte for byte")
        summary = [m for m in self.api.sent if "Full answer attached" in m["text"]]
        self.assertEqual(len(summary), 1)
        self.assertNotIn("line 399", summary[0]["text"], "summary is bounded")
        records = ha.list_records(self.paths)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["category"], "query_answer")
        self.assertIn(hp.abbrev(records[0]["event_id"]), records[0]["display_name"], "artifact name is bound to the event")
        derivative = Path(records[0]["derivative_path"])
        self.assertEqual(oct(derivative.stat().st_mode & 0o777), "0o600")
        self.assertEqual(oct(derivative.parent.stat().st_mode & 0o777), "0o700")
        with self.assertRaises(FileExistsError):
            ha._write_exclusive(derivative, b"x")  # never overwritten

    def test_long_output_threshold_is_configurable(self) -> None:
        self.plan_gate()
        bridge = self.make_bridge(long_output_chars=512)
        self.emit("QUERY_RESULT", {"request_id": "q-config", "chat_id": CHAT, "source": "/ask", "ok": True, "answer": "x" * 600, "provider": "codex"})
        bridge.deliver_outbox()
        self.assertEqual(len(self.api.documents), 1)

    def test_long_logs_use_document_path_and_short_logs_stay_inline(self) -> None:
        self.plan_gate()
        self.bridge.handle_update(message(1, "/logs"))
        self.assertEqual(self.api.documents, [])
        for i in range(120):
            self.emit("WAIT_USER", {"reason": f"reason {i} " + "x" * 80})
        result = self.bridge.handle_update(message(2, "/logs 200"))
        self.assertEqual(result["result"], "logs_document")
        self.assertTrue(self.api.documents[-1]["filename"].startswith("logs-"))
        self.assertIn("# Supervisor event log", self.api.documents[-1]["data"].decode())

    def test_long_logs_document_failure_survives_restart_and_retries(self) -> None:
        self.plan_gate()
        for i in range(120):
            self.emit("WAIT_USER", {"reason": f"reason {i} " + "x" * 80})
        self.api.fail["sendDocument"] = [tg.TelegramError("API_ERROR", "temporary", error_code=500)]
        result = self.bridge.handle_update(message(88, "/logs 200"))
        self.assertEqual(result["result"], "logs_document")
        queued = list(self.tg_paths.document_deliveries_dir.glob("*.json"))
        self.assertEqual(len(queued), 1)
        self.assertEqual(json.loads(queued[0].read_text())["status"], "pending")
        bridge2 = self.make_bridge()
        self.assertEqual(bridge2.deliver_document_queue()["sent"], 1)
        self.assertEqual(json.loads(queued[0].read_text())["status"], "delivered")
        self.assertEqual(bridge2.deliver_document_queue()["sent"], 0)
        self.assertEqual(len(self.api.documents), 1)

    def test_replayed_long_logs_update_reuses_registered_artifact_and_delivery(self) -> None:
        self.plan_gate()
        for i in range(120):
            self.emit("WAIT_USER", {"reason": f"reason {i} " + "x" * 80})
        self.bridge.handle_update(message(91, "/logs 200"))
        self.bridge.handle_update(message(91, "/logs 200"))  # crash before update journal/offset persisted
        logs = [record for record in ha.list_records(self.paths) if record["category"] == "logs"]
        self.assertEqual(len(logs), 1)
        self.assertEqual(len(self.api.documents), 1)
        self.assertEqual(len(list(self.tg_paths.document_deliveries_dir.glob("*.json"))), 1)

    def test_restart_recovers_interrupted_direct_document_send_as_ambiguous(self) -> None:
        self.plan_gate()
        record = ha.register_text(self.paths, self.config, category="logs", text=LONG_MD, name="logs", run_id=self.state()["run_id"])
        delivery_id = hs.sha256_bytes(f"crash:{CHAT}".encode())
        path = self.tg_paths.document_deliveries_dir / f"{delivery_id}.json"
        hs.atomic_write_json(path, {"schema_version": 1, "delivery_id": delivery_id, "artifact_id": record["artifact_id"], "chat_id": CHAT, "caption": "logs", "status": "sending", "attempts": 1, "replacements": 0})
        bridge2 = self.make_bridge()
        self.assertEqual(bridge2.deliver_document_queue()["sent"], 1)
        recovered = json.loads(path.read_text())
        self.assertEqual(recovered["status"], "delivered")
        self.assertEqual(recovered["replacements"], 1)

    def test_restart_recovers_interrupted_outbox_document_send_as_ambiguous(self) -> None:
        self.plan_gate()
        event = self.emit("QUERY_RESULT", {"request_id": "q-crash-send", "chat_id": CHAT, "source": "/ask", "ok": True, "answer": LONG_MD, "provider": "codex"})
        sidecar_path = self.paths.outbox_dir / "delivery" / f"{event['event_id']}.json"
        sidecar = self.sidecar(event["event_id"])
        rendered = self.bridge._render_for_delivery(event, None, None)
        artifact_id = self.bridge._document_for_event(event, sidecar_path, sidecar, rendered)
        sidecar = self.sidecar(event["event_id"])
        sidecar["status"] = "sending_document"
        sidecar["parts"]["summary"] = {"status": "delivered", "message_id": 4}
        sidecar["parts"]["document"].update(status="sending", attempts=1, replacements=0)
        hs.atomic_write_json(sidecar_path, sidecar)
        bridge2 = self.make_bridge()
        self.assertGreaterEqual(bridge2.deliver_outbox()["sent"], 1)
        recovered = self.sidecar(event["event_id"])
        self.assertEqual(recovered["status"], "delivered")
        self.assertEqual(recovered["parts"]["document"]["artifact_id"], artifact_id)
        self.assertEqual(recovered["parts"]["document"]["replacements"], 1)
        self.assertEqual(len(self.api.documents), 1)

    def test_final_task_report_summary_plus_document_with_twenty_fields(self) -> None:
        self.plan_gate()
        self.approve_pending()
        with self.sup.store.transaction():
            st = self.sup.store.read_state()
            st["supervisor_state"] = "DONE"
            st["pending_gate"]["status"] = "approved"
            self.sup.emit_event(st, "TASK_DONE", {"stage": "review", "handoff": "all done"})
            self.sup.store.write_state(st)
        self.bridge.deliver_outbox()
        summary = [m for m in self.api.sent if "<b>Task complete</b>" in m["text"]]
        self.assertEqual(len(summary), 1)
        buttons = {b["text"] for row in summary[0]["reply_markup"]["inline_keyboard"] for b in row}
        self.assertEqual(buttons, {"Open Full Report", "Status"})
        doc = self.api.documents[-1]
        self.assertTrue(doc["filename"].startswith("final_report-"))
        body = doc["data"].decode()
        for index in range(1, 21):
            self.assertIn(f"## {index}. ", body)
        self.assertIn("## 20. Commit/push/publication actions still requiring approval", body)
        self.assertNotIn(str(self.state().get("task_reference")), body)
        # the button re-sends the same registered report; a duplicate event never registers a second one
        token = next(b["callback_data"] for row in summary[0]["reply_markup"]["inline_keyboard"] for b in row if b["text"] == "Open Full Report")
        self.bridge.handle_update(callback(9, token))
        self.assertEqual(len(ha.list_records(self.paths)), 1)
        self.assertEqual(len(self.api.documents), 2)

    def test_task_done_prefers_trusted_authoritative_final_report_descriptor(self) -> None:
        self.plan_gate()
        report = self.review_dir / "FINAL_REPORT.md"
        body = "# Authoritative report\n\n## Evidence\n\n23 focused checks passed.\n"
        report.write_text(body)
        descriptor = {"schema_version": 1, "source_path": str(report), "source_sha256": hs.sha256_bytes(report.read_bytes()), "source_bytes": len(report.read_bytes()), "title": "Final release report"}
        event = self.emit("TASK_DONE", {"stage": "review", "handoff": "accepted", "final_report": descriptor})
        self.bridge.deliver_outbox()
        self.assertEqual(self.api.documents[-1]["data"].decode(), body)
        record = [r for r in ha.list_records(self.paths) if r.get("event_id") == event["event_id"]][0]
        self.assertEqual(record["source_sha256"], descriptor["source_sha256"])
        self.assertEqual(record["source_path"], str(report))

    def test_changed_authoritative_final_report_is_not_replaced_by_generated_content(self) -> None:
        self.plan_gate()
        report = self.review_dir / "FINAL_REPORT.md"
        report.write_text("# Original\n")
        descriptor = {"schema_version": 1, "source_path": str(report), "source_sha256": hs.sha256_bytes(report.read_bytes()), "source_bytes": len(report.read_bytes()), "title": "Final report"}
        report.write_text("# Changed after completion\n")
        self.emit("TASK_DONE", {"stage": "review", "handoff": "accepted", "final_report": descriptor})
        self.bridge.deliver_outbox()
        self.assertEqual(self.api.documents, [])
        self.assertEqual(ha.list_records(self.paths), [])

    def test_document_failure_is_durable_and_retried_after_restart_without_duplicates(self) -> None:
        self.plan_gate()
        event = self.emit("QUERY_RESULT", {"request_id": "q-2", "chat_id": CHAT, "source": "/ask", "ok": True, "answer": LONG_MD, "provider": "codex"})
        self.api.fail["sendDocument"] = [tg.TelegramError("HTTPS_FAILURE", "timeout")]
        counts = self.bridge.deliver_outbox()
        self.assertEqual(counts["uncertain"], 1)
        sidecar = self.sidecar(event["event_id"])
        self.assertEqual(sidecar["status"], "delivery_uncertain")
        self.assertEqual(sidecar["parts"]["summary"]["status"], "delivered")
        self.assertEqual(sidecar["parts"]["document"]["status"], "delivery_uncertain")
        artifact_id = sidecar["parts"]["document"]["artifact_id"]
        self.assertTrue(Path(ha.load_record(self.paths, artifact_id)["derivative_path"]).exists(), "local artifact preserved")
        summaries_before = len(self.api.sent)
        bridge2 = self.make_bridge()  # daemon restart
        counts = bridge2.deliver_outbox()
        self.assertEqual(counts["sent"], 1)
        self.assertEqual(len(self.api.sent), summaries_before, "summary not re-sent")
        self.assertEqual(len(self.api.documents), 1, "exactly one replacement document")
        self.assertEqual(self.sidecar(event["event_id"])["status"], "delivered")
        self.assertEqual(self.sidecar(event["event_id"])["parts"]["document"]["artifact_id"], artifact_id, "same artifact reused")
        self.assertEqual(bridge2.deliver_outbox()["sent"], 0)
        # a second uncertain outcome exhausts the replacement budget: no uncontrolled duplicates
        event2 = self.emit("QUERY_RESULT", {"request_id": "q-3", "chat_id": CHAT, "source": "/ask", "ok": True, "answer": LONG_MD, "provider": "codex"})
        self.api.fail["sendDocument"] = [tg.TelegramError("HTTPS_FAILURE", "t1"), tg.TelegramError("HTTPS_FAILURE", "t2")]
        bridge2.deliver_outbox()
        bridge2.deliver_outbox()
        bridge2.deliver_outbox()
        self.assertEqual(self.sidecar(event2["event_id"])["status"], "delivered_document_uncertain")
        self.assertEqual(self.sidecar(event2["event_id"])["parts"]["document"]["attempts"], 2)
        self.assertEqual(len(self.api.documents), 1)

    def test_duplicate_event_ids_do_not_duplicate_documents(self) -> None:
        self.plan_gate()
        event = self.emit("QUERY_RESULT", {"request_id": "q-4", "chat_id": CHAT, "source": "/ask", "ok": True, "answer": LONG_MD, "provider": "codex"})
        self.bridge.deliver_outbox()
        # the same event file is "re-created" (crash/replay): sidecar says delivered, nothing is sent again
        hs.atomic_write_json(self.paths.outbox_dir / "events" / f"{event['event_id']}.json", event)
        self.bridge.deliver_outbox()
        self.assertEqual(len(self.api.documents), 1)
        self.assertEqual(len(ha.list_records(self.paths)), 1)

    def test_artifact_binding_is_persisted_before_network_delivery(self) -> None:
        self.plan_gate()
        event = self.emit("QUERY_RESULT", {"request_id": "q-crash", "chat_id": CHAT, "source": "/ask", "ok": True, "answer": LONG_MD, "provider": "codex"})
        sidecar_path = self.paths.outbox_dir / "delivery" / f"{event['event_id']}.json"
        sidecar = self.sidecar(event["event_id"])
        rendered = self.bridge._render_for_delivery(event, None, None)
        artifact_id = self.bridge._document_for_event(event, sidecar_path, sidecar, rendered)
        self.assertEqual(self.sidecar(event["event_id"])["parts"]["document"]["artifact_id"], artifact_id)
        bridge2 = self.make_bridge()
        bridge2.deliver_outbox()
        self.assertEqual(len(ha.list_records(self.paths)), 1)
        self.assertEqual(len(self.api.documents), 1)

    def test_transient_telegram_5xx_document_failure_is_retried(self) -> None:
        self.plan_gate()
        event = self.emit("QUERY_RESULT", {"request_id": "q-5xx", "chat_id": CHAT, "source": "/ask", "ok": True, "answer": LONG_MD, "provider": "codex"})
        self.api.fail["sendDocument"] = [tg.TelegramError("API_ERROR", "temporary server error", error_code=500)]
        self.bridge.deliver_outbox()
        first = self.sidecar(event["event_id"])
        self.assertEqual(first["status"], "delivery_uncertain")
        self.assertEqual(first["parts"]["document"]["status"], "pending")
        self.bridge.deliver_outbox()
        self.assertEqual(self.sidecar(event["event_id"])["status"], "delivered")
        self.assertEqual(len(self.api.documents), 1)


class ArtifactAccessTests(OutputCase):
    def test_structured_credentials_aws_keys_and_userinfo_urls_are_redacted_everywhere(self) -> None:
        values = (
            '"token": "json-secret-123456"',
            "aws_secret_access_key: yamlSecret1234567890",
            "AKIA1234567890ABCDEF",
            "https://service-user:credential-value@example.invalid/private",
        )
        raw = "# Report\n\n" + "\n".join(values) + "\n"
        short = hp.redact(raw, limit=4000)
        for secret in ("json-secret-123456", "yamlSecret1234567890", "AKIA1234567890ABCDEF", "credential-value"):
            self.assertNotIn(secret, short)
        record = ha.register_text(self.paths, self.config, category="report", text=raw, name="secrets", run_id="r")
        derivative = Path(record["derivative_path"]).read_text()
        for secret in ("json-secret-123456", "yamlSecret1234567890", "AKIA1234567890ABCDEF", "credential-value"):
            self.assertNotIn(secret, derivative)
        self.assertGreaterEqual(record["redactions"], 4)

    def test_uncertain_sensitive_artifact_fails_closed(self) -> None:
        source = self.review_dir / "uncertain.md"
        source.write_text("# Report\n\npassword:\n")
        with self.assertRaisesRegex(hs.SupervisorError, "could not be classified safely"):
            ha.register_file(self.paths, self.config, category="report", source_path=str(source), run_id="r")

    def test_unauthorized_user_cannot_retrieve_a_report_and_no_path_command_exists(self) -> None:
        self.plan_gate()
        record = ha.register_text(self.paths, self.config, category="report", text="# r\n", name="r", run_id=self.state()["run_id"])
        for command in ("/file /etc/passwd", "/report /home/x/.env", f"/artifact {record['artifact_id']}"):
            result = self.bridge.handle_update(message(1, command))
            self.assertEqual(result["result"], "unknown -> help", command)
        self.assertEqual(self.api.documents, [])
        # a view_report token belongs to the owner/chat only
        self.emit("TASK_DONE", {"stage": "x", "handoff": "y"})
        self.bridge.deliver_outbox()
        token = next(b["callback_data"] for m in self.api.sent for row in (m["reply_markup"] or {}).get("inline_keyboard", []) for b in row if b["text"] == "Open Full Report")
        self.assertEqual(self.bridge.handle_update(callback(5, token, user=999))["rejected"], "wrong_user")
        self.assertEqual(self.bridge.handle_update(callback(6, token, chat=777))["rejected"], "wrong_chat")

    def test_registry_refuses_protected_arbitrary_symlinked_and_oversized_sources(self) -> None:
        secret_dir = Path(self.tmp.name) / ".config" / "herdr-telegram"
        secret_dir.mkdir(parents=True)
        (secret_dir / "bot-token").write_text(FAKE_TOKEN)
        outside = Path(self.tmp.name) / "outside.md"
        outside.write_text("# outside\n")
        link = self.review_dir / "link.md"
        link.symlink_to(outside)
        big = self.review_dir / "big.md"
        big.write_bytes(b"x" * (2 * 1024 * 1024 + 1))
        env = self.review_dir / ".env"
        env.write_text("SECRET=1\n")
        binary = self.review_dir / "bin.md"
        binary.write_bytes(b"\xff\xfe\x00")
        for label, path in (("token", secret_dir / "bot-token"), ("outside root", outside), ("symlink", link), ("oversized", big), ("dotenv", env), ("wrong suffix", self.review_dir / "CODEX_PLAN.md.bak"), ("relative", Path("relative.md")), ("binary", binary)):
            if label == "wrong suffix":
                path.write_text("x")
            with self.assertRaises(hs.SupervisorError, msg=label):
                ha.register_file(self.paths, self.config, category="report", source_path=str(path), run_id="r")
        self.assertEqual(ha.list_records(self.paths), [])
        with self.assertRaises(hs.SupervisorError):
            ha.register_text(self.paths, self.config, category="not-a-category", text="x", name="x", run_id="r")

    def test_missing_corrupt_and_changed_artifacts_fail_closed_at_send_time(self) -> None:
        self.plan_gate()
        record = ha.register_text(self.paths, self.config, category="report", text="# ok\n", name="ok", run_id=self.state()["run_id"])
        derivative = Path(record["derivative_path"])
        self.assertTrue(self.bridge.send_artifact(CHAT, record["artifact_id"]))
        derivative.write_text("# tampered\n")
        self.assertFalse(self.bridge.send_artifact(CHAT, record["artifact_id"]))
        self.assertIn("changed", self.api.sent[-1]["text"])
        derivative.unlink()
        self.assertFalse(self.bridge.send_artifact(CHAT, record["artifact_id"]))
        self.assertIn("missing", self.api.sent[-1]["text"])
        (ha.registry_dir(self.paths) / f"{record['artifact_id']}.json").write_text("{not json")
        self.assertFalse(self.bridge.send_artifact(CHAT, record["artifact_id"]))
        self.assertFalse(self.bridge.send_artifact(CHAT, "0" * 32))
        self.assertFalse(self.bridge.send_artifact(CHAT, "../../etc/passwd"))
        self.assertEqual(len(self.api.documents), 1)

    def test_redaction_applies_to_derivatives_and_originals_are_untouched(self) -> None:
        source = self.review_dir / "CLAUDE_HANDOFF.md"
        original = f"# Handoff\n\ntoken {FAKE_TOKEN}\napi_key=abcdef123456\n-----BEGIN PRIVATE KEY-----\nzzz\n-----END PRIVATE KEY-----\nfine text\n"
        source.write_text(original)
        record = ha.register_file(self.paths, self.config, category="handoff", source_path=str(source), run_id="r")
        derivative = Path(record["derivative_path"]).read_text()
        self.assertNotIn(FAKE_TOKEN, derivative)
        self.assertNotIn("abcdef123456", derivative)
        self.assertNotIn("zzz", derivative)
        self.assertIn("fine text", derivative)
        self.assertEqual(source.read_text(), original, "authoritative source unchanged")
        self.assertEqual(record["source_sha256"], hs.sha256_bytes(original.encode()))
        self.assertGreaterEqual(record["redactions"], 3)
        too_secret = self.review_dir / "dump.md"
        too_secret.write_text("\n".join(f"password = hunter{i}hunter{i}" for i in range(80)))
        with self.assertRaises(hs.SupervisorError):
            ha.register_file(self.paths, self.config, category="report", source_path=str(too_secret), run_id="r")

    def test_plan_view_uses_registered_document_and_gate_hash(self) -> None:
        self.plan_gate()
        self.bridge.deliver_outbox()
        buttons = {b["text"]: b["callback_data"] for m in self.api.sent for row in (m["reply_markup"] or {}).get("inline_keyboard", []) for b in row}
        self.bridge.handle_update(callback(1, buttons["View Plan"]))
        self.assertEqual(self.api.documents[-1]["filename"].split("-")[0], "plan")
        self.assertIn("# CODEX_PLAN", self.api.documents[-1]["data"].decode())
        self.assertEqual(ha.list_records(self.paths)[-1]["source_sha256"], self.state()["pending_gate"]["artifact_sha256"])

    def test_registration_binds_to_expected_authoritative_hash(self) -> None:
        source = self.review_dir / "CODEX_REVIEW.md"
        source.write_text("# first\n")
        stale_hash = hs.sha256_bytes(source.read_bytes())
        source.write_text("# changed\n")
        with self.assertRaisesRegex(hs.SupervisorError, "changed"):
            ha.register_file(self.paths, self.config, category="review", source_path=str(source), run_id="r", expected_source_sha256=stale_hash)
        self.assertEqual(ha.list_records(self.paths), [])

    def test_redacted_derivative_enforces_byte_limit(self) -> None:
        self.config["max_artifact_bytes"] = 12
        source = self.review_dir / "small.md"
        source.write_bytes(b"safe")
        # Character bounds are insufficient for UTF-8 document limits; assert the final bytes are checked.
        with mock.patch.object(ha.hp, "redact", return_value="🚀" * 4):
            with self.assertRaisesRegex(hs.SupervisorError, "redacted artifact exceeds"):
                ha.register_file(self.paths, self.config, category="report", source_path=str(source), run_id="r")


class RendererSnapshotTests(OutputCase):
    """Representative mobile messages for every renderer named in the plan; escaping, unicode, missing
    fields, long fields, services, abbreviation, timezone."""

    def test_every_event_type_renders_and_escapes(self) -> None:
        self.plan_gate()
        state = self.state()
        gate = state["pending_gate"]
        status = self.sup.status()
        status["wait_user_requires_action"] = True
        hostile = "<script>alert(1)</script> & \"quotes\" ünïcödé 🚀 " + "x" * 500
        samples = {
            "TASK_STARTED": {"task": hostile, "start": "codex"},
            "PLAN_APPROVAL_REQUIRED": {},
            "QUESTION_ASKED": {},
            "REVISION_REQUESTED": {"gate_type": "plan_approval", "agent": "codex"},
            "WAIT_USER": {"reason": hostile},
            "WAIT_QUOTA": {"provider": "codex", "resume_at": NOW + 1800, "windows": ["five_hour", "weekly"], "deferred_anomaly": "missing_protocol", "early_refresh_available": True},
            "QUOTA_RESUMED": {"provider": "codex", "early": True, "rechecks": 3, "deferred_anomaly": "missing_protocol"},
            "RUNTIME_VALIDATION_READY": {},
            "RUNTIME_VALIDATION_FAILED": {"candidate_sha": SHA_A, "environment": "TEST"},
            "PUSH_APPROVAL_REQUIRED": {},
            "TASK_PAUSED": {}, "TASK_CANCELLED": {}, "TASK_ERROR": {"error": hostile}, "TASK_DONE": {"stage": "review", "handoff": hostile},
            "RECOVERED_AFTER_RESTART": {"state": "WAIT_QUOTA"}, "COMMAND_RESULT": {"action": "approve", "ok": False, "message": hostile},
            "QUERY_RESULT": {"ok": True, "answer": "short answer <b>", "provider": "claude", "failover": True, "failover_reason": {"reason": "quota_blocked", "resets_at": NOW + 3600}, "selection": {"preferred": "codex"}},
            "PLAN_APPROVED": {}, "PUSH_APPROVED": {}, "QUESTION_ANSWERED": {}, "TASK_RESUMED": {}, "RUNTIME_VALIDATION_PASSED": {"candidate_sha": SHA_A, "environment": "TEST"},
            "BACKUP_HEALTH": {"status": "stale", "last_verified": hs.iso_utc(NOW - 3 * 86400), "strategy": "ssh_snapshot", "age_hours": 72, "max_age_hours": 24, "failure_reason": "destination unreachable"},
        }
        runtime_gate = {**gate, "gate_type": "runtime_validation", "summary_fields": {"task_title": "t", "candidate_sha": SHA_A, "local_gate_result": "PASS", "codex_review_status": "APPROVED", "affected_services": ["frontend", "worker", "api"], "rebuild_required": True, "rebuild_reason": "images contain source"}}
        push_gate = {**gate, "gate_type": "push_approval", "summary_fields": {"task_title": "t", "candidate_sha": SHA_A, "local_gate_result": "PASS", "codex_review_status": "APPROVED", "runtime_evidence_status": "PASS", "affected_services": ["frontend"]}}
        question_gate = {**gate, "gate_type": "generic_question", "summary_fields": {"task_title": "t", "question": hostile, "answer_mode": "choice", "choices": ["enum", "text"], "max_answer_chars": 200}}
        gates = {"RUNTIME_VALIDATION_READY": runtime_gate, "PUSH_APPROVAL_REQUIRED": push_gate, "QUESTION_ASKED": question_gate, "PLAN_APPROVAL_REQUIRED": gate}
        for kind, data in samples.items():
            event = {"type": kind, "run_id": state["run_id"], "gate_id": gate["gate_id"], "at_unix": NOW, "data": data}
            rendered = hp.render_event(event, status, gates.get(kind), "Europe/Istanbul")
            text = rendered.html
            self.assertNotIn("<script>", text, kind)
            self.assertIn("&lt;script&gt;", text) if kind in ("TASK_STARTED", "WAIT_USER", "TASK_ERROR", "TASK_DONE", "COMMAND_RESULT", "QUESTION_ASKED") else None
            self.assertLessEqual(len(text), 3800, kind)
            self.assertTrue(text.startswith("<b>"), kind)
            for row in rendered.keyboard:
                self.assertLessEqual(len(row), 3, kind)
            self.assertLessEqual(len(rendered.keyboard), 3, kind)
        self.assertIn("EET", hp.render_event({"type": "WAIT_QUOTA", "run_id": "r", "data": samples["WAIT_QUOTA"]}, status, None, "Europe/Istanbul").html)
        self.assertIn("resets at", hp.render_event({"type": "WAIT_QUOTA", "run_id": "r", "data": samples["WAIT_QUOTA"]}, status, None, "Europe/Istanbul").html)
        # missing optional data never crashes
        for kind in samples:
            hp.render_event({"type": kind, "data": {}}, None, None, "Europe/Istanbul")
        hp.render_event({"type": "UNKNOWN_KIND", "data": {"x": 1}}, None, None, "Not/AZone")

    def test_keyboards_are_state_aware_and_never_fabricate_runtime(self) -> None:
        expectations = {
            "WAIT_PLAN_APPROVAL": {"View Plan", "Approve", "Request Revision", "Reject"},
            "WAIT_QUOTA": {"Status", "Pause", "Refresh quota"},
            "WAIT_RUNTIME_VALIDATION": {"View Report", "Status"},
            "WAIT_PUSH_APPROVAL": {"Approve Push Stage", "Keep Waiting", "Cancel"},
            "PAUSED": {"Resume", "Cancel"},
            "WAIT_USER": {"Send Guidance", "Cancel Task", "Status"},
            "RUNNING": {"Status", "Pause"},
            "DONE": {"Status"},
            "NO_TASK": set(),
        }
        for state, labels in expectations.items():
            spec = hp.keyboard_for_status({"supervisor_state": state, "wait_user_requires_action": state == "WAIT_USER"})
            self.assertEqual({label for row in spec for label, _, _ in row}, labels, state)
        runtime = hp.render_runtime_required({"summary_fields": {"candidate_sha": SHA_A}}, "Europe/Istanbul")
        actions = {action for row in runtime.keyboard for _, action, _ in row}
        self.assertNotIn("approve", actions)
        self.assertIn("NOT RUN", runtime.html)
        self.assertIn("BLOCKED", runtime.html)

    def test_status_answers_the_ux_questions_and_raw_is_sanitized(self) -> None:
        self.plan_gate()
        with self.sup.store.transaction():
            st = self.sup.store.read_state()
            st["wait_user_reason"] = f"token {FAKE_TOKEN}"
            self.sup.store.write_state(st)
        self.bridge.handle_update(message(1, "/status"))
        text = self.api.sent[-1]["text"]
        for fragment in ("<b>Plan awaiting your approval</b>", "Task: Add the widget", "Pending gate: plan approval", "Codex: idle", "5-hour:", "As of "):
            self.assertIn(fragment, text)
        self.assertNotIn("WAIT_PLAN_APPROVAL", text, "internal enum hidden in the normal view")
        self.assertNotIn(FAKE_TOKEN, text)
        self.bridge.handle_update(message(2, "/status raw"))
        raw = self.api.sent[-1]["text"]
        self.assertIn("WAIT_PLAN_APPROVAL", raw)
        self.assertIn("<pre>", raw)
        self.assertNotIn(FAKE_TOKEN, raw)

    def test_status_callback_uses_the_centralized_html_renderer(self) -> None:
        self.plan_gate()
        with self.sup.store.transaction():
            state = self.sup.store.read_state()
            state["pending_gate"]["status"] = "approved"
            state["supervisor_state"] = "RUNNING"
            self.sup.store.write_state(state)
        self.bridge.handle_update(message(30, "/status"))
        token = next(button["callback_data"] for row in self.api.sent[-1]["reply_markup"]["inline_keyboard"] for button in row if button["text"] == "Status")
        self.bridge.handle_update(callback(31, token))
        sent = self.api.sent[-1]
        self.assertEqual(sent["parse_mode"], "HTML")
        self.assertIn("<b>Task in progress</b>", sent["text"])
        self.assertNotIn("Supervisor: RUNNING", sent["text"])

    def test_ask_failover_and_unavailable_render_clearly(self) -> None:
        self.plan_gate()
        self.emit("QUERY_RESULT", {"request_id": "q-5", "chat_id": CHAT, "ok": True, "answer": "It uses CAS first.", "provider": "claude", "failover": True, "failover_reason": {"reason": "quota_blocked", "resets_at": NOW + 3600}, "selection": {"preferred": "codex"}})
        self.emit("QUERY_RESULT", {"request_id": "q-6", "chat_id": CHAT, "ok": False, "answer": "The query cannot be run safely right now. Codex: quota-blocked until 09:00; Claude: currently working on the active supervised task.", "provider": None})
        self.bridge.deliver_outbox()
        texts = "\n".join(self.texts())
        self.assertIn("Answered by Claude because the preferred Codex query session is quota-blocked until", texts)
        self.assertIn("<b>Query not answered</b>", texts)
        self.assertIn("cannot be run safely", texts)
