"""Validated helper-context descriptor, per-project helper unit rendering and the shared read-only
helper-readiness report.

Fresh Codex depends on the user-scoped reset-helper journal: the producer (supervisor) writes a request
below `<state-dir>/codex-reset/pending` and a systemd path watcher starts the oneshot worker that talks to
the Codex app-server. The producer and the worker must agree on the journal root, the Codex executable,
CODEX_HOME and the working directory; this module resolves that agreement once. A request binds the
descriptor fingerprint and the worker refuses a request whose fingerprint it cannot reproduce.

Nothing here writes to a journal, starts a unit or calls a model. Rendering returns text for the operator."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping

CONTEXT_VERSION = 1
DEFAULT_JOURNAL_SUBPATH = ".local/state/herdr-supervisor/codex-reset"
WORKER_COMMAND_SUBPATH = ".local/bin/herdr-codex-reset-worker"
GLOBAL_PATH_UNIT = "herdr-codex-reset-worker.path"
GLOBAL_SERVICE_UNIT = "herdr-codex-reset-worker.service"
CODEX_BIN_ENV = "HERDR_CODEX_BIN"
_FINGERPRINT_DOMAIN = "herdr-helper-context-v1\0"
_FIELDS = ("version", "instance", "journal_root", "codex_bin", "codex_home", "working_directory")
# SHA-256 of the shipped bin/herdr-codex-reset-worker launcher (a test pins it to the file in the source tree).
WORKER_LAUNCHER_SHA256 = "6ac2ee6cb3a295260eae851653354f40b921e62e18e749c18ccf1aa319cbcb6e"
# The installed worker module must declare it understands context-bound requests.
WORKER_PROTOCOL_MARKER = "HELPER_CONTEXT_PROTOCOL = 1"
# How a fresh Codex pane is launched. `agent-start` (the default) lets Herdr launch its canonical executable in its own
# environment, which no supported contract can observe or bind; `explicit-pane-run` has the supervisor submit one
# fully specified command through `pane run`. Mirrored (and pinned equal by a test) in herdr_core.
CODEX_LAUNCH_METHODS = ("agent-start", "explicit-pane-run")
EXPLICIT_LAUNCH_METHOD = "explicit-pane-run"
STRICT_INPUT_POLICY = "require-empty-proof"
GUARDED_INPUT_POLICY = "guarded-new-pane"
CODEX_INPUT_POLICIES = (STRICT_INPUT_POLICY, GUARDED_INPUT_POLICY)
GUARDED_WINDOW_SECONDS = 30.0
# Said wherever the guarded profile is reported: an explicitly selected operating assumption, never a proof.
GUARDED_ASSUMPTION_NOTE = ("guarded-new-pane relies on a trusted interactive-shell startup and on the operator/automation not touching the new pane "
                           f"(no focus, typing, pasting, keys, rename, restart) from its creation until launch verification; it is NOT a proof that the "
                           f"command line is empty, a {int(GUARDED_WINDOW_SECONDS)}-second window is a conservative bound, and a check-to-input race remains")
LAUNCH_RECIPE_VERSION = 1
# A DIRECT recipe launches a NEW Codex TUI (`codex [--model M] [approval args] --no-daemon`) with no pre-created thread: it carries `form` and `model` instead of `thread_id`.
# A recipe without `form` is the legacy thread-resume recipe and is validated exactly as before (journaled legacy preparations stay readable).
DIRECT_FORM = "direct"
# Optional closed approval mode of the explicit Codex launch (absent = `inherit`: the recipe and its digest are exactly today's). A non-inherit mode maps to one
# fixed argument placed between the thread id and `--no-daemon`; it is part of the recipe body and digest, and callers bind it to the configured mode.
# Mirrored by herdr_core.APPROVAL_MODES / APPROVAL_MODE_ARGS (a test pins them equal).
APPROVAL_MODES = {"codex": ("inherit", "auto-review"), "claude": ("inherit", "auto")}
APPROVAL_MODE_ARGS = {("codex", "auto-review"): ("--approve-for-me",), ("claude", "auto"): ("--permission-mode", "auto")}
INHERIT_MODE = "inherit"
ENV_UTILITY_CANDIDATES = ("/usr/bin/env", "/bin/env")
_PLACEHOLDER_THREAD = "00000000-0000-4000-8000-000000000000"
# Required by the amendment before any command is queued: proof that the pane's command line is empty. Herdr 0.9.5
# offers none (its installed skill says the `pane run` admission check "covers this submission, not an earlier
# incomplete line"), so the capability is reported unmet and the route stays unavailable.
EMPTY_INPUT_CAPABILITY = "pane empty interactive input proof"
NO_EMPTY_INPUT_CONTRACT = ("no supported Herdr command reports that a shell pane's command line is empty or at a prompt; a sole foreground shell process does not "
                           "establish that, and the installed skill says the pane run admission check does not cover an earlier incomplete line")
NO_PANE_LAUNCH_CONTRACT = ("the installed Herdr offers no supported way to read or bind the executable and CODEX_HOME that `agent start --kind codex` launches "
                           "(the pane inherits the Herdr server's own environment), so the helper context cannot be proven to match the pane; Fresh Codex stays unavailable "
                           "until Herdr exposes such a contract or an approved design supplies one")


class ContextError(RuntimeError):
    pass


def codex_mode_args(mode: Any) -> list[str]:
    """The fixed arguments of a Codex approval mode; anything outside the closed set is refused."""
    if type(mode) is not str or mode not in APPROVAL_MODES["codex"]:
        raise ContextError(f"unsupported Codex approval mode {mode!r}")
    return [] if mode == INHERIT_MODE else list(APPROVAL_MODE_ARGS[("codex", mode)])


def config_codex_mode(config: Mapping[str, Any]) -> str:
    """The configured Codex approval mode (absent = inherit); a malformed value is refused rather than defaulted."""
    section = config.get("session_start") if isinstance(config, Mapping) else None
    modes = section.get("approval_modes") if isinstance(section, Mapping) else None
    mode = modes.get("codex", INHERIT_MODE) if isinstance(modes, Mapping) else INHERIT_MODE
    codex_mode_args(mode)
    return str(mode)


def _home(home: Path | str | None) -> Path:
    return Path(home) if home is not None else Path.home()


def _clean_path(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or not value.startswith("/") or len(value) > 1024:
        raise ContextError(f"{label} must be an absolute path")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ContextError(f"{label} contains a control character")
    return value


def default_journal_root(home: Path | str | None = None) -> str:
    return os.path.realpath(_home(home) / DEFAULT_JOURNAL_SUBPATH)


def instance_id(journal_root: str) -> str:
    return hashlib.sha256(("herdr-helper-instance-v1\0" + journal_root).encode()).hexdigest()[:12]


def _lookup(which: Callable[..., str | None], name: str, env: Mapping[str, str]) -> str | None:
    """PATH lookup that honours the given environment, not the caller's, when the default resolver is used."""
    return which(name, path=env.get("PATH")) if which is shutil.which else which(name)


