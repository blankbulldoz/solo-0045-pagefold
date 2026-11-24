# Pagefold 数据页合流

把三份有共同祖本的 SQLite 镜像合成一份经过约束校验的独立数据库。网页提供镜像检查、逐行裁决、候选重演、封存和下载。

## 运行

需要 Python 3.11+ 和带 `deserialize`、`serialize` 的 SQLite 3.37+ 构建，无第三方依赖。默认仅监听 `127.0.0.1:8745`，首次启动后访问同一地址。`--state` 指定服务状态数据库。适用于可信的单机操作环境。

```sh
python3 -m pagefold --state ./pagefold-state.sqlite
python3 -m examples.make_images ./demo-images
```

可分别上传 `quartz.sqlite`（祖本）、`cerulean.sqlite`（左支）和 `umber.sqlite`（右支），或在首页直接载入示例并创建计划。初始冲突出现在 `node` 的复合主键 `('north', 1)`，选择右支可成功重演，也可选择祖本或左支。额外示例 `stress-left.sqlite` 和 `stress-right.sqlite` 各自有效，合成后与 `bench` 的唯一约束冲突。

## 两种镜像身份

镜像始终有两个分开维护、分开可查的身份：

- **文件身份（精确摘要）**：登记主键 `id` 仍是原始字节的 SHA-256。对镜像执行 `VACUUM`、重写或整理空闲页后，即使目录和行内容完全没变，字节摘要也会不同，因此会登记成新的镜像。计划的 `sources` 和封存凭证的 `sources` 精确绑定这一文件摘要，历史来源不随后续登记改变。
- **内容身份（`content_digest`）**：对模式签名和保留存储类型的全部行计算的摘要。`VACUUM` 后逻辑等价的两份镜像共享它；但相同行数、相同模式、相同行位置并不足以得到它——整数 `1` 与文本 `"1"`、NULL 与空 BLOB `x''` 的类型编码不同，内容摘要必然不同。

登记响应、`GET /api/images/{id}` 与镜像页同时给出两种身份；镜像页列出“碎片整理后等价的来源”“行位置相同但类型不同的来源”以及使用该镜像的计划和凭证。计划页的“来源身份对照”可逐对打开类型化逐单元格比较：

- `GET /api/images/{id}/identity`：只取身份、等价件、类型漂移件和使用记录。
- `GET /api/images/compare?a={id}&b={id}`：两份镜像的逐行位置对照（JSON）；网页版为 `/images/compare?a=...&b=...`。
- 凭证在 `sources`（精确文件摘要）之外附加 `source_content`（三个角色的内容摘要），旧凭证不会因之后登记 VACUUM 件而变化。

`examples/make_images.py` 会额外生成 `identity-base.sqlite`、`identity-vacuum.sqlite`（碎片后 VACUUM 的等价件）和 `identity-type-shift.sqlite`（同行位置但整数变文本、NULL 变空 BLOB），可直接上传演示。首页“身份对照样本”按钮会一键布置同一组场景：先封存使用旧原件的计划，再登记 VACUUM 件与类型漂移件，随后可在镜像页和计划页查看全部三种展示。

## 操作和接口

网页中的镜像链接可以检查表和主键行，计划详情显示自动项与未裁决项。保存裁决后重演当前版本，确认通过后封存，最后下载 SQLite 镜像和 JSON 凭证。

- `POST /api/images` 接收 `application/octet-stream`，`X-Image-Name` 是可选的 URL 编码标签。
- `GET /api/images/{id}` 查看镜像目录（含 `content_digest` 两种身份、等价件、类型漂移件和使用记录），追加 `/rows?table=名称&offset=0` 查阅 50 行；`/identity` 只取身份关系。
- `GET /api/images/compare?a={id}&b={id}`（或 `/api/images/{id}/compare/{other}`）输出逐行位置、逐单元格存储类型的身份对照。
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
python3 -m examples.walkthrough identity
```

工作流检查使用临时状态库和本机 HTTP 服务。`identity` 检查专门覆盖两种身份：VACUUM 等价件归组、整数/文本与 NULL/空 BLOB 的类型漂移对照、旧镜像在新件登记后的页面与凭证不变。正式自动化测试套件延期。
