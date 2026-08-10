# free autoslice state 悬空媒体引用审计 + 《暖暖》歌切（song_232939_870）tombstone 修复

- 日期：2026-08-09
- 宿主：free（`/opt/bilive/autoslice`）
- 静默窗：`DISABLED` 标志全程在位，`ps` 零 `free_session_autoslice` / `ffmpeg`；写入段持 `flock /opt/bilive/autoslice/runner.lock`
- 机读件（`reports/**` 被 gitignore，故回执与清单落在全量跟踪的 `cleanup_manifests/`，与致因 manifest 同处）：
  - 修复回执 `cleanup_manifests/free_state_dangling_media_ref_tombstone_20260809.json`（free 侧镜像 `state/manual-remediation/2026-07-18-dangling-media-tombstone-20260809T235834Z.json`）
  - 全量清单 `cleanup_manifests/free_state_dangling_media_refs_scan_20260809.json`（302 条逐条分类）
  - 复跑脚本 `scripts/scan_state_dangling_media_refs.py`（只读，不取锁）
- 发现出处：2026-08-10 清盘 worker 报告（`cleanup_manifests/free_autoslice_capacity_cleanup_20260810.json`）

## 结论

被点名的那一条属实，已按 fail-closed 惯例在 state 层 tombstone 并留回执。但全量扫描把发现面从 1 条扩到 **302 条**，横跨 13 个活 state 文件，且分成三个成因完全不同的类。按「发现面扩大就只出报告不扩权」，本次只修了被点名的那一条，其余全部只报不动。

真正需要 Ivan 裁定的不是被点名那条（它已经无害且 fail-closed），而是下面两项：**C 类** 生产 state 引用了 free 上根本不存在的 `vtuber-reproduce` 树，涉及 28 条、全部挂在 `published` 行上；以及 **A_repo_unresolved** 里 14 个 `review_ready` 候选的成品 mp4 在盘上找不到对应文件。

## ① 被点名引用：消费方与实际风险

引用位置 `state/2026-07-18.json` → `songs[2].song_completion_evidence.burned_preview_path`，候选 `song_232939_870`（歌切《暖暖》，`review_ready`，`cover_status=BLOCKED_COVER_AUTHORITY_PREFLIGHT`，`delivery_upload_enabled=false`）。指向的 `out/.../seededsong_120000_424730.recut.burned-final-sapphire72.mp4` 已被 2026-07-30 ENOSPC 清理删除。

该清理把 `out/2026-07-18/*/song_selector_full/**/*.mp4` 整片按「regenerable from retained source recordings」删掉，**没有做 state 引用扫描**——这正是 7/24 误删事故的同一机制。8/10 那次清理的 manifest 自己也写了它「strengthened」了这条先例，等于承认 7/30 那次没扫。

解引用点只有两个，都在 `scripts/build_lidousha_song_review_manifest.py`：

1. `_completion_artifact_bindings`（约 424 行）对该值做 `Path(source_value).resolve(strict=True)`，文件不在就抛 `SongReviewManifestError`——**硬 fail-closed，不会静默变绿**。
2. 第 358 行的 `fresh != frozen` 漂移检查。这条**在删除发生的那一刻就已经永久不可能通过**了：重算 `song_completion_evidence` 时 `_canonical_existing_path` 拿不到文件会返回 `None`，落 `SONG_RECUT_STREAM_CONTRACT_INVALID`，`ready` 变 `False`，于是在约 350 行的 delivery-ready 门就先抛错，根本走不到冻结比对。

`scripts/authorized_upload.py:388` 看起来相关但其实不受影响：它读的是评审包里的 `delivery_authority.song_completion_evidence`，而且只拿 `burned_preview_sha256` 去比对包内自带的字节，从不解引用 state 里的路径。

所以实际风险面比预期小：**没有 status stale / witness 误报的证据**，每条通路都是 fail-closed。真实影响只有一个——这个候选将来一旦要出歌切评审包就会硬失败，而在打 tombstone 之前，那个失败看上去是「文件莫名其妙不见了」，没人能从报错里知道是谁删的。

另外该候选**从未发布**（`publication_registry.runtime.v1.json` 零条目），2026-07-18 也是已关闭的过去日（`pending_talk=[]`、`pending_song=[]`），runner 只 tick 当天，不会再碰它。

## ② 修复：state 层 tombstone

把该字段原地换成一个非路径哨兵字符串，内嵌删除时间、责任 manifest、原路径、存活等价物与 sha256：

