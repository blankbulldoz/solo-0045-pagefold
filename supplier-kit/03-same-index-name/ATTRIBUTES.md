# 场景 3 属性说明：相同索引名称 ≠ 相同目录

## 本样件只暴露一个问题

三份镜像都有一个**同名索引 `stock_lookup`**，但左支把它建在了不同的列上。
每份镜像单独看都合规（显式主键、普通表、独立镜像），所以**三份都能上传成功**；
但 Pagefold 对三方建表与显式索引的完整 SQL 记录计算目录指纹，名称相同而
定义不同会导致指纹不一致，在**创建合流计划阶段**被拒绝，错误码
`schema_mismatch`。本场景不涉及任何上传期问题。

## 文件清单

| 角色 | 坏样件路径 | 修正副本路径 |
| --- | --- | --- |
| 祖本 base | `bad/inventory-base.sqlite` | `fixed/inventory-base.sqlite` |
| 左支 left | `bad/inventory-left.sqlite` | `fixed/inventory-left.sqlite` |
| 右支 right | `bad/inventory-right.sqlite` | `fixed/inventory-right.sqlite` |

坏样件 SHA-256：

| 角色 | SHA-256 | 索引 DDL | 索引列 |
| --- | --- | --- | --- |
| base | `e9d9ee5c16e0727cac3e674d837c9bb2cd6a36ff7a56caf6c6d8e0946df3331b` | `CREATE INDEX stock_lookup ON inventory(bin)` | `bin` |
| left | `4df1ff2f339aaf1ef294823299037a9ab51fa414b51b2c444cce6299f9edb40f` | `CREATE INDEX stock_lookup ON inventory(qty)` | `qty` |
| right | `e9d9ee5c16e0727cac3e674d837c9bb2cd6a36ff7a56caf6c6d8e0946df3331b` | `CREATE INDEX stock_lookup ON inventory(bin)` | `bin` |

## 关键属性：目录指纹（覆盖完整 SQL 记录，不只看名字）

| 镜像 | 目录指纹（前 12 位） |
| --- | --- |
| 坏 base / right（索引在 `bin`） | `deb8b62a1902…` |
| 坏 left（同名索引在 `qty`） | **`722d997c1177…`（不同）** |
| 修正后三份（索引统一在 `bin`） | 全部 `deb8b62a1902…`（一致） |

修正方式：把左支的 `stock_lookup` 改为与祖本、右支完全相同的定义
`ON inventory(bin)`，表和数据不动。

## 服务实测结果（真实 HTTP 入口，按接收方顺序）

1. 三份坏样件分别 `POST /api/images` → 均 **HTTP 201**（单份合规）；
2. 用三者 `POST /api/plans` → **HTTP 409**
   `{"code":"schema_mismatch","detail":"三份镜像的模式记录不一致。"}`；
3. 上传三份修正副本（内容一致，服务按字节去重复用同一镜像 ID）→ 均 201；
4. 创建计划 → **HTTP 201**，`unresolved: 0`、无差异项；
5. `POST /api/plans/{id}/rehearse`（revision 0）→ **`status: "passed"`，
   `changed: 0`**；封存后凭证的 `candidate_digest` 与下载文件 SHA-256 一致。

## 本地复查命令

```sh
# 同名索引，左支定义不同
sqlite3 bad/inventory-base.sqlite  "SELECT sql FROM sqlite_schema WHERE name='stock_lookup';"
sqlite3 bad/inventory-left.sqlite  "SELECT sql FROM sqlite_schema WHERE name='stock_lookup';"
sqlite3 bad/inventory-right.sqlite "SELECT sql FROM sqlite_schema WHERE name='stock_lookup';"
# 修正后三份定义一致
sqlite3 fixed/inventory-left.sqlite "SELECT sql FROM sqlite_schema WHERE name='stock_lookup';"
```
