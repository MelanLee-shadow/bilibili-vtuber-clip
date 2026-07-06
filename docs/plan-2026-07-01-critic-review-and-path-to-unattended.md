# Critic review + plan to unattended (2026-07-01)

Scope: independent critic review of the whole local workspace (docs, src, scripts, tests, workflow/skill docs), followed by a consolidated plan. Six parallel reviewers + one adversarial verifier were run over the repo; every claim below was verified against the actual files.

## Verdict

The implementation is **on-goal, not offroad**. The goal (README/AGENTS/route doc): fully unattended, fail-closed auto-slicing for 李豆沙 on remote `free:/opt/bilive/app`, where candidates are anchors, boundaries come from source-context evidence, and nothing publishes without an `AUTO_UPLOAD` manifest + artifact hash gate. The code matches that architecture stage for stage: selector → source-context planner/executor → evidence → boundary/song resolvers → 5-state decision → shadow render/PTS QA → simulated publish gate. 134 hermetic tests pass, and fail-closed behavior is the strongest-tested theme.

What is *not* done is the last mile, deliberately: there is no real uploader (by design), ~~no real CPA provider command (mock-local only)~~ (real CPA judge landed 2026-07-03: `scripts/cpa_semantic_qa_llm.py`, see P2.1 below), no recording-completion detector, ~~no automatic LRC discovery~~ (netease LRC discovery + DP global-shift alignment landed and validated 2026-07-03) /no automatic spectrogram generation, and monitoring still runs from the local Mac LaunchAgent. The 2026-06-30 spark docs already scoped these; this plan folds them into one ordered list.

## Defects found by this review (and status)

Fixed in this pass:

- **Audit false-pass**: `scripts/audit_lidousha_review_package.py` returned `passed=true` on the explicitly invalidated `redone_gold_447_travel_meaning` package. The status check was exact-match `invalid_review_draft` (missing the extended `invalid_review_draft_*` statuses) and the `INVALID_REDO_REQUIRED.json` marker was ignored. Now both hard-block, with regression tests.
- **Stale gold sample**: `docs/workflows/lidousha-song-finished-package-workflow.md` §10 pointed at the invalidated 447 package as gold; it now points at `redone_fullsong_433_travel_meaning` and records the 447 invalidation as a named counter-example.
- **Workflow doc ↔ skill doc drift**: 14 verified inconsistencies reconciled (audit-enforced limits now stated as ≤2 lines / ≤18 chars; title `《…》` documented as audit-enforced, not preferred; `--play-res 1920x1080` required for the sapphire72 example; 1080p margins 60,60,40; helper script path disambiguated; agy coded default `Gemini 3.5 Flash (Low)` noted; lexicon-leak check added to the skill; `audio_analysis/` added to the package layout; audit JSON artifact produced via stdout redirect; package-status vocabulary cross-referenced).
- **Junk file at repo root**: shell-heredoc debris file removed (see `cleanup_manifests/local_stray_rootfile_cleanup_20260701.json`).

Open — fail-open holes inside otherwise fail-closed logic (P1 below):

1. `src/autoslice/review_evidence.py:121-122` — `to_candidate_review` hardcodes `release_ready=True, review_required_findings=()`, so those two BLOCK gates can never trip through this conversion.
2. `scripts/run_auto_review_shadow_pipeline.py:1121-1126,1144-1166` — trust-by-string: any job manifest that writes `"status": "FULL_SONG_READY"` / `"READY"` gets force-raised scores and stripped SONG/LYRICS gaps with no verification of the LRC/lyric-order proof the selector's reason codes demand.
3. `scripts/run_auto_review_shadow_pipeline.py:1195-1197` — duplicate similarity assumed `0.0` (`assume_unique_for_no_upload_shadow`); fail-open if this path ever feeds a real uploader.
4. `src/autoslice/source_context_executor.py:98` — malformed/missing `source_sha256` silently skips the source-integrity check instead of blocking.
5. `scripts/run_auto_review_shadow_pipeline.py:1310-1311` — CPA semantic QA is optional-by-omission: no `cpa_semantic_response_path` in the job ⇒ evidence passes through unchanged.
6. `src/autoslice/content_evidence.py:80` — payoff heuristic scores `title + all_text`, so a hooky title alone yields payoff 0.96.

Other open findings:

