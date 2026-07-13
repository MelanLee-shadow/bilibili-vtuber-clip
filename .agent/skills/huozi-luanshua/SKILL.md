---
name: huozi-luanshua
description: Build, repair, verify, or review 李豆沙 “活字乱刷” speech reconstructions from historical bilive/Bilibili recordings. Use when Ivan asks to make a sentence from past speech, find longer natural source phrases, distinguish 李豆沙 from guests such as 礼墨, suggest a minimally edited sentence that splices better, or produce no-upload comparison videos.
---

# 活字乱刷

Use the committed runtime at `free:/opt/bilive/autoslice/repo` plus its live cache and recordings as source authority. Treat the macOS worktree as code, test, and review staging.

Read `../../../docs/workflows/huozi-luanshua.md` before running the workflow. Use `scripts/huozi_luanshua.py`; do not replace its evidence and render gates with an ad-hoc concat script.

## Procedure

1. Search all indexed historical transcripts. Treat a date suggested by Ivan as a lead, not the only search scope.
2. Re-extract promising intervals from the immutable original recording. Bind the media hash and run word-timed BCUT plus an independent second transcript authority. If they differ only on a contextual proper-name homophone, preserve both raw observations and add a separate hash-bound human proper-name review; never silently rewrite either ASR result.
3. In mixed-speaker recordings, verify every selected cue as 李豆沙. Explicitly reject known guest cues even when their wording is a perfect match.
4. Record the exact millisecond coverage of each human or voiceprint decision. Never promote context, an adjacent cue, or a partially confirmed phrase to a wider trusted range. A confirmation of `小李是` does not authorize the following `零`.
5. Plan exact reconstruction with the fewest, longest natural pieces. Prefer a continuous `是零` or longer phrase over a standalone clause-final `零`. Inspect the actual selected source IDs and ranges; do not infer continuity from ASR text.
6. If a small wording change preserves the joke and materially improves segment length, render both the original and suggested sentences and label them plainly for Ivan.
7. Render only a verified plan. Keep `upload_enabled=false` and `REVIEW_READY_NO_UPLOAD` unless Ivan separately authorizes publication.
8. Validate each final with full decode, ffprobe, subtitle checks, and fresh BCUT plus Jianying whole-video ASR. Report homophones or uncertain recognition instead of rewriting the evidence.

## Source audit gate

For every delivered candidate, keep a manifest row for each piece with:

- original recording date and absolute media path;
- source utterance and exact core/cut milliseconds;
- immutable media and evidence hashes;
- transcript authorities;
- speaker authority and its exact covered range.

If Ivan says a source is wrong, stop tuning fades. Trace the delivered file hash through the manifest and intermediate piece back to the original recording, discard the disputed source, and rebuild from newly verified material.

## Handoff

Deliver clickable local MP4 paths and a compact source table. Do not upload as part of试听交付. When code or workflow changes were requested, run focused tests first, then the repository suite and the production smoke required by `AGENTS.md` before claiming the runtime is updated.
