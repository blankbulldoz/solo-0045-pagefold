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

## 实体键与主键改名

默认计划仍是原来的逐行模式：主键文本从 `alpha` 改成 `alpha-l` 会显示为“删除 `alpha` + 插入 `alpha-l`”两个条目。若另有一组保持不变的唯一列，可在创建计划时明确把它指定为实体键：

```json
{"base":"…","left":"…","right":"…",
 "entity_keys":[{"table":"account","columns":["tenant","uid"]}]}
```

也接受 `{"account":["uid"]}`、`stable_keys`、`natural_keys`、`identity_keys` 等写法。新模式会返回 `entities`/`renames`：同一实体在祖本、左支、右支中的键被合并为一个裁决组。左右支改成不同候选键时，`accept` 必须用 `side` 指明接受哪一支；`base` 或 `reject` 表示拒绝改名。明确的 `accepted`/`rejected` 结果保存在计划裁决中，候选键被其他实体占用时，重演仍会被主键或唯一约束拦截，但裁决不会丢失。

复合外键跟随实体裁决处理：仅由外键值引起的子表差异不再要求第二次逐行裁决；其他未被行操作覆盖的引用会在重演时按实体的旧键→目标键映射重写。相互交换主键时先删全部旧键再插入目标键，避免临时碰撞，封存前仍执行真实的 `PRAGMA foreign_key_check`。

```sh
python3 -m examples.entity_rename
```

该演练用新增/删除、左右支异键、候选键被抢占和相互占键四个例子对比两种模式，并实际封存、反序列化和检查复合外键。

## 验证

```sh
python3 -m compileall -q pagefold examples
python3 -m examples.walkthrough all
python3 -m examples.entity_rename
```

工作流检查使用临时状态库和本机 HTTP 服务。正式自动化测试套件延期。
