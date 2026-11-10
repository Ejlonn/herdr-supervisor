#!/bin/sh
set -eu

usage() {
    cat <<'USAGE'
Usage: ./install.sh [--dry-run] [--migrate]

Minimal, inactive installation of the user-scoped program files through `herdr-setup`
(src/herdr_setup.py). Existing configuration, credentials, and state are preserved and no
service is loaded, enabled, started, or restarted. A legacy or different installed release
is refused instead of being overwritten: use `herdr-setup update` (see docs/INSTALL.md).
--dry-run previews and writes nothing. --migrate explicitly runs the local state migration
after the files are in place. For the guided full setup run `herdr-setup setup --profile full`.
USAGE
}

DRY_RUN=0
MIGRATE=0
for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=1 ;;
        --migrate) MIGRATE=1 ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; exit 2 ;;
    esac
done

if [ "$(id -u)" -eq 0 ]; then
    echo "Refusing to install as root; run as the target user." >&2
    exit 2
fi

SOURCE_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

# The headless unit runs %h/.local/bin/herdr, so that exact executable is the supported Herdr layout.
# Check it before anything is created so a failed requirement leaves nothing behind.
HERDR_EXE="$HOME/.local/bin/herdr"
if [ ! -x "$HERDR_EXE" ]; then
    echo "Herdr executable not found at $HERDR_EXE; install Herdr user-scoped first (see README requirements)." >&2
    exit 2
fi

if ! command -v python3 >/dev/null 2>&1 || ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
    echo "Python 3.11 or newer is required (python3 was not found or is older)." >&2
    exit 2
fi

SETUP="$SOURCE_DIR/src/herdr_setup.py"
if [ "$DRY_RUN" -eq 1 ]; then
    echo "Found Herdr executable at $HERDR_EXE"
    exec python3 "$SETUP" setup --profile minimal --payload "$SOURCE_DIR"
fi

if [ "$MIGRATE" -eq 1 ]; then
    python3 "$SETUP" setup --profile minimal --payload "$SOURCE_DIR" --apply --approve-current --migrate || exit $?
else
    python3 "$SETUP" setup --profile minimal --payload "$SOURCE_DIR" --apply --approve-current || exit $?
fi

cat <<'EOF2'
Installed inactive user-scoped files. No service was loaded, enabled, started, or restarted.
Next: configure the supervisor, run `herdr-supervisor doctor`, and follow docs/INSTALL.md.
EOF2
echo "Guided full setup (Telegram, quota plugin, optional services; a preview until you apply it):"
echo "  herdr-setup setup --profile full --payload \"$SOURCE_DIR\""
echo "(the installed tool does not search for a source tree: it is told which reviewed tree to use)"
cat <<'EOF2'
Quota plugin: run `herdr-quota-dependency check` (read-only) to see whether the pinned herdr-agent-usage plugin is installed;
this installer never installs, links or configures it. See docs/QUOTA_DEPENDENCY.md.
EOF2
