# 案卷索引：Pagefold 同计划两轮不同选择重演

- 案卷编号：`case-2026-11-25`
- 计划 P：`c6f8373ae70a4fd3ab5c0e0126f265a8`（同一计划，连续两轮不同裁决）
- 服务入口：本机 `127.0.0.1`，持久化状态库 `materials/state/pagefold-state.sqlite`
- 流水规模：真实 HTTP 请求 **50 次**（浏览页 HTML + API JSON + 字节下载），全部逐响应留档
- 终态：计划 P 已 **归档（archived）**，终版 **revision 2**
- 离线核对：**21 项全部通过 = 14 项可复算（recomputed）/ 5 项据当时操作记录（attested）/ 2 项证据缺口（gap）**
  见 `materials/derived/verification-report.json`

> 本索引面向没有全时段页面截图的同事。所有结论都给出可打开的相对路径与流水序号（seq NN 对应 `materials/run-log.jsonl` 与 `materials/http/NN-*`）。证明力分三级：
> **可复算**＝任何人用留存输入离线重跑都能得到同一结果；**据记录**＝只能由当时的请求/响应/状态库记录确认；**缺口**＝历史中本就不存在，刻意保留、不倒推填平。

---

## 1. 一句话结论

在同一份计划 P 上，对同一冲突行 `node ('north',1)` 实际做了**两次不同选择并各重演一次**：

| 轮次 | 版本 | 选择 | 重演结果 | 应用行数 | 候选 SHA-256（前 16 位） | 处置 |
|---|---|---|---|---|---|---|
| 第一轮 | rev1 | **右支 right** | passed | 3 | `c9d6dd3673ae9157…` | 仅留存为历史，**未封存、不可下载、无凭证** |
| 第二轮 | rev2 | **祖本 base** | passed | 2 | `716f1ce54f75dd24…` | **被封存**，可下载、有凭证，归档后仍可提取 |

两版候选字节都在状态库里物理存在且摘要已离线复核；但**只有 rev2 进入封存表 `releases`**。

---

## 2. 哪一版"仍可读取"，哪一版"会被封存"（关键区分）

### rev1（右支选择）——仍可读取，但不会、也无法再被封存
- **可读取的三层证据**
  - HTTP 重演历史 JSON：seq 24 / 28 / 31（`GET /api/plans/P/attempts`），其中 rev1 标 `历史版`。
  - 浏览页 HTML 历史区：seq 25、seq 32（`materials/http/25-*.html`、`32-*.html`），渲染为
    `第 1 版 · 通过 · 历史版 应用 3 行 c9d6dd36…`。
  - 候选**完整字节**仅存于状态库 `attempts` 表（plan P, revision 1, candidate 24576 B），
    其摘要与重演结果、与离线重建三者一致（核对项 `rev1-persisted`、`rev1-row`）。
- **不可封存/不可下载的原因（有记录佐证，非推断）**
  - HTTP 没有针对历史 attempt 的下载入口；`GET /download`、`/certificate` 只认 `releases`。
  - 改选到 rev2 后，曾尝试用旧记录直接封存 rev2 → seq 29 返回 `409 rehearsal_required`，
    证明旧版记录不能用于新版封存。
  - 计划现已归档，seq 41 再请求重演返回 `409 closed_plan`，rev1 永远停在历史版。

### rev2（祖本选择）——被封存的一版
- seq 33 `POST /seal` 成功；seq 34 重复封存返回**同一凭证**（幂等）。
- 封存后再改裁决 → seq 35 返回 `409 closed_plan`，证明封存冻结了选择。
- 凭证：`materials/downloads/certificate-plan-P.json`（seq 36/43 内容一致）。
- 下载候选：`materials/downloads/candidate-plan-P-rev2.sqlite`（seq 37/42 字节一致，24576 B）。
- 凭证 `candidate_digest = 716f1ce5…` 与下载文件 SHA-256、与离线重建结果**三者相同**（核对项 `sealed-digest`）。
- 归档（seq 39）不销毁封存物：seq 42/43 仍能取回同一候选与凭证。

