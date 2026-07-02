# Spark Spec — vtuber-slice autonomous high-quality slicing reset

Date: 2026-06-29 16:35 EDT
Board: `vtuber-slice-auto-slicing`
Authority boundary: production source of truth is `free:/opt/bilive/app` and container `/app`; local macOS workspace is staging/docs/tests/spec evidence.

## 1. Why this reset exists

The current Kanban graph advanced safety infrastructure, but it did not satisfy Ivan's original product target.

The practical failure is precise:

- Current output can still stop at raw candidate clips.
- Current candidates may be the old fixed-window / candidate-boundary products.
- 2026-06-29 evidence showed candidates existed, but they were not post-Jingting, not source-context reviewed, not boundary-resolved, not auto-recut final clips.
- Shadow review skipped on `jingting_incomplete` for 2026-06-29.
- Existing no-upload validation proved fail-closed behavior, not high-quality autonomous slice production.
- Bilibili replay / VOD compensation for incomplete recordings is not implemented.

So the old graph's done state is not product-done. It is safety-gate-done.

## 2. Original user goal recovered from session history

The earliest explicit product statement found in session history:

> "有一个问题是我希望这个项目的终极形态是能够自动切片自动上传，我人手不用review，所以我其实希望这个项目能够设置自动review的功能，并且你看这些切片时长其实和我的时长并不一致，这些切片或者是把歌断了，或者是谈话没有讲完就断了，不符合我的偏好。"

The later P7 implementation prompt made the acceptance boundary even clearer:

> "Ivan 的最终目标不是‘安全地 BLOCK 坏切片’，而是‘李豆沙自动切片可以无人 review、无需人工歌词核对，并且风格接近 Ivan 手动切片’。"

It also specified the intended architecture:

```text
原始录播 + 粗字幕 + 弹幕
→ 高召回内容锚点
→ 扩展源上下文
→ agy 精听源上下文
→ 歌曲/对话结构分析
→ 自动边界解析
→ 自动 review / recut / drop / block
→ 最终切片
→ 渲染后 QA
→ 严格 release gate
→ 幂等上传
```

Therefore the corrected goal is not "produce 90s candidates" and not merely "BLOCK bad candidates". The corrected goal is:

> From live or replay recording input, autonomously produce final high-quality slices whose boundaries are resolved from source-context evidence; songs are complete or blocked/dropped; dialogue has setup/payoff/closure; missing recording ranges are detected and recoverable from Bilibili replay/VOD when available; all release decisions are made by machine evidence; upload remains disabled until a separate explicit approval gate.

## 3. Non-goals and safety boundaries

Non-goals for this reset:

- No public upload / Bilibili publish during implementation.
- No enabling `src.upload.upload`.
- No setting `upload_enabled=true`.
- No push/commit unless Ivan explicitly asks.
- No reading or printing protected runtime secrets: `cookie.json`, `settings-*.toml`, `src/db/data.db`, `tmp/`, API keys/tokens/cookies.

Allowed scope:

- Code-changing implementation on `free:/opt/bilive/app` for no-upload automatic slicing.
- Shadow/no-upload daemon integration.
- Service work only for no-upload shadow/monitor after preflight; no uploader service.
- Replaying historical packages and real recordings for validation.

## 4. Corrected acceptance criteria

The project is not acceptable as "complete" until the no-upload pipeline demonstrates all of these:

1. Recording completeness
   - The system detects incomplete / stalled / gapful recordings.
   - If local recording is incomplete and Bilibili replay/VOD is available, the system can fetch and align the missing source segment, or emits a clear RETRY/BLOCK reason if unavailable.

2. Candidate anchors are only anchors
   - Raw detector candidates are never treated as final release clip boundaries.
   - A final output must cite a boundary-resolution artifact.

3. Source-context Jingting
   - The pipeline expands each candidate to source context before high-confidence review.
   - agy/Jingting provenance is recorded for the source-context evidence.
   - Missing/failed/unknown-provider Jingting remains RETRY/BLOCK.

4. Boundary resolver / recut loop
   - Song candidates are full-song-or-BLOCK/DROP; no fixed 90s song truncation.
   - Dialogue candidates require setup/payoff/closure evidence.
   - AUTO_RECUT materializes a revised clip and the revised clip is re-reviewed.

5. Evidence is real, not placeholders
   - duplicate similarity, subtitle alignment p95, actual cut error, source-context cue coverage, style/profile score, and artifact hashes are measured or explicitly marked missing.
   - Missing evidence cannot become AUTO_UPLOAD/would_upload.

6. End-to-end no-upload validation
   - At least one historical golden package and one real live/replay run go through: recording/source -> anchors -> source-context Jingting -> boundary resolver -> recut/final render -> QA/hash -> review decision.
   - It is acceptable for the decision to be BLOCK/DROP/RETRY if evidence says so; what is not acceptable is stopping at raw candidates.

