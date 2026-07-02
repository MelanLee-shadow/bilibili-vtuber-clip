# Critic review + plan to unattended (2026-07-01)

Scope: independent critic review of the whole local workspace (docs, src, scripts, tests, workflow/skill docs), followed by a consolidated plan. Six parallel reviewers + one adversarial verifier were run over the repo; every claim below was verified against the actual files.

## Verdict

The implementation is **on-goal, not offroad**. The goal (README/AGENTS/route doc): fully unattended, fail-closed auto-slicing for 李豆沙 on remote `free:/opt/bilive/app`, where candidates are anchors, boundaries come from source-context evidence, and nothing publishes without an `AUTO_UPLOAD` manifest + artifact hash gate. The code matches that architecture stage for stage: selector → source-context planner/executor → evidence → boundary/song resolvers → 5-state decision → shadow render/PTS QA → simulated publish gate. 134 hermetic tests pass, and fail-closed behavior is the strongest-tested theme.

What is *not* done is the last mile, deliberately: there is no real uploader (by design), no real CPA provider command (mock-local only), no recording-completion detector, no automatic LRC discovery/spectrogram generation, and monitoring still runs from the local Mac LaunchAgent. The 2026-06-30 spark docs already scoped these; this plan folds them into one ordered list.

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

P1 — close the fail-open holes (local, pure-logic, test-first; order matters because everything downstream trusts these gates):

1. Make CPA semantic QA required-by-default for song/live-source jobs (missing response ⇒ BLOCK reason code, not pass-through), keeping an explicit `cpa_optional=true` escape hatch for talk-only shadow runs.
2. Verify song proof instead of trusting `FULL_SONG_READY` strings: require `lyrics_alignment` provider/model/source fields plus an alignment-report path that exists and hashes, before scores are force-raised.
3. Wire real `release_ready` / `review_required_findings` from the jingting review-required markers into `to_candidate_review`.
4. Block (RETRY_INFRA) on malformed `source_sha256` instead of skipping integrity.
5. Split payoff scoring so the title cannot satisfy content payoff on its own.
6. Delete the dead code; collapse the legacy CPA loader onto the hash-binding module.

P2 — last mile to an honest unattended run (matches the 2026-06-30 spark commitments):

1. Real CPA command wired on `free` for `cpa_semantic_review.py --cpa-command` (contract is already locked; mock stays for tests).
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
```
