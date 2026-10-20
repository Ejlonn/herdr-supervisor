"""Opt-in, operator-dispatched pane helpers. Never changes workflow owners or gates."""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import math
import re
import uuid
from pathlib import Path
from typing import Any

from herdr_cli import match_exact_session, session_identity
from herdr_core import READY_STATES, TERMINAL_STATES, HerdrError, SupervisorError, atomic_write_json, load_json, valid_pane_id
from herdr_redaction import credential_value_present, redact
from herdr_session_catalog import canonical_identity
from herdr_validation import load_query_registry

OPEN_JOBS = {'PREPARED', 'SUBMITTING', 'ACCEPTED', 'UNCERTAIN', 'CANCEL_REQUESTED'}
SAFE_PARENT_STATES = {'PAUSED', 'WAIT_USER', 'WAIT_PLAN_APPROVAL', 'WAIT_RUNTIME_VALIDATION', 'WAIT_PUSH_APPROVAL'}
ID_RE = re.compile(r'[a-z][a-z0-9_-]{0,31}\Z')


def readonly_process(info: Any, model: str, cwd: str) -> bool:
    if not isinstance(info, dict):
        return False
    processes = info.get('foreground_processes')
    if not isinstance(processes, list) or len(processes) != 1:
        return False
    p = processes[0]
    argv = p.get('argv') if isinstance(p, dict) else None
    if not isinstance(argv, list) or not argv or any(not isinstance(v, str) for v in argv):
        return False
    if Path(argv[0]).name != 'codex' or p.get('cwd') != cwd:
        return False
    options: dict[str, str] = {}
    args = argv[1:]
    if args.count('--no-daemon') > 1:
        return False
    args = [arg for arg in args if arg != '--no-daemon']
    if args[:1] == ['resume']:
        if len(args) < 2 or not canonical_identity(args[1]):
            return False
        args = args[2:]
    while args:
        if len(args) < 2 or args[0] not in ('--sandbox', '--ask-for-approval', '--model', '-C', '--cd') or args[0] in options:
            return False
        options[args[0]] = args[1]
        args = args[2:]
    if '-C' in options and '--cd' in options:
        return False
    if options.get('-C', options.get('--cd', cwd)) != cwd:
        return False
    if any(options.get(flag) != value for flag, value in (('--sandbox', 'read-only'), ('--ask-for-approval', 'never'), ('--model', model))):
        return False
    return True


def job_prompt(job: dict[str, Any], instruction: str) -> str:
    # Do not place a parseable result in the request. Results are one logical JSON line.
    return (f"Read-only helper job {job['job_id']}. Fixed model: {job['model']}. Do not change files, "
            "spawn/delegate agents, approve gates, or emit HERDR routing blocks.\n"
            + instruction + "\nReturn one JSON object on its own logical line with precisely these keys: "
            "helper_result_version (integer 1), job_id (this job's ID), nonce (" + job['nonce'] + "), "
            "instruction_sha256 (" + job['instruction_sha256'] + "), outcome (done or failed), summary "
            "(at most 1200 characters). No other keys, paths, tool output, credentials or message bodies.")


def parse_job_result(text: str, job: dict[str, Any]) -> dict[str, Any]:
    if len(text.encode()) > 65536:
        raise SupervisorError('helper output exceeds collection bound')
    matches = []
    for line in text.splitlines():
        if not line.strip().startswith('{'):
            continue
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if not isinstance(value, dict) or set(value) != {'helper_result_version', 'job_id', 'nonce', 'instruction_sha256', 'outcome', 'summary'}:
            continue
        if type(value['helper_result_version']) is not int or value['helper_result_version'] != 1:
            continue
        if any(value[k] != job[k] for k in ('job_id', 'nonce', 'instruction_sha256')):
            continue
        if value['outcome'] not in ('done', 'failed') or not isinstance(value['summary'], str) or not 1 <= len(value['summary']) <= 1200:
            continue
        matches.append({'outcome': value['outcome'], 'summary': redact(value['summary'], limit=1200)})
    if len(matches) != 1:
        raise SupervisorError('no unique job-bound helper result; collect once after settlement or cancel')
    return matches[0]


