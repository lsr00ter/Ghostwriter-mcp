#!/usr/bin/env bash
#
# Keep the vendored Ghostwriter Agent Skills collection in sync with upstream
# (https://github.com/GhostManager/ghostwriter-skills) and keep the skills
# discoverable by Claude Code.
#
#   ./scripts/sync-skills.sh            show pinned commit vs upstream main (default)
#   ./scripts/sync-skills.sh init       clone the submodule and create links
#   ./scripts/sync-skills.sh link       rebuild links only
#   ./scripts/sync-skills.sh preview    fetch upstream and show incoming commits
#   ./scripts/sync-skills.sh update     move the submodule to upstream main and relink
#   ./scripts/sync-skills.sh validate   run upstream's own skill validation
#
set -euo pipefail

ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT"

SUB="vendor/ghostwriter-skills"
LINK_DIR=".claude/skills"
# Links we own point here; anything else in LINK_DIR (e.g. our own skills) is left alone.
LINK_PREFIX="../../$SUB/skills"

c_ok()   { printf '\033[32m%s\033[0m\n' "$*"; }
c_warn() { printf '\033[33m%s\033[0m\n' "$*"; }
c_err()  { printf '\033[31m%s\033[0m\n' "$*" >&2; }
c_dim()  { printf '\033[2m%s\033[0m\n' "$*"; }

has_net_timeout() { command -v timeout >/dev/null 2>&1 && echo timeout || { command -v gtimeout >/dev/null 2>&1 && echo gtimeout; }; }

# Print the header comment block, and nothing past it.
usage() { awk 'NR>1 && /^#/ { sub(/^# ?/, ""); print; next } NR>1 { exit }' "$0"; }

pinned() { git -C "$SUB" rev-parse HEAD 2>/dev/null || true; }

latest() {
    local t; t="$(has_net_timeout)"
    # Scoped with -C so this queries the submodule's origin, not this repo's.
    if [ -n "$t" ]; then
        "$t" 60 git -C "$SUB" ls-remote origin refs/heads/main 2>/dev/null | cut -f1
    else
        git -C "$SUB" ls-remote origin refs/heads/main 2>/dev/null | cut -f1
    fi
}

is_initialised() { [ -e "$SUB/skills" ] && [ -n "$(pinned)" ]; }

require_init() {
    if ! is_initialised; then
        c_warn "Submodule is not checked out yet."
        c_dim  "Run: $0 init"
        return 1
    fi
}

