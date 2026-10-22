# Adaptive project setup

Project onboarding is part of Supervisor, not a separate app. The normal installer never changes your workspace instructions. Setup is explicit and offline; it can run while another Supervisor is serving a different project because it does not load live configuration, construct a Supervisor, use Herdr, invoke models, or write runtime state.

## Choose how much setup you want

| Preset | Instruction files | Workflow skills | Agent roles and planning |
|---|---|---|---|
| `preserve` (default) | Unchanged | None | Keep your existing process |
| `adaptive` | Reviewed appendices | Portable baseline | Lightweight project guidance; no mandatory provider handoff |
| `gated` | Reviewed appendices | Portable baseline | Explicit Codex plan approval → Claude implementation |

The default creates only an external project-context/validation proposal and inactive isolated profile. It does not install our process into your repository. Optional full support-skill imports work with every preset. Add `--write-instructions` in preserve mode only if you want neutral project-context links in the agent instruction files; that option does not add our planning stages, roles or artifact names. Adaptive/gated presets explicitly opt into instruction appendices and the workflow bundle. These choices never reinterpret an active Supervisor run.

## Discover, understand, review, apply

Run from the installed CLI after updating Supervisor, or from a source checkout with `PYTHONPATH=src python3 src/herdr_supervisor.py` in place of `herdr-supervisor`:

```sh
herdr-supervisor project inspect /path/to/project \
  --output /path/to/private-workflows/my-project-discovery
```

Choose a **new external directory** for every proposal. Keep it outside the project repository; it contains private discovery, generated configuration, reviews and future state. Inspect `DISCOVERY.md` and `proposal.json`. The command prints the exact proposal SHA-256. It does not modify the target project.

Discovery is bounded (4,000 entries, eight directory levels, 256 KiB per input/proposal), skips dependency/build trees and symlinks, and never reads `.env` or credential files by default. It recognizes nested Python/JavaScript/Rust/Go manifests, CI paths, tests, migrations, existing instructions and local skills. Candidate commands are recommendations, not executed tests; script values and CI bodies are not copied into the report. A truncated inventory cannot be applied: narrow the root. Unsupported stacks, malformed inputs and unknown runtime/branch policy remain gaps rather than invented facts. Ancestor/global instruction files are not inventoried; the investigating agent must reconcile inherited instructions too.

**Codebase understanding is a separate agent step.** Give `AGENT_INVESTIGATION.md` to the agent already working in the target project. It asks for real code-path tracing and evidence citations, not another copy of generic instructions. No paid model requests or helper sessions are started by onboarding. The agent produces an assessment JSON such as:

```json
{
  "findings": [
    {"text": "The API owns persistence; the web client consumes its contract.", "evidence": ["api/routes.py", "web/src/client.ts"]}
  ],
  "rules": [
    {"text": "Validate both API and client consumers after response schema changes.", "evidence": ["api/routes.py", "web/src/client.ts"]}
  ],
  "checks": [
    {"text": "Use the isolated API contract tests before the broader suite.", "evidence": ["tests/test_contract.py"]}
  ]
}
```

Those example paths are illustrative, not files the tool assumes exist. Every real claim requires 1–8 existing, relative, non-secret source/document citations; each section allows at most 20 claims. Assessment text is reviewed agent output, not proof of correctness or command safety. It may not contain credentials. Rebuild a new proposal with that assessment:

```sh
herdr-supervisor project inspect /path/to/project \
  --assessment /path/to/assessment.json \
  --output /path/to/private-workflows/my-project-reviewed
```

The project-local validation skill and proposal now include the cited project-specific findings and validation recommendations. To also adopt workflow guidance, add `--preset adaptive` to inspection. To keep the existing workflow and only add optional project-context instruction links, use `--write-instructions`. Assessed rules enter instruction appendices only when instruction writing is explicitly selected. Existing skills are inventoried for reuse; onboarding does not install downloaded skills or rewrite global skills.

Read the **exact** `proposal.json` contents, resolve conflicts and operator questions, and approve its printed fingerprint:

```sh
herdr-supervisor project apply /path/to/private-workflows/my-project-reviewed \
  --approval-sha256 EXACT_REVIEWED_PROPOSAL_SHA256
```

When instruction writing is selected and root `AGENTS.md` or `CLAUDE.md` already exists, apply refuses until you add `--acknowledge-existing` **after** reviewing conflicts. Original bytes and permission bits are preserved; a marked appendix is added, never a wholesale replacement. Nested instructions and existing skills are not edited. Existing rules take precedence over the appendix. A changed proposal, onboarding implementation, cited source, manifest, instruction or discovered file inventory invalidates the pending apply. Repeating a completed apply is idempotent while generated targets remain unchanged.

Preserve/adaptive profiles use the general `supervised_v2` controller, which does not force planning or a provider sequence. Only adaptive instructions add our recommended task-size guidance. Use `inspect --preset gated` to explicitly select strict Codex-plan/Claude-implementation procedure. The optional `--policy` flag must agree with the selected preset; it cannot silently turn preserve mode into strict workflow. No policy is inferred from a framework name. Generated instructions do not grant permission to delegate, switch models, spend resets, deploy, migrate, commit or push.

## What gets applied

