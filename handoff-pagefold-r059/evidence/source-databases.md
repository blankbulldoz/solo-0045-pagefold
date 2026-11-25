# 三份来源全表证据

# base · 共同祖本

## `bench` (2 行)

主键列：`code`

| code | label |
|---|---|
| north | 玄石 |
| south | 砂层 |

## `binding` (1 行)

主键列：`id`

| id | bench | slot |
|---|---|---|
| 1 | north | 1 |

## `node` (2 行)

主键列：`bench`, `slot`

| bench | slot | note | payload | weight |
|---|---|---|---|---|
| north | 1 | 祖本节点 | BLOB[2] hex=`0014` b64=`ABQ=` | 1.5 |
| south | 1 | 稳定节点 | BLOB[0] hex=`` b64=`` | 3.0 |

# left · 左支：蓝釉分支，含新增行

## `bench` (2 行)

主键列：`code`

| code | label |
|---|---|
| north | 玄石 |
| south | 砂层 |

## `binding` (1 行)

主键列：`id`

| id | bench | slot |
|---|---|---|
| 1 | north | 1 |

## `node` (3 行)

主键列：`bench`, `slot`

| bench | slot | note | payload | weight |
|---|---|---|---|---|
| north | 1 | 左支覆写 | BLOB[2] hex=`0014` b64=`ABQ=` | 1.5 |
| south | 1 | 稳定节点 | BLOB[0] hex=`` b64=`` | 3.0 |
| south | 2 | 左支新增 | `NULL` | 4.0 |

# right · 右支：赭石分支，含 BLOB/重量与更名

## `bench` (2 行)

主键列：`code`

| code | label |
|---|---|
| north | 玄石 |
| south | 右支更名 |

## `binding` (1 行)

主键列：`id`

| id | bench | slot |
|---|---|---|
| 1 | north | 1 |

## `node` (2 行)

主键列：`bench`, `slot`

| bench | slot | note | payload | weight |
|---|---|---|---|---|
| north | 1 | 祖本节点 | BLOB[2] hex=`ff00` b64=`/wA=` | 2.25 |
| south | 1 | 稳定节点 | BLOB[0] hex=`` b64=`` | 3.0 |
