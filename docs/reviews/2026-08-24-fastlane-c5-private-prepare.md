# C5 candidate-private prepare — `auto_113028_1271_1328`

This is a durable local preparation record for fastlane ruling #5.  It is not
a runtime receipt, an apply record, an upload authorization, or a claim of
public delivery.  No provider, remote runtime, formal state, journal, upload,
or public surface was changed for this record.

## Frozen text authority

The sole candidate-content authority is Ivan's 2026-08-19 exhaustive ruling
#5, mirrored in
`docs/reviews/2026-08-19-ivan-review-batch-rulings.md`: around 0:36, the
mention of niji's summer and humming of
`《にぎにぎにじたうん！》`/`にじさんじ` was mistranscribed in Chinese and
must not be subtitled.  It does not authorize a broader rewrite or a new
manual-review gate.

The committed reviewed baseline is complete and self-consistent:

- source recording: `22966160_20260814-11-30-28.mp4`, SHA-256
  `20c2fc8ccf054c038eb2e308a9ed2a0bb00593de8c7e8895e87ae0b1ee13734c`;
  absolute interval `[1261170, 1376550)` ms;
- diagnostic source: 24 cues, SHA-256
  `5da5af9dba5ce3ff2fee7d585bda809eb3a9edb612a18868ace683cb94281043`;
- ledger: SHA-256
  `6fac5913ee143619c840483a8355e6ba63690f9bf47ddd27b3d34b9fcb5ce1a2`;
- exact release baseline: 21 cues, SHA-256
  `9f33f247deb405b409d50f294bbfab08db7d5f43086241dd736fac0498faf64b`;
- truth diff: SHA-256
  `48142b7ebf737dde41472757030385794deb43ba9091289d1f4962885fd446b1`.

Only source cues 13--15, `[28700, 36140)` ms in the clip (absolute
`[1289870, 1297310)` ms), are `OPERATOR_DROP`; all other source cues are
`OPERATOR_UNCHANGED_FREEZE`.  The baseline explicitly claims text authority
only (`speaker_authority=NOT_CLAIMED_TEXT_ONLY`).

## Local re-materialization

At release-base commit `adc80a5aaf97088635b7b95479a5c89cc2caf2d6`, the
deterministic baseline compiler was run only against the committed source SRT,
reviewed SRT, and decision ledger.  Its re-materialized release SRT,
diagnostic SRT, decision ledger, and truth diff were byte-identical to the
four committed authority artifacts above.  The reconstructed manifest SHA-256
was `27aa4c2135e12ac7db34c2fb324db1be25bc41b7a407d7ae1af0806cb811e89b`;
the local compiler receipt SHA-256 was
`3fcd46ffa8cba518fc4a79c9d304a08a01d45883d7603060de6ca2e3e1d22d6d`.

The historic private human-review material remains diagnostic only: its
reviewed SRT has the same baseline SHA, while its template is still
`PENDING_HUMAN_REVIEW`.  It neither adds text authority nor creates an Ivan
re-review requirement.

## Title and cover

Ruling #5 does not name a title or cover change.  Therefore no candidate title
or cover is modified here.  A future package run may perform ordinary
hash/route/pixel consistency checks on whichever current artifacts the
authoritative run materializes, but must not use the niji crawler/hotspot
guidance to mutate C5's title, cover, or frozen subtitle text.

## Next one-shot action

After the runtime owner performs its normal live source/state/public
preflight, the minimal technical action is one candidate-private full dry run:

```text
python3 scripts/replay_reviewed_subtitle_baseline.py --full-dry-run \
  --runtime-root /opt/bilive/autoslice --date 2026-08-14 \
  --candidate-id auto_113028_1271_1328 \
  --private-stage-parent <new-private-0700-directory>
```

It must remain no-apply and no-upload.  The 2026-08-24 readiness snapshot had
no verified public BVID for C5, so the intended publication lane is **new BV**
if that preflight still finds none.  If the required fresh public/Creator
readback finds an existing BVID, the action must stop and route to **same-BV**;
this private record does not assert either public state.
