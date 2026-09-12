"""Module boundaries and the compatibility facade: acyclic one-way dependencies, one implementation per
symbol, every consumed facade name preserved, and deterministic characterization of state defaults,
configuration, prompts, protocol parsing, status/doctor shape, and CLI exit codes across the split."""

from __future__ import annotations

import ast
import contextlib
import hashlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path

import herdr_cli  # noqa: E402
import herdr_command  # noqa: E402
import herdr_core  # noqa: E402
import herdr_protocol  # noqa: E402
import herdr_quota  # noqa: E402
import herdr_runtime  # noqa: E402
import herdr_validation  # noqa: E402
import herdr_workflow  # noqa: E402
from v2_fixtures import V2Case, hs  # noqa: E402

SRC = Path(hs.__file__).resolve().parent
# One-way layering, lowest first. A module may import only modules that appear before it.
LAYERS = ["herdr_core", "herdr_cli", "herdr_quota", "herdr_protocol", "herdr_redaction", "herdr_validation", "herdr_workflow", "herdr_runtime", "herdr_command"]
FACADE = "herdr_supervisor"
# Names companion modules and tests consume through the facade (recorded before the split).
CONSUMED = sorted({
    "atomic_write_json", "backup_doctor_summary", "blocking_windows", "build_parser", "check_safe_file", "command_error_code", "deep_merge",
    "DEFAULT_CONFIG", "EVENT_NAMESPACE", "find_protocol_blocks", "GATE_STATES", "HerdrCli", "HerdrError", "iso_utc", "load_config", "load_json",
    "load_query_registry", "load_task_file", "main", "MAX_EPOCH", "MAX_HANDOFF_NOTE_CHARS", "migrate_install", "migrate_state_v1", "new_v2_fields",
    "_number", "parse_protocol", "parse_quota_snapshot", "Paths", "PLAN_CHANGED_MESSAGE", "print_status", "ProtocolBlock", "query_owner_summary",
    "QuotaError", "QuotaWindow", "register_query_provider", "request_control", "resolve_config_defaults", "resolve_herdr_bin", "session_identity",
    "sha256_bytes", "sha256_file", "StateStore", "Supervisor", "SupervisorError", "TERMINAL_STATES", "_UPLOAD_ID_RE", "validate_gate_payload",
    "validate_state_v2", "validate_task_text", "validate_upload_record", "WorkerLock", "SupervisorV2Mixin", "iso_local", "pid_alive",
    "canonical_json", "PROVIDERS", "READY_STATES", "HUMAN_WAIT_STATES", "quota_as_dict", "safe_gate_view", "validate_runtime_evidence",
})
RUN, TURN = "20598ca2-d049-4cb9-967a-5652e7bf6eff", "9945228c-bb87-493a-96ea-14d190c49510"


def project_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return {n for n in names if n.startswith("herdr_") or n == "telegram_api"}


class ModuleBoundaryTests(unittest.TestCase):
    def test_layers_are_acyclic_and_never_import_the_facade(self) -> None:
        for index, module in enumerate(LAYERS):
            imports = project_imports(SRC / f"{module}.py")
            self.assertNotIn(FACADE, imports, f"{module} must not import the facade")
            for dep in imports:
                if dep in LAYERS:
                    self.assertLess(LAYERS.index(dep), index, f"{module} -> {dep} violates the one-way layering")
                else:
                    self.assertIn(dep, {"herdr_codex_reset", "herdr_backup", "herdr_telegram", "herdr_query", "herdr_present", "herdr_artifacts"}, f"{module} imports {dep}")
        # companions may use the facade; the facade itself only re-exports
        facade_imports = project_imports(SRC / f"{FACADE}.py")
        self.assertTrue(facade_imports <= set(LAYERS), facade_imports)

    def test_every_symbol_has_one_implementation(self) -> None:
        owners: dict[str, list[str]] = {}
        for module in LAYERS:
            tree = ast.parse((SRC / f"{module}.py").read_text())
            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                    owners.setdefault(node.name, []).append(module)
        duplicates = {name: mods for name, mods in owners.items() if len(mods) > 1}
        self.assertEqual(duplicates, {})

    def test_facade_exports_are_the_owning_implementations(self) -> None:
        for name in CONSUMED:
            self.assertTrue(hasattr(hs, name), name)
            self.assertIn(name, hs.__all__, name)
            obj = getattr(hs, name)
            owner = next((m for m in (herdr_core, herdr_cli, herdr_quota, herdr_protocol, herdr_validation, herdr_workflow, herdr_runtime, herdr_command) if getattr(m, name, None) is obj), None)
            self.assertIsNotNone(owner, f"{name} is not the owning module's object")
        self.assertIs(hs.main, herdr_command.main)
        self.assertIs(hs.Supervisor, herdr_runtime.Supervisor)
        self.assertTrue(issubclass(hs.Supervisor, herdr_workflow.SupervisorV2Mixin))
        self.assertLess(len((SRC / f"{FACADE}.py").read_text().splitlines()), 320, "the facade stays small")

    def test_facade_module_is_the_entry_point_and_launcher_target(self) -> None:
        launcher = (SRC.parent / "bin" / "herdr-supervisor").read_text()
        self.assertIn("herdr_supervisor.py", launcher)
        pyproject = (SRC.parent / "pyproject.toml").read_text()
        self.assertIn('herdr-supervisor = "herdr_supervisor:main"', pyproject)
        for module in LAYERS:
            self.assertIn(f'"{module}"', pyproject, f"{module} must be packaged")


