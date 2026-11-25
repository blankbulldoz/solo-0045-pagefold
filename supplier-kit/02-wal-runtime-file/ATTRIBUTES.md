# 场景 2 属性说明：运行中的文件与独立镜像

## 本样件只暴露一个问题

坏样件是在 **WAL 日志模式下运行的数据库文件**，文件头第 18、19 字节为
`02 02`（WAL 的读/写版本标志）。Pagefold 只接收已完成检查点、自包含的独立
镜像（标志须为 `01 01`），因此在**镜像上传阶段**被拒绝，错误码 `image_journal`。
表结构、主键、数据本身都合规，没有任何其他问题。

注意：关闭连接后目录里没有残留 `-wal`/`-shm` 旁路文件，但**主文件头部的
WAL 标志仍然是 `02 02`，单凭主文件也会被拒绝**——这正是“运行中的文件”与
“独立镜像”的差别所在。

## 文件清单

| 角色 | 路径 | 大小（字节） | SHA-256 |
| --- | --- | --- | --- |
| 坏样件（保留复查，勿改） | `bad/sensor-reading-wal.sqlite` | 8192 | `ba07a50dadb151be2c6093ac976171de8a4fbc8fdd5cb1769e581d96c512977b` |
| 修正副本（可接收成功） | `fixed/sensor-reading-standalone.sqlite` | 8192 | `8d11310290728923e9a72b46d50d5260c7c001fe69a6e6205b2c22c3d88c9976` |

## 关键属性（文件头偏移 18–19）

| 属性 | 坏样件 | 修正副本 |
| --- | --- | --- |
| `PRAGMA journal_mode` | `wal` | `delete` |
| 头部第 18、19 字节 | **`02 02`** | **`01 01`** |
| 旁路文件 | 无（标志仍在主文件内） | 无 |

## 修正步骤（只在副本上执行，原件不动）

1. 复制坏样件为新文件，不直接改原件；
2. `PRAGMA wal_checkpoint(TRUNCATE);`（实测返回 `0,0,0`，无未落页）；
3. `PRAGMA journal_mode=DELETE;` 切回回滚日志模式，头部变为 `01 01`。

构建脚本记录的原件哈希在修正前后一致（`manifest.json` 中
`original_bad_unchanged: true`），原始坏样件可继续复查。

## 服务实测结果（真实 HTTP 入口）

- 上传坏样件：`POST /api/images` → **HTTP 422**
  `{"code":"image_journal","detail":"请先完成检查点并保存为独立的 SQLite 文件。"}`
- 上传修正副本：`POST /api/images` → **HTTP 201**，表 `sensor_reading` 2 行。

## 本地复查命令

```sh
# 直接读文件头：坏件 0202，修正件 0101
od -An -tx1 -j18 -N2 bad/sensor-reading-wal.sqlite
od -An -tx1 -j18 -N2 fixed/sensor-reading-standalone.sqlite
sqlite3 fixed/sensor-reading-standalone.sqlite "SELECT * FROM sensor_reading;"
```
