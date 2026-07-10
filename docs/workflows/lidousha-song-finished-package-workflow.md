# Li Dousha song/collab finished package workflow

This is the durable workflow for 李豆沙 song/collab clips in `vtuber-slice`. It exists because a 2026-06-29 package was incorrectly promoted from auto-slice/Jingting artifacts into a "finished" package without running the song subtitle and AI-cover gates.

## Non-negotiable principle

Do not treat packaging as production.

A package is only a browse layer over artifacts that already passed the real workflow. Existing `.flv`, `.jingting.srt`, `.cover.png`, and `.publish.json` files are candidates, not finished evidence.

## 1. Candidate classification

For every candidate, classify before rendering:

- `song` / `collab_song`: 李豆沙本人必须在现场连续演唱；`collab_song` 可同时有合唱者，但“只有其他人在唱”不算李豆沙歌切。只播放原唱、片尾曲、下播卡音乐、游戏/视频 BGM，或李豆沙只在音乐上说话，一律不是歌切。
- `mixed_song_talk`: a song section plus a talk/accident reaction section.
- `talk`: dialogue / reaction clip without foreground lyrics.
- `reject` / `block`: insufficient context, bad source, duplicate, or unsafe to publish.

These are manual review classes. The pipeline `content_type_hint` (`src/autoslice/full_session_candidate_selector.py`) only emits `song` / `talk`; `collab_song` and `mixed_song_talk` map to `song` for pipeline purposes.

Song and mixed song candidates must not use ordinary ASR/Jingting timing as final lyric timing.

## 2. Subtitle workflow for song / mixed song

Use `.agent/skills/song-lyrics-timeline-aligner/SKILL.md`.

Required evidence:

- Joint performer proof before `song_complete`, recut delivery, cover work, or package materialization (LRC discovery/alignment may run first because the gate consumes actual lyric cues):
  - AGY v2 hard veto: `mode=LIVE_STREAMER_SINGING`, confidence `>=0.85`, `continuous_singing=true`, `background_recording_likelihood<=0.20`, and exactly three specific observations across lyric head/middle/tail.
  - CAM++ identity subclaim: `host_vocal_proof.status=READY`, `decision=LIDOUSHA_VOCAL_PRESENT_ON_LYRIC_CHECKPOINTS`; its source/alignment/profile/model/reference/session-anchor/checkpoint hashes and recorded medians/thresholds must verify.
  - Only their AND result may be `joint_singing_decision=VERIFIED_LIDOUSHA_SINGING`. LRC presence, AGY alone, CAM++ alone, or an LLM performer label is insufficient.
- External timed lyric source: URL or local LRC/SRT path.
- External first lyric timestamp.
- Clip-local first sung lyric timestamp.
- External tail lyric timestamp.
- Clip-local tail lyric timestamp.
- Global offset.
- Tail delta.
- Stretch flag and ratio when used.
- Alignment report JSON.
- For a sparse-ASR audio fallback: the selected canonical LRC identity, nominal LRC zero, current-media/LRC/prompt/raw-observation/run-manifest SHA-256 values, `agy` provider and exact `Gemini 3.5 Flash (High)` model provenance, and the five named audio spot checks.

Default method:

1. Confirm the song and lyric version.
2. Use the external timed lyric source as timing truth.
3. Compute `offset = clip_first_lyric_time - external_first_lyric_time`.
4. Apply the global shift to all lyric cues.
5. Verify the final lyric against the clip-local tail.
6. Use stretch only if first/tail anchors prove a consistent speed difference.
7. For mixed clips, append separately sourced talk cues after the lyric-aligned section and label that in evidence.

For manual stubborn lyrics or visible defects, add a spectrogram/Gemini pass before final burn. In the unattended song lane, when lyric-to-ASR matching is below threshold but one canonical LRC identity is uniquely supported, the current full proof window plus that LRC must go through the strict audio fallback instead of treating sparse Japanese ASR as a terminal failure:

