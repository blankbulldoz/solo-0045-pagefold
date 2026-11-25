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

写入端点也接受网页表单的对应字段。镜像为独立、自包含的文件，未外置日志，三份模式须精确匹配。JSON 行表示使用 `i`、`r`、`t`、`b` 分别标记整数、实数、文本、二进制，`n` 表示 NULL。

## 验证

```sh
python3 -m compileall -q pagefold examples
python3 -m examples.walkthrough all
python3 -m examples.conformance checks
```

工作流检查使用临时状态库和本机 HTTP 服务。正式自动化测试套件延期。

### 三方合流保证检查

`examples.conformance` 是不依赖 HTTP 的可重复合流保证检查，覆盖存在/缺席、
单改/双改、SQLite 类型键相等与序列化表示的区分，以及重演后的业务值一致性：

```sh
python3 -m examples.conformance checks             # 全部 8 层 40 项断言
python3 -m examples.conformance matrix             # 打印 31 个场景的存在矩阵与分类
python3 -m examples.conformance fixtures ./cf-img  # 导出合成镜像供 sqlite3 手工复查
python3 -m examples.conformance checks --keep DIR  # 保留检查所用状态库
```

- **独立判断依据**：`conformance/oracle.py` 不导入 `pagefold.merge`、
  `pagefold.rows` 或 `pagefold.codec`，仅通过 SQLite 引擎探针（`table_xinfo`、
  全表扫描、`WHERE 主键=?`）与定义性三方规则重算预期计划、预期业务库和
  delete/insert/update 操作痕迹，再与产品实现逐层对照。
- **检查分层**：A 引擎证据（整/实同身份、NULL 键拒绝）、B 表示区分
  （`i`/`r`/`t`/`b` 与 `-0.0` 编码）、C 计划分类、D 冲突项四种裁决的重演业务值、
  E 操作痕迹、F 左右支互换对照、G 错误分类但结果巧合相同的检出、H 持久化入口。
- **巧合相同如何检出**：G 层用 BLOB 亲和键列构造磁盘上分别为整数 `202` 与
  等值实数 `202.0` 的同一行身份；按序列化表示分键的错误孪生会把它拆成
  delete+insert 两项，重演终态与正确分类完全一致，但分区集合与操作痕迹
  两层仍能稳定暴露差异——因此检查不只比较最终数据库。
- **互换左右支**：单边修改项的采纳侧随支系对调翻转（left↔right），双支同值
  项（含双删）固定自动采纳；互换后镜像裁决重演的业务库必须与原计划相同。

