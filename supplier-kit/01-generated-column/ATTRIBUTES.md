# 场景 1 属性说明：普通表与生成列

## 本样件只暴露一个问题

`part_ledger` 表含有 **VIRTUAL 生成列** `total`。Pagefold 要求所有列可写入
（重演时需要按计划整行删除再插入），生成列由表达式派生、不可写入，因此在
**镜像上传阶段**即被拒绝，错误码 `table_shape`。除此之外，该文件是页对齐、
已检查点的独立 SQLite 镜像，没有任何其他问题。

## 文件清单

| 角色 | 路径 | 大小（字节） | SHA-256 |
| --- | --- | --- | --- |
| 坏样件（保留复查，勿改） | `bad/part-ledger-generated.sqlite` | 12288 | `6bb6f5d742d3f00e0f9f69f39d0e5b08ff7e71e51d9039a7a7693cbe11fa23c0` |
| 修正副本（可接收成功） | `fixed/part-ledger-plain.sqlite` | 12288 | `3567be4b9edf45e4edbfa100e45016f6223ac0f4cbe1d3b5d88c95660ec8f321` |

## 关键属性（普通列 vs 生成列）

`PRAGMA table_xinfo(part_ledger)` 最后一列 hidden 标志：

| 列 | 坏样件 | 修正副本 | 含义 |
| --- | --- | --- | --- |
| `sku` | 0 | 0 | 普通列，主键 |
| `qty` | 0 | 0 | 普通列 |
| `total` | **2（VIRTUAL 生成列）** | **0（普通列）** | 非 0 即不可写入 |

- 坏样件建表语句：`total INTEGER GENERATED ALWAYS AS (qty * 2) VIRTUAL`
- 修正方式：把 `total` 改为普通可写列 `total INTEGER NOT NULL`，按同一表达式
  `qty * 2` 预计算后落库（`SKU-1: 14`、`SKU-2: 6`），数据语义不变。

## 服务实测结果（真实 HTTP 入口）

- 上传坏样件：`POST /api/images` → **HTTP 422**
  `{"code":"table_shape","detail":"表 part_ledger 需要显式主键，并且所有列须可写入。"}`
- 上传修正副本：`POST /api/images` → **HTTP 201**，返回镜像目录，表 `part_ledger` 2 行。

## 本地复查命令

```sh
# hidden=2 即生成列；修正副本三列均为 0
sqlite3 bad/part-ledger-generated.sqlite "PRAGMA table_xinfo(part_ledger);"
sqlite3 fixed/part-ledger-plain.sqlite "PRAGMA table_xinfo(part_ledger);"
sqlite3 fixed/part-ledger-plain.sqlite "SELECT * FROM part_ledger;"
```
