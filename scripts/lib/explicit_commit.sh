#!/usr/bin/env bash
# Resolve a caller-supplied full commit id; never select a deployment source.
resolve_explicit_commit() {
    local root=$1 requested=${2:-} resolved
    if [[ ! "$requested" =~ ^[0-9a-fA-F]{40}$ ]]; then
        printf 'EXPLICIT_COMMIT_REFUSED: supply a full 40-hex commit SHA\n' >&2
        return 2
    fi
    if [[ "$(git -C "$root" cat-file -t "$requested" 2>/dev/null)" != commit ]]; then
        printf 'EXPLICIT_COMMIT_REFUSED: SHA must name an existing commit object\n' >&2
        return 2
    fi
    resolved=$(git -C "$root" rev-parse --verify "${requested}^{commit}") || return 2
    if ! git -C "$root" merge-base --is-ancestor "$resolved" refs/heads/main; then
        printf 'EXPLICIT_COMMIT_REFUSED: commit must be an ancestor of refs/heads/main\n' >&2
        return 2
    fi
    printf '%s\n' "$resolved"
}