7. Independent review
   - A reviewer checks the final result against the original goal above, not against the weaker safety-gate goal.

8. Upload gate remains blocked
   - Upload/canary/full cutover stays behind a separate blocked approval card.

## 5. Recommended implementation lanes

### Lane A — Production baseline and gap ledger

Purpose: re-open the source of truth before writing more code. The worker must verify the live code paths, current dirty diff, current services, latest artifact shape, and the exact point where 2026-06-29 stopped.

Output: a gap ledger that maps each corrected acceptance criterion to current code/artifacts.

### Lane B — Recording completeness and Bilibili replay compensation

Purpose: make "录播如果不全，应从 Bilibili 原始直播回放补全" a real module, not a wish.

Minimum behavior:

- Detect media stalls, too-short files, discontinuities, missing ranges, and danmaku-only growth.
- Build a source-range ledger per live session.
- Probe for Bilibili replay/VOD availability using existing authenticated environment only; no credential printing.
- Download or stage missing ranges when possible.
- Align downloaded ranges to local timeline.
- If unavailable, emit RETRY_INFRA / SOURCE_GAP_UNRECOVERED instead of pretending the recording is complete.

### Lane C — Source-context Jingting production scheduler

Purpose: move Jingting from "after candidate clip" to "expanded source-context before final boundary".

Minimum behavior:

- Candidate anchor -> source-context job -> context media/SRT -> agy refined source cues.
- Provenance manifest contains provider/model/rc/hash/fallback status.
- No fake `.jingting.done`.
- 2026-06-29-style `jingting_incomplete` becomes actionable work, not silent skip.

### Lane D — Boundary resolver and recut loop

Purpose: convert evidence into actual final boundaries and materialized recuts.

Minimum behavior:

- For songs: detect partial song, expand to full song where evidence supports it, otherwise BLOCK/DROP.
- For dialogue: require setup/payoff/closure and natural endpoint.
- AUTO_RECUT creates a revised output and invalidates stale title/cover/publish/hash sidecars.
- The revised output is re-reviewed before any release marker.

### Lane E — Real quality evidence and hash gate

Purpose: remove placeholder quality metrics.

Minimum behavior:

- actual cut error from render metadata / ffprobe / timeline evidence.
- subtitle alignment p95 from real subtitle/media alignment.
- duplicate similarity from existing published/manual/history artifacts.
- final render/subtitle/cover/evidence/publish JSON hashes in `slice-auto-review.v1`.

### Lane F — End-to-end no-upload daemon integration

Purpose: make the above run unattended under shadow/no-upload, not just in ad hoc replay.

Minimum behavior:

- Daemon state machine drains new recordings and candidates through the full loop.
- Produces durable per-candidate state: anchor, context, jingting, boundary, recut, QA, decision.
- Can recover/retry without duplicate markers or stale sidecars.

### Lane G — Golden/live validation and product acceptance review

Purpose: prove the target, not just compile.

Minimum behavior:

- Replay at least the 2026-06-25 package and the 2026-06-29/latest recording evidence.
- Run the next real Li Dousha stream or available replay through no-upload shadow.
- Report counts: raw anchors, source-context jobs, jingting done, auto_recut, final recuts, block/drop/retry, would_upload, upload=0.
- Review explicitly asks: "Are these final slices after source-context review and recut?" If no, fail.

## 6. Kanban reseeding plan

Supersede the old tail cards as product-complete proof:

- `install-no-upload-shadow-monitor-service` is not the core milestone.
- `post-install-no-upload-service-validation` is not enough.
- `future-cutover-approval-gate` remains blocked and should depend on new acceptance, not the old safety-only chain.

Create a new P8 reset graph on the same board:

1. `P8 root — original-goal reset: autonomous high-quality slicing, no-upload`
2. `P8.0 production baseline and acceptance-gap ledger`
3. `P8.1 recording gap detector + Bilibili replay compensation`
4. `P8.2 source-context Jingting scheduler integration`
5. `P8.3 lyrics/song completeness automation`
6. `P8.4 boundary resolver + auto-recut materialization`
7. `P8.5 real QA metrics + final artifact hash manifest`
8. `P8.6 full no-upload daemon state machine integration`
9. `P8.7 historical + real-stream no-upload validation`
10. `P8.8 independent product acceptance review`
11. `P8.9 future upload/canary approval gate` — blocked by design.

Implementation cards may complete with evidence because explicit downstream review gates exist; if they cannot produce evidence or hit credentials/approval/service/uploader boundaries, they must block with exact reason.

## 7. Immediate product definition

A successful near-term result may still upload nothing. That is acceptable.

But a successful near-term result must show, for at least one real/historical input, that the system progressed beyond raw candidate clips into:

```text
anchor -> source-context Jingting -> boundary decision -> recut/final render -> real QA/hash -> machine review decision
```

If the output is only "14 candidates exist" or "10 candidates BLOCK because evidence is missing", the reset has failed.
