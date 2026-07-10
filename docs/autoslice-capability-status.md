# Auto-slice capability status

Updated: 2026-07-10 (AGY v4 + host-vocal-proof.v2 joint gate deployed and accepted no-upload at `f64cd29`; cron restored)

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

## Auto song slicing: LRC completion + joint live-performance/host-vocal gate

Status: `joint_live_performance_plus_lidousha_vocal_gate_deployed_guarded_acceptance_no_upload`

Song candidates remain source-context anchors, not final clip boundaries. When `free_session_autoslice.py` has already put a window in the song lane, tight/core/full attempts now retain that upstream seed instead of asking a second nondeterministic semantic-recall pass to identify a song from sparse or garbled Japanese singing ASR. Only the expanded full retry may invoke current-audio + canonical-LRC proof.

Implemented front door:

- `--lrc-provider auto` searches NetEase, LRCLIB, and Kugou; clean quoted song titles and Japanese kana are valid queries. Google/public-web search remains a manual discovery route, not an unattended provider or proof source.
- Manual recovery may use Google/public-web search for a credible timed LRC. Japanese language and sparse/garbled singing ASR are discovery inputs, not terminal failure reasons.
- Same-song provider variants are grouped by normalized title+artist or identical full-LRC fingerprint. Audio escalation requires one sufficiently supported identity; different-song ambiguity blocks.
- Tight/core/full attempts receive `--seed-song-candidate-id` and clip-local seed bounds. A seed preserves recall only; it never mints completeness evidence.
- The expanded full retry can use sandboxed `agy` `Gemini 3.5 Flash (High)` against the current media and selected canonical LRC.

Required machine evidence before a song can be treated as complete:

- `song_boundary.status = FULL_SONG_READY` and `lyrics_alignment.status = READY`.
- Current media, canonical LRC, prompt, raw observation and run manifest are SHA-256 bound.
- Every canonical LRC line is affirmatively heard at confidence `>=0.8`; starts are monotonic, adjacent overlap is bounded, and one global shift explains the whole performance without unproved stretch.
- First line, chorus, repeated section, longest instrumental gap and tail are explicitly checked, with the first post-song talk boundary recorded.
- AGY v4 is a hard same-subject live-performance veto, separate from lyric alignment. It requires `mode=LIVE_STREAMER_SINGING`, confidence `>=0.85`, `continuous_live_song_performance=true`, `same_lidousha_live_performer_across_all_lyrics=true`, background-recording likelihood `<=0.20`, and exactly three evidence timestamps in lyric head/middle/tail; all three timestamps must fall inside canonical rows classified as `LIDOUSHA + SINGING_THIS_LYRIC`. The first and final canonical rows must be sung, at least seven rows and at least 80% of all rows must be sung, and any same-performer theatrical dialogue inside the song is allowed only as one contiguous `PERFORMING_THIS_LYRIC_SPOKEN` block of at most six rows, 12 seconds summed voiced duration, 20% of total canonical voiced duration, and 15 seconds wall-clock span. Every row still requires the same live Li Dousha vocal source and forbids another singer/harmony or recorded/playback vocal. Guest/duet/harmony, original/background playback, offscreen/static replay, another singer, ordinary speech over music, ambiguity, multiple/oversized spoken blocks, missing fields, or raw/report disagreement blocks.
- CAM++ then makes the narrower `LIDOUSHA_VOCAL_PRESENT_ON_LYRIC_CHECKPOINTS` subclaim in `host-vocal-proof.v2`. A 4–8s post-song speech anchor must first match the three pinned Li Dousha enrollments at median `>=0.60`. Seven distinct actual aligned **singing** cues of duration `>=2.5s` are sampled for 2.5–4s; spoken-theatrical rows are never eligible checkpoints. Each pass requires both the three-enrollment median `>=0.31` and same-session-anchor score `>=0.31`; at least 5/7 and head/middle/tail coverage are required.
- Only the AGY-live AND CAM++-identity result is named `VERIFIED_LIDOUSHA_SINGING`. A CAM++ match alone is not a singing classifier and cannot override AGY's background/speech-over-music veto.
- The runner verifies source/alignment/profile/model/reference/session-anchor/checkpoint hashes, rejects reused decoded PCM across checkpoints, recomputes recorded medians/thresholds/bucket coverage, and compares the AGY v4 raw/report row-level subject, role, same-source, other-singer and recorded/playback assertions. `host-vocal-proof.v2` binds the required `SINGING_THIS_LYRIC` role plus all five assertions to every checkpoint, so a spoken row or assertion drift cannot satisfy CAM++. The verifier does not rerun CAM++ inference; this is fail-closed artifact verification, not a second ML opinion or a formal identity proof.
- The materialized recut uses `subtitle_source=external_lrc_global_shift`, accurate re-rendering, passing render QA and current SRT/burned-video hashes.