- Generate waveform and spectrogram images for the lyric window and full clip.
- Send the audio window plus LRC/current SRT to the project-approved Gemini/audio route: agy on `free` with the `Gemini 3.5 Flash` model family, preferably `Gemini 3.5 Flash (High)` for alignment probes when available. (The coded jingting default is `Gemini 3.5 Flash (Low)` via `AGY_MODEL` in `scripts/gemini_slice_jingting.py`; override to `(High)` for alignment probes.)
- Save model timing JSON and job logs under `audio_analysis/` or `evidence/`.
- Accept model timing only where the spectrogram/full-clip context supports it.
- Record rejected model claims explicitly. Do not preserve old Gemini 3.1/2.5 model names as the current workflow. If older evidence exists, regenerate it with agy `Gemini 3.5 Flash` or mark it stale.
- Automated audio proof is narrower than an ordinary model suggestion: `src/autoslice/song_repair.py` independently binds the current source and canonical LRC hashes, requires every canonical line to be heard at confidence `>=0.8`, validates monotonic timings and one global shift, rejects unproved tempo stretch, and requires first-line/chorus/repeated-section/longest-gap/tail checks plus the post-song talk boundary. If an exact lyric repeats, `repeated_section` must point to a later audible recurrence, not its first occurrence. Search ranking or an AGY verdict alone is never proof.
- The same AGY v2 raw observation must independently classify the performance. Only `LIVE_STREAMER_SINGING` at confidence `>=0.85`, continuous singing, background-recording likelihood `<=0.20`, and exact head/middle/tail observations can pass. `ORIGINAL_OR_BACKGROUND_PLAYBACK`, `OTHER_SINGER`, `STREAMER_TALKING_OVER_MUSIC`, or `AMBIGUOUS` is a hard veto even when LRC alignment is perfect.
- CAM++ then checks identity on the same aligned lyric evidence. First, 4–8 seconds of post-song host speech must score a median `>=0.60` against three pinned Li Dousha enrollments. Seven distinct actual lyric cues, each at least 2.5 seconds, contribute a central 2.5–4 second sample. A checkpoint passes only when both its three-enrollment median and its comparison to the same-session host anchor are `>=0.31`; at least 5/7 plus head/middle/tail coverage is required. This only establishes `LIDOUSHA_VOCAL_PRESENT_ON_LYRIC_CHECKPOINTS`; the AGY AND is what turns it into a singing decision.
- The runner recomputes hash bindings, stored-score medians, thresholds, and distribution. It does not rerun CAM++ inference; do not treat artifact verification as a second classifier or a formal identity guarantee.

Blockers (each fires on its own; package-shape checks are enforced by `scripts/audit_lidousha_review_package.py`, while live audio-proof checks are enforced by `src/autoslice/song_repair.py` and the runner final gate):

- AGY v2 live-performance observation is missing/malformed, non-live, below confidence, discontinuous, too likely to be a background recording, or lacks exact lyric head/middle/tail evidence.
- No valid hash-bound `host_vocal_proof`, no usable 4–8 second post-song host speech anchor, verifier unavailable, model/reference/profile drift, or `NO_LIDOUSHA_VOCAL_DETECTED`. These block before `foreground_song_overlap_seconds`, `song_complete`, complete-song semantic waiver, recut delivery, or cover staging can become ready.
- `source_srt` is `.jingting.srt` for a song candidate (blocks unconditionally, even with an alignment report).
- No alignment report exists.
- No external lyric source.
- No first/tail anchor.
- Sparse/noisy ASR leaves more than one plausible song/LRC identity, or the best identity lacks the required recall/margin.
- The audio observation is missing, malformed, produced by the wrong provider/model or fallback, mismatches current source/LRC hashes, omits a canonical line, fails confidence/monotonic/single-shift checks, or lacks any of the five spot checks/post-song boundary evidence.
- A lyric/credit cue hangs through a 10s+ gap without explicit evidence.
- Only the first line was fixed while tail was not checked.

## 3. Subtitle viewability gates

Run gates against the final SRT and final ASS before burning:

- Parseable, monotonic SRT.
- No hard overlaps.
- No long static lyric/text cue (`>=10s`) unless justified.
- ASS visual lines split by `\\N` must fit project limits: at most 2 visual lines, at most 18 non-space characters per line (audit-enforced `MAX_VISUAL_LINES` / `MAX_VISUAL_LINE_CHARS`).
- No oversized multiline cue.
- No known 李豆沙 lexicon leaks such as `天不熊`, `kimo熊`, `给我小给我小`, `给我小` (aliases in `lidousha/term_lexicon.json`; final text must use canonical `kmx`).

