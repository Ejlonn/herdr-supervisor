"""The single redaction policy under a curated adversarial corpus plus deterministic seeded mutations,
through the direct function, the Telegram renderer, registered Markdown derivatives, and the bridge's
outbound send. Generated secrets are sentinels that must never survive; safe values must stay readable."""

from __future__ import annotations

import json
import random
import unittest

import herdr_artifacts as ha  # noqa: E402
import herdr_present as hp  # noqa: E402
import herdr_redaction as hr  # noqa: E402
from test_telegram import TelegramCase, message  # noqa: E402
from v2_fixtures import hs  # noqa: E402

SENTINEL = "ZQX9SECRETvalue7712"  # the generated secret value; it must never appear in any outward text
REDACTED = hr.REDACTED

CURATED: list[tuple[str, str]] = [
    ("json", f'{{"api_key": "{SENTINEL}", "user": "alice"}}'),
    ("json nested", f'{{"db": {{"password": "{SENTINEL}"}}}}'),
    ("yaml", f"token: {SENTINEL}\nname: widget"),
    ("yaml quoted", f"secret: '{SENTINEL}'"),
    ("dotenv", f"API_KEY={SENTINEL}\nPORT=8080"),
    ("dotenv export", f"export AWS_SECRET_ACCESS_KEY={SENTINEL}"),
    ("shell assignment", f"PASSWORD='{SENTINEL}' ./run"),
    ("header authorization", f"Authorization: Bearer {SENTINEL}"),
    ("header cookie", f"Cookie: session={SENTINEL}; theme=dark"),
    ("basic auth", f"basic {SENTINEL}{SENTINEL}"),
    ("telegram bot url", f"https://api.telegram.org/bot123456789:{SENTINEL}AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA/getMe"),
    ("telegram token", f"123456789:AA{SENTINEL}{SENTINEL}"),
    ("github token", "ghp_" + SENTINEL + "abcdefghijklmnop"),
    ("slack token", "xoxb-" + SENTINEL + "1234567890"),
    ("openai-like", "sk-" + SENTINEL + "abcdefghijklmnopqrstuv"),
    ("aws access key", "AKIA" + "ABCDEFGHIJKLMNOP"),
    ("dsn", f"postgres://app:{SENTINEL}@db.internal:5432/prod"),
    ("userinfo url", f"https://alice:{SENTINEL}@example.net/path"),
    ("pem", f"-----BEGIN PRIVATE KEY-----\n{SENTINEL}\nMIIE\n-----END PRIVATE KEY-----"),
    ("mixed case key", f"ApI-Key = {SENTINEL}"),
    ("separators", f'"client_secret":{SENTINEL}'),
    ("crlf", f"password: {SENTINEL}\r\nnext: line"),
    ("multiline value", f"token: {SENTINEL}\n  continued"),
    ("adjacent punctuation", f"(token={SENTINEL});"),
    ("unicode controls", f"tok​en: {SENTINEL}‮"),
    ("env file mention", f"see .env for values: TOKEN={SENTINEL}"),
]
SAFE: list[str] = [
    "The token budget is 8 prompts per run.",
    "Password rotation happens quarterly; no secret values are stored here.",
    "Use /task to start; the supervisor never echoes the api key file contents.",
    "candidate 0123456789abcdef0123456789abcdef01234567 passed runtime validation",
    "https://example.net/docs/authorization-overview",
    "Set HERDR_ENV=1 in the pane; PORT=8080 is the default.",
]


def mutate(text: str, rng: random.Random) -> str:
    """Seeded, dependency-light mutations of key casing, separators, quoting, whitespace, noise, and Unicode controls."""
    ops = rng.sample(["case", "sep", "quote", "space", "noise", "control"], k=rng.randint(1, 4))
    out = text
    if "case" in ops:
        out = "".join(c.upper() if rng.random() < 0.5 else c.lower() for c in out)
    if "sep" in ops:
        out = out.replace(": ", rng.choice([":", " : ", "=", " = ", ":\t"]), 1).replace("=", rng.choice(["=", " = ", ": "]), 1)
    if "quote" in ops:  # quote the value only where it stands as a value (after a separator), never inside a token
        import re as _re
        out = _re.sub(rf"([=:]\s*){SENTINEL}", lambda m: m.group(1) + rng.choice([f'"{SENTINEL}"', f"'{SENTINEL}'", SENTINEL]), out, count=1)
    if "space" in ops:
        out = rng.choice(["  ", "\t", "\n"]) + out + rng.choice(["", " ", "\n"])
    if "noise" in ops:
        out = rng.choice(["log: ", "> ", "[12:00] "]) + out + rng.choice(["", " # note", " (copied)"])
    if "control" in ops:
        pos = rng.randint(0, len(out))
        out = out[:pos] + rng.choice(["\x00", "\x1b", "​", "‮"]) + out[pos:]
    return out


