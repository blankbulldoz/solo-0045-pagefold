# Pagefold 数据页合流

把三份有共同祖本的 SQLite 镜像合成一份经过约束校验的独立数据库。网页提供镜像检查、逐行裁决、候选重演、封存和下载。

## 运行

需要 Python 3.11+ 和带 `deserialize`、`serialize` 的 SQLite 3.37+ 构建，无第三方依赖。默认仅监听 `127.0.0.1:8745`，首次启动后访问同一地址。`--state` 指定服务状态数据库。适用于可信的单机操作环境。

```sh
python3 -m pagefold --state ./pagefold-state.sqlite
python3 -m examples.make_images ./demo-images
```

可分别上传 `quartz.sqlite`（祖本）、`cerulean.sqlite`（左支）和 `umber.sqlite`（右支），或在首页直接载入示例并创建计划。初始冲突出现在 `node` 的复合主键 `('north', 1)`：左支只改备注，右支只改二进制和权重。页面在整行裁决之外提供列合成——逐列采纳双方互不冲突的独立改动，并区分“可以组合”（系统建议尚未采纳）与“已经采纳 · 列合成”（合成行已固定为重演目标）。选择右支也可按整行裁决成功重演，也可选择祖本或左支。同列出现两个不同新值时须逐列选定来源；删除碰上修改只能整行裁决；独立列拼成的行若违反行级约束，重演会受阻并保留整行裁决退路。额外示例 `stress-left.sqlite` 和 `stress-right.sqlite` 各自有效，合成后与 `bench` 的唯一约束冲突。

## 操作和接口

网页中的镜像链接可以检查表和主键行，计划详情显示自动项与未裁决项。保存裁决后重演当前版本，确认通过后封存，最后下载 SQLite 镜像和 JSON 凭证。

- `POST /api/images` 接收 `application/octet-stream`，`X-Image-Name` 是可选的 URL 编码标签。
- `GET /api/images/{id}` 查看镜像目录，追加 `/rows?table=名称&offset=0` 查阅 50 行。
- `POST /api/plans` 接收含 `base`、`left`、`right` 镜像摘要的 JSON。
- `GET /api/plans/{id}` 查看计划与版本，追加 `/attempts` 读取重演历史。
- `POST /api/plans/{id}/decisions` 接收 `revision`、`item` 和 `choice`。可选 `base`、`left`、`right`、`clear`；`choice=combine` 时用 `columns`（JSON 对象，列名到 `base`/`left`/`right` 的映射）逐列合成，网页表单改用 `col.列名` 字段。同列冲突未显式选边返回 `column_conflict`，删除碰上修改返回 `row_missing`。
- `POST /api/plans/{id}/rehearse`、`/seal`、`/archive` 接收 `revision`。
- `GET /api/plans/{id}/certificate` 下载封存凭证，`/download` 下载 SQLite 镜像。

写入端点也接受网页表单的对应字段。镜像为独立、自包含的文件，未外置日志，三份模式须精确匹配。JSON 行表示使用 `i`、`r`、`t`、`b` 分别标记整数、实数、文本、二进制，`n` 表示 NULL。

## 验证

```sh
python3 -m compileall -q pagefold examples
python3 -m examples.walkthrough all
python3 -m examples.column_merge all
```

工作流检查使用临时状态库和本机 HTTP 服务。`column_merge` 分别用整行裁决和列合成演示同一复合主键冲突，并覆盖同列不同值、删除碰上修改、合成违反行级约束三条处理路径，逐场核对页面信息与下载数据库中的重演字段。正式自动化测试套件延期。