- `scripts/lidousha_slice_monitor.py` (978 lines) has **zero tests** despite guarding the never-publish invariant (upload-killer, recorder watchdog); `gemini_slice_jingting.py` is ~90% untested (`validate_same_timing`, `subtitle_review_findings`, fmp4 remux fallback).
- Dead code: `cpa_reason_codes_for_decision`, `required_evidence_gaps`, `TermOverride.target_for` variant machinery, vestigial `response_contract` lookup.
- Two CPA modules validate the same schema with different strictness; the legacy `cpa_semantic_review.py` path never checks `request_sha256` binding.
- `tmp/*.py` (9 one-off backtest drivers with hardcoded remote paths) are superseded by `scripts/run_full_session_selector_cpa_shadow.py`; clean via a cleanup manifest unless kept as 6/17-6/19 backtest provenance.
- The local workspace is not a git repo — the entire test/doc/cleanup discipline has no history or rollback. Recommend `git init` locally (remote stays the production repo) so regressions in docs/gates are diffable.

## Plan

P0 — hygiene (done in this session): audit invalidation gates + tests; doc reconciliation; junk-file cleanup; full test suite green.

P1 — close the fail-open holes (local, pure-logic, test-first; order matters because everything downstream trusts these gates). **All six done as of 2026-07-02:**

1. **Done 2026-07-02** — Make CPA semantic QA required-by-default for song/live-source jobs (missing response ⇒ BLOCK reason code, not pass-through), keeping an explicit `cpa_optional=true` escape hatch for talk-only shadow runs.
2. **Done 2026-07-02** — Verify song proof instead of trusting `FULL_SONG_READY` strings: `_verify_lyrics_alignment_proof` requires `lyrics_alignment` provider/model/source(+`external_lrc`) fields plus an `alignment_report_path` that exists and matches `alignment_report_sha256`, both for the 0.98 boundary raise and the score force-raise; a claim without proof surfaces as `SONG_PROOF_UNVERIFIED` BLOCK (merged last so a talk-boundary DROP cannot mask it).
3. **Done 2026-07-02** — `to_candidate_review` now takes the jingting review-required marker (`review_required=` kwarg); marker present ⇒ `release_ready` from marker (malformed ⇒ False, fail-closed) and findings wired, so the `JINGTING_REVIEW_REQUIRED` gate can trip. Both the review-package path and the live-source path read their marker.
4. **Done 2026-07-02** — Declared-but-malformed `source_sha256` now blocks RETRY_INFRA with `SOURCE_SHA256_MALFORMED` (absent stays allowed; executor records the actual hash). The executor test fixture's own malformed `sha256:source` was the proof of the hole — real hashes now.
5. **Done 2026-07-02** — Payoff scores only cue text; a hooky title no longer yields payoff 0.96 by itself.
6. **Done 2026-07-02** — Deleted `cpa_reason_codes_for_decision`, `required_evidence_gaps` (+ its private table), `TermOverride.target_for` variant machinery, vestigial `response_contract` lookup; removed `src/autoslice/cpa_semantic_review.py` entirely (cleanup manifest `local_legacy_cpa_review_module_cleanup_20260702.json`) — a response without its request artifact now blocks with `CPA_SEMANTIC_QA_REQUEST_REQUIRED` instead of skipping hash binding.

