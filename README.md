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
python3 -m examples.blockers_demo
```

`blockers_demo` 用真实 `rebuild` 与真实 HTTP 入口演示阻断集合：两个相互独立的约束组（CHECK 与 UNIQUE/外键）会在同一次结果中收全，颠倒输入次序得到同一份稳定集合；还演示同一字段 CHECK 先报错、掩盖其外键错误的情况，以及修复后（先露出外键、再完全通过）的结果对比。

### 阻断集合

受阻的重演结果除保留原有 `problem: {code, detail}`（首要原因，契约不变）外，还带顶层 `blockers`：

- `verified[]`：本次重建由 SQLite 实际验证的原因（INSERT 约束、`integrity_check`、`foreign_key_check`），含表、编码主键、约束引用、原始信息和处置提示；
- `unchecked[]`：目标行未能写入、无法继续用库内检查复核的条目，回链拦截它的 `verified` 编号，并列出关联外键在计划目标态中的状态；
- `gaps[]`：尚不能下结论但仍影响恢复的缺口，典型是被首个错误掩盖的外键（`code: masked_foreign_key`），会给出缺失父键与回链；另有检查截断、检查未能完成等缺口；
- `items[]` 是上述三类按 `state` 标记的扁平合集，`counts` 给出三类计数。

收集是有界的：不增加额外试算轮次，只把同一批写入继续做完，并运行既有的完整性/外键 PRAGMA；行身份定位仅针对 `foreign_key_check` 已报出的违规。受阻时仍不提交、不产生候选字节；同版重演继续返回同一份缓存证据。

工作流检查使用临时状态库和本机 HTTP 服务。正式自动化测试套件延期。
