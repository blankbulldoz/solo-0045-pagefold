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

### 行引用见证

合流审核时可从任意一行追实际见证：打开 `/witness`，选择镜像、表并按主键列填值（也可直接给 `key` JSON），或在镜像行窗口、计划项的每份来源里点击“上下游见证”。页面在所选镜像内给出：

- **上游**：该行声明的每个外键（含复合外键）按列序整体匹配到的父行，并标注约束编号、列对应和 `ON DELETE/UPDATE` 来源。
- **下游**：每张表每个外键约束的实际入向引用行；一对多时按约束分组，默认展开前 50 行并报告总数与未展开数量，可用 `limit`（上限 200）放大。
- **NULL 规则**：复合外键任一列是 NULL 时按 SQLite `MATCH SIMPLE` 视为整组无引用，不生成边，并指出责任列，因此不会只凭另一列误配。
- **循环与边界**：有向外键环会被标记成回路而不会无限展开；图按节点去重、默认深度 2、最多 240 个节点，深度外的节点“只收录未展开”并说明，每个见证行都能重新设为起点继续追。
- **镜像隔离与对照**：所有见证只来自 URL 指定的那份镜像；页面底部可一键跳到其他已登记镜像的同表同键，比较来源与候选是否存在。

JSON 形式为 `GET /api/images/{id}/witness?table=名称&key=[["t","north"],["i","1"]]`（同义入口 `/api/witness?image={id}&...`，并兼容 `references`、`refs`、`neighbors`、`graph`、`lineage`、`related`、`trace` 等路径别名；也接受 `pk` 或直接用主键列名传值）。返回 `root`、`upstream`、`downstream`、`nodes`、`edges`、`cycles`、`notices` 与 `truncated`。

`examples/witness_check.py` 会构造 `witness-alpha`、`witness-beta` 两份含外键循环、半截 NULL 复合键和 65 行入向引用的同构镜像，走真实 HTTP 证明上述行为及“相同键只得到所查镜像的见证”。

- `POST /api/images` 接收 `application/octet-stream`，`X-Image-Name` 是可选的 URL 编码标签。
- `GET /api/images/{id}` 查看镜像目录，追加 `/rows?table=名称&offset=0` 查阅 50 行。
- `POST /api/plans` 接收含 `base`、`left`、`right` 镜像摘要的 JSON。
- `GET /api/plans/{id}` 查看计划与版本，追加 `/attempts` 读取重演历史。
- `POST /api/plans/{id}/decisions` 接收 `revision`、`item` 和 `choice`。可选 `base`、`left`、`right`、`clear`。
- `POST /api/plans/{id}/rehearse`、`/seal`、`/archive` 接收 `revision`。
- `GET /api/plans/{id}/certificate` 下载封存凭证，`/download` 下载 SQLite 镜像。

写入端点也接受网页表单的对应字段。镜像为独立、自包含的文件，未外置日志，三份模式须精确匹配。JSON 行表示使用 `i`、`r`、`t`、`b` 分别标记整数、实数、文本、二进制，`n` 表示 NULL。

## 验证

```sh
python3 -m compileall -q pagefold examples
python3 -m examples.walkthrough all
python3 -m examples.witness_check
```

工作流检查使用临时状态库和本机 HTTP 服务。正式自动化测试套件延期。
