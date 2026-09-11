#!/bin/sh
set -eu

usage() {
    cat <<'EOF'
Usage: ./uninstall.sh [--dry-run] [--purge --confirm-purge]

Removes only known installed program files. Configuration, credentials, and
state are preserved unless both --purge and --confirm-purge are supplied.
The script refuses a live uninstall while related user services are active,
transitioning, or cannot be queried through the user systemd manager.
EOF
}

DRY_RUN=0
PURGE=0
CONFIRM_PURGE=0
for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=1 ;;
        --purge) PURGE=1 ;;
        --confirm-purge) CONFIRM_PURGE=1 ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; exit 2 ;;
    esac
done
[ "$PURGE" -eq 0 ] || [ "$CONFIRM_PURGE" -eq 1 ] || { echo "--purge also requires --confirm-purge" >&2; exit 2; }

LIB_DIR="$HOME/.local/lib/herdr-supervisor"
BIN_DIR="$HOME/.local/bin"
UNIT_DIR="$HOME/.config/systemd/user"
DOC_DIR="$HOME/.local/share/doc/herdr-supervisor"

# Service-state guard. Only an established inactive/failed answer permits removal: an active or
# transitioning unit refuses, and a query that cannot reach the user manager fails closed instead
# of being mistaken for "inactive".
if [ "$DRY_RUN" -eq 0 ] && command -v systemctl >/dev/null 2>&1; then
    for unit in herdr-telegram.service herdr-supervisor-worker.service herdr-query-worker.service herdr-backup.service herdr-codex-reset-worker.service; do
        state=$(systemctl --user is-active "$unit" 2>/dev/null) && rc=0 || rc=$?
        if [ "$rc" -eq 0 ]; then
            echo "Refusing uninstall while $unit is active; stop it explicitly and retry." >&2
            exit 2
        fi
        case "$state" in
            inactive|failed) ;;
            activating|deactivating|reloading|refreshing)
                echo "Refusing uninstall while $unit is $state; wait for it to settle and retry." >&2
                exit 2 ;;
            *)
                echo "Cannot establish the state of $unit (systemctl exit $rc); retry from a session with a user systemd bus." >&2
                exit 2 ;;
        esac
    done
fi

remove_file() {
    if [ "$DRY_RUN" -eq 1 ]; then echo "Would remove $1"; else rm -f -- "$1"; fi
}

for name in herdr-supervisor herdr-telegram herdr-query-worker herdr-backup herdr-codex-reset-worker; do remove_file "$BIN_DIR/$name"; done
for name in herdr-server.service herdr-supervisor-worker.service herdr-supervisor-worker.path herdr-query-worker.service herdr-query-worker.path herdr-telegram.service herdr-backup.service herdr-backup.timer herdr-codex-reset-worker.service herdr-codex-reset-worker.path; do remove_file "$UNIT_DIR/$name"; done

if [ "$DRY_RUN" -eq 1 ]; then
    echo "Would remove $LIB_DIR and $DOC_DIR"
else
    rm -rf -- "$LIB_DIR" "$DOC_DIR"
fi

if [ "$PURGE" -eq 1 ]; then
    if [ "$DRY_RUN" -eq 1 ]; then
        echo "Would purge supervisor and Telegram configuration/state"
    else
        rm -rf -- "$HOME/.config/herdr-supervisor" "$HOME/.config/herdr-telegram" \
            "$HOME/.local/state/herdr-supervisor" "$HOME/.local/state/herdr-telegram" \
            "$HOME/.local/state/herdr-telegram-locks"
    fi
else
    echo "Configuration, credentials, and state were preserved."
fi
