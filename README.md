# Pagefold 数据页合流

把三份有共同祖本的 SQLite 镜像合成一份经过约束校验的独立数据库。网页提供镜像检查、逐行裁决、候选重演、封存和下载。

## 运行

需要 Python 3.11+ 和带 `deserialize`、`serialize` 的 SQLite 3.37+ 构建，无第三方依赖。默认仅监听 `127.0.0.1:8745`，首次启动后访问同一地址。`--state` 指定服务状态数据库。适用于可信的单机操作环境。

```sh
python3 -m pagefold --state ./pagefold-state.sqlite
python3 -m examples.make_images ./demo-images
```

可分别上传 `quartz.sqlite`（祖本）、`cerulean.sqlite`（左支）和 `umber.sqlite`（右支），或在首页直接载入示例并创建计划。初始冲突出现在 `node` 的复合主键 `('north', 1)`，选择右支可成功重演，也可选择祖本或左支。冲突条目还可以选择“人工合成一行…”，在祖本值基础上逐列改出裁决行。额外示例 `stress-left.sqlite` 和 `stress-right.sqlite` 各自有效，合成后与 `bench` 的唯一约束冲突。

## 操作和接口

网页中的镜像链接可以检查表和主键行，计划详情显示自动项与未裁决项。保存裁决后重演当前版本，确认通过后封存，最后下载 SQLite 镜像和 JSON 凭证。

- `POST /api/images` 接收 `application/octet-stream`，`X-Image-Name` 是可选的 URL 编码标签。
- `GET /api/images/{id}` 查看镜像目录，追加 `/rows?table=名称&offset=0` 查阅 50 行。
- `POST /api/plans` 接收含 `base`、`left`、`right` 镜像摘要的 JSON。
- `GET /api/plans/{id}` 查看计划与版本，追加 `/attempts` 读取重演历史。
- `POST /api/plans/{id}/decisions` 接收 `revision`、`item` 和 `choice`。`choice` 为 `base`、`left`、`right`、`manual` 或 `clear`；`manual` 时再带 `row`。
- `POST /api/plans/{id}/rehearse`、`/seal`、`/archive` 接收 `revision`。
- `GET /api/plans/{id}/certificate` 下载封存凭证，`/download` 下载 SQLite 镜像。

写入端点也接受网页表单的对应字段。镜像为独立、自包含的文件，未外置日志，三份模式须精确匹配。JSON 行表示使用 `i`、`r`、`t`、`b` 分别标记整数、实数、文本、二进制，`n` 表示 NULL。

### 人工合成裁决行

`choice:"manual"` 时，`row` 是与该表列顺序对齐的类型化单元格数组，例如把祖本行与新值合成：

```json
{"revision": 1, "item": "<64 位条目摘要>", "choice": "manual",
 "row": [["i","1"], ["t","alpha"], ["t","人工标题"], ["t",""], ["b","AAEC"], ["r","0x1.6p+1"]]}
```

- 主键只用于对齐：服务端会把 `row` 的主键列按 SQLite 数值身份（整型 `1` 与等值实数 `1.0` 同键）归一后与条目键比对，不一致返回 `422 manual_key`，不会落库或推进版本。
- `["n"]` 是 NULL，`["t",""]` 是空文本，`["b",""]` 是零字节 BLOB，三者分别保真；`i` 校验 64 位整数范围，`b` 须是标准 base64，`r` 接受十进制或 `float.hex()`。转换失败返回 `422 manual_value`，错误体带 `stage:"decision_input"` 与 `column`/`position`。
- 也接受列名映射（缺列回退祖本值）、`values`/`manual` 等行字段名，以及 `{"mode":"manual","row":…}` 形态；网页表单使用 `mt:<列序>` 与 `mv:<列序>` 字段。显式 `"row": null` 表示人工裁决删除对齐行。
- 输入转换通过后裁决才写入并推进版本；之后若重演撞上约束，`/rehearse` 返回 `status:"blocked"`，其 `problem.stage` 为 `"rehearse"` 并带 `table`/`key`。这区分了“哪一步没被接受”。
- 过期 `revision` 一律 `409 stale_revision`，错误体含 `current_revision`；网页草稿按“计划 + 条目 + 版本”暂存在浏览器，版本被另一窗口推进后草稿自动失效。
- `GET /api/plans/{id}` 与封存凭证都给出 `decision_digest`、原始 `decisions` 和展开表/键/行的 `choices`（每条带摘要），因此网页、JSON、失败响应和凭证可追溯到同一选择。

人工行的完整 HTTP 验证（网页表单、JSON、坏输入、改键、过期版本、约束受阻、NULL/空文本、凭证与下载库复核）：

```sh
python3 -m examples.walkthrough manual
```

## 验证

```sh
python3 -m compileall -q pagefold examples
python3 -m examples.walkthrough all
```

工作流检查使用临时状态库和本机 HTTP 服务。正式自动化测试套件延期。
