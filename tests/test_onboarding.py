"""Offline onboarding: isolation, adaptation and exact reviewed application."""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import herdr_command as command
import herdr_onboarding as onboarding
from herdr_core import SupervisorError


class OnboardingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.project = self.home / 'project'
        self.project.mkdir()
        self.bundle = self.home / 'onboarding'

    def put(self, name, data):
        path = self.project / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data)
        return path

    def inspect(self, **kwargs):
        kwargs.setdefault('preset', 'adaptive')
        return onboarding.inspect_project(self.project, self.bundle, **kwargs)

    def apply(self, result, **kwargs):
        return onboarding.apply_project(self.bundle, result['proposal_sha256'], **kwargs)

    def proposal(self):
        return json.loads((self.bundle / 'proposal.json').read_text())

    def test_empty_project_is_unknown_without_invented_checks(self):
        result = self.inspect()
        proposal = self.proposal()
        self.assertEqual(proposal['analysis']['stacks'], [])
        self.assertEqual(proposal['analysis']['checks'], [])
        self.assertEqual(result['status'], 'proposal_only')
        self.assertEqual(list(self.project.iterdir()), [])
        self.assertIn('not a semantic audit', (self.bundle / 'DISCOVERY.md').read_text())
        self.assertFalse((self.bundle / 'state').exists())

    def test_nested_polyglot_adaptation_does_not_copy_script_contents(self):
        secret = 'do-not-echo-script-value'
        self.put('web/package.json', json.dumps({'scripts': {'test': secret, 'lint': secret, 'deploy': secret, 'test:unit': 'vitest'}}))
        self.put('api/pyproject.toml', '[tool.pytest.ini_options]\ntestpaths=["tests"]\n[tool.ruff]\n')
        self.put('api/tests/test_api.py', 'raise RuntimeError("MUST NOT RUN")')
        self.put('api/migrations/001.sql', 'do not execute')
        self.put('.github/workflows/ci.yml', 'untrusted content')
        self.inspect()
        analysis = self.proposal()['analysis']
        self.assertEqual(analysis['stacks'], ['JavaScript/TypeScript', 'Python'])
        self.assertIn('api/migrations/001.sql', analysis['migrations'])
        self.assertEqual(analysis['ci'], ['.github/workflows/ci.yml'])
        self.assertIn({'cwd': 'web', 'command': 'npm run test', 'source': 'web/package.json', 'status': 'candidate only; inspect script and lockfile/package manager before execution'}, analysis['checks'])
        self.assertNotIn(secret, (self.bundle / 'proposal.json').read_text())
        self.assertNotIn('npm run deploy', (self.bundle / 'DISCOVERY.md').read_text())

    def test_rust_and_go_projects_have_separate_candidate_checks(self):
        self.put('rust/Cargo.toml', '[package]\nname="example"')
        self.put('go/go.mod', 'module example')
        self.inspect()
        self.assertEqual(self.proposal()['analysis']['stacks'], ['Go', 'Rust'])
        self.assertEqual({c['command'] for c in self.proposal()['analysis']['checks']}, {'cargo test', 'go test ./...'})

    def test_skills_and_nested_instructions_are_evidence_not_overwritten(self):
        nested = self.put('service/AGENTS.md', 'nested instructions')
        self.put('.agents/skills/domain/SKILL.md', 'existing skill')
        result = self.inspect()
        self.assertIn('service/AGENTS.md', self.proposal()['analysis']['instructions'])
        self.assertIn('.agents/skills/domain/SKILL.md', self.proposal()['analysis']['skills'])
        self.apply(result)
        self.assertEqual(nested.read_text(), 'nested instructions')

    def test_existing_instructions_require_ack_and_preserve_bytes_and_mode(self):
        agents = self.put('AGENTS.md', '# Existing\nKeep this.\n')
        agents.chmod(0o644)
        before = agents.read_bytes()
        result = self.inspect()
        with self.assertRaisesRegex(SupervisorError, 'acknowledge-existing'):
            self.apply(result)
        self.assertFalse((self.project / 'CLAUDE.md').exists())
        self.assertFalse((self.bundle / 'generated').exists())
        self.apply(result, acknowledge_existing=True)
        self.assertTrue(agents.read_bytes().startswith(before + b'\n\n'))
        self.assertEqual(agents.stat().st_mode & 0o777, 0o644)
        self.assertEqual(agents.read_text().count(onboarding.BEGIN), 1)

    def test_isolated_profile_never_contains_an_existing_session(self):
        result = self.inspect()
        applied = self.apply(result)
        config = json.loads((self.bundle / 'generated/supervisor.json').read_text())
        self.assertEqual(applied['status'], 'applied_not_activated')
        self.assertEqual(config['project_root'], str(self.project))
        self.assertEqual(config['default_workflow_policy'], 'supervised_v2')
        self.assertEqual(config['helpers'], {'enabled': False})
        self.assertNotIn('agents', config)
        self.assertFalse((self.bundle / 'state').exists())
        self.assertFalse((self.bundle / 'reviews').exists())

    def test_strict_policy_is_explicit_not_inferred_from_stack(self):
        result = self.inspect(policy='gated_v2', preset='gated')
        self.apply(result)
        self.assertIn('Claude implements after exact plan approval', (self.project / 'AGENTS.md').read_text())
        self.assertIn('gated_v2', (self.bundle / 'generated/supervisor.json').read_text())

    def test_repeat_apply_is_idempotent_and_detects_changed_applied_files(self):
        result = self.inspect()
        self.apply(result)
        original = (self.project / 'AGENTS.md').read_bytes()
        self.assertEqual(self.apply(result)['status'], 'already_applied')
        self.assertEqual((self.project / 'AGENTS.md').read_bytes(), original)
        (self.project / 'AGENTS.md').write_text('modified')
        with self.assertRaisesRegex(SupervisorError, 'files changed'):
            self.apply(result)

    def test_changed_proposal_invalidates_approval(self):
        result = self.inspect()
        with (self.bundle / 'proposal.json').open('ab') as f:
            f.write(b'\n')
        with self.assertRaisesRegex(SupervisorError, 'fingerprint'):
            self.apply(result)
        self.assertFalse((self.project / 'AGENTS.md').exists())

    def test_changed_manifest_requires_new_inspection(self):
        manifest = self.put('package.json', '{"scripts":{"test":"first"}}')
        result = self.inspect()
        manifest.write_text('{"scripts":{"test":"second"}}')
        with self.assertRaisesRegex(SupervisorError, 'evidence changed'):
            self.apply(result)
        self.assertFalse((self.project / 'AGENTS.md').exists())

    def test_added_manifest_requires_new_inspection(self):
        result = self.inspect()
        self.put('package.json', '{}')
        with self.assertRaisesRegex(SupervisorError, 'evidence changed'):
            self.apply(result)

    def test_existing_instructions_changed_requires_new_inspection(self):
        agents = self.put('AGENTS.md', 'before')
        result = self.inspect()
        agents.write_text('after')
        with self.assertRaisesRegex(SupervisorError, 'evidence changed'):
            self.apply(result, acknowledge_existing=True)
        self.assertEqual(agents.read_text(), 'after')

    def test_generated_file_conflict_refuses_all_project_writes(self):
        result = self.inspect()
        (self.bundle / 'generated').mkdir()
        (self.bundle / 'generated/supervisor.json').write_text('existing')
        with self.assertRaisesRegex(SupervisorError, 'target changed'):
            self.apply(result)
        self.assertFalse((self.project / 'AGENTS.md').exists())

    def test_unsafe_output_and_symlink_targets_refused(self):
        with self.assertRaisesRegex(SupervisorError, 'outside'):
            onboarding.inspect_project(self.project, self.project / 'bundle')
        outside = self.home / 'outside'
        outside.write_text('private')
        (self.project / 'AGENTS.md').symlink_to(outside)
        with self.assertRaisesRegex(SupervisorError, 'symlink'):
            self.inspect()
        self.assertEqual(outside.read_text(), 'private')

    def test_symlink_input_not_read_and_never_followed_by_scanner(self):
        outside = self.home / 'secret'
        outside.mkdir()
        (outside / 'package.json').write_text('{"scripts":{"test":"secret"}}')
        (self.project / 'linked').symlink_to(outside, target_is_directory=True)
        (self.project / 'package.json').symlink_to(outside / 'package.json')
        self.inspect()
        self.assertEqual(self.proposal()['analysis']['stacks'], [])

    def test_output_parent_symlink_refused(self):
        (self.home / 'link').symlink_to(self.home, target_is_directory=True)
        with self.assertRaisesRegex(SupervisorError, 'symlinks'):
            onboarding.inspect_project(self.project, self.home / 'link/bundle')

    def test_bounded_scan_marks_truncation_and_apply_refuses(self):
        self.put('first.txt', 'one')
        self.put('second.txt', 'two')
        with patch.object(onboarding, 'MAX_FILES', 1):
            result = self.inspect()
        self.assertTrue(self.proposal()['analysis']['truncated'])
        with self.assertRaisesRegex(SupervisorError, 'truncated'):
            self.apply(result)

    def test_malformed_and_oversized_manifests_are_explicit_gaps(self):
        self.put('package.json', '[]')
        self.put('api/pyproject.toml', 'x' * (onboarding.MAX_BYTES + 1))
        self.inspect()
        self.assertEqual(len(self.proposal()['analysis']['issues']), 2)

    def test_excludes_env_credentials_and_generated_dependencies(self):
        self.put('.env', 'NEVER COPY THIS')
        self.put('credentials.json', 'NEVER COPY THIS')
        self.put('node_modules/a/package.json', '{}')
        self.put('.mypy_cache/fake/pyproject.toml', '[tool.ruff]')
        self.inspect()
        analysis = self.proposal()['analysis']
        self.assertNotIn('.env', analysis['inputs'])
        self.assertEqual(analysis['manifests'], [])
        self.assertNotIn('NEVER COPY THIS', (self.bundle / 'proposal.json').read_text())

    def test_cli_has_no_live_config_binary_or_supervisor_dependencies(self):
        with patch.object(command, 'load_config', side_effect=AssertionError('live config touched')), patch.object(command, 'resolve_herdr_bin', side_effect=AssertionError('Herdr touched')), patch.object(command, 'Supervisor', side_effect=AssertionError('state touched')), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(command.main(['project', 'inspect', str(self.project), '--output', str(self.bundle)]), 0)
            result = json.loads(out.getvalue())
            self.assertEqual(command.main(['project', 'apply', str(self.bundle), '--approval-sha256', result['proposal_sha256']]), 0)

    def test_modified_target_set_refused_even_with_new_review_hash(self):
        self.inspect()
        p = self.proposal()
        p['files'][0]['path'] = '../outside'
        raw = json.dumps(p).encode()
        (self.bundle / 'proposal.json').write_bytes(raw)
        with self.assertRaisesRegex(SupervisorError, 'invalid onboarding'):
            onboarding.apply_project(self.bundle, hashlib.sha256(raw).hexdigest())

    def test_preview_does_not_replace_existing_bundle(self):
        self.inspect()
        with self.assertRaisesRegex(SupervisorError, 'new directory'):
            self.inspect()

    def test_apply_refuses_symlink_generated_parent_before_writing(self):
        result = self.inspect()
        outside = self.home / 'elsewhere'
        outside.mkdir()
        (self.bundle / 'generated').symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(SupervisorError, 'symlink'):
            self.apply(result)
        self.assertFalse((self.project / 'AGENTS.md').exists())
        self.assertEqual(list(outside.iterdir()), [])

    def test_lingering_temp_detected_before_any_write(self):
        (self.project / 'CLAUDE.md.herdr-onboarding-tmp').write_text('interrupted')
        result = self.inspect()
        with self.assertRaisesRegex(SupervisorError, 'temp file'):
            self.apply(result)
        self.assertFalse((self.project / 'AGENTS.md').exists())

    def test_environment_profile_shell_quotes_paths(self):
        self.bundle = self.home / 'profile with spaces'
        result = self.inspect()
        self.apply(result)
        env = (self.bundle / 'generated/profile.env').read_text()
        self.assertIn("export HERDR_SUPERVISOR_CONFIG='", env)
        self.assertNotIn('systemctl', env)
        self.assertNotIn('HERDR_TELEGRAM', env)

    def test_no_models_processes_or_network_actions(self):
        import subprocess
        with patch.object(subprocess, 'run', side_effect=AssertionError('process executed')), patch.object(os, 'system', side_effect=AssertionError('shell executed')):
            result = self.inspect()
            self.apply(result)
        self.assertIn('Do not run project scripts', (self.bundle / 'AGENT_INVESTIGATION.md').read_text())

    def assessment(self, value):
        path = self.home / 'assessment.json'
        path.write_text(json.dumps(value))
        return path

    def test_semantic_assessment_adapts_rules_and_skills_with_citations(self):
        self.put('src/service.py', 'def load(): pass')
        path = self.assessment({'findings': [{'text': 'Service owns reads.', 'evidence': ['src/service.py']}],
                                'rules': [{'text': 'Review changes to the read boundary.', 'evidence': ['src/service.py']}],
                                'checks': [{'text': 'Use the isolated service fixture.', 'evidence': ['src/service.py']}]})
        result = self.inspect(assessment_file=path)
        self.apply(result)
        self.assertIn('Review changes to the read boundary.', (self.project / 'AGENTS.md').read_text())
        skill = (self.bundle / 'generated/skills/project-validation/SKILL.md').read_text()
        self.assertIn('Service owns reads.', skill)
        self.assertIn('src/service.py', skill)
        self.assertIn('unexecuted recommendation', skill)
        self.assertNotIn('def load()', (self.bundle / 'proposal.json').read_text())

    def test_cited_source_change_invalidates_semantic_assessment(self):
        source = self.put('service.py', 'first')
        path = self.assessment({'rules': [{'text': 'Read this service.', 'evidence': ['service.py']}]})
        result = self.inspect(assessment_file=path)
        source.write_text('second')
        with self.assertRaisesRegex(SupervisorError, 'evidence changed'):
            self.apply(result)

    def test_assessment_needs_real_relative_nonsecret_citations(self):
        for citation in ['missing.py', '../outside.md', '.env', 'credentials.json']:
            with self.subTest(citation=citation):
                path = self.assessment({'rules': [{'text': 'Claim.', 'evidence': [citation]}]})
                with self.assertRaises(SupervisorError):
                    self.inspect(assessment_file=path)
                self.assertFalse(self.bundle.exists())

    def test_assessment_rejects_unknown_structure_and_credential_values(self):
        for value in [[], False, '', {'deploy': 'now'}, {'rules': 'text'}, {'findings': [{'text': 'no evidence'}]},
                      {'rules': [{'text': 'password' + '=example-private-value', 'evidence': ['README.md']}]}]:
            path = self.assessment(value)
            with self.assertRaises(SupervisorError):
                self.inspect(assessment_file=path)

    def test_analysis_is_not_an_authority_to_execute(self):
        self.put('README.md', 'ordinary evidence')
        path = self.assessment({'checks': [{'text': 'touch /tmp/do-not-execute', 'evidence': ['README.md']}]})
        import subprocess
        with patch.object(subprocess, 'run', side_effect=AssertionError('executed')):
            result = self.inspect(assessment_file=path)
            self.apply(result)
        self.assertIn('touch /tmp/do-not-execute', (self.bundle / 'generated/skills/project-validation/SKILL.md').read_text())

    def test_concurrent_apply_serializes_before_any_instruction_write(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Event
        result = self.inspect()
        entered, release = Event(), Event()
        actual = onboarding._apply_project_locked
        calls = []
        def slow(*args, **kwargs):
            calls.append('apply')
            entered.set()
            if not release.wait(5):
                raise AssertionError('fixture release timed out')
            return actual(*args, **kwargs)
        with patch.object(onboarding, '_apply_project_locked', side_effect=slow), ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.apply, result)
            self.assertTrue(entered.wait(5))
            try:
                with self.assertRaisesRegex(SupervisorError, 'another apply'):
                    self.apply(result)
                self.assertFalse((self.project / 'AGENTS.md').exists())
            finally:
                release.set()
            self.assertEqual(first.result(timeout=5)['status'], 'applied_not_activated')
        self.assertEqual(calls, ['apply'])

    def test_enforcing_code_drift_invalidates_review(self):
        result = self.inspect()
        actual = onboarding._digest
        code = Path(onboarding.__file__).read_bytes()
        def changed(data):
            return 'changed-code' if data == code else actual(data)
        with patch.object(onboarding, '_digest', side_effect=changed):
            with self.assertRaisesRegex(SupervisorError, 'invalid onboarding'):
                self.apply(result)
        self.assertFalse((self.project / 'AGENTS.md').exists())

    def test_completion_marker_does_not_replace_target_verification(self):
        result = self.inspect()
        (self.bundle / 'applied.json').write_text(json.dumps({'proposal_sha256': result['proposal_sha256'], 'files': []}))
        with self.assertRaises(SupervisorError):
            self.apply(result)
        self.assertFalse((self.project / 'AGENTS.md').exists())

    def test_schema_bool_cannot_stand_in_for_integer(self):
        self.inspect()
        proposal = self.proposal()
        proposal['schema_version'] = True
        raw = json.dumps(proposal).encode()
        (self.bundle / 'proposal.json').write_bytes(raw)
        with self.assertRaisesRegex(SupervisorError, 'invalid onboarding'):
            onboarding.apply_project(self.bundle, hashlib.sha256(raw).hexdigest())

    def test_different_bundles_for_same_project_cannot_race(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Event
        first = self.inspect()
        second_bundle = self.home / 'second-bundle'
        second = onboarding.inspect_project(self.project, second_bundle, preset='adaptive')
        entered, release = Event(), Event()
        actual = onboarding._apply_project_locked
        calls = []
        def slow(*args, **kwargs):
            calls.append('apply')
            entered.set()
            if not release.wait(5):
                raise AssertionError('fixture timeout')
            return actual(*args, **kwargs)
        with patch.object(onboarding, '_apply_project_locked', side_effect=slow), ThreadPoolExecutor(max_workers=2) as pool:
            active = pool.submit(self.apply, first)
            self.assertTrue(entered.wait(5))
            try:
                with self.assertRaisesRegex(SupervisorError, 'another apply.*project'):
                    onboarding.apply_project(second_bundle, second['proposal_sha256'])
            finally:
                release.set()
            self.assertEqual(active.result(timeout=5)['status'], 'applied_not_activated')
        self.assertEqual(calls, ['apply'])
        self.assertIn(str(self.bundle), (self.project / 'AGENTS.md').read_text())
        self.assertFalse((second_bundle / 'generated').exists())

    def test_portable_baseline_is_staged_with_hashes_and_instruction_references(self):
        import herdr_skill_baseline as baseline
        result = self.inspect()
        proposal = self.proposal()
        self.assertEqual(proposal['skill_baseline'], baseline.baseline_manifest())
        self.assertEqual(proposal['skill_baseline']['version'], '1.0.0')
        # Release-owned baseline identity: changes require deliberate version/release review.
        self.assertEqual(proposal['skill_baseline']['sha256'], '87385f63a821ced498982ede213796151dbd6d5799e84ed262384b4575d62f34')
        self.apply(result)
        manifest = json.loads((self.bundle / 'generated/skills/baseline.json').read_text())
        self.assertEqual(len(manifest['files']), 5)
        for path, expected in manifest['files'].items():
            content = (self.bundle / path).read_bytes()
            self.assertEqual(hashlib.sha256(content).hexdigest(), expected)
            self.assertTrue(content.startswith(b'---\nname: '))
            self.assertIn(b'\ndescription: ', content)
            self.assertIn(str(self.bundle / path), (self.project / 'AGENTS.md').read_text())
            self.assertIn(str(self.bundle / path), (self.project / 'CLAUDE.md').read_text())
        self.assertEqual(len(list((self.bundle / 'generated/skills').glob('*/SKILL.md'))), 6)

    def test_baseline_is_self_contained_portable_and_never_imports_installed_skills(self):
        import herdr_skill_baseline as baseline
        for path, content in baseline.baseline_files().items():
            with self.subTest(path=path):
                self.assertNotIn('/home/', content)
                self.assertNotIn('prospera', content.lower())
                self.assertNotIn('ai/development', content)
                self.assertNotIn('10.0.', content)
                self.assertLess(len(content.split()), 400)
                if 'workflow-observer' not in path:
                    self.assertIn('../project-validation/SKILL.md', content)
        self.assertIn('not the full third-party task-observer', baseline.baseline_files()['generated/skills/workflow-observer/SKILL.md'])
        self.assertNotIn('Path.home', Path(baseline.__file__).read_text())

    def test_changed_packaged_baseline_invalidates_pending_approval(self):
        import herdr_skill_baseline as baseline
        result = self.inspect()
        original = baseline.BASELINE_SKILLS['workflow-planning']
        with patch.dict(baseline.BASELINE_SKILLS, {'workflow-planning': (original[0], original[1] + '\nnew baseline rule')}):
            with self.assertRaisesRegex(SupervisorError, 'invalid onboarding'):
                self.apply(result)
        self.assertFalse((self.project / 'AGENTS.md').exists())

    def test_local_skill_customization_is_preserved_and_not_shared(self):
        first = self.inspect()
        self.apply(first)
        local = self.bundle / 'generated/skills/implementation-review/SKILL.md'
        original = local.read_text()
        local.write_text(original + '\nProject-specific observation.\n')
        with self.assertRaisesRegex(SupervisorError, 'files changed'):
            self.apply(first)
        other = self.home / 'another-project'
        other.mkdir()
        second_bundle = self.home / 'another-bundle'
        second = onboarding.inspect_project(other, second_bundle, preset='adaptive')
        onboarding.apply_project(second_bundle, second['proposal_sha256'])
        self.assertEqual((second_bundle / 'generated/skills/implementation-review/SKILL.md').read_text(), original)
        self.assertIn('Project-specific observation.', local.read_text())

    def test_tampered_baseline_file_refuses_even_with_recalculated_proposal_hash(self):
        self.inspect()
        proposal = self.proposal()
        entry = next(f for f in proposal['files'] if f['path'] == 'generated/skills/workflow-planning/SKILL.md')
        entry['content'] += '\nUnversioned alteration.'
        raw = json.dumps(proposal).encode()
        (self.bundle / 'proposal.json').write_bytes(raw)
        with self.assertRaisesRegex(SupervisorError, 'invalid onboarding'):
            onboarding.apply_project(self.bundle, hashlib.sha256(raw).hexdigest())
        self.assertFalse((self.project / 'AGENTS.md').exists())

    def test_default_preserves_existing_workflow_without_instruction_or_baseline_writes(self):
        agents = self.put('AGENTS.md', 'Use our existing single-agent workflow.\n')
        claude = self.put('CLAUDE.md', 'Existing project roles and artifact names.\n')
        before = {p.name: p.read_bytes() for p in (agents, claude)}
        result = onboarding.inspect_project(self.project, self.bundle)
        proposal = self.proposal()
        self.assertEqual(proposal['preset'], 'preserve')
        self.assertFalse(proposal['write_instructions'])
        self.assertIsNone(proposal['skill_baseline'])
        self.assertFalse(any(f['scope'] == 'project' for f in proposal['files']))
        self.apply(result)
        self.assertEqual({p.name: p.read_bytes() for p in (agents, claude)}, before)
        self.assertEqual(len(list((self.bundle / 'generated/skills').glob('*/SKILL.md'))), 1)
        self.assertFalse((self.bundle / 'generated/skills/baseline.json').exists())
        self.assertIn('not selected', (self.bundle / 'DISCOVERY.md').read_text())

    def test_default_on_empty_project_creates_no_agent_instructions(self):
        result = onboarding.inspect_project(self.project, self.bundle)
        self.apply(result)
        self.assertEqual(list(self.project.iterdir()), [])
        self.assertEqual(json.loads((self.bundle / 'generated/supervisor.json').read_text())['default_workflow_policy'], 'supervised_v2')

    def test_explicit_preserve_context_appendix_does_not_assign_roles_or_workflow(self):
        result = onboarding.inspect_project(self.project, self.bundle, write_instructions=True)
        self.apply(result)
        text = (self.project / 'AGENTS.md').read_text()
        self.assertIn("Keep the project's existing workflow", text)
        self.assertNotIn('Plan and obtain scope approval', text)
        self.assertNotIn('Codex plans and independently reviews', text)
        self.assertNotIn('CODEX_PLAN.md', text)
        self.assertNotIn('complete the investigation brief before activation', text)
        self.assertNotIn('workflow-planning/SKILL.md', text)

    def test_strict_policy_requires_explicit_matching_preset(self):
        with self.assertRaisesRegex(SupervisorError, 'conflicts with preset'):
            onboarding.inspect_project(self.project, self.bundle, policy='gated_v2')
        self.assertFalse(self.bundle.exists())

    def test_preserve_does_not_force_existing_managed_instructions_to_upgrade(self):
        agents = self.put('AGENTS.md', onboarding.BEGIN + '\nExisting managed workflow\n' + onboarding.END)
        original = agents.read_bytes()
        result = onboarding.inspect_project(self.project, self.bundle)
        self.apply(result)
        self.assertEqual(agents.read_bytes(), original)

    def test_cli_defaults_to_preserve_and_requires_no_live_service(self):
        with patch.object(command, 'load_config', side_effect=AssertionError('live config')), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(command.main(['project', 'inspect', str(self.project), '--output', str(self.bundle)]), 0)
        result = json.loads(output.getvalue())
        self.apply(result)
        self.assertEqual(list(self.project.iterdir()), [])
        self.assertIsNone(self.proposal()['skill_baseline'])
