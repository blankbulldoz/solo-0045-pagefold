# Pagefold 评审样本 · 文件清单与差异表

- 生成：`python3 -m review.make_samples`（确定性生成，无随机因素；`python3 -m review.make_samples --check` 校验逐字节复现）
- 生成时间：2026-11-25 05:07:51 UTC
- 模式：`entry(id INTEGER PRIMARY KEY, name TEXT, qty INTEGER, weight REAL, note TEXT) STRICT` ＋ `tag(entry_id, label) STRICT, WITHOUT ROWID`（外键指向 entry）＋ 索引 `tag_label`；
  每组三份镜像的建表与索引记录逐字节一致，满足工作区的模式匹配要求。
- 镜像文件不入库（`.gitignore` 忽略 `*.sqlite`），运行生成脚本即可重建相同文件。

## 文件清单

| 文件 | 字节 | entry 行 | tag 行 | SHA-256 |
|---|--:|--:|--:|---|
| `review/images/a-small-conflict/base.sqlite` | 16,384 | 8 | 10 | `028971b6f6acd5fb8f2c2b09c7cdbaf0f5402725885316adc34080ee6ac81ced` |
| `review/images/a-small-conflict/left.sqlite` | 16,384 | 9 | 11 | `4d42fda02715eb734374909e2f552cfae799d8937ba451c8d393cf2da119c553` |
| `review/images/a-small-conflict/right.sqlite` | 16,384 | 8 | 9 | `ad87ef70e1550457adce7fb6ca92c5f9b0099a321ace0dfe355b65f49f93ad47` |
| `review/images/b-dense-auto/base.sqlite` | 122,880 | 2,000 | 2,000 | `02914d055ce0db6fcbde9c23d4c9682187d53f04fbea6208a7ccfc760fdf4989` |
| `review/images/b-dense-auto/left.sqlite` | 159,744 | 2,200 | 2,200 | `205dbeb8cfc2dd20e10824c30faf5994267b746e2a40a02bc2ca71389ed5ab76` |
| `review/images/b-dense-auto/right.sqlite` | 122,880 | 2,000 | 2,000 | `4d1ca83e957650b17dc68ae7db4998d669746a31e6389959450d1f492305789e` |
| `review/images/c-bulk-identical/base.sqlite` | 413,696 | 8,000 | 8,000 | `e9cab38587dee4e0dc794296b53697e0daff2db7d2ea9184fe3b4a2e53898453` |
| `review/images/c-bulk-identical/left.sqlite` | 417,792 | 8,001 | 8,001 | `40793cca587a9efd607eb8039b33745487ef4b172a701b4dbb722500f2332d64` |
| `review/images/c-bulk-identical/right.sqlite` | 417,792 | 7,999 | 7,999 | `eca71ecb458decd73f41d1657ed6b21c1e1b88b1012eb5a29bbaf83d23eb02c2` |
| `review/images/d-noop-decisions/base.sqlite` | 16,384 | 24 | 24 | `14fa4b1ca381c47459e8706ab8f5ea73ce293378df8299d517a5a0eb6ea82842` |
| `review/images/d-noop-decisions/left.sqlite` | 16,384 | 25 | 25 | `9499bdc5d381b2514fbd392e07395052294aad64c8bd7f3f15d77d421ecb5903` |
| `review/images/d-noop-decisions/right.sqlite` | 16,384 | 23 | 23 | `d849c2df18279f096c4f114392b09cd5004fcd10e7ce407c99fa1a090fae1839` |

## 汇总：输入体积 vs 预期计划条目

| 组 | 输入行合计 | 输入字节合计 | 三份相同行 | 预期计划条目 | 其中冲突 | 条目/千行输入 |
|---|--:|--:|--:|--:|--:|--:|
| a-small-conflict（小冲突） | 55 | 49,152 | 15 | 5 | 1 | 90.91 |
| b-dense-auto（密集自动更新） | 12,400 | 405,504 | 2,900 | 1,500 | 0 | 120.97 |
| c-bulk-identical（大量相同行） | 48,000 | 1,249,280 | 15,995 | 7 | 1 | 0.15 |
| d-noop-decisions（空转裁决（有效成果刻意少于计划项）） | 144 | 49,152 | 40 | 10 | 6 | 69.44 |

## a-small-conflict — 小冲突

用途：少量行分歧：核对逐行裁决、版本拦截、封存与归档流程。

来源（祖本 → 分支，均为确定性生成）：
- 祖本：entry 1–8（qty=id%50，note=祖本记录-i）；每个 entry 一条 tag 't1'，entry 2、3 各多一条 't2'（共 8 + 10 行）。
- 左支 = 祖本 + ：改写 entry 1 的 note；新增 entry 9 及其 't1' 标签。
- 右支 = 祖本 + ：改写 entry 1 的 qty；改写 entry 3 的 note；删除 tag (2,'t2')。
- 评审裁决：entry 1 的冲突选右支（qty=999，note 保持祖本值）。

