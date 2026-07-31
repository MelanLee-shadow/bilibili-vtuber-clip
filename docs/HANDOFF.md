# Current handoff

Updated: 2026-07-30T20:15:00-04:00 by Codex root.

本文件只记录会影响下一次操作的 live 状态。流水线规则只读
[`docs/pipeline/`](pipeline/README.md)。`review_ready`、本地 commit、旧 PID 或旧 handoff
都不是发布完成证明；已发布稿必须读取 Creator/public/section fresh-live 回执或 cover-only
receipt。

## 目标

保持 `free:/opt/bilive/autoslice` 正式 cron 不停，继续收敛 2026-07-25、07-26、07-29
及后续日期。用户已授权：修复稿可直接同 BV 编辑上传；当天新稿可权宜上传；指定旧稿重做后
可直接上传，无需再次等待审阅。

## Runtime authority

- 远端部署：`free:/opt/bilive/autoslice/repo/DEPLOYED_COMMIT` =
  `bff2707a536c2808f3450c847aa363d0621d89a7`（2026-07-30T23:17:56Z）。
  `DISABLED` 不存在；正式 runner cron 为每 10 分钟一次。
- 本地 `main` 在 `bff2707` 之后只有封面/发布证据提交 `f2ea7eb`、`f38bbad`
  及本 handoff 更新，没有未部署的生产代码差异。
- 2026-07-31T00:10Z 正式 runner 正在处理 07-26 歌切；不能把某个候选 fail-closed
  解释成整条流水线停止。

## 已完成

- 07-22 与用户点名的 07-24 修复均已上线：`BV1xgg462Env`、`BV1AD366DEd9`、
  `BV1Eo3L6zECt`、`BV1ec3A6bEWF`。07-24 的“礼墨”、0:13 怪叫、0:59
  “他一副”、1:23 “kmx 欺负人”均已按对应修复范围处理；专名与语境通病已进入
  glossary/CPA 最终裁决链。
- `auto_142942_496_618` 已用最新版流水线整片重跑并在原
  `BV1zk386LEjC` 完成同 BV 修复：CID `40453148097 -> 40455570336`，未新建 BV。
  最终字幕将已确认的日语人称统一为假名（`ぼく/おれ/あたし/おら/わたくし`），不再混用
  `boku/ore/atashi`；39/39 reviewed baseline mappings、native-script gate、
  source-truth owner gate、redelivery baseline owner gate 和 exact-final v2 均 PASS。
  最终视频/SRT/封面 SHA-256 分别为
  `70818f97e595a4a29d50d0ab7aa40877e04e3286970c36d448e2150d964dbd37`、
  `34dbb888e1bff78d2832c57360418ec9c890575aaf0d78c93ac0d74882a8d4cc`、
  `2f34fbc1bc28e2503e10f48b488c04ad9afac670b2a4a1ec29277e69d862d9ff`。
  CPA 主图身份复核确认李豆沙为主体，标题字面为“一声‘偶’让小李 / 日语人称翻译大会”。
  Creator/public/section 已回读新 CID；完成侧车为 `VERIFIED_FRESH_LIVE`。
  固化证据在
  `reports/authorized_uploads/2026-07-30-japanese-pronoun-r7/`（commit `f38bbad`）。
- 上述重跑暴露并修复了 baseline owner verifier 的真实漏洞：最终稿经过已审阅的
  日语 native-script canon 后，verifier 仍拿旧罗马音做字面比较。`bff2707` 让 expected
  baseline 经过同一 canon，并在 audit 中记录转换来源；无关文字漂移仍 fail-closed。
  `producer_text_finalization`、package finalization 与 integrity 共 88 个相关测试 PASS，
  修复已部署。
- `auto_192000_909_1014` 的“白色奶龙”封面已按用户提供的
  `/Users/ivan/Downloads/60bae315bc53a44bede0582389460d20475948079.png` 进行 cover-only
  修复：删除无关龙/3D 龙模型，在原位置使用白发、熊猫耳、头顶墨镜的李豆沙“白色奶龙”
  形象。原 `BV1s7326qEc9` 和 CID `40438664771` 均未改变；新公开 CDN 资产为
  `9994c2c75f31d0b1706be4e7709efe226ebcbca0.png`，本地目标与公开回下载 SHA-256 均为
  `8bd5fa4f47468f514dc32bc265896405a46289a31ea930ac748234a18c989c06`。
  CPA 主人公身份与白色奶龙专项视觉 QA 均 PASS，receipt 为 `VERIFIED_EDITED`；
  固化证据在
  `reports/cover_only_repairs/2026-07-30-auto_192000_909_1014-white-milk-dragon/`
  （commit `f2ea7eb`）。