```
TOMBSTONE:cleanup_deleted;deleted_on=2026-07-30T09:15Z;manifest=cleanup_manifests/free_autoslice_enospc_recovery_cleanup_20260730.json;original_path=…;surviving_identical_bytes=…;sha256=94fd762b…;receipt=cleanup_manifests/free_state_dangling_media_ref_tombstone_20260809.json
```

选**字符串**而不是 dict：对假定 `str` 的消费方类型稳定；不以 `/` 开头，所以路径扫描器和 witness 工具不再把它当活引用；`resolve(strict=True)` 抛出的 `FileNotFoundError` 自带出处。`burned_preview_sha256` 原样不动——它是通往存活字节的证明链。`updated_at` 也不动，这次带外手术不是一次 tick，不该伪造。

写入过程：锁内复检静默窗 → 断言当前值确为那条死路径、死路径确实不存在、存活等价物在位、sha256 匹配、计算出的 diff 恰好一个叶子 → 备份 `2026-07-18.pre-dangling-media-tombstone-20260809T235834Z.json` → tmp + fsync + `os.replace` + 目录 fsync。序列化对齐 `runner_state_writeback._atomic_write` 的 `json.dumps(ensure_ascii=False, indent=2)`，且磁盘原本就是该规范格式，所以改动在字节层也是纯单叶。

事后核验：文件重新可解析；对备份做深度叶子 diff 恰好一处；复扫 07-18 从 12 条降到 11 条；tombstone 不再被计为路径引用；`runner.lock` 与 `DISABLED` 未被改动。

实际写了两次：第一次落 tombstone 时 `receipt=` 指向 `reports/`，随后发现 `reports/**` 被 gitignore，于是在同样的锁/备份/原子写纪律下第二次把指针改到 `cleanup_manifests/`。两次各改一个叶子，各留一份备份（`…pre-dangling-media-tombstone-20260809T235834Z.json`、`…pre-tombstone-receipt-retarget-20260810T000536Z.json`）。

`state_sha256` 由 `e16c93bc…` 经 `2eb57acb…` 到 `1c7a7dd8…`。

### 没走的那条恢复路（留给 Ivan）

被删的中间件与存活成品 `repo/lidousha/2026-07-18/歌切_【李豆沙】豆沙歌，《暖暖》｜自__song_232939_870.mp4` **字节完全相同**（sha256 均为 `94fd762b…`，已实测）。也就是说一条 `cp` 就能精确复原这个引用，不需要重编码、不需要重产媒体。

没有这么做，是因为那等于悄悄把一个 `review_ready` 候选解封——在没有裁定的情况下改动 live 政策，违反 7/27 出版 hold-gate 纪律，也违背该次清理的本意。这条路留给 Ivan 主动发起。

## ③ 全量扫描：302 条，三类

| 类 | 条数 | 含义 |
|---|---:|---|
| `B_cleanup_deleted` | 194 | `out/` 下的流水线中间件，被 7/30 或 8/10 清理删除 |
| `A_repo_unresolved` | 49 | `repo/lidousha` 路径，日目录里找不到承载该 candidate_id 的存活文件 |
| `A_rename_drift` | 31 | 成品仍在盘上，只是改名了（hook/标题手术），state 留着手术前的文件名 |
| `C_foreign_workspace` | 28 | 指向 `/home/ivan/Project/vtuber-reproduce`，该树在 free 上**根本不存在** |

按 state 文件分布：07-24（53）、07-26（68）、07-25（56）、07-10（49）领跑；当前活跃的 08-07（12）、08-08（16）也在列。

分类方法：`repo/lidousha` 各日目录的 `*.record.json` 里 `media_path` 仍带 candidate_id，用它建 candidate_id → 存活文件名 索引，就能把「改名」和「删除」区分开。例：07-10 的 `auto_200009_524_545` state 记的是 `临时被叫去彩排，她连声问观众"结束后.mp4`，盘上实为 `临去彩排前反复撒娇确认"你们还要来找.mp4`——同一候选，hook 被改写过。

### B 类：中间件删除

194 条里 175 张是封面 PNG，其余 19 个 mp4 是 `source-context.context.mp4`、`padded_*`、`piece_*` 一类中间件。这批与 8/10 manifest 的 rule-4「regenerable carve-out」自述一致，属该次清理的已知代价。124 条挂在 `published` 行上——成品早已发出，中间件没了不影响已发布内容，但任何回溯性重建（重烧、重出评审包）都会 fail-closed。

