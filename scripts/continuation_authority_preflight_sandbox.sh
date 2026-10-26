#!/bin/bash
# Run continuation_authority_preflight.py for in-flight slices without touching
# production state: private mount + network namespace, /opt read-only, the
# runtime root behind a disposable overlay (all writes land in WORK), optional
# immutable snapshot roots overlaid on their original absolute paths, and
# credential files masked.  Must be started as:
#   sudo -n unshare -m -n --propagation private bash THIS WORK SNAPSHOT CODE TOOL SLICES
# WORK      new empty directory for overlays/reports (outside /opt)
# brief-contract: writes WORK=$1 ; reads: SNAP=$2 CODE=$3 TOOL=$4 SLICES=$5 ; constraints: WORK=existing-empty-directory-outside-/opt,overlay-reports-tmp-home-under-WORK ; expects_change: none
# SNAPSHOT  '-' or a directory holding <snapshot>/opt/... copies of source roots
#           (each top-level root listed in SNAPSHOT/ROOTS, one absolute path per line)
# CODE      code root to test: a deployed /opt/bilive/autoslice/versions/<sha>/repo,
#           or any tree, which is installed as a versioned repo inside the overlay
# TOOL      path of continuation_authority_preflight.py (run with python -I)
# SLICES    file with lines: <spec path> <existing_srt|aggregate_asr>
set -euo pipefail
WORK=$1; SNAP=$2; CODE=$3; TOOL=$4; SLICES=$5
[ "$(readlink /proc/self/ns/net)" != "$(readlink /proc/1/ns/net)" ] || { echo "refuse: no private network namespace" >&2; exit 2; }
[ "$(readlink /proc/self/ns/mnt)" != "$(readlink /proc/1/ns/mnt)" ] || { echo "refuse: no private mount namespace" >&2; exit 2; }
case "$WORK" in /opt/*) echo "refuse: WORK under /opt" >&2; exit 2;; esac
[ -d "$WORK" ] && [ -z "$(ls -A "$WORK")" ] || { echo "refuse: WORK must be an existing empty directory" >&2; exit 2; }
RT=/opt/bilive/autoslice
mount --make-rprivate /
mkdir -p "$WORK"/rt-upper "$WORK"/rt-work "$WORK"/reports "$WORK"/tmp "$WORK"/home
mount --bind /opt /opt
mount -o remount,bind,ro /opt
mount -t overlay overlay -o "lowerdir=$RT,upperdir=$WORK/rt-upper,workdir=$WORK/rt-work" "$RT"
if [ "$SNAP" != - ]; then
    n=0
    while IFS= read -r root; do
        [ -n "$root" ] || continue
        n=$((n + 1)); mkdir -p "$WORK/snap-upper/$n" "$WORK/snap-work/$n"
        mount -t overlay overlay -o "lowerdir=$SNAP$root,upperdir=$WORK/snap-upper/$n,workdir=$WORK/snap-work/$n" "$root"
    done < "$SNAP/ROOTS"
fi
for secret in /opt/bilive/.env /opt/bilive/.env.* "$RT"/*.env "$RT"/.env*; do
    if [ -f "$secret" ]; then mount --bind /dev/null "$secret"; fi
done
if [[ "$CODE" != "$RT"/versions/*/repo ]]; then
    VID=$(find "$CODE" -type f -not -path '*/.git/*' -printf '%P\n' | sort | sha1sum | cut -c1-40)
    mkdir -p "$RT/versions/$VID"
    cp -a "$CODE" "$RT/versions/$VID/repo"
    cp "$RT/current/repo/DEPLOYED_MANIFEST.json" "$RT/versions/$VID/repo/DEPLOYED_MANIFEST.json"
    env -i PATH=/usr/bin:/bin "$RT/venv-main/bin/python" -I -B "$TOOL" --seal-version "$RT/versions/$VID/repo" "$VID" \
        "$RT/current/repo/DEPLOYED_AUTHORITY_MANIFEST.json"
    cp "$(dirname "$(readlink -f "$RT/current/repo")")/runtime-code-lease.py" "$RT/versions/$VID/runtime-code-lease.py"
    CODE="$RT/versions/$VID/repo"
fi
echo "code_root=$CODE"
if [ "$SLICES" = --exec ]; then
    # Diagnose inside the identical view: TOOL is a read-only script, the
    # remaining arguments are its own.
    shift 5
    exec env -i PATH=/usr/local/bin:/usr/bin:/bin HOME="$WORK/home" TMPDIR="$WORK/tmp" LANG=C.UTF-8 \
        CONTINUATION_PREFLIGHT_SANDBOX=1 AUTOSLICE_BASE="$RT" AUTOSLICE_FINAL_REVIEW_RUNTIME_ROOT="$RT" \
        AUTOSLICE_PROFILE=lidousha "$RT/venv-main/bin/python" -I -B "$TOOL" "$CODE" "$@"
fi
if [ "$SLICES" = --canary ]; then
    # Negative canaries: every forbidden effect must fail or stay in WORK.
    if touch /opt/bilive/CONTINUATION_PREFLIGHT_CANARY 2>/dev/null; then echo "CANARY_FAIL opt_writable"; exit 3; fi
    touch "$RT/CONTINUATION_PREFLIGHT_CANARY" && [ -f "$WORK/rt-upper/CONTINUATION_PREFLIGHT_CANARY" ] || { echo "CANARY_FAIL overlay"; exit 3; }
    [ ! -s /opt/bilive/.env ] && [ ! -s "$RT/cpa.env" ] || { echo "CANARY_FAIL credentials_visible"; exit 3; }
    if timeout 5 bash -c 'exec 3<>/dev/tcp/1.1.1.1/443' 2>/dev/null; then echo "CANARY_FAIL network"; exit 3; fi
    echo CANARY_OK
    exit 0
fi
rc=0
while read -r spec substrate; do
    [ -n "$spec" ] || continue
    cid=$(basename "$(dirname "$spec")")
    env -i PATH=/usr/local/bin:/usr/bin:/bin HOME="$WORK/home" TMPDIR="$WORK/tmp" LANG=C.UTF-8 \
        CONTINUATION_PREFLIGHT_SANDBOX=1 AUTOSLICE_BASE="$RT" AUTOSLICE_FINAL_REVIEW_RUNTIME_ROOT="$RT" \
        AUTOSLICE_PROFILE=lidousha GEMINI_PAID_BACKUP_DEV_EXCEPTION=0 PYTHONDONTWRITEBYTECODE=1 \
        ionice -c 3 nice -n 10 "$RT/venv-main/bin/python" -I -B "$TOOL" --spec "$spec" --code-root "$CODE" \
        --substrate "$substrate" --report "$WORK/reports/$cid.json" \
        --time-budget-s "${PREFLIGHT_TIME_BUDGET_S:-0}" || rc=1
done < "$SLICES"
exit $rc
