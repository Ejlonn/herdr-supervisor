"""Versioned portable workflow skills, authored for Herdr Supervisor (Apache-2.0).

This is release-owned data, not a snapshot of installed/global skills. Project
copies may evolve independently; promote changes only through a reviewed release.
"""
from __future__ import annotations

import hashlib
import json

BASELINE_VERSION = '1.0.0'
BASELINE_SKILLS: dict[str, tuple[str, str]] = {
    'workflow-planning': (
        'Plan consequential project changes before implementation; use for architecture, shared contracts, security, data or migration work.',
        '''# Workflow planning

Read existing project instructions and the project-specific findings in `../project-validation/SKILL.md`. Trace the actual entry point, affected consumers and tests; distinguish observed code from assumptions. Existing project rules and the operator's task determine roles and approval requirements.

For small reversible fixes, use a short implementation/check outline unless project policy requires more. For consequential changes, record: current behavior and evidence; desired outcome and non-goals; smallest design and affected paths; data/API and retry implications; risks and rollback; acceptance criteria; focused validation and any runtime prerequisites. Name unanswered decisions instead of inventing branch names, test targets or domain rules.

Keep the plan as the rationale and acceptance source. An execution brief references the approved plan and contains only ordered work, critical constraints, tests, deliverables and stop conditions. Do not duplicate the full plan or use planning approval as permission to deploy, publish or change credentials. Follow the project's chosen artifact names and assigned agent roles; do not impose a provider handoff on a task that does not need one.
'''),
    'implementation-review': (
        'Audit an implementation handoff or review a scoped project diff for correctness, regressions and acceptance failures.',
        '''# Implementation review

Use project-specific contracts and validation guidance from `../project-validation/SKILL.md`. Inventory tracked AND intended untracked files; review actual code and all consumers, not only a summary or test count. Check error paths, authorization/data boundaries, exact primitive types at JSON interfaces, untrusted integrity metadata and concurrent retry/idempotency behavior where touched.

Implementer: report the goal, root cause, before/after behavior, changed paths, executed validation, runtime/rebuild impact, limitations and remaining decisions. Reconcile every logical change with the final diff. Preserve unrelated human work.

Reviewer: prioritize confirmed correctness, security/data integrity, regressions and unmet acceptance criteria. Separate unresolved design decisions and optional cleanup. When review independence is required, report whether it actually occurred; a self-review is not independent. Batch findings from one coherent pass.

After a narrow correction, inspect the delta and immediate consumers, then rerun evidence that can be affected. Broaden only for a changed shared contract or demonstrated risk. Mock success is not proof of real external-tool compatibility; use an isolated real-consumer check where applicable and authorized. Never claim implementation or runtime success from inspection alone.
'''),
    'runtime-validation': (
        'Prepare or assess runtime evidence for a project change; use at a runtime gate or when a correction may invalidate prior evidence.',
        '''# Runtime validation

Read `../project-validation/SKILL.md` and confirm the actual target, candidate revision/digest, allowed side effects and operator ownership. A path, hostname or shell prompt alone does not identify a safe environment. Unknown runtime ownership is a decision to resolve, not implied permission.

Map the final diff to affected services/build artifacts and the smallest business smoke. Documentation-only or test-only changes normally need no restart unless they reveal a product defect. If source is copied into an image, identify the actual build layout; restart alone does not validate newly copied code.

Prepare one checked command block after verifying paths, configuration and available prerequisites read-only. Do not copy project-specific deployment scripts or host assumptions from another repository. Scheduler pauses, migrations, external calls and production changes need their own authority; restore any explicitly paused work and record restoration evidence.

Tie results to the exact candidate and say what ran, who observed it and what remains untested. Collaborative evidence is a proposal until the designated operator accepts it; an actor label is not acceptance. Changed candidate bytes invalidate the affected claim. Before release, reconcile intended diff, focused tests, required runtime/smoke (or justified N/A), handoff accuracy and the project's actual base-branch policy. Do not equate readiness with authorization to commit, push or deploy.
'''),
    'agent-orchestration': (
        'Design or review deterministic control of coding agents, including identity, delivery, gates, lifecycle waits and quota recovery.',
        '''# Agent orchestration

Apply only when changing a controller/adapter; ordinary application work does not require this skill. Read `../project-validation/SKILL.md` for the actual project boundaries and test contracts. Keep project workflow procedure separate from universal controller safety. Persist a run's selected policy rather than reinterpreting it after configuration changes.

Verify supported tool capabilities and exact provider/session identity; aliases and pane names alone are not identity. Ambiguity stops with a specific recovery action, never silent adoption. Delivery intent precedes side effects, uncertain delivery is not permission to resend, and continuation prompts reference prior work instead of replaying it.

The deterministic controller owns lifecycle and quota waits. Polling must not wake an LLM. Agent text never grants human approval: bind typed decisions to run, candidate and exact artifact bytes; a revised decision remains void until its successor is explicitly approved. Read-only questions preserve the pending decision.

Tests must assert mechanisms: prompt counts for at-most-once delivery; busy-to-settled behavior; stale/cross-run refusal; exact identity ambiguity; bounded retries; concurrent journal/counter safety. Consume the real adapter boundary used by deployed callers; a successful permissive shell probe is not proof for a restricted worker. Do not add provider/model switching, helper panes or reset consumption without explicit scope and authority.
'''),
    'workflow-observer': (
        'Record project-local workflow friction and operator feedback during multi-step work; use when a repeatable process improvement is observed.',
        '''# Workflow observer

This is a lightweight, original portable observer, not the full third-party task-observer package. It does not import a global observation log or inherit another project's history.

When a repeatable workflow problem appears, record a short project-local observation in the external workflow bundle's `observations/` directory: date, affected skill/version, concrete evidence, proposed improvement and status. Check existing entries before adding a duplicate. Do not log credentials, private transcripts or speculative failures as facts. Ordinary task output stays in its normal task report.

Keep monitoring inexpensive: do not reread every skill or scan whole histories on every turn. Consult the relevant skill when the work calls for it. Surface actionable observations concisely at task end; observation is not authorization to rewrite instructions.

Changes to project-local skills require review of the concrete proposed delta. Keep the original baseline version/hash manifest as provenance; record local customization separately. Never modify global skills, other projects or the packaged baseline automatically. A portable improvement enters the shared baseline only through an explicit reviewed release and version update; domain rules stay in the project's assessment/validation skill. Existing project instructions take precedence.
'''),
}


def baseline_files() -> dict[str, str]:
    """Render self-contained skills without reading any user's installed skill tree."""
    return {f'generated/skills/{name}/SKILL.md':
            f'---\nname: {name}\ndescription: {description}\n---\n\nPortable workflow baseline {BASELINE_VERSION}.\n\n{body}'
            for name, (description, body) in BASELINE_SKILLS.items()}


def baseline_manifest() -> dict[str, object]:
    hashes = {path: hashlib.sha256(content.encode()).hexdigest() for path, content in baseline_files().items()}
    identity = json.dumps({'version': BASELINE_VERSION, 'files': hashes}, sort_keys=True).encode()
    return {'version': BASELINE_VERSION, 'sha256': hashlib.sha256(identity).hexdigest(), 'files': hashes,
            'origin': 'Herdr Supervisor portable baseline; not imported from installed skills',
            'updates': 'Project copies are isolated. Shared updates require explicit review and versioning.'}
