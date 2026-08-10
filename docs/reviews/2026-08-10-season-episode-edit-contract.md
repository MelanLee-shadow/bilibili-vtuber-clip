# `season/section/episode/edit` 真实契约考据（2026-08-10）

**触发**：`BV1hquD6pE7X` 三合一置换后，合集《小李切片》/《正片》里那一集的显示标题
仍是旧标题。`sync_exact_section_episode_title` 全部 fail-closed 前置校验通过，
`season_episode_edit` 拿到 `{'code': -400, 'message': '请求错误'}`，线上零变化。

**结论一句话**：接口契约现在**已经查清**（权威 = 创作中心前端自身的源码），
旧 payload 的 `sorts` 字段两处都错；`-400` 与这两处错误**高度一致但尚未被实证闭合**，
最终证明只能来自一次浏览器抓包或一次授权的修正调用。

---

## 1. 权威来源：创作中心前端 bundle（读取式，未触碰任何线上数据）

B 站创作中心是公开可取的静态前端。2026-08-10 取回并反编译：

```
https://member.bilibili.com/platform/upload-manager/season        # 页面壳
https://s1.hdslb.com/bfs/static/studio/creativecenter-platform-next/static/js/
    js/creativecenter-v3.index.a015bef9.js                        # 入口 + 错误码表
    async/js/creativecenter-v3.3543.a015bef9.js                   # 合集 API 层
    async/js/creativecenter-v3.9825.a015bef9.js                   # 合集编辑页组件
```

> **证据会失效**：`a015bef9` 是发布哈希，前端一重新部署这些 URL 就 404。
> 因此下面**逐字保留**关键片段（附 chunk 文件名 + 字节偏移 + 取回日期 2026-08-10）。
> chunk 本体不入库。

### 1.1 端点与 csrf 位置 — `3543.js`

`@6093`：
```js
editVideo: e => a.post("/x2/creative/web/season/section/episode/edit", e, {isJSON:!0})
  .then(e => { let {data:t}=e; if(0!=+t.code) return Promise.reject(Error(t.message)) })
```

`@888`（请求拦截器，`isJSON` 分支）：
```js
t.isJSON ? (t.params||(t.params={}), t.params.csrf = e,
            t.headers={...t.headers,"Content-Type":"application/json"},
            delete t.isJSON)
         : (r.csrf = e, t.data = <form-urlencode>(r), ...)
```
→ **csrf 走 query string，body 是纯 JSON。仓里现写法正确，`-400` 不是 csrf 问题**
（csrf 失败是 `-111`）。`index.js@168942` 的错误码表里 `"-400":"参数错误"`。

### 1.2 读取端：`data.episodes` 是唯一正确路径 — `3543.js@4442`

```js
getSection: (e,t) => a.get("/x2/creative/web/season/section", {params:{id:e, sort:t}})
  .then(e => { let {data:t}=e;
    if (0!=+t.code) return Promise.reject(Error(t.message));
    let r = t.data || {};
    r.section  || (r.section  = {});
    r.episodes || (r.episodes = []);
    return r; })
```
`section` 与 `episodes` 是 **`data` 下的平级兄弟**。前端从不去 `data.section.episodes` 取。

### 1.3 列表行 → 组件模型 — `9825.js@102442` 与 `@119090`（两处同码）

```js
let s = t.episodes.map(t => { let i = {
  id: t.id, videoTitle: t.title, aid: t.aid, bvid: t.bvid, cid: t.cid,
  sectionId: t.sectionId, archiveTitle: t.archiveTitle, archiveCTitle: t.videoTitle, ... };
```
→ 组件里的 `id` = `episodes[].id`（合集内单集 ID）；UI 上可编辑的「单集标题」= `episodes[].title`。

### 1.4 `sorts` 怎么造 — 行内改名路径 `9825.js@87921`

```js
editVideoTitle(t,e){
  ...
  y.A.filterWord(i).then(() => {
    t.sorts = this.videoList.map((t,e) => ({id: t.id, sort: e+1}));
    t.sortIndex = e+1;
    this.$emit("onVideoEditConfirm", {...t, videoTitle:i, onEdit:!1});
  })
```

### 1.5 `sorts` 怎么造 — 弹窗路径 `9825.js@83291`

