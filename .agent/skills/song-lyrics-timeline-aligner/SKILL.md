---
name: song-lyrics-timeline-aligner
description: Rebuild accurate song lyric subtitles from an external timed lyric source such as official captions, LRC, music-platform lyrics, or a trusted online lyric timeline. Use for song clips, live singing clips, karaoke subtitles, 李豆沙 song cuts, Bilibili song uploads, or any request to fix lyrics timing by aligning the first sung lyric and checking the final lyric rather than guessing ASR timings.
---

# Song Lyrics Timeline Aligner

## Core Rule

Before treating aligned lyrics as a song clip, materializing a recut, or doing
cover/package work, prove the clip contains **李豆沙本人现场演唱**. LRC
discovery and alignment may run first because the proof consumes actual aligned
lyric cues, but those steps must not set `song_complete` or create delivery.
An LRC match proves only that a song recording is audible; studio vocals,
ending-card music, game/video BGM, and a played original track are not song
clips. Production requires two independent, hash-bound subclaims on the same
source/LRC evidence: AGY v2 must classify a continuous live streamer
performance, and CAM++ must find Li Dousha vocal identity on the selected lyric
checkpoints. Only their AND result is `VERIFIED_LIDOUSHA_SINGING`. Missing,
unavailable, uncertain, other-singer, speech-over-music, or playback-only
evidence fails closed.

For song clips, do not invent a lyric timeline from ASR, visual rhythm, or only the clip opening. The preferred source of truth is an external timed lyric timeline for the original song, then a clip-local alignment:

```text
trusted timed lyrics -> identify first sung lyric in clip -> global shift -> verify tail -> preview/burn with the project subtitle style
```

The normal correction is a constant time offset. Only use speed/stretch correction when the first and last sung lyric anchors prove a real tempo difference, and record the evidence.

For the unattended auto-slice pipeline, a song candidate is only a recall anchor, never the final clip boundary. If a candidate materially overlaps foreground singing, the pipeline must go back to the full source and either produce machine-readable `song_boundary.status = FULL_SONG_READY` plus `lyrics_alignment.status = READY`, or fail closed. A mid-song 60-90s anchor must not be promoted, burned, or marked gold by merely fixing subtitles inside the truncated clip.

## Workflow

1. Locate the clip and current sidecar subtitle.
   - Keep editable song subtitles as sidecar SRT unless Ivan has approved a burn.
   - Preserve any old SRT as a timestamped backup before replacing it.

2. Search for an external timed lyric source.
   - Prefer official captions, official/verified LRC, music-platform synced lyrics, or a trusted lyric site with explicit timestamps.
   - Use web search when local sources do not contain a credible timed lyric file.
   - Search the clean song title first, including the original Japanese title/kana when available. Google or another public-web search is appropriate for manual discovery; do not make a garbled singing-ASR transcript the only query. In the unattended runner, `--lrc-provider auto` queries both NetEase and LRCLIB, groups provider variants by normalized title+artist or identical full-LRC fingerprint, and prefers the canonical LRCLIB record when the identity is otherwise the same. Japanese language or sparse/garbled singing ASR is not itself a terminal failure.
   - Save the source URL/path and the exact first and last lyric timestamps in evidence.
   - If only plain lyrics exist, do not claim exact timing. Use them only as text and manually align from audio.

3. Identify clip anchors from the actual media.
   - Find the clip time of the first sung lyric by listening and, when useful, checking waveform/spectrogram.
   - Find the clip time of the final sung lyric or final phrase ending.
   - For stubborn live-song clips, generate waveform/spectrogram evidence and send the relevant audio window to the project-approved Gemini/audio route: agy on `free` with the `Gemini 3.5 Flash` model family (prefer `Gemini 3.5 Flash (High)` for alignment probes when available; the coded jingting default is `Gemini 3.5 Flash (Low)` via `AGY_MODEL` in `scripts/gemini_slice_jingting.py`). Do not substitute older Gemini 3.1/2.5 model names in this repo's workflow evidence.
   - Treat model outputs as evidence, not truth: accept model timing only when it agrees with waveform/spectrogram and the full-clip context. If the model contradicts the full spectrogram (for example claiming post-song talk starts while music energy visibly continues), keep the supported timing and record the rejected model claim.
   - Do not use pre-song talk, background music, applause, title cards, or the original recording's absolute timestamp as the first lyric anchor.
   - Treat user-heard anchor corrections as stronger than model/ASR guesses.

