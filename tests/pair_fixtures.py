"""Shared fixtures for the named-pair suites: two generic example pairs next to the legacy owners, an administrator-allowed
root, and helpers that count every effectful Herdr call. No real provider, Herdr, Telegram, or user-specific path."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import herdr_pairs as hpair
from test_telegram import TelegramCase
from v2_fixtures import FakeHerdr, hs, write_codex_primary_rollout

ALPHA = {"claude": ("w4:p1", "aaaaaaaa-0000-4000-8000-000000000001"), "codex": ("w4:p2", "aaaaaaaa-0000-4000-8000-000000000002")}
BETA = {"claude": ("w5:p1", "bbbbbbbb-0000-4000-8000-000000000001"), "codex": ("w5:p2", "bbbbbbbb-0000-4000-8000-000000000002")}


def ids(spec: dict[str, tuple[str, str]]) -> dict[str, dict[str, str]]:
    return {provider: {"pane_id": pane, "session_id": session} for provider, (pane, session) in spec.items()}


class PairCase(TelegramCase):
    """The legacy pair (codex-main/claude-main) stays live next to two named example pairs. Names are generic labels."""

    def setUp(self) -> None:
        super().setUp()
        root = self.project_root
        self.repo_a, self.review_a = root / "repo-alpha", root / "reviews" / "alpha"
        self.repo_b, self.review_b = root / "repo-beta", root / "reviews" / "beta"
        for path in (self.repo_a, self.review_a, self.repo_b, self.review_b):
            path.mkdir(parents=True)
        self.config["session_pairs"] = {"enabled": True, "allowed_roots": [str(root)], "instruction_root": str(root)}
        for label, spec in (("alpha", ALPHA), ("beta", BETA)):
            for provider, (pane, session) in spec.items():
                self.herdr.agents[f"{label}-{provider}"] = FakeHerdr.agent(provider, f"{label}-{provider}", pane, session)
                self.herdr.available_panes.add(pane)
            write_codex_primary_rollout(self.codex_native_root, spec["codex"][1])
        self.owners_bytes = self.paths.owners_file.read_bytes()
        self.bridge = self.make_bridge()

    # ----- builders
    def ctx(self, repo: Path, review: Path) -> dict[str, str]:
        return {"instruction_root": str(self.project_root), "repository": str(repo), "review_base": str(review)}

    def register(self, name: str, spec: dict, repo: Path | None = None, review: Path | None = None, **kwargs) -> dict:
        # Existing sessions need their own primary history under the guard.
        # Preserve any test's explicit adverse/custom history.
        session = spec["codex"][1]
        if not list(self.codex_native_root.rglob(f"*{session}.jsonl")):
            write_codex_primary_rollout(self.codex_native_root, session)
        return hpair.register_existing(self.paths, self.config, self.clock.time, name=name, context=self.ctx(repo or self.repo_a, review or self.review_a), identities=ids(spec), **kwargs)

    def saved(self, entry: dict) -> dict:
        return {"policy": "preserve", "profiles": {"codex": "default", "claude": "default"}, "pair": {"mode": "saved", "pair_id": entry["pair_id"], "fingerprint": hpair.entry_fingerprint(entry)}}

    def existing(self, name: str, spec: dict, repo: Path | None = None, review: Path | None = None) -> dict:
        session = spec["codex"][1]
        if not list(self.codex_native_root.rglob(f"*{session}.jsonl")):
            write_codex_primary_rollout(self.codex_native_root, session)
        return {"policy": "preserve", "profiles": {"codex": "default", "claude": "default"},
                "pair": {"mode": "save_existing", "name": name, "context": self.ctx(repo or self.repo_a, review or self.review_a), "identities": ids(spec)}}

    def creation(self, name: str, repo: Path | None = None, review: Path | None = None) -> dict:
        return {"policy": "fresh-all", "profiles": {"codex": "default", "claude": "default"},
                "pair": {"mode": "create", "name": name, "context": self.ctx(repo or self.repo_a, review or self.review_a)}}

    def command(self, selection: dict, run_id: str | None = None, text: str = "Investigate the widget") -> dict:
        run_id = run_id or str(uuid.uuid4())
        return {"request_id": uuid.uuid4().hex[:32], "action": "task", "task_text": text, "source": "telegram", "preallocated_run_id": run_id,
                "session_selection": selection, "actor": "cli", "chat_id": 1, "created_at": "2026-01-01T00:00:00Z"}

    def start(self, selection: dict, run_id: str | None = None) -> dict:
        """Apply a task command under the transaction lock exactly as the inbox worker does; returns the result."""
        command = self.command(selection, run_id)
        with self.sup.store.transaction():
            return self.sup.apply_command(self.sup.store.read_state(required=False), command)

    def pair_plan_payload(self, review_base: Path, **overrides) -> Path:
        """A plan-approval payload whose review directory is below the PAIR's review base (a named run validates against its frozen context)."""
        directory = review_base / "feature-widget"
        directory.mkdir(exist_ok=True)
        (directory / "CODEX_PLAN.md").write_text("# plan\n")
        payload = {"schema_version": 1, "gate_type": "plan_approval", "task_title": "Add widget", "summary": "Plan", "review_directory": str(directory), "scope": "backend",
                   "intended_changes": ["x"], "risk_summary": "low", "affected_components": ["backend"], "migration_required": False, "migration_explanation": "",
                   "runtime_validation_required": True, "rebuild_required": True, "rebuild_reason": "image", "push_approval_required": True, "plan_path": str(directory / "CODEX_PLAN.md"), **overrides}
        path = directory / "plan_payload.json"
        path.write_text(json.dumps(payload))
        return path

    def plan_for(self, selection: dict) -> list[dict]:
        """The scripted first reply for a run started with `selection`: a plan gate inside that run's own review context."""
        pair = selection.get("pair")
        if pair is None:
            return [{"v2": ("plan", "human", "plan_approval", str(self.plan_payload()))}]
        if pair["mode"] == "saved":
            base = Path(hpair.get_entry(self.paths, pair["pair_id"])["context"]["review_base"])
        else:
            base = Path(pair["context"]["review_base"])
        return [{"v2": ("plan", "human", "plan_approval", str(self.pair_plan_payload(base)))}]

    def effects(self) -> dict:
        """Every effectful adapter call so far: a selection or a refusal must leave all of these unchanged."""
        return {"prompts": list(self.herdr.prompts), "starts": list(self.herdr.starts), "splits": list(self.herdr.splits), "pane_runs": list(self.herdr.pane_runs),
                "sent_keys": list(self.herdr.sent_keys), "renames": list(self.herdr.renames), "agent_renames": list(self.herdr.agent_renames), "workspaces": list(self.herdr.workspaces),
                "bootstrap": list(self.herdr.bootstrap_prompts)}

    def owners_unchanged(self) -> bool:
        return self.paths.owners_file.read_bytes() == self.owners_bytes

    def registry_doc(self) -> dict:
        return json.loads(hpair.registry_path(self.paths).read_text())

    def prompt_targets(self) -> set[str]:
        return {name for name, _ in self.herdr.prompts}


__all__ = ["ALPHA", "BETA", "PairCase", "hs", "ids"]
