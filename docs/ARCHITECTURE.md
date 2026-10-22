# Architecture

The supervisor is the single authority for task state, routing, native session ownership, quota waits, delivery certainty, and human gates. Herdr supplies lifecycle and session operations. Codex and Claude produce plans, implementations, and reviews, but neither agent polls the other.

Telegram is an independently restartable control plane. It uses outbound Bot API long polling, validates an exact private owner/chat pair, persists update offsets, and submits typed commands to the supervisor. It never runs raw pane commands or injects Enter/Yes. Network failure leaves an active task untouched and durable notifications queued.

Worker waiting uses Herdr lifecycle waits. While an agent owns a turn, inactive LLMs do not poll, read, or monitor it. Output is read once after a meaningful lifecycle transition. Quota revalidation invokes provider-local deterministic refresh mechanisms and consumes no model tokens.

Human gates bind run, turn, gate, expected state, expiry, and authoritative artifact hash. Actions are opaque, one-time, and revalidated against live state. Unknown or conflicting data fails closed.

Protocol safety and workflow procedure are separate. Every V2 task resolves an immutable `workflow_contract` once at start (`herdr_validation.resolve_workflow_contract`, a pure function of the requested policy) and persists it in state; routing, gate creation, and completion consult only that persisted copy, never mutable configuration, so an active run's meaning cannot change from a config edit or a restart. `gated_v2` and `supervised_v2` share the same typed-gate protocol, loop/auto-turn limits, delivery-certainty handling, and quota/reset-recovery strictness (protocol safety, extended to both). Only the contract fields differ (workflow procedure): whether a plan approval is required before an implementation handoff or before `done`, which agents may start or raise a plan gate, whether a typed gate is mandatory when routing to a human, and the plan artifact's required filename (`CODEX_PLAN.md` for `gated_v2`; any safe `.md` file below the review root for `supervised_v2`). `v1` predates the V2 protocol and contract model entirely and carries no contract; it is retained only for already-migrated historical state.

At a new-task boundary, the owner may preserve both recorded sessions or create selected providers in new panes with bounded configured model profiles. Preparation binds the task/options and complete pre-operation owner set, persists intent before each pane/start side effect, and journals a returned pane before probing it; ambiguous calls are never repeated. Every old and fresh identity is reverified before one atomic `owners.json` update. Direct CLI retries reconcile the same unfinished task-bound operation. Active runs remain pinned to their original exact native IDs, and their pane locators change only through the explicit, bound owner-recovery repair (structured `owner_recovery` wait → preview → apply under the worker lock); automatic recovery restores a missing session only inside its recorded pane. Fresh Codex is thread-first: intent (`THREAD_CREATING`, request id + exact payload) is journaled, the helper's `thread/start` result (`THREAD_CREATED`, sanitized id/model/provider) is persisted before the pane split, the pane resumes that exact thread, and identity verification requires the reported native session to equal the pre-created id. Thread creation is not idempotent, so an unknown outcome stays reconciling until a settled helper result is adopted or the operator abandons the preparation; the helper itself records an interrupted `thread_start` request as an uncertain result rather than resending it.

Runtime evidence carries execution provenance. In the default collaborative mode agents produce a durable `runtime_proposal`; only an authenticated operator confirmation (Telegram callback or the gate-bound `runtime-confirm` CLI) turns it into canonical `runtime_evidence` with `provenance`. Automatic agent recording exists only under a plan payload the human approved with `runtime_validation_mode: automatic_agent`. Evidence without provenance on a still-pending gate is treated as an unconfirmed proposal; evidence whose gate already closed remains accepted history.

Optional backup jobs are independent deterministic user services. They read configured scopes, create staged snapshots, verify checksums, publish a completion marker and latest pointer, and report health. They do not prompt agents or mutate workflow state.

Codex banked resets are explicit per-run authority. Before a Telegram task starts, the bridge reads the supported Codex app-server inventory and records a protected pending start. The owner chooses a budget from zero through the displayed count; the callback is bound to the owner, chat, pending task hash, account fingerprint, expiry, and inventory view. CLI starts use `--codex-reset-budget N`; the default is zero.

The supervisor worker keeps its network-denied sandbox. Reset inventory and consume operations cross a protected request/result journal to the narrow `herdr-codex-reset-worker` user service, which alone may use Codex's supported `account/rateLimits/read` and `account/rateLimitResetCredit/consume` methods. Each consume intent and stable idempotency key is durable before the request. A credit is considered only for an authoritative blocking Codex quota event; recovery must be verified through fresh non-LLM quota reads before the same run and native session continue. An uncertain operation blocks further credits and never causes a new key or prompt replay.

## Modules

Flat modules under `src/`, one-way dependencies (each imports only modules above it), one implementation per symbol:

