# 李豆沙 autoslice 架构

> 当前结构图，2026-07-23。具体规则只读
> [pipeline/README.md](pipeline/README.md) 的对应 step；历史阶段设计与旧 commit 保留在 Git，
> 不在本文件继续滚动复制。

```text
Recorder authority
  BililiveRecorder FileClosed + source integrity
          │
          ▼
Selection
  semantic recall → Tier admission → calibrated effective score
  → exact-contract lifecycle / ordinary backfill
          │
          ▼
Boundary
  anchor + source context → semantic closure review
  → manual lower bound AND cue/syntax/four-proposition gates
          │
          ▼
Text
  ASR/LRC → clip-context → entity/chat/acoustic adjudication
  → reviewed baseline → higher source truth
  → final-owner survival → strict SRT
          │
          ▼
Story surfaces
  one StoryContract → final subtitle + archive title + cover task
  → shared title choke point
  → screenshot/AI route + final-pixel/participant/glyph proof
          │
          ▼
Final bytes
  talk intro roster or song no-intro → burn → record/artifact hashes
          │
          ▼
Package attestation
  audit v2 + policy epoch/fingerprint + complete portable inputs
          │
          ▼
Delivery
  no-upload review
  or Ivan-authorized manifest v3 → upload / same-BV edit
  → public + Creator + exact section closure
```

## 状态边界

- Candidate、bundle lifecycle 与 publish compliance 是不同轴，不能用一个 `status` 互相代替。
- exact recovery 只有 `exact-talk-contract-closure.v1.status=COMPLETE` 才能成为
  `review_ready`；不完整就是 `recovery_incomplete`。
- media ready 但 cover proof 缺失是 `media_ready_cover_pending`，不是 compliant product。
- Producer pass 不是 package pass；package pass 不是 upload authorization；上传 API code 0
  不是 public completion。

## 证据边界

- 高权威 source truth / reviewed baseline 必须在最终 surfaces 存活，不能只证明“曾应用”。
- 人工 boundary 是下界，不是绕过 semantic closure 的绝对终点。
- 截图与 AI 都必须证明最终像素；source reference 人物声明不能无条件复制到修改后图片。
- package auditor 绑定当前代码/资产政策与全部 portable inputs；uploader 重新运行 auditor。
- Runtime、credential、quota、部署 commit 与 public state 一律 live inspect，不从架构文档推断。

## 代码入口

- runner：`scripts/free_session_autoslice.py`
- selection lifecycle：`src/autoslice/candidate_selection.py`
- boundary：`src/autoslice/producer_boundary_resolution.py`
- text/final owner：`src/autoslice/producer_text_pipeline.py`、
  `src/autoslice/producer_text_finalization.py`
- strict SRT：`src/autoslice/subtitle_validation.py`
- title：`src/autoslice/title_policy.py`
- cover proof：`src/autoslice/cover_route_evidence.py`
- package audit：`scripts/audit_lidousha_review_package.py`
- publish：`scripts/authorized_upload.py`
