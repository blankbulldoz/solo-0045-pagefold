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
- `GET /api/plans/{id}/attempts/{revision}` 回查某一版重演当时固定的逐项裁决与证据；`record` 为 `legacy` 表示升级前没有选择细节的旧记录，`current` 表示带选择快照的新记录。
- `GET /api/plans/{id}/audit` 取得案卷索引：裁决事件流（含同版无变化的幂等重试）、各版重演记录、封存凭证与归档状态，归档后仍可只读回查。
- `POST /api/plans/{id}/decisions` 接收 `revision`、`item` 和 `choice`。可选 `base`、`left`、`right`、`clear`；响应里的 `advanced` 标明本次是否推进版序。
- `POST /api/plans/{id}/rehearse`、`/seal`、`/archive` 接收 `revision`。
- `GET /api/plans/{id}/certificate` 下载封存凭证，`/download` 下载 SQLite 镜像。

写入端点也接受网页表单的对应字段。镜像为独立、自包含的文件，未外置日志，三份模式须精确匹配。JSON 行表示使用 `i`、`r`、`t`、`b` 分别标记整数、实数、文本、二进制，`n` 表示 NULL。

### 历史裁决的可复核入口

每条新版重演记录都固定当时的完整裁决：`choices` 给出每个计划项实际生效的来源角色，`decision_digest` 是裁决集合摘要，与封存凭证中的 `decision_digest` 一致即可证明记录关联到真实重演的选项和版本。显式选择祖本（`choice="base"`、`decided=true`）与清空选择（`choice=null`、`decided=false`）即使最终行相同也分别记录。升级前的旧记录没有这些列，读取时标记为 `record="legacy"`，只保留版本、状态和候选摘要，不会被倒推或篡改。

## 验证

```sh
python3 -m compileall -q pagefold examples
python3 -m examples.walkthrough all
python3 -m examples.migration_check
```

`walkthrough audit` 用真实 HTTP 入口造一条升级前旧记录和多条新记录，覆盖重演重试、同版无变化裁决、祖本/清空区分、封存与归档后的只读回查；`migration_check` 用升级前旧表结构造状态库，验证自动迁移后旧记录仍可查、新记录带选择快照。工作流检查使用临时状态库和本机 HTTP 服务。正式自动化测试套件延期。
