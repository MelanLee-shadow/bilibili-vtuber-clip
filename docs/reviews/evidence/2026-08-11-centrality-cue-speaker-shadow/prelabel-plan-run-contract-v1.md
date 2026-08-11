# Holdout prelabel plan/run contract v1 (2026-08-11 08:55Z)

This heartbeat repaired a pre-execution contract mismatch.  It did not build a
new remote plan, decode media, call BCUT, create ASR/cue artifacts, open truth,
or change production.

## Confirmed mismatch

The immutable v0 plan declared artifacts directly below
`segments/<segment-id>/`, including `asr-input-16k-mono-64k.mp3` and a raw ASR
JSON.  The run-one core instead reserved
`segments/<segment-id>/<attempt-id>/` and wrote `asr-input.mp3` plus
`asr.normalized.json`.  Planner and executor tests had no shared canary, so both
surfaces passed independently while describing incompatible namespaces.

Freezing a runtime-bound plan or writing an aggregate finalizer on top of that
mismatch would have created false authority.  The old remote v0 plan remains
immutable and execution-not-authorized.

## v1 contract

- New plans use `speaker-holdout-extraction-plan.v1` and the isolated future
  root `/tmp/hostocc-v2-20260811/speaker-prelabel-plan-v1`.
- Every segment declares the exact attempt template
  `segments/<segment-id>/{attempt_id}` and six exact artifact basenames:
  canonical PCM, 16 kHz mono MP3, normalized ASR JSON, cue table, SRT, and the
  receipt.
- Run-one validates every field and basename before source extraction, run-root
  creation, or provider invocation, then uses those plan-declared names.
- Legacy v0 remains readable by the validate-only CLI, but cannot execute even
  if someone forges its execution booleans and recomputes its payload hash.
- v1 toolchain hashes use one `sha256:<hex>` representation, matching the core's
  future wrapper-hash comparison.

## Canaries and verification

- A mutated artifact name (`asr.raw.json`) is rejected before provider or run
  root.
- A forged-authority v0 plan remains validate-only and never calls provider.
- The successful mock layout contains exactly the six v1 files under the exact
  attempt root and still publishes the receipt last.
- Plan/run-one focused suite: 39 passed.
- Changed-file Ruff and `py_compile` passed.
- Full repository regression: 4333 passed, 0 failed, with two third-party
  deprecation warnings, in 73.11 seconds.

## Remaining stop

No v1 remote plan has been frozen.  The CLI is still validate-only and no real
BCUT adapter or PCM decoder is exposed.  `transcribe_bcut()` also returns only
utterances, so a future adapter must explicitly add `provider=bcut`; it may not
use the auto/Jianying path.  Real BCUT still creates a resource, uploads audio,
and creates a remote task, so execution remains blocked on separate external
upload authority.

After a real wrapper is implemented and hash-bound, the next deterministic
stage is an aggregate verifier requiring all 15 segments, exact 10+5 session
membership, artifact replay, cross-session PCM duplicate rejection, no
truth/prediction authority, and create-only aggregate receipt-last output.
