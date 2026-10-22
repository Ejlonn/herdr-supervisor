"""Optional full skills: explicit imports, resource integrity and project isolation."""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import herdr_command as command
import herdr_onboarding as onboarding
import herdr_support_skills as support
from herdr_core import SupervisorError


class SupportSkillTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.project = self.base / 'project'
        self.project.mkdir()
        self.bundle = self.base / 'proposal'

    def skill(self, name):
        root = self.base / 'sources' / name
        root.mkdir(parents=True)
        (root / 'SKILL.md').write_text(f'---\nname: {name}\ndescription: Test-only {name} fixture.\n---\n\nRead references/guide.md; use scripts/helper.py only when authorized.\n')
        for relative, data in [('references/guide.md', b'Full supporting guide.'), ('scripts/helper.py', b'raise AssertionError("must not run")\n'), ('LICENSE', b'Fixture attribution must survive.')]:
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        return root

    def inspect(self, *specs):
        return onboarding.inspect_project(self.project, self.bundle, support_specs=list(specs), preset='adaptive')

    def apply(self, result):
        return onboarding.apply_project(self.bundle, result['proposal_sha256'])

    def test_all_four_full_skills_are_explicit_and_preserve_resources(self):
        roots = {name: self.skill(name) for name in support.SUPPORT_NAMES}
        result = self.inspect(*(f'{name}={root}' for name, root in roots.items()))
        proposal = json.loads((self.bundle / 'proposal.json').read_text())
        self.assertEqual(set(proposal['support_skills']['skills']), set(support.SUPPORT_NAMES))
        self.assertFalse((self.bundle / 'generated').exists())
        self.apply(result)
        for name, root in roots.items():
            for original in root.rglob('*'):
                if original.is_file():
                    copied = self.bundle / 'generated/skills' / name / original.relative_to(root)
                    self.assertEqual(copied.read_bytes(), original.read_bytes())
        guide = (self.bundle / 'generated/skills/SUPPORT.md').read_text()
        self.assertIn('PyYAML', guide)
        self.assertIn(str(self.bundle / 'project-learning'), guide)
        self.assertIn('do not run both', guide)
        self.assertIn(str(self.bundle / 'generated/skills/ui-ux-pro-max/scripts/search.py'), guide)
        self.assertIn('Optional full support skills:', (self.project / 'AGENTS.md').read_text())

    def test_default_setup_does_not_inspect_global_skills(self):
        with patch.object(support, 'support_read', side_effect=AssertionError('read unselected skill')):
            result = self.inspect()
            self.apply(result)
        self.assertFalse((self.bundle / 'skill-sources').exists())
        self.assertFalse((self.bundle / 'generated/skills/SUPPORT.md').exists())

    def test_binary_and_large_ui_data_survive_verbatim(self):
        root = self.skill('ui-ux-pro-max')
        binary = bytes(range(256)) * 1300
        (root / 'data').mkdir()
        (root / 'data/catalog.bin').write_bytes(binary)
        result = self.inspect(f'ui-ux-pro-max={root}')
        self.apply(result)
        self.assertEqual((self.bundle / 'generated/skills/ui-ux-pro-max/data/catalog.bin').read_bytes(), binary)
        self.assertEqual(self.apply(result)['status'], 'already_applied')

    def test_original_source_updates_cannot_change_the_reviewed_snapshot(self):
        root = self.skill('stop-slop')
        original = (root / 'SKILL.md').read_bytes()
        result = self.inspect(f'stop-slop={root}')
        (root / 'SKILL.md').write_text('global source changed after inspect')
        self.apply(result)
        self.assertEqual((self.bundle / 'generated/skills/stop-slop/SKILL.md').read_bytes(), original)
        self.assertEqual((root / 'SKILL.md').read_text(), 'global source changed after inspect')

    def test_staged_resource_tampering_refuses_all_target_writes(self):
        root = self.skill('skill-creator')
        result = self.inspect(f'skill-creator={root}')
        (self.bundle / 'skill-sources/skill-creator/scripts/helper.py').write_text('changed')
        with self.assertRaisesRegex(SupervisorError, 'resource changed'):
            self.apply(result)
        self.assertFalse((self.project / 'AGENTS.md').exists())
        self.assertFalse((self.bundle / 'generated').exists())

    def test_modified_source_binding_refused_even_with_new_proposal_hash(self):
        root = self.skill('stop-slop')
        self.inspect(f'stop-slop={root}')
        proposal = json.loads((self.bundle / 'proposal.json').read_text())
        entry = next(e for e in proposal['files'] if 'source' in e)
        entry['source'] = '../outside'
        raw = json.dumps(proposal).encode()
        (self.bundle / 'proposal.json').write_bytes(raw)
        with self.assertRaisesRegex(SupervisorError, 'invalid onboarding'):
            onboarding.apply_project(self.bundle, hashlib.sha256(raw).hexdigest())
        self.assertFalse((self.project / 'AGENTS.md').exists())

    def test_source_symlinks_and_missing_helpers_refuse_or_remain_explicit(self):
        root = self.skill('task-observer')
        (root / 'references/linked.md').symlink_to(root / 'LICENSE')
        with self.assertRaisesRegex(SupervisorError, 'symlinks'):
            self.inspect(f'task-observer={root}')
        self.assertFalse(self.bundle.exists())

    def test_secret_filenames_refused_before_any_proposal_is_written(self):
        root = self.skill('stop-slop')
        for name in ('.env', 'credentials.json', 'id_rsa'):
            with self.subTest(name=name):
                secret = root / name
                secret.write_text('private')
                try:
                    with self.assertRaisesRegex(SupervisorError, 'credential'):
                        self.inspect(f'stop-slop={root}')
                    self.assertFalse(self.bundle.exists())
                finally:
                    secret.unlink()

    def test_invalid_unknown_duplicate_or_mismatched_skills_refused(self):
        root = self.skill('stop-slop')
        for specs in [('unknown=' + str(root),), ('stop-slop',), ('stop-slop=' + str(root), 'stop-slop=' + str(root)), ('task-observer=' + str(root),)]:
            with self.subTest(specs=specs), self.assertRaises(SupervisorError):
                self.inspect(*specs)
        self.assertFalse(self.bundle.exists())

    def test_snapshot_limits_fail_before_staging(self):
        root = self.skill('stop-slop')
        for limit in ['SUPPORT_FILE_LIMIT', 'SUPPORT_TOTAL_LIMIT', 'SUPPORT_COUNT_LIMIT']:
            with self.subTest(limit=limit), patch.object(support, limit, 1), self.assertRaises(SupervisorError):
                self.inspect(f'stop-slop={root}')
            self.assertFalse(self.bundle.exists())

    def test_local_imported_skill_edit_is_not_overwritten(self):
        root = self.skill('stop-slop')
        result = self.inspect(f'stop-slop={root}')
        self.apply(result)
        local = self.bundle / 'generated/skills/stop-slop/SKILL.md'
        local.write_text('local customized version')
        with self.assertRaisesRegex(SupervisorError, 'files changed'):
            self.apply(result)
        self.assertEqual(local.read_text(), 'local customized version')
        self.assertIn('Test-only', (root / 'SKILL.md').read_text())

    def test_cli_selection_is_offline_and_never_executes_helpers(self):
        root = self.skill('ui-ux-pro-max')
        import subprocess
        with patch.object(subprocess, 'run', side_effect=AssertionError('helper executed')), patch.object(command, 'load_config', side_effect=AssertionError('live config')), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(command.main(['project', 'inspect', str(self.project), '--output', str(self.bundle), '--support-skill', f'ui-ux-pro-max={root}']), 0)
            result = json.loads(output.getvalue())
            self.assertEqual(command.main(['project', 'apply', str(self.bundle), '--approval-sha256', result['proposal_sha256']]), 0)

    def test_entrypoint_name_prefix_does_not_count_as_exact_name(self):
        root = self.skill('stop-slop')
        entry = root / 'SKILL.md'
        entry.write_text(entry.read_text().replace('name: stop-slop', 'name: stop-slop-extra'))
        with self.assertRaisesRegex(SupervisorError, 'frontmatter'):
            self.inspect(f'stop-slop={root}')

    def test_support_enforcement_code_drift_invalidates_approval(self):
        root = self.skill('stop-slop')
        result = self.inspect(f'stop-slop={root}')
        with patch.object(onboarding, 'support_tool_identity', return_value='changed'):
            with self.assertRaisesRegex(SupervisorError, 'invalid onboarding'):
                self.apply(result)
        self.assertFalse((self.project / 'AGENTS.md').exists())

    def test_helper_command_quotes_operator_selected_bundle_path(self):
        import shlex
        root = self.skill('ui-ux-pro-max')
        self.bundle = self.base / 'profile with spaces and "quotes"'
        result = self.inspect(f'ui-ux-pro-max={root}')
        self.apply(result)
        guide = (self.bundle / 'generated/skills/SUPPORT.md').read_text()
        rendered = guide.split('use `', 1)[1].split('`', 1)[0]
        self.assertEqual(shlex.split(rendered), ['python3', str(self.bundle / 'generated/skills/ui-ux-pro-max/scripts/search.py')])

    def test_full_support_import_does_not_require_adopting_the_bundled_workflow(self):
        root = self.skill('stop-slop')
        result = onboarding.inspect_project(self.project, self.bundle, support_specs=[f'stop-slop={root}'])
        self.apply(result)
        self.assertFalse((self.project / 'AGENTS.md').exists())
        self.assertFalse((self.project / 'CLAUDE.md').exists())
        self.assertTrue((self.bundle / 'generated/skills/stop-slop/SKILL.md').exists())
        self.assertFalse((self.bundle / 'generated/skills/workflow-planning').exists())
        self.assertIsNone(json.loads((self.bundle / 'proposal.json').read_text())['skill_baseline'])
