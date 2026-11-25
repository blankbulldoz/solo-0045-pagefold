# 值班演练卡（三张）

三张可直接导入 Pagefold 的 SQLite 镜像组，彼此独立、互不取代。每张卡只让分支操作不同，
差异全部落在**合流产物**上；三份输入本身都通过 `integrity_check` 和外键检查。

| 卡 | 主题 | 输入是否合法 | 合流产物 | 结局 |
|---|---|---|---|---|
| D1 | 唯一键碰撞 | 合法 | 受组 | `blocked` · `constraint` |
| D2 | 删除导致新增引用孤立 | 合法 | 受组 | `blocked` · `foreign_key` |
| D3 | 复合外键部分为空**不**产生孤立 | 合法 | **通过** | `passed`（本来就应通过） |

三张卡共用一份模式（`site` / `rack` / `lease`，复合主键、`WITHOUT ROWID`、复合外键、
`UNIQUE`、`CHECK`）。每张卡另有一行三边相同的识别行 `site('drill-dN', …)`，
便于在共享状态库中区分卡别，不参与合流。

## 目录

```
drills/
  cards/
    01-unique-collision/{quartz,cerulean,umber}.sqlite  card.json  SHA256SUMS  RUNBOOK.md
    02-orphan-delete/…
    03-partial-null/…
    index.json                  # 三卡一览
  spec.py                       # 权威定义（模式、分支操作、预期）
  build.py                      # python3 -m drills.build 重新生成 cards/
  run_card.py                   # 单卡：走真实 HTTP 入口并逐条核对
  verify.py                     # 全部卡：独立临时服务上核验并回放
  wire.py                       # 共用 HTTP 客户端与断言
```

`card.json` 是每张卡的权威预期：文件摘要、模式摘要、每个差异项、重演结局、
诊断码片段、通过卡的产物行数与逻辑行摘要。`RUNBOOK.md` 是给操作者照着做的一页步骤。

## 导师一次备环境

```sh
python3 -m compileall -q pagefold drills          # 语法检查
python3 -m drills.build                           # 可选：重建镜像（应字节稳定）
python3 -m drills.verify                          # 全部卡离线核验，末端打印“全部演练卡核验通过”
```

`verify` 会为每张卡启动独立的临时状态库和本机 HTTP 服务，因此可以放心连续跑。
若只想抽查一张：`python3 -m drills.verify 02-orphan-delete`。

## 轮转给不同人

每位操作者用**自己的一份新状态库**启动服务，只拿自己那张卡的目录和 RUNBOOK：

```sh
python3 -m pagefold --state /tmp/pagefold-<slug>-<姓名>.sqlite
python3 -m drills.run_card <slug> --origin http://127.0.0.1:8745
```

同一份状态库也可以连续跑三张卡：三张卡的镜像摘要互不相同，计划也彼此独立；
`run_card` 只新增镜像、计划和重演记录，不改动既有的卡。重复运行同一张卡会复用镜像，
创建新计划，结果一致。

## 各自该看到什么

- **D1 唯一键碰撞**：7 个自动项，第 0 版重演 `blocked`，诊断
  `site [["t","ridge"]]：UNIQUE constraint failed: site.name`。左支 `site('ridge','脊线')`
  与右支 `site('mesa','脊线')` 主键不同，计划不视其为冲突，合流后撞上 `site.name` 的 `UNIQUE`。
  正确答案是**不通过**，且不得下载或封存受组候选。
- **D2 删除致孤立**：4 个自动项，第 0 版重演 `blocked`，诊断
  `外键检查失败：lease → rack（约束 0）`。左支删除 `rack('north',2)`，右支新增引用它的
  `lease(5,'north',2,…)`；两边各自合法，合流后引用悬空。
- **D3 部分为空**：5 个自动项，第 0 版直接 `passed`（不需要任何裁决）。`lease` 的
  `(site,slot)` 复合外键在任一列为 NULL 时按 MATCH SIMPLE 跳过检查，所以
  `lease(7,'north',NULL)`、`lease(8,NULL,2)`、`lease(9,NULL,NULL)` 都不是孤立。
  封存后下载应是可行且最终的结局；若它被阻断，先查环境而不是数据。

## 断言依据

- 输入合法性在 `build.py` 中用应用自身的 `describe` / `check_database` / `read_image` 校验，
  并另外直查 `PRAGMA integrity_check` 与 `PRAGMA foreign_key_check`。
- 合流预期由 `build.py` 内一份**不依赖 `pagefold.merge`** 的独立实现重算，
  通过卡的产物行数与逻辑行摘要写入 `card.json`。
- `verify.py` 只经由公开 HTTP 入口操作，并核对：镜像摘要复用、模式摘要、逐项差异、
  重演结局与诊断码、受组无下载/无凭证、通过卡封存凭证与下载字节摘要、产物逻辑行摘要、
  重演/封存的幂等性。
