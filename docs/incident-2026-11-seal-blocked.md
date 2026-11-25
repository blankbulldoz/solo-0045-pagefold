# 排查记录：右支重演通过、改选祖本后封存被挡

## 1. 结论先行

页面上仍展示的「通过」记录没有消失，也没有失效；**变化的是计划对象的修订版本（`plans.revision`），封存条件绑定的是「当前 revision 是否有通过候选」，而那条通过记录固定在旧 revision 上**。

- 冲突条目第一次选右支：裁决写入把 `plans.revision` 从 0 推进到 1（`pagefold/decisions.py:15-22`，实际变更才推进版本，同值幂等）。
- 第 1 版重演通过：在 `attempts` 表写入一行 `(plan_id, revision=1, status=passed, candidate=<字节>)`（`pagefold/attempts.py:42`）。重演历史按 `(plan_id, revision)` 为主键固定，之后任何裁决改动都不会修改或删除它——代码库中没有任何删除/更新 `attempts` 的路径。
- 第二个人对同一冲突改选祖本：这是一次实际变更，`plans.revision` 从 1 推进到 2，`plans.decisions` 变成 `{冲突项: "base"}`。
- 封存时 `seal` 只查**当前 revision** 的重演行（`pagefold/releases.py:25-27`）：
  ```python
  attempt = get_attempt(db, plan_id, revision)
  if attempt is None or attempt["candidate"] is None:
      raise Problem("rehearsal_required", "当前版本须先通过重演。", 409)
  ```
  第 2 版从未重演，`attempts` 里没有 revision=2 的行 → 409 `rehearsal_required`。
- 页面侧的同一逻辑在 `pagefold/plan_views.py:44-46`：只有「当前版且 passed」才启用封存按钮，否则按钮带 `disabled`；而重演历史区无条件列出全部 revision 的记录（`plan_views.py:28-33`），旧通过记录被标注为「历史版」照常显示。

所以这不是数据损坏，也不是通过记录被撤销，而是**重演事实（attempt，按版本固定）与计划当前裁决（plan revision，随裁决推进）解耦后的预期行为**。设计约束「重演事实固定而是否适用于当前裁决会变化」即指此点。

## 2. 为什么只有两次页面抄录无法定性「第二条写入」

状态库只记录**成功提交的事实**，被拒绝的写请求不落任何痕迹：

- 版序不符在 `expect_revision` 直接抛 `stale_revision`（`pagefold/plans.py:58-60`），发生在任何写库之前；
- 封存/归档后在 `expect_open` 抛 `closed_plan`（`pagefold/plans.py:63-65`），同样不写库；
- 同值重复提交在 `decisions.py:15` 的判断下不推进版本、不产生新行（幂等）。

值班同事掌握的两次页面抄录可还原为：

| 抄录 | 页面事实 |
|---|---|
| 第一次 | 冲突项选「右支」，第 1 版重演「通过」，封存可用 |
| 第二次 | 同一冲突项显示「祖本」，页面顶部为第 2 版，重演历史仍有第 1 版「通过 · 历史版」，封存按钮置灰 |

据此**能确定**：存在一次把裁决从 right 改成 base 的成功写入，且 revision 恰好推进了一次（1→2）；第 2 版没有 passed 重演行；计划当时仍是 `open`（否则页面没有裁决表单和重演/封存按钮，`plan_views.py:15,43`）。

**不能确定**：第二条 base 写入到底是一次新操作，还是旧表单/重试；它前后是否还夹杂过被 409 拒绝的请求；以及在两次抄录之间计划是否曾被归档又……（注意：归档是单向状态，代码没有「取消归档」入口，见下）。

## 3. 候选时序矩阵

复现脚本 `examples/incident_replay.py` 用真实 HTTP 入口构造了以下时序，每条时序的完整请求/响应在 `incident-evidence/*.http.log`，状态库事实在 `incident-evidence/snapshot-*.json`。共同前缀均为：建计划（rev0）→ 冲突选右支（rev1）→ rev1 重演通过。