class CharacterizationTests(V2Case):
    """Golden values recorded from the accepted pre-split monolith. Changing them is a behavior change."""

    def test_configuration_and_state_defaults_are_unchanged(self) -> None:
        self.assertEqual(hashlib.sha256(json.dumps(hs.DEFAULT_CONFIG, sort_keys=True).encode()).hexdigest(), "474376d05a028125297f981ce49a613fd5bc4bad3a0847b38561befda118eb69")
        # Recorded after the gate-preserving follow-up added the optional `agent_followup: None` default
        # (pre-follow-up value: a558de0f…). Every other default is byte-identical to the accepted monolith.
        self.assertEqual(hashlib.sha256(json.dumps(hs.new_v2_fields("gated_v2"), sort_keys=True).encode()).hexdigest(), "7c511e64dd533172061e1ef435ad3bf6df4c8544b0b6178f30bd537700733295")
        without = {k: v for k, v in hs.new_v2_fields("gated_v2").items() if k != "agent_followup"}
        self.assertEqual(hashlib.sha256(json.dumps(without, sort_keys=True).encode()).hexdigest(), "a558de0f492561310dc51bdfec920cd1d0aa35a7d3149d8743c441a8b3389260")
        migrated = hs.migrate_state_v1({"schema_version": 1, "supervisor_state": "DONE"})
        self.assertEqual(migrated["workflow_policy"], "v1")
        self.assertEqual(migrated["prompt_metrics"]["prompts"], 0)

    def test_prompt_text_is_unchanged(self) -> None:
        class Builder(hs.SupervisorV2Mixin):
            config = {"review_root": "/r", "project_root": "/p"}

        expected = {
            ("initial", "d415e82a6f1fc191aafb4f099b00dcec164728386c243eda4b5da9cdbe7f16c0"): {"run_id": RUN, "task_text": "T"},
            ("handoff", "8a2766aa37e39df333265001526ce9b07d4db7110ed3446e15b2eeeb7d7ba46a"): {"run_id": RUN, "last_successful_handoff": {"stage": "brief", "from_agent": "codex", "summary": "s"}},
            ("continuation:revision", "a3c4250d4b176cc8b432a3f27eb3d9dd5db329c0fd845f09cf0419f6bd5781f5"): {"run_id": RUN, "continuation": {"kind": "revision", "gate_type": "plan_approval", "note": "n"}},
        }
        for (kind, digest), state in expected.items():
            self.assertEqual(hashlib.sha256(Builder().build_prompt_v2(dict(state), TURN, kind).encode()).hexdigest(), digest, kind)

    def test_protocol_parsing_is_unchanged(self) -> None:
        text = (Path(__file__).resolve().parent / "fixtures" / "codex-recent-unwrapped-wrapped-result.txt").read_text()
        block = hs.parse_protocol(text, RUN, TURN)
        self.assertEqual((block.stage, block.next_agent, block.gate), ("plan", "human", "plan_approval"))
        self.assertIs(type(block), herdr_protocol.ProtocolBlock)

    def test_status_and_doctor_shape(self) -> None:
        self.write_plan()
        self.start_gated([{"v2": ("plan", "human", "plan_approval", str(self.plan_payload()))}])
        status = self.sup.status()
        for key in ("supervisor_state", "task_id", "phase", "active_agent", "delivery", "pending_gate", "prompt_metrics", "consecutive_auto_turns", "operator_handoff", "quota", "agents", "backup", "errors"):
            self.assertIn(key, status)
        doctor = self.sup.doctor()
        for key in ("ok", "herdr_bin", "agents", "quota", "telegram", "backup", "prompt_ack_mode"):
            self.assertIn(key, doctor)

    def test_cli_exit_codes(self) -> None:
        env_backup = {k: os.environ.get(k) for k in ("HERDR_SUPERVISOR_CONFIG", "HERDR_SUPERVISOR_STATE_DIR")}
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["HERDR_SUPERVISOR_CONFIG"] = str(Path(tmp) / "config.json")
            os.environ["HERDR_SUPERVISOR_STATE_DIR"] = str(Path(tmp) / "state")
            (Path(tmp) / "config.json").write_text(json.dumps({"schema_version": 1, "herdr_bin": os.sys.executable, "project_root": tmp}))
            try:
                for argv, code in ((["status"], 0), (["done", "--run-id", RUN], 2), (["approve", "--run-id", RUN, "--gate-id", "g"], 2), (["logs", "--lines", "0"], 2), (["worker"], 0)):
                    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                        self.assertEqual(hs.main(argv), code, argv)
                with self.assertRaises(SystemExit):
                    with contextlib.redirect_stderr(io.StringIO()):
                        hs.main(["no-such-command"])
            finally:
                for key, value in env_backup.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
