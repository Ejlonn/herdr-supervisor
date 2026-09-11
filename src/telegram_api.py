"""Minimal stdlib Telegram Bot API client with sanitized errors.

Outbound HTTPS only (verified TLS to api.telegram.org:443). The token-bearing URL never appears in
exceptions, logs, or reports; every failure maps to a bounded category.
"""

from __future__ import annotations

import json
import re
import socket
import ssl
import urllib.error
import urllib.request
import uuid
from typing import Any, Callable

TELEGRAM_HOST = "api.telegram.org"
MAX_MESSAGE_CHARS = 4096
MAX_CALLBACK_DATA_BYTES = 64
MAX_BODY_BYTES = 1_000_000

TOKEN_SHAPE_RE = re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b")
# Telegram-controlled relative file paths (getFile.file_path): bounded, relative, no traversal/scheme/control.
FILE_PATH_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-]{0,120}(?:/[A-Za-z0-9_][A-Za-z0-9_.\-]{0,120}){0,6}$")


class TelegramError(Exception):
    """Sanitized transport/API failure. `category` is one of the doctor categories."""

    def __init__(self, category: str, message: str, *, http_status: int | None = None, error_code: int | None = None) -> None:
        super().__init__(f"{category}: {message}")
        self.category = category
        self.message = message
        self.http_status = http_status
        self.error_code = error_code


def delivery_failure_is_permanent(error: TelegramError) -> bool:
    """Whether a Telegram send was definitively rejected and should not be retried.

    Authentication failures and non-transient 4xx API responses are permanent. Rate limits, request
    timeout/conflict responses, 5xx responses, and transport failures remain retryable or ambiguous.
    """
    if error.category == "AUTH_FAILURE":
        return True
    code = error.error_code
    return error.category == "API_ERROR" and isinstance(code, int) and 400 <= code < 500 and code not in (408, 409, 429)


def sanitize(text: str, token: str | None = None) -> str:
    """Remove bot-token shapes and token URLs from any text destined for logs/errors/messages."""
    if not isinstance(text, str):
        text = str(text)
    if token:
        text = text.replace(token, "<token>")
    text = re.sub(r"https?://api\.telegram\.org/file/bot[^/\s]+", "https://api.telegram.org/file/bot<token>", text)
    text = re.sub(r"https?://api\.telegram\.org/bot[^/\s]+", "https://api.telegram.org/bot<token>", text)
    text = TOKEN_SHAPE_RE.sub("<token>", text)
    return text


def _classify_exception(error: BaseException) -> tuple[str, str]:
    if isinstance(error, urllib.error.HTTPError):
        return "HTTP", f"http status {error.code}"
    if isinstance(error, socket.gaierror):
        return "DNS_FAILURE", "name resolution failed"
    if isinstance(error, ssl.SSLError):
        return "HTTPS_FAILURE", "tls verification or handshake failed"
    if isinstance(error, (TimeoutError, socket.timeout)):
        return "HTTPS_FAILURE", "https request timed out"
    if isinstance(error, urllib.error.URLError):
        reason = getattr(error, "reason", None)
        if isinstance(reason, socket.gaierror):
            return "DNS_FAILURE", "name resolution failed"
        if isinstance(reason, ssl.SSLError):
            return "HTTPS_FAILURE", "tls verification or handshake failed"
        return "HTTPS_FAILURE", f"transport failed ({type(reason).__name__ if reason is not None else 'unknown'})"
    if isinstance(error, (ConnectionError, OSError)):
        return "HTTPS_FAILURE", f"transport failed ({type(error).__name__})"
    return "HTTPS_FAILURE", f"unexpected transport error ({type(error).__name__})"


