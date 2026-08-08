# 2026-08-08 ERes2NetV2 vs CAM++ pilot — PARTIAL (compute incomplete at session close)

> Engineering optimization ③ (Ivan task #12). Offline pilot of `iic/speech_eres2netv2_sv_zh-cn_16k-common`
> against the production CAM++ baseline, per `docs/reviews/2026-08-08-diarization-sota-survey.md`
> candidate 1 (highest-priority pilot). **Status: compute did not finish before this session had to
> close. No numeric comparison table exists yet.** This doc records what was verified, what broke
> and how it was fixed, and the exact resume path. Do not treat anything below as a verdict.

## Status

- Both models load and score correctly in a local (Mac) offline environment — verified via
  smoke test (see §2).
- Cue-timing/text integrity for the 61-cue game-session truth set was fully verified against the
  live `2026-08-07` output tree (see §3) — this was the one check that could have invalidated the
  whole pilot, and it passed clean.
- The actual per-cue scoring sweep (CAM++ vs ERes2NetV2, both models, both sessions, 101 cues ×
  3 enrollment refs × 2 models = 606 pipeline calls) was **running but not finished** when the
  session had to close: ~51/101 cues had audio extracted, 0 cues had been scored to a durably
  readable location. See §5 for why (buffering) and §6 for the exact resume command.

## 0. Why this ran locally, not on `free`

Original task scoped the compute to `free` (venv-diar, CAM++ production model dir, modelscope
cache). Mid-task the coordinator redirected to run the pilot on this Mac instead, since `free`'s
CPU belongs to the running 2026-08-08 production batch. All compute below is local; `free` was
touched only for **one-time, read-only `scp` transfers** (enrollment refs, both source clips,
session-A `recut.srt`) — no additional inference or write load was put on `free` beyond the two
initial pipeline-load smoke tests done early in the session (before the redirect), which were
negligible (model already cached, <10s each).

## 1. Environment setup (what broke, what fixed it)

- System Python on this Mac is **3.14**, which is too new for PyO3-based wheels: `pip install
  modelscope[audio]` failed building `pydantic-core` with `error: the configured Python
  interpreter version (3.14) is newer than PyO3's maximum supported version (3.13)`. Confirmed
  exact error before switching approach (per protocol: report exact blocker, don't guess around
  silently).
- Fix: found `python3.11` at `~/.local/bin/python3.11` (3.11.15) and built a fresh venv there:
  `/private/tmp/claude-501/.../scratchpad/eres2-pilot-venv` (venv path is session-scratchpad,
  **not durable** — see §6).
- `pip install modelscope[audio]` also pulled in unrelated legacy TTS deps (`pysptk`) that fail
  to build against modern numpy/setuptools. Abandoned the `[audio]` extra; instead matched the
  known-working package set from `free:/opt/bilive/autoslice/venv-diar` (`pip list` there needed
  `python -m pip` since the venv has no `pip` binary — it's uv-managed) by reading
  `site-packages` directly: `torch==2.5.1+cpu`, `torchaudio==2.5.1+cpu`, `modelscope==1.38.1`,
  `funasr==1.3.14`, plus `addict`, `datasets`, `Pillow`, `yapf`, `simplejson`,
  `sortedcontainers`, `kaldiio`, `oss2`, `einops` (installed incrementally, one
  `ModuleNotFoundError` at a time, until both pipelines imported clean).
- Final local venv: `torch 2.5.1+cpu`, `torchaudio 2.5.1+cpu`, `modelscope 1.39.1` (pip resolved
  1.39.1 for the base install before the pin; the explicit `modelscope==1.38.1` pin in the
  incremental install matches free's version — confirm with `pip show modelscope` before reuse).

## 2. Model load smoke test (PASSED)

Both models load via the exact same `pipeline(task=Tasks.speaker_verification, model=...)`
pattern as `src/autoslice/host_vocal_proof.py::_load_campplus_pipeline` / `speaker_finalizer.py`:

- CAM++: loaded from ModelScope **hub id** `damo/speech_campplus_sv_zh-cn_16k-common` — **this is
  not the production-pinned model directory bytes** at
  `free:/opt/bilive/autoslice/models/campp` (the `model_tree_sha256` gate in
  `host_vocal_proof.py:638`). The production dir was not transferred to keep the pilot read-only
  and light; scores below are a **relative** comparison only, not a drop-in validation of the
  exact production checkpoint.
- ERes2NetV2: loaded from hub id `iic/speech_eres2netv2_sv_zh-cn_16k-common`.
- Sanity scores (enrollment ref 1 vs ref 2, same speaker by construction):
  - CAM++: `score=0.9346`
  - ERes2NetV2: `score=0.95205`
  - **Important caveat**: these two numbers are already on different scales for the exact same
    audio pair. Any absolute threshold/margin values from the current CAM++ production policy
    (`ambiguity_band=0.1`, `single_host_median_seed_min`, etc., all in raw CAM++ score units)
    **cannot be carried over to ERes2NetV2 as-is** even if the pilot shows favorable relative
    separation — they would need to be re-derived from ERes2NetV2's own score distribution.
  - ERes2NetV2 hub example diff-speaker pair (bundled `speaker1_a`/`speaker2_a`) scored `0.0904`,
    confirming the model correctly separates known-different speakers.

## 3. Truth-grid integrity check (PASSED — this was the one thing that could have invalidated everything)

`free:/opt/bilive/autoslice/out/2026-08-07/auto_203735_555_680/replacement_recuts/` shows Aug-8
mtimes (the running production batch is currently re-touching this exact candidate). The repo's
`tests/lidousha/fixtures/ivan_truth_diff_20260807.json` (61 cues, Ivan-adjudicated) does not carry
per-cue timing — timing must come from the SRT. Verified: all 61 `truth_text` values in the
fixture match the current `auto_203735_555_680.recut.srt` on `free` **exactly**, byte-for-byte,
same cue count (61=61) — so the 61-cue timing grid is unchanged by whatever the production batch
is currently doing, and it's safe to extract cue audio using that SRT's timestamps against the
plain `auto_203735_555_680.recut.mp4`.

Also discovered and worked around: the **local, already-downloaded** copy of this candidate
(`lidousha/2026-08-07/刚发誓再也不信真善美，她转头就自夸最.mp4`) is the **published, branded**
final cut — it has the Z1 branding intro prepended (duration delta 131.353646s − 125.583333s =
5.770313s, matching the known Z1 intro asset duration of 5754ms in
`assets/lidousha/intro/branding_intro.v1.json`, ±16ms likely from a gap-variant splice). Its
`.speaker.srt` cue timestamps are **content-relative** (cue 1 still starts at `00:00:00,250`),
so naively extracting cues from that local file would pull ~5.77s from the wrong position for
every single cue. **Fixed by scp'ing the clean, unbranded `recut.mp4` from `free`** (matches the
media path originally specified in the task) instead of trusting the local branded file's
timeline.

## 4. Session B (equal-quality, `auto_200511_61_138`) truth parsing

Ivan's worksheet grammar (`lidousha/2026-07-22/auto_200511_61_138.speaker-eval.srt`) was parsed
programmatically: `[label]` = machine prediction; inline `A`/`B` marks split the line into ordered
sub-spans (mark governs text back to the previous mark or line start); zero marks = trust the
machine label for the whole cue; exactly one mark spanning the whole line = whole-cue correction
(not "mixed"); two+ marks = genuinely mixed cue. Parsed all 40 cues cleanly (no parse errors, no
unexpected trailing unmarked text) — **7/40 cues came out mixed** (cues 9, 12, 20, 23, 25, 36, 40).
Timestamps in this worksheet are self-contained relative-to-`source.mp4` (verified cue 1
`00:00:00,250` matches the separately-generated `auto_200511_61_138.recut.srt` in the same
`tmp/speaker-eval-20260722-...` directory, and `source.mp4` duration 77.2s matches the documented
`absolute_source_start/end` span in `docs/reviews/2026-08-07-speaker-crosssession-eval.md`).

## 5. Why there are no numbers yet

The scoring sweep is CPU-bound (no GPU on this Mac) — each cue requires 3 enrollment-reference
comparisons × 2 models = 6 pipeline forward passes, and empirically each pass takes on the order
of several seconds on this hardware. Total planned work: 101 cues × 6 = 606 passes. At session
close, the process (`eres2_pilot.py`, PID 33936 in this session, ~18 min elapsed, ~369% CPU i.e.
using multiple threads) had extracted audio for 51/101 cues via `ffmpeg` (cheap) but the
`pipeline()` scoring calls run sequentially per cue per model and had not yet reached a save
point. Two design details in the pilot script made partial results unrecoverable this run:

1. The script's own stdout was invoked as `... | tail -120`, which buffers all input until EOF —
   so the per-cue progress `print()` lines never reached the captured output file mid-run.
2. `pilot_scores.json` is written **once, at the very end**, after all 101 cues across both
   sessions are scored — there is no incremental/per-cue persistence.

Neither is a correctness bug, just a resumability gap for next time (worth fixing if this pilot
gets re-run: write each cue's row to disk as it's computed, and don't pipe through `tail`).

## 6. Exact resume path

All pilot artifacts live under this session's scratchpad, which is **not durable** across
sessions — if it's gone, recreate per below.

- Scratchpad root: `/private/tmp/claude-501/-Users-ivan-Project-vtuber-slice/5cbe14f2-2623-4f3f-8308-060346f7e8ab/scratchpad/`
- Venv: `eres2-pilot-venv/` (built with `~/.local/bin/python3.11`; packages listed in §1)
- Media (one-time scp'd from `free`, already local, reusable): `eres2-pilot-media/`
  (`enroll_lds_1.wav`, `enroll_lds_2.wav`, `enroll_lds_3.wav`, `session_a_recut.mp4`,
  `session_b_source.mp4`)
- Session-A SRT: `session_a_recut.srt` (scp'd from `free`)
- Pilot script: `eres2_pilot.py` (parses both truth sources, extracts cue audio, scores both
  models, writes `eres2-pilot-out/pilot_scores.json`)
- Analysis script (already written, not yet run against real data):
  `analyze_pilot.py` — computes, per model per session: best-achievable false_guest count at
  false_host=0 (both "all cues, first-segment label" and "non-mixed only" — flag if the binding
  guest cue is itself mixed, since that changes the honest interpretation), host/guest median
  separation, and short-cue (<1500ms) subset stats.

**To resume:**

1. Check if the original process is still alive: `ps -p 33936 -o pid,etime,pcpu,command` (or
   `pgrep -f eres2_pilot.py`). If alive, just wait for
   `eres2-pilot-out/pilot_scores.json` to appear (audio extraction for already-done cues is
   cached — `extract_cue_audio()` skips existing `.wav` files — so a restart would not redo that
   part, but **would** redo all scoring, since there is no per-cue score cache).
2. If dead (most likely once the session closes), restart in the foreground of a real log file
   (not through `tail`, so progress is visible/resumable):
   ```
   cd /private/tmp/claude-501/-Users-ivan-Project-vtuber-slice/5cbe14f2-2623-4f3f-8308-060346f7e8ab/scratchpad
   ./eres2-pilot-venv/bin/python eres2_pilot.py > run.log 2>&1 &
   tail -f run.log
   ```
3. Once `eres2-pilot-out/pilot_scores.json` exists:
   ```
   ./eres2-pilot-venv/bin/python analyze_pilot.py
   ```
   Paste the resulting table into this doc, replacing this PARTIAL status, and only then write a
   verdict (upgrade justified / not) per Ivan's asymmetric metric (minimize false_guest subject to
   false_host=0).
4. If the scratchpad itself is gone: media must be re-scp'd from `free` (paths in the task spec /
   §0 above are still valid, read-only, small — enrollment refs ~5MB total, session A recut.mp4
   ~22MB, session B source.mp4 ~44MB); the venv must be rebuilt per §1 (roughly 5 minutes,
   `python3.11 -m venv` + the package list above — do **not** use `pip install modelscope[audio]`,
   it drags in a broken `pysptk` build on this machine).

## 7. Integration steps (if the eventual numbers justify it) — not yet decided

Per `docs/reviews/2026-08-08-diarization-sota-survey.md` §5 candidate 1, if the completed pilot
shows ERes2NetV2 strictly dominates (lower or equal false_guest at false_host=0 on **both**
sessions, plus non-trivial short-cue separation gain):

- Mirror the production model-pinning pattern: create `free:/opt/bilive/autoslice/models/eres2netv2/`
  from the exact ModelScope snapshot bytes (not re-downloaded ad hoc — pin the directory the same
  way `models/campp` is pinned), compute its `_sha256_directory` tree hash, and add that as a new
  `model_tree_sha256` entry alongside (not replacing, until cut-over is decided) the existing
  CAM++ entry in `assets/lidousha/voiceprint_profile.v1.json`.
- `host_vocal_proof.py:638` and `speaker_finalizer.py`'s `_load_campplus_pipeline`/model-hash-check
  call sites need a model-family-aware branch (or a second profile) — today they assume CAM++
  hard-coded.
- **Do not carry over the CAM++ raw-score thresholds** (`threshold=0.053779`-style two-means
  outputs are self-calibrated per session so that part is fine, but the *inputs* to two-means —
  `single_host_median_seed_min`, `ambiguity_band=0.1`, `short_cue_ms` stays duration-based so it's
  fine) — `single_host_median_seed_min` and any other raw-score floor in
  `assets/lidousha/voiceprint_profile.v1.json`'s `talk_speaker_policy` are in CAM++ score units
  and were derived from CAM++'s distribution; re-derive from a real ERes2NetV2 replay before
  flipping the model, per the scale gap already observed in §2 (0.9346 vs 0.95205 on an identical
  pair).
- Re-enrollment: the three `enroll_lds_*.wav` references are reusable input audio (verification
  models take raw audio in, not a stored embedding-format enrollment), so no new recording is
  needed — just recompute references against the new model, matching the existing
  `reference["sha256"]` gate flow in `_load_runtime`.

## 8. Blockers / open risk

- **No numeric verdict yet.** This is the primary blocker — see §6 to resume.
- The pilot's CAM++ side uses hub-downloaded bytes, not the production-pinned directory; before
  treating any eventual pilot numbers as authoritative for a production cut-over decision, the
  comparison should ideally be re-run once with the actual pinned CAM++ directory copied over
  (small, ~28MB, one more one-time scp) to rule out drift between the hub snapshot and the pinned
  production checkpoint.
