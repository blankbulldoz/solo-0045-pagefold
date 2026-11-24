# Pagefold 数据页合流

把三份有共同祖本的 SQLite 镜像合成一份经过约束校验的独立数据库。网页提供镜像检查、逐行裁决、候选重演、封存和下载。

## 运行

需要 Python 3.11+ 和带 `deserialize`、`serialize` 的 SQLite 3.37+ 构建，无第三方依赖。默认仅监听 `127.0.0.1:8745`，首次启动后访问同一地址。`--state` 指定服务状态数据库。适用于可信的单机操作环境。

```sh
python3 -m pagefold --state ./pagefold-state.sqlite
python3 -m examples.make_images ./demo-images
```

可分别上传 `quartz.sqlite`（祖本）、`cerulean.sqlite`（左支）和 `umber.sqlite`（右支），或在首页直接载入示例并创建计划。初始冲突出现在 `node` 的复合主键 `('north', 1)`，选择右支可成功重演，也可选择祖本或左支。额外示例 `stress-left.sqlite` 和 `stress-right.sqlite` 各自有效，合成后与 `bench` 的唯一约束冲突。离线裁决演练使用 `packet-base.sqlite`、`packet-left.sqlite` 和 `packet-right.sqlite`：四行双边不同、一行仅左支删除。

## 操作和接口

网页中的镜像链接可以检查表和主键行，计划详情显示自动项与未裁决项。保存裁决后重演当前版本，确认通过后封存，最后下载 SQLite 镜像和 JSON 凭证。

- `POST /api/images` 接收 `application/octet-stream`，`X-Image-Name` 是可选的 URL 编码标签。
- `GET /api/images/{id}` 查看镜像目录，追加 `/rows?table=名称&offset=0` 查阅 50 行。
- `POST /api/plans` 接收含 `base`、`left`、`right` 镜像摘要的 JSON。
- `GET /api/plans/{id}` 查看计划与版本，追加 `/attempts` 读取重演历史。
- `POST /api/plans/{id}/decisions` 接收 `revision`、`item` 和 `choice`。可选 `base`、`left`、`right`、`clear`。
- `POST /api/plans/{id}/rehearse`、`/seal`、`/archive` 接收 `revision`。
- `GET /api/plans/{id}/certificate` 下载封存凭证，`/download` 下载 SQLite 镜像。
- `POST /api/plans/{id}/packets` 接收 `revision`、`recipient` 和可选的 `items`（行摘要数组），生成离线裁决包；省略 `items` 表示全部待裁决行。
- `GET /api/packets/{id}` 查看分发包与包内轨迹，追加 `/export` 下载只含包内行的 JSON；`GET /api/plans/{id}/packets` 列出该计划的包。
- `POST /api/packets/{id}/return` 接收 `digest`、`selections`（行摘要 → 选择，可为单值或按顺序的步骤数组，如 `["clear","left"]`）和可选 `note`。
- `POST /api/packets/{id}/merge` 以当前计划的 `revision` 复核并合入，`/withdraw` 撤回未结清的包。
- `GET /api/plans/{id}/decisions` 读取按版序的裁决记录与来源（直接保存或分发包收件人）。

写入端点也接受网页表单的对应字段。分发包只携带被指定的冲突行及其三方值与发出时选择：包外行、自动采纳行与整库内容都不出现；`return` 拒绝包外行的选择。收件人交回后，`merge` 按当前最新版序逐行判断：与发出时选择一致的新值安全并入并推进一次版本，清空后又选回原值的行记为无需改动，分发后被他人改过选择的行留在「需本人再决定」而不覆盖，其他并发裁决保留。镜像为独立、自包含的文件，未外置日志，三份模式须精确匹配。JSON 行表示使用 `i`、`r`、`t`、`b` 分别标记整数、实数、文本、二进制，`n` 表示 NULL。

## 验证

```sh
python3 -m compileall -q pagefold examples
python3 -m examples.walkthrough all
```

工作流检查使用临时状态库和本机 HTTP 服务。正式自动化测试套件延期。