class BotApi:
    """`call(method, params)` -> result. `opener` is injectable for tests (never a real network there)."""

    def __init__(self, token: str, *, opener: Callable[..., Any] | None = None, downloader: Callable[..., Any] | None = None, http_timeout: float = 35.0) -> None:
        if not isinstance(token, str) or not TOKEN_SHAPE_RE.fullmatch(token):
            raise TelegramError("AUTH_FAILURE", "bot token has an invalid shape")
        self._token = token
        self.http_timeout = http_timeout
        self._opener = opener or self._default_open
        self._downloader = downloader or self._default_download
        self.last_http_status: int | None = None

    def _default_download(self, url: str, timeout: float, max_bytes: int) -> bytes:
        """Streaming GET bounded to max_bytes + 1 sentinel byte (the caller rejects the overrun)."""
        context = ssl.create_default_context()
        context.check_hostname = True
        context.verify_mode = ssl.CERT_REQUIRED
        request = urllib.request.Request(url, method="GET", headers={"User-Agent": "herdr-telegram/1"})
        chunks: list[bytes] = []
        total = 0
        with urllib.request.urlopen(request, timeout=timeout, context=context) as response:  # noqa: S310 - fixed https host
            if response.status != 200:
                raise TelegramError("API_ERROR", f"file download returned http {response.status}", http_status=response.status)
            while total <= max_bytes:
                chunk = response.read(min(65536, max_bytes + 1 - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
        return b"".join(chunks)

    def _default_open(self, url: str, body: bytes, timeout: float, content_type: str = "application/json") -> tuple[int, bytes]:
        context = ssl.create_default_context()
        context.check_hostname = True
        context.verify_mode = ssl.CERT_REQUIRED
        request = urllib.request.Request(url, data=body, method="POST", headers={"Content-Type": content_type, "User-Agent": "herdr-telegram/1"})
        try:
            with urllib.request.urlopen(request, timeout=timeout, context=context) as response:  # noqa: S310 - fixed https host
                return response.status, response.read(MAX_BODY_BYTES + 1)
        except urllib.error.HTTPError as error:
            return error.code, error.read(MAX_BODY_BYTES + 1)

    def call(self, method: str, params: dict[str, Any] | None = None, *, timeout: float | None = None, body: bytes | None = None, content_type: str = "application/json") -> Any:
        if not re.fullmatch(r"[A-Za-z]{3,40}", method):
            raise TelegramError("MALFORMED", "invalid method name")
        url = f"https://{TELEGRAM_HOST}/bot{self._token}/{method}"
        body = json.dumps(params or {}).encode("utf-8") if body is None else body
        try:
            if content_type == "application/json":
                status, raw = self._opener(url, body, timeout or self.http_timeout)
            else:
                status, raw = self._opener(url, body, timeout or self.http_timeout, content_type)
        except Exception as error:  # noqa: BLE001 - every transport failure is sanitized
            category, message = _classify_exception(error)
            raise TelegramError(category, sanitize(message, self._token)) from None
        self.last_http_status = status
        if len(raw) > MAX_BODY_BYTES:
            raise TelegramError("MALFORMED", "response body exceeds the bound")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise TelegramError("MALFORMED", f"non-JSON response (http {status})", http_status=status) from None
        if not isinstance(payload, dict):
            raise TelegramError("MALFORMED", "response is not an object", http_status=status)
        if status == 409:
            raise TelegramError("POLLER_CONFLICT", "telegram reported a competing getUpdates consumer", http_status=409, error_code=409)
        if status in (401, 404) or (payload.get("ok") is False and payload.get("error_code") in (401, 404)):
            raise TelegramError("AUTH_FAILURE", "telegram rejected the bot token", http_status=status, error_code=payload.get("error_code"))
        if payload.get("ok") is not True:
            description = sanitize(str(payload.get("description", ""))[:200], self._token)
            raise TelegramError("API_ERROR", f"telegram api error {payload.get('error_code')}: {description}", http_status=status, error_code=payload.get("error_code") if isinstance(payload.get("error_code"), int) else None)
        return payload.get("result")

    # convenience wrappers (all outbound)
    def get_me(self) -> dict[str, Any]:
        result = self.call("getMe")
        if not isinstance(result, dict) or not isinstance(result.get("id"), int):
            raise TelegramError("MALFORMED", "getMe returned no bot id")
        return result

    def get_webhook_info(self) -> dict[str, Any]:
        result = self.call("getWebhookInfo")
        if not isinstance(result, dict):
            raise TelegramError("MALFORMED", "getWebhookInfo returned no object")
        return result

    def get_updates(self, *, offset: int, timeout_seconds: int, limit: int = 50) -> list[dict[str, Any]]:
        timeout_seconds = max(1, min(int(timeout_seconds), 50))
        result = self.call("getUpdates", {"offset": offset, "timeout": timeout_seconds, "limit": max(1, min(limit, 100)), "allowed_updates": ["message", "callback_query"]}, timeout=timeout_seconds + 10)
        if not isinstance(result, list):
            raise TelegramError("MALFORMED", "getUpdates returned no list")
        return [item for item in result if isinstance(item, dict)]

    def get_file(self, file_id: str) -> str:
        """Return the Telegram-controlled relative file_path for a file id (validated, still untrusted)."""
        if not isinstance(file_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,256}", file_id):
            raise TelegramError("MALFORMED", "file id has an invalid shape")
        result = self.call("getFile", {"file_id": file_id})
        file_path = result.get("file_path") if isinstance(result, dict) else None
        if not isinstance(file_path, str) or not FILE_PATH_RE.fullmatch(file_path) or ".." in file_path.split("/"):
            raise TelegramError("MALFORMED", "getFile returned an unacceptable file path")
        return file_path

    def download_file(self, file_path: str, *, max_bytes: int) -> bytes:
        """Bounded download from the fixed Telegram file endpoint; token-bearing URL never escapes."""
        if not isinstance(file_path, str) or not FILE_PATH_RE.fullmatch(file_path) or ".." in file_path.split("/"):
            raise TelegramError("MALFORMED", "refusing to download an unacceptable file path")
        if not isinstance(max_bytes, int) or max_bytes < 1:
            raise TelegramError("MALFORMED", "invalid download bound")
        url = f"https://{TELEGRAM_HOST}/file/bot{self._token}/{file_path}"
        try:
            data = self._downloader(url, self.http_timeout, max_bytes)
        except TelegramError as error:
            raise TelegramError(error.category, sanitize(error.message, self._token), http_status=error.http_status) from None
        except Exception as error:  # noqa: BLE001 - every transport failure is sanitized
            category, message = _classify_exception(error)
            raise TelegramError(category, sanitize(message, self._token)) from None
        if not isinstance(data, (bytes, bytearray)):
            raise TelegramError("MALFORMED", "download returned no bytes")
        if len(data) > max_bytes:
            raise TelegramError("OVERSIZE", f"file exceeds the {max_bytes}-byte limit (streaming overrun)")
        return bytes(data)

    def send_message(self, chat_id: int, text: str, *, reply_markup: dict[str, Any] | None = None, parse_mode: str | None = None) -> dict[str, Any]:
        if len(text) > MAX_MESSAGE_CHARS:
            raise TelegramError("MALFORMED", "message exceeds 4096 characters")
        params: dict[str, Any] = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
        if parse_mode:
            params["parse_mode"] = parse_mode
        if reply_markup:
            params["reply_markup"] = reply_markup
        result = self.call("sendMessage", params)
        if not isinstance(result, dict):
            raise TelegramError("MALFORMED", "sendMessage returned no message")
        return result

    def send_document(self, chat_id: int, filename: str, data: bytes, *, caption: str | None = None, mime: str = "text/markdown") -> dict[str, Any]:
        """stdlib multipart/form-data upload to the fixed sendDocument endpoint (bounded, no token in errors)."""
        if not isinstance(data, (bytes, bytearray)) or not data:
            raise TelegramError("MALFORMED", "document is empty")
        if len(data) > 50 * 1024 * 1024:
            raise TelegramError("MALFORMED", "document exceeds Telegram's 50 MiB limit")
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,120}", filename):
            raise TelegramError("MALFORMED", "document filename has an invalid shape")
        boundary = "herdrb" + uuid.uuid4().hex
        parts: list[bytes] = []

        def field(name: str, value: str) -> None:
            parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode("utf-8"))

        field("chat_id", str(chat_id))
        if caption:
            field("caption", caption[:1024])
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"document\"; filename=\"{filename}\"\r\nContent-Type: {mime}\r\n\r\n".encode("utf-8") + bytes(data) + b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode("utf-8"))
        result = self.call("sendDocument", body=b"".join(parts), content_type=f"multipart/form-data; boundary={boundary}", timeout=self.http_timeout + 30)
        if not isinstance(result, dict) or not isinstance(result.get("message_id"), int):
            raise TelegramError("MALFORMED", "sendDocument returned no message")
        return result

    def answer_callback_query(self, callback_query_id: str, text: str) -> None:
        self.call("answerCallbackQuery", {"callback_query_id": callback_query_id, "text": text[:200]})

    def edit_reply_markup(self, chat_id: int, message_id: int) -> None:
        """Remove buttons from an old card; failures are non-fatal for callers."""
        self.call("editMessageReplyMarkup", {"chat_id": chat_id, "message_id": message_id, "reply_markup": {"inline_keyboard": []}})


def network_probe(*, timeout: float = 10.0, resolver: Callable[..., Any] = socket.getaddrinfo, connector: Callable[..., Any] | None = None) -> dict[str, Any]:
    """Token-free DNS + verified TLS check to the fixed Telegram host. Changes no bot state."""
    try:
        resolver(TELEGRAM_HOST, 443, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return {"status": "DNS_FAILURE", "detail": "name resolution failed"}
    except OSError as error:
        return {"status": "DNS_FAILURE", "detail": f"resolver error ({type(error).__name__})"}
    try:
        if connector is not None:
            connector(TELEGRAM_HOST, 443, timeout)
        else:
            context = ssl.create_default_context()
            with socket.create_connection((TELEGRAM_HOST, 443), timeout=timeout) as raw:
                with context.wrap_socket(raw, server_hostname=TELEGRAM_HOST):
                    pass
    except ssl.SSLError:
        return {"status": "HTTPS_FAILURE", "detail": "tls verification failed"}
    except (OSError, TimeoutError) as error:
        return {"status": "HTTPS_FAILURE", "detail": f"transport failed ({type(error).__name__})"}
    return {"status": "HEALTHY", "detail": "dns and verified https ok (token-free)"}