| 时序 | 后续操作 | 最终 plan 行 | attempts 行 | releases | 封存结果 | 与谁最终状态相同 |
|---|---|---|---|---|---|---|
| **A 真实改选** | base 写入成功（→rev2），不再重演 | rev2 / open / base | 仅 rev1 passed | 无 | 409 rehearsal_required | **与 B 逐表一致** |
| **B 重试混淆** | base 成功（→rev2）后，又重发一条带 rev1 的 base 表单，被 409 stale_revision 拒绝 | rev2 / open / base | 仅 rev1 passed | 无 | 409 rehearsal_required | **与 A 逐表一致** |
| **C 幂等重试（对照）** | 带 rev0 的 right 重试被 409；带 rev1 的 right 同值提交不推进版本 | rev1 / open→sealed / right | rev1 passed | 有 | rev1 可正常封存 | 与现场不符（版本没推进、选择是 right） |
| **D 归档之后** | base 成功（→rev2）→ archive（→archived）→ 此后 decisions/rehearse/seal 全部 409 closed_plan | rev2 / **archived** / base | 仅 rev1 passed | 无 | 409 **closed_plan**（错误码与 A/B 不同） | 页面外观近似，但 state 与错误码不同 |
| **E 新版重演过（对照）** | base 成功（→rev2）后对 rev2 重演，样例数据通过 | rev2 / open / base | rev2 passed + rev1 passed | 无 | 此时封存可用 | attempts 多一行，可一眼排除 |
| **F 恢复路径** | 同 E，再封存 rev2 | rev2 / **sealed** / base | 两条 passed | 有（凭证绑定 rev2） | 成功 | 现场无 release 行，可排除 |
| **G 重演受阻（对照）** | stress 镜像计划重演撞唯一约束，留下 blocked 行（同版重演复用该行），candidate 为 NULL | rev0 / open | 一条 blocked（无候选） | 无 | 409 rehearsal_required | 历史里出现「受阻」记录，可排除 |

### 哪些序列在最终状态上看起来一样

1. **A 与 B 在状态库中完全不可区分。** 脚本对两份状态库剔除每次运行变化的 plan id 后逐表断言相等（`summary.json` 中 `A_equals_B_in_state_db: true`）：
   - `plans`：同为 revision=2、state=open、decisions 为 base；
   - `attempts`：同为仅一行 revision=1 passed，候选字节数与 `candidate_digest` 完全相同（`c9d6dd36…a3d24`，24576 字节）；
   - `releases`：同为空。

   原因是 B 中那条 `stale_revision` 请求在写事务之前就被拒绝，状态库没有任何字段能记录「发生过一次失败重试」。**要排除 B，只能查状态库之外的访问日志/反向代理记录，或同事手里那条 409 HTTP 返回原件**；应用本身不持久化请求日志（`server.py` 只往 stderr 打访问行，不落库、不留文件）。

2. **D（归档之后）与 A/B 的页面观感接近**：历史区都只剩 rev1 通过记录、封存都做不成。但两处硬证据不同：`plans.state` 为 `archived`，且写入/重演/封存返回的是 `closed_plan` 而不是 `rehearsal_required`。注意归档单向，`archive` 只能把状态置为 archived（`pagefold/archive.py:7-9`），没有任何入口能回到 open——因此第二次抄录页面上若仍有可编辑的裁决表单，就可以直接排除「当前已归档」。

3. C、E、F、G 都会在状态库留下与现场互斥的痕迹（release 行、第二条 attempt、blocked 标记或版本未推进），只要补看对应记录即可排除。

### 补看哪一项记录可以排除它们（按性价比排序）

1. **`GET /api/plans/{id}` 的 `state` 与 `revision`**（或计划页顶部「第 N 版 · 状态」）：
   - state=archived → 命中 D 族，封存挡因是 `closed_plan`，且不可恢复编辑；
   - state=sealed 或 `releases` 有行 → 已封存（F/C），与现场矛盾；
   - state=open、revision=2 → 落在 A/B，挡因确定为 `rehearsal_required`。
2. **`GET /api/plans/{id}/attempts` 全量重演历史**（不要只看页面第一条）：
   - 存在 revision=2 且 status=passed → 现场不会被挡（E/F），排除；
   - 存在 revision=2 且 status=blocked → 说明新版重演过但约束失败（G 型），看 `problem.code/detail` 定位冲突；
   - 只有 revision=1 一行 → 与 A/B/D 都相容，继续靠第 1 项区分 D。
3. **`GET /api/plans/{id}/certificate` 与 `/download`**：404 `unpublished` 即从未封存，排除 C/F；200 则凭证里的 `revision` 与 `candidate_digest` 指明实际封存的是哪一版。
4. **状态库直查（服务停机或副本上执行）**：
   ```sql
   SELECT id, revision, state, decisions FROM plans WHERE id = ?;
   SELECT revision, result, length(candidate) FROM attempts WHERE plan_id = ? ORDER BY revision;
   SELECT plan_id, certificate FROM releases WHERE plan_id = ?;
   ```
