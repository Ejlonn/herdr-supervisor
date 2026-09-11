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
