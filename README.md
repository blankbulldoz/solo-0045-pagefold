# Pagefold 数据页合流

把三份有共同祖本的 SQLite 镜像合成一份经过约束校验的独立数据库。网页提供镜像检查、逐行裁决、候选重演、封存和下载。

## 运行

需要 Python 3.11+ 和带 `deserialize`、`serialize` 的 SQLite 3.37+ 构建，无第三方依赖。默认仅监听 `127.0.0.1:8745`，首次启动后访问同一地址。`--state` 指定服务状态数据库。适用于可信的单机操作环境。

```sh
python3 -m pagefold --state ./pagefold-state.sqlite
python3 -m examples.make_images ./demo-images
python3 -m examples.make_images ./demo-images --with-cases
```

可分别上传 `quartz.sqlite`（祖本）、`cerulean.sqlite`（左支）和 `umber.sqlite`（右支），或在首页直接载入示例并创建计划。初始冲突出现在 `node` 的复合主键 `('north', 1)`，选择右支可成功重演，也可选择祖本或左支。额外示例 `stress-left.sqlite` 和 `stress-right.sqlite` 各自有效，合成后与 `bench` 的唯一约束冲突。

结构不一致的建计划请求仍按精确匹配规则返回 `409 schema_mismatch`，但响应和网页不再只给模式摘要：报告会逐项指出哪份镜像的哪类结构差异挡住计划，区分缺表、列缺失/列定义/列顺序、复合主键顺序、显式索引缺失或索引定义不同，并给出各库对应的列定义、主键顺序和索引定义证据，可跳转到对应镜像目录。`schema-cases` 目录提供两组三方镜像：`common-*` 展示两个分支各自多出不同单独索引的共同差异类别，`branch-*` 展示左支仅交换复合主键顺序、右支仅新增一个单独索引的分支差异，二者都不会被笼统报成缺表。

## 操作和接口

网页中的镜像链接可以检查表和主键行，计划详情显示自动项与未裁决项。保存裁决后重演当前版本，确认通过后封存，最后下载 SQLite 镜像和 JSON 凭证。

- `POST /api/images` 接收 `application/octet-stream`，`X-Image-Name` 是可选的 URL 编码标签。
- `GET /api/images/{id}` 查看镜像目录，追加 `/rows?table=名称&offset=0` 查阅 50 行。目录含每张表的列定义（名称、类型、非空、默认值）、主键顺序、显式索引（唯一性、列顺序、定义语句）与约束自动索引。
- `POST /api/plans` 接收含 `base`、`left`、`right` 镜像摘要的 JSON。模式不一致时返回 `409 schema_mismatch`，`mismatch` 字段携带分类差异报告（`findings` 含差异类型、涉及镜像与三方列/键/索引证据），`image_links` 给出三份镜像摘要。
- `GET /api/plans/{id}` 查看计划与版本，追加 `/attempts` 读取重演历史。
- `POST /api/plans/{id}/decisions` 接收 `revision`、`item` 和 `choice`。可选 `base`、`left`、`right`、`clear`。
- `POST /api/plans/{id}/rehearse`、`/seal`、`/archive` 接收 `revision`。
- `GET /api/plans/{id}/certificate` 下载封存凭证，`/download` 下载 SQLite 镜像。

写入端点也接受网页表单的对应字段。镜像为独立、自包含的文件，未外置日志，三份模式须精确匹配。JSON 行表示使用 `i`、`r`、`t`、`b` 分别标记整数、实数、文本、二进制，`n` 表示 NULL。

## 验证

```sh
python3 -m compileall -q pagefold examples
python3 -m examples.walkthrough all
```

工作流检查使用临时状态库和本机 HTTP 服务。正式自动化测试套件延期。
