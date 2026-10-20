"""Explicit local support-skill snapshots. Never discover global skills or execute helpers."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import stat
from pathlib import Path
from typing import Any

from herdr_core import SupervisorError

SUPPORT_NAMES = ('stop-slop', 'ui-ux-pro-max', 'skill-creator', 'task-observer')
SUPPORT_FILE_LIMIT = 2 * 1024 * 1024
SUPPORT_TOTAL_LIMIT = 16 * 1024 * 1024
SUPPORT_COUNT_LIMIT = 256
SUPPORT_SKIP = {'.git', '__pycache__', '.pytest_cache', '.mypy_cache', '.ruff_cache'}


def support_read(path: Path) -> bytes:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > SUPPORT_FILE_LIMIT:
                raise SupervisorError('support resource is not a bounded regular file')
            data = stream.read(SUPPORT_FILE_LIMIT + 1)
            if len(data) > SUPPORT_FILE_LIMIT:
                raise SupervisorError('support resource exceeds file limit')
            return data
    except OSError as error:
        raise SupervisorError('cannot safely read support resource') from error


def _support_path(value: Any) -> str:
    if not isinstance(value, str):
        raise SupervisorError('support resource path must be text')
    path = Path(value)
    if not value or path.is_absolute() or '..' in path.parts or path.as_posix() != value or '\\' in value or any(ord(c) < 32 for c in value):
        raise SupervisorError('unsafe support resource path')
    if any(part in {'.env', '.aws', '.ssh', '.netrc', '.npmrc', '.pypirc', 'credentials.json', 'id_rsa', 'id_ed25519', 'bot-token'} or part.startswith('.env.') for part in path.parts):
        raise SupervisorError('credential files cannot be imported as skill resources')
    return value


def support_tool_identity() -> str:
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def support_identity(skills: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(skills, sort_keys=True).encode()).hexdigest()


def collect_support(specs: list[str]) -> tuple[dict[str, Any], dict[str, bytes]]:
    """Read explicit roots once; source changes after inspect cannot alter the staged candidate."""
    skills: dict[str, Any] = {}
    payloads: dict[str, bytes] = {}
    total = 0
    visited = 0
    for spec in specs:
        name, separator, location = spec.partition('=')
        if name not in SUPPORT_NAMES or not separator or not location or name in skills:
            raise SupervisorError('use --support-skill NAME=DIR with a unique supported name')
        root = Path(location).expanduser().absolute()
        if not root.is_dir() or root.resolve() != root:
            raise SupervisorError('support skill root must be a canonical directory without symlinks')
        files: dict[str, str] = {}
        licenses: list[str] = []
        def fail(error: OSError) -> None:
            raise SupervisorError('cannot inventory support skill') from error
        for directory, dirs, names in os.walk(root, followlinks=False, onerror=fail):
            parent = Path(directory)
            if len(parent.relative_to(root).parts) > 8:
                raise SupervisorError('support skill tree exceeds depth limit')
            dirs[:] = sorted(d for d in dirs if d not in SUPPORT_SKIP)
            visited += len(dirs) + len(names)
            if visited > 1024:
                raise SupervisorError('support tree exceeds entry limit')
            if any((parent / d).is_symlink() for d in dirs):
                raise SupervisorError('support skill symlinks are not portable')
            for filename in sorted(names):
                if filename.endswith(('.pyc', '.pyo')):
                    continue
                path = parent / filename
                relative = _support_path(path.relative_to(root).as_posix())
                if path.is_symlink():
                    raise SupervisorError('support skill symlinks are not portable')
                if len(payloads) >= SUPPORT_COUNT_LIMIT:
                    raise SupervisorError('too many support resource files')
                data = support_read(path)
                total += len(data)
                if total > SUPPORT_TOTAL_LIMIT:
                    raise SupervisorError('support skills exceed total size limit')
                files[relative] = hashlib.sha256(data).hexdigest()
                payloads[f'skill-sources/{name}/{relative}'] = data
                if 'license' in filename.lower() or 'copying' in filename.lower() or 'notice' in filename.lower():
                    licenses.append(relative)
        entry = payloads.get(f'skill-sources/{name}/SKILL.md')
        if entry is None:
            raise SupervisorError('support skill needs SKILL.md')
        try:
            text = entry.decode('utf-8')
        except UnicodeError as error:
            raise SupervisorError('support skill entrypoint must be UTF-8') from error
        header = text.split('---', 2)
        expected_name = rf"(?m)^name: *(?:{re.escape(name)}|'{re.escape(name)}'|\"{re.escape(name)}\") *$"
        if len(header) != 3 or header[0].strip() or not re.search(expected_name, header[1]) or len(re.findall(r'(?m)^name:', header[1])) != 1 or not re.search(r'(?m)^description: *\S', header[1]):
            raise SupervisorError('support skill frontmatter must match the selected name')
        skills[name] = {'files': files, 'license_files': sorted(licenses), 'source': str(root),
                        'revision_sha256': support_identity(files),
                        'redistribution': 'local snapshot only; verify upstream terms before public redistribution'}
    return {'schema_version': 1, 'skills': skills, 'sha256': support_identity(skills)}, payloads


def support_entries(manifest: Any) -> list[dict[str, Any]]:
    """Validate all manifest paths before they can become reviewed apply targets."""
    if not isinstance(manifest, dict) or type(manifest.get('schema_version')) is not int or manifest['schema_version'] != 1:
        raise SupervisorError('invalid support manifest')
    skills = manifest.get('skills')
    if not isinstance(skills, dict) or set(skills) - set(SUPPORT_NAMES) or manifest.get('sha256') != support_identity(skills):
        raise SupervisorError('support manifest identity mismatch')
    entries = []
    for name, info in sorted(skills.items()):
        if not isinstance(info, dict) or not isinstance(info.get('files'), dict) or 'SKILL.md' not in info['files']:
            raise SupervisorError('invalid support resource inventory')
        if info.get('revision_sha256') != support_identity(info['files']):
            raise SupervisorError('support revision identity mismatch')
        for relative, digest in sorted(info['files'].items()):
            _support_path(relative)
            if not isinstance(digest, str) or len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
                raise SupervisorError('invalid support file digest')
            entries.append({'scope': 'bundle', 'path': f'generated/skills/{name}/{relative}', 'before_sha256': None,
                            'source': f'skill-sources/{name}/{relative}', 'sha256': digest, 'mode': 'create'})
    if len(entries) > SUPPORT_COUNT_LIMIT:
        raise SupervisorError('too many support resource files')
    return entries


def support_guide(manifest: dict[str, Any], bundle: Path) -> str:
    names = sorted(manifest['skills'])
    lines = ['# Optional support skills', '', 'Explicit, hash-pinned local snapshots; no helpers were executed or dependencies installed.',
             'Read only the skill relevant to the task. Existing project instructions and operator authority take precedence.', '']
    for name in names:
        root = bundle / 'generated/skills' / name
        lines.append(f'- {name}: `{root / "SKILL.md"}` (snapshot {manifest["skills"][name]["revision_sha256"]}).')
        if name == 'ui-ux-pro-max':
            search_command = 'python3 ' + shlex.quote(str(root / 'scripts/search.py'))
            lines.append(f'  Local adapter: use `{search_command}` with a scoped query. The copied upstream entrypoint may mention CLAUDE_PLUGIN_ROOT; resolve helpers under this skill directory, not a global plugin path. Python 3 is required.')
        if name == 'skill-creator':
            lines.append('  Stage created/updated skills in this external project bundle, never global skill directories. Its Python validation/YAML generation helpers need PyYAML; install dependencies only with separate authority.')
        if name == 'task-observer':
            lines.append(f'  Pin its workspace folder to `{bundle / "project-learning"}` so logs and staged skill changes stay project-local. This full observer replaces workflow-observer for this project; do not run both. No pre-existing observation history was imported.')
    lines += ['', 'Upstream resources, license files and attribution are preserved byte-for-byte. Missing/unclear redistribution terms are not permission to publish the snapshot.',
              'Local observations never promote updates to the baseline, other projects or global skills. Review any local skill edits; new imports require a fresh proposal.']
    return '\n'.join(lines) + '\n'
