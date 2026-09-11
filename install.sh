#!/bin/sh
set -eu

usage() {
    cat <<'EOF'
Usage: ./install.sh [--dry-run] [--migrate]

Installs user-scoped files without starting or enabling services. Existing
configuration, credentials, and state are preserved. --migrate explicitly
runs the local state migration after files are staged.
EOF
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
LIB_DIR="$HOME/.local/lib/herdr-supervisor"
BIN_DIR="$HOME/.local/bin"
UNIT_DIR="$HOME/.config/systemd/user"
DOC_DIR="$HOME/.local/share/doc/herdr-supervisor"
SUP_CONFIG_DIR="$HOME/.config/herdr-supervisor"
TG_CONFIG_DIR="$HOME/.config/herdr-telegram"
SUP_STATE_DIR="$HOME/.local/state/herdr-supervisor"
TG_STATE_DIR="$HOME/.local/state/herdr-telegram"
TG_LOCK_DIR="$HOME/.local/state/herdr-telegram-locks"

# The headless unit runs %h/.local/bin/herdr, so that exact executable is the supported Herdr layout.
# Check it before any directory or file is created so a failed requirement leaves nothing behind.
HERDR_EXE="$HOME/.local/bin/herdr"
if [ ! -x "$HERDR_EXE" ]; then
    echo "Herdr executable not found at $HERDR_EXE; install Herdr user-scoped first (see README requirements)." >&2
    exit 2
fi

if [ "$DRY_RUN" -eq 1 ]; then
    printf '%s\n' \
        "Found Herdr executable at $HERDR_EXE" \
        "Would install library files in $LIB_DIR" \
        "Would install launchers in $BIN_DIR" \
        "Would install inactive user units in $UNIT_DIR" \
        "Would preserve existing configuration, credentials, and state"
    [ "$MIGRATE" -eq 0 ] || echo "Would run the explicit state migration"
    exit 0
fi

mkdir -p "$LIB_DIR" "$BIN_DIR" "$UNIT_DIR" "$DOC_DIR/examples" \
    "$SUP_CONFIG_DIR" "$TG_CONFIG_DIR" "$SUP_STATE_DIR" "$SUP_STATE_DIR/query" \
    "$SUP_STATE_DIR/outbox" "$SUP_STATE_DIR/codex-reset/pending" "$SUP_STATE_DIR/codex-reset/processing" \
    "$SUP_STATE_DIR/codex-reset/results" "$SUP_STATE_DIR/pending-starts" "$TG_STATE_DIR" "$TG_LOCK_DIR"
chmod 0700 "$SUP_CONFIG_DIR" "$TG_CONFIG_DIR" "$SUP_STATE_DIR" "$TG_STATE_DIR" "$TG_LOCK_DIR"
chmod 0700 "$SUP_STATE_DIR/query" "$SUP_STATE_DIR/outbox"
chmod 0700 "$SUP_STATE_DIR/codex-reset" "$SUP_STATE_DIR/codex-reset/pending" \
    "$SUP_STATE_DIR/codex-reset/processing" "$SUP_STATE_DIR/codex-reset/results" "$SUP_STATE_DIR/pending-starts"

for file in "$SOURCE_DIR"/src/*.py; do
    install -m 0644 "$file" "$LIB_DIR/$(basename "$file")"
done
for file in "$SOURCE_DIR"/bin/*; do
    install -m 0755 "$file" "$BIN_DIR/$(basename "$file")"
done
for file in "$SOURCE_DIR"/systemd/*; do
    install -m 0644 "$file" "$UNIT_DIR/$(basename "$file")"
done
for file in "$SOURCE_DIR"/docs/*.md; do
    install -m 0644 "$file" "$DOC_DIR/$(basename "$file")"
done
for file in "$SOURCE_DIR"/config/*.example.json; do
    install -m 0644 "$file" "$DOC_DIR/examples/$(basename "$file")"
done
for name in README.md LICENSE VERSION MANIFEST.txt; do
    install -m 0644 "$SOURCE_DIR/$name" "$DOC_DIR/$name"
done

if [ ! -e "$SUP_CONFIG_DIR/config.json" ]; then
    install -m 0600 "$SOURCE_DIR/config/supervisor.example.json" "$SUP_CONFIG_DIR/config.json"
fi
if [ ! -e "$SUP_CONFIG_DIR/backup.json" ]; then
    install -m 0600 "$SOURCE_DIR/config/backup.example.json" "$SUP_CONFIG_DIR/backup.json"
fi
if [ ! -e "$TG_CONFIG_DIR/config.json" ]; then
    install -m 0600 "$SOURCE_DIR/config/telegram.example.json" "$TG_CONFIG_DIR/config.json"
fi

# Validate candidate unit syntax without loading, enabling, starting, or restarting anything.
# systemd-analyze --user needs a runtime directory to resolve user paths; without one it aborts
# before reading any unit, which is not a unit defect. A genuine bad unit still fails the install.
if command -v systemd-analyze >/dev/null 2>&1; then
    if [ -n "${XDG_RUNTIME_DIR:-}" ]; then
        systemd-analyze --user verify "$SOURCE_DIR"/systemd/*
    else
        echo "Skipped unit validation: XDG_RUNTIME_DIR is unset in this session (no user runtime directory)." >&2
    fi
fi

if [ "$MIGRATE" -eq 1 ]; then
    "$BIN_DIR/herdr-supervisor" migrate
fi

cat <<'EOF'
Installed inactive user-scoped files. No service was loaded, enabled, started, or restarted.
Next: configure the supervisor, run `herdr-supervisor doctor`, and follow docs/INSTALL.md.
EOF