def resolve_codex_bin(config: Mapping[str, Any] | None = None, *, environ: Mapping[str, str] | None = None,
                      which: Callable[[str], str | None] = shutil.which, home: Path | str | None = None) -> str:
    """The one Codex executable resolution used by the producer and the worker (same order, same checks).
    Operator-controlled sources only: the pinned environment value, the supervisor config, PATH, ~/.local/bin."""
    env = os.environ if environ is None else environ
    configured = None
    if isinstance(config, Mapping) and isinstance(config.get("codex_reset"), Mapping):
        configured = config["codex_reset"].get("codex_bin")
    candidates = (env.get(CODEX_BIN_ENV), configured, _lookup(which, "codex", env), str(_home(home) / ".local/bin/codex"))
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.startswith("/") and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return os.path.realpath(candidate)
    raise ContextError("Codex executable not found; configure codex_reset.codex_bin")


def resolve_codex_home(environ: Mapping[str, str] | None = None, home: Path | str | None = None) -> str:
    env = os.environ if environ is None else environ
    value = env.get("CODEX_HOME")
    return os.path.realpath(_clean_path(value, "CODEX_HOME") if value else str(_home(home) / ".codex"))


def fingerprint_for_environment(journal_root: Path | str, working_directory: str, environ: Mapping[str, str], home: Path | str) -> str:
    """The helper context a worker started with `environ` would resolve: the single implementation shared by the
    worker (its own environment) and by readiness (the environment the loaded unit would give it)."""
    configured: Any = None
    config_path = Path(environ.get("HERDR_SUPERVISOR_CONFIG") or Path(home) / ".config/herdr-supervisor/config.json")
    try:
        configured = json.loads(config_path.read_text())
    except (OSError, ValueError, TypeError):
        configured = None
    return fingerprint(build_context(journal_root, working_directory, config=configured if isinstance(configured, dict) else None, environ=environ, home=home))


def build_context(journal_root: Path | str, working_directory: str, *, config: Mapping[str, Any] | None = None,
                  environ: Mapping[str, str] | None = None, which: Callable[[str], str | None] = shutil.which,
                  home: Path | str | None = None) -> dict[str, Any]:
    root = os.path.realpath(_clean_path(str(journal_root), "journal root"))
    return {
        "version": CONTEXT_VERSION,
        "instance": instance_id(root),
        "journal_root": root,
        "codex_bin": resolve_codex_bin(config, environ=environ, which=which, home=home),
        "codex_home": resolve_codex_home(environ, home),
        "working_directory": _clean_path(working_directory, "working directory"),
    }