差异表（独立于 pagefold 的 SQL 逐键对比；“增/改/删”相对祖本）：

| 表 | 祖本行 | 三份相同 | 仅左支变 | 仅右支变 | 双侧同值 | 双侧冲突 |
|---|--:|--:|--:|--:|--:|--:|
| entry | 8 | 6 | 1/0/0 | 0/1/0 | 0/0/0 | 1 |
| tag | 10 | 9 | 1/0/0 | 0/0/1 | 0/0/0 | 0 |

预期计划条目 **5**（自动 4 ＋ 冲突 1）；预期重演变更 **5** 行。

## b-dense-auto — 密集自动更新

用途：单侧高密度更新：计划条目多，但全部自动采纳、零冲突。

来源（祖本 → 分支，均为确定性生成）：
- 祖本：entry 1–2000（note 为 NULL）；每个 entry 一条 tag 't1'（共 4,000 行）。
- 左支 = 祖本 + ：改写全部偶数 id（1,000 行）的 qty 与 note；新增 entry 2001–2200 及其标签（400 行）。
- 右支 = 祖本 + ：改写 id%20=1 的 100 行 weight（与左支改动不重叠）。
- 评审裁决：无冲突，不裁决，直接重演。

差异表（独立于 pagefold 的 SQL 逐键对比；“增/改/删”相对祖本）：

| 表 | 祖本行 | 三份相同 | 仅左支变 | 仅右支变 | 双侧同值 | 双侧冲突 |
|---|--:|--:|--:|--:|--:|--:|
| entry | 2,000 | 900 | 200/1000/0 | 0/100/0 | 0/0/0 | 0 |
| tag | 2,000 | 2,000 | 200/0/0 | 0/0/0 | 0/0/0 | 0 |

预期计划条目 **1500**（自动 1500 ＋ 冲突 0）；预期重演变更 **1500** 行。

## c-bulk-identical — 大量相同行

用途：三份镜像 99.96% 行相同：验证大量相同键不会被误计为大量变更。

来源（祖本 → 分支，均为确定性生成）：
- 祖本：entry 1–8000（note 为 NULL）；每个 entry 一条 tag 't1'（共 16,000 行，接近 20,000 行上限）。
- 左支 = 祖本 + ：改写 entry 100、200 的 note；新增 entry 8001 及其标签。
- 右支 = 祖本 + ：改写 entry 200（与左支不同值）与 entry 300；删除 entry 7000 及其标签。
- 评审裁决：entry 200 的冲突选左支（note=左支冲突-200）。

差异表（独立于 pagefold 的 SQL 逐键对比；“增/改/删”相对祖本）：

| 表 | 祖本行 | 三份相同 | 仅左支变 | 仅右支变 | 双侧同值 | 双侧冲突 |
|---|--:|--:|--:|--:|--:|--:|
| entry | 8,000 | 7,996 | 1/1/0 | 0/1/1 | 0/0/0 | 1 |
| tag | 8,000 | 7,999 | 1/0/0 | 0/0/1 | 0/0/0 | 0 |

预期计划条目 **7**（自动 6 ＋ 冲突 1）；预期重演变更 **7** 行。

## d-noop-decisions — 空转裁决（有效成果刻意少于计划项）

用途：6 项冲突中 4 项刻意裁决为祖本：计划 10 条，实际变更只有 6 行。

来源（祖本 → 分支，均为确定性生成）：
- 祖本：entry 1–24（note=祖本-i）；每个 entry 一条 tag 't1'（共 48 行）。
- 左支 = 祖本 + ：改写 entry 1–6 的 note；新增 entry 100 及其标签。
- 右支 = 祖本 + ：改写 entry 1–6 的 qty（与左支冲突）；删除 entry 24 及其标签。
- 评审裁决：entry 1–4 选祖本（空转，不产生变更），entry 5 选左支，entry 6 选右支。

差异表（独立于 pagefold 的 SQL 逐键对比；“增/改/删”相对祖本）：

| 表 | 祖本行 | 三份相同 | 仅左支变 | 仅右支变 | 双侧同值 | 双侧冲突 |
|---|--:|--:|--:|--:|--:|--:|
| entry | 24 | 17 | 1/0/0 | 0/0/1 | 0/0/0 | 6 |
| tag | 24 | 23 | 1/0/0 | 0/0/1 | 0/0/0 | 0 |

预期计划条目 **10**（自动 4 ＋ 冲突 6）；预期重演变更 **6** 行。
