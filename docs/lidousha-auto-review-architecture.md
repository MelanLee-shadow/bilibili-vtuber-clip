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
  → screenshot/AI route
  → source-frame facts ≠ cover-text/layout narrative
  → final-pixel/participant/glyph proof
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
  ├─ no-upload review
  ├─ Ivan-authorized manifest v3 → new upload
  └─ exact same-BV:
     committed exact-point contract/hash
     → final-byte perceptual review → truthfully attributed receipt
     + separate Ivan repair authorization → same-BV edit
  → public + Creator + exact section closure
```

## 状态边界

- Candidate、bundle lifecycle 与 publish compliance 是不同轴，不能用一个 `status` 互相代替。
- exact recovery 只有 `exact-talk-contract-closure.v1.status=COMPLETE` 才能成为
  `review_ready`；不完整就是 `recovery_incomplete`。
- media ready 但 cover proof 缺失是 `media_ready_cover_pending`，不是 compliant product。
- Producer pass 不是 package pass；package pass 不是 upload authorization；上传 API code 0
  不是 public completion。
- recovery manifest 的 pending-human 状态、package audit 与 final perceptual receipt 是
  不同证据面；receipt 只准入 exact same-BV repair，不是 `AUTO_UPLOAD` 或新 BV 授权，也不
  替代 Ivan 对该修复动作的独立授权。细则只读
  [80-package-delivery.md](pipeline/80-package-delivery.md) 与
  [90-publish.md](pipeline/90-publish.md)。

## 证据边界

- `required=true` 的高权威 source truth 必须以有效 post-apply 精确 cue projection 在最终
  surfaces 存活；`local_windows` 只用于发现，`required:false` 只作 best-effort，二者都不能
  冒充 final owner。reviewed baseline 同样必须在最终 surfaces 存活，不能只证明“曾应用”。
- 人工 boundary 是下界，不是绕过 semantic closure 的绝对终点。
- 截图与 AI 都必须证明最终像素。hash-bound 源帧可见事实与由封面文字/版式表达的故事叙事
  必须分开；源帧没直接显示的动作、物件或反转不能倒推成像素事实，source reference 声明
  也不能无条件复制到修改后图片。完整规则只读 [70-cover.md](pipeline/70-cover.md)。
- package auditor 绑定当前代码/资产政策与全部 portable inputs；uploader 重新运行 auditor。
  它不承担最终人眼/听感复核，也不能自行产生发布权限。
- final perceptual receipt 必须绑定 committed exact-point contract/hash、package
  manifest/audit、title/record、same-BV publication target 与最终媒体证据，并如实记录实际
  reviewer。只有 root 真正完成完整逐点观看才可标成 `delegated_root_agent`；不能冒称 Ivan
  已亲自观看。字段与漂移判据只读 [80-package-delivery.md](pipeline/80-package-delivery.md)
  和 [90-publish.md](pipeline/90-publish.md)。
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
