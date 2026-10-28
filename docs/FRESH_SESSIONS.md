# Fresh sessions and human-handled startup

Fresh task starts preserve existing native sessions. Codex can start directly in a
new pane instead of creating an app-server thread and hoping another CLI process
can resume it. The process uses the configured executable, CODEX_HOME and working
directory, model profile, approval mode and `--no-daemon`. The supervisor binds
only the exact verified native identity and never silently adopts a replacement.
Legacy preparation records retain their recorded thread-first form.

## Explicit configuration

`session_start.codex_launch_method` accepts `agent-start` (legacy default) or
`explicit-pane-run`. Direct fresh Codex requires the latter and the read-only
capability/recipe checks reported by `doctor`. Installing code does not change an
existing configuration or enable a more permissive input policy.

`session_start.codex_input_policy` defaults to `require-empty-proof`.
`guarded-new-pane` is an explicit operator choice for environments without that
proof: trusted interactive-shell startup, no operator/automation input from pane
creation through launch verification, and a single 30-second submission window.
This assumption is not an enforced empty-input proof. It cannot be re-armed after
a consumed marker, expiry or uncertain submission.

Optional `session_start.approval_modes` supports Codex `inherit` or `auto-review`
(`--approve-for-me`), and Claude `inherit` or `auto` (`--permission-mode auto`).
Modes are closed choices, not arbitrary arguments. Codex auto-review retains its
workspace-write sandbox; provider refusals still stop execution. A default model
profile inherits provider settings; it does not prove a particular model ran.

## Update, notice and Claude folder-trust prompts

A blocked new process may open a durable startup wait. The notice identifies the
exact pane, screen, deadline and continuation. The human handles that screen in
that pane. The supervisor never chooses Update, Skip or Trust on the human's
behalf. Claude trust is recognized only for the exact displayed authorized folder;
a wrapped/missing/ambiguous folder is unsupported. Other screens remain unknown.

After handling the prompt, use its Telegram Continue action or the exact printed
`herdr-supervisor continue-startup --wait-id ...` command. Continuation verifies
the original process, executable, working directory, identity, screen and deployed
contracts. Changed processes or executable bytes hold for an explicit decision;
this is not automatic compatibility with every provider upgrade. Expired waits do
not relaunch, re-bootstrap or resend input. Cancel with the corresponding
`cancel-startup` command. No current-session restart/rebind is implicit.

Foreground-process snapshots can be temporarily empty or ambiguous during
provider detection. The alias/wait path observes an already-bound launch within
its original bounded budget, performing no mutation while uncertain. Persistent
ambiguity or replacement still refuses. Continuation verifies a running provider
using the process contract; the adapter's empty-shell predicate applies only
before launch, not as an existence test for an occupied provider pane.

## Evidence and limits

The installed-lineage feature passed a four-session native diagnostic (two seeds,
two fresh replacements) with one synthetic task and cleanup. The human handled
Codex update and Claude trust prompts. Both Codex modes had native evidence;
Claude's visible auto-mode evidence required human acceptance. Model selection
and actual provider executable upgrades remain untested. Publication integration
preserves newer upstream workflow profiles, onboarding and reliability changes;
its offline integration checks are distinct from that earlier native test.

Most tests use isolated fakes and need no private files. Three optional historical
reader-oracle checks in `test_direct_launch.py` require a frozen pre-direct-launch
library via `HERDR_PREVIOUS_LIBRARY`; they skip explicitly when absent. Release
package inventory and private diagnostic/trust-tool tests stay in their original
private evidence, not in the public source suite. No live provider test is run by
normal test discovery.

Historical approval-mode reader comparisons additionally accept
`HERDR_PRE_APPROVAL_LIBRARY` pointing to a pre-approval-mode library snapshot.
Without it, those three historical comparisons skip explicitly; ordinary
approval-mode tests always run. Private installation byte-inventory checks
remain in their original deployment evidence.
