# Pagefold 数据页合流

把三份有共同祖本的 SQLite 镜像合成一份经过约束校验的独立数据库。网页提供镜像检查、逐行裁决、候选重演、封存和下载。

## 运行

需要 Python 3.11+ 和带 `deserialize`、`serialize` 的 SQLite 3.37+ 构建，无第三方依赖。默认仅监听 `127.0.0.1:8745`，首次启动后访问同一地址。`--state` 指定服务状态数据库。适用于可信的单机操作环境。

```sh
python3 -m pagefold --state ./pagefold-state.sqlite
python3 -m examples.make_images ./demo-images
```

可分别上传 `quartz.sqlite`（祖本）、`cerulean.sqlite`（左支）和 `umber.sqlite`（右支），或在首页直接载入示例并创建计划。初始冲突出现在 `node` 的复合主键 `('north', 1)`，选择右支可成功重演，也可选择祖本或左支。额外示例 `stress-left.sqlite` 和 `stress-right.sqlite` 各自有效，合成后与 `bench` 的唯一约束冲突。首页的“载入分批示例”使用 `bulk-quartz.sqlite`、`bulk-cerulean.sqlite` 和 `bulk-umber.sqlite`，共 49 项待决差异，分布在 `node` 与 `bench` 两表，用于演示按表、按待决状态分批翻阅。

## 分批审阅

计划页默认只看待决行，每页 20 项，可按表切换。地址参数：

- `table` 只读一张表的差异行；`pending=1` 只看待决、`pending=0` 含已裁决。
- `limit` 每页行数，1 至 100，默认 20。
- `cursor` 上一页最后一项的标识；翻页后仍保留 `table`、`pending` 与 `limit`。
- `revision` 打开页面时读到的版序。若计划已推进，页面会显示“链接指向第 N 版，计划现为第 M 版”，当页行与未决数按当前版本读取，而表单仍按 N 版提交，保存会被拒绝为过期版本。
- `saved` 保存后回到列表时标记刚处理的行。该行已离开待决集合时，列表从它之后的下一待决行继续，并显示当前位置（如“第 21–40 项，共 48 项”）。

保存裁决的网页表单携带当前表、筛选、每页行数与翻页位置，提交后以 303 回到同一视图；JSON 写入结果在 `saved.next` 中给出同样的继续地址。某表在此位置之后没有待决行时，页面说明计划其余待决数并提供“从该表开头继续”和“查看该表全部行”。

`GET /api/plans/{id}` 不带参数时仍返回全部行以兼容既有读取；带 `pending`、`table`、`limit`、`cursor`、`revision`、`saved` 时按同一规则分页，响应含 `counts`、`tables`、`view` 和 `saved`。

## 操作和接口

网页中的镜像链接可以检查表和主键行，计划详情显示自动项与未裁决项。保存裁决后重演当前版本，确认通过后封存，最后下载 SQLite 镜像和 JSON 凭证。

- `POST /api/images` 接收 `application/octet-stream`，`X-Image-Name` 是可选的 URL 编码标签。
- `GET /api/images/{id}` 查看镜像目录，追加 `/rows?table=名称&offset=0` 查阅 50 行。
- `POST /api/plans` 接收含 `base`、`left`、`right` 镜像摘要的 JSON。
- `GET /api/plans/{id}` 查看计划与版本，追加 `/attempts` 读取重演历史；计划路由接受 `table`、`pending`、`limit`、`cursor`、`revision`、`saved` 查询参数。
- `POST /api/plans/{id}/decisions` 接收 `revision`、`item` 和 `choice`。可选 `base`、`left`、`right`、`clear`；带 `table`、`pending`、`limit`、`cursor` 时响应附 `saved.next` 继续地址。
- `POST /api/plans/{id}/rehearse`、`/seal`、`/archive` 接收 `revision`。
- `GET /api/plans/{id}/certificate` 下载封存凭证，`/download` 下载 SQLite 镜像。

写入端点也接受网页表单的对应字段。镜像为独立、自包含的文件，未外置日志，三份模式须精确匹配。JSON 行表示使用 `i`、`r`、`t`、`b` 分别标记整数、实数、文本、二进制，`n` 表示 NULL。

## 验证

```sh
python3 -m compileall -q pagefold examples
python3 -m examples.walkthrough all
```

工作流检查使用临时状态库和本机 HTTP 服务。正式自动化测试套件延期。
