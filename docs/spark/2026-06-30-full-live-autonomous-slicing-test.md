# 2026-06-30 Direct Takeover Spec: Full-live autonomous slicing test

> **SUPERSEDED design snapshot — do not execute.** It preserves the 2026-06-30
> test plan only. Current gates and commands start at
> [../pipeline/README.md](../pipeline/README.md).

## Goal

Drive vtuber-slice toward Ivan's target: unattended high-quality slicing that can take a full Li Dousha livestream source, generate/review/recut candidates automatically, fail closed on missing evidence, and stop before upload unless explicitly approved.

This phase must prove the pipeline on one complete livestream, not just isolated clips.

## Non-goals / safety boundaries

- No Bilibili upload or publish.
- No uploader daemon/service start.
- No reading or printing cookies, settings files, DB secrets, or token-bearing configs.
- No Kanban dispatch or Kanban worker usage.
- No pretending an incomplete local recording is complete.
- Official replay/auth/download paths must fail closed when locator/auth/tooling is unavailable.

## Current verified baseline

- Jingting daemon is single-instance and 2026-06-29 has pending=0, done=14, retry=0, locks=0.
- Shadow daemon is single-instance with a directory lock.
- No upload process is running.
- 2026-06-29 no-upload shadow correctly blocks due to source gap 19:00:15.044 -> 21:35:13.000 and BILIBILI_REPLAY_AUTH_REQUIRED.
- live-source path can run via injected agy runner and materialize no-upload replacement_recuts artifacts.
- Relevant tests on free passed: 74 passed.

## Problem discovered during full-live sample selection

The local recording dates currently visible under `live-streaming/22966160` are not clean complete livestreams under current source_integrity:

- 2026-06-29: large gap, known broken recording.
- 2026-06-25: ~619829ms missing range.
- 2026-06-24: ~597829ms missing range.
- 2026-06-17: multiple gaps including ~290650ms.

Therefore the acceptance test cannot honestly use these local recordings as a complete-source success case unless an official/replacement source fills the gaps.

## Recommended implementation lanes

### Lane A — full-live sample and replay source

1. Search existing artifacts for `official_source`, `replacement_source`, `source_official_meta`, BVID/CID, and existing downloaded official full sources.
2. If a complete official source exists, select it as the acceptance sample and record exact file/metadata paths.
3. If no complete official source exists, implement a fail-closed seeded replay manifest path first:
   - Input: manually supplied or discovered BVID/CID/source URL metadata.
   - Output: `official_source/replay_manifest.json` and staged media path.
   - Do not read cookies or download unless auth/tooling is explicitly available and non-secret.
4. Only after a complete source is present, run full-live no-upload E2E.

### Lane B — render QA evidence回灌

1. Add TDD tests proving materialized live-source recut/final render records `actual_cut_error_ms` from `render_qa.evaluate_render_pts`.
2. Persist render QA manifest next to materialized media.
3. Feed actual cut error/hash evidence into final decision/evidence instead of leaving `ACTUAL_CUT_ERROR_MISSING`.
4. Keep fail-closed if metadata cannot prove source start/end.

### Lane C — daemon live-source routing

1. Add TDD tests: when a candidate lacks `.jingting.done` but publish/evidence contains source video/SRT and anchor timing metadata, daemon routes it to `run_shadow_pipeline(source_video=..., source_srt=..., source_context_job=...)` instead of returning `jingting_incomplete`.
2. If current artifacts do not contain enough source/anchor metadata, define the exact upstream fields and keep daemon fail-closed.
3. Do not fallback to guessed boundaries.

### Lane D — full-live no-upload acceptance

Acceptance command must run against one complete full-stream source and produce a report containing:

- source_integrity: can_use_local_source=true or official_source coverage proof.
- candidates evaluated > 0.
- Jingting/source-context evidence present for tested candidates.
- boundary resolver outcomes recorded.
- materialized outputs for AUTO_RECUT/AUTO_UPLOAD candidates, if any.
- render QA actual_cut_error_ms recorded or explicit fail-closed reason.
- auto_upload=0 unless Ivan explicitly authorizes upload.
- no uploader process observed before/after.

## Subagent fan-out

Dispatched background subagents:

1. Full-live acceptance sample selector: find complete source/official metadata and propose exact no-upload E2E command.
2. Render QA回灌 implementer: TDD lane for actual_cut_error_ms from materialized outputs.
3. Daemon live-source routing implementer: TDD lane for missing-jingting candidates with source/anchor metadata.

## Commander obligations

- Verify all subagent claims directly.
- Run tests on authoritative remote before touching runtime services.
- Use no-upload shadow only.
- Stop for credentials/auth/download/upload/publish decisions.