- Only when selected: a marked appendix in root `AGENTS.md` and `CLAUDE.md`. Preserve mode leaves both unchanged by default.
- `generated/supervisor.json` inside the external bundle, with isolated project/review roots and optional helpers/tracking disabled.
- `generated/profile.env`, selecting a distinct Supervisor configuration and state directory.
- `generated/skills/project-validation/SKILL.md`, adapted from the project's assessment.
- Only for adaptive/gated presets, five portable baseline skills: `workflow-planning`, `implementation-review`, `runtime-validation`, `agent-orchestration` and `workflow-observer`.
- `generated/skills/baseline.json`, recording the baseline version, identity hash and original per-skill hashes. When selected, the skills are referenced from instruction appendices; they are not installed into native automatic-discovery directories. Preserve mode has no baseline manifest or baseline skills.
- `applied.json`, recording exact file hashes after successful application.

**Application is not activation.** Do not source the profile into the running Supervisor's shell, change its services, copy owner files or share its state directory. In a separate terminal, review the generated environment file, explicitly select this project's sessions, and run `herdr-supervisor doctor` before starting a task. Initial enrollment follows existing owner requirements. Telegram project selection, multi-instance service provisioning and bot/token isolation are not implemented by this feature; configure and review those separately. Do not run two bridges polling the same bot token.

Apply attempts are serialized for the same bundle and project, including different proposals targeting that project. Stop editors/agents that might change the target instruction files before apply. Validation precedes all writes and each file is atomically published, but a multi-file OS crash or I/O failure is **not** an all-files transaction. An interrupted apply may leave a partial appendix/profile; inspect its files and restore/reconcile manually before generating a new proposal. The command never claims completion before the final marker. Existing managed appendices are refused on re-inspection rather than silently upgraded.

## Versioned portable skills

Baseline **1.0.0** is shipped inside Supervisor and copied only when the operator selects an adaptive/gated proposal. Preserve mode does not copy or activate it. It contains portable workflow methodology, not a copy of the current machine's installed skills or Prospera-specific hosts, scripts, branches and business rules. Normal source, wheel and installer distributions carry the same baseline. `workflow-observer` is a small original task-observation skill, not the full third-party task-observer distribution and not a dependency on its global logs.

Load a skill only when its trigger applies; onboarding does not inject every skill body into each turn. Project-specific facts and adaptations belong in the assessed rules and `project-validation`. Core baseline files are pinned at initial application. A changed packaged baseline invalidates pending proposals even when the version string was accidentally left unchanged.

After setup, a project may review and customize its own skill copies. Keep `baseline.json` as original provenance and record local changes separately. The observer records suggestions; it cannot automatically rewrite skills or promote a change. A repeat apply refuses locally modified files rather than overwriting them. No task in one project changes another project's baseline or the globally installed skills. Portable improvements require a separate reviewed baseline version/release; this feature does not automatically upgrade existing managed instruction blocks.

## Optional full support skills

Onboarding also supports explicit local imports of **stop-slop**, **ui-ux-pro-max**, **skill-creator** and the full **task-observer**. These are optional snapshots, separate from the portable baseline. Supervisor does not search your global skill directories, fetch a repository, or silently choose your latest locally revised version. Point it at the particular reviewed copies you want:

```sh
herdr-supervisor project inspect /path/to/project \
  --output /path/to/private-workflows/project-with-support \
  --support-skill stop-slop=/path/to/reviewed-skills/stop-slop \
  --support-skill ui-ux-pro-max=/path/to/reviewed-skills/ui-ux-pro-max \
  --support-skill skill-creator=/path/to/reviewed-skills/skill-creator \
  --support-skill task-observer=/path/to/reviewed-skills/task-observer
```

Each `NAME=DIR` must identify the complete skill directory, not just `SKILL.md`. All regular supporting files are preserved, including references, scripts, UI datasets, images, licenses and attribution. Git metadata and Python/tool caches are excluded. Imports refuse symlinks, credential-file paths, missing/mismatched entrypoint names, and excessive resource inventories. Limits across selected imports: 256 files and 16 MiB total, 2 MiB per file, eight directory levels. No helper runs during import/apply, and dependencies are never installed automatically.

Inspection stores immutable resource candidates under `skill-sources/` in the external proposal bundle. The proposal records every source file's SHA-256, the full snapshot identity, source location and available license files. Approval binds the resource inventory; changed snapshot bytes or substituted source bindings refuse application before project files are written. Subsequent edits to the original source directory cannot change the staged version you reviewed. Missing/unclear license terms do not grant redistribution rights: these are private local imports, not third-party assets shipped inside Supervisor's public release.

After approved application, the full copies live under `generated/skills/NAME/`. Read `generated/skills/SUPPORT.md` for integration details:

- **stop-slop:** its prose rules and reference examples remain intact.
- **ui-ux-pro-max:** the search scripts and all local data remain together. Use the copied skill's absolute script path instead of assuming a global `CLAUDE_PLUGIN_ROOT`. Python 3 is required.
- **skill-creator:** stage proposed skills inside this project's external workflow bundle. Its YAML validation/generation helpers require PyYAML; dependency installation is a separate action.
- **task-observer:** the full skill and resources are preserved. Its workspace/log anchor is explicitly this project's `project-learning/` directory. It replaces the lightweight workflow-observer for that project; do not load both. No global or Prospera observation history is copied.

When instruction writing is selected, both appendices point to this guide; otherwise open it directly from the external bundle. Existing user/project instructions still take precedence; helpers remain subject to task scope and side-effect approval. Local skill changes stay local, and repeat apply refuses to overwrite them. Imports are not a claim that every helper has been executed or that optional dependencies are installed. For a reproducible shared setup, keep reviewed source exports and their snapshot manifests under your own versioning rather than automatically synchronizing installed skills.
