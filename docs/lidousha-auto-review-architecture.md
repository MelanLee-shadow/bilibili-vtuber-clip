# 李豆沙 autoslice 架构

> 当前高层结构图，2026-07-24。本文件不定义准入、schema、阈值或状态机；现行规则只读
> [pipeline/README.md](pipeline/README.md) 的对应 step 及其指向的机器强制层。

```text
Recorder authority
  BililiveRecorder closure + source integrity
          │
          ▼
Selection
  semantic recall → Tier/calibration → exact or ordinary lifecycle
          │
          ▼
Boundary
  candidate anchor + padded source context
  → candidate-relative story scope / frozen boundary-owner contract
  → typed endpoint authority + source/final-delivery semantic review
          │
          ▼
Text
  ASR/LRC → whole-clip/session context → entity/chat/acoustic adjudication
  → source truth applied across padded context
  → reviewed baseline restored as text authority, then higher truth replayed
  → final interval classifies inside / context-only / invalid straddle-or-mixed
  → clean SRT + speaker surfaces + strict final review
          │
          ▼
Story surfaces
  one StoryContract → subtitle / archive title / cover task
  → shared title choke point
  → screenshot or AI route → final-pixel/participant/glyph proof
          │
          ▼
Final bytes and package
  talk intro roster or song no-intro → burn → artifact hashes
  → portable package → current canonical audit
          │
          ▼
Delivery
  ├─ no-upload review
  ├─ Ivan-authorized new-BV lane
  └─ exact same-BV lane:
     evidence-v2 template → actual final-byte review → create-only receipt
     + separate Ivan repair authorization → durable repair state machine
     → fresh verify-live → create-only completed sidecar
```

## 作用域关系

- 选片锚点、candidate-relative story scope、padded text context 与最终交付区间是四个不同
  作用域。边界所有权只从 story scope 冻结；padded context 仍可承载必须正确落字的证据。
- reviewed baseline 是最终文字映射权威，不是 boundary owner。source truth 的最终可见性由
  resolver 后的交付区间重新分类；具体 inside/context-only/straddle 契约只读
  [30-boundary.md](pipeline/30-boundary.md)、
  [40-subtitle-text.md](pipeline/40-subtitle-text.md) 与
  [80-package-delivery.md](pipeline/80-package-delivery.md)。
- 截图与 AI 都只是封面路线。源帧事实、文字/版式叙事、截图帧绑定和最终人物/像素证明只读
  [70-cover.md](pipeline/70-cover.md)。
- package audit、最终感知复核、上传授权、repair journal 与 fresh public completion 是不同
  证据面；完整操作和完成条件只读
  [90-publish.md](pipeline/90-publish.md)。

## 代码入口

- runner：`scripts/free_session_autoslice.py`
- selection：`src/autoslice/candidate_selection.py`
- boundary scope/owner：`src/autoslice/producer_boundary_owner_contract.py`、
  `src/autoslice/producer_boundary_resolution.py`
- text/final interval：`src/autoslice/source_subtitle_truth.py`、
  `src/autoslice/producer_text_finalization.py`
- cover proof：`src/autoslice/publish_staging.py`、`src/autoslice/cover_repair.py`、
  `src/autoslice/cover_route_evidence.py`
- package audit：`scripts/audit_lidousha_review_package.py`
- final review/publish：`scripts/build_lidousha_final_human_review.py`、
  `scripts/authorized_upload.py`
