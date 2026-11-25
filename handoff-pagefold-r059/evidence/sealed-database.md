# 封存结果全表证据

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

## `node` (3 行)

主键列：`bench`, `slot`

| bench | slot | note | payload | weight |
|---|---|---|---|---|
| north | 1 | 祖本节点 | BLOB[2] hex=`ff00` b64=`/wA=` | 2.25 |
| south | 1 | 稳定节点 | BLOB[0] hex=`` b64=`` | 3.0 |
| south | 2 | 左支新增 | `NULL` | 4.0 |