link() {
    if ! [ -e "$SUB/skills" ]; then
        c_err "Cannot link: $SUB/skills is missing. Run '$0 init' first."
        return 1
    fi
    mkdir -p "$LINK_DIR"

    local expected=() name
    for d in "$SUB"/skills/*/; do
        [ -f "$d/SKILL.md" ] || continue
        name="$(basename "$d")"
        expected+=("$name")
        ln -sfn "$LINK_PREFIX/$name" "$LINK_DIR/$name"
    done

    # Drop links we own whose skill disappeared upstream, so a removed skill
    # does not linger as a dangling link. Never touches links we do not own.
    local l target base keep
    for l in "$LINK_DIR"/*; do
        [ -L "$l" ] || continue
        target="$(readlink "$l")"
        case "$target" in
            "$LINK_PREFIX"/*) ;;
            *) continue ;;
        esac
        base="$(basename "$l")"
        keep=""
        for name in "${expected[@]:-}"; do
            [ "$name" = "$base" ] && keep=1 && break
        done
        if [ -z "$keep" ]; then
            rm "$l"
            c_dim "  pruned stale link: $base"
        fi
    done

    c_ok "Linked ${#expected[@]} upstream skill(s) into $LINK_DIR"
    for name in "${expected[@]:-}"; do
        if [ -f "$LINK_DIR/$name/SKILL.md" ]; then
            printf '  %s\n' "$name"
        else
            c_err "  $name is dangling"
            return 1
        fi
    done
}

cmd_status() {
    if ! is_initialised; then
        c_warn "Submodule not initialised."
        c_dim  "Run: $0 init"
        return 1
    fi
    local p l
    p="$(pinned)"
    echo "submodule : $SUB"
    echo "pinned    : $(git -C "$SUB" log -1 --format='%h %ad %s' --date=short)"
    if l="$(latest)" && [ -n "$l" ]; then
        if [ "$p" = "$l" ]; then
            c_ok "up to date with origin/main ($(echo "$l" | cut -c1-9))"
        else
            c_warn "upstream main has moved to $(echo "$l" | cut -c1-9)"
            c_dim  "Run: $0 preview"
        fi
    else
        c_dim "upstream main unknown (offline?)"
    fi
    echo
    echo "skills in $LINK_DIR:"
    local n
    for n in "$LINK_DIR"/*; do
        [ -e "$n" ] || continue
        if [ -f "$n/SKILL.md" ]; then
            printf '  %-24s %s\n' "$(basename "$n")" "$( [ -L "$n" ] && echo link || echo local)"
        else
            c_warn "  $(basename "$n") (dangling)"
        fi
    done
}

cmd_init() {
    if is_initialised; then
        c_dim "Submodule already initialised."
    else
        git submodule update --init --checkout "$SUB"
    fi
    link
    echo
    c_dim "Pinned to $(git -C "$SUB" rev-parse --short HEAD)"
}

cmd_preview() {
    require_init || return 1
    git -C "$SUB" fetch --quiet origin main
    local p l
    p="$(pinned)"
    l="$(git -C "$SUB" rev-parse FETCH_HEAD)"
    if [ "$p" = "$l" ]; then
        c_ok "Already at upstream main ($(echo "$p" | cut -c1-9))."
        return 0
    fi

    echo "Incoming upstream commits:"
    echo
    git -C "$SUB" log --oneline --no-decorate "$p..$l" | sed 's/^/  /'
    echo
    echo "Files changed:"
    echo
    git -C "$SUB" diff --stat "$p" "$l" | sed 's/^/  /'

    # Skills ship executable helpers; changing them changes what an agent may run.
    local scripts
    scripts="$(git -C "$SUB" diff --name-only "$p" "$l" -- '*.py' '*.sh' '*.js' '*.ts')"
    if [ -n "$scripts" ]; then
        echo
        c_warn "Executable code changed - review before updating:"
        echo "$scripts" | sed 's/^/  /'
    fi
    echo
    echo "Apply with: $0 update"
}

cmd_update() {
    require_init || return 1
    if [ -n "$(git status --porcelain)" ]; then
        c_err "Working tree is not clean; commit or stash first so this bump is reviewable on its own."
        return 1
    fi
    git -C "$SUB" fetch --quiet origin main
    local p l
    p="$(pinned)"
    l="$(git -C "$SUB" rev-parse FETCH_HEAD)"
    if [ "$p" = "$l" ]; then
        c_ok "Already at upstream main; nothing to do."
        link
        return 0
    fi
    echo "Moving $(echo "$p" | cut -c1-9) -> $(echo "$l" | cut -c1-9)"
    git -C "$SUB" log --oneline --no-decorate "$p..$l" | sed 's/^/  /'
    git -C "$SUB" checkout --quiet --detach "$l"
    link
    echo
    c_ok "Submodule moved. Review and commit:"
    c_dim  "  git add $SUB $LINK_DIR && git commit -m 'chore: bump ghostwriter-skills'"
}

cmd_validate() {
    require_init || return 1
    if ! command -v skills-ref >/dev/null 2>&1; then
        c_warn "upstream 'validate' needs skills-ref, which is not installed:"
        c_dim  "  https://github.com/agentskills/agentskills/tree/main/skills-ref"
        c_dim  "Running the fixture tests only."
        echo
        (cd "$SUB" && python3 -m unittest discover -s tests -p 'test_*.py')
        return 0
    fi
    (cd "$SUB" && make validate)
}

case "${1:-status}" in
    status)   cmd_status ;;
    init)     cmd_init ;;
    link)     link ;;
    preview)  cmd_preview ;;
    update)   cmd_update ;;
    validate) cmd_validate ;;
    -h|--help|help)
        usage
        ;;
    *)
        c_err "Unknown command: $1"
        usage
        exit 1
        ;;
esac
