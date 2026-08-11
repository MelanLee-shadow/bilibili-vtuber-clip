# Hash-bound holdout BCUT wrapper (2026-08-11 09:35Z)

This heartbeat implemented and mock-tested the bounded execution adapter that
the v1 prelabel plan can bind. It did **not** authorize or perform a BCUT
resource creation, audio upload, ASR task, media decode on `free`, truth reveal,
prediction, production write, deployment, or publication.

## Outcome and authority boundary

The wrapper at `scripts/run_speaker_holdout_prelabel_one.py` has two distinct
surfaces:

1. With only `--plan` and its expected file SHA, it performs neutral
   validate-only inspection and reports
   `VALIDATED_ONLY_EXECUTION_NOT_ATTEMPTED`.
2. Segment execution additionally requires exact `--segment-id` and
   `--attempt-id`, a v1 plan whose status and three execution-authority fields
   explicitly authorize the external upload, and exact bindings for the
   wrapper, core, planner, Python, ffmpeg, ASR client, and fixed parameters.

The current planner deliberately emits
`BOUND_EXTERNAL_UPLOAD_NOT_AUTHORIZED`, `plan_only=true`, and
`external_audio_upload_authorized=false`. Therefore the newly implemented path
is unreachable from every plan produced in this heartbeat. Changing local code
or booleans alone is not an authority receipt.

## Fixed execution contract

- Scratch authority is the exact future root
  `/tmp/hostocc-v2-20260811/speaker-prelabel-plan-v1`; the plan's allowed root,
  plan parent, run parent, create-only policy, and exact forbidden production
  roots must match before any client source is loaded or process is spawned.
- The core opens the scratch root only if it is a current-owner, mode-0700,
  non-symlink directory. Attempt directories and artifacts are opened relative
  to directory descriptors with no-follow/create-only semantics. Every newly
  created directory entry is parent-fsynced before source extraction or provider
  access, making same-attempt suppression crash-durable; the success receipt is
  still written last.
- Source to ASR MP3 and MP3 to canonical PCM use two exact, hash-bound ffmpeg
  argument vectors. Input/output media use pipes only; there is no `-y`,
  tempfile, arbitrary output path, shell command, or inherited process
  environment. The MP3 is fixed to mono 16 kHz/64 kbit/s; PCM is mono 16 kHz
  signed little-endian 16-bit.
- The ASR client bytes are opened once by descriptor, hash-checked, compiled,
  and executed only inside the final transcribe callback. The wrapper calls
  `transcribe_bcut()` directly with model 7, the exact BCUT base URL, 3-second
  polling, and a 900-second timeout. The `auto`, Jianying, and Kuaishou routes
  are not reachable.
- Ambient HTTP/HTTPS/ALL proxy variables, `SSLKEYLOGFILE`, `SSL_CERT_FILE`, and
  `SSL_CERT_DIR` fail closed before ASR client source execution. This prevents
  an out-of-scratch TLS key-log write and ambient trust-root drift. The wrapper
  adds the explicit `provider=bcut` field required by the deterministic
  normalizer and removes elapsed-time observations from the normalized payload.
- Plan JSON is now read, hashed, UTF-8 decoded, and parsed from the same file
  descriptor. Inode/stat/path rechecks prevent a same-UID rename swap from
  validating one file hash and parsing another self-consistent plan.

Final local code SHA-256 values before commit:

| file | SHA-256 |
|---|---|
| planner | `1ff049c220188eb306ffc5518c229d174021db68b62bbacd5560bc1a53ca264c` |
| wrapper | `9c8566b61b45da305455f529817edf904b379bb120cbca965bde2e03a8bce4af` |
| run-one core | `b75c6df15ef951f15ff21ce73f5413cb340dc3c5fdc97834d6f63915771b0e98` |

## Deterministic and negative canaries

The focused suite locks the following behavior:

- both complete ffmpeg argument lists, fixed subprocess environment, direct
  BCUT call, model/base URL, poll interval, and timeout;
- v0 validate-only, v1 `plan_only`, missing execution authority, wrapper/core/
  planner/Python/ffmpeg/client hash drift, artifact-contract drift, `/opt`
  scratch escape, proxy injection, source stat/byte drift, and plan rename-swap
  all stop before provider access;
- provider failure cannot publish a success receipt, and retrying the same
  attempt cannot perform a second upload; parent-directory fsync occurs before
  source extraction or provider access;
- malformed, overlapping, empty, or out-of-PCM cue results fail or are retained
  according to the frozen contract; elapsed-only variation cannot change the
  deterministic normalized result;
- successful in-memory execution creates exactly the five artifacts plus the
  receipt at the plan-declared attempt path, with truth still
  `UNLABELED_LOCKED` and prediction still `NOT_RUN`.

Verification results:

- focused planner/run-one suite: **48 passed**;
- changed-file Ruff, `py_compile`, CLI `--help`, and `git diff --check`: passed;
- full repository regression after the final durability fix: **4342 passed**,
  0 failed, 2 third-party deprecation warnings, 69.10 seconds.

## `free` validate-only smoke

A new create-only review directory was written only below the authorized
scratch namespace:

`/tmp/hostocc-v2-20260811/speaker-prelabel-plan-v0/run-one-wrapper-validate-v4`

It contains only the wrapper and core above; their remote hashes equal the
local hashes. Against the immutable v0 plan file SHA
`3ad31f1c4e32cfee7d2f270c353930a7216c7b077f9140b8294f34e7b406a263`,
the wrapper returned:

```json
{"external_audio_upload_authorized":false,"plan_payload_sha256":"sha256:f22ab3eb5fffe243199809d941ff17a99c4e07df6d880e27ba9f199100b94a8a","plan_status":"EXTRACTION_PLAN_FROZEN_EXECUTION_NOT_AUTHORIZED","segment_count":15,"status":"VALIDATED_ONLY_EXECUTION_NOT_ATTEMPTED"}
```

No segment/attempt was supplied. The legacy artifact root and the v1 plan root
both remained absent, proving this smoke did not decode or upload media and did
not publish an extraction receipt.

## Remaining stop and next bounded step

There is still no authorized v1 plan or external-upload authority. Real BCUT
execution creates a remote resource, uploads audio, and creates an ASR task;
that side effect is outside this heartbeat's permission and must not be inferred
from an implemented wrapper. The two holdouts also remain unlabeled; human
listening is still a hard blocker and no machine truth may be invented.

The next leakage-safe code-only step is the aggregate receipt verifier: require
all 15 frozen members (10 + 5 by session), exactly one valid success receipt per
member, replay every artifact/cue/SRT hash and bounds check, reject cross-session
PCM duplicates, preserve truth/prediction false, and publish one aggregate
receipt create-only. Actual extraction must wait for separate external-upload
authority; production integration and deployment remain later, independently
authorized stages.
