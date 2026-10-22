# Telegram setup and security

After reboot, use the [recovery checks](RECOVERY.md#telegram-after-reboot). Doctor distinguishes Bot API access from actual bridge polling health. Pending outbound failures are processed in bounded batches after inbound polling; queued documents retain their existing uncertainty and replacement limits.

Create a private bot with BotFather and obtain your numeric Telegram user ID and private chat ID. Do not place the token in a command line, source file, environment variable, repository, or Herdr prompt.

Run:

```sh
herdr-telegram setup --owner-user-id YOUR_NUMERIC_ID --chat-id YOUR_PRIVATE_CHAT_ID
chmod 0600 ~/.config/herdr-telegram/config.json ~/.config/herdr-telegram/bot-token
herdr-telegram doctor --network
```

Setup prompts for the token without echoing it and starts in `shadow` mode. The doctor distinguishes unconfigured credentials, DNS/HTTPS failures, authentication failure, webhook conflict, duplicate long-poll conflict when detectable, and healthy access. A webhook conflict fails closed; the tool never deletes a webhook automatically.

The bridge opens no listener and requires no public URL, SSH path, VPN route, or inbound firewall rule. The server only needs outbound TLS access to `api.telegram.org:443`.

The task-start card shows `Task to start:` with the typed task text (or the validated filename and bounded excerpt of an uploaded file), a **Workflow: Supervised / Plan approval** choice, before the optional reset policy. Its buttons are `Start · N` — N is the maximum automatic banked-reset budget for the run, 0 is the default — at most two per row, then a full-width Cancel; `/reset-budget N` covers larger inventories. Every callback stays bound to the pending task hash, owner, chat, expiry, fresh inventory, and the selected workflow policy; file contents never enter callback records or command journals.

### Workflow choice

`Workflow: Supervised` (`supervised_v2`, checked by default unless configuration sets `default_workflow_policy` to `gated_v2`) starts the general policy: planning is optional and either provider may work. `Workflow: Plan approval` (`gated_v2`) is Prospera's strict Codex-plans-first procedure, unchanged. Tapping either button updates the pending task's recorded choice, bumps its view revision, and redraws the confirmation card — the same staleness rule as the session-policy and model-profile buttons: a `Start` button minted against an older revision or a different workflow choice is rejected as a stale binding rather than silently starting under the wrong policy. `/status` and `/status raw` show the resolved `workflow_policy` for the active run once it starts, and it never changes for that run afterward.

Under `Workflow: Supervised` only, a second choice — `Start with: Codex` / `Start with: Claude` — appears (Codex checked by default), letting the operator pick either provider for the first turn, exactly as the plan requires. Switching the workflow choice back to `Plan approval` immediately normalizes the recorded starting agent back to Codex and removes the choice from the redrawn card: `gated_v2` never offers or accepts a Claude start. Both choices are bound together into the pending record, the interaction revision, the final `Start` button, the enqueued command, the reset-budget authorization binding, and the initialized run's own `start_agent`; a stale or tampered value at any of those points is rejected before a run is created.

When a settled, accepted turn yields no readable routing result, the card leads with the outcome — the agent finished, Supervisor received no workflow result, no approval or next step was created — and explains each button: `Retry routing result` (one-time, bound to that run and turn; rereads the settled transcript, submits nothing, wakes nothing, cannot duplicate a gate), `Ask agent`, `Request revision`, and `Cancel task`. If the reread finds nothing the retry button is gone and the card says so. Turn ids, delivery states, and transcript terms stay out of the card; `/status raw` and the logs keep them.

`Ask agent` is distinct from `Request revision`. It is offered at every pending gate card and at the unread-result card, and by `/ask-agent <question>`. The button opens a one-message reply intent bound to the exact run, decision (gate id and artifact/payload hashes, or the unread turn), owner, chat, and expiry; the reply becomes one durable `ask_agent` command that the worker validates again against live state before preparing anything. The worker persists the suspended decision, sends exactly one purpose-built read-only prompt to the same active native session (never a replacement identity), verifies the hash-bound Markdown response the agent wrote below the review directory, registers it through the artifact registry, restores the decision unchanged, and emits one `AGENT_FOLLOWUP_READY` event. The card then says who answered and that the decision is unchanged, attaches the document, and shows the restored decision's own controls. Nothing succeeds asynchronously before the worker resolves; a pending gate card that has not been delivered yet is held, not superseded, while a question is out.

If no verified answer comes back (no frame, wrong hash, wrong decision, unsafe path, or an uncertain delivery), the bridge shows `Answer not verified`: the question may have reached the agent, the decision is unchanged, and the buttons are `Retry reading answer` (once, no prompt), `Return to decision` (restores the suspended gate or wait without contacting anyone), and `Request revision` (abandons the question and voids the decision through the ordinary revision path). `/resume` cannot bypass an unresolved question.

The generic action-required wait card (`Task stopped: your decision is needed`) likewise states that no approval or next step was created and explains `Request revision` and `Cancel task`; the former `Send Guidance` label is gone because that action always was a revision request.

Fresh-session choices appear on the task-start card only while the policy's complete capability chain is verified (Fresh Claude: the Herdr pane/agent contract; Fresh Codex: additionally the Codex app-server, `thread/start` schema, and `resume SESSION_ID`); the same predicate is rechecked at callback time and again by the worker before any side effect. A failed preparation reports that no task was delivered, names the preserved old sessions in abbreviated form, and offers the recovery choices (retry the same start, abandon the preparation, or start with preserved sessions).

Runtime validation cards: the `Runtime validation required` card explains that the agent proposes evidence and the operator records it. When the agent submits evidence, a `Runtime PASS/FAIL proposed — your confirmation is needed` card shows the bounded command results and offers `Record PASS` or `Record FAIL` — only the result the evidence bytes state — bound to the run, gate, candidate, environment, and evidence hash, plus `Request Revision`, `Ask agent`, and `Status`. Nothing is recorded, no push gate is created, and no success message is shown until the worker applies the authenticated confirmation; a replaced proposal, a revised gate, or another run makes the button inert. `/status` distinguishes `PASS proposed (unconfirmed)` from `PASS accepted` and says who must act next and why the task is still waiting.

`Session pane ownership needs repair` cards say which saved session is duplicated, moved, or lost its recorded pane, that no other conversation was adopted and nothing was resent, and what to do: close duplicate panes first, then `Refresh repair preview`; the preview result offers `Apply pane repair` only when exactly one idle carrier of the same session exists in another pane (bound to the run, recovery id, and pane), reports "in place" when the recorded pane already holds it (Resume), or names what must change first. No plan/revision buttons appear on these cards.

`/defer [note]` offers to defer a pending runtime or push gate and hand the settled run off: it sends a confirmation card bound to the run, the gate, the settled turn, the candidate and the unmet obligations, and enqueues nothing until you tap it. The applying worker repeats the full eligibility check (live agent, child-settlement sample, unchanged plan and candidate); nothing is sent to an agent and no PASS or push approval is recorded.

`/done [note]` is the operator-handoff completion. The bridge only enqueues it when the supervisor's own eligibility rule reports a handoff-ready wait (the agent routed `done` and nothing but your runtime validation or push approval is missing); the detached worker repeats that rule with live agent inspection before recording the completion. The Done button is offered only in that state and is bound to the exact run, wait, and readiness turn, so it is inert after any state change. The closing message and `/status` state that the remaining actions are yours and unverified; no push approval or runtime evidence is created.

Uploaded `.md` and `.txt` tasks are untrusted data. They are owner/chat checked, size/type checked, stored under opaque non-overwriting names, previewed, and started only after explicit confirmation. Long outputs use only registered, redacted workflow artifacts; arbitrary filesystem retrieval is unsupported.

Provision each read-only query session in a separate Herdr pane, using the launch contract shown in
`config/supervisor.example.json`. For example, after placing an unused pane at a shell prompt:

```sh
herdr agent start codex-query --kind codex --pane PANE_ID -- \
  --sandbox read-only --ask-for-approval never --profile herdr-query -C "$HOME/workspace"
herdr-supervisor register-query --provider codex --agent-name codex-query \
  --acknowledge-read-only-contract

herdr agent start claude-query --kind claude --pane OTHER_PANE_ID -- --permission-mode plan
herdr-supervisor register-query --provider claude --agent-name claude-query \
  --acknowledge-read-only-contract
```

Use the configured project root in place of `$HOME/workspace`. Registration checks that the named agent
exists exactly once, is idle, has the expected kind and alias, has a stable native identity, and does not
reuse a workflow session. It records that exact identity but never creates or restores a session. The
acknowledgement confirms that you launched the agent with the displayed read-only contract; inspect the
agent before acknowledging it. Workflow sessions are never borrowed for `/ask`, and busy or quota-blocked
query providers are skipped. An action-like question is redirected to `/task`.

### Approval request repair

A malformed agent-authored gate is not a decision for the operator. Supervisor
asks the same session to repair its request once, using the normal quota and
confirmed-delivery safeguards. It does not replay the task or grant approval.
If repair fails again, **Repair approval request** requests another bound repair
without a revision note. Stale or duplicate clicks cannot send another prompt.
Failed prerequisites (for example, a review that has not passed) remain blocked.

Approval buttons for runtime and push gates also run the receiving controller's read-only binding predicates before anything is queued. A definite mismatch (changed plan, changed candidate, wrong configured repository, drifted manifest tree) is reported with the predicate and expected/observed values and is not queued; evidence the restricted bridge cannot read is left to the worker.

Agents can check a request without changing state:

```sh
herdr-supervisor gate-preflight --gate-type runtime_validation --payload /absolute/review/payload.json
```

Runtime requests may include `decision_question`, `approval_consequence`,
`decline_consequence`, and `expected_output` together. The card then offers
**Approve runtime test** and **Decline**. Approval permits the described checks;
it does not execute commands, record PASS, or authorize publishing. Run the
reviewed checks separately and submit evidence; collaborative results still
require operator confirmation.

Git candidates retain the full 40-character `candidate_sha`. Private runtime
candidates use `candidate_kind: manifest`, a full SHA-256 `candidate_digest`,
and `candidate_manifest` pointing to a JSON map of canonical relative file
paths to SHA-256 hashes. `repository` must equal the plan's approved
`runtime_manifest_root`, outside the product repository. The manifest must
cover every file; links, path escapes, duplicates, extra files and changed bytes
are rejected. Keep mutable runtime output outside that root. These requests
require all four explanation fields and cannot authorize a Git push. Evidence
uses `candidate_digest`; CLI evidence submission uses `--candidate-digest`.

`/sessions` is a read-only catalog of live panes, exact native identity problems,
and opt-in native/helper children. It never starts, adopts, renames or prompts an
agent. Separate helper mutations remain explicit CLI operations; see SESSIONS.md.
