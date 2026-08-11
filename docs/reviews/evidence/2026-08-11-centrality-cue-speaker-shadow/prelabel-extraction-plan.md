# Holdout prelabel extraction plan (2026-08-11 08:06Z)

This heartbeat advanced H1/H2 from source discovery to a frozen **plan only**.
It did not run ffmpeg, ASR, CAM++, prediction, or human labeling.  It is not a
`PRELABEL_PACKAGE_FROZEN` receipt and has no production/deployment authority.

## Accepted inputs

- Source-freeze deterministic payload:
  `sha256:c4e27632a0c279747697e3168d2a91cab535967a5d186fcd3e0c5cb0ff284184`.
- Accepted replay file hashes:
  `e80baec84ac8de4bca7de5a1edb265da732de2c4cf674aba29b8886c4c37de57` and
  `18c71fe21a3f4ab89c0233b053a6e030308b57c8f4eb56745c7d4576c4ba7370`.
- Exact inventory: room `22966160`, 2026-08-10 = 10 segments and
  2026-08-11 = 5 segments.  One room/date is one session.

The planner revalidated both manifest schemas, exact accepted hashes, source-only
authority, path/date/role/count uniqueness, current regular-file stat, and all 15
current source SHA-256 values.  It rejects the invalid 20-row v2 artifact and any
self-consistent replacement manifest that is not the accepted v3 pair.

## Planner and plan

- Local implementation: `scripts/build_speaker_holdout_prelabel_plan.py`.
- Planner bytes used on `free`:
  `e4ef1c0dd47ef888cbfe95debac402909f5ff63eb40f3157f1fbae0dd1e2abb0`.
- Private authorized scratch root (mode 0700):
  `free:/tmp/hostocc-v2-20260811/speaker-prelabel-plan-v0/`.
- Create-only plan:
  `speaker-prelabel-20260811-v0.plan.json`.
- Plan file SHA-256:
  `3ad31f1c4e32cfee7d2f270c353930a7216c7b077f9140b8294f34e7b406a263`.
- Deterministic plan payload:
  `sha256:f22ab3eb5fffe243199809d941ff17a99c4e07df6d880e27ba9f199100b94a8a`.

The plan fixes raw little-endian PCM16/16 kHz/mono, explicit BCUT provider/model
7, complete-inventory selection, `threshold_state=null`, cross-session canonical
PCM dedup, relative create-only artifact paths, and at most one future segment per
invocation.  `provider=auto`, Jianying failover, production runner entry points,
truth, predictions, and production paths are forbidden.

## Exact stop

The plan status is `EXTRACTION_PLAN_FROZEN_EXECUTION_NOT_AUTHORIZED` and its
toolchain says `BLOCKED_HASH_BOUND_RUNNER_NOT_IMPLEMENTED`.  Real BCUT execution
creates an external resource, uploads audio, and creates a remote ASR task; the
current heartbeat authorized only local repo writes and `free:/tmp` writes, so no
provider execution was attempted.  The planned run root does not exist, and no
PCM/MP3/ASR/SRT/cue/receipt file was created.

The next bounded engineering step is to implement and mock-test a `run-one`
wrapper with source revalidation, private attempt directories, normalized cue
validation, and receipt-last create-only publication.  A real provider run still
requires explicit external-upload authority.  Human truth remains blocked until
ASR/cues, the candidate algorithm, thresholds, and predictions are frozen.