def validate_context(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != set(_FIELDS) or type(value["version"]) is not int or value["version"] != CONTEXT_VERSION:
        raise ContextError("helper context descriptor is malformed")
    for key in ("journal_root", "codex_bin", "codex_home", "working_directory"):
        _clean_path(value[key], key)
    if value["instance"] != instance_id(value["journal_root"]):
        raise ContextError("helper context instance does not match its journal root")
    return value


def fingerprint(context: Mapping[str, Any]) -> str:
    validate_context(dict(context))
    body = json.dumps({key: context[key] for key in _FIELDS}, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256((_FINGERPRINT_DOMAIN + body).encode()).hexdigest()


# ---------------------------------------------------------------- systemd rendering


def _percent(value: str) -> str:
    return value.replace("%", "%%")


def _quote(value: str) -> str:
    return '"' + _percent(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def unit_names(context: Mapping[str, Any], home: Path | str | None = None) -> tuple[str, str, bool]:
    """(path unit, service unit, is_global). The shipped global units serve exactly the default journal."""
    if context["journal_root"] == default_journal_root(home):
        return GLOBAL_PATH_UNIT, GLOBAL_SERVICE_UNIT, True
    stem = f"herdr-codex-reset-worker-{context['instance']}"
    return f"{stem}.path", f"{stem}.service", False


def render_units(context: Mapping[str, Any], home: Path | str | None = None) -> dict[str, str]:
    """Text of the per-project inactive path/service pair. Pure: writes nothing, enables nothing. The write
    scope is exactly the CODEX_HOME state and this project's journal root, never the whole home directory."""
    context = validate_context(dict(context))
    path_unit, service_unit, is_global = unit_names(context, home)
    if is_global:
        raise ContextError("the default journal is served by the shipped global units; render only a non-default project")
    worker = str(_home(home) / WORKER_COMMAND_SUBPATH)
    root = context["journal_root"]
    path_text = (
        f"[Unit]\nDescription=Watch for Herdr Codex helper requests (project {context['instance']})\n"
        f"[Path]\nDirectoryNotEmpty={_percent(root)}/pending\nDirectoryMode=0700\nUnit={service_unit}\n"
        "[Install]\nWantedBy=default.target\n"
    )
    service_text = (
        f"[Unit]\nDescription=Process one supported Codex helper request (project {context['instance']})\n"
        "[Service]\nType=oneshot\nSuccessExitStatus=0 3\nUMask=0077\n"
        f"ExecStart={_quote(worker)} --once --state-dir {_quote(root)}\n"
        f"Environment={_quote('CODEX_HOME=' + context['codex_home'])}\n"
        f"Environment={_quote(CODEX_BIN_ENV + '=' + context['codex_bin'])}\n"
        "NoNewPrivileges=yes\nPrivateTmp=yes\nProtectSystem=strict\nProtectHome=read-only\n"
        f"ReadWritePaths={_quote(context['codex_home'])} {_quote(root)}\n"
        "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6\nRestrictSUIDSGID=yes\nLockPersonality=yes\nMemoryDenyWriteExecute=yes\n"
    )
    return {path_unit: path_text, service_unit: service_text}


def parse_unit(text: str) -> dict[str, list[tuple[str, str]]]:
    """Minimal read-only systemd unit reader: raw values (specifiers are expanded per word by `_expand`)."""
    sections: dict[str, list[tuple[str, str]]] = {}
    current = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1]
            sections.setdefault(current, [])
        elif current is not None and "=" in line:
            key, value = line.split("=", 1)
            sections[current].append((key.strip(), value.strip()))
    return sections


def _expand(value: str, home: Path | str | None) -> str:
    """`%%` and `%h` only (what the shipped and rendered units use); applied AFTER word splitting."""
    return value.replace("%%", "\0").replace("%h", str(_home(home))).replace("\0", "%")


def _words(value: str) -> list[str]:
    words: list[str] = []
    current: list[str] = []
    quoted = False
    index = 0
    started = False
    while index < len(value):
        char = value[index]
        if char == "\\" and index + 1 < len(value):
            current.append(value[index + 1])
            index += 2
            started = True
            continue
        if char == '"':
            quoted = not quoted
            started = True
        elif char == " " and not quoted:
            if started:
                words.append("".join(current))
                current, started = [], False
        else:
            current.append(char)
            started = True
        index += 1
    if started:
        words.append("".join(current))
    return words


def _values(sections: dict[str, list[tuple[str, str]]], section: str, key: str) -> list[str]:
    return [value for name, value in sections.get(section, []) if name == key]


def inspect_units(context: Mapping[str, Any], unit_dir: Path, home: Path | str | None = None) -> list[str]:
    """Structural agreement between the installed units and the descriptor; returns actionable problems."""
    problems: list[str] = []
    path_unit, service_unit, is_global = unit_names(context, home)
    home_dir = os.path.realpath(_home(home))
    texts: dict[str, dict[str, list[tuple[str, str]]]] = {}
    for name in (path_unit, service_unit):
        try:
            texts[name] = parse_unit((Path(unit_dir) / name).read_text())
        except OSError:
            problems.append(f"helper unit {name} is not installed in {unit_dir}; render it with `herdr-supervisor helper-context --render-dir DIR` and install it")
    if problems:
        return problems
    watched = [_expand(value, home) for value in _values(texts[path_unit], "Path", "DirectoryNotEmpty")]
    expected_pending = f"{context['journal_root']}/pending"
    if len(watched) != 1 or os.path.realpath(watched[0]) != expected_pending:
        problems.append(f"{path_unit} watches {watched[0] if watched else 'nothing'} instead of {expected_pending}")
    if _values(texts[path_unit], "Path", "Unit") != [service_unit]:
        problems.append(f"{path_unit} does not start {service_unit}")
    execs = _values(texts[service_unit], "Service", "ExecStart")
    words = [_expand(word, home) for word in _words(execs[0])] if len(execs) == 1 else []
    if not words or "--once" not in words:
        problems.append(f"{service_unit} does not run the worker with --once")
    else:
        state_dirs = [words[i + 1] for i, word in enumerate(words[:-1]) if word == "--state-dir"]
        if is_global:
            if state_dirs and os.path.realpath(state_dirs[0]) != context["journal_root"]:
                problems.append(f"{service_unit} --state-dir {state_dirs[0]} is not the journal root {context['journal_root']}")
        elif len(state_dirs) != 1 or os.path.realpath(state_dirs[0]) != context["journal_root"]:
            problems.append(f"{service_unit} must pass --state-dir {context['journal_root']} explicitly")
    writable = [os.path.realpath(word) for value in _values(texts[service_unit], "Service", "ReadWritePaths") for word in (_expand(w, home) for w in _words(value)) if word.startswith("/")]
    for label, target in (("journal root", context["journal_root"]), ("CODEX_HOME", context["codex_home"])):
        if not any(target == entry or target.startswith(entry.rstrip("/") + "/") for entry in writable):
            problems.append(f"{service_unit} ReadWritePaths does not cover the {label} {target}")
    if any(entry == "/" or home_dir == entry or home_dir.startswith(entry.rstrip("/") + "/") for entry in writable):
        problems.append(f"{service_unit} ReadWritePaths is broader than the journal and CODEX_HOME (whole home or root)")
    environment: dict[str, str] = {}
    for value in _values(texts[service_unit], "Service", "Environment"):
        for word in (_expand(w, home) for w in _words(value)):
            if "=" in word:
                name, item = word.split("=", 1)
                environment[name] = item
    unit_home = os.path.realpath(environment["CODEX_HOME"]) if "CODEX_HOME" in environment else os.path.realpath(_home(home) / ".codex")
    if unit_home != context["codex_home"]:
        problems.append(f"{service_unit} would use CODEX_HOME {unit_home} instead of {context['codex_home']}")
    if CODEX_BIN_ENV in environment and os.path.realpath(environment[CODEX_BIN_ENV]) != context["codex_bin"]:
        problems.append(f"{service_unit} pins Codex executable {environment[CODEX_BIN_ENV]} instead of {context['codex_bin']}")
    if not is_global and CODEX_BIN_ENV not in environment:
        problems.append(f"{service_unit} does not pin the Codex executable ({CODEX_BIN_ENV})")
    return problems


# ---------------------------------------------------------------- explicit launch recipe


def is_native_elf(path: str) -> bool:
    """Production executable check: a directly executable native ELF binary, never a script/shebang launcher."""
    try:
        with open(path, "rb") as handle:
            return handle.read(4) == b"\x7fELF"
    except OSError:
        return False


def env_supports_chdir(env_path: str, runner: Callable[..., Any] = subprocess.run) -> bool:
    """Read-only capability probe of `env --help` (it executes nothing else)."""
    try:
        done = runner([env_path, "--help"], capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0 and "--chdir" in (done.stdout or "")


def _identity(path: str, *, content: bool) -> dict[str, Any]:
    real = os.path.realpath(path)
    stat = os.stat(real)
    identity: dict[str, Any] = {"path": real, "size": stat.st_size}
    if content:
        digest = hashlib.sha256()
        with open(real, "rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
        identity["sha256"] = digest.hexdigest()
    return identity


def resolve_env_utility(*, candidates: tuple[str, ...] = ENV_UTILITY_CANDIDATES) -> str | None:
    for candidate in candidates:
        if Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return os.path.realpath(candidate)
    return None


def _recipe_digest(recipe: Mapping[str, Any]) -> str:
    body = {key: recipe[key] for key in recipe if key != "digest"}
    return hashlib.sha256(("herdr-launch-recipe-v1\0" + json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)).encode()).hexdigest()


def build_launch_recipe(context: Mapping[str, Any], thread_id: str, *, content: bool = True, env_utility: str | None = None,
                        executable_check: Callable[[str], bool] = is_native_elf, chdir_probe: Callable[[str], bool] = env_supports_chdir,
                        approval_mode: str = INHERIT_MODE) -> dict[str, Any]:
    """The one deterministic Codex launch recipe shared by readiness and execution.

    Everything comes from the validated helper context (executable, CODEX_HOME, working directory) and the exact
    helper-created thread UUID; nothing from Telegram or task metadata. The command is
    `env --chdir CWD CODEX_HOME=HOME CODEX resume THREAD --no-daemon` quoted with `shlex.join` (no eval, no shell
    exports). It SPECIFIES the context by command; it does not observe Herdr's own environment, and Herdr
    integration variables are inherited (env without -i). Scripts/shebang launchers and anything not directly
    executable fail unavailable. `executable_check` is a test seam; production always uses the native-ELF check."""
    validate_context(dict(context))
    mode_args = codex_mode_args(approval_mode)
    try:
        if str(uuid.UUID(thread_id)) != thread_id:
            raise ValueError
    except (ValueError, AttributeError, TypeError) as error:
        raise ContextError("launch recipe needs the canonical helper-created thread UUID") from error
    env_path = env_utility or resolve_env_utility()
    if env_path is None or not os.access(env_path, os.X_OK):
        raise ContextError("no absolute env utility is available for the explicit launch recipe")
    if not chdir_probe(env_path):
        raise ContextError(f"{env_path} does not support --chdir; the explicit launch recipe is unavailable")
    codex = context["codex_bin"]
    if not os.path.isfile(codex) or not os.access(codex, os.X_OK):
        raise ContextError(f"the pinned Codex executable {codex} is missing or not executable")
    if not executable_check(codex):
        raise ContextError(f"{codex} is not a directly executable native binary (scripts and shebang launchers are not supported by the explicit launch recipe)")
    argv = [os.path.realpath(env_path), "--chdir", context["working_directory"], f"CODEX_HOME={context['codex_home']}", codex, "resume", thread_id, *mode_args, "--no-daemon"]
    recipe: dict[str, Any] = {
        "version": LAUNCH_RECIPE_VERSION, "method": EXPLICIT_LAUNCH_METHOD, "basis": "context-specified-by-command",
        "env": _identity(env_path, content=content), "codex": _identity(codex, content=content),
        "cwd": context["working_directory"], "codex_home": context["codex_home"], "thread_id": thread_id,
        "argv": argv, "command": shlex.join(argv),
    }
    if approval_mode != INHERIT_MODE:
        recipe["approval_mode"] = approval_mode  # only a non-inherit mode adds a key: an inherit recipe (and its digest) is byte-for-byte the earlier one
    recipe["digest"] = _recipe_digest(recipe)
    return recipe


def _direct_argv(env_path: str, cwd: str, codex_home: str, codex: str, model: str | None, mode_args: list[str]) -> list[str]:
    return [env_path, "--chdir", cwd, f"CODEX_HOME={codex_home}", codex, *([] if model is None else ["--model", model]), *mode_args, "--no-daemon"]


def valid_model(model: Any) -> bool:
    return model is None or (isinstance(model, str) and 0 < len(model) <= 128 and model.isprintable() and not model.startswith("-") and " " not in model)


def build_direct_launch_recipe(context: Mapping[str, Any], model: str | None, *, content: bool = True, env_utility: str | None = None,
                               executable_check: Callable[[str], bool] = is_native_elf, chdir_probe: Callable[[str], bool] = env_supports_chdir,
                               approval_mode: str = INHERIT_MODE) -> dict[str, Any]:
    """The deterministic recipe of a NEW Codex TUI started directly in a fresh pane: `env --chdir CWD CODEX_HOME=HOME CODEX [--model M] [approval args] --no-daemon`.
    Same context, executable, env-utility and quoting rules as `build_launch_recipe`, but no pre-created thread and no `resume` argument: the native session is
    created by this process and its identity is learned only from the owned pane."""
    validate_context(dict(context))
    mode_args = codex_mode_args(approval_mode)
    if not valid_model(model):
        raise ContextError("direct launch recipe model is invalid")
    env_path = env_utility or resolve_env_utility()
    if env_path is None or not os.access(env_path, os.X_OK):
        raise ContextError("no absolute env utility is available for the explicit launch recipe")
    if not chdir_probe(env_path):
        raise ContextError(f"{env_path} does not support --chdir; the explicit launch recipe is unavailable")
    codex = context["codex_bin"]
    if not os.path.isfile(codex) or not os.access(codex, os.X_OK):
        raise ContextError(f"the pinned Codex executable {codex} is missing or not executable")
    if not executable_check(codex):
        raise ContextError(f"{codex} is not a directly executable native binary (scripts and shebang launchers are not supported by the explicit launch recipe)")
    argv = _direct_argv(os.path.realpath(env_path), context["working_directory"], context["codex_home"], codex, model, mode_args)
    recipe: dict[str, Any] = {
        "version": LAUNCH_RECIPE_VERSION, "method": EXPLICIT_LAUNCH_METHOD, "basis": "context-specified-by-command", "form": DIRECT_FORM, "model": model,
        "env": _identity(env_path, content=content), "codex": _identity(codex, content=content),
        "cwd": context["working_directory"], "codex_home": context["codex_home"],
        "argv": argv, "command": shlex.join(argv),
    }
    if approval_mode != INHERIT_MODE:
        recipe["approval_mode"] = approval_mode
    recipe["digest"] = _recipe_digest(recipe)
    return recipe


def _validate_direct_recipe(value: dict[str, Any]) -> dict[str, Any]:
    keys = {"version", "method", "basis", "form", "model", "env", "codex", "cwd", "codex_home", "argv", "command", "digest"}
    if set(value) - {"approval_mode"} != keys or type(value["version"]) is not int or value["version"] != LAUNCH_RECIPE_VERSION or value["form"] != DIRECT_FORM:
        raise ContextError("launch recipe is malformed")
    mode = value.get("approval_mode", INHERIT_MODE)
    if ("approval_mode" in value and mode == INHERIT_MODE) or type(mode) is not str:
        raise ContextError("launch recipe approval mode is malformed")
    mode_args = codex_mode_args(mode)
    if value["method"] != EXPLICIT_LAUNCH_METHOD or value["basis"] != "context-specified-by-command" or not valid_model(value["model"]):
        raise ContextError("launch recipe method is invalid")
    for name in ("env", "codex"):
        ident = value[name]
        if not isinstance(ident, dict) or not {"path", "size"} <= set(ident) or set(ident) - {"path", "size", "sha256"} or type(ident["size"]) is not int:
            raise ContextError("launch recipe executable identity is malformed")
        _clean_path(ident["path"], f"{name} path")
        if "sha256" in ident and (not isinstance(ident["sha256"], str) or len(ident["sha256"]) != 64):
            raise ContextError("launch recipe executable digest is malformed")
    for name in ("cwd", "codex_home"):
        _clean_path(value[name], name)
    expected = _direct_argv(value["env"]["path"], value["cwd"], value["codex_home"], value["codex"]["path"], value["model"], mode_args)
    if value["argv"] != expected or value["command"] != shlex.join(expected) or value["digest"] != _recipe_digest(value):
        raise ContextError("launch recipe command does not match its parts")
    return value


def validate_launch_recipe(value: Any) -> dict[str, Any]:
    """Strict shape/consistency check of a journaled recipe (types, argv structure, command, digest)."""
    if isinstance(value, dict) and "form" in value:
        return _validate_direct_recipe(value)
    keys = {"version", "method", "basis", "env", "codex", "cwd", "codex_home", "thread_id", "argv", "command", "digest"}
    if not isinstance(value, dict) or set(value) - {"approval_mode"} != keys or type(value["version"]) is not int or value["version"] != LAUNCH_RECIPE_VERSION:
        raise ContextError("launch recipe is malformed")
    mode = value.get("approval_mode", INHERIT_MODE)
    if ("approval_mode" in value and mode == INHERIT_MODE) or type(mode) is not str:
        raise ContextError("launch recipe approval mode is malformed")  # inherit is represented by the absence of the key, never by a value
    mode_args = codex_mode_args(mode)
    if value["method"] != EXPLICIT_LAUNCH_METHOD or value["basis"] != "context-specified-by-command":
        raise ContextError("launch recipe method is invalid")
    for name in ("env", "codex"):
        ident = value[name]
        if not isinstance(ident, dict) or not {"path", "size"} <= set(ident) or set(ident) - {"path", "size", "sha256"} or type(ident["size"]) is not int:
            raise ContextError("launch recipe executable identity is malformed")
        _clean_path(ident["path"], f"{name} path")
        if "sha256" in ident and (not isinstance(ident["sha256"], str) or len(ident["sha256"]) != 64):
            raise ContextError("launch recipe executable digest is malformed")
    for name in ("cwd", "codex_home"):
        _clean_path(value[name], name)
    try:
        if str(uuid.UUID(value["thread_id"])) != value["thread_id"]:
            raise ValueError
    except (ValueError, AttributeError, TypeError) as error:
        raise ContextError("launch recipe thread id is invalid") from error
    expected = [value["env"]["path"], "--chdir", value["cwd"], f"CODEX_HOME={value['codex_home']}", value["codex"]["path"], "resume", value["thread_id"], *mode_args, "--no-daemon"]
    if value["argv"] != expected or value["command"] != shlex.join(expected) or value["digest"] != _recipe_digest(value):
        raise ContextError("launch recipe command does not match its parts")
    return value


def recipe_authority_problem(recipe: Mapping[str, Any], context: Mapping[str, Any], thread_id: str | None, env_utility: str | None, approval_mode: str = INHERIT_MODE,
                             model: str | None = None) -> str | None:
    """A journaled recipe is untrusted metadata: a recomputed digest proves internal consistency, not origin. Every
    execution-affecting field must equal what the AUTHORITATIVE inputs permit — the unchanged helper context, the exact
    recorded thread (legacy form) or the recorded model (direct form, `thread_id` None) and the supported env resolver — before any executable named by the journal is probed or run."""
    try:
        validate_launch_recipe(dict(recipe))
        validate_context(dict(context))
        authorized_args = codex_mode_args(approval_mode)
    except ContextError as error:
        return f"the journaled launch recipe is malformed: {error}"
    if env_utility is None:
        return "no supported env utility is available to authorize the journaled launch recipe"
    direct = thread_id is None
    if direct != (recipe.get("form") == DIRECT_FORM):
        return "the journaled launch recipe is not of the form this preparation recorded (thread resume versus direct launch); it was not probed or run"
    if direct:
        expected = _direct_argv(os.path.realpath(env_utility), context["working_directory"], context["codex_home"], context["codex_bin"], model, authorized_args)
        identity_checks = (("model", recipe["model"] == model),)
    else:
        expected = [os.path.realpath(env_utility), "--chdir", context["working_directory"], f"CODEX_HOME={context['codex_home']}", context["codex_bin"], "resume", str(thread_id), *authorized_args, "--no-daemon"]
        identity_checks = (("thread", recipe["thread_id"] == thread_id),)
    differing = [name for name, ok in (("approval mode", recipe.get("approval_mode", INHERIT_MODE) == approval_mode), *identity_checks, ("working directory", recipe["cwd"] == context["working_directory"]),
                                       ("CODEX_HOME", recipe["codex_home"] == context["codex_home"]), ("Codex executable path", recipe["codex"]["path"] == context["codex_bin"]),
                                       ("env utility path", recipe["env"]["path"] == os.path.realpath(env_utility)), ("command", recipe["argv"] == expected)) if not ok]
    return ("the journaled launch recipe differs from the authorized helper context / " + ("model" if direct else "thread") + " / env utility (" + ", ".join(differing) + "); it was not probed or run") if differing else None


def revalidate_launch_recipe(recipe: Mapping[str, Any], *, context: Mapping[str, Any], thread_id: str | None, env_utility: str | None,
                             executable_check: Callable[[str], bool] = is_native_elf, chdir_probe: Callable[[str], bool] = env_supports_chdir,
                             approval_mode: str = INHERIT_MODE, model: str | None = None) -> str | None:
    """Drift check immediately before submission. AUTHORITY first (no I/O on journal-supplied paths), then the original
    recorded byte identities: drift is refused, never reauthorized by regenerating the recipe."""
    problem = recipe_authority_problem(recipe, context, thread_id, env_utility, approval_mode, model)
    if problem:
        return problem
    try:
        for name in ("env", "codex"):
            now = _identity(recipe[name]["path"], content=True)
            if now != recipe[name]:
                return f"the {name} executable {recipe[name]['path']} changed since the launch recipe was journaled"
        if not os.access(recipe["codex"]["path"], os.X_OK) or not executable_check(recipe["codex"]["path"]):
            return f"{recipe['codex']['path']} is no longer a directly executable native binary"
        if not chdir_probe(recipe["env"]["path"]):
            return f"{recipe['env']['path']} no longer supports --chdir"
    except (ContextError, OSError) as error:
        return f"the journaled launch recipe cannot be revalidated: {error}"
    return None


def explicit_launch_problem(context: Mapping[str, Any], capabilities: Mapping[str, Any] | Callable[[], Any] | None, *,
                            executable_check: Callable[[str], bool] = is_native_elf, chdir_probe: Callable[[str], bool] = env_supports_chdir,
                            env_utility: str | None = None, input_policy: str = STRICT_INPUT_POLICY, approval_mode: str = INHERIT_MODE, direct: bool = False) -> str | None:
    """Readiness of the explicit launch method: the same recipe constructor (cheap identities, placeholder thread)
    plus the Herdr capabilities it needs (`pane run`, `pane process-info`, `agent rename`). Inherited `agent start`
    context stays unverified; this reports only that the command-specified method is eligible."""
    try:
        if direct:
            build_direct_launch_recipe(context, None, content=False, env_utility=env_utility, executable_check=executable_check, chdir_probe=chdir_probe, approval_mode=approval_mode)
        else:
            build_launch_recipe(context, _PLACEHOLDER_THREAD, content=False, env_utility=env_utility, executable_check=executable_check, chdir_probe=chdir_probe, approval_mode=approval_mode)
    except (ContextError, OSError) as error:
        return f"the explicit Codex launch recipe is unavailable: {error}"
    report = capabilities() if callable(capabilities) else capabilities
    if input_policy not in CODEX_INPUT_POLICIES:
        return f"unknown Codex input policy {input_policy!r}"
    if input_policy == GUARDED_INPUT_POLICY and isinstance(report, Mapping):
        # The real empty-input capability stays False/unknown; guarded mode does not fake it, it simply does not require it.
        report = {name: ok for name, ok in report.items() if name != EMPTY_INPUT_CAPABILITY}
    if not isinstance(report, Mapping) or not report or not all(value is True for value in report.values()):
        missing = [name for name, ok in (report or {}).items() if ok is not True] if isinstance(report, Mapping) and report else ["capability probe"]
        note = ""
        if EMPTY_INPUT_CAPABILITY in missing:
            note = (f" ({EMPTY_INPUT_CAPABILITY}: {NO_EMPTY_INPUT_CONTRACT})")
        return "the explicit Codex launch method needs Herdr capabilities that are not verified: " + ", ".join(missing) + note
    return None


# ---------------------------------------------------------------- loaded-unit evidence and readiness

_PATH_PROPS = ("LoadState", "FragmentPath", "DropInPaths", "NeedDaemonReload", "ActiveState", "SubState", "UnitFileState", "Paths", "Unit")
_SERVICE_PROPS = ("LoadState", "FragmentPath", "DropInPaths", "NeedDaemonReload", "ActiveState", "Type", "ExecStart", "Environment",
                  "ReadWritePaths", "ProtectSystem", "ProtectHome", "NoNewPrivileges")
_EXEC_RE = re.compile(r"^\{ path=(?P<path>.*?) ; argv\[\]=(?P<argv>.*?) ; ignore_errors=")


def systemctl_user(args: list[str], runner: Callable[..., Any] = subprocess.run) -> str | None:
    """Read-only `systemctl --user` query; None when the user manager cannot be queried."""
    try:
        completed = runner(["systemctl", "--user", *args], capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return completed.stdout if completed.returncode == 0 or completed.stdout else None


def unit_properties(unit: str, props: tuple[str, ...], systemctl: Callable[[list[str]], str | None]) -> dict[str, str] | None:
    """The manager's loaded properties of one unit (what systemd will actually run), or None if unavailable."""
    output = systemctl(["show", unit, *(arg for prop in props for arg in ("-p", prop))])
    if output is None:
        return None
    values = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
    return values if all(prop in values for prop in props) else None


def manager_environment(systemctl: Callable[[list[str]], str | None]) -> dict[str, str] | None:
    """The user manager's environment (`systemctl --user show-environment`); quoted/complex values are not guessed."""
    output = systemctl(["show-environment"])
    if output is None:
        return None
    environment: dict[str, str] = {}
    for line in output.splitlines():
        if "=" not in line:
            continue
        name, value = line.split("=", 1)
        if value.startswith("$'"):
            continue  # shell-escaped value: not interpreted; a variable we need being absent fails closed below
        environment[name] = value
    return environment


def inspect_loaded_units(context: Mapping[str, Any], unit_dir: Path, home: Path | str | None, *, systemctl: Callable[[list[str]], str | None]) -> tuple[list[str], dict[str, Any]]:
    """Reconcile what systemd has LOADED (fragment, drop-ins, ExecStart, environment, sandbox, write scope, activation)
    with the descriptor. Disk fragments are not trusted on their own; any drop-in, pending reload, query failure or
    deviation is a reason. Returns (problems, activation facts)."""
    home_path, home_literal = os.path.realpath(_home(home)), str(_home(home))
    path_unit, service_unit, is_global = unit_names(context, home)
    problems: list[str] = []
    facts: dict[str, Any] = {"unit_file_state": None, "active_state": None, "sub_state": None}
    path_props = unit_properties(path_unit, _PATH_PROPS, systemctl)
    service_props = unit_properties(service_unit, _SERVICE_PROPS, systemctl)
    manager_env = manager_environment(systemctl)
    if path_props is None or service_props is None or manager_env is None:
        missing = [name for name, value in ((path_unit, path_props), (service_unit, service_props), ("the user manager environment", manager_env)) if value is None]
        return [f"loaded configuration of {', '.join(missing)} could not be verified (systemctl --user is unavailable or incomplete here); run `systemctl --user status {path_unit}` in a login shell"], facts
    facts.update(unit_file_state=path_props["UnitFileState"], active_state=path_props["ActiveState"], sub_state=path_props["SubState"])
    root, root_literal = context["journal_root"], str(_home(home) / DEFAULT_JOURNAL_SUBPATH) if is_global else context["journal_root"]
    for name, props in ((path_unit, path_props), (service_unit, service_props)):
        if props["LoadState"] != "loaded":
            problems.append(f"{name} is not loaded by the user manager (LoadState={props['LoadState']})")
        if os.path.realpath(props["FragmentPath"]) != os.path.realpath(Path(unit_dir) / name):
            problems.append(f"{name} is loaded from {props['FragmentPath'] or 'nowhere'} instead of {Path(unit_dir) / name}")
        if props["DropInPaths"].strip():
            problems.append(f"{name} has unit overrides ({props['DropInPaths']}); drop-ins can change the worker, environment or write scope and are not supported for this helper")
        if props["NeedDaemonReload"] != "no":
            problems.append(f"{name} changed on disk after it was loaded; run `systemctl --user daemon-reload` after reviewing it")
    if path_props["UnitFileState"] != "enabled" or path_props["ActiveState"] != "active":
        problems.append(f"{path_unit} is {path_props['UnitFileState']}/{path_props['ActiveState']}; enable it explicitly (`systemctl --user enable --now {path_unit}`) after reviewing the unit")
    if path_props["Unit"] != service_unit:
        problems.append(f"{path_unit} starts {path_props['Unit']} instead of {service_unit}")
    if path_props["Paths"] not in {f"{root}/pending (DirectoryNotEmpty)", f"{root_literal}/pending (DirectoryNotEmpty)"}:
        problems.append(f"{path_unit} watches {path_props['Paths'] or 'nothing'} instead of {root}/pending (DirectoryNotEmpty)")
    if service_props["ActiveState"] == "failed":
        problems.append(f"{service_unit} is failed; inspect its journal before resetting it")
    worker = str(_home(home) / WORKER_COMMAND_SUBPATH)
    match = _EXEC_RE.match(service_props["ExecStart"])
    expected_argv = {f"{worker} --once"} if is_global else set()
    expected_argv |= {f"{worker} --once --state-dir {candidate}" for candidate in (root, root_literal)}
    if match is None or os.path.realpath(match.group("path")) != os.path.realpath(worker) or match.group("argv") not in expected_argv:
        problems.append(f"{service_unit} would run {match.group('argv') if match else service_props['ExecStart'] or 'nothing'} instead of the supported worker `{worker} --once" + ("" if is_global else f" --state-dir {root}") + "`")
    if not is_global and match is not None and "--state-dir" not in match.group("argv"):
        problems.append(f"{service_unit} must pass --state-dir {root} explicitly")
    # Sandbox and write scope: exactly the CODEX_HOME state and this project's journal, nothing broader.
    for key, expected in (("Type", "oneshot"), ("ProtectSystem", "strict"), ("ProtectHome", "read-only"), ("NoNewPrivileges", "yes")):
        if service_props[key] != expected:
            problems.append(f"{service_unit} {key}={service_props[key] or 'unset'} instead of {expected}")
    codex_home_forms = {context["codex_home"], str(_home(home) / ".codex")} if context["codex_home"] == os.path.realpath(_home(home) / ".codex") else {context["codex_home"]}
    writable = {f"{a} {b}" for a in codex_home_forms for b in {root, root_literal}}
    if service_props["ReadWritePaths"] not in writable:
        problems.append(f"{service_unit} write scope is `{service_props['ReadWritePaths'] or 'none'}` instead of exactly `{context['codex_home']} {root}`")
    # The environment the worker would really have, and the context it would therefore resolve.
    pinned = f"CODEX_HOME={context['codex_home']} {CODEX_BIN_ENV}={context['codex_bin']}"
    unit_env = service_props["Environment"]
    if is_global:
        if unit_env.strip():
            problems.append(f"{service_unit} sets environment `{unit_env}`; the shipped global unit sets none")
        effective = dict(manager_env)
    elif unit_env != pinned:
        problems.append(f"{service_unit} environment is `{unit_env or 'empty'}` instead of the pinned `{pinned}`")
        effective = dict(manager_env)
    else:
        effective = {**manager_env, "CODEX_HOME": context["codex_home"], CODEX_BIN_ENV: context["codex_bin"]}
    if os.path.realpath(effective.get("HOME", "")) != home_path:
        problems.append(f"the user manager's HOME is {effective.get('HOME') or 'unset'} but the worker launcher runs from {home_literal}")
    else:
        try:
            worker_view = fingerprint_for_environment(root, context["working_directory"], effective, home_literal)
        except ContextError as error:
            problems.append(f"the worker would not resolve a helper context: {error}")
        else:
            if worker_view != fingerprint(context):
                problems.append(f"the worker would resolve helper context {worker_view[:12]} but this producer binds {fingerprint(context)[:12]} (Codex executable, CODEX_HOME or configuration differ)")
    return problems, facts


def inspect_worker_runtime(home: Path | str | None) -> list[str]:
    """The supported launcher and the installed library it executes (read-only file facts)."""
    problems: list[str] = []
    worker = _home(home) / WORKER_COMMAND_SUBPATH
    try:
        launcher_ok = worker.is_file() and os.access(worker, os.X_OK) and hashlib.sha256(worker.read_bytes()).hexdigest() == WORKER_LAUNCHER_SHA256
    except OSError:
        launcher_ok = False
    if not launcher_ok:
        problems.append(f"{worker} is missing, not executable, or not the supported worker launcher; reinstall the reviewed version")
    library = _home(home) / ".local/lib/herdr-supervisor"
    try:
        module_text = (library / "herdr_codex_reset.py").read_text()
    except OSError:
        module_text = ""
    if WORKER_PROTOCOL_MARKER not in module_text.splitlines() or not (library / "herdr_helper_context.py").is_file():
        problems.append(f"the installed worker library in {library} does not support context-bound requests (needs herdr_codex_reset.py with {WORKER_PROTOCOL_MARKER} and herdr_helper_context.py)")
    return problems


def pane_launch_problem(context: Mapping[str, Any], launch_context: Callable[[], Any] | None) -> str | None:
    """Binding of the pane launch context. The supervisor's own PATH/HOME is NOT evidence of what Herdr launches:
    only a supported adapter contract that reports the launched executable and CODEX_HOME counts."""
    if not callable(launch_context):
        return NO_PANE_LAUNCH_CONTRACT
    try:
        observed = launch_context()
    except Exception as error:  # noqa: BLE001 - any adapter failure is missing evidence
        return f"the Herdr launch-context contract failed ({type(error).__name__}); Fresh Codex stays unavailable"
    if observed is None:
        return NO_PANE_LAUNCH_CONTRACT
    if not isinstance(observed, dict) or set(observed) != {"executable", "codex_home"} or not all(isinstance(observed[key], str) and observed[key].startswith("/") for key in observed):
        return "the Herdr launch-context contract returned an unusable value; Fresh Codex stays unavailable"
    if os.path.realpath(observed["executable"]) != context["codex_bin"] or os.path.realpath(observed["codex_home"]) != context["codex_home"]:
        return (f"Herdr launches Codex {observed['executable']} with CODEX_HOME {observed['codex_home']} but the helper binds {context['codex_bin']} / {context['codex_home']}; "
                "the thread would be created under another executable or account")
    return None


def helper_readiness(journal_root: Path | str, config: Mapping[str, Any], *, environ: Mapping[str, str] | None = None, home: Path | str | None = None,
                     unit_dir: Path | str | None = None, systemctl: Callable[[list[str]], str | None] = systemctl_user,
                     which: Callable[..., str | None] = shutil.which, launch_context: Callable[[], Any] | None = None,
                     launch_method: str = "agent-start", launch_capabilities: Mapping[str, Any] | Callable[[], Any] | None = None,
                     input_policy: str = STRICT_INPUT_POLICY,
                     executable_check: Callable[[str], bool] = is_native_elf, chdir_probe: Callable[[str], bool] = env_supports_chdir) -> dict[str, Any]:
    """The one read-only readiness report shared by doctor, the Telegram availability predicate and execution.

    `ready` needs: the producer descriptor resolves; journal directories exist and are writable; the supported worker
    launcher and installed library are present; systemd's LOADED unit configuration (no drop-ins, no pending reload,
    exact ExecStart, environment, sandbox and write scope, active enabled watcher) reproduces the same helper context
    the producer binds; and the selected launch method is eligible: `agent-start` needs a supported contract that
    binds the pane launch context (none exists in Herdr 0.9.5); `explicit-pane-run` needs the command-specified recipe
    and the Herdr capabilities it uses (this specifies the context by command; it does not observe Herdr's own). An inactive oneshot service is normal.
    All of this is structural, read-only evidence; a real deployment acceptance is a separate, explicitly approved step."""
    report: dict[str, Any] = {"ready": False, "reasons": [], "context": None, "fingerprint": None, "units": None,
                              "activation": "untested", "evidence": "structural-read-only", "checks": {}, "notes": [], "input_policy": None, "empty_input_proof": "unavailable"}
    reasons: list[str] = report["reasons"]
    checks: dict[str, bool] = report["checks"]
    try:
        context = build_context(journal_root, str(config["project_root"]), config=config, environ=environ, which=which, home=home)
    except (ContextError, KeyError, OSError) as error:
        reasons.append(f"helper context cannot be resolved: {error}")
        checks["descriptor_resolved"] = False
        return report
    checks["descriptor_resolved"] = True
    report["context"], report["fingerprint"] = context, fingerprint(context)
    missing = [name for name in ("pending", "processing", "results") if not (Path(context["journal_root"]) / name).is_dir()]
    checks["journal_directories"] = not missing
    if missing:
        reasons.append(f"helper journal {context['journal_root']} lacks {', '.join(missing)}; create it with mode 0700 or rerun the installer")
    elif not os.access(context["journal_root"], os.W_OK | os.X_OK):
        checks["journal_directories"] = False
        reasons.append(f"helper journal {context['journal_root']} is not writable by this process")
    unit_directory = Path(unit_dir) if unit_dir is not None else _home(home) / ".config/systemd/user"
    path_unit, service_unit, _ = unit_names(context, home)
    report["units"] = {"path": path_unit, "service": service_unit, "directory": str(unit_directory)}
    runtime = inspect_worker_runtime(home)
    checks["worker_runtime"] = not runtime
    reasons.extend(runtime)
    fragments = inspect_units(context, unit_directory, home)
    checks["unit_fragments_agree"] = not fragments
    reasons.extend(fragments)
    loaded, facts = inspect_loaded_units(context, unit_directory, home, systemctl=systemctl)
    report["activation"] = facts
    checks["loaded_units_agree"] = not loaded
    reasons.extend(loaded)
    report["launch_method"] = launch_method
    report["input_policy"] = input_policy if launch_method == EXPLICIT_LAUNCH_METHOD else None
    report["empty_input_proof"] = "unavailable"  # the real capability is false/unknown under every policy
    report["notes"] = [GUARDED_ASSUMPTION_NOTE] if (launch_method == EXPLICIT_LAUNCH_METHOD and input_policy == GUARDED_INPUT_POLICY) else []
    if launch_method == EXPLICIT_LAUNCH_METHOD:
        launch: str | None
        try:
            mode = config_codex_mode(config)
        except ContextError as error:
            launch = f"the explicit Codex launch recipe is unavailable: {error}"
        else:
            launch = explicit_launch_problem(context, launch_capabilities, executable_check=executable_check, chdir_probe=chdir_probe, input_policy=input_policy, approval_mode=mode)
        checks["explicit_launch_recipe"] = launch is None
    elif launch_method == "agent-start":
        launch = pane_launch_problem(context, launch_context)
        checks["pane_launch_context"] = launch is None
    else:
        launch = f"unknown Codex launch method {launch_method!r}"
        checks["pane_launch_context"] = False
    if launch is not None:
        reasons.append(launch)
    report["ready"] = all(checks.values()) and not reasons
    return report


def direct_launch_readiness(journal_root: Path | str, config: Mapping[str, Any], *, environ: Mapping[str, str] | None = None, home: Path | str | None = None,
                            which: Callable[..., str | None] = shutil.which, launch_capabilities: Mapping[str, Any] | Callable[[], Any] | None = None,
                            input_policy: str = STRICT_INPUT_POLICY, executable_check: Callable[[str], bool] = is_native_elf,
                            chdir_probe: Callable[[str], bool] = env_supports_chdir) -> dict[str, Any]:
    """Read-only readiness of the DIRECT fresh-Codex launch (no pre-created thread): the helper context (pinned Codex executable, CODEX_HOME, working directory) resolves and
    the explicit recipe plus the Herdr capabilities it uses (`pane run`, `pane process-info`, `agent rename`) are eligible. It deliberately does NOT inspect the helper's
    journal directories or systemd units: this launch creates no helper thread (quota-reset helper readiness stays its own report)."""
    report: dict[str, Any] = {"ready": False, "reasons": [], "context": None, "fingerprint": None, "evidence": "structural-read-only", "checks": {}, "notes": [],
                              "launch_form": DIRECT_FORM, "launch_method": EXPLICIT_LAUNCH_METHOD, "input_policy": input_policy, "empty_input_proof": "unavailable"}
    reasons: list[str] = report["reasons"]
    try:
        context = build_context(journal_root, str(config["project_root"]), config=config, environ=environ, which=which, home=home)
    except (ContextError, KeyError, OSError) as error:
        reasons.append(f"helper context cannot be resolved: {error}")
        report["checks"]["descriptor_resolved"] = False
        return report
    report["checks"]["descriptor_resolved"] = True
    report["context"], report["fingerprint"] = context, fingerprint(context)
    report["notes"] = [GUARDED_ASSUMPTION_NOTE] if input_policy == GUARDED_INPUT_POLICY else []
    try:
        mode = config_codex_mode(config)
    except ContextError as error:
        problem: str | None = f"the explicit Codex launch recipe is unavailable: {error}"
    else:
        problem = explicit_launch_problem(context, launch_capabilities, executable_check=executable_check, chdir_probe=chdir_probe, input_policy=input_policy, approval_mode=mode, direct=True)
    report["checks"]["explicit_launch_recipe"] = problem is None
    if problem is not None:
        reasons.append(problem)
    report["ready"] = all(report["checks"].values()) and not reasons
    return report