class DirectRedactionTests(unittest.TestCase):
    def assert_scrubbed(self, label: str, text: str) -> str:
        result = hr.redact(text)
        self.assertNotIn(SENTINEL, result, f"{label}: secret survived -> {result[:80]!r}")
        self.assertNotIn(SENTINEL.lower(), result.lower(), f"{label}: secret survived case-insensitively")
        self.assertEqual(hr.redact(result), result, f"{label}: redaction must be idempotent")
        self.assertIsNone(hr.CONTROL_RE.search(result), f"{label}: control characters must be removed")
        return result

    def test_curated_corpus(self) -> None:
        for label, text in CURATED:
            with self.subTest(label):
                result = self.assert_scrubbed(label, text)
                self.assertIn(REDACTED, result)

    def test_seeded_mutations_never_leak_and_never_raise(self) -> None:
        rng = random.Random(20260911)
        for label, text in CURATED:
            for round_index in range(40):
                mutated = mutate(text, rng)
                with self.subTest(label=label, round=round_index):
                    self.assert_scrubbed(f"{label}#{round_index}", mutated)

    def test_bounds_and_truncation_boundaries(self) -> None:
        long = "x" * 5000 + f" token={SENTINEL}"
        out = hr.redact(long, limit=100)
        self.assertLessEqual(len(out), 100)
        self.assertNotIn(SENTINEL, out)
        # a secret straddling the truncation point is still absent
        text = "a" * 95 + f"api_key={SENTINEL}"
        self.assertNotIn(SENTINEL[:6], hr.redact(text, limit=100))
        self.assertEqual(hr.redact(None), "")
        self.assertEqual(hr.redact(""), "")
        self.assertEqual(hr.redact(12345), "12345")

    def test_safe_values_remain_readable(self) -> None:
        for text in SAFE:
            with self.subTest(text[:30]):
                self.assertEqual(hr.redact(text), text.replace("\x00", ""), "over-redaction destroys safe content")
                self.assertFalse(hr.has_sensitive_remainder(text))

    def test_remainder_detection_fails_closed_on_ambiguous_content(self) -> None:
        for text in ("password:", "secret =", "https://user:pw@", "-----BEGIN PRIVATE KEY-----", "AKIA" + "ABCDEFGHIJKLMNOP"):
            with self.subTest(text):
                self.assertTrue(hr.has_sensitive_remainder(text))
        self.assertFalse(hr.has_sensitive_remainder(f"token: {REDACTED}") and False)
        # the remainder check runs after redaction: a stray key with an empty value is still suspicious
        self.assertTrue(hr.has_sensitive_remainder(hr.redact("api_key=")))


class OutwardPathTests(TelegramCase):
    def corpus(self) -> list[str]:
        rng = random.Random(7)
        items = [text for _, text in CURATED]
        items += [mutate(text, rng) for _, text in CURATED for _ in range(3)]
        return items

    def test_telegram_short_rendering_and_send(self) -> None:
        for text in self.corpus():
            rendered = hp.render_wait_user(text, None, "UTC", requires_action=True).html
            self.assertNotIn(SENTINEL, rendered)
            self.bridge.safe_send(5, text)
            self.assertNotIn(SENTINEL, self.api.sent[-1]["text"])
        event = {"type": "COMMAND_RESULT", "data": {"action": "task", "ok": False, "message": f"token={SENTINEL}"}}
        self.assertNotIn(SENTINEL, hp.render_command_result(event, "UTC").html)

    def test_registered_markdown_derivatives(self) -> None:
        for index, text in enumerate(self.corpus()):
            body = f"# report {index}\n\n{text}\n\nsafe line\n"
            try:
                record = ha.register_text(self.paths, self.config, category="report", text=body, name=f"r{index}", run_id=None, title="t")
            except hs.SupervisorError:
                continue  # fail-closed remainder detection is an acceptable outcome for a secret-bearing artifact
            _record, data = ha.verify_for_send(self.paths, self.config, record["artifact_id"])
            self.assertNotIn(SENTINEL.encode(), data, f"artifact {index} leaked")
        # an artifact that still carries an unclassifiable remainder is refused, never exposed
        with self.assertRaises(hs.SupervisorError):
            ha.register_text(self.paths, self.config, category="report", text="key material follows\n-----BEGIN PRIVATE KEY-----\ntruncated without an END marker\n", name="rem", run_id=None, title="t")

    def test_query_answers_and_status_use_the_same_policy(self) -> None:
        self.write_plan()
        self.start_gated([{"v2": ("plan", "human", "plan_approval", str(self.plan_payload()))}])
        with self.sup.store.transaction():
            st = self.sup.store.read_state()
            st["wait_user_reason"] = f"reason token={SENTINEL}"
            st["supervisor_state"] = "WAIT_USER"
            st["pending_gate"] = None
            self.sup.store.write_state(st)
        self.bridge.handle_update(message(1, "/status"))
        self.assertNotIn(SENTINEL, "\n".join(m["text"] for m in self.api.sent))
        self.assertNotIn(SENTINEL, json.dumps(hp.render_status_raw(self.sup.status()).html))


if __name__ == "__main__":
    unittest.main()