Use the repo audit script when present. The script only prints; redirect stdout to produce the package's `audit_lidousha_review_package.json` artifact:

```bash
python3 scripts/audit_lidousha_review_package.py <package-root> --json > <package-root>/audit_lidousha_review_package.json
```

A non-passing audit means the package is not finished. Note the audit script currently enforces the static-cue, line-count/length, song-evidence, title, and cover gates plus invalidation markers — it does not yet check SRT monotonicity, cue overlaps, or lexicon leaks; verify those manually or in tests.

## 4. Title workflow

For 李豆沙 song uploads:

- Bilibili title keeps prefix: `【李豆沙】豆沙歌，...`.
- The title must contain the song name in `《...》` — the audit hard-blocks (`SONG_TITLE_FORMAT_INVALID`) without prefix + `《》`. The *hook phrasing* with live context is the preference on top of that.
- Avoid bare catalog titles unless Ivan asks.
- Do not invent drama from ASR hallucinations.

All title-bearing artifacts must come from one source of truth:

- `title.txt`
- `publish.json`
- `review_manifest.json`
- evidence JSON
- final cover text

Cover text omits `【李豆沙】豆沙歌，` and uses the same short hook phrase.

## 5. AI cover workflow: CPA, not Hermes FAL

The established Li Dousha AI cover path uses the CPA OpenAI-compatible service, not Hermes `image_generate` as the primary route.

Environment:

- `CPA_BASE_URL`, e.g. `https://cpa.aierlma.top/v1`
- `CPA_API_KEY`

Endpoint/model:

- endpoint: `/images/edits`
- method: `images.edit`
- model: `gpt-image-2`
- `image_gen_model: cpa`

Required artifacts:

- identity/reference image under `cover_refs/`
- redacted request artifact under `evidence/`
- redacted response artifact under `evidence/`
- AI background under `covers_ai_original/`
- final cover with embedded `cover_text` under `covers/`
- SHA256 for reference image, AI background, and final cover

Rules:

- Do not conclude AI cover is unavailable just because Hermes `image_generate` / FAL is not configured.
- Do not store API keys in request/response artifacts.
- Prompt must ask for no readable text/UI/watermark; title is overlaid locally afterward.
- The overlaid cover text must omit `【李豆沙】豆沙歌，` / `【李豆沙】`; use the same short hook phrase as `cover_text`, not the full Bilibili title.
- Use the latest project-approved cover-title evidence, not only old 6/19 examples. The composition is now persona-driven and chosen per clip by `_lidousha_cover_art_direction` (in `scripts/run_auto_review_shadow_pipeline.py`): a stable `sha256(candidate_id)` rotation plus a persona keyword lexicon picks {role, expression, background, layout, hook color, hook word}, optionally refined by a fail-open CPA judge. Talk clips rotate `left-split` / `right-split` / `banner` (a large chest-up bust on one side or lower-center, the opposite side or top a graphic zone for a big multi-color title); song clips always use `song-clean` (a soft portrait plus a clean scenic title zone). Backgrounds come from a mood pool — talk = busy (pop-art-burst / halftone-dots / speed-lines), song/tender = clean (soft-radial / clean-scenic) — so covers are not all pop-art. The overlaid title keeps the approved base palette (cream `#FFF6D6` fill + navy `#12244F` stroke) with a rotating accent-colored hook word over a soft dark card, `ZCOOLKuaiLe-Regular.ttf` (fail-closed), and `-4.0` default tilt (talk layouts also use -3/-2). Expression fits the clip's in-character role (default soft/cute 清纯, 机灵/得意 secondary, never tongue-out) and the outfit/skin/hair follow that clip's own reference frame. The old centered/single-band placement and the `chest_safe_top_y=680` chest-line no-go zone are superseded. Do not use arbitrary default fonts/layouts for finished covers.
- Visual-inspect the AI background and final cover for unrelated people/assets, text pollution, or wrong identity.
- `fallback_used: false` must be recorded for finished covers.

