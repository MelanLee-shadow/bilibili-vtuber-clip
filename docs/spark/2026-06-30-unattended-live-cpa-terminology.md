# 2026-06-30 Spark Spec: Unattended Li Dousha live slicing with terminology + CPA semantic QA

## Goal

Move vtuber-slice from no-upload proof-of-concept toward unattended live slicing for a future Li Dousha livestream:

1. Record a real future livestream from start to finish.
2. Generate or locate full-source media + full-source SRT.
3. Build candidate windows automatically from the full session.
4. Apply Li Dousha-specific terminology corrections before semantic review.
5. Ask CPA from a script, not from an interactive Hermes agent, to perform semantic QA and return a machine-readable JSON file.
6. Materialize preview recuts, run render QA, and decide `AUTO_UPLOAD` / `AUTO_RECUT` / `DROP` / `BLOCK` fail-closed.
7. Stop before real upload unless Ivan explicitly authorizes upload.

## Corrections from Ivan

- The phrase previously transcribed as `天不熊` is wrong.
- Correct subtitle/output term: `kmx`.
- `kimo熊` is only a developer alias / pronunciation hint for `kmx`; it must normalize to `kmx` and should not appear in final subtitles.
- Ivan-edited subtitles are authoritative terminology evidence.
- Current known edited subtitle source:
  - `lidousha/2026-06-19/315s_cry_expression/315s_manual_22966160_2026-06-19-19-30-01-cry-expression.manual-edited.zh.srt`
  - Contains examples:
    - `kmx, kmx，你看有花`
    - `kmx被骗到了`

## Non-goals / safety boundaries

- Do not use Kanban for this direct-takeover lane.
- Do not upload or publish to Bilibili without explicit Ivan authorization.
- Do not read or print cookies, credentials, tokens, or account secrets.
- Do not rely on Hermes being present during unattended runs; all external model calls must be script-callable.
- Do not treat unparseable CPA output as success.
- Do not treat unknown terminology as corrected unless backed by Ivan-edited subtitles or explicit lexicon entry.

## Architecture

### 1. Terminology library

Purpose: normalize Li Dousha-specific names, memes, and recurring stream words before semantic QA and optionally before title/cover generation.

Sources:

1. Ivan-edited SRT files matching `*.manual-edited*.srt`.
2. Optional explicit JSON/YAML lexicon checked into the repo or stored under a project report path.
3. Future manually approved CPA/semantic QA corrections.

Minimum schema:

```json
{
  "schema_version": "lidousha-terminology.v1",
  "entries": [
    {
      "canonical": "kmx",
      "aliases": ["kimo熊", "天不熊"],
      "kind": "name",
      "source": "manual-edited-srt",
      "evidence_paths": ["lidousha/2026-06-19/...manual-edited.zh.srt"],
      "notes": "Ivan correction: 天不熊 is wrong; kmx is pronounced/heard as kimo熊."
    }
  ]
}
```

Fail-closed rules:

- Terminology correction may annotate and normalize text for QA.
- It must not silently rewrite publish subtitles unless the correction is backed by lexicon evidence.
- Ambiguous replacements should emit `TERMINOLOGY_AMBIGUOUS` and block upload.

### 2. CPA semantic QA script

Purpose: ask CPA to judge whether a candidate is semantically complete, context-safe, title-worthy, and terminology-correct.

This must be runnable by scripts during unattended mode, not by the interactive Hermes session.

Request artifact:

```json
{
  "schema_version": "cpa-semantic-review-request.v1",
  "candidate_id": "...",
  "room_id": "22966160",
  "source": {
    "video_path": "...",
    "srt_path": "...",
    "start_ms": 0,
    "end_ms": 0
  },
  "candidate_text": "...",
  "normalized_text": "...",
  "terminology": {
    "schema_version": "lidousha-terminology.v1",
    "applied_terms": ["kmx"]
  },
  "checks_requested": [
    "semantic_completeness",
    "context_dependency",
    "terminology_correctness",
    "title_hook_quality",
    "unsafe_upload_risk"
  ]
}
```

Response artifact:

```json
{
  "schema_version": "cpa-semantic-review-response.v1",
  "candidate_id": "...",
  "release_ready": true,
  "semantic_complete": true,
  "terminology_ok": true,
  "title_hook_score": 0.0,
  "context_dependency_score": 0.0,
  "reason_codes": [],
  "required_fixes": [],
  "evidence": {
    "summary": "...",
    "terminology_findings": []
  }
}
```

Fail-closed rules:

- Missing response file -> `CPA_SEMANTIC_QA_MISSING`.
- Invalid JSON -> `CPA_SEMANTIC_QA_INVALID_JSON`.
- Schema mismatch -> `CPA_SEMANTIC_QA_SCHEMA_MISMATCH`.
- Candidate id mismatch -> `CPA_SEMANTIC_QA_CANDIDATE_MISMATCH`.
- `release_ready=false` or non-empty reason codes -> merge into auto-review reason codes.
- CPA response is advisory for semantic gates, not a bypass for render/source/upload gates.

### 3. Review evidence integration

CPA semantic QA should become a check in `ReviewEvidence.checks` and machine-readable metadata in `ReviewEvidence.metadata`.

Suggested check:

```json
{
  "code": "CPA_SEMANTIC_QA",
  "pass": true,
  "severity": "PASS",
  "evidence": {
    "response_path": "...",
    "semantic_complete": true,
    "terminology_ok": true,
    "title_hook_score": 0.85
  }
}
```

Decision impact:

- `semantic_complete=false` -> `OPEN_LOOPS_PRESENT` or `CPA_SEMANTIC_INCOMPLETE`.
- `terminology_ok=false` -> `TERMINOLOGY_QA_FAILED`.
- `context_dependency_score` too high -> `CONTEXT_DEPENDENCY_HIGH`.
- unparseable/missing CPA -> `BLOCK` / fail-closed.

### 4. Future-live unattended test flow

1. Pre-live setup:
   - Confirm recorder is running for room `22966160`.
   - Confirm expected output root and free disk.
   - Confirm no uploader process.
   - Confirm terminology library exists and includes `kmx` correction.
2. During live:
   - Record normally.
   - Do not upload.
   - Optional: collect danmaku/chat but do not rely on it for final claims.
3. After live ends:
   - Detect recording complete and source file stable.
   - Generate or locate full-source SRT.
   - Run source integrity ledger.
   - If gaps exist, attempt official/replacement source only through existing safe paths; otherwise block.
4. Candidate selection:
   - Use `full_session_candidate_selector` to find setup -> payoff -> closure windows.
   - Exclude song-like windows unless song workflow is explicitly selected.
5. Terminology normalization:
   - Load Ivan-edited subtitle-derived lexicon.
   - Normalize candidate text for QA and record applied terms.
6. CPA semantic QA:
   - Write request JSON.
   - Call CPA script/CLI with a strict JSON-only prompt/contract.
   - Parse response JSON; fail closed if invalid.
7. Source-context review + recut:
   - Run live-source pipeline.
   - Materialize no-upload preview for viable candidates.
   - Accurate re-render if stream-copy cut drift exceeds threshold.
   - Persist artifact hashes and render QA.
8. Final no-upload acceptance:
   - `AUTO_UPLOAD` allowed only when all gates pass.
   - No real upload unless Ivan explicitly enables it.

## First implementation slice

1. Add terminology library module + tests:
   - parse manual-edited SRT
   - load explicit `kmx` correction
   - normalize `天不熊` -> `kmx` in candidate QA text
2. Add CPA semantic QA module + tests:
   - validate request/response schema
   - write/read response JSON
   - convert CPA result into ReviewEvidence check/reason codes
3. Add future-live direct-run playbook/script skeleton:
   - no real external request in tests
   - no upload
   - clear artifact paths and failure reasons

## Verification strategy

- Unit tests for terminology extraction and normalization.
- Unit tests for CPA JSON validation fail-closed cases.
- Integration test: candidate text containing `天不熊` becomes QA-normalized to `kmx` and carries terminology evidence.
- Integration test: invalid/missing CPA JSON blocks or reason-codes the candidate.
- Remote/container smoke only after local tests pass.
- Future live test must be performed on a livestream that occurs after this spec, from recording start through final no-upload slicing output.