- `herdr_core.py` — constants, error types, `Paths`, configuration defaults/loading, atomic JSON I/O, hashes, the worker lock, bounded numeric helpers, and the single `SUPERVISOR_VERSION`.
- `herdr_cli.py` — the Herdr CLI adapter, error-code parsing, native session identity, and the read-only version/capability contract probe (`MIN_HERDR_VERSION`, `REQUIRED_CAPABILITIES`).
- `herdr_quota.py` — quota snapshots, blocking windows, resume timing, report conversion.
- `herdr_protocol.py` — the eight-key routing protocol: width-independent frame reconstruction and run/turn-bound parsing; the separate seven-key follow-up response frame (never accepted by the router, never accepts a routing block).
- `herdr_redaction.py` — the single redaction and sensitive-remainder policy used by every outward path.
- `herdr_validation.py` — state defaults, migration and validation, the durable `StateStore`, and the safe-file, task, upload, gate-payload, runtime-evidence, and query-registry validators.
- `herdr_workflow.py` — V2 events, gates, inbox/outbox, approvals, operator handoff, missing-result retry, the gate-preserving agent follow-up (suspend → one prompt → verified hash-bound response → restore), recovery (mixin).
- `herdr_sessions.py` — bounded new-task session/model policy, provider capability probing (model flag; Codex app-server/`thread/start`/`resume` chain), durable fresh preparation (thread-first for Codex), exact identity verification, and atomic ownership binding.
- `herdr_runtime.py` — the supervisor engine: identity-safe command targets, delivery lifecycle, quota/reset reconciliation, thread-start via the helper journal, run/resume/monitor loop, status and doctor data (runtime evidence view, product-repository validity, Fresh Codex components).
- `herdr_command.py` — argument parser, terminal output, control commands, `main`.
- `herdr_supervisor.py` — compatibility facade and console entry point re-exporting the supported names.
- Companions: `herdr_telegram.py` (bridge), `herdr_query.py` (read-only query worker), `herdr_backup.py`, `herdr_artifacts.py`, `herdr_present.py` (rendering), `herdr_codex_reset.py` (banked-reset helper), `telegram_api.py` (HTTPS client).

Command targets are identity-safe: the configured alias is used only while it names the persisted native session; when the alias is absent, the pane of the single live record with the persisted provider and exact session id is used, a changed pane only updates the recorded locator, and provider kind, pane, title, recency, or sole-agent presence never authorize a target.

Opt-in session/helper extensions: `herdr_session_catalog.py` normalizes the live
inventory and extracts bounded explicit native-child metadata;
`herdr_helpers.py` owns a separate helper registry and at-most-once job journal.
Neither module changes workflow owner/gate state. See [sessions and helpers](SESSIONS.md).

`herdr_native_results.py` is the bounded, read-only recovery of an exact primary session's final
routing result from its provider-native transcript. The supported terminal read stays first; the native
transcript is consulted only when it holds no valid current frame for an accepted V2 turn. The file is found
only by the exact owned session id below the provider's configured root (or by the validated root-relative path
recorded at delivery time), never from a payload path, and its own metadata must prove it is the owned primary
session (Claude: every user/assistant record primary with the owned `sessionId`; Codex: a `session_meta` whose
id and plain-string source match). It accepts only a primary final (Claude `end_turn` with `isSidechain=false`;
Codex `final_answer` completed by `task_complete`) after the delivered prompt that carries the turn id, and only
when the records after that prompt were read completely: a partial, malformed, oversized or unsupported
result-bearing record there (a final whose content, phase, role, turn id or payload container has an unsupported
shape; a record type outside the documented list) makes the result `uncertain`, never the earlier frame, while
valid empty content and documented non-result records stay harmless. The sampled file extent (size, identity,
modification time) is re-checked after the read and parse: growth, truncation, replacement or an in-place rewrite
during the read makes the result `uncertain` (the bound is never extended to chase a writer; a later retry on the
settled file decides). A Codex record type that a newer CLI adds fails closed until it is classified. One budget (directory
entries, wall-clock seconds, bytes, records) covers discovery, metadata, the window read and parsing. It routes
nothing itself: the controller re-verifies ownership and settlement and then uses the ordinary parser, route and
gate path once.

`herdr_role_readiness.py` reports subagent-role readiness without inventing it: definitions installed (names and
hashes), the parent's exact owned session and the provider CLI's version, operator-reported observations (authenticated
by the local operator account, bound to that exact session and version, never execution authority) and selectability,
which stays UNKNOWN unless a current operator observation of the parent's Agent tool catalog exists. A session or version
change invalidates any observation (a restart of the same session id with the same CLI version is not detected). The
parent is verified by the control path's own predicate (exact owned session in its recorded pane, no duplicate carrier). It invokes no agent, restarts nothing and edits no definition or permission.

`herdr_native_identity.py` is the Supervisor-owned Codex SessionStart hook. It
accepts nullable transcript paths and reports identity only when the event is
canonical and its pane hosts a local `--no-daemon` Codex client. Shared-server
contexts and native subagent events are rejected; no prompts or transcript bodies
are read. The exact hook definition requires native Codex trust review.

`herdr_onboarding.py` provides offline bounded project discovery, staged instruction/skill/profile proposals and fingerprint-bound application. Its CLI dispatch precedes live config, Herdr and Supervisor construction. It does not activate services or alter workflow state.

`herdr_skill_baseline.py` contains the versioned portable skill baseline and its content-hash manifest. Onboarding stages isolated copies and binds their identity into approval; it never reads global installed skills.

`herdr_support_skills.py` stages explicit optional local skill snapshots, including binary data, helpers, references and licenses. Reviewed proposals pin the complete resource inventory; apply reads staged bytes, never global skill paths, and never executes helpers.
