# 2026-06-30 Gemini 3.5 Flash skill cleanup

## Trigger

Ivan corrected the workflow: the available audio model route for this repo should be agy `Gemini 3.5 Flash`, not exploratory Gemini 3.1/2.5 names. Song lyric alignment must be recorded in skills and old/wrong/stale workflow references must be removed.

## Decisions

- Project-local skill `.agent/skills/song-lyrics-timeline-aligner/SKILL.md` is authoritative for song lyric alignment.
- For stubborn live-song lyric timing, use waveform/spectrogram evidence and agy `Gemini 3.5 Flash` (prefer `Gemini 3.5 Flash (High)` when available) for per-line timing probes.
- Model output is evidence, not truth; accept only when it agrees with spectrogram/full-clip context.
- Stale evidence from Gemini 3.1/2.5 should be regenerated with Gemini 3.5 Flash or deleted/marked invalid.
- The obsolete bundle-side one-sample redo reference was superseded by `vtuber-slice-review-packaging/references/lidousha-song-cpa-spectrogram-gold-sample.md` and removed; do not keep the stale path in current docs.
- Finished Li Dousha covers use CPA `/images/edits` plus project-approved title overlay. Cover text must not include `【李豆沙】豆沙歌，`; use cover_text only. Use approved title layout/template where available, e.g. 6/19 `title-center-v2` parameters.
- Do not rely only on 6/19 cover/title examples. Remote 6/24 approved manual recuts provide closer evidence: song-hook covers use CPA `gpt-image-2`, `ZCOOLKuaiLe-Regular.ttf`, font sizes around 122–126, `angle_degrees=-4.0`, 1920x1080 final covers, title placement in empty visual areas, and hook cover_text without the `豆沙歌` prefix. Talk covers may have manual title-position repairs to avoid covering Li Dousha.

## Files changed

- `.agent/skills/song-lyrics-timeline-aligner/SKILL.md`
- `docs/workflows/lidousha-song-finished-package-workflow.md`
- global skill `vtuber-slice-review-packaging/SKILL.md`
- global skill reference `vtuber-slice-review-packaging/references/lidousha-song-cpa-spectrogram-gold-sample.md`
- global skill reference `vtuber-slice-review-packaging/references/lidousha-20260624-approved-style-evidence.md`
- global skill `vtuber-slice-review-bundles/SKILL.md`
- removed the obsolete bundle-side one-sample redo reference file

## Verification target

Search skill/workflow docs for stale current-model claims:

- no active workflow should prescribe Gemini 3.1/2.5 for Li Dousha lyric alignment;
- project-local skill should prescribe agy Gemini 3.5 Flash;
- old bundle reference path should not exist;
- package evidence should keep `gemini_3_5_flash_high_timing.json` and not keep active `gemini_3_1_pro_high_timing.json`.
