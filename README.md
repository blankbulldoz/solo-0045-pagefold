# Pagefold 数据页合流

把三份有共同祖本的 SQLite 镜像合成一份经过约束校验的独立数据库。网页提供镜像检查、逐行裁决、候选重演、封存和下载。

## 运行

需要 Python 3.11+ 和带 `deserialize`、`serialize` 的 SQLite 3.37+ 构建，无第三方依赖。默认仅监听 `127.0.0.1:8745`，首次启动后访问同一地址。`--state` 指定服务状态数据库。适用于可信的单机操作环境。

```sh
python3 -m pagefold --state ./pagefold-state.sqlite
python3 -m examples.make_images ./demo-images
```

### 采集本机在线 SQLite

手工准备独立文件之外，可由服务端配置采集允许的本机 SQLite 源：

```sh
python3 -m pagefold --state ./pagefold-state.sqlite \
  --sources-config ./examples/source-policy.example.json
# 或临时追加命名源：--allow-source wal=/var/lib/operations/operations.sqlite
```

配置只在服务端保存；请求只能提交配置中的源名称（或恰好位于 `roots` 内的文件名），不能提交任意路径。`GET /api/sources` 查看允许清单，`POST /api/sources/{name}/collect` 采集并登记，请求体可传 `{"label":"时点标签"}`；也兼容 `POST /api/sources/collect`、`POST /api/images/collect`，字段为 `source`、`name` 或 `path`（仍须命中白名单）。`GET /api/collections` 与 `/api/collections/{id}` 回查采集凭证。

采集器以只读 URI 和 `PRAGMA query_only=ON` 打开源库，通过 SQLite Online Backup API 分页复制到私有内存目标，再 `VACUUM` 成无外置 WAL、文件格式版本为 `1/1` 的独立镜像。目录检查、约束检查、行扫描和登记摘要都读取最终序列化字节，因此结构目录与数据行来自同一一致时点。SQLite 本身没有通用业务提交号；若要辨认“包含了哪些提交”，在线源应在同一事务内维护提交台账，例如 `pagefold_commit(seq INTEGER PRIMARY KEY, commit_id TEXT NOT NULL)`，并在服务端配置 `commit_sql`。返回的 `included_commits` 是镜像内的提交前缀，镜像 SHA-256 即精确时点摘要。

并发写入、分页读源中断、登记摘要和原库影响可用以下脚本复现：

```sh
python3 -m examples.collect_wal_demo --dir /tmp/pagefold-collect-demo
```

可分别上传 `quartz.sqlite`（祖本）、`cerulean.sqlite`（左支）和 `umber.sqlite`（右支），或在首页直接载入示例并创建计划。初始冲突出现在 `node` 的复合主键 `('north', 1)`，选择右支可成功重演，也可选择祖本或左支。额外示例 `stress-left.sqlite` 和 `stress-right.sqlite` 各自有效，合成后与 `bench` 的唯一约束冲突。

## 操作和接口

网页中的镜像链接可以检查表和主键行，计划详情显示自动项与未裁决项。保存裁决后重演当前版本，确认通过后封存，最后下载 SQLite 镜像和 JSON 凭证。

- `POST /api/images` 接收 `application/octet-stream`，`X-Image-Name` 是可选的 URL 编码标签。
- `GET /api/sources` 列出服务端允许的本机采集源；`POST /api/sources/{name}/collect` 从允许源采集一致时点并登记。
- `GET /api/collections`、`/api/collections/{id}` 回查采集源、包含提交和镜像摘要。
- `GET /api/images/{id}` 查看镜像目录，追加 `/rows?table=名称&offset=0` 查阅 50 行；`/download` 下载登记的独立 SQLite 字节。
- `POST /api/plans` 接收含 `base`、`left`、`right` 镜像摘要的 JSON。
- `GET /api/plans/{id}` 查看计划与版本，追加 `/attempts` 读取重演历史。
- `POST /api/plans/{id}/decisions` 接收 `revision`、`item` 和 `choice`。可选 `base`、`left`、`right`、`clear`。
- `POST /api/plans/{id}/rehearse`、`/seal`、`/archive` 接收 `revision`。
- `GET /api/plans/{id}/certificate` 下载封存凭证，`/download` 下载 SQLite 镜像。

写入端点也接受网页表单的对应字段。镜像为独立、自包含的文件，未外置日志，三份模式须精确匹配。JSON 行表示使用 `i`、`r`、`t`、`b` 分别标记整数、实数、文本、二进制，`n` 表示 NULL。

## 验证

```sh
python3 -m compileall -q pagefold examples
python3 -m examples.walkthrough all
python3 -m examples.collect_wal_demo
```

工作流检查使用临时状态库和本机 HTTP 服务；采集演示会另外启动真实 HTTP 入口并制造 WAL 并发写入和分页读源中断。正式自动化测试套件延期。
