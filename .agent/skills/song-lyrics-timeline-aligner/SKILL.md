---
name: song-lyrics-timeline-aligner
description: Rebuild and verify song-cut lyric subtitles from a trusted timed lyric source, proving full-song boundaries and current live Li Dousha performance before package or publication.
---

# Song Lyrics Timeline Aligner

This skill is an operator recipe. Read the current authorities first:

- `../../../docs/pipeline/50-song-lane.md`
- `../../../docs/pipeline/60-title.md`
- `../../../docs/pipeline/70-cover.md`
- `../../../docs/pipeline/80-package-delivery.md`

Never select a provider/model, schema version, threshold, runtime command or acceptance state from a dated
spark/review report. Read them from the current code/profile and live runtime.

## Procedure

1. Treat the candidate as a recall anchor, not the final song. Return to the complete source recording and
   bind its identity/hash.
2. Find a trusted timed lyric source for the exact performed version. Preserve provider URL/ID, raw bytes and
   hash. Search hits or title similarity alone are not proof.
3. Establish clip-local first and actual-final lyric anchors. Fit one global shift first:
   `clip_time = lyric_time + offset`. Do not stretch merely because a middle line looks late; allow stretch only
   when explicit head/tail evidence proves stable drift.
4. Verify the whole performed sequence: monotonic matching, plausible span, no unexplained middle run, real
   opening/ending and post-song transition. A truncated 60–90 second anchor cannot be promoted by repairing
   subtitles inside it.
5. Run the current live-performance observation and host-vocal proof on the same source/LRC evidence. The
   production schemas are read from `src/autoslice/song_common.py` and
   `src/autoslice/host_vocal_proof.py`; both subclaims must pass. Perfect LRC alignment can still be an original
   track, BGM or replay and is never sufficient by itself.
6. Materialize subtitles only from accepted timed lyrics/performed rows, never raw singing ASR timings.
   Preserve canonical lyric wording and credit/source evidence.
7. Run `src/autoslice/subtitle_validation.py::validate_srt_file`: every non-empty block consumed, consecutive
   indices, exact timestamps, duration at least 300ms, monotonic/no overlap, valid text and media bounds.
8. Generate the approved ASS and accurately re-encode the final cut. Do not use `-c copy`; verify audio/video
   streams, first lyric, tail lyric and post-song transition against the final bytes.
9. Final title is exactly `【李豆沙】豆沙歌，《canonical歌名》`; cover text is exactly `《歌名》`.
   Song cover uses the current song route from the cover step and must carry real final-pixel/glyph evidence.
10. Build the portable package and require the current package audit v2 input closure. A stored `passed:true`
    or an old sample package cannot be reused.
11. Keep `upload_allowed=false` unless Ivan separately authorizes this exact artifact. If authorized, use the
    publish step’s manifest-bound uploader; do not upload from this skill.

## Failure discipline

- Missing/ambiguous timed lyrics, version mismatch, incomplete boundary, live-performance/identity failure,
  model/runtime/hash drift, malformed SRT or cover/package proof all remain BLOCK.
- Do not infer “not singing” from sparse Japanese/singing ASR, but do not infer “Li Dousha singing” from an LRC
  match or one voiceprint score.
- Do not fix only the first lyric and skip the tail.
- Do not leave a lyric cue hanging through an instrumental gap without evidence.
- Do not use a song failure as a talk candidate; song intervals remain quarantined according to the song step.
