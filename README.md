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
- `GET /api/images/{id}/lineage` 读取来源谱系（导入记录、强/弱/缺失来源边、计划角色），追加 `/routes` 只看同等可信的强推导路线。
- `GET /api/lineage/preflight?base=…&left=…&right=…` 或在 `POST /api/plans` 附带 `"preflight": 1`，在建计划前发现三个候选的共同输入与含糊来源，不写入任何计划。
- 上传时可用 `X-Lineage-Claims`（逗号分隔的 64 位摘要）登记操作者口头来源（弱证据），用 `X-Lineage-Missing` 登记来源缺失说明；标签与已登记镜像同名而字节不同会自动产生文件名弱边。

写入端点也接受网页表单的对应字段。镜像为独立、自包含的文件，未外置日志，三份模式须精确匹配。JSON 行表示使用 `i`、`r`、`t`、`b` 分别标记整数、实数、文本、二进制，`n` 表示 NULL。

## 分支谱系与共同输入

镜像按字节摘要寻址，因此“字节身份”和“来源声明”是两种强度不同的证据，谱系把它们分层登记：

- **强证据（可字节核验）**：重新导入与某封存候选逐字节相同的文件得到 `sealed_output` 边；正式计划选定后三个来源各得一条 `plan_role` 边。同一文件可在不同计划里复用为不同角色，同一字节也可能由多条同等可信的强路线解释，`/routes` 一并返回。
- **弱证据（无法仅凭字节核验）**：`X-Lineage-Claims` 的操作者声明、同名改写触发的文件名推测。指向未登记摘要的声明保留为悬空边。
- **无证据**：`X-Lineage-Missing` 登记的来源缺失边，预检时把含缺失边的候选标为含糊。

`python3 -m examples.lineage_demo` 构造两代封存成果：一代计划封存出 C1，C1 回填后在二代计划中改任祖本；脚本展示共同输入预检、含糊/缺失边、角色复用与多条并列强路线。`python3 -m examples.lineage_view_check` 检查谱系网页渲染。

## 验证

```sh
python3 -m compileall -q pagefold examples
python3 -m examples.walkthrough all
python3 -m examples.lineage_demo summary
```

工作流检查使用临时状态库和本机 HTTP 服务。正式自动化测试套件延期。
