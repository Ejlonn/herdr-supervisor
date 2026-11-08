#!/usr/bin/env bash
# Sync shared, opt-in Claude/Codex helpers from this source checkout.
# Repository-prefixed roles use CHECKOUT to select the target repository.
# Requires Bash, GNU realpath/mv and Python 3.11+ (Codex TOML validation).
# Dry-run writes nothing. Changed installed bytes require --update. Never deletes roles.
# Symlink ancestry/targets and non-regular targets refuse for both providers.
# All source validation/conflict checks precede writes; publication is atomic per file.
# Codex selects only the explicit helper inventory and requires a destination.
set -euo pipefail

package_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
provider=claude
src=""
dest=""
dry=0
update=0
while [ $# -gt 0 ]; do
    case "$1" in
        --provider) provider="${2:?--provider needs claude or codex}"; shift ;;
        --dry-run) dry=1 ;;
        --update) update=1 ;;
        --src) src="${2:?--src needs a directory}"; shift ;;
        --dest) dest="${2:?--dest needs a directory}"; shift ;;
        *) printf 'usage: %s [--provider claude|codex] [--dry-run] [--update] [--src DIR] [--dest DIR]\n' "$0" >&2; exit 2 ;;
    esac
    shift
done

approved_codex_roles="repository_explorer repository_reviewer"
case "$provider" in
    claude)
        [ -n "$src" ] || src="$package_root/agents/claude"
        [ -n "$dest" ] || dest="${HOME:?HOME is not set}/.claude/agents"
        glob_prefix="repository-"; suffix=".md"
        name_re='^repository-[a-z0-9]+(-[a-z0-9]+)*\.md$'
        invalid_text="not a regular file with a repository-<name>.md name"
        ;;
    codex)
        [ -n "$src" ] || src="$package_root/agents/codex"
        [ -n "$dest" ] || { printf 'usage: --provider codex requires an explicit --dest (there is no default destination)\n' >&2; exit 2; }
        glob_prefix="repository_"; suffix=".toml"
        name_re='^repository_[a-z0-9]+(_[a-z0-9]+)*\.toml$'
        invalid_text="not a regular file with a repository_<name>.toml name"
        ;;
    *) printf 'usage: --provider must be claude or codex\n' >&2; exit 2 ;;
esac

