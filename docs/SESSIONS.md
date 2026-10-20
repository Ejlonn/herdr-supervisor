# Sessions and helpers

`herdr-supervisor sessions --json` lists every observed agent, its role and exact
identity readiness. Telegram `/sessions` shows a bounded version. Missing native
identities and duplicate/moved sessions are reported, never guessed from a pane.
Configured model and verified observation are distinct; sidebar quota labels are
not proof of the model running a session.

Native children are observations, not separate workflow agents or pane targets.
Tracking is disabled by default. Enable `native_tracking.enabled`, set absolute
`roots`, and register `files` with `path` and `parent_session_id`. Then run
`sessions --refresh-native`. Reads are bounded and incremental; only metadata
with an explicit parent and a recorded supported version is accepted. Initial
Codex contract: 0.154.0 session_meta/thread_spawn. Unknown contracts (including
parent-less guardian records) are unavailable. Claude tracking is not live
validated. Token totals are unavailable rather than guessed or double-counted.

Separate helpers are disabled by default (`helpers.enabled: false`). To opt in,
set it to true. Start a dedicated Codex pane yourself with explicit
`--no-daemon --sandbox read-only --ask-for-approval never --model MODEL`. Its cwd and native
identity must be reported accurately by Herdr. Do not reuse a workflow/query
owner. The default limit is two active helper jobs; one job per worker.

```sh
herdr-supervisor helper register reader --pane PANE --model MODEL --cwd DIRECTORY
# Review the preview; add --apply --session-id UUID to register that exact previewed binding.
herdr-supervisor helper list
herdr-supervisor helper dispatch reader --instructions /your/review-root/job.md
herdr-supervisor helper collect JOB_UUID
herdr-supervisor helper cancel JOB_UUID
```

Dispatch requires a settled parent at a human gate or paused checkpoint, an
unchanged instruction artifact, and a verified read-only fixed-model helper.
Helpers cannot approve gates, change models, spawn more helpers, or finish the
parent task. Results are bounded redacted evidence for the parent to review.
Write-mode helper jobs are not supported in this release.

Accepted/uncertain/submitting jobs are never resent after restart. `collect`
checks the settled pane once; busy or blocked helpers require waiting first.
Cancellation sends no terminal input and keeps the worker's slot reserved until
`collect` verifies settlement. Identity drift requires resolving the drift and
explicit registration; parent/instruction/deadline changes require cancel then
collect after settlement. A changed session cannot be used to settle an old job. For a permanently lost
worker, `helper cancel JOB_UUID --abandon` explicitly releases the slot and
quarantines the worker; it can be registered again only after verified settlement.

These features borrow attempt bookkeeping and action binding concepts from
[baton-herdr](https://github.com/selorito/baton-herdr), inspected at
0fd3ecb20b6d312dfb65df03520913246ea3fd5b. They do not install baton or introduce
its automatic provider handoff or implicit pane relocation.

Fresh Codex launches use `resume THREAD_ID --no-daemon`: each pane's native
hook runs with its own pane environment instead of the shared daemon's original
pane. The switch is capability-probed; unsupported CLIs cannot advertise Fresh
Codex. Preserved native conversations are not replaced automatically. A provider
at a sign-in screen has no authenticated conversation identity to bind.

## Optional pane-local Codex identity hook

The installer ships `herdr_native_identity.py`; it does **not** edit Codex's hook
configuration or grant hook trust. Existing Herdr native identity integration
remains usable where its reported provider/session/pane identity is correct.
For the pane-local workaround, first confirm the installed Codex resume help
supports `--no-daemon`. Use that option for manual resumes too. Where supported,
append it to `agents.codex.native_resume_args` in Supervisor's configuration so
Supervisor-managed recovery uses the same mode.

To opt in, back up `~/.codex/hooks.json` and merge a single SessionStart command
into its existing structure (preserve unrelated hooks):

```json
{
  "hooks": {
    "SessionStart": [{
      "hooks": [{
        "type": "command",
        "command": "python3 \"$HOME/.local/lib/herdr-supervisor/herdr_native_identity.py\"",
        "timeout": 10
      }]
    }]
  }
}
```

If replacing the managed Herdr SessionStart handler for this workaround, replace
only that known handler rather than keeping two competing identity reporters.
Review this exact command in Codex's native `/hooks` screen and trust it there;
do not disable native hook-trust checks. Hook enablement depends on the installed
Codex version/settings. Resume the intended idle thread in its original pane
with `--no-daemon`, then verify the real reported native UUID and pane before
Supervisor starts work. A resumed client may not emit SessionStart until its
first turn; use a separately authorized bounded smoke if needed, rather than
inventing a report or submitting an unrelated task. The supported local repair
was exercised with Codex 0.160.1; it is not a universal compatibility guarantee.
