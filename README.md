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
- `POST /api/plans` 接收含 `base`、`left`、`right` 镜像摘要的 JSON。模式不一致时可附 `bridge` 桥接声明（见下节）；附 `"trial": true` 只试算不落库。
- `GET /api/plans/{id}` 查看计划与版本，含桥接结果；追加 `/attempts` 读取重演历史。
- `POST /api/plans/{id}/decisions` 接收 `revision`、`item` 和 `choice`。可选 `base`、`left`、`right`、`clear`。
- `POST /api/plans/{id}/rehearse`、`/seal`、`/archive` 接收 `revision`。
- `GET /api/plans/{id}/certificate` 下载封存凭证，`/download` 下载 SQLite 镜像。

## 结构桥接

分支以不同名称或次序加入同义列时，三份模式不再精确匹配。新建计划可附 `bridge` 声明，显式给出用于合流的共同表和键空间：

```json
{"target": "left",
 "tables": [{"name": "node", "key": ["bench", "slot"],
             "from": {"base": "node", "right": "node"},
             "columns": [{"name": "memo", "from": {"base": "note", "right": "note"}},
                         {"name": "severity", "from": {"base": null}}]}]}
```

`target` 指定共同模式取自哪一方；`key` 须与该方主键一致；`columns` 省略时按同名对应，`from` 中的 `null` 表示该角色缺席此列、以目标列的默认值（或 NULL）填充。桥接结果（共同模式、逐列对应、迁移对原行的影响、原身份校验）随计划保存并在计划页展示；祖本镜像按声明物理迁移（改表名、改列名、补列）后登记为桥接祖本，供原有裁决、重演、封存流程使用。

可无损对应的结构（改名、列次序、带默认值或可空的补列）会被接受；到不了同一目标的部分——未纳入声明的表或列、类型不一致、键空间对不上、缺席且 NOT NULL 无默认值的列、索引或外键映射后不一致——列入试算结果的 `unreachable`，并非空时拒绝创建计划（409 `bridge_unreachable`）。祖本侧的改名列要求列属性与目标完全一致（物理迁移保留列定义），其他角色只要求类型一致。检查约束等目录未记录的结构须操作者自行核对。

写入端点也接受网页表单的对应字段。镜像为独立、自包含的文件，未外置日志，三份模式须精确匹配。JSON 行表示使用 `i`、`r`、`t`、`b` 分别标记整数、实数、文本、二进制，`n` 表示 NULL。

## 验证

```sh
python3 -m compileall -q pagefold examples
python3 -m examples.walkthrough all
python3 -m examples.bridge_merge
```

工作流检查使用临时状态库和本机 HTTP 服务。桥接演练覆盖不同列次序与列重命名两个例子，分别运行试算、原身份校验和合流后约束验证，并展示无法合流结构的识别。正式自动化测试套件延期。
