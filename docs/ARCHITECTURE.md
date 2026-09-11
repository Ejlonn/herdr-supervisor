# Architecture

The supervisor is the single authority for task state, routing, native session ownership, quota waits, delivery certainty, and human gates. Herdr supplies lifecycle and session operations. Codex and Claude produce plans, implementations, and reviews, but neither agent polls the other.

Telegram is an independently restartable control plane. It uses outbound Bot API long polling, validates an exact private owner/chat pair, persists update offsets, and submits typed commands to the supervisor. It never runs raw pane commands or injects Enter/Yes. Network failure leaves an active task untouched and durable notifications queued.

Worker waiting uses Herdr lifecycle waits. While an agent owns a turn, inactive LLMs do not poll, read, or monitor it. Output is read once after a meaningful lifecycle transition. Quota revalidation invokes provider-local deterministic refresh mechanisms and consumes no model tokens.

Human gates bind run, turn, gate, expected state, expiry, and authoritative artifact hash. Actions are opaque, one-time, and revalidated against live state. Unknown or conflicting data fails closed.

Optional backup jobs are independent deterministic user services. They read configured scopes, create staged snapshots, verify checksums, publish a completion marker and latest pointer, and report health. They do not prompt agents or mutate workflow state.

Codex banked resets are explicit per-run authority. Before a Telegram task starts, the bridge reads the supported Codex app-server inventory and records a protected pending start. The owner chooses a budget from zero through the displayed count; the callback is bound to the owner, chat, pending task hash, account fingerprint, expiry, and inventory view. CLI starts use `--codex-reset-budget N`; the default is zero.

The supervisor worker keeps its network-denied sandbox. Reset inventory and consume operations cross a protected request/result journal to the narrow `herdr-codex-reset-worker` user service, which alone may use Codex's supported `account/rateLimits/read` and `account/rateLimitResetCredit/consume` methods. Each consume intent and stable idempotency key is durable before the request. A credit is considered only for an authoritative blocking Codex quota event; recovery must be verified through fresh non-LLM quota reads before the same run and native session continue. An uncertain operation blocks further credits and never causes a new key or prompt replay.
