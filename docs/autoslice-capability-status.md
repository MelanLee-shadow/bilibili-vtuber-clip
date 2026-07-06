# Auto-slice capability status

Updated: 2026-07-04 (aggregate-ASR subtitle substrate + CPA text-only correction)

Scope: no-upload selector/shadow/review automation. Public upload remains fail-closed and still requires an explicit `AUTO_UPLOAD` decision manifest plus artifact-hash gate.

## Subtitle substrate = free aggregate ASR + CPA correction (2026-07-04, Ivan decision)

Status: `aggregate_asr_timeline_plus_cpa_text_correction`

The subtitle timeline+draft no longer comes from an LLM listening to audio. The free aggregate ASR (`scripts/free_asr_client.py`: bcut 必剪 primary, jianying 剪映 backup; kuaishou removed — B站 AI-subtitle-同源引擎, no login) returns **sentence-level millisecond boundaries in seconds** (30 min → ~17s). That owns the timeline and the draft text. Correction is then a pure TEXT task, so it goes to **CPA**, not agy:

- `_build_aggregate_asr_transcriber` (`run_full_session_selector_cpa_shadow.py`): aggregate ASR draft → `_cpa_correct_draft_cues` (CPA gpt-5.4-mini corrects proper nouns / memes / homophones from `assets/lidousha/glossary.txt` + time-paired danmaku). The LLM only sees/returns numbered cue **texts**; corrected texts are spliced back onto the ASR timestamps, so **timeline preservation is structural**, not a validation afterthought.
- **agy's former job (listen to audio, invent a 1–2s-grid transcript) is gone.** agy is available only as a legacy `--correct agy` multimodal refine (also reads on-screen frames; slower) or the `agy_fresh` fallback substrate for a total ASR outage.
- Fail-open: a correction outage ships the accurate raw ASR draft (proper nouns uncorrected) rather than failing the clip; a total ASR outage raises so the caller can fall back.
- **Song clips unchanged**: aggregate ASR is sparse/error-prone on singing (B站 also special-cases music rows); song subtitles keep the LRC global-shift flow, aggregate ASR only a reference track.
- Driver: `scripts/produce_slice_package.py --substrate aggregate_asr --correct cpa` (default). Why CPA over agy: correction is text; CPA is the pipeline's existing judge, one fast call vs chunked-agy-on-free, and splice-back guarantees the timeline. agy's only unique value (reading pixels) narrows to optional on-screen-text extraction, later replaceable by OCR.

## Semantic recall + viewer-context QA (primary discovery lane, 2026-07-03)

Status: `semantic_recall_primary_keyword_fallback`

Ivan's rule (2026-07-03): interestingness and context completeness are semantic judgments — keyword matching cannot be the gate. Funny moments include danmaku-triggered banter and on-screen reactions, not just storytelling; every candidate must be checked for missing context from a viewer's perspective.

- `src/autoslice/semantic_candidate_selector.py` (`--semantic-recall-llm-command`): viewer-perspective LLM scans the whole-session SRT for candidate windows (stories / danmaku banter / memes / songs) and must pull each window's context trigger inside it. Keyword `primary`/`fallback_recall` lanes remain only as LLM-outage fallbacks.
- CPA judge (`scripts/cpa_semantic_qa_llm.py`) now receives ±90s of out-of-window transcript (`surrounding_context`) and must answer `viewer_context_ok` plus expansion-suggestion milliseconds. A clip a viewer cannot follow (and cannot reasonably infer) blocks with `VIEWER_CONTEXT_INCOMPLETE`; the selector auto-expands the window once (`*_ctxexp`) and re-reviews. Proven complete songs waive this code (unchanged product rule).
- Semantic-lane candidates carry `boundary_authority=semantic`: the keyword boundary resolver's verdict is demoted to `ADVISORY_*` codes (no DROP, no window rewrite), and keyword-derived payoff/standalone/editorial scores are superseded by the CPA verdict (`_apply_semantic_authority_evidence`). Machine-verifiable gates (timing alignment, cut error, duplicates, song proof, jingting provenance, upload risk) keep full authority.