Fallback frame covers are allowed only for browse/debug packages and must be explicitly marked `review_fallback`; they are not publish-grade.

## 6. Burn/render workflow

Burn only after final subtitles and title/cover are ready enough for review.

- Use the clean source video/FLV, not a previously burned MP4.
- Burn from the final ASS, not from rough SRT styling.
- Use approved sapphire style for the project (exact parameters: `.agent/skills/song-lyrics-timeline-aligner/SKILL.md` § 李豆沙 Burn Style, as emitted by `align_timed_lyrics.py --ass-out`).
- Auto-review shadow burns must preserve that same contract: `scripts/run_auto_review_shadow_pipeline.py --burn-preview` emits `*.final-sapphire72.ass` and `*.burned-final-sapphire72.mp4` with `subtitle_style=lidousha-final-sapphire72`. Do not regress this to `subtitles=<srt>:force_style=...` or any default SRT/libass style.
- The sapphire72 ASS header is the 1080p variant exactly as `align_timed_lyrics.py --ass-out --play-res 1920x1080` emits it: `PlayResX/Y 1920x1080`, Fontsize 72, margins 60,60,40, Outline 3, Shadow 2, `BackColour &H70000000`. Never pair Fontsize 72 with the 720p PlayRes/margins.
- Song lyric timing contract (root cause of the "subtitles ~20ms early" bug class, fixed 2026-07-03):
  - The burned lyric timeline comes from the external LRC global-shift model (`clip_time = lrc_time + offset`), never from raw ASR cue timings. The materialized recut records `subtitle_source=external_lrc_global_shift` and `lyric_offset_ms`; ASR-based `subtitle_source=asr_cues` is only for non-song clips.
  - The lyrics-alignment proof must survive the single-global-shift validation in `src/autoslice/song_repair.py` (`enforce_global_shift_alignment`): one median offset explains the matches, cue order monotonic, performance span vs LRC span ratio plausible, no long unmatched middle run. Greedy per-line matching alone is not proof (many lines piling onto one cue previously faked a 28s "complete" song).
  - Song recuts are always accurately re-encoded (two-stage seek + re-encode), never shipped as `-c copy` cuts: copy cuts leave audio/video stream starts quantized to packet/keyframe boundaries (measured 20-90ms skew), which shifts burned lyrics off the audio.
  - ASS event times are rounded (not floored) to centiseconds.
  - ASS viewability (Ivan 2026-07-03, spec source: LLM Multimodal ASR `polish_srt_for_viewing.py --max-chars 28`): at most 28 chars per visual line, at most 2 visual lines per dialogue, single line preferred; over-long cues are split into sequential sub-cues by text share (`_layout_cue_for_display`), never stacked 3-4 lines high.
- Verify with ffprobe that the burned MP4 has video and audio streams.
- Extract check frames around first lyric and tail lyric.

## 6.1 Auto workflow binding for cover + title

The unattended/no-upload shadow workflow must not silently downgrade the finished-package requirements:

- `--publish-staging` must stage a CPA/OpenAI-compatible `images.edit` cover generation chain with `model=gpt-image-2`, redacted request/response artifacts, `covers_ai_original/`, local title overlay, final `covers/` output, and hashes for reference/background/final cover.
- If CPA cover generation cannot run or fails, publish staging must fail closed with `cover_status=BLOCKED_AI_COVER_REQUIRED` and `fallback_used=false`. A raw frame extraction such as `<clip>.cover.jpg` is allowed only as explicit browse/debug fallback, not as a publish-grade cover.
- Cover text is derived from the Bilibili title with the `【李豆沙】豆沙歌，` / `【李豆沙】` prefix removed. If the title/hook changes, the cover must be regenerated or restaged with matching `cover_text`.
- Non-李豆沙 live-song smoke tests may exercise selector/repair/burn in no-upload mode, but must not be represented as a Li Dousha publish package or uploaded.
- CPA stages are REAL in workflow/e2e tests too (Ivan, 2026-07-03): semantic QA judge, song-hint, title, and the AI cover must hit the real CPA endpoint. Fake responders are only acceptable inside pytest unit tests. CPA sits behind Cloudflare — every direct HTTP call needs a browser User-Agent or it 403s with error 1010.
- The canonical validated invocation (produced the accepted 2026-07-03 full-song package, `reports/live-song-test/20260703-010424-room362064/OPEN_ME.md`) is recorded in `docs/spark/2026-06-30-future-live-e2e-runbook.md` § "Canonical validated song e2e command". Run that shape; do not re-derive the flags from scratch, and do not copy commands from acceptance reports older than 2026-07-03 (they predate the lyric-timing/cover fixes and are marked superseded).

