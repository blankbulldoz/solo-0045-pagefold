# 按计划隔离恢复操作手册（恢复演练）

适用场景：生产状态库（`pagefold-state.sqlite`）中有多个团队的合流计划。接到恢复演练
请求时，只把**获准的一份计划**放到另一端口的新实例；生产实例继续在原端口处理封存等
工作，不中断、不被改动。

本目录（`recovery/`）提供三个无第三方依赖的工具，只用 Python 3.11 标准库：

| 文件 | 职责 |
| --- | --- |
| `recovery/recover.py` | 只读打开生产库，在单个读事务内把获准计划的闭包装配到独立演练库 |
| `recovery/verify.py` | 不依赖 pagefold 包的独立核验器（SQLite + 清单 + 可选 HTTP 交叉核对） |
| `recovery/drill.py` | 端到端演练脚本，覆盖并发写入、中断重跑、双端口网页核对 |

## 1. 隔离边界与时点保证

- **只读生产库**：以 `file:...?mode=ro` 打开并设 `PRAGMA query_only=ON`，恢复过程对生产
  零写入；生产服务继续在原端口运行。
- **同一已提交时点**：恢复全程持有一个延迟读事务（WAL 快照隔离）。事务开始后生产侧的
  并发提交不会进入演练库。清单的 `snapshot_marker_at_begin/end` 与 `stable_throughout`
  字段留痕，核验器会断言事务视角未漂移。
- **按计划闭包复制**：只复制获准计划 `base/left/right` 引用到的镜像（按 SHA-256 去重，
  共享祖本只带一份）、该计划的 `attempts` 重演历史与 `releases` 封存成果。其他团队独有
  的镜像、计划、候选、凭证一律不复制，清单的 `excluded` 字段逐项列出。
- **归档与受阻都支持**：已 `archived`（含已封存可下载）和仅 `open` 但重演 `blocked`
  的计划都可恢复；受阻记录保留诊断且 `candidate` 为 NULL，演练实例同样不提供下载。
- **原子交付**：先写 `输出名.part` 暂存库，单事务装配并通过 `integrity_check` 与
  `foreign_key_check` 后 `os.replace` 原子改名。中断只留下 `.part`，不会出现半成品占据
  正式路径；重跑会先清除暂存件。
- **自包含单文件**：交付前切回 rollback journal 并检查点，演练库没有 `-wal/-shm` 附属件。

## 2. 标准操作步骤

```sh
# 0) 先看清生产库里有哪些计划（只读，不产生文件）
python3 -m recovery.recover --source ./pagefold-state.sqlite --list

# 1) 按获准的计划 ID 恢复到演练库（生产服务可同时在 8745 运行）
python3 -m recovery.recover \
  --source ./pagefold-state.sqlite \
  --output ./drill/drill-<plan>.sqlite \
  --plan <计划ID>

# 2) SQLite 独立核验（强烈建议同时给 --source 做隔离边界交叉核对）
python3 -m recovery.verify ./drill/drill-<plan>.sqlite \
  --source ./pagefold-state.sqlite \
  --manifest ./drill/drill-<plan>.manifest.json

# 3) 在另一端口启动演练实例（不要与生产端口相同）
python3 -m pagefold --state ./drill/drill-<plan>.sqlite --port 8791

# 4) 网页/API 核对（核验器也可直接打 HTTP）
python3 -m recovery.verify ./drill/drill-<plan>.sqlite \
  --source ./pagefold-state.sqlite \
  --manifest ./drill/drill-<plan>.manifest.json \
  --http-base http://127.0.0.1:8791
```

关键网页检查：

- `GET /api/plans` 只返回获准的一份计划；直接访问他方计划 ID 返回 `404 unknown_plan`。
- `GET /api/images` 数量等于计划三方引用去重后的镜像数；他方独有镜像不出现。
- 已封存计划可经 `/api/plans/{id}/certificate` 与 `/download` 取得凭证和候选，下载字节的
  SHA-256 与凭证 `candidate_digest` 一致；受阻计划的下载请求返回 `404 unpublished`。

关键 SQLite 检查（核验器已自动执行，也可手工复核）：

```sh
sqlite3 ./drill/drill-<plan>.sqlite "SELECT id,state,revision FROM plans;"
sqlite3 ./drill/drill-<plan>.sqlite "SELECT id FROM images;"
sqlite3 ./drill/drill-<plan>.sqlite "PRAGMA integrity_check;"
sqlite3 ./drill/drill-<plan>.sqlite "PRAGMA foreign_key_check;"
```

## 3. 中断与重跑

- 恢复中断（进程被杀死、机器掉电）后，正式输出路径要么不存在、要么是上一次完整交付的
  库；同目录只会留下 `*.part` 暂存件。
- 暂存件不是可交接实例：它可能只有空表结构。`recovery.verify` 对暂存件会因
  “演练库恰好一份计划”等检查非零退出。
- 用**原命令重跑**即可：工具先删除暂存件，再重新从生产库的最新已提交时点装配并原子改名。
- `--crash-point images|plan` 仅供演练注入中断（在装镜像后/写计划前硬退出），生产操作
  不要使用。

## 4. 清单（manifest）字段说明

`*.manifest.json` 与演练库成对交付，格式标识 `pagefold-recovery-manifest-v1`：

- `source.snapshot_marker_at_begin/end`：读事务起止时的行数与最大 rowid；
  `stable_throughout=true` 表示整个恢复看到的是同一时点。
- `selected_plan`：计划 ID、版本、状态、三来源角色 ID 及计划行规范化哈希。
- `included_images`：进入演练库的每份镜像 ID、标签与字节 SHA-256。
- `attempt_revisions` / `release_present`：携带的重演历史版本与封存情况。
- `excluded.plan_ids / image_ids`：明确被排除的他方计划与镜像，便于审计隔离边界。
- `drill_database.file_sha256`：交付瞬间的文件字节哈希；`logical_sha256` 是四表内容的
  规范化哈希。演练实例被 pagefold 服务打开后可能切到 WAL、文件字节变化，但逻辑哈希不变，
  核验以逻辑哈希为准。

## 5. 一键端到端演练

```sh
python3 -m recovery.drill
```

脚本在临时产物目录 `./drill-artifacts/` 中：

1. 建立两个共享祖本的计划：计划 A（裁决→重演→封存→归档）、计划 B（重演受阻）。
2. 生产实例在 `127.0.0.1:8745` 全程运行；恢复期间并发线程持续登记新镜像。
3. 恢复计划 A 并断言事务进行中的并发提交零混入、团队 B 独有镜像零泄漏。
4. 注入中断证明 `.part` 不可交接，再原命令重跑并通过全部核验。
5. 分别在 `8791`（计划 A）、`8792`（计划 B）启动演练实例做真实 HTTP 核对。
6. 恢复后继续向生产库写入，证明演练库逻辑哈希保持冻结、生产服务不受影响。