## Temporal pairing + screen-text track + island snap (2026-07-04)

Status: `time_paired_visual_evidence_and_island_start_snap`

Ivan's rule: on-screen text and danmaku are TIME-PAIRED subtitle evidence —
text visible at time T is a candidate for the words near T (she reads what
appears the moment it appears); text far from T is NOT a candidate.

- Fresh transcription now runs a dedicated screen-text extraction pass first
  (agy watches the finished clip and emits `*.screen_text.json`: mm:ss +
  exact text + kind, excluding rolling danmaku), then transcribes with BOTH
  timed tracks (screen text + danmaku) under an explicit TEMPORAL PAIRING
  RULE (~10s = strong candidate, >20s = not a candidate). The jingting chunk
  refine prompt carries the same rule.
- Timing QA island snap upgraded: ANY cue with VAD speech islands gets its
  start pulled to the first island (and overhanging end pulled back) past the
  slack — fixes the "字幕挂了半天，人最后几秒才开口" defect. Cues with no
  islands stay untouched (low-recall VAD cannot testify to absence).
- Title style is now a repo skill: `.agent/skills/lidousha-title-style/SKILL.md`
  (85+ harvested production titles, structure taxonomy, hard rule #0: an
  Ivan-given title is final and must not be rewritten).

## Li Dousha knowledge assets + fresh-transcription finals (2026-07-03 evening)

Status: `lidousha_assets_vendored_and_fresh_transcription_for_talk_finals`

Ivan's review of the first finished clips set four rules, now implemented:

- **Knowledge assets** (`assets/lidousha/`, repo = source of truth, push with
  `scripts/sync_lidousha_assets.sh`): `glossary.txt` (term-learning loop: Ivan
  corrects a name → edit here → sync; added 小室/Ado), `title_style.md`
  (harvested production titles + rules), `persona.md` (traits shared by
  title/CPA/cover). **Bug fixed**: the local pipeline's `glossary()` only knew
  free-host paths, so every local jingting ran with an EMPTY glossary — the
  repo path is now first.
- **Titles are Lidousha-first**: viewers click for HER, not the content. The
  title prompt now injects persona + style rules + real historical titles as
  few-shot (e.g. "反沙，不是反李豆沙！"); content is supporting material only.
- **Cover composition**: Li Dousha owns the visual center — face/upper body
  unobstructed, the local title overlay (lower-center) may cover her lower
  body, supporting characters stay secondary. The old "clean empty space"
  prompt wording (which shoved her aside) is gone.
- **Fresh-transcription finals** (talk only): the finished clip's subtitle no
  longer inherits the integer-second production ASR. After the accurate
  re-render, the final media is re-transcribed from scratch (agy Gemini 3.5
  Flash High, glossary + danmaku lines + on-screen text reading, accurate
  per-utterance timing), validated (`_fresh_srt_to_source_cues`: cue count,
  monotonic, within clip), VAD-sanitized, and becomes
  `subtitle_source=fresh_agy_transcription`. Transcriber failure falls back to
  the sanitized ASR cues with a recorded `FAILED_FALLBACK_ASR_CUES`.

## Danmaku + on-screen text evidence chain (2026-07-03, Ivan-approved route)

Status: `danmaku_lane_and_visual_hints_wired`

Ivan's route decision: the stream frame carries rolling danmaku and other
on-screen text (image captions, UI), and most of Li Dousha's speech reacts to
on-screen content or reads danmaku aloud — so both are first-class subtitle
hints and recall signals. The heavy lifting deliberately does NOT run on
`free` (too weak): Gemini reads the frames during jingting, and danmaku text
comes deterministically from blrec raw XML (no OCR needed).

Wiring (`--danmaku-xml <date>/sources/<segment>.xml`, blrec `save_raw_danmaku=true`):

- **Burst recall lane** (`src/autoslice/danmaku_evidence.py`): 30s-bucket
  density vs session median baseline → burst windows (+sample texts) injected
  into the semantic recall prompt as priority zones. 7/2 backtest: the shipped
  clip sat in a burst; the missed 反沙绕口令 meme was the session's biggest
  burst (x12) — this lane closes exactly that gap.
