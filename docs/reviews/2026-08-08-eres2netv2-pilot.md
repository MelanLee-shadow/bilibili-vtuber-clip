# 2026-08-08 ERes2NetV2 vs CAM++ pilot — COMPLETE (do not switch)

> Engineering optimization ③ (Ivan task #12). Offline, truth-referenced comparison of
> `iic/speech_eres2netv2_sv_zh-cn_16k-common` against the production CAM++ model tree.
> **Status: complete. All 61 + 40 cues and all 606 model/reference comparisons were scored.**
> **Decision: do not replace CAM++ with ERes2NetV2.**

## 1. Executive decision

ERes2NetV2 does not satisfy the requested asymmetric objective: while forcing zero false
李豆沙 calls on each truth set, minimize false 连线 calls and improve short-cue separation.
The primary analysis excludes mixed-speaker cues because one whole-cue embedding cannot be
assigned a causal first-segment truth label.

| Primary non-mixed result | CAM++ | ERes2NetV2 | Better |
|---|---:|---:|---|
| Session A false 连线 at false 李豆沙 = 0 | 3 / 22 | 1 / 22 | ERes2NetV2 |
| Session B false 连线 at false 李豆沙 = 0 | 1 / 26 | 4 / 26 | CAM++ |
| Total false 连线 | **4 / 48** | **5 / 48** | **CAM++** |
| Short-cue false 连线 (`<1500 ms`) | **2 / 14** | **5 / 14** | **CAM++** |
| Observed-set pooled non-mixed rank AUC | **0.9964** | 0.9896 | **CAM++** |
| Observed-set pooled short-cue rank AUC | **0.9966** | 0.9728 | **CAM++** |

The candidate gate fixed before viewing the completed scores therefore **FAILS**. Session A's
two-cue gain is outweighed by a three-cue regression in Session B, and all four Session B
ERes2NetV2 false-连线 errors are short host cues. This is an optimistic, truth-informed
separability oracle rather than a production replay; failure here is enough to reject a current
production switch. No model/profile/policy production files were changed.

## 2. Evaluation contract and interpretation boundary

For every cue, each model scored the extracted cue against all three hash-bound 李豆沙 enrollment
WAVs. The cue score is the median of those three pairwise cosine scores.

- Primary set: 88 non-mixed cues across both sessions (48 李豆沙, 40 连线).
- Sensitivity set: all 101 cues with a mixed cue provisionally assigned its first truth segment.
  This is reported only to check whether the conclusion is fragile; it is not a valid primary
  measure for mixed audio.
- Classifier convention: predict 李豆沙 iff `score >= threshold`.
- Per-model, per-session oracle threshold:
  `nextafter(max(non-mixed 连线 score), +infinity)`. Equivalently for the stored finite values,
  a cue must score strictly above the largest 连线 score to be called 李豆沙.
- “False 李豆沙” means a true 连线 cue called 李豆沙. The oracle construction makes this zero
  in-sample. “False 连线” means a true 李豆沙 cue called 连线; this is the quantity minimized.
- Short cue: duration strictly below 1500 ms, evaluated with the same session-wide primary
  boundary rather than a short-only fitted boundary.
- Rank AUC is included as a threshold-free separation diagnostic. The pooled values also compare
  pairs across sessions and are therefore observed-set diagnostics, not session-stratified
  estimates. Raw medians, raw gaps and thresholds are never compared across model families.

The oracle uses ground truth to choose a boundary. Its zero false-李豆沙 result is a comparison
constraint, **not** evidence that a deployable threshold would achieve zero future errors.

The predeclared candidate gate required all of the following:

1. ERes2NetV2 false-连线 count no worse than CAM++ in each session.
2. Strictly fewer false-连线 cues in total.
3. At least one fewer short false-连线 cue in total.
4. Observed-set pooled short-cue rank AUC no lower than CAM++.

ERes2NetV2 failed all four candidate-gate conditions. Both models separately satisfy the
oracle-constructed comparison constraint of zero in-sample false 李豆沙.

## 3. Inputs, truth reconstruction and timing integrity

### Session A — `auto_203735_555_680`

- Media: 125.583333 s clean recut, SHA-256
  `f45b83aabcfaabdfb15c68215962ddf620b2f158161a88e7dd20a96d97433bed`.
- SRT: 61 cues, SHA-256
  `6a21bada764e8038fd3c87743aa2e9080ce52f9ed5eb58ec49f0db2acee45c16`.
- Truth fixture: `tests/lidousha/fixtures/ivan_truth_diff_20260807.json`, SHA-256
  `7e14692958ac1ddf4907470642752b1083755ac57e352d7e72fa0d7451691c12`.
- All 61 truth texts match the SRT exactly. There are 6 mixed cues. The 55 primary cues contain
  22 李豆沙 and 33 连线; the 23 short primary cues contain 6 李豆沙 and 17 连线.

The scorer hard-gates both media hash and duration. This prevents recurrence of the earlier
5.770313 s mistake: the 131.353646 s published file contains a branding intro while the SRT
timestamps remain content-relative. Scoring that file without an offset would shift every cue.
Only the 125.583333 s unbranded recut was used here.

### Session B — `auto_200511_61_138`

- Media: 77.2 s source, SHA-256
  `f5662ff2c919101e90c1e5028f68d5c5f59aa4e68d5fd0eb00d11121f805cbc7`.
- Reviewed SRT: 40 cues, SHA-256
  `07bc7780d5eef6536659edef64cbe514f9fae794f75481ea79ca885123d71080`.
- Speaker override: 11 corrected cues, SHA-256
  `1e2fe85ac75c89c89a9e3842ef42d9763ce7a0de410a4a5e7fbb09bc464d97d4`.
- Exact machine-labelled base SRT: SHA-256
  `fe802962785a20ed49b3279c4375b0cb7320eb5e97940bc172f8d72b0a56bdb1`.

The reviewed plain SRT plus the 11-row override is not, by itself, a complete speaker truth set:
29 accepted machine labels must be inherited from the exact automatic SRT to which the override
is bound. That hash-bound `speaker-final.srt` was recovered read-only from the recorded `free`
evaluation directory and copied to the allowed local pilot directory; no remote inference or
remote write was performed. Applying the 11 overrides produces 7 mixed cues. The 33 primary cues
contain 26 李豆沙 and 7 连线; the 12 short primary cues contain 8 李豆沙 and 4 连线.

### Enrollment and extraction

All three mono 16 kHz enrollment WAVs exactly match `voiceprint_profile.v1.json`:

- `enroll_lds_1.wav`: `d54a637d520df906a4a17728f2112ca3819b6afe43e2e038e8d6db52c094105f`
- `enroll_lds_2.wav`: `8fc095fe8558d218fadea7f6273ee5296f85662164aa125aae68eb9381ce7ef3`
- `enroll_lds_3.wav`: `d257b7aef63f022772fbacc61d35954a65b4727b29f062edb0b398531b95c5a2`

Cues were extracted at exact SRT intervals as PCM16 mono 16 kHz, with zero handles, preserving
the original PARTIAL pilot contract. The cue-audio namespace is bound to input fingerprint
`ac4dfb49dda5c419664e8fcec835bf9e3fd797b80182c983e6cafdf05f3bb065`;
cached WAVs are duration-validated before reuse. Production `speaker_finalizer.py` can add bounded
±150 ms handles, so these are not byte-identical production cue windows.

## 4. WSL environment and model identity

The fresh venv is `/home/ivan/Project/vtuber-reproduce/pilot/eres2-pilot-venv`.

- Python 3.11.15 was present and used. Python 3.12.3 was available only as the requested fallback
  and was not used. A 3.12 rerun could resolve different binary wheels/dependency versions and
  would need a new smoke/full verification; no 3.12 equivalence is claimed.
- `torch==2.5.1+cpu`, `torchaudio==2.5.1+cpu`, `modelscope==1.38.1`,
  `funasr==1.3.14`, `numpy==2.4.6`, `datasets==5.0.1`, `transformers==5.14.1`,
  `scipy==1.17.1`, `soundfile==0.14.0`.
- `python -m pip check`: `No broken requirements found.`
- `modelscope[audio]` was deliberately not installed. The old PARTIAL run showed that it pulls
  the unrelated legacy `pysptk` build chain. The earlier Mac Python 3.14 PyO3 failure is likewise
  avoided by using 3.11.
- All package and model caches are under the allowed pilot directory.

Model directories were downloaded once, then the full scoring run used the resolved local
snapshot directories:

| Model | Hub identity | Directory tree SHA-256 | ref1 vs ref2 smoke |
|---|---|---|---:|
| CAM++ | `damo/speech_campplus_sv_zh-cn_16k-common` | `f01588f1ceacd25fb3853e765846e3e34befddcff5c61bc14ddaf529745ed0c3` | 0.93460 |
| ERes2NetV2 | `iic/speech_eres2netv2_sv_zh-cn_16k-common` | `c2a9103af9b3b3ee66702de521bf53355119568096e52fcc14fe562000939f1a` | 0.95205 |

The CAM++ tree hash exactly equals the intended production model hash registered in this repo's
current voiceprint profile. This closes checkpoint drift relative to the repo-registered baseline;
the live `/opt/bilive/autoslice/models/campp` directory was not re-hashed during this local run.
The ERes2NetV2 download was reached initially through a mutable `master` snapshot name, so its
tree hash—not that name—is the durable identity for this result.

The different same-speaker smoke scores already demonstrate different score scales. The
ERes2NetV2 model card's 0.360 threshold and every CAM++ policy threshold are unrelated to the
truth-fitted oracle boundaries below; none is transferable without recalibration.

## 5. Complete numeric results

### Primary: non-mixed cues

The displayed threshold rounds to the same six decimals as its max-guest boundary; internally it
is the next representable float above that boundary.

| Session | Model | n (H/G) | max guest / oracle threshold | binding guest | false 李豆沙 | false 连线 cues | short n (H/G) | short false 连线 | rank AUC | short AUC |
|---|---|---:|---:|---:|---:|---|---:|---|---:|---:|
| A | CAM++ | 55 (22/33) | 0.419920 / 0.419920… | 37 | 0 | 3: 24, 58, 61 | 23 (6/17) | 1: 58 | 0.9945 | 1.0000 |
| A | ERes2NetV2 | 55 (22/33) | 0.409110 / 0.409110… | 38 | 0 | 1: 58 | 23 (6/17) | 1: 58 | 0.9945 | 1.0000 |
| B | CAM++ | 33 (26/7) | 0.352280 / 0.352280… | 16 | 0 | 1: 17 | 12 (8/4) | 1: 17 | 0.9945 | 0.9688 |
| B | ERes2NetV2 | 33 (26/7) | 0.475280 / 0.475280… | 16 | 0 | 4: 15, 17, 19, 32 | 12 (8/4) | 4: 15, 17, 19, 32 | 0.9725 | 0.8438 |

Session B explains the rejection. Its ERes2NetV2 boundary is forced above guest cue 16 at
0.47528; host cues 15, 17, 19 and 32 then all fall below that boundary. Each is shorter than
1500 ms. CAM++ loses only cue 17 under the same zero-false-李豆沙 constraint.

### Mixed-cue first-segment sensitivity

Including a mixed cue's entire waveform under its first segment's label is intentionally not a
primary metric. It does not rescue ERes2NetV2:

| Session | Model | false 连线 at false 李豆沙 = 0 | short false 连线 | rank AUC | short AUC |
|---|---|---:|---:|---:|---:|
| A, all 61 | CAM++ | 10 | 4 | 0.9703 | 1.0000 |
| A, all 61 | ERes2NetV2 | 10 | 3 | 0.9593 | 1.0000 |
| B, all 40 | CAM++ | 11 | 5 | 0.9283 | 0.9500 |
| B, all 40 | ERes2NetV2 | 11 | 5 | 0.8996 | 0.7500 |

### Cross-session threshold transfer diagnostic

Even a threshold derived for one session does not transfer safely to the other session within
the same model family:

| Model | Calibration → test | false 李豆沙 | false 连线 |
|---|---|---:|---:|
| CAM++ | A → B | 0 | 1 |
| CAM++ | B → A | 2 (A cues 37, 38) | 0 |
| ERes2NetV2 | A → B | 1 (B cue 16) | 2 (B cues 17, 32) |
| ERes2NetV2 | B → A | 0 | 3 (A cues 25, 58, 61) |

This diagnostic reinforces the scale/calibration warning: the per-session oracle thresholds are
comparison tools, not production constants.

## 6. Recommendation: do not switch

Keep CAM++ as the production speaker model and do not rebind the production voiceprint profile,
anchors, policies or host-vocal proofs to this ERes2NetV2 snapshot. ERes2NetV2's local API is
technically compatible enough to return 192-dimensional single-input embeddings: on enrollment
refs 1 and 2, pair score versus embedding/cosine differed by only `2.14e-06` (CAM++ control:
`2.62e-06`). That removes one interface uncertainty but does not overcome the failed quality
gate, and it was not an exact production-venv/full-finalizer replay.

The decision is specifically “do not switch to this ERes2NetV2 snapshot on this evidence,” not a
claim that ERes2NetV2 is universally inferior. A future revisit would need additional guests and
sessions, then the full production extraction/finalizer chain. Post-hoc threshold tuning on these
same 101 truth-labelled cues should not be used to reverse the pre-analysis gate result.

## 7. Production switch requirements if a future candidate passes

These steps are documented for completeness; **they are not authorized or recommended for the
current ERes2NetV2 result.**

1. Install the exact candidate snapshot additively (for example under
   `/opt/bilive/autoslice/models/eres2netv2`), compute the repository's directory-tree SHA-256,
   and leave `/opt/bilive/autoslice/models/campp` intact for rollback. Pin by bytes/tree hash,
   not a mutable hub revision name.
2. Create a separate versioned ERes profile and an explicit talk model/profile selector. The
   current `voiceprint_profile.v1.json` has one singular `model` object; it cannot hold a parallel
   second hash entry. `host_vocal_proof.py:638` is a general supplied-directory-versus-profile
   tree-hash gate, not the actual hard-coded CAM++ selection. Current hard-coded CAM++ paths are
   in `producer_speaker.py` and deployment validation; the talk dispatch must propagate the new
   selector instead of silently retaining its CAM defaults. Host-vocal routing already has
   environment selectors. Keep the existing CAM profile immutable for rollback.
3. In the exact production venv, verify pair-score versus `output_emb=True` +
   `compute_cos_similarity` parity, 192-value shape, float32/tolerance behavior, model-aware cache
   fingerprints and separation between CAM++ and ERes caches. The WSL probe in §6 passes only the
   basic API/shape contract. Generalize the profile contract to declare model family and embedding
   dimension; version the CAM-specific cache schema and decision/provenance fields (including
   `campp_audio` and `campp_invoked`) so an ERes decision is never serialized as CAM++ evidence.
4. Re-derive every raw-score gate using independent ERes calibration data. For talk these include
   `host_session_seed_min`, `guest_seed_max`, `guest_session_similarity_max`,
   `guest_cluster_similarity_min`, `ambiguity_band`, `single_host_median_seed_min`, and the
   semantic corroboration margin `0.08`. The per-session two-means threshold is self-derived, but
   the surrounding gates are not. Duration-only values such as `short_cue_ms` may remain.
5. Do not infer singing calibration from this talk pilot. Prefer leaving the host-vocal/song path
   on CAM++. If that path is also changed, re-derive `minimum_session_enroll_median=0.50`, direct
   checkpoint `0.31`, and session-bridge lyric score `0.22`; update the compiled fail-closed policy
   and profile bytes together. The `host_vocal_proof.py:638` tree gate must then bind the new
   model directory to that new profile. The current profile preserves a legacy
   `minimum_session_lyric_score=0.31` for profile-hash stability, while runtime proof generation
   and verification use the canonical compiled bridge gate `0.22`; do not recalibrate only that
   legacy JSON field or update profile JSON without the compiled policy/proof schema.
6. Reuse the three raw enrollment WAVs unchanged if their bytes remain identical; no new recording
   is required. Recompute and rebind all model-dependent data: reference embeddings/caches, the
   three source-session anchor manifests and their profile/model/scores, and the three
   paired `source_session_anchor_path` + `source_session_anchor_sha256` entries in
   `speaker_batch_plans/2026-07-09.json`. Preserve the runtime score-recompute tolerance of
   `1e-5`. Version or partition CAM and ERes anchor sets, and make deployment validation
   selector/model-family-aware: the current deploy check validates every
   `speaker_session_anchors/*.json` against one hard-coded profile and `/models/campp`, so a
   mixed-family directory would otherwise fail closed or be checked against the wrong runtime.
7. Regenerate host-vocal proofs if their canonical profile/model binding changes. Existing proofs
   bind exact profile SHA, model ID/tree and reference hashes and cannot be silently reused.
8. Replay both truth sets through the full production `speaker_finalizer.py` path with production
   ±150 ms extraction handles, anchor-bank construction, two-means, margin policy and cache. Any
   automatic-SRT change invalidates the hash-bound override base and requires re-review/rebinding.
9. Canary into isolated output with `speaker_mode=required`. `FAST_SOLO` bypasses the acoustic
   model and therefore cannot validate a model migration. Cut over under the runner lock only
   after deployment validation succeeds.
10. Roll back atomically: restore the CAM selector, exact old profile/policy bytes, old anchors,
    batch-plan hashes and proof bindings together. Merely pointing back to the old model directory
    is insufficient.
11. Update the authoritative pipeline documentation that currently promises CAM++ behavior,
    including `docs/pipeline/40-subtitle-text.md` and `docs/pipeline/50-song-lane.md`, only as part
    of a verified future cutover.

## 8. Reproducibility and durable outputs

Repo source claims in this report were inspected at commit
`e6d36f0a50ad9735fe5f4e748796a084237b081a` on branch `claude/session-live-context`.
The migrated scorer now validates all inputs before inference, writes an atomic checkpoint after
every cue, records unrounded pipeline-returned floats, and resumes only when the input fingerprint
and model sources match. The complete scoring process exited 0 at `2026-08-08T22:28:09-04:00`.

Fresh-venv rebuild/install sequence used (pip itself was not upgraded):

```bash
mkdir -p \
  /home/ivan/Project/vtuber-reproduce/pilot/.cache/pip \
  /home/ivan/Project/vtuber-reproduce/pilot/.cache/modelscope/hub \
  /home/ivan/Project/vtuber-reproduce/pilot/.cache/modelscope/modules \
  /home/ivan/Project/vtuber-reproduce/pilot/.cache/torch \
  /home/ivan/Project/vtuber-reproduce/pilot/.cache/huggingface \
  /home/ivan/Project/vtuber-reproduce/pilot/.tmp
export PIP_CACHE_DIR=/home/ivan/Project/vtuber-reproduce/pilot/.cache/pip
export MODELSCOPE_CACHE=/home/ivan/Project/vtuber-reproduce/pilot/.cache/modelscope/hub
export MODELSCOPE_MODULES_CACHE=/home/ivan/Project/vtuber-reproduce/pilot/.cache/modelscope/modules
export TORCH_HOME=/home/ivan/Project/vtuber-reproduce/pilot/.cache/torch
export HF_HOME=/home/ivan/Project/vtuber-reproduce/pilot/.cache/huggingface
export XDG_CACHE_HOME=/home/ivan/Project/vtuber-reproduce/pilot/.cache
export TMPDIR=/home/ivan/Project/vtuber-reproduce/pilot/.tmp
/home/ivan/.local/bin/python3.11 -m venv \
  /home/ivan/Project/vtuber-reproduce/pilot/eres2-pilot-venv
/home/ivan/Project/vtuber-reproduce/pilot/eres2-pilot-venv/bin/python -m pip install \
  'setuptools==79.0.1'
/home/ivan/Project/vtuber-reproduce/pilot/eres2-pilot-venv/bin/python -m pip install \
  --index-url https://download.pytorch.org/whl/cpu \
  'torch==2.5.1+cpu' 'torchaudio==2.5.1+cpu'
/home/ivan/Project/vtuber-reproduce/pilot/eres2-pilot-venv/bin/python -m pip install \
  'modelscope==1.38.1' 'funasr==1.3.14' \
  addict datasets Pillow yapf simplejson sortedcontainers kaldiio oss2 einops
/home/ivan/Project/vtuber-reproduce/pilot/eres2-pilot-venv/bin/python -m pip check
```

The exact resolved environment is recorded in `pilot/eres2-pilot-venv.freeze.txt`, SHA-256
`661e68118d97c193375ec3ec6f68915acfeb3c13b9f02ce0fbbf57a200e1cea2`.

Commands used after the models were resolved to local directories:

```bash
cd /home/ivan/Project/vtuber-reproduce
pilot/eres2-pilot-venv/bin/python -u pilot/eres2_pilot.py \
  --campp-model pilot/.cache/modelscope/hub/models/damo--speech_campplus_sv_zh-cn_16k-common/snapshots/master \
  --eres2-model pilot/.cache/modelscope/hub/models/iic--speech_eres2netv2_sv_zh-cn_16k-common/snapshots/master
pilot/eres2-pilot-venv/bin/python pilot/analyze_pilot.py
pilot/eres2-pilot-venv/bin/python pilot/probe_embedding_contract.py
```

Durable local artifacts:

- `pilot/eres2_pilot.py` — SHA-256
  `fdf228a78707b1a556aee8d923d5ea5dfcc2e4ecfe9036d4bd81615dd4126499`
- `pilot/analyze_pilot.py` — SHA-256
  `ea2cdd2e1bb35c82bf45d66666311033e4d8d1a40099449fd36d71a39a24dece`
- `pilot/probe_embedding_contract.py` — SHA-256
  `fe867ff549e34322f661c1cb97f43d66bd6be8ea810982bbcdb62a6f1863e21d`
- `pilot/eres2-pilot-out/pilot_scores.json` — SHA-256
  `519040322a509250e457ae024072151b3cbfeb5b3e5fc3b3a371958370d4070f`
- `pilot/eres2-pilot-out/pilot_analysis.json` — SHA-256
  `526e3990137fa29502133b31cf1909018ae699475be417e11f9769dbee69817a`
- `pilot/eres2-pilot-out/pilot_analysis.md` — SHA-256
  `685c35e105934efb3de811159e2c7f84c694bd30b5dc5b11316a78978234ed27`
- `pilot/eres2-pilot-out/embedding_contract_probe.json` — SHA-256
  `3e39d04ca13597cff9a23332158c6c20caf8eab4297a646f22ed5e87bb2fd2aa`
- `logs/pilot.log` — append-only progress and completion record.

The deterministic analyzer asserted exact 61 + 40 session grids, 13 mixed and 88 non-mixed cues,
three finite `[-1, 1]` scores per model/cue, and exactly 606 reference comparisons. A separate
minimal recomputation independently reproduced all four primary boundaries and false-连线 cue
lists.

Observed write scope: repo `git status --porcelain` was clean at the start of this resumed run and
shows only this requested report modified at handoff. Task-generated runtime files are under
`/home/ivan/Project/vtuber-reproduce/pilot/` and the append-only
`/home/ivan/Project/vtuber-reproduce/logs/pilot.log`. This is a checked statement about the
observed baseline and final state, not a claim about unrelated filesystem history.

## 9. Remaining limits

- Only two sessions, one equal-quality guest condition and one fixed three-reference enrollment
  bank were tested. The result is sufficient to reject this candidate under the requested gate,
  but it is not a population-level model ranking.
- Thresholds are fitted with truth in-sample. Cross-session transfer already shows drift.
- Thirteen mixed cues are excluded from the primary metric; model replacement does not solve
  overlap/mixed-speaker segmentation.
- Exact SRT windows use zero handles, while production may add bounded ±150 ms context.
- The full production anchor-bank/finalizer, cache lifecycle and singing host-vocal path were not
  replayed. The local 192-dimensional embedding probe is narrower than that validation.
- Python 3.12 was not exercised. Any forced 3.12 rebuild is a new environment and must be
  reverified rather than assumed equivalent to this Python 3.11.15 result.

## 10. Attack/defense review record

Default round count: 3. Actual round count: 3 (Round 3 used a disclosed non-Pro fallback).
Reduction gates were not applicable because this is a scientific result with a production-change
recommendation. Fewer rounds were not used. The last Round 2-reviewed report snapshot was SHA-256
`4be35efd6f4f7569df145591ad2813e3c4316629634b88be74a973bfcee714bc`.

### Round 1 — plan/method attack

Execution: independent data/method subagent, read-only. Accepted findings recovered Session B's
exact hash-bound machine-label base; excluded mixed cues from the primary gate; formalized the
oracle threshold/tie rule and short-cue gate; added input/model tree hashing and per-cue atomic
checkpoints; and separated the direct enrollment/cue oracle from a production-finalizer replay.
The initial P0 truth gap and all P1 method/reproducibility findings were resolved before analysis.

### Round 2 — completed artifact attack

Execution: two independent read-only subagents.

- The statistics reviewer independently matched the 101/13/88 grid, all 606 scores and medians,
  all four boundaries/error lists, AUCs, candidate gate, truth reconstruction, cue WAV contracts
  and output hashes. It found no correctness issue. Its P3 wording concern was accepted: pooled
  AUC is now explicitly an observed-set, cross-session diagnostic rather than a stratified
  estimate.
- The production/source reviewer found two P1 migration gaps, both accepted: a selector alone
  would leave talk dispatch/deploy anchor validation tied to CAM++, and ERes decisions could still
  be serialized under CAM-specific cache/provenance names. The checklist now requires selector
  propagation, model-family-aware deploy validation, partitioned anchors, explicit family/dimension
  contracts and versioned provenance/cache fields. P2 fixes added path+hash anchor rebinding, the
  legacy-profile-0.31 versus canonical-runtime-0.22 split, authoritative docs, repo/live hash
  wording, exact cache/venv/freeze/script provenance. Targeted re-review found no unresolved
  P0/P1/P2.

### Round 3 — ChatGPT Pro goal-alignment review

Pro status: **blocked before submission; fallback-not-Pro**. The visible WSL Chromium/CDP route was
healthy and authenticated, but exact mode selection failed closed. `Pro Extended` and then `Pro`
were each absent; the only visible candidates were `Advanced`, `Model GPT-5.6 Sol` and
`Effort Extra High`. The two durable records are
`2026-08-09T02-43-19-933Z-7c6b88fe5a79c4af.json` and
`2026-08-09T02-44-02-292Z-7c6b88fe5a79c4af.json` under the pinned Hermes run directory, both in
`mode_not_confirmed` state with zero user/assistant turns. No prompt was transmitted, no thread or
conversation ID was created, and no Pro response exists. `Extra High` is not represented as Pro.

Fallback execution: a fresh independent goal-alignment subagent, read-only and explicitly not
ChatGPT Pro, reviewed report SHA-256
`4a200a0fd6e2b4bfe346ac0b3d70f2cc7d239aa288a1aa3e07bd0cb4e21ec877` against the original user
request. It found no P0, no privacy issue and no unsupported switch verdict. Its two P1 handoff
findings were accepted here: replace the stale pending-Pro status with the exact blocker/fallback
record, and create a real unified integrator diff. Its P2 write-scope wording was also accepted.
The final diff is generated after this record update at
`pilot/2026-08-08-eres2netv2-pilot.diff` and checked as a reversible report-only patch.