for p in "$src" "$dest"; do
    case "/$p/" in */../*) printf 'refused: a ".." component in %s\n' "$p" >&2; exit 2 ;; esac
done
# Also check not-yet-existing destinations below a linked parent.
for p in "$src" "$dest"; do
    [ "$(realpath -ms -- "$p")" = "$(realpath -m -- "$p")" ] || { printf 'refused: a symlink in the path ancestry of %s\n' "$p" >&2; exit 4; }
done

[ -d "$src" ] || { printf 'source directory missing: %s\n' "$src" >&2; exit 4; }
[ ! -e "$dest" ] || [ -d "$dest" ] || { printf 'destination exists and is not a directory: %s\n' "$dest" >&2; exit 4; }

changes=0
conflicts=0
invalid=0
declare -A wanted=()
pend_file=(); pend_name=(); pend_verb=()

for file in "$src"/${glob_prefix}*${suffix}; do
    [ -e "$file" ] || [ -L "$file" ] || continue
    name=$(basename "$file")
    if [ -L "$file" ] || [ ! -f "$file" ] || ! [[ "$name" =~ $name_re ]]; then
        printf 'INVALID  %s (%s)\n' "$name" "$invalid_text" >&2
        invalid=$((invalid + 1))
        continue
    fi
    stem=${name%"$suffix"}
    if [ "$provider" = claude ]; then
        # the frontmatter name must equal the file name without .md
        if ! sed -n '1,/^---$/{/^name: /p}' "$file" | grep -qx "name: ${name%.md}"; then
            printf 'INVALID  %s (frontmatter name does not match the file name)\n' "$name" >&2
            invalid=$((invalid + 1))
            continue
        fi
    else
        # the PARSED top-level `name` (a string) must equal the file name without .toml EXACTLY, and the role must be on the approved inventory.
        # Real TOML semantics (python3 standard-library tomllib, no package): multiline strings cannot spoof it; parse errors and duplicate keys refuse.
        # The comparison happens INSIDE Python on the parsed string: no untrusted name is ever carried through shell text (command substitution would drop
        # trailing newlines and NUL bytes). The shell only reads a fixed exit code: 0 match, 3 parse failure, 4 name missing/not a string, 5 name differs.
        rc_name=0
        python3 -I -c 'import sys, tomllib
try:
    with open(sys.argv[1], "rb") as handle:
        data = tomllib.load(handle)
except Exception:
    sys.exit(3)
if type(data.get("name")) is not str:
    sys.exit(4)
sys.exit(0 if data["name"] == sys.argv[2] else 5)' "$file" "$stem" >/dev/null 2>&1 || rc_name=$?
        case "$rc_name" in
            0) ;;
            5) printf 'INVALID  %s (TOML name does not match the file name)\n' "$name" >&2
               invalid=$((invalid + 1))
               continue ;;
            *) printf 'INVALID  %s (not parseable TOML, or the top-level name is missing or not a string)\n' "$name" >&2
               invalid=$((invalid + 1))
               continue ;;
        esac
        case " $approved_codex_roles " in
            *" $stem "*) ;;
            *) printf 'INVALID  %s (role is not on the approved Codex inventory: %s)\n' "$name" "$approved_codex_roles" >&2
               invalid=$((invalid + 1))
               continue ;;
        esac
    fi
    wanted["$name"]=1
    target="$dest/$name"
    if [ -L "$target" ]; then
        printf 'CONFLICT %s (installed path is a symlink; never followed or replaced)\n' "$name"
        conflicts=$((conflicts + 1))
        continue
    fi
    if [ -e "$target" ] && [ ! -f "$target" ]; then
        printf 'CONFLICT %s (installed path exists and is not a regular file; refused even with --update)\n' "$name"
        conflicts=$((conflicts + 1))
        continue
    fi
    if [ ! -e "$target" ]; then
        verb=INSTALL
    elif cmp -s "$file" "$target"; then
        printf 'unchanged %s\n' "$name"
        continue
    elif [ "$update" -eq 1 ]; then
        verb=UPDATE
    else
        printf 'CONFLICT %s (installed copy differs; rerun with --update only after checking it)\n' "$name"
        conflicts=$((conflicts + 1))
        continue
    fi
    changes=$((changes + 1))
    if [ "$dry" -eq 1 ]; then
        printf 'would %s %s -> %s\n' "$verb" "$name" "$target"
    else
        pend_file+=("$file"); pend_name+=("$name"); pend_verb+=("$verb")
    fi
done

if [ "${#pend_file[@]}" -gt 0 ]; then
    if [ "$invalid" -ne 0 ] || [ "$conflicts" -ne 0 ]; then
        printf 'nothing written: invalid or conflicting input present (all sources are checked before any write)\n'
        changes=0
    else
        for i in "${!pend_file[@]}"; do
            mkdir -p "$dest"
            tmp=$(mktemp "$dest/.${pend_name[$i]}.XXXXXX")
            cp "${pend_file[$i]}" "$tmp"
            chmod 0644 "$tmp"
            # -T: the destination is the file itself, never a directory to move into; the result is verified to be the exact regular file
            mv -f -T -- "$tmp" "$dest/${pend_name[$i]}" || { rm -f -- "${tmp:?}"; printf 'FAILED   %s (publication refused; nothing left behind)\n' "${pend_name[$i]}" >&2; exit 4; }
            if [ -L "$dest/${pend_name[$i]}" ] || [ ! -f "$dest/${pend_name[$i]}" ] || ! cmp -s "${pend_file[$i]}" "$dest/${pend_name[$i]}"; then
                printf 'FAILED   %s (post-install verification: not the exact regular source bytes)\n' "${pend_name[$i]}" >&2
                exit 4
            fi
            printf '%s %s -> %s\n' "${pend_verb[$i]}" "${pend_name[$i]}" "$dest/${pend_name[$i]}"
        done
    fi
fi

preserved=0
if [ -d "$dest" ]; then
    for installed in "$dest"/*; do
        [ -e "$installed" ] || { [ "$provider" = codex ] && [ -L "$installed" ]; } || continue
        name=$(basename "$installed")
        if [[ "$name" == ${glob_prefix}*${suffix} ]]; then
            if [ -z "${wanted[$name]:-}" ]; then
                printf 'ORPHAN   %s (no source; left in place, never deleted)\n' "$name"
            fi
        else
            preserved=$((preserved + 1))
        fi
    done
fi

printf 'summary: changes=%d conflicts=%d invalid=%d non-prefixed-preserved=%d dry-run=%d\n' "$changes" "$conflicts" "$invalid" "$preserved" "$dry"
[ "$invalid" -eq 0 ] || exit 4
[ "$conflicts" -eq 0 ] || exit 3