```js
onEditAvModalComplete(t){ if(this.validateVideo(t)){
  let e = window._.cloneDeep(this.videoList),
      i = e.findIndex(e => e.id === t.id),
      s = Number(t.sortIndex) - 1;
  i<=s ? (e.splice(s+1,0,t), e.splice(i,1)) : (e.splice(s,0,t), e.splice(i+1,1));
  t.sorts = e.map((t,e) => ({id: t.id, sort: e+1}));
  this.$emit("confirm", t); ... }}
```
两条路径结论一致：**`sorts` 是整节的完整顺序表**，元素 `{id: <单集 id>, sort: <1 起位次>}`。

### 1.6 最终 payload — `9825.js@97758` / `@121271`（两处同码）

```js
onVideoEditConfirm(t){ let e=this.videoList.findIndex(e=>e.id===t.id);
  if(-1!==e){ let i={...this.videoList[e], ...t}; this.$set(this.videoList,e,i);
    let s = { id: i.id, title: i.videoTitle, aid: i.aid, cid: i.cid,
              seasonId: this.epId, sectionId: this.sectionId };
    i.sortIndex && (s.sorts = i.sorts, s.order = Number(i.sortIndex));
    this.loading=!0; y.A.editVideo(s)...
```

---

## 2. 正确的 payload

```
POST https://member.bilibili.com/x2/creative/web/season/section/episode/edit?csrf=<bili_jct>
Content-Type: application/json
Cookie: SESSDATA=…; bili_jct=…
```
```json
{
  "id":        215001342,
  "title":     "<新的合集内单集显示标题>",
  "aid":       117067168679822,
  "cid":       40765099962,
  "seasonId":  8383206,
  "sectionId": 9320779,
  "sorts": [ {"id": <该 section 每一条 episode 的 id>, "sort": <1 起位次>}, "… 共 103 条 …" ],
  "order":     100
}
```

| 字段 | 依据 |
|---|---|
| `id` | `9825.js@97758` `id:i.id`；`i.id` 来自 `@102442` 的 `id:t.id` ← `episodes[].id` |
| `title` | 同上 `title:i.videoTitle`；`videoTitle` ← `episodes[].title`（合集显示标题） |
| `aid` / `cid` | 同上，直接透传 `episodes[].aid` / `episodes[].cid` |
| `seasonId` / `sectionId` | 同上，来自路由/组件 props，非 snake_case |
| `sorts` | `9825.js@87921` + `@83291`，两条 UI 路径都是**全表** `{id: 单集 id, sort: 位次}` |
| `order` | `9825.js@97758` `Number(i.sortIndex)`；`sortIndex` = 列表下标+1 |
| csrf 在 query、body 纯 JSON | `3543.js@888` |

**`order` 与线上 `episodes[].order` 的关系（实测）**：2026-08-10 只读 GET
`season/section?id=9320779`（103 条）与 `id=9364628`（13 条），两节的 `order`
**恒等于 1 起下标**（`[1,2,3,…,n]`）。所以传 API 返回的 `order` 与传位次等价——
但代码里现在按位次推导并交叉校验，不等即 fail-closed。

**`sorts` 省略分支**：`i.sortIndex && (...)` 说明前端认为不带 `sorts`/`order` 也合法
（即 `{id,title,aid,cid,seasonId,sectionId}` 六字段）。但两条 UI 路径都必带，
**服务端是否接受未经验证**，不要当成已知可用的简化写法。

---

## 3. 旧 payload 错在哪 / `-400` 归因

旧实现（`bilibili_member_api.py` 改前 ~353-371）：
```json
"sorts": [{"id": 40765099962, "sort": 1}],   "order": 100
```

**已证成立的两处偏离**（对照上面的前端源码，是事实不是推测）：

1. **`sorts[].id` 传的是分 P 的 cid（40765099962），应为合集内单集 id（215001342）。**
   这个 cid 在 section 9320779 里根本不是任何一条 episode 的 id。
2. **`sorts` 只有 1 条，而该 section 有 103 条。** 前端永远发全表。
   附带后果：`order=100` 落在一张只有 1 条的顺序表之外。

**未证成立的一步（明确标注为推断）**：以上两处偏离**导致** `-400`。
支持它的是：`-400` 在前端自己的错误码表里就是「参数错误」；除这两处外，
端点、方法、csrf 位置、Content-Type、其余六个字段全部与浏览器一致；
本次 archive/section 身份全部是当前值。
**否证/坐实它只有两条路**：(a) 抓一次真实浏览器请求做 diff；(b) 用修正后的 payload
发一次授权调用看返回。本次任务禁止写操作，故到此为止。

