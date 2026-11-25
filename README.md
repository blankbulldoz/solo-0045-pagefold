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

归档时可以只停止计划，也可以在同一事务内建立承接计划：当一份计划因错误祖本需要停止时，给出更正后的祖本（及必要时更正的分支），系统按主键逐条核对旧的手工选择——来源行未变的选择直接保留（`retained`），来源行已变的留待重审（`needs_review`），已能自动采纳的标记为 `automatic`，新三方下不再有差异的条目标记为 `dropped`。承接报告随承接关系保存，归档前后两个计划页面互相链接。整个过程要么全部完成、要么整体回滚，已封存的 SQLite 与凭证在归档后仍可原样下载。

- `POST /api/images` 接收 `application/octet-stream`，`X-Image-Name` 是可选的 URL 编码标签。
- `GET /api/images/{id}` 查看镜像目录，追加 `/rows?table=名称&offset=0` 查阅 50 行。
- `POST /api/plans` 接收含 `base`、`left`、`right` 镜像摘要的 JSON。
- `GET /api/plans/{id}` 查看计划与版本，追加 `/attempts` 读取重演历史。
- `POST /api/plans/{id}/decisions` 接收 `revision`、`item` 和 `choice`。可选 `base`、`left`、`right`、`clear`。
- `POST /api/plans/{id}/rehearse`、`/seal`、`/archive` 接收 `revision`。
- `POST /api/plans/{id}/archive` 可额外携带任意 `base`、`left`、`right` 镜像摘要：留空的角色沿用当前镜像。给出至少一个角色时，归档与「建立承接计划」在同一事务内完成。
- `GET /api/plans/{id}/carryover` 读取承接关系与逐条核对报告（无承接关系时返回 404）。
- `GET /api/plans/{id}/certificate` 下载封存凭证，`/download` 下载 SQLite 镜像。

写入端点也接受网页表单的对应字段。镜像为独立、自包含的文件，未外置日志，三份模式须精确匹配。JSON 行表示使用 `i`、`r`、`t`、`b` 分别标记整数、实数、文本、二进制，`n` 表示 NULL。

## 验证

```sh
python3 -m compileall -q pagefold examples
python3 -m examples.walkthrough all
python3 -m examples.succession_drill
```

工作流检查使用临时状态库和本机 HTTP 服务。`walkthrough` 覆盖镜像登记到归档的既有流程；`succession_drill` 覆盖可承接与需重审两个计划、四种承接去向、归档/承接中途失败的整体回滚，以及归档后封存成果仍可提取。正式自动化测试套件延期。