### A_rename_drift：31 条，无数据丢失

标题/hook 手术改了文件名，state 没跟着更新。成品都在。属账面漂移，不是丢件。

### A_repo_unresolved：49 条，其中 14 个候选值得看

按行状态拆：38 条 `review_ready`、6 条 `delivery_quarantined`、5 条 `candidate_rejected`。后两类是正常的——被拒/被隔离的候选，成品本来就该清掉。

真正需要眼睛的是这 14 个 `review_ready` 候选（去重后）：

| state | candidate_id |
|---|---|
| 2026-07-09 | `auto_210025_1065_1218`、`auto_213023_323_431` |
| 2026-07-10 | `auto_190017_1068_1217`、`auto_193009_1539_1637`、`song_200009_217`、`song_212005_1444` |
| 2026-07-11 | `auto_170019_302_355`、`auto_173012_345_521`、`auto_180011_1636_1782` |
| 2026-07-16 | `auto_155648_152_178`（在 `operator_quarantined_picks` 里）、`auto_162645_712_787`、`auto_162645_938_967` |
| 2026-07-18 | `auto_225942_1025_1073`、`auto_225942_434_520` |

这些行仍标 `review_ready`，但 `summary.delivery` 指向的成品 mp4 在日目录里找不到同 candidate_id 的存活文件。可能是更早的改名规则（record.json 里没留 candidate_id 痕迹）导致索引失配，也可能是真丢件。逐条判真伪需要打开对应 record.json 和音频比对，超出本次授权，只登记。

`song_212005_1444` 与既有记忆里「song_212005 materialized_recut 交付集成缺口 fail-closed」对得上，可能是同一根因的另一面。

### C 类：生产 state 引用不存在的 reproduce 树 —— 建议优先裁定

28 条全部来自 `state/2026-08-07.json`（12）和 `state/2026-08-08.json`（16），全部挂在 **`published`** 行上，路径形如：

```
/home/ivan/Project/vtuber-reproduce/out/2026-08-08/auto_200130_1323_1603/replacement_recuts/covers/….cover.png
```

`/home/ivan/Project/vtuber-reproduce` 在 free 上**整棵树都不存在**，从来没被这台机器写过。字段集中在封面身份核验证据：`final_host_identity_verification.{comparison_path,final_cover_path,reference_path,witness.image_path}`、`source_composition_verification.*`，08-08 pick[1] 还多一组 `rejected_final_host_identity_verification.*`。

这不是清理删除，也不是改名——是**异机工作区路径漏进了生产 state**。涉及的候选（`auto_203735_555_680`、`auto_220747_488_680`、`auto_200130_1323_1603`、`auto_200130_1722_1792`）都已 published，意味着已发布内容的封面身份见证在本机无法复核。与既有记忆「生产基线在 codex 分支上，main 勿直接部署」指向同一片区域。

急迫性在于 **2026-08-08 是活跃日**：`status=publication_in_progress`、`pending_talk=8`、`pending_song=1`。runner 一旦解封就会继续 tick 这个文件。

## 未做 / 剩余风险

- 只修了被点名的 1 条，其余 301 条只登记不动。
- 成品侧的冻结 sidecar（`*.delivery.manifest.json`、`*.record.json`）内部几乎肯定也带着同一条已死的 `out/` 路径。它们是内容寻址的冻结件，不在本次范围内——所以**不要指望这次 state 手术能让歌切评审包重新构建成功，它仍会 fail-closed，这是设计如此**。
- `.pre-*` 备份快照未扫也未动（默认排除；`--include-backups` 可开）。
- A_repo_unresolved 那 14 个 `review_ready` 候选的真伪未逐条仲裁。
- 未对 C 类做任何清理或重绑。

## 建议下一步（均需 Ivan 裁定）

1. **C 类优先**：决定已发布条目的封面身份见证是补录到 free、重绑到本机路径、还是标记为异机产出。08-08 仍在活跃窗内。
2. 给容量清理流程加一道**删除前 state 引用扫描**，把 `scripts/scan_state_dangling_media_refs.py` 挂进去——7/30 那次的缺失正是这一步。
3. A_rename_drift 那 31 条可批量重绑到存活文件名，纯账面修正、无媒体动作。
4. 《暖暖》歌切若要继续推进，走上面那条 `cp` 复原路（字节等同，无需重产）。