> 同一冲突行的三方取值在浏览页 triptych 完整可见（`materials/http/32-*.html`）：
> 祖本 payload `ABQ=`(=0014)、weight 1.5；左支 note `左支覆写`；右支 payload `/wA=`(=ff00)、weight 2.25。
> 封存候选该行实测为祖本值 `('祖本节点', 0014, 1.5)`（核对项 `sealed-row`，可复算）；
> rev1 候选该行实测为右支值 `('祖本节点', FF00, 2.25)`（核对项 `rev1-row`，可复算）。

---

## 3. 证据缺口（保留，不填平）

| 缺口 | 事实依据 | 为什么不能倒推 |
|---|---|---|
| **从未选择"左支 left"重演** | 状态库 `attempts` 中计划 P 实际只有 revision 1、2 两条；流水无任何 left 裁决 | 左支候选的摘要与字节在历史中根本不存在。虽能用留存输入**另算**一个 left 候选，但那将是新实验、不是当时操作，故不写入本案结论（核对项 `gap-left-choice`） |
| **stress 计划 Q 重演受阻，无候选** | seq 48 `status=blocked`，`UNIQUE constraint failed: bench.label`；状态库该 attempt 的 candidate 为 **NULL**；seq 49/50 下载与凭证均 `404 unpublished` | blocked 不产生任何候选字节，404 留档即其全部证据，无法、也不应补造（核对项 `gap-stress-blocked`） |

---

## 4. 三层证据落点（浏览页 / HTTP / 凭证）

| 证据层 | 位置 | 代表条目 |
|---|---|---|
| 浏览页（服务器渲染 HTML，替代截图） | `materials/http/*.html`（共 9 个计划/镜像/行页面） | seq 15 待裁决、21 右支已选、32 两版历史并列、38 已封存、44 已归档 |
| HTTP API 返回（JSON / 状态码） | `materials/http/*.json`、`*.bin` 与 `run-log.jsonl` | 裁决推进/拒绝（18–20、26、35）、重演（22、30、48）、封存（33–34）、各类 4xx（06、13、17、19、29、41、49、50） |
| 封存凭证 | `materials/downloads/certificate-plan-P.json` | 绑定三来源、模式、计划、裁决、候选五份摘要与 revision=2 |
| 封存候选字节 | `materials/downloads/candidate-plan-P-rev2.sqlite` | 可独立打开的 SQLite，完整性 ok、零外键违规 |
| 持久化全量历史 | `materials/state/pagefold-state.sqlite` | `images/plans/attempts/releases` 四表；rev1、rev2 候选字节均在内 |
| 当时输入字节 | `materials/state/uploaded-*.sqlite`（5 份） | 复算的唯一输入，摘要即登记 id |

### 按阶段的流水索引（seq 区间）
- 01–06 镜像登记：三库登记、重复上传复用同 id（seq 05）、坏镜像 422（seq 06）
- 07–13 目录与行核对：HTML+JSON 双证据，未知镜像 404
- 14–17 建计划 P：3 项差异（2 自动 + 1 冲突），未决重演 409
- 18–25 **第一轮（右支）**：裁决→rev1，旧版序 409，幂等不涨版，重演 passed，历史仅 rev1
- 26–32 **第二轮（祖本）**：改选→rev2，旧记录封存被拒 409，重演 passed，历史两版并列
- 33–38 **封存 rev2**：封存/幂等/冻结/凭证/下载
- 39–44 **归档**：状态转 archived，写入被拒，封存物仍可取
- 45–50 **旁证 stress 计划 Q**：各自合法的两支合流触发唯一约束，blocked、无候选、无凭证

机器可读的完整逐条索引（含每请求方法、路由、状态码、字节数、留档文件路径、是否符合预期）：`materials/run-log.jsonl`。

---

## 5. 关键标识与摘要

| 项 | 值 |
|---|---|
| 祖本 base（quartz） | `90438d5a76cd0744920d78acb009df1d19aa23baad0a7ed6775927b53e7a1a25` |
| 左支 left（cerulean） | `ab1bceb83ee5599db0a06e4429977951f26378db082cbffa052f8215a14d6f75` |
| 右支 right（umber） | `d6a547d0982d6150b30a8a78706c3db162c3d339c41ee918a718e46dce39a009` |
| 冲突条目 id | `3d9fecf2f8e0e841605f0984d234e878b149ba4a0758ce68606e4c9543b0c3bf`（表 node，键 `('north',1)`） |
| 模式签名 schema | `2850b20c7d73163e0e8f153ae064af159f3389cc6d3ed1a5c909218ba29fb6ac` |
| 计划摘要 plan_digest | `a9d19d0259fa61f4fac8ab9119ecce992be8764d5e66df3b27808e00f4722f32` |
| 终版裁决 decision_digest | `484fc47f2a4a38c887e61297befe922082e224fba49595f587fa5925cf49d3dd` |
| rev1 候选（右支，历史） | `c9d6dd3673ae91571c1cd52197a3b18bcb082b74a159de777c32f4cfcc6a3d24` |
| rev2 候选（祖本，封存） | `716f1ce54f75dd24e09f70cda01582c240b10debf2763b8ea9678c393dae269f` |