**交棒文档里两条已过时的说法**（`docs/HANDOFF.md:25`，属 orchestrator 的文件，本报告不代改）：
- 「上次 -400 极可能是用了置换前的旧 cid」——已被 orchestrator 实测证伪（身份全为当前值仍 -400）。
- 「section 9320779 的 episodes 列表里按 aid 已查不到该条」——2026-08-10 只读复核：
  查得到，`id=215001342 / order=100 / aid=117067168679822 / cid=40765099962`，
  `title` 是旧标题、`videoTitle`/`archiveTitle` 是新标题。当初查不到应是解析路径取错。

---

## 4. `data.section.episodes` 解析路径：仓内**不存在**该 bug

三处解析全部是「递归找任意 `episodes` 列表」，因此天然命中 `data.episodes`：

- `src/autoslice/same_bv_section_title_sync.py:episode_rows`
- `src/autoslice/same_bv_repair.py`（复用上面同一个 `_episode_rows`）
- `scripts/authorized_upload.py:_section_episode_rows`（同构实现）

佐证：`3543.js@4442` 的 `getSection` 把 `section` 与 `episodes` 并列归一；
线上实测 `data` 的 keys 就是 `['episodes','section']`，且 `data.section.Episodes` 恒为 `null`。
**无代码改动需要。** 走错路径的是当时的人工探查/交棒记述，不是仓里的代码。

---

## 5. 这些单测是真契约还是自证？——**自证（self-mirroring）**

- `tests/test_bilibili_member_api.py::test_season_episode_edit_preserves_episode_and_page_order`
- `tests/test_same_bv_repair.py::test_production_adapter_rebinds_exact_live_episode_identity`

`git log -S SEASON_EPISODE_EDIT`：两处期望 payload 与实现**同一个 commit**
`6243dfb`（2026-07-28 `fix(publish): converge stale same-BV section titles`）写入，
commit body 只有一行标题，无抓包、无 `code 0` 回执、无外部文档引用。
即：**它们把实现者当时臆想的 payload 又抄了一遍当断言**，
认证的是一个**从未成功过**的请求体。这正是本仓「伪裁定」模式的又一例。

顺带排除一个看似的第三方佐证：GitHub 上唯一实现该端点的 `Yuelioi/bpi-rs`
（`src/creativecenter/season/edit.rs`）也是扁平结构，但它的 doc 链接指向
「编辑合集小节」（另一个端点）、参数表与函数签名对不上、且 `#[serde(flatten)]`
会让 `sorts` 键出现两次——**同样是未验证的照搬，不能当独立证据**。
社区文档 `bilibili-API-collect` 系列（含各 fork）**根本没有收录这个端点**。

---

## 6. 这个函数历史上成功过吗？——分两层，别合并

**A. `BiliSession.season_episode_edit`（Python）：从未成功过。**
`6243dfb`（7/28）出生，全仓无任何 `code 0` 回执，唯一已知的真实调用就是这次 `-400`。
定性：**写完没验证过的死代码**。

**B. 端点本身：2026-07-07 有一次未记录 payload 的疑似成功。**
`f5530ec`（7/7）的 commit body 与 `reports/…/xiexie_song_2130/replacement_recuts/`
下的 `xiexie.uploaded.json` / `.public_verify.json` 声称
「`x/vu/web/edit` 整表回提改标题 + `season/section/episode/edit` 同步合集条目标题」，
并写了 `collection_episode_title_synced: true`。当时仓里**还没有**这个函数
（早三周），是一次性脚本，payload 未留存，free 上也已找不到该脚本。

对这条自证式回执做了一次只读旁证（2026-08-10 GET `season/section?id=9364628`）：
`BV1MgMt6UEGp` 的 episode `id=207062407`、`order=8`（共 13 条）、
`title = "【李豆沙】豆沙歌，《屑屑》你"`（= 改名后的稿件标题），
而 `videoTitle` 仍是上传时的文件名 stem。它排在 8/13 而非末位，
不像「删掉重加」的结果。→ **确实有东西在 7/7 之后改动过那条 episode 的标题**，
与回执方向一致。但这只提高了「该端点可被成功调用」的可信度，
**不能重建当时的 payload**，也不构成对旧 Python payload 的任何背书。

