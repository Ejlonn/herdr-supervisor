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

## Named session pairs

Named pairs are off by default. An administrator opts in with `session_pairs` in the
configuration: `enabled: true`, up to sixteen canonical absolute `allowed_roots`, and an
optional `instruction_root`. A pair is a saved, immutable record of one Claude and one Codex
native session (provider, pane locator, exact native session id) plus the repository, review
base and instruction root they work in. Names are display labels only: never paths, never
authority, never part of identity. Telegram can choose or save a pair on the task Start card
(`/pair-set NAME | REPOSITORY | REVIEW_BASE` completes a save/create choice); the repository and
review base must lie inside an allowed root, so chat input can never widen the roots.

Choosing a pair changes nothing until Start. Starting binds the pair's exact identities and
context into the run (`session_pair`), and the run keeps that frozen binding through every later
answer, approval, gate payload check, candidate-head check and follow-up destination, even if
the registry, owners or configuration change afterwards. Selecting another pair is a boundary
operation: the previous run and its worker must be finished, and both sessions of the new pair
must be live, settled and not owned by another pair or query worker. A missing, busy or changed
identity is refused with an explanation; the supervisor never picks another session, launches,
renames, rebinds or sends input to satisfy a selection.

## Codex native helper history guard

Codex may spawn native helpers on its own. Before the supervisor sends input to, reads a result
from, or routes for a Codex parent, `herdr_codex_children.py` reads that exact parent's complete
native transcript (and its child transcripts) once, under one shared byte, entry and time budget,
and answers *transparent* (no helper), *complete* (one approved helper whose final was delivered),
*hold* (a helper is still running; the supervisor waits and never interrupts it), or a stop kind:
*unknown* (missing, malformed, unsupported, changed or over-budget evidence is never treated as
zero helpers) or *violation* (more than one helper, an unapproved role, model or effort, a
prohibited interaction). Only the optional `repository_explorer` (gpt-6-luna, max) and
`repository_reviewer` (gpt-6.1-sol, high) definitions in `agents/codex/` are approved, with
`fork_turns` exactly `none`, one helper per parent lifetime. The guard observes recorded actions;
it cannot prevent a spawn. A Codex parent whose native history cannot be read fails closed, so
the Codex session root (the provider's standard location unless `native_session_roots` overrides it)
must be readable. Routing uses the parent's own native
final, never a pane frame, once a helper was involved.

## Optional pane-local Codex identity hook

The installer ships `herdr_native_identity.py`; it does **not** edit Codex's hook
configuration or grant hook trust. Existing Herdr native identity integration
remains usable where its reported provider/session/pane identity is correct.
For the pane-local workaround, first confirm the installed Codex resume help
supports `--no-daemon`. Use that option for manual resumes too. Where supported,
append it to `agents.codex.native_resume_args` in Supervisor's configuration so
Supervisor-managed recovery uses the same mode.

The launcher `~/.local/bin/herdr-native-identity` resolves the active program generation, so the hook keeps working across updates. A hook that was configured earlier with the old program-directory path (`~/.local/lib/herdr-supervisor/herdr_native_identity.py`) keeps working too: `herdr-setup` maintains that path as a link to the active generation.

To opt in, back up `~/.codex/hooks.json` and merge a single SessionStart command
into its existing structure (preserve unrelated hooks):

```json
{
  "hooks": {
    "SessionStart": [{
      "hooks": [{
        "type": "command",
        "command": "\"$HOME/.local/bin/herdr-native-identity\"",
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
