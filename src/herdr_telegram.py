#!/usr/bin/env python3
"""herdr-telegram: deterministic Telegram long-poll bridge for herdr-supervisor.

Boundary (enforced by tests): this module authenticates updates, renders typed supervisor state and
events, enqueues validated command files, and delivers durable notifications. It has no code path
for pane/agent input, keys, shells, Git, workflow decisions, runtime evidence, or session recovery.
Network: outbound verified HTTPS to api.telegram.org only; no listener, webhook, or inbound socket.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import getpass
import hashlib
import json
import os
import re
import secrets
import stat
import sys
import time
import uuid
import zoneinfo
from pathlib import Path
from typing import Any, Callable

import herdr_artifacts as ha
import herdr_backup as hb
import herdr_present as hp
import herdr_query
import herdr_supervisor as hs
import herdr_codex_reset as hcr
import telegram_api as tg

CONFIG_SCHEMA = 1
MODES = ("unconfigured", "shadow", "actionable")
CHUNK = 3800
DEFAULT_TIMEZONE = "UTC"
COMMANDS = ("status", "task", "reset-budget", "plan", "pending", "pause", "resume", "cancel", "logs", "doctor", "ask", "revise", "answer", "backup", "help")
UPLOAD_STATUSES = ("pending_confirmation", "reset_authorization", "start_enqueued", "started", "cancelled", "expired", "failed", "superseded")
_FILENAME_RE = re.compile(r"[^A-Za-z0-9._ ()\-]")
INFO_CONTROLS = (("Status", "status"), ("Pause", "pause"), ("Cancel", "cancel"))

DEFAULT_TG_CONFIG: dict[str, Any] = {
    "schema_version": CONFIG_SCHEMA,
    "mode": "unconfigured",
    "owner_user_id": None,
    "chat_id": None,
    "timezone": DEFAULT_TIMEZONE,
    "plain_text_queries": False,
    "token_file": None,
    "poll_timeout_seconds": 25,
    "http_timeout_seconds": 35,
    "backoff_base_seconds": 1.0,
    "backoff_cap_seconds": 60.0,
    "callback_ttl_seconds": 7 * 86400,
    "intent_ttl_seconds": 1800,
    "max_task_chars": 4000,
    "informational_collapse_after": 8,
    "long_output_chars": hp.LONG_MESSAGE_CHARS,
    "max_document_replacements": 1,
}

_SECRET_PATTERNS = hp._SECRET_PATTERNS
_CONTROL_RE = hp.CONTROL_RE
redact = hp.redact
fmt_local = hp.fmt_local
chunks = hp.chunks


# --------------------------------------------------------------------------- config / paths


class TelegramPaths:
    def __init__(self, config_file: Path | None = None, state_dir: Path | None = None) -> None:
        home = Path.home()
        self.config_file = config_file or Path(os.environ.get("HERDR_TELEGRAM_CONFIG", home / ".config/herdr-telegram/config.json"))
        self.state_dir = state_dir or Path(os.environ.get("HERDR_TELEGRAM_STATE_DIR", home / ".local/state/herdr-telegram"))

    @property
    def offset_file(self) -> Path:
        return self.state_dir / "offset.json"

    @property
    def updates_dir(self) -> Path:
        return self.state_dir / "updates"

    @property
    def interactions_dir(self) -> Path:
        return self.state_dir / "interactions"

    @property
    def intents_dir(self) -> Path:
        return self.state_dir / "intents"

    @property
    def document_deliveries_dir(self) -> Path:
        return self.state_dir / "document-deliveries"

    @property
    def lock_file(self) -> Path:
        return self.state_dir / "daemon.lock"

    @staticmethod
    def bot_lock_dir() -> Path:
        """F8/F11: fixed under $HOME, independent of the configurable Telegram state directory. The unit
        grants write access to exactly this directory; `ensure_bot_lock_dir()` creates it at install/setup."""
        return Path.home() / ".local/state/herdr-telegram-locks"

    @classmethod
    def ensure_bot_lock_dir(cls) -> Path:
        lock_dir = cls.bot_lock_dir()
        lock_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(lock_dir, 0o700)
        return lock_dir

    @classmethod
    def bot_lock_file(cls, bot_id: int) -> Path:
        """F8: one consumer per authenticated bot identity."""
        return cls.ensure_bot_lock_dir() / f"bot-{int(bot_id)}.lock"

    @property
    def heartbeat_file(self) -> Path:
        return self.state_dir / "heartbeat.json"

    @property
    def conflict_file(self) -> Path:
        return self.state_dir / "conflict.json"

    @property
    def rate_file(self) -> Path:
        return self.state_dir / "query-rate.json"

    def ensure(self) -> None:
        for path in (self.state_dir, self.updates_dir, self.interactions_dir, self.intents_dir, self.document_deliveries_dir):
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(path, 0o700)


def load_telegram_config(path: Path) -> dict[str, Any]:
    config = dict(DEFAULT_TG_CONFIG)
    if path.exists():
        raw = hs.load_json(path, label="telegram configuration")
        if not isinstance(raw, dict):
            raise hs.SupervisorError("telegram configuration must be a JSON object")
        config.update(raw)
    if config.get("schema_version") != CONFIG_SCHEMA:
        raise hs.SupervisorError("unsupported telegram configuration schema")
    if config.get("mode") not in MODES:
        raise hs.SupervisorError(f"invalid telegram mode {config.get('mode')!r}")
    for key in ("owner_user_id", "chat_id"):
        value = config.get(key)
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value == 0):
            raise hs.SupervisorError(f"telegram {key} must be a non-zero integer or null")
    if not isinstance(config.get("plain_text_queries"), bool):
        raise hs.SupervisorError("plain_text_queries must be a boolean")
    for key in ("poll_timeout_seconds", "http_timeout_seconds", "backoff_base_seconds", "backoff_cap_seconds", "callback_ttl_seconds", "intent_ttl_seconds", "max_task_chars", "informational_collapse_after", "long_output_chars"):
        number = hs._number(config.get(key))
        if number is None or number <= 0 or number > hs.MAX_EPOCH:
            raise hs.SupervisorError(f"telegram {key} must be a finite positive number")
    if int(config["poll_timeout_seconds"]) > 50 or config["http_timeout_seconds"] <= config["poll_timeout_seconds"]:
        raise hs.SupervisorError("poll_timeout_seconds must be <= 50 and shorter than http_timeout_seconds")
    if not 512 <= int(config["long_output_chars"]) <= hp.CHUNK:
        raise hs.SupervisorError(f"long_output_chars must be between 512 and {hp.CHUNK}")
    replacements = config.get("max_document_replacements")
    if isinstance(replacements, bool) or not isinstance(replacements, int) or not 0 <= replacements <= 3:
        raise hs.SupervisorError("max_document_replacements must be an integer from 0 to 3")
    if config["mode"] != "unconfigured":
        if config.get("owner_user_id") is None or not isinstance(config.get("token_file"), str):
            raise hs.SupervisorError("shadow/actionable mode requires owner_user_id and token_file")
        if config["mode"] == "actionable" and config.get("chat_id") is None:
            raise hs.SupervisorError("actionable mode requires an exact private chat_id")
    try:
        zoneinfo.ZoneInfo(str(config.get("timezone")))
    except (zoneinfo.ZoneInfoNotFoundError, ValueError) as error:
        raise hs.SupervisorError("telegram timezone is unknown") from error
    return config


def check_private_file(path: Path, label: str) -> None:
    try:
        info = path.lstat()
    except OSError as error:
        raise hs.SupervisorError(f"{label} is missing: {path}") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise hs.SupervisorError(f"{label} must be a regular file: {path}")
    if info.st_uid != os.getuid():
        raise hs.SupervisorError(f"{label} must be owned by the current user")
    if info.st_mode & 0o077:
        raise hs.SupervisorError(f"{label} must be mode 0600 (found {oct(info.st_mode & 0o777)})")


def read_token(path: Path) -> str:
    check_private_file(path, "bot token file")
    token = path.read_text(encoding="utf-8").strip()
    if not tg.TOKEN_SHAPE_RE.fullmatch(token):
        raise hs.SupervisorError("bot token file does not contain a token of the expected shape")
    return token


# --------------------------------------------------------------------------- bridge


class Bridge:
    def __init__(
        self,
        *,
        sup_paths: hs.Paths,
        sup_config: dict[str, Any],
        tg_paths: TelegramPaths,
        tg_config: dict[str, Any],
        api: Any,
        bot_id: int,
        status_reader: Callable[[], dict[str, Any]],
        clock: Callable[[], float] = time.time,
        sleeper: Callable[[float], None] = time.sleep,
        rng: Callable[[], float] | None = None,
        reset_inventory_reader: Callable[[], hcr.ResetInventory] | None = None,
    ) -> None:
        self.sup_paths = sup_paths
        self.sup_config = sup_config
        self.tg_paths = tg_paths
        self.config = tg_config
        self.api = api
        self.bot_id = bot_id
        self.status_reader = status_reader
        self.clock = clock
        self.sleeper = sleeper
        self.rng = rng or (lambda: secrets.randbelow(1000) / 1000)
        self.reset_inventory_reader = reset_inventory_reader or (lambda: (_ for _ in ()).throw(hcr.ResetError("inventory reader not configured")))
        self.tz = str(tg_config.get("timezone") or DEFAULT_TIMEZONE)
        # A typed, mutation-free view of supervisor state and a command enqueuer (typed API, no pane control).
        self.store = hs.StateStore(sup_paths, clock)
        self.enqueuer = hs.Supervisor(sup_paths, sup_config, herdr=None, clock=clock)
        self.backoff = 0.0
        self.stopped_reason: str | None = None
        tg_paths.ensure()

    # ----- durable bookkeeping

    def read_offset(self) -> int:
        if not self.tg_paths.offset_file.exists():
            return 0
        value = hs.load_json(self.tg_paths.offset_file, label="telegram offset")
        offset = value.get("next_update_id") if isinstance(value, dict) else None
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise hs.SupervisorError("telegram offset file is invalid")
        return offset

    def write_offset(self, next_update_id: int) -> None:
        current = self.read_offset()
        if next_update_id < current:
            return  # offsets are monotonic
        hs.atomic_write_json(self.tg_paths.offset_file, {"next_update_id": next_update_id, "updated_at": hs.iso_utc(self.clock())})

    def request_id_for(self, update_id: int, suffix: str = "") -> str:
        return hashlib.sha256(f"{self.bot_id}:{update_id}:{suffix}".encode()).hexdigest()[:32]

    def journal_path(self, update_id: int) -> Path:
        return self.tg_paths.updates_dir / f"{update_id}.json"

    # ----- authentication

    def authenticate(self, message_like: dict[str, Any]) -> tuple[bool, str, int | None, int | None]:
        user = message_like.get("from") if isinstance(message_like.get("from"), dict) else {}
        chat = message_like.get("chat") if isinstance(message_like.get("chat"), dict) else {}
        user_id = user.get("id") if isinstance(user.get("id"), int) and not isinstance(user.get("id"), bool) else None
        chat_id = chat.get("id") if isinstance(chat.get("id"), int) and not isinstance(chat.get("id"), bool) else None
        owner = self.config.get("owner_user_id")
        if self.config.get("mode") == "unconfigured" or owner is None:
            return False, "unconfigured", user_id, chat_id
        if user_id != owner:
            return False, "wrong_user", user_id, chat_id
        if chat.get("type") != "private":
            return False, "not_private", user_id, chat_id
        if self.config.get("chat_id") is not None and chat_id != self.config["chat_id"]:
            return False, "wrong_chat", user_id, chat_id
        return True, "ok", user_id, chat_id

    # ----- outbound helpers (durable; ambiguous outcomes are recorded, never blindly retried for actionable cards)

    def send(self, chat_id: int, text: str, *, reply_markup: dict[str, Any] | None = None, html: bool = False) -> dict[str, Any]:
        parts = chunks(text if html else redact(text, limit=100_000))
        result: dict[str, Any] = {}
        for index, part in enumerate(parts):
            result = self.api.send_message(chat_id, part, reply_markup=reply_markup if index == len(parts) - 1 else None, parse_mode="HTML" if html else None)
        return result

    def safe_send(self, chat_id: int | None, text: str, *, reply_markup: dict[str, Any] | None = None, html: bool = False) -> bool:
        if chat_id is None:
            return False
        try:
            self.send(chat_id, text, reply_markup=reply_markup, html=html)
            return True
        except tg.TelegramError:
            return False

    # ----- presentation helpers

    def _keyboard(self, spec: list[list[tuple[str, str, dict[str, Any]]]], *, event: dict[str, Any], gate: dict[str, Any] | None, chat_id: int) -> dict[str, Any] | None:
        rows = []
        for row in spec:
            buttons = []
            for label, action, extra in row:
                token = self.new_interaction(action=action, event=event, gate=gate, chat_id=chat_id, extra=extra or None)
                buttons.append({"text": label[:40], "callback_data": token})
            if buttons:
                rows.append(buttons)
        return {"inline_keyboard": rows} if rows else None

    def _pseudo_event(self, kind: str, *, gate: dict[str, Any] | None = None) -> dict[str, Any]:
        state = self._state() or {}
        return {"event_id": f"{kind}:{secrets.token_hex(6)}", "run_id": state.get("run_id"), "gate_id": (gate or {}).get("gate_id"), "supervisor_state": state.get("supervisor_state") or "NO_TASK"}

    def send_rendered(self, chat_id: int, rendered: hp.Rendered, *, event: dict[str, Any] | None = None, gate: dict[str, Any] | None = None, category: str = "report") -> bool:
        """Short HTML message with state-aware buttons; a long body becomes a registered Markdown document."""
        event = event or self._pseudo_event("render", gate=gate)
        markup = self._keyboard(rendered.keyboard, event=event, gate=gate, chat_id=chat_id) if rendered.keyboard else None
        ok = self.safe_send(chat_id, rendered.html, reply_markup=markup, html=True)
        if ok and rendered.document_markdown:
            try:
                prior = [r for r in ha.list_records(self.sup_paths, event.get("run_id")) if r.get("category") == category and r.get("event_id") == event.get("event_id")]
                record = prior[-1] if prior else ha.register_text(self.sup_paths, self.sup_config, category=category, text=rendered.document_markdown, name=rendered.document_name or category, run_id=event.get("run_id"), event_id=event.get("event_id"), title=rendered.document_title)
            except hs.SupervisorError as error:
                self.safe_send(chat_id, f"The full document could not be prepared safely: {redact(str(error), limit=160)}")
                return ok
            ok = self.send_artifact(chat_id, record["artifact_id"], delivery_key=str(event.get("event_id") or record["artifact_id"]))
        return ok

    def send_artifact(self, chat_id: int, artifact_id: str, *, caption: str | None = None, delivery_key: str | None = None) -> bool:
        """Durably queue and attempt one registered-artifact delivery to the configured owner chat."""
        if self.config.get("chat_id") is not None and chat_id != self.config["chat_id"]:
            return False
        try:
            record, _ = ha.verify_for_send(self.sup_paths, self.sup_config, artifact_id)
        except hs.SupervisorError as error:
            self.safe_send(chat_id, f"Document unavailable: {redact(str(error), limit=160)}")
            return False
        identity = hashlib.sha256(f"{delivery_key or secrets.token_hex(16)}:{chat_id}".encode()).hexdigest()
        delivery_path = self.tg_paths.document_deliveries_dir / f"{identity}.json"
        if delivery_path.exists():
            delivery = hs.load_json(delivery_path, label="document delivery")
            if not isinstance(delivery, dict) or delivery.get("artifact_id") != artifact_id or delivery.get("chat_id") != chat_id:
                return False
        else:
            delivery = {
                "schema_version": 1, "delivery_id": identity, "artifact_id": artifact_id, "chat_id": chat_id,
                "caption": hp.redact(caption or record.get("title") or "", limit=200), "status": "pending",
                "attempts": 0, "replacements": 0, "created_at": hs.iso_utc(self.clock()),
            }
            hs.atomic_write_json(delivery_path, delivery)
        return self._attempt_queued_document(delivery_path, delivery)

    def _attempt_queued_document(self, delivery_path: Path, delivery: dict[str, Any]) -> bool:
        if delivery.get("status") == "delivered":
            return True
        if delivery.get("status") not in ("pending", "delivery_uncertain"):
            return False
        if delivery.get("status") == "delivery_uncertain" and int(delivery.get("replacements") or 0) >= int(self.config.get("max_document_replacements", 1)):
            delivery.update(status="manual_review", reason="document delivery uncertain; replacement budget exhausted", updated_at=hs.iso_utc(self.clock()))
            hs.atomic_write_json(delivery_path, delivery)
            return False
        try:
            record, data = ha.verify_for_send(self.sup_paths, self.sup_config, str(delivery.get("artifact_id")))
        except hs.SupervisorError as error:
            delivery.update(status="failed", reason=redact(str(error), limit=120), updated_at=hs.iso_utc(self.clock()))
            hs.atomic_write_json(delivery_path, delivery)
            return False
        was_uncertain = delivery.get("status") == "delivery_uncertain"
        delivery["attempts"] = int(delivery.get("attempts") or 0) + 1
        if was_uncertain:
            delivery["replacements"] = int(delivery.get("replacements") or 0) + 1
        delivery.update(status="sending", updated_at=hs.iso_utc(self.clock()))
        hs.atomic_write_json(delivery_path, delivery)
        try:
            result = self.api.send_document(int(delivery["chat_id"]), record["display_name"], data, caption=str(delivery.get("caption") or "")[:200])
        except tg.TelegramError as error:
            if tg.delivery_failure_is_permanent(error):
                status = "failed"
            elif error.category in ("HTTPS_FAILURE",):
                status = "delivery_uncertain"
            else:
                status = "pending"
            delivery.update(status=status, reason=error.category, updated_at=hs.iso_utc(self.clock()))
            hs.atomic_write_json(delivery_path, delivery)
            return False
        delivery.update(status="delivered", message_id=result.get("message_id"), file_id=(result.get("document") or {}).get("file_id"), updated_at=hs.iso_utc(self.clock()))
        hs.atomic_write_json(delivery_path, delivery)
        return True

    def deliver_document_queue(self) -> dict[str, int]:
        counts = {"sent": 0, "pending": 0, "manual_review": 0, "failed": 0}
        for path in sorted(self.tg_paths.document_deliveries_dir.glob("*.json")):
            delivery = hs.load_json(path, label="document delivery")
            if not isinstance(delivery, dict):
                counts["failed"] += 1
                continue
            before = delivery.get("status")
            if before == "sending":
                # A daemon crash can occur after the request left the process but before Telegram's reply
                # was persisted. Treat that boundary as ambiguous and permit only the configured bounded
                # replacement behavior.
                delivery.update(status="delivery_uncertain", reason="recovered after interrupted send", updated_at=hs.iso_utc(self.clock()))
                hs.atomic_write_json(path, delivery)
                before = "delivery_uncertain"
            if before in ("pending", "delivery_uncertain"):
                self._attempt_queued_document(path, delivery)
                delivery = hs.load_json(path, label="document delivery")
            after = delivery.get("status") if isinstance(delivery, dict) else "failed"
            if after == "delivered" and before != "delivered":
                counts["sent"] += 1
            elif after in ("pending", "delivery_uncertain"):
                counts["pending"] += 1
            elif after == "manual_review":
                counts["manual_review"] += 1
            elif after == "failed":
                counts["failed"] += 1
        return counts

    def _latest_artifact(self, category: str, run_id: str | None) -> dict[str, Any] | None:
        records = [r for r in ha.list_records(self.sup_paths, run_id) if r.get("category") == category]
        return records[-1] if records else None

    def _gate_document(self, chat_id: int, gate: dict[str, Any], *, category: str) -> dict[str, Any]:
        """Register the gate's authoritative artifact (still hash-bound) and send it as a document."""
        artifact_path = gate.get("artifact_path")
        if not isinstance(artifact_path, str) or not Path(artifact_path).exists():
            self.safe_send(chat_id, "No document is registered for this gate.")
            return {"result": "missing"}
        if hs.sha256_file(Path(artifact_path)) != gate.get("artifact_sha256"):
            self.safe_send(chat_id, hs.PLAN_CHANGED_MESSAGE)
            return {"result": "changed"}
        try:
            record = ha.register_file(self.sup_paths, self.sup_config, category=category, source_path=artifact_path, run_id=gate.get("run_id"), event_id=gate.get("gate_id"), title=Path(artifact_path).name, expected_source_sha256=gate.get("artifact_sha256"))
        except hs.SupervisorError as error:
            self.safe_send(chat_id, f"Document cannot be exposed safely: {redact(str(error), limit=160)}")
            if category == "plan":
                self._disable_remote_approval(gate["gate_id"])
            return {"result": "refused"}
        if record.get("redactions") and category == "plan":
            self.safe_send(chat_id, "Plan details contain material that cannot be displayed safely; remote approval is disabled for this artifact. Review it locally.")
            self._disable_remote_approval(gate["gate_id"])
            return {"result": "redaction_refused"}
        return {"result": "document" if self.send_artifact(chat_id, record["artifact_id"], delivery_key=f"gate:{gate.get('gate_id')}:{category}") else "send_queued"}

    def owner_chat(self) -> int | None:
        return self.config.get("chat_id")

    # ----- update processing

    def poll_once(self) -> int:
        """One getUpdates round. Each update is journaled and its command durably enqueued before the
        offset advances, so a crash at any point re-processes idempotently (same request_id)."""
        offset = self.read_offset()
        updates = self.api.get_updates(offset=offset, timeout_seconds=int(self.config["poll_timeout_seconds"]))
        processed = 0
        for update in sorted(updates, key=lambda u: u.get("update_id", -1)):
            update_id = update.get("update_id")
            if isinstance(update_id, bool) or not isinstance(update_id, int) or update_id < offset:
                continue
            journal = self.journal_path(update_id)
            if journal.exists():
                self.write_offset(update_id + 1)
                continue
            result = self.handle_update(update)
            hs.atomic_write_json(journal, {"update_id": update_id, "at": hs.iso_utc(self.clock()), "result": result})
            self.write_offset(update_id + 1)
            processed += 1
        return processed

    def handle_update(self, update: dict[str, Any]) -> dict[str, Any]:
        update_id = int(update.get("update_id", 0))
        if isinstance(update.get("callback_query"), dict):
            return self.handle_callback(update_id, update["callback_query"])
        message = update.get("message")
        if not isinstance(message, dict):
            return {"ignored": "unsupported update"}
        ok, reason, user_id, chat_id = self.authenticate(message)
        text = message.get("text")
        if not ok:
            return {"rejected": reason, "user_id": user_id}  # no details leak to unknown users/chats
        if isinstance(message.get("document"), dict) and not isinstance(text, str):
            return {"document": True, **self.handle_document(update_id, message, user_id, chat_id)}
        if not isinstance(text, str) or not text.strip():
            return {"ignored": "non-text message"}
        text = _CONTROL_RE.sub("", text).strip()
        if text.startswith("/"):
            command, _, argument = text[1:].partition(" ")
            command = command.split("@", 1)[0].lower()
            return self.handle_command(update_id, command, argument.strip(), user_id, chat_id)
        return self.handle_plain_text(update_id, text, user_id, chat_id)

    # ----- commands

    def handle_command(self, update_id: int, command: str, argument: str, user_id: int, chat_id: int) -> dict[str, Any]:
        if command not in COMMANDS:
            self.safe_send(chat_id, self.help_text())
            return {"command": command, "result": "unknown -> help"}
        handler = getattr(self, f"cmd_{command.replace('-', '_')}")
        return {"command": command, **handler(update_id, argument, user_id, chat_id)}

    def cmd_help(self, update_id: int, argument: str, user_id: int, chat_id: int) -> dict[str, Any]:
        self.safe_send(chat_id, self.help_text())
        return {"result": "help"}

    def help_text(self) -> str:
        return (
            "herdr-supervisor remote control\n"
            "/status – task, stage, agents, quotas, gate\n"
            "/task <text> – start a gated task with Codex (rejected while a task is active)\n"
            "/plan – pending plan summary; /pending – current human gate\n"
            "/pause /resume /cancel – supervisor control (sessions preserved)\n"
            "/logs – recent events; /doctor – health; /backup – backup health (/backup now requests a run)\n"
            "/ask <question> – read-only. State questions are answered without a model; repository questions use the\n"
            "  an eligible configured read-only query session and may consume that provider's quota. /ask never starts or changes a task.\n"
            "/revise <note> – revision for the pending gate; /answer <text> – answer a pending question\n"
            f"mode: {self.config.get('mode')}"
        )

    def cmd_status(self, update_id: int, argument: str, user_id: int, chat_id: int) -> dict[str, Any]:
        status = self.status_reader()
        status["checked_at_unix"] = self.clock()
        if argument.strip().lower() == "raw":
            self.send_rendered(chat_id, hp.render_status_raw(status))
            return {"result": "status_raw"}
        self.send_rendered(chat_id, hp.render_status(status, self.tz), gate=(self._state() or {}).get("pending_gate"))
        return {"result": "status"}

    def render_status(self, status: dict[str, Any]) -> str:
        lines = [f"Supervisor: {status.get('supervisor_state')}"]
        if status.get("task_id"):
            lines += [
                f"Task {str(status.get('task_id'))[:8]}: {redact(status.get('task'), limit=300)}",
                f"Policy: {status.get('workflow_policy')}  Stage: {status.get('phase')}  Active: {status.get('active_agent')}",
            ]
        for provider in ("codex", "claude"):
            agent = (status.get("agents") or {}).get(provider) or {}
            session = (status.get("native_sessions_abbrev") or {}).get(provider) or (agent.get("native_session_id") or "")[:8]
            lines.append(f"{provider}: {agent.get('lifecycle') or 'n/a'} session {session or '?'}")
            quota = (status.get("quota") or {}).get(provider) or {}
            if quota.get("ok"):
                for window in quota.get("windows", []):
                    lines.append(f"  {window['kind']}: {window['remaining_percent']:.0f}% left, resets {fmt_local(window['resets_at'], self.tz)}{' BLOCKING' if window.get('blocking') else ''}")
                if quota.get("context_used_percent") is not None:
                    lines.append(f"  context {quota['context_used_percent']:.0f}% (info only)")
            else:
                lines.append(f"  quota: {redact(quota.get('error'), limit=120)}")
        gate = status.get("pending_gate")
        if gate and gate.get("status") == "pending":
            lines.append(f"Gate: {gate.get('gate_type')} {str(gate.get('gate_id'))[:8]} (state {gate.get('expected_state')})")
        if status.get("candidate_sha"):
            evidence = status.get("runtime_evidence") or {}
            push = status.get("push_approval") or {}
            lines.append(f"Candidate {status['candidate_sha'][:12]}: runtime {evidence.get('result') or 'missing'}; push {'approved' if push.get('candidate_sha') == status['candidate_sha'] else 'not approved'}")
        if status.get("wait_user_reason"):
            lines.append(f"WAIT_USER: {redact(status['wait_user_reason'], limit=300)}")
        last = status.get("last_event") or {}
        if last:
            lines.append(f"Last event: {last.get('type')} #{last.get('sequence')} {last.get('at_utc')}")
        lines.append(f"Time: {fmt_local(self.clock(), self.tz)}")
        return "\n".join(lines)

    def cmd_task(self, update_id: int, argument: str, user_id: int, chat_id: int) -> dict[str, Any]:
        if not argument:
            self.safe_send(chat_id, "Usage: /task <task text>")
            return {"result": "usage"}
        if len(argument) > int(self.config["max_task_chars"]):
            self.safe_send(chat_id, f"Task text exceeds {self.config['max_task_chars']} characters.")
            return {"result": "too_long"}
        state = self._state()
        if state is not None and state.get("supervisor_state") not in hs.TERMINAL_STATES:
            self.safe_send(chat_id, f"A task is still {state['supervisor_state']}; the supervisor has no task queue. Cancel or finish it first.")
            return {"result": "busy"}
        return self._stage_reset_authorization(update_id, user_id, chat_id, {"task_text": argument, "source": "telegram"})

    def _stage_reset_authorization(self, update_id: int, user_id: int, chat_id: int, task: dict[str, Any]) -> dict[str, Any]:
        self.sup_paths.pending_starts_dir.mkdir(parents=True,exist_ok=True,mode=0o700)
        for old in self.sup_paths.pending_starts_dir.glob("*.json"):
            try: previous=hs.load_json(old,label="pending task")
            except hs.SupervisorError: continue
            if previous.get("chat_id")==chat_id and previous.get("status")=="pending":
                previous["status"]="superseded"; hs.atomic_write_json(old,previous)
        pending_id = str(uuid.uuid4())
        inventory_error = None
        try:
            inventory = hcr.ensure_inventory_fresh(
                self.reset_inventory_reader(), now=self.clock(),
                max_age=float(self.sup_config["codex_reset"]["inventory_max_age_seconds"]),
            )
            inventory_count, fingerprint = inventory.available_count, inventory.account_fingerprint
            available = inventory_count if fingerprint else 0
        except (hcr.ResetError, OSError) as error:
            # Inventory failure removes automatic redemption authority, but it must never bypass the
            # human task-start decision. Stage an explicit zero-budget card and preserve the task.
            inventory_count, available, fingerprint = None, 0, None
            inventory_error = type(error).__name__
        record = {"schema_version": 1, "pending_id": pending_id, "owner_user_id": user_id, "chat_id": chat_id,
                  "created_at_unix": self.clock(), "expires_at_unix": self.clock() + float(self.config["callback_ttl_seconds"]),
                  "available_count": available, "inventory_available_count": inventory_count, "account_fingerprint": fingerprint, "task": task,
                  "inventory_status": "unavailable" if inventory_error else "verified", "inventory_error": inventory_error,
                  "task_sha256": hashlib.sha256(json.dumps(task, sort_keys=True).encode()).hexdigest(), "status": "pending"}
        path = self.sup_paths.pending_starts_dir / f"{pending_id}.json"
        if path.exists():
            raise hs.SupervisorError("pending task identity collision")
        hs.atomic_write_json(path, record)
        pseudo = {"event_id": f"reset-auth:{pending_id}", "run_id": pending_id, "supervisor_state": "PENDING_START"}
        buttons=[]
        for budget in range(min(available, 8) + 1):
            token=self.new_interaction(action="reset_budget",event=pseudo,gate=None,chat_id=chat_id,
                                       extra={"pending_id":pending_id,"budget":budget,"available_count":available,
                                              "account_fingerprint":fingerprint,"task_sha256":record["task_sha256"]})
            buttons.append({"text":"Start task · 0 resets" if budget==0 else f"Start task · {budget} reset{'s' if budget != 1 else ''}","callback_data":token})
        rows=[buttons[i:i+4] for i in range(0,len(buttons),4)]
        cancel=self.new_interaction(action="reset_budget_cancel",event=pseudo,gate=None,chat_id=chat_id,
                                    extra={"pending_id":pending_id,"task_sha256":record["task_sha256"]})
        rows.append([{"text":"Cancel","callback_data":cancel}])
        consequence="A Full Reset can refresh both 5-hour and weekly windows and may change the weekly reset date."
        suffix="\nFor a larger value use /reset-budget N." if available>8 else ""
        identity_note="" if fingerprint else "\nAutomatic redemption is unavailable because the account identity could not be verified; choose 0."
        mode_note="\n\nSHADOW: selecting a budget will not start a task." if self.config.get("mode") != "actionable" else ""
        shown_count = str(inventory_count) if inventory_count is not None else "unavailable"
        self.safe_send(chat_id,f"Codex reset policy\n\nBanked resets available: {shown_count}\n\nChoose the reset budget and start this task.\n\n{consequence}{identity_note}{suffix}{mode_note}",reply_markup={"inline_keyboard":rows})
        return {"result":"reset_budget_required","pending_id":pending_id,"available":inventory_count,"authorizable":available}

    def cmd_reset_budget(self, update_id: int, argument: str, user_id: int, chat_id: int) -> dict[str, Any]:
        if not re.fullmatch(r"\d+", argument):
            self.safe_send(chat_id,"Usage: /reset-budget N")
            return {"result":"usage"}
        pending=sorted(self.sup_paths.pending_starts_dir.glob("*.json"),key=lambda p:p.stat().st_mtime,reverse=True)
        if not pending:
            self.safe_send(chat_id,"No task is waiting for a reset budget.")
            return {"result":"none"}
        return self._start_pending_budget(pending[0],int(argument),user_id,chat_id,None,None)

    def _start_pending_budget(self,path:Path,budget:int,user_id:int,chat_id:int,callback_id:str|None,interaction:tuple[Path,dict[str,Any]]|None)->dict[str,Any]:
        record=hs.load_json(path,label="pending task")
        if record.get("status")!="pending" or record.get("owner_user_id")!=user_id or record.get("chat_id")!=chat_id or self.clock()>float(record.get("expires_at_unix",0)):
            if callback_id:self._ack(callback_id,"This reset authorization is no longer valid.")
            return {"rejected":"stale_pending_start"}
        if interaction and (interaction[1].get("task_sha256")!=record.get("task_sha256") or interaction[1].get("pending_id")!=record.get("pending_id")):
            if callback_id:self._ack(callback_id,"Task authorization binding changed.")
            return {"rejected":"task_binding_changed"}
        if isinstance(budget,bool) or not isinstance(budget,int) or budget<0 or budget>int(record.get("available_count",0)):
            if callback_id:self._ack(callback_id,"Budget is outside the displayed range.")
            return {"rejected":"invalid_budget"}
        fingerprint=record.get("account_fingerprint")
        if budget>0:
            try:
                current=hcr.ensure_inventory_fresh(
                    self.reset_inventory_reader(), now=self.clock(),
                    max_age=float(self.sup_config["codex_reset"]["inventory_max_age_seconds"]),
                )
            except (hcr.ResetError,OSError):
                if callback_id:self._ack(callback_id,"Reset inventory cannot be verified; task was not started.")
                return {"rejected":"inventory_unavailable"}
            if current.account_fingerprint!=fingerprint or current.available_count!=record.get("available_count"):
                if callback_id:self._ack(callback_id,"Reset inventory changed; review a new authorization card.")
                return {"rejected":"stale_inventory"}
        task=dict(record["task"])
        command={"request_id":hashlib.sha256(f"pending-start:{record['pending_id']}".encode()).hexdigest()[:32],"action":"task",
                 **task,"preallocated_run_id":record["pending_id"],
                 "pending_start_id":record["pending_id"],"pending_task_sha256":record["task_sha256"],
                 "codex_reset_authorization":{"budget":budget,"available_count":record["available_count"],"account_fingerprint":fingerprint},
                 "actor":f"telegram:{user_id}","chat_id":chat_id,"created_at":hs.iso_utc(self.clock())}
        if self.config.get("mode")!="actionable":
            if callback_id:self._ack(callback_id,"SHADOW: task not started.")
            return {"result":"shadow"}
        self.enqueuer.enqueue_command(command)
        record.update({"status":"start_enqueued","budget":budget,"request_id":command["request_id"]}); hs.atomic_write_json(path,record)
        upload_id = task.get("upload_id")
        if isinstance(upload_id, str):
            meta = self._load_upload(upload_id)
            if meta is not None and meta.get("status") == "reset_authorization":
                meta.update({"status":"start_enqueued","request_id":command["request_id"],"start_enqueued_at":hs.iso_utc(self.clock())})
                meta.pop("excerpt",None)
                hs.atomic_write_json(self._upload_meta_path(upload_id),meta)
                self._supersede_upload_interactions(upload_id)
        if interaction:
            self._consume(interaction[0],interaction[1],request_id=command["request_id"])
        if callback_id:self._ack(callback_id,"Task start received; reset budget recorded.")
        self.safe_send(chat_id,f"Task start received\n\nCodex banked resets available at authorization: {record['available_count']}\nAuthorized for this task: {budget}")
        return {"result":"enqueued","request_id":command["request_id"],"budget":budget}

    def cmd_plan(self, update_id: int, argument: str, user_id: int, chat_id: int) -> dict[str, Any]:
        state = self._state()
        gate = (state or {}).get("pending_gate")
        if not gate or gate.get("status") != "pending" or gate.get("gate_type") != "plan_approval":
            self.safe_send(chat_id, "No pending plan approval.")
            return {"result": "none"}
        self.send_rendered(chat_id, hp.render_plan_approval(gate, self.status_reader(), self.tz), gate=gate)
        if argument.lower() in ("details", "full"):
            return self._send_plan_details(chat_id, gate)
        return {"result": "summary"}

    def _send_plan_details(self, chat_id: int, gate: dict[str, Any]) -> dict[str, Any]:
        """The gate's already-validated artifact is delivered as a registered Markdown document."""
        return self._gate_document(chat_id, gate, category="plan" if gate.get("gate_type") == "plan_approval" else "report")

    def _send_plan_details_legacy_text(self, chat_id: int, gate: dict[str, Any]) -> dict[str, Any]:
        """Send the already-validated artifact only; refuse when redaction would hide material."""
        try:
            path = hs.check_safe_file(gate["artifact_path"], root=Path(self.sup_config["review_root"]), max_bytes=int(self.sup_config["max_artifact_bytes"]), label="plan artifact")
        except hs.SupervisorError as error:
            self.safe_send(chat_id, f"Plan details unavailable: {redact(str(error), limit=200)}")
            return {"result": "unsafe"}
        if hs.sha256_file(path) != gate.get("artifact_sha256"):
            self.safe_send(chat_id, hs.PLAN_CHANGED_MESSAGE)
            return {"result": "changed"}
        raw = path.read_text(encoding="utf-8", errors="replace")
        redacted = redact(raw, limit=200_000)
        if "[redacted]" in redacted:
            self.safe_send(chat_id, "Plan details contain material that cannot be displayed safely; remote approval is disabled for this artifact. Review it locally.")
            self._disable_remote_approval(gate["gate_id"])
            return {"result": "redaction_refused"}
        for part in chunks(redacted):
            self.safe_send(chat_id, part)
        return {"result": "details"}

    def _disable_remote_approval(self, gate_id: str) -> None:
        for record_path in self.tg_paths.interactions_dir.glob("*.json"):
            record = hs.load_json(record_path, label="interaction")
            if isinstance(record, dict) and record.get("gate_id") == gate_id and record.get("action") in ("approve",) and not record.get("consumed"):
                record["superseded"] = True
                record["superseded_reason"] = "redaction refused remote approval"
                hs.atomic_write_json(record_path, record)

    def cmd_pending(self, update_id: int, argument: str, user_id: int, chat_id: int) -> dict[str, Any]:
        state = self._state()
        gate = (state or {}).get("pending_gate")
        if not state or not gate or gate.get("status") != "pending":
            reason = (state or {}).get("wait_user_reason")
            self.safe_send(chat_id, f"No typed human gate is pending.{' WAIT_USER: ' + redact(reason, limit=300) if reason else ''}")
            return {"result": "none"}
        self.send_rendered(chat_id, hp.render_event({"type": {"plan_approval": "PLAN_APPROVAL_REQUIRED", "generic_question": "QUESTION_ASKED", "runtime_validation": "RUNTIME_VALIDATION_READY", "push_approval": "PUSH_APPROVAL_REQUIRED"}[gate["gate_type"]], "run_id": state["run_id"], "gate_id": gate["gate_id"], "data": {}}, self.status_reader(), gate, self.tz), gate=gate)
        return {"result": gate.get("gate_type")}

    def _control(self, update_id: int, action: str, user_id: int, chat_id: int) -> dict[str, Any]:
        # F10: a control is bound to the run observed now; without an applicable task nothing is queued.
        state = self._state()
        if state is None or state.get("supervisor_state") in hs.TERMINAL_STATES:
            self.safe_send(chat_id, f"No applicable task to {action} ({'no task' if state is None else state.get('supervisor_state')}); nothing queued.")
            return {"result": "no_task", "action": action}
        command = {"request_id": self.request_id_for(update_id), "action": action, "run_id": state["run_id"], "actor": f"telegram:{user_id}", "chat_id": chat_id, "source": "telegram", "created_at": hs.iso_utc(self.clock())}
        return self._enqueue_or_shadow(command, chat_id, f"{action} run {state['run_id'][:8]} (native sessions preserved)")

    def cmd_pause(self, update_id: int, argument: str, user_id: int, chat_id: int) -> dict[str, Any]:
        return self._control(update_id, "pause", user_id, chat_id)

    def cmd_resume(self, update_id: int, argument: str, user_id: int, chat_id: int) -> dict[str, Any]:
        return self._control(update_id, "resume", user_id, chat_id)

    def cmd_cancel(self, update_id: int, argument: str, user_id: int, chat_id: int) -> dict[str, Any]:
        return self._control(update_id, "cancel", user_id, chat_id)

    def cmd_logs(self, update_id: int, argument: str, user_id: int, chat_id: int) -> dict[str, Any]:
        events = self.enqueuer.list_events()
        if not events:
            self.safe_send(chat_id, "No events.")
            return {"result": "empty"}
        wanted = 15
        if argument.strip().isdigit():
            wanted = max(1, min(int(argument.strip()), 500))
        selected = events[-wanted:]
        lines = [f"{fmt_local(e.get('at_unix'), self.tz)} #{e.get('sequence')} {e.get('type')}: {redact(json.dumps(e.get('data'), ensure_ascii=False), limit=160)}" for e in selected]
        text = "\n".join(lines)
        if hp.is_long(text, int(self.config["long_output_chars"])):
            markdown = "# Supervisor event log\n\n" + "\n".join(f"- {line}" for line in lines) + "\n"
            self.send_rendered(chat_id, hp.Rendered(f"{hp.title_line('Event log')}\n{len(selected)} events; the full log is attached as Markdown.", document_markdown=markdown, document_name="event-log", document_title="Supervisor event log"), event={"event_id": f"telegram-logs-{self.request_id_for(update_id, 'logs')}", "run_id": (self._state() or {}).get("run_id")}, category="logs")
            return {"result": "logs_document", "count": len(selected)}
        self.safe_send(chat_id, text)
        return {"result": "logs", "count": len(selected)}

    def backup_health(self) -> dict[str, Any]:
        """Health of the optional backup subsystem; never raises, never runs a backup."""
        try:
            config = hb.load_backup_config(hb.backup_config_file(self.sup_paths))
            return hb.health(config, hb.load_backup_state(self.sup_paths), self.clock())
        except (hs.SupervisorError, OSError, ValueError) as error:
            return {"status": "failed", "failure_reason": f"backup configuration unreadable: {error}", "strategy": None, "protects": [], "not_protected": []}

    def cmd_backup(self, update_id: int, argument: str, user_id: int, chat_id: int) -> dict[str, Any]:
        health = self.backup_health()
        if argument.strip().lower() == "now":
            if health.get("status") == "disabled":
                self.safe_send(chat_id, "Backups are disabled; nothing to run.")
                return {"result": "backup_disabled"}
            if self.config.get("mode") != "actionable":
                self.safe_send(chat_id, "SHADOW: would request a backup run; nothing changed.")
                return {"result": "shadow"}
            hb.request_backup(self.sup_paths)
            self.safe_send(chat_id, "Backup run requested; the next scheduled backup timer tick will run it and report health.")
            return {"result": "backup_requested"}
        self.send_rendered(chat_id, hp.render_backup_health(health, self.tz), event={"run_id": "backup", "expires_at_unix": self.clock() + 86400}, category="report")
        return {"result": "backup_health", "status": health.get("status")}

    def cmd_doctor(self, update_id: int, argument: str, user_id: int, chat_id: int) -> dict[str, Any]:
        status = self.status_reader()
        summary = doctor_summary(network=False, tg_paths=self.tg_paths)
        agents = status.get("agents") or {}
        lines = [
            f"Supervisor: {status.get('supervisor_state')} (errors: {len(status.get('errors') or [])})",
            f"Codex: detected={agents.get('codex', {}).get('detected')} match={agents.get('codex', {}).get('session_matches')}",
            f"Claude: detected={agents.get('claude', {}).get('detected')} match={agents.get('claude', {}).get('session_matches')}",
            f"Telegram: {summary.get('status')} mode={self.config.get('mode')}",
            f"Backup: {self.backup_health().get('status')}",
        ]
        self.safe_send(chat_id, "\n".join(lines))
        return {"result": "doctor"}

    def cmd_ask(self, update_id: int, argument: str, user_id: int, chat_id: int) -> dict[str, Any]:
        return self._query(update_id, argument, user_id, chat_id, source="/ask")

    def handle_plain_text(self, update_id: int, text: str, user_id: int, chat_id: int) -> dict[str, Any]:
        intent = self._live_intent(chat_id)
        if intent is not None:
            return self._consume_intent(update_id, intent, text, user_id, chat_id)
        if not self.config.get("plain_text_queries"):
            self.safe_send(chat_id, "Plain text is disabled. Use /ask <question> for read-only questions or /task <text> to request an action.")
            return {"result": "plain_text_disabled"}
        return self._query(update_id, text, user_id, chat_id, source="text")

    def _query(self, update_id: int, question: str, user_id: int, chat_id: int, *, source: str) -> dict[str, Any]:
        if not question:
            self.safe_send(chat_id, "Usage: /ask <question>")
            return {"result": "usage"}
        limit = int(self.sup_config["query"]["max_question_chars"])
        if len(question) > limit:
            self.safe_send(chat_id, f"Question exceeds {limit} characters.")
            return {"result": "too_long"}
        kind, detail = herdr_query.classify_question(question)
        if kind == "refuse":
            self.safe_send(chat_id, herdr_query.REFUSAL_TEXT)
            return {"result": "refused", "reason": detail}
        if kind == "state":
            self.safe_send(chat_id, herdr_query.answer_state_intent(detail, self.status_reader(), timezone=self.tz))
            return {"result": "state", "intent": detail}
        if not self._rate_ok():
            self.safe_send(chat_id, "Model-backed query rate limit reached; try later. State questions remain available.")
            return {"result": "rate_limited"}
        if self.config.get("mode") != "actionable":
            self.safe_send(chat_id, f"SHADOW: would select an eligible configured read-only query provider; no provider is selected until execution. Question: {redact(question, limit=300)}")
            return {"result": "shadow_query"}
        request = {"request_id": self.request_id_for(update_id, "q"), "question": question, "chat_id": chat_id, "user_id": user_id, "source": source, "created_at": hs.iso_utc(self.clock())}
        herdr_query.enqueue_query(self.sup_paths, request)
        self.safe_send(chat_id, "Read-only question queued. The worker will select a safe available query provider; the answer will identify the provider and any failover.")
        return {"result": "model_query_enqueued"}

    def _rate_ok(self) -> bool:
        limit = int(self.sup_config["query"]["rate_limit_per_hour"])
        now = self.clock()
        stamps: list[float] = []
        if self.tg_paths.rate_file.exists():
            value = hs.load_json(self.tg_paths.rate_file, label="query rate")
            stamps = [float(x) for x in value.get("stamps", []) if hs._number(x) is not None] if isinstance(value, dict) else []
        stamps = [x for x in stamps if now - x < 3600]
        if len(stamps) >= limit:
            return False
        stamps.append(now)
        hs.atomic_write_json(self.tg_paths.rate_file, {"stamps": stamps})
        return True

    def cmd_revise(self, update_id: int, argument: str, user_id: int, chat_id: int) -> dict[str, Any]:
        state = self._state()
        gate = (state or {}).get("pending_gate")
        typed_gate = gate if isinstance(gate, dict) and gate.get("status") == "pending" else None
        action_wait = bool(state and state.get("supervisor_state") == "WAIT_USER" and state.get("wait_user_requires_action") and typed_gate is None)
        if not state or (typed_gate is None and not action_wait):
            self.safe_send(chat_id, "No pending gate or action-required wait to revise.")
            return {"result": "none"}
        gate_id = typed_gate["gate_id"] if typed_gate is not None else "-"
        expected_state = typed_gate.get("expected_state") if typed_gate is not None else "WAIT_USER"
        if not argument:
            self._set_intent(chat_id, {"kind": "revise", "run_id": state["run_id"], "gate_id": gate_id, "expected_state": expected_state})
            self.safe_send(chat_id, "Reply with the revision note (one message).")
            return {"result": "intent"}
        command = {"request_id": self.request_id_for(update_id), "action": "revise", "run_id": state["run_id"], "gate_id": gate_id, "expected_state": expected_state, "note": argument, "actor": f"telegram:{user_id}", "chat_id": chat_id, "source": "telegram", "created_at": hs.iso_utc(self.clock())}
        return self._enqueue_or_shadow(command, chat_id, "request a revision of the pending gate")

    def cmd_answer(self, update_id: int, argument: str, user_id: int, chat_id: int) -> dict[str, Any]:
        state = self._state()
        gate = (state or {}).get("pending_gate")
        if not state or not gate or gate.get("status") != "pending" or gate.get("gate_type") != "generic_question":
            self.safe_send(chat_id, "No pending question.")
            return {"result": "none"}
        if not argument:
            self.safe_send(chat_id, "Usage: /answer <text>")
            return {"result": "usage"}
        command = {"request_id": self.request_id_for(update_id), "action": "answer", "run_id": state["run_id"], "gate_id": gate["gate_id"], "expected_state": state["supervisor_state"], "answer": argument, "actor": f"telegram:{user_id}", "chat_id": chat_id, "source": "telegram", "created_at": hs.iso_utc(self.clock())}
        return self._enqueue_or_shadow(command, chat_id, "answer the pending question")

    # ----- reply intents (bounded, one-time)

    def _intent_path(self, chat_id: int) -> Path:
        return self.tg_paths.intents_dir / f"{chat_id}.json"

    def _set_intent(self, chat_id: int, intent: dict[str, Any]) -> None:
        hs.atomic_write_json(self._intent_path(chat_id), {**intent, "chat_id": chat_id, "expires_at_unix": self.clock() + float(self.config["intent_ttl_seconds"])})

    def _live_intent(self, chat_id: int) -> dict[str, Any] | None:
        path = self._intent_path(chat_id)
        if not path.exists():
            return None
        intent = hs.load_json(path, label="reply intent")
        with contextlib.suppress(FileNotFoundError):
            path.unlink()  # one-time
        if not isinstance(intent, dict) or self.clock() > float(intent.get("expires_at_unix") or 0):
            return None
        return intent

    def _consume_intent(self, update_id: int, intent: dict[str, Any], text: str, user_id: int, chat_id: int) -> dict[str, Any]:
        action = "revise" if intent.get("kind") == "revise" else "answer"
        command = {"request_id": self.request_id_for(update_id), "action": action, "run_id": intent.get("run_id"), "gate_id": intent.get("gate_id"), "expected_state": intent.get("expected_state"), "actor": f"telegram:{user_id}", "chat_id": chat_id, "source": "telegram", "created_at": hs.iso_utc(self.clock())}
        command["note" if action == "revise" else "answer"] = text
        return self._enqueue_or_shadow(command, chat_id, f"{action} via reply")

    # ----- callbacks (opaque one-time tokens)

    def handle_callback(self, update_id: int, callback: dict[str, Any]) -> dict[str, Any]:
        callback_id = str(callback.get("id", ""))
        message = callback.get("message") if isinstance(callback.get("message"), dict) else {}
        probe = {"from": callback.get("from"), "chat": message.get("chat")}
        ok, reason, user_id, chat_id = self.authenticate(probe)
        data = callback.get("data")
        if not ok:
            self._ack(callback_id, "Not authorized.")
            return {"rejected": reason}
        if not isinstance(data, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,63}", data):
            self._ack(callback_id, "Unknown action.")
            return {"rejected": "malformed_callback"}
        record_path = self.tg_paths.interactions_dir / f"{data}.json"
        if not record_path.exists():
            self._ack(callback_id, "This button is no longer valid.")
            return {"rejected": "unknown_token"}
        record = hs.load_json(record_path, label="interaction")
        problem = self._validate_record(record, user_id, chat_id)
        if problem:
            self._ack(callback_id, problem)
            return {"rejected": problem}
        action = record["action"]
        if action == "reset_budget_cancel":
            pending_id = record.get("pending_id")
            path = self.sup_paths.pending_starts_dir / f"{pending_id}.json"
            if not isinstance(pending_id, str) or not path.exists():
                self._ack(callback_id, "This task authorization is no longer valid.")
                return {"rejected": "missing_pending_start"}
            pending = hs.load_json(path, label="pending task")
            if pending.get("status") != "pending" or pending.get("task_sha256") != record.get("task_sha256"):
                self._ack(callback_id, "This task authorization is no longer valid.")
                return {"rejected": "stale_pending_start"}
            pending.update({"status": "cancelled", "cancelled_at": hs.iso_utc(self.clock())})
            hs.atomic_write_json(path, pending)
            upload_id = (pending.get("task") or {}).get("upload_id")
            if isinstance(upload_id, str):
                meta = self._load_upload(upload_id)
                if meta is not None and meta.get("status") == "reset_authorization":
                    self._finish_upload(meta, "cancelled", delete_content=True)
            self._consume(record_path, record)
            self._ack(callback_id, "Task start cancelled.")
            self.safe_send(chat_id, "Task start cancelled. No supervised run was created.")
            return {"callback": "reset_budget_cancel", "result": "cancelled"}
        if action == "reset_budget":
            pending_id = record.get("pending_id")
            path = self.sup_paths.pending_starts_dir / f"{pending_id}.json"
            if not isinstance(pending_id, str) or not path.exists():
                self._ack(callback_id, "This task authorization is no longer valid.")
                return {"rejected": "missing_pending_start"}
            return {"callback":"reset_budget", **self._start_pending_budget(path, record.get("budget"), user_id, chat_id, callback_id, (record_path, record))}
        if action in ("upload_start", "upload_cancel"):
            return self.handle_upload_callback(update_id, callback_id, record_path, record, user_id, chat_id)
        if action == "backup_now":
            # Backup requests are independent of task state: consume the token and leave a request marker; the
            # timer-driven backup command picks it up. Nothing runs inside the bridge.
            self._consume(record_path, record)
            if self.config.get("mode") != "actionable":
                self._ack(callback_id, "SHADOW: would request a backup; nothing changed.")
                return {"callback": "backup_now", "result": "shadow"}
            hb.request_backup(self.sup_paths)
            self._ack(callback_id, "Backup run requested.")
            return {"callback": "backup_now", "result": "backup_requested"}
        # F10: every callback is revalidated against live run/gate/state/artifact before any action,
        # early return, or shadow preview; a card from an earlier run or a changed artifact is inert.
        problem = self._revalidate_against_state(record)
        if problem:
            self._ack(callback_id, problem)
            return {"rejected": problem}
        # F7: every successfully handled token is one-time, including read-only, intent, and shadow paths.
        if action == "status":
            self._consume(record_path, record)
            self._ack(callback_id, "Status follows.")
            status = self.status_reader()
            status["checked_at_unix"] = self.clock()
            self.send_rendered(chat_id, hp.render_status(status, self.tz), gate=(self._state() or {}).get("pending_gate"))
            return {"callback": "status"}
        if action == "keep_waiting":
            self._consume(record_path, record)
            self._ack(callback_id, "Still waiting; nothing changed.")
            return {"callback": "keep_waiting"}
        if action == "view_details":
            self._consume(record_path, record)
            self._ack(callback_id, "Sending the document.")
            return {"callback": "view_details", **self._send_plan_details(chat_id, self._state_gate_or(record))}
        if action == "view_report":
            self._consume(record_path, record)
            self._ack(callback_id, "Sending the report.")
            report = self._latest_artifact("final_report", record.get("run_id"))
            if report is None:
                self.safe_send(chat_id, "No final report is registered for this run yet.")
                return {"callback": "view_report", "result": "missing"}
            return {"callback": "view_report", "result": "document" if self.send_artifact(chat_id, report["artifact_id"], delivery_key=f"view-report:{data}") else "send_queued"}
        if action == "revise":
            self._consume(record_path, record)
            self._set_intent(chat_id, {"kind": "revise", "run_id": record["run_id"], "gate_id": record["gate_id"], "expected_state": record["expected_state"]})
            self._ack(callback_id, "Reply with the revision note.")
            self.safe_send(chat_id, "Reply with the revision note (one message), or /revise <note>.")
            return {"callback": "revise_intent"}
        command = {"request_id": self.request_id_for(update_id, data), "action": action, "actor": f"telegram:{user_id}", "chat_id": chat_id, "source": "telegram", "callback_query_id": callback_id, "interaction_id": data, "created_at": hs.iso_utc(self.clock())}
        if action in ("approve", "reject", "answer"):
            command.update(run_id=record["run_id"], gate_id=record["gate_id"], expected_state=record["expected_state"], artifact_sha256=record.get("artifact_sha256"), payload_sha256=record.get("payload_sha256"))
            if action == "answer":
                command["answer"] = record.get("choice")
        if action in ("pause", "cancel", "resume", "refresh_quota"):
            command["run_id"] = record.get("run_id")
        if self.config.get("mode") != "actionable":
            self._consume(record_path, record, note="shadow")
            self._ack(callback_id, f"SHADOW: would {action}; no state changed.")
            return {"callback": action, "result": "shadow"}
        # Durable command first, then one-time consumption of the token; a crash in between re-enqueues the same request_id.
        self.enqueuer.enqueue_command(command)
        self._consume(record_path, record, request_id=command["request_id"])
        self._ack(callback_id, "Request received; the supervisor will confirm separately.")
        return {"callback": action, "result": "enqueued", "request_id": command["request_id"]}

    def _consume(self, record_path: Path, record: dict[str, Any], **extra: Any) -> None:
        record["consumed"] = True
        record["consumed_at"] = hs.iso_utc(self.clock())
        record.update(extra)
        hs.atomic_write_json(record_path, record)

    def _state_gate_or(self, record: dict[str, Any]) -> dict[str, Any]:
        state = self._state() or {}
        gate = state.get("pending_gate") or {}
        return gate if gate.get("gate_id") == record.get("gate_id") else {"artifact_path": "/", "artifact_sha256": None, "gate_id": record.get("gate_id")}

    def _validate_record(self, record: Any, user_id: int, chat_id: int) -> str | None:
        if not isinstance(record, dict):
            return "This button is no longer valid."
        if record.get("owner_user_id") != user_id or record.get("chat_id") != chat_id:
            return "Not authorized for this action."
        if record.get("consumed"):
            return "Already used (one-time action)."
        if record.get("superseded"):
            return "Superseded; use the newest card."
        if self.clock() > float(record.get("expires_at_unix") or 0):
            return "Expired."
        return None

    def _revalidate_against_state(self, record: dict[str, Any]) -> str | None:
        state = self._state()
        if state is None:
            return "No task."
        if state.get("run_id") != record.get("run_id"):
            return "This card belongs to an earlier run; use the newest card."
        if record.get("action") == "revise" and record.get("gate_id") == "-":
            if state.get("supervisor_state") != "WAIT_USER" or not state.get("wait_user_requires_action"):
                return "This guidance request is no longer valid; use the newest card or /status."
            if state.get("supervisor_state") != record.get("expected_state"):
                return f"State changed ({state.get('supervisor_state')}); use the newest card."
            return None
        if record.get("action") in ("pause", "cancel", "status", "keep_waiting", "resume", "refresh_quota", "view_report"):
            if state.get("supervisor_state") in hs.TERMINAL_STATES and record.get("action") not in ("status", "view_report"):
                return f"Task is already {state.get('supervisor_state')}."
            if not record.get("expected_state"):
                return "This card is not bound to a supervisor state; use the newest card or /status."
            if state.get("supervisor_state") != record["expected_state"]:
                return f"State changed ({state.get('supervisor_state')}); use the newest card or /status."
            return None
        gate = state.get("pending_gate") or {}
        if state.get("run_id") != record.get("run_id") or gate.get("gate_id") != record.get("gate_id") or gate.get("status") != "pending":
            return "Gate superseded or already resolved."
        if state.get("supervisor_state") != record.get("expected_state"):
            return f"State changed ({state.get('supervisor_state')}); use the newest card."
        if record.get("artifact_sha256") and gate.get("artifact_sha256") != record["artifact_sha256"]:
            return hs.PLAN_CHANGED_MESSAGE
        if record.get("artifact_sha256") and Path(str(gate.get("artifact_path"))).exists() and hs.sha256_file(Path(gate["artifact_path"])) != record["artifact_sha256"]:
            return hs.PLAN_CHANGED_MESSAGE
        return None

    def _ack(self, callback_id: str, text: str) -> None:
        if not callback_id:
            return
        with contextlib.suppress(tg.TelegramError):
            self.api.answer_callback_query(callback_id, text)

    # ----- uploaded task files (.md / .txt): two-phase, no run before Start Task

    @property
    def task_files_dir(self) -> Path:
        return self.sup_paths.task_files_dir

    def upload_id_for(self, update_id: int) -> str:
        return hashlib.sha256(f"herdr-upload:{self.bot_id}:{update_id}".encode()).hexdigest()[:32]

    def _upload_meta_path(self, upload_id: str) -> Path:
        return self.task_files_dir / f"{upload_id}.json"

    def _load_upload(self, upload_id: str) -> dict[str, Any] | None:
        """Validated upload record bound to the trusted id, or None when absent/corrupt (never trusted)."""
        if not isinstance(upload_id, str) or not hs._UPLOAD_ID_RE.match(upload_id):
            return None
        meta_path = self._upload_meta_path(upload_id)
        if not meta_path.exists():
            return None
        try:
            return hs.validate_upload_record(hs.load_json(meta_path, label="upload record"), expected_upload_id=upload_id, task_files_dir=self.task_files_dir, config=self.sup_config)
        except hs.SupervisorError:
            return None

    def _task_snapshot(self) -> dict[str, Any]:
        state = self._state()
        if state is None:
            return {"task_id": None, "state": "NO_TASK"}
        return {"task_id": state.get("task_id"), "state": state.get("supervisor_state")}

    def _no_active_task(self, snapshot: dict[str, Any]) -> bool:
        return snapshot["state"] == "NO_TASK" or snapshot["state"] in hs.TERMINAL_STATES

    @staticmethod
    def _display_filename(raw: Any) -> str:
        name = str(raw or "")
        name = name.replace("\\", "/").rsplit("/", 1)[-1]  # basename only; never used as a path
        name = _CONTROL_RE.sub("", name)
        name = _FILENAME_RE.sub("_", name).strip()
        suffix = Path(name).suffix.lower() if "." in name else ""
        stem = name[: -len(suffix)] if suffix else name
        return (stem[:72].strip() or "task") + suffix

    def handle_document(self, update_id: int, message: dict[str, Any], user_id: int, chat_id: int) -> dict[str, Any]:
        """Validate metadata before any network access, download within the bound, stage atomically,
        then send a preview with one-time Start Task / Cancel buttons. No supervisor run exists yet."""
        document = message["document"]
        max_bytes = int(self.sup_config["max_task_file_bytes"])
        suffixes = [x.lower() for x in self.sup_config["task_file_suffixes"]]
        display = self._display_filename(document.get("file_name"))
        suffix = Path(display).suffix.lower()
        if suffix not in suffixes:
            self.safe_send(chat_id, f"Unsupported task file type; only {', '.join(suffixes)} are accepted.")
            return {"result": "unsupported_type"}
        # Telegram's declared MIME is advisory only for an allowed .md/.txt filename (the platform labels
        # .md as e.g. text/x-web-markdown or application/octet-stream). Content is still bounded and must
        # pass the strict UTF-8 / NUL / control checks after download; the MIME is recorded as metadata.
        mime = document.get("mime_type")
        declared_mime = mime.split(";", 1)[0].strip().lower()[:80] if isinstance(mime, str) else None
        declared = document.get("file_size")
        if isinstance(declared, bool) or not isinstance(declared, int) or declared <= 0:
            self.safe_send(chat_id, "Task file size is unknown or empty; upload a non-empty .md/.txt file.")
            return {"result": "bad_size"}
        if declared > max_bytes:
            self.safe_send(chat_id, f"Task file too large ({declared} bytes; limit {max_bytes}). Nothing was downloaded.")
            return {"result": "oversize_declared"}
        file_id = document.get("file_id")
        if not isinstance(file_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,256}", file_id):
            self.safe_send(chat_id, "Task file reference is malformed.")
            return {"result": "bad_file_id"}
        snapshot = self._task_snapshot()
        if not self._no_active_task(snapshot):
            self.safe_send(chat_id, f"A task is still {snapshot['state']}; the supervisor has no task queue. Nothing was downloaded.")
            return {"result": "busy"}
        if self.config.get("mode") != "actionable":
            self.safe_send(chat_id, f"SHADOW: would download and preview task file '{display}' ({declared} bytes); nothing downloaded, no task.")
            return {"result": "shadow"}
        upload_id = self.upload_id_for(update_id)
        meta_path = self._upload_meta_path(upload_id)
        if meta_path.exists():
            # replay of an already-recorded upload: reuse the validated record, never re-download
            meta = self._load_upload(upload_id)
            if meta is None:
                self.safe_send(chat_id, "Upload record is corrupt; upload the file again.")
                return {"result": "corrupt_record", "upload_id": upload_id}
            if meta["status"] == "pending_confirmation":
                self._require_preview_sent(meta, chat_id)
            return {"result": "replayed", "upload_id": upload_id, "status": meta["status"]}
        try:
            file_path = self.api.get_file(file_id)
            raw = self.api.download_file(file_path, max_bytes=max_bytes)
        except tg.TelegramError as error:
            self.safe_send(chat_id, f"Task file download failed ({error.category}); nothing staged. Try again later.")
            return {"result": "download_failed", "category": error.category}
        try:
            text = hs.validate_task_text(raw, max_bytes=max_bytes, label="task file")
        except hs.SupervisorError as error:
            self.safe_send(chat_id, f"Task file rejected: {redact(str(error), limit=200)}")
            return {"result": "invalid_content"}
        digest = hs.sha256_bytes(raw)
        try:
            content_path = self._publish_upload_content(upload_id, suffix, raw)
        except hs.SupervisorError as error:
            self.safe_send(chat_id, f"Task file could not be staged safely: {redact(str(error), limit=200)}")
            return {"result": "stage_failed"}
        self._supersede_pending_uploads(chat_id, except_upload=upload_id)
        now = self.clock()
        meta = {
            "schema_version": 1, "upload_id": upload_id, "update_id": update_id, "owner_user_id": user_id, "chat_id": chat_id,
            "file_id": file_id, "file_unique_id": document.get("file_unique_id") if isinstance(document.get("file_unique_id"), str) else None, "declared_mime": declared_mime,
            "display_filename": display, "suffix": suffix, "declared_size": declared, "bytes": len(raw), "chars": len(text), "sha256": digest,
            "content_path": str(content_path), "status": "pending_confirmation", "created_at": hs.iso_utc(now),
            "expires_at_unix": now + float(self.config["callback_ttl_seconds"]), "snapshot": snapshot, "excerpt": redact(text[:400], limit=420),
            "interactions": {}, "request_id": None,
        }
        hs.atomic_write_json(meta_path, meta)
        self._require_preview_sent(meta, chat_id)
        return {"result": "staged", "upload_id": upload_id, "bytes": len(raw)}

    def _require_preview_sent(self, meta: dict[str, Any], chat_id: int) -> None:
        """F1: an unconfirmed preview send must not finalize the update. Raising here leaves the journal
        unwritten and the offset unadvanced, so the same update is retried after network recovery; the
        staged content and record are reused (no second download) and only the newest pair is live."""
        if not self._send_upload_preview(meta, chat_id):
            raise tg.TelegramError("HTTPS_FAILURE", "task-file preview could not be delivered; update left unfinalized for retry")

    def _publish_upload_content(self, upload_id: str, suffix: str, raw: bytes) -> Path:
        """Exclusive hidden temp -> fsync -> no-replace publish (hard link) -> unlink temp."""
        directory = self.task_files_dir
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(directory, 0o700)
        final = directory / f"{upload_id}{suffix}"
        tmp = directory / f".{upload_id}.{os.getpid()}.tmp"
        max_bytes = int(self.sup_config["max_task_file_bytes"])
        if len(raw) > max_bytes:
            raise hs.SupervisorError("task file exceeds max_task_file_bytes")
        if final.exists():
            existing = final.read_bytes() if final.is_file() and not final.is_symlink() else b""
            if hs.sha256_bytes(existing) == hs.sha256_bytes(raw):
                return final  # crash after publication: identical immutable content is reused
            raise hs.SupervisorError("a different task file already exists for this upload id; refusing to overwrite")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            os.link(tmp, final)  # fails if `final` appeared meanwhile: never replaces existing content
            dir_fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except FileExistsError as error:
            raise hs.SupervisorError("task file already exists; refusing to overwrite") from error
        finally:
            with contextlib.suppress(FileNotFoundError):
                tmp.unlink()
        os.chmod(final, 0o600)
        return final

    def _send_upload_preview(self, meta: dict[str, Any], chat_id: int) -> bool:
        text = self.render_upload_preview(meta)
        pseudo_event = {"event_id": f"upload:{meta['upload_id']}", "run_id": None, "supervisor_state": meta["snapshot"]["state"]}
        extra = {"upload_id": meta["upload_id"], "sha256": meta["sha256"], "bytes": meta["bytes"], "content_path": meta["content_path"], "snapshot": meta["snapshot"], "mode": self.config.get("mode")}
        start = self.new_interaction(action="upload_start", event=pseudo_event, gate=None, chat_id=chat_id, extra=extra)
        cancel = self.new_interaction(action="upload_cancel", event=pseudo_event, gate=None, chat_id=chat_id, extra=extra)
        self._supersede_upload_interactions(meta["upload_id"], keep={start, cancel})
        meta["interactions"] = {"start": start, "cancel": cancel}
        hs.atomic_write_json(self._upload_meta_path(meta["upload_id"]), meta)
        markup = {"inline_keyboard": [[{"text": "Start Task", "callback_data": start}, {"text": "Cancel", "callback_data": cancel}]]}
        return self.safe_send(chat_id, text, reply_markup=markup)

    def render_upload_preview(self, meta: dict[str, Any]) -> str:
        return "\n".join([
            f"Task file received: {redact(meta.get('display_filename'), limit=80)} ({meta.get('suffix')})",
            f"Size: {meta.get('bytes')} bytes, {meta.get('chars')} characters; SHA-256 {str(meta.get('sha256'))[:12]}",
            f"Upload {str(meta.get('upload_id'))[:8]}; expires {fmt_local(meta.get('expires_at_unix'), self.tz)}",
            "Excerpt:",
            redact(meta.get("excerpt"), limit=420),
            "",
            "No task has started. Start Task submits this file as ONE gated task: Codex plans first, then the plan needs your approval before any implementation.",
        ])

    def _supersede_upload_interactions(self, upload_id: str, *, keep: set[str] | None = None) -> None:
        for record_path in self.tg_paths.interactions_dir.glob("*.json"):
            record = hs.load_json(record_path, label="interaction")
            if not isinstance(record, dict) or record.get("upload_id") != upload_id or record.get("consumed") or record.get("superseded"):
                continue
            if keep and record.get("token") in keep:
                continue
            record["superseded"] = True
            hs.atomic_write_json(record_path, record)

    def _finish_upload(self, meta: dict[str, Any], status: str, *, delete_content: bool) -> None:
        # `meta` is always a validated record: its id is hex32 and equals the trusted filename stem, and
        # its content path is exactly <task_files_dir>/<id><suffix>. Paths are rebuilt from those values.
        upload_id = hs.validate_upload_record(meta, expected_upload_id=meta.get("upload_id"), task_files_dir=self.task_files_dir, config=self.sup_config)["upload_id"]
        if delete_content:
            content = self.task_files_dir / f"{upload_id}{meta['suffix']}"
            if content.is_file() and not content.is_symlink():
                content.unlink()
        meta["status"] = status
        meta["finished_at"] = hs.iso_utc(self.clock())
        meta.pop("excerpt", None)
        hs.atomic_write_json(self.task_files_dir / f"{upload_id}.json", meta)
        self._supersede_upload_interactions(upload_id)

    def _supersede_pending_uploads(self, chat_id: int, *, except_upload: str) -> int:
        """One pending upload per authorized chat: an older unstarted upload becomes inert and its content is removed."""
        count = 0
        if not self.task_files_dir.exists():
            return 0
        for meta_path in self.task_files_dir.glob("*.json"):
            meta = self._load_upload(meta_path.stem)
            if meta is None or meta["upload_id"] == except_upload or meta["chat_id"] != chat_id or meta["status"] != "pending_confirmation":
                continue  # corrupt records are never acted on
            self._finish_upload(meta, "superseded", delete_content=True)
            count += 1
        return count

    def reap_uploads(self) -> int:
        count = 0
        if not self.task_files_dir.exists():
            return 0
        now = self.clock()
        for meta_path in self.task_files_dir.glob("*.json"):
            meta = self._load_upload(meta_path.stem)
            if meta is not None and meta["status"] == "pending_confirmation" and now > float(meta["expires_at_unix"]):
                self._finish_upload(meta, "expired", delete_content=True)
                count += 1
        return count

    def _validate_upload_record(self, record: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
        """Live revalidation of a Start/Cancel token: record, mode, upload status, staged file, size, hash, task snapshot."""
        upload_id = record.get("upload_id")
        if not isinstance(upload_id, str) or not hs._UPLOAD_ID_RE.match(upload_id):
            return None, "Unknown upload."
        if not self._upload_meta_path(upload_id).exists():
            return None, "Upload record no longer exists."
        meta = self._load_upload(upload_id)
        if meta is None:
            return None, "Upload record is corrupt; refusing."
        if meta["status"] != "pending_confirmation":
            return None, f"Upload is already {meta['status']}."
        if meta["chat_id"] != record.get("chat_id") or meta["owner_user_id"] != record.get("owner_user_id"):
            return None, "Not authorized for this upload."
        if record.get("token") not in meta["interactions"].values():
            return None, "Superseded; use the newest upload card."
        if self.clock() > float(meta["expires_at_unix"]):
            return None, "Upload expired; upload the file again."
        max_bytes = int(self.sup_config["max_task_file_bytes"])
        content = self.task_files_dir / f"{upload_id}{meta['suffix']}"
        try:
            info = content.lstat()
        except OSError:
            return None, "Staged task file is missing or unsafe."
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_size > max_bytes:
            return None, "Staged task file is missing or unsafe."
        with content.open("rb") as handle:
            raw = handle.read(max_bytes + 1)
        if len(raw) != meta["bytes"] or len(raw) > max_bytes or hs.sha256_bytes(raw) != meta["sha256"] or record.get("sha256") != meta["sha256"] or record.get("bytes") != meta["bytes"]:
            return None, "Staged task file changed; refusing."
        return meta, None

    def handle_upload_callback(self, update_id: int, callback_id: str, record_path: Path, record: dict[str, Any], user_id: int, chat_id: int) -> dict[str, Any]:
        meta, problem = self._validate_upload_record(record)
        if problem:
            self._ack(callback_id, problem)
            return {"rejected": problem}
        action = record["action"]
        if action == "upload_cancel":
            self._finish_upload(meta, "cancelled", delete_content=True)  # consumes both buttons
            self._consume(record_path, record)
            self._ack(callback_id, "Upload cancelled; no task was started.")
            self.safe_send(chat_id, f"Upload {meta['upload_id'][:8]} cancelled and removed. No supervisor task was started.")
            return {"callback": "upload_cancel", "upload_id": meta["upload_id"]}
        # Start Task
        if self.config.get("mode") != "actionable":
            self._ack(callback_id, "SHADOW: would start the task; nothing changed.")
            return {"rejected": "shadow mode"}
        snapshot = self._task_snapshot()
        if not self._no_active_task(snapshot) or snapshot != meta.get("snapshot"):
            self._ack(callback_id, f"Task state changed ({snapshot['state']}); this upload is inert. Upload again when no task is active.")
            self._finish_upload(meta, "superseded", delete_content=True)
            return {"rejected": "task_snapshot_changed"}
        self._consume(record_path, record)
        meta["status"] = "reset_authorization"
        hs.atomic_write_json(self._upload_meta_path(meta["upload_id"]), meta)
        result = self._stage_reset_authorization(update_id, user_id, chat_id,
            {"task_file":meta["content_path"],"task_file_sha256":meta["sha256"],"upload_id":meta["upload_id"],
             "task_reference":meta["content_path"],"source":"telegram-upload"})
        if result.get("result") == "enqueued":
            meta["status"]="start_enqueued"; meta["request_id"]=result.get("request_id"); meta["start_enqueued_at"]=hs.iso_utc(self.clock())
            meta.pop("excerpt",None); hs.atomic_write_json(self._upload_meta_path(meta["upload_id"]),meta)
            self._supersede_upload_interactions(meta["upload_id"])
        return {"callback":"upload_start","upload_id":meta["upload_id"],**result}

    # ----- interactions

    def new_interaction(self, *, action: str, event: dict[str, Any], gate: dict[str, Any] | None, chat_id: int, extra: dict[str, Any] | None = None) -> str:
        token = secrets.token_urlsafe(24)
        record = {
            "token": token, "action": action, "event_id": event.get("event_id"), "run_id": event.get("run_id"),
            "gate_id": (gate or {}).get("gate_id"), "expected_state": (gate or {}).get("expected_state") or event.get("supervisor_state"),
            "artifact_sha256": (gate or {}).get("artifact_sha256"), "payload_sha256": (gate or {}).get("payload_sha256"),
            "candidate_sha": (gate or {}).get("candidate_sha"), "owner_user_id": self.config.get("owner_user_id"), "chat_id": chat_id,
            "created_at": hs.iso_utc(self.clock()), "expires_at_unix": self.clock() + float(self.config["callback_ttl_seconds"]),
            "consumed": False, "superseded": False, **(extra or {}),
        }
        hs.atomic_write_json(self.tg_paths.interactions_dir / f"{token}.json", record)
        return token

    def supersede_interactions(self, gate_id: str | None, event_id: str | None = None) -> int:
        count = 0
        for record_path in self.tg_paths.interactions_dir.glob("*.json"):
            record = hs.load_json(record_path, label="interaction")
            if not isinstance(record, dict) or record.get("consumed") or record.get("superseded"):
                continue
            if (gate_id and record.get("gate_id") == gate_id) or (event_id and record.get("event_id") == event_id):
                record["superseded"] = True
                hs.atomic_write_json(record_path, record)
                count += 1
        return count

    # ----- rendering

    def render_gate_card(self, gate: dict[str, Any], state: dict[str, Any] | None) -> str:
        fields = gate.get("summary_fields") or {}
        kind = gate.get("gate_type")
        lines = [f"[{kind}] gate {str(gate.get('gate_id'))[:8]} — run {str(gate.get('run_id'))[:8]}", f"Task: {redact(fields.get('task_title'), limit=200)}", f"Summary: {redact(fields.get('summary'), limit=600)}"]
        if kind == "plan_approval":
            lines += [
                f"Scope: {redact(fields.get('scope'), limit=400)}",
                "Changes: " + redact("; ".join(fields.get("intended_changes") or []), limit=600),
                f"Risk: {redact(fields.get('risk_summary'), limit=400)}",
                "Components: " + redact(", ".join(fields.get("affected_components") or []), limit=300),
                f"Migration: {fields.get('migration_required')}  Runtime validation: {fields.get('runtime_validation_required')}  Rebuild: {fields.get('rebuild_required')} ({redact(fields.get('rebuild_reason'), limit=120)})  Push approval: {fields.get('push_approval_required')}",
                f"Plan fingerprint: {str(gate.get('artifact_sha256'))[:12]}  payload {str(gate.get('payload_sha256'))[:12]}",
                f"Review dir: {redact(fields.get('review_directory'), limit=200)}",
            ]
        elif kind == "runtime_validation":
            lines += [
                "RUNTIME VALIDATION NOT RUN — push is blocked until exact-SHA evidence is recorded via the CLI.",
                f"Candidate: {str(fields.get('candidate_sha'))[:12]}  Local gate: {fields.get('local_gate_result')}  Review: {fields.get('codex_review_status')}",
                "Prepared commits: " + redact("; ".join(fields.get("prepared_commits") or []) or "n/a", limit=300),
                "Services: " + redact(", ".join(fields.get("affected_services") or []) or "none", limit=200),
                f"Rebuild: {fields.get('rebuild_required')} ({redact(fields.get('rebuild_reason'), limit=120)})",
                f"Local evidence: {redact(fields.get('local_evidence_summary'), limit=400)}",
                f"Record with: herdr-supervisor runtime-pass|runtime-fail --run-id {gate.get('run_id')} --candidate-sha {fields.get('candidate_sha')} --environment TEST --evidence-file <json under the review dir>",
            ]
        elif kind == "push_approval":
            lines += [
                f"Candidate: {str(fields.get('candidate_sha'))[:12]}  Local gate: {fields.get('local_gate_result')}  Runtime: {fields.get('runtime_evidence_status') or 'n/a'}",
                "Prepared commits: " + redact("; ".join(fields.get("prepared_commits") or []) or "n/a", limit=300),
                "Services: " + redact(", ".join(fields.get("affected_services") or []) or "none", limit=200),
                "Approval authorizes the push stage only; the human performs the push/PR.",
            ]
        elif kind == "generic_question":
            lines += [f"Question: {redact(fields.get('question'), limit=800)}"]
            if fields.get("answer_mode") == "choice":
                lines.append("Choices: " + redact(" | ".join(fields.get("choices") or []), limit=400))
            else:
                lines.append(f"Reply with /answer <text> (max {fields.get('max_answer_chars')} chars).")
        lines.append(f"Expires: {fmt_local(gate.get('expires_at_unix'), self.tz)}")
        return "\n".join(lines)

    def render_event(self, event: dict[str, Any]) -> str:
        data = event.get("data") or {}
        kind = event.get("type")
        when = fmt_local(event.get("at_unix"), self.tz)
        run = str(event.get("run_id"))[:8]
        if kind == "WAIT_QUOTA":
            return f"[{when}] run {run}: {data.get('provider')} quota exhausted ({', '.join(data.get('windows') or [])}). Task preserved; waiting until {fmt_local(data.get('resume_at'), self.tz)}."
        if kind == "QUOTA_RESUMED":
            return f"[{when}] run {run}: {data.get('provider')} quota wait ended; continuing in the same session."
        if kind == "WAIT_USER":
            return f"[{when}] run {run}: WAIT_USER — {redact(data.get('reason'), limit=500)}"
        if kind == "RUNTIME_VALIDATION_FAILED":
            return f"[{when}] run {run}: runtime validation FAILED for {str(data.get('candidate_sha'))[:12]} on {data.get('environment')}; still blocked."
        if kind == "COMMAND_RESULT":
            return f"[{when}] {data.get('action')}: {'OK' if data.get('ok') else 'REJECTED'} — {redact(data.get('message'), limit=400)}"
        if kind == "QUERY_RESULT":
            return f"[{when}] answer ({data.get('source', 'query')}):\n{redact(data.get('answer'), limit=3500)}"
        if kind == "RECOVERED_AFTER_RESTART":
            return f"[{when}] run {run}: supervisor recovered after restart in state {data.get('state')}."
        return f"[{when}] run {run}: {kind} {redact(json.dumps(data, ensure_ascii=False), limit=400)}"

    # ----- outbox delivery

    def deliver_outbox(self) -> dict[str, int]:
        counts = {"sent": 0, "superseded": 0, "uncertain": 0, "collapsed": 0, "skipped": 0}
        chat_id = self.owner_chat()
        events_dir = self.sup_paths.outbox_dir / "events"
        delivery_dir = self.sup_paths.outbox_dir / "delivery"
        if not events_dir.exists() or self.config.get("mode") == "unconfigured":
            return counts
        pending: list[tuple[dict[str, Any], dict[str, Any], Path]] = []
        for event_path in sorted(events_dir.glob("*.json")):
            event = hs.load_json(event_path, label="event")
            sidecar_path = delivery_dir / event_path.name
            sidecar = hs.load_json(sidecar_path, label="delivery") if sidecar_path.exists() else {"event_id": event.get("event_id"), "status": "pending", "attempts": 0}
            if sidecar.get("status") in ("sending", "sending_document"):
                # The previous process died across an ambiguous network boundary. Preserve part progress,
                # then use the existing one-replacement/manual-review policy instead of stranding or
                # blindly replaying the event.
                document = (sidecar.get("parts") or {}).get("document")
                if isinstance(document, dict) and document.get("status") == "sending":
                    document.update(status="delivery_uncertain", reason="recovered after interrupted send")
                sidecar.update(status="delivery_uncertain", reason="recovered after interrupted send", updated_at=hs.iso_utc(self.clock()))
                hs.atomic_write_json(sidecar_path, sidecar)
            if sidecar.get("status") in ("pending", "delivery_uncertain"):
                pending.append((event, sidecar, sidecar_path))
        pending.sort(key=lambda item: (item[0].get("run_id", ""), item[0].get("sequence", 0)))
        now = self.clock()
        summarize_paths: list[Path] = []
        informational = [item for item in pending if not item[0].get("actionable") and item[0].get("type") not in ("COMMAND_RESULT", "QUERY_RESULT")]
        collapse = len(informational) > int(self.config["informational_collapse_after"])
        for event, sidecar, sidecar_path in pending:
            summarize_paths = []
            target_chat = chat_id
            if event.get("type") in ("COMMAND_RESULT", "QUERY_RESULT") and isinstance((event.get("data") or {}).get("chat_id"), int):
                target_chat = event["data"]["chat_id"]
            if target_chat is None:
                counts["skipped"] += 1
                continue
            if now > float(event.get("expires_at_unix") or 0):
                self._mark(sidecar_path, sidecar, "expired")
                counts["superseded"] += 1
                continue
            status_view = self.status_reader() if event.get("type") in ("TASK_DONE", "WAIT_QUOTA", "WAIT_USER", "PLAN_APPROVAL_REQUIRED") else None
            gate = None
            if event.get("actionable"):
                gate = self._gate_for_event(event)
                if gate is None:
                    self._mark(sidecar_path, sidecar, "superseded", reason="gate no longer pending")
                    self.supersede_interactions(event.get("gate_id"), event.get("event_id"))
                    counts["superseded"] += 1
                    continue
                if sidecar.get("status") == "delivery_uncertain":
                    # Never blindly resend the same card: supersede old tokens, send one replacement with fresh tokens.
                    self.supersede_interactions(event.get("gate_id"), event.get("event_id"))
            else:
                if collapse and (event, sidecar, sidecar_path) in informational and event is not informational[-1][0]:
                    self._mark(sidecar_path, sidecar, "collapsed")
                    counts["collapsed"] += 1
                    continue
            rendered = self._render_for_delivery(event, status_view, gate)
            text = rendered.html
            if not event.get("actionable") and informational and event is informational[-1][0]:
                summarize_paths = [p for p in delivery_dir.glob("*.json") if (hs.load_json(p, label="delivery") or {}).get("status") == "collapsed"]
                if summarize_paths:
                    text = f"({len(summarize_paths)} earlier informational events collapsed)\n" + text
            markup = self._keyboard(rendered.keyboard, event=event, gate=gate, chat_id=target_chat) if rendered.keyboard else None
            document_id = self._document_for_event(event, sidecar_path, sidecar, rendered)
            if document_id and sidecar.get("parts", {}).get("summary", {}).get("status") == "delivered":
                # summary already confirmed (restart after an uncertain document): deliver only the document part
                self._deliver_document_part(sidecar_path, sidecar, target_chat, document_id, counts)
                continue
            self._mark(sidecar_path, sidecar, "sending")
            summarize_paths = summarize_paths if not event.get("actionable") else []
            try:
                result = self.send(target_chat, text, reply_markup=markup, html=True)
            except tg.TelegramError as error:
                if tg.delivery_failure_is_permanent(error):
                    self._mark(sidecar_path, sidecar, "failed", reason=error.category)
                else:
                    self._mark(sidecar_path, sidecar, "delivery_uncertain", reason=error.category)
                counts["uncertain"] += 1
                continue
            for path in summarize_paths:  # only after the summary actually went out
                record = hs.load_json(path, label="delivery")
                record["status"] = "collapsed_summarized"
                hs.atomic_write_json(path, record)
            if document_id:
                parts = sidecar.setdefault("parts", {})
                parts["summary"] = {"status": "delivered", "message_id": result.get("message_id")}
                self._deliver_document_part(sidecar_path, sidecar, target_chat, document_id, counts)
                continue
            self._mark(sidecar_path, sidecar, "delivered", message_id=result.get("message_id"))
            counts["sent"] += 1
        return counts

    def _render_for_delivery(self, event: dict[str, Any], status: dict[str, Any] | None, gate: dict[str, Any] | None) -> hp.Rendered:
        rendered = hp.render_event(event, status, gate, self.tz, long_threshold=int(self.config["long_output_chars"]))
        if event.get("type") == "TASK_DONE" and status is not None:
            rendered = hp.render_task_done(event, status, self.tz, highlights=self._done_highlights(status, event))
        return rendered

    def _done_highlights(self, status: dict[str, Any], event: dict[str, Any]) -> list[str]:
        data = event.get("data") or {}
        items = [f"final stage: {data.get('stage')}", str(data.get("handoff") or "")[:160]]
        policy = status.get("runtime_policy") or {}
        evidence = status.get("runtime_evidence") or {}
        if policy.get("runtime_validation_required"):
            items.append("runtime validation " + ("passed" if evidence.get("result") == "PASS" else "not recorded"))
        if policy.get("push_approval_required"):
            items.append("push stage " + ("approved" if status.get("push_approval") else "not approved"))
        return [i for i in items if i]

    def _document_for_event(self, event: dict[str, Any], sidecar_path: Path, sidecar: dict[str, Any], rendered: hp.Rendered) -> str | None:
        """Bind at most one registered document to an event: an explicit artifact_id from the producer, a
        report generated once for TASK_DONE, or a long rendered body. The id is persisted in the sidecar so
        restarts and duplicate events reuse the same artifact instead of registering another."""
        parts = sidecar.get("parts") or {}
        existing = (parts.get("document") or {}).get("artifact_id")
        if existing:
            return existing
        data = event.get("data") or {}
        artifact_id = data.get("artifact_id") if isinstance(data.get("artifact_id"), str) else None
        if not artifact_id and event.get("type") == "TASK_DONE":
            try:
                artifact_id = self._final_report_artifact(event)["artifact_id"]
            except hs.SupervisorError:
                artifact_id = None
        if not artifact_id and rendered.document_markdown:
            try:
                artifact_id = ha.register_text(self.sup_paths, self.sup_config, category="query_answer" if event.get("type") == "QUERY_RESULT" else "report", text=rendered.document_markdown, name=rendered.document_name or "output", run_id=event.get("run_id"), event_id=event.get("event_id"), title=rendered.document_title)["artifact_id"]
            except hs.SupervisorError:
                artifact_id = None
        if artifact_id:
            sidecar.setdefault("parts", {})["document"] = {"artifact_id": artifact_id, "status": "pending", "attempts": 0}
            # Persist the immutable binding before any network operation. A crash after registration then
            # reuses this artifact rather than creating a second derivative for the same event.
            sidecar["updated_at"] = hs.iso_utc(self.clock())
            hs.atomic_write_json(sidecar_path, sidecar)
        return artifact_id

    def _final_report_artifact(self, event: dict[str, Any]) -> dict[str, Any]:
        descriptor = (event.get("data") or {}).get("final_report")
        if isinstance(descriptor, dict):
            if descriptor.get("schema_version") != 1 or not isinstance(descriptor.get("source_path"), str) or not isinstance(descriptor.get("source_sha256"), str):
                raise hs.SupervisorError("final report descriptor is invalid")
            for record in ha.list_records(self.sup_paths, event.get("run_id")):
                if record.get("category") == "final_report" and record.get("event_id") == event.get("event_id") and record.get("source_sha256") == descriptor["source_sha256"]:
                    return record
            record = ha.register_file(
                self.sup_paths,
                self.sup_config,
                category="final_report",
                source_path=descriptor["source_path"],
                run_id=event.get("run_id"),
                task_id=(self._state() or {}).get("task_id"),
                event_id=event.get("event_id"),
                title=str(descriptor.get("title") or "Final task report"),
                expected_source_sha256=descriptor["source_sha256"],
                expected_source_bytes=descriptor.get("source_bytes"),
            )
            return record
        existing = self._latest_artifact("final_report", event.get("run_id"))
        if existing:
            return existing
        state = self._state() or {}
        fields = build_final_report_fields(state, event, self.enqueuer.list_events())
        markdown = hp.render_final_report_markdown(fields)
        return ha.register_text(self.sup_paths, self.sup_config, category="final_report", text=markdown, name=f"task-{hp.abbrev(event.get('run_id'))}-final-report", run_id=event.get("run_id"), task_id=state.get("task_id"), event_id=event.get("event_id"), title="Final task report")

    def _deliver_document_part(self, sidecar_path: Path, sidecar: dict[str, Any], chat_id: int, artifact_id: str, counts: dict[str, int]) -> None:
        """Part-level durable delivery. A failed send stays pending for backoff/retry; an ambiguous send is
        marked uncertain and replaced at most `max_document_replacements` times; an unsafe artifact fails closed."""
        parts = sidecar.setdefault("parts", {})
        doc = parts.setdefault("document", {"artifact_id": artifact_id, "status": "pending", "attempts": 0})
        limit = int(self.config.get("max_document_replacements", 1))
        if doc.get("status") == "delivery_uncertain" and int(doc.get("replacements") or 0) >= limit:
            self._mark(sidecar_path, sidecar, "delivered_document_uncertain", reason="document delivery uncertain; replacement budget exhausted")
            counts["uncertain"] += 1
            return
        try:
            record, data = ha.verify_for_send(self.sup_paths, self.sup_config, artifact_id)
        except hs.SupervisorError as error:
            doc.update(status="failed", reason=str(error)[:120])
            self._mark(sidecar_path, sidecar, "delivered", reason="document unsafe; summary only")
            self.safe_send(chat_id, f"The attached report could not be verified ({redact(str(error), limit=120)}); the local file is preserved.")
            counts["sent"] += 1
            return
        doc["attempts"] = int(doc.get("attempts") or 0) + 1
        if doc.get("status") == "delivery_uncertain":
            doc["replacements"] = int(doc.get("replacements") or 0) + 1
        doc["status"] = "sending"
        hs.atomic_write_json(sidecar_path, {**sidecar, "status": "sending_document", "updated_at": hs.iso_utc(self.clock())})
        try:
            result = self.api.send_document(chat_id, record["display_name"], data, caption=(record.get("title") or "")[:200])
        except tg.TelegramError as error:
            if tg.delivery_failure_is_permanent(error):
                doc.update(status="failed", reason=error.category)
                self._mark(sidecar_path, sidecar, "delivered", reason="document failed permanently; summary delivered")
                counts["sent"] += 1
            else:
                doc.update(status="delivery_uncertain" if error.category in ("HTTPS_FAILURE",) else "pending", reason=error.category)
                self._mark(sidecar_path, sidecar, "delivery_uncertain", reason=f"document {error.category}")
                counts["uncertain"] += 1
            return
        doc.update(status="delivered", message_id=result.get("message_id"), file_id=(result.get("document") or {}).get("file_id"))
        self._mark(sidecar_path, sidecar, "delivered", message_id=(parts.get("summary") or {}).get("message_id"))
        counts["sent"] += 1

    def _gate_for_event(self, event: dict[str, Any]) -> dict[str, Any] | None:
        state = self._state()
        if state is None:
            return None
        gate = state.get("pending_gate")
        if not isinstance(gate, dict) or gate.get("status") != "pending" or gate.get("gate_id") != event.get("gate_id"):
            return None
        if state.get("supervisor_state") != gate.get("expected_state"):
            return None
        return gate

    def _keyboard_for_gate(self, event: dict[str, Any], gate: dict[str, Any], chat_id: int) -> dict[str, Any]:
        kind = gate.get("gate_type")
        rows: list[list[dict[str, str]]] = []
        if kind == "plan_approval":
            rows = [[{"text": "Approve", "callback_data": self.new_interaction(action="approve", event=event, gate=gate, chat_id=chat_id)}, {"text": "Reject", "callback_data": self.new_interaction(action="reject", event=event, gate=gate, chat_id=chat_id)}],
                    [{"text": "Request revision", "callback_data": self.new_interaction(action="revise", event=event, gate=gate, chat_id=chat_id)}, {"text": "View details", "callback_data": self.new_interaction(action="view_details", event=event, gate=gate, chat_id=chat_id)}]]
        elif kind == "push_approval":
            rows = [[{"text": "Approve push stage", "callback_data": self.new_interaction(action="approve", event=event, gate=gate, chat_id=chat_id)}],
                    [{"text": "Keep waiting", "callback_data": self.new_interaction(action="keep_waiting", event=event, gate=gate, chat_id=chat_id)}, {"text": "Cancel", "callback_data": self.new_interaction(action="cancel", event=event, gate=gate, chat_id=chat_id)}]]
        elif kind == "generic_question" and (gate.get("summary_fields") or {}).get("answer_mode") == "choice":
            rows = [[{"text": choice[:40], "callback_data": self.new_interaction(action="answer", event=event, gate=gate, chat_id=chat_id, extra={"choice": choice})}] for choice in gate["summary_fields"]["choices"]]
            rows.append([{"text": "Request revision", "callback_data": self.new_interaction(action="revise", event=event, gate=gate, chat_id=chat_id)}])
        else:
            rows = [[{"text": "Status", "callback_data": self.new_interaction(action="status", event=event, gate=gate, chat_id=chat_id)}, {"text": "Pause", "callback_data": self.new_interaction(action="pause", event=event, gate=gate, chat_id=chat_id)}, {"text": "Cancel", "callback_data": self.new_interaction(action="cancel", event=event, gate=gate, chat_id=chat_id)}]]
        return {"inline_keyboard": rows}

    def _info_keyboard(self, event: dict[str, Any], chat_id: int) -> dict[str, Any]:
        return {"inline_keyboard": [[{"text": label, "callback_data": self.new_interaction(action=action, event=event, gate=None, chat_id=chat_id)} for label, action in INFO_CONTROLS]]}

    def _mark(self, path: Path, sidecar: dict[str, Any], status: str, **extra: Any) -> None:
        sidecar.update(status=status, updated_at=hs.iso_utc(self.clock()), attempts=int(sidecar.get("attempts") or 0) + (1 if status == "sending" else 0), **extra)
        hs.atomic_write_json(path, sidecar)

    # ----- helpers

    def _state(self) -> dict[str, Any] | None:
        try:
            return self.store.read_state(required=False)
        except hs.SupervisorError:
            return None

    def _enqueue_or_shadow(self, command: dict[str, Any], chat_id: int, description: str) -> dict[str, Any]:
        if self.config.get("mode") != "actionable":
            self.safe_send(chat_id, f"SHADOW: would {description}; no supervisor state changed.")
            return {"result": "shadow", "action": command["action"]}
        self.enqueuer.enqueue_command(command)
        self.safe_send(chat_id, f"Queued: {description}. The supervisor confirms separately.")
        return {"result": "enqueued", "action": command["action"], "request_id": command["request_id"]}

    # ----- daemon loop

    def heartbeat(self, note: str) -> None:
        hs.atomic_write_json(self.tg_paths.heartbeat_file, {"pid": os.getpid(), "at_unix": self.clock(), "note": note})

    def run(self, *, max_rounds: int | None = None) -> int:
        """Long-poll loop: outbound only, single consumer, bounded backoff. Telegram failures never touch
        supervisor state; HTTP 409 and a foreign webhook stop the loop fail-closed."""
        with self.tg_paths.lock_file.open("a+", encoding="utf-8") as lock, TelegramPaths.bot_lock_file(self.bot_id).open("a+", encoding="utf-8") as bot_lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(bot_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (BlockingIOError, PermissionError):
                self.stopped_reason = "POLLER_CONFLICT_LOCAL"
                return 3
            try:
                info = self.api.get_webhook_info()
            except tg.TelegramError as error:
                self.stopped_reason = error.category
                return 2
            if info.get("url"):
                self.stopped_reason = "WEBHOOK_CONFLICT"
                hs.atomic_write_json(self.tg_paths.conflict_file, {"kind": "WEBHOOK_CONFLICT", "at": hs.iso_utc(self.clock())})
                return 2
            rounds = 0
            while max_rounds is None or rounds < max_rounds:
                rounds += 1
                self.heartbeat("poll")
                try:
                    self.reap_uploads()
                    self.deliver_outbox()
                    self.deliver_document_queue()
                    self.poll_once()
                    self.backoff = 0.0
                    with contextlib.suppress(FileNotFoundError):
                        if self.tg_paths.conflict_file.exists():
                            self.tg_paths.conflict_file.unlink()
                except tg.TelegramError as error:
                    if error.category == "POLLER_CONFLICT":
                        hs.atomic_write_json(self.tg_paths.conflict_file, {"kind": "POLLER_CONFLICT", "http_status": 409, "at": hs.iso_utc(self.clock())})
                        self.stopped_reason = "POLLER_CONFLICT"
                        return 2
                    if error.category == "AUTH_FAILURE":
                        self.stopped_reason = "AUTH_FAILURE"
                        return 2
                    self._sleep_backoff()
                except hs.SupervisorError:
                    self._sleep_backoff()
            return 0

    def _sleep_backoff(self) -> None:
        base = float(self.config["backoff_base_seconds"])
        cap = float(self.config["backoff_cap_seconds"])
        self.backoff = min(cap, max(base, self.backoff * 2)) if self.backoff else base
        self.sleeper(self.backoff + self.rng() * min(1.0, self.backoff))


def build_final_report_fields(state: dict[str, Any], event: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
    """Assemble the twenty report fields from durable supervisor state (agents may extend items through a
    registered `final_report` artifact of their own; this generator never invents evidence)."""
    data = event.get("data") or {}
    policy = state.get("runtime_policy") or {}
    evidence = state.get("runtime_evidence") or {}
    approvals = state.get("approvals") or []
    handoffs = [e for e in events if e.get("type") == "COMMAND_RESULT"]
    return {
        "title": f"Task report: {str(state.get('task_text') or '').strip().splitlines()[0][:80] if state.get('task_text') else 'supervised task'}",
        "run_id": state.get("run_id"), "generated_at": hs.iso_utc(float(event.get("at_unix") or 0)),
        "changed": data.get("handoff") or "see the implementation handoff",
        "files": "see the implementation handoff registered for this run",
        "tests": "see the implementation handoff",
        "root_cause": "not applicable to this task" if not state.get("deferred_anomaly") else f"deferred anomaly {state['deferred_anomaly'].get('kind')} reconciled: {state['deferred_anomaly'].get('continuation')}",
        "precedence": "typed human gates > valid routing > (missing protocol: quota refresh -> WAIT_QUOTA if blocking, else WAIT_USER)",
        "reconciliation": "one reconciliation turn per deferred anomaly; original prompt never replayed",
        "early_wake": f"quota rechecked every {state.get('quota_recheck_seconds', 'configured')} seconds via non-LLM refresh",
        "no_llm_polling": "quota waits issue no prompts/reads (asserted by tests)",
        "ask_failover": "read-only query sessions only; provider chosen by quota/lifecycle; failover shown to the human",
        "ux": "typed renderers with state-aware keyboards",
        "long_input": "uploaded task artifact accepted through the confirmed task-file workflow" if state.get("task_reference") else "short text task",
        "long_output": "this report was delivered as a registered Markdown document",
        "live_tests": [f"{e.get('data', {}).get('action')}: {'ok' if e.get('data', {}).get('ok') else 'rejected'}" for e in handoffs][-10:],
        "live_not_performed": "see the implementation handoff",
        "secret_scan": "see the implementation handoff",
        "repo_readiness": "see the implementation handoff",
        "limitations": "see the implementation handoff",
        "release": "recommendation is made by the reviewer, not by this generator",
        "human_actions": [f"{a.get('gate_type')}: {a.get('action')} by {a.get('actor')}" for a in approvals][-10:] or ["none recorded"],
        "approval_actions": "commit, push, tag, publication and service activation always require separate human authorization",
        "policy": policy, "runtime_evidence": {k: evidence.get(k) for k in ("result", "candidate_sha", "environment")} if evidence else None,
    }


# --------------------------------------------------------------------------- doctor / setup / main


def doctor_summary(*, network: bool, tg_paths: TelegramPaths | None = None, api_factory: Callable[[str], Any] | None = None) -> dict[str, Any]:
    tg_paths = tg_paths or TelegramPaths()
    report: dict[str, Any] = {"status": "UNCONFIGURED", "config_file": str(tg_paths.config_file), "checks": {}}
    if not tg_paths.config_file.exists():
        report["checks"]["config"] = "absent"
        return report
    try:
        config = load_telegram_config(tg_paths.config_file)
    except hs.SupervisorError as error:
        report.update(status="CONFIG_ERROR", detail=tg.sanitize(str(error)))
        return report
    report["mode"] = config["mode"]
    report["checks"]["config"] = "ok"
    if config["mode"] == "unconfigured":
        return report
    try:
        check_private_file(tg_paths.config_file, "telegram config")
        token = read_token(Path(config["token_file"]))
        report["checks"]["token_file"] = "ok (0600, shape valid)"
    except hs.SupervisorError as error:
        report.update(status="CONFIG_ERROR", detail=tg.sanitize(str(error)))
        return report
    lock_held = False
    if tg_paths.lock_file.exists():
        with tg_paths.lock_file.open("r", encoding="utf-8") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except (BlockingIOError, PermissionError):
                lock_held = True
    report["checks"]["local_daemon"] = "running" if lock_held else "not running"
    if tg_paths.conflict_file.exists():
        conflict = hs.load_json(tg_paths.conflict_file, label="conflict")
        report["checks"]["conflict"] = conflict.get("kind") if isinstance(conflict, dict) else "unknown"
        report["status"] = conflict.get("kind") if isinstance(conflict, dict) and conflict.get("kind") in ("POLLER_CONFLICT", "WEBHOOK_CONFLICT") else "POLLER_CONFLICT"
        return report
    if not network:
        report["status"] = "CONFIGURED_NO_NETWORK_CHECK"
        return report
    try:
        api = (api_factory or (lambda t: tg.BotApi(t)))(token)
        me = api.get_me()
        report["checks"]["auth"] = f"ok (bot {me.get('id')})"
        info = api.get_webhook_info()
        if info.get("url"):
            report["checks"]["webhook"] = "nonempty url"
            report["status"] = "WEBHOOK_CONFLICT"
            return report
        report["checks"]["webhook"] = "empty"
    except tg.TelegramError as error:
        report["status"] = error.category if error.category in ("DNS_FAILURE", "HTTPS_FAILURE", "AUTH_FAILURE", "POLLER_CONFLICT") else "HTTPS_FAILURE"
        report["detail"] = tg.sanitize(error.message, token)
        return report
    report["status"] = "HEALTHY"
    return report


def setup(tg_paths: TelegramPaths, *, owner_user_id: int, chat_id: int | None, timezone: str, token_source: Path | None, ask_secret: Callable[[str], str] = getpass.getpass) -> Path:
    """Non-echoed token entry or import from a protected file; never a CLI argument; initial mode shadow."""
    tg_paths.ensure()
    TelegramPaths.ensure_bot_lock_dir()
    tg_paths.config_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(tg_paths.config_file.parent, 0o700)
    token_file = tg_paths.config_file.parent / "bot-token"
    if token_source is not None:
        check_private_file(token_source, "token source file")
        token = token_source.read_text(encoding="utf-8").strip()
    else:
        token = ask_secret("Telegram bot token (not echoed): ").strip()
    if not tg.TOKEN_SHAPE_RE.fullmatch(token):
        raise hs.SupervisorError("token has an invalid shape; nothing written")
    tmp = token_file.with_name(".bot-token.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(token + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, token_file)
    config = {**DEFAULT_TG_CONFIG, "mode": "shadow", "owner_user_id": owner_user_id, "chat_id": chat_id, "timezone": timezone, "token_file": str(token_file)}
    load_telegram_config_from_dict(config)
    hs.atomic_write_json(tg_paths.config_file, config, mode=0o600)
    return tg_paths.config_file


def load_telegram_config_from_dict(config: dict[str, Any]) -> None:
    import tempfile  # noqa: PLC0415

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "c.json"
        path.write_text(json.dumps(config))
        load_telegram_config(path)


def build_bridge(tg_paths: TelegramPaths | None = None, *, api: Any = None) -> Bridge:
    tg_paths = tg_paths or TelegramPaths()
    tg_config = load_telegram_config(tg_paths.config_file)
    sup_paths = hs.Paths.from_environment()
    sup_config = hs.load_config(sup_paths.config_file)
    if tg_config["mode"] == "unconfigured":
        raise hs.SupervisorError("telegram is unconfigured; run `herdr-telegram setup` first")
    check_private_file(tg_paths.config_file, "telegram config")
    token = read_token(Path(tg_config["token_file"]))
    api = api or tg.BotApi(token, http_timeout=float(tg_config["http_timeout_seconds"]))
    me = api.get_me()
    herdr = hs.HerdrCli(hs.resolve_herdr_bin(sup_config.get("herdr_bin")))
    supervisor = hs.Supervisor(sup_paths, sup_config, herdr)
    return Bridge(sup_paths=sup_paths, sup_config=sup_config, tg_paths=tg_paths, tg_config=tg_config, api=api,
                  bot_id=int(me["id"]), status_reader=supervisor.status,
                  reset_inventory_reader=lambda: hcr.AppServerClient().inventory())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="herdr-telegram", description="Telegram long-poll bridge for herdr-supervisor (outbound HTTPS only).")
    sub = parser.add_subparsers(dest="command", required=True)
    setup_cmd = sub.add_parser("setup", help="store the bot token (non-echoed prompt or protected file), owner id, chat id; mode starts as shadow")
    setup_cmd.add_argument("--owner-user-id", type=int, required=True)
    setup_cmd.add_argument("--chat-id", type=int, default=None)
    setup_cmd.add_argument("--timezone", default=DEFAULT_TIMEZONE)
    setup_cmd.add_argument("--token-file", default=None, help="import the token from this mode-0600 file instead of prompting")
    doctor = sub.add_parser("doctor", help="telegram health (read-only; --network performs getMe/getWebhookInfo)")
    doctor.add_argument("--json", action="store_true")
    doctor.add_argument("--network", action="store_true")
    doctor.add_argument("--probe-network-only", action="store_true", help="token-free DNS + verified TLS check")
    sub.add_parser("daemon", help="run the long-poll bridge in the foreground (singleton)")
    sub.add_parser("prepare", help="create the protected state and per-bot lock directories (0700); no network, no service action")
    args = parser.parse_args(argv)
    tg_paths = TelegramPaths()
    try:
        if args.command == "setup":
            path = setup(tg_paths, owner_user_id=args.owner_user_id, chat_id=args.chat_id, timezone=args.timezone, token_source=Path(args.token_file) if args.token_file else None)
            print(f"telegram configured in shadow mode: {path} (switch mode to actionable by editing the file after doctor --network is HEALTHY)")
            return 0
        if args.command == "doctor":
            if args.probe_network_only:
                report = tg.network_probe()
            else:
                report = doctor_summary(network=args.network, tg_paths=tg_paths)
            print(json.dumps(report, indent=2, sort_keys=True) if args.json else "\n".join(f"{k}: {v}" for k, v in report.items()))
            return 0 if report.get("status") in ("HEALTHY", "UNCONFIGURED", "CONFIGURED_NO_NETWORK_CHECK") else 1
        if args.command == "prepare":
            tg_paths.ensure()
            lock_dir = TelegramPaths.ensure_bot_lock_dir()
            print(f"prepared: {tg_paths.state_dir} (0700), {lock_dir} (0700)")
            return 0
        if args.command == "daemon":
            bridge = build_bridge(tg_paths)
            code = bridge.run()
            print(f"daemon stopped: {bridge.stopped_reason or 'ok'}", file=sys.stderr)
            return code
    except hs.SupervisorError as error:
        print(f"ERROR: {tg.sanitize(str(error))}", file=sys.stderr)
        return 2
    except tg.TelegramError as error:
        print(f"ERROR: {error.category}: {tg.sanitize(error.message)}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