---

## 7. 本次代码改动（已配单测，未对线上发起任何写请求）

`src/autoslice/bilibili_member_api.py::season_episode_edit`
- 参数 `page_cids: Sequence[int]` → **`section_episode_ids: Sequence[int]`**
  （整节 episode id，顺序原样）。
- `sorts` 改由它派生：`[{"id": eid, "sort": i} for i, eid in enumerate(ids, 1)]`。
- 新 fail-closed 不变式：id 唯一且为正、**本条 id 必须在表内**、
  **`order` 必须等于本条在表内的 1 起位次**。
- docstring 记下契约来源与「原样回传 = 不改顺序」的语义。

`src/autoslice/same_bv_section_title_sync.py`
- 新增 `section_order_episode_ids(rows)`：只有当每行 `order` 恰好等于它的 1 起下标时
  才交出 id 表。这既证明读回的是规范顺序，也让整节重排是**可证的 no-op**
  （这是修正后 payload 的真实风险面：`sorts` 会重写整节 103 条的顺序）。
- 保留原来的 `page_cids` 单 P 身份守卫（它是好守卫，只是不该当 `sorts` 的来源）。
- 送出前再校验一次 `episode_id` 的位次与 `order` 一致。

测试
- 重写 `test_season_episode_edit_sends_whole_section_sorts_by_episode_id`（4 条 fixture，逐字断言 payload）。
- 新增 `test_season_episode_edit_never_puts_page_cid_into_sorts` 回归门。
- 负向参数化扩到 7 例（含 order/位次不符两例）。
- 重写 `test_production_adapter_rebinds_exact_live_episode_identity` 为 3 条 episode 的 section。
- 新增 `test_section_title_sync_fails_closed_on_unusable_section_order`（order 乱序 / 缺 id / 重复 id）。

`/Users/ivan/Project/vtuber-slice/.venv/bin/python -m pytest -q`：**3641 passed**。

---

## 8. 还需要的一步：抓一次真实请求（零风险预检）

前端源码等价于抓包，但**发起线上写之前**做一次 diff 仍是最便宜的确认，
而且它本身不产生任何写（只要不点保存就不发；点了保存改的是「本来就要改成的标题」）。

1. 浏览器登录 → **创作中心 → 内容管理 → 合集管理 → 《小李切片》→ 编辑 →
   分组（小节）《正片》→ 编辑、添加单集**。
2. F12 → Network → Filter 填 `episode/edit`，勾 Preserve log。
3. 在单集列表里**点一下那条的标题文字**（进入行内编辑）→ 改成目标标题 → 回车或点别处失焦。
4. 抓 `POST …/season/section/episode/edit?csrf=…`，记 **Request Payload 全文** 与 Response。
   - 之前会先飞一个 `POST /x/vu/web/staff-title/filter`（敏感词预检），**与本契约无关，忽略**。
   - 也可只点开「编辑单集」弹窗改标题（走 §1.5 那条路径），payload 形状相同。
5. 与 §2 的 JSON 逐字段 diff。若一致，剩下的就是拿修正后的
   `sync_exact_section_episode_title` 发一次授权调用。

**执行条件（给真正跑那一次授权调用的人）**：修正后的 payload 是一次
**整节重排写**。§7 的 no-op 不变式只证明了「读回来的顺序是规范的」，
没有覆盖读与写之间的竞态——free runner 每传完一条 talk 就会往 section 9320779
做 `episodes/add`。若正好在这个窗口里新增一条，我们那张 103 条的 `sorts` 表就少了它，
**服务端对表内缺失 id 的处理未知**。所以：确认当时没有上传在飞，
或在 POST 前紧贴着再读一次 section 并比对 `epCount`。

---

## 附：本次用到的只读线上调用（全部 GET，无写）

| 调用 | 时间 | 结果 |
|---|---|---|
| `GET member.bilibili.com/x2/creative/web/season/section?id=9320779` | 2026-08-10 | code 0，103 条，`order` = 1..103，命中 `BV1hquD6pE7X` |
| `GET member.bilibili.com/x2/creative/web/season/section?id=9364628` | 2026-08-10 | code 0，13 条，`order` = 1..13，命中 `BV1MgMt6UEGp` |
| 创作中心静态 bundle（无鉴权 CDN） | 2026-08-10 | 页面壳 + 2 个主 chunk + 148 个 async chunk |
