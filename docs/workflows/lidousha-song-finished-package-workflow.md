# Li Dousha song/collab finished package workflow

This is the durable workflow for 李豆沙 song/collab clips in `vtuber-slice`. It exists because a 2026-06-29 package was incorrectly promoted from auto-slice/Jingting artifacts into a "finished" package without running the song subtitle and AI-cover gates.

## Non-negotiable principle

Do not treat packaging as production.

A package is only a browse layer over artifacts that already passed the real workflow. Existing `.flv`, `.jingting.srt`, `.cover.png`, and `.publish.json` files are candidates, not finished evidence.

## 1. Candidate classification

For every candidate, classify before rendering:

- `song` / `collab_song`: foreground singing or lyrics dominate.
- `mixed_song_talk`: a song section plus a talk/accident reaction section.
- `talk`: dialogue / reaction clip without foreground lyrics.
- `reject` / `block`: insufficient context, bad source, duplicate, or unsafe to publish.

These are manual review classes. The pipeline `content_type_hint` (`src/autoslice/full_session_candidate_selector.py`) only emits `song` / `talk`; `collab_song` and `mixed_song_talk` map to `song` for pipeline purposes.

Song and mixed song candidates must not use ordinary ASR/Jingting timing as final lyric timing.

## 2. Subtitle workflow for song / mixed song

Use `.agent/skills/song-lyrics-timeline-aligner/SKILL.md`.

Required evidence:

- External timed lyric source: URL or local LRC/SRT path.
- External first lyric timestamp.
- Clip-local first sung lyric timestamp.
- External tail lyric timestamp.
- Clip-local tail lyric timestamp.
- Global offset.
- Tail delta.
- Stretch flag and ratio when used.
- Alignment report JSON.

Default method:

1. Confirm the song and lyric version.
2. Use the external timed lyric source as timing truth.
3. Compute `offset = clip_first_lyric_time - external_first_lyric_time`.
4. Apply the global shift to all lyric cues.
5. Verify the final lyric against the clip-local tail.
6. Use stretch only if first/tail anchors prove a consistent speed difference.
7. For mixed clips, append separately sourced talk cues after the lyric-aligned section and label that in evidence.

For stubborn lyrics or visible defects, add a spectrogram/Gemini pass before final burn:

- Generate waveform and spectrogram images for the lyric window and full clip.
- Send the audio window plus LRC/current SRT to the project-approved Gemini/audio route: agy on `free` with the `Gemini 3.5 Flash` model family, preferably `Gemini 3.5 Flash (High)` for alignment probes when available. (The coded jingting default is `Gemini 3.5 Flash (Low)` via `AGY_MODEL` in `scripts/gemini_slice_jingting.py`; override to `(High)` for alignment probes.)
- Save model timing JSON and job logs under `audio_analysis/` or `evidence/`.
- Accept model timing only where the spectrogram/full-clip context supports it.
- Record rejected model claims explicitly. Do not preserve old Gemini 3.1/2.5 model names as the current workflow. If older evidence exists, regenerate it with agy `Gemini 3.5 Flash` or mark it stale.

Blockers (each fires on its own; this matches `scripts/audit_lidousha_review_package.py` behavior):

- `source_srt` is `.jingting.srt` for a song candidate (blocks unconditionally, even with an alignment report).
- No alignment report exists.
- No external lyric source.
- No first/tail anchor.
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
- Use the latest project-approved cover-title evidence, not only old 6/19 examples. For the closer 6/24 approved manual recuts, song-hook covers used CPA `gpt-image-2` backgrounds plus `ZCOOLKuaiLe-Regular.ttf`, font sizes around 122–126, `angle_degrees=-4.0`, 1920x1080 final covers, and centers chosen from empty visual areas. Talk covers may have manual title-position repairs to avoid covering Li Dousha. Do not use arbitrary default fonts/layouts for finished covers.
- Visual-inspect the AI background and final cover for unrelated people/assets, text pollution, or wrong identity.
- `fallback_used: false` must be recorded for finished covers.

Fallback frame covers are allowed only for browse/debug packages and must be explicitly marked `review_fallback`; they are not publish-grade.

## 6. Burn/render workflow

Burn only after final subtitles and title/cover are ready enough for review.

- Use the clean source video/FLV, not a previously burned MP4.
- Burn from the final ASS, not from rough SRT styling.
- Use approved sapphire style for the project (exact parameters: `.agent/skills/song-lyrics-timeline-aligner/SKILL.md` § 李豆沙 Burn Style, as emitted by `align_timed_lyrics.py --ass-out`).
- Verify with ffprobe that the burned MP4 has video and audio streams.
- Extract check frames around first lyric and tail lyric.

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
