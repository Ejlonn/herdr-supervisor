# Telegram setup and security

Create a private bot with BotFather and obtain your numeric Telegram user ID and private chat ID. Do not place the token in a command line, source file, environment variable, repository, or Herdr prompt.

Run:

```sh
herdr-telegram setup --owner-user-id YOUR_NUMERIC_ID --chat-id YOUR_PRIVATE_CHAT_ID
chmod 0600 ~/.config/herdr-telegram/config.json ~/.config/herdr-telegram/bot-token
herdr-telegram doctor --network
```

Setup prompts for the token without echoing it and starts in `shadow` mode. The doctor distinguishes unconfigured credentials, DNS/HTTPS failures, authentication failure, webhook conflict, duplicate long-poll conflict when detectable, and healthy access. A webhook conflict fails closed; the tool never deletes a webhook automatically.

The bridge opens no listener and requires no public URL, SSH path, VPN route, or inbound firewall rule. The server only needs outbound TLS access to `api.telegram.org:443`.

The task-start card shows `Task to start:` with the typed task text (or the validated filename and bounded excerpt of an uploaded file) before the optional reset policy. Its buttons are `Start · N` — N is the maximum automatic banked-reset budget for the run, 0 is the default — at most two per row, then a full-width Cancel; `/reset-budget N` covers larger inventories. Every callback stays bound to the pending task hash, owner, chat, expiry, and fresh inventory; file contents never enter callback records or command journals.

When a settled, accepted turn yields no readable routing result, the card leads with the outcome — the agent finished, Supervisor received no workflow result, no approval or next step was created — and explains each button: `Retry routing result` (one-time, bound to that run and turn; rereads the settled transcript, submits nothing, wakes nothing, cannot duplicate a gate), `Ask agent`, `Request revision`, and `Cancel task`. If the reread finds nothing the retry button is gone and the card says so. Turn ids, delivery states, and transcript terms stay out of the card; `/status raw` and the logs keep them.

`Ask agent` is distinct from `Request revision`. It is offered at every pending gate card and at the unread-result card, and by `/ask-agent <question>`. The button opens a one-message reply intent bound to the exact run, decision (gate id and artifact/payload hashes, or the unread turn), owner, chat, and expiry; the reply becomes one durable `ask_agent` command that the worker validates again against live state before preparing anything. The worker persists the suspended decision, sends exactly one purpose-built read-only prompt to the same active native session (never a replacement identity), verifies the hash-bound Markdown response the agent wrote below the review directory, registers it through the artifact registry, restores the decision unchanged, and emits one `AGENT_FOLLOWUP_READY` event. The card then says who answered and that the decision is unchanged, attaches the document, and shows the restored decision's own controls. Nothing succeeds asynchronously before the worker resolves; a pending gate card that has not been delivered yet is held, not superseded, while a question is out.

If no verified answer comes back (no frame, wrong hash, wrong decision, unsafe path, or an uncertain delivery), the bridge shows `Answer not verified`: the question may have reached the agent, the decision is unchanged, and the buttons are `Retry reading answer` (once, no prompt), `Return to decision` (restores the suspended gate or wait without contacting anyone), and `Request revision` (abandons the question and voids the decision through the ordinary revision path). `/resume` cannot bypass an unresolved question.

The generic action-required wait card (`Task stopped: your decision is needed`) likewise states that no approval or next step was created and explains `Request revision` and `Cancel task`; the former `Send Guidance` label is gone because that action always was a revision request.

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