class HelperManager:
    def __init__(self, supervisor: Any):
        self.sup = supervisor
        self.root = supervisor.paths.state_dir / 'helpers'

    @contextlib.contextmanager
    def locked(self) -> Any:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (self.root / 'registry.lock').open('a+') as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def enabled(self) -> None:
        if not self.sup.config.get('helpers', {}).get('enabled'):
            raise SupervisorError('helpers are disabled; opt in with helpers.enabled')

    def registry(self) -> dict[str, Any]:
        path = self.root / 'registry.json'
        if not path.exists():
            return {}
        value = load_json(path, label='helper registry')
        if not isinstance(value, dict) or value.get('schema_version') != 1 or not isinstance(value.get('workers'), dict):
            raise SupervisorError('helper registry schema is invalid; restore a verified registry')
        return value['workers']

    def jobs(self) -> list[dict[str, Any]]:
        if not self.root.exists():
            return []
        return [load_json(p, label='helper job') for p in sorted(self.root.glob('job-*.json'))]

    def view(self) -> dict[str, Any]:
        return {'enabled': bool(self.sup.config.get('helpers', {}).get('enabled')), 'workers': self.registry(),
                'jobs': [{k: j.get(k) for k in ('job_id', 'helper', 'status', 'outcome', 'summary', 'next_action')} for j in self.jobs()]}

    def _save(self, job: dict[str, Any]) -> None:
        atomic_write_json(self.root / f"job-{job['job_id']}.json", job)

    def _job(self, job_id: str) -> dict[str, Any]:
        if not canonical_identity(job_id):
            raise SupervisorError('helper job ID must be canonical UUID')
        return load_json(self.root / f'job-{job_id}.json', label='helper job')

    def _verify(self, binding: dict[str, Any], *, ready: bool = True) -> dict[str, Any]:
        live = self.sup.herdr.list_agents()
        match = match_exact_session(live, binding['provider'], binding['session_id'])
        exact = [match.record] if match.record is not None else []
        if match.ambiguous or match.provider_conflicts or len(exact) != 1 or exact[0].get('pane_id') != binding['pane_id']:
            raise SupervisorError('helper identity missing, duplicate or moved; explicitly re-register after resolving it')
        agent = exact[0]
        if ready and agent.get('agent_status') not in READY_STATES:
            raise SupervisorError('helper is busy or blocked; wait for settlement')
        protected = [*self.sup.owners().values(), *load_query_registry(self.sup.paths).values()]
        if any(b.get('pane_id') == binding['pane_id'] or b.get('session_id') == binding['session_id'] for b in protected):
            raise SupervisorError('workflow/query owners cannot be helper workers')
        if binding['provider'] != 'codex' or not readonly_process(self.sup.herdr.process_info(binding['pane_id']), binding['model'], binding['cwd']):
            raise SupervisorError('read-only fixed-model process contract unavailable; launch a dedicated Codex helper with explicit sandbox, approval and model flags')
        return agent

    def register(self, name: str, pane: str, model: str, cwd: str, *, apply: bool = False, expected_session: str | None = None) -> dict[str, Any]:
        self.enabled()
        if not ID_RE.fullmatch(name) or not valid_pane_id(pane) or not re.fullmatch(r'[A-Za-z0-9_.:/-]{1,80}', model):
            raise SupervisorError('invalid helper name, pane or model')
        directory = str(Path(cwd).resolve(strict=True))
        if not Path(directory).is_dir():
            raise SupervisorError('helper cwd must be an existing directory')
        with self.locked():
            registry = self.registry()
            live = [a for a in self.sup.herdr.list_agents() if a.get('pane_id') == pane]
            if len(live) != 1 or not canonical_identity(session_identity(live[0])):
                raise SupervisorError('helper needs exactly one native identity in the selected pane')
            binding = {'provider': live[0]['agent'], 'session_id': session_identity(live[0]), 'pane_id': pane,
                       'model': model, 'cwd': directory, 'access': 'read-only'}
            if apply and expected_session != binding["session_id"]:
                raise SupervisorError("apply requires the exact --session-id from the registration preview")
            self._verify(binding)
            if any(j.get('helper') == name and j.get('status') in OPEN_JOBS for j in self.jobs()):
                raise SupervisorError('helper has an unresolved job; collect or settle cancellation first')
            if any(n != name and (b.get('pane_id') == pane or b.get('session_id') == binding['session_id']) for n, b in registry.items()):
                raise SupervisorError('another helper already owns this pane/session')
            if apply:
                registry[name] = binding
                atomic_write_json(self.root / 'registry.json', {'schema_version': 1, 'workers': registry})
            return {'applied': apply, 'helper': name, 'binding': binding}

    def _parent(self) -> dict[str, Any]:
        state = self.sup.store.read_state()
        if state is None or state.get('supervisor_state') in TERMINAL_STATES or state.get('supervisor_state') not in SAFE_PARENT_STATES:
            raise SupervisorError('helper dispatch requires a paused parent or human gate; pause the parent first')
        delivery = state.get('delivery') or {}
        turn = delivery.get('turn_id') or (state.get('last_protocol') or {}).get('turn_id')
        provider = state.get('active_agent')
        owner = self.sup.owners().get(provider, {})
        if not canonical_identity(state.get('run_id')) or not canonical_identity(turn) or not canonical_identity(owner.get('session_id')):
            raise SupervisorError('parent run/turn/native identity is incomplete')
        match = match_exact_session(self.sup.herdr.list_agents(), provider, owner['session_id'])
        carriers = [match.record] if match.record is not None else []
        if match.ambiguous or match.provider_conflicts or len(carriers) != 1 or carriers[0].get('pane_id') != owner.get('pane_id') or carriers[0].get('agent_status') not in READY_STATES:
            raise SupervisorError('parent session must be unique, settled and in its recorded pane')
        return {'run_id': state['run_id'], 'turn_id': turn, 'provider': provider, 'session_id': owner['session_id']}

    def _current(self, job: dict[str, Any]) -> None:
        if self._parent() != job['parent']:
            raise SupervisorError('parent changed; cancel the stale helper job')

    def _instruction(self, path: Path) -> tuple[str, str, str]:
        root = Path(self.sup.config['review_root']).resolve()
        resolved = path.resolve(strict=True)
        if path.is_symlink() or not resolved.is_relative_to(root) or resolved.suffix != '.md':
            raise SupervisorError('helper instructions must be a Markdown file inside review_root')
        raw = resolved.read_bytes()
        if not 1 <= len(raw) <= 8192:
            raise SupervisorError('helper instructions must be 1..8192 bytes')
        text = raw.decode('utf-8')
        if credential_value_present(text):
            raise SupervisorError('helper instructions contain possible credentials; remove them before dispatch')
        return text, str(resolved), hashlib.sha256(raw).hexdigest()

    def dispatch(self, name: str, instruction_path: Path, *, deadline_seconds: int = 300) -> dict[str, Any]:
        self.enabled()
        if type(deadline_seconds) is not int or not 30 <= deadline_seconds <= 1800:
            raise SupervisorError('helper deadline must be 30..1800 seconds')
        with self.locked(), self.sup.store.transaction():
            parent = self._parent()
            registry = self.registry()
            if name not in registry:
                raise SupervisorError('register the helper before dispatch')
            binding = registry[name]
            if binding.get('quarantined'):
                raise SupervisorError('helper was abandoned; verify settlement and explicitly register it again')
            jobs = self.jobs()
            if sum(j.get('status') in OPEN_JOBS for j in jobs) >= 2 or any(j.get('helper') == name and j.get('status') in OPEN_JOBS for j in jobs):
                raise SupervisorError('helper slots are occupied; collect or settle cancellation first')
            instruction, path, digest = self._instruction(instruction_path)
            self._verify(binding)
            job = {'schema_version': 1, 'job_id': str(uuid.uuid4()), 'nonce': str(uuid.uuid4()), 'helper': name,
                   'parent': parent, 'binding': binding, 'model': binding['model'], 'instruction_path': path,
                   'instruction_sha256': digest, 'deadline': self.sup.clock() + deadline_seconds,
                   'status': 'PREPARED', 'next_action': 'collect or cancel; never resend this job'}
            self._save(job)
            self._current(job)
            self._verify(binding)
            job['status'] = 'SUBMITTING'
            self._save(job)  # a crash here means uncertain, never a permission to resend
            try:
                if self.sup.herdr.prompt_ack_supported():
                    self.sup.herdr.prompt_ack(binding['pane_id'], job_prompt(job, instruction), timeout_ms=5000)
                else:
                    self.sup.herdr.prompt(binding['pane_id'], job_prompt(job, instruction), timeout_ms=5000)
                job['status'] = 'ACCEPTED'
            except HerdrError:
                job['status'] = 'UNCERTAIN'
            self._save(job)
            return {k: job[k] for k in ('job_id', 'status', 'next_action')}

    def collect(self, job_id: str) -> dict[str, Any]:
        self.enabled()
        with self.locked(), self.sup.store.transaction():
            job = self._job(job_id)
            if job.get('status') not in OPEN_JOBS:
                return {k: job.get(k) for k in ('job_id', 'status', 'outcome', 'summary')}
            self._verify(job['binding'])
            if job['status'] == 'CANCEL_REQUESTED':
                job.update(status='CANCELLED', next_action='worker settled; slot released')
                self._save(job)
                return {'job_id': job_id, 'status': 'CANCELLED'}
            self._current(job)
            if not isinstance(job.get('deadline'), (int, float)) or not math.isfinite(job['deadline']) or self.sup.clock() > job['deadline']:
                raise SupervisorError('helper deadline expired; cancel then collect after settlement')
            _, _, digest = self._instruction(Path(job['instruction_path']))
            if digest != job['instruction_sha256']:
                raise SupervisorError('helper instructions changed; cancel the stale job')
            text = self.sup.herdr.read_agent(job['binding']['pane_id'], source='recent-unwrapped', lines=80)
            result = parse_job_result(text, job)
            job.update(result, status='COMPLETED', next_action='parent may review helper evidence; gates unchanged')
            self._save(job)
            return {k: job[k] for k in ('job_id', 'status', 'outcome', 'summary')}

    def cancel(self, job_id: str, *, abandon: bool = False) -> dict[str, Any]:
        with self.locked():
            job = self._job(job_id)
            if job.get('status') in OPEN_JOBS:
                if abandon:
                    registry = self.registry()
                    worker = registry.get(job['helper'])
                    if worker and worker.get('session_id') == job['binding']['session_id']:
                        worker['quarantined'] = True
                        atomic_write_json(self.root / 'registry.json', {'schema_version': 1, 'workers': registry})
                    job.update(status='ABANDONED', next_action='slot released; worker excluded until explicitly re-registered after verified settlement; no input sent')
                else:
                    job.update(status='CANCEL_REQUESTED', next_action='collect once the worker settles; cancellation sent no input')
                self._save(job)
            return {k: job.get(k) for k in ('job_id', 'status', 'next_action')}
