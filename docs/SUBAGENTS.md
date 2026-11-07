# Shared repository helpers

The same definitions work for application repositories and Supervisor itself.
Role names describe jobs; they do not select a repository or imply access
to a particular machine. The parent supplies the
verified checkout and any context/changelog paths for each task.

| Provider | Role | Model | Purpose |
|---|---|---|---|
| Codex | `repository_explorer` | `gpt-6-luna`, high | Read-only code/architecture evidence |
| Claude | `repository-tracer` | Haiku | Read-only code-path tracing |
| Claude | `repository-test-runner` | Haiku | Run exactly the supplied bounded test commands |
| Claude | `repository-diff-auditor` | Sonnet | Reconcile the diff with the supplied task changelog |

Helpers provide evidence. The parent owns implementation, design, scope,
severity, validation and release decisions. An auditor PASS proves only that the
diff and changelog agree. Shell-based tests and personal reconciliation usually
cost less than delegating; a helper is optional, never a mandatory workflow step.

## Install explicitly

From this Git/source checkout (including an extracted source distribution):

```sh
bash scripts/sync-agents.sh --provider claude --dry-run
bash scripts/sync-agents.sh --provider claude
bash scripts/sync-agents.sh --provider codex --dest "$HOME/.codex/agents" --dry-run
bash scripts/sync-agents.sh --provider codex --dest "$HOME/.codex/agents"
```

This user-level installation makes one shared set discoverable from either
project. There is no need to copy definitions into both repositories. A custom
Claude `--dest` is also supported. Edit these Git-owned sources and rerun the
script; never edit an installed copy. Updating different installed bytes requires
an explicit `--update` after inspecting the diff and keeping a backup. The main
Supervisor installer does not run this optional setup, enable delegation or
restart anything. Wheel installation alone does not install these definitions.

The script requires Bash, GNU `realpath`/`mv` and Python 3.11+ for Codex TOML
parsing. Defaults are relative to the script, independent of the caller's cwd.
`--src` can select another reviewed inventory. Codex requires an explicit
destination and accepts only `repository_explorer`. The script checks source
names/metadata, conflicts, `..` components, symlink ancestry and non-regular
targets before any write. Publication is atomic per file; this is not a
multi-file filesystem transaction. Dry-run creates nothing, unrelated files are
preserved, and orphans are reported rather than deleted. No provider is called.

## Select the checkout, not a preset name

For either repository the parent first verifies:

```sh
git -C /absolute/task-checkout rev-parse --show-toplevel
git -C /absolute/task-checkout status -sb
```

The first helper input line is `CHECKOUT=/absolute/task-checkout`; use the actual
root printed by Git. The auditor's second line is the supplied absolute changelog
path. Other roles receive bounded claims or exact test commands afterwards.
Context files outside the checkout must be explicitly named by the parent.
Never infer the repository, review directory or ownership from a helper's name.

Named session pairs, when provided by the installed controller, still bind each
run to its original sessions and context. Installing these definitions does not
add or change presets, switch owners, clear conversations or override approvals.

## Pickup, authorization and limits

Files on disk do not prove that an existing parent can select them. Use the
provider's supported fresh-session pickup and check that parent's own callable
catalog; never restart or replace another conversation automatically. Repository
and human delegation policy remains authoritative. These files do not enable
delegation globally or change a model's sandbox/permissions.

An authorized Codex parent selects `agent_type=repository_explorer` with
`fork_turns=none` and no model override. The role fixes its model/effort. Await
the exact child's successful completion and final handback before routing.

Controller-specific role inventories, concurrency/spend limits and lifecycle
evidence requirements remain authoritative. An installed controller may refuse
a role it does not explicitly allow. This package changes no such inventory,
does not install an admission or transcript guard, and does not authorize name
substitution, session reset/rebind or a budget increase. Resolve unknown or
incompatible provider/controller contracts before invoking a helper.

Claude helpers remain optional with at most one active helper unless the human
authorizes otherwise. Collect all started results before handoff. Do not edit
the checkout while its delegated tests or audit are in flight. Unexpected edits,
identity changes or uncertain completion stop delegation; do not silently retry.
