# Pagefold 交班目录（2026-11-25）

本次交班用**同一组样例镜像**（`quartz` 祖本、`cerulean` 左支、`umber` 右支，外加
`stress-left` / `stress-right` 两个合法但合流时撞唯一约束的分支）在同一状态库
`handover/handover-state.sqlite` 中跑出三个现场，分别覆盖计划状态机的三种交班姿态：

| 现场 | 计划状态 | 还有工作？ | 可领取文件 | 仅作历史的记录 | 计划 ID |
|---|---|---|---|---|---|
| `site-a-open/` 开放 | `open`（第 3 版，1 项待裁决） | **是**，需继续裁决 | 无（下载/凭证均 `unpublished`） | rev1 一条 `passed` 重演、受阻旁证计划一条 `blocked` | 见各目录 `case-summary.json` |
| `site-b-archived/` 已归档未封存 | `archived`（第 0 版） | 否，写入口已全部关闭 | 无（从未封存，归档不会补发成果） | 一条 `blocked` 重演，只可查询 | 同上 |
| `site-c-sealed/` 归档后仍有封存成果 | `archived`（第 1 版，封存后归档） | 否，写入口已关闭 | **有**：`release/candidate.sqlite` 与 `release/certificate.json` | rev1 那条 `passed` 已固化为发布对象，不再是草稿 | 同上 |

## 一条总规则：看见“通过”不等于可交付

重演记录（attempt）和可交付成果（release）是两张表、两种资格：

- `attempts` 表里的 `passed` 只证明**它所属的那个版本**曾经重演成功；裁决一变、版本推进，
  旧记录立即降为“历史版”，封存时按**当前版本**重新校验，查不到当前版的通过候选就拒绝
  （`rehearsal_required`）。
- 只有 `releases` 表里存在记录的计划，`/download` 和 `/certificate` 才可取；否则一律
  404 `unpublished`。归档只改计划状态，**不创建、也不销毁** release。

`site-a-open` 专门保留了这条反例：rev1 选右支并重演 `passed`，随后改判祖本（rev2）、
再清除裁决（rev3，当前版）。现在 `GET /attempts` 仍能看到那条“通过”，但它属于 rev1，
当前 rev3 未裁决，封存被拒、无任何文件可领。**不要因为看到“通过”字样就把它归类为可交付。**

## 三个现场逐一说明

### 1. `site-a-open/` —— 开放，仍有工作（接手后要继续干活）

- 计划保持 `open`，当前第 3 版、`unresolved=1`，冲突行是 `node` 复合主键
  `('north', 1)`，等待在祖本/左支/右支之间裁决。
- 历史：rev1 的 `passed` 尝试（候选摘要 `c9d6dd36…`）已**失去当前资格**，仅留档；
  对当前版封存实测返回 `rehearsal_required`，当前版重演返回 `unresolved`。
- 同目录还挂了一个受阻旁证计划（`stress-plan-page.html`，ID 见摘要）：两个分支各自合法，
  合流后撞 `bench.label` 唯一约束，`blocked` 且无候选、无 release，演示“行级选择成立
  ≠ 关系约束成立”。
- 可领取文件：**无**。下载与凭证入口均 404 `unpublished`。

### 2. `site-b-archived/` —— 已归档未封存（没有可领的，记录只是历史）

- 用 stress 分支建计划，rev0 重演即 `blocked`（唯一约束冲突），封存被拒
  （`rehearsal_required`），随后直接归档（允许 open→archived，不要求先封存）。
- 归档后 `rehearse` / `seal` / `decisions` 全部 409 `closed_plan`；重复 `archive` 幂等返回
  同一归档态，不产生新写入。
- 可领取文件：**无**。归档后下载仍是 404 `unpublished`——历史记录不能当交付物。
- 仅剩的价值是审计：`attempts` 里那条 `blocked` 和本目录证据可解释“为什么没有成果”。

### 3. `site-c-sealed/` —— 封存后归档（成果保留，可领取、可离线复核）

- 正常三分支计划，冲突选右支，rev1 重演 `passed` 后封存（open→sealed），再归档
  （sealed→archived）。
- 归档后重演/裁决仍 409 `closed_plan`，但**封存对象原样保留**：归档前后下载字节、凭证
  完全一致（摘要 `c9d6dd36…`，24576 字节）。
- 可领取文件（本目录 `release/` 各存一份，也可随时从运行中的服务重新下载）：
  - `candidate.sqlite`：独立 SQLite 成果，`PRAGMA integrity_check=ok`、
    `foreign_key_check` 为空；
  - `certificate.json`：绑定三来源摘要、模式摘要、计划/裁决摘要、版本与候选摘要。
- 离线复核方式见 `NEXT_OPERATOR.md`，凭证里的 `candidate_digest` 必须等于下载文件的
  SHA-256。

## 目录结构与证据索引

```
handover/
├── README.md                     # 本文件（交班目录）
├── NEXT_OPERATOR.md              # 下一位操作者入口与命令
├── handover-state.sqlite         # 唯一状态库：三个现场都在这里（git 忽略，可重建）
├── images-manifest.json          # 同一组样例镜像的 ID/标签/大小
├── cases-index.json              # 三现场汇总：状态、版本、计划ID、各入口路径
├── http-trace.json               # 构造全过程 44 个 HTTP 请求的方法/状态码/响应
├── verify-report-offline.json    # 离线复核 22 项（状态库 + 制品）
├── verify-report-live.json       # 在线只读复核 13 项（运行中的服务）
├── site-a-open/
│   ├── case-summary.json         # 现场A 裁决/拒绝码/历史尝试/入口
│   ├── plan-page.html            # 渲染页：裁决中 · 1 项待裁决 · 历史版“通过”
│   └── stress-plan-page.html     # 受阻旁证计划渲染页
├── site-b-archived/
│   ├── case-summary.json
│   └── plan-page.html            # 渲染页：已归档 · 重演历史“受阻” · 无下载入口
└── site-c-sealed/
    ├── case-summary.json
    ├── plan-page.html            # 渲染页：已归档 · 仍有“下载 SQLite 成果/凭证”
    └── release/
        ├── candidate.sqlite      # 可领取成果（与服务下载字节一致）
        └── certificate.json      # 可领取凭证
```

## 怎么按目录复核

```sh
# 离线（不需要起服务，直接核状态库与落盘制品，22 项）
python3 -I tools/verify_handover.py --offline

# 在线（先起服务，只读核对，不写入，13 项）
python3 -m pagefold --state ./handover/handover-state.sqlite   # 终端 A
python3 -I tools/verify_handover.py --live                     # 终端 B
```

两份报告分别写入 `verify-report-offline.json` / `verify-report-live.json`，任一检查失败
脚本以非零码退出。重建整个现场（会先清空 `handover/` 再重放）：
`python3 -m tools.build_handover`。构造与复核脚本在 `tools/`，均只走仓库自带的真实
HTTP 入口，不直接改业务表。

## 判定速查

- **还有没有工作**：计划 `state=open` 即有（看 `unresolved`）；`sealed`/`archived` 即停。
- **哪些文件能领取**：仅当 `releases` 表有该计划记录（现场 C）。判断依据是 release，
  不是 attempts 里的 `passed`。
- **什么记录只是历史**：所有 `attempts` 行——无论 `passed` 还是 `blocked`——在版本推进
  或计划封存/归档后都只是可查询的历史；`blocked` 永远不产生候选，旧版 `passed` 不能用于
  新版封存。
