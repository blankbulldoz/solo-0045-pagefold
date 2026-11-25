# Pagefold 数据页合流

把三份有共同祖本的 SQLite 镜像合成一份经过约束校验的独立数据库。网页提供镜像检查、逐行裁决、候选重演、封存和下载。

## 运行

需要 Python 3.11+ 和带 `deserialize`、`serialize` 的 SQLite 3.37+ 构建，无第三方依赖。服务以标准 PEP 3333 WSGI 应用提供，默认仅监听 `127.0.0.1:8745`，首次启动后访问同一地址。`--state` 指定服务状态数据库。适用于可信的单机操作环境。

### 标准 WSGI 启动

模块级 `pagefold.wsgi:application` 是标准 WSGI 入口，可被任何 WSGI 服务器直接引用，通过环境变量配置（首次请求时才打开状态库，兼容多 worker 与预导入）：

| 环境变量 | 说明 | 默认 |
| --- | --- | --- |
| `PAGEFOLD_STATE` | 服务状态数据库路径 | `pagefold-state.sqlite` |
| `PAGEFOLD_ALLOWED_HOSTS` | 允许的 `Host` 白名单，逗号分隔的 `主机:端口` | 空（需显式配置） |

```sh
# gunicorn（生产）
PAGEFOLD_STATE=./pagefold-state.sqlite \
PAGEFOLD_ALLOWED_HOSTS=127.0.0.1:8000,localhost:8000 \
  gunicorn -w 4 -b 127.0.0.1:8000 'pagefold.wsgi:application'

# uWSGI
PAGEFOLD_STATE=./pagefold-state.sqlite PAGEFOLD_ALLOWED_HOSTS=127.0.0.1:8000 \
  uwsgi --http 127.0.0.1:8000 --module pagefold.wsgi:application

# 标准库 WSGI 运行器（本机演练，零依赖）
python3 -m pagefold --state ./pagefold-state.sqlite
python3 -m examples.make_images ./demo-images
```

经反向代理部署时，把对外的 `主机:端口` 加入 `PAGEFOLD_ALLOWED_HOSTS`（或 stdlib 运行器的 `--allowed-host`），浏览器表单提交后的 303 回跳地址仍是相对路径，代理无需改写 Location。应用在 `SCRIPT_NAME` 前缀挂载时会自动给回跳加前缀。

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
python3 -m examples.wsgi_drill --report wsgi-migration-report.json
```

工作流检查使用临时状态库和本机 WSGI 服务。正式自动化测试套件延期。

### HTTP→WSGI 迁移对照演练

`examples/wsgi_drill` 在同一进程并排拉起迁移前的 `http.server` 协议（`examples/_legacy_http.py`，仅用于对照）与迁移后的标准 WSGI 应用，喂入完全相同的请求序列并逐项核对，结果写入 `--report` 指定的 JSON：

1. **同构工作流**：34 个步骤逐跳比较状态码、内容类型与归一化响应摘要（计划 ID/镜像摘要做占位归一化）；
2. **版本 / 来源 / 摘要**：三方镜像 SHA-256、`candidate_digest`、候选体积、模式摘要、revision 推进轨迹两栈完全一致；新栈响应带 `X-Pagefold-Entry: wsgi` 标记，旧栈无此头；
3. **失败语义矩阵**：12 类失败的状态码与问题 `code` 一一对应（如 422 `image_header`、415 `media_type`、411 `content_length`、409 `stale_revision`、403 `origin`/`host`、501 不支持方法），另对两栈共用的 `inputs.payload` 直接验证 411/400/413 约束；
4. **上限附近请求**：逼近 4 MiB 的合法镜像（约 3.9 MiB）登记成功，超限 413、恰满但结构非法 422、JSON 写入超 32 KiB 413，沿用原约束；
5. **多 worker 并发同一计划**：4 个 worker 基于同一 revision 同时裁决，恰有一个写入把版本推进到 1，其余变更写入收 `stale_revision` 409（`clear` 幂等空操作除外）；并发重演收敛到同一候选；并确定性验证 `SQLITE_BUSY → 503 state_busy`；
6. **代理转发与表单回跳**：经反向代理保留原始 `Host` 的 `POST /demo` 返回 303 相对回跳并可正常打开计划页，伪造 Host 被白名单拒绝（403），`SCRIPT_NAME=/pf` 前缀挂载的回跳自动带上前缀。

`mismatches` 为空即表示新 WSGI 入口完整保留了旧协议的浏览器用法、JSON、镜像上传、错误、重定向与候选下载语义。
