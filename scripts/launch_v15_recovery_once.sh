#!/bin/bash
# Refresh and run the isolated 2026-07-22 V15 recovery lane once.
#
# The production repo intentionally retains historical delivery packages under
# repo/lidousha/.  They are runtime state, not code, and must not be cloned into
# a recovery runner: doing so duplicates several GiB and can fill the host.
set -euo pipefail

RECOVERY_BASE="${AUTOSLICE_V15_BASE:-/opt/bilive/autoslice/recovery/2026-07-22/full-rerun-v15-screenshot-cover}"
PRODUCTION_REPO="${AUTOSLICE_PRODUCTION_REPO:-/opt/bilive/autoslice/repo}"
RECOVERY_ROOT=/opt/bilive/autoslice/recovery

case "$RECOVERY_BASE" in
    "$RECOVERY_ROOT"/*) ;;
    *)
        echo "REFUSE: V15 recovery base is outside $RECOVERY_ROOT: $RECOVERY_BASE" >&2
        exit 2
        ;;
esac

test -d "$RECOVERY_BASE"
test -d "$RECOVERY_BASE/repo"
test -f "$PRODUCTION_REPO/DEPLOYED_COMMIT"
test -f "$PRODUCTION_REPO/scripts/free_session_autoslice.py"

busy=0
for pid in $(pgrep -f "python3 scripts/free_session_autoslice.py" || true); do
    if tr '\0' '\n' <"/proc/$pid/environ" 2>/dev/null \
        | grep -Fxq "AUTOSLICE_BASE=$RECOVERY_BASE"; then
        busy=1
        break
    fi
done
if [ "$busy" = "1" ]; then
    printf '%s V15_BUSY_SKIP\n' "$(date -u +%FT%TZ)" \
        >>"$RECOVERY_BASE/logs/r19-launcher.log"
    exit 0
fi

STAGE="$RECOVERY_BASE/repo.new"
OLD="$RECOVERY_BASE/repo.old"
if [ -e "$OLD" ]; then
    echo "REFUSE: prior V15 repo.old exists and requires inspection: $OLD" >&2
    exit 3
fi

rm -rf -- "$STAGE"
mkdir -p "$STAGE"

# Copy code and committed runtime assets, excluding the production lane's
# accumulated delivery packages.  Retain legacy repo-root lexicon files only
# when they exist; the current glossary authority is the channel-profile asset
# at assets/lidousha/glossary.txt.
tar -C "$PRODUCTION_REPO" \
    --exclude='./.git' \
    --exclude='./lidousha' \
    --exclude='./lidousha/**' \
    -cf - . \
    | tar -C "$STAGE" -xf -
mkdir -p "$STAGE/lidousha"
for legacy_lexicon in lidousha_glossary.txt term_lexicon.json; do
    if [ -f "$PRODUCTION_REPO/lidousha/$legacy_lexicon" ]; then
        cp -p \
            "$PRODUCTION_REPO/lidousha/$legacy_lexicon" \
            "$STAGE/lidousha/$legacy_lexicon"
    fi
done

test -f "$STAGE/DEPLOYED_COMMIT"
test -f "$STAGE/scripts/free_session_autoslice.py"
test -f "$STAGE/assets/lidousha/glossary.txt"
if find "$STAGE/lidousha" -mindepth 1 -maxdepth 1 -type d | grep -q .; then
    echo "REFUSE: V15 slim refresh unexpectedly copied delivery directories" >&2
    exit 4
fi
stage_bytes=$(du -sb "$STAGE" | awk '{print $1}')
if [ "$stage_bytes" -gt 268435456 ]; then
    echo "REFUSE: V15 slim refresh exceeds 256 MiB: $stage_bytes bytes" >&2
    exit 5
fi

mv "$RECOVERY_BASE/repo" "$OLD"
mv "$STAGE" "$RECOVERY_BASE/repo"
rm -rf -- "$OLD"

cd "$RECOVERY_BASE/repo"
printf '%s launching r19 from %s (slim repo: %s bytes)\n' \
    "$(date -u +%FT%TZ)" \
    "$(head -c 12 DEPLOYED_COMMIT)" \
    "$stage_bytes" \
    >>"$RECOVERY_BASE/logs/r19-launcher.log"
exec env \
    AUTOSLICE_BASE="$RECOVERY_BASE" \
    AUTOSLICE_REC_ROOT="$RECOVERY_BASE/recordings" \
    AUTOSLICE_COVER_MODE=screenshot \
    PYTHONDONTWRITEBYTECODE=1 \
    /usr/bin/flock -n "$RECOVERY_BASE/runner.lock" \
    /usr/bin/python3 scripts/free_session_autoslice.py --once \
    >>"$RECOVERY_BASE/logs/v15-runner-once.log" 2>&1
