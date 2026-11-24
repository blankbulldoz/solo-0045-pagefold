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

网页中的镜像链接可以检查表和主键行，计划详情显示自动项与未裁决项。保存裁决后重演当前版本；重演通过后，计划页会列出**将要封存的证据**（候选 SHA-256、大小、版序、计划/裁决摘要、祖本/左支/右支来源），勾选“确认摘要与来源”后才完成封存，最后下载 SQLite 镜像和 JSON 凭证。

### 封存确认与多标签现场

封存确认把操作者所见的证据绑定到请求：`plan`、`revision`、`candidate_digest`、`sources`。网页表单回显全部字段并要求勾选 `confirmed=yes`；JSON 客户端可先取证据对象，再原样作为 `confirmation` 提交。

- `GET /api/plans/{id}/seal`（别名 `/confirmation`）返回当前版可封存证据与待回显的 `confirmation`；当前版没有通过候选时 `sealable=false`。
- `POST /api/plans/{id}/seal` 接收 `revision` 与明确确认。缺少确认返回 `409 confirmation_required`（同时给出 `evidence` 与 `confirmation` 模板）。
- **过期确认**：确认的版序或候选摘要已不是当前证据，返回 `409 stale_confirmation`，其中 `confirmed` 是旧对象、`current` 是**新的比较对象**（新版序、新摘要、来源、最近重演）；网页会渲染左右对照表。
- **指向另一计划的确认**：确认中的 `plan` 与 URL 计划不同，返回 `409 confirmation_plan_mismatch`，带 `confirmation_plan`、`other_plan` 与当前计划的 `current`。即使两个计划版序相同、候选摘要逐字节相同，也不能交叉封存。
- **成功后响应丢失的重试**：原样重发同一确认会重认同一条 `releases` 记录并返回完全相同的凭证，不新增封存行；不带确认的旧客户端重试也会幂等取回同一凭证。已封存计划若收到与凭证不符的完整确认，返回 `409 seal_conflict`。

`examples/seal_confirmation_demo.py` 用两份左右支互换、冲突选择镜像到对应角色的计划，得到逐字节相同的候选，走查上述全部情形并核对界面 HTML、状态库封存记录、凭证与下载字节：

```sh
python3 -m examples.seal_confirmation_demo --out ./demo-artifacts
```

## 接口

- `POST /api/images` 接收 `application/octet-stream`，`X-Image-Name` 是可选的 URL 编码标签。
- `GET /api/images/{id}` 查看镜像目录，追加 `/rows?table=名称&offset=0` 查阅 50 行。
- `POST /api/plans` 接收含 `base`、`left`、`right` 镜像摘要的 JSON。
- `GET /api/plans/{id}` 查看计划与版本，追加 `/attempts` 读取重演历史，追加 `/seal` 读取封存确认证据。
- `POST /api/plans/{id}/decisions` 接收 `revision`、`item` 和 `choice`。可选 `base`、`left`、`right`、`clear`。
- `POST /api/plans/{id}/rehearse`、`/seal`、`/archive` 接收 `revision`；`/seal` 另需上文的明确确认。
- `GET /api/plans/{id}/certificate` 下载封存凭证，`/download` 下载 SQLite 镜像。

写入端点也接受网页表单的对应字段。镜像为独立、自包含的文件，未外置日志，三份模式须精确匹配。JSON 行表示使用 `i`、`r`、`t`、`b` 分别标记整数、实数、文本、二进制，`n` 表示 NULL。

## 验证

```sh
python3 -m compileall -q pagefold examples
python3 -m examples.walkthrough all
```

工作流检查使用临时状态库和本机 HTTP 服务。正式自动化测试套件延期。
