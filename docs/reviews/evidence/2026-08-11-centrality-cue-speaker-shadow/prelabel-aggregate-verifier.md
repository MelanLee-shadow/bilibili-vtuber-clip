# Exact 10+5 holdout ASR/cue aggregate verifier (2026-08-11 10:13Z)

This heartbeat completed the local, hash-bound aggregate verifier for the two
unlabeled holdout sessions. It did **not** run ffmpeg or BCUT on `free`, upload
audio, create a real v1 plan or segment receipt, open human truth, run speaker
predictions, write production state, deploy, publish, or push.

## Outcome

`scripts/finalize_speaker_holdout_prelabel.py` can seal an aggregate only when
all of the following are true:

- the execution-authorized v1 plan, verifier, run-one core, wrapper, planner,
  Python, ffmpeg, and ASR client still match their exact bindings;
- the two fixed-hash accepted source-freeze replays have identical deterministic
  populations and the plan contains exactly those members: 10 from 2026-08-10
  and 5 from 2026-08-11, matching every date, role, segment ID, source path,
  size, mtime, media hash, adapter-target hash, and duration;
- every member has exactly one receipt-last success attempt; partial failed
  attempts remain disclosed but cannot count as success;
- every artifact size/hash replays, normalized ASR and cue bounds replay, SRT is
  byte-identical, and the persisted MP3 decodes through the plan-bound ffmpeg to
  the exact canonical PCM;
- no canonical PCM is duplicated across the two sessions, and all cue tables
  remain `truth_state=UNLABELED_LOCKED`, `prediction_state=NOT_RUN`.

The final status is deliberately
`ASR_CUE_PACKAGE_FROZEN_PREDICTIONS_NOT_RUN`. It does not claim that a prelabel
prediction package, human truth, production gate, deployment, or upload is
authorized.

## Concurrency and publication

Run-one and aggregate finalization now share one private run-level `flock`.
Run-one checks for the aggregate seal while holding that lock and stops before
audio/provider work. Finalization holds it across two independent complete
materializations and create-only publication. The aggregate receipt uses a
private fsynced temp file, hard-link `O_EXCL` publication, parent-directory
fsync, temp unlink, and a second directory fsync. Newly created lock/attempt
names are parent-fsynced before any external provider can run.

An independent read-only review found one high-severity gap in the first draft:
it proved only the 10+5 counts, not exact membership. The final implementation
parses both fixed-hash replays and requires full member equality. A negative
canary replaces one planned member and recomputes the plan and file hashes; the
aggregate still fails before publication. The reviewer found no remaining
material blocker after that fix.

## Verification

- planner/run-one/aggregate plus runtime-architecture focus: **67 passed**;
- full repository regression: **4352 passed**, 0 failed, 2 third-party
  deprecation warnings, 76.12 seconds;
- changed-file Ruff, `py_compile`, aggregate CLI `--help`, and
  `git diff --check`: passed.

Final pre-commit SHA-256 values:

| file | SHA-256 |
|---|---|
| planner | `a1508a52f8e4dfef1d96bf40a544464f5564c76b080b47deefcd086d4135b863` |
| run-one wrapper | `1e88d08934b29fd3ce0988af7055adcb815fa124c5169b6318d0ee8066d526f3` |
| aggregate verifier | `4e4a9ab9d52b67084458d82e75208ec17a946abe099e5a74eac90e4fb8908b02` |
| shared core | `7ccb7040990c174873966702f98a1973f6eebfaa059968d1f53f5e1587a50c5e` |
| aggregate canaries | `233e4ad0df62042a238e935fbd44658902f6a81290b2a1992447ebddc830d983` |

## Exact stop

No execution-authorized v1 plan or real 15-member extraction receipts exist.
Creating them requires BCUT resource creation, external audio upload, and task
creation, which are outside the current authority. After those receipts exist,
the next leakage-safe state is to freeze prediction bytes before any human
label is opened. The two locked cross-session human truth sets remain a real
listening blocker; acceptance gates are unchanged and must not be lowered.