---

## 6. 结论的证明力分级（详见 verification-report.json）

- **可复算（14 项）**：三来源字节摘要、计划/模式/裁决摘要、rev1 与 rev2 离线重建摘要、下载候选摘要与独立可打开性、冲突行取值、rev1 在状态库的物理留存与摘要。
  复算路径：`pagefold.merge.calculate` → `pagefold.replay.amendments/rebuild`，输入仅 `state/uploaded-*.sqlite`。
- **据当时操作记录（5 项）**：仅 rev2 进入 releases、计划终态 archived/rev2、50 条流水完整且状态码相符、浏览页"已封存/已归档"文案、历史区两版并列。
- **缺口（2 项）**：left 选择从未发生；stress 受阻无候选。均按原样保留，未补造。

---

## 7. 目录与复现方式

```
case-2026-11-25/
├── CASE-INDEX.md                 # 本索引（人工入口）
├── scripts/                      # 受信脚本，与材料分目录
│   ├── serve.py                  # 启动持久化服务（状态库落在 materials/state）
│   ├── run_rehearsals.py         # 真实 HTTP 两轮重演 + 逐响应留档（生成 run-log 与 http/）
│   ├── verify_case.py            # 离线复算/核对（不联网、只读状态库），产出核对报告
│   └── build_manifest.py         # 逐文件 SHA-256 清单
└── materials/
    ├── MANIFEST.sha256 / .json   # 60 个文件的哈希清单（sha256sum 兼容，可独立校验）
    ├── run-log.jsonl             # 50 次请求的机器可读流水
    ├── http/                     # 每次响应体留档（HTML/JSON/字节），NN 前缀=流水序号
    ├── downloads/                # 封存凭证 + 封存候选（rev2）
    ├── state/                    # 状态库、5 份上传输入、运行标识、服务日志
    └── derived/                  # 离线核对报告
```

复现 / 复核（Python 3.11+，标准库，均以隔离模式运行）：

```sh
# A. 仅离线复核已有案卷（不启动服务、不联网）
python3 -I scripts/verify_case.py
python3 -I scripts/build_manifest.py
(cd materials && sha256sum -c MANIFEST.sha256)   # 期望 60/60 OK

# B. 从零重跑两轮真实 HTTP 重演（会重建 materials，旧证据将被覆盖，请先备份）
python3 -I scripts/serve.py 8765 &               # 监听 127.0.0.1:8765
PAGEFOLD_PORT=8765 python3 -I scripts/run_rehearsals.py
```

说明：所有脚本显式把受信项目根加入 `sys.path`，材料目录从不加入路径；读取上传材料一律经 `python3 -I`，避免材料目录内同名模块被导入。

---

## 8. 版本控制可见性提示（重要）

仓库根 `.gitignore` 含 `*.sqlite`、`*.sqlite-shm`、`*.sqlite-wal`，因此本案卷中的 **7 个 SQLite 证据文件不会被 git 跟踪，但它们在本地真实存在且完整可用**，并已全部计入 `materials/MANIFEST.sha256`（哈希锚定，可用 `sha256sum -c` 校验）：

- `materials/state/pagefold-state.sqlite`（持久化全量历史）
- `materials/state/uploaded-{quartz,cerulean,umber,stress-left,stress-right}.sqlite`（5 份当时输入）
- `materials/downloads/candidate-plan-P-rev2.sqlite`（封存候选）

即：通过 git 取案卷时这些字节默认不带出，需连同本地 `materials/` 目录整体移交；缺失任一文件时，`MANIFEST.sha256` 校验会立即报错，不会被静默当作"无此证据"。其余 53 个文件（HTML/JSON/流水/报告/清单/脚本/索引）均正常纳入版本控制。