**Product-goal steering (Ivan, 2026-07-02): repair-first, fail-closed last.** The goal is high-quality unattended slices *produced*, not merely bad slices blocked. Before any BLOCK, the pipeline must spend its repair budget: incomplete boundary ⇒ re-expand window from source context and AUTO_RECUT; incomplete song ⇒ actively run external-LRC discovery + alignment (don't just report SONG_PARTIAL); terminology leak ⇒ auto-normalize via term_lexicon and re-verify; missing ASR/evidence ⇒ call the API again / generate the artifact (spectrogram, chunk probe). Only after repairs are exhausted may it BLOCK, and the report must list what was attempted. Slices must be interesting, context-complete (never cut mid-thought), song slices a complete song, subtitles burned and accurate, AI cover + title per workflow. Upload remains out of scope for now; the AUTO_UPLOAD hash gate stays as the final safety net.

**2026-07-02 real-live-room E2E test + first repair-first layer (done).** 李豆沙 was offline, so the test used a real live 虚拟Singer room (26730839, 宫心绘梨奈, singing+chat): 5 min captured off the live FLV, Groq `whisper-large-v3-turbo` ASR (via `free`'s key) → 61 cues, then the full unattended path. Findings and fixes, all verified against the real capture:

- **Selector recall was zero** on an arbitrary real stream — the setup markers are lidousha-specific. Added `select_fallback_session_candidates` (performance-run recall: contiguous long-cue runs, edge-trimmed of chat quips); the runner falls back to it and stamps `selector_stage`. On the real capture it surfaced both renditions of the performed song (28.7–175.4s, 203.2–284.4s).
- **Repair-first song completeness**: new `src/autoslice/song_repair.py` — before a song candidate can BLOCK, it attempts external-LRC discovery (pluggable provider; real NetEase API provider included, `--lrc-provider netease`), fuzzy-aligns LRC lines to ASR cues, verifies head+tail coverage, and on success writes a lyrics-alignment report whose sha256 satisfies the P1.2 proof gate — scores raise only on earned proof. Every attempt (including failures) is written to `<candidate>.song-repair.json` and into evidence metadata, so a BLOCK now says what was tried. On the real capture the NetEase search returned the wrong song (ASR lyrics too garbled), alignment matched 0% and the repair honestly declined — decision DROP with the full attempt trail. Next repair lever: audio fingerprinting / better query synthesis, since noisy ASR text search is weak.
- **Subtitle burn stage**: `--burn-preview` burns the recut's SRT into a `.burned.mp4` shadow preview (never published) with its own sha256; verified on the real capture with real ffmpeg (frame-checked, hanzi rendered).
- Test suite: 159 passed (`tests/test_song_repair.py`, fallback-recall test shaped like the real capture, pipeline-level repair-earns-proof / repair-fails-then-blocks tests, burn dry-run contract).

**2026-07-02 second pass — repair-first execution (P2.1 + P2.2 + zero-output fixes), all test-first, 181 tests green:**

1. **Real CPA judge wired (P2.1 done).** `src/autoslice/llm_client.py` (direct OpenAI-compatible + command-bridge transports; bridge keeps keys on `free` — `scripts/llm_via_free_groq.sh` pipes prompt→ssh→Groq→completion, smoke-tested live). `scripts/cpa_semantic_qa_llm.py` fills the locked response contract: LLM supplies only judgment fields; candidate_id/request_sha256/artifact paths are forced from the request artifact, so hash binding holds. Malformed judgments raise (fail-closed), not-ready-without-reasons is forced to a BLOCK code, scores clamp to [0,1]. Verified against real Groq (`llama-3.3-70b-versatile`) on the real capture: it correctly blocked a mediocre chat fragment with `CPA_SEMANTIC_INCOMPLETE`/`NOT_INTERESTING` and passed no false `TERMINOLOGY_QA_FAILED` after the terminology clause was scoped to content that actually involves the terms.
2. **Song identification upgraded (zero-output lever #1).** `song_repair` now: builds several text queries + accepts providers returning multiple LRC candidates (NetEase returns top-3 per query), **ranks candidates by actual lyric-to-ASR alignment ratio** instead of trusting search order, and takes an optional LLM hint step that guesses the song name from garbled ASR (`--song-hint-llm-command`). Every candidate's alignment percentage lands in the repair trail. On the real capture the machinery ran fully (LLM guessed, 7–10 candidates fetched and aligned per song) and still honestly refused — this particular 古风 cover is unidentifiable from 0–11% alignment; that's a data limit, not a silent give-up.
3. **Talk fallback recall (zero-output lever #2).** `select_fallback_session_candidates` now also emits talk candidates outside song runs (payoff/closure-bearing windows; no dead-air gaps >15s inside a window; no sub-1.2s orphan or connective starts). Song-run edge trim raised to 3s so lead-in banter isn't swallowed into the song span. On the real capture this surfaced the chat moment as a third candidate that flowed through the full gate chain.
4. **Recording-completion detector + daemon wiring (P2.2 done).** `evaluate_recording_completion`/`detect_recording_completion` in the shadow daemon: size-stability over a quiet window (mtime is untrustworthy on the cloud mount); `run_once` now skips `recording_in_progress` instead of slicing a growing file (`--force` bypasses). The daemon's no-prepared-slices route falls back to recall when the primary selector is empty and stamps `selector_stage` + glossary provenance (`glossary_path`/`glossary_sha256`) into job manifests (runner too).
5. **Publish staging (workflow parity, upload stays off).** `--publish-staging` mirrors production local_prepare: LLM title (falls back to job title on bridge failure), cover frame extraction, `*.publish.json` with hard-coded `upload_enabled: false` and artifact hashes.

**2026-07-02 third pass — 20-minute real-stream acceptance (room 10984778, 桃桃栀, gacha-chat session, 421 ASR cues via chunked Groq):**

- Two more zero-output defects found and fixed on real data: (a) dense chat chained into fake "singing runs" and starved talk recall — added `_looks_like_performance` (median cue ≥3s AND chat-particle fraction <35% must both hold); (b) **every** materialized recut blocked on `ACTUAL_CUT_ERROR_HIGH` ~1.1s because the precise re-render only ran for AUTO_UPLOAD boundaries, and even then ffmpeg's input-side `-ss` on live-captured TS lands after the target (no seek index). Fixes: re-render on any measured >100ms cut error, and two-stage seek (coarse input `-ss` 10s early + precise output-side `-ss`) — verified 1116ms → 9ms on the real TS.
- Final run: 4 talk candidates from the **primary** selector (this streamer's chat happens to contain setup markers), all mechanically green — cut error repaired, subtitles burned, LLM titles staged (e.g. 《男人你竟然敢拒绝我，你成功引起了我的注意哈哈哈》), covers extracted, `publish.json` drafts with `upload_enabled:false`. All four were then blocked **only** by the real CPA judge (`CPA_SEMANTIC_INCOMPLETE`/`CONTEXT_DEPENDENCY_HIGH`/`NOT_INTERESTING`) — an honest verdict on 20 minutes of viewer-gacha chatter. Judge discrimination was calibrated separately: a clearly-good complete-joke candidate gets `release_ready=true`, hook 0.9. Zero releasable output on mediocre talk content is the gates working, not the pipeline failing; the machinery to produce, repair, burn, and stage reviewable slices is now demonstrated end-to-end on real live data.
- **Ivan steering follow-up (2026-07-02): complete song slices are not “boring” content.** Once machine evidence proves a foreground full song (`foreground_song_overlap_seconds>5`, `song_complete=true`, `lyrics_alignment_ready=true`), CPA semantic reasons that only mean boring/chat-context/no-hook (`NOT_INTERESTING`, `CONTEXT_DEPENDENCY_HIGH`, generic `CPA_SEMANTIC_INCOMPLETE`/`CPA_RELEASE_NOT_READY`) are waived; terminology and unsafe-upload reasons still block. This keeps “complete songs must pass by form evidence” separate from the talk-clip interestingness gate.
- Remaining data-dependent milestone: a complete-song release on a real full-session recording (李豆沙's next stream — full sessions contain song head+tail, and her repertoire is LRC-discoverable; the proof machinery is validated in hermetic tests). Song identification for garbled ASR of niche covers still wants an audio-fingerprint lever.

P2 — last mile to an honest unattended run (matches the 2026-06-30 spark commitments, now under the repair-first principle above):

1. ~~Real CPA command wired for `cpa_semantic_review.py --cpa-command`~~ — **done 2026-07-03**: `scripts/cpa_semantic_qa_llm.py` direct transport against `$CPA_BASE_URL` (`gpt-5.4-mini`), validated in the room-362064 full-song e2e. Per Ivan, CPA stages (semantic QA, song-hint, title, `images.edit` cover) are real in workflow/e2e tests too; mock responders remain only in pytest unit tests. Canonical command: `docs/spark/2026-06-30-future-live-e2e-runbook.md` § "Canonical validated song e2e command".
2. Recording-completion detector + `no_prepared_slices` selector fallback hooks in the shadow daemon (`detect_recording_completion`, `run_full_live_selector_once`), plus glossary provenance (`glossary_path`/`glossary_sha256`) in job manifests.
3. Automatic external-LRC discovery/ingest + waveform/spectrogram artifact generation + Gemini chunk-probe artifact schema in `source_context_executor` (the remaining P1/P2 items in `docs/autoslice-capability-status.md`).
4. `future_live_summary.json` aggregate + the actual future-live no-upload E2E acceptance run on the next post-runbook livestream (acceptance: `AUTO_UPLOAD>=1` or explained fail-closed; `actual_cut_error_ms<=100`; lexicon-clean subtitles; zero upload processes).
5. Tests for `lidousha_slice_monitor.py` `evaluate()`/`count_live`/upload-kill decision and for `gemini_slice_jingting.py` gate helpers; fail-closed test for the selector-CPA runner with a broken responder.

P3 — deployment and controlled release (unchanged from the route doc, still gated on P2 evidence):

1. Move scheduling from the Mac LaunchAgent to remote systemd/timers (scan, local_prepare, jingting, shadow daemon).
2. Remote worktree read-only classification + cleanup manifest (never touch `cookie.json`/prod config blindly).
3. A real uploader that consumes an `AUTO_UPLOAD` manifest, re-verifies all six artifact sha256s on disk, and is idempotent (BV-keyed ledger) — then shadow → canary (1/场) → limited → full (≤10, 0 allowed), each step gated on accumulated shadow metrics.

Definition of done for the project remains the 2026-06-29 reset doc's: autonomous high-quality final slices from source-context evidence, upload behind the hash gate, with an independent acceptance review against the product goal — not merely "bad clips blocked".

## Verification

```bash
python3 -m compileall -q src scripts tests
python3 -m pytest tests -q   # 136 passed after this pass
# 2026-07-02 follow-up (P1.1): python3 -m pytest tests -q => 140 passed
# 2026-07-02 P1.2-P1.6 complete: python3 -m pytest tests -q => 149 passed
#   (144 after P1.2; +4 P1.3; +2 P1.4; +2 P1.5; +2 new CPA gate tests; -5 legacy
#    cpa_semantic_review tests removed with the module)
```