- 其他已完成的同 BV/封面修复仍保持公开：粉色小姐姐 `BV1RPNR6dET9`、同事家开播
  `BV1Eo3L6zECt`、沙豆李投票 `BV1zzgd6JEHe`。下一次操作不应重复上传或新建替代 BV。
- Gemini 路由审计确认代码顺序正确：AGY 仍是音频 witness 首选，三把 free Gemini key
  最近生成 canary 仍为 429；paid backup 的最新最小生成 canary 已恢复 HTTP 200，新的音频
  witness 任务会在 typed AGY/free-key failure 后使用 paid fallback。CPA 是最终文字/语义
  裁判，并是图像 witness 首选；CPA 不接音频不等于 CPA 不能看图。

## 当前正式队列

- 07-25：`review_ready`，pending talk/song 均为 0；6 个 talk 已就绪，1 个 song 进入状态。
  `song_192000_1321` 当前 deterministic packaging 拒绝原因为 active publish draft
  缺失且不属于 verified deferred-cover case，需要流水线补齐合法 draft authority，不能
  绕过门直接发布。
- 07-26：2026-07-31T00:10Z 为 `processing`，pending song = 1；7 个 talk 已就绪。
  多个 song tight-window 能识别歌曲，但扩大到 full-source 后仍缺 positive LRC boundary
  proof；runner 正在按 typed recoverable 规则重新入队，不等待用户。
- 07-29：`review_ready_with_failures`，pending talk/song 均为 0；5 个 talk pick。
  `auto_225056_814_887` 的 `subtitle_authority / chat_authority_finalization` 仍需流水线
  自行重建 authority。

## 阻塞

没有需要 Ivan 补充的外部 blocker。

- 歌切 active draft 缺失、positive LRC boundary proof 缺失、provider transient、
  subtitle/chat authority 失败都属于流水线/操作层 blocker；应保持 typed retry 或修复
  authority，不得停下来等待用户，也不得为了“变绿”绕过发布门。
- `auto_152944_1411_1463` 的 cover repair 当前在 image request 前 fail-closed，以保留
  screenshot route；下一次应修复 route authority，而不是用未验证 AI 像素覆盖截图路线。
- `auto_142942_496_618` 的 r6 因 recovery 目录漏建 `logs/` 在 ASR 前失败，且继承旧 lifetime
  retry cap；r6 仅保留为操作失败证据。不要原地洗绿或重用其 fingerprint；成功 authority 是
  fresh r7。

## 下一步

1. 让当前 07-26 tick 与后续 cron 继续；每次只按 fresh state、runner log 和最终 artifact
   回执判断进度。
2. 修复 `song_192000_1321` 的 active publish draft authority；继续要求 07-26 歌切取得
   positive LRC boundary proof，不能把“识别出歌曲”当成边界真值。
3. 修复 `auto_152944_1411_1463` 的 screenshot-preserving cover route，以及
   `auto_225056_814_887` 的 subtitle/chat authority。
4. 后续封面继续执行最终实图检查：多人联动必须确认李豆沙主体；特殊梗必须绑定正确参考形象；
   CPA vision 为首选，公开 CDN 回下载需与目标图 hash 一致。

## 固化规则

- 用户只指出 1–2 个问题且未说明问题穷尽：默认整片重跑；指出 3 个及以上问题：修指定位置及
  背后通病，不因这条规则再次整片重跑。
- 大部分 glossary 专名允许按期望收益机械替换；专名之间平等，两个已注册专名冲突时由 CPA
  结合文字上下文裁决。
- 已发布稿只走 `scripts/authorized_upload.py repair-*`；封面单独修复只走
  `scripts/bili_cover_edit.py`。禁止新建替代 BV、裸 API、legacy replace 或删除旧证据制造绿灯。
