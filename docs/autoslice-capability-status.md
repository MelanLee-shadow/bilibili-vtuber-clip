# Auto-slice capability status

Updated: 2026-07-01T03:10:37Z

Scope: no-upload selector/shadow/review automation. Public upload remains fail-closed and still requires an explicit `AUTO_UPLOAD` decision manifest plus artifact-hash gate.

## Auto song slicing: selector + shadow path complete for no-upload

Status: `selector_and_shadow_complete_no_upload`

The pipeline now treats song candidates as source-context anchors, not final clip boundaries.

Implemented front door:

- Full-session song-like windows are emitted as `content_type_hint = song` candidates instead of being filtered out.
- Song candidates carry `requires_full_source_song_boundary_redo = true`.
- Song source-context jobs cover the full source instead of only a local pre/post window.
- Song anchors are marked `AUTO_RECUT` with `SONG_BOUNDARY_REDO_REQUIRED` / `SONG_FULL_SOURCE_REQUIRED` until full proof is available.

Required machine evidence before a song can be treated as complete:

- `song_boundary.status = FULL_SONG_READY`
- `lyrics_alignment.status = READY`
- external timed lyric source or equivalent recorded
- first/last lyric anchors recorded
- spectrogram/waveform and/or `agy` `Gemini 3.5 Flash` alignment evidence recorded
- final SRT/ASS, cover workflow, render/audit evidence recorded before any finished/gold status

Behavior:

- If a song-like full-session window is found, selector emits a song anchor rather than dropping it.
- If a song anchor starts in the middle of a song and full-song proof is present, auto-review emits an `AUTO_RECUT` plan to the full-song range.
- If full-song boundary or lyrics alignment proof is missing, song candidates remain fail-closed as BLOCK/DROP/AUTO_RECUT-required; they do not become publishable.
- Fixing subtitles inside a truncated 60-90s candidate is explicitly not enough.

Code paths:

- `src/autoslice/full_session_candidate_selector.py::select_full_session_candidates`
- `src/autoslice/full_session_candidate_selector.py::FullSessionCandidate.to_source_context_job`
- `scripts/run_auto_review_shadow_pipeline.py::_resolve_song_boundary`
- `scripts/run_auto_review_shadow_pipeline.py::_apply_live_source_machine_evidence`
- `scripts/run_auto_review_shadow_pipeline.py::_recut_plan_record`

Regression tests:

- `tests/test_full_session_candidate_selector.py::test_song_like_window_is_emitted_as_song_anchor_not_filtered_out`
- `tests/test_full_session_candidate_selector.py::test_filters_open_loops_and_overlapping_duplicates_while_classifying_song_windows`
- `tests/test_auto_review_shadow_pipeline.py::test_live_source_song_window_blocks_without_full_song_proof`
- `tests/test_auto_review_shadow_pipeline.py::test_live_source_song_window_auto_recuts_to_full_song_boundary_when_alignment_proof_present`

Still not claimed complete; these remain fail-closed P1/P2 work:

- automatic external LRC discovery/ingest in `source_context_executor`
- automatic waveform/spectrogram artifact generation for song redo jobs
- automatic Gemini 3.5 Flash chunk-probe artifact schema from executor
- review-package audit expansion for spectrogram/waveform/probe/approved-cover-style finished gates

## Auto talk slicing: full-session fallback connected

Status: `full_session_selector_fallback_connected_no_upload_shadow`

The daemon no longer stops at `no_prepared_slices` when full source media plus SRT exists. It now runs the full-session selector, selects setup/payoff/closure talk candidates, and routes them through the live-source shadow pipeline.

Code paths:

- `scripts/lidousha_auto_review_shadow_daemon.py::_full_session_live_source_routes`
- `src/autoslice/full_session_candidate_selector.py::select_full_session_candidates`
- `scripts/run_auto_review_shadow_pipeline.py::_resolve_live_source_boundary`

Regression tests:

- `tests/test_lidousha_auto_review_shadow_daemon.py::test_run_once_uses_full_session_selector_when_no_prepared_slices_exist`
- `tests/test_full_session_candidate_selector.py`
- `tests/test_auto_review_shadow_pipeline.py::test_live_source_dialogue_boundary_auto_recuts_and_records_source_context_metadata`

## Still fail-closed

- This does not enable upload.
- `.jingting.done` still does not mean release-ready.
- Deterministic frame covers remain review fallback only, not finished publish artifacts.
- Candidate anchors are still not final boundaries; boundary resolver or song boundary evidence must decide final start/end.
