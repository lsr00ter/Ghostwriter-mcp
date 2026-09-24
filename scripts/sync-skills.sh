#!/usr/bin/env bash
#
# Keep the vendored Ghostwriter Agent Skills collection in sync with upstream
# (https://github.com/GhostManager/ghostwriter-skills) and keep every skill
# discoverable by the agents used in this project.
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

# Project-local skill directories, as defined by the Agent Skills tooling.
# .agents is the shared location; the rest are each agent's own. All sit two
# levels below the repo root, so one relative prefix works for every one.
LINK_DIRS=(".agents/skills" ".claude/skills" ".codex/skills" ".pi/skills")

# Skill sources: this repo's own skills and the vendored upstream collection.
LOCAL_SKILLS="skills"
LOCAL_PREFIX="../../$LOCAL_SKILLS"
VENDOR_SKILLS="$SUB/skills"
VENDOR_PREFIX="../../$VENDOR_SKILLS"

c_ok()   { printf '\033[32m%s\033[0m\n' "$*"; }
c_warn() { printf '\033[33m%s\033[0m\n' "$*"; }
c_err()  { printf '\033[31m%s\033[0m\n' "$*" >&2; }
c_dim()  { printf '\033[2m%s\033[0m\n' "$*"; }

# Print the header comment block, and nothing past it.
usage() { awk 'NR>1 && /^#/ { sub(/^# ?/, ""); print; next } NR>1 { exit }' "$0"; }

has_net_timeout() { command -v timeout >/dev/null 2>&1 && echo timeout || { command -v gtimeout >/dev/null 2>&1 && echo gtimeout; }; }

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

is_initialised() { [ -e "$VENDOR_SKILLS" ] && [ -n "$(pinned)" ]; }

require_init() {
    if ! is_initialised; then
        c_warn "Submodule is not checked out yet."
        c_dim  "Run: $0 init"
        return 1
    fi
}

# Skill directory names in a source tree, one per line.
skill_names_in() {
    local src="$1" d
    [ -d "$src" ] || return 0
    for d in "$src"/*/; do
        [ -f "$d/SKILL.md" ] && basename "$d"
    done
    return 0
}

# Rebuild the links in one discovery directory. Only links pointing at a source
# we own are managed or pruned, so anything else a user added is left alone.
link_dir() {
    local dir="$1" name target base keep local_count=0 vendor_count=0
    mkdir -p "$dir"

    local expected=()
    while IFS= read -r name; do
        [ -n "$name" ] || continue
        expected+=("$name")
        local_count=$((local_count+1))
        ln -sfn "$LOCAL_PREFIX/$name" "$dir/$name"
    done < <(skill_names_in "$LOCAL_SKILLS")

    while IFS= read -r name; do
        [ -n "$name" ] || continue
        expected+=("$name")
        vendor_count=$((vendor_count+1))
        ln -sfn "$VENDOR_PREFIX/$name" "$dir/$name"
    done < <(skill_names_in "$VENDOR_SKILLS")

    # Prune links we own whose skill disappeared upstream.
    for l in "$dir"/*; do
        [ -L "$l" ] || continue
        target="$(readlink "$l")"
        case "$target" in
            "$LOCAL_PREFIX"/*|"$VENDOR_PREFIX"/*) ;;
            *) continue ;;
        esac
        base="$(basename "$l")"
        keep=""
        for name in "${expected[@]:-}"; do
            [ "$name" = "$base" ] && keep=1 && break
        done
        if [ -z "$keep" ]; then
            rm "$l"
            c_dim "  pruned stale link: $dir/$base"
        fi
    done

    # Fail loudly if any expected link does not resolve. A dangling link means an
    # agent silently cannot see the skill.
    local bad=0
    for name in "${expected[@]:-}"; do
        if ! [ -f "$dir/$name/SKILL.md" ]; then
            c_err "  dangling: $dir/$name"
            bad=1
        fi
    done
    printf '  %-16s %d local + %d upstream\n' "$dir" "$local_count" "$vendor_count"
    return "$bad"
}

link_all() {
    local dir rc=0
    for dir in "${LINK_DIRS[@]}"; do
        link_dir "$dir" || rc=1
    done
    if [ "$rc" -eq 0 ]; then
        c_ok "Links rebuilt in ${#LINK_DIRS[@]} discovery directories"
    else
        c_err "Some links do not resolve"
    fi
    return "$rc"
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

    local dir n total_bad=0
    for dir in "${LINK_DIRS[@]}"; do
        echo
        echo "$dir:"
        [ -d "$dir" ] || { c_warn "  missing - run: $0 link"; continue; }
        for n in "$dir"/*; do
            [ -e "$n" ] || continue
            if [ -f "$n/SKILL.md" ]; then
                printf '  %-24s %s\n' "$(basename "$n")" "$( [ -L "$n" ] && echo link || echo local)"
            else
                c_warn "  $(basename "$n") (dangling)"
                total_bad=$((total_bad+1))
            fi
        done
    done
    [ "$total_bad" -eq 0 ] || return 1
}

cmd_init() {
    if is_initialised; then
        c_dim "Submodule already initialised."
    else
        git submodule update --init --checkout "$SUB"
    fi
    link_all
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
        link_all
        return 0
    fi
    echo "Moving $(echo "$p" | cut -c1-9) -> $(echo "$l" | cut -c1-9)"
    git -C "$SUB" log --oneline --no-decorate "$p..$l" | sed 's/^/  /'
    git -C "$SUB" checkout --quiet --detach "$l"
    link_all
    echo
    c_ok "Submodule moved. Review and commit:"
    c_dim  "  git add $SUB ${LINK_DIRS[*]} && git commit -m 'chore: bump ghostwriter-skills'"
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
    link)     link_all ;;
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
