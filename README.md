# Pagefold 数据页合流

把三份有共同祖本的 SQLite 镜像合成一份经过约束校验的独立数据库。网页提供镜像检查、逐行裁决、候选重演、封存和下载。

## 运行

需要 Python 3.11+ 和带 `deserialize`、`serialize` 的 SQLite 3.37+ 构建，无第三方依赖。默认仅监听 `127.0.0.1:8745`，首次启动后访问同一地址。`--state` 指定服务状态数据库。适用于可信的单机操作环境。

```sh
python3 -m pagefold --state ./pagefold-state.sqlite
python3 -m examples.make_images ./demo-images
```

可分别上传 `quartz.sqlite`（祖本）、`cerulean.sqlite`（左支）和 `umber.sqlite`（右支），或在首页直接载入示例并创建计划。首页“规则冲突示例”会载入同一批删除/修改、双支改写、权重变更和 SQL NULL 例外冲突及一组覆盖/重叠/例外规则。初始冲突出现在 `node` 的复合主键 `('north', 1)`，选择右支可成功重演，也可选择祖本或左支。额外示例 `stress-left.sqlite` 和 `stress-right.sqlite` 各自有效，合成后与 `bench` 的唯一约束冲突。

## 操作和接口

网页中的镜像链接可以检查表和主键行，计划详情显示自动项与未裁决项。保存裁决后重演当前版本，确认通过后封存，最后下载 SQLite 镜像和 JSON 凭证。

- `POST /api/images` 接收 `application/octet-stream`，`X-Image-Name` 是可选的 URL 编码标签。
- `GET /api/images/{id}` 查看镜像目录，追加 `/rows?table=名称&offset=0` 查阅 50 行。
- `POST /api/plans` 接收含 `base`、`left`、`right` 镜像摘要的 JSON。
- `GET /api/plans/{id}` 查看计划与版本，追加 `/attempts` 读取重演历史。
- `POST /api/plans/{id}/decisions` 接收 `revision`、`item` 和 `choice`。可选 `base`、`left`、`right`、`clear`。
- `POST /api/plans/{id}/rehearse`、`/seal`、`/archive` 接收 `revision`。
- `GET /api/plans/{id}/certificate` 下载封存凭证，`/download` 下载 SQLite 镜像。
- `GET /api/rules`、`POST /api/rules` 管理选行习惯规则；网页 `/rules` 可新建规则，也可填入计划 ID 先试算未保存草稿。
- `GET /api/plans/{id}/suggestions` 用已保存规则试算；`POST /api/plans/{id}/suggestions` 可带未保存 `rule` 或 `rules` 草稿，不会推进版本或写裁决。
- `python3 -m examples.rule_trial` 用同一批四类冲突演示覆盖、同优先级重叠分歧、例外，以及人工写入前后的版本和重演结果。

规则试算返回每条命中规则的优先级、意见和结构化理由。条件支持 `all` / `any` / `not`，以及 `row_state`、`change_kind`、`branch_changed`、`column_state`、`column_changed`、`only_changed_columns`、`delete_vs_edit`。`row_state.state="absent"` 表示该主键的整行在分支中不存在；`column_state.state="sql_null"` 表示行存在但该单元格是 SQL `NULL`，`not_null` 才检查非空值。多个规则意见不同时，最高优先级唯一意见仅作为建议；同优先级仍分歧会标记为 `contested`，不产生自动结论。采纳仍调用既有 `POST /api/plans/{id}/decisions`，因此继续受当前 `revision`、幂等重试和计划状态约束。

写入端点也接受网页表单的对应字段。镜像为独立、自包含的文件，未外置日志，三份模式须精确匹配。JSON 行表示使用 `i`、`r`、`t`、`b` 分别标记整数、实数、文本、二进制，`n` 表示 NULL。

## 验证

```sh
python3 -m compileall -q pagefold examples
python3 -m examples.walkthrough all
python3 -m examples.rule_trial
```

工作流检查使用临时状态库和本机 HTTP 服务。正式自动化测试套件延期。
