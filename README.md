# Pagefold 数据页合流

把三份有共同祖本的 SQLite 镜像合成一份经过约束校验的独立数据库。网页提供镜像检查、逐行裁决、候选重演、封存和下载。

## 运行

需要 Python 3.11+ 和带 `deserialize`、`serialize` 的 SQLite 3.37+ 构建，无第三方依赖。默认仅监听 `127.0.0.1:8745`，首次启动后访问同一地址。`--state` 指定服务状态数据库。适用于可信的单机操作环境。

```sh
python3 -m pagefold --state ./pagefold-state.sqlite
python3 -m examples.make_images ./demo-images
```

状态库只保存元数据；镜像、重演候选和封存成果等不可变文件按原始字节的 SHA-256 存放在 `<状态库>.blobs/` 内容库中，同一内容跨来源、跨成果只保存一份，写入经临时文件原子换名。读取时按摘要重新校验，下载与凭证始终字节一致。

## 迁移

内嵌二进制的旧结构状态库（v1）用迁移命令升级为元数据加内容库的新结构（v2）。迁移可续执行：复制按批提交，中断只留下"文件已写、状态未换"的现场，重新运行自动接续；全部复制并预校验通过后才在单事务内切换，旧结构可用到最后一刻。

```sh
python3 -m pagefold.migrate run ./pagefold-state.sqlite     # 迁移并自动校验
python3 -m pagefold.migrate verify ./pagefold-state.sqlite  # 逐项核对历史凭证字节
```

服务启动时检测到旧结构会拒绝启动并提示迁移。迁移演练（`examples/migration_check`）覆盖中断恢复与新旧下载对照。

可分别上传 `quartz.sqlite`（祖本）、`cerulean.sqlite`（左支）和 `umber.sqlite`（右支），或在首页直接载入示例并创建计划。初始冲突出现在 `node` 的复合主键 `('north', 1)`，选择右支可成功重演，也可选择祖本或左支。额外示例 `stress-left.sqlite` 和 `stress-right.sqlite` 各自有效，合成后与 `bench` 的唯一约束冲突。

## 操作和接口

网页中的镜像链接可以检查表和主键行，计划详情显示自动项与未裁决项。保存裁决后重演当前版本，确认通过后封存，最后下载 SQLite 镜像和 JSON 凭证。

- `POST /api/images` 接收 `application/octet-stream`，`X-Image-Name` 是可选的 URL 编码标签。
- `GET /api/images/{id}` 查看镜像目录，追加 `/rows?table=名称&offset=0` 查阅 50 行。
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
python3 -m examples.migration_check
```

工作流检查使用临时状态库和本机 HTTP 服务。迁移演练用历史提交中的旧代码造出旧结构状态库，覆盖中断恢复、逐项凭证校验和新旧服务下载对照。正式自动化测试套件延期。
