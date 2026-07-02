# Future Li Dousha live E2E no-upload runbook

Generated: 2026-06-30

## Purpose

Run the next future Li Dousha livestream through the unattended slicing stack from recording start to final no-upload slice decision.

This runbook is direct-takeover only: no Kanban, no real upload, no publish side effects.

## Critical terminology rule

- Final subtitles/output must use `kmx`.
- `kimo熊` is only a developer alias/pronunciation hint for `kmx`.
- `天不熊` is a wrong transcript and must normalize to `kmx`.
- Authoritative seed source: Ivan-edited subtitles under `lidousha/**/**/*.manual-edited*.srt`.
- Project lexicon path: `lidousha/term_lexicon.json`.

Before future-live testing, sync the lexicon to the runtime roots where discovery can find it:

```bash
scp /Users/ivan/Project/vtuber-slice/lidousha/term_lexicon.json free:/opt/bilive/app/lidousha/term_lexicon.json
ssh free 'docker cp /opt/bilive/app/lidousha/term_lexicon.json bilive_record:/app/lidousha/term_lexicon.json'
```

## Required model/QA rule

Semantic QA must be script-driven CPA, not an interactive Hermes answer.

- Script wrapper: `scripts/cpa_semantic_review.py`
- Request schema: `cpa-semantic-review-request.v1`
- Response schema: `cpa-semantic-review-response.v1`
- Output must be JSON-only and parseable.
- Response must echo the request's `request_sha256` and `artifact_paths`.
- Missing/invalid/mismatched CPA JSON fails closed.

Example mock command shape:

```bash
python3 scripts/cpa_semantic_review.py \
  --candidate-id CANDIDATE_ID \
  --candidate-text 'raw candidate text' \
  --normalized-text 'terminology-normalized candidate text' \
  --request-json /app/reports/.../candidate.cpa.request.json \
  --response-json /app/reports/.../candidate.cpa.response.json \
  --cpa-command 'REAL_CPA_CLI {request_json} {response_json}'
```

The exact `REAL_CPA_CLI` still needs to be filled from the machine's CPA provider configuration. Do not fake it in production. In tests, use a fake responder only.

## Pre-live checklist

Run on `free` before the next live starts:

```bash
ssh free 'set -euo pipefail
printf "== recorder/container ==\n"
docker ps --format "{{.Names}} {{.Status}}" | grep -E "bilive_record|blrec" || true
printf "== no upload processes ==\n"
ps -ef | grep -E "src\.upload\.upload|UploadController|biliup" | grep -v grep || true
docker exec bilive_record sh -lc "ps -ef | grep -E \"src\\.upload\\.upload|UploadController|biliup\" | grep -v grep || true"
printf "== term lexicon ==\n"
docker exec bilive_record sh -lc "test -s /app/lidousha/term_lexicon.json && python3 - <<'PY'
from pathlib import Path
from src.autoslice.term_lexicon import load_term_lexicon, normalize_text
lex=load_term_lexicon('/app/lidousha/term_lexicon.json')
print(normalize_text('天不熊和kimo熊', lexicon=lex))
PY"
'
```

Expected lexicon output:

```text
kmx和kmx
```

## Recording phase

1. Let `bilive_record`/blrec record room `22966160` normally.
2. Do not start uploader.
3. Watch only for liveness/disk/source health.
4. If recorder fails, preserve raw files; do not delete sources.

Suggested monitor:

```bash
ssh free 'docker exec bilive_record sh -lc "date; du -sh /app/Videos/22966160 || true; ps -ef | grep -E \"blrec|bilive|record\" | grep -v grep || true"'
```

## Post-live source stabilization

After live ends, wait until the latest date directory is stable:

```bash
ssh free 'docker exec bilive_record sh -lc "cd /app && python3 - <<'PY'
from pathlib import Path
import time
root=Path('/app/Videos/22966160')
latest=max([p for p in root.iterdir() if p.is_dir() and p.name[:4].isdigit()], key=lambda p:p.name)
print('latest', latest)
files=sorted(latest.rglob('*'))
for p in files[-20:]:
    if p.is_file(): print(p, p.stat().st_size)
PY"'
```

Hard gate:

- Full source media must exist and be stable.
- Full source SRT must exist or be generated before selector review.
- `source_integrity` must pass or official/replacement source must fill gaps.

## Candidate selection + CPA + no-upload slicing

The intended unattended runner should perform these steps:

1. Parse full source SRT with `term_lexicon` discovery enabled.
2. Run `select_full_session_candidates(...)`.
3. For each top candidate:
   - collect raw candidate text;
   - normalize text through `term_lexicon` (`天不熊`/`kimo熊` -> `kmx`);
   - write CPA request JSON;
   - run CPA command;
   - validate response JSON;
   - include `cpa_semantic_response_path` in the source-context job.
4. Run `run_auto_review_shadow_pipeline.py` in live-source no-upload mode.
5. Materialize preview recut for viable candidates.
6. Accurate re-render when keyframe drift exceeds threshold.
7. Persist summary, evidence, materialized recut, render QA, CPA request/response.

No-upload acceptance criteria:

- `AUTO_UPLOAD >= 1` or explicit fail-closed explanation.
- `reason_codes == []` for the accepted candidate.
- `no_upload: true`.
- `no_upload_or_free_deploy_performed: true`.
- CPA response JSON present and valid.
- `actual_cut_error_ms <= 100`.
- final subtitle text contains `kmx` and not `天不熊`/`kimo熊`.
- no upload process before/after.

## Production blockers before real upload

Even if no-upload returns `AUTO_UPLOAD`, real upload still requires explicit Ivan authorization and the existing upload gate:

- AUTO_UPLOAD manifest.
- artifact hashes.
- no CPA/semantic/terminology reason codes.
- no Jingting provenance gaps.
- no source-integrity gaps.
- no render QA failure.

## Current implementation status

Implemented locally in this direct takeover lane:

- `lidousha/term_lexicon.json` corrected: final output uses `kmx`; `kimo熊` is alias only.
- `src/autoslice/term_lexicon.py` no longer emits alias as display output.
- `scripts/cpa_semantic_review.py` writes strict request JSON, invokes external CPA command, and validates response JSON fail-closed.
- `src/autoslice/cpa_semantic_qa.py` is the authoritative CPA request/response contract: request hash, artifact-path checks, score range checks, and ReviewEvidence integration.
- `src/autoslice/cpa_semantic_review.py` remains only as a legacy response-only fallback for old source-context jobs.
- `scripts/run_auto_review_shadow_pipeline.py` can apply `cpa_semantic_request_path` + `cpa_semantic_response_path` from the source-context job and BLOCK on terminology/CPA failures.

Still needed:

- Fill in the real CPA command for `--cpa-command` on `free`.
- Add or wire a full future-live runner that calls the selector, term normalizer, CPA script, and shadow pipeline in one command.
- Run it on a livestream that occurs after this runbook is written.
