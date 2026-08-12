# ChatGPT Pro decision: keep the exact reviewed story title

- Thread: <https://chatgpt.com/c/6a7bcf0d-2c78-83ea-b676-54eeaa3b6cf8>
- Visible mode: `Pro`
- Prompt SHA-256: `d03ddbd21e9e01aac9a2159dec4bf2d307dc605b7af97b92013bc58c0f8f3d2f`
- Normalized response SHA-256: `426fb1e4205923301f9674d3ed962414b0a1c6ac5d30e7a8d6b637aba66489c6`
- Durable run record: `/Users/ivan/.codex/chatgpt-pro-consult-runs/2026-08-12T01-39-53-393Z-d03ddbd21e9e01aa.json`

## Accepted decision

Keep the exact Ivan-approved title:

`【李豆沙】莉娅求小李“就算你是狼也放过我”，结伴后小李突然连声道歉`

The transcript proves a proposal to go together, oral acceptance, and then the
apology. `结伴后` is therefore an adequately supported compressed title surface,
not a hard factual contradiction. The CPA proposal `答应一起走后` is a reasonable
literal hedge, but Ivan did not authorize replacing that independently approved
span. Preserve the original title bytes and record the hedge as non-blocking
dissent.

This conclusion uses Ivan's existing authority; Pro is not a new publication
authority. The exact machine-title snapshot Ivan reviewed must be recoverable and
hash-bound. If it is not, the title remains blocked for a new Ivan decision.

## Required P0 contract

Add one typed candidate- and finding-scoped keep authority. It must bind:

- candidate id and title field;
- exact approved title and displayed machine-title snapshot hashes;
- Ivan's title-delta approval and later publication-authorization messages;
- only the enumerated `莉亚` -> `莉娅` entity-surface transform and its entity
  authority;
- source media, exact reviewed interval, reviewed plain SRT, reviewed speaker SRT,
  and entity-authority hashes;
- the exact blocked source-fact receipt/finding fingerprint, disputed span
  `结伴后`, and disposition `KEEP_EXACT_OPERATOR_APPROVED_VALUE`;
- a self hash and `record_dissent=true`.

The gate may resolve only an exact `SUPPORTED_COMPRESSION_HEDGE`. It must keep the
blocked adjudication and dissent visible, require zero other unmatched blocking
findings, preserve the approved title hash, and emit
`PASS_WITH_RECORDED_DISSENT`. It must never become a general manual-title bypass
or authorize the model's replacement title.

## Required tests

- exact current candidate/receipt/title/truth bindings pass without title-byte
  mutation;
- the model-proposed title has a different hash and remains unauthorized;
- any unrelated actor, addressee, entity, event, or temporal contradiction still
  blocks;
- finding class/span/evidence/proposal/receipt drift blocks;
- media, interval, plain SRT, speaker SRT, or entity-authority drift blocks;
- candidate reuse and missing exact reviewed-title snapshot block;
- hard factual contradictions can never be relabelled as a compression hedge.

## Root disposition

Implement only the narrow keep-original contract, then regenerate the story
package from immutable inputs. Do not rerun the provider until it samples `KEEP`,
do not accept `答应一起走后` under the existing user quote, and do not suppress the
source-fact report.
