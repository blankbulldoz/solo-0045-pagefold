# 报表读法对照成果：同一源库、两种裁决

这份成果给下游报表组一个最小但完整的对照样本：**同一组源镜像**经 Pagefold 实际合流两次，
分别把两处冲突裁决到左支与右支，得到两个独立、可下载、已封存的 SQLite 成果库。

- 两个库的**跨表业务汇总计数完全相同**；
- 明细却落在**不同的复合键**上；
- 两份成果各自保留 Pagefold 封存凭证，凭证指向同一组来源摘要。

报表维护者无需启动 Pagefold，只需要任意 SQLite 工具（`sqlite3` 命令行、
DB Browser for SQLite、SQLiteStudio、Python 的 `sqlite3` 模块等）即可验证。

## 目录

```
deliverables/reporting-pair/
├── README.md                          # 本说明
├── manifest.json                      # 摘要、计数、差异键、凭证索引
├── verify.py                          # 独立校验脚本（只用 Python 标准库）
├── SHA256SUMS                         # 交付文件校验和
├── sources/                           # 合流输入，仅用于佐证凭证
│   ├── base.sqlite                    #   共同祖本
│   ├── left.sqlite                    #   左支
│   └── right.sqlite                   #   右支
├── outcomes/
│   ├── decision-left.sqlite           # 裁决到左支的成果库
│   ├── decision-left.certificate.json # 对应封存凭证
│   ├── decision-right.sqlite          # 裁决到右支的成果库
│   └── decision-right.certificate.json
└── queries/
    ├── key_by_key.sql                 # 逐键对比（定位差异复合键）
    ├── grouped_history.sql            # 分组统计（证明两库汇总相同）
    └── wrong_join_counterexample.sql  # 复现“只连接复合键一部分”的误报
```

## 数据模型与业务计数

三张表，复合键为 `node(bench, slot)`，`binding` 用 `(bench, slot)` 外键指向节点。

- `node`：4 个节点 —— `north/1`、`north/2`、`south/1`、`south/2`
- `binding`：3 条绑定 —— `id=10`、`id=20`、`id=30`
- 两处冲突：`id=10` 与 `id=20` 的归属在左支与右支中不同，必须逐行裁决
- `id=30` 在三个分支中相同，自动采纳，始终绑定 `north/2`

两个成果的跨表汇总（在 `verify.py` 与 `manifest.json` 中都有记录）：

| 指标 | 左支裁决 | 右支裁决 |
| --- | ---: | ---: |
| 节点总数 | 4 | 4 |
| 绑定总数 | 3 | 3 |
| 有绑定的节点 | 2 | 2 |
| **未绑定节点** | **2** | **2** |
| **多重绑定节点** | **1** | **1** |
| 绑定数直方图 | 0×2, 1×1, 2×1 | 0×2, 1×1, 2×1 |

明细差异（复合键级）：

| `(bench, slot)` | 左支绑定数 | 右支绑定数 | 情况 |
| --- | ---: | ---: | --- |
| `north/1` | 0 | 2 | 左支未绑定，右支多重绑定 |
| `south/2` | 2 | 0 | 左支多重绑定，右支未绑定 |
| `north/2` | 1 | 1 | 未受影响（对照） |
| `south/1` | 0 | 0 | 两库都未绑定（对照） |

这样设计的目的：单看任何汇总或直方图，两库完全一样；只有按复合键下钻，
才能看出“未绑定/多重绑定”这两种异常落在不同节点上。报表在这两种情况下
最常把键或连接条件读错，样本正好覆盖。

## 两步验证误报

以下命令都**先进入本目录**（`queries/*.sql` 使用相对路径 `outcomes/...`）：

```sh
cd deliverables/reporting-pair
```

用 `sqlite3` 命令行执行（推荐；`:memory:` 只是承载 `ATTACH` 的空宿主）：

```sh
sqlite3 :memory: < queries/grouped_history.sql
sqlite3 :memory: < queries/key_by_key.sql
sqlite3 :memory: < queries/wrong_join_counterexample.sql
```

没有 `sqlite3` 命令行时，可用任意能执行 SQL 文件的工具（DB Browser 的
“执行 SQL”窗口、SQLiteStudio 等）。用 Python 标准库执行同一个 SQL 文件：

```sh
python3 - <<'PY'
import sqlite3
db = sqlite3.connect(":memory:")
for stmt in open("queries/grouped_history.sql").read().split(";"):
    stmt = "\n".join(l for l in stmt.splitlines() if not l.strip().startswith("--")).strip()
    if not stmt:
        continue
    cursor = db.execute(stmt)
    for row in cursor.fetchall():
        print(row)
PY
```

