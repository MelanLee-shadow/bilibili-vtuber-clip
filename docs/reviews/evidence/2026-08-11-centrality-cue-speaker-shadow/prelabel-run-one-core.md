# Holdout prelabel `run-one` core (2026-08-11 08:26Z)

This heartbeat implemented and mock-tested the receipt-last core for one
holdout segment.  It did not authorize or call a real provider.  The immutable
plan from the previous heartbeat remains execution-blocked and was not edited.

## Implementation boundary

- `src/autoslice/speaker_holdout_prelabel.py` validates the immutable plan,
  exact source member/stat/SHA, runner hash, safe attempt ID, provider/model,
  canonical PCM, ordered cue/word bounds, and all authority flags.
- `scripts/run_speaker_holdout_prelabel_one.py` is deliberately validate-only.
  It has no CLI route that calls BCUT, ffmpeg, Jianying, Kuaishou, production
  runners, or arbitrary output paths.
- Real execution exists only as a dependency-injected core.  It requires a
  future plan status `EXTRACTION_PLAN_FROZEN_EXTERNAL_UPLOAD_AUTHORIZED`, both
  external-upload booleans true, and an exact path/SHA binding for the wrapper.
  The current plan satisfies none of those execution conditions.

If a future authorized caller is supplied, the core reserves a private,
create-only attempt directory before any provider callback.  Therefore the same
attempt ID cannot upload twice.  It derives exact ASR-input MP3 and raw
PCM16/16kHz/mono bytes, passes the same MP3 bytes to the injected BCUT callback,
removes nondeterministic `elapsed_s`, validates cue and word bounds against PCM,
and writes five artifacts before writing the success receipt last.  Provider
failure leaves an attempt without `extraction-receipt.json`; retry requires a
new attempt ID.

Empty-ASR segments are retained as `ASR_EMPTY_SEGMENT_RETAINED`, not silently
dropped.  Cue tables remain `truth_state=UNLABELED_LOCKED` and
`prediction_state=NOT_RUN`.  Receipts explicitly keep aggregate ASR,
predictions, human truth, production, and deployment authority false.

## Verification

- Focused planner/freezer/run-one tests: 45 passed.
- Run-one-specific tests: 16 passed.  Canaries cover unauthorized current plan,
  source drift before provider, wrapper/plan drift, duplicate attempts before a
  second upload, provider failure without receipt, malformed/overlapping/
  out-of-bounds cues, word bounds, empty ASR retention, and elapsed-only hash
  stability.
- Full repository regression: 4331 passed, 0 failed, with 2 third-party
  deprecation warnings, in 68.00 seconds.
- Changed-file Ruff, `py_compile`, CLI help, and `git diff --check` passed.

Remote validate-only smoke used exact local bytes:

- core SHA-256:
  `0f0cfcda33ab6ea213b07abbf62a6978f900cd3913105c775dd88a7460e6ab45`;
- CLI SHA-256:
  `f84d2beff04eca3a49af7675b5a86f4580e89b14877cf2cabc73ea140d4278c3`;
- scratch:
  `free:/tmp/hostocc-v2-20260811/speaker-prelabel-plan-v0/run-one-validate-v1/`.

The first transfer into the fresh v1 directory placed identical flat copies at
its root before the repository-shaped `src/autoslice/` and `scripts/` paths
were added.  They were not executed or removed; the smoke hash-checked and ran
the repository-shaped paths above.  No existing scratch byte was overwritten.

The smoke read the existing plan file SHA
`3ad31f1c4e32cfee7d2f270c353930a7216c7b077f9140b8294f34e7b406a263`
and returned:

```text
status=VALIDATED_ONLY_EXTERNAL_PROVIDER_EXECUTION_NOT_IMPLEMENTED
plan_status=EXTRACTION_PLAN_FROZEN_EXECUTION_NOT_AUTHORIZED
segment_count=15
external_audio_upload_authorized=false
```

The planned run root still does not exist.  No media decode, ASR resource,
audio upload, cue artifact, prediction, truth, production state, or deployment
action occurred.

## Next stop

The next possible deterministic step is to freeze a new plan version that binds
the finished wrapper plus exact ffmpeg/runtime fingerprints.  It must still keep
external upload false unless Ivan grants that separate action.  Without such
authority, no real ASR segment may run; without frozen ASR/cues/predictions, the
holdout remains unopened.