4. Apply alignment.
   - Compute `offset = clip_first_lyric_time - external_first_lyric_time`.
   - Default to `clip_time = external_time + offset` for every lyric.
   - Check the predicted final lyric time against the clip-local final lyric.
   - If the tail is close, keep the pure shift.
   - If the tail is not close, re-check the lyric version first. Many failures are from a different cover/version, not speed drift.
   - Only if first and final anchors both prove consistent speed difference, apply a linear stretch and record the ratio.

5. Build readable cues.
   - Keep lyric lines semantically intact; do not split into ASR-style fragments just to hit arbitrary line lengths.
   - Merge very short adjacent LRC lines when they are one sung phrase and the display remains readable.
   - Limit cue display over long instrumental gaps. A lyric cue must not hang through a 10s+ interlude just because the next LRC timestamp is far away.
   - For repeated choruses, align each occurrence from the LRC timestamps, not by copying the first chorus timings by hand.

6. Validate and preview.
   - Validate SRT structure: monotonic cue times, no overlap, no zero/negative durations.
   - Check final SRT/ASS for 李豆沙 lexicon leaks: known ASR aliases in `lidousha/term_lexicon.json` (e.g. `天不熊`, `kimo熊`, `给我小给我小`, `给我小`) must appear as canonical `kmx` in final text.
   - Keep ASS visual lines within the viewability limits (Ivan 2026-07-03, matching the LLM Multimodal ASR project's `polish_srt_for_viewing.py --max-chars 28`): at most 28 characters per visual line, at most 2 visual lines per dialogue, single line preferred. Over-long cue text must be split into sequential sub-cues (time allocated by text share), never stacked into 3-4 lines that cover the picture.
   - Spot-check at least first lyric, first chorus, second verse or repeated chorus, a long gap, and tail.
   - If a preview uses SRT directly, remember ffmpeg/libass will apply default styling. For 李豆沙 publish/burn previews, render ASS with sapphire-outline style instead.

7. Sync evidence.
   - Record lyric source, offset, tail delta, whether stretch was used, SRT hash, and preview checkpoints.
   - When Gemini/model listening or spectrogram review was used, preserve the prompt/job log, model timing JSON, spectrogram/waveform images, and a note explaining which model suggestions were accepted or rejected. The evidence should record `provider: agy` and `model: Gemini 3.5 Flash (...)`; stale evidence from Gemini 3.1/2.5 should be regenerated or clearly marked invalid.
   - Sync the final SRT and evidence back to the matching remote CloudDrive folder.
   - If ASS was generated for burn preview, sync that too.

8. Auto-slice integration contract (implemented in `src/autoslice/full_session_candidate_selector.py` and `scripts/run_auto_review_shadow_pipeline.py`).
   - `kind=song` means 李豆沙 herself is audibly singing. Merely hearing the canonical recording, a stream outro/ending card, game/video audio, or background music is never sufficient. Recall prompts should exclude those cases, but recall remains advisory; the final machine gate is authoritative.
   - Full-session selectors must emit song-like windows as song anchors (`content_type_hint=song`, `requires_full_source_song_boundary_redo=true`) instead of filtering them out as noise.
   - When `scripts/free_session_autoslice.py` has already placed an item in the song lane, that upstream anchor is carried into every tight/core/full selector attempt with `--seed-song-candidate-id` plus clip-local `--seed-song-anchor-start-ms` / `--seed-song-anchor-end-ms`. Do not ask a nondeterministic semantic-recall pass to rediscover whether sparse or garbled Japanese ASR is a song. Only the expanded full-source retry may add `--agy-audio-lrc-align`; seeding preserves recall but never proves completeness.
   - If ordinary lyric-to-ASR alignment is below threshold, audio escalation is allowed only after the candidates resolve to one sufficiently supported canonical song identity. Different-provider rows with the same normalized title+artist or the same complete LRC fingerprint count as one identity; ambiguous or weakly supported different songs must fail closed rather than being forced onto the audio.
   - The audio fallback must inspect the current full proof window and canonical LRC in a sandboxed `agy` `Gemini 3.5 Flash (High)` run. Code, not the model, mints lyric-alignment proof: source/LRC/prompt/raw-output/run-manifest hashes must bind; every canonical line must be affirmatively heard with confidence at least 0.8; starts must be strictly monotonic with at most 250 ms adjacent overlap; one global shift must explain every line within ±1500 ms; tempo drift needs separate stretch proof; and first line/chorus/repeated section/longest instrumental gap/tail plus post-song talk must be checked. When an exact lyric repeats, the repeated-section spot must bind a later audible recurrence, not the first occurrence. A malformed, mismatching, incomplete, or fallback-model observation remains blocked.
   - The same AGY v2 observation is a hard live-performance veto. It must report `mode=LIVE_STREAMER_SINGING`, confidence `>=0.85`, `continuous_singing=true`, `background_recording_likelihood<=0.20`, and exactly three specific evidence timestamps covering lyric head/middle/tail. `ORIGINAL_OR_BACKGROUND_PLAYBACK`, `OTHER_SINGER`, `STREAMER_TALKING_OVER_MUSIC`, `AMBIGUOUS`, or malformed evidence blocks even if all LRC lines align and CAM++ sees occasional Li Dousha speech.
   - A song/live-source job must carry `song_boundary` evidence with `status = FULL_SONG_READY`, full-source clip bounds (`clip_start_ms`, `clip_end_ms`), first/last lyric anchors, and the accepted evidence source such as external LRC + chunked `Gemini 3.5 Flash` + spectrogram/waveform.
   - The same job must carry `lyrics_alignment.status = READY` with provider/model/source metadata. Without this proof, song candidates remain BLOCK/DROP; do not silently pass partial songs.
   - The same job must also carry a separately generated `host_vocal_proof`. First extract 4–8 seconds of post-song host speech and require its median against three hash-pinned Li Dousha enrollments to be `>=0.60`. Then select seven distinct, actually aligned lyric cues of at least 2.5 seconds, sample the central 2.5–4 seconds of each, and require both the three-enrollment median `>=0.31` and the same-session host-anchor score `>=0.31`. At least 5/7 checkpoints and at least one in each of head/middle/tail must pass. This subclaim is honestly named `LIDOUSHA_VOCAL_PRESENT_ON_LYRIC_CHECKPOINTS`; it does not by itself prove that the matching voice is singing rather than talking over music.
   - The runner verifies source/alignment/profile/model/reference/session-anchor/checkpoint hash bindings and recomputes recorded score medians, thresholds, and bucket coverage. It does **not** rerun CAM++ inference, so do not describe this as an independent ML reclassification or formal identity proof.
   - Only after AGY's live-performance subclaim and `host_vocal_proof.status=READY` / `decision=LIDOUSHA_VOCAL_PRESENT_ON_LYRIC_CHECKPOINTS` both pass may the runner mint the joint `VERIFIED_LIDOUSHA_SINGING` decision, set `foreground_song_overlap_seconds`, set `song_complete=true`, resolve a full-song recut boundary, apply the complete-song semantic waiver, or deliver a song artifact. Non-live performance mode, `NO_LIDOUSHA_VOCAL_DETECTED`, verifier outage, missing references/model, a missing/short post-song host anchor, or any hash drift remains BLOCK.
   - When the original candidate anchor starts in the middle of a song, auto-review must emit an `AUTO_RECUT` plan to the full-song range instead of treating the anchor range as final.
   - The generated package must include final SRT/ASS, alignment report, cover workflow metadata, and render/audit evidence before it can be considered complete. Package layout and `review_manifest.json.status` vocabulary follow `docs/workflows/lidousha-song-finished-package-workflow.md` §7 (`corrected_review_sample_passed_no_upload` / `invalid_review_draft*` / `blocked_*`).
   - Burn previews from the auto pipeline must use the 李豆沙 sapphire ASS style (`*.final-sapphire72.ass`, `subtitle_style=lidousha-final-sapphire72`), not direct SRT/default `force_style` rendering. The sapphire72 header is the 1080p variant (PlayRes 1920x1080, margins 60,60,40, Shadow 2, BackColour `&H70000000`), and ASS event times are rounded to centiseconds, not floored.
   - The auto pipeline's burned lyric timeline must come from the external LRC global-shift model (`clip_time = lrc_time + offset`, `subtitle_source=external_lrc_global_shift`), never from raw ASR cue timings — ASR onsets are systematically early/noisy and produced the "lyrics ~20ms early" bug class.
   - A lyrics-alignment proof only counts when it survives `enforce_global_shift_alignment` in `src/autoslice/song_repair.py`: one median offset explains the matches, matched cue order is monotonic, performance span vs LRC span is plausible, and there is no long unmatched middle run. Greedy per-line fuzzy matching alone once promoted a 28s fragment to a fake "complete" song.
   - Song recuts must always be accurately re-encoded (two-stage seek + re-encode); `-c copy` cuts leave audio/video stream starts quantized to packet/keyframe boundaries (measured 20-90ms skew) and silently desync burned lyrics.
   - Publish staging for 李豆沙 must use the CPA `images.edit` AI-cover chain plus local title overlay. If the AI cover cannot be produced, fail closed with a blocked cover status; never replace it with a raw video-frame cover and call that publish-ready.
   - CPA stages are real in workflow/e2e tests too (Ivan, 2026-07-03): semantic QA, song-hint, title, and cover all hit the real CPA endpoint (browser User-Agent required — Cloudflare 403s error 1010 otherwise). Fake responders belong only in pytest unit tests. The canonical validated end-to-end command is recorded in `docs/spark/2026-06-30-future-live-e2e-runbook.md` § "Canonical validated song e2e command" — run that shape instead of re-deriving flags, and never copy commands from acceptance reports marked SUPERSEDED.
   - For 李豆沙 song covers, the upload title keeps `【李豆沙】豆沙歌，...`, but cover text omits that prefix and must be regenerated whenever the title/hook changes.

9. Prepare the Bilibili song title.
   - Keep the project song-prefix format `【李豆沙】豆沙歌，...`.
   - Do not default to a bare title like `【李豆沙】豆沙歌，《歌名》` unless Ivan explicitly asks for a plain catalog title.
   - Prefer a short hook phrase that incorporates the song title in `《...》` and reflects the lyric/live-room context, e.g. `【李豆沙】豆沙歌，原来都是《梦一场》吗？`, `【李豆沙】豆沙歌，《左手右手》牵着你轮回到第一次见她的时刻`, or `【李豆沙】豆沙歌，假装不知情的《年轮》`.
   - Keep the title faithful and compact. Do not invent unrelated drama, and do not change the cover text unless the cover is being regenerated.
   - If the Bilibili song title is changed, update the matching cover before reporting the edit complete. Cover text should omit `【李豆沙】豆沙歌，` and reuse the same hook phrase in a short readable form.

## Failure Modes To Avoid

- Do not infer first lyric time from the start of the clip. In the `梦一场` case, the correct first lyric was around 19s, not 4.5s.
- Do not fix only the first line and leave the rest on an ASR/interpolated timeline.
- Do not use local ASR as lyric timing truth for singing. Singing with BGM often breaks speech ASR coverage and drift.
- Do not interpret a failed Japanese singing-ASR transcript as proof that no timed lyrics exist, but also do not interpret a search hit as proof that the returned LRC is the performed song. Preserve the song anchor, establish a unique lyric identity, then require current-audio proof.
- Do not interpret perfect LRC alignment as proof that 李豆沙 is singing. The 2026-07-09《芽吹くとき》false positive had 25/25 lines at confidence 0.95 because the original recording played under a static goodbye card; it is a negative golden case, not an accepted song clip.
- Do not stretch the whole song because one middle cue feels late. Verify first and last lyric anchors first.
- Do not let a line remain visible across a long instrumental gap.
- Do not burn a final video from default SRT styling. SRT is timing/text; burn style must come from the approved ASS style.
- Do not report "done" without checking a tail lyric. A subtitle can have a correct opening and still be wrong for the rest of the song.

## Helper Script

Use `.agent/skills/song-lyrics-timeline-aligner/scripts/align_timed_lyrics.py` (skill-local; there is no copy under the repo-root `scripts/`) for first drafts from LRC:

```bash
python3 .agent/skills/song-lyrics-timeline-aligner/scripts/align_timed_lyrics.py \
  --lrc external.lrc \
  --out clip.song.srt \
  --clip-first 00:00:19.200 \
  --report clip.song-alignment.json \
  --play-res 1920x1080 \
  --ass-out clip.final-sapphire72.ass
```

The default `--play-res` is `1280x720`, which emits the 48pt style — pass `--play-res 1920x1080` when you want the 72pt (`sapphire72`) variant.

When the last lyric was checked in the clip, add it:

```bash
python3 .agent/skills/song-lyrics-timeline-aligner/scripts/align_timed_lyrics.py \
  --lrc external.lrc \
  --out clip.song.srt \
  --clip-first 00:00:19.200 \
  --clip-last 00:03:15.700 \
  --report clip.song-alignment.json
```

By default, the script reports tail drift but does not stretch. Use `--allow-stretch` only after verifying the source version is correct and the clip is actually faster/slower.

## 李豆沙 Burn Style

For 李豆沙 burn previews and final ASS, use the established sapphire-outline style instead of default black-outline SRT rendering (these values are exactly what `align_timed_lyrics.py --ass-out` emits in `write_ass`; `fontsdir=/app/assets` is an ffmpeg/libass burn-time option, not an ASS style field):

```text
Fontname=Microsoft YaHei
PrimaryColour=&H00FFFFFF
OutlineColour=&H00BA520F
Outline=3
Alignment=2
fontsdir=/app/assets
```

For 720p 6/24-style outputs (`--play-res 1280x720`, the default), use `Fontsize=48`, `Shadow=2`, `BackColour=&H70000000`, margins `40,40,30`. For 1080p outputs (`--play-res 1920x1080`), the script emits the approved 72-size variant: `Fontsize=72`, margins `60,60,40`. Always inspect preview frames after generating ASS.
