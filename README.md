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

网页中的镜像链接可以检查表和主键行，计划详情显示自动项与未裁决项。建立计划前先进入**可确认预览**（首页表单 `POST /plans/preview` 后重定向到可在多个窗口打开的 `/plans/preview/{token}` 固定链接）：核对祖本/左支/右支对应的标签与字节摘要、新增/删除/修改及自动/冲突等变更类型、差异项与待裁决数等预估处理量；超过直接审阅规模（默认差异 > 200 项或冲突 > 50 项）时会提示仍要创建、返回调整角色或缩小范围重做。确认后才写入正式计划，中途退出不留计划记录。保存裁决后重演当前版本，确认通过后封存，最后下载 SQLite 镜像和 JSON 凭证。

- `POST /api/images` 接收 `application/octet-stream`，`X-Image-Name` 是可选的 URL 编码标签。
- `GET /api/images/{id}` 查看镜像目录，追加 `/rows?table=名称&offset=0` 查阅 50 行。
- `POST /api/plans/preview` 接收含 `base`、`left`、`right` 镜像摘要的 JSON 或表单，返回绑定这三个来源的一次性预览凭证 `preview_token`、角色卡、变更类型计数、预估处理量、规模判定与建议动作；此调用不写 `plans` 表。
- `GET /api/plans/preview/{token}` 回读预览及其确认状态；`POST /api/plans/preview/{token}/confirm` 可用路径凭证确认。
- `POST /api/plans` 接收含 `base`、`left`、`right` 镜像摘要的 JSON。附带 `preview_token`（也接受 `token`/`preview` 字段名）时按服务端预览绑定创建：提交来源若与预览摘要不符返回 `409 preview_binding`，同一凭证重复确认（含两个窗口并发）幂等返回同一个正式计划；不带凭证时保留原有直接创建行为。
- `GET /api/plans/{id}` 查看计划与版本，追加 `/attempts` 读取重演历史。
- `POST /api/plans/{id}/decisions` 接收 `revision`、`item` 和 `choice`。可选 `base`、`left`、`right`、`clear`。
- `POST /api/plans/{id}/rehearse`、`/seal`、`/archive` 接收 `revision`。
- `GET /api/plans/{id}/certificate` 下载封存凭证，`/download` 下载 SQLite 镜像。

写入端点也接受网页表单的对应字段。镜像为独立、自包含的文件，未外置日志，三份模式须精确匹配。JSON 行表示使用 `i`、`r`、`t`、`b` 分别标记整数、实数、文本、二进制，`n` 表示 NULL。

## 验证

```sh
python3 -m compileall -q pagefold examples
python3 -m examples.walkthrough all
python3 -m examples.preview_flow
```

`examples.preview_flow` 用临时状态库和本机 HTTP 服务，构造无冲突、普通冲突和大变更量三组实例，逐项对照预览与实际落库计划，并验证同标签异字节互换被拦、两窗口并发确认同一预览只产生一个计划、只预览不确认时 `plans` 表无记录。

工作流检查使用临时状态库和本机 HTTP 服务。正式自动化测试套件延期。