### 第一步：分组统计 —— 两库必须一致

`grouped_history.sql` 把每个复合键的绑定数折成直方图，并按成果分组：

```
left  | 0 | 2
left  | 1 | 1
left  | 2 | 1
right | 0 | 2
right | 1 | 1
right | 2 | 1
```

**判读：** 两份成果的桶计数应当逐行相同。如果报表给出的“未绑定节点数”
或“多重绑定节点数”在两库间不同，报表的读法已经错了 —— 这两类异常
在样本里是**同样多**的，只是落在不同键上。

### 第二步：逐键对比 —— 差异只能出现在指定复合键

`key_by_key.sql` 以完整复合键 `(bench, slot)` 连接两份成果，输出所有绑定数不同的键：

```
bench | slot | left_bindings | right_bindings | state
north | 1    | 0             | 2              | changed
south | 2    | 2             | 0              | changed
```

**判读：**

1. 输出应当恰好是 `north/1` 与 `south/2` 两行，且 `state` 均为 `changed`；
2. 若出现 `left-only` / `right-only`，说明报表或查询把两库的键集合对错了；
3. 若报表标记的异常键不在这两行里，或这两行之外还有差异，问题在报表侧；
4. `north/2`（稳定单绑定）与 `south/1`（两库都未绑定）不在结果里，可作为
   “并非所有键都移动”的对照。

### 反例：一次错误连接如何制造误报

`wrong_join_counterexample.sql` 故意只按 `bench` 连接、漏掉 `slot`，模拟报表最常见的
复合键误用。结果变成：

```
outcome | unbound | multi
left    | 0       | 2
right   | 2       | 2
```

同一批数据、同一套计数口径，仅仅因为连接条件少了复合键的一半，就得出
“左支没有未绑定节点、右支有两个”的错误结论。把这条反例与第一步的正确直方图
并排看，就能向报表维护者证明：**汇总差异不是合流造成的，而是连接条件丢了 `slot`。**

## 来源凭证

每个成果目录内有一份 `pagefold-certificate-v1` JSON：

- `sources`：`base` / `left` / `right` 三个镜像按原始字节的 SHA-256；
- `schema`：三份镜像共同的模式摘要；
- `plan` / `revision`：各自的合流计划与裁决版本；
- `plan_digest` / `decision_digest`：目标行集合与裁决集合的摘要；
- `candidate_digest` / `candidate_size`：与同目录 `.sqlite` 文件逐字节对应。

两份凭证的 `sources` 与 `schema` 完全相同，`plan`、`decision_digest`、
`candidate_digest` 不同。这正是“同源、不同裁决”的可核验表述。
`manifest.json` 汇总了来源摘要、两库计数、差异键和凭证路径。

## 独立校验

```sh
python3 verify.py
```

脚本不导入 Pagefold、不联网、不写文件，只读交付物，检查：

1. 三个源镜像与两个成果文件是否与 `manifest.json`、凭证中的摘要一致；
2. 两个成果是否通过 `PRAGMA integrity_check` 与 `PRAGMA foreign_key_check`；
3. 七个跨表计数与直方图是否在两库中完全相同；
4. 差异是否恰好只落在 `north/1` 与 `south/2`，且两个对照键未被波及。

全部通过时退出码为 0。

## 复现

两个成果库由两次真实 HTTP 合流生成，不是手工拼装。仓库内可重建：

```sh
python3 -m examples.reporting_pair deliverables/reporting-pair
```

- 三个源镜像与两个成果的**字节内容**可重复生成（摘要稳定）；
- 计划 ID 由 Pagefold 在创建计划时随机分配，因此 `plan` 字段每次不同，
  但来源、模式、候选字节与凭证绑定关系不变。

## 给报表维护者的最短检查单

1. 用任意 SQLite 工具打开 `outcomes/decision-left.sqlite` 与 `decision-right.sqlite`；
2. 跑 `grouped_history.sql`：两库直方图必须一致，否则是报表汇总口径问题；
3. 跑 `key_by_key.sql`：差异必须只有 `north/1` 与 `south/2`，否则是复合键读法问题；
4. 用 `wrong_join_counterexample.sql` 复现误报，确认报表连接条件是否漏了 `slot`；
5. 需要留档时，把两份 `.certificate.json` 与 `manifest.json` 一并归档。
