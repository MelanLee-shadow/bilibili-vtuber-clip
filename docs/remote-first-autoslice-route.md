# Remote-first autoslice route

> 当前运行拓扑说明，2026-07-23。流水线产品规则以
> [pipeline/README.md](pipeline/README.md) 为唯一入口；本文件只说明 authority 和交付方向。

## Authority

```text
free:/opt/bilive/autoslice/repo
  commit-only autoslice 部署树；DEPLOYED_COMMIT 是版本读回

free:/opt/bilive/autoslice/{state,out,reports}
  live state、产物、审计、账本与运行证据

free:/opt/bilive/bilive-recorder
container bililive_recorder:/rec
  唯一 BililiveRecorder 与录播输入
```

`free:/opt/bilive/app` / `bilive_record:/app` 是 tooling/legacy app，不是 recorder 或
post-stream autoslice authority。本地 macOS repo 用于 source staging、tests、docs 与审片镜像，
不能单独证明部署、运行、产物或公开状态。

## 路径

```text
BililiveRecorder 文件闭合与源完整性
→ 高召回 anchor / Tier 校准 / exact state
→ source-context + semantic boundary
→ 字幕 authority、long-context repair、strict SRT
→ title / cover final-pixel / StoryContract
→ final burn + package audit v2
→ delivery branch:
  ├─ no-upload review
  ├─ Ivan-authorized new upload
  └─ exact same-BV:
     committed exact-point contract/hash
     + final-byte perceptual review + truthfully attributed receipt
     + separate Ivan repair authorization
→ public + Creator + exact section closure
```

生产默认不需要 Ivan 手动逐片裁时间线、逐条挑候选或守着上传；安全门无法证明时由系统
BLOCK/RETRY/隔离。`review_ready`、脚本 rc=0、本地包存在或 JSON 自报通过都不是发布证明。
package audit 也只证明机器可判定的结构、hash 与政策闭包，不等于最终感知复核。exact
same-BV 的 final perceptual receipt 必须绑定 committed exact-point contract/hash、
package manifest/audit、title/record、publication target 与最终媒体证据，并如实记录实际
reviewer；只有 root 真的完成本轮完整逐点观看才可写 `delegated_root_agent`，不得冒称
Ivan 已亲自观看。receipt 只准入同 BV 修复，不是 `AUTO_UPLOAD`、不授权新 BV，也不替代
Ivan 对该修复动作的明确授权；具体门只读
[pipeline/80-package-delivery.md](pipeline/80-package-delivery.md) 与
[pipeline/90-publish.md](pipeline/90-publish.md)。

## 部署与验证

- 只从 clean committed tree 经 `scripts/deploy_free_autoslice.sh` 部署；不向远端 runtime
  单文件热补丁。
- 部署后核对 `DEPLOYED_COMMIT`、受管树 hash、外部 runtime media、kill switch、cron/lock
  与真实 state/out/report。
- `DISABLED`、进程、mount、录播段完整性和公开稿件是 live state，文档不固化当前值。
- 发布始终是独立 Ivan-authorized step；机器 audit、最终感知复核 receipt、authorized manifest 与
  public readback 各自证明不同事实，不能互相代替。新投稿与 exact same-BV 的精确准入分别
  以 [pipeline/90-publish.md](pipeline/90-publish.md) 为准。
- 已发稿修复保留同 BV；完整闭环见 [pipeline/90-publish.md](pipeline/90-publish.md)。

封面路线中的 source-frame 可见事实与 cover-text/layout 叙事必须分开；截图没有直接拍到
的动作、物件或反转不得写成源帧像素证据。具体 claim contract 只读
[pipeline/70-cover.md](pipeline/70-cover.md)。

## 本地职责

长期保留源码、测试、step docs、profile/assets、skills 与小型审计证据。`reports/**`、
`lidousha/YYYY-MM-DD/**`、媒体、缓存与临时 replay 默认可丢弃；任务明确点名的审片包/事故证据
除外。任何清理先做只读分类并保护用户已有改动。
