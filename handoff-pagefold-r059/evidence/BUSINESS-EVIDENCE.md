# 业务证据与合流裁决说明

本文件只解释仓库示例 `quartz / cerulean / umber` 的实际合流结果。所有值均来自本目录中的三份来源库与已封存结果库；可用 `../tools/verify_offline.py` 离线复核，不需要启动 Pagefold 服务。

## 1. 一句话结论

- **左支 `cerulean` 增加了**复合键为 `node.('south', 2)` 的新业务行：备注 `左支新增`、`payload = NULL`、`weight = 4.0`。
- 唯一冲突是 **`node.('north', 1)`**：左支改了 `note`，右支改了同一整行的 `payload` 和 `weight`。
- 本次实际裁决为 **选择右支**。因此该冲突键采用右支整行：`payload = x'ff00'`、`weight = 2.25`；`note` 随右支保持祖本值 `祖本节点`，左支的 `左支覆写` **不会**进入结果。
- 左支单边新增的 `('south', 2)` 按三方规则自动采纳，仍然存在。封存结果的 `node` 有 **3 行**，右支原库只有 **2 行**，所以这不是“右支整库覆盖左支”。

## 2. 逐行业务对照

BLOB 用十六进制表示；`x''` 是长度为 0 的 BLOB，和 `NULL` 不同。

| 业务键 | 祖本 `quartz` | 左支 `cerulean` | 右支 `umber` | 封存结果 | 判定 |
|---|---|---|---|---|---|
| `bench('north')` | `玄石` | `玄石` | `玄石` | `玄石` | 三方相同，省略为变更项 |
| `bench('south')` | `砂层` | `砂层` | `右支更名` | `右支更名` | 仅右支修改，自动取右支 |
| `node('north',1)` | note=`祖本节点`; payload=`0014`; weight=`1.5` | note=`左支覆写`; payload=`0014`; weight=`1.5` | note=`祖本节点`; payload=`ff00`; weight=`2.25` | note=`祖本节点`; payload=`ff00`; weight=`2.25` | 双边都改了同一行，形成冲突；人工裁决 `right`，按整行取右支 |
| `node('south',1)` | `稳定节点`; payload=`x''`; weight=`3.0` | 同祖本 | 同祖本 | 同祖本 | 三方相同 |
| `node('south',2)` | 不存在 | `左支新增`; payload=`NULL`; weight=`4.0` | 不存在 | `左支新增`; payload=`NULL`; weight=`4.0` | 仅左支新增，自动取左支；证明左支增量已合入 |
| `binding(1)` | `('north',1)` | 同 | 同 | `('north',1)` | 三方相同；外键检查通过 |

机器可读的逐项断言在 `reconciliation.json`，其中以下字段必须为 `true`：

```json
{
  "left_added_key_present_in_sealed_result": true,
  "sealed_conflict_row_equals_right_full_row": true,
  "sealed_conflict_note_is_base_value_not_left_overwrite": true,
  "sealed_conflict_blob_is_right_ff00": true,
  "not_whole_right_database": true
}
```

## 3. 为什么不是右支整库覆盖

如果简单用右支文件覆盖，结果应当：

- `node` 只有右支的两个键：`('north',1)`、`('south',1)`；
- 不会有左支新增的 `('south',2)`。

实际封存库的键集合是：

```text
('north', 1)
('south', 1)
('south', 2)   ← 左支单边新增，仍在
```

`evidence/sealed-database.md`、`evidence/csv/node.csv` 和 `sealed-database.json` 都是从封存 SQLite 实际导出的副本。校验工具还会重新打开数据库查询，而不是只信任这些导出文件。

## 4. 复合键与 BLOB 直接查询

推荐：

```sh
python3 tools/query_row.py north 1
python3 tools/query_row.py north 1 --json
```

预期核心值：

```text
复合键：table=node, (bench='north', slot=1)
note   = '祖本节点'
payload= b'\xff\x00'  # hex=ff00, base64=/wA=, length=2
weight = 2.25
```

也可用 Python 标准库直接查：

```py
import sqlite3
db = sqlite3.connect("file:result/merged-r059-right-choice.sqlite?mode=ro", uri=True)
print(db.execute(
    "SELECT bench, slot, note, hex(payload), weight "
    "FROM node WHERE bench=? AND slot=?",
    ("north", 1),
).fetchone())
# ('north', 1, '祖本节点', 'ff00', 2.25)
```

如果系统装有 `sqlite3` CLI：

```sh
sqlite3 result/merged-r059-right-choice.sqlite \
  "SELECT bench,slot,note,hex(payload),weight FROM node WHERE bench='north' AND slot=1;"
```

## 5. 同模式另一份文件如何被识别

`negative-tests/stress-left-same-schema.sqlite` 是仓库示例生成的“同模式另一份库”。它的模式摘要与本次凭证相同：

```text
schema = 2850b20c7d73163e0e8f153ae064af159f3389cc6d3ed1a5c909218ba29fb6ac
```

但它的字节 SHA-256 不是左支 `cerulean`：

```text
stress-left = 6b7331fe95f7c5b796f16849218703f3b3030c9c3e4dba9d0b23707c21391662
left        = ab1bceb83ee5599db0a06e4429977951f26378db082cbffa052f8215a14d6f75
```

并且它多了 `bench('stress-left')` 行业务数据。因此：

- **只看建表/索引模式会误认**；
- Pagefold 的镜像 ID 和凭证来源角色绑定的是**原始字节 SHA-256**；
- 本交接包的校验器会重算来源哈希、凭证摘要和清单哈希；
- 对一个混入外来 SQLite 的目录，会标记为 `foreign_same_schema`；
- 如果外来文件顶替了 `left-cerulean.sqlite` 的文件名，则会同时报告缺少受信任的 `left` 角色。

已捕获的实测判别输出见 `../negative-tests/mixup-selftest.txt`；整包被加入外来文件或左支文件被顶替时的非零退出记录见 `../negative-tests/package-mixup-detection.txt`。