- **CPA viewer-context evidence**: real danmaku inside the candidate window
  (`*.danmaku.json` → request metadata `danmaku_context`) — the judge now sees
  what viewers saw, so "弹幕起头在切片内吗" is judged on ground truth instead
  of guessing from the streamer's paraphrase.
- **Jingting hints**: each ~5min chunk's danmaku lines go into the agy prompt,
  and the prompt instructs Gemini to READ on-screen text (rolling danmaku,
  captions, UI) as correction evidence for names/memes/homophones — bounded by
  the no-invention rule (on-screen text justifies a correction only when it
  matches the audio).

Future (roadmap, not yet built): a dedicated OCR pass (e.g. PaddleOCR on the
Mac) extracting non-danmaku on-screen text into CPA/content evidence, and
danmaku-burst-only candidates as a recall lane when the LLM lane is down.

## Subtitle timing QA for talk recuts (2026-07-03, ASR-orchestra lessons)

Status: `talk_recut_timing_qa_vad_positive_evidence_only`

Ivan's 7/2 clip review found shipped subtitle-timing defects: a 24s trailing "好漂亮" (whisper stuck-segment hallucination straight from the coarse integer-second source ASR), 1s flash cues, and cues hanging over non-speech. Fix (`src/autoslice/subtitle_timing_qa.py`, wired into `_materialize_recut_record` for `subtitle_source=asr_cues` only — the LRC song path is untouched):

- silero VAD (onnxruntime on `free`, `scripts/free_silero_vad_spans.py` → `/opt/bilive/vad/`; v5 needs 64-sample context per chunk or it silently outputs zeros) provides speech spans.
- **VAD is positive evidence only**: ground-truthing (agy listening audit) proved loud-BGM speech, screams, and laughter score ~0 — real content that must never be dropped on VAD silence alone.
- The one deletion rule: whisper stuck-segment = duplicate-of-recent-text + duration ≥ hard max (12s) + no VAD support.
- Long low-density cues (>6s, <0.8 chars/s) retime to their VAD speech islands (else clamp); strong-support cues snap overhanging edges; flash cues extend to ≥1s bounded by the next cue.
- Every action lands in `<recut>.timing_qa.json` + the recut manifest; VAD outage is recorded (`SUBTITLE_TIMING_QA_UNAVAILABLE`), never silent.
- Jingting prompt hardened: unclear/music-masked audio keeps the draft text — no wholesale line rewrites from guesswork (the "吸铁石→喜鹊" hallucination class).

## Chunked jingting second-listen (2026-07-03)

Status: `chunked_agy_refine_default_for_ssh_runner`

Whole-session contexts (30 min / 1.2GB) deterministically return empty output from agy gemini-3.5-flash (rc=0, zero stdout/stderr, no output.srt — reproduced twice on the 7/2 job; known bug family antigravity-cli#76 / gemini-cli#24290). The ssh runner (`--agy-ssh-host`) now:

- splits the context draft at cue gaps into ~5 min chunks (`src/autoslice/jingting_chunker.py`; practitioner sweet spot for transcription accuracy),
- re-encodes each chunk to 1280p/crf28 (~50MB — the proven-good input profile) before upload,
- runs one agy call per chunk under a pseudo-TTY (`script -qec`), validates per-chunk timing, merges texts back onto the untouched draft timeline, re-validates,
- retries each chunk at most once, and classifies failures as `AGY_EMPTY_OUTPUT` / `AGY_TIMEOUT` / `AGY_FAILED_RC` (`AgyRunnerError`) so the review evidence names the real failure.

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

- ~~automatic external LRC discovery/ingest~~ — done 2026-07-03 in the repair-first stage (`src/autoslice/song_repair.py`: netease provider + LLM song-hint + monotonic DP alignment + single-global-shift completeness gate; validated on a real live full song, see `reports/live-song-test/20260703-010424-room362064/OPEN_ME.md`). Song lyric timing contract: burned timeline is LRC + verified global shift (`subtitle_source=external_lrc_global_shift`), song recuts always accurately re-encoded.
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
