# Pagefold 数据页合流

把三份有共同祖本的 SQLite 镜像合成一份经过约束校验的独立数据库。网页提供镜像检查、逐行裁决、候选重演、封存和下载。

## 运行

需要 Python 3.11+ 和带 `deserialize`、`serialize` 的 SQLite 3.37+ 构建，无第三方依赖。默认仅监听 `127.0.0.1:8745`，首次启动后访问同一地址。`--state` 指定服务状态数据库。适用于可信的单机操作环境。

```sh
python3 -m pagefold --state ./pagefold-state.sqlite
python3 -m examples.make_images ./demo-images
```

可分别上传 `quartz.sqlite`（祖本）、`cerulean.sqlite`（左支）和 `umber.sqlite`（右支），或在首页直接载入示例并创建计划。初始冲突出现在 `node` 的复合主键 `('north', 1)`，选择右支可成功重演，也可选择祖本或左支。额外示例 `stress-left.sqlite` 和 `stress-right.sqlite` 各自有效，全表合流时与 `bench`/`alias` 的唯一约束冲突。

## 关系集合合流（范围）

创建计划时可通过 `scope` 传入表名子集（首页表单用逗号分隔）。只有范围内表的三方差异作为主动变更进入重演，其余表默认逐行保持祖本，最终仍是一份可独立打开的完整 SQLite。范围分析区分三类条目：

- **主动变更（active）**：范围内表的行差异，沿用自动采纳或逐行裁决。
- **派生变更（derived）**：范围内删除/改键使外键父行消失时，范围外子表（含复合外键全列匹配）必须跟随的删除，须在重演前显式确认，确认随版本裁剪。
- **范围阻塞（blocked）**：当前集合无法闭环的约束，附 `remedy`：`expand` 给出需要纳入的上游父表（可确认的扩展范围），`narrow` 给出必须移出的表（如唯一约束与祖本未改行相撞、跨支同名标签等只能缩小交付范围的情形）。

`cedar-left.sqlite` / `cedar-right.sqlite` 演示复合外键派生删除与上游 `expand`：以 `scope=["node"]` 创建时，`binding` 的复合外键产生派生删除，而 `node('gale',1)` 缺少 `bench` 父行，需把 `bench` 扩入范围并确认派生项后才能重演。`stress-*` 配合 `scope=["alias"]` 演示必须 `narrow` 的硬冲突；收窄到 `bench` 后可交付。封存凭证绑定 `scope`、`amendments` 及其摘要，重演结果额外报告 `untouched_tables`，并在重演中逐表断言未参与表与祖本完全一致。

## 操作和接口

网页中的镜像链接可以检查表和主键行，计划详情显示自动项与未裁决项、范围面板（主动/派生/阻塞/未参与表）。保存裁决或调整范围后重演当前版本，确认通过后封存，最后下载 SQLite 镜像和 JSON 凭证。

- `POST /api/images` 接收 `application/octet-stream`，`X-Image-Name` 是可选的 URL 编码标签。
- `GET /api/images/{id}` 查看镜像目录，追加 `/rows?table=名称&offset=0` 查阅 50 行。
- `POST /api/plans` 接收含 `base`、`left`、`right` 镜像摘要的 JSON，可选 `scope`（表名数组）。
- `GET /api/plans/{id}` 查看计划、版本与 `scope_report`，追加 `/attempts` 读取重演历史。
- `POST /api/plans/{id}/decisions` 接收 `revision`、`item` 和 `choice`。可选 `base`、`left`、`right`、`clear`。
- `POST /api/plans/{id}/scope` 接收 `revision` 与 `scope`（表名数组；省略或空字符串恢复全表）。
- `POST /api/plans/{id}/amendments` 接收 `revision` 与 `confirm`（派生条目 id 数组）。
- `POST /api/plans/{id}/rehearse`、`/seal`、`/archive` 接收 `revision`。
- `GET /api/plans/{id}/certificate` 下载封存凭证，`/download` 下载 SQLite 镜像。

写入端点也接受网页表单的对应字段。镜像为独立、自包含的文件，未外置日志，三份模式须精确匹配。JSON 行表示使用 `i`、`r`、`t`、`b` 分别标记整数、实数、文本、二进制，`n` 表示 NULL。

## 验证

```sh
python3 -m compileall -q pagefold examples
python3 -m examples.walkthrough all
```

工作流检查使用临时状态库和本机 HTTP 服务。正式自动化测试套件延期。
