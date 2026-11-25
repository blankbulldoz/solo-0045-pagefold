# Pagefold 数据页合流

把三份有共同祖本的 SQLite 镜像合成一份经过约束校验的独立数据库。网页提供镜像检查、逐行裁决、候选重演、封存和下载。

## 运行

需要 Python 3.11+ 和带 `deserialize`、`serialize` 的 SQLite 3.37+ 构建，无第三方依赖。默认仅监听 `127.0.0.1:8745`，首次启动后访问同一地址。`--state` 指定服务状态数据库。适用于可信的单机操作环境。

```sh
python3 -m pagefold --state ./pagefold-state.sqlite
python3 -m examples.make_images ./demo-images
```

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
python3 -m examples.concurrency_check            # 可加 --json 证据路径、--keep 保留状态库
```

工作流检查使用临时状态库和本机 HTTP 服务。正式自动化测试套件延期。

`concurrency_check` 不依赖重复撞调度，而是在真实 HTTP 入口与生产写事务
（`BEGIN IMMEDIATE` / `COMMIT`）之间插入确定性时序门，把封存、重新选择、
归档、重演的每个交错顺序固定下来：

- 六个三操作排列与两方相遇场景：终态只由 SQLite 实际提交次序决定，
  按计划状态机（`open->sealed`、`open->archived`、`sealed->archived`）
  给出互斥结果，并逐个核对 200/409 与 `stale_revision`、`closed_plan`。
- 候选已重演但选择随后改变：分别固定"重演先提交"和"选择先提交"两种
  次序，核对 `attempts` 证据、旧候选不可封存与新版本必须重新重演。
- HTTP 断开窗口：原始 socket 客户端在服务端 `COMMIT` 之后、响应发出
  之前断开，核对状态已 durable、服务端无异常日志，并验证重试语义
  （旧版本 `stale_revision`、封存/归档幂等）。
- 持久状态核对：每个场景后以只读连接直接检查状态库的 `plans`、
  `attempts`、`releases`；最后用独立子进程重启生产入口，重新下载
  SQLite 镜像并校验凭证摘要、完整性、外键和冲突行内容。
