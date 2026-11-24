# Pagefold 数据页合流

把三份有共同祖本的 SQLite 镜像合成一份经过约束校验的独立数据库。网页提供镜像检查、逐行裁决、候选重演、封存和下载。

## 运行

需要 Python 3.11+ 和带 `deserialize`、`serialize` 的 SQLite 3.37+ 构建，无第三方依赖。默认仅监听 `127.0.0.1:8745`，首次启动后访问同一地址。`--state` 指定服务状态数据库。适用于可信的单机操作环境。

```sh
python3 -m pagefold --state ./pagefold-state.sqlite
python3 -m examples.make_images ./demo-images
# 配置组织签名密钥环（演练密钥）后即可颁发交接凭证
python3 -m pagefold --state ./pagefold-state.sqlite --keys ./pagefold-keys --init-keys
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
- `POST /api/plans/{id}/handover` 颁发组织交接凭证（幂等），`GET` 下载；`GET /api/keyring` 取得公开密钥环。

写入端点也接受网页表单的对应字段。镜像为独立、自包含的文件，未外置日志，三份模式须精确匹配。JSON 行表示使用 `i`、`r`、`t`、`b` 分别标记整数、实数、文本、二进制，`n` 表示 NULL。

## 交接凭证与签名密钥轮换

封存凭证只说明候选数据库的推导来源；交接凭证（`pagefold-handover-v1`）由组织 Ed25519 密钥对封存凭证的规范字节摘要、`base/left/right` 角色位置、模式/计划/裁决/候选摘要逐项签名确认。两个计划即使产生字节完全相同的候选，只要左右角色不同，待签内容就不同，不能互相代用。

- 密钥环（`pagefold-keyring-v1`）的信任锚自签名；每次轮换生成一条由旧密钥签名的轮换记录，旧密钥标记 `retired`，新密钥成为唯一活动密钥。
- 历史交接凭证继续用公开环中的旧公钥验证（可跨轮换），但服务端只能用当前活动密钥新签，旧密钥无法再签发新凭证。
- 收件方只需要公开材料：`ring.json`、`*.pub.pem`、封存凭证、交接凭证和可选的 SQLite 文件，验证全程离线，不接触私钥。
- `python3 -m pagefold.ed25519` 可单独运行 RFC 8032 测试向量自检；纯标准库签名与 OpenSSL 3 的 PEM/验证互通。

```sh
python3 -m examples.keyring_cli init ./pagefold-keys --organization 示例组织
python3 -m examples.keyring_cli rotate ./pagefold-keys --note 年度轮换
python3 -m examples.keyring_cli export ./pagefold-keys ./public-materials
python3 -m examples.keyring_cli verify ./public-materials/ring.json
python3 -m examples.verify_handover --ring ./public-materials/ring.json \
  --handover handover.json --certificate certificate.json --sqlite candidate.sqlite
```

## 验证

```sh
python3 -m compileall -q pagefold examples
python3 -m examples.walkthrough all
python3 -m pagefold.ed25519
python3 -m examples.handover_drill
```

工作流检查使用临时状态库和本机 HTTP 服务。`handover_drill` 覆盖轮换前后签署、旧凭证跨轮换离线验证、角色互换不可代用、篡改拒绝与 OpenSSL 交叉核验。正式自动化测试套件延期。