Corrected negative acceptance (2026-07-10):

- `song_223019_166_mebukutoki_rerun_v4` / yonige《芽吹くとき》 is **superseded and rejected**, not an accepted Li Dousha song clip. Its 25/25 LRC alignment proved only that the studio recording played; the video is a static goodbye/end card and Ivan confirmed Li Dousha was not singing.
- This false positive is the negative golden case. In the historical pre-deploy calibration probe, it scored `0/7` above the pinned CAM++ threshold (`0.31`), while a confirmed live Li Dousha sample scored `7/7` against enrollments and `5/7` against its same-session host anchor. Those calibration counts are not the current v5 acceptance result, which is the separately recorded `6/7`. A synthetic loud Li-Dousha-speech-over-BGM mix could still make CAM++ pass `6/7`, which is why the independent AGY live-performance veto is mandatory.
- Its active delivery files are quarantined under `_superseded`; the six-file incident repair is `COMMITTED` and records `blocked` / `SONG_BACKGROUND_PLAYBACK_ONLY`. No upload occurred.

The old run still proves only the Japanese sparse-ASR **LRC recovery** path. It does not prove a live performer. The current joint gate is deployed from `f64cd29494fdc0d2b37d249e659897514fd701dc` and has completed guarded no-upload acceptance. The real-singing《屑屑》v5 run reached `READY` / `MATERIALIZED` (52/52 heard; 48 sung plus one four-row bounded theatrical-spoken block; host-vocal proof 6/7 with all checkpoints bound to sung rows). The same decoded audio under a static replay picture was blocked by AGY's playback mode. The fresh current《芽吹くとき》run automatically recovered the correct LRCLIB Japanese LRC and 25/25 timing, but its AGY observation overcalled the playback as live; the independent host-vocal proof scored 0/7 and the final joint gate correctly returned `BLOCK / SONG_NOT_LIDOUSHA_SINGING`, with no recut. This observed single-model error is why neither AGY nor CAM++ is sufficient alone. At `2026-07-10 10:50:29Z` the exact cron entrypoint smoke returned 0 with state, summaries, and the 32,015-byte upload ledger unchanged; the runner lock was free and `DISABLED` was removed. The real `*/10` scheduler then ran naturally at `11:00:02Z` and logged `tick done` with `live=False` and `2026-07-09:review_ready`. A future song becomes deliverable only when both independent gates pass. Missing synchronized lyrics, ambiguous identity/version, malformed/hash-unbound evidence, CAM++ runtime/reference drift, no/short post-song host anchor, no Li Dousha vocal, or failed render proof remains fail closed. Guest/other-singer ROC coverage is still incomplete. Nothing here enables upload.

Code paths:

- `scripts/free_session_autoslice.py::produce_song`
- `scripts/run_full_session_selector_cpa_shadow.py::_seeded_song_candidate`
- `src/autoslice/song_repair.py::attempt_song_repair`
- `src/autoslice/agy_lrc_alignment.py::run_agy_audio_lrc_alignment`
- `src/autoslice/host_vocal_proof.py::generate_host_vocal_proof`
- `src/autoslice/host_vocal_proof.py::verify_host_vocal_proof_claim`
- `scripts/run_auto_review_shadow_pipeline.py::_resolve_song_boundary`
- `scripts/run_auto_review_shadow_pipeline.py::_apply_live_source_machine_evidence`

Regression tests:

- `tests/test_full_session_selector_cpa_shadow_runner.py::test_seeded_song_cli_bypasses_empty_semantic_recall`
- `tests/test_free_session_autoslice.py::test_full_song_proof_retry_seeds_original_anchor_and_enables_audio_lrc`
- `tests/test_song_repair.py::test_sparse_japanese_asr_escalates_current_audio_and_mints_bound_proof`
- `tests/test_song_repair.py::test_audio_lrc_alignment_mutations_fail_closed`
- `tests/test_auto_review_shadow_pipeline.py::test_live_source_song_window_blocks_without_full_song_proof`
- `tests/test_auto_review_shadow_pipeline.py::test_live_source_song_window_auto_recuts_to_full_song_boundary_when_alignment_proof_present`

Still not claimed complete:

- automatic success for Japanese songs without a reliable unique synchronized-LRC identity
- exhaustive real-media guest/duet/other-singer ROC calibration or a formal performer proof
- exhaustive repeated-run variance calibration for AGY video/audio live-vs-playback classification; the observed《芽吹くとき》AGY false positive is contained by the mandatory CAM++ AND, not erased from the evidence
- automatic waveform/spectrogram artifact generation for song redo jobs
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
