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

The real CPA command (validated 2026-07-03, room 362064 full-song e2e). CPA stages must be REAL in workflow tests too — a fake responder is only acceptable inside pytest unit tests, never in an acceptance/e2e run:

```bash
# semantic QA judge (fills --cpa-command of the selector runner)
python3 scripts/cpa_semantic_qa_llm.py \
  --request {request_json} --response {response_json} \
  --transport direct --model gpt-5.4-mini \
  --api-base "$CPA_BASE_URL" --api-key-env CPA_API_KEY
```

Notes:
- `CPA_BASE_URL`/`CPA_API_KEY` live in the Mac zsh environment.
- CPA sits behind Cloudflare: every direct HTTP call must send a browser User-Agent (`Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)`) or it 403s with error 1010. Already handled in `src/autoslice/llm_client.py`, `_call_cpa_image_edit`, and `scripts/llm_via_cpa.sh`.
- Song-hint and title LLM stages use `scripts/llm_via_cpa.sh {prompt_file} {completion_file}` (CPA chat, default model `gpt-5.4-mini`).
- The AI cover chain calls CPA `images/edits` with `gpt-image-2` directly from publish staging; if it cannot run, staging fails closed (`BLOCKED_AI_COVER_REQUIRED`).

## Canonical validated song e2e command

This exact shape produced the accepted full-song package on 2026-07-03 (`reports/live-song-test/20260703-010424-room362064/OPEN_ME.md` — complete 《雨天》, LRC-timed sapphire burn, real CPA AI cover):

```bash
python3 scripts/run_full_session_selector_cpa_shadow.py \
  --source-video <capture>.mp4 \
  --source-srt <capture>.source.srt \
  --output-dir <report_dir>/full_selector_no_upload \
  --room-id <room_id> \
  --copy-draft-context \
  --max-candidates 4 \
  --cpa-command "python3 scripts/cpa_semantic_qa_llm.py --request {request_json} --response {response_json} --transport direct --model gpt-5.4-mini --api-base $CPA_BASE_URL --api-key-env CPA_API_KEY" \
  --lrc-provider netease \
  --burn-preview \
  --publish-staging \
  --song-hint-llm-command "bash scripts/llm_via_cpa.sh {prompt_file} {completion_file}" \
  --title-llm-command "bash scripts/llm_via_cpa.sh {prompt_file} {completion_file}"
```

- `--copy-draft-context` is correct when `<capture>.source.srt` already came from a real AGY transcription (e.g. `scripts/transcribe_live_song_via_agy.sh`, Gemini 3.5 Flash (High) on the `free` host); production jingting refinement instead runs agy inside the pipeline.
- **2026-07-03 additions** (validated on the 7/2 lidousha recording, `reports/lidousha-autoslice-20260702/full_selector_jingting_v2/`): add `--semantic-recall-llm-command "bash scripts/llm_via_cpa.sh {prompt_file} {completion_file}"` so candidate discovery is viewer-perspective semantic recall (stories, danmaku banter, memes — keyword lanes are fallback only), and use `--agy-ssh-host free` for in-pipeline jingting: it now chunks the context at cue gaps into ~5 min 1280p clips per agy call (whole-session inputs deterministically return empty output — see handoff 2026-07-03). The CPA judge also enforces the viewer-context check (`VIEWER_CONTEXT_INCOMPLETE` + auto window expansion retry).
- **Danmaku evidence** (2026-07-03 route decision): add `--danmaku-xml <capture dir>/sources/<segment>.xml` (blrec raw danmaku; scp it next to the source video first). Enables the burst recall hints, real danmaku in CPA viewer-context, and per-chunk danmaku hint lines for jingting. `--agy-ssh-host` also enables silero-VAD subtitle timing QA (provisioned at `free:/opt/bilive/vad/`; VAD is positive evidence only).
- Capture length matters: record long enough to contain a COMPLETE song (15 min captured 3 complete songs; a 90s probe can never pass the completeness gate).
- The song contract lives in `docs/workflows/lidousha-song-finished-package-workflow.md` §6/§6.1 and `.agent/skills/song-lyrics-timeline-aligner/SKILL.md` §8: LRC global-shift subtitle timeline, forced accurate re-encode, sapphire72 1080p ASS, real-CPA cover chain, fail-closed everywhere.

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
- `src/autoslice/cpa_semantic_review.py` was removed 2026-07-02 (plan P1.6): the response-only fallback never verified `request_sha256` binding. Jobs must provide `cpa_semantic_request_path` alongside the response, or the pipeline blocks with `CPA_SEMANTIC_QA_REQUEST_REQUIRED` (see `cleanup_manifests/local_legacy_cpa_review_module_cleanup_20260702.json`).
- `scripts/run_auto_review_shadow_pipeline.py` can apply `cpa_semantic_request_path` + `cpa_semantic_response_path` from the source-context job and BLOCK on terminology/CPA failures.

Still needed:

- ~~Fill in the real CPA command for `--cpa-command`~~ — filled and validated 2026-07-03 (see "Canonical validated song e2e command" above).
- Add or wire a full future-live runner that calls the selector, term normalizer, CPA script, and shadow pipeline in one command — `scripts/run_full_session_selector_cpa_shadow.py` now is that runner for the no-upload lane; a real Li Dousha future-live run on `free` is still pending.
- Run it on a Li Dousha livestream that occurs after this runbook is written (the 2026-07-03 validation used a non-李豆沙 singing room in no-upload mode).
