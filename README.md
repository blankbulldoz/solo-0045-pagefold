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

## 重演的并发语义

重演先在短读事务中读取计划、裁决和祖本，再在短写事务中登记“本版正在重演”，随后在**不持有任何状态库写事务**的情况下重建候选，最后用短写事务落库。

- 同一计划同一版的重复请求不会各算一遍：后到请求等待正在进行的那一次，返回同一份候选并带 `"shared": "rehearsal"`；等待超过预算时返回 `202` 与 `status: "running"`（页面显示“处理中”），稍后重试即可复用已落库结果；正常重复调用返回已存在记录并带 `"shared": "attempt"`。
- 重演读取输入后，如果计划版本推进或计划被封存/归档，落库结果标记为 `status: "stale"`，附带 `reason`（`stale_revision` 或 `closed_plan`）和其中已算出的结果，但**不保留候选字节**，因此不可封存、不可下载，并继续出现在 `/attempts` 历史中。过期版本的封存与下载被拒绝。
- 写入不再被重演阻塞：其他计划可在一次大镜像重演期间保存裁决、重演并封存；同一计划也可在重演期间保存新裁决，该新版本要另行重演。
- 重演进行中的封存返回 `rehearsal_running`；只有当前版本通过的记录可以封存，凭证与下载绑定同一候选摘要。

## 验证

```sh
python3 -m compileall -q pagefold examples
python3 -m examples.walkthrough all
python3 -m examples.rehearsal_timing       # 受控异步时序演示（HTTP）
python3 -m examples.check_rehearsal_timing # 并发/失效边界检查（进程内）
```

工作流检查使用临时状态库和本机 HTTP 服务。`rehearsal_timing` 用可释放的闸门固定重演中 `rebuild` 阶段的先后，演示同版重入共享一次重演、其他计划不被冻结、写后版本失效和读后归档失效，并打印可复核的时序 JSON（`--report` 可另存）。`check_rehearsal_timing` 覆盖等待超时、封存竞争、过期占位接管和“读取与占位之间被抢先”的路径。正式自动化测试套件延期。