5. **状态库之外的访问日志**：这是区分 A 与 B 的唯一途径——数一下对 `/decisions` 的 POST 次数和各自的 HTTP 状态码：一条 200 是 A；一条 200 加一条 409 `stale_revision` 是 B。若零散 HTTP 返回里已有 409 原件，按其 `code` 即可归类（`stale_revision`=重试/旧表单；`closed_plan`=归档后写入；`rehearsal_required`=点封存时的挡因，不证明写入路径）。

## 4. 没有完整请求记录时，能确定与不能确定的边界

**能确定：**

- 改变条件的对象是 **plan 的 revision/decisions**，不是 attempt；rev1 的通过记录与候选字节完整无损（已验证候选 SHA-256 与 result 中 `candidate_digest` 一致）。
- 当前第 2 版没有任何 attempt 行，所以封存被挡的直接原因是 `rehearsal_required`（前提：state=open）。
- 成功的 right→base 改选恰好发生一次：每次实际变更只 +1，revision 从 1 到 2 只跨过一个版本。
- 计划从未封存：`releases` 无行，certificate/download 会 404。
- 旧候选（rev1，右支选择）**仍在状态库 `attempts.candidate` 中**，但它不对应任何 release，不能通过 `/download` 取出；只能在状态库副本里核查其字节与摘要。

**不能确定：**

- 被 409 拒绝的请求是否发生过、发生过几次（stale 重试、归档后写入都不留痕）。
- A 与 B 的区别：单次成功改选 vs 成功改选叠加旧表单重试，最终状态逐字节同构。
- 各操作的实际时间与操作人：表结构没有时间戳/审计字段，`rowid` 只反映同库内写入先后。
- 若现场拿不到任何访问日志，A/B 只能并列为「同等可能」，无法进一步收敛。

## 5. 接班人处置指引

**先核查旧候选是否仍在（只读，不改变状态）：**

```sh
python3 -m examples.incident_replay   # 重建七条时序并生成证据，可在临时库上练习
```

对真实状态库执行第 3 节的三条 SQL，确认：rev1 attempt 行存在、`candidate` 非 NULL、摘要与 result 一致；`releases` 为空。

**再判断哪一版可以封存：**

- `state=open`（A/B 情形）：对**当前 revision=2** 重新发起 `POST /api/plans/{id}/rehearse`（带 `revision: 2`）。
  - 通过 → 再 `seal`（revision 2）即可封存；新凭证绑定 rev2 候选，其摘要与 rev1 不同（复现中 rev1=`c9d6dd36…`、rev2=`716f1ce5…`），因为祖本选择和右支选择产出的是不同的结果数据库。
  - 受阻 → 历史会新增一条 blocked 记录，按 `problem.detail` 解决约束冲突后重演；blocked 行没有候选，永远不可封存。
- `state=archived`（D 情形）：代码没有解封入口，不能再重演或封存；若该计划曾封存，归档不销毁 release，`/download` 与 `/certificate` 仍可提取（`archive.py` 与 walkthrough 的 archive 环节保证）。本现场 `releases` 为空，意味着归档状态下无法补出可交付物，只能新建计划重走裁决。
- 不要尝试封存 rev1：即使其候选仍在库中，`seal` 按请求携带的当前 revision 取 attempt，对 rev2 永远查不到 rev1 的候选；对 rev1 的封存请求又会先被 `stale_revision` 挡住。

## 6. 复现材料清单

- `examples/incident_replay.py`：独立可运行的时序矩阵（仅标准库 + 本仓库代码，走真实 HTTP 入口）。
  ```sh
  python3 -m examples.incident_replay ./incident-evidence
  ```
  输出 `A_…`～`G_…` 七组：每组一份 `.http.log`（请求/响应/错误码全文抄录，模拟值班同事的页面+HTTP 碎片）、一份 `snapshot-*.json`（plans/attempts/releases 三表事实，BLOB 以长度表示）、一份独立 `state-*.sqlite`；另有 `summary.json` 汇总并内置 A/B 等价断言。
- 现场同款数据无需额外准备：`pagefold/demo.py` 的 quartz/cerulean/umber 即 README 所述样例，冲突项为 `node` 复合主键 `('north',1)`；stress-left/stress-right 用于 G 的约束受阻。
- 既有回归：`python3 -m examples.walkthrough all` 的 `attempts` 环节断言了同一机制（「旧记录保留但不能封存新版本」，`walkthrough.py:123-128`），可作为机制未回归的旁证。
- `incident-evidence/` 已加入 `.gitignore`（含状态库与临时证据），需要随工单移交时单独打包。
