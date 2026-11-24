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

## 原始镜像回收

长期保留计划后，可以用两阶段的回收清空不再需要的原始镜像字节：先 `GET /api/reclaims` 预审，返回保留清单（附理由与引用角色）、可回收清单和预审摘要；确认时 `POST /api/reclaims` 携带该摘要。网页入口是 `/reclaims`。

- 保留来源：仍由开放计划（可继续重演）、已封存结果，或已归档但可复核凭证的计划所用。
- 同一文件的多个引用按摘要只出现一次，体积只计一次；重复登记不会重复计费。
- 预审摘要覆盖全部镜像记录与计划引用。预审之后登记新镜像、创建新计划或改变计划状态，都会让旧摘要失效，`POST` 返回 `stale_report` 并保留新引用。
- 执行只清空可回收镜像的原始字节并标记 `reclaimed`，目录、摘要、计划、裁决、重演历史、封存凭证与已下载成果保持可读。已回收镜像的行查询返回 `image_reclaimed`（410），也不能再作为新计划的来源。
- 重复提交同一摘要返回同一执行结果；记录可按摘要（`/api/reclaims/{digest}`）与历史（`/api/reclaims/history`）查询。

写入端点也接受网页表单的对应字段。镜像为独立、自包含的文件，未外置日志，三份模式须精确匹配。JSON 行表示使用 `i`、`r`、`t`、`b` 分别标记整数、实数、文本、二进制，`n` 表示 NULL。

## 验证

```sh
python3 -m compileall -q pagefold examples
python3 -m examples.walkthrough all
```

工作流检查使用临时状态库和本机 HTTP 服务。`reclaim-preview` 与 `reclaim-confirm` 覆盖共用来源、封存/归档结果、去重体积、过期预审和保留历史。正式自动化测试套件延期。
