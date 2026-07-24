# Remote-first autoslice route

> 当前运行拓扑说明，2026-07-24。本文件只说明 authority 与交付方向；流水线产品规则以
> [pipeline/README.md](pipeline/README.md) 为唯一入口。

## Authority

```text
free:/opt/bilive/autoslice/repo
  committed autoslice deployment; read DEPLOYED_COMMIT and managed hashes

free:/opt/bilive/autoslice/{state,out,reports}
  live state, artifacts, audits, journals and runtime evidence

free:/opt/bilive/bilive-recorder
container bililive_recorder:/rec
  sole BililiveRecorder and recording input

Bilibili public + Creator + exact section
  public delivery truth
```

`free:/opt/bilive/app` / `bilive_record:/app` 是 tooling/legacy app，不是 recorder 或
post-stream autoslice authority。本地 macOS repo 用于 source staging、tests、docs 与审片镜像，
不能单独证明部署、运行、产物或公开状态。

## 交付方向

```text
recording closure
→ selection anchor and lifecycle
→ source-context boundary + story-scoped owners
→ full-context text repair + final-interval verification
→ title / cover / StoryContract
→ final burn + portable package + canonical audit
→ one delivery branch:
  ├─ no-upload review
  ├─ explicitly authorized new-BV upload
  └─ exact same-BV repair:
     evidence-v2 final-byte review + create-only receipt
     + separate repair authorization
     → durable repair journal → fresh verify-live completed sidecar
```

各箭头的准入、证据 schema、失败状态和完成判据只从对应 step 读取：

- source/selection/boundary/text：
  [10](pipeline/10-source-recording.md)、
  [20](pipeline/20-selection.md)、
  [30](pipeline/30-boundary.md)、
  [40](pipeline/40-subtitle-text.md)；
- title/cover/package：
  [60](pipeline/60-title.md)、
  [70](pipeline/70-cover.md)、
  [80](pipeline/80-package-delivery.md)；
- authorization、same-BV 与公开闭环：
  [90](pipeline/90-publish.md)。

## 运行纪律

- 部署从 clean committed tree 进入 `scripts/deploy_free_autoslice.sh`；部署完成仍须现场读回
  commit、managed hashes、runtime state、process/lock、source/artifact 与公开面。
- `DISABLED`、mount、credential、quota、runner、artifact 和 public state 都是 live truth，
  本文不固化当前值。
- 新投稿、same-BV repair、最终感知 receipt 和公开完成互不借权；具体操作只用
  [bilive publish skill](../.agent/skills/bilive-autoslice-publish/SKILL.md) 并服从 80/90 step。

## 本地职责

长期保留源码、测试、step docs、profile/assets、skills 与小型审计证据。`reports/**`、
`lidousha/YYYY-MM-DD/**`、媒体、缓存与临时 replay 默认是运行产物；任务明确点名的审片包/
事故证据除外。清理前先做只读分类并保护用户已有改动。
