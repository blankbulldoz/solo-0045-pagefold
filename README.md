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
- `POST /api/bundles` 接收 `members`（2–8 项 `plan`+`revision`，计划不可重复）。每份成员须是计划当前版本且有通过重演的合格候选，任一成员版本漂移或缺候选则整组拒绝、不落库；同一套选择重复提交返回同一交付（首次 201，之后 200）。
- `GET /api/bundles` 列出交付；`GET /api/bundles/{id}` 获取交付清单；`GET /api/bundles/{id}/members/{plan}` 下载该成员的 SQLite 镜像。

联合交付面向须同时发布的多份独立 SQLite：交付身份即成员清单摘要，成员按计划与版本号明确选择——两份计划的同一版号不是同一身份。清单记录每份文件的来源、模式、裁决与候选摘要（与封存凭证同构，已封存计划可逐项核对）及组合成员关系，重复领取可逐字节核对为同一套。

写入端点也接受网页表单的对应字段。镜像为独立、自包含的文件，未外置日志，三份模式须精确匹配。JSON 行表示使用 `i`、`r`、`t`、`b` 分别标记整数、实数、文本、二进制，`n` 表示 NULL。

## 验证

```sh
python3 -m compileall -q pagefold examples
python3 -m examples.walkthrough all
python3 -m examples.joint_release all
```

工作流检查使用临时状态库和本机 HTTP 服务。联合交付演练覆盖版本漂移整组失败、成功发布与重复领取核对。正式自动化测试套件延期。