## 7. Package workflow

A review package should contain:

- `media/` burned MP4
- `subtitles/` final SRT
- `ass/` final ASS
- `covers_ai_original/` AI background
- `covers/` final cover
- `cover_refs/` identity/reference frame
- `publish/` title and publish metadata
- `evidence/` lyric alignment, cover request/response, and semantic evidence
- `evidence/` also includes the delivered `*.host-vocal-proof.json`; the runtime proof's checkpoint WAV/hash chain remains in the protected autoslice evidence store.
- `audio_analysis/` spectrogram/waveform images and model timing JSON when a Gemini/spectrogram pass was used
- `review_manifest.json`
- `audit_lidousha_review_package.json`
- `README_review.md`
- `index.html`
- `contact_sheet.jpg`

Package status meanings (`review_manifest.json.status`):

- `corrected_review_sample_passed_no_upload` / `finished_review_package_no_upload_pending_human_review`: workflow gates pass; no upload performed.
- `invalid_review_draft` (or any `invalid_review_draft_*` extended status): old/wrong package; must not be used as finished evidence. The audit script hard-blocks any package whose status starts with `invalid_review_draft` or that contains an `INVALID_REDO_REQUIRED.json` marker.
- `blocked_*`: a known blocker remains.

## 8. Verification before reporting success

Before saying a package is complete:

1. Run the package audit script and require `passed: true`.
2. Run targeted tests for the audit script after edits.
3. Run ffprobe on the burned MP4.
4. Visual-check contact sheet: AI cover + burned first/tail frames.
5. Check no upload process is running unless explicitly requested.
6. Read back manifest status and file existence.

Do not say `finished` or `publish-ready` unless upload gates and AUTO_UPLOAD/hash gates are explicitly satisfied. A passed local review package with no upload is still `no_upload`.

## 9. Cleanup rules

After a corrected package exists:

- Delete or quarantine old invalid review packages so they cannot be mistaken for finished outputs.
- Delete one-off flawed builder scripts such as the 2026-06-29 `tmp/build_20260629_finished_package.py` that copied `.jingting.srt` and existing covers without real gates.
- Remove fallback covers from a corrected package after the CPA AI cover is generated, unless they are explicitly referenced by a debug manifest.
- Clean both local staging and remote runtime/report paths when the old artifact exists in both places.
- Record cleanup in the final response with exact paths and verification output.

## 10. Known 2026-06-29 samples

Latest corrected sample path (see `lidousha/2026-06-29/README_WHERE_IS_LATEST.md`):

`lidousha/2026-06-29/redone_fullsong_433_travel_meaning`

Expected status:

- `finished_review_package_no_upload_pending_human_review`
- package audit: `passed true`, `blocking 0`, `issues 0`
- cover generation: `image_gen_model cpa`, `model gpt-image-2`, `method images.edit`
- subtitle alignment: external LRC plus spectrogram and agy `Gemini 3.5 Flash` timing verification
- song: `《旅行的意义》`
- external LRC source: `https://www.kugeci.com/song/dJ3Q5ILP`

Known INVALID sample — do not use as gold/finished evidence:

`lidousha/2026-06-29/redone_gold_447_travel_meaning`

Invalidated by Ivan review on 2026-07-01 (`INVALID_REDO_REQUIRED.json`, status `invalid_review_draft_song_boundary_subtitle_cover_failed`): the source clip cuts the song, the subtitle timeline is still wrong, and the cover was not regenerated with the approved 6/24 style. This is the concrete case behind the rule that a mid-song anchor must be recut to the full song, never promoted by fixing subtitles inside the truncated clip. The audit script now fails any package carrying this marker.
